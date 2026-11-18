# iscsi-rbd-migration

Migrate VMs between two independent OpenStack clouds: iSCSI (LVM / Dell PowerStore) source
to Ceph RBD destination. Cold or live (pre-sync + short cutover), keep or change IP,
choose volume type / flavor / security group, ephemeral VMs via Glance, full rollback.
v3 adds a conf file, per-project runs, instance-based destination names, `--server-group`,
`--volumes-only`, `migrate-volume` and batch runs: see [GUIDE.md](GUIDE.md) §2b and [DESIGN-V3.md](DESIGN-V3.md).
v4 adds `migrate-volume --in-use` (hot, crash-consistent copy of an attached volume), `--keep-server-group`
(destination group `az-<source group>`), `MIG_AZ` / `AZ_TYPE_MAP` and `MIG_DEFAULT_OPTS` (default options
in the conf file; every conf key is described in [migrate.conf.example](migrate.conf.example)): see
[GUIDE.md](GUIDE.md) §2b.1, §2b.9–2b.11 and [DESIGN-V4.md](DESIGN-V4.md).
v5 adds `--flavor-fallback`: when the destination lacks the source's flavor name, the flavor is taken
from a map file (`FLAVOR_MAP_FILE`, "source-flavor destination-flavor" lines); flavors are never
created: see [GUIDE.md](GUIDE.md) §2b.12 and [DESIGN-V5.md](DESIGN-V5.md).

| Doc | Content |
|---|---|
| [DEPLOY.md](DEPLOY.md) | Step-by-step production install on the convert host |
| [GUIDE.md](GUIDE.md) | Usage: options, example commands, workflow, lab setup |
| [WORKFLOW.md](WORKFLOW.md) | Step by step: one VM, cold and live, default options (commands, state, failure, timings) |
| [DESIGN-V2.md](DESIGN-V2.md) | Design (live mode, blocksync hash-cache, IP modes) |
| [DESIGN-V3.md](DESIGN-V3.md) | Design v3 (batch by host/project/VM list, server group, volumes-only, naming) |
| [DESIGN-V4.md](DESIGN-V4.md) | Design v4 (in-use volume copy, keep server group, AZ/type map, default options) |
| [DESIGN-V5.md](DESIGN-V5.md) | Design v5 (flavor fallback through a map file, opt-in, never creates flavors) |
| [migrate.conf.example](migrate.conf.example) | Template of `/opt/mig/migrate.conf`: every key, grouped, one-line description each |
| [POWERSTORE-VS-LAB.md](POWERSTORE-VS-LAB.md) | PowerStore 5000T vs lab; single-path checklist |
| [TEST-REPORT-20261112.md](TEST-REPORT-20261112.md) | Test results v2/v2.1 (220 PASS / 0 FAIL) |
| [TEST-REPORT-20261117.md](TEST-REPORT-20261117.md) | Test results v3 (gates 110/0 + 140/0, features P1 22/22, P2 retest 7/7) |
| [TEST-REPORT-20261118.md](TEST-REPORT-20261118.md) | Test results v4 (gates 140/0 ×2, features 64/0 over 27 design cases) |
| [TEST-REPORT-20261118-v5.md](TEST-REPORT-20261118-v5.md) | Test results v5 (features 16/16 over 12 design cases; regression gate round 7) |
| [CHANGELOG.md](CHANGELOG.md) | Version history v1 → v4: features, fixes, commits, design + test report per version |
| [DEVELOPING.md](DEVELOPING.md) | For maintainers: architecture, state files, config layering, rules, workflow, tests, adding an option |

Tool = 4 files installed to `/usr/local/bin`: `migrate-vm.sh`, `migrate-batch.sh`, `brick_attach.py`,
`blocksync.py`. `migrate-batch.sh` (v3) is the batch driver: it runs `migrate-vm.sh` over the VMs
selected by host, project or VM list, and looks for `migrate-vm.sh` in its own directory (or `MIGRATE_VM`).
Everything in `0*-*.sh`, `kolla/`, `lab.sh`, `tests/` is lab scaffolding (see DEPLOY.md §0).

Install on the convert host (full prerequisites in [DEPLOY.md](DEPLOY.md)):

```bash
git clone https://github.com/nguyenhuukhoi/iscsi-rbd-migration.git
cd iscsi-rbd-migration
install -m 0755 migrate-vm.sh migrate-batch.sh brick_attach.py blocksync.py /usr/local/bin/
install -m 0600 -D migrate.conf.example /opt/mig/migrate.conf   # optional; edit, uncomment what you need
```

Quick start:

```bash
migrate-vm.sh plan <vm>                  # read-only preflight
migrate-vm.sh migrate <vm> --mode cold
migrate-vm.sh migrate <vm> --mode live --ip new --volume-type ssd
migrate-vm.sh rollback <vm>
```
