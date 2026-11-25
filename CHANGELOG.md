# Changelog

Lịch sử thay đổi của tool. Từ v7: app Python `migrate` (`migratetool/`). Từ v1 tới v6: `migrate-vm.sh`,
`migrate-batch.sh`, `brick_attach.py`, `blocksync.py`, nay nằm trong [archive/](archive/README.md).
Bản mới nhất ở trên. Viết theo tinh thần [Keep a Changelog](https://keepachangelog.com/): mỗi bản ghi
ngày, nhánh, trạng thái, phần **Thêm** / **Đổi hành vi** / **Sửa**, commit chính, thiết kế và báo cáo test.

Repo git bắt đầu từ bản v2.1 (commit đầu `e973d00`, 17/11). Bản v1 và v2 có trước repo nên không có
commit riêng. Mã v1 lưu ở [archive/migrate-vm-v1-20261105.sh](archive/migrate-vm-v1-20261105.sh).

Repo công khai được khởi tạo lại ngày 22/11/2026 với một commit chứa bản 1.0.0. Mã commit nhắc trong file này và trong RELEASE-1.0.0.md thuộc lịch sử phát triển trước đó, không có trong repo công khai.

Cách thêm một mục mới: xem [DEVELOPING.md](DEVELOPING.md). Tài liệu lịch sử được giữ nguyên trong
[archive/docs/](archive/docs/); các liên kết gốc trong mục 7.x trở xuống được giữ theo bản ghi lịch sử.

## [1.2.3] — 2026-11-25 · branch `install-constraints` · one install path: the `[host]` extra under OpenStack upper-constraints

Thiết kế: phụ lục 1.2.3 của thiết kế 1.2 (sự cố cài trên host Ubuntu 22.04/Python 3.10: `pip install -r requirements-host.txt` dừng ở `Could not find a version that satisfies the requirement cliff==4.17.0`, vì bộ pin đóng băng từ Python 3.12 mà cliff ≥ 4.15 đòi Python ≥ 3.11). Test: `bash tests/unit/run.sh --parity` trên mig-host `ALL pass=1088 fail=2` (checks 2, unit 66, p5unit 45, p6unit 63, v72unit 84, v73unit 106, v1unit 144, v11unit 36, v12unit 62, v13unit 84, ui 191, parity 205): 2 FAIL là F-10b/F-10c, race đã biết của runner (S4, chưa sửa; `migui/runner.py` không đổi trong bản này); `tests/ui/run.sh` chạy ngay sau đó 193/0; UI guards 130/0; `./release.sh` build wheel OK. Gate lab thật với venv mới cài theo README bước 2 (constraints 2026.1, OSC 9.0.1): e2e 50/0, failure-injection 17/0. Báo cáo: `TEST-REPORT-1.2.3.md` (labkit). Commit chính: `3495bda` (cài đặt), `9ed68b2` (docs), `4ae29b8` (version). Mã migrate/rollback không đổi; chỉ đổi câu gợi ý của doctor khi brick không chạy.

### Đổi hành vi

- **Một cách cài duy nhất**, dùng chung cho checkout và wheel: `pip install -c https://releases.openstack.org/constraints/upper/<release> "<checkout hoặc wheel>[host]"` rồi `pip check`; `<release>` là release OpenStack của cloud đích (README bước 2, DEPLOY mục 2 và 2.1). Bản client do file constraints của release quyết định, nên cài được trên mọi Python mà release đó hỗ trợ (DEPLOY 2.1 có bảng release ↔ Python tối thiểu: 2025.1–2026.1 ≥ 3.10, 2026.2 ≥ 3.11). Cách A (bộ pin cứng) và lựa chọn A/B bị bỏ.
- Nâng cấp (README mục Nâng cấp, DEPLOY 7): `pip install --no-deps` chỉ thay tool, client trong venv giữ nguyên; đổi bộ client là cài venv mới theo bước 2 và nghiệm thu lại.
- Sau `ln -sfn … /usr/local/bin/migrate`, README bước 2 và DEPLOY 2 chạy `hash -r` và `type -a migrate`: host OpenStack hay có `/usr/bin/migrate` của gói `python3-migrate` (sqlalchemy-migrate), và khi nó đứng trước thì `migrate --version` in `Invalid command --version` của sqlalchemy-migrate (gặp trên host 22.04 thật). DEPLOY 8 thêm dòng xử lý.
- `migrate doctor` khi brick không chạy: `brick cannot start (...): install the 'host' extra into this venv with the release constraints (DEPLOY.md section 2.1)`.

### Bỏ

- `requirements-host.txt` (pip freeze của venv lab trên Python 3.12).
- Pin `==` trong extra `host` của `pyproject.toml`: extra giờ chỉ liệt kê 7 gói (`python-openstackclient`, `openstacksdk`, `os-brick`, `python-cinderclient`, `keystoneauth1`, `oslo.concurrency`, `oslo.config`), là nơi duy nhất ghi danh sách này.

### Biết trước

- Bộ client đi theo release constraints, nên host khác release/Python/OS có bộ client khác. Tool đọc chữ OSC in ra (ví dụ câu "không tìm thấy" của rollback), vì vậy **host mới phải nghiệm thu** (doctor, plan, migrate + rollback một VM thử) trước khi dùng thật (DEPLOY 2.1). Bộ đã chạy gate lab (DEPLOY 2.1): constraints 2026.1 trên Python 3.12.3, `python-openstackclient` 9.0.1, `openstacksdk` 4.10.0, `os-brick` 7.0.0, `python-cinderclient` 9.9.0, `keystoneauth1` 5.13.1, `oslo.concurrency` 7.4.1, `oslo.config` 10.3.0 (bộ pin cũ: OSC 10.5.0, SDK 4.22.0, os-brick 7.1.1).
- Ubuntu 22.04 chưa chứng minh bằng cài thật: lab không có image 22.04. Chỉ kiểm mức resolve (`pip install --dry-run --python-version 3.10 --only-binary=:all:`): constraints 2026.1 resolve được cho Python 3.10, 2026.2 thì không (gói đòi Python ≥ 3.11), bộ pin cũ tái hiện đúng lỗi `cliff==4.17.0`.

## [1.2.2] — 2026-11-25 · branch `preflight-polish` · Ceph capacity counts the ephemeral root; doctor says when the conf did not load

Thiết kế: phụ lục 1.2.2 của thiết kế 1.2 (S1, S5 từ backlog của review độc lập 1.2.0). Test: `bash tests/unit/run.sh --parity` (`tests/unit/v13unit.py` thêm T29, T30; T28 → 1.2.2): trên mig-host `ALL pass=1090 fail=0` (checks 2, unit 66, p5unit 45, p6unit 63, v72unit 84, v73unit 106, v1unit 144, v11unit 36, v12unit 62, v13unit 84, ui 193, parity 205); `tests/ui/run.sh` 193/0; UI guards 130/0. Parity không đổi (dòng `OK/WARN Ceph pool` đã nằm trong rule bỏ qua từ 7.2). Báo cáo: `TEST-REPORT-1.2.2.md` (labkit). Commit chính: `dfc4806` (S1), `47939f0` (S5), `0df9340` (test), `9f1fdc5` (docs). Đường copy và rollback không đổi.

### Thêm

- `migrate doctor` khi conf không parse được: ngay sau dòng `FAIL conf <path>: ...` in `INFO conf <path> not loaded: the checks below use the environment and the built-in defaults only; fix the conf first`. `MIG_ISCSI_IP` rỗng trong trường hợp đó thành `INFO MIG_ISCSI_IP not checked: conf not loaded` (giá trị có thể nằm trong conf chưa nạp); có trong biến môi trường thì vẫn kiểm như thường. Các kiểm khác giữ nguyên. Doctor vẫn trả 1 vì dòng FAIL của conf.

### Sửa

- **S1** Dung lượng Ceph của preflight không cộng root disk ephemeral: VM ephemeral (root thành volume, không `--ephemeral-as-image`) không có dòng `Ceph pool` nào khi không có volume, hoặc thiếu đúng phần root khi có. Nay cộng flavor disk của root vào pool của volume root, cùng quy tắc quota `gigabytes` đã cộng. Ví dụ: root 20 GB, pool còn 15 GB → `FAIL Ceph pool volumes: 20 GB needed, 15 GB left (MAX AVAIL)`; root 20 GB + volume data 2 GB cùng pool → `22 GB needed`. `--ephemeral-as-image` và `migrate volume` không đổi.
- **S5** `migrate doctor` với conf hỏng (ví dụ `VOLUME_TYPE_MAP="a"`) báo thêm `FAIL MIG_ISCSI_IP is not set` dù conf có đặt: lỗi giả, che lỗi thật. Nay là dòng INFO ở trên.

### Biết trước

- Như 1.2.1. Dung lượng root ephemeral tính theo flavor disk; virtual size lớn hơn của image chỉ biết khi download (như quota); flavor disk 0 hoặc không đọc được thì không cộng gì (preflight đã WARN `free space not checked`).

## [1.2.1] — 2026-11-25 · branch `type-map-star` · VOLUME_TYPE_MAP fallback key `*`; `__DEFAULT__` is a real type name

Thiết kế: phụ lục 1.2.1 của thiết kế 1.2 (từ L2 của review độc lập 1.2.0: Cinder đặt tên type mặc định thật là `__DEFAULT__`, trùng khóa fallback của 1.2.0, trong khi `RBD_POOL_MAP` đã hiểu `__DEFAULT__` là tên type thật). Test: `bash tests/unit/run.sh --parity` (`tests/unit/v13unit.py` T3/T7/T23 đổi sang `*`, thêm T25–T28): trên mig-host `ALL pass=1081 fail=0` (checks 2, unit 66, p5unit 45, p6unit 63, v72unit 84, v73unit 106, v1unit 144, v11unit 36, v12unit 62, v13unit 75, ui 193, parity 205); UI guards 130/0. Parity không đổi (`v13_rule` không khớp câu WARN). Báo cáo: `TEST-REPORT-1.2.1.md` (labkit). Commit chính: `1cb9107` (map), `3dd3ff2` (test), `372a6c1` (docs).

### Thêm

- `VOLUME_TYPE_MAP` khóa `*`: `*:type-đích` là fallback cho volume nguồn có type không có trong map, volume nguồn không có type và root disk ephemeral. Nhãn nguồn của dòng INFO và `effective.volume_types[...].from`: `VOLUME_TYPE_MAP (*)`. `doctor` liệt kê `*` trong các nguồn của type đích (`OK VOLUME_TYPE_MAP destination type rbd-hdd (for ps-silver, *)`).
- `*` ở vế đích (`a:*`) dừng mọi lệnh `VOLUME_TYPE_MAP: '*' is only a source key` trước khi gọi cloud; `doctor` FAIL dòng conf với câu đó.

### Đổi hành vi

- **Không tương thích ngược có chủ đích, không có shim:** trong `VOLUME_TYPE_MAP`, `__DEFAULT__` giờ là **tên type thật** (type mặc định của Cinder), khớp đúng chuỗi như mọi tên khác và như trong `RBD_POOL_MAP`; không còn là fallback. Conf viết `__DEFAULT__:x` với ý fallback (cách viết của 1.2.0) giờ chỉ đổi type của volume nguồn mang type `__DEFAULT__`; các type nguồn khác không có trong map về type của lần chạy kèm WARN. Đổi thành `*:x`. Ví dụ: map `__DEFAULT__:rbd-hdd,*:rbd-ssd` đưa volume type `__DEFAULT__` về `rbd-hdd` (`VOLUME_TYPE_MAP (__DEFAULT__)`), volume `ps-silver` và volume không có type về `rbd-ssd` (`VOLUME_TYPE_MAP (*)`).
- Câu WARN preflight: `source type <s> has no entry in VOLUME_TYPE_MAP and no '*' entry: lands on <type>` (1.2.0: `… and no __DEFAULT__: …`).

### Sửa

- **L2** Không map riêng được volume nguồn type `__DEFAULT__` khác với fallback, và nhãn `VOLUME_TYPE_MAP (__DEFAULT__)` không nói khớp theo tên hay theo fallback: nay `(__DEFAULT__)` luôn là khớp tên, `(*)` luôn là fallback.

### Biết trước

- Như 1.2.0 (map chỉ theo tên type nguồn; `--verify readback` qua cùng handle librbd; canary 2023.1 + PowerStore chưa chạy). Đường copy và rollback không đổi so với 1.2.0.

## [1.2.0] — 2026-11-24 · branch `volume-type-map` · VOLUME_TYPE_MAP (type and pool per volume), rollback WARNs instead of silence, README upgrade

Thiết kế: xem review 1.1.0 backlog (việc 1–5; ngoài phạm vi: rbd_cache readback, scrub `source_error` ngoài scrub value hiện có, canary PowerStore/2023.1). Test: `bash tests/unit/run.sh --parity` (bộ mới `tests/unit/v13unit.py`, `tests/ui/v13ui.py`, case `source_error` trong `tests/ui/guards.mjs`): trên mig-host `ALL pass=1072 fail=0` (checks 2, unit 66, p5unit 45, p6unit 63, v72unit 84, v73unit 106, v1unit 144, v11unit 36, v12unit 62, v13unit 66, ui 193, parity 205); UI guards 130/0. Parity không đặt map: map rỗng giữ hành vi 1.1.1 (quy tắc `v13_rule`). Báo cáo: `TEST-REPORT-1.2.0.md` (labkit). Commit chính: `1d22035` (map), `33ada51` (R1), `f205203` (R3), `9c81ad2` (lời từ chối application credential), `341178c` (test), `5adcd93`/`e006de6` (docs).

### Thêm

- **VOLUME_TYPE_MAP** (conf, whitelist `options.json` `conf`, `migrate.conf.example`): `VOLUME_TYPE_MAP="type-nguồn:type-đích,…[,__DEFAULT__:type-đích]"`. Mỗi volume đích lấy type theo thứ tự `--volume-type` gõ tay (mọi volume) → `VOLUME_TYPE_MAP[type nguồn]` → `VOLUME_TYPE_MAP[__DEFAULT__]` → type của lần chạy (`AZ_TYPE_MAP`, `MIG_DEFAULT_OPTS`, default Cinder); pool theo `RBD_POOL_MAP` của type đó, không có thì `RBD_POOL`. `__DEFAULT__` cũng áp cho volume nguồn không có type và root disk ephemeral. Áp cho migrate cold/live, root ephemeral thành volume, `migrate volume`. Map sai dạng dừng `VOLUME_TYPE_MAP: …` trước mọi lệnh cloud.
- Preflight khi map có đặt: dòng INFO từng volume thêm `(<nguồn>)`, `WARN source type <s> has no entry in VOLUME_TYPE_MAP and no __DEFAULT__: lands on <type>`, `INFO VOLUME_TYPE_MAP in use: <n> volume(s) mapped`, `INFO volume type <t> from cli overrides VOLUME_TYPE_MAP`, `volume type show` một lần mỗi type đích (`FAIL destination volume type <t> (VOLUME_TYPE_MAP (<s>)) does not exist`), `RBD access to pool` và dung lượng Ceph mỗi pool, quota `volumes_<type>`/`gigabytes_<type>` theo type đã map. `migrate doctor` kiểm mỗi type đích của map có ở đích.
- `effective.volume_types` (`options.json`, `plan --json`): `{"value": {<id volume nguồn>|"ephemeral-root": {"type", "pool", "from"}}, "from", "detail"}`. File state mới `volume-types` (`<id nguồn> <type|-> <pool>` mỗi dòng; copy đọc lại pool của từng volume từ đây). `volume-map` giữ hai cột: rollback (và state cũ) không đổi.
- README mục **Nâng cấp** cho host cài bằng `git clone` (giữ checkout cũ thành `/opt/mig-src.bak-<bản đang chạy>`); DEPLOY mục 7 trỏ tới đó cho đường `git clone`.

### Đổi hành vi

- **R3** Rollback: port đích (`dst-ports`) `port show` lỗi không còn bị coi là "không có port": `WARN cannot read destination port <id> (<lý do>): left, check it by hand`; not found nêu đúng port → `INFO destination port <id> already gone`; `port delete` lỗi → `WARN cannot delete destination port <id> (<lý do>): delete it by hand`. Rollback đi tiếp, trả nguồn (trước đây lỗi đọc bị nuốt im lặng, lỗi xóa làm rollback dừng khi nguồn còn lock). Quét tên `mig-<ident>-<i>` cũng vậy (không có tên đó vẫn im lặng). `image list` đích lỗi → `WARN cannot list destination images named mig-snap-<vm>: skipped`, image trong `dst-image-id` vẫn bị xóa.
- **R3** UI: `original.source_error` (đã scrub) trong chuẩn bị rollback; hộp xác nhận ghi `Nguồn hiện tại: không đọc được (<lý do>)`.
- Rollback bằng application credential của state có scope trong record (ghi bằng password openrc) bị từ chối bằng câu `this state was written by a run scoped with SRC_PROJECT=<A> DST_PROJECT=<B> (a password openrc); <openrc> is an application credential, which cannot be rescoped: run the rollback with the password openrc that wrote the state; nothing was done` thay cho `SRC_PROJECT/DST_PROJECT needs a password openrc …` (migrate/volume và scope từ môi trường giữ câu cũ).
- **R1** `rollback.gone()`: dòng nêu đúng ID có `(HTTP 404)` ở bất kỳ đâu, hoặc token `404` đứng riêng theo sau là `Not Found`/hết dòng, cũng là "đã mất" (trước đây chỉ khi dòng kết thúc bằng 404: một `Not Found (HTTP 404) (Request-ID: …) …/volumes/<id>` làm rollback dừng `cannot read …` dù object đã mất).
- `doctor`: `VOLUME_TYPE_MAP` sai dạng trong môi trường làm FAIL dòng conf, các kiểm khác vẫn chạy.

### Sửa

- —

### Biết trước

- Map chỉ theo **tên type nguồn**, không theo ID volume; không đổi type của volume đích đã tạo; UI không chọn map (hiện output CLI). Dung lượng Ceph theo pool chỉ cộng volume được copy, chưa cộng root ephemeral (như 1.1.1).
- `--verify readback` của live mode vẫn đọc lại qua cùng handle librbd (có `rbd_cache`); canary trên hạ tầng 2023.1 + PowerStore chưa chạy.

## [1.1.1] — 2026-11-24 · branch `rollback-dest-check` · rollback trusts "gone" only for the object named, and reads the destination before it deletes (no data-path change)

Thiết kế: `ISCSI-RBD-MIGRATION-ROLLBACK-DEST-CHECK-DESIGN.md` (Lab), từ review độc lập 1.1.0 (23/11: P1, P2-a, P2-b, P3-1..P3-6). Test: `bash tests/unit/run.sh --parity` (bộ mới
`tests/unit/v12unit.py`, `tests/ui/v12ui.py`): trên mig-host `ALL pass=1001 fail=0` (checks 2, unit 66, p5unit 45, p6unit 63, v72unit 84, v73unit 106, v1unit 144, v11unit 36, v12unit 62, ui 188, parity 205); UI guards 127/0. Báo cáo: `TEST-REPORT-rollback-dest-check.md` (labkit).

### Thêm

- **A21** `rollback.gone(stderr, oid)` thay `GONE_RE` và `server.PORT_GONE_RE`: một object chỉ được coi là đã mất khi một dòng trả lời nêu đúng ID của nó và nói `No <Kind> found for <id>`, `No <kind> with a name or ID of '<id>' exists`, `<id> could not be found` hoặc kết thúc bằng `(HTTP 404)`. Áp cho `wait_gone`, `drop_volume`, `delete_server`, probe nguồn A20, summary, `_port_device` (rollback) và `detach_source_ports` (migrate).
- **A21** `rollback.require_destination`: ngay sau A20, trước lệnh xóa đầu tiên, rollback đọc `server show <dst-server-id> -f json -c id -c status` (không có server ID thì `volume show` volume đích đầu tiên; volume đơn cũng vậy). Đúng object hoặc not found nêu đúng ID thì đi tiếp (chạy lại sau lần xóa dở); còn lại dừng `cannot read destination server|volume <id> (<lý do>): nothing was deleted at the destination, the source was not touched; check the destination credentials/endpoint/DST_PROJECT and run the rollback again`.
- `wait_gone`: 5 lần `show` lỗi liên tiếp không nêu object thì dừng `cannot read <label> <id> after its delete (...)` (khoảng 15 s), không chờ 600 s. `drop_volume`: `cannot read ...; nothing more was deleted`.
- `rollback --dry-run [--json]`: `dst_error` (lý do khi server đích không đọc được; `dst_server_status` từ cùng probe), dòng `WARN the destination server could not be read (...): the rollback refuses until it can`; `projects {src, dst, origin: record|env|none}`. UI: hộp xác nhận rollback có dòng `Đích hiện tại: <status>` (`đã xóa`, `không đọc được`, `không có server đích ghi nhận`); runner chuyển `dst_error` vào `original`, không 409.
- **P2-a** Rollback (CLI, cả dry-run; runner UI đi qua CLI) lấy `SRC_PROJECT`/`DST_PROJECT` từ `options.json` của state khi môi trường không đặt (`INFO scope SRC_PROJECT=… DST_PROJECT=… (from the state's options.json)`); state không có record giữ scope hiện hành (INFO); volume chỉ tìm được qua cloud theo tên giữ scope hiện hành (INFO).
- **P2-a** Server đích chưa ghi ID được tìm bằng `server list --name ^<name>$ --all-projects` (403 thì thử lại không cờ); owner mark và created ≥ t-stop vẫn là điều kiện xóa.
- **P3-2** A6 (`clean_before_cutover`) hỏi nguồn trước khi xóa volume đích của lần chạy lỗi trước (`require_source`): không tìm thấy/không đọc được thì dừng exit 3, `this re-run deleted nothing; the earlier state stays live`.
- **P2-b** Preflight: `INFO volume <id> (<name>) source type <t> -> destination type <t_dst> pool <pool>` cho từng volume, `WARN <n> source volume types (...) all land on destination type <t_dst>: pass --volume-type per run if they must differ` khi VM có ≥ 2 type nguồn. Không đổi exit code.

### Đổi hành vi

- Rollback từ chối khi đích không đọc được (endpoint thiếu trong catalog, `(HTTP 404)` không nêu object, auth, mạng): không xóa gì, không đụng nguồn, state sống. Trước đây (1.0.0–1.1.0) lỗi có chữ `not found`/`404` trần được coi là đã xóa: rollback trả nguồn (unlock, gắn lại port cùng MAC/IP, start), retire state, exit 0 trong khi server và volume đích vẫn còn (hai VM cùng MAC/IP).
- Port nguồn: lỗi endpoint của Neutron không còn được coi là "Nova đã xóa port" (rollback từng bỏ qua gắn lại NIC và exit 0; migrate từng tạo reserve port thừa): thử lại rồi dừng.
- Rollback với `SRC_PROJECT`/`DST_PROJECT` khác record của state (kể cả chỉ đặt một bên) dừng `scope mismatch: …; nothing was done`. Mục "Biết trước" về scope của [1.1.0] đã đóng: CLI và UI đều chạy trong scope của record.
- A6 thêm một lệnh đọc nguồn trước khi dọn; summary/dry-run trả `dst_server_status` `null` (không phải status) khi `server show` exit 0 mà không phải đúng server.

### Biết trước

- Volume metadata của Cinder, server `tags` và `config_drive` không được chép; thứ tự NIC ở đích theo `port list --server` (DESIGN mục 9).
- `--verify readback` của live mode đọc lại qua cùng handle librbd (có `rbd_cache`): chứng minh ghi đúng offset, không chứng minh dữ liệu đã tới OSD; `--verify full` là kiểm độc lập.
- Một volume type/pool cho mọi volume của VM (map theo từng volume: backlog 1.2).
- Rollback dùng cùng loại openrc với lần migrate: state ghi bằng password openrc (record `effective.projects` có project, ví dụ mọi lần chạy qua selector) rollback bằng application credential bị từ chối ngay với lời `SRC_PROJECT/DST_PROJECT needs a password openrc` (scope khôi phục từ record, application credential không rescope được; 1.1.0 bỏ qua record nên chạy được). State ghi bằng application credential (record không có project) rollback bằng gì cũng được. Lời báo lỗi nêu rõ record: backlog 1.2. Ghi ở README, GUIDE mục 6.

## [1.1.0] — 2026-11-23 · branch `rollback-source-check` · rollback checks the source exists before it deletes anything (no data-path change)

Thiết kế: `ISCSI-RBD-MIGRATION-ROLLBACK-SOURCE-CHECK-DESIGN.md` (Lab). Test: `bash tests/unit/run.sh --parity` (bộ mới
`tests/unit/v11unit.py`, `tests/ui/v11ui.py`): trên mig-host `ALL pass=934 fail=0` (checks 2, unit 66, p5unit 45, p6unit 63,
v72unit 84, v73unit 106, v1unit 144, v11unit 36, ui 183, parity 205); UI guards 122/0. Báo cáo: `TEST-REPORT-rollback-source-check.md` (labkit).

### Thêm

- **A20** `migrate rollback` hỏi nguồn trước mọi thao tác xóa ở đích: `openstack server show <source id> -f json -c id -c status` cho state VM, `volume show <source id>` cho state `migrate volume` (`rollback.require_source`). Trả lời được thì đi tiếp như 1.0.1 (status nào cũng được). Not found: dừng, exit 1, `source VM|volume <id> (<name>) is not found at the source (deleted, not visible to the source credentials/SRC_PROJECT, or the wrong source endpoint/region): the destination (server …, volumes …) may be the only remaining copy; this rollback deleted nothing. … check the source scope and endpoint before removing it by hand …` (404 có thể là đã xóa, scope sai hoặc endpoint/region sai), state vẫn sống; thông báo nói `migrate prune` không bao giờ xóa state sống, nên khi chắc chắn giữ đích thì xóa thư mục state bằng tay. Lỗi khác (auth, mạng, scope): dừng, `cannot read source VM|volume <id> (<lý do>): this rollback deleted nothing; check the source credentials/SRC_PROJECT and run the rollback again`. Rollback chạy lại sau lỗi giữa chừng vẫn qua khi nguồn vẫn đọc được (chỉ hỏi nguồn). `clean_before_cutover` (A6) không đổi.
- `rollback --dry-run [--json]`: trường `source_status` (status Nova/Cinder hiện tại của nguồn, `"GONE"` khi not found: đã xóa, hoặc credentials/scope/endpoint nguồn không thấy, `"UNKNOWN"` khi show trả đúng id mà không có status, `null` khi không đọc được, khi exit 0 nhưng output không phải object có đúng id (`unexpected answer from <kind> show`: rollback cũng từ chối), hoặc khi state không có id nguồn) và `source_error` (lý do khi `null`). State thiếu id trong `server.json`/`volume.json`: rollback dừng `state <dir> has no source id in …: nothing was deleted` trước mọi lệnh. Text: `, now <status>` ở dòng đầu; dòng `ERROR the source … is not found at the source: … this rollback is refused` (GONE) hoặc `WARN the source … could not be read (…)` (null). Summary trước prompt cũng vậy.
- UI: runner từ chối chuẩn bị rollback (`POST /api/rollback`) khi dry-run nói `source_status: "GONE"`: 409 `<ident>: the source VM|volume is not found at the source (deleted, or not visible to the source credentials); the destination may be the only remaining copy, a rollback would delete it`, không tạo batch. `null` vẫn chuẩn bị được, `original.source_status = null`. Hộp xác nhận rollback có dòng `Nguồn hiện tại: <status>` (`không tìm thấy (đã xóa, hoặc credentials/scope/endpoint nguồn không thấy)`, `không đọc được`).

### Đổi hành vi

- Rollback từ chối khi VM/volume nguồn không tìm thấy (đã xóa, hoặc credentials/scope/endpoint nguồn không thấy) hoặc không đọc được. Trước đây (1.0.1) nó xóa server, port, volume, image đích rồi mới lỗi ở bước trả nguồn: bản sao có thể là duy nhất còn lại đã mất.

### Biết trước

- Rollback chuẩn bị trong UI chạy với scope hiện hành của runner, không lấy `SRC_PROJECT`/`DST_PROJECT` từ record của job gốc (như CLI). VM/volume nguồn nằm ở project khác có thể trả 404: A20 coi là không tìm thấy, rollback từ chối, không xóa gì.

## [1.0.1] — 2026-11-23 · branch `fix-1.0.1` · bug fixes from the review of 23/11 (no data-path change)

- Selector: Ctrl-C / `kill -INT` during the migrate loop of a selection (`migrate migrate|volume`; a SIGINT earlier — listing, plan, prompt — exits 130 as before) still prints Summary, the rollback hint of the interrupted row (`FAIL(interrupted)`, later rows `SKIP(not-run)`) and Totals, then exits 130. A row whose child had already returned keeps the result of its exit code (a row whose child had exited but whose exit code was not yet recorded is still `FAIL(interrupted)`).
- UI runner: a cancel that arrives after migrate printed its `result` rc 0 leaves the job OK, rc 0, `cancel.late` (the batch still stops, its notification says `CANCELLED`; the audit `end` carries the real `exit`); the UI notes "Hủy đến sau khi job đã xong". A batch stopped with "Dừng sau con hiện tại" also notifies `CANCELLED` (no job failed); a batch with a failed job notifies `FAIL` even if it was cancelled after that; a batch whose jobs are all OK notifies OK even when the cancel or stop came after the last job.
- Selector: `kill -INT <pid>` of `migrate plan --plan-jobs N` passes SIGINT to every running plan child and starts no other; exit 130 within ~10 s even if a child was still starting or an openstack call of it was still running (plan children of a parallel plan run in their own process group; a second SIGINT after 3 s, SIGKILL of the group after 5 s more: plan is read-only) (the bound assumes no descendant leaves the group and I/O is not stuck; a parallel plan has no controlling terminal, so credentials must be non-interactive — the openrc must not `read` from /dev/tty).
- UI runner: a running `migrate ... --dry-run` (e.g. `rollback --dry-run`; only a `--dry-run` after the verb counts) no longer counts as a run outside the UI (no more 423).
- UI runner: the host lock file it holds says `pid N (migui-runner)`, so the CLI's refusal names who holds it.

## [1.0.0] — 2026-11-21 · branch `v1` · engine + runner/web (Opus), UI + tài liệu (Codex), review/click-through (Fable), click matrix (Codex) · gate lab 22/11 (RELEASE-1.0.0.md mục 5)

**Đổi cách đánh số: 1.0.0 = 7.3.0 + review 21/11; lịch sử 7.x bên dưới.** Baseline `269ed24`;
engine và UI đã gộp ở `959370f`. Hợp đồng thực thi: [DESIGN.md](DESIGN.md); hồ sơ review:
[REVIEW-20261121.md](REVIEW-20261121.md). Test: `bash tests/unit/run.sh --parity` (bộ mới `v1unit.py`,
`tests/ui/v1ui.py`); kết quả trên mig-host 22/11 (`a573fc9`): `ALL pass=871 fail=0` (checks 2, unit 66, p5unit 45, p6unit 63,
v72unit 84, v73unit 106, v1unit 128, ui 172, parity 205). UI guards 115/0; click matrix 221/0/0 (Edge headless, UI của `c3b7621`).
Bằng chứng, môi trường và gate còn thiếu: [RELEASE-1.0.0.md](RELEASE-1.0.0.md).

### An toàn dữ liệu (engine)

- **A1** `blocksync.py` đọc mỗi chunk đủ byte (`read_exact`): `pread` thiếu byte thì đọc tiếp; EOF/lỗi đọc giữa chừng →
  exit 4, không ghi hash state, pass FAIL (`blocksync failed … a short read from the source clone (exit 4)`). Test với
  blocksync thật, file device giả, librbd giả (short read, device co lại).
- **A2** Dấu chủ sở hữu: server đích `--property mig.source_id=<src id> --property mig.state=<state>`, port đích không
  có description nguồn mang `mig:<state>`, `options.json` ghi `owner_mark`. Rollback chỉ xóa server không ghi ID khi có
  dấu khớp; server cùng tên không dấu: bỏ qua (state 1.0.0) hoặc dừng kèm lệnh tay (state cũ). Quét port `mig-<arg>-<i>`
  cũng theo dấu.
- **A3** Rollback: `port show` lỗi khác "not found" → hỏi lại ≤ 60 lần rồi dừng; port gắn vào VM khác → dừng, in lệnh tay.
- **A4** `server delete --wait` kiểm exit code; poll `server show -f json -c id -c status` tới "not found" (≤ 600 s)
  trước khi đụng nguồn; không xác nhận được → dừng, nguồn không bật.
- **A5** `migrate rollback <t>`: không `--yes` thì in tóm tắt (t-stop, tuổi, server đích + trạng thái, port/volume/image
  sẽ xóa, WARN khi đích ACTIVE > 1 h) rồi hỏi `Type <tên>` (hoặc ident); stdin không phải terminal → exit 2. Thêm
  `--dry-run [--json]` (chỉ đọc). Runner gọi rollback với `--yes`.
- **A6** `migrate migrate` gặp state có `volume-map` nhưng chưa có `t-stop` → tự xóa volume đích + clone đã ghi, retire
  state, chạy tiếp; selector không SKIP các state đó. State qua cutover: selector ghi `SKIP(state-exists)` kèm lệnh
  rollback và trả 1; chỉ khi tiến trình con tự gặp state đó mới báo `state exists … run rollback first (that run stopped
  the source at <UTC>)`, exit 4 (đính chính 22/11, xem mục sửa sau gate).
- **A7** Khóa host `MIG_LOCK` (mặc định `/run/mig/migrate.lock`) cho migrate/volume/rollback (plan không khóa), truyền cho
  tiến trình con qua `MIG_LOCK_FD`; runner giữ cùng file cho cả đợt (đọc `MIG_LOCK` qua probe). Thêm flock trên thư mục
  state.
- **A8** Preflight WARN khi cold copy > `MIG_COLD_WARN_GB` (mặc định 500), khuyên `--mode live`.
- **A9** VM ephemeral: root disk thành volume đích tạo từ image đã upload (`volume create <vm>-vda --image --size <disk
  flavor, ít nhất virtual size>`), boot `--volume`, image đích xóa sau khi volume available, volume-map
  `ephemeral-root <id>`. `--ephemeral-as-image` giữ cách 7.x.
- **A10** `--ip <addr> --start-source`: FAIL khi addr là IP hoặc allowed-address-pair của nguồn; WARN AAP khi
  `--start-source`.
- **A11** Preflight FAIL khi flavor nguồn có ephemeral hoặc swap disk.
- **A12** Ngay trước `server stop`: so lại volume, port, flavor của nguồn với lúc export; lệch → dừng trước cutover (exit 3).
- **A13** SG đích cùng tên được so rule với nguồn: WARN thiếu/thừa; bản copy do tool tạo mang description
  `mig-copy of <src id>[: <mô tả nguồn>]`; copy dở (thiếu rule) → WARN `HALF-MADE COPY`.
- **A14** Nguồn bị lock có `locked_reason` của người (không phải `mig:`) → FAIL preflight. Rollback trả nguồn về đúng
  trạng thái nguồn điện lúc export (SHUTOFF giữ SHUTOFF).
- **A15** Rollback poll từng volume đã xóa tới "not found"; `error_deleting` hoặc quá hạn → dừng, không retire state.
- **A17** brick giữ bản ghi sau detach thành `<id>.detached.json` (có WWN); log WWN khi attach.
- **A18** State ghi qua `fsync` (file + thư mục); đọc lỗi khác ENOENT → dừng.
- **A19** brick: lỗi `complete()` → disconnect trước khi xóa attachment; detach gặp attachment 404 vẫn coi là xong.

### Bảo mật

- **B1** Runner thay mọi giá trị `OS_PASSWORD`/`OS_APPLICATION_CREDENTIAL_SECRET`/`OS_TOKEN` của 2 openrc (lấy qua probe)
  bằng `[scrubbed]`; thêm mẫu JSON/nháy và URL `scheme://u:p@`; khóa giống credential trong object bị thay cả giá trị.
- **B2** Tra SDK của `migrate volume` chạy `sys.executable -I`.
- **B3** Source openrc trong môi trường sạch (bỏ mọi `OS_*`), `set -e`, bắt buộc `OS_AUTH_URL`; doctor WARN openrc không
  phải root 0600, FAIL khi 2 openrc cùng Keystone (lúc chạy: WARN stderr).
- **B4** Phiên web hết hiệu lực khi user bị xóa hoặc đổi mật khẩu (so `changed`+hash ở request không background;
  stream đang mở không revalidate credential mỗi event).
- **B5** Không ghi được audit → POST thay đổi trả 503; `install.sh` cảnh báo rõ khi fs không có `+a`.
- **B6** `os.umask(0o077)` đầu CLI.
- **B7** Khóa đăng nhập theo user (5/10 phút) + toàn cục (20/10 phút).
- **B8** Tối đa 8 request API cùng lúc, 2 SSE mỗi phiên, timeout đọc header 30 s.
- **B9** Doctor kiểm euid 0 hoặc `sudo -n true`.

### Vận hành

- **C1** Web touch phiên ở mỗi sự kiện plan và mỗi 60 s khi batch của phiên chạy; runner plan tối đa 4 VM cùng lúc
  (`--plan-jobs` của runner); CLI `migrate plan … --plan-jobs N` (1–8, dòng tee có tiền tố `[<ident>]`).
- **C3** `proc.run` deadline `MIG_CMD_TIMEOUT` (600 s) cho openstack/rbd/ceph/systemctl (trừ `image save`), exit 124;
  Glance REST timeout 60 s, upload 600 s không tiến triển.
- **C4** `PROJECT_MAP_FILE` không đọc được → dừng.
- **C5** Batch ghi `conf_digest` (sha256 conf + 2 file map); lúc chạy lệch → 409 "plan again".
- **C6** `migrate doctor --write-check` (4 MiB mỗi pool: import, export so sánh, discard qua librbd, rm);
  `POST /api/doctor {"write_check": true}`.
- **C7** brick: session keystoneauth theo `OS_CACERT`/`OS_INSECURE`, `OS_INTERFACE`, `OS_REGION_NAME`; doctor gọi
  `brick --check-auth`.
- **C8** Exit code VM: 3 lỗi chuẩn bị trong vùng được phân loại, 4 sau t-stop; 5 khi nhiều hàng đã attempted và có
  FAIL. Preflight/lỗi chung vẫn 1; volume đơn chưa có phân lớp 3/4.
- **C9** `migrate prune --older-than Nd [--dry-run] [--json]`: chỉ `.rolled-back.*` và thư mục batch log cũ.
- **C10** Quota thiếu `In Use` → WARN, không coi là 0.
- **C11** Server group cùng tên/policy nhưng khác rules → FAIL kèm `--server-group <id>`; `--rule` bị từ chối → WARN rõ.
- **C12** Thư mục batch log `<time>-<6 hex>`.
- **C13** `LIST_MAX` 128.
- **C14** `MIGUI_NOTIFY_CMD` (argv JSON, không shell) cuối mỗi job/batch, env `MIG_EVENT`, `MIG_JOB`, `MIG_STATE`…;
  `notify_rc` trong job record.
- **C15** `[project.optional-dependencies] host`, `requirements-host.txt` (pip freeze venv lab), `release.sh`/`make
  release` (wheel + sha256).

### UI back end (hợp đồng mục 3)

- **D2** Luật gõ tên tính từ option hiệu lực của plan row (keep_ports, ip addr, start_source, in_use).
- **D3** Gate Doctor chỉ cho inventory, list, plan, run batch vm/vol.
- **D5** Xác nhận theo dòng (tên giữ nguyên, có khoảng trắng/dấu phẩy), chấp nhận ident đầy đủ.
- **D9** `GET /api/jobs?offset&limit&q&state&kind&from&to` → `{jobs,total,active}`; `GET /api/jobs.csv`.
- **D12** `migrate status --json`/`list --json`: `phase` (preparing/cutover/migrated), `source_power`.
- **D16** Plan row: `volumes_total`, `gb_total`, `nics`, `source_power`, `impact`. Batch: `fingerprint`, `impact`,
  `conf_digest`, `session`. Job: `volumes_total`, `phase_detail`, `exit_class`, `notify_rc`. `/api/session`:
  `ui_version`, `features`. Rollback `original`: `t_stop`, `dst_server_status`, `age_seconds`, `delete`.

### Test, chất lượng

- **E1** Test blocksync thật (A1), VM lạ cùng tên sau t-stop (A2). Crash tại từng marker: chưa làm.
- **E2** `run.sh` tính cả exit code của suite.
- **E3** `migratetool/contract.py`: tiền tố preflight, chuỗi grep, exit code, dấu chủ sở hữu.
- **E6** SEC-7 grep thêm `"bash", "-c"` (whitelist `clouds.rc_env`).
- Parity: luật máy móc `parity.v1_rule` (mỗi dòng `Intended: v1 <ID>`), app chạy rollback với `--yes` và plan/migrate
  thêm `--ephemeral-as-image` ở kịch bản cũ.

### UI do Codex viết và sửa sau click-through

- **D1/D4/D6–D8/D10–D17** (`a15f6db`): stepper Doctor → Chọn → Tùy chọn → Xác nhận → Chạy; Tổng quan có số liệu và
  job gần đây; options theo nhóm với preview; fingerprint vô hiệu plan khi selection/options đổi; khối tác động,
  checkbox WARN và dialog RPO rollback; countdown SIGKILL gắn job và dựng lại từ server; reconnect SSE; progress
  dùng tổng volume từ plan; Lịch sử lọc/phân trang 50/CSV/rerun/diff; theme sáng/tối và accessibility.
- **Câu chữ UI** (`cf01857`, `12b94e1`, `45f41c1`): nhãn tiếng Việt tự nhiên; một nút Chạy Doctor ở Tổng quan,
  link kết quả; sửa heading login, hint options, chữ Doctor và placeholder phase.
- **Click-through của Fable** (`04d5506`): dấu ngăn title; stepper disabled không điều hướng; theme button nêu hành
  động; mỗi WARN khác nhau một acknowledge; banner nguồn down phản ánh rollback CLI; đóng dialog rollback trở về
  màn gốc; dán danh sách phân biệt không tìm thấy/mơ hồ/không đủ điều kiện.
- Merge UI vào v1: `959370f`.
- **UI round 2** (`efc46cf`, Codex): ô IP "theo dòng" nằm trong nhóm Mạng của Tùy chọn (IPv4 validate); dialog Rollback
  riêng (phạm vi xóa, cutover/tuổi, trạng thái đích, nguồn, RPO, tên/ident, nút đỏ "Rollback <tên>"); stepper chỉ trong
  luồng đợt; Tổng quan tải `/api/jobs` ngay với skeleton; Chạy giữ job cuối (`phase_detail`, volume k/N, exit_class);
  SIGKILL khóa = disabled + tooltip; `window.miguiTest` chỉ với `?test=1`; bỏ fallback runner cũ; `rule.ids` ở form;
  HOSTS_MAX 128. Guards 101/0; browser.mjs 52/0 (Codex) / 38/0 (Fable, stack 1.0.0).
- **Click-through round 2 của Fable** (`d6b3a7f`, `aaad988`): cột Giai đoạn Lịch sử in `[object Object]`; tên volume bị
  cắt "vol-src-…"; hàng đợi ngược thứ tự sau khi đợt xong; nút SIGKILL giữ "— khóa"; câu chữ dialog Rollback; caption
  bảng IP lặp ba lần; pill state bị cắt; thẻ hoạt động gần đây cao 130 px; bảng Instance tràn 47 px ở 1440 px.
- Giới hạn còn lại: root ephemeral chưa nằm trong mọi tổng GB/volume của UI.

### Sửa sau gate lab và click matrix 22/11

Gate lab trên hai cloud 2026.1 (LVM/LIO iSCSI → Ceph) tìm ra lỗi tool sau. Chi tiết từng lượt và các lỗi phía test: [RELEASE-1.0.0.md](RELEASE-1.0.0.md) mục 5.

- **Rollback nhận câu "đã xóa" của OSC 10.x** (`d06a9a7`): OSC 10.5 báo `No Server found for <id>`. `GONE_RE` cũ không khớp, nên rollback chờ hết 600 s rồi mới dừng.
- **SIGINT khi chạy nền từ script** (`8cc5eae`): job nền của bash bỏ qua SIGINT và Python giữ nguyên. `migrate` và tiến trình con nay nhận lại SIGINT.
- **`kill -INT` vào PID của `migrate` hủy được cả tiến trình con** (Astra review cuối): trước đây chỉ Ctrl-C (gửi cả nhóm tiến trình) mới tới con; tín hiệu gửi riêng vào PID cha thì con chạy tiếp tới hết. Selector nay chuyển SIGINT cho con. SIGINT đầu tiên hủy run; các SIGINT sau bị bỏ qua để không cắt ngang việc dọn dẹp (tháo clone, giữ state). SIGKILL vẫn là cách dừng cứng.
- **Volume đơn nhả khóa state khi xong run** (Astra review cuối), như VM.
- **blocksync bị dừng khi run bị ngắt** (`5785f47`, `c3b7621`, `ed739b4`, `a573fc9`): SIGINT gửi riêng vào `migrate` không tới blocksync, nên blocksync có thể ghi tiếp vào đích trong lúc tool tháo clone. `proc.run_stream` nay dừng blocksync trên mọi đường thoát bất thường (SIGINT, lỗi callback): gửi SIGINT, quá 30 s thì SIGKILL, chờ thu hồi xong mới cho dọn dẹp tiếp. Một SIGINT đến giữa lúc đang dừng được giữ lại và chỉ ném ra sau khi blocksync đã chết. Kiểm trên lab: SIGINT vào PID `migrate` lúc presync live thì trả 130, không còn blocksync hay tiến trình con, nguồn chưa bị dừng.
- **Handler SIGINT chỉ sống trong một lần chạy** (`c3b7621`, `ed739b4`): `cli.sigint_scope()` cài handler hủy cho `cli.main`/`single.main` rồi trả lại handler cũ cho caller cùng process; ngoài main thread thì không đụng tới signal.
- **Rollback lấy khóa host trước khi tra state** (`c685914`, có từ 7.3): migrate chạy lại tạo lại thư mục state, chưa có `server.json`. Rollback chen vào lúc đó báo sai `no state for <vm>` thay vì "đang có migrate khác chạy". Tóm tắt A5 nay hiển thị trong lúc giữ khóa. `--dry-run` vẫn không lấy khóa.
- **Tài liệu mã thoát** (`c685914`): `migrate migrate <vm>` gặp state đã cutover thì ghi `SKIP(state-exists)` kèm lệnh rollback và trả 1. Mã 4 chỉ xảy ra khi chính tiến trình con gặp state đó.
- **Gate R1, R3, `MIG_ISCSI_IP`** (`663a31b`, `5ee3ac5`, `5fefbf4`): openrc có `read` thì báo đúng dòng; volume khóa state trước khi kiểm volume-map; Doctor FAIL khi `MIG_ISCSI_IP` trống hoặc không thuộc host; cài UI chép DEPLOY.md; hướng dẫn nâng cấp tại chỗ.

Click matrix của Codex Sol bấm từng nút và option của UI qua Edge headless, Astra review 3 vòng. Lỗi UI tìm được:

- **Ma trận vòng 1** (`d146aab`, `8572e3e`): volume type `__DEFAULT__` bị từ chối; ô giới hạn live nhận giá trị ngoài biên; nút Quay lại về sai màn; ghi chú READY hứa sai; focus không trở về sau dialog rollback; Esc ở Lịch sử; job volume không ghi option hiệu lực.
- **Astra vòng 2** (`c9b8931`, `b1bb1f5`): option hiệu lực đọc từ `options.json` nay được scrub như mọi đường khác; mọi validator neo bằng `\Z`, không còn lọt ký tự xuống dòng; `defaultMode` đọc `MIG_DEFAULT_OPTS` như CLI; `badInput` của ô số; Plan kiểm lại giới hạn live.
- **Ma trận vòng 3** (`c652ddb`, `f75b88a`, `549e572`): skip link focus vùng chính thay vì nhảy về Tổng quan; "Theo dõi →" ở Tổng quan hiện đúng job đã chọn; Tổng quan xếp job gần đây theo thời gian.
- **Astra vòng 3** (`c685914`): job được theo dõi không còn che job đang chạy. Màn Chạy báo job nào đang chạy và có nút "Xem job đang chạy". Hàng đợi và nút dừng thuộc đúng đợt của job đang xem. Link Hàng đợi và đăng xuất bỏ ghim. Focus sau dialog chỉ chờ nút bị khóa khi người dùng chưa chuyển đi. Esc khi đang mở ô chọn nhiều chỉ đóng ô đó, không đóng chi tiết Lịch sử phía sau.
- **Astra review cuối**: nút "Dừng sau con hiện tại" chỉ gửi tới đợt đang hiển thị; nếu đợt vừa đổi mà màn chưa cập nhật thì từ chối và vẽ lại. Bấm link Hàng đợi khi đang ở màn Chạy bỏ ghim và vẽ lại ngay. Nút "Xem job đang chạy" chuyển focus về tiêu đề job. Nút mở dialog bị xóa khỏi trang thì focus về tiêu đề màn. Câu "không ghi nhận option" không còn quy hết cho bản < 7.3.
- **Tài liệu** (`fa8d1db`): DEPLOY 2.1 hướng dẫn OpenStack CLI/SDK: nằm trong venv, cài theo bộ đã test hoặc theo upper-constraints của release.

### Tài liệu hợp nhất và hiệu chỉnh so với tài liệu cũ

- **F1** Thay ví dụ production dùng `--keep-source-ports --verify none` bằng verify bật và port nguồn được tháo.
  Đưa năm option rủi ro vào mục ngoại lệ có điều kiện, nêu người chấp thuận và RPO.
- Một bộ README/DESIGN/GUIDE/DEPLOY/RUNBOOK/UI/DEVELOPING/RELEASE thay hai bộ hướng dẫn và các design delta.
  Chuyển 20 tài liệu vào `archive/docs/`, giữ nội dung lưu trữ; REVIEW ở root.
- Cài từ wheel + requirements-host; UI cài riêng; có venv theo version, audit +a, hook notify và downgrade rehearsal.
- Làm rõ chạy lại: VM trước cutover tự dọn rồi chạy mới (A6); sau cutover không resume. Volume đơn chưa A6.
- Sửa “rollback không hỏi”: prompt tên/ident, non-TTY thiếu yes trả 2; dry-run JSON có summary.
- Sửa “state tương thích hai chiều, hai bản chạy song song”: không dùng binary cũ để xử lý tùy ý state owner-mark/root
  BFV mới, không chạy song song để lách khóa host.
- Sửa “blocksync giống từng byte archive”: A1 khác reference. Sửa “WWN mất sau detach”: giữ `.detached.json`.
- Sửa “cold tạo volume trong downtime”: precreate trước t-stop. Root ephemeral mặc định thành BFV; cờ image là opt-out.
- Sửa “SG có sẵn không so rule”, “chỉ so policy server group”: so rule, WARN thiếu/thừa/HALF-MADE COPY; group rules
  khác gây FAIL; nhánh client không nhận rule vẫn WARN và fallback.
- Sửa “project map không đọc được chỉ WARN”: lỗi dừng. Precedence có ngoại lệ SYS_PY và các tầng AZ/type; volume đơn
  nhận cả volume-az. Plan chỉ đọc cloud nhưng có thể ghi lại export local.
- Làm rõ RBD access: chỉ rbd ls; write-check có giới hạn discard/delete cần kiểm ngoài.
- Sửa mô hình UI token-file/public TLS/runner không network/7 message/khóa riêng: accounts, loopback-only, runner
  cần network/devices, 9 message, dùng MIG_LOCK chung. Phiên plan/batch có touch, revocation không bao phủ tức thời
  mọi background stream; Doctor gate không chặn xử lý sự cố.
- Sửa mô tả UI kế hoạch tuần tự và map đọc theo kiểu bỏ qua lỗi: plan 4 luồng mặc định runner, digest conf/maps trước
  run; digest chỉ 1 MiB/file và không phải snapshot nguyên tử. CSV theo trang; rerun cần plan/xác nhận mới.
- Làm rõ marker t-stop không chứng minh stop thành công; migrated không chứng minh ACTIVE; mọi exit trước cutover
  không nhất thiết 3, mọi batch có FAIL không nhất thiết 5; API timeout 124 được ánh xạ theo ngữ cảnh.
- Ghi đúng phạm vi còn thiếu: Glance REST chưa filter region; rollback discovery và vài preflight lookup chưa
  fail-closed toàn bộ; E1 crash-at-marker chưa làm. Không biến docstring mục tiêu thành bảo đảm đã triển khai.

## [7.3.0] — 2026-11-20 · branch `feature/7.3-cli-json` (from main `dfd54da`) · review Fable (R1 fixed) + gate lab ĐẠT 200/0 regression (e2e 50, live 26, newip 26, extras 30, p4feat 24, p6feat 27, failure-injection 17) + v73feat 20/0 — TEST-REPORT-20261120-v73.md · unit + parity 570/0

What the CLI needs before a UI can drive it, per [DESIGN-V7-3-CLI.md](DESIGN-V7-3-CLI.md) (sections 1–8, security
rules SEC-1…SEC-17 of section 11). Without the new flags and commands the behaviour is the 7.2 one, byte for byte
(parity), except the D3 line of an ephemeral VM (section 8) and two additions the design asks for: the state file
`options.json` / `rollback.json` and one more `doctor` line. Guide: [GUIDE-PYTHON.md](GUIDE-PYTHON.md) section 4d.
Test: `bash tests/unit/run.sh --parity` (new `v73unit.py`, one case per section and per SEC rule); lab gate:
TEST-REPORT-20261120-v73.md.

### Added

- **1** `options.json` in every migrate/volume state directory, next to the unchanged one-line `options`: the
  command as typed (`migrate migrate <ident> … --yes`), `effective` = every option with its value and its origin
  (`from`: `cli`, `default`, `MIG_DEFAULT_OPTS`, `conf`, `az`, `source`, plus `detail`), the conf whitelist
  (`MIG_DEFAULT_OPTS`, `MIG_AZ`, `MIG_VOLUME_AZ`, `AZ_TYPE_MAP`, `RBD_POOL_MAP`, `RBD_POOL`, `PROJECT_MAP_FILE`,
  `FLAVOR_MAP_FILE`, `SRC_PROJECT`, `DST_PROJECT`), version, start time. A finished rollback writes `rollback.json`
  into the `.rolled-back.<epoch>` directory. Both 0600, never overwritten.
- **2** `migrate plan … --json`, `migrate plan volume … --json`, `migrate doctor --json`: one JSON object on stdout,
  the text on stderr. A plan row carries the command `migrate` would run, the effective options, the preflight
  checks (`level`, `text`) and their counts, its log path.
- **3** `migrate list [selectors] [--volumes [--available|--attached|--all]] [--json]`: the selection of plan/migrate
  without action, with host, AZ, flavor, image, boot (`bfv`/`ephemeral`), networks, volumes and the state directory
  of each VM (or the volumes with what they are attached to). 3 openstack calls for a host.
- **4** `migrate inventory [--count [--host H]] [--json]`: hosts, projects of both clouds; compute/volume zones,
  flavors, volume types of the destination.
- **5** `migrate status [--all] [--json]`: live, plan-only and rolled-back state directories, batch logs, running
  migrate processes. No cloud call.
- **6** `--dst-host <hypervisor>`: `server create --host` (compute API 2.74, admin). Preflight FAILs before anything is
  stopped when the host is not a nova-compute host, is down/disabled, sits in another zone than `--az`, or the
  destination token lacks admin; without `--az` the host's zone becomes the AZ (FAIL when
  `AZ_TYPE_MAP` maps that zone: pass `--az` or `--volume-type`, review R1). Command line only (dies in
  `MIG_DEFAULT_OPTS`). `options` gets ` DST_HOST=<h>` only when it is used. `doctor` says whether the destination
  Nova reaches 2.74.
- **7** `migrate migrate|volume … --progress-json`: `@@ {"event": …}` lines on stdout (phase, copy, verify,
  blocksync, pass, t_stop, downtime, result), copy/blocksync at most one per second.
- **Security (section 11)**: every JSON goes through `jsonout.emit_json`, which refuses credential-like keys and the
  openrcs' secret values (dies, prints nothing); the JSON commands use read-only clouds (`list`/`show`/`token issue`
  only); `list`/`inventory`/`status` write nothing; selector values and `--vm-file` are bounded; `status` never
  follows a symlink out of its roots.

### Changed

- **8 (D3b)** Ephemeral VM: the Glance snapshot, download and upload count as copy time:
  `downtime breakdown: total 213s, copy 107s (glance 107s), verify 0s, control-plane 106s`.
- `migrate doctor` prints one more line: `OK    destination Nova API N.M >= 2.74 (--dst-host usable)` (or INFO).

### UI

Branch `feature/7.3-ui` (from `feature/7.3-cli-json`) · phase 1 (code + simulated harness); not deployed yet.
Design: [DESIGN-UI.md](DESIGN-UI.md) (19–20/11). Install and use: [migui/deploy/DEPLOY-UI.md](migui/deploy/DEPLOY-UI.md).
Test report: TEST-REPORT-20261120-ui.md.

- **migui** (`migui/`, standard library only): a web UI that drives the CLI above and nothing else. Two processes:
  `migui-web` (user `migui`, 127.0.0.1:8787 only, login token, one session, CSRF checks, CSP without inline script,
  audit `/var/log/migui/audit.jsonl`) and `migui-runner` (root, unix socket `/run/migui/runner.sock` 0660
  root:migui, SO_PEERCRED, 9 messages, argv lists only, one migrate/volume/rollback at a time, scrubbed output, job
  records `/var/lib/migui/jobs/<id>.json` read-only once ended).
- Screens of DESIGN-UI 14: Doctor (must be green first), Instance (source host/project → list → quick search →
  tick → destination AZ/host → options → plan), Volume, Xác nhận (the verbatim commands with their effective options
  and source badges; type the number of VMs, or the name for one VM / `--keep-source-ports` / `--ip <addr>` /
  `--in-use` / rollback), Hàng đợi / Đang chạy (live log over SSE, progress from `@@` lines, downtime clock, two-step
  cancel), Lịch sử (jobs + `migrate status --json`, re-run with the same options, diff two jobs, rollback).
- `migui/deploy/`: `migui-runner.service`, `migui-web.service` (hardened), idempotent `install.sh`
  (`--uninstall`, `--purge`), `DEPLOY-UI.md`.
- Tests: `tests/ui/` (suite `ui` of `tests/unit/run.sh`): the 10 security tests of DESIGN-UI 10 and the functional
  flows against a real runner + web and a fake `migrate` replaying the simulated clouds of `tests/unit`. The SEC-7
  grep (`shell=True`, `os.system`, `eval(`, `exec(`) now covers `migui/` too.
- Deviations from DESIGN-UI, with reasons, in DEPLOY-UI.md 6 and the test report: the runner unit cannot have
  `PrivateNetwork`/`ProtectSystem=strict` (migrate inherits them); the destination project is shown, not chosen (the
  CLI takes it from `PROJECT_MAP_FILE` only); the web reads the token at each login instead of once at start (a
  runner restart rotates it).

## [7.2.0] — 2026-11-19 · nhánh `feature/7.2-audit-fixes` (từ main `e4c419b`) · review Fable + gate lab ĐẠT 200/0 (e2e 50, live 26, newip 26, extras 30, p4feat 24, p6feat 27, failure-injection 17 — TEST-REPORT-20261119-v72.md) · unit + parity 460/0

Sửa toàn bộ phát hiện của [AUDIT-20261119.md](AUDIT-20261119.md) theo [DESIGN-V7-2.md](DESIGN-V7-2.md) (mã A1…D4
giữ nguyên). Chỉ app Python; bản bash trong `archive/` không đổi. Hướng dẫn: [GUIDE-PYTHON.md](GUIDE-PYTHON.md)
mục 4c. Test: `bash tests/unit/run.sh` (unit + parity, mục 5.3 của [DEVELOPING-PYTHON.md](DEVELOPING-PYTHON.md));
gate lab: TEST-REPORT-20261119-v72.md.

### Đổi hành vi

- **A1** Cold: tạo mọi volume đích **trước** khi tắt VM nguồn (như live). Lỗi phía đích (quota, type, pool, caps)
  dừng run khi VM nguồn còn chạy. Log mới `destination volumes created; stopping the source now`; các lệnh
  `volume create`/`rbd info` nay đứng trước `stop source VM`. Size lấy từ `volumes.json`.
- **A4** Tên VM khớp VM ở **nhiều project** (không chỉ trong một project): mỗi VM `SKIP(ambiguous-name: use the ID)`.
- **B1** Sau khi tháo port, chỉ câu trả lời "not found" mới là Nova đã xóa port; lỗi khác hỏi lại (≤ 60 lần) rồi
  dừng `cannot read port <id> after detach: …`. Rollback gắn lại `source-ports` ∪ `port-ids.json`.
- **B2** Đường ephemeral: endpoint Glance theo `OS_INTERFACE` của openrc đích (mặc định `public`, trước ghim
  `internal`); đọc `OS_CACERT`/`OS_INSECURE`; token mới trước POST, PUT, PATCH.
- **B3** `detect_boot` đối chiếu trường `image` của server; mâu thuẫn thì dừng `cannot tell the root disk of …`.
- **B6** Image property của volume chỉ chép allowlist khách (như ephemeral): không còn `os_hash_*`, `os_hidden`.
- **C2** `--max-passes` ≥ 1, `--delta-limit` ≥ 0 (`… takes a whole number >= 1, not '0'`).
- **C5** Tên VM có `/` (hoặc `.`/`..`): dừng, chạy bằng ID.
- **C7** Preflight không còn `systemctl start iscsid`: chỉ kiểm, FAIL kèm `INFO start it: systemctl enable --now iscsid`.
- `flavor show` của preflight đọc `-f json` (cùng một lệnh, cùng dòng OK/FAIL) để biết vCPU/RAM cho quota.

### Thêm

- **A2** Preflight quota **cả hai cloud** và dung lượng đích (bổ sung 19/11 tối): nguồn `volumes`/`gigabytes`
  (+ per-type) cho clone tạm; đích `instances`, `cores`, `ram`, `volumes`, `gigabytes` (+ per-type của type đã
  chọn), `ports`, `secgroups`, `secgroup-rules`, `server_group_members`; IP trống trong allocation pool khi
  `--ip new`; pool Ceph qua `ceph df detail` (FAIL/WARN <10%/INFO khi thiếu caps). Dòng `  OK    source quota: …`,
  `  OK    destination quota: …`, `  FAIL  destination quota: <res> need N, M free (used U of L)`. Cloud không trả
  lời quota: WARN, không chặn. `migrate volume` kiểm phần volume.
- **A3** Floating IP: `ports.json` có `floating_ips` (khi có), `plan` in `  fip   <addr> on <mac>`, preflight WARN
  "not migrated", rollback gắn lại floating IP mất liên kết vào port giữ chỗ.
- **B2** Preflight ephemeral kiểm chỗ trống `/var/lib/mig-migrate` so với disk của flavor, `OS_CACERT` nạp được
  và image endpoint của `OS_INTERFACE` (review 19/11: trước đó hai việc sau chạy sau khi đã tắt nguồn).
- **B4** `--ip <addr>` phải là IPv4 thật; preflight FAIL `IP <addr> is not in subnet <cidr> of <net>`.
- **C6** `--volume-az ZONE` / `MIG_VOLUME_AZ`: AZ Cinder của volume đích khi khác tên AZ compute. Header preflight
  ` volume-az=<x>`, file state `options` thêm `VOLUME_AZ=` cuối dòng.
- **C8** Rollback tìm server đích theo tên khi `dst-server-id` rỗng (Nova tạo nhưng client timeout): đúng 1, tạo sau
  `t-stop` thì xóa.
- **D2** Lệnh `migrate doctor` (chỉ đọc) và `python -I -m migratetool.brick --check`.
- **D3** Cuối mỗi migrate: `downtime breakdown: total Ns, copy Ns, verify Ns, control-plane Ns`.
- **D1** Harness unit + parity vào repo: `tests/unit/` (`run.sh`, stdlib), thêm `v72unit.py` (mỗi mã ≥ 1 case).

### Sửa

- **B5** Rollback không dừng khi `image list`/`volume list` phía nguồn lỗi (WARN rồi làm tiếp: unlock, gắn port,
  bật VM); clone sót đi qua `drop_volume`.
- **C1** Escape tên VM trong mọi `server list --name ^<tên>$` (tên có `.`).
- **C3** Dọn SG copy khi lỗi: xóa hết rule trước rồi mới xóa group (tránh `in use`).
- **C4** File lỗi brick nằm trong thư mục state của run; `volume show` của rollback không còn file `.err` chung.
- **D4** Phiên bản `7.2.0`.

## [7.1.0] (đang làm: `7.1.0.dev1`) — 2026-11-18 · merge main 19/11 · review + gate lab ĐẠT 183/0 (e2e 50, live 26, newip 26, extras 30, p4feat 24, p6feat 27 — TEST-REPORT-20261118-v7.md mục "Gate lab 7.1")

Phase P6 của [DESIGN-V7.md](DESIGN-V7.md): port, security group và server group ở đích giống nguồn. Chỉ app
Python; bản bash trong `archive/` không đổi. Hướng dẫn: [GUIDE-PYTHON.md](GUIDE-PYTHON.md) mục 4b (có phần chuyển
từ 7.0). Test: TEST-REPORT-20261118-v7.md mục Phase 6.

### Đổi hành vi (mặc định mới)

- **Security group**: mỗi SG trên port nguồn → `<AZ đích>-<tên>` trong project đích. Có rồi thì dùng luôn (không so
  rule); chưa có thì `migrate` tạo trước khi tắt VM nguồn và chép đủ rule (xóa 2 rule egress Neutron tự thêm, copy
  đệ quy SG được trỏ qua remote group, chống vòng lặp). Bỏ cách tìm SG cùng tên. `--security-group` gõ tay vẫn
  thắng, tên nguyên văn. Rollback giữ các SG này.
- **Server group**: VM nguồn trong group thì đích luôn dùng/tạo `<AZ đích>-<tên>` cùng policy, không cần flag.
  7.0 dùng chữ cứng `az-` và chỉ khi `--keep-server-group`. ⚠ Group `az-*` cũ ở đích không được dùng lại.
- `<AZ đích>` = `--az` > `MIG_AZ` > AZ compute duy nhất của đích. Đích từ 2 AZ trở lên mà không có cả hai thì
  preflight FAIL.
- ⚠ **BREAKING — bỏ `--keep-server-group`** (user chốt 19/11). Gõ cờ này thì app dừng như mọi option lạ:
  `ERROR: unknown option --keep-server-group`. Nằm trong `MIG_DEFAULT_OPTS` thì dừng ngay lúc đọc conf:
  `ERROR: MIG_DEFAULT_OPTS: unknown option '--keep-server-group' (MIG_DEFAULT_OPTS="…")`. Script, cron, conf thời
  7.0 phải xóa cờ trước khi lên 7.1; hành vi không cờ của 7.1 chính là việc cờ đó từng làm (tên `<AZ>-` thay `az-`).
  `migrate volume <vol> --keep-server-group` cũng ra `unknown option` (7.0: "cannot apply").

### Thêm

- **Port fidelity**: port đích chép port security tắt (`--disable-port-security`, không SG),
  allowed-address-pairs (`--ip new` thì bỏ kèm WARN), description, dns_name (khi đích có `dns-integration` và
  `dns_name` đúng là hostname). Port giữ chỗ phía nguồn cũng mang pairs, port security, description.
  Bỏ preflight FAIL `has allowed-address-pairs (not handled)`. Nhiều fixed IP/port vẫn từ chối.
- Opt-out: `--no-copy-secgroup` (về đúng hành vi 7.0: SG cùng tên có sẵn), `--no-server-group`.
- State: file `dst-security-groups`; `ports.json` có thêm `description`/`dns_name` khi port có giá trị (port
  thường: file giống hệt trước).
- Port tắt port security mà gõ `--security-group`: dừng ngay, nêu xung đột.

### Sửa

- Tiến trình con brick chạy `python -I -m migratetool.brick` (như tiến trình con selector từ P5): một cây nguồn
  `migratetool/` ở thư mục hiện tại không che được package đã cài.

## [v7] — 2026-11-18 · nhánh `feature/v7-python-app` (fork từ v6 `6b6c48b`) · phát hành 7.0.0 (`a4ecb38`)

Viết lại thành **một app Python `migrate`**, thay hẳn `migrate-vm.sh` + `migrate-batch.sh` (user chốt
18/11). Đây là bản **port giữ nguyên hành vi**: cùng state, cùng conf, cùng cách gọi hạ tầng (vẫn
subprocess `openstack`/`qemu-img`/`rbd`, chưa SDK), cùng quy tắc nghiệp vụ. Thiết kế:
[DESIGN-V7.md](DESIGN-V7.md). Test: TEST-REPORT-20261118-v7.md.
Hướng dẫn: [GUIDE-PYTHON.md](GUIDE-PYTHON.md), [DEPLOY-PYTHON.md](DEPLOY-PYTHON.md),
[WORKFLOW-PYTHON.md](WORKFLOW-PYTHON.md), [DEVELOPING-PYTHON.md](DEVELOPING-PYTHON.md).

### Test

- Regression gate trên lab: 4 bộ cũ nguyên văn, chỉ sed đổi lệnh và thêm `--yes`, cộng `blocksync-unit`:
  - cộng dồn P1–P3: **140/140**;
  - chạy lại cả 4 bộ trên build P4: e2e 50/0, live 26/0, newip 26/0, extras 30/0.
- Feature V3–V6 trên CLI mới (`p4feat.sh`): **24/24**. Lượt đầu có 10 FAIL, cả 10 đều oan và đã chẩn đoán
  trong report.
- Harness giả lập so với bản bash trên 6 chiều (stdout, stderr, mã thoát, file state, chuỗi lời gọi ra
  ngoài, world cuối):
  - P1 44/44 + 4/4 rollback chéo;
  - P2 65/65;
  - P3 86/86;
  - P4 141/141;
  - P5 **153/153** (141 cũ + 12 mới cho `plan volume`).
- Unit: P1 23/23, P2–P3 25/25, P4 65/0, P5 48/0 (Linux và Windows).

### Thêm

- App `migrate`:
  - package `migratetool/` + `pyproject.toml`;
  - cài bằng `pip install --no-build-isolation .` vào `/opt/mig-venv`;
  - lệnh `plan`, `migrate`, `volume`, `rollback`.
- **CLI thống nhất theo selector**:
  - chọn bằng `--vm`/`--host`/`--project`/`--vm-file`, positional là cách viết gọn của `--vm`;
  - 1 VM hay N VM đi cùng một đường: bảng `Selected`/`Plan`/`Summary`;
  - mỗi VM là một tiến trình con, có project riêng và log riêng ở `/var/log/mig-batch/<thời gian>/`;
  - không còn lệnh `batch`, không còn dấu `--`.
- **Luôn hỏi `Type yes`** trước khi migrate, kể cả 1 VM, trừ khi có `--yes`. stdin không phải terminal mà
  thiếu `--yes` thì app dừng.
- **Volume theo đợt** gộp vào `migrate volume`: `--project p --available|--attached|--all`, `--vm a,b`.
  Một volume vẫn là `migrate volume <volume> [--in-use]`.
- **`migrate plan volume <selection>`** (P5): dry-run của volume theo đợt, thay
  `migrate-batch.sh --volumes … --plan-only`.
  - Volume available ra `READY`, không có plan riêng, giống batch.
  - VM của `--attached`/`--vm` chạy preflight `--volumes-only`.
  - Không hỏi, không đổi gì.
- `blocksync.py` đóng gói trong package, giống từng byte bản bash, chạy bằng `SYS_PY -I`. `brick_attach.py`
  thành module `migratetool.brick`.
- Gõ kiểu cũ thì app dừng kèm hướng dẫn lệnh mới: `batch`, `migrate-volume`, `--`, `--plan-only`, `--volumes`.

### Đổi hành vi (đã duyệt)

- **Thay thế**: 4 file của bản bash chuyển vào [archive/](archive/README.md), giữ nguyên byte. Bản bash
  dừng ở commit `6b6c48b`. Tài liệu bản bash (GUIDE/DEPLOY/WORKFLOW/DEVELOPING) giữ nguyên; tài liệu app
  ở các file `*-PYTHON.md` mới.
- `migrate migrate` luôn hỏi xác nhận (bản bash chạy thẳng). Script và cron phải thêm `--yes`.
- ⚠ **Một VM cũng tự scope `SRC_PROJECT`/`DST_PROJECT` theo project của VM.**
  - Bản bash chạy lẻ mà quên đặt env thì VM đích rơi vào project của openrc.
  - `PROJECT_MAP_FILE` giờ áp cho cả lệnh 1 VM.
  - Env `SRC_PROJECT`/`DST_PROJECT` bị project của VM đè khi chạy theo selector.
- State, log và lệnh rollback dùng **đúng chữ đã gõ**: `--vm sg-vm1` thì rollback `sg-vm1`. Riêng
  `--host`/`--project` vẫn theo ID như batch.
- Đợt mà mọi hàng đều SKIP thì thoát mã 1; batch thoát 0. `plan` một VM đã có state ra
  `SKIP(state-exists)`, không export đè lên state cũ.
- `migrate plan` in preflight ra màn hình. Batch chỉ ghi vào log.
- Application credential dùng được qua selector khi VM thuộc đúng project của token. Batch bash từ chối thẳng.
- Conf được đọc chặt: chỉ `KEY=VALUE`. Gặp dòng bash phức tạp thì app dừng kèm số dòng, nên phải soát conf
  production trước khi chuyển.
- stderr của tiến trình con gộp vào stdout của `migrate`.
- Tiến trình con chạy `python -I -m migratetool.single` (P5): thư mục `migratetool/` của repo nằm ở thư mục
  hiện tại không che được package đã cài.
- Message lỗi rõ hơn. Các chuỗi mà người, test và script đang bám vào thì giữ nguyên văn
  ([DEVELOPING-PYTHON.md](DEVELOPING-PYTHON.md) mục 4). Blocksync in rác thay cho JSON thì app dừng kèm nội
  dung đó, thay vì lỗi jq khó hiểu như bash.

### Sửa (bug của bản bash)

- Rollback khi có từ 2 clone `mig-clone-*` sót cùng tên: bash truyền chuỗi nhiều dòng thành 1 đối số nên
  không dọn được. App dọn từng clone.
- Server property chứa `\` hoặc tab bị `@tsv` của bash làm hỏng khi chép sang VM đích. App chép nguyên văn.
- `--max-passes`/`--delta-limit` không phải số nguyên: bash lặp pre-sync **vô hạn**. App dừng ngay, ví dụ
  `ERROR: --max-passes takes a whole number, not 'x'`.

### Tài liệu

- Mới: GUIDE-PYTHON, DEPLOY-PYTHON, WORKFLOW-PYTHON, DEVELOPING-PYTHON, archive/README.
- Sửa: README (bảng trỏ bản Python hiện hành / bản bash trong archive, lệnh cài mới), `migrate.conf.example`
  (tên lệnh `migrate`, cách app đọc file, ví dụ file map project).

### Commit

- Code: P1 `e0a41a1`, P2 `1fb2e88`, P3 `f584bd5`, P4 `8a48c45`, P5 `5e67653`.
- Thiết kế: `1f25651`, `fc77812`, `18b4f75`; P6 port fidelity `5b020e2` (thiết kế, chưa code).
- Report: `3a28aa8`, `b5f1979`, `9b47e50`, `0d58db0`, cùng commit report P5.
- Archive: `b6f0e2f`. Docs: commit docs P5.

## [v6] — 2026-11-18 · nhánh `feature/v6-batch-hint` (fork từ v5) · test xong: 6/6 lab + 8/8 giả lập

Chỉ đổi message lỗi của `migrate-batch.sh`: gõ `plan`/`migrate`/`rollback` như lệnh con (thói quen
từ migrate-vm.sh — 2 lỗi thật trên production 18/11) → die kèm hint chỉ cả hai cách gõ đúng.
Logic, exit code, các message khác giữ nguyên văn; `migrate-vm.sh` không đổi (md5 `5706e763`) nên
gate vòng 7 của v5 còn hiệu lực. Thiết kế: [DESIGN-V6.md](DESIGN-V6.md). Test:
TEST-REPORT-20261118-v6.md. Commit: code `aa457dd`, design `9368fe4`.

## [v5] — 2026-11-18 · nhánh `feature/v5-flavor-map` (fork từ v4) · test xong, CHỜ MERGE: feature 16/16, gate vòng 7 = 140/0

Thiết kế: [DESIGN-V5.md](DESIGN-V5.md). Test: TEST-REPORT-20261118-v5.md.
Hướng dẫn: [GUIDE.md](GUIDE.md) mục 2b.12.

### Test

- Feature trên lab: **16/16 PASS**, phủ 12 case của DESIGN-V5 §4.
- Regression gate vòng 7 (5 bộ cũ nguyên văn trên build `0c49e3c`): **đang chạy**, kết quả sẽ ghi vào
  report.
- Harness giả lập: V5 27/27. Không có flag thì output preflight giống build v4 từng byte (3 kịch bản).
  Chạy lại các bộ A/B, C, 2d, 2e đều xanh.

### Thêm

- `--flavor-fallback` (cho `plan`/`migrate`, mặc định tắt) cùng biến conf/env `FLAVOR_MAP_FILE`: file
  map riêng, mỗi dòng `flavor-nguồn flavor-đích`. Khi bật flag, flavor được chọn theo thứ tự:
  1. `--flavor` gõ tay luôn thắng;
  2. đích có flavor cùng tên thì dùng luôn, không tra map;
  3. đích thiếu thì tra map;
  4. không có trong map thì FAIL kèm gợi ý thêm dòng.
- Tool không bao giờ tạo flavor. Flavor map ra phải có sẵn ở đích, nhỏ hơn nguồn thì WARN.
- Bật flag mà `FLAVOR_MAP_FILE` rỗng hoặc dòng map sai dạng thì die.
- Dùng được trong `MIG_DEFAULT_OPTS` và batch passthrough (bộ lọc `VOLPASS` của batch đã biết flag này).
  `migrate-volume` từ chối flag.

### Tài liệu

- GUIDE 2b.12, cùng bảng "`migrate-vm.sh` ↔ `migrate-batch.sh`" ở 2b.6: batch không có lệnh con, gõ
  `migrate-batch.sh plan <vm>` sẽ ra `unknown option plan`.
- `migrate.conf.example`: thêm `FLAVOR_MAP_FILE`.

### Commit

Code: `0c49e3c`. Thiết kế: `141213b`. Docs: commit docs v5.

## [v4] — 2026-11-18 · nhánh `feature/v4-sg-inuse-vol` · test xong, CHỜ MERGE

Thiết kế: [DESIGN-V4.md](DESIGN-V4.md). Test: TEST-REPORT-20261118.md.
Hướng dẫn: [GUIDE.md](GUIDE.md) mục 2b.1, 2b.1b, 2b.9–2b.11.

### Test

- Regression gate (5 bộ test cũ chạy nguyên văn, không sửa dòng nào trong `tests/`):
  - vòng 5 = **140/0** trên build A+B+C (`e4d76d0`);
  - vòng 6 = **140/0** trên build cuối (`51aa611`).
- Feature test trên lab: 4 phase = **64 PASS / 0 FAIL**, phủ đủ 27 case của DESIGN-V4 §4.
- Harness giả lập (không gọi cloud): A/B 27/27, C + batch filter 17/17, §2d 9/9, §2e 28/28.
- Chưa test trên lab, mới phủ bằng harness giả lập:
  - `--keep-server-group` với group có `rules` (`max_server_per_host`);
  - client từ chối `--rule`;
  - microversion bị ghim dưới 2.64 (trả `policies[]`).

### Thêm

- `migrate-volume <volume> --in-use`: copy volume đang gắn vào VM, không stop, không lock, không
  detach. Bản copy lấy từ một clone crash-consistent. Tool in mốc giờ clone (UTC) và ghi vào state
  `clone-time`. Không truyền flag thì volume in-use vẫn bị từ chối như v3, message gợi ý `--in-use`.
  Volume in-use mà multiattach thì bị từ chối. Lab đã chứng minh mốc: dữ liệu ghi trước clone có
  trong bản copy, ghi sau thì không.
- `--keep-server-group` (cho `plan`/`migrate`): VM đích vào server group `az-<tên group nguồn>` ở project
  đích, cùng policy với group nguồn (và cùng rules nếu client nhận `--rule`).
  - Group chưa có thì `migrate` tạo ngay trước khi tạo server; `plan` không tạo gì.
  - Group có sẵn nhưng khác policy, hoặc nhiều group trùng tên: preflight FAIL.
  - Không dùng chung với `--server-group`.
  - State ghi `dst-server-group`; rollback không xóa group.
  - Migrate rải rác nhiều lần (hoặc qua batch) vẫn dồn về cùng một group.
- `MIG_AZ` và `AZ_TYPE_MAP` (conf hoặc env): AZ mặc định và volume type theo AZ, áp dụng khi không truyền
  `--az` / `--volume-type`. CLI luôn thắng. Preflight in nguồn gốc giá trị. Chuỗi AZ → type → pool
  giải thích ở [GUIDE.md](GUIDE.md) mục 2b.1b.
- `MIG_DEFAULT_OPTS` (conf hoặc env): option mặc định, parse trước option CLI nên CLI thắng.
  - `MIG_DEFAULT_OPTS=""` tắt cho một lần chạy.
  - `--keep-server-group` từ defaults gặp `--server-group` gõ tay: bỏ keep kèm INFO, không die.
  - `migrate-volume` chỉ lấy `--volume-type`, `--az`, `--verify`.
  - `--in-use` không bao giờ được lấy từ defaults.
  - Token sai thì die nêu tên biến.
  - Preflight in 1 dòng INFO liệt kê option đã áp.
- `migrate.conf.example`: mẫu đủ mọi biến conf, chia nhóm, mỗi biến có mô tả và ví dụ.

### Sửa

- `migrate-batch.sh`: bộ lọc option truyền xuống cho volume rời giờ coi `--keep-server-group` và
  `--in-use` là option không tham số. Trước đây nó nuốt option kế tiếp, ví dụ `--volume-type X` bị bỏ
  mất.
- Rollback dọn được volume đích mồ côi. `create_dest_volume` ghi `volume-map` ngay khi có ID volume
  (trước bước chờ và check pool RBD), và rollback VM xử lý được volume đang `error` hoặc đã bị xóa.
  Lab: volume đích rơi `error` thì rollback vẫn chạy hết và dọn được; code cũ die ở bước này.

### Tài liệu

- [WORKFLOW.md](WORKFLOW.md): từng bước migrate một VM, cold 10 bước / live 11 bước với option mặc định
  (lệnh thật, state, fail thì sao, thời lượng lab).
- [DEVELOPING.md](DEVELOPING.md): hướng dẫn cho người phát triển tiếp (kiến trúc, state, lớp cấu hình,
  quy ước, quy trình, test, checklist thêm option).
- CHANGELOG.md (file này).
- GUIDE: 2b.1b (chuỗi AZ → type → pool), `MIG_DEFAULT_OPTS` ở 2b.1, 2b.9 `--in-use`,
  2b.10 `--keep-server-group`, 2b.11 ví dụ v4.
- DEPLOY: sửa chỗ ghi sai vị trí WWN và `brick-attach.err` (mục 9), thêm phần chưa test của v4 (mục 10).

### Commit

Code:

- `b46e51b`: in-use + keep-server-group.
- `e4d76d0`: MIG_AZ/AZ_TYPE_MAP + fix batch.
- `af151e2`: volume mồ côi.
- `51aa611`: MIG_DEFAULT_OPTS + `migrate.conf.example`.

Thiết kế: `6eb2d9e`, `30b9da2`, `e105dd1`, `7bed2c8`. Report: `2111b1d`.
Docs: `1353cc5`, `1641270`, `797089b`, cùng commit docs chốt v4.

## [v3] — 2026-11-17/18 · nhánh `feature/batch-migrate` · test xong, CHỜ MERGE

Thiết kế: [DESIGN-V3.md](DESIGN-V3.md). Test: TEST-REPORT-20261117.md (P1 + P2).
Hướng dẫn: [GUIDE.md](GUIDE.md) mục 2b.

### Thêm

- File cấu hình `/opt/mig/migrate.conf` (đổi đường dẫn bằng `MIG_CONF`). Thứ tự ưu tiên: CLI > env >
  conf > mặc định.
- `SRC_PROJECT` / `DST_PROJECT`: chạy mỗi phía trong project của VM. Bắt buộc password openrc;
  application credential bị chặn bằng message riêng.
- Lệnh `migrate-volume <volume>`: copy một volume available qua clone, nguồn không bị đụng. Kèm
  `rollback <volume>`.
- `--server-group <tên|ID>` (group phải có sẵn ở đích) và `--volumes-only` (cold, chỉ copy volume).
- `migrate-batch.sh`:
  - chọn VM theo `--host` / `--project` / `--vm` / `--vm-file`;
  - `--volumes attached|available|all`;
  - plan hết, xác nhận một lần, rồi migrate tuần tự;
  - `PROJECT_MAP_FILE` khi project đích khác tên.

### Đổi hành vi (đã duyệt)

- Tên phía đích theo instance, luôn bật: volume `<vm>-<device>`, port `<vm>-<ip>`. Volume rời giữ
  tên nguồn và mang description `migrated from <id>` làm guard chống copy trùng.
- Keypair thiếu ở đích không còn làm preflight FAIL: tool báo INFO và tạo VM không có `key_name`.

### Sửa

- Security group trùng tên giữa các project (vd `default`): tên được resolve sang ID trong đúng
  project của token (`c862d09`). Phía nguồn, lúc giữa cutover, group không resolve được chỉ WARN.

### Commit

P1: `9e38771`, `c733678`, `2b69692`. P2: `23fb678`. Fix: `c862d09`. Docs: `08e32cf`, `75aaef9`.
Thiết kế: `6d9ac7c` → `4d18796`.

### Test

P1: gate 110/0, feature 22/22. P2: gate 110/0 + 140/0, feature 11 PASS vòng đầu, retest 7/7.
Tìm thấy và đã fix 1 bug thật (SG đa project).

## [Production fixes] — 2026-11-17 · nhánh `main` (HEAD `726d968`)

Sửa sau lần chạy production đầu tiên, cùng vài option theo yêu cầu vận hành. Test ghi ở
TEST-REPORT-20261112.md các mục "Bổ sung 17/11".

### Thêm

- `--keep-source-ports` (`c3b732d`): với `--ip same`, không tháo port nguồn. Đổi lại phải chấp nhận
  rủi ro split-brain nếu ai bật VM nguồn.
- `--az <zone>` (`c2e6b5b`): AZ đích cho cả volume lẫn server. Preflight kiểm AZ ở cả compute lẫn
  volume service.
- Hiển thị % tiến độ copy/verify/blocksync (`726d968`).
- `brick_attach.py` nhận application credential qua `OS_AUTH_TYPE` (`12394da`).

### Sửa

- `fb71cd2`:
  - VM nguồn đã bị lock (stop trả 409): tool tự unlock kèm WARN rồi đi tiếp.
  - Preflight FAIL nếu đích đã có server cùng tên không thuộc state của lần chạy này.
- `40241ba`: lock VM nguồn chỉ sau khi tháo port và snapshot ephemeral xong. Lock của Nova chặn các
  thao tác này với creds không-admin.
- `6174780`: nhiều VM trùng tên trong project nguồn thì tool die sớm, liệt kê ID; chạy lại bằng ID.
- `d209701`: cold mode tôn trọng `--verify none`.

Docs: `eb62117` (cài bằng git clone).

## [v2.1] — 2026-11-13

Test: TEST-REPORT-20261112.md các mục "Bổ sung 13/11": `extras.sh` 30/0,
`powerstore-sim.sh` 30/0, e2e qua multipath 50/0. Mã nằm trong commit đầu của repo `e973d00`.

### Thêm

- `--flavor`, `--security-group`. Server properties và description được copy sang VM đích.
- VM root ephemeral (không có volume): đi đường Glance snapshot, tải về rồi đẩy lên đích bằng REST.
- VM nhiều NIC, mỗi NIC giữ đúng port (NIC phải nằm trên network riêng).
- Nguồn kiểu PowerStore (LIO 2 portal): attach qua multipath bằng `MIG_MULTIPATH=1`.

## [v2] — 2026-11-12

Thiết kế: [DESIGN-V2.md](DESIGN-V2.md). Test: TEST-REPORT-20261112.md,
gồm `blocksync-unit` 8/0, `e2e-migrate` 50/0, `live-migrate` 26/0, `newip-voltype` 26/0.

### Thêm

- `--mode live`: `blocksync.py` sync theo hash từng chunk 4 MiB, pre-sync nhiều lượt trong lúc VM
  chạy, cutover chỉ sync phần delta. Tham số đi kèm: `--max-passes`, `--delta-limit`,
  `--verify readback|full|none`.
- `--volume-type` cùng `RBD_POOL_MAP` (map type → pool RBD).
- `--ip same|new|<addr>` và `--start-source`.
- `lab.sh` để dựng và reset lab.

## [v1] — 2026-11-05

Test: TEST-REPORT-20261105.md: `e2e-migrate` 50/0, `e2e-nova-port` 12/0,
`failure-injection` 17/0. Mã: [archive/migrate-vm-v1-20261105.sh](archive/migrate-vm-v1-20261105.sh).

### Thêm

- Cold migrate VM boot-from-volume, giữ nguyên IP và MAC:
  1. stop + lock VM nguồn, tháo port nguồn;
  2. clone volume, attach clone qua `brick_attach.py`;
  3. `qemu-img convert` sang RBD, rồi `qemu-img compare`;
  4. tạo port cùng MAC/IP và VM đích.
- `plan` (preflight chỉ đọc) và `rollback` (dọn đích, trả VM nguồn).
