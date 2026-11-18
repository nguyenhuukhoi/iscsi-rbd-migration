"""Ports and servers on both sides (bash detach_source_ports, keep_sg_ensure, create_destination)."""
import shlex
import time

from . import proc
from .preflight import dst_server_groups_named
from .context import flavor_name
from .util import Die, ifs_split, jq_alt, jqs, log

USER_DATA = """#cloud-config
ssh_deletekeys: false
preserve_hostname: true
"""


# --- source ports ------------------------------------------------------------------------------
# A stopped VM with its port attached can still be started, by an admin too, despite the lock. In
# the lab that gave two VMs on one IP and MAC. Disabling the port is not an option: the source cloud
# then blackholes traffic from its remaining VMs to the migrated MAC. Detaching keeps the port
# allocated and removes the NIC from the VM.
def detach_source_ports(r, vm, st, sid):
    st.write("source-ports", "")
    ports = st.json("ports.json")
    for i, p in enumerate(st.json("port-ids.json")):
        pid = p["ID"]
        r.src.out("server", "remove", "port", sid, pid)
        gone, dev = False, ""
        for _ in range(60):
            res = r.src.run("port", "show", pid, "-f", "value", "-c", "device_id", stderr=proc.DEVNULL)
            if res.rc != 0:
                dev, gone = "", True
                break
            dev = res.stdout.rstrip("\n")
            if not dev:
                break
            time.sleep(2)
        if dev:
            raise Die("port %s still attached to %s" % (pid, dev))
        if gone:
            pid = _reserve_port(r, vm, st, ports[i], i)
        st.append("source-ports", pid)


def _reserve_port(r, vm, st, port, i):
    """Nova deletes ports it created itself. Re-create at once to hold the IP and MAC for rollback.
    Groups go by ID (an admin sees every project's "default"). This runs mid-cutover and the port's
    job is holding the address, so a group that does not resolve is left out, not fatal."""
    net, ip, mac = jqs(port.get("network")), jqs(port.get("ip")), jqs(port.get("mac"))
    if not r.src._pid:
        tok = r.src.run("token", "issue", "-f", "value", "-c", "project_id", stderr=proc.DEVNULL)
        if tok.rc == 0:
            r.src._pid = tok.stdout.rstrip("\n")
    sg_args, missed = [], []
    for sg in port.get("security_groups") or []:
        if not sg:
            continue
        try:
            sgid, why = r.src.sg_id(sg), "not found"
        except Die as e:
            sgid, why = "", str(e)
        if sgid:
            sg_args += ["--security-group", sgid]
        else:
            missed.append(sg)
            log("WARN source security group %s not resolved in source project %s: %s"
                % (sg, r.cfg.SRC_PROJECT or r.src._pid or "?", why))
    if not sg_args:
        sg_args = ["--no-security-group"]
    res = r.src.run("port", "create", "mig-reserve-%s-%d" % (vm, i), "--network", net, "--mac-address", mac,
                    "--fixed-ip", "ip-address=" + ip, *sg_args, "-f", "value", "-c", "id")
    if res.rc != 0:
        raise Die("could not re-create a port holding %s %s on %s: that address is no longer reserved for rollback"
                  % (ip, mac, net))
    pid = res.stdout.rstrip("\n")
    log("Nova removed its own port; reserved %s %s again as %s" % (ip, mac, pid))
    # Rollback reattaches this very port to the source VM, so it comes back without these groups.
    if missed:
        log("WARN reserve port %s has no security group %s; after a rollback re-add them: openstack port set%s %s"
            % (pid, " ".join(missed), "".join(" --security-group " + shlex.quote(m) for m in missed), pid))
    return pid


# --- server group --------------------------------------------------------------------------------
def keep_sg_ensure(r, st):
    """Finds or creates r.ksg_dst at the destination, sets r.ksg_id, records it."""
    try:
        dlist = dst_server_groups_named(r, r.ksg_dst)
    except Die:
        raise Die("cannot list destination server groups")
    created = "no"
    if not dlist:
        rule_args = []
        for k, v in r.ksg_rules.items():
            if k:
                rule_args += ["--rule", "%s=%s" % (k, jqs(v))]
        err = st.p("server-group.err")
        # An older client, or a compute API version pinned below 2.64, refuses --rule before any API call.
        res = r.dst.run("server", "group", "create", "--policy", r.ksg_policy, *rule_args, r.ksg_dst,
                        "-f", "value", "-c", "id", stderr=err)
        if res.rc != 0:
            last = (open(err, errors="replace").read().splitlines() or [""])[-1]
            if not (rule_args and "--rule" in open(err, errors="replace").read()):
                raise Die("cannot create server group %s in the destination project: %s" % (r.ksg_dst, last))
            log("WARN openstackclient refused --rule (%s): %s is created without the source rules %s"
                % (last, r.ksg_dst, _compact(r.ksg_rules)))
            r.ksg_id = r.dst.value("server", "group", "create", "--policy", r.ksg_policy, r.ksg_dst,
                                   "-f", "value", "-c", "id")
        else:
            r.ksg_id = res.stdout.rstrip("\n")
        if not r.ksg_id:
            raise Die("server group create returned no ID for " + r.ksg_dst)
        created = "yes"
        log("created destination server group %s (%s, policy %s)" % (r.ksg_dst, r.ksg_id, r.ksg_policy))
    elif len(dlist) == 1:
        did, dpol = dlist[0]
        if dpol != r.ksg_policy:
            raise Die("server group %s has policy %s at the destination, the source group has %s"
                      % (r.ksg_dst, dpol, r.ksg_policy))
        r.ksg_id = did
        log("destination server group %s (%s) exists: the VM joins it" % (r.ksg_dst, r.ksg_id))
    else:
        raise Die("%d server groups are named %s in the destination project: drop --keep-server-group and pass "
                  "--server-group <id>" % (len(dlist), r.ksg_dst))
    st.write("dst-server-group", "%s created=%s\n" % (r.ksg_id, created))


def _compact(obj):
    import json
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


# --- destination server -------------------------------------------------------------------------
def create_destination(r, vm, st):
    o = r.opts
    server = st.json("server.json")
    # The argument may be a server ID (used to disambiguate duplicate names); the destination
    # VM, its ports and volumes carry the real name, as the preflight duplicate guard assumes.
    vmname = jqs(server.get("name"))
    ports = st.json("ports.json")
    if ports:
        r.dst.project_id()
    nics = []
    for i, p in enumerate(ports):
        net, ip, mac, cidr = jqs(p.get("network")), jqs(p.get("ip")), jqs(p.get("mac")), jqs(p.get("cidr"))
        subnets = r.dst.json("subnet", "list", "--network", net, "-f", "json")
        subnet = "\n".join(s.get("ID") for s in subnets if s.get("Subnet") == cidr)
        if o.ov_sg:
            sgs = ifs_split(o.ov_sg, ",")
        else:
            sgs = [s for s in (p.get("security_groups") or []) if s]
        # By ID: a name such as "default" exists once per project and an admin sees them all.
        sg_args = []
        for sg in sgs:
            sgid = r.dst.sg_id(sg)
            if not sgid:
                raise Die("security group %s not found in destination project %s" % (sg, r.cfg.DST_PROJECT or r.dst._pid))
            sg_args += ["--security-group", sgid]
        if not sg_args:
            sg_args = ["--no-security-group"]
        # Ports are named <vm>-<ip> but used and rolled back by ID: a name is not unique across
        # projects, and an admin's name lookup sees every project.
        if o.ipmode == "same":
            pname = "%s-%s" % (vmname, ip)
            pid = r.dst.value("port", "create", pname, "--network", net, "--mac-address", mac,
                              "--fixed-ip", "subnet=%s,ip-address=%s" % (subnet, ip), *sg_args, "-f", "value", "-c", "id")
        elif o.ipmode == "new":
            # The address is unknown until Neutron allocates it: renamed right below.
            pname = "%s-pending%d" % (vmname, i)
            pid = r.dst.value("port", "create", pname, "--network", net, "--fixed-ip", "subnet=" + subnet,
                              *sg_args, "-f", "value", "-c", "id")
        else:
            pname = "%s-%s" % (vmname, o.ipaddr)
            pid = r.dst.value("port", "create", pname, "--network", net,
                              "--fixed-ip", "subnet=%s,ip-address=%s" % (subnet, o.ipaddr), *sg_args,
                              "-f", "value", "-c", "id")
        st.append("dst-ports", pid)
        pj = r.dst.json("port", "show", pid, "-f", "json")
        fixed = pj.get("fixed_ips") or [{}]
        pinfo = "%s %s" % (jqs(fixed[0].get("ip_address")), jqs(pj.get("mac_address")))
        if o.ipmode == "new":
            pname = "%s-%s" % (vmname, pinfo.split(" ", 1)[0])
            r.dst.out("port", "set", "--name", pname, pid)
        log("destination port %s %s" % (pname, pinfo))
        nics += ["--port", pid]

    flavor = o.ov_flavor or flavor_name(server)
    key = jq_alt(server.get("key_name"))
    key = "" if key is None else jqs(key)
    # Nova cannot assign another user's keypair: create without it; the key is inside the disk anyway.
    if key and not r.dst.ok("keypair", "show", key):
        log("WARN keypair %s not at destination: the VM is created without key_name (add it back later)" % key)
        key = ""
    hint = []
    if o.server_group:
        groups = r.dst.json("server", "group", "list", "-f", "json")
        ids = [g.get("ID") for g in groups if g.get("Name") == o.server_group or g.get("ID") == o.server_group]
        if len(ids) != 1:
            raise Die("server group %s is missing or ambiguous in the destination project" % o.server_group)
        hint = ["--hint", "group=" + ids[0]]
    # Set by preflight only with --keep-server-group and a source VM that is in a group.
    if r.ksg_dst:
        keep_sg_ensure(r, st)
        hint = ["--hint", "group=" + r.ksg_id]

    # Server metadata travels with the VM: properties and description.
    prop_args = []
    props = jq_alt(server.get("properties"), server.get("metadata"), {})
    for k, v in (props.items() if isinstance(props, dict) else []):
        if k:
            prop_args += ["--property", "%s=%s" % (k, jqs(v))]
    desc = jq_alt(server.get("description"))
    if desc is not None and jqs(desc) != "":
        prop_args += ["--description", jqs(desc)]

    # Boot volume first, then data volumes in their original device order. The source user-data is
    # deliberately NOT reused: cloud-init sees a new instance-id and would re-run first-boot scripts
    # on real data. This minimal config only stops it from replacing SSH host keys and the hostname.
    ud = st.p("user-data.yaml")
    st.write("user-data.yaml", USER_DATA)
    args = []
    if r.ephemeral:
        args += ["--image", st.read("dst-image-id").rstrip("\n")]
    for a in st.json("attachments.json"):
        svid, device = a.get("Volume ID"), jqs(a.get("Device"))
        dvid = st.dst_volume_of(svid)
        if device in ("/dev/vda", "/dev/sda"):
            args += ["--volume", dvid]
        else:
            args += ["--block-device", "uuid=%s,source_type=volume,destination_type=volume,device_name=%s,boot_index=-1"
                     % (dvid, device[5:] if device.startswith("/dev/") else device)]

    cmd = ["server", "create", vmname, "--flavor", flavor, *nics, *args, "--user-data", ud]
    if o.dest_az:
        cmd += ["--availability-zone", o.dest_az]
    if key:
        cmd += ["--key-name", key]
    cmd += [*hint, *prop_args, "-f", "value", "-c", "id"]
    # bash redirected into the file, so it exists (empty) even when the create fails.
    st.write("dst-server-id", "")
    st.write("dst-server-id", r.dst.out(*cmd))
    r.wait_server(r.dst, st.read("dst-server-id").rstrip("\n"), "ACTIVE")
