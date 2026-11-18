"""Per-run options: MIG_DEFAULT_OPTS parsed first, then the command line (which therefore wins).

A line-by-line port of load_default_opts / parse_option_list / parse_options / apply_az_defaults /
pool_for_type of migrate-vm.sh, including every message and the origin tracking.
"""
import re

from .util import Die, ifs_split

FLAG_OPTS = ("--start-source", "--keep-source-ports", "--keep-server-group", "--volumes-only",
             "--in-use", "--flavor-fallback")
VALUE_OPTS = ("--mode", "--volume-type", "--az", "--ip", "--flavor", "--security-group",
              "--server-group", "--max-passes", "--delta-limit", "--verify")
IPV4_RE = re.compile(r"[0-9]{1,3}(\.[0-9]{1,3}){3}")


class Options:
    def __init__(self):
        self.mode = "cold"
        self.ipmode = "same"
        self.ipaddr = ""
        self.volume_type = ""
        self.dest_az = ""
        self.start_source = False
        self.keep_ports = False
        self.max_passes = "5"
        self.delta_limit_mb = "64"
        self.verify = "readback"
        self.ov_flavor = ""
        self.ov_sg = ""
        self.server_group = ""
        self.volumes_only = False
        self.keep_sg = False
        self.in_use = False
        self.az_cli = False
        self.type_cli = False
        self.flavor_fallback = False
        # MIG_DEFAULT_OPTS bookkeeping
        self.def_opts = []
        self.def_shown = []
        self.def_notes = []
        self.keep_sg_from = ""
        self.server_group_from = ""
        self.cli_given = set()
        # filled by apply_az_defaults
        self.az_from = ""
        self.type_from = ""

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
            if t == "--in-use":
                # Copying an attached volume live is a decision per run, never a standing default.
                self.def_notes.append("--in-use in MIG_DEFAULT_OPTS ignored: it is only taken from the command line")
                continue
            if cmd == "migrate-volume" and t not in ("--volume-type", "--az", "--verify"):
                skipped.append(t + (" " + v if v else ""))
                continue
            self.def_opts.append(t)
            if v:
                self.def_opts.append(v)
            self.def_shown.append(t + (" " + v if v else ""))
        if skipped:
            self.def_notes.append("MIG_DEFAULT_OPTS options that do not apply to migrate-volume, ignored: "
                                  + " ".join(skipped))

    def drop_shown(self, opt):
        self.def_shown = [s for s in self.def_shown if s.split(" ", 1)[0] != opt]

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
            elif a == "--ip":
                v = val()
                if v in ("same", "new"):
                    self.ipmode = v
                elif IPV4_RE.fullmatch(v):
                    self.ipmode, self.ipaddr = "addr", v
                else:
                    raise Die("--ip takes same, new or an IPv4 address" + suffix)
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
                self.server_group = val(); self.server_group_from = origin; i += 2
            elif a == "--keep-server-group":
                self.keep_sg = True; self.keep_sg_from = origin; i += 1
            elif a == "--flavor-fallback":
                self.flavor_fallback = True; i += 1
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
        # One of the two from MIG_DEFAULT_OPTS, the other typed: the typed one wins.
        if self.server_group and self.keep_sg and self.keep_sg_from != self.server_group_from:
            if self.keep_sg_from == "defaults":
                self.keep_sg = False
                self.drop_shown("--keep-server-group")
                self.def_notes.append("--keep-server-group from MIG_DEFAULT_OPTS dropped: --server-group %s is on the command line" % self.server_group)
            else:
                self.drop_shown("--server-group")
                self.def_notes.append("--server-group %s from MIG_DEFAULT_OPTS dropped: --keep-server-group is on the command line" % self.server_group)
                self.server_group = ""
        if self.server_group and self.keep_sg:
            raise Die("choose one: --server-group <name> or --keep-server-group"
                      + (" (both in MIG_DEFAULT_OPTS)" if self.keep_sg_from == "defaults" else ""))

    # --- AZ -> type -> pool ----------------------------------------------------------------
    def apply_az_defaults(self, mig_az, az_type_map):
        if not self.az_cli and mig_az:
            self.dest_az, self.az_from = mig_az, "MIG_AZ"
        if self.type_cli or not self.dest_az or not az_type_map:
            return
        for e in ifs_split(az_type_map, ","):
            if ":" in e and e.split(":", 1)[0] == self.dest_az and e.split(":", 1)[1]:
                self.volume_type = e.split(":", 1)[1]
                self.type_from = "AZ_TYPE_MAP (%s)" % self.dest_az
                return

    def pool_for_type(self, rbd_pool, rbd_pool_map):
        if self.volume_type and rbd_pool_map:
            for e in ifs_split(rbd_pool_map, ","):
                if e.split(":", 1)[0] == self.volume_type:
                    return e.split(":", 1)[1] if ":" in e else e
        return rbd_pool

    def conf_origin_info(self):
        lines = []
        if self.az_from:
            lines.append("  INFO  availability zone %s from %s" % (self.dest_az, self.az_from))
        if self.type_from:
            lines.append("  INFO  volume type %s from %s" % (self.volume_type, self.type_from))
        return lines
