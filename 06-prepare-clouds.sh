#!/usr/bin/env bash
# Run on mig-host as root. Creates identical provider network, flavor and security group on both
# nested clouds, and the sample VM on the source cloud. Idempotent.
set -euo pipefail
source /opt/mig-venv/bin/activate
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY

IMG=/root/noble-server-cloudimg-amd64.img
[ -s "$IMG" ] || curl -fsSL -x http://proxy.example.com:80 --retry 5 -o "$IMG" \
  https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img

# Same L2 for both clouds. Allocation pools do not overlap, so the per-cloud OVN metadata ports
# and new VMs can never collide; migrated VMs keep their address through an explicit fixed IP.
declare -A POOL=([src]="192.168.120.10,192.168.120.99" [dst]="192.168.120.100,192.168.120.199")

for c in src dst; do
  (
    source /opt/mig/$c/admin-openrc.sh
    export OS_CLOUD=
    openstack network show vlan120 >/dev/null 2>&1 || openstack network create vlan120 \
      --share --external --provider-network-type vlan --provider-physical-network physnet1 \
      --provider-segment 120 --mtu 1400 -f value -c id
    openstack subnet show vlan120-subnet >/dev/null 2>&1 || {
      IFS=, read -r start end <<<"${POOL[$c]}"
      openstack subnet create vlan120-subnet --network vlan120 --subnet-range 192.168.120.0/24 \
        --gateway 192.168.120.1 --allocation-pool "start=$start,end=$end" --dhcp -f value -c id
    }
    openstack flavor show mig.small >/dev/null 2>&1 || \
      openstack flavor create mig.small --vcpus 1 --ram 1024 --disk 10 -f value -c id
    openstack security group show mig-vm-sg >/dev/null 2>&1 || {
      openstack security group create mig-vm-sg -f value -c id
      openstack security group rule create mig-vm-sg --ingress --protocol icmp -f value -c id
      openstack security group rule create mig-vm-sg --ingress --protocol tcp --dst-port 22 -f value -c id
      openstack security group rule create mig-vm-sg --ingress --protocol tcp --dst-port 8080 -f value -c id
    }
    openstack keypair show mig-key >/dev/null 2>&1 || \
      openstack keypair create mig-key --public-key /root/.ssh/id_ed25519.pub -f value -c fingerprint
    openstack image show noble >/dev/null 2>&1 || openstack image create noble \
      --disk-format qcow2 --container-format bare --public --file "$IMG" -f value -c id
    echo "== $c services"
    openstack compute service list -f value -c Binary -c Host -c State
    openstack volume service list -f value -c Binary -c Host -c State
    openstack network agent list -f value -c "Agent Type" -c Host -c Alive
  )
done

# Gateway for vlan120 on mig-host; also proves tagged traffic reaches the guests.
ip link show eth1.120 >/dev/null 2>&1 || {
  ip link add link eth1 name eth1.120 type vlan id 120
  ip link set eth1.120 mtu 1400 up
  ip addr add 192.168.120.1/24 dev eth1.120
}

# --- sample VM on the source cloud ------------------------------------------
source /opt/mig/src/admin-openrc.sh
wait_vol() { until [ "$(openstack volume show "$1" -f value -c status)" = available ]; do sleep 5; done; }

openstack volume show demo-root >/dev/null 2>&1 || \
  openstack volume create demo-root --image noble --size 10 --bootable -f value -c id
wait_vol_or_inuse() { local s; while s=$(openstack volume show "$1" -f value -c status); [ "$s" != available ] && [ "$s" != in-use ]; do
  [ "$s" = error ] && { echo "volume $1 error" >&2; exit 1; }; sleep 5; done; }
wait_vol_or_inuse demo-root
openstack volume show demo-data >/dev/null 2>&1 || openstack volume create demo-data --size 2 -f value -c id
wait_vol_or_inuse demo-data

openstack port show demo-port >/dev/null 2>&1 || openstack port create demo-port --network vlan120 \
  --fixed-ip subnet=vlan120-subnet,ip-address=192.168.120.50 --security-group mig-vm-sg -f value -c id

cat > /tmp/demo-ud.yaml <<'EOF'
#cloud-config
password: CHANGE_ME_LAB_PASSWORD
chpasswd: {expire: false}
ssh_pwauth: true
write_files:
  - path: /usr/local/bin/demo-marker.sh
    permissions: '0755'
    content: |
      #!/bin/bash
      # Writes one line per second so the last pre-shutdown write is detectable after migration.
      while true; do date +%s.%N >> /var/lib/demo-marker.log; sync /var/lib/demo-marker.log; sleep 1; done
  - path: /etc/systemd/system/demo-marker.service
    content: |
      [Unit]
      Description=migration test writer
      [Service]
      ExecStart=/usr/local/bin/demo-marker.sh
      Restart=always
      [Install]
      WantedBy=multi-user.target
runcmd:
  - [ bash, -c, "mkfs.ext4 -q -L demodata /dev/vdb && mkdir -p /data && echo 'LABEL=demodata /data ext4 defaults,nofail 0 2' >> /etc/fstab && mount /data" ]
  - [ bash, -c, "dd if=/dev/urandom of=/data/random.bin bs=1M count=300 status=none && sha256sum /data/random.bin > /data/random.sha256 && sync" ]
  - [ systemctl, daemon-reload ]
  - [ systemctl, enable, --now, demo-marker.service ]
EOF

openstack server show demo-vm >/dev/null 2>&1 || openstack server create demo-vm --flavor mig.small \
  --volume demo-root --block-device "uuid=$(openstack volume show demo-data -f value -c id),source_type=volume,destination_type=volume,device_name=vdb,boot_index=-1" \
  --port demo-port --key-name mig-key --user-data /tmp/demo-ud.yaml --wait -f value -c id

openstack server show demo-vm -f value -c status -c addresses -c OS-EXT-SRV-ATTR:host
openstack volume list --long -f value -c Name -c Status -c Size -c Type
echo PREP-DONE
