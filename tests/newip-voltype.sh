#!/usr/bin/env bash
# e2e on mig-host for --ip new + --volume-type + --start-source (cold mode): demo-vm starts ACTIVE
# on the source. After the migrate, both VMs run at once on different IPs and MACs, the destination
# volumes carry the chosen type, and the source ports were never detached. Ends with a rollback.
set -uo pipefail
source /opt/mig-venv/bin/activate
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
VM=demo-vm; OLDIP=192.168.120.50; IFACE=ens3; STATE=/var/lib/mig-migrate/$VM
VTYPE=mig-typeb
REPORT=/root/newip-report.txt; : > "$REPORT"
SSHO=(-n -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 -o LogLevel=ERROR)
src() { ( source /opt/mig/src/admin-openrc.sh; openstack "$@" ); }
dst() { ( source /opt/mig/dst/admin-openrc.sh; openstack "$@" ); }
say() { echo "$*" | tee -a "$REPORT"; }
assert() { if eval "$2" >/dev/null 2>&1; then say "PASS  $1"; else say "FAIL  $1"; fi; }
ossh() { ssh "${SSHO[@]}" "ubuntu@$OLDIP" "$@"; }

say "=== baseline"
assert "no leftover migrate state" "[ ! -d $STATE ]"
[ -d "$STATE" ] && exit 1
for t in $(seq 1 30); do ossh true 2>/dev/null && break; sleep 3; done
ossh true || { say "ABORT: source VM unreachable at $OLDIP"; exit 1; }
SUM=$(ossh 'sudo sha256sum /data/random.bin | cut -d" " -f1')
SRCID=$(src server show $VM -f value -c id)
OLDMAC=$(ossh "cat /sys/class/net/$IFACE/address")
[ -n "$SUM" ] && [ -n "$OLDMAC" ] || { say "ABORT: empty baseline"; exit 1; }
dst volume type show $VTYPE >/dev/null 2>&1 || dst volume type create $VTYPE >/dev/null
assert "volume type $VTYPE exists on destination" "dst volume type show $VTYPE"

say "=== migrate --ip new --volume-type $VTYPE --start-source"
migrate-vm.sh migrate $VM --ip new --volume-type $VTYPE --start-source 2>&1 | grep -vE '^  OK ' | tee -a "$REPORT"; RC=${PIPESTATUS[0]}
assert "migrate rc=0" "[ $RC = 0 ]"
[ "$RC" = 0 ] || { say "ABORT: migrate failed"; exit 1; }

say "=== destination got a new identity on the wire"
NEWIP=$(dst port show mig-$VM-0 -f json | jq -r '.fixed_ips[0].ip_address')
NEWMAC=$(dst port show mig-$VM-0 -f json | jq -r '.mac_address')
say "INFO  destination $NEWIP $NEWMAC (source kept $OLDIP $OLDMAC)"
assert "new IP differs from the source IP" "[ -n '$NEWIP' ] && [ '$NEWIP' != '$OLDIP' ]"
assert "new IP from the destination pool (.100-.199)" "awk -F. '{exit !(\$4 >= 100 && \$4 <= 199)}' <<<'$NEWIP'"
assert "new MAC differs" "[ -n '$NEWMAC' ] && [ '$NEWMAC' != '$OLDMAC' ]"
nssh() { ssh "${SSHO[@]}" "ubuntu@$NEWIP" "$@"; }
for t in $(seq 1 60); do nssh true 2>/dev/null && break; sleep 3; done
assert "destination VM reachable on the new IP" "nssh true"
assert "destination VM is the destination instance" "[ \"\$(nssh 'cat /var/lib/cloud/data/instance-id')\" = \"\$(dst server show $VM -f value -c id)\" ]"
assert "data checksum on destination" "[ \"\$(nssh 'sudo sha256sum /data/random.bin | cut -d\" \" -f1')\" = '$SUM' ]"
assert "hostname preserved" "[ \"\$(nssh hostname)\" = '$VM' ]"

say "=== destination volumes carry the chosen type"
while read -r svid dvid; do
  assert "volume $dvid type is $VTYPE" "[ \"\$(dst volume show $dvid -f value -c type)\" = '$VTYPE' ]"
done < "$STATE/volume-map"

say "=== source untouched on the network and running again (--start-source)"
assert "source port still attached to the source VM" "[ \"\$(src port show demo-port -f value -c device_id)\" = '$SRCID' ]"
assert "source VM ACTIVE again" "[ \"\$(src server show $VM -f value -c status)\" = ACTIVE ]"
assert "source VM unlocked" "src server show $VM -f json | jq -e '.locked==false'"
for t in $(seq 1 60); do ossh true 2>/dev/null && break; sleep 3; done
assert "source VM reachable on the old IP" "ossh true"
assert "old IP really is the source instance" "[ \"\$(ossh 'cat /var/lib/cloud/data/instance-id')\" = '$SRCID' ]"
assert "both VMs see each other (old -> new)" "ossh 'ping -c3 -W2 $NEWIP'"
assert "both VMs see each other (new -> old)" "nssh 'ping -c3 -W2 $OLDIP'"

say "=== rollback removes the destination and keeps the source running"
migrate-vm.sh rollback $VM 2>&1 | tee -a "$REPORT"
assert "destination server removed" "! dst server show $VM"
assert "destination volumes removed" "! dst volume list -f value -c Name | grep -q demo-"
assert "destination port removed" "! dst port show mig-$VM-0"
assert "RBD pool empty" "[ -z \"\$(rbd --id mig-xfer -p mig-volumes ls)\" ]"
assert "source still ACTIVE" "[ \"\$(src server show $VM -f value -c status)\" = ACTIVE ]"
for t in $(seq 1 30); do ossh true 2>/dev/null && break; sleep 3; done
assert "source reachable after rollback" "ossh true"
assert "data checksum on source" "[ \"\$(ossh 'sudo sha256sum /data/random.bin | cut -d\" \" -f1')\" = '$SUM' ]"

say "=== summary"
say "PASS=$(grep -c '^PASS' "$REPORT") FAIL=$(grep -c '^FAIL' "$REPORT")"
