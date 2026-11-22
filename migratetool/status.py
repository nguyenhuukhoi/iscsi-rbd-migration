"""'migrate status' (7.3, DESIGN.md section 7): what this host holds, without asking any cloud.

    migrate status [--all] [--json]

Reads only STATE_ROOT (/var/lib/mig-migrate), LOG_ROOT (/var/log/mig-batch) and /proc/<pid>/cmdline. No openstack
command (the openrcs are only sourced to know which values SEC-1 must keep out of the output). Opens files for
reading only, takes no lock, creates nothing (SEC-10, SEC-11). An entry that is a symlink, or whose realpath leaves
its root, is skipped and counted in "skipped" (SEC-4). A state directory that cannot be read gets an "error" and
the rest goes on.
"""
import os
import re

from . import __version__, config
from .jsonout import emit_json, read_file, read_json, stdout_to_stderr, utc
from .stateview import inside, phase_of, source_power
from .util import Die, out

ROLLED_RE = re.compile(r"^(.+)\.rolled-back\.(\d+)(?:\.\d+)?$")   # '.N': a second retirement in one second
BATCH_RE = re.compile(r"^\d{8}-\d{6}(?:-[0-9a-f]{6})?$")             # C12: '<time>-<6 hex>' since 1.0.0
DEFAULT_LIMIT = 50
ALL_LIMIT = 2000      # SEC-16
PROC = "/proc"        # tests point it at a fake tree
CMDLINE_MAX = 65536


def _json_file(path, name, entry, key):
    """entry[key] = the JSON of path/name (None when absent); a file that cannot be read sets entry["error"]."""
    try:
        entry[key] = read_json(os.path.join(path, name))
    except (OSError, ValueError) as e:
        entry[key] = None
        entry["error"] = "%s: %s" % (name, getattr(e, "strerror", None) or e)


def _text(path, name):
    try:
        return read_file(os.path.join(path, name)).strip()
    except FileNotFoundError:
        return None


def state_entry(name, path):
    e = {"ident": name, "dir": path}
    try:
        files = sorted(os.listdir(path))
        e["kind"] = "vol" if "volume.json" in files else "vm"
        e["files"] = files
        e["live"] = "volume-map" in files
        e["mtime"] = utc(os.stat(path).st_mtime)
        _json_file(path, "options.json", e, "options")
        t = _text(path, "t-stop")
        e["t_stop"] = utc(int(t)) if t and t.isdigit() else None
        e["dst_server_id"] = _text(path, "dst-server-id") or None
        vm = _text(path, "volume-map")
        e["volume_map"] = [l.split()[:2] for l in (vm or "").splitlines() if len(l.split()) >= 2]
        # D12: where a live VM state stands, and the source's power when it was exported (what rollback restores)
        e["phase"] = phase_of(e["kind"], e["live"], t, e["dst_server_id"])
        e["source_power"] = source_power(path) if e["kind"] == "vm" else None
    except (OSError, ValueError) as err:
        e["error"] = getattr(err, "strerror", None) or str(err)
    return e


def processes():
    """Running migrate processes: /proc/<pid>/cmdline naming the migratetool package (python -m migratetool...) or
    the 'migrate' entry point. Nothing else of any process is read (no environ), nothing of other processes is
    returned (SEC-4). No /proc: []."""
    found = []
    try:
        pids = os.listdir(PROC)
    except OSError:
        return found
    me = (os.getpid(), os.getppid())  # this status and what started it (a shell, sudo)
    for pid in pids:
        if not pid.isdigit() or int(pid) in me:
            continue
        try:
            with open(os.path.join(PROC, pid, "cmdline"), "rb") as f:
                raw = f.read(CMDLINE_MAX)
        except OSError:
            continue
        argv = [x.decode("utf-8", "replace") for x in raw.split(b"\0") if x]
        mod = any(a == "-m" and i + 1 < len(argv) and argv[i + 1].split(".")[0] == "migratetool"
                  for i, a in enumerate(argv))
        # the entry point run directly, or by its python (shebang); not 'sudo migrate', 'vim migrate'
        entry = bool(argv) and (os.path.basename(argv[0]) == "migrate" or (
            len(argv) > 1 and os.path.basename(argv[0]).startswith("python") and os.path.basename(argv[1]) == "migrate"))
        if mod or entry:
            found.append({"pid": int(pid), "cmdline": argv})
    return sorted(found, key=lambda x: x["pid"])


def collect(all_):
    limit = ALL_LIMIT if all_ else DEFAULT_LIMIT
    root, logs = config.STATE_ROOT, config.LOG_ROOT
    res = {"tool_version": __version__, "state_root": root, "log_root": logs, "live": [], "plan_only": [],
           "rolled_back": [], "batches": [], "running": processes(), "skipped": 0}
    try:
        names = sorted(os.listdir(root))
    except FileNotFoundError:
        names = []
    except OSError as e:
        names, res["error"] = [], "%s: %s" % (root, e.strerror)
    rolled = []
    for n in names:
        path = os.path.join(root, n)
        if not inside(root, path):
            res["skipped"] += 1
            continue
        if not os.path.isdir(path):
            continue
        m = ROLLED_RE.match(n)
        if m:
            rolled.append((int(m.group(2)), n, m.group(1), path))
            continue
        e = state_entry(n, path)
        res["live" if e.get("live") else "plan_only"].append(e)
    rolled.sort(key=lambda x: (-x[0], x[1]))
    res["rolled_back_total"] = len(rolled)
    for epoch, n, ident, path in rolled[:limit]:
        e = {"ident": ident, "dir": path, "retired": utc(epoch)}
        _json_file(path, "options.json", e, "options")
        _json_file(path, "rollback.json", e, "rollback")
        res["rolled_back"].append(e)
    try:
        batches = sorted((n for n in os.listdir(logs) if BATCH_RE.match(n)), reverse=True)
    except OSError:
        batches = []
    res["batches_total"] = len(batches)
    for n in batches[:limit]:
        path = os.path.join(logs, n)
        if not inside(logs, path) or not os.path.isdir(path):
            res["skipped"] += 1
            continue
        e = {"dir": path}
        try:
            e["logs"] = sorted(os.listdir(path))
            e["mtime"] = utc(os.stat(path).st_mtime)
        except OSError as err:
            e["error"] = err.strerror
        res["batches"].append(e)
    return res


def run_status(args):
    flags = set(args)
    bad = flags - {"--json", "--all"}
    if bad:
        raise Die("unknown option %s" % sorted(bad)[0])
    if "--json" in flags:
        with stdout_to_stderr() as real:
            emit_json(collect("--all" in flags), stream=real)
        return 0
    st = collect("--all" in flags)
    for label, key in (("live", "live"), ("plan", "plan_only")):
        for e in st[key]:
            out("%-5s %-40s %s%s" % (label, e["ident"], "t-stop " + e["t_stop"] if e.get("t_stop") else "",
                                     ("  ERROR " + e["error"]) if e.get("error") else ""))
    out("rolled back: %d (newest %s)" % (st["rolled_back_total"],
                                         st["rolled_back"][0]["retired"] if st["rolled_back"] else "-"))
    out("batches: %d (newest %s)" % (st["batches_total"], st["batches"][0]["dir"] if st["batches"] else "-"))
    for p in st["running"]:
        out("running pid %d: %s" % (p["pid"], " ".join(p["cmdline"])))
    return 0
