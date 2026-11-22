"""Output and failure helpers: the exact line shapes of the bash build (log, die, preflight)."""
import json
import os
import re
import sys
import time

UUID_RE = r"[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}"
RE_ESCAPE = re.compile(r"([][\\.*^$(){}?+|])")  # the batch's sed 's/[][\.*^$(){}?+|]/\\&/g'


def re_escape(s):
    """A name as a literal inside Nova's --name regex ('server list --name ^<name>$'). 7.2 (C1): one helper
    for the selector, the export's duplicate check and the preflight's destination name check."""
    return RE_ESCAPE.sub(r"\\\1", s)


class Die(Exception):
    """bash die: 'ERROR: <message>' on stderr, exit 1."""


class Exit(Exception):
    """Leave with this code and no message (a failed preflight already printed its FAIL lines)."""

    def __init__(self, code=1):
        super().__init__(code)
        self.code = code


class CommandFailed(Die):
    """A command outside any check failed: bash's set -e would have stopped the script here."""


def out(line=""):
    # Flushed every time: qemu-img and other children write to the same stdout.
    print(line, flush=True)


def log(msg):
    out("[%s] %s" % (time.strftime("%H:%M:%S"), msg))


def die(msg):
    raise Die(msg)


def prog():
    """How to name ourselves in 'Rollback: <prog> rollback <vm>' hints (bash prints $0)."""
    a0 = sys.argv[0] if sys.argv else ""
    return a0 if os.path.basename(a0) == "migrate" else "migrate"


def jq_alt(*values):
    """jq's a // b: the first value that is neither null nor false."""
    for v in values:
        if v is not None and v is not False:
            return v
    return None


def jqs(value):
    """A value as jq -r / string interpolation prints it."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def ifs_split(text, sep):
    """bash 'IFS=<sep> read -ra': empty input gives nothing, a trailing separator adds no field."""
    if text == "":
        return []
    parts = text.split(sep)
    if parts and parts[-1] == "":
        parts.pop()
    return parts


def words(text):
    """bash word splitting of $(...) output (default IFS)."""
    return text.split()


def lines(text):
    """Non-empty lines, as 'grep -c .' counts them."""
    return [l for l in text.split("\n") if l != ""]


def jq_dump(obj):
    """Pretty JSON the way 'jq .' / 'jq -s .' writes it."""
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"
