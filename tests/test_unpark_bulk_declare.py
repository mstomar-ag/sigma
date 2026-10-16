"""#1562: `unpark.py declare --from-suggestions` and a suggester that reads clauses, not substrings.

Fake gh only, no network, no model calls.
"""
import json

import pytest

from test_park_kinds_1498 import _Repo, _park_c
from test_unpark import _config, _mod


def _rows(**by_number):
    return {"issues": [{"number": n, "title": "t", "state": "sdlc:parked", "labels": [], "suggested": k}
                       for n, k in by_number.items()], "complete": True, "assignee": None, "degraded": []}


def _wire(monkeypatch, tmp_path, rows):
    u = _mod("unpark")
    (tmp_path / "config.json").write_text(json.dumps(_config()))
    posted = []
    monkeypatch.setattr(u, "list_parked", lambda *a, **k: rows)
    monkeypatch.setattr(u, "declare", lambda d, c, n, kind, **k: posted.append((n, kind)) or (True, "declared %s on #%s" % (kind, n)))
    return u, posted


def test_bulk_declares_confident_and_skips_unknown_and_irreversible(monkeypatch, tmp_path, capsys):
    u, posted = _wire(monkeypatch, tmp_path, _rows(**{"1": "quota", "2": "unknown", "3": "irreversible", "4": "dependency"}))
    assert u.main(["unpark.py", "declare", str(tmp_path), "--from-suggestions"], run=None) == 0
    out = capsys.readouterr().out.splitlines()
    assert posted == [("1", "quota"), ("4", "dependency")]
    assert out[0] == "[declared] #1: quota" and out[3] == "[declared] #4: dependency"
    assert out[1].startswith("[skipped] #2: ") and out[2].startswith("[skipped] #3: ")
    assert "hard-stop" in out[2]


def test_dry_run_prints_the_same_lines_and_writes_nothing(monkeypatch, tmp_path, capsys):
    u, posted = _wire(monkeypatch, tmp_path, _rows(**{"1": "quota", "2": "unknown"}))
    assert u.main(["unpark.py", "declare", str(tmp_path), "--from-suggestions", "--dry-run"], run=None) == 0
    out = capsys.readouterr().out.splitlines()
    assert posted == []
    assert out[0] == "[declared] #1: quota" and out[1].startswith("[skipped] #2: ")


def test_kind_filter_restricts_to_that_suggestion(monkeypatch, tmp_path, capsys):
    u, posted = _wire(monkeypatch, tmp_path, _rows(**{"1": "quota", "4": "dependency"}))
    assert u.main(["unpark.py", "declare", str(tmp_path), "--from-suggestions", "--kind", "quota"], run=None) == 0
    assert posted == [("1", "quota")]
    assert "[skipped] #4" in capsys.readouterr().out


def test_kind_filter_refuses_an_unknown_kind(monkeypatch, tmp_path, capsys):
    u, posted = _wire(monkeypatch, tmp_path, _rows(**{"1": "quota"}))
    assert u.main(["unpark.py", "declare", str(tmp_path), "--from-suggestions", "--kind", "approval"], run=None) == 2
    assert posted == []


def test_a_failed_declaration_is_reported_and_exits_nonzero(monkeypatch, tmp_path, capsys):
    u, _ = _wire(monkeypatch, tmp_path, _rows(**{"1": "quota"}))
    monkeypatch.setattr(u, "declare", lambda *a, **k: (False, "the declaration comment did not post: x"))
    assert u.main(["unpark.py", "declare", str(tmp_path), "--from-suggestions"], run=None) == 1
    assert "[skipped] #1: the declaration comment did not post" in capsys.readouterr().out


def test_end_to_end_with_fake_gh_posts_one_comment_only_for_declared(tmp_path, capsys):
    repo = _Repo(comments=[_park_c("add a rate limit to the login endpoint; needs a product call")])
    (tmp_path / "config.json").write_text(json.dumps(_config()))
    u = _mod("unpark")
    assert u.main(["unpark.py", "declare", str(tmp_path), "--from-suggestions"], run=repo) == 0
    assert "[declared] #5: quota" in capsys.readouterr().out
    assert repo.bodies()[-1] == "Question kind set: quota\nsigma-qkind: quota"


def test_end_to_end_unknown_posts_nothing(tmp_path, capsys):
    repo = _Repo(comments=[_park_c("zzz")])
    (tmp_path / "config.json").write_text(json.dumps(_config()))
    u = _mod("unpark")
    before = len(repo.comments)
    assert u.main(["unpark.py", "declare", str(tmp_path), "--from-suggestions"], run=repo) == 0
    assert "[skipped] #5" in capsys.readouterr().out and len(repo.comments) == before


def test_single_declare_is_unchanged(tmp_path, capsys):
    repo = _Repo()
    (tmp_path / "config.json").write_text(json.dumps(_config()))
    u = _mod("unpark")
    assert u.main(["unpark.py", "declare", str(tmp_path), "5", "needs_decision"], run=repo) == 0
    assert repo.bodies()[-1] == "Question kind set: needs_decision\nsigma-qkind: needs_decision"


# --------------------------------------------------------------------------- the suggester

@pytest.mark.parametrize("text,kind", [
    ("we should drop table users", "irreversible"),
    ("drop table users before the migration", "irreversible"),
    ("delete the production bucket", "irreversible"),
    ("we will force push to main", "irreversible"),
    ("this is irreversible", "irreversible"),
    ("the change cannot be undone", "irreversible"),
    ("changes requested on the API", "needs_decision"),
    ("zzz", "unknown"),
    ("hit a rate limit", "quota"),
    ("rebase needed, merge conflict in loop.py", "merge_conflict"),
    ("ci fail on the unit suite", "failing_check"),
    ("depends on #12", "dependency"),
])
def test_existing_suggestions_are_unchanged(text, kind):
    assert _mod("loop")._suggest_kind(text) == kind


@pytest.mark.parametrize("text", [
    "the doc for `delete` is unclear; which approach do we document",
    "the detector for delete commands needs a decision, should we widen it",
    "Park reasons that mention the word delete are read by the suggester; should we change it",
    "redact delete tokens from the log output; should we",
])
def test_descriptive_deny_word_is_not_irreversible(text):
    assert _mod("loop")._suggest_kind(text) != "irreversible"
