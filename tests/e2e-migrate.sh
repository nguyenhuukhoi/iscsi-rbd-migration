#!/usr/bin/env bash
# Run on mig-host as root, with demo-vm ACTIVE on the source. Migrates, verifies, rolls back and
# migrates again, leaving the VM on the destination. Every assertion must be able to fail.
set -uo pipefail
source /opt/mig-venv/bin/activate
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
VM=demo-vm; IP=192.168.120.50; IFACE=ens3; STATE=/var/lib/mig-migrate/$VM
REPORT=/root/e2e-report.txt; : > "$REPORT"
SSHO=(-n -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 -o LogLevel=ERROR)
DSTCMP=192.0.2.108
vssh() { ssh "${SSHO[@]}" "ubuntu@$IP" "$@"; }
src() { ( source /opt/mig/src/admin-openrc.sh; openstack "$@" ); }
dst() { ( source /opt/mig/dst/admin-openrc.sh; openstack "$@" ); }
say() { echo "$*" | tee -a "$REPORT"; }
assert() { if eval "$2" >/dev/null 2>&1; then say "PASS  $1"; else say "FAIL  $1"; fi; }
wait_ssh() { local t; for t in $(seq 1 90); do vssh true 2>/dev/null && return 0; sleep 3; done; return 1; }
datasum() { vssh 'sudo sha256sum /data/random.bin | cut -d" " -f1'; }
hostkey() { vssh 'sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub | cut -d" " -f2'; }

# Longest silence in the guest's own once-per-second log since this stop request: the true
# application outage. Older entries are ignored because the log survives earlier test runs.
marker_gap() { vssh 'cat /var/lib/demo-marker.log' | awk -v from="$(( $1 - 5 ))" '$1<from {next} n++ && $1-p>m {m=$1-p; a=p; b=$1} {p=$1} END {printf "%.1f %.3f %.3f\n", m, a, b}'; }

say "=== baseline on source"
wait_ssh || { say "ABORT: source VM unreachable, every comparison would be against nothing"; exit 1; }
SUM=$(datasum); KEY=$(hostkey); IID=$(vssh 'cat /var/lib/cloud/data/instance-id')
for v in "$SUM" "$KEY" "$IID"; do [ -n "$v" ] || { say "ABORT: empty baseline value"; exit 1; }; done
DOM_SRC=$(src server show $VM -f value -c OS-EXT-SRV-ATTR:instance_name)
src_nics() { ssh "${SSHO[@]}" root@192.0.2.104 "docker exec nova_libvirt virsh domiflist $DOM_SRC" | grep -c "$MAC"; }
MAC=$(vssh "cat /sys/class/net/$IFACE/address"); HN=$(vssh hostname)
FSTAB=$(vssh 'sudo sha256sum /etc/fstab | cut -d" " -f1')
src volume set demo-root --image-property hw_qemu_guest_agent=yes
say "instance-id=$IID hostname=$HN mac=$MAC"

say "=== migrate"
migrate-vm.sh migrate $VM 2>&1 | grep -vE '^  OK ' | tee -a "$REPORT"; RC=${PIPESTATUS[0]}
assert "migrate rc=0" "[ $RC = 0 ]"
# Everything below would run against the source VM and pass for the wrong reason.
[ "$RC" = 0 ] || { say "ABORT: migrate failed, remaining checks are meaningless"; exit 1; }
wait_ssh && say "PASS  guest SSH after migrate" || say "FAIL  guest SSH after migrate"

say "=== downtime (guest's own write log)"
TSTOP=$(cat "$STATE/t-stop")
read -r GAP LAST RESUME < <(marker_gap "$TSTOP")
say "app outage ${GAP}s; last write $LAST, resumed $RESUME, stop requested $TSTOP"
assert "writes continued until the stop request" "awk 'BEGIN{exit !($LAST >= $TSTOP - 2)}'"
assert "resumed after the stop request" "awk 'BEGIN{exit !($RESUME > $TSTOP)}'"

say "=== data integrity"
assert "data checksum unchanged" "[ \"\$(datasum)\" = '$SUM' ]"
assert "fstab unchanged" "[ \"\$(vssh 'sudo sha256sum /etc/fstab | cut -d\" \" -f1')\" = '$FSTAB' ]"
assert "root filesystem rw" "vssh 'findmnt -no OPTIONS / | grep -q ^rw'"
assert "/data mounted" "vssh 'findmnt /data'"
assert "no filesystem errors in kernel log" "! vssh 'sudo dmesg | grep -qE \"EXT4-fs error|I/O error\"'"

say "=== guest identity"
assert "instance-id changed to the destination UUID" "[ \"\$(vssh 'cat /var/lib/cloud/data/instance-id')\" = \"\$(dst server show $VM -f value -c id)\" ]"
assert "same interface, MAC and IP" "vssh \"grep -qx $MAC /sys/class/net/$IFACE/address && ip -4 a show $IFACE | grep -q $IP/24\""
assert "SSH host key preserved" "[ \"\$(hostkey)\" = '$KEY' ]"
assert "hostname preserved" "[ \"\$(vssh hostname)\" = '$HN' ]"
assert "metadata served by destination cloud" "vssh 'curl -s -m5 http://169.254.169.254/openstack/latest/meta_data.json' | grep -q \"\$(dst server show $VM -f value -c id)\""

say "=== destination storage is really RBD"
DOM=$(dst server show $VM -f value -c OS-EXT-SRV-ATTR:instance_name)
XML=$(ssh "${SSHO[@]}" root@$DSTCMP "docker exec nova_libvirt virsh dumpxml $DOM")
DROOT=$(dst server volume list $VM -f json | jq -r '.[] | select(.Device=="/dev/vda") | ."Volume ID"')
DDATA=$(dst server volume list $VM -f json | jq -r '.[] | select(.Device=="/dev/vdb") | ."Volume ID"')
assert "libvirt root disk uses rbd" "grep -q \"protocol='rbd' name='mig-volumes/volume-$DROOT'\" <<<\"\$XML\""
assert "libvirt data disk uses rbd" "grep -q \"protocol='rbd' name='mig-volumes/volume-$DDATA'\" <<<\"\$XML\""
assert "image property copied to destination volume" "dst volume show $DROOT -f json | jq -e '.volume_image_metadata.hw_qemu_guest_agent==\"yes\"'"
assert "property effective in libvirt" "grep -q 'org.qemu.guest_agent' <<<\"\$XML\""
say "INFO  RBD usage: $(rbd --id mig-xfer -p mig-volumes du volume-$DROOT 2>/dev/null | tail -1 | tr -s ' ')"

say "=== source preserved"
assert "source SHUTOFF and locked" "src server show $VM -f json | jq -e '.status==\"SHUTOFF\" and .locked==true'"
assert "source volumes still in-use" "[ \"\$(src volume show demo-root -f value -c status)\" = in-use ]"
assert "source port detached but still allocated" "src port show demo-port -f json | jq -e '.admin_state_up==true and .device_id==\"\"'"
assert "source VM has no NIC with this MAC" "[ \"\$(src_nics)\" = 0 ]"
assert "no clone left" "! src volume list -f value -c Name | grep -q mig-clone"
assert "no iSCSI session on transfer host" "! iscsiadm -m session"

say "=== mixed-phase network: peer VM still on source cloud"
PEER=mig-peer
src server show $PEER >/dev/null 2>&1 || src server create $PEER --image noble --flavor mig.small \
  --network vlan120 --security-group mig-vm-sg --key-name mig-key --wait -f value -c id >/dev/null
PEERIP=$(src server show $PEER -f json | jq -r '.addresses.vlan120[0]')
say "INFO  peer on source cloud got $PEERIP from its own pool"
pssh() { ssh "${SSHO[@]}" "ubuntu@$PEERIP" "$@"; }
for t in $(seq 1 60); do pssh true 2>/dev/null && break; sleep 5; done
assert "peer got its address by DHCP from the source cloud" "pssh 'ip -4 a | grep -q $PEERIP/24 && journalctl -q -b -u systemd-networkd | grep -q \"DHCPv4 address\" || sudo journalctl -q -b -u systemd-networkd | grep -q \"DHCPv4 address\"'"
assert "peer (source cloud) reaches migrated VM (destination cloud)" "pssh 'ping -c3 -W2 $IP'"
assert "migrated VM reaches peer" "vssh 'ping -c3 -W2 $PEERIP'"
assert "peer sees the migrated VM's MAC" "pssh \"ping -c1 -W2 $IP >/dev/null; ip neigh show $IP | grep -q $MAC\""
REPLIES=$(arping -c4 -w6 -I eth1.120 $IP 2>/dev/null | grep -c 'bytes from')
say "INFO  arping replies for 4 requests: $REPLIES"
assert "exactly one ARP answer per request" "[ '$REPLIES' = 4 ]"

say "=== reboot on destination: fresh DHCP"
BOOT0=$(vssh 'cat /proc/sys/kernel/random/boot_id')
dst server reboot --hard $VM; sleep 15; wait_ssh
assert "guest really rebooted" "[ \"\$(vssh 'cat /proc/sys/kernel/random/boot_id')\" != '$BOOT0' ]"
assert "DHCP from destination cloud after reboot" "vssh 'sudo journalctl -q -b -u systemd-networkd | grep -q \"DHCPv4 address $IP/24\"'"
assert "data checksum after reboot" "[ \"\$(datasum)\" = '$SUM' ]"
assert "SSH host key kept after reboot" "[ \"\$(hostkey)\" = '$KEY' ]"

say "=== admin powers on the locked source while the destination VM runs"
OUT=$(src server start $VM 2>&1); RC=$?
for t in $(seq 1 20); do [ "$(src server show $VM -f value -c status)" = ACTIVE ] && break; sleep 3; done
ST=$(src server show $VM -f value -c status)
say "INFO  start as admin: rc=$RC status=$ST ${OUT:0:110}"
if [ "$ST" = ACTIVE ]; then
  sleep 45   # let the source guest boot far enough to bring its NIC up and answer ARP
  REPLIES=$(arping -c4 -w6 -I eth1.120 $IP 2>/dev/null | grep -c 'bytes from')
  say "INFO  arping replies with both VMs powered on: $REPLIES"
  assert "still one ARP answer per request" "[ '$REPLIES' = 4 ]"
  assert "SSH still lands on the destination VM" "[ \"\$(vssh 'cat /var/lib/cloud/data/instance-id')\" = \"\$(dst server show $VM -f value -c id)\" ]"
  assert "powered-on source has no NIC on the wire" "[ \"\$(src_nics)\" = 0 ]"
  assert "peer still reaches the destination VM" "pssh 'ping -c3 -W2 $IP'"
  src server stop $VM; for t in $(seq 1 40); do [ "$(src server show $VM -f value -c status)" = SHUTOFF ] && break; sleep 3; done
fi
assert "source SHUTOFF again before continuing" "[ \"\$(src server show $VM -f value -c status)\" = SHUTOFF ]"
wait_ssh

say "=== rollback"
vssh 'sudo touch /root/written-on-destination; sync'
migrate-vm.sh rollback $VM 2>&1 | tee -a "$REPORT"
sleep 10; wait_ssh && say "PASS  source reachable after rollback" || say "FAIL  source after rollback"
assert "running on the original instance" "[ \"\$(vssh 'sudo cat /sys/class/dmi/id/product_uuid' | tr A-Z a-z)\" = '$IID' ]"
assert "destination-only write absent" "! vssh 'sudo test -f /root/written-on-destination'"
assert "data checksum on source" "[ \"\$(datasum)\" = '$SUM' ]"
assert "destination server removed" "! dst server show $VM"
assert "destination volumes removed" "! dst volume list -f value -c Name | grep -q demo-"
assert "destination port removed" "[ -z \"\$(dst port list --mac-address $MAC -f value -c ID)\" ]"
assert "RBD pool empty" "[ -z \"\$(rbd --id mig-xfer -p mig-volumes ls)\" ]"
assert "source unlocked" "src server show $VM -f json | jq -e '.locked==false'"
assert "source port reattached" "[ \"\$(src port show demo-port -f value -c device_id)\" = '$IID' ]"
assert "same interface, MAC and IP after reattach" "vssh \"grep -qx $MAC /sys/class/net/$IFACE/address && ip -4 a show $IFACE | grep -q $IP/24\""
assert "peer reaches the restored source VM" "pssh 'ping -c3 -W2 $IP'"

say "=== summary"
say "PASS=$(grep -c '^PASS' "$REPORT") FAIL=$(grep -c '^FAIL' "$REPORT") WARN=$(grep -c '^WARN' "$REPORT")"
