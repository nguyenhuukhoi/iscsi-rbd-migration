#!/usr/bin/env python3
"""V7-P5 unit checks: 'migrate plan volume', the --plan-only/--volumes hints, the -I child entry.

Runs anywhere Python >= 3.10 runs (no openstack, no bash): the clouds and the per-row child are faked.

    python p5unit.py <directory holding migratetool/>
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout

PKG_PARENT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
sys.path.insert(0, PKG_PARENT)
from migratetool import cli, config, selector  # noqa: E402
from migratetool.proc import Result  # noqa: E402
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


# --- 1. parser ----------------------------------------------------------------------------------
def sel(verb, args, plan_only=False):
    return selector.finish(selector.split_args(verb, args, plan_only))


def dies(label, fn, want):
    try:
        fn()
        ok(label, False, "no error")
    except Die as e:
        ok(label, want in str(e), str(e))


s = sel("volume", ["--project", "p", "--available", "--verify", "none"], plan_only=True)
ok("plan volume --project --available: scope, plan_only, options passed", s.plan_only and s.scope == "available"
   and s.projects == ["p"] and s.passed == ["--verify", "none"], vars(s))
s = sel("volume", ["--vm", "a,b"], plan_only=True)
ok("plan volume --vm: attached by default", s.scope == "attached" and s.vms == ["a", "b"], vars(s))
s = sel("volume", ["--project", "p", "--all", "--vm", "a"], plan_only=True)
ok("plan volume --project --all --vm", s.scope == "all", vars(s))
dies("plan volume --project without scope: names 'migrate plan volume'",
     lambda: sel("volume", ["--project", "p"], True), "migrate plan volume --project needs --available, --attached or --all")
dies("plan volume --available without --project", lambda: sel("volume", ["--vm", "a", "--available"], True),
     "migrate plan volume --available needs --project")
dies("plan volume --host alone", lambda: sel("volume", ["--host", "h"], True),
     "migrate plan volume --attached needs --project or --vm/--vm-file")
dies("plan volume --in-use", lambda: sel("volume", ["--vm", "a", "--in-use"], True), "--in-use copies one attached volume")
dies("plan volume --mode live", lambda: sel("volume", ["--vm", "a", "--mode", "live"], True),
     "copies cold only: drop --mode live")
dies("plan volume, two scopes", lambda: sel("volume", ["--project", "p", "--available", "--all"], True),
     "choose one of --available, --attached, --all")
dies("unchanged: migrate volume --project without scope", lambda: sel("volume", ["--project", "p"]),
     "migrate volume --project needs --available, --attached or --all")
dies("hint: volume --plan-only -> plan volume", lambda: sel("volume", ["--project", "p", "--available", "--plan-only"]),
     "--plan-only is now a command of its own: migrate plan volume --project <p> --available|--attached|--all, "
     "or migrate plan volume --vm <vms>")
dies("hint: lone volume --plan-only -> plan volume", lambda: selector.split_args("volume", ["v1", "--plan-only"]),
     "migrate plan volume")
dies("hint: plan --volumes -> plan volume", lambda: sel("plan", ["--project", "p", "--volumes", "available"]),
     "--volumes is now: migrate plan volume --project <p> --available|--attached|--all, or migrate plan volume --vm <vms>")
dies("unchanged: migrate --volumes -> volume", lambda: sel("migrate", ["--project", "p", "--volumes", "all"]),
     "--volumes is now: migrate volume --project <p>")
dies("unchanged: migrate --plan-only -> plan", lambda: sel("migrate", ["--vm", "a", "--plan-only"]),
     "--plan-only is now a command of its own: migrate plan <vm>")
ok("child entry runs isolated: [python, -I, -m, migratetool.single]",
   selector.CHILD == [sys.executable, "-I", "-m", "migratetool.single"], selector.CHILD)


# --- 2. the selector run, clouds and children faked ------------------------------------------------
ADMIN, PB = "pid-admin", "pid-projb"
V1, V2, V3, V4, V5 = ("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222",
                      "33333333-3333-4333-8333-333333333333", "44444444-4444-4444-8444-444444444444",
                      "55555555-5555-4555-8555-555555555555")
VM1, VM2 = "aaaaaaaa-0000-4000-8000-0000000000a1", "aaaaaaaa-0000-4000-8000-0000000000a2"


def world():
    return {
        "projects": {ADMIN: "admin", PB: "proj-b"},
        "servers": [{"ID": VM1, "Name": "web-01", "Status": "ACTIVE", "Project ID": PB, "Host": "cmp-01"},
                    {"ID": VM2, "Name": "web-02", "Status": "SHUTOFF", "Project ID": PB, "Host": "cmp-01"}],
        "volumes": {PB: [{"ID": V1, "Name": "data-c", "Status": "available"},
                         {"ID": V2, "Name": None, "Status": "available"},
                         {"ID": V3, "Name": "mig-clone-xyz", "Status": "available"},
                         {"ID": V4, "Name": "broken", "Status": "error"},
                         {"ID": V5, "Name": "web-01-vdb", "Status": "in-use"}],
                    ADMIN: []},
    }


class FakeCloud:
    def __init__(self, w, calls):
        self.w, self.calls = w, calls

    def auth_type(self):
        return "password"

    def project_id(self):
        return ADMIN

    def _pid(self, p):
        return p if p in self.w["projects"] else {v: k for k, v in self.w["projects"].items()}.get(p)

    def run(self, *args, stderr=None, capture=True):
        self.calls.append(" ".join(args))
        a = list(args)
        if a[:2] == ["server", "list"]:
            got = self.w["servers"]
            if "--project" in a:
                pid = self._pid(a[a.index("--project") + 1])
                got = [x for x in got if x["Project ID"] == pid]
            if "--host" in a:
                got = [x for x in got if x["Host"] == a[a.index("--host") + 1]]
            if "--name" in a:
                import re
                rx = a[a.index("--name") + 1]
                got = [x for x in got if re.search(rx, x["Name"])]
            return Result(0, json.dumps([{k: v for k, v in x.items() if k != "Host"} for x in got]), "")
        if a[:2] == ["server", "show"]:
            return Result(1, "", "No server")
        if a[:2] == ["project", "show"]:
            pid = self._pid(a[2])
            if pid is None:
                return Result(1, "", "No project")
            if "json" in a:
                return Result(0, json.dumps({"id": pid, "name": self.w["projects"][pid]}), "")
            return Result(0, (self.w["projects"][pid] if "name" in a else pid) + "\n", "")
        if a[:2] == ["volume", "list"]:
            return Result(0, json.dumps(self.w["volumes"].get(a[a.index("--project") + 1], [])), "")
        raise AssertionError("unexpected call: %s" % " ".join(a))

    def out(self, *args, stderr=None):
        r = self.run(*args)
        if r.rc:
            raise Die("openstack %s failed" % " ".join(args))
        return r.stdout

    def value(self, *args, stderr=None):
        return self.out(*args).rstrip("\n")

    def json(self, *args, stderr=None):
        return json.loads(self.out(*args))

    def try_value(self, *args):
        return self.run(*args).stdout.rstrip("\n")


def sel_run(args, w=None, child_rc=None, pre_state=(), tty=False):
    """'migrate plan volume <args>' with fake clouds: (rc, stdout, child calls, cloud calls)."""
    tmp = tempfile.mkdtemp()
    config.STATE_ROOT = os.path.join(tmp, "state")
    config.LOG_ROOT = os.path.join(tmp, "log")
    for n in pre_state:
        os.makedirs(os.path.join(config.STATE_ROOT, n))
        open(os.path.join(config.STATE_ROOT, n, "volume-map"), "w").close()
        with open(os.path.join(config.STATE_ROOT, n, "t-stop"), "w") as f:  # 1.0.0 (A6): past its cutover
            f.write("1700000000\n")
    children, calls = [], []
    w = w or world()
    orig_init, orig_child = selector.Runner.__init__, selector.Runner.child

    def init(self, s, cfg):
        orig_init(self, s, cfg)
        self.src, self.dst = FakeCloud(w, calls), FakeCloud(w, calls)

    def child(self, r, verb, a, logf, tee, prefix="", own_group=False):
        children.append((verb, r.ident, list(a), tee, dict(r.env)))
        rc = (child_rc or {}).get(r.id, 0)
        text = "Destination preflight (volumes-only):\n  OK    something\n" if rc == 0 else "  FAIL  flavor m1.big\n"
        with open(logf, "w") as f:
            f.write(text)
        if tee:
            sys.stdout.write(text)
        return rc

    class NoTTY(io.StringIO):
        def isatty(self):
            return tty

    selector.Runner.__init__, selector.Runner.child = init, child
    old_env, old_stdin = dict(os.environ), sys.stdin
    os.environ.update({"MIG_CONF": os.path.join(tmp, "none.conf"), "SRC_RC": "/x/src.rc", "DST_RC": "/x/dst.rc"})
    sys.stdin = NoTTY("")
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = cli.main(["plan", "volume"] + args)
    finally:
        selector.Runner.__init__, selector.Runner.child = orig_init, orig_child
        os.environ.clear()
        os.environ.update(old_env)
        sys.stdin = old_stdin
        shutil.rmtree(tmp, ignore_errors=True)
    return rc, buf.getvalue(), children, calls


def table(text, title):
    """Rows of one table ('KIND' header excluded)."""
    lines = text.split("\n")
    i = lines.index(title)
    rows = []
    for l in lines[i + 2:]:
        if not l.startswith(("vm ", "vol ")):
            break
        rows.append(l.split())
    return rows


rc, o, ch, calls = sel_run(["--project", "proj-b", "--available"])
plan = table(o, "Plan:")
res = {r[1]: r[-1] for r in plan}
ok("available: READY for the two copyable volumes", res.get(V1) == "READY" and res.get(V2) == "READY", plan)
ok("available: leftover clone SKIP(mig-clone), error volume SKIP(status), in-use not listed",
   res.get(V3) == "SKIP(mig-clone)" and res.get(V4) == "SKIP(status)" and V5 not in res, plan)
ok("available: no child process at all (no preflight for a volume)", ch == [], ch)
ok("available: Selected and Plan tables, exit 0, no prompt",
   rc == 0 and "Selected:" in o and "Plan:" in o and "Type yes" not in o and "Summary:" not in o, (rc, o))
ok("available: read-only listing calls only", all(c.split()[0] in ("server", "project", "volume") and
                                                  c.split()[1] in ("list", "show") for c in calls), calls)
ok("available: the in-use note names the attached selection",
   "1 in-use volume(s) not listed — they go with their VM (migrate volume --attached)" in o, o)

rc, o, ch, _ = sel_run(["--project", "proj-b", "--available"], pre_state=("vol-" + V1,))
res = {r[1]: r[-1] for r in table(o, "Plan:")}
ok("available: earlier state -> SKIP(state-exists) + rollback hint by volume ID",
   res.get(V1) == "SKIP(state-exists)" and res.get(V2) == "READY" and ("rollback " + V1) in o and rc == 0, o)

w = world()
w["volumes"][PB] = [v for v in w["volumes"][PB] if v["ID"] in (V3, V4, V5)]
rc, o, ch, _ = sel_run(["--project", "proj-b", "--available"], w=w)
ok("available: rows but none copyable (all SKIP) -> exit 1", rc == 1 and "SKIP(mig-clone)" in o, (rc, o))

w = world()
w["volumes"][PB] = [v for v in w["volumes"][PB] if v["ID"] == V5]
rc, o, ch, _ = sel_run(["--project", "proj-b", "--available"], w=w)
ok("available: no row at all -> 'nothing selected', exit 0 (as every selector run)",
   rc == 0 and "nothing selected" in o and "Selected:" not in o, (rc, o))

rc, o, ch, _ = sel_run(["--vm", "web-01,web-02", "--verify", "none"])
ok("--vm: one 'plan <vm> --volumes-only <options>' child per VM, shown on stdout",
   [(c[0], c[1], c[2], c[3]) for c in ch] == [("plan", "web-01", ["--volumes-only", "--verify", "none"], True),
                                              ("plan", "web-02", ["--volumes-only", "--verify", "none"], True)], ch)
ok("--vm: children scoped to the VM's project", all(c[4] == {"SRC_PROJECT": "proj-b", "DST_PROJECT": "proj-b"}
                                                    for c in ch), ch)
res = {r[1]: r[-1] for r in table(o, "Plan:")}
ok("--vm: PLAN-OK rows, exit 0, never a migrate child", res == {VM1: "PLAN-OK", VM2: "PLAN-OK"} and rc == 0
   and all(c[0] == "plan" for c in ch) and "  OK    something" in o, (res, rc))

rc, o, ch, _ = sel_run(["--vm", "web-01,web-02"], child_rc={VM2: 1})
res = {r[1]: r[-1] for r in table(o, "Plan:")}
ok("--vm: a failing preflight -> PLAN-FAIL with its FAIL line, exit 1",
   res.get(VM2) == "PLAN-FAIL" and "    FAIL  flavor m1.big" in o and rc == 1, (res, rc, o))

rc, o, ch, _ = sel_run(["--project", "proj-b", "--all"])
res = [(r[0], r[1], r[-1]) for r in table(o, "Plan:")]
ok("--all: VMs planned (attached first), then the available volumes READY",
   [x[0] for x in res] == ["vm", "vm", "vol", "vol", "vol", "vol"] and len(ch) == 2 and rc == 0, res)

rc, o, ch, _ = sel_run(["--project", "proj-b", "--attached"], pre_state=("web-01",))
res = {r[1]: r[-1] for r in table(o, "Plan:")}
ok("--attached: state under the VM name -> SKIP(state-exists), the other planned",
   res.get(VM1) == "SKIP(state-exists)" and res.get(VM2) == "PLAN-OK" and len(ch) == 1 and rc == 0, res)

rc, o, ch, _ = sel_run(["--vm", "nosuch"])
ok("--vm nosuch: SKIP(not-found) only -> exit 1", rc == 1 and "SKIP(not-found)" in o and ch == [], (rc, o))

rc, o, ch, _ = sel_run(["--project", "proj-b", "--available", "--yes"])
ok("--yes is accepted and changes nothing (as with 'migrate plan')", rc == 0 and ch == [] and "Type yes" not in o)


# --- 3. the CLI entry (subprocess, from a scratch directory) ---------------------------------------
scratch = tempfile.mkdtemp()
env = dict(os.environ, PYTHONPATH=PKG_PARENT, MIG_CONF=os.path.join(scratch, "none.conf"),
           SRC_RC=os.path.join(scratch, "src.rc"), DST_RC=os.path.join(scratch, "dst.rc"))


def cli_run(*args):
    return subprocess.run([sys.executable, "-m", "migratetool", *args], env=env, capture_output=True, text=True,
                          cwd=scratch, stdin=subprocess.DEVNULL)


p = cli_run("plan", "volume")
ok("plan volume, no arguments: its usage, exit 2", p.returncode == 2 and "usage: migrate plan volume" in p.stdout,
   (p.returncode, p.stdout[:200], p.stderr))
p = cli_run("plan", "volume", "--help")
ok("plan volume --help: exit 0, selection without --yes/--in-use",
   p.returncode == 0 and "--available" in p.stdout and "--volume-type" in p.stdout and "--yes" not in p.stdout
   and "--in-use" not in p.stdout and "READY" in p.stdout, (p.returncode, p.stdout))
p = cli_run("plan", "volume", "--project", "p", "-h")
ok("plan volume ... -h: help, exit 0", p.returncode == 0 and "usage: migrate plan volume" in p.stdout)
p = cli_run("plan", "volume", "v1")
ok("plan volume <volume>: dies, no dry-run for one volume",
   p.returncode == 1 and "A single volume has no dry-run: migrate volume <volume> runs its read-only checks" in p.stderr,
   p.stderr)
p = cli_run("plan", "volume", "v1", "--project", "p", "--available")
ok("plan volume <volume> + selection: same die", p.returncode == 1 and "plan volume takes a selection" in p.stderr,
   p.stderr)
p = cli_run("volume", "v1", "--plan-only")
ok("volume <volume> --plan-only: hint to plan volume",
   p.returncode == 1 and "--plan-only is now a command of its own: migrate plan volume" in p.stderr, p.stderr)
p = cli_run("volume", "--project", "p", "--available", "--plan-only")
ok("volume --project --available --plan-only: hint to plan volume",
   p.returncode == 1 and "migrate plan volume --project <p> --available|--attached|--all" in p.stderr, p.stderr)
p = cli_run("plan", "volume", "--project", "p")
ok("plan volume --project without scope: dies before any cloud call",
   p.returncode == 1 and "migrate plan volume --project needs --available, --attached or --all" in p.stderr, p.stderr)
p = cli_run("plan", "--help")
ok("plan --help points at plan volume", p.returncode == 0 and "migrate plan volume --project P" in p.stdout, p.stdout)
p = cli_run("volume", "--help")
ok("volume --help unchanged + points at plan volume",
   p.returncode == 0 and "--in-use" in p.stdout and "--continue-on-error" in p.stdout
   and "Dry-run of a selection: migrate plan volume" in p.stdout, p.stdout)
p = cli_run("--help")
ok("migrate --help lists plan volume",  # argparse wraps the line: compare with whitespace collapsed
   p.returncode == 0 and "('plan volume <selection>': the dry-run of 'volume')" in " ".join(p.stdout.split()), p.stdout)
p = cli_run("plan", "--vm", "volume", "--mode", "live", "--max-passes", "x")
ok("a VM named 'volume' still plans with --vm volume (reaches the per-VM checks)",
   p.returncode == 1 and "--max-passes takes a whole number >= 1, not 'x'" in p.stderr, p.stderr)


# --- 4. -I: a migratetool/ source tree in the current directory does not shadow the installed one ---
venv = os.environ.get("P5_VENV_PY")  # python of a venv where the package is pip-installed
if venv:
    decoy = tempfile.mkdtemp()
    os.makedirs(os.path.join(decoy, "migratetool"))
    for f in ("__init__.py", "single.py"):
        open(os.path.join(decoy, "migratetool", f), "w").write("print('DECOY source tree imported')\n")
    e = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    plain = subprocess.run([venv, "-m", "migratetool.single"], cwd=decoy, env=e, capture_output=True, text=True)
    iso = subprocess.run([venv, "-I", "-m", "migratetool.single"], cwd=decoy, env=e, capture_output=True, text=True)
    ok("without -I: the decoy in the current directory wins (the bug)", "DECOY" in plain.stdout,
       (plain.stdout, plain.stderr))
    ok("with -I: the installed child entry runs (its usage message), not the decoy",
       "DECOY" not in iso.stdout and iso.returncode == 1 and "usage: python -m migratetool.single" in iso.stderr,
       (iso.stdout, iso.stderr))
    e2 = dict(e, SRC_PROJECT="proj-b", DST_PROJECT="proj-b", MIG_CONF="/nonexistent", SRC_RC="/x/s.rc")
    code = ("import os, sys, migratetool.config as c; cfg = c.load(); "
            "print(cfg.SRC_PROJECT, cfg.DST_PROJECT, cfg.SRC_RC, sys.flags.isolated, "
            "os.path.dirname(c.__file__) != os.path.join(os.getcwd(), 'migratetool'))")
    env_probe = subprocess.run([venv, "-I", "-c", code], cwd=decoy, env=e2, capture_output=True, text=True)
    ok("with -I: SRC_PROJECT/DST_PROJECT/SRC_RC still reach config.load(), package from site-packages",
       env_probe.stdout.split() == ["proj-b", "proj-b", "/x/s.rc", "1", "True"], (env_probe.stdout, env_probe.stderr))
    shutil.rmtree(decoy, ignore_errors=True)
else:
    print("SKIP -I checks: set P5_VENV_PY to the python of a venv with the package installed")

shutil.rmtree(scratch, ignore_errors=True)
print("TOTAL pass=%d fail=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
