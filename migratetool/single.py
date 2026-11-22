"""Child entry of a selector run (what migrate-batch.sh ran as migrate-vm.sh):

    python -m migratetool.single plan|migrate|migrate-volume <vm-or-volume> [options]

One VM or one volume, in the project SRC_PROJECT/DST_PROJECT name, no selection and no prompt.
Not meant to be typed: 'migrate plan|migrate|volume' select, confirm and call it once per row.

7.3: 'plan <vm> --json' (from 'migrate plan --json') prints the effective options as one '@@ {"effective": ...}'
line after the preflight and talks to both clouds read-only; 'migrate|migrate-volume ... --progress-json' prints
the progress events and a closing result event. Both markers are taken off before the options are parsed.
"""
import sys

from . import jsonout, progress
from .cli import guarded, progress_result, run_vm, run_volume, sigint_scope
from .util import Die


def main(argv=None):
    with sigint_scope():  # the cancel handler for this child's run, then the caller's handler again
        return _main(argv)


def _main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    cmd = argv[0] if argv else ""
    target = argv[1] if len(argv) > 1 else ""
    rest = argv[2:]
    if cmd == "plan" and "--json" in rest:
        rest = [a for a in rest if a != "--json"]
        jsonout.EFFECTIVE_LINE = True
    if cmd in ("migrate", "migrate-volume") and "--progress-json" in rest:
        rest = [a for a in rest if a != "--progress-json"]
        progress.enable()

    def go():
        if len(argv) < 2:
            raise Die("usage: python -m migratetool.single plan|migrate|migrate-volume <vm-or-volume> [options]")
        if cmd in ("plan", "migrate"):
            return run_vm(cmd, target, rest)
        if cmd == "migrate-volume":
            return run_volume(target, rest)
        raise Die("unknown command " + cmd)
    rc = guarded(go)
    if progress.ENABLED:
        progress_result(rc, target if cmd == "migrate" else "")
    return rc


if __name__ == "__main__":
    sys.exit(main())
