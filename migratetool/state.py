"""State directory /var/lib/mig-migrate/<arg>/: same layout and file formats as the bash build,
so either build can roll back what the other one started (DESIGN.md, "state directory").

Every write and append is flushed to disk (fsync of the file, and of the directory for a new file): a power loss
right after a destination ID was recorded must not lose it, or rollback cannot find that object (A18). A read that
fails for any reason other than "no such file" stops the run: an unreadable dst-server-id read as "" would make a
rollback skip the server.
"""
import errno
import fcntl
import json
import os
import time

from . import config
from .util import Die


def _fsync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class StateDir:
    def __init__(self, arg, root=None):
        # The name typed becomes a directory name (and mig-snap-<name>): a '/' would nest it or leave STATE_ROOT. The
        # server ID always works.
        if "/" in arg or arg in (".", ".."):
            raise Die("VM name %r cannot name a state directory: run with the server ID" % arg)
        self.arg = arg
        self.root = root or config.STATE_ROOT
        self.path = os.path.join(self.root, arg)
        self.lock_fd = None

    def p(self, name):
        return os.path.join(self.path, name)

    def exists(self, name):
        return os.path.exists(self.p(name))

    def nonempty(self, name):
        """bash [ -s file ]"""
        try:
            return os.path.getsize(self.p(name)) > 0
        except OSError:
            return False

    def create(self):
        os.makedirs(self.path, exist_ok=True)
        os.chmod(self.path, 0o700)

    def lock(self):
        """A7: an exclusive flock on the state directory itself (no lock file: the directory is the object), held
        until this process ends or unlock(). A second migrate or rollback of the same state dies here, whatever
        host lock (MIG_LOCK) each of them took."""
        if self.lock_fd is not None:
            return
        fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            os.close(fd)
            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                raise Die("state %s is in use by another migrate or rollback (it holds the directory's lock)"
                          % self.path)
            raise
        self.lock_fd = fd

    def unlock(self):
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None

    def read(self, name, default=""):
        try:
            with open(self.p(name), encoding="utf-8", errors="replace") as f:
                return f.read()
        except FileNotFoundError:
            return default
        except OSError as e:
            raise Die("cannot read state file %s: %s" % (self.p(name), e.strerror or e))

    def json(self, name):
        with open(self.p(name), encoding="utf-8") as f:
            return json.load(f)

    def _sync(self, f, new):
        f.flush()
        os.fsync(f.fileno())
        if new:
            _fsync_dir(self.path)

    def write(self, name, text):
        new = not os.path.exists(self.p(name))
        with open(self.p(name), "w", encoding="utf-8") as f:
            f.write(text)
            self._sync(f, new)

    def append(self, name, line):
        new = not os.path.exists(self.p(name))
        with open(self.p(name), "a", encoding="utf-8") as f:
            f.write(line + "\n")
            self._sync(f, new)

    def remove(self, name):
        try:
            os.remove(self.p(name))
        except FileNotFoundError:
            pass

    def retire(self):
        """mv <dir> <dir>.rolled-back.<epoch>; the new path is kept in self.retired (rollback.json). Two retirements
        in one second (an auto-clean right before a rollback) get a suffix instead of failing."""
        base = "%s.rolled-back.%d" % (self.path, int(time.time()))
        self.retired, n = base, 0
        while os.path.exists(self.retired):
            n += 1
            self.retired = "%s.%d" % (base, n)
        os.rename(self.path, self.retired)
        _fsync_dir(self.root)

    # --- volume-map: '<source-volume-id> <destination-volume-id>' per line ------------------------
    def volume_map(self):
        pairs = []
        for line in self.read("volume-map").splitlines():
            f = line.split()
            if len(f) >= 2:
                pairs.append((f[0], f[1]))
        return pairs

    def dst_volume_of(self, svid):
        """awk -v s=<svid> '$1==s {print $2}' volume-map"""
        return "\n".join(d for s, d in self.volume_map() if s == svid)
