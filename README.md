# Migrate

Chuyển VM giữa hai cloud OpenStack độc lập: Cinder iSCSI trên LVM hoặc Dell PowerStore ở nguồn → Cinder trên Ceph RBD ở đích. Package `migratetool/` cung cấp CLI `migrate`. Web UI `migui/` gọi cùng CLI trên host convert. `live` giảm downtime bằng pre-sync; vẫn phải tắt VM để cutover. Không chuyển RAM hoặc kết nối đang mở.

```text
Operator CLI --------------------> migrate
Browser -- SSH tunnel -----------> migui-web (user migui)
migui-web -- Unix socket ---------> migui-runner (root)
migui-runner ---------------------> migrate (cùng khóa host với CLI)
migrate -- OpenStack API ---------> cloud nguồn + cloud đích
Cinder nguồn --------------------> clone tạm, không ghi volume gốc
os-brick ------------------------> gắn clone iSCSI read-only
cold: qemu-img / live: blocksync -> RBD volume do Cinder đích tạo
cutover -------------------------> stop nguồn, tháo port, lock, copy cuối
Nova đích <----------------------- volume + port mới; state giữ để rollback
```

## Cài và chạy thử

Chạy bằng root trên host convert Ubuntu 24.04, thấy được API hai cloud, portal iSCSI nguồn và Ceph public network đích. Sáu bước dưới đã chạy nguyên văn trên lab (23/11/2026).

1. Gói hệ thống. Client Ceph lấy từ repo Ceph Tentacle: `ceph-common` 19.x của Ubuntu không đọc được key `aes256k` của cụm Tentacle ≥ 20.2.4.

```bash
curl -fsSL https://download.ceph.com/keys/release.asc -o /usr/share/keyrings/ceph-release.asc
echo "deb [signed-by=/usr/share/keyrings/ceph-release.asc] https://download.ceph.com/debian-tentacle noble main" \
  > /etc/apt/sources.list.d/ceph-tentacle.list
apt-get update
apt-get install -y git open-iscsi qemu-utils qemu-block-extra ceph-common python3-rbd jq \
  python3-venv python3-dev gcc libffi-dev libssl-dev e2fsprogs
systemctl enable --now iscsid
```

2. Lấy mã và cài tool vào venv (cài luôn OpenStack CLI và SDK đúng bộ đã test; chọn bản theo release OpenStack: [DEPLOY.md mục 2.1](DEPLOY.md#21-openstack-cli-và-sdk)). `<tag>` là bản muốn cài, ví dụ tag mới nhất ở trang Releases trên GitHub (bỏ `--branch` để lấy `main`):

```bash
git clone --branch <tag> --depth 1 https://github.com/nguyenhuukhoi/iscsi-rbd-migration.git /opt/mig-src
python3 -m venv /opt/mig-venv
/opt/mig-venv/bin/pip install -r /opt/mig-src/requirements-host.txt /opt/mig-src
ln -sfn /opt/mig-venv/bin/migrate /usr/local/bin/migrate
migrate --version
```

Host không ra Internet, hoặc cần wheel + SHA256 theo quy trình thay đổi: build bằng `release.sh` trên máy khác, xem [DEPLOY.md mục 2](DEPLOY.md#2-artifact-và-venv).

3. Openrc hai cloud, file root 0600, chỉ gồm các dòng `export OS_*` (không `read`, không prompt; mẫu password và application credential: [DEPLOY.md mục 3](DEPLOY.md#3-openrc-hai-cloud)):

```bash
install -d -m 0700 /opt/mig/src /opt/mig/dst
install -m 0600 /dev/null /opt/mig/src/admin-openrc.sh   # cloud nguồn (iSCSI)
install -m 0600 /dev/null /opt/mig/dst/admin-openrc.sh   # cloud đích (Ceph)
```

4. Ceph client của cụm đích: `/etc/ceph/ceph.conf` và keyring của user có quyền ghi pool đích (tạo user: [DEPLOY.md mục 4](DEPLOY.md#4-ceph-và-iscsi-powerstore)), kiểm bằng `rbd --id mig-xfer -p <pool> ls`.

5. Conf `/opt/mig/migrate.conf` (root 0600) và thư mục làm việc. `RBD_POOL_MAP` map **tên volume type đích** sang pool RBD; toàn bộ khóa trong [migrate.conf.example](migrate.conf.example) và [GUIDE.md mục 4](GUIDE.md#4-cấu-hình-và-map).

```bash
cat > /opt/mig/migrate.conf <<'EOF'
RBD_USER=mig-xfer
RBD_POOL=volumes
RBD_POOL_MAP="__DEFAULT__:volumes"
MIG_ISCSI_IP=192.0.2.20
MIG_DEFAULT_OPTS="--verify full"
EOF
chmod 0600 /opt/mig/migrate.conf
install -d -m 0700 /run/mig /var/lib/mig-migrate /var/log/mig-batch
printf '%s\n' 'd /run/mig 0700 root root -' > /etc/tmpfiles.d/migrate.conf
```

`MIG_ISCSI_IP` là IP của host này trên mạng iSCSI.

6. Kiểm tra rồi chạy canary (đọc plan, gõ `yes` khi CLI hỏi; `--yes` cho script):

```bash
migrate doctor --write-check
migrate plan canary-vm --mode cold --ip same
migrate migrate canary-vm --mode cold --ip same
migrate rollback canary-vm        # trả VM về nguồn khi cần
```

Trên lab: doctor 21 mục OK; VM 10 GB + 2 GB cold mất 8 phút kể cả `--verify full`, rollback 1,5 phút. `doctor --write-check` tạo rồi dọn image RBD thử nghiệm; plan ghi export/log cục bộ, không sửa cloud. Chi tiết (PowerStore/multipath, host không Internet, nâng cấp): [DEPLOY.md](DEPLOY.md).

UI được cài từ cùng checkout:

```bash
sudo /opt/mig-src/migui/deploy/install.sh
sudo /opt/migui/bin/migui-passwd add operator
# Trên workstation:
ssh -L 8787:127.0.0.1:8787 operator@192.0.2.20
```

Mở `http://127.0.0.1:8787/`. Đăng nhập → Doctor → Chọn → Tùy chọn → Xác nhận → Chạy. Không cài hoặc restart UI khi còn job.

### Nâng cấp

Host đã cài theo các bước trên (`git clone` vào `/opt/mig-src`). `git clone` không ghi đè thư mục đã có, nên checkout cũ được đổi tên kèm số bản đang chạy rồi clone bản mới. `<tag>` là bản muốn lên:

```bash
migrate status            # phải: không job nào đang chạy (CLI lẫn UI)
mv /opt/mig-src /opt/mig-src.bak-$(migrate --version | awk '{print $NF}')
git clone --branch <tag> --depth 1 https://github.com/nguyenhuukhoi/iscsi-rbd-migration.git /opt/mig-src
/opt/mig-venv/bin/pip install -r /opt/mig-src/requirements-host.txt /opt/mig-src
migrate --version && migrate doctor
sudo /opt/mig-src/migui/deploy/install.sh     # UI, chỉ khi không còn job
```

Conf `/opt/mig/migrate.conf`, openrc và thư mục state không đổi: state của bản cũ vẫn rollback được bằng bản mới. Hạ cấp và cài bằng wheel/pin: [DEPLOY.md mục 7](DEPLOY.md#7-nâng-cấp-hạ-cấp-gỡ).

## Đọc tài liệu

| File | Dùng khi |
|---|---|
| [DESIGN.md](DESIGN.md) | Thiết kế chuẩn, state, rollback, kiểm tra và hợp đồng JSON/UI |
| [GUIDE.md](GUIDE.md) | Lệnh, option, cấu hình và ví dụ vận hành |
| [DEPLOY.md](DEPLOY.md) | Host, credentials, Ceph, iSCSI, systemd và nâng cấp |
| [RUNBOOK.md](RUNBOOK.md) | Chiến dịch, nghiệm thu, sự cố và bàn giao ca |
| [UI.md](UI.md) | Thao tác từng màn hình web |
| [DEVELOPING.md](DEVELOPING.md) | Code, test, release và chống leak |
| [CHANGELOG.md](CHANGELOG.md) | Lịch sử sản phẩm |
| [RELEASE-1.0.0.md](RELEASE-1.0.0.md) | Bằng chứng gate lab 22/11 và danh sách gate chưa chạy |
| [archive/README.md](archive/README.md) | Bản đồ tài liệu lưu trữ |

## Tương thích và trạng thái production

Mục tiêu triển khai: nguồn OpenStack **2023.1 + PowerStore iSCSI**, đích **2026.1 + Ceph 19/20**. Lab dùng LVM/LIO iSCSI. Convert host Linux có systemd, Python **≥ 3.10**, quyền root, IQN riêng, đường tới API hai cloud, iSCSI nguồn và Ceph đích. Ubuntu 24.04/Python 3.12 là môi trường pin dependency. Cần `qemu-img` có RBD, os-brick trong venv và `python3-rbd` của Python hệ thống. PowerStore dùng single-path có chủ đích; phải đối chiếu WWN ở canary.

Gate lab 1.0.0 chạy ngày 22/11/2026 trên hai cloud lab 2026.1 (nguồn LVM/LIO iSCSI, đích Ceph RBD) đạt toàn bộ; chi tiết và các gate chưa chạy ở [RELEASE-1.0.0.md](RELEASE-1.0.0.md) mục 5, số liệu từng bản ở [CHANGELOG.md](CHANGELOG.md). Lab không phải PowerStore: canary trên đúng hạ tầng 2023.1 + PowerStore chưa chạy, chưa tuyên bố đạt production. Rollback xóa bản đích và quay về nguồn; mọi ghi mới ở đích sau cutover không được đồng bộ ngược. Duyệt RPO trước chiến dịch. Rollback hỏi nguồn trước: VM/volume nguồn không tìm thấy (đã xóa, hoặc credentials/scope/endpoint nguồn không thấy) hoặc không đọc được thì từ chối và không xóa gì ở đích, vì đích có thể là bản sao duy nhất còn lại. Rollback cũng phải đọc được đích trước lệnh xóa đầu tiên và chỉ coi một object đích là đã mất khi API nêu đúng ID của nó; lỗi endpoint/404 trần thì dừng, không đụng nguồn. Rollback dùng cùng loại openrc với lần migrate: state ghi bằng password openrc (record có project) rollback bằng application credential bị từ chối `this state was written by a run scoped with ... (a password openrc)`, vì application credential không rescope được; chạy rollback bằng đúng password openrc đã migrate.
