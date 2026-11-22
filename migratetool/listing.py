"""'migrate list' (7.3, DESIGN.md section 7): the selection of plan/migrate, and nothing else.

    migrate list [<vm>] [--vm V,..] [--host H,..] [--project P,..] [--vm-file F]
                 [--volumes [--available|--attached|--all]] [--json]

The selector's own listing (selector.Runner.enumerate_vms / enumerate_volumes, unchanged: same SKIP rules, same
ambiguity rule) plus what a table of instances needs. No export, no preflight, no log directory, no state written.

openstack commands, all through a read-only Cloud (SEC-9):
  source  server list --all-projects --long [--host H | --project P | --name ^V$] -f json   (one per selector value,
          as plan; without admin the same without --all-projects)
  source  project show <id> -f value -c name          once per project met (cached)
  source  volume list --all-projects --long -f json   once, for every row's volumes (without admin: without
          --all-projects)
  source  server show <id> -f json                    only for a VM whose listing lacks Host, Availability Zone,
          Flavor, Image Name or Networks
  source  server show <id> -f json                    only for a --vm <id> the name listing does not return (as plan)
  source  project show <p> -f value -c id             --host with --project (as plan)
  source  project show <p> -f json + volume list --project <id> -f json   --volumes --available|--all (as plan)
For a --host of 24 VMs in one project: 3 commands.
"""
import json
import os
import stat

from . import config, proc
from .jsonout import CONTROL, emit_json, read_file, stdout_to_stderr
from .selector import SCOPES, SELECTORS, Runner, Selection, _add_list
from .stateview import state_of
from .util import Die, log
from .vmdef import image_says_bfv

VM_FILE_MAX_BYTES = 1 << 20
VM_FILE_MAX_LINES = 10000
VALUE_MAX = 255
NEEDED = ("Host", "Availability Zone", "Image Name", "Networks")


def check_value(opt, v):
    """SEC-6: a selector value is a name or an ID: no control character, no '/', not '.'/'..', bounded."""
    if not v or CONTROL.search(v) or "/" in v or v in (".", "..") or len(v) > VALUE_MAX:
        raise Die("%s %r: not a name or ID (no '/', no control character, at most %d characters)" % (opt, v, VALUE_MAX))
    return v


def read_vm_file(path):
    """SEC-6: --vm-file is a regular file (not a symlink), at most 1 MB and 10 000 lines."""
    try:
        st = os.lstat(path)
    except OSError as e:
        raise Die("cannot read --vm-file %s: %s" % (path, e.strerror))
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise Die("--vm-file %s must be a regular file (not a symlink)" % path)
    if st.st_size > VM_FILE_MAX_BYTES:
        raise Die("--vm-file %s is larger than %d bytes" % (path, VM_FILE_MAX_BYTES))
    try:
        text = read_file(path, VM_FILE_MAX_BYTES)
    except (OSError, ValueError) as e:
        raise Die("cannot read --vm-file %s: %s" % (path, e))
    lines = text.split("\n")
    if len(lines) > VM_FILE_MAX_LINES:
        raise Die("--vm-file %s has more than %d lines" % (path, VM_FILE_MAX_LINES))
    names = []
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if line:
            names.append(check_value("--vm-file", line))
    return names


def parse(args):
    """(selection, volumes, json)."""
    s = Selection("list")
    volumes = False
    i = 0
    while i < len(args):
        a = args[i]
        if a in SELECTORS:
            if i + 1 >= len(args) or not args[i + 1]:
                raise Die("%s needs a value" % a)
            v = args[i + 1]
            i += 2
            if a == "--vm-file":
                s.vm_file = v
                continue
            vals = []
            _add_list(vals, v)
            for x in vals:
                check_value(a, x)
            {"--vm": s.vms, "--host": s.hosts, "--project": s.projects}[a].extend(vals)
        elif a == "--volumes":
            volumes, i = True, i + 1
        elif a in SCOPES:
            if s.scope and s.scope != a[2:]:
                raise Die("choose one of --available, --attached, --all")
            s.scope, i = a[2:], i + 1
        elif a == "--json":
            s.json, i = True, i + 1
        elif a.startswith("-") and a != "-":
            raise Die("unknown option %s" % a)
        else:
            s.vms.append(check_value("<vm>", a))
            i += 1
    if s.vm_file:
        s.vms += read_vm_file(s.vm_file)
    seen = set()
    s.vms = [v for v in s.vms if not (v in seen or seen.add(v))]
    if not (s.hosts or s.projects or s.vms):
        raise Die("select something: <vm>, --vm, --host, --project or --vm-file")
    if s.scope and not volumes:
        raise Die("--%s lists volumes: add --volumes" % s.scope)
    if volumes:
        if not s.scope:
            s.scope = "all" if s.projects and not s.vms else "attached"
        if s.scope in ("available", "all") and not s.projects:
            raise Die("--volumes --%s needs --project" % s.scope)
        if s.scope in ("available", "all") and s.hosts:
            raise Die("--host filters VMs: use it with --volumes --attached only")
    return s, volumes


# --- SEC-8: a field of an openstack listing that is not what it should be reads as empty ----------------------
def _str(v):
    if isinstance(v, str):
        return v
    if isinstance(v, bool) or v is None:
        return ""
    if isinstance(v, (int, float)):
        return str(v)
    return ""


def _int(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.isdigit():
        return int(v)
    return None


def _flavor(v):
    if isinstance(v, dict):
        return _str(v.get("original_name")) or _str(v.get("name"))
    return _str(v).split(" ")[0]


def networks(v):
    """openstackclient's Networks column: {"net": ["ip", ...]} (current) or "net=ip, ip; net2=ip" (older).
    A list of {"network", "ips"}: network names are data, never JSON keys (SEC-1 checks every key)."""
    nets = []
    if isinstance(v, dict):
        for k, ips in v.items():
            ips = ips if isinstance(ips, list) else [ips]
            nets.append({"network": _str(k), "ips": [_str(x) for x in ips if _str(x)]})
    elif isinstance(v, str):
        for part in v.split(";"):
            if "=" in part:
                n, ips = part.split("=", 1)
                nets.append({"network": n.strip(), "ips": [x.strip() for x in ips.split(",") if x.strip()]})
    return nets


class Lister(Runner):
    """The selector's Runner, read-only, without a log directory; keeps each listing entry for the extra columns."""

    def __init__(self, sel, cfg):
        super().__init__(sel, cfg, readonly=True)
        self.log_dir = None
        self.entries = {}
        self._vols = None

    def listing(self, n, *args):
        got = super().listing(n, *args)
        for e in got if isinstance(got, list) else []:
            if isinstance(e, dict) and _str(e.get("ID")):
                self.entries.setdefault(_str(e.get("ID")), e)
        return got

    def volumes(self):
        """{volume ID: listing entry} from one 'volume list --all-projects --long' (own project without admin)."""
        if self._vols is None:
            res = self.src.run("volume", "list", "--all-projects", "--long", "-f", "json", stderr=proc.CAPTURE)
            if res.rc != 0:
                res = self.src.run("volume", "list", "--long", "-f", "json", stderr=proc.CAPTURE)
            try:
                got = json.loads(res.stdout or "[]") if res.rc == 0 else []
            except ValueError:
                got = []
            self._vols = {_str(v.get("ID")): v for v in (got if isinstance(got, list) else [])
                          if isinstance(v, dict) and _str(v.get("ID"))}
        return self._vols

    def attached(self, sid):
        """[(volume ID, device)] attached to server sid, from the volume listing's 'Attached to' column."""
        found = []
        for vid, v in self.volumes().items():
            att = v.get("Attached to")
            for a in att if isinstance(att, list) else []:
                if isinstance(a, dict) and _str(a.get("server_id")) == sid:
                    found.append((vid, _str(a.get("device"))))
        return sorted(found, key=lambda x: (x[1], x[0]))

    def vm_row(self, r):
        e = self.entries.get(r.id) or {}
        locked = e.get("Locked") if isinstance(e.get("Locked"), bool) else None
        host, az = _str(e.get("Host")), _str(e.get("Availability Zone"))
        flavor = _flavor(e["Flavor Name"] if "Flavor Name" in e else e.get("Flavor"))
        image, nets = e.get("Image Name"), networks(e.get("Networks"))
        if r.id != "-" and (any(k not in e for k in NEEDED) or not ("Flavor" in e or "Flavor Name" in e)):
            res = self.src.run("server", "show", r.id, "-f", "json", stderr=proc.DEVNULL)
            try:
                sj = json.loads(res.stdout) if res.rc == 0 else {}
            except ValueError:
                sj = {}
            sj = sj if isinstance(sj, dict) else {}
            host = host or _str(sj.get("OS-EXT-SRV-ATTR:host"))
            az = az or _str(sj.get("OS-EXT-AZ:availability_zone"))
            flavor = flavor or _flavor(sj.get("flavor"))
            if "Image Name" not in e:
                image = sj.get("image")
            nets = nets or networks(sj.get("addresses"))
            if isinstance(sj.get("locked"), bool):
                locked = sj["locked"]
        bfv = image_says_bfv(image if isinstance(image, (str, dict)) or image is None else "")
        if isinstance(image, dict):
            image = image.get("name") or image.get("id")
        vols = []
        for vid, dev in (self.attached(r.id) if r.id != "-" else []):
            v = self.volumes().get(vid, {})
            vols.append({"id": vid, "name": _str(v.get("Name")), "size": _int(v.get("Size")), "device": dev})
        # SEC-8: the listing's own fields, read defensively (the selector's Row keeps its 7.2 reading)
        name = _str(e["Name"]) if "Name" in e else r.name
        status = _str(e["Status"]) if "Status" in e else r.status
        return {"kind": "vm", "id": r.id, "name": name, "ident": r.ident, "status": status, "result": r.res,
                "project": {"src": r.sp, "dst": r.dp}, "project_id": r.pid, "host": host, "az": az, "flavor": flavor,
                "image": "" if bfv else _str(image), "boot": "bfv" if bfv else "ephemeral", "networks": nets,
                "volumes": vols, "locked": locked,
                "state": state_of([r.id, r.name]) if r.id != "-" else state_of([r.name])}

    def vol_row(self, vid, name, status, sp, dp, pid, res, attached_to=None):
        v = self.volumes().get(vid, {})
        return {"kind": "vol", "id": vid, "name": name, "status": status, "result": res,
                "size": _int(v.get("Size")), "type": _str(v.get("Type")), "az": _str(v.get("Availability Zone")),
                "bootable": _str(v.get("Bootable")), "attached_to": attached_to,
                "project": {"src": sp, "dst": dp}, "project_id": pid, "state": state_of(["vol-" + vid])}

    def collect(self, volumes):
        """(rows for JSON, rows for the text table)."""
        s = self.s
        self.load_pmap()
        if not volumes or s.scope != "available":
            self.enumerate_vms()
        vm_rows = list(self.rows)
        rows, table = [], []
        if not volumes:
            for r in vm_rows:
                rows.append(self.vm_row(r))
            return rows, vm_rows
        if s.scope in ("attached", "all"):
            from .selector import Row
            for r in vm_rows:
                if r.id == "-":
                    continue
                for vid, dev in self.attached(r.id):
                    v = self.volumes().get(vid, {})
                    name = _str(v.get("Name"))
                    rows.append(self.vol_row(vid, name, _str(v.get("Status")), r.sp, r.dp, r.pid, r.res,
                                             {"server_id": r.id, "server_name": r.name, "device": dev}))
                    table.append(Row("vol", vid, name, _str(v.get("Status")), r.sp, r.dp, r.res))
        if s.scope in ("available", "all"):
            self.rows = []
            self.enumerate_volumes()
            for r in self.rows:
                rows.append(self.vol_row(r.id, r.name, r.status, r.sp, r.dp, r.pid, r.res))
                table.append(r)
        return rows, table


def run_list(args):
    s, volumes = parse(args)
    cfg = config.load()
    if not s.json:
        lister = Lister(s, cfg)
        _rows, table = lister.collect(volumes)
        if not table:
            log("nothing selected")
            return 0
        lister.rows = table
        lister.show("Selected:")
        return 0
    from . import __version__
    with stdout_to_stderr() as real:
        lister = Lister(s, cfg)
        rows, _table = lister.collect(volumes)
        totals = {}
        for row in rows:
            key = "SKIP" if row["result"].startswith("SKIP") else row["result"]
            totals[key] = totals.get(key, 0) + 1
        emit_json({"tool_version": __version__, "kind": "vol" if volumes else "vm", "rows": rows, "totals": totals},
                  stream=real, cfg=cfg)
    return 0
