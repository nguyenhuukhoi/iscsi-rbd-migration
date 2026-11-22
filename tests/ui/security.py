#!/usr/bin/env python3
"""The 10 security tests of DESIGN-UI section 10 and the login tests of 16.4, against a real migui-runner + migui-web
(harness.Stack) and the fake migrate. Each test is named SEC-UI-<n> after its line in section 10, SEC-UI-16-<n> after
its number in 16.4 (3 and 4 also run the web's own State/Handler in this process, to count hashlib.scrypt calls),
SEC-UI-18 / SEC-UI-19 / SEC-UI-20 after the sections of the plan stream, of the rollback display name and of the
live plan lines.

    python3 -I tests/ui/security.py <repo>
"""
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness as H  # noqa: E402
from harness import VM1, VM2, VM3, XSS_NAME, ok  # noqa: E402

sys.path.insert(0, H.REPO)
from migui import auth, web  # noqa: E402

LEAK = re.compile(r"OS_PASSWORD|SECRET|AQ[A-Za-z0-9+/=]{30,}|HUNTER-TWO|s3cr3t-value|gAAAAABfake")
STATIC = os.path.join(H.REPO, "migui", "static")


def migrate_procs():
    """fake migrate processes running now (ps), for 'no process was started'."""
    out = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
    return [l for l in out.splitlines() if re.search(r"-I \S*fake_migrate\.py", l) and "migui.runner" not in l]


s = H.Stack()
try:
    # ---------------------------------------------------------------- 1. no cookie: 401 on every API, static 200
    gets = ["/api/session", "/api/jobs", "/api/jobs/0123456789abcdef", "/api/jobs/0123456789abcdef/stream",
            "/api/jobs/0123456789abcdef/log"]
    posts = ["/api/logout", "/api/doctor", "/api/inventory", "/api/list", "/api/plan", "/api/rollback", "/api/status",
             "/api/batches/0123456789abcdef/run", "/api/batches/0123456789abcdef/stop",
             "/api/jobs/0123456789abcdef/cancel"]
    codes = {p: s.get(p).status for p in gets}
    codes.update({p: s.post(p, {}).status for p in posts})
    stat = {p: s.get(p).status for p in ("/", "/index.html", "/app.js", "/app.css")}
    ok("SEC-UI-1 without a cookie every API answers 401, the static pages 200",
       set(codes.values()) == {401} and set(stat.values()) == {200}, (codes, stat))
    s.cookie = "forged-" + "x" * 40
    ok("SEC-UI-1 a forged cookie is 401 too", s.get("/api/jobs").status == 401 and s.post("/api/doctor").status == 401)
    s.cookie = ""

    # ---------------------------------------------------------------- 3. CSRF: X-Requested-With, Origin, Host
    s.ready()
    body = {"kind": "vm", "items": [{"ident": VM1}], "options": {"az": "AZ1"}}
    n0 = len(s.calls())
    r_xrw = s.post("/api/plan", body, xrw=False)
    r_evil = s.post("/api/plan", body, headers={"Origin": "http://evil.example"})
    r_noorig = s.post("/api/plan", body, origin=False)
    r_sfs = s.post("/api/plan", body, headers={"Sec-Fetch-Site": "cross-site"})
    r_host = s.post("/api/plan", body, headers={"Host": "evil.example:8787"})
    r_ctype = s.request("POST", "/api/plan", raw=b'{"kind":"vm"}', headers={"Content-Type": "text/plain"})
    r_big = s.request("POST", "/api/plan", raw=b"{" + b" " * (70 * 1024) + b"}")
    ok("SEC-UI-3 POST /api/plan without X-Requested-With, with Origin http://evil, without Origin or Sec-Fetch-Site "
       "cross-site: 403; another Host: 421; text/plain: 415; body > 64 KB: 413; no migrate started",
       (r_xrw.status, r_evil.status, r_noorig.status, r_sfs.status, r_host.status, r_ctype.status, r_big.status)
       == (403, 403, 403, 403, 421, 415, 413) and len(s.calls()) == n0,
       (r_xrw, r_evil, r_noorig, r_sfs, r_host, r_ctype, r_big, s.calls()[n0:]))
    ok("SEC-UI-3 audit holds csrf-rejected", any(a["action"] == "csrf-rejected" for a in s.audit_lines()))
    n0 = len(s.calls())
    r_nc, ev_nc, _p = s.sse_post("/api/plan", body, cookie=False)
    r_nx, ev_nx, _p = s.sse_post("/api/plan", body, xrw=False)
    r_eo, ev_eo, _p = s.sse_post("/api/plan", body, headers={"Origin": "http://evil.example"})
    ok("SEC-UI-18 the plan stream keeps the checks of a POST: no cookie 401, no X-Requested-With 403, Origin evil 403, "
       "each a JSON answer (no event stream), no migrate started",
       (r_nc.status, r_nx.status, r_eo.status) == (401, 403, 403) and not (ev_nc or ev_nx or ev_eo)
       and all(r.headers.get("content-type", "").startswith("application/json") for r in (r_nc, r_nx, r_eo))
       and len(s.calls()) == n0, (r_nc, r_nx, r_eo, s.calls()[n0:]))

    # ---------------------------------------------------------------- 4. injection: 400, no process
    n0 = len(s.calls())
    bad = ["web-01; id", "$(id)", "--yes", "../x", "a/b", "`id`", "x\ny", "-x", "a" * 65, "", " web-01"]
    res = {}
    for ident in bad:
        res[ident] = s.post("/api/plan", {"kind": "vm", "items": [{"ident": ident}], "options": {}}).status
    opts_bad = [{"az": "AZ1 --yes"}, {"mode": "live; id"}, {"verify": "none\n"}, {"shell": "x"},
                {"max_passes": "5; id"}, {"max_passes": 51}, {"keep_source_ports": "yes"},
                {"security_group": ["a,b"]}, {"server_group": "../x"}, {"dst_host": "$(id)"}, {"flavor": "m1.small x"}]
    for o in opts_bad:
        res[str(o)] = s.post("/api/plan", {"kind": "vm", "items": [{"ident": VM1}], "options": o}).status
    res["ip"] = s.post("/api/plan", {"kind": "vm", "items": [{"ident": VM1, "ip": "1.2.3.4; id"}]}).status
    res["ip999"] = s.post("/api/plan", {"kind": "vm", "items": [{"ident": VM1, "ip": "999.1.1.1"}]}).status
    res["rollback"] = s.post("/api/rollback", {"ident": "../../etc"}).status
    res["list-host"] = s.post("/api/list", {"hosts": ["cmp1; id"]}).status
    res["list-host-unknown"] = s.post("/api/list", {"hosts": ["cmp9"]}).status
    res["not-listed"] = s.post("/api/plan", {"kind": "vm", "items": [{"ident": "8c1e4b90-2f1a-4c2e-9d0b-6a3e51f09a01"}]}).status
    res["az-not-in-inventory"] = s.post("/api/plan", {"kind": "vm", "items": [{"ident": VM1}], "options": {"az": "AZ9"}}).status
    res["dst-host-other-az"] = s.post("/api/plan", {"kind": "vm", "items": [{"ident": VM1}],
                                                    "options": {"az": "AZ1", "dst_host": "dst-cmp2"}}).status
    time.sleep(0.3)
    ok("SEC-UI-4 ident 'web-01; id', '$(id)', '--yes', '../x', 'a/b', bad option values, unknown options and values "
       "not in the runner's inventory/listing: 400, no migrate process started (calls.log and ps)",
       set(res.values()) == {400} and len(s.calls()) == n0 and not migrate_procs(), (res, s.calls()[n0:], migrate_procs()))
    # the runner checks again on its own (T14: it does not trust the web)
    import socket as so
    import json as js
    c = so.socket(so.AF_UNIX)
    c.connect(s.sock)
    c.sendall((js.dumps({"op": "plan", "session": "0123456789abcdef", "user": H.USER, "kind": "vm",
                         "items": [{"ident": "$(id)"}], "options": {}}) + "\n").encode())
    got = c.makefile("rb").read().decode()
    c.close()
    c = so.socket(so.AF_UNIX)
    c.connect(s.sock)
    c.sendall(b'{"op": "exec", "cmd": "id"}\n')
    got2 = c.makefile("rb").read().decode()
    c.close()
    ok("SEC-UI-4 the runner refuses the same ident sent straight to its socket, and any message outside its 9",
       '"status":400' in got and '"status":400' in got2 and len(s.calls()) == n0, (got, got2))
    ok("SEC-UI-4 audit holds the runner's 'rejected' lines", sum(1 for a in s.audit_lines()
                                                                if a["action"] == "rejected" and a["who"] == "runner") >= 3)

    # ---------------------------------------------------------------- 5. confirm token: other batch, wrong, voided
    s.control({"mode": "slow", "steps": 4, "step_secs": 0.05})
    p1 = s.plan([VM1], {"az": "AZ1"}).json["batch"]
    p2 = s.plan([VM2], {"az": "AZ1"}).json["batch"]
    swapped = s.post("/api/batches/%s/run" % p1["id"], {"typed": "app01", "confirm_token": p2["token"]})
    p1 = s.plan([VM1], {"az": "AZ1"}).json["batch"]
    wrong = s.run_batch(p1, "app02")
    again = s.run_batch(p1, "app01")
    p3 = s.plan([VM1, VM2], {"az": "AZ1"}).json["batch"]
    count_wrong = s.run_batch(p3, "3")
    ok("SEC-UI-5 another batch's token, a wrong typed name, then the right name on the voided token, a wrong count: 409",
       (swapped.status, wrong.status, again.status, count_wrong.status) == (409, 409, 409, 409),
       (swapped, wrong, again, count_wrong))
    p4 = s.plan([VM2], {"az": "AZ1"}).json["batch"]
    old_cookie = s.cookie
    s.login()  # a new session: the old one and its batches are gone
    s.post("/api/doctor")
    other = s.run_batch(p4, "app02")
    s.cookie = old_cookie
    ok("SEC-UI-5 a batch planned by an earlier session: 409 (and the old cookie is 401)",
       other.status == 409 and s.get("/api/jobs").status == 401, other)
    s.login()
    s.post("/api/doctor")
    s.post("/api/inventory")
    s.post("/api/list", {"hosts": ["cmp1"]})

    # ---------------------------------------------------------------- 6. two runs at once: 202 + 423
    s.control({"mode": "slow", "steps": 20, "step_secs": 0.1})
    pa = s.plan([VM1], {"az": "AZ1"}).json["batch"]
    pb = s.plan([VM2], {"az": "AZ1"}).json["batch"]
    out = {}

    def go(b, name):
        out[name] = s.run_batch(b, name)
    ts = [threading.Thread(target=go, args=(pa, "app01")), threading.Thread(target=go, args=(pb, "app02"))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    sts = sorted(r.status for r in out.values())
    ok("SEC-UI-6 two POST /run at the same moment: one 202, one 423 (locked)", sts == [202, 423], out)
    s.wait_idle()
    loser = [n for n, r in out.items() if r.status == 423]
    if loser:
        b = pa if loser[0] == "app01" else pb
        later = s.run_batch(b, loser[0])
        ok("SEC-UI-6 the locked batch keeps its token and runs once the lock is free", later.status == 202, later)
        s.wait_idle()
finally:
    s.stop()

# ---------------------------------------------------------------- 19.2 rollback display name from server.json
def live_state(stack, entries):
    """The fake's live state dirs: {ident: dir or None (= <state root>/<ident>)}."""
    st = {"live": {}, "rolled_back": []}
    for ident, d in entries.items():
        e = {"kind": "vm", "options": None, "t_stop": "2026-11-20T00:00:00Z", "dst": None, "started": "2026-11-20T00:00:00Z"}
        if d:
            e["dir"] = d
        st["live"][ident] = e
    with open(os.path.join(stack.fake, "state.json"), "w") as f:
        json.dump(st, f)


def server_json(d, obj=None, raw=None):
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "server.json"), "wb") as f:
        f.write(raw if raw is not None else json.dumps(obj).encode())


s = H.Stack()
try:
    s.login()
    s.post("/api/doctor")  # a session that never listed, on a runner that never listed: its name cache is empty
    root = s.state_root
    out = os.path.join(s.dir, "outside")
    server_json(os.path.join(root, VM1), {"id": VM1, "name": "mig-peer"})
    server_json(os.path.join(root, VM2), {"id": VM2, "name": "bad name <b>"})
    server_json(os.path.join(out, VM3), {"id": VM3, "name": "evil-outside"})
    os.symlink(os.path.join(out, VM3), os.path.join(root, VM3))            # a state dir that leads outside the root
    os.makedirs(os.path.join(root, "linkvm"))
    server_json(os.path.join(out, "target"), {"name": "linked-name"})
    os.symlink(os.path.join(out, "target", "server.json"), os.path.join(root, "linkvm", "server.json"))
    server_json(os.path.join(root, "bigvm"), raw=json.dumps({"name": "big-name", "pad": "x" * (1 << 20)}).encode())
    server_json(os.path.join(out, "elsewhere"), {"name": "elsewhere-name"})
    live_state(s, {VM1: None, VM2: None, VM3: None, "linkvm": None, "bigvm": None,
                   "elsevm": os.path.join(out, "elsewhere")})
    got = {}
    for ident in (VM1, VM2, VM3, "linkvm", "bigvm", "elsevm"):
        r = s.post("/api/rollback", {"ident": ident})
        got[ident] = ((r.json or {}).get("batch") or {}).get("names"), ((r.json or {}).get("batch") or {}).get("rule")
    ok("SEC-UI-19 (19.2) rollback in a session that never listed: the name to type comes from server.json of the state "
       "dir (mig-peer), rule name with the ident as the only alternative",
       got[VM1] == (["mig-peer"], {"mode": "name", "expect": ["mig-peer"], "alt": [VM1], "ids": [VM1]})
       and not any(c[0] == "list" for c in s.calls()), (got[VM1], s.calls()))
    ok("SEC-UI-19 (19.2) server.json name outside NAME_RE, a state dir symlinked outside --state-root, server.json a "
       "symlink (O_NOFOLLOW), server.json over 1 MiB, a dir outside the root: the name is the ident, no alternative",
       all(got[i] == ([i], {"mode": "name", "expect": [i], "ids": [i]}) for i in (VM2, VM3, "linkvm", "bigvm", "elsevm")), got)
    rb = s.post("/api/rollback", {"ident": VM1}).json["batch"]
    wrong = s.run_batch(rb, "app01")
    rb = s.post("/api/rollback", {"ident": VM1}).json["batch"]
    by_ident = s.run_batch(rb, VM1)
    j1 = s.wait_job(by_ident.json["jobs"][0]) if by_ident.status == 202 else {}
    live_state(s, {VM1: None})
    server_json(os.path.join(root, VM1), {"id": VM1, "name": "mig-peer"})  # the rollback retired the dir, as the CLI does
    rb = s.post("/api/rollback", {"ident": VM1}).json["batch"]
    both = s.run_batch(rb, "mig-peer\n" + VM1)
    rb = s.post("/api/rollback", {"ident": VM1}).json["batch"]
    by_name = s.run_batch(rb, "mig-peer")
    j2 = s.wait_job(by_name.json["jobs"][0]) if by_name.status == 202 else {}
    ok("SEC-UI-19 (19.2) typed: another name 409; the full ident 202 (job OK, name mig-peer); name + ident together "
       "409; the name alone 202",
       (wrong.status, by_ident.status, both.status, by_name.status) == (409, 202, 409, 202)
       and j1.get("state") == "OK" and j1.get("name") == "mig-peer" and j2.get("state") == "OK",
       (wrong, by_ident, both, by_name, j1.get("state"), j2.get("state")))
finally:
    s.stop()

# ---------------------------------------------------------------- 2. password guessing: 5 failures then 429 + audit
# (16.4 (2): the same with user name + password)
s = H.Stack()
try:
    wrong = ["wrong-password-%02d" % i for i in range(6)]
    sts = [s.login(password=w).status for w in wrong]
    right = s.login()
    lines = s.audit_lines()
    ok("SEC-UI-2 (16.4-2) six wrong passwords: 401 x5 then 429; the right password is refused while locked; audit "
       "has 'locked' and 5 'login-failed' with user=test",
       sts == [401] * 5 + [429] and right.status == 429 and any(a["action"] == "locked" for a in lines)
       and [a["detail"]["user"] for a in lines if a["action"] == "login-failed"] == [H.USER] * 5, (sts, right, lines[-3:]))
    texts = [open(p, errors="replace").read() for p in (s.audit, os.path.join(s.dir, "web.stderr"),
                                                         os.path.join(s.dir, "runner.stderr"))]
    ok("SEC-UI-2 no password (right or wrong) and no hash or salt reaches the audit or the web/runner logs",
       not any(pw in t for pw in wrong + [H.PASSWORD] for t in texts)
       and not any(v in t for v in json.load(open(s.users))["users"][H.USER].values()
                   if isinstance(v, str) and len(v) >= 32 for t in texts))
finally:
    s.stop()

# ---------------------------------------------------------------- 16.4: user name + password login


class ScryptSpy:
    """hashlib.scrypt wrapped: counts calls, records maxmem and how many ran at the same moment."""

    def __init__(self, delay=0.0):
        self.real, self.delay = hashlib.scrypt, delay
        self.calls, self.maxmem, self.active, self.peak = 0, [], 0, 0
        self.mu = threading.Lock()

    def __call__(self, *a, **kw):
        with self.mu:
            self.calls += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.maxmem.append(kw.get("maxmem"))
        try:
            time.sleep(self.delay)
            return self.real(*a, **kw)
        finally:
            with self.mu:
                self.active -= 1

    def __enter__(self):
        hashlib.scrypt = self
        return self

    def __exit__(self, *exc):
        hashlib.scrypt = self.real


class InProcWeb(H.Stack):
    """migui-web's State/Handler in this process (so hashlib.scrypt can be patched), on a free port; login only
    (no runner behind it)."""

    def __init__(self, users_file, audit):
        self.port = H.free_port()
        self.cookie = ""
        self.audit = audit
        cfg = web.parse_args(["--port", str(self.port), "--socket", "/nonexistent/runner.sock", "--users-file",
                              users_file, "--audit", audit])
        self.srv = web.Server(("127.0.0.1", self.port), web.Handler)
        self.srv.state = web.State(cfg)
        self.thread = threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self.thread.start()

    def stop(self, keep=False):
        self.srv.shutdown()
        self.srv.server_close()


s = H.Stack()
try:
    a = s.login(user="nosuchuser", password="wrong-password-1")
    b = s.login(password="wrong-password-2")
    ok("SEC-UI-16-1 an unknown user and a wrong password: the same status (401) and the same message",
       a.status == b.status == 401 and a.json == b.json == {"error": "wrong username or password"}, (a, b))
    good = s.login()
    lf = [x for x in s.audit_lines() if x["action"] == "login-failed"]
    ok("SEC-UI-16-1 the right password logs in (200); login-failed audit lines name the user tried, never the password",
       good.status == 200 and [x["detail"]["user"] for x in lf] == ["nosuchuser", H.USER], (good, lf))

    # (4) refused before hashing: 400, and (via the in-process web below) no scrypt
    def refused(user, pw):
        r = s.login(user=user, password=pw)
        s.login()  # a 400 counts toward the lockout: a good login after each one resets the count
        return r
    long_pw = refused(H.USER, "x" * 129)
    bad_names = {n: refused(n, H.PASSWORD).status for n in ("Test", "1abc", "a b", "-x", "a" * 33, "../x", "", "tést")}
    not_str = [s.post("/api/login", b).status for b in ({"username": "test"}, {"password": "x" * 16},
                                                          {"username": ["test"], "password": "x" * 16},
                                                          {"username": "test", "password": 12345678901234})]
    invalid = [x["detail"]["user"] for x in s.audit_lines() if x["action"] == "login-failed"][2:]
    ok("SEC-UI-16-4 a password of 129 characters, a name outside ^[a-z][a-z0-9._-]{0,31}$, or a missing / non-string "
       "field: 400 (no value echoed); the audit names the user, or '(invalid)' outside USER_RE", long_pw.status == 400
       and set(bad_names.values()) == {400} and not_str == [400] * 4 and "x" * 20 not in long_pw.raw.decode()
       and invalid == [H.USER] + ["(invalid)"] * 8, (long_pw, bad_names, not_str, invalid))

    # (5) users file missing / corrupt / over its limits: 503 that says what to do
    users = s.users
    saved = open(users, "rb").read()
    rec = json.loads(saved)["users"][H.USER]
    res = {}
    os.rename(users, users + ".away")
    res["missing"] = s.login()
    os.rename(users + ".away", users)
    cases = {"not JSON": b"{not json", "wrong shape": b'{"version": 1, "users": []}', "empty": b"",
             "51 users": json.dumps({"version": 1, "users": {"u%02d" % i: rec for i in range(51)}}).encode(),
             "65 KB": json.dumps({"version": 1, "users": {H.USER: rec}, "pad": "x" * (65 * 1024)}).encode(),
             "bad record": json.dumps({"version": 1, "users": {H.USER: dict(rec, n=1000)}}).encode(),
             "no users": b'{"version": 1, "users": {}}'}
    for k, raw in cases.items():
        with open(users, "wb") as f:
            f.write(raw)
        res[k] = s.login()
    with open(users, "wb") as f:
        f.write(saved)
    back = s.login()
    ok("SEC-UI-16-5 users file missing, not JSON, wrong shape, empty, 51 users, > 64 KB, a bad record, no users: 503 "
       "(not 401) naming migui-passwd; the file back: 200",
       all(r.status == 503 and "migui-passwd" in r.json.get("error", "") for r in res.values()) and back.status == 200,
       {k: (r.status, r.json) for k, r in res.items()})
    ok("SEC-UI-16-5 a 503 is no failed login (no lockout after 8 of them)", back.status == 200 and not any(
        x["action"] == "locked" for x in s.audit_lines()))

    # (6) the runner refuses a message without a valid user (sent straight to its socket)
    out = {}
    for label, extra in (("no user", {}), ("user Test", {"user": "Test"}), ("user ../x", {"user": "../x"}),
                         ("user 42", {"user": 42}), ("user ok", {"user": H.USER})):
        c = socket.socket(socket.AF_UNIX)
        c.connect(s.sock)
        c.sendall((json.dumps(dict({"op": "status", "session": "0123456789abcdef"}, **extra)) + "\n").encode())
        out[label] = c.makefile("rb").read().decode()
        c.close()
    ok("SEC-UI-16-6 runner message without 'user', or with a user outside USER_RE: 400 (audit 'rejected'); with a "
       "valid user: answered",
       all('"status":400' in out[k] and "user" in out[k] for k in ("no user", "user Test", "user ../x", "user 42"))
       and '"jobs"' in out["user ok"] and '"status":400' not in out["user ok"]
       and sum(1 for x in s.audit_lines() if x["action"] == "rejected" and "user" in str(x.get("detail"))) >= 4, out)
finally:
    s.stop()

# (3) and (4) with scrypt counted: the web's own State/Handler in this process
tmpd = tempfile.mkdtemp(prefix="migui-test-", dir=H.WORK)
try:
    uf, af = os.path.join(tmpd, "users"), os.path.join(tmpd, "audit.jsonl")
    with open(uf, "wb") as f:
        f.write(auth.dump_users({H.USER: auth.new_record(H.PASSWORD)}))
    w = InProcWeb(uf, af)
    try:
        counts = {}
        for label, user, pw in (("unknown user", "nosuchuser", "wrong-password-1"), ("wrong password", H.USER, "x" * 20),
                                ("right password", H.USER, H.PASSWORD), ("129 characters", H.USER, "x" * 129),
                                ("name outside USER_RE", "Root", H.PASSWORD)):
            with ScryptSpy() as spy:
                t0 = time.time()
                r = w.login(user=user, password=pw)
                counts[label] = (r.status, spy.calls, round(time.time() - t0, 3), set(spy.maxmem))
        ok("SEC-UI-16-3 login of an unknown user runs scrypt exactly once (like a wrong or right password), always "
           "with maxmem 64 MiB",
           [counts[k][:2] for k in ("unknown user", "wrong password", "right password")] == [(401, 1), (401, 1), (200, 1)]
           and all(counts[k][3] == {64 * 1024 * 1024} for k in ("unknown user", "wrong password", "right password")),
           counts)
        ok("SEC-UI-16-4 a 129-character password and a name outside USER_RE: 400 with scrypt never called",
           counts["129 characters"][:2] == (400, 0) and counts["name outside USER_RE"][:2] == (400, 0), counts)
        w.login()
        with ScryptSpy(delay=0.3) as spy:
            rs = []
            ts = [threading.Thread(target=lambda: rs.append(w.login(password="wrong-password-%d" % i).status))
                  for i in range(3)]
            [t.start() for t in ts]
            [t.join() for t in ts]
        ok("SEC-UI-16-3 three logins at once: scrypt runs one at a time (one lock, peak 1), three 401",
           spy.calls == 3 and spy.peak == 1 and sorted(rs) == [401] * 3, (spy.calls, spy.peak, rs))
    finally:
        w.stop()
    users = auth.load_users(uf)
    with ScryptSpy() as spy:
        v = [auth.verify_password(users, "nosuchuser", "x" * 16), auth.verify_password(users, H.USER, "x" * 16),
             auth.verify_password(users, H.USER, H.PASSWORD)]
    ok("SEC-UI-16-3 verify_password(): unknown user False, wrong False, right True; exactly one scrypt each",
       v == [False, False, True] and spy.calls == 3, (v, spy.calls))
finally:
    shutil.rmtree(tmpd, ignore_errors=True)

# ---------------------------------------------------------------- 5b. expired token
s = H.Stack(confirm_ttl=1)
try:
    s.ready()
    b = s.plan([VM1], {"az": "AZ1"}).json["batch"]
    time.sleep(2.2)
    r = s.run_batch(b, "app01")
    ok("SEC-UI-5 a confirm token older than its TTL: 409", r.status == 409, r)
finally:
    s.stop()

# ---------------------------------------------------------------- 7. SO_PEERCRED
s = H.Stack(peer_user="nobody")
try:
    s.login()
    r = s.post("/api/doctor")
    import socket as so
    c = so.socket(so.AF_UNIX)
    c.connect(s.sock)
    c.sendall(b'{"op": "doctor"}\n')
    got = c.makefile("rb").read().decode()
    c.close()
    ok("SEC-UI-7 runner configured for another peer user: the web (and any other process) is refused (403), no "
       "migrate runs, audit says why", r.status == 403 and '"status":403' in got and not s.calls()
       and any("peer uid" in str(a.get("detail")) for a in s.audit_lines()), (r, got, s.calls()))
finally:
    s.stop()
if os.getuid() == 0:
    s = H.Stack()
    try:
        os.chmod(s.dir, 0o755)
        os.chmod(s.run, 0o755)
        code = ("import os, socket, sys; os.setgid(65534); os.setuid(65534)\n"
                "c = socket.socket(socket.AF_UNIX)\n"
                "try:\n    c.connect(sys.argv[1]); c.sendall(b'{\"op\": \"doctor\"}\\n'); print(c.makefile('rb').read().decode())\n"
                "except OSError as e:\n    print('connect refused', e.errno)\n")
        got = subprocess.run([H.PY, "-I", "-c", code, s.sock], capture_output=True, text=True).stdout
        ok("SEC-UI-7 uid 65534 (not the peer user) on the runner socket: refused (EACCES by mode 0660 or 403 by "
           "SO_PEERCRED)", "connect refused" in got or '"status":403' in got, got)
        os.chmod(s.sock, 0o666)  # even with the socket opened to everyone, SO_PEERCRED still refuses
        got = subprocess.run([H.PY, "-I", "-c", code, s.sock], capture_output=True, text=True).stdout
        ok("SEC-UI-7 socket made world-writable on purpose: SO_PEERCRED alone still refuses uid 65534",
           '"status":403' in got and not s.calls(), got)
    finally:
        s.stop()

# ---------------------------------------------------------------- 8. XSS: data is never markup
s = H.Stack()
try:
    d, i, l = s.ready()
    names = [r["name"] for r in (l.json or {}).get("data", {}).get("rows", [])]
    idx = s.get("/")
    csp = idx.headers.get("content-security-policy", "")
    ok("SEC-UI-8 a VM named '%s' comes back as a JSON string (application/json, nosniff), never as HTML" % XSS_NAME,
       XSS_NAME in names and l.headers.get("content-type", "").startswith("application/json")
       and l.headers.get("x-content-type-options") == "nosniff", (names, l.headers))
    ok("SEC-UI-8 CSP on the page: script-src 'self', style-src 'self', no 'unsafe-inline'/'unsafe-eval', frame "
       "ancestors none; X-Frame-Options DENY",
       "script-src 'self'" in csp and "style-src 'self'" in csp and "default-src 'self'" in csp
       and "connect-src 'self'" in csp and "unsafe" not in csp and "frame-ancestors 'none'" in csp
       and idx.headers.get("x-frame-options") == "DENY", (csp, idx.headers))
    html = open(os.path.join(STATIC, "index.html"), encoding="utf-8").read()
    js = open(os.path.join(STATIC, "app.js"), encoding="utf-8").read()
    inline_script = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", html)
    handlers = re.findall(r"\son[a-z]+\s*=", html)
    styles = re.findall(r"\sstyle\s*=", html)
    sinks = re.findall(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function|setAttribute\('on|"
                       r"setAttribute\(\"on|\.style\.cssText|javascript:", js)
    ok("SEC-UI-8 static pages: no inline <script>, no on*= handler, no style= attribute; app.js: no innerHTML/"
       "outerHTML/insertAdjacentHTML/document.write/eval/new Function (every server string goes through textContent)",
       not inline_script and not handlers and not styles and not sinks, (inline_script, handlers, styles, sinks))
    svg_ns = "http://www.w3.org/2000/svg"  # the SVG namespace name of the icon sprite: an identifier, never fetched
    ext = (re.findall(r"(?:src|href)=\"(https?:)?//", html.replace(svg_ns, ""))
           + re.findall(r"https?://", js.replace(svg_ns, "")))
    ok("SEC-UI-8 no CDN or external URL in the pages", not ext, ext)
finally:
    s.stop()

# ---------------------------------------------------------------- 9. bind outside loopback: refused
s = H.Stack(start_web=False)
try:
    res = {}
    for addr in ("0.0.0.0", "192.0.2.109", "::", "localhost"):
        p = subprocess.run(s.web_argv("--bind", addr), capture_output=True, text=True, timeout=20)
        res[addr] = (p.returncode, "loopback" in p.stderr)
    ok("SEC-UI-9 migui-web --bind 0.0.0.0 / a LAN address / :: / a host name: exits 2 before listening, says why",
       all(v == (2, True) for v in res.values()), res)
finally:
    s.stop()

# ---------------------------------------------------------------- 10. nothing secret in audit, logs, stream
s = H.Stack(control={"mode": "leaky", "steps": 3, "step_secs": 0.01})
try:
    s.ready()
    b = s.plan([VM1], {"az": "AZ1"}).json["batch"]
    r = s.run_batch(b, "app01")
    jid = r.json["jobs"][0]
    st, events = s.stream(jid)
    j = s.wait_job(jid)
    log = s.get("/api/jobs/%s/log" % jid).raw.decode()
    texts = {"sse": repr(events), "log download": log, "audit": open(s.audit).read(),
             "job record": open(os.path.join(s.lib, "jobs", jid + ".json")).read(),
             "job log file": open(os.path.join(s.lib, "jobs", jid + ".log")).read(),
             "web stderr": open(os.path.join(s.dir, "web.stderr")).read(),
             "runner stderr": open(os.path.join(s.dir, "runner.stderr")).read()}
    hits = {k: LEAK.findall(v) for k, v in texts.items() if LEAK.search(v)}
    ok("SEC-UI-10 a migrate that prints OS_PASSWORD=, OS_APPLICATION_CREDENTIAL_SECRET=, X-Auth-Token and a "
       "'key = AQ...' line: none of it in the SSE stream, the log download, the audit, the job record/log, the web "
       "or runner stderr (grep OS_PASSWORD|SECRET|AQ[A-Za-z0-9+/=]{30,})",
       j.get("state") == "OK" and not hits and "[scrubbed]" in log, (j.get("state"), hits))
    ok("SEC-UI-10 the login password is in none of them either", not any(H.PASSWORD in v for v in texts.values()))
finally:
    s.stop()

# ---------------------------------------------------------------- 20. the live plan lines are scrubbed too (T9)
s = H.Stack(control={"plan_lines": {"n": 6, "delay": 0, "secret": True}})
try:
    s.ready()
    p = s.plan([VM1], {"az": "AZ1"})
    lines = [d for n, d in (p.json or {}).get("events") or [] if n == "line"]
    leak = [d["text"] for d in lines if "ABC-NOT-A-REAL-ONE" in d["text"] or "OS_PASSWORD" in d["text"]]
    others = {"audit": open(s.audit).read(), "web stderr": open(os.path.join(s.dir, "web.stderr")).read(),
              "runner stderr": open(os.path.join(s.dir, "runner.stderr")).read()}
    ok("SEC-UI-20 a plan that prints OS_PASSWORD=ABC-NOT-A-REAL-ONE on stderr: its 'line' event reads "
       "'debug: [scrubbed]', the secret is in no line event, nor in the audit or the web/runner stderr",
       p.status == 200 and any(d["text"] == "debug: [scrubbed]" for d in lines) and not leak
       and not any("ABC-NOT-A-REAL-ONE" in v for v in others.values()), ([d["text"] for d in lines], leak))
finally:
    s.stop()

sys.exit(H.total())
