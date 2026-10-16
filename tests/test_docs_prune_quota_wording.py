"""#1701: two statements that described behaviour the code does not have.

The sweep prunes rewrite backups by itself (#1557), so no guide may say nothing does. And a
`keep-parked` on a quota park posts no opt-out marker but is NOT resumed by the sweep: it only
resumes a dependency park whose named blockers close, and a quota park names none."""
import importlib.util
import pathlib
import re

import test_auto_unpark as tau

ROOT = pathlib.Path(__file__).resolve().parent.parent
GUIDES = sorted((ROOT / "docs").glob("*.md")) + [ROOT / "README.md", ROOT / "AGENTS.md"]


def _flat(p):
    return re.sub(r"\s+", " ", p.read_text(encoding="utf-8"))


def test_no_guide_says_nothing_prunes_by_itself():
    bad = [p.name for p in GUIDES if re.search(r"nothing prunes by itself|does not either, so while upkeep", _flat(p), re.I)]
    assert bad == []


def test_branching_model_points_at_the_automatic_prune():
    t = _flat(ROOT / "docs" / "branching-model.md")
    assert "prunes by itself" in t and "upkeep.md" in t


def test_rulebook_and_unpark_comment_do_not_promise_a_quota_resume():
    for rel in ("docs/decision-rulebook.md", "skills/sigma-loop/scripts/unpark.py"):
        t = _flat(ROOT / rel)
        assert "may resume the goal after the quota window" not in t, rel
        assert "or its quota window resets" not in t, rel
    assert "the sweep does not resume it either" in _flat(ROOT / "docs" / "decision-rulebook.md")


def test_changelog_says_the_sweep_does_not_resume_a_quota_park():
    t = _flat(ROOT / "CHANGELOG.md")
    assert re.search(r"quota park.{0,200}sweep does not resume", t)


def test_sweep_takes_no_action_on_a_quota_keep_parked_issue():
    au = tau._mod("auto_unpark")
    body = "Parked: model quota exhausted, wait it out until the window resets."
    for labels in (("sdlc:parked",), ("sdlc:blocked",)):
        issues = [tau._blocked_issue(77, body=body, labels=labels)]
        run = tau._sweep_runner()
        source = au.sources.GitHubSource(tau._config(), run=run)
        actions, resolved = au.compute_unpark_actions(".sdlc", tau._config(), source, issues, run=run)
        assert actions == [] and resolved == {}
