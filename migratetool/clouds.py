"""The two clouds: the openstack CLI run with an openrc's environment, scoped to SRC/DST_PROJECT.

bash ran '( source "$RC"; scope_project "$PROJECT"; openstack "$@" )' for every call. Here the
openrc is sourced once by bash (it is a bash file) and its environment reused for every call.
"""
import json
import os
import re
import sys

from . import proc
from .util import CommandFailed, Die, UUID_RE, lines

PROXY_VARS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")
_rc_env_cache = {}
# 7.3 (SEC-9): the openstackclient action words. The first one of a command line (after its global options, before
# its own options) is its action: a read-only Cloud runs only list, show and token issue.
OSC_VERBS = frozenset((
    "list", "show", "issue", "create", "delete", "set", "unset", "add", "remove", "stop", "start", "lock", "unlock",
    "save", "reboot", "rebuild", "resize", "migrate", "evacuate", "pause", "unpause", "suspend", "resume", "shelve",
    "unshelve", "rescue", "unrescue", "attach", "detach", "extend", "revert", "backup", "restore", "transfer",
    "accept", "import", "export", "purge", "cleanup", "failover", "retype", "manage", "unmanage", "upload",
    "download", "dump", "abandon", "adopt", "confirm", "revoke", "grant", "rotate", "enable", "disable"))


def action_of(args):
    """(words, action) of an openstack argv: words up to its first option, after leading global options and their
    values; action = the first word that is an openstackclient action, "" when none is."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 1 if "=" in args[i] else 2
    words = []
    for a in args[i:]:
        if a.startswith("-"):
            break
        words.append(a)
    return words, next((w for w in words if w in OSC_VERBS), "")


def read_only(args):
    """SEC-9: list, show, token issue (catalog list, endpoint list, availability zone list, compute service list,
    quota show, limits show, versions show are list/show)."""
    words, action = action_of(args)
    return action in ("list", "show") or (action == "issue" and words[:1] == ["token"])


def base_env(cfg):
    """The environment the bash build ran its children with: proxies unset, the venv's bin first
    (it sourced /opt/mig-venv/bin/activate), conf 'export' lines and MIG_ISCSI_IP/MIG_MULTIPATH."""
    env = dict(os.environ)
    for v in PROXY_VARS:
        env.pop(v, None)
    if sys.prefix != sys.base_prefix:  # running from a venv: put its bin first, as activate does
        env["VIRTUAL_ENV"] = sys.prefix
        env["PATH"] = os.path.join(sys.prefix, "bin") + os.pathsep + env.get("PATH", "")
    env.update(cfg.exported)
    env["MIG_ISCSI_IP"] = cfg.MIG_ISCSI_IP
    if cfg.multipath_set:
        env["MIG_MULTIPATH"] = cfg.MIG_MULTIPATH
    return env


def clean_env(env):
    """B3: the environment an openrc is sourced in: no OS_* of the shell that started migrate (an operator who
    sourced another cloud's openrc would otherwise lend it every variable the openrc does not set itself)."""
    return {k: v for k, v in env.items() if not k.startswith("OS_")}


def rc_env(rc, env):
    """Environment after 'source <rc>' (cached per file). B3: sourced in a clean environment (clean_env) with
    'set -e', so that a command of the openrc that fails stops here instead of leaving half its variables unset;
    an openrc that sets no OS_AUTH_URL is refused. The only 'bash -c' of the package (SEC-7 / E6 whitelist)."""
    if rc not in _rc_env_cache:
        if not os.access(rc, os.R_OK):
            raise Die("cannot read openrc %s" % rc)
        r = proc.run(["bash", "-c", 'set -e; source "$1" >/dev/null 2>&1; exec env -0', "bash", rc],
                     env=clean_env(env), capture=True, stderr=proc.DEVNULL)
        if r.rc != 0:
            raise Die("sourcing openrc %s failed (exit %d): a command in it failed%s" % (rc, r.rc, prompt_hint(rc)))
        new = {}
        for item in r.stdout.split("\0"):
            if "=" in item:
                k, v = item.split("=", 1)
                new[k] = v
        if not new.get("OS_AUTH_URL"):
            raise Die("openrc %s sets no OS_AUTH_URL" % rc)
        _rc_env_cache[rc] = new
    return dict(_rc_env_cache[rc])


_READ_RE = re.compile(r"(?:^|[;&|({]|\bthen|\bdo|\belse)\s*read\b")


def prompt_lines(rc):
    """R1: the line numbers of an openrc that ask for input ('read', as the openrc Horizon downloads does for the
    password). migrate sources an openrc with stdin closed: such a line reads nothing and fails under 'set -e'."""
    try:
        with open(rc, errors="replace") as f:
            text = f.read(1 << 20)
    except OSError:
        return []
    found = []
    for n, line in enumerate(text.splitlines(), 1):
        code = line.split("#", 1)[0]
        if _READ_RE.search(code):
            found.append(n)
    return found


def prompt_hint(rc):
    """R1: the part of the 'sourcing openrc failed' message that names a prompting line ('' when there is none)."""
    found = prompt_lines(rc)
    if not found:
        return ""
    return ("; line %s asks for input ('read'), and migrate sources the openrc without a terminal: write the password "
            "into the file (root, 0600) or use an application credential" % ", ".join(str(n) for n in found[:3]))


def rc_file_problem(rc):
    """B3: why an openrc's owner/mode is unsafe ('' when it is root-owned and not readable by group or others)."""
    try:
        st = os.stat(rc)
    except OSError as e:
        return e.strerror or str(e)
    probs = []
    if st.st_uid != 0:
        probs.append("owner uid %d, not root" % st.st_uid)
    if st.st_mode & 0o077:
        probs.append("mode %04o, not 0600" % (st.st_mode & 0o7777))
    return ", ".join(probs)


def auth_url(rc, env):
    """OS_AUTH_URL of an openrc without its trailing '/' ('' when it cannot be sourced)."""
    try:
        return (rc_env(rc, env).get("OS_AUTH_URL") or "").rstrip("/")
    except Die:
        return ""


class Cloud:
    def __init__(self, side, rc, project, env):
        self.side = side          # "source" / "destination", for messages
        self.rc = rc
        self.project = project or ""
        self._base = env
        self._env = None
        self._pid = ""
        self.readonly = False  # 7.3 (SEC-9): list/show/token issue only, anything else dies before it runs

    def env(self):
        if self._env is None:
            e = rc_env(self.rc, self._base)
            if self.project:  # scope_project
                e["OS_PROJECT_NAME"] = self.project
                for v in ("OS_PROJECT_ID", "OS_TENANT_NAME", "OS_TENANT_ID"):
                    e.pop(v, None)
            self._env = e
        return dict(self._env)

    def auth_type(self):
        return rc_env(self.rc, self._base).get("OS_AUTH_TYPE", "")

    # --- running the CLI -----------------------------------------------------------------------
    def run(self, *args, stderr=proc.INHERIT, capture=True):
        if self.readonly and not read_only(args):
            raise Die("read-only command tried: openstack %s" % " ".join(args))
        return proc.run(["openstack", *args], env=self.env(), capture=capture, stderr=stderr)

    def out(self, *args, stderr=proc.INHERIT):
        """stdout of a command that must succeed (bash: a plain $(...) under set -e)."""
        r = self.run(*args, stderr=stderr)
        if r.rc != 0:
            raise CommandFailed("openstack %s failed (exit %d)%s"
                                % (" ".join(args), r.rc, (": " + r.stderr.strip().splitlines()[-1]) if r.stderr.strip() else ""))
        return r.stdout

    def value(self, *args, stderr=proc.INHERIT):
        """'-f value' output without its trailing newlines, as $(...) gives it."""
        return self.out(*args, stderr=stderr).rstrip("\n")

    def json(self, *args, stderr=proc.INHERIT):
        return json.loads(self.out(*args, stderr=stderr))

    def ok(self, *args):
        """Exit status only, output discarded (bash: '... >/dev/null 2>&1' inside a test)."""
        return self.run(*args, stderr=proc.DEVNULL).rc == 0

    def try_value(self, *args):
        """$(cmd 2>/dev/null) inside a condition: whatever stdout said, even when the command failed."""
        return self.run(*args, stderr=proc.DEVNULL).stdout.rstrip("\n")

    def try_value_inherit(self, *args):
        """$(cmd) inside a condition (the wait loops): stdout whatever happened, stderr shown."""
        return self.run(*args).stdout.rstrip("\n")

    # --- project and security groups ---------------------------------------------------------
    def project_id(self):
        """The token's project (bash dst_project_id / src_project_id)."""
        if not self._pid:
            r = self.run("token", "issue", "-f", "value", "-c", "project_id")
            if r.rc != 0:
                raise Die("cannot get a %s token" % self.side)
            self._pid = r.stdout.rstrip("\n")
        if not self._pid:
            raise Die("the %s token is not scoped to a project" % self.side)
        return self._pid

    def sg_id(self, name):
        """A security group's ID inside this token's own project; '' when absent (bash *_sg_id)."""
        if re.fullmatch(UUID_RE, name):
            return name
        pid = self.project_id()
        r = self.run("security", "group", "list", "--project", pid, "-f", "json")
        if r.rc != 0:
            raise Die("cannot list the security groups of %s project %s" % (self.side, pid))
        ids = [g.get("ID", g.get("id")) for g in json.loads(r.stdout)
               if (g.get("Name") if g.get("Name") is not None else g.get("name")) == name]
        if len(ids) > 1:
            where = self.project or pid
            if self.side == "destination":
                raise Die("%d security groups are named '%s' in destination project %s: %s — pass the ID to --security-group"
                          % (len(ids), name, where, " ".join(ids)))
            raise Die("%d security groups are named '%s' in source project %s: %s"
                      % (len(ids), name, where, " ".join(ids)))
        return ids[0] if ids else ""


def ids_of(text):
    """IDs from '-f value -c ID' output, one per line."""
    return lines(text)
