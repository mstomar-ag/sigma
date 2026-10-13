"""verify_merge.py -- sigma-rebase slice 3 (#2306, epic #2303, design `.sdlc/design/2288.md` §6-§7).

WHY THESE TESTS RUN REAL `git` AND REAL SHELL COMMANDS for the verify half, mirroring
`test_rebase_brief.py`/`test_conflict_walk.py`'s own rationale: `run_verify_command` shells out via
`subprocess.run(cmd, shell=True, ...)`, and whether that captures both streams, reports the real
exit code and survives a bad `cwd` are properties of the real subprocess machinery, not of this
code. `gh` itself is never real here -- `tests/conftest.py`'s `_no_live_gh` autouse fixture makes
any un-injected `gh` call fail loudly by design, so every landing-PR / merge test below injects a
fake `run` that answers `gh` calls itself and delegates everything else (`git`) to the real runner,
exactly `test_rebase_brief.py::test_commit_context_falls_back_to_the_pr_description_via_gh`'s own
pattern.
"""
import importlib.util
import json
import os
import pathlib
import subprocess

import pytest
from journal_events import journal_events

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-rebase" / "scripts"


def _load(name, directory=SCRIPTS):
    spec = importlib.util.spec_from_file_location(name, pathlib.Path(directory) / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    if name == "verify_merge" and not os.environ.get("SIGMA_TEST_SHELL_POLICY_REAL"):
        # #707: `verify.command` needs the Git-local opt-in; these fixtures are the operator's repos.
        m.shell_policy.repository_shell_commands_allowed = lambda path: True
    return m


def _mod():
    return _load("verify_merge")


BASE = "main"
BRANCH = "feature/x"


# --------------------------------------------------------------------------- the real-git fixture
# (byte-for-byte the same shape as test_rebase_brief.py's/test_conflict_walk.py's own World -- this
# repo's own "no shared test-helper module" convention, see test_conflict_walk.py's _runner docstring)


def _git(cwd, *args):
    env = dict(os.environ)
    env.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
                "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull})
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, env=env)
    if p.returncode != 0:
        raise AssertionError("git %s failed in %s: %s" % (" ".join(args), cwd, p.stderr or p.stdout))
    return p.stdout.strip()


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _run(cwd, argv):
    """The kit's standard `(cwd, argv) -> stdout` contract, raising on a non-zero exit."""
    proc = subprocess.run([str(a) for a in argv], cwd=str(cwd), capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError("%s: %s" % (" ".join(str(a) for a in argv),
                                       (proc.stderr or proc.stdout).strip()))
    return proc.stdout.strip()


class World:
    """A throwaway bare remote plus ONE ordinary clone, checked out on `feature/x` -- the human's
    own live checkout, not an ephemeral worktree (design D-2)."""

    def __init__(self, root):
        self.root = pathlib.Path(root)
        self.remote = self.root / "remote.git"
        self.local = self.root / "local"

    def build(self):
        _git(self.root, "init", "-q", "--bare", str(self.remote))
        _git(self.root, "init", "-q", "-b", BASE, str(self.local))
        _git(self.local, "remote", "add", "origin", str(self.remote))
        _write(self.local / "seed.txt", "seed\n")
        _git(self.local, "add", "seed.txt")
        _git(self.local, "commit", "-q", "-m", "seed")
        _git(self.local, "push", "-q", "-u", "origin", BASE)
        _git(self.local, "checkout", "-q", "-b", BRANCH)
        _write(self.local / "f.txt", "f\n")
        _git(self.local, "add", "f.txt")
        _git(self.local, "commit", "-q", "-m", "feat: seed the feature branch (#1)")
        _git(self.local, "push", "-q", "-u", "origin", BRANCH)
        return self

    def sdlc(self, verify_command=None, ledger=False):
        d = self.local / ".sdlc"
        d.mkdir(exist_ok=True)
        cfg = {"work": {"base": BASE, "remote": "origin"}}
        if verify_command is not None:
            cfg["verify"] = {"command": verify_command}
        if ledger:
            cfg["ledger"] = {"enabled": True, "actor": "test-actor"}
        (d / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        return d


def _fake_run(gh_handlers, real_cwd=None):
    """A `(cwd, argv) -> stdout` runner: any `["gh", ...]` call is answered by the first matching
    handler (a `(predicate, response_or_exception)` pair, predicate over the full argv), everything
    else falls through to the REAL runner. Recording every call lets a test assert exactly what was
    (and was not) sent to `gh` -- e.g. that a merge call never carries a delete flag."""
    calls = []

    def run(cwd, argv):
        calls.append(list(argv))
        if argv and argv[0] == "gh":
            for predicate, response in gh_handlers:
                if predicate(argv):
                    if isinstance(response, Exception):
                        raise response
                    return response(argv) if callable(response) else response
            raise AssertionError("un-handled gh call in fake_run: %r" % (argv,))
        return _run(cwd, argv)

    run.calls = calls
    return run


# --------------------------------------------------------------------------- verify_command / run_verify_command (§6)


def test_verify_command_reads_the_configured_key():
    m = _mod()
    assert m.verify_command({"verify": {"command": "pytest -q"}}) == "pytest -q"


def test_verify_command_is_honestly_absent_when_unset():
    m = _mod()
    assert m.verify_command({}) is None
    assert m.verify_command({"verify": {}}) is None
    assert m.verify_command({"verify": {"command": ""}}) is None


def test_run_verify_command_reports_a_real_pass(tmp_path):
    m = _mod()
    result = m.run_verify_command("exit 0", str(tmp_path))
    assert result["ok"] is True
    assert result["exit"] == 0
    assert result["why"] is None


def test_run_verify_command_reports_a_real_failure_with_a_tail(tmp_path):
    m = _mod()
    result = m.run_verify_command(
        "echo one; echo two; echo three >&2; exit 7", str(tmp_path))
    assert result["ok"] is False
    assert result["exit"] == 7
    assert "one" in result["tail"] or "two" in result["tail"] or "three" in result["tail"]


def test_run_verify_command_never_raises_when_the_tree_does_not_exist(tmp_path):
    m = _mod()
    missing = tmp_path / "does-not-exist"
    result = m.run_verify_command("exit 0", str(missing))
    assert result["ok"] is False
    assert result["exit"] is None
    assert result["why"]


def test_run_verify_command_honours_a_monkeypatched_subprocess_run(tmp_path, monkeypatch):
    """The exact property this suite's own `_no_live_gh` guard depends on for every OTHER caller in
    this repo: `subprocess.run` is looked up fresh at call time, not bound at import time. Proven
    here directly, not just relied upon."""
    m = _mod()
    calls = []

    def fake(cmd, shell, capture_output, text, cwd):
        calls.append((cmd, cwd))
        return subprocess.CompletedProcess(cmd, 0, "patched-out", "")

    monkeypatch.setattr(subprocess, "run", fake)
    result = m.run_verify_command("anything", str(tmp_path))
    assert result["ok"] is True
    assert calls == [("anything", str(tmp_path))]


def test_format_verify_report_shows_pass_plainly():
    m = _mod()
    out = m.format_verify_report("pytest -q", "/repo",
                                 {"ok": True, "exit": 0, "ms": 12, "tail": [], "why": None})
    assert "PASSED" in out
    assert "pytest -q" in out
    assert "/repo" in out


def test_format_verify_report_shows_fail_with_tail_never_swallowed():
    m = _mod()
    out = m.format_verify_report(
        "pytest -q", "/repo",
        {"ok": False, "exit": 1, "ms": 5, "tail": ["FAILED test_x", "1 failed"], "why": None})
    assert "FAILED" in out
    assert "FAILED test_x" in out
    assert "1 failed" in out


def test_format_verify_report_names_a_could_not_even_start_failure():
    m = _mod()
    out = m.format_verify_report(
        "pytest -q", "/nope",
        {"ok": False, "exit": None, "ms": 0, "tail": [], "why": "No such file or directory"})
    assert "did not even run" in out
    assert "No such file or directory" in out


# --------------------------------------------------------------------------- _parse_created_pr_number


@pytest.mark.parametrize("out,expected", [
    ("https://github.com/acme/app/pull/123\n", 123),
    ("https://github.com/acme/app/pull/9", 9),
    ("some progress line\nhttps://github.com/acme/app/pull/42\n", 42),
    ("", None),
    ("no url here at all", None),
])
def test_parse_created_pr_number(out, expected):
    assert _mod()._parse_created_pr_number(out) == expected


# --------------------------------------------------------------------------- ensure_landing_pr (§7 steps 1-2)


def _issues_empty():
    return lambda argv: "pulls?head=" in " ".join(argv), "[]"


def _issues_error():
    return lambda argv: "pulls?head=" in " ".join(argv), RuntimeError("gh: could not resolve host")


def _issues_row(**row):
    payload = json.dumps([row])
    return lambda argv: "pulls?head=" in " ".join(argv), payload


def test_ensure_landing_pr_opens_a_new_one_when_none_exists(tmp_path):
    m = _mod()
    world = World(tmp_path).build()
    cwd = str(world.local)
    created_argv = []

    def create(argv):
        created_argv.append(argv)
        return "https://github.com/acme/app/pull/55\n"

    run = _fake_run([
        _issues_empty(),
        (lambda a: a[:3] == ["gh", "pr", "create"], create),
    ])
    result = m.ensure_landing_pr(run, cwd, {"work": {"base": BASE}}, BRANCH, BASE)
    assert result["outcome"] == m.CREATED
    assert result["number"] == 55
    assert created_argv[0][:3] == ["gh", "pr", "create"]
    assert "--base" in created_argv[0] and BASE in created_argv[0]
    assert "--head" in created_argv[0] and BRANCH in created_argv[0]
    assert "--fill" in created_argv[0]


def test_ensure_landing_pr_refuses_to_create_without_a_base(tmp_path):
    m = _mod()
    world = World(tmp_path).build()
    cwd = str(world.local)
    run = _fake_run([_issues_empty()])
    result = m.ensure_landing_pr(run, cwd, {}, BRANCH, "")
    assert result["outcome"] == m.REFUSED
    assert "work.base is empty" in result["why"]
    assert not any(c[:3] == ["gh", "pr", "create"] for c in run.calls)


def test_ensure_landing_pr_refuses_when_the_pr_read_fails(tmp_path):
    m = _mod()
    world = World(tmp_path).build()
    cwd = str(world.local)
    run = _fake_run([_issues_error()])
    result = m.ensure_landing_pr(run, cwd, {"work": {"base": BASE}}, BRANCH, BASE)
    assert result["outcome"] == m.REFUSED
    assert "risking a duplicate" in result["why"]
    assert not any(c[:3] == ["gh", "pr", "create"] for c in run.calls)


def test_ensure_landing_pr_uses_an_already_open_ready_pr_as_is(tmp_path):
    m = _mod()
    world = World(tmp_path).build()
    cwd = str(world.local)
    run = _fake_run([_issues_row(number=9, url="https://github.com/acme/app/pull/9",
                                 draft=False, state="open", merged_at=None)])
    result = m.ensure_landing_pr(run, cwd, {"work": {"base": BASE}}, BRANCH, BASE)
    assert result["outcome"] == m.EXISTS_READY
    assert result["number"] == 9
    assert not any(c[:2] == ["gh", "pr"] and c[2] in ("ready", "create") for c in run.calls)


def test_ensure_landing_pr_readies_an_open_draft(tmp_path):
    m = _mod()
    world = World(tmp_path).build()
    cwd = str(world.local)
    readied = []

    def ready(argv):
        readied.append(argv)
        return ""

    run = _fake_run([
        _issues_row(number=9, url="https://github.com/acme/app/pull/9",
                    draft=True, state="open", merged_at=None),
        (lambda a: a[:3] == ["gh", "pr", "ready"], ready),
    ])
    result = m.ensure_landing_pr(run, cwd, {"work": {"base": BASE}}, BRANCH, BASE)
    assert result["outcome"] == m.READIED
    assert result["number"] == 9
    assert readied == [["gh", "pr", "ready", "9"]]


def test_ensure_landing_pr_reports_a_refused_ready(tmp_path):
    m = _mod()
    world = World(tmp_path).build()
    cwd = str(world.local)
    run = _fake_run([
        _issues_row(number=9, url="https://github.com/acme/app/pull/9",
                    draft=True, state="open", merged_at=None),
        (lambda a: a[:3] == ["gh", "pr", "ready"], RuntimeError("422")),
    ])
    result = m.ensure_landing_pr(run, cwd, {"work": {"base": BASE}}, BRANCH, BASE)
    assert result["outcome"] == m.REFUSED
    assert "422" in result["why"]


def test_ensure_landing_pr_reports_already_merged_and_creates_nothing(tmp_path):
    m = _mod()
    world = World(tmp_path).build()
    cwd = str(world.local)
    run = _fake_run([_issues_row(number=3, url="https://github.com/acme/app/pull/3",
                                 draft=False, state="closed", merged_at="2026-09-09T00:00:00Z")])
    result = m.ensure_landing_pr(run, cwd, {"work": {"base": BASE}}, BRANCH, BASE)
    assert result["outcome"] == m.ALREADY_MERGED
    assert result["number"] == 3
    assert not any(c[:3] == ["gh", "pr", "create"] for c in run.calls)


def test_ensure_landing_pr_never_reopens_a_pr_a_human_closed(tmp_path):
    """The load-bearing property this function shares with `unit_completion._draft`: a landing PR
    a human already closed without merging is never reopened automatically."""
    m = _mod()
    world = World(tmp_path).build()
    cwd = str(world.local)
    run = _fake_run([_issues_row(number=3, url="https://github.com/acme/app/pull/3",
                                 draft=False, state="closed", merged_at=None)])
    result = m.ensure_landing_pr(run, cwd, {"work": {"base": BASE}}, BRANCH, BASE)
    assert result["outcome"] == m.DECLINED
    assert result["number"] == 3
    assert not any(c[:3] == ["gh", "pr", "create"] for c in run.calls)
    assert not any(c[:3] == ["gh", "pr", "ready"] for c in run.calls)


def test_ensure_landing_pr_reports_a_refused_create(tmp_path):
    m = _mod()
    world = World(tmp_path).build()
    cwd = str(world.local)
    run = _fake_run([
        _issues_empty(),
        (lambda a: a[:3] == ["gh", "pr", "create"], RuntimeError("could not create pull request")),
    ])
    result = m.ensure_landing_pr(run, cwd, {"work": {"base": BASE}}, BRANCH, BASE)
    assert result["outcome"] == m.REFUSED
    assert "could not create" in result["why"]


# --------------------------------------------------------------------------- merge_pr (§7 step 3)


def test_merge_pr_calls_a_plain_standalone_gh_pr_merge(tmp_path):
    m = _mod()
    world = World(tmp_path).build()
    cwd = str(world.local)
    run = _fake_run([(lambda a: a[:3] == ["gh", "pr", "merge"], "")])
    result = m.merge_pr(run, cwd, 55)
    assert result["ok"] is True
    assert run.calls == [["gh", "pr", "merge", "55", "--merge"]]


def test_merge_pr_never_carries_a_delete_flag():
    """§13b's hard invariant (BR-26), asserted directly against the exact argv sent to `gh`."""
    m = _mod()
    run = _fake_run([(lambda a: a[:3] == ["gh", "pr", "merge"], "")])
    m.merge_pr(run, "/repo", 1)
    assert not any("--delete" in c or "-d" in c for c in run.calls[0])


def test_merge_pr_reports_a_refused_merge_never_raises():
    m = _mod()
    run = _fake_run([(lambda a: a[:3] == ["gh", "pr", "merge"], RuntimeError("not mergeable"))])
    result = m.merge_pr(run, "/repo", 1)
    assert result["ok"] is False
    assert "not mergeable" in result["why"]


# --------------------------------------------------------------------------- record_merge (§7 step 4, D-6)


def _canonical_merged_pr(number, branch=BRANCH, sha="a" * 40):
    return json.dumps({"number": number, "node_id": "PR_%s" % number,
                       "created_at": "2026-01-01T00:00:00Z", "merged_at": "2026-01-02T00:00:00Z",
                       "merge_commit_sha": sha, "head": {"ref": branch},
                       "base": {"ref": BASE, "repo": {"node_id": "R_1"}}})


def test_feature_receipt_boundary_uses_the_loop_cli_not_a_direct_module_import(tmp_path):
    """Feature landing receipt writes stay a serialized cross-skill contract."""
    m = _mod(); d = tmp_path / ".sdlc"; d.mkdir()
    run = _fake_run([(lambda a: a[:3] == ["gh", "api", "repos/{owner}/{repo}/pulls/55"],
                     _canonical_merged_pr(55))])
    facts = m._persist_landing_receipt(str(d), run, str(tmp_path), BRANCH, 55)
    assert m._validated_landing_receipt(str(d), run, str(tmp_path), BRANCH, 55) == facts
    source = (SCRIPTS / "verify_merge.py").read_text(encoding="utf-8")
    assert '_load("merge_observation"' not in source
    assert '"feature-landing"' in source


def test_record_merge_writes_the_ledgers_reserved_merged_kind(tmp_path):
    m = _mod()
    d = tmp_path / ".sdlc"
    d.mkdir()
    config = {"ledger": {"enabled": True, "actor": "test-actor"}}
    run = _fake_run([(lambda a: a[:3] == ["gh", "api", "repos/{owner}/{repo}/pulls/55"],
                     _canonical_merged_pr(55))])
    m._persist_landing_receipt(str(d), run, str(tmp_path), "feature/x", 55)
    m.record_merge(str(d), config, "feature/x", 55, "PR #55 merged (merge).", run=run, cwd=str(tmp_path),
                   ownership=m.DURABLE_RECEIPT)
    entries = m.ledger.read_all(str(d))
    merged = [e for e in entries if e.get("kind") == "merged"]
    assert len(merged) == 1
    assert merged[0]["goal"] == "feature/x"
    assert merged[0]["pr"] == "55"
    receipt = _load("merge_observation", ROOT / "skills" / "sigma-loop" / "scripts")
    facts = {"canonical_repository_id": "R_1", "owner_kind": "unit", "owner_id": "feature/x",
             "goal": None, "head_ref": "feature/x", "base_ref": BASE, "pr_number": 55,
             "pr_node_id": "PR_55", "creating_writer": "verify_merge.ensure_landing_pr",
             "pr_created_at": "2026-01-01T00:00:00Z"}
    expected, _ = receipt.observation_keys(receipt.ownership_key(facts), "a" * 40)
    assert merged[0]["merged_entry_key"] == expected


def test_record_merge_uses_the_same_canonical_facts_for_the_journal(tmp_path):
    m = _mod(); d = tmp_path / ".sdlc"; d.mkdir()
    config = {"ledger": {"enabled": True, "actor": "test-actor"}, "journal": {"enabled": True}}
    run = _fake_run([(lambda a: a[:3] == ["gh", "api", "repos/{owner}/{repo}/pulls/55"],
                     _canonical_merged_pr(55))])
    m._persist_landing_receipt(str(d), run, str(tmp_path), BRANCH, 55)
    m.record_merge(str(d), config, BRANCH, 55, "why", run=run, cwd=str(tmp_path),
                   ownership=m.DURABLE_RECEIPT)
    events = journal_events(m.ledger, str(d))
    observed = [event for event in events if event.get("kind") == "merge_observed"]
    assert len(observed) == 1 and observed[0]["subject_kind"] == "branch"
    assert observed[0]["subject"] == BRANCH and observed[0]["pr"] == 55


def test_record_merge_retries_only_the_feature_landing_sink_that_is_pending(tmp_path, monkeypatch):
    m = _mod(); d = tmp_path / ".sdlc"; d.mkdir()
    config = {"ledger": {"enabled": True, "actor": "test-actor"}, "journal": {"enabled": True}}
    run = _fake_run([(lambda a: a[:3] == ["gh", "api", "repos/{owner}/{repo}/pulls/55"],
                     _canonical_merged_pr(55))])
    m._persist_landing_receipt(str(d), run, str(tmp_path), BRANCH, 55)
    real_append = m.ledger.safe_append
    monkeypatch.setattr(m.ledger, "safe_append", lambda *args, **kwargs:
                        None if args[1] == "merge_observed" else real_append(*args, **kwargs))
    m.record_merge(str(d), config, BRANCH, 55, "why", run=run, cwd=str(tmp_path))
    delivery = next((d / "state" / "feature-merge-deliveries").glob("*.json"))
    assert json.loads(delivery.read_text())["entry_delivered"] is True
    assert json.loads(delivery.read_text())["journal_delivered"] is False
    monkeypatch.setattr(m.ledger, "safe_append", real_append)
    m.record_merge(str(d), config, BRANCH, 55, "why", run=run, cwd=str(tmp_path))
    assert json.loads(delivery.read_text())["journal_delivered"] is True
    assert len([row for row in m.ledger.read_all(str(d)) if row.get("kind") == "merged"]) == 1
    assert len([row for row in journal_events(m.ledger, str(d)) if row.get("kind") == "merge_observed"]) == 1


def test_record_merge_is_a_true_no_op_when_the_ledger_is_off(tmp_path):
    m = _mod()
    d = tmp_path / ".sdlc"
    d.mkdir()
    m.record_merge(str(d), {}, "feature/x", 55, "why")
    assert m.ledger.read_all(str(d)) == []


def test_record_merge_never_raises_on_a_broken_config(tmp_path, monkeypatch):
    """`safe_append` is already fail-open by construction (module docstring) -- proven here by
    forcing `append` itself to blow up and confirming `record_merge` still returns quietly."""
    m = _mod()
    d = tmp_path / ".sdlc"
    d.mkdir()

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(m.ledger, "append", boom)
    run = _fake_run([(lambda a: a[:3] == ["gh", "api", "repos/{owner}/{repo}/pulls/1"],
                     _canonical_merged_pr(1))])
    m.record_merge(str(d), {"ledger": {"enabled": True}}, "feature/x", 1, "why", run=run,
                   cwd=str(tmp_path), ownership=m.DURABLE_RECEIPT)  # must not raise


# --------------------------------------------------------------------------- verify_and_offer_merge (§6-§7, the tail)


def _decide(value):
    calls = []

    def decide():
        calls.append(True)
        return value

    decide.calls = calls
    return decide


def test_tail_reports_no_command_and_never_touches_gh(tmp_path):
    m = _mod()
    run = _fake_run([])
    decide = _decide(True)
    report = m.verify_and_offer_merge(run, "/repo", str(tmp_path / ".sdlc"), {}, BRANCH, BASE, decide)
    assert report["outcome"] == m.NO_COMMAND
    assert decide.calls == []
    assert run.calls == []


def test_tail_stops_on_a_failed_verify_and_never_asks_to_merge(tmp_path):
    m = _mod()
    run = _fake_run([])
    decide = _decide(True)
    config = {"verify": {"command": "exit 1"}}
    report = m.verify_and_offer_merge(run, str(tmp_path), str(tmp_path / ".sdlc"), config,
                                      BRANCH, BASE, decide)
    assert report["outcome"] == m.VERIFY_FAILED
    assert report["verify"]["ok"] is False
    assert decide.calls == []           # never asked -- "always asked, only once green"
    assert run.calls == []              # no gh call was ever made


def test_tail_stops_when_the_human_says_no_and_leaves_everything_as_is(tmp_path):
    m = _mod()
    run = _fake_run([])
    decide = _decide(False)
    config = {"verify": {"command": "exit 0"}}
    report = m.verify_and_offer_merge(run, str(tmp_path), str(tmp_path / ".sdlc"), config,
                                      BRANCH, BASE, decide)
    assert report["outcome"] == m.DECLINED_MERGE
    assert decide.calls == [True]
    assert run.calls == []              # no landing-PR discovery, no gh call at all


def test_tail_lands_a_fresh_pr_end_to_end_and_records_the_ledger(tmp_path):
    m = _mod()
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir()
    config = {"verify": {"command": "exit 0"},
              "ledger": {"enabled": True, "actor": "test-actor"}}
    run = _fake_run([
        _issues_empty(),
        (lambda a: a[:3] == ["gh", "pr", "create"], "https://github.com/acme/app/pull/77\n"),
        (lambda a: a[:3] == ["gh", "pr", "merge"], ""),
        (lambda a: a[:3] == ["gh", "api", "repos/{owner}/{repo}/pulls/77"], _canonical_merged_pr(77)),
    ])
    decide = _decide(True)
    report = m.verify_and_offer_merge(run, str(tmp_path), str(sdlc), config, BRANCH, BASE, decide)
    assert report["outcome"] == m.MERGED
    assert report["landing"]["outcome"] == m.CREATED
    assert report["merge"]["ok"] is True
    entries = m.ledger.read_all(str(sdlc))
    merged = [e for e in entries if e.get("kind") == "merged"]
    assert len(merged) == 1 and merged[0]["pr"] == "77"


def test_tail_reports_a_failed_merge_and_never_records_a_ledger_entry(tmp_path):
    m = _mod()
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir()
    config = {"verify": {"command": "exit 0"},
              "ledger": {"enabled": True, "actor": "test-actor"}}
    run = _fake_run([
        _issues_empty(),
        (lambda a: a[:3] == ["gh", "pr", "create"], "https://github.com/acme/app/pull/77\n"),
        (lambda a: a[:3] == ["gh", "api", "repos/{owner}/{repo}/pulls/77"], _canonical_merged_pr(77)),
        (lambda a: a[:3] == ["gh", "pr", "merge"], RuntimeError("required check pending")),
    ])
    decide = _decide(True)
    report = m.verify_and_offer_merge(run, str(tmp_path), str(sdlc), config, BRANCH, BASE, decide)
    assert report["outcome"] == m.MERGE_FAILED
    assert m.ledger.read_all(str(sdlc)) == []


def test_tail_stops_at_landing_refusal_and_never_attempts_a_merge(tmp_path):
    m = _mod()
    run = _fake_run([_issues_error()])
    decide = _decide(True)
    config = {"verify": {"command": "exit 0"}}
    report = m.verify_and_offer_merge(run, str(tmp_path), str(tmp_path / ".sdlc"), config,
                                      BRANCH, BASE, decide)
    assert report["stage"] == "land"
    assert report["outcome"] == m.REFUSED
    assert not any(c[:3] == ["gh", "pr", "merge"] for c in run.calls)


def test_tail_stops_at_already_merged_and_never_attempts_a_merge(tmp_path):
    m = _mod()
    run = _fake_run([_issues_row(number=3, url="https://github.com/acme/app/pull/3",
                                 draft=False, state="closed", merged_at="2026-09-09T00:00:00Z")])
    decide = _decide(True)
    config = {"verify": {"command": "exit 0"}}
    report = m.verify_and_offer_merge(run, str(tmp_path), str(tmp_path / ".sdlc"), config,
                                      BRANCH, BASE, decide)
    assert report["outcome"] == m.ALREADY_MERGED
    assert not any(c[:3] == ["gh", "pr", "merge"] for c in run.calls)


def test_tail_does_not_attribute_a_preexisting_landing_pr_to_sigma(tmp_path):
    """A human-created, already-merged landing has no durable Sigma ownership proof."""
    m = _mod(); sdlc = tmp_path / ".sdlc"; sdlc.mkdir()
    config = {"verify": {"command": "exit 0"}, "ledger": {"enabled": True, "actor": "test-actor"}}
    run = _fake_run([
        _issues_row(number=55, url="https://github.com/acme/app/pull/55", draft=False,
                    state="closed", merged_at="2026-09-09T00:00:00Z"),
    ])
    report = m.verify_and_offer_merge(run, str(tmp_path), str(sdlc), config, BRANCH, BASE, _decide(True))
    assert report["outcome"] == m.ALREADY_MERGED
    assert m.ledger.read_all(str(sdlc)) == []
    assert any(call[:3] == ["gh", "api", "repos/{owner}/{repo}/pulls/55"] for call in run.calls)


def test_tail_reconciles_a_durably_owned_feature_landing_after_a_restart(tmp_path):
    """A PR created by this tail remains attributable after it lands while the process is gone."""
    m = _mod(); sdlc = tmp_path / ".sdlc"; sdlc.mkdir()
    config = {"verify": {"command": "exit 0"}, "ledger": {"enabled": True, "actor": "test-actor"},
              "journal": {"enabled": True}}
    created = _fake_run([
        _issues_empty(),
        (lambda a: a[:3] == ["gh", "pr", "create"], "https://github.com/acme/app/pull/55\n"),
        (lambda a: a[:3] == ["gh", "api", "repos/{owner}/{repo}/pulls/55"], _canonical_merged_pr(55)),
        (lambda a: a[:3] == ["gh", "pr", "merge"], RuntimeError("process interrupted")),
    ])
    first = m.verify_and_offer_merge(created, str(tmp_path), str(sdlc), config, BRANCH, BASE, _decide(True))
    assert first["outcome"] == m.MERGE_FAILED

    resumed = _fake_run([
        _issues_row(number=55, url="https://github.com/acme/app/pull/55", draft=False,
                    state="closed", merged_at="2026-09-09T00:00:00Z"),
        (lambda a: a[:3] == ["gh", "api", "repos/{owner}/{repo}/pulls/55"], _canonical_merged_pr(55)),
    ])
    report = m.verify_and_offer_merge(resumed, str(tmp_path), str(sdlc), config, BRANCH, BASE, _decide(True))
    assert report["outcome"] == m.ALREADY_MERGED
    entries = [row for row in m.ledger.read_all(str(sdlc)) if row.get("kind") == "merged"]
    events = [row for row in journal_events(m.ledger, str(sdlc)) if row.get("kind") == "merge_observed"]
    assert len(entries) == 1 and entries[0]["pr"] == "55"
    assert len(events) == 1 and events[0]["pr"] == 55


# --------------------------------------------------------------------------- main() -- the CLI (§6-§7)


def test_usage_is_reported_for_a_bad_invocation(capsys):
    m = _mod()
    rc = m.main(["verify_merge.py"])
    assert rc == 2


def test_main_refuses_cleanly_when_no_base_is_configured(tmp_path):
    m = _mod()
    world = World(tmp_path).build()
    d = world.local / ".sdlc"
    d.mkdir()
    (d / "config.json").write_text('{"work": {"remote": "origin"}}', encoding="utf-8")
    rc = m.main(["verify_merge.py", "land", str(d)])
    assert rc == 1


def test_main_refuses_cleanly_when_base_is_this_very_branch(tmp_path):
    m = _mod()
    world = World(tmp_path).build()
    d = world.sdlc(verify_command="exit 0")
    (d / "config.json").write_text(
        json.dumps({"work": {"base": BRANCH, "remote": "origin"}, "verify": {"command": "exit 0"}}),
        encoding="utf-8")
    rc = m.main(["verify_merge.py", "land", str(d)])
    assert rc == 1


def test_main_refuses_a_branch_that_is_not_checked_out(tmp_path):
    m = _mod()
    world = World(tmp_path).build()
    _git(world.local, "checkout", "-q", "-b", "feature/y")
    _git(world.local, "push", "-q", "-u", "origin", "feature/y")
    d = world.sdlc(verify_command="exit 0")
    rc = m.main(["verify_merge.py", "land", str(d), BRANCH])
    assert rc == 1


def test_main_refuses_while_a_rebase_is_still_stopped(tmp_path):
    m = _mod()
    world = World(tmp_path).build()
    # land a conflicting commit on base, then trigger a real stopped rebase
    cur = _git(world.local, "rev-parse", "--abbrev-ref", "HEAD")
    _git(world.local, "checkout", "-q", BASE)
    _write(world.local / "f.txt", "base changed\n")
    _git(world.local, "add", "f.txt")
    _git(world.local, "commit", "-q", "-m", "chore: base edits f.txt")
    _git(world.local, "push", "-q", "origin", BASE)
    _git(world.local, "checkout", "-q", cur)
    _write(world.local / "f.txt", "branch changed\n")
    _git(world.local, "add", "f.txt")
    _git(world.local, "commit", "-q", "-m", "feat: branch also edits f.txt")
    try:
        _run(str(world.local), ["git", "rebase", "origin/%s" % BASE])
    except Exception:
        pass
    d = world.sdlc(verify_command="exit 0")
    rc = m.main(["verify_merge.py", "land", str(d)])
    assert rc == 1
    _run(str(world.local), ["git", "rebase", "--abort"])


def test_main_reports_no_command_when_verify_is_unconfigured(tmp_path, capsys):
    m = _mod()
    world = World(tmp_path).build()
    d = world.sdlc()
    rc = m.main(["verify_merge.py", "land", str(d)])
    assert rc == 3
    assert "no verify.command" in capsys.readouterr().err


def test_main_reports_a_failed_verify_and_never_prompts(tmp_path, capsys, monkeypatch):
    m = _mod()
    world = World(tmp_path).build()
    d = world.sdlc(verify_command="exit 1")

    def boom(prompt=""):
        raise AssertionError("must never prompt after a failed verify")

    monkeypatch.setattr("builtins.input", boom)
    rc = m.main(["verify_merge.py", "land", str(d)])
    assert rc == 1
    assert "FAILED" in capsys.readouterr().out


def test_main_stops_cleanly_on_a_no_answer(tmp_path, capsys, monkeypatch):
    m = _mod()
    world = World(tmp_path).build()
    d = world.sdlc(verify_command="exit 0")
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    rc = m.main(["verify_merge.py", "land", str(d)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "PASSED" in out
    assert "not merging" in out


def test_main_lands_a_fresh_pr_end_to_end_on_a_yes_answer(tmp_path, capsys, monkeypatch):
    """The full wiring, proven together: `main()` resolves branch/base, verifies for real, prompts
    for real (via a monkeypatched `input`), discovers no landing PR, opens one and merges it -- all
    through the SAME `feature_rebase._run` `main()` itself reaches for, patched once at the module
    attribute so every caller (including `ensure_landing_pr`'s `unit_completion` calls) sees it."""
    m = _mod()
    world = World(tmp_path).build()
    d = world.sdlc(verify_command="exit 0", ledger=True)

    hybrid = _fake_run([
        _issues_empty(),
        (lambda a: a[:3] == ["gh", "pr", "create"], "https://github.com/acme/app/pull/88\n"),
        (lambda a: a[:3] == ["gh", "pr", "merge"], ""),
        (lambda a: a[:3] == ["gh", "api", "repos/{owner}/{repo}/pulls/88"], _canonical_merged_pr(88)),
    ])
    monkeypatch.setattr(m.feature_rebase, "_run", hybrid)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    rc = m.main(["verify_merge.py", "land", str(d)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASSED" in out
    assert "merged" in out.lower()
    entries = m.ledger.read_all(str(d))
    merged = [e for e in entries if e.get("kind") == "merged"]
    assert len(merged) == 1 and merged[0]["pr"] == "88"


def test_main_readies_an_existing_draft_then_merges_it(tmp_path, capsys, monkeypatch):
    m = _mod()
    world = World(tmp_path).build()
    d = world.sdlc(verify_command="exit 0")

    hybrid = _fake_run([
        _issues_row(number=21, url="https://github.com/acme/app/pull/21",
                    draft=True, state="open", merged_at=None),
        (lambda a: a[:3] == ["gh", "pr", "ready"], ""),
        (lambda a: a[:3] == ["gh", "pr", "merge"], ""),
    ])
    monkeypatch.setattr(m.feature_rebase, "_run", hybrid)
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")

    rc = m.main(["verify_merge.py", "land", str(d)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "marked ready" in out
    assert any(c[:3] == ["gh", "pr", "ready"] for c in hybrid.calls)
    assert any(c[:3] == ["gh", "pr", "merge"] for c in hybrid.calls)


# --- Review round 4 (author-blind Claude subagent, generation 44420a5f at 55e2d183) -------------------

def test_record_merge_writes_the_merged_entry_for_an_already_merged_pr_with_no_persisted_receipt(tmp_path):
    """B2. The base branch's `record_merge` writes `merged` unconditionally when the ledger wants it
    -- it has no ownership concept at all. This PR's `record_merge` required a LOCALLY PERSISTED
    parent receipt to validate before writing anything, so a landing PR this invocation did not
    itself create (an already-open PR from a prior session, a PR opened before this feature
    shipped, or a fresh `.sdlc` state directory) silently recorded NOTHING. The cycle-6 ruling
    already settled this shape once for work.py: the core's own facts are gated by
    ledger.enabled/journal_on, never by receipt ownership -- receipts are the separate, optional
    proof repository-wide acceptance needs. This applies the same ruling to the landing flow."""
    m = _mod(); d = tmp_path / ".sdlc"; d.mkdir()
    config = {"ledger": {"enabled": True, "actor": "test-actor"}}
    run = _fake_run([(lambda a: a[:3] == ["gh", "api", "repos/{owner}/{repo}/pulls/55"],
                     _canonical_merged_pr(55))])
    # No _persist_landing_receipt call: this landing PR was never "created" through this tool.
    m.record_merge(str(d), config, "feature/x", 55, "PR #55 already merged.", run=run, cwd=str(tmp_path))
    entries = m.ledger.read_all(str(d))
    merged = [e for e in entries if e.get("kind") == "merged"]
    assert len(merged) == 1, "an unowned landing PR's merge was never recorded"
    assert merged[0]["pr"] == "55"


def test_verify_and_offer_merge_records_an_already_merged_landing_with_no_receipt(tmp_path):
    """B2, at the real call site: `verify_and_offer_merge`'s ALREADY_MERGED branch only called
    `record_merge` when `landing.get("ownership") == DURABLE_RECEIPT`, so an already-merged landing
    PR this invocation never created skipped `record_merge` entirely -- the same defect, one layer
    up. Now it is unconditional, matching the base branch's own unconditional write."""
    m = _mod()
    world = World(tmp_path).build()
    cwd, d = str(world.local), tmp_path / ".sdlc"; d.mkdir()
    config = {"ledger": {"enabled": True, "actor": "test-actor"}, "verify": {"command": "true"}}
    run = _fake_run([
        _issues_row(number=3, url="https://github.com/acme/app/pull/3",
                    draft=False, state="closed", merged_at="2026-09-09T00:00:00Z"),
        (lambda a: a[:3] == ["gh", "api", "repos/{owner}/{repo}/pulls/3"], _canonical_merged_pr(3)),
    ])
    result = m.verify_and_offer_merge(run, cwd, str(d), config, BRANCH, BASE, decide=lambda: True)
    assert result["landing"]["outcome"] == m.ALREADY_MERGED
    merged = [e for e in m.ledger.read_all(str(d)) if e.get("kind") == "merged"]
    assert len(merged) == 1, "an already-merged, unowned landing PR was never recorded"


def test_ensure_landing_pr_readied_draft_then_merged_records_with_no_persisted_receipt(tmp_path):
    """B2, the other real path: a draft PR this session did not create is readied and merged. No
    receipt was ever persisted for it (only the CREATED path persists one), so `record_merge` must
    still write -- exactly the scenario the reviewer reproduced."""
    m = _mod()
    world = World(tmp_path).build()
    cwd, d = str(world.local), tmp_path / ".sdlc"; d.mkdir()
    config = {"ledger": {"enabled": True, "actor": "test-actor"}, "verify": {"command": "true"}}
    run = _fake_run([
        _issues_row(number=9, url="https://github.com/acme/app/pull/9",
                    draft=True, state="open", merged_at=None),
        (lambda a: a[:3] == ["gh", "pr", "ready"], ""),
        (lambda a: a[:3] == ["gh", "pr", "merge"], ""),
        (lambda a: a[:3] == ["gh", "api", "repos/{owner}/{repo}/pulls/9"], _canonical_merged_pr(9)),
    ])
    result = m.verify_and_offer_merge(run, cwd, str(d), config, BRANCH, BASE, decide=lambda: True)
    assert result["outcome"] == m.MERGED
    merged = [e for e in m.ledger.read_all(str(d)) if e.get("kind") == "merged"]
    assert len(merged) == 1, "a readied-draft landing was merged but never recorded"


def test_record_merge_delivery_write_error_never_fails_a_successful_landing(tmp_path, monkeypatch):
    """#6. `record_merge`'s own docstring promises "a completed landing must not be reported as
    failed", but `_write_delivery` was called unguarded -- an OSError there (full disk, read-only
    state dir) propagated straight out, contradicting the promise the very same function makes."""
    m = _mod(); d = tmp_path / ".sdlc"; d.mkdir()
    config = {"ledger": {"enabled": True, "actor": "test-actor"}}
    run = _fake_run([(lambda a: a[:3] == ["gh", "api", "repos/{owner}/{repo}/pulls/55"],
                     _canonical_merged_pr(55))])
    def boom(*_a, **_k):
        raise OSError("read-only file system")
    monkeypatch.setattr(m, "_write_delivery", boom)
    m.record_merge(str(d), config, "feature/x", 55, "why", run=run, cwd=str(tmp_path))


# --------------------------------------------------------------------------- the base's upkeep gate (#1341)


def _show_runner(base_text=None, fail=False):
    """A `(cwd, argv) -> stdout` fake answering only the read-only `git show <remote>/<base>:.sdlc/config.json`."""
    calls = []

    def run(cwd, argv):
        calls.append(list(argv))
        if fail:
            raise RuntimeError("fatal: path does not exist")
        return base_text

    run.calls = calls
    return run


_GATE_OPEN = json.dumps({"upkeep": {"enabled": True}})
_UNIT = "feature/x"


def test_unit_branch_with_gate_closed_in_tree_but_open_on_base_warns(capsys):
    m = _mod()
    run = _show_runner(_GATE_OPEN)
    got = m._engine_route({"work": {"base": BASE, "remote": "origin"}}, "/nonexistent", _UNIT,
                          lambda: pytest.fail("must not ask"), run=run, cwd="/x")
    err = capsys.readouterr().err
    assert got is None
    assert "upkeep" in err and "legacy" in err and "origin/main" in err
    assert run.calls == [["git", "show", "origin/main:.sdlc/config.json"]]


def test_gate_closed_in_both_prints_nothing(capsys):
    m = _mod()
    got = m._engine_route({"work": {"base": BASE}}, "/nonexistent", _UNIT, lambda: True,
                          run=_show_runner(json.dumps({"work": {}})), cwd="/x")
    out = capsys.readouterr()
    assert got is None and out.err == "" and out.out == ""


def test_unreadable_or_garbled_base_config_is_silent(capsys):
    m = _mod()
    for run in (_show_runner(fail=True), _show_runner("not json"), _show_runner("[1]")):
        assert m._engine_route({"work": {"base": BASE}}, "/nonexistent", _UNIT, lambda: True,
                               run=run, cwd="/x") is None
    assert capsys.readouterr().err == ""


def test_non_unit_branch_never_reads_the_base(capsys):
    m = _mod()
    run = _show_runner(_GATE_OPEN)
    assert m._engine_route({"work": {"base": BASE}}, "/nonexistent", "sdlc/1", lambda: True,
                           run=run, cwd="/x") is None
    assert run.calls == [] and capsys.readouterr().err == ""


def test_gate_open_in_tree_does_not_read_the_base(monkeypatch, capsys):
    m = _mod()
    run = _show_runner(_GATE_OPEN)
    engine = type("E", (), {"land": staticmethod(lambda *a, **k: {"outcome": "merged"})})
    real = m._load
    monkeypatch.setattr(m, "_load", lambda n, d=None: engine if n == "feature_land" else real(n, d))
    got = m._engine_route({"upkeep": {"enabled": True}}, "/nonexistent", _UNIT, lambda: True,
                          run=run, cwd="/x")
    assert got == 0 and run.calls == [] and capsys.readouterr().err == ""
def test_engine_route_refuses_a_pair_unit_before_any_pass_or_pr(tmp_path, capsys):
    """#1339, through the documented gesture's engine route: exit 1, the refusal printed, no pass, no PR."""
    from test_feature_land_front import World, CONFIG, load as load_front
    from test_feature_land_front import pair_unit
    m = _mod()
    w = World(tmp_path)
    pair_unit(w)
    engine = load_front()

    class Engine:
        def land(self, config, sdlc_dir, unit, **kw):
            return engine.land(config, w.sdlc, unit, run=w.run, gh_run=w.gh, rebase_pass=w.rebase_pass,
                               verify=w.verify, **kw)
    real = m._load
    m._load = lambda name, directory=None: Engine() if name == "feature_land" else real(name, directory)
    assert m._engine_route(CONFIG, str(w.sdlc), "feature/voice", lambda: True) == 1
    assert "refused:sibling" in capsys.readouterr().out
    assert w.passes == [] and w.posts() == []


def test_engine_route_prompt_does_not_claim_verification_and_closed_prompt_is_unchanged(monkeypatch):
    """#1346: the engine route verifies INSIDE the landing, so its question cannot say 'Verify passed';
    the closed path's own prompt stays byte-identical."""
    m = _mod()
    asked = []
    def fake_input(prompt=""):
        asked.append(prompt)
        return "y"
    monkeypatch.setattr("builtins.input", fake_input)
    assert m._interactive_land_decide() is True
    assert m._interactive_decide() is True
    assert asked[0] == "Land the unit now? Verification runs first. [y/N]: "
    assert "Verify passed" not in asked[0]
    assert asked[1] == "Verify passed. Merge now? [y/N]: "


def test_main_gives_the_engine_route_the_landing_prompt_and_the_closed_path_the_old_one(tmp_path, monkeypatch):
    m = _mod()
    seen = {}
    monkeypatch.setattr(m.state, "load_config", lambda _d: {})
    monkeypatch.setattr(m.rebase_brief, "resolve_branch", lambda *_a: "feature/x")
    monkeypatch.setattr(m.rebase_brief, "resolve_base", lambda _c: "main")
    monkeypatch.setattr(m.rebase_brief, "current_branch", lambda *_a: "feature/x")
    monkeypatch.setattr(m.feature_rebase, "rebase_stopped", lambda *_a: False)
    def route(config, sdlc_dir, branch, decide, **_kw):
        seen["engine"] = decide
        return 0
    monkeypatch.setattr(m, "_engine_route", route)
    assert m.main(["verify_merge.py", "land", str(tmp_path / ".sdlc")]) == 0
    assert seen["engine"] is m._interactive_land_decide
