"""Known launch-readiness documentation claims stay evidence-backed (#340)."""
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_readme_does_not_claim_unenforced_coverage_floor():
    readme = (ROOT / "README.md").read_text()
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "85% coverage floor" not in readme or "--cov-fail-under" in workflow


def test_readme_hook_count_matches_registry():
    readme = (ROOT / "README.md").read_text()
    hooks = json.loads((ROOT / "hooks/hooks.json").read_text())["hooks"]
    count = sum(len(group["hooks"]) for groups in hooks.values() for group in groups)
    assert f"registers {count} hook commands" in readme


def test_readme_has_no_predecessor_version_claim():
    assert "1.0.9" not in (ROOT / "README.md").read_text()


def test_branching_model_names_human_merge_exception():
    text = (ROOT / "docs/branching-model.md").read_text()
    assert "never merges a feature branch anywhere" not in text or "verify_merge.py" in text


def test_channel_webhook_documents_its_enforced_remote_opt_in():
    text = (ROOT / "skills/sigma-loop/scripts/channel_notify.py").read_text()
    assert "127.0.0.1-only" not in text
    assert "allow_remote_webhook" in text


def undated_changelog_headings(text):
    headings = re.findall(r"^## \[?([0-9]+\.[0-9]+\.[0-9]+)\]?\s*(.*)$", text, re.M)
    # The one undated form is the owner-dated placeholder on the newest heading (see test_release_consistency).
    return [v for i, (v, suffix) in enumerate(headings)
            if not (re.search(r"\b\d{4}-\d{2}-\d{2}\b", suffix) or (i == 0 and suffix.startswith("— DATE-PENDING — ")))]


def test_changelog_versions_are_dated():
    assert undated_changelog_headings((ROOT / "CHANGELOG.md").read_text()) == []
