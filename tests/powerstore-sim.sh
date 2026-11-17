#!/usr/bin/env bash
# Run on mig-host as root, once the source storage serves LIO targets on two portals (.12 and .13).
# PowerStore-sim: attaches a clone of demo-root through brick_attach.py with MIG_MULTIPATH=1 and
# checks two active paths, read-only enforcement, a full read that survives losing one portal,
# path recovery, and a detach that leaves no map, device or session behind. No VM is touched.
set -uo pipefail
source /opt/mig-venv/bin/activate
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
P1=10.77.0.12; P2=10.77.0.13; PORT=3260; MY_IP=10.77.0.20
BRICK=/usr/local/bin/brick_attach.py; STATE_DIR=/var/lib/mig-attach
CLONE_TIMEOUT=${CLONE_TIMEOUT:-900}; READ_TIMEOUT=${READ_TIMEOUT:-900}; RECOVER_TIMEOUT=${RECOVER_TIMEOUT:-60}
FW=(OUTPUT -d "$P2" -m comment --comment pssim-test -j DROP)  # tagged so cleanup removes only ours
WORK=$(mktemp -d); CLONE=""; RP=""; MAP=""; DM=""
REPORT=/root/pssim-report.txt; : > "$REPORT"
src() { ( source /opt/mig/src/admin-openrc.sh; openstack "$@" ); }
brick() { ( source /opt/mig/src/admin-openrc.sh; MIG_MULTIPATH=1 MIG_ISCSI_IP=$MY_IP "$BRICK" "$@" ); }
say() { echo "$*" | tee -a "$REPORT"; }
assert() { if eval "$2" >/dev/null 2>&1; then say "PASS  $1"; else say "FAIL  $1"; fi; }
need() { if eval "$2" >/dev/null 2>&1; then say "PASS  $1"; else say "FAIL  $1"; say "ABORT: precondition failed"; exit 1; fi; }

wait_for() {  # timeout-seconds interval condition ; polls until the condition holds
  local end=$((SECONDS + $1))
  for ((; SECONDS < end; )); do eval "$3" >/dev/null 2>&1 && return 0; sleep "$2"; done
  eval "$3" >/dev/null 2>&1
}
wait_vol() {  # volume-id wanted-status timeout-seconds ; non-zero on error status or timeout
  local end=$((SECONDS + $3)) s
  for ((; SECONDS < end; )); do
    s=$(src volume show "$1" -f value -c status 2>/dev/null)
    [ "$s" = "$2" ] && return 0
    case $s in error*) return 1;; esac
    sleep 3
  done
  return 1
}
sessions_to() { iscsiadm -m session 2>/dev/null | grep -cF " $1"; }  # "tcp: [1] 10.77.0.12:3260,1 iqn..."
# Path lines of the map under test, e.g. "  |- 3:0:0:0 sdb 8:16 active ready running".
mp_paths() { multipath -ll "$MAP" 2>/dev/null | grep -E '[0-9]+:[0-9]+:[0-9]+:[0-9]+ +sd[a-z]+ '; }
paths_ready() { mp_paths | grep -c 'active ready'; }
paths_bad() { mp_paths | grep -cE 'failed|faulty'; }
dm_read_mib() { awk '{print int($3 * 512 / 1048576)}' "/sys/block/$DM/stat" 2>/dev/null || echo 0; }
gone() { local d; for d; do [ ! -e "/sys/block/$d" ] || return 1; done; }
stop_reader() { [ -n "$RP" ] || return 0; pkill -P "$RP" 2>/dev/null; kill "$RP" 2>/dev/null; RP=""; }

cleanup() {
  stop_reader
  while iptables -D "${FW[@]}" 2>/dev/null; do :; done
  if [ -n "$CLONE" ]; then
    [ -e "$STATE_DIR/$CLONE.json" ] && brick detach "$CLONE" >/dev/null 2>&1
    wait_vol "$CLONE" available 300
    src volume delete "$CLONE" >/dev/null 2>&1 || say "WARN  clone $CLONE left on source"
  fi
  rm -rf "$WORK"
  say "=== summary"
  say "PASS=$(grep -c '^PASS' "$REPORT") FAIL=$(grep -c '^FAIL' "$REPORT")"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

say "=== 1 preconditions"
need "multipathd active" "systemctl is-active multipathd"
need "volume demo-root exists on source" "src volume show demo-root"
need "no iSCSI session to 10.77.0.* yet (leftovers would fake the detach checks)" "[ \"\$(sessions_to 10.77.0.)\" = 0 ]"

say "=== 2 sendtargets discovery through $P1 advertises both portals"
# nonpersistent: do not leave node records for the in-use demo volumes on this host.
DISC=$(iscsiadm -m discovery -t sendtargets -p "$P1:$PORT" -o nonpersistent 2>&1)
say "INFO  portals: $(grep -oE '^[0-9.]+:[0-9]+' <<<"$DISC" | sort -u | tr '\n' ' ')"
assert "discovery lists $P1" "grep -qF '$P1:$PORT,' <<<\"\$DISC\""
assert "discovery lists $P2" "grep -qF '$P2:$PORT,' <<<\"\$DISC\""

say "=== 3 clone demo-root"
T0=$SECONDS
CLONE=$(src volume create pssim-clone --source demo-root -f value -c id)
[ -n "$CLONE" ] || { say "ABORT: clone create failed"; exit 1; }
wait_vol "$CLONE" available "$CLONE_TIMEOUT"; RC=$?
say "INFO  clone $CLONE status=$(src volume show "$CLONE" -f value -c status) after $((SECONDS - T0))s"
assert "clone available" "[ $RC = 0 ]"
[ "$RC" = 0 ] || { say "ABORT: clone not available"; exit 1; }
SIZE_GB=$(src volume show "$CLONE" -f value -c size)

say "=== 4 multipath attach through brick_attach.py"
DEV=$(brick attach "$CLONE" 2>"$WORK/attach.err"); RC=$?
assert "brick_attach attach rc=0" "[ $RC = 0 ]"
if [ "$RC" != 0 ] || [ -z "$DEV" ]; then
  tail -3 "$WORK/attach.err" | cut -c1-200 | tee -a "$REPORT"; say "ABORT: attach failed"; exit 1
fi
DM=$(basename "$(readlink -f "$DEV")"); MAP=$(cat "/sys/block/$DM/dm/name" 2>/dev/null)
STATEF=$STATE_DIR/$CLONE.json
say "INFO  device $DEV map=${MAP:-none}"
assert "attach printed a block device" "[ -b '$DEV' ]"
assert "device is a dm-* multipath map" "[[ '$DM' == dm-* ]] && [ -n '$MAP' ] && dmsetup table '$MAP' | grep -qw multipath"
[ -n "$MAP" ] || { say "ABORT: no multipath map, remaining checks are meaningless"; exit 1; }
wait_for 30 1 '[ "$(paths_ready)" = 2 ]'
multipath -ll "$MAP" 2>&1 | tee -a "$REPORT"
SDS=$(mp_paths | grep -oE ' sd[a-z]+ ' | tr -d ' ' | tr '\n' ' ')
assert "map has 2 active ready paths" "[ \"\$(paths_ready)\" = 2 ]"
assert "one session to $P1" "[ \"\$(sessions_to '$P1:$PORT,')\" = 1 ]"
assert "one session to $P2" "[ \"\$(sessions_to '$P2:$PORT,')\" = 1 ]"
assert "device size equals the Cinder size (${SIZE_GB}G)" "[ \"\$(blockdev --getsize64 '$DEV')\" = $((SIZE_GB * 1024 * 1024 * 1024)) ]"
assert "state file records multipath and the WWN" "jq -e '.multipath == true and (.scsi_wwn // \"\") != \"\"' '$STATEF'"
assert "state WWN matches the dm map uuid" "[ \"\$(cat /sys/block/$DM/dm/uuid)\" = \"mpath-\$(jq -r .scsi_wwn '$STATEF')\" ]"

say "=== 5 read-only"
assert "kernel read-only flag on $DEV" "[ \"\$(blockdev --getro '$DEV')\" = 1 ]"
# oflag=direct: a buffered write could land in the page cache and only fail later, or not at all.
assert "direct write to $DEV is refused" "! dd if=/dev/zero of='$DEV' bs=4k count=1 oflag=direct conv=notrunc status=none"

say "=== 6 full read survives losing the $P2 path"
# iflag=direct on both reads: the second one must hit the paths, not the page cache.
T0=$SECONDS; R0=$(dm_read_mib)
( dd if="$DEV" iflag=direct bs=4M status=none | md5sum | cut -d' ' -f1 > "$WORK/md5.failover" ) 2>"$WORK/read.err" &
RP=$!
# Let the read get going (256 MiB in), then cut the second portal while it is still running.
wait_for 30 0.5 '[ $(( $(dm_read_mib) - R0 )) -ge 256 ] || ! kill -0 $RP'
iptables -I "${FW[@]}"
say "INFO  dropped $P2 after $(( $(dm_read_mib) - R0 )) MiB read"
assert "read still in flight when $P2 was dropped" "kill -0 $RP"
wait_for 60 1 '[ "$(paths_bad)" = 1 ]'
say "INFO  multipathd saw the failed path after $((SECONDS - T0))s"
wait_for "$READ_TIMEOUT" 1 "! kill -0 $RP"
if kill -0 "$RP" 2>/dev/null; then
  RRC=timeout; stop_reader
else
  wait "$RP"; RRC=$?; RP=""
fi
say "INFO  full read rc=$RRC took $((SECONDS - T0))s"
[ "$RRC" = 0 ] || tail -3 "$WORK/read.err" | cut -c1-200 | tee -a "$REPORT"
assert "full read with one path down exits 0 within ${READ_TIMEOUT}s" "[ '$RRC' = 0 ]"
multipath -ll "$MAP" 2>&1 | tee -a "$REPORT"
assert "exactly one path failed/faulty" "[ \"\$(paths_bad)\" = 1 ]"
assert "the $P1 path stayed active" "[ \"\$(paths_ready)\" = 1 ]"
iptables -D "${FW[@]}"
T0=$SECONDS
wait_for "$RECOVER_TIMEOUT" 2 '[ "$(paths_ready)" = 2 ]'
say "INFO  paths after restore: ready=$(paths_ready) bad=$(paths_bad) after $((SECONDS - T0))s"
assert "both paths active again within ${RECOVER_TIMEOUT}s" "[ \"\$(paths_ready)\" = 2 ]"
SUM2=$(dd if="$DEV" iflag=direct bs=4M status=none | md5sum | cut -d' ' -f1)
assert "data read during failover equals a clean re-read" "[ -n '$SUM2' ] && [ \"\$(cat '$WORK/md5.failover')\" = '$SUM2' ]"

say "=== 7 detach"
brick detach "$CLONE" 2>"$WORK/detach.err"; RC=$?
assert "brick_attach detach rc=0" "[ $RC = 0 ]"
[ "$RC" = 0 ] || tail -3 "$WORK/detach.err" | cut -c1-200 | tee -a "$REPORT"
assert "multipath map $MAP is gone" "! dmsetup info '$MAP'"
assert "path devices ($SDS) removed" "gone $SDS"
assert "no iSCSI session left to 10.77.0.*" "[ \"\$(sessions_to 10.77.0.)\" = 0 ]"
assert "attach state file removed" "[ ! -e '$STATEF' ]"
wait_vol "$CLONE" available 120; RC=$?
assert "clone back to available" "[ $RC = 0 ]"
src volume delete "$CLONE"
wait_for 300 3 "! src volume show '$CLONE'"
assert "clone deleted" "! src volume show '$CLONE'"
CLONE=""
