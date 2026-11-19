# iscsi-rbd-migration

Migrate VMs between two independent OpenStack clouds: iSCSI (LVM / Dell PowerStore) source
to Ceph RBD destination. Cold or live (pre-sync + short cutover), keep or change IP,
choose volume type / flavor / security group, ephemeral VMs via Glance, full rollback.

**From v7 the tool is one Python app, `migrate`** (package `migratetool/`, installed with pip into
`/opt/mig-venv`). It replaces `migrate-vm.sh` and `migrate-batch.sh`, which now live in
[archive/](archive/README.md). The app keeps the bash build's behaviour, conf file and state
format (state is shared both ways, so either build can roll back the other's runs). Since 7.1 the
destination VM also gets copies of the source's security groups, server group and port details
(allowed-address pairs, port security, description, dns_name) by default. The final lab gate passes
183 checks across 6 suites: see [DESIGN-V7.md](DESIGN-V7.md),
[TEST-REPORT-20261118-v7.md](TEST-REPORT-20261118-v7.md) and [CHANGELOG.md](CHANGELOG.md).
7.2 fixes the findings of [AUDIT-20261119.md](AUDIT-20261119.md) ([DESIGN-V7-2.md](DESIGN-V7-2.md)): cold
mode creates the destination volumes before stopping the source, the preflight checks the quota of both
clouds and the Ceph pool capacity, floating IPs are reported, `--volume-az`, and `migrate doctor`.

| | Python app `migrate` (current, v7+) | Bash build (archived, v1–v6) |
|---|---|---|
| Usage | [GUIDE-PYTHON.md](GUIDE-PYTHON.md) | [GUIDE.md](GUIDE.md) |
| Install on the convert host | [DEPLOY-PYTHON.md](DEPLOY-PYTHON.md) | [DEPLOY.md](DEPLOY.md) |
| One VM step by step (cold, live) | [WORKFLOW-PYTHON.md](WORKFLOW-PYTHON.md) | [WORKFLOW.md](WORKFLOW.md) |
| For maintainers | [DEVELOPING-PYTHON.md](DEVELOPING-PYTHON.md) | [DEVELOPING.md](DEVELOPING.md) |
| Code | `migratetool/` | `archive/migrate-vm.sh`, `archive/migrate-batch.sh`, `archive/brick_attach.py`, `archive/blocksync.py` |

The bash docs stay as they were written: their install commands refer to the scripts at the
repository root, where they lived up to the last bash commit (see [archive/README.md](archive/README.md)).

Shared by both builds:

| Doc | Content |
|---|---|
| [migrate.conf.example](migrate.conf.example) | Template of `/opt/mig/migrate.conf`: every key, grouped, one-line description each |
| [CHANGELOG.md](CHANGELOG.md) | Version history v1 → v7: features, fixes, commits, design + test report per version |
| [DESIGN-V2.md](DESIGN-V2.md) … [DESIGN-V7.md](DESIGN-V7.md) | Designs: live mode / IP modes (v2), batch + naming (v3), in-use copy + server group + AZ map + default options (v4), flavor map (v5), batch hint (v6), Python app (v7) |
| [POWERSTORE-VS-LAB.md](POWERSTORE-VS-LAB.md) | PowerStore 5000T vs lab; single-path checklist |
| `TEST-REPORT-*.md` | Test results per version (v1 → v7) |

Everything in `0*-*.sh`, `kolla/`, `lab.sh`, `tests/*.sh` is lab scaffolding (see DEPLOY.md §0).
`tests/unit/` is the cloud-free unit + parity harness: `bash tests/unit/run.sh` (DEVELOPING-PYTHON.md §5.3).

Install on the convert host (prerequisites: system packages and the `/opt/mig-venv` client venv, see
[DEPLOY-PYTHON.md](DEPLOY-PYTHON.md)):

```bash
git clone https://github.com/nguyenhuukhoi/iscsi-rbd-migration.git
cd iscsi-rbd-migration
/opt/mig-venv/bin/pip install --no-build-isolation .
ln -sf /opt/mig-venv/bin/migrate /usr/local/bin/migrate
install -m 0600 -D migrate.conf.example /opt/mig/migrate.conf   # optional; edit, uncomment what you need
```

Quick start:

```bash
migrate doctor                                       # read-only check of this host, before anything else
migrate plan web-01                                  # read-only dry-run
migrate migrate web-01 --yes                         # always asks 'Type yes' unless --yes
migrate migrate web-01 --mode live --ip new --volume-type ssd --yes
migrate migrate --host cmp-01 --az AZ1 --yes          # every VM of a compute host, one by one
migrate plan volume --project proj-a --available     # dry-run of a volume batch
migrate volume --project proj-a --available --yes    # copy a project's available volumes
migrate rollback web-01                              # roll back with the name or ID you migrated with
```
