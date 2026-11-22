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
suite() {  # suite <label> <command...>: runs it, adds its 'TOTAL pass=N fail=M' to the sums (and its exit code, E2)
  local label=$1 out total p f rc
  shift
  echo "=== $label"
  out=$("$@" 2>&1)
  rc=$?
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
  if [ "$rc" != 0 ] && [ "$f" = 0 ]; then  # E2: a suite that exits non-zero with fail=0 still failed
    echo "$label: exit code $rc although its TOTAL says fail=0"
    f=1
    broken="$broken $label(rc=$rc)"
  fi
  echo "$label: $total"
  pass=$((pass + p))
  fail=$((fail + f))
}

checks() {  # 7.3 (DESIGN-V7-3-CLI 11.6): the CLI starts isolated; SEC-7: no shell, os.system, eval, exec
  local p=0 f=0 want got hits
  want=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$REPO/migratetool/__init__.py")
  # 'python -I -m migratetool.cli --version' from this source tree (-I keeps the current directory off sys.path,
  # so the tree is put there explicitly, then the module runs as -m runs it).
  got=$("$PY" -I -c 'import runpy, sys
sys.path.insert(0, sys.argv[1])
sys.argv = ["migratetool.cli", "--version"]
runpy.run_module("migratetool.cli", run_name="__main__", alter_sys=True)' "$REPO" 2>&1)
  if [ "$got" = "migrate $want" ]; then
    echo "PASS python -I -m migratetool.cli --version: $got"; p=$((p + 1))
  else
    echo "FAIL python -I -m migratetool.cli --version: '$got' (want 'migrate $want')"; f=$((f + 1))
  fi
  hits=$(grep -rn --include='*.py' 'shell=True\|os\.system\|eval(\|exec(' "$REPO/migratetool/" "$REPO/migui/")
  # E6: no 'bash -c' either, except the one that sources an openrc (clouds.rc_env)
  hits="$hits$(grep -rn --include='*.py' '"bash", *"-c"' "$REPO/migratetool/" "$REPO/migui/" | grep -v '^[^:]*/migratetool/clouds.py:')"
  if [ -z "$hits" ]; then
    echo "PASS SEC-7 grep: no shell=True, os.system, eval( or exec( in migratetool/ and migui/"; p=$((p + 1))
  else
    echo "FAIL SEC-7 grep: $hits"; f=$((f + 1))
  fi
  echo "TOTAL pass=$p fail=$f"
}

suite checks checks
suite unit "$PY" -I "$HERE/unit.py"
suite p5unit "$PY" -I "$HERE/p5unit.py" "$REPO"
suite p6unit "$PY" -I "$HERE/p6unit.py" "$REPO"
suite v72unit "$PY" -I "$HERE/v72unit.py" "$REPO"
suite v73unit "$PY" -I "$HERE/v73unit.py" "$REPO"
suite v1unit "$PY" -I "$HERE/v1unit.py" "$REPO"  # 1.0.0: REVIEW-20261121 phase 1
suite ui env PYTHON="$PY" bash "$REPO/tests/ui/run.sh"  # 7.3 UI (migui): DESIGN-UI 10 and 14, tests/ui/
if [ "$PARITY" = 1 ]; then
  suite parity /usr/bin/python3 -I "$HERE/scenarios.py" "$@"
fi
echo "ALL pass=$pass fail=$fail${broken:+ (crashed:$broken)}"
[ "$fail" = 0 ]
