"""Test harness of the UI: a real migui-runner and migui-web on a temporary socket and a free loopback port, the
fake 'migrate' (fake_migrate.py) behind the runner, an HTTP client with the browser's headers. No root needed: the
runner's peer user is the user running the tests, the socket group is left alone.

    stack = Stack(); stack.login(); stack.post("/api/doctor", {}); ... ; stack.stop()

Each stack starts with one account, USER / PASSWORD, made by migui-passwd --users-file <stack>/lib/users (16.4).
migrate's state root is <stack>/state for both the runner (--state-root) and the fake (MIGUI_FAKE_STATE_ROOT): the
runner reads server.json there (19.2), never under the host's /var/lib/mig-migrate. POST /api/plan answers
server-sent events (18): plan() reads them and returns the old {rows, batch, rc} shape plus the events.
"""
import http.client
import json
import os
import pwd
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(HERE))
PY = sys.executable
FAKE = os.path.join(HERE, "fake_migrate.py")
VM1, VM2, VM3 = ("7c1e4b90-2f1a-4c2e-9d0b-6a3e51f09a01", "7c1e4b90-2f1a-4c2e-9d0b-6a3e51f09a02",
                 "7c1e4b90-2f1a-4c2e-9d0b-6a3e51f09a03")
VOL1 = "c91b2e4d-0000-4000-8000-00000000a001"
XSS_NAME = "<img src=x onerror=alert(1)>"
WORK = os.environ.get("MIGUI_TEST_WORK", "/tmp")
BOOT = "import sys; sys.path.insert(0, sys.argv.pop(1)); from migui.%s import main; sys.exit(main())"
USER = "test"
PASSWORD = "test-password-2026-11-20"   # 16.1: 12-128 printable characters, not the name

PASS = FAIL = 0


def ok(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS " + label, flush=True)
    else:
        FAIL += 1
        print("FAIL %s %s" % (label, str(detail)[-2500:]), flush=True)
    return cond


def total():
    print("TOTAL pass=%d fail=%d" % (PASS, FAIL), flush=True)
    return 1 if FAIL else 0


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Resp:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.raw = status, headers, body
        try:
            self.json = json.loads(body.decode("utf-8")) if body else {}
        except ValueError:
            self.json = None

    def __repr__(self):
        return "<%d %s>" % (self.status, self.raw[:300])


class Stack:
    def __init__(self, peer_user=None, confirm_ttl=900, kill_after=2, control=None, start_web=True, plan_jobs=None):
        self.dir = tempfile.mkdtemp(prefix="migui-test-", dir=WORK)
        self.fake = os.path.join(self.dir, "fake")
        self.lib = os.path.join(self.dir, "lib")
        self.run = os.path.join(self.dir, "run")
        self.audit = os.path.join(self.dir, "log", "audit.jsonl")
        os.makedirs(self.fake)
        os.makedirs(self.lib)
        os.makedirs(os.path.dirname(self.audit))
        self.users = os.path.join(self.lib, "users")
        self.state_root = os.path.join(self.dir, "state")
        os.makedirs(self.state_root)
        self.control(control or {})
        self.sock = os.path.join(self.run, "runner.sock")
        me = pwd.getpwuid(os.getuid()).pw_name
        self.runner_log = open(os.path.join(self.dir, "runner.stderr"), "wb")
        # the fake's in-process clouds (tests/unit/inproc.py) make their temporary directories under TMPDIR and
        # MIG_UNIT_WORK: inside this stack's directory, removed with it, never shared with another harness run
        tmp = os.path.join(self.dir, "tmp")
        os.makedirs(tmp)
        env = dict(os.environ, MIGUI_FAKE_DIR=self.fake, TMPDIR=tmp, MIG_UNIT_WORK=tmp,
                   MIGUI_FAKE_STATE_ROOT=self.state_root)
        self.env = env
        p = self.passwd("add", USER, stdin=PASSWORD + "\n")
        if p.returncode != 0:
            raise RuntimeError("migui-passwd add %s: rc %d %s" % (USER, p.returncode, p.stderr))
        argv = [PY, "-I", "-c", BOOT % "runner", REPO, "--socket", self.sock, "--lock", os.path.join(self.run, "runner.lock"),
                "--state-dir", self.lib, "--audit", self.audit, "--peer-user", peer_user or me, "--group", "",
                "--migrate", json.dumps([PY, "-I", FAKE]), "--cli-python", PY, "--cli-path", REPO,
                "--confirm-ttl", str(confirm_ttl), "--kill-after", str(kill_after), "--state-root", self.state_root]
        if plan_jobs is not None:  # C1: the runner's default plans 4 VMs at once
            argv += ["--plan-jobs", str(plan_jobs)]
        self.runner = subprocess.Popen(argv, stdout=self.runner_log, stderr=subprocess.STDOUT, env=env)
        self.wait(lambda: os.path.exists(self.sock), "runner socket")
        self.port = free_port()
        self.web = None
        self.cookie = ""
        if start_web:
            self.web_log = open(os.path.join(self.dir, "web.stderr"), "wb")
            self.web = subprocess.Popen(self.web_argv(), stdout=self.web_log, stderr=subprocess.STDOUT)
            self.wait(self.port_open, "web port")

    def web_argv(self, *extra):
        return [PY, "-I", "-c", BOOT % "web", REPO, "--port", str(self.port), "--socket", self.sock,
                "--users-file", self.users, "--audit", self.audit] + list(extra)

    def passwd(self, *args, stdin=None, users=None):
        """migui-passwd on this stack's users file (or users), password on stdin: CompletedProcess (text)."""
        argv = [PY, "-I", "-c", BOOT % "passwd", REPO, "--users-file", users or self.users, "--audit", self.audit]
        argv += list(args) + (["--stdin"] if stdin is not None else [])
        return subprocess.run(argv, input=stdin if stdin is not None else "", capture_output=True, text=True,
                              timeout=60)

    def control(self, obj):
        with open(os.path.join(self.fake, "control.json"), "w") as f:
            json.dump(obj, f)

    def calls(self):
        try:
            with open(os.path.join(self.fake, "calls.log")) as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []

    def wait(self, cond, what, secs=30):
        t = time.time()
        while time.time() - t < secs:
            if cond():
                return
            for p in (getattr(self, "runner", None), getattr(self, "web", None)):
                if p is not None and p.poll() is not None:
                    raise RuntimeError("%s died: %s" % (what, self.logs()))
            time.sleep(0.05)
        raise RuntimeError("timeout waiting for %s: %s" % (what, self.logs()))

    def port_open(self):
        try:
            socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
            return True
        except OSError:
            return False

    def logs(self):
        out = ""
        for n in ("runner.stderr", "web.stderr"):
            try:
                with open(os.path.join(self.dir, n), "rb") as f:
                    out += "\n--- %s\n%s" % (n, f.read().decode("utf-8", "replace")[-3000:])
            except OSError:
                pass
        return out

    # --- HTTP ---------------------------------------------------------------------------------------------------
    def headers(self, method, headers=None, cookie=True, origin=True, xrw=True):
        h = {}
        if cookie and self.cookie:
            h["Cookie"] = "migui_session=" + self.cookie
        if method == "POST":
            if origin:
                h["Origin"] = "http://127.0.0.1:%d" % self.port
            if xrw:
                h["X-Requested-With"] = "migui"
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        return h

    def request(self, method, path, body=None, headers=None, cookie=True, origin=True, xrw=True, raw=None, timeout=120):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        h = self.headers(method, headers, cookie, origin, xrw)
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        resp = Resp(r.status, {k.lower(): v for k, v in r.getheaders()}, r.read())
        c.close()
        return resp

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body if body is not None else {}, **kw)

    def login(self, user=USER, password=PASSWORD):
        r = self.post("/api/login", {"username": user, "password": password})
        sc = r.headers.get("set-cookie", "")
        if r.status == 200 and sc.startswith("migui_session="):
            self.cookie = sc.split(";", 1)[0].split("=", 1)[1]
        return r

    def ready(self):
        """login + doctor + inventory + list --host cmp1: what the operator does before planning."""
        self.login()
        d = self.post("/api/doctor")
        i = self.post("/api/inventory")
        l = self.post("/api/list", {"hosts": ["cmp1"]})
        return d, i, l

    def sse_post(self, path, body, secs=300, stop=None, **kw):
        """A POST that may answer text/event-stream (18): (Resp, [(event, data, arrival time)], pings). A JSON answer
        (a refusal) has no events. stop(events) -> True closes the connection there (a browser that goes away)."""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=secs)
        c.request("POST", path, body=json.dumps(body).encode(), headers=self.headers("POST", **kw))
        r = c.getresponse()
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        if not hdrs.get("content-type", "").startswith("text/event-stream"):
            resp = Resp(r.status, hdrs, r.read())
            c.close()
            return resp, [], 0
        events, name, data, pings = [], None, [], 0
        t0 = time.time()
        while time.time() - t0 < secs:
            line = r.fp.readline()
            if not line:
                break
            line = line.decode("utf-8").rstrip("\n")
            if line.startswith(":"):
                pings += 1
            elif line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data.append(line[6:])
            elif line == "" and name:
                events.append((name, json.loads("\n".join(data)), time.time()))
                name, data = None, []
                if stop and stop(events):
                    break
        c.close()
        return Resp(r.status, hdrs, b""), events, pings

    def plan(self, idents, options=None, kind="vm", ips=None, **kw):
        """POST /api/plan; a stream comes back as Resp.json = {rows, batch, rc, events: [(event, data)]}."""
        items = [dict({"ident": i}, **({"ip": ips[i]} if ips and i in ips else {})) for i in idents]
        resp, events, _pings = self.sse_post("/api/plan", {"kind": kind, "items": items, "options": options or {}}, **kw)
        if resp.headers.get("content-type", "").startswith("text/event-stream"):
            resp.json = {"rows": [d for n, d, _t in events if n == "row"],
                         "batch": next((d for n, d, _t in events if n == "batch"), None),
                         "rc": next((d.get("rc") for n, d, _t in events if n == "end"), None),
                         "events": [(n, d) for n, d, _t in events]}
        return resp

    def run_batch(self, batch, typed, cont=False):
        return self.post("/api/batches/%s/run" % batch["id"], {"typed": typed, "confirm_token": batch["token"],
                                                               "continue_on_error": cont})

    def stream(self, job_id, secs=120, last=None):
        """The SSE events of one job until 'end': [(event, data)]."""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=secs)
        h = {"Cookie": "migui_session=" + self.cookie}
        if last is not None:
            h["Last-Event-ID"] = str(last)
        c.request("GET", "/api/jobs/%s/stream" % job_id, headers=h)
        r = c.getresponse()
        events, name, data = [], None, []
        if r.status != 200:
            c.close()
            return r.status, [("http", r.read().decode())]
        t0 = time.time()
        while time.time() - t0 < secs:
            line = r.fp.readline()
            if not line:
                break
            line = line.decode("utf-8").rstrip("\n")
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data.append(line[6:])
            elif line == "" and name:
                events.append((name, json.loads("\n".join(data))))
                if name == "end":
                    break
                name, data = None, []
        c.close()
        return 200, events

    def wait_job(self, job_id, states=("OK", "FAIL", "CANCELLED", "SKIPPED"), secs=120):
        t = time.time()
        while time.time() - t < secs:
            j = (self.get("/api/jobs/" + job_id).json or {}).get("job") or {}
            if j.get("state") in states:
                return j
            time.sleep(0.2)
        return j

    def wait_idle(self, secs=120):
        t = time.time()
        while time.time() - t < secs:
            if not (self.get("/api/jobs").json or {}).get("active"):
                return True
            time.sleep(0.2)
        return False

    def audit_lines(self):
        try:
            with open(self.audit) as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []

    def stop(self, keep=False):
        for p in (self.web, self.runner):
            if p is not None and p.poll() is None:
                p.terminate()
                try:
                    p.wait(10)
                except subprocess.TimeoutExpired:
                    p.kill()
        if not keep and not os.environ.get("MIGUI_TEST_KEEP"):
            shutil.rmtree(self.dir, ignore_errors=True)
