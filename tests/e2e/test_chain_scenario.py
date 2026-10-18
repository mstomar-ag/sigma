"""#1595: the Sigma Chain, end to end, on a scratch scenario (offline: a stateful fake `gh`, no network).

Everything is driven through `chain.main([...], run=fake, now=NOW, pick=pick)` argv, the verbs docs/chain.md
documents. The backlog: end goal 1 waits on 2, 2 on 3, 3 on 4. Goal 4 (deepest) is parked, goal 2 is unarmed,
goal 3 carries another unit (a cross-unit edge). Goal 9 is an armed goal outside the chain. A prerequisite,
goal 5, is filed after the first run cycle and blocks 3. Members close deepest first, respecting waits.

Controls (each clause broken alone, the scenario helper then FAILS; each was seen red when written):
  1 exclusive pool   `pick_argv` drops the label scope -> an outside armed goal is claimed
  2 STUCK not DONE   the done clause ignores open members -> an empty pick reads as done
  3 fail-closed cap  `admit` treats an unreadable ledger as empty -> the run proceeds past the cap
  4 no write before approval  `_require_approved` allows everything -> arm writes before approve
"""
import contextlib
import importlib.util
import io
import json
import pathlib
import re
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import test_chain_arm as tca  # noqa: E402
import test_label_answer as tla  # noqa: E402
from test_label_drain_arm import DW, S, _repo  # noqa: E402

NOW = tca.NOW
DOC = pathlib.Path(__file__).resolve().parents[2] / "docs" / "chain.md"
GESTURE_HEADING = "The scratch scenario gesture"
KIND = "Parked by Sigma: this branch conflicts with the base branch.\nsigma-qkind: merge_conflict"
CLOSE_ORDER = [4, 5, 3, 2, 1]


def _fresh(name="chain"):
    return tca._mod(name)


def _broken(name, old, new):
    """-> the module `name` with exactly one clause of its source replaced. The break is in the real code."""
    path = S / (name + ".py")
    src = path.read_text(encoding="utf-8")
    assert src.count(old) == 1, "the clause to break is not in %s exactly once" % name
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    exec(compile(src.replace(old, new), str(path), "exec"), mod.__dict__)
    return mod


def _memo(mod):
    """Load each sibling script once per module instance (the engine reloads them on every call; that is
    seconds of compiling per cycle in a bytecode-free run). Behaviour is unchanged."""
    orig, cache = mod._load, {}
    mod._load = lambda name: cache[name] if name in cache else cache.setdefault(name, orig(name))


def _backlog():
    return [tla.issue(1, "sdlc:goal", "feature:alpha", assignees=("alice",), body="Blocked by #2\n\n" + DW),
            tla.issue(2, "feature:alpha", body="Blocked by #3\n\n" + DW),
            tla.issue(3, "feature:beta", body="Blocked by #4\n\n" + DW),
            tla.issue(4, "sdlc:goal", "sdlc:parked", "feature:beta", body=DW, comments=[KIND]),
            tla.issue(9, "sdlc:goal", assignees=("alice",), body=DW)]


class Pick:
    """The loop's scoped pick as the driver sees it: label scoped when a label is given, leased, one claim per
    cycle, in the record's order, and a member waits while any goal its live body names is open."""

    def __init__(self, fk, base):
        self.fk, self.base = fk, base
        self.claims, self.labels = [], []

    def __call__(self, label, skip, **_kw):
        self.labels.append(label)
        rec = json.loads(pathlib.Path(self.base, "state", "chains", "c1.json").read_text())
        order = [r["number"] for r in rec["members"]]
        order += [n for n in self.fk.by if n not in order]
        for n in order:
            issue = self.fk.by[n]
            names = {x["name"] for x in issue["labels"]}
            waits = [int(w) for w in re.findall(r"Blocked by #(\d+)", issue["body"])]
            if issue["state"] == "closed" or (label and label not in names) or n in self.claims \
                    or str(n) in skip or "sdlc:goal" not in names or names & {"story", "sdlc:parked"} \
                    or any(self.fk.by[w]["state"] != "closed" for w in waits):
                continue
            self.claims.append(n)
            return [n]
        return []


class Scenario:
    """One scratch run. `step` is the only way in: an argv through `chain.main`, never an engine function."""

    def __init__(self, tmp_path, ch=None, issues=None, argv_pick=False):
        self.ch = ch or _fresh()
        _memo(self.ch)
        _memo(self.ch.label_drain)
        self.ch._default_sweep = lambda *_a: ()          # the loop's own sweep starts a process; none here
        self.base = _repo(tmp_path, "alice")
        cp = pathlib.Path(self.base) / "config.json"
        cfg = json.loads(cp.read_text())
        cfg["discovery"]["dag"] = {"mode": "off"}
        cp.write_text(json.dumps(cfg))
        self.cfg_path, self.tmp = cp, pathlib.Path(tmp_path)
        self.fk = tca.ChainFake(issues or _backlog())
        self.pick = Pick(self.fk, self.base)
        self.argv_pick = argv_pick
        self.gestures, self.out = [], ""
        self.rc = None

    def step(self, verb, *rest, log=True, **kw):
        argv = ["chain.py", verb, self.base, "1", *rest]
        if verb == "run" and self.argv_pick:
            kw.pop("pick", None)
        else:
            kw.setdefault("pick", self.pick if verb == "run" else None)
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            self.rc = self.ch.main(argv, run=self.fk, now=NOW + len(self.gestures), **kw)
        self.out = buf.getvalue() + err.getvalue()
        if log:
            shown = ["<sdlc_dir>" if a == self.base else a for a in argv[1:]]
            shown = ["<file>" if a.endswith("answers.json") else a for a in shown]
            line = "chain.py " + " ".join(shown)
            if not self.gestures or self.gestures[-1] != line:
                self.gestures.append(line)
        return self.rc

    def record(self):
        return json.loads(pathlib.Path(self.base, "state", "chains", "c1.json").read_text())

    def close(self, *nums):
        for n in nums:
            self.fk.by[n]["state"] = "closed"

    def to_arm(self):
        t0 = time.perf_counter()
        before = len(self.fk.calls)
        assert self.step("plan") == 0, self.out
        self.plan_calls = len(self.fk.calls) - before
        assert self.step("sheet") == 0, self.out
        self.sheet_seconds = time.perf_counter() - t0
        assert re.search(r"\[cross_unit:\d+-\d+\]", self.out), "the sheet asks no cross-unit question"
        for needle in ("[merge_conflict:4]", "[cap:c1]"):
            assert needle in self.out, "the sheet does not ask %s:\n%s" % (needle, self.out)
        # the probe: nothing writes before approval (not part of the documented sequence)
        assert self.step("arm", log=False) == 2 and self.fk.writes() == [] and self.fk.created == [], \
            "arm wrote before approval: %s" % self.fk.writes()[:2]
        assert self.step("run", log=False) == 2 and self.fk.writes() == []
        assert self.step("answer", "--defaults") == 0, self.out
        answers = self.tmp / "answers.json"
        answers.write_text(json.dumps({"cap:c1": "10"}))
        assert self.step("answer", "--answers", str(answers)) == 0, self.out
        assert self.step("approve") == 0, self.out
        # the only write so far is the parked member's unpark answer; no chain label exists yet
        assert self.fk.created == [] and not [w for w in tca._writes(self.fk) if w[2] == "chain:1"], self.fk.writes()
        assert self.step("arm") == 0, self.out

    def cycle(self, want_rc, want_state, want_picked=None):
        assert self.step("run") == want_rc, (want_rc, self.rc, self.out)
        run = self.record()["run"]
        assert run["state"] == want_state, (want_state, run, self.out)
        return run


def scenario(tmp_path, ch=None, argv_pick=False, drive_pick=None):
    """-> an outcome dict that must be identical on every run. Raises AssertionError when any step is off."""
    sc = Scenario(tmp_path, ch, argv_pick=argv_pick)
    sc.to_arm()
    if argv_pick:
        drive_pick(sc)
    closed, picks = [], []

    def claimed():
        return list(sc.pick.claims)

    sc.cycle(0, "running")
    assert claimed() == [4], claimed()
    # a prerequisite is filed mid-run: new goal 5 blocks 3
    sc.fk.by[5] = tla.issue(5, "sdlc:goal", "feature:beta", body=DW)
    sc.fk.by[3]["body"] = "Blocked by #4\nBlocked by #5\n\n" + DW
    sc.cycle(0, "running")
    assert 5 in sc.record()["run"].get("joined", []) and claimed() == [4, 5], (claimed(), sc.record()["run"])
    # both workers are still busy: an empty pick with open members is STUCK, never DONE
    run = sc.cycle(3, "stuck")
    assert run["stuck"] == "claimed", run
    for n in (4, 5):
        sc.close(n)
        closed.append(n)
    for want in (3, 2, 1):
        sc.cycle(0, "running")
        assert claimed()[-1] == want, claimed()
        sc.close(want)
        closed.append(want)
    sc.cycle(0, "done")
    assert closed == CLOSE_ORDER and claimed() == [4, 5, 3, 2, 1], claimed()
    assert 9 not in claimed(), "a goal outside the chain label was claimed"
    assert set(sc.pick.labels) == {"chain:1"}, sc.pick.labels
    assert sc.step("run") == 0 and sc.record()["run"]["state"] == "done"
    assert sc.step("status") == 0 and "done" in sc.out, sc.out
    labelled = {n for n in sc.fk.by if "chain:1" in sc.fk._names(n)}
    assert labelled == {1, 2, 3, 4, 5}, labelled
    assert "sdlc:goal" not in sc.fk._names(9) or sc.fk._names(9) >= {"sdlc:goal"}
    return {"order": claimed(), "closed": closed, "gestures": sc.gestures, "plan_calls": sc.plan_calls,
            "writes": [list(w) for w in sc.fk.writes()], "calls": len(sc.fk.calls),
            "state": sc.record()["run"]["state"], "cycles": sc.record()["run"]["cycle"]}


# --- the scenario ---------------------------------------------------------------------------------------------

def test_scenario_end_to_end(tmp_path):
    out = scenario(tmp_path)
    assert out["order"] == [4, 5, 3, 2, 1] and out["state"] == "done"
    assert out["plan_calls"] > 0


def test_scenario_deterministic_three_runs(tmp_path):
    runs = []
    for i in range(3):
        d = tmp_path / ("r%d" % i)
        d.mkdir()
        runs.append(scenario(d))
    assert runs[0] == runs[1] == runs[2]
    assert runs[0]["writes"], "the write log is empty: the comparison proves nothing"


def test_scenario_default_pick_argv(tmp_path, monkeypatch):
    """The same scenario through the engine's own default pick: its argv is answered by the scoped fake."""
    seen = []

    def drive(sc):
        def fake_run(argv, **_kw):
            seen.append(list(argv))
            label = argv[argv.index("--label") + 1] if "--label" in argv else None
            skip = set(argv[argv.index("--skip") + 1].split(",")) if "--skip" in argv else set()
            got = sc.pick(label, skip)
            return subprocess.CompletedProcess(argv, 0, stdout="".join("%d\n" % n for n in got), stderr="")
        monkeypatch.setattr(subprocess, "run", fake_run)

    out = scenario(tmp_path, argv_pick=True, drive_pick=drive)
    assert out["order"] == [4, 5, 3, 2, 1] and all("--label" in a for a in seen), seen[:1]


# --- the four controls ----------------------------------------------------------------------------------------

def test_control_pool_exclusive(tmp_path, monkeypatch):
    """pick_argv without `--label`: the pick is not scoped and claims the outside armed goal 9."""
    ch = _fresh()
    real = ch.pick_argv

    def unscoped(*a, **k):
        argv = real(*a, **k)
        i = argv.index("--label")
        return argv[:i] + argv[i + 2:]
    ch.pick_argv = unscoped
    with pytest.raises(AssertionError):
        scenario(tmp_path, ch=ch, argv_pick=True, drive_pick=lambda sc: _argv_driver(sc, monkeypatch))


def _argv_driver(sc, monkeypatch):
    def fake_run(argv, **_kw):
        label = argv[argv.index("--label") + 1] if "--label" in argv else None
        skip = set(argv[argv.index("--skip") + 1].split(",")) if "--skip" in argv else set()
        got = sc.pick(label, skip)
        return subprocess.CompletedProcess(argv, 0, stdout="".join("%d\n" % n for n in got), stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)


def test_control_stuck_not_done(tmp_path):
    """The done clause drops its open-member checks: the busy cycle reads as done."""
    ch = _broken("chain", 'if failure is None and not open_now and not unconfirmed and not blk.get("paused"):',
                 "if failure is None:")
    with pytest.raises(AssertionError):
        scenario(tmp_path, ch=ch)


def _cap_scenario(tmp_path, ch=None, spend=None):
    """Cap set, ledger unreadable: the first cycle must refuse (exit 3, capped) and claim nothing."""
    sc = Scenario(tmp_path, ch)
    sc.to_arm()
    cfg = json.loads(sc.cfg_path.read_text())
    cfg["chain"] = {"max_usd": 50}
    sc.cfg_path.write_text(json.dumps(cfg))
    (pathlib.Path(sc.base) / "events").write_text("not a directory\n")          # present, unreadable as a ledger
    if ch is not None and spend is not None:
        orig = ch._load
        ch._load = lambda name: spend if name == "chain_spend" else orig(name)
    assert sc.step("run") == 3, "the run proceeded past a cap with an unreadable ledger: %s" % sc.out
    run = sc.record()["run"]
    assert run["state"] == "capped" and "unreadable" in run["reason"], run
    assert sc.pick.claims == [], sc.pick.claims
    return run


def test_cap_fail_closed_green(tmp_path):
    assert _cap_scenario(tmp_path)["state"] == "capped"


def test_control_cap_fail_closed(tmp_path):
    """`admit` reads an unreadable ledger as an empty one."""
    spend = _broken("chain_spend", 'if not ok:\n        return False, "capped: the spend ledger is unreadable and a cap is set"',
                    "if False:\n        return False, ''")
    with pytest.raises(AssertionError):
        _cap_scenario(tmp_path, ch=_fresh(), spend=spend)


def test_control_no_write_before_approval(tmp_path):
    """The approved-state gate allows everything: arm writes before approve."""
    ch = _fresh()
    ch._require_approved = lambda rec, config: None
    with pytest.raises(AssertionError):
        scenario(tmp_path, ch=ch)


# --- the docs --------------------------------------------------------------------------------------------------

def _documented_gesture():
    text = DOC.read_text(encoding="utf-8")
    assert "## " + GESTURE_HEADING in text
    after = text.split("## " + GESTURE_HEADING, 1)[1]
    block = re.search(r"```\n(.*?)```", after, re.S).group(1)
    return [l.strip() for l in block.splitlines() if l.strip()]


def test_gesture_copied_from_docs(tmp_path):
    out = scenario(tmp_path)
    assert out["gestures"] == _documented_gesture()
    assert all(re.match(r"chain\.py (plan|sheet|answer|approve|arm|run|status) <sdlc_dir> 1", g)
               for g in out["gestures"])


def test_docs_measure_section_present(tmp_path):
    sc = Scenario(tmp_path)
    sc.to_arm()
    assert sc.sheet_seconds < 30
    text = DOC.read_text(encoding="utf-8")
    assert "## Measured on the scratch scenario" in text
    sect = text.split("## Measured on the scratch scenario", 1)[1].split("\n## ", 1)[0]
    assert "GitHub calls for `plan`: %d" % sc.plan_calls in sect, (sc.plan_calls, sect)
    assert "not meaningful against a live host" in sect and "not measured live" in sect
    assert "Estimate versus actual spend: not measured" in sect
    assert "Not measured: a run on a live repository or a private clone." in sect
