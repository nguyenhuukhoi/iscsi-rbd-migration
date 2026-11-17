#!/usr/bin/env bash
# Run on mig-host as root, with demo-vm ACTIVE on the source. Checks the tool fails safely and that
# rollback recovers from a copy killed halfway. Finishes with a clean migration to the destination.
set -uo pipefail
source /opt/mig-venv/bin/activate
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
VM=demo-vm; IP=192.168.120.50
REPORT=/root/failure-report.txt; : > "$REPORT"
SSHO=(-n -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 -o LogLevel=ERROR)
vssh() { ssh "${SSHO[@]}" "ubuntu@$IP" "$@"; }
src() { ( source /opt/mig/src/admin-openrc.sh; openstack "$@" ); }
dst() { ( source /opt/mig/dst/admin-openrc.sh; openstack "$@" ); }
say() { echo "$*" | tee -a "$REPORT"; }
assert() { if eval "$2" >/dev/null 2>&1; then say "PASS  $1"; else say "FAIL  $1"; fi; }
wait_ssh() { local t; for t in $(seq 1 90); do vssh true 2>/dev/null && return 0; sleep 3; done; return 1; }

wait_ssh || { say "ABORT: source VM unreachable at start"; exit 1; }
SUM=$(vssh 'sudo sha256sum /data/random.bin | cut -d" " -f1')
[ -n "$SUM" ] || { say "ABORT: empty baseline checksum"; exit 1; }

say "=== F1 preflight refuses an occupied IP and does not touch the source"
dst port create f1-blocker --network vlan120 --fixed-ip ip-address=$IP -f value -c id >/dev/null
migrate-vm.sh migrate $VM 2>&1 | grep -E 'FAIL|ERROR' | tee -a "$REPORT"; RC=${PIPESTATUS[0]}
assert "migrate exits non-zero" "[ $RC != 0 ]"
assert "source still ACTIVE and unlocked" "src server show $VM -f json | jq -e '.status==\"ACTIVE\" and .locked==false'"
assert "no volume created on destination" "! dst volume list -f value -c Name | grep -q demo-"
dst port delete f1-blocker

say "=== F2 copy killed halfway"
migrate-vm.sh migrate $VM > /tmp/f2.log 2>&1 & MP=$!
for t in $(seq 1 240); do QP=$(pgrep -x qemu-img | head -1); [ -n "$QP" ] && break; sleep 1; done
[ -n "$QP" ] || { say "ABORT: qemu-img never started, F2 tested nothing"; tail -3 /tmp/f2.log | tee -a "$REPORT"; exit 1; }
sleep 8
say "INFO  killing qemu-img pid=$QP"
kill -9 "$QP"
wait "$MP"; RC=$?
tail -3 /tmp/f2.log | cut -c1-160 | tee -a "$REPORT"
assert "migrate exits non-zero" "[ $RC != 0 ]"
assert "transfer host logged out of iSCSI" "! iscsiadm -m session"
assert "no destination server was created" "! dst server show $VM"
assert "source stopped and locked, not started by accident" "src server show $VM -f json | jq -e '.status==\"SHUTOFF\" and .locked==true'"
assert "second migrate refuses to run over old state" "! migrate-vm.sh migrate $VM"

say "=== F3 rollback after the failed copy"
migrate-vm.sh rollback $VM 2>&1 | tee -a "$REPORT"; RC=${PIPESTATUS[0]}
assert "rollback rc=0" "[ $RC = 0 ]"
sleep 10; wait_ssh && say "PASS  source reachable" || say "FAIL  source reachable"
assert "data checksum unchanged" "[ \"\$(vssh 'sudo sha256sum /data/random.bin | cut -d\" \" -f1')\" = '$SUM' ]"
assert "no clone left on source" "! src volume list -f value -c Name | grep -q mig-clone"
assert "destination volumes removed" "! dst volume list -f value -c Name | grep -q demo-"
assert "RBD pool empty" "[ -z \"\$(rbd --id mig-xfer -p mig-volumes ls)\" ]"

say "=== F4 clean migration after all of the above"
migrate-vm.sh migrate $VM 2>&1 | grep -vE '^  OK ' | tee -a "$REPORT"; RC=${PIPESTATUS[0]}
assert "migrate rc=0" "[ $RC = 0 ]"
[ "$RC" = 0 ] || { say "ABORT: migrate failed, remaining checks are meaningless"; exit 1; }
wait_ssh && say "PASS  guest SSH on destination" || say "FAIL  guest SSH on destination"
assert "data checksum on destination" "[ \"\$(vssh 'sudo sha256sum /data/random.bin | cut -d\" \" -f1')\" = '$SUM' ]"

say "=== summary"
say "PASS=$(grep -c '^PASS' "$REPORT") FAIL=$(grep -c '^FAIL' "$REPORT")"
