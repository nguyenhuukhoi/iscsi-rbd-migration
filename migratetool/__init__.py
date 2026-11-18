"""migrate: move VMs between two independent OpenStack clouds, iSCSI Cinder -> Ceph RBD.

Python port of migrate-vm.sh / migrate-batch.sh (DESIGN-V7). Behaviour, state files and the output
lines other tools grep are kept identical to the bash build; see DESIGN-V7.md section 2.
"""

__version__ = "7.1.0.dev1"
