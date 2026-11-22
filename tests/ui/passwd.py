#!/usr/bin/env python3
"""migui-passwd and migui/auth.py (DESIGN-UI 16.1, 16.2, 16.4): add/set/del/list, --stdin, the name and password
rules, the file mode, the atomic write, the audit, and scrypt with the parameters of 16.1. Cases PW-<n>. Most run
the tool as a process (python -I -c 'from migui.passwd import main' --users-file <tmp>); the ones that patch
something (getpass, os.replace, geteuid, hashlib.scrypt) call migui.passwd.main() in this process.

    python3 -I tests/ui/passwd.py <repo>
"""
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness as H  # noqa: E402
from harness import ok  # noqa: E402

sys.path.insert(0, H.REPO)
from migui import auth, passwd  # noqa: E402

PW = "a-long-enough-password-1"
PW2 = "another-long-password-22"
TMP = tempfile.mkdtemp(prefix="migui-test-passwd-", dir=H.WORK)
USERS = os.path.join(TMP, "users")
AUDIT = os.path.join(TMP, "audit.jsonl")


def tool(*args, stdin=None, users=USERS, umask=None):
    """migui-passwd as a process; stdin given -> --stdin. -> (rc, stdout, stderr)."""
    argv = [H.PY, "-I", "-c", H.BOOT % "passwd", H.REPO, "--users-file", users, "--audit", AUDIT] + list(args)
    if stdin is not None:
        argv.append("--stdin")
    pre = (lambda: os.umask(umask)) if umask is not None else None
    p = subprocess.run(argv, input=stdin if stdin is not None else "", capture_output=True, text=True, timeout=60,
                       preexec_fn=pre)
    return p.returncode, p.stdout, p.stderr


def inproc(argv, stdin=""):
    """migui.passwd.main(argv) in this process: (rc, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    old_in = sys.stdin
    sys.stdin = io.StringIO(stdin)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = passwd.main(argv)
    finally:
        sys.stdin = old_in
    return rc, out.getvalue(), err.getvalue()


def data(path=USERS):
    with open(path, "rb") as f:
        return f.read()


def users(path=USERS):
    return json.loads(data(path))["users"]


def listing(d=TMP):
    return sorted(os.listdir(d))


try:
    # ---------------------------------------------------------------- scrypt with the 16.1 parameters (mig-host 3.12)
    real = hashlib.scrypt
    seen = []

    def spy(*a, **kw):
        seen.append(kw)
        return real(*a, **kw)
    hashlib.scrypt = spy
    try:
        rec = auth.hash_password(PW)
        good, bad = auth.verify_password({"u": dict(rec, created="", changed="")}, "u", PW), \
            auth.verify_password({"u": dict(rec, created="", changed="")}, "u", PW2)
    finally:
        hashlib.scrypt = real
    ok("PW-1 hash_password() with n=2**15 r=8 p=1 dklen 32 succeeds on this Python/OpenSSL (maxmem 64 MiB passed "
       "explicitly; the default 32 MiB refuses these parameters) and verifies",
       rec["n"] == 32768 and rec["r"] == 8 and rec["p"] == 1 and len(rec["salt"]) == 32 and len(rec["hash"]) == 64
       and good is True and bad is False and all(k.get("maxmem") == 64 * 1024 * 1024 and k.get("n") == 32768
                                                 for k in seen) and len(seen) == 3, (rec, seen))
    try:
        hashlib.scrypt(b"x", salt=b"0" * 16, n=32768, r=8, p=1, dklen=32)
        default_ok = True
    except ValueError:
        default_ok = False
    print("INFO hashlib.scrypt(n=2**15, r=8) without maxmem on this host: %s" % ("works" if default_ok else
                                                                                "ValueError (memory limit)"), flush=True)
    hashlib.scrypt = lambda *a, **kw: (_ for _ in ()).throw(ValueError("memory limit exceeded"))
    try:
        rc, out, err = inproc(["--users-file", USERS, "--audit", AUDIT, "add", "nomem", "--stdin"], PW + "\n")
    finally:
        hashlib.scrypt = real
    ok("PW-1 scrypt refusing to run (ValueError): exit 1 'scrypt unavailable or memory limit', nothing written",
       rc == 1 and "scrypt unavailable or memory limit" in err and not os.path.exists(USERS), (rc, err))

    # ---------------------------------------------------------------- add
    rc, out, err = tool("add", "test", stdin=PW + "\n")
    st = os.stat(USERS) if os.path.exists(USERS) else None
    u = users() if st else {}
    ok("PW-2 add test --stdin: exit 0; users file version 1 with one scrypt record (n 32768, r 8, p 1, 16-byte salt, "
       "32-byte hash, created = changed)",
       rc == 0 and json.loads(data())["version"] == 1 and list(u) == ["test"]
       and {k: u["test"][k] for k in ("kdf", "n", "r", "p")} == {"kdf": "scrypt", "n": 32768, "r": 8, "p": 1}
       and len(u["test"]["salt"]) == 32 and len(u["test"]["hash"]) == 64
       and u["test"]["created"] == u["test"]["changed"], (rc, out, err, u))
    ok("PW-2 the file is mode 0640 (umask 077 of the tool, fchmod explicit), no temporary file left, users.lock "
       "beside it", st is not None and stat.S_IMODE(st.st_mode) == 0o640
       and listing() == ["audit.jsonl", "users", "users.lock"], (oct(st.st_mode) if st else None, listing()))
    ok("PW-2 the password is not in the file, not on stdout/stderr", PW not in data().decode() and PW not in out + err)
    ok("PW-2 verify_password() of the stored record: right True, wrong False",
       auth.verify_password(auth.load_users(USERS), "test", PW) is True
       and auth.verify_password(auth.load_users(USERS), "test", PW2) is False)
    for um in (0o000, 0o022):
        os.unlink(USERS)
        rc, out, err = tool("add", "test", stdin=PW + "\n", umask=um)
        ok("PW-3 add under umask %03o: the file is still exactly 0640" % um,
           rc == 0 and stat.S_IMODE(os.stat(USERS).st_mode) == 0o640, (rc, err, oct(os.stat(USERS).st_mode)))

    before = data()
    rc, out, err = tool("add", "test", stdin=PW2 + "\n")
    ok("PW-4 add of an existing name: exit 1, 'exists', file unchanged", rc == 1 and "exists" in err
       and data() == before, (rc, err))

    # ---------------------------------------------------------------- password rules (16.1)
    cases = {"11 characters": ("alice", "abcdefghijk"), "129 characters": ("alice", "p" * 129),
             "= name": ("operator.one", "operator.one"), "= name, other case": ("operator.one", "OPERATOR.ONE"),
             "a tab": ("alice", "abcdef\tghijklmn"), "a control character": ("alice", "abcdefghijkl\x07m"),
             "empty": ("alice", "")}
    res = {}
    for k, (name, pw) in cases.items():
        rc, out, err = tool("add", name, stdin=pw + "\n")
        res[k] = (rc, err.strip(), pw in err if len(pw) > 3 else False)
    ok("PW-5 refused (exit 1, nothing written, the password never echoed): < 12 characters, > 128, the user name in "
       "any case, a tab or control character, empty",
       all(v[0] == 1 and "nothing written" in v[1] and not v[2] for v in res.values()) and data() == before, res)
    rc, out, err = tool("add", "alice", stdin="twelve chars" + "\n")
    rc2, out2, err2 = tool("add", "bob", stdin="ünïcødé pässwörd 1" + "\n")
    ok("PW-5 accepted: exactly 12 characters (a space inside), and non-ASCII printable characters",
       rc == 0 and rc2 == 0 and sorted(users()) == ["alice", "bob", "test"], (rc, err, rc2, err2))

    # ---------------------------------------------------------------- name rules (16.1)
    before = data()
    res = {}
    for name in ("Test", "1abc", "a b", "a" * 33, "../x", "x/y", "tést", "a;id", "_x"):
        rc, out, err = tool("add", name, stdin=PW + "\n")
        res[name] = (rc, "user name" in err)
    rc_dash, _o, err_dash = tool("add", "-x", stdin=PW + "\n")
    ok("PW-6 names outside ^[a-z][a-z0-9._-]{0,31}$: exit 1 'user name', nothing written ('-x' is an unknown option: "
       "exit 2)", all(v == (1, True) for v in res.values()) and rc_dash == 2 and data() == before, (res, rc_dash))
    rc, out, err = tool("add", "a" * 32, stdin=PW + "\n")
    ok("PW-6 a name of 32 characters is fine", rc == 0 and "a" * 32 in users(), (rc, err))

    # ---------------------------------------------------------------- set
    old = users()["test"]
    rc, out, err = tool("set", "test", stdin=PW2 + "\n")
    new = users()["test"]
    ok("PW-7 set test: exit 0, new salt and hash, created kept, the new password verifies and the old does not",
       rc == 0 and new["salt"] != old["salt"] and new["hash"] != old["hash"] and new["created"] == old["created"]
       and auth.verify_password(users(), "test", PW2) and not auth.verify_password(users(), "test", PW), (rc, err))
    before = data()
    r1 = tool("set", "nobody", stdin=PW + "\n")
    r2 = tool("del", "nobody")
    ok("PW-8 set and del of a name that does not exist: exit 1 'no user', file unchanged",
       r1[0] == 1 and r2[0] == 1 and "no user" in r1[2] and "no user" in r2[2] and data() == before, (r1, r2))
    rc, out, err = tool("set", "test", stdin="test" * 3 + "\n")
    ok("PW-8 a password that contains the name but is not the name ('testtesttest' for user test) is accepted",
       rc == 0, (rc, err))
    rc, out, err = tool("set", "test", stdin="short\n")
    ok("PW-8 set with a short password: exit 1, the old record stays", rc == 1 and users()["test"] == json.loads(
        data())["users"]["test"] and auth.verify_password(users(), "test", "testtesttest"), (rc, err))

    # ---------------------------------------------------------------- list
    rc, out, err = tool("list")
    recs = users()
    secrets_ = [v for r in recs.values() for k, v in r.items() if k in ("salt", "hash")]
    ok("PW-9 list: exit 0, every name with created/changed, never a salt or a hash",
       rc == 0 and all(n in out for n in recs) and "created" in out and "changed" in out
       and not any(x in out + err for x in secrets_) and "salt" not in out and "hash" not in out, out)
    rc, out, err = tool("list", users=os.path.join(TMP, "nothing-here"))
    ok("PW-9 list without a users file: exit 1 and says how to create the first user",
       rc == 1 and "migui-passwd add" in err, err)

    # ---------------------------------------------------------------- password never from argv / env
    before = data()
    r1 = tool("add", "carol", PW)
    r2 = tool("add", "carol", "--password", PW)
    r3 = tool("add", "carol", "--password=" + PW)
    ok("PW-10 a password on the command line is refused (exit 2, nothing written, never echoed): extra argument, "
       "--password X, --password=X",
       [r[0] for r in (r1, r2, r3)] == [2, 2, 2] and not any(PW in r[1] + r[2] for r in (r1, r2, r3))
       and data() == before, (r1, r2, r3))
    src = open(os.path.join(H.REPO, "migui", "passwd.py"), encoding="utf-8").read()
    ok("PW-10 migui/passwd.py reads no environment variable (no os.environ, no getenv)",
       not re.search(r"os.environ|getenv", src))
    rc, out, err = tool("add", "carol", stdin="")
    ok("PW-10 --stdin with nothing on stdin: exit 1, nothing written", rc == 1 and data() == before, (rc, err))

    # ---------------------------------------------------------------- getpass: twice, must match
    import getpass
    answers = iter([PW, PW2])
    real_gp = getpass.getpass
    getpass.getpass = lambda prompt="": next(answers)
    try:
        rc, out, err = inproc(["--users-file", USERS, "--audit", AUDIT, "add", "dave"])
        answers = iter([PW, PW])
        rc2, out2, err2 = inproc(["--users-file", USERS, "--audit", AUDIT, "add", "dave"])
    finally:
        getpass.getpass = real_gp
    ok("PW-11 without --stdin the password is asked twice (getpass): different answers exit 1 'do not match', "
       "the same twice adds the user",
       rc == 1 and "do not match" in err and rc2 == 0 and "dave" in users(), (rc, err, rc2, err2))

    # ---------------------------------------------------------------- del
    rc, out, err = tool("del", "dave")
    ok("PW-12 del dave: exit 0, gone", rc == 0 and "dave" not in users(), (rc, err))
    for n in list(users()):
        tool("del", n)
    ok("PW-12 del of the last user: exit 0 with a warning 'no users left: nobody can log in'; the file stays, empty",
       users() == {} and tool("list")[0] == 0, users())
    rc, out, err = tool("add", "erin", stdin=PW + "\n")
    rc_last, out_last, err_last = tool("del", "erin")
    ok("PW-12 the warning is printed on the last del", rc_last == 0 and "no users left: nobody can log in" in err_last,
       err_last)

    # ---------------------------------------------------------------- audit
    lines = [json.loads(x) for x in open(AUDIT) if x.strip()]
    acts = [(a["who"], a["action"], (a.get("detail") or {}).get("user")) for a in lines]
    text = open(AUDIT).read()
    ok("PW-13 audit: who=passwd, user-add / user-set / user-del with detail user; never a password, salt or hash",
       ("passwd", "user-add", "test") in acts and ("passwd", "user-set", "test") in acts
       and ("passwd", "user-del", "dave") in acts and all(a[0] == "passwd" for a in acts)
       and not any(p in text for p in (PW, PW2, "testtesttest")) and '"salt"' not in text and '"hash"' not in text,
       acts)

    # ---------------------------------------------------------------- atomic write: a failed write leaves no torn file
    tool("add", "frank", stdin=PW + "\n")
    before, names = data(), listing()
    real_replace = os.replace

    def broken_replace(a, b):
        raise OSError(28, "No space left on device")
    os.replace = broken_replace
    try:
        rc, out, err = inproc(["--users-file", USERS, "--audit", AUDIT, "add", "grace", "--stdin"], PW + "\n")
    finally:
        os.replace = real_replace
    ok("PW-14 os.replace fails (ENOSPC): exit 1 'the old file is unchanged', the old file byte for byte, the "
       "temporary file removed", rc == 1 and "unchanged" in err and data() == before and listing() == names,
       (rc, err, listing()))
    real_write = os.write
    os.write = lambda fd, b: (_ for _ in ()).throw(OSError(5, "Input/output error"))
    try:
        rc, out, err = inproc(["--users-file", USERS, "--audit", AUDIT, "add", "grace", "--stdin"], PW + "\n")
    finally:
        os.write = real_write
    ok("PW-14 the write itself fails (EIO): exit 1, old file whole, no temporary file",
       rc == 1 and data() == before and listing() == names, (rc, err, listing()))
    if os.geteuid() != 0:
        os.chmod(TMP, 0o500)
        try:
            rc, out, err = tool("add", "grace", stdin=PW + "\n")
        finally:
            os.chmod(TMP, 0o700)
        ok("PW-14 a read-only directory (not root): exit 1, old file whole, nothing left behind",
           rc == 1 and data() == before and listing() == names, (rc, err))
    else:
        print("SKIP PW-14 read-only directory: root ignores directory modes (the os.replace/os.write cases cover it)",
              flush=True)

    # ---------------------------------------------------------------- corrupt file: never overwritten
    with open(USERS, "wb") as f:
        f.write(b"{not json")
    rc, out, err = tool("add", "henry", stdin=PW + "\n")
    ok("PW-15 a corrupt users file: add refuses (exit 1 'fix it or move it away'), the file is left as it was",
       rc == 1 and "move it away" in err and data() == b"{not json", (rc, err))
    with open(USERS, "wb") as f:
        f.write(before)

    # ---------------------------------------------------------------- default path: root only, usage
    real_euid = os.geteuid
    os.geteuid = lambda: 1000
    try:
        rcs = [inproc(argv)[0] for argv in (["list"], ["add", "zed", "--stdin"], ["--users-file", auth.DEFAULT_USERS,
                                                                                   "del", "zed"])]
        msg = inproc(["list"])[2]
    finally:
        os.geteuid = real_euid
    ok("PW-16 the default users file (%s) as a non-root user: exit 2 'run as root' for every command, before any "
       "file is touched" % auth.DEFAULT_USERS, rcs == [2, 2, 2] and "run as root" in msg, (rcs, msg))
    usage = [tool()[0], tool("frob")[0], tool("list", "x")[0], tool("add")[0], tool("--help")[0]]
    ok("PW-16 usage: no command / unknown command / extra or missing argument: exit 2; --help: 0",
       usage == [2, 2, 2, 2, 0], usage)
finally:
    shutil.rmtree(TMP, ignore_errors=True)

sys.exit(H.total())
