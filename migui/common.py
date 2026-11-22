"""What migui.web and migui.runner share: the input whitelist (DESIGN.md section 8.1), the scrubber (T9), the confirmation
rule (DESIGN.md section 8.2), the login name rule (section 8.3), JSON lines and the audit file (T12).

Both processes check every field with these functions: the web first (an early 400), the runner again with the
values of its own 'migrate inventory --json' run (it never trusts the web, T7/T14).
"""
import ipaddress
import json
import os
import re
import time

# 6.1: an ident is a UUID or a plain name (never '/', never a leading '-').
IDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\Z")
# host, project, volume-type, az, volume-az, flavor, server-group, security-group (each)
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")
# a value the runner's own inventory lists (volume type, AZ, host, flavor): a subset of what Cinder/Nova allow that
# takes Cinder's "__DEFAULT__" type (leading "_", up to 255). No whitespace (the state's "options" line is split on it),
# no leading "-" (an argv value), no Unicode or "/". The runner also checks it is in its inventory. Anchored with \Z.
INV_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._:@+-]{0,254}\Z")
JOB_ID_RE = re.compile(r"^[0-9a-f]{16}\Z")
# 16.1: a login name (lower case only, so that "Admin" and "admin" are never two accounts)
USER_RE = re.compile(r"^[a-z][a-z0-9._-]{0,31}\Z")
TOKEN_HEX_RE = re.compile(r"^[0-9a-f]{32}\Z")
MAX_ITEMS = 200          # VMs or volumes in one plan
MAX_LINE = 64 * 1024     # one socket message (T15: the HTTP body limit too)

# Option name (as the UI sends it) -> (CLI option, kind, argument). Order = the order of the command line.
#   enum: allowed values; inv: a set of the runner's inventory; name/names: NAME_RE (names: comma list);
#   int: (low, high); flag: True only; ip: same|new (per-row addresses are item fields).
VM_SPEC = (
    ("mode", "--mode", "enum", ("cold", "live")),
    ("ip", "--ip", "enum", ("same", "new")),
    ("volume_type", "--volume-type", "inv", "volume_types"),
    ("az", "--az", "inv", "zones_compute"),
    ("volume_az", "--volume-az", "inv", "zones_volume"),
    ("dst_host", "--dst-host", "inv", "dst_hosts"),
    ("flavor", "--flavor", "inv", "flavors"),
    ("flavor_fallback", "--flavor-fallback", "flag", None),
    ("security_group", "--security-group", "names", None),
    ("no_copy_secgroup", "--no-copy-secgroup", "flag", None),
    ("server_group", "--server-group", "name", None),
    ("no_server_group", "--no-server-group", "flag", None),
    ("verify", "--verify", "enum", ("readback", "full", "none")),
    ("max_passes", "--max-passes", "int", (1, 50)),
    ("delta_limit", "--delta-limit", "int", (0, 65536)),
    ("start_source", "--start-source", "flag", None),
    ("keep_source_ports", "--keep-source-ports", "flag", None),
    ("volumes_only", "--volumes-only", "flag", None),
    ("ephemeral_as_image", "--ephemeral-as-image", "flag", None),  # 1.0.0 (A9)
)
VOL_SPEC = (
    ("volume_type", "--volume-type", "inv", "volume_types"),
    ("az", "--az", "inv", "zones_compute"),
    ("volume_az", "--volume-az", "inv", "zones_volume"),
    ("verify", "--verify", "enum", ("readback", "full", "none")),
    ("in_use", "--in-use", "flag", None),
)
SPECS = {"vm": VM_SPEC, "vol": VOL_SPEC}


class Reject(Exception):
    """A request refused before anything runs: HTTP status + message (no value of the request is echoed raw)."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def check_ident(v, what="ident"):
    if not isinstance(v, str) or not (UUID_RE.match(v) or IDENT_RE.match(v)):
        raise Reject(400, "%s: not a UUID or a name of letters, digits, '.', '_', '-' (at most 64)" % what)
    return v


def check_name(v, what):
    if not isinstance(v, str) or not NAME_RE.match(v):
        raise Reject(400, "%s: not a valid name" % what)
    return v


def check_ipv4(v, what="ip"):
    if not isinstance(v, str) or len(v) > 15:
        raise Reject(400, "%s: not an IPv4 address" % what)
    try:
        ipaddress.IPv4Address(v)
    except ValueError:
        raise Reject(400, "%s: not an IPv4 address" % what)
    return v


def normalize_options(kind, options, inv=None):
    """The options of one batch as a canonical command-line list (same input -> same list: the confirm token is
    bound to it). inv: the runner's inventory sets ({name: set}); None = the web's structural check only."""
    spec = SPECS.get(kind)
    if spec is None:
        raise Reject(400, "kind must be vm or vol")
    if not isinstance(options, dict):
        raise Reject(400, "options must be an object")
    known = {k for k, *_ in spec}
    for k in options:
        if k not in known:
            raise Reject(400, "unknown option %s" % (k if isinstance(k, str) and NAME_RE.match(k) else "(invalid)"))
    argv = []
    for key, opt, typ, arg in spec:
        v = options.get(key)
        if v is None or v == "" or v is False:
            continue
        if typ == "flag":
            if v is not True:
                raise Reject(400, "%s takes true or false" % key)
            argv.append(opt)
        elif typ == "enum":
            if v not in arg:
                raise Reject(400, "%s takes %s" % (key, "|".join(arg)))
            argv += [opt, v]
        elif typ == "int":
            if isinstance(v, bool) or not isinstance(v, int) or not arg[0] <= v <= arg[1]:
                raise Reject(400, "%s takes an integer %d-%d" % (key, arg[0], arg[1]))
            argv += [opt, str(v)]
        elif typ == "name":
            argv += [opt, check_name(v, key)]
        elif typ == "names":
            if not isinstance(v, list) or not v or len(v) > 16:
                raise Reject(400, "%s takes a list of 1-16 names" % key)
            argv += [opt, ",".join(check_name(x, key) for x in v)]
        elif typ == "inv":
            if not isinstance(v, str) or not INV_NAME_RE.fullmatch(v):  # fullmatch: '$' would let a trailing \n pass
                raise Reject(400, "%s: not a valid name" % key)
            if inv is not None and v not in inv.get(arg, ()):
                raise Reject(400, "%s: %s is not in the inventory of this runner (run inventory again)" % (key, v))
            argv += [opt, v]
    if kind == "vm":
        if options.get("no_server_group") and options.get("server_group"):
            raise Reject(400, "server_group and no_server_group exclude each other")
        if options.get("no_copy_secgroup") and options.get("security_group"):
            raise Reject(400, "security_group and no_copy_secgroup exclude each other")
    return argv


def check_items(kind, items):
    """[{"ident": ..., "ip": optional IPv4 (vm)}] -> [(ident, ip)]; no duplicates."""
    if not isinstance(items, list) or not items or len(items) > MAX_ITEMS:
        raise Reject(400, "items: 1-%d entries" % MAX_ITEMS)
    out, seen = [], set()
    for it in items:
        if not isinstance(it, dict) or set(it) - {"ident", "ip"}:
            raise Reject(400, "an item is {ident, ip}")
        ident = check_ident(it.get("ident"))
        ip = it.get("ip") or ""
        if ip:
            if kind != "vm":
                raise Reject(400, "ip is a VM option")
            check_ipv4(ip)
        if ident in seen:
            raise Reject(400, "an ident appears twice")
        seen.add(ident)
        out.append((ident, ip))
    return out


def confirm_rule(kind, names, by_name, idents=None):
    """14.2: what the operator must type. names: the display name of each job; idents: the ident of each (D5: the
    full ident typed instead of a name is accepted too). by_name: keep-source-ports, a typed --ip address,
    --start-source, --in-use (from the effective options, D2), or a rollback.
    -> {"mode": "count"|"name", "expect": [...], "ids": [...]}"""
    if len(names) == 1 or by_name:
        rule = {"mode": "name", "expect": list(names)}
        if idents is not None:
            rule["ids"] = list(idents)
        return rule
    return {"mode": "count", "expect": [str(len(names))]}


def typed_lines(typed):
    """D5: the typed confirmation as its lines: one name per line, kept whole (a name may hold spaces or commas); the
    line end and blank lines are dropped, nothing else. A JSON list is taken as the lines."""
    if isinstance(typed, list):
        parts = [x for x in typed if isinstance(x, str)]
    elif isinstance(typed, str):
        parts = typed.replace("\r\n", "\n").split("\n")
    else:
        return None
    return [p.strip("\r") for p in parts if p.strip()]


def typed_matches(rule, typed):
    """The typed text against the rule: the count exactly (one line); for names, every job named exactly once, each
    line being the job's name or (D5) its full ident. A name rule may carry "alt" (19.2, rollback only: [ident]): that
    exact text is accepted too."""
    parts = typed_lines(typed)
    if parts is None:
        return False
    if rule["mode"] == "count":
        return [p.strip() for p in parts] == rule["expect"]
    if rule.get("alt") and parts == list(rule["alt"]):
        return True
    names = list(rule["expect"])
    ids = list(rule.get("ids") or [None] * len(names))
    if len(parts) != len(names):
        return False
    free = list(range(len(names)))
    # idents first (unique), then names (a name two VMs share takes one line per VM)
    rest = []
    for p in parts:
        hit = next((i for i in free if ids[i] is not None and ids[i] == p), None)
        if hit is None:
            rest.append(p)
        else:
            free.remove(hit)
    for p in rest:
        hit = next((i for i in free if names[i] == p), None)
        if hit is None:
            return False
        free.remove(hit)
    return not free


# --- T9 / B1: scrub every line that leaves the runner ----------------------------------------------------------------
SCRUBBED = "[scrubbed]"
SCRUB = (
    (re.compile(r"OS_PASSWORD\s*=\s*\S*"), SCRUBBED),
    (re.compile(r"OS_APPLICATION_CREDENTIAL_SECRET\s*=\s*\S*"), SCRUBBED),
    (re.compile(r"OS_TOKEN\s*=\s*\S*"), SCRUBBED),
    (re.compile(r"(?i)X-Auth-Token\s*[:=]?\s*\S*"), SCRUBBED),
    (re.compile(r"(?i)\bkey\s*=\s*AQ[A-Za-z0-9+/=]+"), SCRUBBED),
    (re.compile(r"AQ[A-Za-z0-9+/=]{30,}"), SCRUBBED),
    # B1: a credential as a JSON or Python value ("password": "...", 'secret': '...'), and in a URL (https://u:p@h)
    (re.compile(r"""(?i)(["']?[\w.-]*(?:password|secret|token)["']?\s*[:=]\s*)(["'])(?:(?!\2).)*\2"""),
     r"\1\2" + SCRUBBED + r"\2"),
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^/\s:@]+):[^@\s/]+@"), r"\1:" + SCRUBBED + "@"),
)
# B1: keys whose value never leaves the runner whatever it is (jsonout.SECRET_KEY of the CLI, minus the plain
# 'token' the confirmation of a batch is called by the runner itself)
SECRET_KEY = re.compile(r"(?i)(^OS_|PASSWORD|SECRET|_TOKEN$|^X-AUTH-TOKEN$|AUTH_URL|CACERT|KEYRING|^AQ)")
# B1: the values of OS_PASSWORD / OS_APPLICATION_CREDENTIAL_SECRET / OS_TOKEN of both openrcs (set by the runner from
# its probe; empty in the web, which never sees them). Replaced wherever they appear.
SECRETS = set()
SECRET_MIN = 6   # a shorter value is replaced only as a whole word


def scrub(text):
    for rx, repl in SCRUB:
        text = rx.sub(repl, text)
    for s in SECRETS:
        if len(s) >= SECRET_MIN:
            text = text.replace(s, SCRUBBED)
        elif s:
            text = re.sub(r"(?<![\w])%s(?![\w])" % re.escape(s), SCRUBBED, text)
    return text


def scrub_obj(obj):
    """scrub() on every string of a JSON value (keys too); B1: the value of a credential-like key is replaced whole."""
    if isinstance(obj, str):
        return scrub(obj)
    if isinstance(obj, list):
        return [scrub_obj(x) for x in obj]
    if isinstance(obj, dict):
        return {(scrub(k) if isinstance(k, str) else k):
                (SCRUBBED if isinstance(k, str) and SECRET_KEY.search(k) and v not in (None, "", [], {}) else scrub_obj(v))
                for k, v in obj.items()}
    return obj


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def utc(epoch=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if epoch is None else epoch))


class Audit:
    """T12: append-only JSON lines (ts, who, session, action, argv, job, rc, detail). One write() per line on an
    O_APPEND descriptor opened for that line; a file 'chattr +a' still takes it. Never raises: a failed audit write
    goes to stderr and returns False (B5: the web then refuses the request it was about to forward)."""

    def __init__(self, path, who):
        self.path, self.who = path, who

    def __call__(self, action, session=None, argv=None, job=None, rc=None, **detail):
        rec = {"ts": utc(), "who": self.who, "action": action, "session": session, "argv": argv, "job": job, "rc": rc}
        if detail:
            rec["detail"] = scrub_obj(detail)
        if argv is not None:
            rec["argv"] = scrub_obj(argv)
        line = (dumps(rec) + "\n").encode("utf-8")
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o620)
            try:
                if os.write(fd, line) != len(line):
                    raise OSError(28, "short write")
            finally:
                os.close(fd)
        except OSError as e:
            import sys
            sys.stderr.write("migui: audit write failed: %s\n" % e.strerror)
            return False
        return True


def read_json_line(f, limit=MAX_LINE):
    """One JSON object from a binary file object, at most limit bytes; None at EOF."""
    raw = f.readline(limit + 1)
    if not raw:
        return None
    if len(raw) > limit or not raw.endswith(b"\n"):
        raise Reject(413, "message too long or not terminated")
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Reject(400, "not a JSON line")
    if not isinstance(obj, dict):
        raise Reject(400, "a message is a JSON object")
    return obj
