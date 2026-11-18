# DESIGN V7 — viết lại thành một app Python `migrate`

Trạng thái: user chốt 18/11/2026 ba quyết định gốc: (1) **viết lại Python**, (2) **thay thế hẳn**
`migrate-vm.sh`/`migrate-batch.sh`, (3) tên lệnh **`migrate`**. Nhánh `feature/v7-python-app`,
fork từ `feature/v6-batch-hint` (bản bash đầy đủ nhất — V3+V4+V5+V6 — làm chuẩn đối chiếu).

## 0. Nguyên tắc chỉ đạo: PORT, không phải sáng tác lại

Bản bash đã qua 7 vòng gate (140/0) và production thật. V7 là **bản dịch giữ nguyên hành vi**
sang Python, chia giai đoạn, mỗi giai đoạn nghiệm thu bằng CHÍNH bộ test cũ (chỉ thay cách gõ
lệnh). Mọi cải tiến ý tưởng mới (SDK thay CLI, parallel, …) để SAU khi đạt parity — không trộn
vào V7.

## 1. Hình hài app

- **Python package** `migratetool/` + `pyproject.toml`, cài vào venv sẵn có `/opt/mig-venv`
  bằng `pip install .`, console entry **`migrate`**. Python ≥3.10 (bản trong mig-venv).
- CLI một bộ parser (argparse, subcommands) — **không còn dấu `--`**:

```
migrate plan     [<vm> | --vm a,b | --host h1,h2 | --project p | --vm-file f] [options per-VM]
                 # dry-run cho 1 hay N VM — THAY LUÔN --plan-only của batch cũ
migrate migrate  [<vm> | selector như trên] [options per-VM] [--yes] [--continue-on-error]
migrate rollback <vm|volume|id>
migrate volume   [<volume> | --project p (--available|--attached|--all) | --vm a,b]
                 [--in-use] [--volume-type t] [--az z] [--verify v]
```

**Chốt bổ sung 18/11 (đợt 2, user):** KHÔNG có subcommand `batch` — selector hóa toàn bộ,
migrate 1 VM chỉ là đợt có 1 con; positional `<vm>` = shorthand của `--vm <vm>`:

- Selector giải ra N VM → N=1 hay N=100 đi cùng một đường: enumerate (--all-projects khi cần),
  bảng Selected/Plan/Summary, mỗi VM một tiến trình con + scope project riêng + log riêng,
  SKIP phân loại như batch cũ.
- **`migrate migrate` LUÔN hỏi `Type yes` trừ khi có `--yes`** — kể cả khi chỉ 1 VM. Đây là
  thay đổi hành vi CÓ CHỦ ĐÍCH so với migrate-vm.sh cũ (chạy thẳng), user chốt vì an toàn —
  ngoại lệ được phép của hợp đồng parity §2. Hệ quả: script/cron phải `--yes`; bộ test cũ khi
  sed đổi lệnh thêm `--yes` vào các dòng `migrate migrate` (thay thế máy móc, liệt kê dòng).
  stdin không phải terminal mà thiếu `--yes` → die như batch cũ.
- Volume theo đợt gộp vào `migrate volume` (không còn `--volumes` của batch cũ):
  `migrate volume --project p --available|--attached|--all`, `migrate volume --vm a,b`
  (= attached của các VM đó). Quy tắc naming/giữ tên như V3/V4.
- Một namespace option duy nhất — parser biết option nào là điều phối (selector/--yes/
  --continue-on-error) và option nào per-VM; không còn dấu `--`.
- Gõ kiểu cũ `migrate-vm.sh …` sau khi chuyển đổi: không còn (user chốt thay thế hẳn). Bản bash
  chuyển vào `archive/` như v1, kèm ghi chú commit cuối còn hỗ trợ.

## 2. Những thứ GIỮ NGUYÊN 100% (hợp đồng parity)

1. **State**: đúng layout + format file hiện tại (`/var/lib/mig-migrate/<arg>/` và `vol-<id>/`,
   từng file server.json, attachments.json, volume-map, dst-ports, dst-server-id,
   dst-server-group, clone-time, options…). App mới rollback được state do bản bash tạo và
   ngược lại — bắt buộc, vì production có state đang sống.
2. **Cách gọi hạ tầng**: phase 1–4 vẫn subprocess ra `openstack` CLI, `qemu-img`, `rbd`,
   `blocksync` (module bundled, chạy bằng python HỆ THỐNG vì cần python3-rbd — giữ nguyên ràng
   buộc này), logic brick_attach nhập thành module (nó vốn là Python). KHÔNG chuyển sang
   openstacksdk trong V7 — đổi tầng gọi API là đổi hành vi, để V8+.
3. **Output hợp đồng**: các dòng preflight `  OK/FAIL/INFO/WARN  …`, dòng log `[HH:MM:SS] …`,
   bảng batch, các message die — giữ nguyên văn các chuỗi mà test/batch/con người đang bám vào
   (danh sách chuỗi bị grep: xem DEVELOPING §4). Câu chữ khác được phép đẹp hơn (quy tắc user
   làm rõ 18/11: logic mới là bất khả xâm phạm, câu chữ thì không).
4. **Conf**: vẫn `/opt/mig/migrate.conf` bash-style KEY=VALUE + đủ các biến hiện có (kể cả
   MIG_DEFAULT_OPTS, map files) — parser Python đọc format cũ, ưu tiên CLI > env > conf > mặc
   định như cũ. File conf production không phải sửa.
5. **Toàn bộ quy tắc nghiệp vụ**: naming đích, guard trùng, scope project, appcred guard, lock
   sau detach, rollback idempotent + không đụng nguồn, plan read-only, AZ→type→pool, server
   group az-prefix, flavor map, crash-consistent in-use…

## 3. Kiến trúc module (gợi ý cho Opus, được phép tinh chỉnh)

```
migratetool/
  cli.py        # argparse subcommands, dispatch; load conf/env/defaults
  config.py     # conf file + env + MIG_DEFAULT_OPTS + 3 map (pool/az-type/flavor/project)
  clouds.py     # src()/dst() = chạy openstack CLI với env scope (SRC/DST_PROJECT), JSON parse
  state.py      # state dir, đọc/ghi từng file đúng format bash
  preflight.py  # mọi check OK/FAIL/INFO/WARN
  copyvol.py    # clone→attach(brick)→qemu-img convert/compare, progress watcher (thread)
  livesync.py   # vòng pre-sync + cutover; gọi blocksync qua system python
  server.py     # port/server create, naming, server-group, keypair, properties
  rollback.py
  batch.py      # selector, bảng, loop tuần tự gọi NỘI BỘ (không subprocess chính mình),
                #  nhưng mỗi VM vẫn chạy trong child process (multiprocessing/subprocess
                #  `migrate migrate <id>`) để GIỮ cách ly per-VM + per-project env như bash
  brick.py      # nhập từ brick_attach.py
  blocksync.py  # giữ nguyên file hiện tại (chạy bằng system python)
```

Điểm kiến trúc then chốt: batch gọi từng VM bằng **tiến trình con `migrate migrate <id>`**
(không gọi hàm trong cùng tiến trình) — giữ nguyên tính cách ly env/trap/state giữa các VM và
log mỗi con một file như bản bash. "Một app" là một CLI + một parser, không có nghĩa là một
tiến trình cho cả đợt.

## 4. Giai đoạn + nghiệm thu (gate = bộ test cũ, chỉ sửa CÁCH GÕ LỆNH, liệt kê từng dòng sửa)

| Phase | Nội dung port | Nghiệm thu |
|---|---|---|
| P1 | Skeleton + config + plan/migrate/rollback **cold** (naming, guards, state, progress, scope project) | `tests/e2e-migrate.sh` (bản sửa lệnh) 50/0; so sánh state dir với bản bash trên cùng VM |
| P2 | Live mode (livesync + blocksync) | blocksync-unit 8/0 + live 26/0 |
| P3 | ip new/addr, voltype, ephemeral, multi-NIC, flavor/SG override, properties | newip-voltype 26/0 + extras 30/0 |
| P4 | `volume` (available + --in-use), `batch`, 3 map, MIG_DEFAULT_OPTS, keep-server-group, flavor-fallback, AZ conf | port các harness feature V3–V6 (đã ghi trong 4 TEST-REPORT) chạy lại PASS đủ |
| P5 | Thay thế: install mới (pip), bash scripts → `archive/`, docs cho một app, CHANGELOG v7, TEST-REPORT tổng | Leak-check; user duyệt |

Docs P5 (user chốt 18/11): hướng dẫn app Python viết vào **file MỚI `GUIDE-PYTHON.md`**;
`GUIDE.md` cũ GIỮ NGUYÊN làm tài liệu bản bash (bản bash nằm `archive/` vẫn tra cứu được).
README trỏ cả hai, ghi rõ bản nào cho bản tool nào. DEPLOY/WORKFLOW/DEVELOPING xử tương tự
khi tới P5 sẽ hỏi user (mặc định đề xuất: cũng file -PYTHON mới, bản cũ giữ).

| P6 (feature mới, SAU parity — user chốt 18/11) | Port fidelity: port đích sao chép thuộc tính port nguồn | test plan riêng bên dưới |

### P6 — Port fidelity (chỉ app Python; bash không làm)

Hiện trạng: tool từ chối port có allowed-address-pairs từ v1, và không chép port_security_enabled
→ VM kiểu VIP/keepalived qua đích bị sai hành vi mạng. User chốt 18/11:

- Chép sang port đích: **`port_security_enabled`** (nguồn tắt → đích tạo `--disable-port-security`,
  KHÔNG gắn SG — Neutron cấm; nếu user đồng thời truyền `--security-group` → die nêu xung đột),
  **`allowed_address_pairs`** (nguyên từng cặp ip/mac), **`description`**, **`dns_name`**
  (đích không bật DNS extension thì WARN bỏ qua, không die).
- Preflight: BỎ refusal allowed-address-pairs (với `--ip same|<addr>`). Với **`--ip new`: BỎ
  pairs + WARN** (user chốt — pairs trỏ dải IP cũ, ai cần tự add).
- Port giữ chỗ phía nguồn (tạo lại khi Nova xóa port của nó giữa cutover) cũng phải mang đủ
  pairs/port-security/description như port gốc — không chỉ MAC/IP/SG như hiện tại.
- Multiple fixed-IP per port: VẪN từ chối (ngoài phạm vi).

**Copy security group (user chốt 18/11, gộp vào P6):**

- **Bỏ hẳn map-theo-tên-trùng.** Mọi SG trên port nguồn → port đích gắn **`az-<tên SG nguồn>`**
  trong project đích (quy ước prefix như server group). `az-<tên>` chưa có → tạo + copy rule;
  có rồi → dùng luôn, không so lại rule (như server group "có rồi thì thui").
- Copy rule đầy đủ: direction, ethertype, protocol, port range, remote CIDR, description.
  SG mới do Neutron tạo kèm sẵn 2 rule egress-any → XÓA trước rồi mới chép rule nguồn,
  để bộ rule đích = nguồn từng rule.
- **Rule trỏ-group (remote group): copy đệ quy SG được trỏ** thành `az-<tên nó>` rồi rule trỏ
  sang bản az- (user chốt sau khi xem bằng chứng default SG của lab). Trỏ chính mình (default)
  → trỏ sang chính `az-<tên>`. Phải chống vòng lặp (A trỏ B, B trỏ A) bằng visited set.
- `--security-group` override của user vẫn thắng: tên user đưa dùng NGUYÊN VĂN ở đích,
  không thêm prefix. Port nguồn không có SG → đích vẫn `--no-security-group`.
- Rollback: GIỮ az-SG (dùng chung giữa các VM, như server group).
- Preflight in rõ: `will create az-web-tier (5 rules, + az-db-tier referenced)` / `found az-web-tier`.
- ⚠ Đổi hành vi so với bash/P1–P5 (trước: đòi SG trùng tên có sẵn, thiếu là FAIL): các check SG
  trong suite cũ sẽ lệch — khi gate P6, những dòng suite bám tên SG cũ phải được liệt kê và
  sửa máy móc có ghi lại, như lệ sed đổi lệnh.
- Policy cần verify trên lab bằng member thật (như V3): `allowed_address_pairs` và
  `port_security_enabled` thường là admin_or_network_owner.
- Test plan: VM có pairs (mô phỏng VIP) --ip same end-to-end + rollback; VM port-security-off
  (không SG) end-to-end; nhánh reserve-port (Nova-owned) giữ pairs/PS; `--ip new` bỏ pairs +
  WARN; xung đột PS-off + --security-group die; dns_name trên đích không có extension → WARN;
  member creds probe policy; regression: VM thường không pairs/PS nguyên trạng như parity.

Mỗi phase: Opus code → Fable review diff → test lab → phase sau. Phase chưa xong thì bản bash
trên host convert KHÔNG bị gỡ (hai bản song song trên host lab; production chỉ đổi khi user quyết).

## 5. Rủi ro đã nhận diện

- Khác biệt subprocess/quoting Python vs bash (IFS, glob) → mọi lệnh gọi bằng list-args, cấm shell=True trừ chỗ bất khả kháng có comment.
- `set -e`/trap EXIT của bash → exception + context manager/finally; từng điểm die phải map đủ (đặc biệt các trap dọn clone).
- Progress watcher (đọc /sys/block) → thread daemon, phải dừng sạch mọi nhánh lỗi.
- Môi trường: `migrate` chạy trong mig-venv (có osc) nhưng blocksync cần system python3-rbd — giữ ranh giới subprocess như cũ.
- Hai bản song song trong thời gian chuyển: state format chung là phao — đã khóa ở §2.1.

## 6. Ước lượng

5 phase, mỗi phase một vòng code+review+test trọn vẹn; P1 và P4 nặng nhất. Không hứa xong trong
một phiên — chạy nhiều đợt, sau mỗi phase có báo cáo + có thể dừng/đổi hướng mà không mất gì
(bản bash còn nguyên trên nhánh v6).
