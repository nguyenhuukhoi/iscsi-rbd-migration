# Triển khai migrate-vm từ git lên host convert (production)

Hướng dẫn từng bước: clone repo → host convert chạy được `migrate-vm.sh`. Dành cho hệ thật
(nguồn OpenStack **2023.1** iSCSI/PowerStore, đích **2026.1** Ceph **19.x Squid**, host convert
đi **1 path** tới SAN).
Hướng dẫn dùng tool chi tiết (options, ví dụ lệnh, workflow): [GUIDE.md](GUIDE.md); phần v3
(file cấu hình, chạy theo project, `migrate-batch.sh`, `migrate-volume`, quy tắc đặt tên): GUIDE.md mục 2b.
Lab dựng/test: cũng trong GUIDE.md mục 5 — không cần cho production.

## 0. Trước khi push repo

File trong repo chia 2 nhóm:

| Nhóm | File | Production cần? |
|---|---|---|
| Tool | `migrate-vm.sh`, `migrate-batch.sh` (v3), `brick_attach.py`, `blocksync.py` | ✅ |
| Tài liệu | `GUIDE.md`, `DESIGN-V2.md`, `DESIGN-V3.md`, `DEPLOY.md`, `POWERSTORE-VS-LAB.md`, `TEST-REPORT-*.md`, `workflow-v2*.html`, `cold-vs-live.html` | Đọc |
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
install -m 0755 migrate-vm.sh migrate-batch.sh brick_attach.py blocksync.py /usr/local/bin/
```

`migrate-batch.sh` (v3) gọi `migrate-vm.sh` **nằm cùng thư mục với nó** (đường dẫn thật, sau khi
resolve symlink) — nên cài cả hai vào `/usr/local/bin`. Đặt chỗ khác thì trỏ bằng env
`MIGRATE_VM=<đường dẫn migrate-vm.sh>`. Host đã cài bản cũ: `git pull` rồi chạy lại đúng lệnh
`install` trên.

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

`chmod 600` cả hai. Cần quyền admin — cụ thể vì Neutron chỉ cho admin/chủ network tạo port với
`--mac-address`/`--fixed-ip` (bắt buộc cho `--ip same`), và vì migrate nhiều project cần nhìn
cross-project. VM nằm trên tenant network của chính project thì member của project đó là đủ
(đã verify từng lệnh, chưa test e2e).

**Khuyến nghị: dùng application credential thay vì password** — gắn hạn dùng, thu hồi độc lập,
không lộ password admin. Tool hỗ trợ sẵn (e2e 50/0 ngày 17/11 chạy hoàn toàn bằng appcred):

```bash
# đăng nhập bằng user admin của từng cloud, chạy cho cả src lẫn dst:
openstack application credential create mig-appcred \
  --description "VM migration tool" --expiration 2027-06-30T00:00:00
# Secret chỉ hiện MỘT lần ở output — ghi ngay vào file rc:
```

```bash
# /opt/mig/{src,dst}/admin-openrc.sh dạng application credential:
unset OS_USERNAME OS_PASSWORD OS_PROJECT_NAME OS_PROJECT_ID \
      OS_USER_DOMAIN_NAME OS_PROJECT_DOMAIN_NAME OS_TENANT_NAME
export OS_AUTH_TYPE=v3applicationcredential
export OS_AUTH_URL=https://<keystone>:5000/v3
export OS_IDENTITY_API_VERSION=3
export OS_REGION_NAME=RegionOne
export OS_APPLICATION_CREDENTIAL_ID=<id>
export OS_APPLICATION_CREDENTIAL_SECRET=<secret>
```

Lưu ý appcred: kế thừa đúng role của user tạo ra nó trên project đó (user admin → appcred đủ
quyền admin); hết hạn hoặc user bị disable là chết theo; dòng `unset` bắt buộc để biến password
cũ không lẫn vào. Xong đợt migrate: `openstack application credential delete mig-appcred`.

**Chạy nhiều project (v3: `SRC_PROJECT`/`DST_PROJECT`, `migrate-batch.sh`) bắt buộc password
openrc.** Application credential bị khóa vào đúng project lúc tạo, không rescope được. Tool kiểm tra
trước khi đụng vào gì và dừng với lỗi rõ ràng:

```
ERROR: SRC_PROJECT/DST_PROJECT needs a password openrc: an application credential is bound to one project (<rc>)
ERROR: <rc> is an application credential: a batch runs each VM in its own project and needs a password openrc
```

Yêu cầu với account (mỗi cloud một account):

- Có role trong **mọi project sẽ migrate**, ở cả 2 cloud. Keystone chỉ cấp token cho project mà user
  có role trên đó.
- Có quyền admin ở nguồn, vì batch liệt kê VM bằng `--all-projects`.
- Project đích phải có sẵn: cùng tên với project nguồn, hoặc theo file map ở mục 7b.

Migrate lẻ không đặt `SRC_PROJECT`/`DST_PROJECT` thì appcred vẫn dùng được như trên.

```bash
# /opt/mig/{src,dst}/admin-openrc.sh dạng password (bắt buộc khi chạy đa project):
unset OS_AUTH_TYPE OS_APPLICATION_CREDENTIAL_ID OS_APPLICATION_CREDENTIAL_SECRET
export OS_AUTH_URL=https://<keystone>:5000/v3
export OS_IDENTITY_API_VERSION=3
export OS_REGION_NAME=RegionOne
export OS_USER_DOMAIN_NAME=Default
export OS_PROJECT_DOMAIN_NAME=Default
export OS_USERNAME=<mig-user>
export OS_PASSWORD=<password>
export OS_PROJECT_NAME=<project-mặc-định>   # tool đè bằng SRC_PROJECT/DST_PROJECT khi chạy
```

Các project được chọn phải cùng domain với `OS_PROJECT_DOMAIN_NAME`, vì tool chỉ đè `OS_PROJECT_NAME`.

## 7. Cấu hình iSCSI 1 path

Theo quyết định 17/11 (chi tiết: [POWERSTORE-VS-LAB.md](POWERSTORE-VS-LAB.md) mục "Quyết định 17/11"):

1. `MIG_MULTIPATH` để mặc định (0) — không set gì.
2. Chặn multipathd vồ LUN migrate — blacklist trong `/etc/multipath.conf` hoặc tắt hẳn:
   `systemctl disable --now multipathd` (nếu host không còn vai trò nào khác cần multipath).
3. Soi portal nối vào có thuộc node đang own volume không (`iscsiadm -m session` + owner trên
   mảng) — trúng node non-optimized thì copy chậm hẳn do ALUA forward nội bộ.
4. Set `MIG_ISCSI_IP=<IP của host convert trên mạng iSCSI>` khi chạy tool (os-brick dùng làm
   connector IP; mặc định trong script là IP lab).

## 7b. File cấu hình `/opt/mig/migrate.conf` và map project (v3)

Gom các giá trị của mục 5–7 vào một file thay cho `export` mỗi lần chạy. Cả `migrate-vm.sh` lẫn
`migrate-batch.sh` tự đọc file này. Thứ tự ưu tiên: **option CLI > biến môi trường > migrate.conf >
mặc định trong script**. Env vẫn đè được file, nên cách chạy bằng `export` ở mục 9 không đổi hành vi.
Đổi đường dẫn file bằng env `MIG_CONF=<file>`.

```bash
mkdir -p /opt/mig
cat > /opt/mig/migrate.conf <<'EOF'
# bash KEY=VALUE, không cần export; xóa dòng nào thì biến đó về mặc định trong script
SRC_RC=/opt/mig/src/admin-openrc.sh
DST_RC=/opt/mig/dst/admin-openrc.sh
RBD_POOL=<pool>
RBD_POOL_MAP=""                     # "typeA:poolA,typeB:poolB" nếu có nhiều volume type/pool
RBD_USER=mig-xfer
CEPH_CONF=/etc/ceph/ceph.conf
MIG_ISCSI_IP=<ip-iscsi-host>
MIG_MULTIPATH=0
COPY_STREAMS=8
PROJECT_MAP_FILE=/opt/mig/project-map.conf
# SRC_PROJECT= / DST_PROJECT= : để trống ở đây, đặt theo lần chạy (batch tự đặt cho từng VM)
EOF
chmod 600 /opt/mig/migrate.conf
```

Danh sách biến đọc từ file: `SRC_RC DST_RC RBD_POOL RBD_POOL_MAP RBD_USER CEPH_CONF MIG_ISCSI_IP
MIG_MULTIPATH COPY_STREAMS PROJECT_MAP_FILE SRC_PROJECT DST_PROJECT`.
Cloud đích nhiều AZ hoặc nhiều pool: khai `RBD_POOL_MAP` (và từ v4 `MIG_AZ`, `AZ_TYPE_MAP`) theo
GUIDE mục 2b.1b, nơi giải thích chuỗi AZ → volume type → pool, lý do phải khai và cách tra ngược.

**Map project (tùy chọn)** là file riêng, `migrate.conf` trỏ tới nó qua `PROJECT_MAP_FILE`. Chỉ cần
khi project đích **khác tên** project nguồn; project không có trong file thì giữ nguyên tên. Mỗi dòng
có dạng `project-nguồn project-đích`, **phân cách bằng khoảng trắng** (dòng kiểu `a = b` làm batch
dừng vì sai dạng), `#` là comment:

```bash
cat > /opt/mig/project-map.conf <<'EOF'
# project-nguồn   project-đích
finance           finance-prod
web-legacy        web
EOF
```

Chỉ `migrate-batch.sh` đọc file map. Chạy lẻ `migrate-vm.sh` với project khác tên thì tự đặt
`DST_PROJECT=<project-đích>`.

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
# v3, đa project (password openrc): batch liệt kê + plan, không đổi gì trên 2 cloud
migrate-batch.sh --vm <vm-test> --plan-only
```

## 9. Chạy theo thứ tự an toàn

```bash
# Đã có /opt/mig/migrate.conf (mục 7b) thì bỏ dòng export này
export SRC_RC=/opt/mig/src/admin-openrc.sh DST_RC=/opt/mig/dst/admin-openrc.sh \
       RBD_POOL=<pool> RBD_USER=mig-xfer MIG_ISCSI_IP=<ip-iscsi-host>

migrate-vm.sh plan <vm-test>                 # 1. chỉ đọc, không sửa gì — xem preflight
migrate-vm.sh migrate <vm-test> --mode cold  # 2. VM test trước
migrate-vm.sh migrate <vm-test2> --mode live # 3. rồi mới live
# rollback bất kỳ lúc nào (dọn đích, trả nguồn về như cũ):
migrate-vm.sh rollback <vm>

# v3, theo lô (password openrc, mục 6):
migrate-batch.sh --host <compute-host> --plan-only   # 4. liệt kê + plan toàn bộ, không đổi gì
migrate-batch.sh --host <compute-host> --yes         # 5. migrate tuần tự, dừng ở con FAIL đầu tiên
# rollback con nào: chạy đúng lệnh batch in ra (SRC_PROJECT=… DST_PROJECT=… migrate-vm.sh rollback <id>)
```

`<compute-host>` lấy ở cột Host của `openstack compute service list`, không phải `hypervisor_hostname`.

Với PowerStore: **trước khi copy, đối chiếu WWN của device vừa gắn (ghi trong state
`/var/lib/mig-migrate/<vm>/`) với volume trên mảng** — guard kích thước của tool không bắt được
LUN lạ cùng cỡ (bằng chứng: sự cố stale-backstore 13/11, chỉ checksum trong guest phát hiện).

State từng VM nằm ở `/var/lib/mig-migrate/<vm>/`; job chết giữa chừng thì `rollback` dọn được
cả state dở dang. Log attach lỗi: `/var/lib/mig-migrate/<vm>/brick-attach.err`.
V3: state của `migrate-volume` ở `/var/lib/mig-migrate/vol-<id-volume-nguồn>/`; batch ghi state theo
**ID** VM (`/var/lib/mig-migrate/<id>/`) và log mỗi lần chạy ở `/var/log/mig-batch/<YYYYmmdd-HHMMSS>/`.

## 10. Những gì chưa test trên hệ thật

Đọc mục "Chưa test" của [TEST-REPORT-20261112.md](TEST-REPORT-20261112.md) và checklist
PowerStore trong [POWERSTORE-VS-LAB.md](POWERSTORE-VS-LAB.md) trước khi migrate VM quan trọng:
WWN-check với mảng thật, stanza multipath Dell (nếu sau này bật 2 đường), thin clone/host
object/CHAP của driver PowerStore, và tốc độ đường mạng thật (ước lượng downtime).

V3 (mục "Chưa test" của [TEST-REPORT-20261117.md](TEST-REPORT-20261117.md)): `PROJECT_MAP_FILE`
có cặp project khác tên thật, `--continue-on-error` gặp FAIL giữa chừng thật, `--volumes all`.
