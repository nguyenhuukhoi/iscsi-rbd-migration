# Test report V4 — 2026-11-18 (nhánh feature/v4-sg-inuse-vol)

Build test cuối: `migrate-vm.sh` md5 `69e870aa` + `migrate-batch.sh` md5 `49bd30d4` = commit `51aa611`
(design `DESIGN-V4.md`, các commit code `b46e51b` A+B → `e4d76d0` C+2c → `af151e2` 2d → `51aa611` 2e).
Môi trường: lab mục 9 system-info; chạy trên mig-host; mặc định
`SRC_RC=/opt/mig/src/admin-openrc.sh DST_RC=/opt/mig/dst/admin-openrc.sh`, không có
`/opt/mig/migrate.conf` trừ khi case tự tạo (và tự xóa sau đó).
Log gốc trên mig-host: `/root/gate5-*.txt`, `/root/gate6-*.txt`, `/root/v4-phase*.txt`, `/root/v4-inuse.log`.

Ngoài các bộ dưới đây, mỗi đợt code Opus còn chạy harness GIẢ LẬP (không gọi cloud) trên mig-host:
A/B 27/27, C+batch-filter 17/17, §2d 9/9, §2e 28/28 — không tính vào số liệu lab bên dưới.

## Vòng 5 — regression gate trên build A+B+C (migrate-vm md5 `8c18c72a`, commit e4d76d0)

| # | Case | Lệnh | Kết quả |
|---|---|---|---|
| 1 | blocksync unit | `bash /root/mig-lab/tests/blocksync-unit.sh` | PASS=8 FAIL=0 |
| 2 | e2e cold NGUYÊN BẢN | `bash /root/mig-lab/tests/e2e-migrate.sh` | PASS=50 FAIL=0 WARN=0 |
| 3 | live | `bash /root/mig-lab/tests/live-migrate.sh` | PASS=26 FAIL=0 |
| 4 | newip-voltype | `bash /root/mig-lab/tests/newip-voltype.sh` | PASS=26 FAIL=0 |
| 5 | extras (multi-NIC, flavor/SG, properties, ephemeral) | `bash /root/mig-lab/tests/extras.sh` | PASS=30 FAIL=0 |

**140/0.** Không sửa dòng nào trong tests/ (V4 opt-in toàn bộ nên không có assert nào thuộc thay đổi).

## Feature phase 1 — CLI guard, MIG_DEFAULT_OPTS, MIG_AZ/AZ_TYPE_MAP: 17/17

(Lệnh đầy đủ: `/root/v4-phase1.sh`. `$VDB` = demo-data `286c3419…` đang in-use trên demo-vm.)

| Case design | Lệnh | Kết quả |
|---|---|---|
| 2 (A) | `migrate-vm.sh migrate-volume $VDB` (không flag) | PASS — die kèm hint mới `or copy it live with --in-use (crash-consistent)` |
| 11 (B) | `migrate-vm.sh plan demo-vm --keep-server-group --server-group x` | PASS — die `choose one: --server-group <name> or --keep-server-group` |
| guard A | `migrate-vm.sh plan demo-vm --in-use` | PASS — die `--in-use belongs to migrate-volume` |
| 10 (B) | `migrate-vm.sh plan demo-vm --keep-server-group --verify none` | PASS — `INFO source VM is in no server group`, plan rc=0 |
| 23 (E) | `MIG_DEFAULT_OPTS="--bogus x" migrate-vm.sh plan demo-vm` | PASS — die `MIG_DEFAULT_OPTS: unknown option '--bogus'` |
| 20 (E) | conf `MIG_DEFAULT_OPTS="--keep-server-group --verify none"` → `plan demo-vm`; rồi `plan demo-vm --verify full`; rồi `MIG_DEFAULT_OPTS="" plan` | PASS×3 — INFO applied đủ 2 option; `--verify none (command line wins)`; env rỗng tắt hẳn |
| 21 (E) | conf default keep + `plan demo-vm --server-group tmp-sg --verify none` (tmp-sg có thật ở đích) | PASS — rc=0, không die choose-one, INFO bỏ keep |
| 22 (E) | conf default keep+verify → `migrate-volume data-c` | PASS — `INFO …do not apply to migrate-volume, ignored: --keep-server-group`, copy rc=0, rollback sạch |
| 15 (C) | conf `MIG_AZ=bogus-az` → `plan demo-vm --verify none` rồi `plan demo-vm --az nova --verify none` | PASS×2 — conf: `INFO availability zone bogus-az from MIG_AZ` + `FAIL compute AZ bogus-az`; CLI thắng: rc=0 không còn bogus |
| 16 (C) | conf `MIG_AZ=nova` + `AZ_TYPE_MAP="nova:mig-typeb"` + `RBD_POOL_MAP="mig-typeb:mig-volumes"` → `migrate-volume data-c` | PASS×3 — `INFO volume type mig-typeb from AZ_TYPE_MAP (nova)`, copy rc=0, volume đích `type=mig-typeb` (chuỗi AZ→type→pool trọn vẹn); rollback sạch |
| 17 (C) | conf `AZ_TYPE_MAP="otheraz:mig-typeb"` + `MIG_AZ=nova` → `plan demo-vm --verify none` | PASS — không INFO map, `type=default` |

## Feature phase 2 — migrate-volume --in-use + chứng minh crash-consistent: 17/17

(Lệnh đầy đủ: `/root/v4-phase2.sh`. Marker ghi qua SSH vào guest demo-vm, /data nằm trên vdb.)

| Case design | Lệnh | Kết quả |
|---|---|---|
| 1 | guest ghi `V4MARKER-BEFORE` + sync → `migrate-vm.sh migrate-volume $VDB --in-use` (verify compare mặc định) | PASS×5 — clone attached khi VM chạy; rc=0; in khối `Volume copied crash-consistent as of <UTC>`; `still in-use`; demo-vm ACTIVE trong + sau; state có `clone-time` |
| 6 | sau dòng `clone attached`: guest ghi `V4MARKER-AFTER` + sync; đọc image đích `rbd export mig-volumes/volume-<DVID> - \| grep -a -c V4MARKER-*` | PASS×3 — đích giữ tên `demo-data`; marker BEFORE **có** (1); marker AFTER **không có** (0) — đúng ngữ nghĩa điểm chụp |
| 4 | chạy lại `migrate-volume $VDB --in-use` | PASS — guard chặn copy trùng |
| 5 | `migrate-vm.sh rollback $VDB` | PASS×4 — rc=0, copy đích xóa, volume nguồn vẫn in-use, demo-vm ACTIVE |
| 3 | `migrate-vm.sh migrate-volume data-c --in-use` (volume available) | PASS×3 — `INFO --in-use ignored`, copy như thường, KHÔNG in khối crash-consistent; rollback sạch |

## Feature phase 3 — fix batch filter (2c) + rollback dọn volume mồ côi (2d): 13/13

| Case design | Lệnh | Kết quả |
|---|---|---|
| 18 (2c) | conf map rồi `migrate-batch.sh --project admin --volumes available --yes -- --keep-server-group --volume-type mig-typeb` | PASS×4 — rc=0; volume đích `type=mig-typeb` (option KHÔNG bị nuốt — trước fix sẽ thành type mặc định); batch log `ignoring VM-only options`; rollback sạch |
| 19 (2d) | tạo type đích `err-type` trỏ backend không tồn tại → `migrate-vm.sh migrate demo-vm --volume-type err-type --verify none` → volume đích rơi `error` sau create → die; `migrate-vm.sh rollback demo-vm` | PASS×9 — die sau create; **volume mồ côi CÓ trong volume-map** (fix 2d); orphan status `error` ở đích; nguồn SHUTOFF đúng giai đoạn; rollback rc=0 (code cũ die tại đây vì volume error); orphan bị xóa; đích rỗng; demo-vm ACTIVE + unlock lại; dọn err-type |

Ghi chú: lần chạy ĐẦU của case 19 (log `/root/v4-phase3.txt`) dùng map trỏ pool `cephha-vms` mà
`mig-xfer` không có quyền → preflight chặn sớm `FAIL RBD access to pool` nên không tới được
đường cần test — 4 FAIL trong log đó là lỗi kịch bản harness, không phải lỗi tool (tool chặn
sớm là ĐÚNG). Làm lại bằng err-type như trên (`/root/v4-phase3b.txt`), 9/9.

## Feature phase 4 — --keep-server-group trên lab thật: 17/17

(Setup: group nguồn `web-anti` policy soft-anti-affinity + 2 VM BFV `sg-vm1/2` tạo bằng
`--hint group=<id>`. Lệnh đầy đủ: `/root/v4-phase4.sh`.)

| Case design | Lệnh | Kết quả |
|---|---|---|
| 12 | `migrate-vm.sh plan sg-vm1 --keep-server-group --verify none` | PASS×2 — `OK will create az-web-anti (policy soft-anti-affinity)`; dest KHÔNG có group nào sau plan (plan read-only) |
| 9 | tạo trước `az-web-anti` policy affinity ở đích → plan lại | PASS — `FAIL server group az-web-anti has policy affinity…` |
| 7+8+14 | `migrate-batch.sh --vm sg-vm1,sg-vm2 --yes -- --keep-server-group --verify none` | PASS×6 — Totals OK 2; đích đúng 1 group `az-web-anti`; policy copy đúng; cả 2 VM đích là member; log con 1 `created destination server group`, con 2 `exists: the VM joins it` |
| 13 | `migrate-vm.sh rollback <id sg-vm1>` rồi `<id sg-vm2>` | PASS×6 — group còn sau rollback từng con, còn cả khi rỗng, có INFO `destination server group kept`; 2 VM nguồn ACTIVE lại |
| dọn | xóa az-web-anti, sg-vm1/2 + volume, web-anti, data-c, conf | PASS — đích rỗng (server/volume/group), nguồn còn demo-vm + mig-peer ACTIVE |

## Vòng 6 — regression gate trên BUILD CUỐI (migrate-vm md5 `69e870aa`, commit 51aa611)

| # | Case | Lệnh | Kết quả |
|---|---|---|---|
| 6a | blocksync unit | `bash /root/mig-lab/tests/blocksync-unit.sh` | PASS=8 FAIL=0 |
| 6b | e2e cold NGUYÊN BẢN | `bash /root/mig-lab/tests/e2e-migrate.sh` | PASS=50 FAIL=0 WARN=0 |
| 6c | live | `bash /root/mig-lab/tests/live-migrate.sh` | PASS=26 FAIL=0 |
| 6d | newip-voltype | `bash /root/mig-lab/tests/newip-voltype.sh` | PASS=26 FAIL=0 |
| 6e | extras | `bash /root/mig-lab/tests/extras.sh` | PASS=30 FAIL=0 |

**140/0 — ALL GATES GREEN (V4 final build).**

## Tổng

- Gate: vòng 5 = **140/0** (build A+B+C); vòng 6 = **140/0** (build cuối 51aa611).
- Feature: phase 1–4 = **17+17+13+17 = 64 PASS / 0 FAIL thật** (4 FAIL trong v4-phase3.txt là lỗi
  kịch bản harness, đã làm lại sạch), phủ đủ 27 case của DESIGN-V4 §4 (case 1–23 + guard phụ).
- Trạng thái lab cuối: demo-vm + mig-peer ACTIVE ở nguồn, đích rỗng hoàn toàn, không conf sót,
  không marker sót trong guest.
- Chưa test trên lab (ghi nhận, chấp nhận): `--keep-server-group` với group có `rules`
  (max_server_per_host) trên cloud thật ≥2.64 — lab chỉ có policy không rules (nhánh code --rule
  đã phủ bằng harness giả lập); client cũ từ chối `--rule` (giả lập); microversion ghim <2.64
  trả `policies[]` (giả lập).
