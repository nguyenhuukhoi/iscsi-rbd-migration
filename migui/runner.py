"""migui-runner (DESIGN.md section 8.1): the root half of the UI, the only process that starts 'migrate'.

Listens on a unix socket (default /run/migui/runner.sock, 0660 root:migui). A peer whose SO_PEERCRED uid is not
the migui user is refused (T14). One JSON message per connection, one line; the answer is JSON lines ending with
{"end": rc}. Every message carries "user", the account logged in on the web (16.3, USER_RE): missing or invalid
is a 400. It is stored as "operator" in batches, job records and the runner's audit lines, and shown by status.
Messages (anything else: 400):

  doctor     [write_check]                  migrate doctor --json [--write-check] (C6)
  inventory                                 migrate inventory --json; its hosts/projects/zones/flavors/types become
                                            the only values list and plan accept (6.1)
  list       hosts, projects, volumes, scope   migrate list --json ...
  plan       session, kind vm|vol, items, options   migrate plan <ident> ... --json per VM, up to --plan-jobs (4) at
                                            once (C1; plan volume per project)
                                            -> a PLANNED batch with a confirm token (7, 14.2); before each migrate
                                            plan a {"planning"} line, after it a {"row"} line (18); in between
                                            a {"line"} per stderr line of that plan, as it comes (20)
  migrate    session, batch, confirm_token, typed, continue_on_error   run a planned VM batch, one job at a time
  volume     (same)                         run a planned volume batch
  rollback   session, ident                 prepare: a batch of one 'migrate rollback <ident>' (live state only)
             session, batch, confirm_token, typed   run it
  cancel     session, job_id [, force, typed] | batch_id + after_current   SIGINT to the job's process group;
                                            SIGKILL only on a second, forced cancel kill_after seconds later
  status     [job_id [, from, follow]] [, cli] [, offset, limit, q, state, kind, from, to]   runner jobs (D9: a
                                            filtered page and its total) and queue; one job's log; migrate status --json

The runner never takes a command string: every argv is built from checked fields and started with
Popen(list, start_new_session=True) (T7). The runner writes no login secret: the web checks passwords against
/var/lib/migui/users (migui-passwd, 16). The host lock of migrate itself (MIG_LOCK, default /run/mig/migrate.lock, A7)
is held for a whole batch and handed to each migrate (MIG_LOCK_FD): one migrate, volume or rollback at a time on this
host, CLI or UI (T10). Every line handed to the web is scrubbed (T9, B1: the openrcs' secret values too). A plan keeps
the sha256 of migrate.conf and the map files; a run whose files changed is refused (C5). MIGUI_NOTIFY_CMD (an argv as
a JSON list) runs at the end of each job and batch (C14). Job records
<jobs>/<id>.json are written 0600 and made read-only (0400) when the job ends (13.2).
"""
import argparse
import fcntl
import grp
import hashlib
import hmac
import json
import os
import pwd
import re
import secrets
import signal
import socket
import socketserver
import stat
import struct
import subprocess
import sys
import threading
import time

from . import __version__, common
from .common import (JOB_ID_RE, MAX_LINE, NAME_RE, USER_RE, Audit, Reject, check_ident, check_items, check_name,
                     confirm_rule, dumps, normalize_options, read_json_line, scrub, scrub_obj, typed_matches, utc)

SESSION_RE = re.compile(r"^[0-9a-f]{16}\Z")
LOGS_RE = re.compile(r"logs in (/\S+?);")
RUN_VERBS = ("migrate", "volume", "rollback", "migrate-volume")
TIMEOUTS = {"doctor": 300, "inventory": 600, "list": 600, "plan": 900, "status": 120}
MEM_LOG_MAX = 8 << 20    # bytes of one job's log kept in memory for the stream
FILE_LOG_MAX = 50 << 20  # T15: the runner's copy of a job log; the batch log keeps everything
PING = 15
MAX_PLANNED = 20
MAX_JOBS_LISTED = 300
MAX_CHECKS = 200         # 17: the plan checks kept in a job record, and the length of each text
MAX_CHECK_TEXT = 500
MAX_SERVER_JSON = 1 << 20  # 19.2: server.json of a state directory, read for the display name only
LIST_MAX = 128           # 14.1, C13: source hosts / projects of one list (several hosts under maintenance at once)
PLAN_JOBS = 4            # C1: 'migrate plan <ident> --json' children of one plan request run at once
JOBS_PAGE = 50           # D9: GET /api/jobs default page, at most JOBS_PAGE_MAX
JOBS_PAGE_MAX = 500
NOTIFY_TIMEOUT = 30      # C14: seconds MIGUI_NOTIFY_CMD may take
EXIT_CLASSES = (3, 4, 5)  # C8: before t-stop, after t-stop, a selection with a FAIL
LIVE_LINES = 200         # 20: plan lines streamed per ident (or volume group); the rest is only counted
LIVE_LINE_TEXT = 500     # 20: and the length of each
LEVELS = (("  OK    ", "OK"), ("  WARN  ", "WARN"), ("  FAIL  ", "FAIL"), ("  INFO  ", "INFO"))
# The CLI's own option lists and help, read once at start (6.1: "never hardcoded in two places").
# A7, B1, C5: also the host lock file, the conf and map paths, and the secret values of both openrcs (root to root over
# a pipe, kept in memory only: what scrub() replaces in every line that leaves the runner).
PROBE = ("import json, sys\n"
         "sys.path[:0] = sys.argv[1:]\n"
         "from migratetool import __version__, cli, options\n"
         "meta = {'flags': list(options.FLAG_OPTS), 'values': list(options.VALUE_OPTS),\n"
         "        'help': [[o, m, h] for o, m, h in cli.VM_OPTIONS], 'version': __version__}\n"
         "try:\n"
         "    from migratetool import config, jsonout\n"
         "    cfg = config.load()\n"
         "    meta.update(lock=cfg.MIG_LOCK, conf=cfg.conf_path, project_map=cfg.PROJECT_MAP_FILE,\n"
         "                flavor_map=cfg.FLAVOR_MAP_FILE, secrets=sorted(jsonout.secret_values(cfg)))\n"
         "except Exception as e:\n"
         "    meta['probe_error'] = type(e).__name__\n"
         "print(json.dumps(meta))\n")


def peer_uid(sock):
    pid, uid, gid = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
    return uid


def verb_of(argv):
    """The migrate verb of a /proc cmdline from 'migrate status --json' (python -m migratetool.x VERB, or the
    'migrate' entry point VERB)."""
    for i, a in enumerate(argv):
        if a == "-m" and i + 2 < len(argv) and argv[i + 1].startswith("migratetool"):
            return argv[i + 2]
        if os.path.basename(a) == "migrate" and i + 1 < len(argv) and i <= 1:
            return argv[i + 1]
    return ""


def batch_state(states, stop_reason):
    """The state the batch notification carries: OK when every job is OK; CANCELLED when a job was cancelled or the
    batch was (Review 23/11 #2, round 2: a cancel that came after a job's result rc 0 leaves that job OK and the
    rest SKIPPED, a batch cancelled all the same); FAIL otherwise."""
    if all(s == "OK" for s in states):
        return "OK"
    if "CANCELLED" in states or stop_reason == "cancelled":
        return "CANCELLED"
    return "FAIL"


def _eff(effective, key):
    v = (effective or {}).get(key) if isinstance(effective, dict) else None
    return v.get("value") if isinstance(v, dict) else None


def needs_name(kind, effective, options, ip):
    """D2: the confirmation is by name when the run, with the options it will really have (the plan row's effective
    options, which include MIG_DEFAULT_OPTS), keeps the source ports, gives a fixed address, restarts the source, or
    copies an in-use volume."""
    if kind == "vol":
        return options.get("in_use") is True or _eff(effective, "in_use") is True
    if ip or _eff(effective, "keep_ports") is True or _eff(effective, "start_source") is True:
        return True
    addr = _eff(effective, "ip")
    return isinstance(addr, str) and addr not in ("", "same", "new")


def fingerprint(kind, items, opts):
    """Section 3: sha256 of the set of idents (with their per-row address) and the normalized options of a batch."""
    canon = {"kind": kind, "items": sorted("%s %s" % (i, ip or "") for i, ip in items), "options": list(opts)}
    return hashlib.sha256(json.dumps(canon, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def batch_impact(rows):
    """Section 3: the plan rows of a batch summed up (vms, volumes, gb, stop_source, detach_ports)."""
    out = {"vms": 0, "volumes": 0, "gb": 0, "stop_source": 0, "detach_ports": 0}
    for r in rows:
        if r.get("kind", "vm") == "vm":
            out["vms"] += 1
        for key, field in (("volumes", "volumes_total"), ("gb", "gb_total")):
            v = r.get(field)
            out[key] += v if isinstance(v, int) else 0
        imp = r.get("impact") if isinstance(r.get("impact"), dict) else {}
        out["stop_source"] += 1 if imp.get("stop_source") else 0
        out["detach_ports"] += imp.get("detach_ports") if isinstance(imp.get("detach_ports"), int) else 0
    return out


def notify_argv(text):
    """C14: MIGUI_NOTIFY_CMD is a JSON list of strings (an argv, never a shell line); '' = none. Anything else stops
    the runner at start."""
    if not text.strip():
        return None
    try:
        argv = json.loads(text)
    except ValueError:
        argv = None
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
        raise SystemExit("migui-runner: MIGUI_NOTIFY_CMD must be a JSON list of strings, e.g. "
                         "[\"/usr/local/bin/notify\", \"--to\", \"ops\"] (it is never run by a shell)")
    return argv


DATE_RE = re.compile(r"^\d{4}-\d\d-\d\d(T\d\d:\d\d(:\d\d)?Z?)?\Z")


def job_filter(msg):
    """D9: a predicate on jobs from the status message's q (substring of id, batch, ident, name, operator), state
    (comma list), kind (vm|vol|rollback) and from/to (UTC dates or times, compared with t_plan)."""
    q = msg.get("q") if isinstance(msg.get("q"), str) else ""
    q = q.strip().lower()[:128]
    states = msg.get("state") if isinstance(msg.get("state"), str) else ""
    states = {s for s in states.split(",") if s}
    kind = msg.get("kind") if msg.get("kind") in ("vm", "vol", "rollback") else None
    if msg.get("kind") not in (None, "", "vm", "vol", "rollback"):
        raise Reject(400, "kind: vm, vol or rollback")
    lo, hi = msg.get("from") or "", msg.get("to") or ""
    for v in (lo, hi):
        if v and (not isinstance(v, str) or not DATE_RE.match(v)):
            raise Reject(400, "from/to: YYYY-MM-DD or YYYY-MM-DDTHH:MM[:SS]Z")
    if hi and len(hi) == 10:
        hi += "T23:59:59Z"

    def ok(j):
        if kind and j.kind != kind:
            return False
        if states and j.state not in states:
            return False
        t = j.t_plan or ""
        if lo and t < lo:
            return False
        if hi and t > hi:
            return False
        if q and not any(q in str(x or "").lower() for x in (j.id, j.batch, j.ident, j.name, j.operator)):
            return False
        return True
    return ok


def page_of(msg):
    off, lim = msg.get("offset", 0), msg.get("limit", JOBS_PAGE)
    if isinstance(off, bool) or not isinstance(off, int) or off < 0:
        raise Reject(400, "offset: a whole number >= 0")
    if isinstance(lim, bool) or not isinstance(lim, int) or not 1 <= lim <= JOBS_PAGE_MAX:
        raise Reject(400, "limit: 1-%d" % JOBS_PAGE_MAX)
    return off, lim


class Job:
    """One migrate/volume/rollback run. The record (13.2) is everything but the log lines."""

    FIELDS = ("id", "batch", "kind", "ident", "name", "argv", "effective", "conf", "projects", "session", "operator",
              "request_options", "state", "t_plan", "t_confirm", "t_start", "t_end", "rc", "pid", "pgid", "log",
              "batch_log", "state_dir", "progress", "phase", "t_stop", "downtime", "dst_server_id", "cancel",
              "plan", "checks", "original", "runner_version",
              # 1.0.0: D16 volumes_total, phase_detail (D12/D16), exit_class (C8), notify_rc (C14)
              "volumes_total", "phase_detail", "exit_class", "notify_rc")

    def __init__(self, **kw):
        for f in self.FIELDS:
            setattr(self, f, kw.get(f))
        self.progress = self.progress or {}
        self.cancel = self.cancel or {}
        self.exec_argv = kw.get("exec_argv")
        self.lines = []       # (n, t, kind, payload); kind line|event
        self.dropped = 0      # lines dropped from the head of the memory buffer
        self.mem = 0
        self.n = 0
        self.sealed = False
        self.cond = threading.Condition()
        self.proc = None

    def record(self):
        return {f: getattr(self, f) for f in self.FIELDS}

    @property
    def ended(self):
        return self.state in ("OK", "FAIL", "CANCELLED", "SKIPPED", "LOST", "DETACHED")


class Batch:
    def __init__(self, bid, session, kind, jobs, rule, conf, ttl, operator, fingerprint=None, impact=None,
                 conf_digest=None):
        self.id, self.session, self.kind, self.jobs, self.rule, self.conf = bid, session, kind, jobs, rule, conf
        self.operator = operator
        self.fingerprint, self.impact, self.conf_digest = fingerprint, impact, conf_digest
        self.token = secrets.token_hex(16)
        self.planned = time.time()
        self.expires = self.planned + ttl
        self.stop = False
        self.stop_reason = ""
        self.cont = False
        self.state = "PLANNED"

    def summary(self, with_token=False):
        d = {"id": self.id, "kind": self.kind, "state": self.state, "rule": self.rule, "planned": utc(self.planned),
             "expires": utc(self.expires), "expires_in": max(0, int(self.expires - time.time())),
             "jobs": [j.id for j in self.jobs], "commands": [j.argv for j in self.jobs],
             "names": [j.name for j in self.jobs], "conf": self.conf, "stop": self.stop,
             "stop_reason": self.stop_reason, "continue_on_error": self.cont, "operator": self.operator,
             "fingerprint": self.fingerprint, "impact": self.impact, "conf_digest": self.conf_digest,
             "session": self.session}
        if with_token:
            d["token"] = self.token
        return d


class Runner:
    def __init__(self, cfg):
        self.cfg = cfg
        self.mu = threading.RLock()
        self.jobs = {}
        self.order = []
        self.batches = {}
        self.active = None
        self.inv = None
        self.inv_conf = None
        self.known = {}
        self.plans_running = set()
        self.audit = Audit(cfg.audit, "runner")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("OS_")}
        self.env.setdefault("LANG", "C.UTF-8")
        self.env["PYTHONUNBUFFERED"] = "1"
        self.cli = self.probe()
        self.cli_opts = set(self.cli["flags"]) | set(self.cli["values"])
        # A7: the lock migrate itself takes (MIG_LOCK), unless --lock names another file (then both are taken by
        # nobody else: migrate refuses a MIG_LOCK_FD that is not its MIG_LOCK, so the runner passes MIG_LOCK too).
        self.lock_path = cfg.lock or self.cli.get("lock") or "/run/mig/migrate.lock"
        self.notify = notify_argv(os.environ.get("MIGUI_NOTIFY_CMD", ""))
        self.load_records()

    # --- start -------------------------------------------------------------------------------------------------
    def probe(self):
        argv = [self.cfg.cli_python, "-I", "-c", PROBE] + list(self.cfg.cli_path)
        res = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=60, env=self.env)
        try:
            meta = json.loads(res.stdout.decode("utf-8"))
        except ValueError:
            raise SystemExit("migui-runner: cannot read the CLI's options with %s (rc %d): %s"
                             % (self.cfg.cli_python, res.returncode, res.stderr.decode("utf-8", "replace")[-300:]))
        from .common import SPECS
        common.SECRETS.clear()
        common.SECRETS.update(s for s in meta.pop("secrets", None) or [] if isinstance(s, str) and s)
        missing = sorted({opt for spec in SPECS.values() for _k, opt, _t, _a in spec}
                         - set(meta["flags"]) - set(meta["values"]))
        if missing:
            raise SystemExit("migui-runner: the installed migrate %s lacks %s: install the matching version"
                             % (meta.get("version"), " ".join(missing)))
        return meta

    def load_records(self):
        """Job records of earlier runs (history). A job left RUNNING/QUEUED by a runner that died: its migrate may
        still run (KillMode=process), but nothing follows it any more (8): DETACHED when its pid lives, else LOST."""
        try:
            names = sorted(os.listdir(self.cfg.jobs))
        except FileNotFoundError:
            return
        for n in names:
            if not n.endswith(".json") or not JOB_ID_RE.match(n[:-5]):
                continue
            path = os.path.join(self.cfg.jobs, n)
            try:
                with open(path, encoding="utf-8") as f:
                    rec = json.load(f)
            except (OSError, ValueError):
                continue
            if not isinstance(rec, dict) or rec.get("id") != n[:-5]:
                continue
            job = Job(**{k: rec.get(k) for k in Job.FIELDS})
            if not job.ended:
                alive = False
                if job.pid:
                    try:
                        os.kill(job.pid, 0)
                        alive = True
                    except OSError:
                        alive = False
                job.state = "DETACHED" if alive and job.state == "RUNNING" else "LOST"
                job.t_end = utc()
                try:
                    self.seal(job)
                except OSError:
                    pass
                self.audit("orphan", session=job.session, job=job.id, state=job.state, operator=job.operator)
            job.sealed = True
            self.jobs[job.id] = job
            self.order.append(job.id)
        self.order.sort(key=lambda i: self.jobs[i].t_plan or "")

    # --- records -----------------------------------------------------------------------------------------------
    def write(self, job):
        if job.sealed:
            raise RuntimeError("job %s is sealed" % job.id)
        path = os.path.join(self.cfg.jobs, job.id + ".json")
        tmp = "%s.tmp-%s" % (path, secrets.token_hex(4))
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(job.record(), ensure_ascii=False, sort_keys=True, indent=1) + "\n")
        os.replace(tmp, path)

    def seal(self, job):
        """The last write of a record: then read-only, never written again (13.2)."""
        job.sealed = False
        self.write(job)
        os.chmod(os.path.join(self.cfg.jobs, job.id + ".json"), 0o400)
        job.sealed = True

    def add_line(self, job, kind, payload):
        t = utc()
        with job.cond:
            job.n += 1
            entry = (job.n, t, kind, payload)
            job.lines.append(entry)
            job.mem += len(payload) if isinstance(payload, str) else 200
            while job.mem > MEM_LOG_MAX and len(job.lines) > 1:
                old = job.lines.pop(0)
                job.dropped += 1
                job.mem -= len(old[3]) if isinstance(old[3], str) else 200
            job.cond.notify_all()
        rec = {"n": entry[0], "t": t, kind: payload}
        path = os.path.join(self.cfg.jobs, job.id + ".log")
        try:
            size = os.path.getsize(path) if os.path.exists(path) else 0
            if size < FILE_LOG_MAX:
                fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(dumps(rec) + "\n")
        except OSError:
            pass

    # --- CLI ---------------------------------------------------------------------------------------------------
    def run_cli(self, args, timeout):
        """(rc, stdout text, stderr text) of one read-only migrate command (argv list, no shell)."""
        argv = list(self.cfg.migrate) + list(args)
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env=self.env, start_new_session=True, close_fds=True)
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGINT)
                out, err = p.communicate(timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                os.killpg(p.pid, signal.SIGKILL)
                out, err = p.communicate()
            return 124, "", "timed out after %d s" % timeout
        return p.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    def run_cli_stream(self, args, timeout, on_line):
        """20: run_cli whose stderr is also handed over line by line while the command runs: on_line(scrubbed line,
        no line end) from a reader thread, right away; stdout gathered as in run_cli. Same argv, env, process group,
        timeout and SIGINT-then-SIGKILL. Both readers are joined before it returns: on_line is never called after,
        nor at the same time as the caller's own sends that follow. An exception of on_line (a dead web connection)
        does not stop the drain, or the child would block on a full stderr pipe."""
        argv = list(self.cfg.migrate) + list(args)
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env=self.env, start_new_session=True, close_fds=True)
        out, err = [], []

        def drain_out():
            out.append(p.stdout.read())

        def drain_err():
            for raw in iter(p.stderr.readline, b""):
                err.append(raw)
                try:
                    on_line(scrub(raw.decode("utf-8", "replace").rstrip("\r\n")))
                except Exception:  # noqa: BLE001 (see above)
                    pass
        readers = [threading.Thread(target=f, name="plan-" + f.__name__, daemon=True) for f in (drain_out, drain_err)]
        for t in readers:
            t.start()

        def join(deadline):
            for t in readers:
                t.join(None if deadline is None else max(0.0, deadline - time.monotonic()))
            return not any(t.is_alive() for t in readers)
        deadline = time.monotonic() + timeout
        try:
            p.wait(timeout=timeout)
            done = join(deadline)  # like communicate(timeout): the end of both pipes counts against the timeout
        except subprocess.TimeoutExpired:
            done = False
        if not done:
            try:
                os.killpg(p.pid, signal.SIGINT)
                p.wait(timeout=30)
                if not join(time.monotonic() + 30):
                    raise subprocess.TimeoutExpired(argv, 30)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    pass
                p.wait()
                join(None)
            p.stdout.close()
            p.stderr.close()
            return 124, "", "timed out after %d s" % timeout
        p.stdout.close()
        p.stderr.close()
        return p.returncode, b"".join(out).decode("utf-8", "replace"), b"".join(err).decode("utf-8", "replace")

    def cli_json(self, args, timeout, on_line=None):
        """(rc, object or None, scrubbed stderr tail). on_line: stream stderr through run_cli_stream (20)."""
        rc, out, err = self.run_cli_stream(args, timeout, on_line) if on_line else self.run_cli(args, timeout)
        tail = [scrub(l) for l in err.splitlines()[-30:]]
        try:
            obj = json.loads(out) if out.strip() else None
        except ValueError:
            obj = None
        return rc, scrub_obj(obj) if isinstance(obj, dict) else None, tail

    # --- messages ----------------------------------------------------------------------------------------------
    def op_doctor(self, msg, send):
        args = ["doctor", "--json"] + (["--write-check"] if msg.get("write_check") is True else [])  # C6
        rc, data, tail = self.cli_json(args, TIMEOUTS["doctor"])
        send({"data": data, "stderr": tail})
        return rc

    def op_inventory(self, msg, send):
        try:  # B1: the openrcs may have changed since the start
            self.cli.update({k: v for k, v in self.probe().items() if k in ("lock", "conf", "project_map", "flavor_map")})
        except SystemExit:
            pass
        rc, data, tail = self.cli_json(["inventory", "--json"], TIMEOUTS["inventory"])
        if data:
            s, d = data.get("source") or {}, data.get("destination") or {}

            def names(xs, key):
                return {x.get(key) for x in xs or [] if isinstance(x, dict) and isinstance(x.get(key), str)
                        and NAME_RE.match(x.get(key))}
            inv = {"src_hosts": names(s.get("hosts"), "host"),
                   "src_projects": names(s.get("projects"), "name") | names(s.get("projects"), "id"),
                   "dst_projects": names(d.get("projects"), "name"),
                   "zones_compute": {z for z in (d.get("zones") or {}).get("compute") or [] if isinstance(z, str)},
                   "zones_volume": {z for z in (d.get("zones") or {}).get("volume") or [] if isinstance(z, str)},
                   "dst_hosts": names(d.get("hosts"), "host"),
                   "flavors": names(d.get("flavors"), "name"),
                   "volume_types": {t for t in d.get("volume_types") or [] if isinstance(t, str)}}
            inv["dst_host_info"] = {h.get("host"): h for h in d.get("hosts") or [] if isinstance(h, dict)}
            inv["cli_opts"] = self.cli_opts
            with self.mu:
                self.inv, self.inv_conf = inv, data.get("conf")
        send({"data": data, "stderr": tail})
        send({"catalog": {"help": self.cli["help"], "version": self.cli["version"], "runner": __version__}})
        return rc

    def need_inventory(self):
        if self.inv is None:
            raise Reject(409, "run inventory first (the runner checks hosts, projects and zones against it)")
        return self.inv

    def op_list(self, msg, send):
        inv = self.need_inventory()
        hosts, projects = msg.get("hosts") or [], msg.get("projects") or []
        if (not isinstance(hosts, list) or not isinstance(projects, list)
                or len(hosts) > LIST_MAX or len(projects) > LIST_MAX):
            raise Reject(400, "hosts/projects: lists of at most %d" % LIST_MAX)
        for h in hosts:
            check_name(h, "host")
            if h not in inv["src_hosts"]:
                raise Reject(400, "host %s is not a source nova-compute host of the inventory" % h)
        for p in projects:
            check_name(p, "project")
            if p not in inv["src_projects"]:
                raise Reject(400, "project %s is not a source project of the inventory" % p)
        if not hosts and not projects:
            raise Reject(400, "choose a host or a project")
        volumes = msg.get("volumes") is True
        scope = msg.get("scope") or ""
        if scope not in ("", "available", "attached", "all") or (scope and not volumes):
            raise Reject(400, "scope: available|attached|all with volumes")
        args = ["list", "--json"]
        for h in hosts:
            args += ["--host", h]
        for p in projects:
            args += ["--project", p]
        if volumes:
            args.append("--volumes")
            if scope:
                args.append("--" + scope)
        rc, data, tail = self.cli_json(args, TIMEOUTS["list"])
        if data:
            with self.mu:
                for row in data.get("rows") or []:
                    if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                        continue
                    info = {"kind": row.get("kind"), "name": row.get("name") or "", "status": row.get("status"),
                            "project": row.get("project"), "state": row.get("state"),
                            "attached_to": row.get("attached_to")}
                    self.known[(row.get("kind"), row["id"])] = info
        send({"data": data, "stderr": tail, "argv": ["migrate"] + args})
        return rc

    def op_plan(self, msg, send):
        inv = self.need_inventory()
        session = self.session_of(msg)
        operator = msg["user"]
        kind = msg.get("kind")
        if kind not in ("vm", "vol"):
            raise Reject(400, "kind must be vm or vol")
        items = check_items(kind, msg.get("items"))
        options = msg.get("options") or {}
        opts = normalize_options(kind, options, inv)
        known = []
        for ident, ip in items:
            info = self.known.get((kind, ident))
            if info is None:
                raise Reject(400, "%s is not in a listing of this runner: list again" % ident)
            known.append(info)
        if kind == "vm" and options.get("dst_host"):
            h = inv["dst_host_info"].get(options["dst_host"]) or {}
            if h.get("state") != "up" or h.get("status") != "enabled":
                raise Reject(400, "dst_host is not up and enabled")
            if options.get("az") and h.get("zone") != options["az"]:
                raise Reject(400, "dst_host is not in the az zone")
        if kind == "vol":
            in_use = options.get("in_use") is True
            states = [i.get("status") for i in known]
            if in_use and (len(items) != 1 or states != ["in-use"]):
                raise Reject(400, "in_use takes exactly one in-use volume")
            if not in_use and any(s != "available" for s in states):
                raise Reject(400, "an attached volume goes with its VM, or alone with in_use")
        with self.mu:
            if session in self.plans_running:
                raise Reject(429, "a plan of this session is still running")
            self.plans_running.add(session)
        try:
            rows = self.plan_vms(items, opts, known, send) if kind == "vm" else \
                self.plan_volumes(items, opts, known, options, send)
        finally:
            with self.mu:
                self.plans_running.discard(session)
        jobs = []
        by_name = False
        for (ident, ip), info, row in zip(items, known, rows):
            if row.get("result") not in ("PLAN-OK", "READY"):
                continue
            by_name = by_name or needs_name(kind, row.get("effective"), options, ip)  # D2
            verb = "migrate" if kind == "vm" else "volume"
            args = [verb, ident] + self.item_opts(opts, ip) + ["--yes", "--progress-json"]
            name = info.get("name") or ident
            req = dict(options)
            if ip:
                req["ip_addr"] = ip
            jobs.append(Job(id=secrets.token_hex(8), kind=kind, ident=ident, name=name, argv=["migrate"] + args,
                            exec_argv=list(self.cfg.migrate) + args, effective=row.get("effective"),
                            conf=self.inv_conf, projects=row.get("project") or info.get("project"), session=session,
                            operator=operator,
                            request_options=req, state="PLANNED", t_plan=utc(), plan=self.plan_summary(row),
                            checks=self.plan_checks(row), runner_version=__version__,
                            volumes_total=row.get("volumes_total")))
        if not jobs:
            send({"batch": None})
            return 1
        rule = confirm_rule(kind, [j.name for j in jobs], by_name, [j.ident for j in jobs])
        b = self.new_batch(session, kind, jobs, rule, operator, fingerprint=fingerprint(kind, items, opts),
                           impact=batch_impact([r for r in rows if r.get("result") in ("PLAN-OK", "READY")]),
                           conf_digest=self.conf_digest())
        send({"batch": b.summary(with_token=True)})
        self.audit("plan", session=session, batch=b.id, idents=[j.ident for j in jobs], argv=[j.argv for j in jobs],
                   operator=operator)
        return 0

    @staticmethod
    def item_opts(opts, ip):
        if not ip:
            return list(opts)
        out, i = [], 0
        while i < len(opts):
            if opts[i] == "--ip":
                i += 2
                continue
            out.append(opts[i])
            i += 1
        return out + ["--ip", ip]

    @staticmethod
    def plan_summary(row):
        return {k: row.get(k) for k in ("result", "info", "counts", "log", "command")}

    @staticmethod
    def plan_checks(row):
        """17: the checks of the plan row as the job record keeps them (the plan of that time): None when the row has
        none, else at most MAX_CHECKS {level, text}, each text at most MAX_CHECK_TEXT characters."""
        checks = row.get("checks")
        if not isinstance(checks, list):
            return None
        out = []
        for c in checks[:MAX_CHECKS]:
            if isinstance(c, dict):
                out.append({"level": str(c.get("level") or "")[:16], "text": str(c.get("text") or "")[:MAX_CHECK_TEXT]})
        return out

    @staticmethod
    def planning(send, idents, name, i, n):
        """18: {"planning": ...} right before a migrate plan starts: ident (the first one), idents (every ident this
        one CLI run plans: one VM, or the volumes of one project), name, i (1-based number of the first) of n, t."""
        send({"planning": {"ident": idents[0], "idents": list(idents), "name": name, "i": i, "n": n, "t": utc()}})

    @staticmethod
    def plan_lines(send, idents):
        """20: (on_line, finish) for one migrate plan run of idents (one VM, or the volumes of one project). on_line
        gets each scrubbed stderr line and sends {"line": {ident, idents, text, level, t}}: text cut to
        LIVE_LINE_TEXT, level OK|WARN|FAIL|INFO from the CLI's two-space prefix (nothing else is parsed) or None. The
        '@@ {...}' line (the effective options as JSON) is not forwarded. After LIVE_LINES lines the rest is only
        counted; finish() then sends one line "… (còn N dòng, xem log plan)". Nothing of it goes to the audit."""
        st = {"sent": 0, "more": 0}

        def emit(text, level):
            send({"line": {"ident": idents[0], "idents": list(idents), "text": text[:LIVE_LINE_TEXT], "level": level,
                           "t": utc()}})

        def on_line(text):
            if text.startswith("@@ "):
                return
            if st["sent"] >= LIVE_LINES:
                st["more"] += 1
                return
            st["sent"] += 1
            emit(text, next((lv for pre, lv in LEVELS if text.startswith(pre)), None))

        def finish():
            if st["more"]:
                emit("… (còn %d dòng, xem log plan)" % st["more"], None)
        return on_line, finish

    def plan_cli(self, send, idents, args):
        """cli_json of one migrate plan with its stderr streamed as {"line"} events (20)."""
        on_line, finish = self.plan_lines(send, idents)
        res = self.cli_json(args, TIMEOUTS["plan"], on_line)
        finish()
        return res

    def plan_vms(self, items, opts, known, send):
        """C1: one 'migrate plan <ident> --json' per VM, up to PLAN_JOBS at once (plan is read-only). The events of
        each ident stay whole ({"planning"}, its {"line"}s, its {"row"}); a lock keeps two children's sends apart.
        The rows come back in the order of items."""
        rows = [None] * len(items)
        mu = threading.Lock()

        def locked_send(obj):
            with mu:
                send(obj)

        def one(k):
            (ident, ip), info = items[k], known[k]
            self.planning(locked_send, [ident], info.get("name") or ident, k + 1, len(items))
            args = ["plan", ident] + self.item_opts(opts, ip) + ["--json"]
            rc, data, tail = self.plan_cli(locked_send, [ident], args)
            got = [r for r in (data or {}).get("rows") or [] if isinstance(r, dict)]
            row = next((r for r in got if ident in (r.get("id"), r.get("ident"), r.get("name"))), got[0] if got else None)
            if row is None:
                row = {"result": "PLAN-FAIL", "info": "\n".join(tail[-5:]) or "migrate plan exit %d" % rc}
            row = dict(row)
            row["ident"], row["argv"] = ident, ["migrate"] + args
            row.setdefault("name", info.get("name"))
            rows[k] = row
            locked_send({"row": row})

        jobs = self.cfg.plan_jobs
        if len(items) > 1 and jobs > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=jobs) as pool:
                for f in [pool.submit(one, k) for k in range(len(items))]:
                    f.result()
        else:
            for k in range(len(items)):
                one(k)
        return rows

    # --- 1.0.0 helpers ---------------------------------------------------------------------------------------------
    def conf_digest(self):
        """C5: sha256 over migrate.conf, PROJECT_MAP_FILE and FLAVOR_MAP_FILE (paths from the CLI probe; a missing file
        counts as its name only). A plan and its run must see the same three files."""
        h = hashlib.sha256()
        for key in ("conf", "project_map", "flavor_map"):
            path = self.cli.get(key) or ""
            h.update(("%s=%s\n" % (key, path)).encode("utf-8"))
            if path:
                try:
                    with open(path, "rb") as f:
                        h.update(f.read(MAX_SERVER_JSON))
                except OSError:
                    h.update(b"<missing>")
        return h.hexdigest()

    def rollback_facts(self, ident, entry):
        """A5 / section 3: t_stop, dst_server_status, age_seconds and what the rollback deletes, from 'migrate rollback
        <ident> --dry-run --json' (read-only: one 'server show' of the destination server); the status entry when
        that fails."""
        rc, data, _tail = self.cli_json(["rollback", ident, "--dry-run", "--json"], TIMEOUTS["status"])
        if rc == 0 and isinstance(data, dict):
            return {k: data.get(k) for k in ("t_stop", "dst_server_status", "age_seconds", "delete", "source_power")}
        vm = [x[1] for x in entry.get("volume_map") or [] if isinstance(x, list) and len(x) == 2]
        return {"t_stop": entry.get("t_stop"), "dst_server_status": None, "age_seconds": None,
                "delete": {"server": entry.get("dst_server_id"), "ports": [], "volumes": vm, "images": []},
                "source_power": entry.get("source_power")}

    def notify_event(self, event, ident, state, **extra):
        """C14: MIGUI_NOTIFY_CMD (a JSON list, never a shell) at the end of a job or a batch, with MIG_EVENT, MIG_JOB,
        MIG_STATE (and MIG_BATCH, MIG_NAME, MIG_RC, MIG_OPERATOR) in its environment. Its exit code, or None."""
        if not self.notify:
            return None
        env = {k: v for k, v in self.env.items() if not k.startswith("MIG_LOCK")}
        env.update({"MIG_EVENT": event, "MIG_JOB": ident if event == "job" else "", "MIG_STATE": str(state)})
        for k, v in extra.items():
            if v is not None:
                env["MIG_" + k.upper()] = str(v)
        try:
            res = subprocess.run(self.notify, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, env=env, timeout=NOTIFY_TIMEOUT, close_fds=True)
            rc = res.returncode
        except (OSError, subprocess.TimeoutExpired):
            rc = 124
        self.audit("notify", event=event, job=ident if event == "job" else None, rc=rc)
        return rc

    def plan_volumes(self, items, opts, known, options, send):
        rows = [None] * len(items)
        if options.get("in_use") is True:
            self.planning(send, [items[0][0]], known[0].get("name") or items[0][0], 1, 1)
            rows[0] = {"result": "READY", "ident": items[0][0], "name": known[0].get("name"),
                       "info": "--in-use: migrate volume checks it right before the clone",
                       "project": known[0].get("project")}
            send({"row": rows[0]})
            return rows
        by_project = {}
        for i, info in enumerate(known):
            p = (info.get("project") or {}).get("src") or ""
            by_project.setdefault(p, []).append(i)
        popts = [a for a in opts if a != "--in-use"]
        done = 0
        for p, idx in by_project.items():
            check_name(p, "project")
            group = [items[i][0] for i in idx]
            self.planning(send, group, "project " + p, done + 1, len(items))
            done += len(idx)
            args = ["plan", "volume", "--project", p, "--available", "--json"] + popts
            rc, data, tail = self.plan_cli(send, group, args)
            got = {r.get("id"): r for r in (data or {}).get("rows") or [] if isinstance(r, dict)}
            for i in idx:
                ident = items[i][0]
                row = dict(got.get(ident) or {"result": "PLAN-FAIL",
                                              "info": "\n".join(tail[-5:]) or "not in the plan (exit %d)" % rc})
                row["ident"], row["argv"] = ident, ["migrate"] + args
                row.setdefault("name", known[i].get("name"))
                rows[i] = row
                send({"row": row})
        return rows

    def new_batch(self, session, kind, jobs, rule, operator, conf=None, fingerprint=None, impact=None, conf_digest=None):
        b = Batch(secrets.token_hex(8), session, kind, jobs, rule, conf if conf is not None else self.inv_conf,
                  self.cfg.confirm_ttl, operator, fingerprint, impact, conf_digest)
        for j in jobs:
            j.batch = b.id
        with self.mu:
            self.expire()
            while len([x for x in self.batches.values() if x.state == "PLANNED"]) >= MAX_PLANNED:
                oldest = min((x for x in self.batches.values() if x.state == "PLANNED"), key=lambda x: x.planned)
                del self.batches[oldest.id]
            self.batches[b.id] = b
        return b

    def expire(self):
        now = time.time()
        for bid in [b.id for b in self.batches.values() if b.state == "PLANNED" and b.expires < now]:
            del self.batches[bid]

    @staticmethod
    def session_of(msg):
        s = msg.get("session")
        if not isinstance(s, str) or not SESSION_RE.match(s):
            raise Reject(400, "session: 16 hex characters")
        return s

    def op_rollback(self, msg, send):
        if msg.get("batch") is not None:
            return self.op_run(msg, send, "rollback")
        session = self.session_of(msg)
        operator = msg["user"]
        ident = check_ident(msg.get("ident"))
        rc, data, tail = self.cli_json(["status", "--json"], TIMEOUTS["status"])
        entry = next((e for e in (data or {}).get("live") or [] if isinstance(e, dict) and e.get("ident") == ident),
                     None)
        if entry is None:
            raise Reject(409, "%s has no live state directory (migrate status): nothing to roll back" % ident)
        opts = entry.get("options") if isinstance(entry.get("options"), dict) else None
        # 19.2: the name the operator types is the VM's own (server.json of the state dir), then the listing cache
        # of this runner (empty after a restart, or in a session that never listed), the ident last
        name = self.state_name(entry.get("dir"))
        sid = (opts or {}).get("server_id")
        if name is None:
            with self.mu:
                for (kind, kid), info in self.known.items():
                    if kid in (sid, ident) and info.get("name"):
                        name = info["name"]
                        break
        name = name or ident
        args = ["rollback", ident, "--yes"]  # A5: the typed name below is the confirmation
        eff = (opts or {}).get("effective")
        orig = dict(opts) if opts else {}
        orig.update(self.rollback_facts(ident, entry))
        job = Job(id=secrets.token_hex(8), kind="rollback", ident=ident, name=name, argv=["migrate"] + args,
                  exec_argv=list(self.cfg.migrate) + args, effective=eff, conf=(opts or {}).get("conf"),
                  projects=(eff or {}).get("projects", {}).get("value") if isinstance(eff, dict) else None,
                  session=session, operator=operator, request_options=None, state="PLANNED", t_plan=utc(),
                  state_dir=entry.get("dir"),
                  original=orig, runner_version=__version__)
        rule = confirm_rule("rollback", [name], True, [ident])
        if name != ident:
            rule["alt"] = [ident]  # 19.2: the full ident (UUID, vol-<id>) typed on purpose is accepted too
        b = self.new_batch(session, "rollback", [job], rule, operator, conf=(opts or {}).get("conf"),
                           conf_digest=self.conf_digest())
        send({"batch": b.summary(with_token=True), "original": orig, "state": entry})
        self.audit("plan", session=session, batch=b.id, idents=[ident], argv=[job.argv], operator=operator)
        return 0

    def state_name(self, d):
        """19.2: "name" of <d>/server.json (what migrate saved of the source VM), when it passes NAME_RE; else None."""
        obj = self.state_json(d, "server.json")
        name = obj.get("name") if isinstance(obj, dict) else None
        return name if isinstance(name, str) and NAME_RE.match(name) else None

    def state_json(self, d, fname):
        """The JSON object of <d>/<fname> when d resolves to a directory under --state-root and the file is a regular
        file (O_NOFOLLOW) of at most 1 MiB; else None. d comes from 'migrate status --json' or a job record and is
        never trusted as a path (19.2)."""
        if not isinstance(d, str) or not d.startswith("/") or "\0" in d:
            return None
        root = os.path.realpath(self.cfg.state_root)
        real = os.path.realpath(d)
        try:
            if real == root or os.path.commonpath([root, real]) != root:
                return None
        except ValueError:
            return None
        try:
            fd = os.open(os.path.join(real, fname),
                         os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        except OSError:
            return None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_SERVER_JSON:
                return None
            raw = b""
            while len(raw) <= MAX_SERVER_JSON:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                raw += chunk
        except OSError:
            return None
        finally:
            os.close(fd)
        if len(raw) > MAX_SERVER_JSON:
            return None
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        return obj if isinstance(obj, dict) else None

    def op_migrate(self, msg, send):
        return self.op_run(msg, send, "vm")

    def op_volume(self, msg, send):
        return self.op_run(msg, send, "vol")

    def op_run(self, msg, send, kind):
        session = self.session_of(msg)
        operator = msg["user"]
        bid, token = msg.get("batch"), msg.get("confirm_token")
        if not isinstance(bid, str) or not JOB_ID_RE.match(bid) or not isinstance(token, str):
            raise Reject(400, "batch and confirm_token are required")
        with self.mu:
            self.expire()
            b = self.batches.get(bid)
            if b is None or b.state != "PLANNED":
                self.audit("rejected", session=session, batch=bid, why="no such planned batch", operator=operator)
                raise Reject(409, "no such planned batch (expired, used or never planned): plan again")
            if b.kind != kind:
                raise Reject(409, "this batch is not a %s batch" % kind)
            if b.session != session or b.operator != operator or not hmac.compare_digest(b.token, token):
                del self.batches[bid]
                self.audit("rejected", session=session, batch=bid, why="confirm token or session mismatch",
                           operator=operator)
                raise Reject(409, "confirm token does not match this session's plan: plan again")
            if not typed_matches(b.rule, msg.get("typed")):
                del self.batches[bid]
                self.audit("rejected", session=session, batch=bid, why="typed confirmation mismatch", operator=operator)
                raise Reject(409, "the typed confirmation does not match: the token is void, plan again")
            if b.conf_digest is not None and b.conf_digest != self.conf_digest():
                del self.batches[bid]  # C5: the plan was made with another migrate.conf or map
                self.audit("rejected", session=session, batch=bid, why="configuration changed since the plan",
                           operator=operator)
                raise Reject(409, "migrate.conf, PROJECT_MAP_FILE or FLAVOR_MAP_FILE changed since the plan: plan again")
            lockfd = self.try_lock()
            if lockfd is None:
                raise Reject(423, "another migrate, volume or rollback runs on this host (lock %s): one at a time" % self.lock_path)
            b.state = "STARTING"
        try:
            busy = self.outside_runs()
        except Exception:
            busy = []
        if busy:
            os.close(lockfd)
            with self.mu:
                b.state = "PLANNED"
            raise Reject(423, "a migrate runs outside the UI (pid %s): wait for it" % ", ".join(str(p) for p in busy))
        b.cont = msg.get("continue_on_error") is True
        b.state = "RUNNING"
        now = utc()
        with self.mu:
            for j in b.jobs:
                j.state, j.t_confirm = "QUEUED", now
                self.jobs[j.id] = j
                self.order.append(j.id)
                self.write(j)
            self.active = b
        self.audit("run", session=session, batch=b.id, argv=[j.argv for j in b.jobs], jobs=[j.id for j in b.jobs],
                   operator=operator)
        t = threading.Thread(target=self.run_batch, args=(b, lockfd), name="batch-" + b.id, daemon=True)
        t.start()
        send({"batch": b.summary(), "jobs": [j.id for j in b.jobs]})
        return 0

    def try_lock(self):
        """A7: the host lock migrate takes itself (MIG_LOCK): held for the whole batch and handed to each migrate."""
        d = os.path.dirname(self.lock_path)
        if d and not os.path.isdir(d):
            os.makedirs(d, mode=0o700, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return None
        try:  # Review 23/11 #5: who holds it, for the CLI's "held by ..." message (best effort, as locking.host())
            os.ftruncate(fd, 0)
            os.write(fd, ("pid %d (migui-runner)\n" % os.getpid()).encode())
        except OSError:
            pass
        return fd

    def outside_runs(self):
        """pids of migrate/volume/rollback processes 'migrate status --json' sees (8: runs outside the UI). Review 23/11
        #4: a --dry-run (rollback --dry-run, which the UI itself runs to prepare a rollback) reads only and takes no
        lock: not counted."""
        rc, data, _tail = self.cli_json(["status", "--json"], TIMEOUTS["status"])
        return [p.get("pid") for p in (data or {}).get("running") or []
                if isinstance(p, dict) and verb_of(p.get("cmdline") or []) in RUN_VERBS
                and "--dry-run" not in (p.get("cmdline") or [])]

    def run_batch(self, b, lockfd):
        b.lockfd = lockfd
        try:
            for j in b.jobs:
                if b.stop:
                    j.state, j.t_end = "SKIPPED", utc()
                    self.seal(j)
                    self.audit("skip", session=j.session, job=j.id, why=b.stop_reason, operator=j.operator)
                    continue
                self.run_job(b, j)
                if j.state == "CANCELLED":
                    b.stop, b.stop_reason = True, b.stop_reason or "cancelled"
                elif j.state == "FAIL" and not b.cont:
                    b.stop, b.stop_reason = True, "%s failed (stop on failure)" % j.name
        finally:
            b.state = "DONE"
            os.close(lockfd)
            self.notify_event("batch", b.id, batch_state([j.state for j in b.jobs], b.stop_reason), batch=b.id,
                              operator=b.operator)
            with self.mu:
                if self.active is b:
                    self.active = None
                self.batches.pop(b.id, None)

    def run_job(self, b, j):
        j.state, j.t_start = "RUNNING", utc()
        j.log = os.path.join(self.cfg.jobs, j.id + ".log")
        # The child writes into a file, not a pipe: a runner that stops or restarts (KillMode=process) must not
        # leave migrate with a broken stdout in the middle of a cutover (EPIPE on its next line).
        out_path = os.path.join(self.cfg.jobs, j.id + ".out")
        try:
            fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                lfd = getattr(b, "lockfd", None)
                env = dict(self.env, MIG_LOCK=self.lock_path)
                if lfd is not None:  # A7: this batch's host lock goes to migrate, which re-enters it
                    env["MIG_LOCK_FD"] = str(lfd)
                p = subprocess.Popen(j.exec_argv, stdin=subprocess.DEVNULL, stdout=fd, stderr=subprocess.STDOUT,
                                     env=env, start_new_session=True, close_fds=True,
                                     pass_fds=(lfd,) if lfd is not None else ())
            finally:
                os.close(fd)
        except OSError as e:
            j.state, j.rc, j.t_end = "FAIL", 127, utc()
            self.add_line(j, "line", "migui-runner: cannot start migrate: %s" % e.strerror)
            self.seal(j)
            self.audit("end", session=j.session, job=j.id, argv=j.argv, rc=127, operator=j.operator)
            return
        j.pid = j.pgid = p.pid  # start_new_session: the child leads its own process group
        j.proc = p
        self.write(j)
        self.audit("start", session=j.session, job=j.id, argv=j.argv, pid=p.pid, operator=j.operator)
        with open(out_path, "rb") as f:
            buf = b""
            while True:
                chunk = f.read(65536)
                if chunk:
                    buf += chunk
                    *lines, buf = buf.split(b"\n")
                    for raw in lines:
                        self.on_line(j, raw)
                    continue
                if p.poll() is not None:
                    rest = f.read()
                    if rest:
                        buf += rest
                        continue
                    if buf:
                        self.on_line(j, buf)
                    break
                time.sleep(0.1)
        rc = p.wait()
        j.rc = rc
        result = j.progress.get("result")
        if j.cancel.get("sigint_at") and isinstance(result, dict) and result.get("rc") == 0:
            # Review 23/11 #2: the cancel came after migrate had printed its result rc 0 (the VM is migrated; the
            # selector was still finishing and exited 130 on the SIGINT): the job is OK. sigint_at stays for the
            # audit, the batch still stops (b.stop), the exit code migrate really gave stays in cancel.exit.
            j.state = "OK"
            j.cancel["late"] = True
            j.cancel["exit"] = rc
            j.rc = rc = 0
        elif j.cancel.get("sigint_at"):
            j.state = "CANCELLED"
        else:
            j.state = "OK" if rc == 0 else "FAIL"
        j.exit_class = rc if rc in EXIT_CLASSES else None  # C8
        j.notify_rc = self.notify_event("job", j.id, j.state, batch=j.batch, name=j.name, rc=rc, operator=j.operator)
        if j.kind in ("vm", "vol") and not j.state_dir:
            j.state_dir = os.path.join(self.cfg.state_root, j.ident if j.kind == "vm" else "vol-" + j.ident)
        if j.kind == "vol" and not isinstance(j.effective, dict):
            # a standalone volume is READY at plan time (no preflight, no effective options): the options it ran with
            # are those 'migrate volume' recorded in its own state, options.json (record.write_options_json)
            opts = self.state_json(j.state_dir, "options.json")
            if isinstance(opts, dict) and isinstance(opts.get("effective"), dict):
                j.effective = scrub_obj(opts["effective"])  # T9: as every other JSON of the CLI the runner hands out
        j.t_end = utc()
        j.proc = None
        with j.cond:
            self.seal(j)
            j.cond.notify_all()
        self.audit("end", session=j.session, job=j.id, argv=j.argv, rc=rc, state=j.state, operator=j.operator)

    def on_line(self, j, raw):
        """One line of migrate's output: scrubbed (T9); '@@ {...}' becomes a progress event, the rest a log line."""
        text = scrub(raw.decode("utf-8", "replace").rstrip("\r"))
        if text.startswith("@@ "):
            try:
                ev = json.loads(text[3:])
            except ValueError:
                ev = None
            if isinstance(ev, dict) and isinstance(ev.get("event"), str):
                self.on_event(j, ev)
                self.add_line(j, "event", ev)
                return
        m = LOGS_RE.search(text)
        if m and not j.batch_log:
            j.batch_log = m.group(1)
        self.add_line(j, "line", text)

    def on_event(self, j, ev):
        e = ev["event"]
        j.progress[e] = ev
        pd = j.phase_detail if isinstance(j.phase_detail, dict) else {"done": []}
        if e in ("copy", "verify", "blocksync") and isinstance(ev.get("volume"), str):
            pd["volume"], pd["pct"] = ev["volume"], ev.get("pct")
            if ev.get("pct") == 100 and e != "blocksync" and ev["volume"] not in pd["done"]:
                pd["done"].append(ev["volume"])
        elif e == "pass" and ev.get("n") == "final" and ev.get("volume") not in pd["done"]:
            pd["done"].append(ev.get("volume"))
        pd["volumes_done"] = len(pd["done"])
        pd["volumes_total"] = j.volumes_total
        j.phase_detail = pd
        if e == "phase":
            j.phase = ev.get("name")
            pd["name"] = j.phase
        elif e == "t_stop":
            j.t_stop = ev.get("at")
        elif e == "downtime":
            j.downtime = ev
        elif e == "result":
            j.dst_server_id = ev.get("dst_server_id")

    def op_cancel(self, msg, send):
        session = self.session_of(msg)
        operator = msg["user"]
        if msg.get("after_current") is True:
            bid = msg.get("batch_id")
            with self.mu:
                b = self.active
                if b is None or b.id != bid:
                    raise Reject(409, "that batch is not running")
                b.stop, b.stop_reason = True, "stopped after the current job"
            self.audit("stop-after-current", session=session, batch=bid, operator=operator)
            send({"batch": b.summary()})
            return 0
        jid = msg.get("job_id")
        if not isinstance(jid, str) or not JOB_ID_RE.match(jid):
            raise Reject(400, "job_id: 16 hex characters")
        j = self.jobs.get(jid)
        if j is None or j.state != "RUNNING" or j.proc is None:
            raise Reject(409, "that job is not running")
        force = msg.get("force") is True
        b = self.batches.get(j.batch) or self.active
        if not force:
            if j.cancel.get("sigint_at"):
                send({"cancel": j.cancel, "force_in": max(0, int(self.cfg.kill_after - (time.time() - j.cancel["sigint_epoch"])))})
                return 0
            os.killpg(j.pgid, signal.SIGINT)
            j.cancel = {"sigint_at": utc(), "sigint_epoch": time.time(), "session": session, "operator": operator}
            if b is not None:
                b.stop, b.stop_reason = True, "cancelled"
            self.audit("cancel", session=session, job=jid, argv=j.argv, signal="SIGINT", operator=operator)
            send({"cancel": j.cancel, "force_in": self.cfg.kill_after})
            return 0
        if not j.cancel.get("sigint_at"):
            raise Reject(409, "send the SIGINT cancel first")
        left = self.cfg.kill_after - (time.time() - j.cancel["sigint_epoch"])
        if left > 0:
            raise Reject(409, "SIGKILL only %d s after the SIGINT: %d s left" % (self.cfg.kill_after, int(left) + 1))
        if not typed_matches({"mode": "name", "expect": [j.name], "ids": [j.ident]}, msg.get("typed")):
            self.audit("rejected", session=session, job=jid, why="SIGKILL typed name mismatch", operator=operator)
            raise Reject(409, "type the name of the job's VM or volume to SIGKILL it")
        os.killpg(j.pgid, signal.SIGKILL)
        j.cancel["sigkill_at"] = utc()
        self.audit("kill", session=session, job=jid, argv=j.argv, signal="SIGKILL", operator=operator)
        send({"cancel": j.cancel})
        return 0

    def op_status(self, msg, send):
        jid = msg.get("job_id")
        if jid is not None:
            if not isinstance(jid, str) or not JOB_ID_RE.match(jid):
                raise Reject(400, "job_id: 16 hex characters")
            j = self.jobs.get(jid)
            if j is None:
                raise Reject(404, "no such job")
            frm = msg.get("from") or 0
            if isinstance(frm, bool) or not isinstance(frm, int) or frm < 0:
                raise Reject(400, "from: a line number")
            send({"job": j.record()})
            if msg.get("follow") is True or msg.get("lines") is True:
                self.stream(j, frm, send, follow=msg.get("follow") is True)
            send({"job": j.record()})
            return j.rc if j.rc is not None else 0
        flt = job_filter(msg)
        with self.mu:
            matched = [self.jobs[i] for i in reversed(self.order) if flt(self.jobs[i])]
            act = self.active.summary() if self.active else None
        off, lim = page_of(msg)
        send({"jobs": [j.record() for j in matched[off:off + lim]], "total": len(matched), "offset": off, "limit": lim,
              "active": act})
        rc = 0
        if msg.get("cli") is True:
            rc, data, tail = self.cli_json(["status", "--json"], TIMEOUTS["status"])
            send({"data": data, "stderr": tail})
        return rc

    def stream(self, j, frm, send, follow):
        """Log lines after line frm; with follow, new ones as they come until the job ends (pings every PING s)."""
        if j.sealed and not j.lines:
            for rec in self.file_lines(j, frm):
                send(rec)
            return
        last = frm
        while True:
            with j.cond:
                if j.dropped and last < j.dropped:
                    send({"n": j.dropped, "line": "[%d earlier lines are in the job log file only]" % j.dropped})
                    last = j.dropped
                new = [e for e in j.lines if e[0] > last]
                done = j.sealed
                if not new and not done and follow:
                    j.cond.wait(PING)
                    new = [e for e in j.lines if e[0] > last]
                    done = j.sealed
            for n, t, kind, payload in new:
                send({"n": n, "t": t, kind: payload})
                last = n
            if done or not follow:
                return
            if not new:
                send({"ping": utc()})

    def file_lines(self, j, frm):
        try:
            with open(os.path.join(self.cfg.jobs, j.id + ".log"), encoding="utf-8") as f:
                for raw in f:
                    try:
                        rec = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(rec, dict) and isinstance(rec.get("n"), int) and rec["n"] > frm:
                        yield rec
        except OSError:
            return


OPS = ("doctor", "inventory", "list", "plan", "migrate", "volume", "rollback", "cancel", "status")


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        runner = self.server.runner
        dead = []

        def send(obj):
            if dead:
                raise BrokenPipeError()
            try:
                self.wfile.write((dumps(obj) + "\n").encode("utf-8"))
                self.wfile.flush()
            except OSError:
                dead.append(1)
                raise BrokenPipeError()

        try:
            uid = peer_uid(self.request)
        except OSError:
            uid = -1
        try:
            if uid not in runner.cfg.peer_uids:
                runner.audit("rejected", why="peer uid %d is not allowed" % uid)
                try:  # read (and drop) its message first: closing on unread data would reset the connection
                    self.request.settimeout(2)
                    self.rfile.readline(MAX_LINE + 1)
                except OSError:
                    pass
                send({"error": "peer not allowed", "status": 403})
                send({"end": 1})
                return
            msg = read_json_line(self.rfile)
            if msg is None:
                return
            op = msg.get("op")
            if op not in OPS:
                runner.audit("rejected", why="unknown message")
                raise Reject(400, "unknown message")
            user = msg.get("user")
            if not isinstance(user, str) or not USER_RE.match(user):
                # 16.3: every message names the account logged in on the web; the runner records it as operator
                raise Reject(400, "user: missing or not a valid user name")
            rc = getattr(runner, "op_" + op)(msg, send)
            send({"end": rc})
        except Reject as e:
            if e.status == 400:
                runner.audit("rejected", session=msg_session(locals().get("msg")), why=e.message,
                             operator=msg_user(locals().get("msg")))
            try:
                send({"error": e.message, "status": e.status})
                send({"end": 1})
            except BrokenPipeError:
                pass
        except BrokenPipeError:
            pass
        except Exception as e:  # never let one connection take the runner down
            sys.stderr.write("migui-runner: %s: %s\n" % (type(e).__name__, scrub(str(e))))
            try:
                send({"error": "internal error", "status": 500})
                send({"end": 1})
            except BrokenPipeError:
                pass


def msg_session(msg):
    s = msg.get("session") if isinstance(msg, dict) else None
    return s if isinstance(s, str) and SESSION_RE.match(s) else None


def msg_user(msg):
    u = msg.get("user") if isinstance(msg, dict) else None
    return u if isinstance(u, str) and USER_RE.match(u) else None


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False


def parse_args(argv):
    ap = argparse.ArgumentParser(prog="migui-runner", description="root half of the migrate web UI (DESIGN.md section 8.1)")
    ap.add_argument("--socket", default="/run/migui/runner.sock")
    ap.add_argument("--lock", default="", help="the host lock (A7); default: MIG_LOCK of migrate.conf (/run/mig/migrate.lock), "
                                               "the file every migrate takes itself")
    ap.add_argument("--state-dir", default="/var/lib/migui", help="holds jobs/ (and the users file of the web)")
    ap.add_argument("--audit", default="/var/log/migui/audit.jsonl")
    ap.add_argument("--peer-user", default="migui", help="the only user allowed on the socket (SO_PEERCRED)")
    ap.add_argument("--group", default="migui", help="group of the socket and of the state and log directories; "
                                                     "'' = leave as is")
    ap.add_argument("--migrate", default="/opt/mig-venv/bin/migrate",
                    help="the migrate entry point, or an argv prefix as a JSON list (never a shell string)")
    ap.add_argument("--cli-python", default="", help="python that imports migratetool (default: the shebang of "
                                                     "--migrate's first element)")
    ap.add_argument("--cli-path", action="append", default=[], help="extra sys.path for the option probe (tests)")
    ap.add_argument("--state-root", default="/var/lib/mig-migrate", help="migrate's STATE_ROOT (job records only)")
    ap.add_argument("--confirm-ttl", type=int, default=900)
    ap.add_argument("--plan-jobs", type=int, default=PLAN_JOBS, choices=range(1, 9), metavar="1-8",
                    help="C1: VMs of one plan request planned at once (default %d)" % PLAN_JOBS)
    ap.add_argument("--kill-after", type=int, default=120)
    a = ap.parse_args(argv)
    if a.migrate.startswith("["):
        try:
            a.migrate = json.loads(a.migrate)
        except ValueError:
            ap.error("--migrate must be a path or a JSON list")
    else:
        a.migrate = [a.migrate]
    if not isinstance(a.migrate, list) or not a.migrate or not all(isinstance(x, str) for x in a.migrate):
        ap.error("--migrate must be a non-empty JSON list of strings")
    if not a.cli_python:
        try:
            with open(a.migrate[0], "rb") as f:
                first = f.readline(512).decode("utf-8", "replace").strip()
        except OSError:
            first = ""
        a.cli_python = first[2:].split()[0] if first.startswith("#!") and first[2:].split() else sys.executable
    a.jobs = os.path.join(a.state_dir, "jobs")
    a.peer_uids = {pwd.getpwnam(a.peer_user).pw_uid}
    a.gid = grp.getgrnam(a.group).gr_gid if a.group else None
    return a


def main(argv=None):
    cfg = parse_args(sys.argv[1:] if argv is None else argv)
    os.umask(0o077)
    root = os.geteuid() == 0
    for d, mode in ((cfg.state_dir, 0o750), (cfg.jobs, 0o700), (os.path.dirname(cfg.socket), 0o750),
                    (os.path.dirname(cfg.audit), 0o750)):
        os.makedirs(d, mode=mode, exist_ok=True)
        if root and cfg.gid is not None and d != cfg.jobs:
            os.chown(d, 0, cfg.gid)
    if root and cfg.gid is not None:  # 9: the web appends to the audit (0620 root:migui), never reads it
        fd = os.open(cfg.audit, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o620)
        try:
            st = os.fstat(fd)
            if (st.st_uid, st.st_gid, st.st_mode & 0o7777) != (0, cfg.gid, 0o620):
                # A file made append-only (chattr +a, install.sh) refuses chown/chmod even to root: it was set up
                # with the right owner and mode before; say so instead of failing to start.
                try:
                    os.fchown(fd, 0, cfg.gid)
                    os.fchmod(fd, 0o620)
                except PermissionError:
                    sys.stderr.write("migui-runner: %s is not root:migui 0620 and cannot be changed (append-only?): "
                                     "fix it by hand (chattr -a, chown, chmod, chattr +a)\n" % cfg.audit)
        finally:
            os.close(fd)
    runner = Runner(cfg)
    if os.path.exists(cfg.socket) or os.path.islink(cfg.socket):
        os.unlink(cfg.socket)
    srv = Server(cfg.socket, Handler)
    os.chmod(cfg.socket, 0o660)
    if root and cfg.gid is not None:
        os.chown(cfg.socket, 0, cfg.gid)
    srv.runner = runner

    def stop(_sig, _frm):
        # The running migrate is left alone (KillMode=process): it is in its own session and cleans up itself.
        threading.Thread(target=srv.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    runner.audit("runner-start", version=__version__, cli=runner.cli.get("version"))
    sys.stderr.write("migui-runner %s: listening on %s (peer %s), migrate %s\n"
                     % (__version__, cfg.socket, cfg.peer_user, runner.cli.get("version")))
    sys.stderr.flush()
    try:
        srv.serve_forever(poll_interval=0.5)
    finally:
        srv.server_close()
        try:
            os.unlink(cfg.socket)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
