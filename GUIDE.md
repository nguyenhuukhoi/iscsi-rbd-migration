# Chuyển VM boot-from-volume từ OpenStack iSCSI sang OpenStack Ceph RBD

Bản v1 (cold, giữ IP/MAC) chạy thật ngày 2026-11-05: [TEST-REPORT-20261105.md](TEST-REPORT-20261105.md).
Bản v2 (thêm live mode, chọn volume type, chọn IP) thiết kế ở [DESIGN-V2.md](DESIGN-V2.md), test ngày
2026-11-12: [TEST-REPORT-20261112.md](TEST-REPORT-20261112.md). IP trong tài liệu là IP ví dụ
(dải TEST-NET 192.0.2.x / 198.51.100.x); IP thật ghi ở tài liệu nội bộ ngoài repo.
Bản v3 (file cấu hình, chạy theo project, đặt tên theo instance, `--server-group`, `--volumes-only`,
lệnh `migrate-volume`, chạy lô bằng `migrate-batch.sh`) thiết kế ở [DESIGN-V3.md](DESIGN-V3.md), test
ngày 2026-11-17/18: [TEST-REPORT-20261117.md](TEST-REPORT-20261117.md); hướng dẫn ở mục 2b.
Triển khai lên host convert production (từ git, step-by-step): [DEPLOY.md](DEPLOY.md).

## 1. Cách làm

Sơ đồ workflow đầy đủ (cold + live + 3 chế độ IP + rollback): mở [workflow-v2.html](workflow-v2.html).
Bản dọc tối giản, mã màu theo mode: [workflow-v2-simple.html](workflow-v2-simple.html).
So sánh riêng Cold vs Live (khoảng nào VM còn chạy, khoảng nào downtime): [cold-vs-live.html](cold-vs-live.html).
Dạng chữ:

```
  Bước 0  Lệnh migrate (--mode cold|live · --ip same|new|addr · --volume-type · --flavor · --security-group
          · v3: --server-group · --volumes-only)
  Bước 1  Preflight đích: flavor, network, SG, IP/MAC trống, volume type. FAIL = dừng, chưa tạo gì
  Bước 2  Tạo volume đích <vm>-<vda|vdb…> (--volume-type → đúng pool RBD qua RBD_POOL_MAP)
  Bước 3  CHỈ --mode live, VM vẫn chạy:
            3a clone volume nguồn (crash-consistent) → 3b blocksync (hash 4 MiB, chỉ ghi delta)
            lặp 3a↔3b tới khi delta ≤ --delta-limit hoặc đủ --max-passes
          (--mode cold: bỏ qua bước 3, đi thẳng bước 4)

  ── CUTOVER, downtime bắt đầu ──
  Bước 4  Tắt + lock VM nguồn (cả cold lẫn live)
  Bước 5  Port nguồn: --ip same → tháo, giữ chỗ IP/MAC · --ip new/addr → giữ nguyên
  Bước 6  Copy cuối + verify: cold = qemu-img copy + compare · live = sync delta + readback
          root ephemeral (không volume) = snapshot Glance → tải về → đẩy lên đích
  Bước 7  Tạo port <vm>-<ip> + VM đích: MAC/IP theo --ip, flavor/SG theo override,
          copy server properties + description, user-data tối thiểu giữ SSH host key + hostname
          (--volumes-only: bỏ bước 7, không tạo port/VM đích)
  Bước 8  Hoàn tất — nguồn giữ SHUTOFF + lock (8b: --start-source bật lại nguồn, chỉ ip new/addr)

  Rollback (chạy được ở bất kỳ bước nào):
  R1 lệnh rollback → R2 dọn đích (VM, volume, port, image, clone sót)
  → R3 trả nguồn (gắn lại port nếu đã tháo, unlock, start) → nguồn ACTIVE lại.
  Volume gốc không bao giờ bị sửa hay xóa.
```

Chế độ mặc định là **cold migration**: VM phải tắt trong lúc copy. Host chuyển có 2 chân mạng, một vào iSCSI, một vào Ceph.

```
Cloud nguồn (LVM/iSCSI)                  Host chuyển                 Cloud đích (Ceph RBD)
  VM nguồn ── tắt, lock, tháo port
  volume gốc ── clone ── iSCSI ──> /dev/sdX ── qemu-img ── RBD ──> volume mới (cùng cỡ)
                                                                   port mới (cùng MAC, IP)
                                                                   VM mới boot từ volume
```

| Bước | Lệnh chính | Vì sao |
|---|---|---|
| 1. Tắt và lock VM nguồn | `server stop`, `server lock` | Dữ liệu nhất quán |
| 2. Tháo port khỏi VM nguồn | `server remove port` | Chống 2 VM cùng IP/MAC. Port vẫn giữ IP ở nguồn |
| 3. Clone volume | `volume create --source` | Cinder không cho gắn thêm volume `in-use` |
| 4. Gắn clone vào host chuyển | `brick_attach.py` (Attachment API + os-brick) | Cùng đường với Nova, dùng được với mọi backend iSCSI |
| 5. Tạo volume đích, copy | `qemu-img convert -n --target-is-zero` | Giữ sparse |
| 6. So sánh từng byte | `qemu-img compare` | Sai là dừng, chưa tạo VM đích |
| 7. Tạo port và VM đích | `port create --mac-address --fixed-ip`, `server create --volume` | Cùng MAC, IP, flavor, security group |

Volume gốc không bị sửa hay xóa, nên rollback chỉ cần gắn lại port và bật VM nguồn.

## 2. Dùng công cụ

Chạy bằng `root` trên host chuyển.

```bash
migrate-vm.sh plan     <vm> [options]  # chỉ đọc: xuất cấu hình nguồn, preflight theo đúng options
migrate-vm.sh migrate  <vm> [options]  # chuyển thật
migrate-vm.sh rollback <vm>            # xóa phía đích, trả VM về nguồn
# v3:
migrate-vm.sh migrate-volume <volume> [--volume-type t] [--az z] [--verify v]
                                       # 1 volume available, nguồn không bị đụng (mục 2b.4)
migrate-vm.sh rollback <volume>        # xóa bản copy của volume đó ở đích
migrate-batch.sh <selector…> [--volumes …] [--plan-only] [--yes] [-- <options>]
                                       # chạy lô theo host/project/danh sách VM (mục 2b.6)
```

Options (mặc định = hành vi v1: cold, giữ IP/MAC, volume type mặc định):

| Option | Ý nghĩa |
|---|---|
| `--mode cold\|live` | `cold`: tắt VM rồi copy (qemu-img, verify từng byte). `live`: VM chạy trong lúc pre-sync nhiều lượt bằng `blocksync.py` (hash-cache từng chunk 4 MiB); lúc cutover chỉ tắt VM và sync phần delta, nên downtime theo lượng ghi chứ không theo cỡ volume |
| `--volume-type <type>` | Volume type ở đích cho mọi volume. Type nằm pool khác thì khai `RBD_POOL_MAP` |
| `--ip same\|new\|<addr>` | `same`: giữ IP + MAC, tháo port nguồn lúc cutover. `new`: đích tự cấp IP + MAC mới, **không đụng port nguồn**. `<addr>`: IP chỉ định, MAC mới (VM 1 NIC) |
| `--flavor <tên>` | Flavor cho VM đích (mặc định: cùng tên với nguồn, phải tồn tại ở đích) |
| `--security-group <sg1,sg2>` | Security group cho **mọi** port đích, thay bộ SG của nguồn |
| `--start-source` | Bật lại VM nguồn sau khi chuyển xong (chỉ với `--ip new\|<addr>`; 2 VM chạy song song, trùng hostname + SSH host key, dữ liệu rẽ nhánh từ cutover) |
| `--keep-source-ports` | Với `--ip same`: KHÔNG tháo port nguồn. **Rủi ro**: chỉ còn SHUTOFF+lock giữ VM nguồn khỏi mạng — ai bật lại (admin bypass được lock) là 2 máy cùng MAC+IP trên VLAN (split-brain). Đổi lại: tool không đụng port ở nguồn → phía nguồn chạy được bằng creds member thuần; rollback không cần cắm lại port. Nghiệm thu xong nên xóa VM nguồn sớm |
| `--az <zone>` | AZ đích cho **cả volume lẫn server** (cloud có backend theo AZ / `cross_az_attach=false`). Preflight kiểm AZ tồn tại ở cả compute lẫn volume service. Mặc định: để scheduler đích tự chọn. **Cloud 2 AZ lưu ý**: `RBD_POOL_MAP` map theo *volume-type* chứ không theo AZ — mỗi lần chạy phải truyền bộ khớp nhau `--az AZ1 --volume-type <type-của-AZ1>` (+ pool/`CEPH_CONF` đúng cụm nếu mỗi AZ một cụm Ceph); truyền lệch thì tool dừng an toàn trước khi copy nhưng thông báo nói về type mapping. User Ceph chuyển cần caps trên pool của mọi AZ sẽ migrate vào |
| `--max-passes <n>` | live: tối đa n lượt pre-sync (mặc định 5) |
| `--delta-limit <MB>` | live: cutover khi delta một lượt ≤ ngưỡng (mặc định 64) |
| `--verify readback\|full\|none` | live: `readback` (mặc định) đọc lại từng chunk vừa ghi; `full` thêm `qemu-img compare` toàn bộ trong cutover; `none` không kiểm. Cold: compare toàn bộ trừ khi `none` (compare đọc lại cả 2 phía, 1 luồng — thường lâu bằng/hơn copy; bỏ thì nên checksum trong guest sau khi VM đích boot) |
| `--server-group <tên\|ID>` | (v3) VM đích vào server group đó (`--hint group=<id>`). Group phải có sẵn trong project đích, tool không tự tạo; thiếu thì preflight FAIL kèm lệnh tạo gợi ý (mục 2b.5) |
| `--volumes-only` | (v3) Chỉ cold. Copy volume, **không tạo port/VM đích**; nguồn vẫn stop + lock, port nguồn để nguyên (mục 2b.5) |

VM nguồn **luôn phải tắt lúc cutover**, kể cả live mode — hai cloud độc lập không live-migrate kiểu
Nova được; "live" nghĩa là phần copy lớn chạy khi VM còn sống.

### Ví dụ lệnh (chạy `root` trên mig-host, VM tên `demo-vm`)

```bash
# 0. Xem trước, không đổi gì: cấu hình nguồn + preflight đích theo đúng option định dùng
migrate-vm.sh plan demo-vm
migrate-vm.sh plan demo-vm --mode live --ip new --volume-type mig-typeb

# 1. Cold, giữ nguyên IP + MAC (mặc định — đúng hành vi v1)
migrate-vm.sh migrate demo-vm

# 2. Live: VM chạy trong lúc pre-sync, chỉ tắt lúc cutover
migrate-vm.sh migrate demo-vm --mode live

# 3. Live chỉnh ngưỡng: tối đa 8 lượt, cutover khi delta ≤ 32 MB, thêm compare toàn bộ
migrate-vm.sh migrate demo-vm --mode live --max-passes 8 --delta-limit 32 --verify full

# 4. Cold, IP + MAC mới từ pool đích, volume type chỉ định, bật lại VM nguồn sau khi xong
migrate-vm.sh migrate demo-vm --ip new --volume-type mig-typeb --start-source

# 4b. Đổi flavor + security group ngay lúc chuyển (VM nhiều NIC vẫn giữ đủ port)
migrate-vm.sh migrate demo-vm --flavor mig.small2 --security-group mig-sg-b

# 4c. VM không có volume (root ephemeral) — tự đi đường Glance, chỉ cold
migrate-vm.sh migrate demo-eph

# 4d. Chọn AZ đích cho cả volume lẫn server (cloud 2 AZ, cross_az_attach=false)
migrate-vm.sh migrate demo-vm --az AZ1 --volume-type nvme-az1

# 4e. Không tháo port nguồn (chấp nhận rủi ro split-brain nếu ai bật lại nguồn;
#     đổi lại phía nguồn chạy được creds member thuần)
migrate-vm.sh migrate demo-vm --keep-source-ports

# 4f. Cold bỏ bước compare (nhanh gần gấp đôi; nên checksum trong guest sau khi đích boot)
migrate-vm.sh migrate demo-vm --verify none

# 5. Live, IP chỉ định (VM 1 NIC), MAC mới
migrate-vm.sh migrate demo-vm --mode live --ip 192.168.120.150

# 6. Hối hận: xóa sạch phía đích, trả VM về nguồn (tự hiểu option đã dùng từ state)
migrate-vm.sh rollback demo-vm

# 7. Bộ production điển hình: appcred 2 cloud, đích 2 AZ (type/pool khớp AZ),
#    host convert 1 path, không tháo port nguồn, bỏ compare
#    (tổ hợp 2 NIC + 2 volume + 3 option này đã test nguyên lượt trên lab 17/11)
SRC_RC=/opt/mig/src/appcred-openrc.sh DST_RC=/opt/mig/dst/appcred-openrc.sh \
RBD_POOL=volumes-nvme RBD_POOL_MAP="nvme-az1:volumes-nvme-az1,nvme-az2:volumes-nvme-az2" \
RBD_USER=mig-xfer MIG_ISCSI_IP=<ip-iscsi-của-host-convert> \
migrate-vm.sh migrate app-vm01 --mode cold --az AZ1 --volume-type nvme-az1 \
  --keep-source-ports --verify none
```

Lab (chạy từ workstation, đứng ở `Lab/`):

```bash
bash iscsi-rbd-migration/lab.sh status      # lab còn sống không, demo-vm đang ở đâu
bash iscsi-rbd-migration/lab.sh demo-reset  # đưa demo-vm về ACTIVE ở nguồn
bash iscsi-rbd-migration/lab.sh up          # dựng lab từ đầu (replay, có bước thủ công)
```

Biến môi trường để trỏ sang hệ thống khác:

| Biến | Mặc định trong lab | Ý nghĩa |
|---|---|---|
| `SRC_RC`, `DST_RC` | `/opt/mig/{src,dst}/admin-openrc.sh` | Openrc 2 cloud |
| `RBD_POOL`, `RBD_USER` | `mig-volumes`, `mig-xfer` | Pool Cinder đích mặc định, user Ceph chỉ có quyền trên pool đó |
| `RBD_POOL_MAP` | (trống) | `typeA:poolA,typeB:poolB` — pool cho từng volume type |
| `CEPH_CONF` | `/etc/ceph/ceph.conf` | Cấu hình Ceph |
| `MIG_ISCSI_IP` | `10.77.0.20` | IP host chuyển trên mạng iSCSI |
| `MIG_MULTIPATH` | `0` | `1`: attach qua multipath (SAN nhiều portal kiểu PowerStore) — cần `multipathd` chạy; brick từ chối device không phải dm và so kích thước device với volume (chặn LUN lạ khác cỡ) |
| `COPY_STREAMS` | `8` | Số luồng `qemu-img` (cold) |
| `SRC_PROJECT`, `DST_PROJECT` | (trống) | (v3) Chạy phía nguồn/đích trong project này của account openrc — cần password openrc (mục 2b.2) |
| `PROJECT_MAP_FILE` | (trống) | (v3) File map project nguồn → đích, chỉ `migrate-batch.sh` đọc (mục 2b.6) |
| `MIG_CONF` | `/opt/mig/migrate.conf` | (v3) File cấu hình chứa các biến trên; env thắng file (mục 2b.1) |

Live mode cần thêm `blocksync.py` trong `/usr/local/bin` và `python3-rbd` trên host chuyển.

**Điều kiện phía đích, preflight sẽ kiểm tra:** flavor, security group cùng tên (resolve trong đúng project đích); network cùng tên, subnet cùng CIDR; MAC và IP chưa bị dùng. Từ v3 keypair thiếu ở đích **không còn FAIL**: preflight báo `INFO`, VM đích tạo không có `key_name` (Nova không cho gán keypair của user khác; key vẫn nằm trong đĩa nên SSH vẫn vào được) — add lại sau nếu cần.

**Preflight từ chối:** volume mã hóa, volume multiattach, port nhiều fixed IP, port có allowed-address-pairs, và `--mode live` cho VM không boot từ volume.

**VM không dùng volume (root ephemeral):** đi đường Glance — snapshot VM đã tắt → tải về host chuyển → đẩy lên Glance đích bằng REST (openstackclient lỗi với upload dài) → so sha512 với hash Glance tính → VM đích boot từ image đó. Chỉ cold. Server properties + description luôn được copy sang VM đích; MAC/IP + port của VM nhiều NIC (kể cả khác network) được giữ từng port một.

Khi chạy thật ổn định và không cần rollback nữa, tự xóa bằng tay ở nguồn: server, volume, port. Công cụ cố ý không có lệnh này.

## 2b. Bản v3: file cấu hình, project, đặt tên, chạy lô

Mọi option và cách chạy ở mục 2 giữ nguyên hành vi. Không có `migrate.conf`, không đặt
`SRC_PROJECT`/`DST_PROJECT` thì tool chạy y như v2. Có 2 thay đổi hành vi: tên volume/port ở đích
(mục 2b.3), và keypair thiếu ở đích không còn làm preflight FAIL (mục 2).

### 2b.1. File cấu hình `/opt/mig/migrate.conf`

Dùng thay cho chuỗi `export`. Đây là file bash dạng `KEY=VALUE`, tool `source` nó lúc khởi động. Đổi
đường dẫn bằng env `MIG_CONF=<file>`.

**Thứ tự ưu tiên: option CLI > biến môi trường > migrate.conf > mặc định trong script.** Biến đã có
trong env được giữ lại sau khi source file. Vì vậy cách chạy cũ bằng `export` không đổi hành vi, và
muốn đè conf cho một lần chạy thì chỉ cần đặt env trước lệnh.

Biến đọc từ file (ý nghĩa xem bảng biến môi trường ở mục 2): `SRC_RC DST_RC RBD_POOL RBD_POOL_MAP
RBD_USER CEPH_CONF MIG_ISCSI_IP MIG_MULTIPATH COPY_STREAMS PROJECT_MAP_FILE SRC_PROJECT DST_PROJECT`.
`migrate-batch.sh` cũng đọc file này, nhưng chỉ lấy `SRC_RC`, `DST_RC`, `PROJECT_MAP_FILE`. Các biến còn
lại do từng lần gọi `migrate-vm.sh` tự đọc.

```bash
# /opt/mig/migrate.conf — bash KEY=VALUE, không cần export
SRC_RC=/opt/mig/src/admin-openrc.sh
DST_RC=/opt/mig/dst/admin-openrc.sh
RBD_POOL=volumes-nvme
RBD_POOL_MAP="nvme-az1:volumes-nvme-az1,nvme-az2:volumes-nvme-az2"
RBD_USER=mig-xfer
CEPH_CONF=/etc/ceph/ceph.conf
MIG_ISCSI_IP=192.0.2.20
MIG_MULTIPATH=0
COPY_STREAMS=8
PROJECT_MAP_FILE=/opt/mig/project-map.conf
# SRC_PROJECT / DST_PROJECT: thường không ghi ở đây, đặt theo từng lần chạy (batch tự đặt cho từng VM)
```

```bash
migrate-vm.sh plan app-vm01                                    # mọi giá trị lấy từ conf
COPY_STREAMS=4 migrate-vm.sh migrate app-vm01                  # env đè conf cho riêng lần này
MIG_CONF=/opt/mig/migrate-az2.conf migrate-vm.sh plan app-vm01 # dùng bộ conf khác
```

### 2b.2. Chạy trong project của VM: `SRC_PROJECT` / `DST_PROJECT`

- Mỗi lần source openrc, tool đè `OS_PROJECT_NAME=<SRC_PROJECT|DST_PROJECT>` và bỏ `OS_PROJECT_ID`.
  Nhờ vậy clone tạm, volume, port, server đều nằm trong đúng project đó, quota và ownership tính đúng chỗ.
  Project nhận theo tên, cùng domain với openrc. Không đặt 2 biến này thì tool không scope gì thêm.
- Account: **mỗi cloud một account có role trong mọi project liên quan**, openrc dạng **password**.
  Batch liệt kê VM bằng `--all-projects` nên account nguồn cần quyền admin. `--ip same` trên provider
  network cũng cần admin ở đích.
- Application credential bị khóa vào đúng 1 project, không rescope được. Tool dừng trước khi đụng vào gì:

```
ERROR: SRC_PROJECT/DST_PROJECT needs a password openrc: an application credential is bound to one project (/opt/mig/dst/appcred-openrc.sh)
```

`migrate-batch.sh` luôn chạy mỗi VM trong project của nó, nên từ chối appcred ngay từ đầu:

```
ERROR: /opt/mig/src/appcred-openrc.sh is an application credential: a batch runs each VM in its own project and needs a password openrc
```

- Migrate lẻ không đặt `SRC_PROJECT`/`DST_PROJECT` thì appcred vẫn dùng được như v2.
- Rollback chạy với **đúng cặp `SRC_PROJECT`/`DST_PROJECT` đã dùng lúc migrate**. Lý do: clone sót và
  image `mig-snap-*` được tìm theo tên, mà tìm theo tên chỉ thấy trong project của token. Với VM
  chạy qua batch, batch in sẵn lệnh rollback có đủ 2 biến này (mục 2b.6).

### 2b.3. Quy tắc đặt tên ở đích (luôn bật, không có flag)

| Đối tượng ở đích | Tên | Ví dụ |
|---|---|---|
| Volume của VM (migrate thường, `--volumes-only`, batch `--volumes attached`) | `<instance>-<device>` | `app-vm01-vda`, `app-vm01-vdb` |
| Port, `--ip same` | `<instance>-<ip nguồn>` | `app-vm01-198.51.100.50` |
| Port, `--ip <addr>` | `<instance>-<addr>` | `app-vm01-198.51.100.150` |
| Port, `--ip new` | Tạo port tên `<instance>-pending<i>`, Neutron cấp IP xong thì đổi tên thành `<instance>-<ip mới>` | `app-vm01-pending0` → `app-vm01-198.51.100.77` |
| Volume rời (`migrate-volume`, batch `--volumes available`) | **Giữ nguyên tên nguồn**, kể cả tên rỗng | `data-backup-01` → `data-backup-01` |

- `<instance>` là tên thật của VM nguồn, kể cả khi chạy bằng ID. `<device>` là phần đuôi của device
  attach (`vda`, `vdb`, `sda`…). VM nhiều NIC thì mỗi port mang IP của chính nó.
- Mọi volume đích mang description `migrated from <id-volume-nguồn>` để biết nguồn gốc. Với volume rời,
  description này còn là guard chống copy trùng: tên volume Cinder không unique, nên tool không tra theo tên.
- Volume rời không có tên mà Cinder đích không nhận tên rỗng: tool đặt tên bằng ID nguồn và log `WARN`.
- Rollback xóa theo ID ghi trong state, không theo tên. Đổi tên không ảnh hưởng rollback, và state cũ
  của v2 (port `mig-<vm>-<i>`) vẫn được dọn. Port giữ chỗ phía nguồn vẫn tên `mig-reserve-<vm>-<i>` như v2.
- Guard trùng ở đích, đều chặn ở preflight:

| Lệnh | Bị chặn khi |
|---|---|
| Migrate thường | Đích đã có server cùng tên |
| `--volumes-only` | Đích đã có volume tên `<instance>-<device>` (lỗi `rename or delete it first`) |
| `migrate-volume` | Đích đã có volume mang description `migrated from <id>` (lỗi kèm gợi ý rollback hoặc xóa) |

### 2b.4. Volume rời: `migrate-vm.sh migrate-volume <volume>`

Dùng cho volume **available**, tức không gắn vào VM nào:

```bash
migrate-vm.sh migrate-volume data-backup-01                          # theo tên, trong project nguồn
migrate-vm.sh migrate-volume <volume-id> --volume-type nvme-az1 --az AZ1 --verify none
migrate-vm.sh rollback <volume-id>                                   # xóa bản copy ở đích
```

- Luồng: clone volume nguồn (`mig-clone-<id>`) → attach clone vào host chuyển → tạo volume đích (giữ
  tên nguồn, description `migrated from <id>`, chép cờ bootable và image metadata `hw_*`/`os_*`) →
  `qemu-img convert` → `qemu-img compare` (trừ khi `--verify none`) → xóa clone.
  **Volume gốc không bị đụng, vẫn `available`, không phải dừng thứ gì.**
- Chỉ nhận `--volume-type`, `--az`, `--verify`. Option dành cho server (`--ip`, `--flavor`,
  `--security-group`, `--server-group`, `--start-source`, `--keep-source-ports`, `--volumes-only`,
  `--mode live`) bị từ chối.
- Volume `in-use` bị từ chối, kèm gợi ý cách đúng là `migrate-vm.sh migrate <vm> --volumes-only`. Mọi
  trạng thái khác `available` cũng bị từ chối.
- Có nhiều volume cùng tên trong project nguồn: tool die và liệt kê ID, chạy lại bằng ID.
- Không có lệnh `plan` riêng. Preflight chạy ngay đầu lệnh: AZ, volume type, volume không mã hóa, đích
  chưa có bản copy, quyền RBD, `iscsid`. FAIL thì dừng trước khi tạo gì.
- State nằm ở `/var/lib/mig-migrate/vol-<id-nguồn>/`. `migrate-vm.sh rollback <tên|ID>` xóa volume đích
  và clone sót, không bao giờ sửa nguồn. Nếu có state VM trùng tên với volume thì state VM được ưu tiên,
  khi đó rollback volume bằng **ID**.
- Volume không thuộc project của openrc: đặt `SRC_PROJECT`/`DST_PROJECT` (batch tự đặt).
- Giới hạn: snapshot của volume không đi theo, chỉ copy dữ liệu hiện tại. Cây snapshot ở lại nguồn.

### 2b.5. `--server-group` và `--volumes-only`

**`--server-group <tên|ID>`** (dùng với `plan`, `migrate`):

- VM đích tham gia server group đó (`server create --hint group=<id>`). Group phải **có sẵn trong
  project đích**, tool không bao giờ tự tạo.
- Preflight tìm thấy group thì báo `OK` và in policy. Có nhiều group cùng tên thì FAIL, khi đó truyền
  ID. Không thấy thì FAIL kèm lệnh tạo:

```
  FAIL  server group web-anti not found in the destination project — create it first: openstack server group create --policy anti-affinity web-anti
```

- Anti-affinity mà không đủ host thì Nova báo NoValidHost lúc tạo VM. Preflight không đoán trước được
  scheduler, nên trường hợp này phải rollback.
- Dùng chung với `--volumes-only` thì bị bỏ qua (preflight báo `INFO`), vì không có server nào được tạo.

**`--volumes-only`** (dùng với `plan`, `migrate`; chỉ cold):

- Copy mọi volume của VM sang đích với tên `<instance>-<device>`. **Không tạo port, không tạo server.**
- Nguồn vẫn bị stop + lock như migrate thường, nhưng **port nguồn không bị tháo**, vì không có port đích
  nào dùng MAC/IP đó. Xong lệnh, nguồn ở trạng thái SHUTOFF + locked. `--start-source` dùng được với mọi
  `--ip`. Bật lại nguồn thì dữ liệu nguồn bắt đầu khác bản copy.
- Preflight bỏ qua check network, flavor, SG, keypair, MAC/IP, và thêm check tên `<instance>-<device>`
  chưa có ở đích.
- Bị từ chối: `--mode live`, và VM có root ephemeral (lỗi `ephemeral VM has no volume to migrate`).
- Không mang theo: port/IP, metadata server, server group.
- `migrate-vm.sh rollback <vm>` xóa volume đích, unlock và start lại nguồn.

### 2b.6. Chạy lô: `migrate-batch.sh`

Script bọc ngoài `migrate-vm.sh`: chọn VM, chạy `plan` cho tất cả, hỏi xác nhận một lần, rồi migrate
**lần lượt từng con, theo ID**. Mỗi con chạy với `SRC_PROJECT`/`DST_PROJECT` của project chứa nó.

```
migrate-batch.sh [--host h1[,h2...]] [--project p1[,p2...]] [--vm v1[,v2...]] [--vm-file <file>]
                 [--volumes attached|available|all] [--plan-only] [--yes] [--continue-on-error]
                 [-- <options passed to every migrate-vm.sh call>]
```

Chạy không có tham số thì script in phần usage này.

**Chọn VM (selector):**

| Selector | Ý nghĩa |
|---|---|
| `--host h1[,h2…]` | VM trên các compute host đó. Tên host lấy ở cột **Host** của `openstack compute service list` (cũng là `OS-EXT-SRV-ATTR:host` trong `server show`). **Không phải** `hypervisor_hostname` của `openstack hypervisor list`, thường là FQDN. Sai tên thì batch log `WARN no VM on host …` |
| `--project p1[,p2…]` | VM của các project đó, nhận tên hoặc ID |
| `--vm v1[,v2…]` | VM theo tên hoặc ID |
| `--vm-file <file>` | Mỗi dòng một tên hoặc ID. `#` là comment, dòng trống bị bỏ qua, khoảng trắng đầu/cuối được cắt. Gộp chung với `--vm`. Dòng không ra VM nào thành `SKIP(not-found)`, batch không chết |

- Dùng nhiều selector thì lấy **giao**: `--host` ∩ `--project` ∩ `--vm/--vm-file`. Phải có ít nhất 1 selector.
- Nhiều VM cùng tên trong 1 project thì thành `SKIP(ambiguous-name: use the ID)`. Cùng tên nhưng ở các
  project khác nhau thì cả hai đều được chọn, mỗi con chạy trong project của nó.

**Phạm vi volume (`--volumes`):**

| Giá trị | Làm gì | Điều kiện |
|---|---|---|
| (không có) | Migrate VM đầy đủ: volume + port + VM ở đích | — |
| `attached` | Mỗi VM chạy `migrate-vm.sh migrate <id> --volumes-only`: copy volume, không tạo VM đích, VM nguồn bị stop + lock. Tên `<instance>-<device>` | Cần `--project` hoặc `--vm/--vm-file`. `--host` chỉ để thu hẹp |
| `available` | Mọi volume `available` của project chạy `migrate-vm.sh migrate-volume <id>`: giữ tên nguồn, nguồn không bị đụng | Cần `--project`. Không dùng với `--host`, `--vm`, `--vm-file` |
| `all` | `attached` trước, `available` sau | Cần `--project`. Không dùng với `--host` |

- `--volumes` đi với `--mode live` thì bị từ chối, vì copy volume chỉ chạy cold.
- Volume `in-use` không nằm trong danh sách `available`. Batch log số lượng của chúng: các volume này đi
  theo VM (`attached`). Clone tạm sót lại (`mig-clone-*`) thành `SKIP(mig-clone)`.

**Điều khiển:**

| Option | Ý nghĩa |
|---|---|
| `--plan-only` | Liệt kê và plan tất cả, không đổi gì. Exit 1 nếu có `PLAN-FAIL` |
| `--yes` | Không hỏi xác nhận. Bắt buộc khi stdin không phải terminal (cron, pipe…), nếu thiếu thì batch die |
| `--continue-on-error` | Chạy tiếp sau con FAIL. Mặc định dừng ở con FAIL đầu tiên, các con còn lại thành `SKIP(not-run)` |
| `-- <options>` | Mọi thứ sau `--` được truyền nguyên vẹn cho **mọi** lần gọi `migrate-vm.sh plan`/`migrate` |

**Luồng chạy:**

1. Liệt kê, in bảng `Selected:` gồm các cột KIND / ID / NAME / STATUS / PROJECT src -> dst / RESULT.
2. Chạy `plan` cho mọi VM, in bảng `Plan:` cùng dòng FAIL của từng con `PLAN-FAIL`. `--plan-only` dừng ở đây.
3. Hỏi `Type yes to continue` (bỏ qua nếu có `--yes`).
4. Migrate tuần tự từng con, theo ID.
5. In bảng `Summary:` và dòng `Totals: OK n  FAIL n  SKIP n`. Exit 1 nếu có FAIL.

Lưu ý khi chạy:

- Volume rời (`available`) không có bước plan. Bảng ghi `READY`, preflight chạy lúc `migrate-volume`.
- Với volume rời, batch chỉ truyền `--volume-type`, `--az`, `--verify`, `--mode`. Option chỉ dành cho VM
  bị bỏ qua, có log dòng `available volumes: ignoring VM-only options …`.
- Option sau `--` áp cho **mọi** VM. `--flavor`, `--security-group`, `--server-group` thì mọi VM nhận
  cùng giá trị. `--ip <addr>` chỉ hợp lý khi batch có đúng 1 VM.
- Batch chạy và ghi state theo **ID** (`/var/lib/mig-migrate/<id>/`), nên rollback cũng phải bằng ID.
  Rollback bằng tên không tìm thấy state đó.

**Project đích và `PROJECT_MAP_FILE`:**

- Mặc định project đích **cùng tên** với project nguồn. Project đó phải có sẵn ở đích, và account phải
  có role trong đó.
- Cặp project khác tên thì khai trong một file riêng, trỏ tới bằng `PROJECT_MAP_FILE` (trong conf hoặc env).
  Mỗi dòng có dạng `project-nguồn project-đích`, **phân cách bằng khoảng trắng**, `#` là comment.
  - Project không có dòng nào thì giữ nguyên tên.
  - File không đọc được: batch log `WARN`, mọi project giữ nguyên tên.
  - Dòng sai dạng (thiếu hoặc thừa cột, kể cả kiểu `a = b`) thì batch die.
- Chỉ `migrate-batch.sh` đọc file map. Chạy lẻ `migrate-vm.sh` thì tự đặt `DST_PROJECT`.

```
# /opt/mig/project-map.conf
finance        finance-prod
web-legacy     web
```

**Kết quả (cột RESULT):**

| RESULT | Nghĩa |
|---|---|
| `SKIP(not-found)` | Dòng `--vm`/`--vm-file` không resolve được VM nào |
| `SKIP(ambiguous-name: use the ID)` | Có từ 2 VM cùng tên trong 1 project |
| `SKIP(state-exists)` | Đã có state từ lần chạy trước (`/var/lib/mig-migrate/<id hoặc tên>/` hoặc `vol-<id>/`). Batch in lệnh rollback |
| `SKIP(status)` | VM không ở ACTIVE/SHUTOFF, hoặc volume ở trạng thái khác `available`/`in-use` (error, reserved…) |
| `SKIP(ephemeral-root)` | Chỉ khi copy volume (`--volumes …` hoặc `-- --volumes-only`): VM có root ephemeral, không có volume để copy |
| `SKIP(mig-clone)` | Volume là clone tạm `mig-clone-*` còn sót |
| `SKIP(not-run)` | Không chạy vì batch đã dừng ở con FAIL trước đó |
| `PLAN-OK` / `PLAN-FAIL` | Kết quả `plan` |
| `READY` | Volume rời đang chờ migrate |
| `OK` / `OK(no key_name: <key>)` | Xong. Dạng thứ hai nghĩa là keypair không có ở đích nên VM đích được tạo không có `key_name`; add lại sau |
| `FAIL` / `FAIL(plan)` | Migrate lỗi / tới lượt một con `PLAN-FAIL` |

Với dòng `FAIL` và `SKIP(state-exists)`, batch in sẵn lệnh hoàn tác, có đúng cặp project:

```
  app-vm01: inspect, then to undo: SRC_PROJECT=proj-a DST_PROJECT=proj-a /usr/local/bin/migrate-vm.sh rollback <id>
```

Log của mỗi lần chạy nằm ở `/var/log/mig-batch/<YYYYmmdd-HHMMSS>/`: `plan-<id>.log` và
`migrate-<id>.log` (output đầy đủ của từng con), cùng các file JSON danh sách lúc liệt kê.

Chưa test trên lab (TEST-REPORT-20261117): `PROJECT_MAP_FILE` có cặp khác tên thật,
`--continue-on-error` gặp FAIL giữa chừng thật, và `--volumes all`.

### 2b.7. Ví dụ v3

```bash
# 1. Theo host: xem trước (không đổi gì), rồi chạy thật
migrate-batch.sh --host cmp-01 --plan-only
migrate-batch.sh --host cmp-01 --yes

# 2. Host + project: chỉ VM của proj-a, proj-b nằm trên cmp-01, cmp-02
migrate-batch.sh --host cmp-01,cmp-02 --project proj-a,proj-b --plan-only
migrate-batch.sh --host cmp-01,cmp-02 --project proj-a,proj-b --yes

# 3. Danh sách VM từ file
cat > /root/wave1.txt <<'EOF'
# đợt 1
app-vm01
app-vm02
11111111-2222-3333-4444-555555555555   # VM trùng tên: ghi bằng ID
EOF
migrate-batch.sh --vm-file /root/wave1.txt --plan-only
migrate-batch.sh --vm-file /root/wave1.txt --yes

# 4a. Volume rời của project: giữ tên nguồn, nguồn không bị đụng, không VM nào bị dừng
migrate-batch.sh --project proj-a --volumes available --yes
# 4b. So với volume đang gắn VM: copy theo từng VM, VM nguồn bị stop + lock, tên <vm>-<vdX>,
#     không tạo VM đích
migrate-batch.sh --project proj-a --volumes attached --yes -- --verify none

# 5. Chỉ copy volume của vài VM chỉ định (tương đương --vm … --volumes attached)
migrate-batch.sh --vm app-vm01,app-vm02 --yes -- --volumes-only

# 6. Một account cho nhiều project, chạy lẻ (password openrc; project đích khác tên)
SRC_PROJECT=proj-a DST_PROJECT=proj-a-new migrate-vm.sh plan app-vm01 --server-group web-anti
SRC_PROJECT=proj-a DST_PROJECT=proj-a-new migrate-vm.sh migrate app-vm01 --server-group web-anti
SRC_PROJECT=proj-a DST_PROJECT=proj-a-new migrate-vm.sh rollback app-vm01   # nếu cần hoàn tác

# 7. Batch truyền option xuống từng migrate-vm.sh: cold, không tháo port nguồn, bỏ compare,
#    AZ + volume type khớp nhau (rủi ro của --keep-source-ports: xem bảng option mục 2)
migrate-batch.sh --host cmp-01 --project proj-a --yes \
  -- --mode cold --keep-source-ports --verify none --az AZ1 --volume-type nvme-az1
```

### 2b.8. Bug đã sửa: security group trùng tên giữa các project

- Triệu chứng (bắt được khi test batch 18/11): VM thuộc project khác bị preflight báo
  `FAIL security group default`, dù project đó có SG `default`. Nguyên nhân: Neutron cho admin thấy SG
  `default` của **mọi** project, kể cả qua token đã scope, nên tra theo tên bị mơ hồ.
- Đã sửa: tên SG được resolve thành ID **trong đúng project của token** (`security group list --project <id>`).
  - **Phía đích**: không tìm thấy thì preflight FAIL. Có từ 2 group cùng tên trong project thì FAIL,
    khi đó truyền ID cho `--security-group` (option này nhận cả tên lẫn ID).
  - **Phía nguồn**: việc tạo lại port giữ chỗ khi Nova tự xóa port của nó diễn ra giữa cutover. Nếu
    SG không resolve được thì tool chỉ **log WARN và bỏ SG đó, không bao giờ die**, vì việc của port
    giữ chỗ là giữ IP/MAC. Log in sẵn lệnh để add lại SG sau rollback:

```
WARN reserve port <port-id> has no security group default; after a rollback re-add them: openstack port set --security-group default <port-id>
```

## 3. Quy hoạch network cho 2 cloud chung VLAN

1. Tạo network provider cùng VLAN và CIDR ở cả 2 cloud.
2. **Allocation pool không được chồng nhau.** Mỗi cloud có port metadata OVN chiếm 1 IP trong pool (`.10` ở nguồn, `.100` ở đích), và VM mới sẽ lấy IP từ pool.
3. IP của VM chuyển sang nằm ngoài pool đích vẫn được, vì công cụ chỉ định IP tường minh bằng quyền admin.
4. Không cần tắt DHCP ở cloud nào. Trong lab, VM ở mỗi cloud nhận DHCP đúng từ cloud của nó, và VM ở 2 cloud gọi được nhau trong suốt giai đoạn chuyển dần.

## 4. Những bẫy phải biết

| Bẫy | Hậu quả nếu làm sai | Cách đúng |
|---|---|---|
| Chỉ `server lock` VM nguồn | Admin vẫn bật được. Đo được 12 ARP reply cho 6 request, SSH nhảy qua lại giữa 2 VM | Tháo port |
| Disable port nguồn | Cloud nguồn nuốt gói tới MAC đó, VM còn ở nguồn mất liên lạc với VM đã chuyển | Tháo port, không disable |
| Truyền lại user-data gốc | Cloud-init thấy instance-id mới và chạy lại, `runcmd` có `mkfs` sẽ xóa dữ liệu | Chỉ truyền cloud-config tối thiểu |
| Không truyền user-data gì | Cloud-init đổi SSH host key | `ssh_deletekeys: false`, `preserve_hostname: true` |
| Port do Nova tạo (`--network`) | Bị xóa khi tháo, mất giữ chỗ IP | Công cụ tự tạo lại ngay với cùng MAC và IP |
| Attachment `mode="ro"` | tgtadm không ép, kernel vẫn báo `ro=0` | `blockdev --setro` trên host chuyển |
| CLI `cinder local-attach` | Lỗi `'FileFinder' object has no attribute 'find_module'` trên Python 3.12 | Dùng `brick_attach.py` |
| Project nguồn có nhiều VM **trùng tên** | Nova cho phép trùng tên; tool die ngay từ đầu, liệt kê các ID | Chạy lại bằng **ID** (plan/migrate/rollback đều nhận). VM đích vẫn mang tên thật; nhớ rollback bằng đúng đối số đã migrate |

## 5. Dựng lại lab

Từ workstation có thể dùng `lab.sh` (đứng ở `Lab/` hoặc gọi thẳng):

```bash
bash iscsi-rbd-migration/lab.sh status      # 6 VM lab, API 2 cloud, Ceph, vị trí demo-vm
bash iscsi-rbd-migration/lab.sh up          # dựng theo thứ tự dưới, dừng ở các bước thủ công
bash iscsi-rbd-migration/lab.sh demo-reset  # đưa demo-vm về ACTIVE ở nguồn (rollback nếu có state)
```

IP thật ghi ở tài liệu nội bộ ngoài repo. Từ workstation, đứng ở `Lab/`, trừ khi ghi khác.

> **Chưa chạy lại nguyên lượt từ VM trắng.** Các bước này được ghi lại từ lần dựng thật, trong đó nhiều lỗi được sửa giữa chừng. Riêng `02-mighost-setup.sh` đã được gộp thêm bước cài Ceph Tentacle sau khi chạy, bước này lúc đó làm tay.

### 5.1. VM cloud ngoài

```bash
scp -i key.txt iscsi-rbd-migration/{01-outer-infra.sh,user-data.tmpl} ubuntu@198.51.100.79:mig-lab/
ssh -i key.txt ubuntu@198.51.100.79 'sudo bash ~/mig-lab/01-outer-infra.sh'
```

Cần trống 20 core và 40 GB RAM quota, 6 IP trong pool `vlan2019`.

> Neutron cloud ngoài không có `trunk`, nên VLAN lồng đi qua network `--transparent-vlan`. Network này vẫn cần subnet hình thức vì Nova từ chối port không có IP.

### 5.2. Host chuyển kiêm deploy node

Deploy node đặt riêng trên `mig-host`, không dùng `.79`. Lý do: quên `--configdir` trên `.79` thì lệnh sẽ chạy vào cluster chính.

```bash
# Dùng đúng source Kolla đã kiểm chứng trên .79 (commit 8ca92bc8f)
ssh -i key.txt ubuntu@198.51.100.79 'sudo tar -C /opt/src-kolla-ansible-stable-2026.1-20261013 -czf - .' \
  | ssh -i key.txt root@192.0.2.109 'mkdir -p /opt/kolla-ansible-src && tar -C /opt/kolla-ansible-src -xzf -'
scp -i key.txt iscsi-rbd-migration/02-mighost-setup.sh root@192.0.2.109:/root/
ssh -i key.txt root@192.0.2.109 'bash /root/02-mighost-setup.sh'
```

Trên `mig-host`:

- Tạo SSH key riêng (`ssh-keygen -t ed25519`), đưa public key vào `/root/.ssh/authorized_keys` của 5 node.
- Đặt CA Harbor vào `/etc/docker/certs.d/198.51.100.109/ca.crt` trên 5 node, lấy từ `/etc/docker/certs.d/` của node compute có sẵn.
- Tạo wrapper `/usr/local/bin/ka` để mỗi lệnh luôn gắn với đúng một cloud:

```bash
#!/usr/bin/env bash
set -euo pipefail
c=$1; shift
case $c in src|dst) ;; *) echo "cloud must be src or dst" >&2; exit 2;; esac
export PATH=/opt/kolla-venv/bin:$PATH
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
exec kolla-ansible "$@" -i /opt/mig/$c/hosts --configdir /opt/mig/$c
```

### 5.3. Cấu hình Kolla và Ceph

```bash
scp -r -i key.txt iscsi-rbd-migration/{kolla,03-kolla-config.sh} root@192.0.2.109:/root/mig-lab/
ssh -i key.txt root@192.0.2.109 'bash /root/mig-lab/03-kolla-config.sh'
bash iscsi-rbd-migration/04-ceph-prep.sh    # pool, user, chuyển keyring thẳng từ ceph1 sang mig-host
```

> Chạy lại `03` không đụng `passwords.yml`, `ceph.conf` và keyring. Client Ceph trên `mig-host` phải là Tentacle (do `02` cài) thì bước kiểm tra cuối của `04` mới qua.

### 5.4. Bootstrap, image, deploy

```bash
# trên mig-host
ka src bootstrap-servers; ka dst bootstrap-servers
```

Sau đó xử lý image, vì Harbor chỉ có bộ image cho RBD:

| Node | Việc |
|---|---|
| `.104`, `.106` | Thêm proxy vào `docker.service.d` và `containerd.service.d` rồi restart. Docker 29 pull qua containerd, thiếu một trong hai là timeout |
| `.104`, `.106` | `docker pull quay.io/openstack.kolla/iscsid:2026.1-ubuntu-noble` |
| `.106` | `docker pull quay.io/openstack.kolla/tgtd:2026.1-ubuntu-noble` |
| `.107` | `bash 05-patch-ceph-images.sh cinder-volume` |
| `.108` | `bash 05-patch-ceph-images.sh nova-compute nova-libvirt` |

Trên `.106`: `pvcreate /dev/vdb && vgcreate cinder-volumes /dev/vdb`.
Trên `.104`, `.106`: `systemctl disable --now iscsid.service open-iscsi.service`, vì `iscsid` đã chạy trong container.

```bash
# trên mig-host
for c in src dst; do ka $c prechecks && ka $c deploy && ka $c post-deploy; done
bash /root/mig-lab/06-prepare-clouds.sh      # network vlan120, flavor, image, VM mẫu
```

> **Đừng chạy `ka dst pull`**: lệnh này đè image đã vá client Ceph bằng bản trên Harbor.

Cấu hình bắt buộc cho iSCSI trên mạng riêng, đã có sẵn trong `kolla/` và `03-kolla-config.sh`:

| Mục | Giá trị | Không có thì |
|---|---|---|
| `tgtd_interface_address` | `10.77.0.12` | `tgtd` chỉ listen trên IP API, VM kẹt `BUILD` khi login iSCSI |
| `[lvm-1] target_ip_address` | `10.77.0.12` | Cinder quảng bá portal trên mạng API |
| `network_vlan_ranges` | `physnet1` | Không tạo được provider VLAN (Kolla để trống) |
| Bỏ tab trong `ceph.conf` | | Task `Copying over multiple ceph.conf` fail: `Unexpected continuation line` |

### 5.5. Cài công cụ và test

Cách chuẩn (mọi host, kể cả production — chi tiết: [DEPLOY.md](DEPLOY.md)):

```bash
git clone https://github.com/nguyenhuukhoi/iscsi-rbd-migration.git
cd iscsi-rbd-migration
install -m 0755 migrate-vm.sh migrate-batch.sh brick_attach.py blocksync.py /usr/local/bin/
```

Hoặc đẩy từ workstation vào lab như lần dựng gốc:

```bash
scp -i key.txt iscsi-rbd-migration/{migrate-vm.sh,migrate-batch.sh,brick_attach.py,blocksync.py} root@192.0.2.109:/usr/local/bin/
scp -i key.txt iscsi-rbd-migration/tests/*.sh root@192.0.2.109:/root/mig-lab/tests/
# trên mig-host: live mode cần librbd bindings
apt-get install -y python3-rbd
# với demo-vm đang ở nguồn; mỗi test tự rollback về nguồn khi xong, trừ failure-injection
bash /root/mig-lab/tests/blocksync-unit.sh      # không đụng VM
bash /root/mig-lab/tests/e2e-migrate.sh         # cold, giữ IP/MAC
bash /root/mig-lab/tests/live-migrate.sh        # --mode live, đo downtime
bash /root/mig-lab/tests/newip-voltype.sh       # --ip new --volume-type --start-source
bash /root/mig-lab/tests/extras.sh              # multi-NIC 2 network, --flavor/--security-group,
                                                # server properties, VM ephemeral (tạo demo-eph rồi tự xóa)
bash /root/mig-lab/tests/powerstore-sim.sh      # multipath 2 portal LIO: mất đường giữa lúc đọc,
                                                # read-only, detach sạch (không đụng VM)
MIG_MULTIPATH=1 bash /root/mig-lab/tests/e2e-migrate.sh   # e2e cold qua multipath (nguồn LIO)
bash /root/mig-lab/tests/e2e-nova-port.sh
bash /root/mig-lab/tests/failure-injection.sh   # kết thúc với demo-vm ở đích
```

## 6. Nguồn tham khảo

- [Kolla-Ansible: Cinder guide 2026.1](https://docs.openstack.org/kolla-ansible/2026.1/reference/storage/cinder-guide.html)
- [python-brick-cinderclient-ext](https://docs.openstack.org/python-brick-cinderclient-ext/latest/readme.html)
- [OS-Migrate (Red Hat)](https://www.redhat.com/en/blog/migrating-openstack-platform-13-openstack-platform-16-os-migrate): công cụ cùng hướng, cũng clone rồi copy, nhưng cần máy conversion ở cả 2 cloud thay vì 1 host chung
- [OSSN-0108](https://docs.openstack.org/security-notes/OSSN-0108.html), [Rook: cephx key rotation](https://rook.io/docs/rook/v1.20/Storage-Configuration/Advanced/cephx-key-rotation/): key `aes256k`, client tối thiểu 19.2.6 / 20.2.4
