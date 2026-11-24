"""Configuration layer: $MIG_CONF (bash KEY=VALUE file) + environment + built-in defaults.

Precedence as in the bash build: command-line option > environment > $MIG_CONF > default. A
variable of CONF_VARS already present in the environment (even empty) beats the file; an empty
value then falls back to the default, as ${VAR:-default} does.
"""
import os
import re
import shlex
from dataclasses import dataclass, field

from .util import Die, ifs_split

DEFAULT_CONF = "/opt/mig/migrate.conf"
STATE_ROOT = "/var/lib/mig-migrate"
ATTACH_DIR = "/var/lib/mig-attach"  # brick.py's records
LOG_ROOT = "/var/log/mig-batch"     # one directory per selector run, as migrate-batch.sh wrote

# The variables the bash conf loader saves from the environment before sourcing the file.
CONF_VARS = (
    "SRC_RC", "DST_RC", "RBD_POOL", "RBD_POOL_MAP", "RBD_USER", "CEPH_CONF", "MIG_ISCSI_IP",
    "MIG_MULTIPATH", "COPY_STREAMS", "PROJECT_MAP_FILE", "SRC_PROJECT", "DST_PROJECT", "MIG_AZ",
    "MIG_VOLUME_AZ", "AZ_TYPE_MAP", "MIG_DEFAULT_OPTS", "FLAVOR_MAP_FILE",
    # 1.0.0: the host lock (A7), the deadline of control-plane commands (C3), the cold-copy size warning (A8)
    "MIG_LOCK", "MIG_CMD_TIMEOUT", "MIG_COLD_WARN_GB",
    # 1.2.0: the destination volume type of each volume, by the name of its source volume type
    "VOLUME_TYPE_MAP",
)
DEFAULTS = {
    "SRC_RC": "/opt/mig/src/admin-openrc.sh",
    "DST_RC": "/opt/mig/dst/admin-openrc.sh",
    "RBD_POOL": "mig-volumes",
    "RBD_POOL_MAP": "",
    "MIG_AZ": "",
    "MIG_VOLUME_AZ": "",
    "AZ_TYPE_MAP": "",
    "VOLUME_TYPE_MAP": "",
    "MIG_DEFAULT_OPTS": "",
    "FLAVOR_MAP_FILE": "",
    "RBD_USER": "mig-xfer",
    "COPY_STREAMS": "8",
    "MIG_ISCSI_IP": "",  # this host's address on the iSCSI network: no default (doctor FAILs until it is set)
    "CEPH_CONF": "/etc/ceph/ceph.conf",
    "MIG_LOCK": "/run/mig/migrate.lock",
    "MIG_CMD_TIMEOUT": "600",
    "MIG_COLD_WARN_GB": "500",
}
_ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)


@dataclass
class Config:
    conf_path: str = DEFAULT_CONF
    values: dict = field(default_factory=dict)   # CONF_VARS (+ SYS_PY) after precedence/defaults
    multipath_set: bool = False                  # MIG_MULTIPATH was set at all (bash exports it then)
    exported: dict = field(default_factory=dict)  # other 'export X=...' lines of the conf file

    def __getattr__(self, name):
        try:
            return self.__dict__["values"][name]
        except KeyError:
            raise AttributeError(name)


def parse_conf_file(path):
    """{name: (value, exported)} from a bash KEY=VALUE file. Anything that only bash could
    evaluate ($-expansion, commands) is refused instead of being misread."""
    result = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for no, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            exported = False
            if line.startswith("export ") or line.startswith("export\t"):
                exported, line = True, line[6:].lstrip()
            try:
                tokens = shlex.split(line, comments=True, posix=True)
            except ValueError as e:
                raise Die("%s line %d: cannot parse %r (%s)" % (path, no, raw.rstrip("\n"), e))
            if not tokens:
                continue
            if "$" in line.split("#", 1)[0] and not _single_quoted_only(line):
                raise Die("%s line %d: $-expansion is not supported by migrate, write the value "
                          "literally: %s" % (path, no, raw.rstrip("\n")))
            for tok in tokens:
                m = _ASSIGN.match(tok)
                if not m:
                    raise Die("%s line %d: only KEY=VALUE assignments are supported, got: %s"
                              % (path, no, raw.rstrip("\n")))
                result[m.group(1)] = (m.group(2), exported)
    return result


def _single_quoted_only(line):
    """True when every '$' of the line sits inside single quotes (bash leaves those literal)."""
    inside = False
    for ch in line:
        if ch == "'":
            inside = not inside
        elif ch == "$" and not inside:
            return False
        elif ch == "#" and not inside:
            break
    return True


def load(environ=None):
    env = os.environ if environ is None else environ
    conf_path = env.get("MIG_CONF") or DEFAULT_CONF
    conf = {}
    if os.access(conf_path, os.R_OK) and os.path.isfile(conf_path):
        conf = parse_conf_file(conf_path)
    cfg = Config(conf_path=conf_path)
    for var in CONF_VARS:
        if var in env:
            value = env[var]
        elif var in conf:
            value = conf[var][0]
        else:
            value = None
        if var == "MIG_MULTIPATH":
            cfg.multipath_set = value is not None
        if not value and var in DEFAULTS:
            value = DEFAULTS[var]
        cfg.values[var] = value if value is not None else ""
    # Not saved from the environment by the bash loader: the file wins over the environment.
    cfg.values["SYS_PY"] = (conf["SYS_PY"][0] if "SYS_PY" in conf else env.get("SYS_PY")) \
        or "/usr/bin/python3"
    cfg.exported = {k: v for k, (v, exp) in conf.items() if exp and k not in CONF_VARS}
    for var in ("MIG_CMD_TIMEOUT", "MIG_COLD_WARN_GB"):
        if not cfg.values[var].isdigit():
            raise Die("%s takes a whole number, not %r" % (var, cfg.values[var]))
    parse_type_map(cfg.values["VOLUME_TYPE_MAP"])  # 1.2.0: a malformed map stops here, before any cloud call
    from . import proc
    proc.CMD_TIMEOUT = int(cfg.MIG_CMD_TIMEOUT)  # C3: 0 turns the deadline off
    return cfg


# 1.2.1: the fallback key of VOLUME_TYPE_MAP. It was "__DEFAULT__" in 1.2.0, which is also the name Cinder gives its
# real default volume type: "__DEFAULT__" is now a source type name like any other (as RBD_POOL_MAP reads it).
DEFAULT_TYPE_KEY = "*"


def parse_type_map(text):
    """VOLUME_TYPE_MAP (1.2.0) as {source type name: destination type name}, in the order written; '' gives {}.
    Same shape as AZ_TYPE_MAP and RBD_POOL_MAP: "src:dst,src:dst", split on ',' then once on ':'. The source key
    '*' (1.2.1, DEFAULT_TYPE_KEY) names the destination type of a source volume whose type has no entry (or that has no
    type); every other source key, '__DEFAULT__' included, is matched as the exact type name. Every entry must have a
    source, a ':' and a destination, '*' is never a destination, and a source type is named once: anything else is a
    Die."""
    found = {}
    for entry in ifs_split(text or "", ","):
        if ":" not in entry:
            raise Die("VOLUME_TYPE_MAP: entry %r has no ':' (write src_type:dst_type, entries separated by ',')"
                      % entry)
        src, dst = entry.split(":", 1)
        if not src:
            raise Die("VOLUME_TYPE_MAP: entry %r has no source type (write src_type:dst_type)" % entry)
        if not dst:
            raise Die("VOLUME_TYPE_MAP: entry %r has no destination type (write src_type:dst_type)" % entry)
        if dst == DEFAULT_TYPE_KEY:
            raise Die("VOLUME_TYPE_MAP: '%s' is only a source key" % DEFAULT_TYPE_KEY)
        if src in found:
            raise Die("VOLUME_TYPE_MAP: source type %r is mapped twice (%s:%s and %s)" % (src, src, found[src], entry))
        found[src] = dst
    return found
