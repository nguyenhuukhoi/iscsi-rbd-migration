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

# ================================ case 10: rollback --dry-run shows dst_error ================================
DRY = ("cli", ["rollback", "app01", "--dry-run", "--json"])
TEXT = ("cli", ["rollback", "app01", "--dry-run"])


def dry_json(r, i):
    try:
        return json.loads(r.outs[i])
    except ValueError:
        return {}


r = app(world(), [("migrate", "app01", PLAIN), edit(dst_catalog_broken), mark, DRY, TEXT])
obj = dry_json(r, 1)
ok("A21 --dry-run --json, the destination cannot be read: dst_server_status null, dst_error the reason, exit 0",
   r.rc == [0, 0, 0] and "dst_server_status" in obj and obj["dst_server_status"] is None
   and obj.get("dst_error") == MSG and obj.get("dst_server_id") == DSID, (r.rc, obj))
ok("A21 --dry-run (text): a WARN line 'the destination server could not be read (<why>): the rollback refuses until "
   "it can'",
   "  WARN the destination server could not be read (%s): the rollback refuses until it can" % MSG in r.outs[2],
   r.outs[2])
ok("A21 the dry-runs deleted nothing and only read", not dst_deletes(r.calls) and not give_back(r.calls),
   after_mark(r.calls))
r = app(world(), [("migrate", "app01", PLAIN), DRY, TEXT, edit(server_gone), DRY, TEXT])
a_, b_ = dry_json(r, 1), dry_json(r, 3)
ok("A21 --dry-run, the destination read: dst_server_status ACTIVE, dst_error null, no destination WARN; once the "
   "server is gone: GONE, dst_error null, no WARN",
   r.rc == [0, 0, 0, 0, 0] and a_.get("dst_server_status") == "ACTIVE" and "dst_error" in a_
   and a_["dst_error"] is None and b_.get("dst_server_status") == "GONE" and b_.get("dst_error") is None
   and "WARN the destination server" not in r.outs[2] + r.outs[4]
   and "  destination server %s (GONE) is deleted" % DSID in r.outs[4], (r.rc, a_, b_, r.outs[4]))

# ================================ case 11/12: A6 asks the source first ================================
def drop_source_vm(x):
    x["src"]["servers"].pop(SID)


FAILED = world()
FAILED["dst"]["create_status"] = "error"  # the first run dies before its cutover: exit 3, volume-map, no t-stop
unfail = edit(lambda x: x["dst"].pop("create_status"))
r = app(FAILED, [("migrate", "app01", PLAIN), unfail, edit(drop_source_vm), mark, ("migrate", "app01", PLAIN)])
ok("A6+A20: an earlier run failed before the cutover and the source VM is gone: the re-run stops (exit 3) with the "
   "A20 message, 'this re-run deleted nothing; the earlier state stays live'",
   r.rc == [3, 3] and "source VM %s (app01) is not found at the source (deleted, not visible to the source "
   "credentials/SRC_PROJECT, or the wrong source endpoint/region): the destination (server none recorded, volumes "
   "vol-dst-0001) may be the only remaining copy; this re-run deleted nothing; the earlier state stays live." % SID
   in r.err and "failed before the cutover" not in r.out, (r.rc, r.err[-700:]))
ok("A6+A20: ... no 'volume delete' at the destination, the destination volume kept, the earlier state live (not "
   "retired)",
   not [c for c in after_mark(r.calls) if c.startswith("dst\tvolume delete")] and "vol-dst-0001" in r.world["dst"]["volumes"]
   and os.path.isfile(os.path.join(r.state, "app01", "volume-map")) and not retired(r, "app01"),
   (after_mark(r.calls), os.listdir(r.state)))
r = app(FAILED, [("migrate", "app01", PLAIN), unfail,
                 edit(lambda x: x["src"].update(fail={"server show %s -f json -c id -c status" % SID:
                                                      "Internal Server Error (HTTP 500)"})),
                 mark, ("migrate", "app01", PLAIN)])
ok("A6+A20: the source VM cannot be read: 'cannot read source VM ... this re-run deleted nothing; the earlier state "
   "stays live; check the source credentials/SRC_PROJECT and run migrate again', nothing deleted",
   r.rc == [3, 3] and "cannot read source VM %s (Internal Server Error (HTTP 500)): this re-run deleted nothing; the "
   "earlier state stays live; check the source credentials/SRC_PROJECT and run migrate again" % SID in r.err
   and not [c for c in after_mark(r.calls) if c.startswith("dst\tvolume delete")] and not retired(r, "app01"),
   (r.rc, r.err[-500:]))
r = app(FAILED, [("migrate", "app01", PLAIN), unfail, mark, ("migrate", "app01", PLAIN)])
rb = after_mark(r.calls)
probe = "src\tserver show %s -f json -c id -c status" % SID
ok("A6+A20: the source VM there: one probe before the clean-up, then as 1.1.0 (the recorded volume deleted, the "
   "earlier state retired, the run migrates, exit 0)",
   r.rc == [3, 0] and rb.count(probe) == 1 and rb.index(probe) < rb.index("dst\tvolume delete vol-dst-0001")
   and "failed before the cutover" in r.out and len(retired(r, "app01")) == 1 and r.world["dst"]["servers"],
   (r.rc, rb[:5], r.err[-400:]))

# ================================ case 13-15: the rollback runs in the scope the state was written with ================================
SCOPE = {"env": {"SRC_PROJECT": "psrc", "DST_PROJECT": "pdst"}}


def set_env(**kv):
    """A step that changes this process's environment for the steps after it (None: unset)."""
    def step(_wf, _root):
        for k, v in kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return step


unscope = set_env(SRC_PROJECT=None, DST_PROJECT=None)


def need_project(x):
    """The objects of each side live in the project the migrate was scoped to: a token scoped elsewhere sees nothing."""
    x["src"]["need_project"], x["dst"]["need_project"] = "psrc", "pdst"


def options_doc(root):
    with open(os.path.join(root, "app01", "options.json")) as f:
        return json.load(f)


def drop_projects_record(_wf, root):
    """A state written before the projects record existed (options.json without effective.projects)."""
    doc = options_doc(root)
    doc["effective"].pop("projects", None)
    p_ = os.path.join(root, "app01", "options.json")
    os.chmod(p_, 0o600)
    with open(p_, "w") as f:
        json.dump(doc, f)


SEEN = {}


def keep_record(_wf, root):
    SEEN["projects"] = options_doc(root)["effective"].get("projects")


r = app(world(), [("migrate", "app01", PLAIN), keep_record, edit(need_project), unscope, mark, DRY, ROLL], env=SCOPE)
obj = dry_json(r, 1)
ok("P2-a the migrate recorded its scope: options.json effective.projects.value {src: psrc, dst: pdst}",
   (SEEN.get("projects") or {}).get("value") == {"src": "psrc", "dst": "pdst"}, SEEN)
ok("P2-a rollback without SRC_PROJECT/DST_PROJECT of a state written with psrc/pdst: 'INFO scope SRC_PROJECT=psrc "
   "DST_PROJECT=pdst (from the state's options.json)'; the dry-run says projects origin record",
   "INFO scope SRC_PROJECT=psrc DST_PROJECT=pdst (from the state's options.json)" in r.out
   and obj.get("projects") == {"src": "psrc", "dst": "pdst", "origin": "record"}
   and obj.get("source_status") == "SHUTOFF" and obj.get("dst_server_status") == "ACTIVE", (r.out[-800:], obj))
ok("P2-a ... and the rollback runs in that scope (objects only visible there): exit 0, the destination empty, the "
   "source ACTIVE, the state retired",
   r.rc == [0, 0, 0] and not r.world["dst"]["servers"] and not r.world["dst"]["volumes"]
   and src_state(r) == ("ACTIVE", False) and retired(r, "app01"), (r.rc, r.err[-500:]))
r = app(world(), [("migrate", "app01", PLAIN), drop_projects_record, edit(need_project), unscope, mark, ROLL],
        env=SCOPE)
ok("P2-a the same state without its project record (1.1.0's behaviour, environment scope 'admin'): the source is "
   "not visible, A20 refuses; nothing deleted (need_project makes the scope matter)",
   r.rc == [0, 1] and "INFO scope: the state has no project record; using SRC_PROJECT/DST_PROJECT of this environment"
   in r.out and "is not found at the source" in r.err and not dst_deletes(r.calls), (r.rc, r.err[-400:]))
r = app(world(), [("migrate", "app01", PLAIN), set_env(SRC_PROJECT="other"), mark, ROLL, DRY], env=SCOPE)
ok("P2-a rollback with SRC_PROJECT=other for a state written with psrc/pdst: exit 1 'scope mismatch', nothing called "
   "or deleted, the state live; the dry-run refuses the same way",
   r.rc == [0, 1, 1] and "scope mismatch: this state was written with SRC_PROJECT=psrc DST_PROJECT=pdst, this run has "
   "SRC_PROJECT=other DST_PROJECT=pdst: set them to the state's values (or unset them) and run the rollback again; "
   "nothing was done" in r.err and not after_mark(r.calls) and live(r), (r.rc, r.err[-500:], after_mark(r.calls)))
r = app(world(), [("migrate", "app01", PLAIN), set_env(DST_PROJECT=None), mark, ROLL], env=SCOPE)
ok("P2-a one of the two set and the other not ('DST_PROJECT=none'): a mismatch too",
   r.rc == [0, 1] and "this run has SRC_PROJECT=psrc DST_PROJECT=none" in r.err and not after_mark(r.calls),
   (r.rc, r.err[-400:]))
r = app(world(), [("migrate", "app01", PLAIN), mark, ROLL], env=SCOPE)
ok("P2-a rollback with the same SRC_PROJECT/DST_PROJECT as the record: no scope line, the rollback runs",
   r.rc == [0, 0] and "INFO scope" not in r.out and retired(r, "app01"), (r.rc, r.out[-400:]))
r = app(world(), [("migrate", "app01", PLAIN), set_env(SRC_PROJECT="psrc", DST_PROJECT="pdst"), mark, ROLL])
ok("P2-a a state written without a scope (record empty) and a rollback with SRC_PROJECT/DST_PROJECT set: mismatch "
   "('SRC_PROJECT=none DST_PROJECT=none')",
   r.rc == [0, 1] and "this state was written with SRC_PROJECT=none DST_PROJECT=none, this run has SRC_PROJECT=psrc "
   "DST_PROJECT=pdst" in r.err, (r.rc, r.err[-400:]))
r = app(world(), [("migrate", "app01", PLAIN), drop_projects_record, mark, DRY, ROLL])
obj = dry_json(r, 1)
ok("P2-a a state without a project record: 'INFO scope: the state has no project record; ...' (stderr with --json), "
   "projects origin none, the rollback runs",
   r.rc == [0, 0, 0] and "INFO scope: the state has no project record; using SRC_PROJECT/DST_PROJECT of this "
   "environment" in r.errs[1] and obj.get("projects") == {"src": None, "dst": None, "origin": "none"}
   and "INFO scope: the state has no project record" in r.outs[2] and retired(r, "app01"), (r.rc, obj, r.errs[1][-300:]))
r = app(world(), [("migrate", "app01", PLAIN), mark, DRY])
ok("P2-a a state written without a scope, a rollback without one: no scope line, origin none",
   "INFO scope" not in r.text and dry_json(r, 1).get("projects") == {"src": None, "dst": None, "origin": "none"},
   (r.text[-300:], dry_json(r, 1).get("projects")))
VSC = json.loads(json.dumps(VW))
r = app(VSC, [("volume", "data", []), unscope, ("cli", ["rollback", "data", "--dry-run", "--json"])], env=SCOPE)
ok("P2-a a volume found by its name through the source cloud: the scope of this environment, an INFO line, origin "
   "none (no restore without the state directory)",
   r.rc == [0, 0] and "INFO scope: data is looked up through the source cloud: using SRC_PROJECT/DST_PROJECT of this "
   "environment" in r.errs[1] and dry_json(r, 1).get("projects", {}).get("origin") == "none",
   (r.rc, r.errs[1][-400:], dry_json(r, 1).get("projects")))


# ================================ case 16: an unrecorded destination server in another project ================================
def blank_server_id(_wf, root):
    """'server create' never returned its ID: dst-server-id is empty."""
    with open(os.path.join(root, "app01", "dst-server-id"), "w") as f:
        f.write("")


def in_other_project(x):
    x["dst"]["servers"][DSID]["project_id"] = "pother"
    x["dst"]["list_own_project"] = True  # a listing without --all-projects shows only the token's project


r = app(world(), [("migrate", "app01", PLAIN), blank_server_id, edit(in_other_project), mark, ROLL])
lists = [c for c in after_mark(r.calls) if c.startswith("dst\tserver list")]
ok("P2-a an unrecorded destination server (its ID never returned) in another project: 'server list --name ^app01$ "
   "--all-projects' finds it, its owner mark proves it, it is deleted; the rollback finishes",
   r.rc == [0, 0] and lists == ["dst\tserver list --name ^app01$ --all-projects -f json"]
   and DSID not in r.world["dst"]["servers"] and not r.world["dst"]["volumes"] and retired(r, "app01"),
   (r.rc, lists, r.err[-400:]))


def forbid_all(x):
    in_other_project(x)
    x["dst"]["forbid_all_projects"] = True
    for v in x["dst"]["volumes"].values():  # detached, so that the volume step does not wait 300 s for an in-use one
        v["status"] = "available"


r = app(world(), [("migrate", "app01", PLAIN), blank_server_id, edit(forbid_all), mark, ROLL])
lists = [c for c in after_mark(r.calls) if c.startswith("dst\tserver list")]
ok("P2-a ... a token that may not list every project (403): asked again without --all-projects (its own project); "
   "the server is not seen and left alone, as 1.1.0",
   r.rc == [0, 0] and lists == ["dst\tserver list --name ^app01$ --all-projects -f json",
                                "dst\tserver list --name ^app01$ -f json"]
   and DSID in r.world["dst"]["servers"], (r.rc, lists, r.err[-400:]))

shutil.rmtree(WORK, ignore_errors=True)
print("TOTAL pass=%d fail=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
