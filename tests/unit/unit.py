#!/usr/bin/python3
"""Unit checks for pieces the parity runs cannot reach: conf parser, precedence, not-ported exits."""
import os
import subprocess
import sys
import tempfile

H = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(H))
sys.path.insert(0, REPO)
from migratetool import config  # noqa: E402
from migratetool.util import Die  # noqa: E402

PASS = FAIL = 0


def ok(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS " + label)
    else:
        FAIL += 1
        print("FAIL %s %s" % (label, detail))


def conf_of(text, env=None):
    fd, path = tempfile.mkstemp()
    os.write(fd, text.encode())
    os.close(fd)
    e = {"MIG_CONF": path}
    e.update(env or {})
    try:
        return config.load(e)
    finally:
        os.remove(path)


c = conf_of('# comment\nRBD_POOL=volumes   # trailing comment\nRBD_POOL_MAP="a:b,c:d"\n'
            "MIG_DEFAULT_OPTS='--verify none --keep-source-ports'\nexport OS_CACERT=/etc/ca.pem\n"
            "SYS_PY=/opt/py/bin/python3\nMIG_AZ=AZ1 AZ_TYPE_MAP=AZ1:t1\n")
ok("unquoted value + inline comment", c.RBD_POOL == "volumes", c.RBD_POOL)
ok("double-quoted value", c.RBD_POOL_MAP == "a:b,c:d")
ok("single-quoted value with spaces", c.MIG_DEFAULT_OPTS == "--verify none --keep-source-ports")
ok("two assignments on one line", c.MIG_AZ == "AZ1" and c.AZ_TYPE_MAP == "AZ1:t1")
ok("export of a non-conf var reaches the children", c.exported == {"OS_CACERT": "/etc/ca.pem"}, c.exported)
ok("defaults when unset", c.RBD_USER == "mig-xfer" and c.COPY_STREAMS == "8" and c.CEPH_CONF == "/etc/ceph/ceph.conf")
ok("MIG_ISCSI_IP has no default (1.0.0: the lab address is gone; doctor FAILs until it is set)", c.MIG_ISCSI_IP == "")
c = conf_of("RBD_POOL=fromconf\nSYS_PY=/conf/py\n", env={"RBD_POOL": "fromenv", "SYS_PY": "/env/py"})
ok("environment beats conf (saved var)", c.RBD_POOL == "fromenv")
ok("SYS_PY: conf beats environment (not saved by the bash loader)", c.SYS_PY == "/conf/py", c.SYS_PY)
c = conf_of("RBD_POOL=fromconf\n", env={"RBD_POOL": ""})
ok("empty env value falls back to the default, not to the conf", c.RBD_POOL == "mig-volumes", c.RBD_POOL)
c = conf_of("MIG_MULTIPATH=1\n")
ok("MIG_MULTIPATH from conf marks it exported", c.multipath_set and c.MIG_MULTIPATH == "1")
c = conf_of("")
ok("MIG_MULTIPATH unset stays unexported", not c.multipath_set)
for bad, label in (('RBD_POOL="$OTHER"\n', "$-expansion refused"), ("source /etc/x\n", "command refused"),
                   ("RBD_POOL=a; rm -rf /\n", "command after assignment refused")):
    try:
        conf_of(bad)
        ok(label, False, "no error")
    except Die as e:
        ok(label, "line 1" in str(e), str(e))
c = conf_of("RBD_POOL='$literal'\n")
ok("$ inside single quotes is literal", c.RBD_POOL == "$literal")

# --- selector parser (P4) -------------------------------------------------------------------------
from migratetool import selector  # noqa: E402


def sel(verb, args):
    return selector.finish(selector.split_args(verb, args))


def dies(label, verb, args, want):
    try:
        sel(verb, args)
        ok("parser dies: " + label, False, "no error")
    except Die as e:
        ok("parser dies: " + label, want in str(e), str(e))


s = sel("migrate", ["app01", "--yes", "--verify", "none", "--continue-on-error", "--start-source", "--ip", "new"])
ok("positional = --vm, control vs per-VM split", s.vms == ["app01"] and s.yes and s.cont
   and s.passed == ["--verify", "none", "--start-source", "--ip", "new"], vars(s))
s = sel("plan", ["--vm", "b,a", "x", "--vm", " c , ,a", "--host", "h1,h2", "--project", "p"])
ok("lists: positional first, comma lists trimmed, duplicates dropped", s.vms == ["x", "b", "a", "c"]
   and s.hosts == ["h1", "h2"] and s.projects == ["p"], vars(s))
fd, vf = tempfile.mkstemp()
os.write(fd, b"# header\n  vm1  \n\nvm2 # trailing\nvm1\n\tvm3")
os.close(fd)
s = sel("migrate", ["--vm", "vm0", "--vm-file", vf])
ok("--vm-file: comments, blanks, trim, dedupe, no final newline", s.vms == ["vm0", "vm1", "vm2", "vm3"], s.vms)
os.remove(vf)
dies("unreadable --vm-file", "migrate", ["--vm-file", "/nonexistent/f"], "cannot read --vm-file /nonexistent/f")
dies("nothing selected", "migrate", ["--yes"], "select something: <vm>, --vm, --host, --project or --vm-file")
dies("two positionals", "migrate", ["a", "b"], "one VM only (got: a b): select several with --vm a,b")
dies("'--' of the batch", "migrate", ["--vm", "a", "--", "--verify", "none"], "no '--' any more")
dies("--plan-only of the batch", "migrate", ["--vm", "a", "--plan-only"], "--plan-only is now a command of its own")
dies("--volumes of the batch", "migrate", ["--project", "p", "--volumes", "all"], "--volumes is now: migrate volume")
dies("unknown option", "plan", ["a", "--bogus"], "unknown option --bogus")
dies("--vm without value", "plan", ["--vm"], "--vm needs a value")
dies("--vm empty value", "plan", ["--vm", ""], "--vm needs a value")
dies("per-VM option without value", "plan", ["a", "--verify"], "--verify needs a value")
dies("scope options belong to volume", "plan", ["--project", "p", "--available"], "unknown option --available")
s = sel("volume", ["--vm", "a,b", "--verify", "none"])
ok("volume --vm: attached by default", s.scope == "attached" and s.vms == ["a", "b"], vars(s))
s = sel("volume", ["--project", "p", "--all", "--vm", "a"])
ok("volume --project --all --vm", s.scope == "all", vars(s))
dies("volume --project without scope", "volume", ["--project", "p"], "migrate volume --project needs --available, --attached or --all")
dies("volume two scopes", "volume", ["--project", "p", "--available", "--attached"], "choose one of --available, --attached, --all")
dies("volume --available without --project", "volume", ["--vm", "a", "--available"], "migrate volume --available needs --project")
dies("volume --available with --vm", "volume", ["--project", "p", "--available", "--vm", "a"], "drop --vm/--vm-file")
dies("volume --all with --host", "volume", ["--project", "p", "--all", "--host", "h"], "--host filters VMs")
dies("volume --host alone", "volume", ["--host", "h"], "migrate volume --attached needs --project or --vm/--vm-file")
dies("volume selection --in-use", "volume", ["--vm", "a", "--in-use"], "--in-use copies one attached volume")
dies("volume selection --mode live", "volume", ["--vm", "a", "--mode", "live"], "copies cold only: drop --mode live")
dies("volume nothing selected", "volume", ["--yes"], "select something: <volume>")
ok("name regex escaped like the batch's sed", "^" + selector.RE_ESCAPE.sub(r"\\\1", "a.b[1]*(x)|y$") + "$"
   == r"^a\.b\[1\]\*\(x\)\|y\$$")

# --- the CLI (subprocess) -------------------------------------------------------------------------
RCDIR = tempfile.mkdtemp()
for _side in ("src", "dst"):
    with open(os.path.join(RCDIR, _side + ".rc"), "w") as _f:
        _f.write("export OS_AUTH_URL=http://%s:5000/v3\nexport OS_AUTH_TYPE=password\nexport OS_PROJECT_NAME=admin\n"
                 % _side)
env = dict(os.environ, PYTHONPATH=REPO, MIG_CONF="/nonexistent",
           SRC_RC=os.path.join(RCDIR, "src.rc"), DST_RC=os.path.join(RCDIR, "dst.rc"), PATH="/usr/bin:/bin")
# Intended: 7.2 C2: the number messages name their lower bound.
for args, want in ((["plan", "vm", "--mode", "live", "--max-passes", "x"], "--max-passes takes a whole number >= 1, not 'x'"),
                   (["plan", "vm", "--mode", "live", "--delta-limit", "-1"], "--delta-limit takes a whole number >= 0, not '-1'"),
                   (["migrate", "vm", "--in-use"], "--in-use belongs to migrate-volume"),
                   (["migrate", "vm", "--start-source"], "--start-source needs --ip new or --ip <addr>"),
                   (["volume", "v1", "--project", "p"], "a volume goes alone"),
                   (["volume", "v1", "v2"], "one volume only (got: v1 v2)"),
                   (["volume", "v1", "--ip", "new"], "creates no server or port: --ip cannot apply"),
                   (["batch", "--vm", "a"], "there is no 'batch' command"),
                   (["migrate-volume", "v1"], "'migrate-volume' is now: migrate volume <volume>"),
                   (["rollback", "a", "b"], "rollback takes one target (got 2)"),  # 1.0.0 (A5): rollback takes --yes, --dry-run
                   (["rollback", "a", "--force"], "unknown option --force"),
                   (["frob"], "unknown command frob")):
    p = subprocess.run([sys.executable, "-m", "migratetool", *args], env=env, capture_output=True, text=True, cwd="/",
                       stdin=subprocess.DEVNULL)
    ok("dies: " + " ".join(args), p.returncode == 1 and want in p.stderr, p.stderr.strip())
for args, rc, want in ((["plan"], 2, "usage: migrate plan"), (["volume"], 2, "usage: migrate volume"),
                       (["rollback"], 2, "usage: migrate rollback"),
                       (["migrate", "--help"], 0, "--continue-on-error"), (["migrate", "x", "-h"], 0, "--vm-file"),
                       (["volume", "--help"], 0, "--available"), (["plan", "--help"], 0, "options of each VM")):
    p = subprocess.run([sys.executable, "-m", "migratetool", *args], env=env, capture_output=True, text=True, cwd="/")
    ok("help: " + " ".join(args), p.returncode == rc and want in p.stdout, (p.returncode, p.stdout[-300:], p.stderr))
p = subprocess.run([sys.executable, "-m", "migratetool.single"], env=env, capture_output=True, text=True, cwd="/")
ok("child entry without arguments", p.returncode == 1 and "usage: python -m migratetool.single" in p.stderr, p.stderr)
import hashlib  # noqa: E402
bundled = os.path.join(REPO, "migratetool", "blocksync.py")
# 1.0.0 (A1): the bundled blocksync = archive/blocksync.py + read_exact (a short read fails the pass). Every line of
# the archived one is still there, in order, apart from the pread it replaced and the exit-code line of the docstring.
_arch = open(os.path.join(REPO, "archive", "blocksync.py")).read().splitlines()
import difflib  # noqa: E402
_gone = [l[2:] for l in difflib.ndiff(_arch, open(bundled).read().splitlines()) if l.startswith("- ")]
ok("bundled blocksync.py = archive/blocksync.py + A1 read_exact (only the pread line and the exit-code line differ)",
   len(_gone) == 2 and "os.pread(fd" in _gone[1] and "def read_exact(fd, size, off):" in open(bundled).read(), _gone)
p = subprocess.run([sys.executable, "-m", "migratetool"], env=env, capture_output=True, text=True, cwd="/")
ok("no arguments: usage, exit 2", p.returncode == 2 and "usage: migrate" in p.stdout)
p = subprocess.run([sys.executable, "-m", "migratetool", "plan", "vm", "--help"], env=env, capture_output=True, text=True, cwd="/")
ok("plan vm --help", p.returncode == 0 and "--server-group NAME" in p.stdout)
p = subprocess.run([sys.executable, "-m", "migratetool", "--version"], env=env, capture_output=True, text=True, cwd="/")
ok("--version", p.returncode == 0 and "migrate 1.0.0" in p.stdout, p.stdout + p.stderr)
print("TOTAL pass=%d fail=%d" % (PASS, FAIL))
