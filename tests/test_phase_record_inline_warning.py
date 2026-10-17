"""#684: on an INLINE reviewer route the different-agent checks do not apply; `record done` says so loudly
(the phases are recorded, not proved independent). A separate file so the planned test file's bytes, and the
assertion-red evidence bound to them, stay as they were."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import resource_mode  # noqa: E402
import test_phase_record_gate as g  # noqa: E402


def test_done_on_an_inline_route_prints_a_loud_warning(tmp_path):
    p = g._project(tmp_path)
    g.full_run(p)                                                  # the test env has no host marker: inline
    r = g.done(p)
    assert r.returncode == 0 and "INLINE" in r.stderr and "not proved independent" in r.stderr, r.stderr


def test_picking_a_goal_says_the_phase_record_is_enforced(tmp_path):
    p = g._project(tmp_path)
    (p / ".sdlc/goals/0001-x.md").write_text("---\nid: 0001\ntitle: x\nstatus: pending\n---\nbody\n")
    r = g.run(p, "loop.py", "next", ".sdlc", env=resource_mode.carry({}))    # #1759: g.ENV predates conftest
    assert r.stdout.strip().endswith("0001-x.md"), r.stdout + r.stderr
    assert "phase record is ENFORCED" in r.stderr and "OWN dispatched subagent" in r.stderr, r.stderr
    off = g._project(tmp_path / "off") if (tmp_path / "off").mkdir() is None else None
    (off / ".sdlc/config.json").write_text('{"action_log": {"enabled": true}}')       # key absent: off
    (off / ".sdlc/goals/0001-x.md").write_text("---\nid: 0001\ntitle: x\nstatus: pending\n---\nbody\n")
    assert "ENFORCED" not in g.run(off, "loop.py", "next", ".sdlc", env=resource_mode.carry({})).stderr
