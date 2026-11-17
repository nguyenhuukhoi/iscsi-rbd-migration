#!/usr/bin/env python3
"""Hash-cached block sync from a local block device to an RBD image.

Every 4 MiB chunk is hashed (blake2b-128) and the hashes are kept in a state
file. The first pass writes every non-zero chunk (a fresh RBD image reads as
zeros). Later passes re-read the whole device but write only the chunks whose
hash changed, so the cutover pass of a live migration transfers just the delta.

Run as root on the transfer host, with librbd python bindings (python3-rbd).

  blocksync.py --device /dev/sdX --pool POOL --image volume-UUID --id CEPHUSER \
               --conf /etc/ceph/ceph.conf --state /path/hash.bin \
               [--first] [--verify readback|none]

Prints one JSON line with the pass statistics, for example:
  {"chunks": 2560, "changed": 31, "written_bytes": 130023424, ...}

Exit codes: 0 synced, 2 bad usage/state, 3 read-back verification failed.
"""
import argparse
import hashlib
import json
import os
import sys
import time

import rados
import rbd

CHUNK = 4 * 1024 * 1024  # matches the default RBD object size
DIGEST = 16


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--device", required=True)
    p.add_argument("--pool", required=True)
    p.add_argument("--image", required=True)
    p.add_argument("--id", dest="ceph_id", required=True)
    p.add_argument("--conf", default="/etc/ceph/ceph.conf")
    p.add_argument("--state", required=True)
    p.add_argument("--first", action="store_true",
                   help="first pass: no previous state, skip all-zero chunks")
    p.add_argument("--verify", choices=("readback", "none"), default="readback")
    args = p.parse_args()

    fd = os.open(args.device, os.O_RDONLY)
    dev_size = os.lseek(fd, 0, os.SEEK_END)
    nchunks = (dev_size + CHUNK - 1) // CHUNK

    old = None
    if not args.first:
        try:
            with open(args.state, "rb") as f:
                old = f.read()
        except FileNotFoundError:
            sys.exit("state file %s missing: run a --first pass before a delta pass"
                     % args.state)
        if len(old) != nchunks * DIGEST:
            sys.exit("state file %s does not match device size (%d chunks expected)"
                     % (args.state, nchunks))

    cluster = rados.Rados(conffile=args.conf, rados_id=args.ceph_id)
    cluster.connect()
    ioctx = cluster.open_ioctx(args.pool)
    image = rbd.Image(ioctx, args.image)
    if image.size() < dev_size:
        sys.exit("RBD image %s/%s is smaller (%d) than the device (%d)"
                 % (args.pool, args.image, image.size(), dev_size))

    zero = bytes(CHUNK)
    new = bytearray(nchunks * DIGEST)
    changed = written_bytes = zeroed = 0
    t0 = time.monotonic()

    for i in range(nchunks):
        off = i * CHUNK
        data = os.pread(fd, min(CHUNK, dev_size - off), off)
        h = hashlib.blake2b(data, digest_size=DIGEST).digest()
        new[i * DIGEST:(i + 1) * DIGEST] = h
        if old is not None and h == old[i * DIGEST:(i + 1) * DIGEST]:
            continue
        changed += 1
        is_zero = data == zero[:len(data)]
        if is_zero:
            zeroed += 1
            if args.first:
                continue  # a fresh RBD image already reads as zeros
            # Whole aligned objects are really deallocated by discard; a partial
            # tail may be skipped by librbd, so read back and fall back to writing.
            image.discard(off, len(data))
            if image.read(off, len(data)) != data:
                image.write(data, off)
                written_bytes += len(data)
            continue
        image.write(data, off)
        written_bytes += len(data)
        if args.verify == "readback" and image.read(off, len(data)) != data:
            sys.exit(3)
        if changed % 512 == 0:
            print("  .. %d/%d chunks read, %d changed" % (i + 1, nchunks, changed),
                  file=sys.stderr)

    image.flush()
    image.close()
    ioctx.close()
    cluster.shutdown()
    os.close(fd)

    tmp = args.state + ".tmp"
    with open(tmp, "wb") as f:
        f.write(bytes(new))
    os.replace(tmp, args.state)

    print(json.dumps({
        "device_bytes": dev_size,
        "chunks": nchunks,
        "changed": changed,
        "changed_bytes": changed * CHUNK if changed else 0,
        "written_bytes": written_bytes,
        "zero_chunks": zeroed,
        "seconds": round(time.monotonic() - t0, 1),
        "first": args.first,
    }))


if __name__ == "__main__":
    main()
