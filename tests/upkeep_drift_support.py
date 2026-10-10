"""Support for the #919 upkeep drift, state and pin tests. No test lives here and nothing asserts at import time.

It loads the two new library modules BY PATH (never by import, so a missing module is an assertion inside a test and never
a collection error), rebuilds them with a source substitution for the mutation controls, builds throwaway git repositories
with `git fast-import` (which needs no identity, hooks or signing and lets author and committer dates be set apart; the
idents carry no e-mail address, which the repository's exposure scan would flag), and holds the injected runner the modules
take."""
import pathlib
import subprocess
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"
DAY = 86400
T0 = 1_700_000_000
DRIFT = "feature_upkeep_drift"
STATE = "feature_upkeep_state"
NO_DRIFT = "feature_upkeep_drift.py does not exist yet: the drift measure has not been written"
NO_STATE = "feature_upkeep_state.py does not exist yet: the per-unit state module has not been written"

_MEMO = {}


def at(day):
    """Epoch seconds `day` days after a fixed, realistic start (git's date parser misreads dates near 1970)."""
    return T0 + day * DAY


# ------------------------------------------------------------------------------------------ loading

def build(stem, src=None):
    path = SCRIPTS / (stem + ".py")
    namespace = {"__name__": stem + "_variant", "__file__": str(path)}
    exec(compile(path.read_text(encoding="utf-8") if src is None else src, str(path), "exec"), namespace)  # noqa: S102 - test-only
    return types.SimpleNamespace(**namespace)


def source(stem, message):
    path = SCRIPTS / (stem + ".py")
    assert path.is_file(), message
    return path.read_text(encoding="utf-8")


def _memo(stem, message):
    if stem not in _MEMO:
        _MEMO[stem] = build(stem, source(stem, message))
    return _MEMO[stem]


def drift():
    """The real drift measure, built once per process."""
    return _memo(DRIFT, NO_DRIFT)


def ustate():
    """The real per-unit state module, built once per process."""
    return _memo(STATE, NO_STATE)


def sibling(stem):
    """Any other loop script (the gate, the rebase engine), built once per process."""
    if stem not in _MEMO:
        _MEMO[stem] = build(stem)
    return _MEMO[stem]


def variant(stem, message, edits):
    """The module rebuilt with source substitutions [(old, new), ...]. Every target must exist and EVERY occurrence is
    replaced; each caller still asserts that the mutant behaves differently."""
    src = source(stem, message)
    for old, new in edits:
        assert old in src, "mutation target has drifted out of the source: %r" % (old,)
        src = src.replace(old, new)
    return build(stem, src)


def settings(**over):
    """The gate's flat default settings, with overrides spelled by dotted key through a dict: settings(**{"auto.floor": 1})."""
    flat = dict(sibling("feature_upkeep").DEFAULTS)
    flat.update(over)
    return flat


# ------------------------------------------------------------------------------------------ git

def run(cwd, argv):
    """The injected runner: the contract of `feature_sync._run` (stdout stripped, an exception on a non-zero exit)."""
    proc = subprocess.run([str(a) for a in argv], cwd=str(cwd), capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "").strip() or "%s exited %s" % (argv[0], proc.returncode))
    return (proc.stdout or "").strip()


class Recorder:
    """A runner that records every argv it is handed and delegates to `run`."""

    def __init__(self, inner=run):
        self.calls = []
        self.inner = inner

    def __call__(self, cwd, argv):
        self.calls.append(list(argv))
        return self.inner(cwd, argv)


def commit(ref, mark, subject, author, committer=None, parents=None):
    """One commit for `repo`: `parents` is a list of marks; None means the previous commit on the same ref."""
    return {"ref": ref, "mark": mark, "subject": subject, "author": author,
            "committer": author if committer is None else committer, "parents": parents}


def linear(count, ref="main", first=1, day=lambda i: i, subject=lambda i: "Change %d (#%d)" % (i, 100 + i)):
    """`count` commits on `ref`, marks `first` onwards, each authored on day(i) with a pull-request number in its subject."""
    return [commit(ref, first + i - 1, subject(i), at(day(i))) for i in range(1, count + 1)]


def repo(path, commits):
    """A throwaway repository holding `commits`, built in ONE `git fast-import`. -> the path."""
    path = pathlib.Path(path)
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "--initial-branch=main", str(path)], check=True, capture_output=True)
    lines, last = [], {}
    for c in commits:
        data = c["subject"].encode("utf-8")
        lines += ["commit refs/heads/%s" % c["ref"], "mark :%d" % c["mark"],
                  "author A <> %d +0000" % c["author"],
                  "committer C <> %d +0000" % c["committer"], "data %d" % len(data), c["subject"]]
        parents = [last[c["ref"]]] if c["parents"] is None and c["ref"] in last else list(c["parents"] or [])
        if parents:
            lines.append("from :%d" % parents[0])
            lines += ["merge :%d" % p for p in parents[1:]]
        lines.append("")
        last[c["ref"]] = c["mark"]
    subprocess.run(["git", "-C", str(path), "fast-import", "--quiet"], input="\n".join(lines) + "\n", text=True,
                   check=True, capture_output=True)
    return path


def skewed(path):
    """40 linear commits authored on days 1..40; commit 30 keeps its author day but was COMMITTED years earlier. With
    now = day 40 and a 15-day window the author-date count is 16 (days 25..40); `git log --since` stops at commit 30 and
    reports 10."""
    commits = linear(40)
    commits[29]["committer"] = at(-3000)
    return repo(path, commits)


def disordered(path):
    """The same 40 commits, but commit 30 was AUTHORED years earlier (committer day 30): the author-date count is 15,
    and a walk that stops at the first old author date reports 10."""
    commits = linear(40)
    commits[29]["author"] = at(-3000)
    commits[29]["committer"] = at(30)
    return repo(path, commits)


def rev(path, ref):
    return run(path, ["git", "rev-parse", ref])


# ------------------------------------------------------------------------------------------ checks

def expect(bad, label, got, want):
    """Collect a failure instead of raising, so one assertion can report every failing case."""
    if got != want:
        bad.append("%s: got %r, want %r" % (label, got, want))
