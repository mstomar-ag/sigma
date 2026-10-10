"""#945 (part B, slice 4): the stamp and the resolution record.

The library has no caller yet, so a closed upkeep gate changes nothing; the last tests pin that.

MEASURED HERE WITH REAL GIT (design doubts D-4 and D-42, and the seed on the continuation commit's signing and hooks):
  * a commit made at the stop is kept UNCHANGED by `git rebase --continue` (same sha);
  * a plain `git commit` at the stop takes the configured identity as author, so the original author is lost;
  * `rebase --continue` on a staged resolution runs only the `prepare-commit-msg` and `post-commit` hooks and never asks the
    signer (a failing signer does not stop it); a plain `git commit` at the stop runs all four hooks and asks the signer;
  * the engine's stamping commit (`--no-verify --no-gpg-sign`) runs exactly the same two hooks as the continuation and
    asks no signer, so it adds no hook the replay would not have run (see
    `test_continuation_commit_hooks_and_signing_measured`).
The GitHub half of D-4 (does a rewritten push re-reference a bare issue number) cannot be checked offline; the stamp and
the record carry no number, so it does not arise for them."""
import importlib.util
import json
import os
import pathlib
import stat
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"
sys.path.insert(0, str(ROOT / "tools"))

ORIG_NAME = "Original Author"
ORIG_EMAIL = "orig" + "@" + "example.invalid"
ENGINE_NAME = "Engine"
ENGINE_EMAIL = "engine" + "@" + "example.invalid"
ORIG_DATE = "2020-01-02T03:04:05+0200"
RUN_ID = "0123456789ab"
OK_TIP = "a" * 40
NEW_TIP = "b" * 40
DIGEST = "c" * 64


def _mod(name="feature_upkeep_resolution"):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m = _mod()


def _env(extra=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "GIT_EDITOR": "true",
                "GIT_AUTHOR_NAME": ENGINE_NAME, "GIT_AUTHOR_EMAIL": ENGINE_EMAIL,
                "GIT_COMMITTER_NAME": ENGINE_NAME, "GIT_COMMITTER_EMAIL": ENGINE_EMAIL})
    env.update(extra or {})
    return env


def git(cwd, *args, check=True, extra=None):
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, env=_env(extra))
    if check and p.returncode != 0:
        raise AssertionError("git %s failed: %s" % (" ".join(args), p.stderr or p.stdout))
    return p


def run(cwd, argv):
    """The kit's injected runner shape: stdout, raising on a non-zero exit."""
    p = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, env=_env())
    if p.returncode != 0:
        raise RuntimeError("%s failed: %s" % (" ".join(argv), p.stderr or p.stdout))
    return p.stdout


def _write(path, text):
    pathlib.Path(path).write_text(text, encoding="utf-8")


def stopped_repo(tmp_path, message="unit change (#5)\n\nbody line"):
    """A repository whose rebase of `unit` onto `main` is STOPPED on a conflict in `f`, the original commit authored by
    someone other than the configured identity."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    _write(repo / "f", "a\nb\nc\n")
    git(repo, "add", "f")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "checkout", "-q", "-b", "unit")
    _write(repo / "f", "a\nUNIT\nc\n")
    git(repo, "commit", "-q", "-am", message, "--author=%s <%s>" % (ORIG_NAME, ORIG_EMAIL), "--date=" + ORIG_DATE)
    original = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "checkout", "-q", "main")
    _write(repo / "f", "a\nMAIN\nc\n")
    git(repo, "commit", "-q", "-am", "main change")
    git(repo, "checkout", "-q", "unit")
    return repo, original


def stop(repo):
    p = git(repo, "rebase", "main", check=False)
    assert p.returncode != 0 and (repo / ".git" / "rebase-merge").exists(), "the rebase must stop on the conflict"


def resolve(repo):
    _write(repo / "f", "a\nBOTH\nc\n")
    git(repo, "add", "f")


def install_hooks(repo, log):
    for name in ("pre-commit", "commit-msg", "prepare-commit-msg", "post-commit"):
        hook = repo / ".git" / "hooks" / name
        hook.write_text("#!/bin/sh\necho %s >> %s\n" % (name, log))
        hook.chmod(hook.stat().st_mode | stat.S_IEXEC)


def failing_signer(repo, log):
    signer = repo / "failing-signer.sh"
    signer.write_text("#!/bin/sh\necho signer >> %s\nexit 1\n" % log)
    signer.chmod(signer.stat().st_mode | stat.S_IEXEC)
    git(repo, "config", "gpg.program", str(signer))
    git(repo, "config", "commit.gpgsign", "true")


# --------------------------------------------------------------------------- the stamp


def test_stamp_line_shape_and_refusals():
    assert m.stamp_line(1, RUN_ID) == "sigma-resolution: 1 " + RUN_ID
    for level in (0, 3, "1", True, None):
        with pytest.raises(m.StampError):
            m.stamp_line(level, RUN_ID)
    for bad in ("", "ABCDEF012345", "0123456789a", "0123456789abc", None, 5):
        with pytest.raises(m.StampError):
            m.stamp_line(1, bad)
    assert len(m.new_run_id()) == 12 and m.stamp_line(2, m.new_run_id())


def test_stamp_message_builds_the_blank_line_and_keeps_the_subject():
    out = m.stamp_message("unit change (#5)", 1, RUN_ID)
    assert out == "unit change (#5)\n\nsigma-resolution: 1 " + RUN_ID + "\n"
    body = m.stamp_message("unit change (#5)\n\nsome body\nmore body\n\n", 2, RUN_ID)
    assert body.startswith("unit change (#5)\n\nsome body\nmore body\n\nsigma-resolution: 2 ")
    assert body.split("\n")[0] == "unit change (#5)"
    for empty in ("", "  \n", None):
        with pytest.raises(m.StampError):
            m.stamp_message(empty, 1, RUN_ID)


def test_stamp_joins_an_existing_trailer_block():
    out = m.stamp_message("subject\n\nbody\n\nReviewed-by: Someone", 1, RUN_ID)
    assert out.endswith("Reviewed-by: Someone\nsigma-resolution: 1 %s\n" % RUN_ID)


def test_git_reads_the_stamp_as_a_trailer(tmp_path):
    repo, _ = stopped_repo(tmp_path)
    message = m.stamp_message("unit change (#5)", 1, RUN_ID)
    parsed = subprocess.run(["git", "interpret-trailers", "--parse"], input=message, capture_output=True, text=True,
                            env=_env(), cwd=str(repo))
    assert parsed.stdout.strip() == "sigma-resolution: 1 " + RUN_ID


def _arrived(subject):
    spec = importlib.util.spec_from_file_location("feature_rebase_arrival", SCRIPTS / "feature_rebase.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.arrived_through_a_pull_request(subject)


def test_stamp_without_a_blank_line_reads_as_direct_and_the_built_one_does_not(tmp_path):
    """The red control: the stamp appended on the next line joins the title (everything up to the first blank line), the
    `(#N)` anchor no longer ends it, and the commit reads as DIRECT. The built stamp leaves the title alone."""
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    _write(repo / "f", "x")
    git(repo, "add", "f")
    git(repo, "commit", "-q", "-m", "unit change (#5)\nsigma-resolution: 1 " + RUN_ID)
    joined = git(repo, "log", "-1", "--format=%s").stdout.strip()
    assert "sigma-resolution" in joined and _arrived(joined) is False
    git(repo, "commit", "-q", "--amend", "-m", m.stamp_message("unit change (#5)", 1, RUN_ID))
    built = git(repo, "log", "-1", "--format=%s").stdout.strip()
    assert built == "unit change (#5)" and _arrived(built) is True


@pytest.mark.parametrize("text,reason", [
    ("sigma-resolution: 1 0123456789ab and #12", "issue-number"),
    ("auto-resolved #915", "closing-keyword"),
    ("Resolves: #12", "closing-keyword"),
    ("owner/repo#7", "issue-number"),
    ("see the page issues/12 for it", None),
    ("https://example.invalid/o/r/issues/12", "issue-url"),
    ("resolution of the thing", None),
])
def test_wording_refuses_numbers_keywords_and_urls(text, reason):
    got = m.wording_problems(text)
    assert (reason in got) if reason else got == []


def test_wording_refuses_a_registered_marker_in_either_spelling():
    legacy = m._sibling("legacy")
    for marker in sorted(legacy.MARKERS)[:5]:
        assert "marker" in m.wording_problems("note: " + marker)
        assert "marker" in m.wording_problems(legacy.retired_spelling(marker)) if marker.startswith(("<!--", "sigma")) else True
    assert m.wording_problems(m.stamp_line(1, RUN_ID)) == []


def test_the_stamp_passes_the_commit_body_leak_scan():
    leak = importlib.import_module("leak_refs")
    patterns = leak.parse_patterns("internalcorp\nsecret-project\n")
    assert leak.find_hits(m.stamp_message("unit change (#5)", 1, RUN_ID), patterns) == []
    assert leak.find_hits("internalcorp thing", patterns), "the scan is armed (red control)"


def test_the_stamp_key_is_a_registered_non_marker():
    """The marker-literal pin sees a quoted `sigma-...` key. This one is new, has no retired spelling and is declared a
    name, not a marker, in the pin's own list."""
    pin = (ROOT / "tests" / "test_legacy_compat.py").read_text(encoding="utf-8")
    assert '"sigma-resolution"' in pin
    legacy = m._sibling("legacy")
    assert not any(m.STAMP_KEY.startswith(k) or k.startswith(m.STAMP_KEY) for k in legacy.MARKERS)
    with pytest.raises(ValueError):
        legacy.retired_spelling("Sigma-Resolution")


# --------------------------------------------------------------------------- the stamping commit


def test_stamping_commit_keeps_the_original_author_and_continue_keeps_the_commit(tmp_path):
    repo, original = stopped_repo(tmp_path)
    stop(repo)
    resolve(repo)
    sha = m.stamp_commit(run, repo, 1, RUN_ID)
    assert sha == git(repo, "rev-parse", "HEAD").stdout.strip()
    assert m.authorship_problems(run, repo, original, "HEAD") == []
    fields = git(repo, "log", "-1", "--format=%an|%ae|%ad|%cn", "--date=raw").stdout.strip().split("|")
    assert fields[0] == ORIG_NAME and fields[1] == ORIG_EMAIL and fields[2].startswith("1577927045 +0200")
    assert fields[3] == ENGINE_NAME, "the committer is the engine, which is true"
    assert git(repo, "log", "-1", "--format=%s").stdout.strip() == "unit change (#5)"
    assert git(repo, "log", "-1", "--format=%B").stdout.rstrip().endswith("sigma-resolution: 1 " + RUN_ID)
    git(repo, "rebase", "--continue")
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == sha, "--continue keeps a commit made at the stop"
    assert not (repo / ".git" / "rebase-merge").exists()


def test_a_plain_commit_at_the_stop_loses_the_author_and_the_comparison_refuses_it(tmp_path):
    """The red control for the stamping gesture: the plain gesture re-attributes the commit."""
    repo, original = stopped_repo(tmp_path)
    stop(repo)
    resolve(repo)
    git(repo, "commit", "-q", "-m", m.stamp_message("unit change (#5)", 1, RUN_ID))
    problems = m.authorship_problems(run, repo, original, "HEAD")
    assert "name" in problems and "email" in problems and "date" in problems


def test_stamp_commit_refuses_while_a_path_is_unmerged(tmp_path):
    repo, _ = stopped_repo(tmp_path)
    stop(repo)
    with pytest.raises(m.StampError):
        m.stamp_commit(run, repo, 1, RUN_ID)


def test_an_author_that_cannot_ride_through_the_flag_is_refused(tmp_path):
    repo, _ = stopped_repo(tmp_path)
    stop(repo)

    def odd(cwd, argv):
        out = run(cwd, argv)
        return out.replace(ORIG_NAME, "Odd <Name>") if "--format=%an" in " ".join(argv) else out
    with pytest.raises(m.StampError):
        m.original_authorship(odd, repo)


def test_continuation_commit_hooks_and_signing_measured(tmp_path):
    """MEASUREMENT (git 2.49, this machine). With hooks installed and a signer that always fails, `rebase --continue` on a
    staged resolution runs ONLY `prepare-commit-msg` and `post-commit` (never `pre-commit` or `commit-msg`) and never calls
    the signer: it succeeds. A plain `git commit` at the same stop runs all four hooks and calls the signer, which fails."""
    log = tmp_path / "log.txt"
    repo, _ = stopped_repo(tmp_path)
    install_hooks(repo, log)
    stop(repo)
    resolve(repo)
    failing_signer(repo, log)
    log.write_text("")
    plain = git(repo, "commit", "-q", "-m", "x", check=False)
    assert plain.returncode != 0 and "signer" in log.read_text().split(), "a plain commit asks the signer (control)"
    log.write_text("")
    done = git(repo, "rebase", "--continue", check=False)
    assert done.returncode == 0
    assert log.read_text().split() == ["prepare-commit-msg", "post-commit"], log.read_text()


def test_engine_stamping_commit_runs_the_same_hooks_as_the_continuation_and_asks_no_signer(tmp_path):
    log = tmp_path / "log.txt"
    repo, original = stopped_repo(tmp_path)
    install_hooks(repo, log)
    stop(repo)
    resolve(repo)
    failing_signer(repo, log)
    log.write_text("")
    sha = m.stamp_commit(run, repo, 2, RUN_ID)
    assert log.read_text().split() == ["prepare-commit-msg", "post-commit"], log.read_text()
    assert m.authorship_problems(run, repo, original, sha) == []
    git(repo, "rebase", "--continue")
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == sha


# --------------------------------------------------------------------------- the record


def _record(**kw):
    base = dict(unit="billing", level=1, run_id=RUN_ID, original_tip=OK_TIP, new_tip=NEW_TIP,
                backup_ref="refs/sigma/backup/billing/1", files={"CHANGELOG.md": DIGEST},
                checks={"markers": "pass", "baseline": "pass"}, at=1000)
    base.update(kw)
    return m.make_record(**base)


def test_make_record_validates_every_field():
    assert _record()["schema"] == m.SCHEMA_ID and "model" not in _record()
    two = _record(level=2, model="m", cost_usd=0.1, verdict="approve", route="process")
    assert two["verdict"] == "approve"
    for kw in ({"level": 3}, {"run_id": "xyz"}, {"original_tip": "short"}, {"new_tip": None}, {"backup_ref": ""},
               {"files": {}}, {"files": {"a": "nothex"}}, {"checks": {"k": 5}}):
        with pytest.raises(ValueError):
            _record(**kw)


def test_write_read_round_trip_and_path_shape(tmp_path):
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir()
    done = m.write_record(sdlc, _record())
    assert done.ok and done.path.name == "billing-%s.json" % RUN_ID and done.path.parent == sdlc / "state/upkeep/resolutions"
    assert m.read_record(sdlc, "billing", RUN_ID) == _record()
    assert m.read_record(sdlc, "Billing", RUN_ID) == _record(), "one unit key whatever the casing"
    assert m.read_record(sdlc, "billing", "f" * 12) is None


def test_write_refuses_bad_unit_wording_and_a_symlinked_store(tmp_path):
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir()
    assert m.write_record(sdlc, _record(unit="../escape")).reason == "bad-unit"
    assert m.write_record(sdlc, _record(backup_ref="refs/x/closes #12")).reason == "wording"
    assert m.write_record(sdlc, _record(files={"docs/#1.md": DIGEST})).ok, "a conflicted path is data, not prose"
    linked = tmp_path / "linked" / ".sdlc"
    (linked / "state" / "upkeep").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (linked / "state" / "upkeep" / "resolutions").symlink_to(elsewhere)
    assert m.write_record(linked, _record(run_id="ffffffffffff")).reason == "unwritable"
    assert list(elsewhere.iterdir()) == []


def test_read_is_total_on_hostile_content(tmp_path):
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir()
    path = m.store_path(sdlc, RUN_ID, "billing")
    path.parent.mkdir(parents=True)
    for text in ("", "not json", "[1]", '{"schema": "other"}', "[" * 100000, "x" * (m.MAX_RECORD_CHARS + 1)):
        path.write_text(text)
        assert m.read_record(sdlc, "billing", RUN_ID) is None


def test_prune_removes_only_old_record_shaped_files(tmp_path):
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir()
    old = m.write_record(sdlc, _record()).path
    new = m.write_record(sdlc, _record(run_id="ffffffffffff")).path
    other = old.parent / "notes.txt"
    other.write_text("keep")
    stale_tmp = old.parent / (".billing-eeeeeeeeeeee.json.tmp")
    stale_tmp.write_text("{}")
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    link = old.parent / "billing-dddddddddddd.json"
    link.symlink_to(outside)
    os.utime(link, (1000, 1000), follow_symlinks=False)
    for p in (old, stale_tmp):
        os.utime(p, (1000, 1000))
    removed = m.prune(sdlc, 14, now=1000 + 15 * 86400)
    assert removed == 2 and not old.exists() and not stale_tmp.exists()
    assert new.exists() is True or os.path.getmtime(new) < 1000 + 15 * 86400
    assert other.exists() and outside.exists() and (old.parent / "billing-dddddddddddd.json").is_symlink()
    with pytest.raises(ValueError):
        m.prune(sdlc, 0)
    assert m.prune(tmp_path / "absent", 3) == 0


def test_prune_keeps_a_young_record(tmp_path):
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir()
    path = m.write_record(sdlc, _record()).path
    assert m.prune(sdlc, 14, now=os.path.getmtime(path) + 13 * 86400) == 0 and path.exists()


# --------------------------------------------------------------------------- the ledger note and the comment


ON = {"ledger": {"enabled": True, "actor": "dana"}}


def _ledger_sdlc(tmp_path):
    sdlc = tmp_path / ".sdlc"
    (sdlc / "state").mkdir(parents=True)
    (sdlc / "config.json").write_text(json.dumps(ON))
    (sdlc / "state" / "STATE.md").write_text("# Loop State\niteration: 0\nrun_iteration: 0\nlast_run: none\n")
    return sdlc


def test_ledger_note_is_unaddressed_keyed_and_referenced(tmp_path):
    sdlc = _ledger_sdlc(tmp_path)
    entry = m.ledger_note(sdlc, ON, _record(unit="Billing"))
    assert entry["kind"] == "note" and entry["goal"] == "upkeep-billing"
    assert entry["ref"] == "upkeep:resolved:" + NEW_TIP[:12]
    assert not entry.get("to"), "an addressed note could start a paid run"
    assert "resolution %s" % RUN_ID in entry["why"]
    ledger = m._sibling("ledger")
    assert [e["ref"] for e in ledger.read_all(sdlc)] == [entry["ref"]]


def test_ledger_off_writes_nothing(tmp_path):
    sdlc = _ledger_sdlc(tmp_path)
    assert m.ledger_note(sdlc, {}, _record()) is None
    assert not (sdlc / "ledger").exists()


class Gh:
    def __init__(self, error=None):
        self.calls, self.error = [], error

    def __call__(self, args):
        self.calls.append(list(args))
        if self.error:
            raise self.error
        return '{"id": 1}'


def test_comment_posts_once_with_an_integer_finding_and_explicit_repo():
    gh = Gh()
    assert m.post_comment(gh, 12, "o/r", _record()) == {"posted": True, "reason": None}
    assert len(gh.calls) == 1 and gh.calls[0][:4] == ["api", "repos/o/r/issues/12/comments", "--method", "POST"]
    body = gh.calls[0][-1]
    assert body.startswith("body=") and "Backup ref" in body and "#" not in body


@pytest.mark.parametrize("finding,repo,reason", [
    ("12", "o/r", "bad-finding"), (0, "o/r", "bad-finding"), (True, "o/r", "bad-finding"), (None, "o/r", "bad-finding"),
    (12, None, "bad-repo"), (12, "no-slash", "bad-repo"), (12, "o/r --flag", "bad-repo"),
])
def test_comment_refuses_without_an_integer_finding_and_explicit_repo(finding, repo, reason):
    gh = Gh()
    assert m.post_comment(gh, finding, repo, _record()) == {"posted": False, "reason": reason}
    assert gh.calls == []


def test_comment_refuses_wording_and_reports_a_failed_post():
    gh = Gh()
    bad = _record(files={"docs/closes #4.md": DIGEST})
    assert m.post_comment(gh, 12, "o/r", bad) == {"posted": False, "reason": "wording"} and gh.calls == []
    gh_api = m._sibling("gh_api")
    broken = Gh(error=gh_api.GhApiError("boom"))
    assert m.post_comment(broken, 12, "o/r", _record())["reason"] == "gh-failed"


# --------------------------------------------------------------------------- pins and the closed gate


#: The only shipped files that reference the library; each is reached solely behind the upkeep gate.
REGISTERED_IMPORTERS = ["feature_rebase.py", "feature_upkeep_prove.py"]


def test_only_the_registered_importers_reference_the_library():
    """With the gate closed every existing path is byte-identical: only these gated callers reach this module."""
    offenders = []
    for path in sorted(ROOT.glob("skills/*/scripts/*.py")) + sorted(ROOT.glob("hooks/*.py")):
        if path.name != "feature_upkeep_resolution.py" and "feature_upkeep_resolution" in path.read_text(encoding="utf-8"):
            offenders.append(path.name)
    assert sorted(offenders) == REGISTERED_IMPORTERS


def test_the_library_does_not_read_the_gate_block():
    text = (SCRIPTS / "feature_upkeep_resolution.py").read_text(encoding="utf-8")
    assert '"upkeep"' not in text and "'upkeep'" not in text


def test_growth_disposition_names_the_store():
    docs = json.loads((ROOT / "docs" / "launch" / "dispositions" / "945.json").read_text(encoding="utf-8"))
    patterns = {d["pattern"] for d in docs}
    assert ".sdlc/state/upkeep/resolutions/" in patterns and ".sdlc/state/upkeep/resolutions/<unit>-<run>.json" in patterns
    assert all(d["issue"] == "#945" and d["pruner_or_cap"] and d["evidence"] for d in docs)
