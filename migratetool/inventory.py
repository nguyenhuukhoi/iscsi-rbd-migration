"""'migrate inventory' (7.3, DESIGN.md section 7): what both clouds offer, for the dropdowns of the UI.

    migrate inventory [--count [--host H,..]] [--json]

openstack commands, every one through a read-only Cloud (SEC-9), plain openrcs (no SRC_PROJECT/DST_PROJECT scope):
  both         token issue -f json                                (no token: stop, run 'migrate doctor' first)
  both         project list -f json                               (refused, e.g. an application credential: [] and
                                                                   projects_error, the rest goes on)
  both         compute service list --service nova-compute -f json
  destination  availability zone list --compute -f json, availability zone list --volume -f json
  destination  flavor list --all -f json, volume type list -f json
  source       server list --all-projects --host H -f value -c ID  once per host, only with --count (at most 64
                                                                   hosts: name them with --host beyond that, SEC-16)
"""
import json

from . import __version__, config, proc
from .azname import zones_from_listing
from .clouds import Cloud, base_env, rc_env
from .jsonout import emit_json, stdout_to_stderr
from .listing import _int, _str, check_value
from .record import conf_view
from .selector import _add_list
from .util import Die, lines, out

COUNT_MAX_HOSTS = 64


def parse(args):
    want = {"json": False, "count": False, "hosts": []}
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--json":
            want["json"] = True
        elif a == "--count":
            want["count"] = True
        elif a == "--host":
            if i + 1 >= len(args) or not args[i + 1]:
                raise Die("--host needs a value")
            vals = []
            _add_list(vals, args[i + 1])
            want["hosts"] += [check_value("--host", v) for v in vals]
            i += 1
        else:
            raise Die("unknown option %s" % a)
        i += 1
    if want["hosts"] and not want["count"]:
        raise Die("--host names the hosts whose VMs --count counts: add --count")
    return want


def _rows(cloud, *args):
    """(rows, error): a JSON listing; error = its last stderr line when it failed."""
    res = cloud.run(*args, stderr=proc.CAPTURE)
    if res.rc != 0:
        return [], ((res.stderr or "").strip().splitlines() or ["exit %d" % res.rc])[-1]
    try:
        got = json.loads(res.stdout or "[]")
    except ValueError:
        return [], "not JSON"
    return [x for x in got if isinstance(x, dict)] if isinstance(got, list) else [], ""


def side(cloud, label):
    res = cloud.run("token", "issue", "-f", "json", stderr=proc.CAPTURE)
    try:
        tok = json.loads(res.stdout) if res.rc == 0 else None
    except ValueError:
        tok = None
    if not isinstance(tok, dict):
        raise Die("cannot get a %s token (%s): run 'migrate doctor' first"
                  % (label, ((res.stderr or "").strip().splitlines() or ["exit %d" % res.rc])[-1]))
    pid = _str(tok.get("project_id"))
    env = rc_env(cloud.rc, cloud._base)
    d = {"auth_type": env.get("OS_AUTH_TYPE") or "password"}
    projects, err = _rows(cloud, "project", "list", "-f", "json")
    d["projects"] = [{"id": _str(p.get("ID")), "name": _str(p.get("Name"))} for p in projects]
    if err:
        d["projects_error"] = err
    d["scope_project"] = next((p["name"] for p in d["projects"] if p["id"] == pid), "") \
        or env.get("OS_PROJECT_NAME") or pid
    hosts, err = _rows(cloud, "compute", "service", "list", "--service", "nova-compute", "-f", "json")
    d["hosts"] = [{"host": _str(h.get("Host")), "zone": _str(h.get("Zone")), "state": _str(h.get("State")),
                   "status": _str(h.get("Status"))} for h in hosts]
    if err:
        d["hosts_error"] = err
    return d


def collect(want, cfg):
    env = base_env(cfg)
    src = Cloud("source", cfg.SRC_RC, "", env)
    dst = Cloud("destination", cfg.DST_RC, "", env)
    src.readonly = dst.readonly = True
    s, d = side(src, "source"), side(dst, "destination")
    if want["count"]:
        hosts = want["hosts"] or [h["host"] for h in s["hosts"] if h["host"]]
        if len(hosts) > COUNT_MAX_HOSTS:
            raise Die("--count over %d hosts (%d): name the hosts to count with --host" % (COUNT_MAX_HOSTS, len(hosts)))
        by_host = {h["host"]: h for h in s["hosts"]}
        for h in hosts:
            res = src.run("server", "list", "--all-projects", "--host", h, "-f", "value", "-c", "ID",
                          stderr=proc.DEVNULL)
            entry = by_host.get(h)
            if entry is None:
                entry = {"host": h, "zone": "", "state": "", "status": ""}
                s["hosts"].append(entry)
            entry["vms"] = len(lines(res.stdout)) if res.rc == 0 else None
    zones = {}
    for kind in ("compute", "volume"):
        rows, err = _rows(dst, "availability", "zone", "list", "--" + kind, "-f", "json")
        zones[kind] = zones_from_listing(rows) if kind == "compute" else \
            [n for n in (_str(z.get("Zone Name")) for z in rows
                         if z.get("Zone Status", "available") in ("available", None)) if n]
        if err:
            d["zones_error"] = err
    d["zones"] = zones
    flavors, err = _rows(dst, "flavor", "list", "--all", "-f", "json")
    d["flavors"] = [{"name": _str(f.get("Name")), "vcpus": _int(f.get("VCPUs")), "ram": _int(f.get("RAM")),
                     "disk": _int(f.get("Disk"))} for f in flavors]
    if err:
        d["flavors_error"] = err
    types, err = _rows(dst, "volume", "type", "list", "-f", "json")
    d["volume_types"] = [_str(t.get("Name")) for t in types if _str(t.get("Name"))]
    if err:
        d["volume_types_error"] = err
    return {"source": s, "destination": d, "conf": conf_view(cfg), "tool_version": __version__}


def run_inventory(args):
    want = parse(args)
    cfg = config.load()
    if want["json"]:
        with stdout_to_stderr() as real:
            emit_json(collect(want, cfg), stream=real, cfg=cfg)
        return 0
    inv = collect(want, cfg)
    for label in ("source", "destination"):
        x = inv[label]
        out("%s (%s, project %s):" % (label, x["auth_type"], x["scope_project"]))
        for h in x["hosts"]:
            out("  host     %-24s zone %-12s %s/%s%s" % (h["host"], h["zone"], h["state"], h["status"],
                                                       (" %s VMs" % h["vms"]) if h.get("vms") is not None else ""))
        out("  projects " + (" ".join(p["name"] for p in x["projects"]) or "-"))
    d = inv["destination"]
    out("  zones    compute %s; volume %s" % (" ".join(d["zones"]["compute"]) or "-", " ".join(d["zones"]["volume"]) or "-"))
    out("  flavors  " + (" ".join(f["name"] for f in d["flavors"]) or "-"))
    out("  types    " + (" ".join(d["volume_types"]) or "-"))
    return 0
