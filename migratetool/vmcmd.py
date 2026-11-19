"""plan and migrate of one VM (bash cmd_plan, cmd_migrate): cold and live."""
import time

from . import livesync, proc, secgroups
from .copyvol import cleanup_attach, copy_volume_cold, create_dest_volume
from .ephemeral import migrate_ephemeral_root
from .preflight import preflight
from .server import create_destination, detach_source_ports
from .util import Die, Exit, jqs, log, out, prog
from .vmdef import detect_boot, dest_volume_name, export_definition


def cmd_plan(r, st):
    export_definition(r, st.arg, st)
    r.ephemeral = detect_boot(st)
    out("VM %s  status=%s" % (st.arg, jqs(st.json("server.json").get("status"))))
    for p in st.json("ports.json"):
        out("  port  %s %s %s sg=%s" % (jqs(p.get("network")), jqs(p.get("ip")), jqs(p.get("mac")),
                                        ",".join(jqs(s) for s in (p.get("security_groups") or []))))
    for v in st.json("volumes.json"):
        out("  disk  %s %s %sGB bootable=%s" % (jqs(v.get("name")), jqs(v.get("id")), jqs(v.get("size")),
                                               jqs(v.get("bootable"))))
    if not preflight(r, st):
        raise Exit(1)
    out("Preflight passed.")


def cmd_migrate(r, st):
    o = r.opts
    vm = st.arg
    if st.exists("volume-map"):
        raise Die("state exists in %s: run rollback first" % st.path)
    export_definition(r, vm, st)
    r.ephemeral = detect_boot(st)
    if not preflight(r, st):
        raise Exit(1)
    st.write("options", "MODE=%s IPMODE=%s IPADDR=%s VOLUME_TYPE=%s POOL=%s VERIFY=%s FLAVOR=%s SG=%s EPHEMERAL=%s AZ=%s\n"
             % (o.mode, o.ipmode, o.ipaddr, o.volume_type, r.pool, o.verify, o.ov_flavor, o.ov_sg,
                1 if r.ephemeral else 0, o.dest_az))
    # P6: the security group copies are made while the source still runs, not inside the downtime window.
    # A failure here leaves no volume-map: fix and re-run migrate (no rollback needed).
    if r.sgplan is not None:
        secgroups.ensure(r, r.sgplan, st)
    try:  # bash: trap cleanup_attach EXIT ... trap - EXIT
        sid = st.json("server.json").get("id")
        st.write("volume-map", "")
        vids = [a.get("Volume ID") for a in st.json("attachments.json")]

        if o.mode == "live":
            livesync.presync(r, vids, st)
        else:
            # 7.2 (A1): every destination volume exists before the source stops, as in live mode. They are
            # empty and need nothing from the source, so whatever the destination refuses (quota, type, pool,
            # caps) fails here, with the source VM still running. volume-map is written per volume as it
            # is created, so a rollback removes them.
            for vid in vids:
                create_dest_volume(r, vid, st)
            if vids:
                log("destination volumes created; stopping the source now")
        # From volumes.json (exported above): no 'volume show' inside the downtime window.
        sizes = {v.get("id"): jqs(v.get("size")) for v in st.json("volumes.json")}

        # Cutover: from here the service is down until the destination VM boots.
        t_start = time.monotonic()
        st.write("t-stop", "%d\n" % int(time.time()))
        # A pre-existing lock (an interrupted earlier run, or an operator lock) makes
        # "server stop" fail with 409. Migrating this VM is the operator's explicit
        # intent, so lift the lock and continue; the tool re-locks right below.
        if r.src.try_value_inherit("server", "show", sid, "-f", "value", "-c", "locked") == "True":
            res = r.src.run("server", "show", sid, "-f", "value", "-c", "locked_reason", stderr=proc.DEVNULL)
            reason = res.stdout.rstrip("\n") if res.rc == 0 else "unknown"
            log("WARN source VM is already locked (reason: %s); unlocking to proceed" % reason)
            r.src.out("server", "unlock", sid)
        if r.src.try_value_inherit("server", "show", sid, "-f", "value", "-c", "status") != "SHUTOFF":
            log("stop source VM")
            r.src.out("server", "stop", sid)
            r.wait_server(r.src, sid, "SHUTOFF")
        # Everything that still mutates the source server through Nova (port detach, ephemeral
        # snapshot) must happen BEFORE the lock: Nova's lock blocks these calls for non-admin
        # credentials (admins silently bypass it).
        # --volumes-only never detaches: no destination port will take over the MAC+IP.
        if not o.volumes_only and o.ipmode == "same" and not o.keep_ports:
            detach_source_ports(r, vm, st, sid)
        if r.ephemeral:
            migrate_ephemeral_root(r, vm, st, sid)
        r.src.out("server", "lock", sid)
        if o.volumes_only:
            log("source stopped and locked; ports left attached (--volumes-only creates no destination port)")
        elif o.ipmode != "same":
            log("source stopped and locked; ports left attached (--ip %s keeps them out of conflict)" % o.ipmode)
        elif o.keep_ports:
            log("WARN --keep-source-ports: source ports stay attached with the same MAC+IP as the destination VM. "
                "Only SHUTOFF+lock keeps it off the wire — powering it on causes split-brain. Do not start it; "
                "delete it once the migration is accepted.")
        else:
            log("source stopped, its ports detached, now locked")

        for vid in vids:
            if o.mode == "live":
                livesync.final_pass(r, vid, st)
            else:
                copy_volume_cold(r, vid, st.dst_volume_of(vid), sizes.get(vid)
                                 or r.src.value("volume", "show", vid, "-f", "value", "-c", "size"), st)

        if o.volumes_only:
            log("volumes copied; source down for %ds so far" % int(time.monotonic() - t_start))
        else:
            create_destination(r, vm, st)
            log("destination VM ACTIVE; control-plane downtime so far %ds" % int(time.monotonic() - t_start))
    except BaseException:
        cleanup_attach(r)
        raise

    if o.start_source:
        if o.volumes_only:
            log("WARN --start-source: the source VM runs again; its data diverges from the copied volumes from now on")
        else:
            log("WARN --start-source: both VMs will run with the same hostname and SSH host key; their data "
                "diverges from this cutover")
        r.src.out("server", "unlock", sid)
        r.src.out("server", "start", sid)
        r.wait_server(r.src, sid, "ACTIVE")
        log("source VM started again")
    state = "running again (--start-source)" if o.start_source else "kept SHUTOFF and locked"
    if o.volumes_only:
        out("Volumes migrated (--volumes-only, no destination server or port created):")
        for svid, dvid in st.volume_map():
            out("  %s  %s  (from %s)" % (dest_volume_name(svid, st), dvid, svid))
        out("Source VM: %s. Rollback: %s rollback %s" % (state, prog(), vm))
        return
    out("Migrated. Source VM: %s. Rollback: %s rollback %s" % (state, prog(), vm))
