"""One volume without its VM (bash cmd_migrate_volume, volume_preflight, create_volume_copy,
resolve_volume, dst_volumes_described, cmd_rollback_volume).

The source is only read through a temporary clone, so it stays untouched. State:
/var/lib/mig-migrate/vol-<source-id>. The copy keeps the source name; since Cinder names are not
unique, an earlier copy is recognised by its description "migrated from <source-id>".
"""
import json
import os
import re
import sys

from . import config, proc, progress, record
from .copyvol import brick, check_in_pool, cleanup_attach, copy_volume_cold, copy_volume_props, record_volume
from .options import source_type
from .preflight import (Checker, dest_type_checks, iscsid_preflight, quota_preflight, systemctl_ok, type_map_lines,
                        volume_type_lines)
from .rollback import drop_volume, no_source_id, recorded_source_id, require_destination, require_source
from .state import StateDir
from .util import UUID_RE, Die, Exit, jq_alt, jqs, lines, log, out, prog

SERVER_ONLY = ("--ip", "--flavor", "--flavor-fallback", "--security-group", "--server-group",
               "--start-source", "--keep-source-ports", "--volumes-only", "--no-server-group", "--no-copy-secgroup",
               "--dst-host", "--ephemeral-as-image")

# B2: the interpreter running migrate (the venv's, where openstacksdk lives), isolated (-I): never 'python3' from
# PATH. Tests point it at their stub.
SDK_PY = [sys.executable, "-I"]
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
                  "--volume-type, --az, --volume-az and --verify)" % bad)


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
    res = proc.run([*SDK_PY, "-c", _DESCRIBED, description], env=r.dst.env(), capture=True)
    return lines(res.stdout) if res.rc == 0 else None


def volume_preflight(r, st):
    o, cfg = r.opts, r.cfg
    c = Checker()
    vol = st.json("volume.json")
    vid = jqs(vol.get("id"))
    # 1.2.0: the type and pool of this volume (VOLUME_TYPE_MAP by its source type; with an empty map the run's)
    vtype, pool, frm = o.target(source_type(vol), cfg)
    out("Destination preflight (migrate-volume type=%s pool=%s%s%s):"
        % (vtype or "default", pool, (" az=" + o.dest_az) if o.dest_az else "",
           (" volume-az=" + o.volume_az) if o.volume_az != o.dest_az else ""))
    for line in o.conf_origin_info() + o.default_opts_info():
        out(line)
    if o.type_map_set(cfg):  # 1.2.0: where this volume lands and why (nothing new with an empty map)
        type_map_lines(r, o.volume_targets(r, st, "vol"))
        volume_type_lines(r, [vol])
    if o.in_use:
        if jqs(vol.get("status")) == "available":
            out("  INFO  --in-use ignored: %s is available, copied as usual" % vid)
        else:
            out("  INFO  %s is in-use: copied from a clone taken while attached (crash-consistent); its VM keeps running"
                % vid)
            c.check("volume not multiattach", lambda: vol.get("multiattach") is not True)
    if o.volume_az:
        c.check("volume AZ " + o.volume_az, lambda: o.volume_az in r.dst.run(
            "availability", "zone", "list", "--volume", "-f", "value", "-c", "Zone Name", stderr=proc.DEVNULL).stdout.split("\n"))
    dest_type_checks(r, c, [(vtype, frm)])
    c.check("volume not encrypted", lambda: vol.get("encrypted") is not True)
    dids = dst_volumes_described(r, "migrated from " + vid)
    if dids is None:
        c.failed("cannot list destination volumes")
    elif dids:
        c.failed("destination already has a copy of %s: %s — inspect it; roll back the run that made it (%s rollback %s, "
                 "on the host holding its state) or delete it, then re-run" % (vid, " ".join(dids), prog(), vid))
    else:
        out("  OK    no earlier copy of %s at the destination" % vid)
    quota_preflight(r, st, c, [], [vol], None, server=False)
    c.check("RBD access to pool " + pool, lambda: r.rbd("-p", pool, "ls"))
    iscsid_preflight(r, c)
    if cfg.MIG_MULTIPATH == "1":
        c.check("multipathd on transfer host", lambda: systemctl_ok("is-active", "multipathd", r.env))
    return not c.fail


def create_volume_copy(r, st):
    """Creates the destination volume for volume.json, sets r.dvid."""
    vj = st.json("volume.json")
    vid = recorded_source_id(vj)
    if vid is None:
        raise no_source_id(st, "volume.json")
    name = jq_alt(vj.get("name"))
    name = "" if name is None else jqs(name)
    vtype, pool, _frm = r.opts.target(source_type(vj), r.cfg)  # 1.2.0: VOLUME_TYPE_MAP by the source type
    args = ["--size", jqs(vj.get("size")), "--description", "migrated from " + vid, "-f", "value", "-c", "id"]
    if vtype:
        args += ["--type", vtype]
    if r.opts.volume_az:
        args += ["--availability-zone", r.opts.volume_az]
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
    record_volume(r, st, vid, r.dvid, vtype, pool)
    r.wait_volume(r.dst, r.dvid, "available")
    check_in_pool(r, r.dvid, vtype, pool)
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
    st.lock()  # R3 (A7): one migrate-volume or rollback per state directory, as for a VM
    r.volume_state = st  # run_volume unlocks it when the run ends
    if st.exists("volume-map"):  # another run wrote it between the check above and the lock
        raise Die("state exists in %s: run rollback first (%s rollback %s)" % (st.path, prog(), vid))
    st.write("volume.json", raw.rstrip("\n") + "\n")
    name = jq_alt(vj.get("name"))
    name = "" if name is None else jqs(name)
    size = jqs(vj.get("size"))
    out("Volume %s %s  %sGB bootable=%s type=%s" % (name or "(no name)", vid, size, jqs(vj.get("bootable")),
                                                    jqs(jq_alt(vj.get("type"), "-"))))
    if not volume_preflight(r, st):
        raise Exit(1)
    st.write("options", "VOLUME_TYPE=%s POOL=%s VERIFY=%s AZ=%s SRC_PROJECT=%s DST_PROJECT=%s VOLUME_AZ=%s\n"
             % (o.volume_type, r.pool, o.verify, o.dest_az, r.cfg.SRC_PROJECT, r.cfg.DST_PROJECT, o.volume_az))
    record.write_options_json(r, st, "vol", ["migrate", "volume", arg, *r.argv], {"volume_id": vid})  # 7.3 (1.2)
    st.write("volume-map", "")
    try:  # bash: trap cleanup_attach EXIT ... trap - EXIT
        create_volume_copy(r, st)
        log("destination volume %s created; copying from a clone of %s" % (r.dvid, vid))
        progress.phase("copy")
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
    progress.phase("done")


def cmd_rollback_volume(r, arg):
    """Volume name, ID or vol-<id>: deletes the copy, never touches the source."""
    st = find_volume_state(r, arg)
    st.lock()
    return rollback_volume(r, st)


def local_volume_state(arg):
    """1.1.1: the vol-<id> state directory of arg found without asking a cloud (arg is vol-<id>, or the ID itself);
    None when only the source cloud can tell (a volume name)."""
    root = StateDir("").root
    if os.path.exists(os.path.join(root, arg, "volume.json")):
        return StateDir(arg)
    if os.path.exists(os.path.join(root, "vol-" + arg, "volume.json")):
        return StateDir("vol-" + arg)
    return None


def find_volume_state(r, arg):
    """The vol-<id> state directory a rollback of arg works on (name, ID or vol-<id>); Die when there is none."""
    root = StateDir("").root
    st = local_volume_state(arg)
    if st is None:
        vid = resolve_volume(r, arg)
        st = StateDir("vol-" + vid)
        if not (vid and st.exists("volume.json")):
            raise Die("no state for %s: no VM state in %s and no migrate-volume state for a source volume of that "
                      "name or ID" % (arg, os.path.join(root, arg)))
    return st


def rollback_volume(r, st):
    vj = st.json("volume.json")
    vid = recorded_source_id(vj)
    if vid is None:
        raise no_source_id(st, "volume.json")
    # A20: the source volume answers before its copy is deleted.
    require_source(r, "volume", vid, "volume " + (jqs(vj.get("name")) if vj.get("name") else ""), st)
    # A21: and the destination answers (its first recorded volume) before the copy is deleted.
    require_destination(r, st)
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
    return st
