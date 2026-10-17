"""#335 — write-surface scanner and inventory ratchet controls."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "readiness" / "write_surface.py"


def _module():
    spec = importlib.util.spec_from_file_location("write_surface", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scanner_detects_a_github_delete_site(tmp_path):
    source = tmp_path / "a.py"
    source.write_text('import subprocess\n\ndef erase(n):\n    subprocess.run(["gh", "issue", "delete", n])\n')
    findings = _module().scan_paths(tmp_path, [source])
    assert {(f["function"], f["rule"], f["count"]) for f in findings} == {("erase", "gh-issue", 1)}


def test_ratchet_names_an_added_write_site(tmp_path):
    source = tmp_path / "a.py"
    source.write_text('import subprocess\n\ndef erase(n):\n    subprocess.run(["gh", "issue", "delete", n])\n')
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({"entries": []}))
    findings = _module().ratchet(tmp_path, inventory)
    assert findings == ["new write site a.py:erase gh-issue -- add it to docs/launch/write-surface.json with its gate"]


def test_ratchet_rejects_an_empty_gate(tmp_path):
    source = tmp_path / "a.py"
    source.write_text('import subprocess\n\ndef erase(n):\n    subprocess.run(["gh", "issue", "delete", n])\n')
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({"entries": [{"path": "a.py", "function": "erase", "rule": "gh-issue", "count": 1, "gate": "", "risk": "high"}]}))
    assert _module().ratchet(tmp_path, inventory) == ["empty gate a.py:erase gh-issue"]


def test_live_path_selection_uses_tracked_files(tmp_path):
    (tmp_path / "tracked.py").write_text("x = 1\n")
    (tmp_path / "untracked.py").write_text("x = 2\n")
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "tracked.py"], check=True)
    assert _module()._paths(tmp_path) == [tmp_path / "tracked.py"]


def test_scanner_ignores_documented_force_and_finds_real_destructive_calls(tmp_path):
    source = tmp_path / "writes.py"
    source.write_text('''"""Use --force only as a documented option."""

from pathlib import Path
import os

def clear(path):
    Path(path).unlink()
    os.rmdir(path)
''')
    got = {(row["function"], row["rule"]) for row in _module().scan_paths(tmp_path, [source])}
    assert got == {("clear", "fs-remove")}


def test_scan_output_renders_and_check_rejects_bad_inventory(tmp_path):
    source = tmp_path / "a.py"
    source.write_text('import subprocess\nsubprocess.run(["git", "push"])\n')
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "a.py"], check=True)
    inventory = tmp_path / "inventory.json"
    scanned = subprocess.run([sys.executable, str(SCRIPT), "scan", str(tmp_path), "--json", str(inventory)],
                             capture_output=True, text=True)
    assert scanned.returncode == 0, scanned.stderr
    rendered = subprocess.run([sys.executable, str(SCRIPT), "render", str(inventory)],
                              capture_output=True, text=True)
    assert rendered.returncode == 0, rendered.stderr
    assert "| a.py | <module> | git-push | 1 | ungated | high |" in rendered.stdout
    data = json.loads(inventory.read_text())
    data["entries"][0].pop("risk")
    inventory.write_text(json.dumps(data))
    checked = subprocess.run([sys.executable, str(SCRIPT), "check", str(tmp_path), str(inventory)],
                             capture_output=True, text=True)
    assert checked.returncode == 1
    assert "missing risk" in checked.stdout


def test_committed_inventory_matches_the_tracked_write_surface():
    assert _module().ratchet(ROOT, ROOT / "docs" / "launch" / "write-surface.json") == []


def test_each_python_write_rule_has_a_positive_and_a_nonexecuting_lookalike(tmp_path):
    source = tmp_path / "writes.py"
    source.write_text('''"""gh label delete; git push; rm -rf are documentation only."""
import shutil
import os
import subprocess
from pathlib import Path

def writes(path):
    subprocess.run(["gh", "issue", "delete", "1"])
    subprocess.run(["gh", "pr", "merge", "1"])
    subprocess.run(["gh", "label", "delete", "x"])
    subprocess.run(["gh", "project", "item-delete", "x"])
    subprocess.run(["gh", "api", "-X", "POST", "x"])
    _graphql("mutation { x }")
    subprocess.run(["git", "push"])
    subprocess.run(["git", "reset", "--hard"])
    shutil.rmtree(path)
    os.unlink(path)
    Path(path).write_text("x")
''')
    got = {row["rule"] for row in _module().scan_paths(tmp_path, [source])}
    assert got == {"gh-issue", "gh-pr", "gh-label", "gh-project", "gh-api-write",
                   "graphql-mutation", "git-push", "git-destructive", "fs-rmtree",
                   "fs-remove", "fs-write"}


def test_shell_write_sites_include_label_delete_and_ignore_comments(tmp_path):
    source = tmp_path / "writes.sh"
    source.write_text('''#!/bin/sh
# gh label delete docs-only
printf '%s\\n' 'git push docs-only'
gh label delete legacy
git push origin topic
git reset --hard HEAD
rm -rf scratch
''')
    got = {row["rule"] for row in _module().scan_paths(tmp_path, [source])}
    assert got == {"gh-label", "git-push", "git-destructive", "fs-rmtree"}


def test_documented_shell_label_delete_control_fails_the_ratchet(tmp_path):
    source = tmp_path / "control.sh"
    source.write_text("gh label delete legacy\\n")
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({"entries": []}))
    assert _module().ratchet(tmp_path, inventory) == [
        "new write site control.sh:<script> gh-label -- add it to docs/launch/write-surface.json with its gate"
    ]


@pytest.mark.parametrize("rule, text", [
    ("gh-issue", 'subprocess.run(["gh", "issue", "delete", "1"])'),
    ("gh-pr", 'subprocess.run(["gh", "pr", "merge", "1"])'),
    ("gh-label", 'subprocess.run(["gh", "label", "delete", "x"])'),
    ("gh-project", 'subprocess.run(["gh", "project", "item-delete", "x"])'),
    ("gh-api-write", 'subprocess.run(["gh", "api", "-X", "POST", "x"])'),
    ("graphql-mutation", '_graphql("mutation { x }")'),
    ("git-push", 'subprocess.run(["git", "push"])'),
    ("git-destructive", 'subprocess.run(["git", "reset", "--hard"])'),
    ("fs-rmtree", 'shutil.rmtree(path)'),
    ("fs-remove", 'os.unlink(path)'),
    ("fs-write", 'Path(path).write_text("x")'),
])
def test_each_command_rule_ignores_a_nonexecuting_lookalike(tmp_path, rule, text):
    source = tmp_path / "lookalike.py"
    source.write_text("def explain():\n    print(" + repr(text) + ")\n")
    assert rule not in {row["rule"] for row in _module().scan_paths(tmp_path, [source])}


def test_call_site_metadata_escalates_merge_risk_without_escalating_comments(tmp_path):
    source = tmp_path / "a.py"
    source.write_text('import subprocess\n\ndef merge():\n    subprocess.run(["gh", "pr", "merge", "1"])\n')
    row = _module().scan_paths(tmp_path, [source])[0]
    assert row["gate"] == "ungated" and row["risk"] == "medium"
    assert _module()._metadata("skills/sigma-loop/scripts/work.py", "merge", "gh-pr")[1] == "high"


def test_retry_gh_is_an_execution_seam(tmp_path):
    source = tmp_path / "work.py"
    source.write_text('def merge_design(run, cwd):\n    _retry_gh(run, cwd, ["gh", "pr", "merge", "1"])\n')
    assert _module().scan_paths(tmp_path, [source])[0]["rule"] == "gh-pr"


def test_design_pr_rest_writes_are_scanned_and_their_metadata_is_specific(tmp_path):
    """#895 4b-3: merge_design/close_design reach GitHub through gh_api.merge_pr_gh / close_pr_gh, which the
    scanner must count as REST writes (else the migration would silently drop them from the inventory)."""
    source = tmp_path / "work.py"
    source.write_text('def merge_design(api):\n    gh_api.merge_pr_gh(api, 1, "merge", "a" * 40)\n'
                      'def close_design(api):\n    gh_api.close_pr_gh(api, 1)\n')
    rows = {r["function"]: r for r in _module().scan_paths(tmp_path, [source])}
    assert rows["merge_design"]["rule"] == rows["close_design"]["rule"] == "gh-api-write"
    gate, risk = _module()._metadata("skills/sigma-loop/scripts/work.py", "merge_design", "gh-api-write")
    assert gate.startswith("work.enabled; work.auto_merge != off;") and risk == "high"
    assert _module()._metadata("skills/sigma-loop/scripts/work.py", "close_design", "gh-api-write")[0].startswith("ungated")


def test_git_runner_force_and_destructive_forms_are_scanned(tmp_path):
    source = tmp_path / "writes.py"
    source.write_text('''def cleanup(root, scratch):
    gitc(root, ["worktree", "remove", "--force", scratch])

def reconcile(root, name):
    run(root, ["branch", "--force", name, "origin/" + name])

def push(root, remote):
    run(root, ["git", "push", "--force-with-lease=topic:sha", remote])
''')
    got = {(row["function"], row["rule"]) for row in _module().scan_paths(tmp_path, [source])}
    assert got == {("cleanup", "git-destructive"), ("reconcile", "git-destructive"),
                   ("push", "git-push"), ("push", "git-destructive")}


def test_live_git_runner_force_sites_are_in_the_inventory():
    mod = _module()
    paths = [ROOT / "skills/sigma-loop/scripts/diff_revert.py",
             ROOT / "skills/sigma-loop/scripts/work.py",
             ROOT / "skills/sigma-loop/scripts/sync.py"]
    got = {(row["path"], row["function"], row["rule"])
           for row in mod.scan_paths(ROOT, paths)}
    assert ("skills/sigma-loop/scripts/diff_revert.py", "cleanup", "git-destructive") in got
    assert ("skills/sigma-loop/scripts/work.py", "finish", "git-destructive") in got
    assert ("skills/sigma-loop/scripts/sync.py", "init", "git-destructive") in got


def test_issue_decided_write_gates_and_delete_risks_are_exact():
    mod = _module()
    assert mod._metadata("skills/sigma-loop/scripts/feature_propagate.py", "_write_remote", "gh-api-write") == ("granted verdict", "high")
    gate, risk = mod._metadata("skills/sigma-loop/scripts/work.py", "close_design", "gh-api-write")
    assert gate.startswith("ungated") and risk == "high"       # #895 4b-3: REST now, still the ungated direction
    assert mod._metadata("skills/sigma-loop/scripts/work.py", "merge", "gh-pr") == ("work.enabled; work.auto_merge != off; merge rights; fresh verify evidence and CLEAN PR", "high")
    for function, rule in (("_delete_remote_branch", "gh-api-write"), ("_close_issue_the_base_cannot", "gh-api-write")):
        assert mod._metadata("skills/sigma-loop/scripts/work.py", function, rule)[1] == "high"


def test_readiness_tool_write_metadata_is_specific():
    mod = _module()
    assert mod._metadata("tools/readiness/baseline.py", "snapshot", "fs-write") == (
        "explicit snapshot command; empty destination", "medium")
    assert mod._metadata("tools/readiness/baseline.py", "snapshot", "git-destructive") == (
        "explicit snapshot command; empty destination; detached push-disabled clone", "medium")
    assert mod._metadata("tools/readiness/egress_capture.py", "main", "fs-write") == (
        "explicit summarize command; caller-supplied JSON path", "medium")
    assert mod._metadata("tools/readiness/exposure_scan.py", "_write", "fs-write") == (
        "explicit exposure scan; caller-supplied evidence path", "medium")
    assert mod._metadata("tools/readiness/exposure_scan.py", "scan_refs", "git-destructive") == (
        "explicit refs scan; local tag listing is read-only", "low")
    assert mod._metadata("tools/readiness/review_units.py", "main", "fs-write") == (
        "explicit --json PATH; caller-supplied output path", "medium")
    assert mod._metadata("tools/readiness/seed_defects.py", "apply", "fs-write") == (
        "explicit apply command; manifest.json beside the patches, outside the clone; detached clean clone only",
        "medium")
    assert mod._metadata("tools/readiness/injection_drill.py", "file_payloads", "gh-api-write") == (
        "explicit file subcommand; OWNER/sigma-drill- name fullmatch; repository read back and "
        "must be private; declared --max-usd; never run by Sigma", "high")
    assert mod._metadata("tools/readiness/injection_drill.py", "_write_json", "fs-write")[1] == "medium"


def test_931_gh_api_landing_write_helpers_are_seen_and_read_helpers_are_not(tmp_path):
    mod = _module()
    for name in ("merge_pr_pinned", "create_pr_nondraft"):
        assert name in mod._GH_API_WRITES
    source = tmp_path / "caller.py"
    source.write_text('import gh_api\n\ndef land():\n    gh_api.merge_pr_pinned(run, "o/r", 1, "x", merge_method="merge")\n'
                      '    gh_api.create_pr_nondraft(run, "t", "b", "h", "m", "o/r")\n'
                      '    gh_api.commit_parents(run, "o/r", "x")\n    gh_api.branch_rules(run, "o/r", "m")\n'
                      '    gh_api.repo_settings(run, "o/r")\n')
    rows = mod.scan_paths(tmp_path, [source])
    assert [(r["function"], r["rule"], r["count"]) for r in rows] == [("land", "gh-api-write", 2)]


def test_895_4b2_pr_comment_and_merge_helpers_are_seen_and_the_work_rows_are_pinned(tmp_path):
    mod = _module()
    for name in ("comment_pr", "merge_pr_gh"):
        assert name in mod._GH_API_WRITES
    source = tmp_path / "caller.py"
    source.write_text('import gh_api\n\ndef land():\n    gh_api.comment_pr(run, 7, "b")\n'
                      '    gh_api.merge_pr_gh(run, 7, "squash", "x")\n')
    assert [(r["function"], r["rule"], r["count"]) for r in mod.scan_paths(tmp_path, [source])] == \
        [("land", "gh-api-write", 2)]
    inv = json.loads((ROOT / "docs" / "launch" / "write-surface.json").read_text())["entries"]
    work = {(e["function"], e["rule"]) for e in inv if e["path"] == "skills/sigma-loop/scripts/work.py"}
    assert ("post_review", "gh-api-write") in work and ("merge", "gh-api-write") in work
    assert ("post_review", "gh-pr") not in work          # the `gh pr comment` literal is gone (the ratchet's fallback lives in gh_api)


def test_931_gh_api_landing_helper_words_are_pinned():
    # The ratchet checks presence and a non-empty gate, not the words; this pins the words (D-19).
    # Since #926 the generic runner has no inventory row, so the words live in the inventory page.
    text = (ROOT / "docs" / "launch" / "write-surface.md").read_text()
    for phrase in ("merge_pr_pinned", "40-hex", "explicit merge method", "no auto-merge", "no branch-delete",
                   "no caller yet"):
        assert phrase in text, phrase


def test_926_name_resolution_is_function_scoped(tmp_path):
    source = tmp_path / "a.py"
    source.write_text(
        'import subprocess\n\n'
        'def closer(n):\n    args = ["pr", "close", n]\n    subprocess.run(["gh", *args])\n\n'
        'def finish(n):\n    args = ["worktree", "remove", n]\n    subprocess.run(["git", *args])\n\n'
        'def generic(args):\n    subprocess.run(["gh", *args])\n')
    rows = {(r["function"], r["rule"], r["count"]) for r in _module().scan_paths(tmp_path, [source])}
    assert rows == {("closer", "gh-pr", 1), ("finish", "git-destructive", 1)}


def test_926_module_constant_and_enclosing_scope_still_resolve(tmp_path):
    source = tmp_path / "a.py"
    source.write_text(
        'import subprocess\nVERB = ["pr", "close", "1"]\n\n'
        'def outer():\n    args = ["issue", "delete", "2"]\n\n    def inner():\n        subprocess.run(["gh", *args])\n'
        '    inner()\n\ndef uses_module():\n    subprocess.run(["gh", *VERB])\n')
    rows = {(r["function"], r["rule"]) for r in _module().scan_paths(tmp_path, [source])}
    assert rows == {("inner", "gh-issue"), ("uses_module", "gh-pr")}


def test_1842_atomic_write_text_is_an_fs_write_site(tmp_path):
    source = tmp_path / "a.py"
    source.write_text("import state\n\ndef record(p):\n    state.atomic_write_text(p, 'x')\n")
    found = _module().scan_paths(tmp_path, [source])
    assert {(f["function"], f["rule"], f["count"]) for f in found} == {("record", "fs-write", 1)}


def test_1842_scan_of_the_repo_lists_chain_answer():
    found = _module().scan_paths(ROOT, _module()._paths(ROOT))
    chain = {(f["function"], f["rule"]) for f in found if f["path"] == "skills/sigma-loop/scripts/chain.py"}
    assert ("answer", "fs-write") in chain and ("plan", "fs-write") in chain


def test_1842_inventory_has_chain_answer_gate_naming_the_unpark_resolve_reach():
    entries = json.loads((ROOT / "docs" / "launch" / "write-surface.json").read_text())["entries"]
    row = [e for e in entries if e["path"].endswith("scripts/chain.py") and e["function"] == "answer"]
    assert len(row) == 1 and row[0]["rule"] == "fs-write"
    assert "unpark" in row[0]["gate"] and "resolve" in row[0]["gate"] and "GitHub" in row[0]["gate"]
    plan = [e for e in entries if e["path"].endswith("scripts/chain.py") and e["function"] == "plan"][0]
    assert "scanner does not see it" not in plan["gate"]


def test_1842_dropping_the_chain_answer_entry_makes_the_ratchet_report_it():
    data = json.loads((ROOT / "docs" / "launch" / "write-surface.json").read_text())
    data["entries"] = [e for e in data["entries"] if not (e["path"].endswith("scripts/chain.py") and e["function"] == "answer")]
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        inv = Path(d) / "inv.json"
        inv.write_text(json.dumps(data))
        out = _module().ratchet(ROOT, inv)
    assert any("new write site skills/sigma-loop/scripts/chain.py:answer fs-write" in x for x in out)
