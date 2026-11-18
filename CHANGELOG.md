# Changelog

Lịch sử thay đổi của tool (`migrate-vm.sh`, `migrate-batch.sh`, `brick_attach.py`, `blocksync.py`).
Bản mới nhất ở trên. Viết theo tinh thần [Keep a Changelog](https://keepachangelog.com/): mỗi bản ghi
ngày, nhánh, trạng thái, phần **Thêm** / **Đổi hành vi** / **Sửa**, commit chính, thiết kế và báo cáo test.

Repo git bắt đầu từ bản v2.1 (commit đầu `e973d00`, 17/11). Bản v1 và v2 có trước repo nên không có
commit riêng. Mã v1 lưu ở [archive/migrate-vm-v1-20261105.sh](archive/migrate-vm-v1-20261105.sh).

Cách thêm một mục mới cho bản sau: xem [DEVELOPING.md](DEVELOPING.md) mục 5.

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
