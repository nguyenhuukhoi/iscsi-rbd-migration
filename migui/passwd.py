"""migui-passwd (DEPLOY.md section 6): the accounts of the web UI, root only.

    migui-passwd add  <user>        create; asks the password twice (getpass, no echo)
    migui-passwd set  <user>        change the password
    migui-passwd del  <user>        remove
    migui-passwd list               names with created/changed (never a hash or a salt)
       --stdin                      read one line (the password) from stdin, for scripts
       --users-file PATH            default /var/lib/migui/users (tests use a temporary path)
       --audit PATH                 default /var/log/migui/audit.jsonl with the default users file, none otherwise

A password is never taken from argv or the environment. With the default users file the tool must run as root
(exit 2 otherwise) and the file is root:migui 0640; with another path the owner is left alone. Every change is a
new file written next to the old one (O_EXCL, 0640) and renamed over it (os.replace) under flock(users.lock): a
failed write leaves the old file whole and no temporary file behind. Exit 0 done, 1 refused (nothing written),
2 usage.
"""
import errno
import fcntl
import getpass
import grp
import os
import secrets
import sys
import time

from . import auth
from .common import Audit

DEFAULT_AUDIT = "/var/log/migui/audit.jsonl"
GROUP = "migui"
COMMANDS = ("add", "set", "del", "list")
USAGE = """usage: migui-passwd [--users-file PATH] [--audit PATH] [--stdin] {add,set,del} USER
       migui-passwd [--users-file PATH] list

  add USER    create an account (password asked twice, or one line on stdin with --stdin)
  set USER    change its password
  del USER    remove it
  list        names, created, changed

  --stdin             read the password as one line from stdin (never from argv or the environment)
  --users-file PATH   default %s (root only, root:%s 0640)
  --audit PATH        default %s with the default users file, no audit line otherwise

User name: a lower-case letter then a-z 0-9 . _ - (at most 32). Password: %d-%d printable characters, not the
user name.
""" % (auth.DEFAULT_USERS, GROUP, DEFAULT_AUDIT, auth.PW_MIN, auth.PW_MAX)
LOCK_WAIT = 10


class Fail(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def parse(argv):
    """-> dict(cmd, user, users_file, audit, stdin). Never echoes an argument it does not understand (it could be a
    password typed in the wrong place)."""
    o = {"users_file": auth.DEFAULT_USERS, "audit": None, "stdin": False, "help": False}
    pos = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            o["help"] = True
        elif a == "--stdin":
            o["stdin"] = True
        elif a in ("--users-file", "--audit"):
            if i + 1 >= len(argv) or not argv[i + 1]:
                raise Fail(2, "%s needs a path" % a)
            o[a[2:].replace("-", "_")] = argv[i + 1]
            i += 1
        elif a.startswith("--users-file=") or a.startswith("--audit="):
            k, _, v = a.partition("=")
            if not v:
                raise Fail(2, "%s needs a path" % k)
            o[k[2:].replace("-", "_")] = v
        elif a.startswith("-"):
            raise Fail(2, "unknown option (see --help); a password is never given on the command line")
        else:
            pos.append(a)
        i += 1
    if o["help"]:
        return o
    if not pos or pos[0] not in COMMANDS:
        raise Fail(2, "a command is required: add, set, del or list (see --help)")
    want = 0 if pos[0] == "list" else 1
    if len(pos) != want + 1:
        raise Fail(2, "%s takes %s (a password is never given on the command line: use the prompt or --stdin)"
                   % (pos[0], "no argument" if want == 0 else "exactly one user name"))
    o["cmd"] = pos[0]
    o["user"] = pos[1] if want else None
    return o


def read_password(name, use_stdin):
    if use_stdin:
        line = sys.stdin.readline(auth.PW_MAX + 3)
        if not line:
            raise Fail(1, "no password on stdin; nothing written")
        pw = line[:-1] if line.endswith("\n") else line
        if pw.endswith("\r"):
            pw = pw[:-1]
        if len(pw) > auth.PW_MAX:
            raise Fail(1, "password: %d to %d characters; nothing written" % (auth.PW_MIN, auth.PW_MAX))
    else:
        try:
            pw = getpass.getpass("New password for %s: " % name)
            again = getpass.getpass("Again: ")
        except (EOFError, KeyboardInterrupt):
            raise Fail(1, "aborted; nothing written")
        if pw != again:
            raise Fail(1, "the two passwords do not match; nothing written")
    try:
        auth.check_password(name, pw)
    except auth.PolicyError as e:
        raise Fail(1, e.message + "; nothing written")
    return pw


def read_users(path, missing_ok):
    try:
        return auth.load_users(path)
    except auth.UsersFileError as e:
        if e.missing and missing_ok:
            return {}
        if e.missing:
            raise Fail(1, "no users file at %s: create the first user with 'migui-passwd add <name>'" % path)
        raise Fail(1, "%s (%s): fix it or move it away; nothing written" % (e.message, path))


def take_lock(path):
    fd = os.open(path + ".lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    end = time.monotonic() + LOCK_WAIT
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EACCES) or time.monotonic() > end:
                os.close(fd)
                raise Fail(1, "another migui-passwd holds %s.lock; nothing written" % path)
            time.sleep(0.1)


def write_users(path, users, gid):
    """Atomic: a new file in the same directory (O_EXCL, 0640, root:migui when gid), fsync, os.replace."""
    d, base = os.path.split(os.path.abspath(path))
    tmp = os.path.join(d, ".%s.tmp-%s" % (base, secrets.token_hex(4)))
    data = auth.dump_users(users)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o640)
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            if gid is not None:
                os.fchown(fd, 0, gid)
            os.fchmod(fd, 0o640)  # explicit: the umask must not decide the mode the web needs
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dfd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def run(o):
    path = o["users_file"]
    default = os.path.abspath(path) == auth.DEFAULT_USERS
    if default and os.geteuid() != 0:
        raise Fail(2, "run as root (sudo migui-passwd ...): %s is root:%s 0640" % (auth.DEFAULT_USERS, GROUP))
    audit_path = o["audit"] or (DEFAULT_AUDIT if default else None)
    audit = Audit(audit_path, "passwd") if audit_path else None
    cmd, name = o["cmd"], o["user"]
    if name is not None:
        try:
            auth.check_name(name)
        except auth.PolicyError as e:
            raise Fail(1, e.message + "; nothing written")
    if cmd == "list":
        users = read_users(path, missing_ok=False)
        if not users:
            print("(no users: nobody can log in; create one with 'migui-passwd add <name>')")
        for n in sorted(users):
            print("%-32s created %s  changed %s" % (n, users[n].get("created", "?"), users[n].get("changed", "?")))
        return 0
    if not os.path.isdir(os.path.dirname(os.path.abspath(path))):
        raise Fail(1, "the directory of %s does not exist (install.sh creates /var/lib/migui)" % path)
    gid = None
    if default:
        try:
            gid = grp.getgrnam(GROUP).gr_gid
        except KeyError:
            raise Fail(1, "group %s does not exist: run migui/deploy/install.sh first" % GROUP)

    def exists_check(users):
        if cmd == "add" and name in users:
            raise Fail(1, "user %s exists (use 'set' to change its password); nothing written" % name)
        if cmd in ("set", "del") and name not in users:
            raise Fail(1, "no user %s; nothing written" % name)

    exists_check(read_users(path, missing_ok=cmd == "add"))   # before asking for a password in vain
    pw = read_password(name, o["stdin"]) if cmd in ("add", "set") else None
    rec = None
    if pw is not None:
        try:
            rec = auth.hash_password(pw)    # outside the lock: about 0.1 s and 32 MiB
        except auth.ScryptError as e:
            raise Fail(1, e.message + "; nothing written")
        pw = None
    lockfd = take_lock(path)
    try:
        users = read_users(path, missing_ok=cmd == "add")  # again, under the lock
        exists_check(users)
        now = auth.utc()
        if cmd == "del":
            del users[name]
        else:
            rec["created"] = users[name]["created"] if cmd == "set" else now
            rec["changed"] = now
            users[name] = rec
        if len(users) > auth.MAX_USERS:
            raise Fail(1, "at most %d users; nothing written" % auth.MAX_USERS)
        try:
            write_users(path, users, gid)
        except OSError as e:
            raise Fail(1, "cannot write %s: %s; the old file is unchanged" % (path, e.strerror or e))
    finally:
        os.close(lockfd)
    action = {"add": "user-add", "set": "user-set", "del": "user-del"}[cmd]
    if audit:
        audit(action, user=name)
    print("%s: user %s" % ({"add": "added", "set": "password changed", "del": "removed"}[cmd], name))
    if cmd == "del" and not users:
        sys.stderr.write("migui-passwd: warning: no users left: nobody can log in\n")
    return 0


def main(argv=None):
    os.umask(0o077)
    try:
        o = parse(sys.argv[1:] if argv is None else argv)
        if o["help"]:
            sys.stdout.write(USAGE)
            return 0
        return run(o)
    except Fail as e:
        sys.stderr.write("migui-passwd: %s\n" % e.message)
        return e.code


if __name__ == "__main__":
    sys.exit(main())
