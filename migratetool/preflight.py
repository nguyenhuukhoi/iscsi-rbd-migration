"""Destination preflight: read-only checks, one '  OK    ' / '  FAIL  ' / '  INFO  ' / '  WARN  ' line
each (bash preflight, keep_sg_preflight, flavor_fallback_preflight). Never creates anything."""
import json
import os

from . import proc
from .context import flavor_name
from .util import Die, ifs_split, jq_alt, jqs, lines, out, prog
from .vmdef import dest_volume_name


class Checker:
    def __init__(self):
        self.fail = False

    def check(self, label, fn):
        """bash check(): OK when fn() is true, FAIL otherwise or when it raises."""
        try:
            good = bool(fn())
        except (Die, OSError, ValueError, KeyError, TypeError):
            good = False
        out(("  OK    " if good else "  FAIL  ") + label)
        if not good:
            self.fail = True

    def failed(self, line):
        out("  FAIL  " + line)
        self.fail = True

    @staticmethod
    def info(line):
        out("  INFO  " + line)


def _policy_text(policy, joiner=None):
    """(.Policy // .Policies // "unknown") | if type=="array" then ... end"""
    if isinstance(policy, list):
        if joiner is not None:
            return joiner.join(jqs(p) for p in policy)
        return jqs(policy[0]) if policy else "unknown"
    return jqs(policy)


def dst_server_groups_named(r, name):
    """[(id, policy)] of the destination groups of that name; raises when the list fails."""
    groups = r.dst.json("server", "group", "list", "-f", "json", stderr=proc.INHERIT)
    return [(g.get("ID"), _policy_text(jq_alt(g.get("Policy"), g.get("Policies"), "unknown")))
            for g in groups if g.get("Name") == name]


def keep_sg_preflight(r, st, c):
    sid = st.json("server.json").get("id")
    try:
        sgj = r.src.json("server", "group", "list", "--long", "-f", "json")
    except Die:
        return c.failed("cannot list source server groups")
    gids = [g.get("ID") for g in sgj if any(m == sid for m in (g.get("Members") or []))]
    if not gids:
        return out("  INFO  source VM is in no server group")
    if len(gids) > 1:
        return c.failed("source VM is in %d server groups (%s): drop --keep-server-group and pass --server-group <id>"
                        % (len(gids), " ".join(gids)))
    gid = gids[0]
    try:
        gj = r.src.json("server", "group", "show", gid, "-f", "json")
    except Die:
        return c.failed("cannot read source server group %s" % gid)
    # Compute API < 2.64 has a "policies" list and no rules.
    sname = jqs(gj.get("name"))
    pol = jq_alt(gj.get("policy"), gj.get("policies"))
    if isinstance(pol, list):
        pol = jq_alt(pol[0] if pol else None)
    r.ksg_policy = "" if pol is None else jqs(pol)
    rules = jq_alt(gj.get("rules"), {})
    r.ksg_rules = rules if isinstance(rules, dict) else {}
    r.ksg_dst = "az-" + sname
    rtxt = ",".join("%s=%s" % (k, jqs(v)) for k, v in r.ksg_rules.items())
    out("  INFO  source server group %s (%s, policy %s%s) -> destination %s"
        % (sname, gid, r.ksg_policy or "unknown", (", rules " + rtxt) if rtxt else "", r.ksg_dst))
    if not r.ksg_policy:
        return c.failed("cannot read the policy of source server group %s (%s)" % (sname, gid))
    try:
        dlist = dst_server_groups_named(r, r.ksg_dst)
    except Die:
        return c.failed("cannot list destination server groups")
    if not dlist:
        out("  OK    will create %s (policy %s)" % (r.ksg_dst, r.ksg_policy))
    elif len(dlist) == 1:
        did, dpol = dlist[0]
        if dpol == r.ksg_policy:
            out("  OK    server group %s exists (%s, policy %s): reused" % (r.ksg_dst, did, r.ksg_policy))
        else:
            c.failed("server group %s has policy %s at the destination, source group %s has %s — to use it "
                     "anyway drop --keep-server-group and pass --server-group %s" % (r.ksg_dst, dpol, sname, r.ksg_policy, did))
    else:
        c.failed("%d server groups are named %s in the destination project (ambiguous): %s— drop "
                 "--keep-server-group and pass --server-group <id>"
                 % (len(dlist), r.ksg_dst, "".join(i + " " for i, _ in dlist)))


def load_flavor_map(r):
    """FLAVOR_MAP_FILE -> r.flavor_map; the flag was asked for, so a missing map is fatal."""
    path = r.cfg.FLAVOR_MAP_FILE
    if not path:
        raise Die("--flavor-fallback needs FLAVOR_MAP_FILE (lines 'source-flavor destination-flavor'), "
                  "set it in %s or the environment" % r.cfg.conf_path)
    if not (os.path.isfile(path) and os.access(path, os.R_OK)):
        raise Die("--flavor-fallback: FLAVOR_MAP_FILE %s is not a readable file" % path)
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    for line in text.split("\n"):
        fields = line.split("#", 1)[0].split()
        if not fields:
            continue
        if len(fields) != 2:
            raise Die("%s: expected 'source-flavor destination-flavor', got: %s" % (path, line))
        r.flavor_map[fields[0]] = fields[1]


def flavor_fallback_preflight(r, st, c):
    o = r.opts
    if o.ov_flavor:
        out("  INFO  --flavor-fallback has no effect: --flavor %s is given" % o.ov_flavor)
        c.check("flavor " + o.ov_flavor, lambda: r.dst.ok("flavor", "show", o.ov_flavor))
        return
    server = st.json("server.json")
    srcf = flavor_name(server)
    # A same-name flavor at the destination wins: the map is a fallback, not an override.
    if r.dst.ok("flavor", "show", srcf):
        return out("  OK    flavor " + srcf)
    mapped = r.flavor_map.get(srcf, "") if srcf else ""
    path = r.cfg.FLAVOR_MAP_FILE
    if not mapped:
        return c.failed("flavor %s — not at the destination and not in %s: add a line '%s <dest-flavor>' to %s"
                        % (srcf, path, srcf, path))
    out("  INFO  flavor %s -> %s from FLAVOR_MAP_FILE" % (srcf, mapped))
    rr = r.dst.run("flavor", "show", mapped, "-f", "json", stderr=proc.DEVNULL)
    if rr.rc != 0:
        return c.failed("flavor %s (mapped from %s in %s) not found at the destination — the tool never creates flavors"
                        % (mapped, srcf, path))
    dj = json.loads(rr.stdout)
    out("  OK    flavor " + mapped)
    o.ov_flavor = mapped
    # The source side comes from the server's own embedded flavor when present (exact, survives a
    # deleted source flavor); flavor show otherwise.
    f = server.get("flavor")
    sv = jq_alt(f.get("vcpus")) if isinstance(f, dict) else None
    sr = jq_alt(f.get("ram")) if isinstance(f, dict) else None
    if sv is None or sr is None:
        sres = r.src.run("flavor", "show", srcf, "-f", "json", stderr=proc.DEVNULL)
        if sres.rc == 0:
            sj = json.loads(sres.stdout)
            sv, sr = jq_alt(sj.get("vcpus")), jq_alt(sj.get("ram"))
    dv, dr = jq_alt(dj.get("vcpus")), jq_alt(dj.get("ram"))
    vals = ["" if v is None else jqs(v) for v in (sv, sr, dv, dr)]
    if not all(v.isdigit() for v in vals):
        out("  INFO  flavor sizes not compared: vcpus/ram of %s or %s unreadable" % (srcf, mapped))
    elif int(vals[2]) < int(vals[0]) or int(vals[3]) < int(vals[1]):
        out("  WARN  mapped flavor %s is smaller than %s (vcpus %s -> %s, ram %s -> %s MB)"
            % (mapped, srcf, vals[0], vals[2], vals[1], vals[3]))


def preflight(r, st):
    """True when every check passed (bash: preflight returns 0)."""
    o, cfg = r.opts, r.cfg
    c = Checker()
    # Here rather than in the migrate command so that plan reports it too.
    if o.volumes_only and r.ephemeral:
        raise Die("ephemeral VM has no volume to migrate: its root disk is image-backed and --volumes-only "
                  "copies Cinder volumes only")
    server = st.json("server.json")
    flavor = o.ov_flavor or flavor_name(server)
    key = jq_alt(server.get("key_name"))
    key = "" if key is None else jqs(key)
    tail = (" volumes-only" if o.volumes_only else "")
    extra = (" keep-server-group" if o.keep_sg else "") + (" flavor-fallback" if o.flavor_fallback else "")
    out("Destination preflight (mode=%s%s ip=%s%s type=%s pool=%s%s%s%s%s%s):" % (
        o.mode, tail, o.ipmode, (" " + o.ipaddr) if o.ipaddr else "", o.volume_type or "default", r.pool,
        (" az=" + o.dest_az) if o.dest_az else "", (" flavor=" + o.ov_flavor) if o.ov_flavor else "",
        (" sg=" + o.ov_sg) if o.ov_sg else "", (" server-group=" + o.server_group) if o.server_group else "",
        extra))
    for line in o.conf_origin_info() + o.default_opts_info():
        out(line)

    def az_listed(kind):
        res = r.dst.run("availability", "zone", "list", kind, "-f", "value", "-c", "Zone Name", stderr=proc.DEVNULL)
        return o.dest_az in res.stdout.split("\n")

    if o.dest_az:
        if not o.volumes_only:
            c.check("compute AZ " + o.dest_az, lambda: az_listed("--compute"))
        c.check("volume AZ " + o.dest_az, lambda: az_listed("--volume"))

    if o.volumes_only:
        # No server is created, so the guard against a second copy is on the destination volume names.
        # Volumes recorded in this VM's own state are not a clash (plan run after a migrate).
        if o.server_group:
            out("  INFO  --server-group %s ignored: --volumes-only creates no server" % o.server_group)
        if o.keep_sg:
            out("  INFO  --keep-server-group ignored: --volumes-only creates no server")
        if o.flavor_fallback:
            out("  INFO  --flavor-fallback ignored: --volumes-only creates no server")
        recorded = {d for _, d in st.volume_map()}
        for a in st.json("attachments.json"):
            dname = dest_volume_name(a["Volume ID"], st)
            res = r.dst.run("volume", "list", "--name", dname, "-f", "value", "-c", "ID")
            if res.rc != 0:
                c.failed("cannot list destination volumes named " + dname)
                continue
            clash = [d for d in res.stdout.split() if d not in recorded]
            if not clash:
                out("  OK    volume name %s free" % dname)
            else:
                c.failed("destination already has a volume named %s (%s) — rename or delete it first, the migrated "
                         "volume must be unambiguous" % (dname, " ".join(clash)))
    else:
        # A same-named server already on the destination means an earlier migration of
        # this VM (finished, or interrupted after create). Proceeding would duplicate it.
        vmname = jqs(server.get("name"))
        dsid = lines(r.dst.out("server", "list", "--name", "^%s$" % vmname, "-f", "value", "-c", "ID"))
        dsid = dsid[0] if dsid else ""
        if dsid and dsid != st.read("dst-server-id").rstrip("\n"):
            c.failed("destination already has a server named %s (%s) from an earlier migration — inspect it; to redo "
                     "the migration run: %s rollback %s (DELETES that destination server and its volumes)"
                     % (vmname, dsid, prog(), st.arg))
        if not o.flavor_fallback:
            c.check("flavor " + flavor, lambda: r.dst.ok("flavor", "show", flavor))
        else:
            flavor_fallback_preflight(r, st, c)
        # Nova cannot assign another user's keypair; the key itself is inside the disk anyway.
        if key:
            if r.dst.ok("keypair", "show", key):
                out("  OK    keypair " + key)
            else:
                out("  INFO  keypair %s not at destination: the VM will be created without key_name (add it back later)" % key)
        if o.server_group:
            res = r.dst.run("server", "group", "list", "-f", "json")
            if res.rc != 0:
                c.failed("cannot list destination server groups")
            else:
                hits = [g for g in json.loads(res.stdout)
                        if g.get("Name") == o.server_group or g.get("ID") == o.server_group]
                if len(hits) == 1:
                    out("  OK    server group " + o.server_group)
                    pols = [_policy_text(jq_alt(g.get("Policy"), g.get("Policies"), "unknown"), joiner=",")
                            for g in hits]
                    out("  INFO  server group %s policy: %s" % (o.server_group, "\n".join(pols)))
                elif not hits:
                    c.failed("server group %s not found in the destination project — create it first: openstack "
                             "server group create --policy anti-affinity %s" % (o.server_group, o.server_group))
                else:
                    c.failed("%d server groups are named %s in the destination project — pass the ID to --server-group"
                             % (len(hits), o.server_group))
        if o.keep_sg:
            keep_sg_preflight(r, st, c)

    if o.volume_type:
        c.check("volume type " + o.volume_type, lambda: r.dst.ok("volume", "type", "show", o.volume_type))

    def check_sg(label, name):
        """Exactly one group of that name in the destination project."""
        try:
            gid = r.dst.sg_id(name)
        except Die as e:
            return c.failed("%s: %s" % (label, e))
        if gid:
            out("  OK    %s (%s)" % (label, gid))
        else:
            c.failed("%s not found in destination project %s" % (label, r.cfg.DST_PROJECT or r.dst._pid))

    if not o.volumes_only:
        r.dst.project_id()
    if not o.volumes_only and o.ov_sg:
        for osg in ifs_split(o.ov_sg, ","):
            check_sg("override security group " + osg, osg)

    # --volumes-only creates no destination port: no network, address or security group check.
    ports = [] if o.volumes_only else st.json("ports.json")
    if not o.volumes_only and o.ipmode == "addr" and len(ports) != 1:
        c.failed("--ip <addr> works only for single-NIC VMs (this one has %d ports)" % len(ports))
    for p in ports:
        net, cidr, mac, ip = jqs(p.get("network")), jqs(p.get("cidr")), jqs(p.get("mac")), jqs(p.get("ip"))
        c.check("network %s exists" % net, lambda: r.dst.ok("network", "show", net))
        c.check("subnet %s on %s" % (cidr, net), lambda: cidr in r.dst.run(
            "subnet", "list", "--network", net, "-f", "value", "-c", "Subnet", stderr=proc.DEVNULL).stdout.split("\n"))
        # As in bash, a listing that fails reads as "nothing there" (the test only looks at stdout).
        if o.ipmode == "same":
            c.check("MAC %s free" % mac, lambda: r.dst.try_value("port", "list", "--mac-address", mac,
                                                                "-f", "value", "-c", "ID") == "")
            c.check("IP %s free" % ip, lambda: r.dst.try_value("port", "list", "--network", net, "--fixed-ip",
                                                              "ip-address=" + ip, "-f", "value", "-c", "ID") == "")
        elif o.ipmode == "addr":
            c.check("IP %s free" % o.ipaddr, lambda: r.dst.try_value("port", "list", "--network", net, "--fixed-ip",
                                                                    "ip-address=" + o.ipaddr, "-f", "value", "-c", "ID") == "")
        if jqs(p.get("extra_ips")) != "0":
            c.failed("port %s has several fixed IPs (not handled)" % mac)
        if len(p.get("aap") or []) != 0:
            c.failed("port %s has allowed-address-pairs (not handled)" % mac)
        if not o.ov_sg:
            for sg in p.get("security_groups") or []:
                if sg:
                    check_sg("security group " + sg, sg)

    vols = st.json("volumes.json")
    c.check("no encrypted volume", lambda: all(v.get("encrypted") is not True for v in vols))
    c.check("no multiattach volume", lambda: all(v.get("multiattach") is not True for v in vols))
    if r.ephemeral:
        out("  INFO  root disk is ephemeral (image-backed): migrated through a Glance snapshot")
        if o.mode != "cold":
            c.failed("--mode live needs boot-from-volume (an ephemeral disk has nothing to clone)")
    c.check("RBD access to pool " + r.pool, lambda: r.rbd("-p", r.pool, "ls"))
    c.check("iSCSI daemon on transfer host", lambda: systemctl_ok("is-active", "iscsid", r.env)
            or systemctl_ok("start", "iscsid", r.env))
    if r.cfg.MIG_MULTIPATH == "1":
        c.check("multipathd on transfer host", lambda: systemctl_ok("is-active", "multipathd", r.env))
    if o.mode == "live":
        from .livesync import preflight_checks
        preflight_checks(r, c)
    return not c.fail


def systemctl_ok(action, unit, env):
    return proc.run(["systemctl", action, unit], env=env, capture=True, stderr=proc.DEVNULL).rc == 0
