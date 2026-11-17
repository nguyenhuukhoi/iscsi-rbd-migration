#!/usr/bin/env bash
# Live-mode e2e on mig-host: demo-vm ACTIVE on the source, a churn writer dirtying blocks the whole
# time. Migrates with --mode live, measures downtime from the guest's own once-per-second log,
# checks data integrity and that the cutover only carried a delta, then rolls back.
set -uo pipefail
source /opt/mig-venv/bin/activate
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
VM=demo-vm; IP=192.168.120.50; STATE=/var/lib/mig-migrate/$VM
REPORT=/root/live-report.txt; : > "$REPORT"
SSHO=(-n -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 -o LogLevel=ERROR)
vssh() { ssh "${SSHO[@]}" "ubuntu@$IP" "$@"; }
src() { ( source /opt/mig/src/admin-openrc.sh; openstack "$@" ); }
dst() { ( source /opt/mig/dst/admin-openrc.sh; openstack "$@" ); }
say() { echo "$*" | tee -a "$REPORT"; }
assert() { if eval "$2" >/dev/null 2>&1; then say "PASS  $1"; else say "FAIL  $1"; fi; }
wait_ssh() { local t; for t in $(seq 1 90); do vssh true 2>/dev/null && return 0; sleep 3; done; return 1; }
datasum() { vssh 'sudo sha256sum /data/random.bin | cut -d" " -f1'; }
marker_gap() { vssh 'cat /var/lib/demo-marker.log' | awk -v from="$(( $1 - 5 ))" '$1<from {next} n++ && $1-p>m {m=$1-p; a=p; b=$1} {p=$1} END {printf "%.1f %.3f %.3f\n", m, a, b}'; }

say "=== baseline on source, start churn writer"
wait_ssh || { say "ABORT: source VM unreachable"; exit 1; }
assert "no leftover migrate state" "[ ! -d $STATE ]"
[ -d "$STATE" ] && exit 1
SUM=$(datasum); KEY=$(vssh 'sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub | cut -d" " -f2')
for v in "$SUM" "$KEY"; do [ -n "$v" ] || { say "ABORT: empty baseline"; exit 1; }; done
# Churn: every second overwrite a random 4 MB block inside a 400 MB region of /data (vdb) and
# fsync, so every pre-sync pass finds freshly dirtied chunks. Sequence log proves writes continued.
vssh 'sudo tee /usr/local/bin/demo-churn.sh >/dev/null <<"EOS"
#!/bin/bash
i=0
while true; do
  dd if=/dev/urandom of=/data/churn.bin bs=1M count=4 seek=$((4 * (RANDOM % 100))) conv=notrunc,fsync 2>/dev/null
  i=$((i+1)); echo "$(date +%s) $i" >> /data/churn-seq.log; sync /data/churn-seq.log
  sleep 1
done
EOS
sudo chmod +x /usr/local/bin/demo-churn.sh
sudo systemd-run --unit demo-churn /usr/local/bin/demo-churn.sh 2>/dev/null || sudo systemctl restart demo-churn'
sleep 5
assert "churn writer is writing" "vssh 'test -s /data/churn-seq.log'"

say "=== migrate --mode live"
T0=$SECONDS
migrate-vm.sh migrate $VM --mode live 2>&1 | grep -vE '^  OK ' | tee -a "$REPORT"; RC=${PIPESTATUS[0]}
assert "migrate rc=0" "[ $RC = 0 ]"
[ "$RC" = 0 ] || { say "ABORT: migrate failed"; exit 1; }
say "INFO  wall time total $((SECONDS-T0))s"
wait_ssh && say "PASS  guest SSH after migrate" || say "FAIL  guest SSH after migrate"

say "=== downtime and delta"
TSTOP=$(cat "$STATE/t-stop")
read -r GAP LAST RESUME < <(marker_gap "$TSTOP")
say "INFO  app outage ${GAP}s; last write $LAST, resumed $RESUME, stop requested $TSTOP"
assert "writes continued until the stop request" "awk 'BEGIN{exit !($LAST >= $TSTOP - 2)}'"
assert "service resumed" "awk 'BEGIN{exit !($RESUME > $TSTOP)}'"
assert "downtime under the 293s cold reference" "awk 'BEGIN{exit !($GAP < 293)}'"
for f in "$STATE"/sync-*.log; do
  VID=$(basename "$f" | sed 's/sync-//;s/\.log//')
  say "INFO  passes for $VID:"
  jq -r '"      pass: changed=\(.changed)/\(.chunks) written=\(.written_bytes/1048576|floor)MB zeros=\(.zero_chunks) \(.seconds)s first=\(.first)"' "$f" | tee -a "$REPORT"
  FIRSTW=$(jq -sr '.[0].written_bytes' "$f"); LASTW=$(jq -sr '.[-1].written_bytes' "$f")
  NPASS=$(jq -sr length "$f")
  assert "$VID ran pre-sync passes before cutover" "[ $NPASS -ge 2 ]"
  assert "$VID final delta smaller than first full pass" "[ $LASTW -lt $FIRSTW ]"
done

say "=== data integrity on destination"
assert "static data checksum unchanged" "[ \"\$(datasum)\" = '$SUM' ]"
assert "SSH host key preserved" "[ \"\$(vssh 'sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub | cut -d\" \" -f2')\" = '$KEY' ]"
assert "running on the destination cloud" "[ \"\$(vssh 'cat /var/lib/cloud/data/instance-id')\" = \"\$(dst server show $VM -f value -c id)\" ]"
assert "churn file consistent (last seq readable, fs clean)" "vssh 'tail -1 /data/churn-seq.log | grep -qE \"^[0-9]+ [0-9]+$\"'"
LASTSEQ_T=$(vssh 'tail -1 /data/churn-seq.log | cut -d" " -f1')
assert "churn wrote until the stop request" "awk 'BEGIN{exit !($LASTSEQ_T >= $TSTOP - 3)}'"
assert "no filesystem errors in kernel log" "! vssh 'sudo dmesg | grep -qE \"EXT4-fs error|I/O error\"'"
assert "destination volumes on RBD" "dst volume show $VM-vda -f value -c 'os-vol-host-attr:host' | grep -qi rbd"

say "=== source preserved, then rollback"
assert "source SHUTOFF and locked" "src server show $VM -f json | jq -e '.status==\"SHUTOFF\" and .locked==true'"
assert "no clone left on source" "! src volume list -f value -c Name | grep -q mig-clone"
assert "no iSCSI session on transfer host" "! iscsiadm -m session"
vssh 'sudo systemctl stop demo-churn' 2>/dev/null
migrate-vm.sh rollback $VM 2>&1 | tee -a "$REPORT"
sleep 10; wait_ssh && say "PASS  source reachable after rollback" || say "FAIL  source after rollback"
assert "back on the source instance" "[ \"\$(vssh 'cat /var/lib/cloud/data/instance-id')\" = \"\$(src server show $VM -f value -c id)\" ]"
assert "static data checksum on source" "[ \"\$(datasum)\" = '$SUM' ]"
assert "destination volumes removed" "! dst volume list -f value -c Name | grep -q demo-"
assert "RBD pool empty" "[ -z \"\$(rbd --id mig-xfer -p mig-volumes ls)\" ]"
vssh 'sudo systemctl stop demo-churn; sudo systemctl reset-failed demo-churn' 2>/dev/null

say "=== summary"
say "PASS=$(grep -c '^PASS' "$REPORT") FAIL=$(grep -c '^FAIL' "$REPORT")"
