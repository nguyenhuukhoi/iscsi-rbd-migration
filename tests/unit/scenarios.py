#!/usr/bin/python3
"""V7-P1 parity scenarios: bash migrate-vm.sh vs Python migrate on identical fake clouds."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # run with -I: no implicit script directory
import parity as P  # noqa: E402
from parity import base_world, compare

P.setup()
only = sys.argv[1:]


def sc(name):
    return not only or any(o in name for o in only)


def w(**mods):
    world = base_world()
    for path, val in mods.items():
        pass
    return world


# P6 (intended, listed in TEST-REPORT V7 P6): the source server group copy is named '<AZ>-<name>' instead of
# 'az-<name>'. The --keep-server-group scenarios therefore give '--az AZ1' to BOTH builds (so the app has an
# AZ to name the copy after) and read the reference's 'az-web-anti' as 'AZ1-web-anti' (subs_all, calls and
# world included). The app has no --keep-server-group since 7.1 (user 19/11): parity.legacy_optouts runs it
# on its default (which is what the flag asked the reference for). The reference's 'source VM is in no
# server group' line has no counterpart (silent by default), and the advice in two FAIL lines no longer
# says 'drop --keep-server-group'.
KEEP_SUBS = (("  INFO  source VM is in no server group\n", ""),
             ("— to use it anyway drop --keep-server-group and pass --server-group g1",
              "— to use it anyway pass --server-group g1 (or --no-server-group)"),
             ("— drop --keep-server-group and pass --server-group <id>", "— pass --server-group <id>, or --no-server-group"))
KEEP_ALL = (("az-web-anti", "AZ1-web-anti"),)


# ---------------- plan ----------------
if sc("plan-happy"):
    compare("plan-happy", ["plan", "app01"], show=True)
if sc("plan-fails"):
    W = base_world()
    W["dst"]["flavors"].pop("m1.small")
    W["dst"]["keypairs"] = []
    W["dst"]["servers"]["srv-dst-x"] = {"name": "app01", "status": "ACTIVE", "ports": [], "volumes": []}
    W["dst"]["ports"]["port-dst-busy"] = {"name": "x", "network_id": "net-dst-1",
                                          "fixed_ips": [{"subnet_id": "sub-dst-1", "ip_address": "198.51.100.50"}],
                                          "mac_address": "fa:16:3e:aa:bb:01", "security_group_ids": [], "device_id": "",
                                          "port_security_enabled": True, "allowed_address_pairs": []}
    W["dst"]["security_groups"]["sg-dst-web2"] = {"name": "web", "project_id": "pdst"}
    W["src"]["ports"]["port-src-1"]["fixed_ips"].append({"subnet_id": "sub-src-1", "ip_address": "198.51.100.51"})
    W["src"]["ports"]["port-src-1"]["allowed_address_pairs"] = [{"ip_address": "198.51.100.99"}]
    W["src"]["volumes"]["vol-src-data"]["encrypted"] = True
    W["src"]["volumes"]["vol-src-data"]["multiattach"] = True
    # P6 (intended): allowed-address-pairs are copied now: the FAIL line becomes an INFO line, after one
    # 'extension list' call (the destination must have the allowed-address-pairs extension).
    compare("plan-fails (flavor, key INFO, dup server, MAC/IP busy, SG ambiguous, extra IP, AAP, encrypted)",
            ["plan", "app01"], world=W, show=True,
            bash_subs=(("  FAIL  port fa:16:3e:aa:bb:01 has allowed-address-pairs (not handled)",
                        "  INFO  port fa:16:3e:aa:bb:01: allowed-address-pairs copied: 198.51.100.99"),),
            app_calls_ignore=(r"^dst\textension list --network -f value -c Alias$",))
if sc("plan-options"):
    compare("plan-options (--az, --volume-type, --flavor, --security-group, --ip addr)",
            ["plan", "app01", "--az", "AZ1", "--volume-type", "nvme", "--flavor", "c2.small",
             "--security-group", "web,default", "--ip", "198.51.100.77"], show=True)
    compare("plan-bad-az-type", ["plan", "app01", "--az", "nope", "--volume-type", "nope"])
    compare("plan-sg-override-missing", ["plan", "app01", "--security-group", "nosuch"])
if sc("plan-conf"):
    conf = ('MIG_AZ=AZ1   # comment\nAZ_TYPE_MAP="AZ1:nvme,AZ2:x"\nRBD_POOL_MAP=\'nvme:volumes-nvme\'\n'
            'export MIG_DEFAULT_OPTS="--verify none --keep-source-ports"\n')
    compare("plan-conf (MIG_AZ, AZ_TYPE_MAP, RBD_POOL_MAP, MIG_DEFAULT_OPTS)", ["plan", "app01", "--verify", "full"],
            conf=conf, show=True)
    compare("plan-env-beats-conf", ["plan", "app01"], conf=conf, env={"MIG_AZ": "nova", "MIG_DEFAULT_OPTS": ""})
if sc("plan-sg"):
    W = base_world()
    W["dst"]["server_groups"]["sg-dst-anti"] = {"name": "web-anti", "policy": "anti-affinity", "members": []}
    compare("plan-server-group ok", ["plan", "app01", "--server-group", "web-anti"], world=W, show=True)
    compare("plan-server-group missing", ["plan", "app01", "--server-group", "nope"], world=W)
if sc("plan-keep"):
    for label, dst_groups in (("missing", {}),
                              ("same", {"g1": {"name": "az-web-anti", "policy": "soft-anti-affinity", "members": []}}),
                              ("other-policy", {"g1": {"name": "az-web-anti", "policy": "affinity", "members": []}}),
                              ("dup", {"g1": {"name": "az-web-anti", "policy": "soft-anti-affinity", "members": []},
                                       "g2": {"name": "az-web-anti", "policy": "soft-anti-affinity", "members": []}})):
        W = base_world()
        W["src"]["server_groups"]["sg-src-anti"] = {"name": "web-anti", "policy": "soft-anti-affinity",
                                                    "rules": {}, "members": ["srv-src-app01"]}
        W["dst"]["server_groups"] = dst_groups
        compare("plan-keep-server-group " + label, ["plan", "app01", "--keep-server-group", "--az", "AZ1"], world=W,
                show=(label == "missing"), bash_subs=KEEP_SUBS, subs_all=KEEP_ALL)
    compare("plan-keep-server-group no-group", ["plan", "app01", "--keep-server-group", "--az", "AZ1"],
            bash_subs=KEEP_SUBS, subs_all=KEEP_ALL)
if sc("plan-flavor"):
    W = base_world()
    W["dst"]["flavors"].pop("m1.small")
    for label, mapping in (("hit-smaller", "m1.small c2.small\n"), ("miss", "x y\n"), ("ghost", "m1.small ghost\n")):
        import os
        path = os.path.join(P.W, "fmap-" + label)
        open(path, "w").write(mapping)
        compare("plan-flavor-fallback " + label, ["plan", "app01", "--flavor-fallback"], world=W,
                env={"FLAVOR_MAP_FILE": path}, show=(label == "hit-smaller"))
    compare("plan-flavor-fallback no map", ["plan", "app01", "--flavor-fallback"], world=W)
if sc("plan-volumes-only"):
    W = base_world()
    W["dst"]["volumes"]["vol-dst-old"] = {"name": "app01-vdb", "status": "available", "size": 2}
    # P6 (intended): --keep-server-group dropped from BOTH builds' command line. With it the reference died on
    # 'choose one: --server-group <name> or --keep-server-group' before the volumes-only checks this scenario
    # is named after; the app has no such flag any more (7.1).
    compare("plan-volumes-only clash", ["plan", "app01", "--volumes-only", "--server-group", "x",
                                        "--flavor-fallback"], world=W, show=True)
if sc("cli-dies"):
    # P6 (intended): the case '--server-group a --keep-server-group' (the reference's 'choose one' die) is gone:
    # the app has no --keep-server-group; its unknown-option die is checked in scenarios_p6.py.
    for args in (["plan", "app01", "--in-use"], ["plan", "app01", "--bogus"], ["plan", "app01", "--ip", "x"],
                 ["plan", "app01", "--start-source"],
                 ["plan", "app01", "--volumes-only", "--mode", "live"], ["plan", "app01", "--verify", "nope"],
                 ["rollback", "app01", "--x"], ["frob", "x"]):
        compare("cli-die " + " ".join(args), args)
    compare("cli-die MIG_DEFAULT_OPTS typo", ["plan", "app01"], env={"MIG_DEFAULT_OPTS": "--verfy none"})
    W = base_world()
    W["src"]["servers"]["srv-src-app01b"] = dict(W["src"]["servers"]["srv-src-app01"])
    compare("cli-die duplicate names", ["plan", "app01"], world=W)

# ---------------- migrate + rollback ----------------
if sc("mig-cold"):
    compare("migrate-cold + rollback", [["migrate", "app01", "--verify", "full"], ["rollback", "app01"]], show=True)
if sc("mig-state-exists"):
    compare("migrate twice (state exists) + rollback", [["migrate", "app01"], ["migrate", "app01"], ["rollback", "app01"]])
if sc("mig-novaport"):
    W = base_world()
    W["src"]["ports"]["port-src-1"]["nova_owned"] = True
    compare("migrate nova-owned port (reserve) + rollback", [["migrate", "app01", "--verify", "none"], ["rollback", "app01"]],
            world=W, show=True)
if sc("mig-keepsg"):
    W = base_world()
    W["src"]["server_groups"]["sg-src-anti"] = {"name": "web-anti", "policy": "anti-affinity",
                                                "rules": {"max_server_per_host": 2}, "members": ["srv-src-app01"]}
    compare("migrate keep-server-group (create, rules) + rollback",
            [["migrate", "app01", "--keep-server-group", "--az", "AZ1"], ["rollback", "app01"]], world=W, show=True,
            bash_subs=KEEP_SUBS, subs_all=KEEP_ALL)
    W["dst"]["refuse_rule"] = True
    compare("migrate keep-server-group (--rule refused)", [["migrate", "app01", "--keep-server-group", "--az", "AZ1"]],
            world=W, bash_subs=KEEP_SUBS, subs_all=KEEP_ALL)
if sc("mig-volonly"):
    compare("migrate --volumes-only --start-source + rollback",
            [["migrate", "app01", "--volumes-only", "--start-source"], ["rollback", "app01"]], show=True)
if sc("mig-ipnew"):
    compare("migrate --ip new --start-source + rollback",
            [["migrate", "app01", "--ip", "new", "--start-source"], ["rollback", "app01"]], show=True)
    compare("migrate --ip addr --keep-source-ports + rollback",
            [["migrate", "app01", "--ip", "198.51.100.77"], ["rollback", "app01"]])
    compare("migrate --keep-source-ports + rollback", [["migrate", "app01", "--keep-source-ports"], ["rollback", "app01"]])
if sc("mig-locked"):
    W = base_world()
    W["src"]["servers"]["srv-src-app01"]["locked"] = True
    W["src"]["servers"]["srv-src-app01"]["locked_reason"] = "ops"
    compare("migrate pre-locked source + rollback", [["migrate", "app01"], ["rollback", "app01"]], world=W)


def a1_early_check(b, p):
    """Intended: 7.2 A1: a destination volume that fails dies in the reference after the source stopped and its
    port was detached; the app dies before the cutover (no stop, no detach, no lock). Both rollbacks end with
    the source ACTIVE and the destination empty."""
    bad = []
    if b["rc"][0] != 1 or p["rc"] != b["rc"]:
        bad.append("rc %s / %s" % (b["rc"], p["rc"]))
    raw = p["rawcalls"]
    first = raw[:raw.index("#step\t1")] if "#step\t1" in raw else raw
    if any(c.startswith(("src\tserver stop", "src\tserver remove port", "src\tserver lock")) for c in first):
        bad.append("the app touched the source before failing: %s" % [c for c in first if "\tserver " in c])
    for side in ("src", "dst"):
        if p["world"][side]["servers"] != b["world"][side]["servers"]:
            bad.append("%s servers differ after rollback" % side)
    if p["world"]["dst"]["volumes"] or b["world"]["dst"]["volumes"]:
        bad.append("destination volumes left")
    return bad


def a1_late_check(b, p):
    """Intended: 7.2 A1: the copy of the first volume fails; the app had created every destination volume
    before the cutover (the reference only the first), so its rollback deletes two. The source ends the same."""
    bad = []
    if p["rc"] != b["rc"]:
        bad.append("rc %s / %s" % (b["rc"], p["rc"]))
    if "[T] deleted destination volume vol-dst-0002" not in p["out"]:
        bad.append("the app's rollback did not delete the second destination volume")
    if p["world"]["src"] != b["world"]["src"] or p["world"]["dst"]["volumes"]:
        bad.append("source or destination differ after rollback")
    return bad


if sc("mig-pool"):
    # Intended: 7.2 A1 (both): the destination volume fails before the cutover in the app.
    compare("migrate wrong pool map (die after create) + rollback",
            [["migrate", "app01", "--volume-type", "nvme"], ["rollback", "app01"]], show=True,
            expect_diff=("out", "err", "files", "calls", "world"), check=a1_early_check)
    W = base_world()
    W["dst"]["create_status"] = "error"
    compare("migrate dest volume error + rollback", [["migrate", "app01"], ["rollback", "app01"]], world=W,
            expect_diff=("out", "err", "files", "calls", "world"), check=a1_early_check)
if sc("mig-compare-fail"):
    compare("migrate compare mismatch + rollback", [["migrate", "app01"], ["rollback", "app01"]],
            env={"HARNESS_COMPARE_FAIL": "1"}, show=True,
            expect_diff=("out", "files", "calls", "world"), check=a1_late_check)  # Intended: 7.2 A1
if sc("mig-fallback"):
    W = base_world()
    W["dst"]["flavors"].pop("m1.small")
    import os
    path = os.path.join(P.W, "fmap-mig")
    open(path, "w").write("m1.small c2.small\n")
    compare("migrate flavor-fallback + rollback", [["migrate", "app01", "--flavor-fallback"], ["rollback", "app01"]],
            world=W, env={"FLAVOR_MAP_FILE": path})

# ---------------- state written by one build, rolled back by the other (DESIGN-V7 2.1) ----------------
if sc("cross"):
    seq = [["migrate", "app01", "--verify", "none"], ["rollback", "app01"]]
    compare("cross: bash migrate -> py rollback  ==  bash -> bash", seq, impls=("bash", "bash+py"))
    compare("cross: py migrate -> bash rollback  ==  bash -> bash", seq, impls=("bash", "py+bash"))
    W = base_world()
    W["dst"]["create_status"] = "error"
    compare("cross: bash dies at dest volume -> py rollback", [["migrate", "app01"], ["rollback", "app01"]],
            world=W, impls=("bash", "bash+py"))
    W = base_world()
    W["src"]["server_groups"]["g"] = {"name": "web-anti", "policy": "affinity", "rules": {}, "members": ["srv-src-app01"]}
    compare("cross: py keep-server-group -> bash rollback",
            [["migrate", "app01", "--keep-server-group", "--az", "AZ1"], ["rollback", "app01"]], world=W,
            impls=("bash", "py+bash"), bash_subs=KEEP_SUBS, subs_all=KEEP_ALL)

# ---------------- live (phase 2) ----------------
MB = 1048576


def p(written_mb, changed=None, zeros=0, secs=1.0, **extra):
    e = {"changed": changed if changed is not None else max(1, written_mb // 4), "chunks": 2560,
         "written_bytes": written_mb * MB, "zero_chunks": zeros, "seconds": secs}
    e.update(extra)
    return e


def live_world(root, data):
    W = base_world()
    W["sync"] = {"vol-src-root": root, "vol-src-data": data}
    return W


ROLL = ["rollback", "app01"]
if sc("live-plan"):
    compare("live plan", ["plan", "app01", "--mode", "live"], show=True)
    W = base_world()
    W["src"]["servers"]["srv-src-app01"]["volumes"] = [{"Volume ID": "vol-src-data", "Device": "/dev/vdb"}]
    W["src"]["servers"]["srv-src-app01"]["image"] = "cirros (img-src-base)"  # Intended: 7.2 B3 (fixture)
    W["src"]["servers"]["srv-src-app01"]["flavor"]["disk"] = 10  # Intended: 7.2 B2 (fixture: no 'flavor show')
    compare("live plan ephemeral root (FAIL)", ["plan", "app01", "--mode", "live"], world=W, show=True)
    compare("cold plan ephemeral root (INFO)", ["plan", "app01"], world=W)
if sc("live-converge"):
    W = live_world([p(2268, secs=130.2), p(44, secs=12.0), p(104, secs=60.0)], [p(740, secs=34.0), p(20), p(10)])
    compare("live converge in 2 passes + rollback", [["migrate", "app01", "--mode", "live"], ROLL], world=W, show=True)
if sc("live-noconverge"):
    W = live_world([p(500), p(10), p(30)], [p(740)] + [p(130 + i) for i in range(5)])
    compare("live no convergence: WARN after 5 passes, cutover + rollback",
            [["migrate", "app01", "--mode", "live"], ROLL], world=W, show=True)
    compare("live --max-passes 3 cap + --delta-limit 16", [["migrate", "app01", "--mode", "live", "--max-passes", "3",
                                                            "--delta-limit", "16"], ROLL], world=W)
    compare("live --max-passes 1", [["migrate", "app01", "--mode", "live", "--max-passes", "1"], ROLL], world=W)
if sc("live-verify"):
    W = live_world([p(500), p(10), p(5)], [p(100), p(1), p(1)])
    compare("live --verify full", [["migrate", "app01", "--mode", "live", "--verify", "full"], ROLL], world=W, show=True)
    compare("live --verify none", [["migrate", "app01", "--mode", "live", "--verify", "none"], ROLL], world=W)
    compare("live --verify full compare mismatch + rollback",
            [["migrate", "app01", "--mode", "live", "--verify", "full"], ROLL], world=W,
            env={"HARNESS_COMPARE_FAIL": "1"})
if sc("live-zero"):
    W = live_world([p(500), p(0, changed=0), p(0, changed=0)], [p(100, zeros=3), p(0, changed=0, zeros=2), p(0, changed=0)])
    compare("live churn=0 (zero chunks count as delta)", [["migrate", "app01", "--mode", "live", "--delta-limit", "8"], ROLL],
            world=W, show=True)
if sc("live-die"):
    W = live_world([p(500), p(10)], [p(100), p(0, exit=3)])
    compare("live blocksync fails in pre-sync (VM still running) + rollback",
            [["migrate", "app01", "--mode", "live"], ROLL], world=W, show=True)
    W = live_world([p(500), p(10), p(0, exit=1)], [p(100), p(1), p(1)])
    compare("live blocksync fails at cutover + rollback", [["migrate", "app01", "--mode", "live"], ROLL], world=W, show=True)
    W = live_world([p(500), p(10), p(10)], [p(100), p(1), p(1)])
    W["dst"]["create_server_status"] = "ERROR"
    compare("live server ERROR after cutover + rollback", [["migrate", "app01", "--mode", "live"], ROLL], world=W)
if sc("live-cross"):
    W = live_world([p(500), p(10), p(10)], [p(100), p(1), p(1)])
    seq = [["migrate", "app01", "--mode", "live"], ROLL]
    compare("live cross: bash migrate -> py rollback", seq, world=W, impls=("bash", "bash+py"))
    compare("live cross: py migrate -> bash rollback", seq, world=W, impls=("bash", "py+bash"))
    W = live_world([p(500), p(10)], [p(100), p(0, exit=3)])
    compare("live cross: py dies in pre-sync -> bash rollback", seq, world=W, impls=("bash", "py+bash"))

# ---------------- phase 3: ephemeral, multi-NIC, newip/voltype shapes ----------------
def eph_world(data_volume=False):
    W = base_world()
    W["src"]["servers"]["srv-src-eph01"] = {
        "name": "eph01", "status": "ACTIVE", "locked": False, "locked_reason": None,
        # Intended: 7.2 B3/B2: an ephemeral server names its image (detect_boot cross-checks it) and its
        # flavor's disk (the free-space check); the reference ignores both fields.
        "image": "cirros (img-src-base)",
        "flavor": {"original_name": "m1.small", "name": "m1.small", "vcpus": 2, "ram": 2048, "disk": 1},
        "key_name": "k1", "properties": {}, "description": None, "ports": ["port-src-eph"],
        "volumes": [{"Volume ID": "vol-src-data", "Device": "/dev/vdb"}] if data_volume else []}
    W["src"]["ports"]["port-src-eph"] = {"name": "", "network_id": "net-src-1", "nova_owned": True,
                                         "fixed_ips": [{"subnet_id": "sub-src-1", "ip_address": "198.51.100.60"}],
                                         "mac_address": "fa:16:3e:aa:bb:60", "security_group_ids": ["sg-src-default"],
                                         "device_id": "srv-src-eph01", "port_security_enabled": True,
                                         "allowed_address_pairs": []}
    W["src"]["snap_props"] = {"hw_disk_bus": "scsi", "hw_qemu_guest_agent": "yes", "os_distro": "ubuntu",
                              "os_hash_algo": "sha512", "os_hash_value": "abc", "os_hidden": False,
                              "stores": "file", "owner_specified.openstack.md5": "x", "os_type": "linux"}
    return W


EROLL = ["rollback", "eph01"]
if sc("eph"):
    compare("ephemeral plan", ["plan", "eph01"], world=eph_world(), show=True)
    compare("ephemeral migrate (Nova port reserve, sha512, guest props) + rollback",
            [["migrate", "eph01"], EROLL], world=eph_world(), show=True)
    compare("ephemeral root + data volume + rollback", [["migrate", "eph01", "--verify", "none"], EROLL],
            world=eph_world(True), show=True)
    W = eph_world()
    W["dst"]["hash_algo"] = "md5"
    compare("ephemeral: Glance hash algo not sha512 (WARN)", [["migrate", "eph01"], EROLL], world=W)
    W = eph_world()
    W["dst"]["hash_bad"] = True
    compare("ephemeral: uploaded hash mismatch (die) + rollback", [["migrate", "eph01"], EROLL], world=W, show=True)
    W = eph_world()
    W["src"]["snap_props"] = {"os_hash_algo": "sha512", "stores": "file"}
    compare("ephemeral: no guest property to copy (no PATCH)", [["migrate", "eph01"], EROLL], world=W)
    for what in ("put", "patch"):
        W = eph_world()
        W["dst"]["glance_fail"] = what
        # curl -sS prints its own error line on stderr before the die message; the Python build does not.
        compare("ephemeral: Glance %s fails (die) + rollback" % what, [["migrate", "eph01"], EROLL], world=W,
                expect_diff=("err",), show=True)
    W = eph_world()
    W["dst"]["glance_fail"] = "post"
    # bash: the failed curl in dimg=$(...) stops the script with curl's exit code 22 and no ERROR line.
    compare("ephemeral: Glance POST fails + rollback", [["migrate", "eph01"], EROLL], world=W,
            expect_diff=("err", "rc"), show=True)
    compare("ephemeral --volumes-only (die)", ["migrate", "eph01", "--volumes-only"], world=eph_world(True))
    compare("ephemeral --mode live (FAIL)", ["migrate", "eph01", "--mode", "live"], world=eph_world())
    W = eph_world()
    W["src"]["snap_status"] = "killed"
    compare("ephemeral: snapshot killed (die) + rollback", [["migrate", "eph01"], EROLL], world=W)
if sc("eph-cross"):
    seq = [["migrate", "eph01"], EROLL]
    compare("ephemeral cross: bash migrate -> py rollback", seq, world=eph_world(True), impls=("bash", "bash+py"))
    compare("ephemeral cross: py migrate -> bash rollback", seq, world=eph_world(True), impls=("bash", "py+bash"))
    W = eph_world()
    W["dst"]["hash_bad"] = True
    compare("ephemeral cross: py dies after upload -> bash rollback", seq, world=W, impls=("bash", "py+bash"))


def multinic_world():
    W = base_world()
    s = W["src"]
    s["networks"]["net-src-2"] = {"name": "vlan121"}
    s["subnets"]["sub-src-2"] = {"cidr": "198.51.101.0/24", "network_id": "net-src-2"}
    s["ports"]["port-src-2"] = {"name": "demo-port2", "network_id": "net-src-2",
                                "fixed_ips": [{"subnet_id": "sub-src-2", "ip_address": "198.51.101.51"}],
                                "mac_address": "fa:16:3e:aa:bb:02", "security_group_ids": ["sg-src-web"],
                                "device_id": "srv-src-app01", "port_security_enabled": True, "allowed_address_pairs": []}
    s["servers"]["srv-src-app01"]["ports"].append("port-src-2")
    s["servers"]["srv-src-app01"]["properties"] = {"env": "prod", "app": "demo"}
    d = W["dst"]
    d["networks"]["net-dst-2"] = {"name": "vlan121"}
    d["subnets"]["sub-dst-2"] = {"cidr": "198.51.101.0/24", "network_id": "net-dst-2"}
    d["flavors"]["mig.small2"] = {"vcpus": 1, "ram": 1024}
    d["security_groups"]["sg-dst-b"] = {"name": "mig-sg-b", "project_id": "pdst"}
    d["volume_types"].append("mig-typeb")
    d["type_pool"]["mig-typeb"] = "mig-volumes"
    return W


if sc("multinic"):
    compare("extras A shape: 2 NICs on 2 networks, --flavor + --security-group override, properties + rollback",
            [["migrate", "app01", "--flavor", "mig.small2", "--security-group", "mig-sg-b"], ROLL],
            world=multinic_world(), show=True)
    compare("2 NICs, source SGs per port (no override) + rollback", [["migrate", "app01", "--verify", "none"], ROLL],
            world=multinic_world())
    compare("2 NICs --ip new (pending rename per port) + rollback", [["migrate", "app01", "--ip", "new"], ROLL],
            world=multinic_world())
    # Intended: 7.2 B4: the address is outside the second NIC's subnet: one more FAIL line.
    compare("2 NICs --ip addr refused (single-NIC only)", ["plan", "app01", "--ip", "198.51.100.77"], world=multinic_world(),
            bash_subs=(("  OK    subnet 198.51.101.0/24 on vlan121\n", "  OK    subnet 198.51.101.0/24 on vlan121\n"
                        "  FAIL  IP 198.51.100.77 is not in subnet 198.51.101.0/24 of vlan121\n"),))
if sc("newip"):
    compare("newip-voltype shape: --ip new --volume-type mig-typeb --start-source + rollback",
            [["migrate", "app01", "--ip", "new", "--volume-type", "mig-typeb", "--start-source"], ROLL],
            world=multinic_world(), show=True)
    compare("--volume-type nvme with RBD_POOL_MAP + rollback", [["migrate", "app01", "--volume-type", "nvme"], ROLL],
            env={"RBD_POOL_MAP": "nvme:volumes-nvme,x:y"})

# ---------------- phase 4: selector CLI vs migrate-batch.sh, volumes, volume rollback ----------------
import os  # noqa: E402

L1, L2, L3, L4 = ("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222",
                  "33333333-3333-4333-8333-333333333333", "44444444-4444-4444-8444-444444444444")
VOL = {"bootable": "false", "encrypted": False, "multiattach": False, "size": 1}
# Real Nova IDs: '--vm <id>' finds a VM by name first, then (UUID only) by 'server show'.
ID1, ID2 = "aaaaaaaa-0000-4000-8000-0000000000a1", "aaaaaaaa-0000-4000-8000-0000000000a2"


def p4_world(app02=True, lone=True):
    W = base_world()
    s = W["src"]
    s["servers"][ID1] = s["servers"].pop("srv-src-app01")
    s["servers"][ID1]["project_id"] = "psrc"
    s["ports"]["port-src-1"]["device_id"] = ID1
    if app02:
        s["servers"][ID2] = {
            "project_id": "psrc",
            "name": "app02", "status": "ACTIVE", "locked": False, "locked_reason": None, "host": "cmp2",
            "flavor": {"original_name": "m1.small", "name": "m1.small", "vcpus": 2, "ram": 2048},
            "key_name": "k1", "properties": {}, "description": None, "ports": ["port-src-a2"],
            "volumes": [{"Volume ID": "vol-src-a2", "Device": "/dev/vda"}]}
        s["ports"]["port-src-a2"] = {"name": "", "network_id": "net-src-1",
                                     "fixed_ips": [{"subnet_id": "sub-src-1", "ip_address": "198.51.100.52"}],
                                     "mac_address": "fa:16:3e:aa:bb:03", "security_group_ids": ["sg-src-default"],
                                     "device_id": ID2, "port_security_enabled": True,
                                     "allowed_address_pairs": []}
        s["volumes"]["vol-src-a2"] = dict(VOL, name="", status="in-use", bootable="true")
    if lone:
        s["volumes"][L1] = dict(VOL, name="lone1", status="available", type="lvm")
        s["volumes"][L2] = dict(VOL, name="", status="available")
        s["volumes"][L3] = dict(VOL, name="mig-clone-xyz", status="available")
        s["volumes"][L4] = dict(VOL, name="broken", status="error")
    return W


def state_of(*names):
    def pre(root):
        for n in names:
            os.makedirs(os.path.join(root, n))
            open(os.path.join(root, n, "volume-map"), "w").write("")
    return pre


def solo(name, impl, args, world, env=None, want=(), unwanted=(), rc=None, conf=None):
    """One implementation alone (behaviour the bash build has no counterpart for). P6: written before P6,
    so the app side runs with the legacy opt-outs, as in compare()."""
    r = P.run(impl, P.copy.deepcopy(world), args, env, conf, optouts=not os.environ.get("HARNESS_NO_OPTOUTS"))
    if r["transformed"]:
        P.TRANSFORMED.append(name)
    text = r["out"] + r["err"]
    bad = ["missing: %r" % w_ for w_ in want if w_ not in text] + ["unexpected: %r" % u for u in unwanted if u in text]
    if rc is not None and r["rc"][-1] != rc:
        bad.append("rc %s, want %s" % (r["rc"], rc))
    if bad:
        P.FAIL += 1
        print("FAIL %s\n  %s\n%s" % (name, "\n  ".join(bad), "\n".join("      | " + l for l in text.splitlines()[-40:])))
    else:
        P.PASS += 1
        print("PASS %s (rc=%s)" % (name, r["rc"]))
    return r


BOTH = ("batch+bash", "app+app")
# bash: '$sp: N in-use volume(s) not listed — they go with their VM (--volumes attached)'; the CLI names its own option.
SUB_INUSE = (("(--volumes attached)", "(PROG volume --attached)"),)

if sc("p4-one"):
    compare("P4 one VM by ID: batch --vm ID --yes == migrate --vm ID --yes; + rollback",
            [["--vm", ID1, "--yes", "--", "--verify", "none"], ["rollback", ID1]], world=p4_world(), impls=BOTH,
            py_args=[["migrate", "--vm", ID1, "--yes", "--verify", "none"], ["rollback", ID1]], show=True)
    compare("P4 positional <vm> = --vm <vm>",
            [["--vm", ID1, "--yes", "--", "--verify", "none"]], world=p4_world(), impls=("batch", "app"),
            py_args=[["migrate", ID1, "--yes", "--verify", "none"]])


def by_name_check(b, p):
    bad = []
    rb = ".rolled-back.<epoch>/server.json"  # rolled back afterwards
    if ID1 + rb not in b["files"]:
        bad.append("batch state should be under the ID")
    if "app01" + rb not in p["files"] or ID1 + rb in p["files"]:
        bad.append("app state should be under the typed name app01")
    if "Rollback: PROG rollback app01" not in p["out"] or "SKIP(" in p["out"]:
        bad.append("unexpected SKIP/hint")
    return bad


if sc("p4-name"):
    # Intended: a --vm value naming exactly one VM is what the child runs with (state app01/, rollback app01),
    # as a single migrate-vm.sh run; the batch always passed the ID. Same clouds afterwards.
    compare("P4 --vm <name>: child runs with the typed name (state dir app01/) — expected diff out/files/calls",
            [["--vm", "app01", "--yes", "--", "--verify", "none"], ["rollback", ID1]], world=p4_world(), impls=BOTH,
            py_args=[["migrate", "--vm", "app01", "--yes", "--verify", "none"], ["rollback", "app01"]],
            expect_diff=("out", "files", "calls"), check=by_name_check)

if sc("p4-project"):
    W = p4_world()
    W["src"]["servers"]["aaaaaaaa-0000-4000-8000-0000000000a3"] = dict(W["src"]["servers"][ID2], name="app03", status="SHUTOFF",
                                                ports=[], volumes=[])
    W["src"]["servers"]["aaaaaaaa-0000-4000-8000-0000000000a4"] = dict(W["src"]["servers"][ID2], name="app04", status="ERROR",
                                                ports=[], volumes=[])
    compare("P4 --project: SKIP(status), SKIP(state-exists)+hint, 2 migrated (--continue-on-error)",
            ["--project", "admin", "--yes", "--continue-on-error", "--", "--verify", "none"], world=W, impls=("batch", "app"),
            py_args=["migrate", "--project", "admin", "--yes", "--continue-on-error", "--verify", "none"],
            pre_state=state_of("app03"), show=True)

if sc("p4-host"):
    compare("P4 --host cmp2 (Host column)", ["--host", "cmp2", "--yes", "--", "--verify", "none"], world=p4_world(),
            impls=("batch", "app"), py_args=["migrate", "--host", "cmp2", "--yes", "--verify", "none"])
    compare("P4 --host cmp1,cmp2 --project admin (intersect)",
            ["--host", "cmp1,cmp2", "--project", "admin", "--yes", "--", "--verify", "none"], world=p4_world(),
            impls=("batch", "app"), py_args=["migrate", "--host", "cmp1,cmp2", "--project", "admin", "--yes",
                                             "--verify", "none"])
    compare("P4 --host nohost: WARN, nothing selected (rc 0)", ["--host", "nohost", "--yes"], world=p4_world(),
            impls=("batch", "app"), py_args=["migrate", "--host", "nohost", "--yes"], show=True)

if sc("p4-vmfile"):
    W = p4_world()
    W["src"]["servers"]["aaaaaaaa-0000-4000-8000-0000000000d1"] = dict(W["src"]["servers"][ID2], name="dup", ports=[], volumes=[])
    W["src"]["servers"]["aaaaaaaa-0000-4000-8000-0000000000d2"] = dict(W["src"]["servers"][ID2], name="dup", ports=[], volumes=[])
    vf = os.path.join(P.W, "vmfile-p4")
    open(vf, "w").write("# selection\n  %s  \n\n%s # trailing comment\n%s\nnosuch" % (ID1, ID2, ID1))
    compare("P4 --vm-file (comments, trim, dedupe, no final newline) + --vm: not-found, ambiguous, 2 migrated",
            ["--vm", "dup", "--vm-file", vf, "--yes", "--", "--verify", "none"], world=W, impls=("batch", "app"),
            py_args=["migrate", "--vm", "dup", "--vm-file", vf, "--yes", "--verify", "none"], show=True)

if sc("p4-nothing"):
    # Intended: the batch exits 0 when nothing ran; a 'migrate migrate' that migrated nothing exits 1.
    compare("P4 --vm nosuch: SKIP(not-found) — expected diff rc (batch 0, app 1)", ["--vm", "nosuch", "--yes"],
            world=p4_world(), impls=("batch", "app"), py_args=["migrate", "--vm", "nosuch", "--yes"],
            expect_diff=("rc",), check=lambda b, p: [] if (b["rc"], p["rc"]) == ([0], [1]) else ["rc %s/%s" % (b["rc"], p["rc"])])

if sc("p4-planfail"):
    W = p4_world()
    W["src"]["servers"][ID2]["flavor"] = {"original_name": "m1.big", "name": "m1.big"}
    compare("P4 PLAN-FAIL first stops the run: FAIL(plan), SKIP(not-run)",
            ["--vm", "%s,%s" % (ID2, ID1), "--yes", "--", "--verify", "none"], world=W, impls=("batch", "app"),
            py_args=["migrate", "--vm", "%s,%s" % (ID2, ID1), "--yes", "--verify", "none"], show=True)
    compare("P4 PLAN-FAIL --continue-on-error: the other migrates",
            ["--vm", "%s,%s" % (ID2, ID1), "--yes", "--continue-on-error", "--", "--verify", "none"], world=W,
            impls=("batch", "app"),
            py_args=["migrate", "--vm", "%s,%s" % (ID2, ID1), "--yes", "--continue-on-error", "--verify", "none"])

if sc("p4-migfail"):
    compare("P4 migrate FAIL: hint with project pair, the rest SKIP(not-run); rollback as hinted",
            [["--vm", "%s,%s" % (ID1, ID2), "--yes"], ["rollback", ID1]], world=p4_world(), impls=BOTH,
            py_args=[["migrate", "--vm", "%s,%s" % (ID1, ID2), "--yes"], ["rollback", ID1]],
            env={"HARNESS_COMPARE_FAIL": "1"}, show=True,
            expect_diff=("out", "files", "calls", "world"), check=a1_late_check)  # Intended: 7.2 A1

if sc("p4-pmap"):
    pm = os.path.join(P.W, "pmap-p4")
    open(pm, "w").write("# source destination\nadmin tenant-b   # renamed\n\nother x\n")
    compare("P4 PROJECT_MAP_FILE: DST_PROJECT from the map", ["--vm", ID1, "--yes", "--", "--verify", "none"],
            world=p4_world(), impls=("batch", "app"), py_args=["migrate", "--vm", ID1, "--yes", "--verify", "none"],
            env={"PROJECT_MAP_FILE": pm}, show=True)
    bad = os.path.join(P.W, "pmap-bad")
    open(bad, "w").write("admin a b\n")
    compare("P4 PROJECT_MAP_FILE malformed line dies", ["--vm", ID1, "--yes"], world=p4_world(), impls=("batch", "app"),
            py_args=["migrate", "--vm", ID1, "--yes"], env={"PROJECT_MAP_FILE": bad})
    compare("P4 PROJECT_MAP_FILE unreadable: WARN", ["--vm", ID1, "--yes", "--", "--verify", "none"], world=p4_world(),
            impls=("batch", "app"), py_args=["migrate", "--vm", ID1, "--yes", "--verify", "none"],
            env={"PROJECT_MAP_FILE": "/nonexistent/map"})
    compare("P4 PROJECT_MAP_FILE from MIG_CONF", ["--vm", ID1, "--yes", "--", "--verify", "none"], world=p4_world(),
            impls=("batch", "app"), py_args=["migrate", "--vm", ID1, "--yes", "--verify", "none"],
            conf="PROJECT_MAP_FILE=%s\n" % pm)

if sc("p4-confirm"):
    compare("P4 no --yes, stdin not a terminal: dies after the Plan table", ["--vm", ID1, "--", "--verify", "none"],
            world=p4_world(), impls=("batch", "app"), py_args=["migrate", "--vm", ID1, "--verify", "none"], show=True)
    compare("P4 terminal, answer yes", ["--vm", ID1, "--", "--verify", "none"], world=p4_world(), impls=("batch", "app"),
            py_args=["migrate", "--vm", ID1, "--verify", "none"], tty="yes\n")
    compare("P4 terminal, answer no", ["--vm", ID1, "--", "--verify", "none"], world=p4_world(), impls=("batch", "app"),
            py_args=["migrate", "--vm", ID1, "--verify", "none"], tty="no\n", show=True)
    W = p4_world()
    W["src"]["servers"][ID2]["flavor"] = {"original_name": "m1.big", "name": "m1.big"}
    compare("P4 terminal, a PLAN-FAIL row: Note line, answer yes", ["--vm", "%s,%s" % (ID2, ID1)], world=W,
            impls=("batch", "app"), py_args=["migrate", "--vm", "%s,%s" % (ID2, ID1)], tty=" yes \n")

if sc("p4-plan"):
    # Intended: 'migrate plan' shows each preflight while it runs (the batch kept them in the log only).
    compare("P4 plan == --plan-only (+ the preflights on stdout)", ["--vm", "%s,%s" % (ID1, ID2), "--plan-only"],
            world=p4_world(), impls=("batch", "app"), py_args=["plan", "--vm", "%s,%s" % (ID1, ID2)],
            expect_diff=("out",), out_subseq=True, show=True)
    W = p4_world()
    W["src"]["servers"][ID2]["flavor"] = {"original_name": "m1.big", "name": "m1.big"}
    compare("P4 plan with a PLAN-FAIL: exit 1", ["--project", "admin", "--plan-only"], world=W, impls=("batch", "app"),
            py_args=["plan", "--project", "admin"], expect_diff=("out",), out_subseq=True)

if sc("p4-listing"):
    W = p4_world()
    W["src"]["no_project_column"] = True
    for s_ in W["src"]["servers"].values():
        s_["project_id"] = "psrc"
    compare("P4 no 'Project ID' column: server show per VM", ["--vm", ID1, "--yes", "--", "--verify", "none"], world=W,
            impls=("batch", "app"), py_args=["migrate", "--vm", ID1, "--yes", "--verify", "none"])
    W = p4_world()
    W["src"]["servers"][ID1]["name"] = "weird.name[1]*(x)|y$"
    # Listing escaped like the batch's sed; a typed name with regex characters runs its child by ID.
    compare("P4 --vm <name with regex characters>: escaped listing, child by ID",
            [["--vm", "weird.name[1]*(x)|y$", "--yes", "--", "--verify", "none"], ["rollback", ID1]], world=W,
            impls=BOTH, py_args=[["migrate", "--vm", "weird.name[1]*(x)|y$", "--yes", "--verify", "none"],
                                 ["rollback", ID1]],
            # Intended: 7.2 C1: the destination duplicate check escapes the name too.
            ref_calls_subs=(("--name ^weird.name[1]*(x)|y$$ ", r"--name ^weird\.name\[1\]\*\(x\)\|y\$$ "),))

if sc("p4-volumes"):
    compare("P4 volumes available: READY rows, mig-clone/status SKIPs, VM-only options dropped; + rollback both",
            [["--project", "admin", "--volumes", "available", "--yes", "--", "--verify", "none", "--start-source"],
             ["rollback", L1], ["rollback", L2]], world=p4_world(), impls=BOTH,
            py_args=[["volume", "--project", "admin", "--available", "--yes", "--verify", "none", "--start-source"],
                     ["rollback", L1], ["rollback", L2]], bash_subs=SUB_INUSE, show=True)
    compare("P4 volumes attached --vm ID: --volumes-only children; + rollback",
            [["--volumes", "attached", "--vm", ID1, "--yes", "--", "--verify", "none"], ["rollback", ID1]],
            world=p4_world(), impls=BOTH,
            py_args=[["volume", "--vm", ID1, "--yes", "--verify", "none"], ["rollback", ID1]])
    compare("P4 volumes all --project: attached first, then available",
            ["--project", "admin", "--volumes", "all", "--yes", "--", "--verify", "none"], world=p4_world(),
            impls=("batch", "app"), py_args=["volume", "--project", "admin", "--all", "--yes", "--verify", "none"],
            bash_subs=SUB_INUSE, show=True)
    compare("P4 volumes available: SKIP(state-exists) + hint by volume ID",
            ["--project", "admin", "--volumes", "available", "--yes", "--continue-on-error"], world=p4_world(),
            impls=("batch", "app"), py_args=["volume", "--project", "admin", "--available", "--yes", "--continue-on-error"],
            bash_subs=SUB_INUSE, pre_state=state_of("vol-" + L1))
    compare("P4 volumes attached --project with an ephemeral VM: SKIP(ephemeral-root), the other copied",
            ["--volumes", "attached", "--project", "admin", "--yes"], world=eph_world(), impls=("batch", "app"),
            py_args=["volume", "--project", "admin", "--attached", "--yes"], show=True)
    W = p4_world()
    W["dst"]["create_status"] = "error"
    compare("P4 volume batch FAIL (dest volume error): hint by ID, SKIP(not-run); rollback",
            [["--project", "admin", "--volumes", "available", "--yes"], ["rollback", L1]], world=W, impls=BOTH,
            py_args=[["volume", "--project", "admin", "--available", "--yes"], ["rollback", L1]], bash_subs=SUB_INUSE)
if sc("p4-voldefaults"):
    # P6 (intended): the VM-only flag in MIG_DEFAULT_OPTS is --keep-source-ports (was --keep-server-group, which the
    # app no longer knows: every volume child would die on it) — for both builds; same point, a flag volumes ignore.
    compare("P4 volumes available with VM-only MIG_DEFAULT_OPTS (--mode live --keep-source-ports): ignored, copied",
            ["--project", "admin", "--volumes", "available", "--yes"], world=p4_world(), impls=("batch", "app"),
            py_args=["volume", "--project", "admin", "--available", "--yes"], bash_subs=SUB_INUSE,
            env={"MIG_DEFAULT_OPTS": "--mode live --keep-source-ports --verify none"})

VB = ("bash+bash", "app+app")
if sc("p4-vol1"):
    compare("P4 volume <name> + rollback <name>", [["migrate-volume", "lone1", "--verify", "none"], ["rollback", "lone1"]],
            world=p4_world(), impls=VB, py_args=[["volume", "lone1", "--verify", "none"], ["rollback", "lone1"]], show=True)
    compare("P4 volume <id> + rollback vol-<id>", [["migrate-volume", L1], ["rollback", "vol-" + L1]], world=p4_world(),
            impls=VB, py_args=[["volume", L1], ["rollback", "vol-" + L1]])
    compare("P4 volume twice (state exists) + rollback <id>",
            [["migrate-volume", L1, "--verify", "none"], ["migrate-volume", "lone1"], ["rollback", L1]], world=p4_world(),
            impls=("bash+bash+bash", "app+app+app"),
            py_args=[["volume", L1, "--verify", "none"], ["volume", "lone1"], ["rollback", L1]])
    W = p4_world()
    W["dst"]["refuse_empty_name"] = True
    compare("P4 volume without a name, destination refuses '': WARN, named by ID; + rollback",
            [["migrate-volume", L2, "--verify", "none"], ["rollback", L2]], world=W, impls=VB,
            py_args=[["volume", L2, "--verify", "none"], ["rollback", L2]], show=True)
    compare("P4 volume --az --volume-type with RBD_POOL_MAP", [["migrate-volume", L1, "--az", "AZ1", "--volume-type", "nvme"],
                                                               ["rollback", L1]],
            world=p4_world(), impls=VB, py_args=[["volume", L1, "--az", "AZ1", "--volume-type", "nvme"], ["rollback", L1]],
            env={"RBD_POOL_MAP": "nvme:volumes-nvme"})
    compare("P4 volume MIG_DEFAULT_OPTS: --in-use and server options ignored with a note",
            [["migrate-volume", L1], ["rollback", L1]], world=p4_world(), impls=VB,
            py_args=[["volume", L1], ["rollback", L1]], env={"MIG_DEFAULT_OPTS": "--in-use --ip new --verify none"})
if sc("p4-vol-inuse"):
    compare("P4 volume in-use without --in-use dies", ["migrate-volume", "data"], world=p4_world(), impls=("bash", "app"),
            py_args=["volume", "data"])
    compare("P4 volume --in-use: crash-consistent copy, clone-time; + rollback",
            [["migrate-volume", "data", "--in-use"], ["rollback", "data"]], world=p4_world(), impls=VB,
            py_args=[["volume", "data", "--in-use"], ["rollback", "data"]], show=True)
    W = p4_world()
    W["src"]["volumes"]["vol-src-data"]["multiattach"] = True
    compare("P4 volume --in-use multiattach FAIL", ["migrate-volume", "data", "--in-use"], world=W, impls=("bash", "app"),
            py_args=["volume", "data", "--in-use"])
    compare("P4 volume --in-use on an available volume: INFO ignored", [["migrate-volume", L1, "--in-use"], ["rollback", L1]],
            world=p4_world(), impls=VB, py_args=[["volume", L1, "--in-use"], ["rollback", L1]])
if sc("p4-vol-dies"):
    W = p4_world()
    W["dst"]["volumes"]["vol-dst-old"] = dict(VOL, name="lone1", status="available", description="migrated from " + L1)
    compare("P4 volume: earlier copy at destination FAIL", ["migrate-volume", L1], world=W, impls=("bash", "app"),
            py_args=["volume", L1])
    W = p4_world()
    W["dst"]["sdk_fail"] = True
    compare("P4 volume: SDK lookup fails", ["migrate-volume", L1], world=W, impls=("bash", "app"), py_args=["volume", L1])
    compare("P4 volume: not found", ["migrate-volume", "nosuch"], world=p4_world(), impls=("bash", "app"),
            py_args=["volume", "nosuch"])
    compare("P4 volume: status error", ["migrate-volume", L4], world=p4_world(), impls=("bash", "app"),
            py_args=["volume", L4])
    W = p4_world()
    W["src"]["volumes"]["55555555-5555-4555-8555-555555555555"] = dict(VOL, name="lone1", status="available")
    compare("P4 volume: duplicate name", ["migrate-volume", "lone1"], world=W, impls=("bash", "app"),
            py_args=["volume", "lone1"])
    # Intended rewording: the command is 'migrate volume' now (and it copies in-use volumes too, with --in-use).
    # P6 (intended): the server-only flag is --keep-source-ports (was --keep-server-group: unknown to the app in 7.1).
    compare("P4 volume: server-only options refused", ["migrate-volume", L1, "--ip", "new", "--mode", "live",
                                                       "--keep-source-ports"],
            world=p4_world(), impls=("bash", "app"), py_args=["volume", L1, "--ip", "new", "--mode", "live",
                                                             "--keep-source-ports"],
            bash_subs=(("migrate-volume copies one available volume", "PROG volume copies one volume"),
                       # Intended: 7.2 C6: a lone volume takes --volume-az too
                       ("(it takes --volume-type, --az and --verify)", "(it takes --volume-type, --az, --volume-az and --verify)")))
    compare("P4 rollback with no state at all", ["rollback", "nosuch"], world=p4_world(), impls=("bash", "app"))
if sc("p4-vol-cross"):
    seq = [["migrate-volume", L1, "--verify", "none"], ["rollback", L1]]
    pseq = [["volume", L1, "--verify", "none"], ["rollback", L1]]
    compare("P4 volume cross: bash migrate-volume -> app rollback", seq, world=p4_world(), impls=("bash+bash", "bash+app"))
    compare("P4 volume cross: app volume -> bash rollback", seq, world=p4_world(), impls=("bash+bash", "app+bash"),
            py_args=[pseq[0], seq[1]])
    W = p4_world()
    W["dst"]["create_status"] = "error"
    compare("P4 volume cross: app dies at the destination volume -> bash rollback", seq, world=W,
            impls=("bash+bash", "app+bash"), py_args=[pseq[0], seq[1]])
    compare("P4 volume cross: bash dies at the destination volume -> app rollback", seq, world=W,
            impls=("bash+bash", "bash+app"))

if sc("p4-appcred"):
    AC = {"SRC_RC": os.path.join(P.W, "src-ac.rc"), "DST_RC": os.path.join(P.W, "dst-ac.rc")}
    W = p4_world()
    W["src"]["forbid_all_projects"] = True

    def appcred_check(b, p):
        bad = []
        sb = {k: v for k, v in b["files"].items() if not k.startswith("log/")}
        sp = {k: v for k, v in p["files"].items() if not k.startswith("log/")}
        if sb != sp:
            bad.append("state differs: %s" % sorted(set(sb) ^ set(sp)))
        if p["calls"][-len(b["calls"]):] != b["calls"]:
            bad.append("the migrate child's calls are not the single run's calls")
        if "SRC_PROJECT=" in p["out"] or "DST_PROJECT=" in p["out"]:
            bad.append("project env shown")
        return bad
    # Intended: the batch refused application credentials; one VM in the credential's own project
    # runs unscoped, exactly as a single migrate-vm.sh run (no admin: listing without --all-projects).
    compare("P4 appcred, own project: same as single migrate-vm.sh (state, calls tail, world)",
            [["migrate", "app01", "--verify", "none"], ["rollback", "app01"]], world=W, impls=("bash+bash", "app+app"),
            py_args=[["migrate", "app01", "--yes", "--verify", "none"], ["rollback", "app01"]], env=AC,
            expect_diff=("out", "files", "calls"), check=appcred_check)
    W = p4_world()
    W["src"]["projects"] = {"psrc": {"name": "admin"}, "pother": {"name": "other"}}
    W["src"]["servers"]["srv-src-oth"] = dict(W["src"]["servers"][ID2], name="oth01", project_id="pother", ports=[],
                                              volumes=[])
    solo("P4 appcred, VM of another project: PLAN-FAIL with the appcred message, own VM migrates", "app",
         ["migrate", "--vm", "oth01,app01", "--yes", "--continue-on-error", "--verify", "none"], W, env=AC,
         want=("SRC_PROJECT/DST_PROJECT needs a password openrc", "FAIL(plan)", "Totals: OK 1  FAIL 1  SKIP 0"),
         rc=1)
    solo("P4 batch refuses appcred (reference)", "batch", ["--vm", "app01", "--yes"], p4_world(), env=AC,
         want=("is an application credential",), rc=1)

# ---------------- phase 5: 'migrate plan volume' vs the batch's '--volumes ... --plan-only' ----------------
if sc("p5-planvol"):
    compare("P5 plan volume --available == --volumes available --plan-only (READY rows, SKIPs, no child, rc 0)",
            ["--project", "admin", "--volumes", "available", "--plan-only"], world=p4_world(), impls=("batch", "app"),
            py_args=["plan", "volume", "--project", "admin", "--available"], bash_subs=SUB_INUSE, show=True)
    compare("P5 plan volume --available with an earlier state: SKIP(state-exists) + rollback hint by ID",
            ["--project", "admin", "--volumes", "available", "--plan-only"], world=p4_world(), impls=("batch", "app"),
            py_args=["plan", "volume", "--project", "admin", "--available"], bash_subs=SUB_INUSE,
            pre_state=state_of("vol-" + L1))
    # Intended (as 'migrate plan'): the VM preflights are shown while they run; the batch kept them in the log.
    compare("P5 plan volume --vm ID == --volumes attached --vm ID --plan-only (+ preflight on stdout)",
            ["--volumes", "attached", "--vm", ID1, "--plan-only", "--", "--verify", "none"], world=p4_world(),
            impls=("batch", "app"), py_args=["plan", "volume", "--vm", ID1, "--verify", "none"],
            expect_diff=("out",), out_subseq=True, show=True)
    compare("P5 plan volume --project --attached == --volumes attached --project --plan-only",
            ["--volumes", "attached", "--project", "admin", "--plan-only"], world=p4_world(), impls=("batch", "app"),
            py_args=["plan", "volume", "--project", "admin", "--attached"], expect_diff=("out",), out_subseq=True)
    compare("P5 plan volume --project --all == --volumes all --plan-only (VMs planned, then READY volumes)",
            ["--project", "admin", "--volumes", "all", "--plan-only"], world=p4_world(), impls=("batch", "app"),
            py_args=["plan", "volume", "--project", "admin", "--all"], bash_subs=SUB_INUSE, expect_diff=("out",),
            out_subseq=True, show=True)
    compare("P5 plan volume --vm with a failing preflight (bogus type): PLAN-FAIL, rc 1 both",
            ["--volumes", "attached", "--vm", "%s,%s" % (ID1, ID2), "--plan-only", "--", "--volume-type", "bogus"],
            world=p4_world(), impls=("batch", "app"),
            py_args=["plan", "volume", "--vm", "%s,%s" % (ID1, ID2), "--volume-type", "bogus"],
            expect_diff=("out",), out_subseq=True)
    # P6 (intended): --keep-source-ports instead of --keep-server-group in MIG_DEFAULT_OPTS (see p4-voldefaults).
    compare("P5 plan volume --available, MIG_DEFAULT_OPTS with VM-only options: unchanged listing",
            ["--project", "admin", "--volumes", "available", "--plan-only"], world=p4_world(), impls=("batch", "app"),
            py_args=["plan", "volume", "--project", "admin", "--available"], bash_subs=SUB_INUSE,
            env={"MIG_DEFAULT_OPTS": "--mode live --keep-source-ports --verify none"})
    W = p4_world(lone=False)
    compare("P5 plan volume --available, no available volume: 'nothing selected', rc 0 both",
            ["--project", "admin", "--volumes", "available", "--plan-only"], world=W, impls=("batch", "app"),
            py_args=["plan", "volume", "--project", "admin", "--available"], bash_subs=SUB_INUSE)
    W = p4_world()
    for k in (L1, L2):
        W["src"]["volumes"].pop(k)

    def all_skip(b, p):
        return [] if b["rc"] == [0] and p["rc"] == [1] else ["rc: batch %s app %s, want [0] [1]" % (b["rc"], p["rc"])]
    # Intended (P4, every selector run): nothing selected could be planned -> the app exits 1, the batch 0.
    compare("P5 plan volume --available, only SKIP rows: app rc 1 (batch 0), output identical",
            ["--project", "admin", "--volumes", "available", "--plan-only"], world=W, impls=("batch", "app"),
            py_args=["plan", "volume", "--project", "admin", "--available"], bash_subs=SUB_INUSE,
            expect_diff=("rc",), check=all_skip)
    solo("P5 migrate volume ... --plan-only: hint to plan volume, nothing listed", "app",
         ["volume", "--project", "admin", "--available", "--plan-only"], p4_world(),
         want=("--plan-only is now a command of its own: PROG plan volume --project <p>",), rc=1)
    solo("P5 plan volume <volume>: no dry-run for one volume", "app", ["plan", "volume", L1], p4_world(),
         want=("A single volume has no dry-run",), rc=1)
    solo("P5 plan --project --volumes available: hint to plan volume", "app",
         ["plan", "--project", "admin", "--volumes", "available"], p4_world(),
         want=("--volumes is now: PROG plan volume --project <p>",), rc=1)

exec(open(os.path.join(P.H, "scenarios_p6.py")).read())

for n_ in P.TRANSFORMED:
    print("OPTOUTS " + n_)
print("OPTOUTS total=%d (app side ran with the P6 legacy opt-outs)" % len(P.TRANSFORMED))
print("TOTAL pass=%d fail=%d" % (P.PASS, P.FAIL))
