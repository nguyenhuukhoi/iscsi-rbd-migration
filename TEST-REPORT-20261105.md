# Kết quả test chuyển VM iSCSI → Ceph RBD (2026-11-05)

Hướng dẫn: [GUIDE.md](GUIDE.md). Báo cáo thô: [tests/results-20261105/](tests/results-20261105/).

## Kết luận

Cách làm chạy được trên lab: VM boot-from-volume sang được cloud Ceph RBD với **cùng MAC, IP, dữ liệu, SSH host key, hostname**, và rollback về nguồn được.

**Chưa đủ để áp thẳng vào production**: xem mục "Chưa test" và "Khác với hệ thống thật".

## Kết quả bộ test cuối

Chạy trên bản script cuối cùng.

| Bộ test | Nội dung | PASS | FAIL |
|---|---|---:|---:|
| `e2e-migrate.sh` | Chuyển, kiểm tra, reboot ở đích, admin bật VM nguồn, rollback | 50 | 0 |
| `e2e-nova-port.sh` | VM `--network` + `--boot-from-volume`: port do Nova tạo, volume không tên | 12 | 0 |
| `failure-injection.sh` | Preflight từ chối, `kill -9` qemu-img giữa lúc copy, rollback, chuyển lại | 17 | 0 |

Những gì đã kiểm chứng:

| Nhóm | Kết quả |
|---|---|
| Dữ liệu | `qemu-img compare` khớp từng byte, SHA-256 file ngẫu nhiên 300 MB không đổi, kể cả sau hard reboot |
| Dữ liệu ghi cuối | Tiến trình ghi mỗi giây vẫn ghi sau lúc yêu cầu stop 5 giây (tắt êm), không có lỗi ext4 hay I/O |
| Storage đích | `virsh dumpxml` xác nhận cả 2 đĩa dùng `protocol='rbd'`, image property có hiệu lực trong libvirt |
| Network | Cùng tên interface, MAC, IP; nhận DHCP và metadata từ cloud đích sau reboot |
| Chuyển dần | VM ở nguồn và VM đã chuyển gọi được nhau 2 chiều qua VLAN, mỗi ARP request đúng 1 reply |
| Rollback | Về đúng instance gốc, dữ liệu ghi ở đích không lẫn sang, đích sạch (kể cả pool RBD) |
| Chết giữa chừng | Tự logout iSCSI, không tạo VM đích, từ chối chạy đè; rollback dọn cả clone còn sót |

## Downtime

Đo từ log ghi mỗi giây trong guest, VM 1 vCPU, 1 GB RAM, 2 đĩa (10 GB + 2 GB):

**Dịch vụ gián đoạn 293 giây.**

| Giai đoạn | Thời gian |
|---|---:|
| Tắt VM, lock, tháo port | 26 s |
| Clone 2 volume | 19 s |
| **Copy** | **66 s** |
| **So sánh từng byte** | **87 s** |
| Gắn, gỡ, xóa clone, tạo volume đích và các gọi API | khoảng 50 s |
| Tạo port và VM đích | khoảng 22 s |
| Guest boot tới khi ghi lại | khoảng 24 s |

Tốc độ copy khoảng 180–227 MB/s logical. Copy và so sánh đều đọc **toàn bộ dung lượng logical** của volume, kể cả phần trống (volume 10 GB chỉ chiếm 2,1 GB trên RBD vẫn mất 57 giây copy). Vì vậy thời gian tăng theo cỡ volume, không theo dữ liệu thực có.

Số này đo trên VM lồng và mạng ảo, chỉ dùng để thấy tỷ lệ các giai đoạn, **không dùng để ước lượng thời gian production**. Muốn ước lượng phải đo trên đường mạng thật.

## Phát hiện từ thử nghiệm

### Volume `in-use` không gắn được vào host khác

| Cách | Kết quả |
|---|---|
| Nova detach root volume khi VM tắt | ❌ HTTP 400 |
| Attachment API trên volume `in-use` | ❌ HTTP 400: `status must be available or downloading` |
| Clone volume đang `in-use` | ✅ 5–10 giây trên LVM thin |

### Giữ VM nguồn không lên mạng

Đo khi VM đã chuyển đang chạy ở đích:

| Trạng thái port nguồn | VM nguồn | Peer ↔ VM đã chuyển | ARP từ dây |
|---|---|---|---|
| Disabled, còn gắn | Tắt | ❌ Mất cả 2 chiều | 4/4 |
| Enabled, còn gắn | Tắt | ✅ | 4/4 |
| **Enabled, còn gắn** | **Bật** | Ping OK nhưng **SSH nhảy qua lại giữa 2 VM** | **12 reply / 6 request** |
| Enabled, đã tháo (cách đang dùng) | Tắt | ✅ | 4/4 |
| Enabled, đã tháo (cách đang dùng) | Bật | ✅ VM nguồn có 0 NIC | 4/4 |

`server lock` không chặn được: admin vẫn bật được VM nguồn.

### Key Ceph

Ceph 20.2.5 tạo key `aes256k` (tiền tố `Ag`) và từ chối tạo kiểu `aes`: `creating key with insecure key type ("aes") not allowed`.

| Client | Đọc key `Ag` |
|---|---|
| Ceph 20.2.5 (Tentacle repo) trên `mig-host` | ✅ |
| Ceph 20.2.0 trong image Kolla 2026.1 | ❌ `error parsing file` |

Lab xử lý bằng cách vá image cục bộ trên 2 node đích lên client 20.2.5, không đẩy lên Harbor, không nới `mon_auth_allow_insecure_key`. Bản vá chỉ phục vụ lab và **image 2026.1 hiện tại sẽ gặp đúng lỗi này với mọi cluster cấp key `aes256k`**.

### Khác

`tgtd` restart làm mất toàn bộ target (đếm được 0 target, restart `cinder_volume` cũng chưa có lại). VM mới vẫn tạo target khi gắn. Chưa test tác động lên VM đang chạy, nên **đừng restart `tgtd` khi có VM đang chạy** cho tới khi test.

## Chưa test

| Mục | Vì sao đáng lo |
|---|---|
| SAN iSCSI thật, multipath | Lab dùng LVM + tgtd, một đường. `brick_attach.py` đặt `use_multipath=False` |
| LVM thick hoặc SAN không có clone nhanh | Clone là copy đầy đủ, downtime và dung lượng tăng mạnh |
| VM nhiều NIC | Có code, chưa chạy với VM nhiều NIC |
| Project khác `admin` | **Công cụ tạo mọi thứ trong project của openrc**, chưa ánh xạ project, quota, chủ sở hữu |
| Volume type khác nhau | Volume đích dùng type mặc định |
| Windows, driver khác virtio | Chỉ test Ubuntu 24.04 |
| Nhiều VM song song | Chỉ chạy tuần tự. Trên cùng VM, state nằm theo tên nên trùng tên VM giữa các project sẽ đụng nhau |
| Volume lớn, tải ghi nặng | Volume lớn nhất 10 GB, tải chỉ là một dòng mỗi giây |
| Script cloud-init `per-instance` trong guest | Cloud-init vẫn thấy instance-id mới. Đã kiểm chứng `runcmd` gốc không chạy lại, chưa test script đặt trong `/var/lib/cloud/scripts/per-instance` |
| Ứng dụng gắn với UUID của instance hoặc serial đĩa | UUID và volume ID đều đổi. `fstab` theo LABEL/UUID không ảnh hưởng, `/dev/disk/by-id` thì có |

## Khác với hệ thống thật

| Lab | Hệ thống thật cần xác nhận |
|---|---|
| Một controller, không VIP, không ProxySQL | Không ảnh hưởng luồng chuyển, vì công cụ chỉ gọi API |
| Ceph 20.2.5, key `Ag` | Cluster `711ad99e…` ghi trong tài liệu là 19.2.3, key `AQ`. Nhiều khả năng không cần vá image, cần kiểm tra lại phiên bản |
| Mạng iSCSI và provider là mạng ảo MTU 1442 | Kiểm tra jumbo frame và đường từ host chuyển tới cả 2 mạng |
| Provider VLAN giả lập bằng network `vlan-transparent` | Trên switch thật phải cho VLAN đi tới compute của cả 2 cloud |
| Kolla `22.2.1.dev18` trên `mig-host`, `.79` hiện `dev20` | Cùng commit `8ca92bc8f`, số `dev` khác vì chép source không kèm đủ lịch sử git. Nguyên nhân này là suy đoán, chưa xác minh |

## Sự cố trong lúc làm

| Sự cố | Nguyên nhân | Xử lý |
|---|---|---|
| `install-deps` lặp lỗi `ansible-galaxy` | Venv không có trong `PATH` | Thêm vào `PATH` |
| `iscsid` 404 | Harbor không có image iSCSI | Pull từ quay.io |
| VM kẹt `BUILD` | `tgtd` listen trên IP API | `tgtd_interface_address` |
| Không tạo được provider VLAN | Kolla để trống `network_vlan_ranges` | Override `ml2_conf.ini` |
| `demo-vm` lần đầu qua 35/35 nhưng thiếu image metadata | Regex `jq` sai trong process substitution, script không dừng; các assert quá yếu | Sửa regex, viết lại test |
| Rollback báo thành công mà VM chưa bật | Lỗi cú pháp trong điều kiện `if`, không có bước tự kiểm tra | Sửa, thêm kiểm tra `ACTIVE` cuối rollback |
| Test PASS giả khi baseline rỗng | So sánh chuỗi rỗng với chuỗi rỗng | Test dừng ngay nếu baseline hoặc migrate fail |

Các lần chạy test trước bản cuối (35/0, 32/10, 40/2, 34/13) **không được tính** vào kết quả.

## Bảo mật cần biết

- `client.mig-probe`: log lỗi của Ceph đã in key của user test này ra output phiên làm việc. User đã xóa ngay, không dùng lại.
- Harbor: trong lúc kiểm tra phát hiện registry chấp nhận credential quản trị dùng lại từ hệ thống khác. Không dùng lại. Đã khuyến nghị tách password riêng cho registry.
