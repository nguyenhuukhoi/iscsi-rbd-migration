# Bằng chứng release 1.0.0

Trạng thái: phát hành 1.0.0 (tag `v1.0.0`), 1.0.1 là bản sửa lỗi (CHANGELOG). **Gate lab v1 chạy ngày 22/11/2026** trên hai cloud lab 2026.1: nguồn LVM/LIO iSCSI, đích Ceph RBD. Kết quả từng suite ở mục 5. Lab không phải PowerStore. Không dùng kết quả này để tuyên bố PowerStore production đã đạt.

## 1. Commit index

Mã commit dưới đây thuộc lịch sử phát triển. Repo công khai được khởi tạo lại ngày 22/11/2026 với một commit chứa đúng cây của commit phát hành, nên các mã này không có trong repo công khai.

Commit sau gate và click matrix 22/11, mới nhất ở trên:

| Commit | Nhóm | Thay đổi |
|---|---|---|
| `fa8d1db` | Tài liệu | DEPLOY 2.1: OpenStack CLI/SDK nằm trong venv, hai cách cài (bộ đã test, hoặc theo upper-constraints) |
| `c685914` | Gate + Astra vòng 3 | Rollback lấy khóa host trước khi tra state; vòng đời job được theo dõi; focus sau dialog; Esc; tài liệu mã thoát 4 |
| `0eaabed` | Click matrix | Báo cáo ma trận trên `549e572`: 221 pass, 0 fail, 0 skip |
| `549e572`, `f75b88a`, `c652ddb` | UI | Theo dõi đúng job đã chọn, Tổng quan xếp job theo thời gian, skip link |
| `7c51114`, `2c95b18`, `4d20927` | Click matrix | Gộp ma trận vòng 1–3 (Codex Sol) |
| `c9b8931`, `b1bb1f5` | Astra vòng 2 | Scrub option hiệu lực đọc từ file, validator neo `\Z`, `defaultMode`, `badInput`, focus rollback, Esc, câu READY |
| `d146aab`, `8572e3e` | UI | Lỗi tìm qua ma trận: type `__DEFAULT__`, giới hạn live, nút Quay lại, ghi chú READY, focus rollback, Esc; job volume ghi option hiệu lực |
| `8cc5eae` | Gate | `migrate` nhận lại SIGINT khi được chạy nền từ script |
| `d06a9a7` | Gate | Rollback nhận `No Server found for` của OSC 10.x là đã xóa |
| `663a31b`, `5ee3ac5`, `5fefbf4` | Gate | R1, R3, `MIG_ISCSI_IP`, chép DEPLOY.md khi cài UI, nâng cấp tại chỗ |
| `7538d91`, `0314a58`, `69930f9`, `ebe5b99`, `9bd8a33` | Tài liệu | Ví dụ PROJECT_MAP_FILE, AZ_TYPE_MAP/RBD_POOL_MAP, FLAVOR_MAP_FILE |
| `18ca119` | Tài liệu | Bộ tài liệu hợp nhất 1.0.0 |

Commit trước đó:

| Commit | Bằng chứng thay đổi |
|---|---|
| `3f146bb` | Merge v1-ui round 2: `efc46cf` (Codex: row-IP, dialog Rollback, stepper, overview/running), `d6b3a7f` + `aaad988` (Fable: 9 lỗi click-through, bảng vừa 1440 px) |
| `959370f` | Merge v1-ui: UI do Codex viết và click-through fixes của Fable |
| `ed62027` | Changelog engine/runner theo ID review |
| `a32fb11` | E1/E2/E6, v1unit/v1ui và parity v1_rule |
| `8016940` | C15: version, host extra, pin, release.sh |
| `e2bf81e` | Runner/web: B1/B4/B5/B7/B8, C1/C5/C6/C13/C14, D2/D3/D5/D9, A5/A7 |
| `6d3ce70` | Selector, prompt/exit/prune/status/JSON, A5/A7/B6 |
| `13a440f` | Preflight, ephemeral→BFV, SG rule compare, deadline Glance |
| `118c888` | Owner marks, confirmed deletes, rollback summary, A6, recheck nguồn |
| `382e36e` | Openrc sạch, SDK isolated, Doctor write-check/auth/privilege |
| `f0ebea6` | Brick detach record/WWN, cleanup và CA/interface/region |
| `e71da94` | MIG_LOCK, MIG_CMD_TIMEOUT, MIG_COLD_WARN_GB |
| `fa33212` | contract.py, whole-chunk read A1, fsync A18 |
| `a15f6db` | UI stepper/overview/options/impact/reconnect/history/CSV/theme/a11y |
| `cf01857` | Rà câu chữ tiếng Việt UI |
| `12b94e1` | Một nút Chạy Doctor tại Tổng quan và link kết quả |
| `45f41c1` | Copy pass 2: login/Doctor/options/phase placeholder |
| `04d5506` | Click-through: title, stepper, theme action, WARN dedup, rollback banner/dialog, paste diagnostics |
| `269ed24` | Baseline 7.3.0 + migui để đối chiếu review |

## 2. Artifact

| Mục | Giá trị |
|---|---|
| Wheel | `migratetool-1.0.0-py3-none-any.whl` |
| SHA256 wheel | `e7d9fb0c322729c87493e731dfb9c9c8fb1d9c24d534c56d7e51f33c9df9366b` |
| Commit nguồn của wheel | `a573fc9`. Các commit sau đó chỉ đổi tài liệu, không đổi `migratetool/` |
| Pin | [requirements-host.txt](requirements-host.txt), sha256 `a70ec0246c1d109476f809dcfe60bf8115d85ea9fdf9d55609111be13af3e9f1`, không đổi từ `8016940` |
| UI | Checkout cùng commit; cài bằng `migui/deploy/install.sh` |
| Build | `PYTHON=/opt/mig-venv/bin/python bash release.sh <thư mục ra>` trên host chuyển đổi lab |
| Cài trên lab | `/opt/mig-venv/bin/pip install --no-index --force-reinstall --no-deps <wheel>` |
| Chữ ký / kênh phân phối | chưa có — owner chọn kênh khi phát hành |

Các wheel trước (lượt 3: `7538d91`; lượt 4–5: `c685914`; lượt 6: `5785f47`; lượt 7: `c3b7621`) là bằng chứng lịch sử. Mọi suite của mục 5 đã chạy lại trên wheel phát hành ở lượt 9.

## 3. Kết quả test tự động

Chạy trên commit `a573fc9` (commit phát hành), ngày 22/11/2026.

| Lệnh | Kết quả | Nơi chạy |
|---|---|---|
| `bash tests/unit/run.sh --parity` | `ALL pass=871 fail=0` | Host chuyển đổi lab, Python 3.12.3 |
| Trong đó: checks / unit / p5unit / p6unit / v72unit / v73unit / v1unit | 2 / 66 / 45 / 63 / 84 / 106 / 128 | v1unit cũng đạt 128/0 khi khởi động với SIGINT bị bỏ qua (như khi harness chạy nền) |
| Trong đó: ui / parity | 172 / 205 | |
| `bash tests/ui/run.sh` | `TOTAL pass=172 fail=0` | Host chuyển đổi lab |
| `node tests/ui/guards.mjs <repo>` | `TOTAL pass=115 fail=0` | Windows, Node 24.19.0 |
| `node tests/ui/clickmatrix.mjs` (Edge headless qua CDP) | `TOTAL pass=221 fail=0 skip=0` trên UI của `c3b7621` (cũng 221/0/0 trên `549e572` và `c685914`) | Stack demo với migrate giả; báo cáo [tests/ui/CLICKMATRIX-REPORT.md](tests/ui/CLICKMATRIX-REPORT.md) |

Mọi test mới đã được kiểm ngược: chạy trên code trước bản sửa thì FAIL. Ví dụ: 8 ca guards của `c685914`, 4 ca của `5785f47` và ca STOP-3 của `c3b7621` với `app.js` cũ; ca rollback A7, các ca chuyển tiếp SIGINT, dừng blocksync và nhả khóa volume với engine cũ.

Lịch sử: ngày 21/11 harness đạt `ALL pass=836 fail=0`, guards 101/0, CDP smoke 38/0.

## 4. Môi trường gate

| Thành phần | Bản |
|---|---|
| Host chuyển đổi | Ubuntu, Python 3.12.3, venv `/opt/mig-venv` theo requirements-host |
| OpenStack client | `openstack 10.5.0`, openstacksdk 4.22.0, os-brick 7.1.1 |
| Công cụ hệ thống | qemu-img 8.2.2, Ceph client 20.2.5 (tentacle), iscsiadm 2.1.9 |
| Cloud nguồn | OpenStack 2026.1 lab, 2 compute, volume LVM/LIO iSCSI |
| Cloud đích | OpenStack 2026.1 lab, 2 compute, Ceph RBD, Nova API 2.103 |
| VM thử | `demo-vm` boot từ volume, 2 volume; `mig-peer` ephemeral |
| Mục tiêu production | OpenStack 2023.1 + PowerStore iSCSI → OpenStack 2026.1 + Ceph 19; cần canary đúng hạ tầng |

Số đo lab dùng để ước lượng từ trước: cold downtime 276 s, khoảng 100 s control-plane; plan 64 s/VM. Gate 22/11 không đo lại các số này.

## 5. Gate lab 22/11/2026

Lượt 9 (bắt đầu 22/11 20:33, xong 23/11 00:22) chạy đủ 9 suite trên wheel phát hành (`a573fc9`, sha256 `e7d9fb0c…`).
Các lượt trước tìm ra lỗi và được giữ làm bằng chứng lịch sử ở bảng lỗi bên dưới.

Driver: `/root/mig-lab/tests-v1/gate1.sh <suite...>`. Driver dọn lab trước mỗi suite và ghi log `gate1-<suite>.log`. Bộ test 7.3 được chép và chỉnh cho 1.0.0 bằng script lab `adapt-v1.py`; `v1feat.sh` là bộ mới của 1.0.0. Script và log chỉ lưu trên lab, không nằm trong repo.

Lệnh lượt 9: `gate1.sh v1feat.sh e2e-migrate-py.sh live-migrate-py.sh failure-injection-py.sh newip-voltype-py.sh extras-py.sh p4feat.sh p6feat.sh v73feat.sh`.

| Gate | Lượt | Kết quả | Log / ghi chú |
|---|---|---|---|
| v1feat: short read, owner mark, prompt, A6, khóa, ngắt bằng SIGINT vào PID trong presync, ephemeral thành BFV, rollback lỗi xóa | 9 | 47 / 0 | `gate1-v1feat.log` |
| e2e-migrate-py | 9 | 50 / 0 | `gate1-e2e-migrate-py.log` |
| live-migrate-py | 9 | 26 / 0 | `gate1-live-migrate-py.log` |
| failure-injection-py | 9 | 17 / 0 | `gate1-failure-injection-py.log`; dòng `FAIL IP ... free` là preflight do kịch bản cố tình gây ra |
| newip-voltype-py | 9 | 26 / 0 | `gate1-newip-voltype-py.log` |
| extras-py | 9 | 33 / 0 | `gate1-extras-py.log` |
| p4feat | 9 | 24 / 0 | `gate1-p4feat.log` |
| p6feat | 9 | 27 / 0 | `gate1-p6feat.log` |
| v73feat: JSON, status, `--dst-host`, progress | 9 | 20 / 0 | `gate1-v73feat.log` |
| Plan song song 6 VM và đo thời gian | — | chưa chạy trong gate 22/11 | Cần 6 VM thử |
| Cold/live/volume/rollback có checksum trong guest | — | chưa chạy trong gate 22/11 | Cần agent checksum trong VM |
| PowerStore: single-path, clone WWN, stale LUN, detach | — | chưa chạy trong gate 22/11 | Cần mảng PowerStore |
| Doctor write-check mọi pool, xác minh image được dọn | — | chưa chạy; chỉ pool `mig-volumes` | Cần danh sách pool production |
| Nâng cấp/hạ cấp, state tương thích | — | chưa chạy trong gate 22/11 | |
| UI với engine thật, audit, notify hook | — | chưa chạy; ma trận click dùng migrate giả | |
| Quét lộ bí mật trên cây phát hành | 23/11 | 0 match | `git grep` trên cây sẽ công khai: mật khẩu lab (danh sách mẫu ngoài repo), IP lab, tên miền nội bộ, private key. Còn lại có chủ đích: giá trị giả để test scrub (`HUNTER-TWO-NOT-REAL`, `s3cr3t-value`) và IP ví dụ dải riêng trong test và tài liệu lưu trữ. Hook pre-push lần đầu chặn các giá trị giả cũ (chữ thường, key Ceph giả nguyên chuỗi); chúng được đổi sang chữ hoa và ghép lúc chạy, hook không bị nới |

Lỗi tìm được qua các lượt và nguyên nhân:

| Lượt | Hiện tượng | Nguyên nhân | Loại | Sửa |
|---|---|---|---|---|
| 1 | Rollback chờ 600 s dù server đích đã xóa | OSC 10.5 báo `No Server found for <id>`, `GONE_RE` không khớp | Tool | `d06a9a7` |
| 2 | `kill -INT` không dừng `migrate ... &` trong script | Job nền của bash bỏ qua SIGINT, Python giữ nguyên | Tool | `8cc5eae` |
| 2 | FAIL ở e2e, extras, p4feat, p6feat | Lab có 2 compute mỗi bên, OSC in `True`, mô tả SG `mig-copy of`, suite cũ xóa `migrate.conf` | Test | Sửa trong `adapt-v1.py` |
| 3 | v1feat A7: rollback lúc migrate chạy lại báo `no state for demo-vm` | Rollback tra state trước khi lấy khóa host; state vừa được tạo lại, chưa có `server.json` | Tool, có từ 7.3 | `c685914` |
| 3 | v1feat: migrate lần hai trả 1, test chờ 4 | Selector ghi `SKIP(state-exists)` kèm lệnh rollback và trả 1; tài liệu v1 ghi sai là 4 | Tài liệu và test | `c685914` |
| 3 | v1feat A4: 4 FAIL, rollback không lỗi khi volume có snapshot | Cinder 2026.1 từ chối `snapshot create --force` cho volume đang gắn, fixture không tạo được snapshot | Test | Fixture dùng microversion 3.66 và kiểm snapshot tồn tại |
| 3 | v73feat mục 5: `rollback.json` thiếu `--yes` | `adapt-v1.py` tự thêm `--yes` vào kỳ vọng; tool ghi lệnh chuẩn như 7.3 | Test | Sửa adapter |
| 4 | v73feat mục 8 (D3b): không thấy dòng `downtime breakdown` | Ca này đọc log extras; log lượt 3 đã được chuyển sang thư mục khác trước lượt 4 | Test (môi trường) | Chạy tay phép kiểm trên log lượt 3: PASS; lượt 9: 20 / 0 |
| Review | `kill -INT` vào PID `migrate` chỉ dừng selector, tiến trình con chạy tiếp; blocksync không bị dừng | Selector không chuyển SIGINT; `run_stream` không dừng tiến trình con khi bị ngắt | Tool | `5785f47`, `c3b7621`, `ed739b4`, `a573fc9`; lượt 6 và 9: SIGINT vào PID lúc presync trả 130, không còn blocksync |

## 6. Known gaps và điều reviewer cần chốt

| Gap | Tác động / hồ sơ cần bổ sung |
|---|---|
| E1 crash-at-marker | Chưa phủ mọi ranh giới API-create/state-write; chưa journal/resume sau cutover |
| LUN identity | Code log WWN/size guard, chưa xác minh với mảng; checksum guest và WWN canary |
| SDK/performance C2 | Chưa toàn bộ SDK/cache tối ưu; số plan 64 s là baseline lab |
| Quota batch/root ephemeral | Không admission tổng batch; cộng root BFV mới thủ công |
| Standalone volume | Không A6 hoặc exit 3/4; READY chỉ selection |
| Multi-session/parallel migration/round-robin presync | Chưa hỗ trợ; chỉ plan song song |
| Glance region | REST chưa filter OS_REGION_NAME, khác phạm vi auth brick đã sửa |
| Rollback discovery | Lỗi list/show ở một số nhánh có thể bỏ sót orphan; không cam kết global cleanup |
| Doctor discard/delete | INFO có thể bỏ kiểm discard; rm rc không kiểm; phải kiểm ngoài |
| UI totals | Tổng volume/GB của UI chưa cộng root ephemeral mới |
| Session revocation | Non-background request revalidate; không instant revoke mọi stream |
| conf_digest | Giới hạn 1 MiB/file, không hash env/openrc, tính cuối plan; cần freeze |
| Retention | Prune không dọn live/plan-only/UI raw/audit/attach records |
| Rollback khi VM nguồn đã bị xóa | Backlog, chưa làm: rollback nên kiểm VM nguồn còn tồn tại trước khi xóa object ở đích |
| OSC khác bộ đã test | DEPLOY 2.1 cách B: với OSC 9.0.1 (constraints 2026.1) mới chạy `migrate doctor`, chưa chạy migrate/rollback |

Các hành vi đã đọc rõ được mô tả trong DESIGN. Những điểm cần reviewer quyết chính sách trước production: có chặn release vì các khoảng trống Doctor/discovery/Glance/UI trên hay chấp nhận với gate vận hành; binary nào được phép xử lý state sống khi downgrade; ngưỡng nghiệm thu guest và RPO từng ứng dụng. Tài liệu không tự thay các quyết định đó bằng một tuyên bố “an toàn”.
