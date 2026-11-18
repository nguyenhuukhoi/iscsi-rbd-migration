"""Reading the source VM into the state directory (bash export_definition, detect_boot)."""
import os

from .util import Die, jq_dump, jqs, lines


def export_definition(r, vm, st):
    st.create()
    # Nova allows duplicate names in a project; "server show <name>" then fails with an
    # unhelpful OSC error. Zero matches is fine: the argument may itself be a server ID.
    ids = lines(r.src.out("server", "list", "--name", "^%s$" % vm, "-f", "value", "-c", "ID"))
    if len(ids) > 1:
        raise Die("%d servers are named '%s' in the source project: %s — re-run with the server ID "
                  "(migrate/plan/rollback all take it)" % (len(ids), vm, " ".join(ids)))
    sid = r.src.value("server", "show", vm, "-f", "value", "-c", "id")
    st.write("server.json", r.src.out("server", "show", sid, "-f", "json"))
    st.write("attachments.json", r.src.out("server", "volume", "list", sid, "-f", "json"))
    st.write("port-ids.json", r.src.out("port", "list", "--server", sid, "-f", "json", "-c", "ID"))

    ports = []
    for p in st.json("port-ids.json"):
        pj = r.src.json("port", "show", p["ID"], "-f", "json")
        fixed = pj.get("fixed_ips") or []
        first = fixed[0] if fixed else {}
        net = r.src.value("network", "show", jqs(pj.get("network_id")), "-f", "value", "-c", "name")
        cidr = r.src.value("subnet", "show", jqs(first.get("subnet_id")), "-f", "value", "-c", "cidr")
        sgs = [r.src.value("security", "group", "show", sg, "-f", "value", "-c", "name")
               for sg in (pj.get("security_group_ids") or [])]
        entry = {
            "network": net, "cidr": cidr, "security_groups": sgs, "mac": pj.get("mac_address"),
            "ip": first.get("ip_address"), "extra_ips": len(fixed) - 1,
            "port_security": pj.get("port_security_enabled"), "aap": pj.get("allowed_address_pairs"),
        }
        # P6 copies these too. Recorded only when set, so that a plain port's entry stays what the bash
        # build writes (either build reads the other's state; the bash build ignores these keys).
        for key in ("description", "dns_name"):
            if isinstance(pj.get(key), str) and pj.get(key):
                entry[key] = pj[key]
        ports.append(entry)
    st.write("ports.json", jq_dump(ports))

    vols = [r.src.json("volume", "show", a["Volume ID"], "-f", "json") for a in st.json("attachments.json")]
    st.write("volumes.json", jq_dump(vols))


def detect_boot(st):
    """True when the root disk is not a volume (ephemeral, image-backed)."""
    return not any(a.get("Device") in ("/dev/vda", "/dev/sda") for a in st.json("attachments.json"))


def dest_volume_name(svid, st):
    """<instance-name>-<device>, e.g. app01-vdb (the volume ID when it has no device)."""
    dev = next((a.get("Device") for a in st.json("attachments.json") if a.get("Volume ID") == svid), None)
    if dev is None or dev == "":
        dev = svid
    return "%s-%s" % (jqs(st.json("server.json").get("name")), os.path.basename(dev) if "/" in dev else dev)
