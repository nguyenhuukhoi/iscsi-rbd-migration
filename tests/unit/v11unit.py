#!/usr/bin/env python3
"""1.1.0 unit checks: A20, a rollback deletes nothing at the destination before it has proven that the source object
(the VM, or the volume of a migrate-volume state) still exists. Runs the app in this process against the fake clouds
(inproc.py), like v1unit.py.

    python3 v11unit.py <directory holding migratetool/>
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
WORK = tempfile.mkdtemp(prefix="v11unit-")
DST_DELETES = ("dst\tserver delete", "dst\tvolume delete", "dst\tport delete", "dst\timage delete")


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


def drop_source_vm(x):
    """Somebody deleted the source VM (clean-up) while the state directory stayed live."""
    x["src"]["servers"].pop(SID)


def last_err(r):
    return (r.err.strip().splitlines() or [""])[-1]


def retired(r, ident):
    return glob.glob(os.path.join(r.state, ident + ".rolled-back.*"))


# ================================ case 1/2: the source VM is gone ================================
r = app(world(), [("migrate", "app01", PLAIN), edit(drop_source_vm), mark, ROLL])
# Case 1 (the reproduction): what the rollback did, printed for the test report whatever the result.
print("INFO case 1 rc=%s last stderr line=%r dst servers=%s dst volumes=%s dst deletes=%s state live=%s"
      % (r.rc, last_err(r), sorted(r.world["dst"]["servers"]), sorted(r.world["dst"]["volumes"]),
         dst_deletes(r.calls), os.path.isdir(os.path.join(r.state, "app01"))))
ok("A20 the source VM is gone: the rollback stops (exit 1) naming it, 'no longer exists'",
   r.rc == [0, 1] and "no longer exists" in r.err and SID in r.err, (r.rc, r.err[-600:]))
ok("A20 the source VM is gone: no 'server delete', 'volume delete', 'port delete' or 'image delete' at the destination",
   not dst_deletes(r.calls), dst_deletes(r.calls))
ok("A20 the source VM is gone: the destination server and both volumes are still there",
   "srv-dst-0001" in r.world["dst"]["servers"]
   and {"vol-dst-0001", "vol-dst-0002"} <= set(r.world["dst"]["volumes"]), (r.world["dst"]["servers"].keys(),
                                                                             r.world["dst"]["volumes"].keys()))
ok("A20 the source VM is gone: the state directory stays live (not retired)",
   os.path.isfile(os.path.join(r.state, "app01", "volume-map")) and not retired(r, "app01"), os.listdir(r.state))
ok("A20 the message names what is the only copy (server, volumes), the live state directory, and says prune never "
   "removes it (no invented command)",
   "the destination (server srv-dst-0001, volumes vol-dst-0001 vol-dst-0002) is the only copy, nothing was deleted"
   in r.err and "the state directory %s stays live" % os.path.join(r.state, "app01") in r.err
   and "'migrate prune' removes only rolled-back state directories, never a live one" in r.err
   and "remove that directory by hand" in r.err, r.err[-800:])
ok("A20 the probe is the rollback's last call at the source: no unlock, no port, no start after it",
   [c for c in after_mark(r.calls) if c.startswith("src\t")][-1:] == ["src\tserver show %s -f json -c id -c status" % SID]
   and not [c for c in after_mark(r.calls) if c.startswith("src\tserver unlock")], after_mark(r.calls))


def blank_server_id(_wf, root):
    """'server create' never returned its ID: dst-server-id is empty."""
    with open(os.path.join(root, "app01", "dst-server-id"), "w") as f:
        f.write("")


r = app(world(), [("migrate", "app01", PLAIN), blank_server_id, edit(drop_source_vm), mark, ROLL])
ok("A20 the source VM is gone and no server ID was recorded: same refusal, 'server none recorded', no name search",
   r.rc == [0, 1] and "the destination (server none recorded, volumes vol-dst-0001 vol-dst-0002)" in r.err
   and not dst_deletes(r.calls) and not [c for c in after_mark(r.calls) if c.startswith("dst\tserver list")],
   (r.rc, r.err[-500:]))

# ================================ case 3: the source cannot be read ================================
r = app(world(), [("migrate", "app01", PLAIN),
                  edit(lambda x: x["src"].update(fail={"server show %s" % SID: "Internal Server Error (HTTP 500)"})),
                  mark, ROLL])
ok("A20 'server show' of the source answers 500: the rollback stops, 'cannot read source VM' with the reason; "
   "nothing deleted at the destination, the state live",
   r.rc == [0, 1] and "cannot read source VM %s (Internal Server Error (HTTP 500)): nothing was deleted; check the "
   "source credentials/SRC_PROJECT and run the rollback again" % SID in r.err and not dst_deletes(r.calls)
   and "srv-dst-0001" in r.world["dst"]["servers"] and not retired(r, "app01"), (r.rc, r.err[-500:]))
r = app(world(), [("migrate", "app01", PLAIN),
                  edit(lambda x: x["src"].update(fail={"server show": "The request you have made requires "
                                                                      "authentication. (HTTP 401)"})), mark, ROLL])
ok("A20 the source answers 401 (credentials): 'cannot read source VM', nothing deleted (401 is not 'not found')",
   r.rc == [0, 1] and "cannot read source VM" in r.err and "(HTTP 401)" in r.err and not dst_deletes(r.calls),
   (r.rc, r.err[-400:]))

# ================================ case 4: the source is there ================================
r = app(world(), [("migrate", "app01", PLAIN), mark, ROLL])
rb = after_mark(r.calls)
probe = "src\tserver show %s -f json -c id -c status" % SID
ok("A20 the source VM is there (SHUTOFF after the cutover): one probe first, then the rollback as in 1.0.1 (server, "
   "port, volumes deleted, source ACTIVE, state retired)",
   r.rc == [0, 0] and rb.count(probe) == 1 and rb.index(probe) < rb.index("dst\tserver delete srv-dst-0001 --wait")
   and not r.world["dst"]["servers"] and not r.world["dst"]["volumes"]
   and r.world["src"]["servers"][SID]["status"] == "ACTIVE" and retired(r, "app01"), (r.rc, rb[:6], r.err[-300:]))
r = app(world(), [("migrate", "app01", PLAIN), edit(lambda x: x["src"]["servers"][SID].update(status="ERROR")),
                  mark, ROLL])
ok("A20 any status passes the probe (ERROR here): the destination is deleted; what follows is the 1.0.1 rollback",
   "srv-dst-0001" not in r.world["dst"]["servers"] and "no longer exists" not in r.err
   and "cannot read source" not in r.err, (r.rc, r.err[-300:]))

# ================================ case 7: migrate-volume, the source volume is gone ================================
VW = world()
VW["src"]["volumes"]["vol-src-data"]["status"] = "available"
VROLL = ("rollback", "vol-vol-src-data")
r = app(VW, [("volume", "data", []), edit(lambda x: x["src"]["volumes"].pop("vol-src-data")), mark, VROLL])
ok("A20 volume: the source volume is gone: exit 1, 'source volume vol-src-data (data) no longer exists', the copy "
   "kept, no 'volume delete', the state live",
   r.rc == [0, 1] and "source volume vol-src-data (data) no longer exists at the source: the destination (volumes "
   "vol-dst-0001) is the only copy" in r.err and "vol-dst-0001" in r.world["dst"]["volumes"]
   and not dst_deletes(r.calls) and not retired(r, "vol-vol-src-data")
   and after_mark(r.calls)[-1] == "src\tvolume show vol-src-data -f json -c id -c status", (r.rc, r.err[-500:]))
r = app(VW, [("volume", "data", []),
             edit(lambda x: x["src"].update(fail={"volume show vol-src-data": "Gateway Timeout (HTTP 504)"})),
             mark, VROLL])
ok("A20 volume: the source volume cannot be read: 'cannot read source volume', the copy kept",
   r.rc == [0, 1] and "cannot read source volume vol-src-data (Gateway Timeout (HTTP 504)): nothing was deleted"
   in r.err and "vol-dst-0001" in r.world["dst"]["volumes"] and not dst_deletes(r.calls), (r.rc, r.err[-400:]))
r = app(VW, [("volume", "data", []), mark, VROLL])
ok("A20 volume: the source volume is there: probed once, the copy deleted, the state retired (as 1.0.1)",
   r.rc == [0, 0] and after_mark(r.calls).count("src\tvolume show vol-src-data -f json -c id -c status") == 1
   and "vol-dst-0001" not in r.world["dst"]["volumes"] and retired(r, "vol-vol-src-data"), (r.rc, r.err[-300:]))

# ================================ case 8: a rollback run again after it failed half-way ================================
r = app(world(), [("migrate", "app01", PLAIN),
                  edit(lambda x: x["dst"].update(fail={"volume delete": "Service Unavailable (HTTP 503)"})), ROLL,
                  edit(lambda x: x["dst"].pop("fail")), mark, ROLL])
ok("A20 a rollback that failed half-way (destination server already deleted, the source still there) runs again: "
   "the probe asks the source only, the rollback finishes",
   r.rc == [0, 1, 0] and "srv-dst-0001" not in r.world["dst"]["servers"] and not r.world["dst"]["volumes"]
   and r.world["src"]["servers"][SID]["status"] == "ACTIVE" and retired(r, "app01")
   and after_mark(r.calls).count(probe) == 1, (r.rc, r.err[-500:]))

# ================================ case 5/6: rollback --dry-run shows source_status ================================
DRY = ("cli", ["rollback", "app01", "--dry-run", "--json"])
TEXT = ("cli", ["rollback", "app01", "--dry-run"])


def dry_json(r, i):
    try:
        return json.loads(r.outs[i])
    except ValueError:
        return {}


r = app(world(), [("migrate", "app01", PLAIN), edit(drop_source_vm), mark, DRY, TEXT, ("cli", ["rollback", "app01"])])
obj = dry_json(r, 1)
ok("A20 --dry-run --json, the source VM gone: source_status GONE, source_error null, exit 0, the rest as before",
   r.rc[:3] == [0, 0, 0] and obj.get("source_status") == "GONE" and "source_error" in obj
   and obj.get("source_error") is None and obj.get("source_power") == "ACTIVE"
   and obj.get("delete", {}).get("server") == "srv-dst-0001", (r.rc, obj))
ok("A20 --dry-run (text), the source VM gone: an ERROR line 'this rollback is refused', no 'then the source VM is' line",
   "  ERROR the source VM no longer exists: the destination is the only copy; this rollback is refused" in r.outs[2]
   and "then the source VM is" not in r.outs[2] and "now GONE" not in r.outs[2], r.outs[2])
ok("A20 'rollback' without --yes and no terminal, the source VM gone: the summary on stderr has the ERROR line; exit 2",
   r.rc[3] == 2 and "ERROR the source VM no longer exists" in r.errs[3], (r.rc, r.errs[3][-500:]))
ok("A20 the dry-runs and the refused prompt deleted nothing; the dry-run probe is read-only 'server show'",
   not dst_deletes(r.calls) and "srv-dst-0001" in r.world["dst"]["servers"]
   and after_mark(r.calls).count("src\tserver show %s -f json -c id -c status" % SID) == 3, after_mark(r.calls))
r = app(world(), [("migrate", "app01", PLAIN), DRY, TEXT])
obj = dry_json(r, 1)
ok("A20 --dry-run --json, the source VM there: source_status is its status now (SHUTOFF after the cutover), "
   "source_power the one at export (ACTIVE); the text says 'now SHUTOFF', no ERROR/WARN",
   r.rc == [0, 0, 0] and obj.get("source_status") == "SHUTOFF" and obj.get("source_power") == "ACTIVE"
   and obj.get("source_error") is None
   and r.outs[2].startswith("Rollback of app01 (source VM app01 %s, now SHUTOFF):" % SID)
   and "then the source VM is unlocked, its ports given back, started" in r.outs[2]
   and "ERROR" not in r.outs[2] and "WARN" not in r.outs[2], (r.rc, obj, r.outs[2]))
r = app(world(), [("migrate", "app01", PLAIN),
                  edit(lambda x: x["src"].update(fail={"server show %s" % SID: "Internal Server Error (HTTP 500)"})),
                  DRY, TEXT])
obj = dry_json(r, 1)
ok("A20 --dry-run, the source cannot be read: source_status null, source_error the reason, a WARN line",
   r.rc == [0, 0, 0] and "source_status" in obj and obj["source_status"] is None
   and obj.get("source_error") == "Internal Server Error (HTTP 500)"
   and "  WARN the source VM could not be read (Internal Server Error (HTTP 500)): the rollback refuses until it can"
   in r.outs[2], (r.rc, obj, r.outs[2]))
r = app(VW, [("volume", "data", []), ("cli", ["rollback", "vol-vol-src-data", "--dry-run", "--json"]),
             edit(lambda x: x["src"]["volumes"].pop("vol-src-data")),
             ("cli", ["rollback", "vol-vol-src-data", "--dry-run", "--json"]),
             ("cli", ["rollback", "vol-vol-src-data", "--dry-run"])])
a_, b_ = dry_json(r, 1), dry_json(r, 2)
ok("A20 volume --dry-run: source_status available while it is there, GONE once deleted; the text has the ERROR line",
   r.rc == [0, 0, 0, 0] and a_.get("kind") == "vol" and a_.get("source_status") == "available"
   and b_.get("source_status") == "GONE"
   and "  ERROR the source volume no longer exists: the destination is the only copy; this rollback is refused"
   in r.outs[3] and "vol-dst-0001" in r.world["dst"]["volumes"], (r.rc, a_, b_, r.outs[3]))

shutil.rmtree(WORK, ignore_errors=True)
print("TOTAL pass=%d fail=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
