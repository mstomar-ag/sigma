"""Hermetic controls for the launch exposure scanner (#333)."""
import hashlib
import importlib.util
import json
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "readiness" / "exposure_scan.py"


def _tool():
    spec = importlib.util.spec_from_file_location("exposure_scan_333", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, text=True,
                          capture_output=True).stdout.strip()


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    return repo


def _run(*args):
    # The operator's private-pattern file must not change what a hermetic control sees.
    env = {k: v for k, v in os.environ.items() if k != "SIGMA_LEAK_PATTERNS"}
    return subprocess.run([sys.executable, str(TOOL), *map(str, args)], text=True,
                          capture_output=True, timeout=300, env=env)


HOME = "/Us" + "ers/"          # built at runtime: this file is itself scanned


def _h(text):
    """The allowlist's content identity: sha256 of the stripped line (or lines) a match spans."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def _tracked(tmp_path, repo, entries, name="out"):
    allow = tmp_path / (name + "-allow.json")
    allow.write_text(json.dumps(entries), encoding="utf-8")
    out = tmp_path / (name + ".json")
    result = _run("tracked", repo, "--allowlist", allow, "--json", out)
    return result, (json.loads(out.read_text()) if out.exists() else None)


def _commit(repo, message="change"):
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", message)


def _fixture(tmp_path):
    """A repo with one reviewed fixture line and an entry that covers exactly that line."""
    repo = _repo(tmp_path)
    reviewed = "path = " + HOME + "fixture/one"
    (repo / "fixture.txt").write_text("# header\n" + reviewed + "\n", encoding="utf-8")
    _commit(repo, "reviewed fixture")
    entry = {"path": "fixture.txt", "rule": "absolute-home-path", "reason": "reviewed fixture",
             "lines": [_h(reviewed)]}
    return repo, reviewed, entry


def test_history_finds_deleted_token_that_tracked_does_not_and_never_prints_value(tmp_path):
    repo = _repo(tmp_path)
    token = "ghp_" + "x" * 36
    (repo / "a.txt").write_text("token=" + token, encoding="utf-8")
    _git(repo, "add", "a.txt"); _git(repo, "commit", "-qm", "add secret")
    (repo / "a.txt").unlink()
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "remove secret")

    history_json, tracked_json = tmp_path / "history.json", tmp_path / "tracked.json"
    history = _run("history", repo, "--json", history_json)
    tracked = _run("tracked", repo, "--json", tracked_json)
    assert history.returncode == 1 and tracked.returncode == 0
    assert token not in history.stdout and token not in history_json.read_text()
    assert history_json.with_suffix(".md").exists()
    assert any(x["rule"] == "gh-token" for x in json.loads(history_json.read_text())["findings"])
    assert json.loads(tracked_json.read_text())["findings"] == []


def test_history_preserves_every_path_for_a_reused_reachable_blob(tmp_path):
    repo = _repo(tmp_path)
    token = "ghp_" + "z" * 36
    (repo / "a.txt").write_text(token, encoding="utf-8")
    _git(repo, "add", "a.txt"); _git(repo, "commit", "-qm", "first path")
    (repo / "b.txt").write_text(token, encoding="utf-8")
    _git(repo, "add", "b.txt"); _git(repo, "commit", "-qm", "second path")

    out = tmp_path / "history.json"
    result = _run("history", repo, "--json", out)
    finding = next(x for x in json.loads(out.read_text())["findings"] if x["rule"] == "gh-token")

    assert result.returncode == 1
    assert finding["paths"] == ["a.txt", "b.txt"]
    assert len(finding["commits"]) == 2


def test_allowlist_suppresses_exact_path_rule_and_stale_entry_is_a_finding(tmp_path):
    repo = _repo(tmp_path)
    token = "ghp_" + "y" * 36
    (repo / "a.txt").write_text(token, encoding="utf-8")
    (repo / "allow.json").write_text(json.dumps([
        {"path": "a.txt", "rule": "gh-token", "reason": "synthetic fixture", "lines": [_h(token)]},
        {"path": "gone.txt", "rule": "gh-token", "reason": "must be removed", "lines": [_h("x")]},
    ]), encoding="utf-8")
    _git(repo, "add", "."); _git(repo, "commit", "-qm", "fixture")
    out = tmp_path / "out.json"
    result = _run("tracked", repo, "--allowlist", repo / "allow.json", "--json", out)
    report = json.loads(out.read_text())
    assert result.returncode == 1 and token not in result.stdout and token not in out.read_text()
    assert report["findings"] == []
    assert report["stale_allowlist"] == [{"path": "gone.txt", "rule": "gh-token"}]


def test_blob_scoped_allowlist_does_not_hide_a_changed_fixture_at_the_same_path(tmp_path):
    repo = _repo(tmp_path)
    fixture = repo / "fixture.txt"
    fixture.write_text("/Us" + "ers/fixture/one", encoding="utf-8")
    _git(repo, "add", "."); _git(repo, "commit", "-qm", "reviewed fixture")
    reviewed_blob = _git(repo, "rev-parse", "HEAD:fixture.txt")
    allow = repo / "allow.json"
    allow.write_text(json.dumps([{
        "path": "fixture.txt", "rule": "absolute-home-path", "blob": reviewed_blob,
        "reason": "reviewed hermetic fixture",
    }]), encoding="utf-8")

    green_json = tmp_path / "green.json"
    green = _run("tracked", repo, "--allowlist", allow, "--json", green_json)
    assert green.returncode == 0
    assert json.loads(green_json.read_text())["findings"] == []

    fixture.write_text("/Us" + "ers/fixture/two", encoding="utf-8")
    _git(repo, "add", "fixture.txt"); _git(repo, "commit", "-qm", "changed fixture")
    red_json = tmp_path / "red.json"
    red = _run("tracked", repo, "--allowlist", allow, "--json", red_json)
    report = json.loads(red_json.read_text())
    assert red.returncode == 1
    assert any(x["rule"] == "absolute-home-path" and x["path"] == "fixture.txt"
               for x in report["findings"])
    assert report["stale_allowlist"] == [{"path": "fixture.txt", "rule": "absolute-home-path"}]


def test_home_path_is_private_reference_and_large_blob_is_counted_without_reading(tmp_path):
    repo = _repo(tmp_path)
    (repo / "path.txt").write_text("/Users/alice/x", encoding="utf-8")
    (repo / "large.bin").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    _git(repo, "add", "."); _git(repo, "commit", "-qm", "fixtures")
    out = tmp_path / "out.json"
    result = _run("history", repo, "--json", out)
    report = json.loads(out.read_text())
    assert result.returncode == 1
    assert any(x["rule"] == "absolute-home-path" and x["path"] == "path.txt"
               for x in report["findings"])
    assert report["skipped"]["oversized"] == 1


def test_private_patterns_cover_normal_windows_paths_and_allow_anthropic_noreply(tmp_path):
    repo = _repo(tmp_path)
    (repo / "windows.txt").write_text(r"C:\Users\alice\x", encoding="utf-8")
    (repo / "safe-email.txt").write_text("noreply@anthropic.com", encoding="utf-8")
    _git(repo, "add", "."); _git(repo, "commit", "-qm", "private path fixtures")

    out = tmp_path / "out.json"
    result = _run("tracked", repo, "--json", out)
    findings = json.loads(out.read_text())["findings"]

    assert result.returncode == 1
    assert any(x["rule"] == "absolute-home-path" and x["path"] == "windows.txt" for x in findings)
    assert not any(x["rule"] == "email-address" and x["path"] == "safe-email.txt" for x in findings)


def test_human_evidence_omits_nonshipped_test_and_tool_paths(tmp_path):
    scan = _tool()
    report = {
        "mode": "history", "findings": [
            {"rule": "auth", "path": "tests/removed.py", "line": 7, "blob": "a" * 40},
            {"rule": "auth", "path": "tools/removed.py", "line": 8, "blob": "b" * 40},
        ], "skipped": {"oversized": 0, "binary": 0},
        "counts": {"legacy_issue_references": 0}, "stale_allowlist": [],
    }
    out = tmp_path / "evidence.json"
    scan._write(report, out)
    rendered = out.with_suffix(".md").read_text()
    assert "tests/removed.py" not in rendered and "tools/removed.py" not in rendered
    assert rendered.count("[historical non-shipped path]") == 2
    assert "aaaaaaaaaaaa" in rendered and "bbbbbbbbbbbb" in rendered


def test_refs_lists_remote_and_local_only_tags_without_network(monkeypatch, tmp_path, capsys):
    scan = _tool()
    repo = _repo(tmp_path)
    (repo / "readme").write_text("x", encoding="utf-8")
    _git(repo, "add", "."); _git(repo, "commit", "-qm", "initial")
    _git(repo, "tag", "local-only")
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-q", str(remote))
    _git(repo, "remote", "add", "origin", str(remote))
    result = scan.scan_refs(repo)
    assert result["remote"] == []
    assert result["local_only_tags"] == ["local-only"]


def test_line_scoped_entry_survives_an_unrelated_edit_of_the_file(tmp_path):
    repo, reviewed, entry = _fixture(tmp_path)
    green, report = _tracked(tmp_path, repo, [entry], "before")
    assert green.returncode == 0 and report["findings"] == [] and report["stale_allowlist"] == []

    (repo / "fixture.txt").write_text("# a different header\nunrelated\n" + reviewed + "\nmore\n",
                                      encoding="utf-8")
    _commit(repo)
    after, report = _tracked(tmp_path, repo, [entry], "after")
    assert after.returncode == 0 and report["findings"] == [] and report["stale_allowlist"] == []


def test_a_new_finding_added_to_an_allowlisted_file_still_fails(tmp_path):
    repo, reviewed, entry = _fixture(tmp_path)
    (repo / "fixture.txt").write_text("# header\n" + reviewed + "\npath = " + HOME + "other/two\n",
                                      encoding="utf-8")
    _commit(repo)
    result, report = _tracked(tmp_path, repo, [entry])
    assert result.returncode == 1
    assert [(x["rule"], x["path"], x["line"]) for x in report["findings"]] == [
        ("absolute-home-path", "fixture.txt", 2)]
    # the entry no longer equals what the file holds, so it must be re-triaged, not trusted
    assert report["stale_allowlist"] == [{"path": "fixture.txt", "rule": "absolute-home-path"}]


def test_a_new_finding_of_another_rule_in_an_allowlisted_file_still_fails(tmp_path):
    repo, reviewed, entry = _fixture(tmp_path)
    planted = "ghp_" + "q" * 36
    (repo / "fixture.txt").write_text("# header\n" + reviewed + "\n" + planted + "\n", encoding="utf-8")
    _commit(repo)
    result, report = _tracked(tmp_path, repo, [entry])
    assert result.returncode == 1 and planted not in result.stdout
    assert [x["rule"] for x in report["findings"]] == ["gh-token"]
    assert report["stale_allowlist"] == []          # the reviewed finding itself is unchanged


def test_one_more_copy_of_a_reviewed_line_is_not_covered(tmp_path):
    repo, reviewed, entry = _fixture(tmp_path)
    (repo / "fixture.txt").write_text("# header\n" + reviewed + "\n" + reviewed + "\n", encoding="utf-8")
    _commit(repo)
    result, report = _tracked(tmp_path, repo, [entry])
    assert result.returncode == 1
    assert [x["rule"] for x in report["findings"]] == ["absolute-home-path"]


def test_editing_the_reviewed_line_itself_fails_with_a_finding_and_a_stale_entry(tmp_path):
    repo, reviewed, entry = _fixture(tmp_path)
    (repo / "fixture.txt").write_text("# header\npath = " + HOME + "fixture/changed\n", encoding="utf-8")
    _commit(repo)
    result, report = _tracked(tmp_path, repo, [entry])
    assert result.returncode == 1
    assert [x["rule"] for x in report["findings"]] == ["absolute-home-path"]
    assert report["stale_allowlist"] == [{"path": "fixture.txt", "rule": "absolute-home-path"}]


def test_entry_whose_finding_was_removed_is_stale_and_fails(tmp_path):
    repo, reviewed, entry = _fixture(tmp_path)
    (repo / "fixture.txt").write_text("# header\nnothing to see\n", encoding="utf-8")
    _commit(repo)
    result, report = _tracked(tmp_path, repo, [entry])
    assert result.returncode == 1 and report["findings"] == []
    assert report["stale_allowlist"] == [{"path": "fixture.txt", "rule": "absolute-home-path"}]


def test_a_multiline_key_block_is_covered_in_whole_so_a_changed_body_fails(tmp_path):
    repo = _repo(tmp_path)
    begin, end = "-----BEGIN PRIVATE " + "KEY-----", "-----END PRIVATE " + "KEY-----"
    block = begin + "\n" + "A" * 64 + "\n" + end
    (repo / "k.txt").write_text("intro\n" + block + "\n", encoding="utf-8")
    _commit(repo)
    entry = {"path": "k.txt", "rule": "private-key", "reason": "synthetic", "lines": [_h(block)]}
    green, report = _tracked(tmp_path, repo, [entry], "green")
    assert green.returncode == 0 and report["findings"] == []

    (repo / "k.txt").write_text("intro\n" + block.replace("A" * 64, "B" * 64) + "\n", encoding="utf-8")
    _commit(repo)
    red, report = _tracked(tmp_path, repo, [entry], "red")
    assert red.returncode == 1
    assert [x["rule"] for x in report["findings"]] == ["private-key"]


def test_an_entry_scoped_to_neither_content_nor_blob_is_refused(tmp_path):
    repo, reviewed, entry = _fixture(tmp_path)
    bare = {"path": "fixture.txt", "rule": "absolute-home-path", "reason": "any content"}
    both = dict(entry, blob="a" * 40)
    draft = dict(entry, reason="TRIAGE REQUIRED: replace with the reviewed reason")
    private = dict(entry, rule="private-pattern-1")
    for bad in ([bare], [both], [draft], [private], [entry, dict(entry)], [dict(entry, lines=["not-a-hash"])],
                [dict(entry, lines=[])]):
        result, _ = _tracked(tmp_path, repo, bad)
        assert result.returncode == 2 and "allowlist" in result.stderr


def test_history_mode_never_applies_the_allowlist(tmp_path):
    repo, reviewed, entry = _fixture(tmp_path)
    allow = tmp_path / "allow.json"
    allow.write_text(json.dumps([entry]), encoding="utf-8")
    out = tmp_path / "history.json"
    result = _run("history", repo, "--allowlist", allow, "--json", out)
    report = json.loads(out.read_text())
    assert result.returncode == 1
    assert [x["rule"] for x in report["findings"]] == ["absolute-home-path"]
    assert report["stale_allowlist"] == []


def test_propose_writes_hashes_not_values_and_the_draft_needs_a_human_reason(tmp_path):
    repo, reviewed, entry = _fixture(tmp_path)
    draft = tmp_path / "draft.json"
    empty = tmp_path / "empty.json"
    empty.write_text("[]", encoding="utf-8")
    out = tmp_path / "out.json"
    result = _run("tracked", repo, "--allowlist", empty, "--propose", draft, "--json", out)
    assert result.returncode == 1
    proposed = json.loads(draft.read_text())
    assert [(x["path"], x["rule"], x["lines"]) for x in proposed] == [
        ("fixture.txt", "absolute-home-path", [_h(reviewed)])]
    assert HOME + "fixture" not in draft.read_text()
    # the evidence never carries the hash either: it names rule, path, line and blob only
    assert _h(reviewed) not in out.read_text() and _h(reviewed) not in out.with_suffix(".md").read_text()

    refused = _run("tracked", repo, "--allowlist", draft, "--json", tmp_path / "x.json")
    assert refused.returncode == 2 and "draft reason" in refused.stderr
    proposed[0]["reason"] = "reviewed fixture"
    draft.write_text(json.dumps(proposed), encoding="utf-8")
    assert _run("tracked", repo, "--allowlist", draft, "--json", tmp_path / "y.json").returncode == 0
    assert _run("history", repo, "--propose", draft).returncode == 2


def test_the_tracked_scan_of_this_tree_exits_0_with_no_stale_allowlist_entry(tmp_path):
    """The documented gesture on the working tree: every finding carried, no entry stale.

    The scan reads committed blobs, so the tracked paths (their working-tree bytes) are
    committed into a scratch repository first; untracked files are not what the gate scans.  Red only on
    a git checkout with the old tool (an archive has no .git and skips); no private-pattern file here.
    """
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    names = [n for n in _git(ROOT, "ls-files", "-z").split("\0")
             if n and (ROOT / n).is_file()]
    repo = _repo(tmp_path)
    for name in names:
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / name).read_bytes())
    _commit(repo, "tree")
    out = tmp_path / "tracked.json"
    result = _run("tracked", repo, "--json", out)
    report = json.loads(out.read_text())
    assert (result.returncode, report["findings"], report["stale_allowlist"]) == (0, [], [])


def test_tracked_mode_refuses_a_tree_whose_tracked_files_differ_from_head(tmp_path):
    repo, reviewed, entry = _fixture(tmp_path)
    (repo / "fixture.txt").write_text("# header\nnothing\n", encoding="utf-8")      # uncommitted edit
    result, report = _tracked(tmp_path, repo, [entry])
    assert result.returncode == 2 and "differ from HEAD" in result.stderr and report is None


def test_list_allowed_names_what_an_entry_covers_without_changing_the_verdict(tmp_path):
    repo, reviewed, entry = _fixture(tmp_path)
    allow = tmp_path / "allow.json"
    allow.write_text(json.dumps([entry]), encoding="utf-8")
    result = _run("tracked", repo, "--allowlist", allow, "--list-allowed", "--json", tmp_path / "o.json")
    assert result.returncode == 0
    assert any(row.startswith("allowed absolute-home-path fixture.txt:2 ") for row in result.stdout.splitlines())
    assert HOME + "fixture" not in result.stdout          # the preview is redacted like any finding


def test_propose_refuses_a_path_inside_the_repository_or_over_the_evidence(tmp_path):
    repo, reviewed, entry = _fixture(tmp_path)
    empty = tmp_path / "empty.json"
    empty.write_text("[]", encoding="utf-8")
    out = tmp_path / "out.json"
    for target in (repo / "draft.json", out, out.with_suffix(".md")):
        result = _run("tracked", repo, "--allowlist", empty, "--propose", target, "--json", out)
        assert result.returncode == 2 and "outside the repository" in result.stderr


def test_propose_replaces_an_entry_whose_reviewed_lines_were_partly_removed(tmp_path):
    repo = _repo(tmp_path)
    one, two = "a = " + HOME + "one/x", "b = " + HOME + "two/x"
    (repo / "f.txt").write_text(one + "\n" + two + "\n", encoding="utf-8")
    _commit(repo)
    entry = {"path": "f.txt", "rule": "absolute-home-path", "reason": "reviewed", "lines": [_h(one), _h(two)]}
    (repo / "f.txt").write_text(one + "\n", encoding="utf-8")
    _commit(repo)
    allow, draft = tmp_path / "allow.json", tmp_path / "draft.json"
    allow.write_text(json.dumps([entry]), encoding="utf-8")
    result = _run("tracked", repo, "--allowlist", allow, "--propose", draft, "--json", tmp_path / "o.json")
    assert result.returncode == 1 and "stale-allowlist" in result.stdout
    assert [x["lines"] for x in json.loads(draft.read_text())] == [[_h(one)]]


def test_crlf_line_endings_and_indentation_do_not_stale_a_single_line_entry(tmp_path):
    repo, reviewed, entry = _fixture(tmp_path)
    (repo / "fixture.txt").write_bytes(("# header\r\n    " + reviewed + "\r\n").encode("utf-8"))
    _commit(repo)
    result, report = _tracked(tmp_path, repo, [entry])
    assert result.returncode == 0 and report["stale_allowlist"] == []


def test_two_matches_on_one_line_are_counted_twice(tmp_path):
    repo = _repo(tmp_path)
    line = "x = " + HOME + "a/p and " + HOME + "b/q"
    (repo / "f.txt").write_text(line + "\n", encoding="utf-8")
    _commit(repo)
    once = {"path": "f.txt", "rule": "absolute-home-path", "reason": "reviewed", "lines": [_h(line)]}
    twice = dict(once, lines=[_h(line), _h(line)])
    red, report = _tracked(tmp_path, repo, [once], "once")
    assert red.returncode == 1 and [x["rule"] for x in report["findings"]] == ["absolute-home-path"]
    green, report = _tracked(tmp_path, repo, [twice], "twice")
    assert green.returncode == 0 and report["findings"] == []


def test_default_allowlist_is_read_from_head_not_the_working_tree(tmp_path):
    repo, reviewed, entry = _fixture(tmp_path)
    default = repo / "docs" / "launch" / "exposure-allowlist.json"
    default.parent.mkdir(parents=True)
    default.write_text(json.dumps([entry]), encoding="utf-8")        # present, but never committed
    out = tmp_path / "untracked.json"
    result = _run("tracked", repo, "--json", out)
    report = json.loads(out.read_text())
    assert result.returncode == 1 and report["allowlist"]["source"] == "none"
    assert [x["rule"] for x in report["findings"]] == ["absolute-home-path"]

    _commit(repo, "track the allowlist")
    out = tmp_path / "tracked.json"
    assert _run("tracked", repo, "--json", out).returncode == 0
    report = json.loads(out.read_text())
    assert report["allowlist"]["source"] == "HEAD" and report["allowlist"]["covered_findings"] == 1
    assert report["allowlist"]["blob"] == _git(repo, "rev-parse", "HEAD:docs/launch/exposure-allowlist.json")
    assert _h(reviewed) not in out.read_text()                      # counts and a blob id, never a hash of a line
