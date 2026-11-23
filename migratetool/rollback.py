"""Rollback of a VM state directory (bash cmd_rollback + drop_volume): delete the destination side, give the source
back. Re-runnable: every step skips what is already gone. Works on state written by the bash build as well (same
files). DESIGN.md, "rollback":

- A2: only what this run owns is deleted: the IDs it recorded, and a server it never recorded only when that server
  carries the run's owner mark (properties mig.source_id / mig.state, contract.OWNER_*). A same-name server without
  the mark is left alone; for state written before the marks existed (no 'owner_mark' in options.json) the rollback
  stops and names the server instead of guessing.
- A4 / A15: a destination server or volume counts as deleted only once the API answers "not found" (polled up to
  GONE_WAIT seconds); the source is touched only after the destination server is confirmed gone, and the state is
  retired only after every destination volume is.
- A3: a source port is "gone" only when Neutron says so; any other error is retried, then stops the rollback. A port
  attached elsewhere than the source VM stops it too.
- A14: the source gets back the power state it had when migrate exported it (SHUTOFF stays SHUTOFF).
- A5: summary() is what 'migrate rollback' shows before it asks 'Type <name>' (and --dry-run prints).
- A6: clean_before_cutover() is the destination half alone, for a run that failed before it stopped the source.
- A20 (1.1.0): nothing is deleted at the destination before the source object (the VM, or the volume of a
  migrate-volume state) has answered 'show'. Gone: the destination is the only copy, the rollback refuses. Not
  readable (auth, network, scope): it refuses too, and says so.
"""
import json
import os
import re
import time
from datetime import datetime

from . import config, contract, proc
from .copyvol import brick
from .jsonout import read_json
from .server import PORT_GONE_RE
from .util import Die, jqs, lines, log, re_escape, words

# "No Server found for <id>" is what openstackclient 10.x (openstacksdk) prints for a server, volume, image or port that
# is gone; older clients said "No server with a name or ID of '<id>' exists." (found on the lab gate, 2026-11-22).
GONE_RE = re.compile(r"no \w+ with a name or id|no \w+ found for|not found|could not be found|\(HTTP 404\)", re.I)
GONE_WAIT = 600       # seconds a deleted server or volume may take to disappear
PORT_RETRIES = 60     # 'port show' answers that are neither the port nor "not found"
ACTIVE_WARN = 3600    # A5: a destination VM older than this gets the red warning


def _last_line(text):
    return (text.strip().splitlines() or [""])[-1]


def wait_gone(r, cloud, kind, oid, label, limit=None):
    """A4/A15: '<kind> show <id> -f json -c id -c status' until it says not found. A volume in error_deleting stops
    at once; anything still there after limit seconds stops too: the cleanup is not complete."""
    limit = GONE_WAIT if limit is None else limit
    start, why = time.monotonic(), ""
    while True:
        res = cloud.run(kind, "show", oid, "-f", "json", "-c", "id", "-c", "status", stderr=proc.CAPTURE)
        if res.rc != 0:
            if GONE_RE.search(res.stderr):
                return
            why = _last_line(res.stderr) or "exit %d" % res.rc
        else:
            try:
                status = json.loads(res.stdout or "{}").get("status")
            except (ValueError, AttributeError):
                status = None
            why = "status %s" % jqs(status)
            if kind == "volume" and isinstance(status, str) and status.startswith("error"):
                raise Die("%s %s is %s after its delete: cleanup not complete, fix it at the destination and run the "
                          "rollback again" % (label, oid, status))
        if time.monotonic() - start > limit:
            raise Die("%s %s still there %ds after its delete (%s): cleanup not complete, run the rollback again"
                      % (label, oid, limit, why))
        time.sleep(3)


def drop_volume(r, cloud, vid, label):
    """Deletes it once settled and waits until it is gone; gone already is fine, in-use is refused."""
    # 7.2 (C4): captured, no shared volume-show.err file under STATE_ROOT.
    res = cloud.run("volume", "show", vid, "-f", "value", "-c", "status", stderr=proc.CAPTURE)
    if res.rc != 0:
        text = res.stderr
        if not GONE_RE.search(text):
            raise Die("cannot read %s %s: %s" % (label, vid, (text.splitlines() or [""])[-1]))
        log("%s %s already gone" % (label, vid))
        return
    s = res.stdout.rstrip("\n")
    if s == "in-use":
        raise Die("%s %s is in-use: detach it first, then re-run the rollback" % (label, vid))
    if not s.startswith("error"):
        r.wait_volume(cloud, vid, "available", 300)
    cloud.out("volume", "delete", vid)
    wait_gone(r, cloud, "volume", vid, label)
    log("deleted %s %s" % (label, vid))


def delete_server(r, sid, label="destination server"):
    """A4: 'server delete --wait', its exit code checked, then polled until Nova says not found."""
    res = r.dst.run("server", "delete", sid, "--wait", stderr=proc.CAPTURE)
    if res.rc != 0 and not GONE_RE.search(res.stderr):
        raise Die("%s %s: delete failed (%s): the source VM was not touched; run the rollback again"
                  % (label, sid, _last_line(res.stderr) or "exit %d" % res.rc))
    wait_gone(r, r.dst, "server", sid, label)


def reassociate_floating_ips(r, st):
    """7.2 (A3): a floating IP of a source port that lost it (Nova deleted the port it had created itself,
    and Neutron dropped the association with it) goes onto the port now holding that slot: source-ports
    line i (the reserve port, or the port itself) for ports.json entry i, else port-ids.json entry i.
    One that is still associated is left alone; a failure is a WARN, never the end of the rollback."""
    ports = st.json("ports.json")
    if not any(p.get("floating_ips") for p in ports):
        return
    held = words(st.read("source-ports")) if st.exists("source-ports") else []
    ids = [p["ID"] for p in st.json("port-ids.json")]
    for i, p in enumerate(ports):
        for addr in p.get("floating_ips") or []:
            pid = held[i] if i < len(held) else (ids[i] if i < len(ids) else "")
            res = r.src.run("floating", "ip", "show", addr, "-f", "value", "-c", "port_id", stderr=proc.CAPTURE)
            if res.rc != 0:
                log("WARN floating IP %s not re-associated: %s" % (addr, _last_line(res.stderr) or "floating ip show failed"))
                continue
            if res.stdout.strip() not in ("", "None"):
                continue
            res = r.src.run("floating", "ip", "set", "--port", pid, addr, stderr=proc.CAPTURE)
            if res.rc != 0:
                log("WARN floating IP %s not re-associated: %s" % (addr, _last_line(res.stderr) or "floating ip set failed"))
                continue
            log("re-associated floating IP %s with port %s" % (addr, pid))


def _epoch(stamp):
    """Nova's 'created' (2026-11-19T10:00:00Z) as an epoch; None when it does not parse."""
    try:
        return int(datetime.fromisoformat(str(stamp).strip().replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def properties_of(server):
    """A server's properties from 'server show -f json': a dict, or openstackclient's "k='v', k2='v2'" text."""
    props = server.get("properties")
    if props is None:
        props = server.get("metadata")
    if isinstance(props, dict):
        return {str(k): jqs(v) for k, v in props.items()}
    if isinstance(props, str):
        return dict(re.findall(r"([^\s,=]+)='([^']*)'", props))
    return {}


def owner_marks_used(st):
    """The run that wrote st marked what it created (options.json 'owner_mark', 1.0.0 and later)."""
    try:
        doc = read_json(st.p("options.json"))
    except (OSError, ValueError):
        return False
    return isinstance(doc, dict) and bool(doc.get("owner_mark"))


def owned_by(props, source_id, state_arg):
    return props.get(contract.OWNER_SOURCE) == source_id and props.get(contract.OWNER_STATE) == state_arg


def delete_unrecorded_server(r, st):
    """Nova created the destination server but 'server create' never returned its ID (a client timeout):
    dst-server-id is empty. A2: a server of that name created after this run stopped the source (t-stop) is deleted
    only when it carries this run's owner mark. Unmarked ones: left alone when this run marks what it creates (it is
    not ours); for state that predates the marks the rollback stops and names them (ownership cannot be proven)."""
    try:
        t_stop = int(st.read("t-stop").strip())
    except ValueError:
        return  # the run never reached the cutover: no server can have been created
    server = st.json("server.json")
    name, source_id = jqs(server.get("name")), jqs(server.get("id"))
    res = r.dst.run("server", "list", "--name", "^%s$" % re_escape(name), "-f", "json", stderr=proc.DEVNULL)
    try:
        rows = json.loads(res.stdout or "[]") if res.rc == 0 else []
    except ValueError:
        rows = []
    if not isinstance(rows, list):
        return
    mine, foreign = [], []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sid = row.get("ID") or row.get("id")
        if not sid:
            continue
        res = r.dst.run("server", "show", sid, "-f", "json", stderr=proc.DEVNULL)
        try:
            sj = json.loads(res.stdout) if res.rc == 0 else None
        except ValueError:
            sj = None
        if not isinstance(sj, dict):
            continue
        created = next((row[k] for k in ("Created", "Created At", "created", "created_at") if row.get(k)), None)
        when = _epoch(created if created is not None else sj.get("created"))
        if when is None or when < t_stop:
            continue  # older than this run's cutover: not created by it
        (mine if owned_by(properties_of(sj), source_id, st.arg) else foreign).append(sid)
    for sid in mine:
        delete_server(r, sid)
        log("deleted destination server %s found by name (its ID was never recorded)" % sid)
    if not foreign:
        return
    if owner_marks_used(st):
        log("INFO destination server %s named %s carries no owner mark of this run: not created by it, left alone"
            % (" ".join(foreign), name))
        return
    raise Die("destination server %s named %s was created after this run stopped the source, but this state (written "
              "before 1.0.0) has no owner mark to prove it is the copy: inspect it; if it is the copy delete it "
              "(openstack server delete %s), if it is not rename it (openstack server set --name <other> <id>); then run "
              "the rollback again. Nothing was deleted." % (" ".join(foreign), name, " ".join(foreign)))


def _sweep_legacy_port(r, st, pname):
    """The mig-<arg>-<i> ports of state written before dst-ports existed: deleted only when the port carries this
    state's owner description, or for state that predates the marks (the name was the only record then)."""
    if not r.dst.ok("port", "show", pname):
        return
    if owner_marks_used(st):
        res = r.dst.run("port", "show", pname, "-f", "json", stderr=proc.DEVNULL)
        try:
            desc = json.loads(res.stdout).get("description") if res.rc == 0 else None
        except (ValueError, AttributeError):
            desc = None
        if desc != contract.PORT_OWNER_PREFIX + st.arg:
            log("INFO destination port %s is not marked as this run's (description %r): left alone" % (pname, desc))
            return
    r.dst.out("port", "delete", pname)


def drop_recorded_volumes(r, st):
    for _svid, dvid in st.volume_map():
        # Recorded before it became available, so it may be in error or already gone: drop_volume handles both.
        # Still in-use right after the server delete: wait for the detach, as before.
        if r.dst.try_value("volume", "show", dvid, "-f", "value", "-c", "status") == "in-use":
            r.wait_volume(r.dst, dvid, "available", 300)
        drop_volume(r, r.dst, dvid, "destination volume")


def drop_leftover_clones(r, st):
    """A run that died mid-copy leaves its temporary clone behind. Only this VM's own clones match."""
    for a in st.json("attachments.json"):
        vid = a.get("Volume ID")
        res = r.src.run("volume", "list", "--name", "mig-clone-%s" % vid, "-f", "value", "-c", "ID")
        if res.rc != 0:
            log("WARN cannot list source clones named mig-clone-%s: skipped" % vid)
            continue
        for cid in lines(res.stdout):
            if os.path.exists(os.path.join(config.ATTACH_DIR, cid + ".json")):
                brick(r, "detach", cid, proc.DEVNULL)  # || true
            drop_volume(r, r.src, cid, "leftover clone")


def drop_images(r, st, vm):
    """Ephemeral path leftovers: uploaded destination images (by name too, for a run that died before recording
    the id), a source snapshot from a run that died mid-download, the local file."""
    imgs = []
    if st.nonempty("dst-image-id"):
        imgs += words(st.read("dst-image-id"))
    # A failing listing inside 'for img in $(...)' did not stop the bash build either.
    imgs += words(r.dst.run("image", "list", "--name", "mig-snap-" + vm, "-f", "value", "-c", "ID").stdout)
    for img in imgs:
        if r.dst.run("image", "delete", img, stderr=proc.DEVNULL).rc == 0:
            log("deleted destination image " + img)
    # 7.2 (B5): a listing that fails is a WARN, as at the destination above: stopping here would leave the source
    # VM locked, without its ports and stopped until the rollback is run again.
    res = r.src.run("image", "list", "--name", "mig-snap-" + vm, "-f", "value", "-c", "ID")
    if res.rc != 0:
        log("WARN cannot list source images named mig-snap-%s: skipped" % vm)
    simg = words(res.stdout) if res.rc == 0 else []
    if simg:
        r.src.out("image", "delete", *simg)
    st.remove("root.img")


def _port_device(r, pid):
    """A3: (gone, device_id) of a source port: gone only when Neutron says the port does not exist; another error is
    asked again (PORT_RETRIES times, 2 s apart), then stops the rollback."""
    last = ""
    for _ in range(PORT_RETRIES):
        res = r.src.run("port", "show", pid, "-f", "value", "-c", "device_id", stderr=proc.CAPTURE)
        if res.rc == 0:
            return False, res.stdout.rstrip("\n")
        if PORT_GONE_RE.search(res.stderr):
            return True, ""
        last = _last_line(res.stderr) or "exit %d" % res.rc
        time.sleep(2)
    raise Die("cannot read source port %s (%s): its NIC was not given back; the destination is already deleted, run "
              "the rollback again" % (pid, last))


def source_status(r, kind, oid):
    """A20: (status, why) of the source object, read-only ('<kind> show <id> -f json -c id -c status'): its Nova or
    Cinder status when it answers, "GONE" when the API says not found, None (why: the last stderr line) otherwise."""
    res = r.src.run(kind, "show", oid, "-f", "json", "-c", "id", "-c", "status", stderr=proc.CAPTURE)
    if res.rc == 0:
        try:
            status = json.loads(res.stdout or "{}").get("status")
        except (ValueError, AttributeError):
            status = None
        return (status if isinstance(status, str) and status else "UNKNOWN"), ""
    if GONE_RE.search(res.stderr):
        return "GONE", ""
    return None, _last_line(res.stderr) or "exit %d" % res.rc


def require_source(r, kind, oid, label, st):
    """A20: the source object still exists, else Die before anything is deleted at the destination; its status.
    kind: server|volume (the openstack noun); label: "VM <name>" or "volume <name>". Gone: the destination objects
    this state recorded are the only copy. 'migrate prune' never removes a live state directory (only
    <ident>.rolled-back.<epoch> ones), so retiring the state is named as a step by hand, not as a command."""
    noun, _sep, name = label.partition(" ")
    status, why = source_status(r, kind, oid)
    if status == "GONE":
        held = []
        if kind == "server":
            held.append("server %s" % (st.read("dst-server-id").strip() or "none recorded"))
        held.append("volumes %s" % (" ".join(dv for _s, dv in st.volume_map()) or "none recorded"))
        raise Die("source %s %s (%s) no longer exists at the source: the destination (%s) is the only copy, nothing "
                  "was deleted. Keep the destination: the state directory %s stays live ('migrate prune' removes only "
                  "rolled-back state directories, never a live one); once you are sure the destination is the copy to "
                  "keep, remove that directory by hand. A rollback cannot give back a %s that is gone."
                  % (noun, oid, name or "no name", ", ".join(held), st.path, noun))
    if status is None:
        raise Die("cannot read source %s %s (%s): nothing was deleted; check the source credentials/SRC_PROJECT and "
                  "run the rollback again" % (noun, oid, why))
    return status


def rollback_vm(r, st):
    if not st.exists("server.json"):
        raise Die("no state for %s in %s" % (st.arg, st.path))
    server = st.json("server.json")
    sid = server.get("id")
    vm = st.arg
    # A20: the source VM answers before anything at the destination is deleted.
    require_source(r, "server", sid, "VM " + jqs(server.get("name")), st)

    if st.nonempty("dst-server-id"):
        log("delete destination server")
        delete_server(r, st.read("dst-server-id").rstrip("\n"))
    elif st.exists("volume-map"):
        delete_unrecorded_server(r, st)
    # Other migrated VMs may be members; an empty group is harmless and a later migrate reuses it.
    if st.nonempty("dst-server-group"):
        log("INFO destination server group kept (%s): rollback never deletes a server group"
            % st.read("dst-server-group").rstrip("\n"))
    # The '<AZ>-<name>' security group copies are shared the same way.
    if st.nonempty("dst-security-groups"):
        kept = [l.split()[1] for l in st.read("dst-security-groups").splitlines() if len(l.split()) >= 2]
        log("INFO destination security groups kept (%s): rollback never deletes a security group" % ", ".join(kept))
    # Destination ports go by the IDs create_destination recorded, whatever their name. The mig-<arg>-<i> name sweep
    # below also covers state written before dst-ports existed.
    if st.nonempty("dst-ports"):
        for pid in st.read("dst-ports").split("\n"):
            if not pid:
                continue
            if r.dst.ok("port", "show", pid):
                r.dst.out("port", "delete", pid)
                log("deleted destination port " + pid)
    for i in range(len(st.json("ports.json"))):
        _sweep_legacy_port(r, st, "mig-%s-%d" % (vm, i))
    if st.exists("volume-map"):
        drop_recorded_volumes(r, st)
    drop_images(r, st, vm)
    drop_leftover_clones(r, st)
    r.src.run("server", "unlock", sid)  # || true
    # Reattach the ports this run detached (--ip same): source-ports (the ports and reserve ports recorded as they
    # were detached) plus every port of port-ids.json, so that a port detached right before a failure, and not
    # recorded yet, comes back too. Still attached to the source VM (a run that failed before the detach step, --ip
    # new): skipped. Attached to anything else: A3 stops.
    pids = words(st.read("source-ports")) if st.exists("source-ports") else []
    for p in st.json("port-ids.json"):
        if p["ID"] not in pids:
            pids.append(p["ID"])
    for pid in pids:
        gone, dev = _port_device(r, pid)
        if gone:
            log("port %s no longer exists (Nova deleted it; its reserve port is reattached instead)" % pid)
            continue
        if dev == sid:
            continue
        if dev:
            raise Die("source port %s is attached to %s, not to the source VM %s: give it back by hand (openstack "
                      "server remove port %s %s; openstack server add port %s %s), then run the rollback again"
                      % (pid, dev, sid, dev, pid, sid, pid))
        r.src.out("server", "add", "port", sid, pid)
        for _ in range(60):
            if r.src.try_value_inherit("port", "show", pid, "-f", "value", "-c", "device_id") == sid:
                break
            time.sleep(2)
        if r.src.try_value_inherit("port", "show", pid, "-f", "value", "-c", "device_id") != sid:
            raise Die("port %s did not reattach" % pid)
        log("reattached source port " + pid)
    reassociate_floating_ips(r, st)
    # A14: the power state the source had when migrate exported it.
    if jqs(server.get("status")) == "SHUTOFF":
        if r.src.try_value_inherit("server", "show", sid, "-f", "value", "-c", "status") != "SHUTOFF":
            raise Die("source VM %s is not SHUTOFF (it was when migrate exported it): check it" % sid)
        st.retire()
        log("source VM kept SHUTOFF, as it was before the migration")
        return
    if r.src.try_value_inherit("server", "show", sid, "-f", "value", "-c", "status") == "SHUTOFF":
        r.src.out("server", "start", sid)
    r.wait_server(r.src, sid, "ACTIVE")
    if r.src.try_value_inherit("server", "show", sid, "-f", "value", "-c", "status") != "ACTIVE":
        raise Die("source VM did not reach ACTIVE")
    st.retire()
    log("source VM ACTIVE again")


def clean_before_cutover(r, st):
    """A6: a re-run of 'migrate migrate' finds the state of a run that failed before it stopped the source (volume-map,
    no t-stop). The destination objects it recorded (volumes, clones left on this host) are deleted, the source is not
    touched (it never stopped), the directory is retired like a rollback's. Security group copies stay (shared)."""
    log("an earlier run of %s failed before the cutover (state %s, no t-stop): deleting the destination objects it "
        "recorded; the source VM was never stopped" % (st.arg, st.path))
    if st.exists("volume-map"):
        drop_recorded_volumes(r, st)
    drop_leftover_clones(r, st)
    st.unlock()
    st.retire()
    log("earlier state retired as %s; starting again" % os.path.basename(st.retired))


def _age(seconds):
    if seconds is None:
        return "?"
    h, rest = divmod(int(seconds), 3600)
    return "%dh%02dm" % (h, rest // 60) if h else "%dm%02ds" % divmod(rest, 60)


def summary(r, st, kind):
    """A5: what a rollback of st would delete, read-only. kind vm|vol. dst_server_status: 'server show' at the
    destination (None when there is no server ID, 'GONE' when Nova no longer has it)."""
    now = int(time.time())
    try:
        t_stop = int(st.read("t-stop").strip())
    except ValueError:
        t_stop = None
    d = {"ident": st.arg, "kind": kind, "dir": st.path, "t_stop": None, "age_seconds": None,
         "dst_server_id": None, "dst_server_status": None, "source_power": None,
         "delete": {"server": None, "ports": [], "volumes": [dv for _s, dv in st.volume_map()], "images": []}}
    if t_stop is not None:
        d["t_stop"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t_stop))
        d["age_seconds"] = max(0, now - t_stop)
    if kind == "vol":
        vj = st.json("volume.json") if st.exists("volume.json") else {}
        d["name"], d["source_id"] = jqs(vj.get("name")) if vj.get("name") else "", jqs(vj.get("id"))
        return d
    server = st.json("server.json")
    d["name"], d["source_id"] = jqs(server.get("name")), jqs(server.get("id"))
    d["source_power"] = server.get("status") if isinstance(server.get("status"), str) else None
    dsid = st.read("dst-server-id").strip()
    if dsid:
        d["dst_server_id"] = d["delete"]["server"] = dsid
        res = r.dst.run("server", "show", dsid, "-f", "json", "-c", "id", "-c", "status", stderr=proc.CAPTURE)
        if res.rc == 0:
            try:
                d["dst_server_status"] = json.loads(res.stdout).get("status")
            except (ValueError, AttributeError):
                d["dst_server_status"] = None
        elif GONE_RE.search(res.stderr):
            d["dst_server_status"] = "GONE"
    d["delete"]["ports"] = [p for p in st.read("dst-ports").split("\n") if p]
    d["delete"]["images"] = words(st.read("dst-image-id"))
    return d


def summary_lines(d):
    """The text of summary() shown before the prompt (one string per line)."""
    out = ["Rollback of %s (%s %s %s):" % (d["ident"], "source VM" if d["kind"] == "vm" else "source volume",
                                            d.get("name") or "(no name)", d.get("source_id"))]
    if d["kind"] == "vol":
        out.append("  destination volume(s) deleted: %s; the source volume was never modified"
                   % (" ".join(d["delete"]["volumes"]) or "none recorded"))
        return out
    if d["t_stop"]:
        out.append("  the source was stopped at %s (%s ago)" % (d["t_stop"], _age(d["age_seconds"])))
    else:
        out.append("  the run never stopped the source (no t-stop)")
    if d["dst_server_id"]:
        out.append("  destination server %s (%s) is deleted" % (d["dst_server_id"], d["dst_server_status"] or "?"))
    if d["delete"]["ports"]:
        out.append("  destination ports deleted: %s" % " ".join(d["delete"]["ports"]))
    if d["delete"]["volumes"]:
        out.append("  destination volumes deleted: %s" % " ".join(d["delete"]["volumes"]))
    if d["delete"]["images"]:
        out.append("  destination images deleted: %s" % " ".join(d["delete"]["images"]))
    if d["dst_server_status"] == "ACTIVE" and (d["age_seconds"] or 0) > ACTIVE_WARN:
        out.append("  WARN the destination VM has run for %s: whatever was written on it since %s is lost"
                   % (_age(d["age_seconds"]), d["t_stop"]))
    after = "kept SHUTOFF" if d["source_power"] == "SHUTOFF" else "unlocked, its ports given back, started"
    out.append("  then the source VM is %s" % after)
    return out
