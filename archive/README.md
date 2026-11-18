# archive/

Mã các bản cũ, giữ nguyên từng byte để tra cứu, so sánh và quay về khi cần.

## Bản bash v2 → v6: thay bằng app Python `migrate` từ v7

| File | md5 | Commit cuối đổi nội dung |
|---|---|---|
| `migrate-vm.sh` | `5706e763db1474b1fd5312ca0e7dbfdb` | `0c49e3c` (V5, flavor fallback) |
| `migrate-batch.sh` | `2a3189f1ce6f4e811f98f9489fa91fe9` | `aa457dd` (V6, batch hint) |
| `brick_attach.py` | `ce2512f892c69b03e8c06a04637adbcc` | `726d968` (bản production) |
| `blocksync.py` | `ae9cd19fc921508b311d79242e9bb36d` | `726d968` (bản production) |

- **Bản bash dừng ở commit `6b6c48b`** ("V6 wrap-up", nhánh `feature/v6-batch-hint`). Đó là bản bash đầy
  đủ nhất (V3 + V4 + V5 + V6), qua 7 vòng regression gate 140/0, và là chuẩn đối chiếu của V7.
- Nhánh `feature/v7-python-app` fork từ commit đó. 4 file trên được giữ nguyên ở gốc repo suốt P1–P4 để
  làm chuẩn so sánh. Tới P5 chúng được `git mv` vào đây, không sửa byte nào, như `migrate-vm-v1` từng làm.
- **App Python `migrate` thay thế từ v7.** Hướng dẫn: [GUIDE-PYTHON.md](../GUIDE-PYTHON.md), cài đặt:
  [DEPLOY-PYTHON.md](../DEPLOY-PYTHON.md).
- `migratetool/blocksync.py` của app giống từng byte `blocksync.py` ở đây. `migratetool/brick.py` là bản
  port của `brick_attach.py`.
- Tài liệu của bản bash ([GUIDE.md](../GUIDE.md), [DEPLOY.md](../DEPLOY.md), [WORKFLOW.md](../WORKFLOW.md),
  [DEVELOPING.md](../DEVELOPING.md)) giữ nguyên như lúc viết. Lệnh cài trong đó (`install -m 0755
  migrate-vm.sh …`) chạy ở gốc repo của commit `6b6c48b`. Với repo hiện tại thì cài từ thư mục này:

```bash
install -m 0755 archive/migrate-vm.sh archive/migrate-batch.sh archive/brick_attach.py archive/blocksync.py /usr/local/bin/
```

State của hai bản dùng chung format, nên bản bash cài từ đây vẫn rollback được các lần chạy của app, và
ngược lại.

## Bản v1

`migrate-vm-v1-20261105.sh`: bản v1 (05/11/2026, cold, giữ IP/MAC), có trước khi lập repo. Test:
[TEST-REPORT-20261105.md](../TEST-REPORT-20261105.md).
