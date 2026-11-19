# V7-P6 scenarios, exec'd at the end of scenarios.py (shares its names: P, compare, base_world, sc, ...).
# Port fidelity, security group + server group copies named '<AZ>-<name>'. The app runs on the P6 DEFAULTS
# here (no legacy opt-outs). Mostly app-only with explicit checks: the bash build refuses pairs and has no
# copy at all. The 'p6-regress' sweep compares app vs bash on plain VMs: identical except the group names.
import copy as _copy
import difflib as _difflib
import json as _json
import re as _re


def p6w(azs=("nova",)):
    """base_world with ONE destination compute AZ ('nova'): the copies are named nova-<name>."""
    W = base_world()
    W["dst"]["azs"]["compute"] = list(azs)
    return W


def R(rid, direction, eth, proto=None, lo=None, hi=None, rip=None, rg=None, desc="", rag=None):
    r = {"id": rid, "direction": direction, "ethertype": eth, "protocol": proto, "port_range_min": lo,
         "port_range_max": hi, "remote_ip_prefix": rip, "remote_group_id": rg, "description": desc}
    if rag:
        r["remote_address_group_id"] = rag
    return r


def neutron_default_rules(gid, pfx):
    """What Neutron puts in a project's 'default' group: ingress from itself, egress anywhere."""
    return [R(pfx + "1", "ingress", "IPv4", rg=gid), R(pfx + "2", "ingress", "IPv6", rg=gid),
            R(pfx + "3", "egress", "IPv4"), R(pfx + "4", "egress", "IPv6")]


WEB_RULES = [R("rw1", "ingress", "IPv4", "tcp", 22, 22, rip="0.0.0.0/0", desc="ssh"),
             R("rw2", "ingress", "IPv4", "tcp", 8000, 8080, rip="10.0.0.0/8"),
             R("rw3", "ingress", "IPv4", "icmp", 8, 0),
             R("rw4", "ingress", "IPv4", "112", rg="sg-src-web", desc="vrrp"),
             R("rw5", "egress", "IPv4"), R("rw6", "egress", "IPv6")]


def sg_world(azs=("nova",)):
    W = p6w(azs)
    s = W["src"]["security_groups"]
    s["sg-src-web"].update(description="web tier", rules=_copy.deepcopy(WEB_RULES))
    s["sg-src-default"].update(rules=neutron_default_rules("sg-src-default", "rd"))
    return W


def in_group(W, policy="anti-affinity", rules=None):
    W["src"]["server_groups"]["sg-src-anti"] = {"name": "web-anti", "policy": policy, "rules": rules or {},
                                                "members": ["srv-src-app01"]}
    return W


def app_only(name, steps, world, check, impl="py", env=None, conf=None, pre_state=None, show=False):
    """The app alone on the P6 defaults; check(r) -> problems (r also carries the initial world)."""
    r = P.run(impl, _copy.deepcopy(world), steps, env, conf, pre_state)
    r["world0"] = world
    try:
        bad = check(r)
    except Exception:
        import traceback
        bad = ["check raised: " + traceback.format_exc()]
    text = r["out"] + r["err"]
    if bad:
        P.FAIL += 1
        print("FAIL %s\n  %s\n%s" % (name, "\n  ".join(bad), "\n".join("      | " + l for l in text.splitlines()[-70:])))
    else:
        P.PASS += 1
        print("PASS %s (rc=%s)" % (name, r["rc"]))
        if show:
            print("\n".join("      | " + l for l in r["out"].splitlines()[-70:]))
    return r


# --- helpers for the checks ---------------------------------------------------------------------------
# What copying a group does (the export reads names with "show <id> -f value -c name", not a copy).
COPY_CALLS = r"\tsecurity group (create|rule|show \S+ -f json)"


def calls_of(r, rx):
    return [l for l in r["calls"] if _re.search(rx, l)]


def group_id(world, side, name):
    hits = [k for k, g in world[side]["security_groups"].items() if g["name"] == name]
    return hits[0] if len(hits) == 1 else None


def norm_rules(cloud, gid, strip=""):
    """A group's rules as Neutron means them: empty remote = 0.0.0.0/0 (::/0), remote group by name."""
    names = {k: v["name"] for k, v in cloud["security_groups"].items()}
    got = []
    for x in cloud["security_groups"][gid].get("rules", []):
        rg = x.get("remote_group_id")
        rip = x.get("remote_ip_prefix") or ("" if rg else ("0.0.0.0/0" if x["ethertype"] == "IPv4" else "::/0"))
        rgn = names.get(rg, "?" + str(rg)) if rg else ""
        if strip and rgn.startswith(strip):
            rgn = rgn[len(strip):]
        got.append((x["direction"], x["ethertype"], None if x.get("protocol") is None else str(x["protocol"]),
                    x.get("port_range_min"), x.get("port_range_max"), rip, rgn, x.get("description") or ""))
    return sorted(got, key=str)


def dst_port(r, i=0):
    """The destination port created i-th (by creation order of the port-dst-NNNN IDs)."""
    ports = sorted(k for k in r["world"]["dst"]["ports"] if _re.fullmatch(r"port-dst-\d{4}", k))
    return r["world"]["dst"]["ports"][ports[i]] if len(ports) > i else None


def src_clean(r):
    """Source side back as it was (bookkeeping keys of the stub aside)."""
    a = {k: v for k, v in r["world0"]["src"].items() if k not in ("images", "projects")}
    b = {k: v for k, v in r["world"]["src"].items() if k not in ("images", "projects")}
    return [] if a == b else ["source side not restored: %s" % sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))]


def dst_empty_but_groups(r):
    """No server, volume or port the run created is left at the destination (groups may stay)."""
    d, d0 = r["world"]["dst"], r["world0"]["dst"]
    left = {k: [x for x in d[k] if x not in d0[k]] for k in ("servers", "volumes", "ports")}
    left = {k: v for k, v in left.items() if v}
    return ["destination not clean: %s" % left] if left else []


def want(r, *texts):
    return ["missing: %r" % t for t in texts if t not in r["out"] + r["err"]]


def unwanted(r, *texts):
    return ["unexpected: %r" % t for t in texts if t in r["out"] + r["err"]]


# ================= security group copies =================================================================
if sc("p6-sg"):
    def chk_create(r):
        bad = [] if r["rc"] == [0, 0] else ["rc %s" % r["rc"]]
        d, s = r["world"]["dst"], r["world0"]["src"]
        web, dflt = group_id(r["world"], "dst", "nova-web"), group_id(r["world"], "dst", "nova-default")
        if not (web and dflt):
            return bad + ["copies missing: %s" % [g["name"] for g in d["security_groups"].values()]]
        for mine, src in ((web, "sg-src-web"), (dflt, "sg-src-default")):
            if norm_rules(d, mine, "nova-") != norm_rules(s, src):
                bad.append("rules of %s differ:\n    dst %s\n    src %s" % (d["security_groups"][mine]["name"],
                                                                         norm_rules(d, mine, "nova-"), norm_rules(s, src)))
        if d["security_groups"][web].get("description") != "web tier":
            bad.append("group description not copied")
        if len(calls_of(r, r"^dst\tsecurity group rule delete rule-dst-\d{4} rule-dst-\d{4}$")) != 2:
            bad.append("Neutron's two egress rules not deleted once per created group")
        stop = next(i for i, l in enumerate(r["calls"]) if l.startswith("src\tserver stop"))
        if max(i for i, l in enumerate(r["calls"]) if "\tsecurity group rule create" in l) > stop:
            bad.append("groups copied after the source was stopped (inside the downtime)")
        bad += want(r, "  OK    security group default -> will create nova-default (4 rules)",
                    "  OK    security group web -> will create nova-web (6 rules)",
                    "  INFO  copies of the source server group and security groups are named nova-<source name>: nova is "
                    "the only compute availability zone of the destination (no --az, no MIG_AZ)",
                    "created destination security group nova-web (", "INFO destination security groups kept "
                    "(nova-default, nova-web): rollback never deletes a security group")
        st = r["files"].get("app01.rolled-back.<epoch>/dst-security-groups", "")
        if [l.split()[1:] for l in st.splitlines()] != [["nova-default", "created=yes"], ["nova-web", "created=yes"]]:
            bad.append("dst-security-groups state: %r" % st)
        return bad + dst_empty_but_groups(r) + src_clean(r)
    app_only("P6 SG copy: nova-default/nova-web created with every source rule (Neutron's egress rules deleted "
             "first, self-reference kept), before the stop; rollback keeps them",
             [["migrate", "app01", "--verify", "none"], ["rollback", "app01"]], sg_world(), chk_create, show=True)

    def port_groups(r, i=0):
        p = dst_port(r, i)
        names = {k: g["name"] for k, g in r["world"]["dst"]["security_groups"].items()}
        return [names.get(g, g) for g in (p or {}).get("security_group_ids", [])]

    def loop_world():
        W = p6w()
        s = W["src"]["security_groups"]
        s["sg-src-app"] = {"name": "app", "project_id": "psrc", "description": "",
                           "rules": [R("ra1", "ingress", "IPv4", "tcp", 5432, 5432, rg="sg-src-db"),
                                     R("ra2", "ingress", "IPv4", rg="sg-src-app"),
                                     R("ra3", "ingress", "IPv4", "tcp", 6379, 6379, rg="sg-src-cache"),
                                     R("ra4", "egress", "IPv4")]}
        s["sg-src-db"] = {"name": "db", "project_id": "psrc",
                          "rules": [R("rb1", "ingress", "IPv4", "tcp", 80, 80, rg="sg-src-app"),
                                    R("rb2", "ingress", "IPv6", "tcp", 443, 443, rg="sg-src-db")]}
        s["sg-src-cache"] = {"name": "cache", "project_id": "psrc", "rules": [R("rc1", "egress", "IPv4")]}
        W["dst"]["security_groups"]["sg-dst-ncache"] = {"name": "nova-cache", "project_id": "pdst",
                                                        "rules": [R("xc1", "ingress", "IPv4", "tcp", 1, 1)]}
        W["src"]["ports"]["port-src-1"]["security_group_ids"] = ["sg-src-app"]
        return W

    def chk_loop(r):
        bad = [] if r["rc"] == [0] else ["rc %s" % r["rc"]]
        w_, s = r["world"], r["world0"]["src"]
        d = w_["dst"]
        app, db = group_id(w_, "dst", "nova-app"), group_id(w_, "dst", "nova-db")
        if not (app and db):
            return bad + ["nova-app/nova-db missing"]
        for mine, src in ((app, "sg-src-app"), (db, "sg-src-db")):
            if norm_rules(d, mine, "nova-") != norm_rules(s, src):
                bad.append("rules of %s:\n    %s\n    %s" % (src, norm_rules(d, mine, "nova-"), norm_rules(s, src)))
        if d["security_groups"]["sg-dst-ncache"]["rules"] != r["world0"]["dst"]["security_groups"]["sg-dst-ncache"]["rules"]:
            bad.append("existing nova-cache was modified")
        if calls_of(r, r"^src\tsecurity group show sg-src-cache"):
            bad.append("found group nova-cache was read at the source (should not be walked)")
        if len(calls_of(r, r"^dst\tsecurity group create")) != 2:
            bad.append("expected exactly 2 creates (app, db): %s" % calls_of(r, r"group create"))
        if port_groups(r) != ["nova-app"]:
            bad.append("port groups %s" % port_groups(r))
        return bad + want(r, "  OK    security group app -> will create nova-app (4 rules, + nova-db referenced, "
                             "nova-cache referenced, found)")
    app_only("P6 SG copy: remote groups copied recursively (app -> db -> app loop, self-reference, a referenced "
             "copy that exists is used as is)", [["migrate", "app01", "--verify", "none"]], loop_world(), chk_loop,
             show=True)

    W = sg_world()
    W["dst"]["security_groups"]["sg-dst-nweb"] = {"name": "nova-web", "project_id": "pdst",
                                                  "rules": [R("xw1", "ingress", "IPv4", "tcp", 443, 443)]}
    W["dst"]["security_groups"]["sg-dst-ndef"] = {"name": "nova-default", "project_id": "pdst", "rules": []}

    def chk_reuse(r):
        bad = [] if r["rc"] == [0] else ["rc %s" % r["rc"]]
        if calls_of(r, COPY_CALLS):
            bad.append("found groups must not be created, read or changed: %s" % calls_of(r, COPY_CALLS))
        if r["world"]["dst"]["security_groups"] != r["world0"]["dst"]["security_groups"]:
            bad.append("destination groups changed")
        if port_groups(r) != ["nova-default", "nova-web"]:
            bad.append("port groups %s" % port_groups(r))
        return bad + want(r, "  OK    security group web -> found nova-web (sg-dst-nweb)",
                          "destination security group nova-web (sg-dst-nweb) exists: used as is, its rules are not compared")
    app_only("P6 SG copy: <AZ>-<name> already there -> used as is, rules not compared", [["migrate", "app01"]], W,
             chk_reuse)

    W = sg_world()
    del W["dst"]["security_groups"]["sg-dst-web"]

    def chk_nocopy(r):
        bad = [] if r["rc"] == [1] else ["rc %s" % r["rc"]]
        if calls_of(r, COPY_CALLS) or calls_of(r, r"availability zone list --compute -f json"):
            bad.append("--no-copy-secgroup must not plan copies")
        return bad + want(r, "  OK    security group default (sg-dst-default)",
                          "  FAIL  security group web not found in destination project pdst",
                          "no-copy-secgroup):")
    app_only("P6 --no-copy-secgroup: same-name destination groups as up to 7.0 (web missing -> FAIL)",
             ["plan", "app01", "--no-copy-secgroup"], W, chk_nocopy)

    W = sg_world(azs=("nova", "AZ1"))
    W["dst"]["security_groups"]["sg-dst-nweb"] = {"name": "nova-web", "project_id": "pdst", "rules": []}

    def chk_override(r):
        bad = [] if r["rc"] == [0] else ["rc %s" % r["rc"]]
        if calls_of(r, COPY_CALLS) or calls_of(r, r"availability zone list --compute -f json"):
            bad.append("--security-group must not plan copies")
        if port_groups(r) != ["web"]:
            bad.append("port groups %s (want the typed name, no prefix)" % port_groups(r))
        return bad + want(r, "  OK    override security group web (sg-dst-web)")
    app_only("P6 --security-group web: taken verbatim (no prefix, nova-web ignored, no AZ needed)",
             [["migrate", "app01", "--security-group", "web"]], W, chk_override)

    W = sg_world()
    W["src"]["security_groups"]["sg-src-web"]["rules"].append(R("rw9", "ingress", "IPv4", "tcp", 9, 9, rag="ag-1"))
    app_only("P6 SG copy: a rule with a remote address group -> FAIL, nothing created",
             ["plan", "app01"], W, lambda r: ([] if r["rc"] == [1] else ["rc %s" % r["rc"]]) + want(
                 r, "  FAIL  security group web -> nova-web: rule rw9 of web has a remote address group, which is "
                    "not copied — create nova-web at the destination yourself, or pass --security-group"))

    W = sg_world()
    W["dst"]["security_groups"]["sg-dst-n1"] = {"name": "nova-web", "project_id": "pdst"}
    W["dst"]["security_groups"]["sg-dst-n2"] = {"name": "nova-web", "project_id": "pdst"}
    app_only("P6 SG copy: two nova-web at the destination -> FAIL (ambiguous)", ["plan", "app01"], W,
             lambda r: ([] if r["rc"] == [1] else ["rc %s" % r["rc"]]) + want(
                 r, "  FAIL  security group web -> nova-web: 2 security groups are named nova-web in destination "
                    "project pdst (sg-dst-n1 sg-dst-n2) — keep one, or pass --security-group"))

    W = sg_world()
    W["dst"]["rule_fail"] = "112"

    def chk_rulefail(r):
        bad = [] if r["rc"] == [1] else ["rc %s" % r["rc"]]
        names = [g["name"] for g in r["world"]["dst"]["security_groups"].values()]
        if any(n.startswith("nova-") for n in names):
            bad.append("half-copied groups left: %s" % names)
        if "app01/volume-map" in r["files"]:
            bad.append("volume-map written: a re-run would be refused")
        srv = r["world"]["src"]["servers"]["srv-src-app01"]
        if srv["status"] != "ACTIVE" or srv["locked"]:
            bad.append("source touched: %s locked=%s" % (srv["status"], srv["locked"]))
        return bad + want(r, "WARN deleted the security groups this run had just created (nova-default, nova-web): "
                             "a re-run creates them again", "ERROR: openstack security group rule create")
    app_only("P6 SG copy: a rule create fails -> the groups just created are deleted, source never stopped, no "
             "volume-map", [["migrate", "app01"]], W, chk_rulefail)

    W = multinic_world()
    W["dst"]["azs"]["compute"] = ["nova"]

    def chk_two_ports(r):
        bad = [] if r["rc"] == [0] else ["rc %s" % r["rc"]]
        if len(calls_of(r, r"^dst\tsecurity group create nova-web")) != 1:
            bad.append("nova-web created %d times" % len(calls_of(r, r"^dst\tsecurity group create nova-web")))
        if port_groups(r, 0) != ["nova-default", "nova-web"] or port_groups(r, 1) != ["nova-web"]:
            bad.append("port groups %s %s" % (port_groups(r, 0), port_groups(r, 1)))
        return bad + want(r, "  OK    security group web -> nova-web (see above)")
    app_only("P6 SG copy: one group on two ports -> one copy, one full preflight line", [["migrate", "app01"]], W,
             chk_two_ports)

# ================= server group copy (default) ===========================================================
if sc("p6-servergroup"):
    def chk_sgroup(r):
        bad = [] if r["rc"] == [0, 0] else ["rc %s" % r["rc"]]
        g = [x for x in r["world"]["dst"]["server_groups"].values() if x["name"] == "nova-web-anti"]
        if len(g) != 1 or g[0]["policy"] != "anti-affinity":
            return bad + ["nova-web-anti: %s" % r["world"]["dst"]["server_groups"]]
        if not calls_of(r, r"^dst\tserver create .*--hint group=sg-dst-\d{4}"):
            bad.append("server not created in the group")
        return bad + want(r, "  INFO  source server group web-anti (sg-src-anti, policy anti-affinity) -> destination "
                             "nova-web-anti", "  OK    will create nova-web-anti (policy anti-affinity)",
                          "created destination server group nova-web-anti",
                          "INFO destination server group kept") + unwanted(r, "keep-server-group")
    app_only("P6 server group by default: nova-web-anti created + joined without any flag; rollback keeps it",
             [["migrate", "app01", "--verify", "none"], ["rollback", "app01"]], in_group(p6w()), chk_sgroup, show=True)

    W = in_group(p6w())
    W["dst"]["server_groups"]["g1"] = {"name": "nova-web-anti", "policy": "affinity", "members": []}
    app_only("P6 server group: <AZ>-<name> with another policy -> FAIL", ["plan", "app01"], W,
             lambda r: ([] if r["rc"] == [1] else ["rc"]) + want(
                 r, "  FAIL  server group nova-web-anti has policy affinity at the destination, source group web-anti "
                    "has anti-affinity — to use it anyway pass --server-group g1 (or --no-server-group)"))

    W = in_group(p6w())
    W["dst"]["server_groups"]["g1"] = {"name": "nova-web-anti", "policy": "anti-affinity", "members": []}
    app_only("P6 server group: <AZ>-<name> exists with the policy -> reused",
             [["migrate", "app01", "--verify", "none"]], W,
             lambda r: ([] if r["rc"] == [0] else ["rc"]) + want(
                 r, "  OK    server group nova-web-anti exists (g1, policy anti-affinity): reused") +
             ([] if r["world"]["dst"]["server_groups"]["g1"]["members"] else ["not a member of g1"]))

    def chk_nogroup(r):
        bad = [] if r["rc"] == [0] else ["rc %s" % r["rc"]]
        if calls_of(r, r"\tserver group"):
            bad.append("--no-server-group still looked at server groups: %s" % calls_of(r, r"\tserver group"))
        if calls_of(r, r"--hint"):
            bad.append("--hint given")
        return bad + want(r, "no-server-group):")
    app_only("P6 --no-server-group: the source group is not even looked up", [["migrate", "app01", "--no-server-group"]],
             in_group(p6w()), chk_nogroup)

    app_only("P6 server group + --az AZ1 (2 compute AZs): named AZ1-web-anti",
             ["plan", "app01", "--az", "AZ1"], in_group(sg_world(azs=("nova", "AZ1"))),
             lambda r: ([] if r["rc"] == [0] else ["rc %s" % r["rc"]]) + want(
                 r, "-> destination AZ1-web-anti", "  OK    will create AZ1-web-anti (policy anti-affinity)",
                 "  OK    security group web -> will create AZ1-web (6 rules)") +
             unwanted(r, "copies of the source server group and security groups are named"))
    app_only("P6 server group + MIG_AZ=AZ1: named AZ1-web-anti", ["plan", "app01"],
             in_group(sg_world(azs=("nova", "AZ1"))),
             lambda r: ([] if r["rc"] == [0] else ["rc %s" % r["rc"]]) + want(
                 r, "  INFO  availability zone AZ1 from MIG_AZ", "-> destination AZ1-web-anti"),
             env={"MIG_AZ": "AZ1"})

    W = in_group(p6w())
    W["dst"]["server_groups"]["g9"] = {"name": "db-anti", "policy": "anti-affinity", "members": []}
    app_only("P6 --server-group db-anti wins over the source group (not looked up)",
             [["migrate", "app01", "--server-group", "db-anti"]], W,
             lambda r: ([] if r["rc"] == [0] else ["rc"]) + (["source groups listed"] if calls_of(r, r"^src\tserver group") else [])
             + ([] if calls_of(r, r"--hint group=g9") else ["not in db-anti"]))

    # --keep-server-group is gone (user 19/11): an unknown option like any other, before any cloud call.
    def chk_unknown(msg):
        return lambda r: ([] if r["rc"] == [1] else ["rc %s" % r["rc"]]) + want(r, msg) + (
            ["cloud called: %s" % r["calls"][:3]] if r["calls"] else [])
    app_only("P6 --keep-server-group removed: 'migrate migrate app01 --keep-server-group --yes' -> unknown option",
             ["migrate", "app01", "--keep-server-group", "--yes"], in_group(p6w()),
             chk_unknown("ERROR: unknown option --keep-server-group"), impl="app")
    app_only("P6 --keep-server-group removed: 'migrate plan --vm app01 --keep-server-group' -> unknown option",
             ["plan", "--vm", "app01", "--keep-server-group"], in_group(p6w()),
             chk_unknown("ERROR: unknown option --keep-server-group"), impl="app")
    app_only("P6 --keep-server-group removed: per-VM engine (selector child) -> unknown option",
             ["plan", "app01", "--keep-server-group"], in_group(p6w()),
             chk_unknown("ERROR: unknown option --keep-server-group"))
    app_only("P6 --keep-server-group removed: 'migrate volume <vol> --keep-server-group' -> unknown option",
             ["volume", L1, "--keep-server-group"], p4_world(),
             chk_unknown("ERROR: unknown option --keep-server-group"), impl="app")
    app_only("P6 --keep-server-group removed: in MIG_DEFAULT_OPTS -> dies naming MIG_DEFAULT_OPTS (migrate)",
             ["migrate", "app01", "--yes"], in_group(p6w()),
             chk_unknown("ERROR: MIG_DEFAULT_OPTS: unknown option '--keep-server-group' "
                         "(MIG_DEFAULT_OPTS=\"--keep-server-group --verify none\")"),
             impl="app", env={"MIG_DEFAULT_OPTS": "--keep-server-group --verify none"})
    app_only("P6 --keep-server-group removed: in MIG_DEFAULT_OPTS (conf file) -> dies naming MIG_DEFAULT_OPTS (plan)",
             ["plan", "app01"], in_group(p6w()),
             chk_unknown("ERROR: MIG_DEFAULT_OPTS: unknown option '--keep-server-group' "
                         "(MIG_DEFAULT_OPTS=\"--keep-server-group\")"),
             impl="app", conf='MIG_DEFAULT_OPTS="--keep-server-group"\n')

# ================= the AZ of the names ===================================================================
if sc("p6-az"):
    W = in_group(sg_world(azs=("nova", "AZ1")))
    app_only("P6 AZ: no --az/MIG_AZ and 2 compute AZs -> one FAIL naming the way out", ["plan", "app01"], W,
             lambda r: ([] if r["rc"] == [1] else ["rc"]) + want(
                 r, "  FAIL  cannot name the destination copies of the source server group and security groups: no "
                    "--az, no MIG_AZ, and the destination has 2 compute availability zones (nova AZ1) — pass --az "
                    "<zone> or set MIG_AZ (opt out: --no-server-group, --no-copy-secgroup)") +
             ([] if r["out"].count("cannot name the destination copies") == 1 else ["FAIL repeated"]))
    W = sg_world(azs=("internal", "nova", "AZ9"))
    W["dst"]["azs_down"] = ["AZ9"]
    app_only("P6 AZ: 'internal' and an unavailable zone do not count -> nova", ["plan", "app01"], W,
             lambda r: ([] if r["rc"] == [0] else ["rc %s" % r["rc"]]) + want(
                 r, "nova is the only compute availability zone of the destination",
                 "  OK    security group web -> will create nova-web (6 rules)"))
    W = sg_world()
    W["dst"]["az_list_fail"] = True
    app_only("P6 AZ: zone listing fails -> FAIL", ["plan", "app01"], W,
             lambda r: ([] if r["rc"] == [1] else ["rc"]) + want(
                 r, "  FAIL  cannot list the destination compute availability zones to name the copies"))
    W = base_world()
    W["src"]["ports"]["port-src-1"]["security_group_ids"] = []
    app_only("P6 AZ: nothing to copy (no group on the port, no server group) -> no AZ needed, plan OK with 2 AZs",
             ["plan", "app01"], W,
             lambda r: ([] if r["rc"] == [0] else ["rc %s" % r["rc"]]) +
             (["AZ listed"] if calls_of(r, r"availability zone list --compute -f json") else []))
    app_only("P6 AZ: 2 AZs + both opt-outs -> plan OK", ["plan", "app01", "--no-copy-secgroup", "--no-server-group"],
             in_group(base_world()), lambda r: [] if r["rc"] == [0] else ["rc %s" % r["rc"]])

# ================= port fidelity =========================================================================
VIP_PAIRS = [{"ip_address": "198.51.100.99", "mac_address": "fa:16:3e:aa:bb:01"},
             {"ip_address": "198.51.100.98", "mac_address": "00:00:5e:00:01:05"}]


def vip_world(nova_owned=False, desc="vip holder"):
    W = sg_world()
    p_ = W["src"]["ports"]["port-src-1"]
    p_["allowed_address_pairs"] = _copy.deepcopy(VIP_PAIRS)
    p_["description"] = desc
    if nova_owned:
        p_["nova_owned"] = True
    return W


def ps_off_world(nova_owned=False):
    W = p6w()
    p_ = W["src"]["ports"]["port-src-1"]
    p_.update(port_security_enabled=False, security_group_ids=[], description="no filtering")
    if nova_owned:
        p_["nova_owned"] = True
    return W


if sc("p6-port"):
    def chk_pairs(r):
        bad = [] if r["rc"] == [0, 0] else ["rc %s" % r["rc"]]
        mig = calls_of(r, r"^dst\tport create app01-198\.51\.100\.50 ")
        if not mig or "--allowed-address ip-address=198.51.100.99 --allowed-address ip-address=198.51.100.98," \
                       "mac-address=00:00:5e:00:01:05 --description vip holder" not in mig[0]:
            bad.append("port create: %s" % mig)
        # The world after the rollback has no destination port: the migrate step is judged on its calls and on
        # the stub's Neutron (which fills the port's own MAC into a pair given without one).
        return bad + want(r, "  INFO  port fa:16:3e:aa:bb:01: allowed-address-pairs copied: 198.51.100.99 "
                             "(fa:16:3e:aa:bb:01), 198.51.100.98 (00:00:5e:00:01:05)") + dst_empty_but_groups(r) + src_clean(r)
    app_only("P6 pairs (VIP) --ip same: copied (own MAC implied, VRRP MAC kept) + description; rollback restores the source",
             [["migrate", "app01", "--verify", "none"], ["rollback", "app01"]], vip_world(), chk_pairs, show=True)

    def chk_pairs_world(r):
        bad = [] if r["rc"] == [0] else ["rc %s" % r["rc"]]
        p_ = dst_port(r)
        if p_["allowed_address_pairs"] != VIP_PAIRS or p_.get("description") != "vip holder" or not p_["port_security_enabled"]:
            bad.append("destination port %s" % p_)
        return bad
    app_only("P6 pairs --ip same: destination port == source port (pairs, description, port security)",
             [["migrate", "app01", "--verify", "none"]], vip_world(), chk_pairs_world)

    def chk_pairs_addr(r):
        bad = [] if r["rc"] == [0] else ["rc %s" % r["rc"]]
        p_ = dst_port(r)
        want_pairs = [{"ip_address": "198.51.100.99", "mac_address": p_["mac_address"]},
                      {"ip_address": "198.51.100.98", "mac_address": "00:00:5e:00:01:05"}]
        if p_["mac_address"] == "fa:16:3e:aa:bb:01" or p_["allowed_address_pairs"] != want_pairs:
            bad.append("destination port %s" % p_)
        return bad
    app_only("P6 pairs --ip <addr>: the pair on the port's own MAC follows the new MAC, the VRRP MAC is kept",
             [["migrate", "app01", "--ip", "198.51.100.77", "--verify", "none"]], vip_world(), chk_pairs_addr)

    def chk_pairs_new(r):
        bad = [] if r["rc"] == [0] else ["rc %s" % r["rc"]]
        p_ = dst_port(r)
        if p_["allowed_address_pairs"] or calls_of(r, r"^dst\tport create .*--allowed-address"):
            bad.append("pairs copied with --ip new")
        if p_.get("description") != "vip holder":
            bad.append("description not copied with --ip new")
        return bad + want(r, "  WARN  port fa:16:3e:aa:bb:01: allowed-address-pairs not copied with --ip new (they point "
                             "at the old addresses; add what still applies yourself): 198.51.100.99 (fa:16:3e:aa:bb:01), "
                             "198.51.100.98 (00:00:5e:00:01:05)",
                          "WARN allowed-address-pairs of source port fa:16:3e:aa:bb:01 not copied (--ip new); to add "
                          "back what still applies: openstack port set --allowed-address ip-address=198.51.100.99 "
                          "--allowed-address ip-address=198.51.100.98,mac-address=00:00:5e:00:01:05 port-dst-")
    app_only("P6 pairs --ip new: dropped + WARN (preflight and log, with the command to add them back)",
             [["migrate", "app01", "--ip", "new", "--verify", "none"]], vip_world(), chk_pairs_new)

    def chk_psoff(r):
        bad = [] if r["rc"] == [0, 0] else ["rc %s" % r["rc"]]
        mig = calls_of(r, r"^dst\tport create app01-")
        if not mig or "--no-security-group --disable-port-security --description no filtering" not in mig[0]:
            bad.append("port create: %s" % mig)
        if calls_of(r, r"\tsecurity group") or calls_of(r, r"availability zone list --compute -f json"):
            bad.append("no group to copy, yet: %s" % calls_of(r, r"security group|zone list --compute -f json"))
        return bad + want(r, "  INFO  port fa:16:3e:aa:bb:01: port security disabled at the source: the destination port "
                             "is created with --disable-port-security and no security group") + dst_empty_but_groups(r) + src_clean(r)
    app_only("P6 port security off: destination port without port security and without group; rollback",
             [["migrate", "app01", "--verify", "none"], ["rollback", "app01"]], ps_off_world(), chk_psoff, show=True)
    app_only("P6 port security off: destination port state", [["migrate", "app01", "--verify", "none"]], ps_off_world(),
             lambda r: [] if (dst_port(r)["port_security_enabled"] is False and dst_port(r)["security_group_ids"] == []
                              and dst_port(r).get("description") == "no filtering") else ["port %s" % dst_port(r)])

    def chk_psoff_conflict(r):
        bad = [] if r["rc"] == [1] else ["rc %s" % r["rc"]]
        if r["world"]["dst"] != dict(r["world0"]["dst"], images={}) and r["world"]["dst"] != r["world0"]["dst"]:
            bad.append("destination changed")
        if r["world"]["src"]["servers"]["srv-src-app01"]["status"] != "ACTIVE":
            bad.append("source stopped")
        return bad + want(r, "ERROR: port fa:16:3e:aa:bb:01 has port security disabled at the source, so its copy takes no "
                             "security group: --security-group web conflicts with it — drop --security-group, or enable "
                             "port security on the source port first")
    app_only("P6 port security off + --security-group -> die, nothing touched",
             [["migrate", "app01", "--security-group", "web"]], ps_off_world(), chk_psoff_conflict)
    app_only("P6 port security off + --security-group -> plan dies too",
             ["plan", "app01", "--security-group", "web"], ps_off_world(),
             lambda r: ([] if r["rc"] == [1] else ["rc"]) + want(r, "--security-group web conflicts with it"))

    W = ps_off_world()
    W["dst"]["extensions"] = ["security-group"]
    app_only("P6 port security off, destination without port-security extension -> FAIL", ["plan", "app01"], W,
             lambda r: ([] if r["rc"] == [1] else ["rc"]) + want(
                 r, "  FAIL  port fa:16:3e:aa:bb:01 has port security disabled at the source, the destination has no "
                    "port-security extension"))

    def chk_reserve_pairs(r):
        bad = [] if r["rc"] == [0, 0] else ["rc %s" % r["rc"]]
        res = calls_of(r, r"^src\tport create mig-reserve-app01-0 ")
        if not res or "--allowed-address ip-address=198.51.100.99 --allowed-address ip-address=198.51.100.98," \
                       "mac-address=00:00:5e:00:01:05 --description vip holder" not in res[0]:
            bad.append("reserve port create: %s" % res)
        # After the rollback the source VM holds the reserve port: same pairs/description/port security as the original.
        srv = r["world"]["src"]["servers"]["srv-src-app01"]
        rp = r["world"]["src"]["ports"].get(srv["ports"][0]) if srv["ports"] else None
        if not rp or rp["name"] != "mig-reserve-app01-0" or rp["allowed_address_pairs"] != VIP_PAIRS \
                or rp.get("description") != "vip holder" or rp["port_security_enabled"] is not True:
            bad.append("reattached reserve port %s" % rp)
        return bad + want(r, "Nova removed its own port; reserved 198.51.100.50 fa:16:3e:aa:bb:01 again as port-src-",
                          "reattached source port port-src-")
    app_only("P6 reserve port (Nova-owned) carries the pairs + description; rollback reattaches it",
             [["migrate", "app01", "--verify", "none"], ["rollback", "app01"]], vip_world(nova_owned=True), chk_reserve_pairs)

    def chk_reserve_psoff(r):
        bad = [] if r["rc"] == [0, 0] else ["rc %s" % r["rc"]]
        res = calls_of(r, r"^src\tport create mig-reserve-app01-0 ")
        if not res or "--no-security-group --disable-port-security --description no filtering" not in res[0]:
            bad.append("reserve port create: %s" % res)
        srv = r["world"]["src"]["servers"]["srv-src-app01"]
        rp = r["world"]["src"]["ports"].get(srv["ports"][0]) if srv["ports"] else None
        if not rp or rp["port_security_enabled"] is not False or rp["security_group_ids"]:
            bad.append("reattached reserve port %s" % rp)
        return bad
    app_only("P6 reserve port (Nova-owned) with port security off keeps it off; rollback reattaches it",
             [["migrate", "app01", "--verify", "none"], ["rollback", "app01"]], ps_off_world(nova_owned=True),
             chk_reserve_psoff)

    def dns_world(name, ext):
        W = sg_world()
        W["src"]["ports"]["port-src-1"]["dns_name"] = name
        if ext:
            W["dst"]["extensions"] = ["security-group", "port-security", "allowed-address-pairs", "dns-integration"]
        return W
    app_only("P6 dns_name, destination without dns-integration -> WARN, not copied, migrate goes on",
             [["migrate", "app01", "--verify", "none"]], dns_world("app01", False),
             lambda r: ([] if r["rc"] == [0] else ["rc %s" % r["rc"]]) + want(
                 r, "  WARN  port fa:16:3e:aa:bb:01: dns_name app01 not copied: the destination has no dns-integration "
                    "extension") + (["--dns-name passed"] if calls_of(r, r"--dns-name") else []))
    app_only("P6 dns_name = the hostname, destination with dns-integration -> copied",
             [["migrate", "app01", "--verify", "none"]], dns_world("app01", True),
             lambda r: ([] if r["rc"] == [0] else ["rc %s" % r["rc"]]) + want(
                 r, "  INFO  port fa:16:3e:aa:bb:01: dns_name app01 copied") +
             ([] if dst_port(r).get("dns_name") == "app01" else ["dns_name %s" % dst_port(r).get("dns_name")]))
    app_only("P6 dns_name not the hostname (Nova would refuse the port) -> WARN, not copied",
             [["migrate", "app01", "--verify", "none"]], dns_world("web-vip", True),
             lambda r: ([] if r["rc"] == [0] else ["rc %s" % r["rc"]]) + want(
                 r, "  WARN  port fa:16:3e:aa:bb:01: dns_name web-vip not copied: Nova refuses a port whose dns_name is "
                    "not the new instance's hostname") + (["--dns-name passed"] if calls_of(r, r"--dns-name") else []))

    W = vip_world()
    W["src"]["ports"]["port-src-1"]["fixed_ips"].append({"subnet_id": "sub-src-1", "ip_address": "198.51.100.51"})
    app_only("P6 several fixed IPs: still refused (pairs no longer are)", ["plan", "app01"], W,
             lambda r: ([] if r["rc"] == [1] else ["rc"]) + want(
                 r, "  FAIL  port fa:16:3e:aa:bb:01 has several fixed IPs (not handled)") +
             unwanted(r, "has allowed-address-pairs (not handled)"))

    app_only("P6 migrate volume refuses --no-copy-secgroup / --no-server-group (server options)",
             ["volume", L1, "--no-copy-secgroup", "--no-server-group"], p4_world(),
             lambda r: ([] if r["rc"] == [1] else ["rc"]) + want(
                 r, "creates no server or port: --no-copy-secgroup --no-server-group cannot apply (it takes "
                    "--volume-type, --az, --volume-az and --verify)"),  # Intended: 7.2 C6
             impl="app")

# ================= cross build + the CLI path =============================================================
if sc("p6-cross"):
    def chk_cross(r):
        bad = [] if r["rc"] == [0, 0] else ["rc %s" % r["rc"]]
        return bad + dst_empty_but_groups(r) + src_clean(r) + (
            [] if group_id(r["world"], "dst", "nova-web") else ["copies gone"])
    app_only("P6 cross: app migrate (pairs + group copies) -> bash rollback cleans the destination, keeps the groups",
             [["migrate", "app01", "--verify", "none"], ["rollback", "app01"]], vip_world(), chk_cross, impl="py+bash")
    app_only("P6 CLI: migrate app01 --yes on the defaults (selector child), then rollback",
             [["migrate", "app01", "--yes", "--verify", "none"], ["rollback", "app01"]], in_group(vip_world()),
             lambda r: ([] if r["rc"] == [0, 0] else ["rc %s" % r["rc"]]) + want(
                 r, "Totals: OK 1  FAIL 0  SKIP 0", "will create nova-web (6 rules)", "will create nova-web-anti")
             + dst_empty_but_groups(r) + src_clean(r), impl="app")

# ================= regression: a plain VM is the 7.0 behaviour but for the group names ======================
# Generated IDs, and the addresses the stub derives from its ID counter (a port without a fixed IP or MAC:
# --ip new): the app's group copies use counter values first, so these shift without meaning anything.
_IDS = _re.compile(r"\b(vol|port|srv|sg|img|secgrp|rule)-(src|dst)-\d{4}\b|\b(198\.51\.100)\.(1\d\d)\b|"
                   r"\b(fa:16:3e:00:00):([0-9a-f]{2})\b")
_DROP = _re.compile(r"\t(security group |token issue|availability zone list --compute -f json|server group list --long)")


def _renumber(texts):
    m, n = {}, {}
    for t in texts:
        for x in _IDS.finditer(t):
            if x.group(0) not in m:
                k = (x.group(1), x.group(2)) if x.group(1) else ("addr", x.group(3) or x.group(5))
                n[k] = n.get(k, 0) + 1
                m[x.group(0)] = "%s-%s-#%d" % (k[0], k[1], n[k])
    return lambda t: _IDS.sub(lambda x: m.get(x.group(0), x.group(0)), t)


def _sg_line(x):
    return "security group" in x or "copies of the source server group" in x


def _view(r, strip):
    names = {side: {k: (g["name"][len(strip):] if side == "dst" and strip and g["name"].startswith(strip) else g["name"])
                    for k, g in r["world"][side]["security_groups"].items()} for side in ("src", "dst")}
    calls = []
    for l in r["calls"]:
        if _DROP.search(l):
            continue
        side = l.split("\t", 1)[0]
        if side in names:
            l = _re.sub(r"--security-group (\S+)", lambda x: "--security-group <%s>" % names[side].get(x.group(1), x.group(1)), l)
        calls.append(l)
    out = "\n".join(x for x in r["out"].splitlines() if not _sg_line(x))
    ren = _renumber(calls + [out])  # a MAC Neutron picked shows in the output only
    w_ = _copy.deepcopy(r["world"])
    w_.pop("counter", None)
    w_.pop("ids", None)  # 7.2: the stub numbers IDs per prefix; the app creates more of some kinds (the copies)
    for side in ("src", "dst"):
        for p_ in w_[side]["ports"].values():
            p_["security_group_ids"] = ["<%s>" % names[side].get(g, g) for g in p_.get("security_group_ids", [])]
        w_[side].pop("security_groups", None)
    files = {ren(k): ren("\n".join(x for x in v.splitlines() if not _sg_line(x)))
             for k, v in r["files"].items() if not k.endswith("dst-security-groups")}
    return {"rc": r["rc"], "calls": [ren(l) for l in calls], "err": ren(r["err"]), "out": ren(out),
            "files": files, "world": ren(_json.dumps(w_, sort_keys=True, indent=0))}


def p6_equivalent(b, p):
    vb, vp = _view(b, ""), _view(p, "nova-")
    bad = []
    for k in ("rc", "calls", "out", "err", "files", "world"):
        if vb[k] == vp[k]:
            continue
        if isinstance(vb[k], dict):
            d = "\n".join("  file %s:\n%s" % (f, "\n".join(_difflib.unified_diff(
                (vb[k].get(f) or "").splitlines(), (vp[k].get(f) or "").splitlines(), "bash", "py", lineterm="", n=1)))
                for f in sorted(set(vb[k]) | set(vp[k])) if vb[k].get(f) != vp[k].get(f))
        else:
            a_, b_ = (vb[k], vp[k]) if isinstance(vb[k], list) else (str(vb[k]).splitlines(), str(vp[k]).splitlines())
            d = "\n".join(_difflib.unified_diff(a_, b_, "bash", "py", lineterm="", n=1))
        bad.append("%s differs beyond the group names:\n%s" % (k, d[:3000]))
    if not any(g["name"].startswith("nova-") for g in p["world"]["dst"]["security_groups"].values()):
        if "will create nova-" not in p["out"]:  # a plan creates nothing: it announces the copies
            bad.append("no nova-* copy at the destination")
    return bad


if sc("p6-regress"):
    ALL = ("out", "err", "rc", "files", "calls", "world")
    compare("P6 regression: plan of a plain VM == 7.0 but the group names", ["plan", "app01"], world=p6w(), p6=True,
            expect_diff=ALL, check=p6_equivalent)
    compare("P6 regression: cold migrate + rollback == 7.0 but the group names",
            [["migrate", "app01", "--verify", "full"], ["rollback", "app01"]], world=p6w(), p6=True, expect_diff=ALL,
            check=p6_equivalent)
    W = p6w()
    W["src"]["ports"]["port-src-1"]["nova_owned"] = True
    compare("P6 regression: Nova-owned port (reserve) + rollback", [["migrate", "app01", "--verify", "none"], ROLL],
            world=W, p6=True, expect_diff=ALL, check=p6_equivalent)
    compare("P6 regression: --ip new --start-source + rollback", [["migrate", "app01", "--ip", "new", "--start-source"], ROLL],
            world=p6w(), p6=True, expect_diff=ALL, check=p6_equivalent)
    W = live_world([p(500), p(10), p(10)], [p(100), p(1), p(1)])
    W["dst"]["azs"]["compute"] = ["nova"]
    compare("P6 regression: live + rollback", [["migrate", "app01", "--mode", "live"], ROLL], world=W, p6=True,
            expect_diff=ALL, check=p6_equivalent)
    W = multinic_world()
    W["dst"]["azs"]["compute"] = ["nova"]
    compare("P6 regression: 2 NICs + rollback", [["migrate", "app01", "--verify", "none"], ROLL], world=W, p6=True,
            expect_diff=ALL, check=p6_equivalent)
    W = eph_world()
    W["dst"]["azs"]["compute"] = ["nova"]
    compare("P6 regression: ephemeral + rollback", [["migrate", "eph01"], EROLL], world=W, p6=True, expect_diff=ALL,
            check=p6_equivalent)
    W = p4_world()
    W["dst"]["azs"]["compute"] = ["nova"]
    compare("P6 regression: selector one VM (batch vs app) + rollback",
            [["--vm", ID1, "--yes", "--", "--verify", "none"], ["rollback", ID1]], world=W, impls=BOTH,
            py_args=[["migrate", "--vm", ID1, "--yes", "--verify", "none"], ["rollback", ID1]], p6=True,
            expect_diff=ALL, check=p6_equivalent)
