# Test report V5 — 2026-11-18 (nhánh feature/v5-flavor-map)

Build test: `migrate-vm.sh` md5 `5706e763` + `migrate-batch.sh` md5 `5b24bdba` = commit `0c49e3c`
(design [DESIGN-V5.md](DESIGN-V5.md)). Nhánh fork từ `feature/v4-sg-inuse-vol` (V4 đã xanh,
xem [TEST-REPORT-20261118.md](TEST-REPORT-20261118.md)).
Môi trường: lab mục 9 system-info, chạy trên mig-host; không có `/opt/mig/migrate.conf` trừ khi
case tự tạo. Log gốc: `/root/v5-phase.txt`, `/root/gate7-*.txt`.
Harness GIẢ LẬP của Opus (không gọi cloud): V5 27/27 + chạy lại A/B 27, C 17, 2d 9, 2e 28 —
không tính vào số liệu lab dưới đây. Diff không-flag so với build V4 đã chứng minh giống từng byte
trên 3 kịch bản stub.

## Feature — flavor fallback (12 case DESIGN-V5 §4): 16 PASS / 0 FAIL thật

Setup: flavor nguồn `v5-src` (1 vCPU/1024M, KHÔNG có ở đích), flavor đích `v5-dst` (1/1024) +
`v5-tiny` (1/512), VM test `f5-vm` BFV 10G flavor v5-src; map `/opt/mig/flavor-map.conf`.
(Lệnh đầy đủ: `/root/v5-phase.sh` trên mig-host.)

| Case design | Lệnh | Kết quả |
|---|---|---|
| 11 | `migrate-vm.sh plan f5-vm --flavor-fallback --verify none` (FLAVOR_MAP_FILE rỗng) | PASS — die `--flavor-fallback needs FLAVOR_MAP_FILE` |
| 5 | map chứa `v5-src = v5-dst` | PASS — die `expected 'source-flavor destination-flavor'` |
| 6 | map khai nhưng KHÔNG flag → `plan f5-vm --verify none` | PASS — `FAIL flavor v5-src` như V4, không đụng map (hành vi cũ giữ nguyên) |
| 3 | flag + map không có dòng v5-src | PASS — FAIL kèm gợi ý `add a line 'v5-src <dest-flavor>' to /opt/mig/flavor-map.conf` |
| 4 | map `v5-src ghost-flavor` | PASS — FAIL `...not found at the destination — the tool never creates flavors` |
| 8 | map `v5-src v5-tiny` (RAM 1024→512) | PASS — `INFO flavor v5-src -> v5-tiny from FLAVOR_MAP_FILE` + `WARN mapped flavor v5-tiny is smaller than v5-src`, plan rc=0 |
| 2 | tạo tạm flavor `v5-src` Ở ĐÍCH → plan flag+map | PASS — `OK flavor v5-src`, map KHÔNG được tra (đúng nghĩa fallback) |
| 7 | `plan f5-vm --flavor mig.small --flavor-fallback` | PASS — `INFO --flavor-fallback has no effect: --flavor mig.small is given` |
| 10 | `plan f5-vm --volumes-only --flavor-fallback` (map rỗng) | PASS — INFO ignored, không cần map |
| 12 | conf `FLAVOR_MAP_FILE=…` + `MIG_DEFAULT_OPTS="--flavor-fallback"` → `plan f5-vm --verify none` (không flag tay) | PASS — INFO map xuất hiện, fallback chạy từ defaults |
| 1 | map `v5-src v5-dst` → `migrate f5-vm --flavor-fallback --verify none` → rollback | PASS×3 — migrate rc=0; **VM đích flavor `v5-dst`**; rollback sạch, f5-vm ACTIVE lại |
| 9 | `migrate-batch.sh --vm f5-vm,demo-vm --plan-only -- --flavor-fallback --verify none` | PASS (retake) — rc=0, bảng plan đủ 2 dòng `PLAN-OK`: f5-vm qua map, demo-vm qua cùng-tên. Lần chạy đầu assert harness đếm `grep -c PLAN-OK` trúng cả dòng log lẫn dòng bảng (2 VM → 4 match) nên báo FAIL OAN — output gốc trong `/root/v5-phase.txt` đã cho thấy cả 2 PLAN-OK; chạy lại với assert đếm riêng dòng bảng: PASS |

Trạng thái lab sau phase: demo-vm + mig-peer ACTIVE ở nguồn, đích rỗng, flavor/map/conf test đã xóa.

## Regression gate vòng 7 — 5 bộ cũ nguyên văn trên build V5 (md5 5706e763)

| # | Case | Lệnh | Kết quả |
|---|---|---|---|
| 7a | blocksync unit | `bash /root/mig-lab/tests/blocksync-unit.sh` | PASS=8 FAIL=0 |
| 7b | e2e cold NGUYÊN BẢN | `bash /root/mig-lab/tests/e2e-migrate.sh` | PASS=50 FAIL=0 WARN=0 |
| 7c | live | `bash /root/mig-lab/tests/live-migrate.sh` | PASS=26 FAIL=0 |
| 7d | newip-voltype | `bash /root/mig-lab/tests/newip-voltype.sh` | PASS=26 FAIL=0 |
| 7e | extras | `bash /root/mig-lab/tests/extras.sh` | PASS=30 FAIL=0 |

**140/0 — ALL GATES GREEN (V5 build).**

## Tổng

- Feature: **16/16 PASS** (12 case design; 1 FAIL oan của harness đã retake sạch).
- Gate vòng 7: **140/0** (build V5 0c49e3c, không sửa assert nào).
- Chưa test trên lab (chấp nhận): map nhiều dòng với nhiều VM migrate THẬT cùng đợt (lab chỉ
  plan-only cho cặp 2 flavor; đường chọn flavor per-VM đã phủ bằng case 1+9); flavor nguồn đã bị
  XÓA khỏi Nova nguồn (specs đọc từ server.json — phủ bằng harness giả lập của Opus).
