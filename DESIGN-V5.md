# DESIGN V5 — flavor fallback qua file map

Trạng thái: user chốt 18/11/2026 (cơ chế = file map, không tự tạo flavor, không copy specs;
bật = flag opt-in). Nhánh: `feature/v5-flavor-map`, fork từ `feature/v4-sg-inuse-vol`
(V4 đã test xanh, user quyết chưa merge — V5 chồng tiếp lên V4).

## 1. Vấn đề

Preflight hiện FAIL khi đích không có flavor **cùng tên** với nguồn; cách duy nhất là truyền
`--flavor` tay từng VM. Hai cloud khác bộ flavor thì batch nhiều VM phải can thiệp từng con.

## 2. Hành vi

### Flag `--flavor-fallback` (plan/migrate, mặc định TẮT)

- Không flag: hành vi y hệt V4, kể cả khi FLAVOR_MAP_FILE có khai.
- Có flag, thứ tự chọn flavor cho VM đích:
  1. `--flavor <x>` trên CLI → dùng x như hiện tại, map KHÔNG được tra (flag thành no-op, 1 dòng INFO).
  2. Đích CÓ flavor cùng tên nguồn → dùng như hiện tại, map KHÔNG được tra (đúng nghĩa *fallback*:
     chỉ kích hoạt khi thiếu).
  3. Đích THIẾU flavor cùng tên → tra map: có dòng `flavor-nguồn flavor-đích` → dùng flavor-đích;
     preflight in `INFO flavor <src> -> <dst> from FLAVOR_MAP_FILE`.
  4. Thiếu và KHÔNG có trong map → preflight FAIL như cũ, message nối thêm gợi ý
     `add a line '<src-flavor> <dest-flavor>' to $FLAVOR_MAP_FILE`.
- flavor-đích trong map phải TỒN TẠI ở đích, không thì preflight FAIL (tool không bao giờ tự tạo
  flavor — user chốt).
- Guard specs (WARN, không FAIL): flavor map ra có vCPU hoặc RAM **nhỏ hơn** flavor nguồn →
  `WARN mapped flavor <dst> is smaller than <src> (<specs>)` — VM vẫn migrate, user tự chịu.
- `--volumes-only` + flag → INFO ignored (không có server), như --server-group/--keep-server-group.
- `migrate-volume` không nhận flag (volume_options_only từ chối như các option server-only).
- MIG_DEFAULT_OPTS chứa được `--flavor-fallback` (flag không tham số — nhớ thêm vào nhánh
  option-không-tham-số của load_default_opts VÀ bộ lọc VOLPASS trong migrate-batch.sh).

### File map `FLAVOR_MAP_FILE`

- Biến conf/env mới `FLAVOR_MAP_FILE` (vào conf loader + migrate.conf.example), KHÔNG có đường
  dẫn mặc định — giống PROJECT_MAP_FILE. Flag bật mà biến rỗng/file không đọc được → die rõ ràng
  (khác project-map chỉ WARN, vì ở đây user đã chủ động bật flag).
- Format giống project-map: mỗi dòng `flavor-nguồn flavor-đích` phân cách khoảng trắng, `#` comment,
  dòng sai dạng (kể cả `a = b`) → die `expected 'source-flavor destination-flavor'`.
- Chỉ migrate-vm.sh đọc (per-VM); batch hưởng qua passthrough, mỗi VM tra map theo flavor CỦA NÓ
  → một map chung phục vụ batch trộn nhiều cỡ VM.

```
# /opt/mig/flavor-map.conf
m1.large      c2.large
m1.xlarge     c2.xlarge
legacy.tiny   m1.small
```

## 3. Những gì KHÔNG đổi

Không flag → từng byte như V4 (gate regression chứng minh). `--flavor` CLI giữ nguyên ngữ nghĩa
và luôn thắng. Không đụng naming/state/rollback — flavor chỉ ảnh hưởng lệnh `server create`,
state `options` ghi flavor cuối cùng như hiện tại.

## 4. Test plan (lab)

Gate: 5 bộ cũ nguyên văn trên build V5 (kỳ vọng sửa 0 assert).

Feature: (1) flag + map hit: nguồn flavor X không có ở đích, map X→Y → preflight INFO map,
VM đích flavor Y; rollback sạch; (2) đích CÓ flavor cùng tên → map không được tra (không INFO);
(3) flag + thiếu + không trong map → FAIL kèm gợi ý add dòng map; (4) map trỏ flavor-đích không
tồn tại → FAIL; (5) dòng map sai dạng → die; (6) KHÔNG flag + map có khai → FAIL như V4 (hành vi
cũ); (7) `--flavor` CLI + flag → dùng CLI, INFO no-op; (8) map ra flavor nhỏ hơn → WARN vẫn chạy;
(9) batch 2 VM khác flavor, 1 map chung → mỗi con đúng flavor map của nó; (10) `--volumes-only`
+ flag → INFO ignored; (11) flag bật mà FLAVOR_MAP_FILE rỗng → die; (12) `--flavor-fallback`
trong MIG_DEFAULT_OPTS → hoạt động như gõ tay, và batch volume rời không bị nuốt option.

Sau test: TEST-REPORT theo ngày (lệnh nguyên văn + kết quả); GUIDE 2b thêm mục + ví dụ;
migrate.conf.example + CHANGELOG + DEVELOPING cập nhật.

## 5. Thứ tự

1. Design này commit lên nhánh mới → Opus code (migrate-vm.sh + 1 dòng VOLPASS migrate-batch.sh).
2. Fable review → gate + feature test lab → report md → user quyết (merge hay giữ nhánh như V3/V4).
