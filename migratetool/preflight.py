"""Destination preflight: read-only checks, one '  OK    ' / '  FAIL  ' / '  INFO  ' / '  WARN  ' line
each (bash preflight, keep_sg_preflight, flavor_fallback_preflight; P6: server group and security group
copies, port fidelity). Never creates anything."""
import ipaddress
import json
import os
import re
import shutil

from . import azname, config, proc, secgroups
from .context import flavor_name
from .util import Die, ifs_split, jq_alt, jqs, lines, out, prog, re_escape
from .vmdef import dest_volume_name

# A dns_name Nova accepts on a port it is given: Nova refuses one that is not the new instance's hostname
# (PortNotUsableDNS), and derives that hostname from the name. Copied only when it is plainly that name.
HOSTNAME_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")


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


def need_prefix(r, c):
    """The '<AZ>' that names the destination copies; '' after one FAIL line when it cannot be told."""
    if r.prefix is None:
        try:
            r.prefix, how = azname.resolve(r)
        except Die as e:
            r.prefix = ""
            c.failed(str(e))
        else:
            if how:
                out("  INFO  copies of the source server group and security groups are named %s-<source name>: %s"
                    % (r.prefix, how))
    return r.prefix


def server_group_preflight(r, st, c):
    """The source VM's server group, recreated at the destination as '<AZ>-<name>' with its policy (the
    default since 7.1; up to 7.0 only with --keep-server-group, named 'az-<name>'). A VM in no group adds
    no line."""
    sid = st.json("server.json").get("id")
    try:
        sgj = r.src.json("server", "group", "list", "--long", "-f", "json")
    except Die:
        return c.failed("cannot list source server groups")
    gids = [g.get("ID") for g in sgj if any(m == sid for m in (g.get("Members") or []))]
    if not gids:
        return
    if len(gids) > 1:
        return c.failed("source VM is in %d server groups (%s): pass --server-group <id>, or --no-server-group"
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
    prefix = need_prefix(r, c)
    if not prefix:
        return
    r.ksg_dst = "%s-%s" % (prefix, sname)
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
                     "anyway pass --server-group %s (or --no-server-group)" % (r.ksg_dst, dpol, sname, r.ksg_policy, did))
    else:
        c.failed("%d server groups are named %s in the destination project (ambiguous): %s— pass "
                 "--server-group <id>, or --no-server-group"
                 % (len(dlist), r.ksg_dst, "".join(i + " " for i, _ in dlist)))


# --- port fidelity (P6) ---------------------------------------------------------------------------
def net_extensions(r):
    """Aliases of the destination's network extensions (cached); None when they cannot be listed."""
    if r.net_ext is None:
        res = r.dst.run("extension", "list", "--network", "-f", "value", "-c", "Alias", stderr=proc.DEVNULL)
        r.net_ext = set(lines(res.stdout)) if res.rc == 0 else False
    return r.net_ext if r.net_ext is not False else None


def plain_hostname(dns_name, vmname):
    """dns_name is the hostname any Nova derives from vmname (lowercase letters, digits, inner '-')."""
    return dns_name == vmname.lower() and bool(HOSTNAME_RE.fullmatch(dns_name))


def pair_text(pairs):
    return ", ".join(jqs(x.get("ip_address")) + (" (%s)" % x["mac_address"] if x.get("mac_address") else "")
                     for x in pairs)


def port_security_conflict(r, ports):
    """--security-group on a VM with a port-security-disabled port: Neutron takes no group on that port."""
    o = r.opts
    for p in ports:
        if o.ov_sg and p.get("port_security") is False:
            raise Die("port %s has port security disabled at the source, so its copy takes no security group: "
                      "--security-group %s conflicts with it — drop --security-group, or enable port security on the "
                      "source port first" % (jqs(p.get("mac")), o.ov_sg))


def port_fidelity_preflight(r, p, i, vmname, c):
    """Port security, allowed-address-pairs and dns_name of one source port (the description is copied as is)."""
    mac = jqs(p.get("mac"))
    if p.get("port_security") is False:
        ext = net_extensions(r)
        if ext is None:
            c.failed("cannot list the destination network extensions (port %s has port security disabled)" % mac)
        elif "port-security" not in ext:
            c.failed("port %s has port security disabled at the source, the destination has no port-security "
                     "extension to create its copy that way" % mac)
        else:
            out("  INFO  port %s: port security disabled at the source: the destination port is created with "
                "--disable-port-security and no security group" % mac)
    pairs = [x for x in (p.get("aap") or []) if isinstance(x, dict)]
    if pairs:
        if r.opts.ipmode == "new":
            out("  WARN  port %s: allowed-address-pairs not copied with --ip new (they point at the old addresses; "
                "add what still applies yourself): %s" % (mac, pair_text(pairs)))
        else:
            ext = net_extensions(r)
            if ext is not None and "allowed-address-pairs" not in ext:
                c.failed("port %s has allowed-address-pairs, the destination has no allowed-address-pairs extension"
                         % mac)
            else:
                out("  INFO  port %s: allowed-address-pairs copied: %s" % (mac, pair_text(pairs)))
    dns = p.get("dns_name") or ""
    if dns:
        ext = net_extensions(r)
        if ext is None or "dns-integration" not in ext:
            out("  WARN  port %s: dns_name %s not copied: the destination %s" % (
                mac, dns, "network extensions cannot be listed" if ext is None else "has no dns-integration extension"))
        elif not plain_hostname(dns, vmname):
            out("  WARN  port %s: dns_name %s not copied: Nova refuses a port whose dns_name is not the new instance's "
                "hostname, and %s is not plainly the hostname of '%s' (Nova sets the port's dns_name itself)"
                % (mac, dns, dns, vmname))
        else:
            r.dns_copy.add(i)
            out("  INFO  port %s: dns_name %s copied" % (mac, dns))
    # 7.2 (A3): the floating IP stays with the source cloud; only the fixed address travels.
    fips = [jqs(a) for a in (p.get("floating_ips") or [])]
    if fips:
        out("  WARN  port %s: floating IP %s not migrated (a floating IP does not cross clouds): allocate one at the "
            "destination and update DNS" % (mac, ", ".join(fips)))


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


def dst_flavor(r, name, into):
    """'flavor show <name> -f json' at the destination: True when it exists, its fields copied into 'into'
    (the quota check reads vcpus and ram there). One call, as the plain existence check before 7.2."""
    res = r.dst.run("flavor", "show", name, "-f", "json", stderr=proc.DEVNULL)
    if res.rc != 0:
        return False
    try:
        got = json.loads(res.stdout or "{}")
    except ValueError:
        got = {}
    if isinstance(got, dict):
        into.update(got)
    return True


def flavor_fallback_preflight(r, st, c):
    """The flavor checks of --flavor-fallback; returns the destination flavor's fields (None: no flavor)."""
    o = r.opts
    if o.ov_flavor:
        out("  INFO  --flavor-fallback has no effect: --flavor %s is given" % o.ov_flavor)
        fj = {}
        c.check("flavor " + o.ov_flavor, lambda: dst_flavor(r, o.ov_flavor, fj))
        return fj
    server = st.json("server.json")
    srcf = flavor_name(server)
    # A same-name flavor at the destination wins: the map is a fallback, not an override.
    fj = {}
    if dst_flavor(r, srcf, fj):
        out("  OK    flavor " + srcf)
        return fj
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
    return dj if isinstance(dj, dict) else {}


# --- quota and capacity (7.2, A2) ------------------------------------------------------------------
# Everything one run consumes, checked on both clouds before anything is created. Resource names as
# 'openstack quota show --usage' prints them (openstackclient renames some Neutron ones: secgroups,
# secgroup-rules; Cinder's per-type ones are volumes_<type>, gigabytes_<type>); the first name of each entry
# is the one in our lines.
QUOTA_ALIASES = {
    "ports": ("ports", "port"),
    "security_groups": ("secgroups", "security_groups", "security-groups", "security_group"),
    "security_group_rules": ("secgroup-rules", "security_group_rules", "security-group-rules", "security_group_rule"),
}
# 'limits show --absolute' names of the per-group member limit (Nova API / openstacksdk spellings).
GROUP_MEMBER_LIMITS = ("maxServerGroupMembers", "max_server_group_members", "server_group_members")
GiB = 1073741824


def _whole(v):
    """A quota number as openstackclient prints it (int, or its text); None when it is not one."""
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _quota_table(cloud, side):
    """{resource: row} of 'quota show --usage -f json' (the token's project); None after a WARN line."""
    res = cloud.run("quota", "show", "--usage", "-f", "json", stderr=proc.CAPTURE)
    rows = None
    if res.rc == 0:
        try:
            rows = json.loads(res.stdout or "")
        except ValueError:
            rows = None
    if not isinstance(rows, list):
        why = (res.stderr.strip().splitlines() or [""])[-1] if res.rc != 0 else ""
        out("  WARN  %s quota not checked: %s"
            % (side, why or "'openstack quota show --usage -f json' printed no usage table"))
        return None
    return {str(row["Resource"]): row for row in rows if isinstance(row, dict) and row.get("Resource") is not None}


def _check_quota(c, side, table, needs, extra=()):
    """needs: [(resource, amount)]; extra: [(resource, need, free, used, limit)] read elsewhere. One FAIL line
    per resource short, else one OK line listing need/free. Missing resources and Limit -1 are skipped."""
    rows = []
    for res_name, n in needs:
        if n <= 0:
            continue
        row = next((table[a] for a in QUOTA_ALIASES.get(res_name, (res_name,)) if a in table), None)
        if row is None:
            continue
        limit, used, reserved = _whole(row.get("Limit")), _whole(row.get("In Use")), _whole(row.get("Reserved"))
        if limit is None or limit < 0:  # -1: unlimited
            continue
        used = used or 0
        rows.append((res_name, n, limit - used - (reserved or 0), used, limit))
    shown, short = [], False
    for res_name, n, free, used, limit in rows + list(extra):
        if free < n:
            c.failed("%s quota: %s need %d, %d free (used %d of %d)" % (side, res_name, n, max(free, 0), used, limit))
            short = True
            continue
        shown.append("%s %d/%d%s%s" % (res_name, n, free, " free" if not shown else "", " MB" if res_name == "ram" else ""))
    if short:
        return
    if shown:
        out("  OK    %s quota: %s" % (side, ", ".join(shown)))
    else:
        out("  OK    %s quota: no limit set on what this run uses" % side)


def _vol_type(v):
    t = jq_alt(v.get("type"), v.get("volume_type"))
    return "" if t is None else jqs(t)


def source_needs(vols):
    """The temporary clone at the source: one at a time (dropped before the next volume is cloned; one at a
    time in live mode too), so +1 volume and the largest size; per type too, the clone keeps its source's type."""
    if not vols:
        return []
    sizes = [_whole(v.get("size")) or 0 for v in vols]
    need = [("volumes", 1), ("gigabytes", max(sizes))]
    by_type = {}
    for v, size in zip(vols, sizes):
        t = _vol_type(v)
        if t:
            by_type[t] = max(by_type.get(t, 0), size)
    for t, size in by_type.items():
        need += [("volumes_" + t, 1), ("gigabytes_" + t, size)]
    return need


def quota_needs(r, ports, vols, flavor_json, server=True):
    """[(resource, amount)] this run creates at the destination, in the order of the preflight line."""
    need = []
    if server:
        need.append(("instances", 1))
        fj = flavor_json if isinstance(flavor_json, dict) else {}
        for res, key in (("cores", "vcpus"), ("ram", "ram")):
            n = _whole(fj.get(key))
            if n is not None:
                need.append((res, n))
    # The volumes copied (an ephemeral root disk is not one of them: it travels as an image).
    gb = sum(_whole(v.get("size")) or 0 for v in vols)
    need += [("volumes", len(vols)), ("gigabytes", gb)]
    if r.opts.volume_type:  # per-type quota of the type chosen (--volume-type, AZ_TYPE_MAP)
        need += [("volumes_" + r.opts.volume_type, len(vols)), ("gigabytes_" + r.opts.volume_type, gb)]
    if server:
        need.append(("ports", len(ports)))
        made = [g for g in r.sgplan.groups.values() if g.create] if r.sgplan is not None else []
        need.append(("security_groups", len(made)))
        need.append(("security_group_rules", sum(len(g.rules) for g in made)))
    return [(res, n) for res, n in need if n > 0]


def group_member_need(r, table):
    """(resource, need, free, used, limit) of the server group the VM joins; None: no group, or no limit."""
    group = r.opts.server_group or r.ksg_dst
    if not group:
        return None
    limit = None
    res = r.dst.run("limits", "show", "--absolute", "-f", "json", stderr=proc.DEVNULL)
    if res.rc == 0:
        try:
            rows = json.loads(res.stdout or "[]")
        except ValueError:
            rows = []
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and row.get("Name") in GROUP_MEMBER_LIMITS:
                limit = _whole(row.get("Value"))
                break
    if limit is None:  # the quota table carries the same per-group limit
        row = next((table[a] for a in ("server-group-members", "server_group_members") if a in table), None)
        limit = _whole(row.get("Limit")) if row else None
    if limit is None or limit < 0:
        return None
    used = 0  # a group this run creates starts empty
    res = r.dst.run("server", "group", "show", group, "-f", "json", "-c", "members", stderr=proc.DEVNULL)
    if res.rc == 0:
        try:
            members = json.loads(res.stdout or "{}").get("members")
        except (ValueError, AttributeError):
            members = None
        if isinstance(members, str):
            members = [m for m in re.split(r"[,\s]+", members) if m]
        used = len(members) if isinstance(members, list) else 0
    return ("server_group_members", 1, limit - used, used, limit)


def _pool_size(pools):
    """Addresses in a subnet's allocation_pools ([{start, end}], or openstackclient's 'start-end' text)."""
    if isinstance(pools, str):
        pools = [{"start": a.split("-", 1)[0], "end": a.split("-", 1)[1]}
                 for a in re.split(r"[,\s]+", pools) if "-" in a]
    total = 0
    for p in pools if isinstance(pools, list) else []:
        if not isinstance(p, dict):
            continue
        try:
            total += int(ipaddress.ip_address(p["end"])) - int(ipaddress.ip_address(p["start"])) + 1
        except (KeyError, ValueError):
            continue
    return total


def free_address_preflight(r, ports, c):
    """--ip new: Neutron picks each new port's address from the subnet's allocation pools, so one free address
    per port. Ports holding a fixed IP on the subnet count as used. A lookup that fails checks nothing."""
    want = {}
    for p in ports:
        key = (jqs(p.get("network")), jqs(p.get("cidr")))
        want[key] = want.get(key, 0) + 1
    shown = []
    for (net, cidr), n in want.items():
        res = r.dst.run("subnet", "list", "--network", net, "-f", "json", "-c", "ID", "-c", "Subnet",
                        stderr=proc.DEVNULL)
        try:
            sid = next(s.get("ID") for s in json.loads(res.stdout or "[]") if s.get("Subnet") == cidr)
        except (ValueError, StopIteration, AttributeError):
            continue
        res = r.dst.run("subnet", "show", sid, "-f", "json", stderr=proc.DEVNULL)
        if res.rc != 0:
            continue
        try:
            size = _pool_size(json.loads(res.stdout).get("allocation_pools"))
        except (ValueError, AttributeError):
            continue
        res = r.dst.run("port", "list", "--fixed-ip", "subnet=" + sid, "-f", "value", "-c", "ID", stderr=proc.DEVNULL)
        if res.rc != 0:
            continue
        free = size - len(lines(res.stdout))
        if free < n:
            c.failed("subnet %s: no free address in its allocation pools for --ip new" % cidr)
        else:
            shown.append("%s %d/%d" % (cidr, n, free))
    if shown:
        out("  OK    free addresses for --ip new: " + ", ".join(shown))


def ceph_capacity_preflight(r, vols, c):
    """The destination RBD pool must hold what is copied into it: 'ceph df detail' (MAX AVAIL, and the pool
    quota when one is set). FAIL when it does not fit, WARN when under 10% of the pool is left after the copy.
    The transfer user may lack the caps to ask: then an INFO line only."""
    need = sum(_whole(v.get("size")) or 0 for v in vols)
    if need <= 0:
        return
    res = proc.run(["ceph", "-c", r.cfg.CEPH_CONF, "--id", r.cfg.RBD_USER, "df", "detail", "-f", "json"],
                   env=r.env, capture=True, stderr=proc.CAPTURE)
    why, stats = "", None
    if res.rc != 0:
        why = (res.stderr.strip().splitlines() or ["ceph df exited %d" % res.rc])[-1]
    else:
        try:
            stats = next(p.get("stats") or {} for p in json.loads(res.stdout).get("pools") or []
                         if p.get("name") == r.pool)
        except (ValueError, AttributeError, StopIteration):
            why = "pool %s is not in the 'ceph df' output" % r.pool
    if stats is None:
        return out("  INFO  Ceph pool capacity not checked: %s" % why)
    avail = _whole(stats.get("max_avail"))
    if avail is None:
        return out("  INFO  Ceph pool capacity not checked: no max_avail for pool %s" % r.pool)
    stored = _whole(stats.get("stored"))
    stored = _whole(stats.get("bytes_used")) or 0 if stored is None else stored
    room, what = avail, "MAX AVAIL"
    quota = _whole(stats.get("quota_bytes")) or 0
    if quota > 0 and quota - stored < room:
        room, what = max(quota - stored, 0), "pool quota"
    need_b = need * GiB
    if room < need_b:
        return c.failed("Ceph pool %s: %d GB needed, %d GB left (%s)" % (r.pool, need, room // GiB, what))
    if (room - need_b) * 10 < stored + room:
        return out("  WARN  Ceph pool %s: %d GB needed, %d GB left (%s): under 10%% of the pool free after the copy"
                   % (r.pool, need, room // GiB, what))
    out("  OK    Ceph pool %s: %d GB needed, %d GB left (%s)" % (r.pool, need, room // GiB, what))


def quota_preflight(r, st, c, ports, vols, flavor_json, server=True, clone_vols=None):
    """Quota of both projects and the destination pool's capacity, for everything this run uses. vols: the
    volumes copied (one destination volume each); clone_vols: the source volumes cloned (default vols). A cloud
    that cannot answer 'quota show --usage' only gets a WARN: a cloud without that API must not block."""
    src_need = source_needs(vols if clone_vols is None else clone_vols)
    if src_need:
        table = _quota_table(r.src, "source")
        if table is not None:
            _check_quota(c, "source", table, src_need)
    table = _quota_table(r.dst, "destination")
    if table is not None:
        extra = []
        if server:
            g = group_member_need(r, table)
            if g:
                extra.append(g)
        _check_quota(c, "destination", table, quota_needs(r, ports, vols, flavor_json, server), extra)
    if server and r.opts.ipmode == "new" and ports:
        free_address_preflight(r, ports, c)
    ceph_capacity_preflight(r, vols, c)


def ephemeral_space_preflight(r, server, c):
    """7.2 (B2): the snapshot of an ephemeral root disk is downloaded into the state directory: room for up
    to the flavor's disk size there (the server's embedded flavor, else 'flavor show' at the source)."""
    f = server.get("flavor")
    disk = jq_alt(f.get("disk")) if isinstance(f, dict) else None
    if disk is None:
        res = r.src.run("flavor", "show", flavor_name(server), "-f", "json", stderr=proc.DEVNULL)
        if res.rc == 0:
            try:
                disk = json.loads(res.stdout).get("disk")
            except (ValueError, AttributeError):
                disk = None
    need = _whole(disk)
    try:
        free = shutil.disk_usage(config.STATE_ROOT).free // 1073741824
    except OSError as e:
        return out("  WARN  free space not checked: %s" % e)
    if not need:  # unknown, or 0 (the root disk is as large as the image)
        return out("  WARN  free space not checked: the root disk size of flavor %s is unknown (%s GB free in %s)"
                   % (flavor_name(server), free, config.STATE_ROOT))
    if free < need:
        return c.failed("free space in %s: %d GB, the root disk snapshot needs up to %d GB" % (config.STATE_ROOT, free, need))
    out("  OK    free space in %s: %d GB for a %d GB root disk" % (config.STATE_ROOT, free, need))


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
    extra = ((" flavor-fallback" if o.flavor_fallback else "")
             + (" no-server-group" if o.no_server_group else "") + (" no-copy-secgroup" if o.no_copy_sg else ""))
    out("Destination preflight (mode=%s%s ip=%s%s type=%s pool=%s%s%s%s%s%s%s):" % (
        o.mode, tail, o.ipmode, (" " + o.ipaddr) if o.ipaddr else "", o.volume_type or "default", r.pool,
        (" az=" + o.dest_az) if o.dest_az else "", (" volume-az=" + o.volume_az) if o.volume_az != o.dest_az else "", (" flavor=" + o.ov_flavor) if o.ov_flavor else "",
        (" sg=" + o.ov_sg) if o.ov_sg else "", (" server-group=" + o.server_group) if o.server_group else "",
        extra))
    flavor_json = None  # the destination flavor's fields, for the quota check
    for line in o.conf_origin_info() + o.default_opts_info():
        out(line)

    def az_listed(kind, zone):
        res = r.dst.run("availability", "zone", "list", kind, "-f", "value", "-c", "Zone Name", stderr=proc.DEVNULL)
        return zone in res.stdout.split("\n")

    if o.dest_az and not o.volumes_only:
        c.check("compute AZ " + o.dest_az, lambda: az_listed("--compute", o.dest_az))
    if o.volume_az:
        c.check("volume AZ " + o.volume_az, lambda: az_listed("--volume", o.volume_az))

    if o.volumes_only:
        # No server is created, so the guard against a second copy is on the destination volume names.
        # Volumes recorded in this VM's own state are not a clash (plan run after a migrate).
        if o.server_group:
            out("  INFO  --server-group %s ignored: --volumes-only creates no server" % o.server_group)
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
        port_security_conflict(r, st.json("ports.json"))
        # A same-named server already on the destination means an earlier migration of
        # this VM (finished, or interrupted after create). Proceeding would duplicate it.
        vmname = jqs(server.get("name"))
        dsid = lines(r.dst.out("server", "list", "--name", "^%s$" % re_escape(vmname), "-f", "value", "-c", "ID"))
        dsid = dsid[0] if dsid else ""
        if dsid and dsid != st.read("dst-server-id").rstrip("\n"):
            c.failed("destination already has a server named %s (%s) from an earlier migration — inspect it; to redo "
                     "the migration run: %s rollback %s (DELETES that destination server and its volumes)"
                     % (vmname, dsid, prog(), st.arg))
        if not o.flavor_fallback:
            flavor_json = {}
            c.check("flavor " + flavor, lambda: dst_flavor(r, flavor, flavor_json))
        else:
            flavor_json = flavor_fallback_preflight(r, st, c)
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
        # An explicit --server-group wins over the source group.
        if not o.server_group and not o.no_server_group:
            server_group_preflight(r, st, c)

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
    # P6: the source groups are copied as '<AZ>-<name>' unless --security-group (names as typed) or
    # --no-copy-secgroup (same-name groups that must already exist, as up to 7.0).
    copy_sg = not o.volumes_only and not o.ov_sg and not o.no_copy_sg
    if copy_sg:
        names = []
        for p in ports:
            names += [s for s in (p.get("security_groups") or []) if s and s not in names]
        if names and need_prefix(r, c):
            try:
                r.sgplan = secgroups.plan_copy(r, names, r.prefix)
            except Die as e:
                c.failed("cannot plan the security group copies: %s" % e)
    shown = {}
    vmname = jqs(server.get("name"))
    for i, p in enumerate(ports):
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
            # 7.2 (B4): Neutron answers a lookup of an address outside the network with an error, which the
            # check below reads as "free".
            if not in_subnet(o.ipaddr, cidr):
                c.failed("IP %s is not in subnet %s of %s" % (o.ipaddr, cidr, net))
            c.check("IP %s free" % o.ipaddr, lambda: r.dst.try_value("port", "list", "--network", net, "--fixed-ip",
                                                                    "ip-address=" + o.ipaddr, "-f", "value", "-c", "ID") == "")
        if jqs(p.get("extra_ips")) != "0":
            c.failed("port %s has several fixed IPs (not handled)" % mac)
        port_fidelity_preflight(r, p, i, vmname, c)
        if o.ov_sg:
            continue
        for sg in p.get("security_groups") or []:
            if not sg:
                continue
            if not copy_sg:
                check_sg("security group " + sg, sg)
            elif r.sgplan is not None:  # None: the FAIL line why is printed above
                if sg in shown:  # a group on several ports: one full line, its FAIL once
                    if shown[sg]:
                        out("  OK    security group %s -> %s-%s (see above)" % (sg, r.prefix, sg))
                    continue
                good, text = secgroups.describe(r.sgplan, sg)
                shown[sg] = good
                if good:
                    out("  OK    " + text)
                else:
                    c.failed(text)

    vols = st.json("volumes.json")
    c.check("no encrypted volume", lambda: all(v.get("encrypted") is not True for v in vols))
    c.check("no multiattach volume", lambda: all(v.get("multiattach") is not True for v in vols))
    if r.ephemeral:
        out("  INFO  root disk is ephemeral (image-backed): migrated through a Glance snapshot")
        if o.mode != "cold":
            c.failed("--mode live needs boot-from-volume (an ephemeral disk has nothing to clone)")
        ephemeral_space_preflight(r, server, c)
    quota_preflight(r, st, c, ports, vols, flavor_json, server=not o.volumes_only)
    c.check("RBD access to pool " + r.pool, lambda: r.rbd("-p", r.pool, "ls"))
    iscsid_preflight(r, c)
    if r.cfg.MIG_MULTIPATH == "1":
        c.check("multipathd on transfer host", lambda: systemctl_ok("is-active", "multipathd", r.env))
    if o.mode == "live":
        from .livesync import preflight_checks
        preflight_checks(r, c)
    return not c.fail


def in_subnet(addr, cidr):
    """addr inside cidr; False when either one does not parse."""
    try:
        return ipaddress.ip_address(addr) in ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return False


def iscsid_preflight(r, c):
    """7.2 (C7): read-only like every other check: no 'systemctl start' any more, the operator starts it."""
    good = systemctl_ok("is-active", "iscsid", r.env)
    c.check("iSCSI daemon on transfer host", lambda: good)
    if not good:
        c.info("start it: systemctl enable --now iscsid")


def systemctl_ok(action, unit, env):
    return proc.run(["systemctl", action, unit], env=env, capture=True, stderr=proc.DEVNULL).rc == 0
