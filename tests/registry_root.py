"""A real-git root checkout for #954: `work.py start` against its own registry writes.

`World(tmp_path).build()` is a bare `remote.git` plus a root checkout on `main` that has ADOPTED the
branching model (`.sdlc/features/units/` exists), with `feature/voice` and `feature/billing` pushed
at main's tip. Every `gh` read is the one canned issue body below; every `git` call is real.

WHY A HELPER MODULE AND NOT A FIXTURE. Two test files share it, and the installed `red_green.py`
binds a red to the bytes of the TEST FILE only -- this file sits outside red identity, which is
also why it must not be edited casually once reds are recorded (an edit here can weaken a test
without voiding its red).

OUTPUT DISCIPLINE (plan §7, measured against the installed `red_green.py`). Any `Captured
stdout|stderr|log` section anywhere in a verify run voids every traceback-attributed red in that
run. So every subprocess here captures its own output, nothing here prints, and each test runs its
whole body inside `drained(capfd)`, which consumes whatever reached the file descriptors before
pytest can turn it into a report section.
"""
import contextlib
import importlib.util
import inspect
import json
import os
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"

#: §8e's layout: one chain file per registry file, mirroring its path under `.sdlc/features/`.
RECORD_DIR = ".sdlc/state/features/provenance"
SHARD = ".sdlc/features/units/voice.json"
PAGE = ".sdlc/features/voice.md"
REPO = "acme/app"
CONFIG = {"work": {"enabled": True, "base": "main"},
          "discovery": {"source": "github", "github": {"repo": REPO}}}
#: The one issue every goal number reads as: it declares the `voice` unit.
ISSUE = {"body": "Feature: voice\nBranch: feature/voice\n", "labels": [{"name": "feature:voice"}]}
#: `setup.RUNTIME_IGNORES`' shape. `.sdlc/features/` is deliberately NOT ignored: adopters track it.
GITIGNORE = ".sdlc/state/\n.sdlc/work/\n.sdlc/ledger/\n.sdlc/events/\n"
#: §15's maintainer gesture, verbatim.
COMMIT_GESTURE = 'git add .sdlc/features && git commit -m "registry: record unit membership"'


def module(name):
    """A script loaded fresh by path, or None when the file does not exist -- so a test reaching a
    module this change adds fails by ASSERTION on the old tree, never by ImportError."""
    path = SCRIPTS / f"{name}.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def git(cwd, *args):
    """Real git, output captured. A setup step that fails raises with git's own words."""
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError("git %s: %s" % (" ".join(args), (proc.stderr or proc.stdout).strip()))
    return proc.stdout


def blob_id(data):
    """git's own object id for `data` as a blob, computed here rather than borrowed, so a test that
    compares against it cannot inherit a bug from the module under test."""
    import hashlib
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class _Out:
    def __init__(self, capfd):
        self._capfd = capfd

    def take(self):
        """-> (stdout, stderr) written since the last take, consumed so pytest never reports it."""
        return self._capfd.readouterr()


@contextlib.contextmanager
def drained(capfd):
    """Consume FD-level output on every exit -- pass, assertion failure or error -- before pytest's
    call phase closes the capture fixture and would otherwise flush it into a report section."""
    out = _Out(capfd)
    try:
        yield out
    finally:
        capfd.readouterr()


class World:
    """A root checkout on its base, the `.sdlc` layer `work.start` reads, and the gestures a test
    drives it with."""

    def __init__(self, tmp_path):
        self.base = pathlib.Path(tmp_path).resolve()
        self.remote = self.base / "remote.git"
        self.root = self.base / "root"
        self.sdlc = self.root / ".sdlc"
        self.features = self.sdlc / "features"
        self.work = module("work")
        self._mods = {}
        # The ORIGINAL runner, captured before anything patches `work._run`: `cli` replaces that
        # attribute with `self.run`, which must not then call itself.
        self._git_run = self.work._run
        self.calls = []

    def mod(self, name):
        """A script loaded once per World: a loop of hundreds of writes must not re-execute a
        module chain per write."""
        if name not in self._mods:
            self._mods[name] = module(name)
        return self._mods[name]

    # -- construction ---------------------------------------------------------------------------
    def layout(self):
        """The `.sdlc` directories alone, no git: enough for a fake-runner check or a direct write."""
        (self.features / "units").mkdir(parents=True, exist_ok=True)
        (self.sdlc / "state").mkdir(parents=True, exist_ok=True)
        (self.sdlc / "config.json").write_text(json.dumps(CONFIG), encoding="utf-8")
        return self

    def build(self, units=("voice", "billing")):
        self.base.mkdir(parents=True, exist_ok=True)
        git(self.base, "init", "-q", "--bare", "-b", "main", str(self.remote))
        git(self.base, "init", "-q", "-b", "main", str(self.root))
        for key, value in (("user.email", "t@example.invalid"), ("user.name", "t"),
                           ("commit.gpgsign", "false"), ("core.autocrlf", "false")):
            git(self.root, "config", key, value)
        git(self.root, "remote", "add", "origin", str(self.remote))
        self.layout()
        (self.root / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
        (self.root / "seed.txt").write_text("seed\n", encoding="utf-8")
        git(self.root, "add", ".gitignore", "seed.txt", ".sdlc/config.json")
        git(self.root, "commit", "-q", "-m", "seed")
        git(self.root, "push", "-q", "-u", "origin", "main")
        for unit in units:
            git(self.root, "push", "-q", "origin", "main:refs/heads/feature/" + unit)
        return self

    # -- the runner -----------------------------------------------------------------------------
    def run(self, cwd, argv):
        """Real git; the one canned issue read; anything else is a test bug and raises."""
        argv = [str(a) for a in argv]
        self.calls.append(" ".join(argv))
        if argv[0] == "git":
            return self._git_run(cwd, argv)
        if argv[:2] == ["gh", "api"] and re.fullmatch(r"repos/%s/issues/\d+" % REPO, argv[2]):
            return json.dumps(ISSUE)
        raise RuntimeError("unexpected call in a #954 world: %s" % " ".join(argv))

    # -- the gestures ---------------------------------------------------------------------------
    def start(self, goal):
        """`work.start` with the absolute, resolved `.sdlc`, as `loop.py` and tests/test_work.py do."""
        return self.work.start(str(self.sdlc), CONFIG, str(goal), run=self.run)

    def cli(self, goal, monkeypatch, out):
        """THE DOCUMENTED GESTURE (skills/sigma-loop/SKILL.md step 3a): `work.py start .sdlc <goal>
        --session-pid <pid>` from the project root, so `main` hands the RELATIVE `.sdlc` to every
        writer and to the checker exactly as the CLI does. -> (exit code, what `main` printed).
        `out` is the test's `drained` handle; output from before this call is consumed first."""
        monkeypatch.chdir(self.root)
        monkeypatch.setattr(self.work, "_run", self.run)
        out.take()
        rc = self.work.main(["work.py", "start", ".sdlc", str(goal), "--session-pid", str(os.getpid())])
        printed, _ = out.take()
        return rc, printed.strip()

    def check(self, run=None):
        """`work._dirty_root_refusal` exactly as `start()` calls it, passing `sdlc_dir=` only where
        the signature has it. Signature-adaptive on purpose: against the old two-argument-plus-run
        function a keyword it does not take would be a TypeError, and a TypeError records no red."""
        fn = self.work._dirty_root_refusal
        kwargs = {}
        if "sdlc_dir" in inspect.signature(fn).parameters:
            kwargs["sdlc_dir"] = str(self.sdlc)
        return fn(self.work.project_root(str(self.sdlc)), self.work.settings(CONFIG),
                  run or self.run, **kwargs)

    def commit_registry(self):
        """§15's gesture, verbatim, through the shell."""
        proc = subprocess.run(COMMIT_GESTURE, shell=True, cwd=str(self.root),
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError("the registry commit failed: %s" % (proc.stderr or proc.stdout))

    def porcelain(self):
        """`git status --porcelain`, raw: NOT stripped, so line 1 keeps its leading space."""
        return git(self.root, "status", "--porcelain")

    def porcelain_v2(self):
        return git(self.root, "status", "--porcelain=v2", "--untracked-files=no")

    def v2_entry(self, rel):
        """The porcelain v2 line for `rel`, split into its fields, or None."""
        for line in self.porcelain_v2().splitlines():
            if line.endswith(" " + rel):
                return line.split(" ")
        return None

    def sigma_shard_write(self, goal, unit="voice"):
        """A Sigma shard write the way every pick makes one: `feature_sync.amend`, under the unit
        lock, appending `goal` under this repo."""
        sync = self.mod("feature_sync")

        def mutate(entry):
            repos = entry.setdefault("repos", {})
            mine = repos.setdefault(REPO, {"branch": "feature/" + unit, "owner": None,
                                           "authorized": False, "goals": []})
            mine["goals"] = list(mine.get("goals") or []) + [int(goal)]

        return sync.amend(str(self.sdlc), unit, mutate)

    def legacy_pick(self, goal, unit="voice"):
        """What a pick wrote into the ROOT checkout before #1941 (shard, then page). `work.start` no
        longer does; a root that still carries such dirt (an upgrade, a hand-run sync) is the state
        the dirty-root guard exists for, so its tests build that state with this."""
        self.sigma_shard_write(goal, unit)
        self.sigma_page_write(unit)

    def sigma_page_write(self, unit="voice"):
        """A Sigma page write the way `_sync_doc` makes one: `feature_doc.sync` from the registry."""
        doc = self.mod("feature_doc")
        registry = self.mod("feature_registry")
        entry = registry.read(registry.registry_dir(self.sdlc)).get(unit)
        return doc.sync(registry.registry_dir(self.sdlc), unit, entry)

    def read(self, rel):
        return (self.root / rel).read_bytes()

    def record(self, rel):
        """The chain record for a registry file `rel` (relative to `.sdlc/features/`)."""
        return self.root / RECORD_DIR / (rel + ".chain.json")
