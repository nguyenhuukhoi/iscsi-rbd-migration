#!/usr/bin/env python3
"""7.3 unit checks (DESIGN-V7-3-CLI section 10 and 11): every section 1-8 has cases named after it, every SEC rule
one case named 'SEC-<n> ...'. Runs the app in this process against the fake clouds (inproc.py).

    python3 v73unit.py <directory holding migratetool/>
"""
import io
import json
import os
import shutil
import stat
import sys
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # run with -I: no implicit script directory
import inproc  # noqa: E402  (puts the package on sys.path, patches proc.run and the selector child)
from inproc import app, edit, idx, tree, world  # noqa: E402
import migratetool  # noqa: E402
from migratetool import config, jsonout, record  # noqa: E402
from migratetool.options import Options  # noqa: E402
from migratetool.util import Die  # noqa: E402

PASS = FAIL = 0
PLAIN = ["--no-copy-secgroup", "--no-server-group"]
ROLL = ("rollback", "app01")


def ok(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS " + label)
    else:
        FAIL += 1
        print("FAIL %s %s" % (label, str(detail)[-3000:]))


def dies(label, fn, want):
    try:
        fn()
        ok(label, False, "no error")
    except Die as e:
        ok(label, want in str(e), str(e))


def jload(path):
    with open(path) as f:
        return json.load(f)


def one_json(text):
    """The whole stdout is exactly one JSON object (10: json.loads of all of it)."""
    try:
        obj = json.loads(text)
    except ValueError as e:
        return None, "not one JSON document: %s: %r" % (e, text[:300])
    return obj, "" if isinstance(obj, dict) and text.count("\n") == 1 else "not one line holding an object"


BAD_KEY = jsonout.SECRET_KEY


def bad_keys(obj, path="$"):
    """Keys anywhere in obj that look like credentials (10: OS_*, *_RC, PASSWORD, SECRET)."""
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if BAD_KEY.search(k):
                found.append(path + "." + k)
            found += bad_keys(v, path + "." + k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            found += bad_keys(v, "%s[%d]" % (path, i))
    return found


def sg_world():
    """The source VM in a server group (copied as <AZ>-web-anti by default)."""
    w = world()
    w["src"]["server_groups"]["sg-src-anti"] = {"name": "web-anti", "policy": "anti-affinity", "rules": {},
                                                "members": ["srv-src-app01"]}
    return w


# ================================ 1. options.json + effective ================================
r = app(world(), [("migrate", "app01", PLAIN + ["--mode", "live", "--az", "AZ1"])],
        env={"env": {"MIG_DEFAULT_OPTS": "--verify none --max-passes 3"}})
oj = os.path.join(r.state, "app01", "options.json")
doc = jload(oj) if os.path.exists(oj) else {}
eff = doc.get("effective", {})
ok("1.1 effective: typed --mode/--az are cli, MIG_DEFAULT_OPTS --verify/--max-passes, --ip default, volume_az from az",
   r.rc == [0] and eff.get("mode") == {"value": "live", "from": "cli"} and eff.get("az") == {"value": "AZ1", "from": "cli"}
   and eff.get("verify") == {"value": "none", "from": "MIG_DEFAULT_OPTS"}
   and eff.get("max_passes") == {"value": 3, "from": "MIG_DEFAULT_OPTS"}
   and eff.get("ip") == {"value": "same", "from": "default"} and eff.get("volume_az") == {"value": "AZ1", "from": "az"}
   and eff.get("no_copy_sg") == {"value": True, "from": "cli"}
   and eff.get("pool") == {"value": "mig-volumes", "from": "default", "detail": "RBD_POOL"},
   (r.rc, eff, r.text[-500:]))
ok("1.1 every 'from' is one of the six origins",
   eff and all(v.get("from") in ("cli", "default", "MIG_DEFAULT_OPTS", "conf", "az", "source") for v in eff.values()),
   sorted(set(v.get("from") for v in eff.values())))
r = app(world(), [("migrate", "app01", PLAIN)],
        env={"env": {"MIG_AZ": "AZ1", "AZ_TYPE_MAP": "AZ1:nvme", "RBD_POOL_MAP": "nvme:volumes-nvme",
                     "MIG_VOLUME_AZ": "nova"}})
eff = jload(os.path.join(r.state, "app01", "options.json"))["effective"] if r.rc == [0] else {}
ok("1.1 conf: MIG_AZ, AZ_TYPE_MAP (detail names the zone), RBD_POOL_MAP, MIG_VOLUME_AZ",
   eff.get("az") == {"value": "AZ1", "from": "conf", "detail": "MIG_AZ"}
   and eff.get("volume_type") == {"value": "nvme", "from": "conf", "detail": "AZ_TYPE_MAP (AZ1)"}
   and eff.get("pool") == {"value": "volumes-nvme", "from": "conf", "detail": "RBD_POOL_MAP"}
   and eff.get("volume_az") == {"value": "nova", "from": "conf", "detail": "MIG_VOLUME_AZ"}, (r.rc, eff, r.text[-400:]))
r = app(sg_world(), [("migrate", "app01", ["--az", "AZ1"])])
eff = jload(os.path.join(r.state, "app01", "options.json"))["effective"] if r.rc == [0] else {}
ok("1.1 source: flavor, security group copies (create), server group copy with its policy",
   eff.get("flavor") == {"value": "m1.small", "from": "source"}
   and eff.get("security_groups", {}).get("from") == "source"
   and {"src": "web", "dst": "AZ1-web", "action": "create"} in eff["security_groups"]["value"]
   and eff.get("server_group") == {"value": {"src": "web-anti", "dst": "AZ1-web-anti", "policy": "anti-affinity"},
                                   "from": "source"}, (r.rc, eff, r.text[-400:]))
fmap = os.path.join(inproc.WORK, "flavor.map")
with open(fmap, "w") as f:
    f.write("m1.small c2.small\n")
w = world()
del w["dst"]["flavors"]["m1.small"]
r = app(w, [("migrate", "app01", PLAIN + ["--flavor-fallback"])], env={"env": {"FLAVOR_MAP_FILE": fmap}})
eff = jload(os.path.join(r.state, "app01", "options.json"))["effective"] if r.rc == [0] else {}
ok("1.1 --flavor-fallback: flavor from conf FLAVOR_MAP_FILE, the flag itself cli",
   eff.get("flavor") == {"value": "c2.small", "from": "conf", "detail": "FLAVOR_MAP_FILE"}
   and eff.get("flavor_fallback") == {"value": True, "from": "cli"}
   and eff.get("security_groups", {}).get("from") == "cli", (r.rc, eff, r.text[-400:]))
r = app(world(), [("migrate", "app01", ["--no-copy-secgroup", "--server-group", "x"]),
                  ], env={"env": {}})
ok("1.1 a preflight FAIL writes no options.json (the 'options' line is not written either)",
   r.rc == [1] and not os.path.exists(os.path.join(r.state, "app01", "options.json"))
   and not os.path.exists(os.path.join(r.state, "app01", "options")), (r.rc, r.text[-300:]))

r = app(world(), [("migrate", "app01", PLAIN + ["--az", "AZ1"]), ROLL])
retired = [d for d in os.listdir(r.state) if d.startswith("app01.rolled-back.")]
rdir = os.path.join(r.state, retired[0]) if retired else ""
doc = jload(os.path.join(rdir, "options.json")) if rdir else {}
ok("1.2 options.json: command as typed (+ --yes), conf whitelist, version, ident, server_id, kind, started",
   doc.get("command") == ["migrate", "migrate", "app01"] + PLAIN + ["--az", "AZ1", "--yes"]
   and set(doc.get("conf", {})) == {"path", "found"} | set(record.CONF_KEYS)
   and doc.get("tool_version") == migratetool.__version__ and doc.get("ident") == "app01"
   and doc.get("server_id") == "srv-src-app01" and doc.get("kind") == "vm"
   and doc.get("started", "").endswith("Z") and doc["conf"]["found"] is False, doc)
with open(os.path.join(rdir, "options")) if rdir else io.StringIO() as f:
    old = f.read()
ok("1.2 the old 'options' line is unchanged", old.startswith("MODE=cold IPMODE=same IPADDR= VOLUME_TYPE= POOL=mig-volumes")
   and old.rstrip("\n").endswith(" AZ=AZ1 VOLUME_AZ=AZ1") and "\n" == old[-1] and old.count("\n") == 1, old)
rb = jload(os.path.join(rdir, "rollback.json")) if rdir and os.path.exists(os.path.join(rdir, "rollback.json")) else {}
ok("1.2 rollback.json in the retired directory (command, started, ended, rc), options.json kept beside it",
   rb.get("command") == ["migrate", "rollback", "app01"] and rb.get("rc") == 0 and rb.get("ended", "").endswith("Z")
   and rb.get("ident") == "app01" and os.path.exists(os.path.join(rdir, "options.json")), (retired, rb))
w = world()
w["src"]["volumes"]["vol-src-data"]["status"] = "available"
r = app(w, [("volume", "data", ["--volume-type", "nvme"])], env={"env": {"RBD_POOL_MAP": "nvme:volumes-nvme"}})
vdir = os.path.join(r.state, "vol-vol-src-data")
doc = jload(os.path.join(vdir, "options.json")) if os.path.exists(os.path.join(vdir, "options.json")) else {}
ok("1.2 migrate volume: options.json kind vol, volume keys only",
   r.rc == [0] and doc.get("kind") == "vol" and doc.get("volume_id") == "vol-src-data"
   and doc.get("command") == ["migrate", "volume", "data", "--volume-type", "nvme"]
   and doc["effective"]["volume_type"] == {"value": "nvme", "from": "cli"} and "mode" not in doc["effective"]
   and doc["effective"]["pool"] == {"value": "volumes-nvme", "from": "conf", "detail": "RBD_POOL_MAP"}
   and "flavor" not in doc["effective"], (r.rc, doc, r.text[-300:]))

# ================================ 2. --json for plan and doctor ================================
r = app(world(), [("cli", ["plan", "app01", "--json"] + PLAIN)])
obj, why = one_json(r.out)
row = (obj or {}).get("rows", [{}])[0]
ok("2.2 plan --json: stdout is exactly one JSON object, rc 0, the tables and preflight text on stderr",
   obj is not None and not why and r.rc == [0] and "Selected:" in r.err and "Plan:" in r.err
   and "Destination preflight" in r.err and "Selected:" not in r.out, (why, r.rc, r.out[:300], r.err[-300:]))
ok("2.2 plan --json row: result, project, env, command, effective, checks, counts, log",
   row.get("result") == "PLAN-OK" and row.get("kind") == "vm" and row.get("ident") == "app01"
   and row.get("project") == {"src": "admin", "dst": "admin"}
   and row.get("env") == {"SRC_PROJECT": "admin", "DST_PROJECT": "admin"}
   and row.get("command") == ["migrate", "migrate", "app01"] + PLAIN + ["--yes"]
   and (row.get("effective") or {}).get("no_copy_sg") == {"value": True, "from": "cli"}
   and {"level": "OK", "text": "flavor m1.small"} in (row.get("checks") or [])
   and row.get("counts", {}).get("fail") == 0 and row["counts"]["ok"] == sum(1 for c in row["checks"] if c["level"] == "OK")
   and row.get("log", "").endswith("/plan-srv-src-app01.log") and obj.get("totals") == {"PLAN-OK": 1}
   and obj.get("verb") == "plan" and obj.get("rc") == 0 and obj.get("tool_version") == migratetool.__version__,
   (obj, r.err[-400:]))
ok("2.2 plan --json: no credential-like key at any depth", obj is not None and not bad_keys(obj), bad_keys(obj or {}))
ok("2.2 plan --json: the child got the marker, its log keeps the '@@' line, the parent's checks do not",
   r.children and r.children[0][:3] == ["plan", "app01", "--json"]
   and any(l.startswith("@@ {\"effective\":") for l in open(row.get("log") or os.devnull).read().splitlines())
   and not any(c["text"].startswith("@@") for c in row.get("checks") or []), (r.children, row.get("log")))
r = app(world(), [("cli", ["plan", "--vm", "app01,ghost", "--json", "--ip", "10.9.9.9"] + PLAIN)])
obj, why = one_json(r.out)
rows = {x["name"]: x for x in (obj or {}).get("rows", [])}
ok("2.2 plan --json PLAN-FAIL + SKIP: rc 1, totals, info carries the FAIL line, SKIP row has null command/effective/checks",
   obj is not None and r.rc == [1] and obj.get("rc") == 1 and obj.get("totals") == {"PLAN-FAIL": 1, "SKIP": 1}
   and rows["app01"]["result"] == "PLAN-FAIL" and "IP 10.9.9.9 is not in subnet" in rows["app01"]["info"]
   and rows["app01"]["counts"]["fail"] >= 1 and rows["app01"]["effective"]["ip"] == {"value": "10.9.9.9", "from": "cli"}
   and rows["ghost"]["result"] == "SKIP(not-found)" and rows["ghost"]["command"] is None
   and rows["ghost"]["effective"] is None and rows["ghost"]["checks"] is None, (why, r.rc, obj))
w = world()
w["src"]["volumes"]["vol-src-data"]["status"] = "available"
r = app(w, [("cli", ["plan", "volume", "--project", "admin", "--available", "--json", "--volume-type", "nvme",
                     "--mode", "cold"])])
obj, why = one_json(r.out)
row = (obj or {}).get("rows", [{}])[0]
ok("2.2 plan volume --json: kind vol, READY, the command 'migrate volume' would run",
   obj is not None and obj.get("verb") == "plan volume" and row.get("kind") == "vol" and row.get("result") == "READY"
   and row.get("command") == ["migrate", "volume", "vol-src-data", "--volume-type", "nvme", "--mode", "cold"]
   and row.get("effective") is None and r.rc == [0], (why, r.rc, obj, r.err[-300:]))
r = app(world(), [("cli", ["plan", "app01"] + PLAIN)])
ok("2.1 without --json: the plan prints its tables on stdout, no '@@' line, the child gets no marker",
   "Selected:" in r.out and "Plan:" in r.out and "@@" not in r.text and r.children[0][:3] == ["plan", "app01", PLAIN[0]],
   (r.children, r.out[-300:]))
r = app(world(), [("cli", ["doctor", "--json"])])
obj, why = one_json(r.out)
ok("2.3 doctor --json: one object, its checks and counts, passed, the text on stderr",
   obj is not None and not why and r.rc == [0] and obj.get("passed") is True
   and {"level": "OK", "text": "destination: admin role"} in obj.get("checks", [])
   and obj["counts"]["fail"] == 0 and obj["counts"]["ok"] == len([c for c in obj["checks"] if c["level"] == "OK"])
   and "Doctor passed." in r.err and not bad_keys(obj), (why, r.rc, r.out[:400], r.err[-300:]))
inproc.MISSING.add("qemu-img")
r = app(world(), [("cli", ["doctor", "--json"])])
inproc.MISSING.discard("qemu-img")
obj, why = one_json(r.out)
ok("2.3 doctor --json with a FAIL: passed false, rc 1, the FAIL in checks",
   obj is not None and r.rc == [1] and obj.get("passed") is False and obj["counts"]["fail"] >= 1
   and {"level": "FAIL", "text": "qemu-img: not found on PATH"} in obj["checks"], (why, r.rc, obj))
r = app(world(), [("cli", ["doctor", "--bogus"]), ("cli", ["migrate", "app01", "--json"])])
ok("2.1 --json is refused where it does not exist (doctor --bogus as before, migrate --json unknown)",
   r.rc == [1, 1] and "ERROR: doctor takes --json and --write-check only (got --bogus)" in r.errs[0] and "ERROR: unknown option --json" in r.errs[1]
   and r.out == "", r.errs)

# ================================ 3. migrate list ================================
def eph_world():
    w = world()
    w["src"]["servers"]["srv-src-eph01"] = {
        "name": "eph01", "status": "SHUTOFF", "locked": False, "locked_reason": None, "image": "cirros (img-base)",
        "flavor": {"original_name": "m1.small", "name": "m1.small", "vcpus": 2, "ram": 2048, "disk": 1},
        "key_name": "k1", "properties": {}, "description": None, "ports": ["port-src-eph"], "volumes": [],
        "host": "cmp2", "availability_zone": "zone-b"}
    w["src"]["ports"]["port-src-eph"] = {"name": "", "network_id": "net-src-1", "mac_address": "fa:16:3e:aa:bb:60",
                                         "fixed_ips": [{"subnet_id": "sub-src-1", "ip_address": "198.51.100.60"}],
                                         "security_group_ids": ["sg-src-default"], "device_id": "srv-src-eph01",
                                         "port_security_enabled": True, "allowed_address_pairs": []}
    return w


r = app(world(), [("cli", ["list", "--host", "cmp1", "--json"])])
obj, why = one_json(r.out)
row = (obj or {}).get("rows", [{}])[0]
osc = [c for c in r.calls if c.startswith(("src\t", "dst\t"))]
ok("3 list --json --host: one object, the VM with host, az, flavor, boot, networks, volumes (device), no state",
   obj is not None and not why and r.rc == [0] and obj.get("kind") == "vm" and row.get("id") == "srv-src-app01"
   and row.get("host") == "cmp1" and row.get("az") == "nova" and row.get("flavor") == "m1.small"
   and row.get("boot") == "bfv" and row.get("image") == "" and row.get("result") == "selected"
   and row.get("networks") == [{"network": "vlan120", "ips": ["198.51.100.50"]}]
   and row.get("volumes") == [{"id": "vol-src-root", "name": "", "size": 10, "device": "/dev/vda"},
                              {"id": "vol-src-data", "name": "data", "size": 2, "device": "/dev/vdb"}]
   and row.get("state") is None and row.get("project") == {"src": "admin", "dst": "admin"}, (why, obj, r.err[-300:]))
ok("3 list --json: 3 openstack commands for a host (server list, project show, volume list), no server/volume show",
   len(osc) == 3 and osc[0].startswith("src\tserver list --all-projects --long --host cmp1")
   and "src\tproject show psrc -f value -c name" in osc and "src\tvolume list --all-projects --long -f json" in osc
   and not [c for c in osc if " show " in c and not c.startswith("src\tproject show")], osc)
ok("3 list: no log directory, nothing under STATE_ROOT", not os.path.exists(r.log) and os.listdir(r.state) == [],
   (os.path.exists(r.log), os.listdir(r.state)))
r = app(world(), [("migrate", "app01", PLAIN), ("cli", ["list", "app01", "--json"])])
obj, why = one_json(r.outs[1])
row = (obj or {}).get("rows", [{}])[0]
st = row.get("state") or {}
ok("3 list --json after a migrate: SKIP(state-exists) and the live state with its options.json",
   obj is not None and row.get("result") == "SKIP(state-exists)" and st.get("live") is True
   and st.get("dir", "").endswith("/app01") and (st.get("options") or {}).get("kind") == "vm"
   and st.get("started") == st["options"]["started"], (why, row))
r = app(eph_world(), [("cli", ["list", "eph01", "--json"])])
obj, why = one_json(r.out)
row = (obj or {}).get("rows", [{}])[0]
ok("3 list --json: an ephemeral VM reads boot ephemeral with its image name, host and zone of its own",
   obj is not None and row.get("boot") == "ephemeral" and row.get("image") == "cirros" and row.get("host") == "cmp2"
   and row.get("az") == "zone-b" and row.get("volumes") == [] and row.get("status") == "SHUTOFF", (why, row))
w = world()
w["src"]["short_list"] = True
r = app(w, [("cli", ["list", "app01", "--json"])])
obj, why = one_json(r.out)
row = (obj or {}).get("rows", [{}])[0]
ok("3 list --json: a listing without those columns asks 'server show' for that VM only",
   obj is not None and [c for c in r.calls if c.startswith("src\tserver show")] == ["src\tserver show srv-src-app01 -f json"]
   and row.get("flavor") == "m1.small" and row.get("locked") is False, (r.calls, row))
w = world()
w["src"]["volumes"]["vol-src-free"] = {"name": "spare", "status": "available", "size": 5, "bootable": "false"}
w["src"]["volumes"]["vol-src-bad"] = {"name": "bad", "status": "error", "size": 1}
r = app(w, [("cli", ["list", "--project", "admin", "--volumes", "--json"])])
obj, why = one_json(r.out)
rows = {x["id"]: x for x in (obj or {}).get("rows", [])}
ok("3 list --volumes --project (all): attached volumes with their VM first, then available ones, statuses as plan",
   obj is not None and obj.get("kind") == "vol" and list(rows)[:2] == ["vol-src-root", "vol-src-data"]
   and rows["vol-src-root"]["attached_to"] == {"server_id": "srv-src-app01", "server_name": "app01", "device": "/dev/vda"}
   and rows["vol-src-free"]["result"] == "selected" and rows["vol-src-free"]["size"] == 5
   and rows["vol-src-free"]["attached_to"] is None and rows["vol-src-bad"]["result"] == "SKIP(status)"
   and rows["vol-src-data"]["type"] == "__DEFAULT__", (why, obj, r.err[-300:]))
r = app(world(), [("cli", ["list", "--host", "cmp1"])])
ok("3 list without --json: the Selected table on stdout",
   r.rc == [0] and "Selected:" in r.out and "srv-src-app01" in r.out and not r.out.lstrip().startswith("{"), r.out)

# ================================ 4. migrate inventory ================================
w = world()
w["dst"]["azs"]["compute"] = ["internal", "AZ1", "nova"]
r = app(w, [("cli", ["inventory", "--json"])])
obj, why = one_json(r.out)
src, dst = (obj or {}).get("source", {}), (obj or {}).get("destination", {})
ok("4 inventory --json: one object, hosts and projects of both clouds, the destination's zones, flavors, types",
   obj is not None and not why and r.rc == [0]
   and src.get("hosts") == [{"host": "cmp1", "zone": "nova", "state": "up", "status": "enabled"},
                            {"host": "cmp2", "zone": "zone-b", "state": "up", "status": "enabled"}]
   and dst.get("hosts", [{}])[0] == {"host": "dst-cmp1", "zone": "AZ1", "state": "up", "status": "enabled"}
   and src.get("projects") == [{"id": "psrc", "name": "admin"}] and src.get("scope_project") == "admin"
   and src.get("auth_type") == "password"
   and dst.get("zones") == {"compute": ["AZ1", "nova"], "volume": ["nova", "AZ1"]}
   and {"name": "c2.small", "vcpus": 1, "ram": 2048, "disk": 0} in dst.get("flavors", [])
   and dst.get("volume_types") == ["__DEFAULT__", "nvme"]
   and set(obj.get("conf", {})) == {"path", "found"} | set(record.CONF_KEYS) and not bad_keys(obj),
   (why, obj, r.err[-300:]))
ok("4 inventory: read-only commands only, no per-host count without --count",
   all(c.split("\t", 1)[1].split()[1] in ("list", "issue") or c.split("\t", 1)[1].startswith(
       ("compute service list", "availability zone list", "volume type list")) for c in r.calls)
   and not [c for c in r.calls if "--host" in c] and "vms" not in src["hosts"][0], r.calls)
w = world()
w["src"]["servers"]["srv-src-b"] = dict(w["src"]["servers"]["srv-src-app01"], name="b", host="cmp2", ports=[], volumes=[])
w["src"]["servers"]["srv-src-c"] = dict(w["src"]["servers"]["srv-src-app01"], name="c", host="cmp2", ports=[], volumes=[])
r = app(w, [("cli", ["inventory", "--json", "--count"]), ("cli", ["inventory", "--json", "--count", "--host", "cmp2"])])
o1, _ = one_json(r.outs[0])
o2, _ = one_json(r.outs[1])
ok("4 inventory --count: VMs per source host (one listing per host), --host narrows the count",
   o1 is not None and [h.get("vms") for h in o1["source"]["hosts"]] == [1, 2]
   and o2 is not None and [h.get("vms") for h in o2["source"]["hosts"]] == [None, 2]
   and "src\tserver list --all-projects --host cmp2 -f value -c ID" in r.calls, (o1, o2))
w = world()
w["dst"]["project_list_fail"] = "You are not authorized to perform the requested action: identity:list_projects. (HTTP 403)"
r = app(w, [("cli", ["inventory", "--json"])])
obj, why = one_json(r.out)
ok("4 a cloud that refuses 'project list': projects [] and projects_error, the rest still there",
   obj is not None and r.rc == [0] and obj["destination"]["projects"] == []
   and "HTTP 403" in obj["destination"]["projects_error"] and obj["destination"]["flavors"], (why, obj))
w = world()
w["src"]["fail"] = {"token issue": "The request you have made requires authentication. (HTTP 401)"}
r = app(w, [("cli", ["inventory", "--json"])])
ok("4 no source token: stop like doctor would, stdout empty, rc 1",
   r.rc == [1] and r.out == "" and "ERROR: cannot get a source token (The request you have made requires "
                                   "authentication. (HTTP 401)): run 'migrate doctor' first" in r.err, (r.rc, r.out, r.err))
r = app(world(), [("cli", ["inventory"])])
ok("4 inventory without --json: a text summary", r.rc == [0] and "source (password, project admin):" in r.out
   and "  zones    compute nova AZ1; volume nova AZ1" in r.out and not r.out.startswith("{"), r.out)

# ================================ 5. migrate status ================================
MARK = []


def mark(_wf, _root):
    """Remembers how many calls were made so far (status must add none)."""
    with open(os.environ["HARNESS_CALLS"]) as f:
        MARK.append(len(f.read().splitlines()))


r = app(world(), [("migrate", "app01", PLAIN), ("plan", "app01", PLAIN + ["--volumes-only"]), mark,
                  ("cli", ["status", "--json"])])
obj, why = one_json(r.outs[2])
live = (obj or {}).get("live", [{}])[0]
ok("5 status --json after a migrate: the live entry with options.json, files, t_stop, dst_server_id, volume_map",
   obj is not None and not why and live.get("ident") == "app01" and live.get("kind") == "vm" and live.get("live") is True
   and (live.get("options") or {}).get("command", [None])[0] == "migrate"
   and {"server.json", "volume-map", "dst-server-id", "t-stop", "options", "options.json"} <= set(live.get("files", []))
   and live.get("t_stop", "").endswith("Z") and live.get("dst_server_id") == "srv-dst-0001"
   and live.get("volume_map") == [["vol-src-root", "vol-dst-0001"], ["vol-src-data", "vol-dst-0002"]]
   and obj.get("rolled_back") == [] and isinstance(obj.get("running"), list), (why, obj))
ok("5 status asks no cloud: not one openstack command", MARK and len(r.calls) == MARK[-1], r.calls[MARK[-1]:] if MARK else "")
r = app(world(), [("cli", ["plan", "app01"] + PLAIN), ("migrate", "app01", PLAIN), ROLL,
                  ("cli", ["status", "--json"])])
obj, why = one_json(r.outs[3])
rb = (obj or {}).get("rolled_back", [{}])[0]
ok("5 status after a rollback: rolled_back with retired time, options.json and rollback.json; batches listed",
   obj is not None and rb.get("ident") == "app01" and rb.get("retired", "").endswith("Z")
   and (rb.get("options") or {}).get("kind") == "vm" and (rb.get("rollback") or {}).get("rc") == 0
   and obj.get("live") == [] and obj.get("rolled_back_total") == 1 and len(obj.get("batches", [])) == 1
   and any(x.startswith("plan-") for x in obj["batches"][0]["logs"]), (why, obj))


def bash_state(_wf, root):
    """A state directory written by 7.2 or the bash build: no options.json."""
    d = os.path.join(root, "old-vm")
    os.makedirs(d)
    for name, text in (("server.json", '{"id": "x"}'), ("volume-map", "a b\n"), ("options", "MODE=cold\n")):
        with open(os.path.join(d, name), "w") as f:
            f.write(text)
    os.makedirs(os.path.join(root, "planned"))
    with open(os.path.join(root, "planned", "server.json"), "w") as f:
        f.write("{}")


r = app(world(), [bash_state, ("cli", ["status", "--json"])])
obj, why = one_json(r.out)
live = {e["ident"]: e for e in (obj or {}).get("live", [])}
plan_only = {e["ident"]: e for e in (obj or {}).get("plan_only", [])}
ok("5 status: a state without options.json (7.2, bash) reads options null; a plan-only directory is plan_only",
   obj is not None and live.get("old-vm", {}).get("options", 1) is None and live["old-vm"]["volume_map"] == [["a", "b"]]
   and plan_only.get("planned", {}).get("live") is False and "error" not in live["old-vm"], (why, obj))
fake_proc = os.path.join(inproc.WORK, "proc")
for pid, argv in (("101", ["/opt/mig-venv/bin/python", "-I", "-m", "migratetool.single", "migrate", "web-01", "--yes"]),
                  ("102", ["/usr/sbin/sshd", "-D"]), ("104", ["sudo", "migrate", "status"]),
                  ("103", ["/opt/mig-venv/bin/python", "/opt/mig-venv/bin/migrate", "migrate", "--host", "cmp1", "--yes"])):
    os.makedirs(os.path.join(fake_proc, pid))
    with open(os.path.join(fake_proc, pid, "cmdline"), "wb") as f:
        f.write(b"\0".join(a.encode() for a in argv) + b"\0")
    with open(os.path.join(fake_proc, pid, "environ"), "wb") as f:
        f.write(b"MIG_FAKE_ENVIRON=never-shown\0")  # a process environment: status must never read it
from migratetool import status as status_mod  # noqa: E402
status_mod.PROC = fake_proc
r = app(world(), [("cli", ["status", "--json"])])
status_mod.PROC = "/nonexistent-proc"
r2 = app(world(), [("cli", ["status", "--json"])])
status_mod.PROC = "/proc"
obj, why = one_json(r.out)
obj2, _ = one_json(r2.out)
ok("5 running: migratetool processes only (module or entry point, not 'sudo migrate'), their cmdline, never their environ; "
   "no /proc: []",
   obj is not None and [p["pid"] for p in obj["running"]] == [101, 103]
   and obj["running"][0]["cmdline"][3] == "migratetool.single" and "never-shown" not in r.out
   and obj2 is not None and obj2["running"] == [], (why, obj and obj["running"]))

# ================================ 6. --dst-host ================================
r = app(world(), [("migrate", "app01", PLAIN + ["--az", "AZ1", "--dst-host", "dst-cmp1"])])
sc = [c for c in r.calls if "server create" in c]
srv = r.world["dst"]["servers"].get("srv-dst-0001", {})
eff = jload(os.path.join(r.state, "app01", "options.json"))["effective"] if r.rc == [0] else {}
with open(os.path.join(r.state, "app01", "options")) as f:
    optline = f.read()
ok("6 --dst-host: preflight OK line, server create --os-compute-api-version 2.74 ... --host, on that host",
   r.rc == [0] and "  OK    destination host dst-cmp1 (zone AZ1, up, enabled)" in r.out
   and sc and sc[0].startswith("dst\t--os-compute-api-version 2.74 server create app01 ")
   and " --availability-zone AZ1 " in sc[0] and " --host dst-cmp1 " in sc[0]
   and srv.get("host") == "dst-cmp1" and srv.get("api_version") == "2.74", (r.rc, sc, r.out[-500:]))
ok("6 --dst-host recorded: effective dst_host cli, 'options' line ends with DST_HOST=",
   eff.get("dst_host") == {"value": "dst-cmp1", "from": "cli"} and optline.rstrip("\n").endswith(" VOLUME_AZ=AZ1 DST_HOST=dst-cmp1"),
   (eff.get("dst_host"), optline))
r = app(world(), [("migrate", "app01", PLAIN + ["--dst-host", "dst-cmp1"])])
sc = [c for c in r.calls if "server create" in c]
vc = [c for c in r.calls if c.startswith("dst\tvolume create")]
eff = jload(os.path.join(r.state, "app01", "options.json"))["effective"] if r.rc == [0] else {}
ok("6 --dst-host without --az: the host's zone becomes the AZ of the server and the volumes (INFO line)",
   r.rc == [0] and "  INFO  availability zone AZ1 from host dst-cmp1" in r.out and "  OK    volume AZ AZ1" in r.out
   and " --availability-zone AZ1 " in sc[0] and all("--availability-zone AZ1" in c for c in vc)
   and eff.get("az") == {"value": "AZ1", "from": "cli", "detail": "zone of --dst-host dst-cmp1"}
   and eff.get("volume_az") == {"value": "AZ1", "from": "az"}, (r.rc, sc, vc, eff.get("az"), r.out[-500:]))
r = app(world(), [("migrate", "app01", PLAIN)])
ok("6 without --dst-host: server create unchanged (no API version, no --host), 'options' without DST_HOST",
   not [c for c in r.calls if "--os-compute-api-version" in c or " --host " in c]
   and "DST_HOST" not in open(os.path.join(r.state, "app01", "options")).read(), r.calls)
r = app(world(), [("doctor",)])
w = world()
w["dst"]["nova_max_microversion"] = "2.60"
r2 = app(w, [("doctor",)])
ok("6 doctor: destination Nova API >= 2.74 OK line; an older Nova only an INFO (exit still 0)",
   "  OK    destination Nova API 2.96 >= 2.74 (--dst-host usable)" in r.out and r.rc == [0]
   and "  INFO  destination Nova API 2.60 < 2.74: --dst-host not available" in r2.out and r2.rc == [0], (r.out, r2.out))
w = world()
w["src"]["volumes"]["vol-src-data"]["status"] = "available"
r = app(w, [("cli", ["volume", "data", "--dst-host", "dst-cmp1"]),
            ("cli", ["migrate", "app01", "--volumes-only", "--dst-host", "dst-cmp1", "--yes"] + PLAIN)])
ok("6 a lone volume refuses --dst-host; --volumes-only ignores it with an INFO",
   r.rc[0] == 1 and "--dst-host cannot apply" in r.errs[0] and r.rc[1] == 0
   and "  INFO  --dst-host dst-cmp1 ignored: --volumes-only creates no server" in r.outs[1], (r.rc, r.errs, r.outs[1][-400:]))


def no_stop(r):
    return idx(r.calls, "src\tserver stop") is None and idx(r.calls, "dst\tvolume create") is None


for label, mod, args, want in (
        ("not a nova-compute host", None, ["--dst-host", "ghost-01"], "  FAIL  destination host ghost-01 is not a nova-compute host"),
        ("down", {"state": "down"}, ["--dst-host", "dst-cmp1"],
         "  FAIL  destination host dst-cmp1 is down or disabled (state=down, status=enabled)"),
        ("disabled", {"status": "disabled"}, ["--dst-host", "dst-cmp1"],
         "  FAIL  destination host dst-cmp1 is down or disabled (state=up, status=disabled)"),
        ("in another zone than --az", None, ["--az", "AZ1", "--dst-host", "dst-cmp2"],
         "  FAIL  destination host dst-cmp2 is in zone nova, not AZ1 (--az)")):
    w = world()
    if mod:
        w["dst"]["compute_hosts"] = [dict({"host": "dst-cmp1", "zone": "AZ1"}, **mod)]
    r = app(w, [("migrate", "app01", PLAIN + args)])
    ok("SEC-12 --dst-host %s: FAIL in the preflight, the source never stopped, no destination volume" % label,
       r.rc == [1] and want in r.out and no_stop(r) and not r.world["dst"]["volumes"], (r.rc, r.out[-600:]))
w = world()
w["dst"]["roles"] = ["member"]
r = app(w, [("migrate", "app01", PLAIN + ["--dst-host", "dst-cmp1"])])
ok("SEC-12 --dst-host without the admin role at the destination: FAIL before anything stops",
   r.rc == [1] and "  FAIL  --dst-host needs the admin role at the destination (server create --host is admin only)"
   in r.out and no_stop(r), (r.rc, r.out[-500:]))
w = world()
w["dst"]["fail"] = {"compute service list": "Unable to establish connection (HTTP 503)"}
r = app(w, [("cli", ["plan", "app01", "--dst-host", "dst-cmp1"] + PLAIN)])
ok("SEC-12 the host listing fails: FAIL (never guessed OK)",
   r.rc == [1] and "FAIL  cannot list the destination nova-compute hosts for --dst-host dst-cmp1: Unable to establish "
                   "connection (HTTP 503)" in r.out, r.out[-500:])
TYPEMAP = {"env": {"AZ_TYPE_MAP": "AZ1:nvme", "RBD_POOL_MAP": "nvme:volumes-nvme"}}
r = app(world(), [("migrate", "app01", PLAIN + ["--dst-host", "dst-cmp1"])], env=TYPEMAP)
ok("SEC-12 --dst-host without --az and AZ_TYPE_MAP for the host's zone: FAIL, source never stopped",
   r.rc == [1] and "  FAIL  --dst-host dst-cmp1 is in zone AZ1, which AZ_TYPE_MAP maps to volume type nvme: pass --az "
                   "AZ1 (or --volume-type nvme)" in r.out and no_stop(r) and not r.world["dst"]["volumes"]
   and "  OK    destination host" not in r.out, (r.rc, r.out[-600:]))
r = app(world(), [("migrate", "app01", PLAIN + ["--dst-host", "dst-cmp1", "--az", "AZ1"])], env=TYPEMAP)
r2 = app(world(), [("plan", "app01", PLAIN + ["--dst-host", "dst-cmp1", "--volume-type", "nvme"])], env=TYPEMAP)
pools = sorted({v.get("pool") for v in r.world["dst"]["volumes"].values()})
ok("SEC-12 --dst-host with --az (AZ_TYPE_MAP applied at startup), or with --volume-type: passes, type and pool mapped",
   r.rc == [0] and "  OK    destination host dst-cmp1 (zone AZ1, up, enabled)" in r.out
   and all("--type nvme" in c for c in r.calls if c.startswith("dst\tvolume create")) and pools == ["volumes-nvme"]
   and r2.rc == [0] and "AZ_TYPE_MAP maps" not in r2.out, (r.rc, pools, r.out[-400:], r2.rc, r2.out[-400:]))
r = app(world(), [("migrate", "app01", PLAIN + ["--dst-host", "dst-cmp1"]),
                  ("cli", ["plan", "app01", "--dst-host", "dst-cmp1"])],
        env={"env": {"MIG_DEFAULT_OPTS": "--verify none --dst-host dst-cmp1"}})
ok("SEC-13 --dst-host in MIG_DEFAULT_OPTS: the run stops (per VM and before a selector lists anything)",
   r.rc == [1, 1] and all("ERROR: MIG_DEFAULT_OPTS: --dst-host is a decision per run, it is only taken from the command "
                          "line" in e for e in r.errs) and not r.calls, (r.rc, r.errs, r.calls))
for bad in ("a;b", "../x", "h" * 200, "-x", "a b", "a\nb"):
    r = app(world(), [("migrate", "app01", PLAIN + ["--dst-host", bad])])
    ok("SEC-5 --dst-host %r refused when the options are parsed, no cloud call" % (bad[:12] + ("..." if len(bad) > 12 else "")),
       r.rc == [1] and "ERROR: --dst-host takes a hypervisor host name" in r.err and not r.calls, (r.rc, r.err, r.calls[:3]))
w = world()
w["dst"]["create_server_fail"] = "No valid host was found. (HTTP 400)"
r = app(w, [("migrate", "app01", PLAIN + ["--dst-host", "dst-cmp1"]), ROLL])
srv = r.world["src"]["servers"]["srv-src-app01"]
ok("SEC-14 server create --host fails after the cutover: the run fails, rollback cleans up and restarts the source",
   r.rc == [4, 0] and "No valid host was found" in r.err  # 1.0.0 (C8): failed after t-stop and not r.world["dst"]["volumes"]
   and not r.world["dst"]["servers"] and srv["status"] == "ACTIVE" and not srv["locked"]
   and not [d for d in os.listdir(r.state) if d == "app01"], (r.rc, r.err[-300:], r.out[-400:]))

# ================================ 7. --progress-json ================================
from migratetool import copyvol, progress  # noqa: E402


def events(text):
    """The '@@ ' lines of an output, each one JSON object (None when one is not)."""
    got = []
    for line in text.splitlines():
        if line.startswith("@@ "):
            try:
                got.append(json.loads(line[3:]))
            except ValueError:
                got.append(None)
    return got


r = app(world(), [("cli", ["migrate", "app01", "--progress-json", "--yes"] + PLAIN)])
ev = events(r.out)
names = [e.get("event") for e in ev if e]
phases = [e["name"] for e in ev if e and e.get("event") == "phase"]
copies = [e for e in ev if e and e.get("event") == "copy"]
res = [e for e in ev if e and e.get("event") == "result"]
dt = [e for e in ev if e and e.get("event") == "downtime"]
ok("7 migrate --progress-json (cold): phases cutover, copy, verify, create, done; t_stop; copy 100% per volume",
   r.rc == [0] and None not in ev and phases == ["cutover", "copy", "verify", "verify", "create", "done"]
   and [e["volume"] for e in copies if e["pct"] == 100] == ["vol-src-root", "vol-src-data"]
   and names.count("t_stop") == 1 and [e for e in ev if e.get("event") == "t_stop"][0]["at"].endswith("Z"),
   (r.rc, ev, r.err[-300:]))
ok("7 downtime and result events: the D3 numbers, rc 0 and the destination server",
   len(dt) == 1 and set(dt[0]) == {"event", "total", "copy", "verify", "control_plane"}
   and res == [{"event": "result", "rc": 0, "dst_server_id": "srv-dst-0001"}] and names[-1] == "result", (dt, res))
ok("7 the child got the flag, the options it parsed did not (options.json command has no --progress-json)",
   r.children[-1][:3] == ["migrate", "app01", "--progress-json"]
   and "--progress-json" not in jload(os.path.join(r.state, "app01", "options.json"))["command"], r.children)
w = world()
w["sync"] = {"vol-src-root": [{"changed": 40, "chunks": 2560, "written_bytes": 167772160, "zero_chunks": 0, "seconds": 2.5},
                              {"changed": 3, "chunks": 2560, "written_bytes": 12582912, "zero_chunks": 0, "seconds": 0.4}]}
r = app(w, [("cli", ["migrate", "app01", "--mode", "live", "--progress-json", "--yes"] + PLAIN)])
ev = [e for e in events(r.out) if e]
bs = [e for e in ev if e["event"] == "blocksync"]
ps = [e for e in ev if e["event"] == "pass"]
ok("7 live: presync phase first, blocksync events from blocksync's own stderr line, pass events per pass",
   r.rc == [0] and [e["name"] for e in ev if e["event"] == "phase"][0] == "presync"
   and bs and bs[0] == {"event": "blocksync", "volume": "vol-src-root", "pass": 1, "pct": 50, "changed_mib": 4}
   and ps[0] == {"event": "pass", "volume": "vol-src-root", "n": 1, "chunks": 40, "mb": 160, "secs": 2.5}
   and [e["n"] for e in ps if e["volume"] == "vol-src-root"][-1] == "final"
   and "[blocksync] 50% (1.0/2.0 GiB), changed 4 MiB" in r.out, (r.rc, ev[:8], r.err[-300:]))
r = app(world(), [("cli", ["migrate", "app01", "--yes"] + PLAIN), ("cli", ["migrate", "app01", "--mode", "live", "--yes"]
                                                                   + PLAIN)])
ok("7 without --progress-json: no '@@' line at all, the child gets no flag (cold and live)",
   r.rc == [0, 1] and "@@" not in r.text and all("--progress-json" not in c for c in r.children), (r.rc, r.children))
w = world()
w["src"]["volumes"]["vol-src-data"]["status"] = "available"
r = app(w, [("cli", ["volume", "data", "--progress-json"]), ("cli", ["plan", "app01", "--progress-json"])])
ev = [e for e in events(r.outs[0]) if e]
ok("7 lone volume --progress-json: copy phase, 100%, done, result rc 0; plan refuses the flag",
   r.rc == [0, 1] and [e.get("name") for e in ev if e["event"] == "phase"] == ["copy", "verify", "done"]
   and ev[-1] == {"event": "result", "rc": 0} and "ERROR: unknown option --progress-json" in r.errs[1], (r.rc, ev, r.errs))
w = world()
w["dst"]["create_server_fail"] = "No valid host was found. (HTTP 400)"
r = app(w, [("cli", ["migrate", "app01", "--progress-json", "--yes"] + PLAIN)])
ev = [e for e in events(r.out) if e]
ok("7 a failing run still closes with a result event (rc 4 after t-stop, C8; the destination server null)",
   r.rc == [4] and ev[-1] == {"event": "result", "rc": 4, "dst_server_id": None}
   and "done" not in [e.get("name") for e in ev], (r.rc, ev[-3:]))

# SEC-17: one copy/blocksync line per second each, the others unlimited.
TICK = [0.0]
progress.clock = lambda: TICK[0]
buf = io.StringIO()
with redirect_stdout(buf):
    progress.enable()
    for i in range(1000):
        TICK[0] = i * 0.005  # 1000 progress lines in 5 seconds
        progress.emit("copy", volume="v", pct=i // 10, mbps=100)
        progress.emit("blocksync", volume="v", pct=i // 10, changed_mib=1, **{"pass": 1})
    for i in range(20):
        progress.emit("pass", volume="v", n=i, chunks=1, mb=1, secs=0.1)
    progress.emit("copy", force=True, volume="v", pct=100, mbps=100)
    progress.reset()
progress.clock = __import__("time").monotonic
ev = events(buf.getvalue())
ok("SEC-17 1000 copy + 1000 blocksync lines in 5 s: at most 6 of each; pass events unlimited; the closing 100% kept",
   0 < sum(1 for e in ev if e["event"] == "copy" and e["pct"] < 100) <= 6
   and 0 < sum(1 for e in ev if e["event"] == "blocksync") <= 6 and sum(1 for e in ev if e["event"] == "pass") == 20
   and ev[-1] == {"event": "copy", "volume": "v", "pct": 100, "mbps": 100}, [e["event"] for e in ev])
w = world()
w["bs_lines"] = ["[blocksync] %d%% (0.%d/1.0 GiB), changed %d MiB" % (p, p // 10, p // 10) for p in (5, 10, 50, 95, 100)]
r = app(w, [("cli", ["migrate", "app01", "--mode", "live", "--progress-json", "--yes"] + PLAIN)])
ev = [e for e in events(r.out) if e and e["event"] == "blocksync"]
by_pass = {}
for e in ev:
    by_pass.setdefault((e["volume"], e["pass"]), []).append(e["pct"])
ok("SEC-17 / 7 blocksync: lines within a second are limited, yet every pass (final too) ends with its 100% line",
   r.rc == [0] and len(by_pass) == 6 and all(p[-1] == 100 for p in by_pass.values())
   and sum(len(p) for p in by_pass.values()) == 7, (r.rc, by_pass))


class Ticks:
    """Event.wait of the copy watcher: three ticks, then stop."""
    def __init__(self):
        self.n = 0

    def wait(self, _secs):
        self.n += 1
        return self.n > 3

    def set(self):
        pass


stat_file = os.path.join(inproc.WORK, "stat")
counter = [0]


def fake_read(_path):
    counter[0] += 2097152  # sectors: 1 GiB more at every read
    return counter[0]


watcher = copyvol.Watcher("/dev/fake", "4", "copy", "vol-x")
watcher.stop_ev, watcher._read = Ticks(), fake_read
buf = io.StringIO()
TICK[0] = 0.0
progress.clock = lambda: TICK.__setitem__(0, TICK[0] + 15) or TICK[0]  # the watcher ticks every 15 s
with redirect_stdout(buf):
    progress.enable()
    watcher._loop(stat_file, 0)
    progress.reset()
progress.clock = __import__("time").monotonic
ev = events(buf.getvalue())
ok("7 the copy watcher's tick is a copy event of that volume (pct, mbps) next to its log line",
   [e["pct"] for e in ev] == [25, 50, 75] and all(e["volume"] == "vol-x" and e["event"] == "copy" for e in ev)
   and "] copy 25% (1.0/4 GiB" in buf.getvalue(), buf.getvalue())

# ================================ 8. D3b: an ephemeral root's copy time ================================
import re  # noqa: E402
from migratetool import ephemeral  # noqa: E402


class SlowClock:
    """time for ephemeral.py: every monotonic() read is 7 s later (the snapshot, download and upload take time)."""
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        self.t += 7.0
        return self.t

    def sleep(self, _s):
        pass

    def __getattr__(self, name):
        return getattr(__import__("time"), name)


def eph_run_world(data=False):
    w = eph_world()
    w["src"]["servers"]["srv-src-eph01"]["status"] = "ACTIVE"
    if data:
        w["src"]["servers"]["srv-src-eph01"]["volumes"] = [{"Volume ID": "vol-src-data", "Device": "/dev/vdb"}]
        w["src"]["volumes"]["vol-src-data"]["status"] = "in-use"
    return w


real_time = ephemeral.time
ephemeral.time = SlowClock()
r = app(eph_run_world(), [("cli", ["migrate", "eph01", "--progress-json", "--yes"] + PLAIN)])
ephemeral.time = real_time
m = re.search(r"\] downtime breakdown: total (\d+)s, copy (\d+)s \(glance (\d+)s\), verify (\d+)s, control-plane (\d+)s",
              r.out)
dt = [e for e in events(r.out) if e and e.get("event") == "downtime"]
ok("8 D3b ephemeral: the Glance snapshot/download/upload is counted as copy ('copy Ns (glance Ns)'), event too",
   r.rc == [0] and m and int(m.group(3)) > 0 and int(m.group(2)) == int(m.group(3))
   and dt and dt[0].get("glance") == int(m.group(3)) and dt[0]["copy"] == int(m.group(2)), (r.rc, r.out[-600:]))
ephemeral.time = SlowClock()
r = app(eph_run_world(data=True), [("migrate", "eph01", PLAIN)])
ephemeral.time = real_time
m = re.search(r"copy (\d+)s \(glance (\d+)s\)", r.out)
ok("8 D3b ephemeral with a data volume: copy = Glance + the volume copy (copy >= glance)",
   r.rc == [0] and m and int(m.group(1)) >= int(m.group(2)) > 0, (r.rc, r.out[-500:]))
r = app(world(), [("cli", ["migrate", "app01", "--progress-json", "--yes"] + PLAIN)])
ok("8 D3b boot-from-volume: the breakdown line as in 7.2 (no glance bracket, no glance field)",
   re.search(r"\] downtime breakdown: total \d+s, copy \d+s, verify \d+s, control-plane \d+s\n", r.out)
   and "glance" not in r.out, r.out[-400:])

# ================================ 11. security rules ================================
from types import SimpleNamespace as NS  # noqa: E402
from migratetool import clouds, state  # noqa: E402
from migratetool import status as status_mod2  # noqa: E402

FAKE = "Zq7-not-a-real-secret-91"  # stands for an openrc password in these cases


def secret_steps(_wf, root):
    os.makedirs(os.path.join(root, FAKE))  # a state directory named like the secret (status)


w = world()
w["src"]["servers"]["srv-src-app01"]["name"] = FAKE  # list, plan
w["dst"]["flavors"][FAKE] = {"vcpus": 1, "ram": 1, "disk": 1}  # inventory
cases = (("list", ["list", "--host", "cmp1", "--json"]), ("plan", ["plan", FAKE, "--json"] + PLAIN),
         ("inventory", ["inventory", "--json"]), ("status", ["status", "--json"]), ("doctor", ["doctor", "--json"]))
r = app(w, [secret_steps] + [("cli", a) for _n, a in cases],
        env={"src_rc": {"OS_PASSWORD": FAKE}, "dst_rc": {"OS_PROJECT_NAME": FAKE}})
ok("SEC-1 a secret value of an openrc in any --json answer (list, plan, inventory, status, doctor): die, stdout empty",
   r.rc == [1] * 5 and r.outs == [""] * 5 and all("refusing to print JSON: " in e for e in r.errs)
   and not any(FAKE in e.split("refusing to print JSON: ")[-1] for e in r.errs), (r.rc, r.outs, [e[-200:] for e in r.errs]))
leaks = []
for bad in ({"OS_PASSWORD": "x"}, {"a": [{"src_rc": 1}]}, {"token_project": "admin"}, {"x": {"auth_url": "u"}},
            {"CACERT": 1}, {"ssh_key": 1}, {"AQB": 1}, {"password": 1}, {"app_secret": 1}):
    try:
        with redirect_stdout(io.StringIO()) as so:
            jsonout.emit_json(bad, cfg=config.load())
        leaks.append((bad, so.getvalue()))
    except Die as e:
        if so.getvalue():
            leaks.append((bad, so.getvalue()))
ok("SEC-1 emit_json refuses credential-like keys at any depth (OS_, _RC, PASSWORD, SECRET, TOKEN, AUTH_URL, CACERT, "
   "KEY, AQ) and prints nothing", not leaks, leaks)
with redirect_stdout(io.StringIO()) as so, redirect_stderr(io.StringIO()) as se:
    progress.enable()
    dropped = progress.emit("result", rc=0, OS_TOKEN="x")
    progress.reset()
ok("SEC-1 a progress line with a credential is dropped with a warning, never printed, never raised",
   dropped is False and so.getvalue() == "" and "WARN JSON line not printed" in se.getvalue(), se.getvalue())

r = app(world(), [("migrate", "app01", PLAIN)],
        env={"env": {"CEPH_CONF": "/etc/ceph/x.conf", "MIG_ISCSI_IP": "10.0.0.9", "RBD_USER": "u"}})
doc = jload(os.path.join(r.state, "app01", "options.json")) if r.rc == [0] else {}
ok("SEC-2 options.json conf is the whitelist exactly (no SRC_RC/DST_RC, CEPH_*, OS_*, RBD_USER, MIG_ISCSI_IP)",
   set(doc.get("conf", {})) == {"path", "found"} | set(record.CONF_KEYS)
   and not [k for k in doc.get("conf", {}) if k.endswith("_RC") or k.startswith(("CEPH", "OS_"))]
   and "/etc/ceph/x.conf" not in json.dumps(doc), doc.get("conf"))

sroot = os.path.join(inproc.WORK, "sec3")
os.makedirs(sroot)
st = state.StateDir("web-01", root=sroot)
st.create()
fake_r = NS(opts=Options(), cfg=config.load(), pool="p", started="")
dies("SEC-3 a recorded command line with a newline dies before options.json exists",
     lambda: record.write_options_json(fake_r, st, "vm", ["migrate", "migrate", "web-01", "--flavor", "a\nb"], {}),
     "control character")
ok("SEC-3 ... and nothing was written", not os.path.exists(st.p("options.json")), os.listdir(st.path))
dies("SEC-3 the same for rollback.json", lambda: record.write_rollback_json(config.load(), st.path, "x\ry", ""),
     "control character")


def evil(_wf, root):
    os.symlink("/etc", os.path.join(root, "evil"))
    os.symlink("/etc", os.path.join(root, "web.rolled-back.1795000000"))
    os.makedirs(os.path.join(root, "good"))
    with open(os.path.join(root, "good", "volume-map"), "w") as f:
        f.write("")
    os.symlink("/etc/passwd", os.path.join(root, "good", "options.json"))


w = world()
w["src"]["servers"]["srv-src-app01"]["name"] = "evil"
r = app(w, [evil, ("cli", ["status", "--json"]), ("cli", ["list", "--host", "cmp1", "--json"])])
obj, why = one_json(r.outs[0])
lobj, _ = one_json(r.outs[1])
ok("SEC-4 status: symlinked state entries (to /etc) skipped and counted, a symlinked options.json is not followed",
   obj is not None and obj.get("skipped") == 2 and "/etc" not in r.outs[0] and "root:" not in r.outs[0]
   and [e["ident"] for e in obj["live"]] == ["good"] and obj["live"][0]["options"] is None
   and "error" in obj["live"][0], (why, obj))
ok("SEC-4 list: a VM named like a symlinked state directory has no state", lobj is not None
   and lobj["rows"][0]["state"] is None and "/etc" not in r.outs[1], lobj)

calls_before = []
for label, args in (("--host a/b", ["--host", "a/b"]), ("--vm ..", ["--vm", ".."]), ("--project with a newline",
                    ["--project", "p\nq"]), ("a 300-character --vm", ["--vm", "v" * 300])):
    r = app(world(), [("cli", ["list"] + args + ["--json"])])
    ok("SEC-6 list %s: refused before any cloud call" % label,
       r.rc == [1] and r.out == "" and "not a name or ID" in r.err and not r.calls, (r.rc, r.err, r.calls[:2]))
vf = os.path.join(inproc.WORK, "vms.txt")
with open(vf, "w") as f:
    f.write("app01\n")
os.symlink(vf, vf + ".link")
with open(vf + ".big", "w") as f:
    f.write("x" * (1 << 20) + "y")
with open(vf + ".long", "w") as f:
    f.write("a\n" * 10001)
for label, path, want in (("a symlink", vf + ".link", "must be a regular file (not a symlink)"),
                          ("a directory", inproc.WORK, "must be a regular file"),
                          ("over 1 MB", vf + ".big", "is larger than 1048576 bytes"),
                          ("over 10 000 lines", vf + ".long", "has more than 10000 lines")):
    r = app(world(), [("cli", ["list", "--vm-file", path, "--json"]), ("cli", ["inventory", "--count", "--host", "a/b"])])
    ok("SEC-6 --vm-file %s refused; inventory --host validated the same way" % label,
       r.rc == [1, 1] and want in r.errs[0] and "not a name or ID" in r.errs[1] and not r.calls, (r.rc, r.errs))
r = app(world(), [("cli", ["list", "--vm-file", vf, "--json"])])
ok("SEC-6 a plain --vm-file is read", r.rc == [0] and one_json(r.out)[0]["rows"][0]["name"] == "app01", r.err[-300:])

pat = re.compile(r"shell=True|os\.system|eval\(|exec\(")
hits = []
pkg = os.path.join(inproc.PKG_PARENT, "migratetool")
for name in sorted(os.listdir(pkg)):
    if name.endswith(".py"):
        with open(os.path.join(pkg, name)) as f:
            hits += ["%s:%d" % (name, i) for i, line in enumerate(f, 1) if pat.search(line)]
ok("SEC-7 no shell=True, os.system, eval( or exec( anywhere in migratetool/ (run.sh greps the same)", not hits, hits)

w = world()
w["src"]["servers"]["srv-src-app01"]["name"] = ["x"]
w["src"]["servers"]["srv-src-app01"]["networks_column"] = ["not", "a", "dict"]
r = app(w, [("cli", ["list", "--host", "cmp1", "--json"])])
obj, why = one_json(r.out)
row = (obj or {}).get("rows", [{}])[0]
ok("SEC-8 a listing field of the wrong type (Name as a list, Networks as a list) reads as empty, no crash",
   obj is not None and r.rc == [0] and row.get("name") == "" and row.get("networks") == [] and row.get("host") == "cmp1",
   (why, row, r.err[-300:]))

dies("SEC-9 a read-only Cloud refuses a mutating command before it runs",
     lambda: setattr(c9 := clouds.Cloud("source", "/nonexistent", "", {}), "readonly", True) or c9.run("server", "stop", "x"),
     "read-only command tried: openstack server stop x")
ok("SEC-9 read_only(): list/show/token issue only; a positional that looks like an action does not count",
   clouds.read_only(["server", "list", "--all-projects"]) and clouds.read_only(["volume", "show", "delete"])
   and clouds.read_only(["token", "issue", "-f", "json"]) and clouds.read_only(["compute", "service", "list"])
   and not clouds.read_only(["server", "delete", "show"]) and not clouds.read_only(["image", "save", "x"])
   and not clouds.read_only(["--os-compute-api-version", "2.74", "server", "create", "x"])
   and not clouds.read_only(["catalog"]))
r = app(world(), [("cli", ["plan", "app01", "--json"] + PLAIN), ("cli", ["doctor", "--json"]),
                  ("cli", ["list", "--host", "cmp1", "--json"]), ("cli", ["inventory", "--json", "--count"]),
                  ("cli", ["status", "--json"])])
mut = [c for c in r.calls if c.startswith(("src\t", "dst\t")) and
       re.search(r"\b(create|set|delete|stop|start|lock|unlock|add|remove|save)\b", c.split("\t", 1)[1].split(" -")[0])]
ok("SEC-9 the five JSON commands ran: not one create/set/delete/stop/start/lock/add/remove call",
   r.rc == [0] * 5 and r.calls and not mut, (r.rc, mut))


def plan_dir(_wf, root):
    TREE.append(tree(root))


TREE = []
r = app(world(), [("migrate", "app01", PLAIN), plan_dir, ("cli", ["list", "--host", "cmp1", "--json"]),
                  ("cli", ["inventory", "--json"]), ("cli", ["status", "--json", "--all"]), ("cli", ["doctor", "--json"]),
                  plan_dir])
ok("SEC-10 list, inventory, status, doctor --json leave STATE_ROOT exactly as it was (modes, sizes, mtimes)",
   r.rc == [0] * 5 and len(TREE) == 2 and TREE[0] == TREE[1], (r.rc, set(TREE[0].items()) ^ set(TREE[1].items())
                                                               if len(TREE) == 2 else TREE))
r1 = app(world(), [("cli", ["plan", "app01"] + PLAIN), plan_dir])
r2 = app(world(), [("cli", ["plan", "app01", "--json"] + PLAIN), plan_dir])
ok("SEC-10 plan --json writes in STATE_ROOT what plan writes (7.2: its plan directory only)",
   sorted(TREE[-2]) == sorted(TREE[-1]) and "app01/options.json" not in TREE[-1], (sorted(TREE[-2]), sorted(TREE[-1])))


def unreadable(_wf, root):
    for name in ("bad", "fine"):
        os.makedirs(os.path.join(root, name))
        with open(os.path.join(root, name, "volume-map"), "w") as f:
            f.write("a b\n")
    os.makedirs(os.path.join(root, "bad", "options.json"))  # a directory where a file should be
    os.makedirs(os.path.join(root, "locked"))
    with open(os.path.join(root, "locked", "volume-map"), "w") as f:
        f.write("")
    os.chmod(os.path.join(root, "locked"), 0)


r = app(world(), [unreadable, ("cli", ["status", "--json"])])
os.chmod(os.path.join(r.state, "locked"), 0o700)
obj, why = one_json(r.out)
live = {e["ident"]: e for e in (obj or {}).get("live", []) + (obj or {}).get("plan_only", [])}
ok("SEC-11 status: a state directory that cannot be read gets 'error', the others are listed, rc 0",
   obj is not None and r.rc == [0] and "error" in live.get("bad", {}) and "error" not in live.get("fine", {"error": 1})
   and live["fine"]["volume_map"] == [["a", "b"]]
   and ("error" in live.get("locked", {}) or os.geteuid() == 0), (why, live))

r = app(world(), [("migrate", "app01", PLAIN), ROLL])
retired = [d for d in os.listdir(r.state) if d.startswith("app01.rolled-back.")]
modes = [stat.S_IMODE(os.stat(os.path.join(r.state, retired[0], n)).st_mode) for n in ("options.json", "rollback.json")] \
    if retired else []
ok("SEC-15 options.json and rollback.json are 0600 in a 0700 state directory",
   modes == [0o600, 0o600] and stat.S_IMODE(os.stat(os.path.join(r.state, retired[0])).st_mode) == 0o700, (retired, modes))
p15 = os.path.join(inproc.WORK, "exists.json")
with open(p15, "w") as f:
    f.write("{}")
dies("SEC-15 an existing file is never overwritten", lambda: jsonout.write_private_json(p15, {"a": 1}), "exists: not overwritten")
os.symlink(os.path.join(inproc.WORK, "target.json"), p15 + ".link")
dies("SEC-15 a symlink in its place is not followed", lambda: jsonout.write_private_json(p15 + ".link", {"a": 1}),
     "exists: not overwritten")
ok("SEC-15 ... and its target was not created", not os.path.exists(os.path.join(inproc.WORK, "target.json")))

w = world()
w["src"]["compute_hosts"] = [{"host": "h%02d" % i, "zone": "nova"} for i in range(65)]
r = app(w, [("cli", ["inventory", "--json", "--count"]), ("cli", ["inventory", "--json", "--count", "--host", "h01,h02"])])
ok("SEC-16 inventory --count over 64 hosts dies (name them with --host); with --host it counts those",
   r.rc == [1, 0] and r.outs[0] == "" and "--count over 64 hosts (65): name the hosts to count with --host" in r.errs[0]
   and len([c for c in r.calls if c.startswith("src\tserver list --all-projects --host")]) == 2, (r.rc, r.errs))


def many_rolled(_wf, root):
    for i in range(60):
        os.makedirs(os.path.join(root, "vm%02d.rolled-back.%d" % (i, 1795000000 + i)))


r = app(world(), [many_rolled, ("cli", ["status", "--json"]), ("cli", ["status", "--json", "--all"])])
o1, _ = one_json(r.outs[0])
o2, _ = one_json(r.outs[1])
ok("SEC-16 status: the newest 50 rolled-back directories by default, --all up to 2000, the total always",
   o1 is not None and len(o1["rolled_back"]) == 50 and o1["rolled_back_total"] == 60
   and o1["rolled_back"][0]["ident"] == "vm59" and len(o2["rolled_back"]) == 60 and status_mod2.ALL_LIMIT == 2000,
   (o1 and len(o1["rolled_back"]), o2 and len(o2["rolled_back"])))

# ================================ end ================================
shutil.rmtree(inproc.WORK, ignore_errors=True)
print("TOTAL pass=%d fail=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
