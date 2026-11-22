"""The destination AZ name that prefixes the copies of the source server group and security groups (P6):
'<AZ>-<source name>', e.g. dc10-epz-web-anti.

Which AZ, in this order:
  1. --az, else MIG_AZ (both end up in opts.dest_az; MIG_AZ is applied by Options.apply_az_defaults);
  2. neither: the destination's only compute availability zone. Read-only, so 'plan' and 'migrate' name
     the copies the same way before anything exists. Nova's own 'internal' zone and zones that are not
     available do not count. No zone, or several: no name can be trusted to match where the scheduler will
     put the VM, so the preflight fails and asks for --az (or MIG_AZ, or the opt-outs).

The AZ is used for the names only: without --az/MIG_AZ the volumes and the server are still created
without an availability zone, as before.
"""
import json

from . import proc
from .util import Die

INTERNAL = "internal"  # nova.conf internal_service_availability_zone (default)
OPT_OUTS = "--no-server-group, --no-copy-secgroup"


def zones_from_listing(rows):
    """Usable compute zone names, in listing order, from 'availability zone list --compute -f json'."""
    names = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        name = row.get("Zone Name")
        status = row.get("Zone Status", "available")
        if not name or name == INTERNAL or status not in ("available", None):
            continue
        if name not in names:
            names.append(name)
    return names


def pick(zones):
    """The one zone, or Die with what was found."""
    if len(zones) == 1:
        return zones[0]
    if not zones:
        found = "the destination lists no usable compute availability zone"
    else:
        found = "the destination has %d compute availability zones (%s)" % (len(zones), " ".join(zones))
    raise Die("cannot name the destination copies of the source server group and security groups: no --az, no "
              "MIG_AZ, and %s — pass --az <zone> or set MIG_AZ (opt out: %s)" % (found, OPT_OUTS))


def resolve(r):
    """(prefix, how): how is '' when it came from --az/MIG_AZ (the preflight header shows it), else the
    sentence for the INFO line. Raises Die when it cannot be told."""
    if r.opts.dest_az:
        return r.opts.dest_az, ""
    res = r.dst.run("availability", "zone", "list", "--compute", "-f", "json", stderr=proc.DEVNULL)
    if res.rc != 0:
        raise Die("cannot list the destination compute availability zones to name the copies of the source server "
                  "group and security groups — pass --az <zone> or set MIG_AZ (opt out: %s)" % OPT_OUTS)
    try:
        rows = json.loads(res.stdout or "[]")
    except ValueError:
        rows = []
    az = pick(zones_from_listing(rows))
    return az, "%s is the only compute availability zone of the destination (no --az, no MIG_AZ)" % az
