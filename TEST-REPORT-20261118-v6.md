# Test report V6 — 2026-11-18 (nhánh feature/v6-batch-hint)

Build: `migrate-batch.sh` md5 `2a3189f1` = commit `aa457dd`; `migrate-vm.sh` md5 `5706e763`
**không đổi** so với V5 ⇒ regression gate vòng 7 (140/0, [TEST-REPORT-20261118-v5.md](TEST-REPORT-20261118-v5.md))
còn nguyên hiệu lực, không chạy lại. Design: [DESIGN-V6.md](DESIGN-V6.md). V6 chỉ đổi NỘI DUNG
message lỗi của batch (3 hint mới), không đổi logic/exit code — theo quy tắc user làm rõ 18/11:
"không phá tính năng cũ" áp cho logic, không áp cho câu chữ (trừ chuỗi bị grep — 2 message cũ
được giữ nguyên văn và có case khẳng định).

Harness giả lập của Opus: 8/8 (gồm 2 case thêm: `Plan` viết hoa không ra hint; `--vm plan` hiểu
`plan` là tên VM). Lab thật (log: scratchpad `v4feat/v6-lab.txt`):

| # | Case design | Lệnh | Kết quả |
|---|---|---|---|
| 1 | hint plan | `migrate-batch.sh plan demo-vm` | PASS — die `migrate-batch.sh has no 'plan' command — use: migrate-vm.sh plan <vm>, or: migrate-batch.sh --vm <vm> --plan-only` |
| 2 | hint migrate | `migrate-batch.sh migrate demo-vm` | PASS — die hint migrate |
| 3 | hint rollback | `migrate-batch.sh rollback demo-vm` | PASS — die hint rollback |
| 4 | message cũ giữ nguyên văn | `migrate-batch.sh --vm demo-vm --az x --yes` | PASS — `ERROR: unknown option --az (options for migrate-vm.sh go after --)` đúng từng chữ |
| 5 | message cũ giữ nguyên văn | `migrate-batch.sh --bogus` | PASS — đúng từng chữ |
| 6 | smoke đường thật | `migrate-batch.sh --vm demo-vm --plan-only` | PASS — rc=0, bảng `PLAN-OK` cho demo-vm |

Tổng: **6/6 lab + 8/8 giả lập**. Lab resting state không đổi (chỉ plan, không migrate gì).
