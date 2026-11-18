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

## Phase 4 — volume + batch + maps + selector CLI (commit `8a48c45`) — ĐẠT 2026-11-18

Port nốt: `migrate volume` (available/--in-use/selector --project/--vm), selector hóa migrate/plan
(`--vm/--host/--project/--vm-file`, positional = shorthand), **luôn hỏi `Type yes` trừ `--yes`**,
3 map + MIG_DEFAULT_OPTS qua selector, rollback volume state. Batch chạy mỗi VM bằng tiến trình
con `python -m migratetool.single` (giữ cách ly env/project + log riêng `/var/log/mig-batch/<ts>/`
như bash). Parity giả lập 141/141 (so cả file log từng con với batch bash) + unit 65/0.

### 10 điểm lệch Opus tự khai — Fable duyệt chấp nhận cả 10 (2026-11-18)

Đáng nói nhất: **⚠ `migrate migrate <vm>` (1 VM) giờ auto-scope SRC/DST_PROJECT theo project
của VM** — bash cũ chạy admin mà quên set env thì VM đích rơi vào project admin; giờ VM đích vào
project cùng tên (map được qua PROJECT_MAP_FILE). Appcred dùng được qua selector khi VM thuộc
đúng project của token (batch bash từ chối thẳng). stderr tiến trình con gộp vào stdout cha.
Preflight chạy 2 lần khi plan→migrate (mỗi lệnh một lần — hành vi batch cũ giữ nguyên).
Gap tự nhận: `migrate plan` chưa nhận selector volume (mất dry-run `--volumes available
--plan-only` của batch cũ) — bù ở P5.

### Lab thật — regression gate: cả 4 suite NGUYÊN BẢN sed đổi lệnh + thêm `--yes`

Sed bổ sung cho quyết định "luôn hỏi xác nhận": `s/\(migrate migrate [^|>]*\) 2>&1/\1 --yes 2>\&1/`
(sự cố lần đầu: `&` trong replacement không escape làm nát dòng — đã sửa `2>\&1`, verify bằng
grep lines-without-yes = rỗng trước khi chạy).

| # | Case | Lệnh | Kết quả |
|---|---|---|---|
| 8 | e2e cold trên build P4 | `bash /root/mig-lab/tests/e2e-migrate-py.sh` | **PASS=50 FAIL=0 WARN=0** |
| 9 | live trên build P4 | `bash /root/mig-lab/tests/live-migrate-py.sh` | **PASS=26 FAIL=0** |
| 10 | newip-voltype trên build P4 | `bash /root/mig-lab/tests/newip-voltype-py.sh` | **PASS=26 FAIL=0** |
| 11 | extras trên build P4 | `bash /root/mig-lab/tests/extras-py.sh` | **PASS=30 FAIL=0** |
| 12 | Feature V3–V6 trên CLI mới (`p4feat.sh`, 24 check) | `bash /root/mig-lab/tests/p4feat.sh` | **PASS=24 FAIL=0** (lượt 2; lượt 1: 10 FAIL đều oan, xem dưới) |

Các case trong `p4feat.sh` (fixture: proj-b + batch-vm BFV thuộc proj-b, data-c proj-b, sg-vm1/2
flavor v5-src trong group soft-anti-affinity web-anti, flavor v5-dst chỉ có ở đích):

| Case | Lệnh | Kiểm chứng |
|---|---|---|
| A1 | `migrate plan --host mig-src-cmp --verify none` | 3 PLAN-OK (đa project, có proj-b) + 2 PLAN-FAIL đúng lý do `flavor v5-src` thiếu ở đích, rc=1 |
| E1 | `migrate migrate demo-vm --verify none </dev/null` | die đòi `--yes` khi stdin không phải tty; không migrate gì |
| A2 | `migrate migrate batch-vm --verify none --yes` (KHÔNG set env) | VM đích vào đúng proj-b (auto-scope ⚠); rollback bằng tên, nguồn ACTIVE, đích sạch |
| A3 | `migrate plan --vm-file f` (có dòng rác) | rác → SKIP(not-found), demo-vm PLAN-OK |
| A4 | `migrate volume --project proj-b --available --yes` | OK 1, đích giữ nguyên tên data-c, nguồn còn nguyên; rollback bằng vol-ID |
| A5 | `migrate volume --vm demo-vm --verify none --yes` | copy cả vda+vdb (demo-vm-vda/vdb), không tạo server; rollback bằng `demo-vm` (đúng chữ đã gõ) |
| A6 | `migrate migrate --vm demo-vm --volume-type bogus-type --yes` | PLAN-FAIL chặn, không đụng đích, rc=1 |
| B1 | `migrate volume <vdb-id> --in-use` | copy crash-consistent, VM nguồn ACTIVE suốt; rollback sạch |
| C1 | `migrate migrate --vm sg-vm1,sg-vm2 --keep-server-group --flavor-fallback --verify none --yes` + FLAVOR_MAP_FILE | plan báo will-create không tạo; 2 VM một lệnh; group az-web-anti đúng policy + 2 member; flavor map ra v5-dst; rollback giữ group, nguồn ACTIVE |
| B4 | MIG_DEFAULT_OPTS trong conf | plan in `MIG_DEFAULT_OPTS applied` + keep-sg có hiệu lực; env rỗng tắt được |
| D1 | `migrate batch` / `migrate migrate-volume` | die kèm hướng dẫn lệnh mới, không chạy gì |

**Lượt 1: 14/24, 10 FAIL — chẩn đoán xong cả 10 đều OAN, không có bug code:**
(a) A1: harness quên sg-VM dùng flavor v5-src chưa có ở đích → PLAN-FAIL là ĐÚNG (bằng chứng
log con `FAIL flavor v5-src`); (b) A5b+C1d: harness rollback bằng ID theo thói quen batch bash,
nhưng CLI mới ghi state theo ĐÚNG CHỮ đã gõ (`--vm sg-vm1` → state `sg-vm1`) — điểm lệch đã
duyệt, tool tự in hint rollback đúng identifier; (c) A4: project admin nguồn có sẵn 4 volume
available vô danh (rác test cũ) nên quét available bắt cả 5 — tool đúng, chuyển fixture sang
proj-b; (d) A6/B1/B4a/cleanup: dây chuyền từ (b) — state demo-vm còn sống → SKIP(state-exists),
copy vdb còn ở đích → preflight dst-exists chặn (đều là guard đúng). Điểm cộng quan sát được:
rollback 2 VM mà NGUỒN đã bị xóa tay vẫn dọn sạch tài nguyên đích rồi mới chết ở bước re-attach
port nguồn — idempotent đúng thiết kế. Dọn lab bằng chính `migrate rollback` của tool.

## Phase 5 — thay thế + docs: (chưa)

## Phase 5 — thay thế + docs: (chưa)
