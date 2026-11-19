# DESIGN 7.2 — sửa toàn bộ phát hiện của AUDIT-20261119

Trạng thái: user chốt 19/11 tối ("fix hết"). Fable thiết kế; Opus code trên nhánh `feature/7.2-audit-fixes`
từ `main` `e4c419b`; Fable review diff trước khi gate lab. Phát hành `7.2.0` (đổi hành vi → minor bump).
Nguồn: [AUDIT-20261119.md](AUDIT-20261119.md) (mã A1…D4 giữ nguyên dưới đây).

Nguyên tắc chung (không đổi so với DESIGN-V7 §2): state format, conf, chuỗi output bị grep (DEVELOPING-PYTHON
§4) giữ nguyên trừ các lệch **tự khai** ở §9. Mọi check mới là dòng `  OK    ` / `  FAIL  ` / `  WARN  ` /
`  INFO  ` chuẩn của preflight. Không SDK in-process; vẫn subprocess `openstack`.

---

## 1. A1 — cold mode tạo volume đích TRƯỚC khi tắt nguồn

`vmcmd.cmd_migrate`, trong `try`, trước `t_start`:

```
st.write("volume-map", "")
vids = [...]
if o.mode == "live": livesync.presync(r, vids, st)          # như cũ (đã tạo volume đích bên trong)
else:
    for vid in vids: create_dest_volume(r, vid, st)          # MỚI: trước stop
    log("destination volumes created; stopping the source now")
t_start = ...; st.write("t-stop", ...)
... stop / detach / ephemeral / lock như cũ ...
for vid in vids:
    if live: livesync.final_pass(r, vid, st)
    else:    copy_volume_cold(r, vid, st.dst_volume_of(vid), <size>, st)   # không create nữa
```

- `size`: lấy từ `volumes.json` đã export (`jqs(v["size"])`) thay vì `volume show` thêm một lần trong downtime.
- Rollback không đổi: `volume-map` đã ghi trước khi tạo → dọn được cả khi chết giữa chừng.
- Lệch output tự khai: các dòng `volume … created`/`rbd info` giờ đứng trước `stop source VM`.

## 2. A2 — preflight quota đích

Hàm mới `preflight.quota_preflight(r, st, c, ports, vols, flavor_json)`; gọi ở cuối VM preflight (sau mọi check
khác, trước RBD) và trong `volcmd.volume_preflight` (chỉ volumes/gigabytes).

- Lệnh: `r.dst.run("quota", "show", "--usage", "-f", "json")` (token đã scope project đích). rc≠0 hoặc JSON hỏng →
  `  WARN  destination quota not checked: <dòng stderr cuối>` (không FAIL: cloud thiếu API không được chặn).
- Nhu cầu: `instances 1`, `cores flavor.vcpus`, `ram flavor.ram` (lấy từ `flavor show <flavor> -f json` ở đích —
  gộp với check `flavor` hiện có: đổi `r.dst.ok` thành `run` + json, giữ nguyên dòng `  OK    flavor X`),
  `volumes len(vols)`, `gigabytes sum(size)`, `ports len(ports)`, `security_groups` = số group `create=True`
  trong `r.sgplan`, `security_group_rules` = tổng rule của các group đó. `--volumes-only`: chỉ volumes/gigabytes.
  Ephemeral: không tính volume root.
- `Limit == -1` → bỏ qua tài nguyên đó. Free = Limit − In Use − Reserved.
- Mỗi tài nguyên thiếu: `  FAIL  destination quota: <res> need N, M free (used U of L)`. Đủ hết: một dòng
  `  OK    destination quota: instances 1/10 free, cores 2/20, ram 2048/51200 MB, volumes 2/10, gigabytes 41/1000, ports 1/50`
  (chỉ liệt kê tài nguyên đã kiểm).

## 3. A3 — floating IP: export + WARN + rollback gắn lại

- `vmdef.export_definition`: mỗi port thêm `fips = lines(r.src.run("floating","ip","list","--port",pid,"-f","value","-c","Floating IP Address", stderr=DEVNULL).stdout)`;
  ghi `entry["floating_ips"] = fips` **chỉ khi không rỗng** (như `description`/`dns_name`, bash bỏ qua key lạ).
- `preflight.port_fidelity_preflight`: có `floating_ips` →
  `  WARN  port <mac>: floating IP <a>[, <b>] not migrated (a floating IP does not cross clouds): allocate one at the destination and update DNS`.
- `cmd_plan` dòng `  port …` giữ nguyên; thêm dòng riêng `  fip   <addr> on <mac>` sau dòng port đó.
- `rollback_vm`, sau khi gắn lại port: với mỗi port có `floating_ips` trong `ports.json` (theo chỉ số i ↔ port
  đã gắn lại), `floating ip show <a> -f value -c port_id` rỗng → `floating ip set --port <pid> <a>` →
  `log("re-associated floating IP <a> with port <pid>")`; lỗi → `log("WARN floating IP <a> not re-associated: …")`, không dừng.

## 4. A4 — tên VM khớp nhiều VM ở nhiều project

`selector.enumerate_vms`: `m` (khớp tên) có `len(m) > 1` **bất kể project** → mỗi VM một hàng
`SKIP(ambiguous-name: use the ID)` (chuỗi cũ, grep ổn định). Khớp bằng ID không bị ảnh hưởng.

## 5. B1 — detach: "gone" chỉ khi Neutron nói không có port; rollback gắn lại theo hợp

- `server.detach_source_ports`: `port show` với `stderr=proc.CAPTURE`. rc≠0: nếu stderr khớp
  `re.I: no port found|not found|could not be found|404` → `gone`; ngược lại coi là lỗi tạm, `sleep 2`, tiếp
  vòng (tối đa 60). Hết vòng mà vẫn lỗi → `Die("cannot read port %s after detach: %s" % (pid, last_err))`.
- `rollback_vm`: danh sách gắn lại = `source-ports` (nếu có) **∪** `port-ids.json`, giữ thứ tự, bỏ trùng. Mỗi pid:
  `port show -f value -c device_id` rc≠0 → `log("port %s no longer exists (Nova deleted it; its reserve port is reattached instead)")`, bỏ qua;
  `device_id` khác rỗng → bỏ qua; còn lại gắn như cũ. Dòng `reattached source port` giữ nguyên.

## 6. B2 — ephemeral sẵn sàng production

- Endpoint: `--interface <OS_INTERFACE của env đích, mặc định public>`; `endpoint list` rỗng → Die rõ.
- `_rest`: `ssl.create_default_context(cafile=OS_CACERT)` khi env có `OS_CACERT`; `OS_INSECURE` ∈ {1,true,yes} →
  `check_hostname=False, verify_mode=CERT_NONE`. Dùng `HTTPSHandler(context=ctx)` + `ProxyHandler({})`.
- Token: hàm `fresh_token()` = `token issue -f value -c id`, gọi ngay trước POST, trước PUT, trước PATCH (3 lần).
- Preflight ephemeral (trong nhánh `r.ephemeral` của `preflight`): dung lượng trống của `config.STATE_ROOT`
  (`shutil.disk_usage`) so với disk GB của flavor nguồn (`server.flavor.disk` nếu có, else `flavor show` nguồn;
  không đọc được → `  WARN  free space not checked`). Thiếu → `  FAIL  free space in /var/lib/mig-migrate: X GB, the root disk snapshot needs up to Y GB`; đủ → `  OK    free space … X GB for a Y GB root disk`.
- **Chưa làm, chờ user**: VM đích ephemeral vẫn boot `--image` (không volume). Ghi vào GUIDE là hạn chế.

## 7. B3 — detect_boot cross-check `server.image`

`vmdef.detect_boot(st)`: `by_dev` như cũ. `img = server.json["image"]`: BFV khi `img` rỗng/None, hoặc chuỗi chứa
`booted from volume`, hoặc dict không có `id`. Mâu thuẫn (by_dev nói ephemeral mà image nói BFV, hoặc ngược lại) →
`Die("cannot tell the root disk of %s: attachments %s, image %s — expected /dev/vda or /dev/sda for a boot-from-volume server")`.
(Die, không FAIL: cùng cách xử lý với "ephemeral VM has no volume to migrate" hiện tại.)

## 8. B4 — `--ip <addr>` kiểm tra địa chỉ và subnet

- `options.parse_option_list`: thay `IPV4_RE` bằng `ipaddress.IPv4Address(v)` (ValueError → Die cũ
  "--ip takes same, new or an IPv4 address").
- `preflight`: nhánh `addr`, trước check "IP free": `ip_address(addr) in ip_network(cidr, strict=False)` sai →
  `  FAIL  IP <addr> is not in subnet <cidr> of <net>` (vẫn chạy check free sau đó).

## 9. B5 — rollback không dừng vì lệnh liệt kê phụ

- `rollback.py:82` image list nguồn, `:89` clone list: `.run`; rc≠0 → `log("WARN cannot list source <images|clones> …: skipped")`.
- Clone: thay `wait_volume + volume delete` bằng `drop_volume(r, r.src, cid, "leftover clone")` (giữ brick detach trước).

## 10. B6 — allowlist image-property của volume

`copyvol.IMAGE_PROP_RE` → dùng chung `GUEST_PROP_RE` (chuyển hằng sang `copyvol`, `ephemeral` import lại):
`^(hw_|os_distro$|os_type$|os_secure_boot$|os_require_quiesce$|hypervisor_type$|architecture$|vm_mode$)`.

## 11. Nhóm C

| # | Thay đổi |
|---|---|
| C1 | `util.re_escape(s)` (regex của selector `RE_ESCAPE`); dùng ở `vmdef.export_definition` và check dst-name trong `preflight`; `selector` import lại từ util |
| C2 | `livesync.check_numbers`: `--max-passes` ≥ 1, `--delta-limit` ≥ 0; sai → Die `"%s takes a whole number >= 1, not '%s'"` |
| C3 | `secgroups.ensure` cleanup: với các group đã tạo, xóa hết rule (`rule list` → `rule delete`) trước, rồi xóa group |
| C4 | `attach_clone(r, vid, st)`: err file `st.p("brick-attach.err")`; `rollback.drop_volume`: `stderr=proc.CAPTURE`, đọc `res.stderr`, bỏ file `volume-show.err` |
| C5 | `StateDir.__init__`: arg chứa `/` hoặc là `.`/`..` → Die `"VM name %r cannot name a state directory: run with the server ID"` |
| C6 | Option `--volume-az ZONE` (VALUE_OPTS, VOLUME_PASS, help VM_OPTIONS + volume), conf `MIG_VOLUME_AZ` (CONF_VARS, DEFAULTS "", conf.example). `Options.volume_az`; `apply_az_defaults`: không gõ → `MIG_VOLUME_AZ`, không có → `dest_az` (hành vi cũ). `volume create --availability-zone <volume_az>`; preflight `volume AZ <volume_az>`; header preflight thêm ` volume-az=<x>` khi khác `dest_az`; file `options` thêm `VOLUME_AZ=` cuối dòng |
| C7 | preflight/volume_preflight: `iscsid` chỉ `is-active`; FAIL kèm `  INFO  start it: systemctl enable --now iscsid` |
| C8 | `rollback_vm`: `dst-server-id` rỗng **và** `volume-map` tồn tại → `server list --name ^<re_escape(name)>$ -f json` ở đích; đúng 1 kết quả và `created` ≥ `t-stop` (epoch) → `server delete --wait`, `log("deleted destination server %s found by name (its ID was never recorded)")`; khác → bỏ qua |

## 12. D1 — unit harness vào repo

- Mang `/root/v7h-p6/` (unit.py, p5unit.py, p6unit.py, parity.py, scenarios*.py, stub_*.py, pyrun.py) vào
  `tests/unit/`, chuẩn hóa đường dẫn (`pkg` → import từ repo root), runner `tests/unit/run.sh` chạy được bằng
  `python3` hệ thống lẫn venv, stdlib-only, < 60 s. Parity vs bash: cập nhật `expect_diff`/`bash_subs` cho mọi
  lệch của 7.2 với comment `Intended: 7.2 <mã>`.
- Case mới bắt buộc (mỗi mã ≥ 1 case, tên case chứa mã): A1 thứ tự `volume create` < `server stop` trong
  chuỗi lời gọi; A2 FAIL khi gigabytes thiếu, OK khi đủ, WARN khi lệnh lỗi, Limit −1 bỏ qua; A3 WARN + fip line +
  rollback re-associate; A4 hai project → 2 hàng SKIP; B1 stderr 503 → retry rồi OK, stderr "No Port found" →
  reserve, rollback gắn lại port ngoài `source-ports`; B2 cacert/insecure/interface, fresh token 3 lần, df FAIL;
  B3 Die khi mâu thuẫn; B4 `999.1.1.1` Die, ngoài subnet FAIL; B5 image list lỗi → WARN, rollback vẫn unlock;
  B6 `os_hash_value` không copy, `hw_disk_bus` copy; C1 tên có `.`; C2 `--max-passes 0`; C3 thứ tự rule→group;
  C5 tên có `/`; C6 `--volume-az`/`MIG_VOLUME_AZ`/fallback; C8 xóa theo tên khi created ≥ t-stop, không xóa khi <;
  D2 doctor FAIL khi thiếu lệnh.

## 13. D2 — `migrate doctor`

Lệnh mới, read-only, không cần VM. Mỗi mục một dòng OK/FAIL/WARN/INFO; exit 1 khi có FAIL.

1. `openstack --version`, `qemu-img --version`, `rbd --version`, `iscsiadm --version`; `multipath -h` nếu `MIG_MULTIPATH=1`.
2. `python -I -m migratetool.brick --check` (brick: cờ mới, chỉ import os_brick/cinderclient/keystoneauth1/oslo, in `ok`, exit 0).
3. `SYS_PY -I -c "import rados, rbd"`; `blocksync.py` đọc được.
4. Conf: đường dẫn, parse được; `SRC_RC`/`DST_RC` đọc được (`os.access`); `OS_AUTH_TYPE` hợp lệ.
5. `token issue` hai phía → `  OK    source token (project <name>)`; appcred → INFO.
6. Role: `role assignment list --effective --user <OS_USERNAME|appcred user> --project <pid> --names -f value -c Role` (password) hoặc `application credential show` (appcred); không có `admin` → `  WARN  <side>: no admin role: --ip same needs port --mac-address (admin_or_network_owner)`. Lỗi lệnh → INFO "roles not checked".
7. `rbd -p <RBD_POOL> ls` (và từng pool trong `RBD_POOL_MAP`); `systemctl is-active iscsid`; `multipathd` nếu bật.
8. `STATE_ROOT`, `LOG_ROOT` tạo/ghi được.

## 14. D3 — đo overhead control-plane

Cuối `cmd_migrate` (cả volumes-only): `log("downtime breakdown: total %ds, copy %ds, verify %ds, control-plane %ds")`
với copy/verify cộng dồn từ `copy_volume_cold`/`sync_volume_pass` (ghi vào `r.t_copy`, `r.t_verify`). Live: copy =
final pass, verify = compare full nếu có. Test report ghi số thật của lab cho cold và live.

## 15. D4 — phiên bản & tài liệu

- `pyproject.toml`, `__init__.py` → `7.2.0`. CHANGELOG `## [7.2.0] — 2026-11-19`: mục Thêm/Đổi hành vi/Sửa theo mã.
- GUIDE-PYTHON: mục "4c. 7.2": thứ tự cold mới, quota, floating IP, `--volume-az`, `doctor`, hạn chế ephemeral
  (no volume, HTTPS cần OS_CACERT). DEPLOY-PYTHON: mục "0. Chạy `migrate doctor` trước", bảng mục 12 trỏ sang doctor.
  `migrate.conf.example`: `MIG_VOLUME_AZ`. README quick start thêm `migrate doctor`. AUDIT-20261119.md: thêm cột
  "Trạng thái 7.2".

## 16. Gate

1. `tests/unit/run.sh` 0 FAIL (trên mig-host, python hệ thống).
2. Lab (mig-host, sau khi `pip install --no-build-isolation .` vào `/opt/mig-venv`, dọn volume vô danh/`mig-clone-*`
   trước mỗi suite): `e2e-migrate-py.sh`, `live-migrate-py.sh`, `newip-voltype-py.sh`, `extras-py.sh`, p6feat,
   `failure-injection` — 0 FAIL, và kiểm tra bằng mắt trong log e2e: `volume … created` đứng trước `stop source VM`.
3. Lab tay: `migrate doctor` (OK toàn bộ); `migrate plan demo-vm --ip 999.1.1.1` (Die), `--ip 10.9.9.9` (FAIL subnet);
   `migrate plan demo-vm` có dòng quota OK; tạo VM `demo-vm` thứ hai ở project khác → `migrate plan demo-vm` ra 2 hàng
   SKIP(ambiguous) rồi xóa; `--volume-az nova` OK.
4. TEST-REPORT-20261119-v72.md: từng case lệnh nguyên văn + kết quả, số downtime breakdown cold/live.

Không push; Fable leak-check + review rồi user chốt merge.

---

## Bổ sung 19/11 tối (user hỏi "safeguard tất cả resource chưa") — mở rộng §2 A2

A2 phải phủ MỌI tài nguyên một lần migrate tiêu thụ, cả hai phía. Cùng hàm `quota_preflight`, cùng định dạng dòng:

| Phía | Tài nguyên | Nhu cầu | Nguồn số liệu |
|---|---|---|---|
| nguồn | `volumes` | +1 (clone tạm; live mode cũng chỉ 1 clone sống tại một thời điểm) | `r.src quota show --usage -f json` (token scope project nguồn) |
| nguồn | `gigabytes` | + size của volume LỚN NHẤT (clone từng volume một, drop trước khi clone volume sau) | như trên |
| nguồn | `volumes_<type>`, `gigabytes_<type>` của type volume nguồn (clone kế thừa type) | như hai dòng trên, theo type của từng volume | như trên; thiếu entry → bỏ qua |
| đích | `volumes_<type>`, `gigabytes_<type>` | khi `--volume-type`/AZ_TYPE_MAP chọn type: số volume, tổng GB | `r.dst quota show --usage`; thiếu entry → bỏ qua |
| đích | `server_group_members` | 1, chỉ khi VM sẽ vào server group (`r.ksg_dst` hoặc `--server-group`) | `limits show --absolute`: `max_server_group_members` so với số member hiện tại của group đích (`server group show -c members`); group chưa tồn tại → 0 |
| đích | IP trống trong subnet khi `--ip new` | 1 mỗi port | `subnet show <id> -f json` → tổng địa chỉ trong `allocation_pools` trừ số port có fixed IP trên subnet (`port list --fixed-ip subnet=<id> -f value -c ID`); ≤ 0 → `  FAIL  subnet <cidr>: no free address in its allocation pools for --ip new` |
| đích | dung lượng pool Ceph | tổng GB volume đích | `ceph df -f json --id RBD_USER -c CEPH_CONF` (hoặc `rbd pool stats`); lỗi caps → `  INFO  Ceph pool capacity not checked: <lý do>`; pool có `quota_max_bytes` hoặc cluster `MAX AVAIL` < nhu cầu → FAIL; dưới 10% dự phòng sau copy → WARN |

Dòng tổng kết OK tách hai phía: `  OK    source quota: volumes 1/10 free 1, gigabytes 200/1000 free 150` và
`  OK    destination quota: …`. `--volumes-only`/`migrate volume --in-use`: nguồn vẫn tính clone; volume rời
available cũng clone → tính như nhau. Unit case bắt buộc thêm: nguồn thiếu volumes (FAIL), nguồn thiếu
gigabytes theo volume lớn nhất, per-type thiếu ở đích, server group đầy, subnet hết IP với `--ip new`, ceph df
không có caps → INFO.
