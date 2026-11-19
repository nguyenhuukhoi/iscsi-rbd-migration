"""Rollback of a VM state directory (bash cmd_rollback + drop_volume): delete the destination side,
give the source back. Re-runnable: every step skips what is already gone. Works on state written
by the bash build as well (same files)."""
import os
import re
import time

from . import config, proc
from .copyvol import brick
from .util import Die, lines, log, words

GONE_RE = re.compile(r"no volume with a name or id|not found|could not be found", re.I)


def drop_volume(r, cloud, vid, label):
    """Deletes it once settled; gone is fine, in-use is refused."""
    err = os.path.join(config.STATE_ROOT, "volume-show.err")
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


def _last_line(text):
    return (text.strip().splitlines() or [""])[-1]


def reassociate_floating_ips(r, st):
    """7.2 (A3): a floating IP of a source port that lost it (Nova deleted the port it had created itself,
    and Neutron dropped the association with it) goes onto the port now holding that slot: source-ports
    line i (the reserve port, or the port itself) for ports.json entry i, else port-ids.json entry i.
    One that is still associated is left alone; a failure is a WARN, never the end of the rollback."""
    ports = st.json("ports.json")
    if not any(p.get("floating_ips") for p in ports):
        return
    held = words(st.read("source-ports")) if st.exists("source-ports") else []
    ids = [p["ID"] for p in st.json("port-ids.json")]
    for i, p in enumerate(ports):
        for addr in p.get("floating_ips") or []:
            pid = held[i] if i < len(held) else (ids[i] if i < len(ids) else "")
            res = r.src.run("floating", "ip", "show", addr, "-f", "value", "-c", "port_id", stderr=proc.CAPTURE)
            if res.rc != 0:
                log("WARN floating IP %s not re-associated: %s" % (addr, _last_line(res.stderr) or "floating ip show failed"))
                continue
            if res.stdout.strip() not in ("", "None"):
                continue
            res = r.src.run("floating", "ip", "set", "--port", pid, addr, stderr=proc.CAPTURE)
            if res.rc != 0:
                log("WARN floating IP %s not re-associated: %s" % (addr, _last_line(res.stderr) or "floating ip set failed"))
                continue
            log("re-associated floating IP %s with port %s" % (addr, pid))


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
    # P6: the '<AZ>-<name>' security group copies are shared the same way.
    if st.nonempty("dst-security-groups"):
        kept = [l.split()[1] for l in st.read("dst-security-groups").splitlines() if len(l.split()) >= 2]
        log("INFO destination security groups kept (%s): rollback never deletes a security group" % ", ".join(kept))
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
            if os.path.exists(os.path.join(config.ATTACH_DIR, cid + ".json")):
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
    reassociate_floating_ips(r, st)
    if r.src.try_value_inherit("server", "show", sid, "-f", "value", "-c", "status") == "SHUTOFF":
        r.src.out("server", "start", sid)
    r.wait_server(r.src, sid, "ACTIVE")
    if r.src.try_value_inherit("server", "show", sid, "-f", "value", "-c", "status") != "ACTIVE":
        raise Die("source VM did not reach ACTIVE")
    st.retire()
    log("source VM ACTIVE again")
