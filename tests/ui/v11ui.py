#!/usr/bin/env python3
"""1.1.0 checks of the UI back end (A20 on the runner side): POST /api/rollback of a state whose source VM or volume no
longer exists ('migrate rollback <ident> --dry-run --json' says source_status "GONE") is refused with 409 before a
batch exists; a source that could not be read (null) is still prepared, with original.source_status null. Real
migui-runner + migui-web on a temporary socket, the fake migrate behind them (its control.json "source_status").

    python3 v11ui.py <repo>
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness as H  # noqa: E402
from harness import VM1, VOL1, ok  # noqa: E402

AZ = {"az": "AZ1"}


def plans(s):
    return [a for a in s.audit_lines() if a.get("action") == "plan"]


s = H.Stack()
try:
    s.ready()
    p = s.plan([VM1], AZ)
    r = s.run_batch(p.json["batch"], "app01")
    j = s.wait_job(r.json["jobs"][0]) if r.status == 202 else {}
    s.post("/api/list", {"projects": ["admin"], "volumes": True, "scope": "available"})
    pv = s.plan([VOL1], {"az": "AZ1", "verify": "none"}, kind="vol")
    rv = s.run_batch(pv.json["batch"], "data-archive-01")
    jv = s.wait_job(rv.json["jobs"][0]) if rv.status == 202 else {}
    ok("A20 setup: app01 and the volume migrated, both states live", j.get("state") == "OK" and jv.get("state") == "OK",
       (j.get("state"), jv.get("state")))

    s.control({"source_status": {VM1: "GONE", "vol-" + VOL1: "GONE"}})
    before, ncalls = len(plans(s)), len(s.calls())
    gone = s.post("/api/rollback", {"ident": VM1})
    err = (gone.json or {}).get("error", "")
    ok("A20 rollback prepare, the source VM gone: 409 '<ident>: the source VM no longer exists at the source; the "
       "destination is the only copy, a rollback would delete it', no batch",
       gone.status == 409 and err == "%s: the source VM no longer exists at the source; the destination is the only "
       "copy, a rollback would delete it" % VM1 and "batch" not in (gone.json or {}), gone)
    new = s.calls()[ncalls:]
    ok("A20 the refusal planned nothing: no 'plan' in the audit; the runner ran the dry-run and no rollback",
       len(plans(s)) == before and ["rollback", VM1, "--dry-run", "--json"] in new
       and not [c for c in new if c[:1] == ["rollback"] and "--dry-run" not in c], (plans(s)[before:], new))
    gv = s.post("/api/rollback", {"ident": "vol-" + VOL1})
    ok("A20 rollback prepare, the source volume gone: 409 naming 'the source volume'",
       gv.status == 409 and "the source volume no longer exists at the source" in (gv.json or {}).get("error", "")
       and len(plans(s)) == before, gv)

    s.control({"source_status": {VM1: None}})
    nul = s.post("/api/rollback", {"ident": VM1})
    o = (nul.json or {}).get("original") or {}
    ok("A20 rollback prepare, the source could not be read (null): prepared (200, a batch), original.source_status null",
       nul.status == 200 and (nul.json or {}).get("batch") and "source_status" in o and o["source_status"] is None
       and len(plans(s)) == before + 1, nul)

    s.control({})
    act = s.post("/api/rollback", {"ident": VM1})
    o = (act.json or {}).get("original") or {}
    ok("A20 rollback prepare, the source there: original.source_status ACTIVE, and the rollback runs (202, OK)",
       act.status == 200 and o.get("source_status") == "ACTIVE", act)
    run = s.run_batch(act.json["batch"], "app01")
    jr = s.wait_job(run.json["jobs"][0]) if run.status == 202 else {}
    ok("A20 ... the rollback job is OK", run.status == 202 and jr.get("state") == "OK", (run, jr.get("state")))
finally:
    s.stop()

sys.exit(H.total())
