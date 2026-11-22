#!/usr/bin/env python3
"""install.sh re-run with an append-only audit file (first lab deploy, 20/11): audit_setup of migui/deploy/install.sh,
sourced alone (MIGUI_INSTALL_FUNCS_ONLY=1), run several times on a temporary file. Cases INST-<n>.

Needs root (chown, chattr); without root every case is reported as a SKIP line and counted as passed. The file lives
under /var/tmp when that file system supports chattr +a (ext4/xfs), else under /tmp (then the +a part is skipped).

    python3 -I tests/ui/install_audit.py <repo>
"""
import grp
import os
import shutil
import stat
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness as H  # noqa: E402
from harness import ok  # noqa: E402

INSTALL = os.path.join(H.REPO, "migui", "deploy", "install.sh")


def has_group(name):
    try:
        return grp.getgrnam(name).gr_gid
    except KeyError:
        return None


def setup(path, group):
    p = subprocess.run(["bash", "-c", 'set -eu; MIGUI_INSTALL_FUNCS_ONLY=1; . "$1"; audit_setup "$2" "$3"',
                        "_", INSTALL, path, group], capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def attrs(path):
    p = subprocess.run(["lsattr", "-d", path], capture_output=True, text=True)
    return p.stdout.split()[0] if p.returncode == 0 and p.stdout.split() else ""


if os.getuid() != 0:
    print("SKIP INST-* need root (chown, chattr): run on mig-host as root")
    sys.exit(H.total())

group = next(g for g in ("daemon", "nogroup", "bin") if has_group(g) is not None)
gid = has_group(group)
base = None
for cand in ("/var/tmp", "/tmp"):
    d = tempfile.mkdtemp(prefix="migui-test-inst-", dir=cand)
    probe = os.path.join(d, "probe")
    open(probe, "w").close()
    plus_a = subprocess.run(["chattr", "+a", probe], capture_output=True).returncode == 0
    subprocess.run(["chattr", "-a", probe], capture_output=True)
    os.unlink(probe)
    if plus_a or cand == "/tmp":
        base = d
        break
    shutil.rmtree(d)
f = os.path.join(base, "audit.jsonl")
try:
    def state():
        st = os.stat(f)
        return st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode), "a" in attrs(f)

    rc1, out1 = setup(f, group)
    s1 = state()
    ok("INST-1 first run: the audit file is created root:%s 0620%s" % (group, ", append-only" if plus_a else ""),
       rc1 == 0 and s1 == (0, gid, 0o620, plus_a), (rc1, out1, s1))
    with open(f, "a") as fh:
        fh.write('{"action":"x"}\n')
    rc2, out2 = setup(f, group)
    ok("INST-2 second run on the append-only file: exit 0, nothing changed, says 'already set'; the content stays",
       rc2 == 0 and state() == s1 and ("already set" in out2 or not plus_a) and "owner/mode" not in out2
       and open(f).read() == '{"action":"x"}\n', (rc2, out2, state()))
    subprocess.run(["chattr", "-a", f], capture_output=True)
    os.chmod(f, 0o644)
    os.chown(f, 0, 0)
    if plus_a:
        subprocess.run(["chattr", "+a", f], check=True)
    rc3, out3 = setup(f, group)
    ok("INST-3 drifted owner/mode on an append-only file: exit 0, fixed to root:%s 0620, append-only again" % group,
       rc3 == 0 and state() == (0, gid, 0o620, plus_a) and "owner/mode 0:0:644 -> 0:%d:620" % gid in out3,
       (rc3, out3, state()))
    if not plus_a:
        print("SKIP INST +a parts: %s does not support chattr +a" % base)
finally:
    subprocess.run(["chattr", "-a", f], capture_output=True)
    shutil.rmtree(base, ignore_errors=True)
sys.exit(H.total())
