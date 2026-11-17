# Chuyển VM boot-from-volume từ OpenStack iSCSI sang OpenStack Ceph RBD

Bản v1 (cold, giữ IP/MAC) chạy thật ngày 2026-11-05: [TEST-REPORT-20261105.md](TEST-REPORT-20261105.md).
Bản v2 (thêm live mode, chọn volume type, chọn IP) thiết kế ở [DESIGN-V2.md](DESIGN-V2.md), test ngày
2026-11-12: [TEST-REPORT-20261112.md](TEST-REPORT-20261112.md). IP trong tài liệu là IP ví dụ
(dải TEST-NET 192.0.2.x / 198.51.100.x); IP thật ghi ở tài liệu nội bộ ngoài repo.
Triển khai lên host convert production (từ git, step-by-step): [DEPLOY.md](DEPLOY.md).

## 1. Cách làm

Sơ đồ workflow đầy đủ (cold + live + 3 chế độ IP + rollback): mở [workflow-v2.html](workflow-v2.html).
Bản dọc tối giản, mã màu theo mode: [workflow-v2-simple.html](workflow-v2-simple.html).
So sánh riêng Cold vs Live (khoảng nào VM còn chạy, khoảng nào downtime): [cold-vs-live.html](cold-vs-live.html).
Dạng chữ:

```
  Bước 0  Lệnh migrate (--mode cold|live · --ip same|new|addr · --volume-type · --flavor · --security-group)
  Bước 1  Preflight đích: flavor, network, SG, IP/MAC trống, volume type. FAIL = dừng, chưa tạo gì
  Bước 2  Tạo volume đích (--volume-type → đúng pool RBD qua RBD_POOL_MAP)
  Bước 3  CHỈ --mode live, VM vẫn chạy:
            3a clone volume nguồn (crash-consistent) → 3b blocksync (hash 4 MiB, chỉ ghi delta)
            lặp 3a↔3b tới khi delta ≤ --delta-limit hoặc đủ --max-passes
          (--mode cold: bỏ qua bước 3, đi thẳng bước 4)

  ── CUTOVER, downtime bắt đầu ──
  Bước 4  Tắt + lock VM nguồn (cả cold lẫn live)
  Bước 5  Port nguồn: --ip same → tháo, giữ chỗ IP/MAC · --ip new/addr → giữ nguyên
  Bước 6  Copy cuối + verify: cold = qemu-img copy + compare · live = sync delta + readback
          root ephemeral (không volume) = snapshot Glance → tải về → đẩy lên đích
  Bước 7  Tạo port + VM đích: MAC/IP theo --ip, flavor/SG theo override,
          copy server properties + description, user-data tối thiểu giữ SSH host key + hostname
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
| `--verify readback\|full\|none` | live: `readback` (mặc định) đọc lại từng chunk vừa ghi; `full` thêm `qemu-img compare` toàn bộ trong cutover; `none` không kiểm. Cold luôn compare toàn bộ |

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

# 5. Live, IP chỉ định (VM 1 NIC), MAC mới
migrate-vm.sh migrate demo-vm --mode live --ip 192.168.120.150

# 6. Hối hận: xóa sạch phía đích, trả VM về nguồn (tự hiểu option đã dùng từ state)
migrate-vm.sh rollback demo-vm

# 7. Trỏ sang hệ thống khác + volume type nằm pool khác
SRC_RC=/opt/prod/src-openrc.sh DST_RC=/opt/prod/dst-openrc.sh \
RBD_POOL=cinder-ssd RBD_POOL_MAP="hdd:cinder-hdd,ssd:cinder-ssd" RBD_USER=mig-xfer \
MIG_ISCSI_IP=10.77.0.20 \
migrate-vm.sh migrate app-vm01 --mode live --volume-type hdd
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

Live mode cần thêm `blocksync.py` trong `/usr/local/bin` và `python3-rbd` trên host chuyển.

**Điều kiện phía đích, preflight sẽ kiểm tra:** flavor, keypair, security group cùng tên; network cùng tên, subnet cùng CIDR; MAC và IP chưa bị dùng.

**Preflight từ chối:** volume mã hóa, volume multiattach, port nhiều fixed IP, port có allowed-address-pairs, và `--mode live` cho VM không boot từ volume.

**VM không dùng volume (root ephemeral):** đi đường Glance — snapshot VM đã tắt → tải về host chuyển → đẩy lên Glance đích bằng REST (openstackclient lỗi với upload dài) → so sha512 với hash Glance tính → VM đích boot từ image đó. Chỉ cold. Server properties + description luôn được copy sang VM đích; MAC/IP + port của VM nhiều NIC (kể cả khác network) được giữ từng port một.

Khi chạy thật ổn định và không cần rollback nữa, tự xóa bằng tay ở nguồn: server, volume, port. Công cụ cố ý không có lệnh này.

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
install -m 0755 migrate-vm.sh brick_attach.py blocksync.py /usr/local/bin/
```

Hoặc đẩy từ workstation vào lab như lần dựng gốc:

```bash
scp -i key.txt iscsi-rbd-migration/{migrate-vm.sh,brick_attach.py,blocksync.py} root@192.0.2.109:/usr/local/bin/
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
