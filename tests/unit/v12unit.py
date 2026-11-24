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

# ================================ gone(): only an answer that names the object ================================
from migratetool import rollback  # noqa: E402
from migratetool.proc import Result  # noqa: E402
from migratetool.util import Die  # noqa: E402

U = "a03980c4-1faf-4ec6-b6de-a46947f50b25"
yes = [("No Server found for %s" % U, U), ("No Port found for port-src-1", "port-src-1"),
       ("No volume with a name or ID of 'vol-dst-0001' exists.", "vol-dst-0001"),
       ("ERROR (CommandError): No server with a name or ID of 'srv-dst-0001' exists.", "srv-dst-0001"),
       ("Instance %s could not be found. (HTTP 404) (Request-ID: req-1)" % U, U),
       ("Volume %s could not be found. (HTTP 404)" % U, U), ("Port %s not found (HTTP 404)" % U, U),
       ("NO SERVER FOUND FOR %s" % U.upper(), U), ("some warning\nNo Server found for %s\n" % U, U)]
no = [(MSG, DSID), (MSG_VOL, "vol-dst-0001"), ("openstack: command not found", DSID), ("Not Found (HTTP 404)", DSID),
      ("404 Not Found: the resource could not be found.", DSID), ("No Server found for srv-dst-00012", DSID),
      ("No Server found for %s" % U, ""), ("No Server found for %s" % U, None),
      ("Internal Server Error (HTTP 500) for %s" % DSID, DSID), ("No Server found for xsrv-dst-0001", DSID),
      ("Service Unavailable (HTTP 503)", DSID), ("", DSID)]
ok("A21 gone(): 'No <Kind> found for <id>', \"No <kind> with a name or ID of '<id>' exists\", '<id> could not be "
   "found', '<id> ... (HTTP 404)' are gone, any case, on any line",
   all(rollback.gone(m, o) for m, o in yes), [m for m, o in yes if not rollback.gone(m, o)])
ok("A21 gone(): 'endpoint ... not found', 'command not found', a 404 naming nothing, another id, an empty id, a 500 "
   "or 503 naming the id are not gone",
   not any(rollback.gone(m, o) for m, o in no), [m for m, o in no if rollback.gone(m, o)])
ok("A21 GONE_RE no longer exists in the engine (gone() everywhere)", not hasattr(rollback, "GONE_RE"))


class _Cloud:
    """cloud.run answering from a list (the last answer repeats); counts the calls."""
    def __init__(self, answers, side="destination"):
        self.answers, self.calls, self.side = list(answers), 0, side

    def run(self, *a, **k):
        self.calls += 1
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


gc = _Cloud([Result(1, "", MSG + "\n")])
try:
    rollback.wait_gone(None, gc, "server", DSID, "destination server")
    ok("A21 wait_gone: every 'show' fails naming nothing: dies after READ_ERRORS (5) reads, not after 600 s", False,
       "returned")
except Die as err:
    ok("A21 wait_gone: every 'show' fails naming nothing: dies after READ_ERRORS (5) reads, not after 600 s",
       gc.calls == 5 and str(err) == "cannot read destination server %s after its delete (%s): cleanup not complete; "
       "check the destination credentials/endpoint and run the rollback again" % (DSID, MSG), (gc.calls, str(err)))
gc = _Cloud([Result(1, "", MSG)] * 4 + [Result(0, '{"id": "%s", "status": "DELETED"}' % DSID, "")]
            + [Result(1, "", MSG)] * 4 + [Result(1, "", "No Server found for %s" % DSID)])
try:
    rollback.wait_gone(None, gc, "server", DSID, "destination server")
    ok("A21 wait_gone: read errors that are not consecutive (an answer in between) go on; gone at the 10th read",
       gc.calls == 10, gc.calls)
except Die as err:
    ok("A21 wait_gone: read errors that are not consecutive (an answer in between) go on; gone at the 10th read",
       False, str(err))
gc = _Cloud([Result(1, "", "Service Unavailable (HTTP 503)")], side="source")
try:
    rollback.wait_gone(None, gc, "volume", "c9", "leftover clone")
except Die as err:
    ok("A21 wait_gone on the source (a leftover clone) names the source credentials",
       "cannot read leftover clone c9 after its delete (Service Unavailable (HTTP 503)): cleanup not complete; check "
       "the source credentials/endpoint" in str(err), str(err))


def server_gone(x):
    """An earlier rollback deleted the destination server (its volumes are detached, available)."""
    x["dst"]["servers"].pop(DSID)
    for v in x["dst"]["volumes"].values():
        v["status"] = "available"


# ================================ case 4: the destination server is really gone (a rollback run again) ================================
for label, fn in (("'No Server found for <id>' (openstackclient 10.x)",
                   lambda x: (server_gone(x), x["dst"].update(fail={"server show %s" % DSID: "No Server found for " + DSID,
                                                                     "server delete %s" % DSID: "No Server found for " + DSID}))),
                  ("\"No server with a name or ID of '<id>' exists.\" (older client)", server_gone)):
    r = app(world(), [("migrate", "app01", PLAIN), edit(fn), mark, ROLL])
    ok("A21 the destination server is gone, %s: the probe takes it as a valid answer, the rollback goes on: volumes "
       "deleted, the source ACTIVE with its port, the state retired (exit 0)" % label,
       r.rc == [0, 0] and not r.world["dst"]["volumes"] and src_state(r) == ("ACTIVE", False) and retired(r, "app01")
       and "src\tserver add port %s port-src-1" % SID in after_mark(r.calls), (r.rc, r.err[-500:]))

# ================================ case 5: the server is deleted, then the volumes cannot be read ================================
r = app(world(), [("migrate", "app01", PLAIN),
                  edit(lambda x: x["dst"].update(fail={"volume show": MSG_VOL, "volume delete": MSG_VOL})), mark, ROLL,
                  edit(lambda x: x["dst"].pop("fail")), ROLL])
first = r.errs[1]
ok("A21 the server deleted, then 'volume show' answers 'endpoint ... not found': exit 1 at the volume, 'cannot read "
   "destination volume ... nothing more was deleted'",
   r.rc[:2] == [0, 1] and "cannot read destination volume vol-dst-0001: %s; nothing more was deleted" % MSG_VOL in first,
   (r.rc, first[-500:]))
rb = after_mark(r.calls)
cut = [i for i, c in enumerate(rb) if c == "src\tserver show %s -f json -c id -c status" % SID][1]
ok("A21 ... the first rollback did not touch the source (no unlock, port or start before the second run), the state "
   "stayed live",
   not [c for c in rb[:cut] if c.startswith(SRC_GIVE_BACK)] and "dst\tvolume delete" not in rb[:cut]
   and "nothing more was deleted" in first and "Traceback" not in first, rb[:cut])
ok("A21 ... the rollback run again (endpoint back) finishes: its first destination call is the probe (the server, "
   "now not found), volumes deleted, the source ACTIVE, the state retired",
   r.rc == [0, 1, 0] and [c for c in rb[cut:] if c.startswith("dst\t")][:1]
   == ["dst\tserver show %s -f json -c id -c status" % DSID]
   and not r.world["dst"]["servers"] and not r.world["dst"]["volumes"]
   and src_state(r) == ("ACTIVE", False) and retired(r, "app01"), (r.rc, r.errs[2][-500:]))

# ================================ case 6: 'server delete' OK, then every 'server show' fails to read ================================
r = app(world(), [("migrate", "app01", PLAIN),
                  edit(lambda x: x["dst"].update(fail_from={"server show %s" % DSID: [1, MSG]})), mark, ROLL])
shows = [c for c in after_mark(r.calls) if c.startswith("dst\tserver show %s" % DSID)]
ok("A21 the server delete is accepted, then every 'server show' fails naming nothing: exit 1 'cannot read destination "
   "server ... after its delete' within 6 'server show' (the probe + 5), not a 600 s wait",
   r.rc == [0, 1] and "cannot read destination server %s after its delete (%s): cleanup not complete; check the "
   "destination credentials/endpoint and run the rollback again" % (DSID, MSG) in r.err and len(shows) <= 6,
   (r.rc, len(shows), r.err[-500:]))
ok("A21 ... the source was not touched (no unlock, port or start), the state live",
   not give_back(r.calls) and src_state(r) == ("SHUTOFF", True) and live(r), (give_back(r.calls), src_state(r)))

# ================================ case 18: migrate-volume, the destination cannot be read ================================
VW = world()
VW["src"]["volumes"]["vol-src-data"]["status"] = "available"
VROLL = ("rollback", "vol-vol-src-data")
r = app(VW, [("volume", "data", []), edit(dst_catalog_broken), mark, VROLL])
ok("A21 volume: the destination answers 'endpoint ... not found': exit 1 'cannot read destination volume', the copy "
   "kept, no 'volume delete', the state live",
   r.rc == [0, 1] and "cannot read destination volume vol-dst-0001 (%s): nothing was deleted at the destination, the "
   "source was not touched" % MSG_VOL in r.err and "vol-dst-0001" in r.world["dst"]["volumes"]
   and not dst_deletes(r.calls) and not retired(r, "vol-vol-src-data")
   and os.path.isfile(os.path.join(r.state, "vol-vol-src-data", "volume.json")), (r.rc, r.err[-500:]))
r = app(VW, [("volume", "data", []), mark, VROLL])
ok("A21 volume: the destination readable: one probe of the copy, then the rollback as in 1.1.0",
   r.rc == [0, 0] and after_mark(r.calls).count("dst\tvolume show vol-dst-0001 -f json -c id -c status") >= 1
   and after_mark(r.calls).index("dst\tvolume show vol-dst-0001 -f json -c id -c status")
   < after_mark(r.calls).index("dst\tvolume delete vol-dst-0001")
   and "vol-dst-0001" not in r.world["dst"]["volumes"] and retired(r, "vol-vol-src-data"), (r.rc, r.err[-300:]))

# ================================ case 7/8: the source port at the end of the rollback ================================
r = app(world(), [("migrate", "app01", PLAIN), edit(lambda x: x["src"].update(fail={"port show": MSG_NET})), mark,
                  ROLL])
ok("A21 'port show' of the source port answers 'public endpoint for network service ... not found': retried, then "
   "exit 1 'cannot read source port port-src-1'; never taken as 'no longer exists'",
   r.rc == [0, 1] and "cannot read source port port-src-1 (%s): its NIC was not given back" % MSG_NET in r.err
   and "no longer exists" not in r.text, (r.rc, r.err[-500:]))
ok("A21 ... no 'server add port', no start, the state not retired",
   not [c for c in after_mark(r.calls) if c.startswith(("src\tserver add port", "src\tserver start"))]
   and not retired(r, "app01"), give_back(r.calls))
nova_port = edit(lambda x: x["src"]["ports"]["port-src-1"].update(nova_owned=True))
r = app(world(), [nova_port, ("migrate", "app01", PLAIN), mark, ROLL])
reserve = [c.split()[-1] for c in after_mark(r.calls) if c.startswith("src\tserver add port %s " % SID)]
ok("A21 the source port is really gone ('No Port found for port-src-1', Nova deleted its own port): 'port port-src-1 "
   "no longer exists', its reserve port reattached, exit 0, the state retired",
   r.rc == [0, 0] and "port port-src-1 no longer exists (Nova deleted it; its reserve port is reattached instead)"
   in r.out and reserve and reserve != ["port-src-1"] and retired(r, "app01")
   and src_state(r) == ("ACTIVE", False), (r.rc, reserve, r.err[-500:]))

# ================================ case 9: detach_source_ports reads the port with a broken catalog ================================
r = app(world(), [edit(lambda x: x["src"].update(fail={"port show port-src-1 -f value -c device_id": MSG_NET})),
                  ("migrate", "app01", PLAIN)])
ok("A21 migrate: 'port show' after the detach answers 'endpoint ... not found': exit 4 'cannot read port port-src-1 "
   "after detach', no reserve port created",
   r.rc == [4] and "cannot read port port-src-1 after detach: %s" % MSG_NET in r.err
   and not [c for c in r.calls if c.startswith("src\tport create")], (r.rc, r.err[-500:]))
r = app(world(), [nova_port, ("migrate", "app01", PLAIN)])
ok("A21 migrate: a port Nova really deleted ('No Port found for port-src-1') still gets its reserve port (as 1.1.0)",
   r.rc == [0] and [c for c in r.calls if c.startswith("src\tport create mig-reserve-app01-0 ")], (r.rc, r.err[-300:]))

shutil.rmtree(WORK, ignore_errors=True)
print("TOTAL pass=%d fail=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
