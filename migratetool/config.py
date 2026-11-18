"""Configuration layer: $MIG_CONF (bash KEY=VALUE file) + environment + built-in defaults.

Precedence as in the bash build: command-line option > environment > $MIG_CONF > default. A
variable of CONF_VARS already present in the environment (even empty) beats the file; an empty
value then falls back to the default, as ${VAR:-default} does.
"""
import os
import re
import shlex
from dataclasses import dataclass, field

from .util import Die

DEFAULT_CONF = "/opt/mig/migrate.conf"
STATE_ROOT = "/var/lib/mig-migrate"
ATTACH_DIR = "/var/lib/mig-attach"  # brick.py's records

# The variables the bash conf loader saves from the environment before sourcing the file.
CONF_VARS = (
    "SRC_RC", "DST_RC", "RBD_POOL", "RBD_POOL_MAP", "RBD_USER", "CEPH_CONF", "MIG_ISCSI_IP",
    "MIG_MULTIPATH", "COPY_STREAMS", "PROJECT_MAP_FILE", "SRC_PROJECT", "DST_PROJECT", "MIG_AZ",
    "AZ_TYPE_MAP", "MIG_DEFAULT_OPTS", "FLAVOR_MAP_FILE",
)
DEFAULTS = {
    "SRC_RC": "/opt/mig/src/admin-openrc.sh",
    "DST_RC": "/opt/mig/dst/admin-openrc.sh",
    "RBD_POOL": "mig-volumes",
    "RBD_POOL_MAP": "",
    "MIG_AZ": "",
    "AZ_TYPE_MAP": "",
    "MIG_DEFAULT_OPTS": "",
    "FLAVOR_MAP_FILE": "",
    "RBD_USER": "mig-xfer",
    "COPY_STREAMS": "8",
    "MIG_ISCSI_IP": "10.77.0.20",
    "CEPH_CONF": "/etc/ceph/ceph.conf",
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
    return cfg
