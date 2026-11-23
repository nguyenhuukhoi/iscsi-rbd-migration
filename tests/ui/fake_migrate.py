#!/usr/bin/env python3
"""A fake 'migrate' for the UI tests: the real CLI code against the simulated clouds of tests/unit, plus a little
state of its own so that a migrate, then status and list, then a rollback hang together across processes.

    MIGUI_FAKE_DIR=<dir> python3 -I fake_migrate.py <migrate arguments>

doctor/inventory/list/plan --json: migratetool.cli run in this process by tests/unit/inproc.py on ui_world() (three
VMs on host cmp1 including one named with HTML markup, an available volume). Their output is the CLI's own.
migrate/volume --progress-json: by default the real run (inproc, real '@@' events); <dir>/control.json can pick per
ident another behaviour: "slow" (a scripted run that takes a while and stops cleanly on SIGINT, rc 130), "stubborn"
(ignores SIGINT: only SIGKILL ends it), "fail" (rc 1 after its t_stop), "leaky" (prints credential-looking lines,
then OK: the runner must scrub them), "late" (a slow run that prints its result rc 0, then stays "late_secs" more:
a SIGINT then exits 130, fix-1.0.1 #2). "plan_checks": a list of {level, text} that replaces the checks (and counts)
of every plan row that has checks, the result left as the CLI said (DESIGN-UI 17). "plan_lines": the stderr of a
plan is scripted lines printed one by one with a delay (DESIGN-UI 20, see plan_lines()). status --json: built from
<dir>/state.json (what earlier runs left live) in the schema of migratetool/status.py; the state root is
$MIGUI_FAKE_STATE_ROOT (the harness gives the runner the same one), an entry's "dir" overrides its directory (19.2
tests). rollback: moves the live entry to rolled_back.
Every invocation is appended to <dir>/calls.log (the tests check nothing ran after a refused request).
"""
import json
import os
import signal
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
FAKE = os.environ.get("MIGUI_FAKE_DIR") or "/tmp/migui-fake"
ARGS = sys.argv[1:]
REAL_SLEEP = time.sleep
STATE_ROOT = os.environ.get("MIGUI_FAKE_STATE_ROOT") or "/var/lib/mig-migrate"
XSS_NAME = "<img src=x onerror=alert(1)>"


def jload(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def jsave(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


CONTROL = jload(os.path.join(FAKE, "control.json"), {})


def state():
    return jload(os.path.join(FAKE, "state.json"), {"live": {}, "rolled_back": []})


def out(line):
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def event(ev, **kw):
    d = {"event": ev}
    d.update(kw)
    out("@@ " + json.dumps(d, sort_keys=True, separators=(",", ":")))


VM_IDS = {1: "7c1e4b90-2f1a-4c2e-9d0b-6a3e51f09a01", 2: "7c1e4b90-2f1a-4c2e-9d0b-6a3e51f09a02",
          3: "7c1e4b90-2f1a-4c2e-9d0b-6a3e51f09a03"}
VOL_ID = "c91b2e4d-0000-4000-8000-00000000a001"
VOL_OTHER = "c91b2e4d-0000-4000-8000-00000000b001"


def ui_world(base):
    """The base world of tests/unit with UUID-shaped IDs (the selector looks an ID up only when it is one): app01,
    app02, a VM named with HTML markup, all on cmp1, and the available volume data-archive-01; a second source
    project 'other' (no VM) with its own available volume other-data-01 (DESIGN-UI 21: several projects)."""
    w = base
    src = w["src"]
    app = src["servers"].pop("srv-src-app01")
    src["servers"][VM_IDS[1]] = app
    app["project_id"] = "psrc"
    src["ports"]["port-src-1"]["device_id"] = VM_IDS[1]
    for n, name, ip in ((2, "app02", "198.51.100.51"), (3, XSS_NAME, "198.51.100.52")):
        sid = VM_IDS[n]
        s = json.loads(json.dumps(app))
        s["name"] = name
        s["ports"] = ["port-src-%d" % n]
        s["volumes"] = [{"Volume ID": "vol-src-root%d" % n, "Device": "/dev/vda"}]
        src["servers"][sid] = s
        p = json.loads(json.dumps(src["ports"]["port-src-1"]))
        p["fixed_ips"] = [{"subnet_id": "sub-src-1", "ip_address": ip}]
        p["mac_address"] = "fa:16:3e:aa:bb:%02d" % n
        p["device_id"] = sid
        src["ports"]["port-src-%d" % n] = p
        src["volumes"]["vol-src-root%d" % n] = json.loads(json.dumps(src["volumes"]["vol-src-root"]))
    src["volumes"][VOL_ID] = {"name": "data-archive-01", "status": "available", "size": 5, "bootable": "false",
                                     "encrypted": False, "multiattach": False}
    src.setdefault("projects", {src["project_id"]: {"name": "admin"}})["pother"] = {"name": "other"}
    src["volumes"][VOL_OTHER] = {"name": "other-data-01", "status": "available", "size": 3, "bootable": "false",
                                 "encrypted": False, "multiattach": False, "project_id": "pother"}
    extra = CONTROL.get("world_extra") or {}
    if extra:
        if extra.get("hosts"):
            src["compute_hosts"] = [{"host": "cmp%d" % i, "zone": "nova"} for i in range(1, int(extra["hosts"]) + 1)]
        if extra.get("projects"):
            for i in range(1, int(extra["projects"]) + 1):
                src["projects"]["px%d" % i] = {"name": "project-%02d" % i}
        if extra.get("duplicate_names"):
            for i in (4, 5):
                sid = "7c1e4b90-2f1a-4c2e-9d0b-6a3e51f09a%02d" % i
                server = json.loads(json.dumps(app))
                server.update(name="duplicate-vm", host="cmp2", ports=[], volumes=[])
                src["servers"][sid] = server
                vid = "c91b2e4d-0000-4000-8000-00000000a%03d" % i
                src["volumes"][vid] = {"name": "duplicate-vol", "status": "available", "size": 1,
                                       "bootable": "false", "encrypted": False, "multiattach": False}
        if extra.get("post_fail_pair"):
            for i, name in ((7, "policy-fail-vm"), (8, "policy-after-vm")):
                sid = "7c1e4b90-2f1a-4c2e-9d0b-6a3e51f09a%02d" % i
                server = json.loads(json.dumps(app))
                server.update(name=name, host="cmp2", ports=["port-src-%d" % i],
                              volumes=[{"Volume ID": "vol-src-root%d" % i, "Device": "/dev/vda"}])
                src["servers"][sid] = server
                port = json.loads(json.dumps(src["ports"]["port-src-1"]))
                port.update(device_id=sid, fixed_ips=[{"subnet_id": "sub-src-1",
                                                    "ip_address": "198.51.100.%d" % (50 + i)}])
                src["ports"]["port-src-%d" % i] = port
                src["volumes"]["vol-src-root%d" % i] = json.loads(json.dumps(src["volumes"]["vol-src-root"]))
        if extra.get("server_group"):
            w["dst"]["server_groups"]["sg-dst-web-anti"] = {"name": extra["server_group"],
                                                                  "policy": "anti-affinity", "members": []}
        if extra.get("fallback_source_flavor"):
            name = extra["fallback_source_flavor"]
            sid = "7c1e4b90-2f1a-4c2e-9d0b-6a3e51f09a06"
            server = json.loads(json.dumps(app))
            server.update(name="flavor-fallback-vm", host="cmp2", ports=["port-src-6"],
                          volumes=[{"Volume ID": "vol-src-root6", "Device": "/dev/vda"}])
            server["flavor"].update(original_name=name, name=name)
            src["servers"][sid] = server
            port = json.loads(json.dumps(src["ports"]["port-src-1"]))
            port.update(device_id=sid, fixed_ips=[{"subnet_id": "sub-src-1", "ip_address": "198.51.100.56"}])
            src["ports"]["port-src-6"] = port
            src["volumes"]["vol-src-root6"] = json.loads(json.dumps(src["volumes"]["vol-src-root"]))
            src["flavors"][name] = {"vcpus": 2, "ram": 2048}
    return w


def inproc_app(argv):
    """Run 'migrate <argv>' on the simulated clouds; returns the inproc result."""
    sys.argv = [sys.argv[0], REPO]
    sys.path[:0] = [os.path.join(REPO, "tests", "unit"), REPO]
    import inproc  # noqa: E402  (patches proc.run, time.sleep, the selector child)
    env_values = {}
    if isinstance(CONTROL.get("flavor_map"), dict):
        map_file = os.path.join(FAKE, "flavor-map.txt")
        with open(map_file, "w", encoding="utf-8") as f:
            for source, dest in CONTROL["flavor_map"].items():
                f.write("%s %s\n" % (source, dest))
        env_values["FLAVOR_MAP_FILE"] = map_file
    if isinstance(CONTROL.get("default_opts"), str):
        env_values["MIG_DEFAULT_OPTS"] = CONTROL["default_opts"]
    return inproc.app(ui_world(inproc.world()), [("cli", list(argv))], env={"env": env_values} if env_values else None)


def overlay_state(obj):
    """A live state this fake keeps marks list rows, and a plan of such a VM is a SKIP(state-exists)."""
    live = state()["live"]
    for row in obj.get("rows") or []:
        for k in (row.get("ident"), row.get("id"), row.get("name"), "vol-%s" % row.get("id")):
            if k in live:
                e = live[k]
                row["state"] = {"dir": os.path.join(STATE_ROOT, k), "live": True, "started": e.get("started"),
                                "options": e.get("options")}
                if "verb" in obj:
                    row["result"] = "SKIP(state-exists)"
                    row["command"] = row["effective"] = row["checks"] = row["counts"] = None
                break
    return obj


def plan_checks(obj):
    """control.json "plan_checks": the checks of each plan row that has some, counts recomputed (DESIGN-UI 17)."""
    checks = CONTROL.get("plan_checks")
    if ARGS[0] != "plan" or not isinstance(checks, list):
        return obj
    for row in obj.get("rows") or []:
        if isinstance(row.get("checks"), list):
            row["checks"] = checks
            row["counts"] = {k: sum(1 for c in checks if c.get("level") == k.upper())
                             for k in ("ok", "info", "warn", "fail")}
    return obj


PLAN_HEAD = ("VM {ident} status=ACTIVE host=cmp1", "  port port-src-1 198.51.100.50",
             "Destination preflight (mode=cold ip=same type=__DEFAULT__ pool=volumes):",
             "  OK    Ceph pool volumes: 12 GB needed, 900 GB left", "  WARN  flavor m1.small mapped by name",
             "  FAIL  quota RAM short by 2048 MB")


def plan_lines(ident):
    """control.json "plan_lines": {"n", "delay", "secret"} (DESIGN-UI 20): instead of the CLI's own stderr, n lines,
    delay seconds apart, flushed one by one: PLAN_HEAD, then '  OK    check <k>'; a '@@ {...}' line after the
    first one (not counted in n); "secret": also a line with OS_PASSWORD=ABC-NOT-A-REAL-ONE (the runner scrubs)."""
    c = CONTROL.get("plan_lines") or {}
    n, delay = int(c.get("n", 6)), float(c.get("delay", 0))
    for k in range(n):
        line = PLAN_HEAD[k].format(ident=ident) if k < len(PLAN_HEAD) else "  OK    check %d" % k
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
        if k == 0:
            sys.stderr.write('@@ {"event":"effective","options":{"az":{"value":"AZ1","from":"cli"}}}\n')
            if c.get("secret"):
                sys.stderr.write("debug: OS_PASSWORD=ABC-NOT-A-REAL-ONE" + "\n")
            sys.stderr.flush()
        if delay:
            REAL_SLEEP(delay)


def read_only():
    if ARGS[0] == "plan" and CONTROL.get("plan_delay"):
        REAL_SLEEP(float(CONTROL["plan_delay"]))
    r = inproc_app(ARGS)
    text = r.out
    if r.out.strip() and ARGS[0] in ("list", "plan"):
        try:
            text = json.dumps(plan_checks(overlay_state(json.loads(r.out)))) + "\n"
        except ValueError:
            pass
    if ARGS[0] == "plan" and CONTROL.get("plan_lines"):
        plan_lines(ARGS[1])
    else:
        sys.stderr.write(r.err)
    sys.stdout.write(text)
    return r.rc[-1]


def status():
    st = state()
    live = []
    for ident, e in sorted(st["live"].items()):
        live.append({"ident": ident, "dir": e.get("dir") or os.path.join(STATE_ROOT, ident), "kind": e.get("kind", "vm"),
                     "files": ["options", "options.json", "volume-map"], "live": True, "mtime": e.get("started"),
                     "options": e.get("options"), "t_stop": e.get("t_stop"), "dst_server_id": e.get("dst"),
                     "volume_map": []})
    obj = {"tool_version": "7.3.0", "state_root": STATE_ROOT, "log_root": "/var/log/mig-batch", "live": live,
           "plan_only": [], "rolled_back": st["rolled_back"][-50:], "rolled_back_total": len(st["rolled_back"]),
           "batches": [], "batches_total": 0, "running": CONTROL.get("running", []), "skipped": 0}
    out(json.dumps(obj, sort_keys=True))
    return 0


def remember(ident, kind, options, t_stop=None, dst=None):
    st = state()
    st["live"][ident] = {"kind": kind, "options": options, "t_stop": t_stop, "dst": dst,
                         "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    jsave(os.path.join(FAKE, "state.json"), st)


def scripted(ident, mode, kind):
    """slow / stubborn / fail / leaky runs: lines and '@@' events over a few seconds."""
    if mode == "stubborn":
        signal.signal(signal.SIGINT, lambda *_: out("SIGINT ignored (stubborn fake)"))
    out("migrate: logs in /var/log/mig-batch/20261120-120000; project map: none (0 entries, other projects keep "
        "their name)")
    out("=== %s %s (%s) admin -> admin" % (kind, ident, ident))
    if mode == "leaky" and os.environ.get("FAKE_LEAK_LINE"):  # 1.0.0 (B1): a value of an openrc
        out(os.environ["FAKE_LEAK_LINE"])
    if mode == "leaky":
        out("debug: OS_PASSWORD=HUNTER-TWO-NOT-REAL OS_APPLICATION_CREDENTIAL_SECRET=s3cr3t-value")
        out("REQ: curl -g -i -X GET http://keystone:5000/v3 -H \"X-Auth-Token: gAAAAABfakefakefakefake\"")
        out("[client.admin] key = " + "AQB" + "fake" * 9 + "==")  # built at run time: no key-shaped literal in the source
    event("phase", name="presync")
    steps = int(CONTROL.get("steps", 30))
    t_stop = None
    try:
        for i in range(steps):
            event("copy", volume="vol-src-root", pct=int(100 * (i + 1) / steps), mbps=180)
            if i == steps // 2:
                t_stop = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                out("stop source VM")
                event("t_stop", at=t_stop)
                if mode == "fail":
                    out("ERROR: destination volume create failed (fake)")
                    event("result", rc=1, dst_server_id=None)
                    remember(ident if kind == "vm" else "vol-" + ident, kind, None, t_stop)
                    return 1
            out("copy %d/%d" % (i + 1, steps))
            REAL_SLEEP(float(CONTROL.get("step_secs", 0.2)))
    except KeyboardInterrupt:
        out("interrupted: detaching the clone, the state stays for rollback")
        event("result", rc=130, dst_server_id=None)
        if t_stop:
            remember(ident if kind == "vm" else "vol-" + ident, kind, None, t_stop)
        return 130
    event("downtime", total=12, copy=5, verify=0, control_plane=7)
    event("phase", name="done")
    event("result", rc=0, dst_server_id="srv-dst-fake")
    remember(ident if kind == "vm" else "vol-" + ident, kind, None, t_stop, "srv-dst-fake")
    out("Migrated.")
    if mode == "late":
        # fix-1.0.1 (Review 23/11 #2): the VM is migrated (result rc 0 printed), the selector still finishes its
        # Summary and lock for "late_secs"; a SIGINT that lands there makes it exit 130 as the real one does.
        signal.signal(signal.SIGINT, signal.default_int_handler)  # the runner may have been started with SIGINT ignored
        try:
            REAL_SLEEP(float(CONTROL.get("late_secs", 3)))
        except KeyboardInterrupt:
            out("interrupted after the result: exit 130")
            return 130
    return 0


def run(kind):
    ident = ARGS[1]
    mode = (CONTROL.get("modes") or {}).get(ident) or CONTROL.get("mode") or "real"
    if mode != "real":
        return scripted(ident, mode, kind)
    r = inproc_app(ARGS)
    for line in r.out.splitlines():
        out(line)
        REAL_SLEEP(0.005)
    sys.stderr.write(r.err)
    rc = r.rc[-1]
    if rc == 0:
        d = ident if kind == "vm" else "vol-" + ident
        opts = jload(os.path.join(r.state, d, "options.json"), None)
        tst = None
        try:
            with open(os.path.join(r.state, d, "t-stop")) as f:
                tst = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(f.read().strip())))
        except (OSError, ValueError):
            pass
        remember(d, kind, opts, tst, (opts or {}).get("dst_server_id"))
        if opts is not None and isinstance(CONTROL.get("effective_extra"), dict):  # T9 test: a value the runner must scrub
            opts.setdefault("effective", {}).update(CONTROL["effective_extra"])
        if opts is not None:  # like the real CLI: <STATE_ROOT>/<d>/options.json, read by the runner at the end of a job
            os.makedirs(os.path.join(STATE_ROOT, d), mode=0o700, exist_ok=True)
            jsave(os.path.join(STATE_ROOT, d, "options.json"), opts)
    return rc


def rollback():
    target = ARGS[1]
    st = state()
    if target not in st["live"]:
        out("ERROR: no state directory %s/%s" % (STATE_ROOT, target))
        return 1
    if "--dry-run" in ARGS:  # 1.0.0 (A5): the summary migui-runner shows with a rollback; nothing changes
        e = st["live"][target]
        # 1.1.0 (A20): control.json "source_status": {ident: "GONE" | null | status} (default ACTIVE)
        src = CONTROL.get("source_status") or {}
        out(json.dumps({"ident": target, "kind": e.get("kind", "vm"), "t_stop": e.get("t_stop"), "age_seconds": 60,
                        "dst_server_id": e.get("dst"), "dst_server_status": "ACTIVE" if e.get("dst") else None,
                        "source_power": "ACTIVE", "source_status": src[target] if target in src else "ACTIVE",
                        "delete": {"server": e.get("dst"), "ports": [], "volumes": [], "images": []}}, sort_keys=True))
        return 0
    e = st["live"].pop(target)
    retired = "%s/%s.rolled-back.%d" % (STATE_ROOT, target, time.time())
    base, n = retired, 0
    while os.path.exists(retired):  # two rollbacks in one second: .1, .2 ... as state.retire does
        n += 1
        retired = "%s.%d" % (base, n)
    if os.path.isdir(os.path.join(STATE_ROOT, target)):  # the state directory the run mirrored, retired as the CLI does
        os.rename(os.path.join(STATE_ROOT, target), retired)
    st["rolled_back"].append({"ident": target, "dir": retired,
                              "retired": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "options": e.get("options"),
                              "rollback": None})
    jsave(os.path.join(FAKE, "state.json"), st)
    out("rollback %s: destination objects deleted, source VM started" % target)
    return 0


def main():
    os.makedirs(FAKE, exist_ok=True)
    with open(os.path.join(FAKE, "calls.log"), "a") as f:
        f.write(json.dumps(ARGS) + "\n")
    with open(os.path.join(FAKE, "env.log"), "a") as f:  # 1.0.0 (A7): what the runner handed this migrate
        f.write(json.dumps({"args": ARGS, "MIG_LOCK": os.environ.get("MIG_LOCK"),
                            "MIG_LOCK_FD": os.environ.get("MIG_LOCK_FD")}) + "\n")
    if not ARGS:
        return 2
    cmd = ARGS[0]
    if cmd == "status":
        return status()
    if cmd in ("doctor", "inventory", "list", "plan"):
        return read_only()
    if cmd == "migrate":
        return run("vm")
    if cmd == "volume":
        return run("vol")
    if cmd == "rollback":
        return rollback()
    out("ERROR: fake migrate: unknown command %s" % cmd)
    return 1


if __name__ == "__main__":
    rc = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(rc)  # the simulated Glance server thread of tests/unit/parity.py must not keep us alive
