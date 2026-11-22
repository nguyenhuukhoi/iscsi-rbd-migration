"""Live mode: hash-cached pre-sync passes while the source VM runs, then one delta pass at cutover
(bash sync_volume_pass and the live loop of cmd_migrate).

blocksync.py ships inside this package (archive/blocksync.py of the bash build, plus the whole-chunk read of
A1: a short read fails the pass, exit 4), and is run as a file by the SYSTEM python (SYS_PY, default /usr/bin/python3): the librbd bindings
(python3-rbd) live there, not in the venv that runs migrate. It is never imported by migrate.
Bundled rather than installed separately in /usr/local/bin: one 'pip install' then carries both
ends of the JSON statistics contract, so they cannot drift apart.
"""
import json
import math
import os
import re
import sys
import time

from . import proc, progress
from .copyvol import attach_clone, drop_clone
from .util import Die, log

BLOCKSYNC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blocksync.py")
CHUNK = 4194304
# blocksync.py's progress line on stderr (7.3: read as a blocksync event with --progress-json)
BLOCKSYNC_LINE = re.compile(r"^\[blocksync\] (\d+)% .*changed (\d+) MiB")


def sync_volume_pass(r, vid, dvid, st, pass_no, first, final):
    """One blocksync pass from a fresh clone; returns the pass delta in MB (bash CHANGED_MB)."""
    vflag = ["--verify", "none"] if r.opts.verify == "none" else ["--verify", "readback"]
    attach_clone(r, vid, st)
    # -I: the script's own directory (this package) must not shadow the modules blocksync and librbd import.
    cmd = [r.cfg.SYS_PY, "-I", BLOCKSYNC, "--device", r.device, "--pool", r.pool, "--image", "volume-" + dvid,
           "--id", r.cfg.RBD_USER, "--conf", r.cfg.CEPH_CONF, "--state", st.p("hash-%s.bin" % vid)]
    if first:
        cmd.append("--first")
    # stdout: one JSON line of statistics; stderr: the "[blocksync] NN%" progress, passed through.
    t0 = time.monotonic()
    if progress.ENABLED:
        # 7.3 (7): the same stderr lines, passed through as they come, each also read as a blocksync event.
        def on_err(line):
            sys.stderr.write(line)
            sys.stderr.flush()
            m = BLOCKSYNC_LINE.match(line)
            if m:  # the closing 100% of a pass always goes, as the closing copy line does (SEC-17)
                progress.emit("blocksync", force=m.group(1) == "100", volume=vid, pct=int(m.group(1)),
                              changed_mib=int(m.group(2)), **{"pass": pass_no})
        res = proc.run_stream(cmd + vflag, env=r.env, on_stderr_line=on_err)
    else:
        res = proc.run(cmd + vflag, env=r.env, capture=True, stderr=proc.INHERIT)
    if final:  # 7.2 (D3): only the cutover pass is inside the downtime window
        r.t_copy += time.monotonic() - t0
    if res.rc != 0:
        raise Die("blocksync failed for %s (pass %s)%s" % (vid, pass_no, ": a short read from the source clone (exit 4)"
                                                          if res.rc == 4 else ""))
    out = res.stdout.rstrip("\n")
    st.append("sync-%s.log" % vid, out)
    try:
        stats = json.loads(out)
        # Number literals as blocksync wrote them, the way jq prints an unmodified number.
        lit = json.loads(out, parse_float=str, parse_int=str)
    except ValueError:
        raise Die("blocksync printed no statistics for %s (pass %s): %r" % (vid, pass_no, out[-200:]))
    changed_mb = math.ceil((stats["written_bytes"] + stats["zero_chunks"] * CHUNK) / 1048576)
    log("pass %s %s: %s/%s chunks changed, %d MB written, %ss" % (
        pass_no, vid, lit["changed"], lit["chunks"], math.floor(stats["written_bytes"] / 1048576), lit["seconds"]))
    progress.emit("pass", volume=vid, n=pass_no, chunks=stats["changed"], mb=math.floor(stats["written_bytes"] / 1048576),
                  secs=stats["seconds"])
    if final and r.opts.verify == "full":
        log("full compare (cutover, --verify full)")
        progress.emit("phase", name="verify", volume=vid)
        img = "rbd:%s/volume-%s:id=%s:conf=%s" % (r.pool, dvid, r.cfg.RBD_USER, r.cfg.CEPH_CONF)
        t0 = time.monotonic()
        rc = proc.run(["qemu-img", "compare", "-f", "raw", "-F", "raw", r.device, img], env=r.env, capture=False).rc
        r.t_verify += time.monotonic() - t0
        if rc != 0:
            raise Die("full verification failed for " + vid)
    drop_clone(r)
    return changed_mb


def presync(r, vids, st):
    """Pre-sync while the source VM keeps running. Each pass reads a fresh crash-consistent clone;
    only the chunks whose hash changed since the previous pass are written."""
    from .copyvol import create_dest_volume
    max_passes, delta_limit = int(r.opts.max_passes), int(r.opts.delta_limit_mb)  # checked by check_numbers
    for vid in vids:
        create_dest_volume(r, vid, st)
        pass_no, first = 1, True
        while True:
            changed_mb = sync_volume_pass(r, vid, r.dvid, st, pass_no, first, False)
            first = False
            if pass_no >= 2 and changed_mb <= delta_limit:
                log("%s converged (delta %d MB <= %d MB)" % (vid, changed_mb, delta_limit))
                break
            if pass_no >= max_passes:
                log("WARN %s did not converge after %d passes (last delta %d MB); cutover will carry it"
                    % (vid, pass_no, changed_mb))
                break
            pass_no += 1


def check_numbers(opts):
    """bash compared these with [ -ge ]/[ -le ]: a non-number never matched, so the pre-sync loop
    of a bad --max-passes never ended. Refuse them before anything starts. 7.2 (C2): --max-passes 0 ran
    as 1: at least one pass is asked for explicitly."""
    for opt, value, least in (("--max-passes", opts.max_passes, 1), ("--delta-limit", opts.delta_limit_mb, 0)):
        if not (value.isdigit() and int(value) >= least):
            raise Die("%s takes a whole number >= %d, not '%s'" % (opt, least, value))


def final_pass(r, vid, st):
    """Cutover: the last delta pass, with readback (and a full compare with --verify full)."""
    sync_volume_pass(r, vid, st.dst_volume_of(vid), st, "final", False, True)


def preflight_checks(r, c):
    """The two live-only preflight lines (bash: [ -x $BLOCKSYNC ], $SYS_PY -c 'import rados, rbd')."""
    c.check("blocksync present", lambda: os.access(BLOCKSYNC, os.R_OK))
    if not os.access(BLOCKSYNC, os.R_OK):
        c.info("%s is missing from the migratetool package: reinstall it (pip install .)" % BLOCKSYNC)
    sys_py = r.cfg.SYS_PY
    # Same interpreter flags as the real call, so the check tells the truth about it.
    ok = proc.run([sys_py, "-I", "-c", "import rados, rbd"], env=r.env, capture=True, stderr=proc.DEVNULL).rc == 0
    c.check("librbd python bindings", lambda: ok)
    if not ok:
        c.info("blocksync runs under SYS_PY=%s, which cannot import rados/rbd: install python3-rbd for that "
               "interpreter, or point SYS_PY at one that has it" % sys_py)
