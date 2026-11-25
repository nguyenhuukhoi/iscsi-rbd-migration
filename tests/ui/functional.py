#!/usr/bin/env python3
"""Functional tests of the UI (DESIGN-UI 5, 7, 8, 13, 14) against a real migui-runner + migui-web and the fake
migrate: login and sessions, the Doctor gate, plan -> confirm -> run -> SSE stream -> history, the count and name
confirmations of 14.2, rollback, cancel (SIGINT, then SIGKILL after kill_after), stop on FAIL, volumes, job
records (13.2), a runner restart under a running job (8), the plan checks kept with the job (17), the plan stream
(18), the live lines of the plan being made (20) and several source hosts / projects in one list (21). Cases are
named F-<n>.

    python3 -I tests/ui/functional.py <repo>
"""
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness as H  # noqa: E402
from harness import VM1, VM2, VM3, VOL1, XSS_NAME, ok  # noqa: E402

AZ = {"az": "AZ1"}
GUARDS = 21  # PASS lines of tests/ui/guards.mjs

# ---------------------------------------------------------------- F-1 login, cookie, one session, logout, gate
s = H.Stack()
try:
    r = s.login()
    sc = r.headers.get("set-cookie", "")
    ok("F-1 login: cookie HttpOnly; SameSite=Strict; Path=/; Max-Age 8 h; session id is 16 hex, not the cookie",
       r.status == 200 and "HttpOnly" in sc and "SameSite=Strict" in sc and "Path=/" in sc and "Max-Age=28800" in sc
       and re.match(r"^[0-9a-f]{16}$", r.json.get("session", "")) and r.json["session"] not in sc, (r, sc))
    ok("F-1 (16.3) login answers the session with its user; GET /api/session too", r.json.get("user") == H.USER
       and s.get("/api/session").json.get("user") == H.USER, r)
    ok("F-1 GET /api/session with the cookie: 200, Cache-Control no-store",
       s.get("/api/session").status == 200 and s.get("/api/session").headers.get("cache-control") == "no-store")
    ok("F-1 Doctor gate: list, inventory and plan before a green doctor answer 412",
       [s.post(p, b).status for p, b in (("/api/list", {"hosts": ["cmp1"]}), ("/api/inventory", {}),
                                         ("/api/plan", {"kind": "vm", "items": [{"ident": VM1}]}))] == [412] * 3)
    d = s.post("/api/doctor")
    ok("F-1 doctor: migrate doctor --json passed, doctor_ok", d.status == 200 and d.json["doctor_ok"] is True
       and d.json["data"]["counts"]["fail"] == 0, d)
    ok("F-1 list before inventory: the runner refuses (409, its whitelist comes from its own inventory)",
       s.post("/api/list", {"hosts": ["cmp1"]}).status == 409)
    first = s.cookie
    s.login()
    second = s.cookie
    s.cookie = first
    old = s.get("/api/jobs").status
    s.cookie = second
    ok("F-1 one session at a time: a new login voids the old cookie (401), the new one works",
       old == 401 and s.get("/api/jobs").status == 200)
    rep = [a for a in s.audit_lines() if a["action"] == "session-replaced"]
    ok("F-1 (16.3) audit session-replaced names the old user and the new one",
       len(rep) == 1 and rep[0]["detail"] == {"user": H.USER, "new_user": H.USER}, rep)
    lo = s.post("/api/logout")
    ok("F-1 logout: 200, cookie cleared, the cookie is dead after it",
       lo.status == 200 and "Max-Age=0" in lo.headers.get("set-cookie", "") and s.get("/api/jobs").status == 401)
finally:
    s.stop()

s = H.Stack(start_web=False)
try:
    s.web_log = open(os.path.join(s.dir, "web.stderr"), "wb")
    s.web = subprocess.Popen(s.web_argv("--idle", "1"), stdout=s.web_log, stderr=subprocess.STDOUT)
    s.wait(s.port_open, "web port")
    s.login()
    a = s.get("/api/jobs", headers={"X-Migui-Background": "1"}).status
    time.sleep(2.2)
    b = s.get("/api/jobs").status
    ok("F-1 idle timeout (web --idle 1): 200, then 401 after 2 s without a request; audit session-expired",
       (a, b) == (200, 401) and any(x["action"] == "session-expired" for x in s.audit_lines()), (a, b))
finally:
    s.stop()

# ---------------------------------------------------------------- F-2 plan -> confirm (name) -> run -> stream
s = H.Stack()
try:
    d, i, l = s.ready()
    inv = i.json.get("data") or {}
    ok("F-2 inventory: source hosts, destination zones/hosts/flavors/types, conf, the CLI's option help",
       [h["host"] for h in inv["source"]["hosts"]] == ["cmp1", "cmp2"] and "AZ1" in inv["destination"]["zones"]["compute"]
       and any(o[0] == "--dst-host" for o in i.json["catalog"]["help"]), i)
    rows = l.json["data"]["rows"]
    ok("F-2 list --host cmp1: three VMs with boot, volumes, networks, state", len(rows) == 3
       and all(k in rows[0] for k in ("boot", "volumes", "networks", "state", "host", "az")) and l.json["argv"]
       == ["migrate", "list", "--json", "--host", "cmp1"], rows[:1])
    p = s.plan([VM1], {"az": "AZ1", "mode": "live", "verify": "none"})
    b = p.json.get("batch") or {}
    row = (p.json.get("rows") or [{}])[0]
    ok("F-2 plan 1 VM: PLAN-OK with checks, effective (mode from cli), a batch: name rule, 32-hex token, 16-hex id, "
       "the verbatim command",
       p.status == 200 and row.get("result") == "PLAN-OK" and row["effective"]["mode"] == {"value": "live", "from": "cli"}
       and b.get("rule") == {"mode": "name", "expect": ["app01"], "ids": [VM1]} and re.match(r"^[0-9a-f]{32}$", b.get("token", ""))
       and re.match(r"^[0-9a-f]{16}$", b.get("id", "")) and b["commands"] == [
           ["migrate", "migrate", VM1, "--mode", "live", "--az", "AZ1", "--verify", "none", "--yes", "--progress-json"]],
       p)
    plan_calls = [c for c in s.calls() if c[0] == "plan"]
    ok("F-2 the runner planned with 'migrate plan <id> <options> --json' (argv, no shell)",
       plan_calls[-1] == ["plan", VM1, "--mode", "live", "--az", "AZ1", "--verify", "none", "--json"], plan_calls)
    r = s.run_batch(b, "app01")
    jid = (r.json.get("jobs") or [""])[0]
    ok("F-2 run with the VM name typed: 202, one job id of 16 hex", r.status == 202 and re.match(r"^[0-9a-f]{16}$", jid), r)
    st, ev = s.stream(jid)
    kinds = [e[0] for e in ev]
    prog = [e[1]["event"]["event"] for e in ev if e[0] == "progress"]
    lines = [e[1]["text"] for e in ev if e[0] == "line"]
    ok("F-2 SSE stream: state, log lines, progress events (phase, t_stop, downtime, result) and end rc 0",
       st == 200 and kinds[0] == "state" and kinds[-1] == "end" and ev[-1][1]["rc"] == 0
       and {"phase", "t_stop", "downtime", "result"} <= set(prog) and any("Totals: OK 1" in x for x in lines),
       (st, kinds[:5], prog, lines[-3:]))
    j = s.wait_job(jid)
    ok("F-2 job record: argv, effective, conf snapshot, projects, session, timestamps, rc 0, t_stop, dst server, "
       "batch log, state dir",
       (j["state"] == "OK" and j["rc"] == 0 and j["argv"] == b["commands"][0] and j["effective"]["az"]["value"] == "AZ1"
           and "path" in j["conf"] and j["projects"] == {"src": "admin", "dst": "admin"}
           and all(j[k] for k in ("t_plan", "t_confirm", "t_start", "t_end", "t_stop", "dst_server_id", "batch_log"))
           and j["state_dir"] == os.path.join(s.state_root, VM1) and re.match(r"^[0-9a-f]{16}$", j["session"])), j)
    ok("F-2 (17) the job record keeps the real plan's checks (every level, OK included) as the plan row had them",
       j.get("checks") == row.get("checks") and len(row.get("checks") or []) > 0
       and any(c.get("level") == "OK" for c in row["checks"]), (len(row.get("checks") or []), len(j.get("checks") or [])))
    rec = os.path.join(s.lib, "jobs", jid + ".json")
    logf = os.path.join(s.lib, "jobs", jid + ".log")
    ok("F-2 on disk: jobs/ 0700, the ended record 0400 (immutable), its log 0600",
       stat.S_IMODE(os.stat(os.path.join(s.lib, "jobs")).st_mode) == 0o700 and stat.S_IMODE(os.stat(rec).st_mode) == 0o400
       and stat.S_IMODE(os.stat(logf).st_mode) == 0o600,
       [oct(stat.S_IMODE(os.stat(x).st_mode)) for x in (os.path.join(s.lib, "jobs"), rec, logf)])
    ok("F-2 (16.1) users file 0640 (written by migui-passwd); no token file any more",
       stat.S_IMODE(os.stat(s.users).st_mode) == 0o640 and not os.path.exists(os.path.join(s.lib, "token")))
    ok("F-2 (16.3) the batch and the job record carry operator = the logged-in user; so does the history",
       b.get("operator") == H.USER and j.get("operator") == H.USER
       and s.post("/api/status").json["jobs"][0].get("operator") == H.USER, (b.get("operator"), j.get("operator")))
    with open(rec) as f:
        ok("F-2 (16.3) on disk: the sealed job record has operator", json.load(f).get("operator") == H.USER)
    lg = s.get("/api/jobs/%s/log" % jid)
    ok("F-2 log download: text/plain attachment with the lines and the @@ events",
       lg.status == 200 and "attachment" in lg.headers.get("content-disposition", "") and "@@ " in lg.raw.decode(), lg)
    st2, ev2 = s.stream(jid, last=len(ev) - 5)
    ok("F-2 the stream of an ended job replays from Last-Event-ID", st2 == 200 and ev2[-1][0] == "end"
       and all(e[1]["n"] > len(ev) - 5 for e in ev2 if e[0] in ("line", "progress")), ev2[:3])
    acts = [(a["who"], a["action"]) for a in s.audit_lines()]
    ok("F-2 audit: login, doctor, plan (web+runner), run (web+runner), start, end",
       {("web", "login"), ("web", "doctor"), ("web", "plan"), ("runner", "plan"), ("web", "run"), ("runner", "run"),
        ("runner", "start"), ("runner", "end")} <= set(acts), acts)
    web_u = {a["action"]: (a.get("detail") or {}).get("user") for a in s.audit_lines()
             if a["who"] == "web" and a["action"] in ("login", "doctor", "plan", "run")}
    run_u = {a["action"]: (a.get("detail") or {}).get("operator") for a in s.audit_lines()
             if a["who"] == "runner" and a["action"] in ("plan", "run", "start", "end")}
    ok("F-2 (16.3) audit: the web's login, doctor, plan and run lines carry user=test, the runner's plan, run, start "
       "and end lines operator=test",
       web_u == dict.fromkeys(("login", "doctor", "plan", "run"), H.USER)
       and run_u == dict.fromkeys(("plan", "run", "start", "end"), H.USER), (web_u, run_u))
    h = s.post("/api/status")
    ok("F-2 history: POST /api/status = runner jobs + migrate status --json (the VM's state is live)",
       h.status == 200 and h.json["jobs"][0]["id"] == jid and [e["ident"] for e in h.json["cli"]["live"]] == [VM1], h)
    l2 = s.post("/api/list", {"hosts": ["cmp1"]})
    st_row = next(r for r in l2.json["data"]["rows"] if r["id"] == VM1)
    p2 = s.plan([VM1], AZ)
    ok("F-2 after the migrate: list shows the live state, a new plan of that VM is SKIP(state-exists) and gives no batch",
       st_row["state"]["live"] is True and p2.json["rows"][0]["result"] == "SKIP(state-exists)" and p2.json["batch"] is None,
       (st_row["state"], p2))

    # ------------------------------------------------------------ F-3 rollback (name, original options)
    rb = s.post("/api/rollback", {"ident": VM1})
    rbb = rb.json.get("batch") or {}
    ok("F-3 rollback prepare: one 'migrate rollback <ident>' command, name rule, the original options.json",
       rb.status == 200 and rbb["commands"] == [["migrate", "rollback", VM1, "--yes"]] and rbb["rule"] == {"mode": "name", "expect": ["app01"],
                                                                                              "alt": [VM1], "ids": [VM1]}
       and rb.json["original"]["effective"]["mode"]["value"] == "live", rb)
    bad = s.run_batch(rbb, "2")
    rb = s.post("/api/rollback", {"ident": VM1})
    good = s.run_batch(rb.json["batch"], "app01")
    jr = s.wait_job(good.json["jobs"][0]) if good.status == 202 else {}
    h = s.post("/api/status")
    ok("F-3 rollback: a count instead of the name is 409; the name is 202, the job OK, the state no longer live",
       bad.status == 409 and good.status == 202 and jr.get("state") == "OK" and jr.get("kind") == "rollback"
       and not h.json["cli"]["live"], (bad, good, jr.get("state")))
    nolive = s.post("/api/rollback", {"ident": VM2})
    ok("F-3 rollback of a VM without a live state: 409, nothing planned", nolive.status == 409, nolive)

    # ------------------------------------------------------------ F-4 batch of 2: count rule, one after the other
    p = s.plan([VM1, VM2], AZ)
    b = p.json["batch"]
    ok("F-4 plan 2 VMs: rule count, expect '2'", b["rule"] == {"mode": "count", "expect": ["2"]}, b)
    r = s.run_batch(b, "2")
    ids = r.json.get("jobs") or []
    s.wait_idle()
    js = [s.get("/api/jobs/" + x).json["job"] for x in ids]
    ok("F-4 typed '2': 202, two jobs, both OK, the second started after the first ended",
       r.status == 202 and len(js) == 2 and [x["state"] for x in js] == ["OK", "OK"] and js[1]["t_start"] >= js[0]["t_end"],
       [(x["state"], x["t_start"], x["t_end"]) for x in js])
    for x in (VM1, VM2):
        rbx = s.post("/api/rollback", {"ident": x}).json["batch"]
        s.run_batch(rbx, rbx["names"][0])
        s.wait_idle()

    # ------------------------------------------------------------ F-5 keep-source-ports / --ip addr: names
    p = s.plan([VM1, VM2], {"az": "AZ1", "keep_source_ports": True})
    b = p.json["batch"]
    ok("F-5 keep-source-ports on 2 VMs: rule name, both names", b and b["rule"] == {"mode": "name", "expect": ["app01", "app02"], "ids": [VM1, VM2]}, p)
    cnt = s.run_batch(b, "2")
    b = s.plan([VM1, VM2], {"az": "AZ1", "keep_source_ports": True}).json["batch"]
    names = s.run_batch(b, "app02\napp01")
    s.wait_idle()
    ok("F-5 keep-source-ports: the count is 409, every name (any order, one per line) is 202",
       cnt.status == 409 and names.status == 202, (cnt, names))
    for x in (VM1, VM2):
        rbx = s.post("/api/rollback", {"ident": x}).json["batch"]
        s.run_batch(rbx, rbx["names"][0])
        s.wait_idle()
    p = s.plan([VM1, VM2], AZ, ips={VM1: "198.51.100.77"})
    b = p.json.get("batch") or {}
    ok("F-5 --ip <addr> on one row: that row's command carries --ip 198.51.100.77, rule name",
       b.get("rule", {}).get("mode") == "name" and b["commands"][0][-4:] == ["--ip", "198.51.100.77", "--yes", "--progress-json"]
       and "--ip" not in b["commands"][1], p)
    ok("F-5 dst_host in the az: the command carries --dst-host",
       (s.plan([VM1], {"az": "AZ1", "dst_host": "dst-cmp1"}).json.get("batch") or {}).get("commands", [[]])[0][3:7]
       == ["--az", "AZ1", "--dst-host", "dst-cmp1"])

    # ------------------------------------------------------------ F-6 stop on FAIL, continue-on-error
    s.control({"modes": {VM1: "fail"}, "mode": "slow", "steps": 4, "step_secs": 0.05})
    r = s.run_batch(s.plan([VM1, VM2], AZ).json["batch"], "2")
    s.wait_idle()
    js = [s.get("/api/jobs/" + x).json["job"] for x in r.json["jobs"]]
    ok("F-6 the first VM FAILs: the batch stops, the second is SKIPPED (default 'dừng đợt nếu FAIL')",
       [x["state"] for x in js] == ["FAIL", "SKIPPED"] and js[0]["rc"] == 1 and js[0]["t_stop"], [x["state"] for x in js])
    rbx = s.post("/api/rollback", {"ident": VM1}).json["batch"]
    s.run_batch(rbx, "app01")
    s.wait_idle()
    r = s.run_batch(s.plan([VM1, VM2], AZ).json["batch"], "2", cont=True)
    s.wait_idle()
    js = [s.get("/api/jobs/" + x).json["job"] for x in r.json["jobs"]]
    ok("F-6 continue_on_error: FAIL then OK", [x["state"] for x in js] == ["FAIL", "OK"], [x["state"] for x in js])
    for x in (VM1, VM2):
        rbx = s.post("/api/rollback", {"ident": x}).json.get("batch")
        if rbx:
            s.run_batch(rbx, rbx["names"][0])
            s.wait_idle()

    # ------------------------------------------------------------ F-7 cancel: SIGINT, stop the batch
    s.control({"mode": "slow", "steps": 100, "step_secs": 0.1})
    r = s.run_batch(s.plan([VM1, VM2], AZ).json["batch"], "2")
    jid = r.json["jobs"][0]
    s.wait_job(jid, states=("RUNNING",))
    t0 = time.time()
    while not s.get("/api/jobs/" + jid).json["job"].get("t_stop") and time.time() - t0 < 20:
        time.sleep(0.2)  # cancel after the cutover began: the state stays for a rollback
    c1 = s.post("/api/jobs/%s/cancel" % jid, {"force": False})
    early = s.post("/api/jobs/%s/cancel" % jid, {"force": True, "typed": "app01"})
    j = s.wait_job(jid)
    s.wait_idle()
    j2 = s.get("/api/jobs/" + r.json["jobs"][1]).json["job"]
    acts = [a["action"] for a in s.audit_lines()]
    ok("F-7 cancel: 200 with force_in; SIGKILL right away is 409; the job ends CANCELLED (rc 130, it cleaned up), "
       "the rest of the batch SKIPPED, audit 'cancel'",
       c1.status == 200 and c1.json.get("force_in") == 2 and early.status == 409 and j["state"] == "CANCELLED"
       and j["rc"] == 130 and j["cancel"]["sigint_at"] and j2["state"] == "SKIPPED" and "cancel" in acts,
       (c1, early, j.get("state"), j.get("rc"), j2.get("state")))
    rbx = s.post("/api/rollback", {"ident": VM1}).json.get("batch")
    ok("F-7 after a cancel past t-stop the state is live: rollback offered", bool(rbx))
    if rbx:
        s.run_batch(rbx, "app01")
        s.wait_idle()

    # ------------------------------------------------------------ F-8 SIGKILL: second cancel after kill_after, typed
    s.control({"mode": "stubborn", "steps": 200, "step_secs": 0.1})
    r = s.run_batch(s.plan([VM2], AZ).json["batch"], "app02")
    jid = r.json["jobs"][0]
    s.wait_job(jid, states=("RUNNING",))
    time.sleep(0.5)
    s.post("/api/jobs/%s/cancel" % jid, {"force": False})
    time.sleep(1.0)
    still = s.get("/api/jobs/" + jid).json["job"]["state"]
    time.sleep(1.3)
    wrong = s.post("/api/jobs/%s/cancel" % jid, {"force": True, "typed": "app01"})
    kill = s.post("/api/jobs/%s/cancel" % jid, {"force": True, "typed": "app02"})
    j = s.wait_job(jid)
    ok("F-8 a migrate that ignores SIGINT keeps RUNNING; after kill_after a SIGKILL with the wrong name is 409, with "
       "the right name 200: CANCELLED rc -9, audit 'kill'",
       still == "RUNNING" and wrong.status == 409 and kill.status == 200 and j["state"] == "CANCELLED" and j["rc"] == -9
       and j["cancel"].get("sigkill_at") and any(a["action"] == "kill" and a["who"] == "runner" for a in s.audit_lines()),
       (still, wrong, kill, j.get("state"), j.get("rc")))
    s.wait_idle()

    # ------------------------------------------------------------ F-9 stop after the current job
    s.control({"mode": "slow", "steps": 15, "step_secs": 0.1})
    st_ = s.post("/api/status").json["cli"]["live"]
    for e in st_:
        rbx = s.post("/api/rollback", {"ident": e["ident"]}).json["batch"]
        s.run_batch(rbx, rbx["names"][0])
        s.wait_idle()
    r = s.run_batch(s.plan([VM1, VM2], AZ).json["batch"], "2")
    act = s.get("/api/jobs").json["active"]
    stp = s.post("/api/batches/%s/stop" % act["id"], {})
    s.wait_idle()
    js = [s.get("/api/jobs/" + x).json["job"]["state"] for x in r.json["jobs"]]
    ok("F-9 'Dừng sau con hiện tại': the running job finishes OK, the next is SKIPPED", stp.status == 200
       and js == ["OK", "SKIPPED"], (stp, js))
finally:
    s.stop()

# ---------------------------------------------------------------- F-10 volumes
s = H.Stack()
try:
    s.ready()
    l = s.post("/api/list", {"projects": ["admin"], "volumes": True, "scope": "available"})
    rows = (l.json.get("data") or {}).get("rows") or []
    ok("F-10 list --volumes --available of project admin: the available volume", [r["id"] for r in rows] == [VOL1], l)
    inuse = s.plan([VOL1], {"in_use": True}, kind="vol")
    p = s.plan([VOL1], {"az": "AZ1", "verify": "none"}, kind="vol")
    b = p.json.get("batch") or {}
    ok("F-10 in_use on an available volume: 400; plan volume: READY, name rule, command 'migrate volume <id> --az AZ1 "
       "--verify none --yes --progress-json'",
       inuse.status == 400 and p.json["rows"][0]["result"] == "READY" and b.get("rule") == {"mode": "name", "expect": ["data-archive-01"], "ids": [VOL1]}
       and b["commands"] == [["migrate", "volume", VOL1, "--az", "AZ1", "--verify", "none", "--yes", "--progress-json"]], (inuse, p))
    s.control({"effective_extra": {"leak": {"value": "OS_PASSWORD=HUNTER-TWO-NOT-REAL", "from": "cli"}}})
    vm_run = s.post("/api/batches/%s/run" % b["id"], {"typed": "data-archive-01", "confirm_token": b["token"]})
    jid = vm_run.json["jobs"][0]
    j = s.wait_job(jid)
    h = s.post("/api/status").json
    ok("F-10 run the volume: 202, OK, state vol-<id> live; rollback vol-<id> is offered",
       vm_run.status == 202 and j["state"] == "OK" and j["kind"] == "vol" and [e["ident"] for e in h["cli"]["live"]] == ["vol-" + VOL1]
       and s.post("/api/rollback", {"ident": "vol-" + VOL1}).status == 200, (vm_run, j.get("state"), h["cli"]["live"]))
    eff = j.get("effective") or {}
    ok("F-10b the finished volume job records the effective options migrate volume ran with (READY had none at plan)",
       (eff.get("verify") or {}).get("value") == "none" and (eff.get("verify") or {}).get("from") == "cli"
       and (eff.get("az") or {}).get("value") == "AZ1", eff)
    ok("F-10c the effective options read from options.json are scrubbed like every CLI JSON (T9)",
       "HUNTER-TWO" not in json.dumps(eff) and "scrubbed" in json.dumps(eff.get("leak")), eff.get("leak"))
    s.control({})
finally:
    s.stop()

# ---------------------------------------------------------------- F-10d S4 (1.2.4): an ended job record is complete
# The runner used to publish the ended state before the notify hook, state_dir, the effective options and t_end, so a
# reader in that window saw state OK with t_end/effective None. A notify hook that marks its start then sleeps 0.5 s
# widens the window; the job is read without a pause (one job, then the listing, in turn) until it has ended.
mark = os.path.join(H.WORK, "f10d-notify-%d" % os.getpid())
if os.path.exists(mark):
    os.unlink(mark)
os.environ["MIGUI_NOTIFY_CMD"] = json.dumps([H.PY, "-I", "-c", "import sys, time\nopen(sys.argv[1], 'a').close()\n"
                                             "time.sleep(0.5)\n", mark])
s = H.Stack()
try:
    s.ready()
    s.post("/api/list", {"projects": ["admin"], "volumes": True, "scope": "available"})  # the runner's whitelist
    b = s.plan([VOL1], {"az": "AZ1", "verify": "none"}, kind="vol").json.get("batch") or {}
    r = s.post("/api/batches/%s/run" % b.get("id"), {"typed": "data-archive-01", "confirm_token": b.get("token")})
    jid = ((r.json or {}).get("jobs") or [""])[0]
    ENDED = ("OK", "FAIL", "CANCELLED", "SKIPPED", "LOST", "DETACHED")
    seen, in_hook, bad, last = 0, [], [], {}
    t0 = time.time()
    while r.status == 202 and time.time() - t0 < 120:
        hook = os.path.exists(mark)  # read before the record: a sample marked in_hook was taken after the hook began
        if seen % 2:
            rec = next((x for x in (s.get("/api/jobs").json or {}).get("jobs") or [] if x.get("id") == jid), {})
        else:
            rec = (s.get("/api/jobs/" + jid).json or {}).get("job") or {}
        seen += 1
        last = rec
        st = rec.get("state")
        if hook and st not in ENDED:
            in_hook.append((st, rec.get("rc"), rec.get("t_end")))
        if st in ENDED:
            eff = rec.get("effective") or {}
            if (rec.get("t_end") is None or rec.get("rc") is None or rec.get("state_dir") is None
                    or rec.get("notify_rc") is None or not isinstance(rec.get("effective"), dict)
                    or "verify" not in eff or "az" not in eff):
                bad.append({k: rec.get(k) for k in ("state", "rc", "t_end", "state_dir", "notify_rc", "effective")})
            if rec.get("t_end") is not None:  # complete: the end is published (an incomplete one is read again)
                break
    ok("F-10d S4 (1.2.4) every ended record read during the end of a volume job has rc, t_end, state_dir, notify_rc "
       "and effective {verify, az}; the job ends OK, notify_rc 0",
       r.status == 202 and not bad and last.get("state") == "OK" and last.get("notify_rc") == 0,
       (r.status, seen, len(bad), bad[:2], {k: last.get(k) for k in ("state", "rc", "t_end", "notify_rc")}))
    ok("F-10d S4 (1.2.4) reads landed inside the 0.5 s notify hook and the job still showed RUNNING there (rc, t_end "
       "None): the hook runs before the end is published",
       len(in_hook) > 0 and all(x == ("RUNNING", None, None) for x in in_hook), (seen, len(in_hook), in_hook[:3]))
finally:
    s.stop()
    os.environ.pop("MIGUI_NOTIFY_CMD", None)
    try:
        os.unlink(mark)
    except OSError:
        pass

# ---------------------------------------------------------------- F-11 one plan per session at a time
s = H.Stack()
try:
    s.ready()
    s.control({"plan_delay": 1.5})
    res = {}

    def go(k, ident):
        res[k] = s.plan([ident], AZ).status
    ts = [threading.Thread(target=go, args=("a", VM1)), threading.Thread(target=go, args=("b", VM2))]
    ts[0].start()
    time.sleep(0.3)
    ts[1].start()
    [t.join() for t in ts]
    ok("F-11 two plans of one session at once: 200 and 429", sorted(res.values()) == [200, 429], res)
finally:
    s.stop()

# ---------------------------------------------------------------- F-12 runner restart under a running job (8)
s = H.Stack(control={"mode": "slow", "steps": 60, "step_secs": 0.1})
try:
    s.ready()
    r = s.run_batch(s.plan([VM1], AZ).json["batch"], "app01")
    jid = r.json["jobs"][0]
    s.wait_job(jid, states=("RUNNING",))
    pid = s.get("/api/jobs/" + jid).json["job"]["pid"]
    s.runner.terminate()
    s.runner.wait(10)
    alive = os.path.exists("/proc/%d" % pid)
    s.runner = subprocess.Popen(s.runner.args, stdout=s.runner_log, stderr=subprocess.STDOUT,
                                env=s.env)
    s.wait(lambda: os.path.exists(s.sock) and s.login().status == 200, "runner restart")
    j = s.get("/api/jobs/" + jid).json["job"]
    ok("F-12 runner stopped while a migrate runs: migrate keeps running (own session); the restarted runner marks "
       "the job DETACHED, never RUNNING again; the same account logs in again",
       alive and j["state"] == "DETACHED", (alive, j.get("state")))
    s.control({"mode": "slow", "steps": 2, "step_secs": 0.01, "running": [
        {"pid": pid, "cmdline": ["/usr/bin/python3", "/opt/mig-venv/bin/migrate", "migrate", VM1, "--yes"]}]})
    s.post("/api/doctor")
    s.post("/api/inventory")
    s.post("/api/list", {"hosts": ["cmp1"]})
    r = s.run_batch(s.plan([VM2], AZ).json["batch"], "app02")
    ok("F-12 a new run while 'migrate status' shows a migrate outside the UI: 423", r.status == 423, r)
    try:
        os.kill(pid, 9)
    except OSError:
        pass
finally:
    s.stop()

# ---------------------------------------------------------------- F-14 "— mọi host —": every inventory host
s = H.Stack()
try:
    s.login()
    s.post("/api/doctor")
    inv = s.post("/api/inventory").json["data"]
    every = [h["host"] for h in inv["source"]["hosts"] if h["state"] == "up"]
    n0 = len(s.calls())
    l = s.post("/api/list", {"hosts": every, "projects": []})
    ok("F-14 'mọi host' without a project = one --host per up source host of the inventory, in its order: the "
       "runner runs exactly that and returns that argv for the UI to show",
       every == ["cmp1", "cmp2"] and l.status == 200
       and l.json["argv"] == ["migrate", "list", "--json", "--host", "cmp1", "--host", "cmp2"]
       and s.calls()[n0:] == [["list", "--json", "--host", "cmp1", "--host", "cmp2"]]
       and len(l.json["data"]["rows"]) == 3, (every, l, s.calls()[n0:]))
    n0 = len(s.calls())
    big = s.post("/api/list", {"hosts": ["cmp1"] * 129, "projects": []})
    ok("F-14 more than 128 hosts (LIST_MAX, C13): 400 from the runner, nothing listed",
       big.status == 400 and "at most 128" in (big.json or {}).get("error", "") and len(s.calls()) == n0, big)
    n0 = len(s.calls())
    full = s.post("/api/list", {"hosts": every * 64, "projects": []})
    ok("F-14 exactly 128 hosts: accepted by the runner's count check, listed with 128 --host",
       len(every * 64) == 128 and full.status == 200
       and s.calls()[n0:] == [["list", "--json"] + sum([["--host", h] for h in every * 64], [])],
       (full, s.calls()[n0:]))
    js = open(os.path.join(H.REPO, "migui", "static", "app.js"), encoding="utf-8").read()
    rn = open(os.path.join(H.REPO, "migui", "runner.py"), encoding="utf-8").read()
    hmax = re.search(r"const HOSTS_MAX = (\d+);", js)
    ok("F-14 app.js sends srcHostsForAll(S.inv) when neither host nor project is chosen, stops above HOSTS_MAX (at most "
       "the runner's own limit LIST_MAX = 128, C13); the old 'Chọn ít nhất host hoặc project' refusal is gone",
       "hosts = srcHostsForAll(S.inv);" in js and hmax and int(hmax.group(1)) <= 128 and "hosts.length > HOSTS_MAX" in js
       and "LIST_MAX = 128 " in rn and "len(hosts) > LIST_MAX" in rn
       and "Chọn ít nhất host hoặc project" not in js)
finally:
    s.stop()

# ---------------------------------------------------------------- F-21 several source hosts and projects (21)
VOL_OTHER = "c91b2e4d-0000-4000-8000-00000000b001"   # fake_migrate.ui_world: project 'other', available
s = H.Stack()
try:
    s.login()
    s.post("/api/doctor")
    inv = s.post("/api/inventory").json["data"]
    projs = sorted(p["name"] for p in inv["source"]["projects"])
    n0 = len(s.calls())
    l = s.post("/api/list", {"hosts": ["cmp1", "cmp2"], "projects": ["admin", "other"]})
    want = ["list", "--json", "--host", "cmp1", "--host", "cmp2", "--project", "admin", "--project", "other"]
    ok("F-21 list with 2 hosts + 2 projects (what the multi-select sends): the fake CLI gets --host x2 and --project "
       "x2 in that order, the argv shown to the UI is the same; the three VMs of admin on cmp1",
       projs == ["admin", "other"] and l.status == 200 and s.calls()[n0:] == [want]
       and l.json["argv"] == ["migrate"] + want
       and sorted(r["id"] for r in l.json["data"]["rows"]) == [VM1, VM2, VM3], (projs, l, s.calls()[n0:]))
    n0 = len(s.calls())
    v = s.post("/api/list", {"projects": ["admin", "other"], "volumes": True, "scope": "available"})
    want = ["list", "--json", "--project", "admin", "--project", "other", "--volumes", "--available"]
    rows = (v.json.get("data") or {}).get("rows") or []
    ok("F-21 volume list with 2 projects: the fake CLI gets 'list --json --project admin --project other --volumes "
       "--available', and lists the available volume of each project",
       v.status == 200 and s.calls()[n0:] == [want] and v.json["argv"] == ["migrate"] + want
       and sorted((r["id"], (r.get("project") or {}).get("src")) for r in rows)
       == sorted([(VOL1, "admin"), (VOL_OTHER, "other")]),
       (v, s.calls()[n0:]))
    p = s.plan([VOL1, VOL_OTHER], {"az": "AZ1", "verify": "none"}, kind="vol")
    b = p.json.get("batch") or {}
    pl = [d for n, d in (p.json or {}).get("events") or [] if n == "planning"]
    prow = {r.get("ident"): r for r in p.json.get("rows") or []}
    ok("F-21 plan of volumes ticked from 2 projects: one 'plan volume --project <p>' run per project (18 "
       "planning.idents), each row carries its own project's argv; both READY, one batch with both commands",
       sorted(d.get("idents") for d in pl) == sorted([[VOL1], [VOL_OTHER]])
       and (prow.get(VOL1) or {}).get("argv", [])[:5] == ["migrate", "plan", "volume", "--project", "admin"]
       and (prow.get(VOL_OTHER) or {}).get("argv", [])[:5] == ["migrate", "plan", "volume", "--project", "other"]
       and sorted(r["result"] for r in prow.values()) == ["READY", "READY"]
       and sorted(c[2] for c in b.get("commands") or []) == sorted([VOL1, VOL_OTHER]), p)
finally:
    s.stop()

# ---------------------------------------------------------------- F-15 buttons lock while their request runs (15)
def _match(js, i):
    """Index just past the bracket closing the one at js[i], skipping JS string literals."""
    pairs, depth, q = {"(": ")", "{": "}", "[": "]"}, [], None
    j = i
    while j < len(js):
        ch = js[j]
        if q:
            if ch == "\\":
                j += 2
                continue
            if ch == q:
                q = None
        elif ch in "'\"`":
            q = ch
        elif ch in pairs:
            depth.append(pairs[ch])
        elif depth and ch == depth[-1]:
            depth.pop()
            if not depth:
                return j + 1
        j += 1
    return len(js)


def ui_handlers(js):
    """[(where, body)] of every click/submit handler: addEventListener('click'|'submit', ...) and el(..., {on: {click}})."""
    found = []
    for m in re.finditer(r"(\$\('([\w-]+)'\)|\w+)\.addEventListener\('(click|submit)',", js):
        o = js.index("(", m.start() + len(m.group(1)))
        found.append((m.group(2) or m.group(1), js[o:_match(js, o)]))
    for m in re.finditer(r"on: \{ click: ", js):
        o = m.start() + 4
        found.append(("on:click@%d" % js.count("\n", 0, m.start()), js[o:_match(js, o)]))
    return found


def js_functions(js):
    out = {}
    for m in re.finditer(r"(?:async )?function (\w+)\(", js):
        o = js.index("{", m.end())
        out[m.group(1)] = js[o:_match(js, o)]
    return out


js_src = open(os.path.join(H.REPO, "migui", "static", "app.js"), encoding="utf-8").read()
funcs = js_functions(js_src)
unsafe = {n for n, b in funcs.items() if n != "api" and "api(" in b and "busy(" not in b}
bad = []
hs = ui_handlers(js_src)
for where, body in hs:
    calls_api = "api(" in body or any(re.search(r"\b%s\b" % n, body) for n in unsafe)  # called or passed
    if calls_api and "busy(" not in body:
        bad.append(where)
ok("F-15 static: every click/submit handler that reaches the API (directly or through %s) goes through busy(); "
   "busy() guards a second click and restores the button in finally" % sorted(unsafe),
   len(hs) >= 20 and not bad and "btn.dataset.busy === '1' || btn.disabled) return undefined" in js_src
   and "} finally {\n      delete btn.dataset.busy;" in js_src, (len(hs), bad))
node = shutil.which("node")
if node:
    p = subprocess.run([node, os.path.join(H.HERE, "guards.mjs"), H.REPO], capture_output=True, text=True, timeout=120)
    ok("F-15 page logic in node (tests/ui/guards.mjs): double login submit = 1 POST, double plan click = 1 POST with "
       "both plan buttons locked, double Rollback click = 1 POST, unlock after an error; the plan content of 17 on the "
       "confirm screen and in the history detail; the plan panel of 18; the automatic Doctor and '#confirm' of 19; the live plan lines of 20; the multi-select source boxes of 21",
       p.returncode == 0 and "TOTAL pass=%d fail=0" % GUARDS in p.stdout, p.stdout + p.stderr)
else:
    print("SKIP F-15 page logic in node (tests/ui/guards.mjs): no node on this host; run it where node is installed: "
          "node tests/ui/guards.mjs <repo>", flush=True)
s = H.Stack(control={"plan_delay": 2})
try:
    s.ready()
    res = {}

    def go(k, ident):
        res[k] = s.plan([ident], AZ).status
    n0 = len([c for c in s.calls() if c[0] == "plan"])
    ts = [threading.Thread(target=go, args=("a", VM1)), threading.Thread(target=go, args=("b", VM2))]
    ts[0].start()
    time.sleep(0.3)
    ts[1].start()
    [t.join() for t in ts]
    n1 = len([c for c in s.calls() if c[0] == "plan"])
    ok("F-15 server side: a second plan of the session while the first runs (2 s) is refused 429 and migrate plan ran "
       "exactly once", sorted(res.values()) == [200, 429] and n1 - n0 == 1, (res, n1 - n0))
    s.cookie = ""
    res = []

    def bad_login():
        res.append(s.login(password="wrong-password-x").status)
    before = sum(1 for a in s.audit_lines() if a["action"] == "login-failed")
    ts = [threading.Thread(target=bad_login) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    after = sum(1 for a in s.audit_lines() if a["action"] == "login-failed")
    ok("F-15 server side: two wrong logins sent at once = exactly two 'login-failed' audit lines (one per request "
       "actually sent; the page sends one, GUARD-1)", res == [401, 401] and after - before == 2, (res, after - before))
finally:
    s.stop()

# ---------------------------------------------------------------- F-16 the plan content goes with the job (17)
C3 = [{"level": "OK", "text": "source VM ACTIVE"}, {"level": "WARN", "text": "flavor mapped by name"},
      {"level": "FAIL", "text": "quota RAM short <b>x</b>"}]
s = H.Stack(control={"plan_checks": C3, "mode": "slow", "steps": 2, "step_secs": 0.01})
try:
    s.ready()
    p = s.plan([VM1], AZ)
    row = (p.json.get("rows") or [{}])[0]
    b = p.json.get("batch") or {}
    ok("F-16 (17) plan through the runner: the /api/plan row keeps all 3 checks (OK, WARN, FAIL) as migrate plan "
       "--json gave them, with counts, effective, log and the plan argv",
       p.status == 200 and row.get("checks") == C3 and row.get("counts") == {"ok": 1, "info": 0, "warn": 1, "fail": 1}
       and isinstance(row.get("effective"), dict) and row.get("log")
       and row.get("argv") == ["migrate", "plan", VM1, "--az", "AZ1", "--json"] and b.get("jobs"), p)
    r = s.run_batch(b, "app01")
    jid = (r.json.get("jobs") or [""])[0]
    j = s.wait_job(jid)
    h = s.post("/api/status").json
    hj = next((x for x in h.get("jobs") or [] if x.get("id") == jid), {})
    with open(os.path.join(s.lib, "jobs", jid + ".json")) as f:
        disk = json.load(f)
    ok("F-16 (17) the job record keeps the plan of that time: checks in GET /api/jobs/<id>, in the history "
       "(POST /api/status) and in the sealed record on disk",
       r.status == 202 and j.get("state") == "OK" and j.get("checks") == C3 and hj.get("checks") == C3
       and disk.get("checks") == C3, (r, j.get("state"), j.get("checks"), hj.get("checks"), disk.get("checks")))
    rbx = s.post("/api/rollback", {"ident": VM1}).json["batch"]
    jr = s.wait_job(s.run_batch(rbx, "app01").json["jobs"][0])
    ok("F-16 (17) a rollback job has no plan: checks null (the page shows 'không ghi nhận')",
       jr.get("state") == "OK" and "checks" in jr and jr["checks"] is None, jr)
    many = [{"level": ("OK", "INFO", "WARN", "FAIL")[n % 4], "text": "c%03d " % n + "x" * 800} for n in range(250)]
    s.control({"plan_checks": many, "mode": "slow", "steps": 2, "step_secs": 0.01})
    p = s.plan([VM2], AZ)
    row = (p.json.get("rows") or [{}])[0]
    r = s.run_batch(p.json["batch"], "app02")
    j = s.wait_job(r.json["jobs"][0])
    ck = j.get("checks") or []
    ok("F-16 (17) records stay small: a plan of 250 checks of 805 characters reaches the page whole, the job record "
       "keeps the first 200, each text cut to 500",
       len(row.get("checks") or []) == 250 and len(row["checks"][0]["text"]) == 805 and j.get("state") == "OK"
       and len(ck) == 200 and all(len(c["text"]) == 500 for c in ck)
       and [c["level"] for c in ck] == [m["level"] for m in many[:200]] and ck[199]["text"].startswith("c199 "),
       (len(row.get("checks") or []), len(ck), {len(c["text"]) for c in ck}))
finally:
    s.stop()

# ---------------------------------------------------------------- F-17 the plan as it happens (18)
# plan_jobs=1: these cases check the order of a sequential plan (C1's parallel plans: tests/ui/v1ui.py)
s = H.Stack(control={"plan_delay": 1, "mode": "slow", "steps": 2, "step_secs": 0.01}, plan_jobs=1)
try:
    s.ready()
    THREE = {"kind": "vm", "items": [{"ident": VM1}, {"ident": VM2}, {"ident": VM3}], "options": AZ}
    resp, evs, _pings = s.sse_post("/api/plan", THREE)
    names = [n for n, _d, _t in evs if n != "line"]  # the plan's own lines (20) are checked in F-20
    ok("F-17 (18) POST /api/plan answers text/event-stream, Cache-Control no-store: planning + row for each of 3 VMs in "
       "plan order, then batch, then end",
       resp.status == 200 and resp.headers.get("content-type", "").startswith("text/event-stream")
       and resp.headers.get("cache-control") == "no-store" and names == ["planning", "row"] * 3 + ["batch", "end"],
       (resp, resp.headers, names))
    pl = [d for n, d, _t in evs if n == "planning"]
    rows = [d for n, d, _t in evs if n == "row"]
    ok("F-17 (18) each planning names its ident, the listed name, i of n and a UTC time; each row is the one just "
       "announced",
       [(d.get("ident"), d.get("idents"), d.get("i"), d.get("n")) for d in pl]
       == [(VM1, [VM1], 1, 3), (VM2, [VM2], 2, 3), (VM3, [VM3], 3, 3)]
       and [d.get("name") for d in pl] == ["app01", "app02", XSS_NAME]
       and all(re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", d.get("t") or "") for d in pl)
       and [r.get("ident") for r in rows] == [VM1, VM2, VM3], (pl, [r.get("ident") for r in rows]))
    tt = {}
    for n, _d, t in evs:
        tt.setdefault(n, []).append(t)
    ok("F-17 (18) the events arrive while the plan runs (migrate plan takes >= 1 s per VM here): each planning >= 0.8 s "
       "before its row, the first row >= 1.5 s before the batch",
       len(tt.get("row", [])) == 3 and all(r - p >= 0.8 for p, r in zip(tt["planning"], tt["row"]))
       and tt["batch"][0] - tt["row"][0] >= 1.5, {k: [round(x - evs[0][2], 2) for x in v] for k, v in tt.items()})
    seq, cur, mixed = [], None, []
    for n, d, _t in evs:
        if n == "planning":
            cur = d.get("ident")
        elif n == "row":
            cur = None
        elif n == "line":
            seq.append(d)
            if d.get("ident") != cur or d.get("idents") != [cur]:
                mixed.append(d)
    ok("F-20 (20) with the CLI's own plan output: every 'line' event comes between the planning and the row of its "
       "ident, the 'Destination preflight' line is there, OK lines carry level OK, no '@@ ' line",
       seq and not mixed and any(d["text"].startswith("Destination preflight") for d in seq)
       and any(d["level"] == "OK" and d["text"].startswith("  OK    ") for d in seq)
       and not any(d["text"].startswith("@@ ") for d in seq), (len(seq), mixed[:3], [d.get("text") for d in seq[:40]]))
    b17 = next((d for n, d, _t in evs if n == "batch"), None) or {}

    # the browser goes away after the first row: the plan goes on, the web drains it and audits it
    n0 = len([c for c in s.calls() if c[0] == "plan"])
    a0 = len([a for a in s.audit_lines() if a["who"] == "web" and a["action"] == "plan"])
    resp, evs, _pings = s.sse_post("/api/plan", THREE, stop=lambda e: any(n == "row" for n, _d, _t in e))
    t_cut = time.time()
    while time.time() - t_cut < 60:
        if len([a for a in s.audit_lines() if a["who"] == "web" and a["action"] == "plan"]) > a0:
            break
        time.sleep(0.2)
    plans = [c for c in s.calls() if c[0] == "plan"][n0:]
    wa = ([a for a in s.audit_lines() if a["who"] == "web" and a["action"] == "plan"] or [{}])[-1]
    ra = ([a for a in s.audit_lines() if a["who"] == "runner" and a["action"] == "plan"] or [{}])[-1]
    wd, rd = wa.get("detail") or {}, ra.get("detail") or {}
    ok("F-18 (18) the client disconnects after the first row: the runner still plans all 3 VMs (3 migrate plan) and "
       "makes the batch; the web keeps draining and writes its 'plan' audit line at the end (same batch, client gone)",
       resp.status == 200 and [n for n, _d, _t in evs if n != "line"] == ["planning", "row"] and len(plans) == 3
       and rd.get("idents") == [VM1, VM2, VM3] and wd.get("batch") and wd.get("batch") == rd.get("batch")
       and wd.get("client") == "gone" and wa.get("rc") == 0, (evs, plans, wa, ra))
    again = s.plan([VM1], AZ)
    ok("F-18 (18) the session's next plan runs (200, the runner is free again)", again.status == 200
       and (again.json or {}).get("batch"), again)
    typed = "\n".join(b17.get("rule", {}).get("expect") or [])
    r = s.run_batch(b17, typed) if b17 else None
    ok("F-17 (18) the batch received on the stream is remembered by the session and runs (202)",
       r is not None and r.status == 202 and len(r.json.get("jobs") or []) == len(b17.get("jobs") or [0]), (b17, r))
    s.wait_idle()
finally:
    s.stop()

# ---------------------------------------------------------------- F-20 the lines of the plan being made (20)
s = H.Stack(control={"plan_lines": {"n": 6, "delay": 0.2}}, plan_jobs=1)  # sequential order checked (C1: v1ui.py)
try:
    s.ready()
    p = s.plan([VM1, VM2], AZ)
    evs = (p.json or {}).get("events") or []
    seen, by, cur = [], {VM1: [], VM2: []}, None
    for n, d in evs:
        if n == "planning":
            cur = d.get("ident")
        elif n == "row":
            seen.append(("row", d.get("ident"), len(by.get(d.get("ident"), []))))
            cur = None
        elif n == "line" and d.get("ident") == cur:
            by[cur].append(d)
    want_txt = ["VM %s status=ACTIVE host=cmp1", "  port port-src-1 198.51.100.50",
                "Destination preflight (mode=cold ip=same type=__DEFAULT__ pool=volumes):",
                "  OK    Ceph pool volumes: 12 GB needed, 900 GB left", "  WARN  flavor m1.small mapped by name",
                "  FAIL  quota RAM short by 2048 MB"]
    lines_all = [d for n, d in evs if n == "line"]
    ok("F-20 (20) fake plan printing 6 stderr lines 0.2 s apart (and one '@@ {...}'): for each of 2 VMs exactly six "
       "'line' events between its planning and its row, text as printed, level None/None/None/OK/WARN/FAIL, "
       "ident/idents of that VM, a UTC time; no '@@' line forwarded",
       p.status == 200 and seen == [("row", VM1, 6), ("row", VM2, 6)] and len(lines_all) == 12
       and all([d["text"] for d in by[v]] == [t.replace("%s", v) for t in want_txt] for v in (VM1, VM2))
       and all([d["level"] for d in by[v]] == [None, None, None, "OK", "WARN", "FAIL"] for v in (VM1, VM2))
       and all(d["idents"] == [d["ident"]] and re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", d.get("t") or "")
               for d in lines_all)
       and not any("@@" in d["text"] or "effective" in d["text"] for d in lines_all) and p.json.get("batch"),
       (seen, [(d.get("ident"), d.get("level"), d.get("text")) for d in lines_all]))
    resp, sevs, _pings = s.sse_post("/api/plan", {"kind": "vm", "items": [{"ident": VM1}], "options": AZ})
    tl = [t for n, d, t in sevs if n == "line"]
    tr = [t for n, d, t in sevs if n == "row"]
    ok("F-20 (20) the lines arrive as the plan prints them: the 6th >= 0.8 s after the 1st, the row after the 6th",
       len(tl) == 6 and tl[5] - tl[0] >= 0.8 and tr and tr[0] >= tl[5], [round(t - tl[0], 2) for t in tl + tr] if tl else sevs)

    s.control({"plan_lines": {"n": 1000, "delay": 0}})
    p = s.plan([VM1], AZ)
    evs = (p.json or {}).get("events") or []
    li = [d for n, d in evs if n == "line"]
    order = [n for n, _d in evs if n != "line"]
    last_line = max((i for i, (n, _d) in enumerate(evs) if n == "line"), default=-1)
    row_at = next((i for i, (n, _d) in enumerate(evs) if n == "row"), -1)
    ok("F-20 (20) fake plan printing 1000 lines: exactly 200 'line' events as printed, then one '… (còn 800 dòng, xem "
       "log plan)' (level None), all before the row; the plan still ends PLAN-OK|PLAN-FAIL with its batch",
       len(li) == 201 and [d["text"] for d in li[:6]] == [t.replace("%s", VM1) for t in want_txt]
       and [d["text"] for d in li[6:200]] == ["  OK    check %d" % k for k in range(6, 200)]
       and li[200]["text"] == "… (còn 800 dòng, xem log plan)" and li[200]["level"] is None
       and li[200]["ident"] == VM1 and order == ["planning", "row", "batch", "end"] and 0 <= last_line < row_at,
       (len(li), li[-1] if li else None, order))

    s.control({"plan_lines": {"n": 3, "delay": 0}})
    l = s.post("/api/list", {"projects": ["admin"], "volumes": True, "scope": "available"})
    p = s.plan([VOL1], {"az": "AZ1", "verify": "none"}, kind="vol")
    evs = (p.json or {}).get("events") or []
    pl = next((d for n, d in evs if n == "planning"), {})
    li = [d for n, d in evs if n == "line"]
    ok("F-20 (20) plan volume: the lines of the one 'migrate plan volume --project' run carry the idents of its "
       "planning event (the project's volumes)",
       l.status == 200 and pl.get("idents") == [VOL1] and len(li) == 3
       and all(d["ident"] == VOL1 and d["idents"] == [VOL1] for d in li)
       and [n for n, _d in evs] == ["planning", "line", "line", "line", "row", "batch", "end"], (pl, li, evs))
    ok("F-20 (20) no plan line goes to the audit", "Ceph pool volumes" not in open(s.audit).read()
       and "check 199" not in open(s.audit).read())
finally:
    s.stop()

# ---------------------------------------------------------------- F-13 paths and ids
s = H.Stack()
try:
    s.login()
    res = [s.get(p).status for p in ("/api/jobs/../../etc/passwd", "/api/jobs/ZZZZ", "/api/jobs/0123456789abcdef",
                                     "/static/../web.py", "/app.js/../../web.py", "/api/nothing")]
    ok("F-13 job paths: traversal and non-hex ids 404, an unknown id 404 from the runner, nothing outside static served",
       res == [404, 404, 404, 404, 404, 404], res)
    ok("F-13 PUT/DELETE on the API: 405", s.request("PUT", "/api/jobs").status == 405
       and s.request("DELETE", "/api/jobs").status == 405)
finally:
    s.stop()

sys.exit(H.total())
