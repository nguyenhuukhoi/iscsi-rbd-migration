# Triển khai app `migrate` (bản Python, v7) lên host convert

Hướng dẫn từng bước: clone repo → host convert chạy được lệnh `migrate`. Hệ đích nhắm tới giống bản bash:
nguồn OpenStack **2023.1** iSCSI/PowerStore, đích **2026.1** Ceph **19.x**, host convert đi **1 path** tới SAN.
Cách dùng lệnh: [GUIDE-PYTHON.md](GUIDE-PYTHON.md). Cài bản bash cũ: [DEPLOY.md](DEPLOY.md) (script nay ở
[archive/](archive/README.md)).

Các mục 1, 2, 5, 6, 7 dưới đây giống hệt bản bash. Ở đó tài liệu chỉ nhắc lại điểm chính và trỏ sang
[DEPLOY.md](DEPLOY.md). Phần mới là mục 3, 4, 8, 9.

## 0. App gồm những gì

| Thành phần | Nằm ở | Chạy bằng |
|---|---|---|
| Lệnh `migrate` | `/opt/mig-venv/bin/migrate` (symlink sang `/usr/local/bin/migrate`) | Python của venv `/opt/mig-venv` |
| Package `migratetool` | `/opt/mig-venv/lib/python3.*/site-packages/migratetool/` | |
| Gắn volume (thay `brick_attach.py`) | `migratetool/brick.py`, chạy thành tiến trình con `python -I -m migratetool.brick` (`-I` từ 7.1) | Python của venv (cần os-brick) |
| Live sync (thay `blocksync.py`) | `migratetool/blocksync.py`, giống từng byte bản cũ | **Python hệ thống** `SYS_PY` (mặc định `/usr/bin/python3`, cần `python3-rbd`), chạy với `-I` |

Không còn file nào phải copy vào `/usr/local/bin` ngoài symlink `migrate`. Một lần `pip install` cài đủ cả
hai đầu của hợp đồng JSON giữa `migrate` và blocksync, nên hai bên không lệch phiên bản nhau được.

## 1. Yêu cầu host convert

Như [DEPLOY.md](DEPLOY.md) mục 1:

- Ubuntu 24.04, quyền root.
- Tới được API 2 cloud, mạng iSCSI của SAN nguồn và mạng public của Ceph đích.
- Đĩa trống đủ chứa image của VM ephemeral.
- Initiator IQN duy nhất.

Thêm: Python **≥ 3.10**. Ubuntu 24.04 có sẵn 3.12.

## 2. Gói hệ thống

```bash
apt-get update
apt-get install -y open-iscsi qemu-utils qemu-block-extra ceph-common python3-rbd jq \
  python3-venv python3-dev gcc libffi-dev libssl-dev
systemctl enable --now iscsid
```

`python3-rbd` là bắt buộc cho `--mode live`. Blocksync đọc RBD qua librbd của **python hệ thống**, không
phải của venv. Cài `rbd` bằng pip vào venv thì không có tác dụng. Python hệ thống nằm chỗ khác thì đặt
`SYS_PY=/đường/dẫn/python3` trong `/opt/mig/migrate.conf`; như bản bash, giá trị trong conf thắng env.

## 3. Venv `/opt/mig-venv`

Venv này giống bản bash, đã có thì dùng lại:

```bash
python3 -m venv /opt/mig-venv
/opt/mig-venv/bin/pip install -U pip setuptools wheel
/opt/mig-venv/bin/pip install \
  -c https://releases.openstack.org/constraints/upper/2026.1 \
  python-openstackclient python-cinderclient python-brick-cinderclient-ext os-brick
```

`setuptools` (≥ 61) và `wheel` là bắt buộc vì bước cài app dùng `--no-build-isolation`: pip build bằng đúng
setuptools/wheel của venv, không tự tải bản mới. Venv tạo trên Ubuntu mà bỏ qua dòng đó thì
`pip install --no-build-isolation .` chết với đúng hai dấu hiệu này: metadata ra `UNKNOWN.egg-info`
(setuptools cũ không đọc được `[project]` trong pyproject.toml) và `error: invalid command 'bdist_wheel'`
(thiếu wheel) — chạy lại dòng `pip install -U pip setuptools wheel` là hết.

App gọi lệnh `openstack` của venv qua subprocess, như bản bash: V7 chưa chuyển sang openstacksdk. Module
`brick` import os-brick từ venv. Một bộ client 2026.1 dùng được cho cả 2 cloud. Với cloud nguồn chỉ cần
Cinder microversion ≥ 3.54; 2023.1 có tới 3.70.

## 4. Cài app

```bash
git clone https://github.com/nguyenhuukhoi/iscsi-rbd-migration.git   # đã có repo thì: git pull
cd iscsi-rbd-migration
git checkout <nhánh hoặc tag bản v7>
/opt/mig-venv/bin/python -c 'import setuptools' || /opt/mig-venv/bin/pip install 'setuptools>=61'
/opt/mig-venv/bin/pip install --no-build-isolation .
ln -sf /opt/mig-venv/bin/migrate /usr/local/bin/migrate
migrate --version            # migrate 7.0.0.dev1
```

- **`--no-build-isolation`**: nếu thiếu cờ này, pip dựng một môi trường build tạm rồi tải `setuptools` từ
  PyPI. Host convert thường không ra Internet trực tiếp nên bước đó treo hoặc lỗi. Có cờ thì pip dùng luôn
  `setuptools` sẵn trong venv. Venv có openstackclient thường đã có `setuptools`, vì `pbr` kéo nó theo.
  Lab dùng bản 84.2.1.
- **Symlink** `/usr/local/bin/migrate` giúp chạy `migrate` mà không phải `source activate`. App tự đặt
  `bin/` của venv lên đầu `PATH` cho mọi tiến trình con, giống dòng `source /opt/mig-venv/bin/activate`
  của bản bash. Vì vậy lệnh `openstack` mà nó gọi luôn là lệnh của venv.
- **Nâng cấp**: `git pull`, rồi chạy lại đúng lệnh `pip install --no-build-isolation .`. Không nâng cấp
  khi đang có lần migrate chạy dở. State thì không cần lo, vì format state không đổi giữa các bản.
- **Gỡ**: `/opt/mig-venv/bin/pip uninstall migratetool && rm /usr/local/bin/migrate`. State và log không bị đụng.
- Đừng chạy `migrate` từ bên trong thư mục repo bằng `python -m migratetool`. Đường chính thức là lệnh đã
  cài. Tiến trình con của mỗi VM chạy `python -I -m migratetool.single` nên luôn dùng package đã cài, kể cả
  khi thư mục hiện tại có `migratetool/` của repo.

## 5. Soát `/opt/mig/migrate.conf` production trước khi chuyển

App đọc **đúng file conf cũ, cùng tên biến**, không phải sửa gì, với một điều kiện: file chỉ được chứa dữ
liệu. Bản bash `source` file nên câu lệnh bash nào cũng chạy được. App thì chỉ nhận:

- dòng `KEY=VALUE`, có thể có `export` đứng trước, có nháy đơn/kép, comment `#`;
- nhiều phép gán trên một dòng.

Gặp thứ chỉ bash hiểu được, app **dừng kèm số dòng** trước khi chạm vào cloud nào:

```
ERROR: /opt/mig/migrate.conf line 7: $-expansion is not supported by migrate, write the value literally: RBD_POOL_MAP="$POOLS"
ERROR: /opt/mig/migrate.conf line 12: only KEY=VALUE assignments are supported, got: source /opt/mig/site.conf
```

Soát một lần, không đụng cloud:

```bash
/opt/mig-venv/bin/python -c 'from migratetool import config; c = config.load(); print(c.conf_path); [print(k, "=", repr(v)) for k, v in sorted(c.values.items())]'
```

Lệnh in đúng giá trị app sẽ dùng, sau khi đã áp env > conf > mặc định. Có dòng lỗi thì sửa thành giá trị
viết thẳng, ví dụ `RBD_POOL_MAP="nvme-az1:volumes-nvme-az1"` thay cho `RBD_POOL_MAP="$POOLS"`. Biến ngoài
danh sách conf mà có `export` (ví dụ `export OS_CACERT=/etc/ssl/certs/ca.pem`) vẫn được truyền xuống mọi
lệnh con, như khi bash `source`.

Mẫu đủ mọi biến: [migrate.conf.example](migrate.conf.example). Ý nghĩa từng biến và 4 map:
[GUIDE-PYTHON.md](GUIDE-PYTHON.md) mục 7.

## 6. Ceph đích, openrc 2 cloud, iSCSI 1 path

Không đổi so với bản bash, làm theo [DEPLOY.md](DEPLOY.md):

- Mục 5: `ceph.conf` + keyring `client.mig-xfer`.
- Mục 6: openrc tại `/opt/mig/{src,dst}/admin-openrc.sh`, `chmod 600`, khuyến nghị application credential.
- Mục 7: `MIG_MULTIPATH=0`, chặn multipathd, `MIG_ISCSI_IP`.

Khác ở mục 6, về application credential (appcred):

| | Bản bash | App |
|---|---|---|
| Chạy 1 VM thuộc **đúng project của appcred** | Được | Được |
| Chạy theo đợt, VM thuộc đúng project của appcred | Batch từ chối appcred | **Được** |
| VM thuộc project khác | Bash từ chối (`SRC_PROJECT/DST_PROJECT needs a password openrc …`) | Hàng đó `PLAN-FAIL` với cùng message. Muốn chạy đa project thì dùng password openrc |

Account password chạy đa project vẫn cần role trong mọi project liên quan ở cả 2 cloud, và quyền admin ở
nguồn để liệt kê `--all-projects`.

## 7. Map project và các map khác

Giống bản bash: `PROJECT_MAP_FILE`, `FLAVOR_MAP_FILE`, `RBD_POOL_MAP`, `AZ_TYPE_MAP`. Ví dụ từng dòng ở
[GUIDE-PYTHON.md](GUIDE-PYTHON.md) mục 7.4.

**Khác bash: `PROJECT_MAP_FILE` giờ áp cho cả lệnh 1 VM.** Trước đây chạy lẻ `migrate-vm.sh` với project
đích khác tên thì phải tự đặt `DST_PROJECT=…`. Giờ app tự đặt project theo VM và đè env đó, nên **mọi cặp
project khác tên phải nằm trong file map**.

## 8. Chuyển từ bản bash sang app

**State tương thích 2 chiều.** Hai bản dùng chung `/var/lib/mig-migrate/<vm>/` và `vol-<id>/`, cùng
từng file, cùng format.

- State do `migrate-vm.sh` tạo thì `migrate rollback` dọn được.
- State do `migrate` tạo thì `migrate-vm.sh rollback` dọn được.
- Đã kiểm bằng harness giả lập: 4 kịch bản chéo VM, 4 kịch bản chéo volume, kể cả volume mồ côi và
  server group.

**Hai bản chạy song song được** trong thời gian chuyển: script bash cũ trong `/usr/local/bin` cứ để đó,
app cài vào venv. Cả hai đọc chung conf và state, và guard `volume-map` chặn chạy đè lên cùng một VM:
bash báo `state exists`, app báo `SKIP(state-exists)`. Không cần gỡ bản bash ngay.

**Thói quen phải đổi:**

| Thói quen bash | Với app |
|---|---|
| Script/cron gọi `migrate-vm.sh migrate <vm>` chạy thẳng | Phải thêm `--yes`. Thiếu thì app dừng (`stdin is not a terminal …`) |
| Batch đặt option sau `--` | Option cùng dòng, không có `--` |
| Chạy lẻ với `SRC_PROJECT=… DST_PROJECT=…` | Không cần nữa. Project khác tên: `PROJECT_MAP_FILE` (mục 7) |
| Rollback batch luôn bằng ID | Rollback bằng đúng chữ đã gõ; `--host`/`--project` thì vẫn theo ID |
| Batch exit 0 khi mọi hàng SKIP | Exit 1 |

Bảng đổi lệnh đầy đủ: [GUIDE-PYTHON.md](GUIDE-PYTHON.md) mục 10.

**Quay về bản bash** (nếu cần): state không phải đụng. Cài lại 4 file từ `archive/`:

```bash
install -m 0755 archive/migrate-vm.sh archive/migrate-batch.sh archive/brick_attach.py archive/blocksync.py /usr/local/bin/
```

## 9. Verify trước khi chạy thật

```bash
ceph -n client.mig-xfer --keyring /etc/ceph/ceph.client.mig-xfer.keyring -s   # thấy cluster đích
rbd  -n client.mig-xfer ls <pool>                                             # đọc được pool
/usr/bin/python3 -c 'import rbd, rados; print("rbd bindings OK")'             # live mode
migrate --version
migrate plan <vm-test>                        # 1 VM: liệt kê + preflight, không đổi gì trên cloud
migrate plan <vm-test> --mode live            # thêm 2 dòng: OK blocksync present, OK librbd python bindings
migrate plan volume --project <p> --available # volume rời sẽ được copy (READY), không đổi gì
```

Smoke-test blocksync không đụng VM (tùy chọn). Test cần quyền tạo image RBD trong pool `mig-volumes`; pool
khác thì sửa dòng `POOL=` của test. `tests/blocksync-unit.sh` gọi `blocksync.py` qua `PATH`, nên trỏ tạm
vào bản trong package. Chạy trong shell **không** activate venv: script có `#!/usr/bin/env python3`, nếu
`python3` là của venv thì không có librbd và cả 8 check đều FAIL oan.

```bash
PKG=$(/opt/mig-venv/bin/python -c 'import migratetool, os; print(os.path.dirname(migratetool.__file__))')
mkdir -p /tmp/bs && ln -sf "$PKG/blocksync.py" /tmp/bs/blocksync.py
PATH=/tmp/bs:$PATH bash tests/blocksync-unit.sh        # kỳ vọng PASS=8 FAIL=0; PATH không có /opt/mig-venv/bin
```

## 10. Chạy theo thứ tự an toàn

```bash
migrate plan <vm-test>                          # 1. chỉ đọc
migrate migrate <vm-test> --yes                 # 2. cold, VM test trước
migrate rollback <vm-test>                      #    thử luôn hoàn tác
migrate migrate <vm-test2> --mode live --yes    # 3. rồi mới live
migrate plan --host <compute-host>              # 4. cả host: liệt kê + plan, không đổi gì
migrate migrate --host <compute-host> --yes     # 5. tuần tự, dừng ở con FAIL đầu tiên
```

- `<compute-host>` lấy ở cột Host của `openstack compute service list`.
- Hàng nào FAIL thì chạy đúng lệnh rollback mà app in sẵn, đã có đủ cặp project.

Kiểm WWN với PowerStore không đổi:

- Device đang gắn là clone `mig-clone-<id-volume-nguồn>`.
- Bản ghi attach nằm ở `/var/lib/mig-attach/<clone-id>.json`, trường `scsi_wwn`. File này bị xóa khi
  detach, nên phải đọc trong lúc copy.

Cách đọc: [DEPLOY.md](DEPLOY.md) mục 9.

Vị trí state và log:

- VM: `/var/lib/mig-migrate/<chữ đã gõ hoặc ID>/`.
- Volume rời: `/var/lib/mig-migrate/vol-<id>/`.
- Log mỗi lần chạy: `/var/log/mig-batch/<YYYYmmdd-HHMMSS>/`.
- Lỗi attach: `/var/lib/mig-migrate/brick-attach.err`.

## 11. Những gì chưa test trên hệ thật

Mọi mục "chưa test" của bản bash vẫn còn nguyên: [DEPLOY.md](DEPLOY.md) mục 10 (SAN thật, multipath Dell,
`--keep-server-group` với rules, microversion < 2.64…).

7.1 (P6) đã qua lab 19/11 (gate chốt 183/0, gồm 27 check P6 trên cloud thật — TEST-REPORT-20261118-v7.md
mục "Gate lab 7.1"). Còn CHƯA test trên hệ thật:

- **Policy Neutron production với creds member**: tạo port mang pairs / tắt port security mặc định upstream cần
  admin hoặc chủ network; lab không chặn nên chưa chứng minh được nhánh bị-chặn. Probe trước bằng chính creds
  sẽ dùng (tạo thử 1 port với `--allowed-address`, rồi xóa).
- **`dns-integration` thật**: lab quảng cáo extension nhưng KHÔNG lưu `dns_name` (ML2 thiếu driver — create
  echo giá trị, `port show` ra `None`), nên nhánh chép dns_name dương tính mới phủ bằng giả lập. Trên cloud
  đích thật, kiểm bằng `openstack port create --dns-name test … && openstack port show … -c dns_name` trước.

Trước khi dùng thật đọc [GUIDE-PYTHON.md](GUIDE-PYTHON.md) mục 4b.6 (chuyển từ 7.0: group `az-*` cũ không
được dùng lại, SG cùng tên không còn là mặc định, cloud nhiều AZ cần `--az`/`MIG_AZ`).

Riêng app, đã chạy trên lab: 5 bộ test cũ (140 check), 24 check feature V3–V6 và 27 check P6. Chưa chạy trên
hệ production thật. Lần đầu nên:

- dùng một VM test, đi đúng thứ tự mục 10;
- so state dir và log với một lần chạy bản bash nếu còn giữ.

Bổ sung P5:

- `migrate plan volume` đã đối chiếu với `migrate-batch.sh --volumes … --plan-only` bằng harness giả lập
  (12 kịch bản).
- Trên lab thật mới chạy chỉ đọc 2 lệnh: `--project admin --available` và `--vm demo-vm`. Chưa chạy
  `--all`, và chưa chạy với project khác `admin`.

## 12. Lỗi thường gặp khi dựng host mới (gặp thật 19/11)

Cả loạt dưới đây chung một gốc: venv dựng tay mà bỏ qua mục 2–3. Dựng host mới cứ chạy trọn mục 2 rồi
mục 3 một lượt trước khi cài app.

| Triệu chứng | Nguyên nhân | Sửa |
|---|---|---|
| `pip install --no-build-isolation .` chết: metadata ra `UNKNOWN.egg-info`, `error: invalid command 'bdist_wheel'` | venv thiếu `setuptools>=61` / `wheel` (Ubuntu 22.04: setuptools 59 đi kèm Python 3.10) | `pip install -U pip setuptools wheel` (mục 3) rồi cài lại. Nâng mỗi pip là chưa đủ |
| `ERROR: cannot list the source servers (--name ^<vm>$)` ngay sau dòng `logs in /var/log/mig-batch/…` | venv thiếu `python-openstackclient`, hoặc openrc nguồn sai (auth-url/mật khẩu/cert), hoặc API chỉ tới được qua proxy (app unset `http(s)_proxy` khi gọi — cần proxy thì khai `export https_proxy=…` trong migrate.conf) | Tra bằng tay: `source <SRC_RC>; openstack server list --long --name '^<vm>$'` — lỗi thật hiện ở đây. Thiếu client thì cài bộ 4 gói mục 3 |
| `ModuleNotFoundError: No module named 'os_brick'` khi tới bước gắn volume | venv thiếu `os-brick` (tiến trình con `python -I -m migratetool.brick` import từ venv) | Bộ 4 gói mục 3 (nhớ `-c` upper-constraints); kèm gói hệ thống `open-iscsi` + `qemu-utils` (mục 2). Cài xong chạy lại đúng lệnh cũ — app tự tiếp tục từ state |
| `volume-<id> not visible in pool <pool>: wrong RBD_POOL_MAP for type 'default', or <user> lacks caps on that pool` | Không gõ `--volume-type` nên Cinder dùng type mặc định của đích, mà backend của type đó ghi vào pool khác `RBD_POOL`/map đang khai | Khai `RBD_POOL_MAP="<type>:<pool>"` khớp backend và gõ `--volume-type` (GUIDE-PYTHON mục 2b.1b). `RBD_POOL` chỉ dành cho type ngoài map — đặt = pool của type mặc định. Volume lỗi đã nằm trong state: `migrate rollback <vm>` tự xóa. Map đúng rồi mà vẫn lỗi thì kiểm caps: `ceph auth get client.<RBD_USER>` |
