#!/usr/bin/env bash
# Lab orchestrator for the iSCSI -> Ceph RBD migration lab (two nested Kolla 2026.1 clouds).
# Run from the Lab/ directory on the workstation (Git Bash). IPs: ../system-info.md section 9.
#
#   lab.sh status      health of the 6 lab VMs, both cloud APIs, Ceph access, demo-vm location
#   lab.sh up          build the lab in GUIDE.md section 5 order (01 -> 06); stops at manual steps
#   lab.sh demo-reset  put demo-vm back ACTIVE on the source cloud (rollback if migrate state exists)
#
# "up" replays the 2026-11-05 build notes and has NEVER been replayed from a blank VM; treat it as
# a guided script, not a tested one. "status" and "demo-reset" are the everyday, tested paths.
set -euo pipefail
cd "$(dirname "$0")/.."    # Lab/

KEY=${KEY:-key.txt}
MIG=root@192.0.2.109
OUTER=ubuntu@198.51.100.79
S="ssh -i $KEY -o StrictHostKeyChecking=no -o ConnectTimeout=10 -o LogLevel=ERROR"
say() { echo "== $*"; }
ask() { read -rp ">> $1 [enter to continue, ctrl-c to abort] " _; }

cmd_status() {
  say "transfer host / deploy node (mig-host .109)"
  $S $MIG 'hostname; uptime -p'

  say "lab nodes reachable from mig-host (ICMP)"
  $S $MIG 'for ip in 103 104 106 107 108; do
             ping -c1 -W2 192.0.2.$ip >/dev/null && echo "  192.0.2.$ip OK" || echo "  192.0.2.$ip UNREACHABLE"
           done'

  say "cloud APIs and services"
  $S $MIG 'source /opt/mig-venv/bin/activate; unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
           for c in src dst; do
             echo "-- $c"
             ( source /opt/mig/$c/admin-openrc.sh
               openstack volume service list -f value -c Binary -c Host -c State
               openstack compute service list -f value -c Binary -c Host -c State ) || echo "  $c API FAILED"
           done'

  say "Ceph access from the transfer host"
  $S $MIG 'rbd --id mig-xfer -p mig-volumes ls >/dev/null && echo "  rbd pool mig-volumes OK" || echo "  rbd FAILED"
           python3 -c "import rados, rbd" 2>/dev/null && echo "  python3 rbd bindings OK" || echo "  python3-rbd MISSING (needed for --mode live)"'

  say "demo-vm location"
  $S $MIG 'source /opt/mig-venv/bin/activate; unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
           for c in src dst; do
             ( source /opt/mig/$c/admin-openrc.sh
               st=$(openstack server show demo-vm -f value -c status 2>/dev/null) && echo "  $c: demo-vm $st" || echo "  $c: no demo-vm"
             )
           done
           [ -d /var/lib/mig-migrate/demo-vm ] && echo "  migrate state PRESENT (/var/lib/mig-migrate/demo-vm)" || echo "  no migrate state"'
}

cmd_demo_reset() {
  $S $MIG 'set -e
           if [ -d /var/lib/mig-migrate/demo-vm ]; then
             echo "migrate state found: rolling back"
             migrate-vm.sh rollback demo-vm
           else
             source /opt/mig-venv/bin/activate; unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
             source /opt/mig/src/admin-openrc.sh
             openstack server unlock demo-vm || true
             [ "$(openstack server show demo-vm -f value -c status)" = ACTIVE ] || openstack server start demo-vm
             until [ "$(openstack server show demo-vm -f value -c status)" = ACTIVE ]; do sleep 3; done
             echo "demo-vm ACTIVE on source"
           fi'
}

cmd_up() {
  cat <<'EOF'
This replays the 2026-11-05 build (GUIDE.md section 5). It was recorded from a real build where
errors were fixed along the way and has not been replayed from scratch. Watch every step.
Prerequisites: 20 free cores / 40 GB RAM quota on the outer cloud, 6 free IPs in vlan2019.
EOF
  ask "step 1/6: create the 6 outer VMs via $OUTER"
  scp -i "$KEY" iscsi-rbd-migration/{01-outer-infra.sh,user-data.tmpl} "$OUTER":mig-lab/
  $S $OUTER 'sudo bash ~/mig-lab/01-outer-infra.sh'

  ask "step 2/6: transfer-host setup on $MIG (copies the verified Kolla source from .79)"
  $S $OUTER 'sudo tar -C /opt/src-kolla-ansible-stable-2026.1-20261013 -czf - .' \
    | $S $MIG 'mkdir -p /opt/kolla-ansible-src && tar -C /opt/kolla-ansible-src -xzf -'
  scp -i "$KEY" iscsi-rbd-migration/02-mighost-setup.sh "$MIG":/root/
  $S $MIG 'bash /root/02-mighost-setup.sh'
  cat <<'EOF'
MANUAL (see GUIDE 5.2): on mig-host create an ssh key, push it to the 5 nodes' authorized_keys,
install the Harbor CA on the 5 nodes, and create the /usr/local/bin/ka wrapper.
EOF
  ask "manual 5.2 done"

  ask "step 3/6: Kolla config + Ceph pool/user"
  $S $MIG 'mkdir -p /root/mig-lab'
  scp -r -i "$KEY" iscsi-rbd-migration/kolla iscsi-rbd-migration/03-kolla-config.sh "$MIG":/root/mig-lab/
  $S $MIG 'bash /root/mig-lab/03-kolla-config.sh'
  bash iscsi-rbd-migration/04-ceph-prep.sh

  ask "step 4/6: bootstrap-servers on both clouds"
  $S $MIG 'bash -lc "ka src bootstrap-servers && ka dst bootstrap-servers"'
  cat <<'EOF'
MANUAL (see GUIDE 5.4): docker proxy + quay.io pulls for iscsid/tgtd on .104/.106,
05-patch-ceph-images.sh on .107/.108, pvcreate/vgcreate on .106, disable host iscsid on .104/.106.
Never run "ka dst pull": it overwrites the patched Ceph-client images.
EOF
  ask "manual 5.4 done"

  ask "step 5/6: deploy both clouds + prepare networks, flavor, demo VM"
  scp -i "$KEY" iscsi-rbd-migration/06-prepare-clouds.sh "$MIG":/root/mig-lab/
  $S $MIG 'bash -lc "for c in src dst; do ka \$c prechecks && ka \$c deploy && ka \$c post-deploy; done"'
  $S $MIG 'bash /root/mig-lab/06-prepare-clouds.sh'

  ask "step 6/6: install the migration tool and tests"
  # Bash build (archive/ since v7): the parity reference for the Python app, called by the original suites.
  scp -i "$KEY" iscsi-rbd-migration/archive/{migrate-vm.sh,brick_attach.py,blocksync.py} "$MIG":/usr/local/bin/
  $S $MIG 'chmod +x /usr/local/bin/{migrate-vm.sh,brick_attach.py,blocksync.py}; mkdir -p /root/mig-lab/tests'
  # Python app 'migrate' (v7+) into /opt/mig-venv, created in step 2 by 02-mighost-setup.sh (DEPLOY-PYTHON.md
  # section 4). A fresh source dir each time: a stale build/ or a removed module must not end up in the wheel.
  # README.md goes along because pyproject.toml names it as the package readme.
  $S $MIG 'rm -rf /root/migrate-py && mkdir -p /root/migrate-py'
  scp -r -i "$KEY" iscsi-rbd-migration/{pyproject.toml,README.md,migratetool} "$MIG":/root/migrate-py/
  $S $MIG '/opt/mig-venv/bin/pip install -q --no-build-isolation /root/migrate-py && /opt/mig-venv/bin/migrate --version'
  scp -i "$KEY" iscsi-rbd-migration/tests/*.sh "$MIG":/root/mig-lab/tests/
  $S $MIG 'apt-get install -y python3-rbd >/dev/null && python3 -c "import rados, rbd" && echo "python3-rbd OK"'
  echo "Lab up. Verify with: lab.sh status"
}

case ${1:-} in
  status) cmd_status;;
  up) cmd_up;;
  demo-reset) cmd_demo_reset;;
  *) sed -n '2,12p' "$0"; exit 2;;
esac
