"""Accounts of the web UI (DESIGN.md section 8.3): the users file, the password rule and scrypt.

Shared by migui.web (reads the file at every login, verifies) and migui.passwd (root: adds, changes, removes).

    {"version": 1, "users": {"<name>": {"kdf": "scrypt", "n": 32768, "r": 8, "p": 1, "salt": "<hex 16 byte>",
                                        "hash": "<hex 32 byte>", "created": "<utc>", "changed": "<utc>"}}}

scrypt runs with an explicit maxmem: OpenSSL's default limit (32 MiB) refuses n=2**15 r=8, which needs 32 MiB
plus a little. The parameters are stored with each record, so a later, larger n does not break older accounts;
load_users() refuses parameters whose memory would exceed MAXMEM. Nothing here ever prints or logs a password,
a salt or a hash.
"""
import hashlib
import hmac
import json
import os
import secrets

from .common import USER_RE, utc

DEFAULT_USERS = "/var/lib/migui/users"
VERSION = 1
KDF = {"n": 1 << 15, "r": 8, "p": 1}
DKLEN = 32
SALT_LEN = 16
MAXMEM = 64 * 1024 * 1024        # 128 * n * r = 32 MiB for KDF; OpenSSL wants a bit more than that
PW_MIN, PW_MAX = 12, 128
MAX_FILE = 64 * 1024
MAX_USERS = 50
RECORD_KEYS = {"kdf", "n", "r", "p", "salt", "hash", "created", "changed"}

# 16.3: an unknown user is hashed too, with this salt (new at every start), so that the time of the answer does
# not tell which names exist. The comparison target is random: it never matches.
_DUMMY_SALT = secrets.token_bytes(SALT_LEN)
_DUMMY_HASH = secrets.token_bytes(DKLEN)


class UsersFileError(Exception):
    """The users file is missing, unreadable, corrupt or over its limits (the web answers 503)."""

    def __init__(self, message, missing=False):
        super().__init__(message)
        self.message = message
        self.missing = missing


class ScryptError(Exception):
    """hashlib.scrypt is missing or refused to run (memory limit)."""

    message = "scrypt unavailable or memory limit (hashlib.scrypt needs OpenSSL >= 1.1 and maxmem 64 MiB)"


class PolicyError(Exception):
    """A name or a password outside 16.1 (the message never contains the value)."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


def scrypt(password, salt, n, r, p):
    """The derived key (DKLEN bytes) of a password string; ScryptError when scrypt cannot run."""
    try:
        return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=MAXMEM, dklen=DKLEN)
    except (AttributeError, ValueError, MemoryError):
        raise ScryptError()


def self_test():
    """One scrypt with the parameters of 16.1, at start: a host where it cannot run says so before any login."""
    scrypt("migui-self-test", _DUMMY_SALT, KDF["n"], KDF["r"], KDF["p"])


def check_name(name):
    if not isinstance(name, str) or not USER_RE.match(name):
        raise PolicyError("user name: a lower-case letter, then a-z 0-9 . _ - (at most 32 characters)")
    return name


def check_password(name, password):
    """16.1: 12-128 printable characters, not the user name (any case). No composition rule (NIST 800-63B)."""
    if not isinstance(password, str) or not PW_MIN <= len(password) <= PW_MAX:
        raise PolicyError("password: %d to %d characters" % (PW_MIN, PW_MAX))
    if not all(ch.isprintable() for ch in password):
        raise PolicyError("password: printable characters only (no tab, newline or control character)")
    if password.casefold() == name.casefold():
        raise PolicyError("password: must not be the user name")
    return password


def hash_password(password):
    """A new record (kdf, n, r, p, salt, hash) for password, with the parameters of 16.1 and a fresh salt."""
    salt = secrets.token_bytes(SALT_LEN)
    dk = scrypt(password, salt, KDF["n"], KDF["r"], KDF["p"])
    return {"kdf": "scrypt", "n": KDF["n"], "r": KDF["r"], "p": KDF["p"], "salt": salt.hex(), "hash": dk.hex()}


def new_record(password, created=None):
    rec = hash_password(password)
    now = utc()
    rec["created"] = created or now
    rec["changed"] = now
    return rec


def verify_password(users, name, password):
    """True when name is in users and password matches its record. scrypt runs exactly once either way (an
    unknown name is hashed with the dummy salt), so the time of the answer does not reveal the name."""
    rec = users.get(name) if isinstance(name, str) else None
    if not isinstance(password, str):
        password = ""
    if rec is None:
        dk = scrypt(password, _DUMMY_SALT, KDF["n"], KDF["r"], KDF["p"])
        hmac.compare_digest(dk, _DUMMY_HASH)
        return False
    dk = scrypt(password, bytes.fromhex(rec["salt"]), rec["n"], rec["r"], rec["p"])
    return hmac.compare_digest(dk, bytes.fromhex(rec["hash"]))


def _hex(v, nbytes):
    if not isinstance(v, str) or len(v) != 2 * nbytes:
        return False
    try:
        bytes.fromhex(v)
    except ValueError:
        return False
    return True


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def check_record(rec):
    """A stored record is well formed and its scrypt parameters fit MAXMEM (else the file counts as corrupt)."""
    if not isinstance(rec, dict) or set(rec) != RECORD_KEYS or rec["kdf"] != "scrypt":
        return False
    n, r, p = rec["n"], rec["r"], rec["p"]
    if not (_int(n) and _int(r) and _int(p)) or n < (1 << 14) or n & (n - 1) or not 1 <= r <= 32 or not 1 <= p <= 4:
        return False
    if 128 * n * r > MAXMEM // 2:  # V of scrypt; half of maxmem leaves OpenSSL its margin (B, XY: a few KB)
        return False
    return _hex(rec["salt"], SALT_LEN) and _hex(rec["hash"], DKLEN) and isinstance(rec["created"], str) \
        and isinstance(rec["changed"], str)


def parse_users(raw):
    """{name: record} of the bytes of a users file; UsersFileError when they are not a valid file."""
    if len(raw) > MAX_FILE:
        raise UsersFileError("users file larger than %d KB" % (MAX_FILE // 1024))
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise UsersFileError("users file is not valid JSON")
    if not isinstance(obj, dict) or obj.get("version") != VERSION or not isinstance(obj.get("users"), dict):
        raise UsersFileError("users file is not a version %d users file" % VERSION)
    users = obj["users"]
    if len(users) > MAX_USERS:
        raise UsersFileError("users file holds more than %d users" % MAX_USERS)
    for name, rec in users.items():
        if not USER_RE.match(name) or not check_record(rec):
            raise UsersFileError("users file has an invalid entry")
    return users


def load_users(path):
    """{name: record} of the users file at path (read whole at each call: a new password needs no restart)."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        raise UsersFileError("no users file: run migui-passwd add", missing=True)
    except OSError as e:
        raise UsersFileError("cannot read the users file: %s" % e.strerror)
    try:
        with os.fdopen(fd, "rb") as f:
            raw = f.read(MAX_FILE + 1)
    except OSError as e:
        raise UsersFileError("cannot read the users file: %s" % e.strerror)
    return parse_users(raw)


def dump_users(users):
    """The bytes of a users file holding users."""
    return (json.dumps({"version": VERSION, "users": users}, sort_keys=True, indent=1) + "\n").encode("utf-8")
