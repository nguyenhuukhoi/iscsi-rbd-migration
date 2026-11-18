# Workflow: migrate một instance, từng bước

Tài liệu này đi qua từng bước tool chạy khi migrate **một** VM boot-from-volume với **option mặc
định**. Có 2 lệnh:

```bash
migrate-vm.sh migrate vm-a               # cold (mặc định)
migrate-vm.sh migrate vm-a --mode live   # live
```

Mọi lệnh, đường dẫn và thông báo log đều trích từ `migrate-vm.sh` / `blocksync.py` / `brick_attach.py`
tại commit `1641270`. Số đo thời gian lấy từ báo cáo test có thật và ghi nguồn ngay cạnh. Các số đó đo
trên **lab lồng**, chỉ để thấy tỷ lệ giữa các bước, không dùng ước lượng production (production phụ
thuộc đường mạng iSCSI và Ceph thật). IP trong ví dụ thuộc dải TEST-NET.

Ví dụ xuyên suốt: VM `vm-a`, một NIC IP `198.51.100.50`, hai volume `vda` (root 10 GB) và `vdb`
(data 2 GB). Đây cũng là dạng VM dùng trong các lần đo.

Sơ đồ tổng quát và rollback R1–R3: [GUIDE.md](GUIDE.md) mục 1. Chi tiết file state:
[DEVELOPING.md](DEVELOPING.md) mục 2.

## 0. "Mặc định" nghĩa là gì

| Thứ | Mặc định | Nghĩa |
|---|---|---|
| Mode | `cold` | Tắt VM nguồn trước khi copy |
| IP | `--ip same` | Giữ nguyên IP + MAC. Port nguồn bị **tháo** khỏi VM nguồn lúc cutover (vẫn giữ chỗ IP/MAC ở nguồn) |
| Verify | `--verify readback` | Cold: mọi giá trị trừ `none` đều là `qemu-img compare` toàn bộ. Live: đọc lại từng chunk vừa ghi |
| Volume type | type mặc định của cloud đích | (v4) Nếu có AZ hiệu lực (`--az` hoặc `MIG_AZ`) và khai `AZ_TYPE_MAP` thì lấy type theo AZ ([GUIDE.md](GUIDE.md) 2b.1b) |
| Pool RBD | `RBD_POOL` (lab: `mig-volumes`) | Hoặc pool theo type trong `RBD_POOL_MAP` |
| AZ | scheduler đích tự chọn | (v4) Hoặc `MIG_AZ` |
| Live: số lượt / ngưỡng | `--max-passes 5`, `--delta-limit 64` (MB) | |
| Luồng copy (cold) | `COPY_STREAMS=8` | `qemu-img convert -m 8` |
| Flavor, security group | cùng **tên** với nguồn | Phải có sẵn ở đích. SG được resolve sang ID trong project đích |
| Keypair | giữ nếu đích có keypair cùng tên | Không có thì tạo VM không `key_name` (key vẫn nằm trong đĩa) |
| Tên ở đích (v3+) | volume `<vm>-<device>`, port `<vm>-<ip>` | `vm-a-vda`, `vm-a-vdb`, `vm-a-198.51.100.50` |
| Sau khi xong | VM nguồn giữ SHUTOFF + lock | `--start-source` mới bật lại (chỉ với `--ip new\|<addr>`) |

## 1. Cold: `migrate-vm.sh migrate vm-a`

10 bước. **Downtime bắt đầu ở bước 4** (stop VM) và kéo tới khi guest ở đích boot xong. Mọi bước
sau bước 3 nằm trong downtime.

Thời gian tham khảo có 2 nguồn:

- Bảng "Downtime" của [TEST-REPORT-20261105.md](TEST-REPORT-20261105.md) (bản v1, VM 10 + 2 GB,
  tổng **293 s**).
- [TEST-REPORT-20261112.md](TEST-REPORT-20261112.md): cold cùng ngày với lần đo live là **276,8 s**.
  Lần cold 10 GB ngày 17/11 cho copy **150–307 MB/s**, verify **47–74 MB/s** (verify đọc 1 luồng).

### Bước 1. Khởi động, chặn chạy đè

| | |
|---|---|
| Tool làm gì | Đọc conf (`$MIG_CONF`), env, option. `apply_az_defaults` lấp AZ/type từ conf (v4). `check_project_scope` chặn appcred khi có `SRC_PROJECT`/`DST_PROJECT`. Nếu `/var/lib/mig-migrate/vm-a/volume-map` đã có thì die `state exists in … : run rollback first` |
| State | Chưa ghi gì |
| VM nguồn | ACTIVE, chưa bị đụng |
| Fail thì | Die ngay, không có gì để dọn |
| Thời lượng | Tức thời |

### Bước 2. Đọc cấu hình nguồn (`export_definition`, `detect_boot`)

| | |
|---|---|
| Tool làm gì | Gọi lần lượt: `server list --name ^vm-a$` (trùng tên thì die, liệt kê ID, bảo chạy lại bằng ID), `server show`, `server volume list`, `port list --server`. Rồi với từng port: `port show`, `network show`, `subnet show`, `security group show` (lấy tên). Với từng volume: `volume show`. Có `/dev/vda` hoặc `/dev/sda` trong danh sách volume gắn thì là boot-from-volume; không thì rẽ sang đường ephemeral (mục 4) |
| State | Tạo thư mục `/var/lib/mig-migrate/vm-a/` (chmod 700), ghi `server.json`, `attachments.json`, `port-ids.json`, `ports.json`, `volumes.json` |
| VM nguồn | ACTIVE, chỉ bị đọc |
| Fail thì | Die. Chưa có `volume-map` nên chạy lại `migrate` được ngay, không cần rollback |
| Thời lượng | Chưa đo riêng (chỉ là gọi API đọc) |

### Bước 3. Preflight đích (`preflight`)

| | |
|---|---|
| Tool làm gì | In `Destination preflight (mode=cold ip=same type=default pool=<pool>):`, rồi mỗi kiểm tra một dòng `OK`/`FAIL`/`INFO`. Các kiểm tra: |
| | • đích chưa có server tên `vm-a` (có thì FAIL kèm lệnh rollback); |
| | • `flavor show <cùng tên>`; keypair (thiếu chỉ INFO); |
| | • mỗi port: network cùng tên, subnet cùng CIDR, MAC chưa ai dùng (`port list --mac-address`), IP chưa ai dùng, chỉ 1 fixed IP, không có allowed-address-pairs, mỗi security group resolve được đúng 1 ID trong project đích; |
| | • volume không mã hóa, không multiattach; |
| | • `rbd … -p <pool> ls`; `iscsid` đang chạy (chưa chạy thì start). |
| State | Không thêm gì. **Không tạo hay sửa gì trên cloud** |
| VM nguồn | ACTIVE |
| Fail thì | Sau các dòng `FAIL`, tool thoát mã 1 (không có dòng `ERROR`). Chưa có `volume-map`: sửa phía đích rồi chạy lại. Muốn xem trước mà không chạy thật thì dùng `migrate-vm.sh plan vm-a`, nó chạy đúng bước 2 + 3 |
| Thời lượng | Chưa đo riêng |

Preflight qua thì tool ghi file `options` (chỉ để tra cứu), bật trap EXIT `cleanup_attach` (chết
giữa chừng thì tự detach clone), rồi tạo `volume-map` **rỗng**. Từ lúc này đã có state: muốn chạy lại
thì phải `rollback` trước.

### Bước 4. Bắt đầu cutover: stop VM nguồn

| | |
|---|---|
| Tool làm gì | Ghi `t-stop` (epoch). VM đang bị lock thì log `WARN source VM is already locked (reason: …); unlocking to proceed` rồi `server unlock`. VM chưa SHUTOFF thì log `stop source VM`, `server stop`, chờ SHUTOFF (tối đa 600 s) |
| State | `t-stop` |
| VM nguồn | **SHUTOFF, downtime bắt đầu** |
| Fail thì | Chờ quá hạn hoặc VM vào ERROR thì die. `migrate-vm.sh rollback vm-a` unlock và start lại VM |
| Thời lượng | Bước 4–6 cộng lại 26 s (v1) |

### Bước 5. Tháo port nguồn (`detach_source_ports`, vì `--ip same`)

| | |
|---|---|
| Tool làm gì | Với từng port: `server remove port`, rồi chờ `device_id` của port rỗng (tối đa 60 × 2 s). Port do **Nova tự tạo** (VM tạo bằng `--network`) sẽ bị Nova xóa khi tháo. Khi đó tool tạo lại ngay port `mig-reserve-vm-a-<i>` cùng MAC/IP để giữ chỗ cho rollback, log `Nova removed its own port; reserved <ip> <mac> again as <id>` |
| State | `source-ports`: ID các port đã tháo (hoặc port giữ chỗ) |
| VM nguồn | SHUTOFF, không còn NIC. IP/MAC vẫn được giữ ở cloud nguồn |
| Fail thì | Port không nhả thì die `port <id> still attached to …`. Rollback gắn lại các port có trong `source-ports`; port chưa kịp tháo thì vẫn đang gắn và được bỏ qua |
| Thời lượng | Nằm trong 26 s ở bước 4 |

### Bước 6. Lock VM nguồn

| | |
|---|---|
| Tool làm gì | `server lock`, log `source stopped, its ports detached, now locked`. Lock phải **sau** bước 5: lock của Nova chặn thao tác tháo port với creds không-admin |
| State | Không thêm |
| VM nguồn | SHUTOFF + locked, không NIC |
| Fail thì | Die. Rollback unlock, gắn lại port, start |
| Thời lượng | Nằm trong 26 s ở bước 4 |

### Bước 7. Tạo volume đích (`create_dest_volume`, lặp từng volume theo thứ tự gắn)

| | |
|---|---|
| Tool làm gì | `volume create vm-a-vda --size <cỡ nguồn> --description "migrated from <src-id>"` (thêm `--type`/`--availability-zone` nếu có). Chờ `available`. Kiểm `rbd info volume-<id>` trong pool, không thấy thì die `volume-<id> not visible in pool <pool>: wrong RBD_POOL_MAP …`. Volume nguồn bootable thì `volume set --bootable`; copy các `--image-property` `hw_*`/`os_*`… |
| State | `volume-map` thêm dòng `<src-id> <dst-id>` **ngay khi có ID**, trước bước chờ và check pool (v4) |
| VM nguồn | SHUTOFF + locked |
| Fail thì | Die. Rollback xóa mọi volume trong `volume-map`, kể cả volume đang `error` hoặc đã mất |
| Thời lượng | Gộp trong "gắn, gỡ, xóa clone, tạo volume đích và các gọi API ≈ 50 s" (v1) |

### Bước 8. Clone → attach → copy → verify → dọn clone (`copy_volume_cold`, cùng vòng lặp với bước 7)

| | |
|---|---|
| Tool làm gì | |
| • clone | `volume create mig-clone-<src-id> --source <src-id>`, chờ `available`. Volume gốc không bao giờ bị gắn hay ghi |
| • attach | `brick_attach.py attach <clone-id>`: Cinder Attachment API ở mode `ro`, os-brick login iSCSI, kiểm **cỡ device = cỡ volume** (chặn LUN lạ), `blockdev --setro`. Log `clone attached at /dev/sdX` |
| • copy | Log `copy /dev/sdX -> <pool>/volume-<dst-id>`, rồi `qemu-img convert -f raw -O raw -n --target-is-zero -m 8 -W /dev/sdX rbd:<pool>/volume-<dst-id>:id=<RBD_USER>:conf=<CEPH_CONF>`. Mỗi 15 s in `copy NN% (x.y/10 GiB, R MB/s)`; xong in `copy done in Ns (… MB/s logical)` |
| • verify | Log `verify byte-for-byte`, `qemu-img compare -f raw -F raw /dev/sdX rbd:…`, in `verify NN% …`, rồi `verify OK in Ns` |
| • dọn | `brick_attach.py detach`, chờ `available`, `volume delete` clone |
| State | Lúc đang gắn: `/var/lib/mig-attach/<clone-id>.json` (có `scsi_wwn`), xóa khi detach. Lỗi attach ghi ở `/var/lib/mig-migrate/brick-attach.err` |
| VM nguồn | SHUTOFF + locked |
| Fail thì | Compare lệch thì die `verification failed for <src-id>`. Copy hay attach lỗi cũng die. Trap EXIT tự detach clone đang gắn. Rollback xóa volume đích và clone `mig-clone-<src-id>` còn sót |
| Thời lượng (v1) | Clone 2 volume 19 s, copy 66 s, so sánh 87 s. Lần 17/11 (10 GB): copy 150–307 MB/s, verify 47–74 MB/s. Copy và verify đọc **toàn bộ dung lượng logical**, kể cả phần trống, nên thời gian theo cỡ volume, không theo dữ liệu thật |

### Bước 9. Tạo port và VM đích (`create_destination`)

| | |
|---|---|
| Tool làm gì | |
| • port | Với từng port: `port create vm-a-198.51.100.50 --network <cùng tên> --mac-address <MAC nguồn> --fixed-ip subnet=<id>,ip-address=198.51.100.50 --security-group <id>…`. Log `destination port <tên> <ip> <mac>` |
| • VM | Rồi `server create vm-a --flavor <cùng tên> --port <id> --volume <dst-vda> --block-device uuid=<dst-vdb>,source_type=volume,destination_type=volume,device_name=vdb,boot_index=-1 --user-data <file> [--key-name] [--property k=v…] [--description …]`. User-data chỉ là cloud-config tối thiểu (`ssh_deletekeys: false`, `preserve_hostname: true`), không dùng lại user-data nguồn |
| • chờ | Chờ ACTIVE (tối đa 600 s), log `destination VM ACTIVE; control-plane downtime so far Ns` |
| State | `dst-ports` (ghi từng port ngay khi tạo), `user-data.yaml`, `dst-server-id` |
| VM nguồn | SHUTOFF + locked |
| Fail thì | VM vào ERROR hoặc quá hạn thì die. Rollback xóa VM đích, port đích, volume đích, rồi trả nguồn |
| Thời lượng (v1) | Tạo port + VM ≈ 22 s. Guest boot tới khi ghi lại được ≈ 24 s |

### Bước 10. Hoàn tất

| | |
|---|---|
| Tool làm gì | Tắt trap, in `Migrated. Source VM: kept SHUTOFF and locked. Rollback: <đường dẫn>/migrate-vm.sh rollback vm-a` |
| State | Giữ nguyên thư mục state (cần cho rollback) |
| VM nguồn | SHUTOFF + locked, không NIC. Volume gốc nguyên vẹn |
| Downtime | Hết khi guest ở đích lên mạng. Tổng 293 s (v1) / 276,8 s (12/11) trên lab |

## 2. Live: `migrate-vm.sh migrate vm-a --mode live`

11 bước. Khác cold ở chỗ: tạo volume đích và **pre-sync lặp nhiều lượt khi VM vẫn chạy** (bước 4–5).
Cutover chỉ sync phần delta. Downtime bắt đầu ở bước 6.

Số đo lấy từ [TEST-REPORT-20261112.md](TEST-REPORT-20261112.md), mục "Live mode: số đo". VM 10 + 2 GB,
guest ghi đè ngẫu nhiên 4 MB + fsync mỗi giây vào volume data. Downtime **250,5 s** (cold cùng ngày
276,8 s), tổng wall time 687 s.

**Điều kiện riêng của live** (preflight kiểm):

- VM phải boot-from-volume. Root ephemeral thì `FAIL --mode live needs boot-from-volume`.
- `blocksync.py` có mặt (`blocksync present`).
- Python **hệ thống** có `python3-rbd` (`librbd python bindings`).

### Bước 1–3. Như cold

Khác duy nhất: dòng tiêu đề preflight ghi `mode=live`, và có thêm 2 kiểm tra blocksync/librbd ở trên.

### Bước 4. Tạo volume đích khi VM còn chạy

Giống cold bước 7 (`create_dest_volume`, `volume-map` ghi ngay khi có ID), nhưng làm **trước** khi
stop. VM nguồn ACTIVE. Fail thì rollback xóa volume đích; VM nguồn vẫn chạy suốt.

Bước 4 và 5 lặp **theo từng volume**: tạo `vm-a-vda` rồi pre-sync `vda` tới khi hội tụ, xong mới tạo
và pre-sync `vm-a-vdb`.

### Bước 5. Pre-sync lặp (`sync_volume_pass`), VM vẫn ACTIVE

| | |
|---|---|
| Tool làm gì | **Mỗi lượt** của mỗi volume: |
| | 1. clone mới `mig-clone-<src-id>` (crash-consistent tại thời điểm clone) → `brick_attach.py attach`; |
| | 2. `/usr/bin/python3 blocksync.py --device /dev/sdX --pool <pool> --image volume-<dst-id> --id <RBD_USER> --conf <CEPH_CONF> --state …/hash-<src-id>.bin [--first] --verify readback`; |
| | 3. detach, xóa clone. |
| Cách blocksync chạy | Đọc toàn bộ device theo chunk 4 MiB, hash blake2b-128. Lượt đầu (`--first`) ghi mọi chunk khác 0. Các lượt sau chỉ ghi chunk có hash đổi. Mỗi chunk ghi xong được đọc lại để so (readback); lệch thì blocksync thoát mã 3 và tool die `blocksync failed for <src-id> (pass N)` |
| Khi nào dừng lặp | Từ lượt 2 trở đi, delta (byte đã ghi + số chunk thành 0 × 4 MiB) ≤ 64 MB thì log `<src-id> converged (delta N MB <= 64 MB)`. Đủ 5 lượt chưa hội tụ thì log `WARN <src-id> did not converge after 5 passes (last delta N MB); cutover will carry it` **và vẫn cutover** |
| Log | Mỗi lượt: `pass N <src-id>: X/Y chunks changed, Z MB written, Ts`. blocksync in `[blocksync] NN% (…), changed N MiB` ra stderr khoảng mỗi 5% |
| State | `hash-<src-id>.bin` (cache hash), `sync-<src-id>.log` (JSON từng lượt) |
| VM nguồn | **ACTIVE, phục vụ bình thường** |
| Fail thì | Die. VM nguồn vẫn chạy. Rollback xóa volume đích và clone sót |
| Thời lượng (lab) | Root 10 GB: lượt 1 ghi 2 268 MB / 130 s, lượt 2 còn 44 MB → hội tụ. Data 2 GB bị ghi liên tục: lượt 1 ghi 740 MB / 34 s, lượt 2–5 dao động 104–156 MB, **không hội tụ**, tool cảnh báo rồi vẫn cutover |

Mỗi lượt đều tạo một clone mới. Trên backend clone chậm (LVM thick, SAN không clone nhanh), clone là
copy đầy đủ, nên mỗi lượt tốn thêm đúng phần đó.

### Bước 6–8. Stop, tháo port, lock: như cold bước 4–6

Downtime bắt đầu từ đây.

### Bước 9. Sync delta lần cuối

| | |
|---|---|
| Tool làm gì | Mỗi volume: clone mới (lúc này VM đã tắt, nên clone nhất quán) → attach → blocksync **không** `--first`, có readback → log `pass final <src-id>: …` → dọn clone. Chỉ khi `--verify full` mới thêm `qemu-img compare` toàn bộ |
| State | `sync-<src-id>.log` thêm một dòng |
| VM nguồn | SHUTOFF + locked |
| Fail thì | Die. Rollback như cold |
| Thời lượng (lab) | Root 104 MB / 60 s, data 140 MB / 16 s. Thời gian chủ yếu là **đọc lại toàn bộ** device để tính hash (~200 MB/s), nên lượt cuối vẫn tỷ lệ với cỡ volume; chỉ phần ghi + verify co lại theo delta |

### Bước 10–11. Tạo port/VM đích và hoàn tất: như cold bước 9–10

Downtime lab là 250,5 s.

### Lưu ý về live

- **Ghi càng nhiều, live càng giống cold.** Tốc độ ghi của guest lớn thì delta cuối lớn, nên downtime
  tiến dần về cold. Lab chỉ thử với 4 MB/s.
- Live chỉ lợi rõ khi volume lớn mà ít ghi. Lý do: cold phải copy + compare toàn bộ trong lúc VM tắt,
  còn live chỉ mất một lượt đọc hash lúc cutover.

## 3. Sau khi xong

**Trạng thái chuẩn:**

| Phía | Trạng thái |
|---|---|
| Nguồn | VM `vm-a` SHUTOFF + locked, không NIC (port tháo ra vẫn giữ IP/MAC). Volume gốc nguyên vẹn |
| Đích | VM `vm-a` ACTIVE, port `vm-a-198.51.100.50` cùng MAC/IP, volume `vm-a-vda`, `vm-a-vdb` (description `migrated from <src-id>`) |
| Host chuyển | `/var/lib/mig-migrate/vm-a/` còn nguyên (cần cho rollback). Không còn clone hay attach nào |

**Nghiệm thu:**

- Vào guest ở đích: dịch vụ chạy, dữ liệu đúng. Nên **checksum dữ liệu trong guest**. Bắt buộc phải
  làm khi đã bỏ compare (`--verify none`).
- Với SAN thật: `qemu-img compare` chỉ so device đã đọc với RBD, nên **không** bắt được trường hợp
  device là LUN khác cùng cỡ. Phải đối chiếu WWN của device với volume trên mảng; xem
  [DEPLOY.md](DEPLOY.md) mục 9 và [POWERSTORE-VS-LAB.md](POWERSTORE-VS-LAB.md). WWN của clone đang gắn
  nằm ở `/var/lib/mig-attach/<clone-id>.json` (trường `scsi_wwn`) và file này **bị xóa khi detach**,
  nên phải xem trong lúc copy.

**Rollback** `migrate-vm.sh rollback vm-a`. Nó chạy lại được nhiều lần, mỗi bước bỏ qua thứ đã dọn.

1. Phía đích, **xóa**: VM đích, port đích, volume đích trong `volume-map`, image Glance (đường
   ephemeral).
2. Phía nguồn, chỉ dọn **đồ tạm của chính tool**: clone `mig-clone-<src-id>` còn sót, snapshot
   `mig-snap-vm-a`.
3. Phía nguồn, **trả lại nguyên trạng**: unlock, gắn lại đúng các port đã tháo, start, chờ ACTIVE.
4. Thư mục state đổi tên thành `vm-a.rolled-back.<epoch>`.

Dữ liệu ghi ở VM đích sau khi migrate **mất theo** volume đích. VM nguồn chạy lại với dữ liệu tại lúc
cutover.

**Khi nào xóa nguồn:** khi đã nghiệm thu xong và chắc chắn không rollback. Tool không có lệnh xóa
nguồn; xóa tay server, volume, port ở nguồn. Xóa rồi thì rollback không còn tác dụng. Chạy với
`--keep-source-ports` thì nên xóa sớm (rủi ro split-brain, xem GUIDE mục 2).

## 4. Các đường rẽ khỏi mặc định

| Muốn | Xem |
|---|---|
| IP + MAC mới, hoặc IP chỉ định (`--ip new\|<addr>`), bật lại nguồn (`--start-source`) | [GUIDE.md](GUIDE.md) mục 2, bảng option |
| Không tháo port nguồn (`--keep-source-ports`) | [GUIDE.md](GUIDE.md) mục 2 |
| Chỉ copy volume, không tạo VM đích (`--volumes-only`) | [GUIDE.md](GUIDE.md) 2b.5 |
| VM root ephemeral: tool **tự rẽ** sang Glance snapshot, chỉ cold | [GUIDE.md](GUIDE.md) mục 2, đoạn "VM không dùng volume" |
| Server group: có sẵn (`--server-group`) hoặc giữ group nguồn (`--keep-server-group`, v4) | [GUIDE.md](GUIDE.md) 2b.5; [DESIGN-V4.md](DESIGN-V4.md) §2 |
| AZ + volume type theo conf (`MIG_AZ`, `AZ_TYPE_MAP`, v4) | [GUIDE.md](GUIDE.md) 2b.1b |
| Một volume rời, không có VM (`migrate-volume`); volume đang gắn, copy nóng (`--in-use`, v4) | [GUIDE.md](GUIDE.md) 2b.4; [DESIGN-V4.md](DESIGN-V4.md) §1 |
| Nhiều VM một lượt (`migrate-batch.sh`) | [GUIDE.md](GUIDE.md) 2b.6 |
