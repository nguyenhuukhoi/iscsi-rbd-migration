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

## 1b. File cấu hình (chốt 17/11: thay export bằng .conf; map project file riêng)

- **`/opt/mig/migrate.conf`** (đổi đường dẫn bằng `MIG_CONF=<file>`): bash `KEY=VALUE`, chứa các
  giá trị trước đây truyền qua export: `SRC_RC DST_RC RBD_POOL RBD_POOL_MAP RBD_USER CEPH_CONF
  MIG_ISCSI_IP MIG_MULTIPATH COPY_STREAMS PROJECT_MAP_FILE`.
- **Thứ tự ưu tiên: option CLI > biến env > migrate.conf > mặc định trong script.** Env vẫn đè
  được conf (loader lưu các biến env đã set trước khi source conf rồi khôi phục lại) — script cũ,
  test cũ, cách chạy cũ bằng export **không đổi hành vi**.
- **`/opt/mig/project-map.conf`** (file riêng, trỏ qua `PROJECT_MAP_FILE` trong migrate.conf):
  mỗi dòng `project-nguồn  project-đích` (phân cách khoảng trắng), `#` là comment. Project không
  có trong file → dùng cùng tên. Không có file → toàn bộ map cùng tên.

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
- Map project nguồn→đích: **cùng tên là mặc định** (user đã duyệt), cặp lệch tên khai trong
  `project-map.conf` (mục 1b).

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
| Tên volume/port đích | **Chốt cuối 17/11 (user)**: áp MẶC ĐỊNH cho migrate thường lẫn volumes-only, không flag, không tiền tố/hậu tố. Volume: `<instance>-<device>` (vd `app-vm01-vda`, `app-vm01-vdb`). Port: `<instance>-<ip>` (vd `app-vm01-192.168.120.50`); với `--ip new` tạo port xong đọc IP được cấp rồi `port set --name` lại cho đúng quy tắc. Flag `--name-from-instance` bỏ hẳn. Description volume giữ `migrated from <src-vol-id>`. Test cũ assert tên cũ (`demo-root`, `mig-<vm>-<i>`) sửa đúng các dòng đó theo cơ chế gate §5 |
| Tên instance trùng → arg là ID | Tên dùng cho đặt tên lấy từ `server.json .name` (đã có từ fix 6174780) |

## 4b. Convert toàn bộ volume của một project (chốt thêm 17/11)

Batch mode volume: `migrate-batch.sh --project <p> --volumes attached|available|all`.

| Scope | Cách làm | Naming đích |
|---|---|---|
| `attached` | Liệt kê VM của project có volume → chạy từng VM bằng `migrate-vm.sh migrate <id> --volumes-only` (stop + lock VM như thiết kế volumes-only) | `<instance>-<device>` |
| `available` | Liệt kê `volume list --project <p> --status available` → lệnh mới `migrate-vm.sh migrate-volume <vol-id>`: clone nguồn → attach clone ro → copy → verify — volume gốc không bị đụng, KHÔNG dừng gì | **Giữ nguyên tên nguồn** (kể cả rỗng); mọi volume đích mang description `migrated from <src-vol-id>` |
| `all` | Cả hai scope trên, attached trước, available sau | Theo từng scope |
| Danh sách instance tự chọn | `migrate-batch.sh --vm vm1,vm2[,...] -- --volumes-only` (selector `--vm` sẵn có + passthrough) — convert volume của đúng các instance chỉ định | `<instance>-<device>` |

- Volume trạng thái khác (error, reserved, in-use mơ hồ…) → liệt kê SKIP(status) trong bảng.
- Guard trùng ở đích cho available: tra theo **description** `migrated from <src-id>` (tên volume
  Cinder không unique nên không tra theo tên) — đã có thì FAIL con đó, gợi ý xóa/rollback.
- Rollback `migrate-volume`: xóa volume đích theo state (`/var/lib/mig-migrate/vol-<id>/`);
  nguồn nguyên vẹn suốt quá trình.
- Giới hạn nói trước: snapshot của volume KHÔNG đi theo (chỉ copy dữ liệu hiện tại);
  volume available đang có snapshot vẫn clone được nhưng cây snapshot ở lại nguồn.

## 5. Cam kết tương thích ngược (user yêu cầu 17/11: không phá logic/tính năng cũ)

- **Mọi option hiện có dùng được trong thiết kế mới**, cả gọi lẻ lẫn truyền qua batch sau `--`:
  `--mode cold|live, --ip same|new|<addr>, --volume-type, --az, --flavor, --security-group,
  --start-source, --keep-source-ports, --verify, --max-passes, --delta-limit`.
- **Không có `migrate.conf`** → tool đọc env/mặc định y hệt hôm nay. Không có
  `SRC_PROJECT`/`DST_PROJECT` → không scope gì thêm, appcred vẫn dùng được cho migrate lẻ.
- Luồng copy/verify/rollback, guard trùng đích/trùng tên, thứ tự lock, progress %, state dir,
  format `volume-map`/`options` — giữ nguyên.
- Thay đổi hành vi **duy nhất** được phép (đã duyệt ở mục 3): VM có keypair không còn FAIL
  preflight mà bỏ `key_name` + báo; và tên volume/port ở đích theo instance nguồn (trước đây
  volume đích lấy tên volume nguồn — thường rỗng với BFV — và port tên `mig-*`). Rollback xóa
  theo ID trong state nên đổi tên không ảnh hưởng rollback.
- **Regression gate bắt buộc trước khi merge** (cơ chế do thiết kế đặt ra để chứng minh cam kết,
  không phải nguyên văn yêu cầu của user): chạy lại các bộ test cũ — `blocksync-unit.sh` 8/0,
  `e2e-migrate.sh` 50/0 (không conf, chạy bằng env như cũ), `live-migrate.sh` 26/0 — trên bản
  nhánh, rồi mới test tính năng mới (mục 7). Test cũ **chỉ được sửa đúng các assert dính tới
  thay đổi đã duyệt** (naming volume/port ở đích), liệt kê từng dòng trong commit; mọi assert
  khác giữ nguyên từng chữ. Với naming mặc định (chốt cuối): các assert dính tên cũ ở
  `live-migrate.sh:75`, `extras.sh:87,93,94`, `newip-voltype.sh:36-37,68` được sửa theo quy tắc
  mới (volume `<vm>-vda`; port tra theo `--server`/ID thay vì đoán tên khi IP chưa biết).

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
