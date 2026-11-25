#!/usr/bin/env python3
"""1.0.0 checks of the UI back end (REVIEW-20261121 section 3 contract; B1, B4, B5, B7, B8, C1, C5, C6, C14, D3, D9,
A5 and A7 on the runner side). Real migui-runner + migui-web on a temporary socket, the fake migrate behind them.

    python3 v1ui.py <repo>
"""
import http.client
import json
import os
import re
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness as H  # noqa: E402
from harness import VM1, VM2, VM3, ok  # noqa: E402

TMP = tempfile.mkdtemp(prefix="v1ui-", dir=H.WORK)
SECRET = "Very-Secret-Value-1234"
conf = os.path.join(TMP, "migrate.conf")
with open(conf, "w") as f:
    f.write("RBD_POOL=mig-volumes\n")
rc = os.path.join(TMP, "src.rc")
with open(rc, "w") as f:
    f.write("export OS_AUTH_URL=http://src:5000/v3\nexport OS_PASSWORD=%s\n" % SECRET)
notify_out = os.path.join(TMP, "notify.jsonl")
NOTIFY = [H.PY, "-I", "-c", "import json, os, sys\nwith open(sys.argv[1], 'a') as f:\n"
          "    f.write(json.dumps({k: v for k, v in os.environ.items() if k.startswith('MIG_')}) + '\\n')\n", notify_out]
os.environ.update(MIG_CONF=conf, SRC_RC=rc, MIGUI_NOTIFY_CMD=json.dumps(NOTIFY),
                  FAKE_LEAK_LINE="debug: password=%s url https://admin:%s@keystone/v3" % (SECRET, SECRET))

s = H.Stack(control={"plan_delay": 1})
try:
    s.login()
    ses = s.get("/api/session").json
    ok("1.2.4 GET /api/session: ui_version and features {write_check, plan_jobs, prune}",
       ses.get("ui_version") == "1.2.4" and ses.get("version") == "1.2.4"
       and ses.get("features") == {"write_check": True, "plan_jobs": 4, "prune": True}, ses)
    # D3: before a doctor, only inventory/list/plan/run are gated
    st = [s.post("/api/rollback", {"ident": VM1}).status, s.get("/api/jobs").status,
          s.post("/api/jobs/%s/cancel" % ("0" * 16), {}).status, s.post("/api/status").status,
          s.post("/api/list", {"hosts": ["cmp1"]}).status, s.post("/api/plan", {"kind": "vm", "items": [{"ident": VM1}]}).status]
    ok("D3 without a green doctor: rollback 409 (no live state), jobs 200, cancel 409, status 200 — never 412; list and "
       "plan 412", st == [409, 200, 409, 200, 412, 412], st)
    d = s.post("/api/doctor", {"write_check": True})
    ok("C6 POST /api/doctor {write_check: true}: the runner runs 'migrate doctor --json --write-check'",
       d.status == 200 and ["doctor", "--json", "--write-check"] in s.calls(), (d.status, s.calls()[-2:]))
    s.post("/api/inventory")
    s.post("/api/list", {"hosts": ["cmp1"]})
    # C1: three VMs, each 'migrate plan' takes >= 1 s: planned at once
    t0 = time.monotonic()
    resp, events, _p = s.sse_post("/api/plan", {"kind": "vm", "items": [{"ident": x} for x in (VM1, VM2, VM3)],
                                                "options": {"az": "AZ1"}})
    took = time.monotonic() - t0
    order = [(e, d_.get("ident")) for e, d_, _t in events if e in ("planning", "row")]
    per = {}
    for i, (e, ident) in enumerate(order):
        per.setdefault(ident, []).append((e, i))
    batch = next((d_ for e, d_, _t in events if e == "batch"), None) or {}
    last_row = max(i for i, (e, d_, _t) in enumerate(events) if e == "row")
    first_batch = next(i for i, (e, d_, _t) in enumerate(events) if e == "batch")
    ok("C1 a plan of 3 VMs (1 s each) runs them at once (< 2.5 s); per ident: planning before its row; the batch after "
       "every row", took < 2.5 and sorted(per) == sorted([VM1, VM2, VM3])
       and all([e for e, _i in v] == ["planning", "row"] for v in per.values()) and last_row < first_batch,
       (round(took, 2), order))
    ok("section 3: batch summary fingerprint, conf_digest (sha256), impact, rule from the plan, session",
       re.match(r"^[0-9a-f]{64}$", batch.get("fingerprint") or "") and re.match(r"^[0-9a-f]{64}$", batch.get("conf_digest") or "")
       and batch.get("impact") == {"vms": 3, "volumes": 4, "gb": 32, "stop_source": 3, "detach_ports": 3}
       and batch.get("rule") == {"mode": "count", "expect": ["3"]} and batch.get("session") == ses["session"], batch)
    # C5: the conf changes between plan and run
    with open(conf, "a") as f:
        f.write("MIG_AZ=AZ1\n")
    r = s.run_batch(batch, "3")
    ok("C5 migrate.conf changed after the plan: run answers 409 'plan again', nothing started",
       r.status == 409 and "changed since the plan" in (r.json or {}).get("error", "")
       and not [c for c in s.calls() if c[0] == "migrate"], r)
    p = s.plan([VM1, VM2], {"az": "AZ1"})
    r = s.run_batch(p.json["batch"], "2")
    jobs = r.json.get("jobs") or []
    for j in jobs:
        s.wait_job(j)
    s.wait_idle()
    recs = [s.get("/api/jobs/" + j).json["job"] for j in jobs]
    ok("section 3: job records carry volumes_total (from the plan), exit_class (None on OK), notify_rc 0",
       [x.get("volumes_total") for x in recs] == [2, 1] and all(x.get("exit_class") is None and x.get("notify_rc") == 0
                                                                for x in recs), [(x.get("volumes_total"), x.get("exit_class"),
                                                                                 x.get("notify_rc")) for x in recs])
    env_log = [json.loads(l) for l in open(os.path.join(s.fake, "env.log")) if l.strip()]
    mig = [e for e in env_log if e["args"][:1] == ["migrate"]]
    ok("A7 every migrate the runner starts gets its host lock: MIG_LOCK = the runner's lock file, MIG_LOCK_FD set",
       mig and all(e["MIG_LOCK"] == os.path.join(s.run, "runner.lock") and (e["MIG_LOCK_FD"] or "").isdigit() for e in mig),
       mig)
    notes = [json.loads(l) for l in open(notify_out)] if os.path.exists(notify_out) else []
    ok("C14 MIGUI_NOTIFY_CMD (an argv, no shell) at the end of each job and of the batch: MIG_EVENT, MIG_JOB, MIG_STATE",
       [n.get("MIG_EVENT") for n in notes] == ["job", "job", "batch"] and notes[0].get("MIG_JOB") == jobs[0]
       and all(n.get("MIG_STATE") == "OK" for n in notes) and not any("MIG_LOCK_FD" in n for n in notes), notes)
    # D9: query, paging, CSV
    a = s.get("/api/jobs?limit=1").json
    b = s.get("/api/jobs?q=app02").json
    c = s.get("/api/jobs?kind=rollback").json
    e = s.get("/api/jobs?state=OK&from=2020-01-01&to=2099-12-31").json
    bad = s.get("/api/jobs?offset=-1").status
    ok("D9 GET /api/jobs: {jobs, total, active}; limit, q, kind, state, from/to; a bad offset is 400",
       len(a["jobs"]) == 1 and a["total"] == 2 and "active" in a and [x["name"] for x in b["jobs"]] == ["app02"]
       and c["total"] == 0 and e["total"] == 2 and bad == 400, (a.get("total"), b, c, e.get("total"), bad))
    csvr = s.get("/api/jobs.csv")
    lines = csvr.raw.decode().splitlines()
    ok("D9 GET /api/jobs.csv: text/csv attachment, a header and one line per job",
       csvr.status == 200 and csvr.headers.get("content-type", "").startswith("text/csv")
       and "attachment" in csvr.headers.get("content-disposition", "") and lines[0].startswith("id,batch,kind,ident,name")
       and len(lines) == 3, (csvr.status, lines[:3]))
    # A5: the rollback the runner prepares
    rb = s.post("/api/rollback", {"ident": VM1}).json
    o = rb.get("original") or {}
    ok("A5 rollback prepare: 'migrate rollback <ident> --yes' (the typed name is the confirmation); original has t_stop, "
       "dst_server_status, age_seconds, delete",
       rb["batch"]["commands"] == [["migrate", "rollback", VM1, "--yes"]] and "dst_server_status" in o
       and "t_stop" in o and isinstance(o.get("age_seconds"), int) and isinstance(o.get("delete"), dict)
       and ["rollback", VM1, "--dry-run", "--json"] in s.calls(), rb)
    r = s.run_batch(rb["batch"], VM1)  # D5: the full ident typed instead of the name
    ok("D5 the ident typed in full confirms a rollback too", r.status == 202, r)
    s.wait_idle()
    rb2 = s.post("/api/rollback", {"ident": VM2}).json
    s.run_batch(rb2["batch"], "app02")
    s.wait_idle()
    # B1: a line carrying an openrc's secret value
    s.control({"mode": "leaky", "steps": 2, "step_secs": 0.01})
    p = s.plan([VM2], {"az": "AZ1"})
    r = s.run_batch(p.json["batch"], "app02")
    jid = r.json["jobs"][0]
    s.wait_job(jid)
    log = s.get("/api/jobs/%s/log" % jid).raw.decode()
    ok("B1 the runner scrubs the openrc's secret value wherever it appears (from its probe), and URL credentials",
       SECRET not in log and "password=[scrubbed]" in log and "https://admin:[scrubbed]@keystone" in log, log[-600:])
    s.wait_idle()
    # B5: the audit cannot be written
    os.rename(s.audit, s.audit + ".moved")
    os.mkdir(s.audit)
    r = s.post("/api/plan", {"kind": "vm", "items": [{"ident": VM3}], "options": {"az": "AZ1"}})
    os.rmdir(s.audit)
    os.rename(s.audit + ".moved", s.audit)
    ok("B5 an audit line that cannot be written: the mutating POST answers 503, nothing runs",
       r.status == 503 and "audit" in (r.json or {}).get("error", ""), r)
    # B8: event streams per session
    s.control({"mode": "slow", "steps": 200, "step_secs": 0.05})
    p = s.plan([VM3], {"az": "AZ1"})
    r = s.run_batch(p.json["batch"], H.XSS_NAME)
    jid = r.json["jobs"][0]
    conns = []
    for _ in range(2):
        c_ = http.client.HTTPConnection("127.0.0.1", s.port, timeout=30)
        c_.request("GET", "/api/jobs/%s/stream" % jid, headers=s.headers("GET"))
        conns.append((c_, c_.getresponse()))
    third = s.get("/api/jobs/%s/stream" % jid, timeout=10).status
    for c_, resp_ in conns:
        c_.close()
    ok("B8 a third event stream of one session: 429 (two are open)", [x[1].status for x in conns] == [200, 200]
       and third == 429, third)
    s.post("/api/jobs/%s/cancel" % jid, {})
    s.wait_idle()
    # B4: the password changes: the session ends
    p_ = s.passwd("set", H.USER, stdin="another-password-2026\n")
    after = s.get("/api/jobs")
    ok("B4 migui-passwd set: the next request of the old session is 401 (audit session-revoked)",
       p_.returncode == 0 and after.status == 401 and any(x["action"] == "session-revoked" for x in s.audit_lines()),
       (p_.stderr, after))
    # B7: a lock per name
    for _ in range(5):
        s.login(H.USER, "wrong-password-0000")
    locked = s.login(H.USER, "another-password-2026")
    s.passwd("add", "other", stdin="other-password-2026\n")
    other = s.login("other", "other-password-2026")
    ok("B7 five failures lock that name only (429 even with the right password); another account still logs in",
       locked.status == 429 and other.status == 200, (locked, other))
finally:
    s.stop()
    for k in ("MIG_CONF", "SRC_RC", "MIGUI_NOTIFY_CMD", "FAKE_LEAK_LINE"):
        os.environ.pop(k, None)

# fix-1.0.1 (Review 23/11 #2): a cancel that lands after migrate printed its result rc 0 (the VM is migrated, the
# selector still finishing) leaves the job OK, flagged cancel.late; the batch still stops (the next job SKIPPED).
# Round 2: late_secs 6 so the cancel cannot miss the window on a slow host; the audit 'end' keeps the real exit, the
# batch notification says CANCELLED (no job failed).
late_notify = os.path.join(TMP, "notify-late.jsonl")
os.environ["MIGUI_NOTIFY_CMD"] = json.dumps(NOTIFY[:-1] + [late_notify])
s = H.Stack(control={"modes": {VM1: "late", VM2: "slow"}, "steps": 4, "step_secs": 0.05, "late_secs": 6})
try:
    s.ready()
    p = s.plan([VM1, VM2], {"az": "AZ1"})
    r = s.run_batch(p.json["batch"], "2")
    jobs = r.json.get("jobs") or []
    t0 = time.time()
    seen = None
    while time.time() - t0 < 30:
        jr = (s.get("/api/jobs/" + jobs[0]).json or {}).get("job") or {}
        seen = (jr.get("progress") or {}).get("result")
        if seen:
            break
        time.sleep(0.1)
    c = s.post("/api/jobs/%s/cancel" % jobs[0], {"force": False})
    j1 = s.wait_job(jobs[0])
    s.wait_idle()
    j2 = s.get("/api/jobs/" + jobs[1]).json["job"]
    log1 = s.get("/api/jobs/%s/log" % jobs[0]).raw.decode()
    ok("fix-1.0.1 #2 cancel after the result rc 0 (migrate exits 130 while finishing): job OK, rc 0, cancel.late, "
       "sigint_at kept; the next job SKIPPED",
       seen and seen.get("rc") == 0 and c.status == 200 and "interrupted after the result" in log1
       and j1.get("state") == "OK" and j1.get("rc") == 0 and (j1.get("cancel") or {}).get("late") is True
       and (j1.get("cancel") or {}).get("sigint_at") and j2.get("state") == "SKIPPED",
       (seen, c, j1.get("state"), j1.get("rc"), j1.get("cancel"), j2.get("state"), log1[-300:]))
    end1 = [x for x in s.audit_lines() if x.get("action") == "end" and x.get("job") == jobs[0]]
    ok("fix-1.0.1 #2 round 2 the late-cancelled job: cancel.exit 130 (the exit migrate gave), audit 'end' rc 0 with "
       "detail exit 130",
       (j1.get("cancel") or {}).get("exit") == 130 and len(end1) == 1 and end1[0].get("rc") == 0
       and (end1[0].get("detail") or {}).get("exit") == 130 and (end1[0].get("detail") or {}).get("state") == "OK",
       (j1.get("cancel"), end1))
    lnotes = [json.loads(l) for l in open(late_notify)] if os.path.exists(late_notify) else []
    ok("fix-1.0.1 #2 round 2 the batch of the late cancel ([OK, SKIPPED], stopped as cancelled) notifies CANCELLED, "
       "not FAIL; its first job notifies OK",
       [(n.get("MIG_EVENT"), n.get("MIG_STATE")) for n in lnotes] == [("job", "OK"), ("batch", "CANCELLED")],
       lnotes)
finally:
    s.stop()
    os.environ.pop("MIGUI_NOTIFY_CMD", None)

# fix-1.0.1 round 3: 'Dừng sau con hiện tại' (POST /api/batches/<id>/stop) while the first of two slow jobs runs: the
# job finishes OK, the next is SKIPPED, and the batch notifies CANCELLED (the operator stopped it), not FAIL.
stop_notify = os.path.join(TMP, "notify-stop.jsonl")
os.environ["MIGUI_NOTIFY_CMD"] = json.dumps(NOTIFY[:-1] + [stop_notify])
s = H.Stack(control={"modes": {VM1: "slow", VM2: "slow"}, "steps": 30, "step_secs": 0.1})
try:
    s.ready()
    p = s.plan([VM1, VM2], {"az": "AZ1"})
    r = s.run_batch(p.json["batch"], "2")
    jobs = r.json.get("jobs") or []
    act = s.get("/api/jobs").json.get("active") or {}
    stp = s.post("/api/batches/%s/stop" % act.get("id"), {})
    s.wait_idle()
    js = [s.get("/api/jobs/" + x).json["job"]["state"] for x in jobs]
    snotes = [json.loads(l) for l in open(stop_notify)] if os.path.exists(stop_notify) else []
    ok("fix-1.0.1 #2 round 3 stop after the current job ([OK, SKIPPED]): the batch notifies CANCELLED, not FAIL; its "
       "first job notifies OK",
       stp.status == 200 and js == ["OK", "SKIPPED"]
       and [(n.get("MIG_EVENT"), n.get("MIG_STATE")) for n in snotes] == [("job", "OK"), ("batch", "CANCELLED")],
       (stp, js, snotes))
finally:
    s.stop()
    os.environ.pop("MIGUI_NOTIFY_CMD", None)

sys.exit(H.total())
