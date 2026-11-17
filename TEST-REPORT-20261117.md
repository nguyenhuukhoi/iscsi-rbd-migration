# Test report V3 (P1 + P2) — 2026-11-17/18 (nhánh feature/batch-migrate)

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
- Tổng P1: **gate 110 PASS / 0 FAIL (2 vòng), feature 22/22 PASS**.

---

# Phần 2 — V3-P2 (2026-11-18, cùng nhánh)

Build test P2: commit `23fb678` (`migrate-vm.sh` md5 `921ef37c`, thêm `migrate-batch.sh` +
lệnh `migrate-volume`). Build sau fix SG: commit `c862d09` (md5 `1d4bee26`).
Log gốc trên mig-host: `/root/gate3-*.txt`, `/root/gate4-*.txt`, `/root/p2-*.txt`, `/root/p2r-*.txt`.

## Vòng 3 — regression gate trên build P2 (commit 23fb678)

| # | Case | Lệnh | Kết quả |
|---|---|---|---|
| 16 | blocksync unit | `bash /root/mig-lab/tests/blocksync-unit.sh` | PASS=8 FAIL=0 |
| 17 | e2e cold NGUYÊN BẢN | `bash /root/mig-lab/tests/e2e-migrate.sh` | PASS=50 FAIL=0 WARN=0 |
| 18 | live | `bash /root/mig-lab/tests/live-migrate.sh` | PASS=26 FAIL=0 |
| 19 | newip-voltype | `bash /root/mig-lab/tests/newip-voltype.sh` | PASS=26 FAIL=0 |

## Tính năng mới P2 — vòng đầu (build 23fb678): 11 PASS / 5 FAIL

Setup chung: proj-b + `batch-vm` (ACTIVE, vlan120) ở nguồn, volume rời `data-backup-01` 1G
available trong admin, compute host nguồn = `mig-src-cmp`.

| # | Case | Lệnh | Kết quả |
|---|---|---|---|
| 20 | Chọn theo host, plan-only, đa project | `migrate-batch.sh --host mig-src-cmp --plan-only` | PASS — bảng đủ 3 VM thuộc 2 project (batch-vm/proj-b, demo-vm + mig-peer/admin), không migrate gì. **Nhưng batch-vm ra PLAN-FAIL `security group default` → lộ BUG THẬT (xem dưới)** |
| 21 | Migrate VM đa project | `migrate-batch.sh --vm batch-vm --yes -- --verify none` | **FAIL — bug SG**: preflight `FAIL security group default`, Totals `FAIL 1`, không migrate. Rollback đúng scope (`SRC_PROJECT=proj-b DST_PROJECT=proj-b`) PASS |
| 22 | vm-file có dòng rác | `printf 'no-such-vm\nbatch-vm\n' > /root/vmlist.txt` rồi `migrate-batch.sh --vm-file /root/vmlist.txt --plan-only` | PASS — `no-such-vm` thành `SKIP(not-found)`, batch-vm vẫn được plan |
| 23 | Volume rời cả project, giữ nguyên tên | `migrate-batch.sh --project admin --volumes available --yes` | PASS — copy+verify OK trong 8s; đích có volume **cùng tên `data-backup-01`**; nguồn vẫn `available` nguyên vẹn; in-use volume được báo "not listed — they go with their VM". Rollback theo ID `migrate-vm.sh rollback 97c23752-…` PASS, copy đích dọn sạch |
| 24 | Volume attached theo VM | `migrate-batch.sh --vm demo-vm --volumes attached --yes -- --verify none` | PASS — đích có `demo-vm-vda` (10G) + `demo-vm-vdb` (2G), KHÔNG tạo server/port; nguồn SHUTOFF+locked đúng thiết kế. Assert rollback của harness FAIL OAN (xem ghi chú) |
| 25 | PLAN-FAIL chặn migrate | `migrate-batch.sh --vm demo-vm --yes -- --volume-type bogus-type` | Lần đầu dính dây chuyền từ case 24 → `SKIP(state-exists)` (đúng hành vi guard, nhưng không test được điều muốn test); retest ở case 29 |

## Bug thật phát hiện: security group `default` đa project

- Hiện tượng: preflight đích `FAIL security group default` với VM thuộc proj-b, dù proj-b có SG default.
- Nguyên nhân: tool resolve SG theo TÊN bằng token admin — admin thấy SG `default` của MỌI
  project nên tên thành mơ hồ/lệch project.
- Fix (commit `c862d09`): resolve SG name→ID trong đúng project scope
  (`openstack security group list --project <pid>`); phía đích die rõ nếu không resolve được,
  phía NGUỒN (đang giữa cutover) không die — chỉ WARN + bỏ SG thiếu kèm lệnh `port set` gợi ý.

## Retest sau fix SG (build c862d09): 7/7 PASS

| # | Case | Lệnh | Kết quả |
|---|---|---|---|
| 26 | Batch migrate VM đa project | `migrate-batch.sh --vm batch-vm --yes -- --verify none` | PASS — preflight `OK security group default (83ed0fb4…)`, VM đích ACTIVE, port `batch-vm-192.168.120.67`, downtime 145s, Totals `OK 1` |
| 27 | VM đích đúng project | `openstack server show` (project_id) | PASS — VM đích thuộc proj-b |
| 28 | Rollback batch | `SRC_PROJECT=proj-b DST_PROJECT=proj-b migrate-vm.sh rollback 42242a49-…` | PASS — batch-vm ACTIVE lại ở nguồn, đích sạch |
| 29 | PLAN-FAIL chặn migrate (retest case 25) | `migrate-batch.sh --vm demo-vm --yes -- --volume-type bogus-type` | PASS — `PLAN-FAIL volume type bogus-type`, exit!=0, bảng ghi `FAIL(plan)`, Totals `FAIL 1`, KHÔNG migrate gì |

## Vòng 4 — regression gate trên build fix SG (commit c862d09, md5 1d4bee26)

| # | Case | Lệnh | Kết quả |
|---|---|---|---|
| 30 | blocksync unit | `bash /root/mig-lab/tests/blocksync-unit.sh` | PASS=8 FAIL=0 |
| 31 | e2e cold NGUYÊN BẢN | `bash /root/mig-lab/tests/e2e-migrate.sh` | PASS=50 FAIL=0 WARN=0 |
| 32 | live | `bash /root/mig-lab/tests/live-migrate.sh` | PASS=26 FAIL=0 |
| 33 | newip-voltype | `bash /root/mig-lab/tests/newip-voltype.sh` | PASS=26 FAIL=0 |
| 34 | extras (multi-NIC, port theo IP — vì fix SG đụng đường tạo port đích) | `bash /root/mig-lab/tests/extras.sh` | PASS=30 FAIL=0 |

## Ghi chú P2

- 5 FAIL vòng đầu phân rã: **2 là bug thật** (case 20/21 — SG default, đã fix + retest PASS);
  **1 FAIL oan của harness** (case 24: rollback bằng TÊN `demo-vm` trong khi batch chạy và ghi
  state theo ID — chạy đúng `SRC_PROJECT=admin DST_PROJECT=admin migrate-vm.sh rollback
  b2f4d5b0-…` thì PASS); **2 dây chuyền** từ FAIL oan đó (case 25: demo-vm còn state →
  `SKIP(state-exists)` — bản thân guard hoạt động đúng; retest sạch ở case 29 PASS).
- 3 giả định thiết kế P2 verify trên lab trước khi code: `import openstack` (SDK) OK trên
  mig-host; `openstack volume create "" --size 1` chấp nhận tên rỗng (cần cho giữ-nguyên-tên
  kể cả volume không tên); `server list --long` có cột Project ID (cần cho enumerate đa project).
- Trạng thái lab sau toàn bộ: demo-vm ACTIVE ở nguồn, đích rỗng, proj-b/batch-vm/vmlist/
  data-backup-copy đã dọn.
- Tổng P2: **gate 110/0 (vòng 3) + 140/0 (vòng 4); feature 11 PASS vòng đầu + 7/7 retest;
  1 bug thật phát hiện-fix-chứng minh (SG default đa project)**.
- Chưa test (chấp nhận, ghi ở design §6): PROJECT_MAP_FILE với mapping thật khác tên
  (lab chỉ có map rỗng — "other projects keep their name"), `--continue-on-error` với FAIL
  giữa chừng thật, `--volumes all`.
