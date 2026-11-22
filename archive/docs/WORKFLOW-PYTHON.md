# Workflow (app `migrate`): migrate một instance, từng bước

Tài liệu này đi qua từng bước khi app `migrate` chuyển **một** VM boot-from-volume với **option mặc
định**. Có 2 lệnh:

```bash
migrate migrate vm-a --yes               # cold (mặc định)
migrate migrate vm-a --mode live --yes   # live
```

Bỏ `--yes` thì app hỏi `Type yes` trước bước 1 (mục 0). Bản này dựng lại [WORKFLOW.md](WORKFLOW.md) của bản
bash cho app Python. Các bước, lệnh gọi ra ngoài, file state và dòng log giống bản bash. App đã được kiểm
bằng harness so từng lời gọi `openstack`/`qemu-img`/`rbd` theo thứ tự, và bằng 5 bộ test cũ trên lab.
Phần thêm là **mục 0**: lớp chọn VM và xác nhận bọc ngoài.

Số đo thời gian lấy từ các báo cáo test của bản bash. Bước copy giống hệt, nên dùng được cho app. Các số
đó đo trên **lab lồng**, chỉ để thấy tỷ lệ giữa các bước, không ước lượng được production. IP thuộc dải
TEST-NET.

Ví dụ xuyên suốt: VM `vm-a` thuộc project `proj-a`, một NIC IP `198.51.100.50`, hai volume `vda` (root
10 GB) và `vdb` (data 2 GB). Tên module/hàm trong ngoặc là chỗ tìm trong `migratetool/`
([DEVELOPING-PYTHON.md](DEVELOPING-PYTHON.md) mục 1).

## 0. Lớp bọc ngoài: chọn VM, plan, xác nhận

| Bước | App làm gì | Đổi gì |
|---|---|---|
| 0a. Đọc lệnh | Tách selector với option của VM (`selector.split_args`/`finish`). Đọc conf (`config.load`). Kiểm option mọi VM sẽ dùng (`check_child_options`): option sai, `--max-passes x` hay `--in-use` đều dừng ở đây | Không |
| 0b. Liệt kê | `server list --all-projects --long --name ^vm-a$ -f json`, `project show <id>`. In bảng `Selected:`. Nếu `/var/lib/mig-migrate/vm-a/volume-map` (hoặc `<id>/volume-map`) đã có thì `SKIP(state-exists)` kèm lệnh rollback, rồi dừng với mã 1 | Ghi log JSON vào `/var/log/mig-batch/<YYYYmmdd-HHMMSS>/` |
| 0c. Plan | Tiến trình con `python -I -m migratetool.single plan vm-a <option>` với `SRC_PROJECT=proj-a DST_PROJECT=proj-a` (đổi tên theo `PROJECT_MAP_FILE` nếu có). Chạy bước 1–3 dưới đây, chỉ đọc. Output vào `plan-<id>.log`. In bảng `Plan:` | Chỉ file state cục bộ, không có `volume-map` |
| 0d. Xác nhận | `Migrate 1 item(s) one by one? Type yes to continue:`. Có `--yes` thì bỏ qua. stdin không phải terminal mà thiếu `--yes` thì dừng | Không |
| 0e. Migrate | Tiến trình con `python -I -m migratetool.single migrate vm-a <option>`, cùng cặp project. Chạy bước 1–10 (cold) hoặc 1–11 (live), output hiện ra màn hình và ghi `migrate-<id>.log` | Mọi thứ của bước 1–10 |
| 0f. Tổng kết | Bảng `Summary:`, dòng `Totals: OK 1  FAIL 0  SKIP 0`. Thoát 0 nếu OK | Không |

- Preflight chạy **2 lần**: lần ở 0c, và lần nữa ở bước 3 trong tiến trình migrate. Batch cũ cũng làm vậy.
  Lần 2 bắt được thay đổi ở đích xảy ra trong lúc chờ người gõ `yes`.
- **Tiến trình con chạy theo đúng chữ đã gõ** (`vm-a`). Vì thế state nằm ở `/var/lib/mig-migrate/vm-a/`, và
  rollback là `migrate rollback vm-a`.

## 1. "Mặc định" nghĩa là gì

Giống bảng mục 0 của [WORKFLOW.md](WORKFLOW.md). Tóm lại:

| Thứ | Mặc định |
|---|---|
| Mode / IP / Verify | `cold` / `--ip same` (giữ IP + MAC, tháo port nguồn) / cold so toàn bộ (`readback` ≠ `none`) |
| Volume type, pool, AZ | Type mặc định của đích (hoặc `AZ_TYPE_MAP`), `RBD_POOL` (hoặc `RBD_POOL_MAP`), scheduler tự chọn (hoặc `MIG_AZ`) |
| Live | `--max-passes 5`, `--delta-limit 64` MB |
| Luồng copy | `COPY_STREAMS=8` |
| Flavor, SG, keypair | Cùng tên với nguồn. Keypair thiếu ở đích thì tạo VM không có `key_name` |
| Tên ở đích | `vm-a-vda`, `vm-a-vdb`, port `vm-a-198.51.100.50` |
| Project | `proj-a` ở cả 2 phía (tự đặt, mục 0c) |
| Sau khi xong | VM nguồn SHUTOFF + lock |

## 2. Cold: `migrate migrate vm-a --yes`

10 bước, tất cả chạy trong tiến trình con của mục 0e. **Downtime bắt đầu ở bước 4.**

Thời gian tham khảo, đo bằng bản bash, VM 10 + 2 GB trên lab:

- Tổng 293 s (v1, TEST-REPORT-20261105.md).
- Tổng 276,8 s (12/11, TEST-REPORT-20261112.md).
- Copy 150–307 MB/s, verify 47–74 MB/s (17/11).

| # | Bước (module) | Lệnh / log chính | State | VM nguồn | Fail thì |
|---|---|---|---|---|---|
| 1 | Khởi động, chặn chạy đè (`cli.run_vm`, `vmcmd.cmd_migrate`) | Đọc conf + `MIG_DEFAULT_OPTS` + option, lấp AZ/type từ conf. Appcred với `SRC_PROJECT`/`DST_PROJECT` thì dừng. Có `volume-map` thì dừng: `state exists in … : run rollback first` | Chưa ghi | ACTIVE | Dừng, không có gì để dọn |
| 2 | Đọc nguồn (`vmdef.export_definition`, `detect_boot`) | `server list --name ^vm-a$` (trùng tên thì dừng, liệt kê ID), `server show`, `server volume list`, `port list --server`, rồi `port/network/subnet/security group show`, `volume show` | Tạo `/var/lib/mig-migrate/vm-a/`: `server.json`, `attachments.json`, `port-ids.json`, `ports.json`, `volumes.json` | ACTIVE, chỉ bị đọc | Dừng. Chưa có `volume-map` nên chạy lại được ngay |
| 3 | Preflight đích (`preflight.preflight`) | `Destination preflight (mode=cold ip=same type=default pool=<pool>):` rồi từng dòng `OK`/`FAIL`/`INFO`: tên server chưa có ở đích, flavor, keypair, server group nguồn → `<AZ>-<tên>` (7.1), network/subnet/MAC/IP, port security/pairs/dns_name (7.1), SG → `security group web -> will create <AZ>-web (N rules)` hoặc `-> found …` (7.1), volume không mã hóa/multiattach, `rbd ls`, `iscsid` | Không thêm. **Không tạo gì trên cloud** | ACTIVE | Sau các dòng `FAIL` thì thoát mã 1. Sửa đích rồi chạy lại |
| 3b | Copy SG (7.1, `secgroups.ensure`) | SG `<AZ>-<tên>` chưa có thì `security group create`, xóa 2 rule egress mặc định, chép từng rule; log `created destination security group <AZ>-web (<id>) from web: N rules` | `dst-security-groups` | **ACTIVE: chưa tính downtime** | Xóa các SG vừa tạo, dừng. Chưa có `volume-map` nên chạy lại được ngay |
| — | Qua preflight | Ghi `options`, bật dọn tự động (`try/except` gọi `copyvol.cleanup_attach`), tạo `volume-map` rỗng | `options`, `volume-map` | ACTIVE | Từ đây muốn chạy lại phải `rollback` |
| 4 | Stop | Ghi `t-stop`. VM đang lock thì log `WARN source VM is already locked …; unlocking to proceed`. Log `stop source VM`, `server stop`, chờ SHUTOFF (≤ 600 s) | `t-stop` | **SHUTOFF, downtime bắt đầu** | Rollback unlock + start lại |
| 5 | Tháo port (`server.detach_source_ports`) | `server remove port`, chờ `device_id` rỗng. Port do Nova tạo bị Nova xóa: tạo lại `mig-reserve-vm-a-<i>` cùng MAC/IP, log `Nova removed its own port; reserved …` | `source-ports` | SHUTOFF, không NIC | `port … still attached to …`. Rollback gắn lại port đã tháo |
| 6 | Lock | `server lock`, log `source stopped, its ports detached, now locked`. Lock đặt **sau** bước 5, vì lock của Nova chặn việc tháo port với creds không phải admin | — | SHUTOFF + lock | Rollback unlock, gắn port, start |
| 7 | Tạo volume đích (`copyvol.create_dest_volume`) | `volume create vm-a-vda --size … --description "migrated from <src-id>"`, chờ `available`, `rbd info volume-<id>` trong pool (sai pool thì dừng `… not visible in pool …: wrong RBD_POOL_MAP …`), chép bootable + image property | `volume-map` thêm `<src-id> <dst-id>` **ngay khi có ID** | SHUTOFF + lock | Rollback xóa mọi volume trong `volume-map`, kể cả volume `error` |
| 8 | Clone → attach → copy → verify → dọn (`copyvol.copy_volume_cold`) | `volume create mig-clone-<src-id> --source …`, rồi `python -I -m migratetool.brick attach <clone-id>`: attach chỉ đọc, kiểm cỡ device = cỡ volume, log `clone attached at /dev/sdX`. Tiếp theo `qemu-img convert -f raw -O raw -n --target-is-zero -m 8 -W /dev/sdX rbd:…`, mỗi 15 s log `copy NN% …`, xong log `copy done in Ns`. Rồi `qemu-img compare` (`verify byte-for-byte` → `verify OK in Ns`). Cuối cùng detach + xóa clone | Lúc gắn: `/var/lib/mig-attach/<clone-id>.json` (có `scsi_wwn`), xóa khi detach | SHUTOFF + lock | `verification failed for <src-id>`. Lỗi giữa chừng thì clone tự được detach. Rollback xóa volume đích + clone sót |
| 9 | Port + VM đích (`server.create_destination`) | `port create vm-a-198.51.100.50 --network … --mac-address … --fixed-ip … --security-group <id>` (7.1: id của `<AZ>-<tên>`, cộng `--allowed-address …`/`--disable-port-security`/`--description`/`--dns-name` theo port nguồn), server group `<AZ>-<tên>` (7.1, tạo nếu chưa có), rồi `server create vm-a --flavor … --port … --volume <dst-vda> --block-device …vdb… --user-data …`, chờ ACTIVE (≤ 600 s), log `destination VM ACTIVE; control-plane downtime so far Ns` | `dst-ports`, `dst-server-group`, `user-data.yaml`, `dst-server-id` | SHUTOFF + lock | Rollback xóa VM, port, volume đích rồi trả nguồn (giữ server group + SG) |
| 10 | Xong | `Migrated. Source VM: kept SHUTOFF and locked. Rollback: migrate rollback vm-a`. Tiến trình cha in `Summary:` + `Totals:` | Giữ nguyên thư mục state (cần cho rollback) | SHUTOFF + lock, volume gốc nguyên vẹn | — |

Thời lượng từng bước (v1): bước 4–6 hết 26 s. Clone 2 volume hết 19 s, copy 66 s, so 87 s. Tạo port + VM
hết 22 s, guest boot hết 24 s.

## 3. Live: `migrate migrate vm-a --mode live --yes`

11 bước. Khác cold ở chỗ volume đích được tạo và **pre-sync lặp nhiều lượt khi VM còn chạy**; cutover chỉ
sync phần delta. Downtime đo trên lab bằng bản bash là **250,5 s**, cold cùng ngày 276,8 s
(TEST-REPORT-20261112.md).

Preflight live kiểm thêm:

- VM phải boot-from-volume. Root ephemeral thì `FAIL --mode live needs boot-from-volume`.
- `OK    blocksync present`: file `blocksync.py` trong package.
- `OK    librbd python bindings`: `SYS_PY` import được `rados, rbd`.

Ngay từ mục 0a, app kiểm `--max-passes`/`--delta-limit` là số nguyên. Nhập sai thì dừng, không lặp vô hạn
như bản bash.

| # | Bước (module) | Khác cold |
|---|---|---|
| 1–3 | Như cold | Dòng tiêu đề preflight ghi `mode=live`, thêm 2 kiểm tra trên |
| 4 | Tạo volume đích khi VM còn chạy (`livesync.presync` → `create_dest_volume`) | Như cold bước 7 nhưng làm **trước** khi stop, VM ACTIVE. Bước 4 và 5 lặp theo từng volume |
| 5 | Pre-sync lặp (`livesync.sync_volume_pass`) | Mỗi lượt: clone mới, attach, rồi `/usr/bin/python3 -I <package>/blocksync.py --device /dev/sdX --pool <pool> --image volume-<dst-id> --id <RBD_USER> --conf <CEPH_CONF> --state …/hash-<src-id>.bin [--first] --verify readback`, cuối cùng detach + xóa clone. Log `pass N <src-id>: X/Y chunks changed, Z MB written, Ts`. Từ lượt 2, delta ≤ 64 MB thì log `<src-id> converged (delta N MB <= 64 MB)`. Đủ 5 lượt chưa hội tụ thì log `WARN … did not converge after 5 passes …; cutover will carry it` và vẫn cutover. State: `hash-<src-id>.bin`, `sync-<src-id>.log`. VM nguồn **ACTIVE** |
| 6–8 | Stop, tháo port, lock | Như cold bước 4–6. Downtime bắt đầu |
| 9 | Delta cuối (`livesync.final_pass`) | Clone mới (VM đã tắt nên nhất quán), blocksync không `--first`, có readback, log `pass final <src-id>: …`. `--verify full` thì thêm `qemu-img compare` |
| 10–11 | Port/VM đích, xong | Như cold bước 9–10 |

Số đo pre-sync trên lab:

- Root 10 GB: lượt 1 ghi 2 268 MB trong 130 s, lượt 2 còn 44 MB thì hội tụ.
- Data 2 GB, guest ghi 4 MB/s: không hội tụ sau 5 lượt.
- Lượt cuối: root 104 MB / 60 s, data 140 MB / 16 s. Lượt này vẫn đọc hash toàn bộ device, nên thời gian vẫn
  tỷ lệ với cỡ volume.

Guest ghi càng nhiều thì live càng giống cold.

## 4. Sau khi xong

| Phía | Trạng thái |
|---|---|
| Nguồn | `vm-a` SHUTOFF + lock, không NIC (port tháo ra vẫn giữ IP/MAC). Volume gốc nguyên vẹn |
| Đích | `vm-a` ACTIVE trong `proj-a`, port `vm-a-198.51.100.50` cùng MAC/IP, volume `vm-a-vda`, `vm-a-vdb` |
| Host chuyển | `/var/lib/mig-migrate/vm-a/` còn nguyên, cần cho rollback. Log ở `/var/log/mig-batch/<thời gian>/` |

**Nghiệm thu:**

- Checksum dữ liệu trong guest. Bắt buộc khi đã bỏ so sánh bằng `--verify none`.
- Với SAN thật, đối chiếu WWN của clone đang gắn với volume trên mảng, trong lúc copy:
  [DEPLOY-PYTHON.md](DEPLOY-PYTHON.md) mục 10.

**Rollback** bằng đúng chữ đã gõ: `migrate rollback vm-a`. `vm-a` thuộc project khác project của openrc
thì gõ `SRC_PROJECT=proj-a DST_PROJECT=proj-a migrate rollback vm-a`. Rollback chạy lại được nhiều lần:

1. Đích: xóa VM, port, volume trong `volume-map`, image Glance (đường ephemeral).
2. Nguồn: dọn đồ tạm của tool (`mig-clone-<src-id>`, `mig-snap-vm-a`).
3. Nguồn: unlock, gắn lại đúng các port đã tháo, start, chờ ACTIVE.
4. Đổi tên thư mục state thành `vm-a.rolled-back.<epoch>`.

Dữ liệu ghi ở VM đích sau khi migrate mất theo volume đích. Xóa nguồn chỉ khi đã nghiệm thu xong, và làm
bằng tay: tool không có lệnh xóa nguồn.

## 5. Các đường rẽ khỏi mặc định

| Muốn | Lệnh | Xem |
|---|---|---|
| IP + MAC mới, bật lại nguồn | `migrate migrate vm-a --ip new --start-source --yes` | [GUIDE-PYTHON.md](GUIDE-PYTHON.md) mục 4 |
| Không tháo port nguồn | `--keep-source-ports` | [GUIDE.md](GUIDE.md) mục 2 (rủi ro split-brain) |
| Chỉ copy volume, không tạo VM đích | `migrate migrate vm-a --volumes-only --yes` hoặc `migrate volume --vm vm-a --yes` | [GUIDE-PYTHON.md](GUIDE-PYTHON.md) mục 5.2 |
| VM root ephemeral | Tự rẽ sang Glance snapshot, chỉ cold | [GUIDE.md](GUIDE.md) mục 2 |
| Server group | `--server-group <g>`; 7.1: theo group nguồn là mặc định, tắt bằng `--no-server-group` | [GUIDE-PYTHON.md](GUIDE-PYTHON.md) mục 4b.4, [GUIDE.md](GUIDE.md) 2b.5 |
| Giữ SG cùng tên như 7.0 thay vì copy `<AZ>-<tên>` | `--no-copy-secgroup` | [GUIDE-PYTHON.md](GUIDE-PYTHON.md) mục 4b.3 |
| Một volume, copy nóng | `migrate volume <vol> --in-use` | [GUIDE-PYTHON.md](GUIDE-PYTHON.md) mục 5.1 |
| Nhiều VM một lượt | `migrate migrate --host … / --project … / --vm a,b --yes` | [GUIDE-PYTHON.md](GUIDE-PYTHON.md) mục 2 |
