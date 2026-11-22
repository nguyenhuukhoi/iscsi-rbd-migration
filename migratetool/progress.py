"""--progress-json (7.3, DESIGN.md section 7): '@@ {"event": ...}' lines on stdout, next to the usual log.

Off unless 'migrate migrate|volume --progress-json' turned it on: then nothing at all is printed, byte for byte the
7.2 output. Events: phase (presync, cutover, copy, verify, create, done), copy / verify (pct, mbps: the /sys/block
watcher, then 100 at the end of each volume), blocksync (pass, pct, changed_mib: blocksync's own stderr line),
pass, t_stop, downtime, result. copy, verify and blocksync are limited to one line per second each (SEC-17); the
closing 100% of a volume is always printed. Every line goes through jsonout.emit_json (SEC-1); a line it refuses is
dropped with a warning instead of stopping a migration in its cutover.
"""
import threading
import time

from . import jsonout

ENABLED = False
LIMITED = ("copy", "verify", "blocksync")
MIN_INTERVAL = 1.0
clock = time.monotonic  # tests replace it
_last = {}
_lock = threading.Lock()  # the copy watcher emits from its own thread


def enable():
    global ENABLED
    ENABLED = True
    _last.clear()


def reset():
    global ENABLED
    ENABLED = False
    _last.clear()


def emit(event, force=False, **fields):
    """Print one event (fields as given; 'pass' needs **{"pass": n}). False when off, limited or refused."""
    if not ENABLED:
        return False
    with _lock:
        if event in LIMITED and not force:
            now = clock()
            if event in _last and now - _last[event] < MIN_INTERVAL:
                return False
            _last[event] = now
        obj = {"event": event}
        obj.update(fields)
        return jsonout.emit_json(obj, prefix="@@ ", on_leak="drop")


def phase(name):
    return emit("phase", name=name)
