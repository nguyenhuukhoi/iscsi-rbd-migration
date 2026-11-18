# Chuyển VM boot-from-volume từ OpenStack iSCSI sang OpenStack Ceph RBD

Bản v1 (cold, giữ IP/MAC) chạy thật ngày 2026-11-05: [TEST-REPORT-20261105.md](TEST-REPORT-20261105.md).
Bản v2 (thêm live mode, chọn volume type, chọn IP) thiết kế ở [DESIGN-V2.md](DESIGN-V2.md), test ngày
2026-11-12: [TEST-REPORT-20261112.md](TEST-REPORT-20261112.md). IP trong tài liệu là IP ví dụ
(dải TEST-NET 192.0.2.x / 198.51.100.x); IP thật ghi ở tài liệu nội bộ ngoài repo.
Bản v3 (file cấu hình, chạy theo project, đặt tên theo instance, `--server-group`, `--volumes-only`,
lệnh `migrate-volume`, chạy lô bằng `migrate-batch.sh`) thiết kế ở [DESIGN-V3.md](DESIGN-V3.md), test
ngày 2026-11-17/18: [TEST-REPORT-20261117.md](TEST-REPORT-20261117.md); hướng dẫn ở mục 2b.
Bản v4 (`migrate-volume --in-use`, `--keep-server-group`, `MIG_AZ`/`AZ_TYPE_MAP`, `MIG_DEFAULT_OPTS`)
thiết kế ở [DESIGN-V4.md](DESIGN-V4.md), test ngày 2026-11-18: [TEST-REPORT-20261118.md](TEST-REPORT-20261118.md);
hướng dẫn ở mục 2b.1, 2b.1b, 2b.9–2b.11. Bản v5 (`--flavor-fallback` + `FLAVOR_MAP_FILE`: đích thiếu
flavor cùng tên thì tra file map) thiết kế ở [DESIGN-V5.md](DESIGN-V5.md), test ngày 2026-11-18:
[TEST-REPORT-20261118-v5.md](TEST-REPORT-20261118-v5.md); hướng dẫn ở mục 2b.12. Lịch sử các bản:
[CHANGELOG.md](CHANGELOG.md).
Triển khai lên host convert production (từ git, step-by-step): [DEPLOY.md](DEPLOY.md).

## 1. Cách làm

Sơ đồ workflow đầy đủ (cold + live + 3 chế độ IP + rollback): mở [workflow-v2.html](workflow-v2.html).
Bản dọc tối giản, mã màu theo mode: [workflow-v2-simple.html](workflow-v2-simple.html).
So sánh riêng Cold vs Live (khoảng nào VM còn chạy, khoảng nào downtime): [cold-vs-live.html](cold-vs-live.html).
Chi tiết từng bước với option mặc định (lệnh thật, state, fail thì sao, thời lượng lab): [WORKFLOW.md](WORKFLOW.md).
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
migrate-vm.sh migrate-volume <volume> [--volume-type t] [--az z] [--verify v] [--in-use]
                                       # 1 volume available, nguồn không bị đụng (mục 2b.4)
                                       # v4 --in-use: volume đang gắn, copy nóng crash-consistent (mục 2b.9)
migrate-vm.sh rollback <volume>        # xóa bản copy của volume đó ở đích
migrate-batch.sh <selector…> [--volumes …] [--plan-only] [--yes] [-- <options>]
                                       # chạy lô theo host/project/danh sách VM (mục 2b.6)
```

Options (mặc định = hành vi v1: cold, giữ IP/MAC, volume type mặc định):

| Option | Ý nghĩa |
|---|---|
| `--mode cold\|live` | `cold`: tắt VM rồi copy (qemu-img, verify từng byte). `live`: VM chạy trong lúc pre-sync nhiều lượt bằng `blocksync.py` (hash-cache từng chunk 4 MiB); lúc cutover chỉ tắt VM và sync phần delta, nên downtime theo lượng ghi chứ không theo cỡ volume |
| `--volume-type <type>` | Volume type ở đích cho mọi volume. Type nằm pool khác thì khai `RBD_POOL_MAP`. (v4) Không truyền thì lấy theo AZ từ `AZ_TYPE_MAP` nếu có (mục 2b.1b) |
| `--ip same\|new\|<addr>` | `same`: giữ IP + MAC, tháo port nguồn lúc cutover. `new`: đích tự cấp IP + MAC mới, **không đụng port nguồn**. `<addr>`: IP chỉ định, MAC mới (VM 1 NIC) |
| `--flavor <tên>` | Flavor cho VM đích (mặc định: cùng tên với nguồn, phải tồn tại ở đích) |
| `--flavor-fallback` | (v5) Đích **không có** flavor cùng tên với nguồn thì dùng flavor mà `FLAVOR_MAP_FILE` map tới. Có cùng tên thì vẫn dùng như cũ; `--flavor` gõ tay luôn thắng; tool không bao giờ tạo flavor (mục 2b.12) |
| `--security-group <sg1,sg2>` | Security group cho **mọi** port đích, thay bộ SG của nguồn |
| `--start-source` | Bật lại VM nguồn sau khi chuyển xong (chỉ với `--ip new\|<addr>`; 2 VM chạy song song, trùng hostname + SSH host key, dữ liệu rẽ nhánh từ cutover) |
| `--keep-source-ports` | Với `--ip same`: KHÔNG tháo port nguồn. **Rủi ro**: chỉ còn SHUTOFF+lock giữ VM nguồn khỏi mạng — ai bật lại (admin bypass được lock) là 2 máy cùng MAC+IP trên VLAN (split-brain). Đổi lại: tool không đụng port ở nguồn → phía nguồn chạy được bằng creds member thuần; rollback không cần cắm lại port. Nghiệm thu xong nên xóa VM nguồn sớm |
| `--az <zone>` | AZ đích cho **cả volume lẫn server** (cloud có backend theo AZ / `cross_az_attach=false`). Preflight kiểm AZ tồn tại ở cả compute lẫn volume service. Mặc định: `MIG_AZ` (v4) nếu có, không thì để scheduler đích tự chọn. **Cloud 2 AZ lưu ý**: `RBD_POOL_MAP` map theo *volume-type* chứ không theo AZ — mỗi lần chạy phải truyền bộ khớp nhau `--az AZ1 --volume-type <type-của-AZ1>` (v4: khai `AZ_TYPE_MAP` thì chỉ cần `--az`, mục 2b.1b) (+ pool/`CEPH_CONF` đúng cụm nếu mỗi AZ một cụm Ceph); truyền lệch thì tool dừng an toàn trước khi copy nhưng thông báo nói về type mapping. User Ceph chuyển cần caps trên pool của mọi AZ sẽ migrate vào |
| `--max-passes <n>` | live: tối đa n lượt pre-sync (mặc định 5) |
| `--delta-limit <MB>` | live: cutover khi delta một lượt ≤ ngưỡng (mặc định 64) |
| `--verify readback\|full\|none` | live: `readback` (mặc định) đọc lại từng chunk vừa ghi; `full` thêm `qemu-img compare` toàn bộ trong cutover; `none` không kiểm. Cold: compare toàn bộ trừ khi `none` (compare đọc lại cả 2 phía, 1 luồng — thường lâu bằng/hơn copy; bỏ thì nên checksum trong guest sau khi VM đích boot) |
| `--server-group <tên\|ID>` | (v3) VM đích vào server group đó (`--hint group=<id>`). Group phải có sẵn trong project đích, tool không tự tạo; thiếu thì preflight FAIL kèm lệnh tạo gợi ý (mục 2b.5) |
| `--keep-server-group` | (v4) VM đích vào server group `az-<tên group nguồn>` ở project đích, cùng policy. Chưa có thì `migrate` tạo, `plan` không tạo; rollback không xóa group. Không dùng chung với `--server-group` (mục 2b.10) |
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
| `RBD_POOL_MAP` | (trống) | `typeA:poolA,typeB:poolB` — pool cho từng volume type. Vì sao phải khai, cách tra ngược type → pool: mục 2b.1b |
| `MIG_AZ` | (trống) | (v4) AZ mặc định khi không truyền `--az`; truyền `--az` thì CLI thắng (mục 2b.1b) |
| `AZ_TYPE_MAP` | (trống) | (v4) `AZ1:typeA,AZ2:typeB` — volume type theo AZ khi không truyền `--volume-type`; `--volume-type` tay luôn thắng (mục 2b.1b) |
| `MIG_DEFAULT_OPTS` | (trống) | (v4) Option mặc định, vd `"--keep-server-group --verify none"`, parse trước option CLI nên CLI thắng; `MIG_DEFAULT_OPTS=""` tắt cho một lần chạy. Quy tắc đủ: [migrate.conf.example](migrate.conf.example) |
| `FLAVOR_MAP_FILE` | (trống) | (v5) File map `flavor-nguồn flavor-đích`, chỉ dùng khi có `--flavor-fallback`. Không có đường dẫn mặc định; bật flag mà biến này rỗng thì tool dừng (mục 2b.12) |
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

## 2b. Bản v3 → v5: file cấu hình, project, đặt tên, chạy lô, copy nóng, server group, flavor map

Mọi option và cách chạy ở mục 2 giữ nguyên hành vi. Không có `migrate.conf`, không đặt
`SRC_PROJECT`/`DST_PROJECT` thì tool chạy y như v2. Có 2 thay đổi hành vi: tên volume/port ở đích
(mục 2b.3), và keypair thiếu ở đích không còn làm preflight FAIL (mục 2).

v4 chỉ **thêm**, mọi thứ đều opt-in. Không truyền flag mới, không đặt biến mới thì build v4 chạy y như
v3: regression gate 140/0 hai vòng, xem [TEST-REPORT-20261118.md](TEST-REPORT-20261118.md). v5 cũng
vậy: không có `--flavor-fallback` thì chạy y như v4, kể cả khi `FLAVOR_MAP_FILE` có khai.

### 2b.1. File cấu hình `/opt/mig/migrate.conf`

Dùng thay cho chuỗi `export`. Đây là file bash dạng `KEY=VALUE`, tool `source` nó lúc khởi động. Đổi
đường dẫn bằng env `MIG_CONF=<file>`.

**Thứ tự ưu tiên: option CLI > biến môi trường > migrate.conf > mặc định trong script.** Biến đã có
trong env được giữ lại sau khi source file. Vì vậy cách chạy cũ bằng `export` không đổi hành vi, và
muốn đè conf cho một lần chạy thì chỉ cần đặt env trước lệnh.

Biến đọc từ file (ý nghĩa xem bảng biến môi trường ở mục 2): `SRC_RC DST_RC RBD_POOL RBD_POOL_MAP
RBD_USER CEPH_CONF MIG_ISCSI_IP MIG_MULTIPATH COPY_STREAMS PROJECT_MAP_FILE SRC_PROJECT DST_PROJECT`,
từ v4 thêm `MIG_AZ AZ_TYPE_MAP` (mục 2b.1b) và `MIG_DEFAULT_OPTS`, từ v5 thêm `FLAVOR_MAP_FILE`
(mục 2b.12). Mẫu đủ mọi biến, mỗi biến có mô tả:
[migrate.conf.example](migrate.conf.example) (`install -m 600 migrate.conf.example /opt/mig/migrate.conf`).
`migrate-batch.sh` cũng đọc file này, nhưng chỉ lấy `SRC_RC`, `DST_RC`, `PROJECT_MAP_FILE`. Các biến còn
lại do từng lần gọi `migrate-vm.sh` tự đọc.

```bash
# /opt/mig/migrate.conf — bash KEY=VALUE, không cần export
SRC_RC=/opt/mig/src/admin-openrc.sh
DST_RC=/opt/mig/dst/admin-openrc.sh
RBD_POOL=volumes-nvme
RBD_POOL_MAP="nvme-az1:volumes-nvme-az1,nvme-az2:volumes-nvme-az2"
MIG_AZ=AZ1                               # v4: AZ khi không truyền --az
AZ_TYPE_MAP="AZ1:nvme-az1,AZ2:nvme-az2"  # v4: volume type theo AZ khi không truyền --volume-type
MIG_DEFAULT_OPTS="--verify none"         # v4: option luôn áp, CLI thắng; MIG_DEFAULT_OPTS="" tắt
RBD_USER=mig-xfer
CEPH_CONF=/etc/ceph/ceph.conf
MIG_ISCSI_IP=192.0.2.20
MIG_MULTIPATH=0
COPY_STREAMS=8
PROJECT_MAP_FILE=/opt/mig/project-map.conf
FLAVOR_MAP_FILE=/opt/mig/flavor-map.conf  # v5: chỉ dùng khi có --flavor-fallback (mục 2b.12)
# SRC_PROJECT / DST_PROJECT: thường không ghi ở đây, đặt theo từng lần chạy (batch tự đặt cho từng VM)
```

```bash
migrate-vm.sh plan app-vm01                                    # mọi giá trị lấy từ conf
COPY_STREAMS=4 migrate-vm.sh migrate app-vm01                  # env đè conf cho riêng lần này
MIG_CONF=/opt/mig/migrate-az2.conf migrate-vm.sh plan app-vm01 # dùng bộ conf khác
```

**`MIG_DEFAULT_OPTS` (v4): option "lần nào cũng phải nhớ truyền" đưa vào conf.**

- Chuỗi option được parse **trước** option gõ trên CLI của `plan`/`migrate`/`migrate-volume`. Vì vậy với
  option có giá trị, CLI luôn thắng: conf có `--verify none`, gõ `--verify full` thì `full` thắng.
- Preflight in 1 dòng nói rõ đã áp những gì:
  `INFO  MIG_DEFAULT_OPTS applied before the command line: --keep-server-group, --verify none (command line wins)`.
- Option dạng cờ (`--keep-server-group`, `--keep-source-ports`…) không tắt được từ CLI. Một lần chạy không
  muốn dùng default nào thì đặt env rỗng: `MIG_DEFAULT_OPTS="" migrate-vm.sh …`.
- Tách theo khoảng trắng, nên giá trị chứa khoảng trắng không dùng được ở đây.
- Gõ sai tên option hoặc thiếu giá trị thì tool dừng ngay, thông báo nêu `MIG_DEFAULT_OPTS`.
- `migrate-volume` chỉ lấy `--volume-type`, `--az`, `--verify`; option khác bị bỏ kèm 1 dòng INFO.
- `--in-use` **không bao giờ** được lấy từ đây.
- `rollback` không dùng biến này.
- Đủ quy tắc (xung đột với `--server-group`, thứ tự so với `MIG_AZ`/`AZ_TYPE_MAP`) ghi ở
  [migrate.conf.example](migrate.conf.example). Ví dụ kết hợp: mục 2b.11.

### 2b.1b. Chuỗi AZ → volume type → pool: `AZ_TYPE_MAP` và `RBD_POOL_MAP`

Tool ghi dữ liệu thẳng vào Ceph đích, nên với mỗi volume nó phải biết 3 thứ khớp nhau: AZ, volume
type, pool RBD. Hai map nối chúng thành một chuỗi:

```
AZ ──AZ_TYPE_MAP──► volume type ──RBD_POOL_MAP──► pool RBD
AZ1                 nvme-az1                       volumes-nvme-az1
```

Hai map trả lời hai câu hỏi khác nhau:

| Map | Trả lời câu | Bản chất | Không khai thì |
|---|---|---|---|
| `AZ_TYPE_MAP` (v4) | "AZ này dùng type nào?" | Quy ước **của người vận hành**, thuần cho tiện tay | Vẫn chạy: gõ `--volume-type` tay mỗi lần (hoặc dùng type mặc định của cloud đích) |
| `RBD_POOL_MAP` | "Cinder đổ type này vào pool nào?" | **Bản chép sự thật của cloud đích** | Type không có trong map thì tool ghi vào `RBD_POOL`. Dùng type không nằm ở `RBD_POOL` thì **bắt buộc** khai đúng |

**Vì sao phải khai `RBD_POOL_MAP` khi Cinder đã cấu hình type → pool rồi?** Vì tool không hỏi Cinder
được:

- Quan hệ type → pool nằm trong `cinder.conf` trên controller đích (dòng `rbd_pool` của từng backend).
  Không có API nào trả về tên pool.
- `openstack volume show` (admin) chỉ cho `os-vol-host-attr:host` dạng
  `controller@rbd-nvme-az1#rbd-nvme-az1`. Đó là **tên backend**, không phải tên pool Ceph.
- Tool không ghi qua Cinder. Nó ghi thẳng vào Ceph (`qemu-img convert … rbd:<pool>/volume-<id>`, live
  thì `blocksync.py`) bằng keyring riêng (`RBD_USER`), nên phải biết đúng tên pool.

**Ví dụ: chép một lần từ `cinder.conf` sang map**

```ini
# cinder.conf cloud đích (đã cấu hình sẵn)
[rbd-nvme-az1]
volume_backend_name = rbd-nvme-az1
rbd_pool = volumes-nvme-az1
[rbd-nvme-az2]
volume_backend_name = rbd-nvme-az2
rbd_pool = volumes-nvme-az2
```

```bash
# /opt/mig/migrate.conf
MIG_AZ=AZ1                                                          # v4: AZ khi không truyền --az
AZ_TYPE_MAP="AZ1:nvme-az1,AZ2:nvme-az2"                             # v4: quy ước của mình
RBD_POOL_MAP="nvme-az1:volumes-nvme-az1,nvme-az2:volumes-nvme-az2"  # chép từ rbd_pool ở trên
```

Cả hai map cùng dạng `khóa:giá-trị,khóa:giá-trị`, **không có khoảng trắng** vì tool không tự cắt.

**Một lần chạy đi qua chuỗi thế nào:** `migrate-vm.sh migrate app-vm01 --az AZ1`

1. Không có `--volume-type`, tool tra `AZ_TYPE_MAP` theo AZ1 và ra `nvme-az1`. Preflight in:
   `INFO  volume type nvme-az1 from AZ_TYPE_MAP (AZ1)`.
2. Tool tạo volume đích bằng `--type nvme-az1 --availability-zone AZ1`. Cinder đổ type đó vào backend
   `rbd-nvme-az1`, nên image tự nằm ở pool `volumes-nvme-az1`.
3. Tool tra `RBD_POOL_MAP` theo `nvme-az1` và ra `volumes-nvme-az1`. Nó kiểm `volume-<id>` có thật
   trong pool đó, rồi ghi + verify ở `rbd:volumes-nvme-az1/volume-<id>`.

```bash
migrate-vm.sh migrate app-vm02 --az AZ2                         # map ra nvme-az2 → pool volumes-nvme-az2
migrate-vm.sh migrate app-vm03 --az AZ2 --volume-type nvme-az1  # type tay luôn thắng map (lệch AZ/type thì tự chịu)
migrate-vm.sh migrate app-vm04                                  # v4: az=AZ1 từ MIG_AZ, type=nvme-az1 từ map
```

Lấy AZ từ `MIG_AZ` thì preflight in thêm `INFO  availability zone AZ1 from MIG_AZ`. Chuỗi này áp cho
`plan`, `migrate`, `migrate-volume`. `migrate-batch.sh` không cần option mới, vì mỗi lần gọi
`migrate-vm.sh` tự đọc conf.

**Thiếu hoặc sai thì sao**

| Tình huống | Kết quả |
|---|---|
| Không khai `AZ_TYPE_MAP` | Chạy như v3: type mặc định của cloud đích, hoặc gõ `--volume-type` tay mỗi lần |
| AZ hiệu lực không có trong `AZ_TYPE_MAP` | Như không có map |
| Cloud đích chỉ có 1 pool | Không cần map nào, `RBD_POOL` là đủ |
| `RBD_POOL_MAP` sai hoặc thiếu type đang dùng | Tool dừng ngay sau khi tạo volume đích, **trước khi ghi byte nào**: `volume-<id> not visible in pool <pool>: wrong RBD_POOL_MAP for type '<type>', or mig-xfer lacks caps on that pool`. Không hỏng dữ liệu, đây là lưới an toàn |

Lưới an toàn này **không nằm trong preflight**. Preflight chỉ kiểm `RBD_USER` liệt kê được pool mà map
chỉ tới (`rbd ls`); nó chưa biết Cinder sẽ đổ volume vào đâu. Khi bị chặn:

- Cold: VM nguồn đã stop + lock (và tháo port nếu `--ip same`). Live: vẫn đang pre-sync, VM nguồn chạy.
- Từ v4, volume đích được ghi vào state ngay khi tạo, nên rollback tự dọn cả volume vừa tạo này
  (kể cả khi nó kẹt ở trạng thái error hoặc đã bị xóa tay trước đó).
- Chạy `migrate-vm.sh rollback <vm>` (trả VM nguồn, dọn phía đích), sửa map, chạy lại.
- Với `migrate-volume`: `migrate-vm.sh rollback <volume>` dọn hết, như trước.

Vì vậy nên kiểm map một lần trước đợt chạy thật, theo một trong hai cách dưới.

**Tra ngược type → pool khi không chắc**

Cách 1, đọc cấu hình:

```bash
openstack volume type show nvme-az1 -c properties   # admin: ra volume_backend_name='rbd-nvme-az1'
# trên controller đích: liệt kê backend + pool, tìm mục có volume_backend_name đó, đọc rbd_pool
# (Kolla: /etc/kolla/cinder-volume/cinder.conf)
grep -nE '^\[|volume_backend_name|rbd_pool' /etc/cinder/cinder.conf
```

`properties` trống nghĩa là type không ghim backend. Khi đó scheduler chọn backend, type → pool không
cố định. Nên ghim type vào một backend, hoặc dùng cách 2 để xem thực tế.

Cách 2, thực nghiệm bằng volume test 1 GB:

```bash
VID=$(openstack volume create mig-pool-probe --size 1 --type nvme-az1 --availability-zone AZ1 -f value -c id)
openstack volume show "$VID" -f value -c status                              # chờ available
rbd -c /etc/ceph/ceph.conf --id mig-xfer -p volumes-nvme-az1 ls | grep "volume-$VID"   # có dòng = đúng pool
openstack volume delete "$VID"
```

`mig-xfer` chỉ có caps trên các pool đã cấp. Muốn dò một pool đoán mò mà user này không đọc được thì
chạy `rbd ls` trên node Ceph bằng user admin.

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
- Volume `in-use` bị từ chối, kèm gợi ý 2 cách: đi theo VM (`migrate-vm.sh migrate <vm> --volumes-only`),
  hoặc từ v4 copy nóng bằng `--in-use` (mục 2b.9). Trạng thái khác `available`/`in-use` thì bị từ chối.
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
- Muốn VM đích vào group **tương ứng group nguồn**, tool tự tạo khi chưa có: dùng `--keep-server-group`
  (v4, mục 2b.10).

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

**Quen tay `migrate-vm.sh` thì đổi sang batch thế nào.** Batch **không có lệnh con** `plan`/`migrate`/
`rollback`, chỉ có option. **Quy tắc: trước `--` là option của batch (selector và điều khiển), sau `--`
là option truyền xuống `migrate-vm.sh`.** Hai lỗi đã gặp thật trên production 18/11:

| Gõ sai | Lỗi | Gõ đúng |
|---|---|---|
| `migrate-batch.sh plan app-vm01` | `ERROR: unknown option plan (options for migrate-vm.sh go after --)` | `migrate-batch.sh --vm app-vm01 --plan-only` |
| `migrate-batch.sh --vm app-vm01 --az AZ1 --yes` | `ERROR: unknown option --az (options for migrate-vm.sh go after --)` | `migrate-batch.sh --vm app-vm01 --yes -- --az AZ1` |

| `migrate-vm.sh` | `migrate-batch.sh` tương đương | Ghi chú |
|---|---|---|
| `migrate-vm.sh plan <vm> [opts]` | `migrate-batch.sh --vm <vm> --plan-only [-- opts]` | Batch tự đặt `SRC_PROJECT`/`DST_PROJECT` theo project của VM. Chạy lẻ với VM thuộc project khác project của openrc thì phải tự đặt: `SRC_PROJECT=proj-a DST_PROJECT=proj-a migrate-vm.sh plan <vm>` |
| `migrate-vm.sh migrate <vm> [opts]` | `migrate-batch.sh --vm <vm> --yes [-- opts]` | Option của `migrate-vm.sh` đặt **sau `--`**. Bỏ `--yes` thì batch hỏi `Type yes to continue` (cần terminal) |
| `migrate-vm.sh rollback <vm>` | (không có) | Rollback luôn bằng `migrate-vm.sh`, theo **ID**, đúng cặp project. Batch in sẵn lệnh này cho dòng `FAIL` và `SKIP(state-exists)`. Dòng `OK` thì lấy ID và cột `PROJECT src -> dst` trong bảng `Summary:`: `SRC_PROJECT=… DST_PROJECT=… migrate-vm.sh rollback <id>` |

Batch cần password openrc cho cả 2 cloud, vì mỗi VM chạy trong project riêng; application credential
bị từ chối. `migrate-vm.sh` chạy lẻ không đặt `SRC_PROJECT`/`DST_PROJECT` thì dùng appcred được.

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

### 2b.9. (v4) Copy nóng volume đang gắn: `migrate-volume <volume> --in-use`

Copy một volume **đang gắn vào VM đang chạy**, không tắt VM. Bản copy là **crash-consistent**: giống
hệt đĩa nếu VM bị rút điện đúng lúc clone.

```bash
migrate-vm.sh migrate-volume data-01 --in-use     # data-01 đang gắn vào app-vm01, VM vẫn chạy
migrate-vm.sh rollback <id-volume-nguồn>          # xóa bản copy ở đích; nguồn không bị đụng
```

Không có `--in-use` thì volume in-use vẫn bị từ chối như v3, và thông báo gợi ý cả 2 cách:

```
ERROR: volume data-01 (<id>) is in-use: an attached volume goes with its VM (/usr/local/bin/migrate-vm.sh migrate <vm> --volumes-only), or copy it live with --in-use (crash-consistent)
```

Có `--in-use` thì preflight nói rõ cách copy:

```
Destination preflight (migrate-volume type=default pool=volumes):
  INFO  <id> is in-use: copied from a clone taken while attached (crash-consistent); its VM keeps running
  OK    volume not multiattach
  OK    volume not encrypted
  OK    no earlier copy of <id> at the destination
  …
```

Luồng giống hệt volume available: clone → attach clone vào host chuyển → tạo volume đích (giữ tên
nguồn, description `migrated from <id>`) → `qemu-img convert` → `qemu-img compare` → xóa clone. **VM và
volume nguồn không bị stop, lock hay detach.** Cuối lệnh, tool in khối sau (giờ thật thay cho
`YYYY-MM-DDTHH:MM:SSZ`):

```
Volume copied crash-consistent as of YYYY-MM-DDTHH:MM:SSZ: data written after that moment
is NOT in the copy (like a sudden power-off; a busy DB will need crash recovery on first mount).
Source VM and volume untouched.
Volume migrated: data-01 <id-nguồn> -> <id-đích>. Source volume untouched (still in-use). Rollback: /usr/local/bin/migrate-vm.sh rollback <id-nguồn>
```

**Hiểu đúng "crash-consistent":**

- Mốc in ra là lúc tool gửi lệnh clone (UTC). Backend chụp ngay sau đó vài giây. Mốc này được ghi vào
  state `/var/lib/mig-migrate/vol-<id>/clone-time`.
- Dữ liệu guest đã ghi **và flush** xuống đĩa trước mốc thì có trong bản copy. Dữ liệu ghi sau mốc thì
  không có. Lab đã chứng minh: dòng đánh dấu ghi trước clone có trong image đích, dòng ghi sau
  `clone attached` thì không ([TEST-REPORT-20261118.md](TEST-REPORT-20261118.md), phase 2).
- Phần còn nằm trong page cache của guest, chưa flush, thì **không** có, giống hệt rút điện.
  Filesystem sẽ replay journal. DB đang ghi nhiều sẽ phải chạy crash recovery lần đầu mount/start.
- Cần bản copy nhất quán mức ứng dụng thì người vận hành tự quiesce trước (dừng app, flush DB,
  `fsfreeze`), vì tool không đụng vào VM. Cách khác là đi theo VM bằng `migrate <vm> --volumes-only`:
  VM tắt êm rồi mới copy.

| Cách | VM nguồn | Bản copy |
|---|---|---|
| `migrate-vm.sh migrate <vm> --volumes-only` | stop + lock, xong vẫn SHUTOFF | Nhất quán tại lúc VM tắt êm; copy mọi volume của VM, tên `<vm>-<device>` |
| `migrate-vm.sh migrate-volume <vol> --in-use` | chạy tiếp, không bị đụng | Crash-consistent tại mốc clone; từng volume một, giữ tên nguồn |

**Quy tắc:**

- Vẫn từ chối: volume mã hóa, volume in-use **multiattach** (preflight `FAIL  volume not multiattach`),
  và trạng thái khác `available`/`in-use`.
- `--in-use` trên volume `available` thì chỉ báo `INFO  --in-use ignored: <id> is available, copied as usual`,
  rồi copy như thường, không in khối crash-consistent.
- Chạy lại trên cùng volume thì bị chặn, vì đích đã có bản copy (`description` guard). Muốn copy lại thì
  `rollback` trước.
- Rollback bằng **ID** volume nguồn (state ở `vol-<id>/`): chỉ xóa bản copy ở đích.
- `migrate-batch.sh --volumes available` **không** gom volume in-use. Muốn copy nóng volume của VM đang
  chạy thì gọi `migrate-volume <id> --in-use` cho từng volume.
- `--in-use` không bao giờ được lấy từ `MIG_DEFAULT_OPTS`: phải gõ tay mỗi lần.

### 2b.10. (v4) Giữ server group: `--keep-server-group`

VM nguồn nằm trong server group nào thì VM đích vào group **tương ứng** ở đích. Group đó tên là
`az-<tên group nguồn>`, cùng policy, tool tự tạo khi chưa có.

| Cách chạy | VM đích vào group nào | Group ở đích |
|---|---|---|
| Không flag | Không group nào | — |
| `--server-group <tên\|ID>` | Đúng group chỉ định | Phải có sẵn, tool không tạo (mục 2b.5) |
| `--keep-server-group` | `az-<tên group nguồn>` | Tool tạo nếu chưa có (lúc `migrate`), có rồi thì dùng lại |

Preflight in ra (ví dụ group nguồn `web-anti`, policy soft-anti-affinity):

```
  INFO  source server group web-anti (<id>, policy soft-anti-affinity) -> destination az-web-anti
  OK    will create az-web-anti (policy soft-anti-affinity)
```

Các trường hợp khác ở đích:

| Ở đích | Preflight |
|---|---|
| Đã có, cùng policy | `OK    server group az-web-anti exists (<id>, policy soft-anti-affinity): reused` |
| Đã có nhưng **khác policy** | `FAIL  server group az-web-anti has policy affinity at the destination, source group web-anti has soft-anti-affinity — to use it anyway drop --keep-server-group and pass --server-group <id>` |
| Có từ 2 group trùng tên | `FAIL` ambiguous, khi đó dùng `--server-group <id>` |
| VM nguồn không thuộc group nào | `INFO  source VM is in no server group`, chạy tiếp như không có flag |

**Quy tắc:**

- Tra group nguồn trong **project nguồn**, theo danh sách member có ID của VM.
- Chép `policy`. Microversion cũ trả `policies[]` thì lấy phần tử đầu.
- Có `rules` (vd `max_server_per_host`) thì chép theo. Client không nhận `--rule` thì tool WARN rồi tạo
  group không có rules.
- **`plan` không tạo gì.** `migrate` tạo group ngay trước khi tạo server, log
  `created destination server group az-web-anti (<id>, policy …)`. Lần sau thấy có sẵn thì log
  `destination server group az-web-anti (<id>) exists: the VM joins it`.
- VM đích vào group qua `server create --hint group=<id>`. State ghi `dst-server-group`
  (`<id> created=yes|no`).
- **Rollback không xóa group**, vì có thể VM khác đang là member; group rỗng thì vô hại. Rollback chỉ báo
  `INFO destination server group kept (<id> created=…): rollback never deletes a server group`.
- Migrate rải rác nhiều ngày vẫn hội tụ: mỗi lần chạy đều tra lại `az-<tên>`, con đầu tạo, các con sau
  dùng lại (miễn là chưa ai xóa group).
- Batch: `-- --keep-server-group` áp cho mọi VM, **mỗi VM theo group của chính nó**. Batch chạy tuần tự
  nên con đầu tạo group, con sau vào cùng group.
- Dùng chung với `--server-group`: gõ tay cả hai thì die
  `choose one: --server-group <name> or --keep-server-group`. Nếu một trong hai đến từ
  `MIG_DEFAULT_OPTS` thì cái gõ tay thắng, cái kia bị bỏ kèm INFO.
- `--volumes-only`: bị bỏ qua (INFO), vì không tạo server.
- Anti-affinity cứng mà đích thiếu host thì Nova báo NoValidHost lúc tạo server, phải rollback.
  Soft-anti-affinity không chặn.

**Cẩn thận: quên flag một lần là VM đó đứng ngoài group, không add lại được.** Nova không cho đưa server
đã tồn tại vào group. Cách sửa duy nhất là `rollback` VM đó (khi nguồn chưa bị xóa) rồi migrate lại có
flag. Nên đưa flag vào conf: `MIG_DEFAULT_OPTS="--keep-server-group"`. Làm vậy an toàn, vì VM không thuộc
group nào thì chỉ có một dòng INFO và chạy bình thường.

### 2b.11. Ví dụ v4

Rút từ các lệnh đã chạy thật trong [TEST-REPORT-20261118.md](TEST-REPORT-20261118.md); tên đã đổi
sang tên chung.

```bash
# 1. Copy nóng volume dữ liệu đang gắn VM đang chạy, rồi hoàn tác nếu cần
migrate-vm.sh migrate-volume data-01 --in-use
migrate-vm.sh rollback <id-volume-nguồn>

# 2. Volume available mà vẫn thêm --in-use (vd script dùng chung): chỉ INFO, copy như thường
migrate-vm.sh migrate-volume data-02 --in-use

# 3. Giữ server group: xem trước (không tạo gì ở đích), rồi chạy lô 2 VM cùng group nguồn web-anti
migrate-vm.sh plan web-01 --keep-server-group --verify none
migrate-batch.sh --vm web-01,web-02 --yes -- --keep-server-group --verify none
#    con 1 log "created destination server group az-web-anti …", con 2 "… exists: the VM joins it"
#    hoàn tác (batch chạy theo ID); az-web-anti vẫn còn sau đó, kể cả khi rỗng:
migrate-vm.sh rollback <id-web-01>
migrate-vm.sh rollback <id-web-02>

# 4. Đưa vào conf: lần nào cũng giữ group và bỏ compare, khỏi nhớ truyền
echo 'MIG_DEFAULT_OPTS="--keep-server-group --verify none"' >> /opt/mig/migrate.conf
migrate-vm.sh plan web-03                  # INFO  MIG_DEFAULT_OPTS applied before the command line: --keep-server-group, --verify none
migrate-vm.sh plan web-03 --verify full    # … --verify none (command line wins)
migrate-batch.sh --project proj-web --yes  # mỗi VM theo group nguồn của nó; VM không có group: INFO, chạy bình thường
migrate-vm.sh migrate web-04 --server-group db-anti   # VM này vào group khác: keep tự bỏ (INFO), không die
MIG_DEFAULT_OPTS="" migrate-vm.sh migrate web-05      # lần này không dùng default nào
migrate-vm.sh migrate-volume data-03                  # chỉ nhận --verify none; --keep-server-group bị bỏ kèm INFO

# 5. Volume rời của project, passthrough có option chỉ dành cho VM: --volume-type vẫn tới đúng volume
migrate-batch.sh --project proj-a --volumes available --yes -- --keep-server-group --volume-type nvme-az1
#    log "available volumes: ignoring VM-only options --keep-server-group"

# 6. AZ + volume type từ conf (MIG_AZ, AZ_TYPE_MAP, RBD_POOL_MAP; mục 2b.1b): không truyền gì
migrate-vm.sh migrate app-vm01   # INFO  availability zone AZ1 from MIG_AZ
                                 # INFO  volume type nvme-az1 from AZ_TYPE_MAP (AZ1)
```

### 2b.12. (v5) Flavor dự phòng qua file map: `--flavor-fallback`

Hai cloud khác bộ flavor thì preflight FAIL ở mọi VM có flavor không tồn tại ở đích. Trước v5 chỉ có
cách gõ `--flavor` tay từng con. Từ v5, khai một file map dùng chung, rồi bật `--flavor-fallback`.

**Thứ tự chọn flavor cho VM đích khi có `--flavor-fallback`:**

| # | Tình huống | Tool làm gì | Preflight in |
|---|---|---|---|
| 1 | Có `--flavor <x>` (gõ tay hoặc trong `MIG_DEFAULT_OPTS`) | Dùng x, **không tra map** | `INFO  --flavor-fallback has no effect: --flavor x is given`, rồi `OK    flavor x` |
| 2 | Đích **có** flavor cùng tên với nguồn | Dùng flavor đó, **không tra map** (map chỉ là dự phòng) | `OK    flavor m1.large` |
| 3 | Đích **thiếu**, map có dòng cho flavor nguồn | Dùng flavor map ra | `INFO  flavor m1.large -> c2.large from FLAVOR_MAP_FILE`, rồi `OK    flavor c2.large` |
| 4 | Đích thiếu, map **không có** dòng | FAIL như v4, kèm gợi ý | `FAIL  flavor m1.medium — not at the destination and not in /opt/mig/flavor-map.conf: add a line 'm1.medium <dest-flavor>' to /opt/mig/flavor-map.conf` |

**Tool không bao giờ tự tạo flavor.** Flavor đích trong map cũng phải có sẵn ở đích, không thì FAIL:

```
  FAIL  flavor nowhere (mapped from m1.ghost in /opt/mig/flavor-map.conf) not found at the destination — the tool never creates flavors
```

Flavor map ra **nhỏ hơn** nguồn (vCPU hoặc RAM) thì preflight in WARN, VM vẫn migrate, người vận hành
tự chịu:

```
  WARN  mapped flavor c2.small is smaller than m1.large (vcpus 4 -> 2, ram 8192 -> 4096 MB)
```

Thông số phía nguồn lấy từ flavor gắn trong `server show` của chính VM, nên vẫn so được khi flavor nguồn
đã bị xóa. Phía đích dùng `flavor show`. Không đọc được thông số thì tool chỉ in
`INFO  flavor sizes not compared: …`.

**File map `FLAVOR_MAP_FILE`.** Format giống map project của batch:

- mỗi dòng `flavor-nguồn flavor-đích`, phân cách bằng khoảng trắng;
- `#` là comment, dòng trống bị bỏ qua;
- dòng sai dạng (thiếu hoặc thừa cột, kể cả kiểu `a = b`) thì tool dừng:
  `ERROR: /opt/mig/flavor-map.conf: expected 'source-flavor destination-flavor', got: m1.large = c2.large`.

```
# /opt/mig/flavor-map.conf
m1.large      c2.large
m1.xlarge     c2.xlarge
legacy.tiny   m1.small
```

**Quy tắc:**

- Không có `--flavor-fallback` thì chạy y như v4, kể cả khi `FLAVOR_MAP_FILE` có khai: map không được
  đọc, flavor thiếu thì `FAIL  flavor <tên>` như cũ.
- Bật flag mà `FLAVOR_MAP_FILE` rỗng hoặc file không đọc được thì tool **dừng**, khác map project của
  batch (chỉ WARN). Lý do: bật flag là đã chủ động yêu cầu map.

  ```
  ERROR: --flavor-fallback needs FLAVOR_MAP_FILE (lines 'source-flavor destination-flavor'), set it in /opt/mig/migrate.conf or the environment
  ```

  Map được đọc và kiểm ngay khi bật flag, kể cả khi lần đó không cần tra (flavor cùng tên có sẵn).
- `--volumes-only` + flag: `INFO  --flavor-fallback ignored: --volumes-only creates no server`, và không
  cần map.
- `migrate-volume` không nhận flag gõ tay (bị từ chối như các option chỉ dành cho server). Nếu flag
  nằm trong `MIG_DEFAULT_OPTS` thì bị bỏ kèm 1 dòng INFO.
- Đặt được trong `MIG_DEFAULT_OPTS`, khi đó hoạt động như gõ tay. Nhớ khai luôn `FLAVOR_MAP_FILE`, không
  thì mọi `plan`/`migrate` VM đều dừng.
- Batch: `-- --flavor-fallback` áp cho mọi VM, **mỗi VM tra map theo flavor của chính nó**. Một map
  dùng chung phục vụ được lô trộn nhiều cỡ VM.
- Flavor map ra được dùng cho `server create` và ghi vào file state `options` (`FLAVOR=`), giống flavor
  truyền bằng `--flavor`.

**Ví dụ** (rút từ các lệnh đã chạy thật trong [TEST-REPORT-20261118-v5.md](TEST-REPORT-20261118-v5.md),
tên đã đổi sang tên chung):

```bash
# 1. Khai map trong conf, xem trước rồi chạy một VM
echo 'FLAVOR_MAP_FILE=/opt/mig/flavor-map.conf' >> /opt/mig/migrate.conf
migrate-vm.sh plan app-vm01 --flavor-fallback        # INFO  flavor m1.large -> c2.large from FLAVOR_MAP_FILE
migrate-vm.sh migrate app-vm01 --flavor-fallback     # VM đích dùng flavor c2.large
migrate-vm.sh rollback app-vm01                      # nếu cần hoàn tác

# 2. Gõ --flavor thì map bị bỏ qua cho VM đó
migrate-vm.sh plan app-vm02 --flavor m1.small --flavor-fallback   # INFO  --flavor-fallback has no effect: …

# 3. Lô nhiều cỡ VM, một map chung: mỗi VM tra theo flavor của nó; VM có flavor cùng tên ở đích dùng luôn
migrate-batch.sh --vm app-vm01,app-vm03 --plan-only -- --flavor-fallback --verify none
migrate-batch.sh --vm app-vm01,app-vm03 --yes -- --flavor-fallback --verify none

# 4. Bật mặc định cho mọi lần chạy
echo 'MIG_DEFAULT_OPTS="--flavor-fallback"' >> /opt/mig/migrate.conf
migrate-vm.sh plan app-vm04                          # INFO  MIG_DEFAULT_OPTS applied before the command line: --flavor-fallback
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
