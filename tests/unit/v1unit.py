#!/usr/bin/env python3
"""1.0.0 unit checks (REVIEW-20261121 section 1, phase 1): every finding fixed in the engine has cases named after its
ID (A1 ..., B1 ..., C1 ...). Runs the app in this process against the fake clouds (inproc.py); A1 runs the real
blocksync.py against a fake device file and fake librbd modules; A17/A19/C7 import the real brick.py against fake
os-brick / cinderclient / keystoneauth modules.

    python3 v1unit.py <directory holding migratetool/>
"""
import fcntl
import importlib
import io
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # run with -I: no implicit script directory
import inproc  # noqa: E402  (puts the package on sys.path, patches proc.run and the selector child)
from inproc import app, edit, idx, world  # noqa: E402
import migratetool  # noqa: E402
from migratetool import (cli, clouds, config, contract, doctor, ephemeral, jsonout, locking, proc, prune,  # noqa: E402
                         rollback, selector, state, volcmd)
from migratetool.util import Die  # noqa: E402

PASS = FAIL = 0
PLAIN = ["--no-copy-secgroup", "--no-server-group"]
ROLL = ("rollback", "app01")
H = os.path.dirname(os.path.abspath(__file__))
REPO = inproc.PKG_PARENT
WORK = tempfile.mkdtemp(prefix="v1unit-")


def ok(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS " + label)
    else:
        FAIL += 1
        print("FAIL %s %s" % (label, str(detail)[-3000:]))


def jload(path):
    with open(path) as f:
        return json.load(f)


def set_file(path, text):
    """Replace a state file (options.json is written 0600 O_EXCL by the app: remove first)."""
    if os.path.exists(path):
        os.remove(path)
    with open(path, "w") as f:
        f.write(text)


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
    w["src"]["snap_props"] = {"hw_disk_bus": "scsi", "os_distro": "ubuntu"}
    return w


# ================================ E3: the contracts live in contract.py ================================
from migui import runner as mrunner  # noqa: E402
ok("E3 contract: preflight prefixes, check regex, grep strings, exit codes",
   contract.OK == "  OK    " and contract.FAIL == "  FAIL  " and contract.WARN == "  WARN  " and contract.INFO == "  INFO  "
   and contract.CHECK_RE.match("  FAIL  x").groups() == ("FAIL", "x") and contract.PREFLIGHT_PASSED == "Preflight passed."
   and contract.EPHEMERAL_NO_VOLUME == "ephemeral VM has no volume to migrate"
   and contract.LOGS_RE.search("[t] logs in /var/log/mig-batch/x; project").group(1) == "/var/log/mig-batch/x"
   and (contract.EXIT_OK, contract.EXIT_FAIL, contract.EXIT_USAGE, contract.EXIT_BEFORE_CUTOVER,
        contract.EXIT_AFTER_CUTOVER, contract.EXIT_BATCH_FAIL, contract.EXIT_INTERRUPTED) == (0, 1, 2, 3, 4, 5, 130))
ok("E3 migui keeps the same LEVELS and LOGS_RE as contract.py (it never imports migratetool)",
   mrunner.LEVELS == contract.LEVELS and mrunner.LOGS_RE.pattern == contract.LOGS_RE.pattern)
src = open(os.path.join(REPO, "migratetool", "selector.py")).read()
ok("E3 the selector reads its grep strings from contract.py",
   "contract.EPHEMERAL_NO_VOLUME" in src and "contract.LOGS_IN" in src and "contract.KEYPAIR_WARN_RE" in src
   and "ephemeral VM has no volume to migrate" not in src)

# ================================ A1: blocksync reads each chunk whole ================================
FAKE_LIBRBD = '''
import os
DIR = os.environ["FAKE_RBD_DIR"]


class Rados:
    def __init__(self, conffile=None, rados_id=None):
        pass

    def connect(self):
        pass

    def open_ioctx(self, pool):
        return Ioctx()

    def shutdown(self):
        pass


class Ioctx:
    def close(self):
        pass


if os.environ.get("FAKE_SHORT_READS"):  # os.pread returns at most half of what is asked, every other call
    _pread, _n = os.pread, [0]

    def _short(fd, n, off):
        _n[0] += 1
        return _pread(fd, max(1, n // 2) if _n[0] % 2 else n, off)
    os.pread = _short
'''
FAKE_RBD = '''
import os
DIR = os.environ["FAKE_RBD_DIR"]


class Image:
    def __init__(self, ioctx, name):
        self.path = os.path.join(DIR, name)
        if not os.path.exists(self.path):
            open(self.path, "wb").write(b"\\0" * int(os.environ["FAKE_IMAGE_SIZE"]))
        dev = os.environ.get("FAKE_TRUNCATE")
        if dev:  # the device goes away after blocksync measured it: every read past this point is short (EOF)
            os.truncate(dev, int(os.environ["FAKE_TRUNCATE_TO"]))

    def size(self):
        return os.path.getsize(self.path)

    def write(self, data, off):
        with open(self.path, "r+b") as f:
            f.seek(off)
            f.write(data)

    def read(self, off, n):
        with open(self.path, "rb") as f:
            f.seek(off)
            return f.read(n)

    def discard(self, off, n):
        self.write(b"\\0" * n, off)

    def flush(self):
        pass

    def close(self):
        pass
'''
fake = os.path.join(WORK, "fakelib")
os.makedirs(fake)
open(os.path.join(fake, "rados.py"), "w").write(FAKE_LIBRBD)
open(os.path.join(fake, "rbd.py"), "w").write(FAKE_RBD)
BS = os.path.join(REPO, "migratetool", "blocksync.py")
SIZE = 3 * 4194304 + 12345  # three whole chunks and a partial tail


def blocksync(tag, env_extra=None, first=True):
    d = os.path.join(WORK, "bs-" + tag)
    os.makedirs(d)
    dev = os.path.join(d, "dev.img")
    data = os.urandom(SIZE)
    open(dev, "wb").write(data)
    env = dict(os.environ, PYTHONPATH=fake, FAKE_RBD_DIR=d, FAKE_IMAGE_SIZE=str(SIZE))
    env.update(env_extra(dev) if env_extra else {})
    cmd = [sys.executable, BS, "--device", dev, "--pool", "p", "--image", "volume-x", "--id", "u",
           "--state", os.path.join(d, "hash.bin")] + (["--first"] if first else [])
    p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
    img = open(os.path.join(d, "volume-x"), "rb").read() if os.path.exists(os.path.join(d, "volume-x")) else b""
    return p, data, img, os.path.exists(os.path.join(d, "hash.bin"))


p, data, img, hashed = blocksync("plain")
ok("A1 real blocksync, fake device: a first pass copies every byte, writes its hash state, exit 0",
   p.returncode == 0 and img == data and hashed and json.loads(p.stdout)["chunks"] == 4, (p.returncode, p.stderr[-300:]))
p, data, img, hashed = blocksync("short", lambda dev: {"FAKE_SHORT_READS": "1"})
ok("A1 os.pread returning half of each chunk: read_exact keeps reading, the copy is whole (exit 0)",
   p.returncode == 0 and img == data and hashed, (p.returncode, p.stderr[-300:]))
p, data, img, hashed = blocksync("eof", lambda dev: {"FAKE_TRUNCATE": dev, "FAKE_TRUNCATE_TO": str(4194304 + 100)})
ok("A1 the device shrinks after it was measured (EOF mid-chunk): exit 4, 'short read' named, no hash state written",
   p.returncode == 4 and "short read at offset" in p.stderr and not hashed, (p.returncode, p.stderr[-300:], hashed))
w = world()
w["sync"] = {"vol-src-root": [{"exit": 4}]}
r = app(w, [("migrate", "app01", PLAIN + ["--mode", "live"])])
ok("A1 livesync: a pass that exits 4 fails the run naming the short read (before the cutover: exit 3)",
   r.rc == [3] and "a short read from the source clone (exit 4)" in r.err, (r.rc, r.err))

# ================================ A2: owner marks ================================
r = app(world(), [("migrate", "app01", PLAIN)])
create = [c for c in r.calls if c.startswith("dst\tserver create")]
port = [c for c in r.calls if c.startswith("dst\tport create")]
oj = jload(os.path.join(r.state, "app01", "options.json"))
ok("A2 server create carries --property mig.source_id=<source> --property mig.state=<state>",
   create and create[0].endswith("--property role=web --description web server --property mig.source_id=srv-src-app01 "
                                 "--property mig.state=app01 -f value -c id"), create)
ok("A2 a destination port without a source description is described 'mig:<state>'; options.json says marks are used",
   port and "--description mig:app01" in port[0] and oj.get("owner_mark") == {
       "server": {"mig.source_id": "srv-src-app01", "mig.state": "app01"}, "port_description": "mig:app01"}, (port, oj))


def blank_server_id(_wf, root):
    open(os.path.join(root, "app01", "dst-server-id"), "w").close()


def stranger(x):
    """An unrelated destination server named app01, created now (after t-stop), without the owner mark."""
    x["dst"]["servers"]["srv-other"] = {"name": "app01", "status": "ACTIVE", "properties": {"owner": "someone"},
                                        "ports": [], "volumes": [], "flavor": {"original_name": "m1.small"}}


def drop_ours(x):
    """The run's own server vanished (deleted by hand); only the stranger is left."""
    s = x["dst"]["servers"].pop("srv-dst-0001")
    for v in s["volumes"]:
        x["dst"]["volumes"][v["Volume ID"]]["status"] = "available"
    for p_ in s["ports"]:
        x["dst"]["ports"][p_]["device_id"] = ""


r = app(world(), [("migrate", "app01", PLAIN), blank_server_id, edit(drop_ours), edit(stranger), ROLL])
ok("A2 an unrelated same-name server created after t-stop (no owner mark): never deleted; the rollback goes on",
   r.rc == [0, 0] and "srv-other" in r.world["dst"]["servers"]
   and "carries no owner mark of this run: not created by it, left alone" in r.out
   and r.world["src"]["servers"]["srv-src-app01"]["status"] == "ACTIVE", (r.rc, r.out[-600:]))
r = app(world(), [("migrate", "app01", PLAIN), blank_server_id, edit(stranger), ROLL])
ok("A2 with our marked server and the stranger: ours deleted by its mark, the stranger kept",
   r.rc == [0, 0] and "srv-dst-0001" not in r.world["dst"]["servers"] and "srv-other" in r.world["dst"]["servers"]
   and "found by name (its ID was never recorded)" in r.out, (r.rc, r.out[-600:]))


def legacy(_wf, root):
    """State written before 1.0.0: options.json without owner_mark."""
    p_ = os.path.join(root, "app01", "options.json")
    doc = jload(p_)
    doc.pop("owner_mark", None)
    set_file(p_, json.dumps(doc))


r = app(world(), [("migrate", "app01", PLAIN), blank_server_id, edit(drop_ours), edit(stranger), legacy, ROLL])
ok("A2 legacy state (no marks) and an unmarked same-name server after t-stop: the rollback stops, names it, deletes "
   "nothing, leaves the source as it was",
   r.rc == [0, 1] and "no owner mark to prove it is the copy" in r.err and "openstack server delete srv-other" in r.err
   and "srv-other" in r.world["dst"]["servers"] and r.world["src"]["servers"]["srv-src-app01"]["status"] == "SHUTOFF"
   and os.path.isdir(os.path.join(r.state, "app01")), (r.rc, r.err[-500:]))
r = app(world(), [("migrate", "app01", PLAIN), blank_server_id, edit(drop_ours), edit(stranger), legacy,
                  edit(lambda x: x["dst"]["servers"]["srv-other"].update(created="2020-01-01T00:00:00Z")), ROLL])
ok("A2 legacy state, a same-name server older than t-stop: not a candidate at all, the rollback finishes",
   r.rc == [0, 0] and "srv-other" in r.world["dst"]["servers"], (r.rc, r.err[-300:]))

# ================================ A3: source ports at rollback ================================
w = world()
r = app(w, [("migrate", "app01", PLAIN),
            edit(lambda x: x["src"].update(port_show_fail={"port-src-1": [999, "Service Unavailable (HTTP 503)"]})),
            ROLL])
ok("A3 'port show' answering 503 at rollback: retried, then the rollback stops (the port is not taken as gone, the "
   "source is not started)",
   r.rc == [0, 1] and "cannot read source port port-src-1 (Service Unavailable (HTTP 503))" in r.err
   and r.world["src"]["servers"]["srv-src-app01"]["status"] == "SHUTOFF", (r.rc, r.err[-400:]))
r = app(world(), [("migrate", "app01", PLAIN),
                  edit(lambda x: x["src"]["ports"]["port-src-1"].update(device_id="srv-somebody-else")), ROLL])
ok("A3 a source port attached to another server: the rollback stops and says how to give it back",
   r.rc == [0, 1] and "is attached to srv-somebody-else, not to the source VM" in r.err, (r.rc, r.err[-300:]))

# ================================ A4 / A15: deletes confirmed before the source is touched ======================
saved_wait = rollback.GONE_WAIT
rollback.GONE_WAIT = 0
r = app(world(), [("migrate", "app01", PLAIN), edit(lambda x: x["dst"].update(server_delete_noop=True)), ROLL])
ok("A4 the destination server is still there after 'server delete': the rollback stops before the source "
   "(no unlock, no port, no start)",
   r.rc == [0, 1] and "still there" in r.err and "cleanup not complete" in r.err
   and idx(r.calls[r.calls.index("dst\tserver delete srv-dst-0001 --wait"):], "src\tserver unlock") is None
   and r.world["src"]["servers"]["srv-src-app01"]["status"] == "SHUTOFF", (r.rc, r.err[-300:]))
r = app(world(), [("migrate", "app01", PLAIN), edit(lambda x: x["dst"].update(fail={"server delete": "boom (HTTP 503)"})),
                  ROLL])
ok("A4 'server delete' failing: its exit code is checked, the rollback stops, the source untouched",
   r.rc == [0, 1] and "delete failed (boom (HTTP 503))" in r.err
   and r.world["src"]["servers"]["srv-src-app01"]["status"] == "SHUTOFF", (r.rc, r.err[-300:]))
r = app(world(), [("migrate", "app01", PLAIN), edit(lambda x: x["dst"].update(volume_delete_status="error_deleting")),
                  ROLL])
ok("A15 a destination volume left error_deleting: the state is not retired, 'run the rollback again'",
   r.rc == [0, 1] and "is error_deleting after its delete" in r.err and os.path.isdir(os.path.join(r.state, "app01")),
   (r.rc, r.err[-300:]))
rollback.GONE_WAIT = saved_wait
r = app(world(), [("migrate", "app01", PLAIN), ROLL])
gone = [c for c in r.calls if c.endswith("-f json -c id -c status")]
ok("A4/A15 a normal rollback polls each deleted server and volume until 'not found'",
   r.rc == [0, 0] and "dst\tserver show srv-dst-0001 -f json -c id -c status" in gone
   and "dst\tvolume show vol-dst-0001 -f json -c id -c status" in gone, gone)

# ================================ A5: rollback asks ================================
r = app(world(), [("migrate", "app01", PLAIN), ("cli", ["rollback", "app01"])])
ok("A5 'migrate rollback' without --yes, stdin not a terminal: summary on stderr, exit 2, nothing deleted",
   r.rc == [0, 2] and "re-run with --yes" in r.err and "Rollback of app01" in r.err
   and "srv-dst-0001" in r.world["dst"]["servers"], (r.rc, r.err[-400:]))
r = app(world(), [("migrate", "app01", PLAIN), ("cli", ["rollback", "app01", "--dry-run", "--json"])])
obj = json.loads(r.outs[1]) if r.outs[1].strip() else {}
ok("A5 --dry-run --json: t_stop, age, the destination server and its status, what is deleted; nothing changes",
   r.rc == [0, 0] and obj.get("dst_server_status") == "ACTIVE" and obj.get("delete", {}).get("server") == "srv-dst-0001"
   and obj["delete"]["volumes"] == ["vol-dst-0001", "vol-dst-0002"] and obj["delete"]["ports"] == ["port-dst-0001"]
   and obj.get("t_stop", "").endswith("Z") and isinstance(obj.get("age_seconds"), int)
   and obj.get("source_power") == "ACTIVE" and "srv-dst-0001" in r.world["dst"]["servers"]
   and not [c for c in r.calls[r.calls.index("dst\tserver create " + [c for c in r.calls if c.startswith(
       "dst\tserver create")][0][len("dst\tserver create "):]) + 1:] if " delete " in c], (r.rc, obj))


class TTY(io.StringIO):
    def isatty(self):
        return True


def typed(answer):
    def step(_wf, _root):
        sys.stdin = TTY(answer + "\n")
    return step


saved_stdin = sys.stdin
r = app(world(), [("migrate", "app01", PLAIN), typed("app02"), ("cli", ["rollback", "app01"])])
ok("A5 a terminal, the wrong name typed: 'not confirmed', nothing deleted",
   r.rc == [0, 1] and "not confirmed, nothing changed" in r.err and "Type app01 to roll back" in r.err
   and "srv-dst-0001" in r.world["dst"]["servers"], (r.rc, r.err[-300:]))
r = app(world(), [("migrate", "app01", PLAIN), typed("app01"), ("cli", ["rollback", "app01"])])
ok("A5 a terminal, the name typed: rolled back", r.rc == [0, 0] and not r.world["dst"]["servers"], (r.rc, r.err[-300:]))
sys.stdin = saved_stdin
r = app(world(), [("migrate", "app01", PLAIN), ("cli", ["rollback", "app01", "--yes"])])
ok("A5 --yes: no prompt, rolled back", r.rc == [0, 0] and not r.world["dst"]["servers"], (r.rc, r.err[-300:]))

# ================================ A6: a failed run before the cutover cleans itself ======================
w = world()
w["dst"]["create_status"] = "error"
r = app(w, [("migrate", "app01", PLAIN), edit(lambda x: x["dst"].pop("create_status")), ("migrate", "app01", PLAIN)])
retired = [d for d in os.listdir(r.state) if d.startswith("app01.rolled-back.")]
ok("A6 failed before the cutover (exit 3), re-run: the recorded destination volume is deleted, the old state retired, "
   "the run goes on and migrates (exit 0)",
   r.rc == [3, 0] and "failed before the cutover" in r.out and "deleted destination volume vol-dst-0001" in r.out
   and len(retired) == 1 and "vol-dst-0001" not in r.world["dst"]["volumes"]
   and r.world["dst"]["servers"] and idx(r.calls, "src\tserver stop") is not None, (r.rc, r.out[-800:]))
r = app(world(), [("migrate", "app01", PLAIN), ("migrate", "app01", PLAIN)])
ok("A6 a state past its cutover: exit 4, 'state exists ... run rollback first (that run stopped the source at <UTC>)'",
   r.rc == [0, 4] and re.search(r"state exists in \S+: run rollback first \(that run stopped the source at "
                                r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\)", r.err), (r.rc, r.err))


def pre_cutover_state(_wf, root):
    d = os.path.join(root, "app01")
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "volume-map"), "w").close()


r = app(world(), [pre_cutover_state, ("cli", ["migrate", "app01", "--yes"] + PLAIN)])
ok("A6 selector: a VM whose state never reached the cutover is selected (no SKIP), its child cleans and migrates",
   r.rc == [0] and "SKIP(state-exists)" not in r.out and "OK" in r.out.split("Summary:")[-1], (r.rc, r.out[-600:]))

# ================================ A7: locks ================================
r = app(world(), [])  # a world: its MIG_LOCK path
lockpath = os.environ["MIG_LOCK"]
fd = os.open(lockpath, os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)


def same_lock(_wf, _root):
    os.environ["MIG_LOCK"] = lockpath


r = app(world(), [same_lock, ("migrate", "app01", PLAIN), ("cli", ["plan", "app01"] + PLAIN)])
ok("A7 another process holds MIG_LOCK: migrate dies before it touches anything; plan does not lock",
   r.rc == [1, 0] and "another migrate, volume or rollback runs on this host" in r.err
   and idx(r.calls, "src\tserver stop") is None and idx(r.calls, "dst\tvolume create") is None, (r.rc, r.err))
os.close(fd)


def hold_state(_wf, root):
    hold_state.fd = os.open(os.path.join(root, "app01"), os.O_RDONLY)
    fcntl.flock(hold_state.fd, fcntl.LOCK_EX)


r = app(world(), [("migrate", "app01", PLAIN), hold_state, ROLL])
os.close(hold_state.fd)
ok("A7 the state directory is locked by another process: the rollback dies ('in use'), nothing deleted",
   r.rc == [0, 1] and "is in use by another migrate or rollback" in r.err and r.world["dst"]["servers"], (r.rc, r.err))
os.environ["MIG_LOCK_FD"] = "999"
r = app(world(), [("migrate", "app01", PLAIN)])
del os.environ["MIG_LOCK_FD"]
ok("A7 an inherited MIG_LOCK_FD that is not the MIG_LOCK file: refused", r.rc == [1] and "is not the lock file" in r.err,
   r.err)
src = open(os.path.join(REPO, "migratetool", "selector.py")).read()
ok("A7 the selector hands its lock to each child (pass_fds, MIG_LOCK_FD) and locks migrate/volume runs only",
   "pass_fds=fds" in src and "locking.child_env(env)" in src and 'if verb in ("migrate", "volume") and not plan_only' in src)

# ================================ A8, A10, A11, A14: preflight ================================
r = app(world(), [("plan", "app01", PLAIN)], env={"env": {"MIG_COLD_WARN_GB": "5"}})
ok("A8 cold copy above MIG_COLD_WARN_GB: WARN advising --mode live (plan still passes)",
   r.rc == [0] and "  WARN  cold copy of 12 GB (more than MIG_COLD_WARN_GB=5)" in r.out, r.out[-500:])
r = app(world(), [("plan", "app01", PLAIN + ["--mode", "live"])], env={"env": {"MIG_COLD_WARN_GB": "5"}})
ok("A8 live: no such warning", r.rc == [0] and "cold copy of" not in r.out)
r = app(world(), [("plan", "app01", PLAIN + ["--ip", "198.51.100.50", "--start-source"])])
ok("A10 --ip <the source's own address> --start-source: FAIL",
   r.rc == [1] and "  FAIL  --ip 198.51.100.50 is the source VM's own address" in r.out, r.out[-400:])
w = world()
w["src"]["ports"]["port-src-1"]["allowed_address_pairs"] = [{"ip_address": "198.51.100.99", "mac_address": "fa:16:3e:aa:bb:01"}]
r = app(w, [("plan", "app01", PLAIN + ["--ip", "198.51.100.99", "--start-source"])])
ok("A10 --ip <an allowed-address-pair of the source> --start-source: FAIL; the pair also WARNs",
   r.rc == [1] and "is an allowed-address-pair of source port" in r.out and "  WARN  port fa:16:3e:aa:bb:01 has "
   "allowed-address-pairs 198.51.100.99" in r.out, r.out[-500:])
w = world()
w["src"]["servers"]["srv-src-app01"]["flavor"].update(ephemeral=10, swap=0)
r = app(w, [("plan", "app01", PLAIN)])
ok("A11 a flavor with an ephemeral disk: FAIL (its data would not travel)",
   r.rc == [1] and "  FAIL  flavor m1.small has ephemeral disk 10 GB" in r.out, r.out[-300:])
w = world()
w["src"]["flavors"]["m1.small"]["swap"] = 512
r = app(w, [("plan", "app01", PLAIN)])
ok("A11 swap from 'flavor show' (no embedded value): FAIL",
   r.rc == [1] and "has swap 512 MB" in r.out
   and "src\tflavor show m1.small -f json -c swap -c OS-FLV-EXT-DATA:ephemeral" in r.calls, r.out[-300:])
w = world()
w["src"]["servers"]["srv-src-app01"].update(locked=True, locked_reason="incident 4711")
r = app(w, [("plan", "app01", PLAIN)])
ok("A14 a source locked with an operator's reason: FAIL in the plan", r.rc == [1] and
   "  FAIL  source VM is locked with reason 'incident 4711'" in r.out, r.out[-300:])
w = world()
w["src"]["servers"]["srv-src-app01"]["status"] = "SHUTOFF"
r = app(w, [("migrate", "app01", PLAIN), ROLL])
ok("A14 a source that was SHUTOFF before the migration stays SHUTOFF after the rollback (no 'server start')",
   r.rc == [0, 0] and r.world["src"]["servers"]["srv-src-app01"]["status"] == "SHUTOFF"
   and not [c for c in r.calls if c.startswith("src\tserver start")] and "kept SHUTOFF" in r.out, (r.rc, r.out[-300:]))

# ================================ A9: an ephemeral root disk becomes a volume ======================
r = app(eph_world(disk=3), [("migrate", "eph01", PLAIN), ("rollback", "eph01")])
vc = [c for c in r.calls if c.startswith("dst\tvolume create eph01-vda")]
sc = [c for c in r.calls if c.startswith("dst\tserver create")]
st_dir = [d for d in os.listdir(r.state) if d.startswith("eph01.rolled-back.")]
vmap = open(os.path.join(r.state, st_dir[0], "volume-map")).read() if st_dir else ""
ok("A9 ephemeral root: a destination volume from the uploaded image (flavor disk size), boot --volume, the image "
   "deleted, volume-map 'ephemeral-root <id>'",
   r.rc == [0, 0] and vc and " --image img-dst-0001 --size 3 " in vc[0] and sc and " --volume vol-dst-0001 " in sc[0]
   and "--image" not in sc[0] and "dst\timage delete img-dst-0001" in r.calls
   and vmap == "ephemeral-root vol-dst-0001\n", (r.rc, vc, sc, vmap, r.text[-600:]))
ok("A9 the rollback deletes the root volume too", "deleted destination volume vol-dst-0001" in r.out
   and not r.world["dst"]["volumes"], r.out[-400:])
r = app(eph_world(), [("migrate", "eph01", PLAIN + ["--ephemeral-as-image"])])
sc = [c for c in r.calls if c.startswith("dst\tserver create")]
ok("A9 --ephemeral-as-image: the 7.3 path (boot --image, no volume)",
   r.rc == [0] and sc and " --image img-dst-0001 " in sc[0] and not [c for c in r.calls if "volume create eph01-vda" in c],
   (r.rc, sc))
w = eph_world(disk=0)
w["root_virtual_size"] = 5 * 1073741824 + 1
r = app(w, [("migrate", "eph01", PLAIN)])
vc = [c for c in r.calls if c.startswith("dst\tvolume create eph01-vda")]
ok("A9 a flavor with disk 0: the volume takes the image's virtual size (qemu-img info), rounded up",
   r.rc == [0] and vc and " --size 6 " in vc[0], (r.rc, vc))

# ================================ A12: the source must not change before its stop ======================
from migratetool import vmcmd  # noqa: E402
orig_create = vmcmd.create_dest_volume


def create_and_attach(r_, vid, st):
    orig_create(r_, vid, st)
    if vid == "vol-src-data":  # an operator hot-attaches a volume while migrate prepares
        wf = os.environ["HARNESS_WORLD"]
        x = jload(wf)
        x["src"]["volumes"]["vol-src-new"] = dict(x["src"]["volumes"]["vol-src-data"], name="new")
        x["src"]["servers"]["srv-src-app01"]["volumes"].append({"Volume ID": "vol-src-new", "Device": "/dev/vdc"})
        json.dump(x, open(wf, "w"))


vmcmd.create_dest_volume = create_and_attach
r = app(world(), [("migrate", "app01", PLAIN)])
vmcmd.create_dest_volume = orig_create
ok("A12 a volume attached after the export: the run stops before the source stops (exit 3, no 'server stop')",
   r.rc == [3] and "changed since migrate exported it" in r.err and "vol-src-new" in r.err
   and idx(r.calls, "src\tserver stop") is None and not os.path.exists(os.path.join(r.state, "app01", "t-stop")),
   (r.rc, r.err))

# ================================ A13: security group copies compared ======================
w = world()
w["src"]["security_groups"]["sg-src-web"]["rules"] = [
    {"id": "r1", "direction": "ingress", "ethertype": "IPv4", "protocol": "tcp", "port_range_min": 443,
     "port_range_max": 443, "remote_ip_prefix": "0.0.0.0/0", "remote_group_id": None, "description": ""}]
w["dst"]["azs"]["compute"] = ["nova"]
w["dst"]["security_groups"]["sg-dst-nweb"] = {"name": "nova-web", "project_id": "pdst", "description": "", "rules": []}
r = app(w, [("plan", "app01", ["--no-server-group"])])
ok("A13 a copy found at the destination without the source's rule: WARN naming the missing rule (no FAIL)",
   "  OK    security group web -> found nova-web (sg-dst-nweb)" in r.out
   and "  WARN  security group web -> found nova-web: missing ingress IPv4 tcp 443:443 0.0.0.0/0" in r.out, r.out[-800:])
w["dst"]["security_groups"]["sg-dst-nweb"]["description"] = "mig-copy of sg-src-web"
r = app(w, [("plan", "app01", ["--no-server-group"])])
ok("A13 a copy migrate made itself and left without rules (a killed run): 'HALF-MADE COPY'",
   "HALF-MADE COPY" in r.out and "delete nova-web at the destination" in r.out, r.out[-600:])
del w["dst"]["security_groups"]["sg-dst-nweb"]
r = app(w, [("migrate", "app01", ["--no-server-group"])])
sgc = [c for c in r.calls if c.startswith("dst\tsecurity group create nova-web")]
ok("A13 a copy migrate creates is described 'mig-copy of <source id>'", r.rc == [0] and sgc
   and "--description mig-copy of sg-src-web" in sgc[0], (r.rc, sgc))

# ================================ A17 / A19 / C7: brick with fake os-brick ======================
CALLS = []


def fake_modules():
    def mod(name, **attrs):
        m = types.ModuleType(name)
        m.__dict__.update(attrs)
        sys.modules[name] = m
        return m

    class Conn:
        def connect_volume(self, conn):
            CALLS.append("connect")
            return {"path": "/dev/null", "scsi_wwn": "36001405abcdef"}

        def disconnect_volume(self, conn, device, force=False, ignore_errors=False):
            CALLS.append(("disconnect", force))

    class NotFound(Exception):
        code = 404

    class Atts:
        def create(self, vid, props, mode):
            CALLS.append(("create", mode))
            return {"id": "att-1", "connection_info": {"driver_volume_type": "iscsi"}}

        def complete(self, aid):
            CALLS.append("complete")
            if FAIL_COMPLETE[0]:
                raise RuntimeError("complete failed")

        def delete(self, aid):
            CALLS.append("delete")
            if DELETE_404[0]:
                raise NotFound()

    class Client:
        def __init__(self, ver, session=None, endpoint_type=None, region_name=None):
            CALLS.append(("client", endpoint_type, region_name, session.verify))
            self.attachments = Atts()
            self.volumes = types.SimpleNamespace(get=lambda v: types.SimpleNamespace(size=0))

    class Loader:
        def load_from_options(self, **kw):
            return kw

    class Session:
        def __init__(self, auth=None, verify=True):
            self.verify = verify

        def get_project_id(self):
            return "p-1"

    mod("os_brick", setup=lambda c: None)
    mod("os_brick.initiator")
    mod("os_brick.initiator.connector", InitiatorConnector=types.SimpleNamespace(factory=lambda *a, **k: Conn()),
        get_connector_properties=lambda *a, **k: {})
    sys.modules["os_brick.initiator"].connector = sys.modules["os_brick.initiator.connector"]
    mod("os_brick.privileged")
    mod("os_brick.privileged.default", set_client_mode=lambda x: None)
    sys.modules["os_brick.privileged"].default = sys.modules["os_brick.privileged.default"]
    mod("cinderclient")
    mod("cinderclient.client", Client=Client)
    sys.modules["cinderclient"].client = sys.modules["cinderclient.client"]
    mod("keystoneauth1")
    mod("keystoneauth1.loading", get_plugin_loader=lambda n: Loader())
    mod("keystoneauth1.session", Session=Session)
    sys.modules["keystoneauth1"].loading = sys.modules["keystoneauth1.loading"]
    sys.modules["keystoneauth1"].session = sys.modules["keystoneauth1.session"]
    mod("oslo_concurrency")
    mod("oslo_concurrency.lockutils", set_defaults=lambda d: None)
    sys.modules["oslo_concurrency"].lockutils = sys.modules["oslo_concurrency.lockutils"]
    mod("oslo_config")
    mod("oslo_config.cfg", CONF=None)
    sys.modules["oslo_config"].cfg = sys.modules["oslo_config.cfg"]


FAIL_COMPLETE, DELETE_404 = [False], [False]
fake_modules()
from migratetool import brick  # noqa: E402
brick.STATE_DIR = os.path.join(WORK, "attach")
os.makedirs(brick.STATE_DIR)
saved_env = dict(os.environ)
os.environ.update(OS_AUTH_URL="http://k", OS_USERNAME="u", OS_PASSWORD="p", OS_PROJECT_NAME="x",
                  OS_AUTH_TYPE="password", OS_INTERFACE="internal", OS_REGION_NAME="RegionTwo", OS_CACERT="/etc/ca.pem")
FAIL_COMPLETE[0] = True
try:
    brick.attach("vol-1")
    raised = False
except RuntimeError:
    raised = True
ok("A19 attach: complete() fails -> the connected device is disconnected (forced) before the attachment is deleted",
   raised and CALLS[-2:] == [("disconnect", True), "delete"] and not os.path.exists(brick.state_path("vol-1")), CALLS)
ok("C7 brick's Cinder client: OS_INTERFACE, OS_REGION_NAME and OS_CACERT of the openrc",
   ("client", "internal", "RegionTwo", "/etc/ca.pem") in CALLS, CALLS)
os.environ["OS_INSECURE"] = "true"
del CALLS[:]
FAIL_COMPLETE[0] = False
real_subprocess_run = brick.subprocess.run
brick.subprocess.run = lambda *a, **k: None  # blockdev --setro
with redirect_stdout(io.StringIO()):
    brick.attach("vol-2")
brick.subprocess.run = real_subprocess_run
ok("C7 OS_INSECURE: verify=False", ("client", "internal", "RegionTwo", False) in CALLS, CALLS)
rec = jload(brick.state_path("vol-2"))
DELETE_404[0] = True
with redirect_stderr(io.StringIO()):
    brick.detach("vol-2")
kept = jload(brick.detached_path("vol-2")) if os.path.exists(brick.detached_path("vol-2")) else {}
ok("A19 detach: attachment already gone (404) is fine after the disconnect; A17: the record is kept as "
   "<id>.detached.json with its WWN",
   not os.path.exists(brick.state_path("vol-2")) and kept.get("scsi_wwn") == "36001405abcdef" and rec == kept, kept)
buf = io.StringIO()
with redirect_stdout(buf):
    brick.check_auth()
ok("C7 brick --check-auth prints 'ok <project id>'", buf.getvalue() == "ok p-1\n", buf.getvalue())
os.environ.clear()
os.environ.update(saved_env)
from migratetool import copyvol  # noqa: E402
config.ATTACH_DIR = brick.STATE_DIR
open(os.path.join(brick.STATE_DIR, "c-1.json"), "w").write(json.dumps({"scsi_wwn": "36001405beef"}))
ok("A17 the WWN of an attached clone is read back for the log", copyvol.clone_wwn("c-1") == "36001405beef")

# ================================ A18: state writes and reads ================================
d = os.path.join(WORK, "a18")
config.STATE_ROOT = d
st = state.StateDir("vm1")
st.create()
synced = []
real_fsync = os.fsync
os.fsync = lambda fd: synced.append(fd)
st.write("dst-server-id", "x\n")
st.append("volume-map", "a b")
os.fsync = real_fsync
ok("A18 every write and append is fsynced (file and, for a new file, its directory)", len(synced) >= 4, synced)
os.makedirs(st.p("t-stop"))
try:
    st.read("t-stop")
    ok("A18 a state file that cannot be read stops the run", False, "no error")
except Die as e:
    ok("A18 a state file that cannot be read stops the run", "cannot read state file" in str(e), str(e))
ok("A18 a missing state file reads as empty", st.read("nothing") == "")

# ================================ B2, B3, B6, B9 ================================
ok("B2 the SDK lookup of migrate volume runs sys.executable -I, never 'python3' from PATH",
   volcmd.SDK_PY == [sys.executable, "-I"])
rcdir = os.path.join(WORK, "rc")
os.makedirs(rcdir)
good = os.path.join(rcdir, "good.rc")
open(good, "w").write("export OS_AUTH_URL=http://x:5000/v3\nexport OS_USERNAME=u\n")
bad = os.path.join(rcdir, "bad.rc")
open(bad, "w").write("export OS_AUTH_URL=http://x:5000/v3\nfalse\nexport OS_USERNAME=u\n")
none = os.path.join(rcdir, "none.rc")
open(none, "w").write("export OS_USERNAME=u\n")
clouds._rc_env_cache.clear()
real = inproc.ORIG_RUN
proc.run, saved_run = real, proc.run
e = clouds.rc_env(good, dict(os.environ, OS_REGION_NAME="leaked", OS_CACERT="/leaked"))
ok("B3 the openrc is sourced in a clean environment: OS_* of the caller do not leak in",
   "OS_REGION_NAME" not in e and "OS_CACERT" not in e and e.get("OS_USERNAME") == "u", sorted(k for k in e if k[:3] == "OS_"))
for rc_, want in ((bad, "a command in it failed"), (none, "sets no OS_AUTH_URL")):
    try:
        clouds.rc_env(rc_, dict(os.environ))
        ok("B3 %s refused" % os.path.basename(rc_), False, "no error")
    except Die as err:
        ok("B3 %s refused (set -e / OS_AUTH_URL)" % os.path.basename(rc_), want in str(err), str(err))
os.chmod(good, 0o644)
ok("B3 an openrc readable by others: owner/mode problem named",
   "mode 0644, not 0600" in clouds.rc_file_problem(good) or os.geteuid() != 0, clouds.rc_file_problem(good))
proc.run = saved_run
clouds._rc_env_cache.clear()
src = open(os.path.join(REPO, "migratetool", "cli.py")).read()
ok("B6 'migrate' sets umask 077 before anything", "os.umask(0o077)" in src.split("def main(")[1].split("def _main")[0])

# a doctor on a world where both openrcs name one Keystone
r = app(world(), [("doctor",)], env={"src_rc": {}, "dst_rc": {}})
ok("B9 doctor as root: 'running as root (brick needs no sudo)'; C7: brick's Keystone session checked",
   "  OK    running as root (brick needs no sudo)" in r.out or os.geteuid() != 0, r.out[-800:])
ok("C7 doctor: brick --check-auth line", "  OK    brick: Keystone session of the source openrc works" in r.out, r.out)


def same_keystone(_wf, _root):
    for side in ("src", "dst"):
        p_ = os.environ["%s_RC" % side.upper()]
        set_file(p_, "export OS_AUTH_URL=http://same:5000/v3\nexport OS_AUTH_TYPE=password\nexport OS_PROJECT_NAME=admin\n")
    clouds._rc_env_cache.clear()


r = app(world(), [same_keystone, ("doctor",)])
ok("B3 doctor: SRC_RC and DST_RC naming one Keystone is a FAIL",
   r.rc == [1] and "  FAIL  SRC_RC and DST_RC point at the same Keystone http://same:5000/v3" in r.out, r.out[-600:])

# ================================ C3: deadlines ================================
ok("C3 deadline: openstack/rbd/ceph/systemctl get MIG_CMD_TIMEOUT; qemu-img, blocksync, image save do not",
   proc.deadline(["openstack", "server", "show", "x"]) == proc.CMD_TIMEOUT and proc.deadline(["rbd", "ls"]) == proc.CMD_TIMEOUT
   and proc.deadline(["qemu-img", "convert"]) is None and proc.deadline(["openstack", "image", "save", "--file", "f", "i"]) is None
   and proc.deadline(["/usr/bin/python3", "blocksync.py"]) is None)
slow = os.path.join(WORK, "bin")
os.makedirs(slow)
open(os.path.join(slow, "openstack"), "w").write("#!/bin/sh\nsleep 5\n")
os.chmod(os.path.join(slow, "openstack"), 0o755)
saved_t = proc.CMD_TIMEOUT
proc.CMD_TIMEOUT = 1
t0 = time.monotonic()
res = inproc.ORIG_RUN([os.path.join(slow, "openstack"), "server", "list"], capture=True, stderr=proc.CAPTURE)
proc.CMD_TIMEOUT = saved_t
ok("C3 a hanging openstack call is killed at the deadline: exit 124, 'timed out after 1 s'",
   res.rc == 124 and "timed out after 1 s" in res.stderr and time.monotonic() - t0 < 4, (res, time.monotonic() - t0))
ok("C3 Glance REST calls have a timeout (60 s, 600 s without progress for the upload)",
   ephemeral.REST_TIMEOUT == 60 and ephemeral.UPLOAD_STALL == 600
   and "timeout=UPLOAD_STALL" in open(os.path.join(REPO, "migratetool", "ephemeral.py")).read())
r = app(world(), [("plan", "app01", PLAIN)], env={"env": {"MIG_CMD_TIMEOUT": "ten"}})
ok("C3 MIG_CMD_TIMEOUT must be a number", r.rc == [1] and "MIG_CMD_TIMEOUT takes a whole number" in r.err, r.err)

# ================================ C4, C12: the selector ================================
r = app(world(), [("cli", ["plan", "app01"] + PLAIN)], env={"env": {"PROJECT_MAP_FILE": "/nonexistent/map"}})
ok("C4 an unreadable PROJECT_MAP_FILE stops the run (no silent same-name mapping)",
   r.rc == [1] and "PROJECT_MAP_FILE /nonexistent/map is not a readable file" in r.err, (r.rc, r.err))
r = app(world(), [("cli", ["plan", "app01"] + PLAIN)])
ok("C12 batch log directory '<YYYYmmdd-HHMMSS>-<6 hex>'",
   r.rc == [0] and re.search(r"logs in \S+/log/\d{8}-\d{6}-[0-9a-f]{6};", r.out), r.out[:300])

# ================================ C8: exit codes of a selection ================================
w = world()
w["src"]["servers"]["srv-src-app02"] = dict(json.loads(json.dumps(w["src"]["servers"]["srv-src-app01"])), name="app02",
                                            volumes=[{"Volume ID": "vol-src-root2", "Device": "/dev/vda"}], ports=[])
w["src"]["volumes"]["vol-src-root2"] = dict(w["src"]["volumes"]["vol-src-root"])
w["dst"]["create_status"] = "error"
r = app(w, [("cli", ["migrate", "--vm", "app01,app02", "--yes", "--continue-on-error"] + PLAIN)])
ok("C8 a selection of two rows with a FAIL exits 5", r.rc == [5] and "Totals: OK 0  FAIL 2" in r.out, (r.rc, r.out[-300:]))
r = app(w, [("cli", ["migrate", "app01", "--yes"] + PLAIN)])
ok("C8 one row failing before its cutover: the selection exits 3 (its child's code)", r.rc == [3], (r.rc, r.out[-300:]))
r = app(world(), [("cli", ["migrate", "app01", "--yes"] + PLAIN),
                  edit(lambda x: x["dst"].update(fail={"server create": "No valid host was found (HTTP 400)"}))])
ok("C8 success exits 0", r.rc == [0])

# ================================ C1: --plan-jobs ================================
seen, peak, mu = [], [0], threading.Lock()
live = [0]


def slow_child(self, r_, verb, args, logf, tee, prefix=""):
    with mu:
        live[0] += 1
        peak[0] = max(peak[0], live[0])
    threading.Event().wait(0.3)  # time.sleep is a no-op in this harness
    with open(logf, "w") as f:
        f.write("Destination preflight (x):\n  OK    something\nPreflight passed.\n")
    if tee:
        with self._out:
            sys.stdout.write(prefix + "  OK    something\n")
    with mu:
        live[0] -= 1
        seen.append(r_.ident)
    return 0


w = world()
for n_ in (2, 3):
    w["src"]["servers"]["srv-src-app0%d" % n_] = dict(json.loads(json.dumps(w["src"]["servers"]["srv-src-app01"])),
                                                     name="app0%d" % n_)
orig_child = selector.Runner.child
selector.Runner.child = slow_child
r = app(w, [("cli", ["plan", "--vm", "app01,app02,app03", "--plan-jobs", "3"] + PLAIN)])
selector.Runner.child = orig_child
ok("C1 --plan-jobs 3: three plan children at once, each tee line after [<ident>], the Plan table complete",
   r.rc == [0] and peak[0] == 3 and sorted(seen) == ["app01", "app02", "app03"]
   and "[app02]   OK    something" in r.out and r.out.count("PLAN-OK") >= 3, (r.rc, peak, r.out[-600:]))
r = app(world(), [("cli", ["plan", "app01", "--plan-jobs", "9"] + PLAIN)])
ok("C1 --plan-jobs takes 1 to 8", r.rc == [1] and "--plan-jobs takes a number from 1 to 8" in r.err, r.err)

# ================================ C6: doctor --write-check ================================
r = app(world(), [("cli", ["doctor", "--write-check"])])
wc = [c for c in r.calls if c.startswith("rbd\t")]
ok("C6 doctor --write-check: import (write), export (read back, compared), rm, per pool; the image is mig-doctor-<epoch>",
   "  OK    RBD write check pool mig-volumes as mig-xfer: 4 MiB written, read back, discarded, removed" in r.out
   and any(re.search(r"\bimport \S+ mig-volumes/mig-doctor-\d+$", c) for c in wc)
   and any(re.search(r"\brm mig-volumes/mig-doctor-\d+$", c) for c in wc), (r.out[-600:], wc))


def rbd_fails(wf, _root):
    open(wf + ".rbd-fail", "w").close()


r = app(world(), [rbd_fails, ("cli", ["doctor", "--json", "--write-check"])])
obj = json.loads(r.out) if r.out.strip() else {}
ok("C6 a pool the user cannot write: FAIL, the image removed anyway, doctor --json passed false",
   r.rc == [1] and obj.get("passed") is False and any(c["level"] == "FAIL" and "RBD write check pool mig-volumes" in c["text"]
                                                       for c in obj.get("checks", []))
   and any(re.search(r"\brm mig-volumes/mig-doctor-\d+$", c) for c in r.calls), (r.rc, obj))
r = app(world(), [("cli", ["doctor"])])
ok("C6 without --write-check doctor writes nothing", not [c for c in r.calls if c.startswith("rbd\t") and " import " in c])

# ================================ C9: prune ================================
r = app(world(), [])
root, logs = config.STATE_ROOT, config.LOG_ROOT
old = int(time.time()) - 40 * 86400
new = int(time.time()) - 2 * 86400
for n_ in ("a.rolled-back.%d" % old, "b.rolled-back.%d" % new, "live-old", "plan-only"):
    os.makedirs(os.path.join(root, n_))
open(os.path.join(root, "live-old", "volume-map"), "w").close()
os.utime(os.path.join(root, "live-old"), (old, old))
os.makedirs(os.path.join(logs, time.strftime("%Y%m%d-%H%M%S", time.localtime(old)) + "-abc123"))
os.makedirs(os.path.join(logs, time.strftime("%Y%m%d-%H%M%S", time.localtime(new))))
os.makedirs(os.path.join(logs, "keep-me"))
open(os.path.join(root, "a.rolled-back.%d" % old, "server.json"), "w").write("{}" * 50)
buf, err = io.StringIO(), io.StringIO()
with redirect_stdout(buf), redirect_stderr(err):
    rc_ = cli.main(["prune", "--older-than", "30d", "--dry-run", "--json"])
dry = json.loads(buf.getvalue())
left_after_dry = sorted(os.listdir(root))
with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
    rc2 = cli.main(["prune", "--older-than", "30d"])
ok("C9 prune --dry-run --json lists the old rolled-back dir and the old batch dir (bytes counted), deletes nothing",
   rc_ == 0 and dry["dry_run"] is True and sorted(x["kind"] for x in dry["removed"]) == ["batch", "state"]
   and dry["bytes"] == 100 and left_after_dry == sorted(["a.rolled-back.%d" % old, "b.rolled-back.%d" % new, "live-old",
                                                         "plan-only"]), (dry, left_after_dry))
ok("C9 prune: only those two go; a live state, a plan-only state, a recent one and other names stay",
   rc2 == 0 and sorted(os.listdir(root)) == sorted(["b.rolled-back.%d" % new, "live-old", "plan-only"])
   and len(os.listdir(logs)) == 2 and "keep-me" in os.listdir(logs), (os.listdir(root), os.listdir(logs)))
with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as e_:
    rc3 = cli.main(["prune", "--older-than", "0d"])
ok("C9 --older-than needs at least 1d", rc3 == 1)

# ================================ C10, C11 ================================
w = world()
w["dst"]["quota"] = {"instances": [10, None, 0], "cores": [20, 0, 0]}
r = app(w, [("plan", "app01", PLAIN)])
ok("C10 a quota row without 'In Use': WARN 'usage of ... not readable', not counted as 0",
   "  WARN  destination quota: usage of instances not readable (limit 10): not checked" in r.out, r.out[-600:])
w = world()
w["src"]["server_groups"]["sg-src-anti"] = {"name": "web-anti", "policy": "anti-affinity",
                                            "rules": {"max_server_per_host": 2}, "members": ["srv-src-app01"]}
w["dst"]["server_groups"]["g1"] = {"name": "AZ1-web-anti", "policy": "anti-affinity", "rules": {}, "members": []}
r = app(w, [("plan", "app01", ["--no-copy-secgroup", "--az", "AZ1"])])
ok("C11 a destination group of that name and policy but other rules: FAIL with --server-group <id>",
   r.rc == [1] and "  FAIL  server group AZ1-web-anti exists at the destination (g1) with rules none, the source group "
   "web-anti has max_server_per_host=2" in r.out and "--server-group g1" in r.out, r.out[-500:])
w["dst"]["server_groups"]["g1"]["rules"] = {"max_server_per_host": 2}
r = app(w, [("plan", "app01", ["--no-copy-secgroup", "--az", "AZ1"])])
ok("C11 same rules: reused", r.rc == [0] and "server group AZ1-web-anti exists (g1, policy anti-affinity): reused" in r.out)

# ================================ D12, D16 ================================
r = app(world(), [("migrate", "app01", PLAIN), ("cli", ["status", "--json"])])
st_ = json.loads(r.outs[1])
e_ = st_["live"][0] if st_.get("live") else {}
ok("D12 status --json: phase 'migrated', source_power from the export", e_.get("phase") == "migrated"
   and e_.get("source_power") == "ACTIVE", e_)
w = world()
w["dst"]["create_status"] = "error"
r = app(w, [("migrate", "app01", PLAIN), ("cli", ["status", "--json"])])
e_ = json.loads(r.outs[1])["live"][0]
ok("D12 a run that failed before the cutover: phase 'preparing'", e_.get("phase") == "preparing", e_)
r = app(world(), [("cli", ["plan", "app01", "--json"] + PLAIN)])
row = json.loads(r.out)["rows"][0]
ok("D16 plan --json row: volumes_total, gb_total, nics, source_power, impact",
   row.get("volumes_total") == 2 and row.get("gb_total") == 12 and row.get("nics") == 1
   and row.get("source_power") == "ACTIVE"
   and row.get("impact") == {"stop_source": True, "detach_ports": 1, "source_after": "SHUTOFF+locked",
                             "creates": {"server": 1, "ports": 1, "volumes": 2, "security_groups": 0,
                                         "server_group": 0}}, row)

# ================================ B1: the runner's scrubber ================================
from migui import common  # noqa: E402
common.SECRETS.clear()
common.SECRETS.update({"Sup3r-s3cret-value", "abc"})
t = common.scrub('{"OS_PASSWORD": "x1", "password": "hunter2"} url https://admin:pw123@keystone:5000/v3 '
                 'then Sup3r-s3cret-value and abcdef and abc.')
ok("B1 scrub: JSON-quoted credentials, URL userinfo, the openrc secret values (a short one as a whole word only)",
   "hunter2" not in t and "pw123" not in t and "Sup3r-s3cret-value" not in t and "abcdef" in t
   and "[scrubbed]." in t and "x1" not in t, t)
o = common.scrub_obj({"auth": {"os_password": "zz", "OS_AUTH_URL": "http://k", "token": "batch-token", "text": "Sup3r-s3cret-value"}})
ok("B1 scrub_obj: credential-like keys lose their value; text values scrubbed; a plain 'token' key is left",
   o == {"auth": {"os_password": "[scrubbed]", "OS_AUTH_URL": "[scrubbed]", "token": "batch-token",
                  "text": "[scrubbed]"}}, o)
common.SECRETS.clear()

# ================================ D5: typed names ================================
rule = common.confirm_rule("vm", ["web 1", "web, two", "app"], True, ["id-1", "id-2", "id-3"])
ok("D5 names with spaces and commas, one per line, in any order; a full ident instead of a name",
   common.typed_matches(rule, "web, two\napp\nweb 1\n") and common.typed_matches(rule, "id-1\nweb, two\napp")
   and not common.typed_matches(rule, "web 1 web, two app") and not common.typed_matches(rule, "web 1\napp")
   and not common.typed_matches(rule, "web 1\nweb 1\napp"))
rule = common.confirm_rule("vm", ["same", "same"], True, ["u1", "u2"])
ok("D5 two VMs of one name: the name twice", common.typed_matches(rule, "same\nsame")
   and not common.typed_matches(rule, "same"))
ok("D5 count rule unchanged", common.typed_matches(common.confirm_rule("vm", ["a", "b"], False), "2"))

# ================================ D2: confirmation rule from the effective options ================================
eff = {"keep_ports": {"value": True, "from": "MIG_DEFAULT_OPTS"}, "ip": {"value": "same", "from": "default"}}
ok("D2 keep_ports from MIG_DEFAULT_OPTS (not sent by the UI) makes the rule by name",
   mrunner.needs_name("vm", eff, {}, "") is True
   and mrunner.needs_name("vm", {"ip": {"value": "10.0.0.9", "from": "cli"}}, {}, "") is True
   and mrunner.needs_name("vm", {"start_source": {"value": True, "from": "MIG_DEFAULT_OPTS"}}, {}, "") is True
   and mrunner.needs_name("vm", {"ip": {"value": "same", "from": "default"}}, {}, "") is False
   and mrunner.needs_name("vol", {"in_use": {"value": True, "from": "cli"}}, {}, "") is True)

# ================================ gate follow-ups: R1, R3, MIG_ISCSI_IP, DEPLOY.md ================================
rcdir = os.path.join(WORK, "rc-r1")
os.makedirs(rcdir)
asks = os.path.join(rcdir, "asks.rc")
open(asks, "w").write("export OS_AUTH_URL=http://x:5000/v3\n# read is fine in a comment\necho 'Password: '\n"
                       "read -sr OS_PASSWORD_INPUT\nexport OS_PASSWORD=$OS_PASSWORD_INPUT\n")
quiet = os.path.join(rcdir, "quiet.rc")
open(quiet, "w").write("export OS_AUTH_URL=http://x:5000/v3\nfalse\n# read -s X\nexport OS_READ_ONLY=1\n")
ok("R1 prompt_lines: the 'read' line of a Horizon openrc, not a comment, not a word that contains read",
   clouds.prompt_lines(asks) == [4] and clouds.prompt_lines(quiet) == [], (clouds.prompt_lines(asks), clouds.prompt_lines(quiet)))
clouds._rc_env_cache.clear()
proc.run, saved_run = inproc.ORIG_RUN, proc.run
for rc_, want, absent in ((asks, "line 4 asks for input ('read')", None), (quiet, "a command in it failed", "asks for input")):
    try:
        clouds.rc_env(rc_, dict(os.environ))
        ok("R1 %s refused" % os.path.basename(rc_), False, "no error")
    except Die as err:
        ok("R1 %s refused, the message names the prompting line only when there is one" % os.path.basename(rc_),
           want in str(err) and (absent is None or absent not in str(err)), str(err))
proc.run = saved_run
clouds._rc_env_cache.clear()


def prompting_src(_wf, _root):
    p_ = os.environ["SRC_RC"]
    with open(p_, "a") as f:
        f.write("read -sr OS_PASSWORD_INPUT\n")
    clouds._rc_env_cache.clear()


r = app(world(), [prompting_src, ("doctor",)])
ok("R1 doctor: an openrc that prompts is a FAIL that names the line",
   r.rc == [1] and re.search(r"FAIL  source openrc: sourcing openrc .*src.rc failed \(exit 1\): a command in it failed; "
                             r"line 5 asks for input", r.out), r.out[-900:])

w = world()
w["src"]["volumes"]["vol-src-data"]["status"] = "available"


def hold_vol(_wf, root):
    d_ = os.path.join(root, "vol-vol-src-data")
    os.makedirs(d_, mode=0o700)
    hold_vol.fd = os.open(d_, os.O_RDONLY)
    fcntl.flock(hold_vol.fd, fcntl.LOCK_EX)


r = app(w, [hold_vol, ("volume", "data", [])])
os.close(hold_vol.fd)
ok("R3 migrate volume: another process holds the state directory: dies before it creates anything",
   r.rc == [1] and "is in use by another migrate or rollback" in r.err and idx(r.calls, "dst\tvolume create") is None,
   (r.rc, r.err[-500:]))
r = app(w, [("volume", "data", []), ("volume", "data", [])])
ok("R3 migrate volume twice: the second run stops at the state of the first",
   r.rc == [0, 1] and "state exists in" in r.err, (r.rc, r.err[-400:]))

ok("MIG_ISCSI_IP: no default in the engine; brick falls back only when it is unset or empty",
   config.DEFAULTS["MIG_ISCSI_IP"] == ""
   and 'os.environ.get("MIG_ISCSI_IP") or socket.gethostbyname' in open(os.path.join(REPO, "migratetool", "brick.py")).read())
for value, line in (("", "  FAIL  MIG_ISCSI_IP is not set: set it in the conf to this host's address on the iSCSI network"),
                    ("192.0.2.250", "  FAIL  MIG_ISCSI_IP 192.0.2.250 is not an address of this host (addresses of this host:"),
                    ("127.0.0.1", "  OK    MIG_ISCSI_IP 127.0.0.1 is an address of this host")):
    r = app(world(), [("doctor",)], env={"env": {"MIG_ISCSI_IP": value}})
    ok("MIG_ISCSI_IP doctor %r" % value, line in r.out and (r.rc == [0]) == (value == "127.0.0.1"), r.out[-700:])
saved_la = doctor.local_addresses
doctor.local_addresses = lambda env: None
r = app(world(), [("doctor",)], env={"env": {"MIG_ISCSI_IP": "10.0.0.9"}})
doctor.local_addresses = saved_la
ok("MIG_ISCSI_IP doctor: 'ip' unavailable is an INFO, not a FAIL",
   "  INFO  MIG_ISCSI_IP 10.0.0.9 not compared with this host's addresses" in r.out and r.rc == [0], r.out[-600:])
inst = open(os.path.join(REPO, "migui", "deploy", "install.sh")).read()
units = open(os.path.join(REPO, "migui", "deploy", "migui-web.service")).read()
ok("install.sh copies DEPLOY.md to the path the units name in Documentation=",
   'install -m 0644 -o root -g root "$PKG/../DEPLOY.md" "$PREFIX/DEPLOY.md"' in inst
   and "Documentation=file:///opt/migui/DEPLOY.md" in units and inst.index("DEPLOY.md\" \"$PREFIX") < inst.index("# units"))

# ================================ gate 2026-11-22: what openstackclient 10.x prints for a deleted object ================================
real = ['No Server found for a03980c4-1faf-4ec6-b6de-a46947f50b25', 'No Volume found for nosuchvol',
        'No Image found for nosuchimg', 'No Port found for nosuchport',
        "No server with a name or ID of 'x' exists."]
ok('A4/A15 GONE_RE: the not-found messages of openstackclient 10.5 (lab) and of older clients',
   all(rollback.GONE_RE.search(m) for m in real)
   and not rollback.GONE_RE.search('No valid host was found. There are not enough hosts available.'),
   [m for m in real if not rollback.GONE_RE.search(m)])


class _GoneCloud:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), 0

    def run(self, *a, **k):
        self.calls += 1
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


from migratetool.proc import Result  # noqa: E402
saved_sleep, rollback.time.sleep = rollback.time.sleep, (lambda s: None)
gc = _GoneCloud([Result(0, '{"id": "s1", "status": "DELETED"}', ''),
                 Result(1, '', 'No Server found for s1\n')])
try:
    rollback.wait_gone(None, gc, 'server', 's1', 'destination server', limit=60)
    ok('A4 wait_gone returns at the first "No Server found for" (openstackclient 10.5)', gc.calls == 2, gc.calls)
except Die as err:
    ok('A4 wait_gone returns at the first "No Server found for" (openstackclient 10.5)', False, str(err))
gc = _GoneCloud([Result(1, '', 'Internal Server Error (HTTP 500)\n')])
try:
    rollback.wait_gone(None, gc, 'server', 's1', 'destination server', limit=0)
    ok('A4 wait_gone: an API error is not "gone"', False, 'returned')
except Die as err:
    ok('A4 wait_gone: an API error is not "gone"', 'still there' in str(err), str(err))
rollback.time.sleep = saved_sleep

# ================================ click matrix 2026-11-22: inventory names as Cinder/Nova allow them ================================
inv_ = {'volume_types': ['__DEFAULT__', 'rbd-az1'], 'zones_compute': ['nova'], 'zones_volume': ['nova'], 'dst_hosts': [], 'flavors': []}
ok('UI volume_type __DEFAULT__ (Cinder default type, offered by inventory) is accepted by web and runner validation',
   common.normalize_options('vm', {'volume_type': '__DEFAULT__'}) == ['--volume-type', '__DEFAULT__']
   and common.normalize_options('vol', {'volume_type': '__DEFAULT__'}, inv_) == ['--volume-type', '__DEFAULT__'])
bad_ = []
for v_ in ('-x', 'a b', 'x\ny', '', 'a' * 256, 'nvme'):
    try:
        common.normalize_options('vm', {'volume_type': v_}, inv_)
        bad_.append(v_)
    except common.Reject:
        pass
ok('UI inventory names: a leading -, whitespace, a newline, 256 characters, or a name not in the inventory are refused',
   bad_ == [''], bad_)

# ================================ gate 2026-11-22: SIGINT cancels a migrate started in the background ================================
probe = ('import os, signal, sys; sys.path.insert(0, sys.argv[1]); from migratetool import cli; '
         'before = signal.getsignal(signal.SIGINT) is signal.SIG_IGN; cli.honour_sigint()\n'
         'try:\n    os.kill(os.getpid(), signal.SIGINT); import time; time.sleep(2); print("ignored", before)\n'
         'except KeyboardInterrupt:\n    print("interrupted", before)')
res = subprocess.run([sys.executable, '-I', '-c', probe, REPO], capture_output=True, text=True, timeout=60,
                     preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_IGN))
ok('A6 gate: a migrate started with SIGINT ignored ("migrate ... &" in a script) takes SIGINT back: kill -INT cancels it',
   res.stdout.strip() == 'interrupted True', (res.stdout, res.stderr[-300:]))
src_ = open(os.path.join(REPO, 'migratetool', 'single.py')).read()
ok('A6 gate: the per-VM child takes SIGINT back too', 'with sigint_scope():' in src_.split('def main(')[1][:200])

# ================================ Astra round 2: validators anchored with \Z ================================
nl_ = []
for check_, val_ in (('ident', 'app01\n'), ('name', 'nvme\n'), ('inv', 'rbd-az1\n'), ('user', 'khoi\n'), ('job', 'a' * 16 + '\n')):
    try:
        if check_ == 'ident':
            common.check_ident(val_, 'vm')
        elif check_ == 'name':
            common.check_name(val_, 'flavor')
        elif check_ == 'inv':
            common.normalize_options('vm', {'volume_type': val_}, {'volume_types': [val_]})
        elif check_ == 'user':
            assert not common.USER_RE.match(val_)
            raise common.Reject(400, 'user')
        else:
            assert not common.JOB_ID_RE.match(val_)
            raise common.Reject(400, 'job')
        nl_.append(check_)
    except common.Reject:
        pass
ok('Astra r2: a trailing newline no longer passes the ident, name, inventory, user or job-id validators (anchored at the very end)',
   nl_ == [], nl_)

# ================================ gate 22/11: rollback takes the host lock first; exit code of a cutover state ======
r = app(world(), [])
lock2 = os.environ["MIG_LOCK"]
fd2 = os.open(lock2, os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd2, fcntl.LOCK_EX)


def rerun_window(_wf, root):
    # what a re-run leaves for a moment while it cleans its failed run: the state directory again, no server.json yet
    os.environ["MIG_LOCK"] = lock2
    os.makedirs(os.path.join(root, "app01"), exist_ok=True)


r = app(world(), [rerun_window, ("cli", ["rollback", "app01", "--yes"])])
ok("gate A7: a rollback while another migrate holds the host lock says so, even when the state has no server.json "
   "yet (a re-run starting again), not 'no state'",
   r.rc == [1] and "another migrate, volume or rollback runs on this host" in r.err and "no state" not in r.err, (r.rc, r.err))
os.close(fd2)


def take_lock(_wf, _root):
    take_lock.fd = os.open(os.environ["MIG_LOCK"], os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(take_lock.fd, fcntl.LOCK_EX)


r = app(world(), [("migrate", "app01", PLAIN), take_lock, ("cli", ["rollback", "app01", "--dry-run"]),
                  ("cli", ["rollback", "app01", "--yes"])])
os.close(take_lock.fd)
ok("gate A7: --dry-run reads only and takes no lock (it answers while another run holds it); the real rollback is refused",
   r.rc == [0, 0, 1] and "another migrate, volume or rollback runs on this host" in r.err and r.world["dst"]["servers"],
   (r.rc, r.err[-400:]))
r = app(world(), [("migrate", "app01", PLAIN), ("cli", ["migrate", "app01", "--yes"] + PLAIN)])
ok("gate exit code: 'migrate migrate <vm>' on a state past its cutover is a SKIP(state-exists) row with the rollback "
   "command, nothing run, exit 1 (GUIDE section 7, DESIGN A6)",
   r.rc == [0, 1] and "SKIP(state-exists)" in r.out and "rollback app01" in r.out, (r.rc, r.out[-500:]))

# ================================ Astra final review: kill -INT <selector pid> reaches the child; one SIGINT only ======
fwd_child = (
    "import os, signal, sys, time\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from migratetool import cli\n"
    "cli.honour_sigint()\n"
    "print('child started', flush=True)\n"
    "try:\n"
    "    time.sleep(20)\n"
    "    print('child finished its run', flush=True)\n"
    "except KeyboardInterrupt:\n"
    "    print('child interrupted', flush=True)\n"
    "    os.kill(os.getpid(), signal.SIGINT)  # a second SIGINT while it cleans up (the one the selector passes on)\n"
    "    time.sleep(0.5)\n"
    "    print('child cleanup done', flush=True)\n"
    "    sys.exit(130)\n")
fwd_parent = (
    "import os, signal, sys, threading, time, types\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from migratetool import cli, selector\n"
    "cli.honour_sigint()\n"
    "selector.CHILD = [sys.executable, '-I', '-c', sys.argv[2], sys.argv[1]]\n"
    "run = selector.Runner.__new__(selector.Runner)\n"
    "run._out, run._pmu, run._procs, run._stopping = threading.Lock(), threading.Lock(), set(), False\n"
    "row = types.SimpleNamespace(env={}, ident='x')\n"
    "def later():\n"
    "    time.sleep(2)\n"
    "    os.kill(os.getpid(), signal.SIGINT)  # this PID only, as 'kill -INT <pid>' from another terminal\n"
    "threading.Thread(target=later, daemon=True).start()\n"
    "t0 = time.time()\n"
    "try:\n"
    "    run.child(row, 'migrate', [], sys.argv[3], tee=False)\n"
    "    print('parent: child ran to the end')\n"
    "except KeyboardInterrupt:\n"
    "    print('parent: interrupted after %.0f s' % (time.time() - t0))\n")
fwd_log = os.path.join(WORK, "fwd-child.log")
res = subprocess.run([sys.executable, '-I', '-c', fwd_parent, REPO, fwd_child, fwd_log], capture_output=True, text=True,
                     timeout=60)
fwd_out = open(fwd_log).read() if os.path.exists(fwd_log) else ""
ok("final review: kill -INT to the selector PID only is passed on to its child, which cancels instead of running to the end",
   "parent: interrupted after" in res.stdout and "child interrupted" in fwd_out and "child finished its run" not in fwd_out,
   (res.stdout, res.stderr[-300:], fwd_out))
ok("final review: a second SIGINT during the child's cleanup is ignored; the cleanup ends (SIGKILL stays the hard stop)",
   "child cleanup done" in fwd_out, fwd_out)
once = ('import os, signal, sys, time; sys.path.insert(0, sys.argv[1]); from migratetool import cli; cli.honour_sigint()\n'
        'try:\n    os.kill(os.getpid(), signal.SIGINT); time.sleep(2)\n'
        'except KeyboardInterrupt:\n    print("first", signal.getsignal(signal.SIGINT) is signal.SIG_IGN)\n'
        '    os.kill(os.getpid(), signal.SIGINT); time.sleep(0.3); print("second ignored")')
res = subprocess.run([sys.executable, '-I', '-c', once, REPO], capture_output=True, text=True, timeout=60)
ok("final review: the first SIGINT cancels and leaves SIGINT ignored for the unwinding",
   res.stdout.split() == ["first", "True", "second", "ignored"], (res.stdout, res.stderr[-300:]))

rs_probe = (
    "import os, signal, sys, threading, time\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from migratetool import cli, proc\n"
    "cli.honour_sigint()\n"
    "pids = []\n"
    "child = 'import os, sys, time; sys.stderr.write(str(os.getpid()) + chr(10)); sys.stderr.flush(); time.sleep(30)'\n"
    "def later():\n"
    "    time.sleep(1.5)\n"
    "    os.kill(os.getpid(), signal.SIGINT)  # this process only: the child (blocksync) does not get it\n"
    "threading.Thread(target=later, daemon=True).start()\n"
    "t0 = time.time()\n"
    "try:\n"
    "    proc.run_stream([sys.executable, '-c', child], on_stderr_line=lambda l: pids.append(int(l)))\n"
    "    print('ran to the end')\n"
    "except KeyboardInterrupt:\n"
    "    alive = True\n"
    "    try:\n"
    "        os.kill(pids[0], 0)\n"
    "    except ProcessLookupError:\n"
    "        alive = False\n"
    "    print('interrupted after %d s, child alive %s' % (time.time() - t0, alive))\n")
res = subprocess.run([sys.executable, '-I', '-c', rs_probe, REPO], capture_output=True, text=True, timeout=90)
ok("final review: run_stream interrupted (SIGINT to migrate only) stops its child (blocksync) before the cleanup runs",
   re.match(r"interrupted after [0-4] s, child alive False$", res.stdout.strip()) is not None, (res.stdout, res.stderr[-300:]))

scope_probe = (
    "import signal, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from migratetool import cli\n"
    "signal.signal(signal.SIGINT, signal.default_int_handler)  # an interactive caller; the harness may inherit SIGINT ignored\n"
    "before = signal.getsignal(signal.SIGINT) is signal.default_int_handler\n"
    "with cli.sigint_scope():\n"
    "    inside = signal.getsignal(signal.SIGINT) is cli._sigint_once\n"
    "    try:\n"
    "        signal.raise_signal(signal.SIGINT)\n"
    "    except KeyboardInterrupt:\n"
    "        ignored = signal.getsignal(signal.SIGINT) is signal.SIG_IGN\n"
    "print(before, inside, ignored, signal.getsignal(signal.SIGINT) is signal.default_int_handler)\n")
res = subprocess.run([sys.executable, '-I', '-c', scope_probe, REPO], capture_output=True, text=True, timeout=60)
ok("final review 2: sigint_scope arms the cancel handler for one entry point and gives the caller its handler back, "
   "even after a cancel left SIGINT ignored", res.stdout.split() == ["True"] * 4, (res.stdout, res.stderr[-300:]))
cb_probe = (
    "import os, signal, sys, time\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from migratetool import proc\n"
    "signal.signal(signal.SIGINT, signal.default_int_handler)  # as migrate after honour_sigint: blocksync starts with SIGINT default\n"
    "pids = []\n"
    "child = 'import os, sys, time; sys.stderr.write(str(os.getpid()) + chr(10)); sys.stderr.flush(); time.sleep(30)'\n"
    "def cb(line):\n"
    "    pids.append(int(line))\n"
    "    raise ValueError('callback failed')\n"
    "t0 = time.time()\n"
    "try:\n"
    "    proc.run_stream([sys.executable, '-c', child], on_stderr_line=cb)\n"
    "except ValueError:\n"
    "    try:\n"
    "        os.kill(pids[0], 0); alive = True\n"
    "    except ProcessLookupError:\n"
    "        alive = False\n"
    "    print('error after %d s, child alive %s' % (time.time() - t0, alive))\n")
res = subprocess.run([sys.executable, '-I', '-c', cb_probe, REPO], capture_output=True, text=True, timeout=90)
ok("final review 2: an error in the run_stream callback stops the child too (not only a SIGINT)",
   re.match(r"error after [0-4] s, child alive False$", res.stdout.strip()) is not None, (res.stdout, res.stderr[-300:]))
stub_probe = (
    "import subprocess, sys, time\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from migratetool import proc\n"
    "child = 'import signal, sys, time; signal.signal(signal.SIGINT, signal.SIG_IGN); sys.stdout.write(chr(10)); sys.stdout.flush(); time.sleep(30)'\n"
    "p = subprocess.Popen([sys.executable, '-c', child], stdout=subprocess.PIPE)\n"
    "p.stdout.readline()\n"
    "t0 = time.time(); proc.stop(p, grace=1)\n"
    "print(p.returncode, round(time.time() - t0))\n")
res = subprocess.run([sys.executable, '-I', '-c', stub_probe, REPO], capture_output=True, text=True, timeout=60)
ok("final review 2: proc.stop kills a child that ignores SIGINT once the grace time is over, and reaps it",
   res.stdout.split() == ["-9", "1"], (res.stdout, res.stderr[-300:]))

stopint_probe = (
    "import os, signal, subprocess, sys, threading, time\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from migratetool import proc\n"
    "signal.signal(signal.SIGINT, signal.default_int_handler)\n"
    "child = 'import signal, sys, time; signal.signal(signal.SIGINT, signal.SIG_IGN); sys.stdout.write(chr(10)); sys.stdout.flush(); time.sleep(60)'\n"
    "p = subprocess.Popen([sys.executable, '-c', child], stdout=subprocess.PIPE)\n"
    "p.stdout.readline()\n"
    "def later():\n"
    "    time.sleep(1)\n"
    "    os.kill(os.getpid(), signal.SIGINT)\n"
    "threading.Thread(target=later, daemon=True).start()\n"
    "t0 = time.time()\n"
    "try:\n"
    "    proc.stop(p, grace=20)\n"
    "    print('no interrupt')\n"
    "except KeyboardInterrupt:\n"
    "    print('interrupt', p.returncode, round(time.time() - t0))\n")
res = subprocess.run([sys.executable, '-I', '-c', stopint_probe, REPO], capture_output=True, text=True, timeout=90)
ok("final review 3: a SIGINT while proc.stop waits: the child is killed and reaped first, then the interrupt goes on",
   res.stdout.split() == ["interrupt", "-9", "1"], (res.stdout, res.stderr[-300:]))
thread_probe = (
    "import signal, sys, threading\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from migratetool import cli\n"
    "signal.signal(signal.SIGINT, signal.default_int_handler)  # the harness may inherit SIGINT ignored\n"
    "out = []\n"
    "def worker():\n"
    "    try:\n"
    "        with cli.sigint_scope():\n"
    "            out.append('inside')\n"
    "        out.append('left')\n"
    "    except Exception as e:\n"
    "        out.append(type(e).__name__)\n"
    "t = threading.Thread(target=worker); t.start(); t.join()\n"
    "print(' '.join(out), signal.getsignal(signal.SIGINT) is signal.default_int_handler)\n")
res = subprocess.run([sys.executable, '-I', '-c', thread_probe, REPO], capture_output=True, text=True, timeout=60)
ok("final review 3: sigint_scope in a worker thread leaves the signal handlers alone instead of failing",
   res.stdout.split() == ["inside", "left", "True"], (res.stdout, res.stderr[-300:]))


def vol_lock_free(_wf, root):
    d_ = [x for x in os.listdir(root) if x.startswith("vol-")][0]
    fd_ = os.open(os.path.join(root, d_), os.O_RDONLY)
    try:
        fcntl.flock(fd_, fcntl.LOCK_EX | fcntl.LOCK_NB)
        vol_lock_free.free = True
    except OSError:
        vol_lock_free.free = False
    finally:
        os.close(fd_)


w2 = world()
w2["src"]["volumes"]["vol-src-data"]["status"] = "available"
r = app(w2, [("volume", "data", []), vol_lock_free])
ok("final review: R3 a finished migrate volume releases its state directory lock (taken again at once in the same process)",
   r.rc == [0] and vol_lock_free.free is True, (r.rc, getattr(vol_lock_free, "free", None), r.err[-300:]))

# ================================ fix-1.0.1 (Review 23/11): the selector under SIGINT ================================
# A probe process runs Runner.run() of 'migrate migrate --vm app01,app02 --yes' with the listing and the plan stubbed
# and CHILD replaced by a fake child; SIGINT goes to the probe's PID only ('kill -INT <pid>').
sel_probe = (
    "import os, signal, sys, threading, types\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from migratetool import cli, selector\n"
    "signal.signal(signal.SIGINT, signal.default_int_handler)  # the harness may run with SIGINT ignored\n"
    "selector.CHILD = [sys.executable, '-I', '-c', sys.argv[2], sys.argv[1]]\n"
    "run = selector.Runner.__new__(selector.Runner)\n"
    "run._out, run._pmu, run._procs, run._stopping = threading.Lock(), threading.Lock(), set(), False\n"
    "run.s = types.SimpleNamespace(verb=sys.argv[4], scope='all', plan_only=False, plan_jobs=int(sys.argv[5]), yes=True,\n"
    "                              cont=False, passed=[], progress=False, json=False)\n"
    "run.cfg = types.SimpleNamespace(PROJECT_MAP_FILE='')\n"
    "run.pmap, run.rows, run.log_dir = {}, [], sys.argv[3]\n"
    "def rows():\n"
    "    for n in sys.argv[6].split(','):\n"
    "        r = selector.Row('vm', 'id-' + n, n, 'ACTIVE', 'admin', 'admin', 'selected')\n"
    "        r.ident = n\n"
    "        run.rows.append(r)\n"
    "run.enumerate_vms, run.load_pmap, run.row_env = rows, (lambda: None), (lambda r: None)\n"
    "run.volume_pass = lambda: ([], [])\n"
    "if sys.argv[4] == 'migrate':\n"
    "    run.plan_row = lambda r, vmx, tee, prefix: setattr(r, 'res', 'PLAN-OK')\n"
    "with cli.sigint_scope():\n"
    "    rc = cli.guarded(run.run)\n"
    "print('rc', rc, flush=True)\n")
# the fake child: says it started (with its PID), then sleeps; a SIGINT makes it exit 130 as a migrate child does
sel_child = (
    "import os, signal, sys, time\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from migratetool import cli\n"
    "ign = float(os.environ.get('FAKE_CHILD_IGN', '0'))\n"
    "if ign:  # round 2: a child started after the selector had the SIGINT inherits it ignored, until honour_sigint()\n"
    "    signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
    "else:\n"
    "    cli.honour_sigint()\n"
    "print('child started %s pid %d' % (sys.argv[3], os.getpid()), flush=True)\n"
    "try:\n"
    "    if ign:\n"
    "        time.sleep(ign)\n"
    "        cli.honour_sigint()\n"
    "    time.sleep(float(os.environ.get('FAKE_CHILD_SECS', '5')))\n"
    "    print('child finished', flush=True)\n"
    "except KeyboardInterrupt:\n"
    "    print('child interrupted', flush=True)\n"
    "    sys.exit(130)\n")


def sel_run(verb, jobs, idents, secs, wait_for, after=1.0, ign=0):
    """The probe; SIGINT to its PID 'after' s after the output shows each of wait_for: (rc, output, seconds from the
    SIGINT to the probe's exit, PIDs of the children). ign: each child starts with SIGINT ignored for that many s."""
    d_ = tempfile.mkdtemp(dir=WORK)
    p_ = subprocess.Popen([sys.executable, '-I', '-c', sel_probe, REPO, sel_child, d_, verb, str(jobs), idents],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                          env=dict(os.environ, FAKE_CHILD_SECS=str(secs), FAKE_CHILD_IGN=str(ign)))
    got, pids, need = [], [], set(wait_for)
    timer = threading.Timer(60, p_.kill)  # a probe that hangs must not hang the suite
    timer.start()
    for line in p_.stdout:
        got.append(line)
        m_ = re.search(r"child started (\S+) pid (\d+)", line)
        if m_:
            pids.append(int(m_.group(2)))
            need.discard(m_.group(1))
            if not need:
                break
    threading.Event().wait(after)  # time.sleep is a no-op in this harness
    os.kill(p_.pid, signal.SIGINT)
    t_ = time.monotonic()
    got.append(p_.stdout.read())
    p_.wait()
    took = time.monotonic() - t_
    timer.cancel()
    return p_.returncode, "".join(got), took, pids


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


rc_, out_, _took, _pids = sel_run("migrate", 1, "app01,app02", 5, ["app01"])
ok("fix-1.0.1 #1 kill -INT during a selector migrate run: the Summary is printed, the running row FAIL(interrupted) "
   "with its rollback hint, the row after it SKIP(not-run), exit 130",
   rc_ == 0 and "rc 130" in out_ and 0 <= out_.find("child interrupted") < out_.find("Summary:")
   and re.search(r"^vm\s+id-app01\s+app01\s.*FAIL\(interrupted\)$", out_, re.M)
   and re.search(r"^vm\s+id-app02\s+app02\s.*SKIP\(not-run\)$", out_, re.M)
   and re.search(r"^  app01: inspect, then to undo: \S*migrate rollback app01$", out_, re.M)
   and "Totals: OK 0  FAIL 1  SKIP 1" in out_, out_)
rc_, out_, took_, pids_ = sel_run("plan", 3, "app01,app02,app03", 30, ["app01", "app02", "app03"])
alive_ = [p_ for p_ in pids_ if pid_alive(p_)]
ok("fix-1.0.1 #3 plan --plan-jobs 3, three plan children sleeping 30 s, kill -INT to the selector PID only (not the "
   "group) after 1 s: the selector exits 130 in < 15 s and all three children are dead (each got the SIGINT)",
   rc_ == 0 and "rc 130" in out_ and took_ < 15 and len(pids_) == 3 and not alive_ and out_.count("child interrupted") == 3,
   ("took %.2f s" % took_, pids_, alive_, out_[-1500:]))
print("INFO fix-1.0.1 #3 selector exit %.2f s after the SIGINT; children %s, alive after: %s" % (took_, pids_, alive_))
rc_, out_, took_, pids_ = sel_run("plan", 2, "app01,app02,app03", 30, ["app01", "app02"])
ok("fix-1.0.1 #3 plan --plan-jobs 2 over three VMs, kill -INT while two run: the third plan child is never started, "
   "exit 130 in < 15 s",
   rc_ == 0 and "rc 130" in out_ and took_ < 15 and "child started app03" not in out_
   and not [p_ for p_ in pids_ if pid_alive(p_)], ("took %.2f s" % took_, out_[-1500:]))
# Round 2: the race of a plan child started between the SIGINT and the stop. It inherits SIGINT ignored and the one the
# selector passes on lands before its handler is installed (simulated: ignored for its first 2 s): the second SIGINT,
# PLAN_STOP_GRACE_1 s later, stops it.
rc_, out_, took_, pids_ = sel_run("plan", 2, "app01,app02", 30, ["app01", "app02"], after=0.5, ign=2)
alive_ = [p_ for p_ in pids_ if pid_alive(p_)]
ok("fix-1.0.1 #3 round 2 plan --plan-jobs 2, both children start with SIGINT ignored for 2 s (the startup race), "
   "kill -INT 0.5 s after both run: the first SIGINT is lost, the second one stops them; exit 130 in < 15 s, both dead",
   rc_ == 0 and "rc 130" in out_ and took_ < 15 and len(pids_) == 2 and not alive_
   and out_.count("child interrupted") == 2, ("took %.2f s" % took_, pids_, alive_, out_[-1500:]))
print("INFO fix-1.0.1 #3 round 2 (startup race) selector exit %.2f s after the SIGINT; children %s, alive after: %s"
      % (took_, pids_, alive_))
# ... and a child that never honours SIGINT (ignored for 60 s) is killed after PLAN_STOP_GRACE_1 + PLAN_STOP_GRACE_2 s.
rc_, out_, took_, pids_ = sel_run("plan", 2, "app01,app02", 30, ["app01", "app02"], after=0.5, ign=60)
alive_ = [p_ for p_ in pids_ if pid_alive(p_)]
ok("fix-1.0.1 #3 round 2 plan --plan-jobs 2, both children ignore SIGINT for 60 s: they are killed, the selector exits "
   "130 in < 15 s, no 'child finished'",
   rc_ == 0 and "rc 130" in out_ and took_ < 15 and len(pids_) == 2 and not alive_ and "child finished" not in out_,
   ("took %.2f s" % took_, pids_, alive_, out_[-1500:]))
print("INFO fix-1.0.1 #3 round 2 (never honours SIGINT) selector exit %.2f s after the SIGINT; children %s, alive after: %s"
      % (took_, pids_, alive_))

# ================================ fix-1.0.1 #4 (Review 23/11): outside_runs() leaves a --dry-run out ================================
orun = mrunner.Runner.__new__(mrunner.Runner)
orun.cli_json = lambda args, timeout, on_line=None: (0, {"running": [
    {"pid": 101, "cmdline": ["/opt/mig-venv/bin/migrate", "rollback", "x", "--dry-run", "--json"]},
    {"pid": 102, "cmdline": ["/opt/mig-venv/bin/migrate", "rollback", "x", "--yes"]},
    {"pid": 103, "cmdline": ["/usr/bin/python3", "/opt/mig-venv/bin/migrate", "migrate", "app01", "--yes"]},
    {"pid": 104, "cmdline": ["/opt/mig-venv/bin/migrate", "status", "--json"]}]}, "")
ok("fix-1.0.1 #4 outside_runs(): 'migrate rollback x --dry-run --json' is not a run outside the UI; 'rollback x --yes' "
   "and 'migrate app01' still are", orun.outside_runs() == [102, 103], orun.outside_runs())

# ================================ fix-1.0.1 #5 (Review 23/11): the runner writes its pid into the lock file ================================
lrun = mrunner.Runner.__new__(mrunner.Runner)
lrun.lock_path = os.path.join(WORK, "lock5", "migrate.lock")
os.makedirs(os.path.dirname(lrun.lock_path))
set_file(lrun.lock_path, "pid 1 (an older holder)\nmore text\n")
lfd = lrun.try_lock()
second = lrun.try_lock()
held5 = open(lrun.lock_path).read()
ok("fix-1.0.1 #5 try_lock(): the lock file says 'pid <runner pid> (migui-runner)' (the old text replaced), which the CLI "
   "shows as 'held by ...'; a second try_lock() is refused",
   lfd is not None and second is None and held5 == "pid %d (migui-runner)\n" % os.getpid()
   and locking._holder(lrun.lock_path) == "pid %d (migui-runner)" % os.getpid(), (lfd, second, held5))
if lfd is not None:
    os.close(lfd)

# ================================ version ================================
ok("1.0.1 version: migratetool, migui and pyproject agree",
   migratetool.__version__ == "1.0.1" and __import__("migui").__version__ == "1.0.1"
   and 'version = "1.0.1"' in open(os.path.join(REPO, "pyproject.toml")).read(), migratetool.__version__)

shutil.rmtree(WORK, ignore_errors=True)
print("TOTAL pass=%d fail=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
