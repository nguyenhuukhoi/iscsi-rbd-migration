# Runbook chiến dịch migration

Mỗi wave có người điều khiển, chủ ứng dụng nghiệm thu và người quyết rollback. Không coi exit 0 hoặc Nova ACTIVE là nghiệm thu dữ liệu. Thiết kế cơ chế ở [DESIGN.md](DESIGN.md); cú pháp ở [GUIDE.md](GUIDE.md).

## 1. Trước chiến dịch

| Gate | Bằng chứng phải lưu |
|---|---|
| Artifact | Commit, engine/UI version, wheel SHA256, requirements pin, thời gian cài |
| Convert host | Doctor JSON và log, write-check trên mọi pool, disk headroom, NTP, IQN duy nhất |
| Storage | Type→pool xác minh bằng Cinder UUID và RBD image; caps mig-xfer; single-path/blacklist đúng |
| PowerStore | WWN của clone đang attach khớp volume clone trên mảng; portal/owner đã kiểm |
| Cloud scope | Keystone/region/interface/project UUID ở hai phía; account và role đã thử |
| Network | Network/CIDR/MAC/IP, routing/VLAN, SG/AAP/VRRP, FIP/DNS và IPAM đã duyệt |
| Dung lượng | Quota và Ceph headroom cho **cả wave**, root ephemeral mới và tài nguyên tạm |
| Freeze | Conf, project/flavor map, AZ/type/pool, nguồn attachments/ports/flavor đóng băng |
| Ứng dụng | Backup, checksum guest, kiểm transaction, downtime budget, RPO rollback được ký nhận |
| Tổ chức | Wave nhỏ đầu tiên, điểm dừng, lịch ca và kênh liên lạc đã thống nhất |

```bash
migrate --version
migrate doctor --write-check --json > /root/campaign-doctor.json 2> /root/campaign-doctor.log
migrate inventory --json > /root/campaign-inventory.json
migrate status --all --json > /root/campaign-state-before.json
```

Đọc mọi FAIL/WARN/INFO. Dòng `discard not checked` chưa chứng minh discard hoạt động. Kiểm `mig-doctor-*` đã được dọn khỏi từng pool vì code không kiểm rc rm. Doctor xanh không chứng minh SAN identity, mọi policy Neutron hay quota toàn đợt.

### Canary bắt buộc

Chạy trên đúng backend, credential, AZ và topology dự kiến dùng thật. Canary dùng dữ liệu thử; không chạy test có khả năng phá dữ liệu trên VM production.

1. Tạo file thử trong guest trên root và mọi data disk. Lưu `sha256sum`, mount/device, filesystem UUID và dữ liệu giao dịch mẫu ra nơi ngoài VM.
2. Cold với `MIG_DEFAULT_OPTS=` và `--verify full`, port nguồn tháo. Đối chiếu clone WWN khi attach; kiểm `.detached.json` sau detach.
3. Nghiệm thu guest đích: boot, checksum tất cả file, mount, dịch vụ, mạng và không có writer nguồn.
4. Ghi thêm dữ liệu thử chỉ ở đích để xác minh hiểu RPO. Lưu phần cần giữ bên ngoài, rollback có xác nhận, kiểm nguồn trở lại và checksum ban đầu.
5. Lặp lại với live. Tạo tải ghi thử và xem delta/pass cuối. Kiểm trường hợp nguồn vốn SHUTOFF.
6. Copy một volume available, gắn vào guest thử cô lập, đọc checksum; tháo guest thử rồi rollback volume. Nếu cần `--in-use`, làm canary riêng với sự chấp thuận crash consistency.
7. Nếu có ephemeral VM trong chiến dịch, thử root snapshot→BFV và rollback. Kiểm size root, type/pool, image đã dọn và boot thành công.

Checksum phải đọc **trong guest**. qemu-img compare và size guard đều có thể khớp khi tool đang đọc nhầm LUN cùng cỡ. Không thay WWN check bằng dung lượng device hoặc bằng việc “iSCSI session còn sống”.

## 2. Mỗi wave

### Chọn và plan

Ưu tiên danh sách UUID đã duyệt để tên trùng không gây nhầm. Không plan lại đối tượng có state sống; dùng status/dry-run rollback để điều tra. CLI plan ghi export cục bộ và có thể thay bằng chứng source_power cũ.

```bash
MIG_DEFAULT_OPTS= migrate plan --vm-file /root/wave-01.txt --mode live --verify readback --plan-jobs 4 --json > /root/wave-01-plan.json 2> /root/wave-01-plan.log
```

Trong UI: Chọn → Tùy chọn → Plan. Mở chi tiết từng hàng, gồm effective/from, command, scope project và checks. Hàng READY volume rời chưa được preflight đầy đủ. Tổng volume/GB từ UI có thể chưa gồm root ephemeral mới; cộng riêng khi duyệt quota.

### Duyệt tác động

| Câu hỏi | Điều kiện cho phép chạy |
|---|---|
| Những VM nào dừng? | Danh sách khớp change, cửa sổ downtime đã mở |
| Port nào tháo? | Same mặc định detach; ngoại lệ có phê duyệt và cô lập nguồn |
| Đích ở đâu? | Project/AZ/host/type/pool khớp bảng mapping đã đóng băng |
| Verify gì? | Readback/full theo kế hoạch; none chỉ ngoại lệ đã duyệt |
| WARN nào còn? | Mỗi WARN có quyết định; HALF-MADE COPY phải xử lý trước |
| Khi nào dừng wave? | Lỗi checksum, sai WWN, sai scope, vượt downtime hoặc lỗi control-plane chưa hiểu |
| Rollback có mất gì? | Chủ dữ liệu chấp nhận mất ghi ở đích, hoặc đã có cách giữ dữ liệu |

CLI execute tự plan lại rồi hỏi `yes`. Với UI, một đối tượng hoặc batch có option rủi ro phải gõ tên theo từng dòng; batch thường gõ số lượng. Token hết hạn sau 15 phút hoặc conf/maps thay đổi thì plan lại. Không chỉnh input để “khớp nút” nếu khác đối tượng đã duyệt.

### Chạy và theo dõi

```bash
MIG_DEFAULT_OPTS= migrate migrate --vm-file /root/wave-01.txt --mode live --verify readback
```

Ghi batch log path ngay. Theo dõi presync delta, marker t-stop, copy/verify, tạo server, downtime breakdown và kết quả từng hàng. Một migration mỗi lúc; plan song song không có nghĩa migration song song. Giữ dừng ở FAIL đầu tiên trong wave đầu.

Trong UI dùng Chạy để theo dõi, Dừng sau con hiện tại khi cần giữ con đang chạy. Mất browser/tunnel không hủy job. Login mới thay phiên cũ nhưng không giết job. Runner restart có thể để child chạy DETACHED; không bấm chạy thêm chỉ vì UI mất progress.

### Nghiệm thu sau mỗi VM

| Nhóm | Kiểm ngoài ACTIVE |
|---|---|
| Dữ liệu | Checksum guest, filesystem/mount/device, dữ liệu ứng dụng, transaction consistency |
| Boot | Console, cloud-init, root BFV đúng, disk thứ tự, SSH host key và hostname |
| Network | Fixed IP/MAC/AAP, port security, SG rules, route/DNS/FIP, lưu lượng hai chiều |
| Compute | Flavor, AZ/host, server-group policy/rules và anti-affinity thực tế |
| Ứng dụng | Health endpoint, đọc/ghi thử được duyệt, queue/cron/agent/cluster membership |
| Nguồn | SHUTOFF+locked và port đã tháo theo kế hoạch; không có writer trùng |
| Dọn tạm | Clone/attachment/device đã tháo, image tạm đã dọn; SG được giữ có chủ đích |
| Bằng chứng | ID nguồn/đích, operator, thời gian, result/exit, acceptance và người duyệt |

Không xóa nguồn, state hoặc reserve ports chỉ vì thấy OK. Chỉ giải phóng nguồn sau thời hạn rollback và quyết định đóng change riêng.

## 3. Sự cố

Trước thao tác: dừng nhận wave mới, ghi ident/batch, bảo toàn state/log và xác minh process còn sống. Không chạy `rm -rf` state, không xóa lockfile, không reset Cinder state để ép xóa khi volume còn gắn.

| Tình huống / thông báo | Hành động |
|---|---|
| Cần hủy có kiểm soát | SIGINT process group qua UI Hủy; CLI Ctrl-C. Chờ cleanup detach. Kiểm state và cloud trước rollback |
| SIGINT không kết thúc | UI cho SIGKILL sau thời hạn `--kill-after` của runner (mặc định 120 s), cần gõ tên. Chỉ dùng sau khi chấp nhận bỏ cleanup; kiểm LUN/attachment/clone và phase thủ công |
| SIGKILL, mất điện, process crash | Không có finally đáng tin cậy. Đọc state/file IDs, cloud status, attachment records. Không mặc định nguồn vẫn chạy vì thiếu log cuối |
| Runner restart: DETACHED/LOST | Kiểm PID/cmdline, CLI status và raw/batch log bằng root. DETACHED không phải job đã fail; không tự resume queue |
| `timed out after ... s`, rc 124 | Xác minh thao tác API đã thành công phía server chưa. Không retry create/delete mù; kiểm IDs/owner marks trước |
| Exit 3, phase preparing | Xác nhận chưa t-stop/dst-server-id và không còn process. Sửa nguyên nhân; CLI rerun A6 dọn recorded objects rồi bắt đầu mới. UI chưa hỗ trợ chọn lại state này |
| Exit 4 hoặc `state exists ... that run stopped the source ...` | Marker đã tồn tại; nguồn có thể đã dừng. Dry-run rollback và quyết định RPO; không xóa marker để ép chạy tiếp |
| Exit 1/5/130 | Không suy ra phase từ mã chung. Kiểm từng hàng, state, t-stop, ID đích và tình trạng nguồn |
| `the source VM changed since migrate exported it ...` | Recheck bắt hot-attach/NIC/flavor đổi; dừng thay đổi nguồn, làm lại plan sau dọn state trước cutover |
| `cannot read source port ...` | Port show lỗi API không được coi là mất; sửa API/quyền rồi rollback lại |
| `is attached to ... not to the source VM` | Không chiếm lại port tự động. Xác minh người dùng hiện tại, phối hợp chủ VM, thực hiện lệnh trả port mà tool in khi đã duyệt |
| `is error_deleting after its delete` | Giữ state. Đội Cinder kiểm backend/attachment, bảo đảm không còn consumer rồi sửa lỗi; chạy rollback lại |
| `still there`, `cleanup not complete` | Server đích chưa được xác nhận xóa; không bật nguồn thủ công. Xác minh Nova trước |
| `no owner mark to prove it is the copy` | State thiếu bằng chứng ownership; kiểm ID/time/properties và tài sản của chủ khác trước thao tác tay |
| `HALF-MADE COPY` | WARN không chặn engine. Dừng wave, so rule; chỉ xóa SG copy và tạo lại nếu không có port/VM khác dùng |
| `another migrate, volume or rollback runs on this host` | Kiểm run giữ MIG_LOCK. Đợi/điều tra nó; không thay path khóa |
| `is in use by another migrate or rollback` | State-directory flock còn giữ; xác minh process trước |
| Device busy / LUN nghi ngờ | Dừng copy, kiểm WWN/device/attachment và multipath blacklist. Không flush/disconnect thiết bị dùng bởi process khác |

Với lỗi xảy ra ngay sau create nhưng chưa ghi ID, rollback có discovery có owner mark cho server. Discovery không phải journal hoàn chỉnh; API list lỗi có thể bỏ sót orphan. Trước bật nguồn phải xác minh độc lập không còn bản đích hoạt động cùng IP/dữ liệu.

Standalone volume lỗi không có A6 hay exit classes VM. Dùng ident `vol-<source-id>`, xem `volume-map`, tháo consumer của bản đích nếu đã gắn thử rồi rollback có xác nhận.

## 4. Quyết định rollback

1. Xác minh đích đã nhận ghi chưa. Cô lập writers; lấy phần dữ liệu cần giữ bằng quy trình ứng dụng trước thao tác xóa.
2. Chốt bản dữ liệu nào là thẩm quyền. Tool không merge hai phía, không copy ngược.
3. Lưu dry-run, đọc IDs, project, t-stop/age và source_power. Tuổi t-stop không phải thời gian bắt đầu ghi ở đích.
4. Kiểm process kết thúc và không còn consumer khác của volume/port sẽ xóa.
5. Chạy rollback đúng scope trong hint hoặc original job. Xác nhận tên/ident; automation chỉ dùng `--yes` sau khi đã ghi quyết định.
6. Kiểm server/volume đích gone, port nguồn đúng device_id, nguồn đúng trạng thái export, checksum và ứng dụng nguồn. Kiểm FIP riêng vì restore FIP chỉ best effort.
7. Lưu `.rolled-back.<epoch>` và `rollback.json`; ghi lý do, RPO thực tế, các object giữ lại.

```bash
SRC_PROJECT=finance DST_PROJECT=finance-prod migrate rollback canary-vm --dry-run --json > /root/rollback-review.json
SRC_PROJECT=finance DST_PROJECT=finance-prod migrate rollback canary-vm
```

**RPO phải được nói rõ:** rollback bỏ mọi thay đổi chỉ có trên đích kể từ cutover. Nguồn vốn SHUTOFF được giữ SHUTOFF, không tự bật vì yêu cầu kiểm thử. Nếu cần phục vụ lại, đó là thao tác được chủ ứng dụng duyệt riêng.

## 5. Dọn và đóng chiến dịch

| Tài nguyên | Cách xử lý |
|---|---|
| State retired/batch log cũ | Prune dry-run, kiểm retention rồi prune thật |
| State sống sau migration thành công | Giữ cho rollback/handover; prune không xóa |
| Plan-only export | Giữ hoặc dọn có kiểm tra thủ công sau chiến dịch; prune không xóa |
| Source reserve ports | Giữ trong cửa sổ rollback; sau đóng change kiểm ownership/IPAM rồi chủ mạng duyệt giải phóng |
| SG/server group copy | Rollback giữ vì có thể dùng chung; không xóa theo prefix hàng loạt |
| Clone/device/attachment | Đối chiếu source ID, active process và WWN; dọn chỉ khi không consumer |
| Image tạm / mig-doctor image | Xác minh đúng lần chạy, kiểm dependency BFV rồi xử lý leftover |
| UI jobs/audit/attach records | Chính sách retention riêng, root-only; prune engine không đụng |

```bash
migrate prune --older-than 30d --dry-run --json
# Sau khi duyệt danh sách và bảo đảm không có job dùng log cũ:
migrate prune --older-than 30d
```

## 6. Bàn giao ca

Đọc Lịch sử theo batch/operator/state và mở chi tiết command, effective/from, conf snapshot, plan/checks, IDs, rc/exit_class, downtime, notify_rc. CSV UI chỉ xuất trang/filter hiện tại; không coi là bản sao đầy đủ lịch sử. Chụp thêm CLI status cho run ngoài UI:

```bash
migrate status --all --json > /root/handover-status.json
```

Ghi từng ident còn sống: phase, t-stop, dst_server_id, source_power export, trạng thái cloud vừa kiểm, PID/job nếu còn, bước cuối log, quyết định chờ/rollback và người chịu trách nhiệm. `migrated` chỉ có nghĩa state đã ghi server ID. Ghi riêng DETACHED/LOST và những WARN chưa xử lý. Bàn giao đường dẫn bằng chứng trong kho bảo mật; không gửi raw credentials hoặc `.out` ra kênh công khai.

## 7. Ước lượng từ đo đạc

Số đã ghi trong lab: **cold downtime 276 s**, gồm khoảng **100 s control-plane**; **plan khoảng 64 s/VM**; runner plan **4 VM song song**. Đây là số lab, không phải SLA PowerStore hoặc giới hạn tool. Xem nguồn bằng chứng trong [RELEASE-1.0.0.md](RELEASE-1.0.0.md).

Ước lượng cold = stop/detach/clone/attach/API wait + logical data copy + verify + create/boot. Tách theo `downtime breakdown` của canary cùng cỡ/tải. Verify full đọc lại toàn volume nên không lấy chỉ throughput ghi để tính.

Ước lượng live = thời gian đọc quét lượt cuối + ghi/verify delta + control-plane + boot; presync nằm ngoài downtime nhưng vẫn tiêu thụ SAN/network. Volume đầu có thể đổi tiếp trong lúc sync volume sau. Tải ghi không hội tụ làm cutover lâu hơn.

Nếu 8 VM đều plan mất 64 s và không contention, 4 luồng cho thời gian lý tưởng khoảng 128 s, cộng overhead. Tổng downtime wave là tổng downtime từng VM vì execute tuần tự; thời gian wave còn gồm plan, presync và acceptance. Dành buffer từ số đo phân vị chậm của canary, không nhân một số throughput quảng cáo của SAN.
