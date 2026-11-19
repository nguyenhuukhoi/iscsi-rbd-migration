#!/usr/bin/env bash
# Unit + parity harness of the migratetool package: stdlib only, no cloud, no root (DEVELOPING-PYTHON 5.3).
#
#   tests/unit/run.sh                    the unit suites (any python3 >= 3.10, a venv's too; well under a minute)
#   tests/unit/run.sh --parity [NAME..]  + the bash-reference-vs-app parity scenarios (Linux, bash, jq;
#                                        about 20 minutes; NAME filters scenarios by substring)
#
# PYTHON=<interpreter> picks the python of the unit suites (default python3). The parity runs always use
# /usr/bin/python3 for the stubs and the app, as the bash reference does. Generated files go to
# $MIG_UNIT_WORK (default /tmp/mig-unit-work), never into the repository.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
PY=${PYTHON:-python3}
PARITY=0
if [ "${1:-}" = "--parity" ]; then
  PARITY=1
  shift
fi
export MIG_UNIT_WORK=${MIG_UNIT_WORK:-/tmp/mig-unit-work}
mkdir -p "$MIG_UNIT_WORK"
cd "$MIG_UNIT_WORK" || exit 2

pass=0 fail=0 broken=""
suite() {  # suite <label> <command...>: runs it, adds its 'TOTAL pass=N fail=M' to the sums
  local label=$1 out total p f
  shift
  echo "=== $label"
  out=$("$@" 2>&1)
  printf '%s\n' "$out" > "$MIG_UNIT_WORK/$label.log"
  # The FAIL lines only (the parity suite prints long diagnostics); the whole output stays in the log.
  printf '%s\n' "$out" | grep -E '^(FAIL|SKIP) '
  echo "(full output: $MIG_UNIT_WORK/$label.log)"
  total=$(printf '%s\n' "$out" | grep -E '^TOTAL pass=[0-9]+ fail=[0-9]+$' | tail -1)
  if [ -z "$total" ]; then
    echo "$label: no TOTAL line (crashed)"
    broken="$broken $label"
    fail=$((fail + 1))
    return
  fi
  p=${total#TOTAL pass=}; p=${p%% *}
  f=${total##*fail=}
  echo "$label: $total"
  pass=$((pass + p))
  fail=$((fail + f))
}

suite unit "$PY" -I "$HERE/unit.py"
suite p5unit "$PY" -I "$HERE/p5unit.py" "$REPO"
suite p6unit "$PY" -I "$HERE/p6unit.py" "$REPO"
suite v72unit "$PY" -I "$HERE/v72unit.py" "$REPO"
if [ "$PARITY" = 1 ]; then
  suite parity /usr/bin/python3 -I "$HERE/scenarios.py" "$@"
fi
echo "ALL pass=$pass fail=$fail${broken:+ (crashed:$broken)}"
[ "$fail" = 0 ]
