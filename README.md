# iscsi-rbd-migration

Migrate VMs between two independent OpenStack clouds: iSCSI (LVM / Dell PowerStore) source
to Ceph RBD destination. Cold or live (pre-sync + short cutover), keep or change IP,
choose volume type / flavor / security group, ephemeral VMs via Glance, full rollback.
v3 adds a conf file, per-project runs, instance-based destination names, `--server-group`,
`--volumes-only`, `migrate-volume` and batch runs: see [GUIDE.md](GUIDE.md) §2b and [DESIGN-V3.md](DESIGN-V3.md).

| Doc | Content |
|---|---|
| [DEPLOY.md](DEPLOY.md) | Step-by-step production install on the convert host |
| [GUIDE.md](GUIDE.md) | Usage: options, example commands, workflow, lab setup |
| [DESIGN-V2.md](DESIGN-V2.md) | Design (live mode, blocksync hash-cache, IP modes) |
| [DESIGN-V3.md](DESIGN-V3.md) | Design v3 (batch by host/project/VM list, server group, volumes-only, naming) |
| [POWERSTORE-VS-LAB.md](POWERSTORE-VS-LAB.md) | PowerStore 5000T vs lab; single-path checklist |
| [TEST-REPORT-20261112.md](TEST-REPORT-20261112.md) | Test results v2/v2.1 (220 PASS / 0 FAIL) |

Tool = 4 files installed to `/usr/local/bin`: `migrate-vm.sh`, `migrate-batch.sh`, `brick_attach.py`,
`blocksync.py`. `migrate-batch.sh` (v3) is the batch driver: it runs `migrate-vm.sh` over the VMs
selected by host, project or VM list, and looks for `migrate-vm.sh` in its own directory (or `MIGRATE_VM`).
Everything in `0*-*.sh`, `kolla/`, `lab.sh`, `tests/` is lab scaffolding (see DEPLOY.md §0).

Install on the convert host (full prerequisites in [DEPLOY.md](DEPLOY.md)):

```bash
git clone https://github.com/nguyenhuukhoi/iscsi-rbd-migration.git
cd iscsi-rbd-migration
install -m 0755 migrate-vm.sh migrate-batch.sh brick_attach.py blocksync.py /usr/local/bin/
```

Quick start:

```bash
migrate-vm.sh plan <vm>                  # read-only preflight
migrate-vm.sh migrate <vm> --mode cold
migrate-vm.sh migrate <vm> --mode live --ip new --volume-type ssd
migrate-vm.sh rollback <vm>
```
