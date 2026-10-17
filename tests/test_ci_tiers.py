"""The three CI tiers (owner decision 2026-10-16): PR smoke, nightly, weekly full. Parses ci.yml, no network."""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
SMOKE = ROOT / ".github" / "smoke-tests.txt"
NIGHTLY_CRON = "17 5 * * *"
WEEKLY_CRON = "47 5 * * 0"


def job_if(text, job):
    body = "\n" + text.split("\n  %s:\n" % job, 1)[1]
    return body.split("\n    if: ", 1)[1].split("\n", 1)[0]


def validate_tiers(text, smoke_text):
    for cron in (NIGHTLY_CRON, WEEKLY_CRON):
        assert '- cron: "%s"' % cron in text, "missing schedule " + cron
    pr = job_if(text, "test")
    assert "pull_request" in pr and "'push'" in pr, "`test` (the required check) must run on PRs and pushes"
    assert "inputs.tier == 'smoke'" in pr, "`test` must be runnable by hand as the smoke tier"
    for job, cron, tier in (("nightly", NIGHTLY_CRON, "nightly"), ("full", WEEKLY_CRON, "full")):
        cond = job_if(text, job)
        assert "github.event.schedule == '%s'" % cron in cond, "%s does not run on its own cron" % job
        assert "inputs.tier == '%s'" % tier in cond, "%s cannot be run by hand" % job
        assert "pull_request" not in cond and "'push'" not in cond, "%s must not run on PRs or pushes" % job
    test_job = text.split("\n  test:\n", 1)[1].split("\n  full:\n", 1)[0]
    assert ".github/smoke-tests.txt" in test_job and "tests/ $SLOW" not in test_job, "`test` must run the smoke list only"
    assert "SLOW" in text.split("\n  nightly:\n", 1)[1], "nightly must run the suite minus the heaviest files"
    paths = [l.strip() for l in smoke_text.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    assert paths, "the smoke list is empty"
    for p in paths:
        assert (ROOT / p).is_file(), "smoke list names a file that does not exist: " + p


def test_the_three_tiers_are_wired_as_decided():
    validate_tiers(WORKFLOW.read_text(encoding="utf-8"), SMOKE.read_text(encoding="utf-8"))


def _broken(old, new):
    text = WORKFLOW.read_text(encoding="utf-8")
    assert old in text
    return text.replace(old, new, 1)


def test_control_nightly_job_on_pull_request_is_rejected():
    text = WORKFLOW.read_text(encoding="utf-8")
    head, tail = text.split("\n  nightly:\n", 1)            # the condition text is shared with onboarding-control
    cond = job_if(text, "nightly")
    broken = head + "\n  nightly:\n" + tail.replace(cond, "${{ github.event_name == 'pull_request' || " + cond[len("${{ "):], 1)
    assert broken != text
    with pytest.raises(AssertionError, match="must not run on PRs"):
        validate_tiers(broken, SMOKE.read_text(encoding="utf-8"))


def test_control_weekly_job_without_its_cron_is_rejected():
    text = _broken('- cron: "%s"' % WEEKLY_CRON, '- cron: "1 1 * * 1"')
    with pytest.raises(AssertionError, match="missing schedule"):
        validate_tiers(text, SMOKE.read_text(encoding="utf-8"))


def test_control_pr_job_running_the_whole_suite_is_rejected():
    text = _broken("python -m pytest $(grep -v '^#' .github/smoke-tests.txt)", "python -m pytest tests/ $SLOW")
    with pytest.raises(AssertionError, match="smoke list only"):
        validate_tiers(text, SMOKE.read_text(encoding="utf-8"))


def test_control_smoke_list_naming_a_missing_file_is_rejected():
    with pytest.raises(AssertionError, match="does not exist"):
        validate_tiers(WORKFLOW.read_text(encoding="utf-8"), "tests/test_no_such_file.py\n")
