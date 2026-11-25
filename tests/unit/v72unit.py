#!/usr/bin/env python3
"""7.2 unit checks (DESIGN-V7-2 section 12): one or more cases per audit code, each named after it.

The app runs in this process against the fake clouds of stub_openstack.py / stub_tools.py: proc.run is
replaced by a dispatcher that executes the stub scripts in-process (no interpreter start per call), so a full
migrate + rollback takes a fraction of a second. Linux only (the brick stub hands out /dev/loop0); stdlib only.

    python3 v72unit.py <directory holding migratetool/>
"""
import io
import json
import os
import re
import shutil
import ssl
import sys
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace as NS

H = os.path.dirname(os.path.abspath(__file__))
PKG_PARENT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(H)))
sys.path.insert(0, PKG_PARENT)
sys.path.insert(1, H)
import migratetool  # noqa: E402
from migratetool import (cli, clouds, config, copyvol, doctor, livesync, preflight, proc, rollback,  # noqa: E402
                         secgroups, selector, state)
from migratetool.options import Options  # noqa: E402
from migratetool.proc import Result  # noqa: E402
from migratetool.util import Die  # noqa: E402
import parity  # noqa: E402  (base_world, and the fake Glance server it starts)

PASS = FAIL = 0
WORK = tempfile.mkdtemp(prefix="v72unit-")
time.sleep = lambda s: None  # every wait and retry loop of the app


def ok(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS " + label)
    else:
        FAIL += 1
        print("FAIL %s %s" % (label, str(detail)[-3000:]))


def dies(label, fn, want):
    try:
        fn()
        ok(label, False, "no error")
    except Die as e:
        ok(label, want in str(e), str(e))


# --- the stubs, in-process ----------------------------------------------------------------------------
ORIG_RUN = proc.run
CODE = {}
MISSING = set()  # programs a case pretends are not installed


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
        if cmd[len(copyvol.BRICK):] in (["--check"], ["--check-auth"]):  # 1.0.0 (C7): --check-auth too
            res = (0, "ok\n", "")
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


proc.run = fake_run


# --- one app run on a fake world ------------------------------------------------------------------------
def world():
    return parity.base_world()


def app(w, steps, env=None):
    """Runs the steps (('plan'|'migrate', vm, [options]), ('rollback', target), ('volume', vol, [options]),
    ('doctor',), or a callable(world_path, state_root) between steps) on world w."""
    d = tempfile.mkdtemp(dir=WORK)
    wf, calls = os.path.join(d, "world.json"), os.path.join(d, "calls")
    w = json.loads(json.dumps(w))
    w.setdefault("glance_url", parity.GLANCE_URL)  # a case may set its own ("": no image endpoint)
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
                       "SRC_RC": os.path.join(d, "src.rc"), "DST_RC": os.path.join(d, "dst.rc"),
                       "MIG_LOCK": os.path.join(d, "migrate.lock"),  # 1.0.0 (A7): never the host's /run/mig
                       "MIG_ISCSI_IP": "127.0.0.1"})  # 1.0.0: no default; an address of every host (doctor)
    for k in ("MIG_VOLUME_AZ", "MIG_AZ", "MIG_DEFAULT_OPTS", "SRC_PROJECT", "DST_PROJECT"):
        os.environ.pop(k, None)
    os.environ.update((env or {}).get("env", {}))
    config.STATE_ROOT, config.ATTACH_DIR, config.LOG_ROOT = (os.path.join(d, "state"), os.path.join(d, "attach"),
                                                           os.path.join(d, "log"))
    clouds._rc_env_cache.clear()
    outs, errs, rcs = [], [], []
    for step in steps:
        if callable(step):
            step(wf, config.STATE_ROOT)
            continue
        so, se = io.StringIO(), io.StringIO()
        with redirect_stdout(so), redirect_stderr(se):
            if step[0] == "rollback":
                rc = cli.guarded(lambda: cli.run_rollback(step[1]))
            elif step[0] == "doctor":
                rc = cli.guarded(doctor.run_doctor)
            elif step[0] == "volume":
                rc = cli.guarded(lambda: cli.run_volume(step[1], list(step[2])))
            else:
                rc = cli.guarded(lambda: cli.run_vm(step[0], step[1], list(step[2])))
        outs.append(so.getvalue())
        errs.append(se.getvalue())
        rcs.append(rc)
    with open(calls) as f:
        cl = f.read().splitlines()
    with open(wf) as f:
        fw = json.load(f)
    return NS(rc=rcs, out="".join(outs), err="".join(errs), calls=cl, world=fw, state=config.STATE_ROOT, dir=d,
              text="".join(outs) + "".join(errs))


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


PLAIN = ["--no-copy-secgroup", "--no-server-group"]  # the 7.0 behaviour: no copy needs an AZ name
ROLL = ("rollback", "app01")

# --- A1: destination volumes before the source stops ------------------------------------------------------
r = app(world(), [("migrate", "app01", PLAIN)])
creates = [i for i, c in enumerate(r.calls) if c.startswith("dst\tvolume create")]
stop = idx(r.calls, "src\tserver stop")
ok("A1 cold: every 'volume create' at the destination comes before 'server stop'",
   r.rc == [0] and len(creates) == 2 and stop is not None and max(creates) < stop, (r.rc, r.calls, r.text))
ok("A1 cold: the volume size is read from volumes.json, no 'volume show -c size' in the window",
   not [c for c in r.calls if c.endswith("-f value -c size")], r.calls)
ok("A1 cold: 'destination volumes created' logged before 'stop source VM'",
   0 <= r.out.find("destination volumes created; stopping the source now") < r.out.find("stop source VM"), r.out)
w = world()
w["dst"]["create_status"] = "error"
r = app(w, [("migrate", "app01", PLAIN), ROLL])
ok("A1 cold: a destination volume that fails stops the run with the source still running and attached",
   r.rc[0] == 3 and idx(r.calls, "src\tserver stop") is None and idx(r.calls, "src\tserver remove port") is None
   and "entered status error" in r.err, (r.rc, r.err))
ok("A1 cold: rollback after such a failure removes the volume and leaves the source ACTIVE",
   r.rc[1] == 0 and not r.world["dst"]["volumes"] and r.world["src"]["servers"]["srv-src-app01"]["status"] == "ACTIVE",
   (r.rc, r.world["dst"]["volumes"], r.text[-800:]))
r = app(world(), [("migrate", "app01", PLAIN + ["--mode", "live"])])
ok("A1 live: unchanged, the presync creates the volumes before the stop",
   r.rc == [0] and max(i for i, c in enumerate(r.calls) if c.startswith("dst\tvolume create")) < idx(r.calls, "src\tserver stop")
   and "destination volumes created; stopping" not in r.out, (r.rc, r.text[-600:]))

# --- A2: quota of both clouds, capacity ---------------------------------------------------------------
r = app(world(), [("plan", "app01", PLAIN)])
ok("A2 OK line: destination quota lists need/free of what the run creates",
   "  OK    destination quota: instances 1/10 free, cores 2/20, ram 2048/51200 MB, volumes 2/10, gigabytes 12/1000, "
       "ports 1/50" in r.out and r.rc == [0], r.out)
ok("A2 OK line: source quota of the temporary clone (largest volume)",
   "  OK    source quota: volumes 1/10 free, gigabytes 10/1000" in r.out, r.out)
ok("A2 Ceph pool capacity line", "  OK    Ceph pool mig-volumes: 12 GB needed, 1024 GB left (MAX AVAIL)" in r.out, r.out)
w = world()
w["dst"]["quota"] = {"instances": [10, 0, 0], "volumes": [10, 0, 0], "gigabytes": [100, 95, 0]}
r = app(w, [("plan", "app01", PLAIN)])
ok("A2 FAIL when the destination gigabytes are short",
   "  FAIL  destination quota: gigabytes need 12, 5 free (used 95 of 100)" in r.out and r.rc == [1]
   and "  OK    destination quota" not in r.out, r.out)
w = world()
w["dst"]["quota"] = {"instances": [10, 0, 0], "volumes": [10, 0, 0], "gigabytes": [-1, 5000, 0]}
r = app(w, [("plan", "app01", PLAIN)])
ok("A2 Limit -1 is skipped", "  OK    destination quota: instances 1/10 free, volumes 2/10" in r.out
   and "gigabytes" not in r.out.split("destination quota:")[1].split("\n")[0], r.out)
w = world()
w["dst"]["quota_fail"] = "Unknown command ['quota', 'show', '--usage']"
r = app(w, [("plan", "app01", PLAIN)])
ok("A2 WARN when the destination cannot answer, the plan still passes",
   "  WARN  destination quota not checked: Unknown command ['quota', 'show', '--usage']" in r.out and r.rc == [0], r.out)
w = world()
w["src"]["quota"] = {"volumes": [10, 10, 0], "gigabytes": [1000, 0, 0]}
r = app(w, [("plan", "app01", PLAIN)])
ok("A2 source volumes short (the clone) -> FAIL",
   "  FAIL  source quota: volumes need 1, 0 free (used 10 of 10)" in r.out and r.rc == [1], r.out)
w = world()
w["src"]["quota"] = {"volumes": [10, 0, 0], "gigabytes": [100, 95, 0]}
r = app(w, [("plan", "app01", PLAIN)])
ok("A2 source gigabytes short by the largest volume (10 GB, not the 12 GB total)",
   "  FAIL  source quota: gigabytes need 10, 5 free (used 95 of 100)" in r.out and r.rc == [1], r.out)
w = world()
w["src"]["volumes"]["vol-src-root"]["type"] = "iscsi-gold"
w["src"]["quota"] = {"volumes": [10, 0, 0], "volumes_iscsi-gold": [5, 5, 0], "gigabytes_iscsi-gold": [100, 0, 0]}
r = app(w, [("plan", "app01", PLAIN)])
ok("A2 source per-type quota of the clone's type", "  FAIL  source quota: volumes_iscsi-gold need 1, 0 free" in r.out, r.out)
w = world()
w["dst"]["quota"] = {"volumes": [10, 0, 0], "volumes_nvme": [1, 0, 0], "gigabytes_nvme": [100, 0, 0]}
r = app(w, [("plan", "app01", PLAIN + ["--volume-type", "nvme"])])
ok("A2 destination per-type quota short", "  FAIL  destination quota: volumes_nvme need 2, 1 free (used 0 of 1)" in r.out
   and r.rc == [1], r.out)
w = world()
w["src"]["server_groups"]["sg-src-anti"] = {"name": "web-anti", "policy": "anti-affinity", "rules": {},
                                            "members": ["srv-src-app01"]}
w["dst"]["server_groups"]["sg-dst-full"] = {"name": "AZ1-web-anti", "policy": "anti-affinity", "rules": {},
                                            "members": ["m%d" % i for i in range(10)]}
r = app(w, [("plan", "app01", ["--no-copy-secgroup", "--az", "AZ1"])])
ok("A2 server group full -> FAIL server_group_members",
   "  FAIL  destination quota: server_group_members need 1, 0 free (used 10 of 10)" in r.out and r.rc == [1], r.out)
w["dst"]["server_groups"]["sg-dst-full"]["members"] = ["m1"]
r = app(w, [("plan", "app01", ["--no-copy-secgroup", "--az", "AZ1"])])
ok("A2 server group with room -> listed in the OK line", "server_group_members 1/9" in r.out and r.rc == [0], r.out)
w = world()
w["dst"]["subnets"]["sub-dst-1"]["allocation_pools"] = [{"start": "198.51.100.10", "end": "198.51.100.10"}]
w["dst"]["ports"]["port-dst-taken"] = {"name": "x", "network_id": "net-dst-1", "mac_address": "fa:16:3e:00:00:99",
                                       "fixed_ips": [{"subnet_id": "sub-dst-1", "ip_address": "198.51.100.10"}],
                                       "security_group_ids": [], "device_id": "", "port_security_enabled": True,
                                       "allowed_address_pairs": []}
r = app(w, [("plan", "app01", PLAIN + ["--ip", "new"])])
ok("A2 subnet exhausted with --ip new -> FAIL",
   "  FAIL  subnet 198.51.100.0/24: no free address in its allocation pools for --ip new" in r.out and r.rc == [1], r.out)
r = app(world(), [("plan", "app01", PLAIN + ["--ip", "new"])])
ok("A2 --ip new with room: OK line", "  OK    free addresses for --ip new: 198.51.100.0/24 1/253" in r.out, r.out)
w = world()
w["dst"]["ceph_fail"] = "Error EACCES: access denied"
r = app(w, [("plan", "app01", PLAIN)])
ok("A2 ceph df without caps -> INFO, not a FAIL",
   "  INFO  Ceph pool capacity not checked: Error EACCES: access denied" in r.out and r.rc == [0], r.out)
w = world()
w["dst"]["ceph_pools"] = {"mig-volumes": {"max_avail": 5 * 1073741824, "stored": 0, "quota_bytes": 0}}
r = app(w, [("plan", "app01", PLAIN)])
ok("A2 Ceph pool too small -> FAIL", "  FAIL  Ceph pool mig-volumes: 12 GB needed, 5 GB left (MAX AVAIL)" in r.out
   and r.rc == [1], r.out)
w["dst"]["ceph_pools"] = {"mig-volumes": {"max_avail": 1 << 50, "stored": 100 * 1073741824, "quota_bytes": 105 * 1073741824}}
r = app(w, [("plan", "app01", PLAIN)])
ok("A2 Ceph pool quota smaller than MAX AVAIL -> FAIL on the quota",
   "  FAIL  Ceph pool mig-volumes: 12 GB needed, 5 GB left (pool quota)" in r.out, r.out)
w["dst"]["ceph_pools"] = {"mig-volumes": {"max_avail": 13 * 1073741824, "stored": 100 * 1073741824, "quota_bytes": 0}}
r = app(w, [("plan", "app01", PLAIN)])
ok("A2 Ceph pool under 10% left after the copy -> WARN", "  WARN  Ceph pool mig-volumes: 12 GB needed, 13 GB left" in r.out
   and r.rc == [0], r.out)
w = world()
w["src"]["volumes"]["vol-src-data"]["status"] = "available"
r = app(w, [("volume", "data", [])])
ok("A2 migrate volume: source clone + destination volumes/gigabytes",
   "  OK    source quota: volumes 1/10 free, gigabytes 2/1000" in r.out
   and "  OK    destination quota: volumes 1/10 free, gigabytes 2/1000" in r.out, r.text)

# --- A3: floating IPs --------------------------------------------------------------------------------------
w = world()
w["src"]["floating_ips"] = {"fip-1": {"floating_ip_address": "203.0.113.10", "port_id": "port-src-1"}}
r = app(w, [("plan", "app01", PLAIN)])
ok("A3 plan: fip line under the port and a WARN", "  fip   203.0.113.10 on fa:16:3e:aa:bb:01" in r.out
   and "  WARN  port fa:16:3e:aa:bb:01: floating IP 203.0.113.10 not migrated (a floating IP does not cross clouds): "
       "allocate one at the destination and update DNS" in r.out and r.rc == [0], r.out)
with open(os.path.join(r.state, "app01", "ports.json")) as f:
    ok("A3 export: floating_ips recorded on the port", json.load(f)[0].get("floating_ips") == ["203.0.113.10"])
r = app(world(), [("plan", "app01", PLAIN)])
with open(os.path.join(r.state, "app01", "ports.json")) as f:
    ok("A3 export: no key when the port has none (bash-written shape kept)", "floating_ips" not in json.load(f)[0])
w["src"]["ports"]["port-src-1"]["nova_owned"] = True
r = app(w, [("migrate", "app01", PLAIN), ROLL])
fip = r.world["src"]["floating_ips"]["fip-1"]
reserve = [k for k, p in r.world["src"]["ports"].items() if p.get("name", "").startswith("mig-reserve-app01")]
ok("A3 rollback re-associates a floating IP that Nova's port deletion dropped",
   r.rc == [0, 0] and reserve and fip["port_id"] == reserve[0]
   and "re-associated floating IP 203.0.113.10 with port %s" % reserve[0] in r.out, (r.rc, fip, reserve, r.out[-900:]))
w2 = json.loads(json.dumps(w))
w2["src"]["ports"]["port-src-1"].pop("nova_owned")
r = app(w2, [("migrate", "app01", PLAIN), ROLL])
ok("A3 rollback leaves a still-associated floating IP alone",
   r.rc == [0, 0] and "re-associated" not in r.out and not [c for c in r.calls if "floating ip set" in c], r.out[-600:])

# --- A4: one name in two projects -------------------------------------------------------------------------
w = world()
w["src"]["projects"] = {"psrc": {"name": "admin"}, "pother": {"name": "other"}}
TWIN = "aaaaaaaa-0000-4000-8000-0000000000f1"
w["src"]["servers"][TWIN] = dict(w["src"]["servers"]["srv-src-app01"], project_id="pother", ports=[],
                                           volumes=[])
app(w, [])  # sets up the environment and the world file
os.makedirs(config.LOG_ROOT, exist_ok=True)
run = selector.Runner(selector.finish(selector.split_args("plan", ["app01"])), config.load())
os.makedirs(run.log_dir, exist_ok=True)
with redirect_stdout(io.StringIO()):
    run.enumerate_vms()
ok("A4 a name matching VMs of two projects: one SKIP(ambiguous-name) row each",
   [(x.id, x.res) for x in run.rows] == [("srv-src-app01", "SKIP(ambiguous-name: use the ID)"),
                                         (TWIN, "SKIP(ambiguous-name: use the ID)")],
   [(x.id, x.res) for x in run.rows])
run = selector.Runner(selector.finish(selector.split_args("plan", [TWIN])), config.load())
os.makedirs(run.log_dir, exist_ok=True)
with redirect_stdout(io.StringIO()):
    run.enumerate_vms()
ok("A4 the ID still selects exactly that VM", [(x.id, x.res) for x in run.rows] == [(TWIN, "selected")],
   [(x.id, x.res) for x in run.rows])

# --- B1: gone only on 'not found'; rollback reattaches the union ------------------------------------------
w = world()
w["src"]["port_show_fail"] = {"port-src-1": [2, "Service Unavailable (HTTP 503)"]}
r = app(w, [("migrate", "app01", PLAIN)])
ok("B1 a 503 on 'port show' after the detach is retried, no reserve port",
   r.rc == [0] and not [c for c in r.calls if "port create mig-reserve" in c] and "Nova removed" not in r.out,
   (r.rc, r.text[-600:]))
w = world()
w["src"]["ports"]["port-src-1"]["nova_owned"] = True
r = app(w, [("migrate", "app01", PLAIN), ROLL])
ok("B1 'No Port found' -> Nova deleted it: reserve port, rollback logs the gone port and reattaches the reserve",
   r.rc == [0, 0] and "Nova removed its own port; reserved" in r.out
   and "port port-src-1 no longer exists (Nova deleted it; its reserve port is reattached instead)" in r.out
   and "reattached source port port-src-0" in r.out, (r.rc, r.out[-900:]))
w = world()
w["src"]["port_show_fail"] = {"port-src-1": [999, "Service Unavailable (HTTP 503)"]}
r = app(w, [("migrate", "app01", PLAIN),
            edit(lambda x: x["src"].pop("port_show_fail")), ROLL])
ok("B1 a port that never answers: the run stops ('cannot read port ... after detach')",
   r.rc[0] == 4 and "cannot read port port-src-1 after detach: Service Unavailable (HTTP 503)" in r.err, r.err)  # C8: after t-stop
ok("B1 rollback reattaches the detached port missing from source-ports",
   r.rc[1] == 0 and "reattached source port port-src-1" in r.out
   and r.world["src"]["ports"]["port-src-1"]["device_id"] == "srv-src-app01", (r.rc, r.out[-600:]))

# --- B2: ephemeral production path -------------------------------------------------------------------------
from migratetool import ephemeral  # noqa: E402

ctx = ephemeral.tls_context({"OS_INSECURE": "True"})
ok("B2 OS_INSECURE=true: no verification", ctx.verify_mode == ssl.CERT_NONE and not ctx.check_hostname)
ok("B2 no TLS variable: Python's defaults (None)", ephemeral.tls_context({}) is None)
bundle = next((p for p in ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt") if os.path.exists(p)), None)
if bundle:
    ctx = ephemeral.tls_context({"OS_CACERT": bundle})
    ok("B2 OS_CACERT: a verifying context", ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname)
dies("B2 OS_CACERT that cannot be read: a clear stop", lambda: ephemeral.tls_context({"OS_CACERT": "/nonexistent.pem"}),
     "cannot load OS_CACERT /nonexistent.pem")


def eph_world(disk=1):
    w = world()
    w["src"]["servers"]["srv-src-eph01"] = {
        "name": "eph01", "status": "ACTIVE", "locked": False, "locked_reason": None, "image": "cirros (img-base)",
        "flavor": {"original_name": "m1.small", "name": "m1.small", "vcpus": 2, "ram": 2048, "disk": disk},
        "key_name": "k1", "properties": {}, "description": None, "ports": ["port-src-eph"], "volumes": []}
    w["src"]["ports"]["port-src-eph"] = {"name": "", "network_id": "net-src-1", "mac_address": "fa:16:3e:aa:bb:60",
                                         "fixed_ips": [{"subnet_id": "sub-src-1", "ip_address": "198.51.100.60"}],
                                         "security_group_ids": ["sg-src-default"], "device_id": "srv-src-eph01",
                                         "port_security_enabled": True, "allowed_address_pairs": []}
    w["src"]["snap_props"] = {"hw_disk_bus": "scsi", "os_distro": "ubuntu", "os_hash_value": "abc"}
    return w


r = app(eph_world(), [("migrate", "eph01", PLAIN)])
el = idx(r.calls, "dst\tendpoint list")
toks = [c for c in r.calls[el:] if c == "dst\ttoken issue -f value -c id"] if el is not None else []
glance = [c.split()[1] for c in r.calls if c.startswith("glance\t")]
ok("B2 endpoint: public by default (no OS_INTERFACE)",
   r.rc == [0] and r.calls[el].startswith("dst\tendpoint list --service image --interface public"), (r.rc, r.text[-600:]))
ok("B2 a fresh token right before each of POST, PUT, PATCH", len(toks) == 3 and glance == ["POST", "PUT", "PATCH"]
   and all(r.calls[r.calls.index(c) - 1] == "dst\ttoken issue -f value -c id"
           for c in r.calls if c.startswith("glance\t")), (toks, glance))
r = app(eph_world(), [("migrate", "eph01", PLAIN)], env={"dst_rc": {"OS_INTERFACE": "internal"}})
ok("B2 endpoint: OS_INTERFACE of the destination openrc",
   r.rc == [0] and any(c.startswith("dst\tendpoint list --service image --interface internal") for c in r.calls), r.text[-400:])
r = app(eph_world(), [("plan", "eph01", PLAIN)])
ok("B2 free space: OK line with the flavor's disk",
   "  OK    free space in %s: " % r.state in r.out and "GB for a 1 GB root disk" in r.out, r.out)
r = app(eph_world(disk=10 ** 9), [("plan", "eph01", PLAIN)])
ok("B2 free space short -> FAIL", "  FAIL  free space in %s: " % r.state in r.out
   and "the root disk snapshot needs up to 1000000000 GB" in r.out and r.rc == [1], r.out)
r = app(eph_world(disk=0), [("plan", "eph01", PLAIN)])
ok("B2 free space unknown (disk 0) -> WARN", "  WARN  free space not checked: the root disk size of flavor m1.small" in r.out,
   r.out)
r = app(eph_world(), [("plan", "eph01", PLAIN)])
ok("B2 preflight: the image endpoint of OS_INTERFACE (public) is found while the source runs",
   "  OK    image endpoint %s (public)" % parity.GLANCE_URL in r.out and r.rc == [0], r.out)
r = app(eph_world(), [("plan", "eph01", PLAIN)], env={"dst_rc": {"OS_CACERT": "/nonexistent-ca.pem"}})
ok("B2 preflight: an OS_CACERT that cannot be loaded -> FAIL before the source stops",
   "  FAIL  OS_CACERT /nonexistent-ca.pem cannot be loaded: " in r.out and r.rc == [1], r.out)
w = eph_world()
w["glance_url"] = ""
r = app(w, [("migrate", "eph01", PLAIN)])
ok("B2 preflight: no image endpoint for that interface -> FAIL, the source is never stopped",
   "  FAIL  the destination catalog lists no public image endpoint: set OS_INTERFACE in " in r.out and r.rc == [1]
   and idx(r.calls, "src\tserver stop") is None, (r.rc, r.out))

# --- B3: detect_boot cross-check ---------------------------------------------------------------------------
w = world()
w["src"]["servers"]["srv-src-app01"]["image"] = "cirros (img-1)"
r = app(w, [("plan", "app01", PLAIN)])
ok("B3 a root volume on vda but an image named -> stop, no guess",
   r.rc == [1] and "cannot tell the root disk of app01: attachments /dev/vda,/dev/vdb, image cirros (img-1)" in r.err, r.err)
w = eph_world()
w["src"]["servers"]["srv-src-eph01"]["image"] = "N/A (booted from volume)"
r = app(w, [("plan", "eph01", PLAIN)])
ok("B3 no root attachment but 'booted from volume' -> stop", r.rc == [1] and "cannot tell the root disk of eph01" in r.err,
   r.err)
w = world()
w["src"]["servers"]["srv-src-app01"]["image"] = "N/A (booted from volume)"
r = app(w, [("plan", "app01", PLAIN)])
ok("B3 agreement (vda + 'booted from volume') plans as before", r.rc == [0], r.text[-300:])

# --- B4: --ip <addr> ---------------------------------------------------------------------------------------


def opts(args, defaults=""):
    o = Options()
    o.load_default_opts("migrate", defaults)
    o.parse_options(args)
    return o


dies("B4 --ip 999.1.1.1 is refused", lambda: opts(["--ip", "999.1.1.1"]), "--ip takes same, new or an IPv4 address")
ok("B4 --ip 198.51.100.77 accepted", opts(["--ip", "198.51.100.77"]).ipaddr == "198.51.100.77")
r = app(world(), [("plan", "app01", PLAIN + ["--ip", "10.9.9.9"])])
ok("B4 an address outside the port's subnet -> FAIL",
   "  FAIL  IP 10.9.9.9 is not in subnet 198.51.100.0/24 of vlan120" in r.out and r.rc == [1], r.out)
r = app(world(), [("plan", "app01", PLAIN + ["--ip", "198.51.100.77"])])
ok("B4 an address inside: no subnet FAIL", "not in subnet" not in r.out and r.rc == [0], r.out)

# --- B5: rollback goes on after a failing source listing --------------------------------------------------
r = app(world(), [("migrate", "app01", PLAIN),
                  edit(lambda x: x["src"].update(fail={"image list": "Service Unavailable (HTTP 503)",
                                                       "volume list --name mig-clone": "HTTP 503"})), ROLL])
srv = r.world["src"]["servers"]["srv-src-app01"]
ok("B5 'image list' / clone 'volume list' fail at the source: WARN, the rollback still unlocks and starts it",
   r.rc == [0, 0] and "WARN cannot list source images named mig-snap-app01: skipped" in r.out
   and "WARN cannot list source clones named mig-clone-vol-src-root: skipped" in r.out
   and not srv["locked"] and srv["status"] == "ACTIVE", (r.rc, r.out[-900:]))
r = app(world(), [("migrate", "app01", PLAIN),
                  edit(lambda x: x["src"]["volumes"].update({"vol-src-0099": {
                      "name": "mig-clone-vol-src-root", "status": "error", "size": 10}})), ROLL])
ok("B5 a leftover clone in error is deleted through drop_volume (no wait that dies)",
   r.rc == [0, 0] and "deleted leftover clone vol-src-0099" in r.out and "vol-src-0099" not in r.world["src"]["volumes"],
   (r.rc, r.out[-600:]))

# --- B6: image property allowlist ---------------------------------------------------------------------------
seen = []
fake = NS(dst=NS(out=lambda *a, **k: seen.append(list(a)) or ""))
copyvol.copy_volume_props(fake, {"bootable": "true", "volume_image_metadata": {
    "hw_disk_bus": "virtio", "os_distro": "ubuntu", "os_hash_value": "abc", "os_hash_algo": "sha512", "os_hidden": False,
    "image_name": "u24"}}, "d1")
props = [a[-1] for a in seen if "--image-property" in a]
ok("B6 hw_disk_bus and os_distro copied, os_hash_value / os_hash_algo / os_hidden / image_name not",
   props == ["hw_disk_bus=virtio", "os_distro=ubuntu"] and ["volume", "set", "d1", "--bootable"] in seen, seen)

# --- C1: a name with regex characters ----------------------------------------------------------------------
w = world()
w["src"]["servers"]["srv-src-app01"]["name"] = "web.example"
w["src"]["servers"]["srv-src-other"] = dict(w["src"]["servers"]["srv-src-app01"], name="webXexample", ports=[], volumes=[])
r = app(w, [("plan", "web.example", PLAIN)])
ok("C1 a name with '.' is escaped in the export's and the preflight's --name regex",
   r.rc == [0] and "src\tserver list --name ^web\\.example$ -f value -c ID" in r.calls
   and "dst\tserver list --name ^web\\.example$ -f value -c ID" in r.calls, (r.rc, r.text[-400:]))

# --- C2: pass numbers ----------------------------------------------------------------------------------------
dies("C2 --max-passes 0 is refused", lambda: livesync.check_numbers(opts(["--mode", "live", "--max-passes", "0"])),
     "--max-passes takes a whole number >= 1, not '0'")
dies("C2 --delta-limit x is refused", lambda: livesync.check_numbers(opts(["--mode", "live", "--delta-limit", "x"])),
     "--delta-limit takes a whole number >= 0, not 'x'")
livesync.check_numbers(opts(["--mode", "live", "--max-passes", "1", "--delta-limit", "0"]))
ok("C2 --max-passes 1 --delta-limit 0 accepted", True)


# --- C3: security group cleanup order ---------------------------------------------------------------------
class SGs:
    """Neutron security groups: a group a rule of another group points at cannot be deleted."""

    def __init__(self, groups, fail_on):
        self.groups, self.fail_on, self.calls, self.n = groups, fail_on, [], 0
        self.side, self.project, self._pid = "destination", "", "pdst"

    def project_id(self):
        return self._pid

    def run(self, *a, stderr=None, capture=True):
        a = list(a)
        self.calls.append(" ".join(a))
        if a[:3] == ["security", "group", "list"]:
            return Result(0, json.dumps([{"ID": k, "Name": g["name"]} for k, g in self.groups.items()]), "")
        if a[:4] == ["security", "group", "rule", "list"]:
            return Result(0, "".join(x["id"] + "\n" for x in self.groups[a[4]]["rules"]), "")
        if a[:4] == ["security", "group", "rule", "delete"]:
            for g in self.groups.values():
                g["rules"] = [x for x in g["rules"] if x["id"] not in a[4:]]
            return Result(0, "", "")
        if a[:3] == ["security", "group", "delete"]:
            if any(x.get("remote") == a[3] for k, g in self.groups.items() if k != a[3] for x in g["rules"]):
                return Result(1, "", "in use")
            self.groups.pop(a[3])
            return Result(0, "", "")
        raise AssertionError(a)

    def out(self, *a, stderr=None):
        a = list(a)
        self.calls.append(" ".join(a))
        if a[:3] == ["security", "group", "create"]:
            self.n += 1
            gid = "g%d" % self.n
            self.groups[gid] = {"name": a[3], "rules": []}
            return gid + "\n"
        if a[:4] == ["security", "group", "rule", "list"]:
            return "".join(x["id"] + "\n" for x in self.groups[a[4]]["rules"])
        if a[:4] == ["security", "group", "rule", "delete"]:
            return ""
        if a[:4] == ["security", "group", "rule", "create"]:
            if self.fail_on in a:
                raise Die("rule create failed: quota")
            remote = a[a.index("--remote-group") + 1] if "--remote-group" in a else None
            self.groups[a[4]]["rules"].append({"id": "r%d" % len(self.calls), "remote": remote})
            return "x\n"
        raise AssertionError(a)

    def value(self, *a, stderr=None):
        return self.out(*a).rstrip("\n")


def sg_rule(rid, remote=None, port=None):
    return {"id": rid, "direction": "ingress", "ethertype": "IPv4", "protocol": "tcp" if port else None,
            "port_range_min": port, "port_range_max": port, "remote_ip_prefix": None, "remote_group_id": remote,
            "description": ""}


plan = secgroups.Plan("Z")
ga, gb = secgroups.Group("sa", "a", "Z-a"), secgroups.Group("sb", "b", "Z-b")
ga.create = gb.create = True
ga.rules = [sg_rule("a1", port=1)]
gb.rules = [sg_rule("b1", remote="sa", port=2), sg_rule("b2", port=3)]  # b -> a: Z-a cannot go first; b2 fails
plan.groups = {"sa": ga, "sb": gb}
dst = SGs({}, fail_on="3:3")
try:
    with redirect_stdout(io.StringIO()) as buf:
        secgroups.ensure(NS(dst=dst), plan, NS(write=lambda *a: None))
    ok("C3 a failing rule raises", False)
except Die:
    ok("C3 cleanup: every rule of the copies deleted first, then the groups: none left behind",
       dst.groups == {} and "WARN deleted the security groups this run had just created (Z-a, Z-b)" in buf.getvalue()
       and max(i for i, c in enumerate(dst.calls) if " rule delete " in c)
       < min(i for i, c in enumerate(dst.calls) if c.startswith("security group delete")), (dst.groups, dst.calls))

# --- C4, C5 ----------------------------------------------------------------------------------------------
r = app(world(), [("migrate", "app01", PLAIN)])
ok("C4 brick's stderr goes to the run's own state directory, not a shared STATE_ROOT file",
   os.path.exists(os.path.join(r.state, "app01", "brick-attach.err"))
   and not os.path.exists(os.path.join(r.state, "brick-attach.err")), os.listdir(r.state))
dies("C5 a VM name with '/' cannot name a state directory", lambda: state.StateDir("web/01"),
     "VM name 'web/01' cannot name a state directory: run with the server ID")
dies("C5 '..' neither", lambda: state.StateDir(".."), "cannot name a state directory")

# --- C6: --volume-az / MIG_VOLUME_AZ ------------------------------------------------------------------------
r = app(world(), [("migrate", "app01", PLAIN + ["--az", "AZ1", "--volume-az", "nova"])])
vc = [c for c in r.calls if c.startswith("dst\tvolume create")]
sc = [c for c in r.calls if c.startswith("dst\tserver create")]
ok("C6 --volume-az: volumes in that AZ, the server in --az, header and state say so",
   r.rc == [0] and all("--availability-zone nova" in c for c in vc) and "--availability-zone AZ1" in sc[0]
   and " az=AZ1 volume-az=nova" in r.out and "  OK    volume AZ nova" in r.out and "  OK    compute AZ AZ1" in r.out,
   (r.rc, vc, sc, r.out[:600]))
with open(os.path.join(r.state, "app01", "options")) as f:
    ok("C6 state options end with VOLUME_AZ=", f.read().rstrip("\n").endswith(" AZ=AZ1 VOLUME_AZ=nova"))
r = app(world(), [("migrate", "app01", PLAIN + ["--az", "AZ1"])], env={"env": {"MIG_VOLUME_AZ": "nova"}})
ok("C6 MIG_VOLUME_AZ when --volume-az is not typed",
   r.rc == [0] and all("--availability-zone nova" in c for c in r.calls if c.startswith("dst\tvolume create")), r.text[-300:])
r = app(world(), [("migrate", "app01", PLAIN + ["--az", "AZ1", "--volume-az", "AZ1"])],
        env={"env": {"MIG_VOLUME_AZ": "nova"}})
ok("C6 typed --volume-az wins over MIG_VOLUME_AZ",
   all("--availability-zone AZ1" in c for c in r.calls if c.startswith("dst\tvolume create")) and "volume-az=" not in r.out)
r = app(world(), [("migrate", "app01", PLAIN + ["--az", "AZ1"])])
ok("C6 neither: the volumes follow --az (7.1 behaviour), no volume-az token",
   all("--availability-zone AZ1" in c for c in r.calls if c.startswith("dst\tvolume create")) and "volume-az=" not in r.out)
ok("C6 option lists: --volume-az takes a value and goes to lone volumes",
   "--volume-az" in selector.VALUE_OPTS and "--volume-az" in selector.VOLUME_PASS)

# --- C7: iscsid not started by the preflight ---------------------------------------------------------------
w = world()
w["inactive_units"] = ["iscsid"]
r = app(w, [("plan", "app01", PLAIN)])
ok("C7 iscsid inactive: FAIL + INFO how to start it, never 'systemctl start'",
   "  FAIL  iSCSI daemon on transfer host\n  INFO  start it: systemctl enable --now iscsid" in r.out
   and not [c for c in r.calls if c.startswith("systemctl\tstart")] and r.rc == [1], r.out)


# --- C8: rollback finds a destination server whose ID was never recorded -----------------------------------
def blank_server_id(_wf, root):
    open(os.path.join(root, "app01", "dst-server-id"), "w").close()


r = app(world(), [("migrate", "app01", PLAIN), blank_server_id, ROLL])
ok("C8 created after t-stop, one of that name: deleted by name",
   r.rc == [0, 0] and not r.world["dst"]["servers"]
   and "deleted destination server srv-dst-0001 found by name (its ID was never recorded)" in r.out, (r.rc, r.out[-800:]))
def older_server(x):
    """The destination server predates the run; its volumes are let go so that the rollback can finish."""
    s = x["dst"]["servers"]["srv-dst-0001"]
    s["created"] = "2020-01-01T00:00:00Z"
    for v in s["volumes"]:
        x["dst"]["volumes"][v["Volume ID"]]["status"] = "available"
    s["volumes"] = []


r = app(world(), [("migrate", "app01", PLAIN), blank_server_id, edit(older_server), ROLL])
ok("C8 created before t-stop: left alone", "srv-dst-0001" in r.world["dst"]["servers"] and "found by name" not in r.out,
   r.out[-600:])

# --- D2: doctor -----------------------------------------------------------------------------------------------
r = app(world(), [("doctor",)])
ok("D2 doctor on a ready host: exit 0, tokens and roles of both sides",
   r.rc == [0] and "  OK    source token (project admin)" in r.out and "  OK    destination: admin role" in r.out
   and "Doctor passed." in r.out, r.text)
MISSING.add("qemu-img")
w = world()
w["dst"]["roles"] = ["member"]
r = app(w, [("doctor",)])
MISSING.discard("qemu-img")
ok("D2 doctor: a missing program FAILs (exit 1), no admin role WARNs",
   r.rc == [1] and "  FAIL  qemu-img: not found on PATH" in r.out
   and "  WARN  destination: no admin role: --ip same needs port --mac-address (admin_or_network_owner)" in r.out, r.out)
ok("D2 brick --check prints ok", "def main():\n    if sys.argv[1:] == [\"--check\"]:" in open(
    os.path.join(PKG_PARENT, "migratetool", "brick.py")).read())

# --- D3: downtime breakdown ----------------------------------------------------------------------------------
r = app(world(), [("migrate", "app01", PLAIN)])
lines_ = r.out.splitlines()
i_active = next(i for i, l in enumerate(lines_) if "destination VM ACTIVE" in l)
ok("D3 cold: breakdown line right after 'destination VM ACTIVE'",
   "] downtime breakdown: total " in lines_[i_active + 1] and ", copy " in lines_[i_active + 1]
   and ", control-plane " in lines_[i_active + 1], lines_[i_active:i_active + 2])
r = app(world(), [("migrate", "app01", PLAIN + ["--volumes-only"])])
ok("D3 volumes-only too", "] downtime breakdown: total " in r.out, r.out[-400:])
r = app(eph_world(), [("migrate", "eph01", PLAIN)])
ok("D3 ephemeral (7.3 D3b): the Glance snapshot/download/upload is copy time, named in brackets",
   r.rc == [0] and re.search(r"\] downtime breakdown: total \d+s, copy \d+s \(glance \d+s\), verify \d+s, "
                             r"control-plane \d+s\n", r.out), r.out[-400:])
ok("D3 boot-from-volume: no glance bracket", "(glance" not in app(world(), [("migrate", "app01", PLAIN)]).out)

# --- D4: version ---------------------------------------------------------------------------------------------
ok("D4 version 1.2.3 (v1 bumped it, fix-1.0.1, 1.1.0, 1.1.1, 1.2.0, 1.2.1, 1.2.2 and 1.2.3 again)", migratetool.__version__ == "1.2.3", migratetool.__version__)

shutil.rmtree(WORK, ignore_errors=True)
print("TOTAL pass=%d fail=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
