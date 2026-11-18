# Changelog

Lịch sử thay đổi của tool (`migrate-vm.sh`, `migrate-batch.sh`, `brick_attach.py`, `blocksync.py`).
Bản mới nhất ở trên. Viết theo tinh thần [Keep a Changelog](https://keepachangelog.com/): mỗi bản ghi
ngày, nhánh, trạng thái, phần **Thêm** / **Đổi hành vi** / **Sửa**, commit chính, thiết kế và báo cáo test.

Repo git bắt đầu từ bản v2.1 (commit đầu `e973d00`, 17/11). Bản v1 và v2 có trước repo nên không có
commit riêng. Mã v1 lưu ở [archive/migrate-vm-v1-20261105.sh](archive/migrate-vm-v1-20261105.sh).

Cách thêm một mục mới cho bản sau: xem [DEVELOPING.md](DEVELOPING.md) mục 5.

## [v4] — 2026-11-18 · nhánh `feature/v4-sg-inuse-vol` · ĐANG TEST

Thiết kế: [DESIGN-V4.md](DESIGN-V4.md). Test: regression gate (5 bộ test cũ chạy nguyên văn) và
feature test (19 case ở DESIGN-V4 §4) **đang chạy, báo cáo sẽ bổ sung** (`TEST-REPORT-<ngày>.md`).
Chưa merge.

### Thêm

- `migrate-volume <volume> --in-use`: copy volume đang gắn vào VM, không stop, không lock, không
  detach. Bản copy lấy từ một clone crash-consistent. Tool in mốc giờ clone (UTC) và ghi vào state
  `clone-time`. Không truyền flag thì volume in-use vẫn bị từ chối như v3, message gợi ý `--in-use`.
  Volume in-use mà multiattach thì bị từ chối.
- `--keep-server-group` (cho `plan`/`migrate`): VM đích vào server group `az-<tên group nguồn>` ở project
  đích, cùng policy với group nguồn (và cùng rules nếu client nhận `--rule`).
  - Group chưa có thì `migrate` tạo ngay trước khi tạo server; `plan` không tạo gì.
  - Group có sẵn nhưng khác policy, hoặc nhiều group trùng tên: preflight FAIL.
  - Không dùng chung với `--server-group`.
  - State ghi `dst-server-group`; rollback không xóa group.
- `MIG_AZ` và `AZ_TYPE_MAP` (conf hoặc env): AZ mặc định và volume type theo AZ, áp dụng khi không truyền
  `--az` / `--volume-type`. CLI luôn thắng. Preflight in nguồn gốc giá trị. Chuỗi AZ → type → pool
  giải thích ở [GUIDE.md](GUIDE.md) mục 2b.1b.

### Sửa

- `migrate-batch.sh`: bộ lọc option truyền xuống cho volume rời giờ coi `--keep-server-group` và
  `--in-use` là option không tham số. Trước đây nó nuốt option kế tiếp, ví dụ `--volume-type X` bị bỏ
  mất.
- Rollback dọn được volume đích mồ côi. `create_dest_volume` ghi `volume-map` ngay khi có ID volume
  (trước bước chờ và check pool RBD), và rollback VM xử lý được volume đang `error` hoặc đã bị xóa.

### Commit

Code: `b46e51b` (in-use + keep-server-group), `e4d76d0` (MIG_AZ/AZ_TYPE_MAP + fix batch), `af151e2`
(volume mồ côi). Thiết kế: `6eb2d9e`, `30b9da2`, `e105dd1`. Docs: `1353cc5` (GUIDE 2b.1b).

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
