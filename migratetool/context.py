"""Everything one command run shares (the bash build kept it in global variables)."""
import sys
import time

from . import proc
from .clouds import Cloud, auth_url, base_env
from .util import Die, jq_alt


class Run:
    def __init__(self, cfg, opts):
        self.cfg = cfg
        self.opts = opts
        self.env = base_env(cfg)
        self.src = Cloud("source", cfg.SRC_RC, cfg.SRC_PROJECT, self.env)
        self.dst = Cloud("destination", cfg.DST_RC, cfg.DST_PROJECT, self.env)
        self.pool = cfg.RBD_POOL  # the pool of the run's volume type (header, 'options' line, doctor)
        self.dpool = ""  # 1.2.0: the pool of the destination volume being created or copied (VOLUME_TYPE_MAP)
        self.ephemeral = False
        self.flavor_map = {}
        # the source server group, recreated at the destination (default since 7.1; 7.0: --keep-server-group)
        self.ksg_dst = ""
        self.ksg_policy = ""
        self.ksg_rules = {}
        self.ksg_id = ""
        # P6: name prefix of the destination copies (azname.resolve; None = not asked yet, "" = unresolved),
        # the security group copy plan (secgroups.Plan), the destination's network extension aliases, and
        # the source ports whose dns_name is copied (by port index)
        self.prefix = None
        self.sgplan = None
        self.net_ext = None
        self.dns_copy = set()
        # set while a clone is attached to this host (bash ATTACHED); the watcher thread (WATCH_PID)
        self.attached = ""
        self.watcher = None
        self.clone = ""
        self.clone_time = ""
        self.device = ""
        self.dvid = ""
        # 7.2 (D3): seconds spent inside the downtime window copying and verifying data (the rest is the
        # control plane: openstack calls, waits, attach/detach)
        self.t_copy = 0.0
        self.t_verify = 0.0
        self.t_glance = 0.0  # 7.3 (8, D3b): the part of t_copy an ephemeral root disk spent going through Glance
        # 7.3 (1.2): what options.json records: the options as typed (child argv), the start time, and the
        # flavor map that chose the destination flavor (--flavor-fallback)
        self.argv = []
        self.started = ""
        self.flavor_from = ""
        # C8: set once t-stop is written (a later failure exits 4, an earlier one 3); exit_code overrides both
        self.cut = False
        self.exit_code = None
        self.ksg_create = False  # D16: the preflight found no '<AZ>-<group>' at the destination: migrate creates it

    def check_project_scope(self):
        """An application credential cannot be rescoped: fail before touching anything."""
        for project, cloud in ((self.cfg.SRC_PROJECT, self.src), (self.cfg.DST_PROJECT, self.dst)):
            if project and "applicationcredential" in cloud.auth_type():
                if getattr(self.cfg, "scope_origin", None) == "record":
                    # 1.2.0: a rollback whose scope came from the state (cli.rollback_scope): the openrc is the
                    # problem, not SRC_PROJECT/DST_PROJECT (this run did not set them, or set them as recorded).
                    raise Die("this state was written by a run scoped with SRC_PROJECT=%s DST_PROJECT=%s (a password "
                              "openrc); %s is an application credential, which cannot be rescoped: run the rollback "
                              "with the password openrc that wrote the state; nothing was done"
                              % (self.cfg.SRC_PROJECT or "none", self.cfg.DST_PROJECT or "none", cloud.rc))
                raise Die("SRC_PROJECT/DST_PROJECT needs a password openrc: an application credential "
                          "is bound to one project (%s)" % cloud.rc)
        # B3: two openrcs of one Keystone are almost always a copy-paste mistake ('migrate doctor' FAILs on it).
        src_url = auth_url(self.cfg.SRC_RC, self.env)
        if src_url and src_url == auth_url(self.cfg.DST_RC, self.env):
            sys.stderr.write("WARN SRC_RC and DST_RC point at the same Keystone (%s): migrate moves VMs between two "
                             "clouds\n" % src_url)
            sys.stderr.flush()

    def rbd(self, *args, quiet=True):
        r = proc.run(["rbd", "-c", self.cfg.CEPH_CONF, "--id", self.cfg.RBD_USER, *args],
                     env=self.env, capture=True, stderr=proc.DEVNULL if quiet else proc.INHERIT)
        return r.rc == 0

    # --- waits (bash wait_volume / wait_server) -----------------------------------------------
    def wait_volume(self, cloud, vid, want, limit=3600):
        start = time.monotonic()
        while True:
            s = cloud.try_value_inherit("volume", "show", vid, "-f", "value", "-c", "status")
            if s == want:
                return
            if s.startswith("error"):
                raise Die("volume %s entered status %s" % (vid, s))
            if time.monotonic() - start > limit:
                raise Die("timeout waiting for volume %s (%s, want %s)" % (vid, s, want))
            time.sleep(3)

    def wait_server(self, cloud, sid, want, limit=600):
        start = time.monotonic()
        while True:
            s = cloud.try_value_inherit("server", "show", sid, "-f", "value", "-c", "status")
            if s == want:
                return
            if s == "ERROR":
                raise Die("server %s entered ERROR" % sid)
            if time.monotonic() - start > limit:
                raise Die("timeout waiting for server %s (%s, want %s)" % (sid, s, want))
            time.sleep(3)


def flavor_name(server):
    """'.flavor | if type=="object" then (.original_name // .name) else (. | split(" ")[0]) end'"""
    f = server.get("flavor")
    if isinstance(f, dict):
        v = jq_alt(f.get("original_name"), f.get("name"))
        return "" if v is None else str(v)
    if isinstance(f, str):
        return f.split(" ")[0]
    return ""
