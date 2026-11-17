#!/usr/bin/env bash
# Run on a destination node as root: bash 05-patch-ceph-images.sh <image-name>...
# The 2026.1 images ship Ceph 20.2.0, which cannot parse the aes256k keys of Tentacle >= 20.2.4.
# Rebuilds the images LOCALLY under the same tag with the Ceph 20.2.5 client. Nothing is pushed to
# Harbor. "kolla-ansible pull" would restore the unpatched images, so do not run it on these nodes.
set -euo pipefail
REG=198.51.100.109/openstack.kolla
TAG=2026.1-ubuntu-noble
CEPH_VER=20.2.5-1noble
PROXY=http://proxy.example.com:80

ctx=$(mktemp -d); trap 'rm -rf "$ctx"' EXIT
curl -fsSL -x "$PROXY" https://download.ceph.com/keys/release.asc -o "$ctx/ceph-release.asc"

cat >"$ctx/patch.sh" <<EOF
set -e
echo "deb [signed-by=/usr/share/keyrings/ceph-release.asc] https://download.ceph.com/debian-tentacle noble main" \
  > /etc/apt/sources.list.d/ceph-tentacle.list
apt-get update -qq
pkgs=\$(dpkg-query -W -f '\${Package} \${Status}\n' | awk '/install ok installed/ && \$1 ~ /^(ceph|librados|librbd|libradosstriper|libcephfs|librgw|python3-(rados|rbd|rgw|cephfs|ceph))/ {print \$1"=$CEPH_VER"}')
apt-get install -y -qq --allow-downgrades \$pkgs
rm -f /etc/apt/sources.list.d/ceph-tentacle.list
rm -rf /var/lib/apt/lists/*
EOF

for name in "$@"; do
  img=$REG/$name:$TAG
  docker pull -q "$img" >/dev/null
  if docker run --rm --entrypoint rbd "$img" --version | grep -q 'version 20\.2\.5'; then
    echo "$name: already patched"; continue
  fi
  user=$(docker inspect -f '{{.Config.User}}' "$img")
  cat >"$ctx/Dockerfile" <<EOF
FROM $img
USER root
COPY ceph-release.asc /usr/share/keyrings/ceph-release.asc
COPY patch.sh /tmp/patch.sh
RUN bash /tmp/patch.sh && rm -f /tmp/patch.sh
USER ${user:-root}
EOF
  docker build -q --network host --build-arg http_proxy=$PROXY --build-arg https_proxy=$PROXY -t "$img" "$ctx"
  echo "$name: $(docker run --rm --entrypoint rbd "$img" --version)"
done
