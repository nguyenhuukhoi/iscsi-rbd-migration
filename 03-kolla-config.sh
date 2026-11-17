#!/usr/bin/env bash
# Run on mig-host as root from the directory holding kolla/. Builds /opt/mig/{src,dst}.
# Keeps generated passwords on re-run. Ceph material is installed by 04-ceph-prep.sh.
set -euo pipefail
cd "$(dirname "$0")"
K=/opt/kolla-venv
SHARE=$K/share/kolla-ansible

for c in src dst; do
  d=/opt/mig/$c
  mkdir -p "$d/config"
  chmod 700 "$d"
  cat kolla/common-globals.yml <(grep -v '^---' "kolla/$c-globals.yml") > "$d/globals.yml"
  # Group definitions come from the matching kolla-ansible release.
  { cat "kolla/$c-hosts.ini"; sed -n '/^\[baremetal:children\]/,$p' "$SHARE/ansible/inventory/multinode"; } > "$d/hosts"
  if [ ! -f "$d/passwords.yml" ]; then
    cp "$SHARE/etc_examples/kolla/passwords.yml" "$d/passwords.yml"
    "$K/bin/kolla-genpwd" -p "$d/passwords.yml"
  fi
  chmod 600 "$d/passwords.yml"
done

# Source: publish iSCSI targets on the dedicated fabric, not on the API network.
# Kolla does not template target_ip_address, so Cinder would default to my_ip (eth0).
mkdir -p /opt/mig/src/config/cinder
cat > /opt/mig/src/config/cinder/cinder-volume.conf <<'EOF'
[lvm-1]
target_ip_address = 10.77.0.12
target_secondary_ip_addresses = 10.77.0.13
EOF

# Neutron on both clouds: provider MTU must fit the 1442-byte outer overlay.
for c in src dst; do
  mkdir -p /opt/mig/$c/config/neutron
  cat > /opt/mig/$c/config/neutron/ml2_conf.ini <<'EOF'
[ml2]
physical_network_mtus = physnet1:1442

# Kolla leaves this empty unless Ironic is enabled, which blocks VLAN provider networks.
[ml2_type_vlan]
network_vlan_ranges = physnet1
EOF
done
echo CONFIG-DONE
