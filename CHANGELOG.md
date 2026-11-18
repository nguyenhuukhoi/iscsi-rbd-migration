# Changelog

Lịch sử thay đổi của tool. Từ v7: app Python `migrate` (`migratetool/`). Từ v1 tới v6: `migrate-vm.sh`,
`migrate-batch.sh`, `brick_attach.py`, `blocksync.py`, nay nằm trong [archive/](archive/README.md).
Bản mới nhất ở trên. Viết theo tinh thần [Keep a Changelog](https://keepachangelog.com/): mỗi bản ghi
ngày, nhánh, trạng thái, phần **Thêm** / **Đổi hành vi** / **Sửa**, commit chính, thiết kế và báo cáo test.

Repo git bắt đầu từ bản v2.1 (commit đầu `e973d00`, 17/11). Bản v1 và v2 có trước repo nên không có
commit riêng. Mã v1 lưu ở [archive/migrate-vm-v1-20261105.sh](archive/migrate-vm-v1-20261105.sh).

Cách thêm một mục mới cho bản sau: xem [DEVELOPING-PYTHON.md](DEVELOPING-PYTHON.md) mục 6 (bản bash:
[DEVELOPING.md](DEVELOPING.md) mục 5).

## [v7] — 2026-11-18 · nhánh `feature/v7-python-app` (fork từ v6 `6b6c48b`) · P1–P4 test xong, P5 chờ duyệt

Viết lại thành **một app Python `migrate`**, thay hẳn `migrate-vm.sh` + `migrate-batch.sh` (user chốt
18/11). Đây là bản **port giữ nguyên hành vi**: cùng state, cùng conf, cùng cách gọi hạ tầng (vẫn
subprocess `openstack`/`qemu-img`/`rbd`, chưa SDK), cùng quy tắc nghiệp vụ. Thiết kế:
[DESIGN-V7.md](DESIGN-V7.md). Test: [TEST-REPORT-20261118-v7.md](TEST-REPORT-20261118-v7.md).
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
[TEST-REPORT-20261118-v6.md](TEST-REPORT-20261118-v6.md). Commit: code `aa457dd`, design `9368fe4`.

## [v5] — 2026-11-18 · nhánh `feature/v5-flavor-map` (fork từ v4) · test xong, CHỜ MERGE: feature 16/16, gate vòng 7 = 140/0

Thiết kế: [DESIGN-V5.md](DESIGN-V5.md). Test: [TEST-REPORT-20261118-v5.md](TEST-REPORT-20261118-v5.md).
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

Thiết kế: [DESIGN-V4.md](DESIGN-V4.md). Test: [TEST-REPORT-20261118.md](TEST-REPORT-20261118.md).
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

Thiết kế: [DESIGN-V3.md](DESIGN-V3.md). Test: [TEST-REPORT-20261117.md](TEST-REPORT-20261117.md) (P1 + P2).
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
[TEST-REPORT-20261112.md](TEST-REPORT-20261112.md) các mục "Bổ sung 17/11".

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

Test: [TEST-REPORT-20261112.md](TEST-REPORT-20261112.md) các mục "Bổ sung 13/11": `extras.sh` 30/0,
`powerstore-sim.sh` 30/0, e2e qua multipath 50/0. Mã nằm trong commit đầu của repo `e973d00`.

### Thêm

- `--flavor`, `--security-group`. Server properties và description được copy sang VM đích.
- VM root ephemeral (không có volume): đi đường Glance snapshot, tải về rồi đẩy lên đích bằng REST.
- VM nhiều NIC, mỗi NIC giữ đúng port (NIC phải nằm trên network riêng).
- Nguồn kiểu PowerStore (LIO 2 portal): attach qua multipath bằng `MIG_MULTIPATH=1`.

## [v2] — 2026-11-12

Thiết kế: [DESIGN-V2.md](DESIGN-V2.md). Test: [TEST-REPORT-20261112.md](TEST-REPORT-20261112.md),
gồm `blocksync-unit` 8/0, `e2e-migrate` 50/0, `live-migrate` 26/0, `newip-voltype` 26/0.

### Thêm

- `--mode live`: `blocksync.py` sync theo hash từng chunk 4 MiB, pre-sync nhiều lượt trong lúc VM
  chạy, cutover chỉ sync phần delta. Tham số đi kèm: `--max-passes`, `--delta-limit`,
  `--verify readback|full|none`.
- `--volume-type` cùng `RBD_POOL_MAP` (map type → pool RBD).
- `--ip same|new|<addr>` và `--start-source`.
- `lab.sh` để dựng và reset lab.

## [v1] — 2026-11-05

Test: [TEST-REPORT-20261105.md](TEST-REPORT-20261105.md): `e2e-migrate` 50/0, `e2e-nova-port` 12/0,
`failure-injection` 17/0. Mã: [archive/migrate-vm-v1-20261105.sh](archive/migrate-vm-v1-20261105.sh).

### Thêm

- Cold migrate VM boot-from-volume, giữ nguyên IP và MAC:
  1. stop + lock VM nguồn, tháo port nguồn;
  2. clone volume, attach clone qua `brick_attach.py`;
  3. `qemu-img convert` sang RBD, rồi `qemu-img compare`;
  4. tạo port cùng MAC/IP và VM đích.
- `plan` (preflight chỉ đọc) và `rollback` (dọn đích, trả VM nguồn).
