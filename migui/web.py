"""migui-web (DESIGN.md section 8): the unprivileged half of the UI.

http.server on 127.0.0.1:8787 (loopback only, T1), user migui. Serves the static pages of migui/static (read once
at start, no path from a request ever reaches the file system) and a JSON API under /api/. Everything that runs
goes to migui-runner over its unix socket; this process has no credential and reads no state directory.

Security (DESIGN.md section 8.3): login with a user name and a password (POST body) checked against the users file
(/var/lib/migui/users, root:migui 0640, written by migui-passwd, read again at each login): a name outside USER_RE
or a password over 128 characters is a 400 before any hashing; scrypt runs one at a time (a lock: N logins never
hold N x 32 MiB), an unknown name is hashed too (dummy salt), wrong name and wrong password get the same 401;
5 failures of one name in 10 minutes lock that name for 15 minutes, 20 of all names lock every login (T3, B7); a missing or corrupt users file is a 503 that says
so. The session carries its user: every runner message has "user", every audit line of a session has user=.
One session at a time, cookie HttpOnly SameSite=Strict, 8 h, 30 min idle (T4; a plan streaming, or a batch of the
session running, keeps it alive: C1); a session ends when its account is removed or its password changed (B4);
at most 8 API requests at once and 2 event streams per session (B8); the Doctor gate covers inventory, list, plan and
the run of a migrate or volume batch only (D3); a mutating POST whose audit line cannot be written is a 503 (B5);
every POST needs the cookie, Origin http://127.0.0.1:PORT or http://localhost:PORT, X-Requested-With: migui and a
JSON body of at most 64 KB (T5, T15); the Host header must name the loopback port (DNS rebinding); CSP without
inline script or style, X-Frame-Options DENY (T6); Cache-Control no-store (T16); audit lines in
/var/log/migui/audit.jsonl (T12). Destructive actions need the runner's confirm token and the typed confirmation
(7, 14.2).
"""
import argparse
import csv
import hmac
import io
import ipaddress
import json
import os
import queue
import re
import secrets
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import __version__, auth
from .common import (MAX_LINE, USER_RE, Audit, Reject, check_ident, check_items, dumps, normalize_options,
                     read_json_line)

COOKIE = "migui_session"
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/app.css": ("app.css", "text/css; charset=utf-8")}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; "
       "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
JOB_PATH = re.compile(r"^/api/jobs/([0-9a-f]{16})(/stream|/log|/cancel)?\Z")
BATCH_PATH = re.compile(r"^/api/batches/([0-9a-f]{16})/(run|stop)\Z")
FAIL_WINDOW, FAIL_MAX, LOCK_FOR = 600, 5, 900
GLOBAL_FAIL_MAX = 20  # B7: failures of every name together in FAIL_WINDOW lock all logins
API_CONCURRENT = 8    # B8: API requests served at once (SSE streams aside)
SSE_PER_SESSION = 2   # B8: open event streams (job or plan) of one session
TOUCH_EVERY = 60      # C1: seconds between two touches of a session whose batch runs
UI_FEATURES = {"write_check": True, "plan_jobs": 4, "prune": True}  # section 3: GET /api/session
JOBS_CSV_COLUMNS = ("id", "batch", "kind", "ident", "name", "operator", "state", "rc", "exit_class", "phase",
                    "t_plan", "t_start", "t_stop", "t_end", "volumes_total", "dst_server_id", "notify_rc")
RUN_OPS = {"vm": "migrate", "vol": "volume", "rollback": "rollback"}
# 16.3: one scrypt at a time in this process (32 MiB each): other logins wait for it
HASH_LOCK = threading.Lock()
LOGIN_FAILED = "wrong username or password"
PING = 15  # seconds without an event before an SSE comment line (the plan stream, 18; the runner pings the job stream)


def cred_stamp(rec):
    """B4: what identifies the password a session logged in with (its change time and its hash, never shown)."""
    if not isinstance(rec, dict):
        return None
    return "%s|%s" % (rec.get("changed"), rec.get("hash"))


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


class Session:
    def __init__(self, user):
        self.user = user                     # 16.3: the account that logged in (USER_RE)
        self.secret = secrets.token_urlsafe(32)
        self.id = secrets.token_hex(8)       # what the runner and the audit see, never the cookie
        self.created = self.last = time.time()
        self.doctor_ok = False
        self.batches = {}                    # batch id -> kind (vm|vol|rollback), from this session's plans
        self.changed = None                  # B4: the users-file record's 'changed' at login
        self.streams = 0                     # B8: open SSE streams

    def remember(self, bid, kind):
        """A batch this session planned (the runner keeps at most 20 planned ones; older ids are dead anyway)."""
        self.batches[bid] = kind
        while len(self.batches) > 50:
            self.batches.pop(next(iter(self.batches)))


class State:
    def __init__(self, cfg):
        self.cfg = cfg
        self.mu = threading.Lock()
        self.session = None
        self.failures = []                   # B7: every failure (the global threshold)
        self.locked_until = 0.0
        self.user_failures = {}              # B7: name -> failure times
        self.user_locked = {}                # B7: name -> locked until
        self.api = threading.BoundedSemaphore(API_CONCURRENT)
        self.audit = Audit(cfg.audit, "web")
        self.static = {}
        for path, (name, ctype) in STATIC.items():
            with open(os.path.join(cfg.static, name), "rb") as f:
                self.static[path] = (f.read(), ctype)

    def current(self, secret, touch=True):
        """The session the cookie names, if valid now (8 h max, 30 min idle); None otherwise."""
        with self.mu:
            s = self.session
            if s is None or not secret or not hmac.compare_digest(s.secret.encode(), secret.encode()):
                return None
            now = time.time()
            if now - s.created > self.cfg.session_max or now - s.last > self.cfg.idle:
                self.audit("session-expired", session=s.id, user=s.user)
                self.session = None
                return None
            if touch:
                s.last = now
            return s


def runner_call(cfg, msg, timeout=None):
    """Yields the runner's reply objects; raises ApiError on an error line or a dead socket."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    try:
        s.connect(cfg.socket)
    except OSError:
        s.close()
        raise ApiError(503, "migui-runner is not reachable")
    s.settimeout(timeout)
    try:
        s.sendall((dumps(msg) + "\n").encode("utf-8"))
        f = s.makefile("rb")
        while True:
            try:
                obj = read_json_line(f, limit=64 << 20)
            except Reject as e:
                raise ApiError(502, "bad answer from the runner: " + e.message)
            except OSError:
                raise ApiError(504, "the runner did not answer in time")
            if obj is None:
                raise ApiError(502, "the runner closed the connection")
            if "error" in obj:
                st = obj.get("status")
                raise ApiError(st if isinstance(st, int) and 400 <= st < 600 else 500, str(obj["error"]))
            yield obj
            if "end" in obj:
                return
    finally:
        s.close()


def collect(cfg, msg, timeout=None):
    return list(runner_call(cfg, msg, timeout))


def pick(replies, key):
    return next((r[key] for r in replies if key in r), None)


def end_rc(replies):
    return pick(replies, "end")


class Handler(BaseHTTPRequestHandler):
    server_version = "migui"
    sys_version = ""
    timeout = 30  # B8: a client that sends its headers slower than this is dropped

    # --- plumbing ----------------------------------------------------------------------------------------------
    def log_message(self, fmt, *args):  # method, path, status only (no client address, no body)
        sys.stderr.write("migui-web: %s\n" % (fmt % args))

    def headers_common(self, api=True):
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cache-Control", "no-store")

    def send_json(self, status, obj, cookie=None):
        body = (dumps(obj) + "\n").encode("utf-8")
        self.send_response(status)
        self.headers_common()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def cookie(self):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                return v
        return ""

    @property
    def st(self):
        return self.server.state

    @property
    def cfg(self):
        return self.server.state.cfg

    def host_ok(self):
        return (self.headers.get("Host") or "") in self.cfg.hosts

    def csrf_ok(self):
        """T5: X-Requested-With: migui, an Origin of this UI, Sec-Fetch-Site same-origin when sent."""
        if self.headers.get("X-Requested-With") != "migui":
            return False
        if (self.headers.get("Origin") or "") not in self.cfg.origins:
            return False
        sfs = self.headers.get("Sec-Fetch-Site")
        return sfs in (None, "same-origin")

    def body(self):
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ApiError(415, "Content-Type must be application/json")
        try:
            n = int(self.headers.get("Content-Length") or "-1")
        except ValueError:
            n = -1
        if n < 0:
            raise ApiError(411, "Content-Length required")
        if n > MAX_LINE:
            raise ApiError(413, "body larger than 64 KB")
        raw = self.rfile.read(n) if n else b"{}"
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError(400, "body is not JSON")
        if not isinstance(obj, dict):
            raise ApiError(400, "body must be a JSON object")
        return obj

    # --- dispatch ----------------------------------------------------------------------------------------------
    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def do_HEAD(self):
        self.dispatch("HEAD")

    def dispatch(self, method):
        path = self.path.split("?", 1)[0]
        try:
            if not self.host_ok():
                raise ApiError(421, "wrong Host header: open the UI as http://127.0.0.1:%d/" % self.cfg.port)
            if method == "GET" and path in self.st.static:
                body, ctype = self.st.static[path]
                self.send_response(200)
                self.headers_common()
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if not path.startswith("/api/"):
                raise ApiError(404, "not found")
            if method not in ("GET", "POST"):
                raise ApiError(405, "method not allowed")
            if method == "POST":
                if not self.csrf_ok():
                    self.st.audit("csrf-rejected", path=path)
                    raise ApiError(403, "cross-site request refused (Origin / X-Requested-With)")
                body = self.body()
                if path == "/api/login":
                    return self.login(body)
            bg = self.headers.get("X-Migui-Background") == "1"
            sess = self.st.current(self.cookie(), touch=not bg and not path.endswith("/stream"))
            if sess is None:
                raise ApiError(401, "log in first")
            if not bg and not self.user_still_valid(sess):
                raise ApiError(401, "this account was removed or its password changed: log in again")
            stream = path.endswith("/stream") or path == "/api/plan"
            if stream:  # B8: at most SSE_PER_SESSION event streams per session
                with self.st.mu:
                    if sess.streams >= SSE_PER_SESSION:
                        raise ApiError(429, "too many open event streams for this session (%d)" % SSE_PER_SESSION)
                    sess.streams += 1
                try:
                    return self.get(path, sess) if method == "GET" else self.post(path, sess, body)
                finally:
                    with self.st.mu:
                        sess.streams -= 1
            if not self.st.api.acquire(timeout=10):  # B8
                raise ApiError(503, "the UI is busy (%d requests at once): try again" % API_CONCURRENT)
            try:
                return self.get(path, sess) if method == "GET" else self.post(path, sess, body)
            finally:
                self.st.api.release()
        except ApiError as e:
            try:
                self.send_json(e.status, {"error": e.message})
            except OSError:
                pass
        except Reject as e:
            self.send_json(e.status, {"error": e.message})
        except (BrokenPipeError, ConnectionResetError):
            pass

    # --- login -------------------------------------------------------------------------------------------------
    def login(self, body):
        st, now = self.st, time.time()
        name, password = body.get("username"), body.get("password")
        with st.mu:
            if now < st.locked_until:
                raise ApiError(429, "too many failed logins: locked for %d s" % int(st.locked_until - now))
            if isinstance(name, str) and now < st.user_locked.get(name, 0):
                raise ApiError(429, "too many failed logins for this name: locked for %d s"
                               % int(st.user_locked[name] - now))
        if not isinstance(name, str) or not isinstance(password, str):
            raise ApiError(400, "username and password are required (strings)")
        if not USER_RE.match(name) or len(password) > auth.PW_MAX:
            # 16.3: refused before any hashing (no scrypt spent on junk); still a failed login for the lockout
            self.login_failed(now, name if USER_RE.match(name) else "(invalid)")
            raise ApiError(400, "username: a lower-case letter then a-z 0-9 . _ - (at most 32); password: at most "
                                "%d characters" % auth.PW_MAX)
        try:
            users = auth.load_users(self.cfg.users_file)
        except auth.UsersFileError as e:
            st.audit("users-file-error", why=e.message)
            raise ApiError(503, e.message if e.missing else e.message + ": check it with migui-passwd list")
        if not users:
            st.audit("users-file-error", why="no users")
            raise ApiError(503, "no users in the users file: run migui-passwd add")
        with HASH_LOCK:
            with st.mu:  # a lock that began while this request waited for the hash lock
                if time.time() < st.locked_until:
                    raise ApiError(429, "too many failed logins: locked for %d s" % int(st.locked_until - time.time()))
            try:
                ok = auth.verify_password(users, name, password)
            except auth.ScryptError as e:
                st.audit("scrypt-error")
                raise ApiError(503, e.message)
        if not ok:
            self.login_failed(now, name)
            raise ApiError(401, LOGIN_FAILED)
        with st.mu:
            old = st.session
            st.session = Session(name)
            st.user_failures.pop(name, None)
            st.failures = []
            sess = st.session
            sess.changed = cred_stamp(users.get(name)) if isinstance(users, dict) else None
        if old is not None:
            st.audit("session-replaced", session=old.id, user=old.user, new_user=name)
        st.audit("login", session=sess.id, user=name)
        cookie = "%s=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=%d" % (COOKIE, sess.secret, self.cfg.session_max)
        self.send_json(200, self.session_info(sess), cookie=cookie)

    def login_failed(self, now, user):
        """T3: one failure toward the lockout (5 in 10 minutes -> 15 minutes); user = the name if it passed
        USER_RE, else "(invalid)" (never the password)."""
        st = self.st
        with st.mu:
            # B7: one name: FAIL_MAX in FAIL_WINDOW locks that name; every name together: GLOBAL_FAIL_MAX locks all
            mine = [t for t in st.user_failures.get(user, []) if now - t < FAIL_WINDOW] + [now]
            st.user_failures[user] = mine
            if len(mine) >= FAIL_MAX and user != "(invalid)":  # a name outside USER_RE locks no account
                st.user_locked[user] = now + LOCK_FOR
                st.user_failures[user] = []
                st.audit("locked", seconds=LOCK_FOR, user=user, scope="user")
            st.failures = [t for t in st.failures if now - t < FAIL_WINDOW] + [now]
            if len(st.failures) >= GLOBAL_FAIL_MAX:
                st.locked_until, st.failures = now + LOCK_FOR, []
                st.audit("locked", seconds=LOCK_FOR, user=user, scope="all")
            st.audit("login-failed", user=user)

    def session_info(self, s):
        now = time.time()
        return {"session": s.id, "user": s.user, "expires_in": int(s.created + self.cfg.session_max - now),
                "idle_timeout": self.cfg.idle, "doctor_ok": s.doctor_ok, "version": __version__,
                "ui_version": __version__, "features": dict(UI_FEATURES)}

    def user_still_valid(self, sess):
        """B4: the account of the session still exists with the password it logged in with (the users file is read
        again: migui-passwd remove or passwd ends the session at its next request)."""
        try:
            users = auth.load_users(self.cfg.users_file)
        except auth.UsersFileError:
            return False
        rec = users.get(sess.user) if isinstance(users, dict) else None
        if not isinstance(rec, dict) or cred_stamp(rec) != sess.changed:
            with self.st.mu:
                if self.st.session is sess:
                    self.st.session = None
            self.st.audit("session-revoked", session=sess.id, user=sess.user)
            return False
        return True

    def jobs_query(self):
        """D9: offset, limit, q, state, kind, from, to of the query string (checked again by the runner)."""
        qs = parse_qs(urlsplit(self.path).query, keep_blank_values=False)
        got = {}
        for key in ("q", "state", "kind", "from", "to"):
            if key in qs:
                got[key] = qs[key][0][:200]
        for key in ("offset", "limit"):
            if key in qs:
                try:
                    got[key] = int(qs[key][0])
                except ValueError:
                    raise ApiError(400, "%s: a whole number" % key)
        if self.path.split("?", 1)[0] == "/api/jobs.csv" and "limit" not in got:
            got["limit"] = 500
        return got

    def jobs_csv(self, jobs):
        """D9: the jobs of the query as CSV (a cell that a spreadsheet would run as a formula starts with a quote)."""
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(JOBS_CSV_COLUMNS)
        for j in jobs:
            row = []
            for c in JOBS_CSV_COLUMNS:
                v = j.get(c)
                v = "" if v is None else str(v)
                if v[:1] in ("=", "+", "-", "@", "\t", "\r"):
                    v = "'" + v
                row.append(v)
            w.writerow(row)
        body = buf.getvalue().encode("utf-8")
        self.send_response(200)
        self.headers_common()
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="migui-jobs.csv"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # --- GET -----------------------------------------------------------------------------------------------------
    def get(self, path, sess):
        if path == "/api/session":
            return self.send_json(200, self.session_info(sess))
        if path in ("/api/jobs", "/api/jobs.csv"):
            msg = dict(self.jobs_query(), op="status", session=sess.id, user=sess.user)
            r = collect(self.cfg, msg, 60)
            if path == "/api/jobs.csv":
                return self.jobs_csv(pick(r, "jobs") or [])
            return self.send_json(200, {"jobs": pick(r, "jobs") or [], "total": pick(r, "total"),
                                        "active": pick(r, "active")})
        m = JOB_PATH.match(path)
        if not m or m.group(2) == "/cancel":
            raise ApiError(404, "not found")
        jid, sub = m.group(1), m.group(2)
        if sub is None:
            r = collect(self.cfg, {"op": "status", "session": sess.id, "user": sess.user, "job_id": jid}, 60)
            return self.send_json(200, {"job": pick(r, "job")})
        if sub == "/log":
            return self.log_text(sess, jid)
        return self.sse(sess, jid)

    def log_text(self, sess, jid):
        lines = []
        msg = {"op": "status", "session": sess.id, "user": sess.user, "job_id": jid, "lines": True}
        for obj in runner_call(self.cfg, msg, 120):
            if "line" in obj:
                lines.append("[%s] %s" % (obj.get("t", ""), obj["line"]))
            elif "event" in obj:
                lines.append("[%s] @@ %s" % (obj.get("t", ""), dumps(obj["event"])))
        body = ("\n".join(lines) + "\n").encode("utf-8")
        self.send_response(200)
        self.headers_common()
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="migui-job-%s.log"' % jid)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def sse(self, sess, jid):
        """Server-sent events of one job: state (the record), line, progress (@@ events), ping, end."""
        try:
            frm = int(self.headers.get("Last-Event-ID") or "0")
        except ValueError:
            frm = 0
        frm = max(0, frm)
        it = runner_call(self.cfg, {"op": "status", "session": sess.id, "user": sess.user, "job_id": jid,
                                    "follow": True, "from": frm})
        first = next(it)  # an error (404) raises before the 200 below
        self.sse_headers()
        ev = self.sse_event
        try:
            ev("state", first.get("job"))
            for obj in it:
                if self.st.current(self.cookie(), touch=False) is None:
                    ev("end", {"rc": None, "reason": "session ended"})
                    return
                if "line" in obj:
                    ev("line", {"n": obj.get("n"), "t": obj.get("t"), "text": obj["line"]}, obj.get("n"))
                elif "event" in obj:
                    ev("progress", {"n": obj.get("n"), "t": obj.get("t"), "event": obj["event"]}, obj.get("n"))
                elif "ping" in obj:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                elif "job" in obj:
                    ev("state", obj["job"])
                elif "end" in obj:
                    ev("end", {"rc": obj["end"]})
        except ApiError as e:
            ev("end", {"rc": None, "reason": e.message})
        finally:
            it.close()

    def sse_headers(self):
        self.send_response(200)
        self.headers_common()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def sse_event(self, name, data, eid=None):
        out = ""
        if eid is not None:
            out += "id: %d\n" % eid
        out += "event: %s\ndata: %s\n\n" % (name, dumps(data))
        self.wfile.write(out.encode("utf-8"))
        self.wfile.flush()

    def plan_stream(self, sess, kind, items, msg):
        """18: POST /api/plan answers server-sent events while the runner plans: planning (a migrate plan starts),
        line (one line of that plan as it prints it, scrubbed by the runner, 20), row (its result), batch (the PLANNED batch, or null), end {rc}, error {error, status} (the runner failed
        mid-way); a ': ping' comment after PING s without an event. A refusal before the first event (400, 409, 429
        of the runner) is a plain JSON error with its status, like every other POST. A reader thread drains the
        runner: a browser that goes away mid-plan stops the writes, never the plan; the batch is still remembered in
        the session and the audit line 'plan' is written when the runner ends, as before."""
        q = queue.Queue()

        def reader():
            try:
                for obj in runner_call(self.cfg, msg):
                    q.put(("obj", obj))
            except ApiError as e:
                q.put(("err", e))
            except Exception as e:  # never leave the handler waiting on the queue
                q.put(("err", ApiError(502, "runner stream: %s" % type(e).__name__)))
            q.put(("eof", None))
        threading.Thread(target=reader, name="plan-" + sess.id, daemon=True).start()
        what, first = q.get()
        if what == "err":
            raise first
        self.sse_headers()
        client = [True]

        def send(name, data):
            if not client[0]:
                return
            try:
                if name is None:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                else:
                    self.sse_event(name, data)
            except OSError:
                client[0] = False  # the browser left: keep draining the runner (the plan goes on)
        batch, item = None, (what, first)
        while True:
            if item is None:
                try:
                    item = q.get(timeout=PING)
                except queue.Empty:
                    send(None, None)
                    continue
            what, obj = item
            item = None
            if what == "eof":
                break
            with self.st.mu:  # C1: a plan that runs long keeps its session alive
                if self.st.session is sess:
                    sess.last = time.time()
            if what == "err":
                send("error", {"error": obj.message, "status": obj.status})
                continue
            if "planning" in obj:
                send("planning", obj["planning"])
            elif "line" in obj:
                send("line", obj["line"])
            elif "row" in obj:
                send("row", obj["row"])
            elif "batch" in obj:
                batch = obj["batch"]
                if batch:
                    with self.st.mu:
                        sess.remember(batch["id"], kind)
                send("batch", batch)
            elif "end" in obj:
                self.st.audit("plan", session=sess.id, user=sess.user, rc=obj["end"], batch=(batch or {}).get("id"),
                              idents=[i for i, _ in items], client="connected" if client[0] else "gone")
                send("end", {"rc": obj["end"]})

    # --- POST ----------------------------------------------------------------------------------------------------
    def post(self, path, sess, body):
        cfg = self.cfg
        if path == "/api/logout":
            with self.st.mu:
                if self.st.session is sess:
                    self.st.session = None
            self.st.audit("logout", session=sess.id, user=sess.user)
            return self.send_json(200, {"ok": True}, cookie="%s=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0" % COOKIE)
        if path in MUTATING and not self.st.audit("request", session=sess.id, user=sess.user, path=path):
            raise ApiError(503, "the audit log cannot be written: nothing runs until it can (B5)")
        if path == "/api/doctor":
            r = collect(cfg, {"op": "doctor", "session": sess.id, "user": sess.user,
                              "write_check": body.get("write_check") is True})
            data = pick(r, "data")
            sess.doctor_ok = bool(isinstance(data, dict) and data.get("passed") is True and end_rc(r) == 0)
            self.st.audit("doctor", session=sess.id, user=sess.user, rc=end_rc(r), passed=sess.doctor_ok)
            return self.send_json(200, {"data": data, "stderr": pick(r, "stderr"), "rc": end_rc(r),
                                        "doctor_ok": sess.doctor_ok})
        if path == "/api/status":
            r = collect(cfg, {"op": "status", "session": sess.id, "user": sess.user, "cli": True})
            return self.send_json(200, {"jobs": pick(r, "jobs") or [], "active": pick(r, "active"),
                                        "cli": pick(r, "data"), "rc": end_rc(r)})
        gated = path in ("/api/inventory", "/api/list", "/api/plan")
        m = BATCH_PATH.match(path)
        if m and m.group(2) == "run":
            with self.st.mu:
                gated = sess.batches.get(m.group(1)) in ("vm", "vol")  # D3: a rollback is never gated
        if gated and not sess.doctor_ok:
            raise ApiError(412, "run Doctor first: it must pass before inventory, list, plan and run")
        if path == "/api/inventory":
            r = collect(cfg, {"op": "inventory", "session": sess.id, "user": sess.user})
            return self.send_json(200, {"data": pick(r, "data"), "catalog": pick(r, "catalog"),
                                        "stderr": pick(r, "stderr"), "rc": end_rc(r)})
        if path == "/api/list":
            msg = {"op": "list", "session": sess.id, "user": sess.user, "hosts": body.get("hosts") or [],
                   "projects": body.get("projects") or [], "volumes": body.get("volumes") is True,
                   "scope": body.get("scope") or ""}
            r = collect(cfg, msg)
            return self.send_json(200, {"data": pick(r, "data"), "stderr": pick(r, "stderr"), "argv": pick(r, "argv"),
                                        "rc": end_rc(r)})
        if path == "/api/plan":
            kind = body.get("kind")
            items = check_items(kind if kind in ("vm", "vol") else "vm", body.get("items"))  # 6.1: web checks first
            normalize_options(kind, body.get("options") or {})
            msg = {"op": "plan", "session": sess.id, "user": sess.user, "kind": kind,
                   "options": body.get("options") or {},
                   "items": [{"ident": i, "ip": ip} if ip else {"ident": i} for i, ip in items]}
            return self.plan_stream(sess, kind, items, msg)
        if path == "/api/rollback":
            ident = check_ident(body.get("ident"))
            r = collect(cfg, {"op": "rollback", "session": sess.id, "user": sess.user, "ident": ident})
            batch = pick(r, "batch")
            with self.st.mu:
                sess.remember(batch["id"], "rollback")
            self.st.audit("rollback-plan", session=sess.id, user=sess.user, batch=batch["id"], idents=[ident])
            return self.send_json(200, {"batch": batch, "original": pick(r, "original"), "state": pick(r, "state")})
        m = BATCH_PATH.match(path)
        if m:
            bid, what = m.groups()
            if what == "stop":
                r = collect(cfg, {"op": "cancel", "session": sess.id, "user": sess.user, "batch_id": bid,
                                  "after_current": True}, 30)
                return self.send_json(200, {"batch": pick(r, "batch")})
            with self.st.mu:
                kind = sess.batches.get(bid)
            if kind is None:
                self.st.audit("rejected", session=sess.id, user=sess.user, batch=bid,
                              why="batch not planned by this session")
                raise ApiError(409, "this batch was not planned in this session: plan again")
            token = body.get("confirm_token")
            if not isinstance(token, str) or not re.match(r"^[0-9a-f]{32}$", token):
                raise ApiError(409, "confirm_token missing or malformed")
            msg = {"op": RUN_OPS[kind], "session": sess.id, "user": sess.user, "batch": bid, "confirm_token": token,
                   "typed": body.get("typed"), "continue_on_error": body.get("continue_on_error") is True}
            try:
                r = collect(cfg, msg, 120)
            except ApiError as e:
                if e.status == 409:
                    with self.st.mu:
                        sess.batches.pop(bid, None)
                self.st.audit("run-refused", session=sess.id, user=sess.user, batch=bid, status=e.status)
                raise
            with self.st.mu:
                sess.batches.pop(bid, None)
            b = pick(r, "batch") or {}
            self.st.audit("run", session=sess.id, user=sess.user, batch=bid, argv=b.get("commands"),
                          jobs=pick(r, "jobs"))
            return self.send_json(202, {"batch": b, "jobs": pick(r, "jobs")})
        m = JOB_PATH.match(path)
        if m and m.group(2) == "/cancel":
            jid = m.group(1)
            msg = {"op": "cancel", "session": sess.id, "user": sess.user, "job_id": jid,
                   "force": body.get("force") is True}
            if body.get("typed") is not None:
                msg["typed"] = body.get("typed")
            r = collect(cfg, msg, 30)
            self.st.audit("cancel" if not msg["force"] else "kill", session=sess.id, user=sess.user, job=jid)
            return self.send_json(200, {"cancel": pick(r, "cancel"), "force_in": pick(r, "force_in")})
        raise ApiError(404, "not found")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


MUTATING_FIXED = ("/api/plan", "/api/rollback", "/api/doctor")


class _Mutating:
    """B5: the POST paths that start or change something (each one first writes an audit line)."""

    def __contains__(self, path):
        return path in MUTATING_FIXED or bool(BATCH_PATH.match(path)) or (
            bool(JOB_PATH.match(path)) and path.endswith("/cancel"))


MUTATING = _Mutating()


def toucher(state, cfg):
    """C1: every TOUCH_EVERY s, the session whose batch the runner runs stays alive (a long batch is not watched by
    a request that touches it: the job stream does not)."""
    while True:
        time.sleep(TOUCH_EVERY)
        with state.mu:
            s = state.session
        if s is None:
            continue
        try:
            act = pick(collect(cfg, {"op": "status", "session": s.id, "user": s.user, "limit": 1}, 30), "active")
        except (ApiError, OSError):
            continue
        if isinstance(act, dict) and act.get("session") == s.id:
            with state.mu:
                if state.session is s:
                    s.last = time.time()


def parse_args(argv):
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(prog="migui-web", description="web half of the migrate UI (DESIGN.md section 8.1)")
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--origin-port", type=int, default=0, help="port the browser uses when ssh -L maps another local "
                                                               "port (default: --port)")
    ap.add_argument("--socket", default="/run/migui/runner.sock")
    ap.add_argument("--users-file", default=auth.DEFAULT_USERS, help="accounts written by migui-passwd (16.1), read at "
                                                                  "each login")
    ap.add_argument("--audit", default="/var/log/migui/audit.jsonl")
    ap.add_argument("--static", default=os.path.join(here, "static"))
    ap.add_argument("--session-max", type=int, default=8 * 3600)
    ap.add_argument("--idle", type=int, default=30 * 60)
    a = ap.parse_args(argv)
    try:
        loop = ipaddress.ip_address(a.bind).is_loopback
    except ValueError:
        loop = False
    if not loop:
        # T1 [invariant]: loopback only; the operator comes in through ssh -L. 7.3 has no TLS mode.
        ap.exit(2, "migui-web: refusing to listen on %s: only a loopback address (127.0.0.1, ::1) is allowed, "
                   "open the UI through 'ssh -L %d:127.0.0.1:%d' (DESIGN.md section 8.3 T1)\n" % (a.bind, a.port, a.port))
    op = a.origin_port or a.port
    a.hosts = {"127.0.0.1:%d" % op, "localhost:%d" % op}
    if op != a.port:
        a.hosts |= {"127.0.0.1:%d" % a.port, "localhost:%d" % a.port}
    a.origins = {"http://" + h for h in a.hosts}
    return a


def main(argv=None):
    cfg = parse_args(sys.argv[1:] if argv is None else argv)
    os.umask(0o077)
    try:
        auth.self_test()  # 16.1: a host whose scrypt cannot run says so now, not at the first login
    except auth.ScryptError as e:
        sys.stderr.write("migui-web: %s\n" % e.message)
        return 2
    state = State(cfg)
    srv = Server((cfg.bind, cfg.port), Handler)
    srv.state = state
    threading.Thread(target=toucher, args=(state, cfg), name="toucher", daemon=True).start()
    if cfg.port == 0:
        cfg.port = srv.server_address[1]
    state.audit("web-start", version=__version__, bind="%s:%d" % (cfg.bind, srv.server_address[1]))
    sys.stderr.write("migui-web %s: http://%s:%d/ (runner %s)\n" % (__version__, cfg.bind, srv.server_address[1],
                                                                   cfg.socket))
    sys.stderr.flush()
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
