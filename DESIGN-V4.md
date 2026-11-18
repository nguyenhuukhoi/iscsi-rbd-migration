# DESIGN V4 — migrate-volume cho volume in-use + server group aware + AZ trong conf

Trạng thái: user chốt 18/11/2026 (A+B); Feature C (AZ conf/map) user chốt cùng ngày, bổ sung sau.
Nhánh: `feature/v4-sg-inuse-vol`, fork từ `feature/batch-migrate`
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

## 2b. Feature C — AZ mặc định + map AZ→volume-type trong conf

Yêu cầu user 18/11: "AZ trong conf/map — khai AZ mặc định trong /opt/mig/migrate.conf hoặc map
AZ→volume-type→pool để khỏi truyền tay mỗi lần". Production có 2 AZ, mỗi AZ một backend/pool,
cross_az_attach=false → mỗi lần chạy đang phải truyền tay cặp `--az` + `--volume-type` khớp nhau.

### Hành vi

- 2 biến conf/env mới (vào danh sách conf loader hiện có, cùng thứ tự ưu tiên CLI > env > conf > mặc định):
  - `MIG_AZ`: AZ mặc định khi KHÔNG truyền `--az`. Truyền `--az` thì CLI thắng. Rỗng → như hiện tại.
  - `AZ_TYPE_MAP`: map AZ → volume type, format như RBD_POOL_MAP: `"AZ1:nvme-az1,AZ2:nvme-az2"`.
- Khi có AZ hiệu lực (từ `--az` hoặc `MIG_AZ`) và KHÔNG truyền `--volume-type`: tra `AZ_TYPE_MAP`;
  có entry → dùng type đó (log 1 dòng INFO `volume type <t> from AZ_TYPE_MAP (<az>)`); không có
  entry → như hiện tại (type mặc định cloud đích + `RBD_POOL` fallback).
- `--volume-type` truyền tay LUÔN thắng map. Pool vẫn resolve qua chuỗi sẵn có type→pool
  (`RBD_POOL_MAP`) — không thêm map pool mới, chuỗi đầy đủ là AZ → type (AZ_TYPE_MAP) → pool (RBD_POOL_MAP).
- Áp cho `migrate`, `plan`, `migrate-volume`. Batch hưởng tự động (mỗi lần gọi migrate-vm.sh tự
  đọc conf), không cần option batch mới.
- Preflight in rõ nguồn gốc giá trị khi lấy từ conf/map (để log tự giải thích).

### Ví dụ production (2 AZ)

```bash
# /opt/mig/migrate.conf
MIG_AZ=AZ1
AZ_TYPE_MAP="AZ1:nvme-az1,AZ2:nvme-az2"
RBD_POOL_MAP="nvme-az1:volumes-nvme-az1,nvme-az2:volumes-nvme-az2"

migrate-vm.sh migrate app-vm01                 # az=AZ1, type=nvme-az1, pool=volumes-nvme-az1 — không truyền gì
migrate-vm.sh migrate app-vm02 --az AZ2        # az=AZ2 → type=nvme-az2 theo map
migrate-vm.sh migrate app-vm03 --az AZ2 --volume-type nvme-az1   # type tay thắng map (tự chịu lệch)
```

## 2c. Fix kèm V4 — bộ lọc passthrough của migrate-batch.sh

Bug Opus phát hiện 18/11 khi code Feature B: vòng lọc VOLPASS (migrate-batch.sh:268) coi mọi
option lạ là CÓ tham số đi kèm → `-- --keep-server-group --volume-type X` làm `--keep-server-group`
nuốt luôn `--volume-type`, volume rời bị copy bằng type mặc định. Fix: thêm `--keep-server-group`
(và `--in-use` nếu lọt vào đây) vào nhánh option-không-tham-số (`--start-source|--keep-source-ports|--volumes-only`).

## 2d. Fix kèm V4 — rollback dọn được volume đích mồ côi (user chốt 18/11)

Hiện trạng (bẫy production 17/11): trên đường migrate VM, guard `not visible in pool` chạy SAU
khi tạo volume đích nhưng TRƯỚC khi ghi volume-map vào state → die vì sai RBD_POOL_MAP thì volume
vừa tạo thành mồ côi, rollback không dọn, phải xóa tay. Đường migrate-volume không dính (ghi state
trước check). Fix: trong `create_dest_volume`, ghi volume-map vào state ngay khi có ID volume đích
(trước `wait_volume`/check pool), như `create_volume_copy` đang làm. Sau fix, die ở bất kỳ điểm nào
sau khi tạo volume → `rollback <vm>` dọn hết. GUIDE 2b.1b bỏ gạch đầu dòng "xóa tay".

## 2e. Feature E — MIG_DEFAULT_OPTS: option mặc định trong conf (user chốt 18/11)

Yêu cầu user: "đưa 'lần nào cũng nhớ truyền' thành default đi; trong .conf ghi hết tất cả các
option có thể overwrite vào và nhớ mô tả".

### Hành vi

- Biến conf/env mới `MIG_DEFAULT_OPTS` (vào conf loader như các biến khác, env thắng conf):
  chuỗi option được chèn vào TRƯỚC option CLI của `plan`/`migrate`/`migrate-volume`.
  Tách token theo khoảng trắng (giá trị có khoảng trắng không hỗ trợ — ghi rõ trong docs).
- Ưu tiên: vì defaults parse trước, **CLI luôn thắng** với option có giá trị (--verify, --az...,
  gán sau đè gán trước). Tắt toàn bộ cho một lần chạy: `MIG_DEFAULT_OPTS="" migrate-vm.sh ...`.
- Xung đột mềm: `--keep-server-group` đến từ DEFAULTS + CLI truyền `--server-group x` → bỏ keep,
  log INFO, không die (cần theo dõi nguồn gốc flag, kiểu AZ_CLI). Gõ tay cả hai trên CLI → die như cũ.
- `migrate-volume`: chỉ nhận subset hợp lệ từ defaults (`--volume-type --az --verify`); token
  khác trong defaults bị bỏ kèm 1 dòng INFO (không die — defaults là policy chung cho cả VM lẫn
  volume). `--in-use` KHÔNG nhận từ defaults (quyết định nguy hiểm, phải gõ tay).
- Token không phải option hợp lệ → die nêu rõ `MIG_DEFAULT_OPTS` và token sai (bắt typo sớm).
- Preflight in 1 dòng INFO liệt kê defaults đã áp (tự giải thích log).
- rollback không nhận defaults (rollback takes no options — giữ nguyên).
- Batch không đổi: từng lần gọi migrate-vm.sh tự đọc conf nên defaults áp tự động.

### migrate.conf.example (file mới trong repo)

Template đầy đủ MỌI biến conf, mỗi biến một dòng mô tả + giá trị ví dụ/TEST-NET, nhóm theo:
kết nối (SRC_RC DST_RC), Ceph (RBD_POOL RBD_POOL_MAP RBD_USER CEPH_CONF), host chuyển
(MIG_ISCSI_IP MIG_MULTIPATH COPY_STREAMS), project (SRC_PROJECT DST_PROJECT PROJECT_MAP_FILE),
AZ (MIG_AZ AZ_TYPE_MAP), defaults (MIG_DEFAULT_OPTS). DEPLOY 7b + GUIDE 2b.1 trỏ tới file này
thay vì chép template dài.

### Test plan bổ sung

(20) conf `MIG_DEFAULT_OPTS="--keep-server-group --verify none"` → plan hiện cả hai + INFO nguồn
gốc; `--verify full` CLI thắng; `MIG_DEFAULT_OPTS=""` env tắt hết. (21) default keep-server-group
+ CLI `--server-group <x>` → INFO bỏ keep, dùng x, không die. (22) `migrate-volume` với defaults
chứa option VM-only → INFO bỏ, chạy tiếp. (23) token rác trong MIG_DEFAULT_OPTS → die nêu tên biến.

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

Feature C: (15) `MIG_AZ` trong conf, không truyền `--az` → plan/preflight ra đúng AZ; truyền
`--az` khác → CLI thắng; (16) `AZ_TYPE_MAP` chọn type khi không truyền `--volume-type`, volume
rơi đúng pool theo RBD_POOL_MAP; `--volume-type` tay thắng map; (17) AZ không có trong map →
chạy như không có map. Fix 2c: (18) batch `--volumes available` + `-- --keep-server-group
--volume-type X` → volume đích đúng type X (không bị nuốt option). Fix 2d: (19) migrate với
RBD_POOL_MAP cố tình sai → die sau khi tạo volume đích; `rollback <vm>` dọn volume đó + nguồn
về nguyên trạng, đích sạch.

Docs đợt V4 (sau khi test xanh): GUIDE 2b thêm `--in-use`/`--keep-server-group`/`MIG_AZ`/
`AZ_TYPE_MAP`; thêm giải thích RBD_POOL_MAP là map volume-type→pool (tool ghi thẳng RBD nên phải
biết Cinder đổ type nào vào pool nào; kèm cách tra ngược và lỗi "not visible in pool" khi map sai)
— user yêu cầu 18/11.

Sau test: cập nhật TEST-REPORT (file mới theo ngày chạy) đủ lệnh nguyên văn + kết quả; GUIDE
mục 2b thêm 2 flag; DEPLOY nếu đụng.

## 5. Thứ tự làm

1. Design này commit lên nhánh mới → Opus code (A + B một đợt, cùng file migrate-vm.sh).
2. Fable review diff → gate + feature test trên lab → report md.
3. User quyết merge (V3 + V4 có thể merge chung hoặc lần lượt — V4 chồng lên V3).
