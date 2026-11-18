"""Selector runs of 'migrate plan|migrate|volume' and 'migrate plan volume' (port of migrate-batch.sh).

One VM is a selection of one: list, Selected/Plan/Summary tables, one child process per row with
its own SRC_PROJECT/DST_PROJECT and its own log file under /var/log/mig-batch/<time>/.
"""
import fnmatch
import json
import os
import re
import subprocess
import sys
import time

from . import config, proc
from .clouds import Cloud, base_env
from .livesync import check_numbers
from .options import FLAG_OPTS, VALUE_OPTS, Options
from .util import UUID_RE, Die, jq_dump, log, out, prog

# The child entry: one VM or one volume, never a selection (no recursion, no prompt).
# -I (isolated): the child imports the installed package even when the current directory holds a
# migratetool/ source tree (a repo clone), which 'python -m' would otherwise put first on sys.path.
# The child still gets its whole environment (SRC_RC, SRC_PROJECT/DST_PROJECT, MIG_CONF, ...) through
# Popen's env; -I only stops the interpreter itself from reading PYTHON* variables and user site-packages.
CHILD = [sys.executable, "-I", "-m", "migratetool.single"]
SELECTORS = ("--vm", "--host", "--project", "--vm-file")
SCOPES = ("--available", "--attached", "--all")
VOLUME_PASS = ("--volume-type", "--az", "--verify", "--mode")  # what a lone volume accepts (batch VOLPASS)
IN_USE_ONE = ("--in-use copies one attached volume: migrate volume <volume> --in-use (the attached volumes of a "
              "selected VM go with their VM)")
RE_ESCAPE = re.compile(r"([][\\.*^$(){}?+|])")  # the batch's sed 's/[][\.*^$(){}?+|]/\\&/g'


def _add_list(dest, text):
    for x in text.split(","):
        x = x.strip()
        if x:
            dest.append(x)


def _s(v):
    """A JSON field as bash read it back from jq -r ... | join(): null becomes ''."""
    return "" if v is None else str(v)


def _alt(*values):
    """jq a // b // ...: the first neither null nor false; '' when none."""
    for v in values:
        if v is not None and v is not False:
            return v
    return ""


def _tail1(text):
    """tail -1: the last line, an empty one included."""
    parts = text.split("\n")
    if text.endswith("\n"):
        parts.pop()
    return parts[-1] if parts else ""


class Selection:
    def __init__(self, verb, plan_only=False):
        self.verb = verb            # plan | migrate | volume
        self.plan_only = plan_only  # 'migrate plan volume': the volume selection, planned only
        self.hosts, self.projects, self.vms = [], [], []
        self.vm_file = ""
        self.positional = []
        self.scope = ""             # volume: attached | available | all
        self.yes = False
        self.cont = False
        self.passed = []            # per-VM options, handed to every child unchanged

    def selected_by_option(self):
        return bool(self.hosts or self.projects or self.vms or self.vm_file or self.scope)


def split_args(verb, args, plan_only=False):
    """One option namespace: selectors and run control stay here, every other option goes to each child."""
    s = Selection(verb, plan_only)
    i = 0
    while i < len(args):
        a = args[i]
        if a in SELECTORS:
            if i + 1 >= len(args) or not args[i + 1]:
                raise Die("%s needs a value" % a)
            v = args[i + 1]
            i += 2
            if a == "--vm":
                _add_list(s.vms, v)
            elif a == "--host":
                _add_list(s.hosts, v)
            elif a == "--project":
                _add_list(s.projects, v)
            else:
                s.vm_file = v
        elif a == "--yes":
            s.yes, i = True, i + 1
        elif a == "--continue-on-error":
            s.cont, i = True, i + 1
        elif verb == "volume" and a in SCOPES:
            if s.scope and s.scope != a[2:]:
                raise Die("choose one of --available, --attached, --all")
            s.scope, i = a[2:], i + 1
        elif a in VALUE_OPTS:
            if i + 1 >= len(args):
                raise Die("%s needs a value" % a)
            s.passed += [a, args[i + 1]]
            i += 2
        elif a in FLAG_OPTS:
            s.passed.append(a)
            i += 1
        # Habits of the bash build: say what replaced them.
        elif a == "--":
            raise Die("no '--' any more: the options for each VM go on the same command line")
        elif a == "--plan-only":
            if verb == "volume":
                raise Die("--plan-only is now a command of its own: %s plan volume --project <p> "
                          "--available|--attached|--all, or %s plan volume --vm <vms>" % (prog(), prog()))
            raise Die("--plan-only is now a command of its own: %s plan <vm> (or --vm/--host/--project/--vm-file)"
                      % prog())
        elif a == "--volumes":
            if verb == "plan":
                raise Die("--volumes is now: %s plan volume --project <p> --available|--attached|--all, or "
                          "%s plan volume --vm <vms>" % (prog(), prog()))
            raise Die("--volumes is now: %s volume --project <p> --available|--attached|--all, or %s volume --vm <vms>"
                      % (prog(), prog()))
        elif a.startswith("-") and a != "-":
            raise Die("unknown option %s" % a)
        else:
            s.positional.append(a)
            i += 1
    return s


def finish(s):
    """The checks after the batch's option loop; reads --vm-file."""
    verb = s.verb
    if len(s.positional) > 1:
        raise Die("one %s only (got: %s): select several with --vm a,b"
                  % ("volume" if verb == "volume" else "VM", " ".join(s.positional)))
    if s.positional:
        s.vms.insert(0, s.positional[0])
    if s.vm_file:
        if not os.access(s.vm_file, os.R_OK):
            raise Die("cannot read --vm-file " + s.vm_file)
        with open(s.vm_file, encoding="utf-8", errors="replace") as f:
            for line in f.read().split("\n"):
                line = line.split("#", 1)[0].strip()
                if line:
                    s.vms.append(line)
    seen = set()
    s.vms = [v for v in s.vms if not (v in seen or seen.add(v))]
    if not (s.hosts or s.projects or s.vms):
        if verb == "volume":
            raise Die("select something: <volume>, --project (with --available, --attached or --all), --vm or "
                      "--vm-file")
        raise Die("select something: <vm>, --vm, --host, --project or --vm-file")
    if verb == "volume":
        what = "migrate plan volume" if s.plan_only else "migrate volume"
        if not s.scope:
            if s.projects and not s.vms:
                raise Die("%s --project needs --available, --attached or --all" % what)
            s.scope = "attached"
        if s.scope == "attached" and not (s.projects or s.vms):
            raise Die("%s --attached needs --project or --vm/--vm-file (--host only narrows them)" % what)
        if s.scope in ("available", "all"):
            if not s.projects:
                raise Die("%s --%s needs --project" % (what, s.scope))
            if s.hosts:
                raise Die("--host filters VMs: use it with --attached only")
            if s.scope == "available" and s.vms:
                raise Die("--available lists volumes, not VMs: drop --vm/--vm-file")
        if "--in-use" in s.passed:
            raise Die(IN_USE_ONE)
        if " --mode live " in " %s " % " ".join(s.passed):
            raise Die("migrate volume copies cold only: drop --mode live")
    return s


def check_child_options(s, cfg):
    """What every VM child would refuse (migrate-vm.sh's option checks), refused once before anything is
    listed. Lone volumes take only their own options (and their own MIG_DEFAULT_OPTS filter)."""
    if s.verb == "volume" and s.scope == "available":
        return
    o = Options()
    o.load_default_opts(s.verb if s.verb == "plan" else "migrate", cfg.MIG_DEFAULT_OPTS)
    o.parse_options((["--volumes-only"] if s.verb == "volume" else []) + list(s.passed))
    if o.in_use:
        raise Die("--in-use belongs to migrate-volume: plan and migrate move a VM with its volumes (--mode live keeps "
                  "it running until the cutover)")
    if o.mode == "live":
        check_numbers(o)


class Row:
    def __init__(self, kind, rid, name, status, sp, dp, res, info="", pid=""):
        self.kind, self.id, self.name, self.status = kind, rid, name, status
        self.sp, self.dp, self.res, self.info = sp, dp, res, info
        self.pid = pid            # source project ID
        self.ident = rid          # the name or ID the child runs with (see enumerate_vms)
        self.env = {}             # SRC_PROJECT / DST_PROJECT of this row's child


class Runner:
    def __init__(self, sel, cfg):
        self.s = sel
        self.cfg = cfg
        env = base_env(cfg)
        # Listing goes through the plain openrcs: no project scope.
        self.src = Cloud("source", cfg.SRC_RC, "", env)
        self.dst = Cloud("destination", cfg.DST_RC, "", env)
        self.rows = []
        self.pmap = {}
        self.pnames = {}
        self.typed = {}           # VM ID -> the --vm value that named exactly that VM
        self._src_token_pid = None
        self._dst_token_pname = None
        self.log_dir = os.path.join(config.LOG_ROOT, time.strftime("%Y%m%d-%H%M%S"))

    # --- output -------------------------------------------------------------------------------
    def show(self, title):
        out()
        out(title)
        out("%-4s %-36s %-24s %-9s %-32s %s" % ("KIND", "ID", "NAME", "STATUS", "PROJECT src -> dst", "RESULT"))
        for r in self.rows:
            out("%-4s %-36s %-24s %-9s %-32s %s" % (r.kind, r.id, r.name or "-", r.status, "%s -> %s" % (r.sp, r.dp),
                                                   r.res))

    def count(self, pattern):
        return sum(1 for r in self.rows if fnmatch.fnmatchcase(r.res, pattern))

    def hints(self):
        """How to undo a failed run or an earlier state; rollback goes by the recorded ID or name."""
        for r in self.rows:
            if r.res in ("FAIL", "SKIP(state-exists)"):
                envtxt = "".join("%s=%s " % (k, r.env[k]) for k in ("SRC_PROJECT", "DST_PROJECT") if k in r.env)
                out("  %s: inspect, then to undo: %s%s rollback %s" % (r.name or r.id, envtxt, prog(), r.info))

    def _write(self, name, text):
        with open(os.path.join(self.log_dir, name), "w", encoding="utf-8") as f:
            f.write(text)

    # --- projects -----------------------------------------------------------------------------
    def load_pmap(self):
        path = self.cfg.PROJECT_MAP_FILE
        if path and os.access(path, os.R_OK):
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            lines = text.split("\n")
            if text.endswith("\n"):
                lines.pop()
            for line in lines:
                fields = line.split("#", 1)[0].split()
                if not fields:
                    continue
                if len(fields) != 2:
                    raise Die("%s: expected 'source-project destination-project', got: %s" % (path, line))
                self.pmap[fields[0]] = fields[1]

    def proj_name(self, pid):
        if pid not in self.pnames:
            self.pnames[pid] = self.src.value("project", "show", pid, "-f", "value", "-c", "name")
        return self.pnames[pid]

    def src_token_pid(self):
        if self._src_token_pid is None:
            self._src_token_pid = self.src.project_id()
        return self._src_token_pid

    def dst_token_pname(self):
        if self._dst_token_pname is None:
            try:
                self._dst_token_pname = self.dst.try_value("project", "show", self.dst.project_id(),
                                                           "-f", "value", "-c", "name")
            except Die:
                self._dst_token_pname = ""
        return self._dst_token_pname

    def row_env(self, r):
        """SRC_PROJECT/DST_PROJECT of the row's child, as the batch set them. An application credential
        cannot be rescoped (the batch refused one outright): a row in the credential's own project
        runs without it, as a single migrate-vm.sh run did; any other project is set and the child
        refuses it with its own message."""
        if r.sp == "-" or r.res not in ("selected", "SKIP(state-exists)"):
            return
        if not ("applicationcredential" in self.src.auth_type() and r.pid == self.src_token_pid()):
            r.env["SRC_PROJECT"] = r.sp
        if not ("applicationcredential" in self.dst.auth_type() and r.dp == self.dst_token_pname()):
            r.env["DST_PROJECT"] = r.dp

    # --- rows ---------------------------------------------------------------------------------
    def listing(self, n, *args):
        """server list --all-projects --long ..., saved as list-<n>.json. A token that may not list
        other projects (no admin role, an application credential) lists its own project instead."""
        res = self.src.run("server", "list", "--all-projects", "--long", *args, "-f", "json", stderr=proc.CAPTURE)
        if res.rc != 0:
            res = self.src.run("server", "list", "--long", *args, "-f", "json")
            if res.rc != 0:
                raise Die("cannot list the source servers (%s)" % " ".join(args))
        self._write("list-%d.json" % n, res.stdout)
        return json.loads(res.stdout or "[]")

    def enumerate_vms(self):
        s = self.s
        lists = []
        n = 0
        if s.hosts:
            for h in s.hosts:
                n += 1
                got = self.listing(n, "--host", h)
                if not got:
                    log("WARN no VM on host %s (the name must match the Host column of 'openstack compute service list')"
                        % h)
                lists.append(got)
        elif s.projects:
            for p in s.projects:
                n += 1
                lists.append(self.listing(n, "--project", p))
        else:
            for v in s.vms:
                n += 1
                got = self.listing(n, "--name", "^" + RE_ESCAPE.sub(r"\\\1", v) + "$")
                if not got and re.fullmatch(UUID_RE, v):
                    res = self.src.run("server", "show", v, "-f", "json", stderr=proc.DEVNULL)
                    got = []
                    if res.rc == 0:
                        sj = json.loads(res.stdout)
                        got = [{"ID": sj.get("id"), "Name": sj.get("name"), "Status": sj.get("status"),
                                "Project ID": _alt(sj.get("project_id"), sj.get("tenant_id"))}]
                    self._write("list-%d.json" % n, jq_dump(got))
                lists.append(got)
        vms = []
        for lst in lists:
            for e in lst:
                row = {"id": e.get("ID"), "name": e.get("Name"), "status": e.get("Status"),
                       "project_id": _alt(e.get("Project ID"))}
                if not any(x["id"] == row["id"] for x in vms):
                    vms.append(row)
        self._write("vms.json", jq_dump(vms))

        if s.vms:  # keep the listed VMs only; a name twice in one project needs the ID
            picked = []
            for v in s.vms:
                m = [x for x in vms if x["id"] == v or x["name"] == v]
                if not m:
                    self.rows.append(Row("vm", "-", v, "-", "-", "-", "SKIP(not-found)"))
                    continue
                groups = {}
                for x in m:
                    groups.setdefault(json.dumps(x["project_id"]), []).append(x)
                if any(len(g) > 1 for g in groups.values()):
                    for x in m:
                        self.rows.append(Row("vm", _s(x["id"]), _s(x["name"]), _s(x["status"]), "-", "-",
                                             "SKIP(ambiguous-name: use the ID)"))
                    continue
                picked += m
                # A name with regex characters stays with the ID: the child's own duplicate check and
                # 'server show <name>' go through Nova's name regex, unescaped.
                if len(m) == 1 and not RE_ESCAPE.search(v):
                    self.typed.setdefault(m[0]["id"], v)
            self._write("selected.jsonl", "".join(json.dumps(x, ensure_ascii=False, separators=(",", ":")) + "\n"
                                                  for x in picked))
            vms = []
            for x in picked:
                if not any(y["id"] == x["id"] for y in vms):
                    vms.append(x)
            self._write("vms.json", jq_dump(vms))

        want = set()  # --host and --project together: keep those projects' VMs
        if s.hosts:
            for p in s.projects:
                want.add(self.src.value("project", "show", p, "-f", "value", "-c", "id"))
        for x in vms:
            vid, name, status, pid = _s(x["id"]), _s(x["name"]), _s(x["status"]), _s(x["project_id"])
            if not pid:
                # Clients that print no "Project ID" column in "server list --long": ask the server itself.
                sj = self.src.json("server", "show", vid, "-f", "json")
                pid = _s(_alt(sj.get("project_id"), sj.get("tenant_id")))
            if not pid:
                raise Die("cannot find the project of VM " + vid)
            if want and pid not in want:
                continue
            pn = self.proj_name(pid)
            res, info = "selected", ""
            if status in ("ACTIVE", "SHUTOFF"):
                for cand in (vid, name):
                    if os.path.exists(os.path.join(config.STATE_ROOT, cand, "volume-map")):
                        res, info = "SKIP(state-exists)", cand
                        break
            else:
                res = "SKIP(status)"
            row = Row("vm", vid, name, status, pn, self.pmap.get(pn, pn), res, info, pid)
            # The child runs with what was typed when that names exactly this VM, so that state, log
            # lines and the rollback hint use it, as with a single migrate-vm.sh run; else the ID.
            row.ident = _s(self.typed.get(x["id"], vid))
            self.rows.append(row)

    def enumerate_volumes(self):
        for p in self.s.projects:
            pj = self.src.json("project", "show", p, "-f", "json")
            sp, pid = _s(pj.get("name")), _s(pj.get("id"))
            raw = self.src.out("volume", "list", "--project", pid, "-f", "json")
            self._write("volumes-%s.json" % sp, raw)
            vols = json.loads(raw or "[]")
            log("%s: %d in-use volume(s) not listed — they go with their VM (migrate volume --attached)"
                % (sp, sum(1 for v in vols if v.get("Status") == "in-use")))
            for v in vols:
                vid, name, status = _s(v.get("ID")), _s(_alt(v.get("Name"))), _s(v.get("Status"))
                res, info = "selected", ""
                if status == "in-use":
                    continue
                if status == "available":
                    if name.startswith("mig-clone-"):
                        res = "SKIP(mig-clone)"  # a leftover temporary clone
                    elif os.path.exists(os.path.join(config.STATE_ROOT, "vol-" + vid, "volume-map")):
                        res, info = "SKIP(state-exists)", vid
                else:
                    res = "SKIP(status)"
                self.rows.append(Row("vol", vid, name, status, sp, self.pmap.get(sp, sp), res, info, pid))

    # --- children -----------------------------------------------------------------------------
    def child(self, r, verb, args, logf, tee):
        """One child, stdin /dev/null, stdout+stderr into its log (and to the terminal when tee)."""
        env = dict(os.environ)
        env.update(r.env)
        with open(logf, "w", encoding="utf-8") as lf:
            p = subprocess.Popen(CHILD + [verb, r.ident, *args], env=env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                                 bufsize=1)
            try:
                for line in p.stdout:
                    lf.write(line)
                    lf.flush()
                    if tee:
                        sys.stdout.write(line)
                        sys.stdout.flush()
            except KeyboardInterrupt:
                p.wait()  # the child got the same signal: let it clean up first
                raise
            return p.wait()

    def run(self):
        s, cfg = self.s, self.cfg
        verb = s.verb
        self.load_pmap()
        os.makedirs(self.log_dir, exist_ok=True)
        log("logs in %s; project map: %s (%d entries, other projects keep their name)"
            % (self.log_dir, cfg.PROJECT_MAP_FILE or "none", len(self.pmap)))
        if cfg.PROJECT_MAP_FILE and not os.access(cfg.PROJECT_MAP_FILE, os.R_OK):
            log("WARN %s is not readable: every project keeps its name" % cfg.PROJECT_MAP_FILE)
        if verb != "volume" or s.scope != "available":
            self.enumerate_vms()
        if verb == "volume" and s.scope in ("available", "all"):
            self.enumerate_volumes()
        if not self.rows:
            log("nothing selected")
            return 0
        for r in self.rows:
            self.row_env(r)
        self.show("Selected:")

        # --- plan ---------------------------------------------------------------------------
        vmx = ["--volumes-only"] if verb == "volume" else []
        for r in self.rows:
            if r.res != "selected":
                continue
            if r.kind == "vol":
                r.res = "READY"  # migrate-volume checks at run time
                continue
            f = os.path.join(self.log_dir, "plan-%s.log" % r.id)
            # 'migrate plan' (and 'plan volume') shows each preflight as it runs; inside 'migrate migrate'
            # and 'migrate volume' it goes to the log only.
            if self.child(r, "plan", vmx + s.passed, f, tee=(verb == "plan" or s.plan_only)) == 0:
                r.res = "PLAN-OK"
            else:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
                if "ephemeral VM has no volume to migrate" in text:
                    r.res = "SKIP(ephemeral-root)"
                else:
                    fails = [l for l in text.split("\n") if re.match(r" +FAIL|ERROR", l)]
                    r.res, r.info = "PLAN-FAIL", "\n".join(fails) if fails else _tail1(text)
            log("plan %s (%s): %s" % (r.name, r.id, r.res))
        self.show("Plan:")
        for r in self.rows:
            if r.res != "PLAN-FAIL":
                continue
            out("%s (%s), full output in %s/plan-%s.log:" % (r.name, r.id, self.log_dir, r.id))
            for line in r.info.split("\n"):
                out("    " + line.lstrip(" "))
        self.hints()
        if verb == "plan" or s.plan_only:
            # A dry-run fails on a PLAN-FAIL, and when nothing it selected could be planned at all.
            # 'plan volume' stops here as the batch's '--volumes ... --plan-only' did: an available
            # volume has no plan of its own (READY, migrate-volume checks it at run time), the VMs of
            # --attached ran 'plan --volumes-only' above.
            return 1 if self.count("PLAN-FAIL") or not (self.count("PLAN-OK") + self.count("READY")) else 0

        # --- migrate, one at a time ------------------------------------------------------------
        todo = self.count("PLAN-OK") + self.count("READY")
        if todo > 0 and not s.yes:
            if not sys.stdin.isatty():
                raise Die("stdin is not a terminal: re-run with --yes to migrate without confirmation")
            if self.count("PLAN-FAIL") and not s.cont:
                out("Note: without --continue-on-error the run stops at the first PLAN-FAIL row.")
            sys.stderr.write("Migrate %d item(s) one by one%s? Type yes to continue: "
                             % (todo, (" with: " + " ".join(s.passed)) if s.passed else ""))
            sys.stderr.flush()
            if sys.stdin.readline().strip(" \t\n") != "yes":
                raise Die("not confirmed, nothing changed")
        # migrate-volume refuses server options: keep only those that apply to a lone volume.
        volpass, dropped = [], []
        j = 0
        while j < len(s.passed):
            a = s.passed[j]
            nxt = s.passed[j + 1] if j + 1 < len(s.passed) else ""
            if a in VOLUME_PASS:
                volpass += [a, nxt]
                j += 2
            elif a in FLAG_OPTS:
                dropped.append(a)
                j += 1
            else:
                dropped.append("%s %s" % (a, nxt))
                j += 2
        if self.count("READY") and dropped:
            log("available volumes: ignoring VM-only options " + " ".join(dropped))

        stop = False
        for r in self.rows:
            if r.res not in ("PLAN-OK", "READY", "PLAN-FAIL"):
                continue
            if stop:
                r.res = "SKIP(not-run)"
                continue
            if r.res == "PLAN-FAIL":
                r.res = "FAIL(plan)"
            else:
                if r.kind == "vm":
                    verb_c, args = "migrate", vmx + s.passed
                else:
                    verb_c, args = "migrate-volume", volpass
                f = os.path.join(self.log_dir, "migrate-%s.log" % r.id)
                log("=== %s %s (%s) %s -> %s" % (r.kind, r.name or "(no name)", r.id, r.sp, r.dp))
                if self.child(r, verb_c, args, f, tee=True) == 0:
                    r.res = "OK"
                    with open(f, encoding="utf-8", errors="replace") as fh:
                        m = re.search(r"WARN keypair ([^ \n]*) not at destination", fh.read())
                    if m:
                        r.res = "OK(no key_name: %s)" % m.group(1)
                else:
                    r.res, r.info = "FAIL", r.ident
            if r.res.startswith("FAIL") and not s.cont:
                stop = True

        self.show("Summary:")
        self.hints()
        out("Totals: OK %d  FAIL %d  SKIP %d  (logs: %s)"
            % (self.count("OK*"), self.count("FAIL*"), self.count("SKIP*"), self.log_dir))
        # Nothing migrated is a failure too (one VM whose state exists, a name that is not found).
        return 1 if self.count("FAIL*") or not self.count("OK*") else 0


def run(verb, args, plan_only=False):
    """migrate plan|migrate|volume over a selection; plan_only with 'volume' = 'migrate plan volume'."""
    s = finish(split_args(verb, args, plan_only))
    cfg = config.load()
    check_child_options(s, cfg)
    return Runner(s, cfg).run()
