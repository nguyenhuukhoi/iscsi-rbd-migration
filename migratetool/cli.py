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
import contextlib
import os
import signal
import sys
import threading

from . import __version__, config, contract, jsonout, locking, progress, selector
from .context import Run
from .livesync import check_numbers
from .jsonout import utc
from .options import Options
from .preflight import load_flavor_map
from .record import write_rollback_json
from .rollback import rollback_vm, summary as rollback_summary, summary_lines as rollback_summary_lines
from .state import StateDir
from .util import Die, Exit, log, prog
from .vmcmd import cmd_migrate, cmd_plan
from .volcmd import cmd_migrate_volume, volume_options_only

VM_OPTIONS = (
    ("--mode", "cold|live", "cold (default): stop the VM, qemu-img copy + full compare (unless --verify none). "
                            "live: hash-cached pre-sync passes while the VM runs; the cutover only syncs the "
                            "final delta (blocksync, run by SYS_PY)."),
    ("--volume-type", "TYPE", "Cinder volume type for every destination volume, VOLUME_TYPE_MAP included (default: "
                              "per volume VOLUME_TYPE_MAP by its source type, else the AZ's entry in AZ_TYPE_MAP, else "
                              "the cloud default). RBD_POOL_MAP maps types to RBD pools."),
    ("--az", "ZONE", "destination availability zone of the server, and of the volumes unless --volume-az (default: "
                     "MIG_AZ, else the destination schedulers pick). Also the <AZ> of the copied group names (without "
                     "--az and MIG_AZ: the destination's only compute AZ)."),
    ("--volume-az", "ZONE", "Cinder availability zone of the destination volumes when it is not named like the compute "
                            "one (default: MIG_VOLUME_AZ, else the --az zone)."),
    ("--dst-host", "HOST", "destination hypervisor of the new server (Host column of 'openstack compute service list' "
                           "at the destination; server create --host, Nova API 2.74, admin role). The preflight checks "
                           "it is a nova-compute host, up, enabled, in the --az zone (without --az its zone becomes the "
                           "AZ). Command line only. Default: the scheduler picks."),
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
    ("--volumes-only", None, "cold only: copy the volumes, create no destination port or server."),
    ("--ephemeral-as-image", None, "an ephemeral (image-backed) root disk: boot the destination VM from the uploaded "
                                   "image, as up to 7.3 (default: a destination volume made from it, boot from volume)."),
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


JSON_HELP = ("print one JSON object on stdout (the text lines go to stderr); never carries openrc values. "
             "GUIDE.md section 7.")
PLAN_JOBS_HELP = ("plan up to N VMs at once (1-8, default 1; plan is read-only); with N > 1 each line of a plan is "
                  "shown after [<ident>], its log keeps it as is.")
PROGRESS_HELP = ("also print machine-readable progress lines on stdout: '@@ {\"event\": ...}' (phase, copy, blocksync, "
                 "pass, t_stop, downtime, result); without it the output is unchanged.")


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
        g.add_argument("--progress-json", action="store_true", help=PROGRESS_HELP)
    else:
        g.add_argument("--json", action="store_true", help=JSON_HELP)
        g.add_argument("--plan-jobs", metavar="N", help=PLAN_JOBS_HELP)
    g = vp.add_argument_group("options")
    if not plan:
        g.add_argument("--in-use", action="store_true",
                       help="one volume only: copy an attached volume from a clone taken while attached "
                            "(crash-consistent).")
    for opt, metavar, h in VM_OPTIONS:
        if opt in ("--volume-type", "--az", "--volume-az", "--verify"):
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
            g.add_argument("--progress-json", action="store_true", help=PROGRESS_HELP)
        else:
            g.add_argument("--json", action="store_true", help=JSON_HELP)
            g.add_argument("--plan-jobs", metavar="N", help=PLAN_JOBS_HELP)
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
    text = ("read-only: the selection plan and migrate would make, with host, AZ, flavor, boot, networks, volumes "
            "and the state directory of each VM (or volume); exports nothing, runs no preflight")
    lp = sub.add_parser("list", help=text, description=text, formatter_class=fmt)
    lp.add_argument("vm", nargs="?", help="VM name or ID (same as --vm <vm>)")
    g = lp.add_argument_group("selection")
    for opt, metavar, h in SELECTOR_HELP:
        g.add_argument(opt, metavar=metavar, help=h)
    g.add_argument("--volumes", action="store_true", help="list volumes instead of VMs: --attached (the volumes of the "
                                                          "selected VMs, default with --vm), --available, --all "
                                                          "(default with --project).")
    g.add_argument("--available", action="store_true", help="with --volumes --project: its available volumes.")
    g.add_argument("--attached", action="store_true", help="with --volumes: the volumes of the selected VMs.")
    g.add_argument("--all", action="store_true", help="with --volumes --project: attached first, then available.")
    lp.add_argument("--json", action="store_true", help=JSON_HELP)
    text = ("read-only: hosts, projects, availability zones, flavors and volume types of both clouds (what the "
            "options can name)")
    ip = sub.add_parser("inventory", help=text, description=text)
    ip.add_argument("--count", action="store_true", help="also count the VMs of each source host (one listing per "
                                                         "host, at most %d hosts)." % 64)
    ip.add_argument("--host", metavar="HOSTS", help="with --count: only these source hosts.")
    ip.add_argument("--json", action="store_true", help=JSON_HELP)
    text = ("read-only, no cloud: the state directories of this host (live, plan only, rolled back), the batch log "
            "directories and the migrate processes running now")
    sp = sub.add_parser("status", help=text, description=text)
    sp.add_argument("--all", action="store_true", help="every rolled-back directory and batch (at most 2000; "
                                                       "default the newest 50).")
    sp.add_argument("--json", action="store_true", help=JSON_HELP)
    text = "read-only check of this host: programs, python modules, conf, openrcs and tokens, roles, RBD pools, iscsid"
    dp = sub.add_parser("doctor", help=text, description=text + "; exit 1 when a check fails. Run it before anything "
                                                                "else.")
    dp.add_argument("--json", action="store_true", help=JSON_HELP)
    dp.add_argument("--write-check", action="store_true",
                    help="also prove the RBD user can write: create a 4 MiB image mig-doctor-<epoch> in each pool, write, "
                         "read back, discard and delete it (the only thing doctor changes; never a cloud).")
    rb = sub.add_parser("rollback", help="delete the destination objects, restore the source VM; or delete a volume "
                                         "copy",
                        description="delete the destination objects, give the source VM back its ports and the power "
                                    "state it had before the migration; or delete a volume copy (the source volume is "
                                    "never modified). Shows what it deletes and asks 'Type <name>' unless --yes.")
    rb.add_argument("target", help="the VM name or ID the migrate ran with, or a volume name, ID or vol-<id>")
    rb.add_argument("--yes", action="store_true", help="no summary, no prompt (required when stdin is not a terminal)")
    rb.add_argument("--dry-run", action="store_true", help="print the summary only, change nothing (with --json: one "
                                                           "JSON object)")
    rb.add_argument("--json", action="store_true", help="with --dry-run: " + JSON_HELP)
    text = "delete old rolled-back state directories and batch log directories"
    pp = sub.add_parser("prune", help=text, description=text + ". Never a live state, never a plan-only state.")
    pp.add_argument("--older-than", metavar="Nd", required=True, help="age, in days: 30d (at least 1d)")
    pp.add_argument("--dry-run", action="store_true", help="list what would go, delete nothing")
    pp.add_argument("--json", action="store_true", help=JSON_HELP)
    return p


def _help(argv, parser=None):
    try:
        (parser or build_parser()).parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 0
    return 0


def progress_result(rc, vm):
    """7.3 (7): the closing '@@ {"event": "result"}' line of a migrate run: its exit code and, for a VM, the ID of the
    destination server it recorded (null when there is none)."""
    fields = {"rc": rc}
    if vm:
        try:
            st = StateDir(vm)
            fields["dst_server_id"] = st.read("dst-server-id").strip() or None
        except Die:
            fields["dst_server_id"] = None
    progress.emit("result", **fields)


def guarded(fn):
    """bash die / exit / Ctrl-C as exit codes. A Die carries its own exit code when it has one (C8: 3 or 4 for a migrate
    that failed before or after its cutover); otherwise 1."""
    try:
        return fn()
    except Die as e:
        print("ERROR: %s" % e, file=sys.stderr, flush=True)
        return getattr(e, "exit_code", None) or contract.EXIT_FAIL
    except Exit as e:
        return e.code
    except KeyboardInterrupt:
        return contract.EXIT_INTERRUPTED


def _sigint_once(_signum, _frame):
    """The first SIGINT cancels (KeyboardInterrupt unwinds the run); the ones after it are ignored, so that a second
    Ctrl-C, or the SIGINT a selector passes on to a child that already had it, cannot cut the cleanup short (detach,
    keep the state). SIGKILL stays the hard stop."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    raise KeyboardInterrupt


def honour_sigint():
    """SIGINT is how a migrate is cancelled (Ctrl-C, the UI's Hủy, kill -INT): it unwinds the run, detaches the clone
    and keeps the state, exit 130. Installed whether SIGINT was ignored (a process started in the background of a
    non-interactive shell, 'migrate ... &' in a script, inherits it ignored and Python keeps it so) or had Python's
    default handler. A handler some other code installed is left alone."""
    if signal.getsignal(signal.SIGINT) in (signal.SIG_IGN, signal.default_int_handler, _sigint_once):
        signal.signal(signal.SIGINT, _sigint_once)


@contextlib.contextmanager
def sigint_scope():
    """honour_sigint() for the life of one entry point (main of the CLI or of a child), then the handler the caller
    had: a library caller that runs main() in its own process is not left with SIGINT ignored after a cancel."""
    if threading.current_thread() is not threading.main_thread():
        yield  # signal handlers belong to the main thread: an embedding caller's worker leaves them as they are
        return
    prev = signal.getsignal(signal.SIGINT)
    honour_sigint()
    try:
        yield
    finally:
        if prev is not None:
            signal.signal(signal.SIGINT, prev)


def main(argv=None):
    os.umask(0o077)  # B6: state, logs and JSON records are root's alone, whatever umask the shell had
    argv = sys.argv[1:] if argv is None else list(argv)
    with sigint_scope():
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
    if cmd in ("plan", "migrate", "volume", "rollback", "prune"):
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
            if not s.progress:
                return run_volume(s.positional[0], s.passed)
            progress.enable()  # 7.3 (7): a lone volume runs in this process
            rc = guarded(lambda: run_volume(s.positional[0], s.passed))
            progress_result(rc, "")
            return rc
        if s.positional:
            raise Die("a volume goes alone: %s volume <volume> [--in-use] [--volume-type t] [--az z] [--verify v]; "
                      "a selection takes --project or --vm/--vm-file, not a volume" % prog())
        return selector.run("volume", rest)
    if cmd == "list":
        if "-h" in rest or "--help" in rest or not rest:
            _help([cmd, "--help"])
            return 0 if rest else 2
        from .listing import run_list
        return run_list(rest)
    if cmd == "status":
        if "-h" in rest or "--help" in rest:
            return _help([cmd, "--help"])
        from .status import run_status
        return run_status(rest)
    if cmd == "inventory":
        if "-h" in rest or "--help" in rest:
            return _help([cmd, "--help"])
        from .inventory import run_inventory
        return run_inventory(rest)
    if cmd == "doctor":
        if "-h" in rest or "--help" in rest:
            return _help([cmd, "--help"])
        from .doctor import run_doctor, run_doctor_json
        bad = [a for a in rest if a not in ("--json", "--write-check")]
        if bad:
            raise Die("doctor takes --json and --write-check only (got %s)" % bad[0])
        if "--json" in rest:
            return run_doctor_json(write_check="--write-check" in rest)
        return run_doctor(write_check="--write-check" in rest)
    if cmd == "rollback":
        flags = [a for a in rest if a.startswith("-")]
        targets = [a for a in rest if not a.startswith("-")]
        bad = [a for a in flags if a not in ("--yes", "--dry-run", "--json")]
        if bad:
            raise Die("unknown option %s: rollback takes <target> [--yes] [--dry-run [--json]]" % bad[0])
        if len(targets) != 1:
            raise Die("rollback takes one target (got %d)" % len(targets))
        if "--json" in flags and "--dry-run" not in flags:
            raise Die("--json goes with --dry-run")
        return run_rollback(targets[0], yes="--yes" in flags, dry_run="--dry-run" in flags, as_json="--json" in flags,
                            ask=True)
    if cmd == "prune":
        from .prune import run_prune
        return run_prune(rest)
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
    """One VM (the child of a selector run): bash migrate-vm.sh plan|migrate <vm> [options]. A migrate holds the host
    lock (A7) and exits 3 when it failed before its cutover, 4 after it (C8)."""
    started = utc()
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
    r.argv, r.started = list(args), started
    if cmd == "plan" and jsonout.EFFECTIVE_LINE:  # SEC-9: the plan child of plan --json reads only
        r.src.readonly = r.dst.readonly = True
    if o.flavor_fallback and not o.volumes_only:
        load_flavor_map(r)
    o.apply_az_defaults(cfg.MIG_AZ, cfg.AZ_TYPE_MAP, cfg.MIG_VOLUME_AZ)
    r.pool = o.pool_for_type(cfg.RBD_POOL, cfg.RBD_POOL_MAP)
    r.check_project_scope()
    st = StateDir(vm)
    if cmd == "plan":
        cmd_plan(r, st)
        return 0
    with locking.held(cfg):
        try:
            cmd_migrate(r, st)
        except Die as e:
            if getattr(e, "exit_code", None) is None:
                e.exit_code = r.exit_code or (contract.EXIT_AFTER_CUTOVER if r.cut else contract.EXIT_BEFORE_CUTOVER)
            raise
        finally:
            st.unlock()
    return 0


def run_volume(target, args):
    """One volume: bash migrate-vm.sh migrate-volume <volume> [options]. Holds the host lock (A7)."""
    volume_options_only(args)  # the typed options only; load_default_opts filters the defaults
    started = utc()
    cfg = config.load()
    o = Options()
    o.load_default_opts("migrate-volume", cfg.MIG_DEFAULT_OPTS)
    o.parse_options(args)
    r = Run(cfg, o)
    r.argv, r.started = list(args), started
    o.apply_az_defaults(cfg.MIG_AZ, cfg.AZ_TYPE_MAP, cfg.MIG_VOLUME_AZ)
    r.pool = o.pool_for_type(cfg.RBD_POOL, cfg.RBD_POOL_MAP)
    r.check_project_scope()
    with locking.held(cfg):
        try:
            cmd_migrate_volume(r, target)
        finally:
            st = getattr(r, "volume_state", None)  # R3: the state directory lock ends with the run, as for a VM
            if st is not None:
                st.unlock()
    return 0


def confirm_rollback(d):
    """A5: the summary, then 'Type <name>' (the name of the VM or volume, or the ident typed in full). Not a
    terminal: exit 2 without changing anything."""
    for line in rollback_summary_lines(d):
        print(line, file=sys.stderr)
    want = d.get("name") or d["ident"]
    if not sys.stdin.isatty():
        print("ERROR: stdin is not a terminal: re-run with --yes to roll back without confirmation", file=sys.stderr,
              flush=True)
        raise Exit(contract.EXIT_USAGE)
    sys.stderr.write("Type %s to roll back: " % want)
    sys.stderr.flush()
    typed = sys.stdin.readline().rstrip("\r\n")
    if typed not in (want, d["ident"]):
        raise Die("not confirmed, nothing changed")


def rollback_scope(cfg, st):
    """P2-a (1.1.1): SRC_PROJECT/DST_PROJECT of a rollback come from the run that wrote st (options.json, effective
    projects.value {src, dst}). Neither set here: the record's are used (cfg changed in place). Set here: they must be
    the record's (both; one set and one not counts as different), else Die before anything is read or deleted. A state
    without that record (written before 7.3, or by the bash build) keeps this environment's. Returns (INFO line or
    None, origin "record" | "env" | "none")."""
    def shown(v):
        return v or "none"
    try:
        doc = jsonout.read_json(st.p("options.json"))
    except (OSError, ValueError):
        doc = None
    eff = doc.get("effective") if isinstance(doc, dict) else None
    proj = eff.get("projects") if isinstance(eff, dict) else None
    val = proj.get("value") if isinstance(proj, dict) else None
    cur = (cfg.SRC_PROJECT or "", cfg.DST_PROJECT or "")
    if not isinstance(val, dict):
        return ("INFO scope: the state has no project record; using SRC_PROJECT/DST_PROJECT of this environment",
                "env" if any(cur) else "none")
    rec = tuple(v if isinstance(v, str) else "" for v in (val.get("src"), val.get("dst")))
    if not any(cur):
        if not any(rec):
            return None, "none"
        cfg.values["SRC_PROJECT"], cfg.values["DST_PROJECT"] = rec
        return ("INFO scope SRC_PROJECT=%s DST_PROJECT=%s (from the state's options.json)" % (shown(rec[0]),
                                                                                             shown(rec[1])), "record")
    if cur != rec:
        raise Die("scope mismatch: this state was written with SRC_PROJECT=%s DST_PROJECT=%s, this run has "
                  "SRC_PROJECT=%s DST_PROJECT=%s: set them to the state's values (or unset them) and run the rollback "
                  "again; nothing was done" % (shown(rec[0]), shown(rec[1]), shown(cur[0]), shown(cur[1])))
    return None, "record"


def run_rollback(target, yes=True, dry_run=False, as_json=False, ask=False):
    """migrate rollback <target>. Library callers (tests) pass nothing and get the plain rollback; the command line
    passes ask=True: without --yes it shows summary() and asks (A5), --dry-run only shows it. Holds the host lock and
    the state directory lock (A7). The host lock comes first, before the state is looked up: a migrate of the same VM
    that is cleaning its failed run and starting again has no server.json for a moment, and a rollback looking then
    would say 'no state' instead of 'another migrate runs'. --dry-run reads only and takes no lock, as plan.
    P2-a (1.1.1): the state is first looked for on disk only (no cloud), and its project record sets the scope
    (rollback_scope) before the clouds are set up; under the lock the same lookup must find the same state. A volume
    found only through the source cloud (by name) keeps the scope of this environment (INFO)."""
    from .volcmd import find_volume_state, local_volume_state, rollback_volume
    started = utc()
    cfg = config.load()

    def local_state():
        # A VM state under that name wins, exactly as before; then a migrate-volume state found by its directory.
        if os.path.exists(os.path.join(StateDir(target).path, "server.json")):
            return StateDir(target), "vm"
        st_ = local_volume_state(target)
        return (st_, "vol") if st_ is not None else None

    # P2-a (1.1.1): the scope of the run that wrote the state, restored before the clouds are set up.
    found = local_state()
    if found is not None:
        note, origin = rollback_scope(cfg, found[0])
    else:
        note = ("INFO scope: %s is looked up through the source cloud: using SRC_PROJECT/DST_PROJECT of this "
                "environment" % target)
        origin = "env" if (cfg.SRC_PROJECT or cfg.DST_PROJECT) else "none"
    if note:
        if dry_run and as_json:
            print(note, file=sys.stderr, flush=True)
        else:
            log(note)
    r = Run(cfg, Options())
    r.check_project_scope()

    def target_state():
        now = local_state()
        if (now and (now[0].path, now[1])) != (found and (found[0].path, found[1])):
            # The scope above was decided from what was there before the lock: a state that appeared or went since.
            raise Die("the state of %s changed while the rollback started (a migrate of it ran): run the rollback "
                      "again; nothing was done" % target)
        return now if now is not None else (find_volume_state(r, target), "vol")

    def summary(st, kind):
        r.src.readonly = r.dst.readonly = True
        try:
            return rollback_summary(r, st, kind)
        finally:
            r.src.readonly = r.dst.readonly = False

    if dry_run:
        d = summary(*target_state())
        d["projects"] = {"src": cfg.SRC_PROJECT or None, "dst": cfg.DST_PROJECT or None, "origin": origin}
        if as_json:
            with jsonout.stdout_to_stderr() as real:
                jsonout.emit_json(d, stream=real, cfg=cfg)
        else:
            for line in rollback_summary_lines(d):
                print(line)
        return 0
    with locking.held(cfg):
        st, kind = target_state()
        if ask and not yes:
            confirm_rollback(summary(st, kind))  # under the lock: what is shown is what gets deleted
        st.lock()
        try:
            if kind == "vm":
                rollback_vm(r, st)
            else:
                rollback_volume(r, st)
        finally:
            st.unlock()
    # 1.2: what this rollback was, next to the options.json of the run it undid.
    write_rollback_json(cfg, st.retired, target, started)
    return 0


if __name__ == "__main__":  # python -m migratetool.cli (run.sh starts it isolated, --version)
    sys.exit(main())
