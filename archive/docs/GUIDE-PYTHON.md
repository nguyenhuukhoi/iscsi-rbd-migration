# Hướng dẫn dùng app `migrate` (bản Python, từ v7)

Từ v7, một lệnh `migrate` (package Python `migratetool/`) thay hẳn hai script `migrate-vm.sh` và
`migrate-batch.sh`. Hai script cũ nằm trong [archive/](archive/README.md), hướng dẫn của chúng vẫn là
[GUIDE.md](GUIDE.md).

App là **bản dịch giữ nguyên hành vi** của bản bash ([DESIGN-V7.md](DESIGN-V7.md)):

- cùng cách copy (cold, live), cùng preflight, cùng quy tắc đặt tên;
- cùng file cấu hình `/opt/mig/migrate.conf`;
- cùng thư mục state `/var/lib/mig-migrate/`, nên state do bản bash tạo thì app rollback được, và ngược lại.

Bằng chứng: app qua đủ 140/140 check của 5 bộ test cũ trên lab thật cùng 24/24 check feature V3–V6
(TEST-REPORT-20261118-v7.md). Cái đổi là **cách gõ lệnh**, cùng vài
điểm cố ý khác mà mục 9 liệt kê. **Đọc mục 9 trước khi chạy thật.**

**Từ 7.1 (P6), app làm thêm việc bản bash không làm:** security group và server group ở đích là bản copy
`<AZ đích>-<tên nguồn>`, port đích mang theo allowed-address-pairs, port security, description, dns_name.
Đây là **mặc định mới**, khác 7.0. **Đọc mục 4b trước khi chạy 7.1**, nhất là phần chuyển từ 7.0.

**7.2** sửa các phát hiện của [AUDIT-20261119.md](AUDIT-20261119.md): cold tạo volume đích trước khi tắt VM
nguồn, preflight kiểm quota hai cloud và dung lượng pool Ceph, cảnh báo floating IP, `--volume-az`, lệnh
`migrate doctor`. Tóm tắt và hạn chế còn lại: mục 4c.

**7.3** adds machine-readable output for a UI (and scripts): `--json` on `plan` and `doctor`, the read-only commands
`list`, `inventory` and `status`, `--progress-json` on `migrate`/`volume`, the state file `options.json`, and
`--dst-host` to pick the destination hypervisor. Nothing changes without them. Section 4d.

Cài đặt: [DEPLOY-PYTHON.md](DEPLOY-PYTHON.md). Từng bước một VM cold/live: [WORKFLOW-PYTHON.md](WORKFLOW-PYTHON.md).
IP trong tài liệu thuộc dải TEST-NET (`192.0.2.x`, `198.51.100.x`); tên VM, project, host là tên ví dụ.

## 1. Các lệnh

| Lệnh | Làm gì | Hỏi `Type yes`? | Đổi gì trên cloud? |
|---|---|---|---|
| `migrate plan <chọn VM> [option]` | Dry-run: liệt kê, xuất cấu hình nguồn, preflight đích cho từng VM | Không | Không |
| `migrate migrate <chọn VM> [option] [--yes] [--continue-on-error]` | Plan hết, hỏi một lần, rồi migrate **lần lượt từng VM** | **Luôn hỏi**, trừ khi có `--yes` | Có |
| `migrate volume <volume> [--in-use] [--volume-type t] [--az z] [--verify v]` | Copy **một** volume, không tạo server | Không (như `migrate-volume` cũ) | Có |
| `migrate volume <chọn volume> [--yes] [...]` | Copy volume **theo đợt** | Luôn hỏi, trừ khi có `--yes` | Có |
| `migrate plan volume <chọn volume> [...]` | Dry-run của `migrate volume` theo đợt (mới ở P5) | Không | Không |
| `migrate doctor` | (7.2) Kiểm host chuyển: chương trình, module, conf, openrc, token, quyền, pool RBD, iscsid (mục 4c.5) | Không | Không |
| `migrate rollback <vm \| volume \| vol-<id>>` | Xóa phía đích, trả VM nguồn về như cũ; hoặc xóa bản copy của volume | Không | Có (chỉ phía đích + trả trạng thái nguồn) |
| `migrate list <chọn VM> [--volumes …] [--json]` | (7.3) The selection plan/migrate would make, with host, AZ, flavor, boot, networks, volumes, state (4d.3) | No | No |
| `migrate inventory [--count] [--json]` | (7.3) Hosts, projects, zones, flavors, volume types of both clouds (4d.4) | No | No |
| `migrate status [--all] [--json]` | (7.3) State directories, batch logs, running migrate processes of this host; asks no cloud (4d.5) | No | No |

- `migrate --help`, `migrate <lệnh> --help` và `migrate plan volume --help` in đủ option.
- Gõ một lệnh mà không kèm tham số thì app in usage của lệnh đó và thoát mã 2.
- `plan` không tạo, sửa hay xóa gì trên 2 cloud. Như `plan` của bản bash, nó chỉ ghi file cục bộ
  (`server.json`, `ports.json`…) vào `/var/lib/mig-migrate/<vm>/`. Thư mục này không có `volume-map`, nên
  không chặn lần migrate sau.

## 2. Chọn VM (selector)

| Gõ | Chọn | Ví dụ |
|---|---|---|
| `<vm>` (positional) | Đúng 1 VM, theo tên hoặc ID. Viết gọn của `--vm <vm>` | `migrate plan web-01` |
| `--vm a,b` | Danh sách tên hoặc ID, ngăn bằng dấu phẩy | `--vm web-01,web-02` |
| `--host h1,h2` | VM trên các compute host đó. Tên lấy ở cột **Host** của `openstack compute service list`, không phải `hypervisor_hostname` | `--host cmp-01` |
| `--project p1,p2` | VM của các project đó, theo tên hoặc ID | `--project proj-a` |
| `--vm-file <file>` | Mỗi dòng một tên hoặc ID; `#` là comment, dòng trống bỏ qua | `--vm-file /root/wave1.txt` |

- **Nhiều selector thì lấy giao.** Ví dụ `--host cmp-01 --project proj-a` chọn các VM của `proj-a` đang
  nằm trên `cmp-01`.
- Positional chỉ nhận 1 VM. Gõ `migrate plan web-01 web-02` thì app dừng với
  `ERROR: one VM only (got: web-01 web-02): select several with --vm a,b`.
- App liệt kê bằng `server list --all-projects` (cần admin). Token không có quyền liệt kê mọi project thì
  app liệt kê trong project của token.
- **1 VM hay 100 VM đi cùng một đường:**
  1. In bảng `Selected:`.
  2. Plan từng VM, mỗi VM là một tiến trình con chạy trong project của chính VM đó.
  3. In bảng `Plan:`.
  4. Hỏi `Type yes`.
  5. Migrate tuần tự từng VM.
  6. In bảng `Summary:` và dòng `Totals:`.

  Mỗi lần chạy có một thư mục log `/var/log/mig-batch/<YYYYmmdd-HHMMSS>/`, chứa `plan-<id>.log`,
  `migrate-<id>.log` và các file JSON lúc liệt kê. Kể cả khi chỉ có 1 VM.

Ví dụ `migrate migrate web-01 --verify none` (rút gọn; VM ở project `proj-a`):

```
[10:15:02] logs in /var/log/mig-batch/20261118-101502; project map: none (0 entries, other projects keep their name)

Selected:
KIND ID                                   NAME                     STATUS    PROJECT src -> dst               RESULT
vm   3f1c2a4e-0000-4000-8000-0000000000a1 web-01                   ACTIVE    proj-a -> proj-a                 selected
[10:15:09] plan web-01 (3f1c2a4e-0000-4000-8000-0000000000a1): PLAN-OK

Plan:
KIND ID                                   NAME                     STATUS    PROJECT src -> dst               RESULT
vm   3f1c2a4e-0000-4000-8000-0000000000a1 web-01                   ACTIVE    proj-a -> proj-a                 PLAN-OK
Migrate 1 item(s) one by one with: --verify none? Type yes to continue: yes
[10:15:20] === vm web-01 (3f1c2a4e-0000-4000-8000-0000000000a1) proj-a -> proj-a
...                                            (output đầy đủ của tiến trình con, như migrate-vm.sh cũ)
Migrated. Source VM: kept SHUTOFF and locked. Rollback: migrate rollback web-01

Summary:
KIND ID                                   NAME                     STATUS    PROJECT src -> dst               RESULT
vm   3f1c2a4e-0000-4000-8000-0000000000a1 web-01                   ACTIVE    proj-a -> proj-a                 OK
Totals: OK 1  FAIL 0  SKIP 0  (logs: /var/log/mig-batch/20261118-101502)
```

`migrate plan` in thêm output preflight của từng VM ngay khi nó chạy. Bản batch cũ chỉ ghi phần này vào log.
`migrate migrate` thì giữ preflight trong `plan-<id>.log`, chỉ hiện output lúc migrate.

**Cột RESULT:**

| RESULT | Nghĩa |
|---|---|
| `SKIP(not-found)` | Tên hoặc ID gõ vào không ra VM nào |
| `SKIP(ambiguous-name: use the ID)` | Có từ 2 VM cùng tên trong 1 project. Chạy lại bằng ID |
| `SKIP(state-exists)` | Đã có state của lần chạy trước (`<tên hoặc ID>/volume-map`, hoặc `vol-<id>/volume-map`). App in lệnh rollback |
| `SKIP(status)` | VM không ở `ACTIVE`/`SHUTOFF`, hoặc volume ở trạng thái khác `available` (`error`, `reserved`…) |
| `SKIP(ephemeral-root)` | Chỉ khi copy volume: VM boot từ ephemeral nên không có volume để copy |
| `SKIP(mig-clone)` | Volume là clone tạm `mig-clone-*` còn sót của lần chạy trước |
| `SKIP(not-run)` | Không chạy, vì đợt đã dừng ở con FAIL trước đó (không có `--continue-on-error`) |
| `PLAN-OK` / `PLAN-FAIL` | Kết quả plan. Dòng FAIL của từng con được in dưới bảng `Plan:` |
| `READY` | Volume rời đang chờ copy. Volume rời không có plan riêng, xem mục 5.3 |
| `OK` / `OK(no key_name: <key>)` | Xong. Dạng thứ hai: keypair không có ở đích, VM đích được tạo không có `key_name` |
| `FAIL` / `FAIL(plan)` | Migrate lỗi / tới lượt một con `PLAN-FAIL` |

**Mã thoát:**

| Lệnh | 0 | 1 |
|---|---|---|
| `plan`, `plan volume` | Có ít nhất 1 hàng `PLAN-OK`/`READY` và không có `PLAN-FAIL` | Có `PLAN-FAIL`, hoặc mọi hàng đều `SKIP` |
| `migrate`, `volume` theo đợt | Có ít nhất 1 `OK` và không có `FAIL` | Có `FAIL`, hoặc không migrate được con nào |
| `volume <1 volume>`, `rollback` | Xong | Lỗi |

- Selector không ra hàng nào (log `nothing selected`, ví dụ host không có VM) thì thoát 0, giống batch cũ.
- Lỗi trước khi chạy (`ERROR: …`) thì thoát 1. Ctrl-C thì thoát 130.

Lệnh rollback mà app in sẵn bắt đầu bằng đường dẫn của chính lệnh vừa gõ, ví dụ
`/usr/local/bin/migrate rollback web-01`. Bash cũng in `$0` như vậy. Tài liệu này viết gọn thành
`migrate rollback …`.

## 3. Xác nhận `Type yes`

`migrate migrate` và `migrate volume` theo đợt **luôn hỏi** trước khi đụng vào cloud, kể cả khi chỉ có
1 VM:

```
Migrate 3 item(s) one by one with: --az dc10-epz? Type yes to continue:
```

- Số trong câu hỏi là số hàng `PLAN-OK` + `READY`. Phần `with:` liệt kê option sẽ truyền cho mọi VM.
- Phải gõ đúng `yes`. Gõ khác thì app dừng với `ERROR: not confirmed, nothing changed`, không đổi gì.
- `--yes` bỏ qua câu hỏi. **Script, cron, `nohup`, pipe bắt buộc có `--yes`.** Thiếu `--yes` mà stdin không
  phải terminal thì app dừng trước khi làm gì:
  `ERROR: stdin is not a terminal: re-run with --yes to migrate without confirmation`.
- Không hỏi: `plan`, `plan volume`, `volume <1 volume>`, `rollback`.
- Có hàng `PLAN-FAIL` mà không có `--continue-on-error` thì app in thêm
  `Note: without --continue-on-error the run stops at the first PLAN-FAIL row.` trước câu hỏi.

`--continue-on-error`: chạy tiếp sau con FAIL. Mặc định đợt dừng ở con FAIL đầu tiên, các con sau thành
`SKIP(not-run)`.

## 4. Option cho từng VM

Option giờ viết **cùng dòng lệnh, ở vị trí nào cũng được**, không còn dấu `--` như batch cũ. App tự tách
option điều phối (selector, `--yes`, `--continue-on-error`) với option của từng VM, rồi truyền nguyên phần
option của VM cho mọi VM trong đợt. Mặc định giống hệt bản bash: cold, giữ IP/MAC, volume type mặc định.

| Option | Ý nghĩa |
|---|---|
| `--mode cold\|live` | `cold` (mặc định): tắt VM rồi copy bằng `qemu-img`, sau đó so toàn bộ. `live`: VM chạy trong lúc pre-sync nhiều lượt (blocksync, hash từng chunk 4 MiB). Cutover chỉ tắt VM rồi sync phần delta |
| `--volume-type <type>` | Volume type ở đích cho mọi volume. Không truyền thì dùng type mặc định của cloud, hoặc type theo AZ trong `AZ_TYPE_MAP` (mục 7.4) |
| `--az <zone>` | AZ đích của server, và của volume khi không có `--volume-az`. Không truyền thì dùng `MIG_AZ`, không có nữa thì để scheduler đích tự chọn. Từ 7.1 còn là `<AZ>` trong tên các bản copy group (mục 4b.2) |
| `--volume-az <zone>` | (7.2) AZ Cinder của volume đích khi khác tên AZ compute. Không truyền thì `MIG_VOLUME_AZ`, không có nữa thì theo `--az` (mục 4c.4) |
| `--ip same\|new\|<addr>` | `same` (mặc định): giữ IP + MAC, tháo port nguồn lúc cutover. `new`: IP + MAC mới, không đụng port nguồn. `<addr>`: IP chỉ định, MAC mới (VM 1 NIC) |
| `--flavor <tên>` | Flavor cho VM đích. Mặc định: cùng tên với nguồn |
| `--flavor-fallback` | Đích không có flavor cùng tên thì dùng flavor mà `FLAVOR_MAP_FILE` map tới. App không bao giờ tạo flavor |
| `--security-group <sg1,sg2>` | Security group cho mọi port đích, thay bộ SG của nguồn. Tên lấy **nguyên văn**, không thêm `<AZ>-` |
| `--no-copy-secgroup` | (7.1) Không copy SG: mỗi port đích lấy SG **cùng tên** đã có sẵn ở đích, thiếu là FAIL, đúng như 7.0 (mục 4b.3) |
| `--start-source` | Bật lại VM nguồn sau khi xong. Chỉ đi với `--ip new\|<addr>` hoặc `--volumes-only` |
| `--keep-source-ports` | Với `--ip same`: không tháo port nguồn. Rủi ro split-brain nếu ai bật lại VM nguồn |
| `--server-group <tên\|ID>` | VM đích vào group có sẵn ở đích, thay cho bản copy group nguồn |
| `--no-server-group` | (7.1) Không theo server group nguồn: VM đích không vào group nào (trừ `--server-group`) |
| ~~`--keep-server-group`~~ | **Bỏ từ 7.1** (theo group nguồn đã là mặc định, mục 4b.4). Gõ là `ERROR: unknown option --keep-server-group`; nằm trong `MIG_DEFAULT_OPTS` cũng dừng. Script/conf cũ phải xóa cờ này (mục 4b.6) |
| `--volumes-only` | Chỉ cold: copy volume, không tạo port và VM đích |
| `--max-passes <n>` | Live: tối đa n lượt pre-sync (mặc định 5) |
| `--delta-limit <MB>` | Live: cutover khi delta một lượt ≤ ngưỡng (mặc định 64) |
| `--verify readback\|full\|none` | Cold: so toàn bộ, trừ khi `none`. Live: `readback` (mặc định) đọc lại từng chunk vừa ghi; `full` thêm một lần so toàn bộ lúc cutover; `none` không kiểm |

Mỗi option chi tiết ra sao (điều kiện, preflight, rủi ro) thì không đổi so với bản bash, **trừ phần port,
security group và server group của 7.1** (mục 4b). Xem [GUIDE.md](GUIDE.md) mục 2 và 2b; trong đó cứ đọc
`migrate-vm.sh migrate <vm> …` thành `migrate migrate <vm> … --yes`.

## 4b. Port, security group, server group ở đích giống nguồn (từ 7.1, P6)

### 4b.1. Tóm tắt

| Thứ | Đến 7.0 (và bản bash) | Từ 7.1, mặc định | Không muốn thì |
|---|---|---|---|
| Security group của port | Đích phải có sẵn SG **cùng tên**, thiếu là FAIL | Dùng `<AZ>-<tên SG>`, chưa có thì **tạo + chép đủ rule** | `--no-copy-secgroup` (= 7.0), hoặc `--security-group` (tên gõ tay, nguyên văn) |
| Server group | Chỉ khi `--keep-server-group`, tên `az-<tên>` | **Luôn** theo group nguồn, tên `<AZ>-<tên>` | `--no-server-group`, hoặc `--server-group <group có sẵn>` |
| allowed-address-pairs | Preflight FAIL (VM kiểu VIP/keepalived không chuyển được) | Chép từng cặp IP/MAC. `--ip new` thì bỏ kèm WARN | — |
| Port security tắt | Port đích vẫn bật port security, gắn SG | Port đích `--disable-port-security`, không SG | — |
| `description`, `dns_name` của port | Không chép | Chép (`dns_name` có điều kiện, mục 4b.5) | — |
| Port giữ chỗ phía nguồn (Nova xóa port của nó lúc tháo) | Giữ MAC/IP/SG | Giữ cả pairs, port security, description | — |
| Port có nhiều fixed IP | Từ chối | **Vẫn từ chối** | — |

Ví dụ: VM `web-01` ở nguồn có port với SG `web-tier`, nằm trong server group `web-anti`.

```bash
migrate migrate web-01 --az dc10-epz --yes
```

| Ở đích | Kết quả |
|---|---|
| Port `web-01-198.51.100.50` | SG `dc10-epz-web-tier` (tạo nếu chưa có, đủ rule của `web-tier`) |
| VM `web-01` | Vào server group `dc10-epz-web-anti` (tạo nếu chưa có, cùng policy với `web-anti`) |
| Sau `migrate rollback web-01` | Xóa VM, port, volume đích. **Giữ** `dc10-epz-web-tier` và `dc10-epz-web-anti` (VM khác có thể đang dùng) |

### 4b.2. `<AZ>` trong tên là gì

| Thứ tự | Lấy từ | Ví dụ |
|---|---|---|
| 1 | `--az` | `--az dc10-epz`: SG `web-tier` → `dc10-epz-web-tier` |
| 2 | `MIG_AZ` (env hoặc conf) | `MIG_AZ=AZ1`: group `web-anti` → `AZ1-web-anti` |
| 3 | Không có cả hai: **AZ compute duy nhất** của cloud đích (`openstack availability zone list --compute`, bỏ zone `internal` và zone `not available`) | Lab có đúng 1 AZ `nova`: `web-tier` → `nova-web-tier` |

- Bước 3 chỉ đọc, nên `plan` và `migrate` ra cùng một tên. Preflight in rõ nguồn gốc:
  `INFO  copies of the source server group and security groups are named nova-<source name>: nova is the only compute availability zone of the destination (no --az, no MIG_AZ)`.
- Đích có **0 hoặc từ 2 AZ** trở lên mà không có `--az`/`MIG_AZ` thì preflight FAIL, vì không biết scheduler sẽ
  đặt VM ở AZ nào:
  `FAIL  cannot name the destination copies of the source server group and security groups: no --az, no MIG_AZ, and the destination has 2 compute availability zones (AZ1 AZ2) — pass --az <zone> or set MIG_AZ (opt out: --no-server-group, --no-copy-secgroup)`.
- Chỉ hỏi AZ khi thật sự cần đặt tên: VM không có SG nào trên port và không ở server group nào thì không hỏi.
- AZ chỉ dùng để **đặt tên**. Không truyền `--az` thì volume và server vẫn tạo không chỉ định AZ như trước.
- Cùng group nguồn sang 2 AZ = 2 bản copy riêng (`AZ1-web-anti`, `AZ2-web-anti`): anti-affinity tính theo AZ.

### 4b.3. Copy security group

Mỗi SG trên port nguồn ứng với `<AZ>-<tên>` trong project đích:

| Ở đích | `plan` | `migrate` |
|---|---|---|
| Có đúng 1 SG tên đó | `OK    security group web-tier -> found dc10-epz-web-tier (<id>)` | Dùng luôn, **không so rule** |
| Chưa có | `OK    security group web-tier -> will create dc10-epz-web-tier (5 rules, + dc10-epz-db-tier referenced)` | Tạo, **trước khi tắt VM nguồn** (không cộng vào downtime) |
| Có 2 SG trùng tên | `FAIL  security group web-tier -> dc10-epz-web-tier: 2 security groups are named …` | — |

Khi tạo, app làm đúng thứ tự:

1. Tạo SG `<AZ>-<tên>`, mang description của SG nguồn; SG nguồn stateless thì bản copy cũng `--stateless`.
2. **Xóa 2 rule egress-any** mà Neutron tự thêm vào mọi SG mới, để bộ rule đích bằng đúng bộ rule nguồn.
3. Chép từng rule: direction, ethertype, protocol (cả dạng số, ví dụ VRRP `112`), port range (ICMP: type/code),
   remote CIDR, description.

Rule trỏ tới một SG khác (remote group):

| Nguồn | Đích |
|---|---|
| `web-tier` có rule "tcp 5432 từ group `db-tier`" | Copy luôn `db-tier` thành `<AZ>-db-tier` (đệ quy), rule trỏ sang bản copy |
| Rule trỏ chính nó (kiểu SG `default`: "mọi thứ từ chính group này") | Trỏ sang chính `<AZ>-default` |
| `app` trỏ `db`, `db` trỏ ngược `app` | Mỗi SG copy một lần (có chống vòng lặp), cả hai tạo xong mới chép rule |
| SG được trỏ đã có bản `<AZ>-<tên>` ở đích | Dùng bản đó, không đụng vào |

Các trường hợp dừng ở preflight (chưa tạo gì):

- Rule dùng **remote address group**: app không chép loại này. Tạo tay `<AZ>-<tên>` ở đích, hoặc dùng `--security-group`.
- Project nguồn có 2 SG cùng tên với SG trên port: tên bản copy sẽ mập mờ.

Tạo rule lỗi giữa chừng (ví dụ hết quota rule): app **xóa các SG vừa tạo trong lần chạy đó**, rồi dừng. Lúc này
VM nguồn **chưa bị tắt** và chưa có `volume-map`, nên sửa nguyên nhân rồi chạy lại `migrate` ngay, không cần
rollback.

- Bản copy đã có mà rule khác nguồn thì app không sửa. Muốn đồng bộ lại thì xóa bản copy ở đích (khi không còn
  port nào dùng), lần migrate sau tạo lại.
- `--security-group sg1,sg2` thắng mặc định: mọi port đích dùng đúng các tên đó, không thêm `<AZ>-`, phải có sẵn ở đích.
- Port nguồn không có SG nào → port đích `--no-security-group`, như trước.
- Rollback **giữ** mọi SG `<AZ>-*`. State ghi ở file `dst-security-groups` (`<id> <tên> created=yes|no`), rollback in
  `INFO destination security groups kept (…)`.

### 4b.4. Server group

| Nguồn | Ở đích | Kết quả |
|---|---|---|
| VM không thuộc group nào | — | Như cũ, không hint |
| VM thuộc group `web-anti` (policy `anti-affinity`) | Chưa có `<AZ>-web-anti` | `migrate` tạo, cùng policy và rule; `plan` chỉ báo `will create` |
| | Có `<AZ>-web-anti` cùng policy | Dùng luôn |
| | Có nhưng **khác policy** | Preflight FAIL. Muốn dùng nó thì `--server-group <id>`, không theo nguồn thì `--no-server-group` |
| VM thuộc 2 group | | Preflight FAIL, chọn bằng `--server-group <id>` hoặc `--no-server-group` |

- `--server-group <tên|ID>` thắng: VM vào group đó, app không nhìn group nguồn.
- Rollback giữ group (như 7.0).
- VM không thuộc group nào thì preflight không in dòng nào về server group.
- **`--keep-server-group` đã bỏ ở 7.1** (theo group nguồn là mặc định nên cờ này thừa). App coi nó là option lạ:

  | Cờ nằm ở | App báo (thoát mã 1, chưa đụng cloud) |
  |---|---|
  | Dòng lệnh: `migrate migrate web-01 --keep-server-group --yes` | `ERROR: unknown option --keep-server-group` |
  | Conf: `MIG_DEFAULT_OPTS="--keep-server-group --verify none"` | `ERROR: MIG_DEFAULT_OPTS: unknown option '--keep-server-group' (MIG_DEFAULT_OPTS="--keep-server-group --verify none")` |

  Xóa cờ là được: hành vi 7.1 không cờ chính là cái cờ này từng làm, chỉ khác tên (`<AZ>-<tên>` thay `az-<tên>`).

### 4b.5. Port đích mang gì từ port nguồn

| Thuộc tính port nguồn | `--ip same` | `--ip <addr>` | `--ip new` |
|---|---|---|---|
| Port security tắt | `--disable-port-security`, không SG | như `same` | như `same` |
| allowed-address-pairs | Chép từng cặp | Chép; cặp mang MAC của chính port đi theo MAC mới, MAC khác (ví dụ VRRP `00:00:5e:00:01:05`) giữ nguyên | **Bỏ**, preflight in WARN, log in sẵn lệnh `openstack port set --allowed-address …` để thêm lại |
| description | Chép | Chép | Chép |
| dns_name | Chép nếu đủ điều kiện dưới | như `same` | như `same` |

Ví dụ VM keepalived: port `198.51.100.50`, pairs `198.51.100.99` (VIP) và `198.51.100.98` với MAC VRRP. Với
`--ip same`, preflight in:

```
  INFO  port fa:16:3e:aa:bb:01: allowed-address-pairs copied: 198.51.100.99 (fa:16:3e:aa:bb:01), 198.51.100.98 (00:00:5e:00:01:05)
```

- **Port security tắt + `--security-group`** thì app dừng ngay (Neutron không cho gắn SG vào port tắt port security):
  `ERROR: port fa:16:3e:aa:bb:01 has port security disabled at the source, so its copy takes no security group: --security-group web conflicts with it — …`.
- Đích phải có extension tương ứng: `port-security` (port tắt port security), `allowed-address-pairs` (có pairs).
  Thiếu thì preflight FAIL.
- **`dns_name` chỉ chép khi** (1) đích có extension `dns-integration` và (2) `dns_name` đúng là hostname Nova sẽ
  đặt cho VM đích: tên VM viết thường, chỉ gồm `a-z 0-9 -`. Lý do: Nova từ chối port có `dns_name` khác hostname
  (`PortNotUsableDNS`, VM đích lên ERROR) và tự đặt `dns_name` = hostname khi có extension. Không đủ điều kiện thì
  preflight in WARN, port không mang `dns_name`, migrate vẫn chạy. Ví dụ VM `app01`, `dns_name app01`: chép;
  `dns_name web-vip`: WARN.
  ⚠ Có cloud **quảng cáo `dns-integration` nhưng không lưu** (ML2 thiếu extension driver): `port create
  --dns-name` echo về giá trị nhưng `port show` ra `None` — tool chép xong mà đích vẫn trống, không có lỗi nào.
  Nghi ngờ thì kiểm đích trước: `openstack port create --dns-name test … && openstack port show … -c dns_name`.
- Port giữ chỗ phía nguồn (`mig-reserve-<vm>-<i>`, khi Nova tự xóa port của nó lúc tháo) mang đủ pairs, port
  security, description như port gốc, nên rollback gắn lại vẫn đúng hành vi mạng.
  ⚠ Port này là port user-created: sau rollback, nếu xóa VM nguồn bằng tay thì Nova chỉ **detach** nó chứ không
  xóa — port DOWN ở lại giữ IP và security group (chặn cả việc xóa SG đó). Xóa VM xong phải xóa port
  `mig-reserve-*` của nó riêng: `openstack port list --device-owner "" | grep mig-reserve`.
- Tạo port với pairs hoặc tắt port security cần quyền admin hoặc chủ network (policy Neutron mặc định —
  lab có thể không chặn member, production nên probe trước bằng chính creds sẽ dùng).

### 4b.6. Chuyển từ 7.0 lên 7.1

1. ⚠ **Server group `az-<tên>` do 7.0 tạo (`--keep-server-group`) không được dùng lại.** 7.1 tìm `<AZ>-<tên>`.
   Ví dụ `web-01` đã migrate ở 7.0 nằm trong `az-web-anti`; `web-02` migrate ở 7.1 với `--az dc10-epz` vào
   `dc10-epz-web-anti`, nên 2 VM không còn anti-affinity với nhau. Muốn `web-02` vào group cũ thì
   `--server-group az-web-anti`. Nova không cho đổi tên server group.
2. **SG cùng tên đã tạo tay ở đích theo kiểu 7.0 (`web-tier`) không còn được dùng mặc định.** 7.1 tạo
   `<AZ>-web-tier`. Muốn giữ kiểu cũ: `--no-copy-secgroup` (đặt được trong `MIG_DEFAULT_OPTS`), hoặc
   `--security-group web-tier`.
3. Cloud đích nhiều AZ: phải có `--az` hoặc `MIG_AZ` (mục 4b.2).
4. ⚠ **BREAKING: `--keep-server-group` không còn.** Script, cron, `MIG_DEFAULT_OPTS` nào còn cờ này thì 7.1
   **dừng ngay** (`unknown option`, mục 4b.4). Xóa cờ trước khi lên 7.1, ví dụ
   `MIG_DEFAULT_OPTS="--keep-server-group --verify none"` → `MIG_DEFAULT_OPTS="--verify none"`.
5. State: `ports.json` thêm khóa `description`/`dns_name` khi port có giá trị; thêm file `dst-security-groups`.
   Bản bash (archive) bỏ qua các khóa/file này, rollback chéo vẫn được.
6. Bản bash trong `archive/` giữ hành vi cũ (`az-`, SG cùng tên, từ chối pairs).

## 4c. Thay đổi của 7.2 (sửa theo AUDIT-20261119)

Thiết kế: [DESIGN-V7-2.md](DESIGN-V7-2.md). Mã A1…D4 trong ngoặc là mã của [AUDIT-20261119.md](AUDIT-20261119.md).

### 4c.1. Cold: volume đích tạo TRƯỚC khi tắt VM nguồn (A1)

Thứ tự cold mới (live không đổi, presync vốn đã làm vậy):

1. export, preflight, copy SG (như 7.1);
2. **tạo mọi volume đích** (rỗng, chưa cần dữ liệu nguồn): `volume create`, chờ `available`, `rbd info`, set
   bootable và image property. Log: `destination volumes created; stopping the source now`;
3. tắt VM nguồn, tháo port, lock (bắt đầu downtime);
4. clone → attach → `qemu-img convert` + compare vào volume đã tạo; tạo port + VM đích.

Lợi ích: lỗi phía đích không phụ thuộc nguồn (quota, sai type, sai `RBD_POOL_MAP`, `mig-xfer` thiếu caps) lộ ra
khi VM nguồn **còn chạy**. Ví dụ volume type mặc định không vào `volumes-nvme`: 7.1 báo lỗi sau khi VM đã
SHUTOFF và port đã tháo; 7.2 dừng ở bước 2, VM vẫn ACTIVE, chỉ cần `migrate rollback <vm>` để xóa volume
rỗng vừa tạo. Size volume lấy từ `volumes.json` đã export, bớt một lệnh `volume show` trong downtime.

Cuối mỗi lần migrate có thêm dòng đo downtime (D3):
`downtime breakdown: total 95s, copy 40s, verify 22s, control-plane 33s` (copy/verify = `qemu-img
convert`/`compare` ở cold; ở live là pass blocksync cuối và lần compare của `--verify full`; control-plane = phần
còn lại: lệnh `openstack`, chờ, attach/detach).

### 4c.2. Preflight kiểm quota và dung lượng (A2)

Preflight kiểm **mọi tài nguyên** một lần migrate tiêu thụ, ở **cả hai cloud**, trước khi tạo bất cứ gì:

| Phía | Tài nguyên | Nhu cầu (ví dụ VM 2 vCPU/2 GB, volume 10 GB + 2 GB, 1 port) |
|---|---|---|
| nguồn | `volumes`, `gigabytes` | clone tạm: +1 volume, + size volume **lớn nhất** (10 GB; clone từng cái một) |
| nguồn | `volumes_<type>`, `gigabytes_<type>` | như trên, theo type của từng volume nguồn (khi project có quota theo type) |
| đích | `instances`, `cores`, `ram` | 1, vCPU và RAM của flavor đích (2, 2048 MB) |
| đích | `volumes`, `gigabytes` | số volume, tổng GB (2, 12) |
| đích | `volumes_<type>`, `gigabytes_<type>` | khi chọn type (`--volume-type`/`AZ_TYPE_MAP`) |
| đích | `ports`, `secgroups`, `secgroup-rules` | số port; số SG sẽ tạo và tổng rule của chúng |
| đích | `server_group_members` | 1, khi VM sẽ vào server group (giới hạn từ `limits show --absolute`) |
| đích | IP trống trong `allocation_pools` | 1 mỗi port, chỉ với `--ip new` |
| đích | dung lượng pool Ceph | tổng GB volume đích, đọc `ceph df detail` bằng `RBD_USER` |

Dòng in ra (ví dụ):

```
  OK    source quota: volumes 1/10 free, gigabytes 10/1000
  OK    destination quota: instances 1/10 free, cores 2/20, ram 2048/51200 MB, volumes 2/10, gigabytes 12/1000, ports 1/50
  OK    Ceph pool volumes: 12 GB needed, 1024 GB left (MAX AVAIL)
  FAIL  destination quota: gigabytes need 12, 5 free (used 95 of 100)
  FAIL  subnet 198.51.100.0/24: no free address in its allocation pools for --ip new
  INFO  Ceph pool capacity not checked: Error EACCES: access denied
```

- Số `a/b` là **cần/còn trống**. Còn trống = Limit − In Use − Reserved. Limit `-1` (không giới hạn) và tài nguyên
  cloud không trả về thì bỏ qua.
- Cloud không trả lời `openstack quota show --usage` (client cũ, thiếu quyền): `WARN … quota not checked`,
  **không chặn**.
- Pool Ceph: FAIL khi `MAX AVAIL` (hoặc quota của pool) nhỏ hơn nhu cầu; WARN khi sau copy còn dưới 10% pool.
  `ceph df` cần quyền đọc mon: caps `mon 'profile rbd'` của `mig-xfer` đã có sẵn quyền đó, không phải cấp thêm.
  Ceph từ chối (caps khác, không có lệnh `ceph`) thì chỉ ra INFO `Ceph pool capacity not checked: …`, không chặn.
- `--volumes-only` và `migrate volume` kiểm phần volume (cả clone phía nguồn).

### 4c.3. Floating IP: cảnh báo, không chuyển (A3)

Floating IP thuộc cloud nguồn, **không** sang được cloud khác. 7.2 ghi floating IP của từng port vào
`ports.json` (khóa `floating_ips`, chỉ khi có), `plan` in thêm dòng `  fip   203.0.113.10 on <mac>` và preflight
in `WARN port <mac>: floating IP 203.0.113.10 not migrated (…): allocate one at the destination and update DNS`.
Sau cutover: cấp floating IP ở đích, gắn vào port đích, sửa DNS. Rollback gắn lại floating IP vào port giữ chỗ khi
Nova đã xóa port gốc (Neutron bỏ liên kết theo port).

### 4c.4. `--volume-az` khi AZ Cinder đích khác tên AZ compute (C6)

Ví dụ đích có compute AZ `AZ1`, `AZ2` nhưng Cinder chỉ có AZ `nova`: `--az AZ1` một mình sẽ FAIL `volume AZ AZ1`.

```bash
migrate migrate web-01 --az AZ1 --volume-az nova --yes
```

Thứ tự ưu tiên của AZ volume: `--volume-az` gõ tay > `MIG_VOLUME_AZ` (conf/env) > `--volume-az` trong
`MIG_DEFAULT_OPTS` > AZ của server (`--az`/`MIG_AZ`, như 7.1). Header preflight thêm ` volume-az=nova` khi khác
`az=`; file state `options` thêm `VOLUME_AZ=` cuối dòng. `migrate volume` cũng nhận `--volume-az`.

### 4c.5. `migrate doctor` (D2)

Chạy **trước mọi thứ** trên host chuyển mới (hoặc sau khi nâng cấp): kiểm chương trình (`openstack`,
`qemu-img`, `rbd`, `iscsiadm`, `multipath` khi `MIG_MULTIPATH=1`), module của brick (os-brick, cinderclient),
`SYS_PY` import được `rados, rbd`, conf đọc được, 2 openrc + token, quyền admin (WARN nếu thiếu: `--ip same` cần
`port create --mac-address`), `rbd ls` từng pool, `iscsid`, thư mục state/log. Chỉ đọc, không cần VM; thoát mã 1
khi có FAIL. Hướng dẫn cài: [DEPLOY-PYTHON.md](DEPLOY-PYTHON.md) mục 0.

### 4c.6. Các thay đổi nhỏ khác

| Mã | Trước 7.2 | 7.2 |
|---|---|---|
| A4 | Tên VM trùng ở **2 project khác nhau**: chọn cả hai, `--yes` migrate cả hai | Mỗi VM một hàng `SKIP(ambiguous-name: use the ID)`; chọn bằng ID |
| B1 | `port show` lỗi tạm (503) sau khi tháo port bị hiểu là Nova đã xóa port | Chỉ "not found" mới là đã xóa; lỗi khác thì hỏi lại (tối đa 60 lần), hết thì dừng `cannot read port …`. Rollback gắn lại cả port chưa kịp ghi vào `source-ports` |
| B3 | Không có attachment `/dev/vda\|sda` là ephemeral | Đối chiếu trường `image` của server; mâu thuẫn thì dừng `cannot tell the root disk of …` |
| B4 | `--ip 999.1.1.1` lọt, IP ngoài subnet báo "free" | `--ip` phải là IPv4 thật; preflight FAIL `IP … is not in subnet …` |
| B5 | Rollback dừng giữa chừng khi `image list`/`volume list` nguồn lỗi | WARN rồi làm tiếp (unlock, gắn port, bật VM) |
| B6 | Image property `os_hash_*`/`os_hidden` của volume bị chép sang | Chỉ chép allowlist khách (`hw_*`, `os_distro`, `os_type`, …) như đường ephemeral |
| C1 | `--name ^<tên>$` không escape (tên có `.`) | Escape ở mọi chỗ |
| C2 | `--max-passes 0` chạy như 1 | `--max-passes` ≥ 1, `--delta-limit` ≥ 0 |
| C5 | Tên VM có `/` thành thư mục lồng | Dừng: chạy bằng ID |
| C7 | Preflight tự `systemctl start iscsid` | Chỉ kiểm; FAIL kèm `INFO start it: systemctl enable --now iscsid` |
| C8 | Nova tạo server nhưng client timeout: rollback bỏ sót server | Rollback tìm theo tên; đúng 1 server tạo sau `t-stop` thì xóa |

### 4c.7. Hạn chế còn lại của đường ephemeral (B2)

- VM đích của nguồn ephemeral vẫn boot **từ image**, **không có volume** (chờ user chốt
  `--boot-from-volume`). Với quy tắc "instance phải có volume", VM loại này cần xử lý tay sau khi migrate.
- Upload Glance dùng endpoint theo `OS_INTERFACE` của openrc đích (mặc định `public`, 7.1 ghim `internal`), lấy
  token mới trước mỗi lời gọi, và **đọc `OS_CACERT`/`OS_INSECURE`**: Glance HTTPS dùng CA nội bộ thì openrc đích
  (hoặc conf: `export OS_CACERT=…`) phải có `OS_CACERT`, nếu không upload báo `CERTIFICATE_VERIFY_FAILED`.
- Preflight kiểm chỗ trống của `/var/lib/mig-migrate` so với disk của flavor nguồn (FAIL khi thiếu, WARN khi
  flavor disk 0/không đọc được).
- Preflight kiểm luôn, khi VM nguồn còn chạy: `OS_CACERT` nạp được (`FAIL  OS_CACERT <path> cannot be loaded: …`)
  và catalog đích có image endpoint của `OS_INTERFACE` (`  OK    image endpoint <url> (public)`, thiếu thì
  `FAIL  the destination catalog lists no <iface> image endpoint …`).

## 4d. Machine-readable output (7.3)

For a UI or a script: every answer below is **one JSON object on stdout** (keys sorted, one line), the usual text
goes to **stderr**, the exit code is the one of the text command. Design: [DESIGN-V7-3-CLI.md](DESIGN-V7-3-CLI.md).

What every JSON answer guarantees (DESIGN-V7-3-CLI section 11):

- **No credentials.** The output never holds a key that looks like one (`OS_*`, `*_RC`, `PASSWORD`, `SECRET`,
  `TOKEN`, `AUTH_URL`, `CACERT`, `*KEY`) nor a value equal to `OS_PASSWORD`, `OS_APPLICATION_CREDENTIAL_SECRET` or
  `OS_TOKEN` of either openrc. If it would, the command stops with `ERROR: refusing to print JSON: …` and prints
  nothing on stdout. The conf part is a fixed whitelist (no `SRC_RC`/`DST_RC`, no `CEPH_*`).
- **Read-only.** `plan --json`, `doctor --json`, `list`, `inventory`, `status` run `openstack … list|show` and
  `token issue` only; anything else dies before it runs. `list`, `inventory`, `status` write nothing on this host;
  `plan --json` writes what `plan` writes (its plan directory).
- **All or nothing.** The object is printed once, complete; a failure gives an empty stdout and `ERROR:` on stderr.

### 4d.1. `options.json`: what a run was started with

Every `migrate migrate` (each VM) and `migrate volume` writes `/var/lib/mig-migrate/<vm>/options.json` next to the
old `options` line (unchanged). `effective` lists each option with its value and **where it came from**:

| `from` | Meaning |
|---|---|
| `cli` | typed on the command line (also: the zone of `--dst-host`, `detail` says so) |
| `MIG_DEFAULT_OPTS` | applied from `MIG_DEFAULT_OPTS` |
| `conf` | `MIG_AZ`, `MIG_VOLUME_AZ`, `AZ_TYPE_MAP`, `RBD_POOL_MAP`, `FLAVOR_MAP_FILE`, `PROJECT_MAP_FILE` (named in `detail`) |
| `az` | `volume_az` follows the server's AZ |
| `source` | taken from the source VM (flavor, security group and server group copies) |
| `default` | nothing set it |

```json
{"command": ["migrate", "migrate", "web-01", "--mode", "live", "--az", "AZ1", "--yes"],
 "effective": {"mode": {"value": "live", "from": "cli"}, "az": {"value": "AZ1", "from": "cli"},
               "volume_az": {"value": "AZ1", "from": "az"},
               "volume_type": {"value": "ssd", "from": "conf", "detail": "AZ_TYPE_MAP (AZ1)"},
               "pool": {"value": "volumes-nvme", "from": "conf", "detail": "RBD_POOL_MAP"},
               "flavor": {"value": "m1.large", "from": "source"},
               "security_groups": {"value": [{"src": "web", "dst": "AZ1-web", "action": "create"}], "from": "source"},
               "server_group": {"value": {"src": "web-anti", "dst": "AZ1-web-anti", "policy": "anti-affinity"},
                                "from": "source"},
               "dst_host": {"value": "", "from": "default"}, "…": "…"},
 "conf": {"path": "/opt/mig/migrate.conf", "found": true, "MIG_DEFAULT_OPTS": "", "AZ_TYPE_MAP": "AZ1:ssd", "…": "…"},
 "tool_version": "7.3.0", "started": "2026-11-20T09:10:03Z", "ident": "web-01", "server_id": "7c1e…", "kind": "vm"}
```

A rollback that finished writes `rollback.json` (`command`, `started`, `ended`, `rc`) into the
`.rolled-back.<epoch>` directory. State written by 7.2 or the bash build has no `options.json`: `status` and `list`
show `"options": null`.

### 4d.2. `plan --json`, `plan volume --json`, `doctor --json`

```bash
migrate plan --host cmp-03 --az AZ1 --json > plan.json      # the tables and preflights go to stderr
migrate doctor --json | jq '.passed, .counts'
```

A plan row: `kind`, `id`, `name`, `ident`, `status`, `project` (`src`, `dst`), `result` (`PLAN-OK`, `PLAN-FAIL`,
`READY`, `SKIP(...)`), `info` (the FAIL lines of a `PLAN-FAIL`), `env`, `command` (what `migrate` will run for that
row, e.g. `["migrate", "migrate", "web-01", "--az", "AZ1", "--yes"]`), `effective` (as in 4d.1), `checks`
(`[{"level": "OK", "text": "compute AZ AZ1"}, …]`), `counts` (`ok`, `warn`, `fail`, `info`), `log`. A `SKIP` row has
`command`, `effective`, `checks` null. Top level: `verb`, `log_dir`, `rows`, `totals`, `rc`. `doctor --json`:
`checks`, `counts`, `passed`.

### 4d.3. `migrate list`: the instances (or volumes) before choosing

```bash
migrate list --host cmp-03 --json
migrate list --project prod --volumes --json     # attached volumes (with their VM) first, then available ones
```

Same selectors and rules as `plan` (same `SKIP(state-exists)`, `SKIP(status)`, ambiguous names), but no export, no
preflight, no log directory. A VM row adds `host`, `az`, `flavor`, `image` (`""` when boot-from-volume), `boot`
(`bfv`/`ephemeral`), `networks` (`[{"network": "vlan120", "ips": ["192.0.2.30"]}]`), `volumes`
(`[{"id", "name", "size", "device"}]`), `locked` (when the listing says), `state` (`{"dir", "live", "started",
"options"}` or null). A volume row: `id`, `name`, `size`, `status`, `type`, `az` (empty: `volume list` has no zone
column), `bootable`, `attached_to` (`{"server_id", "server_name", "device"}` or null), `state` (`vol-<id>`).
Cost: one `server list` per selector value, one `volume list --all-projects --long`, one `project show` per project.
Without `--json`: the `Selected:` table. Selector values with `/` or a control character, and a `--vm-file` that is a
symlink, over 1 MB or over 10 000 lines, are refused.

### 4d.4. `migrate inventory`

Per cloud: `hosts` (`host`, `zone`, `state`, `status` of `compute service list --service nova-compute`), `projects`
(`[]` plus `projects_error` when the token may not list them, e.g. an application credential), `scope_project`,
`auth_type`. Destination also: `zones` (`compute`, `volume`; no `internal`, no unavailable zone), `flavors`
(`name`, `vcpus`, `ram`, `disk`), `volume_types`. `--count` adds `vms` to each source host (one listing per host, at
most 64 hosts; more: name them with `--host`). No token: `ERROR: cannot get a source token (…): run 'migrate doctor'
first`.

### 4d.5. `migrate status`

Reads `/var/lib/mig-migrate`, `/var/log/mig-batch` and `/proc/<pid>/cmdline` only, asks no cloud:
`live` (directories with a `volume-map`: `ident`, `kind`, `files`, `options`, `t_stop`, `dst_server_id`,
`volume_map`, `mtime`), `plan_only`, `rolled_back` (newest 50, `--all` up to 2000; `retired`, `options`,
`rollback`; `rolled_back_total`), `batches` (newest 50 too), `running` (the `migrate`/`migratetool` processes with
their command line, nothing of any other process), `skipped` (entries that are symlinks or leave the roots). A
directory that cannot be read gets `"error"`, the others are still listed.

### 4d.6. `--dst-host <hypervisor>`: put the new server on one host

```bash
migrate plan web-01 --az AZ1 --dst-host ceph-compute-11      # the preflight checks the host
migrate migrate web-01 --az AZ1 --dst-host ceph-compute-11 --yes
```

`server create --host` (Nova compute API **2.74**, **admin** at the destination; DEPLOY-PYTHON section 0). The
preflight FAILs, with the source still running, when the host is not in the destination's `compute service list
--service nova-compute`, is down or disabled, sits in another zone than `--az`/`MIG_AZ`, or the destination token
has no admin role. Without `--az` the host's zone becomes the AZ of the server and the volumes (an `INFO` line says
so); when `AZ_TYPE_MAP` maps that zone the preflight FAILs instead (the volume type and pool are chosen
before the zone is known): pass `--az <zone>` or `--volume-type`. Per run only: in `MIG_DEFAULT_OPTS` it stops the run. The state line `options` ends with `DST_HOST=<host>`.
Rollback is unchanged.

### 4d.7. `--progress-json`: progress lines for a UI

`migrate migrate … --progress-json` and `migrate volume … --progress-json` print, besides the usual log, lines
starting with `@@ ` on stdout (filter them with `grep '^@@ '`); without the flag nothing changes:

```text
@@ {"event":"phase","name":"cutover"}
@@ {"at":"2026-11-20T09:12:40Z","event":"t_stop"}
@@ {"event":"copy","mbps":180,"pct":37,"volume":"<source volume id>"}
@@ {"changed_mib":9,"event":"blocksync","pass":3,"pct":62,"volume":"<id>"}
@@ {"chunks":310,"event":"pass","mb":1180,"n":2,"secs":64.1,"volume":"<id>"}
@@ {"control_plane":111,"copy":133,"event":"downtime","total":245,"verify":0}
@@ {"dst_server_id":"<id>","event":"result","rc":0}
```

Phases: `presync` (live), `cutover`, `copy`, `verify`, `create`, `done`. `copy`/`verify`/`blocksync` come at most
once a second each; every volume ends with a `copy` at `pct` 100. Every run ends with `result` (failed ones too).

### 4d.8. Downtime breakdown of an ephemeral VM (D3b)

The Glance snapshot, download and upload of an ephemeral root disk is data going through this host: it now counts
as copy time, shown in brackets: `downtime breakdown: total 213s, copy 107s (glance 107s), verify 0s,
control-plane 106s` (7.2 printed `copy 0s, control-plane 213s`). Boot-from-volume VMs print the 7.2 line.

## 4e. Web UI (7.3, `migui`)

The same commands from a browser: `migui-web` (127.0.0.1:8787, through `ssh -L 8787:127.0.0.1:8787 <mig-host>`)
and `migui-runner` (root, the only process that starts `migrate`). The UI runs exactly the commands of 4d plus
`migrate migrate|volume <id> … --yes --progress-json` and `migrate rollback <target>`, shows each one verbatim before
it runs, one VM at a time, and asks for the number of VMs (or the VM name) to confirm. Install, login token, screens,
security model and uninstall: [migui/deploy/DEPLOY-UI.md](migui/deploy/DEPLOY-UI.md). Design:
[DESIGN-UI.md](DESIGN-UI.md).

## 5. Volume

### 5.1. Một volume: `migrate volume <volume>`

```bash
migrate volume data-01                                          # theo tên, trong project nguồn
migrate volume 5d0e9f3a-0000-4000-8000-00000000b001 --volume-type nvme-az1 --az AZ1 --verify none
migrate volume 5d0e9f3a-0000-4000-8000-00000000b001 --in-use    # volume đang gắn VM đang chạy: copy nóng
```

- Giống `migrate-vm.sh migrate-volume` cũ: **không hỏi**, chạy preflight trước rồi mới copy.
- Nhận `--volume-type`, `--az`, `--verify`, `--in-use`. Option dành cho server (`--ip`, `--flavor`,
  `--security-group`, `--server-group`, `--start-source`, `--keep-source-ports`, `--volumes-only`,
  `--flavor-fallback`, `--mode live`, và từ 7.1 `--no-server-group`, `--no-copy-secgroup`) bị từ chối.
- Volume `in-use` mà không có `--in-use` thì app từ chối và gợi ý 2 cách: copy nóng bằng `--in-use`, hoặc
  đi theo VM bằng `migrate migrate <vm> --volumes-only`.
- Bản copy giữ tên nguồn và mang description `migrated from <id>`.
- State nằm ở `/var/lib/mig-migrate/vol-<id>/`.
- Lệnh này không đi qua selector, nên tìm volume trong project của openrc. Volume thuộc project khác thì đặt
  project cho lần chạy đó, như bản bash: `SRC_PROJECT=proj-b DST_PROJECT=proj-b migrate volume data-01`.
  Theo đợt (`--project`) thì app tự đặt.

Copy nóng `--in-use` là bản **crash-consistent**: giống đĩa của một VM bị rút điện đúng lúc clone. Ví dụ
clone lúc 10:20:05Z thì dữ liệu ghi lúc 10:20:30 không có trong bản copy. Giải thích đủ: [GUIDE.md](GUIDE.md)
mục 2b.9.

### 5.2. Theo đợt: `migrate volume --project … | --vm …`

| Gõ | Copy gì | Điều kiện |
|---|---|---|
| `--project p --available` | Mọi volume `available` của project p. Giữ tên nguồn; nguồn không bị đụng, không VM nào bị dừng | Không dùng với `--host`, `--vm`, `--vm-file` |
| `--project p --attached` | Volume của các VM trong project p. Mỗi VM chạy như `migrate --volumes-only`: VM nguồn bị stop + lock, volume đích tên `<vm>-<device>`, không tạo VM đích | `--host` chỉ để thu hẹp |
| `--project p --all` | `--attached` trước, `--available` sau | Không dùng với `--host` |
| `--vm a,b` (hoặc `--vm-file`) | Volume của các VM đó. `--attached` là mặc định khi có `--vm` | |

- Hỏi `Type yes` như `migrate migrate` (mục 3).
- Volume `in-use` không nằm trong danh sách `--available`. App log số lượng của chúng:
  `proj-a: 2 in-use volume(s) not listed — they go with their VM (migrate volume --attached)`.
- Volume rời chỉ nhận `--volume-type`, `--az`, `--verify`, `--mode`. Option chỉ dành cho VM thì bị bỏ, kèm
  log `available volumes: ignoring VM-only options …`.
- Bị từ chối: `--mode live` (copy volume chỉ chạy cold), và `--in-use` (chỉ dùng cho 1 volume, mục 5.1).
- `--project p` mà không có `--available|--attached|--all` thì app dừng, bắt chọn một trong ba.

### 5.3. Dry-run theo đợt: `migrate plan volume …` (mới ở P5)

Nhận đúng bộ chọn của `migrate volume` theo đợt. Nó liệt kê đúng những gì `migrate volume` sẽ copy, rồi
dừng: không hỏi, không đổi gì. Thay cho `migrate-batch.sh --volumes … --plan-only` của bản cũ.

```bash
migrate plan volume --project proj-a --available    # volume available của proj-a sẽ được copy
migrate plan volume --project proj-a --all          # VM (preflight --volumes-only) rồi tới volume available
migrate plan volume --vm web-01,web-02              # preflight --volumes-only của 2 VM
```

| Hàng | Plan làm gì | RESULT |
|---|---|---|
| Volume available | **Không có preflight riêng**, giống batch cũ. Các check của volume (AZ, type, mã hóa, bản copy cũ ở đích, quyền RBD) chạy lúc `migrate volume` copy chính volume đó, trước khi tạo gì | `READY` |
| VM (`--attached`, `--vm`, phần đầu của `--all`) | Chạy preflight `plan <vm> --volumes-only` trong project của VM, in ra màn hình | `PLAN-OK` / `PLAN-FAIL` |
| Còn lại | Taxonomy SKIP như mục 2 | `SKIP(...)` |

Ví dụ: project `proj-a` có 4 volume không gắn VM nào, gồm `data-c`, một volume không tên, clone sót
`mig-clone-xyz` và volume `broken` đang `error`.

```
Plan:
KIND ID                                   NAME                     STATUS    PROJECT src -> dst               RESULT
vol  11111111-1111-4111-8111-111111111111 data-c                   available proj-a -> proj-a                 READY
vol  22222222-2222-4222-8222-222222222222 -                        available proj-a -> proj-a                 READY
vol  33333333-3333-4333-8333-333333333333 mig-clone-xyz            available proj-a -> proj-a                 SKIP(mig-clone)
vol  44444444-4444-4444-8444-444444444444 broken                   error     proj-a -> proj-a                 SKIP(status)
```

Kết quả: 2 volume sẽ được copy, thoát mã 0. Nếu chỉ còn 2 hàng SKIP thì thoát mã 1, vì không có gì để copy.

- Mã thoát như `plan` (mục 2).
- Một volume lẻ không có dry-run. `migrate plan volume data-01` dừng và chỉ sang `migrate volume data-01`:
  lệnh đó chạy các check chỉ đọc trước, và dừng ở dòng FAIL trước khi tạo gì.
- Gõ theo thói quen `migrate volume … --plan-only` thì app dừng và chỉ sang `migrate plan volume …`.

## 6. Rollback

Rollback = hoàn tác một lần migrate: **xóa những gì tool đã tạo bên đích, trả VM nguồn về như trước khi
chạy**. Nguồn không bao giờ mất gì — tool không xóa hay sửa VM/volume/port gốc. Cụ thể, theo thứ tự:

1. Xóa VM đích, các port tool tạo ở đích, và mọi volume đã copy sang đích (kể cả clone tạm `mig-*` còn
   sót nếu lần migrate chết giữa chừng).
2. Server group `<AZ>-<tên>` (7.0: `az-<tên>`, do `--keep-server-group` tạo) và từ 7.1 các security group
   `<AZ>-<tên>` ở đích **được giữ lại** — các VM khác có thể đang dùng (mục 4b).
3. Phía nguồn: gắn lại port nếu đã tháo, mở khóa (unlock) và bật lại VM. Ví dụ sau
   `migrate migrate web-01 --yes` thì `web-01` nguồn đang SHUTOFF + locked; `migrate rollback web-01`
   đưa nó về ACTIVE như cũ.
4. Với state volume (`vol-<id>`): chỉ xóa bản copy ở đích, volume nguồn không bị đụng.

**Rollback bằng đúng chữ đã dùng lúc migrate.** State nằm ở thư mục mang đúng chữ đó, và tool in sẵn lệnh
rollback ở cuối mỗi lần migrate.

| Đã chạy | Thư mục state | Rollback |
|---|---|---|
| `migrate migrate web-01 --yes` | `/var/lib/mig-migrate/web-01/` | `migrate rollback web-01` |
| `migrate migrate 3f1c2a4e-…-0000000000a1 --yes` | `…/3f1c2a4e-…-0000000000a1/` | `migrate rollback 3f1c2a4e-…-0000000000a1` |
| `migrate migrate --vm sg-vm1,sg-vm2 --yes` | `…/sg-vm1/`, `…/sg-vm2/` | `migrate rollback sg-vm1`, `migrate rollback sg-vm2` |
| `migrate migrate --host cmp-01 --yes` (hoặc `--project`) | `…/<id>/` mỗi VM, như batch cũ | `migrate rollback <id>` |
| `migrate volume --vm web-01 --yes` | `…/web-01/` | `migrate rollback web-01` |
| `migrate volume data-01`, hoặc theo đợt `--available` | `…/vol-<id>/` | `migrate rollback data-01`, `<id>` hoặc `vol-<id>` |

- Tên VM có ký tự đặc biệt của regex (`. [ ] * ( ) | $ ^ ? + { } \`) thì app chạy VM đó bằng ID, nên
  state và lệnh rollback cũng theo ID.
- **VM thuộc project khác project của openrc** thì rollback phải kèm đúng cặp project, lấy ở cột
  `PROJECT src -> dst`, ví dụ `SRC_PROJECT=proj-b DST_PROJECT=proj-b migrate rollback web-01`.
  - Với hàng `FAIL` và `SKIP(state-exists)`, app in sẵn lệnh có đủ cặp này:
    `  web-01: inspect, then to undo: SRC_PROJECT=proj-b DST_PROJECT=proj-b migrate rollback web-01`.
  - Với hàng `OK`, dòng `Rollback: migrate rollback web-01` của tiến trình con không kèm cặp này. Tự thêm
    nếu VM không thuộc project của openrc.
- Rollback chạy lại được nhiều lần và không bao giờ sửa hay xóa VM, volume, port gốc. Xong thì thư mục state
  đổi tên thành `<tên>.rolled-back.<epoch>`.
- Rollback chéo bản được: state do `migrate-vm.sh` tạo thì `migrate rollback` dọn được, và ngược lại.
- Có cả state VM lẫn state volume cùng tên thì state VM được ưu tiên. Khi đó rollback volume bằng ID hoặc
  `vol-<id>`.

## 7. Cấu hình

### 7.1. File `/opt/mig/migrate.conf`

Giữ nguyên file của bản bash: dạng `KEY=VALUE`, cùng tên biến, cùng đường dẫn (đổi bằng env
`MIG_CONF=<file>`). Thứ tự ưu tiên: **option CLI > biến môi trường > migrate.conf > mặc định.** Mẫu đủ mọi
biến kèm mô tả: [migrate.conf.example](migrate.conf.example).

```bash
COPY_STREAMS=4 migrate migrate web-01 --yes          # env đè conf cho riêng lần này
MIG_CONF=/opt/mig/migrate-az2.conf migrate plan web-01   # dùng bộ conf khác
MIG_AZ= migrate plan web-01                          # env rỗng: tắt MIG_AZ của conf cho lần này
```

**App đọc file chặt hơn bash.** Bash `source` file nên chạy được mọi thứ. App chỉ đọc dữ liệu:

| Dòng trong file | App |
|---|---|
| `RBD_POOL=volumes`, `RBD_POOL_MAP="a:b,c:d"`, `MIG_DEFAULT_OPTS='--verify none'` | Đọc |
| `MIG_AZ=AZ1 AZ_TYPE_MAP=AZ1:t1` (nhiều phép gán trên 1 dòng), comment `#` cuối dòng | Đọc |
| `export OS_CACERT=/etc/ssl/ca.pem` (biến ngoài danh sách conf) | Truyền xuống mọi lệnh con (`openstack`, `rbd`…) |
| `RBD_POOL="$OTHER"`, `RBD_POOL=$(cat /x)` | Dừng: `… line 3: $-expansion is not supported by migrate, write the value literally: …` |
| `source /etc/x`, `if …`, `RBD_POOL=a; rm -f /tmp/x` | Dừng: `… line 5: only KEY=VALUE assignments are supported, got: …` |
| `RBD_POOL='$literal'` | Đọc nguyên chữ `$literal` (trong nháy đơn bash cũng không thay) |

Vì vậy, trước khi chuyển sang app, chạy `migrate plan <vm-test>` một lần trên conf production để soát
([DEPLOY-PYTHON.md](DEPLOY-PYTHON.md) mục 5).

### 7.2. Các biến

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `SRC_RC`, `DST_RC` | `/opt/mig/{src,dst}/admin-openrc.sh` | Openrc 2 cloud |
| `RBD_POOL` | `mig-volumes` | Pool RBD khi type không có trong `RBD_POOL_MAP` |
| `RBD_POOL_MAP` | (trống) | Map volume type → pool (mục 7.4) |
| `RBD_USER` | `mig-xfer` | User Ceph của host chuyển |
| `CEPH_CONF` | `/etc/ceph/ceph.conf` | ceph.conf của cụm đích |
| `MIG_ISCSI_IP` | IP của lab: production **luôn** phải đặt | IP host chuyển trên mạng iSCSI |
| `MIG_MULTIPATH` | `0` | `1` = attach qua multipath |
| `COPY_STREAMS` | `8` | Số luồng `qemu-img convert` (cold) |
| `MIG_AZ` | (trống) | AZ khi không truyền `--az` |
| `MIG_VOLUME_AZ` | (trống) | (7.2) AZ volume khi không truyền `--volume-az`; trống thì volume theo AZ của server (mục 4c.4) |
| `AZ_TYPE_MAP` | (trống) | Map AZ → volume type (mục 7.4) |
| `MIG_DEFAULT_OPTS` | (trống) | Option luôn áp (mục 7.3) |
| `PROJECT_MAP_FILE` | (trống) | File map project nguồn → đích (mục 7.4) |
| `FLAVOR_MAP_FILE` | (trống) | File map flavor nguồn → đích, chỉ dùng với `--flavor-fallback` (mục 7.4) |
| `SRC_PROJECT`, `DST_PROJECT` | (trống) | Lệnh chạy theo selector (`plan`, `migrate`, `volume --project/--vm`, `plan volume`) **tự đặt** theo project của từng VM/volume (mục 9a). Chỉ đặt tay cho `volume <1 volume>` và `rollback`, như bản bash |
| `SYS_PY` | `/usr/bin/python3` | Python hệ thống chạy blocksync (cần `python3-rbd`). Như bản bash, giá trị trong conf thắng env |
| `MIG_CONF` | `/opt/mig/migrate.conf` | Đường dẫn file conf |

### 7.3. `MIG_DEFAULT_OPTS`: option luôn áp

Dùng cho option lần nào cũng phải nhớ gõ. App parse chuỗi này **trước** option trên dòng lệnh, nên với
option có giá trị thì dòng lệnh thắng.

```bash
# /opt/mig/migrate.conf
MIG_DEFAULT_OPTS="--no-copy-secgroup --verify none"
```

```bash
migrate plan web-03                  # preflight in: INFO  MIG_DEFAULT_OPTS applied before the command line: --no-copy-secgroup, --verify none
migrate plan web-03 --verify full    # … --verify none (command line wins): lần này so toàn bộ
MIG_DEFAULT_OPTS="" migrate plan web-03   # lần này không dùng default nào
```

- Option dạng cờ (`--no-copy-secgroup`, `--keep-source-ports`…) không tắt được từ dòng lệnh. Muốn bỏ thì
  đặt `MIG_DEFAULT_OPTS=""` cho lần chạy đó.
- Chuỗi tách theo khoảng trắng, nên giá trị chứa khoảng trắng không dùng được ở đây.
- `--keep-server-group` ở đây làm app dừng từ 7.1 (`MIG_DEFAULT_OPTS: unknown option '--keep-server-group' …`,
  mục 4b.4): xóa nó khỏi conf.
- `migrate volume` chỉ lấy `--volume-type`, `--az`, `--verify`. Option khác bị bỏ kèm 1 dòng INFO.
- `--in-use` không bao giờ được lấy từ đây. `rollback` không dùng biến này.
- Gõ sai tên option hoặc thiếu giá trị thì app dừng, thông báo nêu `MIG_DEFAULT_OPTS`.

### 7.4. Bốn map

Hai map đầu viết thẳng trong conf, dạng `khóa:giá-trị,khóa:giá-trị` và **không có khoảng trắng**. Hai map
sau là file riêng, mỗi dòng 2 cột phân cách bằng khoảng trắng, `#` là comment.

**`RBD_POOL_MAP`: volume type → pool RBD.** Bản chép từ `rbd_pool` trong `cinder.conf` của cloud đích.
App ghi thẳng vào Ceph nên phải biết pool; Cinder không có API nào trả về tên pool.

```bash
RBD_POOL_MAP="nvme-az1:volumes-nvme-az1,nvme-az2:volumes-nvme-az2"
```

| Mục | Nghĩa |
|---|---|
| `nvme-az1:volumes-nvme-az1` | Volume type `nvme-az1` nằm ở pool `volumes-nvme-az1`. App ghi `rbd:volumes-nvme-az1/volume-<id>` |
| `nvme-az2:volumes-nvme-az2` | Tương tự cho `nvme-az2` |
| (type khác, hoặc không có type) | Ghi vào `RBD_POOL` |

Khai sai thì app dừng ngay sau khi tạo volume đích, **trước khi ghi byte nào**:
`volume-<id> not visible in pool …: wrong RBD_POOL_MAP for type …`. Rollback dọn được volume đó.

**`AZ_TYPE_MAP`: AZ → volume type.** Quy ước của người vận hành, chỉ để khỏi gõ `--volume-type`.

```bash
MIG_AZ=AZ1
AZ_TYPE_MAP="AZ1:nvme-az1,AZ2:nvme-az2"
```

| Lệnh | AZ | Type | Pool (qua `RBD_POOL_MAP`) |
|---|---|---|---|
| `migrate migrate app-vm01 --yes` | `AZ1` (từ `MIG_AZ`) | `nvme-az1` (map) | `volumes-nvme-az1` |
| `migrate migrate app-vm02 --az AZ2 --yes` | `AZ2` | `nvme-az2` (map) | `volumes-nvme-az2` |
| `migrate migrate app-vm03 --az AZ2 --volume-type nvme-az1 --yes` | `AZ2` | `nvme-az1` (gõ tay thắng map) | `volumes-nvme-az1` |

AZ không có trong map thì như không có map. Chuỗi AZ → type → pool, cách tra ngược type → pool: [GUIDE.md](GUIDE.md) mục 2b.1b.

**`PROJECT_MAP_FILE`: project nguồn → project đích**, khi project đích khác tên.

```bash
PROJECT_MAP_FILE=/opt/mig/project-map.conf
```

```
# /opt/mig/project-map.conf
# project-nguồn   project-đích
finance           finance-prod
web-legacy        web
```

| Dòng | Nghĩa |
|---|---|
| `finance finance-prod` | VM của project `finance` ở nguồn sang project `finance-prod` ở đích |
| `web-legacy web` | VM của `web-legacy` sang `web` |
| (project không có dòng nào) | Giữ nguyên tên, ví dụ `proj-a` → `proj-a` |

- Dòng sai dạng thì app dừng: thiếu hoặc thừa cột, kể cả kiểu `finance = finance-prod`.
- File không đọc được thì app log WARN, mọi project giữ nguyên tên.
- **Khác bash: map áp cho mọi lệnh, kể cả `migrate migrate <1 VM>`.** Bản bash chỉ batch đọc map này.

**`FLAVOR_MAP_FILE`: flavor nguồn → flavor đích**, chỉ dùng khi có `--flavor-fallback` (gõ tay hoặc trong
`MIG_DEFAULT_OPTS`).

```bash
FLAVOR_MAP_FILE=/opt/mig/flavor-map.conf
```

```
# /opt/mig/flavor-map.conf
m1.large      c2.large
m1.xlarge     c2.xlarge
legacy.tiny   m1.small
```

| VM nguồn dùng | Đích có | Kết quả |
|---|---|---|
| `m1.large` | `m1.large` | Dùng `m1.large`: có cùng tên thì không tra map |
| `m1.large` | không có `m1.large` | Dùng `c2.large`. Preflight in `INFO  flavor m1.large -> c2.large from FLAVOR_MAP_FILE` |
| `m1.medium` | không có, map cũng không có dòng | `FAIL  flavor m1.medium — not at the destination and not in /opt/mig/flavor-map.conf: add a line 'm1.medium <dest-flavor>' …` |
| (bất kỳ, có gõ `--flavor m1.small`) | | Dùng `m1.small`, không tra map |

- Flavor đích trong map phải có sẵn ở đích, vì app không bao giờ tạo flavor.
- Flavor map ra nhỏ hơn nguồn thì preflight in WARN, ví dụ `vcpus 4 -> 2, ram 8192 -> 4096 MB`. VM vẫn được migrate.
- Bật flag mà biến rỗng, file không đọc được hoặc có dòng sai dạng thì app dừng.
- Chi tiết: [GUIDE.md](GUIDE.md) mục 2b.12.

## 8. Ví dụ lệnh

Chạy bằng `root` trên host chuyển.

```bash
# 1. Xem trước 1 VM, không đổi gì
migrate plan web-01

# 2. Migrate 1 VM (cold, giữ IP/MAC); script/cron thì bắt buộc --yes
migrate migrate web-01 --yes

# 3. Cả một compute host, đích AZ dc10-epz
migrate plan --host cmp-01 --az dc10-epz
migrate migrate --host cmp-01 --az dc10-epz --yes

# 4. Volume rời của một project: xem trước, rồi copy (giữ tên nguồn, nguồn không bị đụng)
migrate plan volume --project p --available
migrate volume --project p --available --yes

# 5. Copy nóng 1 volume đang gắn vào VM đang chạy (crash-consistent, không hỏi)
migrate volume 5d0e9f3a-0000-4000-8000-00000000b001 --in-use

# 6. Hoàn tác: đúng chữ đã gõ lúc migrate
migrate rollback web-01

# 7. Live, chỉnh ngưỡng: tối đa 8 lượt, cutover khi delta <= 32 MB, thêm so toàn bộ
migrate migrate web-02 --mode live --max-passes 8 --delta-limit 32 --verify full --yes

# 8. IP + MAC mới, volume type chỉ định, bật lại VM nguồn sau khi xong
migrate migrate web-03 --ip new --volume-type nvme-az1 --start-source --yes

# 9. Danh sách VM từ file, chạy tiếp dù có con FAIL
cat > /root/wave1.txt <<'EOF'
# đợt 1
app-vm01
app-vm02
3f1c2a4e-0000-4000-8000-0000000000a9   # VM trùng tên: ghi bằng ID
EOF
migrate plan --vm-file /root/wave1.txt
migrate migrate --vm-file /root/wave1.txt --yes --continue-on-error

# 10. Hai VM cùng server group nguồn web-anti: 7.1 theo group nguồn mặc định, không cờ nào.
#     Conf có MIG_AZ=dc10-epz: con đầu tạo dc10-epz-web-anti + các SG dc10-epz-*, con sau dùng lại
migrate migrate --vm web-01,web-02 --verify none --yes
migrate rollback web-01; migrate rollback web-02     # group và SG dc10-epz-* vẫn giữ lại

# 11. Đích thiếu flavor cùng tên: tra FLAVOR_MAP_FILE
migrate plan --vm app-vm01,app-vm03 --flavor-fallback --verify none

# 12. Chỉ copy volume của 2 VM (VM nguồn bị stop + lock, không tạo VM đích)
migrate plan volume --vm app-vm01,app-vm02
migrate volume --vm app-vm01,app-vm02 --yes

# 13. Bộ production điển hình: không tháo port nguồn, bỏ so sánh, AZ + type khớp nhau
migrate migrate --host cmp-01 --project proj-a --keep-source-ports --verify none \
  --az AZ1 --volume-type nvme-az1 --yes
```

## 9. Khác bản bash: phải biết trước khi chạy thật

**(a) ⚠ Một VM cũng tự chạy trong project của chính nó.** `migrate migrate web-01` đặt
`SRC_PROJECT`/`DST_PROJECT` theo project của `web-01`, đổi tên theo `PROJECT_MAP_FILE` nếu có, giống
cách batch cũ làm cho từng VM.

- Bản bash chạy lẻ mà quên đặt env thì VM đích rơi vào project của openrc (thường là `admin`). App hết
  lỗi này. Ví dụ VM `web-01` thuộc `proj-b`: VM đích, volume `web-01-vda` và port `web-01-198.51.100.50`
  đều nằm ở project `proj-b` của đích.
- Hệ quả: `SRC_PROJECT`/`DST_PROJECT` đặt tay trong env bị project của VM đè, khi chạy theo selector
  (`plan`, `migrate`, `volume --project/--vm`, `plan volume`). Project đích khác tên thì khai
  `PROJECT_MAP_FILE` (mục 7.4), không đặt `DST_PROJECT`.
- Hai lệnh vẫn đọc 2 biến này từ env, như bản bash: `migrate volume <1 volume>`, ví dụ
  `SRC_PROJECT=proj-b DST_PROJECT=proj-b migrate volume data-01`, và `migrate rollback` (mục 6).

**(b) State, log và rollback dùng đúng chữ bạn gõ.** `--vm sg-vm1` ghi state vào `…/sg-vm1/`, nên
rollback bằng `migrate rollback sg-vm1`. Chạy bằng ID thì rollback bằng ID. Batch cũ luôn chạy theo ID,
nên quen tay rollback bằng ID sau `--vm <tên>` sẽ ra `no state for …`. Tool luôn in sẵn lệnh rollback đúng.
`--host`/`--project` thì vẫn theo ID như batch cũ (bảng ở mục 6).

**(c) Đợt mà mọi hàng đều SKIP thì thoát mã 1.** Bash batch thoát 0. Ví dụ
`migrate migrate --vm web-01 --yes` khi `web-01` đã có state ra `SKIP(state-exists)`, kèm lệnh rollback,
và thoát mã 1. Gõ sai tên thì ra `SKIP(not-found)` và cũng thoát 1. Script kiểm `$?` sẽ thấy ngay.

**(d) `--max-passes`/`--delta-limit` không phải số nguyên thì app dừng ngay.** Ví dụ
`migrate migrate web-01 --mode live --max-passes x` ra `ERROR: --max-passes takes a whole number, not 'x'`
trước khi chạm vào cloud. Bản bash nhận rồi **lặp pre-sync vô hạn**.

**(e) Application credential dùng được qua selector**, khi VM thuộc đúng project của appcred. Batch
bash từ chối appcred thẳng.

**(p) Từ 7.1: security group và server group ở đích là bản copy `<AZ>-<tên>`, port đích mang pairs, port
security, description, dns_name.** Bản bash: SG cùng tên có sẵn, group `az-<tên>` chỉ khi `--keep-server-group`,
từ chối allowed-address-pairs. Chi tiết và cách chuyển từ 7.0: mục 4b.

- VM của project khác thì hàng đó `PLAN-FAIL` với
  `SRC_PROJECT/DST_PROJECT needs a password openrc: an application credential is bound to one project (…)`.
  Các VM còn lại vẫn chạy được (cùng `--continue-on-error`).
- Token appcred không liệt kê được `--all-projects` thì app liệt kê trong project của token.

Các điểm khác, nhỏ hơn:

| # | Bản bash | App |
|---|---|---|
| f | `migrate-vm.sh migrate <vm>` chạy thẳng | **Luôn hỏi `Type yes`**, trừ `--yes` (mục 3) |
| g | `migrate-vm.sh plan <vm>` chạy cả khi VM đã có state hoặc đang không ACTIVE/SHUTOFF | `SKIP(state-exists)` + lệnh rollback / `SKIP(status)`, không export đè lên state cũ |
| h | Batch giữ preflight trong log | `migrate plan` in preflight ra màn hình khi chạy |
| i | Option của VM đặt sau `--` (batch) | Cùng dòng lệnh, không có `--`. Gõ `--` thì app dừng kèm hướng dẫn |
| j | Mỗi lần chạy lẻ không có thư mục log | Mọi lần chạy (kể cả 1 VM) có `/var/log/mig-batch/<thời gian>/` |
| k | stderr của từng VM đi riêng | stderr của tiến trình con gộp vào stdout của `migrate` (vẫn ghi đủ vào log từng con) |
| l | Conf được `source` (chạy được lệnh bash) | Chỉ đọc `KEY=VALUE`, dòng bash phức tạp thì dừng kèm số dòng (mục 7.1) |
| m | Rollback ≥2 clone sót cùng tên: truyền sai đối số | Dọn từng clone (bug bash đã sửa) |
| n | Server property chứa `\` hoặc tab bị `@tsv` làm hỏng khi chép | Chép nguyên văn (bug bash đã sửa) |
| o | Message lỗi | Nhiều message rõ hơn. Các chuỗi mà người, test và script đang bám vào thì giữ nguyên văn ([DEVELOPING-PYTHON.md](DEVELOPING-PYTHON.md) mục 4) |

## 10. Quen tay bản bash: gõ gì bây giờ

| Bản bash | App |
|---|---|
| `migrate-vm.sh plan web-01 [opts]` | `migrate plan web-01 [opts]` |
| `migrate-vm.sh migrate web-01 [opts]` | `migrate migrate web-01 [opts] --yes` (hoặc trả lời `yes`) |
| `migrate-vm.sh rollback web-01` | `migrate rollback web-01` |
| `migrate-vm.sh migrate-volume data-01 [--in-use]` | `migrate volume data-01 [--in-use]` |
| `migrate-vm.sh rollback <volume-id>` | `migrate rollback <volume-id>` |
| `migrate-batch.sh --host cmp-01 --plan-only` | `migrate plan --host cmp-01` |
| `migrate-batch.sh --host cmp-01 --yes -- --az AZ1` | `migrate migrate --host cmp-01 --az AZ1 --yes` |
| `migrate-batch.sh --project p --volumes available --plan-only` | `migrate plan volume --project p --available` |
| `migrate-batch.sh --project p --volumes available --yes` | `migrate volume --project p --available --yes` |
| `migrate-batch.sh --vm a,b --volumes attached --yes` | `migrate volume --vm a,b --yes` |
| `SRC_PROJECT=p DST_PROJECT=p-new migrate-vm.sh migrate web-01` | Dòng `p p-new` trong `PROJECT_MAP_FILE`, rồi `migrate migrate web-01 --yes` |

Gõ kiểu cũ thì app dừng và chỉ cách gõ mới, chưa làm gì:

| Gõ | App báo |
|---|---|
| `migrate batch --vm a` | `there is no 'batch' command: plan and migrate take the selection themselves: …` |
| `migrate migrate-volume data-01` | `'migrate-volume' is now: migrate volume <volume> [--in-use]` |
| `migrate migrate --vm a -- --verify none` | `no '--' any more: the options for each VM go on the same command line` |
| `migrate migrate --vm a --plan-only` | `--plan-only is now a command of its own: migrate plan <vm> (or --vm/--host/--project/--vm-file)` |
| `migrate volume --project p --available --plan-only` | `--plan-only is now a command of its own: migrate plan volume --project <p> --available\|--attached\|--all, or migrate plan volume --vm <vms>` |
| `migrate migrate --project p --volumes available` | `--volumes is now: migrate volume --project <p> --available\|--attached\|--all, or migrate volume --vm <vms>` |
| `migrate plan --project p --volumes available` | `--volumes is now: migrate plan volume --project <p> …` |
| `migrate migrate web-01 --keep-server-group --yes` (7.0) | `unknown option --keep-server-group`: từ 7.1 bỏ cờ, theo group nguồn là mặc định (mục 4b.4) |

Một VM tên đúng là `volume` thì plan bằng `migrate plan --vm volume`, vì `migrate plan volume …` là lệnh
dry-run cho volume.

## 11. Không đổi so với bản bash

Hành vi bên dưới giống hệt bản bash. Tài liệu của bản bash vẫn đúng, chỉ cần đổi cách gõ lệnh theo mục 10:

| Chủ đề | Đọc |
|---|---|
| Luồng cold/live, rollback R1–R3, vì sao tháo port | [GUIDE.md](GUIDE.md) mục 1, 4 |
| Điều kiện phía đích, preflight từ chối gì, VM ephemeral | [GUIDE.md](GUIDE.md) mục 2 |
| Chuỗi AZ → type → pool, tra ngược pool | [GUIDE.md](GUIDE.md) mục 2b.1b |
| Quy tắc đặt tên ở đích, guard chống trùng | [GUIDE.md](GUIDE.md) mục 2b.3 |
| `--server-group`, `--volumes-only` | [GUIDE.md](GUIDE.md) mục 2b.5 |
| Copy nóng `--in-use` (crash-consistent) | [GUIDE.md](GUIDE.md) mục 2b.9 |
| `--keep-server-group` (bản bash và app 7.0; app 7.1 đã bỏ cờ, xem mục 4b.4) | [GUIDE.md](GUIDE.md) mục 2b.10 |
| `--flavor-fallback` | [GUIDE.md](GUIDE.md) mục 2b.12 |
| Quy hoạch network 2 cloud, các bẫy | [GUIDE.md](GUIDE.md) mục 3, 4 |
