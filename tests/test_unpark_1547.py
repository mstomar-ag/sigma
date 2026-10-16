"""#1547: a keep-parked on a WAITING route posts no sweep opt-out marker; every other route does.

Driven through `unpark.main(["unpark.py", "resolve", <sdlc_dir>, <issue>, "--answers", F,
"--decision", "keep-parked"])` -- the documented gesture -- on a temp git repo with a fake gh.
"""
import json, subprocess

import pytest

import test_unpark as tu

_mod = tu._mod


def _marker_posted(tmp_path, kind, route):
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    u, q, au = _mod("unpark"), _mod("qkind"), _mod("auto_unpark")
    (tmp_path / "config.json").write_text(json.dumps(tu._config()))
    answers = tmp_path / "a.json"
    answers.write_text(json.dumps({"answers": {"route": route}}))
    run = tu._runner(views={"5": tu._view(comments=[tu._park("x\n" + q.render_line(kind))])})
    code = u.main(["unpark.py", "resolve", str(tmp_path), "5", "--answers", str(answers),
                   "--decision", "keep-parked"], run=run)
    assert code == 0
    comments = [c[c.index("--body") + 1] for c in run.calls if c[:2] == ["issue", "comment"]]
    return any(au.KEEP_PARKED_MARKER in c for c in comments)


@pytest.mark.parametrize("kind,route,posted", [
    ("dependency", "wait for it", False),
    ("quota", "wait it out", False),
    ("quota", "retry now", True),
    ("dependency", "drop this", True),
    ("needs_decision", "anything", True),
])
def test_keep_parked_marker_only_for_non_waiting_routes(tmp_path, kind, route, posted):
    assert _marker_posted(tmp_path, kind, route) is posted
