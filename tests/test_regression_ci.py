"""#1060 (AC-6) -- the workflow runs the layer-2 control on anchor-touching PRs and nightly.

`validate_job` is the guard; each `test_*_rejected` breaks the workflow text one documented way and
demands the guard raise, so a guard that cannot fail is caught here (a red control, not decoration)."""
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
GESTURE = 'python3 evals/regression/control.py run --changed-files "$RUNNER_TEMP/changed-files.txt"'


def job(text):
    assert "\n  regression-control:" in text, "regression-control job missing"
    return text.split("\n  regression-control:", 1)[1].split("\n  onboarding-control:", 1)[0]


def validate_job(text):
    j = "\n".join(ln for ln in job(text).splitlines() if not ln.lstrip().startswith("#"))
    head = j.split("steps:", 1)[0]
    for ev in ("pull_request", "schedule", "workflow_dispatch"):
        assert f"github.event_name == '{ev}'" in head, f"job does not run on {ev}"
    assert "push" not in head, "job must not run on push"
    assert GESTURE in j, "PR step must run control.py with --changed-files on the diff"
    assert "git diff --name-only HEAD^1 HEAD" in j and "fetch-depth: 2" in j
    assert re.search(r"^\s+python3 evals/regression/control\.py run$", j, re.M), "nightly step must run every row"
    assert "if: ${{ github.event_name == 'pull_request' }}" in j and "if: ${{ github.event_name != 'pull_request' }}" in j, \
        "each control step needs its own event guard (HEAD^1 diff is PR-only)"
    assert "--record" not in j and "--strict" not in j, "CI must not write the matrix or go red on pending gates"
    for banned in ("secrets.", "ANTHROPIC", "claude ", "gh ", "contents: write"):
        assert banned not in j, f"control job must not use {banned!r}"


def test_workflow_declares_the_regression_control_job():
    validate_job(WORKFLOW.read_text(encoding="utf-8"))


def test_control_dropping_changed_files_flag_is_rejected():
    text = WORKFLOW.read_text(encoding="utf-8").replace(' --changed-files "$RUNNER_TEMP/changed-files.txt"', "")
    with pytest.raises(AssertionError, match="--changed-files"):
        validate_job(text)


def test_control_dropping_schedule_event_is_rejected():
    text = WORKFLOW.read_text(encoding="utf-8")
    broken = text.replace(" || github.event_name == 'schedule'", "")
    assert broken != text
    with pytest.raises(AssertionError, match="schedule"):
        validate_job(broken)


def test_control_step_without_its_event_guard_is_rejected():
    text = WORKFLOW.read_text(encoding="utf-8").replace("        if: ${{ github.event_name == 'pull_request' }}\n", "")
    with pytest.raises(AssertionError, match="event guard"):
        validate_job(text)


def test_control_step_cannot_write_or_spend():
    # The break ADDS a recording run beside the nightly one, so every earlier clause still holds and the
    # `--record` clause is the one that fires. Rewriting the nightly line instead tripped the nightly-row
    # clause first, and this passed only while pytest's repr of the job text happened to contain "--record".
    text = WORKFLOW.read_text(encoding="utf-8")
    broken = text.replace("control.py run\n", "control.py run\n          python3 evals/regression/control.py run --record\n")
    assert broken != text
    with pytest.raises(AssertionError, match="must not write the matrix"):
        validate_job(broken)


def _control():
    spec = importlib.util.spec_from_file_location("control_under_test", ROOT / "evals/regression/control.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_anchor_file_change_selects_its_rows_and_unrelated_change_selects_none():
    """The step feeds `git diff --name-only` lines to control.py's own selector; run it on the real catalogue."""
    import json
    import types
    reg = ROOT / "evals" / "regression"
    rows = json.loads((reg / "catalogue.json").read_text())["rows"]
    gates = {g["id"]: g for g in json.loads((reg / "gates.json").read_text())["gates"]}
    select = _control()._select
    args = types.SimpleNamespace(regression=None)
    hit = select(rows, gates, args, {"skills/sigma-loop/scripts/work.py"})
    assert {r["id"] for r in hit} >= {"skip-plan-review/code", "drop-review-recording/code",
                                      "disable-secret-scan/commit-noop"}
    assert select(rows, gates, args, {"skills/sigma-loop/scripts/scrub.py"})
    assert select(rows, gates, args, {"README.md", "docs/launch/review-plan.md"}) == []


# ---- #879: the `onboarding-control` job runs the plain control against the budget and the planted regressions

BUDGET_STEP = "python3 evals/regression/budget.py"
PLANTED_STEP = "python -m pytest tests/test_onboarding_control.py -k planted -q -n 2"


def validate_onboarding_job(text):
    assert "\n  onboarding-control:" in text, "onboarding-control job missing"
    j = text.split("\n  onboarding-control:", 1)[1].split("\n  nightly:", 1)[0]
    assert "\n  regression-control:" not in j, "onboarding-control must come after regression-control"
    head = "\n".join(ln for ln in j.split("steps:", 1)[0].splitlines() if not ln.lstrip().startswith("#"))
    # nightly tier: the daily cron, never a PR or a push (that tier is the smoke `test` job's)
    assert "github.event.schedule == '17 5 * * *'" in head, "job does not run on the nightly cron"
    assert "inputs.tier == 'nightly'" in head, "job cannot be run by hand as the nightly tier"
    assert "pull_request" not in head and "'push'" not in head, "job must not run on pull_request or push"
    j = "\n".join(ln for ln in j.splitlines() if not ln.lstrip().startswith("#"))
    assert re.search(rf"^\s+run: {re.escape(BUDGET_STEP)}$", j, re.M), "onboarding job must run the budget step"
    assert PLANTED_STEP in j, "onboarding job must run the planted-regression tests"
    assert "continue-on-error" not in j, "the budget step must be able to fail the job"
    assert "runs-on: macos-latest" in head and "timeout-minutes:" in head, "job must be the macOS cell with a timeout"


def test_onboarding_job_runs_the_control_against_the_budget():
    validate_onboarding_job(WORKFLOW.read_text(encoding="utf-8"))
    budget = json.loads((ROOT / "evals" / "regression" / "budget.json").read_text(encoding="utf-8"))
    assert isinstance(budget["ceiling_seconds"], (int, float)) and budget["ceiling_seconds"] > 0, \
        "budget.json must hold a measured ceiling, not null (null means unenforced)"


def test_onboarding_job_without_the_budget_step_is_rejected():
    text = WORKFLOW.read_text(encoding="utf-8")
    broken = text.replace(BUDGET_STEP, "true")
    assert broken != text
    with pytest.raises(AssertionError, match="budget step"):
        validate_onboarding_job(broken)


def test_onboarding_job_without_the_planted_tests_is_rejected():
    text = WORKFLOW.read_text(encoding="utf-8")
    broken = text.replace(" -k planted", "")
    assert broken != text
    with pytest.raises(AssertionError, match="planted-regression"):
        validate_onboarding_job(broken)


def _onboarding_if(text):
    return text.split("\n  onboarding-control:", 1)[1].split("\n    if: ", 1)[1].split("\n", 1)[0]


def test_onboarding_job_moved_back_onto_pull_requests_is_rejected():
    text = WORKFLOW.read_text(encoding="utf-8")
    cond = _onboarding_if(text)
    broken = text.replace(cond, "${{ github.event_name == 'pull_request' || " + cond[len("${{ "):], 1)
    assert broken != text
    with pytest.raises(AssertionError, match="must not run on pull_request"):
        validate_onboarding_job(broken)


def test_onboarding_job_without_the_nightly_cron_is_rejected():
    text = WORKFLOW.read_text(encoding="utf-8")
    broken = text.replace(_onboarding_if(text), "${{ inputs.tier == 'nightly' }}", 1)
    assert broken != text
    with pytest.raises(AssertionError, match="nightly cron"):
        validate_onboarding_job(broken)


def test_onboarding_job_that_cannot_be_run_by_hand_is_rejected():
    text = WORKFLOW.read_text(encoding="utf-8")
    broken = text.replace(_onboarding_if(text), "${{ github.event.schedule == '17 5 * * *' }}", 1)
    assert broken != text
    with pytest.raises(AssertionError, match="by hand"):
        validate_onboarding_job(broken)
