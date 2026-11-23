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

shutil.rmtree(WORK, ignore_errors=True)
print("TOTAL pass=%d fail=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
