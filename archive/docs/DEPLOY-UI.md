# DEPLOY-UI — the migrate web UI (migui, 7.3)

A thin web UI for `migrate` on the convert host (mig-host). It holds no migration logic: every action is one
`migrate …` command the operator could type, shown verbatim before it runs, one VM at a time. Design:
[DESIGN-UI.md](../../DESIGN-UI.md) (sections 2–14). Standard library only (Python ≥ 3.10, plain JS/CSS, no CDN, no
pip, no npm).

```
browser ──ssh -L 8787:127.0.0.1:8787──▶ mig-host
   migui-web    user migui, 127.0.0.1:8787, login + CSRF + confirmation + audit, no credential, no state access
      │ unix socket /run/migui/runner.sock (0660 root:migui, SO_PEERCRED = migui), JSON lines
   migui-runner root, the only process that starts migrate (argv lists, one job at a time, flock)
      │
   migrate (as today) → /var/lib/mig-migrate, /var/log/mig-batch
```

## 1. Prerequisites

- `migrate` 7.3.0 installed as in [DEPLOY-PYTHON.md](../../DEPLOY-PYTHON.md) (`/opt/mig-venv/bin/migrate`), and
  `migrate doctor` green as root. The UI only drives `doctor --json`, `inventory --json`, `list --json`,
  `plan … --json`, `status --json`, `migrate <id> … --yes --progress-json`, `volume <id> … --yes --progress-json`
  and `rollback <target>`; a `migrate` older than 7.3 is refused at start (the runner reads the CLI's option
  lists and stops when one it needs is missing).
- `/usr/bin/python3` ≥ 3.10 (Ubuntu 24.04: 3.12), systemd ≥ 250.

## 2. Install (or update)

From a clone of the repository on mig-host, as root:

```bash
cd iscsi-rbd-migration
sudo migui/deploy/install.sh                  # MIGRATE=/path/to/migrate if it is not /opt/mig-venv/bin/migrate
```

It is idempotent (run it again after a `git pull`). What it does:

| Path | Owner / mode | What |
|---|---|---|
| user `migui`, group `migui` | system user, no login, no home | runs migui-web |
| `/opt/migui/lib/migui/` | root 0755, files 0644 | a copy of `migui/` (code, static pages, these units) |
| `/opt/migui/bin/migui-runner`, `migui-web`, `migui-passwd` | root 0755 | wrappers: `/usr/bin/python3 -I`, `--migrate <path>` baked in (runner) |
| `/etc/systemd/system/migui-runner.service`, `migui-web.service` | root 0644 | units, enabled and (re)started |
| `/var/lib/migui/` | root:migui 0750 | |
| `/var/lib/migui/users` | root:migui 0640 | accounts (name + scrypt hash), written only by `migui-passwd`; the web reads it at each login. install.sh fixes its owner/mode if it exists and removes the old `/var/lib/migui/token` |
| `/var/lib/migui/users.lock` | root 0600 | `flock` of `migui-passwd` |
| `/var/lib/migui/jobs/` | root 0700 | job records `<id>.json` (0600, 0400 once ended), `<id>.log` (scrubbed copy for the UI, 0600), `<id>.out` (migrate's raw output, 0600) |
| `/var/log/migui/audit.jsonl` | root:migui 0620, `chattr +a` when the file system allows | audit, append-only |
| `/run/migui/` | root:migui 0750 (systemd `RuntimeDirectory`) | `runner.sock` 0660 root:migui, `runner.lock` 0600 |

`sudo migui/deploy/install.sh --no-start` installs without (re)starting. Re-running it restarts both units: the
session ends (log in again), and a job that was running keeps running (see 6) but the UI no longer follows it:
update when idle. Accounts survive updates (`--purge` removes them).

After the first install, create an account (section 3).

## 3. Open the UI

The web listens on 127.0.0.1:8787 only and refuses to start on any other address (DESIGN-UI T1). From your
workstation:

```bash
ssh -L 8787:127.0.0.1:8787 <you>@<mig-host>
# then open http://127.0.0.1:8787/ (or http://localhost:8787/)
```

Keep the **same local port** (8787): the web checks the `Host` and `Origin` headers against
`127.0.0.1:8787` / `localhost:8787` (anti CSRF / DNS rebinding). If you must use another local port, add
`--origin-port <that port>` to the web (`sudo systemctl edit migui-web`, override `ExecStart=`).

### Accounts and login (DESIGN-UI 16)

Login is a user name and a password. Accounts live in `/var/lib/migui/users` on mig-host and are managed by root
with `migui-passwd` (there is no sign-up and no password change from the web):

```bash
sudo /opt/migui/bin/migui-passwd add alice      # asks the password twice (no echo)
sudo /opt/migui/bin/migui-passwd set alice      # change it
sudo /opt/migui/bin/migui-passwd del alice      # remove (removing the last one warns: nobody can log in)
sudo /opt/migui/bin/migui-passwd list           # names, created, changed (never a hash)
printf '%s\n' "$PW" | sudo /opt/migui/bin/migui-passwd add alice --stdin   # scripts: one line on stdin
```

- User name: a lower-case letter, then `a-z 0-9 . _ -`, at most 32 characters.
- Password: 12 to 128 printable characters, not the user name (any case). No composition rule. It is never
  accepted on the command line or from the environment.
- Stored as scrypt (n=2¹⁵, r=8, p=1, 16-byte salt, 32-byte key; about 0.1 s and 32 MiB per check). The file is
  rewritten atomically (temporary file + rename, under `flock users.lock`); a change takes effect at the next
  login, no restart. Each change writes an audit line (`who` `passwd`, `user-add|user-set|user-del`, the name only).
- Not root with the default file: exit 2. A password outside the rules, a duplicate name, an unknown name: exit 1,
  nothing written.

On the login page, 5 failed logins in 10 minutes lock the login for 15 minutes (HTTP 429, audit `locked`); a wrong
name and a wrong password get the same answer (401 "wrong username or password"). A missing, corrupt or empty
users file answers 503 with the fix ("run migui-passwd add"). One session at a time: a new login ends the previous
one (audit `session-replaced` with both users). A session lasts at most 8 h and ends after 30 min without a click
(polling and the live log do not count). "Đăng xuất" ends it at once. The header shows
`<user> · phiên hết hạn sau …`; every job records its `operator` (shown in Lịch sử and on the running job).

## 4. Using it

1. **Doctor** — must be green before Instance/Volume open (enforced by the server, HTTP 412 otherwise). A green
   doctor then loads the inventory (hosts, projects, zones, flavors, volume types) that fills the dropdowns; the
   runner accepts only those values afterwards.
2. **Instance** — source host and/or project → *Tải danh sách* (`migrate list --json`), quick search in the browser,
   tick VMs; destination AZ (`--az`) and host (`--dst-host`, default "Nova scheduler chọn"); options; *Plan N VM*
   (`migrate plan <id> … --json` per VM). The destination project is shown, not chosen: it is the source name or the
   `PROJECT_MAP_FILE` entry, exactly as the CLI decides.
3. **Xác nhận** — the N verbatim commands, each with its effective options and their source; type the **number of
   VMs** (a batch) or the **VM name** (one VM, `--keep-source-ports`, a per-row `--ip <addr>`, `--in-use`,
   rollback). A wrong entry voids the confirmation (HTTP 409): plan again. The confirmation expires after 15 min.
4. **Hàng đợi / Đang chạy** — one job at a time; live log, progress (from `@@` lines), downtime clock from `t-stop`;
   *Hủy* sends SIGINT to the job's process group (migrate cleans up and keeps its state for a rollback); a SIGKILL is
   possible only 120 s after the SIGINT, with the VM name typed, and is audited. The batch stops at the first FAIL
   unless "dừng đợt nếu một con FAIL" was unticked; *Dừng sau con hiện tại* ends it after the running job.
5. **Lịch sử** — runner job records plus `migrate status --json` (also runs made outside the UI); effective options,
   conf snapshot, *Chạy lại với cùng option* (fills the form, you plan and confirm again), *So với job khác* (diff),
   *Rollback* (typed name).

## 5. Configuration

- Settings for migrate itself (`MIG_CONF`, `MIG_DEFAULT_OPTS`, …): `sudo systemctl edit migui-runner` →
  `[Service]` `Environment=MIG_CONF=/opt/mig/migrate.conf`. The runner passes its environment to migrate minus any
  `OS_*` variable; it never reads an openrc and never sends one to the web.
- Runner options (`/opt/migui/bin/migui-runner --help`): `--migrate`, `--peer-user` (default `migui`), `--group`,
  `--socket`, `--state-dir`, `--audit`, `--confirm-ttl` (900 s), `--kill-after` (120 s).
- Web options (`/opt/migui/bin/migui-web --help`): `--port` (8787), `--origin-port`, `--socket`, `--users-file`,
  `--audit`, `--session-max` (8 h), `--idle` (30 min). `--bind` accepts loopback addresses only.


### 5.1. Timeouts and why each one exists

| What | Value | Where | Why |
|---|---|---|---|
| Confirm token of a planned batch | 15 min (`--confirm-ttl 900`) | runner | A plan is a snapshot: flavor, ports, volumes, destination host as they were at that moment. Running it two hours later could use a stale IP, miss a volume added since, or land on a full host. When the token expires the operator plans again, so the command always matches the current state. |
| One CLI call made by the runner | doctor 5 min, inventory 10 min, list 10 min, plan 15 min, status 2 min (`TIMEOUTS` in runner.py) | runner | An `openstack` call hangs for ever when Keystone or an API is down. Without a limit the Plan button stays locked, the runner keeps its lock and nobody can do anything. With it the runner kills the call, reports the error and unlocks. Plan is the longest: one VM takes 10–20 s and a batch is planned one VM after the other. |
| A migrate / volume / rollback job | **none** | runner | Copying a 500 GB volume takes hours. A job ends only by itself or when the operator cancels it (SIGINT, then SIGKILL after `--kill-after 120` s with the VM name typed). |
| Login session | 8 h total, 30 min idle (`--session-max`, `--idle`) | web | A tab left open on an unattended machine would let anyone run a migration. Idle logout works like a bank site. A running job is not affected: it lives on mig-host; log in again and it is in Running / History. |
| Failed logins | 5 in 10 min → locked 15 min (`FAIL_WINDOW`, `FAIL_MAX`, `LOCK_FOR` in web.py) | web | Slows password guessing to a few hundred tries a day. |
| Socket connect web → runner | 10 s | web | A runner that is not running answers 503 at once instead of a hanging page. |
| SSE keep-alive (job stream, plan stream) | ping every 15 s | web | The ssh tunnel and the browser drop an idle connection; a ping keeps it open while a long copy prints nothing. |

What is lost on a timeout: an expired confirm token or an expired session only costs a new plan (10–20 s per
VM) or a new login. A timed-out CLI call costs nothing on the clouds: doctor, inventory, list and plan only read.


### 5.2. Destination project: `PROJECT_MAP_FILE`

The "Project" box under **Đích** on the Instance and Volume screens is read-only: it shows where the copies will
land. `migrate` has no `--dst-project` option; a VM or a volume goes to the destination project **with the same
name** as its source project, unless a map file says otherwise. The map is a setting of `migrate` itself, read by
the UI's runner at every plan, so it is set up on mig-host, not in the browser.

1. Write the map, one `source destination` pair per line (names or IDs, whitespace separated, `#` comments):

   ```
   # /opt/mig/project-map.conf
   # source-project   destination-project
   finance            finance-prod
   web-legacy         web
   ```

   A project with no line keeps its name (`proj-a` → `proj-a`). A malformed line (one or three columns, or
   `a = b`) stops every plan with an error; an unreadable file only logs a WARN and every project keeps its name.

2. Point `migrate` at it in `/opt/mig/migrate.conf` (create the file if it does not exist; `migrate` reads this
   path by default, the runner's unit only needs `Environment=MIG_CONF=` when the file lives elsewhere):

   ```
   PROJECT_MAP_FILE=/opt/mig/project-map.conf
   ```

   ```bash
   sudo install -m 600 -o root -g root /dev/null /opt/mig/project-map.conf   # then edit it
   sudo chmod 600 /opt/mig/migrate.conf
   ```

3. No restart: the next **Plan** reads the file. The pill next to the Project box changes from
   `= tên nguồn, PROJECT_MAP_FILE không có` to `PROJECT_MAP_FILE /opt/mig/project-map.conf`, the confirm screen
   prints `project finance → finance-prod` under each command, and the option table shows `projects` with source
   `conf · PROJECT_MAP_FILE`. The destination project must already exist: with a name that does not, the plan row ends PLAN-FAIL with "cannot get a destination token" (the destination openrc is scoped to that project);
   the UI never creates projects.

The same file applies to `migrate` run by hand on mig-host (every command, one VM included, since 7.0). Rollback
uses the pair recorded in the job, so changing the map later does not affect a rollback.

## 6. Security model in short

| | |
|---|---|
| Network | 127.0.0.1 only; reach it through SSH. No TLS, no LDAP/OIDC, one operator (DESIGN-UI 6, "not in 7.3"). |
| Web process | user migui, no capability, `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `PrivateDevices`, seccomp `@system-service`, `MemoryDenyWriteExecute`, `IPAddressDeny=any` + `IPAddressAllow=localhost`, `RestrictAddressFamilies=AF_UNIX AF_INET`; writes only `/var/log/migui`; `/opt/mig`, `/root`, `/var/lib/mig-migrate`, `/var/log/mig-batch`, `/var/lib/migui/jobs` are inaccessible to it. |
| Requests | session cookie `HttpOnly; SameSite=Strict`; every POST needs `Origin` of this UI, `X-Requested-With: migui`, a JSON body ≤ 64 KB; CSP `default-src 'self'` without inline script or style; `X-Frame-Options: DENY`; `Cache-Control: no-store`. |
| Runner | root; accepts only its 9 messages from uid migui (SO_PEERCRED); re-checks every field (idents from its own `list`, hosts/projects/zones/flavors/types from its own `inventory`, options against an allowlist and the CLI's own flag list); builds argv lists (never a shell); one job at a time (`flock`, plus a refusal when `migrate status` shows a migrate started outside the UI); scrubs `OS_PASSWORD=`, `OS_APPLICATION_CREDENTIAL_SECRET=`, `OS_TOKEN=`, `X-Auth-Token`, `key = AQ…` from every line it hands out. |
| Confirmation | the runner issues a confirm token (16 random bytes) bound to the session and to the exact commands of the batch, valid 15 min; the typed count/name is checked by the runner; any mismatch voids it. |
| Audit | `/var/log/migui/audit.jsonl`: one JSON line per login, failure, lock, plan, run, start, end, cancel, kill, refusal (`ts`, `who`, `session` id, `action`, `argv`, `job`, `rc`, `detail`; the web's lines carry `detail.user`, the runner's `detail.operator`). Never a password, a hash or a cookie. |

### Why the runner is not sandboxed like the web

DESIGN-UI 2 describes the runner as "root, without network". It cannot be: every `migrate` it starts is its child
and inherits its sandbox (mount namespace, seccomp filter, network namespace, cgroup IP firewall), and migrate
needs the network (both clouds' APIs, iSCSI to the source storage, RBD to Ceph), `/dev`, `/sys` (SCSI rescans),
`/etc/iscsi`, `/run` (iscsid and multipathd sockets) and root's capabilities. `PrivateNetwork=`,
`ProtectSystem=strict`, `PrivateDevices=`, `IPAddressDeny=`, `RestrictAddressFamilies=` or an empty
`CapabilityBoundingSet=` on the runner would make every migration fail. The runner unit keeps what leaves migrate
whole (`NoNewPrivileges`, `LockPersonality`, `RestrictRealtime`, `RestrictSUIDSGID`, `ProtectClock`,
`ProtectHostname`, `UMask=0077`) and `KillMode=process`; its attack surface is the socket above. A later version
could start each migrate as its own transient unit (`systemd-run`) and sandbox the runner fully.

### A runner restart under a running job

`KillMode=process` stops only the runner; migrate runs in its own session and writes its output to
`/var/lib/migui/jobs/<id>.out` (a file, not a pipe, so it never gets EPIPE). The restarted runner marks the job
`DETACHED` (its process still lives) or `LOST` and refuses to start another job while `migrate status` shows a
migrate process. Check the batch log, then use the state (rollback or let it finish).

## 7. Troubleshooting

| Symptom | Meaning |
|---|---|
| HTTP 503 "migui-runner is not reachable" | `systemctl status migui-runner`; `journalctl -u migui-runner` |
| HTTP 503 at login "no users file" / "users file …" | no account yet, or the file is corrupt: `sudo /opt/migui/bin/migui-passwd list`, then `add` |
| HTTP 503 "scrypt unavailable or memory limit" | Python/OpenSSL without scrypt (needs Python ≥ 3.10 with OpenSSL ≥ 1.1); migui-web also refuses to start |
| HTTP 400 at login | user name with capitals or other characters, or a password over 128 characters (counts as a failed login) |
| runner exits "the installed migrate … lacks --…" | migrate older than 7.3: upgrade it (DEPLOY-PYTHON.md) |
| 401 right after login | another login took the session, or 30 min idle |
| 412 | Doctor is not green in this session |
| 409 on *Chạy* | wrong count/name, expired or replaced confirmation: plan again |
| 423 | a job runs (UI or command line); wait |
| 421 | the browser reached the UI under another host/port: use `http://127.0.0.1:8787/` with `ssh -L 8787:…:8787` |

## 8. Uninstall

```bash
sudo migui/deploy/install.sh --uninstall   # stops and removes both units and /opt/migui; keeps jobs and audit
sudo migui/deploy/install.sh --purge       # also /var/lib/migui, /var/log/migui and the migui user
```

Neither touches migrate, its configuration, `/var/lib/mig-migrate` or `/var/log/mig-batch`.

## 9. Tests

`bash tests/unit/run.sh` runs the UI suites too (suite `ui` = `tests/ui/run.sh`): a real runner and web on a
temporary socket and a free loopback port, a fake `migrate` replaying the simulated clouds of `tests/unit`; the 10
security tests of DESIGN-UI 10 and the login tests of 16.4 (`tests/ui/security.py`), the functional ones
(`tests/ui/functional.py`) and `migui-passwd` (`tests/ui/passwd.py`). Each stack logs in as user `test`, made with
`migui-passwd --users-file <tmp>`. No root
needed (as root, the SO_PEERCRED test also connects as uid 65534).
