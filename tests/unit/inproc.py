"""In-process harness for the 7.3 unit checks (v73unit.py): the same machinery as v72unit.py, importable.

proc.run is replaced by a dispatcher that executes the stub scripts (stub_openstack.py, stub_tools.py, ...) in this
process; selector children (migratetool.single) run in this process too, so a selector run with its plan children
takes milliseconds. Linux only (the brick stub hands out /dev/loop0); stdlib only.

    import inproc; r = inproc.app(world, [("cli", ["list", "--host", "cmp1", "--json"])])
"""
import io
import json
import os
import sys
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace as NS

H = os.path.dirname(os.path.abspath(__file__))
PKG_PARENT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(H)))
sys.path.insert(0, PKG_PARENT)
sys.path.insert(1, H)
from migratetool import cli, clouds, config, copyvol, doctor, jsonout, livesync, proc, selector, single  # noqa: E402
from migratetool.proc import Result  # noqa: E402
import parity  # noqa: E402  (base_world, and the fake Glance server it starts)

WORK = tempfile.mkdtemp(prefix="v73unit-")
time.sleep = lambda s: None  # every wait and retry loop of the app
ORIG_RUN = proc.run
CODE = {}
MISSING = set()  # programs a case pretends are not installed
CHILD_ARGV = []  # every selector child: [verb, ident, *args]
# A7: the fake migrate of tests/ui runs under migui-runner's lock (MIG_LOCK + MIG_LOCK_FD in its environment)
OUTER_LOCK = os.environ.get("MIG_LOCK") if os.environ.get("MIG_LOCK_FD") else None


def _code(name):
    if name not in CODE:
        path = os.path.join(H, name)
        with open(path) as f:
            CODE[name] = compile(f.read(), path, "exec")
    return CODE[name]


def _exec(script, argv, env):
    """Runs a stub script as a program: (rc, stdout, stderr)."""
    saved_env, saved_argv = dict(os.environ), sys.argv
    os.environ.clear()
    os.environ.update(env or saved_env)
    sys.argv = argv
    so, se = io.StringIO(), io.StringIO()
    rc = 0
    try:
        with redirect_stdout(so), redirect_stderr(se):
            try:
                exec(_code(script), {"__name__": "__main__", "__file__": os.path.join(H, script)})
            except SystemExit as e:
                if isinstance(e.code, int):
                    rc = e.code
                elif e.code is not None:
                    se.write(str(e.code) + "\n")
                    rc = 1
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        sys.argv = saved_argv
    return rc, so.getvalue(), se.getvalue()


def fake_run(cmd, env=None, capture=True, stderr=proc.INHERIT):
    prog = os.path.basename(cmd[0])
    if prog in MISSING:
        res = (127, "", "%s: command not found" % prog)
    elif cmd[:len(copyvol.BRICK)] == copyvol.BRICK:
        if cmd[len(copyvol.BRICK):] == ["--check"]:
            res = (0, "ok\n", "")
        elif cmd[len(copyvol.BRICK):] == ["--check-auth"]:  # 1.0.0 (C7): doctor asks brick for a token
            res = (0, "ok psrc\n", "")
        else:
            res = _exec("stub_tools.py", ["brick"] + cmd[len(copyvol.BRICK):], env)
    elif livesync.BLOCKSYNC in cmd:
        res = _exec("stub_blocksync.py", ["blocksync.py"] + cmd[cmd.index(livesync.BLOCKSYNC) + 1:], env)
    elif prog == "openstack":
        if cmd[1:] == ["--version"]:
            res = (0, "openstack 7.0.0\n", "")
        else:
            res = _exec("stub_openstack.py", cmd, env)
    elif prog in ("rbd", "qemu-img", "systemctl", "ceph"):
        res = _exec("stub_tools.py", cmd, env)
    elif "-c" in cmd[1:3] and "block_storage" in cmd[cmd.index("-c") + 1]:  # 1.0.0 (B2): sys.executable -I -c
        res = _exec("stub_python3.py", [cmd[0]] + cmd[cmd.index("-c"):], env)
    elif cmd[1:] == ["-I", "-c", "import rados, rbd"]:
        res = (0, "", "")
    elif prog in ("iscsiadm", "multipath"):
        res = (0, "%s version 2.1.9\n" % prog, "")
    else:
        return ORIG_RUN(cmd, env=env, capture=capture, stderr=stderr)
    rc, so, se = res
    if not capture:
        sys.stdout.write(so)
        so = ""
    if stderr == proc.INHERIT:
        sys.stderr.write(se)
    elif stderr not in (proc.DEVNULL, proc.CAPTURE):
        with open(stderr, "w") as f:
            f.write(se)
    return Result(rc, so, se if stderr == proc.CAPTURE else "")


def fake_stream(cmd, env=None, on_stderr_line=None):
    """proc.run_stream (7.3, --progress-json): stdout captured, each stderr line handed over as it comes."""
    res = fake_run(cmd, env=env, capture=True, stderr=proc.CAPTURE)
    for line in res.stderr.splitlines(True):
        if on_stderr_line:
            on_stderr_line(line)
    return Result(res.rc, res.stdout, "")


proc.run = fake_run
if hasattr(proc, "run_stream"):
    proc.run_stream = fake_stream


def fake_child(self, r, verb, args, logf, tee, prefix="", own_group=False):
    """selector.Runner.child in this process: single.main with the row's SRC_PROJECT/DST_PROJECT, its stdout and
    stderr into the log (and to our stdout when tee), as the real child's are."""
    CHILD_ARGV.append([verb, r.ident, *args])
    saved = dict(os.environ)
    os.environ.update(r.env)
    buf = io.StringIO()
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            rc = single.main([verb, r.ident, *args])
    finally:
        os.environ.clear()
        os.environ.update(saved)
        reset_flags()
    with open(logf, "w", encoding="utf-8") as f:
        f.write(buf.getvalue())
    if tee:
        sys.stdout.write(buf.getvalue())
    return rc


def reset_flags():
    """Module flags a child or a run sets (--json marker, --progress-json): a new run starts clean."""
    for mod, name, value in ((jsonout, "EFFECTIVE_LINE", False),):
        if hasattr(mod, name):
            setattr(mod, name, value)
    try:
        from migratetool import progress
        progress.reset()
    except ImportError:
        pass


selector.Runner.child = fake_child


def world():
    return parity.base_world()


def app(w, steps, env=None):
    """Runs the steps on world w: ('plan'|'migrate', vm, [options]) (the per-VM engine), ('rollback', target),
    ('volume', vol, [options]), ('doctor',), ('cli', [argv]) (the 'migrate' command itself, selector children in
    this process), or a callable(world_path, state_root) between steps."""
    d = tempfile.mkdtemp(dir=WORK)
    wf, calls = os.path.join(d, "world.json"), os.path.join(d, "calls")
    w = json.loads(json.dumps(w))
    w.setdefault("glance_url", parity.GLANCE_URL)
    with open(wf, "w") as f:
        json.dump(w, f)
    open(calls, "w").close()
    parity.GLANCE["world"], parity.GLANCE["calls"] = wf, calls
    for side in ("src", "dst"):
        with open(os.path.join(d, side + ".rc"), "w") as f:
            f.write("export OS_AUTH_URL=http://%s:5000/v3\nexport OS_AUTH_TYPE=password\nexport OS_PROJECT_NAME=admin\n"
                    "export OS_USERNAME=admin\n" % side)
            for k, v in (env or {}).get(side + "_rc", {}).items():
                f.write("export %s=%s\n" % (k, v))
    os.environ.update({"HARNESS_WORLD": wf, "HARNESS_CALLS": calls, "HARNESS_STATE": os.path.join(d, "state"),
                       "HARNESS_ATTACH": os.path.join(d, "attach"), "HARNESS_BLOCKDEV": "/dev/loop0",
                       "HARNESS_TOOLS": os.path.join(H, "stub_tools.py"), "MIG_CONF": "/nonexistent",
                       "SRC_RC": os.path.join(d, "src.rc"), "DST_RC": os.path.join(d, "dst.rc")})
    for k in [v for v in config.CONF_VARS if v not in ("SRC_RC", "DST_RC")]:  # no conf leaks from case to case
        os.environ.pop(k, None)
    os.environ["MIG_ISCSI_IP"] = "127.0.0.1"  # 1.0.0: no default any more; an address of every host (doctor)
    # 1.0.0 (A7): the host lock of this world; a lock handed down by a runner (the UI harness) keeps its file
    os.environ["MIG_LOCK"] = OUTER_LOCK or os.path.join(d, "migrate.lock")
    os.environ.update((env or {}).get("env", {}))
    config.STATE_ROOT, config.ATTACH_DIR, config.LOG_ROOT = (os.path.join(d, "state"), os.path.join(d, "attach"),
                                                           os.path.join(d, "log"))
    os.makedirs(config.STATE_ROOT, exist_ok=True)
    clouds._rc_env_cache.clear()
    del CHILD_ARGV[:]
    outs, errs, rcs = [], [], []
    for step in steps:
        if callable(step):
            step(wf, config.STATE_ROOT)
            continue
        so, se = io.StringIO(), io.StringIO()
        reset_flags()
        with redirect_stdout(so), redirect_stderr(se):
            if step[0] == "rollback":
                rc = cli.guarded(lambda: cli.run_rollback(step[1]))
            elif step[0] == "doctor":
                rc = cli.guarded(doctor.run_doctor)
            elif step[0] == "volume":
                rc = cli.guarded(lambda: cli.run_volume(step[1], list(step[2])))
            elif step[0] == "cli":
                rc = cli.main(list(step[1]))
            else:
                rc = cli.guarded(lambda: cli.run_vm(step[0], step[1], list(step[2])))
        reset_flags()
        outs.append(so.getvalue())
        errs.append(se.getvalue())
        rcs.append(rc)
    with open(calls) as f:
        cl = f.read().splitlines()
    with open(wf) as f:
        fw = json.load(f)
    return NS(rc=rcs, out="".join(outs), err="".join(errs), outs=outs, errs=errs, calls=cl, world=fw,
              state=config.STATE_ROOT, log=config.LOG_ROOT, dir=d, text="".join(outs) + "".join(errs),
              children=list(CHILD_ARGV))


def idx(calls, prefix):
    return next((i for i, c in enumerate(calls) if c.startswith(prefix)), None)


def edit(fn):
    """A step that changes the world between two runs: fn(world)."""
    def step(wf, _root):
        with open(wf) as f:
            w = json.load(f)
        fn(w)
        with open(wf, "w") as f:
            json.dump(w, f)
    return step


def tree(root):
    """{relative path: (mode, size, mtime)} of everything under root (SEC-10 before/after)."""
    snap = {}
    for base, dirs, files in os.walk(root):
        for n in dirs + files:
            p = os.path.join(base, n)
            st = os.lstat(p)
            snap[os.path.relpath(p, root)] = (st.st_mode, st.st_size if n in files else 0, st.st_mtime_ns)
    return snap
