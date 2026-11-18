"""State directory /var/lib/mig-migrate/<arg>/: same layout and file formats as the bash build,
so either build can roll back what the other one started (DESIGN-V7 section 2.1)."""
import json
import os
import time

from . import config


class StateDir:
    def __init__(self, arg, root=None):
        self.arg = arg
        self.root = root or config.STATE_ROOT
        self.path = os.path.join(self.root, arg)

    def p(self, name):
        return os.path.join(self.path, name)

    def exists(self, name):
        return os.path.exists(self.p(name))

    def nonempty(self, name):
        """bash [ -s file ]"""
        try:
            return os.path.getsize(self.p(name)) > 0
        except OSError:
            return False

    def create(self):
        os.makedirs(self.path, exist_ok=True)
        os.chmod(self.path, 0o700)

    def read(self, name, default=""):
        try:
            with open(self.p(name), encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return default

    def json(self, name):
        with open(self.p(name), encoding="utf-8") as f:
            return json.load(f)

    def write(self, name, text):
        with open(self.p(name), "w", encoding="utf-8") as f:
            f.write(text)

    def append(self, name, line):
        with open(self.p(name), "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def remove(self, name):
        try:
            os.remove(self.p(name))
        except FileNotFoundError:
            pass

    def retire(self):
        """mv <dir> <dir>.rolled-back.<epoch>"""
        os.rename(self.path, "%s.rolled-back.%d" % (self.path, int(time.time())))

    # --- volume-map: '<source-volume-id> <destination-volume-id>' per line ------------------------
    def volume_map(self):
        pairs = []
        for line in self.read("volume-map").splitlines():
            f = line.split()
            if len(f) >= 2:
                pairs.append((f[0], f[1]))
        return pairs

    def dst_volume_of(self, svid):
        """awk -v s=<svid> '$1==s {print $2}' volume-map"""
        return "\n".join(d for s, d in self.volume_map() if s == svid)
