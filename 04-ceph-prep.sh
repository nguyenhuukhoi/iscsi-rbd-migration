#!/usr/bin/env bash
# Run from the workstation in Lab/. Creates pools and scoped users on the Ceph lab, then streams
# the keyrings from ceph1 straight to mig-host. Keys never land on the workstation or in output.
set -euo pipefail
O="-i key.txt -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o BatchMode=yes"
CEPH=root@192.0.2.114
MIG=root@192.0.2.109
DST=/opt/mig/dst/config

ssh $O $CEPH bash -s <<'EOF'
set -e
for p in mig-volumes mig-vms; do
  ceph osd pool ls | grep -qx "$p" || ceph osd pool create "$p" 32
  ceph osd pool application enable "$p" rbd >/dev/null 2>&1 || true
  rbd pool init "$p" 2>/dev/null || true
done
# Tentacle 20.2.5 issues aes256k keys ("Ag...") and refuses the old aes type. Clients must be
# >= 20.2.4: mig-host uses the Tentacle repo, dst images are patched by 05-patch-ceph-images.sh.
ceph auth get-or-create client.mig-cinder mon 'profile rbd' \
  osd 'profile rbd pool=mig-volumes, profile rbd pool=mig-vms' >/dev/null
ceph auth get-or-create client.mig-xfer mon 'profile rbd' \
  osd 'profile rbd pool=mig-volumes' >/dev/null
ceph osd pool ls | grep mig-
EOF

ssh -n $O $MIG "mkdir -p $DST/cinder/cinder-volume $DST/nova /etc/ceph"
# Kolla's merge_configs rejects the tab indentation that ceph generates.
ssh -n $O $CEPH 'ceph config generate-minimal-conf' | sed 's/^[[:space:]]*//' | ssh $O $MIG \
  "tee $DST/cinder/ceph.conf $DST/nova/ceph.conf /etc/ceph/ceph.conf >/dev/null"
ssh -n $O $CEPH 'ceph auth get client.mig-cinder 2>/dev/null' | ssh $O $MIG \
  "umask 077; tee $DST/cinder/cinder-volume/ceph.client.mig-cinder.keyring $DST/nova/ceph.client.mig-cinder.keyring >/dev/null"
ssh -n $O $CEPH 'ceph auth get client.mig-xfer 2>/dev/null' | ssh $O $MIG \
  "umask 077; cat > /etc/ceph/ceph.client.mig-xfer.keyring"

# Functional proof that the Squid client on mig-host can parse the key and write to the pool.
ssh -n $O $MIG 'rbd --id mig-xfer -p mig-volumes create probe --size 16 && rbd --id mig-xfer -p mig-volumes ls && rbd --id mig-xfer -p mig-volumes rm probe --no-progress && echo RBD-OK; if rbd --id mig-xfer -p mig-vms ls >/dev/null 2>&1; then echo "SCOPE-WARN: reads mig-vms"; else echo SCOPE-OK; fi'
