# Hướng dẫn vận hành Migrate

CLI và UI dùng chung engine, cấu hình và khóa host. Chạy từng VM tuần tự. Đọc [RUNBOOK.md](RUNBOOK.md) trước chiến dịch; dựng host theo [DEPLOY.md](DEPLOY.md). Tên và địa chỉ trong ví dụ là dữ liệu tài liệu.

## 1. Lệnh và xác nhận

| Lệnh | Tác dụng | Xác nhận / tác động |
|---|---|---|
| `migrate --version` | In `migrate <version>` | Không |
| `migrate --help`, `migrate <command> --help` | In usage | Không |
| `migrate list <selectors> [--json]` | Liệt kê VM và state | Không export, không preflight |
| `migrate list --volumes <selectors> [--json]` | Liệt kê volume | Không copy |
| `migrate inventory [--count] [--host H] [--json]` | Hosts, projects; AZ, flavor, type ở đích | Chỉ đọc cloud |
| `migrate status [--all] [--json]` | State, batch log, process trên host này | Không gọi API cloud |
| `migrate doctor [--write-check] [--json]` | Kiểm convert host | `--write-check` ghi RBD thử; có thể tạo thư mục local |
| `migrate plan <selectors> [options] [--json]` | Export và preflight từng VM | Ghi export và log local; không đổi cloud |
| `migrate migrate <selectors> [options]` | Plan, xác nhận, chuyển từng VM | Gõ `yes`, hoặc `--yes` |
| `migrate plan volume <selectors> [options]` | Plan volume theo phạm vi | Volume rời chỉ `READY`; kiểm đầy đủ lúc copy |
| `migrate volume <volume> [options]` | Copy một volume bằng tên/UUID | **Chạy ngay, không prompt** |
| `migrate volume <selectors> [options]` | Copy volume theo đợt | Gõ `yes`, hoặc `--yes` |
| `migrate rollback <ident> [--dry-run [--json]] [--yes]` | Xem hoặc thực hiện hoàn tác | Gõ tên/ident; `--yes` bỏ prompt |
| `migrate prune --older-than Nd [--dry-run] [--json]` | Dọn state đã retire và batch log cũ | Không prompt; xem dry-run trước |

`migrate migrate` tự plan lại trước khi hỏi. Plan riêng không giữ quota cho CLI tiếp theo. UI có token xác nhận riêng, xem [UI.md](UI.md).

Plan không khóa host. Không chạy plan trên state đang migrate hoặc cần rollback: export có thể ghi lại `server.json`, gồm trạng thái nguồn dùng khi hoàn tác (`vmcmd.cmd_plan`). Dùng `status`, log và `rollback --dry-run` để điều tra state đang sống.

Không có dry-run trực tiếp cho `migrate volume <volume>`. `migrate plan volume` cần selector. Không suy diễn `READY` thành đã kiểm quyền ghi, quota hoặc RBD của volume đó.

## 2. Chọn đối tượng

| Cú pháp | Phạm vi | Mặc định / nguồn |
|---|---|---|
| `<vm>` | Một tên hoặc UUID; tương đương `--vm <vm>` | CLI |
| `--vm a,b` | Danh sách tên/UUID | CLI; không chọn ngầm |
| `--host h1,h2` | VM trên compute host nguồn | Cột `Host` của `compute service list` |
| `--project p1,p2` | VM/volume trong project nguồn | Tên hoặc UUID |
| `--vm-file FILE` | Mỗi dòng một tên/UUID | Bỏ dòng trống, comment `#` |
| `--available` | Volume rời ở `available` | Cần project khi chạy đợt |
| `--attached` | Volume của VM được chọn | Mặc định với `--vm`; chạy VM theo `--volumes-only` |
| `--all` trong lựa chọn volume | Attached trước, available sau | `list --volumes --project` mặc định all; execute phải chọn phạm vi |

Nhiều selector lấy **giao**. Tên VM mơ hồ phải dùng UUID. VM đủ điều kiện là `ACTIVE` hoặc `SHUTOFF`. Listing thử `--all-projects`, rồi fallback project của token nếu không được phép (`selector.Runner.enumerate_vms`).

Mọi VM chạy trong project của chính nó. Project đích lấy tên nguồn hoặc `PROJECT_MAP_FILE`. App credential chỉ dùng được trong project của nó. Chuyển đa project cần password openrc có role ở các project liên quan.

State dùng chữ đã chọn nếu tên phù hợp; chọn theo host/project hoặc tên có ký tự regex thì dùng UUID. UI dùng UUID từ listing. Rollback phải dùng **ident trong log/state**.

| `RESULT` | Cách đọc |
|---|---|
| `selected` | Được chọn, chưa plan |
| `SKIP(not-found)` | Không tìm thấy |
| `SKIP(ambiguous-name: use the ID)` | Tên mơ hồ; dùng UUID |
| `SKIP(state-exists)` | State chặn lần chạy mới; kiểm trạng thái và rollback |
| `SKIP(status)` | Trạng thái không đủ điều kiện |
| `SKIP(ephemeral-root)` | Yêu cầu volume-only trên VM ephemeral |
| `SKIP(mig-clone)` | Clone tạm bị loại khỏi lựa chọn |
| `SKIP(not-run)` | Đợt dừng trước hàng này |
| `PLAN-OK`, `PLAN-FAIL` | Preflight VM đạt / không đạt |
| `READY` | Volume rời sẽ được kiểm trước copy |
| `OK`, `OK(no key_name: <key>)` | Engine hoàn tất; biến thể báo thiếu keypair ở đích |
| `FAIL`, `FAIL(plan)` | Chạy lỗi / hàng plan lỗi |

CLI cho phép chạy lại VM `preparing` để tự dọn A6. UI chặn chọn mọi hàng có state sống, kể cả `preparing`. Xem [DESIGN.md](DESIGN.md#4-state-khóa-và-chạy-lại).

## 3. Bảng option

Giá trị thực tế và nguồn nằm trong `effective` của plan và `options.json`. Cờ boolean trong `MIG_DEFAULT_OPTS` không có cờ đối nghịch để tắt; đặt `MIG_DEFAULT_OPTS=` cho lần chạy cần bỏ chúng.

| Flag | Ý nghĩa | Mặc định | Nguồn |
|---|---|---|---|
| `--mode cold\|live` | Cold copy trong downtime; live pre-sync rồi delta cuối | `cold` | CLI, `MIG_DEFAULT_OPTS` |
| `--ip same\|new\|<addr>` | Giữ IP/MAC; cấp mới; IP chỉ định cho một NIC | `same` | CLI, `MIG_DEFAULT_OPTS` |
| `--volume-type TYPE` | Type mọi volume đích | Cloud default nếu không map | CLI, `AZ_TYPE_MAP`, `MIG_DEFAULT_OPTS` |
| `--az AZ` | Compute AZ; fallback volume AZ | Scheduler | CLI, `MIG_AZ`, `MIG_DEFAULT_OPTS`; suy từ host |
| `--volume-az AZ` | Cinder AZ | Compute AZ hiệu lực | CLI, `MIG_VOLUME_AZ`, `MIG_DEFAULT_OPTS` |
| `--dst-host HOST` | Nova host đích; cần admin và API 2.74 | Scheduler | Chỉ CLI; cấm trong `MIG_DEFAULT_OPTS` |
| `--verify readback\|full\|none` | Cold: full compare nếu khác none; live: readback hoặc thêm full compare cuối. `readback` đọc lại qua cùng handle librbd (có `rbd_cache`): chứng minh ghi đúng offset, không chứng minh dữ liệu đã tới OSD; `full` (qemu-img compare, tiến trình khác) mới là kiểm độc lập | `readback` | CLI, `MIG_DEFAULT_OPTS` |
| `--max-passes N` | Pre-sync tối đa, N ≥ 1 | `5` | CLI, `MIG_DEFAULT_OPTS` |
| `--delta-limit MB` | Ngưỡng delta từ lượt 2, MB ≥ 0 | `64` | CLI, `MIG_DEFAULT_OPTS` |
| `--flavor NAME` | Flavor đích có sẵn | Cùng tên nguồn | CLI, `MIG_DEFAULT_OPTS` |
| `--flavor-fallback` | Thiếu cùng tên thì tra `FLAVOR_MAP_FILE` | Tắt | CLI, `MIG_DEFAULT_OPTS` |
| `--security-group a,b` | SG đích cho mọi port, tên nguyên văn | Copy `<AZ>-<name>` | CLI, `MIG_DEFAULT_OPTS` |
| `--no-copy-secgroup` | Dùng SG cùng tên đã có ở đích | Tắt | CLI, `MIG_DEFAULT_OPTS` |
| `--server-group NAME_OR_ID` | Group đích có sẵn | Theo group nguồn | CLI, `MIG_DEFAULT_OPTS` |
| `--no-server-group` | Không theo group nguồn | Tắt; `--server-group` vẫn thắng | CLI, `MIG_DEFAULT_OPTS` |
| `--volumes-only` | Cold: stop/lock nguồn, copy volume, không port/server | Tắt | CLI, `MIG_DEFAULT_OPTS`; selector attached |
| `--keep-source-ports` | Với `same`, không tháo port nguồn | Tắt | CLI, `MIG_DEFAULT_OPTS`; ngoại lệ cần duyệt |
| `--start-source` | Unlock/start nguồn sau khi xong | Tắt | CLI, `MIG_DEFAULT_OPTS`; ngoại lệ cần duyệt |
| `--ephemeral-as-image` | Root ephemeral boot bằng image đích | Tắt; mặc định BFV | CLI, `MIG_DEFAULT_OPTS`; không có checkbox UI |
| `--in-use` | Copy nóng một volume đang gắn | Tắt | Chỉ CLI trực tiếp; bỏ qua trong `MIG_DEFAULT_OPTS` |
| `--yes` | Bỏ prompt execute batch / rollback | Tắt | CLI; runner thêm sau xác nhận UI |
| `--continue-on-error` | Tiếp tục sau FAIL | Tắt | CLI; UI có lựa chọn dừng đợt |
| `--plan-jobs N` | Plan VM song song, 1–8 | CLI `1`; runner `4` | CLI plan, runner `--plan-jobs` |
| `--json` | Một object stdout | Tắt | plan/list/inventory/status/doctor/prune, rollback dry-run |
| `--progress-json` | Thêm dòng `@@ {"event":...}` | Tắt | migrate/volume; runner thêm |
| `--dry-run` | Chỉ xem xóa gì | Tắt | rollback/prune |
| `--older-than Nd` | Prune, 1–99999 ngày | Bắt buộc | CLI prune |
| `--count` | Đếm VM theo host nguồn, tối đa 64 host | Tắt | inventory; `--host` lọc khi đếm |
| `--all` trong status | Tới 2000 mục retired/batch | 50 mục mới nhất mỗi loại | CLI status |
| `--volumes` | Listing volume | Listing VM | CLI list |
| `--write-check` | Ghi/đọc/discard/xóa image RBD thử | Tắt | doctor, hoặc nút UI |
| `-h`, `--help`, `--version` | Usage / phiên bản | Không áp dụng | CLI |

Volume rời chỉ dùng nhóm lưu trữ `--volume-type`, `--az`, `--volume-az`, `--verify` và `--in-use` theo điều kiện. Không có live pre-sync cho volume rời. Volume attached theo selector chạy VM cold `--volumes-only`, có downtime. Giới hạn xem [DESIGN.md](DESIGN.md#3-các-luồng-dữ-liệu).

Với `--plan-jobs` lớn hơn 1, mỗi plan child chạy trong session riêng, không có controlling terminal, nên credentials phải không tương tác: openrc không được `read` từ `/dev/tty` hay hỏi mật khẩu kiểu `getpass`, nếu không plan song song sẽ lỗi; cần nhập tay thì dùng `--plan-jobs 1`.

## 4. Cấu hình và map

File `/opt/mig/migrate.conf`, hoặc env `MIG_CONF`, chỉ là dữ liệu `KEY=VALUE`, có thể có `export`, nháy, comment. Không chạy `source`, `if` hoặc mở rộng `$VAR`. Openrc là shell tin cậy, xem [DEPLOY.md](DEPLOY.md#3-openrc-hai-cloud).

| Khóa | Ý nghĩa | Khi không đặt |
|---|---|---|
| `MIG_CONF` | Biến môi trường chọn file conf, không lấy từ bên trong file | `/opt/mig/migrate.conf` |
| `SRC_RC` | Openrc nguồn | `/opt/mig/src/admin-openrc.sh` |
| `DST_RC` | Openrc đích | `/opt/mig/dst/admin-openrc.sh` |
| `RBD_POOL` | Pool fallback backend Cinder đích | `mig-volumes` |
| `RBD_POOL_MAP` | `type:pool,type:pool` | Rỗng |
| `RBD_USER` | Ceph user, bỏ `client.` | `mig-xfer` |
| `CEPH_CONF` | Ceph cluster đích | `/etc/ceph/ceph.conf` |
| `MIG_ISCSI_IP` | Connector IP trên mạng iSCSI | Không có mặc định; Doctor FAIL khi chưa đặt hoặc không phải địa chỉ của host |
| `MIG_MULTIPATH` | `0` single-path, `1` multipath | Brick dùng `0` |
| `COPY_STREAMS` | Luồng ghi qemu-img | `8` |
| `SYS_PY` | Python có rados/rbd | `/usr/bin/python3`; loader hỗ trợ dù mẫu không liệt kê |
| `SRC_PROJECT` | Scope nguồn volume đơn/rollback | Rỗng; selector đặt theo đối tượng |
| `DST_PROJECT` | Scope đích volume đơn/rollback | Rỗng; selector đặt theo map |
| `PROJECT_MAP_FILE` | File map project | Rỗng: giữ tên |
| `FLAVOR_MAP_FILE` | File map cho `--flavor-fallback` | Rỗng |
| `MIG_AZ` | Compute AZ | Rỗng |
| `MIG_VOLUME_AZ` | Cinder AZ | Rỗng: theo compute AZ |
| `AZ_TYPE_MAP` | `AZ:type,AZ:type` | Rỗng |
| `MIG_DEFAULT_OPTS` | Token option tách whitespace | Rỗng |
| `MIG_LOCK` | Khóa chung CLI/UI | `/run/mig/migrate.lock` |
| `MIG_CMD_TIMEOUT` | Deadline control-plane, giây; `0` tắt | `600` |
| `MIG_COLD_WARN_GB` | WARN khi cold lớn hơn ngưỡng GB | `500` |

Bảng bao gồm mọi khóa của [migrate.conf.example](migrate.conf.example), cộng `SYS_PY` và biến chọn file `MIG_CONF`. Quy tắc thực thi được đối chiếu với `config.load` và `Options`.

Thứ tự chung, nguồn trước thắng nguồn sau: option gõ trong lệnh, rồi biến môi trường, rồi conf, rồi mặc định của tool. Biến môi trường đặt rỗng vẫn thắng conf, sau đó khóa có mặc định thì dùng mặc định. **Ngoại lệ `SYS_PY`: conf thắng biến môi trường** (`config.load`).

Compute AZ, volume type và pool có thứ tự riêng: xem bảng ở [mục 4.2](#42-az_type_map-và-rbd_pool_map--az-quyết-định-volume-type-và-pool). Volume AZ lấy theo thứ tự: `--volume-az` gõ trong lệnh, rồi `MIG_VOLUME_AZ`, rồi `--volume-az` trong `MIG_DEFAULT_OPTS`; không có thì bằng compute AZ.

Ví dụ, thay tên theo inventory đã nghiệm thu:

```ini
MIG_ISCSI_IP=192.0.2.20
MIG_MULTIPATH=0
MIG_AZ=AZ1
MIG_VOLUME_AZ=nova
AZ_TYPE_MAP="AZ1:rbd-az1,AZ2:rbd-az2"
RBD_POOL_MAP="rbd-az1:volumes-az1,rbd-az2:volumes-az2"
RBD_POOL=volumes-az1
PROJECT_MAP_FILE=/opt/mig/project-map.conf
FLAVOR_MAP_FILE=/opt/mig/flavor-map.conf
MIG_DEFAULT_OPTS="--verify full"
```

Không tự tạo project, flavor, type, pool, network hoặc subnet. Đóng băng mọi map trong đợt: đổi map giữa plan và chạy thì UI trả 409 (conf digest), CLI dùng map của lúc chạy.

### 4.1. PROJECT_MAP_FILE — đổi tên project ở đích

**Khi nào cần:** project đích không cùng tên project nguồn. Không có map thì VM của project `finance` vào project `finance` ở đích.

**Định dạng:** mỗi dòng `<tên project nguồn> <tên project đích>`, cách nhau bằng khoảng trắng, `#` là comment. Dùng **tên**, không dùng UUID: khóa được so với tên project nguồn, giá trị được đặt vào `OS_PROJECT_NAME` của openrc đích (cùng domain với openrc). Project không có dòng nào giữ nguyên tên.

**Cách làm:**

1. Liệt kê project nguồn và đích:

   ```bash
   migrate inventory --json | jq -r '.source.projects[].name, "--", .destination.projects[].name'
   ```

2. Tạo file root 0600:

   ```bash
   install -m 0600 -o root -g root /dev/null /opt/mig/project-map.conf
   ```

   ```text
   # /opt/mig/project-map.conf
   # source-project   destination-project
   finance            finance-prod
   web-legacy         web
   ```

   Project không có dòng nào trong file thì giữ nguyên tên. Ví dụ VM của project `hr` ở nguồn sẽ vào project `hr` ở đích, nên không cần viết dòng `hr hr`.

3. Trỏ conf tới file: `PROJECT_MAP_FILE=/opt/mig/project-map.conf` trong `/opt/mig/migrate.conf`.
4. Kiểm bằng plan, không đổi cloud:

   ```bash
   migrate plan --project finance --json | jq -r '.rows[] | "\(.name) \(.project.src) -> \(.project.dst) \(.result)"'
   ```

   Kết quả mong đợi `app01 finance -> finance-prod PLAN-OK`. Log batch có dòng `logs in /var/log/mig-batch/<…>; project map: /opt/mig/project-map.conf (2 entries, other projects keep their name)`. Bảng `Selected:` in cột `PROJECT src -> dst`.

**Lưu ý:**

| Tình huống | Hành vi |
|---|---|
| File không đọc được | Mọi lệnh có selector dừng: `PROJECT_MAP_FILE … is not a readable file: fix it, or unset PROJECT_MAP_FILE to keep every project name` |
| Dòng không đủ hai cột, hoặc kiểu `a = b` | Dừng: `…: expected 'source-project destination-project', got: <dòng>` |
| Project đích chưa tồn tại hoặc account không có role | Hàng đó PLAN-FAIL vì không lấy được token đích; tool không tạo project |
| Openrc đích là application credential | Chỉ dùng được cho đúng project của credential; map sang project khác bị từ chối, dùng openrc password |
| `migrate volume <volume>` một volume lẻ | **Không đọc map**: dùng `SRC_PROJECT`/`DST_PROJECT` của env hoặc conf. `migrate volume --project … --available` thì có map |
| Rollback | Dùng scope của state hoặc của hint trong log; đổi map sau đó không ảnh hưởng |

### 4.2. AZ_TYPE_MAP và RBD_POOL_MAP — AZ quyết định volume type và pool

**Khi nào cần:** đích có nhiều AZ, mỗi AZ có backend Ceph riêng. Ví dụ AZ1 dùng type `rbd-az1` (pool `volumes-az1`), AZ2 dùng `rbd-az2` (pool `volumes-az2`).

**Tool chọn ba giá trị theo thứ tự cho mỗi VM.** Mỗi giá trị lấy nguồn **đầu tiên** có giá trị trong cột giữa; không nguồn nào có thì dùng cột cuối. Bước sau dùng kết quả của bước trước.

| Bước | Giá trị | Thứ tự ưu tiên | Không có nguồn nào |
|---|---|---|---|
| 1 | compute AZ | `--az` gõ trong lệnh → `MIG_AZ` trong conf → `--az` trong `MIG_DEFAULT_OPTS` | Nova tự chọn AZ, và bước 2 **không** tra `AZ_TYPE_MAP` |
| 2 | volume type | `--volume-type` gõ trong lệnh → `AZ_TYPE_MAP` tra theo compute AZ của bước 1 → `--volume-type` trong `MIG_DEFAULT_OPTS` | type mặc định của Cinder đích |
| 3 | pool RBD | `RBD_POOL_MAP` tra theo volume type của bước 2 | `RBD_POOL` |

Ví dụ với conf `MIG_AZ=AZ1`, `AZ_TYPE_MAP="AZ1:rbd-az1,AZ2:rbd-az2"`, `RBD_POOL_MAP="rbd-az1:volumes-az1,rbd-az2:volumes-az2"`:

1. `migrate migrate app01 --az AZ2`. Bước 1: có `--az` nên AZ = AZ2. Bước 2: không gõ `--volume-type`, tra map được `rbd-az2`. Bước 3: tra `RBD_POOL_MAP` được `volumes-az2`.
2. `migrate migrate app01`. Bước 1: không gõ `--az`, lấy `MIG_AZ` = AZ1. Bước 2: `rbd-az1`. Bước 3: `volumes-az1`.

**Định dạng:** một chuỗi trong conf, các cặp cách nhau bằng dấu phẩy, không khoảng trắng: `AZ_TYPE_MAP="AZ:type,AZ:type"`, `RBD_POOL_MAP="type:pool,type:pool"`.

**Cách làm:**

1. Lấy AZ và volume type thật ở đích:

   ```bash
   migrate inventory --json | jq '.destination.zones, .destination.volume_types'
   ```

2. Hỏi đội Cinder type nào ghi vào pool nào (backend của type, `rbd_pool` trong `cinder.conf`), rồi kiểm caps của `mig-xfer` trên từng pool:

   ```bash
   rbd -c /etc/ceph/ceph.conf --id mig-xfer -p volumes-az2 ls >/dev/null && echo ok
   ```

3. Ghi vào conf:

   ```ini
   AZ_TYPE_MAP="AZ1:rbd-az1,AZ2:rbd-az2"
   RBD_POOL_MAP="rbd-az1:volumes-az1,rbd-az2:volumes-az2"
   RBD_POOL=volumes-az1
   ```

4. Kiểm mọi pool bằng `migrate doctor --write-check` (mỗi pool của `RBD_POOL_MAP` một dòng `RBD write check pool …`), rồi plan một VM vào từng AZ:

   ```bash
   migrate plan app01 --az AZ2
   ```

   Header plan in `type=rbd-az2 pool=volumes-az2` và dòng `  INFO  volume type rbd-az2 from AZ_TYPE_MAP (AZ2)`; khi AZ lấy từ conf thì có thêm `  INFO  availability zone AZ1 from MIG_AZ`. Trong `--json`, `effective.volume_type` là `{"value": "rbd-az2", "from": "conf", "detail": "AZ_TYPE_MAP (AZ2)"}`, `effective.pool` là `{"value": "volumes-az2", "from": "conf", "detail": "RBD_POOL_MAP"}`.

**Ví dụ kết quả:**

| Lệnh | compute AZ | type | pool |
|---|---|---|---|
| `migrate migrate app01 --az AZ2` | AZ2 | rbd-az2 (map) | volumes-az2 |
| `migrate migrate app01 --az AZ2 --volume-type ssd` | AZ2 | ssd (gõ tay thắng map) | `RBD_POOL_MAP[ssd]`, không có thì `RBD_POOL` |
| `migrate migrate app01` với `MIG_AZ=AZ1` | AZ1 | rbd-az1 | volumes-az1 |
| `migrate migrate app01` không `--az`, không `MIG_AZ` | Nova chọn | không map: type trong `MIG_DEFAULT_OPTS` hoặc default | `RBD_POOL_MAP[type]` hoặc `RBD_POOL` |

**Lưu ý:**

- **Pool sai thì dừng ở bước copy.** Tool kiểm image RBD của volume đích ngay sau khi Cinder tạo: `volume-<id> not visible in pool <pool>: wrong RBD_POOL_MAP for type '<type>', or mig-xfer lacks caps on that pool`. Trước cutover nên nguồn chưa bị dừng; chạy lại sẽ tự dọn volume vừa tạo.
- **`--dst-host` mà không `--az`:** nếu zone của host có trong `AZ_TYPE_MAP`, preflight FAIL và yêu cầu `--az <zone>` hoặc `--volume-type`, để không chọn nhầm pool.
- **`MIG_VOLUME_AZ`/`--volume-az`** chỉ đổi AZ của Cinder volume, không đổi type. Map tra theo **compute AZ**.
- **Volume rời** (`migrate volume`) dùng cùng chuỗi suy ra với `--az` của lệnh đó.

### 4.3. FLAVOR_MAP_FILE — flavor đích khi không có flavor cùng tên

**Khi nào cần:** đích đặt tên flavor khác nguồn. Mặc định tool dùng flavor **cùng tên** ở đích; thiếu thì PLAN-FAIL `flavor <tên>`. Map chỉ được dùng khi bật `--flavor-fallback`.

**Định dạng:** mỗi dòng `<tên flavor nguồn> <tên flavor đích>`, `#` là comment. Khóa là tên flavor nguồn trong `server show` (`original_name`), nên flavor nguồn đã bị xóa vẫn tra được.

**Cách làm:**

1. Liệt kê flavor đang dùng ở nguồn và flavor có ở đích:

   ```bash
   migrate list --project finance --json | jq -r '[.rows[].flavor] | unique[]'
   migrate inventory --json | jq -r '.destination.flavors[] | "\(.name) \(.vcpus) \(.ram) \(.disk)"'
   ```

2. Tạo file:

   ```text
   # /opt/mig/flavor-map.conf
   # source-flavor   destination-flavor
   m1.large          c2.large
   m1.xlarge         c2.xlarge
   ```

3. Conf: `FLAVOR_MAP_FILE=/opt/mig/flavor-map.conf`. Bật theo lệnh bằng `--flavor-fallback`, hoặc cho cả đợt bằng `MIG_DEFAULT_OPTS="--flavor-fallback"`.
4. Plan: `migrate plan app01 --flavor-fallback`. Mong đợi `  INFO  flavor m1.large -> c2.large from FLAVOR_MAP_FILE` rồi `  OK    flavor c2.large`; `effective.flavor.from` là `conf` (FLAVOR_MAP_FILE).

**Thứ tự chọn flavor:**

```text
--flavor X  >  flavor cùng tên ở đích  >  FLAVOR_MAP_FILE (chỉ khi --flavor-fallback)  >  PLAN-FAIL
```

| Tình huống | Dòng preflight |
|---|---|
| Đích có flavor cùng tên | `  OK    flavor m1.large`: map không được dùng |
| Thiếu cùng tên, không có dòng map | `  FAIL  flavor m1.large — not at the destination and not in /opt/mig/flavor-map.conf: add a line 'm1.large <dest-flavor>' to …` |
| Có dòng map nhưng flavor đích không tồn tại | `  FAIL  flavor c2.large (mapped from m1.large in …) not found at the destination — the tool never creates flavors` |
| Flavor đích nhỏ hơn (vCPU hoặc RAM) | `  WARN  mapped flavor c2.large is smaller than m1.large (vcpus 4 -> 2, ram 8192 -> 4096 MB)`: duyệt với chủ ứng dụng trước khi chạy |
| `--flavor-fallback` mà không đặt `FLAVOR_MAP_FILE` | Dừng: `--flavor-fallback needs FLAVOR_MAP_FILE (lines 'source-flavor destination-flavor'), set it in …` |
| Có `--flavor X` | `  INFO  --flavor-fallback has no effect: --flavor X is given` |

Disk của flavor không được so: VM boot-from-volume giữ cỡ volume nguồn; root ephemeral lấy max(disk flavor, virtual size của image).

## 5. Ví dụ an toàn

Ví dụ bỏ option mặc định ẩn bằng `MIG_DEFAULT_OPTS=`. Map/AZ trong conf vẫn áp dụng. Giữ `--ip same`, tháo port nguồn và bật verify. Cần network cùng tên và subnet cùng CIDR ở đích.

### Canary cold và rollback

```bash
migrate doctor --write-check
MIG_DEFAULT_OPTS= migrate plan canary-vm --mode cold --ip same --verify full
MIG_DEFAULT_OPTS= migrate migrate canary-vm --mode cold --ip same --verify full
migrate status --json > /root/canary-status.json
migrate rollback canary-vm --dry-run --json > /root/canary-rollback.json
migrate rollback canary-vm
```

Gõ `yes` khi migrate hỏi. Rollback hỏi `Type canary-vm to roll back: `. Chỉ xác nhận sau khi kiểm phạm vi xóa, chấp thuận mất ghi ở đích và cô lập ứng dụng đích. Dùng hint có `SRC_PROJECT`/`DST_PROJECT` từ log khi cần scope khác openrc.

### Wave live

```bash
MIG_DEFAULT_OPTS= migrate plan --host cmp-01 --project finance --mode live --verify readback --plan-jobs 4 --json > /root/wave-plan.json
MIG_DEFAULT_OPTS= migrate migrate --host cmp-01 --project finance --mode live --verify readback --max-passes 5 --delta-limit 64
```

Đọc mọi WARN/FAIL trước `yes`. Đợt đầu giữ dừng ở lỗi đầu tiên. `--verify full` tăng downtime vì compare cuối. Không bật nguồn khi đích `ACTIVE`.

### Volume

```bash
migrate list --volumes --project finance --available --json
MIG_DEFAULT_OPTS= migrate plan volume --project finance --available --volume-type rbd-az1 --verify full
MIG_DEFAULT_OPTS= migrate volume --project finance --available --volume-type rbd-az1 --verify full
MIG_DEFAULT_OPTS= migrate plan volume --vm canary-vm --verify full
MIG_DEFAULT_OPTS= migrate volume --vm canary-vm --verify full
```

Hai lệnh cuối dừng VM nguồn, không tạo VM đích. Kiểm `volume-map`, gắn copy vào guest thử cô lập để đọc checksum. Snapshot lịch sử ở lại nguồn.

## 6. Rollback, chạy lại, prune

`rollback --dry-run --json` liệt kê chủ yếu ID đã ghi; không phải inventory mồ côi toàn diện. Rollback thật không dùng `--json`. Prompt nhận tên nguồn hoặc ident đầy đủ. Không TTY và thiếu `--yes` → exit 2. `--yes` bỏ tóm tắt tương tác và prompt; automation cần lưu dry-run trước.

Rollback hỏi nguồn trước khi xóa gì ở đích (A20): nguồn trả not found (`source_status` = `GONE` trong dry-run: VM/volume đã bị xóa, credentials/`SRC_PROJECT` nguồn không thấy nó, hoặc sai endpoint/region) thì rollback từ chối, exit 1, không xóa gì ở đích vì đích có thể là bản sao duy nhất còn lại; kiểm scope và endpoint nguồn trước; muốn bỏ state đó thì xóa thư mục state bằng tay (`prune` không đụng state sống). Không đọc được nguồn (`source_status` = `null`) thì cũng từ chối: sửa credentials/`SRC_PROJECT` rồi chạy lại. Rollback chạy lại sau khi lỗi giữa chừng vẫn qua khi nguồn vẫn đọc được.

Rollback cũng phải đọc được đích trước lệnh xóa đầu tiên (A21, 1.1.1): `server show` server đích đã ghi (không có thì volume đích đầu tiên) trả đúng object hoặc not found nêu đúng ID thì đi tiếp; lỗi không nêu object (ví dụ `public endpoint for compute service in RegionOne region not found` khi sai `OS_REGION_NAME`/`OS_INTERFACE` hoặc catalog đích đổi, `(HTTP 404)` trần) thì dừng `cannot read destination ...`, không xóa gì, không đụng nguồn (`dst_server_status` = `null`, `dst_error` trong dry-run); sửa credentials/endpoint/`DST_PROJECT` rồi chạy lại. Scope: rollback tự lấy `SRC_PROJECT`/`DST_PROJECT` từ `options.json` của state khi môi trường không đặt (log `INFO scope ... (from the state's options.json)`); đặt khác record thì dừng `scope mismatch`, không làm gì; bỏ hai biến đó hoặc đặt đúng giá trị của state rồi chạy lại. Rollback dùng cùng loại openrc với lần migrate: state ghi bằng password openrc (record có project) rollback bằng application credential bị từ chối `needs a password openrc`, vì application credential không rescope được.

Rollback xóa server/port/volume/image đích của lần chạy, dọn clone, trả port và trạng thái điện nguồn lúc export. SG/server group copy được giữ. Nguồn vốn `SHUTOFF` vẫn `SHUTOFF`; nguồn vốn chạy được start lại. **Không reverse sync**. RPO: mất toàn bộ ghi chỉ có ở đích sau cutover. `t_stop` là thời gian marker, không phải thời gian ứng dụng chạy ở đích.

VM chưa cutover có thể chạy lại: xóa volume/clone đã ghi rồi bắt đầu mới. Có `t-stop` hoặc `dst-server-id` thì xử lý rollback trước. Standalone volume không có A6; rollback volume để dọn.

```bash
migrate prune --older-than 30d --dry-run --json > /root/prune-review.json
migrate prune --older-than 30d
```

Prune chỉ xóa thư mục retired và batch log đúng mẫu, cũ hơn ngưỡng. Không xóa state sống, plan-only, cloud object, attach record, UI job hoặc audit. Không prune khi job dài còn dùng log cũ. Duyệt chính sách lưu bằng chứng trước.

## 7. JSON và exit code

`--json`: stdout một object, thông báo người đọc ở stderr. Lỗi trước emit có thể để stdout rỗng. Kiểm cả exit và JSON parse. `--progress-json` vẫn có log thường; chỉ `@@ ` là event. Schema: [DESIGN.md](DESIGN.md#7-hợp-đồng-machine-readable).

```bash
migrate doctor --json > /root/doctor.json 2> /root/doctor.log
jq '{passed, counts, checks: [.checks[] | select(.level == "FAIL" or .level == "WARN")]}' /root/doctor.json
migrate status --json | jq '.live[] | {ident, phase, source_power, dst_server_id}'
```

| Exit | Nghĩa |
|---|---|
| `0` | Thành công; selection rỗng cũng có thể 0, đọc `totals` |
| `1` | Lỗi chung, plan/preflight FAIL, toàn SKIP (kể cả VM có state đã cutover: dòng `SKIP(state-exists)` kèm lệnh rollback), volume đơn/rollback lỗi |
| `2` | Parser usage hoặc rollback thiếu TTY/`--yes`; không phải mọi lỗi option đều 2 |
| `3` | VM lỗi trong chuẩn bị sau khi bắt đầu ghi state; chưa marker cutover |
| `4` | VM lỗi sau `t-stop` |
| `5` | Selector có nhiều kết quả thực thi và có FAIL |
| `130` | SIGINT/Ctrl-C được bắt; vẫn phải kiểm state |

`124` trong log là deadline subprocess. Lệnh ngoài có thể trả 1/3/4/5 tùy vị trí. Preflight FAIL vẫn 1. Đợt dừng ở con lỗi đầu có thể giữ mã của con đó thay vì 5 (`selector.Runner.run`, `Runner.exit_code`, `vmcmd.cmd_migrate`). Không suy trạng thái cloud chỉ từ exit.

## 8. Ngoại lệ có điều kiện

Đây là yêu cầu quy trình, không phải hệ thống phê duyệt tích hợp. UI xác nhận không thay thế quyết định của chủ ứng dụng.

| Option | Rủi ro | Chấp thuận cần có |
|---|---|---|
| `--keep-source-ports` | Nguồn bật lại trùng IP/MAC | Chủ mạng và ứng dụng duyệt cô lập nguồn, ngăn bật tự động |
| `--verify none` | Bỏ kiểm dữ liệu thông thường | Chủ dữ liệu chấp nhận rủi ro; checksum guest/backup và phương án phục hồi |
| `--start-source` | Hai VM cùng hostname, SSH key, cron, cluster membership, có thể trùng VIP | IP mới/khác nguồn hoặc volume-only; chủ ứng dụng duyệt cô lập và ngăn tác vụ trùng |
| `--ephemeral-as-image` | Image còn lại, root đích có lifecycle ephemeral | Chủ hạ tầng duyệt image policy, dung lượng và canary boot |
| `--in-use` | Crash-consistent một thời điểm; không nhất quán giao dịch/multi-volume | Một volume, không multiattach; chủ dữ liệu duyệt RPO và phục hồi ứng dụng |

`--verify none` vẫn kiểm hash Glance và zero sau discard blocksync. Hai kiểm đó không thay verify đầy đủ. `--start-source` bị chặn nếu IP chỉ định trùng IP/AAP nguồn; WARN VIP khác vẫn cần xử lý.

## 9. UI

Đăng nhập → Doctor → Chọn Instance/Volume → Tùy chọn → Plan → đọc tác động và WARN → gõ xác nhận → Chạy → nghiệm thu → Lịch sử. [UI.md](UI.md) mô tả controls, HTTP và phiên. Job UI ghi `operator`, command, effective và log. CLI ngoài UI có state trong status nhưng không có đầy đủ job record của runner.
