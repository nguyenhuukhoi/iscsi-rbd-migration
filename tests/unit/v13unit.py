#!/usr/bin/env python3
"""1.2.0 unit checks: VOLUME_TYPE_MAP (each destination volume gets the type and pool of its source volume type),
R1 (gone() takes a 404 anywhere on a line that names the object), R3 (a destination port or image listing the rollback
cannot read or delete is a WARN, not silence and not the end of the rollback), the wording of an application credential
refused for a state scoped by its record, doctor's check of the map, the version, and the parity rule v13_rule. Runs
the app in this process against the fake clouds (inproc.py), like v12unit.py. Test IDs T1-T24: the 1.2.0 design;
T25-T28 and the '*' key in T3/T7/T23: its 1.2.1 appendix (the fallback key is '*', '__DEFAULT__' a type name);
T29 (the Ceph capacity of an ephemeral root disk) and T30 (doctor with a conf that does not parse): its 1.2.2 appendix.

    python3 v13unit.py <directory holding migratetool/>
"""
import glob
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # run with -I: no implicit script directory
import inproc  # noqa: E402  (puts the package on sys.path, patches proc.run and the selector child)
from inproc import app, edit, world  # noqa: E402

PASS = FAIL = 0
PLAIN = ["--no-copy-secgroup", "--no-server-group"]
ROLL = ("rollback", "app01")
SID = "srv-src-app01"
WORK = tempfile.mkdtemp(prefix="v13unit-")
REPO = inproc.PKG_PARENT
MAP = "ps-gold:rbd-ssd,ps-silver:rbd-hdd"
POOLS = "rbd-ssd:volumes-ssd,rbd-hdd:volumes"


def ok(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS " + label)
    else:
        FAIL += 1
        print("FAIL %s %s" % (label, str(detail)[-3000:]))


def mark(_wf, _root):
    """A '#rollback' line in the call log: what comes after it is the rollback's."""
    with open(os.environ["HARNESS_CALLS"], "a") as f:
        f.write("#rollback\n")


def after_mark(calls):
    return calls[calls.index("#rollback") + 1:] if "#rollback" in calls else list(calls)


def retired(r, ident):
    return glob.glob(os.path.join(r.state, ident + ".rolled-back.*"))


def read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def jload(path):
    text = read(path)
    try:
        return json.loads(text) if text is not None else {}
    except ValueError:
        return {}


def has(calls, prefix):
    return any(c.startswith(prefix) for c in calls)


def rbd_info(calls, pool, dvid):
    return any(c.startswith("rbd\t") and (" -p %s info volume-%s" % (pool, dvid)) in c for c in calls)


def vtm_world():
    """Section 2.3 of the design: root ps-gold 10 GB, data ps-silver 2 GB, data2 ps-bronze 1 GB; the destination has
    the types rbd-ssd (pool volumes-ssd) and rbd-hdd (pool volumes), its default type lands in volumes."""
    w = world()
    s = w["src"]
    s["volumes"]["vol-src-root"]["type"] = "ps-gold"
    s["volumes"]["vol-src-data"]["type"] = "ps-silver"
    s["volumes"]["vol-src-data2"] = {"name": "data2", "status": "in-use", "size": 1, "bootable": "false",
                                     "encrypted": False, "multiattach": False, "type": "ps-bronze"}
    s["servers"][SID]["volumes"].append({"Volume ID": "vol-src-data2", "Device": "/dev/vdc"})
    d = w["dst"]
    d["volume_types"] = ["rbd-ssd", "rbd-hdd"]
    d["type_pool"] = {"": "volumes", "rbd-ssd": "volumes-ssd", "rbd-hdd": "volumes"}
    return w


def menv(vmap=MAP, **extra):
    e = {"VOLUME_TYPE_MAP": vmap, "RBD_POOL_MAP": POOLS, "RBD_POOL": "volumes"}
    e.update(extra)
    return {"env": e}


INFO_ROOT = "  INFO  volume vol-src-root (no name) source type ps-gold -> destination type %s pool %s"
INFO_DATA = "  INFO  volume vol-src-data (data) source type ps-silver -> destination type %s pool %s"
INFO_DATA2 = "  INFO  volume vol-src-data2 (data2) source type ps-bronze -> destination type %s pool %s"
WARN_BRONZE = ("  WARN  source type ps-bronze has no entry in VOLUME_TYPE_MAP and no '*' entry: lands on default")

# ================================ T1: the map without --volume-type (plan, then migrate cold) ================================
r = app(vtm_world(), [("plan", "app01", PLAIN), ("migrate", "app01", PLAIN)], env=menv())
plan = r.outs[0]
ok("T1 plan: one INFO per volume with its own type, pool and origin; ps-bronze (not in the map) lands on the run's "
   "type (default) in RBD_POOL with a WARN; 'VOLUME_TYPE_MAP in use: 2 volume(s) mapped'; no 1.1.1 'all land on' WARN",
   r.rc[0] == 0 and (INFO_ROOT % ("rbd-ssd", "volumes-ssd")) + " (VOLUME_TYPE_MAP (ps-gold))" in plan
   and (INFO_DATA % ("rbd-hdd", "volumes")) + " (VOLUME_TYPE_MAP (ps-silver))" in plan
   and (INFO_DATA2 % ("default", "volumes")) + " (default)" in plan and WARN_BRONZE in plan
   and plan.count("  WARN  source type ") == 1 and "  INFO  VOLUME_TYPE_MAP in use: 2 volume(s) mapped" in plan
   and "all land on destination type" not in plan and "overrides VOLUME_TYPE_MAP" not in plan, (r.rc, plan[-2500:]))
ok("T1 plan: the header keeps the run's type and pool (type=default pool=volumes); one type check per mapped type "
   "(rbd-ssd, rbd-hdd), named by where it came from",
   "Destination preflight (mode=cold ip=same type=default pool=volumes" in plan
   and "  OK    volume type rbd-ssd (VOLUME_TYPE_MAP (ps-gold))" in plan
   and "  OK    volume type rbd-hdd (VOLUME_TYPE_MAP (ps-silver))" in plan
   and [c for c in r.calls if c.startswith("dst\tvolume type show")][:2]
   == ["dst\tvolume type show rbd-ssd", "dst\tvolume type show rbd-hdd"], plan[-2500:])
ok("T1 plan: Ceph capacity per pool (volumes-ssd 10 GB, volumes 3 GB), RBD access to both pools (two lines)",
   "  OK    Ceph pool volumes-ssd: 10 GB needed, " in plan and "  OK    Ceph pool volumes: 3 GB needed, " in plan
   and "  OK    RBD access to pool volumes-ssd" in plan and "  OK    RBD access to pool volumes" in plan
   and plan.count("RBD access to pool") == 2 and plan.count("Ceph pool ") == 2, plan[-1500:])
ok("T1 migrate: 'volume create' with --type rbd-ssd (root), --type rbd-hdd (data), no --type (data2, the default type)",
   r.rc == [0, 0] and has(r.calls, "dst\tvolume create app01-vda --size 10 --type rbd-ssd --description ")
   and has(r.calls, "dst\tvolume create app01-vdb --size 2 --type rbd-hdd --description ")
   and has(r.calls, "dst\tvolume create app01-vdc --size 1 --description "), (r.rc, r.err[-500:]))
ok("T1 migrate: 'rbd info' in each volume's pool, the qemu-img copy into it",
   rbd_info(r.calls, "volumes-ssd", "vol-dst-0001") and rbd_info(r.calls, "volumes", "vol-dst-0002")
   and rbd_info(r.calls, "volumes", "vol-dst-0003")
   and any("rbd:volumes-ssd/volume-vol-dst-0001:" in c for c in r.calls if c.startswith("qemu-img\tconvert"))
   and any("rbd:volumes/volume-vol-dst-0002:" in c for c in r.calls if c.startswith("qemu-img\tconvert"))
   and any("rbd:volumes/volume-vol-dst-0003:" in c for c in r.calls if c.startswith("qemu-img\tconvert"))
   and not any("rbd:volumes-ssd/volume-vol-dst-0002" in c or "rbd:volumes-ssd/volume-vol-dst-0003" in c
               for c in r.calls), [c for c in r.calls if c.startswith(("rbd\t", "qemu-img\t"))])
dv = r.world["dst"]["volumes"]
ok("T1 migrate: the destination volumes have those types and pools",
   (dv.get("vol-dst-0001", {}).get("type"), dv.get("vol-dst-0001", {}).get("pool")) == ("rbd-ssd", "volumes-ssd")
   and (dv.get("vol-dst-0002", {}).get("type"), dv.get("vol-dst-0002", {}).get("pool")) == ("rbd-hdd", "volumes")
   and (dv.get("vol-dst-0003", {}).get("type"), dv.get("vol-dst-0003", {}).get("pool")) == (None, "volumes"), dv)
sd = os.path.join(r.state, "app01")
ok("T1 state: volume-map keeps its two columns; volume-types '<source id> <type|-> <pool>' per volume",
   read(os.path.join(sd, "volume-map")) == "vol-src-root vol-dst-0001\nvol-src-data vol-dst-0002\n"
                                           "vol-src-data2 vol-dst-0003\n"
   and read(os.path.join(sd, "volume-types")) == "vol-src-root rbd-ssd volumes-ssd\nvol-src-data rbd-hdd volumes\n"
                                                 "vol-src-data2 - volumes\n",
   (read(os.path.join(sd, "volume-map")), read(os.path.join(sd, "volume-types"))))
doc = jload(os.path.join(sd, "options.json"))
eff = doc.get("effective", {})
ok("T1 options.json: effective.volume_types {<source id>: {type, pool, from}} from conf VOLUME_TYPE_MAP; volume_type "
   "and pool keep the run's; conf carries VOLUME_TYPE_MAP",
   eff.get("volume_types") == {"value": {
       "vol-src-root": {"type": "rbd-ssd", "pool": "volumes-ssd", "from": "VOLUME_TYPE_MAP (ps-gold)"},
       "vol-src-data": {"type": "rbd-hdd", "pool": "volumes", "from": "VOLUME_TYPE_MAP (ps-silver)"},
       "vol-src-data2": {"type": "", "pool": "volumes", "from": "default"}}, "from": "conf", "detail": "VOLUME_TYPE_MAP"}
   and eff.get("volume_type") == {"value": "", "from": "default"}
   and eff.get("pool") == {"value": "volumes", "from": "default", "detail": "RBD_POOL"}
   and doc.get("conf", {}).get("VOLUME_TYPE_MAP") == MAP, (eff.get("volume_types"), doc.get("conf")))
r = app(vtm_world(), [("cli", ["plan", "app01", "--json"] + PLAIN)], env=menv())
try:
    row = json.loads(r.out).get("rows", [{}])[0]
except (ValueError, AttributeError):
    row = {}
ok("T1 plan --json: the row's effective carries volume_types too",
   ((row.get("effective") or {}).get("volume_types") or {}).get("value", {}).get("vol-src-root")
   == {"type": "rbd-ssd", "pool": "volumes-ssd", "from": "VOLUME_TYPE_MAP (ps-gold)"}, (r.rc, row.get("effective")))

# ================================ T2: --volume-type typed wins over the map ================================
r = app(vtm_world(), [("plan", "app01", PLAIN + ["--volume-type", "rbd-hdd"]),
                      ("migrate", "app01", PLAIN + ["--volume-type", "rbd-hdd"])], env=menv())
plan = r.outs[0]
ok("T2 --volume-type rbd-hdd: one 'INFO volume type rbd-hdd from cli overrides VOLUME_TYPE_MAP'; every volume "
   "rbd-hdd/volumes (cli); no WARN, no 'in use' line; one RBD access line",
   r.rc == [0, 0] and plan.count("  INFO  volume type rbd-hdd from cli overrides VOLUME_TYPE_MAP") == 1
   and (INFO_ROOT % ("rbd-hdd", "volumes")) + " (cli)" in plan and (INFO_DATA2 % ("rbd-hdd", "volumes")) + " (cli)" in plan
   and "  WARN  source type" not in plan and "VOLUME_TYPE_MAP in use" not in plan
   and plan.count("RBD access to pool") == 1 and "  OK    volume type rbd-hdd (cli)" in plan, (r.rc, plan[-2000:]))
ok("T2 ... the three volumes are created with --type rbd-hdd and checked in pool volumes",
   all(has(r.calls, "dst\tvolume create app01-%s --size %s --type rbd-hdd " % (d, s))
       for d, s in (("vda", 10), ("vdb", 2), ("vdc", 1)))
   and all(rbd_info(r.calls, "volumes", "vol-dst-000%d" % i) for i in (1, 2, 3))
   and not any(" -p volumes-ssd " in c for c in r.calls if c.startswith("rbd\t")), r.calls[-60:])

# ================================ T3: '*' (1.2.1) covers the type the map does not name ================================
r = app(vtm_world(), [("plan", "app01", PLAIN), ("migrate", "app01", PLAIN)],
        env=menv(MAP + ",*:rbd-hdd"))
plan = r.outs[0]
ok("T3 *:rbd-hdd: data2 (ps-bronze) -> rbd-hdd/volumes (VOLUME_TYPE_MAP (*)), no WARN, 3 mapped",
   r.rc == [0, 0] and (INFO_DATA2 % ("rbd-hdd", "volumes")) + " (VOLUME_TYPE_MAP (*))" in plan
   and "  WARN  source type" not in plan and "  INFO  VOLUME_TYPE_MAP in use: 3 volume(s) mapped" in plan
   and has(r.calls, "dst\tvolume create app01-vdc --size 1 --type rbd-hdd ")
   and "vol-src-data2 rbd-hdd volumes\n" in (read(os.path.join(r.state, "app01", "volume-types")) or ""),
   (r.rc, plan[-1500:]))
w = vtm_world()
w["src"]["volumes"]["vol-src-data2"].pop("type")
r = app(w, [("plan", "app01", PLAIN)], env=menv(MAP + ",*:rbd-hdd"))
ok("T3 a source volume without a type takes '*' too", r.rc == [0] and (
    "  INFO  volume vol-src-data2 (data2) source type unknown -> destination type rbd-hdd pool volumes "
    "(VOLUME_TYPE_MAP (*))") in r.out, r.out[-1500:])
r = app(w, [("plan", "app01", PLAIN)], env=menv())
ok("T3 ... and without '*' it lands on the run's type with a WARN naming it 'unknown'",
   r.rc == [0] and "  WARN  source type unknown has no entry in VOLUME_TYPE_MAP and no '*' entry: lands on default"
   in r.out, r.out[-1500:])

# ================================ T4: a mapped destination type that does not exist ================================
r = app(vtm_world(), [("migrate", "app01", PLAIN)], env=menv("ps-gold:rbd-nvme,ps-silver:rbd-hdd"))
ok("T4 ps-gold:rbd-nvme, no such type at the destination: preflight FAIL 'destination volume type rbd-nvme "
   "(VOLUME_TYPE_MAP (ps-gold)) does not exist', exit 1, nothing created, the source not touched",
   r.rc == [1] and "  FAIL  destination volume type rbd-nvme (VOLUME_TYPE_MAP (ps-gold)) does not exist" in r.out
   and "  OK    volume type rbd-hdd (VOLUME_TYPE_MAP (ps-silver))" in r.out
   and not has(r.calls, "dst\tvolume create") and not has(r.calls, "src\tserver stop")
   and not has(r.calls, "src\tvolume create"), (r.rc, r.out[-1500:]))

# ================================ T5: an empty map is 1.1.1 ================================
r = app(vtm_world(), [("plan", "app01", PLAIN), ("migrate", "app01", PLAIN)], env=menv(""))
plan = r.outs[0]
ok("T5 empty map: the 1.1.1 lines (INFO without an origin, the 'all land on' WARN), no VOLUME_TYPE_MAP line, one type "
   "of the run for every volume, one RBD access line, one Ceph pool line",
   r.rc == [0, 0] and (INFO_ROOT % ("default", "volumes")) + "\n" in plan
   and (INFO_DATA2 % ("default", "volumes")) + "\n" in plan
   and "  WARN  3 source volume types (ps-gold, ps-silver, ps-bronze) all land on destination type default: pass "
       "--volume-type per run if they must differ" in plan
   and "VOLUME_TYPE_MAP" not in r.text and plan.count("RBD access to pool") == 1 and plan.count("Ceph pool ") == 1
   and not has(r.calls, "dst\tvolume type show") and not any("--type" in c for c in r.calls
                                                            if c.startswith("dst\tvolume create")),
   (r.rc, plan[-2000:]))
ok("T5 empty map: volume-map two columns, volume-types names the run's pool for each volume",
   read(os.path.join(r.state, "app01", "volume-types")) == "vol-src-root - volumes\nvol-src-data - volumes\n"
                                                           "vol-src-data2 - volumes\n",
   read(os.path.join(r.state, "app01", "volume-types")))
r = app(world(), [("plan", "app01", PLAIN + ["--volume-type", "nvme"])], env={"env": {"RBD_POOL_MAP": "nvme:volumes-nvme"}})
ok("T5 empty map with --volume-type: no 'overrides' line (nothing to override), the 1.1.1 'volume type nvme' check",
   r.rc == [0] and "overrides" not in r.out and "  OK    volume type nvme\n" in r.out
   and "  OK    RBD access to pool volumes-nvme" in r.out, r.out[-1500:])

# ================================ T6: live mode, two pools ================================
r = app(vtm_world(), [("migrate", "app01", PLAIN + ["--mode", "live"])], env=menv())
bs = [c for c in r.calls if c.startswith("blocksync\t")]


def pools_of(vid):
    return {c.split(" --pool ", 1)[1].split()[0] for c in bs if ("--state " in c and ("hash-%s.bin" % vid) in c)}


ok("T6 live: every blocksync pass (pre-sync and cutover) of each volume writes into that volume's pool",
   r.rc == [0] and len(bs) >= 9 and pools_of("vol-src-root") == {"volumes-ssd"} and pools_of("vol-src-data") == {"volumes"}
   and pools_of("vol-src-data2") == {"volumes"}
   and all((" --image volume-vol-dst-0001 " in c) == ("hash-vol-src-root.bin" in c) for c in bs),
   (r.rc, bs, r.err[-400:]))
r = app(vtm_world(), [("migrate", "app01", PLAIN + ["--mode", "live", "--verify", "full"])], env=menv())
ok("T6 live --verify full: the cutover compare reads each volume from its pool",
   r.rc == [0] and any("rbd:volumes-ssd/volume-vol-dst-0001:" in c for c in r.calls if c.startswith("qemu-img\tcompare"))
   and any("rbd:volumes/volume-vol-dst-0002:" in c for c in r.calls if c.startswith("qemu-img\tcompare")),
   [c for c in r.calls if c.startswith("qemu-img")])


# ================================ T7: the root disk of an ephemeral VM ================================
def eph_world(disk=1):
    w = vtm_world()
    w["src"]["servers"]["srv-src-eph01"] = {
        "name": "eph01", "status": "ACTIVE", "locked": False, "locked_reason": None, "image": "cirros (img-base)",
        "flavor": {"original_name": "m1.small", "name": "m1.small", "vcpus": 2, "ram": 2048, "disk": disk},
        "key_name": "k1", "properties": {}, "description": None, "ports": ["port-src-eph"], "volumes": []}
    w["src"]["ports"]["port-src-eph"] = {"name": "", "network_id": "net-src-1", "mac_address": "fa:16:3e:aa:bb:60",
                                         "fixed_ips": [{"subnet_id": "sub-src-1", "ip_address": "198.51.100.60"}],
                                         "security_group_ids": ["sg-src-default"], "device_id": "srv-src-eph01",
                                         "port_security_enabled": True, "allowed_address_pairs": []}
    return w


r = app(eph_world(), [("plan", "eph01", PLAIN), ("migrate", "eph01", PLAIN)], env=menv(MAP + ",*:rbd-ssd"))
ok("T7 ephemeral root with *:rbd-ssd: 'volume create eph01-vda --image ... --type rbd-ssd', checked in "
   "volumes-ssd, volume-types 'ephemeral-root rbd-ssd volumes-ssd', plan INFO 'root disk volume -> ... (VOLUME_TYPE_MAP "
   "(*))', 1 mapped",
   r.rc == [0, 0] and any(c.startswith("dst\tvolume create eph01-vda --image ") and " --type rbd-ssd " in c
                          for c in r.calls)
   and rbd_info(r.calls, "volumes-ssd", "vol-dst-0001")
   and read(os.path.join(r.state, "eph01", "volume-types")) == "ephemeral-root rbd-ssd volumes-ssd\n"
   and "  INFO  root disk volume -> destination type rbd-ssd pool volumes-ssd (VOLUME_TYPE_MAP (*))"
   in r.outs[0] and "  INFO  VOLUME_TYPE_MAP in use: 1 volume(s) mapped" in r.outs[0]
   and jload(os.path.join(r.state, "eph01", "options.json")).get("effective", {}).get("volume_types", {}).get("value")
   == {"ephemeral-root": {"type": "rbd-ssd", "pool": "volumes-ssd", "from": "VOLUME_TYPE_MAP (*)"}},
   (r.rc, [c for c in r.calls if "volume create" in c], r.err[-500:]))
r = app(eph_world(), [("plan", "eph01", PLAIN)], env=menv(MAP + ",__DEFAULT__:rbd-ssd"))
ok("T7 ephemeral root with __DEFAULT__:rbd-ssd (1.2.1: a type name, not the fallback): the run's type, no 'in use' "
   "line", r.rc == [0] and "  INFO  root disk volume -> destination type default pool volumes (default)" in r.out
   and "VOLUME_TYPE_MAP in use" not in r.out, (r.rc, r.out[-800:]))
r = app(eph_world(), [("plan", "eph01", PLAIN), ("migrate", "eph01", PLAIN)], env=menv())
ok("T7 ephemeral root without '*': the run's type (no --type), RBD_POOL; no 'in use' line, no WARN (no source "
   "type to name)",
   r.rc == [0, 0] and any(c.startswith("dst\tvolume create eph01-vda --image ") and "--type" not in c for c in r.calls)
   and rbd_info(r.calls, "volumes", "vol-dst-0001")
   and read(os.path.join(r.state, "eph01", "volume-types")) == "ephemeral-root - volumes\n"
   and "VOLUME_TYPE_MAP in use" not in r.outs[0] and "  WARN  source type" not in r.outs[0]
   and "  INFO  root disk volume -> destination type default pool volumes (default)" in r.outs[0],
   (r.rc, [c for c in r.calls if "volume create" in c], r.outs[0][-800:]))

# ================================ T8: migrate volume <vol> with the map ================================
VW = vtm_world()
VW["src"]["volumes"]["vol-src-data"]["status"] = "available"
r = app(VW, [("volume", "data", [])], env=menv())
vd = os.path.join(r.state, "vol-vol-src-data")
ok("T8 migrate volume data (ps-silver): header type=rbd-hdd pool=volumes, the INFO line, created with --type rbd-hdd, "
   "checked and copied in pool volumes, volume-types written",
   r.rc == [0] and "Destination preflight (migrate-volume type=rbd-hdd pool=volumes):" in r.out
   and (INFO_DATA % ("rbd-hdd", "volumes")) + " (VOLUME_TYPE_MAP (ps-silver))" in r.out
   and "  INFO  VOLUME_TYPE_MAP in use: 1 volume(s) mapped" in r.out
   and "  OK    volume type rbd-hdd (VOLUME_TYPE_MAP (ps-silver))" in r.out
   and "  OK    RBD access to pool volumes" in r.out
   and has(r.calls, "dst\tvolume create data --size 2 --description migrated from vol-src-data -f value -c id --type "
                    "rbd-hdd") and rbd_info(r.calls, "volumes", "vol-dst-0001")
   and any("rbd:volumes/volume-vol-dst-0001:" in c for c in r.calls if c.startswith("qemu-img\tconvert"))
   and read(os.path.join(vd, "volume-types")) == "vol-src-data rbd-hdd volumes\n"
   and read(os.path.join(vd, "volume-map")) == "vol-src-data vol-dst-0001\n", (r.rc, r.out[-1500:]))
VW2 = vtm_world()
VW2["src"]["volumes"]["vol-src-root"].update(status="available", name="rootvol")
r = app(VW2, [("volume", "rootvol", [])], env=menv())
ok("T8 migrate volume of the ps-gold volume: rbd-ssd in volumes-ssd (capacity and RBD access there)",
   r.rc == [0] and "Destination preflight (migrate-volume type=rbd-ssd pool=volumes-ssd):" in r.out
   and "  OK    Ceph pool volumes-ssd: 10 GB needed" in r.out and "  OK    RBD access to pool volumes-ssd" in r.out
   and rbd_info(r.calls, "volumes-ssd", "vol-dst-0001"), (r.rc, r.out[-1500:]))
VW3 = world()
VW3["src"]["volumes"]["vol-src-data"]["status"] = "available"
r = app(VW3, [("volume", "data", [])])
ok("T8 migrate volume with an empty map: no INFO volume line (1.1.1 printed none for a single volume)",
   r.rc == [0] and "source type" not in r.out and "VOLUME_TYPE_MAP" not in r.out, r.out[-1000:])

# ================================ T9: a malformed map stops before any cloud call ================================
for bad, why in (("a:", "entry 'a:' has no destination type"), (":b", "entry ':b' has no source type"),
                 ("a", "entry 'a' has no ':'"), ("a:x,a:y", "source type 'a' is mapped twice (a:x and a:y)")):
    r = app(vtm_world(), [("migrate", "app01", PLAIN), ("plan", "app01", PLAIN), ("volume", "data", []),
                          ("rollback", "app01"), mark, ("doctor",)], env=menv(bad))
    ok("T9 VOLUME_TYPE_MAP=%r: migrate, plan, volume and rollback die 'ERROR: VOLUME_TYPE_MAP: %s' (exit 1) and the "
       "call log is empty up to then (no openstack, rbd or ceph call)" % (bad, why),
       r.rc[:4] == [1, 1, 1, 1] and all(e.count("ERROR: VOLUME_TYPE_MAP: " + why) == 1 for e in r.errs[:4])
       and r.calls[:1] == ["#rollback"], (r.rc, r.errs[:4], r.calls[:3]))
    ok("T9 VOLUME_TYPE_MAP=%r: doctor FAILs its conf line with the reason and still runs its other checks" % bad,
       "  FAIL  conf " in r.outs[4] and ("VOLUME_TYPE_MAP: " + why) in r.outs[4] and "  OK    RBD access to pool"
       in r.outs[4] and "Traceback" not in r.text, (r.rc, r.outs[4][-1200:], r.errs[4][-300:]))

# ================================ T10: Ceph capacity per pool ================================
w = vtm_world()
w["dst"]["ceph_pools"] = {"volumes-ssd": {"max_avail": 5 << 30, "stored": 0, "quota_bytes": 0},
                          "volumes": {"max_avail": 1 << 40, "stored": 0, "quota_bytes": 0}}
r = app(w, [("plan", "app01", PLAIN)], env=menv())
ok("T10 capacity per pool: volumes-ssd (5 GB left) FAILs for its 10 GB, volumes (1 TB) OK for its 3 GB; one 'ceph df'",
   r.rc == [1] and "  FAIL  Ceph pool volumes-ssd: 10 GB needed, 5 GB left (MAX AVAIL)" in r.out
   and "  OK    Ceph pool volumes: 3 GB needed, 1024 GB left (MAX AVAIL)" in r.out
   and sum(1 for c in r.calls if c.startswith("ceph\t")) == 1, (r.rc, r.out[-1200:]))
w["dst"]["ceph_pools"] = {"volumes": {"max_avail": 1 << 40, "stored": 0, "quota_bytes": 0}}
r = app(w, [("plan", "app01", PLAIN)], env=menv())
ok("T10 a pool missing from 'ceph df': its INFO line, the other pool still checked",
   "  INFO  Ceph pool capacity not checked: pool volumes-ssd is not in the 'ceph df' output" in r.out
   and "  OK    Ceph pool volumes: 3 GB needed" in r.out, r.out[-1200:])
w["dst"]["ceph_fail"] = "Error EACCES: access denied"
r = app(w, [("plan", "app01", PLAIN)], env=menv())
ok("T10 'ceph df' refused: one INFO line, as in 1.1.1",
   r.out.count("Ceph pool capacity not checked: Error EACCES: access denied") == 1, r.out[-1200:])
w = vtm_world()
w["dst"]["quota"] = {"volumes": [10, 0, 0], "gigabytes": [1000, 0, 0], "volumes_rbd-ssd": [5, 0, 0],
                     "gigabytes_rbd-ssd": [5, 0, 0], "volumes_rbd-hdd": [5, 0, 0], "gigabytes_rbd-hdd": [100, 0, 0],
                     "instances": [10, 0, 0], "cores": [20, 0, 0], "ram": [51200, 0, 0], "ports": [50, 0, 0]}
r = app(w, [("plan", "app01", PLAIN)], env=menv())
ok("T10 per-type quota by the mapped type: gigabytes_rbd-ssd needs 10 (root) and FAILs at 5; rbd-hdd needs 1 volume, "
   "2 GB (data only: data2 is the default type)",
   r.rc == [1] and "  FAIL  destination quota: gigabytes_rbd-ssd need 10, 5 free (used 0 of 5)" in r.out
   and "volumes_rbd-hdd" not in [l for l in r.out.splitlines() if "FAIL" in l], (r.rc, r.out[-1500:]))

# ================================ T11-T16: gone() takes a 404 that names the object anywhere on the line ================================
from migratetool import rollback  # noqa: E402

U = "a03980c4-1faf-4ec6-b6de-a46947f50b25"
V = "9b1c2d3e-0000-4ec6-b6de-a46947f50b99"
CASES = (
    ("T11", "Not Found (HTTP 404) (Request-ID: req-5f0c) http://172.20.40.4:8776/v3/0a1b2c/volumes/%s" % U, U, True),
    ("T12", "No Volume found for %s" % U, U, True),
    ("T13", "public endpoint for compute service in RegionOne region not found", U, False),
    ("T14", "Not Found (HTTP 404) (Request-ID: req-5f0c) http://172.20.40.4:8776/v3/0a1b2c/volumes/%s" % V, U, False),
    ("T15", "volume %s is in state 404-pending" % U, U, False),
    ("T16", "Unable to establish connection to http://172.20.40.4:8776/v3/0a1b2c/volumes/%s: timed out" % U, U, False),
)
for tid, msg, oid, want in CASES:
    ok("%s gone(%r) is %s" % (tid, msg[:90], want), rollback.gone(msg, oid) is want, rollback.gone(msg, oid))
more = [("404 Not Found: volume %s" % U, U, True), ("volume %s: 404" % U, U, True), ("volume %s: 404." % U, U, True),
        ("GET http://10.4.0.1:8776/v2/404abc/volumes/%s failed" % U, U, False),
        ("server %s answered 4040 bytes" % U, U, False), ("server %s: HTTP 404 not found" % U, U, True)]
ok("T11-T16 more shapes: a 404 token of its own followed by Not Found or the end (with a dot: _ENDS_404 of 1.1.1) "
   "is gone; /v2/404abc and 4040 are not",
   all(rollback.gone(m, o) is w_ for m, o, w_ in more), [(m, rollback.gone(m, o)) for m, o, w_ in more])

# ================================ T17-T20: the destination ports and images of a rollback (R3) ================================
FORBID = "Policy doesn't allow get_port to be performed. (HTTP 403) (Request-ID: req-77)"


def dst_ports(root):
    return [p for p in (read(os.path.join(root, "app01", "dst-ports")) or "").split("\n") if p]


SEEN = {}


def keep_ports(_wf, root):
    SEEN["ports"] = dst_ports(root)


def src_state(r):
    s = r.world["src"]["servers"].get(SID, {})
    return s.get("status"), s.get("locked")


r = app(world(), [("migrate", "app01", PLAIN), keep_ports, edit(lambda x: x["dst"].update(fail={"port show": FORBID})),
                  mark, ROLL])
pid = (SEEN.get("ports") or ["?"])[0]
ok("T17 'port show' of the destination port fails (403): 'WARN cannot read destination port <id> (<why>): left, "
   "check it by hand', no 'port delete', the rollback goes on: exit 0, the source ACTIVE, unlocked, its port back",
   r.rc == [0, 0] and "WARN cannot read destination port %s (%s): left, check it by hand" % (pid, FORBID) in r.out
   and not has(after_mark(r.calls), "dst\tport delete") and pid in r.world["dst"]["ports"]
   and src_state(r) == ("ACTIVE", False) and "src\tserver add port %s port-src-1" % SID in after_mark(r.calls)
   and retired(r, "app01"), (r.rc, pid, r.out[-800:], r.err[-400:]))
ok("T17 ... the legacy name sweep (mig-app01-0) gets the same WARN, not silence",
   "WARN cannot read destination port mig-app01-0 (%s): left, check it by hand" % FORBID in r.out, r.out[-800:])
r = app(world(), [("migrate", "app01", PLAIN), keep_ports, mark, ROLL])
ok("T17 control: a readable destination port is deleted as before ('deleted destination port <id>'), no WARN, and "
   "the sweep of mig-app01-0 (no such port) is silent",
   r.rc == [0, 0] and "deleted destination port %s" % SEEN["ports"][0] in r.out and "WARN" not in r.out
   and "mig-app01-0" not in r.out, (r.rc, r.out[-600:]))
NF = "Not Found (HTTP 404) (Request-ID: req-88) http://dst:9696/v2.0/ports/%s"
r = app(world(), [("migrate", "app01", PLAIN), keep_ports,
                  lambda wf, root: edit(lambda x: x["dst"].update(fail={"port show " + dst_ports(root)[0]:
                                                                        NF % dst_ports(root)[0]}))(wf, root),
                  mark, ROLL])
pid = SEEN["ports"][0]
ok("T18 'port show' answers a 404 naming the port (R1 shape): 'INFO destination port <id> already gone', no "
   "'port delete', exit 0",
   r.rc == [0, 0] and "INFO destination port %s already gone" % pid in r.out
   and not has(after_mark(r.calls), "dst\tport delete %s" % pid) and retired(r, "app01"), (r.rc, r.out[-600:]))
CONFLICT = "Conflict (HTTP 409) (Request-ID: req-99): port is in use"
r = app(world(), [("migrate", "app01", PLAIN), keep_ports,
                  edit(lambda x: x["dst"].update(fail={"port delete": CONFLICT})), mark, ROLL])
pid = SEEN["ports"][0]
ok("T19 'port delete' fails: 'WARN cannot delete destination port <id> (<why>): delete it by hand', the rollback "
   "goes on: volumes deleted, the source back, exit 0",
   r.rc == [0, 0] and "WARN cannot delete destination port %s (%s): delete it by hand" % (pid, CONFLICT) in r.out
   and has(after_mark(r.calls), "dst\tport delete %s" % pid) and not r.world["dst"]["volumes"]
   and src_state(r) == ("ACTIVE", False) and retired(r, "app01"), (r.rc, r.out[-600:], r.err[-300:]))
IMG503 = "Service Unavailable (HTTP 503)"
r = app(eph_world(), [("migrate", "eph01", PLAIN + ["--ephemeral-as-image"]),
                      edit(lambda x: x["dst"].update(fail={"image list": IMG503})), mark, ("rollback", "eph01")])
dimg = [c.split()[-1] for c in after_mark(r.calls) if c.startswith("dst\timage delete ")]
ok("T20 destination 'image list' fails: 'WARN cannot list destination images named mig-snap-eph01: skipped', the "
   "recorded dst-image-id still deleted, exit 0",
   r.rc == [0, 0] and "WARN cannot list destination images named mig-snap-eph01: skipped" in r.out
   and len(dimg) == 1 and dimg[0] not in r.world["dst"].get("images", {}) and retired(r, "eph01"),
   (r.rc, dimg, r.out[-600:], r.err[-300:]))
r = app(eph_world(), [("migrate", "eph01", PLAIN + ["--ephemeral-as-image"]), mark, ("rollback", "eph01")])
ok("T20 control: the listing answers: no WARN, the image deleted once (recorded and listed are the same ID)",
   r.rc == [0, 0] and "cannot list destination images" not in r.out
   and len([c for c in after_mark(r.calls) if c.startswith("dst\timage delete ")]) >= 1
   and not r.world["dst"].get("images"), (r.rc, r.out[-400:]))

# ================================ T22: rollback with an application credential of a state scoped by its record ================================
SCOPE = {"env": {"SRC_PROJECT": "psrc", "DST_PROJECT": "pdst"}}


def set_env(**kv):
    def step(_wf, _root):
        for k, v in kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return step


def appcred(side):
    """The openrc of that side becomes an application credential (after the migrate, which used a password openrc)."""
    def step(_wf, _root):
        with open(os.environ[side.upper() + "_RC"], "a") as f:
            f.write("export OS_AUTH_TYPE=v3applicationcredential\nexport OS_APPLICATION_CREDENTIAL_ID=ac1\n")
        inproc.clouds._rc_env_cache.clear()
    return step


NEW = ("this state was written by a run scoped with SRC_PROJECT=psrc DST_PROJECT=pdst (a password openrc); %s is an "
       "application credential, which cannot be rescoped: run the rollback with the password openrc that wrote the "
       "state; nothing was done")
for side in ("dst", "src"):
    r = app(world(), [("migrate", "app01", PLAIN), set_env(SRC_PROJECT=None, DST_PROJECT=None), appcred(side), mark,
                      ROLL], env=SCOPE)
    ok("T22 rollback of a state scoped psrc/pdst (restored from its record) with a %s application credential: exit 1 "
       "with the new sentence naming that openrc, not 'needs a password openrc'; nothing called after the scope" % side,
       r.rc == [0, 1] and NEW % os.environ[side.upper() + "_RC"] in r.err and "needs a password openrc" not in r.err
       and not [c for c in after_mark(r.calls) if c.startswith(("src\t", "dst\t"))]
       and os.path.isfile(os.path.join(r.state, "app01", "volume-map")), (r.rc, r.err[-600:], after_mark(r.calls)[:4]))
r = app(world(), [("migrate", "app01", PLAIN), appcred("dst"), mark, ROLL], env=SCOPE)
ok("T22 ... the same with SRC_PROJECT/DST_PROJECT set as recorded (origin record): the new sentence too",
   r.rc == [0, 1] and NEW % os.environ["DST_RC"] in r.err, (r.rc, r.err[-500:]))
r = app(world(), [appcred("dst"), ("migrate", "app01", PLAIN)], env=SCOPE)
ok("T22 migrate with an application credential and SRC_PROJECT/DST_PROJECT: the old sentence (env scope)",
   r.rc == [1] and "SRC_PROJECT/DST_PROJECT needs a password openrc: an application credential is bound to one "
   "project" in r.err and "this state was written" not in r.err, (r.rc, r.err[-400:]))

# ================================ T23: doctor checks the map's destination types ================================
w = vtm_world()
r = app(w, [("doctor",)], env=menv("ps-gold:rbd-nvme,ps-silver:rbd-hdd,*:rbd-hdd",
                                   RBD_POOL_MAP="rbd-hdd:volumes"))
ok("T23 doctor: 'FAIL VOLUME_TYPE_MAP destination type rbd-nvme (for ps-gold) is not a volume type of the "
   "destination', 'OK ... rbd-hdd (for ps-silver, *)', INFO for a type RBD_POOL_MAP does not name; exit 1",
   r.rc == [1] and "  FAIL  VOLUME_TYPE_MAP destination type rbd-nvme (for ps-gold) is not a volume type of the "
   "destination" in r.out and "  OK    VOLUME_TYPE_MAP destination type rbd-hdd (for ps-silver, *)" in r.out
   and "  INFO  VOLUME_TYPE_MAP destination type rbd-nvme has no RBD_POOL_MAP entry: uses RBD_POOL volumes" in r.out
   and "rbd-hdd has no RBD_POOL_MAP entry" not in r.out
   and r.calls.count("dst\tvolume type list -f value -c Name") == 1, (r.rc, r.out[-1500:]))
r = app(w, [("doctor",)], env=menv())
ok("T23 doctor, every mapped type there: two OK lines, no FAIL of the map",
   "  OK    VOLUME_TYPE_MAP destination type rbd-ssd (for ps-gold)" in r.out
   and "  OK    VOLUME_TYPE_MAP destination type rbd-hdd (for ps-silver)" in r.out
   and "FAIL  VOLUME_TYPE_MAP" not in r.out, r.out[-1500:])
r = app(world(), [("doctor",)])
ok("T23 doctor with an empty map: no VOLUME_TYPE_MAP line, no 'volume type list'",
   "VOLUME_TYPE_MAP" not in r.out and "dst\tvolume type list -f value -c Name" not in r.calls, r.out[-800:])
w = vtm_world()
w["dst"]["fail"] = {"volume type list": "Service Unavailable (HTTP 503)"}
r = app(w, [("doctor",)], env=menv())
ok("T23 doctor, the listing fails: one FAIL 'VOLUME_TYPE_MAP not checked: cannot list the destination volume types'",
   "  FAIL  VOLUME_TYPE_MAP not checked: cannot list the destination volume types (Service Unavailable (HTTP 503))"
   in r.out, r.out[-800:])

# ================================ T25: __DEFAULT__ is a source type name, '*' the fallback (1.2.1) ================================
def dflt_world():
    """T25/T26: root of Cinder's default type '__DEFAULT__' 10 GB, data ps-silver 2 GB, data2 without a type 1 GB."""
    w = vtm_world()
    w["src"]["volumes"]["vol-src-root"]["type"] = "__DEFAULT__"
    w["src"]["volumes"]["vol-src-data2"].pop("type")
    return w


INFO_DROOT = "  INFO  volume vol-src-root (no name) source type __DEFAULT__ -> destination type %s pool %s"
INFO_NODATA2 = "  INFO  volume vol-src-data2 (data2) source type unknown -> destination type %s pool %s"
MAP_DS = "__DEFAULT__:rbd-hdd,*:rbd-ssd"
r = app(dflt_world(), [("plan", "app01", PLAIN), ("migrate", "app01", PLAIN)], env=menv(MAP_DS))
plan = r.outs[0]
ok("T25 map __DEFAULT__:rbd-hdd,*:rbd-ssd: the __DEFAULT__ volume -> rbd-hdd/volumes (VOLUME_TYPE_MAP (__DEFAULT__)), "
   "ps-silver and the volume without a type -> rbd-ssd/volumes-ssd (VOLUME_TYPE_MAP (*)), no WARN, 3 mapped",
   r.rc == [0, 0] and (INFO_DROOT % ("rbd-hdd", "volumes")) + " (VOLUME_TYPE_MAP (__DEFAULT__))" in plan
   and (INFO_DATA % ("rbd-ssd", "volumes-ssd")) + " (VOLUME_TYPE_MAP (*))" in plan
   and (INFO_NODATA2 % ("rbd-ssd", "volumes-ssd")) + " (VOLUME_TYPE_MAP (*))" in plan
   and "  WARN  source type" not in plan and "  INFO  VOLUME_TYPE_MAP in use: 3 volume(s) mapped" in plan
   and "  OK    volume type rbd-hdd (VOLUME_TYPE_MAP (__DEFAULT__))" in plan
   and "  OK    volume type rbd-ssd (VOLUME_TYPE_MAP (*))" in plan, (r.rc, plan[-2500:]))
ok("T25 migrate: created with --type rbd-hdd (root), --type rbd-ssd (data, data2); volume-types and "
   "effective.volume_types say so",
   has(r.calls, "dst\tvolume create app01-vda --size 10 --type rbd-hdd --description ")
   and has(r.calls, "dst\tvolume create app01-vdb --size 2 --type rbd-ssd --description ")
   and has(r.calls, "dst\tvolume create app01-vdc --size 1 --type rbd-ssd --description ")
   and rbd_info(r.calls, "volumes", "vol-dst-0001") and rbd_info(r.calls, "volumes-ssd", "vol-dst-0002")
   and rbd_info(r.calls, "volumes-ssd", "vol-dst-0003")
   and read(os.path.join(r.state, "app01", "volume-types")) == "vol-src-root rbd-hdd volumes\n"
                                                              "vol-src-data rbd-ssd volumes-ssd\n"
                                                              "vol-src-data2 rbd-ssd volumes-ssd\n"
   and jload(os.path.join(r.state, "app01", "options.json")).get("effective", {}).get("volume_types", {}).get("value")
   == {"vol-src-root": {"type": "rbd-hdd", "pool": "volumes", "from": "VOLUME_TYPE_MAP (__DEFAULT__)"},
       "vol-src-data": {"type": "rbd-ssd", "pool": "volumes-ssd", "from": "VOLUME_TYPE_MAP (*)"},
       "vol-src-data2": {"type": "rbd-ssd", "pool": "volumes-ssd", "from": "VOLUME_TYPE_MAP (*)"}},
   (r.rc, [c for c in r.calls if "volume create" in c], read(os.path.join(r.state, "app01", "volume-types"))))

# ================================ T26: __DEFAULT__ without '*' covers only the type of that name ================================
r = app(dflt_world(), [("plan", "app01", PLAIN), ("migrate", "app01", PLAIN)], env=menv("__DEFAULT__:rbd-hdd"))
plan = r.outs[0]
ok("T26 map __DEFAULT__:rbd-hdd without '*': ps-silver lands on the run's type (default) with \"WARN source type "
   "ps-silver has no entry in VOLUME_TYPE_MAP and no '*' entry: lands on default\" (the volume without a type too, "
   "as 'unknown'); the __DEFAULT__ volume -> rbd-hdd (VOLUME_TYPE_MAP (__DEFAULT__)); 1 mapped",
   r.rc == [0, 0] and (INFO_DROOT % ("rbd-hdd", "volumes")) + " (VOLUME_TYPE_MAP (__DEFAULT__))" in plan
   and (INFO_DATA % ("default", "volumes")) + " (default)" in plan
   and (INFO_NODATA2 % ("default", "volumes")) + " (default)" in plan
   and "  WARN  source type ps-silver has no entry in VOLUME_TYPE_MAP and no '*' entry: lands on default" in plan
   and "  WARN  source type unknown has no entry in VOLUME_TYPE_MAP and no '*' entry: lands on default" in plan
   and plan.count("  WARN  source type ") == 2 and "  INFO  VOLUME_TYPE_MAP in use: 1 volume(s) mapped" in plan,
   (r.rc, plan[-2500:]))
ok("T26 migrate: root created with --type rbd-hdd, data and data2 without --type (the Cinder default of the "
   "destination)",
   has(r.calls, "dst\tvolume create app01-vda --size 10 --type rbd-hdd --description ")
   and has(r.calls, "dst\tvolume create app01-vdb --size 2 --description ")
   and has(r.calls, "dst\tvolume create app01-vdc --size 1 --description "),
   (r.rc, [c for c in r.calls if "volume create" in c]))
r = app(dflt_world(), [("plan", "app01", PLAIN)],
        env=menv("__DEFAULT__:rbd-hdd", MIG_DEFAULT_OPTS="--volume-type rbd-ssd"))
ok("T26 ... with the run's type rbd-ssd (MIG_DEFAULT_OPTS): the WARN names it ('lands on rbd-ssd')",
   r.rc == [0] and "  WARN  source type ps-silver has no entry in VOLUME_TYPE_MAP and no '*' entry: lands on rbd-ssd"
   in r.out and (INFO_DATA % ("rbd-ssd", "volumes-ssd")) + " (MIG_DEFAULT_OPTS)" in r.out, r.out[-1500:])

# ================================ T27: '*' as a destination is refused before any cloud call ================================
for bad in ("a:*", "ps-gold:rbd-ssd,*:*"):
    r = app(vtm_world(), [("migrate", "app01", PLAIN), ("plan", "app01", PLAIN), ("volume", "data", []),
                          ("rollback", "app01"), mark, ("doctor",)], env=menv(bad))
    ok("T27 VOLUME_TYPE_MAP=%r: migrate, plan, volume and rollback die \"ERROR: VOLUME_TYPE_MAP: '*' is only a source "
       "key\" (exit 1), no cloud call; doctor FAILs its conf line with it" % bad,
       r.rc[:4] == [1, 1, 1, 1] and all(e.count("ERROR: VOLUME_TYPE_MAP: '*' is only a source key") == 1
                                         for e in r.errs[:4])
       and r.calls[:1] == ["#rollback"] and "  FAIL  conf " in r.outs[4]
       and "VOLUME_TYPE_MAP: '*' is only a source key" in r.outs[4] and "Traceback" not in r.text,
       (r.rc, r.errs[:4], r.calls[:3], r.outs[4][-800:]))

# ================================ T28: version 1.2.2 (T24 of 1.2.0 follows) ================================
import migratetool  # noqa: E402
import migui  # noqa: E402

ok("T28 version 1.2.2: migratetool, migui and pyproject agree",
   migratetool.__version__ == "1.2.2" and migui.__version__ == "1.2.2"
   and 'version = "1.2.2"' in (read(os.path.join(REPO, "pyproject.toml")) or ""), migratetool.__version__)

# ================================ T29: the Ceph capacity counts the ephemeral root disk (1.2.2, S1) ================================
GB = 1 << 30
w = eph_world(disk=20)
w["dst"]["ceph_pools"] = {"volumes": {"max_avail": 15 * GB, "stored": 0, "quota_bytes": 0}}
r = app(w, [("plan", "eph01", PLAIN)], env=menv(""))
ok("T29 ephemeral VM, root disk 20 GB, no volume, pool volumes 15 GB left: 'FAIL Ceph pool volumes: 20 GB needed, "
   "15 GB left (MAX AVAIL)' (up to 1.2.1: no Ceph pool line at all); exit 1",
   r.rc == [1] and "  FAIL  Ceph pool volumes: 20 GB needed, 15 GB left (MAX AVAIL)" in r.out
   and r.out.count("Ceph pool ") == 1 and sum(1 for c in r.calls if c.startswith("ceph\t")) == 1, (r.rc, r.out[-1500:]))
r = app(w, [("plan", "eph01", PLAIN + ["--ephemeral-as-image"])], env=menv(""))
ok("T29 ... with --ephemeral-as-image (the root stays an image): no Ceph pool line, no 'ceph df', as before",
   "Ceph pool" not in r.out and not has(r.calls, "ceph\t"), (r.rc, r.out[-1500:]))


def eph_data_world(t=None):
    """eph01 (root 20 GB) with one attached data volume of 2 GB (of source type t)."""
    w = eph_world(disk=20)
    w["src"]["volumes"]["vol-src-edata"] = {"name": "edata", "status": "in-use", "size": 2, "bootable": "false",
                                            "encrypted": False, "multiattach": False}
    if t:
        w["src"]["volumes"]["vol-src-edata"]["type"] = t
    w["src"]["servers"]["srv-src-eph01"]["volumes"] = [{"Volume ID": "vol-src-edata", "Device": "/dev/vdb"}]
    return w


r = app(eph_data_world(), [("plan", "eph01", PLAIN)], env=menv(""))
ok("T29 ephemeral root 20 GB + a 2 GB data volume, both in pool volumes: one line 'OK Ceph pool volumes: 22 GB "
   "needed'", "  OK    Ceph pool volumes: 22 GB needed, 1024 GB left (MAX AVAIL)" in r.out
   and r.out.count("Ceph pool ") == 1, (r.rc, r.out[-1500:]))
r = app(eph_data_world("ps-silver"), [("plan", "eph01", PLAIN)], env=menv(MAP + ",*:rbd-ssd"))
ok("T29 with the map (ps-silver:rbd-hdd, *:rbd-ssd): the root counts in the pool of its volume (volumes-ssd 20 GB), "
   "the data volume in its own (volumes 2 GB)",
   "  OK    Ceph pool volumes-ssd: 20 GB needed, " in r.out
   and "  OK    Ceph pool volumes: 2 GB needed, " in r.out and r.out.count("Ceph pool ") == 2, (r.rc, r.out[-1500:]))
w = eph_world(disk=20)
w["dst"]["quota"] = {"volumes": [10, 0, 0], "gigabytes": [1000, 0, 0], "instances": [10, 0, 0], "cores": [20, 0, 0],
                     "ram": [51200, 0, 0], "ports": [50, 0, 0]}
w["dst"]["ceph_pools"] = {"volumes": {"max_avail": 15 * GB, "stored": 0, "quota_bytes": 0}}
r = app(w, [("plan", "eph01", PLAIN)], env=menv(""))
ok("T29 the quota and the Ceph pool count the same root disk (gigabytes 20/1000 on the quota line, 20 GB in the pool)",
   "  FAIL  Ceph pool volumes: 20 GB needed, 15 GB left (MAX AVAIL)" in r.out
   and "gigabytes 20/1000" in r.out, (r.rc, r.out[-1500:]))


# ================================ T30: doctor with a conf that does not parse (1.2.2, S5) ================================
def conf_step(text, iscsi_env=None):
    """The conf file holds text; MIG_ISCSI_IP comes from the environment only when iscsi_env is given."""
    def step(_wf, root):
        path = os.path.join(os.path.dirname(root), "migrate.conf")
        with open(path, "w") as f:
            f.write(text)
        os.environ["MIG_CONF"] = path
        if iscsi_env is None:
            os.environ.pop("MIG_ISCSI_IP", None)
        else:
            os.environ["MIG_ISCSI_IP"] = iscsi_env
    return step


BADCONF = 'MIG_ISCSI_IP=127.0.0.1\nVOLUME_TYPE_MAP="a"\n'
NOTLOADED = ("  INFO  conf %s not loaded: the checks below use the environment and the built-in defaults only; "
             "fix the conf first")
r = app(world(), [conf_step(BADCONF), ("doctor",)])
cpath = os.path.join(r.dir, "migrate.conf")
ok("T30 conf with MIG_ISCSI_IP and VOLUME_TYPE_MAP=\"a\": doctor exit 1, FAIL on the conf line, then the new INFO "
   "line, 'INFO MIG_ISCSI_IP not checked: conf not loaded' and no 'FAIL MIG_ISCSI_IP is not set'; the other checks "
   "still run",
   r.rc == [1] and ("  FAIL  conf %s: VOLUME_TYPE_MAP: entry 'a' has no ':'" % cpath) in r.out
   and (NOTLOADED % cpath) in r.out
   and r.out.index("  FAIL  conf ") < r.out.index(NOTLOADED % cpath)
   and "  INFO  MIG_ISCSI_IP not checked: conf not loaded" in r.out
   and "MIG_ISCSI_IP is not set" not in r.out and "  OK    RBD access to pool" in r.out
   and "Traceback" not in r.text, (r.rc, r.out[-2500:], r.err[-300:]))
r = app(world(), [conf_step(BADCONF, iscsi_env="127.0.0.1"), ("doctor",)])
ok("T30 ... MIG_ISCSI_IP from the environment is checked as usual (no 'not checked' line)",
   r.rc == [1] and (NOTLOADED % os.path.join(r.dir, "migrate.conf")) in r.out
   and "MIG_ISCSI_IP not checked" not in r.out and "MIG_ISCSI_IP 127.0.0.1" in r.out, (r.rc, r.out[-2000:]))
r = app(world(), [conf_step("MIG_ISCSI_IP=127.0.0.1\n"), ("doctor",)])
ok("T30 control: a conf that parses: 'OK conf <path>', no 'not loaded' line, MIG_ISCSI_IP from the conf checked",
   ("  OK    conf %s" % os.path.join(r.dir, "migrate.conf")) in r.out and "not loaded" not in r.out
   and "MIG_ISCSI_IP not checked" not in r.out and "MIG_ISCSI_IP is not set" not in r.out
   and "MIG_ISCSI_IP 127.0.0.1" in r.out, (r.rc, r.out[-2000:]))
r = app(world(), [conf_step(BADCONF), ("cli", ["doctor", "--json"])])
try:
    dj = json.loads(r.out)
except ValueError:
    dj = {}
ok("T30 doctor --json: the two INFO lines are checks of level INFO, no FAIL naming MIG_ISCSI_IP",
   {"level": "INFO", "text": "MIG_ISCSI_IP not checked: conf not loaded"} in dj.get("checks", [])
   and {"level": "INFO", "text": (NOTLOADED % os.path.join(r.dir, "migrate.conf"))[8:]} in dj.get("checks", [])
   and not [c for c in dj.get("checks", []) if c.get("level") == "FAIL" and "MIG_ISCSI_IP" in c.get("text", "")],
   (r.rc, r.out[-1500:], r.err[-800:]))

# ================================ config.parse_type_map and Options.type_for ================================
from migratetool import config  # noqa: E402
from migratetool.options import Options  # noqa: E402
from migratetool.util import Die  # noqa: E402

ok("parse_type_map: order kept, '' -> {}, '*' and __DEFAULT__ keys like the others, ':' split once; "
   "DEFAULT_TYPE_KEY is '*' (1.2.1)",
   list(config.parse_type_map("b:x,a:y,__DEFAULT__:z,*:w").items())
   == [("b", "x"), ("a", "y"), ("__DEFAULT__", "z"), ("*", "w")]
   and config.parse_type_map("") == {} and config.parse_type_map("a:b:c") == {"a": "b:c"}
   and config.parse_type_map("a:b,") == {"a": "b"} and config.DEFAULT_TYPE_KEY == "*"
   and config.parse_type_map("a:__DEFAULT__") == {"a": "__DEFAULT__"}, "")
bad = []
for text in ("a:", ":b", "a", "a:x,a:y", "a:b,,c:d", "*:x,*:y", "a:*", "*:*"):
    try:
        config.parse_type_map(text)
        bad.append(text)
    except Die as e:
        if not str(e).startswith("VOLUME_TYPE_MAP: ") or (text.endswith(":*") and
                                                           str(e) != "VOLUME_TYPE_MAP: '*' is only a source key"):
            bad.append((text, str(e)))
ok("parse_type_map: a:, :b, a, a twice, an empty entry in the middle, '*' twice: Die 'VOLUME_TYPE_MAP: ...'; "
   "a:* and *:* (T27): Die \"VOLUME_TYPE_MAP: '*' is only a source key\"", not bad, bad)


class _Cfg:
    def __init__(self, **v):
        self.values = dict({"VOLUME_TYPE_MAP": "", "RBD_POOL": "p0", "RBD_POOL_MAP": ""}, **v)

    def __getattr__(self, k):
        try:
            return self.__dict__["values"][k]
        except KeyError:
            raise AttributeError(k)


o = Options()
o.volume_type, o.type_from = "az-t", "AZ_TYPE_MAP (AZ1)"
cf = _Cfg(VOLUME_TYPE_MAP="g:t1,*:t9", RBD_POOL_MAP="t1:p1,az-t:p2")
ok("type_for: map by source type, '*' for others and for '', the run's type (AZ_TYPE_MAP) only without them; "
   "pool_for by RBD_POOL_MAP of that type",
   o.type_for("g", cf) == ("t1", "VOLUME_TYPE_MAP (g)") and o.type_for("x", cf) == ("t9", "VOLUME_TYPE_MAP (*)")
   and o.type_for("", cf) == ("t9", "VOLUME_TYPE_MAP (*)")
   and o.type_for("x", _Cfg(VOLUME_TYPE_MAP="g:t1")) == ("az-t", "AZ_TYPE_MAP (AZ1)")
   and o.pool_for("t1", cf) == "p1" and o.pool_for("t9", cf) == "p0" and o.pool_for("", cf) == "p0"
   and o.target("g", cf) == ("t1", "p1", "VOLUME_TYPE_MAP (g)"), (o.type_for("g", cf), o.type_for("x", cf)))
cd = _Cfg(VOLUME_TYPE_MAP="__DEFAULT__:t5")
ok("type_for (1.2.1): __DEFAULT__ matches only the source type of that name; without '*' the others (and '') get "
   "the run's type",
   o.type_for("__DEFAULT__", cd) == ("t5", "VOLUME_TYPE_MAP (__DEFAULT__)")
   and o.type_for("x", cd) == ("az-t", "AZ_TYPE_MAP (AZ1)") and o.type_for("", cd) == ("az-t", "AZ_TYPE_MAP (AZ1)")
   and o.type_for("__DEFAULT__", _Cfg(VOLUME_TYPE_MAP="__DEFAULT__:t5,*:t9")) == ("t5", "VOLUME_TYPE_MAP (__DEFAULT__)"),
   (o.type_for("__DEFAULT__", cd), o.type_for("x", cd)))
o.type_cli = True
ok("type_for: a typed --volume-type wins for every volume", o.type_for("g", cf) == ("az-t", "cli"), o.type_for("g", cf))

# ================================ parity: v13_rule drops what 1.2.0 adds, nothing else ================================
import parity  # noqa: E402

TXT = ("Destination preflight (mode=cold ip=same type=default pool=mig-volumes):\n"
       "  INFO  VOLUME_TYPE_MAP in use: 2 volume(s) mapped\n"
       "  INFO  volume type rbd-hdd from cli overrides VOLUME_TYPE_MAP\n"
       "  OK    RBD access to pool volumes-ssd\n  OK    RBD access to pool volumes\n  OK    iSCSI daemon\n"
       "[app01]   OK    RBD access to pool a\n[app02]   OK    RBD access to pool a\n"
       "[10:00:00] INFO destination port p1 already gone\n"
       "[10:00:00] WARN cannot read destination port p2 (HTTP 403): left, check it by hand\n"
       "[10:00:00] WARN cannot delete destination port p3 (HTTP 409): delete it by hand\n"
       "[10:00:00] WARN cannot list destination images named mig-snap-app01: skipped\n"
       "[10:00:00] deleted destination port p4\n")
WANT = ("Destination preflight (mode=cold ip=same type=default pool=mig-volumes):\n"
        "  OK    RBD access to pool volumes-ssd\n  OK    iSCSI daemon\n"
        "[app01]   OK    RBD access to pool a\n[app02]   OK    RBD access to pool a\n"
        "[10:00:00] deleted destination port p4\n")
p = {"out": TXT, "err": TXT, "files": {"log/x.log": TXT, "app01/options": TXT, "app01/volume-types": "v - p\n",
                                       "app01.rolled-back.<epoch>/volume-types": "v - p\n", "app01/volume-map": "v d\n"}}
parity.v13_rule(p)
ok("v13_rule: the VOLUME_TYPE_MAP and R3 lines go from out, err and log files; a second RBD access line of the same "
   "VM goes, two VMs' lines stay; the volume-types state files go, the other state files stay as they are",
   p["out"] == p["err"] == p["files"]["log/x.log"] == WANT and p["files"]["app01/options"] == TXT
   and set(p["files"]) == {"log/x.log", "app01/options", "app01/volume-map"}, p)


def step_mark(n):
    def f(_wf, _root):
        with open(os.environ["HARNESS_CALLS"], "a") as fh:
            fh.write("#step\t%d\n" % n)
    return f


r = app(world(), [step_mark(0), ("migrate", "app01", PLAIN), step_mark(1), ROLL])
before = {"out": r.out, "err": r.err, "files": {"app01.rolled-back.<epoch>/volume-map": "x"}}
after = json.loads(json.dumps(before))
parity.v13_rule(after)
ok("v13_rule on a real empty-map migrate + rollback: out and err unchanged (no 1.2.0 line to drop)",
   r.rc == [0, 0] and after["out"] == before["out"] and after["err"] == before["err"], "")

shutil.rmtree(WORK, ignore_errors=True)
print("TOTAL pass=%d fail=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
