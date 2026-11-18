"""Child entry of a selector run (what migrate-batch.sh ran as migrate-vm.sh):

    python -m migratetool.single plan|migrate|migrate-volume <vm-or-volume> [options]

One VM or one volume, in the project SRC_PROJECT/DST_PROJECT name, no selection and no prompt.
Not meant to be typed: 'migrate plan|migrate|volume' select, confirm and call it once per row.
"""
import sys

from .cli import guarded, run_vm, run_volume
from .util import Die


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)

    def go():
        if len(argv) < 2:
            raise Die("usage: python -m migratetool.single plan|migrate|migrate-volume <vm-or-volume> [options]")
        cmd, target, rest = argv[0], argv[1], argv[2:]
        if cmd in ("plan", "migrate"):
            return run_vm(cmd, target, rest)
        if cmd == "migrate-volume":
            return run_volume(target, rest)
        raise Die("unknown command " + cmd)
    return guarded(go)


if __name__ == "__main__":
    sys.exit(main())
