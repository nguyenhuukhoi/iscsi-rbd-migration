# Phát triển tiếp iscsi-rbd-migration

Tài liệu cho người sửa hoặc mở rộng tool. Đọc kèm:

- [GUIDE.md](GUIDE.md): cách dùng và luồng chạy.
- `DESIGN-V*.md`: vì sao làm như vậy.
- [CHANGELOG.md](CHANGELOG.md): đã thay đổi những gì.

Số dòng trong tài liệu này tính theo commit `af151e2` (nhánh `feature/v4-sg-inuse-vol`). Code đổi thì
số dòng lệch, khi đó tìm theo tên hàm.

## 1. Kiến trúc

### 1.1. Bốn file của tool

Bốn file này cài vào `/usr/local/bin` trên host chuyển:

| File | Vai trò | Chạy bằng |
|---|---|---|
| `migrate-vm.sh` | CLI chính: `plan`, `migrate`, `rollback`, `migrate-volume` | bash, trong `/opt/mig-venv` (openstackclient) |
| `migrate-batch.sh` | Chạy lô: chọn VM/volume, plan tất cả, xác nhận một lần, gọi `migrate-vm.sh` tuần tự với `SRC_PROJECT`/`DST_PROJECT` riêng cho từng VM | bash, mig-venv |
| `brick_attach.py` | `attach\|detach <volume-id>`: gắn volume (luôn là clone) vào host chuyển qua Cinder Attachment API + os-brick, in ra đường dẫn device. Bản ghi attach nằm ở `/var/lib/mig-attach/<volume-id>.json` | python của mig-venv |
| `blocksync.py` | Live mode: sync từ block device sang image RBD theo hash từng chunk 4 MiB, ghi cache hash ra file `--state`, in đúng 1 dòng JSON thống kê | **python hệ thống** `/usr/bin/python3` (cần `python3-rbd`), không phải venv |

`migrate-batch.sh` tìm `migrate-vm.sh` cùng thư mục với nó, hoặc theo biến `MIGRATE_VM`. Những thứ còn
lại trong repo (`0*-*.sh`, `kolla/`, `lab.sh`, `tests/`, `user-data.tmpl`) là phần dựng lab, không
thuộc tool.

### 1.2. Luồng migrate

Xem [GUIDE.md](GUIDE.md) mục 1: 8 bước từ preflight tới hoàn tất, cùng rollback R1–R3. Ở đây không
chép lại.

### 1.3. Các hàm theo từng giai đoạn trong `migrate-vm.sh`

| Giai đoạn | Hàm (dòng) | Ghi chú |
|---|---|---|
| Khởi động | conf loader (67–77), giá trị mặc định (81–96), `source` mig-venv (98) | Mục 3 |
| Gọi cloud | `scope_project` (103), `src_env`/`dst_env` (108–109), `src`/`dst` (110–111) | Mọi lệnh `openstack` đi qua `src`/`dst`, chạy trong subshell |
| Lỗi / log | `die` (113), `log` (112), `check_project_scope` (115) | |
| Dispatch | dòng 1244–1260 | `plan\|migrate` → `parse_options` → `apply_az_defaults` → `pool_for_type`. `migrate-volume` chạy `volume_options_only` trước |
| Option | `parse_options` (167), `pool_for_type` (198), `apply_az_defaults` (213), `volume_options_only` (1084) | Kiểm tra xung đột đặt ở cuối `parse_options` |
| Đọc nguồn | `export_definition` (298), `detect_boot` (555), `dest_volume_name` (332) | Ghi `server.json`, `attachments.json`, `ports.json`… |
| Preflight | `preflight` (419), `keep_sg_preflight` (349), `volume_preflight` (1115), `cmd_plan` (560) | Chỉ đọc. In dòng `OK`/`FAIL`/`INFO` |
| Migrate | `cmd_migrate` (889) | Thứ tự trong hàm, xem bảng dưới |
| Copy | `create_dest_volume` (575), `attach_clone` (608), `copy_volume_cold` (627), `sync_volume_pass` (656), `drop_clone` (620) | Luôn đọc từ **clone**, không đọc volume gốc |
| Tiến độ / dọn | `progress_watch` (263), `stop_watch` (290), `cleanup_attach` (573) | `cleanup_attach` là trap EXIT (dòng 897) |
| Nguồn ephemeral | `migrate_ephemeral_root` (678) | Đi đường Glance snapshot + REST |
| Tạo đích | `create_destination` (784), `keep_sg_ensure` (391) | Tạo port → server group → `server create` |
| Rollback | `cmd_rollback` (998), `cmd_rollback_volume` (1219), `drop_volume` (1203) | |
| migrate-volume | `cmd_migrate_volume` (1168), `resolve_volume` (1094), `dst_volumes_described` (1103), `create_volume_copy` (1146) | |
| Hàm chờ | `wait_volume` (230), `wait_image` (239), `wait_server` (248) | Status `error*` thì die |
| Tên → ID | `dst_sg_id` (135), `src_sg_id` (151), `dst_project_id` (131) | Resolve trong project của token |

Thứ tự bên trong `cmd_migrate`:

1. Guard state: `volume-map` đã có thì die (891).
2. `export_definition`, `detect_boot`, `preflight` (892–894); ghi `options` (895).
3. Bật trap EXIT `cleanup_attach` (897), tạo `volume-map` rỗng (899).
4. Chỉ live: pre-sync bằng `create_dest_volume` + `sync_volume_pass` lặp tới khi hội tụ (904–918).
5. Cutover (920): ghi `t-stop`; nguồn đang bị lock thì unlock kèm WARN; stop (932).
6. `detach_source_ports` (941), `migrate_ephemeral_root` (943), **rồi mới** `src server lock` (944).
   Lock của Nova chặn các thao tác này với creds không-admin, nên lock phải đứng sau.
7. Copy lần cuối: cold dùng `create_dest_volume` + `copy_volume_cold`, live dùng lượt
   `sync_volume_pass` cuối.
8. `create_destination` (969), tắt trap (972), `--start-source` nếu có.

### 1.4. `migrate-batch.sh`

| Đoạn | Dòng | Việc |
|---|---|---|
| options | 56 | Option của batch. Phần sau `--` vào mảng `PASS` và được truyền xuống mọi lần gọi `migrate-vm.sh` |
| rows | 116 | Liệt kê VM/volume theo selector (giao nhau), mỗi dòng một VM/volume. State đã có thì `SKIP(state-exists)` |
| plan | 228 | Chạy `migrate-vm.sh plan` cho từng VM, gom các dòng FAIL |
| migrate | 255 | Xác nhận một lần, rồi chạy tuần tự. Lọc `PASS` thành `VOLPASS` cho volume rời (264–271) |

Hai file có **hợp đồng chuỗi** với nhau: batch đọc output của `migrate-vm.sh`.

- `migrate-batch.sh:240` grep `^ +FAIL|^ERROR`.
- `:237` grep `ephemeral VM has no volume to migrate`.
- `:287` grep `WARN keypair <key> not at destination`.

Đổi các chuỗi này trong `migrate-vm.sh` thì phải sửa batch cùng lúc.

## 2. State

Gốc state là `STATE_ROOT=/var/lib/mig-migrate` (dòng 91). Mỗi lần chạy có một thư mục riêng. Rollback
thành công thì thư mục được đổi tên thành `<dir>.rolled-back.<epoch>`.

### 2.1. VM: `/var/lib/mig-migrate/<vm>/`

`<vm>` là **đúng đối số đã truyền** (tên hoặc ID). Batch luôn truyền ID, nên phải rollback bằng ID.

| File | Ghi bởi | Đọc bởi | Nội dung |
|---|---|---|---|
| `server.json` | `export_definition` | preflight, `create_destination`, `cmd_migrate`, `cmd_rollback`, `keep_sg_preflight`; dispatcher dùng nó để nhận ra rollback VM | `server show -f json` của nguồn |
| `attachments.json` | `export_definition` | `detect_boot`, `cmd_migrate`, `create_destination` (thứ tự device), `cmd_rollback` (dọn clone sót) | `server volume list` |
| `port-ids.json` | `export_definition` | `detach_source_ports`, `cmd_rollback` (khi không có `source-ports`) | ID các port nguồn |
| `ports.json` | `export_definition` | preflight, `create_destination`, `detach_source_ports`, `cmd_rollback` | network, CIDR, MAC, IP, tên SG của từng port |
| `volumes.json` | `export_definition` | preflight (kiểm encrypted/multiattach) | `volume show` từng volume |
| `options` | `cmd_migrate` | **không ai đọc** (chỉ để người tra cứu) | Tham số đã dùng. Rollback không dựa vào file này mà dựa vào việc file state nào đang có |
| `volume-map` | `cmd_migrate` tạo rỗng, `create_dest_volume` thêm dòng **ngay khi có ID volume** | live cutover, `create_destination`, output `--volumes-only`, preflight `--volumes-only`, `cmd_rollback`; batch dùng để phát hiện state đã có | Mỗi dòng `<src-vol-id> <dst-vol-id>`. File tồn tại nghĩa là có state |
| `t-stop` | `cmd_migrate` | test (`e2e-migrate.sh`, `live-migrate.sh`) để đo downtime | Epoch lúc cutover |
| `source-ports` | `detach_source_ports` | `cmd_rollback` | Port đã tháo (hoặc port giữ chỗ tạo lại) để gắn lại khi rollback |
| `dst-ports` | `create_destination` | `cmd_rollback` | ID các port đích |
| `dst-server-id` | `create_destination` | preflight (guard trùng tên), `cmd_rollback` | ID VM đích |
| `dst-image-id` | `migrate_ephemeral_root` | `create_destination` (`--image`), `cmd_rollback` | Image Glance ở đích |
| `dst-server-group` | `keep_sg_ensure` (v4) | `cmd_rollback` (chỉ in INFO, không xóa) | `<group-id> created=yes\|no` |
| `hash-<vid>.bin`, `sync-<vid>.log` | `sync_volume_pass` / `blocksync.py` | `blocksync.py` lượt sau; `live-migrate.sh` | Cache hash và JSON thống kê từng lượt |
| `user-data.yaml` | `create_destination` | `server create --user-data` | cloud-config tối thiểu |
| `root.img` | `migrate_ephemeral_root` | (tạm) | Image tải về; xóa sau khi upload, rollback cũng xóa |
| `*.err` | nhiều nơi | thông báo lỗi | stderr được giữ lại để in vào message die |

### 2.2. Volume rời: `/var/lib/mig-migrate/vol-<source-volume-id>/`

| File | Ghi bởi | Nội dung |
|---|---|---|
| `volume.json` | `cmd_migrate_volume` | `volume show` của nguồn. Đây là dấu để dispatcher nhận ra rollback volume |
| `volume-map` | `cmd_migrate_volume` (tạo rỗng), `create_volume_copy` | Một dòng `<src-id> <dst-id>`, ghi trước bước chờ |
| `options` | `cmd_migrate_volume` | Tham số đã dùng (chỉ để tra cứu) |
| `clone-time` | `cmd_migrate_volume` (v4, chỉ khi `--in-use`) | Mốc giờ clone (UTC) của bản copy crash-consistent |

### 2.3. Ngoài thư mục state

- `$STATE_ROOT/{brick-attach,sg-resolve,volume-show}.err`: stderr tạm.
- `/var/lib/mig-attach/<clone-id>.json`: bản ghi attach của `brick_attach.py`. Rollback dùng nó để
  detach clone sót.

### 2.4. Luật về state

- **Ghi state trước khi chờ.** ID của bất cứ thứ gì tool tạo ra phải vào state ngay khi có, trước mọi
  bước `wait_*` hay check có thể die. Bài học từ v4 §2d: volume ghi sau check pool thành mồ côi,
  rollback không dọn được.
- `volume-map` tồn tại nghĩa là có state. `migrate` và `migrate-volume` từ chối chạy đè, batch đánh
  dấu `SKIP(state-exists)`.
- Dispatcher rollback (1259): có `<arg>/server.json` thì là rollback VM, không thì là rollback volume.

## 3. Lớp cấu hình

Thứ tự ưu tiên: **CLI > biến môi trường > `$MIG_CONF` > mặc định trong script.**

Conf loader (67–77) làm như sau:

1. Với mỗi biến trong danh sách (70–71), nếu biến đó đã có trong env thì lưu lại (`${!_v+x}`).
2. `source "$MIG_CONF"`.
3. Ghi đè lại các giá trị env đã lưu.

Nhờ vậy env luôn thắng file, và cách chạy cũ bằng `export` không đổi hành vi. Sau đó các dòng
`VAR=${VAR:-default}` (81–96) lấp chỗ trống. `MIG_MULTIPATH` được `export` vì `brick_attach.py` đọc nó
từ env (79). CLI thắng nhờ `parse_options` gán sau cùng. Riêng `MIG_AZ`/`AZ_TYPE_MAP` chỉ lấp phần CLI
bỏ trống, thông qua `apply_az_defaults` (213) và hai cờ `AZ_CLI`/`TYPE_CLI`.

Các biến conf loader đọc: `SRC_RC DST_RC RBD_POOL RBD_POOL_MAP RBD_USER CEPH_CONF MIG_ISCSI_IP
MIG_MULTIPATH COPY_STREAMS PROJECT_MAP_FILE SRC_PROJECT DST_PROJECT MIG_AZ AZ_TYPE_MAP`. Ý nghĩa và mặc
định xem bảng biến môi trường ở [GUIDE.md](GUIDE.md) mục 2. `MIG_CONF` chọn file conf (mặc định
`/opt/mig/migrate.conf`).

`migrate-batch.sh` có loader riêng (dòng 28–34), chỉ lấy `SRC_RC DST_RC PROJECT_MAP_FILE`. Các biến
còn lại do mỗi lần gọi `migrate-vm.sh` tự đọc.

Thêm một biến conf mới cần đủ 3 chỗ:

1. Thêm tên biến vào danh sách ở dòng 70–71.
2. Thêm dòng mặc định `VAR=${VAR:-…}`.
3. Thêm dòng vào bảng env trong GUIDE mục 2.

Thiếu bước 1 thì env sẽ không thắng được file.

## 4. Quy ước bất di bất dịch

1. **Không phá hành vi cũ.** Mọi thay đổi phải qua regression gate: chạy **nguyên văn** 5 bộ test cũ
   (mục 6). Chỉ được sửa assert dính trực tiếp tới thay đổi đã được duyệt trong DESIGN, và phải liệt
   kê từng dòng sửa (file:dòng) trong commit cùng TEST-REPORT. Option mới không truyền thì output
   phải giống bản trước từng chữ.
2. **Tên phía đích luôn theo quy tắc, không có flag tắt:** volume `<vm>-<device>`, port `<vm>-<ip>`,
   volume rời giữ tên nguồn và mang description `migrated from <id>`. Rollback xóa theo ID trong state,
   không xóa theo tên.
3. **Rollback chạy lại được nhiều lần.** Mỗi bước phải bỏ qua thứ đã dọn: `|| true`, kiểm tồn tại
   trước khi xóa, `drop_volume` coi "đã mất" là xong. Phía nguồn, rollback chỉ được trả lại trạng thái
   trước khi migrate: unlock, gắn lại đúng các port tool đã tháo, start. Ngoài ra nó chỉ dọn đồ tạm do
   chính tool tạo (`mig-clone-<vid>`, `mig-snap-<vm>`). Không bao giờ sửa hay xóa server, volume, port
   gốc. Thứ có thể đang được VM khác dùng chung thì không xóa (vd server group `az-*`).
4. **Không bao giờ ghi vào dữ liệu nguồn**, ở mọi đường chạy. Tool chỉ đọc qua clone. Thao tác lên
   *trạng thái* nguồn (stop, lock, tháo port) chỉ xảy ra ở cutover, và rollback trả lại được hết.
5. **Preflight chỉ đọc.** `plan` không được tạo, sửa hay xóa gì trên cả 2 cloud. Thứ cần tạo (vd group
   `az-*`) thì preflight chỉ báo `OK will create …`, còn `migrate` mới tạo. Preflight chỉ được ghi state
   cục bộ và (nếu cần) start `iscsid` trên host chuyển.
6. **Die sớm và chỉ cách sửa.** Message `die`/`FAIL` phải nói lỗi gì **và** làm gì tiếp: lệnh rollback,
   option thay thế, ID cần truyền. Kiểm tra được trước khi đụng nguồn thì đặt trong preflight hoặc
   `parse_options`.
7. **Option mới mặc định tắt.** Hành vi mới phải bật bằng flag hoặc biến. Không truyền thì tool chạy
   như bản cũ.

Quy ước kỹ thuật (đã từng gây lỗi thật):

- Mọi lệnh `openstack` phải qua `src`/`dst`, hoặc `( src_env; … )` / `( dst_env; … )` trong subshell,
  để `SRC_PROJECT`/`DST_PROJECT` scope đúng project.
- Tên (security group, server group…) phải resolve sang **ID trong project của token**, vì admin nhìn
  thấy `default` của mọi project (bug v3, `c862d09`).
- Script chạy dưới `set -euo pipefail`. Có 3 bẫy:
  - Phải gán `$(jq …)` vào biến trước rồi mới dùng. Lỗi bên trong process substitution `<(…)` không
    dừng script.
  - `grep -c` không khớp dòng nào thì trả mã 1, phải viết `|| true`.
  - Lỗi bên trong `if ! x=$(…)` không làm script dừng, phải tự xử lý.
- Dòng preflight theo đúng khuôn `"  OK    …"`, `"  FAIL  …"`, `"  INFO  …"`, vì batch grep `^ +FAIL`.
- Option mới không có tham số thì phải thêm vào bộ lọc `VOLPASS` của batch (`migrate-batch.sh:268`).
  Không thêm thì option kế tiếp bị nuốt (bug v4 §2c).

## 5. Quy trình phát triển

1. **Bàn**: yêu cầu, giới hạn, các phương án.
2. **User chốt** phương án.
3. Viết `DESIGN-Vx.md`, hoặc thêm mục vào DESIGN của bản đang làm. Nội dung gồm:
   - hành vi;
   - từ chối và giới hạn;
   - state và rollback;
   - những gì KHÔNG đổi;
   - test plan đánh số case.
4. Code trên **nhánh mới** (`feature/<tên>`), không code thẳng vào `main`. Commit nhỏ, message nói rõ
   phần nào.
5. **Review chéo**: một người (hoặc agent) khác đọc diff đối chiếu với DESIGN. Điểm nào DESIGN mơ hồ
   thì nêu ra, không tự quyết lặng lẽ.
6. **Test trên lab:**
   - regression gate (mục 6, chạy nguyên văn);
   - feature test theo từng case của DESIGN.
7. Viết `TEST-REPORT-<yyyymmdd>.md`: mỗi case có **lệnh nguyên văn và kết quả**, gồm cả FAIL oan và
   cách xác minh lại, assert cũ nào bị sửa, trạng thái lab sau test, những gì chưa test.
8. Cập nhật docs: GUIDE (option, ví dụ), DEPLOY nếu đụng tới, thêm mục cho bản mới trong CHANGELOG.
9. **User quyết merge** về `main`.

## 6. Hệ thống test

Test chạy trên **lab 2 cloud thật**: nguồn Cinder LVM/iSCSI, đích Cinder Ceph RBD, cùng một host chuyển.
Cách dựng ở [GUIDE.md](GUIDE.md) mục 5. Copy test vào host chuyển và chạy bằng `root` theo GUIDE
mục 5.5. Phần lớn test cần `demo-vm` đang ACTIVE ở nguồn và chưa có state; mỗi test tự rollback khi
xong, trừ `failure-injection.sh`.

### 6.1. Regression gate: 5 bộ, kỳ vọng tổng 140 PASS / 0 FAIL

| Bộ | Kiểm tra gì | Kỳ vọng |
|---|---|---|
| `blocksync-unit.sh` | `blocksync.py` giữa loop device và RBD: byte giống hệt sau lượt đầu và lượt delta, lượt delta chỉ ghi chunk thật sự đổi. Không đụng VM | 8/0 |
| `e2e-migrate.sh` | Cold mặc định, giữ IP/MAC. Kiểm dữ liệu, SSH host key, libvirt dùng RBD, reboot ở đích, VM khác ở nguồn còn gọi được, rollback, rồi migrate lại | 50/0 |
| `live-migrate.sh` | `--mode live` trong lúc guest ghi liên tục: downtime đo bằng log của chính guest, toàn vẹn dữ liệu, cutover chỉ mang delta, rollback | 26/0 |
| `newip-voltype.sh` | `--ip new --volume-type --start-source`: 2 VM chạy song song, volume đích đúng type, port nguồn không bị tháo, rollback | 26/0 |
| `extras.sh` | VM nhiều NIC (2 network), override `--flavor`/`--security-group`, server properties; VM ephemeral qua Glance | 30/0 |

### 6.2. Các bộ khác (chạy khi đụng tới phần liên quan)

| Bộ | Kiểm tra gì |
|---|---|
| `failure-injection.sh` | Preflight từ chối, `kill -9` qemu-img giữa lúc copy, rollback dọn clone, chuyển lại (kết thúc với VM ở đích) |
| `e2e-nova-port.sh` | VM tạo bằng `--network` + `--boot-from-volume`: port do Nova sở hữu, volume không tên |
| `attach-matrix.sh` | Đường API nào cho host chuyển đọc được boot volume của VM đã tắt |
| `port-guard-matrix.sh` | Trạng thái port nguồn nào còn giữ liên lạc giữa VM ở nguồn và VM đã chuyển |
| `powerstore-sim.sh` | Multipath LIO 2 portal: mất một đường giữa lúc đọc, chỉ đọc, detach sạch |

Nên có thêm một **harness giả lập** cho từng thay đổi: thay `src`/`dst` bằng hàm trả fixture JSON, rồi
gọi trực tiếp hàm của `migrate-vm.sh` (cắt file trước dòng dispatch rồi `source`). Cách này test logic
nhanh và không cần cloud. Nó chỉ bổ sung, **không thay** được test lab.

### 6.3. Bẫy khi viết test

- **`set -o pipefail` + `grep -q`**: `cmd | grep -q X` trả mã lỗi của `cmd` (vd `plan` die mã 1) dù
  grep đã khớp, nên assert FAIL oan. `grep -q` thoát sớm còn có thể làm `cmd` nhận SIGPIPE (141).
  Cách đúng: `out=$(cmd 2>&1)` rồi `grep -q X <<<"$out"`.
- **Phải source openrc trước khi hỏi `openstack`.** Test dùng hàm bọc `src()`/`dst()`. Lệnh
  `openstack` gõ trần trong shell harness chưa source rc sẽ báo `Missing value auth-url`, và assert
  FAIL oan.
- **Không `scp` đè một script bash đang chạy.** Bash đọc file theo offset, nên lệnh sau sẽ bị lệch dòng.
  Muốn sửa thì copy sang tên khác, hoặc chờ script chạy xong.
- `pkill -f <pattern>` khớp cả chính phiên đang gõ lệnh đó.
- Rollback bằng **đúng đối số đã dùng lúc migrate**. Batch ghi state theo ID, nên rollback bằng tên
  sẽ không thấy state.
- Assert phải có khả năng FAIL thật (đừng assert trên biến rỗng). Test xong phải trả lab về trạng thái
  ban đầu: `demo-vm` ACTIVE ở nguồn, đích sạch, không còn state.

## 7. Thêm một option mới: checklist

Ví dụ thật: `--keep-server-group` (v4).

1. **Design**: thêm mục vào DESIGN (DESIGN-V4 §2), ghi rõ:
   - hành vi;
   - xung đột với `--server-group`;
   - `plan` không tạo gì;
   - state `dst-server-group`;
   - rollback không xóa group;
   - các case test (7)–(14).

   Chờ user chốt.
2. **Usage header** (dòng 41–43). Help in ra bằng `sed -n '2,51p'` (dòng 1244): header dài thêm thì
   tăng số dòng cuối, rồi chạy `migrate-vm.sh` không tham số để thấy option cuối vẫn hiện đủ.
3. **Biến + parse**:
   - giá trị mặc định tắt: `KEEP_SG=0` (dòng 165);
   - nhánh `case` trong `parse_options` (dòng 181): option không tham số dùng `shift`, có tham số dùng
     `X=$2; shift 2`;
   - kiểm tra xung đột ở cuối `parse_options` (dòng 195).
4. **Từ chối ở lệnh không áp dụng**:
   - `migrate-volume`: thêm vào `volume_options_only` (dòng 1087);
   - ngược lại, `--in-use` bị `plan`/`migrate` từ chối ngay tại dispatcher (dòng 1248);
   - batch: option không tham số thì thêm vào bộ lọc `VOLPASS` (`migrate-batch.sh:268`).
5. **Preflight (chỉ đọc)**: `keep_sg_preflight` (349), gắn vào `preflight` ở dòng 493.
   - Chỉ in dòng OK/FAIL/INFO và đặt `fail=1` khi FAIL.
   - Thêm chữ ` keep-server-group` vào dòng tiêu đề preflight (430).
   - `--volumes-only` thì in INFO "ignored" (441).
6. **Hành động**: `keep_sg_ensure` (391), gọi ở dòng 850 bên trong `create_destination`, ngay trước
   `server create`. Kiểm lại hiện trạng vì có thể đã đổi từ lúc preflight (copy có khi mất hàng giờ).
7. **State**: ghi `dst-server-group` (416) ngay khi có ID.
8. **Rollback**: quyết định dọn hay giữ. Group dùng chung nên giữ, chỉ in INFO (dòng 1008). Rollback
   phải chạy được dù hành động làm dở chừng.
9. **Test**:
   - harness giả lập cho logic (không có group / sẽ tạo / dùng lại / khác policy / trùng tên /
     microversion cũ);
   - feature test trên lab theo từng case của DESIGN;
   - regression gate 140/0;
   - TEST-REPORT.
10. **Docs**: bảng option + ví dụ trong GUIDE, mục mới trong CHANGELOG. Làm sau khi test xanh.

Trước khi commit:

```bash
for f in migrate-vm.sh migrate-batch.sh; do bash -n "$f" || echo "syntax error in $f"; done
grep -nE '<pattern thông tin nội bộ>' <các file đã sửa>   # phải rỗng: repo public, không IP/mật khẩu thật
```
