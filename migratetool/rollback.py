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
  migrate-volume state) has answered 'show'. Not found: the destination may be the only remaining copy, the rollback
  refuses. Not readable (auth, network, scope, an unexpected answer): it refuses too, and says so.
- A21 (1.1.1): a destination object (or a source port) counts as deleted only when the API says that this very object
  is not found (gone(): the answer names its ID). 'public endpoint for compute service ... not found' (keystoneauth,
  a catalog without that service or region), 'command not found' or the 404 of a wrong endpoint URL name no object and
  prove nothing. Nothing is deleted at the destination before the destination API has answered one 'show' validly in
  this run (require_destination): the recorded server, else the first recorded volume, there or not found.
"""
import json
import os
import re
import time
from datetime import datetime

from . import config, contract, proc
from .copyvol import brick
from .jsonout import read_json
from .util import Die, jqs, lines, log, re_escape, words

# A21 (1.1.1): how an API says that the object asked for is gone, on a line that names that object. "No Server found
# for <id>" is what openstackclient 10.x (openstacksdk) prints for a server, volume, image or port that is gone (lab
# gate, 2026-11-22); older clients said "No server with a name or ID of '<id>' exists."; novaclient/cinderclient say
# "Instance <id> could not be found. (HTTP 404)".
_GONE_FORMS = (r"no \w+ found for '?{o}'?(?![\w-])", r"no \w+ with a name or id of '{o}' exists",
               r"(?<![\w-]){o}'? could not be found")
_ENDS_404 = re.compile(r"(?:\(HTTP 404\)|(?<![\w.])404)[.\s]*$")
# R1 (1.2.0): a 404 anywhere on the line that names the object, not only at its end: "(HTTP 404)" wherever it is (a
# 'Not Found (HTTP 404) (Request-ID: req-...) ... /v3/<project>/volumes/<id>' answer), or a 404 token of its own
# (not part of an ID, an address or a URL path such as /v2/404abc or 172.20.40.4) followed by 'Not Found' or by the
# end of the line. _ENDS_404 (1.1.1) still counts: what it took as gone stays gone.
_HAS_404 = re.compile(r"\(HTTP 404\)|(?<![\w./-])404(?![\w.-])\s*(?:not found|$)", re.I)
GONE_WAIT = 600       # seconds a deleted server or volume may take to disappear
READ_ERRORS = 5       # A21: consecutive 'show' failures naming no object before wait_gone gives up (about 15 s)
PORT_RETRIES = 60     # 'port show' answers that are neither the port nor "not found"
ACTIVE_WARN = 3600    # A5: a destination VM older than this gets the red warning


def _last_line(text):
    return (text.strip().splitlines() or [""])[-1]


def gone(stderr, oid):
    """True only when the API says that object oid itself is not found (A21, 1.1.1): a line of stderr names oid (as
    it is, any case) and says 'No <Kind> found for <oid>', "No <kind> with a name or ID of '<oid>' exists", '<oid>
    could not be found', or ends in '(HTTP 404)' / '404'; R1 (1.2.0): or has '(HTTP 404)' anywhere, or a 404 token of
    its own followed by 'Not Found' or the end of the line (_HAS_404). Anything that names no object (keystoneauth's
    'public endpoint for compute service in RegionOne region not found', 'openstack: command not found', the 404 of a
    wrong endpoint URL) is not proof that the object was deleted: False. oid empty or None: False."""
    if not isinstance(oid, str) or not oid.strip():
        return False
    o = re.escape(oid)
    named = re.compile(r"(?<![\w-])%s(?![\w-])" % o, re.I)
    forms = [re.compile(f.format(o=o), re.I) for f in _GONE_FORMS]
    for line in (stderr or "").splitlines():
        if not named.search(line):
            continue
        if any(f.search(line) for f in forms) or _ENDS_404.search(line) or _HAS_404.search(line):
            return True
    return False


def wait_gone(r, cloud, kind, oid, label, limit=None):
    """A4/A15: '<kind> show <id> -f json -c id -c status' until it says that object is not found (gone()). A volume in
    error_deleting stops at once; anything still there after limit seconds stops too: the cleanup is not complete.
    A21: a 'show' that fails without naming the object is a read error, not a delete; READ_ERRORS of them in a row (or
    a read error as the last answer at limit) stop the rollback: 'cannot read', not a 600 s wait."""
    limit = GONE_WAIT if limit is None else limit
    start, why, unread = time.monotonic(), "", 0
    side = getattr(cloud, "side", "destination")
    while True:
        res = cloud.run(kind, "show", oid, "-f", "json", "-c", "id", "-c", "status", stderr=proc.CAPTURE)
        if res.rc != 0:
            if gone(res.stderr, oid):
                return
            why = _last_line(res.stderr) or "exit %d" % res.rc
            unread += 1
            if unread >= READ_ERRORS or time.monotonic() - start > limit:
                raise Die("cannot read %s %s after its delete (%s): cleanup not complete; check the %s "
                          "credentials/endpoint and run the rollback again" % (label, oid, why, side))
        else:
            unread = 0
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
        if not gone(text, vid):
            raise Die("cannot read %s %s: %s; nothing more was deleted"
                      % (label, vid, _last_line(text) or "exit %d" % res.rc))
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
    """A4: 'server delete --wait', its exit code checked, then polled until Nova says not found. A21: a failed delete
    counts as done only when the answer says that server is not found (gone())."""
    res = r.dst.run("server", "delete", sid, "--wait", stderr=proc.CAPTURE)
    if res.rc != 0 and not gone(res.stderr, sid):
        raise Die("%s %s: delete failed (%s): the source VM was not touched; check the destination "
                  "credentials/endpoint/DST_PROJECT and run the rollback again"
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
    # P2-a (1.1.1): every project (the server is the copy whatever project the token is scoped to); a token that may
    # not list other projects (403: no admin role, an application credential) lists its own, as selector.listing.
    pattern = "^%s$" % re_escape(name)
    res = r.dst.run("server", "list", "--name", pattern, "--all-projects", "-f", "json", stderr=proc.DEVNULL)
    if res.rc != 0:
        res = r.dst.run("server", "list", "--name", pattern, "-f", "json", stderr=proc.DEVNULL)
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
    """A3: (gone, device_id) of a source port: gone only when Neutron says that port does not exist (A21: gone(), the
    answer names it; an endpoint missing from the catalog is not a deleted port); another error is asked again
    (PORT_RETRIES times, 2 s apart), then stops the rollback."""
    last = ""
    for _ in range(PORT_RETRIES):
        res = r.src.run("port", "show", pid, "-f", "value", "-c", "device_id", stderr=proc.CAPTURE)
        if res.rc == 0:
            return False, res.stdout.rstrip("\n")
        if gone(res.stderr, pid):
            return True, ""
        last = _last_line(res.stderr) or "exit %d" % res.rc
        time.sleep(2)
    raise Die("cannot read source port %s (%s): its NIC was not given back; the destination is already deleted, run "
              "the rollback again" % (pid, last))


def recorded_source_id(doc):
    """A20: the source ID a state recorded (the "id" of server.json / volume.json), None when it has none: then
    nothing may be probed or deleted ('openstack server show None' is no answer)."""
    sid = doc.get("id") if isinstance(doc, dict) else None
    return sid if isinstance(sid, str) and sid.strip() else None


def no_source_id(st, fname):
    return Die("state %s has no source id in %s: nothing was deleted" % (st.path, fname))


def object_status(cloud, kind, oid):
    """A20/A21: (status, why) of one object, read-only ('<kind> show <id> -f json -c id -c status'): its Nova or
    Cinder status when it answers with that object ("UNKNOWN" when the answer has the right id but no status),
    "GONE" when the API says that object is not found (gone()), None otherwise (why: the last stderr line, or
    "unexpected answer from <kind> show" when exit 0 printed something else than an object with that id: never taken
    as proof that it exists)."""
    res = cloud.run(kind, "show", oid, "-f", "json", "-c", "id", "-c", "status", stderr=proc.CAPTURE)
    if res.rc == 0:
        try:
            doc = json.loads(res.stdout or "")
        except ValueError:
            doc = None
        if not isinstance(doc, dict) or doc.get("id") != oid:
            return None, "unexpected answer from %s show" % kind
        status = doc.get("status")
        return (status if isinstance(status, str) and status else "UNKNOWN"), ""
    if gone(res.stderr, oid):
        return "GONE", ""
    return None, _last_line(res.stderr) or "exit %d" % res.rc


def source_status(r, kind, oid):
    """A20: object_status of the source object."""
    return object_status(r.src, kind, oid)


def require_source(r, kind, oid, label, st, nothing="this rollback deleted nothing", again="run the rollback again"):
    """A20: the source object still exists, else Die before anything is deleted at the destination; its status.
    kind: server|volume (the openstack noun); label: "VM <name>" or "volume <name>". Not found (deleted,
    or not visible to the source credentials/SRC_PROJECT, or the wrong endpoint): the destination objects this state
    recorded may be the only remaining copy. 'migrate prune' never removes a live state directory (only
    <ident>.rolled-back.<epoch> ones), so retiring the state is named as a step by hand, not as a command. nothing /
    again: the words for what this run did not do and what to run next (A6 asks too, as a re-run of migrate)."""
    noun, _sep, name = label.partition(" ")
    status, why = source_status(r, kind, oid)
    if status == "GONE":
        held = []
        if kind == "server":
            held.append("server %s" % (st.read("dst-server-id").strip() or "none recorded"))
        held.append("volumes %s" % (" ".join(dv for _s, dv in st.volume_map()) or "none recorded"))
        raise Die("source %s %s (%s) is not found at the source (deleted, not visible to the source "
                  "credentials/SRC_PROJECT, or the wrong source endpoint/region): the destination (%s) may be the only "
                  "remaining copy; %s. Keep the destination. The state directory %s stays "
                  "live ('migrate prune' removes only rolled-back state directories, never a live one): check the "
                  "source scope and endpoint before removing it by hand, once you are sure the destination is the copy "
                  "to keep. A rollback cannot give back a %s that is gone."
                  % (noun, oid, name or "no name", ", ".join(held), nothing, st.path, noun))
    if status is None:
        raise Die("cannot read source %s %s (%s): %s; check the source credentials/SRC_PROJECT and %s"
                  % (noun, oid, why, nothing, again))
    return status


def destination_probe(st):
    """A21: (kind, id) of the destination object the rollback reads before its first delete: the recorded server,
    else the first destination volume of volume-map; None when the state recorded neither (nothing to delete at the
    destination by ID)."""
    dsid = st.read("dst-server-id").strip()
    if dsid:
        return "server", dsid
    vmap = st.volume_map()
    return ("volume", vmap[0][1]) if vmap else None


def require_destination(r, st):
    """A21: one valid answer of the destination API in this run before anything is deleted there: that object (any
    status) or "not found" naming it (a rollback run again after one that deleted it). Anything else (an endpoint
    missing from the catalog, a 404 naming nothing, auth, network, an unexpected answer) dies here, before the first
    delete and before the source is touched. Returns the status read ("GONE" when gone), None when the state recorded
    nothing to probe."""
    probe = destination_probe(st)
    if probe is None:
        return None
    kind, oid = probe
    status, why = object_status(r.dst, kind, oid)
    if status is None:
        raise Die("cannot read destination %s %s (%s): nothing was deleted at the destination, the source was not "
                  "touched; check the destination credentials/endpoint/DST_PROJECT and run the rollback again"
                  % (kind, oid, why))
    return status


def rollback_vm(r, st):
    if not st.exists("server.json"):
        raise Die("no state for %s in %s" % (st.arg, st.path))
    server = st.json("server.json")
    sid = recorded_source_id(server)
    if sid is None:
        raise no_source_id(st, "server.json")
    vm = st.arg
    # A20: the source VM answers before anything at the destination is deleted.
    require_source(r, "server", sid, "VM " + jqs(server.get("name")), st)
    # A21: and the destination answers too, before its first delete.
    require_destination(r, st)

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
        lost, dev = _port_device(r, pid)
        if lost:
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
    touched (it never stopped), the directory is retired like a rollback's. Security group copies stay (shared).
    1.1.1 (A20 for A6): the source VM answers first, as in a rollback; not found or not readable, nothing is deleted and
    the earlier state stays live. A state without a source id (written by a very old build, or an empty state) is
    cleaned as before, without the probe. No destination probe here: before the cutover no server holds the
    destination volumes, and drop_volume takes a volume as gone only when Cinder names it (gone())."""
    server = st.json("server.json") if st.exists("server.json") else {}
    sid = recorded_source_id(server)
    if sid is not None:
        require_source(r, "server", sid, "VM " + jqs(server.get("name")), st,
                       nothing="this re-run deleted nothing; the earlier state stays live", again="run migrate again")
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
    destination (None when there is no server ID, 'GONE' when Nova no longer has it). A20 (1.1.0): source_status is
    the source object's status now, by the same probe the rollback makes ('GONE' when not found, None when it could
    not be read; source_error then says why), next to source_power, its status when migrate exported it. A21 (1.1.1):
    dst_server_status comes from the probe the rollback makes at the destination (object_status: "GONE" only when Nova
    names that server not found, None when it could not be read); dst_error then says why (None when read or gone).
    The rollback refuses while it cannot read it."""
    now = int(time.time())
    try:
        t_stop = int(st.read("t-stop").strip())
    except ValueError:
        t_stop = None
    d = {"ident": st.arg, "kind": kind, "dir": st.path, "t_stop": None, "age_seconds": None,
         "dst_server_id": None, "dst_server_status": None, "source_power": None, "source_status": None,
         "source_error": None, "dst_error": None,
         "delete": {"server": None, "ports": [], "volumes": [dv for _s, dv in st.volume_map()], "images": []}}
    if t_stop is not None:
        d["t_stop"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t_stop))
        d["age_seconds"] = max(0, now - t_stop)
    def probe(kind_, doc):
        """source_id, source_status, source_error; a state without a source ID is not probed."""
        d["source_id"] = recorded_source_id(doc)
        if d["source_id"] is None:
            d["source_error"] = "no source id in state"
            return
        d["source_status"], why = source_status(r, kind_, d["source_id"])
        d["source_error"] = why or None

    if kind == "vol":
        vj = st.json("volume.json") if st.exists("volume.json") else {}
        d["name"] = jqs(vj.get("name")) if vj.get("name") else ""
        probe("volume", vj)
        return d
    server = st.json("server.json")
    d["name"] = jqs(server.get("name"))
    probe("server", server)
    d["source_power"] = server.get("status") if isinstance(server.get("status"), str) else None
    dsid = st.read("dst-server-id").strip()
    if dsid:
        d["dst_server_id"] = d["delete"]["server"] = dsid
        d["dst_server_status"], why = object_status(r.dst, "server", dsid)
        d["dst_error"] = why or None
    d["delete"]["ports"] = [p for p in st.read("dst-ports").split("\n") if p]
    d["delete"]["images"] = words(st.read("dst-image-id"))
    return d


def summary_lines(d):
    """The text of summary() shown before the prompt (one string per line). A20: the source's status now on the
    first line; an ERROR line when it is gone (the rollback refuses), a WARN line when it could not be read. A21:
    a WARN line too when the recorded destination server could not be read."""
    noun = "VM" if d["kind"] == "vm" else "volume"
    src = d.get("source_status")
    now = ", now %s" % src if src and src != "GONE" else ""
    out = ["Rollback of %s (source %s %s %s%s):" % (d["ident"], noun, d.get("name") or "(no name)",
                                                   d.get("source_id") or "(no source id)", now)]
    tail = []
    if src == "GONE":
        tail.append("  ERROR the source %s is not found at the source: the destination may be the only remaining "
                    "copy; this rollback is refused" % noun)
    elif "source_status" in d and src is None:
        tail.append("  WARN the source %s could not be read (%s): the rollback refuses until it can"
                    % (noun, d.get("source_error") or "no answer"))
    if d.get("dst_server_id") and d.get("dst_server_status") is None:
        tail.append("  WARN the destination server could not be read (%s): the rollback refuses until it can"
                    % (d.get("dst_error") or "no answer"))
    if d["kind"] == "vol":
        out.append("  destination volume(s) deleted: %s; the source volume was never modified"
                   % (" ".join(d["delete"]["volumes"]) or "none recorded"))
        return out + tail
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
    if src != "GONE":
        after = "kept SHUTOFF" if d["source_power"] == "SHUTOFF" else "unlocked, its ports given back, started"
        out.append("  then the source VM is %s" % after)
    return out + tail
