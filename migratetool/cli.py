"""The 'migrate' command.

argparse describes the commands and produces --help. The command line itself is split by
selector.split_args (selectors and run control vs. the options of each VM) and the per-VM options
are parsed by options.Options, a line-by-line port of the bash parser: MIG_DEFAULT_OPTS first, then
the command line, with the same origin tracking, conflicts and messages.

plan/migrate/volume over a selection run every VM (or volume) as a child process
(migratetool.single), each with its own project scope and log file, like migrate-batch.sh did.
'plan volume <selection>' is the volume selection stopped after its plan stage (the batch's
'--volumes ... --plan-only').
"""
import argparse
import os
import sys

from . import __version__, config, selector
from .context import Run
from .livesync import check_numbers
from .options import Options
from .preflight import load_flavor_map
from .rollback import rollback_vm
from .state import StateDir
from .util import Die, Exit, prog
from .vmcmd import cmd_migrate, cmd_plan
from .volcmd import cmd_migrate_volume, cmd_rollback_volume, volume_options_only

VM_OPTIONS = (
    ("--mode", "cold|live", "cold (default): stop the VM, qemu-img copy + full compare (unless --verify none). "
                            "live: hash-cached pre-sync passes while the VM runs; the cutover only syncs the "
                            "final delta (blocksync, run by SYS_PY)."),
    ("--volume-type", "TYPE", "Cinder volume type for every destination volume (default: cloud default, or the "
                              "AZ's entry in AZ_TYPE_MAP). RBD_POOL_MAP maps types to RBD pools."),
    ("--az", "ZONE", "destination availability zone for BOTH the volumes and the server (default: MIG_AZ, else "
                     "the destination schedulers pick). Also the <AZ> of the copied group names (without --az and "
                     "MIG_AZ: the destination's only compute AZ)."),
    ("--flavor", "NAME", "flavor for the destination VM (default: same name as the source)."),
    ("--flavor-fallback", None, "destination lacks the source's flavor name: use the flavor FLAVOR_MAP_FILE maps it "
                                "to. Never creates a flavor; --flavor still wins."),
    ("--security-group", "SGS", "comma-separated destination security groups for every destination port, names "
                                "taken as typed (default: each source group copied as <AZ>-<name>)."),
    ("--no-copy-secgroup", None, "do not copy the source security groups: each port takes the destination groups "
                                 "of the same names, which must exist (the behaviour up to 7.0)."),
    ("--ip", "same|new|ADDR", "same (default): keep MAC+IP, detach source ports at cutover. new: new MAC+IP, "
                              "source ports stay attached. ADDR: that address, new MAC (single-NIC VMs)."),
    ("--start-source", None, "after a successful migrate, unlock and start the source VM again (with --ip new|ADDR, "
                             "or with --volumes-only)."),
    ("--keep-source-ports", None, "with --ip same: do NOT detach the source ports. UNSAFE: only SHUTOFF+lock keeps "
                                  "the old VM off the wire."),
    ("--server-group", "NAME", "existing destination server group the new VM joins (--hint group=<id>), instead "
                               "of the copy of the source group."),
    ("--no-server-group", None, "do not follow the source server group (by default the new VM joins <AZ>-<source "
                                "group name>, same policy, created by migrate when missing)."),
    ("--keep-server-group", None, "accepted for old scripts, changes nothing: following the source server group is "
                                  "the default now. Not with --server-group or --no-server-group."),
    ("--volumes-only", None, "cold only: copy the volumes, create no destination port or server."),
    ("--max-passes", "N", "live: maximum pre-sync passes (default 5)."),
    ("--delta-limit", "MB", "live: cut over once a pass changed at most this much (default 64)."),
    ("--verify", "readback|full|none", "cold: full qemu-img compare unless none. live: readback (default) re-reads "
                                       "every written chunk, full adds a cutover compare, none trusts the writes."),
)
SELECTOR_HELP = (
    ("--vm", "VMS", "VM names or IDs, comma-separated (<vm> alone is the same as --vm <vm>)."),
    ("--host", "HOSTS", "source compute hosts (Host column of 'openstack compute service list')."),
    ("--project", "PROJECTS", "source projects, name or ID."),
    ("--vm-file", "FILE", "one VM name or ID per line (# comments and blank lines skipped). Selectors intersect."),
)
EPILOG = """Every VM runs as its own child process in its own project: SRC_PROJECT/DST_PROJECT = the VM's
project (renamed by PROJECT_MAP_FILE "source destination" lines). Lists with --all-projects (admin).
Logs: /var/log/mig-batch/<time>/ (plan-<id>.log, migrate-<id>.log). State: /var/lib/mig-migrate/<vm>/.
Configuration: /opt/mig/migrate.conf (or $MIG_CONF), bash KEY=VALUE, template migrate.conf.example.
Precedence: option > environment > migrate.conf > default. MIG_DEFAULT_OPTS="--opt ..." is applied
before the command-line options, so the command line wins."""
PLAN_NOTE = """Volumes without their VM: %(prog)s volume --project P --available|--attached|--all, or
%(prog)s volume --vm a,b = the dry-run of 'migrate volume' (%(prog)s volume --help).

"""
VOLUME_EPILOG = """
  %(prog)s <volume> [--in-use]   one volume (name or ID) of the source project, no prompt
  %(prog)s --project P --available    every available volume of P (keeps its name)
  %(prog)s --project P --attached     the volumes of P's VMs (= migrate --volumes-only)
  %(prog)s --project P --all          both: attached first, then available
  %(prog)s --vm a,b                   the volumes of those VMs
A selection asks 'Type yes' unless --yes. State: /var/lib/mig-migrate/vol-<id>/.
Dry-run of a selection: migrate plan volume <the same selection>."""
PLAN_VOLUME_EPILOG = """
  %(prog)s --project P --available    the available volumes of P that 'migrate volume' would copy
  %(prog)s --project P --attached     the VMs of P: each one's preflight with --volumes-only
  %(prog)s --project P --all          both: attached first, then available
  %(prog)s --vm a,b                   those VMs: each one's preflight with --volumes-only
Changes nothing and never asks. An available volume has no plan of its own: it shows READY
('migrate volume' checks it right before copying it), as the batch's --plan-only did. Exit 1 when a
plan fails or every selected row is a SKIP. A single volume has no dry-run: 'migrate volume <volume>'
runs its read-only checks first and stops on a FAIL before creating anything."""


def add_volume_args(vp, plan):
    """The selection and options of 'volume'; 'plan volume' takes the same minus what only a run uses."""
    g = vp.add_argument_group("selection")
    for opt, metavar, h in SELECTOR_HELP:
        g.add_argument(opt, metavar=metavar, help=h)
    g.add_argument("--available", action="store_true", help="with --project: its available volumes.")
    g.add_argument("--attached", action="store_true", help="the volumes of the selected VMs (default with --vm).")
    g.add_argument("--all", action="store_true", help="with --project: attached first, then available.")
    if not plan:
        g.add_argument("--yes", action="store_true",
                       help="no confirmation prompt (required when stdin is not a terminal).")
        g.add_argument("--continue-on-error", action="store_true", help="go on after a failure.")
    g = vp.add_argument_group("options")
    if not plan:
        g.add_argument("--in-use", action="store_true",
                       help="one volume only: copy an attached volume from a clone taken while attached "
                            "(crash-consistent).")
    for opt, metavar, h in VM_OPTIONS:
        if opt in ("--volume-type", "--az", "--verify"):
            g.add_argument(opt, metavar=metavar, help=h)


def build_plan_volume_parser():
    text = "read-only dry-run of 'migrate volume' over a selection: list it, plan the VMs of --attached"
    p = argparse.ArgumentParser(prog="migrate plan volume", description=text, epilog=PLAN_VOLUME_EPILOG,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    add_volume_args(p, plan=True)
    return p


def build_parser():
    fmt = argparse.RawDescriptionHelpFormatter
    p = argparse.ArgumentParser(
        prog="migrate", formatter_class=fmt, epilog=EPILOG,
        description="Migrate VMs between two independent OpenStack clouds: iSCSI Cinder -> Ceph RBD.")
    p.add_argument("--version", action="version", version="migrate " + __version__)
    sub = p.add_subparsers(dest="command", metavar="COMMAND")
    for name, text in (("plan", "read-only dry-run: list the selection, export each VM, run the destination "
                                "preflight; exit 1 when a plan fails ('plan volume <selection>': the dry-run "
                                "of 'volume')"),
                       ("migrate", "plan the selection, ask 'Type yes' (unless --yes), then migrate one VM at a time "
                                   "(cold stops it first, live only at the cutover)")):
        sp = sub.add_parser(name, help=text, description=text, formatter_class=fmt,
                            epilog=(PLAN_NOTE + EPILOG) if name == "plan" else EPILOG)
        sp.add_argument("vm", nargs="?", help="VM name or ID (same as --vm <vm>)")
        g = sp.add_argument_group("selection")
        for opt, metavar, h in SELECTOR_HELP:
            g.add_argument(opt, metavar=metavar, help=h)
        if name == "migrate":
            g.add_argument("--yes", action="store_true",
                           help="no confirmation prompt (required when stdin is not a terminal).")
            g.add_argument("--continue-on-error", action="store_true",
                           help="go on after a failure (default: stop at the first one, the rest SKIP(not-run)).")
        g = sp.add_argument_group("options of each VM")
        for opt, metavar, h in VM_OPTIONS:
            if metavar:
                g.add_argument(opt, metavar=metavar, help=h)
            else:
                g.add_argument(opt, action="store_true", help=h)
    text = "copy volumes without creating a server: one volume, or the volumes of a selection"
    vp = sub.add_parser("volume", help=text, description=text, formatter_class=fmt, epilog=VOLUME_EPILOG)
    vp.add_argument("volume", nargs="?", help="volume name or ID (alone: no selector options)")
    add_volume_args(vp, plan=False)
    rb = sub.add_parser("rollback", help="delete the destination objects, restore and start the source VM; or delete "
                                         "a volume copy",
                        description="delete the destination objects, restore and start the source VM; or delete a "
                                    "volume copy (the source volume is never modified)")
    rb.add_argument("target", help="the VM name or ID the migrate ran with, or a volume name, ID or vol-<id>")
    return p


def _help(argv, parser=None):
    try:
        (parser or build_parser()).parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 0
    return 0


def guarded(fn):
    """bash die / exit / Ctrl-C as exit codes."""
    try:
        return fn()
    except Die as e:
        print("ERROR: %s" % e, file=sys.stderr, flush=True)
        return 1
    except Exit as e:
        return e.code
    except KeyboardInterrupt:
        return 130


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    return guarded(lambda: _main(argv))


def _main(argv):
    if not argv:
        build_parser().print_help()
        return 2
    if argv[0] in ("-h", "--help", "--version"):
        return _help(argv)
    cmd, rest = argv[0], argv[1:]
    # 'plan volume ...' is the dry-run of 'volume' (a VM named "volume" is planned with --vm volume).
    if cmd == "plan" and rest[:1] == ["volume"]:
        return run_plan_volume(rest[1:])
    if cmd in ("plan", "migrate", "volume", "rollback"):
        if not rest:
            _help([cmd, "--help"])
            return 2
        if "-h" in rest or "--help" in rest:
            return _help([cmd, "--help"])
    if cmd in ("plan", "migrate"):
        return selector.run(cmd, rest)
    if cmd == "volume":
        s = selector.split_args("volume", rest)
        if s.positional and not s.selected_by_option():
            if len(s.positional) > 1:
                raise Die("one volume only (got: %s): several go by --project or --vm" % " ".join(s.positional))
            return run_volume(s.positional[0], s.passed)
        if s.positional:
            raise Die("a volume goes alone: %s volume <volume> [--in-use] [--volume-type t] [--az z] [--verify v]; "
                      "a selection takes --project or --vm/--vm-file, not a volume" % prog())
        return selector.run("volume", rest)
    if cmd == "rollback":
        if len(rest) > 1:
            raise Die("rollback takes no options")
        return run_rollback(rest[0])
    # Commands of the bash build typed by habit: point at their replacement.
    if cmd == "batch":
        raise Die("there is no 'batch' command: plan and migrate take the selection themselves: %s migrate --vm a,b | "
                  "--host h | --project p | --vm-file f" % prog())
    if cmd == "migrate-volume":
        raise Die("'migrate-volume' is now: %s volume <volume> [--in-use]" % prog())
    raise Die("unknown command " + cmd)


def run_plan_volume(args):
    """migrate plan volume <selection>: the batch's '--volumes attached|available|all --plan-only'."""
    if not args:
        _help(["--help"], build_plan_volume_parser())
        return 2
    if "-h" in args or "--help" in args:
        return _help(["--help"], build_plan_volume_parser())
    s = selector.split_args("volume", args, plan_only=True)
    if s.positional:
        raise Die("%s plan volume takes a selection: --project <p> --available|--attached|--all, or --vm <vms>. "
                  "A single volume has no dry-run: %s volume <volume> runs its read-only checks first and stops "
                  "on a FAIL before creating anything" % (prog(), prog()))
    return selector.run("volume", args, plan_only=True)


def run_vm(cmd, vm, args):
    """One VM (the child of a selector run): bash migrate-vm.sh plan|migrate <vm> [options]."""
    cfg = config.load()
    o = Options()
    o.load_default_opts(cmd, cfg.MIG_DEFAULT_OPTS)
    o.parse_options(args)
    if o.in_use:
        raise Die("--in-use belongs to migrate-volume: plan and migrate move a VM with its volumes (--mode live keeps "
                  "it running until the cutover)")
    if o.mode == "live":
        check_numbers(o)
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


def run_volume(target, args):
    """One volume: bash migrate-vm.sh migrate-volume <volume> [options]."""
    volume_options_only(args)  # the typed options only; load_default_opts filters the defaults
    cfg = config.load()
    o = Options()
    o.load_default_opts("migrate-volume", cfg.MIG_DEFAULT_OPTS)
    o.parse_options(args)
    r = Run(cfg, o)
    o.apply_az_defaults(cfg.MIG_AZ, cfg.AZ_TYPE_MAP)
    r.pool = o.pool_for_type(cfg.RBD_POOL, cfg.RBD_POOL_MAP)
    r.check_project_scope()
    cmd_migrate_volume(r, target)
    return 0


def run_rollback(target):
    cfg = config.load()
    r = Run(cfg, Options())
    r.check_project_scope()
    # A VM state under that name wins, exactly as before; anything else is taken as a volume.
    if os.path.exists(os.path.join(StateDir(target).path, "server.json")):
        rollback_vm(r, StateDir(target))
    else:
        cmd_rollback_volume(r, target)
    return 0
