"""'migrate doctor' (7.2, D2): is this host ready to migrate? Read-only, needs no VM.

One '  OK    ' / '  FAIL  ' / '  WARN  ' / '  INFO  ' line per item, exit 1 when any FAIL:
  1. the programs: openstack, qemu-img, rbd, iscsiadm (multipath with MIG_MULTIPATH=1);
  2. the brick child imports os-brick and the Cinder client (python -I -m migratetool.brick --check);
  3. SYS_PY imports the librbd bindings, the bundled blocksync.py is readable;
  4. the conf file parses, both openrcs are readable, OS_AUTH_TYPE is one brick understands;
  5. a token on each side; 6. the admin role (--ip same creates ports with --mac-address);
  7. RBD access to RBD_POOL and every pool of RBD_POOL_MAP, iscsid (and multipathd) running;
  8. the state and log directories can be created and written.
Every check runs even after a FAIL, so one run lists everything to fix.
"""
import json
import os

from . import __version__, config, proc
from .clouds import Cloud, base_env, rc_env
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
        c.failed("brick cannot start (%s): install os-brick and python-cinderclient into this venv (DEPLOY-PYTHON "
                 "section 3)" % ((res.stderr.strip().splitlines() or ["exit %d" % res.rc])[-1]))
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
        return config.load(dict(os.environ, MIG_CONF="/nonexistent"))
    if os.path.isfile(path) and os.access(path, os.R_OK):
        out("  OK    conf %s" % path)
    else:
        out("  INFO  conf %s not found: defaults and environment only" % path)
    return cfg


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
    appcred = "applicationcredential" in auth
    if appcred:
        out("  INFO  %s: application credential: bound to its own project (SRC_PROJECT/DST_PROJECT cannot rescope it)"
            % label)
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
    if roles is None:
        out("  INFO  %s: roles not checked: %s"
            % (label, (res.stderr.strip().splitlines() or ["exit %d" % res.rc])[-1]))
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


def check_dirs(c):
    for label, path in (("state directory", config.STATE_ROOT), ("log directory", config.LOG_ROOT)):
        try:
            os.makedirs(path, exist_ok=True)
            good = os.access(path, os.W_OK)
        except OSError:
            good = False
        c.check("%s %s writable" % (label, path), lambda: good)


def run_doctor():
    c = Checker()
    out("migrate %s doctor:" % __version__)
    cfg = check_conf(c)
    env = base_env(cfg)
    check_programs(c, cfg, env)
    check_pythons(c, cfg, env)
    check_side(c, Cloud("source", cfg.SRC_RC, cfg.SRC_PROJECT, env), "source")
    check_side(c, Cloud("destination", cfg.DST_RC, cfg.DST_PROJECT, env), "destination")
    check_storage(c, cfg, env)
    check_dirs(c)
    out("Doctor found problems: fix the FAIL lines above." if c.fail else "Doctor passed.")
    return 1 if c.fail else 0
