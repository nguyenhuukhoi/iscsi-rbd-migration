# migrate 1.0.1

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

## Lấy mã và build wheel

Trên máy build (git, python3, pip; không cần là host convert):

```bash
git clone --branch v1.0.1 --depth 1 https://github.com/nguyenhuukhoi/iscsi-rbd-migration.git
cd iscsi-rbd-migration
./release.sh        # tạo dist/migratetool-1.0.1-py3-none-any.whl và .sha256
```

`--branch` nhận tên tag: checkout đúng bản phát hành (bỏ cờ này để lấy `main`). Chép `dist/*.whl`, `dist/*.sha256`, `requirements-host.txt` và cả checkout (UI cài từ `migui/deploy/install.sh`) sang host convert, rồi làm theo mục dưới.

## Cài và chạy thử

Trên host đã chuẩn bị theo [DEPLOY.md](DEPLOY.md), dùng wheel được reviewer xác minh SHA256 và file pin cùng release:

```bash
python3 -m venv /opt/mig-venv
/opt/mig-venv/bin/pip install -r requirements-host.txt dist/migratetool-1.0.1-py3-none-any.whl
ln -sfn /opt/mig-venv/bin/migrate /usr/local/bin/migrate
migrate --version
migrate doctor --write-check
MIG_DEFAULT_OPTS= migrate plan canary-vm --mode cold --ip same --verify full
MIG_DEFAULT_OPTS= migrate migrate canary-vm --mode cold --ip same --verify full
```

Lệnh `pip install` trên cài luôn OpenStack CLI (`openstack`) và SDK vào `/opt/mig-venv`, đúng bộ bản đã test. Muốn tự chọn bản theo release OpenStack, xem [DEPLOY.md mục 2.1](DEPLOY.md#21-openstack-cli-và-sdk).

Chạy bằng root. Đọc plan rồi gõ `yes` khi CLI hỏi. Ví dụ giữ verify và tháo port nguồn. `doctor --write-check` tạo rồi dọn image RBD thử nghiệm. Plan ghi export/log cục bộ, không sửa cloud.

UI được cài riêng từ checkout cùng release, không nằm trong wheel:

```bash
sudo migui/deploy/install.sh
sudo /opt/migui/bin/migui-passwd add operator
# Trên workstation:
ssh -L 8787:127.0.0.1:8787 operator@192.0.2.20
```

Mở `http://127.0.0.1:8787/`. Đăng nhập → Doctor → Chọn → Tùy chọn → Xác nhận → Chạy. Không cài hoặc restart UI khi còn job.

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
| [RELEASE-1.0.0.md](RELEASE-1.0.0.md) | Bằng chứng kiểm thử và gate chưa hoàn tất |
| [archive/README.md](archive/README.md) | Bản đồ tài liệu lưu trữ |

## Tương thích và trạng thái production

Mục tiêu triển khai: nguồn OpenStack **2023.1 + PowerStore iSCSI**, đích **2026.1 + Ceph 19**. Lab dùng LVM/LIO iSCSI. Convert host Linux có systemd, Python **≥ 3.10**, quyền root, IQN riêng, đường tới API hai cloud, iSCSI nguồn và Ceph đích. Ubuntu 24.04/Python 3.12 là môi trường pin dependency. Cần `qemu-img` có RBD, os-brick trong venv và `python3-rbd` của Python hệ thống. PowerStore dùng single-path có chủ đích; phải đối chiếu WWN ở canary.

**Chưa tuyên bố đã đạt gate production v1.** Báo cáo đã có: `ALL pass=836 fail=0`, UI guards `101/0`, CDP smoke `38/0`. Gate lab v1 và canary PowerStore còn phải ghi nhận trong [RELEASE-1.0.0.md](RELEASE-1.0.0.md). Rollback xóa bản đích và quay về nguồn; mọi ghi mới ở đích sau cutover không được đồng bộ ngược. Duyệt RPO trước chiến dịch.
