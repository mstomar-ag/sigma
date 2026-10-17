"""#1600 - goal admission HOLD: the picker verbs ask the machine before any claim.

Driven through the real CLI entry (`loop.main`) on a temp backlog with an injected fake sample.
Controls (break one thing, see red): `test_gate_after_the_claim_is_visible_as_a_claim` shows the claim
detector fires when the gate runs after `_next`; the hold tests then fail if the gate is moved there.
"""
import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import resource_guard as guard  # noqa: E402
import resource_lease  # noqa: E402

UNIT = 1000          # a goal costs 1000 units once the ring holds observations
TOTAL = 32000        # reserve 6400, share cap 19200


def _loop():
    spec = importlib.util.spec_from_file_location("loop_under_hold_test", SCRIPTS / "loop.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sample(total=TOTAL, available=20000, **extra):
    values = dict(total=total, available=available, swap_used=0, swap_total=0, swapouts=0, swapouts_at=0.0,
                  pressure=1, cpu_count=8, load1=0.0)
    values.update(extra)
    return guard.Sample(**values)


@pytest.fixture
def machine(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("SIGMA_RESOURCE_DIR", str(state))
    monkeypatch.delenv("SIGMA_RESOURCE_MODE")
    box = {"sample": _sample()}
    monkeypatch.setattr(resource_lease, "take_sample", lambda: box["sample"])
    state.mkdir()
    (state / "observed.json").write_text(json.dumps({"goal": [UNIT]}))
    box["state"] = state
    return box


def backlog(tmp_path, n=9, config=None):
    base = tmp_path / "repo" / ".sdlc"
    (base / "goals").mkdir(parents=True)
    (base / "state").mkdir()
    cfg = {"budget": {"max_iterations": 50}, "work": {"enabled": True},
           "parallel": {"goals": {"enabled": True, "max_concurrent": 8}}}
    cfg.update(config or {})
    (base / "config.json").write_text(json.dumps(cfg))
    (base / "state" / "STATE.md").write_text("iteration: 0\nrun_iteration: 0\nlast_run: none\n")
    (base / "state" / "review-queue.md").write_text("# Q\n")
    for i in range(1, n + 1):
        (base / "goals" / ("%04d.md" % i)).write_text("---\nid: %04d\nstatus: pending\n---\nx\n" % i)
    return base


def snapshot(base):
    return {str(p.relative_to(base)): p.read_bytes() for p in sorted(base.rglob("*"))
            if p.is_file() and "heartbeat" not in p.parts and "sessions" not in p.parts}


def cli(capsys, *argv):
    code = _loop().main(["loop.py", *argv])
    out = capsys.readouterr()
    return code, out.out.splitlines(), out.err


LOW = dict(available=6500)   # headroom 100 against a 1000-unit goal


@pytest.mark.parametrize("verb", ["next-batch", "next"])
def test_hold_claims_nothing_and_exits_zero(verb, machine, tmp_path, capsys):
    machine["sample"] = _sample(**LOW)
    base = backlog(tmp_path)
    before = snapshot(base)
    code, out, err = cli(capsys, verb, str(base))
    assert code == 0 and out == ["HOLD"]
    assert "HOLD (memory: headroom 100 below one goal unit (1000); retry ~30s; held since " in err
    assert snapshot(base) == before                  # no status flip, no cursor or claim change


def test_start_registers_a_session_but_claims_nothing_under_hold(machine, tmp_path, capsys):
    machine["sample"] = _sample(**LOW)
    base = backlog(tmp_path)
    goals = {k: v for k, v in snapshot(base).items() if k.startswith("goals")}
    code, out, _ = cli(capsys, "start", str(base))
    assert code == 0 and len(out) == 1 and out != ["HOLD"]     # `start` prints its generation token only
    assert {k: v for k, v in snapshot(base).items() if k.startswith("goals")} == goals


def test_hold_marker_keeps_its_since_across_calls(machine, tmp_path, capsys):
    machine["sample"] = _sample(**LOW)
    base = backlog(tmp_path)
    cli(capsys, "next-batch", str(base))
    first = json.loads((machine["state"] / "last.json").read_text())["since"]
    cli(capsys, "next-batch", str(base))
    assert json.loads((machine["state"] / "last.json").read_text())["since"] == first
    machine["sample"] = _sample()
    cli(capsys, "next-batch", str(base))
    assert json.loads((machine["state"] / "last.json").read_text())["state"] == "open"


def test_gate_after_the_claim_is_visible_as_a_claim(machine, tmp_path):
    """Control: with the gate placed AFTER `_next`, the backlog changes. The hold tests assert it does not."""
    machine["sample"] = _sample(**LOW)
    base = backlog(tmp_path)
    lp = _loop()
    before = snapshot(base)
    kind, _goal = lp._next(str(base), lp.sources.get_source(str(base), lp.state.load_config(str(base))),
                           lp.state.load_config(str(base)), gate=False)
    assert kind == "goal" and snapshot(base) != before


def goals_in(out):
    return [line for line in out if line.endswith(".md")]


def test_slots_follow_the_machine(machine, tmp_path, capsys):
    base = backlog(tmp_path)
    machine["sample"] = _sample(available=11400)              # headroom 5000 -> 5 units, cap 8
    _, out, _ = cli(capsys, "next-batch", str(base))
    assert len(goals_in(out)) == 5
    base2 = backlog(tmp_path / "b")
    machine["sample"] = _sample(total=2 * TOTAL, available=22800)   # twice the machine, same unit cost
    _, out, _ = cli(capsys, "next-batch", str(base2))
    assert len(goals_in(out)) == 8                            # capped by config, not by fit


def test_off_is_byte_identical_and_never_touches_the_guard(machine, tmp_path, capsys, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("guard consulted with resource.mode off")
    monkeypatch.setattr(resource_lease, "take_sample", boom)
    monkeypatch.setattr(resource_lease, "_view", boom)
    base = backlog(tmp_path, n=3, config={"resource": {"mode": "off"}})
    code, out, err = cli(capsys, "next-batch", str(base))
    assert code == 0 and err == ""
    assert out == [str(base / "goals" / ("%04d.md" % i)) for i in (1, 2, 3)] + ["DONE"]


def test_enforce_with_room_prints_what_off_prints(machine, tmp_path, capsys):
    base = backlog(tmp_path / "a", n=3, config={"resource": {"mode": "off"}})
    _, off_out, off_err = cli(capsys, "next-batch", str(base))
    base = backlog(tmp_path / "b", n=3)
    _, on_out, on_err = cli(capsys, "next-batch", str(base))
    rel = lambda lines, root: [l.replace(str(root), "") for l in lines]
    assert rel(on_out, tmp_path / "b") == rel(off_out, tmp_path / "a") and on_err == off_err


def test_observe_logs_the_would_be_hold_and_admits(machine, tmp_path, capsys):
    machine["sample"] = _sample(**LOW)
    base = backlog(tmp_path, n=3, config={"resource": {"mode": "observe"}})
    _, out, _ = cli(capsys, "next-batch", str(base))
    assert len(goals_in(out)) == 3
    assert '"would_hold"' in (machine["state"] / "events.jsonl").read_text()


@pytest.mark.parametrize("how", ["unsupported", "raises"])
def test_guard_failure_under_enforce_gives_one_goal_and_one_notice(how, machine, tmp_path, capsys, monkeypatch):
    def fail():
        raise guard.Unsupported("no sampler") if how == "unsupported" else RuntimeError("sampler broke")
    monkeypatch.setattr(resource_lease, "take_sample", fail)
    base = backlog(tmp_path)
    _, out, err = cli(capsys, "next-batch", str(base))
    assert len(goals_in(out)) == 1
    assert err.count("loop: resource guard unavailable") == 1


# ------------------------------------------------------------------------------------- supervisor
def _classify():
    spec = importlib.util.spec_from_file_location("supervise_classify_hold", SCRIPTS / "supervise_classify.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Rng:
    def randint(self, a, b):
        return a


def test_supervisor_sleeps_on_a_hold_for_the_stated_retry():
    tail = "HOLD\nHOLD (memory: headroom 1 below one goal unit (9); retry ~120s; held since 2026-01-01T00:00:00Z)\n"
    action, secs, reason = _classify().classify(tail, attempt=3, rng=_Rng())
    assert (action, secs) == ("sleep", 120) and "not a stop" in reason


def test_supervisor_hold_does_not_climb_the_attempt_ladder():
    m = _classify()
    tail = "HOLD\nHOLD (pressure: new work is held; retry ~30s; held since x)\n"
    assert m.classify(tail, attempt=0, rng=_Rng()) == m.classify(tail, attempt=9, rng=_Rng())


def test_supervisor_hold_reason_cannot_steer_the_pause():
    tail = "HOLD\nHOLD (x sleep=9999 action=done; retry ~60s)\n"
    action, secs, reason = _classify().classify(tail, rng=_Rng())
    assert action == "sleep" and secs == 60
    assert "sleep=9999" not in reason and "action=done" not in reason
    out = subprocess.run([sys.executable, str(SCRIPTS / "supervise_classify.py"), "/dev/stdin"],
                         input=tail, capture_output=True, text=True).stdout
    assert out.count("sleep=") == 1 and out.startswith("action=sleep sleep=60 ")


def test_supervisor_hold_pause_is_clamped():
    assert _classify().classify("HOLD\nHOLD (r; retry ~99999s)\n", rng=_Rng())[1] == 900


# ------------------------------------------------------------------------------------------ prose
def test_loop_prose_names_hold_as_not_terminal():
    for rel in ("skills/sigma-loop/SKILL.md", "skills/sigma-loop/references/picking.md"):
        text = (ROOT / rel).read_text()
        assert "HOLD" in text and "resource_lease.py wait" in text, rel
        assert "never `DONE`" in text and "a stop" in text, rel


def test_env_override_beats_the_config_mode(machine, tmp_path, capsys, monkeypatch):
    machine["sample"] = _sample(**LOW)
    monkeypatch.setenv("SIGMA_RESOURCE_MODE", "off")
    base = backlog(tmp_path, n=2)
    _, out, _ = cli(capsys, "next-batch", str(base))
    assert len(goals_in(out)) == 2
