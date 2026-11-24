# Sử dụng web UI Migrate

UI chạy trên convert host qua SSH tunnel. Cài đặt và account ở [DEPLOY.md](DEPLOY.md). Ý nghĩa option và ngoại lệ ở [GUIDE.md](GUIDE.md); điều kiện nghiệm thu ở [RUNBOOK.md](RUNBOOK.md). Các tên màn và thao tác dưới đây theo `migui/static/index.html` và `app.js` đang có.

## 1. Đăng nhập và điều hướng

Mở `http://127.0.0.1:8787/` sau khi mở tunnel. Nhập username/password do quản trị cấp. Không có đăng ký hoặc đổi password trong web. Header hiện operator và thời gian phiên còn lại. Đăng xuất hủy phiên, không hủy job.

Một phiên đăng nhập dùng chung toàn hệ thống. Login mới, kể cả account khác, thay phiên cũ. Phiên tối đa 8 h, idle 30 phút. Plan events và batch đang chạy có touch idle; background polling đơn thuần không giữ phiên. Đổi/xóa account được phát hiện ở request tương tác kế tiếp; không bảo đảm mọi stream mở bị ngắt ngay.

Stepper chỉ hiện trong luồng đợt: Chọn → Tùy chọn → Xác nhận → Chạy; bước Doctor chỉ được thêm vào đầu stepper khi vào từ nút **Bắt đầu đợt mới**. Tổng quan, Lịch sử và Doctor mở từ navigation không có stepper. Bước disabled không điều hướng. UI có link bỏ qua navigation để tới nội dung; focus chuyển về heading khi đổi màn.

Ảnh chờ reviewer bổ sung: `docs/ui/login.png`.

## 2. Tổng quan

Màn sau login có thẻ số đang chạy, đang chờ, OK hôm nay, cần xử lý; trạng thái Doctor; nút bắt đầu đợt; các job gần đây; chỉ báo kết nối và phần giải thích thuật ngữ.

| Control | Tác dụng |
|---|---|
| Chạy Doctor | Chạy kiểm rồi mở màn Doctor; có link xem kết quả đã có |
| Bắt đầu đợt mới | Đi tới luồng chọn sau khi đủ gate |
| Job gần đây | Mở chi tiết/theo dõi job |
| Navigation Instance/Volume | Chọn loại tác vụ |
| Theme | Đổi sáng/tối; nhãn nút nói hành động kế tiếp |

Thẻ số là query job history theo trạng thái/thời gian, không phải đo trạng thái cloud trực tiếp. Status nền cập nhật thường xuyên; có thể có độ trễ. Login khi Doctor chưa đạt sẽ tự gọi Doctor. Reload còn session không phải login mới.

Ảnh: `docs/ui/overview.png`.

## 3. Doctor

Các dòng giữ nguyên tiếng Anh của CLI, được nhóm theo chương trình/Python/cloud/storage/thư mục. Có thời điểm chạy và counts OK/WARN/FAIL/INFO. Mở log để xem lý do đầy đủ; bảng giải nghĩa nằm trong [DESIGN.md](DESIGN.md#63-catalog-doctor).

| Control | Tác dụng |
|---|---|
| Chạy Doctor | Chạy Doctor thông thường (`migrate doctor --json`) |
| Kiểm tra ghi RBD | Hiện sau khi Doctor xanh; dialog giải thích tạo image thử, nút **Kiểm tra ghi RBD** trong dialog mới chạy `--write-check` |
| Instance / Volume trên navigation | Mở khóa khi Doctor xanh trong phiên |

Doctor xanh cho phép inventory/list/plan/run VM hoặc volume trong phiên. Rollback, cancel, status và Lịch sử không bị gate Doctor; vẫn cần login. Doctor thường không chứng minh quyền ghi RBD. Khi write-check có `discard not checked`, phải xử lý dù kết luận có thể xanh. Kiểm leftover image bằng quy trình storage.

Ảnh: `docs/ui/doctor.png`.

## 4. Chọn Instance hoặc Volume

| Control | Tác dụng |
|---|---|
| Host nguồn multi-select | Chọn nhiều compute host từ inventory |
| Project nguồn multi-select | Chọn nhiều project; kết hợp host bằng giao |
| Tải danh sách | Gọi list với lựa chọn hiện tại |
| Ô tìm | Lọc danh sách đang tải trong browser |
| Dán danh sách | Resolve tên hoặc UUID trong danh sách; báo riêng không tìm thấy, mơ hồ, không đủ điều kiện |
| Checkbox/hàng VM | Chọn VM đủ điều kiện, cập nhật tổng ở thanh dưới |
| Copy ID | Sao chép UUID đầy đủ thay vì ID rút gọn |
| Rollback ở hàng có state | Mở chuẩn bị rollback; không bắt plan migrate trước |
| Tiếp: Tùy chọn | Mang tập lựa chọn sang bước tiếp |

Một lần tải nhận tối đa **128 host/project** (UI và runner cùng giới hạn). Ở tab Instance, không chọn host/project thì UI dùng các host nguồn đang up để tải phạm vi mặc định. Nếu nhiều host hơn, chia nhóm rõ ràng.

Bảng VM cho biết project nguồn→đích, boot BFV/ephemeral, volume/GB, IP, flavor, host/AZ, status và state. Project đích là text từ `PROJECT_MAP_FILE`, không sửa trong browser. Tên trùng phải dùng UUID. Dán chỉ chọn trong tập đã tải; không tự mở rộng cloud scope.

Hàng có state sống không tick được, kể cả phase preparing. Nếu cần A6 chạy lại trước cutover, dùng CLI theo RUNBOOK. `source_power` là trạng thái export, không phải query nguồn hiện tại.

Tab Volume yêu cầu chọn project. Volume có lựa chọn scope available/attached/all theo form. Attached chạy đường VM volume-only, có thể dừng nguồn. Volume in-use chỉ bật có chủ đích cho một volume; backend yêu cầu trạng thái/selection phù hợp. READY không phải preflight hoàn chỉnh.

Ảnh: `docs/ui/select-instances.png`, `docs/ui/select-volumes.png`.

## 5. Tùy chọn và plan

Form VM nhóm Đích, Mạng, Lưu trữ, Tính toán, Live và Nâng cao. Form Volume chỉ có các option áp dụng. Ô trống thường là **không gửi override**, không có nghĩa xóa mặc định trong conf.

| Nhóm/control | Thao tác và ý nghĩa |
|---|---|
| Project đích | Chỉ xem mapping; muốn đổi phải sửa `PROJECT_MAP_FILE` trên host rồi plan lại ([GUIDE.md](GUIDE.md#41-project_map_file--đổi-tên-project-ở-đích)). AZ → type → pool và flavor map: [GUIDE.md](GUIDE.md#42-az_type_map-volume_type_map-và-rbd_pool_map--type-và-pool-của-từng-volume), [mục 4.3](GUIDE.md#43-flavor_map_file--flavor-đích-khi-không-có-flavor-cùng-tên) |
| AZ, host đích | Chọn từ inventory; để host mặc định thì Nova scheduler chọn |
| ip: same / new / theo dòng | same giữ IP/MAC và tháo port nguồn; new cấp mới; **theo dòng** mở bảng "VM đã chọn → IP mới" ngay trong nhóm Mạng: nhập IPv4 cho từng VM (một NIC), ô trống = same; IP sai định dạng khóa nút Plan |
| Keep source ports | Mở cảnh báo và cần acknowledge; vẫn có thể được bật ngầm bởi MIG_DEFAULT_OPTS |
| Volume type, volume AZ | Chọn backend/AZ Cinder, độc lập compute AZ khi cần |
| Verify | Readback/full/none; none có cảnh báo rủi ro |
| Flavor, fallback | Override flavor hoặc dùng map khi không có cùng tên |
| Security groups, copy security group | Override SG; bỏ tick copy gửi `--no-copy-secgroup` để dùng SG cùng tên đã có |
| Server group, copy server group | Override group có sẵn; bỏ tick copy gửi `--no-server-group` để không theo group nguồn |
| Mode | Cold/live; bỏ trống theo mặc định cấu hình |
| Max passes, delta limit | Hiện khi mode hiệu lực live; bounds 1–50 và 0–65536 |
| Start source, volumes-only | Ngoại lệ/luồng copy volume theo GUIDE |
| Xem trước | Lệnh mẫu và tác động minh họa của lựa chọn hiện tại |
| Plan N VM/volume | Gọi plan; panel hiện VM đang làm, check mới nhất, số xong, thời gian và log |

Không có control `--ephemeral-as-image` trong UI tĩnh. Nếu chính sách yêu cầu cờ này, dùng CLI với phê duyệt ngoại lệ. Các cờ bật như Start source và Keep source ports không có override tắt khi bỏ tick. Tick lại copy SG/server group cũng không phủ định `--no-copy-secgroup`/`--no-server-group` đã có trong MIG_DEFAULT_OPTS. Ô IP theo dòng rỗng cũng không override IP mặc định đã cấu hình. Kiểm **effective/from trong kết quả plan**.

Preview trước plan chỉ minh họa, không là bằng chứng thực thi. Plan thật quyết định command, scope và checks. Copy volume-only vẫn stop nguồn dù preview có thể diễn đạt thiếu. Tổng GB không gồm đầy đủ root ephemeral mới. Không lấy preview để duyệt quota.

Plan chạy tối đa 4 VM cùng lúc mặc định runner. Chỉ event plan được stream; không có migration ở bước này. Hàng FAIL phải xử lý hoặc loại khỏi lựa chọn rồi plan lại. Sửa selection/options làm mất xác nhận trước. Mất kết nối plan: runner có thể vẫn làm xong, nhưng UI cần plan lại khi không còn token.

Ảnh: `docs/ui/options-instances.png`, `docs/ui/options-volumes.png`, `docs/ui/plan.png`.

## 6. Xác nhận

Khối tác động tóm tắt số nguồn dừng, port tháo, server/volume tạo, mode, verify và nguồn sau khi xong. Mở từng command để đọc argv, project, effective/from và nội dung plan. WARN trùng nội dung được gộp thành một checkbox; đọc và acknowledge từng cảnh báo đang hiện.

| Trường hợp | Gõ gì |
|---|---|
| Một VM/volume | Tên đúng như hiển thị |
| Batch thường | Số đối tượng |
| Batch có effective keep_ports, IP chỉ định, start_source hoặc in_use | Mỗi tên trên một dòng; giữ khoảng trắng/dấu phẩy trong tên |
| Rollback | Tên hoặc ident đầy đủ mà dialog cho phép |

Khi phải gõ tên từng dòng, mỗi dòng có thể là tên hiển thị hoặc ident đầy đủ (`rule.ids`), mỗi đối tượng đúng một lần, thứ tự bất kỳ; form và server kiểm cùng quy tắc. Server vẫn kiểm lại typed khi nhận.

Nút Chạy chỉ bật khi xác nhận khớp và đủ checkbox. Token có hạn 15 phút. Đổi config/maps hoặc sai typed tại server làm 409, phải plan lại. Busy 423 không nghĩa token đã chạy thành công; kiểm job/khóa trước thử tiếp. Không gửi lại liên tục khi request chưa rõ kết quả.

Checkbox dừng đợt khi một con FAIL được bật theo mặc định. Tắt chỉ khi kế hoạch tiếp tục đã được duyệt. Execute vẫn tuần tự.

Ảnh: `docs/ui/confirm.png`.

### Rollback dialog

Mở từ hàng có state, Lịch sử hoặc banner sự cố; tiêu đề **Rollback <tên>**. Dialog liệt kê **Sẽ xóa: N server · N port · N volume · N image** (ID đầy đủ trong mục "ID tài nguyên sẽ xóa"), thời điểm cutover và tuổi t-stop, trạng thái server đích nếu đọc được, nguồn đang ACTIVE/SHUTOFF và nguồn sẽ được bật lại hay giữ SHUTOFF. Dòng **Nguồn hiện tại** là trạng thái nguồn lúc chuẩn bị rollback; nguồn không đọc được thì hiện `không đọc được (<lý do>)` với lý do lấy từ CLI (đã scrub giá trị bí mật), ví dụ `không đọc được (Service Unavailable (HTTP 503))`, và engine sẽ từ chối rollback cho tới khi đọc được. Tick chấp nhận mất dữ liệu ghi ở đích từ thời điểm cutover, rồi gõ tên hoặc ident đầy đủ; nút đỏ **Rollback <tên>** chỉ bật khi cả hai khớp và xác nhận chưa hết hạn (đếm ngược hiện dưới ô gõ). Đóng dialog trở về màn đã mở nó.

Dry-run có thể chưa liệt kê orphan chưa ghi ID. Xác minh cloud/state trước khi confirm. Rollback xóa bản đích, không reverse-sync. Nguồn ban đầu SHUTOFF vẫn SHUTOFF. Không cần Doctor xanh để xử lý rollback.

Ảnh: `docs/ui/rollback.png`.

## 7. Chạy

| Control/chỉ báo | Tác dụng |
|---|---|
| Job header/operator/state | Ai chạy và trạng thái runner |
| Phase, downtime, volume, result | Dựa event CLI; không thay cloud/guest acceptance |
| Copy/verify/create progress | Theo giai đoạn; 100% copy chưa là job xong |
| Log filter | Tất cả, WARN+ hoặc event `@@` |
| Tìm log | Tìm trong log hiện có |
| Tự cuộn | Bám dòng mới; bỏ để đọc phần cũ |
| Tải log | Bản log được scrub cho UI |
| Queue | Trạng thái từng job trong batch |
| Dừng sau con hiện tại | Không chạy con kế tiếp; con đang chạy tiếp tục |
| Hủy | SIGINT process group, không tự rollback |
| Buộc dừng (SIGKILL) | Mở sau thời hạn `--kill-after` của runner (mặc định 120 s) kể từ SIGINT; khi còn khóa nút bị disabled với tooltip "mở sau N s"; phải gõ đúng tên VM rồi xác nhận trong dialog |
| Banner nguồn vẫn tắt | Dựa t-stop và state local; mở rollback/log để xử lý |

Thẻ Giai đoạn lấy từ `phase_detail` (tên giai đoạn, volume đang làm, %); thẻ Volume là k/N theo `phase_detail.volumes_done/volumes_total`. Sau khi đợt kết thúc, panel giữ job cuối: giai đoạn, volume, kết quả và lớp lỗi (`exit_class` 3 = "lỗi trước cutover — nguồn vẫn chạy", 4 = "lỗi sau cutover — nguồn đang tắt, cần rollback", 5 = "đợt có job FAIL"). Khi log còn trống, panel hiện "đang chờ dòng log đầu tiên…". Tổng volume có thể không tính root ephemeral mới. Downtime bắt đầu ở marker trước stop; không coi đồng hồ là xác nhận trạng thái nguồn thực tế.

Mất SSE: chỉ báo kết nối thay đổi, job không dừng; kết nối lại dùng event ID. Mất phiên thì login lại và mở job. Nếu runner restart, job có thể DETACHED/LOST và không tự theo dõi tiếp; đọc batch log/CLI status bằng root theo RUNBOOK.

Banner nguồn down có kiểm state rollback từ CLI trong lần refresh, nhưng vẫn không query cloud power trực tiếp. Nghiệm thu guest ngoài UI sau OK.

Ảnh: `docs/ui/running.png`.

## 8. Lịch sử

Tìm theo job/batch/ident/name/operator bằng ô tìm chung; lọc state, loại và khoảng ngày; phân trang 50. Chỉ live là lọc bổ sung phía client. API date dựa `t_plan`; không coi mọi ngày trên form là thời điểm job kết thúc.

| Control | Tác dụng |
|---|---|
| Tìm/lọc/ngày | Query các job runner đã lưu |
| Trang trước/sau | 50 job một trang |
| CSV | Xuất filter và trang hiện tại; không tự tải hết mọi trang |
| Chi tiết | Command, effective/from, conf snapshot, plan/checks, IDs, times, rc/exit_class, log |
| Chạy lại với cùng option | Điền form để **plan và xác nhận lại**, không chạy ngay |
| So với job khác | So effective, các conf liên quan được chọn và runner version |
| Rollback | Chuẩn bị dry-run summary, mở dialog xác nhận |

Rerun là tiện ích điền form, không snapshot môi trường hoàn chỉnh. Đặc biệt volume in-use phải kiểm/tick lại; không giả định mọi option ngoại lệ được khôi phục. Conf/map hiện tại có thể khác lúc job cũ.

State CLI ngoài UI xuất hiện qua status nhưng không có đầy đủ job/operator/plan record. Đọc `.live`, `.plan_only`, `.rolled_back`, `.running` trong `migrate status --json` khi bàn giao. Phase migrated chỉ là có server ID trong state.

Ảnh: `docs/ui/history.png`, `docs/ui/job-detail.png`, `docs/ui/job-diff.png`.

## 9. Phím tắt, theme và lỗi HTTP

`/` đưa focus tới ô tìm phù hợp nếu không đang nhập form. `Esc` đóng dialog/multi-select/details/diff. `Enter` tại xác nhận gửi khi nút đủ điều kiện; `Shift+Enter` thêm dòng tên. Theme mặc định theo hệ thống; nút đổi lưu `migui-theme` trong localStorage. Không lưu password/token ở đó.

| HTTP | Nghĩa | Xử lý |
|---|---|---|
| `401` | Không session, hết hạn/idle, login khác thay phiên, account bị thu hồi hoặc login sai | Login lại khi phù hợp; job vẫn có thể chạy |
| `409` | Token/typed/batch/state không còn phù hợp, conf đổi, không có state để rollback | Đọc error; plan lại hoặc kiểm status |
| `412` | Chưa có Doctor xanh trong phiên cho operation bị gate | Chạy Doctor; rollback/cancel/history không thuộc gate |
| `423` | Migration/lock đang bận | Xem job và CLI process, không xóa khóa |
| `429` | User/global login lockout hoặc quá 2 SSE/phiên | Đợi lockout hoặc đóng stream/tab thừa |
| `503` | Runner không tới được, audit không ghi được, API concurrency đầy, users/scrypt lỗi | Đọc error và journald; không bypass audit |
| `400` | Input không hợp lệ | Sửa đúng trường, không retry mù |
| `403` | CSRF/Origin/header không hợp lệ | Dùng đúng UI qua tunnel |
| `421` | Host/port không được chấp nhận | Dùng loopback/port tunnel cấu hình |

Login sai 5 lần trong 10 phút khóa user 15 phút; tổng 20 lỗi trong 10 phút khóa toàn cục 15 phút. Không thử nhiều tài khoản để lách khóa. Web giới hạn 8 API request đồng thời, 2 SSE/phiên; header timeout 30 s. Đóng tab không dùng nếu gặp giới hạn stream.

## 10. Danh sách ảnh chờ reviewer

Các PNG chưa thuộc bằng chứng kiểm thử cho tới khi reviewer chụp từ đúng release. Tên tham chiếu đầy đủ:

```text
docs/ui/login.png
docs/ui/overview.png
docs/ui/doctor.png
docs/ui/select-instances.png
docs/ui/select-volumes.png
docs/ui/options-instances.png
docs/ui/options-volumes.png
docs/ui/plan.png
docs/ui/confirm.png
docs/ui/rollback.png
docs/ui/running.png
docs/ui/history.png
docs/ui/job-detail.png
docs/ui/job-diff.png
```

Chụp bằng dữ liệu ví dụ và account thử nghiệm. Không đưa hostname/IP/credential production vào ảnh.
