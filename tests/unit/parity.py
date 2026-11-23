#!/usr/bin/python3
"""Parity harness V7-P1: run migrate-vm.sh (bash, reference) and the Python 'migrate' against the
same fake clouds and compare stdout/stderr, exit code, state files, every external call, final world."""
import copy
import difflib
import json
import os
import re
import shutil
import subprocess
import sys

# H: this directory (stubs, scenarios). REPO: the repository (migratetool/, archive/ = the bash reference).
# W: scratch directory for everything generated (stub links, patched copies, one run-* directory per run),
# never inside the repository: $MIG_UNIT_WORK, default /tmp/mig-unit-work.
H = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(H))
W = os.environ.get("MIG_UNIT_WORK") or "/tmp/mig-unit-work"
BIN = os.path.join(W, "bin")
BLOCKDEV = "/dev/loop0"
PASS = FAIL = 0
TRANSFORMED = []  # P6: scenarios whose app side ran with legacy_optouts (listed at the end of a run)

# --- fake Glance v2 REST (the ephemeral path talks to it with curl / urllib) ---------------------
import hashlib  # noqa: E402
import threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

GLANCE = {"world": None, "calls": None}


class Glance(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _handle(self):
        body = self._body()
        w = json.load(open(GLANCE["world"]))
        d = w["dst"]
        d.setdefault("images", {})
        shown = body.decode(errors="replace") if self.command in ("POST", "PATCH") else "<%d bytes>" % len(body)
        with open(GLANCE["calls"], "a") as f:
            f.write("glance\t%s %s %s %s\n" % (self.command, self.path, self.headers.get("Content-Type"), shown))
        if self.headers.get("X-Auth-Token") != "tok-dst":
            return self._reply(401, b"")
        fail = d.get("glance_fail")
        if self.command == "POST" and self.path == "/v2/images":
            if fail == "post":
                return self._reply(500, b"boom")
            req = json.loads(body)
            w["counter"] += 1  # numbered per prefix, as stub_openstack.new_id (7.2)
            n = w.setdefault("ids", {}).get("img-dst", 0) + 1
            w["ids"]["img-dst"] = n
            iid = "img-dst-%04d" % n
            d["images"][iid] = {"name": req["name"], "status": "queued", "disk_format": req["disk_format"],
                                "container_format": req["container_format"], "properties": {}}
            json.dump(w, open(GLANCE["world"], "w"), indent=1)
            return self._reply(201, json.dumps({"id": iid, "status": "queued"}).encode())
        parts = self.path.strip("/").split("/")
        iid = parts[2] if len(parts) > 2 else ""
        im = d["images"].get(iid)
        if im is None:
            return self._reply(404, b"")
        if self.command == "PUT" and parts[-1] == "file":
            if fail == "put":
                return self._reply(500, b"boom")
            algo = d.get("hash_algo", "sha512")
            value = "bad" if d.get("hash_bad") else hashlib.sha512(body).hexdigest()
            im["properties"].update({"os_hash_algo": algo, "os_hash_value": value})
            im["status"] = "active"
            json.dump(w, open(GLANCE["world"], "w"), indent=1)
            return self._reply(204, b"")
        if self.command == "PATCH":
            if fail == "patch":
                return self._reply(400, b"bad patch")
            for op in json.loads(body):
                im["properties"][op["path"][1:]] = op["value"]
            json.dump(w, open(GLANCE["world"], "w"), indent=1)
            return self._reply(200, json.dumps({"id": iid}).encode())
        return self._reply(405, b"")

    def _reply(self, code, data):
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_POST = do_PUT = do_PATCH = _handle


_srv = ThreadingHTTPServer(("127.0.0.1", 0), Glance)
threading.Thread(target=_srv.serve_forever, daemon=True).start()
GLANCE_URL = "http://127.0.0.1:%d" % _srv.server_address[1]


def setup():
    os.makedirs(BIN, exist_ok=True)
    for name, target in (("openstack", "stub_openstack.py"), ("rbd", "stub_tools.py"), ("qemu-img", "stub_tools.py"),
                         ("systemctl", "stub_tools.py"), ("brick_attach.py", "stub_tools.py"), ("ceph", "stub_tools.py"),
                         ("python3", "stub_python3.py")):
        p = os.path.join(BIN, name)
        if os.path.lexists(p):
            os.remove(p)
        os.symlink(os.path.join(H, target), p)
        os.chmod(os.path.join(H, target), 0o755)

    def patch(text, pairs, what):
        for old, new in pairs:
            assert old in text, "%s: %r not found" % (what, old)
            text = text.replace(old, new)
        return text
    src = patch(open(os.path.join(REPO, "archive", "migrate-vm.sh")).read(), (
        ("source /opt/mig-venv/bin/activate", ": venv skipped"),
        ("STATE_ROOT=/var/lib/mig-migrate", "STATE_ROOT=$HARNESS_STATE"),
        ("BRICK=/usr/local/bin/brick_attach.py", "BRICK=$HARNESS_BIN/brick_attach.py"),
        ("BLOCKSYNC=/usr/local/bin/blocksync.py", "BLOCKSYNC=$HARNESS_BIN/blocksync.py"),
        ("/var/lib/mig-attach/", "$HARNESS_ATTACH/")), "migrate-vm.sh")
    shutil.copy(os.path.join(H, "stub_blocksync.py"), os.path.join(BIN, "blocksync.py"))
    os.chmod(os.path.join(BIN, "blocksync.py"), 0o755)
    open(os.path.join(W, "mv.sh"), "w").write(src)
    os.chmod(os.path.join(W, "mv.sh"), 0o755)  # migrate-batch.sh runs it directly ($MIGRATE_VM)
    batch = patch(open(os.path.join(REPO, "archive", "migrate-batch.sh")).read(), (
        ("source /opt/mig-venv/bin/activate", ": venv skipped"),
        ("STATE_ROOT=/var/lib/mig-migrate", "STATE_ROOT=$HARNESS_STATE"),
        ("LOG_DIR=/var/log/mig-batch/", "LOG_DIR=$HARNESS_LOG/")), "migrate-batch.sh")
    open(os.path.join(W, "mb.sh"), "w").write(batch)
    pkg = os.path.join(W, "pkg", "migratetool")
    if os.path.exists(os.path.join(W, "pkg")):
        shutil.rmtree(os.path.join(W, "pkg"))
    shutil.copytree(os.path.join(REPO, "migratetool"), pkg, ignore=shutil.ignore_patterns("__pycache__"))
    open(os.path.join(pkg, "brick.py"), "w").write(
        "import os\nexec(open(os.environ['HARNESS_TOOLS']).read())\n")
    shutil.copy(os.path.join(H, "stub_blocksync.py"), os.path.join(pkg, "blocksync.py"))
    for rc, side in (("src.rc", "src"), ("dst.rc", "dst")):
        open(os.path.join(W, rc), "w").write(
            "export OS_AUTH_URL=http://%s:5000/v3\nexport OS_AUTH_TYPE=password\nexport OS_PROJECT_NAME=admin\n" % side)
    for rc, side in (("src-ac.rc", "src"), ("dst-ac.rc", "dst")):
        open(os.path.join(W, rc), "w").write(
            "export OS_AUTH_URL=http://%s:5000/v3\nexport OS_AUTH_TYPE=v3applicationcredential\n"
            "export OS_APPLICATION_CREDENTIAL_ID=ac-%s\nexport OS_APPLICATION_CREDENTIAL_SECRET=x\n" % (side, side))
    # One patch point for the roots (config); children of a selector run come back through here.
    open(os.path.join(W, "pyrun.py"), "w").write("""import os, sys
sys.path.insert(0, os.environ["HARNESS_PKG"])
import migratetool.config as cfgm
cfgm.STATE_ROOT = os.environ["HARNESS_STATE"]
cfgm.ATTACH_DIR = os.environ["HARNESS_ATTACH"]
cfgm.LOG_ROOT = os.environ["HARNESS_LOG"]
import migratetool.selector as sel
sel.CHILD = [sys.executable, os.path.abspath(__file__), "__single__"]
# P6: the brick child runs 'python -I -m migratetool.brick', which ignores PYTHONPATH and so would not
# find this package: point it at the stub module file instead (it execs stub_tools.py as 'brick').
import migratetool.copyvol as cv
assert cv.BRICK[1:] == ["-I", "-m", "migratetool.brick"], cv.BRICK
cv.BRICK = [sys.executable, os.path.join(os.environ["HARNESS_PKG"], "migratetool", "brick.py")]
# 1.0.0 (B2): the SDK lookup of migrate volume runs sys.executable -I; the harness points it at its python3 stub.
import migratetool.volcmd as vc
vc.SDK_PY = [os.path.join(os.environ["HARNESS_BIN"], "python3")]
sys.argv[0] = "migrate"
a = sys.argv[1:]
if a[:1] == ["__single__"]:
    from migratetool.single import main
    sys.exit(main(a[1:]))
if os.environ.get("HARNESS_ENTRY") == "py" and a[:1] and a[0] in ("plan", "migrate", "migrate-volume"):
    from migratetool.single import main  # the per-VM engine, compared with migrate-vm.sh
    sys.exit(main(a))
from migratetool.cli import main
sys.exit(main(a))
""")


def base_world():
    sg_src = {"sg-src-default": {"name": "default", "project_id": "psrc"},
              "sg-src-web": {"name": "web", "project_id": "psrc"}}
    return {
        "counter": 0,
        "src": {
            "project_id": "psrc",
            "servers": {"srv-src-app01": {
                "name": "app01", "status": "ACTIVE", "locked": False, "locked_reason": None,
                "flavor": {"original_name": "m1.small", "name": "m1.small", "vcpus": 2, "ram": 2048},
                "key_name": "k1", "properties": {"role": "web"}, "description": "web server",
                "ports": ["port-src-1"],
                "volumes": [{"Volume ID": "vol-src-root", "Device": "/dev/vda"},
                            {"Volume ID": "vol-src-data", "Device": "/dev/vdb"}]}},
            "ports": {"port-src-1": {"name": "", "network_id": "net-src-1",
                                     "fixed_ips": [{"subnet_id": "sub-src-1", "ip_address": "198.51.100.50"}],
                                     "mac_address": "fa:16:3e:aa:bb:01", "security_group_ids": ["sg-src-default", "sg-src-web"],
                                     "device_id": "srv-src-app01", "port_security_enabled": True,
                                     "allowed_address_pairs": []}},
            "networks": {"net-src-1": {"name": "vlan120"}},
            "subnets": {"sub-src-1": {"cidr": "198.51.100.0/24", "network_id": "net-src-1"}},
            "security_groups": sg_src,
            "volumes": {
                "vol-src-root": {"name": "", "status": "in-use", "size": 10, "bootable": "true", "encrypted": False,
                                 "multiattach": False, "volume_image_metadata": {"hw_disk_bus": "virtio", "os_distro": "ubuntu",
                                                                               "image_name": "u24"}},
                "vol-src-data": {"name": "data", "status": "in-use", "size": 2, "bootable": "false", "encrypted": False,
                                 "multiattach": False}},
            "server_groups": {},
            "flavors": {"m1.small": {"vcpus": 2, "ram": 2048}},
        },
        "dst": {
            "project_id": "pdst",
            "servers": {}, "ports": {},
            "networks": {"net-dst-1": {"name": "vlan120"}},
            "subnets": {"sub-dst-1": {"cidr": "198.51.100.0/24", "network_id": "net-dst-1"}},
            "security_groups": {"sg-dst-default": {"name": "default", "project_id": "pdst"},
                                "sg-dst-web": {"name": "web", "project_id": "pdst"},
                                "sg-other-default": {"name": "default", "project_id": "pother"}},
            "volumes": {}, "server_groups": {},
            "flavors": {"m1.small": {"vcpus": 2, "ram": 2048}, "c2.small": {"vcpus": 1, "ram": 2048}},
            "keypairs": ["k1"], "volume_types": ["nvme"],
            "azs": {"compute": ["nova", "AZ1"], "volume": ["nova", "AZ1"]},
            "type_pool": {"": "mig-volumes", "nvme": "volumes-nvme"},
        },
    }


CMDS = {"bash": lambda: ["bash", os.path.join(W, "mv.sh")], "batch": lambda: ["bash", os.path.join(W, "mb.sh")],
        "py": lambda: ["/usr/bin/python3", os.path.join(W, "pyrun.py")],
        "app": lambda: ["/usr/bin/python3", os.path.join(W, "pyrun.py")]}


def legacy_optouts(step):
    """P6 gate rule for every scenario written before P6 (compare(..., p6=False)): the Python side runs
    its VM plan/migrate steps with the P6 opt-outs, which give back the behaviour up to 7.0 exactly:
    --no-copy-secgroup (same-name destination groups) and --no-server-group (the source group is not
    followed). A step that asks the reference for the source server group with --keep-server-group (gone
    from the app since 7.1, user 19/11) runs the app WITHOUT that flag and without --no-server-group:
    following the source group is the app's default now."""
    if step[:1] not in (["plan"], ["migrate"]) or step[:2] == ["plan", "volume"]:
        return step, False
    # 1.0.0 (A9): and --ephemeral-as-image, the 7.3 handling of an ephemeral root disk (no effect on other VMs).
    if "--keep-server-group" in step:
        return [a for a in step if a != "--keep-server-group"] + ["--no-copy-secgroup", "--ephemeral-as-image"], True
    return step + ["--no-server-group", "--no-copy-secgroup", "--ephemeral-as-image"], True


def header_tokens(text):
    """The reference's 'Destination preflight (...):' lines with the tokens the app's carry: the opt-outs
    added, a reference 'keep-server-group' token dropped (the app has no such flag)."""
    def add(m):
        head = m.group(1)
        if " keep-server-group" in head:
            return head.replace(" keep-server-group", "") + " no-copy-secgroup):"
        return head + " no-server-group no-copy-secgroup):"
    return re.sub(r"^(Destination preflight \(.*)\):$", add, text, flags=re.M)


def prompt_tokens(text):
    """The selector's 'Type yes' prompt lists the options passed to every VM: the opt-outs are among them."""
    def add(m):
        return "%s with: %s--no-server-group --no-copy-secgroup --ephemeral-as-image? Type yes" % (
            m.group(1), (m.group(2)[len(" with: "):] + " ") if m.group(2) else "")
    return re.sub(r"(Migrate \d+ item\(s\) one by one)( with: [^?]*)?\? Type yes", add, text)


def run(impl, world, args, env_extra=None, conf=None, pre_state=None, tty=None, optouts=False):
    """impl: bash (migrate-vm.sh) | batch (migrate-batch.sh) | py (per-VM engine, rollback by the CLI) |
    app (the 'migrate' CLI); 'a+b' = one impl per step. tty: run on a pseudo-terminal with that input.
    optouts: apply legacy_optouts to the py/app steps."""
    d = os.path.join(W, "run-" + impl.replace("+", "-"))
    if os.path.exists(d):
        shutil.rmtree(d)
    os.makedirs(os.path.join(d, "state"))
    if pre_state:
        pre_state(os.path.join(d, "state"))
    wf = os.path.join(d, "world.json")
    world["glance_url"] = GLANCE_URL
    json.dump(world, open(wf, "w"))
    GLANCE["world"] = wf
    GLANCE["calls"] = os.path.join(d, "calls")
    calls = os.path.join(d, "calls")
    open(calls, "w").close()
    conf_path = os.path.join(d, "migrate.conf")
    if conf is not None:
        open(conf_path, "w").write(conf)
    env = {"PATH": BIN + ":/usr/bin:/bin", "HOME": "/root", "LANG": "C.UTF-8",
           "HARNESS_WORLD": wf, "HARNESS_CALLS": calls, "HARNESS_STATE": os.path.join(d, "state"),
           "HARNESS_ATTACH": os.path.join(d, "attach"), "HARNESS_BIN": BIN, "HARNESS_BLOCKDEV": BLOCKDEV,
           "HARNESS_TOOLS": os.path.join(H, "stub_tools.py"), "HARNESS_PKG": os.path.join(W, "pkg"),
           "HARNESS_LOG": os.path.join(d, "log"), "MIGRATE_VM": os.path.join(W, "mv.sh"),
           "PYTHONPATH": os.path.join(W, "pkg"),
           "SRC_RC": os.path.join(W, "src.rc"), "DST_RC": os.path.join(W, "dst.rc"),
           "MIG_CONF": conf_path if conf is not None else "/nonexistent",
           "MIG_LOCK": os.path.join(d, "migrate.lock")}  # 1.0.0 (A7): the host lock of this run, never /run/mig
    env.update(env_extra or {})
    seq = args if args and isinstance(args[0], list) else [args]
    outs, errs, rcs = [], [], []
    impls = impl.split("+") if "+" in impl else [impl] * len(seq)
    transformed = False
    for n, (a, im) in enumerate(zip(seq, impls)):
        with open(calls, "a") as cf:  # 7.2: step boundaries, so that the 7.2 rule can tell whose calls are whose
            cf.write("#step\t%d\n" % n)
        if optouts and im in ("py", "app"):
            a, t = legacy_optouts(a)
            transformed = transformed or t
        if im in ("py", "app") and a[:1] == ["rollback"] and "--yes" not in a and tty is None:
            a = a + ["--yes"]  # 1.0.0 (A5): without a terminal the app asks for --yes, as migrate always did
        cmd = CMDS[im]() + a
        e = dict(env, HARNESS_ENTRY=im)
        if tty is not None:
            import pty
            m, s = pty.openpty()
            os.write(m, tty.encode())
            p = subprocess.run(cmd, env=e, stdin=s, capture_output=True, text=True, timeout=300, cwd=d)
            os.close(s)
            os.close(m)
        else:
            p = subprocess.run(cmd, env=e, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300, cwd=d)
        outs.append("### step %d\n%s" % (n, p.stdout))
        errs.append(p.stderr)
        rcs.append(p.returncode)
    raw = open(calls).read().replace(d, "<RUN>").splitlines()
    files = {}
    for base, label in ((os.path.join(d, "state"), ""), (os.path.join(d, "log"), "log/")):
        for root, _dirs, fs in os.walk(base):
            for f in fs:
                full = os.path.join(root, f)
                rel = label + os.path.relpath(full, base)
                rel = re.sub(r"\.rolled-back\.\d+", ".rolled-back.<epoch>", rel)
                rel = re.sub(r"\d{8}-\d{6}", "<TS>", rel)
                if f.endswith(".err"):
                    continue
                text = "<epoch>" if f == "t-stop" else open(full).read()
                if f == "clone-time":
                    text = re.sub(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", "<UTC>", text)
                files[rel] = norm(text, d) if label else text
    return {"out": norm("".join(outs), d), "err": norm("".join(errs), d), "rc": rcs, "files": files,
            "calls": [l for l in raw if not l.startswith("#step\t")], "rawcalls": raw, "world": json.load(open(wf)),
            "dir": d, "args": seq, "impls": impls,
            "transformed": transformed}


def norm(text, d):
    text = text.replace(os.path.join(W, "mv.sh"), "PROG")
    # How each build names itself: $0 (the patched copy) / "migrate".
    text = re.sub(r"(?<![\w/.-])migrate (rollback|migrate|plan|volume)\b", r"PROG \1", text)
    text = text.replace(d, "<RUN>")
    text = re.sub(r"\d{8}-\d{6}", "<TS>", text)
    # 'Volume copied crash-consistent as of <clone request time>' (seconds differ between two runs).
    text = re.sub(r"as of \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", "as of <UTC>", text)
    text = re.sub(r"\[\d\d:\d\d:\d\d\]", "[T]", text)
    text = re.sub(r"in \d+s", "in Ns", text)
    text = re.sub(r"so far \d+s", "so far Ns", text)
    text = re.sub(r"for \d+s so far", "for Ns so far", text)
    text = re.sub(r"\(\d+ MB/s logical\)", "(N MB/s logical)", text)
    return text


# --- 7.2: the mechanical rule for every scenario written before 7.2 ------------------------------------
# Each line is an intended deviation of the app from the bash reference (DEVELOPING-PYTHON section 2),
# named by its DESIGN-V7-2 code. compare(..., v72=True) turns the rule off (scenarios written for 7.2).
# Lines only the app prints (dropped from its stdout and its child logs):
V72_APP_OUT_DROP = (
    r"^  OK    (source|destination) quota: ",          # Intended: 7.2 A2 (quota of both clouds)
    r"^  (OK    |WARN  )Ceph pool ",                     # Intended: 7.2 A2 (destination pool capacity)
    r"^  INFO  Ceph pool capacity not checked: ",      # Intended: 7.2 A2
    r"^  OK    free addresses for --ip new: ",         # Intended: 7.2 A2 (--ip new allocation pools)
    r"^  (OK    free space in |WARN  free space not checked)",  # Intended: 7.2 B2 (snapshot download room)
    r"^\[T\] destination volumes created; stopping the source now$",  # Intended: 7.2 A1
    r"^\[T\] downtime breakdown: ",                    # Intended: 7.2 D3; Intended: 7.3 8 (D3b: an ephemeral
                                                        # root's Glance time is copy, 'copy Ns (glance Ns)')
    r"^  OK    image endpoint ",                        # Intended: 7.2 B2 (checked in the preflight)
)
# Calls only the app makes (read-only, dropped from its call list):
V72_APP_CALLS_DROP = (
    r"^(src|dst)\tquota show --usage -f json$",        # Intended: 7.2 A2
    r"^dst\tlimits show --absolute -f json$",          # Intended: 7.2 A2 (server group members)
    r"^dst\tserver group show \S+ -f json -c members$",  # Intended: 7.2 A2
    r"^dst\tsubnet list --network \S+ -f json -c ID -c Subnet$",  # Intended: 7.2 A2 (--ip new)
    r"^dst\tsubnet show \S+ -f json$",                 # Intended: 7.2 A2
    r"^dst\tport list --fixed-ip subnet=\S+ -f value -c ID$",  # Intended: 7.2 A2
    r"^ceph\t",                                         # Intended: 7.2 A2 (ceph df detail)
    r"^src\tfloating ip list --port \S+ -f value -c Floating IP Address$",  # Intended: 7.2 A3 (export)
    r"^dst\tserver list --name \S+ -f json$",           # Intended: 7.2 C8 (rollback: server never recorded)
    r"^dst\ttoken issue -f value -c id$",               # Intended: 7.2 B2 (a fresh token per Glance call)
)
# Calls only the reference makes:
V72_REF_CALLS_DROP = (
    r"^dst\ttoken issue -f value -c id$",               # Intended: 7.2 B2 (its one token, issued earlier)
)
NO_LONGER = re.compile(r"^\[T\] port (\S+) no longer exists \(Nova deleted it; its reserve port is reattached "
                       r"instead\)$", re.M)


def a1_reorder(calls):
    """Intended: 7.2 A1. The reference's cold cutover runs, per volume, create_dest_volume (from its 'src volume
    show <vid> -f json' up to the 'src volume show <vid> -f value -c size' of the copy) after the source stopped;
    the app runs every create before the cutover (its first call: 'server show <sid> ... -c locked') and reads
    the size from volumes.json. Rewrites the reference into that order."""
    calls = list(calls)
    start = 0
    lock_rx = re.compile(r"^src\tserver show \S+ -f value -c locked$")
    while True:
        lock = next((i for i in range(start, len(calls)) if lock_rx.match(calls[i])), None)
        if lock is None:
            return calls
        nxt = next((i for i in range(lock + 1, len(calls)) if lock_rx.match(calls[i])), len(calls))
        locked = next((i for i in range(lock + 1, nxt) if calls[i].startswith("src\tserver lock ")), nxt)
        segs, i, ok = [], locked + 1, True
        while i < nxt:
            m = re.match(r"^src\tvolume show (\S+) -f json$", calls[i])
            if not m:
                i += 1
                continue
            size = "src\tvolume show %s -f value -c size" % m.group(1)
            end = next((j for j in range(i + 1, nxt) if calls[j] == size), None)
            if end is None:
                break  # a create that died (not a mechanical case), or the next VM's export
            segs.append((i, end))
            i = end + 1
        if ok and segs:
            moved = [c for s, e in segs for c in calls[s:e]]
            drop = set()
            for s, e in segs:
                drop.update(range(s, e + 1))
            rest = [c for k, c in enumerate(calls[lock:nxt], lock) if k not in drop]
            calls[lock:nxt] = moved + rest
            nxt = lock + len(moved) + len(rest)
        start = nxt


def _steps(calls):
    """The call list cut at run()'s '#step<TAB>n' markers: [[marker, calls...], ...] (a head before the first)."""
    out = [[]]
    for c in calls:
        if c.startswith("#step\t"):
            out.append([])
        out[-1].append(c)
    return out


def v72_rule(b, p, p_impls):
    """Apply the 7.2 rule to a reference result b and an app result p (in place). p_impls: the impl of each
    step on the app side; the reference's calls are rewritten only for the steps the app ran."""
    out_rx = [re.compile(x) for x in V72_APP_OUT_DROP] + [NO_LONGER]

    def drop_lines(text):
        return "".join(l for l in text.splitlines(True) if not any(x.search(l.rstrip("\n")) for x in out_rx))
    # Intended: 7.2 B1: rollback probes every port of port-ids.json too; a port Nova deleted gets the 'no longer
    # exists' line and one more 'port show' (the last one of that port) than the reference made.
    gone = NO_LONGER.findall(p["out"])
    p["out"] = drop_lines(p["out"])
    p["files"] = {k: (drop_lines(v) if k.startswith("log/") else v) for k, v in p["files"].items()}
    pcalls = list(p["rawcalls"])
    for pid in gone:
        probe = "src\tport show %s -f value -c device_id" % pid
        last = max((i for i, c in enumerate(pcalls) if c == probe), default=None)
        if last is not None:
            del pcalls[last]
    psteps = _steps(pcalls)
    for n, step in enumerate(psteps):
        if n == 0 or (n - 1 < len(p_impls) and p_impls[n - 1] in ("py", "app")):
            psteps[n] = [c for c in step if not any(re.search(x, c) for x in V72_APP_CALLS_DROP)]
    steps = _steps(b["rawcalls"])
    for n, step in enumerate(steps):
        app_ran = n == 0 or (n - 1 < len(p_impls) and p_impls[n - 1] in ("py", "app"))
        if not app_ran:
            continue
        ref = [c for c in step if not any(re.search(x, c) for x in V72_REF_CALLS_DROP)]
        ref = [re.sub(r"^dst\tflavor show (\S+)$", r"dst\tflavor show \1 -f json", c) for c in ref]  # Intended: 7.2 A2
        ref = [c.replace("--interface internal", "--interface public") for c in ref]  # Intended: 7.2 B2
        steps[n] = a1_reorder(ref)
        # Intended: 7.2 B2: the preflight looks the image endpoint up too: the app's first extra ones go.
        ep = re.compile(r"^dst\tendpoint list --service image ")
        if n < len(psteps):
            extra = sum(1 for c in psteps[n] if ep.match(c)) - sum(1 for c in steps[n] if ep.match(c))
            keep = []
            for c in psteps[n]:
                if extra > 0 and ep.match(c):
                    extra -= 1
                    continue
                keep.append(c)
            psteps[n] = keep
    b["calls"] = [c for step in steps for c in step if not c.startswith("#step\t")]
    p["calls"] = [c for step in psteps for c in step if not c.startswith("#step\t")]
    # Intended: 7.2 B5: a leftover clone goes through drop_volume: one more status read before its delete.
    i = 0
    while i + 2 < len(p["calls"]):
        x, y, z = p["calls"][i:i + 3]
        m = re.match(r"^src\tvolume show (\S+) -f value -c status$", x)
        if m and y == x and z == "src\tvolume delete " + m.group(1):
            del p["calls"][i]
        i += 1

    def volume_az(text):  # Intended: 7.2 C6: the state 'options' line ends with VOLUME_AZ=
        m = re.search(r"(?:^| )AZ=(\S*)", text)
        if not m or "VOLUME_AZ=" in text:
            return text
        return text.rstrip("\n") + " VOLUME_AZ=%s\n" % m.group(1)
    b["files"] = {k: (volume_az(v) if os.path.basename(k) == "options" and "VOLUME_AZ=" in p["files"].get(k, "") else v)
                  for k, v in b["files"].items()}


# --- 7.3: the mechanical rule for every scenario (the reference has no such files) -----------------------------
# Intended: 7.3 1.2: the app records each migrate run in options.json (next to the unchanged one-line 'options') and
# each finished rollback in rollback.json inside the retired directory. Both are new files only the app writes.
V73_APP_FILES_DROP = re.compile(r"(^|/)(options|rollback)\.json$")


def v73_rule(p):
    p["files"] = {k: v for k, v in p["files"].items() if not V73_APP_FILES_DROP.search(k)}


# --- 1.0.0: the mechanical rule for every scenario written before 1.0.0 (compare(..., v1=True) turns it off) ---------
# Each line is an intended deviation of the app from the reference, named by its REVIEW-20261121 ID.
# Calls only the app makes (read-only), dropped from its call list:
V1_APP_CALLS_DROP = (
    r"^(src|dst)\t(server|volume) show \S+ -f json -c id -c status$",          # Intended: v1 A4/A15 (gone polls), A5
    r"^src\tserver volume list \S+ -f value -c Volume ID$",                     # Intended: v1 A12 (re-check at cutover)
    r"^src\tport list --server \S+ -f value -c ID$",                            # Intended: v1 A12
    r"^src\tserver show \S+ -f json -c flavor$",                                # Intended: v1 A12
    r"^src\tflavor show \S+ -f json -c swap -c OS-FLV-EXT-DATA:ephemeral$",     # Intended: v1 A11
    r"^dst\tserver group show \S+ -f json$",                                    # Intended: v1 C11 (rules of a reused group)
)
# Arguments only the app passes (dropped from its calls): the owner marks (A2).
V1_APP_ARGS_DROP = (
    re.compile(r" --property mig\.source_id=\S+ --property mig\.state=\S+"),   # Intended: v1 A2 (server)
    re.compile(r" --description mig:\S+"),                                     # Intended: v1 A2 (port)
)
V1_STATE_EXISTS = re.compile(r" \(that run stopped the source[^)\n]*\)")       # Intended: v1 A6
V1_BATCH_DIR = re.compile(r"<TS>-[0-9a-f]{6}")                                  # Intended: v1 C12
# Lines the app words differently (applied to its out, err and child logs): (regex, replacement)
V1_TEXT_SUBS = (
    (re.compile(" — those limits are NOT enforced at the destination \\(C11: [^)]*\\)"), ""),   # Intended: v1 C11
    (re.compile(r"^  WARN  flavor \S+: ephemeral and swap disk sizes not readable[^\n]*\n", re.M), ""),  # Intended: v1 A11
    (re.compile(r"ERROR: unknown option \S+: rollback takes <target> \[--yes\] \[--dry-run \[--json\]\]"),
     "ERROR: rollback takes no options"),                                         # Intended: v1 A5 (rollback options)
)


def v1_rule(b, p):
    """Apply the 1.0.0 rule to an app result p (in place); b (the reference) is left as it is."""
    calls = [c for c in p["calls"] if not any(re.search(x, c) for x in V1_APP_CALLS_DROP)]
    for rx in V1_APP_ARGS_DROP:
        calls = [rx.sub("", c) for c in calls]
    p["calls"] = calls
    # Intended: v1 C8: exit codes 3 (failed before t-stop), 4 (after), 5 (a selection with a FAIL) were all 1.
    p["rc"] = [1 if rc in (3, 4, 5) else rc for rc in p["rc"]]
    p["err"] = V1_STATE_EXISTS.sub("", p["err"])
    for rx, to in V1_TEXT_SUBS:
        p["out"], p["err"] = rx.sub(to, p["out"]), rx.sub(to, p["err"])
        p["files"] = {k: rx.sub(to, v) for k, v in p["files"].items()}
    p["out"] = V1_BATCH_DIR.sub("<TS>", p["out"])
    p["err"] = V1_BATCH_DIR.sub("<TS>", p["err"])
    p["files"] = {V1_BATCH_DIR.sub("<TS>", k): V1_STATE_EXISTS.sub("", V1_BATCH_DIR.sub("<TS>", v))
                  for k, v in p["files"].items()}
    # Intended: v1 A2: the marks in the final world (server properties, port descriptions of ports without one).
    for s in p["world"].get("dst", {}).get("servers", {}).values():
        props = s.get("properties")
        if isinstance(props, dict):
            for k in ("mig.source_id", "mig.state"):
                props.pop(k, None)
    for port in p["world"].get("dst", {}).get("ports", {}).values():
        if str(port.get("description") or "").startswith("mig:"):
            port.pop("description", None)


# --- 1.1.0: the mechanical rule for every scenario (the reference never asks the source before its rollback) ---------
# Intended: v1.1 A20: a rollback run by the app first reads the source object ('server show <sid>' for a VM state,
# 'volume show <vid>' for a migrate-volume one) and deletes nothing at the destination when it is gone. That read-only
# call is dropped from each app rollback step: the calls with the ID of the step's first such call (once, twice when a
# prompt summary read it too). Not a regex over every step: a migrate step, or the reference's rollback, keeps its calls.
V11_SOURCE_PROBE = re.compile(r"^src\t(server|volume) show (\S+) -f json -c id -c status$")


def v11_rule(p):
    """Apply the 1.1.0 rule to an app result p (in place): p['rawcalls'] and p['calls'] lose the A20 probe of each
    rollback step the app ran (run() records each step's args and impl)."""
    args, impls = p.get("args") or [], p.get("impls") or []
    steps = _steps(p["rawcalls"])
    for n, step in enumerate(steps):
        i = n - 1  # steps[0] is the head before the first '#step' marker
        app_rollback = 0 <= i < min(len(args), len(impls)) and impls[i] in ("py", "app") \
            and list(args[i][:1]) == ["rollback"]
        if not app_rollback:
            continue
        probes = [m.group(1, 2) if m else None for m in (V11_SOURCE_PROBE.match(c) for c in step)]
        first = next((x for x in probes if x), None)
        if first:
            steps[n] = [c for c, x in zip(step, probes) if x != first]
    p["rawcalls"] = [c for step in steps for c in step]
    p["calls"] = [c for c in p["rawcalls"] if not c.startswith("#step\t")]


def compare(name, args, world=None, env=None, conf=None, expect_diff=(), pre_state=None, show=False,
            impls=("bash", "py"), py_args=None, bash_subs=(), out_subseq=False, tty=None, check=None,
            p6=False, subs_all=(), app_calls_ignore=(), v72=False, ref_calls_subs=(), v1=False):
    """py_args: the second impl's command line when it differs (new CLI). bash_subs: (old, new)
    rewordings applied to the reference's out/err/log files (each one an intended deviation).
    out_subseq: the reference's stdout lines must appear, in order, in the other's (which prints
    more). check(b, p) -> list of problems: extra assertions.
    P6: p6=False (every scenario written before P6) runs the Python side with legacy_optouts and adds
    their tokens to the reference's preflight headers; subs_all: (old, new) applied to the reference's
    out/err/files AND calls/world (a renamed object); app_calls_ignore: regexes of calls only the app
    makes (dropped from its call list before the comparison). Each use is an intended deviation."""
    global PASS, FAIL
    world = world or base_world()
    b = run(impls[0], copy.deepcopy(world), args, env, conf, pre_state, tty)
    # subs_all renames an object: one that already exists in the world carries its new name for the app.
    pworld = copy.deepcopy(world)
    for old, new in subs_all:
        pworld = json.loads(json.dumps(pworld).replace(old, new))
    # HARNESS_NO_OPTOUTS=1: run the old scenarios against the P6 defaults (shows which ones they change).
    p = run(impls[1], pworld, py_args or args, env, conf, pre_state, tty,
            optouts=not p6 and not os.environ.get("HARNESS_NO_OPTOUTS"))
    v11_rule(p)  # 1.1.0: before the other rules, while each step's calls are still marked
    if not v72:
        nsteps = len(args) if args and isinstance(args[0], list) else 1
        v72_rule(b, p, impls[1].split("+") if "+" in impls[1] else [impls[1]] * nsteps)
    v73_rule(p)
    if not v1:
        v1_rule(b, p)
    for old, new in ref_calls_subs:  # (old, new) in the reference's calls only (each one an intended deviation)
        b["calls"] = [c.replace(old, new) for c in b["calls"]]
    if p["transformed"]:
        TRANSFORMED.append(name)
        b["out"] = header_tokens(b["out"])
        b["err"] = prompt_tokens(b["err"])
        b["files"] = {k: header_tokens(v) for k, v in b["files"].items()}
    for old, new in subs_all:
        b["calls"] = [l.replace(old, new) for l in b["calls"]]
        b["world"] = json.loads(json.dumps(b["world"]).replace(old, new))
    for old, new in tuple(bash_subs) + tuple(subs_all):
        b["out"], b["err"] = b["out"].replace(old, new), b["err"].replace(old, new)
        b["files"] = {k: v.replace(old, new) for k, v in b["files"].items()}
    if app_calls_ignore:
        p["calls"] = [l for l in p["calls"] if not any(re.search(rx, l) for rx in app_calls_ignore)]
    problems = []
    if out_subseq:
        it = iter(p["out"].splitlines())
        missing = [l for l in b["out"].splitlines() if not l.startswith("### ") and not any(l == x for x in it)]
        if missing:
            problems.append("--- out: reference lines missing (in order) from the other:\n" + "\n".join(missing[:20]))
    if check:
        problems += ["--- check: " + x for x in check(b, p)]
    for key in ("out", "err", "rc", "files", "calls", "world"):
        if key in expect_diff:
            continue
        if b[key] != p[key]:
            if isinstance(b[key], str):
                diff = "\n".join(difflib.unified_diff(b[key].splitlines(), p[key].splitlines(), "bash", "py", lineterm="", n=1))
            elif key == "calls":
                diff = "\n".join(difflib.unified_diff(b[key], p[key], "bash", "py", lineterm="", n=1))
            elif isinstance(b[key], dict) and key == "files":
                diff = ""
                for f in sorted(set(b[key]) | set(p[key])):
                    if b[key].get(f) != p[key].get(f):
                        diff += "  file %s: bash=%r py=%r\n" % (f, (b[key].get(f) or "")[:300], (p[key].get(f) or "")[:300])
            else:
                diff = "  bash=%s\n  py=%s" % (str(b[key])[:500], str(p[key])[:500])
            problems.append("--- %s differs:\n%s" % (key, diff))
    if problems:
        FAIL += 1
        print("FAIL %s" % name)
        for pr in problems:
            print(pr[:4000])
    else:
        PASS += 1
        print("PASS %s (rc=%s)" % (name, b["rc"]))
    if show or problems:
        print("    bash stdout:\n" + "\n".join("      | " + l for l in b["out"].splitlines()[-40:]))
        if b["err"]:
            print("    bash stderr:\n" + "\n".join("      | " + l for l in b["err"].splitlines()[-10:]))
        if p["err"] and problems:
            print("    py stderr:\n" + "\n".join("      | " + l for l in p["err"].splitlines()[-10:]))
    return b, p
