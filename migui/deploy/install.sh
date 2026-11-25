#!/usr/bin/env bash
# Install (or update, or remove) the migrate web UI on mig-host. Idempotent: run it again after a 'git pull'.
#
#   sudo migui/deploy/install.sh                 install/update, enable and (re)start both units
#   sudo migui/deploy/install.sh --no-start      install/update only
#   sudo migui/deploy/install.sh --uninstall     stop and remove the units and /opt/migui (keeps jobs and audit)
#   sudo migui/deploy/install.sh --purge         --uninstall + /var/lib/migui, /var/log/migui and the migui user
#
# MIGRATE=<path of the migrate entry point> (default: /opt/mig-venv/bin/migrate, else 'command -v migrate').
# Layout (DEPLOY.md section 6): /opt/migui/{bin,lib/migui} root 0755 (bin: migui-runner, migui-web, migui-passwd);
# /var/lib/migui root:migui 0750 (users 0640 root:migui, made by migui-passwd; jobs/ root 0700); /var/log/migui
# root:migui 0750 with audit.jsonl root:migui 0620 (+a when the fs supports it);
# /run/migui root:migui 0750 (runner.sock 0660), made by systemd RuntimeDirectory= and fixed up by the runner.
set -eu

audit_setup() {  # audit_setup <file> <group>: root:<group> 0620, append-only when the fs allows; safe to re-run
  local f=$1 g=$2 gid want have name
  name=$(basename "$f")
  gid=$(getent group "$g" | cut -d: -f3)
  [ -e "$f" ] || install -m 0620 -o root -g "$g" /dev/null "$f"
  want="0:$gid:620"
  have=$(stat -c '%u:%g:%a' "$f")
  if [ "$have" != "$want" ]; then
    # an append-only file refuses chown/chmod even to root: lift +a, fix, set it again below
    chattr -a "$f" 2>/dev/null || true
    chown "root:$g" "$f"
    chmod 0620 "$f"
    echo "$name: owner/mode $have -> $want"
  fi
  if lsattr -d "$f" 2>/dev/null | awk '{print $1}' | grep -q a; then
    echo "$name: append-only (chattr +a, already set)"
  elif chattr +a "$f" 2>/dev/null; then
    echo "$name: append-only (chattr +a)"
  else
    echo "$name: this file system has no append-only attribute; mode 0620 root:$g only"
    # B5: say plainly what that costs (the web user can truncate the trail it appends to)
    echo "WARNING: $f is not append-only: a compromised migui user could truncate the audit trail;" \
         "put /var/log/migui on a file system with chattr +a (ext4, xfs) or ship the audit off the host" >&2
  fi
}
# tests/ui/install_audit.py sources this file for audit_setup alone
if [ "${MIGUI_INSTALL_FUNCS_ONLY:-0}" = 1 ]; then
  return 0 2>/dev/null || exit 0
fi

HERE=$(cd "$(dirname "$0")" && pwd)
PKG=$(cd "$HERE/.." && pwd)
PREFIX=/opt/migui
UNITS=/etc/systemd/system
ACTION=install
START=1
case "${1:-}" in
  "") ;;
  --no-start) START=0 ;;
  --uninstall) ACTION=uninstall ;;
  --purge) ACTION=purge ;;
  *) echo "usage: $0 [--no-start|--uninstall|--purge]" >&2; exit 2 ;;
esac
[ "$(id -u)" = 0 ] || { echo "run as root (sudo $0)" >&2; exit 1; }

if [ "$ACTION" != install ]; then
  for u in migui-web migui-runner; do
    systemctl disable --now "$u.service" 2>/dev/null || true
    rm -f "$UNITS/$u.service"
  done
  systemctl daemon-reload
  rm -rf "$PREFIX"
  if [ "$ACTION" = purge ]; then
    chattr -a /var/log/migui/audit.jsonl 2>/dev/null || true
    rm -rf /var/lib/migui /var/log/migui /run/migui
    userdel migui 2>/dev/null || true
    getent group migui >/dev/null && groupdel migui 2>/dev/null || true
    echo "migui removed (jobs, audit and the migui user too)."
  else
    echo "migui removed; kept /var/lib/migui/jobs and /var/log/migui/audit.jsonl (--purge removes them)."
  fi
  exit 0
fi

MIGRATE=${MIGRATE:-/opt/mig-venv/bin/migrate}
if [ ! -x "$MIGRATE" ]; then
  MIGRATE=$(command -v migrate || true)
fi
[ -n "$MIGRATE" ] && [ -x "$MIGRATE" ] || { echo "migrate entry point not found: set MIGRATE=<path>" >&2; exit 1; }
MIGRATE=$(readlink -f "$MIGRATE")
PY=/usr/bin/python3
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 10))' || { echo "$PY is older than 3.10" >&2; exit 1; }

# user and group (no login, no home)
getent group migui >/dev/null || groupadd --system migui
id migui >/dev/null 2>&1 || useradd --system --gid migui --home-dir /nonexistent --no-create-home \
  --shell /usr/sbin/nologin --comment "migrate web UI" migui

# code: a fresh copy, root-owned, not writable by migui
install -d -m 0755 -o root -g root "$PREFIX" "$PREFIX/bin" "$PREFIX/lib"
rm -rf "$PREFIX/lib/migui.new"
cp -r "$PKG" "$PREFIX/lib/migui.new"
find "$PREFIX/lib/migui.new" -name __pycache__ -prune -exec rm -rf {} +
chown -R root:root "$PREFIX/lib/migui.new"
chmod -R u=rwX,go=rX "$PREFIX/lib/migui.new"
rm -rf "$PREFIX/lib/migui.old"
[ -d "$PREFIX/lib/migui" ] && mv "$PREFIX/lib/migui" "$PREFIX/lib/migui.old"
mv "$PREFIX/lib/migui.new" "$PREFIX/lib/migui"
rm -rf "$PREFIX/lib/migui.old"
# the guide the units name in Documentation= (DEPLOY.md of the same checkout), when this is a checkout
if [ -f "$PKG/../DEPLOY.md" ]; then
  install -m 0644 -o root -g root "$PKG/../DEPLOY.md" "$PREFIX/DEPLOY.md"
fi
for name in runner web passwd; do
  extra=""
  [ "$name" = runner ] && extra="\"--migrate\", \"$MIGRATE\", "
  cat > "$PREFIX/bin/migui-$name" <<WRAP
#!$PY -I
# migui-$name, written by migui/deploy/install.sh
import sys
sys.path.insert(0, "$PREFIX/lib")
from migui.$name import main
sys.exit(main([${extra}] + sys.argv[1:]))
WRAP
  chown root:root "$PREFIX/bin/migui-$name"
  chmod 0755 "$PREFIX/bin/migui-$name"
done

# state, log (DEPLOY.md section 6)
install -d -m 0750 -o root -g migui /var/lib/migui /var/log/migui
install -d -m 0700 -o root -g root /var/lib/migui/jobs
# 16: the login token is gone (accounts in /var/lib/migui/users, made with migui-passwd)
rm -f /var/lib/migui/token
if [ -f /var/lib/migui/users ] && [ ! -L /var/lib/migui/users ]; then
  chown root:migui /var/lib/migui/users
  chmod 0640 /var/lib/migui/users
fi
audit_setup /var/log/migui/audit.jsonl migui

# units
install -m 0644 -o root -g root "$HERE/migui-runner.service" "$HERE/migui-web.service" "$UNITS/"
systemctl daemon-reload
# 1.2.3: every migrate the runner starts must open /dev/sdX. A DeviceAllow= on the runner (a drop-in, or ProtectClock=
# on systemd <= 249, which adds char-rtc) closes its device policy: refuse to (re)start it like that.
devallow=$(systemctl show -p DeviceAllow --value migui-runner.service 2>/dev/null || true)
if [ -n "$devallow" ]; then
  echo "install.sh: migui-runner.service has DeviceAllow=$devallow (a drop-in, or ProtectClock= on systemd <= 249):" \
    "every migrate it starts would get EPERM on /dev/sdX. Remove it (systemctl edit migui-runner;" \
    "systemctl cat migui-runner) and run install.sh again (DEPLOY.md section 6)" >&2
  exit 1
fi
if [ "$START" = 1 ]; then
  systemctl enable migui-runner.service migui-web.service
  systemctl restart migui-runner.service
  systemctl restart migui-web.service
  sleep 1
  systemctl --no-pager --lines=0 status migui-runner.service migui-web.service || true
fi
cat <<MSG

migui installed: migrate = $MIGRATE
  create the first user:              sudo /opt/migui/bin/migui-passwd add <name>
  open from your workstation:         ssh -L 8787:127.0.0.1:8787 <you>@<this host>   then http://127.0.0.1:8787/
  audit:                              /var/log/migui/audit.jsonl
  guide:                              $PREFIX/DEPLOY.md
MSG
