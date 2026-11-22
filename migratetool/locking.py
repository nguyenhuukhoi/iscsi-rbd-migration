"""A7: one migrate, volume or rollback at a time on this host.

host(cfg) takes an exclusive flock on MIG_LOCK (default /run/mig/migrate.lock) for 'migrate migrate', 'migrate
volume' and 'migrate rollback' ('plan' and the read-only commands never lock). A process that cannot get it dies
before it lists or changes anything. The lock is re-entrant inside one process (a selector run and its in-process
volume) and is handed to child processes: the parent passes its descriptor (pass_fds) and its number in MIG_LOCK_FD,
and the child accepts it only when it is the very file MIG_LOCK names (same device and inode). migui-runner holds the
same file for a whole batch and hands it to each migrate it starts the same way, so a CLI run and a UI run exclude
each other. The state directory of a run has its own lock on top (StateDir.lock).
"""
import errno
import fcntl
import os

from .util import Die

ENV_FD = "MIG_LOCK_FD"
_held = {"fd": None, "path": None, "depth": 0}


def _same_file(fd, path):
    try:
        a, b = os.fstat(fd), os.stat(path)
    except OSError:
        return False
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _holder(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(64).strip()
    except OSError:
        return ""


def host(cfg):
    """Take (or re-enter) the host lock; returns its descriptor. Die when another process holds it."""
    path = cfg.MIG_LOCK
    if _held["fd"] is not None:
        if _held["path"] != path:
            raise Die("MIG_LOCK changed inside one run (%s, then %s)" % (_held["path"], path))
        _held["depth"] += 1
        return _held["fd"]
    inherited = os.environ.get(ENV_FD, "")
    if inherited:
        if not inherited.isdigit() or not _same_file(int(inherited), path):
            raise Die("%s=%s is not the lock file MIG_LOCK=%s: the program that started migrate (migui-runner "
                      "--lock, or a selector run) must use the same MIG_LOCK" % (ENV_FD, inherited, path))
        fd = int(inherited)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # the parent's own lock: same open file, so it succeeds
        except OSError:
            raise Die("the lock %s handed down in %s is not held by the parent" % (path, ENV_FD))
        own = False
    else:
        d = os.path.dirname(path)
        if d and not os.path.isdir(d):
            os.makedirs(d, mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            os.close(fd)
            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                who = _holder(path)
                raise Die("another migrate, volume or rollback runs on this host (lock %s%s): one at a time; wait for "
                          "it ('migrate status' lists it)" % (path, (", held by " + who) if who else ""))
            raise
        own = True
        try:  # who holds it, for the message above (best effort)
            os.ftruncate(fd, 0)
            os.write(fd, ("pid %d\n" % os.getpid()).encode())
        except OSError:
            pass
    _held.update(fd=fd, path=path, depth=1, own=own)
    return fd


def release():
    """Leave the lock once; the last release of a lock this process opened closes it (tests, in-process runs)."""
    if _held["fd"] is None:
        return
    _held["depth"] -= 1
    if _held["depth"] > 0:
        return
    if _held.get("own"):
        try:
            os.close(_held["fd"])
        except OSError:
            pass
    _held.update(fd=None, path=None, depth=0, own=False)


def child_env(env):
    """env (a dict) for a child process that runs under this process's lock, and the fds to pass."""
    if _held["fd"] is None:
        return env, ()
    env = dict(env)
    env[ENV_FD] = str(_held["fd"])
    return env, (_held["fd"],)


class held:
    """with locking.held(cfg): ... (host() then release())."""

    def __init__(self, cfg):
        self.cfg = cfg

    def __enter__(self):
        return host(self.cfg)

    def __exit__(self, *exc):
        release()
        return False
