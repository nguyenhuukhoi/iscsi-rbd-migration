"""Selector runs of 'migrate plan|migrate|volume' and 'migrate plan volume' (port of migrate-batch.sh).

One VM is a selection of one: list, Selected/Plan/Summary tables, one child process per row with
its own SRC_PROJECT/DST_PROJECT and its own log file under /var/log/mig-batch/<time>/.
"""
import fnmatch
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time

from . import config, contract, locking, proc
from .clouds import Cloud, base_env
from .livesync import check_numbers
from .options import FLAG_OPTS, VALUE_OPTS, Options
from .util import RE_ESCAPE, UUID_RE, Die, jq_dump, log, out, prog, re_escape

# The child entry: one VM or one volume, never a selection (no recursion, no prompt).
# -I (isolated): the child imports the installed package even when the current directory holds a
# migratetool/ source tree (a repo clone), which 'python -m' would otherwise put first on sys.path.
# The child still gets its whole environment (SRC_RC, SRC_PROJECT/DST_PROJECT, MIG_CONF, ...) through
# Popen's env; -I only stops the interpreter itself from reading PYTHON* variables and user site-packages.
CHILD = [sys.executable, "-I", "-m", "migratetool.single"]
SELECTORS = ("--vm", "--host", "--project", "--vm-file")
SCOPES = ("--available", "--attached", "--all")
VOLUME_PASS = ("--volume-type", "--az", "--volume-az", "--verify", "--mode")  # what a lone volume accepts (batch VOLPASS)
MAX_PLAN_JOBS = 8  # C1: plan is read-only; more than this only queues behind the APIs
IN_USE_ONE = ("--in-use copies one attached volume: migrate volume <volume> --in-use (the attached volumes of a "
              "selected VM go with their VM)")


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
        self.json = False           # 7.3 (2): plan --json, one JSON object on stdout, the text on stderr
        self.progress = False       # 7.3 (7): migrate|volume --progress-json, handed to every migrate child
        self.plan_jobs = 1          # C1: plan children run at once (plan only)

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
        elif a == "--json" and (verb == "plan" or plan_only):
            s.json, i = True, i + 1
        elif a == "--plan-jobs" and (verb == "plan" or plan_only):
            v = args[i + 1] if i + 1 < len(args) else ""
            if not (v.isdigit() and 1 <= int(v) <= MAX_PLAN_JOBS):
                raise Die("--plan-jobs takes a number from 1 to %d" % MAX_PLAN_JOBS)
            s.plan_jobs, i = int(v), i + 2
        elif a == "--progress-json" and verb in ("migrate", "volume") and not plan_only:
            s.progress, i = True, i + 1
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
        self.log = ""             # 7.3: the plan log of this row (plan --json reads its checks there)
        self.child_rc = None      # C8: exit code of this row's migrate child
        self.size = None          # D16: GB of a lone volume (from its listing)


class Runner:
    def __init__(self, sel, cfg, readonly=False):
        self.s = sel
        self.cfg = cfg
        env = base_env(cfg)
        # Listing goes through the plain openrcs: no project scope.
        self.src = Cloud("source", cfg.SRC_RC, "", env)
        self.dst = Cloud("destination", cfg.DST_RC, "", env)
        # 7.3 (SEC-9): plan --json, list, inventory: list/show only
        self.src.readonly = self.dst.readonly = readonly
        self.rows = []
        self.pmap = {}
        self.pnames = {}
        self.typed = {}           # VM ID -> the --vm value that named exactly that VM
        self._src_token_pid = None
        self._dst_token_pname = None
        # C12: '<time>-<6 hex>': two runs started in the same second get two directories
        self.log_dir = os.path.join(config.LOG_ROOT, "%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), secrets.token_hex(3)))
        self._out = threading.Lock()  # C1: one tee line at a time from parallel plan children

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
            # Review 23/11 #1: every FAIL* row with an ident to roll back (FAIL, FAIL(interrupted)); FAIL(plan) ran nothing
            if (r.res.startswith("FAIL") and r.res != "FAIL(plan)") or r.res == "SKIP(state-exists)":
                envtxt = "".join("%s=%s " % (k, r.env[k]) for k in ("SRC_PROJECT", "DST_PROJECT") if k in r.env)
                out("  %s: inspect, then to undo: %s%s rollback %s" % (r.name or r.id, envtxt, prog(), r.info))

    def _write(self, name, text):
        if self.log_dir is None:  # 7.3: 'migrate list' keeps no log directory
            return
        with open(os.path.join(self.log_dir, name), "w", encoding="utf-8") as f:
            f.write(text)

    # --- projects -----------------------------------------------------------------------------
    def load_pmap(self):
        path = self.cfg.PROJECT_MAP_FILE
        if path and not (os.path.isfile(path) and os.access(path, os.R_OK)):
            # C4: a map that cannot be read would send every VM to the project of the same name: refused.
            raise Die("PROJECT_MAP_FILE %s is not a readable file: fix it, or unset PROJECT_MAP_FILE to keep every "
                      "project name" % path)
        if path:
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
                got = self.listing(n, "--name", "^" + re_escape(v) + "$")
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

        if s.vms:  # keep the listed VMs only; a name that matches several VMs needs the ID
            picked = []
            for v in s.vms:
                m = [x for x in vms if x["id"] == v or x["name"] == v]
                if not m:
                    self.rows.append(Row("vm", "-", v, "-", "-", "-", "SKIP(not-found)"))
                    continue
                # 7.2 (A4): ambiguous in ANY project, not only within one: the listing is --all-projects, so a
                # name shared by VMs of two projects would otherwise select (and with --yes migrate) both.
                # An ID matches one VM only.
                if len(m) > 1:
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
                    # A6: only a state past its cutover is a SKIP; one that failed before it cleans itself on the
                    # re-run of its child.
                    if past_cutover(cand):
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
                row = Row("vol", vid, name, status, sp, self.pmap.get(sp, sp), res, info, pid)
                row.size = v.get("Size") if isinstance(v.get("Size"), int) else None
                self.rows.append(row)

    def volume_pass(self):
        """migrate-volume refuses server options: (the options that apply to a lone volume, the dropped ones)."""
        passed = self.s.passed
        volpass, dropped = [], []
        j = 0
        while j < len(passed):
            a = passed[j]
            nxt = passed[j + 1] if j + 1 < len(passed) else ""
            if a in VOLUME_PASS:
                volpass += [a, nxt]
                j += 2
            elif a in FLAG_OPTS:
                dropped.append(a)
                j += 1
            else:
                dropped.append("%s %s" % (a, nxt))
                j += 2
        return volpass, dropped

    # --- 7.3 (2.2): plan --json ----------------------------------------------------------------------
    def plan_report(self, rc):
        """The plan as one object. Read-only: the listing above (server list, project show, volume list), each
        child's own preflight (its log), nothing else. checks = the child's '  OK|WARN|FAIL|INFO  ' lines,
        effective = its '@@ {"effective": ...}' line."""
        from . import __version__
        from .record import vm_command
        s = self.s
        vmx = ["--volumes-only"] if s.verb == "volume" else []
        volpass, _ = self.volume_pass()
        rows, totals = [], {}
        for r in self.rows:
            key = "SKIP" if r.res.startswith("SKIP") else r.res
            totals[key] = totals.get(key, 0) + 1
            row = {"kind": r.kind, "id": r.id, "name": r.name, "ident": r.ident, "status": r.status,
                   "project": {"src": r.sp, "dst": r.dp}, "result": r.res, "info": r.info, "env": dict(r.env),
                   "command": None, "effective": None, "checks": None, "counts": None, "log": None,
                   "volumes_total": None, "gb_total": None, "nics": None, "source_power": None, "impact": None}
            if r.kind == "vol" and not r.res.startswith("SKIP"):
                row.update(volumes_total=1, gb_total=r.size, nics=0,
                           impact={"stop_source": False, "detach_ports": 0, "source_after": "running",
                                   "creates": {"server": 0, "ports": 0, "volumes": 1, "security_groups": 0,
                                               "server_group": 0}})
            if not r.res.startswith("SKIP"):
                if r.kind == "vm":
                    row["command"] = vm_command("migrate", r.ident, vmx + s.passed)
                else:
                    row["command"] = ["migrate", "volume", r.ident, *volpass]
            if r.log and not r.res.startswith("SKIP"):
                row["log"] = r.log
                facts = {}
                row["checks"], row["effective"] = read_plan_log(r.log, facts)
                row.update(facts)  # D16: volumes_total, gb_total, nics, source_power, impact
                row["counts"] = {k: sum(1 for c in row["checks"] if c["level"] == k.upper())
                                 for k in ("ok", "warn", "fail", "info")}
            rows.append(row)
        return {"tool_version": __version__, "verb": "plan volume" if s.plan_only else "plan", "log_dir": self.log_dir,
                "rows": rows, "totals": totals, "rc": rc}

    # --- children -----------------------------------------------------------------------------
    def child(self, r, verb, args, logf, tee, prefix=""):
        """One child, stdin /dev/null, stdout+stderr into its log (and to the terminal when tee, each line after prefix).
        A7: a child of 'migrate migrate|volume' inherits this run's host lock (MIG_LOCK_FD)."""
        env = dict(os.environ)
        env.update(r.env)
        env, fds = locking.child_env(env)
        with open(logf, "w", encoding="utf-8") as lf:
            p = subprocess.Popen(CHILD + [verb, r.ident, *args], env=env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                                 bufsize=1, pass_fds=fds)
            def copy(line):
                lf.write(line)
                lf.flush()
                if tee:
                    with self._out:
                        sys.stdout.write(prefix + line)
                        sys.stdout.flush()

            try:
                for line in p.stdout:
                    copy(line)
            except KeyboardInterrupt:
                # Ctrl-C reaches the whole process group, but kill -INT <pid of this selector> reaches this process only:
                # pass it on. A child that had it already ignores this one (cli._sigint_once), so its cleanup is not cut.
                try:
                    p.send_signal(signal.SIGINT)
                except OSError:
                    pass
                # Keep reading until the child is done: its cleanup lines belong in its log, and a pipe nobody reads
                # would block a child that prints more than the pipe holds.
                for line in p.stdout:
                    copy(line)
                p.wait()
                raise
            return p.wait()

    def run(self):
        s, cfg = self.s, self.cfg
        verb = s.verb
        self.load_pmap()
        os.makedirs(self.log_dir, exist_ok=True)
        log(contract.LOGS_IN
            % (self.log_dir, cfg.PROJECT_MAP_FILE or "none", len(self.pmap)))
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
        todo_plan = []
        for r in self.rows:
            if r.res != "selected":
                continue
            if r.kind == "vol":
                r.res = "READY"  # migrate-volume checks at run time
                continue
            todo_plan.append(r)
        tee = verb == "plan" or s.plan_only
        jobs = s.plan_jobs if (verb == "plan" or s.plan_only) else 1
        if jobs > 1 and len(todo_plan) > 1:
            # C1: plan children are read-only: up to --plan-jobs at once; each tee line names its ident.
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=jobs) as pool:
                list(pool.map(lambda r: self.plan_row(r, vmx, tee, "[%s] " % r.ident), todo_plan))
        else:
            for r in todo_plan:
                self.plan_row(r, vmx, tee, "")
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
        volpass, dropped = self.volume_pass()
        if self.count("READY") and dropped:
            log("available volumes: ignoring VM-only options " + " ".join(dropped))

        stop = False
        attempted = []
        current = None
        try:
            for r in self.rows:
                if r.res not in ("PLAN-OK", "READY", "PLAN-FAIL"):
                    continue
                if stop:
                    r.res = "SKIP(not-run)"
                    continue
                attempted.append(r)
                if r.res == "PLAN-FAIL":
                    r.res = "FAIL(plan)"
                else:
                    if r.kind == "vm":
                        verb_c, args = "migrate", vmx + s.passed
                    else:
                        verb_c, args = "migrate-volume", volpass
                    f = os.path.join(self.log_dir, "migrate-%s.log" % r.id)
                    log("=== %s %s (%s) %s -> %s" % (r.kind, r.name or "(no name)", r.id, r.sp, r.dp))
                    if s.progress:  # 7.3 (7): single.py takes the flag off before the options
                        args = ["--progress-json"] + args
                    current = r
                    r.child_rc = self.child(r, verb_c, args, f, tee=True)
                    if r.child_rc == 0:
                        r.res = "OK"
                        with open(f, encoding="utf-8", errors="replace") as fh:
                            m = contract.KEYPAIR_WARN_RE.search(fh.read())
                        if m:
                            r.res = "OK(no key_name: %s)" % m.group(1)
                    else:
                        r.res, r.info = "FAIL", r.ident
                if r.res.startswith("FAIL") and not s.cont:
                    stop = True
        except KeyboardInterrupt:
            # Review 23/11 #1: Ctrl-C / kill -INT still ends with the Summary and the rollback hint of the row that was
            # running (child() has drained and reaped it); then the interrupt goes on (exit 130). SIGINT is ignored
            # from here on (cli._sigint_once), so a second Ctrl-C cannot cut the summary short.
            self.interrupted(current)
            raise
        self.summary()
        return self.exit_code(attempted)

    def summary(self):
        self.show("Summary:")
        self.hints()
        out("Totals: OK %d  FAIL %d  SKIP %d  (logs: %s)"
            % (self.count("OK*"), self.count("FAIL*"), self.count("SKIP*"), self.log_dir))

    def interrupted(self, current):
        """Review 23/11 #1: the rows of an interrupted migrate run. The row whose child was running (it has not been
        given a result yet) is FAIL(interrupted) with its rollback hint; every row not run yet is SKIP(not-run)."""
        if current is not None and current.res in ("PLAN-OK", "READY"):
            current.res, current.info = "FAIL(interrupted)", current.ident
        for r in self.rows:
            if r.res in ("PLAN-OK", "READY", "PLAN-FAIL"):
                r.res = "SKIP(not-run)"
        self.summary()

    def plan_row(self, r, vmx, tee, prefix):
        """The plan child of one row; sets r.res (PLAN-OK, PLAN-FAIL, SKIP(ephemeral-root)) and r.info."""
        s = self.s
        f = os.path.join(self.log_dir, "plan-%s.log" % r.id)
        r.log = f
        # 'migrate plan' (and 'plan volume') shows each preflight as it runs; inside 'migrate migrate'
        # and 'migrate volume' it goes to the log only. With --json the child also prints its effective
        # options as one '@@ {"effective": ...}' line (single.py takes the marker off before the options).
        marker = ["--json"] if s.json else []
        if self.child(r, "plan", marker + vmx + s.passed, f, tee=tee, prefix=prefix) == 0:
            r.res = "PLAN-OK"
        else:
            with open(f, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            if contract.EPHEMERAL_NO_VOLUME in text:
                r.res = "SKIP(ephemeral-root)"
            else:
                fails = [l for l in text.split("\n") if contract.PLAN_FAIL_RE.match(l)]
                r.res, r.info = "PLAN-FAIL", "\n".join(fails) if fails else _tail1(text)
        with self._out:
            log("plan %s (%s): %s" % (r.name, r.id, r.res))

    def exit_code(self, attempted):
        """C8: 0 every row OK; one row run: its child's 3 (failed before the cutover) or 4 (after), 1 otherwise (a plan
        that failed, nothing migrated); several rows with a FAIL: 5."""
        if not self.count("FAIL*"):
            # Nothing migrated is a failure too (one VM whose state exists, a name that is not found).
            return contract.EXIT_OK if self.count("OK*") else contract.EXIT_FAIL
        if len(attempted) > 1:
            return contract.EXIT_BATCH_FAIL
        rc = attempted[0].child_rc if attempted else None
        if rc in (contract.EXIT_BEFORE_CUTOVER, contract.EXIT_AFTER_CUTOVER, contract.EXIT_INTERRUPTED):
            return rc
        return contract.EXIT_FAIL


CHECK_RE = contract.CHECK_RE


def past_cutover(name):
    """A6: a state directory whose run stopped the source (t-stop or a destination server ID): only a rollback undoes
    it. One with a volume-map but neither failed before its cutover and cleans itself when migrate runs again."""
    d = os.path.join(config.STATE_ROOT, name)
    if not os.path.exists(os.path.join(d, "volume-map")):
        return False
    for f in ("t-stop", "dst-server-id"):
        try:
            if os.path.getsize(os.path.join(d, f)) > 0:
                return True
        except OSError:
            pass
    return False


def read_plan_log(path, facts=None):
    """(checks, effective) of a plan child's log: its preflight lines (2.1 regex; anything else is left to the log
    itself) and its '@@ ' line (None when the child stopped before printing one). facts (a dict): filled with the
    D16 facts of that line (volumes_total, gb_total, nics, source_power, impact)."""
    from .jsonout import read_file
    checks, effective = [], None
    try:
        text = read_file(path)
    except (OSError, ValueError):
        return checks, effective
    for line in text.split("\n"):
        if line.startswith(contract.PROGRESS_PREFIX):
            try:
                got = json.loads(line[len(contract.PROGRESS_PREFIX):])
            except ValueError:
                continue
            if isinstance(got, dict) and "effective" in got:
                effective = got["effective"]
                if facts is not None and isinstance(got.get("facts"), dict):
                    facts.update(got["facts"])
            continue
        m = CHECK_RE.match(line)
        if m:
            checks.append({"level": m.group(1), "text": m.group(2)})
    return checks, effective


def run(verb, args, plan_only=False):
    """migrate plan|migrate|volume over a selection; plan_only with 'volume' = 'migrate plan volume'. A7: migrate and
    volume hold the host lock for the whole selection (their children inherit it); plan never locks."""
    s = finish(split_args(verb, args, plan_only))
    cfg = config.load()
    check_child_options(s, cfg)
    if not s.json:
        if verb in ("migrate", "volume") and not plan_only:
            with locking.held(cfg):
                return Runner(s, cfg).run()
        return Runner(s, cfg).run()
    # 2.1: the text goes to stderr (the log of a batch keeps all of it), stdout gets the one object.
    from .jsonout import emit_json, stdout_to_stderr
    with stdout_to_stderr() as real:
        runner = Runner(s, cfg, readonly=True)
        rc = runner.run()
        emit_json(runner.plan_report(rc), stream=real, cfg=cfg)
    return rc
