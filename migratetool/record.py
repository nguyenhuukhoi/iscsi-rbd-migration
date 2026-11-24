"""7.3 (DESIGN.md sections 4 and 7): what a run records about itself next to the bash-format state files.

options.json (migrate, volume): the command line, the effective options with their origin, the conf whitelist.
rollback.json (rollback): written into the retired '.rolled-back.<epoch>' directory, never over options.json.
The old one-line 'options' file stays exactly as before (rollback and grep read it).
"""
import os

from . import __version__
from .jsonout import check_argv, utc, write_private_json

# SEC-2: the only configuration keys a JSON answer carries (never SRC_RC/DST_RC, CEPH_*, OS_*).
CONF_KEYS = ("MIG_DEFAULT_OPTS", "MIG_AZ", "MIG_VOLUME_AZ", "AZ_TYPE_MAP", "VOLUME_TYPE_MAP", "RBD_POOL_MAP",
             "RBD_POOL", "PROJECT_MAP_FILE", "FLAVOR_MAP_FILE", "SRC_PROJECT", "DST_PROJECT")


def conf_view(cfg):
    d = {"path": cfg.conf_path, "found": os.path.isfile(cfg.conf_path) and os.access(cfg.conf_path, os.R_OK)}
    for k in CONF_KEYS:
        d[k] = cfg.values.get(k, "")
    return d


def vm_command(verb, ident, args):
    """The command a person types to run this one VM again: a child of a confirmed selector run is
    'migrate migrate <ident> <options> --yes' (the confirmation already happened)."""
    cmd = ["migrate", verb, ident, *args]
    if verb == "migrate":
        cmd.append("--yes")
    return cmd


def write_options_json(r, st, kind, command, extra):
    """options.json of a migrate (kind vm) or volume (kind vol) run, right after the 'options' line."""
    check_argv(command)
    # The state check of the caller passed (no volume-map): an options.json here is from a run that stopped before
    # its cutover state existed (a security group copy that failed, plan has none). Replaced, not appended to.
    st.remove("options.json")
    doc = {"command": command, "effective": r.opts.effective(r, st, kind), "conf": conf_view(r.cfg),
           "tool_version": __version__, "started": getattr(r, "started", "") or utc(), "ident": st.arg, "kind": kind}
    doc.update(extra)
    write_private_json(st.p("options.json"), doc, r.cfg)


def write_rollback_json(cfg, retired_dir, target, started):
    """rollback.json in the directory the rollback just retired (only after a rollback that finished)."""
    command = ["migrate", "rollback", target]
    check_argv(command)
    write_private_json(os.path.join(retired_dir, "rollback.json"),
                       {"command": command, "started": started, "ended": utc(), "rc": 0, "tool_version": __version__,
                        "ident": os.path.basename(retired_dir).split(".rolled-back.", 1)[0]}, cfg)
