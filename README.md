# iscsi-rbd-migration

Migrate VMs between two independent OpenStack clouds: iSCSI (LVM / Dell PowerStore) source
to Ceph RBD destination. Cold or live (pre-sync + short cutover), keep or change IP,
choose volume type / flavor / security group, ephemeral VMs via Glance, full rollback.

| Doc | Content |
|---|---|
| [DEPLOY.md](DEPLOY.md) | Step-by-step production install on the convert host |
| [GUIDE.md](GUIDE.md) | Usage: options, example commands, workflow, lab setup |
| [DESIGN-V2.md](DESIGN-V2.md) | Design (live mode, blocksync hash-cache, IP modes) |
| [POWERSTORE-VS-LAB.md](POWERSTORE-VS-LAB.md) | PowerStore 5000T vs lab; single-path checklist |
| [TEST-REPORT-20261112.md](TEST-REPORT-20261112.md) | Test results v2/v2.1 (220 PASS / 0 FAIL) |

Tool = 3 files installed to `/usr/local/bin`: `migrate-vm.sh`, `brick_attach.py`, `blocksync.py`.
Everything in `0*-*.sh`, `kolla/`, `lab.sh`, `tests/` is lab scaffolding (see DEPLOY.md §0).

Quick start:

```bash
migrate-vm.sh plan <vm>                  # read-only preflight
migrate-vm.sh migrate <vm> --mode cold
migrate-vm.sh migrate <vm> --mode live --ip new --volume-type ssd
migrate-vm.sh rollback <vm>
```
