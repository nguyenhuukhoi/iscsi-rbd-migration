#!/usr/bin/env python3
"""V7-P6 unit checks: the AZ name resolver, the security group rule builder and copy plan/creation, the
port fidelity options, the new flags, the -I brick entry.

Runs anywhere Python >= 3.10 runs (no openstack, no bash): the clouds are faked.

    python p6unit.py <directory holding migratetool/>
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from types import SimpleNamespace as NS

PKG_PARENT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
sys.path.insert(0, PKG_PARENT)
import migratetool  # noqa: E402
from migratetool import azname, copyvol, preflight, proc, secgroups, selector, server, volcmd  # noqa: E402
from migratetool.options import FLAG_OPTS, VALUE_OPTS, Options  # noqa: E402
from migratetool.proc import Result  # noqa: E402
from migratetool.util import Die  # noqa: E402

PASS = FAIL = 0


def ok(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS " + label)
    else:
        FAIL += 1
        print("FAIL %s %s" % (label, detail))


def dies(label, fn, want):
    try:
        fn()
        ok(label, False, "no error")
    except Die as e:
        ok(label, want in str(e), str(e))


# --- 1. AZ resolver -------------------------------------------------------------------------------
Z = azname.zones_from_listing
ok("zones: one available zone", Z([{"Zone Name": "nova", "Zone Status": "available"}]) == ["nova"])
ok("zones: 'internal' and 'not available' do not count, duplicates once, order kept",
   Z([{"Zone Name": "internal", "Zone Status": "available"}, {"Zone Name": "AZ2", "Zone Status": "available"},
      {"Zone Name": "AZ9", "Zone Status": "not available"}, {"Zone Name": "AZ2", "Zone Status": "available"},
      {"Zone Name": "AZ1"}]) == ["AZ2", "AZ1"])
ok("zones: garbage tolerated", Z(None) == [] and Z([1, "x", {"Zone Status": "available"}]) == [])
ok("pick: the only zone", azname.pick(["dc10-epz"]) == "dc10-epz")
dies("pick: no zone", lambda: azname.pick([]), "the destination lists no usable compute availability zone — pass --az")
dies("pick: two zones named, way out given", lambda: azname.pick(["AZ1", "AZ2"]),
     "no --az, no MIG_AZ, and the destination has 2 compute availability zones (AZ1 AZ2) — pass --az <zone> or set "
     "MIG_AZ (opt out: --no-server-group, --no-copy-secgroup)")


class AZCloud:
    def __init__(self, rc, rows):
        self.rc, self.rows, self.calls = rc, rows, []

    def run(self, *a, stderr=None, capture=True):
        self.calls.append(list(a))
        return Result(self.rc, json.dumps(self.rows) if self.rc == 0 else "", "boom")


c = AZCloud(0, [{"Zone Name": "nova", "Zone Status": "available"}])
got = azname.resolve(NS(opts=NS(dest_az="dc10-epz"), dst=c))
ok("resolve: --az/MIG_AZ wins, no cloud call, no INFO sentence", got == ("dc10-epz", "") and c.calls == [], (got, c.calls))
got = azname.resolve(NS(opts=NS(dest_az=""), dst=c))
ok("resolve: the destination's only compute AZ, read-only listing",
   got[0] == "nova" and "only compute availability zone" in got[1]
   and c.calls == [["availability", "zone", "list", "--compute", "-f", "json"]], (got, c.calls))
dies("resolve: listing fails", lambda: azname.resolve(NS(opts=NS(dest_az=""), dst=AZCloud(1, []))),
     "cannot list the destination compute availability zones")
dies("resolve: two zones", lambda: azname.resolve(NS(opts=NS(dest_az=""), dst=AZCloud(0, [
    {"Zone Name": "a", "Zone Status": "available"}, {"Zone Name": "b", "Zone Status": "available"}]))), "(a b)")

# --- 2. rule builder ----------------------------------------------------------------------------------
RA = secgroups.rule_args


def rule(**kw):
    base = {"direction": "ingress", "ethertype": "IPv4", "protocol": None, "port_range_min": None,
            "port_range_max": None, "remote_ip_prefix": None, "remote_group_id": None, "description": ""}
    base.update(kw)
    return base


ok("rule: tcp 22 from a CIDR, description",
   RA(rule(protocol="tcp", port_range_min=22, port_range_max=22, remote_ip_prefix="0.0.0.0/0", description="ssh"))
   == ["--ingress", "--ethertype", "IPv4", "--protocol", "tcp", "--dst-port", "22:22", "--remote-ip", "0.0.0.0/0",
       "--description", "ssh"])
ok("rule: port range", RA(rule(protocol="udp", port_range_min=8000, port_range_max=8080))[-2:] == ["--dst-port", "8000:8080"])
ok("rule: only a minimum port -> that port", RA(rule(protocol="tcp", port_range_min=80))[-2:] == ["--dst-port", "80:80"])
ok("rule: ICMP type/code, no --dst-port",
   RA(rule(protocol="icmp", port_range_min=8, port_range_max=0)) ==
   ["--ingress", "--ethertype", "IPv4", "--protocol", "icmp", "--icmp-type", "8", "--icmp-code", "0"])
ok("rule: ICMP type only", RA(rule(protocol="icmp", port_range_min=3)) [-2:] == ["--icmp-type", "3"])
ok("rule: ipv6-icmp and protocol 58 are ICMP too",
   "--icmp-type" in RA(rule(ethertype="IPv6", protocol="ipv6-icmp", port_range_min=135))
   and "--icmp-type" in RA(rule(ethertype="IPv6", protocol="58", port_range_min=136)))
ok("rule: protocol number (VRRP 112) + remote group mapped to the destination ID",
   RA(rule(protocol="112", remote_group_id="src-id"), "dst-id") ==
   ["--ingress", "--ethertype", "IPv4", "--protocol", "112", "--remote-group", "dst-id"])
ok("rule: remote group wins over a CIDR", "--remote-ip" not in RA(rule(remote_group_id="g", remote_ip_prefix="10.0.0.0/8"), "d"))
ok("rule: egress any IPv6 (no remote: openstackclient's ::/0)", RA(rule(direction="egress", ethertype="IPv6")) ==
   ["--egress", "--ethertype", "IPv6"])
ok("rule: protocol 0 is not 'any'", RA(rule(protocol=0))[-2:] == ["--protocol", "0"])
K = secgroups.rule_key
ok("rule key: no remote == 0.0.0.0/0 (IPv4) and == ::/0 (IPv6)",
   K(rule(direction="egress")) == K(rule(direction="egress", remote_ip_prefix="0.0.0.0/0"))
   and K(rule(direction="egress", ethertype="IPv6")) == K(rule(direction="egress", ethertype="IPv6", remote_ip_prefix="::/0")))
ok("rule key: direction, protocol case, ports, remote group tell rules apart",
   len({K(rule()), K(rule(direction="egress")), K(rule(protocol="tcp")), K(rule(protocol="tcp", port_range_min=1)),
        K(rule(remote_group_id="g"))}) == 5 and K(rule(protocol="TCP")) == K(rule(protocol="tcp")))


# --- 3. copy plan + creation, clouds faked --------------------------------------------------------------
class SGCloud:
    """Neutron security groups of one side, for the plan and ensure() (records every call)."""

    def __init__(self, side, pid, groups, fail_on=None):
        self.side, self.project, self._pid = side, "", pid
        self.groups, self.calls, self.fail_on, self.n = groups, [], fail_on, 0

    def project_id(self):
        return self._pid

    def run(self, *a, stderr=None, capture=True):
        a = list(a)
        self.calls.append(" ".join(a))
        if a[:3] == ["security", "group", "list"]:
            return Result(0, json.dumps([{"ID": k, "Name": g["name"]} for k, g in self.groups.items()]), "")
        if a[:3] == ["security", "group", "delete"]:
            # Neutron refuses to delete a group that a rule of another group points at (7.2 C3).
            if any(a[3] in (x.get("args") or []) for k, g in self.groups.items() if k != a[3] for x in g["rules"]):
                return Result(1, "", "Security Group %s in use. (HTTP 409)" % a[3])
            self.groups.pop(a[3], None)
            return Result(0, "", "")
        if a[:4] == ["security", "group", "rule", "list"]:  # 7.2 (C3): the cleanup deletes the rules first
            return Result(0, "".join(x["id"] + "\n" for x in self.groups[a[4]]["rules"]), "")
        if a[:4] == ["security", "group", "rule", "delete"]:
            for g in self.groups.values():
                g["rules"] = [x for x in g["rules"] if x["id"] not in a[4:]]
            return Result(0, "", "")
        raise AssertionError("unexpected run: %s" % a)

    def out(self, *a, stderr=None):
        a = list(a)
        self.calls.append(" ".join(a))
        if a[:3] == ["security", "group", "create"]:
            self.n += 1
            gid = "new-%d" % self.n
            self.groups[gid] = {"name": a[3], "rules": [{"id": gid + "-e4"}, {"id": gid + "-e6"}],
                                "description": a[a.index("--description") + 1] if "--description" in a else "",
                                "stateful": "--stateless" not in a}
            return gid + "\n"
        if a[:4] == ["security", "group", "rule", "list"]:
            return "".join(x["id"] + "\n" for x in self.groups[a[4]]["rules"])
        if a[:4] == ["security", "group", "rule", "delete"]:
            for g in self.groups.values():
                g["rules"] = [x for x in g["rules"] if x["id"] not in a[4:]]
            return ""
        if a[:4] == ["security", "group", "rule", "create"]:
            if self.fail_on and self.fail_on in a:
                raise Die("openstack security group rule create failed (exit 1): quota")
            self.groups[a[4]]["rules"].append({"id": "r%d" % len(self.calls), "args": a[5:-4]})
            return "x\n"
        if a[:3] == ["security", "group", "show"]:
            g = self.groups[a[3]]
            if "json" in a:
                return json.dumps({"id": a[3], "name": g["name"], "description": g.get("description", ""),
                                   "stateful": g.get("stateful", True), "rules": g.get("rules", [])})
            return g["name"] + "\n"
        raise AssertionError("unexpected out: %s" % a)

    def value(self, *a, stderr=None):
        return self.out(*a).rstrip("\n")

    def json(self, *a, stderr=None):
        return json.loads(self.out(*a))


def src_groups():
    return {
        "s-app": {"name": "app", "description": "app tier", "rules": [
            rule(id="a1", protocol="tcp", port_range_min=5432, port_range_max=5432, remote_group_id="s-db"),
            rule(id="a2", remote_group_id="s-app"),
            rule(id="a3", protocol="tcp", port_range_min=6379, port_range_max=6379, remote_group_id="s-cache"),
            rule(id="a4", direction="egress"), rule(id="a5", direction="egress", remote_ip_prefix="0.0.0.0/0")]},
        "s-db": {"name": "db", "stateful": False, "rules": [
            rule(id="b1", protocol="tcp", port_range_min=80, port_range_max=80, remote_group_id="s-app")]},
        "s-cache": {"name": "cache", "rules": [rule(id="c1", direction="egress")]},
        "s-web": {"name": "web", "rules": [rule(id="w1", protocol="tcp", port_range_min=1, port_range_max=1,
                                               remote_address_group_id="ag")]},
        "s-dup1": {"name": "dup", "rules": []}, "s-dup2": {"name": "dup", "rules": []},
        "s-amb": {"name": "amb", "rules": []},
    }


def make_r(dst_groups=None, fail_on=None):
    src = SGCloud("source", "psrc", src_groups())
    dst = SGCloud("destination", "pdst", dst_groups if dst_groups is not None else
                  {"d-cache": {"name": "dc10-epz-cache", "rules": [{"id": "keep"}]}}, fail_on)
    return NS(src=src, dst=dst, cfg=NS(SRC_PROJECT="", DST_PROJECT=""))


r = make_r()
plan = secgroups.plan_copy(r, ["app"], "dc10-epz")
g = plan.groups
ok("plan: app -> db -> app loop + self-reference ends; cache found, not walked",
   list(g) == ["s-app", "s-db", "s-cache"] and g["s-app"].create and g["s-db"].create and not g["s-cache"].create
   and g["s-cache"].dst_id == "d-cache", {k: (v.dst_name, v.create) for k, v in g.items()})
ok("plan: one 'show -f json' per group to create; 1.0.0 (A13): the found one is shown too (its rules compared)",
   [c for c in r.src.calls if c.startswith("security group show")] ==
   ["security group show s-app -f json", "security group show s-db -f json", "security group show s-cache -f json"],
   r.src.calls)
ok("plan: duplicate rule (no remote == 0.0.0.0/0) copied once; description + stateless kept",
   len(g["s-app"].rules) == 4 and g["s-app"].description == "app tier" and g["s-db"].stateful is False)
ok("plan: refs exclude the group itself", g["s-app"].refs == ["s-db", "s-cache"] and g["s-db"].refs == ["s-app"])
good, text = secgroups.describe(plan, "app")
ok("describe: will create, rules, referenced copies", good and text ==
   "security group app -> will create dc10-epz-app (4 rules, + dc10-epz-db referenced, dc10-epz-cache referenced, found)",
   text)
ok("describe: found (the copy of that name exists)",
   secgroups.describe(secgroups.plan_copy(make_r(), ["cache"], "dc10-epz"), "cache") ==
   (True, "security group cache -> found dc10-epz-cache (d-cache)"))
plan2 = secgroups.plan_copy(make_r(), ["cache", "nosuch", "dup", "web"], "AZ1")
ok("describe: another AZ, another copy (AZ1-cache is created, dc10-epz-cache ignored)",
   secgroups.describe(plan2, "cache") == (True, "security group cache -> will create AZ1-cache (1 rule)"),
   secgroups.describe(plan2, "cache"))
ok("describe: source group missing", secgroups.describe(plan2, "nosuch") ==
   (False, "security group nosuch: not found in source project psrc"), secgroups.describe(plan2, "nosuch"))
ok("describe: two source groups of one name", not secgroups.describe(plan2, "dup")[0]
   and "2 security groups are named 'dup' in source project psrc (s-dup1 s-dup2)" in secgroups.describe(plan2, "dup")[1])
ok("describe: remote address group -> FAIL text", secgroups.describe(plan2, "web") ==
   (False, "security group web -> AZ1-web: rule w1 of web has a remote address group, which is not copied — create "
           "AZ1-web at the destination yourself, or pass --security-group"), secgroups.describe(plan2, "web"))
r3 = make_r({"x1": {"name": "Z-amb", "rules": []}, "x2": {"name": "Z-amb", "rules": []}})
ok("describe: two destination copies of one name",
   "2 security groups are named Z-amb in destination project pdst (x1 x2)" in
   secgroups.describe(secgroups.plan_copy(r3, ["amb"], "Z"), "amb")[1])
src = src_groups()
src["s-app"]["rules"].append(rule(id="a9", remote_group_id="s-web"))
r4 = make_r()
r4.src.groups = src
p4 = secgroups.plan_copy(r4, ["app"], "Z")
ok("describe: a referenced copy that cannot be made fails the port's group",
   secgroups.describe(p4, "app") == (False, "security group app -> Z-web (referenced): rule w1 of web has a remote "
                                           "address group, which is not copied — create Z-web at the destination "
                                           "yourself, or pass --security-group"), secgroups.describe(p4, "app"))


class St:
    def __init__(self):
        self.files = {}

    def write(self, name, text):
        self.files[name] = text


r = make_r()
plan = secgroups.plan_copy(r, ["app"], "dc10-epz")
st = St()
buf = io.StringIO()
with redirect_stdout(buf):
    secgroups.ensure(r, plan, st)
d = r.dst.groups
app_id = [k for k, v in d.items() if v["name"] == "dc10-epz-app"][0]
db_id = [k for k, v in d.items() if v["name"] == "dc10-epz-db"][0]
ok("ensure: both copies created before any rule (loop safe), Neutron's egress rules deleted",
   [c.split()[2] for c in r.dst.calls if c.startswith("security group") and c.split()[2] in ("create", "rule")][:4]
   == ["create", "rule", "rule", "create"] and all(x["id"] not in (app_id + "-e4", app_id + "-e6") for x in d[app_id]["rules"]),
   r.dst.calls)
ok("ensure: group description (1.0.0 A13: after 'mig-copy of <source id>: ') and --stateless copied",
   d[app_id]["description"] == "mig-copy of s-app: app tier" and d[db_id]["stateful"] is False)
app_rules = [x["args"] for x in d[app_id]["rules"]]
ok("ensure: remote groups mapped (db copy, itself, the found cache)",
   ["--ingress", "--ethertype", "IPv4", "--protocol", "tcp", "--dst-port", "5432:5432", "--remote-group", db_id] in app_rules
   and ["--ingress", "--ethertype", "IPv4", "--remote-group", app_id] in app_rules
   and ["--ingress", "--ethertype", "IPv4", "--protocol", "tcp", "--dst-port", "6379:6379", "--remote-group", "d-cache"] in app_rules
   and len(app_rules) == 4, app_rules)
ok("ensure: state lists every group used",
   st.files.get("dst-security-groups") == "%s dc10-epz-app created=yes\n%s dc10-epz-db created=yes\nd-cache dc10-epz-cache "
   "created=no\n" % (app_id, db_id), st.files)
ok("ensure: logs found and created", "dc10-epz-cache (d-cache) exists: used as is" in buf.getvalue()
   and "created destination security group dc10-epz-app" in buf.getvalue(), buf.getvalue())
ok("ensure: the found group is not touched", d["d-cache"]["rules"] == [{"id": "keep"}])

r = make_r(fail_on="80:80")  # db's rule: app's rules are written first, then db's fails
plan = secgroups.plan_copy(r, ["app"], "dc10-epz")
st = St()
buf = io.StringIO()
try:
    with redirect_stdout(buf):
        secgroups.ensure(r, plan, st)
    ok("ensure: a failing rule raises", False)
except Die:
    ok("ensure: a failing rule -> the groups just created are deleted, nothing recorded",
       sorted(v["name"] for v in r.dst.groups.values()) == ["dc10-epz-cache"] and st.files == {}
       and "WARN deleted the security groups this run had just created (dc10-epz-app, dc10-epz-db)" in buf.getvalue(),
       (r.dst.groups, buf.getvalue()))

# --- 4. port fidelity -----------------------------------------------------------------------------------
PORT = {"mac": "fa:16:3e:AA:bb:01", "aap": [{"ip_address": "198.51.100.99", "mac_address": "fa:16:3e:aa:bb:01"},
                                            {"ip_address": "198.51.100.98", "mac_address": "00:00:5e:00:01:05"},
                                            {"ip_address": "198.51.100.97"}, "junk"],
        "port_security": True, "description": "vip holder"}
ok("pairs: own MAC (any case) left to Neutron, other MAC kept, no MAC = ip only, junk skipped",
   server.pair_args(PORT) == ["--allowed-address", "ip-address=198.51.100.99",
                              "--allowed-address", "ip-address=198.51.100.98,mac-address=00:00:5e:00:01:05",
                              "--allowed-address", "ip-address=198.51.100.97"], server.pair_args(PORT))
ok("fidelity: pairs + description", server.fidelity_args(PORT)[-2:] == ["--description", "vip holder"]
   and "--disable-port-security" not in server.fidelity_args(PORT))
ok("fidelity: port security off", server.fidelity_args({"mac": "m", "port_security": False}) == ["--disable-port-security"])
ok("fidelity: a plain port (bash-written state: no description key) adds nothing",
   server.fidelity_args({"mac": "m", "port_security": True, "aap": []}) == [] and
   server.fidelity_args({"mac": "m", "port_security": None, "aap": None}) == [])
ok("dns_name: plainly the hostname",
   preflight.plain_hostname("app01", "app01") and preflight.plain_hostname("app-01", "App-01"))
ok("dns_name: not plainly the hostname", not any(preflight.plain_hostname(a, b) for a, b in (
    ("web-vip", "app01"), ("app.01", "app.01"), ("app_01", "app_01"), ("-app", "-app"), ("a" * 64, "a" * 64))))

# --- 5. options -----------------------------------------------------------------------------------------
ok("FLAG_OPTS has the opt-outs", "--no-server-group" in FLAG_OPTS and "--no-copy-secgroup" in FLAG_OPTS)


def opts(defaults, args, cmd="migrate"):
    o = Options()
    o.load_default_opts(cmd, defaults)
    o.parse_options(args)
    return o


o = opts("", ["--no-server-group", "--no-copy-secgroup", "--verify", "none"])
ok("parse: both opt-outs", o.no_server_group and o.no_copy_sg and o.verify == "none")
ok("parse: --server-group with --no-server-group is fine", opts("", ["--server-group", "x", "--no-server-group"]).server_group == "x")
# --keep-server-group is gone (user 19/11): an unknown option everywhere, no message of its own.
ok("--keep-server-group in no option list", "--keep-server-group" not in FLAG_OPTS + VALUE_OPTS
   and "--keep-server-group" not in volcmd.SERVER_ONLY and not hasattr(Options(), "keep_sg"))
dies("parse: --keep-server-group typed -> unknown option", lambda: opts("", ["--verify", "none", "--keep-server-group"]),
     "unknown option --keep-server-group")
dies("parse: --keep-server-group with --server-group -> unknown option (no 'choose one' any more)",
     lambda: opts("", ["--server-group", "a", "--keep-server-group"]), "unknown option --keep-server-group")
dies("MIG_DEFAULT_OPTS: --keep-server-group -> dies naming MIG_DEFAULT_OPTS",
     lambda: opts("--keep-server-group --verify none", []),
     "MIG_DEFAULT_OPTS: unknown option '--keep-server-group' (MIG_DEFAULT_OPTS=\"--keep-server-group --verify none\")")
dies("MIG_DEFAULT_OPTS of one volume: --keep-server-group dies too (unknown before the volume filter)",
     lambda: opts("--keep-server-group", [], cmd="migrate-volume"), "MIG_DEFAULT_OPTS: unknown option '--keep-server-group'")
dies("selector: --keep-server-group -> unknown option before anything is listed",
     lambda: selector.split_args("migrate", ["--vm", "a", "--keep-server-group", "--yes"]), "unknown option --keep-server-group")
o = opts("--no-copy-secgroup --no-server-group --verify none", [], cmd="migrate-volume")
ok("MIG_DEFAULT_OPTS for one volume: the opt-outs are skipped with the usual note",
   any("do not apply to migrate-volume, ignored: --no-copy-secgroup --no-server-group" in l for l in o.default_opts_info()),
   o.default_opts_info())
dies("migrate volume refuses the opt-outs", lambda: volcmd.volume_options_only(["--no-copy-secgroup"]),
     "creates no server or port: --no-copy-secgroup cannot apply")
s = selector.split_args("migrate", ["--vm", "a", "--no-copy-secgroup", "--no-server-group", "x"])
ok("selector: the opt-outs are flags passed to every VM (do not swallow the next word)",
   s.passed == ["--no-copy-secgroup", "--no-server-group"] and s.positional == ["x"], vars(s))

# --- 6. -I brick, version, help -------------------------------------------------------------------------
ok("brick child runs isolated: [python, -I, -m, migratetool.brick]",
   copyvol.BRICK == [sys.executable, "-I", "-m", "migratetool.brick"], copyvol.BRICK)
seen = []
orig = proc.run
proc.run = lambda cmd, env=None, capture=True, stderr=None: seen.append((cmd, env)) or Result(0, "/dev/sdx\n", "")
try:
    fake = NS(src=NS(env=lambda: {"OS_AUTH_URL": "u"}), cfg=NS(MIG_ISCSI_IP="192.0.2.20", multipath_set=False))
    copyvol.brick(fake, "attach", "vol-1", proc.DEVNULL)
finally:
    proc.run = orig
ok("brick(): BRICK + action + id, environment still passed",
   seen and seen[0][0] == copyvol.BRICK + ["attach", "vol-1"] and seen[0][1].get("MIG_ISCSI_IP") == "192.0.2.20"
   and seen[0][1].get("OS_AUTH_URL") == "u", seen)
ok("version 1.2.1", migratetool.__version__ == "1.2.1", migratetool.__version__)  # Intended: 7.2 D4, 7.3
scratch = tempfile.mkdtemp()
env = dict(os.environ, PYTHONPATH=PKG_PARENT, MIG_CONF=os.path.join(scratch, "none.conf"))
h = subprocess.run([sys.executable, "-m", "migratetool", "migrate", "--help"], env=env, capture_output=True, text=True,
                   cwd=scratch)
hv = subprocess.run([sys.executable, "-m", "migratetool", "volume", "--help"], env=env, capture_output=True, text=True,
                    cwd=scratch)
ok("help: migrate lists --no-server-group / --no-copy-secgroup, volume does not; nobody lists --keep-server-group",
   h.returncode == 0 and "--no-server-group" in h.stdout and "--no-copy-secgroup" in h.stdout
   and "--no-copy-secgroup" not in hv.stdout and "keep-server-group" not in h.stdout + hv.stdout, h.stdout[-400:])
shutil.rmtree(scratch, ignore_errors=True)

venv = os.environ.get("P6_VENV_PY")  # python of a venv where the package is installed (with os-brick)
if venv:
    decoy = tempfile.mkdtemp()
    os.makedirs(os.path.join(decoy, "migratetool"))
    for f in ("__init__.py", "brick.py"):
        open(os.path.join(decoy, "migratetool", f), "w").write("print('DECOY source tree imported')\n")
    e = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    plain = subprocess.run([venv, "-m", "migratetool.brick"], cwd=decoy, env=e, capture_output=True, text=True)
    iso = subprocess.run([venv, "-I", "-m", "migratetool.brick"], cwd=decoy, env=e, capture_output=True, text=True)
    ok("brick without -I: the decoy in the current directory wins (the risk)", "DECOY" in plain.stdout,
       (plain.stdout, plain.stderr[-300:]))
    ok("brick with -I: the installed brick runs (its usage, exit 1), not the decoy",
       "DECOY" not in iso.stdout and iso.returncode == 1 and "attach" in iso.stderr, (iso.stdout, iso.stderr[-300:]))
    shutil.rmtree(decoy, ignore_errors=True)
else:
    print("SKIP -I brick checks: set P6_VENV_PY to the python of a venv with the package installed")

print("TOTAL pass=%d fail=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
