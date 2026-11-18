# Test report V7 (app Python `migrate`) — cập nhật theo phase (nhánh feature/v7-python-app)

Design: [DESIGN-V7.md](DESIGN-V7.md). Chuẩn đối chiếu: bản bash V6 (`migrate-vm.sh` md5 `5706e763`,
`migrate-batch.sh` `2a3189f1`) — không bị sửa trong suốt V7.

## Phase 1 — skeleton + đường cold (commit `e0a41a1`) — ĐẠT 2026-11-18

Package `migratetool/` 2162 dòng, entry `migrate`, cài `pip install --no-build-isolation .`
vào `/opt/mig-venv`.

### Parity giả lập (Opus, stub openstack/qemu-img/rbd/brick, không gọi cloud)

So bash vs Python trên cùng "world", 6 chiều: stdout (chuẩn hóa giờ), stderr, exit code, mọi
file state, **toàn bộ chuỗi lệnh gọi ra ngoài theo thứ tự**, world cuối.

| Bộ | Kết quả |
|---|---|
| Parity 44 kịch bản (plan/migrate/rollback cold, conf+defaults, keep-sg 4 trạng thái, flavor-fallback 4 trạng thái, 11 điểm die CLI, die giữa chừng: sai pool map / volume error / compare lệch — đều rollback được) | 44/44 giống hệt cả 6 chiều |
| Rollback chéo bản: bash migrate → Python rollback và ngược lại (kể cả volume mồ côi, keep-sg) | 4/4 |
| Unit (conf reader, precedence, not-ported, help/version) | 23/23 |

### Lab thật (mig-host)

| # | Case | Lệnh | Kết quả |
|---|---|---|---|
| 1 | State + output parity trên cloud thật | `migrate-vm.sh plan demo-vm --verify none` rồi `migrate plan demo-vm --verify none`, diff state dir + diff output (bỏ timestamp) | PASS — `STATE-IDENTICAL` + `OUTPUT-IDENTICAL` (từng byte) |
| 2 | Gate P1: e2e cold NGUYÊN BẢN, chỉ sed đổi lệnh (`migrate-vm.sh migrate/rollback ` → `migrate migrate/rollback ` — diff đúng 2 dòng, sinh máy bằng sed, file `/root/mig-lab/tests/e2e-migrate-py.sh`) | `bash /root/mig-lab/tests/e2e-migrate-py.sh` | **PASS=50 FAIL=0 WARN=0** — migrate cold + rollback thật trên demo-vm bằng app Python |

### 16 điểm lệch bash Opus tự khai — Fable duyệt chấp nhận cả 16 (2026-11-18)

Nhóm chính: message lỗi rõ hơn (quy tắc 18/11: câu chữ tự do, logic bất biến); 2 chỗ SỬA bug
tiềm ẩn của bash (rollback ≥2 clone sót cùng tên — bash truyền chuỗi nhiều dòng làm 1 đối số;
property chứa `\`/tab bị `@tsv` của bash làm hỏng); conf reader Python chặt hơn (chỉ KEY=VALUE,
gặp bash phức tạp thì die kèm số dòng — **phải soát conf production trước khi chuyển**); openrc
source 1 lần/lần chạy (rc có prompt sẽ khác — production không có); watcher %/JSON format/env
tiến trình con sẽ nghiệm thu tiếp ở các phase sau trên thiết bị thật.

### Chưa port (die rõ `not ported yet`, không để state chặn)

`--mode live` (P2), ephemeral migrate (P3), `migrate volume`, `migrate batch`, rollback state
volume (P4).

## Phase 2 — live mode (commit `1fb2e88`) — ĐẠT 2026-11-18

Bundle `blocksync.py` vào package (unit test khóa md5 giống từng byte bản gốc), chạy qua
`SYS_PY -I` (python hệ thống, cách ly sys.path khỏi package). Parity giả lập: 65/65 trước khi
thêm `-I` + chạy lại 21/21 sau, unit 25/25 — phủ 17 kịch bản live (hội tụ/không hội tụ/max-passes/
3 chế độ verify/blocksync chết giữa pre-sync lẫn cutover/churn 0/rollback chéo bash↔py).
Cải thiện có chủ đích: chặn `--max-passes`/`--delta-limit` không phải số (bash cũ LẶP VÔ HẠN với
input rác); blocksync in rác thay JSON → die kèm nội dung (bash ra lỗi jq khó hiểu).

### Lab thật

| # | Case | Lệnh | Kết quả |
|---|---|---|---|
| 3 | Preflight live qua cầu bundled blocksync + system python | `migrate plan demo-vm --mode live --verify none` | PASS — `OK blocksync present` + `OK librbd python bindings`, Preflight passed |
| 4 | blocksync-unit NGUYÊN BẢN (file gốc, python hệ thống) | `bash /root/mig-lab/tests/blocksync-unit.sh` | PASS=8 FAIL=0 |
| 5 | Gate P2: live suite NGUYÊN BẢN, sed đổi lệnh (2 dòng, `/root/mig-lab/tests/live-migrate-py.sh`) | `bash /root/mig-lab/tests/live-migrate-py.sh` | **PASS=26 FAIL=0** — pre-sync + cutover + rollback thật bằng app Python |

Ghi chú harness: lần chạy đầu 2 bộ này ĐỎ OAN do wrapper của Fable — (a) activate mig-venv làm
`#!/usr/bin/env python3` của blocksync trỏ vào python venv không có librbd (8 FAIL); (b) lệnh
smoke `migrate plan --mode live` để sót state dir làm live suite fail check "no leftover state"
(bash plan cũng ghi state y vậy). Dọn + chạy lại không venv → xanh như trên; code không đổi.

## Phase 2b — CLI thống nhất (user chốt 18/11 đợt 2, commit design `18b4f75`)

Bỏ subcommand `batch`: selector hóa (`--vm/--host/--project/--vm-file`, positional = shorthand);
`migrate plan` thay `--plan-only`; `migrate migrate` LUÔN hỏi xác nhận trừ `--yes` (đổi hành vi
có chủ đích, an toàn hơn bash); volume theo đợt gộp vào `migrate volume`. Thực thi ở P4.

## Phase 3 — ephemeral + newip/extras (commit `f584bd5`) — ĐẠT 2026-11-18

Port đường ephemeral: snapshot → Glance REST (urllib, đúng trình tự curl của bash: POST record,
PUT stream, chờ active, so sha512, JSON-patch allowlist property) → server image-backed; dọn
`mig-snap-*`. Parity giả lập 86/86 + unit 25/25, trong đó Glance GIẢ là HTTP server thật tính
sha512 và ghi từng request — curl (bash) với urllib (Python) bị so TỪNG LỜI GỌI REST. 12 kịch bản
ephemeral + 3 rollback chéo + 6 kịch bản hình dạng suite. Lệch có chủ đích (duyệt): message lỗi
REST rõ hơn (POST fail: bash exit 22 của curl im lặng, Python ERROR + exit 1).

### Lab thật

| # | Case | Lệnh | Kết quả |
|---|---|---|---|
| 6 | newip-voltype NGUYÊN BẢN sed đổi lệnh (`newip-voltype-py.sh`) | `bash /root/mig-lab/tests/newip-voltype-py.sh` | **PASS=26 FAIL=0** |
| 7 | extras NGUYÊN BẢN sed đổi lệnh (`extras-py.sh`) — multi-NIC, flavor/SG override, properties, **ephemeral qua Glance thật** | `bash /root/mig-lab/tests/extras-py.sh` | **PASS=30 FAIL=0** |

**Cộng dồn P1–P3: app Python qua đủ 140/140 check của 5 suite cũ** (e2e 50 + blocksync 8 +
live 26 + newip 26 + extras 30).

## Phase 4 — volume + batch + maps + selector CLI (commit `8a48c45`) — ĐẠT 2026-11-18

Port nốt: `migrate volume` (available/--in-use/selector --project/--vm), selector hóa migrate/plan
(`--vm/--host/--project/--vm-file`, positional = shorthand), **luôn hỏi `Type yes` trừ `--yes`**,
3 map + MIG_DEFAULT_OPTS qua selector, rollback volume state. Batch chạy mỗi VM bằng tiến trình
con `python -m migratetool.single` (giữ cách ly env/project + log riêng `/var/log/mig-batch/<ts>/`
như bash). Parity giả lập 141/141 (so cả file log từng con với batch bash) + unit 65/0.

### 10 điểm lệch Opus tự khai — Fable duyệt chấp nhận cả 10 (2026-11-18)

Đáng nói nhất: **⚠ `migrate migrate <vm>` (1 VM) giờ auto-scope SRC/DST_PROJECT theo project
của VM** — bash cũ chạy admin mà quên set env thì VM đích rơi vào project admin; giờ VM đích vào
project cùng tên (map được qua PROJECT_MAP_FILE). Appcred dùng được qua selector khi VM thuộc
đúng project của token (batch bash từ chối thẳng). stderr tiến trình con gộp vào stdout cha.
Preflight chạy 2 lần khi plan→migrate (mỗi lệnh một lần — hành vi batch cũ giữ nguyên).
Gap tự nhận: `migrate plan` chưa nhận selector volume (mất dry-run `--volumes available
--plan-only` của batch cũ) — bù ở P5.

### Lab thật — regression gate: cả 4 suite NGUYÊN BẢN sed đổi lệnh + thêm `--yes`

Sed bổ sung cho quyết định "luôn hỏi xác nhận": `s/\(migrate migrate [^|>]*\) 2>&1/\1 --yes 2>\&1/`
(sự cố lần đầu: `&` trong replacement không escape làm nát dòng — đã sửa `2>\&1`, verify bằng
grep lines-without-yes = rỗng trước khi chạy).

| # | Case | Lệnh | Kết quả |
|---|---|---|---|
| 8 | e2e cold trên build P4 | `bash /root/mig-lab/tests/e2e-migrate-py.sh` | **PASS=50 FAIL=0 WARN=0** |
| 9 | live trên build P4 | `bash /root/mig-lab/tests/live-migrate-py.sh` | **PASS=26 FAIL=0** |
| 10 | newip-voltype trên build P4 | `bash /root/mig-lab/tests/newip-voltype-py.sh` | **PASS=26 FAIL=0** |
| 11 | extras trên build P4 | `bash /root/mig-lab/tests/extras-py.sh` | **PASS=30 FAIL=0** |
| 12 | Feature V3–V6 trên CLI mới (`p4feat.sh`, 24 check) | `bash /root/mig-lab/tests/p4feat.sh` | **PASS=24 FAIL=0** (lượt 2; lượt 1: 10 FAIL đều oan, xem dưới) |

Các case trong `p4feat.sh` (fixture: proj-b + batch-vm BFV thuộc proj-b, data-c proj-b, sg-vm1/2
flavor v5-src trong group soft-anti-affinity web-anti, flavor v5-dst chỉ có ở đích):

| Case | Lệnh | Kiểm chứng |
|---|---|---|
| A1 | `migrate plan --host mig-src-cmp --verify none` | 3 PLAN-OK (đa project, có proj-b) + 2 PLAN-FAIL đúng lý do `flavor v5-src` thiếu ở đích, rc=1 |
| E1 | `migrate migrate demo-vm --verify none </dev/null` | die đòi `--yes` khi stdin không phải tty; không migrate gì |
| A2 | `migrate migrate batch-vm --verify none --yes` (KHÔNG set env) | VM đích vào đúng proj-b (auto-scope ⚠); rollback bằng tên, nguồn ACTIVE, đích sạch |
| A3 | `migrate plan --vm-file f` (có dòng rác) | rác → SKIP(not-found), demo-vm PLAN-OK |
| A4 | `migrate volume --project proj-b --available --yes` | OK 1, đích giữ nguyên tên data-c, nguồn còn nguyên; rollback bằng vol-ID |
| A5 | `migrate volume --vm demo-vm --verify none --yes` | copy cả vda+vdb (demo-vm-vda/vdb), không tạo server; rollback bằng `demo-vm` (đúng chữ đã gõ) |
| A6 | `migrate migrate --vm demo-vm --volume-type bogus-type --yes` | PLAN-FAIL chặn, không đụng đích, rc=1 |
| B1 | `migrate volume <vdb-id> --in-use` | copy crash-consistent, VM nguồn ACTIVE suốt; rollback sạch |
| C1 | `migrate migrate --vm sg-vm1,sg-vm2 --keep-server-group --flavor-fallback --verify none --yes` + FLAVOR_MAP_FILE | plan báo will-create không tạo; 2 VM một lệnh; group az-web-anti đúng policy + 2 member; flavor map ra v5-dst; rollback giữ group, nguồn ACTIVE |
| B4 | MIG_DEFAULT_OPTS trong conf | plan in `MIG_DEFAULT_OPTS applied` + keep-sg có hiệu lực; env rỗng tắt được |
| D1 | `migrate batch` / `migrate migrate-volume` | die kèm hướng dẫn lệnh mới, không chạy gì |

**Lượt 1: 14/24, 10 FAIL — chẩn đoán xong cả 10 đều OAN, không có bug code:**
(a) A1: harness quên sg-VM dùng flavor v5-src chưa có ở đích → PLAN-FAIL là ĐÚNG (bằng chứng
log con `FAIL flavor v5-src`); (b) A5b+C1d: harness rollback bằng ID theo thói quen batch bash,
nhưng CLI mới ghi state theo ĐÚNG CHỮ đã gõ (`--vm sg-vm1` → state `sg-vm1`) — điểm lệch đã
duyệt, tool tự in hint rollback đúng identifier; (c) A4: project admin nguồn có sẵn 4 volume
available vô danh (rác test cũ) nên quét available bắt cả 5 — tool đúng, chuyển fixture sang
proj-b; (d) A6/B1/B4a/cleanup: dây chuyền từ (b) — state demo-vm còn sống → SKIP(state-exists),
copy vdb còn ở đích → preflight dst-exists chặn (đều là guard đúng). Điểm cộng quan sát được:
rollback 2 VM mà NGUỒN đã bị xóa tay vẫn dọn sạch tài nguyên đích rồi mới chết ở bước re-attach
port nguồn — idempotent đúng thiết kế. Dọn lab bằng chính `migrate rollback` của tool.

## Phase 5 — `plan volume` + `-I` + thay thế + docs (code `5e67653`) — ĐẠT 2026-11-18 (Fable duyệt 15 điểm lệch + gate lab xanh)

Phần code:

- `migrate plan volume <selection>` bù gap dry-run volume theo đợt (P4 tự nhận). Nó chạy đúng đường
  `--volumes … --plan-only` của batch: volume available `READY` không có plan con, VM của `--attached`/`--vm`
  chạy `plan --volumes-only`.
- Hint: `migrate volume … --plan-only` và `migrate plan … --volumes` chỉ sang `plan volume`.
- Tiến trình con selector chạy `python -I -m migratetool.single`.

Phần thay thế: 4 file bash `git mv` vào `archive/`, giữ nguyên byte. Docs `*-PYTHON.md`, README, CHANGELOG,
`migrate.conf.example`.

### Test

Harness giả lập P1–P4 + 12 kịch bản P5, chép ra thư mục riêng trên mig-host (`/root/v7h-p5`, ngoài repo,
không gọi cloud). Bộ unit P5 `p5unit.py`: clouds và tiến trình con giả, chạy được cả Windows lẫn Linux.

| # | Case | Lệnh | Kết quả |
|---|---|---|---|
| 13 | Parity 12 kịch bản P5: app `plan volume` vs `migrate-batch.sh --volumes … --plan-only` | `python3 scenarios.py p5-` | **12/0** |
| 14 | Regression toàn bộ harness P1–P5 trên build P5 | `python3 scenarios.py` | **153/0** (14 ph 35 s) |
| 15 | Unit cũ P1–P4 | `python3 unit.py` | **65/0** |
| 16 | Unit P5 trên mig-host; `-I` kiểm với package **cài thật** trong `/opt/mig-venv` | `P5_VENV_PY=/opt/mig-venv/bin/python3 python3 p5unit.py /root/v7h-p5` | **48/0** |
| 17 | Unit P5 trên Windows: Python 3.13, venv riêng cài bằng `pip install --no-build-isolation --no-deps .` | `P5_VENV_PY=<venv>\Scripts\python.exe python -B p5unit.py <repo>` | **48/0** |
| 18 | Lab thật, chỉ đọc: volume rời của admin | `PYTHONPATH=/root/v7h-p5 /opt/mig-venv/bin/python3 -m migratetool plan volume --project admin --available` | rc 0. Log `admin: 2 in-use volume(s) not listed — … (migrate volume --attached)`, bảng Selected + Plan **6 hàng READY**, không tiến trình con, không hỏi |
| 19 | Lab thật, chỉ đọc: volume của VM | `PYTHONPATH=/root/v7h-p5 /opt/mig-venv/bin/python3 -m migratetool plan volume --vm demo-vm --verify none` | rc 0. Tiến trình con `python -I -m migratetool.single plan demo-vm --volumes-only --verify none` chạy bằng package đã cài, preflight volumes-only 6 OK, `PLAN-OK` |
| 20 | Công thức sed của gate (DEVELOPING-PYTHON §5.1) sinh lại đúng file gate P4 | 4 phép sed trên `{e2e-migrate,live-migrate,newip-voltype,extras}.sh` rồi `cmp` với `*-py.sh` | **4/4 giống từng byte**; không dòng `migrate migrate` nào thiếu `--yes` |
| 21 | Chuyển archive giữ nguyên byte | `md5sum archive/{migrate-vm.sh,migrate-batch.sh,blocksync.py,brick_attach.py}` + `git ls-files -s` | md5 `5706e763`/`2a3189f1`/`ae9cd19f`/`ce2512f8`, blob git trùng trước khi chuyển |

Ở case 18–19, tiến trình cha chạy code P5 từ cây nguồn. Tiến trình con dùng `-I` nên import package đã cài
(build P4), vốn giống P5 ở mọi đường của tiến trình con. Lab không cài build P5.

**12 kịch bản parity P5:**

- `--available` == `--volumes available --plan-only`: READY, SKIP(mig-clone)/SKIP(status), rc 0;
- có state cũ: SKIP(state-exists) + hint rollback theo ID;
- `--vm ID` == `--volumes attached --vm ID --plan-only`: preflight thêm trên stdout là lệch đã duyệt;
- `--project --attached`;
- `--project --all`;
- `--vm` 2 VM với `--volume-type bogus`: PLAN-FAIL, rc 1 cả hai;
- `MIG_DEFAULT_OPTS` có option chỉ dành cho VM;
- không có volume available: `nothing selected`, rc 0 cả hai;
- chỉ còn hàng SKIP: app rc 1, batch rc 0, output giống hệt (lệch đã duyệt từ P4);
- 3 kịch bản riêng app: hint `--plan-only`, `plan volume <1 volume>` dừng, hint `--volumes`.

So 6 chiều như mọi phase. Lời gọi `openstack` của app giống batch **từng lệnh, đúng thứ tự**, và world cuối
không đổi, tức chỉ đọc.

**48 check unit P5:**

- 16 check parser: scope, thông báo theo lệnh đã gõ, các hint, `CHILD` có `-I`.
- 17 check `Runner` với clouds giả:
  - READY, không tiến trình con, chỉ gọi `list`/`show`;
  - mọi hàng SKIP → rc 1; không hàng nào → rc 0;
  - `--vm`: con `plan --volumes-only` có tee, đúng cặp project;
  - PLAN-FAIL → rc 1;
  - `--all` đúng thứ tự;
  - `--yes` không làm gì thêm.
- 12 check CLI subprocess: help, usage rc 2, die, VM tên `volume` qua `--vm volume`.
- 3 check `-I` thật:
  - không `-I` thì package giả trong thư mục hiện tại thắng: chứng minh bug có thật;
  - có `-I` thì package đã cài chạy;
  - `SRC_PROJECT`/`DST_PROJECT`/`SRC_RC` vẫn tới `config.load()`.

### Điểm lệch / quyết định Opus tự đưa ra (chờ Fable duyệt)

1. **`plan volume` của `--attached`/`--vm`/`--all` vẫn chạy preflight con `plan --volumes-only` cho hàng VM**,
   đúng như batch `--plan-only`. Chỉ hàng volume available là không có plan con.
2. **`plan volume` in cả bảng `Plan:`**, không chỉ bảng `Selected:`, giống batch `--plan-only`.
3. **Mã thoát**: rc 1 khi có PLAN-FAIL hoặc mọi hàng SKIP. Không hàng nào (`nothing selected`) thì **rc 0**,
   giống mọi selector run hiện tại (`migrate plan --host <host rỗng>` cũng 0) và giống batch. Fable muốn rc 1
   cho trường hợp này thì sửa 1 dòng `return 0` trong `Runner.run`, áp cho mọi lệnh.
4. **`migrate plan volume <1 volume>` dừng** kèm hướng dẫn, không dựng plan cho 1 volume. Bash
   `migrate-volume` không có plan; preflight của nó chạy trước khi tạo gì.
5. `plan volume` nhận rồi bỏ qua `--yes`/`--continue-on-error`, như `migrate plan` hiện tại. Help của
   `plan volume` không liệt kê 2 option này, cũng không liệt kê `--in-use` (bị từ chối như selector volume).
6. **`migrate plan volume …` chiếm từ `volume` sau `plan`**: VM tên đúng là `volume` thì plan bằng
   `--vm volume`. Đã ghi vào GUIDE-PYTHON và có unit test.
7. **Hint `--volumes` cho `plan`** (thêm ngoài danh sách): `migrate plan … --volumes x` chỉ sang
   `migrate plan volume …`, còn `migrate`/`volume` giữ hint cũ.
8. **Thông báo chọn volume nêu lệnh đã gõ**: `migrate plan volume --project needs …`. Câu chữ của
   `migrate volume` không đổi.
9. **`-I` chỉ thêm ở `selector.CHILD`**, đúng phạm vi. `copyvol.brick` vẫn gọi
   `[sys.executable, "-m", "migratetool.brick"]` **không `-I`**: cùng loại rủi ro thư mục hiện tại che
   package, nhưng chỉ khi thư mục đó có `migratetool/`. Thêm `-I` ở đó thì harness hiện tại phải đổi cách
   stub brick (harness dựa vào `PYTHONPATH`). Đề xuất: làm ở phase sau, có duyệt.
10. Docstring `livesync.py` đổi "the repository's blocksync.py" thành "archive/blocksync.py" sau khi đã
    test. Chỉ comment; diff với bản đã test đúng 1 dòng đó.
11. **`lab.sh up` bước 6/6 còn `scp iscsi-rbd-migration/{migrate-vm.sh,brick_attach.py,blocksync.py}`** từ
    gốc repo. Sau khi chuyển vào `archive/`, bước này hỏng đường dẫn. **Không sửa** vì ngoài phạm vi. Sửa
    thì đổi sang `pip install` app hoặc trỏ `archive/`.
12. `pyproject.toml` vẫn `version = "7.0.0.dev1"`. Không đổi vì ngoài phạm vi; đặt `7.0.0` khi user chốt phát hành.
13. TEST-REPORT này thêm mục P5 (CLAUDE.md bắt ghi test bằng md). Bỏ 2 dòng trùng "Phase 5 — (chưa)".
14. Harness (`/root/v7h-p5` trên mig-host, bản gốc ở máy làm việc) và `p5unit.py` **không đưa vào repo**,
    như P1–P4. DEVELOPING-PYTHON §5.3 ghi rõ điều này.
15. Docs, chi tiết ở báo cáo P5 gửi Fable:
    - `migrate.conf.example` sửa thêm câu mô tả cách đọc file: app không `source`, parser chặt;
    - thêm mục "SRC/DST_PROJECT chỉ `volume <1 volume>` và `rollback` đọc";
    - thêm ví dụ file map project, vì mẫu cũ thiếu.

### Trạng thái lab sau P5

Cloud không đổi. Case 18–19 chỉ đọc. `/var/lib/mig-migrate/demo-vm/` đã có từ trước (5 file của một
lần plan, không có `volume-map`), case 19 ghi lại đúng 5 file đó. Còn để lại:

- 2 thư mục log `/var/log/mig-batch/20261118-190648` và `-190654`;
- harness `/root/v7h-p5`.

Bản cài trong `/opt/mig-venv` vẫn là build P4 tại thời điểm Opus bàn giao; gate dưới đây do Fable chạy
sau khi cài build P5 (commit `82d2643` + docs) vào venv.

### Gate lab trên build P5 — ĐẠT 2026-11-18 (Fable)

| # | Case | Lệnh | Kết quả |
|---|---|---|---|
| 22 | 4 suite NGUYÊN BẢN sed trên build P5 | `bash /root/mig-lab/tests/{e2e-migrate,live-migrate,newip-voltype,extras}-py.sh` | **50/0 + 26/0 + 26/0 + 30/0** |
| 23 | `plan volume` sống trên bản cài thật | `migrate plan volume --project admin --available` → rc 0, bảng READY; `migrate plan volume --vm demo-vm --verify none` → rc 0, PLAN-OK; `migrate volume --project admin --available --plan-only` → rc 1 + hint `plan volume` | **3/3** |
| 24 | p4feat trên build P5 — lượt 2 | `bash /root/mig-lab/tests/p4feat.sh` | 13/24 — **cả 11 FAIL đều oan do môi trường**, xem dưới |
| 25 | p4feat trên build P5 — lượt 3, môi trường sạch | `bash /root/mig-lab/tests/p4feat.sh` | **PASS=24 FAIL=0** |

Chẩn đoán lượt 2 (case 24): quota volume project admin cloud nguồn **kín 10/10** — 2 root sg-VM fixture
+ 2 volume demo + 6 volume vô danh available tích tụ (4 tồn từ trước V7 + 2 rò trong ngày; kiểu rò: suite
xóa server xong xóa volume ngay, Cinder thỉnh thoảng còn giữ attachment nên delete trượt im lặng). A5 tạo
clone thứ 11 → `VolumeLimitExceeded` → tool die đúng chuẩn kèm hint rollback; A4 vớ thêm 1 volume vô danh
rò trong proj-b nên `OK 2` thay vì `OK 1`; các case sau dính dây chuyền. Xử lý: xóa 10 volume rác vô danh
(cả 2 cloud), dọn state, quota về 2/10 → lượt 3 xanh tuyệt đối. Không có bug code nào trong cả 2 lượt.
