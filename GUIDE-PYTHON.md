# Hướng dẫn dùng app `migrate` (bản Python, từ v7)

Từ v7, một lệnh `migrate` (package Python `migratetool/`) thay hẳn hai script `migrate-vm.sh` và
`migrate-batch.sh`. Hai script cũ nằm trong [archive/](archive/README.md), hướng dẫn của chúng vẫn là
[GUIDE.md](GUIDE.md).

App là **bản dịch giữ nguyên hành vi** của bản bash ([DESIGN-V7.md](DESIGN-V7.md)):

- cùng cách copy (cold, live), cùng preflight, cùng quy tắc đặt tên;
- cùng file cấu hình `/opt/mig/migrate.conf`;
- cùng thư mục state `/var/lib/mig-migrate/`, nên state do bản bash tạo thì app rollback được, và ngược lại.

Bằng chứng: app qua đủ 140/140 check của 5 bộ test cũ trên lab thật cùng 24/24 check feature V3–V6
([TEST-REPORT-20261118-v7.md](TEST-REPORT-20261118-v7.md)). Cái đổi là **cách gõ lệnh**, cùng vài
điểm cố ý khác mà mục 9 liệt kê. **Đọc mục 9 trước khi chạy thật.**

**Từ 7.1 (P6), app làm thêm việc bản bash không làm:** security group và server group ở đích là bản copy
`<AZ đích>-<tên nguồn>`, port đích mang theo allowed-address-pairs, port security, description, dns_name.
Đây là **mặc định mới**, khác 7.0. **Đọc mục 4b trước khi chạy 7.1**, nhất là phần chuyển từ 7.0.

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
| `migrate rollback <vm \| volume \| vol-<id>>` | Xóa phía đích, trả VM nguồn về như cũ; hoặc xóa bản copy của volume | Không | Có (chỉ phía đích + trả trạng thái nguồn) |

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
| `--az <zone>` | AZ đích cho **cả volume lẫn server**. Không truyền thì dùng `MIG_AZ`, không có nữa thì để scheduler đích tự chọn. Từ 7.1 còn là `<AZ>` trong tên các bản copy group (mục 4b.2) |
| `--ip same\|new\|<addr>` | `same` (mặc định): giữ IP + MAC, tháo port nguồn lúc cutover. `new`: IP + MAC mới, không đụng port nguồn. `<addr>`: IP chỉ định, MAC mới (VM 1 NIC) |
| `--flavor <tên>` | Flavor cho VM đích. Mặc định: cùng tên với nguồn |
| `--flavor-fallback` | Đích không có flavor cùng tên thì dùng flavor mà `FLAVOR_MAP_FILE` map tới. App không bao giờ tạo flavor |
| `--security-group <sg1,sg2>` | Security group cho mọi port đích, thay bộ SG của nguồn. Tên lấy **nguyên văn**, không thêm `<AZ>-` |
| `--no-copy-secgroup` | (7.1) Không copy SG: mỗi port đích lấy SG **cùng tên** đã có sẵn ở đích, thiếu là FAIL, đúng như 7.0 (mục 4b.3) |
| `--start-source` | Bật lại VM nguồn sau khi xong. Chỉ đi với `--ip new\|<addr>` hoặc `--volumes-only` |
| `--keep-source-ports` | Với `--ip same`: không tháo port nguồn. Rủi ro split-brain nếu ai bật lại VM nguồn |
| `--server-group <tên\|ID>` | VM đích vào group có sẵn ở đích, thay cho bản copy group nguồn |
| `--no-server-group` | (7.1) Không theo server group nguồn: VM đích không vào group nào (trừ `--server-group`) |
| `--keep-server-group` | 7.0: VM đích vào group `az-<tên group nguồn>`. **Từ 7.1 không còn tác dụng**: theo group nguồn đã là mặc định (tên `<AZ>-<tên>`, mục 4b.4). Vẫn nhận để script cũ chạy được, preflight in 1 dòng INFO. Không dùng chung với `--server-group` hay `--no-server-group` |
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
- `--keep-server-group` vẫn nhận nhưng không đổi gì, preflight in
  `INFO  --keep-server-group changes nothing now: the source server group is followed by default (opt out: --no-server-group)`.
  Gõ cùng `--no-server-group` thì app dừng (`choose one: …`). Nếu nó nằm trong `MIG_DEFAULT_OPTS` còn dòng lệnh gõ
  `--no-server-group` thì cái gõ tay thắng, kèm 1 dòng INFO.

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
- Port giữ chỗ phía nguồn (`mig-reserve-<vm>-<i>`, khi Nova tự xóa port của nó lúc tháo) mang đủ pairs, port
  security, description như port gốc, nên rollback gắn lại vẫn đúng hành vi mạng.
- Tạo port với pairs hoặc tắt port security cần quyền admin hoặc chủ network (policy Neutron mặc định).

### 4b.6. Chuyển từ 7.0 lên 7.1

1. ⚠ **Server group `az-<tên>` do 7.0 tạo (`--keep-server-group`) không được dùng lại.** 7.1 tìm `<AZ>-<tên>`.
   Ví dụ `web-01` đã migrate ở 7.0 nằm trong `az-web-anti`; `web-02` migrate ở 7.1 với `--az dc10-epz` vào
   `dc10-epz-web-anti`, nên 2 VM không còn anti-affinity với nhau. Muốn `web-02` vào group cũ thì
   `--server-group az-web-anti`. Nova không cho đổi tên server group.
2. **SG cùng tên đã tạo tay ở đích theo kiểu 7.0 (`web-tier`) không còn được dùng mặc định.** 7.1 tạo
   `<AZ>-web-tier`. Muốn giữ kiểu cũ: `--no-copy-secgroup` (đặt được trong `MIG_DEFAULT_OPTS`), hoặc
   `--security-group web-tier`.
3. Cloud đích nhiều AZ: phải có `--az` hoặc `MIG_AZ` (mục 4b.2).
4. `MIG_DEFAULT_OPTS="--keep-server-group …"` vẫn chạy, chỉ thêm 1 dòng INFO. Nên bỏ khỏi conf.
5. State: `ports.json` thêm khóa `description`/`dns_name` khi port có giá trị; thêm file `dst-security-groups`.
   Bản bash (archive) bỏ qua các khóa/file này, rollback chéo vẫn được.
6. Bản bash trong `archive/` giữ hành vi cũ (`az-`, SG cùng tên, từ chối pairs).

## 5. Volume

### 5.1. Một volume: `migrate volume <volume>`

```bash
migrate volume data-01                                          # theo tên, trong project nguồn
migrate volume 5d0e9f3a-0000-4000-8000-00000000b001 --volume-type nvme-az1 --az AZ1 --verify none
migrate volume 5d0e9f3a-0000-4000-8000-00000000b001 --in-use    # volume đang gắn VM đang chạy: copy nóng
```

- Giống `migrate-vm.sh migrate-volume` cũ: **không hỏi**, chạy preflight trước rồi mới copy.
- Nhận `--volume-type`, `--az`, `--verify`, `--in-use`. Option dành cho server (`--ip`, `--flavor`,
  `--security-group`, `--server-group`, `--keep-server-group`, `--start-source`, `--keep-source-ports`,
  `--volumes-only`, `--flavor-fallback`, `--mode live`, và từ 7.1 `--no-server-group`, `--no-copy-secgroup`)
  bị từ chối.
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
- `--keep-server-group` (không còn tác dụng từ 7.1, mục 4b.4) ở đây mà dòng lệnh gõ `--server-group db-anti`
  hoặc `--no-server-group` thì cái gõ tay thắng: app bỏ keep kèm 1 dòng INFO, không dừng.
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

# 10. Hai VM cùng server group nguồn web-anti (7.1: mặc định, không cần flag): con đầu tạo
#     dc10-epz-web-anti + các SG dc10-epz-*, con sau dùng lại
migrate migrate --vm web-01,web-02 --az dc10-epz --verify none --yes
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
| `--keep-server-group` (bản bash và app 7.0; app từ 7.1: mục 4b.4) | [GUIDE.md](GUIDE.md) mục 2b.10 |
| `--flavor-fallback` | [GUIDE.md](GUIDE.md) mục 2b.12 |
| Quy hoạch network 2 cloud, các bẫy | [GUIDE.md](GUIDE.md) mục 3, 4 |
