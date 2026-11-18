"""The 'migrate' command.

argparse describes the commands and produces --help. The options of plan/migrate are parsed by
options.Options, a line-by-line port of the bash parser: MIG_DEFAULT_OPTS first, then the command
line, with the same origin tracking, conflicts and messages (argparse cannot merge two sources).
"""
import argparse
import os
import sys

from . import __version__, config
from .context import Run
from .options import Options
from .preflight import load_flavor_map
from .rollback import rollback_vm
from .state import StateDir
from .util import Die, Exit
from .vmcmd import NOT_PORTED, cmd_migrate, cmd_plan

VM_OPTIONS = (
    ("--mode", "cold|live", "cold (default): stop the VM, qemu-img copy + full compare (unless --verify none). "
                            "live: not ported yet (phase 2)."),
    ("--volume-type", "TYPE", "Cinder volume type for every destination volume (default: cloud default, or the "
                              "AZ's entry in AZ_TYPE_MAP). RBD_POOL_MAP maps types to RBD pools."),
    ("--az", "ZONE", "destination availability zone for BOTH the volumes and the server (default: MIG_AZ, else "
                     "the destination schedulers pick)."),
    ("--flavor", "NAME", "flavor for the destination VM (default: same name as the source)."),
    ("--flavor-fallback", None, "destination lacks the source's flavor name: use the flavor FLAVOR_MAP_FILE maps it "
                                "to. Never creates a flavor; --flavor still wins."),
    ("--security-group", "SGS", "comma-separated security groups for every destination port (default: the "
                                "source ports' group names)."),
    ("--ip", "same|new|ADDR", "same (default): keep MAC+IP, detach source ports at cutover. new: new MAC+IP, "
                              "source ports stay attached. ADDR: that address, new MAC (single-NIC VMs)."),
    ("--start-source", None, "after a successful migrate, unlock and start the source VM again (with --ip new|ADDR, "
                             "or with --volumes-only)."),
    ("--keep-source-ports", None, "with --ip same: do NOT detach the source ports. UNSAFE: only SHUTOFF+lock keeps "
                                  "the old VM off the wire."),
    ("--server-group", "NAME", "existing destination server group the new VM joins (--hint group=<id>)."),
    ("--keep-server-group", None, "the new VM joins az-<source group name>, created when missing (by migrate, "
                                  "never by plan). Not with --server-group."),
    ("--volumes-only", None, "cold only: copy the volumes, create no destination port or server."),
    ("--max-passes", "N", "live: maximum pre-sync passes (default 5)."),
    ("--delta-limit", "MB", "live: cut over once a pass changed at most this much (default 64)."),
    ("--verify", "readback|full|none", "cold: full qemu-img compare unless none."),
)
EPILOG = """Configuration: /opt/mig/migrate.conf (or $MIG_CONF), bash KEY=VALUE, template migrate.conf.example.
Precedence: option > environment > migrate.conf > default. MIG_DEFAULT_OPTS="--opt ..." is applied
before the command-line options, so the command line wins. State: /var/lib/mig-migrate/<vm>/."""


def build_parser():
    p = argparse.ArgumentParser(
        prog="migrate", formatter_class=argparse.RawDescriptionHelpFormatter, epilog=EPILOG,
        description="Migrate VMs between two independent OpenStack clouds: iSCSI Cinder -> Ceph RBD.")
    p.add_argument("--version", action="version", version="migrate " + __version__)
    sub = p.add_subparsers(dest="command", metavar="COMMAND")
    for name, text in (("plan", "read-only: export the source definition, run the destination preflight"),
                       ("migrate", "migrate one VM (cold stops it first)")):
        sp = sub.add_parser(name, help=text, description=text, epilog=EPILOG,
                            formatter_class=argparse.RawDescriptionHelpFormatter)
        sp.add_argument("vm", help="VM name or ID")
        for opt, metavar, h in VM_OPTIONS:
            if metavar:
                sp.add_argument(opt, metavar=metavar, help=h)
            else:
                sp.add_argument(opt, action="store_true", help=h)
    rb = sub.add_parser("rollback", help="delete the destination objects, restore and start the source VM",
                        description="delete the destination objects, restore and start the source VM")
    rb.add_argument("vm", help="the name or ID the migrate ran with")
    sub.add_parser("volume", help="copy one volume (" + NOT_PORTED + ")")
    sub.add_parser("batch", help="many VMs (" + NOT_PORTED + ")")
    return p


def _help(argv):
    try:
        build_parser().parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 0
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    try:
        if not argv:
            build_parser().print_help()
            return 2
        if argv[0] in ("-h", "--help", "--version"):
            return _help(argv)
        cmd, rest = argv[0], argv[1:]
        if cmd in ("plan", "migrate"):
            if not rest or rest[0].startswith("-") and rest[0] in ("-h", "--help"):
                code = _help([cmd, "--help"])
                return 2 if not rest else code
            if "-h" in rest[1:] or "--help" in rest[1:]:
                return _help([cmd, "--help"])
            return run_vm(cmd, rest[0], rest[1:])
        if cmd == "rollback":
            if not rest or rest[0] in ("-h", "--help"):
                code = _help([cmd, "--help"])
                return 2 if not rest else code
            if len(rest) > 1:
                raise Die("rollback takes no options")
            return run_rollback(rest[0])
        if cmd in ("volume", "migrate-volume", "batch"):
            raise Die("'%s': %s" % (cmd, NOT_PORTED))
        raise Die("unknown command " + cmd)
    except Die as e:
        print("ERROR: %s" % e, file=sys.stderr, flush=True)
        return 1
    except Exit as e:
        return e.code


def run_vm(cmd, vm, args):
    cfg = config.load()
    o = Options()
    o.load_default_opts(cmd, cfg.MIG_DEFAULT_OPTS)
    o.parse_options(args)
    if o.in_use:
        raise Die("--in-use belongs to migrate-volume: plan and migrate move a VM with its volumes (--mode live keeps "
                  "it running until the cutover)")
    if o.mode == "live":
        raise Die("--mode live: " + NOT_PORTED)
    r = Run(cfg, o)
    if o.flavor_fallback and not o.volumes_only:
        load_flavor_map(r)
    o.apply_az_defaults(cfg.MIG_AZ, cfg.AZ_TYPE_MAP)
    r.pool = o.pool_for_type(cfg.RBD_POOL, cfg.RBD_POOL_MAP)
    r.check_project_scope()
    st = StateDir(vm)
    if cmd == "plan":
        cmd_plan(r, st)
    else:
        cmd_migrate(r, st)
    return 0


def run_rollback(target):
    cfg = config.load()
    r = Run(cfg, Options())
    r.check_project_scope()
    st = StateDir(target)
    # A VM state under that name wins, exactly as before; anything else is taken as a volume.
    if os.path.exists(st.p("server.json")):
        rollback_vm(r, st)
        return 0
    raise Die("rollback of a migrate-volume copy (%s): %s" % (target, NOT_PORTED))
