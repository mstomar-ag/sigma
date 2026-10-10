import hashlib, pathlib, importlib.util, tempfile, os, subprocess, sys, json, re, shlex

import pytest

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _rc():
    spec = importlib.util.spec_from_file_location("review_context", S / "review_context.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def _repo(d):
    """A minimal adopted repo: .sdlc with a north-star + project.md, a CLAUDE.md, a frozen contract."""
    root = pathlib.Path(d)
    base = root / ".sdlc"; (base / "context").mkdir(parents=True); (base / "plans").mkdir()
    (base / "context" / "north-star.md").write_text(
        "## Vision\nShip acme/widget.\n## Strategy\nNon-goals: no multi-tenant.\n"
        "## Architecture\n1. UI holds no business logic.\n", encoding="utf-8")
    (base / "project.md").write_text("# acme/widget\nStack: python. verify: pytest.\n", encoding="utf-8")
    (root / "CLAUDE.md").write_text("Rule: no client strings.\n", encoding="utf-8")
    contracts = root / "docs" / "CONTRACTS"; contracts.mkdir(parents=True)
    (contracts / "api.md").write_text("Status: FROZEN\n", encoding="utf-8")
    return str(base), str(root)


def test_brief_is_project_informed_and_author_blind():
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        out = _rc().brief(base, "make the button blue", "plan-review", repo_root=root)
        # project-informed: the north-star and its rules are in the pack
        assert "north-star" in out and "UI holds no business logic" in out
        assert "no multi-tenant" in out          # non-goals the plan is judged against
        assert "CLAUDE.md" in out and "FROZEN" in out
        # author-blind: it frames the reader as the INDEPENDENT reviewer and points at the code
        assert "INDEPENDENT reviewer" in out
        assert "blast radius" in out.lower() and "whole repository" in out
        # the goal is carried; the maker's reasoning is not (there is no field for it)
        assert "make the button blue" in out
        assert "reasoning" in out.lower()        # only ever "you have not seen the author's reasoning"
        assert out.lower().count("author") >= 1


def test_every_phase_names_its_own_artifact():
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        rc = _rc()
        assert "PLAN" in rc.brief(base, "g", "plan-review", repo_root=root).upper() or \
               ".sdlc/plans" in rc.brief(base, "g", "plan-review", repo_root=root)
        assert "diff" in rc.brief(base, "g", "code-review", repo_root=root).lower()
        pr = rc.brief(base, "g", "pr-review", artifact="42", repo_root=root)
        assert "#42" in pr and "gh pr diff 42" in pr
        assert "intent" in rc.brief(base, "g", "retro", repo_root=root).lower() or \
               "goal it claimed" in rc.brief(base, "g", "retro", repo_root=root)


def test_goal_given_as_a_file_path_is_read():
    """Local mode passes a goal FILE path — the brief must carry the file's intent, not the path."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        goal_file = pathlib.Path(d) / ".sdlc" / "goals" / "0007.md"
        goal_file.parent.mkdir(parents=True, exist_ok=True)
        goal_file.write_text("---\nid: 0007\n---\nAdd a dark-mode toggle to the settings page.\n",
                             encoding="utf-8")
        out = _rc().brief(base, str(goal_file), "plan-review", repo_root=root)
        assert "dark-mode toggle" in out


def test_github_bare_issue_number_points_to_the_issue():
    """Github mode passes a bare issue NUMBER; the reviewer must be told to read the issue for the
    acceptance criteria, not handed just the digits."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        out = _rc().brief(base, "42", "pr-review", artifact="42", repo_root=root)
        assert "issue #42" in out and "gh issue view 42" in out
        assert "acceptance criteria" in out


def test_unknown_phase_is_a_loud_error():
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        try:
            _rc().brief(base, "g", "sign-off", repo_root=root)
            assert False, "expected ValueError on unknown phase"
        except ValueError as exc:
            assert "unknown --for" in str(exc)


def test_fail_open_when_project_docs_absent():
    """A repo with no north-star/project.md/CLAUDE.md still yields a usable brief — the missing
    section drops (never fabricated, never a crash) and the reviewer instruction + artifact pointer
    are always present.

    The absence is now DECLARED rather than silent: fail-open keeps the review running, but a
    reviewer that doesn't know what it was denied returns a confident verdict on partial inputs,
    which reads exactly like a real pass."""
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"; base.mkdir(parents=True)
        out = _rc().brief(str(base), "some goal", "code-review", repo_root=d)
        assert "INDEPENDENT reviewer" in out and "blast radius" in out.lower()
        assert "## north-star" not in out              # no fabricated section, no crash
        assert "alignment cannot be judged" in out     # ...but the gap is stated, not hidden


def test_brief_requires_the_structured_verdict_the_review_gate_parses():
    """A process reviewer must know the exact verdict line run_resolved_review accepts."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        out = _rc().brief(base, "some goal", "code-review", repo_root=root)
        assert "`VERDICT: approve`" in out
        assert "`VERDICT: block`" in out
        assert "Do not use any other verdict spelling." in out


def test_cli_ascii_safe_under_c_locale():
    """The printed brief must survive a non-utf8 locale (there is a C-locale test elsewhere)."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        env = dict(os.environ, LC_ALL="C", PYTHONIOENCODING="ascii")
        p = subprocess.run([sys.executable, str(S / "review_context.py"), "brief", base,
                            "make it blue", "--for", "plan-review", "--repo-root", root],
                           capture_output=True, text=True, env=env)
        assert p.returncode == 0, p.stderr
        assert "INDEPENDENT reviewer" in p.stdout


def test_cli_missing_for_flag_usages_out():
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        p = subprocess.run([sys.executable, str(S / "review_context.py"), "brief", base, "g"],
                           capture_output=True, text=True)
        assert p.returncode == 2 and "usage:" in p.stderr


def test_cli_parses_artifact_and_repo_root_flags():
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        p = subprocess.run([sys.executable, str(S / "review_context.py"), "brief", base, "g",
                            "--for", "pr-review", "--artifact", "42", "--repo-root", root],
                           capture_output=True, text=True)
        assert p.returncode == 0 and "#42" in p.stdout
        assert "FROZEN" in p.stdout               # --repo-root was honored (contracts found under it)


def test_cli_unknown_verb_and_bad_phase():
    p = subprocess.run([sys.executable, str(S / "review_context.py"), "explain"],
                       capture_output=True, text=True)
    assert p.returncode == 2 and "usage:" in p.stderr
    with tempfile.TemporaryDirectory() as d:
        base, _ = _repo(d)
        p2 = subprocess.run([sys.executable, str(S / "review_context.py"), "brief", base, "g",
                            "--for", "sign-off"], capture_output=True, text=True)
        assert p2.returncode == 2 and "unknown --for" in p2.stderr


# --- the dossier: closing the blast-radius gap the module docstring names ------------------------
# A fresh reviewer "cannot see BLAST RADIUS". Research already measured it and stored the sweep
# commands verbatim — handing those over turns "trace the callers yourself" into a checkable start.

def _dossier_at(base, name, body="| BR-1 | src/pay.py:42 | charge() | caller | in-scope |"):
    research = pathlib.Path(base) / "research"; research.mkdir(parents=True, exist_ok=True)
    (research / (name + ".md")).write_text(
        "# Research\n**Queries (re-run these at Review):**\n- `grep -rn charge src/`\n" + body,
        encoding="utf-8")


def test_dossier_is_included_and_its_queries_are_flagged_for_rerun():
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        goals = pathlib.Path(base) / "goals"; goals.mkdir(exist_ok=True)
        goal = goals / "0007-fix-retry.md"; goal.write_text("---\nstatus: pending\n---\nfix it\n")
        _dossier_at(base, "0007-fix-retry")
        out = _rc().brief(base, str(goal), "plan-review", repo_root=root)
        assert "src/pay.py:42" in out and "grep -rn charge src/" in out
        assert "landed" in out and "AFTER research" in out       # told WHY to re-run them


def test_dossier_found_under_the_bare_slug_too():
    """Goals are `NNNN-slug.md`; Research may file the dossier under either form."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        goals = pathlib.Path(base) / "goals"; goals.mkdir(exist_ok=True)
        goal = goals / "0007-fix-retry.md"; goal.write_text("---\nstatus: pending\n---\nfix it\n")
        _dossier_at(base, "fix-retry")
        assert "src/pay.py:42" in _rc().brief(base, str(goal), "plan-review", repo_root=root)


def test_dossier_found_for_a_github_mode_goal_with_a_descriptive_slug_suffix():
    """#1808: in github mode `$goal` is a BARE issue number with no slug of its own, but Research
    routinely appends a descriptive one when it files the dossier
    (`.sdlc/research/1756-repo-identity-declared-override.md`, sigma-research/SKILL.md's own
    `<goal-slug>` instruction) -- confirmed live on goal #1756/PR #1806, where this produced a false
    "No Research dossier: blast radius was never measured" claim against a dossier that was
    genuinely on disk. `phase_doc_file`'s stem/slug matching only ever checked the EXACT numeric
    stem (`1756.md`) or the numeric stem stripped of everything after its first dash, which for a
    dash-free bare number is a no-op -- so a real, correctly-filed dossier under a slug suffix was
    invisible to it."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        _dossier_at(base, "1756-repo-identity-declared-override")
        src = _FakeSource({"1756": {"title": "Repo identity", "body": "AC: declared override wins."}})
        out = _rc().brief(base, "1756", "plan-review", repo_root=root, source=src)
        assert "src/pay.py:42" in out                        # the dossier IS in the pack
        assert "blast radius was never measured" not in out  # the false gap must be gone


def test_dossier_is_project_evidence_not_the_makers_reasoning():
    """It records what the code IS, never why the author chose what they chose — so including it
    cannot reintroduce the anchoring this module exists to remove."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        out = _rc().brief(base, "a goal", "plan-review", repo_root=root)
        assert "author's reasoning" in out and "INDEPENDENT reviewer" in out


# --- ABSENT never PASS: fail-open must not mean fail-silent --------------------------------------

def test_a_missing_dossier_is_stated_not_hidden():
    """An under-briefed reviewer returning a confident 'no issues' is worse than a biased one: the
    verdict is indistinguishable from a real pass."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        out = _rc().brief(base, "a goal", "plan-review", repo_root=root)
        assert "Inputs NOT available" in out
        assert "blast radius was never measured" in out
        assert "ABSENT, never PASS" in out


def test_a_nonexistent_artifact_tells_the_reviewer_to_stop():
    """The exact failure the plan-persistence gap produced: an independent reviewer pointed at a plan
    file the Plan phase never wrote, reviewing from the goal text and calling it clean."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        out = _rc().brief(base, "a goal", "plan-review",
                          artifact=str(pathlib.Path(d) / ".sdlc" / "plans" / "gone.md"), repo_root=root)
        assert "does not exist" in out and "nothing to review" in out


def test_a_complete_brief_carries_no_gap_notice():
    """The notice must stay rare, or it stops being read."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        goals = pathlib.Path(base) / "goals"; goals.mkdir(exist_ok=True)
        goal = goals / "0001-g.md"; goal.write_text("---\nstatus: pending\n---\ndo it\n")
        _dossier_at(base, "0001-g")
        plan = pathlib.Path(base) / "plans" / "0001-g.md"; plan.write_text("the plan\n")
        out = _rc().brief(base, str(goal), "plan-review", artifact=str(plan), repo_root=root)
        assert "Inputs NOT available" not in out


def test_pr_review_artifact_is_a_number_not_a_path():
    """A PR number must never be probed as a filesystem path and reported missing."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        out = _rc().brief(base, "a goal", "pr-review", artifact="42", repo_root=root)
        assert "does not exist" not in out


def test_a_drop_in_repo_is_told_what_it_cannot_judge():
    """No north-star is legitimate — but the reviewer must know alignment is out of scope rather
    than silently reporting a clean strategic review it never performed."""
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"; base.mkdir()
        out = _rc().brief(str(base), "a goal", "plan-review", repo_root=d)
        assert "alignment cannot be judged" in out


def test_plan_phase_persists_the_plan_the_reviewer_is_pointed_at():
    """review_context points plan-review at `.sdlc/plans/`; the Plan phase must actually write there
    or the independent reviewer arrives with nothing, and hard_plan_gate denies every edit."""
    t = (pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-plan" / "SKILL.md").read_text()
    assert ".sdlc/plans/" in t
    assert "hard_plan_gate" in t and "review.independent" in t


# --- #500: pinning safe-by-construction Path(goal).stem sites (no work.stem() reduction needed) ---

def test_phase_doc_safe_from_traversal_via_stem_extraction():
    """#500: _phase_doc() uses Path(goal).stem to extract only the filename, preventing any `../`-bearing
    goal from escaping .sdlc/<subdir>/. Path(goal).stem removes both directory components and file
    extensions, so even if goal is `../../../etc/passwd`, stem is just `passwd`, and the lookup stays
    confined to that subdirectory. One resolver now serves research/ AND plans/, so the guard is proven
    for both rather than only the one it was written for."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        rc = _rc()
        for subdir in ("research", "plans"):
            target = pathlib.Path(base) / subdir; target.mkdir(parents=True, exist_ok=True)
            # create a safe artifact the confined lookup should land on
            (target / "passwd.md").write_text("# Doc\n**Queries:**\n- safe query\n", encoding="utf-8")
            # a traversal attempt should read from .sdlc/<subdir>/passwd.md, not escape
            result = rc._phase_doc(base, "../../../etc/passwd", subdir)
            assert "safe query" in result, f"goal with ../ should read from {subdir}/, not escape it"
            # confirm the escape directory DOES NOT exist (proof the traversal failed)
            assert not (pathlib.Path(d) / "etc" / "passwd.md").exists(), \
                f"traversal must not access files outside .sdlc/{subdir}/"


# --- plan grounding: the code reviewer must be able to trace the diff against the plan ---

def test_code_review_brief_carries_the_plan():
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        (pathlib.Path(base) / "plans" / "0007-blue-button.md").write_text(
            "# plan\n## Recolor the button\n", encoding="utf-8")
        out = _rc().brief(base, "0007-blue-button.md", "code-review", repo_root=root)
        assert "## Recolor the button" in out
        assert "unplanned scope" in out


def test_plan_resolves_by_bare_slug_too():
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        (pathlib.Path(base) / "plans" / "blue-button.md").write_text(
            "# plan\n## Recolor the button\n", encoding="utf-8")
        out = _rc().brief(base, "0007-blue-button.md", "code-review", repo_root=root)
        assert "## Recolor the button" in out


def test_absent_plan_is_declared_not_silently_dropped():
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        out = _rc().brief(base, "0007-blue-button.md", "code-review", repo_root=root)
        assert "No plan for this goal" in out


def test_plan_review_does_not_duplicate_the_plan_it_already_reviews():
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        (pathlib.Path(base) / "plans" / "0007-blue-button.md").write_text(
            "# plan\n1. Recolor the button.\n", encoding="utf-8")
        out = _rc().brief(base, "0007-blue-button.md", "plan-review", repo_root=root)
        assert "unplanned scope" not in out


# --- #258: the plan-review brief names the exact bytes under review ---------------------------------

def test_plan_sha256_hashes_raw_bytes():
    """#258: the one plan hash the brief, the record writer and the `pr` gate all share. Raw bytes,
    never decoded or stripped -- `work._run` strips stdout, so a hash of `git show` output could never
    match a file hash, and two plans differing only by a trailing newline must not collide."""
    with tempfile.TemporaryDirectory() as d:
        with_nl, without_nl = pathlib.Path(d, "a.md"), pathlib.Path(d, "b.md")
        with_nl.write_bytes(b"# plan\n"); without_nl.write_bytes(b"# plan")
        rc = _rc()
        assert rc.plan_sha256(with_nl) == hashlib.sha256(b"# plan\n").hexdigest()
        assert rc.plan_sha256(without_nl) == hashlib.sha256(b"# plan").hexdigest()
        assert rc.plan_sha256(with_nl) != rc.plan_sha256(without_nl)


def test_plan_review_brief_names_the_plan_file_and_its_sha256():
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        plan = pathlib.Path(base) / "plans" / "0007-blue-button.md"
        plan.write_bytes(b"# plan\n1. Recolor the button.\n")
        out = _rc().brief(base, "0007-blue-button.md", "plan-review", repo_root=root)
        found = re.search(r"^Plan sha256: ([0-9a-f]{64})$", out, re.M)
        assert found and found.group(1) == hashlib.sha256(plan.read_bytes()).hexdigest()
        assert ("Plan file: " + plan.as_posix()) in out.splitlines()


def test_plan_review_brief_without_a_plan_has_no_plan_identity():
    """A PIN of today's behaviour, not a red-first test: it passes before #258 and no single edit of
    the new code makes it fail, since a plan that does not exist cannot be hashed."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        out = _rc().brief(base, "0007-blue-button.md", "plan-review", repo_root=root)
        assert "Plan sha256:" not in out


def test_code_review_brief_does_not_carry_the_plan_identity():
    """Passes before #258 too; its control is C11(ii) (the phase check widened to every phase)."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        (pathlib.Path(base) / "plans" / "0007-blue-button.md").write_text("# plan\n", encoding="utf-8")
        out = _rc().brief(base, "0007-blue-button.md", "code-review", repo_root=root)
        assert "Plan sha256:" not in out


def test_plan_review_brief_falls_back_to_the_branch_copy_without_naming_the_worktree():
    """#258 B1: the main checkout has no plan and the goal's worktree carries it committed. The brief
    hashes the branch copy and points at it with the runnable `git show` shape `_branch_doc` already
    hands a reviewer -- never the worktree path, which `reviewer.py check --scratch` refuses."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _goal_branch_repo(d)
        wt = pathlib.Path(root) / ".sdlc" / "work" / "0007-blue-button"
        _git(root, "worktree", "add", "-q", str(wt), "sdlc/0007-blue-button")
        record = pathlib.Path(base) / "state" / "work" / "0007-blue-button.json"
        record.parent.mkdir(parents=True)
        record.write_text(json.dumps({"worktree": str(wt), "branch": "sdlc/0007-blue-button",
                                      "base": "main", "remote": "origin", "pr": ""}))
        rc = _rc()
        out = rc.brief(base, "0007-blue-button.md", "plan-review", repo_root=root)
        sha = hashlib.sha256((wt / ".sdlc" / "plans" / "0007-blue-button.md").read_bytes()).hexdigest()
        assert ("Plan sha256: " + sha) in out.splitlines()
        assert ("Plan file: git -C %s show sdlc/0007-blue-button:.sdlc/plans/0007-blue-button.md"
                % pathlib.Path(base).resolve().parent) in out.splitlines()
        identity = out.split("## The plan under review, by content", 1)[1].split("\n\n## ", 1)[0]
        assert ".sdlc/work/" not in identity
        assert rc._load("reviewer")._check_brief_text(out, [str(wt), str(wt.resolve())]) == (True, [])


# --- intent grounding: a github-mode goal is a NUMBER; the brief must carry its body, not a pointer ---

class _FakeSource:
    """Recording fake for sources.GitHubSource — the same seam sources.py's own tests use."""

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def fetch_title_body(self, goal):
        self.calls.append(str(goal))
        return self.mapping.get(str(goal), {"title": "", "body": ""})


def test_github_mode_inlines_the_issue_body():
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        src = _FakeSource({"42": {"title": "Blue button", "body": "AC: the button is blue."}})
        out = _rc().brief(base, "42", "code-review", repo_root=root, source=src)
        assert "AC: the button is blue." in out      # the intent is IN the pack
        assert "Blue button" in out
        assert "#42" in out                          # the number is still cited
        assert src.calls == ["42"]                   # exactly one fetch


def test_github_fetch_failure_degrades_to_the_pointer_and_never_raises():
    class _Boom:
        def fetch_title_body(self, goal):
            raise RuntimeError("gh exploded")

    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        out = _rc().brief(base, "42", "code-review", repo_root=root, source=_Boom())
        assert "gh issue view 42" in out             # the pre-existing pointer survives
        assert "could not be fetched" in out         # and the gap is declared, not hidden


def test_local_mode_never_fetches():
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        src = _FakeSource({})
        out = _rc().brief(base, "make the button blue", "code-review", repo_root=root, source=src)
        assert src.calls == []
        assert "make the button blue" in out


# --- a decomposition child must be judged against the whole it was split out of ------------------

def test_decomposition_child_carries_its_parent_goal():
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        src = _FakeSource({
            "42": {"title": "Child 3 of 5",
                   "body": "sigma:decomposed-from=7\nDo the third slice."},
            "7": {"title": "Parent epic", "body": "Ship the whole widget."},
        })
        out = _rc().brief(base, "42", "code-review", repo_root=root, source=src)
        assert "Ship the whole widget." in out
        assert "ONE piece of a larger goal" in out
        assert src.calls == ["42", "7"]           # child then parent, one level only


def test_a_goal_with_no_parent_marker_fetches_no_parent():
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        src = _FakeSource({"42": {"title": "Standalone", "body": "Just do it."}})
        out = _rc().brief(base, "42", "code-review", repo_root=root, source=src)
        assert src.calls == ["42"]
        assert "ONE piece of a larger goal" not in out


def test_parent_marker_number_stops_at_the_first_digit_run():
    """#688 finding 5: the number after the marker must be the LEADING digit run, not every digit
    anywhere in the line remainder concatenated together. A marker line with a trailing digit past
    the number (a hypothetical future format, e.g. "...=#7 (part 2)") must still resolve parent #7,
    never the wrong issue #72 a naive digit-scan-and-join would silently produce."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        src = _FakeSource({
            "42": {"title": "Child 2 of 2",
                   "body": "sigma:decomposed-from=7 (part 2)\nDo the second slice."},
            "7": {"title": "Parent epic", "body": "Ship the whole widget."},
        })
        out = _rc().brief(base, "42", "code-review", repo_root=root, source=src)
        assert "Ship the whole widget." in out
        assert src.calls == ["42", "7"]           # the real parent, never "72"


# --- cost: the plan's OUTLINE is the contract; its body is what the diff already shows -----------

def test_plan_block_carries_the_outline_not_the_body():
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        (pathlib.Path(base) / "plans" / "0007-blue-button.md").write_text(
            "# Plan\n## Task 1 - recolor `button.css`\nSome very long implementation prose here.\n"
            "## Definition of done\nMore prose.\n", encoding="utf-8")
        out = _rc().brief(base, "0007-blue-button.md", "code-review", repo_root=root)
        assert "## Task 1 - recolor `button.css`" in out      # the contract: what was agreed
        assert "## Definition of done" in out
        assert "Some very long implementation prose" not in out   # the body is NOT carried
        assert "0007-blue-button.md" in out                   # ...but the path is, to read on demand


def test_plan_outline_keeps_the_brief_small():
    """The regression this fixes: a 50KB plan must not become a 50KB brief."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        big = "# Plan\n" + "".join(
            "## Task %d\n%s\n" % (i, "filler prose line\n" * 200) for i in range(1, 11))
        (pathlib.Path(base) / "plans" / "0007-blue-button.md").write_text(big, encoding="utf-8")
        out = _rc().brief(base, "0007-blue-button.md", "code-review", repo_root=root)
        assert len(big) > 30_000                    # the plan really is large
        assert len(out) < 10_000                    # the brief is not
        assert "## Task 10" in out                  # every heading survived


def test_plan_outline_ignores_hashes_inside_code_fences():
    """Measured: .sdlc/plans/100.md has 32 fences holding 29 lines that start with `#` - Python
    comments. A naive startswith would present those to the reviewer as plan structure."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        (pathlib.Path(base) / "plans" / "0007-blue-button.md").write_text(
            "## Task 1 - recolor\n"
            "```python\n"
            "# this is a code comment, not a heading\n"
            "## neither is this\n"
            "```\n"
            "## Task 2 - ship it\n", encoding="utf-8")
        out = _rc().brief(base, "0007-blue-button.md", "code-review", repo_root=root)
        assert "## Task 1 - recolor" in out and "## Task 2 - ship it" in out
        assert "code comment" not in out
        assert "neither is this" not in out


# --- #2237: a plan's correction to the research dossier must reach the diff-review brief, not just
# the (possibly now-wrong) research text -----------------------------------------------------------

def test_plan_correction_reaches_the_diff_review_brief_2237():
    """#2237: `review_context.py brief` embeds `.sdlc/research/<n>.md` verbatim, and — before this
    fix — only ever showed the PLAN's headings (see `test_plan_block_carries_the_outline_not_the_body`
    just above), never its body. So a plan section that corrects something the research dossier got
    wrong reached the reviewer as an unopened heading sitting beside the stale research claim stated
    as plain fact — the correction's actual TEXT never arrived. The heading below deliberately does
    NOT contain "shared_settings" itself, so this is a true test of whether the body reaches the
    brief, not a coincidental heading match."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        _dossier_at(base, "9001-rotation-config",
                    body="The rotation config lives in the `legacy_config` table today.")
        (pathlib.Path(base) / "plans" / "9001-rotation-config.md").write_text(
            "# Plan\n\n## 1. Design\n\n"
            "### 1.1 **[corrected]** Config location\n\n"
            "Research said the rotation config lives in `legacy_config`; that was wrong -- verified "
            "against the current code, it actually lives in the new `shared_settings` table.\n\n"
            "### 1.2 Rollout\n\nNothing about the correction here.\n", encoding="utf-8")

        out = _rc().brief(base, "9001-rotation-config.md", "code-review", repo_root=root)

        assert "legacy_config" in out                       # the stale research claim still arrives
        assert "shared_settings" in out                  # ...and now so does the correction's TEXT
        assert "the new `shared_settings` table" in out  # the whole corrective sentence, not a scrap
        assert "Nothing about the correction here" not in out  # sibling section stays outline-only
        assert out.index("legacy_config") < out.index("shared_settings")  # correction sits after the claim


def test_plan_correction_not_flagged_leaves_only_the_heading():
    """Control on the control: a plan section whose heading does NOT read as a correction must stay
    outline-only, exactly like every other plan section — proves the new block is additive, not a
    second full-body dump of the whole plan."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        (pathlib.Path(base) / "plans" / "9002-unrelated.md").write_text(
            "# Plan\n## Task 1 - wire the button\nSome long prose nobody needs restated.\n",
            encoding="utf-8")
        out = _rc().brief(base, "9002-unrelated.md", "code-review", repo_root=root)
        assert "## Task 1 - wire the button" in out
        assert "Some long prose nobody needs restated" not in out


# --- #1762 false negative: a github-mode goal's research can live in an issue COMMENT, never a file,
# and the missing-dossier gap must not assert "never measured" when it did -----------------------

class _FakeSourceWithComments:
    """Recording fake for sources.GitHubSource exposing BOTH the seam _fetch_issue already uses
    (fetch_title_body) and the one _research_note_posted uses (fetch_comments_strict) -- the same
    duck-typed method loop.py/feature_owner.py/feature_propagate.py already call this way, returning
    its real raw shape: {"comments": [...], "labels": [...]}."""

    def __init__(self, title_body, comments):
        self.title_body = title_body
        self.comments = comments
        self.calls = []

    def fetch_title_body(self, goal):
        self.calls.append(("title_body", str(goal)))
        return self.title_body

    def fetch_comments_strict(self, goal):
        self.calls.append(("comments_strict", str(goal)))
        return {"comments": self.comments, "labels": []}


def test_research_note_detector_matches_both_conventions_and_rejects_prose():
    """SKILL.md prescribes lowercase 'research: <summary>'; the live convention actually observed on
    a real issue (#1762, confirmed via `gh issue view 1762 --json comments`) is uppercase
    'RESEARCH (lane: medium): ...'. Both must be detected; a human comment merely containing the
    word must not be, or the gap notice would go silent on goals that never had research at all."""
    with tempfile.TemporaryDirectory() as d:
        base, _ = _repo(d)
        rc = _rc()
        for body in ["research: measured 3 call sites.",
                     "RESEARCH (lane: medium): Read src/store.py..."]:
            src = _FakeSourceWithComments({}, [{"body": body}])
            assert rc._research_note_posted(base, "1762", source=src) is True, body
        prose = _FakeSourceWithComments({}, [{"body": "Research shows this pattern is safer overall."}])
        assert rc._research_note_posted(base, "1762", source=prose) is False


def test_missing_dossier_but_research_note_on_issue_rephrases_the_gap_honestly():
    """The exact false negative this fixes: no local .sdlc/research/1762.md, but the issue thread
    already carries a Research finding -- the old wording ("blast radius was never measured for this
    goal") is a flat falsehood in this case and must be gone; the reviewer must be told the truth
    instead, without the comment's own text (possibly the maker's reasoning) being inlined."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        src = _FakeSourceWithComments(
            {"title": "Fix the thing", "body": "AC: it is fixed."},
            [{"body": "RESEARCH (lane: medium): read store.py, found 3 call sites."}])
        out = _rc().brief(base, "1762", "code-review", repo_root=root, source=src)
        assert "blast radius was never measured" not in out
        assert "issue's comment thread" in out
        assert "found 3 call sites" not in out            # the comment's own text is never inlined
        assert ("comments_strict", "1762") in src.calls
        out.encode("ascii")                                # must survive a non-utf8 locale too


def test_missing_dossier_and_no_research_comment_keeps_the_strong_original_gap():
    """No file AND nothing research-shaped in the thread -- this is a REAL gap, so the notice must
    keep pushing the reviewer to trace callers rather than going quiet in github mode generally."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        src = _FakeSourceWithComments(
            {"title": "Fix the thing", "body": "AC: it is fixed."},
            [{"body": "Promoted to sdlc:goal via /sigma-promote."}])
        out = _rc().brief(base, "1762", "code-review", repo_root=root, source=src)
        assert "blast radius was never measured for this goal" in out


def test_research_comment_check_skipped_when_dossier_file_already_exists():
    """The file already answers the question -- spending a `gh` round-trip to re-ask it would just
    be cost with no payoff, and _missing()'s own rule is to stay rare or stop being read."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        _dossier_at(base, "1762")
        src = _FakeSourceWithComments({"title": "t", "body": "b"}, [])
        _rc().brief(base, "1762", "code-review", repo_root=root, source=src)
        assert ("comments_strict", "1762") not in src.calls


def test_research_comment_check_skipped_in_local_mode():
    """A local-mode goal is never a bare issue number; the github-only comment check must not fire
    (or attempt any gh call) for it, and the original wording must still cover the real local gap."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        src = _FakeSourceWithComments({"title": "t", "body": "b"},
                                       [{"body": "RESEARCH (lane: medium): should never be asked."}])
        out = _rc().brief(base, "a local-mode goal text", "code-review", repo_root=root, source=src)
        assert src.calls == []
        assert "blast radius was never measured for this goal" in out


def test_research_comment_check_still_fires_when_issue_has_a_blank_title_and_body():
    """goal_is_pointer=True does NOT only mean the issue fetch failed -- _issue_text also returns ""
    (setting goal_is_pointer=True the same way) for an issue that was fetched FINE but genuinely has
    a blank title and body. That's a normal, valid issue state, not a sign gh/network is broken, and
    it must not be treated as a reason to skip a real Research comment sitting on that same thread --
    the comment check must still fire and still catch it."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        src = _FakeSourceWithComments({"title": "", "body": ""},   # fetched fine, just empty
                                       [{"body": "RESEARCH (lane: medium): read store.py."}])
        out = _rc().brief(base, "1762", "code-review", repo_root=root, source=src)
        assert ("comments_strict", "1762") in src.calls
        assert "blast radius was never measured" not in out
        assert "issue's comment thread" in out


def test_research_comment_check_fails_open_on_a_raising_source():
    """fetch_comments_strict is deliberately the RAISING sibling of fetch_title_body (sources.py) --
    an unreadable timeline must degrade to the original wording, never crash the whole brief."""
    class _BoomComments:
        def fetch_title_body(self, goal):
            return {"title": "t", "body": "b"}

        def fetch_comments_strict(self, goal):
            raise RuntimeError("gh exploded")

    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        out = _rc().brief(base, "1762", "code-review", repo_root=root, source=_BoomComments())
        assert "blast radius was never measured for this goal" in out


def test_research_comment_check_with_no_source_injected_fails_open():
    """The real production path (source=None, matching the CLI's own call shape): no
    .sdlc/config.json exists in this fixture, so _fetch_issue's OWN identical fallback fails first,
    setting goal_is_pointer=True before research_note_seen is even computed -- proving brief()'s
    OVERALL behavior stays safe end to end. This does not, by itself, exercise
    _research_note_posted's own fallback logic (see the direct test right below, which does)."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        out = _rc().brief(base, "1762", "code-review", repo_root=root)
        assert "blast radius was never measured for this goal" in out


def test_research_note_posted_own_fallback_fails_open_with_no_config():
    """Direct, isolated test of _research_note_posted's OWN source=None fallback, bypassing brief()
    entirely -- brief()'s _fetch_issue call hits an identical fallback FIRST and (in this fixture,
    with no config.json) fails first, which masks whether _research_note_posted's own resolution
    (state.load_config -> sources.get_source -> fetch_comments_strict) is even reachable or correct.
    Calling it directly proves ITS fallback -- not _fetch_issue's -- fails open rather than raising."""
    with tempfile.TemporaryDirectory() as d:
        base, _ = _repo(d)
        assert _rc()._research_note_posted(base, "1762", source=None) is False


# --- #1826: goal-review's own artifact -- the story/epic issue itself, fetched+inlined like _parent() ---

def test_goal_review_phase_is_named():
    assert "goal-review" in _rc().PHASES


def test_goal_review_default_pointer_names_the_design_artifact():
    """No --artifact given: the fallback prose must name the design write-up and state plainly
    that no branch/code exists yet -- the qualitative difference from code-review's diff-pointer
    and pr-review's PR-pointer this phase is explicitly modeled apart from."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        out = _rc().brief(base, "1826", "goal-review", repo_root=root)
        assert ".sdlc/design/" in out
        assert "no branch or code exists yet".lower() in out.lower() or "No branch or code exists" in out


def test_goal_review_artifact_is_fetched_and_inlined():
    """Unlike a diff or a plan file already on the local filesystem, the goal-review artifact IS an
    issue -- mirrors _parent()'s own fetch-and-inline shape, not a live-diff-style pointer."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        src = _FakeSource({"1826": {"title": "goal-review issue",
                                     "body": "AC: confirm the design write-up."}})
        out = _rc().brief(base, "1826", "goal-review", artifact="1826", repo_root=root, source=src)
        assert "goal-review issue" in out
        assert "AC: confirm the design write-up." in out
        assert "## What you are reviewing" in out
        # inlined directly under the artifact section, not left as a bare "go read it" pointer
        section = out.split("## What you are reviewing", 1)[1]
        assert "AC: confirm the design write-up." in section.split("##", 1)[0]


def test_goal_review_states_no_branch_or_code_exists_on_the_success_path():
    """#1853: SKILL.md's own claim ("states plainly that no branch or code exists yet") must hold
    on the ONE call shape sigma-goal-review/SKILL.md ever actually makes -- artifact == goal, fetch
    succeeds. That sentence used to live only in the no-artifact-passed dict fallback, a branch
    this real call shape never reaches. Live-confirmed missing before this fix (2026-08-28
    validation run)."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        src = _FakeSource({"1826": {"title": "goal-review issue", "body": "AC: confirm it."}})
        out = _rc().brief(base, "1826", "goal-review", artifact="1826", repo_root=root, source=src)
        assert "no branch or code exists yet" in out.lower()


def test_goal_review_states_no_branch_or_code_exists_when_the_fetch_fails_too():
    """The failure-path sibling: a fetch failure must not silently drop this safety statement
    either -- it was equally absent there before this fix, just less visible since that branch
    also says "could not be fetched" (a different, narrower kind of warning)."""
    class _Boom:
        def fetch_title_body(self, goal):
            raise RuntimeError("gh exploded")

    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        out = _rc().brief(base, "1826", "goal-review", artifact="1826", repo_root=root, source=_Boom())
        assert "no branch or code exists yet" in out.lower()


def test_goal_review_does_not_fetch_the_same_issue_twice():
    """`sigma-goal-review/SKILL.md` always invokes this phase with `artifact` equal to `goal` itself
    -- there is no separate Epic issue yet at this stage, so the story/epic issue under review and
    the goal the change serves are the SAME ticket (by design, per the skill's own step 2). The
    brief already fetches that issue once to fill '## The goal this change serves'; a second,
    identical `fetch_title_body` call for '## What you are reviewing' is a redundant `gh issue view`
    against the exact same number -- wasted API/rate-limit budget on every single goal-review run,
    not just this one. One fetch must cover both sections."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        src = _FakeSource({"1826": {"title": "Dossier pipeline epic",
                                     "body": "Some blast radius body text."}})
        out = _rc().brief(base, "1826", "goal-review", artifact="1826", repo_root=root, source=src)
        assert src.calls == ["1826"]                                    # exactly one fetch
        assert out.count("Some blast radius body text.") == 2           # still shown in both sections


def test_goal_review_does_not_retry_a_failed_fetch_for_the_identical_artifact_number():
    """The failure-path mirror of the test above: a failed fetch for the shared goal/artifact
    number must not be retried a second time for the artifact section either."""
    calls = []

    class _Boom:
        def fetch_title_body(self, goal):
            calls.append(str(goal))
            raise RuntimeError("gh exploded")

    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        out = _rc().brief(base, "1826", "goal-review", artifact="1826", repo_root=root, source=_Boom())
        assert calls == ["1826"]                                        # exactly one attempt, not two
        assert "could not be fetched" in out


def test_goal_review_artifact_fetch_failure_degrades_and_states_the_gap():
    class _Boom:
        def fetch_title_body(self, goal):
            raise RuntimeError("gh exploded")

    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        out = _rc().brief(base, "1826", "goal-review", artifact="1826", repo_root=root, source=_Boom())
        assert "gh issue view 1826" in out
        assert "could not be fetched" in out
        assert "Inputs NOT available" in out


def test_goal_review_artifact_is_a_number_not_a_path():
    """A story/epic issue number must never be probed as a filesystem path and reported missing --
    mirrors test_pr_review_artifact_is_a_number_not_a_path for the identical reason."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        src = _FakeSource({"1826": {"title": "t", "body": "b"}})
        out = _rc().brief(base, "1826", "goal-review", artifact="1826", repo_root=root, source=src)
        assert "does not exist" not in out


def _git_init(root):
    """Convention discovery is git-index-backed, so a fixture that exercises it must be a real repo.
    No commit needed — `ls-files --others` sees the working tree."""
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)


def test_nested_claude_md_files_bind_too():
    """A directory-scoped CLAUDE.md governs its subtree. A reviewer handed only the root one judges a
    change under `web/` against rules that do not govern it, and misses the ones that do."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        _git_init(root)
        r = pathlib.Path(root)
        (r / "web").mkdir(); (r / "web" / "CLAUDE.md").write_text("web rules\n", encoding="utf-8")
        out = _rc().brief(base, "a goal", "code-review", repo_root=root)
        assert "web/CLAUDE.md" in out
        assert "repo root" in out           # the root pointer is not displaced by the nested one


def test_discovery_skips_ignored_trees_and_lookalike_names():
    """The controls. `*CLAUDE.md` as a pathspec also matches `MYCLAUDE.md`, and a filesystem walk
    would pull in vendored copies git already knows to ignore — both were live defects in the design."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        _git_init(root)
        r = pathlib.Path(root)
        (r / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
        (r / "web").mkdir(); (r / "web" / "MYCLAUDE.md").write_text("decoy\n", encoding="utf-8")
        (r / "node_modules").mkdir()
        (r / "node_modules" / "CLAUDE.md").write_text("vendored\n", encoding="utf-8")
        out = _rc().brief(base, "a goal", "code-review", repo_root=root)
        assert "MYCLAUDE.md" not in out
        assert "node_modules/CLAUDE.md" not in out


def test_nested_conventions_reported_with_no_root_claude_md():
    """A repo whose only conventions live in subdirectories still gets a conventions section — the
    root file's absence must not suppress rules that do exist."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        (pathlib.Path(root) / "CLAUDE.md").unlink()
        _git_init(root)
        (pathlib.Path(root) / "api").mkdir()
        (pathlib.Path(root) / "api" / "CLAUDE.md").write_text("api rules\n", encoding="utf-8")
        out = _rc().brief(base, "a goal", "code-review", repo_root=root)
        assert "api/CLAUDE.md" in out


def test_convention_discovery_fails_open_outside_a_git_repo():
    """This module's standing rule. No git, not a repo, or a wedged call degrades to exactly what
    shipped before — the root pointer — and never raises."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)               # deliberately NOT git init'd
        (pathlib.Path(root) / "web").mkdir()
        (pathlib.Path(root) / "web" / "CLAUDE.md").write_text("web\n", encoding="utf-8")
        out = _rc().brief(base, "a goal", "code-review", repo_root=root)
        assert "repo root" in out
        assert "web/CLAUDE.md" not in out


# --- #1778: the brief the two SKILL.md files actually prescribe, run where the loop runs it -----


def _committed_repo(root):
    """A real repo with one commit, so a `git worktree` can be cut from it. `.sdlc/` is gitignored
    exactly as this kit ships it, which is what keeps the north-star out of every worktree."""
    r = pathlib.Path(root)
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True, capture_output=True)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(r), "config", k, v], check=True, capture_output=True)
    (r / ".gitignore").write_text(".sdlc/\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(r), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(r), "commit", "-q", "-m", "init"], check=True,
                    capture_output=True)


def test_brief_from_a_goal_worktree_still_carries_the_north_star():
    """#1778. `sigma-plan-review/SKILL.md` prescribes `review_context.py brief .sdlc "<goal>"`, and
    the loop runs that phase from inside the goal's WORKTREE — where `.sdlc/context/` does not
    exist, because `.sdlc/` is gitignored. Measured on this repo before the fix, the identical
    command produced 14,686 bytes from the main checkout and 1,816 from the worktree, silently
    dropping BOTH project docs. The gesture under test here is that same relative `.sdlc`."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)
        _committed_repo(root)
        wt = pathlib.Path(root) / ".sdlc" / "work" / "1778"
        subprocess.run(["git", "-C", root, "worktree", "add", "-q", "-b", "sdlc/1778", str(wt),
                        "HEAD"], check=True, capture_output=True)
        # the premise, asserted not assumed: the worktree's own .sdlc has no project docs at all
        assert not (wt / ".sdlc" / "context" / "north-star.md").exists()
        assert not (wt / ".sdlc" / "project.md").exists()

        out = _rc().brief(str(wt / ".sdlc"), "a goal", "plan-review", repo_root=str(wt))
        assert "UI holds no business logic" in out       # a numbered architecture rule
        assert "no multi-tenant" in out                  # a declared non-goal
        assert "alignment cannot be judged" not in out   # ...so this is NOT a gap any more


def test_a_worktree_of_a_drop_in_repo_still_declares_the_gap():
    """The fix must not invent a north-star. A project that genuinely has none is still told so,
    from the worktree as from anywhere else."""
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        (root / ".sdlc").mkdir()
        _committed_repo(root)
        wt = root / ".sdlc" / "work" / "1778"
        subprocess.run(["git", "-C", str(root), "worktree", "add", "-q", "-b", "sdlc/1778",
                        str(wt), "HEAD"], check=True, capture_output=True)
        out = _rc().brief(str(wt / ".sdlc"), "a goal", "plan-review", repo_root=str(wt))
        assert "alignment cannot be judged" in out


def test_the_gap_notice_is_not_suppressed_by_an_unrelated_conventions_block():
    """`_missing` used to infer "no north-star or project.md" from the whole assembled string being
    empty — but a root CLAUDE.md alone makes it truthy. So a brief that had lost BOTH project docs
    reported no gap at all: the ABSENT-is-not-PASS rule broken by the line meant to enforce it."""
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        base = root / ".sdlc"; base.mkdir()
        (root / "CLAUDE.md").write_text("Rule: no client strings.\n", encoding="utf-8")
        out = _rc().brief(str(base), "a goal", "plan-review", repo_root=str(root))
        assert "CLAUDE.md" in out                       # the conventions block IS present
        assert "alignment cannot be judged" in out      # and it does not hide the real gap


def test_a_present_project_md_does_not_suppress_the_missing_north_star_gap():
    """#2147, found by the plan-review for it. `_project_context` set ONE `docs_found` bool from
    EITHER of `_PROJECT_DOCS`, and `_missing` gapped on that single bool — so `project.md` present
    plus north-star absent produced a brief carrying neither the north-star nor any notice that it
    was gone. Same shape as the CLAUDE.md variant above, which #1778 closed and this one survived:
    the reviewer is told nothing and reports alignment it never judged."""
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        base = root / ".sdlc"; base.mkdir()
        (base / "project.md").write_text("Stack: python. Verify: pytest -q\n", encoding="utf-8")
        out = _rc().brief(str(base), "a goal", "plan-review", repo_root=str(root))
        assert "Verify: pytest -q" in out                    # project.md really is in the pack
        assert "north-star" in out                           # ...and the gap names the missing doc
        assert "alignment cannot be judged" in out


def test_an_unreadable_north_star_declares_the_gap_from_a_worktree():
    """The `unreachable` state, end to end through the brief rather than through the resolver alone:
    the file exists in the main checkout and cannot be opened, and `project.md` is there to trigger
    the suppression above. A brief that stays silent here is the ABSENT-is-not-PASS rule broken in
    exactly the state `north_star.py`'s three values were introduced for."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return                                               # root ignores the permission bits
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        ctx = root / ".sdlc" / "context"; ctx.mkdir(parents=True)
        (root / ".sdlc" / "project.md").write_text("Stack: python\n", encoding="utf-8")
        target = ctx / "north-star.md"
        target.write_text("## Strategy\nno multi-tenant\n", encoding="utf-8")
        _committed_repo(root)
        wt = root / ".sdlc" / "work" / "2147"
        subprocess.run(["git", "-C", str(root), "worktree", "add", "-q", "-b", "sdlc/2147",
                        str(wt), "HEAD"], check=True, capture_output=True)
        target.chmod(0o000)
        try:
            out = _rc().brief(str(wt / ".sdlc"), "a goal", "plan-review", repo_root=str(wt))
        finally:
            target.chmod(0o644)
        assert "no multi-tenant" not in out                  # it genuinely could not be read
        assert "alignment cannot be judged" in out           # ...and the brief says so


def test_brief_output_publishes_one_immutable_generation(tmp_path):
    base, root = _repo(tmp_path)
    manifest = pathlib.Path(base) / "state" / "review-manifests" / "2577.json"
    code = _rc().main(["review_context.py", "brief", base, "2577", "--for", "code-review",
                       "--artifact", "branch-diff", "--repo-root", root, "--output", str(manifest)])
    assert code == 0
    data = json.loads(manifest.read_text())
    brief = manifest.parent.parent / "review-generations" / data["generation_id"] / "brief.md"
    assert brief.exists() and data["brief_sha256"]


def test_documented_manifest_output_gesture_cannot_escape_its_state_directory(tmp_path):
    """The exact SKILL.md output shape must refuse traversal before writing any manifest."""
    base, root = _repo(tmp_path)
    escaped = pathlib.Path(base) / "state" / "escaped.json"
    output = pathlib.Path(base) / "state" / "review-manifests" / ".." / "escaped.json"
    code = _rc().main(["review_context.py", "brief", base, "../../escaped", "--for", "code-review",
                       "--artifact", "branch-diff", "--repo-root", root, "--output", str(output)])
    assert code == 2
    assert not escaped.exists()


def test_pr_review_generation_binds_the_current_pr_and_head(tmp_path):
    base, root = _repo(tmp_path)
    _committed_repo(pathlib.Path(root))
    subprocess.run(["git", "-C", root, "update-ref", "refs/remotes/origin/main", "HEAD"],
                   check=True, capture_output=True)  # the PR's base, as `work.py pr` targets it
    # The producer reads a real work record and resolves the worktree head, rather than accepting
    # PR/head values from an agent-authored manifest argument.
    record = pathlib.Path(base) / "state" / "work" / "2577.json"; record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"pr": 7, "worktree": root, "remote": "origin", "base": "main"}))
    manifest = pathlib.Path(base) / "state" / "review-manifests" / "2577.json"
    code = _rc().main(["review_context.py", "brief", base, "2577", "--for", "pr-review",
                       "--artifact", "PR#7", "--repo-root", root, "--output", str(manifest)])
    assert code == 0
    data = json.loads(manifest.read_text())
    assert data["pr"] == 7 and re.fullmatch(r"[0-9a-f]{40}", data["head_sha"])


def test_pr_review_bare_number_artifact_refuses_the_manifest_loudly(tmp_path):
    """A caller who forgets the documented "PR#<N>" prefix (--artifact 2819 instead of --artifact
    "PR#2819") must be refused here, not silently handed an unbound manifest -- no pr/head_sha/
    base_ref/diff_sha256 -- that then passes record-subagent-review/review-evidence and only fails
    much later, opaquely, at `work.py post-review`, after a whole review cycle already ran on it."""
    base, root = _repo(tmp_path)
    manifest = pathlib.Path(base) / "state" / "review-manifests" / "2819.json"
    code = _rc().main(["review_context.py", "brief", base, "2819", "--for", "pr-review",
                       "--artifact", "2819", "--repo-root", root, "--output", str(manifest)])
    assert code == 2
    assert not manifest.exists(), "a bare-number artifact must not publish any manifest, bound or not"
    assert not (pathlib.Path(base) / "state" / "review-generations").exists(), \
        "nor the immutable generation directory a manifest would point at"


def test_publish_generation_rejects_a_malformed_pr_review_artifact_directly(tmp_path):
    base, root = _repo(tmp_path)
    manifest = pathlib.Path(base) / "state" / "review-manifests" / "2819.json"
    for bad in ("2819", "src/pay.py", "PR2819", "pr#2819"):
        try:
            _rc().publish_generation(base, "2819", "review\n", bad, str(manifest), "pr-review")
            assert False, "expected a ValueError for artifact=%r" % bad
        except ValueError as exc:
            assert "PR#<N>" in str(exc)
    assert not manifest.exists()


def test_publish_generation_phase_omitted_keeps_prior_unbound_behavior(tmp_path):
    """`phase` is optional and backward compatible: an existing direct caller that omits it (both
    real call sites in tests/test_work.py always pass a well-formed PR# artifact anyway) keeps
    publishing an unbound manifest for a non-PR artifact, exactly as before this fix."""
    base, root = _repo(tmp_path)
    manifest = pathlib.Path(base) / "state" / "review-manifests" / "2819.json"
    data = _rc().publish_generation(base, "2819", "review\n", "not-a-pr", str(manifest))
    assert "pr" not in data and "head_sha" not in data


def test_publish_generation_for_another_phase_keeps_an_unbound_manifest(tmp_path):
    """The guard is pr-review's alone: every other phase publishes the same publisher's unbound
    manifest for an artifact that is not a PR, a bare number included."""
    base, root = _repo(tmp_path)
    manifest = pathlib.Path(base) / "state" / "review-manifests" / "2819.json"
    for phase in ("plan-review", "code-review", "retro", "goal-review"):
        data = _rc().publish_generation(base, "2819", "review\n", "2819", str(manifest), phase)
        assert "pr" not in data and "head_sha" not in data, phase


def _pr_review_repo(tmp_path, goal, pr):
    """`_repo` + a committed checkout + the active work record `--artifact PR#<pr>` binds to."""
    base, root = _repo(tmp_path)
    _committed_repo(pathlib.Path(root))
    subprocess.run(["git", "-C", root, "update-ref", "refs/remotes/origin/main", "HEAD"],
                   check=True, capture_output=True)
    record = pathlib.Path(base) / "state" / "work" / (goal + ".json"); record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"pr": pr, "worktree": root, "remote": "origin", "base": "main"}))
    return base, root


#: The publishing gesture as the docs print it: `python3 "${CLAUDE_SKILL_DIR}/scripts/review_context.py"
#: brief .sdlc "$goal" --for pr-review --artifact "PR#$pr" --output ".sdlc/state/review-manifests/$goal.json"`.
_PUBLISH_GESTURE = re.compile(r'python3 "\$\{CLAUDE_SKILL_DIR\}/scripts/review_context\.py"\s+(brief\s[^`]*--output\s[^`]+)`')
_PUBLISH_DOCS = ("running.md", "landing.md")


def _documented_publish_argv(name):
    text = (S.parent / "references" / name).read_text(encoding="utf-8")
    found = [" ".join(m.group(1).split()) for m in _PUBLISH_GESTURE.finditer(text)]
    found = [g for g in found if "--for pr-review" in g]
    assert len(found) == 1, (name, found)
    return found[0]


def _run_documented_publish(tmp_path, name, artifact_word):
    """Copy the gesture out of the doc, substitute only its placeholders, run it as printed:
    cwd = the repo, a relative `.sdlc`, no `--repo-root`. `artifact_word` replaces `"PR#$pr"`."""
    base, root = _pr_review_repo(tmp_path, "2577", 2819)
    gesture = _documented_publish_argv(name)
    assert '--artifact "PR#$pr"' in gesture, gesture
    argv = gesture.replace('--artifact "PR#$pr"', "--artifact " + artifact_word).replace('"$goal"', "2577")
    argv = shlex.split(argv.replace('".sdlc/state/review-manifests/$goal.json"',
                                     ".sdlc/state/review-manifests/2577.json"))
    proc = subprocess.run([sys.executable, str(S / "review_context.py"), *argv], cwd=root,
                          capture_output=True, text=True, timeout=120)
    return proc, pathlib.Path(base)


@pytest.mark.parametrize("name", _PUBLISH_DOCS)
def test_the_documented_pr_review_gesture_still_publishes_a_bound_manifest(tmp_path, name):
    proc, base = _run_documented_publish(tmp_path, name, "PR#2819")
    assert proc.returncode == 0, proc.stderr
    data = json.loads((base / "state" / "review-manifests" / "2577.json").read_text())
    assert data["pr"] == 2819 and re.fullmatch(r"[0-9a-f]{40}", data["head_sha"])


@pytest.mark.parametrize("name", _PUBLISH_DOCS)
def test_the_documented_pr_review_gesture_without_the_pr_prefix_refuses_and_writes_nothing(tmp_path, name):
    proc, base = _run_documented_publish(tmp_path, name, "2819")
    assert proc.returncode == 2 and proc.stdout == "", proc
    assert "PR#<N>" in proc.stderr, proc.stderr
    assert not (base / "state" / "review-manifests").exists()
    assert not (base / "state" / "review-generations").exists()


def test_pr_review_generations_are_distinct_for_two_heads_with_the_same_brief(tmp_path):
    base, root = _repo(tmp_path)
    _committed_repo(pathlib.Path(root))
    subprocess.run(["git", "-C", root, "update-ref", "refs/remotes/origin/main", "HEAD"],
                   check=True, capture_output=True)  # the PR's base, as `work.py pr` targets it
    record = pathlib.Path(base) / "state" / "work" / "2577.json"; record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"pr": 7, "worktree": root, "remote": "origin", "base": "main"}))
    manifest = pathlib.Path(base) / "state" / "review-manifests" / "2577.json"
    rc = _rc()
    first = rc.publish_generation(base, "2577", "review the same prose", "PR#7", manifest)
    (pathlib.Path(root) / "change.py").write_text("head two\n")
    subprocess.run(["git", "-C", root, "add", "change.py"], check=True, capture_output=True)
    subprocess.run(["git", "-C", root, "commit", "-q", "-m", "second head"], check=True, capture_output=True)
    second = rc.publish_generation(base, "2577", "review the same prose", "PR#7", manifest)
    root_state = pathlib.Path(base) / "state" / "review-generations"
    assert first["head_sha"] != second["head_sha"]
    assert first["generation_id"] != second["generation_id"]
    assert (root_state / first["generation_id"] / "brief.md").exists()
    assert (root_state / second["generation_id"] / "brief.md").exists()


# --- #2647: a phase artifact that lives only on the goal BRANCH still reaches the brief ---
#
# The documented gesture (sigma-review/SKILL.md, landing.md, running.md) runs the brief from the
# MAIN CHECKOUT with `<sdlc_dir>` = `.sdlc`, while `.sdlc/plans/<goal>.md` and
# `.sdlc/research/<goal>.md` are tracked files on `sdlc/<goal>`. Every test below therefore uses
# that exact gesture — one passing the WORKTREE's `.sdlc` would be testing the path that already
# worked, and would be decoration.

def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _goal_branch_repo(d, goal="0007-blue-button", plan="# plan\n## Recolor the button\n",
                      dossier=None, remote_only=False):
    """A main checkout on `main` whose `.sdlc/{plans,research}/` are EMPTY, plus a `sdlc/<goal>`
    branch that carries the artifacts — the real shape #2647 measured. `remote_only` moves the
    branch out to a bare remote and deletes the local ref, the shape a main checkout is in once
    `work.py finish` has run (measured on this repo: `git show sdlc/2575:...` -> "invalid object
    name", `origin/sdlc/2575` resolves)."""
    base, root = _repo(d)
    (pathlib.Path(base) / "research").mkdir(exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t"); _git(root, "config", "user.name", "t")
    _git(root, "add", "-A"); _git(root, "commit", "-qm", "base")
    _git(root, "checkout", "-qb", "sdlc/" + goal)
    (pathlib.Path(base) / "plans" / (goal + ".md")).write_text(plan, encoding="utf-8")
    if dossier:
        (pathlib.Path(base) / "research" / (goal + ".md")).write_text(dossier, encoding="utf-8")
    _git(root, "add", "-A"); _git(root, "commit", "-qm", "plan")
    _git(root, "checkout", "-q", "main")
    # The artifacts are on the branch only; the main checkout's working tree has none.
    assert not (pathlib.Path(base) / "plans" / (goal + ".md")).exists()
    if remote_only:
        bare = pathlib.Path(d) / "remote.git"
        _git(root, "init", "-q", "--bare", str(bare))
        _git(root, "remote", "add", "origin", str(bare))
        _git(root, "push", "-q", "origin", "sdlc/" + goal)
        _git(root, "branch", "-qD", "sdlc/" + goal)
        p = subprocess.run(["git", "-C", root, "rev-parse", "--verify", "-q", "sdlc/" + goal],
                           capture_output=True, text=True)
        assert p.returncode != 0, "local ref should be gone — otherwise the remote path is untested"
    return base, root


def test_plan_on_the_goal_branch_reaches_a_brief_run_from_the_main_checkout():
    with tempfile.TemporaryDirectory() as d:
        base, root = _goal_branch_repo(d)
        out = _rc().brief(base, "0007-blue-button", "pr-review", artifact="99", repo_root=root)
        assert "## Recolor the button" in out                       # the plan outline is there
        assert "unplanned scope" in out                             # ...as the plan block
        assert "No plan for this goal" not in out                   # ...and no false gap line


def test_research_dossier_on_the_goal_branch_reaches_the_same_brief():
    """Same root cause, second face: `_phase_doc` uses the same filesystem-only resolver, so the
    documented gesture drops a dossier that is 466 lines long on the real branch it measured."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _goal_branch_repo(d, dossier="| BR-1 | src/pay.py:42 | charge() | in-scope |")
        out = _rc().brief(base, "0007-blue-button", "pr-review", artifact="99", repo_root=root)
        assert "src/pay.py:42" in out
        assert "No Research dossier" not in out
        assert "No local Research dossier file" not in out


def test_the_branch_artifact_is_found_through_the_remote_ref_when_no_local_branch_exists():
    with tempfile.TemporaryDirectory() as d:
        base, root = _goal_branch_repo(d, remote_only=True)
        out = _rc().brief(base, "0007-blue-button", "pr-review", artifact="99", repo_root=root)
        assert "## Recolor the button" in out
        assert "No plan for this goal" not in out


def test_a_worktree_plan_still_wins_over_a_different_one_on_the_branch():
    """Pins the ORDER, the one contested decision here. `landing.md:102` — "the reviewer BRIEF reads
    the main checkout's, the PR's reviewer reads the branch's" — and #2237's late plan corrections
    are written in the main checkout before they are committed. Branch-first would silently serve
    the stale committed copy, which is the very class of defect #2647 is about."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _goal_branch_repo(d, plan="# plan\n## Stale committed heading\n")
        (pathlib.Path(base) / "plans").mkdir(parents=True, exist_ok=True)
        (pathlib.Path(base) / "plans" / "0007-blue-button.md").write_text(
            "# plan\n## Corrected heading written in the main checkout\n", encoding="utf-8")
        out = _rc().brief(base, "0007-blue-button", "pr-review", artifact="99", repo_root=root)
        assert "## Corrected heading written in the main checkout" in out
        assert "## Stale committed heading" not in out


def test_the_plan_gap_line_returns_when_no_branch_carries_it():
    """The non-vacuity control for the three tests above: the gap line means what it says. The input
    is asserted non-vacuous — the brief is a real brief with its other sections, not an empty
    string that would make any `not in` assertion pass."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _goal_branch_repo(d)
        _git(root, "branch", "-qD", "sdlc/0007-blue-button")        # the only copy, deleted
        out = _rc().brief(base, "0007-blue-button", "pr-review", artifact="99", repo_root=root)
        assert len(out) > 500 and "north-star" in out.lower()       # non-vacuous input
        assert "## Recolor the button" not in out
        assert "No plan for this goal" in out


def test_branch_resolution_fails_open_outside_a_git_repo():
    """No git, no repo, no refs — the brief still builds. Guards against a future `check=True` or a
    bare `subprocess.run(...).stdout` turning a missing ref into a crashed review."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)                                       # deliberately NOT a git repo
        out = _rc().brief(base, "0007-blue-button", "pr-review", artifact="99", repo_root=root)
        assert "No plan for this goal" in out
        assert "north-star" in out.lower()


def test_a_goal_that_would_build_an_option_shaped_ref_never_reaches_git():
    """The filesystem resolver's traversal defence (#486) has a twin here: the branch path builds a
    git REF from `goal`. `work.stem` returns a non-`.md` goal verbatim and `branch_prefix` is
    operator-configurable, so an empty prefix would put a leading `-` in argv.

    This asserts the GUARD, not the outcome. A first draft asserted "no plan in the brief" and
    passed with the guard deleted -- `git ls-tree --upload-pack=... ` fails on its own, so the
    brief looked identical either way. That test was decoration; this one records the argv."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _goal_branch_repo(d)
        (pathlib.Path(base) / "config.json").write_text(
            '{"work": {"branch_prefix": ""}}', encoding="utf-8")
        rc, seen = _rc(), []
        real = rc._git_out
        rc._git_out = lambda root_, args: (seen.append(args), real(root_, args))[1]
        assert rc._branch_doc(base, "--upload-pack=touch /tmp/pwned_2647", "plans") == (None, "")
        assert seen, "no git call recorded - the guard would be untested by this assertion"
        # The bare ref is skipped; only `origin/--upload-pack=...` is tried, which git can only
        # parse as a (nonexistent) revision because it does not lead with a dash.
        assert not any(a.startswith("--upload-pack") for args in seen for a in args), seen
        assert not pathlib.Path("/tmp/pwned_2647").exists()


def test_the_option_shaped_guard_still_lets_an_ordinary_goal_through():
    """Non-vacuity for the test above: the same recording harness, a normal goal, and the branch
    IS read - so the assertion there is about the dash, not about `_branch_doc` never running."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _goal_branch_repo(d)
        rc, seen = _rc(), []
        real = rc._git_out
        rc._git_out = lambda root_, args: (seen.append(args), real(root_, args))[1]
        pointer, text = rc._branch_doc(base, "0007-blue-button", "plans")
        assert "## Recolor the button" in text
        assert pointer.endswith(" show sdlc/0007-blue-button:.sdlc/plans/0007-blue-button.md")
        assert pointer.startswith("git -C ")           # cwd-independent, as the old path was
        assert any(a == "sdlc/0007-blue-button" for args in seen for a in args)


def test_the_branch_resolver_picks_THIS_goal_s_plan_out_of_a_real_tree():
    """The load-bearing half of `_branch_doc`, and the one nothing covered: WHICH artifact it picks.
    A real branch tree is not one file — `git ls-tree origin/sdlc/2575 -- .sdlc/plans/` lists 50 on
    this repo, four of them `2444*`. A resolver that returned the alphabetically first plan passed
    every other test here, and would hand the reviewer another goal's plan under the instruction
    "work in the diff but NOT in the plan is unplanned scope": a confident, wrong verdict, strictly
    worse than the silence #2647 removes."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _goal_branch_repo(d, goal="2444", plan="# plan\n## The 2444 plan\n")
        _git(root, "checkout", "-q", "sdlc/2444")
        plans = pathlib.Path(base) / "plans"
        for name, head in [("244.md", "## The 244 plan"), ("2444-retro.md", "## A 2444 retro"),
                           ("2444-review.md", "## A 2444 review"), ("2570-core.md", "## Another goal")]:
            (plans / name).write_text("# plan\n%s\n" % head, encoding="utf-8")
        _git(root, "add", "-A"); _git(root, "commit", "-qm", "siblings"); _git(root, "checkout", "-q", "main")
        out = _rc().brief(base, "2444", "pr-review", artifact="99", repo_root=root)
        assert "## The 2444 plan" in out                 # its own, exactly
        # `244.md` sorts FIRST in this tree, so a resolver that ignored the goal and took the
        # alphabetically first plan would be caught here, not merely by the sibling `2444-*` names.
        for other in ("## The 244 plan", "## A 2444 retro", "## A 2444 review", "## Another goal"):
            assert other not in out, other


def test_a_bare_number_goal_resolves_a_slugged_plan_on_the_branch_newest_first():
    """The #1808 glob arm, on the branch. In github mode `$goal` is a bare number while Research and
    Plan routinely file under `<number>-<slug>.md`, so this arm is the normal case, not an exotic
    one. A git tree has no mtime, so the tie-break is the last name in sorted order — asserted here
    because `_artifact_names`' docstring documents that difference and nothing else proves it."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _goal_branch_repo(d, goal="2570", plan="# plan\n## exact-name plan\n")
        _git(root, "checkout", "-q", "sdlc/2570")
        plans = pathlib.Path(base) / "plans"
        (plans / "2570.md").unlink()                     # no exact name -> only the glob can answer
        (plans / "2570-a-first-attempt.md").write_text("# plan\n## the first attempt\n", encoding="utf-8")
        (plans / "2570-b-refiled.md").write_text("# plan\n## the refiled plan\n", encoding="utf-8")
        _git(root, "add", "-A"); _git(root, "commit", "-qm", "slugged"); _git(root, "checkout", "-q", "main")
        out = _rc().brief(base, "2570", "pr-review", artifact="99", repo_root=root)
        assert "## the refiled plan" in out              # last in sorted order
        assert "## the first attempt" not in out
        assert "No plan for this goal" not in out


def test_a_worktree_dossier_wins_over_a_different_one_on_the_branch():
    """Finding 2: the plan's order was pinned, the dossier's was not — and the whole argument for
    worktree-first (landing.md's two-copies split; a correction written in the main checkout before
    anything commits it) applies to the P2 dossier no less than to the plan."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _goal_branch_repo(d, dossier="| BR-1 | stale/committed.py:1 |")
        (pathlib.Path(base) / "research").mkdir(parents=True, exist_ok=True)
        (pathlib.Path(base) / "research" / "0007-blue-button.md").write_text(
            "| BR-1 | fresh/in-main-checkout.py:9 |", encoding="utf-8")
        out = _rc().brief(base, "0007-blue-button", "pr-review", artifact="99", repo_root=root)
        assert "fresh/in-main-checkout.py:9" in out
        assert "stale/committed.py:1" not in out


def test_an_empty_plan_blob_on_the_branch_does_not_suppress_the_gap_line():
    """ABSENT is never PASS: a zero-byte committed plan must not answer for a plan. Without the
    `text.strip()` guard a truthy pointer would be handed over and the gap line silently dropped."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _goal_branch_repo(d, plan="")
        out = _rc().brief(base, "0007-blue-button", "pr-review", artifact="99", repo_root=root)
        assert len(out) > 500 and "north-star" in out.lower()      # non-vacuous input
        assert "No plan for this goal" in out


def test_an_unreadable_plan_on_disk_is_not_reported_as_no_plan():
    """Finding 3. `brief()` falls through to the branch on empty TEXT, not on a missing path, so an
    on-disk plan that cannot be read must keep the path it already had rather than become the
    "judge against the goal alone" instruction this whole issue exists to remove."""
    with tempfile.TemporaryDirectory() as d:
        base, root = _repo(d)                                      # no branch, no git
        plan = pathlib.Path(base) / "plans" / "0007-blue-button.md"
        plan.write_text("# plan\n## Recolor the button\n", encoding="utf-8")
        plan.chmod(0o000)
        try:
            out = _rc().brief(base, "0007-blue-button", "code-review", repo_root=root)
        finally:
            plan.chmod(0o644)
        assert "No plan for this goal" not in out
        assert "Full plan" in out and "0007-blue-button.md" in out


# --- #993 (decision rubric slice 3): the `context.documents` key -------------------------------

def _doc_brief(d, documents="__absent__", inline_max=None, files=None):
    base, root = _repo(d)
    for rel, body in (files or {}).items():
        p = pathlib.Path(root) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    ctx = {}
    if documents != "__absent__":
        ctx["documents"] = documents
    if inline_max is not None:
        ctx["inline_max_bytes"] = inline_max
    (pathlib.Path(base) / "config.json").write_text(json.dumps({"context": ctx} if ctx else {}),
                                                    encoding="utf-8")
    return _rc().brief(base, "a goal", "plan-review", repo_root=root)


def test_absent_key_leaves_the_brief_identical():
    with tempfile.TemporaryDirectory() as d:
        plain = _doc_brief(d)
    with tempfile.TemporaryDirectory() as d:
        wrong_type = _doc_brief(d, documents="docs/x.md")      # not a list: ignored, as if absent
    assert hashlib.sha256(plain.encode()).hexdigest() == hashlib.sha256(wrong_type.encode()).hexdigest()
    assert "no documents" not in plain.lower() and "context.documents" not in plain
    with tempfile.TemporaryDirectory() as d:
        named = _doc_brief(d, documents=["docs/x.md"], files={"docs/x.md": "XBODY\n"})
    assert named != plain                                       # red if the key is never read


def test_path_escape_is_refused():
    with tempfile.TemporaryDirectory() as d:
        out = _doc_brief(d, documents=["../secret.md"])
        assert "../secret.md" in out and "refused" in out.lower()


def test_oversize_doc_becomes_a_pointer():
    with tempfile.TemporaryDirectory() as d:
        out = _doc_brief(d, documents=["docs/big.md"], inline_max=100,
                         files={"docs/big.md": "BIGBODY " * 200})
        assert "docs/big.md" in out and "BIGBODY" not in out and "read it" in out.lower()


def test_third_doc_is_inlined():
    with tempfile.TemporaryDirectory() as d:
        out = _doc_brief(d, documents=["docs/third.md"], files={"docs/third.md": "THIRDBODY\n"})
        assert "THIRDBODY" in out and "UI holds no business logic" in out   # both built-ins stay


def test_no_documents_named_is_its_own_state():
    with tempfile.TemporaryDirectory() as d:
        out = _doc_brief(d, documents=[])
        assert "no reference documents named" in out.lower()
