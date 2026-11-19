# Test report 7.2 (sửa AUDIT-20261119) — nhánh `feature/7.2-audit-fixes`

Design: [DESIGN-V7-2.md](DESIGN-V7-2.md) (gồm phần bổ sung A2 19/11 tối). Audit: [AUDIT-20261119.md](AUDIT-20261119.md).
Code: Opus; review phase 1: Fable (19/11, không lỗi logic, 1 sửa B2). Gate lab: Opus chạy 19–20/11 trên mig-host.

Kết luận: **unit + parity 460/0**, **gate lab 200/0 trên 7 suite**, 6 check tay đúng thiết kế. Không sửa code trong
lúc gate (không suite nào FAIL).

## 1. Build kiểm

| Mục | Giá trị |
|---|---|
| Nhánh / commit | `feature/7.2-audit-fixes` @ `19e5282` (B2: TLS và image endpoint kiểm trong preflight) |
| Cài lên mig-host | `git archive HEAD` → `/root/v72-gate/`, `cd /root/v72-gate && /opt/mig-venv/bin/pip install --no-build-isolation .`, `ln -sf /opt/mig-venv/bin/migrate /usr/local/bin/migrate` |
| `migrate --version` | `migrate 7.2.0` (thay `7.1.0.dev1` trong venv lab — có chủ đích, venv lab giữ 7.2.0) |
| Conf | không có `/opt/mig/migrate.conf` (mặc định + env), như gate 7.1 |

## 2. Unit + parity giả lập (không gọi cloud)

Lệnh (trên mig-host, python hệ thống 3.12, bản `git archive` của commit đang kiểm, thư mục tạm riêng):

```
MIG_UNIT_WORK=/tmp/mig-unit-final2 bash tests/unit/run.sh --parity
```

| Bộ | `19e5282` |
|---|---|
| unit.py | 65/0 |
| p5unit.py | 45/0 |
| p6unit.py | 63/0 |
| v72unit.py (mỗi mã A1…D4 ≥ 1 case, tên case chứa mã) | 82/0 |
| parity bash ↔ app, 205 kịch bản, 6 chiều | 205/0 |
| **Tổng** | **`ALL pass=460 fail=0`** (`real 19m0.741s`) |

Baseline của harness ngay khi đưa vào repo, trên code 7.1 chưa sửa: `ALL pass=378 fail=0` (parity 205/0).
Mọi điểm lệch 7.2 so với bản bash ghi trong harness bằng chú thích `Intended: 7.2 <mã>` (luật máy móc
`parity.v72_rule` + 5 kịch bản lỗi mà A1 đổi thứ tự: kiểm riêng bằng `a1_early_check`/`a1_late_check`).

## 3. Doctor (D2) trên mig-host

```
$ migrate doctor
migrate 7.2.0 doctor:
  INFO  conf /opt/mig/migrate.conf not found: defaults and environment only
  OK    openstack: openstack 10.5.0
  OK    qemu-img: qemu-img version 8.2.2 (Debian 1:8.2.2+ds-0ubuntu1.18)
  OK    rbd: ceph version 20.2.5 (ed27c686770872022f4c1de04f6b0ac5d5384ded) tentacle (stable - None)
  OK    iscsiadm: iscsiadm version 2.1.9
  OK    brick: os-brick, cinderclient, keystoneauth1, oslo import
  OK    librbd python bindings for SYS_PY=/usr/bin/python3
  OK    blocksync present (/opt/mig-venv/lib/python3.12/site-packages/migratetool/blocksync.py)
  OK    source token (project admin)
  OK    source: admin role
  OK    destination token (project admin)
  OK    destination: admin role
  OK    RBD access to pool mig-volumes as mig-xfer
  OK    iSCSI daemon on transfer host
  OK    state directory /var/lib/mig-migrate writable
  OK    log directory /var/log/mig-batch writable
Doctor passed.
rc=0
```

## 4. Gate lab — 7 suite nối đuôi

Driver `/root/mig-lab/tests/gate72.sh` (nằm ngoài repo): trước **mỗi** suite dọn lab (volume vô danh/`mig-clone-*`
available ở nguồn, volume đích mang description `migrated from …` còn available, port `mig-reserve-*` không gắn
vào VM nào ở nguồn), ghi `gate72-hygiene.log`, rồi chạy suite. Lệnh:

```
cd /root/mig-lab/tests && nohup bash gate72.sh e2e-migrate-py.sh live-migrate-py.sh newip-voltype-py.sh \
  extras-py.sh p4feat.sh p6feat.sh failure-injection-py.sh > gate72-driver.log 2>&1 < /dev/null &
```

`failure-injection-py.sh` sinh từ `failure-injection.sh` bằng sed (chưa có bản -py trước đây):
`s/migrate-vm\.sh migrate \$VM/migrate migrate $VM --yes/` và `s/migrate-vm\.sh rollback /migrate rollback /`
— đúng 5 dòng đổi (4 dòng migrate, 1 dòng rollback), không còn `migrate-vm.sh`. Các suite khác chạy nguyên bản
đã dùng ở gate 7.1.

| # | Suite | Lệnh | Giờ chạy | Kết quả |
|---|---|---|---|---|
| 1 | e2e cold | `bash e2e-migrate-py.sh` | 21:49–22:06 | **PASS=50 FAIL=0 WARN=0** |
| 2 | live | `bash live-migrate-py.sh` | 22:06–22:28 | **PASS=26 FAIL=0** |
| 3 | newip + volume type | `bash newip-voltype-py.sh` | 22:29–22:39 | **PASS=26 FAIL=0** |
| 4 | extras (2 NIC, ephemeral, override…) | `bash extras-py.sh` | 22:39–23:04 | **PASS=30 FAIL=0** |
| 5 | p4feat (selector, map, defaults) | `bash p4feat.sh` | 23:05–23:44 | **PASS=24 FAIL=0** |
| 6 | p6feat (SG/server group copy, port fidelity) | `bash p6feat.sh` | 23:44–00:09 | **PASS=27 FAIL=0** |
| 7 | failure-injection | `bash failure-injection-py.sh` | 00:09–00:25 | **PASS=17 FAIL=0** |
| | **Tổng** | | | **200 PASS / 0 FAIL** |

Dọn lab trong lúc gate (`gate72-hygiene.log`):

- trước gate (tay): xóa 21 port `mig-reserve-*` mồ côi (DOWN, không gắn VM) ở nguồn từ các lần test cũ; giữ
  `mig-reserve-ae26996b…-0` vì đang là NIC của `mig-peer`;
- trước p6feat: 3 volume vô danh available ở nguồn + 3 port `mig-reserve-*` mồ côi (do p4feat để lại);
- trước các suite khác: không có gì phải dọn; demo-vm luôn ACTIVE, unlock; đích không server, không volume.

### 4.1. A1 — volume đích tạo trước khi tắt nguồn (log e2e cold, `/var/log/mig-batch/20261119-215004/migrate-b2f4d5b0-….log`)

```
[21:52:02] destination security group nova-mig-vm-sg (24721dbc-…) exists: used as is, its rules are not compared
[21:52:19] destination volumes created; stopping the source now
[21:52:24] stop source VM
[21:52:50] source stopped, its ports detached, now locked
[21:53:01] clone attached at /dev/sda
```

Đúng thứ tự mới: tạo đủ 2 volume đích (dòng 21:52:19) **trước** `stop source VM`. failure-injection F1
(IP đích bị chiếm) cũng xác nhận không có volume nào được tạo khi preflight FAIL
(`PASS  no volume created on destination`).

### 4.2. A2 — quota hai cloud và pool Ceph (preflight của cùng log e2e)

```
  OK    source quota: volumes 1/8 free, gigabytes 10/988
  OK    destination quota: instances 1/10 free, cores 1/20, ram 1024/51200 MB, volumes 2/10, gigabytes 12/1000, ports 1/498
  OK    Ceph pool mig-volumes: 12 GB needed, 92 GB left (MAX AVAIL)
```

Tên tài nguyên của `openstack quota show --usage -f json` (OSC 10.5.0) trên lab, đích:
`cores instances ram fixed_ips floating_ips networks security_group_rules security_groups volumes snapshots
gigabytes backups volumes_mig-typeb gigabytes_mig-typeb snapshots_mig-typeb volumes___DEFAULT__ gigabytes___DEFAULT__
snapshots___DEFAULT__ groups ports rbac_policies routers subnets subnet_pools injected-file-size injected-path-size
injected-files key-pairs properties server-group-members server-groups backup-gigabytes per-volume-gigabytes`.
`security_groups`/`security_group_rules` (gạch dưới) nằm trong alias của app. `limits show --absolute`:
`max_server_group_members = 10`, `server_group_members = 10` (cả hai tên đều được app nhận).
User `mig-xfer` (`profile rbd`) đọc được `ceph df detail`: dòng Ceph ra OK, không phải INFO.

Ephemeral (extras, log `20261119-225538`): preflight có
`  OK    free space in /var/lib/mig-migrate: 29 GB for a 10 GB root disk` và
`  OK    image endpoint http://<glance nội bộ>:9292 (internal)` — openrc đích của lab đặt `OS_INTERFACE=internal`.

### 4.3. D3 — downtime breakdown

| Lần chạy | Dòng log |
|---|---|
| e2e cold (2 volume 10+2 GB, `--verify` mặc định) | `[21:57:32] downtime breakdown: total 312s, copy 80s, verify 122s, control-plane 109s` |
| live (2 pass trước cutover, volume data không hội tụ sau 5 pass) | `[22:23:58] downtime breakdown: total 245s, copy 133s, verify 0s, control-plane 111s` |
| failure-injection F4 (cold sạch) | `[00:24:16] downtime breakdown: total 308s, copy 81s, verify 117s, control-plane 109s` |
| extras ephemeral (snapshot Glance) | `[23:01:00] downtime breakdown: total 213s, copy 0s, verify 0s, control-plane 213s` |

Phần control-plane (lệnh `openstack`, chờ, attach/detach) khoảng **100–110 s** mỗi lần cutover cold/live trên lab.
Lưu ý: ở đường ephemeral, snapshot + tải + upload Glance (≈ 107 s ở lần trên) đang bị tính vào control-plane vì
D3 chỉ cộng qemu-img/blocksync vào `copy` — số control-plane của VM ephemeral vì vậy cao hơn thực tế.

## 5. Check tay (lệnh nguyên văn, trên mig-host, demo-vm ở nguồn)

| # | Mã | Lệnh | Kết quả |
|---|---|---|---|
| 1 | B4 | `migrate plan demo-vm --ip 999.1.1.1` | `ERROR: --ip takes same, new or an IPv4 address`, rc=1 — dừng lúc parse option, không gọi cloud |
| 2 | B4 | `migrate plan demo-vm --ip 10.9.9.9` | `  FAIL  IP 10.9.9.9 is not in subnet 192.168.120.0/24 of vlan120` (dòng `IP 10.9.9.9 free` vẫn OK ngay sau), PLAN-FAIL, rc=1 |
| 3 | C6 | `migrate plan demo-vm --volume-az nova` | Header `Destination preflight (mode=cold ip=same type=default pool=mig-volumes volume-az=nova):` (không có `--az` nên `volume-az` khác `az` và được in), `  OK    volume AZ nova`, PLAN-OK, rc=0 |
| 4 | C2 | `migrate plan demo-vm --mode live --max-passes 0` | `ERROR: --max-passes takes a whole number >= 1, not '0'`, rc=1 |
| 5 | A4 | Tạo VM thứ hai tên `demo-vm` ở project `proj-b` (boot từ volume, đúng quy tắc "instance phải có volume"): `openstack --os-project-name proj-b server create demo-vm --flavor mig.small --image noble --boot-from-volume 10 --network vlan121 --wait` → `9658b6a7-…` ACTIVE; rồi `migrate plan demo-vm` | Bảng `Selected:`/`Plan:` có đúng 2 hàng `SKIP(ambiguous-name: use the ID)` (`9658b6a7-…` và `b2f4d5b0-…`), rc=1. Sau đó xóa server `9658b6a7-…` (`server delete --wait`) và volume `869b8a6c-…` của nó |
| 6 | A2 | `migrate plan demo-vm --ip new` | thêm dòng `  OK    free addresses for --ip new: 192.168.120.0/24 1/99`, PLAN-OK, rc=0 |

## 6. Trạng thái lab sau gate

- `migrate rollback demo-vm` sau failure-injection (F4 để demo-vm ở đích): xóa server, port, 2 volume đích, gắn
  lại port nguồn, `source VM ACTIVE again`, rc=0.
- Nguồn: demo-vm ACTIVE, `locked=False`; đúng 2 volume `demo-root`/`demo-data` (in-use); `mig-peer` ACTIVE (giữ
  nguyên, NIC của nó là port `mig-reserve-ae26996b…-0`); không còn port `mig-reserve-*` mồ côi.
- Đích: 0 server, 0 volume, pool `mig-volumes` rỗng; SG còn `default`×6, `mig-sg-b`, `mig-vm-sg`, `nova-default`×2,
  `nova-mig-vm-sg` (như baseline 7.1).
- `/var/lib/mig-migrate`: không còn state sống (đã xóa thư mục plan-only `demo-vm/` do các lệnh plan tay tạo, và 3
  file `.err` dùng chung của bản 7.1 — 7.2 không còn ghi chúng). Không còn phiên iSCSI.
- Venv lab `/opt/mig-venv` giữ **7.2.0**; `/usr/local/bin/migrate` trỏ tới venv.

## 7. Chưa test / hạn chế

- Ephemeral: VM đích vẫn boot từ image, không có volume (B2 còn lại, chờ user chốt).
- Glance HTTPS với `OS_CACERT` chưa có trên lab (Glance lab là HTTP): nhánh CA chỉ phủ bằng unit
  (`v72unit` B2: CA không nạp được → FAIL preflight; `OS_INSECURE`; CA hợp lệ).
- C8 (server tạo mà không ghi được ID), B1 (lỗi 503 khi `port show`), B5 (listing nguồn lỗi), quota thiếu, pool
  Ceph đầy, subnet hết IP: chỉ phủ bằng unit/giả lập — không dựng được lỗi thật trên lab mà không phá cloud.
