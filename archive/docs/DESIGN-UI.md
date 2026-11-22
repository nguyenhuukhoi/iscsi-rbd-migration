# DESIGN-UI — giao diện web cho `migrate` (bản nháp 1, 19/11/2026)

Trạng thái: **đề xuất, chưa code**. Mục tiêu 7.3, sau khi 7.2 qua gate. Mọi điểm có thể đổi theo yêu cầu
của user; các điểm đánh dấu **[bất biến]** là ràng buộc an toàn, đổi phải có lý do ghi lại.

Tóm tắt một câu: một web UI mỏng chạy trên mig-host, **không chứa logic nghiệp vụ**, mọi hành động đều là một
lệnh `migrate …` nguyên văn mà người vận hành có thể tự gõ; **mỗi lúc chỉ một VM** đang migrate; hai tiến trình
tách quyền (web không root, runner root) nói chuyện qua unix socket với giao thức tối giản.

---

## 1. Phạm vi

Có:
- Trang Doctor, chọn VM (host / project / tên / file), Plan, Migrate (cold/live và mọi option hiện có), theo dõi
  tiến độ + log thời gian thực, Hủy, Rollback, Lịch sử (state dir, log batch).
- Hàng đợi FIFO: chọn 12 VM thì UI xếp hàng và **chạy từng con** [bất biến ở 7.3]; con sau chỉ bắt đầu khi con
  trước xong hoặc FAIL (giống `--continue-on-error` tùy chọn).

Không có (để sau, mục 11): song song, nhiều người dùng, phân quyền theo project, mở ra ngoài mig-host, lịch
chạy theo giờ, sửa conf từ UI.

## 2. Kiến trúc: ba tầng, hai tiến trình

```
trình duyệt của operator ──ssh -L 8787:127.0.0.1:8787──▶ mig-host
                                                          │
            ┌─────────────────────────────────────────────┤
            │ migui-web   (user migui, không root, sandbox systemd đầy đủ)
            │   HTTP 127.0.0.1:8787, HTML/JS tĩnh, session, CSRF, audit
            │   KHÔNG đọc /var/lib/mig-migrate, /opt/mig, openrc
            │        │  unix socket /run/migui/runner.sock (0660 root:migui), JSON từng dòng
            │ migui-runner (root, systemd, không mạng)
            │   nhận lệnh đã whitelist → Popen(["migrate", …]) → stream stdout
            │   giữ khóa "một job một lúc", SIGINT khi hủy
            │        │
            │ migrate CLI (như hiện nay) → /var/lib/mig-migrate, /var/log/mig-batch
            └─────────────────────────────────────────────
```

Lý do tách hai tiến trình: tiến trình nhận HTTP là nơi dễ bị tấn công nhất; nó chạy không root, không có
capability, không đọc được credential. Tiến trình root chỉ hiểu đúng 6 thông điệp và tự kiểm tra lại mọi
tham số — **không tin migui-web**.

Phụ thuộc: **chỉ stdlib Python** (`http.server` threading, `json`, `secrets`, `hmac`, `ssl` nếu cần) và JS
thuần. Không CDN, không pip từ internet: mig-host production có thể không ra ngoài. Toàn bộ ≈ 1 200 dòng.

## 3. CLI phải thêm gì (7.3, trước UI)

UI không cào text; CLI thêm output máy đọc. Mỗi cái là một cờ, hành vi cũ giữ nguyên:

| Lệnh | Thêm | Trả về |
|---|---|---|
| `migrate doctor --json` | | `{"checks":[{"level":"OK","text":…}],"fail":0}` |
| `migrate plan … --json` | | bảng Selected/Plan + từng dòng preflight theo VM |
| `migrate status --json` | lệnh mới, read-only | state dir đang sống, `.rolled-back.*`, job batch log, VM nào SHUTOFF+locked ở nguồn |
| `migrate migrate|volume|rollback … --progress-json` | | stdout vẫn như cũ, thêm dòng `@@ {"event":"copy","pct":37,"mbps":180}` cho UI parse; không có cờ thì không in |

Mọi dòng JSON đều **không chứa** giá trị env/openrc.

## 4. Giao thức web ↔ runner

Unix socket, mỗi thông điệp một dòng JSON, runner trả lời từng dòng, kết thúc bằng `{"end":rc}`.

| Thông điệp | Tham số | Runner kiểm gì |
|---|---|---|
| `doctor` | — | — |
| `list` | `selector` (host/project/vm/vm-file), `options` | whitelist mục 6 |
| `plan` | như `list` | như trên |
| `migrate` | `ident` (1 VM), `options`, `confirm_token` | 1 VM duy nhất; token khớp job đã plan; khóa rảnh |
| `rollback` | `ident`, `src_project`, `dst_project`, `confirm_token` | như trên |
| `cancel` | `job_id` | job đang chạy, SIGINT tới process group, chờ migrate tự dọn; SIGKILL chỉ khi operator bấm lần hai sau 120 s và được ghi audit |
| `status` | — | — |

Runner **không bao giờ** nhận chuỗi lệnh thô; nó lắp argv từ các trường đã kiểm và gọi `Popen(list, shell=False)`.

## 5. Màn hình

1. **Doctor** (trang đầu, bắt buộc xanh mới mở các trang khác).
2. **Chọn VM**: form selector → `plan --json` → bảng Selected/Plan tô màu; dòng PLAN-FAIL bung lý do; checkbox
   chọn con nào đưa vào hàng đợi.
3. **Option**: cold/live, ip same/new/addr, volume type, az, volume-az, flavor, flavor-fallback, security-group,
   no-copy-secgroup, server-group, no-server-group, verify, max-passes, delta-limit, start-source,
   keep-source-ports (ô đỏ, cần tick xác nhận riêng). Tooltip lấy từ `VM_OPTIONS` của CLI.
4. **Xác nhận**: hiện **lệnh nguyên văn** sẽ chạy (`migrate migrate web-01 --mode live --az AZ1 --yes`), ô gõ đúng
   tên VM, nút Chạy. Nghi thức "Type yes" của CLI được giữ dưới dạng gõ tên.
5. **Đang chạy**: log stream, thanh tiến độ, đồng hồ downtime từ `t-stop`, nút Hủy (hai bước).
6. **Hàng đợi**: thứ tự, kéo thả, bỏ khỏi hàng, dừng sau con hiện tại.
7. **Lịch sử / Rollback**: từ `status --json`; mỗi state dir có nút Rollback → màn 4 với lệnh rollback và
   `SRC_PROJECT/DST_PROJECT` của nó.

## 6. Bảo mật — mô hình đe dọa

Giả định: mig-host là máy chuyên dụng, có root của cả hai cloud trong `/opt/mig/*/admin-openrc.sh`; **chiếm được
migrate = chiếm được hai cloud**. Nên UI được thiết kế như một cổng vào tài sản mức cao nhất.

| Mối đe dọa | Hậu quả nếu bỏ ngỏ | Biện pháp |
|---|---|---|
| T1 Ai đó trên mạng gọi thẳng cổng 8787 | chạy migrate tùy ý | **[bất biến]** bind `127.0.0.1` only; vào bằng `ssh -L`; UI từ chối khởi động nếu bind khác loopback mà không có `--i-know-tls` + cert |
| T2 Tiến trình khác trên mig-host (user thường, container) nối vào 8787 | như T1 | vẫn bắt đăng nhập dù localhost: token 32 byte `secrets.token_urlsafe` sinh khi start, ghi `/var/lib/migui/token` 0600 root:migui, operator đọc qua SSH; đổi mỗi lần restart |
| T3 Dò token | như T1 | so sánh `hmac.compare_digest`; sai 5 lần/10 phút → khóa 15 phút, ghi audit; token không nằm trong URL (POST body), không log |
| T4 Cướp session | chạy lệnh dưới danh nghĩa operator | cookie `HttpOnly; SameSite=Strict; Path=/`; id 32 byte; hết hạn 8 h, idle 30 phút; `POST /logout` hủy ngay; một session một lúc (login mới vô hiệu session cũ) |
| T5 CSRF (trang web khác trong cùng trình duyệt gọi `POST /jobs` qua tunnel) | migrate/rollback ngoài ý muốn | `SameSite=Strict` + kiểm `Origin`/`Sec-Fetch-Site` phải là `http://127.0.0.1:8787` hoặc `localhost:8787` + header `X-Requested-With: migui` bắt buộc trên mọi request thay đổi; mọi hành động phá hoại thêm **confirm_token** riêng (mục 7) |
| T6 XSS từ dữ liệu cloud (tên VM `<img onerror=…>`, description port, log) | chạy JS trong phiên operator → T4/T5 | CSP `default-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'`; không inline script/style; render mọi chuỗi bằng `textContent`, không `innerHTML`; log hiển thị trong `<pre>` qua `textContent` |
| T7 Command injection qua tên VM / option | chạy lệnh trên mig-host với root | **[bất biến]** không shell, argv list; whitelist mục 6.1; ident ưu tiên UUID chọn từ listing; tên gõ tay phải khớp `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` và không bắt đầu bằng `-`; giá trị option theo enum/regex/ipaddress; runner kiểm lại lần hai |
| T8 Path traversal (job id, file log, vm-file) | đọc file root | job id do server sinh (`secrets.token_hex(8)`), chỉ `[0-9a-f]{16}`; `--vm-file` không nhận đường dẫn từ UI, UI gửi danh sách tên, runner ghi file tạm 0600 trong `/run/migui/` |
| T9 Lộ credential | mất hai cloud | migui-web không có quyền đọc `/opt/mig`, `/root`, state dir; runner không truyền env của openrc cho web; **scrub**: mọi dòng log đi qua web bị lọc `OS_PASSWORD=`, `OS_APPLICATION_CREDENTIAL_SECRET=`, `X-Auth-Token`, `key = AQ…` trước khi gửi ra trình duyệt (phòng migrate in nhầm) |
| T10 Chạy hai migrate đè nhau (double click, hai tab) | hai VM cùng tắt, hoặc cùng VM hai lần | **[bất biến]** runner giữ `flock` `/run/migui/runner.lock`; `migrate` CLI đã tự chặn state-exists; UI khóa nút khi có job |
| T11 Hủy sai cách làm sót clone/port | VM nguồn kẹt, LUN treo | hủy = SIGINT process group → migrate tự `cleanup_attach`; UI hiện hint rollback sau khi hủy; SIGKILL cần bấm lần hai, có cảnh báo, ghi audit |
| T12 Mất dấu ai làm gì | không điều tra được | audit append-only `/var/log/migui/audit.jsonl` (ts, session id, action, argv, job id, rc) + journald; file `chattr +a` nếu fs hỗ trợ; UI không có nút xóa audit |
| T13 Lỗ hổng trong tiến trình web → leo thang | root | migui-web chạy user `migui`, systemd: `NoNewPrivileges=yes`, `ProtectSystem=strict`, `ProtectHome=yes`, `PrivateTmp=yes`, `PrivateDevices=yes`, `CapabilityBoundingSet=`, `RestrictAddressFamilies=AF_UNIX AF_INET`, `IPAddressAllow=127.0.0.1`, `ReadWritePaths=/var/lib/migui /var/log/migui`, `SystemCallFilter=@system-service` |
| T14 Runner bị gọi bởi tiến trình khác | như T1 | socket 0660 root:migui; runner kiểm `SO_PEERCRED` uid == migui; từ chối mọi thông điệp ngoài 7 loại; không bao giờ `eval`/`shell` |
| T15 DoS (gửi `plan --host` liên tục, log khổng lồ) | treo mig-host | mỗi session tối đa 1 job + 1 plan đang chạy; log buffer 50 MB rồi cắt đầu, file đầy đủ vẫn ở `/var/log/mig-batch`; body request ≤ 64 KB |
| T16 Trình duyệt cache dữ liệu nhạy cảm | lộ tên VM/IP khi share máy | `Cache-Control: no-store` toàn bộ API; không dùng localStorage cho gì ngoài tab đang mở |
| T17 Dependency độc | như T13 | stdlib-only, không npm, không pip; cập nhật = cập nhật Python hệ thống |

Không làm ở 7.3 (ghi rõ để không ai tưởng có): TLS, OIDC/LDAP, nhiều user, mở ra ngoài loopback. Nếu sau này
cần mở ra mạng quản trị, mục 11.

### 6.1. Whitelist tham số (runner kiểm lại, web kiểm trước)

| Trường | Hợp lệ |
|---|---|
| ident | UUID, hoặc `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` (và quy tắc C5: không `/`) |
| host, project, volume-type, az, volume-az, flavor, server-group, security-group (mỗi phần tử) | `^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$` |
| mode | `cold`\|`live`; ip | `same`\|`new`\|IPv4 hợp lệ (ipaddress); verify | `readback`\|`full`\|`none` |
| max-passes 1–50, delta-limit 0–65536 | số nguyên |
| cờ bool | chỉ các cờ trong `FLAG_OPTS` của CLI, đọc từ CLI lúc runner khởi động (không hardcode hai nơi) |
| src/dst project cho rollback | regex như host |

Mọi thứ khác → 400, không chạy, ghi audit `rejected`.

## 7. Xác nhận hai bước (chống nhầm tay và chống CSRF phá hoại)

1. `POST /jobs/plan` → server chạy `plan --json`, lưu job `PLANNED` kèm `confirm_token = token_hex(16)` gắn với
   (session, ident, options đã chuẩn hóa), hạn 15 phút.
2. UI hiện lệnh nguyên văn + ô gõ tên VM.
3. `POST /jobs/{id}/run` body `{"typed": "<tên VM>", "confirm_token": …}` → server so tên đã gõ với ident/tên
   thật trong plan và token; sai → 409, token hủy. Rollback và cancel-SIGKILL cùng cơ chế.

Hệ quả: dù T5 lọt, kẻ tấn công cũng không có confirm_token (nó chỉ trả về trong response của phiên hợp lệ,
không nằm trong cookie).

## 8. Máy trạng thái job

```
QUEUED → PLANNING → PLANNED → (confirm) → RUNNING → OK | FAIL | CANCELLED
                       └─ PLAN-FAIL (dừng, có lý do)
```

- Chỉ một job `RUNNING` toàn hệ thống [bất biến]. Hàng đợi chuyển con kế sang PLANNING khi con trước rời RUNNING.
- RUNNING ghi `pid`, `pgid`, `log path`, `t-stop` (từ state dir qua `status --json`).
- Restart migui-runner khi đang RUNNING: runner **không** giết migrate; khởi động lại nó đọc `status --json`,
  thấy state dir sống không có job sở hữu → hiển thị "đang chạy ngoài UI / hoặc chết dở, kiểm tra log batch".

## 9. Phân quyền file

| Đường dẫn | Owner/mode | Ai đọc |
|---|---|---|
| `/var/lib/migui/token` | root:migui 0640 | web (đọc 1 lần khi start) |
| `/var/lib/migui/jobs/<id>.json` | migui 0600 | web |
| `/var/log/migui/audit.jsonl` | root:migui 0620 (web chỉ append), `chattr +a` | root |
| `/run/migui/runner.sock` | root:migui 0660 | web |
| `/var/lib/mig-migrate`, `/var/log/mig-batch`, `/opt/mig` | root 0700 như nay | chỉ runner/migrate |

Web **không** có quyền đọc log batch trực tiếp: nó chỉ có bản copy stream do runner đẩy qua socket (đã scrub).

## 10. Kiểm thử bảo mật tối thiểu trước khi dùng (viết thành `tests/ui-security.sh`)

1. `curl` không cookie → 401 cho mọi API; trang tĩnh vẫn 200.
2. Sai token 6 lần → 429 và audit có `locked`.
3. `POST /jobs/plan` thiếu `X-Requested-With` hoặc `Origin: http://evil` → 403.
4. ident = `web-01; id`, `$(id)`, `--yes`, `../x`, `a/b` → 400, không có tiến trình migrate nào sinh ra (kiểm `ps`).
5. Chạy run với confirm_token của job khác / hết hạn / tên gõ sai → 409.
6. Hai `POST /run` song song → một 202, một 423 (locked).
7. Mở socket runner bằng user khác migui → bị từ chối (PEERCRED).
8. Tên VM chứa `<script>` → hiển thị nguyên văn, CSP báo cáo 0 vi phạm.
9. Bind thử `0.0.0.0` không cert → tiến trình từ chối start.
10. grep audit/log web không có `OS_PASSWORD`, `SECRET`, `AQ[A-Za-z0-9+/=]{30,}`.

## 11. Mở rộng sau (không ảnh hưởng thiết kế trên)

| Nhu cầu | Thay gì |
|---|---|
| Nhiều VM song song | chỉ runner: khóa → semaphore N, cutover vẫn qua khóa riêng (xem trao đổi 19/11 về `--parallel`) |
| Mở ra mạng quản trị | thêm TLS (cert nội bộ) + OIDC/mTLS ở migui-web; runner không đổi |
| Nhiều operator | user table + role (viewer/operator) ở web; audit đã có session id |
| Lịch chạy | queue có `not_before`; runner không đổi |

## 12. Khối lượng và thứ tự

1. CLI `--json`/`status`/`--progress-json` (≈ 250 dòng, có unit test) — làm trong 7.3 trước.
2. migui-runner (≈ 300 dòng) + test socket/whitelist/lock.
3. migui-web (≈ 400 dòng Python + 500 dòng HTML/JS/CSS) + `tests/ui-security.sh`.
4. systemd units, `DEPLOY-UI.md`, chạy thử trên mig-host lab với demo-vm.

Ước lượng Opus: 2 ngày code + 1 ngày test. Mọi số ở trên là ước lượng, không phải đo.

---

## 13. Bổ sung 19/11 (user): mỗi job phải xem được chạy với option gì

Nguyên tắc: UI hiển thị đúng cái CLI đã ghi, không suy diễn lại. Ba lớp:

### 13.1. CLI ghi đủ (7.3, cùng đợt với `--json`)

- File `options` trong state dir giữ dòng cũ (grep ổn định) và thêm file mới `options.json`:

```json
{"command": ["migrate", "migrate", "web-01", "--mode", "live", "--az", "AZ1", "--yes"],
 "effective": {
   "mode":        {"value": "live",  "from": "cli"},
   "ip":          {"value": "same",  "from": "default"},
   "volume_type": {"value": "ssd",   "from": "AZ_TYPE_MAP (AZ1)"},
   "az":          {"value": "AZ1",   "from": "cli"},
   "volume_az":   {"value": "AZ1",   "from": "az"},
   "verify":      {"value": "none",  "from": "MIG_DEFAULT_OPTS"},
   "flavor":      {"value": "m1.large", "from": "source"},
   "security_groups": {"value": "copy <AZ>-<name>", "from": "default"},
   "server_group": {"value": "AZ1-web-anti", "from": "source"},
   "pool":        {"value": "volumes-nvme", "from": "RBD_POOL_MAP"}},
 "conf": {"path": "/opt/mig/migrate.conf", "MIG_DEFAULT_OPTS": "--verify none", "MIG_AZ": "", "AZ_TYPE_MAP": "AZ1:ssd",
          "RBD_POOL_MAP": "ssd:volumes-nvme", "SRC_PROJECT": "prod", "DST_PROJECT": "prod"},
 "tool_version": "7.3.0", "started": "2026-11-19T20:10:03Z", "ident": "web-01", "server_id": "…"}
```

  `from` dùng đúng các nguồn `Options` đã theo dõi (`cli_given`, `def_shown`, `az_from`, `type_from`) nên không có
  logic mới, chỉ là ghi ra. Không ghi giá trị env/openrc.
- `status --json` trả `options.json` của từng state dir (sống lẫn `.rolled-back.*`), và `plan --json` trả cùng khối
  `effective` trước khi chạy, để UI hiện **y hệt** cái sẽ chạy.

### 13.2. Runner lưu bản ghi job

`/var/lib/migui/jobs/<id>.json`: `argv` nguyên văn, `effective` (từ plan), `conf` snapshot, ident, project
src/dst, ai bấm (session id), thời điểm plan/confirm/start/end, rc, đường dẫn log batch, state dir. Bất biến sau
khi job kết thúc (web chỉ ghi append trạng thái).

### 13.3. UI hiển thị

- Màn Xác nhận và màn Đang chạy: bảng **Option hiệu lực** 3 cột `option | giá trị | nguồn`, tô màu nguồn
  (gõ tay / MIG_DEFAULT_OPTS / conf map / mặc định / lấy từ nguồn). Dòng lệnh nguyên văn ở trên bảng, nút copy.
- Màn Lịch sử: mỗi job một dòng, bung ra là bảng trên + conf snapshot + link log; nút **"Chạy lại với cùng
  option"** (đưa về màn Option đã điền sẵn, vẫn phải plan + xác nhận lại) và **"So với job khác"** (diff 2 bảng
  effective, tô dòng khác nhau — dùng khi hỏi "sao con này vào pool khác con kia").
- Rollback hiện option của job gốc để operator biết VM đã migrate `--ip same` hay `new` trước khi bấm.

Cái gì không có trong `options.json` thì UI ghi "không ghi nhận (job chạy bằng bản < 7.3)" chứ không đoán.

---

## 14. Bổ sung 20/11 (user): chọn theo host/project, nhiều instance, đích chọn host/project, volume, rollback

User chốt luồng chọn: **chọn host và project nguồn → hiện danh sách instance → tick 1 hay nhiều, có ô tìm
nhanh → chọn đích (host hoặc project) → chọn option → chạy**. Volume migrate có màn riêng cùng kiểu. Rollback
giữ. Các mục dưới thay §5 (màn 2–4) và bổ sung §3; phần bảo mật §6–§10 không đổi.

### 14.1. Màn "Chọn VM" (thay §5.2 + §5.3)

```
┌ NGUỒN ──────────────────────────┐  ┌ ĐÍCH ───────────────────────────────┐
│ Host    [compute-03 ▾] (tùy)    │  │ Project [prod ▾]  (mặc định = nguồn,  │
│ Project [prod       ▾] (tùy)    │  │          PROJECT_MAP_FILE nếu có)     │
│ [Tải danh sách]                 │  │ AZ      [AZ1 ▾]                       │
└─────────────────────────────────┘  │ Host    [Nova scheduler chọn ▾]       │
                                     │          hoặc một hypervisor của AZ1  │
┌ INSTANCE (24) ─ tìm: [web-0_____] ─ đã chọn 3 ─ [chọn hết đang hiện] ─────┐
│ ☑ web-01  ACTIVE  BFV 2 vol 60G  192.168.120.30  compute-03  prod  PLAN-OK │
│ ☑ web-02  ACTIVE  BFV 1 vol 40G  192.168.120.31  compute-03  prod  PLAN-OK │
│ ☐ db-01   ACTIVE  BFV 3 vol 500G 192.168.120.40  compute-03  prod  SKIP đã migrate → [Rollback] │
│ ☐ tmp-7   SHUTOFF ephemeral      192.168.120.77  compute-03  prod  PLAN-FAIL (quota gigabytes) │
└────────────────────────────────────────────────────────────────────────────┘
┌ OPTION cho 3 VM đã chọn ─────────────────────────────────────────────────┐
│ mode (cold|live)  ip (same|new|<addr> theo từng dòng)  volume-type  az   │
│ volume-az  flavor  flavor-fallback  verify  max-passes  delta-limit       │
│ ☐ start-source  ☐ no-copy-secgroup  ☐ no-server-group  ☐ keep-source-ports (đỏ) │
│ [Plan 3 VM]                                                               │
└───────────────────────────────────────────────────────────────────────────┘
```

- **Host / project nguồn**: hai dropdown, ít nhất một ô. Host lấy từ `compute service list` nguồn (cột Host,
  đúng cái selector `--host` đang dùng), project từ `project list`. Chọn cả hai = giao nhau, như selector CLI.
- **Danh sách instance**: lệnh mới `migrate list --json --host H --project P` (read-only, = selector không hành
  động) trả mỗi VM: id, name, status, boot (bfv/ephemeral), số volume + tổng GB, IP theo network, host, project,
  và **state sống** nếu `/var/lib/mig-migrate/<id|name>` có (→ dòng ghi "đã migrate" + nút Rollback tại chỗ).
- **Tìm nhanh**: lọc **phía trình duyệt** trên danh sách đã tải (tên, ID, IP, host) — không gọi lại CLI, gõ là
  lọc ngay. "Chọn hết đang hiện" chỉ tick những dòng còn lại sau lọc. Tick được nhiều; mỗi VM tick vẫn thành
  **một job riêng** trong hàng đợi, chạy từng con [bất biến §1].
- **Đích**: project (dropdown project đích; mặc định tên trùng nguồn, có `PROJECT_MAP_FILE` thì hiện tên map và
  nguồn "map file"); AZ (zone đích, resolver §P6: chỉ một zone thì tự điền); **host đích**: dropdown
  hypervisor đích đang up/enabled trong AZ đã chọn, mặc định **"Nova scheduler chọn"**. Chọn host → CLI
  mới `--dst-host <hypervisor>` (14.4).
- **Option**: một bộ cho cả đợt đã chọn, cùng tooltip từ `VM_OPTIONS`; riêng `--ip <addr>` là ô theo từng
  dòng (mỗi VM một IP). Option nguy hiểm (`keep-source-ports`) đỏ + tick riêng + bắt gõ tên VM ở màn xác nhận.
- Bấm **Plan N VM** → runner chạy `plan --json` cho từng VM với bộ option đó → cột cuối bảng tô PLAN-OK /
  PLAN-FAIL (bung lý do) / SKIP. Chỉ dòng PLAN-OK mới sang màn xác nhận.
- **Giới hạn 32** (20/11, trước là 16): một lần tải danh sách nhận tối đa 32 host và 32 project nguồn
  (`LIST_MAX` ở runner, `HOSTS_MAX` ở app.js cho "— mọi host —"), vì vận hành hay đưa nhiều host vào bảo trì cùng lúc.

### 14.2. Màn "Xác nhận" cho một đợt (thay §5.4 và cách gõ của §7)

Trước đây mỗi VM dừng ở màn xác nhận gõ tên; với tick nhiều con thì đổi thành **xác nhận một lần cho cả đợt**:

- Liệt kê **N lệnh nguyên văn**, mỗi VM một dòng, bung ra là bảng option hiệu lực §13 của VM đó (plan --json).
- Ô gõ: **số VM** của đợt (ví dụ `3`) — buộc operator đếm lại danh sách; sai → 409, `confirm_token` hủy.
  Đợt chỉ có 1 VM thì gõ tên VM như cũ.
- **Ngoại lệ phải gõ tên từng VM**: đợt bật `--keep-source-ports`, hoặc `--ip <addr>` gõ tay, hoặc rollback.
- Hàng đợi vẫn chạy từng con; mặc định **dừng đợt nếu một con FAIL** (tick "tiếp tục" = `--continue-on-error`).
- `confirm_token` gắn với tập (ident, options) của cả đợt; đổi một option là phải plan lại.

### 14.3. Màn "Volume" (mới)

Cùng khung với màn VM, áp cho `migrate volume` / `plan volume` / `rollback vol-<id>`:

- Nguồn: project (bắt buộc) → `migrate list --json --project P --volumes` trả volume: id, name, size, status
  (available/in-use + VM đang gắn), type, AZ, state sống `vol-<id>` nếu có.
- Bộ lọc: `available` / `attached` / `all` (= cờ CLI), tìm nhanh phía trình duyệt. Tick nhiều.
- Đích: project, AZ (`--az`), `--volume-az`, `--volume-type`, `--verify`.
- `--in-use` (copy volume đang gắn, crash-consistent): chỉ hiện khi **đúng một** volume in-use được tick; bắt
  tick xác nhận riêng vì dữ liệu chỉ nhất quán tới thời điểm clone.
- Mỗi volume một job `migrate volume <id> …`, xác nhận bằng số volume; rollback = `migrate rollback vol-<id>`
  (xóa bản copy, không đụng nguồn) với nút ngay trên dòng có state sống.

### 14.4. CLI thêm cho 7.3 (bổ sung bảng §3)

| Lệnh / cờ | Mục đích | Ghi chú |
|---|---|---|
| `migrate list --json [--host H] [--project P] [--vm …] [--volumes --available\|--attached\|--all]` | nguồn dữ liệu cho bảng instance / volume | read-only; dùng lại selector hiện có; thêm cột boot, volumes, IPs, state sống |
| `migrate inventory --json` | host, project, AZ, flavor, volume type của **cả hai** cloud | 1 lệnh để UI điền dropdown; không trả env |
| `--dst-host <hypervisor>` | ép server đích lên một host | `server create --host` (Nova µversion ≥ 2.74, cần admin). Preflight: host có trong `compute service list` đích, `nova-compute` up + enabled, **AZ của host = --az** (khác → FAIL, không để Nova trả NoValidHost sau khi đã tắt nguồn). Không có cờ = scheduler chọn (hành vi hiện nay) |

### 14.5. Rollback

- Nút Rollback ở ba chỗ: dòng instance/volume có state sống (14.1/14.3), màn Lịch sử (§5.7), màn Đang chạy
  khi job vừa FAIL sau `t-stop`. Luôn qua màn xác nhận gõ **tên VM / volume** (không dùng số đếm), hiện option
  gốc của job (`--ip same` hay `new`, có tháo port không) trước khi bấm, như §13.3.

### 14.6. Thay đổi so với bản 19/11

| Mục | 19/11 | 20/11 |
|---|---|---|
| Chọn nguồn | radio host/project/tên/file | dropdown host + project → bảng tick nhiều + tìm nhanh |
| Đích | chỉ AZ qua option | project + AZ + host (mặc định scheduler) |
| Xác nhận | gõ tên từng VM | gõ **số VM** của đợt; gõ tên khi 1 VM / option nguy hiểm / rollback |
| Volume | chưa có | màn riêng, cùng khung |
| CLI mới | `--json`, `status`, `--progress-json` | + `list --json`, `inventory --json`, `--dst-host` |

## 15. Bổ sung 20/11 (user): nút đang chạy phải khóa — rà toàn bộ nút

Luật chung (áp cho MỌI nút gọi API): bấm → nút `disabled` + chữ đổi thành "Đang …" (hoặc giữ chữ, thêm "…") →
request trả về (thành công hay lỗi) → mở lại. Một helper `busy(button, fn)` duy nhất, không viết tay từng chỗ.
Thêm 3 khóa theo trạng thái: (a) `cf-run` khóa khi đang có job RUNNING (`S.active`), chữ "Đang có job khác chạy";
(b) `vm-plan`/`vol-plan` khóa chéo khi phiên này đang có plan chạy (runner trả 429); (c) form login khóa trong lúc
gửi (bấm Enter hai lần với token sai = 2 lần tính vào lockout).

| Nút | Hiện trạng 02ffc46 | Phải sửa |
|---|---|---|
| form `login-form` | không khóa | khóa input + nút trong lúc gửi |
| `logout` | không khóa | khóa trong lúc gửi |
| `doctor-run` | ĐÃ khóa + "Đang chạy…" | giữ (chuẩn mẫu) |
| `vm-load` | ĐÃ khóa | thêm chữ "Đang tải…" |
| `vol-load` | không khóa | như vm-load |
| `vm-select-shown`/`vm-select-none`/`vol-*` | không gọi API | không đổi |
| `vm-plan` / `vol-plan` | ĐÃ khóa + "Đang plan N…" | thêm khóa chéo (b) |
| `vm-to-confirm` / `vol-to-confirm` | khóa khi chưa có batch | không đổi |
| `cf-run` | khóa khi bấm, mở lại khi lỗi | thêm (a); khi 423 hiện lý do ngay trên nút |
| `cf-back` | điều hướng | không đổi |
| `run-cancel` | 2 lần bấm, khóa sau SIGINT | khóa trong lúc gửi request |
| `run-kill` | không khóa lúc gửi | khóa trong lúc gửi; sau khi gửi xong giữ khóa (đã SIGKILL) |
| `run-stop-after` | không khóa lúc gửi | khóa trong lúc gửi; sau đó khóa luôn (đã có `S.active.stop`) |
| `run-rollback`, nút "Rollback…" trong bảng Instance/Volume/Lịch sử | không khóa, bấm đôi tạo 2 batch | khóa trong lúc `/api/rollback` chạy |
| `run-download`, "Tải log" | link | không đổi |
| `hist-refresh` | không khóa | khóa trong lúc tải |
| "chi tiết", "So với job khác", "Chạy lại với cùng option" | không gọi API | không đổi |

Test (tests/ui/functional): fake runner trả chậm 2 s cho `/api/plan`; bấm plan hai lần trong 2 s → runner chỉ nhận
1 message; login gửi hai lần với token sai khi request đầu chưa về → audit chỉ 1 `login-failed`. CSS: nút
`disabled` mờ + con trỏ `not-allowed` (đã có cho run-kill, áp chung).

## 16. Bổ sung 20/11 (user): đăng nhập bằng username + password (thay token)

Token sinh mỗi lần restart (T2) bất tiện: operator phải SSH đọc lại. Thay bằng tài khoản có mật khẩu, lưu
trên mig-host, tạo/đổi bằng lệnh root `migui-passwd`. Không có "đăng ký", không đổi mật khẩu qua web (mục 11,
làm sau): web (user migui) không có quyền ghi file tài khoản, đúng tinh thần §2 (web không giữ gì quý).

### 16.1. Kho tài khoản

| Mục | Quyết định |
|---|---|
| File | `/var/lib/migui/users` — JSON, **root:migui 0640** (web chỉ đọc), ghi nguyên tử (tmp `O_EXCL` 0640 cùng thư mục + `os.replace`), `flock` file `users.lock` khi sửa |
| Nội dung | `{"version":1,"users":{"<name>":{"kdf":"scrypt","n":32768,"r":8,"p":1,"salt":"<hex 16 byte>","hash":"<hex 32 byte>","created":"<utc>","changed":"<utc>"}}}` |
| Giới hạn | ≤ 64 KB, ≤ 50 user, đọc lại **mỗi lần login** (đổi mật khẩu không cần restart); file thiếu/hỏng → 503 "no users file: run migui-passwd add" (không 401, để operator biết là lỗi cài đặt) |
| KDF | `hashlib.scrypt` n=2¹⁵ r=8 p=1 dklen=32 **maxmem=64 MiB** (≈32 MiB, đo trên mig-host ≈125 ms; *code: maxmem phải truyền tường minh — mặc định 32 MiB của OpenSSL làm Python 3.12.3 báo "memory limit exceeded"*); các tham số lưu cùng bản ghi để sau nâng n không phá user cũ. Không PBKDF2 fallback: Python 3.10+/OpenSSL 1.1 trên Ubuntu có scrypt; thiếu thì `migui-passwd` và web báo lỗi rõ khi start |
| Username | `^[a-z][a-z0-9._-]{0,31}$` (thường, để không nhầm hoa/thường) |
| Mật khẩu | 12–128 ký tự, bất kỳ ký tự in được; ≠ username (không phân biệt hoa/thường); không ép ký tự đặc biệt (NIST 800-63B) |

### 16.2. Lệnh `migui-passwd` (module `migui/passwd.py`, entry `/opt/migui/bin/migui-passwd`)

```
migui-passwd add  <user>        # tạo; hỏi mật khẩu 2 lần qua getpass (không echo)
migui-passwd set  <user>        # đổi mật khẩu
migui-passwd del  <user>
migui-passwd list               # tên + created/changed, không bao giờ in hash/salt
   --stdin                      # đọc 1 dòng mật khẩu từ stdin (script), KHÔNG nhận qua argv/env
   --users-file PATH            # mặc định /var/lib/migui/users (test dùng đường dẫn tạm)
   --audit PATH                 # *thêm khi code*: mặc định /var/log/migui/audit.jsonl khi dùng users file mặc định,
                                # không ghi audit khi --users-file khác (để test không ghi vào audit thật)
```

- Chạy không phải root với đường dẫn mặc định → exit 2 "run as root". Đường dẫn khác (test) thì bỏ chown.
- `add` từ chối tên đã có; `set`/`del` từ chối tên chưa có; `del` user cuối cùng vẫn cho nhưng in cảnh báo
  "no users left: nobody can log in".
- Mật khẩu vi phạm 16.1 → exit 1, không ghi gì. Hai lần gõ không khớp → exit 1.
- Ghi audit `/var/log/migui/audit.jsonl` (who=`passwd`, action `user-add|user-set|user-del`, detail `user`) —
  không ghi mật khẩu, không ghi hash.

### 16.3. Đăng nhập (thay T2/T3 §6)

| Mục | Quyết định |
|---|---|
| Request | `POST /api/login` body `{"username","password"}`; cả hai phải là string; username sai regex hoặc password > 128 → **400 trước khi băm** (không tốn scrypt cho rác) |
| So khớp | tìm user → `scrypt(password, salt, n, r, p)` → `hmac.compare_digest(hash)`. **User không tồn tại vẫn băm** với salt giả cố định (sinh khi web start) để thời gian trả lời không lộ tên user có thật |
| Lỗi | một thông điệp duy nhất `401 "wrong username or password"` cho cả hai trường hợp |
| Lockout | giữ T3: toàn cục 5 lần sai/10 phút → khóa 15 phút (tool một operator, không cần theo user/IP). Audit `login-failed` ghi `user` đã qua regex (sai regex → `"(invalid)"`) |
| Băm tuần tự | một `threading.Lock` riêng quanh scrypt: tối đa 1 băm chạy, request login khác chờ. Chặn DoS bộ nhớ (N request × 32 MiB). `MemoryMax` của migui-web.service phải ≥ 128M (kiểm lại unit) |
| Phiên | `Session.user = <name>`; `session_info` trả `user`; vẫn **một phiên** (T4): login mới thay phiên cũ, audit `session-replaced` ghi cả user cũ và mới |
| Web → runner | mọi message runner thêm `"user": <name>`; runner kiểm regex, **từ chối 400 nếu thiếu/sai**, ghi `operator` vào bản ghi batch/job và audit — Lịch sử/chi tiết job hiện "operator" |
| Audit web | mọi dòng có phiên thêm `user` (Audit detail) |
| Gỡ token | runner **không** ghi `/var/lib/migui/token` nữa (xóa `write_token`), web bỏ `--token-file` thay `--users-file`, install.sh bỏ dòng "token", in "tạo user đầu tiên: `sudo /opt/migui/bin/migui-passwd add <name>`"; install.sh xóa file token cũ nếu còn |
| Trình duyệt | form `username` (`autocomplete=username`) + `password` (`autocomplete=current-password`); góc trên hiện `user · phiên hết hạn sau …`; khóa form khi đang gửi (§15 c) giữ nguyên |

### 16.4. Kiểm thử bắt buộc

tests/ui/security.py thêm: (1) sai user và sai pass trả **cùng** status + message; (2) 6 lần sai → 429 + audit
`locked`; (3) user không tồn tại vẫn gọi scrypt đúng 1 lần (đo qua hàm `verify()` tách riêng, patch
`hashlib.scrypt`); (4) password 129 ký tự → 400, scrypt không được gọi; (5) users file thiếu → 503, JSON hỏng →
503, file thừa user thứ 51 → 503; (6) message runner thiếu `user` hoặc `user` sai regex → 400 (gửi thẳng
socket). tests/ui/functional.py: login đúng trả `user`; audit `login`/`plan`/`run` có `user`; bản ghi job có
`operator`. tests/ui/passwd.py (mới): add/set/del/list, `--stdin`, từ chối < 12 ký tự / = username / tên trùng /
tên sai regex, file sau ghi là 0640, ghi hỏng giữa chừng không để lại file rách (tmp bị xóa). harness.py login
bằng user `test` tạo qua `migui-passwd --users-file`.

### 16.5. Không làm trong đợt này (→ §11)

Đổi mật khẩu từ web (cần op runner `passwd` kiểm mật khẩu cũ), nhiều vai trò (xem/chạy), TOTP, khóa theo user.

## 17. Bổ sung 20/11 (user): màn Xác nhận phải xem được nội dung plan

Hiện mỗi dòng lệnh chỉ bung "option hiệu lực" và ẩn mọi check mức OK, nên operator không thấy plan đã kiểm gì
(user: "kg xem đc nội dung plan"). `plan --json` trả về mỗi row đủ `checks` (15 dòng cho demo-vm, level
OK/INFO/WARN/FAIL + text), `counts`, `effective`, `log`, `command`.

| Chỗ | Phải làm |
|---|---|
| Màn Xác nhận, mỗi lệnh | `<details>` đổi nhãn thành `nội dung plan · N check (x OK · y WARN · z FAIL) · option hiệu lực`; bên trong theo thứ tự: (1) bảng check **đủ mọi mức** như bảng Doctor (pill level + text, WARN/FAIL tô màu), (2) bảng option hiệu lực như cũ, (3) dòng `log plan: <row.log>` và lệnh plan nguyên văn `row.argv`. Mỗi dòng có FAIL/WARN thì details **mở sẵn**; PLAN-OK toàn OK thì đóng |
| Runner | `Job.FIELDS` thêm `checks` (từ row của plan) để bản ghi job giữ plan lúc đó; `status` trả kèm |
| Màn Đang chạy + chi tiết job ở Lịch sử | thêm mục "Plan lúc đó" (bảng check như trên) dưới bảng option; job cũ không có `checks` → "không ghi nhận" |
| Test | functional: plan 1 VM qua fake runner có 3 check (OK, WARN, FAIL) → response row giữ đủ 3; job record sau run có `checks`; guards.mjs: không innerHTML. |

## 18. Bổ sung 20/11 (user): thấy đang plan con nào

Hôm nay bấm Plan chỉ thấy nút "Đang plan N…" rồi chờ: web `collect()` gom hết reply của runner rồi mới trả một
JSON, dù runner đã `send({"row": row})` ngay khi mỗi con plan xong. Với 10 VM, plan ~10–20 s/con, operator
ngồi 3 phút không biết gì.

| Chỗ | Phải làm |
|---|---|
| Runner `op_plan` | trước mỗi lệnh plan gửi `{"planning": {"ident","name","i","n","t"}}`; sau mỗi con vẫn `{"row": row}`; cuối `{"batch": …}` + `{"end": rc}` như cũ |
| Web `POST /api/plan` | trả **SSE** (`text/event-stream`, cùng cơ chế `sse()` của job stream, ping 15 s) thay vì gom: sự kiện `planning`, `row`, `batch`, `end`, `error`. Browser gọi bằng `fetch` POST (EventSource không POST được) và đọc `ReadableStream`; header Origin/X-Requested-With/cookie giữ nguyên nên T5 không đổi. Audit `plan` ghi ở sự kiện `end` như cũ |
| Màn Chọn VM / Volume | khung **"Đang plan"** ngay dưới nút Plan: mỗi ident đã chọn một dòng `tên · trạng thái` với trạng thái chờ → đang plan (từ hh:mm:ss, đếm giây) → PLAN-OK (x OK · y WARN · z FAIL) / PLAN-FAIL (dòng info đầu). Dòng tổng: "plan i/N · đã mất mm:ss". Xong hết: dòng tổng thành "plan N VM mất mm:ss · a OK · b FAIL", nút "Sang xác nhận" mở như cũ. Khung giữ nguyên đến lần plan sau; FAIL xem được lý do tại chỗ (bung `<details>` với checks nếu có) |
| Khóa nút | §15 (b) giữ: `S.planning` suốt stream; mất stream giữa chừng (mạng/tunnel) → dòng tổng "mất kết nối, plan vẫn chạy trên mig-host; bấm Plan lại sẽ chờ runner rảnh" và mở nút |
| Giới hạn | runner vẫn một plan một lúc (429 khi bận) — hiện ngay trong khung. Hủy plan giữa chừng: **không** (đợt sau) |
| Test | functional: fake migrate plan chậm 1 s/con, 3 con → web stream có 3 `planning` + 3 `row` theo thứ tự trước `batch`; client ngắt giữa chừng → runner vẫn plan xong và batch vẫn có (PLANNED, dùng được bằng id? không: plan gắn phiên, batch vẫn nằm trong runner nhưng UI không giữ id → ghi rõ trong khung "plan lại"). security: stream `/api/plan` không có cookie → 401; thiếu X-Requested-With → 403 |

*Code (20/11), lệch so với bảng:* `planning` có thêm `idents` (volume: một lần `plan volume` cho cả project đánh dấu mọi volume của project đó); runner từ chối trước sự kiện đầu (400/409/429) thì web trả JSON lỗi với status đó như mọi POST, sự kiện `error` chỉ dùng cho lỗi giữa stream; audit web `plan` thêm `client=connected|gone`.

## 19. Bổ sung 20/11 (user): hai lỗi khi bấm Rollback sau khi tải lại trang

Hiện tượng (audit 11:43–11:49Z, phiên `437b…`): từ Lịch sử bấm "Rollback…" → bị đá sang màn Doctor; chạy
Doctor rồi bấm lại → gõ tên → 409 "typed confirmation does not match" hai lần.

| # | Nguyên nhân | Sửa |
|---|---|---|
| 19.1 | `route()` đòi `S.doctorOk` cho `#confirm`; sau tải lại trang phiên mới `doctor_ok=false` nên confirm rollback bị chuyển sang Doctor không một lời | Khi login xong mà `doctor_ok=false` → **tự chạy Doctor** một lần (nút hiện "Đang chạy…", khung Doctor hiện kết quả). `route()` gặp `#confirm` khi chưa doctor → giữ `S.confirm`, flash "Doctor chưa chạy trong phiên này, đang chạy…", chạy Doctor, xanh thì quay lại `#confirm`; đỏ thì ở Doctor kèm lý do |
| 19.2 | `op_rollback` lấy tên hiển thị từ cache `self.known` (chỉ có sau op `list`); runner vừa restart (install) và phiên mới chưa tải bảng Instance → `name = ident` (UUID) → màn hình bảo gõ tên nhưng tên là UUID, operator gõ `mig-peer` → mismatch | Runner đọc tên từ **`server.json` trong state dir** của entry (`entry["dir"]`, phải nằm dưới `--state-root`, `O_NOFOLLOW`, ≤ 1 MiB, `name` qua NAME_RE), rồi mới tới cache `known`, cuối cùng mới ident. Rule rollback: `expect` = `[name]`, và `typed_matches` cho rollback nhận **thêm ident** (gõ UUID đầy đủ cũng là cố ý). Màn Xác nhận rollback in rõ: "Gõ đúng tên VM: `mig-peer`" (tên nằm trong label, không bắt operator đoán) |
| Test | security: rollback ở phiên chưa `list` → batch.names = tên trong server.json; server.json có name sai regex → name = ident; typed = ident → chạy. functional: login với `doctor_ok=false` → có đúng 1 request `/api/doctor` tự động; guards.mjs: route `#confirm` không doctor không mất `S.confirm` |

*Code (20/11), lệch so với bảng:* test "login `doctor_ok=false` → đúng 1 `/api/doctor`" nằm ở guards.mjs (GUARD-10, chạy app.js thật trong node; functional.py không có trình duyệt để chạy JS) thay vì functional.py.

## 20. Bổ sung 20/11 (user): trong lúc plan một con, thấy nó đang làm bước gì

Khung "Đang plan" (§18) chỉ nói `demo-vm · đang plan · 29 s`. Một con plan mất 20–40 s (export VM, rồi 15–16
check đích, mỗi check một lệnh `openstack` 1–2 s); operator không biết đang kẹt ở đâu.

Thực tế CLI đã in **từng dòng ngay khi có**: `plan --json` chạy con `migrate plan <ident>` qua `selector.child`
với `tee=True`, mỗi dòng của con (`VM … status=`, `port …`, `disk …`, `Destination preflight (…)`, `  OK    …`,
`  FAIL  …`, `Preflight passed.`) được in ra stderr của tiến trình `plan --json` ngay lập tức (`stdout_to_stderr`).
Chỉ runner là đọc cả khối khi xong (`run_cli` → `communicate`). **Không cần sửa CLI.**

| Chỗ | Phải làm |
|---|---|
| Runner | `run_cli_stream(args, timeout, on_line)`: Popen như `run_cli`, một thread đọc stderr theo dòng và gọi `on_line(scrub(line))` ngay; stdout gom như cũ; timeout/kill y hệt `run_cli`. `op_plan` dùng nó: mỗi dòng → `{"line": {"ident": …, "idents": […], "text": ≤ 500 ký tự, "level": OK|WARN|FAIL|INFO|null, "t": utc}}`; `level` tách từ tiền tố `  OK    ` v.v. Giới hạn: tối đa **200 dòng/ident**, quá thì gửi một dòng `"text": "… (còn N dòng, xem log plan)"` và thôi; dòng dài cắt 500. Dòng `@@ {…}` (JSON effective) **không** chuyển tiếp |
| Web | SSE thêm sự kiện `line` (đã có khung stream §18) |
| UI, khung "Đang plan" | Dòng của con đang plan có thêm hai phần: (1) **giai đoạn**: chưa có dòng → "đang export VM"; thấy `Destination preflight` → "preflight đích · i check" với i = số dòng có level; thấy `Preflight passed.` → "xong"; (2) **dòng mới nhất** in nghiêng mono (`OK    Ceph pool mig-volumes: 12 GB needed…`). Bung `<details open>` "log plan live" có mọi dòng đã nhận (mono, cuộn, giữ ≤ 200). Con xong (`row`) → giai đoạn thành PLAN-OK/FAIL như §18, details đóng lại, nội dung giữ (operator xem lại được) |
| Volume | `plan volume` một con cho cả project: dòng gắn vào nhóm `idents` của `planning`; giai đoạn chỉ "đang plan project P" + dòng mới nhất |
| An toàn | mọi dòng qua `scrub()` (T9) trước khi rời runner; không dòng nào vào audit; không parse gì khác ngoài tiền tố level |
| Test | functional: fake plan in 6 dòng stderr cách nhau 0,2 s → stream có 6 `line` **trước** `row` của con đó, `level` đúng cho dòng `  OK    `/`  FAIL  `, dòng `@@ {` không xuất hiện; fake in 1000 dòng → đúng 200 `line` + 1 dòng "còn 800 dòng"; security: fake in `OS_PASSWORD=ABC-NOT-A-REAL-ONE` trên stderr → sự kiện `line` chứa `[scrubbed]`; guards.mjs: giai đoạn đổi theo dòng (`export` → `preflight · 3 check` → `xong`), details giữ nội dung sau `row` |

*Code (20/11), lệch so với bảng:* UI giữ tối đa 201 dòng/nhóm (200 dòng + dòng "… (còn N dòng, xem log plan)" của runner), không phải 200.

## 21. Bổ sung 20/11 (user): nguồn chọn được nhiều host và nhiều project

§14.1 làm dropdown một host / một project (hoặc "mọi"). Runner và CLI đã nhận tới 32 host + 32 project trong
một lần `list` (§14.1, `LIST_MAX`); chỉ giao diện còn chọn một. User muốn gom ví dụ 3 compute sắp bảo trì, hay 2
project của cùng một khách, trong một bảng.

| Chỗ | Phải làm |
|---|---|
| Ô Host và ô Project (màn Chọn VM) | thay `<select>` bằng **hộp chọn nhiều**: nút hiện tóm tắt (`mọi host`, `mig-src-cmp`, `3 host`) bấm ra một bảng tick (checkbox, mỗi host một dòng kèm state up/down, mỗi project kèm id); có ô tìm nhanh khi > 8 dòng; nút "Tất cả" / "Bỏ hết"; tick quá 32 → dòng cảnh báo tại chỗ, nút Tải khóa với chữ "Tối đa 32 host". Không tick gì = "mọi" như cũ. Tất cả bằng DOM thường (`textContent`), không thư viện |
| Request `list` | `hosts = [tick…]`, `projects = [tick…]`; cả hai rỗng → `srcHostsForAll` như cũ (trần `HOSTS_MAX` 32). Tiêu đề bảng: `Instance trên 3 host / 2 project (N)`; tooltip liệt kê tên |
| Màn Volume | ô Project thành hộp chọn nhiều cùng kiểu; `list --volumes` nhận `projects` nhiều; plan volume đã gom theo project (§18 `planning.idents`) nên không đổi runner |
| Giữ lựa chọn | tick giữ trong `S.vm.src = {hosts:Set, projects:Set}` suốt phiên (tải lại bảng không mất); "Chạy lại với cùng option" không đụng phần này |
| Bàn phím / khóa | Esc đóng hộp; hộp đang mở mà bấm Tải thì đóng rồi tải; §15 giữ nguyên (Tải khóa khi đang tải) |
| Test | guards.mjs: tick 2 host + 1 project → body `list` đúng hai mảng; tick 33 → nút Tải khóa, không request; bỏ hết → về `srcHostsForAll`; functional: `list` với 2 host + 2 project → fake CLI nhận `--host ×2 --project ×2` |

*Code (20/11), lệch so với bảng:* tiêu đề bảng ghi tên khi chỉ tick đúng một host/project (`Instance trên cmp1 / admin (3)`, như tóm tắt trên nút), từ hai trở lên mới ghi số; "Tất cả" chỉ tick các dòng đang khớp ô tìm.
