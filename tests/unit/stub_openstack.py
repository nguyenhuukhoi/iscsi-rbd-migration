#!/usr/bin/python3
"""Fake openstack CLI over a JSON world (HARNESS_WORLD). Side chosen by OS_AUTH_URL (src|dst).
Every call is appended to HARNESS_CALLS as 'side<TAB>argv' so two implementations can be diffed."""
import json
import os
import sys

WORLD = os.environ["HARNESS_WORLD"]
side = "src" if "src" in os.environ.get("OS_AUTH_URL", "") else "dst"
argv = sys.argv[1:]
with open(os.environ["HARNESS_CALLS"], "a") as f:
    f.write("%s\t%s\n" % (side, " ".join(argv)))
API_VERSION = None
if argv[:1] == ["--os-compute-api-version"]:  # 7.3 (--dst-host): a global option before the command
    API_VERSION, argv = argv[1], argv[2:]
w = json.load(open(WORLD))
c = w[side]
c.setdefault("images", {})


def save():
    json.dump(w, open(WORLD, "w"), indent=1)


def new_id(prefix):
    """7.2: numbered per prefix (vol-dst-0001, vol-dst-0002 ...), so that creating the same objects in another
    order (A1: destination volumes before the clones) gives them the same IDs. The global counter still moves
    for every object: addresses and MACs are derived from it."""
    w["counter"] += 1
    n = w.setdefault("ids", {}).get(prefix, 0) + 1
    w["ids"][prefix] = n
    return "%s-%04d" % (prefix, n)


def fail(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def opt(name, default=None):
    if name in argv:
        return argv[argv.index(name) + 1]
    return default


def opts(name):
    return [argv[i + 1] for i, a in enumerate(argv) if a == name]


def emit(obj):
    """-f value -c X / -f json, as openstackclient prints them."""
    if opt("-f") == "value":
        cols = opts("-c")
        rows = obj if isinstance(obj, list) else [obj]
        for r in rows:
            print(" ".join("None" if r.get(col) is None else str(r.get(col)) for col in cols))
    else:
        print(json.dumps(obj, indent=2))


def find(coll, key):
    """By ID, else by unique name."""
    if key in c[coll]:
        return key, c[coll][key]
    hits = [(k, v) for k, v in c[coll].items() if v.get("name") == key]
    if len(hits) == 1:
        return hits[0]
    if not hits and coll == "ports":
        fail("No Port found for %s" % key)  # what openstackclient prints for a network resource (openstacksdk)
    if not hits:
        fail("No %s with a name or ID of '%s' exists." % (coll.rstrip("s"), key))
    fail("More than one %s exists with the name '%s'." % (coll.rstrip("s"), key))


c.setdefault("projects", {c["project_id"]: {"name": "admin"}})


def extensions():
    """Network extension aliases of this side (P6 checks port-security, allowed-address-pairs, dns-integration)."""
    return c.get("extensions", ["security-group", "port-security", "allowed-address-pairs"])


def project_of(key):
    """Project ID by ID or name, None when unknown."""
    if key in c["projects"]:
        return key
    hits = [k for k, p in c["projects"].items() if p["name"] == key]
    return hits[0] if len(hits) == 1 else None


# 7.2 (A2): 'quota show --usage -f json' rows; world[side]["quota"] = {resource: [limit, in_use, reserved]}.
DEFAULT_QUOTA = {"instances": [10, 0, 0], "cores": [20, 0, 0], "ram": [51200, 0, 0], "volumes": [10, 0, 0],
                 "gigabytes": [1000, 0, 0], "ports": [50, 0, 0], "secgroups": [10, 0, 0], "secgroup-rules": [100, 0, 0]}


def fips():
    return c.setdefault("floating_ips", {})


a = argv
pos = [x for x in a]
# 7.2: world[side]["fail"] = {"<argv prefix>": "<message>"}: that command fails (e.g. "image list": "HTTP 503")
for _prefix, _msg in c.get("fail", {}).items():
    if " ".join(a).startswith(_prefix):
        fail(_msg)
# 1.1.1: world[side]["fail_from"] = {"<argv prefix>": [n, "<message>"]}: the first n such commands answer as usual,
# every later one fails with that message (an endpoint lost in the middle of a rollback)
for _prefix, _spec in c.get("fail_from", {}).items():
    if " ".join(a).startswith(_prefix):
        if _spec[0] > 0:
            _spec[0] -= 1
            save()
        else:
            fail(_spec[1])
# 1.1.0: world[side]["answer"] = {"<argv prefix>": "<stdout>"}: that command exits 0 printing exactly this (a proxy or
# client that answers something else than the object asked for)
for _prefix, _out in c.get("answer", {}).items():
    if " ".join(a).startswith(_prefix):
        sys.stdout.write(_out)
        sys.exit(0)
# 1.1.1 (P2-a): world[side]["need_project"] = "<name>": the objects of this side live in that project; a token scoped
# to another one (OS_PROJECT_NAME) does not see them: 'server|volume show|delete' say "No <Kind> found for <id>" and
# 'server list' is empty.
if c.get("need_project") and os.environ.get("OS_PROJECT_NAME") != c["need_project"]:
    if a[:2] in (["server", "show"], ["server", "delete"], ["volume", "show"], ["volume", "delete"]):
        fail("No %s found for %s" % (a[0].capitalize(), a[2]))
    if a[:2] == ["server", "list"]:
        emit([])
        sys.exit(0)
if a[:2] == ["port", "show"] and "device_id" in a and a[2] in c.get("port_show_fail", {}):
    # 7.2 (B1): a transient failure ([count, message]) before the port answers again
    n, msg = c["port_show_fail"][a[2]]
    if n > 0:
        c["port_show_fail"][a[2]] = [n - 1, msg]
        save()
        fail(msg)
if a[:3] == ["quota", "show", "--usage"]:
    if c.get("quota_fail"):
        fail(c["quota_fail"])
    emit([{"Resource": k, "Limit": v[0], "In Use": v[1], "Reserved": v[2]}
          for k, v in c.get("quota", DEFAULT_QUOTA).items()])
elif a[:3] == ["role", "assignment", "list"]:  # 7.2 (D2): world[side]["roles"], default admin
    emit([{"Role": x} for x in c.get("roles", ["admin"])])
elif a[:3] == ["limits", "show", "--absolute"]:
    emit([{"Name": "maxServerGroupMembers", "Value": c.get("max_group_members", 10)},
          {"Name": "maxTotalInstances", "Value": 10}])
elif a[:3] == ["floating", "ip", "list"]:
    port = opt("--port")
    emit([{"ID": k, "Floating IP Address": x["floating_ip_address"], "Port": x.get("port_id")}
          for k, x in fips().items() if not port or x.get("port_id") == port])
elif a[:3] == ["floating", "ip", "show"]:
    hit = [(k, x) for k, x in fips().items() if a[3] in (k, x["floating_ip_address"])]
    if not hit:
        fail("No FloatingIP found for %s" % a[3])
    emit(dict(hit[0][1], id=hit[0][0]))
elif a[:3] == ["floating", "ip", "set"]:
    hit = [x for k, x in fips().items() if a[-1] in (k, x["floating_ip_address"])]
    if not hit:
        fail("No FloatingIP found for %s" % a[-1])
    if c.get("fip_set_fail"):
        fail(c["fip_set_fail"])
    hit[0]["port_id"] = opt("--port")
    save()
elif a[:2] == ["project", "show"]:
    pid = project_of(a[2])
    if pid is None:
        fail("No project with a name or ID of '%s' exists." % a[2])
    emit({"id": pid, "name": c["projects"][pid]["name"], "domain_id": "default", "enabled": True})
elif a[:2] == ["server", "list"] and "--long" in a:
    import re
    allp = "--all-projects" in a
    if allp and c.get("forbid_all_projects"):
        fail("Policy doesn't allow os_compute_api:servers:index:get_all_tenants to be performed. (HTTP 403)")
    name, host, proj = opt("--name", ""), opt("--host"), opt("--project")
    want = project_of(proj) if proj else None
    if proj and want is None:
        fail("No project with a name or ID of '%s' exists." % proj)
    rows = []
    for k, v in c["servers"].items():
        spid = v.get("project_id", c["project_id"])
        if not allp and spid != c["project_id"]:
            continue
        if name and not re.search(name, v["name"]):
            continue
        if host and v.get("host", "cmp1") != host:
            continue
        if want and spid != want:
            continue
        r = {"ID": k, "Name": v["name"], "Status": v["status"], "Host": v.get("host", "cmp1")}
        if not c.get("short_list"):  # 7.3: the other columns of openstackclient 10.5's --long (lab sample)
            img = v.get("image")
            nets = {}
            for p in v.get("ports", []):
                if p in c["ports"]:
                    pn = c["networks"][c["ports"][p]["network_id"]]["name"]
                    nets.setdefault(pn, []).extend(f["ip_address"] for f in c["ports"][p]["fixed_ips"])
            fl = v.get("flavor")
            r.update({"Task State": None, "Power State": 1 if v["status"] == "ACTIVE" else 4,
                      "Networks": v.get("networks_column", nets),
                      "Image Name": img.split(" (")[0] if isinstance(img, str) and img and "booted from" not in img
                      else (img if isinstance(img, str) and img else "N/A (booted from volume)"),
                      "Image ID": "N/A (booted from volume)" if not img else img,
                      "Flavor": fl.get("original_name") if isinstance(fl, dict) else fl,
                      "Availability Zone": v.get("availability_zone") or "nova", "Properties": v.get("properties", {})})
        if not c.get("no_project_column"):
            r["Project ID"] = spid
        rows.append(r)
    emit(rows)
elif a[:2] == ["volume", "list"] and "--all-projects" in a:  # 7.3 (list): one listing, with attachments
    if c.get("forbid_all_projects"):
        fail("Policy doesn't allow volume:get_all to be performed. (HTTP 403)")
    att = {}
    for sid, s in c["servers"].items():
        for x in s.get("volumes", []):
            att.setdefault(x["Volume ID"], []).append(
                {"id": x["Volume ID"], "attachment_id": "att-" + x["Volume ID"], "volume_id": x["Volume ID"],
                 "server_id": sid, "host_name": s.get("host", "cmp1"), "device": x["Device"]})
    emit([{"ID": k, "Name": v.get("name"), "Status": v["status"], "Size": v.get("size"),
           "Type": v.get("type") or "__DEFAULT__", "Bootable": v.get("bootable", "false"), "Attached to": att.get(k, []),
           "Properties": {}} for k, v in c["volumes"].items()])
elif a[:2] == ["volume", "list"] and "--project" in a:
    pid = opt("--project")
    emit([{"ID": k, "Name": v.get("name"), "Status": v["status"], "Size": v.get("size")}
          for k, v in c["volumes"].items() if v.get("project_id", c["project_id"]) == pid])
elif a[:2] == ["token", "issue"]:
    emit({"project_id": c["project_id"], "id": "tok-" + side})
elif a[:2] == ["endpoint", "list"]:
    emit([{"URL": w.get("glance_url", "")}])
elif a[:3] == ["server", "image", "create"]:
    iid = new_id("img-" + side)
    c.setdefault("images", {})[iid] = {"name": opt("--name"), "status": c.get("snap_status", "active"),
                                       "disk_format": "qcow2", "properties": c.get("snap_props", {})}
    save(); emit({"id": iid})
elif a[:2] == ["image", "show"]:
    iid, im = find("images", a[2])
    emit(dict(im, id=iid))
elif a[:2] == ["image", "save"]:
    iid, im = find("images", a[-1])
    open(opt("--file"), "w").write(c.get("image_data", "EPHEMERAL-ROOT-DATA-" + im["name"]))
elif a[:2] == ["image", "list"]:
    emit([{"ID": k} for k, im in c.get("images", {}).items() if im["name"] == opt("--name")])
elif a[:2] == ["image", "delete"]:
    for iid in a[2:]:
        if iid not in c.get("images", {}):
            fail("No image with a name or ID of '%s' exists." % iid)
        del c["images"][iid]
    save()
elif a[:2] == ["server", "list"]:
    name = opt("--name", "")
    import re
    allp = "--all-projects" in a
    if allp and c.get("forbid_all_projects"):  # 1.1.1: rollback lists a server it never recorded in every project
        fail("Policy doesn't allow os_compute_api:servers:index:get_all_tenants to be performed. (HTTP 403)")
    # 1.1.1: world[side]["list_own_project"]: without --all-projects only the token's own project is listed
    own = c.get("list_own_project") and not allp
    rows = [{"ID": k, "Name": v["name"]} for k, v in c["servers"].items() if (not name or re.search(name, v["name"]))
            and not (own and v.get("project_id", c["project_id"]) != c["project_id"])]
    if "--host" in a:  # 7.3 (inventory --count): the VMs of one host, every project
        rows = [x for x in rows if c["servers"][x["ID"]].get("host", "cmp1") == opt("--host")]
    emit(rows)
elif a[:2] == ["server", "show"]:
    sid, s = find("servers", a[2])
    out = {k: v for k, v in s.items() if k not in ("ports", "volumes")}
    out["id"] = sid
    if side == "dst" and "created" not in out:  # 7.2 (C8): a server this run created was created just now
        import time
        out["created"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    emit(out)
elif a[:3] == ["server", "volume", "list"]:
    sid, s = find("servers", a[3])
    emit([{"ID": "att-" + v["Volume ID"], "Device": v["Device"], "Server ID": sid, "Volume ID": v["Volume ID"]}
          for v in s["volumes"]])
elif a[:2] == ["port", "list"]:
    if "--server" in a:
        emit([{"ID": p} for p in c["servers"][opt("--server")]["ports"] if p in c["ports"]])
    else:
        mac, fip, net = opt("--mac-address"), opt("--fixed-ip"), opt("--network")
        rows = []
        for k, p in c["ports"].items():
            if mac and p["mac_address"] != mac:
                continue
            if fip and not any("ip-address=" + f["ip_address"] == fip or "subnet=" + f["subnet_id"] == fip
                               for f in p["fixed_ips"]):
                continue
            if net and c["networks"][p["network_id"]]["name"] != net:
                continue
            rows.append({"ID": k})
        emit(rows)
elif a[:2] == ["port", "show"]:
    pid, p = find("ports", a[2])
    emit(dict(p, id=pid))
elif a[:2] == ["port", "create"]:
    name = a[2]
    netid = [k for k, n in c["networks"].items() if n["name"] == opt("--network")][0]
    fip = dict(x.split("=", 1) for x in opt("--fixed-ip").split(","))
    sub = fip.get("subnet") or [k for k, s in c["subnets"].items() if s["network_id"] == netid][0]
    ip = fip.get("ip-address") or "198.51.100.%d" % (100 + w["counter"])
    # P6: Neutron's rules for port security / allowed-address-pairs / dns_name, as the real API answers.
    ps = "--disable-port-security" not in a
    if not ps and opts("--security-group"):
        fail("Port has security group associated. Cannot disable port security or ip address until security group "
             "is removed (HTTP 409)")
    if opts("--allowed-address") and not ps:
        fail("Port Security must be enabled in order to have allowed address pairs on a port. (HTTP 409)")
    if "--dns-name" in a and "dns-integration" not in extensions():
        fail("Unrecognized attribute(s) 'dns_name' (HTTP 400)")
    pid = new_id("port-" + side)
    mac = opt("--mac-address") or "fa:16:3e:00:00:%02x" % (w["counter"] % 256)
    pairs = []
    for spec in opts("--allowed-address"):
        d = dict(x.split("=", 1) for x in spec.split(","))
        pairs.append({"ip_address": d["ip-address"], "mac_address": d.get("mac-address", mac)})
    c["ports"][pid] = {"name": name, "network_id": netid, "fixed_ips": [{"subnet_id": sub, "ip_address": ip}],
                       "mac_address": mac,
                       "security_group_ids": opts("--security-group"), "device_id": "",
                       "port_security_enabled": ps, "allowed_address_pairs": pairs}
    for key, flag in (("description", "--description"), ("dns_name", "--dns-name")):
        if flag in a:
            c["ports"][pid][key] = opt(flag)
    save(); emit({"id": pid})
elif a[:2] == ["port", "set"]:
    c["ports"][a[-1]]["name"] = opt("--name"); save()
elif a[:2] == ["port", "delete"]:
    pid, _ = find("ports", a[2]); del c["ports"][pid]; save()
elif a[:2] == ["network", "show"]:
    nid, n = find("networks", a[2]); emit(dict(n, id=nid))
elif a[:2] == ["subnet", "show"]:
    sid, s = find("subnets", a[2])
    if "allocation_pools" not in s:  # 7.2 (A2): Neutron's default pool, .2 up to the last host
        import ipaddress
        n = ipaddress.ip_network(s["cidr"], strict=False)
        s = dict(s, allocation_pools=[{"start": str(n.network_address + 2), "end": str(n.broadcast_address - 1)}])
    emit(dict(s, id=sid))
elif a[:2] == ["subnet", "list"]:
    netid = [k for k, n in c["networks"].items() if n["name"] == opt("--network")]
    emit([{"ID": k, "Subnet": s["cidr"]} for k, s in c["subnets"].items() if netid and s["network_id"] == netid[0]])
elif a[:3] == ["security", "group", "show"]:
    gid, g = find("security_groups", a[3])
    emit(dict({"description": "", "stateful": True, "rules": []}, **g, id=gid))
elif a[:3] == ["security", "group", "create"]:
    gid = new_id("secgrp-" + side)
    # Neutron puts egress-any rules (IPv4 and IPv6) in every new group.
    rules = [{"id": new_id("rule-" + side), "direction": "egress", "ethertype": eth, "protocol": None,
              "port_range_min": None, "port_range_max": None, "remote_ip_prefix": None, "remote_group_id": None,
              "description": ""} for eth in ("IPv4", "IPv6")]
    c["security_groups"][gid] = {"name": a[3], "project_id": c["project_id"], "description": opt("--description") or "",
                                 "stateful": "--stateless" not in a, "rules": rules}
    save(); emit({"id": gid})
elif a[:3] == ["security", "group", "delete"]:
    gid, _ = find("security_groups", a[3]); del c["security_groups"][gid]; save()
elif a[:4] == ["security", "group", "rule", "list"]:
    gid, g = find("security_groups", a[4])
    emit([{"ID": r["id"]} for r in g.get("rules", [])])
elif a[:4] == ["security", "group", "rule", "delete"]:
    for rid in a[4:]:
        hit = [g for g in c["security_groups"].values() if any(r["id"] == rid for r in g.get("rules", []))]
        if not hit:
            fail("Failed to delete rule with name or ID '%s'" % rid)
        hit[0]["rules"] = [r for r in hit[0]["rules"] if r["id"] != rid]
    save()
elif a[:4] == ["security", "group", "rule", "create"]:
    gid, g = find("security_groups", a[4])
    eth, proto = opt("--ethertype", "IPv4"), opt("--protocol")
    lo = hi = None
    if opt("--dst-port"):
        pr = opt("--dst-port").split(":")
        lo, hi = int(pr[0]), int(pr[-1])
    if opt("--icmp-type") is not None:
        lo = int(opt("--icmp-type"))
    if opt("--icmp-code") is not None:
        hi = int(opt("--icmp-code"))
    rg, rip = opt("--remote-group"), opt("--remote-ip")
    if rg and rg not in c["security_groups"]:
        fail("Security group %s does not exist (HTTP 404)" % rg)
    if not rg and not rip:  # openstackclient's default
        rip = "0.0.0.0/0" if eth == "IPv4" else "::/0"
    if c.get("rule_fail") and proto == c["rule_fail"]:
        fail("Quota exceeded for resources: ['security_group_rule']. (HTTP 409)")
    rule = {"id": new_id("rule-" + side), "direction": "ingress" if "--ingress" in a else "egress", "ethertype": eth,
            "protocol": proto, "port_range_min": lo, "port_range_max": hi, "remote_ip_prefix": rip,
            "remote_group_id": rg, "description": opt("--description") or ""}

    def key(r):
        rp = r["remote_ip_prefix"] or ("" if r["remote_group_id"] else ("0.0.0.0/0" if r["ethertype"] == "IPv4" else "::/0"))
        return (r["direction"], r["ethertype"], r["protocol"], r["port_range_min"], r["port_range_max"], rp,
                r["remote_group_id"])
    dup = [r for r in g.setdefault("rules", []) if key(r) == key(rule)]
    if dup:
        fail("Security group rule already exists. Rule id is %s. (HTTP 409)" % dup[0]["id"])
    g["rules"].append(rule)
    save(); emit({"id": rule["id"]})
elif a[:3] == ["compute", "service", "list"]:  # 7.3 (inventory, --dst-host): world[side]["compute_hosts"]
    if c.get("service_list_fail"):
        fail(c["service_list_fail"])
    hosts = c.get("compute_hosts") or ([{"host": "cmp1", "zone": "nova"}, {"host": "cmp2", "zone": "zone-b"}]
                                       if side == "src" else
                                       [{"host": "dst-cmp1", "zone": "AZ1"}, {"host": "dst-cmp2", "zone": "nova"}])
    emit([{"ID": i + 1, "Binary": "nova-compute", "Host": h["host"], "Zone": h["zone"],
           "Status": h.get("status", "enabled"), "State": h.get("state", "up"),
           "Updated At": "2026-11-20T00:00:00.000000"} for i, h in enumerate(hosts)])
elif a[:2] == ["project", "list"]:
    if c.get("project_list_fail"):
        fail(c["project_list_fail"])
    emit([{"ID": k, "Name": p["name"]} for k, p in c["projects"].items()])
elif a[:2] == ["flavor", "list"]:
    emit([{"ID": "fl-" + n, "Name": n, "RAM": f.get("ram"), "Disk": f.get("disk", 0), "Ephemeral": 0,
           "VCPUs": f.get("vcpus"), "Is Public": True} for n, f in c["flavors"].items()])
elif a[:3] == ["volume", "type", "list"]:
    emit([{"ID": "vt-" + n, "Name": n, "Is Public": True} for n in ["__DEFAULT__"] + c.get("volume_types", [])])
elif a[:2] == ["versions", "show"]:  # 7.3 (doctor): the compute API's microversion range
    if c.get("versions_fail"):
        fail(c["versions_fail"])
    emit([{"Region Name": "RegionOne", "Service Type": "compute", "Version": "2.1", "Status": "CURRENT",
           "Endpoint": "http://%s:8774/v2.1/" % side, "Min Microversion": "2.1",
           "Max Microversion": c.get("nova_max_microversion", "2.96")}])
elif a[:2] == ["extension", "list"]:
    emit([{"Alias": x} for x in extensions()])
elif a[:3] == ["security", "group", "list"]:
    emit([{"ID": k, "Name": g["name"], "Project": g["project_id"]} for k, g in c["security_groups"].items()
          if g["project_id"] == opt("--project")])
elif a[:2] == ["volume", "show"]:
    vid, v = find("volumes", a[2]); emit(dict(v, id=vid))
elif a[:2] == ["volume", "list"]:
    emit([{"ID": k, "Name": v.get("name")} for k, v in c["volumes"].items() if v.get("name") == opt("--name")])
elif a[:2] == ["volume", "create"]:
    if a[2] == "" and side == "dst" and c.get("refuse_empty_name"):
        fail("Invalid input for field/attribute name. Value: . '' is too short (HTTP 400)")
    vid = new_id("vol-" + side)
    src = opt("--source")
    vol = {"name": a[2], "status": "available", "size": int(opt("--size") or c["volumes"].get(src, {}).get("size", 1)),
           "bootable": "false", "encrypted": False, "multiattach": False, "type": opt("--type"),
           "availability_zone": opt("--availability-zone"), "description": opt("--description")}
    if src:
        vol.update({k: c["volumes"][src][k] for k in ("size", "bootable")})
    if side == "dst":
        tp = c.get("type_pool", {})
        vol["pool"] = tp.get(opt("--type") or "", tp.get("", "mig-volumes"))
        if c.get("create_status"):
            vol["status"] = c["create_status"]
    c["volumes"][vid] = vol
    save(); emit({"id": vid})
elif a[:2] == ["volume", "set"]:
    v = c["volumes"][a[2]]
    if "--bootable" in a:
        v["bootable"] = "true"
    for p in opts("--image-property"):
        v.setdefault("set_props", []).append(p)
    save()
elif a[:2] == ["volume", "delete"]:
    vid, _ = find("volumes", a[2])
    if c.get("volume_delete_status"):  # 1.0.0 (A15): the delete leaves the volume in that status
        c["volumes"][vid]["status"] = c["volume_delete_status"]
    else:
        del c["volumes"][vid]
    save()
elif a[:3] == ["volume", "type", "show"]:
    if a[3] not in ["__DEFAULT__"] + c.get("volume_types", []):  # the list above always offers it
        fail("No volumetype with a name or ID of '%s' exists." % a[3])
    emit({"name": a[3]})
elif a[:2] == ["flavor", "show"]:
    if a[2] not in c["flavors"]:
        fail("No Flavor found for %s" % a[2])
    emit(dict(c["flavors"][a[2]], name=a[2]))
elif a[:2] == ["keypair", "show"]:
    if a[2] not in c.get("keypairs", []):
        fail("No keypair with a name or ID of '%s' exists." % a[2])
    emit({"name": a[2]})
elif a[:3] == ["availability", "zone", "list"]:
    kind = "compute" if "--compute" in a else "volume"
    if c.get("az_list_fail"):
        fail("Unable to establish connection to the compute endpoint")
    down = c.get("azs_down", [])
    emit([{"Zone Name": z, "Zone Status": "not available" if z in down else "available"}
          for z in c.get("azs", {}).get(kind, [])])
elif a[:3] == ["server", "group", "list"]:
    rows = []
    for k, g in c["server_groups"].items():
        r = {"ID": k, "Name": g["name"], "Policy": g["policy"]}
        if "--long" in a:
            r.update({"Members": g["members"], "Project Id": c["project_id"], "User Id": "u"})
        rows.append(r)
    emit(rows)
elif a[:3] == ["server", "group", "show"]:
    gid, g = find("server_groups", a[3])
    emit({"id": gid, "members": g["members"], "name": g["name"], "policy": g["policy"], "rules": g.get("rules", {})})
elif a[:3] == ["server", "group", "create"]:
    if opts("--rule") and c.get("refuse_rule"):
        fail("--os-compute-api-version 2.64 or greater is required to support the --rule option")
    gid = new_id("sg-" + side)
    rules = dict(x.split("=", 1) for x in opts("--rule"))
    c["server_groups"][gid] = {"name": a[-5], "policy": opt("--policy"), "rules": rules, "members": []}
    save(); emit({"id": gid})
elif a[:2] == ["server", "create"]:
    if "--host" in a:  # 7.3: what Nova and openstackclient answer to server create --host
        if API_VERSION is None or tuple(int(x) for x in API_VERSION.split(".")) < (2, 74):
            fail("--os-compute-api-version 2.74 or greater is required to support the --host option")
        if "admin" not in c.get("roles", ["admin"]):
            fail("Policy doesn't allow compute:servers:create:requested_destination to be performed. (HTTP 403)")
    if c.get("create_server_fail"):
        fail(c["create_server_fail"])
    sid = new_id("srv-" + side)
    vols = []
    if opt("--volume"):
        vols.append({"Volume ID": opt("--volume"), "Device": "/dev/vda"})
    for bd in opts("--block-device"):
        d = dict(x.split("=", 1) for x in bd.split(","))
        vols.append({"Volume ID": d["uuid"], "Device": "/dev/" + d["device_name"]})
    ports = opts("--port")
    s = {"name": a[2], "status": c.get("create_server_status", "ACTIVE"), "locked": False,
         "flavor": {"original_name": opt("--flavor")}, "key_name": opt("--key-name"), "ports": ports, "volumes": vols,
         "hint": opts("--hint"), "properties": dict(p.split("=", 1) for p in opts("--property")),
         "description": opt("--description"), "availability_zone": opt("--availability-zone"),
         "image": opt("--image")}
    if opt("--host"):
        s.update({"host": opt("--host"), "api_version": API_VERSION})
    c["servers"][sid] = s
    for p in ports:
        c["ports"][p]["device_id"] = sid
    for v in vols:
        c["volumes"][v["Volume ID"]]["status"] = "in-use"
    for h in opts("--hint"):
        if h.startswith("group="):
            c["server_groups"][h[6:]]["members"].append(sid)
    save(); emit({"id": sid})
elif a[:2] == ["server", "delete"]:
    sid, s = find("servers", a[2])
    if c.get("server_delete_noop"):  # 1.0.0 (A4): Nova accepted the delete, the server stays
        sys.exit(0)
    for v in s["volumes"]:
        if v["Volume ID"] in c["volumes"]:
            c["volumes"][v["Volume ID"]]["status"] = "available"
    for p in s["ports"]:
        if p in c["ports"]:
            c["ports"][p]["device_id"] = ""
    del c["servers"][sid]; save()
elif a[:2] in (["server", "stop"], ["server", "start"], ["server", "lock"], ["server", "unlock"]):
    sid, s = find("servers", a[2])
    if a[1] == "stop":
        s["status"] = "SHUTOFF"
    elif a[1] == "start":
        s["status"] = "ACTIVE"
    else:
        s["locked"] = a[1] == "lock"
    save()
elif a[:3] == ["server", "remove", "port"]:
    sid, s = find("servers", a[3]); pid = a[4]
    s["ports"].remove(pid)
    if c["ports"][pid].get("nova_owned"):
        del c["ports"][pid]
        for x in fips().values():  # Neutron drops the association of a deleted port
            if x.get("port_id") == pid:
                x["port_id"] = None
    else:
        c["ports"][pid]["device_id"] = ""
    save()
elif a[:3] == ["server", "add", "port"]:
    sid, s = find("servers", a[3]); pid = a[4]
    s["ports"].append(pid); c["ports"][pid]["device_id"] = sid; save()
else:
    fail("stub openstack: unhandled: %s" % " ".join(a), 2)
