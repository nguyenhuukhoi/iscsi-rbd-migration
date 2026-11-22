"""Machine-readable output (7.3, DESIGN.md section 7).

emit_json() is the only door for JSON on stdout: it walks the object and refuses it (Die, nothing printed) when a
key looks like a credential or a value is one of the openrcs' secrets (SEC-1). write_private_json() is the only
way a JSON record is written into a state directory: 0600, never over an existing file (SEC-15), same check.
"""
import json
import os
import re
import sys
import time
from contextlib import contextmanager, redirect_stdout

from .util import Die

# SEC-1: keys that must never reach stdout (any depth), and the variables whose values must not either.
SECRET_KEY = re.compile(r"(?i)(OS_|_RC$|PASSWORD|SECRET|TOKEN|AUTH_URL|CACERT|KEY$|^AQ)")
SECRET_VARS = ("OS_PASSWORD", "OS_APPLICATION_CREDENTIAL_SECRET", "OS_TOKEN")
# A secret this long is also refused inside a longer string (a command line, a message).
SUBSTRING_MIN = 8
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
READ_LIMIT = 1 << 20  # a state or log file read for a JSON answer: at most 1 MiB
# Set by single.py for a plan child of 'migrate plan --json': print the '@@ {"effective": ...}' line, read-only clouds.
EFFECTIVE_LINE = False


def dumps(obj):
    """11.6: one compact line, keys sorted (stable diffs)."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def utc(epoch=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if epoch is None else epoch))


def secret_values(cfg=None):
    """The values of OS_PASSWORD / OS_APPLICATION_CREDENTIAL_SECRET / OS_TOKEN in this process's environment and
    in both openrcs (sourced once per file and cached by clouds.rc_env; an unreadable openrc adds nothing)."""
    from . import config
    from .clouds import base_env, rc_env
    vals = {os.environ[v] for v in SECRET_VARS if os.environ.get(v)}
    if cfg is None:
        try:
            cfg = config.load()
        except Die:
            return vals
    env = base_env(cfg)
    for rc in (cfg.SRC_RC, cfg.DST_RC):
        try:
            e = rc_env(rc, env)
        except Die:
            continue
        vals.update(e[v] for v in SECRET_VARS if e.get(v))
    return vals


def find_leak(obj, secrets, path="$"):
    """Where obj carries a credential-like key or a secret value; None when it is clean. Never names the value."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str) or SECRET_KEY.search(k):
                return "key %r at %s looks like a credential" % (k, path)
            why = find_leak(v, secrets, "%s.%s" % (path, k))
            if why:
                return why
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            why = find_leak(v, secrets, "%s[%d]" % (path, i))
            if why:
                return why
    elif isinstance(obj, str) and secrets:
        if obj in secrets or any(len(s) >= SUBSTRING_MIN and s in obj for s in secrets):
            return "the value at %s is a credential of an openrc" % path
    return None


def emit_json(obj, stream=None, prefix="", cfg=None, on_leak="die"):
    """Print obj as one JSON line (after prefix, e.g. '@@ '). A leak: on_leak='die' raises Die before anything is
    printed; 'drop' (progress events in the middle of a cutover) prints a warning on stderr instead and returns
    False, because stopping a migration over a progress line would do more harm than the line."""
    why = find_leak(obj, secret_values(cfg))
    if why:
        if on_leak == "die":
            raise Die("refusing to print JSON: " + why)
        sys.stderr.write("WARN JSON line not printed: %s\n" % why)
        sys.stderr.flush()
        return False
    s = stream or sys.stdout
    s.write(prefix + dumps(obj) + "\n")
    s.flush()
    return True


@contextmanager
def stdout_to_stderr():
    """2.1: with --json every text line goes to stderr; yields the real stdout for the one JSON object."""
    real = sys.stdout
    with redirect_stdout(sys.stderr):
        yield real


def check_argv(argv):
    """SEC-3: a recorded command line holds no newline or other control character."""
    for a in argv:
        if CONTROL.search(a):
            raise Die("a command-line argument holds a control character, refusing to record it: %r" % a)


def write_private_json(path, obj, cfg=None):
    """SEC-15: a new file, mode 0600, never over an existing one (and never through a symlink)."""
    why = find_leak(obj, secret_values(cfg))
    if why:
        raise Die("refusing to write %s: %s" % (path, why))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        raise Die("%s exists: not overwritten" % path)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=1) + "\n")


def read_file(path, limit=READ_LIMIT):
    """A regular file's text, read without following a symlink and without writing anything (SEC-4, SEC-11).
    Raises OSError / ValueError; the caller turns that into an "error" entry."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        st = os.fstat(fd)
        import stat
        if not stat.S_ISREG(st.st_mode):
            raise ValueError("not a regular file")
        if st.st_size > limit:
            raise ValueError("larger than %d bytes" % limit)
        data = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    return data.decode("utf-8", errors="replace")


def read_json(path, limit=READ_LIMIT):
    """read_file + json.loads; None when the file does not exist."""
    try:
        text = read_file(path, limit)
    except FileNotFoundError:
        return None
    return json.loads(text)
