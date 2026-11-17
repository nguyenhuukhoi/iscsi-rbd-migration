# Test report V3-P1 — 2026-11-17 (nhánh feature/batch-migrate)

Build test: `migrate-vm.sh` md5 `83a09da9` = commit `2b69692` (+ design `4d18796`).
Môi trường: lab mục 9 system-info; chạy trên mig-host; creds mặc định appcred
(`SRC_RC=/opt/mig/src/appcred-openrc.sh DST_RC=/opt/mig/dst/appcred-openrc.sh` trừ khi ghi khác).
Log gốc trên mig-host: `/root/gate-*.txt`, `/root/gate2-*.txt`, `/root/p1-*.txt`.

## Vòng 1 — regression gate trên build P1 đầu (commit 9e38771, naming luôn-bật kiểu nic-index)

| # | Case | Lệnh | Kết quả |
|---|---|---|---|
| 1 | blocksync unit | `bash /root/mig-lab/tests/blocksync-unit.sh` | PASS=8 FAIL=0 |
| 2 | e2e cold NGUYÊN BẢN, env-style không conf | `bash /root/mig-lab/tests/e2e-migrate.sh` | PASS=50 FAIL=0 WARN=0 |
| 3 | live (assert :75 sửa theo naming) | `bash /root/mig-lab/tests/live-migrate.sh` | PASS=26 FAIL=0 |

## Vòng 2 — regression gate trên BUILD CHỐT (naming `<vm>-<device>` / `<vm>-<ip>`, commit 2b69692)

| # | Case | Lệnh | Kết quả |
|---|---|---|---|
| 4 | blocksync unit | `bash /root/mig-lab/tests/blocksync-unit.sh` | PASS=8 FAIL=0 |
| 5 | e2e cold NGUYÊN BẢN từng chữ | `bash /root/mig-lab/tests/e2e-migrate.sh` | PASS=50 FAIL=0 WARN=0 |
| 6 | live (assert :75 = `$VM-vda`) | `bash /root/mig-lab/tests/live-migrate.sh` | PASS=26 FAIL=0 |
| 7 | newip-voltype (port bám theo ID, vá luôn assert PASS-rỗng cũ) | `bash /root/mig-lab/tests/newip-voltype.sh` | PASS=26 FAIL=0 |

## Tính năng mới P1

| # | Case | Lệnh | Kết quả |
|---|---|---|---|
| 8 | Multi-NIC + port tên theo IP (`demo-vm-192.168.120.50`, `demo-vm-192.168.121.51`), override flavor/SG, server properties, ephemeral | `bash /root/mig-lab/tests/extras.sh` | PASS=30 FAIL=0 |
| 9 | volumes-only: copy 2 volume, không tạo server/port | `migrate-vm.sh migrate demo-vm --volumes-only` | PASS — đích có `demo-vm-vda` (10G) + `demo-vm-vdb` (2G), không server, không port |
| 10 | volumes-only rollback | `migrate-vm.sh rollback demo-vm` | PASS — volume đích dọn sạch, nguồn ACTIVE lại |
| 11 | server-group thiếu → preflight chặn | `migrate-vm.sh plan demo-vm --server-group mig-anti` | PASS — `FAIL server group mig-anti not found... create it first: openstack server group create --policy anti-affinity mig-anti` |
| 12 | server-group có thật | `openstack server group create --policy anti-affinity mig-anti` rồi `migrate-vm.sh migrate demo-vm --server-group mig-anti --keep-source-ports --verify none` | PASS — VM đích nằm trong group (tra theo ID); rollback sạch |
| 13 | Conf file thay export | tạo `/opt/mig/migrate.conf` (SRC_RC/DST_RC/RBD_POOL) rồi `unset SRC_RC DST_RC RBD_POOL; migrate-vm.sh plan demo-vm` | PASS — `Preflight passed.` thuần bằng conf, không env |
| 14 | Guard appcred + chọn project | `DST_PROJECT=proj-x migrate-vm.sh plan demo-vm` (DST_RC=appcred) | PASS — die: `SRC_PROJECT/DST_PROJECT needs a password openrc: an application credential is bound to one project (...)` |
| 15 | Migrate vào đúng project chỉ định | `DST_PROJECT=proj-b DST_RC=/opt/mig/dst/admin-openrc.sh migrate-vm.sh migrate demo-vm --ip new --verify none --start-source` | PASS — VM đích `project_id` = proj-b; rollback từ proj-b sạch |

## Ghi chú

- Case 9–15 chạy trong harness ad-hoc; 4 assert của harness báo FAIL oan (2 vì shell chưa source
  rc khi hỏi trạng thái nguồn — lộ dòng `Missing value auth-url`; 2 vì `set -o pipefail` lấy
  exit-code của `plan`/`die` đè lên grep match). Cả 4 đã chạy lại đúng cách, PASS, dòng output
  thật ghi ở case 11/14; trạng thái nguồn verify riêng: `demo-vm ACTIVE locked=False`.
- Trạng thái lab sau toàn bộ: demo-vm ACTIVE ở nguồn, đích rỗng, không conf file sót,
  proj-b/mig-anti đã xóa.
- Tổng: **gate 110 PASS / 0 FAIL (2 vòng), feature 22/22 PASS**.
- Chưa test (để P2): `migrate-batch.sh`, `migrate-volume` (available volume), `--vm-file`,
  `--volumes attached|available|all`, PROJECT_MAP_FILE, SRC_PROJECT.
