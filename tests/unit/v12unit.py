#!/usr/bin/env python3
"""1.1.1 unit checks: A21, a rollback takes a destination object (or a source port) as deleted only when the API says
that this very object is not found, and deletes nothing at the destination before the destination API has given one
valid answer in this run. Runs the app in this process against the fake clouds (inproc.py), like v11unit.py.

    python3 v12unit.py <directory holding migratetool/>
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
DSID = "srv-dst-0001"
WORK = tempfile.mkdtemp(prefix="v12unit-")
DST_DELETES = ("dst\tserver delete", "dst\tvolume delete", "dst\tport delete", "dst\timage delete")
SRC_GIVE_BACK = ("src\tserver unlock", "src\tserver add port", "src\tserver start")
# keystoneauth's EndpointNotFound when the catalog has no such service/region/interface (a wrong OS_REGION_NAME or
# OS_INTERFACE, a destination catalog changed during maintenance): 'not found', but about no object at all.
MSG = "public endpoint for compute service in RegionOne region not found"
MSG_VOL = "public endpoint for volumev3 service in RegionOne region not found"
MSG_NET = "public endpoint for network service in RegionOne region not found"
MSG_IMG = "public endpoint for image service in RegionOne region not found"


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


def dst_deletes(calls):
    return [c for c in after_mark(calls) if c.startswith(DST_DELETES)]


def give_back(calls):
    return [c for c in after_mark(calls) if c.startswith(SRC_GIVE_BACK)]


def last_err(r):
    return (r.err.strip().splitlines() or [""])[-1]


def retired(r, ident):
    return glob.glob(os.path.join(r.state, ident + ".rolled-back.*"))


def live(r, ident="app01"):
    return os.path.isfile(os.path.join(r.state, ident, "volume-map")) and not retired(r, ident)


def dst_catalog_broken(x):
    """Every destination call of the rollback answers like a catalog without the service in this region."""
    x["dst"]["fail"] = {"server delete": MSG, "server show": MSG, "server list": MSG,
                        "volume show": MSG_VOL, "volume delete": MSG_VOL, "port show": MSG_NET, "port delete": MSG_NET,
                        "image list": MSG_IMG, "image delete": MSG_IMG}


def src_state(r):
    s = r.world["src"]["servers"].get(SID, {})
    return s.get("status"), s.get("locked")


def untouched(r, label):
    """The checks of a refused rollback: nothing deleted at the destination, the source not given back."""
    ok(label + ": no 'server delete', 'volume delete', 'port delete' or 'image delete' at the destination",
       not dst_deletes(r.calls), dst_deletes(r.calls))
    ok(label + ": no 'server unlock', 'server add port' or 'server start' at the source",
       not give_back(r.calls), give_back(r.calls))
    ok(label + ": the destination server and both volumes are still there",
       DSID in r.world["dst"]["servers"] and {"vol-dst-0001", "vol-dst-0002"} <= set(r.world["dst"]["volumes"]),
       (sorted(r.world["dst"]["servers"]), sorted(r.world["dst"]["volumes"])))
    ok(label + ": the source VM stays SHUTOFF and locked, the state directory live (not retired)",
       src_state(r) == ("SHUTOFF", True) and live(r), (src_state(r), os.listdir(r.state)))


# ================================ case 1/2: the destination answers 'endpoint ... not found' ================================
r = app(world(), [("migrate", "app01", PLAIN), edit(dst_catalog_broken), mark, ROLL])
# Case 1 (the reproduction): what the rollback did, printed for the test report whatever the result.
print("INFO case 1 rc=%s last stderr line=%r dst servers=%s dst volumes=%s src=%s state live=%s retired=%s "
      "src give-back calls=%s" % (r.rc, last_err(r), sorted(r.world["dst"]["servers"]),
                                  sorted(r.world["dst"]["volumes"]), src_state(r), live(r),
                                  [os.path.basename(x) for x in retired(r, "app01")], give_back(r.calls)))
ok("A21 the destination answers 'public endpoint for compute service ... not found': the rollback stops (exit 1), "
   "'cannot read destination server', nothing deleted, the source not touched",
   r.rc == [0, 1] and "cannot read destination server %s (%s): nothing was deleted at the destination, the source was "
   "not touched; check the destination credentials/endpoint/DST_PROJECT and run the rollback again" % (DSID, MSG)
   in r.err, (r.rc, r.err[-600:]))
untouched(r, "A21 destination endpoint not found")
ok("A21 the destination probe is the first destination call of the rollback, after the source probe",
   [c for c in after_mark(r.calls) if c.startswith("dst\t")][:1] == ["dst\tserver show %s -f json -c id -c status" % DSID]
   and after_mark(r.calls).index("src\tserver show %s -f json -c id -c status" % SID)
   < after_mark(r.calls).index("dst\tserver show %s -f json -c id -c status" % DSID), after_mark(r.calls))

# ================================ case 3: the destination answers a bare 404 (no object named) ================================
r = app(world(), [("migrate", "app01", PLAIN),
                  edit(lambda x: x["dst"].update(fail={"server show": "Not Found (HTTP 404)"})), mark, ROLL])
ok("A21 'server show' at the destination answers 'Not Found (HTTP 404)' naming no object: 'cannot read destination "
   "server', exit 1",
   r.rc == [0, 1] and "cannot read destination server %s (Not Found (HTTP 404)): nothing was deleted" % DSID in r.err,
   (r.rc, r.err[-600:]))
untouched(r, "A21 destination bare 404")

shutil.rmtree(WORK, ignore_errors=True)
print("TOTAL pass=%d fail=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
