"""The single place that starts external programs (openstack, qemu-img, rbd, systemctl, brick).

List arguments only, never a shell. Tests replace run() to fake both clouds.

C3: openstack, rbd, ceph and systemctl get a deadline (CMD_TIMEOUT seconds, MIG_CMD_TIMEOUT in migrate.conf, default
600): a control-plane call that hangs must not hold a cutover forever. A command past it is killed and returns exit
code 124 with "timed out after N s" on stderr. Never for the data path (qemu-img, blocksync, brick, the download of a
root disk by 'openstack image save').
"""
import os
import subprocess
import time
from dataclasses import dataclass

INHERIT = "inherit"
DEVNULL = "devnull"
CAPTURE = "capture"
CMD_TIMEOUT = 600          # config.load sets it from MIG_CMD_TIMEOUT
TIMED = ("openstack", "rbd", "ceph", "systemctl")
UNTIMED_ARGS = (("image", "save"),)  # openstack commands that move data (a root disk download)
TIMEOUT_RC = 124


@dataclass
class Result:
    rc: int
    stdout: str
    stderr: str


def deadline(cmd):
    """The timeout of cmd in seconds, None for no deadline."""
    if not cmd or os.path.basename(cmd[0]) not in TIMED or not CMD_TIMEOUT:
        return None
    words = [a for a in cmd[1:] if not a.startswith("-")]
    if any(tuple(words[:len(u)]) == u for u in UNTIMED_ARGS):
        return None
    return CMD_TIMEOUT


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
                text=True, errors="replace", timeout=deadline(cmd),
            )
        except FileNotFoundError:
            msg = "%s: command not found" % cmd[0]
            if err_file:
                err_file.write(msg + "\n")
            return Result(127, "", msg if stderr == CAPTURE else "")
        except subprocess.TimeoutExpired as e:  # subprocess.run killed it already
            msg = "%s: timed out after %d s (MIG_CMD_TIMEOUT)" % (" ".join(cmd[:4]), e.timeout)
            if err_file:
                err_file.write(msg + "\n")
            elif stderr == INHERIT:
                import sys
                sys.stderr.write(msg + "\n")
                sys.stderr.flush()
            out = e.stdout if isinstance(e.stdout, str) else ""
            return Result(TIMEOUT_RC, out, msg if stderr == CAPTURE else "")
        return Result(p.returncode, p.stdout or "", p.stderr or "")
    finally:
        if err_file:
            err_file.close()


def run_stream(cmd, env=None, on_stderr_line=None):
    """Run cmd (a list, no shell) with stdout captured and each stderr line handed to on_stderr_line as it comes
    (--progress-json: blocksync's progress lines). Result.stderr stays empty, as with stderr=INHERIT. No deadline:
    it runs blocksync (the data path)."""
    import threading
    try:
        p = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, errors="replace")
    except FileNotFoundError:
        if on_stderr_line:
            on_stderr_line("%s: command not found\n" % cmd[0])
        return Result(127, "", "")
    got = []
    reader = threading.Thread(target=lambda: got.append(p.stdout.read()), daemon=True)
    try:
        reader.start()
        for line in p.stderr:
            if on_stderr_line:
                on_stderr_line(line)
        reader.join()
        return Result(p.wait(), "".join(got), "")
    except BaseException:
        # Any way out but the normal end (a SIGINT, an error in the callback): Ctrl-C reaches blocksync too (same
        # process group), but a SIGINT sent to this process only (kill -INT, or a selector passing it on) does not.
        # Stop it before the caller cleans up, so that nothing writes to the destination while it is detached.
        stop(p)
        raise
    finally:
        if reader.is_alive():
            reader.join(timeout=5)  # the child is gone (or stopped): its stdout ends
        for f in (p.stdout, p.stderr):
            try:
                f.close()
            except (OSError, ValueError):
                pass


def stop(p, grace=30):
    """SIGINT to a child still running, then SIGKILL after grace seconds; returns once it is reaped. A SIGINT that
    arrives at any point (while signalling, waiting or killing) does not cut it short: it is kept, the child is killed
    and reaped, and only then is the interrupt raised again."""
    import signal
    pending = None
    sent = False
    deadline = time.monotonic() + grace
    while True:
        try:
            if p.poll() is not None:  # inside the try: a SIGINT here is kept too
                break
            if not sent:
                sent = True
                try:
                    p.send_signal(signal.SIGINT)
                except ProcessLookupError:
                    pass
            left = deadline - time.monotonic()
            if pending is not None or left <= 0:
                p.kill()
                p.wait()
            else:
                p.wait(timeout=left)
        except subprocess.TimeoutExpired:
            continue
        except KeyboardInterrupt as e:
            pending = e
    if pending is not None:
        raise pending
