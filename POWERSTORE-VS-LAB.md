# PowerStore 5000T so với lab LVM iSCSI (2026-11-05, cập nhật 17/11)

Ghi chú nghiên cứu từ source Cinder `master` và tài liệu Dell. Bản Cinder của hệ thật cần đối chiếu lại.

## Quyết định 17/11: node convert chạy 1 path (dù SAN multipath)

Hợp lệ — đây chính là cấu hình được test nhiều nhất (toàn bộ test 12–13/11 trừ 2 bộ multipath).
Checklist riêng cho kiểu "host 1 path, SAN nhiều path":

1. `MIG_MULTIPATH` để mặc định 0 — os-brick single-path trả về `/dev/sdX`; guard kích thước vẫn
   chạy, guard dm-* không áp dụng.
2. **Tắt hoặc blacklist LUN trong `multipathd` trên node convert.** multipathd còn chạy sẽ vồ
   `sdX` thành dm → os-brick detach/flush vướng "device busy", dm mồ côi nằm lại.
3. **Soi portal được chọn có thuộc node own volume không** (ALUA): driver trả về portal cả 2 node,
   os-brick single-path lấy cái đầu kết nối được; trúng node non-optimized thì I/O bị forward nội
   bộ, copy chậm hẳn. Kiểm bằng `iscsiadm -m session` + volume owner trên mảng.
4. Không có failover giữa lúc copy: mất đường → chờ `replacement_timeout` (120 s) rồi EIO.
   Cold chạy lại an toàn; live fail pre-sync thì chạy lại pass, fail lúc cutover thì downtime kéo
   dài. Tránh migrate trong cửa sổ NDU/upgrade firmware của mảng.
5. **So WWN device với volume trên mảng vẫn bắt buộc** — rủi ro LUN cấp lại không phụ thuộc số
   đường (sự cố stale-backstore 13/11 xảy ra trên 1 đường).

## Cập nhật 13/11: lab đã giả lập được phần host-side và test PASS

Nguồn lab chuyển từ `tgtd` sang **LIO (`lioadm`) với 2 portal** `10.77.0.12/.13` — đúng phần
"giả lập được" ở bảng dưới. Kết quả chạy thật:

| Test | Kết quả |
|---|---|
| `tests/powerstore-sim.sh` (multipath attach, 2 đường active, mất 1 đường giữa lúc đọc 10 GB, read-only, detach sạch) | **30/0** — multipathd thấy đường hỏng sau 18 s, đọc xong rc=0, 2 đường hồi sau 11 s |
| `e2e-migrate.sh` với `MIG_MULTIPATH=1` (migrate + rollback đầy đủ qua multipath) | **50/0** |

4 việc "cần sửa" bên dưới đã làm: (1) `brick_attach.py` bật multipath qua env `MIG_MULTIPATH=1`,
kèm guard từ chối device không phải dm và **guard so kích thước device với volume** (chặn LUN lạ
khác cỡ); (2) `multipath-tools` + stanza ALUA trên host chuyển; (3) `blockdev --setro` xác nhận có
hiệu lực trên dm (dd oflag=direct fail); (4) WWN được ghi vào state — **so WWN với mảng thật vẫn
phải làm khi có PowerStore**, vì guard kích thước không bắt được LUN lạ cùng cỡ.

Bằng chứng sống cho rủi ro "device không trỏ nơi mình nghĩ": trong lúc dựng, một **backstore LIO
cũ** còn giữ handle làm compute đọc/ghi một nơi, LV thật một nơi (hash 2 đường lệch nhau, LV 0%
allocated). `qemu-img compare` của tool **không bắt được** (so cùng device), chỉ assert checksum
trong guest của e2e bắt được. Kết luận giữ nguyên: với mảng thật phải đối chiếu WWN device với
volume trên mảng trước khi copy.

Cách dựng phần giả lập (đã nằm trong config repo): `src-globals.yml` → `cinder_target_helper:
"lioadm"` + `cinder_volume_extra_volumes: ["/sys/kernel/config:/sys/kernel/config"]` (rtslib trong
container không tự mount configfs được); host storage cần `target_core_mod`/`iscsi_target_mod`
(`/etc/modules-load.d/lio.conf`); portal 2 là IP phụ trên NIC iSCSI (`60-lab.yaml` của `.106`);
`target_secondary_ip_addresses` trong `[lvm-1]`. Lưu ý vận hành: **đổi target stack làm
connection_info cũ trong BDM của Nova thành rác** — hard reboot không refresh, shelve vấp volume
error; VM test thì dựng lại nhanh hơn gỡ.

## Giống và khác

| Hạng mục | Lab (LVM + `tgtd`) | PowerStore 5000T | Tác động lên công cụ chuyển |
|---|---|---|---|
| Lớp công cụ dùng | Cinder API + os-brick | Cinder API + os-brick | Luồng giữ nguyên |
| Clone | LVM thin snapshot | `POST /volume/<id>/clone`, thin clone dùng con trỏ | Nhanh như nhau. Driver có giới hạn 32 trong cây snapshot |
| Provisioning | Thin (thick nếu cấu hình) | Luôn thin + nén, không có thick | Bỏ được lo ngại clone thick |
| Copy và compare | Đọc toàn bộ logical | Đọc toàn bộ logical | Downtime vẫn tăng theo cỡ volume |
| Target | Mỗi volume một IQN, LUN 1 | IQN của mảng theo từng port, volume là LUN theo host | **Khác.** Xem rủi ro LUN bên dưới |
| Đường dẫn | 1 portal | Nhiều portal trên 2 node | **Khác.** Bắt buộc multipath |
| Node | 1 tiến trình `tgtd` | 2 node, dùng ALUA | **Khác.** Không được cấu hình active/active |
| Quyền truy cập | Target tạo khi gắn | Driver tự tạo host object theo IQN initiator, rồi map volume | IQN của host chuyển phải duy nhất |
| Sau detach | Target bị xóa | Bỏ map, host object vẫn còn | Còn sót host object của host chuyển trên mảng |
| CHAP | Do Cinder cấp | Do driver đọc từ mảng, chế độ Single | os-brick dùng thông tin driver trả về |
| Read-only | `tgtadm` bỏ qua `ro` | Không thấy driver xử lý `ro` | Chỉ dựa vào `blockdev --setro` trên host |
| `tgtd` restart mất target | Có | Không áp dụng | Bỏ được lo ngại này |

## Việc cần sửa trước khi chạy với PowerStore

1. `brick_attach.py`: đang đặt `use_multipath=False`, `multipath=False`. Phải bật.
2. Host chuyển: cài `multipath-tools` và stanza PowerStore theo E-Lab Host Connectivity Guide.
3. `blockdev --setro`: kiểm tra có hiệu lực trên device multipath.
4. Xác minh volume: thêm so sánh WWN của device gắn vào với volume trên mảng. Xem rủi ro dưới đây.

## Rủi ro suy luận từ cơ chế, chưa kiểm chứng

- **Device cũ trùng LUN:** PowerStore dùng chung IQN nên số LUN có thể được cấp lại. Nếu lần chạy trước chết và để lại device cũ, lần sau có thể đọc nhầm volume. `qemu-img compare` đọc cùng device đó nên vẫn khớp và **không phát hiện được**.
- **Kiểm tra sạch bằng session:** với IQN dùng chung, còn session iSCSI không có nghĩa là còn volume gắn. Nên kiểm tra theo device/LUN.
- **Mất đường giữa lúc copy:** `no_path_retry` quyết định `qemu-img` treo hay lỗi. Cần test.

## Giả lập được gì

Không tìm thấy simulator PowerStore công khai. Dell trả lời trên diễn đàn tháng 11/2024 là không có.

| Thứ cần test | Giả lập được? | Cách |
|---|---|---|
| Multipath qua 2 portal, attach/detach os-brick | ✅ | `cinder_target_helper: lioadm` + `target_secondary_ip_addresses` |
| Mất một đường giữa lúc copy | ✅ | Chặn một portal bằng iptables |
| ALUA, optimized và non-optimized | ⚠️ Một phần | LUN LIO cấu hình tay ngoài Cinder, vì Cinder không tạo nhóm ALUA |
| Nhiều volume chung IQN, cấp lại LUN | ⚠️ Một phần | LUN LIO cấu hình tay, kiểm tra script ở mức host |
| Failover giữa 2 node thật | ❌ | Chỉ giả lập được phía host |
| Driver, host object, thin clone, giới hạn 32, CHAP thật | ❌ | Cần PowerStore |
| Tốc độ thật | ❌ | Cần đường mạng thật |

Lưu ý: `tgt.py` không dùng `portals_ips`, nên lab hiện tại phải đổi sang LIO. Chưa thử LIO trong container Kolla trên Ubuntu.

## Nguồn

- [Cinder PowerStore driver](https://docs.openstack.org/cinder/latest/configuration/block-storage/drivers/dell-emc-powerstore-driver.html)
- [`adapter.py`](https://github.com/openstack/cinder/blob/master/cinder/volume/drivers/dell_emc/powerstore/adapter.py), [`client.py`](https://github.com/openstack/cinder/blob/master/cinder/volume/drivers/dell_emc/powerstore/client.py)
- [`targets/tgt.py`](https://github.com/openstack/cinder/blob/master/cinder/volume/targets/tgt.py), [`targets/iscsi.py`](https://github.com/openstack/cinder/blob/master/cinder/volume/targets/iscsi.py), [`targets/lio.py`](https://github.com/openstack/cinder/blob/master/cinder/volume/targets/lio.py)
- [Dell KB 000130123: active/active giảm hiệu năng](https://www.dell.com/support/kbdoc/en-us/000130123/powerstore-performance-issue-due-to-multipath-configured-as-active-active-in-host-side)
- [Dell KB 000220440: thiếu cấu hình MPIO](https://www.dell.com/support/kbdoc/en-us/000220440/io-goes-to-one-due-to-missing-mpio-configuration-on-hosts-side)
- [Dell: Snapshots and Thin Clones](https://infohub.delltechnologies.com/en-au/l/dell-powerstore-snapshots-and-thin-clones/snapshots-overview-4)
- [Dell community: PowerStore simulator](https://www.dell.com/community/en/conversations/powervault/powerstore-simulator/647f897cf4ccf8a8de91a78e)
