"""Read-only views of STATE_ROOT and LOG_ROOT for the JSON commands (7.3, DESIGN.md sections 4 and 7).

Nothing here creates, locks or writes anything (SEC-10, SEC-11). A path handed back is inside its root after
realpath, a symlink is never followed (SEC-4): such an entry is skipped and counted instead.
"""
import os

from . import config
from .jsonout import read_json

BAD_NAMES = (".", "..")


def inside(root, path):
    """path is a real directory entry below root: not a symlink, realpath under realpath(root)."""
    if os.path.islink(path):
        return False
    real_root = os.path.realpath(root)
    return os.path.realpath(path).startswith(real_root + os.sep)


def options_of(path):
    """options.json of a state directory: the object, None when absent (state older than 7.3) or unreadable."""
    try:
        got = read_json(os.path.join(path, "options.json"))
    except (OSError, ValueError):
        return None
    return got if isinstance(got, dict) else None


def phase_of(kind, live, t_stop, dst_server_id):
    """D12: preparing (volume-map, no t-stop: the source never stopped), cutover (t-stop, no destination server ID
    yet), migrated (dst-server-id recorded). None for a volume state or a directory that is not live."""
    if kind != "vm" or not live:
        return None
    if dst_server_id:
        return "migrated"
    return "cutover" if t_stop else "preparing"


def source_power(path):
    """D12 / A14: the source server's status in server.json (written by the export, before anything stopped it):
    ACTIVE, SHUTOFF, ... None when there is none or it cannot be read."""
    try:
        got = read_json(os.path.join(path, "server.json"))
    except (OSError, ValueError):
        return None
    status = got.get("status") if isinstance(got, dict) else None
    return status if isinstance(status, str) else None


def state_of(names, root=None):
    """The state directory of the first name that has one ({dir, live, started, options}); None otherwise.
    A VM name that cannot name a directory ('/' in it, '.', '..') has none."""
    root = root or config.STATE_ROOT
    for name in names:
        if not name or "/" in name or name in BAD_NAMES or "\0" in name:
            continue
        path = os.path.join(root, name)
        if not os.path.isdir(path) or not inside(root, path):
            continue
        opts = options_of(path)
        live = os.path.exists(os.path.join(path, "volume-map"))
        kind = "vol" if os.path.exists(os.path.join(path, "volume.json")) else "vm"
        return {"dir": path, "live": live, "started": opts.get("started") if opts else None, "options": opts,
                "phase": phase_of(kind, live, _small(path, "t-stop"), _small(path, "dst-server-id")),
                "source_power": source_power(path) if kind == "vm" else None}
    return None


def _small(path, name):
    """The stripped text of a small state file; '' when absent or unreadable."""
    from .jsonout import read_file
    try:
        return read_file(os.path.join(path, name), 4096).strip()
    except (OSError, ValueError):
        return ""
