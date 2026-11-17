# DESIGN V3 — batch migrate theo node/project/nhóm VM, server group, volume-only (2026-11-17, CHỜ DUYỆT)

Scope do người vận hành chốt 17/11. Ba chi tiết đánh dấu **[đề xuất]** là mặc định do thiết kế
điền, sửa được khi duyệt. Code trên nhánh `feature/batch-migrate`, chưa merge về main.

## 1. Yêu cầu

1. Chọn **1 hay nhiều node** (hypervisor) → convert toàn bộ VM trên đó sang cloud mới.
2. Chọn 1 hay nhiều node **+ 1 hay nhiều project** → chỉ convert VM của các project đó trên các node đó.
3. Convert **một nhóm VM** chỉ định tay.
4. Khi convert 1 VM hay 1 nhóm: **chỉ định server group ở đích**.
5. Chế độ **chỉ convert volume** (không tạo instance ở đích), tên volume đích đặt theo instance
   nguồn và phân biệt được khi có nhiều volume.
6. Khi convert instance: **port và volume ở đích đặt tên theo instance nguồn**.
7. Người vận hành cung cấp **một account thuộc tất cả các project** (mỗi cloud một account).

## 2. Account và scope project

- Account dạng **password openrc** (bắt buộc — application credential bị khóa vào đúng 1 project,
  không rescope được; tool die sớm nếu đưa appcred kèm chọn project).
- Có role trong **mọi project liên quan ở cả 2 cloud**. Mức quyền:
  - **Lọc theo node cần quyền admin** (field `OS-EXT-SRV-ATTR:host` và `--all-projects` là
    admin-only theo policy mặc định).
  - Giữ MAC/IP (`--ip same`) trên provider network cần admin (đã chứng minh 17/11).
  - Mọi thứ còn lại member là đủ.
- **Mỗi VM được xử lý trong scope project của chính nó, ở cả 2 phía**: tool đè
  `OS_PROJECT_NAME` (env `SRC_PROJECT`/`DST_PROJECT`) sau khi source openrc. Nhờ đó clone tạm,
  volume, port, server đều nằm đúng project — quota và ownership đúng chỗ.
- Map project nguồn→đích: **[đề xuất] cùng tên là mặc định**, cặp lệch tên khai
  `PROJECT_MAP="srcA:dstA,srcB:dstB"`.

## 3. `migrate-batch.sh` (script mới, bọc ngoài migrate-vm.sh)

```
migrate-batch.sh [--host h1[,h2...]] [--project p1[,p2...]] [--vm v1[,v2...]]
                 [--plan-only] [--yes] [--continue-on-error]
                 [-- <options chuyển nguyên vẹn cho từng migrate-vm.sh>]
```

- Selector giao nhau khi dùng chung (`--host` ∩ `--project` ∩ `--vm`); phải có ít nhất một.
  `--vm` nhận tên hoặc ID; tên trùng trong project thì bắt dùng ID (tool per-VM đã xử lý).
- Luồng: **liệt kê** (bảng ID / tên / trạng thái / project nguồn / project đích) → chạy `plan`
  cho TOÀN BỘ (bảng PLAN-OK / PLAN-FAIL kèm dòng lỗi) → `--plan-only` dừng ở đây → hỏi xác nhận
  (`--yes` bỏ qua; stdin không phải tty mà thiếu `--yes` thì die) → **migrate tuần tự bằng ID**
  → bảng tổng kết OK / FAIL / SKIP(lý do), exit khác 0 nếu có FAIL.
- VM có state cũ (`/var/lib/mig-migrate/<id>/volume-map`) → SKIP kèm gợi ý rollback.
- **[đề xuất] Fail policy: dừng ngay tại con fail**; cờ `--continue-on-error` để chạy tiếp.
- **[đề xuất] Keypair**: Nova không cho gán keypair của user khác → VM đích tạo **không có**
  `key_name`, bảng tổng kết liệt kê từng con bị bỏ key để user tự add lại (SSH vẫn vào được vì
  authorized_keys nằm trong đĩa). (Thay thế hành vi hiện tại: preflight FAIL cả con.)

## 4. Thay đổi trong `migrate-vm.sh`

| Mục | Thiết kế |
|---|---|
| `SRC_PROJECT` / `DST_PROJECT` (env) | Đè `OS_PROJECT_NAME` + unset `OS_PROJECT_ID` ngay sau khi source rc, ở MỌI chỗ source (wrapper src/dst, token Glance). Die sớm nếu rc là appcred |
| `--server-group <name>` (option mới) | Resolve trong project đích → `--hint group=<uuid>` khi `server create`. Preflight: group tồn tại (FAIL kèm lệnh tạo gợi ý), in policy (affinity/anti-affinity). Không auto-create |
| `--volumes-only` (option mới) | Chỉ copy volume, không tạo port/server ở đích. Cold only đợt này. Nguồn vẫn stop+lock mặc định; `--start-source` được phép với mọi ip-mode (không có xung đột IP). Preflight bỏ check network/flavor/SG/keypair. Rollback: xóa volume đích + unlock/start nguồn |
| Tên volume đích | `<tên-instance>-<device>` (vd `app-vm01-vda`, `app-vm01-vdb`) — áp cho CẢ migrate thường lẫn volumes-only. Description giữ `migrated from <src-vol-id>` |
| Tên port đích | `<tên-instance>-nic<i>` theo thứ tự NIC (vd `app-vm01-nic0`) |
| Tên instance trùng → arg là ID | Tên dùng cho đặt tên lấy từ `server.json .name` (đã có từ fix 6174780) |

## 5. Những điều KHÔNG đổi

- Tool per-VM vẫn dùng được một mình như cũ; không có `SRC_PROJECT`/`DST_PROJECT` thì hành vi
  y hệt hôm nay (appcred vẫn dùng được cho migrate lẻ).
- Luồng copy/verify/rollback, guard trùng đích, lock-sau-detach, progress % — giữ nguyên.

## 6. Giới hạn nói trước

- Server group ở đích phải tạo trước, đúng project; anti-affinity thiếu host thì Nova sẽ NoValidHost
  ở bước tạo — preflight không đoán trước được scheduler.
- Batch chạy tuần tự, không song song (iSCSI + băng thông không lợi khi chạy đôi; giữ đơn giản).
- Volumes-only không mang theo: port/IP, metadata server, server group — chỉ volume + bootable flag.
- Lọc theo node: tên hypervisor phải đúng như `openstack hypervisor list` (FQDN nếu cloud dùng FQDN).

## 7. Kế hoạch test trên lab (trước khi merge)

1. Dựng: project `proj-b` ở cả 2 cloud + user `mig-all` có role ở admin-project lẫn proj-b (cả 2
   cloud) + VM `batch-vm` thuộc proj-b trên compute nguồn + server group `mig-sg-anti`
   (anti-affinity, 1 member) ở project đích.
2. `--host <compute> --plan-only`: bảng đủ demo-vm (admin), mig-peer (admin), batch-vm (proj-b),
   đúng project đích từng con; không đổi trạng thái gì.
3. `--project proj-b --yes`: batch-vm sang đích **nằm đúng project proj-b**, volume tên
   `batch-vm-vda`, port `batch-vm-nic0`; rollback.
4. `--vm demo-vm -- --server-group mig-sg-anti`: VM đích vào đúng group; preflight FAIL khi group
   không tồn tại; rollback.
5. `migrate-vm.sh migrate demo-vm --volumes-only`: đích có đúng 2 volume `demo-vm-vda`,
   `demo-vm-vdb`, không server/port; rollback xóa volume + nguồn ACTIVE lại.
6. Batch 2 VM trong đó 1 con cố tình PLAN-FAIL: mặc định dừng đúng chỗ; `--continue-on-error`
   chạy hết; bảng tổng kết đúng.
7. Dọn: xóa batch-vm (+ volume vì BFV không tự xóa), proj-b, user, server group — lab về nguyên trạng.

## 8. Thứ tự code (Opus, trên nhánh này)

P1: migrate-vm.sh (SRC/DST_PROJECT + naming + --server-group + --volumes-only) → review + test lẻ.
P2: migrate-batch.sh → review + test batch (mục 7). P3: GUIDE/DEPLOY/TEST-REPORT + merge về main.
