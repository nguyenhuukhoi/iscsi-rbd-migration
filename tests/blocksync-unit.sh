#!/usr/bin/env bash
# Unit test for blocksync.py on mig-host: loop device vs RBD image, no VMs involved.
# Verifies byte equality with qemu-img compare (an independent reader) after the first pass and
# after a delta pass, and that the delta pass wrote only the chunks that were really changed.
set -uo pipefail
POOL=mig-volumes; USER=mig-xfer; CONF=/etc/ceph/ceph.conf
IMG=blocksync-unit; MB=256; CHUNK_MB=4
WORK=$(mktemp -d); STATE=$WORK/hash.bin
REPORT=/root/blocksync-unit-report.txt; : > "$REPORT"
say() { echo "$*" | tee -a "$REPORT"; }
assert() { if eval "$2" >/dev/null 2>&1; then say "PASS  $1"; else say "FAIL  $1"; fi; }
RBDURL="rbd:$POOL/$IMG:id=$USER:conf=$CONF"

cleanup() {
  [ -n "${LOOP:-}" ] && losetup -d "$LOOP" 2>/dev/null
  rbd --id "$USER" -p "$POOL" rm "$IMG" 2>/dev/null
  rm -rf "$WORK"
}
trap cleanup EXIT

say "=== setup: ${MB}MB loop device, random data with zero holes"
truncate -s ${MB}M "$WORK/disk.img"
# data in chunks 0-19 and 40-49, zeros elsewhere (tests first-pass zero skipping)
dd if=/dev/urandom of="$WORK/disk.img" bs=${CHUNK_MB}M count=20 conv=notrunc status=none
dd if=/dev/urandom of="$WORK/disk.img" bs=${CHUNK_MB}M count=10 seek=40 conv=notrunc status=none
LOOP=$(losetup --find --show "$WORK/disk.img")
rbd --id "$USER" -p "$POOL" rm "$IMG" 2>/dev/null
rbd --id "$USER" -p "$POOL" create --size ${MB}M "$IMG" || { say "ABORT: cannot create RBD image"; exit 1; }

say "=== first pass"
OUT1=$(blocksync.py --device "$LOOP" --pool "$POOL" --image "$IMG" --id "$USER" --conf "$CONF" --state "$STATE" --first)
say "INFO  $OUT1"
assert "first pass rc=0" "[ -n '$OUT1' ]"
assert "first pass wrote only the 30 data chunks" "[ \"$(jq -r '.changed - .zero_chunks' <<<"$OUT1")\" = 30 ]"
assert "byte-for-byte equal after first pass" "qemu-img compare -f raw -F raw '$LOOP' '$RBDURL'"

say "=== mutate 3 chunks, zero 1 previously-written chunk"
dd if=/dev/urandom of="$WORK/disk.img" bs=${CHUNK_MB}M count=1 seek=5  conv=notrunc status=none
dd if=/dev/urandom of="$WORK/disk.img" bs=${CHUNK_MB}M count=1 seek=30 conv=notrunc status=none  # was a hole
dd if=/dev/urandom of="$WORK/disk.img" bs=${CHUNK_MB}M count=1 seek=63 conv=notrunc status=none  # last chunk
dd if=/dev/zero    of="$WORK/disk.img" bs=${CHUNK_MB}M count=1 seek=45 conv=notrunc status=none  # data -> zero
losetup -d "$LOOP"; LOOP=$(losetup --find --show "$WORK/disk.img")

say "=== delta pass"
OUT2=$(blocksync.py --device "$LOOP" --pool "$POOL" --image "$IMG" --id "$USER" --conf "$CONF" --state "$STATE")
say "INFO  $OUT2"
assert "delta pass rc=0" "[ -n '$OUT2' ]"
assert "exactly 4 chunks changed" "[ \"$(jq -r .changed <<<"$OUT2")\" = 4 ]"
assert "the zeroed chunk went through the discard path" "[ \"$(jq -r .zero_chunks <<<"$OUT2")\" = 1 ]"
assert "byte-for-byte equal after delta pass" "qemu-img compare -f raw -F raw '$LOOP' '$RBDURL'"

say "=== no-change pass writes nothing"
OUT3=$(blocksync.py --device "$LOOP" --pool "$POOL" --image "$IMG" --id "$USER" --conf "$CONF" --state "$STATE")
say "INFO  $OUT3"
assert "zero chunks changed" "[ \"$(jq -r .changed <<<"$OUT3")\" = 0 ]"

say "=== summary"
say "PASS=$(grep -c '^PASS' "$REPORT") FAIL=$(grep -c '^FAIL' "$REPORT")"
