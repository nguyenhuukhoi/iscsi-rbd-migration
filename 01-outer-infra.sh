#!/usr/bin/env bash
# Run on the outer cloud deploy node (198.51.100.79) as root: sudo bash 01-outer-infra.sh
# Creates the isolated networks, ports and 6 VMs for the iSCSI -> RBD migration lab.
# Idempotent. Only creates resources named mig-*; never touches other lab resources.
set -euo pipefail
source /etc/kolla/admin-openrc.sh
export no_proxy="${no_proxy:-},198.51.100.100"
cd "$(dirname "$0")"

MGMT_NET=vlan2019
MGMT_SUBNET=vlan2019-subnet
PROV_NET=mig-provider-net   # carries tagged frames between the two nested clouds
ISCSI_NET=mig-iscsi-net     # dedicated iSCSI fabric of the source cloud
SG=mig-lab-sg
IMAGE=ubuntu-noble-24.04
KEY=lab-stack-key
STOR_DISK_GB=100

# name|flavor|mgmt_ip|has_provider|iscsi_ip
NODES=(
  "mig-src-ctl|lab.large|192.0.2.103|yes|"
  "mig-src-cmp|lab.large|192.0.2.104|yes|10.77.0.11"
  "mig-src-stor|lab.medium|192.0.2.106|no|10.77.0.12"
  "mig-dst-ctl|lab.large|192.0.2.107|yes|"
  "mig-dst-cmp|lab.large|192.0.2.108|yes|"
  "mig-host|lab.medium|192.0.2.109|yes|10.77.0.20"
)

# --- networks ---------------------------------------------------------------
# vlan-transparent is required, otherwise OVN drops the 802.1Q frames of the nested
# provider network. Subnets exist only because Nova refuses a port without a fixed IP:
# no DHCP, no gateway, and the guest does not configure the address on eth1.
ensure_net() {  # name cidr
  openstack network show "$1" >/dev/null 2>&1 || \
    openstack network create "$1" --transparent-vlan --disable-port-security -f value -c id
  openstack subnet show "$1-subnet" >/dev/null 2>&1 || \
    openstack subnet create "$1-subnet" --network "$1" --subnet-range "$2" \
      --no-dhcp --gateway none -f value -c id
}
ensure_net "$PROV_NET" 10.78.0.0/24
ensure_net "$ISCSI_NET" 10.77.0.0/24

# --- security group for the management NIC ----------------------------------
if ! openstack security group show "$SG" >/dev/null 2>&1; then
  openstack security group create "$SG" --description "iSCSI->RBD migration lab: RFC1918 only"
  for cidr in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16; do
    openstack security group rule create "$SG" --ingress --protocol any --remote-ip "$cidr"
  done
fi

ensure_port() {  # name network [extra args...]
  local name=$1 net=$2; shift 2
  openstack port show "$name" >/dev/null 2>&1 || \
    openstack port create "$name" --network "$net" "$@" -f value -c id >/dev/null
}
mac_of() { openstack port show "$1" -f value -c mac_address; }

render_user_data() {  # host mgmt_ip mac0 mac1 mac2 iscsi_ip
  local host=$1 ip=$2 mac0=$3 mac1=$4 mac2=$5 iscsi_ip=$6
  local np
  np="network:
        version: 2
        ethernets:
          eth0:
            match: {macaddress: \"$mac0\"}
            set-name: eth0
            addresses: [$ip/24]
            routes: [{to: default, via: 192.0.2.1}]
            nameservers: {addresses: [210.245.31.136, 210.245.31.145]}"
  if [[ -n $mac1 ]]; then
    np+="
          eth1:
            match: {macaddress: \"$mac1\"}
            set-name: eth1
            dhcp4: false
            mtu: 1442"
  fi
  if [[ -n $mac2 ]]; then
    np+="
          eth2:
            match: {macaddress: \"$mac2\"}
            set-name: eth2
            addresses: [$iscsi_ip/24]
            mtu: 1442"
  fi
  sed -e "s/__HOST__/$host/g" user-data.tmpl | awk -v np="$np" '{ if ($0 ~ /__NETPLAN__/) print "      " np; else print }'
}

# --- ports + servers --------------------------------------------------------
for spec in "${NODES[@]}"; do
  IFS='|' read -r name flavor ip prov iscsi <<<"$spec"

  ensure_port "$name-mgmt" "$MGMT_NET" --fixed-ip "subnet=$MGMT_SUBNET,ip-address=$ip" --security-group "$SG"
  nics=(--port "$name-mgmt"); mac1=""; mac2=""
  if [[ $prov == yes ]]; then
    ensure_port "$name-prov" "$PROV_NET" --disable-port-security
    # A port created before the subnet existed has no address; Nova would reject it.
    [[ $(openstack port show "$name-prov" -f json -c fixed_ips | grep -c ip_address) -gt 0 ]] || \
      openstack port set "$name-prov" --fixed-ip "subnet=$PROV_NET-subnet"
    nics+=(--port "$name-prov"); mac1=$(mac_of "$name-prov")
  fi
  if [[ -n $iscsi ]]; then
    ensure_port "$name-iscsi" "$ISCSI_NET" --disable-port-security \
      --fixed-ip "subnet=$ISCSI_NET-subnet,ip-address=$iscsi"
    nics+=(--port "$name-iscsi"); mac2=$(mac_of "$name-iscsi")
  fi

  openstack server show "$name" >/dev/null 2>&1 && continue
  ud=$(mktemp)
  render_user_data "$name" "$ip" "$(mac_of "$name-mgmt")" "$mac1" "$mac2" "$iscsi" >"$ud"
  openstack server create "$name" --image "$IMAGE" --flavor "$flavor" "${nics[@]}" \
    --key-name "$KEY" --user-data "$ud" -f value -c id
  rm -f "$ud"
done

# --- LVM disk for the source storage node (VG cinder-volumes) ----------------
vol=mig-src-stor-lvm
openstack volume show "$vol" >/dev/null 2>&1 || openstack volume create "$vol" --size "$STOR_DISK_GB" -f value -c id
until [[ $(openstack server show mig-src-stor -f value -c status) == ACTIVE ]]; do sleep 5; done
if [[ $(openstack volume show "$vol" -f value -c status) != in-use ]]; then
  until [[ $(openstack volume show "$vol" -f value -c status) == available ]]; do sleep 3; done
  openstack server add volume mig-src-stor "$vol" >/dev/null
fi

openstack server list --name '^mig-' -c Name -c Status -c Networks -c Host
