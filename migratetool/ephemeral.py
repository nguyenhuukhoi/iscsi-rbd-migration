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


def _rest(method, url, token, body=None, ctype=None, length=None, ctx=None):
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
    with opener.open(req) as resp:
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
    t0 = time.monotonic()
    # Create and upload with plain REST. openstackclient is unusable for this step: with --file it
    # PATCHes attributes again after a long upload (Glance: 400 once active), and without --file it
    # reads image data from stdin whenever stdin is not a tty (nohup: /dev/null -> 415).
    # 7.2 (B2): the interface the destination openrc asks for (OS_INTERFACE, default public), its TLS settings
    # (OS_CACERT / OS_INSECURE, as openstackclient reads them), and a fresh token right before each call: the
    # upload of a large root disk can outlive the token taken before it.
    denv = r.dst.env()
    iface = denv.get("OS_INTERFACE") or "public"
    urls = r.dst.out("endpoint", "list", "--service", "image", "--interface", iface, "-f", "value", "-c", "URL")
    url = next((u.strip() for u in urls.split("\n") if u.strip()), "")
    if not url:
        raise Die("the destination catalog lists no %s image endpoint: set OS_INTERFACE in %s to an interface this "
                  "host reaches (public, internal or admin)" % (iface, r.dst.rc))
    ctx = tls_context(denv)

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
                  os.path.getsize(root), ctx=ctx)
    except (urllib.error.URLError, OSError) as e:
        raise Die("image upload to %s failed for %s (%s)" % (url, dimg, _why(e)))
    wait_image(r, r.dst, dimg)
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
    log("uploaded in %ds; destination image %s (kept, the new VM boots from it)" % (int(time.monotonic() - t0), dimg))
