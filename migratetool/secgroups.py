"""Copies of the source security groups at the destination (P6, the default; --no-copy-secgroup opts out).

Every group on a source port is '<AZ>-<source name>' in the destination project (azname.resolve):
  - one group of that name exists: used as is, its rules are not compared;
  - none: 'migrate' creates it (never 'plan'), with the source group's description and stateful flag,
    deletes the egress rules Neutron puts in every new group, then recreates each source rule: direction,
    ethertype, protocol, port range (ICMP type/code), remote CIDR, description;
  - a rule whose remote is another group copies that group too, recursively, as '<AZ>-<its name>'; a rule
    that points at its own group points at the copy itself. A visited set (source IDs) ends loops such as
    A -> B -> A. All copies exist before the first rule is written, so a rule can point at any of them.
Rollback keeps the groups, as it keeps the server group: other VMs may use them.
"""
import json

from . import proc
from .util import Die, lines, log

ICMP = ("icmp", "icmpv6", "ipv6-icmp", "1", "58")  # openstackclient's is_icmp_protocol()


# --- one rule -----------------------------------------------------------------------------------
def rule_args(rule, remote_group=None):
    """The 'openstack security group rule create <group>' options that recreate this Neutron rule.
    remote_group: the destination ID its remote group maps to (None: the rule has no remote group)."""
    eth = rule.get("ethertype") or "IPv4"
    a = ["--ingress" if rule.get("direction") == "ingress" else "--egress", "--ethertype", eth]
    proto = rule.get("protocol")
    if proto is not None and str(proto) != "":
        a += ["--protocol", str(proto)]
    lo, hi = rule.get("port_range_min"), rule.get("port_range_max")
    if proto is not None and str(proto).lower() in ICMP:
        # openstackclient drops --dst-port for ICMP: type and code have their own options.
        if lo is not None:
            a += ["--icmp-type", str(lo)]
        if hi is not None:
            a += ["--icmp-code", str(hi)]
    elif lo is not None or hi is not None:
        a += ["--dst-port", "%s:%s" % (lo if lo is not None else hi, hi if hi is not None else lo)]
    if remote_group:
        a += ["--remote-group", remote_group]
    elif rule.get("remote_ip_prefix"):
        a += ["--remote-ip", rule["remote_ip_prefix"]]
    # No remote at all: openstackclient sends 0.0.0.0/0 (::/0 for IPv6), which Neutron treats as the same.
    if rule.get("description"):
        a += ["--description", rule["description"]]
    return a


def rule_key(rule):
    """Rules Neutron would hold as one at the destination (an empty remote becomes 0.0.0.0/0 or ::/0)."""
    eth = rule.get("ethertype") or "IPv4"
    rip = rule.get("remote_ip_prefix") or ""
    rg = rule.get("remote_group_id") or ""
    if not rip and not rg:
        rip = "0.0.0.0/0" if eth == "IPv4" else "::/0"
    proto = rule.get("protocol")
    return (rule.get("direction"), eth, "" if proto is None else str(proto).lower(),
            rule.get("port_range_min"), rule.get("port_range_max"), rip, rg)


# --- the plan (preflight, read-only) ------------------------------------------------------------------
class Group:
    def __init__(self, sid, name, dst_name):
        self.sid, self.name, self.dst_name = sid, name, dst_name
        self.dst_id = ""         # found: its ID; to create: set by ensure()
        self.create = False
        self.rules = []          # the source rules to recreate (create only)
        self.description = ""
        self.stateful = True
        self.refs = []           # source IDs of the other groups its rules point at
        self.problem = ""


class Plan:
    def __init__(self, prefix):
        self.prefix = prefix
        self.groups = {}         # source ID -> Group, in discovery order
        self.top = {}            # group name on a source port -> source ID
        self.problem = {}        # group name on a source port -> why it cannot be copied

    def dst_id(self, name):
        return self.groups[self.top[name]].dst_id

    def closure(self, g):
        """The other groups g's copy needs, through its rules, recursively (discovery order)."""
        seen, todo, got = {g.sid}, list(g.refs), []
        while todo:
            sid = todo.pop(0)
            if sid in seen or sid not in self.groups:
                continue
            seen.add(sid)
            x = self.groups[sid]
            got.append(x)
            todo += x.refs
        return got


def _listing(cloud):
    """[(id, name)] of the groups of the token's project."""
    pid = cloud.project_id()
    res = cloud.run("security", "group", "list", "--project", pid, "-f", "json")
    if res.rc != 0:
        raise Die("cannot list the security groups of %s project %s" % (cloud.side, cloud.project or pid))
    return [(g.get("ID", g.get("id")), g.get("Name") if g.get("Name") is not None else g.get("name"))
            for g in json.loads(res.stdout or "[]")]


def plan_copy(r, names, prefix):
    """Read-only: which '<prefix>-<name>' exist at the destination, which would be created and with what."""
    plan = Plan(prefix)
    src_rows = _listing(r.src)
    dst_rows = _listing(r.dst)
    src_names = dict(src_rows)
    where = r.cfg.DST_PROJECT or r.dst.project_id()
    queue = []
    for name in names:
        if name in plan.top or name in plan.problem:
            continue
        ids = [i for i, n in src_rows if n == name]
        if len(ids) == 1:
            plan.top[name] = ids[0]
            queue.append((ids[0], name))
        elif ids:
            plan.problem[name] = ("%d security groups are named '%s' in source project %s (%s): the copy's name "
                                  "would be ambiguous — pass --security-group"
                                  % (len(ids), name, r.cfg.SRC_PROJECT or r.src.project_id(), " ".join(ids)))
        else:
            plan.problem[name] = "not found in source project %s" % (r.cfg.SRC_PROJECT or r.src.project_id())
    visited = set()
    while queue:
        sid, name = queue.pop(0)
        if sid in visited:
            continue
        visited.add(sid)
        g = Group(sid, name, "%s-%s" % (prefix, name))
        plan.groups[sid] = g
        hits = [i for i, n in dst_rows if n == g.dst_name]
        if len(hits) == 1:
            g.dst_id = hits[0]
            continue
        if hits:
            g.problem = ("%d security groups are named %s in destination project %s (%s) — keep one, or pass "
                         "--security-group" % (len(hits), g.dst_name, where, " ".join(hits)))
            continue
        g.create = True
        sj = r.src.json("security", "group", "show", sid, "-f", "json")
        g.description = sj.get("description") or ""
        g.stateful = sj.get("stateful") is not False
        keys = set()
        rules = sj.get("rules")
        if rules is None:
            rules = sj.get("security_group_rules") or []
        for rule in rules:
            if rule.get("remote_address_group_id"):
                g.problem = ("rule %s of %s has a remote address group, which is not copied — create %s at the "
                             "destination yourself, or pass --security-group" % (rule.get("id"), name, g.dst_name))
                break
            k = rule_key(rule)
            if k in keys:
                continue
            keys.add(k)
            g.rules.append(rule)
            rg = rule.get("remote_group_id")
            if rg and rg != sid:
                if rg not in g.refs:
                    g.refs.append(rg)
                if rg not in visited:
                    rname = src_names.get(rg)
                    if rname is None:  # a group of another project: an admin can still read it
                        rname = r.src.value("security", "group", "show", rg, "-f", "value", "-c", "name")
                    queue.append((rg, rname))
    return plan


def describe(plan, name):
    """(good, text) of the preflight line for a group on a source port."""
    if name in plan.problem:
        return False, "security group %s: %s" % (name, plan.problem[name])
    g = plan.groups[plan.top[name]]
    deps = plan.closure(g)
    for x in [g] + deps:
        if x.problem:
            return False, "security group %s -> %s%s: %s" % (name, x.dst_name, "" if x is g else " (referenced)",
                                                              x.problem)
    if not g.create:
        return True, "security group %s -> found %s (%s)" % (name, g.dst_name, g.dst_id)
    parts = ["%d rule%s" % (len(g.rules), "" if len(g.rules) == 1 else "s")]
    parts += ["+ %s referenced" % x.dst_name for x in deps if x.create]
    parts += ["%s referenced, found" % x.dst_name for x in deps if not x.create]
    return True, "security group %s -> will create %s (%s)" % (name, g.dst_name, ", ".join(parts))


# --- migrate: create what is missing ------------------------------------------------------------------
def ensure(r, plan, st):
    """Creates the missing copies, records every group used in dst-security-groups."""
    for g in plan.groups.values():
        if not g.create:
            log("destination security group %s (%s) exists: used as is, its rules are not compared"
                % (g.dst_name, g.dst_id))
    created = []
    try:
        for g in plan.groups.values():
            if not g.create:
                continue
            args = ["security", "group", "create", g.dst_name]
            if g.description:
                args += ["--description", g.description]
            if not g.stateful:
                args += ["--stateless"]
            g.dst_id = r.dst.value(*args, "-f", "value", "-c", "id")
            if not g.dst_id:
                raise Die("security group create returned no ID for " + g.dst_name)
            created.append(g)
            # Neutron puts egress-any rules in every new group: drop them, the copy holds the source's rules only.
            rids = lines(r.dst.out("security", "group", "rule", "list", g.dst_id, "-f", "value", "-c", "ID"))
            if rids:
                r.dst.out("security", "group", "rule", "delete", *rids)
        for g in created:
            for rule in g.rules:
                rg = rule.get("remote_group_id") or ""
                remote = plan.groups[rg].dst_id if rg else None
                r.dst.out("security", "group", "rule", "create", g.dst_id, *rule_args(rule, remote),
                          "-f", "value", "-c", "id")
            log("created destination security group %s (%s) from %s: %d rule%s"
                % (g.dst_name, g.dst_id, g.name, len(g.rules), "" if len(g.rules) == 1 else "s"))
    except BaseException:
        # No port uses them yet. Left behind half-copied, a re-run would find them and use them as they are.
        # 7.2 (C3): their rules first, all of them, then the groups: a rule of one copy pointing at another
        # copy makes Neutron refuse to delete that other group ("in use").
        for g in created:
            res = r.dst.run("security", "group", "rule", "list", g.dst_id, "-f", "value", "-c", "ID",
                            stderr=proc.DEVNULL)
            rids = lines(res.stdout) if res.rc == 0 else []
            if rids:
                r.dst.run("security", "group", "rule", "delete", *rids, stderr=proc.DEVNULL)
        gone = [g.dst_name for g in created
                if r.dst.run("security", "group", "delete", g.dst_id, stderr=proc.DEVNULL).rc == 0]
        if gone:
            log("WARN deleted the security groups this run had just created (%s): a re-run creates them again"
                % ", ".join(gone))
        raise
    st.write("dst-security-groups", "".join("%s %s created=%s\n" % (g.dst_id, g.dst_name, "yes" if g.create else "no")
                                            for g in plan.groups.values()))
