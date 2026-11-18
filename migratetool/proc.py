"""The single place that starts external programs (openstack, qemu-img, rbd, systemctl, brick).

List arguments only, never a shell. Tests replace run() to fake both clouds.
"""
import subprocess
from dataclasses import dataclass

INHERIT = "inherit"
DEVNULL = "devnull"
CAPTURE = "capture"


@dataclass
class Result:
    rc: int
    stdout: str
    stderr: str


def run(cmd, env=None, capture=True, stderr=INHERIT):
    """Run cmd (a list). capture: return stdout as text instead of passing it through.
    stderr: INHERIT, DEVNULL, CAPTURE, or a path the stream is written to (truncated first)."""
    err_file = None
    if stderr == INHERIT:
        err = None
    elif stderr == DEVNULL:
        err = subprocess.DEVNULL
    elif stderr == CAPTURE:
        err = subprocess.PIPE
    else:
        err_file = open(stderr, "w")
        err = err_file
    try:
        try:
            p = subprocess.run(
                cmd, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if capture else None, stderr=err,
                text=True, errors="replace",
            )
        except FileNotFoundError as e:
            msg = "%s: command not found" % cmd[0]
            if err_file:
                err_file.write(msg + "\n")
            return Result(127, "", msg if stderr == CAPTURE else "")
        return Result(p.returncode, p.stdout or "", p.stderr or "")
    finally:
        if err_file:
            err_file.close()
