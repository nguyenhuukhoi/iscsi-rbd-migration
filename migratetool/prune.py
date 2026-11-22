"""'migrate prune --older-than Nd [--dry-run] [--json]' (C9): remove what only history needs.

Removes only:
  - <STATE_ROOT>/<ident>.rolled-back.<epoch>[.n] directories whose epoch is older than N days;
  - <LOG_ROOT>/<YYYYmmdd-HHMMSS>[-<6 hex>] batch log directories whose time is older than N days.
Never a live state directory (volume-map), never a plan-only one, never anything that is a symlink or whose realpath
leaves its root (SEC-4). No cloud is asked. Holds no host lock: nothing a running migrate uses is that old.
"""
import os
import re
import shutil
import time

from . import config
from .jsonout import emit_json, stdout_to_stderr
from .stateview import inside
from .status import BATCH_RE, ROLLED_RE
from .util import Die, out

AGE_RE = re.compile(r"^([1-9][0-9]{0,4})d$")


def _size(path):
    total = 0
    for base, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(base, f)).st_size
            except OSError:
                pass
    return total


def candidates(days, now=None):
    """[(kind, path, epoch)] older than days: kind 'state' or 'batch'."""
    now = time.time() if now is None else now
    cut = now - days * 86400
    got = []
    root = config.STATE_ROOT
    try:
        names = sorted(os.listdir(root))
    except OSError:
        names = []
    for n in names:
        m = ROLLED_RE.match(n)
        path = os.path.join(root, n)
        if not m or not inside(root, path) or not os.path.isdir(path):
            continue
        epoch = int(m.group(2))
        if epoch < cut:
            got.append(("state", path, epoch))
    logs = config.LOG_ROOT
    try:
        names = sorted(os.listdir(logs))
    except OSError:
        names = []
    for n in names:
        path = os.path.join(logs, n)
        if not BATCH_RE.match(n) or not inside(logs, path) or not os.path.isdir(path):
            continue
        try:
            epoch = int(time.mktime(time.strptime(n[:15], "%Y%m%d-%H%M%S")))
        except ValueError:
            continue
        if epoch < cut:
            got.append(("batch", path, epoch))
    return got


def run_prune(args):
    older, dry, as_json = None, False, False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--older-than" and i + 1 < len(args):
            older, i = args[i + 1], i + 2
        elif a.startswith("--older-than="):
            older, i = a.split("=", 1)[1], i + 1
        elif a == "--dry-run":
            dry, i = True, i + 1
        elif a == "--json":
            as_json, i = True, i + 1
        else:
            raise Die("unknown option %s: prune takes --older-than Nd [--dry-run] [--json]" % a)
    m = AGE_RE.match(older or "")
    if not m:
        raise Die("--older-than takes a number of days, at least 1: 30d")
    days = int(m.group(1))
    removed, freed, errors = [], 0, []
    for kind, path, epoch in candidates(days):
        size = _size(path)
        if not dry:
            try:
                shutil.rmtree(path)
            except OSError as e:
                errors.append({"path": path, "error": e.strerror or str(e)})
                continue
        removed.append({"kind": kind, "path": path, "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch)),
                        "bytes": size})
        freed += size
    doc = {"older_than_days": days, "dry_run": dry, "removed": removed, "bytes": freed, "errors": errors,
           "state_root": config.STATE_ROOT, "log_root": config.LOG_ROOT}
    if as_json:
        with stdout_to_stderr() as real:
            emit_json(doc, stream=real)
    else:
        verb = "would remove" if dry else "removed"
        for x in removed:
            out("%s %-5s %s (%s, %d bytes)" % (verb, x["kind"], x["path"], x["time"], x["bytes"]))
        for x in errors:
            out("ERROR %s: %s" % (x["path"], x["error"]))
        out("%s %d director%s older than %dd, %d bytes%s" % (verb, len(removed), "y" if len(removed) == 1 else "ies",
                                                             days, freed, " (dry run)" if dry else ""))
    return 1 if errors else 0
