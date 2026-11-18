"""Volume copy, cold path: destination volume, clone, brick attach, qemu-img convert + compare.

Ports create_dest_volume, copy_volume_props, attach_clone, drop_clone, copy_volume_cold and the
progress watcher of migrate-vm.sh.
"""
import os
import re
import sys
import threading
import time

from . import config, proc
from .util import CommandFailed, Die, jq_alt, jqs, log
from .vmdef import dest_volume_name


# --- progress ---------------------------------------------------------------------------------
class Watcher:
    """Logs how far a long sequential read of a device has got, from the sectors-read counter in
    /sys/block/<dev>/stat (/dev/mapper names resolve to their dm-N). One line every 15 s."""

    def __init__(self, device, size_gb, label):
        self.device, self.size, self.label = device, size_gb, label
        self.stop_ev = threading.Event()
        self.thread = None

    def start(self):
        if not re.fullmatch(r"[1-9][0-9]*", str(self.size)):
            return self
        stat = "/sys/block/%s/stat" % os.path.basename(os.path.realpath(self.device))
        start = self._read(stat)
        if start is None:
            return self
        self.thread = threading.Thread(target=self._loop, args=(stat, start), daemon=True)
        self.thread.start()
        return self

    @staticmethod
    def _read(stat):
        try:
            with open(stat) as f:
                v = f.read().split()[2]
        except (OSError, IndexError):
            return None
        return int(v) if v.isdigit() else None

    def _loop(self, stat, start):
        size = int(self.size)
        total = size * 1073741824
        last, t = start, time.monotonic()
        while not self.stop_ev.wait(15):
            cur = self._read(stat)
            if cur is None:
                continue
            now = time.monotonic()
            done_b = (cur - start) * 512
            pct = min(done_b * 100 // total, 99)
            gib10 = done_b * 10 // 1073741824
            secs = int(now - t)
            rate = (cur - last) * 512 // 1048576 // (secs if secs > 0 else 1)
            log("%s %d%% (%d.%d/%d GiB, %d MB/s)" % (self.label, pct, gib10 // 10, gib10 % 10, size, rate))
            last, t = cur, now

    def stop(self):
        """Safe to call twice (bash stop_watch)."""
        self.stop_ev.set()
        if self.thread is not None:
            self.thread.join()
            self.thread = None


def stop_watch(r):
    if r.watcher is not None:
        r.watcher.stop()
        r.watcher = None


# --- brick ------------------------------------------------------------------------------------
# -I (isolated), as selector.CHILD: the installed package runs even when the current directory holds a
# migratetool/ source tree. The environment below still reaches it through env=; -I only stops the
# interpreter from reading PYTHON* variables and the user site-packages.
BRICK = [sys.executable, "-I", "-m", "migratetool.brick"]


def brick(r, action, volume_id, stderr):
    """python -I -m migratetool.brick attach|detach <id>, with the source openrc's environment."""
    env = r.src.env()
    env["MIG_ISCSI_IP"] = r.cfg.MIG_ISCSI_IP
    if r.cfg.multipath_set:
        env["MIG_MULTIPATH"] = r.cfg.MIG_MULTIPATH
    return proc.run([*BRICK, action, volume_id], env=env, capture=True, stderr=stderr)


def cleanup_attach(r):
    """bash cleanup_attach (the EXIT trap): stop the watcher, detach a clone still attached."""
    stop_watch(r)
    if r.attached:
        try:
            brick(r, "detach", r.attached, proc.INHERIT)
        except Exception:
            pass


# --- destination volume ----------------------------------------------------------------------
def create_dest_volume(r, vid, st):
    """Sets r.dvid and appends to volume-map."""
    vj = r.src.json("volume", "show", vid, "-f", "json")
    # Named after the instance, not the source volume: BFV volumes usually have no name, and the
    # device tells several volumes of one VM apart.
    name = dest_volume_name(vid, st)
    size = jqs(vj.get("size"))
    args = ["volume", "create", name, "--size", size]
    if r.opts.volume_type:
        args += ["--type", r.opts.volume_type]
    if r.opts.dest_az:
        args += ["--availability-zone", r.opts.dest_az]
    args += ["--description", "migrated from " + vid, "-f", "value", "-c", "id"]
    r.dvid = r.dst.value(*args)
    # Recorded before the wait and the pool check, so that rollback also removes a volume that dies there.
    st.append("volume-map", "%s %s" % (vid, r.dvid))
    r.wait_volume(r.dst, r.dvid, "available")
    if not r.rbd("-p", r.pool, "info", "volume-" + r.dvid):
        raise Die("volume-%s not visible in pool %s: wrong RBD_POOL_MAP for type '%s', or %s lacks caps on that pool"
                  % (r.dvid, r.pool, r.opts.volume_type or "default", r.cfg.RBD_USER))
    copy_volume_props(r, vj, r.dvid)


IMAGE_PROP_RE = re.compile(r"^(hw_|os_|hypervisor_type$|architecture$|vm_mode$)")


def copy_volume_props(r, vj, dvid):
    """Boot and guest metadata the destination Nova needs."""
    if jqs(vj.get("bootable")) in ("true", "True"):
        r.dst.out("volume", "set", dvid, "--bootable")
    meta = jq_alt(vj.get("volume_image_metadata"), {})
    for k, v in (meta.items() if isinstance(meta, dict) else []):
        if IMAGE_PROP_RE.search(k):
            r.dst.out("volume", "set", dvid, "--image-property", "%s=%s" % (k, jqs(v)))


# --- clone ------------------------------------------------------------------------------------
def attach_clone(r, vid):
    """Sets r.clone, r.clone_time (UTC, clone request) and r.device."""
    r.clone_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    r.clone = r.src.value("volume", "create", "mig-clone-" + vid, "--source", vid, "-f", "value", "-c", "id")
    r.wait_volume(r.src, r.clone, "available")
    # stderr goes to a file, not /dev/null: brick's own guard messages (multipath, size/LUN
    # mismatch) are the whole point of having it.
    err = os.path.join(config.STATE_ROOT, "brick-attach.err")
    res = brick(r, "attach", r.clone, err)
    r.device = res.stdout.rstrip("\n")
    if res.rc == 0:
        r.attached = r.clone
    if res.rc != 0 or not _is_block(r.device):
        try:
            with open(err, errors="replace") as f:
                tail = " ".join(f.read().splitlines()[-2:])
        except OSError:
            tail = ""
        raise Die("no block device for clone %s: %s" % (r.clone, tail))


def _is_block(path):
    import stat
    try:
        return stat.S_ISBLK(os.stat(path).st_mode)
    except OSError:
        return False


def drop_clone(r):
    res = brick(r, "detach", r.clone, proc.DEVNULL)
    if res.rc != 0:
        raise CommandFailed("brick detach %s failed (exit %d)" % (r.clone, res.rc))
    r.attached = ""
    r.wait_volume(r.src, r.clone, "available")
    r.src.out("volume", "delete", r.clone)


def _qemu(r, *args):
    res = proc.run(["qemu-img", *args], env=r.env, capture=False)
    return res.rc


def copy_volume_cold(r, vid, dvid, size, st):
    attach_clone(r, vid)
    log("clone attached at " + r.device)
    img = "rbd:%s/volume-%s:id=%s:conf=%s" % (r.pool, dvid, r.cfg.RBD_USER, r.cfg.CEPH_CONF)

    log("copy %s -> %s/volume-%s" % (r.device, r.pool, dvid))
    t0 = time.monotonic()
    # The new RBD image is all zeros, so zero runs are skipped and stay unallocated.
    r.watcher = Watcher(r.device, size, "copy").start()
    rc = _qemu(r, "convert", "-f", "raw", "-O", "raw", "-n", "--target-is-zero", "-m", r.cfg.COPY_STREAMS,
               "-W", r.device, img)
    stop_watch(r)
    if rc != 0:
        raise CommandFailed("qemu-img convert failed for %s (exit %d)" % (vid, rc))
    took = int(time.monotonic() - t0)
    log("copy done in %ds (%d MB/s logical)" % (took, int(size) * 1024 // (took if took > 0 else 1)))

    if r.opts.verify != "none":
        log("verify byte-for-byte")
        t0 = time.monotonic()
        r.watcher = Watcher(r.device, size, "verify").start()
        rc = _qemu(r, "compare", "-f", "raw", "-F", "raw", r.device, img)
        stop_watch(r)
        if rc != 0:
            raise Die("verification failed for " + vid)
        log("verify OK in %ds" % int(time.monotonic() - t0))
    else:
        log("verify skipped (--verify none)")
    drop_clone(r)
