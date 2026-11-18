"""Attach a Cinder volume to this host through the Attachment API and os-brick.

Same code path Nova uses, so it works with any iSCSI Cinder backend (LVM, SAN), and the backend
manages ACLs and CHAP itself. Run as root with the source cloud's OS_* variables set.
Ported unchanged from brick_attach.py; migrate runs it as a child process with the source openrc's
environment, exactly as the bash build ran brick_attach.py.

  python -m migratetool.brick attach <volume-id>   -> prints the local device path on stdout
  python -m migratetool.brick detach <volume-id>   -> logs out and deletes this host's attachment

MIG_MULTIPATH=1 attaches through every portal the backend returns and insists on a dm-multipath
device (PowerStore-style SANs); the default (0) keeps the single-path behaviour. Either way the
device size must equal the Cinder volume size, so a stale or foreign LUN is never handed out.
"""
import json
import os
import socket
import subprocess
import sys

import os_brick
from cinderclient import client as cinder_client
from keystoneauth1 import loading, session
from oslo_concurrency import lockutils
from oslo_config import cfg
from os_brick.initiator import connector as brick
from os_brick.privileged import default as priv_default

API_VERSION = "3.54"  # attachment mode parameter
STATE_DIR = "/var/lib/mig-attach"
ROOT_HELPER = "sudo"
MULTIPATH = os.environ.get("MIG_MULTIPATH", "0") == "1"


class WrongDevice(Exception):
    """The connected device is not one we can safely read as this volume."""


def cinder():
    # Auth plugin is chosen via OS_AUTH_TYPE, like openstackclient.
    auth_type = os.environ.get("OS_AUTH_TYPE", "password")
    if auth_type == "v3applicationcredential":
        # An application credential is already bound to one user and project.
        loader = loading.get_plugin_loader("v3applicationcredential")
        auth = loader.load_from_options(
            auth_url=os.environ["OS_AUTH_URL"],
            application_credential_id=os.environ["OS_APPLICATION_CREDENTIAL_ID"],
            application_credential_secret=os.environ["OS_APPLICATION_CREDENTIAL_SECRET"],
        )
    elif auth_type == "password":
        loader = loading.get_plugin_loader("password")
        auth = loader.load_from_options(
            auth_url=os.environ["OS_AUTH_URL"],
            username=os.environ["OS_USERNAME"],
            password=os.environ["OS_PASSWORD"],
            project_name=os.environ["OS_PROJECT_NAME"],
            user_domain_name=os.environ.get("OS_USER_DOMAIN_NAME", "Default"),
            project_domain_name=os.environ.get("OS_PROJECT_DOMAIN_NAME", "Default"),
        )
    else:
        raise SystemExit(
            "unsupported OS_AUTH_TYPE %r (expected password or v3applicationcredential)" % auth_type
        )
    return cinder_client.Client(API_VERSION, session=session.Session(auth=auth))


def my_ip():
    """Address facing the iSCSI portal; used only as connector metadata."""
    return os.environ.get("MIG_ISCSI_IP", socket.gethostbyname(socket.gethostname()))


def state_path(volume_id):
    return os.path.join(STATE_DIR, volume_id + ".json")


def get_connector(protocol, multipath):
    return brick.InitiatorConnector.factory(
        protocol, ROOT_HELPER, use_multipath=multipath, device_scan_attempts=5
    )


def device_size(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        return os.lseek(fd, 0, os.SEEK_END)
    finally:
        os.close(fd)


def verified_path(device, expected_bytes):
    """Real path of the connected device, after checking it is the volume we asked for."""
    path = os.path.realpath(device["path"])
    # os-brick silently falls back to a single /dev/sdX when no dm map shows up.
    if MULTIPATH and not (
        os.path.basename(path).startswith("dm-")
        or "/dev/mapper/" in device["path"]
        or "/dev/mapper/" in path
    ):
        raise WrongDevice("multipath requested but got single-path device %s" % path)
    # Arrays that share one IQN reuse LUN numbers: a device left over from an earlier run can sit
    # on the LUN this volume now uses. qemu-img compare reads the same device and cannot tell.
    size = device_size(path)
    if size != expected_bytes:
        raise WrongDevice(
            "device size %d does not match volume size %d: possible stale/foreign LUN at %s"
            % (size, expected_bytes, path)
        )
    return path


def attach(volume_id):
    props = brick.get_connector_properties(
        ROOT_HELPER, my_ip(), multipath=MULTIPATH, enforce_multipath=MULTIPATH
    )
    api = cinder()
    expected_bytes = api.volumes.get(volume_id).size * 1024**3
    # Read-only: the transfer host must never be able to change source data.
    att = api.attachments.create(volume_id, props, mode="ro")
    conn = att["connection_info"]
    protocol = conn["driver_volume_type"]
    if protocol != "iscsi":
        api.attachments.delete(att["id"])
        sys.exit("unsupported backend protocol: %s" % protocol)
    connector = get_connector(protocol, MULTIPATH)
    try:
        device = connector.connect_volume(conn)
        api.attachments.complete(att["id"])
    except Exception:
        # Leave the volume usable: without this it stays in "attaching" forever.
        api.attachments.delete(att["id"])
        raise
    try:
        path = verified_path(device, expected_bytes)
    except Exception as e:
        # Never hand out a device we cannot vouch for, and leave nothing connected behind.
        try:
            connector.disconnect_volume(conn, device)
        finally:
            api.attachments.delete(att["id"])
        if isinstance(e, WrongDevice):
            sys.exit(str(e))
        raise

    state = {
        "attachment": att["id"], "protocol": protocol, "conn": conn, "device": device,
        "multipath": MULTIPATH, "path": path, "size_bytes": expected_bytes,
        "scsi_wwn": device.get("scsi_wwn"), "multipath_id": device.get("multipath_id"),
    }
    fd = os.open(state_path(volume_id), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(state, f)
    # tgtadm ignores the attachment's "ro" mode, so enforce read-only in the local kernel too.
    subprocess.run(["blockdev", "--setro", path], check=True)
    print(path)


def detach(volume_id):
    with open(state_path(volume_id)) as f:
        st = json.load(f)
    # Disconnect the way the volume was connected, whatever MIG_MULTIPATH says now.
    # State files written before multipath support carry no flag: they were single-path.
    connector = get_connector(st["protocol"], st.get("multipath", False))
    connector.disconnect_volume(st["conn"], st["device"])
    cinder().attachments.delete(st["attachment"])
    os.unlink(state_path(volume_id))


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ("attach", "detach"):
        sys.exit(__doc__)
    # Already root: run os-brick privileged calls in-process instead of spawning privsep.
    priv_default.set_client_mode(False)
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    lockutils.set_defaults(STATE_DIR)
    os_brick.setup(cfg.CONF)
    {"attach": attach, "detach": detach}[sys.argv[1]](sys.argv[2])


if __name__ == "__main__":
    main()
