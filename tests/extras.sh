#!/usr/bin/env bash
# e2e on mig-host for the v2.1 additions, two parts:
#   A. multi-NIC VM + --flavor/--security-group overrides + server properties travel with the VM
#      (demo-vm gets a second port, cold migrate with overrides, rollback, cleanup)
#   B. ephemeral (image-backed, no volume) VM through the Glance snapshot path, then rollback.
# Needs demo-vm ACTIVE on the source and no migrate state.
set -uo pipefail
source /opt/mig-venv/bin/activate
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
# The second NIC lives on its OWN network (vlan121), as real multi-NIC VMs do. Two NICs on one
# subnet break asymmetric-routing-wise on the destination: cloud-init regenerates config for both
# NICs there and the duplicate default route sends replies out of the wrong port, where Neutron
# port security drops them. On separate subnets every reply stays on-link and nothing conflicts.
VM=demo-vm; IP=192.168.120.50; IP2=192.168.121.51
REPORT=/root/extras-report.txt; : > "$REPORT"
SSHO=(-n -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 -o LogLevel=ERROR)
src() { ( source /opt/mig/src/admin-openrc.sh; openstack "$@" ); }
dst() { ( source /opt/mig/dst/admin-openrc.sh; openstack "$@" ); }
say() { echo "$*" | tee -a "$REPORT"; }
assert() { if eval "$2" >/dev/null 2>&1; then say "PASS  $1"; else say "FAIL  $1"; fi; }
vssh() { ssh "${SSHO[@]}" "ubuntu@$IP" "$@"; }
wait_ssh() { local ip=$1 t; for t in $(seq 1 90); do ssh "${SSHO[@]}" "ubuntu@$ip" true 2>/dev/null && return 0; sleep 3; done; return 1; }

say "=== A. multi-NIC + --flavor + --security-group + server properties"
assert "no leftover migrate state" "[ ! -d /var/lib/mig-migrate/$VM ]"
[ -d "/var/lib/mig-migrate/$VM" ] && exit 1
wait_ssh $IP || { say "ABORT: demo-vm unreachable"; exit 1; }
SUM=$(vssh 'sudo sha256sum /data/random.bin | cut -d" " -f1')
SID=$(src server show $VM -f value -c id)

say "--- prepare: destination flavor/SG, vlan121 on both clouds, source second NIC, properties"
dst flavor show mig.small2 >/dev/null 2>&1 || dst flavor create mig.small2 --vcpus 1 --ram 1024 --disk 10 >/dev/null
dst security group show mig-sg-b >/dev/null 2>&1 || {
  dst security group create mig-sg-b >/dev/null
  dst security group rule create mig-sg-b --ingress --protocol icmp >/dev/null
  dst security group rule create mig-sg-b --ingress --protocol tcp --dst-port 22 >/dev/null
}
for c in src dst; do
  $c network show vlan121 >/dev/null 2>&1 || $c network create vlan121 --share --external \
    --provider-network-type vlan --provider-physical-network physnet1 --provider-segment 121 --mtu 1400 >/dev/null
  if ! $c subnet show vlan121-subnet >/dev/null 2>&1; then
    if [ "$c" = src ]; then P="start=192.168.121.10,end=192.168.121.99"; else P="start=192.168.121.100,end=192.168.121.199"; fi
    $c subnet create vlan121-subnet --network vlan121 --subnet-range 192.168.121.0/24 \
      --gateway 192.168.121.1 --allocation-pool "$P" --dhcp >/dev/null
  fi
done
ip link show eth1.121 >/dev/null 2>&1 || {
  ip link add link eth1 name eth1.121 type vlan id 121
  ip link set eth1.121 mtu 1400 up
  ip addr add 192.168.121.1/24 dev eth1.121
}
src port show demo-port2 >/dev/null 2>&1 || src port create demo-port2 --network vlan121 \
  --fixed-ip subnet=vlan121-subnet,ip-address=$IP2 --security-group mig-vm-sg >/dev/null
[ "$(src port show demo-port2 -f value -c device_id)" = "$SID" ] || src server add port $VM demo-port2
# Find the second NIC by MAC, never by name: after a rollback reattach the guest can leave it
# stuck as "cirename0" (udev rename race), so en*-style names cannot be trusted. DHCP on its own
# subnet needs no route tricks, but keep the default route off it so the primary stays primary.
# netplan apply resets networking and freezes the SSH session running it, so it runs detached.
MAC2=$(src port show demo-port2 -f value -c mac_address)
NIC2=$(vssh "grep -li '$MAC2' /sys/class/net/*/address 2>/dev/null | head -1 | awk -F/ '{print \$5}'")
[ -n "$NIC2" ] || { say "ABORT: second NIC (MAC $MAC2) not visible in guest"; exit 1; }
say "INFO  second NIC in guest: $NIC2 ($MAC2)"
vssh "sudo sh -c 'cat > /etc/netplan/60-extra.yaml <<EOF
network:
  version: 2
  ethernets:
    second:
      match: {macaddress: \"$MAC2\"}
      dhcp4: true
      dhcp4-overrides: {use-routes: false}
      routes:
        - {to: 192.168.121.0/24, scope: link}
EOF
chmod 600 /etc/netplan/60-extra.yaml; nohup netplan apply >/dev/null 2>&1 &'"
sleep 8
src server set --property env=prod --property app=demo $VM
for t in $(seq 1 30); do ping -c1 -W2 $IP2 >/dev/null 2>&1 && break; sleep 3; done
assert "second NIC answers on the source ($IP2)" "ping -c2 -W2 $IP2"

say "--- migrate with overrides"
migrate-vm.sh migrate $VM --flavor mig.small2 --security-group mig-sg-b 2>&1 | grep -vE '^  OK ' | tee -a "$REPORT"; RC=${PIPESTATUS[0]}
assert "migrate rc=0" "[ $RC = 0 ]"
[ "$RC" = 0 ] || { say "ABORT: migrate failed"; exit 1; }
wait_ssh $IP && say "PASS  SSH on first NIC after migrate" || say "FAIL  SSH on first NIC after migrate"
for t in $(seq 1 20); do ping -c1 -W2 $IP2 >/dev/null 2>&1 && break; sleep 3; done
assert "second NIC answers on the destination ($IP2)" "ping -c2 -W2 $IP2"
assert "both destination ports exist" "dst port show $VM-nic0 && dst port show $VM-nic1"
assert "second source MAC present on a destination port" \
  "dst port list -f value -c mac_address | grep -qix '$MAC2'"
assert "data checksum unchanged" "[ \"\$(vssh 'sudo sha256sum /data/random.bin | cut -d\" \" -f1')\" = '$SUM' ]"
assert "destination flavor is the override" "dst server show $VM -f json | jq -e '.flavor | (.original_name // .name) == \"mig.small2\"'"
SGB=$(dst security group show mig-sg-b -f value -c id)
assert "destination port 0 carries only mig-sg-b" "dst port show $VM-nic0 -f json | jq -e --arg s '$SGB' '.security_group_ids == [\$s]'"
assert "destination port 1 carries only mig-sg-b" "dst port show $VM-nic1 -f json | jq -e --arg s '$SGB' '.security_group_ids == [\$s]'"
assert "server property env=prod copied" "dst server show $VM -f json | jq -e '(.properties // .metadata).env == \"prod\"'"
assert "server property app=demo copied" "dst server show $VM -f json | jq -e '(.properties // .metadata).app == \"demo\"'"

say "--- rollback and cleanup A"
migrate-vm.sh rollback $VM 2>&1 | tee -a "$REPORT"
wait_ssh $IP && say "PASS  source reachable after rollback" || say "FAIL  source after rollback"
assert "both source ports reattached" \
  "[ \"\$(src port show demo-port -f value -c device_id)\" = '$SID' ] && [ \"\$(src port show demo-port2 -f value -c device_id)\" = '$SID' ]"
assert "destination clean (no server)" "! dst server show $VM"
vssh "sudo sh -c 'rm -f /etc/netplan/60-extra.yaml; nohup netplan apply >/dev/null 2>&1 &'" 2>/dev/null
src server remove port $VM demo-port2 2>/dev/null
src port delete demo-port2 2>/dev/null
src server unset --property env --property app $VM 2>/dev/null
say "INFO  demo-vm restored to one NIC, properties removed"

say "=== B. ephemeral VM (no volume) through the Glance snapshot path"
EVM=demo-eph
src server show $EVM >/dev/null 2>&1 || src server create $EVM --image noble --flavor mig.small \
  --network vlan120 --security-group mig-vm-sg --key-name mig-key --wait >/dev/null
EIP=$(src server show $EVM -f json | jq -r '.addresses.vlan120[0]')
say "INFO  $EVM on source at $EIP"
essh() { ssh "${SSHO[@]}" "ubuntu@$EIP" "$@"; }
wait_ssh $EIP || { say "ABORT: $EVM unreachable"; exit 1; }
assert "source VM really has no volume" "[ -z \"\$(src server volume list $EVM -f value -c 'Volume ID')\" ]"
essh 'sudo dd if=/dev/urandom of=/root/blob.bin bs=1M count=50 status=none && sudo sha256sum /root/blob.bin | sudo tee /root/blob.sha >/dev/null && sync'
ESUM=$(essh 'sudo cut -d" " -f1 /root/blob.sha')
EKEY=$(essh 'sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub | cut -d" " -f2')
[ -n "$ESUM" ] || { say "ABORT: empty ephemeral baseline"; exit 1; }

say "--- migrate ephemeral (cold, same IP)"
migrate-vm.sh migrate $EVM 2>&1 | grep -vE '^  OK ' | tee -a "$REPORT"; RC=${PIPESTATUS[0]}
assert "migrate rc=0" "[ $RC = 0 ]"
[ "$RC" = 0 ] || { say "ABORT: ephemeral migrate failed"; exit 1; }
wait_ssh $EIP && say "PASS  guest SSH after migrate" || say "FAIL  guest SSH after migrate"
assert "running on the destination instance" "[ \"\$(essh 'cat /var/lib/cloud/data/instance-id')\" = \"\$(dst server show $EVM -f value -c id)\" ]"
assert "data checksum unchanged" "[ \"\$(essh 'sudo sha256sum /root/blob.bin | cut -d\" \" -f1')\" = '$ESUM' ]"
assert "SSH host key preserved" "[ \"\$(essh 'sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub | cut -d\" \" -f2')\" = '$EKEY' ]"
assert "destination VM boots from an image, still no volume" "[ -z \"\$(dst server volume list $EVM -f value -c 'Volume ID')\" ]"
assert "destination image mig-snap-$EVM active" "[ \"\$(dst image show mig-snap-$EVM -f value -c status)\" = active ]"
assert "source snapshot cleaned up" "[ -z \"\$(src image list --name mig-snap-$EVM -f value -c ID)\" ]"
assert "no filesystem errors in kernel log" "! essh 'sudo dmesg | grep -qE \"EXT4-fs error|I/O error\"'"

say "--- rollback and cleanup B"
migrate-vm.sh rollback $EVM 2>&1 | tee -a "$REPORT"
wait_ssh $EIP && say "PASS  source reachable after rollback" || say "FAIL  source after rollback"
assert "data checksum on source" "[ \"\$(essh 'sudo sha256sum /root/blob.bin | cut -d\" \" -f1')\" = '$ESUM' ]"
assert "destination image removed" "! dst image show mig-snap-$EVM"
assert "destination server removed" "! dst server show $EVM"
src server delete $EVM --wait 2>/dev/null
RPORT=$(src port list --fixed-ip ip-address=$EIP -f value -c ID)
[ -z "$RPORT" ] || src port delete $RPORT
say "INFO  $EVM deleted, reserve port cleaned"

say "=== summary"
say "PASS=$(grep -c '^PASS' "$REPORT") FAIL=$(grep -c '^FAIL' "$REPORT")"
