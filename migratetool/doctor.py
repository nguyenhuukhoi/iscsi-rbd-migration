"""'migrate doctor' (DESIGN.md, "doctor"): is this host ready to migrate? Read-only (but --write-check), needs no VM.

One '  OK    ' / '  FAIL  ' / '  WARN  ' / '  INFO  ' line per item, exit 1 when any FAIL:
  1. the programs: openstack, qemu-img, rbd, iscsiadm (multipath with MIG_MULTIPATH=1);
  2. the brick child imports os-brick and the Cinder client (python -I -m migratetool.brick --check);
  3. SYS_PY imports the librbd bindings, the bundled blocksync.py is readable;
  4. the conf file parses, both openrcs are readable, OS_AUTH_TYPE is one brick understands;
  5. a token on each side; 6. the admin role (--ip same creates ports with --mac-address);
  7. RBD access to RBD_POOL and every pool of RBD_POOL_MAP, iscsid (and multipathd) running, MIG_ISCSI_IP set to an
     address of this host; 1.2.0: every destination type of VOLUME_TYPE_MAP exists at the destination;
  8. the state and log directories can be created and written;
  9. the openrcs are root-owned 0600 (WARN) and name two different Keystones (B3); brick gets a token with the source
     openrc's TLS/interface/region (C7); root, or 'sudo -n' works (B9);
 10. with --write-check only: each pool takes a 4 MiB write, read-back, discard and delete (C6).
Every check runs even after a FAIL, so one run lists everything to fix.
"""
import json
import os
import time

from . import __version__, config, proc
from .clouds import Cloud, auth_url, base_env, rc_env, rc_file_problem
from .copyvol import BRICK
from .livesync import BLOCKSYNC
from .preflight import Checker, systemctl_ok
from .util import Die, ifs_split, lines, out

AUTH_TYPES = ("", "password", "v3applicationcredential")  # what brick.py's cinder() accepts
ROLE_WARN = "no admin role: --ip same needs port --mac-address (admin_or_network_owner)"


def _first_line(res):
    text = (res.stdout or "").strip() or (res.stderr or "").strip()
    return (text.splitlines() or [""])[0]


def check_programs(c, cfg, env):
    progs = [("openstack", "--version"), ("qemu-img", "--version"), ("rbd", "--version"), ("iscsiadm", "--version")]
    if cfg.MIG_MULTIPATH == "1":
        progs.append(("multipath", "-h"))
    for prog, flag in progs:
        res = proc.run([prog, flag], env=env, capture=True, stderr=proc.CAPTURE)
        if res.rc == 127:
            c.failed("%s: not found on PATH" % prog)
        elif res.rc == 0 or prog == "multipath":  # 'multipath -h' exits 1 after its usage on some versions
            out("  OK    %s: %s" % (prog, _first_line(res)))
        else:
            c.failed("%s %s exited %d: %s" % (prog, flag, res.rc, _first_line(res)))


def check_pythons(c, cfg, env):
    res = proc.run([*BRICK, "--check"], env=env, capture=True, stderr=proc.CAPTURE)
    if res.rc == 0 and res.stdout.strip() == "ok":
        out("  OK    brick: os-brick, cinderclient, keystoneauth1, oslo import")
    else:
        c.failed("brick cannot start (%s): install the 'host' extra into this venv with the release constraints "
                 "(DEPLOY.md section 2.1)" %((res.stderr.strip().splitlines() or ["exit %d" % res.rc])[-1]))
    res = proc.run([cfg.SYS_PY, "-I", "-c", "import rados, rbd"], env=env, capture=True, stderr=proc.CAPTURE)
    if res.rc == 0:
        out("  OK    librbd python bindings for SYS_PY=%s" % cfg.SYS_PY)
    else:
        c.failed("SYS_PY=%s cannot import rados, rbd (live mode needs them): install python3-rbd, or point SYS_PY at "
                 "an interpreter that has it" % cfg.SYS_PY)
    c.check("blocksync present (%s)" % BLOCKSYNC, lambda: os.access(BLOCKSYNC, os.R_OK))


def check_conf(c):
    """The configuration, from the conf file when it parses, else from the environment and the defaults."""
    path = os.environ.get("MIG_CONF") or config.DEFAULT_CONF
    try:
        cfg = config.load()
    except Die as e:
        c.failed("conf %s: %s" % (path, e))
        # 1.2.2 (S5): say once that the checks below do not see the conf, so a FAIL among them is read as such.
        out("  INFO  conf %s not loaded: the checks below use the environment and the built-in defaults only; "
            "fix the conf first" % path)
        env = dict(os.environ, MIG_CONF="/nonexistent")
        try:
            cfg = config.load(env)
        except Die:  # 1.2.0: a malformed VOLUME_TYPE_MAP in the environment (said above): the other checks still run
            env["VOLUME_TYPE_MAP"] = ""
            cfg = config.load(env)
        cfg.conf_failed = True
        return cfg
    if os.path.isfile(path) and os.access(path, os.R_OK):
        out("  OK    conf %s" % path)
    else:
        out("  INFO  conf %s not found: defaults and environment only" % path)
    return cfg


def token_roles(cloud, env, pid):
    """(roles, why) of the openrc's user at project pid: an application credential's own roles, else the effective
    role assignments. roles is None when they cannot be read (why: the last stderr line). Doctor, and the
    preflight of --dst-host (7.3), which needs admin at the destination."""
    if "applicationcredential" in env.get("OS_AUTH_TYPE", ""):
        ident = env.get("OS_APPLICATION_CREDENTIAL_ID") or env.get("OS_APPLICATION_CREDENTIAL_NAME") or ""
        res = cloud.run("application", "credential", "show", ident, "-f", "json", stderr=proc.CAPTURE)
        roles = None
        if res.rc == 0:
            try:
                got = json.loads(res.stdout).get("roles")
            except (ValueError, AttributeError):
                got = None
            if isinstance(got, list):
                roles = [x.get("name") if isinstance(x, dict) else str(x) for x in got]
            elif isinstance(got, str):
                roles = got.replace(",", " ").split()
    else:
        res = cloud.run("role", "assignment", "list", "--effective", "--user", env.get("OS_USERNAME", ""),
                        "--project", pid, "--names", "-f", "value", "-c", "Role", stderr=proc.CAPTURE)
        roles = lines(res.stdout) if res.rc == 0 else None
    return roles, (res.stderr.strip().splitlines() or ["exit %d" % res.rc])[-1]


NOVA_HOST_MICROVERSION = (2, 74)  # server create --host


def microversion(text):
    """'2.96' -> (2, 96); None when it is not one."""
    parts = str(text or "").strip().split(".")
    return (int(parts[0]), int(parts[1])) if len(parts) == 2 and all(p.isdigit() for p in parts) else None


def check_nova_api(cloud):
    """7.3 (6): can --dst-host be used: the destination compute API reaches microversion 2.74. Never a FAIL."""
    try:
        res = cloud.run("versions", "show", "--service", "compute", "-f", "json", stderr=proc.CAPTURE)
    except Die as e:  # an openrc that cannot be sourced: check_side said so already
        return out("  INFO  destination Nova API version not checked (%s): --dst-host needs 2.74" % e)
    try:
        rows = json.loads(res.stdout) if res.rc == 0 else None
    except ValueError:
        rows = None
    best = max((v for v in (microversion(x.get("Max Microversion")) for x in rows if isinstance(x, dict)) if v),
               default=None) if isinstance(rows, list) else None
    if best is None:
        why = (res.stderr.strip().splitlines() or ["no Max Microversion"])[-1] if res.rc != 0 else "no Max Microversion"
        out("  INFO  destination Nova API version not checked (%s): --dst-host needs 2.74" % why)
    elif best >= NOVA_HOST_MICROVERSION:
        out("  OK    destination Nova API %d.%d >= 2.74 (--dst-host usable)" % best)
    else:
        out("  INFO  destination Nova API %d.%d < 2.74: --dst-host not available" % best)


def check_side(c, cloud, label):
    """The openrc, its auth type, a token, and the admin role of one side."""
    if not os.access(cloud.rc, os.R_OK):
        return c.failed("%s openrc %s is not readable" % (label, cloud.rc))
    try:
        env = rc_env(cloud.rc, cloud._base)
    except Die as e:
        return c.failed("%s openrc: %s" % (label, e))
    auth = env.get("OS_AUTH_TYPE", "")
    if auth not in AUTH_TYPES:
        c.failed("%s openrc %s: OS_AUTH_TYPE=%s (brick knows password and v3applicationcredential)"
                 % (label, cloud.rc, auth))
    res = cloud.run("token", "issue", "-f", "json", stderr=proc.CAPTURE)
    try:
        tok = json.loads(res.stdout) if res.rc == 0 else None
    except ValueError:
        tok = None
    if not isinstance(tok, dict):
        return c.failed("%s token: %s" % (label, (res.stderr.strip().splitlines() or ["exit %d" % res.rc])[-1]))
    pid = tok.get("project_id") or ""
    out("  OK    %s token (project %s)" % (label, cloud.project or env.get("OS_PROJECT_NAME") or pid))
    if "applicationcredential" in auth:
        out("  INFO  %s: application credential: bound to its own project (SRC_PROJECT/DST_PROJECT cannot rescope it)"
            % label)
    roles, why = token_roles(cloud, env, pid)
    if roles is None:
        out("  INFO  %s: roles not checked: %s" % (label, why))
    elif "admin" not in roles:
        out("  WARN  %s: %s" % (label, ROLE_WARN))
    else:
        out("  OK    %s: admin role" % label)


def check_storage(c, cfg, env):
    pools = [cfg.RBD_POOL]
    for e in ifs_split(cfg.RBD_POOL_MAP, ","):
        pool = e.split(":", 1)[1] if ":" in e else e
        if pool and pool not in pools:
            pools.append(pool)
    for pool in pools:
        c.check("RBD access to pool %s as %s" % (pool, cfg.RBD_USER), lambda: proc.run(
            ["rbd", "-c", cfg.CEPH_CONF, "--id", cfg.RBD_USER, "-p", pool, "ls"], env=env, capture=True,
            stderr=proc.DEVNULL).rc == 0)
    good = systemctl_ok("is-active", "iscsid", env)
    c.check("iSCSI daemon on transfer host", lambda: good)
    if not good:
        c.info("start it: systemctl enable --now iscsid")
    if cfg.MIG_MULTIPATH == "1":
        c.check("multipathd on transfer host", lambda: systemctl_ok("is-active", "multipathd", env))
    check_iscsi_ip(c, cfg, env)


def check_type_map(c, cfg, dst):
    """1.2.0: every destination type VOLUME_TYPE_MAP names is a volume type of the destination ('volume type list'), one
    OK/FAIL line per type; with RBD_POOL_MAP set, a type it does not name lands in RBD_POOL (INFO). Nothing when
    VOLUME_TYPE_MAP is empty (or did not parse: the conf line said so)."""
    try:
        tmap = config.parse_type_map(cfg.values.get("VOLUME_TYPE_MAP", ""))
    except Die:
        return
    if not tmap:
        return
    wanted = {}
    for src, dst_type in tmap.items():
        wanted.setdefault(dst_type, []).append(src)
    try:
        res = dst.run("volume", "type", "list", "-f", "value", "-c", "Name", stderr=proc.CAPTURE)
    except Die as e:  # an openrc that cannot be sourced: check_side said so already
        return c.failed("VOLUME_TYPE_MAP not checked: cannot list the destination volume types (%s)" % e)
    if res.rc != 0:
        return c.failed("VOLUME_TYPE_MAP not checked: cannot list the destination volume types (%s)"
                        % (res.stderr.strip().splitlines() or ["exit %d" % res.rc])[-1])
    there = set(lines(res.stdout))
    pool_map = {e.split(":", 1)[0] for e in ifs_split(cfg.RBD_POOL_MAP, ",")}
    for dst_type, srcs in wanted.items():
        if dst_type in there:
            out("  OK    VOLUME_TYPE_MAP destination type %s (for %s)" % (dst_type, ", ".join(srcs)))
        else:
            c.failed("VOLUME_TYPE_MAP destination type %s (for %s) is not a volume type of the destination"
                     % (dst_type, ", ".join(srcs)))
        if cfg.RBD_POOL_MAP and dst_type not in pool_map:
            out("  INFO  VOLUME_TYPE_MAP destination type %s has no RBD_POOL_MAP entry: uses RBD_POOL %s"
                % (dst_type, cfg.RBD_POOL))


def local_addresses(env):
    """The addresses of this host's interfaces ('ip -o addr show'), without the prefix length. None when 'ip' cannot
    be run."""
    res = proc.run(["ip", "-o", "addr", "show"], env=env, capture=True, stderr=proc.DEVNULL)
    if res.rc != 0:
        return None
    found = []
    for line in lines(res.stdout):
        words = line.split()
        for i, w in enumerate(words[:-1]):
            if w in ("inet", "inet6"):
                found.append(words[i + 1].split("/", 1)[0])
    return found


def check_iscsi_ip(c, cfg, env):
    """MIG_ISCSI_IP is the connector address brick gives Cinder: it has no default, and it must be an address of this
    host (a value copied from another convert host or a lab would be handed to the array as this host's)."""
    have = local_addresses(env)
    v4 = [a for a in (have or []) if "." in a and not a.startswith("127.")]
    listing = ", ".join(v4) if v4 else "none found"
    ip = cfg.MIG_ISCSI_IP.strip()
    if not ip and getattr(cfg, "conf_failed", False):  # 1.2.2 (S5): the value may well be in the conf not loaded
        out("  INFO  MIG_ISCSI_IP not checked: conf not loaded")
    elif not ip:
        c.failed("MIG_ISCSI_IP is not set: set it in the conf to this host's address on the iSCSI network "
                 "(addresses of this host: %s)" % listing)
    elif have is None:
        out("  INFO  MIG_ISCSI_IP %s not compared with this host's addresses: 'ip -o addr show' failed" % ip)
    elif ip not in have:
        c.failed("MIG_ISCSI_IP %s is not an address of this host (addresses of this host: %s)" % (ip, listing))
    else:
        out("  OK    MIG_ISCSI_IP %s is an address of this host" % ip)


def check_dirs(c):
    for label, path in (("state directory", config.STATE_ROOT), ("log directory", config.LOG_ROOT)):
        try:
            os.makedirs(path, exist_ok=True)
            good = os.access(path, os.W_OK)
        except OSError:
            good = False
        c.check("%s %s writable" % (label, path), lambda: good)


def check_privileges(c, env):
    """B9: brick runs os-brick with ROOT_HELPER 'sudo' only when not root; as root nothing else is needed."""
    if os.geteuid() == 0:
        out("  OK    running as root (brick needs no sudo)")
        return
    res = proc.run(["sudo", "-n", "true"], env=env, capture=True, stderr=proc.CAPTURE)
    c.check("not root: 'sudo -n true' works (brick attaches through sudo)", lambda: res.rc == 0)


def check_openrcs(c, cfg, env):
    """B3: the openrcs are root-owned 0600 (WARN otherwise: they hold the admin passwords), and they name two
    different Keystones (FAIL: both sides in one cloud is a copy-paste mistake)."""
    for label, rc in (("source", cfg.SRC_RC), ("destination", cfg.DST_RC)):
        if os.access(rc, os.R_OK):
            why = rc_file_problem(rc)
            if why:
                out("  WARN  %s openrc %s: %s (it holds a password: chown root, chmod 600)" % (label, rc, why))
    src, dst = auth_url(cfg.SRC_RC, env), auth_url(cfg.DST_RC, env)
    if src and src == dst:
        c.failed("SRC_RC and DST_RC point at the same Keystone %s: migrate moves VMs between two clouds" % src)


def check_brick_auth(c, cfg, env):
    """C7: a Keystone token through the session brick builds (OS_CACERT/OS_INSECURE, OS_INTERFACE, OS_REGION_NAME of
    the source openrc), so that a TLS or catalog problem shows here and not at the first attach."""
    try:
        e = rc_env(cfg.SRC_RC, env)
    except Die as err:
        return out("  INFO  brick authentication not checked: %s" % err)
    res = proc.run([*BRICK, "--check-auth"], env=e, capture=True, stderr=proc.CAPTURE)
    if res.rc == 0 and res.stdout.startswith("ok"):
        out("  OK    brick: Keystone session of the source openrc works (TLS, interface, region as openstackclient)")
    else:
        c.failed("brick cannot authenticate with the source openrc: %s"
                 % ((res.stderr.strip().splitlines() or res.stdout.strip().splitlines() or ["exit %d" % res.rc])[-1]))


WRITE_CHECK_MIB = 4


def write_check_pools(c, cfg, env):
    """C6 (--write-check): prove the RBD user can write each pool, the way the copies do. Per pool: 'rbd import' of
    4 MiB of random data as mig-doctor-<epoch> (write), 'rbd export' and compare (read), with the librbd bindings of
    SYS_PY a discard of the first 4 KiB read back as zeros (blocksync discards), then 'rbd rm'. The image is removed
    whatever happens; the only change doctor ever makes, and never to a cloud."""
    import hashlib
    import tempfile
    pools = [cfg.RBD_POOL]
    for e in ifs_split(cfg.RBD_POOL_MAP, ","):
        pool = e.split(":", 1)[1] if ":" in e else e
        if pool and pool not in pools:
            pools.append(pool)
    base = ["rbd", "-c", cfg.CEPH_CONF, "--id", cfg.RBD_USER]
    tmp = tempfile.mkdtemp(prefix="mig-doctor-")
    try:
        data = os.urandom(WRITE_CHECK_MIB << 20)
        src = os.path.join(tmp, "in.bin")
        with open(src, "wb") as f:
            f.write(data)
        for pool in pools:
            name = "mig-doctor-%d" % int(time.time())
            spec = "%s/%s" % (pool, name)
            why = ""
            try:
                res = proc.run(base + ["import", src, spec], env=env, capture=True, stderr=proc.CAPTURE)
                if res.rc != 0:
                    why = "write (rbd import): " + ((res.stderr.strip().splitlines() or ["exit %d" % res.rc])[-1])
                else:
                    back = os.path.join(tmp, "out.bin")
                    res = proc.run(base + ["export", spec, back], env=env, capture=True, stderr=proc.CAPTURE)
                    if res.rc != 0:
                        why = "read (rbd export): " + ((res.stderr.strip().splitlines() or ["exit %d" % res.rc])[-1])
                    else:
                        with open(back, "rb") as f:
                            got = hashlib.sha256(f.read()).digest()
                        os.remove(back)
                        if got != hashlib.sha256(data).digest():
                            why = "read back differs from what was written"
                        else:
                            res = proc.run([cfg.SYS_PY, "-I", "-c", DISCARD_CHECK, cfg.CEPH_CONF, cfg.RBD_USER, pool,
                                            name], env=env, capture=True, stderr=proc.CAPTURE)
                            if res.rc == 3:
                                why = "discard did not read back as zeros"
                            elif res.rc != 0:
                                out("  INFO  RBD write check pool %s: discard not checked (SYS_PY %s: %s)"
                                    % (pool, cfg.SYS_PY, (res.stderr.strip().splitlines() or ["exit %d" % res.rc])[-1]))
            finally:
                proc.run(base + ["rm", spec], env=env, capture=True, stderr=proc.DEVNULL)
            if why:
                c.failed("RBD write check pool %s as %s: %s" % (pool, cfg.RBD_USER, why))
            else:
                out("  OK    RBD write check pool %s as %s: %d MiB written, read back, discarded, removed"
                    % (pool, cfg.RBD_USER, WRITE_CHECK_MIB))
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# SYS_PY -I -c DISCARD_CHECK <conf> <user> <pool> <image>: exit 0 ok, 3 not zeros after the discard
DISCARD_CHECK = """import sys, rados, rbd
conf, user, pool, name = sys.argv[1:5]
cl = rados.Rados(conffile=conf, rados_id=user)
cl.connect()
io = cl.open_ioctx(pool)
img = rbd.Image(io, name)
try:
    img.discard(0, 4194304)
    ok = img.read(0, 4194304) == bytes(4194304)
finally:
    img.close()
    io.close()
    cl.shutdown()
sys.exit(0 if ok else 3)
"""


class LineTee:
    """7.3 (2.3): the collector of 'doctor --json': every line still goes to the stream (stderr), and is kept."""

    def __init__(self, stream):
        self.stream, self.lines, self._part = stream, [], ""

    def write(self, text):
        self.stream.write(text)
        self._part += text
        while "\n" in self._part:
            line, self._part = self._part.split("\n", 1)
            self.lines.append(line)
        return len(text)

    def flush(self):
        self.stream.flush()


def run_doctor_json(write_check=False):
    """migrate doctor --json: the same checks (same commands: openstack token issue / role assignment list /
    application credential show on each side, versions show at the destination, rbd ls, systemctl is-active),
    their lines on stderr, one object on stdout."""
    import sys
    from contextlib import redirect_stdout
    from .selector import CHECK_RE
    from .jsonout import emit_json
    real = sys.stdout
    tee = LineTee(sys.stderr)
    with redirect_stdout(tee):
        rc = run_doctor(readonly=True, write_check=write_check)
        checks = []
        for line in tee.lines:
            m = CHECK_RE.match(line)
            if m:
                checks.append({"level": m.group(1), "text": m.group(2)})
        emit_json({"tool_version": __version__, "checks": checks, "passed": rc == 0,
                   "counts": {k: sum(1 for c in checks if c["level"] == k.upper()) for k in ("ok", "warn", "fail", "info")}},
                  stream=real)
    return rc


def run_doctor(readonly=False, write_check=False):
    c = Checker()
    out("migrate %s doctor:" % __version__)
    cfg = check_conf(c)
    env = base_env(cfg)
    check_programs(c, cfg, env)
    check_pythons(c, cfg, env)
    src = Cloud("source", cfg.SRC_RC, cfg.SRC_PROJECT, env)
    dst = Cloud("destination", cfg.DST_RC, cfg.DST_PROJECT, env)
    src.readonly = dst.readonly = readonly  # 7.3 (SEC-9): doctor --json
    check_side(c, src, "source")
    check_side(c, dst, "destination")
    check_openrcs(c, cfg, env)
    check_brick_auth(c, cfg, env)
    check_privileges(c, env)
    check_nova_api(dst)
    check_storage(c, cfg, env)
    check_type_map(c, cfg, dst)
    if write_check:
        write_check_pools(c, cfg, env)
    check_dirs(c)
    out("Doctor found problems: fix the FAIL lines above." if c.fail else "Doctor passed.")
    return 1 if c.fail else 0
