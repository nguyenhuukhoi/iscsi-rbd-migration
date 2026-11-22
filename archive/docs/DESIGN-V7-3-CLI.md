# DESIGN-V7-3-CLI — những gì CLI `migrate` phải có trước khi làm UI (7.3.0)

Ngày 20/11/2026. Trạng thái: **thiết kế, chờ Opus code trên nhánh `feature/7.3-cli-json`**. Nguồn yêu cầu:
[DESIGN-UI.md](DESIGN-UI.md) §3, §13.1, §14.4 (UI bản 20/11, giữ local chưa commit). Bản 7.2 (main `dfd54da`)
là nền. Mọi hành vi cũ giữ nguyên khi không dùng cờ mới; mọi output JSON **không bao giờ** chứa giá trị env/openrc.

Tóm tắt: 6 việc, theo thứ tự làm: (1) `options.json` + bảng `effective`; (2) `--json` cho `plan`/`doctor`;
(3) `migrate list --json`; (4) `migrate inventory --json`; (5) `migrate status --json`; (6) `--dst-host`;
(7) `--progress-json`; (8) D3b sửa cách đếm copy cho ephemeral.

---

## 1. `options.json` trong state dir + khối `effective`

### 1.1. Bảng effective

Hàm mới `Options.effective(r)` → dict, mỗi key `{"value": …, "from": …}`. `from` là **một trong đúng 6 chuỗi**
(UI tô màu theo chuỗi này, không parse thêm): `cli`, `default`, `MIG_DEFAULT_OPTS`, `conf` (gồm MIG_AZ,
MIG_VOLUME_AZ, AZ_TYPE_MAP, RBD_POOL_MAP; ghi tên biến vào `detail`), `az` (volume_az rơi về dest_az),
`source` (lấy từ VM nguồn). Có thêm `detail` tự do (vd `"AZ_TYPE_MAP (AZ1)"`).

| key | value | cách xác định `from` |
|---|---|---|
| `mode` | cold/live | `--mode` ∈ `cli_given` → cli; ∈ `def_shown` → MIG_DEFAULT_OPTS; else default |
| `ip` | same/new/`<addr>` | như trên (`--ip`) |
| `volume_type` | chuỗi hoặc `""` | `type_cli` → cli; `type_from` → conf (detail = type_from); trong def_shown → MIG_DEFAULT_OPTS; else default (= type mặc định đích, value `""`) |
| `pool` | tên pool | `pool_for_type` dùng map → conf (detail `RBD_POOL_MAP`); else default (`RBD_POOL`) |
| `az` | dest_az | `az_cli` → cli; `az_from` → conf (detail `MIG_AZ`) hoặc `resolver` khi P6 tự chọn zone duy nhất (**thêm `az_from="resolver"` ở chỗ resolver đang set dest_az**); def_shown → MIG_DEFAULT_OPTS; rỗng → default |
| `volume_az` | | `volume_az_cli` → cli; MIG_VOLUME_AZ → conf; else az |
| `dst_host` | `""` hoặc hypervisor | cli / default (mục 6) |
| `verify`, `max_passes`, `delta_limit_mb`, `start_source`, `keep_ports`, `flavor_fallback`, `no_server_group`, `no_copy_sg`, `volumes_only`, `in_use` | | cli / MIG_DEFAULT_OPTS / default theo `cli_given`/`def_shown` |
| `flavor` | tên flavor sẽ dùng | `ov_flavor` → cli; fallback map đã chọn → conf (detail `FLAVOR_MAP_FILE`); else source |
| `security_groups` | `[{"src":"web-tier","dst":"AZ1-web-tier","action":"create|reuse"}]` | `ov_sg` → cli; `no_copy_sg` → default (`[]`); else source (từ `r.sgplan`) |
| `server_group` | `{"src":"web-anti","dst":"AZ1-web-anti","policy":"anti-affinity"}` hoặc null | `server_group` cli; `no_server_group` → default null; else source |
| `projects` | `{"src":"prod","dst":"prod"}` | `PROJECT_MAP_FILE` map khác tên → conf; else default |

Giá trị lấy từ các trường đang có (`cli_given`, `def_shown`, `az_from`, `type_from`, `sgplan`, …): **không thêm
logic quyết định mới**, chỉ ghi ra. Những key chỉ có sau export (flavor, SG, server group, projects) điền ở
preflight; `effective()` trước đó trả giá trị đã biết tới lúc gọi.

### 1.2. File

`cmd_migrate` (và `volcmd` tương tự, key ít hơn) ngay sau dòng `st.write("options", …)` hiện có ghi thêm
`options.json`:

```json
{"command": ["migrate", "migrate", "web-01", "--mode", "live", "--az", "AZ1", "--yes"],
 "effective": {…1.1…},
 "conf": {"path": "/opt/mig/migrate.conf", "found": true,
          "MIG_DEFAULT_OPTS": "--verify none", "MIG_AZ": "", "MIG_VOLUME_AZ": "", "AZ_TYPE_MAP": "AZ1:ssd",
          "RBD_POOL_MAP": "ssd:volumes-nvme", "RBD_POOL": "volumes", "PROJECT_MAP_FILE": "", "FLAVOR_MAP_FILE": "",
          "SRC_PROJECT": "prod", "DST_PROJECT": "prod"},
 "tool_version": "7.3.0", "started": "2026-11-20T09:10:03Z",
 "ident": "web-01", "server_id": "7c1e…", "kind": "vm"}
```

- `command`: `sys.argv` của tiến trình con (`python -I -m migratetool.single …` → ghi lại dạng người gõ:
  `["migrate", verb, ident, *args]`), **không** gồm env.
- `conf`: chỉ các khóa liệt kê trên (whitelist), không bao giờ `SRC_RC`/`DST_RC`/`CEPH_*`/`OS_*`.
- File cũ `options` (một dòng `MODE=… AZ=…`) giữ nguyên, rollback và grep cũ không đổi.
- Rollback ghi `options.json` của chính nó vào thư mục `.rolled-back.*` dưới tên `rollback.json`
  (`command`, `started`, `ended`, `rc`), không đè `options.json` gốc.

---

## 2. `--json` cho `plan` và `doctor`

### 2.1. Nguyên tắc chung

- Cờ `--json` chỉ đổi **stdout**: in đúng **một** JSON object cuối cùng; mọi dòng text cũ đi **stderr** (để
  log batch vẫn đầy đủ: `selector.child` đã gộp stderr vào log). Exit code như cũ.
- Trong `selector.Runner`, text của child (preflight từng VM) vẫn ghi `log-<n>` như nay; parent **parse** dòng
  `  OK|WARN|FAIL|INFO  …` của child từ file log đó (regex `^  (OK|WARN|FAIL|INFO)\s{2,}(.*)$`) để tạo mảng
  `checks` — không sửa child. Dòng không khớp bị bỏ (UI xem log đầy đủ qua `log` path).
- Có `--json` thì `plan` không in bảng `Selected:`/`Plan:` ra stdout.

### 2.2. `migrate plan … --json`

```json
{"tool_version": "7.3.0", "verb": "plan", "log_dir": "/var/log/mig-batch/20261120-091003",
 "rows": [
   {"kind": "vm", "id": "7c1e…", "name": "web-01", "ident": "web-01", "status": "ACTIVE",
    "project": {"src": "prod", "dst": "prod"}, "result": "PLAN-OK", "info": "",
    "env": {"SRC_PROJECT": "prod", "DST_PROJECT": "prod"},
    "command": ["migrate", "migrate", "web-01", "--mode", "live", "--az", "AZ1", "--yes"],
    "effective": {…1.1…},
    "checks": [{"level": "OK", "text": "compute AZ AZ1"}, {"level": "WARN", "text": "dns_name not copied: …"}],
    "counts": {"ok": 14, "warn": 1, "fail": 0, "info": 2},
    "log": "/var/log/mig-batch/20261120-091003/log-1"}],
 "totals": {"PLAN-OK": 2, "PLAN-FAIL": 1, "SKIP": 1}, "rc": 1}
```

- `command` của row = lệnh child sẽ chạy nếu `migrate` thay `plan` (đúng chuỗi §14.2 UI hiện ra). Với row
  `SKIP(*)` thì `command`, `effective`, `checks` là null.
- `effective` của row: child `plan` ghi `options.json` vào **thư mục plan** của nó (hiện child plan có state
  dir riêng? — nếu không, child in một dòng `@@ {"effective": …}` ra stdout, parent bắt dòng `^@@ ` và bỏ
  khỏi `checks`). Chọn cách dòng `@@`: không đụng state dir khi chỉ plan.
- `plan volume … --json` cùng cấu trúc, `kind: "vol"`.

### 2.3. `migrate doctor --json`

```json
{"tool_version": "7.3.0", "checks": [{"level": "OK", "text": "openstack: openstack 10.5.0"}, …],
 "counts": {"ok": 15, "warn": 0, "fail": 0, "info": 1}, "passed": true}
```
Cách làm: `out()` trong doctor đi qua một collector khi `--json` (doctor đã in theo dạng `  LEVEL  text`).

---

## 3. `migrate list --json` (lệnh mới, read-only)

Cú pháp: `migrate list [--host H]… [--project P]… [--vm V]… [--vm-file F] [--volumes [--available|--attached|--all]] --json`
(không có `--json` → in bảng text như `Selected:` để người gõ tay dùng được).

- Dùng lại `Runner.enumerate_vms` / `enumerate_volumes` nguyên vẹn (cùng listing, cùng luật
  `SKIP(state-exists)`/`SKIP(status)`/ambiguous). **Không** export, **không** preflight, không tạo log dir
  (ghi list-N.json vào thư mục tạm rồi xóa, hoặc `log_dir=None` → `_write` bỏ qua).
- Mỗi row VM bổ sung, từ **một** lệnh `server list --all-projects --long -f json` đã có + `server show` chỉ
  khi thiếu cột: `host` (cột Host), `az` (cột Availability Zone), `flavor` (Flavor Name), `image`
  (Image Name, `""` khi BFV), `networks` (`{"vlan120": ["192.168.120.30"]}`), `boot`: `"bfv"` nếu
  `Image Name` rỗng/`N/A (booted from volume)`, else `"ephemeral"` (cùng luật `image_says_bfv` 7.2 B3),
  `volumes`: `[{"id", "name", "size", "device"}]` từ cột `Attached Volumes`/`volumes_attached` + 1 lần
  `volume list --all-projects -f json` (một lệnh cho cả danh sách, map theo id; không `volume show` từng cái),
  `state`: `{"dir": "/var/lib/mig-migrate/web-01", "live": true, "started": "…", "options": {…options.json…}}`
  hoặc null; `locked` (cột Locked nếu có).
- Row volume: `id, name, size, status, type, az, attached_to: {"server_id","server_name","device"}|null,
  state` (dir `vol-<id>`).
- Số lệnh openstack cho một `--host` 24 VM: 1 `server list` + 1 `volume list` + 1 `project show` mỗi project
  gặp (đã cache `pnames`) — ghi rõ trong docstring, UI tin vào "1,8 s".

---

## 4. `migrate inventory --json` (lệnh mới, read-only)

Một lệnh điền mọi dropdown của UI, cả hai cloud:

```json
{"source": {"hosts": [{"host": "compute-03", "zone": "nova", "state": "up", "status": "enabled", "vms": 24}],
            "projects": [{"id": "…", "name": "prod"}], "token_project": "admin", "auth_type": "password"},
 "destination": {"hosts": [{"host": "ceph-compute-11", "zone": "AZ1", "state": "up", "status": "enabled"}],
                 "projects": [...], "zones": {"compute": ["AZ1", "AZ2"], "volume": ["AZ1", "AZ2"]},
                 "flavors": [{"name": "m1.large", "vcpus": 4, "ram": 8192, "disk": 40}],
                 "volume_types": ["ssd", "hdd", "__DEFAULT__"],
                 "token_project": "admin", "auth_type": "v3applicationcredential"},
 "conf": {…whitelist như 1.2…}, "tool_version": "7.3.0"}
```

- `hosts` = `compute service list --service nova-compute -f json` (cột Host, Zone, State, Status); `vms` của
  nguồn đếm từ `server list --all-projects --host H` **chỉ khi** `--count` (mặc định không đếm: N lệnh).
- `projects` = `project list -f json`; appcred không list được → `[]` + `"projects_error": "…"` (không die).
- Không có token, lệnh die như doctor: UI phải chạy doctor trước (§5 UI).

---

## 5. `migrate status --json` (lệnh mới, read-only, không gọi cloud)

Đọc **chỉ** `/var/lib/mig-migrate` và `/var/log/mig-batch`:

```json
{"live": [{"ident": "web-01", "dir": "…/web-01", "kind": "vm", "options": {…options.json hoặc null…},
           "files": ["server.json", "volume-map", "dst-server-id", "t-stop"], "t_stop": "…", "dst_server_id": "…",
           "volume_map": [["src", "dst"]], "mtime": "…"}],
 "rolled_back": [{"ident": "web-01", "dir": "…/web-01.rolled-back.1795109242", "retired": "2026-11-20T00:23:02Z",
                  "options": {…}, "rollback": {…rollback.json hoặc null…}}],
 "batches": [{"dir": "/var/log/mig-batch/20261120-091003", "logs": ["log-1"], "mtime": "…"}],
 "running": [{"pid": 48213, "cmdline": ["python", "-I", "-m", "migratetool.single", "migrate", "web-01", …]}]}
```

- `running`: quét `/proc/*/cmdline` tìm `migratetool.single`/`migratetool` — để UI phát hiện job chạy ngoài UI
  (§8 UI). Không có `/proc` → `[]`.
- Thư mục chỉ có plan (không `volume-map`) ghi `"live": false` trong mục `plan_only`.
- `rolled_back` mặc định 50 cái mới nhất, `--all` lấy hết (lab hiện có ~180).
- Giá trị `options` của state cũ (< 7.3, không có options.json) = null; UI hiện "không ghi nhận".

---

## 6. `--dst-host <hypervisor>`

- Option VM mới (VM_OPTIONS, VALUE_OPTS; **không** nhận từ MIG_DEFAULT_OPTS — chọn host là quyết định từng lần,
  như `--in-use`; có trong defaults → die với message cùng kiểu).
- Preflight (sau check `compute AZ`): 1 lệnh `compute service list --service nova-compute -f json` ở đích:
  - không có Host trùng → `FAIL  destination host X is not a nova-compute host`;
  - State ≠ up hoặc Status ≠ enabled → `FAIL  destination host X is down or disabled (state=…, status=…)`;
  - có `--az`/AZ resolver và Zone của host ≠ dest_az → `FAIL  destination host X is in zone Y, not Z (--az)`;
    không có dest_az → `INFO  availability zone Z from host X` và **set dest_az = Zone của host, az_from =
    "host"** (vì server create với `--host` vẫn cần AZ cho volume: volume_az rơi theo).
  - OK → `OK    destination host X (zone Z, up, enabled)`.
- Tạo server: `cmd += ["--host", o.dst_host]`; cần `--os-compute-api-version 2.74` → thêm vào `Cloud.run` của
  dst chỉ cho lệnh này (`r.dst.out("--os-compute-api-version", "2.74", "server", "create", …)` — kiểm tra OSC
  10.5 nhận global option trước subcommand; nếu không, env `OS_COMPUTE_API_VERSION=2.74` cho riêng Popen này).
  Doctor thêm dòng `OK    destination Nova API ≥ 2.74 (--dst-host usable)` / `INFO … not available`.
- Không phải admin → Nova 403 → lệnh create fail sau khi nguồn đã tắt. Vì vậy preflight thêm:
  `--dst-host` mà token đích không có role admin (doctor đã biết cách kiểm) → `FAIL  --dst-host needs the admin
  role at the destination`.
- `effective.dst_host` (mục 1.1), `options` dòng cũ thêm `DST_HOST=`.
- Rollback không đổi.

---

## 7. `--progress-json`

- Cờ cho `migrate migrate|volume`. Có cờ: ngoài log cũ, in thêm dòng `@@ {"event": …}` ra **stdout** (cùng
  luồng log batch, UI lọc tiền tố `@@ `). Không cờ: không in gì thêm, byte-identical với 7.2.
- Sự kiện (tối thiểu): `phase` (`{"event":"phase","name":"presync|cutover|copy|verify|create|done"}`),
  `copy` (`{"event":"copy","volume":"<id>","pct":37,"mbps":180}` từ watcher copyvol.py:57-61),
  `blocksync` (`{"event":"blocksync","volume":"<id>","pass":3,"pct":62,"changed_mib":9}` — livesync bắt
  dòng stderr `[blocksync] NN% (…)` của child và chuyển thành JSON; child blocksync.py không đổi),
  `pass` (`{"event":"pass","volume":…,"n":2,"chunks":310,"mb":1180,"secs":64.1}`), `t_stop`
  (`{"event":"t_stop","at":"…"}`), `downtime` (`{"event":"downtime","total":245,"copy":133,"verify":0,
  "control_plane":111}` từ dòng D3), `result` (`{"event":"result","rc":0,"dst_server_id":"…"}`).
- Bash harness parity: dòng `@@` chỉ có khi cờ bật nên parity không đổi; unit test riêng kiểm từng event.

---

## 8. D3b — copy của VM ephemeral

Gate 7.2 cho thấy ephemeral in `copy 0s, control-plane 213s` dù ~107 s là snapshot + download + upload Glance.
Sửa: trong `migrate_ephemeral_root`, thời gian của 3 bước đó cộng vào `t_copy` (dữ liệu đi qua mig-host là
copy). Dòng D3 thêm `glance %ds` khi ephemeral:
`downtime breakdown: total 213s, copy 107s (glance 107s), verify 0s, control-plane 106s`. Unit case v72unit
D3 thêm 1 case ephemeral.

---

## 9. Không làm trong 7.3

UI (web/runner), `--parallel`, ephemeral đích boot-from-volume (`--boot-volume-size`, chờ user chốt),
sửa conf từ CLI.

## 10. Kiểm thử và gate

- Unit (`tests/unit/`, file mới `v73unit.py`): mỗi mục 1–8 ≥ 2 case, tên case chứa mã mục (`1.1`, `2.2`, `3`,
  `4`, `5`, `6`, `7`, `8`); đặc biệt: JSON stdout **đúng một object** (json.loads cả stdout), không có khóa
  nào tên `OS_*`/`*_RC`/`PASSWORD`/`SECRET` ở bất kỳ độ sâu nào (walk đệ quy); `--dst-host` sai zone FAIL
  trước `server stop`; `--dst-host` trong MIG_DEFAULT_OPTS die; `status --json` với state không có
  options.json → null; `list --json` không gọi `server show`/`volume show` khi listing đủ cột (đếm calls).
- Parity: mọi lệnh **không** có cờ mới byte-identical (rule `Intended: 7.3 <mục>` chỉ cho dòng D3b).
- Lab: 7 suite 7.2 nguyên bản (hồi quy) + `v73feat.sh` mới: `list --json` trên demo-vm (so với `server show`),
  `inventory --json`, `plan --json` 1 OK + 1 FAIL (`--ip 10.9.9.9`), `status --json` trước/sau migrate/rollback,
  `--dst-host` = host đích thật (server đích đúng host, `server show -c OS-EXT-SRV-ATTR:host`), `--dst-host`
  host rởm FAIL preflight, `--progress-json` cold + live (đếm event, pct cuối ≥ 99), ephemeral D3b.
- Report `TEST-REPORT-<ngày>-v73.md`, CHANGELOG [7.3.0], GUIDE-PYTHON mục mới "output máy đọc", DEPLOY mục
  Nova 2.74. Version 7.3.0. Leak-check trước push. Không push, không merge: user quyết.

---

## 11. Bảo mật và safeguard — BẮT BUỘC, mỗi điểm có unit test tên `SEC-<n>`

Nguyên tắc: 7.3 mở thêm **đường đọc** (JSON) và **một** đường ghi mới (`--dst-host`). Đường đọc không được làm lộ
bí mật và không được đổi trạng thái; đường ghi phải FAIL trước khi nguồn tắt. Lệnh chạy bằng root trên host giữ
admin của hai cloud, nên mọi input đều coi là thù địch kể cả khi đến từ UI của chính mình.

### 11.1. Không lộ bí mật (SEC-1…4)

| # | Luật | Cách kiểm |
|---|---|---|
| SEC-1 | Mọi JSON ra stdout đi qua **một** hàm `emit_json(obj)` duy nhất; hàm này walk đệ quy và **die** (không in) nếu gặp khóa khớp `(?i)(OS_|_RC$|PASSWORD|SECRET|TOKEN|AUTH_URL|CACERT|KEY$|^AQ)` hoặc giá trị trùng với bất kỳ giá trị nào của `OS_PASSWORD`/`OS_APPLICATION_CREDENTIAL_SECRET`/`OS_TOKEN` đang có trong env của hai openrc. | unit: nhét giá trị password giả vào world, gọi mọi lệnh `--json`, assert die + stdout rỗng |
| SEC-2 | `conf` trong options.json/inventory chỉ gồm whitelist mục 1.2; thêm khóa = sửa whitelist + test. | unit: so key set |
| SEC-3 | `command` ghi lại **không** gồm env; `--ip`, `--dst-host` giá trị người gõ ghi nguyên văn nhưng đã qua validator (11.2) nên không chứa newline/điều khiển. | unit: argv có `\n` → die trước khi ghi |
| SEC-4 | `status --json` đọc `/proc/*/cmdline` chỉ để nhận diện `migratetool`; **không** trả `environ`, không trả cmdline của tiến trình khác; đường dẫn log/state trả về phải nằm trong `STATE_ROOT`/`LOG_ROOT` (realpath, không theo symlink ra ngoài — symlink lạ → bỏ qua + `"skipped": n`). | unit: tạo symlink state → /etc, assert không xuất hiện |

### 11.2. Validate input (SEC-5…8)

| # | Luật | Cách kiểm |
|---|---|---|
| SEC-5 | `--dst-host`: regex `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`, sai → die tại parse (như `--ip`, 7.2 B4), không gọi cloud. | unit: `--dst-host "a;b"`, `"../x"`, 200 ký tự |
| SEC-6 | Selector của `list`/`inventory`: `--host`/`--project`/`--vm` cùng validator 7.2 (`re_escape` + C5 cấm `/`); `--vm-file` phải là file thường, ≤ 1 MB, ≤ 10 000 dòng, không symlink; sai → die. | unit |
| SEC-7 | Mọi argv tới `openstack`/`qemu-img`/`rbd` vẫn là list (`shell=False`), không có chỗ nào format chuỗi lệnh. Grep CI: `grep -rn "shell=True\|os.system\|eval(\|exec(" migratetool/` phải rỗng — thêm vào `tests/unit/run.sh`. | run.sh |
| SEC-8 | JSON vào (listing của openstack): mọi trường dùng qua `_s()`/`jqs()` như nay; kiểu không như mong đợi (list thay str) → `""`, không crash, không die cả đợt. | unit: world trả `"Name": ["x"]` |

### 11.3. Read-only thật sự (SEC-9…11)

| # | Luật | Cách kiểm |
|---|---|---|
| SEC-9 | `list`, `inventory`, `status`, `plan --json`, `doctor --json` chạy qua `Cloud` ở **chế độ read-only**: `Cloud(readonly=True)` chỉ cho phép subcommand có động từ trong `{list, show, token issue, catalog, endpoint list, availability zone list, compute service list, quota show, limits show}`; động từ khác → `Die("read-only command tried: …")`. | unit: parity "calls" của 5 lệnh này không có `create/set/delete/stop/start/lock/add/remove` |
| SEC-10 | 5 lệnh trên **không tạo/sửa** gì trong `STATE_ROOT` (`list`/`inventory`/`status` không mkdir; `plan` giữ hành vi 7.2: chỉ thư mục plan đã có). | unit: snapshot cây STATE_ROOT trước/sau, so bằng |
| SEC-11 | `status --json` không giữ lock, không chạm file của job đang chạy (chỉ `open(..., "r")`); lỗi đọc một thư mục → ghi `"error"` cho thư mục đó, không die. | unit: chmod 000 một state dir |

### 11.4. Safeguard cho đường ghi (SEC-12…15)

| # | Luật | Cách kiểm |
|---|---|---|
| SEC-12 | `--dst-host`: cả 4 điều kiện (host có, up+enabled, đúng AZ, token admin) kiểm trong preflight **trước** `server stop`; thiếu một → `FAIL`, nguồn không tắt, không volume đích nào được tạo. | unit: 4 case, assert `src server stop` không có trong calls |
| SEC-13 | `--dst-host` bị cấm trong `MIG_DEFAULT_OPTS` (như `--in-use`): có → die với message nêu rõ. | unit |
| SEC-14 | `server create --host` thất bại (403/NoValidHost) → luồng lỗi hiện có (rollback dọn được): kiểm C8 7.2 vẫn phủ: không ghi được ID → `delete_unrecorded_server`. | unit: create die → rollback sạch |
| SEC-15 | `options.json`/`rollback.json` ghi bằng `os.open(..., O_WRONLY|O_CREAT|O_EXCL, 0o600)` vào state dir 0700; không ghi đè file đã có (chạy lại khi state tồn tại đã die từ trước). | unit: mode 0600, file có sẵn → die |

### 11.5. Giới hạn tài nguyên (SEC-16…17)

| # | Luật | Cách kiểm |
|---|---|---|
| SEC-16 | `inventory --count` giới hạn ≤ 64 host (quá → die, yêu cầu `--host` cụ thể); `status --json` mặc định 50 `.rolled-back`, `--all` tối đa 2 000; `list` không phân trang gì khác `server list` hiện có. | unit |
| SEC-17 | `--progress-json` tối đa 1 event/giây mỗi loại (`copy`/`blocksync`), tránh log batch phình khi volume lớn; `pass`/`phase`/`result` không giới hạn. | unit: 1 000 dòng tiến độ giả → ≤ N event |

### 11.6. Quy tắc code cho Opus

- Không thêm thư viện ngoài stdlib; không `requests`, không `yaml`.
- `json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))` cho JSON ra; `sort_keys` để
  diff ổn định trong test.
- Mỗi lệnh `--json` có docstring liệt kê **đúng** các lệnh openstack nó gọi (để audit sau đọc được).
- Không in JSON từng phần rồi bỏ dở: gom xong mới `emit_json`; die giữa chừng → stdout rỗng, rc=1, lỗi ở stderr.
- Thêm vào `tests/unit/run.sh` bước `python -I -m migratetool.cli --version` và grep SEC-7.
- `DEVELOPING-PYTHON.md` mục mới "Output JSON: emit_json là cửa duy nhất".
