# Phát triển tiếp app `migrate` (bản Python, v7)

Tài liệu cho người sửa hoặc mở rộng app Python. Đọc kèm:

- [GUIDE-PYTHON.md](GUIDE-PYTHON.md): cách dùng.
- [DESIGN-V7.md](DESIGN-V7.md): vì sao viết lại, hợp đồng parity, các phase.
- [CHANGELOG.md](CHANGELOG.md): đã thay đổi những gì.
- [DEVELOPING.md](DEVELOPING.md): bản cho code bash. Mục 2 (state), 3 (lớp cấu hình) và 4 (quy ước bất di
  bất dịch) vẫn đúng nguyên văn cho app, vì app giữ đúng các hợp đồng đó. Tài liệu này chỉ ghi phần khác.

## 1. Kiến trúc

### 1.1. Module trong `migratetool/`

| Module | Vai trò | Tương ứng bản bash |
|---|---|---|
| `cli.py` | argparse (help, usage), dispatch lệnh, `run_vm`/`run_volume`/`run_rollback`/`run_plan_volume`, `guarded` (die → `ERROR:` + mã 1, Ctrl-C → 130) | dispatcher cuối `migrate-vm.sh`, usage header |
| `selector.py` | `split_args` (tách selector/điều phối với option của VM), `finish` (kiểm selector), `check_child_options`, `Runner` (liệt kê, bảng `Selected/Plan/Summary`, tiến trình con, hỏi `Type yes`, mã thoát) | toàn bộ `migrate-batch.sh` |
| `single.py` | Entry của tiến trình con: `python -I -m migratetool.single plan\|migrate\|migrate-volume <vm-hoặc-volume> [option]`. Một VM hoặc một volume, không chọn, không hỏi. Không dành cho người gõ | `migrate-vm.sh` khi batch gọi nó |
| `options.py` | `Options`: `load_default_opts` (`MIG_DEFAULT_OPTS`), `parse_options` (ghi nguồn gốc option, xung đột), `apply_az_defaults`, `pool_for_type`. `FLAG_OPTS`/`VALUE_OPTS` là danh sách option duy nhất | `load_default_opts`, `parse_option_list`, `parse_options`, `apply_az_defaults`, `pool_for_type` |
| `config.py` | Đọc `$MIG_CONF` (chặt: chỉ `KEY=VALUE`), env, mặc định. Các gốc `STATE_ROOT`, `ATTACH_DIR`, `LOG_ROOT` | conf loader dòng 72–104 |
| `clouds.py` | `Cloud`: gọi `openstack` CLI với env của openrc (source 1 lần bằng bash, cache), scope `SRC_PROJECT`/`DST_PROJECT`, resolve SG → ID. `base_env`: bỏ proxy, đặt `bin/` của venv lên đầu `PATH` | `src`/`dst`, `scope_project`, `*_sg_id` |
| `context.py` | `Run`: ngữ cảnh một lần chạy (cfg, opts, 2 cloud, `rbd`, `wait_volume`/`wait_server`, `check_project_scope`) | biến toàn cục + `wait_*` |
| `proc.py` | Chỗ **duy nhất** chạy chương trình ngoài: list-args, không bao giờ shell | — |
| `util.py` | `out`/`log` (dòng `[HH:MM:SS]`), `Die`/`Exit`/`CommandFailed`, `prog()`, helper kiểu jq | `log`, `die` |
| `state.py` | `StateDir`: đọc/ghi từng file state đúng format bash, `retire` → `.rolled-back.<epoch>` | thao tác file trong `$STATE_ROOT/<vm>` |
| `vmdef.py` | `export_definition`, `detect_boot`, `dest_volume_name` | cùng tên |
| `preflight.py` | `Checker` (dòng `OK/FAIL/INFO/WARN`), `preflight`, `server_group_preflight` (7.0: `keep_sg_preflight`), `flavor_fallback_preflight`, `load_flavor_map`; P6: `need_prefix`, `port_fidelity_preflight`, `port_security_conflict`, `net_extensions`, `plain_hostname` | cùng tên (P6: không có) |
| `azname.py` | P6: `<AZ>` của tên bản copy: `--az` > `MIG_AZ` > AZ compute duy nhất của đích (`zones_from_listing`, `pick`, `resolve`) | — |
| `secgroups.py` | P6: copy SG thành `<AZ>-<tên>`: `rule_args` (builder rule), `rule_key`, `plan_copy` (chỉ đọc, đệ quy remote group, visited set), `describe` (dòng preflight), `ensure` (tạo trước cutover, lỗi thì xóa cái vừa tạo) | — |
| `vmcmd.py` | `cmd_plan`, `cmd_migrate` | cùng tên |
| `copyvol.py` | `create_dest_volume`, `attach_clone`/`drop_clone` (qua `brick`), `copy_volume_cold`, `Watcher` (tiến độ đọc `/sys/block`), `cleanup_attach` | cùng tên, `progress_watch`, trap EXIT |
| `livesync.py` | `presync`, `sync_volume_pass`, `final_pass`, `check_numbers`, 2 dòng preflight live | vòng live trong `cmd_migrate`, `sync_volume_pass` |
| `ephemeral.py` | Snapshot → Glance REST (urllib, đúng trình tự curl cũ) | `migrate_ephemeral_root` |
| `server.py` | `detach_source_ports`, `create_destination`, `server_group_ensure` (7.0: `keep_sg_ensure`); P6: `fidelity_args`, `pair_args` (port đích + port giữ chỗ) | cùng tên (P6: không có) |
| `rollback.py` | `rollback_vm`, `drop_volume` | `cmd_rollback`, `drop_volume` |
| `volcmd.py` | `cmd_migrate_volume`, `volume_preflight`, `cmd_rollback_volume`, `volume_options_only` | cùng tên |
| `brick.py` | Port nguyên văn `brick_attach.py`, chạy bằng `python -I -m migratetool.brick attach\|detach <id>` (python của venv; `-I` từ 7.1) | `brick_attach.py` |
| `blocksync.py` | **Giống từng byte** `archive/blocksync.py`, chạy bằng `SYS_PY -I <file>` (python hệ thống + `python3-rbd`), không bao giờ import | `blocksync.py` |

### 1.2. Mô hình tiến trình

```
migrate migrate --host cmp-01 --yes                       (tiến trình cha: cli → selector.Runner)
  ├─ openstack server list --all-projects …               liệt kê, không scope project
  ├─ [mỗi VM] python -I -m migratetool.single plan <ident> <option>      env SRC_PROJECT/DST_PROJECT của VM
  │     └─ openstack … (scope project) · systemctl · rbd                 → plan-<id>.log
  ├─ hỏi Type yes (trừ --yes)
  └─ [mỗi VM, tuần tự] python -I -m migratetool.single migrate <ident> <option>
        ├─ openstack …, qemu-img convert/compare, rbd                    → migrate-<id>.log + màn hình
        ├─ python -I -m migratetool.brick attach|detach <clone-id>       (python venv, os-brick)
        └─ $SYS_PY -I …/migratetool/blocksync.py …                       (python hệ thống, librbd)
```

- **Mỗi VM một tiến trình con**, không gọi hàm trong cùng tiến trình (DESIGN-V7 §3). Lý do: giữ cách ly
  env/project/state giữa các VM và mỗi con một log, như batch bash.
- `-I` ở tiến trình con: con import package **đã cài**, kể cả khi thư mục hiện tại có `migratetool/` của
  repo (`python -m` mặc định đặt thư mục hiện tại lên đầu `sys.path`). Env không bị mất: `SRC_RC`,
  `SRC_PROJECT`/`DST_PROJECT`, `MIG_CONF`… vẫn truyền qua `env` của `Popen`. `-I` chỉ làm interpreter bỏ
  qua các biến `PYTHON*` và user site-packages. Hệ quả cho người phát triển: chạy từ cây nguồn bằng
  `PYTHONPATH=.` thì tiến trình con **không** thấy cây nguồn. Hoặc `pip install -e .`, hoặc làm như
  harness: thay `selector.CHILD`.
- Lời gọi brick (`copyvol.BRICK`) cũng chạy `-I` từ 7.1 (P6), cùng lý do. Harness giả lập không dùng được
  `PYTHONPATH` cho nó nữa, nên thay `copyvol.BRICK` (như thay `selector.CHILD`).
- Tiến trình con chạy theo **`Row.ident`**, quy ước ở mục 3.

## 2. Hợp đồng parity (DESIGN-V7 §2)

Năm điều giữ nguyên 100% so với bản bash:

1. state;
2. cách gọi hạ tầng (subprocess `openstack`/`qemu-img`/`rbd`/blocksync, chưa SDK);
3. các chuỗi output mà người, script và test bám vào (mục 4);
4. file conf và thứ tự ưu tiên;
5. mọi quy tắc nghiệp vụ.

Quy tắc user làm rõ 18/11: **logic bất khả xâm phạm, câu chữ thì được phép đẹp hơn**, trừ các chuỗi bị
grep ở mục 4.

Mỗi thay đổi được so với bản bash tham chiếu (`archive/migrate-vm.sh` md5 `5706e763`,
`archive/migrate-batch.sh` `2a3189f1`) trên **6 chiều**:

| Chiều | So cái gì |
|---|---|
| stdout | Từng dòng, sau khi chuẩn hóa giờ (`[T]`), thời lượng, đường dẫn tạm, tên lệnh (`PROG`) |
| stderr | Từng dòng |
| Mã thoát | Mỗi bước của kịch bản |
| File state | Mọi file trong `STATE_ROOT` (bỏ `*.err`; `t-stop`, `clone-time` chuẩn hóa) cùng log của từng con |
| Lời gọi ra ngoài | **Toàn bộ chuỗi** lời gọi `openstack`/`rbd`/`qemu-img`/`systemctl`/brick/SDK/Glance REST, đúng thứ tự |
| World cuối | Trạng thái 2 cloud giả sau kịch bản |

Mọi lệch phải được **tự khai** trong báo cáo phase và được duyệt: ghi vào `expect_diff`/`bash_subs` của
harness kèm comment "Intended: …". Lệch không khai thì là bug.

## 3. State theo chữ đã gõ

| Chạy | Tiến trình con chạy với (`Row.ident`) | Thư mục state |
|---|---|---|
| `<vm>` hoặc `--vm <tên>`, tên trỏ đúng 1 VM | Đúng chữ đã gõ | `/var/lib/mig-migrate/<tên>/` |
| `--vm <id>` | ID | `…/<id>/` |
| `--vm <tên có ký tự regex>` (`.[]*()\|$^?+{}\`) | ID. Con chạy `server list --name <tên>` qua regex của Nova không escape, nên phải dùng ID | `…/<id>/` |
| `--host`, `--project` (không có `--vm`) | ID, như batch bash | `…/<id>/` |
| Volume rời | ID volume | `…/vol-<id>/` |

- Phát hiện state cũ (`SKIP(state-exists)`): kiểm cả `<id>/volume-map` lẫn `<tên>/volume-map`, nên state
  do batch bash (theo ID) hay do lần chạy lẻ (theo tên) đều bị bắt.
- `rollback <x>`: có `<x>/server.json` thì là rollback VM. Không có thì rollback volume, tìm lần lượt
  `<x>/volume.json`, `vol-<x>/volume.json`, rồi resolve tên volume → `vol-<id>`.
- Mọi hint rollback (`hints()`, dòng `Rollback:` của con) in đúng `ident`, nên người dùng không phải nhớ
  quy tắc này.

## 4. Chuỗi bị grep: giữ nguyên văn

Chép từ [DEVELOPING.md](DEVELOPING.md) mục 1.4 và 4, tên lệnh đã cập nhật, thêm các chuỗi mà bộ test lab
đọc. Đổi một chuỗi ở đây thì phải sửa bên đọc nó cùng lúc và khai trong báo cáo.

| Chuỗi | Ai đọc | Ở đâu trong app |
|---|---|---|
| Dòng preflight `"  OK    …"`, `"  FAIL  …"`, `"  INFO  …"`, `"  WARN  …"` (2 dấu cách, chữ, đệm tới cột 8) | `selector` grep `^ +FAIL\|^ERROR` để điền dòng lỗi dưới bảng `Plan:`. Test lab lọc `grep -vE '^  OK '` | `preflight.Checker`, `volcmd.volume_preflight` |
| `ERROR: <message>` trên stderr, mã 1 | `selector` (như trên), test (`grep -E 'FAIL\|ERROR'`) | `cli.guarded` |
| `ephemeral VM has no volume to migrate` | `selector` → `SKIP(ephemeral-root)` | `preflight.py` |
| `WARN keypair <key> not at destination` | `selector` → `OK(no key_name: <key>)` | `server.create_destination` |
| Dòng log `[HH:MM:SS] …` | Con người, test | `util.log` |
| `Nova removed its own port; reserved …`, `Migrated.` | `tests/e2e-nova-port.sh` (`grep -E 'Nova removed\|ERROR\|FAIL\|Migrated'`) | `server.py`, `vmcmd.py` |
| `reattached source port …`, `ACTIVE` | `tests/e2e-nova-port.sh` (`grep -E 'reattached\|ERROR\|ACTIVE'`) | `rollback.py` |
| Không dòng nào **bắt đầu ở cột 0** bằng `PASS`, `FAIL`, `WARN` | Test lab đếm `grep -c '^PASS'`/`'^FAIL'`/`'^WARN'` trên report có chứa output của tool | toàn app (bảng bắt đầu bằng `vm `/`vol `, log bắt đầu bằng `[`) |
| `state exists in <dir>: run rollback first` | Con người, `failure-injection.sh` (migrate lần 2 phải thất bại) | `vmcmd.cmd_migrate`, `volcmd` |
| Bảng `Selected:` / `Plan:` / `Summary:`, header `KIND ID NAME STATUS PROJECT src -> dst RESULT`, các giá trị RESULT, `Totals: OK n  FAIL n  SKIP n  (logs: …)`, hint `  <tên>: inspect, then to undo: SRC_PROJECT=… DST_PROJECT=… <prog> rollback <ident>` | Con người, script vận hành viết theo batch cũ | `selector.Runner` |
| blocksync: stdout đúng **1 dòng JSON**, các trường `changed`, `chunks`, `written_bytes`, `zero_chunks`, `seconds`, `first`; stderr `[blocksync] NN% …` | `livesync.sync_volume_pass`; `tests/live-migrate.sh` đọc `sync-<vid>.log` | `blocksync.py` (byte-identical) |
| Tên/format file state, `volume-map` `<src> <dst>`, `t-stop` epoch, tên đích `<vm>-vda` | Rollback chéo bản bash, test lab | `state.py`, `vmdef.dest_volume_name` |

**P6 (7.1) đổi có chủ đích** (bảng trên vẫn đúng; không bộ test lab nào grep các chuỗi này):

| 7.0 | 7.1 |
|---|---|
| `  OK    security group web (<id>)` (tìm SG cùng tên) | Mặc định: `  OK    security group web -> will create nova-web (6 rules)` / `-> found nova-web (<id>)`. Dòng cũ còn nguyên khi `--no-copy-secgroup` |
| `  FAIL  port <mac> has allowed-address-pairs (not handled)` | Bỏ. Thay bằng `  INFO  port <mac>: allowed-address-pairs copied: …` (`--ip new`: `  WARN  … not copied with --ip new …`) |
| Lời khuyên `drop --keep-server-group and pass --server-group <id>` (3 message server group) | `pass --server-group <id>, or --no-server-group` |
| `will create az-<tên>`, `server group az-<tên> exists` | `<AZ>-<tên>` (`p4feat.sh` C1 trên lab bám `az-web-anti`: phải sửa khi gate 7.1) |
| `--keep-server-group`: header token ` keep-server-group`, `  INFO  source VM is in no server group`, `  INFO  --keep-server-group ignored: --volumes-only …`, die `choose one: --server-group <name> or --keep-server-group` | Cờ bị bỏ (19/11): `ERROR: unknown option --keep-server-group` / `ERROR: MIG_DEFAULT_OPTS: unknown option '--keep-server-group' (…)`. VM không thuộc group nào thì không in dòng nào (`p4feat.sh` B4a trên lab grep `source VM is in no server group`: phải sửa) |

Hai chỗ trước đây nằm rải ở batch bash, giờ gom về một nơi:

- Bộ lọc `VOLPASS` của batch nay là `selector.VOLUME_PASS` (option có giá trị được truyền cho volume rời)
  cộng `options.FLAG_OPTS` (danh sách option không tham số). Thêm option cờ mới thì sửa `FLAG_OPTS`, nếu
  không selector nuốt mất token kế tiếp (bug v4 §2c).
- `volume_options_only` nay là `volcmd.SERVER_ONLY`: các option mà `migrate volume <1 volume>` phải từ chối.

## 5. Chạy test

### 5.1. Regression gate trên lab: 4 bộ cũ nguyên văn, chỉ sed đổi lệnh + `--yes`

Bộ test trong `tests/` gọi `migrate-vm.sh`. App được nghiệm thu bằng chính các bộ đó, chỉ đổi **cách gõ
lệnh**, sinh bằng sed. **3 phép đổi lệnh + 1 phép thêm `--yes`:**

```bash
cd /root/mig-lab/tests                     # bản copy test trên host chuyển của lab
for t in e2e-migrate live-migrate newip-voltype extras; do
  sed -e 's/migrate-vm\.sh migrate /migrate migrate /' \
      -e 's/migrate-vm\.sh rollback /migrate rollback /' \
      -e 's/migrate-vm\.sh plan /migrate plan /' \
      -e 's/\(migrate migrate [^|>]*\) 2>&1/\1 --yes 2>\&1/' \
      "$t.sh" > "$t-py.sh"
done
grep -n 'migrate migrate' *-py.sh | grep -v -- '--yes'   # PHẢI rỗng trước khi chạy
diff e2e-migrate.sh e2e-migrate-py.sh                     # chỉ đúng các dòng gọi lệnh (e2e: 2 dòng)
bash e2e-migrate-py.sh                                    # mỗi bộ tự source /opt/mig-venv/bin/activate
```

- **Bẫy `&` của sed:** trong phần thay thế, `&` nghĩa là "cả chuỗi vừa khớp". Viết `2>&1` trần thì mỗi dòng bị
  chèn lại nguyên đoạn khớp và nát dòng lệnh. Phải viết **`2>\&1`**, như lần chạy đầu của P4 đã dính.
  Luôn chạy dòng `grep … | grep -v -- '--yes'` để chắc không còn dòng `migrate migrate` nào thiếu `--yes`.
- Phép thứ 4 cần thiết vì app **luôn hỏi `Type yes`**. Test chạy không có terminal nên thiếu `--yes` là
  app dừng.
- 4 lệnh sed này sinh lại **đúng từng byte** các file `*-py.sh` đã dùng làm gate P4 trên lab (đã `cmp`
  ngày 18/11).
- Số dòng đổi: e2e 2, live 2, newip 2, extras 4.

Kỳ vọng: e2e **50/0**, live **26/0**, newip-voltype **26/0**, extras **30/0**.

Bộ thứ 5, `blocksync-unit.sh`, chạy **nguyên văn**, không sed. Nó gọi `blocksync.py` qua `PATH`, nên trỏ
vào bản trong package. Chạy trong shell **không** có venv: shebang `env python3` của blocksync gặp python của
venv thì không có librbd, 8 FAIL oan như lần đầu P2.

```bash
PKG=$(/opt/mig-venv/bin/python -c 'import migratetool, os; print(os.path.dirname(migratetool.__file__))')
mkdir -p /tmp/bs && ln -sf "$PKG/blocksync.py" /tmp/bs/blocksync.py
PATH=/tmp/bs:$PATH bash blocksync-unit.sh      # 8/0
```

Tổng gate: **140/0**. Test xong phải trả lab về trạng thái ban đầu: `demo-vm` ACTIVE ở nguồn, đích sạch,
không còn state. Bẫy hay gặp: lệnh smoke `migrate plan …` cũng ghi state dir, làm bộ live báo "leftover
state".

### 5.2. Feature trên lab

`/root/mig-lab/tests/p4feat.sh` là bộ 24 check V3–V6 trên CLI mới. Các case liệt kê ở TEST-REPORT V7 mục
Phase 4. Bộ này chưa nằm trong `tests/` của repo.

### 5.3. Harness giả lập (không cần cloud)

Harness parity của V7 gồm `parity.py`, `scenarios.py`, `unit.py` và các stub. Nó chưa nằm trong repo, đang
giữ ở máy làm việc.

- Chạy bản bash tham chiếu và app trên cùng "world" giả: stub `openstack`, `rbd`, `qemu-img`, `systemctl`,
  brick, python3-SDK, cùng một Glance giả là HTTP server thật.
- So 6 chiều ở mục 2.
- Cần Linux có `bash`, `jq`, python3. Harness thay `selector.CHILD` và các gốc `config.STATE_ROOT`/`LOG_ROOT`,
  nên không đụng `/var/lib/mig-migrate`.
- Kết quả từng phase ghi ở TEST-REPORT V7.

Bộ unit nhỏ của P5 (`plan volume`, hint, `-I`) chạy được trên mọi máy có Python ≥ 3.10, không cần
bash/openstack: clouds và tiến trình con đều giả. Lệnh và kết quả ghi ở TEST-REPORT V7 mục Phase 5.

Từ P6 (7.1) app khác bản bash có chủ đích ở SG/server group, nên harness so bash vs app bằng một **luật máy móc**
cho mọi kịch bản viết trước P6: phía app chạy thêm `--no-copy-secgroup` và `--no-server-group`, tức đúng hành vi
7.0, rồi so 6 chiều như cũ. Kịch bản bash gõ `--keep-server-group` (cờ app đã bỏ): phía app bỏ cờ đó và **không**
thêm `--no-server-group`, vì mặc định 7.1 chính là việc cờ đó từng làm. Header preflight của bản bash được chỉnh
token cho khớp. Kịch bản P6 chạy app trên mặc định mới với check riêng. Bộ unit
`p6unit.py` cũng chạy được ở mọi máy. Chi tiết: TEST-REPORT V7 mục Phase 6.

## 6. Quy trình phase → gate

V7 chạy theo phase (DESIGN-V7 §4). Thay đổi sau V7 cũng đi đúng đường này:

1. **Bàn → user chốt → DESIGN** (thêm mục vào DESIGN của bản đang làm). Ghi rõ hành vi, giới hạn, state,
   rollback, những gì KHÔNG đổi, test plan đánh số.
2. **Code** trên nhánh `feature/<tên>`, commit nhỏ, message tiếng Anh. Mọi lời gọi ra ngoài đi qua
   `proc.run` (list-args, **cấm `shell=True`**). Mọi lời gọi `openstack` đi qua `Cloud` để scope đúng project.
3. **Tự khai mọi điểm lệch** so với bản bash và so với DESIGN, trong báo cáo cho người review.
4. **Review chéo** (Fable): đọc diff đối chiếu DESIGN, duyệt từng điểm lệch.
5. **Harness giả lập**: các kịch bản mới, cộng toàn bộ kịch bản cũ chạy lại, phải xanh.
6. **Gate lab**: 4 bộ sed + blocksync-unit (140/0), cộng feature theo DESIGN. FAIL oan phải chẩn đoán đến
   nguyên nhân và ghi lại.
7. **TEST-REPORT**: mỗi case có lệnh nguyên văn và kết quả, gồm FAIL oan, trạng thái lab sau test, những gì
   chưa test.
8. **Docs**: GUIDE-PYTHON (option, ví dụ), DEPLOY-PYTHON nếu đụng tới, mục mới trong CHANGELOG. Docs cũ của
   bản bash không sửa.
9. **User quyết merge/push.**

Trước mỗi commit:

```bash
python3 -m py_compile migratetool/*.py
grep -n 'shell=True' migratetool/*.py                              # phải rỗng
cmp migratetool/blocksync.py archive/blocksync.py                  # bundled blocksync không được lệch
grep -rnE '<pattern thông tin nội bộ: IP lab, proxy, mật khẩu, user thật>' <các file sắp commit>   # phải rỗng
```

Thêm một option mới: theo checklist [DEVELOPING.md](DEVELOPING.md) mục 7, với các chỗ tương ứng trong app:

| Việc | Chỗ trong app |
|---|---|
| Usage/help | `VM_OPTIONS` trong `cli.py` |
| Biến + parse | `Options.__init__`, `parse_option_list` trong `options.py`, thêm tên vào `FLAG_OPTS`/`VALUE_OPTS` |
| Từ chối ở lệnh không áp dụng | `volcmd.SERVER_ONLY` (volume), `selector.VOLUME_PASS` (volume theo đợt) |
| Preflight | `preflight.py` |
| Hành động, state, rollback | Module tương ứng |
