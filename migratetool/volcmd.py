"""One volume without its VM (bash cmd_migrate_volume, volume_preflight, create_volume_copy,
resolve_volume, dst_volumes_described, cmd_rollback_volume).

The source is only read through a temporary clone, so it stays untouched. State:
/var/lib/mig-migrate/vol-<source-id>. The copy keeps the source name; since Cinder names are not
unique, an earlier copy is recognised by its description "migrated from <source-id>".
"""
import json
import os
import re

from . import config, proc
from .copyvol import brick, cleanup_attach, copy_volume_cold, copy_volume_props
from .preflight import Checker, systemctl_ok
from .rollback import drop_volume
from .state import StateDir
from .util import UUID_RE, Die, Exit, jq_alt, jqs, lines, log, out, prog

SERVER_ONLY = ("--ip", "--flavor", "--flavor-fallback", "--security-group", "--server-group", "--keep-server-group",
               "--start-source", "--keep-source-ports", "--volumes-only")

_DESCRIBED = """import sys
import openstack
for v in openstack.connect().block_storage.volumes(details=True):
    if v.description == sys.argv[1]:
        print(v.id)
"""


def volume_options_only(args):
    """Refuse the options that only make sense with a server (bash volume_options_only)."""
    bad = ""
    prev = ""
    for a in args:
        if a in SERVER_ONLY:
            bad += " " + a
        if prev == "--mode" and a == "live":
            bad += " --mode live"
        prev = a
    if bad:
        raise Die("migrate volume copies one volume and creates no server or port:%s cannot apply (it takes "
                  "--volume-type, --az and --verify)" % bad)


def resolve_volume(r, name):
    """The source volume ID (source project); '' when unknown."""
    ids = lines(r.src.out("volume", "list", "--name", name, "-f", "value", "-c", "ID"))
    if len(ids) > 1:
        raise Die("%d volumes are named '%s' in the source project: %s — re-run with the volume ID"
                  % (len(ids), name, " ".join(ids)))
    if ids:
        return ids[0]
    return name if re.fullmatch(UUID_RE, name) else ""


def dst_volumes_described(r, description):
    """IDs of the destination project's volumes carrying that description; None when listing fails.
    openstackclient has no description column or filter: ask the SDK it is built on."""
    # "python3" from PATH, as bash ran it: the venv's (its bin comes first), where openstacksdk lives.
    res = proc.run(["python3", "-c", _DESCRIBED, description], env=r.dst.env(), capture=True)
    return lines(res.stdout) if res.rc == 0 else None


def volume_preflight(r, st):
    o, cfg = r.opts, r.cfg
    c = Checker()
    vol = st.json("volume.json")
    vid = jqs(vol.get("id"))
    out("Destination preflight (migrate-volume type=%s pool=%s%s):"
        % (o.volume_type or "default", r.pool, (" az=" + o.dest_az) if o.dest_az else ""))
    for line in o.conf_origin_info() + o.default_opts_info():
        out(line)
    if o.in_use:
        if jqs(vol.get("status")) == "available":
            out("  INFO  --in-use ignored: %s is available, copied as usual" % vid)
        else:
            out("  INFO  %s is in-use: copied from a clone taken while attached (crash-consistent); its VM keeps running"
                % vid)
            c.check("volume not multiattach", lambda: vol.get("multiattach") is not True)
    if o.dest_az:
        c.check("volume AZ " + o.dest_az, lambda: o.dest_az in r.dst.run(
            "availability", "zone", "list", "--volume", "-f", "value", "-c", "Zone Name", stderr=proc.DEVNULL).stdout.split("\n"))
    if o.volume_type:
        c.check("volume type " + o.volume_type, lambda: r.dst.ok("volume", "type", "show", o.volume_type))
    c.check("volume not encrypted", lambda: vol.get("encrypted") is not True)
    dids = dst_volumes_described(r, "migrated from " + vid)
    if dids is None:
        c.failed("cannot list destination volumes")
    elif dids:
        c.failed("destination already has a copy of %s: %s — inspect it; roll back the run that made it (%s rollback %s, "
                 "on the host holding its state) or delete it, then re-run" % (vid, " ".join(dids), prog(), vid))
    else:
        out("  OK    no earlier copy of %s at the destination" % vid)
    c.check("RBD access to pool " + r.pool, lambda: r.rbd("-p", r.pool, "ls"))
    c.check("iSCSI daemon on transfer host", lambda: systemctl_ok("is-active", "iscsid", r.env)
            or systemctl_ok("start", "iscsid", r.env))
    if cfg.MIG_MULTIPATH == "1":
        c.check("multipathd on transfer host", lambda: systemctl_ok("is-active", "multipathd", r.env))
    return not c.fail


def create_volume_copy(r, st):
    """Creates the destination volume for volume.json, sets r.dvid."""
    vj = st.json("volume.json")
    vid = jqs(vj.get("id"))
    name = jq_alt(vj.get("name"))
    name = "" if name is None else jqs(name)
    args = ["--size", jqs(vj.get("size")), "--description", "migrated from " + vid, "-f", "value", "-c", "id"]
    if r.opts.volume_type:
        args += ["--type", r.opts.volume_type]
    if r.opts.dest_az:
        args += ["--availability-zone", r.opts.dest_az]
    # The source name verbatim, an empty one included (Cinder accepts it); the ID only as fallback.
    if name:
        r.dvid = r.dst.value("volume", "create", name, *args)
    else:
        res = r.dst.run("volume", "create", "", *args, stderr=st.p("create.err"))
        if res.rc == 0:
            r.dvid = res.stdout.rstrip("\n")
        else:
            last = (st.read("create.err").splitlines() or [""])[-1]
            log("WARN source volume %s has no name and the destination refused an empty one (%s): the copy is named %s"
                % (vid, last, vid))
            r.dvid = r.dst.value("volume", "create", vid, *args)
    # Recorded before the wait, so that rollback also removes a copy that never became available.
    st.append("volume-map", "%s %s" % (vid, r.dvid))
    r.wait_volume(r.dst, r.dvid, "available")
    if not r.rbd("-p", r.pool, "info", "volume-" + r.dvid):
        raise Die("volume-%s not visible in pool %s: wrong RBD_POOL_MAP for type '%s', or %s lacks caps on that pool"
                  % (r.dvid, r.pool, r.opts.volume_type or "default", r.cfg.RBD_USER))
    copy_volume_props(r, vj, r.dvid)


def cmd_migrate_volume(r, arg):
    o = r.opts
    vid = resolve_volume(r, arg)
    if not vid:
        raise Die("no volume named '%s' in the source project" % arg)
    st = StateDir("vol-" + vid)
    if st.exists("volume-map"):
        raise Die("state exists in %s: run rollback first (%s rollback %s)" % (st.path, prog(), vid))
    raw = r.src.out("volume", "show", vid, "-f", "json")
    vj = json.loads(raw)
    status = jqs(vj.get("status"))
    if status == "in-use":
        if not o.in_use:
            raise Die("volume %s (%s) is in-use: an attached volume goes with its VM (%s migrate <vm> --volumes-only), "
                      "or copy it live with --in-use (crash-consistent)" % (arg, vid, prog()))
    elif status != "available":
        raise Die("volume %s (%s) is %s: migrate-volume copies available volumes only" % (arg, vid, status))
    st.create()
    st.write("volume.json", raw.rstrip("\n") + "\n")
    name = jq_alt(vj.get("name"))
    name = "" if name is None else jqs(name)
    size = jqs(vj.get("size"))
    out("Volume %s %s  %sGB bootable=%s type=%s" % (name or "(no name)", vid, size, jqs(vj.get("bootable")),
                                                    jqs(jq_alt(vj.get("type"), "-"))))
    if not volume_preflight(r, st):
        raise Exit(1)
    st.write("options", "VOLUME_TYPE=%s POOL=%s VERIFY=%s AZ=%s SRC_PROJECT=%s DST_PROJECT=%s\n"
             % (o.volume_type, r.pool, o.verify, o.dest_az, r.cfg.SRC_PROJECT, r.cfg.DST_PROJECT))
    st.write("volume-map", "")
    try:  # bash: trap cleanup_attach EXIT ... trap - EXIT
        create_volume_copy(r, st)
        log("destination volume %s created; copying from a clone of %s" % (r.dvid, vid))
        copy_volume_cold(r, vid, r.dvid, size, st)
    except BaseException:
        cleanup_attach(r)
        raise
    if status == "in-use":
        st.write("clone-time", r.clone_time + "\n")
        out("Volume copied crash-consistent as of %s: data written after that moment" % r.clone_time)
        out("is NOT in the copy (like a sudden power-off; a busy DB will need crash recovery on first mount).")
        out("Source VM and volume untouched.")
    out("Volume migrated: %s %s -> %s. Source volume untouched (still %s). Rollback: %s rollback %s"
        % (name or "(no name)", vid, r.dvid, status, prog(), vid))


def cmd_rollback_volume(r, arg):
    """Volume name, ID or vol-<id>: deletes the copy, never touches the source."""
    root = StateDir("").root
    if os.path.exists(os.path.join(root, arg, "volume.json")):
        st = StateDir(arg)
    elif os.path.exists(os.path.join(root, "vol-" + arg, "volume.json")):
        st = StateDir("vol-" + arg)
    else:
        vid = resolve_volume(r, arg)
        st = StateDir("vol-" + vid)
        if not (vid and st.exists("volume.json")):
            raise Die("no state for %s: no VM state in %s and no migrate-volume state for a source volume of that "
                      "name or ID" % (arg, os.path.join(root, arg)))
    vid = jqs(st.json("volume.json").get("id"))
    if st.exists("volume-map"):
        for _svid, dvid in st.volume_map():
            drop_volume(r, r.dst, dvid, "destination volume")
    # A run that died mid-copy leaves its clone behind, possibly still attached to this host.
    # A failing listing inside 'for cid in $(...)' did not stop the bash build either.
    for cid in lines(r.src.run("volume", "list", "--name", "mig-clone-" + vid, "-f", "value", "-c", "ID").stdout):
        if os.path.exists(os.path.join(config.ATTACH_DIR, cid + ".json")):
            brick(r, "detach", cid, proc.DEVNULL)  # || true
        drop_volume(r, r.src, cid, "leftover clone")
    st.retire()
    log("rollback done; source volume %s was never modified" % vid)
