#!/usr/bin/env bash
# Run on mig-host as root. The common real-world shape the main test does not cover: a VM created
# with --network and --boot-from-volume, so Nova owns the port and the boot volume has no name.
# Migrates, checks, rolls back, then removes the test VM.
set -uo pipefail
source /opt/mig-venv/bin/activate
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
VM=demo-auto
REPORT=/root/novaport-report.txt; : > "$REPORT"
SSHO=(-n -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 -o LogLevel=ERROR)
src() { ( source /opt/mig/src/admin-openrc.sh; openstack "$@" ); }
dst() { ( source /opt/mig/dst/admin-openrc.sh; openstack "$@" ); }
say() { echo "$*" | tee -a "$REPORT"; }
assert() { if eval "$2" >/dev/null 2>&1; then say "PASS  $1"; else say "FAIL  $1"; fi; }

src server show $VM >/dev/null 2>&1 || src server create $VM --flavor mig.small --image noble \
  --boot-from-volume 10 --network vlan120 --security-group mig-vm-sg --key-name mig-key --wait -f value -c id >/dev/null
IP=$(src server show $VM -f json | jq -r '.addresses.vlan120[0]')
PORT0=$(src port list --server $VM -f value -c ID)
MAC=$(src port show "$PORT0" -f value -c mac_address)
VOL=$(src server volume list $VM -f json | jq -r '.[0]."Volume ID"')
vssh() { ssh "${SSHO[@]}" "ubuntu@$IP" "$@"; }
wait_ssh() { local t; for t in $(seq 1 100); do vssh true 2>/dev/null && return 0; sleep 3; done; return 1; }
wait_ssh || { say "ABORT: $VM unreachable before migrate"; exit 1; }
for t in $(seq 1 40); do vssh 'cloud-init status 2>/dev/null | grep -q done' && break; sleep 5; done
vssh 'echo payload-$(date +%s) | sudo tee /root/payload.txt >/dev/null; sync'
PAY=$(vssh 'sudo cat /root/payload.txt')
[ -n "$PAY" ] || { say "ABORT: empty baseline"; exit 1; }
say "ip=$IP mac=$MAC volume-name='$(src volume show "$VOL" -f value -c name)' payload=$PAY"

say "=== migrate"
migrate-vm.sh migrate $VM 2>&1 | grep -E 'Nova removed|ERROR|FAIL|Migrated' | tee -a "$REPORT"; RC=${PIPESTATUS[0]}
assert "migrate rc=0" "[ $RC = 0 ]"
[ "$RC" = 0 ] || { say "ABORT: migrate failed"; exit 1; }
wait_ssh
DSID=$(dst server show $VM -f value -c id)
assert "guest is the destination instance" "[ \"\$(vssh 'sudo cat /sys/class/dmi/id/product_uuid' | tr A-Z a-z)\" = '$DSID' ]"
assert "payload intact" "[ \"\$(vssh 'sudo cat /root/payload.txt')\" = '$PAY' ]"
assert "same MAC and IP" "vssh \"ip -br link | grep -q $MAC && ip -4 a | grep -q $IP/24\""
assert "IP and MAC still reserved on source" "[ \"\$(src port list --mac-address $MAC -f value -c ID | wc -l)\" = 1 ]"
assert "reservation is not attached to any VM" "[ -z \"\$(src port list --mac-address $MAC -f value -c device_id)\" ]"
assert "unnamed volume received a generated name" "dst server volume list $VM -f json | jq -r '.[0].\"Volume ID\"' | xargs -I{} bash -c 'source /opt/mig/dst/admin-openrc.sh; openstack volume show {} -f value -c name' | grep -q '^mig-'"

say "=== rollback"
migrate-vm.sh rollback $VM 2>&1 | grep -E 'reattached|ERROR|ACTIVE' | tee -a "$REPORT"; RC=${PIPESTATUS[0]}
assert "rollback rc=0" "[ $RC = 0 ]"
sleep 10; wait_ssh && say "PASS  source reachable" || say "FAIL  source reachable"
SID=$(src server show $VM -f value -c id)
assert "guest is the source instance" "[ \"\$(vssh 'sudo cat /sys/class/dmi/id/product_uuid' | tr A-Z a-z)\" = '$SID' ]"
assert "same MAC and IP on the re-created port" "vssh \"ip -br link | grep -q $MAC && ip -4 a | grep -q $IP/24\""
assert "payload intact on source" "[ \"\$(vssh 'sudo cat /root/payload.txt')\" = '$PAY' ]"

say "=== cleanup of this test VM"
src server delete $VM --wait
for t in $(seq 1 30); do [ -z "$(src port list --mac-address $MAC -f value -c ID)" ] && break; P=$(src port list --mac-address $MAC -f value -c ID); src port delete "$P" 2>/dev/null; sleep 2; done
src volume show "$VOL" >/dev/null 2>&1 && { for t in $(seq 1 30); do [ "$(src volume show "$VOL" -f value -c status)" = available ] && break; sleep 2; done; src volume delete "$VOL"; }
say "=== summary"
say "PASS=$(grep -c '^PASS' "$REPORT") FAIL=$(grep -c '^FAIL' "$REPORT")"
