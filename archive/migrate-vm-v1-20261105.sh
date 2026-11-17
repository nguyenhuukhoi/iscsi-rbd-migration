#!/usr/bin/env bash
# Cold-migrate a boot-from-volume VM from an iSCSI Cinder cloud to a Ceph RBD Cinder cloud.
# Run as root on the transfer host, which reaches the iSCSI fabric and the Ceph public network.
#
#   migrate-vm.sh plan     <vm>   read-only: export source definition, run preflight on destination
#   migrate-vm.sh migrate  <vm>   stop source, clone, copy, verify, create the destination VM
#   migrate-vm.sh rollback <vm>   delete destination objects, unlock and start the source VM
#
# Source volumes are never modified or deleted. The source VM is stopped, locked and its ports are
# detached (they stay allocated, so the IP and MAC stay reserved); rollback restores all of it.
# Environment overrides: SRC_RC DST_RC RBD_POOL RBD_USER MIG_ISCSI_IP COPY_STREAMS
set -euo pipefail

SRC_RC=${SRC_RC:-/opt/mig/src/admin-openrc.sh}
DST_RC=${DST_RC:-/opt/mig/dst/admin-openrc.sh}
RBD_POOL=${RBD_POOL:-mig-volumes}
RBD_USER=${RBD_USER:-mig-xfer}
COPY_STREAMS=${COPY_STREAMS:-8}
export MIG_ISCSI_IP=${MIG_ISCSI_IP:-10.77.0.20}
CEPH_CONF=${CEPH_CONF:-/etc/ceph/ceph.conf}
STATE_ROOT=/var/lib/mig-migrate
BRICK=/usr/local/bin/brick_attach.py

source /opt/mig-venv/bin/activate
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY

src() { ( source "$SRC_RC"; openstack "$@" ); }
dst() { ( source "$DST_RC"; openstack "$@" ); }
log() { echo "[$(date +%T)] $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

wait_volume() {  # src|dst volume-id wanted-status [timeout-seconds]
  local cloud=$1 id=$2 want=$3 limit=${4:-3600} start=$SECONDS s
  while s=$($cloud volume show "$id" -f value -c status); [ "$s" != "$want" ]; do
    case $s in error*) die "volume $id entered status $s";; esac
    [ $((SECONDS-start)) -gt "$limit" ] && die "timeout waiting for volume $id ($s, want $want)"
    sleep 3
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
  flavor=$(jq -r '.flavor | if type=="object" then (.original_name // .name) else (. | split(" ")[0]) end' "$dir/server.json")
  key=$(jq -r '.key_name // empty' "$dir/server.json")
  echo "Destination preflight:"
  check "flavor $flavor" "dst flavor show '$flavor'"
  [ -n "$key" ] && check "keypair $key" "dst keypair show '$key'"

  local n i net cidr mac ip
  n=$(jq length "$dir/ports.json")
  for ((i=0; i<n; i++)); do
    net=$(jq -r ".[$i].network" "$dir/ports.json"); cidr=$(jq -r ".[$i].cidr" "$dir/ports.json")
    mac=$(jq -r ".[$i].mac" "$dir/ports.json"); ip=$(jq -r ".[$i].ip" "$dir/ports.json")
    check "network $net exists" "dst network show '$net'"
    check "subnet $cidr on $net" "dst subnet list --network '$net' -f value -c Subnet | grep -qx '$cidr'"
    check "MAC $mac free" "[ -z \"\$(dst port list --mac-address '$mac' -f value -c ID)\" ]"
    check "IP $ip free" "[ -z \"\$(dst port list --network '$net' --fixed-ip ip-address='$ip' -f value -c ID)\" ]"
    [ "$(jq -r ".[$i].extra_ips" "$dir/ports.json")" = 0 ] || { echo "  FAIL  port $mac has several fixed IPs (not handled)"; fail=1; }
    [ "$(jq -r ".[$i].aap | length" "$dir/ports.json")" = 0 ] || { echo "  FAIL  port $mac has allowed-address-pairs (not handled)"; fail=1; }
    local sg
    while read -r sg; do
      [ -n "$sg" ] && check "security group $sg" "dst security group show '$sg'"
    done < <(jq -r ".[$i].security_groups[]" "$dir/ports.json")
  done

  check "no encrypted volume" "jq -e 'all(.[]; .encrypted != true)' '$dir/volumes.json'"
  check "no multiattach volume" "jq -e 'all(.[]; .multiattach != true)' '$dir/volumes.json'"
  check "boot volume is /dev/vda or /dev/sda" "jq -e 'any(.[]; .Device==\"/dev/vda\" or .Device==\"/dev/sda\")' '$dir/attachments.json'"
  check "RBD access to pool $RBD_POOL" "rbd -c '$CEPH_CONF' --id '$RBD_USER' -p '$RBD_POOL' ls"
  check "iSCSI daemon on transfer host" "systemctl is-active iscsid || systemctl start iscsid"
  [ "$fail" = 0 ]
}

cmd_plan() {
  local vm=$1 dir=$STATE_ROOT/$1
  export_definition "$vm" "$dir"
  echo "VM $vm  status=$(jq -r .status "$dir/server.json")"
  jq -r '.[] | "  port  \(.network) \(.ip) \(.mac) sg=\(.security_groups|join(","))"' "$dir/ports.json"
  jq -r '.[] | "  disk  \(.name) \(.id) \(.size)GB bootable=\(.bootable)"' "$dir/volumes.json"
  preflight "$dir"
  echo "Preflight passed."
}

# --- migrate -----------------------------------------------------------------
ATTACHED=""
cleanup_attach() { [ -z "$ATTACHED" ] || ( source "$SRC_RC"; "$BRICK" detach "$ATTACHED" ) || true; }

copy_volume() {  # source-volume-id dir ; appends "src dst" to dir/volume-map
  local vid=$1 dir=$2 vj name size bootable clone device dvid img t0
  vj=$(src volume show "$vid" -f json)
  # Volumes made by "server create --boot-from-volume" have no name.
  name=$(jq -r '.name // ""' <<<"$vj"); name=${name:-mig-$vid}; size=$(jq -r .size <<<"$vj"); bootable=$(jq -r .bootable <<<"$vj")

  log "clone $name ($size GB)"
  t0=$SECONDS
  clone=$(src volume create "mig-clone-$vid" --source "$vid" -f value -c id)
  wait_volume src "$clone" available
  log "clone ready in $((SECONDS-t0))s"

  device=$( source "$SRC_RC"; "$BRICK" attach "$clone" 2>/dev/null )
  ATTACHED=$clone
  [ -b "$device" ] || die "no block device for clone $clone"
  log "clone attached at $device"

  dvid=$(dst volume create "$name" --size "$size" \
         --description "migrated from $vid" -f value -c id)
  wait_volume dst "$dvid" available
  echo "$vid $dvid" >> "$dir/volume-map"
  img="rbd:$RBD_POOL/volume-$dvid:id=$RBD_USER:conf=$CEPH_CONF"

  log "copy $device -> $RBD_POOL/volume-$dvid"
  t0=$SECONDS
  # The new RBD image is all zeros, so zero runs are skipped and stay unallocated.
  qemu-img convert -f raw -O raw -n --target-is-zero -m "$COPY_STREAMS" -W "$device" "$img"
  local took=$((SECONDS-t0))
  log "copy done in ${took}s ($((size*1024/(took>0?took:1))) MB/s logical)"

  log "verify byte-for-byte"
  t0=$SECONDS
  qemu-img compare -f raw -F raw "$device" "$img" || die "verification failed for $vid"
  log "verify OK in $((SECONDS-t0))s"

  ( source "$SRC_RC"; "$BRICK" detach "$clone" 2>/dev/null )
  ATTACHED=""
  wait_volume src "$clone" available
  src volume delete "$clone"

  # Boot and guest metadata the destination Nova needs.
  [ "$bootable" = true ] || [ "$bootable" = True ] && dst volume set "$dvid" --bootable
  # Assigned first: a jq failure inside a process substitution would not stop the script.
  local k v props
  props=$(jq -r '(.volume_image_metadata // {}) | to_entries[]
      | select(.key | test("^(hw_|os_|hypervisor_type$|architecture$|vm_mode$)"))
      | [.key, (.value|tostring)] | @tsv' <<<"$vj")
  while IFS=$'\t' read -r k v; do
    [ -z "$k" ] || dst volume set "$dvid" --image-property "$k=$v"
  done <<<"$props"
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
      while read -r sg; do [ -n "$sg" ] && sg_args+=(--security-group "$sg"); done         < <(jq -r ".[$i].security_groups[]" "$dir/ports.json")
      [ ${#sg_args[@]} -gt 0 ] || sg_args=(--no-security-group)
      pid=$(src port create "mig-reserve-$vm-$i" --network "$net" --mac-address "$mac"             --fixed-ip "ip-address=$ip" "${sg_args[@]}" -f value -c id)
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
    while read -r sg; do [ -n "$sg" ] && sg_args+=(--security-group "$sg"); done \
      < <(jq -r ".[$i].security_groups[]" "$dir/ports.json")
    [ ${#sg_args[@]} -gt 0 ] || sg_args=(--no-security-group)
    pname="mig-$vm-$i"
    dst port create "$pname" --network "$net" --mac-address "$mac" \
      --fixed-ip "subnet=$subnet,ip-address=$ip" "${sg_args[@]}" -f value -c id >/dev/null
    log "destination port $pname $ip $mac"
    nics+=(--port "$pname")
  done

  flavor=$(jq -r '.flavor | if type=="object" then (.original_name // .name) else (. | split(" ")[0]) end' "$dir/server.json")
  key=$(jq -r '.key_name // empty' "$dir/server.json")

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
  while read -r svid device; do
    dvid=$(awk -v s="$svid" '$1==s {print $2}' "$dir/volume-map")
    case $device in
      /dev/vda|/dev/sda) args+=(--volume "$dvid");;
      *) args+=(--block-device "uuid=$dvid,source_type=volume,destination_type=volume,device_name=${device#/dev/},boot_index=-1");;
    esac
  done < <(jq -r '.[] | "\(."Volume ID") \(.Device)"' "$dir/attachments.json")

  dst server create "$vm" --flavor "$flavor" "${nics[@]}" "${args[@]}" --user-data "$ud" \
    ${key:+--key-name "$key"} -f value -c id > "$dir/dst-server-id"
  wait_server dst "$(cat "$dir/dst-server-id")" ACTIVE
}

cmd_migrate() {
  local vm=$1 dir=$STATE_ROOT/$1 sid
  [ ! -e "$dir/volume-map" ] || die "state exists in $dir: run rollback first"
  export_definition "$vm" "$dir"
  preflight "$dir"
  trap cleanup_attach EXIT
  sid=$(jq -r .id "$dir/server.json")

  local t_start=$SECONDS
  date +%s > "$dir/t-stop"
  if [ "$(src server show "$sid" -f value -c status)" != SHUTOFF ]; then
    log "stop source VM"
    src server stop "$sid"
    wait_server src "$sid" SHUTOFF
  fi
  src server lock "$sid"
  detach_source_ports "$vm" "$dir" "$sid"
  log "source stopped, locked and its ports detached"

  : > "$dir/volume-map"
  local vid
  for vid in $(jq -r '.[]."Volume ID"' "$dir/attachments.json"); do
    copy_volume "$vid" "$dir"
  done

  create_destination "$vm" "$dir"
  log "destination VM ACTIVE; control-plane downtime so far $((SECONDS-t_start))s"
  trap - EXIT
  echo "Migrated. Source VM is kept SHUTOFF, locked and without ports. Rollback: $0 rollback $vm"
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
  src server unlock "$sid"
  # A run that failed before the detach step has no list, and its ports are still attached.
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

[ $# -eq 2 ] || { sed -n '2,9p' "$0"; exit 2; }
case $1 in
  plan) cmd_plan "$2";;
  migrate) cmd_migrate "$2";;
  rollback) cmd_rollback "$2";;
  *) die "unknown command $1";;
esac
