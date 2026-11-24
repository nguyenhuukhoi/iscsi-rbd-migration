"""Per-run options: MIG_DEFAULT_OPTS parsed first, then the command line (which therefore wins).

A line-by-line port of load_default_opts / parse_option_list / parse_options / apply_az_defaults /
pool_for_type of migrate-vm.sh, including every message and the origin tracking.
"""
import ipaddress
import re

from .config import DEFAULT_TYPE_KEY, parse_type_map
from .util import Die, ifs_split, jq_alt, jqs

# 7.1 removed --keep-server-group (following the source server group is the default): it is an unknown
# option now, on the command line and in MIG_DEFAULT_OPTS alike.
FLAG_OPTS = ("--start-source", "--keep-source-ports", "--volumes-only",
             "--in-use", "--flavor-fallback", "--no-server-group", "--no-copy-secgroup", "--ephemeral-as-image")
VALUE_OPTS = ("--mode", "--volume-type", "--az", "--volume-az", "--ip", "--flavor", "--security-group",
              "--server-group", "--max-passes", "--delta-limit", "--verify", "--dst-host")
# 7.3 (SEC-5): a hypervisor host name as Nova and 'compute service list' print it
DST_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# 1.2.0: the key of the ephemeral root disk in effective.volume_types and in the state file volume-types (the same
# word volume-map uses for it, server.EPHEMERAL_ROOT)
EPHEMERAL_ROOT_KEY = "ephemeral-root"


def map_pool(vtype, rbd_pool, rbd_pool_map):
    """(pool, named by RBD_POOL_MAP) of destination type vtype ('' = the Cinder default type: RBD_POOL)."""
    if vtype and rbd_pool_map:
        for e in ifs_split(rbd_pool_map, ","):
            if e.split(":", 1)[0] == vtype:
                return (e.split(":", 1)[1] if ":" in e else e), True
    return rbd_pool, False


def type_map(cfg):
    """VOLUME_TYPE_MAP of cfg, parsed ({} when unset; config.load already refused a malformed one)."""
    return parse_type_map(cfg.values.get("VOLUME_TYPE_MAP", ""))


def source_type(v):
    """The source volume type of a 'volume show -f json' object: its type (or volume_type) field, '' when none."""
    t = jq_alt(v.get("type"), v.get("volume_type")) if isinstance(v, dict) else None
    return "" if t is None else jqs(t)


class Options:
    def __init__(self):
        self.mode = "cold"
        self.ipmode = "same"
        self.ipaddr = ""
        self.volume_type = ""
        self.dest_az = ""
        # 7.2 (C6): the Cinder AZ of the destination volumes when it differs from the compute AZ (--volume-az,
        # MIG_VOLUME_AZ); apply_az_defaults falls back to dest_az, the behaviour up to 7.1.
        self.volume_az = ""
        self.volume_az_cli = False
        self.start_source = False
        self.keep_ports = False
        self.max_passes = "5"
        self.delta_limit_mb = "64"
        self.verify = "readback"
        self.ov_flavor = ""
        self.ov_sg = ""
        self.server_group = ""
        self.volumes_only = False
        self.in_use = False
        self.az_cli = False
        self.type_cli = False
        self.flavor_fallback = False
        # P6: the source server group and security groups are recreated at the destination by default.
        self.no_server_group = False
        self.no_copy_sg = False
        # A9: an ephemeral root disk becomes a destination volume unless this keeps it an image (the 7.x behaviour)
        self.ephemeral_as_image = False
        # 7.3 (6): the destination hypervisor (server create --host), typed per run only
        self.dst_host = ""
        # MIG_DEFAULT_OPTS bookkeeping
        self.def_opts = []
        self.def_shown = []
        self.def_notes = []
        self.cli_given = set()
        # filled by apply_az_defaults
        self.az_from = ""
        self.type_from = ""
        # 7.3 (1.1): set by pool_for_type when RBD_POOL_MAP named the pool (recorded, not decided on)
        self.pool_from = ""

    # --- MIG_DEFAULT_OPTS ------------------------------------------------------------------
    def load_default_opts(self, cmd, default_opts):
        toks = default_opts.split()
        skipped = []
        i = 0
        while i < len(toks):
            t, v = toks[i], ""
            if t in FLAG_OPTS:
                i += 1
            elif t in VALUE_OPTS:
                if i + 1 >= len(toks):
                    raise Die('MIG_DEFAULT_OPTS: %s needs a value (MIG_DEFAULT_OPTS="%s")' % (t, default_opts))
                v = toks[i + 1]
                i += 2
            else:
                raise Die("MIG_DEFAULT_OPTS: unknown option '%s' (MIG_DEFAULT_OPTS=\"%s\")" % (t, default_opts))
            if t == "--mode" and v not in ("cold", "live"):
                raise Die("MIG_DEFAULT_OPTS: --mode takes cold or live, not '%s'" % v)
            if t == "--verify" and v not in ("readback", "full", "none"):
                raise Die("MIG_DEFAULT_OPTS: --verify takes readback, full or none, not '%s'" % v)
            if t == "--dst-host":
                # 7.3 (6, SEC-13): which hypervisor gets the VM is a decision per run, like --in-use; unlike it a
                # standing --dst-host would put every VM of every run on one host, so it stops the run.
                raise Die("MIG_DEFAULT_OPTS: --dst-host is a decision per run, it is only taken from the command line "
                          "(MIG_DEFAULT_OPTS=\"%s\")" % default_opts)
            if t == "--in-use":
                # Copying an attached volume live is a decision per run, never a standing default.
                self.def_notes.append("--in-use in MIG_DEFAULT_OPTS ignored: it is only taken from the command line")
                continue
            if cmd == "migrate-volume" and t not in ("--volume-type", "--az", "--volume-az", "--verify"):
                skipped.append(t + (" " + v if v else ""))
                continue
            self.def_opts.append(t)
            if v:
                self.def_opts.append(v)
            self.def_shown.append(t + (" " + v if v else ""))
        if skipped:
            self.def_notes.append("MIG_DEFAULT_OPTS options that do not apply to migrate-volume, ignored: "
                                  + " ".join(skipped))

    def default_opts_info(self):
        lines = []
        shown = []
        for s in self.def_shown:
            if " " in s and s.split(" ", 1)[0] in self.cli_given:
                shown.append(s + " (command line wins)")
            else:
                shown.append(s)
        if shown:
            lines.append("  INFO  MIG_DEFAULT_OPTS applied before the command line: " + ", ".join(shown))
        lines.extend("  INFO  " + n for n in self.def_notes)
        return lines

    # --- parsing ---------------------------------------------------------------------------
    def parse_option_list(self, origin, args):
        suffix = "" if origin == "cli" else " (in MIG_DEFAULT_OPTS)"
        i = 0
        while i < len(args):
            a = args[i]
            if origin == "cli" and a:
                self.cli_given.add(a)

            def val():
                if i + 1 >= len(args):
                    raise Die("%s needs a value%s" % (a, suffix))
                return args[i + 1]

            if a == "--mode":
                self.mode = val(); i += 2
            elif a == "--volume-type":
                self.volume_type = val(); i += 2
                if origin == "cli":
                    self.type_cli = True
            elif a == "--az":
                self.dest_az = val(); i += 2
                if origin == "cli":
                    self.az_cli = True
            elif a == "--volume-az":
                self.volume_az = val(); i += 2
                if origin == "cli":
                    self.volume_az_cli = True
            elif a == "--ip":
                v = val()
                if v in ("same", "new"):
                    self.ipmode = v
                else:
                    # 7.2 (B4): a real address (999.1.1.1 matched the old regex and failed after the cutover).
                    try:
                        ipaddress.IPv4Address(v)
                    except ValueError:
                        raise Die("--ip takes same, new or an IPv4 address" + suffix)
                    self.ipmode, self.ipaddr = "addr", v
                i += 2
            elif a == "--start-source":
                self.start_source = True; i += 1
            elif a == "--keep-source-ports":
                self.keep_ports = True; i += 1
            elif a == "--flavor":
                self.ov_flavor = val(); i += 2
            elif a == "--security-group":
                self.ov_sg = val(); i += 2
            elif a == "--server-group":
                self.server_group = val(); i += 2
            elif a == "--flavor-fallback":
                self.flavor_fallback = True; i += 1
            elif a == "--no-server-group":
                self.no_server_group = True; i += 1
            elif a == "--no-copy-secgroup":
                self.no_copy_sg = True; i += 1
            elif a == "--ephemeral-as-image":
                self.ephemeral_as_image = True; i += 1
            elif a == "--in-use":
                self.in_use = True; i += 1
            elif a == "--volumes-only":
                self.volumes_only = True; i += 1
            elif a == "--max-passes":
                self.max_passes = val(); i += 2
            elif a == "--delta-limit":
                self.delta_limit_mb = val(); i += 2
            elif a == "--verify":
                self.verify = val(); i += 2
            elif a == "--dst-host":
                v = val()
                if not DST_HOST_RE.match(v):
                    raise Die("--dst-host takes a hypervisor host name (letters, digits, '.', '_', '-'; at most 128 "
                              "characters), not %r" % v)
                self.dst_host = v; i += 2
            else:
                raise Die("unknown option %s%s" % (a, suffix))

    def parse_options(self, args):
        if self.def_opts:
            self.parse_option_list("defaults", self.def_opts)
        self.parse_option_list("cli", args)
        if self.mode not in ("cold", "live"):
            raise Die("--mode takes cold or live")
        if self.verify not in ("readback", "full", "none"):
            raise Die("--verify takes readback, full or none")
        if self.volumes_only and self.mode != "cold":
            raise Die("--volumes-only is cold only: drop --mode live")
        # --volumes-only creates no destination VM, so a restarted source cannot clash on MAC/IP.
        if self.start_source and self.ipmode == "same" and not self.volumes_only:
            raise Die("--start-source needs --ip new or --ip <addr>: with the same MAC and IP both VMs cannot be up at once")

    # --- AZ -> type -> pool ----------------------------------------------------------------
    def apply_az_defaults(self, mig_az, az_type_map, mig_volume_az=""):
        if not self.az_cli and mig_az:
            self.dest_az, self.az_from = mig_az, "MIG_AZ"
        # --volume-az typed > MIG_VOLUME_AZ > --volume-az of MIG_DEFAULT_OPTS > the server's AZ (as --az above).
        if not self.volume_az_cli and mig_volume_az:
            self.volume_az = mig_volume_az
        if not self.volume_az:
            self.volume_az = self.dest_az
        if self.type_cli or not self.dest_az or not az_type_map:
            return
        for e in ifs_split(az_type_map, ","):
            if ":" in e and e.split(":", 1)[0] == self.dest_az and e.split(":", 1)[1]:
                self.volume_type = e.split(":", 1)[1]
                self.type_from = "AZ_TYPE_MAP (%s)" % self.dest_az
                return

    def pool_for_type(self, rbd_pool, rbd_pool_map):
        """The pool of the run's volume type (r.pool); pool_from records that RBD_POOL_MAP named it."""
        pool, mapped = map_pool(self.volume_type, rbd_pool, rbd_pool_map)
        if mapped:
            self.pool_from = "RBD_POOL_MAP"
        return pool

    # --- 1.2.0: VOLUME_TYPE_MAP, the type and pool of each volume -------------------------------------------
    def run_type_from(self):
        """Where the run's type (step 4 of type_for) comes from: cli, AZ_TYPE_MAP (<zone>), MIG_DEFAULT_OPTS, default."""
        if self.type_cli:
            return "cli"
        if self.type_from:
            return self.type_from
        if self.origin("--volume-type") == "MIG_DEFAULT_OPTS":
            return "MIG_DEFAULT_OPTS"
        return "default"

    def type_for(self, src_type, cfg):
        """(destination type, from) of one destination volume whose source volume type is src_type ('' when the
        source volume has none, and for the root disk of an ephemeral VM, which has no source volume):
        1. --volume-type typed on the command line, for every volume; 2. VOLUME_TYPE_MAP[src_type];
        3. VOLUME_TYPE_MAP['*'] (1.2.1; '__DEFAULT__' is matched in step 2 as a type name); 4. the run's type
        (AZ_TYPE_MAP, --volume-type of MIG_DEFAULT_OPTS, else '' for the Cinder default). With an empty map every
        volume gets the run's type, as up to 1.1.1."""
        if self.type_cli:
            return self.volume_type, "cli"
        tmap = type_map(cfg)
        if src_type and src_type in tmap:
            return tmap[src_type], "VOLUME_TYPE_MAP (%s)" % src_type
        if DEFAULT_TYPE_KEY in tmap:
            return tmap[DEFAULT_TYPE_KEY], "VOLUME_TYPE_MAP (%s)" % DEFAULT_TYPE_KEY
        return self.volume_type, self.run_type_from()

    @staticmethod
    def pool_for(vtype, cfg):
        """The RBD pool of destination type vtype: RBD_POOL_MAP[vtype], else RBD_POOL."""
        return map_pool(vtype, cfg.RBD_POOL, cfg.RBD_POOL_MAP)[0]

    def target(self, src_type, cfg):
        """(type, pool, from) of one destination volume (type_for, then pool_for)."""
        t, frm = self.type_for(src_type, cfg)
        return t, self.pool_for(t, cfg), frm

    def volume_targets(self, r, st, kind="vm"):
        """1.2.0: {source volume ID: (type, pool, from)} of every destination volume a run of st creates, in the
        order of the export, plus 'ephemeral-root' for the root disk of an ephemeral VM that becomes a volume (A9).
        kind vol: the one volume of volume.json. None while st holds no export (volumes.json / volume.json)."""
        name = "volume.json" if kind == "vol" else "volumes.json"
        if not st.exists(name):
            return None
        vols = [st.json(name)] if kind == "vol" else st.json(name)
        got = {}
        for v in vols:
            if isinstance(v, dict):
                got[jqs(v.get("id"))] = self.target(source_type(v), r.cfg)
        if kind == "vm" and getattr(r, "ephemeral", False) and not self.ephemeral_as_image and not self.volumes_only:
            got[EPHEMERAL_ROOT_KEY] = self.target("", r.cfg)
        return got

    @staticmethod
    def type_map_set(cfg):
        """VOLUME_TYPE_MAP has at least one entry (an empty map keeps every line of 1.1.1 as it was)."""
        return bool(type_map(cfg))

    def conf_origin_info(self):
        lines = []
        if self.az_from:
            lines.append("  INFO  availability zone %s from %s" % (self.dest_az, self.az_from))
        if self.type_from:
            lines.append("  INFO  volume type %s from %s" % (self.volume_type, self.type_from))
        return lines

    # --- 7.3 (1.1): the effective options and where each one came from -----------------------------------
    def origin(self, opt):
        """cli (typed), MIG_DEFAULT_OPTS (applied from it), else default: the bookkeeping above, nothing new."""
        if opt in self.cli_given:
            return "cli"
        if any(s.split(" ", 1)[0] == opt for s in self.def_shown):
            return "MIG_DEFAULT_OPTS"
        return "default"

    def effective(self, r, st=None, kind="vm"):
        """{key: {"value", "from"[, "detail"]}}; from is one of cli, default, MIG_DEFAULT_OPTS, conf, az, source.
        Only records what the run already decided (cli_given, def_shown, az_from, type_from, pool_from and, after
        the preflight, r.sgplan / r.ksg_dst / r.flavor_from). Keys that need the exported VM (flavor, security
        groups, server group) appear once st holds server.json."""
        def e(value, frm, detail=""):
            d = {"value": value, "from": frm}
            if detail:
                d["detail"] = detail
            return d

        def num(v):
            return int(v) if isinstance(v, str) and v.isdigit() else v

        cfg = r.cfg
        eff = {}
        if kind == "vm":
            eff["mode"] = e(self.mode, self.origin("--mode"))
            eff["ip"] = e(self.ipaddr if self.ipmode == "addr" else self.ipmode, self.origin("--ip"))
        if self.type_cli:
            eff["volume_type"] = e(self.volume_type, "cli")
        elif self.type_from:
            eff["volume_type"] = e(self.volume_type, "conf", self.type_from)
        elif self.origin("--volume-type") == "MIG_DEFAULT_OPTS":
            eff["volume_type"] = e(self.volume_type, "MIG_DEFAULT_OPTS")
        else:
            eff["volume_type"] = e(self.volume_type, "default")
        eff["pool"] = e(r.pool, "conf", "RBD_POOL_MAP") if self.pool_from else e(r.pool, "default", "RBD_POOL")
        vtypes = self.volume_targets(r, st, kind) if st is not None else None
        if vtypes is not None:
            # 1.2.0: the type and pool of each destination volume (keyed by source volume ID, the root disk of an
            # ephemeral VM by 'ephemeral-root'). Wrapped like every other key: {"value", "from"[, "detail"]}.
            value = {k: {"type": t, "pool": p, "from": f} for k, (t, p, f) in vtypes.items()}
            if any(f.startswith("VOLUME_TYPE_MAP") for _t, _p, f in vtypes.values()):
                eff["volume_types"] = e(value, "conf", "VOLUME_TYPE_MAP")
            else:
                eff["volume_types"] = dict(eff["volume_type"], value=value)
        if self.az_cli:
            eff["az"] = e(self.dest_az, "cli")
        elif self.az_from == "MIG_AZ":
            eff["az"] = e(self.dest_az, "conf", "MIG_AZ")
        elif self.az_from == "host":
            eff["az"] = e(self.dest_az, "cli", "zone of --dst-host %s" % self.dst_host)
        elif self.dest_az and self.origin("--az") == "MIG_DEFAULT_OPTS":
            eff["az"] = e(self.dest_az, "MIG_DEFAULT_OPTS")
        elif kind == "vm" and getattr(r, "prefix", None):
            # azname.resolve names the copies after the destination's only compute AZ; the server and the
            # volumes are still created without --availability-zone (the scheduler picks).
            eff["az"] = e("", "default", "scheduler picks; copies named %s-<name> (the destination's only compute AZ)"
                          % r.prefix)
        else:
            eff["az"] = e(self.dest_az, "default")
        if self.volume_az_cli:
            eff["volume_az"] = e(self.volume_az, "cli")
        elif cfg.MIG_VOLUME_AZ:
            eff["volume_az"] = e(self.volume_az, "conf", "MIG_VOLUME_AZ")
        elif self.volume_az and self.origin("--volume-az") == "MIG_DEFAULT_OPTS":
            eff["volume_az"] = e(self.volume_az, "MIG_DEFAULT_OPTS")
        elif self.volume_az:
            eff["volume_az"] = e(self.volume_az, "az")
        else:
            eff["volume_az"] = e("", "default")
        if kind == "vm":
            eff["dst_host"] = e(self.dst_host, "cli" if self.dst_host else "default")
        keys = (("verify", "--verify", self.verify), ("in_use", "--in-use", self.in_use))
        if kind == "vm":
            keys += (("max_passes", "--max-passes", num(self.max_passes)),
                     ("delta_limit_mb", "--delta-limit", num(self.delta_limit_mb)),
                     ("start_source", "--start-source", self.start_source),
                     ("keep_ports", "--keep-source-ports", self.keep_ports),
                     ("flavor_fallback", "--flavor-fallback", self.flavor_fallback),
                     ("no_server_group", "--no-server-group", self.no_server_group),
                     ("no_copy_sg", "--no-copy-secgroup", self.no_copy_sg),
                     ("volumes_only", "--volumes-only", self.volumes_only),
                     ("ephemeral_as_image", "--ephemeral-as-image", self.ephemeral_as_image))
        for key, opt, value in keys:
            eff[key] = e(value, self.origin(opt))
        sp, dp = cfg.SRC_PROJECT, cfg.DST_PROJECT
        if sp and dp and sp != dp:
            eff["projects"] = e({"src": sp, "dst": dp}, "conf", "PROJECT_MAP_FILE" if cfg.PROJECT_MAP_FILE
                                else "SRC_PROJECT/DST_PROJECT")
        else:
            eff["projects"] = e({"src": sp, "dst": dp}, "default")
        if kind == "vm" and st is not None and st.exists("server.json"):
            self._effective_server(r, st, eff, e)
        return eff

    def _effective_server(self, r, st, eff, e):
        """flavor, security_groups and server_group: known once the VM is exported (and the preflight ran)."""
        from .context import flavor_name
        if self.volumes_only:
            why = "--volumes-only creates no server"
            eff["flavor"] = e(None, "default", why)
            eff["security_groups"] = e([], "default", why)
            eff["server_group"] = e(None, "default", why)
            return
        server = st.json("server.json")
        if self.ov_flavor and "--flavor" in self.cli_given:
            eff["flavor"] = e(self.ov_flavor, "cli")
        elif self.ov_flavor and getattr(r, "flavor_from", ""):
            eff["flavor"] = e(self.ov_flavor, "conf", r.flavor_from)
        elif self.ov_flavor:
            eff["flavor"] = e(self.ov_flavor, self.origin("--flavor"))
        else:
            eff["flavor"] = e(flavor_name(server), "source")
        ports = st.json("ports.json") if st.exists("ports.json") else []
        names = []
        for p in ports:
            names += [s for s in (p.get("security_groups") or []) if s and s not in names]
        if self.ov_sg:
            eff["security_groups"] = e([{"src": None, "dst": n, "action": "reuse"} for n in ifs_split(self.ov_sg, ",")],
                                       self.origin("--security-group"))
        elif self.no_copy_sg:
            eff["security_groups"] = e([{"src": n, "dst": n, "action": "reuse"} for n in names],
                                       self.origin("--no-copy-secgroup"), "same-name groups of the destination")
        elif getattr(r, "sgplan", None) is not None:
            eff["security_groups"] = e([{"src": g.name, "dst": g.dst_name, "action": "create" if g.create else "reuse"}
                                        for g in r.sgplan.groups.values()], "source")
        else:
            eff["security_groups"] = e([{"src": n, "dst": None, "action": None} for n in names], "source",
                                       "not planned" if names else "")
        if self.server_group:
            eff["server_group"] = e({"src": None, "dst": self.server_group, "policy": None},
                                    self.origin("--server-group"))
        elif self.no_server_group:
            eff["server_group"] = e(None, self.origin("--no-server-group"))
        elif getattr(r, "ksg_dst", ""):
            pre = (r.prefix or "") + "-"
            eff["server_group"] = e({"src": r.ksg_dst[len(pre):] if r.ksg_dst.startswith(pre) else None,
                                     "dst": r.ksg_dst, "policy": r.ksg_policy or None}, "source")
        else:
            eff["server_group"] = e(None, "source")
