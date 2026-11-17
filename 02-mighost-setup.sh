#!/usr/bin/env bash
# Run on mig-host (192.0.2.109) as root. Installs Kolla-Ansible 2026.1 (deploy role) and the
# migration toolchain (transfer role). Expects the source tree at /opt/kolla-ansible-src.
set -euo pipefail
export http_proxy=http://proxy.example.com:80 https_proxy=http://proxy.example.com:80
export no_proxy=localhost,127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16

retry() { local n=0; until "$@"; do n=$((n+1)); [ $n -ge 6 ] && return 1; sleep 10; done; }

# Ceph client from the Tentacle repo: Tentacle >= 20.2.4 issues aes256k keys ("Ag...") which the
# Squid 19.2.3 client in Ubuntu Noble cannot parse.
curl -fsSL --retry 5 https://download.ceph.com/keys/release.asc -o /usr/share/keyrings/ceph-release.asc
echo "deb [signed-by=/usr/share/keyrings/ceph-release.asc] https://download.ceph.com/debian-tentacle noble main" \
  > /etc/apt/sources.list.d/ceph-tentacle.list

retry apt-get update -qq
retry apt-get install -y -qq python3-venv python3-dev gcc libffi-dev libssl-dev git jq arping \
  open-iscsi qemu-utils qemu-block-extra ceph-common

# Deploy role: isolated venv, same versions as the tested /opt/venv-2026.1 on .79.
[ -x /opt/kolla-venv/bin/python ] || python3 -m venv /opt/kolla-venv
retry /opt/kolla-venv/bin/pip install -q -U pip
retry /opt/kolla-venv/bin/pip install -q 'ansible-core==2.20.10' /opt/kolla-ansible-src
# install-deps calls ansible-galaxy by name, so the venv must be on PATH.
PATH=/opt/kolla-venv/bin:$PATH retry /opt/kolla-venv/bin/kolla-ansible install-deps

# Transfer role: OpenStack clients plus os-brick local attach.
[ -x /opt/mig-venv/bin/python ] || python3 -m venv /opt/mig-venv
retry /opt/mig-venv/bin/pip install -q -U pip
retry /opt/mig-venv/bin/pip install -q python-openstackclient python-cinderclient \
  python-brick-cinderclient-ext os-brick

/opt/kolla-venv/bin/pip list 2>/dev/null | grep -E '^(kolla-ansible|ansible-core) '
/opt/mig-venv/bin/pip list 2>/dev/null | grep -E '^(python-openstackclient|python-cinderclient|python-brick-cinderclient-ext|os-brick) '
rbd --version
echo SETUP-DONE
