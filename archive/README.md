# Lưu trữ

Thư mục này chứa mã Bash tham chiếu và tài liệu đã nghỉ. Tài liệu vận hành hiện hành bắt đầu từ [README](../README.md), thiết kế chuẩn là [DESIGN](../DESIGN.md). Không dùng câu lệnh trong archive để điều hành release 1.0.0.

## Mã tham chiếu

| File | md5 lưu trong hồ sơ | Commit cuối đổi nội dung |
|---|---|---|
| `migrate-vm.sh` | `7d9218b436e72e67f1cac31abdb36500` (trước 22/11: `5706e763db1474b1fd5312ca0e7dbfdb`) | `0c49e3c` |
| `migrate-batch.sh` | `2a3189f1ce6f4e811f98f9489fa91fe9` | `aa457dd` |
| `brick_attach.py` | `ce2512f892c69b03e8c06a04637adbcc` | `726d968` |
| `blocksync.py` | `ae9cd19fc921508b311d79242e9bb36d` | `726d968` |

Ngày 22/11/2026, địa chỉ mạng iSCSI của lab trong `migrate-vm.sh`, `migrate-vm-v1-20261105.sh`, `docs/GUIDE-bash.md` và `docs/POWERSTORE-VS-LAB.md` được thay bằng dải tài liệu `198.51.100.x`, và giá trị mật khẩu giả trong `docs/DESIGN-UI.md` được đổi sang chữ hoa `ABC-NOT-A-REAL-ONE` (để hook quét lộ bí mật không chặn); ngoài các dòng đó các file giữ nguyên.

Bash dừng ở `6b6c48b`; harness parity dùng reference này cùng normalization có ID. Các file mã archive không được sửa để làm test xanh. `migratetool/blocksync.py` đã khác reference bởi A1 read_exact; không còn yêu cầu byte-identical. Không suy từ format state quen thuộc rằng binary Bash có thể hoàn tác an toàn mọi state 1.0.0.

`migrate-vm-v1-20261105.sh` là bản trước repo. Lịch sử sản phẩm nằm trong [CHANGELOG](../CHANGELOG.md); hướng dẫn phát triển reference/parity nằm trong [DEVELOPING](../DEVELOPING.md).

## Tài liệu cũ

Giữ nguyên byte nội dung 20 tài liệu khi chuyển vào `docs/`. Ngày, proposal chưa thực hiện, địa chỉ lab, các liên kết tương đối và khuyến nghị cũ trong đó là hồ sơ lịch sử, không phải chuẩn hiện hành. Dùng bảng này để tìm phần đã hợp nhất; không sửa archive để làm nó trông như tài liệu mới.

| Đường dẫn cũ | Bản lưu | Nội dung hiện hành |
|---|---|---|
| `DESIGN-V2.md` | [DESIGN-V2.md](docs/DESIGN-V2.md) | [DESIGN](../DESIGN.md): cold/live/IP/type-pool |
| `DESIGN-V3.md` | [DESIGN-V3.md](docs/DESIGN-V3.md) | [DESIGN](../DESIGN.md), [GUIDE](../GUIDE.md): selector/project/volume/naming |
| `DESIGN-V4.md` | [DESIGN-V4.md](docs/DESIGN-V4.md) | [DESIGN](../DESIGN.md), [GUIDE](../GUIDE.md): in-use, group, AZ/default |
| `DESIGN-V5.md` | [DESIGN-V5.md](docs/DESIGN-V5.md) | [GUIDE](../GUIDE.md): flavor fallback |
| `DESIGN-V6.md` | [DESIGN-V6.md](docs/DESIGN-V6.md) | [GUIDE](../GUIDE.md), [RUNBOOK](../RUNBOOK.md): batch/hints |
| `DESIGN-V7.md` | [DESIGN-V7.md](docs/DESIGN-V7.md) | [DESIGN](../DESIGN.md), [DEVELOPING](../DEVELOPING.md): architecture/parity |
| `DESIGN-V7-2.md` | [DESIGN-V7-2.md](docs/DESIGN-V7-2.md) | [DESIGN](../DESIGN.md): preflight/quota/Glance/doctor |
| `DESIGN-V7-3-CLI.md` | [DESIGN-V7-3-CLI.md](docs/DESIGN-V7-3-CLI.md) | [DESIGN](../DESIGN.md): JSON/effective/progress/CLI contract |
| `DESIGN-UI.md` | [DESIGN-UI.md](docs/DESIGN-UI.md) | [DESIGN](../DESIGN.md): web/runner/T1–T17; [UI](../UI.md): thao tác |
| `GUIDE-PYTHON.md` | [GUIDE-PYTHON.md](docs/GUIDE-PYTHON.md) | [GUIDE](../GUIDE.md), [UI](../UI.md) |
| `DEPLOY-PYTHON.md` | [DEPLOY-PYTHON.md](docs/DEPLOY-PYTHON.md) | [DEPLOY](../DEPLOY.md) |
| `WORKFLOW-PYTHON.md` | [WORKFLOW-PYTHON.md](docs/WORKFLOW-PYTHON.md) | [DESIGN](../DESIGN.md): luồng; [RUNBOOK](../RUNBOOK.md): chiến dịch |
| `DEVELOPING-PYTHON.md` | [DEVELOPING-PYTHON.md](docs/DEVELOPING-PYTHON.md) | [DEVELOPING](../DEVELOPING.md) |
| `GUIDE.md` (Bash) | [GUIDE-bash.md](docs/GUIDE-bash.md) | [GUIDE](../GUIDE.md): vận hành CLI hiện hành |
| `DEPLOY.md` (Bash) | [DEPLOY-bash.md](docs/DEPLOY-bash.md) | [DEPLOY](../DEPLOY.md): prerequisites/openrc/keyring/iSCSI |
| `WORKFLOW.md` (Bash) | [WORKFLOW-bash.md](docs/WORKFLOW-bash.md) | [DESIGN](../DESIGN.md), [RUNBOOK](../RUNBOOK.md) |
| `DEVELOPING.md` (Bash) | [DEVELOPING-bash.md](docs/DEVELOPING-bash.md) | [DEVELOPING](../DEVELOPING.md) |
| `POWERSTORE-VS-LAB.md` | [POWERSTORE-VS-LAB.md](docs/POWERSTORE-VS-LAB.md) | [DEPLOY](../DEPLOY.md), [RUNBOOK](../RUNBOOK.md): single-path/WWN/canary |
| `AUDIT-20261119.md` | [AUDIT-20261119.md](docs/AUDIT-20261119.md) | [DESIGN](../DESIGN.md): hiện trạng; [CHANGELOG](../CHANGELOG.md): lịch sử sửa |
| `migui/deploy/DEPLOY-UI.md` | [DEPLOY-UI.md](docs/DEPLOY-UI.md) | [DEPLOY](../DEPLOY.md): cài; [UI](../UI.md): sử dụng |

[REVIEW-20261121.md](../REVIEW-20261121.md) vẫn ở root để truy vết review IDs. Các entry CHANGELOG cũ, test và comment static vẫn có tên tài liệu lịch sử; tra đúng bản ở bảng này. Screenshots hiện hành được tham chiếu trong [UI.md](../UI.md).
