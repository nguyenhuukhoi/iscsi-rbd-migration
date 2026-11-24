# Thiết kế chuẩn Migrate

Tài liệu mô tả code của bản phát hành hiện tại (số bản: `migrate --version`). Tên hàm trong ngoặc là điểm truy vết khi hành vi không hiển nhiên. [GUIDE.md](GUIDE.md) định nghĩa cú pháp vận hành; [DEPLOY.md](DEPLOY.md) định nghĩa cách dựng host; [RUNBOOK.md](RUNBOOK.md) định nghĩa các bước nghiệm thu. Lịch sử chỉ nằm trong [CHANGELOG.md](CHANGELOG.md).

## 1. Kiến trúc và tiến trình

Hai cloud độc lập không chia sẻ Nova, Keystone, Neutron hoặc storage. Tool tạo tài nguyên mới ở đích. UUID server, volume và port thay đổi. Dữ liệu volume đi qua clone iSCSI read-only rồi ghi thẳng RBD của volume do Cinder đích tạo. Tool không chuyển RAM, phiên TCP, snapshot history hay Cinder backup.

```text
CLI selector (project/host/VM)
  +-- plan children, 1..8 song song, chỉ đọc API
  +-- xác nhận
  +-- mỗi VM một child, tuần tự, sys.executable -I -m migratetool.single
        +-- openstack CLI -> API nguồn và đích, scope riêng từng cloud
        +-- sys.executable -I -m migratetool.brick -> os-brick/Cinder
        +-- qemu-img -> clone RO -> precreated RBD (cold)
        +-- SYS_PY -I blocksync.py -> rados/rbd (live)

Browser -> SSH tunnel -> web (migui) -> Unix socket -> runner (root) -> CLI
```

`selector.Runner.child` cách ly mỗi VM bằng process và env project riêng. `clouds.Cloud` dựng argv, không shell cho lệnh OpenStack. `clouds.rc_env` là ngoại lệ có chủ đích: Bash source openrc tin cậy trong env đã bỏ `OS_*`, với `set -e`. Thiếu `OS_AUTH_URL` hoặc lệnh openrc thất bại làm dừng. Openrc không được hỏi mật khẩu tương tác.

Venv chứa OSC, os-brick và SDK. `blocksync.py` chạy bằng Python hệ thống có librbd, không import vào engine. `volcmd.dst_volumes_described` dùng SDK trong `sys.executable -I` để lọc description, không phải chuyển toàn bộ đường API sang SDK. `proc.run` và `proc.run_stream` quản lý subprocess.

Web không import engine và không đọc credentials/state migration. Runner probe CLI đã cài để biết phiên bản, flags, conf whitelist, khóa và các secret cần scrub. Runner loại `OS_*` khỏi env gọi CLI; CLI tự source đúng openrc. Wheel chỉ chứa `migratetool`; UI được cài riêng từ checkout (`pyproject.toml`, `migui/deploy/install.sh`).

## 2. Option và phạm vi cloud

`config.load` hợp nhất env > conf > defaults. Env đã có, kể cả rỗng, thắng conf. Sau đó giá trị rỗng dùng default của khóa nếu có. `SYS_PY` có thứ tự conf > env > `/usr/bin/python3`. `MIG_CONF` chọn file; không có file thì dùng env/default. File conf không thực thi shell (`parse_conf_file`). Các assignment khác chỉ truyền xuống khi có `export`.

`Options.load_default_opts` và `Options.parse_option_list` áp `MIG_DEFAULT_OPTS` trước argv CLI. Chuỗi mặc định tách whitespace, không hỗ trợ giá trị có khoảng trắng. Cờ boolean chỉ bật, không có cờ phủ định tổng quát. `--dst-host` bị từ chối trong mặc định; `--in-use` bị bỏ qua kèm INFO. Volume rời chỉ lấy type/AZ/volume-AZ/verify từ chuỗi này.

`Options.apply_az_defaults` đặt thứ tự chuyên biệt:

| Giá trị | Thứ tự thắng |
|---|---|
| Compute AZ | CLI `--az` → `MIG_AZ` → `--az` trong default opts |
| Volume AZ | CLI `--volume-az` → `MIG_VOLUME_AZ` → default opts → compute AZ |
| Type (từng volume, 1.2.0) | CLI `--volume-type` (mọi volume) → `VOLUME_TYPE_MAP[type nguồn]` → `VOLUME_TYPE_MAP[__DEFAULT__]` → type của lần chạy: `AZ_TYPE_MAP` theo compute AZ → default opts → cloud default (`Options.type_for`). Root ephemeral không có type nguồn: `__DEFAULT__` hoặc type của lần chạy |
| Pool (từng volume) | Mục đầu khớp type của volume đó trong `RBD_POOL_MAP` → `RBD_POOL` (`Options.pool_for`); `r.pool` là pool của type của lần chạy (header, dòng `options`) |
| Flavor | CLI override → cùng tên nguồn → map nếu bật fallback và cùng tên không có |
| Project | Selector lấy project đối tượng → `PROJECT_MAP_FILE` cho đích; volume đơn/rollback dùng scope được cung cấp |

Map project/flavor có đúng hai cột, comment `#`. File project map không đọc được là lỗi; không âm thầm quay về cùng tên (`Runner.load_pmap`). Flavor map chỉ được dùng khi bật fallback; thiếu file là lỗi. Flavor nhỏ hơn nguồn chỉ WARN. Tool không tạo flavor.

`--dst-host` cần host `up`/`enabled`, đúng AZ và admin role. Khi chưa có AZ, lấy zone của host; nếu zone này nằm trong `AZ_TYPE_MAP` nhưng type chưa được CLI đặt thì FAIL, yêu cầu `--az` hoặc `--volume-type` để tránh pool đã chọn sai (`dst_host_preflight`). API tạo server dùng Nova 2.74. Doctor kiểm max microversion; preflight host không tự kiểm lại max microversion đó.

Hai credential phải thuộc hai Keystone khác nhau. Doctor FAIL khi URL trùng; đường chạy thông thường chỉ WARN. App credential không rescope được. Selector tránh override scope khi đã đúng project app credential (`Runner.row_env`). Password account cần role tại mọi project liên quan.

## 3. Các luồng dữ liệu

### 3.1. Chuẩn bị chung và cửa sổ downtime

`vmcmd.cmd_migrate` lấy khóa, xử lý state cũ, export server/attachments/ports/volumes, nhận diện boot và chạy preflight. `vmdef.detect_boot` đối chiếu root attachment `/dev/vda` hoặc `/dev/sda` với trường image của server; mâu thuẫn không được đoán tùy tiện. Preflight FAIL dừng trước create/cutover.

Sau preflight, tool ghi `options` và `options.json`, tạo SG cần thiết trước `volume-map`. Tạo SG lỗi thì cố dọn SG vừa tạo, nguồn còn nguyên. `volume-map` có mặt là state sống dù file rỗng. Trước marker cutover, `recheck_source` so lại tập volume ID, port ID và tên flavor với export. Khác thì dừng; không so lại toàn bộ properties hoặc rules.

```text
preflight -> options -> SG -> volume-map -> chuẩn bị/copy trước
                                                |
                                     recheck_source
                                                |
                       t-stop (ghi trước lệnh stop)
                                                |
                     unlock nếu cần -> stop -> SHUTOFF
                                                |
                    detach/reserve -> snapshot nếu cần -> lock
                                                |
                     copy cuối/verify -> port/server -> ACTIVE
```

`t-stop` là marker bắt đầu cửa sổ, không phải bằng chứng stop đã thành công. Nguồn vốn SHUTOFF vẫn có marker. Downtime engine kết thúc khi server đích ACTIVE hoặc volume-only copy xong. Nó không đo thời điểm ứng dụng phục vụ lại. `t_copy`, `t_verify`, `t_glance` và phần còn lại control-plane được tách trong log; clone/attach/API wait nằm trong overhead nếu không thuộc timer copy.

### 3.2. Cold boot-from-volume

| Bước | Trước downtime | Trong downtime | Hàm |
|---|---|---|---|
| Export/preflight, SG | Có | Không | `cmd_migrate`, `secgroups.ensure` |
| Tạo mọi Cinder volume đích, đợi available, kiểm RBD image, copy image properties allowlist | Có | Không | `copyvol.create_dest_volume` |
| Recheck nguồn | Có | Không | `recheck_source` |
| Ghi t-stop; unlock lock cho phép; stop và đợi SHUTOFF | Không | Có | `cmd_migrate` |
| Với same: tháo port, tạo reserve nếu Nova xóa port | Không | Có | `server.detach_source_ports` |
| Lock nguồn | Không | Có | `cmd_migrate` |
| Mỗi volume: clone → attach RO → size guard → convert → compare → detach → xóa clone | Không | Có | `copyvol.copy_volume_cold` |
| Tạo port, server group nếu cần, server BFV; đợi ACTIVE | Không | Có | `server.create_destination` |
| Nếu được yêu cầu, unlock/start nguồn | Sau kết quả copy | Có thể phát sinh thêm thời gian | `cmd_migrate` |

`attach_clone` dùng Cinder clone `mig-clone-<source-volume-id>`, không attach volume gốc. Brick yêu cầu attachment read-only và `blockdev --setro`. Guard so kích thước device đúng Cinder size; khi bật multipath phải là device dm. WWN được log nhưng không tự xác minh identity với mảng.

`qemu-img convert` dùng raw, `-n --target-is-zero`, `-m COPY_STREAMS`, `-W`, ghi vào `volume-<destination UUID>` đã kiểm trong pool. Mọi verify khác `none` đều gọi full `qemu-img compare` khi cold. Không tự resume vùng copy dở. Lỗi cố detach clone, giữ state để quyết định dọn/rollback.

### 3.3. Live boot-from-volume

Trước downtime, với từng volume theo thứ tự: tạo volume đích rồi chạy các pass pre-sync. Không round-robin qua nhiều volume. Mỗi pass tạo clone mới, attach RO, đọc toàn bộ logical volume, ghi delta rồi detach/xóa clone (`livesync.sync_volume_pass`).

`blocksync.read_exact` lặp `pread` cho đủ chunk. Chunk 4 MiB, hash BLAKE2b 128-bit. Lượt đầu bỏ chunk toàn zero vì image mới; lượt sau ghi chunk đổi. Zero mới dùng discard, đọc lại để xác nhận zero, fallback write nếu cần. `readback` đọc lại chunk đã ghi qua cùng handle librbd (có `rbd_cache`): chứng minh ghi đúng offset, không chứng minh dữ liệu đã tới OSD; `full` (qemu-img compare, tiến trình khác) thêm compare toàn bộ ở pass cutover cuối và là kiểm độc lập. EOF/lỗi đọc làm blocksync exit 4, không thay hash cache. Exit nội bộ này được engine phân loại theo phase.

Chỉ kiểm ngưỡng delta từ pass thứ 2. Dừng presync khi delta ≤ `--delta-limit`, hoặc tới `--max-passes`. Không hội tụ vẫn WARN rồi cutover. Vì vậy giới hạn pass không bảo đảm downtime. Sau recheck, nguồn stop/port detach/lock như cold. Mỗi volume có pass `final` từ clone mới sau stop; sau đó tạo server đích. Hash cache hỗ trợ delta trong lần chạy, không phải journal cho crash-resume.

### 3.4. Root ephemeral

Chỉ cold. Flavor có thêm ephemeral disk hoặc swap disk bị từ chối; root image-backed không đồng nghĩa flavor ephemeral extra disk (`flavor_extra_disks`). `--volumes-only` trên VM ephemeral bị từ chối.

Preflight kiểm chỗ staging theo flavor disk, CA và image endpoint đích. Trong downtime, sau stop/detach và trước lock nguồn, `ephemeral.migrate_ephemeral_root` tạo snapshot Glance nguồn `mig-snap-<ident>`, download `root.img`, đọc virtual size, tạo image đích rồi ghi ngay `dst-image-id`, upload và so SHA512 nếu Glance cung cấp đúng hash algorithm. Nếu không đủ hash metadata thì WARN. Chỉ properties trong allowlist được chuyển.

Mặc định tạo Cinder root volume `<vm>-vda` từ image vừa upload. Size là max(flavor disk, virtual size làm tròn GiB). Ghi `ephemeral-root <id>` vào `volume-map`; không có file riêng tên `ephemeral-root`. Đợi available, kiểm RBD, xóa image đích theo best effort. `dst-image-id` vẫn là dấu vết để rollback. Tạo server bằng `--volume`. Snapshot nguồn và file staging được dọn theo đường thành công; lỗi có thể để lại để điều tra.

`--ephemeral-as-image` tạo server bằng `--image`, không tạo root volume, giữ image đích. Glance copy/hash vẫn nằm trong downtime dù `--verify none`. Data volume Cinder đi theo cold path. `ephemeral.image_endpoint` chọn endpoint theo `OS_INTERFACE`; hiện lấy URL khớp đầu tiên, không lọc `OS_REGION_NAME`. Canary phải xác minh endpoint trong catalog nhiều region.

### 3.5. Volume đơn và volume-only

`volcmd.cmd_migrate_volume` resolve một volume, kiểm status available hoặc in-use có cờ rõ ràng, export `volume.json`, preflight, tạo volume đích, clone/copy/verify cold rồi dọn clone. Giữ tên nguồn; nếu tên rỗng bị đích từ chối thì fallback UUID nguồn. Description là `migrated from <source-id>`. SDK tìm description này để chặn copy trùng. Nguồn không bị stop hay thay dữ liệu.

`--in-use` lấy bản crash-consistent tại lúc clone và ghi `clone-time`. Không quiesce guest, không snapshot đồng thời nhiều volume. Multiattach bị chặn ở nhánh in-use. Volume available không có cùng kiểm multiattach như luồng VM. Volume encrypted bị chặn.

`volume --attached` chạy VM với `--volumes-only`: cold stop/lock nguồn, không tạo port/server đích, không kiểm network/flavor/keypair như full VM. `--start-source` có thể bật lại nguồn sau copy nếu được duyệt. Standalone volume không có A6, không phân loại lỗi 3/4 theo cutover.

## 4. State, khóa và chạy lại

CLI đặt umask 077. Gốc state `/var/lib/mig-migrate`; state VM `<ident>`, volume đơn `vol-<source-id>`. `StateDir.create` tạo thư mục 0700. File không phải một transaction xuyên API. `write`/`append` flush+fsync file và cố fsync thư mục. Đọc lỗi khác ENOENT làm dừng. JSON record dùng O_EXCL/O_NOFOLLOW, mode 0600; không có cam kết fsync tương đương journal.

| File trong state | Nội dung / nơi tạo |
|---|---|
| `server.json` | Export Nova; gồm status nguồn làm cơ sở restore (`export_definition`) |
| `attachments.json` | Volume ID và device của VM |
| `port-ids.json` | ID port trước detach |
| `ports.json` | Network, CIDR, MAC, fixed IP, SG, AAP, security, DNS, description, floating IP export |
| `volumes.json` | Chi tiết Cinder volume của VM |
| `volume.json` | Export volume đơn |
| `options` | Một dòng `MODE IPMODE IPADDR VOLUME_TYPE POOL VERIFY FLAVOR SG EPHEMERAL AZ VOLUME_AZ`; `DST_HOST` khi dùng; format volume riêng |
| `options.json` | Command, effective với provenance, conf whitelist, tool_version, started, ident, kind; server_id hoặc volume_id; owner_mark cho VM |
| `volume-map` | Mỗi dòng `<source-volume-id> <destination-volume-id>`; root mới dùng source token `ephemeral-root` |
| `volume-types` | 1.2.0, chỉ app ghi: mỗi dòng `<source-volume-id> <type> <pool>` (`ephemeral-root` thay id cho root ephemeral, `-` thay type default), ghi cùng lúc với dòng `volume-map` (`copyvol.record_volume`); copy cold/blocksync đọc lại pool của từng volume từ đây. `volume-map` giữ hai cột để rollback (và bản bash) đọc như cũ; rollback không cần file này |
| `t-stop` | Epoch ghi trước stop; marker phân loại lỗi |
| `source-ports` | ID port tháo hoặc reserve giữ IP/MAC để trả nguồn |
| `dst-ports` | ID port đích đã tạo |
| `dst-server-id` | Có thể rỗng trong cửa sổ create; UUID khi nhận được response |
| `dst-image-id` | Image đích; có thể đã bị dọn sau tạo BFV |
| `dst-security-groups` | `<id> <name> created=yes/no`; không xóa khi rollback |
| `dst-server-group` | `<id> created=yes/no`; không xóa khi rollback |
| `user-data.yaml` | `ssh_deletekeys: false`, `preserve_hostname: true`; không phải user-data gốc |
| `hash-<vid>.bin`, `hash-<vid>.bin.tmp` | Hash cache và file thay thế của blocksync |
| `sync-<vid>.log` | Một dòng JSON thống kê mỗi pass |
| `root.img` | Staging snapshot root; temporary |
| `brick-attach.err` | Stderr attach cho state đó |
| `server-group.err` | Stderr nhánh tạo group/fallback rules |
| `create.err` | Lỗi tạo volume tên rỗng |
| `clone-time` | Mốc clone volume đơn in-use |
| `rollback.json` | Ghi khi rollback thành công; nằm trong thư mục retire |

Ngoài state: `/var/lib/mig-attach/<clone-id>.json` chứa attachment/connection/device và WWN. Detach thành công chuyển thành `<clone-id>.detached.json`, không mất record (`brick.detach`). Đây là dữ liệu root-only, có thể chứa thông tin kết nối storage; không đưa raw vào ticket công khai.

Mỗi selector run tạo `/var/log/mig-batch/<YYYYmmdd-HHMMSS>-<6hex>/`: `list-<n>.json`, `vms.json`, `selected.jsonl`, `volumes-<project>.json` khi dùng; `plan-<id>.log`, `migrate-<id>.log`. Không phải mọi run có đủ mọi file (`Runner._write`, `Runner.child`).

`options.json.owner_mark`:

```json
{"server":{"mig.source_id":"<source-uuid>","mig.state":"<ident>"},"port_description":"mig:<ident>"}
```

`server.create_destination` đặt hai property trên server. Port chỉ dùng `mig:<ident>` nếu không có description nguồn; description nguồn được ưu tiên. Volume giữ description `migrated from ...`; quyền sở hữu khi xóa volume chủ yếu dựa ID trong state.

`stateview` suy phase, không hỏi cloud:

| Điều kiện state VM sống | `phase` | Không được suy ra |
|---|---|---|
| Có `volume-map`, chưa t-stop, chưa ID server | `preparing` | Không chứng minh mọi object đích đã ghi đủ |
| Có t-stop, chưa ID server | `cutover` | Không chứng minh nguồn đã SHUTOFF |
| Có ID server | `migrated` | Không chứng minh server ACTIVE hoặc ứng dụng đạt |

`source_power` lấy từ export, không phải trạng thái nguồn hiện tại. Volume đơn không có phase VM. Không có `volume-map` thì view coi plan-only. Retire đổi tên thành `<ident>.rolled-back.<epoch>`, thêm `.n` nếu trùng. A6 retire không có `rollback.json`; rollback tường minh có file này.

### Khóa

| Khóa | Phạm vi | Cách dùng |
|---|---|---|
| `MIG_LOCK` | Mọi migrate/volume/rollback dùng cùng file trên host | `locking` lấy flock độc quyền không chờ; default `/run/mig/migrate.lock` |
| `MIG_LOCK_FD` | Cha selector/runner → child | FD được truyền qua `pass_fds`; kiểm inode/device đúng file khóa, không tự đặt biến này |
| Flock thư mục state | VM migrate, volume đơn và rollback | `StateDir.lock`; tránh hai process dùng một state |

Volume đơn lấy flock thư mục `vol-<id>` ngay sau khi tạo, rồi kiểm lại `volume-map` (`volcmd.cmd_migrate_volume`). Plan và prune không lấy khóa migration. Khóa chỉ có hiệu lực với process dùng cùng file trên cùng host; không bảo vệ nhiều convert host hoặc thao tác cloud thủ công. Runner lấy đường dẫn khóa từ probe nếu không override `--lock`.

### A6 và mã thoát

`rollback.clean_before_cutover` chỉ dùng cho VM có `volume-map`, không t-stop, không server ID: từ 1.1.1 hỏi nguồn trước (`require_source`, như A20; state không có id nguồn thì bỏ qua): not found hoặc không đọc được thì dừng (exit 3) với `this re-run deleted nothing; the earlier state stays live`, không xóa gì, state cũ vẫn sống; đọc được thì xóa volume đích đã ghi, dọn clone, retire state, export và chạy lại từ đầu. Không gọi stop/unlock/start nguồn trong nhánh này. Nếu dọn thất bại thì không tiếp tục. SG đã copy được giữ.

State có t-stop hoặc server ID chặn chạy lại. Lệnh `migrate migrate <vm>` đi qua selector: selector thấy state đó (`past_cutover`), ghi dòng `SKIP(state-exists)` kèm lệnh rollback, không chạy con, và thoát 1 vì không có gì được migrate. Exit 4 với `state exists in ...: run rollback first (that run stopped the source at ...)` chỉ xảy ra khi chính tiến trình con gặp state đó (`vmcmd.cmd_migrate`), tức state xuất hiện sau lúc selector liệt kê. Nội dung thông báo nói “stopped” nhưng mốc thực thi là marker trước stop.

| Mã hợp đồng (`contract.py`) | Phạm vi thực thi |
|---|---|
| 0 `EXIT_OK` | Thành công; selection rỗng cũng trả 0 |
| 1 `EXIT_FAIL` | Lỗi chung, preflight FAIL, all-SKIP (gồm `SKIP(state-exists)`), volume đơn/rollback lỗi |
| 2 `EXIT_USAGE` | Parser/usage; rollback non-TTY thiếu yes; lỗi `Die` option có thể vẫn 1 |
| 3 `EXIT_BEFORE_CUTOVER` | VM lỗi trong phần chuẩn bị được bọc sau preflight |
| 4 `EXIT_AFTER_CUTOVER` | VM lỗi sau marker; tiến trình con gặp state đã cutover |
| 5 `EXIT_BATCH_FAIL` | `Runner.exit_code`: nhiều hàng đã attempted và có FAIL; không đơn thuần số hàng chọn |
| 130 `EXIT_INTERRUPTED` | SIGINT/Ctrl-C được bắt |

Không có guarantee “mọi lỗi trước t-stop đều 3”: export/preflight/config có thể 1. Volume đơn chưa phân lớp 3/4. Kill cưỡng bức hoặc crash không có output kết quả đáng tin cậy.

## 5. Rollback và hậu điều kiện

`rollback.summary` đọc state và status server đích đã ghi để tạo dry-run. Không khám phá đầy đủ mọi orphan trước prompt. `summary_lines` in `Rollback of ...`, thời điểm/tuổi t-stop, đối tượng sẽ xóa và trạng thái điện dự kiến của nguồn. Nếu đích ACTIVE và tuổi marker > 3600 s thì có WARN mất ghi. CLI hỏi `Type <name> to roll back: `; nhận đúng tên hoặc ident. Sai thì `not confirmed, nothing changed`. Non-TTY thiếu `--yes` trả 2. `--dry-run --json` chỉ đọc, không lấy quyền xóa từ output đó.

Trình tự chính (`cli.run_rollback`, `rollback.rollback_vm`, `drop_recorded_volumes`, `drop_volume`):

1. Xác định scope rồi lấy khóa host và state; đọc state/export để xác định đối tượng. **Scope (P2-a, 1.1.1)**: `cli.run_rollback` (cả `--dry-run`) tìm state trên đĩa trước, không hỏi cloud (state VM `<target>`, `vol-<target>`, hoặc `<target>` có `volume.json`), đọc `options.json` → `effective.projects.value {src, dst}` rồi mới dựng `Run` (`cli.rollback_scope`): môi trường/cấu hình không đặt cả `SRC_PROJECT` lẫn `DST_PROJECT` → dùng giá trị của record, log `INFO scope SRC_PROJECT=<A> DST_PROJECT=<B> (from the state's options.json)`; đặt và bằng record → không log gì; đặt mà khác record (kể cả chỉ đặt một bên, hoặc record rỗng mà môi trường có) → dừng `scope mismatch: this state was written with ... this run has ...; nothing was done`, không gọi API nào; state không có record (bản cũ, bản bash) → giữ scope hiện hành, log `INFO scope: the state has no project record; ...`. Volume chỉ tìm được qua cloud (theo tên, `resolve_volume`) giữ scope hiện hành, log `INFO scope: <target> is looked up through the source cloud: ...`. Dưới khóa, cùng phép tìm phải ra cùng state, không thì dừng (`the state of <target> changed while the rollback started`). `check_project_scope` vẫn chạy sau đó (application credential + scope → dừng như cũ). Từ 1.2.0, `run_rollback` ghi nguồn của scope vào `cfg.scope_origin` (`record` khi scope lấy từ, hoặc bằng, record của state; `env`; `none`); scope `record` mà openrc là application credential thì dừng `this state was written by a run scoped with SRC_PROJECT=<A> DST_PROJECT=<B> (a password openrc); <rc> is an application credential, which cannot be rescoped: run the rollback with the password openrc that wrote the state; nothing was done` (trước mọi lệnh cloud); migrate/volume và scope `env` giữ câu `SRC_PROJECT/DST_PROJECT needs a password openrc ...`. Dry-run JSON có `projects {src, dst, origin: record|env|none}`. Runner UI không đổi luồng: engine tự khôi phục; `self.env` thường không đặt `SRC_PROJECT`/`DST_PROJECT`, nếu đặt khác record thì engine dừng mismatch. **A20**: trước mọi thao tác xóa ở đích, `rollback.require_source` hỏi nguồn `server show <sid> -f json -c id -c status` (volume đơn: `volume show <vid>`, `volcmd.rollback_volume`). Trả lời được (status nào cũng được) thì đi tiếp. Not found (`rollback.gone`: câu trả lời nêu đúng id, xem bước 1b) thì dừng với `is not found at the source (deleted, not visible to the source credentials/SRC_PROJECT, or the wrong source endpoint/region)`: 404 có thể là nguồn đã bị xóa, credentials/scope nguồn không thấy đối tượng hoặc sai endpoint/region, nên thông báo bảo kiểm scope và endpoint nguồn trước khi xóa state bằng tay; đích có thể là bản sao duy nhất còn lại, lần rollback này không xóa gì, state vẫn sống; `migrate prune` không bao giờ xóa state sống nên thông báo chỉ dẫn xóa thư mục state bằng tay khi chắc chắn giữ đích. Lỗi khác (auth, mạng, scope, endpoint thiếu trong catalog) cũng dừng với `cannot read source ...`, lần rollback này không xóa gì; exit 0 mà output không phải object có đúng id cũng tính là không đọc được. Rollback chạy lại sau khi lỗi giữa chừng vẫn qua khi nguồn vẫn đọc được. `clean_before_cutover` (A6) cũng hỏi nguồn từ 1.1.1 (mục 4, "A6 và mã thoát"). Summary/dry-run có `source_status` từ cùng lệnh show: GONE → dòng `ERROR the source ... is not found at the source: ... this rollback is refused`, null → dòng `WARN ... could not be read`, còn lại `, now <status>` ở dòng đầu.
1b. **A21 (1.1.1)**: một object đích (hoặc port nguồn) chỉ được coi là đã mất khi API nói không tìm thấy **đúng object đó**: `rollback.gone(stderr, oid)` đúng khi một dòng stderr chứa `oid` (không phân biệt hoa thường) và nói `No <Kind> found for <oid>` (openstackclient 10.x), `No <kind> with a name or ID of '<oid>' exists` (client cũ), `<oid> could not be found` (novaclient/cinderclient) hoặc kết thúc bằng `(HTTP 404)`/`404`; **R1 (1.2.0)**: hoặc có `(HTTP 404)` ở bất kỳ đâu trên dòng, hoặc token `404` đứng riêng (không phải một phần của ID, địa chỉ IP hay path URL như `/v2/404abc`) theo sau là `Not Found` hoặc hết dòng (`_HAS_404`; ví dụ `Not Found (HTTP 404) (Request-ID: req-…) …/v3/<project>/volumes/<id>`). Câu không nêu object (keystoneauth `public endpoint for compute service in RegionOne region not found` khi catalog thiếu service/region/interface, `openstack: command not found`, 404 của URL endpoint sai) **không** phải đã xóa. Ngay sau A20, `rollback.require_destination` đọc đích một lần: `server show <dst-server-id> -f json -c id -c status`, không có server ID thì `volume show` volume đích đầu tiên của `volume-map` (volume đơn cũng vậy); state không ghi cả hai thì không probe. Hợp lệ khi trả đúng object (status nào cũng được) hoặc not found nêu đúng id (đích đã bị xóa ở lần rollback trước: chạy lại được); mọi thứ khác dừng trước lệnh xóa đầu tiên: `cannot read destination server|volume <id> (<lý do>): nothing was deleted at the destination, the source was not touched; check the destination credentials/endpoint/DST_PROJECT and run the rollback again`. Summary/dry-run: `dst_server_status` từ cùng probe, `dst_error` là lý do khi không đọc được (`null` khi đọc được hoặc GONE), dòng `WARN the destination server could not be read (<lý do>): the rollback refuses until it can`; runner không trả 409 cho trường hợp này (engine từ chối lúc chạy), hộp xác nhận hiện `Đích hiện tại: <status>`.
2. Server đích có ID trong state được xóa theo ID. Nếu thiếu ID, tìm cùng tên (`server list --name ^<name>$ --all-projects`, 403 thì thử lại không cờ trong project của token, từ 1.1.1), thời gian tạo ≥ t-stop và property owner mark khớp. Server lạ không mark được giữ. State không có owner_mark mà gặp candidate không chứng minh được thì dừng và yêu cầu xử lý tay.
3. Kiểm rc `server delete --wait` (rc ≠ 0 chỉ được bỏ qua khi `gone()` nêu đúng server; còn lại dừng `delete failed (...): the source VM was not touched; check the destination credentials/endpoint/DST_PROJECT ...`), poll show tới khi not found nêu đúng id, giới hạn chờ 600 s. Show lỗi không nêu object là lỗi đọc: 5 lần liên tiếp (khoảng 15 s) thì dừng `cannot read <label> <id> after its delete (...)`, không chờ 600 s. Không xác nhận biến mất thì dừng trước khi khôi phục nguồn.
4. Xóa port đích đã ghi; quét tên port fallback chỉ khi đúng mark. **R3 (1.2.0)**: `port show` của port đã ghi (`drop_dest_port`) bắt stderr: trả port → xóa (`port delete` lỗi → `WARN cannot delete destination port <id> (<lý do>): delete it by hand`, rollback đi tiếp); not found nêu đúng port → `INFO destination port <id> already gone`; lỗi khác (quyền, endpoint) → `WARN cannot read destination port <id> (<lý do>): left, check it by hand`, đi tiếp (server đích đã xóa, nguồn đang lock: dừng ở đây chỉ kéo dài downtime). Quét tên fallback: không có tên đó thì im lặng như cũ, lỗi đọc/xóa khác là WARN. Xóa volume đã ghi, chờ available nếu còn in-use sau server delete, rồi poll tới khi not found nêu đúng id. `volume show` lỗi không nêu volume thì dừng `cannot read destination volume <id>: <...>; nothing more was deleted`. `error_deleting` hoặc quá hạn làm dừng, không retire.
5. Dọn image theo ID/tên snapshot và clone còn sót theo nguồn. R3 (1.2.0): `image list` đích lỗi → `WARN cannot list destination images named mig-snap-<vm>: skipped` (đối xứng với nguồn), image ghi trong `dst-image-id` vẫn bị xóa. Một số nhánh là best effort. Giữ SG và server group vì có thể được VM khác dùng.
6. Unlock nguồn; hợp nhất ID trong `source-ports` và export. `port show` lỗi mà không phải not found nêu đúng port (`gone()`, A21: lỗi endpoint của Neutron không phải port đã mất) được thử tối đa 60 lượt, cách 2 s rồi dừng `cannot read source port ...`. Gắn nhầm VM khác thì dừng, in lệnh sửa thủ công. Port được xác nhận không còn thì log và bỏ qua. Gắn lại port, poll `device_id`; thử trả floating IP, lỗi chỉ WARN.
7. Nguồn lúc export SHUTOFF thì kiểm tra vẫn SHUTOFF; còn lại start nếu cần và đợi ACTIVE. Retire state rồi ghi `rollback.json` vào thư mục đã đổi tên (`cli.run_rollback`, `record.write_rollback_json`).

**Hậu điều kiện được kiểm**: không có lệnh xóa đích nào trước câu trả lời hợp lệ đầu tiên của API đích trong lần chạy này (A21); server đã chọn để xóa và volume đã ghi phải trả not found nêu đúng id trước khi rollback hoàn tất; port có thể đọc được phải thuộc đúng nguồn khi được gắn lại; nguồn được trả trạng thái điện theo export. **Không có chứng minh toàn cục** rằng đích không còn orphan hoặc source network đầy đủ: discovery server/clone có nhánh bỏ qua lỗi listing, port đích hoặc listing image đích lỗi chỉ là WARN (R3, 1.2.0: không còn im lặng), image/FIP dọn best effort. IDs đã ghi được tin cậy, không kiểm lại property ownership trước từng delete. Vì vậy bảo vệ state root-only và freeze thao tác tay là bắt buộc.

Rollback không reverse-sync. Sau khi ứng dụng đích nhận ghi, quay về nguồn làm mất mọi ghi chỉ có ở đích. Tuổi marker chỉ ước lượng cửa sổ RPO. Phải cô lập writers và lấy dữ liệu cần giữ trước xác nhận. Nguồn có thể đã được bật bằng `--start-source`; lúc đó hai nhánh dữ liệu cần quyết định của chủ ứng dụng, không thể tự merge.

## 6. Fidelity và kiểm tra

### 6.1. Port và group

| Thuộc tính | Hành vi | Điểm truy vết |
|---|---|---|
| Network/subnet | Cùng tên network và CIDR, không tự tạo | `preflight`, `create_destination` |
| `--ip same` | Giữ fixed IP/MAC; detach nguồn trừ ngoại lệ | `detach_source_ports` |
| `--ip new` | Cấp IP/MAC mới; không detach; bỏ AAP có WARN | `port_fidelity_preflight` |
| IP chỉ định | Một NIC, trong subnet, MAC mới; giữ AAP phù hợp | `preflight`, `create_destination` |
| Nhiều fixed IP trên một port | FAIL | `preflight` |
| Port security tắt | Đích tắt, không SG; SG override xung đột bị từ chối | `port_security_conflict` |
| AAP | Copy; MAC chính của port đi theo MAC mới ở addr; MAC VRRP khác giữ | `server` tạo argv port |
| Description | Copy; nếu rỗng thêm `mig:<ident>` | `create_destination` |
| `dns_name` | Chỉ copy nếu có extension và bằng hostname đơn giản suy từ tên VM | `plain_hostname`, `port_fidelity_preflight` |
| Floating IP | Không chuyển giữa cloud; export để rollback thử gắn lại | `export_definition`, rollback |
| Port Nova-owned bị xóa khi detach | Tạo `mig-reserve-<ident>-<i>` giữ IP/MAC; ghi ID mới | `detach_source_ports` |
| Keypair | Có ở đích thì dùng; thiếu thì tạo VM không key_name, INFO/WARN | `create_destination` |
| User-data | Chỉ file bảo toàn SSH key/hostname, không sao chép user-data nguồn | `create_destination` |

SG mặc định mang tên `<AZ>-<source-name>`. Prefix lấy AZ hiệu lực hoặc compute AZ duy nhất dùng được ở đích. Nếu cần copy mà không xác định được prefix thì FAIL (`azname.resolve`, `need_prefix`). Prefix suy ra để đặt tên không tự ép scheduler AZ.

`secgroups.plan_copy` giải remote-group đệ quy, có chống vòng lặp. `ensure` tạo mọi group trước rồi tạo rules, xóa default egress tự sinh, copy stateful/stateless, description và fields rule. Description group có `mig-copy of <source-id>[: <source-description>]`. Remote address group không được hỗ trợ.

SG tồn tại được so rule (`rule_key`, `rule_warnings`): direction, ethertype, protocol, port range/ICMP type-code, remote CIDR/group đã map. Không dùng description hoặc stateful làm khóa so rule. Thiếu/thừa là WARN, không tự sửa. Bản mang `mig-copy of` thiếu rule thêm WARN `HALF-MADE COPY`; **không phải FAIL**. Dừng vận hành để kiểm và xóa/tạo lại chỉ khi không còn người dùng group đó.

Server group mặc định theo nguồn, tên cùng prefix, policy và rules. Trùng tên khác policy/rules hoặc nhiều group thì FAIL; explicit `--server-group` là quyết định override. Khi client từ chối `--rule`, code WARN rồi tạo không rules. Lỗi đọc rules đích có thể trở thành `{}` trong `dst_group_rules`, vì vậy rule rỗng không chứng minh API đã đọc thành công. Rollback giữ group.

### 6.2. Catalog dòng preflight

Mỗi dòng có tiền tố chính xác `  OK    `, `  FAIL  `, `  WARN  `, `  INFO  ` (`contract.py`). Bảng ghi mẫu text tiếng Anh; `<...>` là trường thay thế, `…` là phần dữ liệu/lý do động. `OK/FAIL` nghĩa cùng label được `Checker.check` in với một trong hai mức. Các nhánh không áp dụng không in dòng. `Destination preflight (...)` và `Preflight passed.` là header/kết luận.

| Dòng hoặc họ dòng | Mức | Ý nghĩa / hàm |
|---|---|---|
| `compute AZ <zone>`; `volume AZ <zone>` | OK/FAIL | AZ có trong listing (`preflight`) |
| `cannot list the destination nova-compute hosts for --dst-host ...` | FAIL | Không đọc được host (`dst_host_preflight`) |
| `destination host ... is not a nova-compute host` | FAIL | Host không tồn tại |
| `destination host ... is down or disabled ...` | FAIL | Host không hoạt động |
| `destination host ... is in zone ..., not ...` | FAIL | Sai AZ |
| `availability zone ... from host ...` | INFO | Suy AZ từ host |
| `--dst-host ... is in zone ..., which AZ_TYPE_MAP maps to volume type ...` | FAIL | Cần AZ/type rõ để chọn pool đúng |
| `destination host ... (zone ..., up, enabled)` | OK | Host hợp lệ |
| `--dst-host needs the admin role at the destination ...` | FAIL | Không admin hoặc không đọc được role |
| `--dst-host ... ignored: --volumes-only creates no server` | INFO | Host không áp dụng |
| `--server-group ... ignored: --volumes-only creates no server` | INFO | Group không áp dụng |
| `--flavor-fallback ignored: --volumes-only creates no server` | INFO | Flavor không áp dụng |
| `volume name ... free`; `cannot list destination volumes named ...` | OK/FAIL | Chống trùng volume-only |
| `destination already has a volume named ...` | FAIL | Có volume ngoài state cùng tên |
| `destination already has a server named ...` | FAIL | Có server cùng tên ngoài ID state |
| `flavor <name>` | OK/FAIL | Flavor đích tồn tại |
| `--flavor-fallback has no effect: --flavor ... is given` | INFO | Override thắng map |
| `flavor ... — not at the destination and not in ...` | FAIL | Thiếu fallback mapping |
| `flavor ... -> ... from FLAVOR_MAP_FILE` | INFO | Chọn fallback |
| `flavor ... (mapped from ...) not found at the destination ...` | FAIL | Không tạo flavor tự động |
| `flavor sizes not compared: ... unreadable` | INFO | Không đủ dữ liệu so size |
| `mapped flavor ... is smaller than ...` | WARN | vCPU/RAM nhỏ hơn nguồn |
| `keypair <name>`; `keypair ... not at destination: ...` | OK/INFO | Thiếu keypair không chặn |
| `server group <name>`; `server group ... policy: ...` | OK/INFO | Explicit group được chọn |
| `server group ... not found in the destination project ...` | FAIL | Cần group có sẵn |
| `... server groups are named ... in the destination project ...` | FAIL | Tên mơ hồ |
| `cannot list source server groups`; `cannot read source server group ...` | FAIL | Không xác định group nguồn |
| `source VM is in ... server groups ...` | FAIL | Cần override khi nhiều group |
| `source server group ... -> destination ...` | INFO | Policy/rules nguồn và tên copy |
| `cannot read the policy of source server group ...` | FAIL | Không đọc được policy |
| `cannot list destination server groups` | FAIL | Không đọc được đích |
| `will create ... (policy ...)`; `server group ... exists (...): reused` | OK | Tạo mới hoặc tái dùng |
| `server group ... has policy ...`; `server group ... exists at the destination ... with rules ...` | FAIL | Khác policy/rules |
| `copies of the source server group and security groups are named ...` | INFO | Giải thích prefix |
| `cannot name the destination copies of the source server group and security groups: ...` | FAIL | AZ không xác định (`need_prefix`) |
| `cannot list the destination compute availability zones to name the copies of the source server group and security groups — pass --az <zone> or set MIG_AZ (opt out: --no-server-group, --no-copy-secgroup)` | FAIL | Không đọc được AZ để đặt tên copy (`azname.resolve`) |
| `volume type <type>` | OK/FAIL | Type của lần chạy có sẵn (`VOLUME_TYPE_MAP` rỗng, như cũ) |
| `volume type <type> (<from>)`; `destination volume type <type> (<from>) does not exist` | OK/FAIL | 1.2.0, `VOLUME_TYPE_MAP` có đặt: một lần cho mỗi type đích khác nhau của VM (`dest_type_checks`); `<from>` như dòng INFO từng volume |
| `VOLUME_TYPE_MAP in use: <n> volume(s) mapped` | INFO | 1.2.0, sau header: số volume (kể cả root ephemeral) nhận type từ map (`type_map_lines`); không in khi không volume nào qua map |
| `volume type <type> from cli overrides VOLUME_TYPE_MAP` | INFO | 1.2.0, một lần: `--volume-type` gõ tay khi map có đặt |
| `override security group ...`; `security group ... (<id>)` | OK/FAIL | SG có đúng một bản trong project |
| `cannot plan the security group copies: ...` | FAIL | Lỗi graph SG (`plan_copy`) |
| `security group ... -> will create ...`; `security group ... -> found ...` | OK | Kế hoạch SG (`describe`) |
| `security group ... -> ... (see above)` | OK | SG chung nhiều port đã kiểm |
| `security group ... -> ...: ...` | FAIL | SG mơ hồ/remote address group/lỗi truy vấn |
| `security group ... -> found ...: missing ...`; `security group ... -> found ...: extra ...` | WARN | So rule (`rule_warnings`) |
| `HALF-MADE COPY (an earlier run was killed while creating it): security group ...` | WARN | Copy mang mark nhưng thiếu rule |
| `network ... exists`; `subnet ... on ...` | OK/FAIL | Network và CIDR khớp |
| `MAC ... free`; `IP ... free` | OK/FAIL | Listing không có conflict; lỗi listing có thể bị coi rỗng |
| `IP ... is not in subnet ... of ...` | FAIL | IP chỉ định ngoài CIDR |
| `--ip <addr> works only for single-NIC VMs ...` | FAIL | Chỉ định IP cần một port |
| `port ... has several fixed IPs (not handled)` | FAIL | Không hỗ trợ nhiều fixed IP |
| `cannot list the destination network extensions ...` | FAIL | Không kiểm được port security |
| `port ... has port security disabled at the source, the destination has no port-security extension ...` | FAIL | Đích thiếu extension |
| `port ...: port security disabled at the source: ...` | INFO | Copy không SG, tắt security |
| `port ... has allowed-address-pairs, the destination has no allowed-address-pairs extension` | FAIL | Không thể copy AAP |
| `port ...: allowed-address-pairs copied: ...` | INFO | AAP được giữ |
| `port ...: allowed-address-pairs not copied with --ip new ...` | WARN | Tự cấu hình AAP sau kiểm |
| `port ...: dns_name ... copied` | INFO | DNS đủ điều kiện |
| `port ...: dns_name ... not copied: ...` | WARN | Thiếu extension/hostname không khớp |
| `port ...: floating IP ... not migrated ...` | WARN | Cần FIP/DNS tại đích |
| `--ip ... is the source VM's own address ...`; `--ip ... is an allowed-address-pair ...` | FAIL | Start-source gây trùng IP |
| `port ... has allowed-address-pairs ... and --start-source keeps the source running: ...` | WARN | Hai bản có thể cùng nhận VIP |
| `no encrypted volume`; `no multiattach volume` | OK/FAIL | VM không dùng volume loại cấm |
| `volume <id> (<name>) source type <t_src> -> destination type <t_dst> pool <pool>[ (<from>)]` | INFO | Type/pool đích của từng volume (`volume_type_lines`, P2-b, 1.1.1). 1.2.0: mỗi volume theo `Options.type_for`; khi `VOLUME_TYPE_MAP` có đặt thêm `(<from>)`: `VOLUME_TYPE_MAP (<t_src>)`, `VOLUME_TYPE_MAP (__DEFAULT__)`, `cli`, `AZ_TYPE_MAP (<AZ>)`, `MIG_DEFAULT_OPTS`, `default`; volume đơn cũng in khi map có đặt |
| `source type <t_src> has no entry in VOLUME_TYPE_MAP and no __DEFAULT__: lands on <type>` | WARN | 1.2.0: type nguồn (hoặc `unknown`) map không phủ, volume về type của lần chạy; một dòng mỗi type nguồn |
| `root disk volume -> destination type <t> pool <p> (<from>)` | INFO | 1.2.0, map có đặt: type/pool của root ephemeral thành volume |
| `<n> source volume types (<t1>, <t2>) all land on destination type <t_dst>: pass --volume-type per run if they must differ` | WARN | VM có ≥ 2 type nguồn khác nhau bị san về một type; không đổi exit code. Chỉ khi `VOLUME_TYPE_MAP` rỗng (1.2.0) |
| `source VM is locked with reason ...` | FAIL | Không tự tháo human lock (`lock_preflight`) |
| `flavor ...: ephemeral and swap disk sizes not readable: ...` | WARN | Không chứng minh thiếu extra disk |
| `flavor ... has ...: the data on a flavor's ephemeral or swap disk is not migrated ...` | FAIL | Extra disk/swap không hỗ trợ |
| `root disk is ephemeral (image-backed): migrated through a Glance snapshot` | INFO | Đường snapshot |
| `--mode live needs boot-from-volume ...` | FAIL | Root ephemeral chỉ cold |
| `free space not checked: ...` | WARN | Thiếu size hoặc không đọc được disk usage |
| `free space in ...` | OK/FAIL | Staging đủ theo flavor disk |
| `OS_CACERT ... cannot be loaded: ...` | FAIL | CA cho REST không dùng được |
| `image endpoint ... (...)` | OK | Tìm endpoint theo interface |
| `the destination VM boots from a volume made from that snapshot ...` | INFO | Root BFV mặc định |
| `cold copy of ... GB (more than MIG_COLD_WARN_GB=...) ...` | WARN | Copy lớn, nên cân nhắc live |
| `source quota not checked: ...`; `destination quota not checked: ...` | WARN | Không có usage table |
| `<side> quota: usage of ... not readable ...` | WARN | Không coi usage thiếu là 0 |
| `<side> quota: ... need ..., ... free ...` | FAIL | Thiếu quota resource |
| `<side> quota: ...`; `<side> quota: no limit set on what this run uses` | OK | Nhu cầu riêng lần chạy phù hợp dữ liệu đọc được |
| `subnet ...: no free address in its allocation pools for --ip new` | FAIL | Không đủ IP cấp mới |
| `free addresses for --ip new: ...` | OK | IP pool hiện đủ |
| `Ceph pool capacity not checked: ...` | INFO | Không đọc được `ceph df detail`/max_avail |
| `Ceph pool ...: ... GB needed, ... GB left ...` | OK/FAIL | Logical GB cần so với MAX AVAIL/quota; 1.2.0: một dòng mỗi pool, cộng size các volume về pool đó (một `ceph df` cho tất cả) |
| `Ceph pool ...: ... under 10% of the pool free after the copy` | WARN | Headroom nhỏ |
| `RBD access to pool <pool>` | OK/FAIL | `rbd ls`, chỉ chứng minh đọc/list; 1.2.0: map có đặt thì một dòng mỗi pool các volume của VM về |
| `iSCSI daemon on transfer host`; `multipathd on transfer host` | OK/FAIL | Daemon hoạt động |
| `start it: systemctl enable --now iscsid` | INFO | Tool không tự start daemon |
| `blocksync present`; `librbd python bindings` | OK/FAIL | Live có helper/bindings (`livesync.preflight_checks`) |
| `... is missing from the migratetool package: reinstall it (pip install .)` | INFO | Helper live không đọc được |
| `blocksync runs under SYS_PY=..., which cannot import rados/rbd: ...` | INFO | Hướng dẫn sửa bindings live |
| `volume not encrypted`; `volume not multiattach` | OK/FAIL | Volume đơn; multiattach chỉ nhánh in-use |
| `--in-use ignored: ... is available, copied as usual` | INFO | Volume available không cần cờ |
| `... is in-use: copied from a clone taken while attached (crash-consistent); its VM keeps running` | INFO | RPO volume nóng |
| `cannot list destination volumes`; `destination already has a copy of ...` | FAIL | Lookup SDK thất bại hoặc đã có copy |
| `no earlier copy of ... at the destination` | OK | Không tìm thấy description trùng |

| Check/message bổ sung | Nghĩa |
|---|---|
| `INFO  availability zone <az> from <origin>` | Nguồn AZ do resolve option |
| `INFO  volume type <type> from <origin>` | Nguồn type do cấu hình/map |
| `INFO  MIG_DEFAULT_OPTS applied before the command line: <options>` | Các mặc định áp trước argv |
| `INFO  --in-use in MIG_DEFAULT_OPTS ignored: it is only taken from the command line` | Không bật in-use bằng mặc định |
| `INFO  MIG_DEFAULT_OPTS options that do not apply to migrate-volume, ignored: <options>` | Mặc định không áp dụng cho volume rời |
| `the destination catalog lists no <interface> image endpoint: set OS_INTERFACE in <rc> to an interface this host reaches (public, internal or admin)` | Thiếu endpoint Glance cho root ephemeral (`ephemeral.no_endpoint_message`) |

Các dòng `INFO` nguồn option do `Options.conf_origin_info`/`default_opts_info` sinh mô tả MIG_AZ, MIG_VOLUME_AZ, AZ_TYPE_MAP và MIG_DEFAULT_OPTS đã áp dụng. Lỗi resolve/source export, xung đột port-security override, map sai hoặc thiếu image endpoint có thể là `ERROR:` thay vì check line; vẫn chặn plan. Không parse phần tiếng Anh động thay cho `checks` JSON.

Quota gồm instances/cores/ram, volumes/gigabytes theo type, ports, SG/rules và members của server group khi cần. Không có reservation cho toàn batch; số group Nova mới không được cộng thành resource riêng trong `quota_needs`. Nguồn cần chỗ cho một clone tại một thời điểm, với volume lớn nhất. Mục thiếu trong quota hoặc limit -1 bị bỏ; missing usage WARN. `free_address_preflight` có nhánh không kiểm khi lookup lỗi. Quota Cinder có cộng root BFV theo flavor disk, nhưng không biết virtual size lớn hơn cho tới download. Ceph capacity chỉ cộng Cinder volume nguồn, không cộng root ephemeral. Phải bổ sung các phần này khi duyệt.

### 6.3. Catalog Doctor

Doctor tiếp tục các nhóm kiểm sau FAIL. `Doctor found problems: fix the FAIL lines above.` trả 1; `Doctor passed.` trả 0. WARN/INFO không làm fail (`doctor.run_doctor`).

| Text dòng (giữ tiếng Anh) | Mức | Ý nghĩa / hàm |
|---|---|---|
| `conf <path>` | OK | Parse thành công (`check_conf`) |
| `conf <path>: ...` | FAIL | Parse lỗi; các kiểm sau dùng env/default |
| `conf <path> not found: defaults and environment only` | INFO | Chưa có conf |
| `<program>: <version>` | OK | openstack/qemu-img/rbd/iscsiadm, thêm multipath nếu bật (`check_programs`) |
| `<program>: not found on PATH` | FAIL | Thiếu executable |
| `<program> <flag> exited <rc>: ...` | FAIL | Không chạy được version probe |
| `brick: os-brick, cinderclient, keystoneauth1, oslo import` | OK | Import venv helper (`check_pythons`) |
| `brick cannot start (...): install os-brick and python-cinderclient into this venv ...` | FAIL | Thiếu module/runtime |
| `librbd python bindings for SYS_PY=...` | OK | Python hệ thống import được rados/rbd |
| `SYS_PY=... cannot import rados, rbd ...` | FAIL | Binding không sẵn |
| `blocksync present (...)` | OK/FAIL | File helper đọc được |
| `<side> openrc <path> is not readable` | FAIL | Không đọc được rc (`check_side`) |
| `<side> openrc: ...` | FAIL | Source rc lỗi; nếu file có dòng `read` thì thông báo nêu số dòng đó (`clouds.prompt_hint`) |
| `<side> openrc ...: OS_AUTH_TYPE=... (brick knows password and v3applicationcredential)` | FAIL | Auth không hỗ trợ |
| `<side> token: ...` | FAIL | Keystone không cấp token |
| `<side> token (project ...)` | OK | Token đúng scope hiện dùng |
| `<side>: application credential: bound to its own project (SRC_PROJECT/DST_PROJECT cannot rescope it)` | INFO | Không đổi project |
| `<side>: roles not checked: ...` | INFO | Không kiểm được role |
| `<side>: no admin role: --ip same needs port --mac-address (admin_or_network_owner)` | WARN | Chưa chứng minh quyền Neutron |
| `<side>: admin role` | OK | Role có admin |
| `<side> openrc ...: ... (it holds a password: chown root, chmod 600)` | WARN | Không root-owned 0600 (`check_openrcs`) |
| `SRC_RC and DST_RC point at the same Keystone ...: migrate moves VMs between two clouds` | FAIL | Hai URL trùng |
| `brick authentication not checked: ...` | INFO | Rc lỗi, không probe auth (`check_brick_auth`) |
| `brick: Keystone session of the source openrc works (TLS, interface, region as openstackclient)` | OK | Session brick cấp được token |
| `brick cannot authenticate with the source openrc: ...` | FAIL | Lỗi session/auth brick |
| `running as root (brick needs no sudo)` | OK | EUID 0 (`check_privileges`) |
| `not root: 'sudo -n true' works (brick attaches through sudo)` | OK/FAIL | sudo không hỏi mật khẩu |
| `destination Nova API ... >= 2.74 (--dst-host usable)` | OK | API đủ mới (`check_nova_api`) |
| `destination Nova API ... < 2.74: --dst-host not available` | INFO | Không dùng dst-host |
| `destination Nova API version not checked (...): --dst-host needs 2.74` | INFO | Không xác định version |
| `RBD access to pool ... as ...` | OK/FAIL | List mỗi pool default/map (`check_storage`) |
| `iSCSI daemon on transfer host`; `multipathd on transfer host` | OK/FAIL | Daemon đang chạy; multipath chỉ khi bật |
| `MIG_ISCSI_IP <ip> is an address of this host` | OK | Địa chỉ connector gửi cho Cinder là của host này (`check_iscsi_ip`, so với `ip -o addr show`) |
| `MIG_ISCSI_IP is not set: ...`; `MIG_ISCSI_IP <ip> is not an address of this host (...)` | FAIL | Không có mặc định; giá trị chép từ host khác bị bắt |
| `MIG_ISCSI_IP <ip> not compared with this host's addresses: ...` | INFO | Không chạy được `ip` |
| `start it: systemctl enable --now iscsid` | INFO | Hướng dẫn bật daemon |
| `RBD write check pool ... as ...: 4 MiB written, read back, discarded, removed` | OK | Đường thử không đặt lý do fail (`write_check_pools`) |
| `RBD write check pool ... as ...: write (rbd import): ...` | FAIL | Ghi thử lỗi |
| `RBD write check pool ... as ...: read (rbd export): ...` | FAIL | Đọc lại lỗi |
| `RBD write check pool ... as ...: read back differs from what was written` | FAIL | Hash khác |
| `RBD write check pool ... as ...: discard did not read back as zeros` | FAIL | Discard readback khác zero |
| `RBD write check pool ...: discard not checked (SYS_PY ...: ...)` | INFO | Helper discard lỗi khác rc 3; không làm Doctor fail |
| `state directory ... writable`; `log directory ... writable` | OK/FAIL | Tạo thư mục nếu thiếu rồi kiểm W_OK (`check_dirs`) |

`--write-check` tạo `mig-doctor-<epoch>` trong từng pool, import 4 MiB random, export so SHA256, discard/readback toàn 4 MiB bằng SYS_PY, rồi gọi rm trong finally. Code không kiểm rc rm. Helper discard lỗi khác 3 chỉ INFO rồi vẫn có thể in dòng OK. Vì vậy đọc mọi INFO và kiểm image thử đã mất; không coi dòng “discarded, removed” là chứng cứ độc lập cho hai hậu điều kiện đó.

### 6.4. Deadline

`proc.deadline` áp `MIG_CMD_TIMEOUT` (600 s, 0 tắt) cho từng openstack/rbd/ceph/systemctl. Timeout giết subprocess và trả rc 124 với `timed out after ... s`. Không áp cho qemu-img, blocksync, brick hoặc `openstack image save`. Poll nhiều lần có thể mất lâu hơn một deadline.

REST Glance dùng timeout 60 s; upload dùng socket timeout 600 s, không phải deadline tổng 600 s cho cả image (`ephemeral.REST_TIMEOUT`, `UPLOAD_STALL`). Runner còn có timeout bao ngoài: doctor 300 s, inventory/list 600 s, plan 900 s, status 120 s. Migrate/volume/rollback job không có timeout tổng. Phải điều tra cloud sau timeout vì API có thể đã nhận thao tác.

## 7. Hợp đồng machine-readable

`jsonout.emit_json` là cửa xuất structured JSON CLI. `--json` chuyển text sang stderr rồi emit một object stdout. Lỗi trước emit có thể không có JSON. Key giống credential bị từ chối theo regex `(?i)(OS_|_RC$|PASSWORD|SECRET|TOKEN|AUTH_URL|CACERT|KEY$|^AQ)`. Value bằng secret đã biết, hoặc chứa secret dài ≥ 8 ký tự, bị từ chối trước khi in. Progress dùng chế độ drop+WARN thay vì làm chết migration. Tên network/project luôn là value, không làm JSON key.

### 7.1. Shape các lệnh

`?` trong bảng nghĩa có thể null hoặc vắng theo nhánh; không phải ký tự thật trong JSON. Kiểu `checks` luôn là danh sách `{level,text}`, counts dùng `ok`, `warn`, `fail`, `info`.

| Lệnh | Object stdout |
|---|---|
| plan / plan volume | `{tool_version, verb, log_dir, rows:[...], totals:{...}, rc}` (`Runner.plan_report`) |
| list | `{tool_version, kind, rows:[...], totals:{...}}` (`listing.run_list`) |
| inventory | `{tool_version, source:{...}, destination:{...}, conf:{...}}` (`inventory`) |
| status | `{tool_version,state_root,log_root,live:[],plan_only:[],rolled_back:[],batches:[],running:[],skipped,rolled_back_total,batches_total,error?}` (`status`) |
| doctor | `{tool_version,checks:[],passed:boolean,counts:{ok,warn,fail,info}}` |
| rollback --dry-run | `{ident,kind,dir,name,source_id,t_stop,age_seconds,dst_server_id,dst_server_status,source_power,source_status,source_error,dst_error,projects:{src,dst,origin},delete:{server,ports:[],volumes:[],images:[]}}`; `dst_server_status`/`dst_error` (A21, 1.1.1): status server đích từ cùng probe rollback làm trước lần xóa đầu, `"GONE"` chỉ khi Nova nói không tìm thấy đúng server đó, `null` khi không đọc được (`dst_error` là lý do, `null` khi đọc được hoặc GONE); `projects` (P2-a): scope rollback dùng, `origin` `record` (từ `options.json` của state), `env` (môi trường hiện hành), `none` (không đặt); `source_status` (A20): status Nova/Cinder hiện tại của nguồn, `"GONE"` khi not found (đã xóa, hoặc credentials/scope nguồn không thấy), `"UNKNOWN"` khi show trả đúng id nhưng không có status, `null` khi không đọc được, khi exit 0 mà output không phải object có đúng id (`unexpected answer from <kind> show`), hoặc khi state không có id nguồn (`no source id in state`); `source_error` là lý do khi `null`; không có tool_version |
| prune | `{older_than_days,dry_run,removed:[{kind,path,time,bytes}],bytes,errors,state_root,log_root}` |

Plan row: `kind`, `id`, `name`, `ident`, `status`, `project:{src,dst}`, `result`, `info`, `env`, `command`, `effective`, `checks`, `counts`, `log`, `volumes_total`, `gb_total`, `nics`, `source_power`, `impact`. `env` chỉ chứa scope project. Hàng SKIP/READY có nhiều trường null. `command` là argv; `effective` là object với từng khóa `{value,from,detail?}` (`Options.effective`, `record.vm_command`).

Khóa effective: `mode`, `ip`, `volume_type`, `pool`, `volume_types`, `az`, `volume_az`, `dst_host`, `verify`, `in_use`, `max_passes`, `delta_limit_mb`, `start_source`, `keep_ports`, `flavor_fallback`, `no_server_group`, `no_copy_sg`, `volumes_only`, `ephemeral_as_image`, `projects`, `flavor`, `security_groups`, `server_group`. Nguồn `from` là `cli`, `default`, `MIG_DEFAULT_OPTS`, `conf`, `az` hoặc `source` tùy trường. `volume_type`/`pool` là type/pool của lần chạy. `volume_types` (1.2.0, có khi state đã có export: `options.json`, plan `--json`): `value` là `{<id volume nguồn>|"ephemeral-root": {"type", "pool", "from"}}` (type/pool thật của từng volume đích, `from` như dòng INFO preflight); `from` `conf` + `detail` `VOLUME_TYPE_MAP` khi có volume qua map, không thì như `volume_type`. Conf whitelist có thêm `VOLUME_TYPE_MAP`.

`impact` VM: `{stop_source,detach_ports,source_after,creates:{server,ports,volumes,security_groups,server_group}}`. `source_after` là `running` hoặc `SHUTOFF+locked`. `volumes_total`/`gb_total` đếm Cinder volume nguồn; root ephemeral mới chỉ được cộng vào `impact.creates.volumes`. Không dùng tổng GB UI để cấp quota root mới (`vmcmd.plan_facts`, `runner.batch_impact`).

List VM row: `kind,id,name,ident,status,result,project,project_id,host,az,flavor,image,boot,networks,volumes,locked,state`. `boot` là `bfv`/`ephemeral`; `networks` là `[{network,ips:[]}]`; `volumes` là `[{id,name,size,device}]`. List volume row: `kind,id,name,status,result,size,type,az,bootable,attached_to,project,project_id,state`; `attached_to` null hoặc `{server_id,server_name,device}`. Không giả định row volume có `ident` (`Lister.vm_row`, `vol_row`).

Inventory mỗi cloud có `auth_type`, `scope_project`, `projects:[{id,name}]`, `hosts` với host/zone/state/status. Đích thêm `zones:{compute:[],volume:[]}`, `flavors:[{name,vcpus,ram,disk}]` và `volume_types:[]` (tên type); lỗi phần nào có trường `*_error`. `--count` bổ sung counts nguồn, giới hạn 64 host. `conf` có `path`, `found` và các khóa whitelist an toàn (`record.CONF_KEYS`): MIG_DEFAULT_OPTS, MIG_AZ, MIG_VOLUME_AZ, AZ_TYPE_MAP, RBD_POOL_MAP, RBD_POOL, PROJECT_MAP_FILE, FLAVOR_MAP_FILE, SRC_PROJECT, DST_PROJECT.

Status live/plan-only entry có `ident,dir,kind,files,live,mtime,options,t_stop,dst_server_id,volume_map,phase,source_power,error`. Retired entry có ident/dir/retired/options/rollback. Process có pid/cmdline. Không follow symlink ra ngoài roots; file đọc JSON giới hạn 1 MiB. Status không hỏi cloud nhưng đường scrub có thể source rc để lấy giá trị secret. View chịu được một số lỗi đọc bằng giá trị trống; không thay `StateDir` trong điều tra lỗi.

### 7.2. Event tiến độ

Mỗi dòng có prefix **`@@ `** rồi JSON. Đây không phải NDJSON toàn stdout. `progress.emit` giới hạn copy/verify/blocksync cùng loại khoảng 1/s; các event khác không bị throttle này. Event kết thúc được force. Trường không có dữ liệu có thể vắng.

| `event` | Trường | Nghĩa |
|---|---|---|
| `phase` | `name`, `volume?` | presync/cutover/copy/verify/create/done |
| `copy`, `verify` | `volume,pct,mbps?` | Tiến độ qemu-img |
| `blocksync` | `volume,pct,changed_mib,pass` | Scan/delta; pass số hoặc `final` |
| `pass` | `volume,n,chunks,mb,secs` | Thống kê pass; chunks là số chunk đổi |
| `t_stop` | `at` | Bắt đầu marker downtime |
| `downtime` | `total,copy,verify,control_plane,glance?` | Giây đo bởi engine |
| `result` | `rc,dst_server_id?,vm?` | Kết quả nếu process đến được chỗ emit |

Blocksync nội bộ xuất một JSON thống kê `changed,chunks,written_bytes,zero_chunks,seconds,first`; stderr có `[blocksync] ...`. `contract.py` tập trung prefix preflight, regex check/log, chuỗi state/ephemeral/keypair, exit và owner mark. Runner giữ bản constants tương ứng, không import engine. Sửa producer phải kiểm consumer bằng test.

## 8. UI: giao thức và bảo mật

### 8.1. Web, runner và job

`migui-web` chỉ bind IPv4 loopback `127.0.0.1`, mặc định 8787. Truy cập qua SSH tunnel; Host/Origin phải đúng hostname loopback và port cho phép. Không có chế độ bind public/TLS trong app. `migui-runner` chạy root; socket `/run/migui/runner.sock` 0660 root:migui và `SO_PEERCRED` kiểm UID peer được cấu hình.

Socket nhận một JSON line, tối đa 64 KiB. Không có shell command tùy ý. Response là JSON line data/stderr hoặc stream event, kết thúc `{"end":<rc>}`; lỗi có `error`/`status`. Web chuyển thành HTTP JSON/SSE (`runner.Handler.handle`, web handler).

| `op` | Dữ liệu chính / kết quả | Kiểm runner |
|---|---|---|
| `doctor` | `write_check` → doctor JSON | Boolean |
| `inventory` | Inventory + conf/features | CLI probe và dữ liệu cloud |
| `list` | `volumes,hosts,projects,scope` → rows | `volumes` chọn danh sách volume; giá trị phải thuộc inventory; tối đa 128 mỗi list phía backend |
| `plan` | `session,user,kind,items,options` → planning/line/row/batch | Items biết từ list, tối đa 200; enum/range/name/UUID |
| `migrate` | `batch,confirm_token,typed,session,user,continue_on_error` | Batch VM đã plan |
| `volume` | Cùng cơ chế batch volume | Không nhận arbitrary argv |
| `rollback` | Chuẩn bị theo ident; hoặc chạy batch đã xác nhận | State sống, dry-run summary, cùng confirm machinery |
| `cancel` | Job hoặc batch; `force,typed` khi kill | Process đang chạy và grace 120 s |
| `status` | Job query/detail/log/stream hoặc CLI status | Job ID 16 hex; paging/filter validate |

Mọi option được kiểm ở cả web/common và runner. Tên/ID UI có whitelist; host/project/AZ/flavor/type phải ở inventory. SG list tối đa 16; live bounds UI/backend 1–50 passes, delta 0–65536. UI và backend cùng giới hạn 128 host/project cho một lần list. Không gửi đường dẫn `--vm-file` từ browser.

Plan VM chạy tối đa `--plan-jobs` (default 4). SSE có `planning`, `line`, `row`, `batch`, `end`, `error`; line có `ident,idents,text,level,t`. Mỗi ident tối đa 200 line, 500 ký tự/line, rồi tóm tắt phần còn lại. Kênh effective nội bộ `@@` không đưa thành log plan. Browser mất kết nối không giết plan; phải plan lại nếu mất token/batch trong UI. Job SSE dùng event ID để nối lại (`Last-Event-ID`).

Runner giữ batch trong RAM, job record ở `/var/lib/migui/jobs/<id>.json`. Job ghi command/effective/conf/projects/session/operator, timestamps, rc, pid/pgid, log, batch_log, state_dir, plan/checks, request_options, original, runner_version, volumes_total, phase_detail, exit_class, notify_rc. Job kết thúc được chmod 0400; root vẫn sửa được. `.out` là raw stdout root-only, không bị giới hạn; `.log` scrub cho UI giới hạn 50 MiB, buffer RAM 8 MiB.

Job đi từ QUEUED sang RUNNING rồi trạng thái kết thúc. Batch mặc định dừng ở FAIL; dừng sau con hiện tại không hủy con đang chạy. SIGINT gửi process group; sau 120 s mới cho SIGKILL có typed confirmation. Không auto rollback. Runner restart để child tiếp tục vì `KillMode=process` và output file; record đang chạy thành DETACHED nếu PID còn, LOST nếu không. Không tự reattach log hoặc resume queue (`load_records`).

### 8.2. Xác nhận và Doctor gate

`new_batch` phát token 16 random bytes dạng 32 hex, TTL 900 s. Token gắn session/operator và argv đã đóng băng trong RAM. `fingerprint` là SHA256 dữ liệu canonical kind/items/options; browser giữ fingerprint cục bộ để vô hiệu preview khi đổi lựa chọn. Run không nhận argv mới từ browser.

`conf_digest` hash tối đa 1 MiB đầu của conf và hai file map theo đường dẫn runner probe biết; tính khi hoàn tất plan, so lại trước run. Không hash openrc hoặc toàn env; thay file giữa lúc plan chưa được bảo vệ như snapshot nguyên tử. Phải freeze cấu hình. Sai typed hoặc conf thay đổi trả 409 và buộc plan lại. Bận khóa trả 423. Token không phải quyền dùng cho session khác (`op_run`).

`common.confirm_rule`/`typed_matches`: một đối tượng thì theo tên; batch có keep_ports, IP chỉ định, start_source hoặc in_use trong **effective** thì theo tên từng dòng; còn lại theo số lượng. `expect` giữ nguyên tên kể cả khoảng trắng/dấu phẩy. `ids` cho phép thay tên bằng ident đầy đủ, mỗi đối tượng đúng một lần, thứ tự bất kỳ. Rollback luôn theo tên với ident thay thế.

UI `confirmationMatches` so `expect`, `alt` và `ids` như server: tên hoặc ident đầy đủ, mỗi đối tượng một lần. Rollback có `alt` nên nhập ident được. Xác nhận WARN bằng checkbox là guard trình duyệt; server không nhận từng checkbox. Các giới hạn này phải được giữ trong manual, không coi UI guard là policy backend.

Doctor gate D3 chỉ nằm ở web cho inventory, list, plan và chạy batch VM/volume. Authenticated rollback/cancel/status/jobs/stream vẫn mở khi Doctor chưa xanh. Gate là trạng thái phiên, không thay preflight engine, không chứng minh write-check đã chạy. Login mới/restart web phải chạy Doctor lại; reload trang có session còn sống không tự tạo phiên mới.

### 8.3. Mô hình đe dọa T1–T17

| ID / nguy cơ | Biện pháp thực tế | Giới hạn và điểm truy vết |
|---|---|---|
| T1 Truy cập từ mạng | Bind loopback, SSH tunnel, kiểm Host/Origin | Không TLS trong app; SSH là ranh giới mạng (`web.main`, handler) |
| T2 Process local gọi web | Username/password bắt buộc | Root hoặc UID migui bị chiếm vẫn là nguy cơ; không token đăng nhập trong file (`auth`, `web`) |
| T3 Dò mật khẩu | Scrypt n=32768,r=8,p=1,salt 16,key 32; hash serial và dummy check tên lạ | Khóa user 5 lỗi/600 s, global 20 lỗi/600 s, mỗi khóa 900 s; state lockout ở RAM (`web.State`, `Handler.login_failed`) |
| T4 Cướp/giữ phiên | Cookie random 32 bytes, HttpOnly, SameSite=Strict; max 8 h/idle 30 m; logout/new login thay phiên | Một phiên toàn hệ thống; cookie không Secure vì HTTP loopback; non-background request mới revalidate user/hash (`State.current`, `Handler.user_still_valid`) |
| T5 CSRF/rebinding | Origin đúng loopback+port, X-Requested-With migui, Sec-Fetch-Site absent/same-origin cho mọi POST, kể cả login | Host lạ 421; thiếu guard 403; không CORS (`web` POST checks) |
| T6 XSS | CSP self script/style/connect/img; object/base/frame none; form-action self; DENY/nosniff/no-referrer | Static dùng textContent; không CDN/inline executable; xem `web` headers và app.js |
| T7 Command injection | Argv list, allowlist flags, enums, bounds và inventory; runner kiểm lại | Openrc và notify executable là code root tin cậy (`common`, `op_plan`, `run_job`) |
| T8 Traversal | Job ID 16 hex, ident whitelist, không browser-supplied path; đọc file qua record đã biết | CLI trực tiếp có phạm vi quyền root rộng hơn UI (`common`, `op_status`) |
| T9 Leak credential | Web bị systemd chặn /opt/mig, root, engine state/log/job dir; scrub pattern và value secret từ probe | Raw `.out` và CLI log là root-only, không cam kết không có secret; secret đổi sau probe cần restart idle (`common.scrub`, `scrub_obj`) |
| T10 Chạy đè | Host flock chung MIG_LOCK, FD hand-down, state lock; runner kiểm process ngoài | Không khóa phân tán; không chống root thay path khóa (`try_lock`, `outside_runs`) |
| T11 Hủy/crash | SIGINT process group, grace 120 s, kill cần typed; giữ state điều tra | SIGKILL không cleanup; không tự rollback (`op_cancel`) |
| T12 Mất audit | O_APPEND audit; installer root:migui 0620 + chattr+a; mutating POST audit fail →503 trước forward | Login/logout và một số audit best effort; không fsync audit; root vẫn có thể bỏ +a (`common.Audit`, `web`) |
| T13 Web bị chiếm | User migui không capability, ProtectSystem=strict, PrivateDevices, ProtectHome, seccomp, loopback only | Runner không có cùng sandbox vì child cần network/devices; UID migui có quyền socket (`*.service`) |
| T14 Peer khác gọi runner | Socket 0660root:migui; SO_PEERCRED kiểm UID; 9 op whitelist | Không phải auth độc lập trước process đã chiếm UID migui (`peer_uid`, handler) |
| T15 DoS | Body 64 KiB; API semaphore 8, chờ 10 s rồi 503; 2 SSE/phiên rồi 429; header 30 s; plan/item/log bounds | Login/SSE ngoài semaphore API; runner socket không có cùng semaphore; raw output/disk không cap (`web`, `Runner`) |
| T16 Cache browser | Cache-Control no-store; không lưu credential ở localStorage | Chỉ theme `migui-theme` được giữ; máy/browser vẫn cần bảo vệ (`web`, app.js) |
| T17 Dependency | UI Python stdlib + JS/CSS local, không build npm/CDN | Engine có dependency pin riêng; bảo trì Python hệ thống và wheel (`pyproject.toml`, requirements-host.txt) |

Tài khoản trong `/var/lib/migui/users` root:migui 0640, tối đa 50 users/64 KiB. Password 12–128 ký tự printable, không bằng username; username lowercase tối đa 32. `migui-passwd` atomic replace và flock; không đưa password vào argv/env. Thay hash hoặc xóa user hủy phiên ở request không background kế tiếp. Background polling không revalidate credentials; stream kiểm cookie/expiry mỗi event. Đây không phải thu hồi tức thời mọi stream đang mở.

Plan events touch idle; batch của phiên đang chạy touch mỗi 60 s. Polling background tự nó không kéo dài idle. Max 8 h vẫn áp dụng. Logout/new login không dừng job. Audit POST plan/run/rollback/cancel/doctor là gate 503; không ghi được audit phải sửa storage/quyền trước thao tác.

### 8.4. Thông báo

`MIGUI_NOTIFY_CMD` là JSON argv, không shell (`notify_argv`, `notify_event`). Khi job thực thi kết thúc và khi batch kết thúc, runner chạy hook tối đa 30 s. Env thêm `MIG_EVENT` (`job`/`batch`), `MIG_JOB`, `MIG_STATE`, `MIG_BATCH`, `MIG_NAME`, `MIG_RC`, `MIG_OPERATOR`; batch có MIG_JOB rỗng. `notify_rc` ghi vào job. Job không spawn được hoặc bị skip có nhánh không gọi hook job. Hook lỗi không đổi kết quả migration; không coi nó là hệ thống delivery bền vững. Cài hook qua [DEPLOY.md](DEPLOY.md#6-cài-web-ui).

## 9. Giới hạn và việc hoãn

| Mục | Giới hạn hiện tại / quyết định vận hành |
|---|---|
| Journal/resume sau cutover | Chưa có; state không atomic xuyên API. Dùng điều tra/rollback, không xóa marker để ép resume |
| LUN identity | Size guard + WWN log chưa đối chiếu clone với PowerStore; canary WWN và guest checksum bắt buộc |
| SDK path | Chủ yếu OSC subprocess; SDK chỉ lookup volume description. Tối ưu số API call chưa hoàn tất |
| Parallel migration | Chỉ plan song song; migration tuần tự. Không nhiều convert host dùng cùng nguồn |
| Multi-session | Một phiên web toàn hệ thống; login mới thay phiên cũ |
| Whole-batch quota | Chỉ check mỗi VM; không reserve quota cho cả wave; cộng thêm root ephemeral và headroom thủ công |
| Round-robin presync | Chưa có; volume đầu có thể tích lũy delta trong khi sync volume sau |
| Standalone volume | Chưa self-clean A6; chưa exit 3/4; READY không phải preflight hoàn chỉnh |
| Crash-at-marker E1 | Chưa có test crash ở mọi ranh giới create/record/fsync; không suy đủ phủ từ unit count |
| Boot và network | Không extra ephemeral/swap/encrypted/multiattach VM; không nhiều fixed IP; FIP, DNS ngoài cloud, guest user-data không tự chuyển |
| Guest consistency | Clone live/in-use crash-consistent; cuối VM stop không thay thế nghiệm thu transaction/application |
| API không đọc được | Một số check MAC/IP, rules, rollback discovery bỏ qua hoặc coi rỗng; cần canary và freeze |
| Glance nhiều region | REST chọn interface nhưng chưa lọc region; xác minh endpoint trước ephemeral |
| Doctor write-check | Discard helper lỗi có thể INFO; rm không kiểm rc; xác minh image thử được dọn |
| UI presentation | Tổng root volume/GB chưa cộng root ephemeral mới; log và CLI state là bằng chứng đối chiếu |
| Upgrade/downgrade state | Không cam kết rollback an toàn bằng binary cũ; giữ binary tạo state và kiểm rehearsal |
| Thứ tự NIC | VM nhiều port: thứ tự gắn ở đích theo `port list --server` (`vmdef.export_definition`), Neutron không bảo đảm trùng thứ tự ở nguồn; guest cấu hình theo tên interface có thể đảo eth0/eth1. Canary kiểm `ip -o link` theo MAC |
| `config_drive` | Không chép; guest chỉ dùng datasource ConfigDrive sẽ không có metadata ở đích. Kiểm `config_drive` trong `server.json` của state |
| Tags và volume metadata | Server `tags` và Cinder volume metadata (không phải image metadata) không chép |
| Volume type/pool | 1.2.0: type theo **tên type nguồn** (`VOLUME_TYPE_MAP`, `__DEFAULT__`), pool theo type đó; không map theo volume ID, không đổi type sau khi volume đích đã tạo, UI không chọn map (chỉ hiện output CLI). `VOLUME_TYPE_MAP` rỗng: một type và một pool cho mọi volume của VM như trước, preflight WARN khi nguồn có ≥ 2 type (mục 6.2). Dung lượng Ceph theo pool chỉ cộng volume được copy, chưa cộng root ephemeral |

Các mục chưa được chứng minh trên hệ thật nằm trong [RELEASE-1.0.0.md](RELEASE-1.0.0.md). Không dùng tài liệu lưu trữ để suy thêm bảo đảm ngoài code hiện hành.
