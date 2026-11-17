#!/usr/bin/env bash
# Run on mig-host as root with demo-vm migrated (running on the destination) and mig-peer ACTIVE on
# the source. Compares source-port states: can peers on the source cloud still reach the migrated VM?
set -uo pipefail
source /opt/mig-venv/bin/activate
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
IP=192.168.120.50
SSHO=(-n -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 -o LogLevel=ERROR)
src() { ( source /opt/mig/src/admin-openrc.sh; openstack "$@" ); }
PEER=$(src server show mig-peer -f json | jq -r '.addresses.vlan120[0]')
pssh() { ssh "${SSHO[@]}" "ubuntu@$PEER" "$@"; }
vssh() { ssh "${SSHO[@]}" "ubuntu@$IP" "$@"; }

probe() {
  sleep 8
  pssh "sudo ip neigh flush to $IP" 2>/dev/null
  vssh "sudo ip neigh flush to $PEER" 2>/dev/null
  local a b c
  pssh "ping -c4 -W2 $IP" >/dev/null 2>&1 && a=OK || a=FAIL
  vssh "ping -c4 -W2 $PEER" >/dev/null 2>&1 && b=OK || b=FAIL
  c=$(arping -c4 -w6 -I eth1.120 $IP 2>/dev/null | grep -c 'bytes from')
  printf '%-34s peer->migrated=%-5s migrated->peer=%-5s wire-arp=%s/4\n' "$1" "$a" "$b" "$c"
}

echo "source server: $(src server show demo-vm -f value -c status)  peer: $PEER"
echo "port now: admin_up=$(src port show demo-port -f value -c admin_state_up) device=$(src port show demo-port -f value -c device_id | cut -c1-8)"

probe "A disabled, attached"
src port set demo-port --enable
probe "B enabled, attached, VM off"
src server remove port demo-vm demo-port; rc=$?
echo "remove port rc=$rc  device_id='$(src port show demo-port -f value -c device_id)' status=$(src port show demo-port -f value -c status)"
probe "C enabled, detached from VM"

echo "--- C + admin powers on the source VM"
src server start demo-vm
for t in $(seq 1 30); do [ "$(src server show demo-vm -f value -c status)" = ACTIVE ] && break; sleep 3; done
echo "source status: $(src server show demo-vm -f value -c status)"
sleep 45
probe "C with source VM powered on"
src server stop demo-vm
for t in $(seq 1 40); do [ "$(src server show demo-vm -f value -c status)" = SHUTOFF ] && break; sleep 3; done
echo "source status: $(src server show demo-vm -f value -c status)"

echo "--- does C keep the address reserved on the source?"
src port create guard-probe --network vlan120 --fixed-ip ip-address=$IP -f value -c id 2>&1 | tail -1 | cut -c1-150
