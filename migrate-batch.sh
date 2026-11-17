#!/usr/bin/env bash
# Batch front-end for migrate-vm.sh: select source VMs by host, project or list (or a project's
# volumes), plan them all, confirm once, then migrate one at a time, each in its own project.
#
#   migrate-batch.sh [--host h1[,h2...]] [--project p1[,p2...]] [--vm v1[,v2...]] [--vm-file <file>]
#                    [--volumes attached|available|all] [--plan-only] [--yes] [--continue-on-error]
#                    [-- <options passed to every migrate-vm.sh call>]
#
#   --host <hosts>        source compute hosts (Host column of "openstack compute service list")
#   --project <projects>  source projects, name or ID
#   --vm <vms>            VM names or IDs; --vm-file <file> adds one per line (# comments, blank
#                         lines skipped). Selectors intersect; at least one is required.
#   --volumes attached    copy the volumes of the selected VMs, create no destination VM
#                         (migrate-vm.sh migrate <id> --volumes-only); needs --project or --vm
#   --volumes available   copy every available volume of --project (migrate-vm.sh migrate-volume)
#   --volumes all         attached first, then available; needs --project
#   --plan-only           list and plan only, change nothing; exit 1 if a plan fails
#   --yes                 no confirmation prompt (required when stdin is not a terminal)
#   --continue-on-error   go on after a failure (default: stop at the first one)
#
# The source openrc lists the VMs with --all-projects (admin); each VM then runs with
# SRC_PROJECT/DST_PROJECT set to its own project. Destination project: the same name, unless
# PROJECT_MAP_FILE has a "source destination" line for it. SRC_RC/DST_RC/PROJECT_MAP_FILE come
# from the environment or $MIG_CONF, as for migrate-vm.sh. Logs: /var/log/mig-batch/<time>/.
set -euo pipefail

MIG_CONF=${MIG_CONF:-/opt/mig/migrate.conf}
if [ -r "$MIG_CONF" ]; then  # same precedence as migrate-vm.sh: the environment wins over the file
  declare -A _env_saved=()
  for _v in SRC_RC DST_RC PROJECT_MAP_FILE; do [ -z "${!_v+x}" ] || _env_saved[$_v]=${!_v}; done
  source "$MIG_CONF"
  for _v in "${!_env_saved[@]}"; do printf -v "$_v" '%s' "${_env_saved[$_v]}"; done
  unset _v _env_saved
fi
SRC_RC=${SRC_RC:-/opt/mig/src/admin-openrc.sh}
DST_RC=${DST_RC:-/opt/mig/dst/admin-openrc.sh}
PROJECT_MAP_FILE=${PROJECT_MAP_FILE:-}
STATE_ROOT=/var/lib/mig-migrate
MIGRATE_VM=${MIGRATE_VM:-$(dirname "$(readlink -f "$0")")/migrate-vm.sh}
LOG_DIR=/var/log/mig-batch/$(date +%Y%m%d-%H%M%S)

source /opt/mig-venv/bin/activate
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY

src() { ( source "$SRC_RC"; openstack "$@" ); }
log() { echo "[$(date +%T)] $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }
trim() { local s=$1; s=${s#"${s%%[![:space:]]*}"}; echo "${s%"${s##*[![:space:]]}"}"; }
is_uuid() { [[ $1 =~ ^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$ ]]; }
add_list() {  # array-name comma-separated-values
  local -n _to=$1; local _x _xs
  IFS=, read -ra _xs <<<"$2"
  for _x in "${_xs[@]}"; do _x=$(trim "$_x"); [ -z "$_x" ] || _to+=("$_x"); done
}

# --- options -------------------------------------------------------------------
HOSTS=() PROJECTS=() VMS=() VM_FILE="" SCOPE="" PLAN_ONLY=0 YES=0 CONTINUE=0 PASS=()
[ $# -gt 0 ] || { sed -n '2,24p' "$0"; exit 2; }
while [ $# -gt 0 ]; do
  case $1 in
    --host|--project|--vm|--vm-file|--volumes) [ -n "${2:-}" ] || die "$1 needs a value";;&
    --host)    add_list HOSTS "$2"; shift 2;;
    --project) add_list PROJECTS "$2"; shift 2;;
    --vm)      add_list VMS "$2"; shift 2;;
    --vm-file) VM_FILE=$2; shift 2;;
    --volumes) SCOPE=$2; shift 2;;
    --plan-only) PLAN_ONLY=1; shift;;
    --yes) YES=1; shift;;
    --continue-on-error) CONTINUE=1; shift;;
    --) shift; PASS=("$@"); break;;
    *) die "unknown option $1 (options for migrate-vm.sh go after --)";;
  esac
done
if [ -n "$VM_FILE" ]; then
  [ -r "$VM_FILE" ] || die "cannot read --vm-file $VM_FILE"
  while IFS= read -r line || [ -n "$line" ]; do
    line=$(trim "${line%%#*}")
    [ -z "$line" ] || VMS+=("$line")
  done < "$VM_FILE"
fi
declare -A _seen=(); _uniq=()
for v in "${VMS[@]}"; do [ -n "${_seen[$v]:-}" ] || { _seen[$v]=1; _uniq+=("$v"); }; done
VMS=("${_uniq[@]}")

[ ${#HOSTS[@]} -gt 0 ] || [ ${#PROJECTS[@]} -gt 0 ] || [ ${#VMS[@]} -gt 0 ] \
  || die "select something: --host, --project, --vm or --vm-file"
case $SCOPE in
  "") ;;
  attached) [ ${#PROJECTS[@]} -gt 0 ] || [ ${#VMS[@]} -gt 0 ] || die "--volumes attached needs --project or --vm/--vm-file (--host only narrows them)";;
  available|all)
    [ ${#PROJECTS[@]} -gt 0 ] || die "--volumes $SCOPE needs --project"
    [ ${#HOSTS[@]} -eq 0 ] || die "--host filters VMs: use it with --volumes attached only"
    [ "$SCOPE" = all ] || [ ${#VMS[@]} -eq 0 ] || die "--volumes available lists volumes, not VMs: drop --vm/--vm-file";;
  *) die "--volumes takes attached, available or all";;
esac
[ -z "$SCOPE" ] || [[ " ${PASS[*]} " != *" --mode live "* ]] || die "--volumes copies cold only: drop --mode live"
[ -x "$MIGRATE_VM" ] || die "migrate-vm.sh not found at $MIGRATE_VM (set MIGRATE_VM)"
for rc in "$SRC_RC" "$DST_RC"; do
  [[ $(source "$rc" >/dev/null 2>&1; echo "${OS_AUTH_TYPE:-}") != *applicationcredential* ]] \
    || die "$rc is an application credential: a batch runs each VM in its own project and needs a password openrc"
done

declare -A PMAP=()  # source project name -> destination project name
if [ -n "$PROJECT_MAP_FILE" ] && [ -r "$PROJECT_MAP_FILE" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    a="" b="" extra=""
    read -r a b extra <<<"$(trim "${line%%#*}")" || true
    [ -n "$a" ] || continue
    [ -n "$b" ] && [ -z "$extra" ] || die "$PROJECT_MAP_FILE: expected 'source-project destination-project', got: $line"
    PMAP[$a]=$b
  done < "$PROJECT_MAP_FILE"
fi
declare -A PNAME=()
proj_name() { [ -n "${PNAME[$1]:-}" ] || PNAME[$1]=$(src project show "$1" -f value -c name); PN=${PNAME[$1]}; }

# --- rows: one per VM or volume ---------------------------------------------------
K=() ID=() NAME=() ST=() SP=() DP=() RES=() INFO=()
add_row() { K+=("$1"); ID+=("$2"); NAME+=("$3"); ST+=("$4"); SP+=("$5"); DP+=("$6"); RES+=("$7"); INFO+=("${8:-}"); }
show() {  # title
  echo; echo "$1"
  printf '%-4s %-36s %-24s %-9s %-32s %s\n' KIND ID NAME STATUS "PROJECT src -> dst" RESULT
  for i in "${!ID[@]}"; do
    printf '%-4s %-36s %-24s %-9s %-32s %s\n' "${K[$i]}" "${ID[$i]}" "${NAME[$i]:--}" "${ST[$i]}" "${SP[$i]} -> ${DP[$i]}" "${RES[$i]}"
  done
}
count() { local n=0 r; for r in "${RES[@]}"; do [[ $r == $1 ]] && n=$((n+1)); done; echo "$n"; }
hints() {  # how to undo a failed run or an earlier state; rollback goes by the recorded ID or name
  local i
  for i in "${!ID[@]}"; do
    case ${RES[$i]} in FAIL|"SKIP(state-exists)") echo "  ${NAME[$i]:-${ID[$i]}}: inspect, then to undo:" \
      "SRC_PROJECT=${SP[$i]} DST_PROJECT=${DP[$i]} $MIGRATE_VM rollback ${INFO[$i]}";; esac
  done
}

mkdir -p "$LOG_DIR"
log "logs in $LOG_DIR; project map: ${PROJECT_MAP_FILE:-none} (${#PMAP[@]} entries, other projects keep their name)"
[ -z "$PROJECT_MAP_FILE" ] || [ -r "$PROJECT_MAP_FILE" ] || log "WARN $PROJECT_MAP_FILE is not readable: every project keeps its name"

if [ "$SCOPE" != available ]; then
  n=0
  if [ ${#HOSTS[@]} -gt 0 ]; then
    for h in "${HOSTS[@]}"; do
      n=$((n+1)); src server list --all-projects --long --host "$h" -f json > "$LOG_DIR/list-$n.json"
      [ "$(jq length "$LOG_DIR/list-$n.json")" != 0 ] || log "WARN no VM on host $h (the name must match the Host column of 'openstack compute service list')"
    done
  elif [ ${#PROJECTS[@]} -gt 0 ]; then
    for p in "${PROJECTS[@]}"; do
      n=$((n+1)); src server list --all-projects --long --project "$p" -f json > "$LOG_DIR/list-$n.json"
    done
  else
    for v in "${VMS[@]}"; do
      n=$((n+1)); f=$LOG_DIR/list-$n.json
      src server list --all-projects --long --name "^$(sed 's/[][\.*^$(){}?+|]/\\&/g' <<<"$v")\$" -f json > "$f"
      if [ "$(jq length "$f")" = 0 ] && is_uuid "$v"; then
        src server show "$v" -f json 2>/dev/null \
          | jq '[{ID: .id, Name: .name, Status: .status, "Project ID": (.project_id // .tenant_id // "")}]' > "$f" \
          || echo '[]' > "$f"
      fi
    done
  fi
  jq -s 'add // [] | map({id: .ID, name: .Name, status: .Status, project_id: (.["Project ID"] // "")})
         | reduce .[] as $r ([]; if any(.[]; .id == $r.id) then . else . + [$r] end)' \
    "$LOG_DIR"/list-*.json > "$LOG_DIR/vms.json"

  if [ ${#VMS[@]} -gt 0 ]; then  # keep the listed VMs only; a name twice in one project needs the ID
    : > "$LOG_DIR/selected.jsonl"
    for v in "${VMS[@]}"; do
      m=$(jq -c --arg v "$v" '[.[] | select(.id == $v or .name == $v)]' "$LOG_DIR/vms.json")
      if [ "$m" = "[]" ]; then
        add_row vm - "$v" - - - "SKIP(not-found)"
      elif jq -e 'group_by(.project_id) | any(length > 1)' <<<"$m" >/dev/null; then
        while IFS=$'\x1f' read -r -u 3 id name status; do
          add_row vm "$id" "$name" "$status" - - "SKIP(ambiguous-name: use the ID)"
        done 3< <(jq -r '.[] | [.id, .name, .status] | join("\u001f")' <<<"$m")
      else
        jq -c '.[]' <<<"$m" >> "$LOG_DIR/selected.jsonl"
      fi
    done
    jq -s 'reduce .[] as $r ([]; if any(.[]; .id == $r.id) then . else . + [$r] end)' \
      "$LOG_DIR/selected.jsonl" > "$LOG_DIR/vms.json"
  fi

  declare -A WANT_PID=()  # --host and --project together: keep those projects' VMs
  if [ ${#HOSTS[@]} -gt 0 ]; then
    for p in "${PROJECTS[@]}"; do pid=$(src project show "$p" -f value -c id); WANT_PID[$pid]=1; done
  fi
  while IFS=$'\x1f' read -r -u 3 id name status pid; do
    # Clients that print no "Project ID" column in "server list --long": ask the server itself.
    [ -n "$pid" ] || pid=$(src server show "$id" -f json | jq -r '.project_id // .tenant_id // empty')
    [ -n "$pid" ] || die "cannot find the project of VM $id"
    [ ${#WANT_PID[@]} -eq 0 ] || [ -n "${WANT_PID[$pid]:-}" ] || continue
    proj_name "$pid"
    r=selected info=""
    case $status in
      ACTIVE|SHUTOFF)
        for s in "$id" "$name"; do
          [ ! -e "$STATE_ROOT/$s/volume-map" ] || { r="SKIP(state-exists)" info=$s; break; }
        done;;
      *) r="SKIP(status)";;
    esac
    add_row vm "$id" "$name" "$status" "$PN" "${PMAP[$PN]:-$PN}" "$r" "$info"
  done 3< <(jq -r '.[] | [.id, .name, .status, .project_id] | join("\u001f")' "$LOG_DIR/vms.json")
fi

if [ "$SCOPE" = available ] || [ "$SCOPE" = all ]; then
  for p in "${PROJECTS[@]}"; do
    pj=$(src project show "$p" -f json)
    sp=$(jq -r .name <<<"$pj"); f=$LOG_DIR/volumes-$sp.json
    src volume list --project "$(jq -r .id <<<"$pj")" -f json > "$f"
    log "$sp: $(jq '[.[] | select(.Status == "in-use")] | length' "$f") in-use volume(s) not listed — they go with their VM (--volumes attached)"
    while IFS=$'\x1f' read -r -u 3 id name status; do
      r=selected info=""
      case $status in
        in-use) continue;;
        available)
          if [[ $name == mig-clone-* ]]; then r="SKIP(mig-clone)"  # a leftover temporary clone
          elif [ -e "$STATE_ROOT/vol-$id/volume-map" ]; then r="SKIP(state-exists)" info=$id; fi;;
        *) r="SKIP(status)";;
      esac
      add_row vol "$id" "$name" "$status" "$sp" "${PMAP[$sp]:-$sp}" "$r" "$info"
    done 3< <(jq -r '.[] | [.ID, (.Name // ""), .Status] | join("\u001f")' "$f")
  done
fi

[ ${#ID[@]} -gt 0 ] || { log "nothing selected"; exit 0; }
show "Selected:"

# --- plan --------------------------------------------------------------------------
VMX=(); [ -z "$SCOPE" ] || VMX=(--volumes-only)
for i in "${!ID[@]}"; do
  [ "${RES[$i]}" = selected ] || continue
  if [ "${K[$i]}" = vol ]; then RES[$i]=READY; continue; fi  # migrate-volume checks at run time
  f=$LOG_DIR/plan-${ID[$i]}.log
  if env SRC_PROJECT="${SP[$i]}" DST_PROJECT="${DP[$i]}" \
       "$MIGRATE_VM" plan "${ID[$i]}" "${VMX[@]}" "${PASS[@]}" >"$f" 2>&1 </dev/null; then
    RES[$i]=PLAN-OK
  elif grep -q 'ephemeral VM has no volume to migrate' "$f"; then
    RES[$i]="SKIP(ephemeral-root)"
  else
    RES[$i]=PLAN-FAIL INFO[$i]=$(grep -E '^ +FAIL|^ERROR' "$f" || tail -1 "$f")
  fi
  log "plan ${NAME[$i]} (${ID[$i]}): ${RES[$i]}"
done
show "Plan:"
for i in "${!ID[@]}"; do
  [ "${RES[$i]}" = PLAN-FAIL ] || continue
  echo "${NAME[$i]} (${ID[$i]}), full output in $LOG_DIR/plan-${ID[$i]}.log:"
  sed 's/^ */    /' <<<"${INFO[$i]}"
done
hints
if [ "$PLAN_ONLY" = 1 ]; then
  [ "$(count PLAN-FAIL)" = 0 ] && exit 0 || exit 1
fi

# --- migrate, one at a time ---------------------------------------------------------
todo=$(( $(count PLAN-OK) + $(count READY) ))
if [ "$todo" -gt 0 ] && [ "$YES" = 0 ]; then
  [ -t 0 ] || die "stdin is not a terminal: re-run with --yes to migrate without confirmation"
  [ "$(count PLAN-FAIL)" = 0 ] || [ "$CONTINUE" = 1 ] || echo "Note: without --continue-on-error the run stops at the first PLAN-FAIL row."
  read -r -p "Migrate $todo item(s) one by one${PASS[*]:+ with: ${PASS[*]}}? Type yes to continue: " ans
  [ "$ans" = yes ] || die "not confirmed, nothing changed"
fi
# migrate-volume refuses server options: keep only those that apply to a lone volume.
VOLPASS=() DROPPED=()
for ((j=0; j<${#PASS[@]}; j++)); do
  case ${PASS[$j]} in
    --volume-type|--az|--verify|--mode) VOLPASS+=("${PASS[$j]}" "${PASS[$((j+1))]:-}"); j=$((j+1));;
    --start-source|--keep-source-ports|--volumes-only) DROPPED+=("${PASS[$j]}");;
    *) DROPPED+=("${PASS[$j]} ${PASS[$((j+1))]:-}"); j=$((j+1));;
  esac
done
[ "$(count READY)" = 0 ] || [ ${#DROPPED[@]} -eq 0 ] || log "available volumes: ignoring VM-only options ${DROPPED[*]}"

STOP=0
for i in "${!ID[@]}"; do
  case ${RES[$i]} in PLAN-OK|READY|PLAN-FAIL) ;; *) continue;; esac
  if [ "$STOP" = 1 ]; then RES[$i]="SKIP(not-run)"; continue; fi
  if [ "${RES[$i]}" = PLAN-FAIL ]; then
    RES[$i]="FAIL(plan)"
  else
    if [ "${K[$i]}" = vm ]; then args=(migrate "${ID[$i]}" "${VMX[@]}" "${PASS[@]}")
    else args=(migrate-volume "${ID[$i]}" "${VOLPASS[@]}"); fi
    f=$LOG_DIR/migrate-${ID[$i]}.log
    log "=== ${K[$i]} ${NAME[$i]:-(no name)} (${ID[$i]}) ${SP[$i]} -> ${DP[$i]}"
    if env SRC_PROJECT="${SP[$i]}" DST_PROJECT="${DP[$i]}" "$MIGRATE_VM" "${args[@]}" </dev/null 2>&1 | tee "$f"; then
      RES[$i]=OK
      key=$(sed -n 's/.*WARN keypair \([^ ]*\) not at destination.*/\1/p' "$f" | head -1)
      [ -z "$key" ] || RES[$i]="OK(no key_name: $key)"
    else
      RES[$i]=FAIL INFO[$i]=${ID[$i]}
    fi
  fi
  [[ ${RES[$i]} != FAIL* ]] || [ "$CONTINUE" = 1 ] || STOP=1
done

show "Summary:"
hints
echo "Totals: OK $(count 'OK*')  FAIL $(count 'FAIL*')  SKIP $(count 'SKIP*')  (logs: $LOG_DIR)"
[ "$(count 'FAIL*')" = 0 ] || exit 1
