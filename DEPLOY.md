# Triển khai migrate-vm từ git lên host convert (production)

Hướng dẫn từng bước: clone repo → host convert chạy được `migrate-vm.sh`. Dành cho hệ thật
(nguồn OpenStack **2023.1** iSCSI/PowerStore, đích **2026.1** Ceph **19.x Squid**, host convert
đi **1 path** tới SAN).
Hướng dẫn dùng tool chi tiết (options, ví dụ lệnh, workflow): [GUIDE.md](GUIDE.md).
Lab dựng/test: cũng trong GUIDE.md mục 5 — không cần cho production.

## 0. Trước khi push repo

File trong repo chia 2 nhóm:

| Nhóm | File | Production cần? |
|---|---|---|
| Tool | `migrate-vm.sh`, `brick_attach.py`, `blocksync.py` | ✅ |
| Tài liệu | `GUIDE.md`, `DESIGN-V2.md`, `DEPLOY.md`, `POWERSTORE-VS-LAB.md`, `TEST-REPORT-*.md`, `workflow-v2*.html`, `cold-vs-live.html` | Đọc |
| Lab | `01…06-*.sh`, `lab.sh`, `kolla/`, `user-data.tmpl`, `tests/`, `archive/` | ❌ (riêng `tests/blocksync-unit.sh` chạy được ngoài lab để smoke-test live mode) |

**Không bao giờ commit:** openrc (`admin-openrc.sh`), keyring Ceph, SSH key (`key.txt`),
`passwords.yml`. Repo hiện không chứa các file này — giữ nguyên như vậy.

## 1. Yêu cầu host convert

- Ubuntu 24.04 (Noble), quyền root.
- Mạng tới được đồng thời: **API 2 cloud** (Keystone/Nova/Cinder/Glance/Neutron),
  **mạng iSCSI của SAN nguồn** (1 đường là đủ), **mạng public của Ceph đích** (mon + OSD).
- Đĩa trống: migrate VM ephemeral cần chỗ chứa file qcow2 bằng cỡ image snapshot.
- Initiator IQN **duy nhất** trong `/etc/iscsi/initiatorname.iscsi` (PowerStore tạo host object
  theo IQN — trùng với host khác là map lộn volume).

## 2. Cài gói hệ thống

Ceph đích là 19.x nên dùng thẳng repo Ubuntu (không cần repo ceph.com như lab):

```bash
apt-get update
apt-get install -y open-iscsi qemu-utils qemu-block-extra ceph-common python3-rbd jq \
  python3-venv python3-dev gcc libffi-dev libssl-dev
systemctl enable --now iscsid
```

`python3-rbd` bắt buộc cho `--mode live` (blocksync chạy bằng python **hệ thống**
`/usr/bin/python3`, không phải venv — đừng pip install rbd vào venv, không có tác dụng).

## 3. Tạo venv os-brick

`migrate-vm.sh` mặc định activate `/opt/mig-venv` — giữ đúng đường dẫn này:

```bash
python3 -m venv /opt/mig-venv
/opt/mig-venv/bin/pip install -U pip
/opt/mig-venv/bin/pip install \
  -c https://releases.openstack.org/constraints/upper/2026.1 \
  python-openstackclient python-cinderclient python-brick-cinderclient-ext os-brick
```

Một bộ client này dùng cho **cả hai cloud**, kể cả khi nguồn đời cũ hơn (vd 2023.1): client
OpenStack tương thích ngược qua version negotiation. Yêu cầu duy nhất với cloud nguồn là
**Cinder API microversion ≥ 3.54** (attachment mode — 2023.1 hỗ trợ tới 3.70). Nguồn khác
2026.1 thì soi kỹ output `plan` lần đầu (chỉ đọc) trước khi migrate thật.

## 4. Cài tool từ repo

```bash
git clone https://github.com/nguyenhuukhoi/iscsi-rbd-migration.git
cd iscsi-rbd-migration
install -m 0755 migrate-vm.sh brick_attach.py blocksync.py /usr/local/bin/
```

## 5. Cấu hình Ceph đích

Chép từ cluster đích về host convert: `/etc/ceph/ceph.conf` + keyring của user chuyển.
User chỉ cần quyền RBD trên pool Cinder đích (tạo trên cluster nếu chưa có):

```bash
# chạy trên node mon của cluster đích; thay <pool> bằng pool Cinder thật
ceph auth get-or-create client.mig-xfer mon 'profile rbd' osd 'profile rbd pool=<pool>'
ceph auth get client.mig-xfer   # dán vào /etc/ceph/ceph.client.mig-xfer.keyring trên host convert
```

## 6. Openrc 2 cloud

Đặt theo đường dẫn mặc định của tool (hoặc trỏ bằng env `SRC_RC`/`DST_RC`):

```
/opt/mig/src/admin-openrc.sh    # cloud nguồn (iSCSI)
/opt/mig/dst/admin-openrc.sh    # cloud đích (Ceph)
```

`chmod 600` cả hai. Cần quyền admin (tool đọc/tạo port, reset state volume, lock server).

## 7. Cấu hình iSCSI 1 path

Theo quyết định 17/11 (chi tiết: [POWERSTORE-VS-LAB.md](POWERSTORE-VS-LAB.md) mục "Quyết định 17/11"):

1. `MIG_MULTIPATH` để mặc định (0) — không set gì.
2. Chặn multipathd vồ LUN migrate — blacklist trong `/etc/multipath.conf` hoặc tắt hẳn:
   `systemctl disable --now multipathd` (nếu host không còn vai trò nào khác cần multipath).
3. Soi portal nối vào có thuộc node đang own volume không (`iscsiadm -m session` + owner trên
   mảng) — trúng node non-optimized thì copy chậm hẳn do ALUA forward nội bộ.
4. Set `MIG_ISCSI_IP=<IP của host convert trên mạng iSCSI>` khi chạy tool (os-brick dùng làm
   connector IP; mặc định trong script là IP lab).

## 8. Verify trước khi chạy thật

```bash
ceph -n client.mig-xfer --keyring /etc/ceph/ceph.client.mig-xfer.keyring -s   # thấy cluster đích
rbd  -n client.mig-xfer ls <pool>                                             # đọc được pool
/usr/bin/python3 -c 'import rbd, rados; print("rbd bindings OK")'             # live mode
source /opt/mig-venv/bin/activate
source /opt/mig/src/admin-openrc.sh && openstack token issue                  # API nguồn
source /opt/mig/dst/admin-openrc.sh && openstack token issue                  # API đích
# smoke-test blocksync không đụng VM (tùy chọn, cần quyền tạo image RBD trong pool):
bash tests/blocksync-unit.sh
```

## 9. Chạy theo thứ tự an toàn

```bash
export SRC_RC=/opt/mig/src/admin-openrc.sh DST_RC=/opt/mig/dst/admin-openrc.sh \
       RBD_POOL=<pool> RBD_USER=mig-xfer MIG_ISCSI_IP=<ip-iscsi-host>

migrate-vm.sh plan <vm-test>                 # 1. chỉ đọc, không sửa gì — xem preflight
migrate-vm.sh migrate <vm-test> --mode cold  # 2. VM test trước
migrate-vm.sh migrate <vm-test2> --mode live # 3. rồi mới live
# rollback bất kỳ lúc nào (dọn đích, trả nguồn về như cũ):
migrate-vm.sh rollback <vm>
```

Với PowerStore: **trước khi copy, đối chiếu WWN của device vừa gắn (ghi trong state
`/var/lib/mig-migrate/<vm>/`) với volume trên mảng** — guard kích thước của tool không bắt được
LUN lạ cùng cỡ (bằng chứng: sự cố stale-backstore 13/11, chỉ checksum trong guest phát hiện).

State từng VM nằm ở `/var/lib/mig-migrate/<vm>/`; job chết giữa chừng thì `rollback` dọn được
cả state dở dang. Log attach lỗi: `/var/lib/mig-migrate/<vm>/brick-attach.err`.

## 10. Những gì chưa test trên hệ thật

Đọc mục "Chưa test" của [TEST-REPORT-20261112.md](TEST-REPORT-20261112.md) và checklist
PowerStore trong [POWERSTORE-VS-LAB.md](POWERSTORE-VS-LAB.md) trước khi migrate VM quan trọng:
WWN-check với mảng thật, stanza multipath Dell (nếu sau này bật 2 đường), thin clone/host
object/CHAP của driver PowerStore, và tốc độ đường mạng thật (ước lượng downtime).
