#!/usr/bin/env python3
"""1.1.1 checks of the UI back end (A21 on the runner side): POST /api/rollback of a state whose destination server
could not be read ('migrate rollback <ident> --dry-run --json' says dst_server_status null and dst_error <why>) is
still prepared (no 409: the engine refuses at run time, and a destination already gone is a valid rollback run
again), with original.dst_error carrying the reason for the dialog. Real migui-runner + migui-web on a temporary
socket, the fake migrate behind them (its control.json "dst_server_status" / "dst_error").

    python3 v12ui.py <repo>
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness as H  # noqa: E402
from harness import VM1, ok  # noqa: E402

AZ = {"az": "AZ1"}
WHY = "public endpoint for compute service in RegionOne region not found"


def plans(s):
    return [a for a in s.audit_lines() if a.get("action") == "plan"]


s = H.Stack()
try:
    s.ready()
    p = s.plan([VM1], AZ)
    r = s.run_batch(p.json["batch"], "app01")
    j = s.wait_job(r.json["jobs"][0]) if r.status == 202 else {}
    ok("A21 setup: app01 migrated, its state live", j.get("state") == "OK", j.get("state"))

    s.control({"dst_server_status": {VM1: None}, "dst_error": {VM1: WHY}})
    before = len(plans(s))
    nul = s.post("/api/rollback", {"ident": VM1})
    o = (nul.json or {}).get("original") or {}
    ok("A21 rollback prepare, the destination server could not be read: prepared (200, a batch, a 'plan' audit "
       "line), original.dst_server_status null and original.dst_error the reason",
       nul.status == 200 and (nul.json or {}).get("batch") and "dst_server_status" in o
       and o["dst_server_status"] is None and o.get("dst_error") == WHY and len(plans(s)) == before + 1, nul)

    s.control({"dst_server_status": {VM1: "GONE"}})
    gone = s.post("/api/rollback", {"ident": VM1})
    o = (gone.json or {}).get("original") or {}
    ok("A21 rollback prepare, the destination server already gone (a rollback run again): prepared, "
       "original.dst_server_status GONE, dst_error null",
       gone.status == 200 and (gone.json or {}).get("batch") and o.get("dst_server_status") == "GONE"
       and "dst_error" in o and o["dst_error"] is None, gone)

    s.control({"dst_server_status": {VM1: "ACTIVE"}})
    act = s.post("/api/rollback", {"ident": VM1})
    o = (act.json or {}).get("original") or {}
    ok("A21 rollback prepare, the destination read: original.dst_server_status ACTIVE, dst_error null; the rollback "
       "runs (202, OK)", act.status == 200 and o.get("dst_server_status") == "ACTIVE" and o.get("dst_error") is None,
       act)
    run = s.run_batch(act.json["batch"], "app01")
    jr = s.wait_job(run.json["jobs"][0]) if run.status == 202 else {}
    ok("A21 ... the rollback job is OK", run.status == 202 and jr.get("state") == "OK", (run, jr.get("state")))
finally:
    s.stop()

sys.exit(H.total())
