#!/usr/bin/env bash
# Migrate a boot-from-volume VM between two independent OpenStack clouds: iSCSI Cinder -> Ceph RBD.
# Run as root on the transfer host, which reaches the iSCSI fabric and the Ceph public network.
#
#   migrate-vm.sh plan     <vm> [options]  read-only: export source definition, destination preflight
#   migrate-vm.sh migrate  <vm> [options]  migrate; cold stops the VM first, live only at cutover
#   migrate-vm.sh rollback <vm>            delete destination objects, restore and start the source VM
#   migrate-vm.sh migrate-volume <volume> [--volume-type t] [--az z] [--verify v] [--in-use]
#                                          copy one available volume (name or ID) through a clone: the
#                                          source is never touched, the copy keeps the source name.
#                                          --in-use also takes an attached volume: copied from a clone
#                                          taken while the VM runs (crash-consistent), nothing detached
#   migrate-vm.sh rollback <volume>        delete that copy (a VM state of the same name wins: use the ID)
#
# Options (migrate and plan):
#   --mode cold|live        cold (default): stop the VM, qemu-img copy + full compare (unless
#                           --verify none).
#                           live: hash-cached pre-sync passes while the VM runs; the cutover only
#                           syncs the final delta, so downtime tracks the write rate, not the size.
#   --volume-type <type>    Cinder volume type for every destination volume (default: cloud default).
#                           RBD_POOL_MAP="typeA:poolA,typeB:poolB" maps types to RBD pools.
#   --az <zone>             destination availability zone for BOTH the volumes and the server
#                           (for clouds with per-AZ backends / cross_az_attach=false).
#                           Default: let the destination schedulers pick.
#   --flavor <name>         flavor for the destination VM (default: same name as the source).
#   --security-group <sgs>  comma-separated security groups for every destination port
#                           (default: the same group names the source ports carry).
#   --ip same|new|<addr>    same (default): keep MAC+IP, detach source ports at cutover.
#                           new: destination allocates a new MAC+IP, source ports stay attached.
#                           <addr>: that address with a new MAC (single-NIC VMs only).
#   --start-source          after a successful migrate, unlock and start the source VM again.
#                           Only with --ip new|<addr> (both VMs then run with the same hostname),
#                           or with --volumes-only, where any --ip is fine (no destination VM).
#   --keep-source-ports     with --ip same: do NOT detach the source ports. UNSAFE: the only
#                           thing keeping the old VM off the wire is then its SHUTOFF+lock state;
#                           anyone who powers it on puts two machines with the same MAC+IP on the
#                           VLAN (split-brain). Rollback then needs no port reattach.
#   --server-group <name>   destination server group the new VM joins (--hint group=<id>). It must
#                           already exist in the destination project; it is never created.
#   --keep-server-group     the new VM joins az-<source group name> in the destination project, with
#                           the source group's policy (and rules). migrate creates it when missing,
#                           plan never does; rollback never deletes it. Not with --server-group.
#   --volumes-only          cold only: copy the volumes, create no destination port or server.
#                           The source is stopped and locked as usual; its ports stay attached.
#   --max-passes <n>        live: maximum pre-sync passes (default 5).
#   --delta-limit <MB>      live: cut over once a pass changed at most this much (default 64).
#   --verify readback|full|none
#                           cold: full qemu-img compare unless none; readback and full are the same.
#                           live: readback (default) re-reads every written chunk, full adds a
#                           cutover compare, none trusts the writes.
#
# Boot-from-volume VMs are copied volume by volume. A VM with an ephemeral (image-backed) root
# disk is migrated cold through a Glance snapshot: snapshot -> download -> upload -> boot; its
# attached data volumes still go the volume way. Server properties and description are copied.
# Source volumes are never modified or deleted; rollback restores everything.
# Destination volumes are named <vm>-<device> (app01-vda, app01-vdb), ports <vm>-<ip>.
# Environment overrides: SRC_RC DST_RC RBD_POOL RBD_POOL_MAP RBD_USER CEPH_CONF MIG_ISCSI_IP COPY_STREAMS
# MIG_MULTIPATH; the same keys may be set in $MIG_CONF (default /opt/mig/migrate.conf), and the
# environment wins over that file. SRC_PROJECT / DST_PROJECT run each side in that project of the
# openrc's account (password openrc only: an application credential is bound to one project).
set -euo pipefail

# Precedence: CLI option > environment > $MIG_CONF > defaults below. Variables already set in the
# environment are put back after sourcing the file, so export-based runs behave exactly as before.
MIG_CONF=${MIG_CONF:-/opt/mig/migrate.conf}
if [ -r "$MIG_CONF" ]; then
  declare -A _env_saved=()
  for _v in SRC_RC DST_RC RBD_POOL RBD_POOL_MAP RBD_USER CEPH_CONF MIG_ISCSI_IP MIG_MULTIPATH \
            COPY_STREAMS PROJECT_MAP_FILE SRC_PROJECT DST_PROJECT; do
    [ -z "${!_v+x}" ] || _env_saved[$_v]=${!_v}
  done
  source "$MIG_CONF"
  for _v in "${!_env_saved[@]}"; do printf -v "$_v" '%s' "${_env_saved[$_v]}"; done
  unset _v _env_saved
fi
# brick_attach.py reads it from its environment; a value from the file is not exported by itself.
[ -z "${MIG_MULTIPATH+x}" ] || export MIG_MULTIPATH

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

# Every use of the openrc files goes through src_env/dst_env (inside a subshell), so that
# SRC_PROJECT/DST_PROJECT put clones, volumes, ports and the server in the VM's own project.
scope_project() {  # project-name ; no-op when empty
  [ -n "$1" ] || return 0
  export OS_PROJECT_NAME=$1
  unset OS_PROJECT_ID OS_TENANT_NAME OS_TENANT_ID
}
src_env() { source "$SRC_RC"; scope_project "${SRC_PROJECT:-}"; }
dst_env() { source "$DST_RC"; scope_project "${DST_PROJECT:-}"; }
src() { ( src_env; openstack "$@" ); }
dst() { ( dst_env; openstack "$@" ); }
log() { echo "[$(date +%T)] $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

check_project_scope() {  # an application credential cannot be rescoped: fail before touching anything
  local auth
  if [ -n "${SRC_PROJECT:-}" ]; then
    auth=$( source "$SRC_RC" >/dev/null 2>&1; echo "${OS_AUTH_TYPE:-}" )
    [[ $auth != *applicationcredential* ]] || die "SRC_PROJECT/DST_PROJECT needs a password openrc: an application credential is bound to one project ($SRC_RC)"
  fi
  if [ -n "${DST_PROJECT:-}" ]; then
    auth=$( source "$DST_RC" >/dev/null 2>&1; echo "${OS_AUTH_TYPE:-}" )
    [[ $auth != *applicationcredential* ]] || die "SRC_PROJECT/DST_PROJECT needs a password openrc: an application credential is bound to one project ($DST_RC)"
  fi
}

# Neutron shows an admin the security groups of every project, even through a project-scoped
# token, so a name such as "default" matches one group per project: names are resolved to IDs
# inside the destination token's own project.
DST_PID=""
dst_project_id() {  # caches DST_PID, the destination token's project; call it outside $(...)
  [ -n "$DST_PID" ] || DST_PID=$(dst token issue -f value -c project_id) || die "cannot get a destination token"
  [ -n "$DST_PID" ] || die "the destination token is not scoped to a project"
}
dst_sg_id() {  # name-or-id ; prints the group's ID in the destination project, nothing when absent
  local json ids n
  if [[ $1 =~ ^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$ ]]; then echo "$1"; return; fi
  dst_project_id
  json=$(dst security group list --project "$DST_PID" -f json) || die "cannot list the security groups of destination project $DST_PID"
  ids=$(jq -r --arg n "$1" '.[] | select((.Name // .name) == $n) | (.ID // .id)' <<<"$json")
  n=$(grep -c . <<<"$ids" || true)
  [ "$n" -le 1 ] || die "$n security groups are named '$1' in destination project ${DST_PROJECT:-$DST_PID}: ${ids//$'\n'/ } — pass the ID to --security-group"
  echo "$ids"
}
# The same on the source side (reserve ports re-created while the VM is cut over).
SRC_PID=""
src_project_id() {  # caches SRC_PID, the source token's project; call it outside $(...)
  [ -n "$SRC_PID" ] || SRC_PID=$(src token issue -f value -c project_id) || die "cannot get a source token"
  [ -n "$SRC_PID" ] || die "the source token is not scoped to a project"
}
src_sg_id() {  # name-or-id ; prints the group's ID in the source project, nothing when absent
  local json ids n
  if [[ $1 =~ ^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$ ]]; then echo "$1"; return; fi
  src_project_id
  json=$(src security group list --project "$SRC_PID" -f json) || die "cannot list the security groups of source project $SRC_PID"
  ids=$(jq -r --arg n "$1" '.[] | select((.Name // .name) == $n) | (.ID // .id)' <<<"$json")
  n=$(grep -c . <<<"$ids" || true)
  [ "$n" -le 1 ] || die "$n security groups are named '$1' in source project ${SRC_PROJECT:-$SRC_PID}: ${ids//$'\n'/ }"
  echo "$ids"
}

# --- options -------------------------------------------------------------------
MODE=cold IPMODE=same IPADDR="" VOLUME_TYPE="" DEST_AZ="" START_SOURCE=0 KEEP_PORTS=0 MAX_PASSES=5 DELTA_LIMIT_MB=64 VERIFY=readback
OV_FLAVOR="" OV_SG="" EPHEMERAL=0 SERVER_GROUP="" VOLUMES_ONLY=0
KEEP_SG=0 IN_USE=0

parse_options() {
  while [ $# -gt 0 ]; do
    case $1 in
      --mode)        MODE=$2; shift 2;;
      --volume-type) VOLUME_TYPE=$2; shift 2;;
      --az)          DEST_AZ=$2; shift 2;;
      --ip)          if [ "$2" = same ] || [ "$2" = new ]; then IPMODE=$2
                     elif [[ $2 =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then IPMODE=addr; IPADDR=$2
                     else die "--ip takes same, new or an IPv4 address"; fi; shift 2;;
      --start-source) START_SOURCE=1; shift;;
      --keep-source-ports) KEEP_PORTS=1; shift;;
      --flavor)       OV_FLAVOR=$2; shift 2;;
      --security-group) OV_SG=$2; shift 2;;
      --server-group) SERVER_GROUP=$2; shift 2;;
      --keep-server-group) KEEP_SG=1; shift;;
      --in-use)       IN_USE=1; shift;;
      --volumes-only) VOLUMES_ONLY=1; shift;;
      --max-passes)  MAX_PASSES=$2; shift 2;;
      --delta-limit) DELTA_LIMIT_MB=$2; shift 2;;
      --verify)      VERIFY=$2; shift 2;;
      *) die "unknown option $1";;
    esac
  done
  case $MODE in cold|live) ;; *) die "--mode takes cold or live";; esac
  case $VERIFY in readback|full|none) ;; *) die "--verify takes readback, full or none";; esac
  [ "$VOLUMES_ONLY" = 0 ] || [ "$MODE" = cold ] || die "--volumes-only is cold only: drop --mode live"
  # --volumes-only creates no destination VM, so a restarted source cannot clash on MAC/IP.
  [ "$START_SOURCE" = 0 ] || [ "$IPMODE" != same ] || [ "$VOLUMES_ONLY" = 1 ] || die "--start-source needs --ip new or --ip <addr>: with the same MAC and IP both VMs cannot be up at once"
  [ -z "$SERVER_GROUP" ] || [ "$KEEP_SG" = 0 ] || die "choose one: --server-group <name> or --keep-server-group"
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

# --- progress ------------------------------------------------------------------
# progress_watch logs how far a long sequential read of a device has got, from the
# sectors-read counter in /sys/block/<dev>/stat (/dev/mapper names resolve to their dm-N).
# Start it as "progress_watch ... & WATCH_PID=$!" and end it with stop_watch; cleanup_attach
# (the EXIT trap) stops it too, so a step that dies leaves no watcher behind.
WATCH_PID=""
progress_watch() {  # device size-gb label
  local dev=$1 size=$2 label=$3 base file total start last cur now t done_b gib10 pct rate sleeper=""
  [[ $size =~ ^[1-9][0-9]*$ ]] || return 0
  base=$(basename "$(readlink -f "$dev")") || return 0
  file=/sys/block/$base/stat
  [ -r "$file" ] || return 0
  # stop_watch's SIGTERM interrupts "wait": take the pending sleep down as well, so no
  # orphan keeps the script's stdout (a pipe to tee, say) open after the step ends.
  trap '[ -z "${sleeper:-}" ] || kill "$sleeper" 2>/dev/null || true; exit 0' TERM
  { read -r _ _ start _ < "$file"; } 2>/dev/null && [[ $start =~ ^[0-9]+$ ]] || return 0
  total=$((size*1073741824)) last=$start t=$SECONDS
  while :; do
    sleep 15 & sleeper=$!
    wait "$sleeper" || true
    sleeper=""
    { read -r _ _ cur _ < "$file"; } 2>/dev/null || continue
    [[ $cur =~ ^[0-9]+$ ]] || continue
    now=$SECONDS
    done_b=$(( (cur-start)*512 ))
    pct=$(( done_b*100/total )); pct=$(( pct>99 ? 99 : pct ))
    gib10=$(( done_b*10/1073741824 ))
    rate=$(( (cur-last)*512/1048576/(now>t ? now-t : 1) ))
    log "$label $pct% ($((gib10/10)).$((gib10%10))/$size GiB, $rate MB/s)"
    last=$cur t=$now
  done
}

stop_watch() {  # stop the running progress_watch, if any; safe to call twice
  [ -n "$WATCH_PID" ] || return 0
  kill "$WATCH_PID" 2>/dev/null || true
  wait "$WATCH_PID" 2>/dev/null || true
  WATCH_PID=""
}

# --- plan: describe the source and check the destination can receive it -----
export_definition() {
  local vm=$1 dir=$2 sid
  mkdir -p "$dir"; chmod 700 "$dir"
  # Nova allows duplicate names in a project; "server show <name>" then fails with an
  # unhelpful OSC error. Zero matches is fine: the argument may itself be a server ID.
  local ids n
  ids=$(src server list --name "^${vm}\$" -f value -c ID)
  n=$(grep -c . <<<"$ids" || true)
  [ "$n" -le 1 ] || die "$n servers are named '$vm' in the source project: ${ids//$'\n'/ } — re-run with the server ID (migrate/plan/rollback all take it)"
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

dest_volume_name() {  # source-volume-id dir ; prints <instance-name>-<device>, e.g. app01-vdb
  local dev
  dev=$(jq -r --arg v "$1" '[.[] | select(."Volume ID"==$v) | .Device][0] // empty' "$2/attachments.json")
  [ -n "$dev" ] || dev=$1
  echo "$(jq -r .name "$2/server.json")-${dev##*/}"
}

# --keep-server-group: the source VM's group (source project) maps to az-<name> in the destination
# project. Preflight only reads; migrate creates a missing group right before the server.
KSG_DST="" KSG_POLICY="" KSG_RULES="{}" KSG_ID=""
dst_server_groups_named() {  # name ; one "<id> <policy>" line per destination group of that name
  local sgj
  sgj=$(dst server group list -f json) || return 1
  jq -r --arg g "$1" '.[] | select(.Name==$g)
      | "\(.ID) \((.Policy // .Policies // "unknown") | if type=="array" then (.[0] // "unknown") else . end)"' <<<"$sgj"
}

keep_sg_preflight() {  # dir ; called from preflight, sets its fail
  local sid sgj gid n gj sname rtxt dlist
  sid=$(jq -r .id "$1/server.json")
  if ! sgj=$(src server group list --long -f json); then
    echo "  FAIL  cannot list source server groups"; fail=1; return 0
  fi
  gid=$(jq -r --arg s "$sid" '.[] | select(any((.Members // [])[]; . == $s)) | .ID' <<<"$sgj")
  n=$(grep -c . <<<"$gid" || true)
  case $n in
    0) echo "  INFO  source VM is in no server group"; return 0;;
    1) ;;
    *) echo "  FAIL  source VM is in $n server groups (${gid//$'\n'/ }): drop --keep-server-group and pass --server-group <id>"
       fail=1; return 0;;
  esac
  if ! gj=$(src server group show "$gid" -f json); then
    echo "  FAIL  cannot read source server group $gid"; fail=1; return 0
  fi
  # Compute API < 2.64 has a "policies" list and no rules.
  sname=$(jq -r .name <<<"$gj")
  KSG_POLICY=$(jq -r '(.policy // .policies // empty) | if type=="array" then (.[0] // empty) else . end' <<<"$gj")
  KSG_RULES=$(jq -c '.rules // {} | if type=="object" then . else {} end' <<<"$gj")
  KSG_DST=az-$sname
  rtxt=$(jq -r 'to_entries | map("\(.key)=\(.value)") | join(",")' <<<"$KSG_RULES")
  echo "  INFO  source server group $sname ($gid, policy ${KSG_POLICY:-unknown}${rtxt:+, rules $rtxt}) -> destination $KSG_DST"
  [ -n "$KSG_POLICY" ] || { echo "  FAIL  cannot read the policy of source server group $sname ($gid)"; fail=1; return 0; }
  if ! dlist=$(dst_server_groups_named "$KSG_DST"); then
    echo "  FAIL  cannot list destination server groups"; fail=1; return 0
  fi
  n=$(grep -c . <<<"$dlist" || true)
  case $n in
    0) echo "  OK    will create $KSG_DST (policy $KSG_POLICY)";;
    1) if [ "${dlist#* }" = "$KSG_POLICY" ]; then
         echo "  OK    server group $KSG_DST exists (${dlist%% *}, policy $KSG_POLICY): reused"
       else
         echo "  FAIL  server group $KSG_DST has policy ${dlist#* } at the destination, source group $sname has $KSG_POLICY — to use it anyway drop --keep-server-group and pass --server-group ${dlist%% *}"
         fail=1
       fi;;
    *) echo "  FAIL  $n server groups are named $KSG_DST in the destination project (ambiguous): $(cut -d' ' -f1 <<<"$dlist" | tr '\n' ' ')— drop --keep-server-group and pass --server-group <id>"
       fail=1;;
  esac
}

keep_sg_ensure() {  # dir ; finds or creates $KSG_DST at the destination, sets KSG_ID, records it
  local dir=$1 dlist n created=no rules k v rule_args=() err=$1/server-group.err
  dlist=$(dst_server_groups_named "$KSG_DST") || die "cannot list destination server groups"
  n=$(grep -c . <<<"$dlist" || true)
  case $n in
    0) rules=$(jq -r 'to_entries[] | [.key, (.value|tostring)] | @tsv' <<<"$KSG_RULES")
       while IFS=$'\t' read -r k v; do
         [ -z "$k" ] || rule_args+=(--rule "$k=$v")
       done <<<"$rules"
       # An older client, or a compute API version pinned below 2.64, refuses --rule before any API call.
       if ! KSG_ID=$(dst server group create --policy "$KSG_POLICY" "${rule_args[@]}" "$KSG_DST" -f value -c id 2>"$err"); then
         [ ${#rule_args[@]} -gt 0 ] && grep -q -- '--rule' "$err" \
           || die "cannot create server group $KSG_DST in the destination project: $(tail -1 "$err")"
         log "WARN openstackclient refused --rule ($(tail -1 "$err")): $KSG_DST is created without the source rules $KSG_RULES"
         KSG_ID=$(dst server group create --policy "$KSG_POLICY" "$KSG_DST" -f value -c id)
       fi
       [ -n "$KSG_ID" ] || die "server group create returned no ID for $KSG_DST"
       created=yes
       log "created destination server group $KSG_DST ($KSG_ID, policy $KSG_POLICY)";;
    1) [ "${dlist#* }" = "$KSG_POLICY" ] \
         || die "server group $KSG_DST has policy ${dlist#* } at the destination, the source group has $KSG_POLICY"
       KSG_ID=${dlist%% *}
       log "destination server group $KSG_DST ($KSG_ID) exists: the VM joins it";;
    *) die "$n server groups are named $KSG_DST in the destination project: drop --keep-server-group and pass --server-group <id>";;
  esac
  echo "$KSG_ID created=$created" > "$dir/dst-server-group"
}

preflight() {
  local dir=$1 fail=0
  check() { if eval "$2" >/dev/null 2>&1; then echo "  OK    $1"; else echo "  FAIL  $1"; fail=1; fi; }

  # Here rather than in cmd_migrate so that plan reports it too.
  [ "$VOLUMES_ONLY" = 0 ] || [ "$EPHEMERAL" = 0 ] || die "ephemeral VM has no volume to migrate: its root disk is image-backed and --volumes-only copies Cinder volumes only"
  local flavor key vo=""
  flavor=${OV_FLAVOR:-$(jq -r '.flavor | if type=="object" then (.original_name // .name) else (. | split(" ")[0]) end' "$dir/server.json")}
  key=$(jq -r '.key_name // empty' "$dir/server.json")
  [ "$VOLUMES_ONLY" = 0 ] || vo=" volumes-only"
  local ksg=""
  [ "$KEEP_SG" = 0 ] || ksg=" keep-server-group"
  echo "Destination preflight (mode=$MODE$vo ip=$IPMODE${IPADDR:+ $IPADDR} type=${VOLUME_TYPE:-default} pool=$POOL${DEST_AZ:+ az=$DEST_AZ}${OV_FLAVOR:+ flavor=$OV_FLAVOR}${OV_SG:+ sg=$OV_SG}${SERVER_GROUP:+ server-group=$SERVER_GROUP}$ksg):"
  if [ -n "$DEST_AZ" ]; then
    [ "$VOLUMES_ONLY" = 1 ] || check "compute AZ $DEST_AZ" "dst availability zone list --compute -f value -c \"Zone Name\" | grep -qx -- \"$DEST_AZ\""
    check "volume AZ $DEST_AZ"  "dst availability zone list --volume  -f value -c \"Zone Name\" | grep -qx -- \"$DEST_AZ\""
  fi
  if [ "$VOLUMES_ONLY" = 1 ]; then
    # No server is created, so the guard against a second copy is on the destination volume names.
    # Volumes recorded in this VM's own state are not a clash (plan run after a migrate).
    [ -z "$SERVER_GROUP" ] || echo "  INFO  --server-group $SERVER_GROUP ignored: --volumes-only creates no server"
    [ "$KEEP_SG" = 0 ] || echo "  INFO  --keep-server-group ignored: --volumes-only creates no server"
    local svid dname dids did clash
    for svid in $(jq -r '.[]."Volume ID"' "$dir/attachments.json"); do
      dname=$(dest_volume_name "$svid" "$dir")
      if ! dids=$(dst volume list --name "$dname" -f value -c ID); then
        echo "  FAIL  cannot list destination volumes named $dname"; fail=1; continue
      fi
      clash=""
      for did in $dids; do grep -qs " $did\$" "$dir/volume-map" || clash+=" $did"; done
      if [ -z "$clash" ]; then
        echo "  OK    volume name $dname free"
      else
        echo "  FAIL  destination already has a volume named $dname (${clash# }) — rename or delete it first, the migrated volume must be unambiguous"
        fail=1
      fi
    done
  else
    # A same-named server already on the destination means an earlier migration of
    # this VM (finished, or interrupted after create). Proceeding would duplicate it.
    local vmname dsid
    vmname=$(jq -r .name "$dir/server.json")
    dsid=$(dst server list --name "^${vmname}\$" -f value -c ID | head -1)
    if [ -n "$dsid" ] && [ "$dsid" != "$(cat "$dir/dst-server-id" 2>/dev/null)" ]; then
      echo "  FAIL  destination already has a server named $vmname ($dsid) from an earlier migration — inspect it; to redo the migration run: $0 rollback $(basename "$dir") (DELETES that destination server and its volumes)"
      fail=1
    fi
    check "flavor $flavor" "dst flavor show '$flavor'"
    # Nova cannot assign another user's keypair; the key itself is inside the disk anyway.
    if [ -n "$key" ]; then
      if dst keypair show "$key" >/dev/null 2>&1; then
        echo "  OK    keypair $key"
      else
        echo "  INFO  keypair $key not at destination: the VM will be created without key_name (add it back later)"
      fi
    fi
    if [ -n "$SERVER_GROUP" ]; then
      local sgj sgn
      if ! sgj=$(dst server group list -f json); then
        echo "  FAIL  cannot list destination server groups"; fail=1
      else
        sgn=$(jq --arg g "$SERVER_GROUP" '[.[] | select(.Name==$g or .ID==$g)] | length' <<<"$sgj")
        case $sgn in
          1) echo "  OK    server group $SERVER_GROUP"
             echo "  INFO  server group $SERVER_GROUP policy: $(jq -r --arg g "$SERVER_GROUP" '.[] | select(.Name==$g or .ID==$g)
                    | (.Policy // .Policies // "unknown") | if type=="array" then join(",") else tostring end' <<<"$sgj")";;
          0) echo "  FAIL  server group $SERVER_GROUP not found in the destination project — create it first: openstack server group create --policy anti-affinity $SERVER_GROUP"
             fail=1;;
          *) echo "  FAIL  $sgn server groups are named $SERVER_GROUP in the destination project — pass the ID to --server-group"
             fail=1;;
        esac
      fi
    fi
    [ "$KEEP_SG" = 0 ] || keep_sg_preflight "$dir"
  fi
  [ -z "$VOLUME_TYPE" ] || check "volume type $VOLUME_TYPE" "dst volume type show '$VOLUME_TYPE'"
  check_sg() {  # label name-or-id ; exactly one group of that name in the destination project
    local id err=$STATE_ROOT/sg-resolve.err
    if id=$(dst_sg_id "$2" 2>"$err") && [ -n "$id" ]; then echo "  OK    $1 ($id)"
    elif grep -q '^ERROR: ' "$err"; then echo "  FAIL  $1: $(sed -n 's/^ERROR: //p' "$err" | tail -1)"; fail=1
    else echo "  FAIL  $1 not found in destination project ${DST_PROJECT:-$DST_PID}"; fail=1; fi
  }
  [ "$VOLUMES_ONLY" = 1 ] || dst_project_id
  if [ "$VOLUMES_ONLY" = 0 ] && [ -n "$OV_SG" ]; then
    local osg
    IFS=, read -ra _ovsgs <<<"$OV_SG"
    for osg in "${_ovsgs[@]}"; do
      check_sg "override security group $osg" "$osg"
    done
  fi

  # --volumes-only creates no destination port: no network, address or security group check.
  local n=0 i net cidr mac ip
  if [ "$VOLUMES_ONLY" = 0 ]; then
    n=$(jq length "$dir/ports.json")
    [ "$IPMODE" != addr ] || [ "$n" = 1 ] || { echo "  FAIL  --ip <addr> works only for single-NIC VMs (this one has $n ports)"; fail=1; }
  fi
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
        [ -n "$sg" ] && check_sg "security group $sg" "$sg"
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
cleanup_attach() { stop_watch; [ -z "$ATTACHED" ] || ( src_env; "$BRICK" detach "$ATTACHED" ) || true; }

create_dest_volume() {  # source-volume-id dir ; sets DVID, appends to volume-map
  local vid=$1 dir=$2 vj name size
  vj=$(src volume show "$vid" -f json)
  # Named after the instance, not the source volume: BFV volumes usually have no name, and the
  # device tells several volumes of one VM apart.
  name=$(dest_volume_name "$vid" "$dir")
  size=$(jq -r .size <<<"$vj")

  DVID=$(dst volume create "$name" --size "$size" ${VOLUME_TYPE:+--type "$VOLUME_TYPE"} \
         ${DEST_AZ:+--availability-zone "$DEST_AZ"} \
         --description "migrated from $vid" -f value -c id)
  wait_volume dst "$DVID" available
  rbd -c "$CEPH_CONF" --id "$RBD_USER" -p "$POOL" info "volume-$DVID" >/dev/null 2>&1 \
    || die "volume-$DVID not visible in pool $POOL: wrong RBD_POOL_MAP for type '${VOLUME_TYPE:-default}', or $RBD_USER lacks caps on that pool"
  echo "$vid $DVID" >> "$dir/volume-map"
  copy_volume_props "$vj" "$DVID"
}

copy_volume_props() {  # source-volume-json dest-volume-id ; boot and guest metadata the destination Nova needs
  local vj=$1 dvid=$2 bootable k v props
  bootable=$(jq -r .bootable <<<"$vj")
  [ "$bootable" = true ] || [ "$bootable" = True ] && dst volume set "$dvid" --bootable
  # Assigned first: a jq failure inside a process substitution would not stop the script.
  props=$(jq -r '(.volume_image_metadata // {}) | to_entries[]
      | select(.key | test("^(hw_|os_|hypervisor_type$|architecture$|vm_mode$)"))
      | [.key, (.value|tostring)] | @tsv' <<<"$vj")
  while IFS=$'\t' read -r k v; do
    [ -z "$k" ] || dst volume set "$dvid" --image-property "$k=$v"
  done <<<"$props"
}

CLONE_TIME=""
attach_clone() {  # source-volume-id ; sets CLONE, CLONE_TIME (UTC, clone request) and DEVICE
  local vid=$1
  CLONE_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  CLONE=$(src volume create "mig-clone-$vid" --source "$vid" -f value -c id)
  wait_volume src "$CLONE" available
  # stderr goes to a file, not /dev/null: brick_attach's own guard messages (multipath,
  # size/LUN mismatch) are the whole point of having it.
  DEVICE=$( src_env; "$BRICK" attach "$CLONE" 2>"$STATE_ROOT/brick-attach.err" )
  ATTACHED=$CLONE
  [ -b "$DEVICE" ] || die "no block device for clone $CLONE: $(tail -2 "$STATE_ROOT/brick-attach.err" 2>/dev/null | tr '\n' ' ')"
}

drop_clone() {
  ( src_env; "$BRICK" detach "$CLONE" 2>/dev/null )
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
  # If qemu-img fails, set -e exits and the EXIT trap (cleanup_attach) stops the watcher.
  progress_watch "$DEVICE" "$size" copy & WATCH_PID=$!
  qemu-img convert -f raw -O raw -n --target-is-zero -m "$COPY_STREAMS" -W "$DEVICE" "$img"
  stop_watch
  local took=$((SECONDS-t0))
  log "copy done in ${took}s ($((size*1024/(took>0?took:1))) MB/s logical)"

  if [ "$VERIFY" != none ]; then
    log "verify byte-for-byte"
    t0=$SECONDS
    progress_watch "$DEVICE" "$size" verify & WATCH_PID=$!
    qemu-img compare -f raw -F raw "$DEVICE" "$img" || die "verification failed for $vid"
    stop_watch
    log "verify OK in $((SECONDS-t0))s"
  else
    log "verify skipped (--verify none)"
  fi
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
  ( src_env; openstack image save --file "$dir/root.img" "$simg" )
  log "downloaded $(du -h "$dir/root.img" | cut -f1) in $((SECONDS-t0))s; upload to destination"
  t0=$SECONDS
  # Create and upload with plain REST. openstackclient is unusable for this step: with --file it
  # PATCHes attributes again after a long upload (Glance: 400 once active), and without --file it
  # reads image data from stdin whenever stdin is not a tty (nohup: /dev/null -> 415).
  local tok url
  tok=$( dst_env; openstack token issue -f value -c id )
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
      # Groups go by ID (an admin sees every project's "default"). This runs mid-cutover and the
      # port's job is holding the address, so a group that does not resolve is left out, not fatal.
      local net ip mac sg sgid sg_args=() missed=() err=$dir/sg-resolve.err
      net=$(jq -r ".[$i].network" "$dir/ports.json"); ip=$(jq -r ".[$i].ip" "$dir/ports.json")
      mac=$(jq -r ".[$i].mac" "$dir/ports.json")
      [ -n "$SRC_PID" ] || SRC_PID=$(src token issue -f value -c project_id 2>/dev/null) || true
      while read -r sg; do
        [ -n "$sg" ] || continue
        if sgid=$(src_sg_id "$sg" 2>"$err") && [ -n "$sgid" ]; then
          sg_args+=(--security-group "$sgid")
        else
          missed+=("$sg")
          log "WARN source security group $sg not resolved in source project ${SRC_PROJECT:-${SRC_PID:-?}}: $(sed -n 's/^ERROR: //p' "$err" | tail -1 | grep . || echo "not found")"
        fi
      done < <(jq -r ".[$i].security_groups[]" "$dir/ports.json")
      [ ${#sg_args[@]} -gt 0 ] || sg_args=(--no-security-group)
      pid=$(src port create "mig-reserve-$vm-$i" --network "$net" --mac-address "$mac" \
            --fixed-ip "ip-address=$ip" "${sg_args[@]}" -f value -c id) \
        || die "could not re-create a port holding $ip $mac on $net: that address is no longer reserved for rollback"
      log "Nova removed its own port; reserved $ip $mac again as $pid"
      # Rollback reattaches this very port to the source VM, so it comes back without these groups.
      [ ${#missed[@]} -eq 0 ] || log "WARN reserve port $pid has no security group ${missed[*]}; after a rollback re-add them: openstack port set$(printf ' --security-group %q' "${missed[@]}") $pid"
    fi
    echo "$pid" >> "$dir/source-ports"
    i=$((i+1))
  done
}

create_destination() {  # vm dir
  local vm=$1 dir=$2 n i nics=() flavor key vmname
  # The argument may be a server ID (used to disambiguate duplicate names); the destination
  # VM, its ports and volumes carry the real name, as the preflight duplicate guard assumes.
  vmname=$(jq -r .name "$dir/server.json")
  n=$(jq length "$dir/ports.json")
  [ "$n" = 0 ] || dst_project_id
  for ((i=0; i<n; i++)); do
    local net ip mac cidr subnet pname pid pinfo sg_args=() sg sgs=() sgid
    net=$(jq -r ".[$i].network" "$dir/ports.json"); ip=$(jq -r ".[$i].ip" "$dir/ports.json")
    mac=$(jq -r ".[$i].mac" "$dir/ports.json"); cidr=$(jq -r ".[$i].cidr" "$dir/ports.json")
    subnet=$(dst subnet list --network "$net" -f json | jq -r --arg c "$cidr" '.[] | select(.Subnet==$c) | .ID')
    if [ -n "$OV_SG" ]; then
      IFS=, read -ra sgs <<<"$OV_SG"
    else
      while read -r sg; do [ -n "$sg" ] && sgs+=("$sg"); done \
        < <(jq -r ".[$i].security_groups[]" "$dir/ports.json")
    fi
    # By ID: a name such as "default" exists once per project and an admin sees them all.
    for sg in "${sgs[@]}"; do
      sgid=$(dst_sg_id "$sg")
      [ -n "$sgid" ] || die "security group $sg not found in destination project ${DST_PROJECT:-$DST_PID}"
      sg_args+=(--security-group "$sgid")
    done
    [ ${#sg_args[@]} -gt 0 ] || sg_args=(--no-security-group)
    # Ports are named <vm>-<ip> but used and rolled back by ID: a name is not unique across
    # projects, and an admin's name lookup sees every project.
    case $IPMODE in
      same) pname="$vmname-$ip"
            pid=$(dst port create "$pname" --network "$net" --mac-address "$mac" \
              --fixed-ip "subnet=$subnet,ip-address=$ip" "${sg_args[@]}" -f value -c id);;
      # The address is unknown until Neutron allocates it: renamed right below.
      new)  pname="$vmname-pending$i"
            pid=$(dst port create "$pname" --network "$net" \
              --fixed-ip "subnet=$subnet" "${sg_args[@]}" -f value -c id);;
      addr) pname="$vmname-$IPADDR"
            pid=$(dst port create "$pname" --network "$net" \
              --fixed-ip "subnet=$subnet,ip-address=$IPADDR" "${sg_args[@]}" -f value -c id);;
    esac
    echo "$pid" >> "$dir/dst-ports"
    pinfo=$(dst port show "$pid" -f json | jq -r '"\(.fixed_ips[0].ip_address) \(.mac_address)"')
    if [ "$IPMODE" = new ]; then
      pname="$vmname-${pinfo%% *}"
      dst port set --name "$pname" "$pid"
    fi
    log "destination port $pname $pinfo"
    nics+=(--port "$pid")
  done

  flavor=${OV_FLAVOR:-$(jq -r '.flavor | if type=="object" then (.original_name // .name) else (. | split(" ")[0]) end' "$dir/server.json")}
  key=$(jq -r '.key_name // empty' "$dir/server.json")
  # Nova cannot assign another user's keypair: create without it; the key is inside the disk anyway.
  if [ -n "$key" ] && ! dst keypair show "$key" >/dev/null 2>&1; then
    log "WARN keypair $key not at destination: the VM is created without key_name (add it back later)"
    key=""
  fi
  local hint=()
  if [ -n "$SERVER_GROUP" ]; then
    local sgid
    sgid=$(dst server group list -f json | jq -r --arg g "$SERVER_GROUP" \
           '[.[] | select(.Name==$g or .ID==$g) | .ID] | if length==1 then .[0] else empty end')
    [ -n "$sgid" ] || die "server group $SERVER_GROUP is missing or ambiguous in the destination project"
    hint=(--hint "group=$sgid")
  fi
  # Set by preflight only with --keep-server-group and a source VM that is in a group.
  if [ -n "$KSG_DST" ]; then
    keep_sg_ensure "$dir"
    hint=(--hint "group=$KSG_ID")
  fi

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

  dst server create "$vmname" --flavor "$flavor" "${nics[@]}" "${args[@]}" --user-data "$ud" \
    ${DEST_AZ:+--availability-zone "$DEST_AZ"} \
    ${key:+--key-name "$key"} "${hint[@]}" "${prop_args[@]}" -f value -c id > "$dir/dst-server-id"
  wait_server dst "$(cat "$dir/dst-server-id")" ACTIVE
}

cmd_migrate() {
  local vm=$1 dir=$STATE_ROOT/$1 sid vid
  [ ! -e "$dir/volume-map" ] || die "state exists in $dir: run rollback first"
  export_definition "$vm" "$dir"
  detect_boot "$dir"
  preflight "$dir"
  printf 'MODE=%s IPMODE=%s IPADDR=%s VOLUME_TYPE=%s POOL=%s VERIFY=%s FLAVOR=%s SG=%s EPHEMERAL=%s AZ=%s\n' \
    "$MODE" "$IPMODE" "$IPADDR" "$VOLUME_TYPE" "$POOL" "$VERIFY" "$OV_FLAVOR" "$OV_SG" "$EPHEMERAL" "$DEST_AZ" > "$dir/options"
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
  # Everything that still mutates the source server through Nova (port detach,
  # ephemeral snapshot) must happen BEFORE the lock: Nova's lock blocks these
  # calls for non-admin credentials (admins silently bypass it, which is why
  # the admin-only lab never caught this ordering).
  # --volumes-only never detaches: no destination port will take over the MAC+IP.
  if [ "$VOLUMES_ONLY" = 0 ] && [ "$IPMODE" = same ] && [ "$KEEP_PORTS" = 0 ]; then
    detach_source_ports "$vm" "$dir" "$sid"
  fi
  [ "$EPHEMERAL" = 0 ] || migrate_ephemeral_root "$vm" "$dir" "$sid"
  src server lock "$sid"
  if [ "$VOLUMES_ONLY" = 1 ]; then
    log "source stopped and locked; ports left attached (--volumes-only creates no destination port)"
  elif [ "$IPMODE" != same ]; then
    log "source stopped and locked; ports left attached (--ip $IPMODE keeps them out of conflict)"
  elif [ "$KEEP_PORTS" = 1 ]; then
    log "WARN --keep-source-ports: source ports stay attached with the same MAC+IP as the destination VM. Only SHUTOFF+lock keeps it off the wire — powering it on causes split-brain. Do not start it; delete it once the migration is accepted."
  else
    log "source stopped, its ports detached, now locked"
  fi

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

  if [ "$VOLUMES_ONLY" = 1 ]; then
    log "volumes copied; source down for $((SECONDS-t_start))s so far"
  else
    create_destination "$vm" "$dir"
    log "destination VM ACTIVE; control-plane downtime so far $((SECONDS-t_start))s"
  fi
  trap - EXIT

  if [ "$START_SOURCE" = 1 ]; then
    if [ "$VOLUMES_ONLY" = 1 ]; then
      log "WARN --start-source: the source VM runs again; its data diverges from the copied volumes from now on"
    else
      log "WARN --start-source: both VMs will run with the same hostname and SSH host key; their data diverges from this cutover"
    fi
    src server unlock "$sid"
    src server start "$sid"
    wait_server src "$sid" ACTIVE
    log "source VM started again"
  fi
  if [ "$VOLUMES_ONLY" = 1 ]; then
    echo "Volumes migrated (--volumes-only, no destination server or port created):"
    local svid dvid
    while read -r svid dvid; do
      echo "  $(dest_volume_name "$svid" "$dir")  $dvid  (from $svid)"
    done < "$dir/volume-map"
    echo "Source VM: $( [ "$START_SOURCE" = 1 ] && echo "running again (--start-source)" || echo "kept SHUTOFF and locked" ). Rollback: $0 rollback $vm"
    return
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
  # Other migrated VMs may be members; an empty group is harmless and a later migrate reuses it.
  [ ! -s "$dir/dst-server-group" ] || log "INFO destination server group kept ($(cat "$dir/dst-server-group")): rollback never deletes a server group"
  # Destination ports go by the IDs create_destination recorded, whatever their name. The
  # mig-<arg>-<i> name sweep below also covers state written before dst-ports existed.
  local i n pname pid
  if [ -s "$dir/dst-ports" ]; then
    while read -r pid; do
      [ -n "$pid" ] || continue
      if dst port show "$pid" >/dev/null 2>&1; then
        dst port delete "$pid"
        log "deleted destination port $pid"
      fi
    done < "$dir/dst-ports"
  fi
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
    [ -e "/var/lib/mig-attach/$cid.json" ] && ( src_env; "$BRICK" detach "$cid" 2>/dev/null ) || true
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

# --- migrate-volume: one available volume, no server ----------------------------
# The source is only read through a temporary clone, so it stays available and untouched.
# State: $STATE_ROOT/vol-<source-id>. The copy keeps the source name; since Cinder names are
# not unique, an earlier copy is recognised by its description "migrated from <source-id>".
# Runs before parse_options, whose own --start-source/--volumes-only messages would mislead here.
volume_options_only() {  # raw options ; refuse the options that only make sense with a server
  local a prev="" bad=""
  for a in "$@"; do
    case $a in --ip|--flavor|--security-group|--server-group|--keep-server-group|--start-source|--keep-source-ports|--volumes-only) bad+=" $a";; esac
    [ "$prev" != --mode ] || [ "$a" != live ] || bad+=" --mode live"
    prev=$a
  done
  [ -z "$bad" ] || die "migrate-volume copies one available volume and creates no server or port:$bad cannot apply (it takes --volume-type, --az and --verify)"
}

resolve_volume() {  # name-or-id ; prints the source volume ID (source project), nothing when unknown
  local ids n
  ids=$(src volume list --name "$1" -f value -c ID)
  n=$(grep -c . <<<"$ids" || true)
  [ "$n" -le 1 ] || die "$n volumes are named '$1' in the source project: ${ids//$'\n'/ } — re-run with the volume ID"
  if [ "$n" = 1 ]; then echo "$ids"
  elif [[ $1 =~ ^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$ ]]; then echo "$1"; fi
}

dst_volumes_described() {  # description ; IDs of the destination project's volumes carrying it
  # openstackclient has no description column or filter: ask the SDK it is built on.
  ( dst_env; python3 - "$1" <<'EOF'
import sys
import openstack
for v in openstack.connect().block_storage.volumes(details=True):
    if v.description == sys.argv[1]:
        print(v.id)
EOF
  )
}

volume_preflight() {  # dir ; destination checks for the volume in $dir/volume.json
  local dir=$1 vid dids fail=0
  check() { if eval "$2" >/dev/null 2>&1; then echo "  OK    $1"; else echo "  FAIL  $1"; fail=1; fi; }
  vid=$(jq -r .id "$dir/volume.json")
  echo "Destination preflight (migrate-volume type=${VOLUME_TYPE:-default} pool=$POOL${DEST_AZ:+ az=$DEST_AZ}):"
  if [ "$IN_USE" = 1 ]; then
    if [ "$(jq -r .status "$dir/volume.json")" = available ]; then
      echo "  INFO  --in-use ignored: $vid is available, copied as usual"
    else
      echo "  INFO  $vid is in-use: copied from a clone taken while attached (crash-consistent); its VM keeps running"
      check "volume not multiattach" "jq -e '.multiattach != true' '$dir/volume.json'"
    fi
  fi
  [ -z "$DEST_AZ" ] || check "volume AZ $DEST_AZ" "dst availability zone list --volume -f value -c \"Zone Name\" | grep -qx -- \"$DEST_AZ\""
  [ -z "$VOLUME_TYPE" ] || check "volume type $VOLUME_TYPE" "dst volume type show '$VOLUME_TYPE'"
  check "volume not encrypted" "jq -e '.encrypted != true' '$dir/volume.json'"
  if ! dids=$(dst_volumes_described "migrated from $vid"); then
    echo "  FAIL  cannot list destination volumes"; fail=1
  elif [ -n "$dids" ]; then
    echo "  FAIL  destination already has a copy of $vid: ${dids//$'\n'/ } — inspect it; roll back the run that made it ($0 rollback $vid, on the host holding its state) or delete it, then re-run"
    fail=1
  else
    echo "  OK    no earlier copy of $vid at the destination"
  fi
  check "RBD access to pool $POOL" "rbd -c '$CEPH_CONF' --id '$RBD_USER' -p '$POOL' ls"
  check "iSCSI daemon on transfer host" "systemctl is-active iscsid || systemctl start iscsid"
  [ "${MIG_MULTIPATH:-0}" != 1 ] || check "multipathd on transfer host" "systemctl is-active multipathd"
  [ "$fail" = 0 ]
}

create_volume_copy() {  # dir ; creates the destination volume for $dir/volume.json, sets DVID
  local dir=$1 vj vid name args
  vj=$(cat "$dir/volume.json")
  vid=$(jq -r .id <<<"$vj"); name=$(jq -r '.name // empty' <<<"$vj")
  args=(--size "$(jq -r .size <<<"$vj")" --description "migrated from $vid" -f value -c id)
  [ -z "$VOLUME_TYPE" ] || args+=(--type "$VOLUME_TYPE")
  [ -z "$DEST_AZ" ] || args+=(--availability-zone "$DEST_AZ")
  # The source name verbatim, an empty one included (Cinder accepts it); the ID only as fallback.
  if [ -n "$name" ]; then
    DVID=$(dst volume create "$name" "${args[@]}")
  elif ! DVID=$(dst volume create "" "${args[@]}" 2>"$dir/create.err"); then
    log "WARN source volume $vid has no name and the destination refused an empty one ($(tail -1 "$dir/create.err")): the copy is named $vid"
    DVID=$(dst volume create "$vid" "${args[@]}")
  fi
  # Recorded before the wait, so that rollback also removes a copy that never became available.
  echo "$vid $DVID" >> "$dir/volume-map"
  wait_volume dst "$DVID" available
  rbd -c "$CEPH_CONF" --id "$RBD_USER" -p "$POOL" info "volume-$DVID" >/dev/null 2>&1 \
    || die "volume-$DVID not visible in pool $POOL: wrong RBD_POOL_MAP for type '${VOLUME_TYPE:-default}', or $RBD_USER lacks caps on that pool"
  copy_volume_props "$vj" "$DVID"
}

cmd_migrate_volume() {  # volume name or ID
  local arg=$1 vid dir vj status name size
  vid=$(resolve_volume "$arg")
  [ -n "$vid" ] || die "no volume named '$arg' in the source project"
  dir=$STATE_ROOT/vol-$vid
  [ ! -e "$dir/volume-map" ] || die "state exists in $dir: run rollback first ($0 rollback $vid)"
  vj=$(src volume show "$vid" -f json)
  status=$(jq -r .status <<<"$vj")
  case $status in
    available) ;;
    in-use) [ "$IN_USE" = 1 ] || die "volume $arg ($vid) is in-use: an attached volume goes with its VM ($0 migrate <vm> --volumes-only), or copy it live with --in-use (crash-consistent)";;
    *) die "volume $arg ($vid) is $status: migrate-volume copies available volumes only";;
  esac
  mkdir -p "$dir"; chmod 700 "$dir"
  printf '%s\n' "$vj" > "$dir/volume.json"
  name=$(jq -r '.name // empty' <<<"$vj"); size=$(jq -r .size <<<"$vj")
  echo "Volume ${name:-(no name)} $vid  ${size}GB bootable=$(jq -r .bootable <<<"$vj") type=$(jq -r '.type // "-"' <<<"$vj")"
  volume_preflight "$dir"
  printf 'VOLUME_TYPE=%s POOL=%s VERIFY=%s AZ=%s SRC_PROJECT=%s DST_PROJECT=%s\n' \
    "$VOLUME_TYPE" "$POOL" "$VERIFY" "$DEST_AZ" "${SRC_PROJECT:-}" "${DST_PROJECT:-}" > "$dir/options"
  : > "$dir/volume-map"
  trap cleanup_attach EXIT
  create_volume_copy "$dir"
  log "destination volume $DVID created; copying from a clone of $vid"
  copy_volume_cold "$vid" "$DVID" "$size" "$dir"
  trap - EXIT
  if [ "$status" = in-use ]; then
    echo "$CLONE_TIME" > "$dir/clone-time"
    echo "Volume copied crash-consistent as of $CLONE_TIME: data written after that moment"
    echo "is NOT in the copy (like a sudden power-off; a busy DB will need crash recovery on first mount)."
    echo "Source VM and volume untouched."
  fi
  echo "Volume migrated: ${name:-(no name)} $vid -> $DVID. Source volume untouched (still $status). Rollback: $0 rollback $vid"
}

drop_volume() {  # src|dst volume-id label ; deletes it once settled; gone is fine, in-use is refused
  local cloud=$1 id=$2 label=$3 s err=$STATE_ROOT/volume-show.err
  if ! s=$($cloud volume show "$id" -f value -c status 2>"$err"); then
    grep -qiE 'no volume with a name or id|not found|could not be found' "$err" \
      || die "cannot read $label $id: $(tail -1 "$err")"
    log "$label $id already gone"; return 0
  fi
  case $s in
    in-use) die "$label $id is in-use: detach it first, then re-run the rollback";;
    error*) ;;
    *) wait_volume "$cloud" "$id" available 300;;
  esac
  $cloud volume delete "$id"
  log "deleted $label $id"
}

cmd_rollback_volume() {  # volume name, ID or vol-<id> ; deletes the copy, never touches the source
  local arg=$1 dir vid dvid cid
  if [ -e "$STATE_ROOT/$arg/volume.json" ]; then dir=$STATE_ROOT/$arg
  elif [ -e "$STATE_ROOT/vol-$arg/volume.json" ]; then dir=$STATE_ROOT/vol-$arg
  else
    vid=$(resolve_volume "$arg")
    dir=$STATE_ROOT/vol-$vid
    [ -n "$vid" ] && [ -e "$dir/volume.json" ] \
      || die "no state for $arg: no VM state in $STATE_ROOT/$arg and no migrate-volume state for a source volume of that name or ID"
  fi
  vid=$(jq -r .id "$dir/volume.json")
  if [ -e "$dir/volume-map" ]; then
    for dvid in $(awk '{print $2}' "$dir/volume-map"); do
      drop_volume dst "$dvid" "destination volume"
    done
  fi
  # A run that died mid-copy leaves its clone behind, possibly still attached to this host.
  for cid in $(src volume list --name "mig-clone-$vid" -f value -c ID); do
    [ ! -e "/var/lib/mig-attach/$cid.json" ] || ( src_env; "$BRICK" detach "$cid" 2>/dev/null ) || true
    drop_volume src "$cid" "leftover clone"
  done
  mv "$dir" "$dir.rolled-back.$(date +%s)"
  log "rollback done; source volume $vid was never modified"
}

[ $# -ge 2 ] || { sed -n '2,50p' "$0"; exit 2; }
CMD=$1 VM=$2; shift 2
case $CMD in
  plan|migrate) parse_options "$@"
                [ "$IN_USE" = 0 ] || die "--in-use belongs to migrate-volume: plan and migrate move a VM with its volumes (--mode live keeps it running until the cutover)"
                POOL=$(pool_for_type); check_project_scope;;
  migrate-volume) volume_options_only "$@"; parse_options "$@"; POOL=$(pool_for_type); check_project_scope;;
  rollback) [ $# -eq 0 ] || die "rollback takes no options"; check_project_scope;;
  *) die "unknown command $CMD";;
esac
case $CMD in
  plan) cmd_plan "$VM";;
  migrate) cmd_migrate "$VM";;
  migrate-volume) cmd_migrate_volume "$VM";;
  # A VM state under that name wins, exactly as before; anything else is taken as a volume.
  rollback) if [ -e "$STATE_ROOT/$VM/server.json" ]; then cmd_rollback "$VM"; else cmd_rollback_volume "$VM"; fi;;
esac
