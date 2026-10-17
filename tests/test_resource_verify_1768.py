"""#1768 - verify admission: one total deadline across commands, and a literal-workers hold is recorded as waiting.

Controls (break one clause, see red): drop the deadline (each command gets the full timeout again) and
`test_total_deadline_*` / `test_remaining_time_*` go red; drop the `note_waiting` call and `test_literal_hold_*` goes red.
"""
import json
import sys
import time

import pytest

from test_resource_verify_1601 import GIB, env, seed  # noqa: F401

unix_only = pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")


@unix_only
def test_total_deadline_spans_all_commands(env, tmp_path):
    started = time.monotonic()
    out = env.run("verify", ["sleep 1", "sleep 1"], str(tmp_path), 1.5, "g1", term_grace=0.2)
    elapsed = time.monotonic() - started
    bounded = env._sibling("bounded_run")
    assert out.result.outcome == bounded.TIMEOUT and out.returncode != 0
    assert len(out.outputs) == 2
    assert elapsed < 1.5 + 1.0, elapsed       # the full 2s of work never ran


def test_remaining_time_passed_to_runner_decreases(env, tmp_path):
    bounded = env._sibling("bounded_run")
    seen = []

    def fake(command, cwd, timeout, **kw):
        seen.append(timeout)
        time.sleep(0.2)
        return bounded.Result(bounded.OK, 0, "", "", 0.2, False, 0, "")
    out = env.run("verify", ["a", "b", "c"], str(tmp_path), 30, "g1", run_group=fake)
    assert out.returncode == 0 and len(seen) == 3
    assert seen[0] <= 30 and seen[0] > seen[1] > seen[2] > 0


def test_nothing_left_is_a_timeout_without_running(env, tmp_path):
    bounded = env._sibling("bounded_run")
    calls = []

    def fake(command, cwd, timeout, **kw):
        calls.append(command)
        time.sleep(0.3)
        return bounded.Result(bounded.OK, 0, "", "", 0.3, False, 0, "")
    out = env.run("verify", ["a", "b"], str(tmp_path), 0.2, "g1", run_group=fake)
    assert calls == ["a"] and out.result.outcome == bounded.TIMEOUT and out.returncode != 0


def test_literal_hold_records_waiting_and_blocks_other_goal_starts(env, tmp_path):
    cfg = {"mode": "enforce"}
    assert not isinstance(env.lease.admit("goal", 1, "r", "g1", config=cfg), env.lease.Hold)
    seed(env, [4 * GIB])
    out = env.run("verify", "true -n 6", str(tmp_path), 30, "g1", config=cfg)
    assert out.hold is not None and "literal -n 6" in out.hold.reason
    waiting = json.loads((env.state / "waiting.json").read_text())
    assert waiting["g1"]["kind"] == "verify" and waiting["g1"]["until"] > waiting["g1"]["since"]
    other = env.lease.admit("goal", 1, "r", "g2", config=cfg)
    assert isinstance(other, env.lease.Hold) and "verify of a started goal is waiting" in other.reason


def test_non_hold_path_leaves_waiting_empty(env, tmp_path):
    out = env.run("verify", "true", str(tmp_path), 30, "g1")
    assert out.hold is None
    path = env.state / "waiting.json"
    assert not path.exists() or json.loads(path.read_text()) == {}
