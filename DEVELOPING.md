# Phát triển migrate

[DESIGN.md](DESIGN.md) là chuẩn hành vi hiện hành. [REVIEW-20261121.md](REVIEW-20261121.md) giữ ID review và phạm vi việc còn thiếu. Khi code và đề xuất review khác nhau, tài liệu mô tả code, đồng thời ghi gap trong [RELEASE-1.0.0.md](RELEASE-1.0.0.md).

## 1. Bản đồ module

### Engine: `migratetool/`

| Module | Trách nhiệm |
|---|---|
| `__init__.py` | `__version__` của engine |
| `__main__.py` | Entry `python -m migratetool` |
| `cli.py` | Argparse/help, dispatch, umask, prompt và exit ngoài cùng |
| `single.py` | Child xử lý một VM/volume theo scope cha giao |
| `selector.py` | Select/intersect, map project, plan children, batch log, bảng và exit batch |
| `options.py` | Parse default/CLI, AZ/type/pool precedence, effective/provenance |
| `config.py` | Parse conf dữ liệu, env/default, paths và giới hạn control-plane |
| `context.py` | Run context, Cloud hai phía, wait helpers, trạng thái copy |
| `clouds.py` | Source openrc sạch, project scope, argv OpenStack, readonly guard |
| `proc.py` | Chạy/stream subprocess và deadlines |
| `util.py` | Die/Exit, log, text/JSON compatibility helpers |
| `contract.py` | Chuỗi/regex/exit/owner-mark dùng chung trong engine |
| `vmdef.py` | Export server/volume/port, nhận diện boot, tên volume đích |
| `vmcmd.py` | VM plan/migrate, A6, recheck nguồn, marker và impact |
| `preflight.py` | Flavor/host/network/storage/quota/group/ephemeral checks |
| `azname.py` | Chọn prefix AZ cho group copy |
| `secgroups.py` | Graph SG, map remote group, compare rules, tạo/dọn copy |
| `server.py` | Detach/reserve nguồn, port/server group/server đích |
| `copyvol.py` | Clone/attach/device guard, cold copy/verify, RBD và image properties |
| `brick.py` | Helper os-brick/Cinder attachment, auth, RO và WWN record |
| `livesync.py` | Điều phối pass, kiểm live, đọc thống kê blocksync |
| `blocksync.py` | Quét chunk, read_exact, hash/delta, discard/readback librbd |
| `ephemeral.py` | Glance REST/snapshot/staging/upload/hash, root image→volume |
| `volcmd.py` | Volume đơn, lookup SDK description, preflight/copy/rollback volume |
| `rollback.py` | Discovery có owner mark, confirmed deletes, trả nguồn, summary/A6 |
| `state.py` | StateDir, file read/write/fsync, flock thư mục, retire |
| `locking.py` | Khóa host MIG_LOCK, kiểm FD/inode kế thừa và truyền khóa cho child |
| `record.py` | Command/effective/conf whitelist trong options.json và rollback.json |
| `jsonout.py` | Chặn leak JSON, safe file reads/writes, chuyển text sang stderr |
| `progress.py` | Event `@@ `, throttle và kết quả tiến độ |
| `listing.py` | List readonly, validate selector/file, VM/volume row |
| `inventory.py` | Inventory hai cloud, option dropdowns, counts |
| `stateview.py` | View state an toàn, phase và source_power từ export |
| `status.py` | State/log roots và /proc, không query cloud |
| `doctor.py` | Host/auth/dependency/storage checks và write-check |
| `prune.py` | Dry-run/xóa retired state và batch log theo tuổi |

### Web UI: `migui/`

| Module/asset | Trách nhiệm |
|---|---|
| `__init__.py` | `__version__` UI |
| `auth.py` | Users file, username/password rules, scrypt |
| `passwd.py` | CLI root quản lý account, atomic write và audit |
| `common.py` | Validation, typed confirmation, scrub, audit, JSON socket helpers |
| `runner.py` | Probe installed CLI, socket peer, plan, batch/job/locks/notify/log/status |
| `web.py` | Loopback HTTP, sessions/CSRF/CSP, concurrency/SSE, Doctor gate và audit gate |
| `static/index.html` | Screens, forms/dialogs, SVG icons và accessibility structure |
| `static/app.js` | Navigation, selection/options, plan stream, confirmation, running/history/theme |
| `static/app.css` | Layout, tokens sáng/tối, responsive và states |
| `deploy/install.sh` | Copy UI, wrappers, account hệ thống, audit/units, uninstall |
| `deploy/*.service` | Phân quyền web/runner, sandbox và restart semantics |

Engine dùng venv; blocksync dùng SYS_PY. Child `-I` bỏ PYTHONPATH/user site và current directory khỏi import path. Phát triển child integration cần cài đúng package vào venv thử, không chỉ chạy CLI từ tree. Harness thay `selector.CHILD`, `copyvol.BRICK`, roots và external command adapters để không dùng cloud thật.

## 2. Hợp đồng phải giữ

`contract.py` định nghĩa tiền tố `  OK    ` / `  WARN  ` / `  FAIL  ` / `  INFO  `, CHECK_RE, `Preflight passed.`, `ephemeral VM has no volume to migrate`, pattern keypair/logs/state, exit 0/1/2/3/4/5/130 và owner marks. UI không import engine: runner giữ regex/constants tương đương và test kiểm đồng bộ.

Thay text bị grep phải sửa mọi consumer và test cùng commit. Không dùng PASS/FAIL/WARN ở cột 0 cho log engine vì harness/lab có bộ đếm riêng. Giữ JSON keys tiếng Anh. Tên cloud object là value, không làm key vì jsonout chặn key giống credential.

Structured stdout phải đi qua `jsonout.emit_json`; conf chỉ qua `record.conf_view`. Progress dùng drop+WARN khi phát hiện leak. JSON record dùng helper safe/private; không ghi trực tiếp bỏ qua check. Không dùng `shell=True`, `os.system`, eval hoặc exec trong product. Bash source openrc ở `clouds.rc_env` là whitelist có chủ đích; không mở rộng whitelist để tiện chạy lệnh.

Không sửa archive để làm parity xanh. **`migratetool/blocksync.py` không còn byte-identical với `archive/blocksync.py`**: A1 thêm read_exact và xử lý short read. `cmp` hai file không phải tiêu chí pass cho release này.

## 3. Test harness

Chạy Linux có Python≥3.10, Bash, jq và môi trường mô phỏng; không chạy suite cần cloud trên production. Tests Python dùng fcntl/Unix sockets nên Windows native không thay được Linux gate.

```bash
bash tests/unit/run.sh
bash tests/unit/run.sh --parity
bash tests/unit/run.sh --parity p6
bash tests/ui/run.sh
node tests/ui/guards.mjs .
```

| Harness | Phạm vi / output |
|---|---|
| `tests/unit/run.sh` | checks, unit, p5unit, p6unit, v72unit, v73unit, v1unit, ui; thêm parity khi yêu cầu; `ALL pass=N fail=M` |
| Unit wrapper E2 | Suite thiếu TOTAL là lỗi; suite rc nonzero dù TOTAL fail 0 cũng bị tính lỗi |
| `tests/unit/v1unit.py` | Review IDs; real blocksync với fake device/librbd, ownership/delete/A6/locks/deadline/prune/JSON |
| `tests/unit/inproc.py` | App trong process với world giả; thay proc và child để nhanh |
| `tests/unit/scenarios.py`, `parity.py` | Chạy Bash reference và app qua stubs/Glance giả, so sáu chiều |
| `tests/ui/run.sh` | security, functional, passwd, install_audit, v1ui và SEC-7 grep; stack thật với CLI giả |
| `tests/ui/v1ui.py` | D3 gate, parallel plan, conf digest, notify, audit 503, scrub value, SSE cap, revocation/lockout |
| `tests/ui/guards.mjs` | Fake DOM/fetch chạy app.js, double submit, fingerprint, WARN, stream, controls/CSP/contrast |
| `tests/ui/browser.mjs` | CDP smoke click-through thật trên stack dùng fake migrate; có bắt đầu migration giả |

`MIG_UNIT_WORK` mặc định `/tmp/mig-unit-work`, lưu log từng suite. `MIGUI_TEST_WORK` mặc định `/tmp`; `MIGUI_TEST_KEEP=1` giữ stack artifacts để điều tra. `PYTHON` chọn interpreter unit/UI; parity stubs dùng `/usr/bin/python3`.

Lưu ý test wrapper UI: nó cộng TOTAL của từng script nhưng chưa tự xét rc từng script như wrapper unit. Không lấy `ALL pass` thay thế việc xem log crash/bất thường. E1 chưa test crash ở mọi marker.

### Parity

So stdout, stderr, rc, state files, chuỗi calls và world cuối. Chuẩn hóa timestamp/path/ID sinh ngẫu nhiên theo quy tắc đã khai; không bỏ cả trường chỉ để né failure.

| Quy tắc | Phạm vi |
|---|---|
| Legacy option transform | Kịch bản trước SG-copy mặc định: thêm no-copy/no-server-group phù hợp; bỏ cờ đã retire theo logic harness |
| `v72_rule` | Quota/Ceph/FIP/breakdown/API calls và thứ tự precreate volume của kịch bản reference |
| `v73_rule` | Bỏ JSON record mới khỏi so state khi reference không ghi chúng |
| `v1_rule` | Bỏ các calls kiểm mới theo ID; bỏ owner mark khỏi world/calls khi so reference; chuẩn hóa suffix batch, text marker, C11 warning và exit 3/4/5→1 |

Mỗi normalization `v1_rule` có comment `Intended: v1 <ID>` như A2/A4/A5/A6/A11/A12/A15/C8/C11/C12. `compare(..., v1=True)` tắt normalization v1 cho case cần so hành vi mới. Kịch bản reference ephemeral thêm `--ephemeral-as-image` cho app; rollback app thêm `--yes`. Parity chứng minh phần tương đương đã định nghĩa, không chứng minh binary cũ xử lý an toàn mọi state mới.

### Browser smoke

Tạo stack thử disposable dùng `tests/ui/fake_migrate.py` qua harness. Mở Chrome/Edge với profile riêng và CDP chỉ loopback. Không chỉ URL tool vào UI production: smoke **bấm chạy**.

```bash
node tests/ui/browser.mjs --url http://127.0.0.1:8790/ --user test --password '<disposable-test-password>' --cdp 9222 --out /tmp/migui-shots
```

Chỉ Node 24 (script từ chối phiên bản khác); Fetch interception qua CDP thay static bằng bản trong worktree, URL thêm `?test=1` để bật `window.miguiTest`. Tham số password của script chỉ dành account disposable; không dùng mật khẩu thật qua argv. Report phải ghi browser, Node, viewport, commit và kết quả. Guards không thay browser smoke, browser fake không thay canary cloud.

## 4. Thêm một hành vi

1. Định nghĩa trigger, kết quả, failure/rollback và review ID trong DESIGN hoặc issue đã duyệt.
2. Sửa module có trách nhiệm; mọi API qua Cloud/proc để test được, không để project/env rò giữa VM.
3. Nếu thêm option: cập nhật help, Options/FLAG_OPTS/VALUE_OPTS, SERVER_ONLY, VOLUME_PASS, effective, record và validation UI nếu có.
4. Nếu thêm state marker/object: định nghĩa crash trước/sau write, ownership proof và cách rollback xác nhận xóa.
5. Viết unit test hành vi có ID; test nhánh lỗi gây sai dữ liệu hoặc quyền trước. Cập nhật parity normalization chỉ cho lệch có chủ đích.
6. Nếu đổi contract: kiểm CLI JSON và runner/common/web, guards/browser cùng version. Không chỉ sửa screenshot.
7. Chạy suite phù hợp, full parity trước review; bổ sung lab gate nếu đụng copy/attach/cutover/rollback.
8. Cập nhật CHANGELOG theo ID, GUIDE/DESIGN và evidence release. Ghi giới hạn chưa kiểm, không ghi PASS suy đoán.

## 5. Release

Ba file version phải khớp: `migratetool/__init__.py`, `migui/__init__.py`, `pyproject.toml`. `release.sh` chỉ tự so engine và pyproject, nên reviewer kiểm UI riêng.

```bash
git status --short
bash tests/unit/run.sh --parity
node tests/ui/guards.mjs .
# Chạy browser smoke trên stack giả và lab gate theo kế hoạch đã duyệt.
./release.sh
```

Release script dùng `pip wheel --no-deps --no-build-isolation`, fallback build isolation nếu cần; output mặc định `dist/`, hoặc `./release.sh <OUTDIR>`. Nó chỉ kiểm dirty migratetool/pyproject, không thay review clean tree toàn bộ. Wheel không có UI, không có openrc/keyring và không gộp requirements-host.

Ghi commit, SHA256 wheel, hash pin, suites, môi trường, test report và gap trong [RELEASE-1.0.0.md](RELEASE-1.0.0.md). Giữ artifact venv/version để rehearsal downgrade. Chỉ tag/publish sau gate và quyết định của chủ release; yêu cầu viết docs không phải yêu cầu publish.

## 6. Chống leak

Không commit openrc/keyring, raw token, password, app credential, private key hoặc ảnh/log mang credentials. Trong docs chỉ dùng TEST-NET `192.0.2.x`/`198.51.100.x` và placeholder rõ ràng. Scan **mọi commit sắp đẩy**, không chỉ HEAD; xóa secret ở commit sau không xóa leak khỏi lịch sử.

File mẫu bí mật thật nằm ngoài repo: `../leak-patterns.txt`. Không in nội dung nó hoặc dòng match lên report công khai. Ví dụ quét ERE, lọc comment/dòng trống, chỉ báo commit có match:

```bash
patterns=$(mktemp)
trap 'rm -f "$patterns"' EXIT
sed '/^[[:space:]]*#/d; /^[[:space:]]*$/d' ../leak-patterns.txt > "$patterns"
test -s "$patterns" || { echo 'missing leak patterns'; exit 1; }
for commit in $(git rev-list origin/main..HEAD); do
  git grep -q -a -E -f "$patterns" "$commit"
  rc=$?
  case "$rc" in
    0) echo "LEAK MATCH: $commit"; exit 1 ;;
    1) ;;
    *) echo "scan failed: $commit"; exit 1 ;;
  esac
done
```

Thay range bằng đúng ref định publish. File pattern thiếu là **chưa scan**, không phải sạch. Bổ sung scan chung private-key header, Ceph key và assignment secret có giá trị thật; kiểm cả tài liệu/ảnh. Hook local không versioned nên clone mới phải thiết lập lại. Không đưa giá trị match vào commit message.
