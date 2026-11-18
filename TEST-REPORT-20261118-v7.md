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

## Phase 2 — live mode: (đang làm)

## Phase 3 — ip/voltype/ephemeral/extras: (chưa)

## Phase 4 — volume + batch + maps: (chưa)

## Phase 5 — thay thế + docs: (chưa)
