# Test report V7 (app Python `migrate`) — cập nhật theo phase (nhánh feature/v7-python-app)

Design: [DESIGN-V7.md](DESIGN-V7.md). Chuẩn đối chiếu: bản bash V6 (`migrate-vm.sh` md5 `5706e763`,
`migrate-batch.sh` `2a3189f1`) — không bị sửa trong suốt V7.

## Phase 1 — skeleton + đường cold (commit `e0a41a1`) — ĐẠT 2026-11-18

Package `migratetool/` 2162 dòng, entry `migrate`, cài `pip install --no-build-isolation .`
vào `/opt/mig-venv`.

### Parity giả lập (Opus, stub openstack/qemu-img/rbd/brick, không gọi cloud)

So bash vs Python trên cùng "world", 6 chiều: stdout (chuẩn hóa giờ), stderr, exit code, mọi
file state, **toàn bộ chuỗi lệnh gọi ra ngoài theo thứ tự**, world cuối.

| Bộ | Kết quả |
|---|---|
| Parity 44 kịch bản (plan/migrate/rollback cold, conf+defaults, keep-sg 4 trạng thái, flavor-fallback 4 trạng thái, 11 điểm die CLI, die giữa chừng: sai pool map / volume error / compare lệch — đều rollback được) | 44/44 giống hệt cả 6 chiều |
| Rollback chéo bản: bash migrate → Python rollback và ngược lại (kể cả volume mồ côi, keep-sg) | 4/4 |
| Unit (conf reader, precedence, not-ported, help/version) | 23/23 |

### Lab thật (mig-host)

| # | Case | Lệnh | Kết quả |
|---|---|---|---|
| 1 | State + output parity trên cloud thật | `migrate-vm.sh plan demo-vm --verify none` rồi `migrate plan demo-vm --verify none`, diff state dir + diff output (bỏ timestamp) | PASS — `STATE-IDENTICAL` + `OUTPUT-IDENTICAL` (từng byte) |
| 2 | Gate P1: e2e cold NGUYÊN BẢN, chỉ sed đổi lệnh (`migrate-vm.sh migrate/rollback ` → `migrate migrate/rollback ` — diff đúng 2 dòng, sinh máy bằng sed, file `/root/mig-lab/tests/e2e-migrate-py.sh`) | `bash /root/mig-lab/tests/e2e-migrate-py.sh` | **PASS=50 FAIL=0 WARN=0** — migrate cold + rollback thật trên demo-vm bằng app Python |

### 16 điểm lệch bash Opus tự khai — Fable duyệt chấp nhận cả 16 (2026-11-18)

Nhóm chính: message lỗi rõ hơn (quy tắc 18/11: câu chữ tự do, logic bất biến); 2 chỗ SỬA bug
tiềm ẩn của bash (rollback ≥2 clone sót cùng tên — bash truyền chuỗi nhiều dòng làm 1 đối số;
property chứa `\`/tab bị `@tsv` của bash làm hỏng); conf reader Python chặt hơn (chỉ KEY=VALUE,
gặp bash phức tạp thì die kèm số dòng — **phải soát conf production trước khi chuyển**); openrc
source 1 lần/lần chạy (rc có prompt sẽ khác — production không có); watcher %/JSON format/env
tiến trình con sẽ nghiệm thu tiếp ở các phase sau trên thiết bị thật.

### Chưa port (die rõ `not ported yet`, không để state chặn)

`--mode live` (P2), ephemeral migrate (P3), `migrate volume`, `migrate batch`, rollback state
volume (P4).

## Phase 2 — live mode (commit `1fb2e88`) — ĐẠT 2026-11-18

Bundle `blocksync.py` vào package (unit test khóa md5 giống từng byte bản gốc), chạy qua
`SYS_PY -I` (python hệ thống, cách ly sys.path khỏi package). Parity giả lập: 65/65 trước khi
thêm `-I` + chạy lại 21/21 sau, unit 25/25 — phủ 17 kịch bản live (hội tụ/không hội tụ/max-passes/
3 chế độ verify/blocksync chết giữa pre-sync lẫn cutover/churn 0/rollback chéo bash↔py).
Cải thiện có chủ đích: chặn `--max-passes`/`--delta-limit` không phải số (bash cũ LẶP VÔ HẠN với
input rác); blocksync in rác thay JSON → die kèm nội dung (bash ra lỗi jq khó hiểu).

### Lab thật

| # | Case | Lệnh | Kết quả |
|---|---|---|---|
| 3 | Preflight live qua cầu bundled blocksync + system python | `migrate plan demo-vm --mode live --verify none` | PASS — `OK blocksync present` + `OK librbd python bindings`, Preflight passed |
| 4 | blocksync-unit NGUYÊN BẢN (file gốc, python hệ thống) | `bash /root/mig-lab/tests/blocksync-unit.sh` | PASS=8 FAIL=0 |
| 5 | Gate P2: live suite NGUYÊN BẢN, sed đổi lệnh (2 dòng, `/root/mig-lab/tests/live-migrate-py.sh`) | `bash /root/mig-lab/tests/live-migrate-py.sh` | **PASS=26 FAIL=0** — pre-sync + cutover + rollback thật bằng app Python |

Ghi chú harness: lần chạy đầu 2 bộ này ĐỎ OAN do wrapper của Fable — (a) activate mig-venv làm
`#!/usr/bin/env python3` của blocksync trỏ vào python venv không có librbd (8 FAIL); (b) lệnh
smoke `migrate plan --mode live` để sót state dir làm live suite fail check "no leftover state"
(bash plan cũng ghi state y vậy). Dọn + chạy lại không venv → xanh như trên; code không đổi.

## Phase 2b — CLI thống nhất (user chốt 18/11 đợt 2, commit design `18b4f75`)

Bỏ subcommand `batch`: selector hóa (`--vm/--host/--project/--vm-file`, positional = shorthand);
`migrate plan` thay `--plan-only`; `migrate migrate` LUÔN hỏi xác nhận trừ `--yes` (đổi hành vi
có chủ đích, an toàn hơn bash); volume theo đợt gộp vào `migrate volume`. Thực thi ở P4.

## Phase 3 — ephemeral + newip/extras (commit `f584bd5`) — ĐẠT 2026-11-18

Port đường ephemeral: snapshot → Glance REST (urllib, đúng trình tự curl của bash: POST record,
PUT stream, chờ active, so sha512, JSON-patch allowlist property) → server image-backed; dọn
`mig-snap-*`. Parity giả lập 86/86 + unit 25/25, trong đó Glance GIẢ là HTTP server thật tính
sha512 và ghi từng request — curl (bash) với urllib (Python) bị so TỪNG LỜI GỌI REST. 12 kịch bản
ephemeral + 3 rollback chéo + 6 kịch bản hình dạng suite. Lệch có chủ đích (duyệt): message lỗi
REST rõ hơn (POST fail: bash exit 22 của curl im lặng, Python ERROR + exit 1).

### Lab thật

| # | Case | Lệnh | Kết quả |
|---|---|---|---|
| 6 | newip-voltype NGUYÊN BẢN sed đổi lệnh (`newip-voltype-py.sh`) | `bash /root/mig-lab/tests/newip-voltype-py.sh` | **PASS=26 FAIL=0** |
| 7 | extras NGUYÊN BẢN sed đổi lệnh (`extras-py.sh`) — multi-NIC, flavor/SG override, properties, **ephemeral qua Glance thật** | `bash /root/mig-lab/tests/extras-py.sh` | **PASS=30 FAIL=0** |

**Cộng dồn P1–P3: app Python qua đủ 140/140 check của 5 suite cũ** (e2e 50 + blocksync 8 +
live 26 + newip 26 + extras 30).

## Phase 4 — volume + batch + maps: (chưa)

## Phase 5 — thay thế + docs: (chưa)
