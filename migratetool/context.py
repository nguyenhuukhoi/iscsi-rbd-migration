"""Everything one command run shares (the bash build kept it in global variables)."""
import time

from . import proc
from .clouds import Cloud, base_env
from .util import Die, jq_alt


class Run:
    def __init__(self, cfg, opts):
        self.cfg = cfg
        self.opts = opts
        self.env = base_env(cfg)
        self.src = Cloud("source", cfg.SRC_RC, cfg.SRC_PROJECT, self.env)
        self.dst = Cloud("destination", cfg.DST_RC, cfg.DST_PROJECT, self.env)
        self.pool = cfg.RBD_POOL
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

    def check_project_scope(self):
        """An application credential cannot be rescoped: fail before touching anything."""
        for project, cloud in ((self.cfg.SRC_PROJECT, self.src), (self.cfg.DST_PROJECT, self.dst)):
            if project and "applicationcredential" in cloud.auth_type():
                raise Die("SRC_PROJECT/DST_PROJECT needs a password openrc: an application credential "
                          "is bound to one project (%s)" % cloud.rc)

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
