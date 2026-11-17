# Thiết kế v2: chuyển VM giữa 2 cloud OpenStack độc lập, iSCSI → Ceph RBD

Ngày 2026-11-12. Nền tảng: bản v1 đã chạy thật 05/11 (cold migration, giữ IP/MAC, 79 PASS / 0 FAIL,
xem [TEST-REPORT-20261105.md](TEST-REPORT-20261105.md)). Bản v1 lưu ở
[archive/migrate-vm-v1-20261105.sh](archive/migrate-vm-v1-20261105.sh).

## 1. Yêu cầu mới

| # | Yêu cầu | Trạng thái v1 |
|---|---|---|
| 1 | Chọn **off VM nguồn để convert** (cold) hoặc **real-time** (VM chạy trong lúc copy, chỉ dừng ngắn lúc cutover) | Chỉ có cold |
| 2 | Chọn **volume type** ở đích | Luôn dùng type mặc định |
| 3 | Chọn **giữ IP cũ** hoặc **lấy IP mới** (hoặc chỉ định IP) | Luôn giữ IP + MAC |
| 4 | Option **dựng lab** để test | Có script 01–06 rời rạc, chưa có lệnh gom |

## 2. Giới hạn vật lý phải chấp nhận

- **Không có zero-downtime thật.** Hai cloud độc lập không chung storage, không chung libvirt, nên
  không live-migrate kiểu Nova được. "Real-time" nghĩa là: copy phần lớn dữ liệu khi VM còn chạy,
  chỉ tắt VM trong lúc đồng bộ phần thay đổi cuối (delta) + tạo VM đích. Downtime tỷ lệ với lượng
  dữ liệu ghi gần cutover, không tỷ lệ với cỡ volume như cold.
- **LVM/iSCSI không có changed-block-tracking.** Không hỏi được backend "block nào đã đổi". Giải
  pháp: tự tính — lưu hash từng chunk sau mỗi lượt copy, lượt sau đọc lại toàn bộ volume phía nguồn
  và chỉ **ghi** chunk có hash khác. Đọc vẫn là toàn bộ (mỗi lượt), nhưng ghi và thời gian cutover
  chỉ còn phần delta; thời gian đọc 1 lượt cũng ngắn hơn nhiều copy+verify đầy đủ.
- **Đọc volume của VM đang chạy trực tiếp là không nhất quán.** Mỗi lượt sync phải đi qua
  `volume create --source` (clone LVM thin, crash-consistent, đã đo 5–10 s). Lượt cuối chạy khi VM
  đã tắt nên nhất quán hoàn toàn.

## 3. CLI

```
migrate-vm.sh plan     <vm> [options]    # chỉ đọc, preflight theo đúng options sẽ dùng
migrate-vm.sh migrate  <vm> [options]
migrate-vm.sh rollback <vm>              # như v1, tự hiểu theo options đã lưu trong state

Options:
  --mode cold|live        cold (mặc định): tắt VM rồi copy — như v1, vẫn dùng qemu-img.
                          live: pre-sync nhiều lượt khi VM chạy, cutover chỉ sync delta.
  --volume-type <type>    volume type ở đích cho mọi volume (mặc định: type mặc định của đích).
  --ip same|new|<addr>    same (mặc định): giữ IP + MAC, tháo port nguồn lúc cutover.
                          new: đích tự cấp IP + MAC mới từ pool, KHÔNG đụng port nguồn.
                          <addr>: IP chỉ định + MAC mới (chỉ VM 1 NIC).
  --start-source          sau khi chuyển xong, bật lại VM nguồn (chỉ cho --ip new|<addr>,
                          vì không còn trùng MAC/IP; cảnh báo trùng hostname).
  --max-passes <n>        live: tối đa n lượt pre-sync (mặc định 5).
  --delta-limit <MB>      live: cutover khi delta của lượt trước ≤ ngưỡng (mặc định 64 MB).
  --verify readback|full|none
                          live: readback (mặc định) — đọc lại từng chunk vừa ghi lên RBD và so
                          sánh; full — thêm qemu-img compare toàn bộ trong cutover (downtime tăng);
                          none — không kiểm. Cold luôn compare toàn bộ như v1.
```

State: `/var/lib/mig-migrate/<vm>/` như v1, thêm `options` (tham số đã dùng), `hash-<vid>.bin`
(hash cache từng chunk), `sync-<vid>.log` (thống kê từng lượt).

## 4. Luồng live

```
pre-sync (VM CHẠY, lặp theo volume):
  tạo volume đích (đúng --volume-type) ──┐ chỉ 1 lần, lượt đầu
  lặp tối đa --max-passes:               │
    clone volume nguồn (crash-consistent, 5–10s)
    gắn clone vào host chuyển (brick_attach.py, read-only)
    blocksync.py: đọc tuần tự, hash từng chunk 4 MiB
       lượt 1: chunk ≠ 0 → ghi RBD; chunk = 0 → bỏ qua (image mới toàn 0)
       lượt sau: hash khác cache → ghi (chunk toàn 0 → rbd discard); giống → bỏ qua
    gỡ clone, xóa clone
    delta ≤ --delta-limit? → thoát vòng lặp

cutover (bắt đầu tính downtime):
  tắt VM nguồn, lock
  --ip same → tháo port nguồn (chống trùng MAC/IP, như v1)
  mỗi volume: clone cuối → blocksync delta (+ verify theo --verify) → gỡ, xóa clone
  tạo port + VM đích theo --ip
  --start-source (ip mới) → unlock + bật lại VM nguồn
```

Rollback không đổi về giao diện: xóa mọi thứ phía đích, xóa clone sót, gắn lại port (chỉ khi đã
tháo — ip `new`/`<addr>` không tháo nên bỏ qua), unlock, bật VM nguồn.

### blocksync.py — vì sao viết mới

| Phương án | Loại |
|---|---|
| `qemu-img convert` mỗi lượt | Ghi lại toàn bộ mỗi lượt, cutover vẫn dài — mất ý nghĩa live |
| `rbd import-diff` | Cần snapshot diff phía nguồn, LVM/iSCSI không có |
| `drbd`/`dm-mirror` lồng vào volume nguồn | Phải sửa storage của VM đang chạy production — quá rủi ro |
| **blocksync hash-cache (chọn)** | Đọc tuần tự + blake2b từng chunk 4 MiB, cache hash local (10 GB ≈ 80 KB cache), ghi qua python `rbd` binding. Chunk 4 MiB trùng object size RBD nên discard chunk-0 giải phóng object |

Cold giữ nguyên `qemu-img convert -m8` + `qemu-img compare` của v1: đã có 79 PASS, qemu-img đa
luồng nhanh hơn python 1 luồng, không có lý do đổi đường đã kiểm chứng.

## 5. Volume type → pool RBD

Tool ghi thẳng vào RBD (`volume-<uuid>`), nên phải biết volume type đó nằm pool nào:

- `RBD_POOL_MAP="typeA:poolA,typeB:poolB"` (env), type không có trong map → `RBD_POOL` mặc định.
- Preflight: `dst volume type show <type>` phải có.
- Sau khi tạo volume đích: `rbd info <pool>/volume-<id>` phải thấy image — sai map hoặc thiếu quyền
  cephx là dừng ngay tại đây, trước khi copy.

Lab chỉ có 1 backend RBD nên mọi type cùng pool; test chỉ chứng minh được "volume đích mang đúng
type", chưa chứng minh nhánh khác pool. Ghi rõ trong phần chưa test.

## 6. Ba chế độ IP

| | `same` (mặc định) | `new` | `<addr>` |
|---|---|---|---|
| MAC | giữ nguyên | Neutron cấp mới | Neutron cấp mới |
| IP | giữ nguyên | từ pool đích | chỉ định |
| Port nguồn | tháo lúc cutover (giữ chỗ IP/MAC để rollback) | **không đụng** | không đụng |
| VM nguồn sau chuyển | SHUTOFF + lock | SHUTOFF + lock, `--start-source` bật lại được | như `new` |
| Preflight MAC/IP | MAC và IP phải trống ở đích | bỏ qua | IP chỉ định phải trống |
| Số NIC | nhiều NIC được (áp cho từng port) | nhiều NIC được | chỉ 1 NIC |

VM nguồn vẫn **phải tắt lúc cutover** kể cả khi đổi IP — không tắt thì lượt sync cuối không nhất
quán. `--start-source` chỉ bật lại *sau khi* copy xong; hai VM lúc đó chạy song song với dữ liệu
rẽ nhánh từ thời điểm cutover (dùng cho kiểm thử/so sánh, không phải HA).

## 7. Lệnh lab

`lab.sh` chạy từ workstation (thư mục `Lab/`), gom các script 01–06 đã có:

```
lab.sh status      # ssh kiểm tra: 6 VM lab, API 2 cloud, ceph, rbd, vị trí demo-vm
lab.sh up          # dựng theo thứ tự GUIDE mục 5 (01→06); dừng ở các bước image thủ công
lab.sh demo-reset  # đưa demo-vm về trạng thái gốc ở nguồn (rollback nếu có state)
```

`up` là replay từ ghi chép lần dựng 05/11, **chưa từng chạy lại nguyên lượt từ VM trắng** — giữ
nguyên cảnh báo của GUIDE. `status`/`demo-reset` là phần dùng hằng ngày và được test.

## 8. Kế hoạch test (lab .103–.109 đang chạy)

| Test | Nội dung | Tiêu chí |
|---|---|---|
| `blocksync-unit.sh` | Loop device 256 MB dữ liệu ngẫu nhiên → sync → sửa vài chunk → delta sync; so với `qemu-img compare` | Khớp từng byte cả 2 lượt; lượt delta chỉ ghi số chunk đã sửa |
| Cold regression | `e2e-migrate.sh` nguyên bản chạy trên tool v2 (option mặc định) | Giữ 50/0 như v1 |
| `live-migrate.sh` | Guest có tiến trình ghi liên tục; `--mode live`; đo downtime bằng marker log | Dữ liệu tĩnh checksum không đổi; ghi liên tục tới lúc stop; delta lượt cuối ≪ cỡ volume; downtime < cold (293 s tham chiếu) |
| `newip-voltype.sh` | `--ip new --volume-type mig-typeb --start-source` | VM đích IP/MAC mới từ pool đích; volume đích đúng type; port nguồn còn gắn; 2 VM chạy song song ping được nhau; rollback sạch |

## 9. Rủi ro còn mở (ngoài mục "Chưa test" của v1, vẫn còn nguyên)

- Delta không hội tụ khi guest ghi nhanh hơn tốc độ 1 lượt đọc: tool dừng ở `--max-passes` và vẫn
  cutover với delta hiện có → downtime dài hơn kỳ vọng. Log in rõ delta từng lượt để người vận hành
  quyết định hủy.
- Hash 128-bit blake2b: xác suất trùng (bỏ sót chunk đã đổi) ~2⁻¹²⁸ mỗi cặp — chấp nhận được;
  `--verify full` dành cho ai không chấp nhận.
- `--ip new` + `--start-source`: hai VM cùng hostname, cùng SSH host key, có thể cùng gọi vào hệ
  thống ngoài (cron, agent). Tool chỉ cảnh báo, không giải quyết.
