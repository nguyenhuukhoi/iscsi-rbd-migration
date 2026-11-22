#!/usr/bin/python3
"""python3 on PATH: fakes the openstacksdk 'volumes described as' lookup of migrate-volume
(bash: python3 - <desc> <<EOF ...; Python: python3 -c <code> <desc>), anything else runs the real one."""
import json
import os
import sys

a = sys.argv[1:]
desc = None
if len(a) == 2 and a[0] == "-":
    sys.stdin.read()
    desc = a[1]
elif len(a) == 3 and a[0] == "-c" and "block_storage.volumes" in a[1]:
    desc = a[2]
if desc is None:
    os.execv("/usr/bin/python3", ["/usr/bin/python3"] + a)
side = "src" if "src" in os.environ.get("OS_AUTH_URL", "") else "dst"
with open(os.environ["HARNESS_CALLS"], "a") as f:
    f.write("%s\tSDK volumes described %s\n" % (side, desc))
w = json.load(open(os.environ["HARNESS_WORLD"]))
c = w[side]
if c.get("sdk_fail"):
    print("keystoneauth1.exceptions.http.Unauthorized: boom", file=sys.stderr)
    sys.exit(1)
for k, v in c["volumes"].items():
    if v.get("description") == desc:
        print(k)
