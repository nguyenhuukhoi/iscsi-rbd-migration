#!/usr/bin/python3
"""Fake blocksync.py: per source volume (from --state hash-<vid>.bin) the world lists one result per
pass under world["sync"][vid]; the last one repeats. Writes the state file like the real one."""
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["HARNESS_CALLS"], "a") as f:
    f.write("blocksync\t%s\n" % " ".join(a if not a.startswith("/dev/") else "<dev>" for a in args))
state = args[args.index("--state") + 1]
vid = os.path.basename(state)[len("hash-"):-len(".bin")]
wf = os.environ["HARNESS_WORLD"]
w = json.load(open(wf))
seq = w.get("sync", {}).get(vid) or [{"changed": 10, "chunks": 2560, "written_bytes": 41943040, "zero_chunks": 0,
                                       "seconds": 1.0}]
idx = w.setdefault("sync_idx", {}).get(vid, 0)
e = seq[min(idx, len(seq) - 1)]
w["sync_idx"][vid] = idx + 1
json.dump(w, open(wf, "w"), indent=1)
if "--first" not in args and not os.path.exists(state):
    sys.exit("state file %s missing: run a --first pass before a delta pass" % state)
open(state, "wb").write(b"h" * 16)
# 7.3: world["bs_lines"] replaces the one progress line (the --progress-json rate limit)
for _l in w.get("bs_lines") or ["[blocksync] 50% (1.0/2.0 GiB), changed 4 MiB"]:
    print(_l, file=sys.stderr, flush=True)
if e.get("exit"):
    sys.exit(e["exit"])
if e.get("garbage"):
    print("not json")
    sys.exit(0)
print(json.dumps({"device_bytes": e["chunks"] * 4194304, "chunks": e["chunks"], "changed": e["changed"],
                  "changed_bytes": e["changed"] * 4194304, "written_bytes": e["written_bytes"],
                  "zero_chunks": e["zero_chunks"], "seconds": e["seconds"], "first": "--first" in args}))
