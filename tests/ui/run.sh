#!/usr/bin/env bash
# UI tests (DESIGN-UI 10 and 14): migui-runner + migui-web on a temporary socket and a free loopback port, the
# fake migrate of fake_migrate.py behind them. No root needed (as root, SEC-UI-7 also connects as uid 65534).
#
#   tests/ui/run.sh            security (section 10 and 16.4) + functional + passwd (16.2) + install_audit (root only);
#                              one 'TOTAL pass=N fail=M'
#
# PYTHON=<interpreter> (default python3, the system one on mig-host: 3.12). Work files: $MIGUI_TEST_WORK (default
# /tmp), one migui-test-* directory per stack, removed afterwards (MIGUI_TEST_KEEP=1 keeps them).
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
PY=${PYTHON:-python3}
pass=0 fail=0
for t in security functional passwd install_audit v1ui v11ui; do
  echo "--- $t"
  out=$("$PY" -I "$HERE/$t.py" "$REPO" 2>&1)
  printf '%s\n' "$out"
  total=$(printf '%s\n' "$out" | grep -E '^TOTAL pass=[0-9]+ fail=[0-9]+$' | tail -1)
  if [ -z "$total" ]; then
    echo "$t: no TOTAL line (crashed)"
    fail=$((fail + 1))
    continue
  fi
  p=${total#TOTAL pass=}; p=${p%% *}
  f=${total##*fail=}
  pass=$((pass + p))
  fail=$((fail + f))
done
# SEC-7 for the UI too: no shell, os.system, eval or exec in migui/; no inline script / innerHTML in the pages
hits=$(grep -rn --include='*.py' 'shell=True\|os\.system\|eval(\|exec(\|"bash", *"-c"' "$REPO/migui/")
if [ -z "$hits" ]; then echo "PASS SEC-7 grep migui/"; pass=$((pass + 1)); else echo "FAIL SEC-7 grep migui/: $hits"; fail=$((fail + 1)); fi
echo "TOTAL pass=$pass fail=$fail"
[ "$fail" = 0 ]
