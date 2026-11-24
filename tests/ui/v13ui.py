#!/usr/bin/env python3
"""1.2.0 checks of the UI back end (R3c on the runner side): POST /api/rollback of a state whose source could not be
read ('migrate rollback <ident> --dry-run --json' says source_status null and source_error <why>) carries
original.source_error for the dialog, scrubbed like every other text of the CLI's answer (an openrc's secret value in
it is replaced). Real migui-runner + migui-web on a temporary socket, the fake migrate behind them (its control.json
"source_status" / "source_error").

    python3 v13ui.py <repo>
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness as H  # noqa: E402
from harness import VM1, ok  # noqa: E402

AZ = {"az": "AZ1"}
TMP = tempfile.mkdtemp(prefix="v13ui-", dir=H.WORK)
SECRET = "Very-Secret-Value-5678"
conf = os.path.join(TMP, "migrate.conf")
with open(conf, "w") as f:
    f.write("RBD_POOL=mig-volumes\n")
rc = os.path.join(TMP, "src.rc")
with open(rc, "w") as f:
    f.write("export OS_AUTH_URL=http://src:5000/v3\nexport OS_PASSWORD=%s\n" % SECRET)
os.environ.update(MIG_CONF=conf, SRC_RC=rc)
WHY = "Service Unavailable (HTTP 503)"
LEAK = "Unauthorized (HTTP 401): password=%s rejected" % SECRET

s = H.Stack()
try:
    s.ready()
    p = s.plan([VM1], AZ)
    r = s.run_batch(p.json["batch"], "app01")
    j = s.wait_job(r.json["jobs"][0]) if r.status == 202 else {}
    ok("R3 setup: app01 migrated, its state live", j.get("state") == "OK", j.get("state"))

    s.control({"source_status": {VM1: None}, "source_error": {VM1: WHY}})
    nul = s.post("/api/rollback", {"ident": VM1})
    o = (nul.json or {}).get("original") or {}
    ok("R3 rollback prepare, the source could not be read: prepared (200, a batch), original.source_status null and "
       "original.source_error the reason",
       nul.status == 200 and (nul.json or {}).get("batch") and "source_status" in o and o["source_status"] is None
       and o.get("source_error") == WHY, nul)

    s.control({"source_status": {VM1: None}, "source_error": {VM1: LEAK}})
    leak = s.post("/api/rollback", {"ident": VM1})
    o = (leak.json or {}).get("original") or {}
    err = o.get("source_error") or ""
    ok("R3 original.source_error is scrubbed like dst_error (the openrc's secret value replaced), the rest kept",
       leak.status == 200 and SECRET not in err and "[scrubbed]" in err and err.startswith("Unauthorized (HTTP 401)")
       and SECRET not in str(leak.json), (leak.status, err))

    s.control({"source_status": {VM1: "SHUTOFF"}, "source_error": {}})
    act = s.post("/api/rollback", {"ident": VM1})
    o = (act.json or {}).get("original") or {}
    ok("R3 rollback prepare, the source read: original.source_status SHUTOFF, source_error null (the key there)",
       act.status == 200 and o.get("source_status") == "SHUTOFF" and "source_error" in o and o["source_error"] is None,
       act)
    run = s.run_batch(act.json["batch"], "app01")
    jr = s.wait_job(run.json["jobs"][0]) if run.status == 202 else {}
    ok("R3 ... the rollback job is OK", run.status == 202 and jr.get("state") == "OK", (run, jr.get("state")))
finally:
    s.stop()

sys.exit(H.total())
