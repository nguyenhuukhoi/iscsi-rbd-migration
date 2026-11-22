"""Ephemeral (image-backed) root disk: it has no Cinder volume to clone, so it travels as a Glance
snapshot: snapshot the stopped VM, download the image, upload it to the destination with its guest
properties, and the new VM boots from it. Cold only; the source VM is already stopped here
(bash migrate_ephemeral_root)."""
import hashlib
import json
import os
import ssl
import time
import urllib.error
import urllib.request

from . import proc
from .copyvol import GUEST_PROP_RE
from .util import Die, jq_alt, jqs, log


TRUE_WORDS = ("1", "true", "yes")


def tls_context(env):
    """7.2 (B2): the TLS settings openstackclient takes from the openrc, for the Glance calls made here:
    OS_INSECURE=1|true|yes (no verification), else OS_CACERT (a private CA). None: Python's defaults."""
    if (env.get("OS_INSECURE") or "").strip().lower() in TRUE_WORDS:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    if env.get("OS_CACERT"):
        try:
            return ssl.create_default_context(cafile=env["OS_CACERT"])
        except (OSError, ssl.SSLError) as e:
            raise Die("cannot load OS_CACERT %s for the Glance upload: %s" % (env["OS_CACERT"], e))
    return None


def cacert_error(env):
    """Why OS_CACERT cannot be loaded ('' when it can, or is not set, or OS_INSECURE wins): the preflight check."""
    if (env.get("OS_INSECURE") or "").strip().lower() in TRUE_WORDS or not env.get("OS_CACERT"):
        return ""
    try:
        ssl.create_default_context(cafile=env["OS_CACERT"])
    except (OSError, ssl.SSLError) as e:
        return str(e)
    return ""


def no_endpoint_message(iface, rc):
    return ("the destination catalog lists no %s image endpoint: set OS_INTERFACE in %s to an interface this host "
            "reaches (public, internal or admin)" % (iface, rc))


def image_endpoint(r, must=True):
    """(interface, URL) of the destination's image endpoint: OS_INTERFACE of the destination openrc, default
    public. must: a failing listing raises (migrate); else it reads as no endpoint (preflight)."""
    iface = r.dst.env().get("OS_INTERFACE") or "public"
    args = ("endpoint", "list", "--service", "image", "--interface", iface, "-f", "value", "-c", "URL")
    urls = r.dst.out(*args) if must else r.dst.run(*args, stderr=proc.DEVNULL).stdout
    return iface, next((u.strip() for u in urls.split("\n") if u.strip()), "")


REST_TIMEOUT = 60      # C3: a Glance call that gets no answer for this long fails
UPLOAD_STALL = 600     # C3: the upload fails when no byte moves for this long (a socket timeout per send)


def _rest(method, url, token, body=None, ctype=None, length=None, ctx=None, timeout=REST_TIMEOUT):
    """One Glance REST call; raises on a failed transfer or an HTTP status >= 400 (curl -sfS).
    No proxy, as the bash build unset the proxy variables for curl. ctx: tls_context() of the destination."""
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("X-Auth-Token", token)
    if ctype:
        req.add_header("Content-Type", ctype)
    if length is not None:
        req.add_header("Content-Length", str(length))
    handlers = [urllib.request.ProxyHandler({})]
    if ctx is not None:
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    opener = urllib.request.build_opener(*handlers)
    with opener.open(req, timeout=timeout) as resp:
        return resp.read()


def _why(e):
    if isinstance(e, urllib.error.HTTPError):
        return "HTTP %d" % e.code
    return str(getattr(e, "reason", e))


def wait_image(r, cloud, iid, limit=1800):
    start = time.monotonic()
    while True:
        s = cloud.try_value_inherit("image", "show", iid, "-f", "value", "-c", "status")
        if s == "active":
            return
        if s.startswith("error") or s == "killed":
            raise Die("image %s entered status %s" % (iid, s))
        if time.monotonic() - start > limit:
            raise Die("timeout waiting for image %s (%s)" % (iid, s))
        time.sleep(5)


def migrate_ephemeral_root(r, vm, st, sid):
    snap = "mig-snap-" + vm
    log("snapshot ephemeral root to Glance as " + snap)
    t_glance = t_glance0 = time.monotonic()  # 7.3 (8, D3b): snapshot + download + upload is data through this host: copy time
    t0 = time.monotonic()
    simg = r.src.value("server", "image", "create", "--name", snap, sid, "-f", "value", "-c", "id")
    wait_image(r, r.src, simg)
    fmt = r.src.value("image", "show", simg, "-f", "value", "-c", "disk_format")
    log("snapshot ready in %ds (format %s); download" % (int(time.monotonic() - t0), fmt))
    t0 = time.monotonic()
    root = st.p("root.img")
    res = r.src.run("image", "save", "--file", root, simg, capture=False)
    if res.rc != 0:
        raise Die("downloading snapshot %s failed (exit %d)" % (simg, res.rc))
    du = proc.run(["du", "-h", root], env=r.env, capture=True).stdout.split("\t")[0].strip()
    log("downloaded %s in %ds; upload to destination" % (du, int(time.monotonic() - t0)))
    root_gb = None if r.opts.ephemeral_as_image else root_volume_size(r, root)
    t0 = time.monotonic()
    # Create and upload with plain REST. openstackclient is unusable for this step: with --file it
    # PATCHes attributes again after a long upload (Glance: 400 once active), and without --file it
    # reads image data from stdin whenever stdin is not a tty (nohup: /dev/null -> 415).
    # 7.2 (B2): the interface the destination openrc asks for (OS_INTERFACE, default public), its TLS settings
    # (OS_CACERT / OS_INSECURE, as openstackclient reads them), and a fresh token right before each call: the
    # upload of a large root disk can outlive the token taken before it.
    # Both are checked by the preflight already (ephemeral_glance_preflight); these stops are the last resort.
    iface, url = image_endpoint(r)
    if not url:
        raise Die(no_endpoint_message(iface, r.dst.rc))
    ctx = tls_context(r.dst.env())

    def fresh_token():
        return r.dst.value("token", "issue", "-f", "value", "-c", "id")

    body = json.dumps({"name": snap, "disk_format": fmt, "container_format": "bare"}, separators=(",", ":")).encode()
    try:
        created = json.loads(_rest("POST", url + "/v2/images", fresh_token(), body, "application/json", ctx=ctx))
        dimg = created.get("id") if isinstance(created, dict) else None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise Die("could not create image record on the destination (%s)" % _why(e))
    if not dimg:
        raise Die("could not create image record on the destination")
    st.write("dst-image-id", dimg + "\n")
    try:
        with open(root, "rb") as f:
            _rest("PUT", "%s/v2/images/%s/file" % (url, dimg), fresh_token(), f, "application/octet-stream",
                  os.path.getsize(root), ctx=ctx, timeout=UPLOAD_STALL)
    except (urllib.error.URLError, OSError) as e:
        raise Die("image upload to %s failed for %s (%s)" % (url, dimg, _why(e)))
    wait_image(r, r.dst, dimg)
    r.t_glance = time.monotonic() - t_glance
    r.t_copy += r.t_glance
    # Glance hashed what it stored; comparing with the local file is a real end-to-end check.
    # os_hash_* are not openstackclient columns, they live inside the properties map.
    ij = r.dst.json("image", "show", dimg, "-f", "json")
    props = ij.get("properties") if isinstance(ij.get("properties"), dict) else {}
    ralgo = jq_alt(props.get("os_hash_algo"), ij.get("os_hash_algo"))
    rhash = jq_alt(props.get("os_hash_value"), ij.get("os_hash_value"))
    ralgo = "" if ralgo is None else jqs(ralgo)
    rhash = "" if rhash is None else jqs(rhash)
    if ralgo == "sha512":
        h = hashlib.sha512()
        with open(root, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        if h.hexdigest() != rhash:
            raise Die("uploaded image hash mismatch for " + dimg)
        log("upload verified (sha512 match)")
    else:
        log("WARN destination Glance hash algo '%s' not sha512; hash check skipped" % ralgo)
    # Copy the guest-relevant properties with a raw JSON-patch: "openstack image set" PATCHes
    # read-only fields along with the property and this Glance answers 400 on an active image.
    sprops = jq_alt(r.src.json("image", "show", simg, "-f", "json").get("properties"), {})
    patch = [{"op": "add", "path": "/" + k, "value": jqs(v)}
             for k, v in (sprops.items() if isinstance(sprops, dict) else []) if GUEST_PROP_RE.search(k)]
    if patch:
        try:
            _rest("PATCH", "%s/v2/images/%s" % (url, dimg), fresh_token(),
                  json.dumps(patch, separators=(",", ":"), ensure_ascii=False).encode(),
                  "application/openstack-images-v2.1-json-patch", ctx=ctx)
        except (urllib.error.URLError, OSError) as e:
            raise Die("setting image properties failed for %s (%s)" % (dimg, _why(e)))
        log("copied guest properties: " + " ".join(p["path"] for p in patch))
    r.src.out("image", "delete", simg)
    st.remove("root.img")
    if r.opts.ephemeral_as_image:
        log("uploaded in %ds; destination image %s (kept, the new VM boots from it)" % (int(time.monotonic() - t0), dimg))
        return
    log("uploaded in %ds; destination image %s" % (int(time.monotonic() - t0), dimg))
    t0 = time.monotonic()
    root_volume_from_image(r, st, sid, dimg, root_gb)
    spent = time.monotonic() - t0  # inside the downtime window too: counted as copy (Glance) time
    r.t_glance += spent
    r.t_copy += spent


def root_volume_size(r, root):
    """A9: GB of the destination root volume: the flavor's disk (preflight: r.root_gb), at least the image's virtual
    size ('qemu-img info --output=json' of the downloaded file, needed when the flavor's disk is 0)."""
    gb = getattr(r, "root_gb", None) or 0
    res = proc.run(["qemu-img", "info", "--output=json", root], env=r.env, capture=True, stderr=proc.CAPTURE)
    try:
        vsize = int(json.loads(res.stdout).get("virtual-size")) if res.rc == 0 else 0
    except (ValueError, TypeError, AttributeError):
        vsize = 0
    need = -(-vsize // 1073741824)
    if max(gb, need) <= 0:
        raise Die("cannot tell the size of the root disk volume: the flavor's disk is %s and 'qemu-img info' of the "
                  "snapshot gave no virtual size (re-run with --ephemeral-as-image to boot from the image)" % (gb or "0"))
    return max(gb, need)


def root_volume_from_image(r, st, sid, dimg, size):
    """A9: the destination root disk as a volume made from the uploaded image (Cinder copies the image into it),
    recorded in volume-map as 'ephemeral-root <id>' before the wait (rollback deletes it); the image is deleted once
    the volume is available (rollback still deletes either)."""
    from .server import EPHEMERAL_ROOT
    name = "%s-vda" % jqs(st.json("server.json").get("name"))
    args = ["volume", "create", name, "--image", dimg, "--size", str(size)]
    if r.opts.volume_type:
        args += ["--type", r.opts.volume_type]
    if r.opts.volume_az:
        args += ["--availability-zone", r.opts.volume_az]
    args += ["--description", "migrated from %s (ephemeral root disk)" % sid, "-f", "value", "-c", "id"]
    dvid = r.dst.value(*args)
    st.append("volume-map", "%s %s" % (EPHEMERAL_ROOT, dvid))
    r.wait_volume(r.dst, dvid, "available")
    if not r.rbd("-p", r.pool, "info", "volume-" + dvid):
        raise Die("volume-%s not visible in pool %s: wrong RBD_POOL_MAP for type '%s', or %s lacks caps on that pool"
                  % (dvid, r.pool, r.opts.volume_type or "default", r.cfg.RBD_USER))
    if r.dst.run("image", "delete", dimg, stderr=proc.CAPTURE).rc == 0:
        log("root disk copied into destination volume %s (%s GB); image %s deleted" % (dvid, size, dimg))
    else:
        log("WARN root disk copied into destination volume %s (%s GB); image %s not deleted: delete it later"
            % (dvid, size, dimg))
