#!/usr/bin/python3
"""rbd / qemu-img / systemctl / brick stubs (dispatch on the program name). Calls are logged."""
import json
import os
import sys

prog = os.path.basename(sys.argv[0])
args = sys.argv[1:]
if prog in ("brick", "brick_attach.py", "__main__.py") or prog == "brick.py":
    prog = "brick"
with open(os.environ["HARNESS_CALLS"], "a") as f:
    f.write("%s\t%s\n" % (prog, " ".join(a if not a.startswith("/dev/") else "<dev>" for a in args)))

if prog == "rbd":
    if "import" in args or "export" in args:  # 1.0.0 (C6): doctor --write-check, one image per file
        store = os.path.join(os.path.dirname(os.environ["HARNESS_WORLD"]), "rbd-images")
        os.makedirs(store, exist_ok=True)
        i = args.index("import") if "import" in args else args.index("export")
        src, dst = args[i + 1], args[i + 2]
        if args[i] == "import":
            if os.path.exists(os.path.join(os.environ["HARNESS_WORLD"] + ".rbd-fail")):
                sys.exit(1)
            open(os.path.join(store, dst.replace("/", "_")), "wb").write(open(src, "rb").read())
        else:
            open(dst, "wb").write(open(os.path.join(store, src.replace("/", "_")), "rb").read())
        sys.exit(0)
    if "info" in args:
        w = json.load(open(os.environ["HARNESS_WORLD"]))
        pool = args[args.index("-p") + 1]
        vid = args[-1][len("volume-"):]
        v = w["dst"]["volumes"].get(vid)
        sys.exit(0 if v and v.get("pool") == pool else 2)
    sys.exit(0)
if prog == "qemu-img":
    if args[0] == "info":  # 1.0.0 (A9): the virtual size of a downloaded root disk (world["root_virtual_size"])
        w = json.load(open(os.environ["HARNESS_WORLD"]))
        print(json.dumps({"virtual-size": w.get("root_virtual_size", 1073741824), "format": "qcow2"}))
        sys.exit(0)
    if args[0] == "compare":
        if os.environ.get("HARNESS_COMPARE_FAIL"):
            print("Content mismatch at offset 0!")
            sys.exit(1)
        print("Images are identical.")
    sys.exit(0)
if prog == "systemctl":  # 7.2 (C7): world["inactive_units"] lists units that are not running
    w = json.load(open(os.environ["HARNESS_WORLD"]))
    sys.exit(3 if args[-1] in w.get("inactive_units", []) else 0)
if prog == "ceph":  # 7.2 (A2): 'ceph df detail -f json' of the destination cluster
    w = json.load(open(os.environ["HARNESS_WORLD"]))
    d = w["dst"]
    if d.get("ceph_fail"):
        print(d["ceph_fail"], file=sys.stderr)
        sys.exit(13)
    pools = d.get("ceph_pools") or {p: {"max_avail": 1 << 40, "stored": 0, "quota_bytes": 0}
                                     for p in set(d.get("type_pool", {}).values()) | {"mig-volumes"}}
    print(json.dumps({"stats": {}, "pools": [{"name": k, "id": i, "stats": v} for i, (k, v) in enumerate(pools.items())]}))
    sys.exit(0)
if prog == "brick":
    action, vid = args[0], args[1]
    rec = os.path.join(os.environ["HARNESS_ATTACH"], vid + ".json")
    if action == "attach":
        os.makedirs(os.environ["HARNESS_ATTACH"], exist_ok=True)
        json.dump({"path": os.environ["HARNESS_BLOCKDEV"]}, open(rec, "w"))
        print(os.environ["HARNESS_BLOCKDEV"])
    else:
        if os.path.exists(rec):
            os.remove(rec)
    sys.exit(0)
sys.exit(3)
