"""#2023: a rate-limited or incomplete backlog read prints `DEGRADED <reset-epoch>`, never `DONE`, and every
consumer of the pick token handles it. Zero network: fake sources and fake runners only.

Controls (each run alone, then restored): printing DONE instead of DEGRADED; run_loop without the DEGRADED
branch (the goal slot would receive the epoch as a goal).
"""
import importlib.util
import json
import pathlib
import sys
import tempfile
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
S = ROOT / "skills" / "sigma-loop" / "scripts"
sys.path.insert(0, str(ROOT / "tools"))


def _mod(name, base=S):
    spec = importlib.util.spec_from_file_location(name + "_t2023", base / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _backlog(d, n=0):
    base = pathlib.Path(d) / ".sdlc"
    (base / "goals").mkdir(parents=True)
    (base / "state").mkdir()
    (base / "config.json").write_text(json.dumps({"budget": {"max_iterations": 10}}))
    (base / "state" / "STATE.md").write_text("iteration: 0\nrun_iteration: 0\nlast_run: none\n")
    (base / "state" / "review-queue.md").write_text("# Q\n")
    for i in range(1, n + 1):
        (base / "goals" / f"{i:04d}.md").write_text(f"---\nid: {i:04d}\nstatus: pending\n---\nx\n")
    return str(base)


class Degraded:
    def next_pending(self, skip=()):
        return None

    def read_degraded(self):
        return True


@pytest.fixture(autouse=True)
def _quota_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGMA_RESOURCE_DIR", str(tmp_path / "quota"))
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("CODEX_SESSION_ID", raising=False)


def _cli(lp, base, verb, capsys, source):
    lp.sources.get_source = lambda sdlc_dir, config: source
    lp.state.start_run(base)
    assert lp.main(["loop.py", verb, base]) == 0
    return capsys.readouterr()


# --- the printed token ------------------------------------------------------------------------------------

def test_next_prints_degraded_epoch_not_done(capsys):
    with tempfile.TemporaryDirectory() as d:
        out = _cli(_mod("loop"), _backlog(d), "next", capsys, Degraded())
    lines = out.out.split()
    assert lines[0] == "DEGRADED" and lines[1].isdigit() and int(lines[1]) > time.time() - 5
    assert "DONE" not in out.out


def test_next_batch_prints_degraded_epoch_not_done(capsys):
    with tempfile.TemporaryDirectory() as d:
        out = _cli(_mod("loop"), _backlog(d), "next-batch", capsys, Degraded())
    first = out.out.strip().splitlines()[-1].split()
    assert first[0] == "DEGRADED" and first[1].isdigit()
    assert "DONE" not in out.out


def test_genuinely_empty_backlog_is_still_done(capsys):
    with tempfile.TemporaryDirectory() as d:
        lp = _mod("loop")
        base = _backlog(d)
        lp.state.start_run(base)
        assert lp.main(["loop.py", "next", base]) == 0
    assert capsys.readouterr().out.strip() == "DONE"


def test_epoch_is_the_recorded_refusals_reset_when_one_is_held():
    lp, quota = _mod("loop"), _mod("gh_quota")
    now = time.time()
    quota.note_refusal(None, "HTTP 429: rate limit\nretry-after: 600\n", now=now)
    got = lp._degraded_epoch(now=now)
    assert abs(got - (now + 600)) <= 1


def test_epoch_without_a_refusal_is_the_bounded_default():
    lp, quota = _mod("loop"), _mod("gh_quota")
    now = 1_800_000_000
    assert lp._degraded_epoch(now=now) == now + quota.DEFAULT_PRIMARY


# --- run_loop consumer ------------------------------------------------------------------------------------

class Flaky:
    """Degraded for `bad` reads, then a drained backlog."""

    def __init__(self, bad):
        self.bad, self.reads = bad, 0

    def next_pending(self, skip=()):
        self.reads += 1
        return None

    def read_degraded(self):
        return self.reads <= self.bad


def _drive(bad, monkeypatch):
    lp = _mod("loop")
    waits, ran = [], []
    monkeypatch.setattr(lp, "wait_degraded", lambda epoch, **kw: waits.append(epoch) or 0)
    src = Flaky(bad)
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d)
        monkeypatch.setattr(lp.sources, "get_source", lambda sdlc_dir, config: src)
        res = lp.run_loop(base, lambda g: ran.append(g) or ("done", ""))
    return res, waits, ran


def test_run_loop_waits_once_then_picks_again(monkeypatch):
    res, waits, ran = _drive(1, monkeypatch)
    assert len(waits) == 1 and isinstance(waits[0], int)
    assert ran == [] and res["stopped"] == "backlog-empty"      # DEGRADED was never run as a goal


def test_run_loop_second_degraded_stops_honestly(monkeypatch):
    res, waits, ran = _drive(5, monkeypatch)
    assert len(waits) == 1 and ran == [] and res["stopped"] == "degraded"


# --- the bounded wait verb --------------------------------------------------------------------------------

class Clock:
    def __init__(self, t):
        self.t, self.slept = t, []

    def now(self):
        return self.t

    def sleep(self, secs):
        self.slept.append(secs)
        self.t += secs


def test_wait_degraded_sleeps_until_the_epoch():
    lp, c = _mod("loop"), Clock(1000)
    assert lp.wait_degraded(1090, now=c.now, sleep=c.sleep) == 90 and c.slept == [90]


def test_wait_degraded_is_capped_at_thirty_minutes():
    lp, c = _mod("loop"), Clock(1000)
    assert lp.wait_degraded(1000 + 86400, now=c.now, sleep=c.sleep) == 1800 and c.slept == [1800]


def test_wait_degraded_past_epoch_does_not_sleep():
    lp, c = _mod("loop"), Clock(1000)
    assert lp.wait_degraded(900, now=c.now, sleep=c.sleep) == 0 and c.slept == []


def test_wait_degraded_refuses_a_non_epoch():
    lp = _mod("loop")
    with pytest.raises(ValueError):
        lp.wait_degraded("soon")
    assert lp.main(["loop.py", "wait-degraded", "soon"]) == 2


# --- supervise_classify consumer ---------------------------------------------------------------------------

class _Rng:
    def randint(self, lo, hi):
        return lo


def test_supervise_treats_degraded_as_a_bounded_sleep_not_done():
    sc, now = _mod("supervise_classify"), 1_800_000_000
    action, wait, reason = sc.classify("DEGRADED %d\n" % (now + 240), now=now, rng=_Rng())
    assert action == "sleep" and wait == 240 and "degraded" in reason


def test_supervise_clamps_the_degraded_wait():
    sc, now = _mod("supervise_classify"), 1_800_000_000
    assert sc.classify("DEGRADED %d" % (now + 999999), now=now, rng=_Rng())[1] == 1800
    assert sc.classify("DEGRADED %d" % (now - 5), now=now, rng=_Rng())[1] == 30


# --- chain consumer ----------------------------------------------------------------------------------------

def test_chain_default_pick_raises_a_rate_limit_stuck_not_an_empty_pick(monkeypatch):
    ch = _mod("chain")

    class P:
        returncode, stdout, stderr = 0, "DEGRADED 1800000300\n", ""

    monkeypatch.setattr(ch.subprocess, "run", lambda *a, **k: P)
    with pytest.raises(RuntimeError) as err:
        ch._default_pick("/x/.sdlc", "lbl", "1", set())
    assert ch._why_failed(err.value) == "rate-limit"


def test_chain_no_pick_set_names_degraded():
    assert "DEGRADED" in _mod("chain")._NO_PICK


# --- onboarding_control consumer ---------------------------------------------------------------------------

def test_onboarding_control_is_red_on_a_degraded_pick():
    oc = _mod("onboarding_control", ROOT / "tools")
    with pytest.raises(oc.Red) as err:
        oc.refuse_degraded_pick("DEGRADED 1800000300")
    assert "rate-limited" in str(err.value) or "rate-limited" in repr(err.value.args)
    oc.refuse_degraded_pick("DONE")
    oc.refuse_degraded_pick(".sdlc/goals/0002-x.md")


# --- sigma-scope assign consumer ---------------------------------------------------------------------------

def test_assign_start_drain_serialises_a_degraded_pick(tmp_path):
    assign = _mod("assign", ROOT / "skills" / "sigma-scope" / "scripts")
    base = _backlog(tmp_path)
    got = assign._start_drain(base, {}, Degraded())
    assert got["picked_kind"] == "DEGRADED" and isinstance(got["picked"], int)
    json.dumps(got)


# --- no extra GitHub call ----------------------------------------------------------------------------------

def test_degraded_epoch_makes_no_github_call(monkeypatch):
    lp = _mod("loop")

    def boom(*a, **k):
        raise AssertionError("a GitHub call was made")

    monkeypatch.setattr(lp.sources, "_run_gh", boom)
    lp._degraded_epoch(now=1_800_000_000)


# --- docs name the token in every consumer text -------------------------------------------------------------

@pytest.mark.parametrize("rel", ["SKILL.md", "references/picking.md", "references/stopping.md"])
def test_loop_skill_texts_name_degraded_and_the_wait_verb(rel):
    text = (ROOT / "skills" / "sigma-loop" / rel).read_text(encoding="utf-8")
    assert "DEGRADED" in text and "wait-degraded" in text, rel
