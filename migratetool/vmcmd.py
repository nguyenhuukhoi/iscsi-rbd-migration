"""plan and migrate of one VM (bash cmd_plan, cmd_migrate): cold and live. DESIGN.md, "migrate"."""
import time

from . import contract, jsonout, livesync, proc, progress, record, secgroups
from .context import flavor_name
from .copyvol import cleanup_attach, copy_volume_cold, create_dest_volume
from .ephemeral import migrate_ephemeral_root
from .preflight import preflight
from .rollback import clean_before_cutover
from .server import create_destination, detach_source_ports
from .jsonout import utc
from .util import Die, Exit, jqs, lines, log, out, prog
from .vmdef import detect_boot, dest_volume_name, export_definition


def plan_facts(r, st):
    """D16: what the plan row tells the UI besides the checks: volume count and size, NICs, the source's power, and
    the impact of a migrate with these options (what stops, what is detached, what is created)."""
    o = r.opts
    vols = st.json("volumes.json")
    ports = st.json("ports.json")
    gb = sum(int(v.get("size")) for v in vols if str(v.get("size")).isdigit())
    server = st.json("server.json")
    root_volume = 1 if (r.ephemeral and not o.ephemeral_as_image and not o.volumes_only) else 0
    made_sg = [g for g in r.sgplan.groups.values() if g.create] if r.sgplan is not None else []
    detach = 0 if (o.volumes_only or o.ipmode != "same" or o.keep_ports) else len(ports)
    return {"volumes_total": len(vols), "gb_total": gb, "nics": len(ports),
            "source_power": server.get("status") if isinstance(server.get("status"), str) else None,
            "impact": {"stop_source": True, "detach_ports": detach,
                       "source_after": "running" if o.start_source else "SHUTOFF+locked",
                       "creates": {"server": 0 if o.volumes_only else 1,
                                   "ports": 0 if o.volumes_only else len(ports),
                                   "volumes": len(vols) + root_volume,
                                   "security_groups": 0 if o.volumes_only else len(made_sg),
                                   "server_group": 1 if (r.ksg_dst and getattr(r, "ksg_create", False)) else 0}}}


def cmd_plan(r, st):
    export_definition(r, st.arg, st)
    r.ephemeral = detect_boot(st)
    out("VM %s  status=%s" % (st.arg, jqs(st.json("server.json").get("status"))))
    for p in st.json("ports.json"):
        out("  port  %s %s %s sg=%s" % (jqs(p.get("network")), jqs(p.get("ip")), jqs(p.get("mac")),
                                        ",".join(jqs(s) for s in (p.get("security_groups") or []))))
        for a in p.get("floating_ips") or []:
            out("  fip   %s on %s" % (jqs(a), jqs(p.get("mac"))))
    for v in st.json("volumes.json"):
        out("  disk  %s %s %sGB bootable=%s" % (jqs(v.get("name")), jqs(v.get("id")), jqs(v.get("size")),
                                               jqs(v.get("bootable"))))
    good = preflight(r, st)
    if jsonout.EFFECTIVE_LINE:  # plan --json: what 'migrate' would run with (and D16 facts), for the parent's row
        jsonout.emit_json({"effective": r.opts.effective(r, st, "vm"), "facts": plan_facts(r, st)},
                          prefix=contract.PROGRESS_PREFIX, cfg=r.cfg)
    if not good:
        raise Exit(1)
    out(contract.PREFLIGHT_PASSED)


def recheck_source(r, st, sid):
    """A12: right before the source stops, its volumes, ports and flavor must still be those of the export (a volume
    hot-attached or a NIC added during a long pre-sync would not be migrated). Read-only; a change stops the run
    before the cutover (exit 3), and a re-run exports again."""
    want_vols = {a.get("Volume ID") for a in st.json("attachments.json")}
    want_ports = {p.get("ID") for p in st.json("port-ids.json")}
    want_flavor = flavor_name(st.json("server.json"))
    now_vols = set(lines(r.src.value("server", "volume", "list", sid, "-f", "value", "-c", "Volume ID")))
    now_ports = set(lines(r.src.value("port", "list", "--server", sid, "-f", "value", "-c", "ID")))
    now_flavor = flavor_name(r.src.json("server", "show", sid, "-f", "json", "-c", "flavor"))
    diffs = []
    if now_vols != want_vols:
        diffs.append("volumes %s, exported %s" % (" ".join(sorted(now_vols)) or "none",
                                                   " ".join(sorted(want_vols)) or "none"))
    if now_ports != want_ports:
        diffs.append("ports %s, exported %s" % (" ".join(sorted(now_ports)) or "none",
                                                 " ".join(sorted(want_ports)) or "none"))
    if now_flavor != want_flavor:
        diffs.append("flavor %s, exported %s" % (now_flavor or "?", want_flavor or "?"))
    if diffs:
        raise Die("the source VM changed since migrate exported it (%s): it was not stopped; run migrate again (it "
                  "exports the VM again)" % "; ".join(diffs))


def cmd_migrate(r, st):
    o = r.opts
    vm = st.arg
    st.create()
    st.lock()  # A7: one run per state directory
    if st.exists("volume-map"):
        t = st.read("t-stop").strip()
        if t or st.nonempty("dst-server-id"):
            # A6: past its cutover the earlier run touched the source: only a rollback undoes that.
            r.exit_code = contract.EXIT_AFTER_CUTOVER
            when = (" at " + utc(int(t))) if t.isdigit() else ""
            raise Die((contract.STATE_EXISTS % st.path) + " (that run stopped the source%s)" % when)
        # A6: it failed before the cutover: clean what it made at the destination and start again.
        clean_before_cutover(r, st)
        st.create()
        st.lock()
    export_definition(r, vm, st)
    r.ephemeral = detect_boot(st)
    if not preflight(r, st):
        raise Exit(1)
    # 7.3 (6): DST_HOST= only when --dst-host is given, so that the line of every other run stays as it was.
    st.write("options", "MODE=%s IPMODE=%s IPADDR=%s VOLUME_TYPE=%s POOL=%s VERIFY=%s FLAVOR=%s SG=%s EPHEMERAL=%s AZ=%s VOLUME_AZ=%s%s\n"
             % (o.mode, o.ipmode, o.ipaddr, o.volume_type, r.pool, o.verify, o.ov_flavor, o.ov_sg,
                1 if r.ephemeral else 0, o.dest_az, o.volume_az, (" DST_HOST=" + o.dst_host) if o.dst_host else ""))
    # 7.3 (1.2): the same run, machine-readable: command, effective options and their origin, conf whitelist.
    # A2: the marks every destination object of this run carries; rollback deletes an unrecorded server only with them.
    src_id = jqs(st.json("server.json").get("id"))
    record.write_options_json(r, st, "vm", record.vm_command("migrate", vm, r.argv),
                              {"server_id": src_id,
                               "owner_mark": {"server": {contract.OWNER_SOURCE: src_id, contract.OWNER_STATE: vm},
                                              "port_description": contract.PORT_OWNER_PREFIX + vm}})
    # P6: the security group copies are made while the source still runs, not inside the downtime window.
    # A failure here leaves no volume-map: fix and re-run migrate (no rollback needed).
    if r.sgplan is not None:
        secgroups.ensure(r, r.sgplan, st)
    try:  # bash: trap cleanup_attach EXIT ... trap - EXIT
        sid = st.json("server.json").get("id")
        st.write("volume-map", "")
        vids = [a.get("Volume ID") for a in st.json("attachments.json")]

        if o.mode == "live":
            progress.phase("presync")
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
        recheck_source(r, st, sid)

        # Cutover: from here the service is down until the destination VM boots.
        t_start = time.monotonic()
        t_stop = int(time.time())
        st.write("t-stop", "%d\n" % t_stop)
        r.cut = True  # C8: a failure from here on exits 4 (the source is stopped: roll back)
        progress.phase("cutover")
        progress.emit("t_stop", at=utc(t_stop))
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
            progress.phase("copy")  # the root disk travels through Glance: data through this host
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

        if vids and not r.ephemeral:
            progress.phase("copy")
        for vid in vids:
            if o.mode == "live":
                livesync.final_pass(r, vid, st)
            else:
                copy_volume_cold(r, vid, st.dst_volume_of(vid), sizes.get(vid)
                                 or r.src.value("volume", "show", vid, "-f", "value", "-c", "size"), st)

        if o.volumes_only:
            total = time.monotonic() - t_start
            log("volumes copied; source down for %ds so far" % int(total))
        else:
            progress.phase("create")
            create_destination(r, vm, st)
            total = time.monotonic() - t_start
            log("destination VM ACTIVE; control-plane downtime so far %ds" % int(total))
        # 7.2 (D3): what the window was spent on; control-plane = the rest (openstack calls, waits, attach).
        # 7.3 (8, D3b): an ephemeral root disk's snapshot, download and upload count as copy, named in brackets.
        glance = (" (glance %ds)" % int(r.t_glance)) if r.ephemeral else ""
        log("downtime breakdown: total %ds, copy %ds%s, verify %ds, control-plane %ds"
            % (int(total), int(r.t_copy), glance, int(r.t_verify), max(int(total - r.t_copy - r.t_verify), 0)))
        fields = {"glance": int(r.t_glance)} if r.ephemeral else {}
        progress.emit("downtime", total=int(total), copy=int(r.t_copy), verify=int(r.t_verify),
                      control_plane=max(int(total - r.t_copy - r.t_verify), 0), **fields)
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
        progress.phase("done")
        return
    out("Migrated. Source VM: %s. Rollback: %s rollback %s" % (state, prog(), vm))
    progress.phase("done")
