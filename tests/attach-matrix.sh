#!/usr/bin/env bash
# Run on mig-host as root. Records which API paths let the transfer host read a boot volume of a
# stopped VM. Read-only for the source data: nothing writes to the volumes under test.
set -uo pipefail
source /opt/mig-venv/bin/activate
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
source /opt/mig/src/admin-openrc.sh
export OS_VOLUME_API_VERSION=3.54
systemctl start iscsid

ROOT=$(openstack volume show demo-root -f value -c id)
echo "== stop VM"
openstack server stop demo-vm
until [ "$(openstack server show demo-vm -f value -c status)" = SHUTOFF ]; do sleep 3; done
openstack volume show demo-root -f value -c status -c attachments | cut -c1-120

echo "== T1 connector of this host"
cinder get-connector 2>&1 | grep -E "initiator|ip |host |multipath" | cut -c1-90

echo "== T2 detach root volume from the stopped server through Nova"
openstack server remove volume demo-vm demo-root 2>&1 | cut -c1-200; echo "rc=${PIPESTATUS[0]}"

echo "== T3 second attachment on the in-use root volume"
cinder local-attach "$ROOT" 2>&1 | tail -3 | cut -c1-220; echo "rc=${PIPESTATUS[0]}"

echo "== T4 clone the in-use root volume, then attach the clone"
START=$(date +%s)
openstack volume create demo-root-clone --source demo-root -f value -c id
while s=$(openstack volume show demo-root-clone -f value -c status); [ "$s" != available ]; do
  [ "$s" = error ] && { echo "clone error"; break; }; sleep 2; done
echo "clone status=$s seconds=$(( $(date +%s)-START ))"
CL=$(openstack volume show demo-root-clone -f value -c id)
cinder local-attach "$CL" 2>&1 | tail -8 | cut -c1-170; echo "attach rc=${PIPESTATUS[0]}"
sleep 2
iscsiadm -m session 2>&1 | cut -c1-110
lsblk -dn -o NAME,SIZE,TRAN,MODEL | grep -v vd
echo "volume status: $(openstack volume show demo-root-clone -f value -c status)"
echo "source status: $(openstack volume show demo-root -f value -c status)"
