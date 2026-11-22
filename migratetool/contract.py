"""The strings, prefixes and codes other programs read from 'migrate' (DESIGN.md, "contracts").

Scripts, the batch selector, migui-runner and the lab test suites grep these. They live here, once, so that a
change is one edit and one unit test (tests/unit/v1unit.py checks each value). The web UI (migui) is a separate
package that never imports migratetool: migui/runner.py keeps its own copy of LEVELS and LOGS_RE and the unit test
compares the two.
"""
import re

# --- preflight / doctor lines: two spaces, the level, padded to column 8 ------------------------------------------
OK = "  OK    "
WARN = "  WARN  "
FAIL = "  FAIL  "
INFO = "  INFO  "
LEVELS = ((OK, "OK"), (WARN, "WARN"), (FAIL, "FAIL"), (INFO, "INFO"))
CHECK_RE = re.compile(r"^  (OK|WARN|FAIL|INFO)\s{2,}(.*)$")
# the selector's 'Plan:' table takes the FAIL/ERROR lines of a failed plan child
PLAN_FAIL_RE = re.compile(r" +FAIL|ERROR")
PREFLIGHT_PASSED = "Preflight passed."

# --- selector ------------------------------------------------------------------------------------------------------
# '[HH:MM:SS] logs in <dir>; project map: ...' (migui-runner takes the batch log directory from it)
LOGS_IN = "logs in %s; project map: %s (%d entries, other projects keep their name)"
LOGS_RE = re.compile(r"logs in (/\S+?);")
EPHEMERAL_NO_VOLUME = "ephemeral VM has no volume to migrate"
KEYPAIR_WARN_RE = re.compile(r"WARN keypair ([^ \n]*) not at destination")
STATE_EXISTS = "state exists in %s: run rollback first"

# --- machine-readable lines ----------------------------------------------------------------------------------------
PROGRESS_PREFIX = "@@ "

# --- exit codes (DESIGN.md "exit codes") ---------------------------------------------------------------------------
EXIT_OK = 0
EXIT_FAIL = 1            # a general failure, a plan that FAILed, nothing selected
EXIT_USAGE = 2           # command-line syntax, a confirmation that cannot be asked (no terminal, no --yes)
EXIT_BEFORE_CUTOVER = 3  # a migrate failed before t-stop: the source never stopped (its state cleans itself on re-run)
EXIT_AFTER_CUTOVER = 4   # a migrate failed after t-stop: the source is stopped, roll back (or fix and re-run rollback)
EXIT_BATCH_FAIL = 5      # a selection of several rows had a FAIL
EXIT_INTERRUPTED = 130   # Ctrl-C / SIGINT

# --- ownership marks of what a run creates at the destination (A2) -------------------------------------------------
OWNER_SOURCE = "mig.source_id"   # server property: the source server ID
OWNER_STATE = "mig.state"        # server property: the state directory name (the ident the run used)
PORT_OWNER_PREFIX = "mig:"       # port description 'mig:<state>' (a port without a source description)
