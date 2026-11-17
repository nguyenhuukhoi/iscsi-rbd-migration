#!/usr/bin/env bash
# Migrate a boot-from-volume VM between two independent OpenStack clouds: iSCSI Cinder -> Ceph RBD.
# Run as root on the transfer host, which reaches the iSCSI fabric and the Ceph public network.
#
#   migrate-vm.sh plan     <vm> [options]  read-only: export source definition, destination preflight
#   migrate-vm.sh migrate  <vm> [options]  migrate; cold stops the VM first, live only at cutover
#   migrate-vm.sh rollback <vm>            delete destination objects, restore and start the source VM
#
# Options (migrate and plan):
#   --mode cold|live        cold (default): stop the VM, qemu-img copy + full compare (v1 behaviour).
#                           live: hash-cached pre-sync passes while the VM runs; the cutover only
#                           syncs the final delta, so downtime tracks the write rate, not the size.
#   --volume-type <type>    Cinder volume type for every destination volume (default: cloud default).
#                           RBD_POOL_MAP="typeA:poolA,typeB:poolB" maps types to RBD pools.
#   --flavor <name>         flavor for the destination VM (default: same name as the source).
#   --security-group <sgs>  comma-separated security groups for every destination port
#                           (default: the same group names the source ports carry).
#   --ip same|new|<addr>    same (default): keep MAC+IP, detach source ports at cutover.
#                           new: destination allocates a new MAC+IP, source ports stay attached.
#                           <addr>: that address with a new MAC (single-NIC VMs only).
#   --start-source          after a successful migrate, unlock and start the source VM again.
#                           Only with --ip new|<addr>; both VMs then run with the same hostname.
#   --max-passes <n>        live: maximum pre-sync passes (default 5).
#   --delta-limit <MB>      live: cut over once a pass changed at most this much (default 64).
#   --verify readback|full|none
#                           live only; cold always runs a full qemu-img compare.
#                           readback (default): re-read every written chunk. full: readback plus a
#                           full compare during cutover. none: trust the writes.
#
# Boot-from-volume VMs are copied volume by volume. A VM with an ephemeral (image-backed) root
# disk is migrated cold through a Glance snapshot: snapshot -> download -> upload -> boot; its
# attached data volumes still go the volume way. Server properties and description are copied.
# Source volumes are never modified or deleted; rollback restores everything.
# Environment overrides: SRC_RC DST_RC RBD_POOL RBD_POOL_MAP RBD_USER CEPH_CONF MIG_ISCSI_IP COPY_STREAMS
set -euo pipefail

SRC_RC=${SRC_RC:-/opt/mig/src/admin-openrc.sh}
DST_RC=${DST_RC:-/opt/mig/dst/admin-openrc.sh}
RBD_POOL=${RBD_POOL:-mig-volumes}
RBD_POOL_MAP=${RBD_POOL_MAP:-}
RBD_USER=${RBD_USER:-mig-xfer}
COPY_STREAMS=${COPY_STREAMS:-8}
export MIG_ISCSI_IP=${MIG_ISCSI_IP:-10.77.0.20}
CEPH_CONF=${CEPH_CONF:-/etc/ceph/ceph.conf}
STATE_ROOT=/var/lib/mig-migrate
BRICK=/usr/local/bin/brick_attach.py
BLOCKSYNC=/usr/local/bin/blocksync.py
# The script runs inside the mig-venv (openstack CLI); the librbd bindings live in the system
# python, so blocksync must be started with that interpreter, not the venv's python3.
SYS_PY=${SYS_PY:-/usr/bin/python3}

source /opt/mig-venv/bin/activate
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY

src() { ( source "$SRC_RC"; openstack "$@" ); }
dst() { ( source "$DST_RC"; openstack "$@" ); }
log() { echo "[$(date +%T)] $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

# --- options -------------------------------------------------------------------
MODE=cold IPMODE=same IPADDR="" VOLUME_TYPE="" START_SOURCE=0 MAX_PASSES=5 DELTA_LIMIT_MB=64 VERIFY=readback
OV_FLAVOR="" OV_SG="" EPHEMERAL=0

parse_options() {
  while [ $# -gt 0 ]; do
    case $1 in
      --mode)        MODE=$2; shift 2;;
      --volume-type) VOLUME_TYPE=$2; shift 2;;
      --ip)          if [ "$2" = same ] || [ "$2" = new ]; then IPMODE=$2
                     elif [[ $2 =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then IPMODE=addr; IPADDR=$2
                     else die "--ip takes same, new or an IPv4 address"; fi; shift 2;;
      --start-source) START_SOURCE=1; shift;;
      --flavor)       OV_FLAVOR=$2; shift 2;;
      --security-group) OV_SG=$2; shift 2;;
      --max-passes)  MAX_PASSES=$2; shift 2;;
      --delta-limit) DELTA_LIMIT_MB=$2; shift 2;;
      --verify)      VERIFY=$2; shift 2;;
      *) die "unknown option $1";;
    esac
  done
  case $MODE in cold|live) ;; *) die "--mode takes cold or live";; esac
  case $VERIFY in readback|full|none) ;; *) die "--verify takes readback, full or none";; esac
  [ "$START_SOURCE" = 0 ] || [ "$IPMODE" != same ] || die "--start-source needs --ip new or --ip <addr>: with the same MAC and IP both VMs cannot be up at once"
}

pool_for_type() {  # prints the RBD pool backing $VOLUME_TYPE
  local e
  if [ -n "$VOLUME_TYPE" ] && [ -n "$RBD_POOL_MAP" ]; then
    IFS=, read -ra _entries <<<"$RBD_POOL_MAP"
    for e in "${_entries[@]}"; do
      [ "${e%%:*}" = "$VOLUME_TYPE" ] && { echo "${e#*:}"; return; }
    done
  fi
  echo "$RBD_POOL"
}
POOL=$RBD_POOL

wait_volume() {  # src|dst volume-id wanted-status [timeout-seconds]
  local cloud=$1 id=$2 want=$3 limit=${4:-3600} start=$SECONDS s
  while s=$($cloud volume show "$id" -f value -c status); [ "$s" != "$want" ]; do
    case $s in error*) die "volume $id entered status $s";; esac
    [ $((SECONDS-start)) -gt "$limit" ] && die "timeout waiting for volume $id ($s, want $want)"
    sleep 3
  done
}

wait_image() {  # src|dst image-id [timeout-seconds]
  local cloud=$1 id=$2 limit=${3:-1800} start=$SECONDS s
  while s=$($cloud image show "$id" -f value -c status); [ "$s" != active ]; do
    case $s in error*|killed) die "image $id entered status $s";; esac
    [ $((SECONDS-start)) -gt "$limit" ] && die "timeout waiting for image $id ($s)"
    sleep 5
  done
}

wait_server() {  # src|dst server wanted-status [timeout-seconds]
  local cloud=$1 id=$2 want=$3 limit=${4:-600} start=$SECONDS s
  while s=$($cloud server show "$id" -f value -c status); [ "$s" != "$want" ]; do
    [ "$s" = ERROR ] && die "server $id entered ERROR"
    [ $((SECONDS-start)) -gt "$limit" ] && die "timeout waiting for server $id ($s, want $want)"
    sleep 3
  done
}

# --- plan: describe the source and check the destination can receive it -----
export_definition() {
  local vm=$1 dir=$2 sid
  mkdir -p "$dir"; chmod 700 "$dir"
  sid=$(src server show "$vm" -f value -c id)
  src server show "$sid" -f json > "$dir/server.json"
  src server volume list "$sid" -f json > "$dir/attachments.json"
  src port list --server "$sid" -f json -c ID > "$dir/port-ids.json"

  jq -r '.[].ID' "$dir/port-ids.json" | while read -r pid; do
    local pj net cidr
    pj=$(src port show "$pid" -f json)
    net=$(src network show "$(jq -r .network_id <<<"$pj")" -f value -c name)
    cidr=$(src subnet show "$(jq -r '.fixed_ips[0].subnet_id' <<<"$pj")" -f value -c cidr)
    local sgs="[]" sg
    for sg in $(jq -r '.security_group_ids[]?' <<<"$pj"); do
      sgs=$(jq --arg n "$(src security group show "$sg" -f value -c name)" '. + [$n]' <<<"$sgs")
    done
    jq -n --arg net "$net" --arg cidr "$cidr" --argjson sgs "$sgs" --argjson p "$pj" '{
        network: $net, cidr: $cidr, security_groups: $sgs, mac: $p.mac_address,
        ip: $p.fixed_ips[0].ip_address, extra_ips: ($p.fixed_ips | length - 1),
        port_security: $p.port_security_enabled, aap: $p.allowed_address_pairs }'
  done | jq -s . > "$dir/ports.json"

  jq -r '.[]."Volume ID"' "$dir/attachments.json" | while read -r vid; do
    src volume show "$vid" -f json
  done | jq -s . > "$dir/volumes.json"
}

preflight() {
  local dir=$1 fail=0
  check() { if eval "$2" >/dev/null 2>&1; then echo "  OK    $1"; else echo "  FAIL  $1"; fail=1; fi; }

  local flavor key
  flavor=${OV_FLAVOR:-$(jq -r '.flavor | if type=="object" then (.original_name // .name) else (. | split(" ")[0]) end' "$dir/server.json")}
  key=$(jq -r '.key_name // empty' "$dir/server.json")
  echo "Destination preflight (mode=$MODE ip=$IPMODE${IPADDR:+ $IPADDR} type=${VOLUME_TYPE:-default} pool=$POOL${OV_FLAVOR:+ flavor=$OV_FLAVOR}${OV_SG:+ sg=$OV_SG}):"
  # A same-named server already on the destination means an earlier migration of
  # this VM (finished, or interrupted after create). Proceeding would duplicate it.
  local vmname dsid
  vmname=$(jq -r .name "$dir/server.json")
  dsid=$(dst server list --name "^${vmname}\$" -f value -c ID | head -1)
  if [ -n "$dsid" ] && [ "$dsid" != "$(cat "$dir/dst-server-id" 2>/dev/null)" ]; then
    echo "  FAIL  destination already has a server named $vmname ($dsid) from an earlier migration — inspect it; to redo the migration run: $0 rollback $vmname (DELETES that destination server and its volumes)"
    fail=1
  fi
  check "flavor $flavor" "dst flavor show '$flavor'"
  [ -n "$key" ] && check "keypair $key" "dst keypair show '$key'"
  [ -z "$VOLUME_TYPE" ] || check "volume type $VOLUME_TYPE" "dst volume type show '$VOLUME_TYPE'"
  if [ -n "$OV_SG" ]; then
    local osg
    IFS=, read -ra _ovsgs <<<"$OV_SG"
    for osg in "${_ovsgs[@]}"; do
      check "override security group $osg" "dst security group show '$osg'"
    done
  fi

  local n i net cidr mac ip
  n=$(jq length "$dir/ports.json")
  [ "$IPMODE" != addr ] || [ "$n" = 1 ] || { echo "  FAIL  --ip <addr> works only for single-NIC VMs (this one has $n ports)"; fail=1; }
  for ((i=0; i<n; i++)); do
    net=$(jq -r ".[$i].network" "$dir/ports.json"); cidr=$(jq -r ".[$i].cidr" "$dir/ports.json")
    mac=$(jq -r ".[$i].mac" "$dir/ports.json"); ip=$(jq -r ".[$i].ip" "$dir/ports.json")
    check "network $net exists" "dst network show '$net'"
    check "subnet $cidr on $net" "dst subnet list --network '$net' -f value -c Subnet | grep -qx '$cidr'"
    case $IPMODE in
      same)
        check "MAC $mac free" "[ -z \"\$(dst port list --mac-address '$mac' -f value -c ID)\" ]"
        check "IP $ip free" "[ -z \"\$(dst port list --network '$net' --fixed-ip ip-address='$ip' -f value -c ID)\" ]";;
      addr)
        check "IP $IPADDR free" "[ -z \"\$(dst port list --network '$net' --fixed-ip ip-address='$IPADDR' -f value -c ID)\" ]";;
    esac
    [ "$(jq -r ".[$i].extra_ips" "$dir/ports.json")" = 0 ] || { echo "  FAIL  port $mac has several fixed IPs (not handled)"; fail=1; }
    [ "$(jq -r ".[$i].aap | length" "$dir/ports.json")" = 0 ] || { echo "  FAIL  port $mac has allowed-address-pairs (not handled)"; fail=1; }
    local sg
    if [ -z "$OV_SG" ]; then
      while read -r sg; do
        [ -n "$sg" ] && check "security group $sg" "dst security group show '$sg'"
      done < <(jq -r ".[$i].security_groups[]" "$dir/ports.json")
    fi
  done

  check "no encrypted volume" "jq -e 'all(.[]; .encrypted != true)' '$dir/volumes.json'"
  check "no multiattach volume" "jq -e 'all(.[]; .multiattach != true)' '$dir/volumes.json'"
  if [ "$EPHEMERAL" = 1 ]; then
    echo "  INFO  root disk is ephemeral (image-backed): migrated through a Glance snapshot"
    [ "$MODE" = cold ] || { echo "  FAIL  --mode live needs boot-from-volume (an ephemeral disk has nothing to clone)"; fail=1; }
  fi
  check "RBD access to pool $POOL" "rbd -c '$CEPH_CONF' --id '$RBD_USER' -p '$POOL' ls"
  check "iSCSI daemon on transfer host" "systemctl is-active iscsid || systemctl start iscsid"
  [ "${MIG_MULTIPATH:-0}" != 1 ] || check "multipathd on transfer host" "systemctl is-active multipathd"
  if [ "$MODE" = live ]; then
    check "blocksync present" "[ -x '$BLOCKSYNC' ]"
    check "librbd python bindings" "'$SYS_PY' -c 'import rados, rbd'"
  fi
  [ "$fail" = 0 ]
}

detect_boot() {  # dir ; sets EPHEMERAL=1 when the root disk is not a volume
  jq -e 'any(.[]; .Device=="/dev/vda" or .Device=="/dev/sda")' "$1/attachments.json" >/dev/null \
    || EPHEMERAL=1
}

cmd_plan() {
  local vm=$1 dir=$STATE_ROOT/$1
  export_definition "$vm" "$dir"
  detect_boot "$dir"
  echo "VM $vm  status=$(jq -r .status "$dir/server.json")"
  jq -r '.[] | "  port  \(.network) \(.ip) \(.mac) sg=\(.security_groups|join(","))"' "$dir/ports.json"
  jq -r '.[] | "  disk  \(.name) \(.id) \(.size)GB bootable=\(.bootable)"' "$dir/volumes.json"
  preflight "$dir"
  echo "Preflight passed."
}

# --- migrate -----------------------------------------------------------------
ATTACHED=""
cleanup_attach() { [ -z "$ATTACHED" ] || ( source "$SRC_RC"; "$BRICK" detach "$ATTACHED" ) || true; }

create_dest_volume() {  # source-volume-id dir ; sets DVID, appends to volume-map
  local vid=$1 dir=$2 vj name size bootable
  vj=$(src volume show "$vid" -f json)
  # Volumes made by "server create --boot-from-volume" have no name.
  name=$(jq -r '.name // ""' <<<"$vj"); name=${name:-mig-$vid}
  size=$(jq -r .size <<<"$vj"); bootable=$(jq -r .bootable <<<"$vj")

  DVID=$(dst volume create "$name" --size "$size" ${VOLUME_TYPE:+--type "$VOLUME_TYPE"} \
         --description "migrated from $vid" -f value -c id)
  wait_volume dst "$DVID" available
  rbd -c "$CEPH_CONF" --id "$RBD_USER" -p "$POOL" info "volume-$DVID" >/dev/null 2>&1 \
    || die "volume-$DVID not visible in pool $POOL: wrong RBD_POOL_MAP for type '${VOLUME_TYPE:-default}', or $RBD_USER lacks caps on that pool"
  echo "$vid $DVID" >> "$dir/volume-map"

  # Boot and guest metadata the destination Nova needs.
  [ "$bootable" = true ] || [ "$bootable" = True ] && dst volume set "$DVID" --bootable
  # Assigned first: a jq failure inside a process substitution would not stop the script.
  local k v props
  props=$(jq -r '(.volume_image_metadata // {}) | to_entries[]
      | select(.key | test("^(hw_|os_|hypervisor_type$|architecture$|vm_mode$)"))
      | [.key, (.value|tostring)] | @tsv' <<<"$vj")
  while IFS=$'\t' read -r k v; do
    [ -z "$k" ] || dst volume set "$DVID" --image-property "$k=$v"
  done <<<"$props"
}

attach_clone() {  # source-volume-id ; sets CLONE and DEVICE
  local vid=$1
  CLONE=$(src volume create "mig-clone-$vid" --source "$vid" -f value -c id)
  wait_volume src "$CLONE" available
  # stderr goes to a file, not /dev/null: brick_attach's own guard messages (multipath,
  # size/LUN mismatch) are the whole point of having it.
  DEVICE=$( source "$SRC_RC"; "$BRICK" attach "$CLONE" 2>"$STATE_ROOT/brick-attach.err" )
  ATTACHED=$CLONE
  [ -b "$DEVICE" ] || die "no block device for clone $CLONE: $(tail -2 "$STATE_ROOT/brick-attach.err" 2>/dev/null | tr '\n' ' ')"
}

drop_clone() {
  ( source "$SRC_RC"; "$BRICK" detach "$CLONE" 2>/dev/null )
  ATTACHED=""
  wait_volume src "$CLONE" available
  src volume delete "$CLONE"
}

copy_volume_cold() {  # source-volume-id dest-volume-id size-gb dir
  local vid=$1 dvid=$2 size=$3 dir=$4 img t0
  attach_clone "$vid"
  log "clone attached at $DEVICE"
  img="rbd:$POOL/volume-$dvid:id=$RBD_USER:conf=$CEPH_CONF"

  log "copy $DEVICE -> $POOL/volume-$dvid"
  t0=$SECONDS
  # The new RBD image is all zeros, so zero runs are skipped and stay unallocated.
  qemu-img convert -f raw -O raw -n --target-is-zero -m "$COPY_STREAMS" -W "$DEVICE" "$img"
  local took=$((SECONDS-t0))
  log "copy done in ${took}s ($((size*1024/(took>0?took:1))) MB/s logical)"

  log "verify byte-for-byte"
  t0=$SECONDS
  qemu-img compare -f raw -F raw "$DEVICE" "$img" || die "verification failed for $vid"
  log "verify OK in $((SECONDS-t0))s"
  drop_clone
}

sync_volume_pass() {  # source-volume-id dest-volume-id dir pass-no first(0|1) final(0|1) ; sets CHANGED_MB
  local vid=$1 dvid=$2 dir=$3 pass=$4 first=$5 final=$6 out t0 vflag=--verify\ readback
  case $VERIFY in none) vflag="--verify none";; esac
  attach_clone "$vid"
  t0=$SECONDS
  out=$("$SYS_PY" "$BLOCKSYNC" --device "$DEVICE" --pool "$POOL" --image "volume-$dvid" \
        --id "$RBD_USER" --conf "$CEPH_CONF" --state "$dir/hash-$vid.bin" \
        $([ "$first" = 1 ] && echo --first) $vflag) || die "blocksync failed for $vid (pass $pass)"
  echo "$out" >> "$dir/sync-$vid.log"
  CHANGED_MB=$(jq -r '(.written_bytes + .zero_chunks*4194304) / 1048576 | ceil' <<<"$out")
  log "pass $pass $vid: $(jq -r '"\(.changed)/\(.chunks) chunks changed, \(.written_bytes/1048576|floor) MB written, \(.seconds)s"' <<<"$out")"
  if [ "$final" = 1 ] && [ "$VERIFY" = full ]; then
    log "full compare (cutover, --verify full)"
    qemu-img compare -f raw -F raw "$DEVICE" "rbd:$POOL/volume-$dvid:id=$RBD_USER:conf=$CEPH_CONF" \
      || die "full verification failed for $vid"
  fi
  drop_clone
}

# An ephemeral root disk has no Cinder volume to clone, so it travels as a Glance snapshot:
# snapshot the stopped VM, download the image, upload it to the destination with its guest
# properties, and boot the new VM from it. Cold only; the source VM is already stopped here.
migrate_ephemeral_root() {  # vm dir server-id
  local vm=$1 dir=$2 sid=$3 snap="mig-snap-$vm" simg dimg fmt k v props t0
  log "snapshot ephemeral root to Glance as $snap"
  t0=$SECONDS
  simg=$(src server image create --name "$snap" "$sid" -f value -c id)
  wait_image src "$simg"
  fmt=$(src image show "$simg" -f value -c disk_format)
  log "snapshot ready in $((SECONDS-t0))s (format $fmt); download"
  t0=$SECONDS
  ( source "$SRC_RC"; openstack image save --file "$dir/root.img" "$simg" )
  log "downloaded $(du -h "$dir/root.img" | cut -f1) in $((SECONDS-t0))s; upload to destination"
  t0=$SECONDS
  # Create and upload with plain REST. openstackclient is unusable for this step: with --file it
  # PATCHes attributes again after a long upload (Glance: 400 once active), and without --file it
  # reads image data from stdin whenever stdin is not a tty (nohup: /dev/null -> 415).
  local tok url
  tok=$( source "$DST_RC"; openstack token issue -f value -c id )
  url=$( dst endpoint list --service image --interface internal -f value -c URL | head -1 )
  dimg=$(curl -sfS -X POST -H "X-Auth-Token: $tok" -H "Content-Type: application/json" \
         -d "{\"name\":\"$snap\",\"disk_format\":\"$fmt\",\"container_format\":\"bare\"}" \
         "$url/v2/images" | jq -r .id)
  [ -n "$dimg" ] && [ "$dimg" != null ] || die "could not create image record on the destination"
  echo "$dimg" > "$dir/dst-image-id"
  curl -sfS -T "$dir/root.img" -H "X-Auth-Token: $tok" \
       -H "Content-Type: application/octet-stream" "$url/v2/images/$dimg/file" \
    || die "image upload to $url failed for $dimg"
  wait_image dst "$dimg"
  # Glance hashed what it stored; comparing with the local file is a real end-to-end check.
  # os_hash_* are not openstackclient columns, they live inside the properties map.
  local lhash rhash ralgo ij
  ij=$(dst image show "$dimg" -f json)
  ralgo=$(jq -r '.properties.os_hash_algo // .os_hash_algo // empty' <<<"$ij")
  rhash=$(jq -r '.properties.os_hash_value // .os_hash_value // empty' <<<"$ij")
  if [ "$ralgo" = sha512 ]; then
    lhash=$(sha512sum "$dir/root.img" | cut -d' ' -f1)
    [ "$lhash" = "$rhash" ] || die "uploaded image hash mismatch for $dimg"
    log "upload verified (sha512 match)"
  else
    log "WARN destination Glance hash algo '$ralgo' not sha512; hash check skipped"
  fi
  # Copy the guest-relevant properties with a raw JSON-patch: "openstack image set" PATCHes
  # read-only fields along with the property and this Glance answers 400 on an active image.
  local patch
  # hw_* and a short allowlist of os_* guest keys only: .properties also carries Glance-managed
  # fields (os_hash_value, os_hash_algo, os_hidden, stores) that reject any PATCH with a 400.
  patch=$(src image show "$simg" -f json | jq -c '[(.properties // {}) | to_entries[]
      | select(.key | test("^(hw_|os_distro$|os_type$|os_secure_boot$|os_require_quiesce$|hypervisor_type$|architecture$|vm_mode$)"))
      | {op: "add", path: ("/" + .key), value: (.value|tostring)}]')
  if [ "$patch" != "[]" ]; then
    curl -sfS -X PATCH -H "X-Auth-Token: $tok" \
      -H "Content-Type: application/openstack-images-v2.1-json-patch" \
      -d "$patch" "$url/v2/images/$dimg" >/dev/null \
      || die "setting image properties failed for $dimg"
    log "copied guest properties: $(jq -r '[.[].path] | join(" ")' <<<"$patch")"
  fi
  src image delete "$simg"
  rm -f "$dir/root.img"
  log "uploaded in $((SECONDS-t0))s; destination image $dimg (kept, the new VM boots from it)"
}

# A stopped VM with its port attached can still be started, by an admin too, despite the lock. In
# the lab that gave two VMs on one IP and MAC: 12 ARP replies to 6 requests, SSH hopping between them.
# Disabling the port is not an option: the source cloud then blackholes traffic from its remaining
# VMs to the migrated MAC. Detaching keeps the port allocated and removes the NIC from the VM.
detach_source_ports() {  # vm dir server-id
  local vm=$1 dir=$2 sid=$3 i=0 pid t dev gone
  : > "$dir/source-ports"
  for pid in $(jq -r '.[].ID' "$dir/port-ids.json"); do
    src server remove port "$sid" "$pid"
    gone=""
    for ((t=0; t<60; t++)); do
      dev=$(src port show "$pid" -f value -c device_id 2>/dev/null) || { dev=""; gone=1; break; }
      [ -z "$dev" ] && break
      sleep 2
    done
    [ -z "$dev" ] || die "port $pid still attached to $dev"
    if [ -n "$gone" ]; then
      # Nova deletes ports it created itself. Re-create at once to hold the IP and MAC for rollback.
      local net ip mac sg sg_args=()
      net=$(jq -r ".[$i].network" "$dir/ports.json"); ip=$(jq -r ".[$i].ip" "$dir/ports.json")
      mac=$(jq -r ".[$i].mac" "$dir/ports.json")
      while read -r sg; do [ -n "$sg" ] && sg_args+=(--security-group "$sg"); done \
        < <(jq -r ".[$i].security_groups[]" "$dir/ports.json")
      [ ${#sg_args[@]} -gt 0 ] || sg_args=(--no-security-group)
      pid=$(src port create "mig-reserve-$vm-$i" --network "$net" --mac-address "$mac" \
            --fixed-ip "ip-address=$ip" "${sg_args[@]}" -f value -c id)
      log "Nova removed its own port; reserved $ip $mac again as $pid"
    fi
    echo "$pid" >> "$dir/source-ports"
    i=$((i+1))
  done
}

create_destination() {  # vm dir
  local vm=$1 dir=$2 n i nics=() flavor key
  n=$(jq length "$dir/ports.json")
  for ((i=0; i<n; i++)); do
    local net ip mac cidr subnet pname sg_args=() sg
    net=$(jq -r ".[$i].network" "$dir/ports.json"); ip=$(jq -r ".[$i].ip" "$dir/ports.json")
    mac=$(jq -r ".[$i].mac" "$dir/ports.json"); cidr=$(jq -r ".[$i].cidr" "$dir/ports.json")
    subnet=$(dst subnet list --network "$net" -f json | jq -r --arg c "$cidr" '.[] | select(.Subnet==$c) | .ID')
    if [ -n "$OV_SG" ]; then
      IFS=, read -ra _ovsgs <<<"$OV_SG"
      for sg in "${_ovsgs[@]}"; do sg_args+=(--security-group "$sg"); done
    else
      while read -r sg; do [ -n "$sg" ] && sg_args+=(--security-group "$sg"); done \
        < <(jq -r ".[$i].security_groups[]" "$dir/ports.json")
    fi
    [ ${#sg_args[@]} -gt 0 ] || sg_args=(--no-security-group)
    pname="mig-$vm-$i"
    case $IPMODE in
      same) dst port create "$pname" --network "$net" --mac-address "$mac" \
              --fixed-ip "subnet=$subnet,ip-address=$ip" "${sg_args[@]}" -f value -c id >/dev/null;;
      new)  dst port create "$pname" --network "$net" \
              --fixed-ip "subnet=$subnet" "${sg_args[@]}" -f value -c id >/dev/null;;
      addr) dst port create "$pname" --network "$net" \
              --fixed-ip "subnet=$subnet,ip-address=$IPADDR" "${sg_args[@]}" -f value -c id >/dev/null;;
    esac
    log "destination port $pname $(dst port show "$pname" -f json | jq -r '"\(.fixed_ips[0].ip_address) \(.mac_address)"')"
    nics+=(--port "$pname")
  done

  flavor=${OV_FLAVOR:-$(jq -r '.flavor | if type=="object" then (.original_name // .name) else (. | split(" ")[0]) end' "$dir/server.json")}
  key=$(jq -r '.key_name // empty' "$dir/server.json")

  # Server metadata travels with the VM: properties and description. Assigned first so a jq
  # failure inside a process substitution cannot slip past set -e.
  local prop_args=() pk pv props desc
  props=$(jq -r '(.properties // .metadata // {}) | to_entries[] | [.key, (.value|tostring)] | @tsv' "$dir/server.json")
  while IFS=$'\t' read -r pk pv; do
    [ -z "$pk" ] || prop_args+=(--property "$pk=$pv")
  done <<<"$props"
  desc=$(jq -r '.description // empty' "$dir/server.json")
  [ -z "$desc" ] || prop_args+=(--description "$desc")

  # Boot volume first, then data volumes in their original device order. The source user-data is
  # deliberately NOT reused: cloud-init sees a new instance-id and would re-run first-boot scripts
  # on real data. This minimal config only stops it from replacing SSH host keys and the hostname.
  local ud=$dir/user-data.yaml
  cat > "$ud" <<'EOF'
#cloud-config
ssh_deletekeys: false
preserve_hostname: true
EOF
  local args=() svid device dvid
  [ "$EPHEMERAL" = 0 ] || args+=(--image "$(cat "$dir/dst-image-id")")
  while read -r svid device; do
    dvid=$(awk -v s="$svid" '$1==s {print $2}' "$dir/volume-map")
    case $device in
      /dev/vda|/dev/sda) args+=(--volume "$dvid");;
      *) args+=(--block-device "uuid=$dvid,source_type=volume,destination_type=volume,device_name=${device#/dev/},boot_index=-1");;
    esac
  done < <(jq -r '.[] | "\(."Volume ID") \(.Device)"' "$dir/attachments.json")

  dst server create "$vm" --flavor "$flavor" "${nics[@]}" "${args[@]}" --user-data "$ud" \
    ${key:+--key-name "$key"} "${prop_args[@]}" -f value -c id > "$dir/dst-server-id"
  wait_server dst "$(cat "$dir/dst-server-id")" ACTIVE
}

cmd_migrate() {
  local vm=$1 dir=$STATE_ROOT/$1 sid vid
  [ ! -e "$dir/volume-map" ] || die "state exists in $dir: run rollback first"
  export_definition "$vm" "$dir"
  detect_boot "$dir"
  preflight "$dir"
  printf 'MODE=%s IPMODE=%s IPADDR=%s VOLUME_TYPE=%s POOL=%s VERIFY=%s FLAVOR=%s SG=%s EPHEMERAL=%s\n' \
    "$MODE" "$IPMODE" "$IPADDR" "$VOLUME_TYPE" "$POOL" "$VERIFY" "$OV_FLAVOR" "$OV_SG" "$EPHEMERAL" > "$dir/options"
  trap cleanup_attach EXIT
  sid=$(jq -r .id "$dir/server.json")
  : > "$dir/volume-map"

  local vids
  vids=$(jq -r '.[]."Volume ID"' "$dir/attachments.json")

  if [ "$MODE" = live ]; then
    # Pre-sync while the source VM keeps running. Each pass reads a fresh crash-consistent
    # clone; only the chunks whose hash changed since the previous pass are written.
    for vid in $vids; do
      create_dest_volume "$vid" "$dir"
      local pass=1 first=1
      while :; do
        sync_volume_pass "$vid" "$DVID" "$dir" "$pass" "$first" 0
        first=0
        [ "$pass" -ge 2 ] && [ "$CHANGED_MB" -le "$DELTA_LIMIT_MB" ] && { log "$vid converged (delta ${CHANGED_MB} MB <= ${DELTA_LIMIT_MB} MB)"; break; }
        [ "$pass" -ge "$MAX_PASSES" ] && { log "WARN $vid did not converge after $pass passes (last delta ${CHANGED_MB} MB); cutover will carry it"; break; }
        pass=$((pass+1))
      done
    done
  fi

  # Cutover: from here the service is down until the destination VM boots.
  local t_start=$SECONDS
  date +%s > "$dir/t-stop"
  # A pre-existing lock (an interrupted earlier run, or an operator lock) makes
  # "server stop" fail with 409. Migrating this VM is the operator's explicit
  # intent, so lift the lock and continue; the tool re-locks right below.
  if [ "$(src server show "$sid" -f value -c locked)" = True ]; then
    log "WARN source VM is already locked (reason: $(src server show "$sid" -f value -c locked_reason 2>/dev/null || echo unknown)); unlocking to proceed"
    src server unlock "$sid"
  fi
  if [ "$(src server show "$sid" -f value -c status)" != SHUTOFF ]; then
    log "stop source VM"
    src server stop "$sid"
    wait_server src "$sid" SHUTOFF
  fi
  src server lock "$sid"
  if [ "$IPMODE" = same ]; then
    detach_source_ports "$vm" "$dir" "$sid"
    log "source stopped, locked and its ports detached"
  else
    log "source stopped and locked; ports left attached (--ip $IPMODE keeps them out of conflict)"
  fi

  [ "$EPHEMERAL" = 0 ] || migrate_ephemeral_root "$vm" "$dir" "$sid"

  for vid in $vids; do
    if [ "$MODE" = live ]; then
      local dvid
      dvid=$(awk -v s="$vid" '$1==s {print $2}' "$dir/volume-map")
      sync_volume_pass "$vid" "$dvid" "$dir" final 0 1
    else
      create_dest_volume "$vid" "$dir"
      copy_volume_cold "$vid" "$DVID" "$(src volume show "$vid" -f value -c size)" "$dir"
    fi
  done

  create_destination "$vm" "$dir"
  log "destination VM ACTIVE; control-plane downtime so far $((SECONDS-t_start))s"
  trap - EXIT

  if [ "$START_SOURCE" = 1 ]; then
    log "WARN --start-source: both VMs will run with the same hostname and SSH host key; their data diverges from this cutover"
    src server unlock "$sid"
    src server start "$sid"
    wait_server src "$sid" ACTIVE
    log "source VM started again"
  fi
  echo "Migrated. Source VM: $( [ "$START_SOURCE" = 1 ] && echo "running again (--start-source)" || echo "kept SHUTOFF and locked" ). Rollback: $0 rollback $vm"
}

# --- rollback ----------------------------------------------------------------
cmd_rollback() {
  local vm=$1 dir=$STATE_ROOT/$1 sid
  [ -e "$dir/server.json" ] || die "no state for $vm in $dir"
  sid=$(jq -r .id "$dir/server.json")

  if [ -s "$dir/dst-server-id" ]; then
    log "delete destination server"
    dst server delete "$(cat "$dir/dst-server-id")" --wait || true
  fi
  local i n pname
  n=$(jq length "$dir/ports.json")
  for ((i=0; i<n; i++)); do
    pname="mig-$vm-$i"
    dst port show "$pname" >/dev/null 2>&1 && dst port delete "$pname"
  done
  if [ -e "$dir/volume-map" ]; then
    local svid dvid
    while read -r svid dvid; do
      wait_volume dst "$dvid" available 300
      dst volume delete "$dvid"
      log "deleted destination volume $dvid"
    done < "$dir/volume-map"
  fi
  # Ephemeral path leftovers: uploaded destination images (by name too, for a run that died
  # before recording the id), a source snapshot from a run that died mid-download, the local file.
  local img
  for img in $( [ -s "$dir/dst-image-id" ] && cat "$dir/dst-image-id"; dst image list --name "mig-snap-$vm" -f value -c ID ) ; do
    dst image delete "$img" 2>/dev/null && log "deleted destination image $img" || true
  done
  local simg
  simg=$(src image list --name "mig-snap-$vm" -f value -c ID)
  [ -z "$simg" ] || src image delete $simg
  rm -f "$dir/root.img"
  # A run that died mid-copy leaves its temporary clone behind. Only this VM's own clones match.
  local vid cid
  for vid in $(jq -r '.[]."Volume ID"' "$dir/attachments.json"); do
    cid=$(src volume list --name "mig-clone-$vid" -f value -c ID)
    [ -z "$cid" ] && continue
    [ -e "/var/lib/mig-attach/$cid.json" ] && ( source "$SRC_RC"; "$BRICK" detach "$cid" 2>/dev/null ) || true
    wait_volume src "$cid" available 300
    src volume delete "$cid"
    log "deleted leftover clone $cid"
  done
  src server unlock "$sid" || true
  # Reattach only the ports this run detached (--ip same). A run that failed before the detach
  # step has no list; its ports are still attached and the device_id check skips them.
  local pid t pids
  if [ -e "$dir/source-ports" ]; then pids=$(cat "$dir/source-ports"); else pids=$(jq -r '.[].ID' "$dir/port-ids.json"); fi
  for pid in $pids; do
    [ -n "$(src port show "$pid" -f value -c device_id)" ] && continue
    src server add port "$sid" "$pid"
    for ((t=0; t<60; t++)); do
      [ "$(src port show "$pid" -f value -c device_id)" = "$sid" ] && break
      sleep 2
    done
    [ "$(src port show "$pid" -f value -c device_id)" = "$sid" ] || die "port $pid did not reattach"
    log "reattached source port $pid"
  done
  if [ "$(src server show "$sid" -f value -c status)" = SHUTOFF ]; then
    src server start "$sid"
  fi
  wait_server src "$sid" ACTIVE
  [ "$(src server show "$sid" -f value -c status)" = ACTIVE ] || die "source VM did not reach ACTIVE"
  mv "$dir" "$dir.rolled-back.$(date +%s)"
  log "source VM ACTIVE again"
}

[ $# -ge 2 ] || { sed -n '2,35p' "$0"; exit 2; }
CMD=$1 VM=$2; shift 2
case $CMD in
  plan|migrate) parse_options "$@"; POOL=$(pool_for_type);;
  rollback) [ $# -eq 0 ] || die "rollback takes no options";;
  *) die "unknown command $CMD";;
esac
case $CMD in
  plan) cmd_plan "$VM";;
  migrate) cmd_migrate "$VM";;
  rollback) cmd_rollback "$VM";;
esac
