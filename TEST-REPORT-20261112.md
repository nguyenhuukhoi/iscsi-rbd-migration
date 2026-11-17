# Kết quả test tool v2 (live mode, volume type, chọn IP) — 2026-11-12, bổ sung v2.1 ngày 13/11

## Bổ sung 13/11: multi-NIC, override flavor/SG, server properties, VM ephemeral

`tests/extras.sh` chạy sạch **30 PASS / 0 FAIL** (lần chạy nguyên vẹn cuối, sau 4 lần fail có lý do ở mục sự cố):

| Phần | Đã kiểm chứng |
|---|---|
| A. Multi-NIC + override | demo-vm 2 NIC trên **2 network khác nhau** (vlan120 + vlan121): cả 2 port sang đích đúng MAC/IP, cả 2 IP trả lời sau migrate; `--flavor mig.small2` và `--security-group mig-sg-b` áp đúng lên VM/port đích; `--property env/app` của server đi theo VM; rollback gắn lại đủ 2 port |
| B. VM ephemeral (không volume) | Đường Glance: snapshot 35 s → tải 2,0 GB về 35 s → đẩy lên đích qua REST → **sha512 local khớp os_hash_value của Glance** → VM đích boot từ image, cùng IP, checksum + SSH host key giữ nguyên; rollback xóa image đích, snapshot nguồn tự dọn |

## Bổ sung 13/11 chiều: giả lập nguồn PowerStore (LIO + 2 portal + multipath)

Nguồn lab chuyển `tgtd` → LIO 2 portal (`10.77.0.12/.13`), host chuyển bật multipath; chi tiết
setup + đối chiếu mảng thật: [POWERSTORE-VS-LAB.md](POWERSTORE-VS-LAB.md).

| Test | PASS | FAIL |
|---|---:|---:|
| `powerstore-sim.sh` — attach dm 2 đường, mất đường .13 giữa lúc đọc 10 GB (phát hiện 18 s, đọc xong rc=0, hồi 2 đường 11 s), read-only, detach sạch | 30 | 0 |
| `e2e-migrate.sh` với `MIG_MULTIPATH=1` — migrate + rollback đầy đủ qua multipath | 50 | 0 |

Run e2e multipath ĐẦU TIÊN ra 43/3: cả 3 FAIL cùng gốc **môi trường** — một backstore LIO cũ còn
giữ handle sau chuỗi attach/terminate lỗi, compute đọc/ghi một nơi, LV thật một nơi (hash 2 đường
lệch, LV 0% allocated). `qemu-img compare` không bắt được (so cùng device); **assert checksum
trong guest của e2e bắt được** — đúng lớp rủi ro "LUN không trỏ nơi mình nghĩ" của PowerStore.
Dọn target + dựng lại demo-vm thì run 2 ra 50/0. Bài học kèm theo: đổi target stack (tgtd→LIO) làm
connection_info trong BDM của Nova thành rác — hard reboot không refresh, shelve vấp volume error;
và `openstack image set`/`image create --file` không dùng được cho Glance trong script (xem mục
sáng 13/11).

Phát hiện đáng giữ từ 13/11:

- **openstackclient không dùng được cho Glance upload/patch trong script**: `image create --file` PATCH lại thuộc tính sau upload dài (Glance 400 khi image đã active); không có `--file` + stdin không phải tty (nohup) thì OSC đọc image data từ stdin (415); `image set --property` cũng dính 400. Tool chuyển hẳn sang REST (curl POST/PUT/JSON-patch).
- `.properties` của image chứa cả field Glance quản lý (`os_hash_*`, `os_hidden`, `stores`) — copy property phải theo **allowlist** (`hw_*`, `os_distro`, `os_type`...), không được quét `os_*`.
- **2 NIC cùng subnet là thiết kế lỗi**: ở đích cloud-init sinh lại config cho cả 2 NIC → 2 default route cùng metric → reply đi sai port, Neutron port security drop (mất cả IP chính). Test chuyển NIC2 sang network riêng; production multi-NIC nên cùng kết luận.
- Tên NIC trong guest không tin được sau rollback (udev kẹt `cirename0`): phải dò theo MAC.
- Vận hành: `pkill -f` tự match phiên của mình; **không scp đè script bash đang chạy** (bash đọc theo offset → lệnh lệch dòng).

Thiết kế: [DESIGN-V2.md](DESIGN-V2.md). Hướng dẫn dùng: [GUIDE.md](GUIDE.md). Nền tảng v1:
[TEST-REPORT-20261105.md](TEST-REPORT-20261105.md). Chạy trên lab mục 9 của
[../system-info.md](../system-info.md), dựng từ 05/11, không dựng lại.

## Kết luận

Cả 4 tính năng mới chạy được trên lab và **không phá hành vi v1**:

| Bộ test | Nội dung | PASS | FAIL |
|---|---|---:|---:|
| `blocksync-unit.sh` | Loop device ↔ RBD, so từng byte bằng `qemu-img compare`, đếm chunk delta | 8 | 0 |
| `e2e-migrate.sh` (v1, không sửa) | Cold mặc định: regression toàn bộ hành vi v1 trên tool v2 | 50 | 0 |
| `live-migrate.sh` | `--mode live` với guest đang ghi 4 MB/s, đo downtime, rollback | 26 | 0 |
| `newip-voltype.sh` | `--ip new --volume-type mig-typeb --start-source`, 2 VM chạy song song, rollback | 26 | 0 |

Trạng thái lab sau test: `demo-vm` ACTIVE ở nguồn, phía đích sạch, pool RBD rỗng.

## Live mode: số đo

VM 1 vCPU/1 GB, volume 10 GB (root) + 2 GB (data), guest ghi đè ngẫu nhiên 4 MB + fsync mỗi giây
vào volume data trong suốt quá trình.

**Downtime dịch vụ (log ghi mỗi giây trong guest): live 250,5 s — cold cùng ngày 276,8 s.**

| Giai đoạn live | Số đo |
|---|---|
| Pre-sync root 10 GB (VM đang chạy) | lượt 1: 2 268 MB ghi / 130 s; lượt 2: 44 MB → hội tụ (≤ 64 MB) |
| Pre-sync data 2 GB (đang bị churn) | lượt 1: 740 MB / 34 s; lượt 2–5 dao động 104–156 MB, **không hội tụ**, tool cảnh báo rồi vẫn cutover như thiết kế |
| Cutover: sync delta cuối | root 104 MB / 60 s; data 140 MB / 16 s (thời gian chủ yếu là **đọc lại toàn bộ** để tính hash) |
| Tổng wall time | 687 s (cold: VM tắt suốt; live: VM chỉ tắt 250 s cuối) |

Vì sao lab chỉ nhanh hơn ~26 s: thời gian lượt cuối vẫn tỷ lệ với **cỡ volume** (phải đọc lại toàn
bộ 12 GB để tính hash, ~200 MB/s), chỉ phần **ghi + verify** co lại theo delta. Volume nhỏ nên phần
tiết kiệm (copy 66 s + compare 87 s của cold) gần bằng phần đọc hash. Lợi ích tăng theo cỡ volume:
với volume lớn mà tỷ lệ ghi thấp, cold tốn copy + compare toàn bộ **trong lúc VM tắt**, còn live chỉ
tốn 1 lượt đọc. Số lab chỉ cho tỷ lệ giữa các giai đoạn; muốn ước lượng production phải đo trên
đường mạng thật.

Đã kiểm chứng thêm trong test live:

- Dữ liệu tĩnh (300 MB random) checksum không đổi qua migrate và qua rollback.
- File đang bị ghi (churn) nhất quán tới giây yêu cầu stop; không lỗi ext4/I/O.
- SSH host key, hostname giữ nguyên; VM nhận metadata từ cloud đích.
- Readback verify (mặc định): từng chunk ghi lên RBD được đọc lại so sánh ngay.

## `--ip new` + `--volume-type` + `--start-source`

- VM đích lấy `192.168.120.188` + MAC mới từ pool đích (nguồn giữ `.50`); **port nguồn không bị
  tháo**, vẫn gắn vào VM nguồn.
- Cả 2 volume đích mang type `mig-typeb`; preflight kiểm tra type tồn tại và image nằm đúng pool
  trước khi copy.
- `--start-source`: VM nguồn được unlock + bật lại sau khi chuyển xong; 2 VM chạy song song, ping
  được nhau 2 chiều, mỗi VM đúng instance của cloud mình.
- Rollback xóa sạch phía đích (server, volume, port, RBD) và **không đụng** VM nguồn đang chạy.

## Sự cố trong lúc làm

| Sự cố | Nguyên nhân | Xử lý |
|---|---|---|
| Preflight live fail `librbd python bindings` | `migrate-vm.sh` activate mig-venv nên `python3` là venv python, không thấy `rbd`/`rados` (nằm ở python hệ thống). Lệnh kiểm tra tay trước đó chạy ngoài venv nên "qua" | Thêm `SYS_PY=/usr/bin/python3`, blocksync luôn chạy bằng python hệ thống |
| Test live từ chối chạy lại | Lần fail trước để lại state dir (chưa có volume-map) | `migrate-vm.sh rollback` dọn được cả state dở dang này — đúng thiết kế |

## Chưa test (mới, cộng với mục "Chưa test" của v1 vẫn nguyên)

| Mục | Vì sao đáng lo |
|---|---|
| Volume type nằm **pool khác** (`RBD_POOL_MAP`) | Lab chỉ có 1 backend RBD; `mig-typeb` cùng pool. Nhánh map pool + cephx caps trên pool thứ hai chưa chạy thật |
| `--ip <addr>` chỉ định | Mới test `same` và `new`; nhánh addr cùng code với new, khác mỗi `--fixed-ip` |
| Ephemeral + volume data gắn kèm | Mới test ephemeral thuần; nhánh trộn (root image + vdb volume) có code, chưa chạy |
| Ephemeral với image lớn | Snapshot 2 GB; image hàng chục GB sẽ cần đĩa trống trên host chuyển bằng cỡ file qcow2 |
| Live với guest ghi nhanh hơn tốc độ đọc 1 lượt | Churn 4 MB/s còn hiền; ghi dữ dội hơn thì delta cuối lớn, downtime tiến về cold |
| `--verify full` và `--verify none` | Mới chạy mặc định `readback` |
| `lab.sh up` nguyên lượt từ VM trắng | Vẫn là replay ghi chép 05/11, chỉ `status`/`demo-reset` được test thật |
| Hai VM song song (`--start-source`) lâu dài | Mới kiểm tra ping/SSH vài phút; trùng hostname + host key với hệ thống ngoài (monitoring, cron) chưa đo |
