"""Rollback of a VM state directory (bash cmd_rollback + drop_volume): delete the destination side,
give the source back. Re-runnable: every step skips what is already gone. Works on state written
by the bash build as well (same files)."""
import os
import re
import time

from . import proc
from .config import ATTACH_DIR, STATE_ROOT
from .copyvol import brick
from .util import Die, lines, log, words

GONE_RE = re.compile(r"no volume with a name or id|not found|could not be found", re.I)


def drop_volume(r, cloud, vid, label):
    """Deletes it once settled; gone is fine, in-use is refused."""
    err = os.path.join(STATE_ROOT, "volume-show.err")
    res = cloud.run("volume", "show", vid, "-f", "value", "-c", "status", stderr=err)
    if res.rc != 0:
        with open(err, errors="replace") as f:
            text = f.read()
        if not GONE_RE.search(text):
            raise Die("cannot read %s %s: %s" % (label, vid, (text.splitlines() or [""])[-1]))
        log("%s %s already gone" % (label, vid))
        return
    s = res.stdout.rstrip("\n")
    if s == "in-use":
        raise Die("%s %s is in-use: detach it first, then re-run the rollback" % (label, vid))
    if not s.startswith("error"):
        r.wait_volume(cloud, vid, "available", 300)
    cloud.out("volume", "delete", vid)
    log("deleted %s %s" % (label, vid))


def rollback_vm(r, st):
    if not st.exists("server.json"):
        raise Die("no state for %s in %s" % (st.arg, st.path))
    sid = st.json("server.json").get("id")
    vm = st.arg

    if st.nonempty("dst-server-id"):
        log("delete destination server")
        r.dst.run("server", "delete", st.read("dst-server-id").rstrip("\n"), "--wait")  # || true
    # Other migrated VMs may be members; an empty group is harmless and a later migrate reuses it.
    if st.nonempty("dst-server-group"):
        log("INFO destination server group kept (%s): rollback never deletes a server group"
            % st.read("dst-server-group").rstrip("\n"))
    # Destination ports go by the IDs create_destination recorded, whatever their name. The
    # mig-<arg>-<i> name sweep below also covers state written before dst-ports existed.
    if st.nonempty("dst-ports"):
        for pid in st.read("dst-ports").split("\n"):
            if not pid:
                continue
            if r.dst.ok("port", "show", pid):
                r.dst.out("port", "delete", pid)
                log("deleted destination port " + pid)
    for i in range(len(st.json("ports.json"))):
        pname = "mig-%s-%d" % (vm, i)
        if r.dst.ok("port", "show", pname):
            r.dst.out("port", "delete", pname)
    if st.exists("volume-map"):
        for _svid, dvid in st.volume_map():
            # Recorded before it became available, so it may be in error or already gone: drop_volume
            # handles both. Still in-use right after the server delete: wait for the detach, as before.
            if r.dst.try_value("volume", "show", dvid, "-f", "value", "-c", "status") == "in-use":
                r.wait_volume(r.dst, dvid, "available", 300)
            drop_volume(r, r.dst, dvid, "destination volume")
    # Ephemeral path leftovers: uploaded destination images (by name too, for a run that died
    # before recording the id), a source snapshot from a run that died mid-download, the local file.
    imgs = []
    if st.nonempty("dst-image-id"):
        imgs += words(st.read("dst-image-id"))
    # A failing listing inside 'for img in $(...)' did not stop the bash build either.
    imgs += words(r.dst.run("image", "list", "--name", "mig-snap-" + vm, "-f", "value", "-c", "ID").stdout)
    for img in imgs:
        if r.dst.run("image", "delete", img, stderr=proc.DEVNULL).rc == 0:
            log("deleted destination image " + img)
    simg = words(r.src.out("image", "list", "--name", "mig-snap-" + vm, "-f", "value", "-c", "ID"))
    if simg:
        r.src.out("image", "delete", *simg)
    st.remove("root.img")
    # A run that died mid-copy leaves its temporary clone behind. Only this VM's own clones match.
    for a in st.json("attachments.json"):
        vid = a.get("Volume ID")
        for cid in lines(r.src.out("volume", "list", "--name", "mig-clone-%s" % vid, "-f", "value", "-c", "ID")):
            if os.path.exists(os.path.join(ATTACH_DIR, cid + ".json")):
                brick(r, "detach", cid, proc.DEVNULL)  # || true
            r.wait_volume(r.src, cid, "available", 300)
            r.src.out("volume", "delete", cid)
            log("deleted leftover clone " + cid)
    r.src.run("server", "unlock", sid)  # || true
    # Reattach only the ports this run detached (--ip same). A run that failed before the detach
    # step has no list; its ports are still attached and the device_id check skips them.
    if st.exists("source-ports"):
        pids = words(st.read("source-ports"))
    else:
        pids = [p["ID"] for p in st.json("port-ids.json")]
    for pid in pids:
        if r.src.try_value_inherit("port", "show", pid, "-f", "value", "-c", "device_id"):
            continue
        r.src.out("server", "add", "port", sid, pid)
        for _ in range(60):
            if r.src.try_value_inherit("port", "show", pid, "-f", "value", "-c", "device_id") == sid:
                break
            time.sleep(2)
        if r.src.try_value_inherit("port", "show", pid, "-f", "value", "-c", "device_id") != sid:
            raise Die("port %s did not reattach" % pid)
        log("reattached source port " + pid)
    if r.src.try_value_inherit("server", "show", sid, "-f", "value", "-c", "status") == "SHUTOFF":
        r.src.out("server", "start", sid)
    r.wait_server(r.src, sid, "ACTIVE")
    if r.src.try_value_inherit("server", "show", sid, "-f", "value", "-c", "status") != "ACTIVE":
        raise Die("source VM did not reach ACTIVE")
    st.retire()
    log("source VM ACTIVE again")
