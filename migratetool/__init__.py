"""migrate: move VMs between two independent OpenStack clouds, iSCSI Cinder -> Ceph RBD.

The Python successor of migrate-vm.sh / migrate-batch.sh. State files and the output lines other tools grep are those
of the bash build (contract.py); what differs on purpose is listed in DESIGN.md and checked by tests/unit/parity.py.
"""

__version__ = "1.0.1"
