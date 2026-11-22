# DESIGN V6 — hint khi gõ lệnh con migrate-vm.sh vào migrate-batch.sh

Trạng thái: user chốt 18/11/2026 ("làm nhưng ở nhánh mới"). Nhánh `feature/v6-batch-hint`,
fork từ `feature/v5-flavor-map`. Xuất phát: 2 lỗi cú pháp user gặp trên production 18/11
(`migrate-batch.sh plan <vm>` và `--az` đặt trước `--`) — lỗi thứ hai message hiện tại đã đủ rõ,
lỗi thứ nhất thì chưa chỉ đường.

## 1. Hành vi

Chỉ đổi NỘI DUNG message lỗi của `migrate-batch.sh`, không đổi exit code, không đổi parse:

- Token không nhận diện được là `plan` | `migrate` | `rollback` (người dùng tưởng batch có lệnh
  con như migrate-vm.sh) → die với hint theo từng từ:
  - `plan`     → `ERROR: migrate-batch.sh has no 'plan' command — use: migrate-vm.sh plan <vm>, or: migrate-batch.sh --vm <vm> --plan-only`
  - `migrate`  → `... — use: migrate-vm.sh migrate <vm>, or: migrate-batch.sh --vm <vm> --yes`
  - `rollback` → `... — use: migrate-vm.sh rollback <vm|id> (with the SRC_PROJECT/DST_PROJECT pair; batch prints the exact command for FAIL and SKIP(state-exists) rows)`
- Mọi token lạ khác: message hiện tại giữ NGUYÊN VĂN (`unknown option X (options for
  migrate-vm.sh go after --)`) — không đổi, test cũ/thói quen grep không gãy.

## 2. Những gì KHÔNG đổi

`migrate-vm.sh` không đụng (md5 giữ `5706e763`) ⇒ gate vòng 7 còn nguyên hiệu lực, KHÔNG cần
chạy lại 5 bộ gate. Batch: mọi đường chạy thật (selector, plan, migrate, volumes, map) không đổi.

## 3. Test plan (lab, nhanh)

(1) `migrate-batch.sh plan demo-vm` → die + hint plan; (2) `migrate-batch.sh migrate demo-vm`
→ hint migrate; (3) `migrate-batch.sh rollback demo-vm` → hint rollback; (4) `migrate-batch.sh
--vm demo-vm --az x --yes` → message cũ nguyên văn (không hint); (5) `migrate-batch.sh --bogus`
→ message cũ nguyên văn; (6) smoke đường thật: `migrate-batch.sh --vm demo-vm --plan-only` rc=0
PLAN-OK. Kết quả ghi thêm vào TEST-REPORT-20261118-v5.md (mục V6) hoặc report mới theo ngày.
