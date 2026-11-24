# Triển khai Migrate

Mục tiêu: convert host Linux giữa OpenStack 2023.1/PowerStore iSCSI và OpenStack 2026.1/Ceph 19 hoặc 20. Bộ pin được lấy từ Ubuntu 24.04, Python 3.12.3. Package yêu cầu Python ≥ 3.10; pin dependency trên Python/OS khác vẫn cần nghiệm thu. UI cần systemd hỗ trợ các directive trong units, hướng triển khai là systemd ≥ 250.

Thực hiện bằng root trên host chuyên dụng. Không đặt openrc, keyring, token hoặc log chưa scrub trong repository. Các địa chỉ `192.0.2.x`/`198.51.100.x` chỉ là ví dụ.

## 1. Chuẩn bị host

| Điều kiện | Xác minh |
|---|---|
| Network | Đến được API hai cloud, portal iSCSI nguồn và Ceph public network đích |
| DNS/NTP/CA | Resolve endpoint chính xác, thời gian đồng bộ, CA đúng của từng cloud |
| Disk | Đủ staging root image, log, state, UI raw output; theo dõi dung lượng trong chiến dịch |
| IQN | `/etc/iscsi/initiatorname.iscsi` có IQN duy nhất, không clone từ convert host khác |
| Network đích | Network cùng tên, subnet cùng CIDR; VLAN/route đúng thiết kế; IP không bị hệ IPAM khác cấp |
| Account | Admin hoặc quyền policy tương ứng; password account có role trong mọi project cần scope |
| Change freeze | Không đổi map, flavor, port, volume attachments trong wave |

Nếu hai cloud cùng VLAN, allocation pools phải được quy hoạch không chồng nhau, kể cả IP service/metadata và IP cấp cho VM mới. Fixed IP migration phải còn được giữ ở nguồn cho tới hết cửa sổ rollback. Với `--ip same`, tháo port nguồn theo engine; chỉ disable port có thể làm mạng nguồn vẫn giữ đường tới MAC đó. Nova lock không ngăn mọi hành động của admin, nên không thay detach bằng lock đơn thuần. Đây là điều kiện nghiệm thu network, không phải check đầy đủ của preflight.

```bash
curl -fsSL https://download.ceph.com/keys/release.asc -o /usr/share/keyrings/ceph-release.asc
echo "deb [signed-by=/usr/share/keyrings/ceph-release.asc] https://download.ceph.com/debian-tentacle noble main" \
  > /etc/apt/sources.list.d/ceph-tentacle.list
apt-get update
apt-get install -y open-iscsi qemu-utils qemu-block-extra ceph-common python3-rbd jq \
  python3-venv python3-dev gcc libffi-dev libssl-dev e2fsprogs
systemctl enable --now iscsid
/usr/bin/python3 -I -c 'import rados, rbd; print("rbd bindings OK")'
```

Client Ceph trên host lấy từ repo Tentacle (≥ 20.2.4): client mới đọc được cả cụm Squid 19 lẫn Tentacle 20, còn `ceph-common` 19.x của Ubuntu không đọc được key `aes256k` mà cụm Tentacle ≥ 20.2.4 phát. Repo và bản client phải được đội storage duyệt. `qemu-block-extra` cung cấp backend RBD cho qemu-img. `python3-rbd` thuộc Python hệ thống, không cài thay bằng pip trong venv. Khi SYS_PY khác đường mặc định, đặt trong conf.

## 2. Artifact và venv

Checkout lấy bằng `git clone --branch <tag> --depth 1 https://github.com/nguyenhuukhoi/iscsi-rbd-migration.git` (xem [README.md mục "Cài và chạy thử"](README.md#cài-và-chạy-thử)). Trên máy build từ commit đã review:

```bash
./release.sh
# hoặc make release
```

Script kiểm version engine với pyproject, từ chối thay đổi chưa commit dưới migratetool/ hoặc pyproject; build wheel không dependency, in SHA256 và short commit. Nó không tự chạy tests và không kiểm dirty toàn repo. UI version phải kiểm riêng. Quy trình đầy đủ ở [DEVELOPING.md](DEVELOPING.md).

Chuyển wheel, `.sha256`, `requirements-host.txt` và checkout UI cùng release tới host. Xác nhận hash bằng kênh tin cậy của release:

```bash
cd dist
sha256sum -c migratetool-<version>-py3-none-any.whl.sha256
cd ..
python3 -m venv /opt/mig-venv
/opt/mig-venv/bin/pip install -r requirements-host.txt dist/migratetool-<version>-py3-none-any.whl
ln -sfn /opt/mig-venv/bin/migrate /usr/local/bin/migrate
/opt/mig-venv/bin/migrate --version
```

Host không Internet: chuẩn bị wheelhouse của toàn bộ pin trên máy cùng platform/Python; cài bằng `--no-index --find-links /opt/mig-artifacts/wheels`. Không resolve dependency mới tùy ý trong cửa sổ thay đổi. Extra `host` trong pyproject chỉ pin dependency trực tiếp; release production dùng `requirements-host.txt` để pin cả tập đã ghi nhận.

CLI tự đưa bin của venv lên PATH cho child. Entry point đã cài là đường vận hành chính thức. `python -m migratetool` từ checkout không bảo đảm child `-I` dùng cùng source tree.

### 2.1 OpenStack CLI và SDK

Tool không dùng OpenStack client của hệ điều hành. Mọi gói client nằm trong venv `/opt/mig-venv`:

| Gói | Tool dùng để làm gì |
|---|---|
| `python-openstackclient` (lệnh `openstack`) | Gọi API Nova, Cinder, Neutron, Glance, Keystone của hai cloud |
| `openstacksdk` | Đọc description của volume đích |
| `os-brick`, `python-cinderclient` | Gắn volume nguồn qua iSCSI để copy |
| `keystoneauth1`, `oslo.concurrency`, `oslo.config` | Xác thực và privsep cho os-brick |

Lệnh `pip install -r requirements-host.txt` ở trên đã cài đủ các gói này. Khi chạy, `migrate` đặt `/opt/mig-venv/bin` lên đầu PATH của tiến trình con. Vì vậy lệnh `openstack` được dùng là `/opt/mig-venv/bin/openstack`, không phải bản cài bằng apt. Nếu venv thiếu `openstack`, tiến trình con sẽ rơi xuống bản trên PATH hệ thống, là bản chưa test. Đừng để xảy ra trường hợp này.

Có hai cách cài:

| Cách | Bản được cài | Dùng khi |
|---|---|---|
| A. Bộ đã test (mặc định) | Đúng bản ghi trong `requirements-host.txt`, ví dụ OSC 10.5.0, SDK 4.22.0, os-brick 7.1.1 | Production. Đây là bộ đã chạy gate lab |
| B. Tự chọn theo release OpenStack | Bản trong upper-constraints của release bạn chọn | Muốn client cùng đời với cloud, hoặc tổ chức bắt buộc theo constraints |

**Cách B: tự cài theo release.** Dùng venv mới: `python3 -m venv` không xóa gói đã cài sẵn trong thư mục cũ, nên cài cách B lên venv của cách A sẽ trộn hai bộ gói. Đổi cách cài trên host đang dùng thì làm lúc không có migrate nào chạy, đổi tên venv cũ để giữ đường lui (`mv /opt/mig-venv /opt/mig-venv.old`). Thay `2026.1` bằng release bạn chọn:

```bash
python3 -m venv /opt/mig-venv
/opt/mig-venv/bin/pip install --upgrade pip
/opt/mig-venv/bin/pip install -c https://releases.openstack.org/constraints/upper/2026.1 \
  python-openstackclient openstacksdk os-brick python-cinderclient keystoneauth1 oslo.concurrency oslo.config
/opt/mig-venv/bin/pip install --no-deps dist/migratetool-<version>-py3-none-any.whl
/opt/mig-venv/bin/pip check
ln -sfn /opt/mig-venv/bin/migrate /usr/local/bin/migrate
```

Host ra Internet qua proxy thì đặt `HTTPS_PROXY` trước các lệnh `pip`, vì pip tải cả file constraints lẫn gói qua HTTPS. File constraints ghim mỗi gói đúng một bản, nên pip không tự chọn bản mới nhất. Wheel `migratetool` không khai báo dependency, nên `--no-deps` không bỏ sót gói nào.

Bản trong upper-constraints, đọc ngày 22/11/2026:

| Release | `python-openstackclient` | `openstacksdk` | `os-brick` | `python-cinderclient` | `keystoneauth1` |
|---|---|---|---|---|---|
| 2025.1 | 7.5.1 | 4.4.1 | 6.11.1 | 9.7.0 | 5.10.0 |
| 2025.2 (Python ≥ 3.10) | 8.2.0 | 4.7.2 | 6.13.2 | 9.8.0 | 5.12.0 |
| 2026.1 | 9.0.1 | 4.10.0 | 7.0.0 | 9.9.0 | 5.13.1 |
| Bộ đã test (cách A) | 10.5.0 | 4.22.0 | 7.1.1 | 9.10.0 | 5.18.1 |

Bộ đã test mới hơn cả 2026.1. Cách B vì vậy luôn cài bản **chưa chạy gate lab**. Lý do cần cẩn thận: tool đọc chữ OSC in ra. Ví dụ OSC 10.5 báo xóa xong bằng câu `No Server found for <id>`, khác câu của bản cũ hơn. Trước khi tool nhận câu này, rollback chờ đủ 600 giây rồi mới dừng. Sau khi cài theo cách B, chạy đủ các bước sau trên một VM thử trước khi dùng thật:

1. `migrate doctor`: mọi dòng OK.
2. `migrate plan <vm thử>`: plan OK, không có dòng lạ.
3. `migrate migrate <vm thử> --yes`, rồi `migrate rollback <vm thử> --yes`: cả hai trả mã 0, rollback xong trong vài phút chứ không chờ hết hạn.

Có bước nào lỗi thì quay về cách A.

**Kiểm tra bản đang cài**, áp dụng cho cả hai cách:

```bash
/opt/mig-venv/bin/openstack --version
/opt/mig-venv/bin/pip list 2>/dev/null | grep -Ei '^(python-openstackclient|openstacksdk|os-brick|python-cinderclient|keystoneauth1) '
/opt/mig-venv/bin/pip check
```

Kết quả mong đợi với cách A: dòng đầu in `openstack 10.5.0`, và `pip check` in `No broken requirements found.`

**Host không có Internet.** Trên một máy cùng hệ điều hành và cùng bản Python, tải trước wheel theo đúng cách đã chọn:

```bash
# cách A
pip download -r requirements-host.txt -d /opt/mig-artifacts/wheels
# cách B
pip download -c https://releases.openstack.org/constraints/upper/2026.1 -d /opt/mig-artifacts/wheels \
  python-openstackclient openstacksdk os-brick python-cinderclient keystoneauth1 oslo.concurrency oslo.config
```

Chép thư mục `wheels` sang host. Khi cài, thay `-c <url>` bằng `-c <file constraints đã tải về>` và thêm `--no-index --find-links /opt/mig-artifacts/wheels` vào mỗi lệnh `pip install`.

## 3. Openrc hai cloud

```bash
install -d -m 0700 -o root -g root /opt/mig /opt/mig/src /opt/mig/dst
install -m 0600 -o root -g root /dev/null /opt/mig/src/admin-openrc.sh
install -m 0600 -o root -g root /dev/null /opt/mig/dst/admin-openrc.sh
```

Điền credentials bằng trình soạn thảo hoặc secret manager, không qua history/argv. Mỗi file chỉ dùng assignment/export/unset cần thiết. **Không có `read`, prompt, lệnh tương tác hoặc lệnh có thể trả nonzero vô tình**. Engine source dưới Bash `set -e`, env đã bỏ `OS_*`; không dựa biến OS của shell gọi.

Mẫu password cho nguồn, thay placeholder trong file bằng giá trị do quản trị cấp:

```bash
export OS_AUTH_TYPE=password
export OS_AUTH_URL='https://192.0.2.10:5000/v3'
export OS_IDENTITY_API_VERSION=3
export OS_REGION_NAME=RegionOne
export OS_INTERFACE=public
export OS_USER_DOMAIN_NAME=Default
export OS_PROJECT_DOMAIN_NAME=Default
export OS_USERNAME='CHANGE_ME'
export OS_PASSWORD='CHANGE_ME'
export OS_PROJECT_NAME='finance'
export OS_CACERT='/opt/mig/src/ca.pem'
```

Đích dùng URL riêng, ví dụ `https://198.51.100.10:5000/v3`, CA riêng và account có role trong project đích. Mẫu IP yêu cầu certificate hợp lệ cho endpoint thực tế; production dùng DNS/cert đã duyệt. Không dùng `OS_INSECURE` như cách sửa lỗi CA thông thường.

Mẫu application credential cho phạm vi một project:

```bash
export OS_AUTH_TYPE=v3applicationcredential
export OS_AUTH_URL='https://192.0.2.10:5000/v3'
export OS_IDENTITY_API_VERSION=3
export OS_REGION_NAME=RegionOne
export OS_INTERFACE=public
export OS_APPLICATION_CREDENTIAL_ID='CHANGE_ME'
export OS_APPLICATION_CREDENTIAL_SECRET='CHANGE_ME'
export OS_CACERT='/opt/mig/src/ca.pem'
```

App credential có thể dùng cho nhiều VM trong **cùng** project của nó. Không dùng để rescope sang project khác. Scope đích cùng tên hoặc theo map phải phù hợp credential đích. Password openrc dùng đa project; mọi project cần có sẵn và có role đúng domain.

```bash
chown root:root /opt/mig/src/admin-openrc.sh /opt/mig/dst/admin-openrc.sh
chmod 0600 /opt/mig/src/admin-openrc.sh /opt/mig/dst/admin-openrc.sh
```

Doctor kiểm token hai phía, quyền file, Keystone trùng, và session brick với CA/interface/region nguồn. Session brick được token không chứng minh toàn bộ attach storage; canary mới xác minh đường đó. REST Glance chọn interface nhưng chưa lọc region; catalog nhiều region phải kiểm endpoint thực tế trước VM ephemeral.

## 4. Ceph và iSCSI PowerStore

### Ceph

Pool/type/backend phải tồn tại. Đội Cinder xác nhận `volume-<UUID>` của type nào thuộc pool nào trước cấu hình map. Trên cluster đích, cấp Ceph user chỉ cho pool cần dùng; ví dụ cho pool `volumes-az1`:

```bash
ceph auth get-or-create client.mig-xfer mon 'profile rbd' \
  osd 'profile rbd pool=volumes-az1' mgr 'profile rbd pool=volumes-az1' \
  -o /root/ceph.client.mig-xfer.keyring
```

Đội storage mở rộng caps cho mọi pool trong map, không dùng wildcard toàn cluster cho thuận tiện. Chuyển `ceph.conf` và keyring bằng kênh quản trị bảo mật tới convert host:

```bash
chown root:root /etc/ceph/ceph.conf /etc/ceph/ceph.client.mig-xfer.keyring
chmod 0600 /etc/ceph/ceph.client.mig-xfer.keyring
rbd -c /etc/ceph/ceph.conf --id mig-xfer -p volumes-az1 ls
```

`rbd ls` chỉ chứng minh quyền list. `doctor --write-check` kiểm đường ghi/đọc. `ceph df detail` có thể không được caps cho phép; preflight khi đó INFO bỏ kiểm capacity. Đội storage phải cung cấp số headroom độc lập. Không mở rộng caps tự động chỉ để làm mất INFO.

### Single-path

Đặt `MIG_MULTIPATH=0` và `MIG_ISCSI_IP` bằng IP NIC iSCSI convert host. Đây là lựa chọn triển khai PowerStore single-path có chủ đích, không suy từ số portal mà driver trả về. Không có failover path trong lúc copy; tránh cửa sổ bảo trì SAN/mạng.

Nếu multipathd còn chạy, blacklist các WWID dành cho migration trong `/etc/multipath.conf` theo inventory mảng đã xác nhận:

```text
blacklist {
    wwid "<exact-migration-lun-wwid>"
}
```

Clone mới có WWID mới. Đội storage phải xác nhận blacklist bao phủ clone sẽ dùng; một WWID canary không bao phủ mọi wave. Nếu host chuyên dụng không có thiết bị cần multipath, có thể vô hiệu multipathd theo chính sách host. Không flush device đang dùng. Kiểm cấu hình thực tế trước canary; không để multipathd chiếm `/dev/sdX` rồi làm detach báo busy.

Trong canary, đối chiếu clone `mig-clone-<source-volume-id>` với volume/WWN trên PowerStore, không so với volume gốc. Kiểm portal qua `iscsiadm -m session`, owner/optimized path trên mảng, device qua log và `lsblk`. Record attach `/var/lib/mig-attach/<clone-id>.json` có `scsi_wwn`; sau detach có `.detached.json`. Kích thước khớp không chứng minh LUN đúng. Checksum trong guest phát hiện trường hợp đọc nhầm LUN cùng cỡ mà compare cùng device không phát hiện.

## 5. Conf và khóa host

Tạo `/opt/mig/migrate.conf` root 0600. Bảng đầy đủ khóa và precedence ở [GUIDE.md](GUIDE.md#4-cấu-hình-và-map). Mẫu tối thiểu sau phải được điều chỉnh theo backend đã kiểm:

```ini
SRC_RC=/opt/mig/src/admin-openrc.sh
DST_RC=/opt/mig/dst/admin-openrc.sh
RBD_USER=mig-xfer
RBD_POOL=volumes-az1
RBD_POOL_MAP="rbd-az1:volumes-az1"
CEPH_CONF=/etc/ceph/ceph.conf
MIG_ISCSI_IP=192.0.2.20
MIG_MULTIPATH=0
COPY_STREAMS=8
MIG_LOCK=/run/mig/migrate.lock
MIG_CMD_TIMEOUT=600
MIG_COLD_WARN_GB=500
MIG_DEFAULT_OPTS="--verify full"
```

```bash
chown root:root /opt/mig/migrate.conf
chmod 0600 /opt/mig/migrate.conf
install -d -m 0700 -o root -g root /run/mig /var/lib/mig-migrate /var/log/mig-batch
printf '%s\n' 'd /run/mig 0700 root root -' > /etc/tmpfiles.d/migrate.conf
systemd-tmpfiles --create /etc/tmpfiles.d/migrate.conf
migrate doctor --write-check
```

Mọi CLI/UI trên host phải dùng cùng `MIG_LOCK`. Không xóa file khóa đang được giữ để “giải phóng”: inode mới cho phép hai run. Không tự đặt `MIG_LOCK_FD`. `/run` được tạo lại sau reboot, nên có tmpfiles rule.

## 6. Cài web UI

Từ checkout đúng release, lúc idle:

```bash
sudo MIGRATE=/opt/mig-venv/bin/migrate migui/deploy/install.sh --no-start
sudo /opt/migui/bin/migui-passwd add operator
sudo systemctl enable --now migui-runner migui-web
```

Installer copy package migui và `DEPLOY.md` của cùng checkout vào `/opt/migui/DEPLOY.md`, file mà `Documentation=` của units trỏ tới. Chạy installer không `--no-start` sẽ enable/restart cả hai unit. Accounts được giữ; session web bị mất khi restart.

| Path | Owner/mode | Vai trò |
|---|---|---|
| `/opt/migui/{bin,lib/migui}` | root 0755, code 0644 | Wrappers system Python `-I`; runner giữ path migrate đã resolve |
| `/var/lib/migui` | root:migui 0750 | Accounts và thư mục jobs |
| `/var/lib/migui/users` | root:migui 0640 | Scrypt hashes, không password plaintext |
| `/var/lib/migui/users.lock` | root 0600 | Khóa đổi account |
| `/var/lib/migui/jobs` | root 0700 | JSON 0600→0400 khi kết thúc, log/out 0600 |
| `/var/log/migui/audit.jsonl` | root:migui 0620, `+a` nếu hỗ trợ | Append audit |
| `/run/migui` | root:migui 0750 | Socket 0660 root:migui |

Kiểm audit thay vì chỉ tin installer:

```bash
lsattr -d /var/log/migui/audit.jsonl
stat -c '%U:%G %a %n' /var/log/migui/audit.jsonl
journalctl -u migui-runner -u migui-web --since today
```

Nếu filesystem không có `chattr +a`, installer WARN: UID migui bị chiếm có thể truncate audit. Đưa audit tới filesystem hỗ trợ hoặc gửi sang hệ thu thập độc lập. Mutating POST không ghi được audit trả 503; không giải quyết bằng chmod 777. Rotation cần quy trình root cho append-only file, không truncate tùy tiện trong ca.

Quản lý account:

```bash
sudo /opt/migui/bin/migui-passwd list
sudo /opt/migui/bin/migui-passwd set operator
sudo /opt/migui/bin/migui-passwd del operator
```

`add`/`set` hỏi password hai lần không echo. `--stdin` đọc một dòng cho automation qua secret manager, không truyền password bằng argv/env. Username chữ thường đầu tiên, sau đó a-z/0-9/._-, tối đa 32; password printable 12–128, không bằng username. Đổi/xóa account làm phiên bị thu hồi ở request tương tác kế tiếp. Không xóa account cuối khi chưa có cách đăng nhập khác.

Workstation mở tunnel cùng port:

```bash
ssh -L 8787:127.0.0.1:8787 operator@192.0.2.20
```

Mở `http://127.0.0.1:8787/`. Port local khác cần web `--origin-port` qua drop-in `ExecStart`; không nới bind ra public. Web sandbox chỉ cho loopback và socket; runner cần quyền network/devices cho child migrate, không áp sandbox PrivateDevices/PrivateNetwork của web lên runner.

### Hook thông báo

Tạo executable root-owned `/opt/mig/notify` theo tích hợp nội bộ. Hook nhận env MIG_EVENT/MIG_JOB/MIG_STATE/MIG_BATCH/MIG_NAME/MIG_RC/MIG_OPERATOR; không tự gửi notification khi chưa cấu hình. Hook không chứa secret trong argv. Cấu hình bằng `systemctl edit migui-runner`:

```ini
[Service]
Environment=MIG_CONF=/opt/mig/migrate.conf
Environment='MIGUI_NOTIFY_CMD=["/opt/mig/notify"]'
```

```bash
systemctl daemon-reload
# Chỉ khi không còn job:
systemctl restart migui-runner
```

JSON argv không qua shell, timeout 30 s. Kiểm `notify_rc` và log hook sau canary; hook lỗi không đổi kết quả migration. Lưu ý [giới hạn delivery](DESIGN.md#84-thông-báo).

## 7. Nâng cấp, hạ cấp, gỡ

Host cài bằng `git clone` theo README thì nâng cấp theo [mục Nâng cấp của README](README.md#nâng-cấp); mục này cho host cài bằng wheel và pin (artifact, không lấy gói từ mạng).

1. Dừng nhận wave mới. Chờ CLI/UI idle, kiểm cả `migrate status --json` và processes; DETACHED có thể vẫn chạy.
2. Lưu binary/version, wheel/hash, pin, conf/maps, state và job/audit vào nơi bảo mật. Giữ state nguyên quyền; không sửa marker.
3. Sao lưu venv và UI đang chạy bằng bản copy giữ nguyên đường dẫn gốc: `cp -a /opt/mig-venv /opt/mig-venv.bak-<version>` và `cp -a /opt/migui /opt/migui.bak-<version>`. Venv không di chuyển được: script trong `bin/` ghi cứng đường dẫn Python của venv, nên **không** `mv` venv đang dùng sang tên khác.
4. Cài pin và wheel mới vào `/opt/mig-venv`, không lấy gói từ mạng: `/opt/mig-venv/bin/pip install --no-index -r requirements-host.txt dist/migratetool-<version>-py3-none-any.whl` rồi `/opt/mig-venv/bin/pip check`. Thiếu một gói đã pin thì lệnh dừng, venv cũ vẫn nguyên.
5. Chạy installer UI từ checkout tương ứng với `MIGRATE=/opt/mig-venv/bin/migrate`. Installer resolve path thật vào wrapper và copy lại `DEPLOY.md`.
6. Kiểm version engine/UI, Doctor, write-check, canary và rollback. Ghi kết quả release.

Hạ cấp khi idle: `rm -rf /opt/mig-venv && mv /opt/mig-venv.bak-<version> /opt/mig-venv`, tương tự cho `/opt/migui`, rồi `systemctl restart migui-runner migui-web` và Doctor/canary. Bản sao lưu nằm đúng đường dẫn gốc nên script trong venv chạy lại được. **Không cam kết state tương thích hai chiều với binary cũ**. State có owner_mark, root ephemeral BFV và marker mới có thể không được binary cũ hiểu đúng. Còn state sống thì ưu tiên binary đã tạo nó để điều tra/rollback; rehearsal hạ cấp trên bản sao lab trước. Không chạy binary cũ song song để lách khóa.

Gỡ UI khi idle:

```bash
sudo migui/deploy/install.sh --uninstall
```

Giữ jobs/accounts/audit để tra cứu. `--purge` xóa cả `/var/lib/migui`, audit và user migui: chỉ dùng sau khi duyệt retention, xuất bằng chứng. Gỡ package bằng pip uninstall trong đúng venv và bỏ symlink CLI sau khi xác nhận không còn state cần xử lý. Gỡ không tự xóa cloud object hoặc state migration. Không xóa credentials/keyring còn cần cho rollback trước khi đóng chiến dịch.

## 8. Troubleshooting

`<...>` dưới đây là phần động; phần tiếng Anh còn lại theo code. Đừng sửa cloud chỉ để làm mất thông báo trước khi biết state/phase.

| Error / dòng log | Kiểm và xử lý |
|---|---|
| `qemu-img: not found on PATH` | Cài qemu-utils/qemu-block-extra; kiểm PATH engine |
| `No module named 'os_brick'` | Cài đủ requirements-host trong venv đang chạy; Doctor lại |
| `openstack: not found on PATH` | Venv thiếu OSC: cài lại theo mục 2.1, rồi kiểm `/opt/mig-venv/bin/openstack --version` |
| `pip check` báo xung đột sau khi tự cài | Gói cài từ hai nguồn khác nhau. Xóa venv, cài lại bằng đúng một cách của mục 2.1 |
| Rollback chờ tới hết hạn (hoặc dừng `cannot read destination ...`) dù đối tượng đích đã bị xóa, sau khi đổi bản OSC | Tool nhận 4 dạng "không tìm thấy" có nêu đúng ID (`No <Kind> found for <id>`, `No <kind> with a name or ID of '<id>' exists`, `<id> could not be found`, dòng kết thúc bằng `(HTTP 404)`/`404`) và một dòng nêu đúng ID có `(HTTP 404)` ở bất kỳ đâu hoặc token `404` đứng riêng theo sau là `Not Found`/hết dòng. Bản OSC in dạng khác thì quay về bộ đã test (mục 2.1, cách A) |
| `this state was written by a run scoped with SRC_PROJECT=... DST_PROJECT=... (a password openrc); ... is an application credential, which cannot be rescoped` | Rollback state ghi bằng password openrc có scope mà openrc hiện tại là application credential: chạy rollback bằng đúng password openrc đã migrate (không cần đặt `SRC_PROJECT`/`DST_PROJECT`, tool lấy từ state) |
| `WARN cannot read destination port <id> (...)` / `WARN cannot delete destination port <id> (...)` khi rollback | Rollback vẫn chạy tiếp và trả nguồn; port đích có thể còn giữ IP. Kiểm `openstack port show <id>` ở đích bằng quyền đủ rồi xóa tay |
| `WARN cannot list destination images named mig-snap-<vm>: skipped` | Image tạm có thể còn trong pool đích; image đã ghi trong state vẫn bị xóa. Kiểm `openstack image list --name mig-snap-<vm>` ở đích rồi xóa tay |
| `SYS_PY=... cannot import rados, rbd` | Cài python3-rbd hệ thống; kiểm SYS_PY conf thắng env |
| `a command in it failed` | Openrc có lệnh trả nonzero; sửa file thành dữ liệu thuần, không tương tác |
| `line N asks for input ('read')` | Openrc tải từ Horizon hỏi mật khẩu bằng `read`; migrate source không có terminal. Ghi mật khẩu vào file (root 0600) hoặc dùng application credential |
| `MIG_ISCSI_IP is not set` | Đặt `MIG_ISCSI_IP` trong conf bằng địa chỉ của host trên mạng iSCSI; dòng lỗi liệt kê các địa chỉ hiện có |
| `MIG_ISCSI_IP ... is not an address of this host` | Giá trị chép từ host khác hoặc lab; đặt lại đúng địa chỉ NIC iSCSI của host này |
| `sets no OS_AUTH_URL` | Openrc chưa khai auth URL |
| `SRC_RC and DST_RC point at the same Keystone ...` | Xác nhận hai cloud và hai rc độc lập |
| `SRC_PROJECT/DST_PROJECT needs a password openrc: an application credential is bound to one project (...)` | Dùng đúng scope appcred hoặc password account đa project |
| `PROJECT_MAP_FILE ... is not a readable file` | Sửa path/quyền; không bỏ map để chạy nhầm project |
| `VOLUME_TYPE_MAP: entry ...` / `VOLUME_TYPE_MAP: '*' is only a source key` / `destination volume type <t> (VOLUME_TYPE_MAP (<src>)) does not exist` | Map sai dạng hoặc `*` đứng ở vế đích (tool dừng trước mọi lệnh cloud), hoặc type đích không có ở đích (preflight FAIL, chưa đụng nguồn). Sửa map theo [GUIDE.md mục 4.2](GUIDE.md#42-az_type_map-volume_type_map-và-rbd_pool_map--type-và-pool-của-từng-volume); `migrate doctor` kiểm từng type đích của map |
| `WARN source type <s> has no entry in VOLUME_TYPE_MAP and no '*' entry: lands on <type>` | Volume đó về type của lần chạy. Thêm `<s>:<type>` hoặc fallback `*:<type>` vào map. Conf viết `__DEFAULT__:<type>` với ý fallback không còn là fallback: `__DEFAULT__` là tên type mặc định thật của Cinder, chỉ khớp volume có type tên đó; đổi thành `*:<type>` |
| `volume-... not visible in pool ...: wrong RBD_POOL_MAP for type '...', or ... lacks caps on that pool` | Xác minh type→backend→pool/caps; xử lý state trước rerun |
| `another migrate, volume or rollback runs on this host` | Kiểm process và khóa; không xóa lock file |
| `is in use by another migrate or rollback` | State đang bị flock; tìm process giữ |
| `state exists in ...: run rollback first (that run stopped the source at ...)` | Xem RUNBOOK sự cố cutover; không xóa state |
| `HALF-MADE COPY` | So rule SG; dừng wave, chỉ tái tạo sau khi xác minh không ai dùng |
| `timed out after ... s` | Control-plane deadline 124; xác minh thao tác đã được API nhận chưa |
| `migui-runner is not reachable` | Socket/unit runner, peer user, journald |
| `wrong username or password` | Không phân biệt tên sai/password sai; kiểm lockout và account |
| `scrypt unavailable or memory limit` | Python/OpenSSL và tài nguyên hash; không giảm quy tắc password tùy tiện |
| HTTP `503` có `audit` | Kiểm append permission, filesystem đầy và audit +a; sửa trước POST tiếp |
| HTTP `421` | Dùng hostname/port tunnel hợp lệ |

Mã HTTP, xác nhận và session khác nằm trong [UI.md](UI.md). Sự cố đang cutover nằm trong [RUNBOOK.md](RUNBOOK.md).
