# DESIGN V4 — migrate-volume cho volume in-use + server group aware

Trạng thái: user chốt 18/11/2026. Nhánh: `feature/v4-sg-inuse-vol`, fork từ `feature/batch-migrate`
(cần code V3: `migrate-volume`, scoping, naming). Quy ước chung V3 (naming, guard, state, rollback,
conf) giữ nguyên — V4 chỉ THÊM, không đổi hành vi có sẵn.

## 1. Feature A — `migrate-volume --in-use`: copy volume đang gắn, không tắt VM

Yêu cầu user: "Không tắt, chấp nhận bản copy tại một thời điểm" (crash-consistent).

### Hành vi

- `migrate-vm.sh migrate-volume <volume> --in-use [--volume-type t] [--az z] [--verify v]`
  chấp nhận volume `in-use`. **Không có `--in-use` thì volume in-use vẫn bị từ chối như V3**
  (message hiện tại nối thêm: `or copy it live with --in-use (crash-consistent)`).
- Luồng y hệt migrate-volume available: clone volume nguồn (Cinder clone được volume đang gắn,
  crash-consistent tại thời điểm clone) → tạo volume đích → attach clone vào host chuyển →
  `qemu-img convert` → compare (trừ `--verify none`) → xóa clone.
  **VM nguồn và volume nguồn không bị đụng: không stop, không lock, không detach.**
- Tên volume đích: **giữ nguyên tên nguồn** (chốt 18/11 — nhất quán migrate-volume; tên rỗng xử
  như V3). Description `migrated from <src-id>` giữ nguyên vai trò guard chống copy trùng.
- Output BẮT BUỘC nói rõ tính chất bản copy, in cả mốc thời gian clone:

```
Volume copied crash-consistent as of <ISO-time of clone>: data written after that moment
is NOT in the copy (like a sudden power-off; a busy DB will need crash recovery on first mount).
Source VM and volume untouched.
```

- Rollback: không đổi — state `vol-<src-id>`, xóa volume đích + clone sót, không đụng nguồn.
- `--in-use` trên volume `available` thì bị bỏ qua kèm 1 dòng INFO (không die — tiện cho script).

### Từ chối / giới hạn

- Vẫn từ chối như V3: volume mã hóa, multiattach, trạng thái khác available/in-use.
- `--in-use` chỉ thuộc `migrate-volume`. `migrate`/`plan`/batch không nhận.
- Batch KHÔNG đổi đợt này: `--volumes available` vẫn chỉ volume rời; muốn copy nóng volume của
  VM đang chạy thì gọi `migrate-volume --in-use` từng volume. (Mở rộng
  `--volumes in-use` cho batch để dành vòng sau nếu user cần.)
- Snapshot tree của volume không đi theo (như V3).

## 2. Feature B — `--keep-server-group`: giữ nhóm server group khi migrate instance

Yêu cầu user: VM nguồn đang ở server group nào thì qua đích các member vẫn nằm cùng group.
Group ở đích: tên = **`az-` + tên group nguồn** (prefix cố định, chốt 18/11); chưa có thì tool
TẠO, có rồi thì dùng luôn; VM đích được add vào lúc tạo server.

### Hành vi

- Flag mới `--keep-server-group` cho `plan`/`migrate` (VM path). Xung đột với `--server-group`
  → die `choose one: --server-group <name> or --keep-server-group`.
- Tra group nguồn: `server group list` trong scope project NGUỒN, tìm group có members chứa ID
  của VM. VM không thuộc group nào → INFO `source VM is in no server group` và chạy tiếp như
  không có flag (no-op, không FAIL — để batch passthrough áp đại trà được).
- Tên group đích: `az-<tên group nguồn>` (vd nguồn `web-anti` → đích `az-web-anti`).
- Preflight đích (scope project ĐÍCH):
  - Group `az-<tên>` đã có + **policy khớp nguồn** → `OK`, dùng luôn ("có rồi thì thui").
  - Đã có nhưng **policy khác** → `FAIL` in policy 2 bên (chốt 18/11 — add nhầm anti↔affinity
    là sai nguy hiểm). Muốn ép thì dùng `--server-group <id>` thủ công.
  - Có ≥2 group trùng tên `az-<tên>` trong project đích → `FAIL ambiguous`, dùng `--server-group <id>`.
  - Chưa có → preflight báo `OK will create az-<tên> (policy <p>)`. **`plan` không tạo gì**
    (plan phải read-only); `migrate` tạo group ngay trước khi tạo server.
- Tạo group: copy `policy` của group nguồn (microversion cũ 2023.1 trả `policies` dạng list →
  lấy phần tử đầu); có `rules` (vd `max_server_per_host`) thì copy theo, client không hỗ trợ
  thì WARN và tạo không rules.
- Add member: như `--server-group` hiện có — `server create --hint group=<id>` (Nova không cho
  add server đã tồn tại vào group, nhưng VM đích luôn tạo mới nên không vướng).
- Batch: không cần option batch mới — passthrough `-- --keep-server-group` áp cho mọi VM, mỗi VM
  theo group CỦA NÓ. Batch chạy tuần tự nên VM đầu tạo group, các VM sau thấy có sẵn → cùng group.
- Rollback: xóa VM đích như thường; **group tự tạo KHÔNG bị xóa** (có thể member khác đang dùng;
  group rỗng vô hại, lần migrate sau dùng lại). State ghi `dst-server-group` (id + created=yes/no)
  để tra cứu; rollback chỉ log INFO nhắc group còn đó.
- `--volumes-only` + `--keep-server-group`: INFO ignored (không có server) — như `--server-group`.
- Anti-affinity thiếu host → NoValidHost lúc tạo server như V3: rollback, preflight không đoán
  scheduler.

## 3. Những gì KHÔNG đổi

Toàn bộ V1–V3: luồng migrate/plan/rollback, naming, guard, conf, batch selectors, `--volumes`,
progress, appcred guard. Hai feature đều opt-in bằng flag; không truyền flag thì build V4 chạy
y hệt build V3 (gate regression phải chứng minh điều này).

## 4. Test plan (lab, trước khi báo user)

Gate: 5 bộ cũ nguyên văn trên build V4 (blocksync-unit, e2e, live, newip-voltype, extras) — chỉ
được sửa assert thuộc thay đổi đã duyệt (V4 không đổi hành vi cũ → kỳ vọng sửa 0 dòng).

Feature A: (1) `--in-use` copy volume đang gắn VM ACTIVE → VM vẫn ACTIVE suốt, volume đích đúng
tên + description, compare PASS; (2) không flag → die với message mới; (3) `--in-use` trên volume
available → INFO + chạy như thường; (4) copy trùng → guard FAIL; (5) rollback xóa copy, nguồn
nguyên; (6) ghi dữ liệu trong guest NGAY TRƯỚC clone → có trong copy; ghi SAU clone → không có
(chứng minh mốc crash-consistent).

Feature B: (7) VM trong group `web-anti` (soft-anti-affinity, vì đích lab ít host), đích chưa có
→ migrate tạo `az-web-anti` đúng policy, VM đích là member; (8) VM thứ 2 cùng group nguồn →
dùng lại `az-web-anti`, 2 member; (9) tạo trước `az-web-anti` policy khác → preflight FAIL;
(10) VM không thuộc group → INFO, migrate bình thường; (11) `--keep-server-group --server-group x`
→ die; (12) `plan --keep-server-group` khi đích chưa có group → báo will-create nhưng KHÔNG tạo
(list group trước/sau giống nhau); (13) rollback không xóa group; (14) batch 2 VM passthrough
`-- --keep-server-group` → cùng group đích.

Sau test: cập nhật TEST-REPORT (file mới theo ngày chạy) đủ lệnh nguyên văn + kết quả; GUIDE
mục 2b thêm 2 flag; DEPLOY nếu đụng.

## 5. Thứ tự làm

1. Design này commit lên nhánh mới → Opus code (A + B một đợt, cùng file migrate-vm.sh).
2. Fable review diff → gate + feature test trên lab → report md.
3. User quyết merge (V3 + V4 có thể merge chung hoặc lần lượt — V4 chồng lên V3).
