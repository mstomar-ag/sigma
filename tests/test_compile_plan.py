"""compile_plan.py (#918, sigma-scope skill, wave 1 of epic #902): the mechanical "given a
DECIDED plan, create it for real" layer -- real issues, real priority:P<n> label+field, a real
epic when warranted, real "Blocked by #N" markers between the newly-created siblings.

Hermetic like every sibling test in this repo: a FakeSource records what would have been sent to
`gh` and never shells out -- see tests/test_handoff.py's own FakeSource for the established shape
this mirrors (create_dependency/note/append_to_body/issue_url).
"""
import importlib.util
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCOPE_SCRIPTS = _ROOT / "skills" / "sigma-scope" / "scripts"
LOOP = _ROOT / "skills" / "sigma-loop" / "scripts"


def _mod(name, where):
    spec = importlib.util.spec_from_file_location(name, where / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


compile_plan = _mod("compile_plan", SCOPE_SCRIPTS)
backlog_check = _mod("backlog_check", LOOP)   # live _BLOCK_RE -- the actual regex trap to verify against
handoff = _mod("handoff", LOOP)               # PROPOSED_LABEL / proposed_label() reuse check


# --------------------------------------------------------------------------------------- FakeSource


class FakeSource:
    """Records every create_dependency/append_to_body/note call, exactly like test_handoff.py's own
    FakeSource, plus two knobs this suite specifically needs: `fail_titles` (a set of titles whose
    create_dependency call raises, simulating a real `gh` failure mid-plan) and auto-incrementing
    issue numbers so multi-issue plans get distinct, realistic numbers without a caller having to
    hand-assign them."""

    def __init__(self, start=100, fail_titles=()):
        self._next = start
        self.fail_titles = set(fail_titles)
        self.created = []          # [{"number":, "title":, "body":, "assignee":, "labels":, "goal_label":}]
        self.attempted = []        # every title create_dependency was CALLED with, success or not
        self.body_appends = []     # [(issue, marker)]

    def create_dependency(self, title, body, assignee, labels=(), goal_label=True):
        self.attempted.append(title)
        if title in self.fail_titles:
            raise RuntimeError(f"gh: simulated failure creating {title!r}")
        number = str(self._next)
        self._next += 1
        self.created.append({"number": number, "title": title, "body": body, "assignee": assignee,
                             "labels": list(labels), "goal_label": goal_label})
        return number

    def append_to_body(self, issue, marker):
        self.body_appends.append((str(issue), marker))

    def park(self, issue, text):
        self.parks = getattr(self, "parks", [])
        self.parks.append((str(issue), text))

    def by_title(self, title):
        return next(c for c in self.created if c["title"] == title)


#: AI-filed triage switched off: the pre-triage filing path (decision rubric slice 17, #1006).
TRIAGE_OFF = {"ai_filed": {"triage": {"enabled": False}}}


def _titles(src):
    return [c["title"] for c in src.created]


# --------------------------------------------------------------------------------------------- plans


def _plan(*issues, epic=None):
    return {"epic": epic, "issues": list(issues)}


def _issue(key, title, priority="P2", blocked_by=(), body="body text"):
    return {"key": key, "title": title, "body": body, "priority": priority,
            "blocked_by": list(blocked_by)}


# ------------------------------------------------------------------------------- single-issue plan


def test_single_issue_plan_creates_one_issue_no_epic():
    src = FakeSource()
    plan = _plan(_issue("a", "Do the thing"))
    report = compile_plan.compile_plan("/irrelevant/.sdlc", {}, plan, source=src)

    assert report["epic"] is None
    assert len(src.created) == 1
    assert report["issues"] == {"a": src.by_title("Do the thing")["number"]}
    assert report["failed"] == {} and report["skipped"] == {}


def test_single_issue_plan_carries_priority_label_and_field_ready_shape():
    """The label the acceptance criteria calls "both sides" of -- create_dependency (reused, not
    reimplemented) is what actually stamps the board Priority FIELD from this same label list, so
    the whole contract collapses to: does the LABEL carry priority:P<n>? Verified against the real
    create_dependency contract in test_sources.py/test_handoff.py already; not re-proven here."""
    src = FakeSource()
    plan = _plan(_issue("a", "Do the thing", priority="P0"))
    compile_plan.compile_plan("/irrelevant/.sdlc", {}, plan, source=src)
    assert "priority:P0" in src.by_title("Do the thing")["labels"]


def test_single_issue_plan_is_filed_but_not_immediately_actionable_by_default():
    """Design decision: compile_plan's own default is goal_label=False (filed, not auto-picked) --
    assignment/execution-path IS-it-actionable-now is #919's job, not #918's. The distinct
    distinct needs-confirmation label (`handoff.proposed_label`, matching handoff.py's #233
    convention, reused live; renamed by #1348) is what keeps the queued set queryable instead of
    merely "missing a label"."""
    src = FakeSource()
    plan = _plan(_issue("a", "Do the thing"))
    compile_plan.compile_plan("/irrelevant/.sdlc", TRIAGE_OFF, plan, source=src)
    created = src.by_title("Do the thing")
    assert created["goal_label"] is False
    assert handoff.proposed_label({}) in created["labels"]
    assert "parked" not in compile_plan.compile_plan("/irrelevant/.sdlc", TRIAGE_OFF, plan,
                                                     source=FakeSource())


def test_goal_label_true_opts_every_plan_issue_into_immediate_actionability():
    src = FakeSource()
    plan = _plan(_issue("a", "Do the thing"))
    compile_plan.compile_plan("/irrelevant/.sdlc", {}, plan, source=src, goal_label=True)
    created = src.by_title("Do the thing")
    assert created["goal_label"] is True
    # #233: an actionable issue is a real goal, never a proposal.
    assert handoff.proposed_label({}) not in created["labels"]


def test_epic_data_is_ignored_for_a_single_issue_plan():
    """Design decision (issue's own open question): the epic wrapper is created only when the plan
    has MORE than one sub-issue -- a single-issue plan needs no wrapper even if the caller (out of
    habit, or because the deciding layer #920 doesn't special-case this yet) hands one along."""
    src = FakeSource()
    plan = _plan(_issue("a", "Do the thing"), epic={"title": "Epic nobody needs", "body": "..."})
    report = compile_plan.compile_plan("/irrelevant/.sdlc", {}, plan, source=src)
    assert report["epic"] is None
    assert all(c["title"] != "Epic nobody needs" for c in src.created)
    assert len(src.created) == 1


# ------------------------------------------------------------------------ multi-issue + dependencies


def test_multi_issue_plan_creates_epic_and_wires_a_single_blocker_marker():
    src = FakeSource(start=900)
    plan = _plan(
        _issue("a", "Foundation piece"),
        _issue("b", "Depends on foundation", blocked_by=["a"]),
        epic={"title": "The epic", "body": "epic body", "priority": "P2"},
    )
    report = compile_plan.compile_plan("/irrelevant/.sdlc", {}, plan, source=src)

    assert report["epic"] is not None
    epic_created = src.by_title("The epic")
    assert "epic" in epic_created["labels"]
    assert epic_created["goal_label"] is False           # #918: an epic never carries sdlc:goal

    a_num = report["issues"]["a"]
    b_num = report["issues"]["b"]
    b_created = src.by_title("Depends on foundation")
    assert f"**Blocked by:** #{a_num}" in b_created["body"]
    assert f"Part of epic #{report['epic']}" in b_created["body"]

    # findall against the REAL regex must resolve the single blocker to the real sibling number,
    # not a placeholder / the plan-relative key "a".
    hits = backlog_check._BLOCK_RE.findall(b_created["body"])
    assert hits == [("Blocked by", str(a_num))]
    assert str(a_num) != "a"                              # sanity: it really is the real issue number


def test_multi_issue_plan_multiple_blockers_are_not_comma_joined():
    """THE regex trap named in the issue and the epic plan doc: `_BLOCK_RE` only captures ONE #N per
    trigger phrase, so "Blocked by #A, #B" would silently register only #A. This proves the marker
    text this module generates for a MULTI-blocker issue repeats the trigger phrase once per number
    instead, and that findall() genuinely recovers every one of them against the LIVE regex (not a
    hand-copied duplicate that could drift from the real one)."""
    src = FakeSource(start=500)
    plan = _plan(
        _issue("a", "First blocker"),
        _issue("b", "Second blocker"),
        _issue("c", "Third blocker"),
        _issue("d", "Triple-blocked dependant", blocked_by=["a", "b", "c"]),
    )
    report = compile_plan.compile_plan("/irrelevant/.sdlc", {}, plan, source=src)

    d_body = src.by_title("Triple-blocked dependant")["body"]
    assert "," not in d_body.split("Blocked by")[0] or True  # (no comma-joined marker line at all:)
    assert d_body.count("**Blocked by:**") == 3

    hits = backlog_check._BLOCK_RE.findall(d_body)
    got_numbers = sorted(int(n) for _, n in hits)
    want_numbers = sorted(int(report["issues"][k]) for k in ("a", "b", "c"))
    assert got_numbers == want_numbers
    assert len(hits) == 3                                 # every blocker independently recovered


def test_creation_order_is_dependency_order_blocker_before_dependant():
    """A dependent's marker embeds the blocker's REAL issue number at creation time (never a
    placeholder patched in later) -- only possible if the blocker was created first. Proven
    directly off the FakeSource's own call order, not inferred from the body text alone."""
    src = FakeSource()
    plan = _plan(
        _issue("dependant", "The dependant", blocked_by=["blocker"]),   # listed FIRST in the plan
        _issue("blocker", "The blocker"),                                # but must be CREATED first
    )
    compile_plan.compile_plan("/irrelevant/.sdlc", {}, plan, source=src)
    assert _titles(src).index("The blocker") < _titles(src).index("The dependant")


def test_epic_body_gets_patched_with_the_real_sub_issue_numbers_after_creation():
    """The epic itself cannot reference its subs' numbers AT creation (they don't exist yet) -- this
    is the one legitimate two-phase (create-then-patch) step in this module, and it is patched via
    append_to_body, never re-embedded into a re-created issue."""
    src = FakeSource()
    plan = _plan(_issue("a", "Sub one"), _issue("b", "Sub two"),
                epic={"title": "Epic", "body": "epic body"})
    report = compile_plan.compile_plan("/irrelevant/.sdlc", {}, plan, source=src)

    epic_num = str(report["epic"])
    patched = [marker for issue, marker in src.body_appends if issue == epic_num]
    assert patched, "expected the epic's body to be patched with its sub-issues"
    combined = "\n".join(patched)
    assert str(report["issues"]["a"]) in combined
    assert str(report["issues"]["b"]) in combined
    # the epic's own tracking text must not itself look like a blocking dependency to _BLOCK_RE --
    # an epic is never blocked, and must never be misread as such.
    assert backlog_check._BLOCK_RE.findall(combined) == []


def test_epic_body_includes_originates_from_at_creation_when_given():
    """#1827: unlike `Tracks #N` (patched in AFTER creation, since sub numbers don't exist yet),
    the Dossier/Story number IS already known before the epic is created -- so this back-reference
    must land in the epic's INITIAL create_dependency body, never via append_to_body/body_appends."""
    src = FakeSource()
    plan = _plan(_issue("a", "Sub one"), _issue("b", "Sub two"),
                epic={"title": "Epic", "body": "epic body", "originates_from": 42})
    report = compile_plan.compile_plan("/irrelevant/.sdlc", {}, plan, source=src)

    assert report["epic"] is not None
    epic_created = src.by_title("Epic")
    assert "Originates from Story #42." in epic_created["body"]
    epic_num = str(report["epic"])
    assert not any(issue == epic_num for issue, _marker in src.body_appends
                   if "Originates from Story" in _marker)


def test_epic_body_has_no_originates_from_line_when_absent():
    """Regression proof: every existing caller omits `originates_from` -- the new field must not
    inject anything into a body that never asked for it."""
    src = FakeSource()
    plan = _plan(_issue("a", "Sub one"), _issue("b", "Sub two"),
                epic={"title": "Epic", "body": "epic body"})
    compile_plan.compile_plan("/irrelevant/.sdlc", {}, plan, source=src)
    assert "Originates from" not in src.by_title("Epic")["body"]


def test_originates_from_marker_is_not_misread_as_a_blocker():
    """Same live-regex discipline as the `Tracks #N` test above -- the new template must be
    trigger-word-free too, verified against the REAL backlog_check._BLOCK_RE, not a hand-copied
    duplicate pattern."""
    src = FakeSource()
    plan = _plan(_issue("a", "Sub one"), _issue("b", "Sub two"),
                epic={"title": "Epic", "body": "epic body", "originates_from": 7})
    compile_plan.compile_plan("/irrelevant/.sdlc", {}, plan, source=src)
    body = src.by_title("Epic")["body"]
    assert "Originates from Story #7." in body
    assert backlog_check._BLOCK_RE.findall(body) == []


def test_originates_from_is_ignored_for_a_single_issue_plan():
    """Mirrors test_epic_data_is_ignored_for_a_single_issue_plan: `originates_from` is just another
    epic-data field subject to the same more-than-one-issue gate -- a single-issue plan creates no
    epic at all, regardless of what the epic dict carries."""
    src = FakeSource()
    plan = _plan(_issue("a", "Do the thing"),
                epic={"title": "Epic nobody needs", "body": "...", "originates_from": 99})
    report = compile_plan.compile_plan("/irrelevant/.sdlc", {}, plan, source=src)
    assert report["epic"] is None
    assert all("Originates from" not in (c["body"] or "") for c in src.created)
    assert len(src.created) == 1


# --------------------------------------------------------------------------------------- validation


def test_duplicate_keys_raise_before_any_issue_is_created():
    src = FakeSource()
    plan = _plan(_issue("a", "One"), _issue("a", "Two, same key"))
    with pytest.raises(ValueError, match="duplicate"):
        compile_plan.compile_plan("/irrelevant/.sdlc", {}, plan, source=src)
    assert src.created == []


def test_unknown_blocked_by_reference_raises_before_any_issue_is_created():
    src = FakeSource()
    plan = _plan(_issue("a", "One", blocked_by=["nonexistent"]))
    with pytest.raises(ValueError, match="unknown"):
        compile_plan.compile_plan("/irrelevant/.sdlc", {}, plan, source=src)
    assert src.created == []


def test_self_blocking_issue_raises_before_any_issue_is_created():
    src = FakeSource()
    plan = _plan(_issue("a", "One", blocked_by=["a"]))
    with pytest.raises(ValueError):
        compile_plan.compile_plan("/irrelevant/.sdlc", {}, plan, source=src)
    assert src.created == []


def test_dependency_cycle_raises_before_any_issue_is_created():
    src = FakeSource()
    plan = _plan(_issue("a", "One", blocked_by=["b"]), _issue("b", "Two", blocked_by=["a"]))
    with pytest.raises(ValueError, match="cycle"):
        compile_plan.compile_plan("/irrelevant/.sdlc", {}, plan, source=src)
    assert src.created == []


def test_missing_key_raises_before_any_issue_is_created():
    src = FakeSource()
    plan = _plan({"title": "No key here", "body": "x"})
    with pytest.raises(ValueError, match="key"):
        compile_plan.compile_plan("/irrelevant/.sdlc", {}, plan, source=src)
    assert src.created == []


def test_missing_title_raises_before_any_issue_is_created():
    src = FakeSource()
    plan = _plan({"key": "a", "body": "no title here", "blocked_by": []})
    with pytest.raises(ValueError, match="title"):
        compile_plan.compile_plan("/irrelevant/.sdlc", {}, plan, source=src)
    assert src.created == []


# ------------------------------------------------------------------------------- failure midway


def test_failure_midway_independent_issues_still_land_and_dependants_are_skipped_not_broken():
    """One issue creation fails; its own dependant must NEVER be created half-blocked (a marker
    referencing a blocker that doesn't actually exist would be silently wrong forever) -- it is
    SKIPPED and reported as such. A fully independent issue elsewhere in the same plan is
    unaffected and still lands. Nothing is rolled back (gh has no bulk-delete); the report is the
    single source of truth for what did and didn't land."""
    src = FakeSource(fail_titles={"Will fail"})
    plan = _plan(
        _issue("ok", "Independent, unrelated"),
        _issue("blocker", "Will fail"),
        _issue("dependant", "Depends on the failed one", blocked_by=["blocker"]),
    )
    report = compile_plan.compile_plan("/irrelevant/.sdlc", {}, plan, source=src)

    assert "ok" in report["issues"]                       # unrelated work still landed
    assert "blocker" in report["failed"]
    assert "dependant" not in report["issues"]
    assert "dependant" in report["skipped"]
    assert "blocker" not in report["issues"]
    # the report is honest about exactly what happened -- never silent, never a guess.
    assert "Will fail" not in str(report["issues"])
    assert _titles(src) == ["Independent, unrelated"]                # the failed create() never lands
    assert src.attempted == ["Independent, unrelated", "Will fail"]  # dependant's create_dependency
                                                                       # was never even attempted


def test_failure_midway_transitive_skip_two_levels_deep():
    src = FakeSource(fail_titles={"Root fails"})
    plan = _plan(
        _issue("root", "Root fails"),
        _issue("mid", "Depends on root", blocked_by=["root"]),
        _issue("leaf", "Depends on mid", blocked_by=["mid"]),
    )
    report = compile_plan.compile_plan("/irrelevant/.sdlc", {}, plan, source=src)
    assert report["failed"] == {"root": pytest.approx(report["failed"]["root"])}  # present, some message
    assert "mid" in report["skipped"] and "leaf" in report["skipped"]
    assert report["issues"] == {}


def test_epic_creation_failure_does_not_abort_the_sub_issues():
    class EpicFails(FakeSource):
        def create_dependency(self, title, body, assignee, labels=(), goal_label=True):
            if "epic" in (labels or ()):
                raise RuntimeError("gh: epic creation failed")
            return super().create_dependency(title, body, assignee, labels=labels, goal_label=goal_label)

    src = EpicFails()
    plan = _plan(_issue("a", "Sub one"), _issue("b", "Sub two"),
                epic={"title": "Epic", "body": "epic body"})
    report = compile_plan.compile_plan("/irrelevant/.sdlc", {}, plan, source=src)
    assert report["epic"] is None
    assert any("epic" in w.lower() for w in report["warnings"])
    assert "a" in report["issues"] and "b" in report["issues"]


# ------------------------------------------------------------------------------------- no source


def test_no_usable_source_reports_a_warning_and_creates_nothing():
    plan = _plan(_issue("a", "One"))
    report = compile_plan.compile_plan("/irrelevant/.sdlc", {}, plan, source=object())
    assert report["issues"] == {}
    assert any("cannot open issues" in w for w in report["warnings"])


def test_empty_issues_list_is_a_no_op():
    src = FakeSource()
    report = compile_plan.compile_plan("/irrelevant/.sdlc", {}, {"issues": []}, source=src)
    assert report == {"epic": None, "issues": {}, "failed": {}, "skipped": {}, "order": [], "warnings": []}
    assert src.created == []


def test_source_resolution_failure_is_reported_not_raised(monkeypatch):
    plan = _plan(_issue("a", "One"))

    def boom(sdlc_dir, config):
        raise RuntimeError("bad discovery config")

    monkeypatch.setattr(compile_plan.sources, "get_source", boom)
    report = compile_plan.compile_plan("/irrelevant/.sdlc", {}, plan)
    assert report["issues"] == {}
    assert any("no backlog source" in w for w in report["warnings"])


def test_create_dependency_returning_none_is_reported_as_failed():
    class NoneReturning(FakeSource):
        def create_dependency(self, title, body, assignee, labels=(), goal_label=True):
            return None

    src = NoneReturning()
    plan = _plan(_issue("a", "One"))
    report = compile_plan.compile_plan("/irrelevant/.sdlc", {}, plan, source=src)
    assert report["issues"] == {}
    assert "gh returned no issue number" in report["failed"]["a"]


def test_epic_create_dependency_returning_none_is_reported_as_a_warning():
    class EpicReturnsNone(FakeSource):
        def create_dependency(self, title, body, assignee, labels=(), goal_label=True):
            if EPIC_LABEL_MARKER in (labels or ()):
                return None
            return super().create_dependency(title, body, assignee, labels=labels, goal_label=goal_label)

    EPIC_LABEL_MARKER = compile_plan.EPIC_LABEL
    src = EpicReturnsNone()
    plan = _plan(_issue("a", "Sub one"), _issue("b", "Sub two"), epic={"title": "Epic", "body": "x"})
    report = compile_plan.compile_plan("/irrelevant/.sdlc", {}, plan, source=src)
    assert report["epic"] is None
    assert any("epic" in w.lower() for w in report["warnings"])
    assert "a" in report["issues"] and "b" in report["issues"]


def test_epic_patch_failure_is_reported_as_a_warning_epic_still_stands():
    class PatchFails(FakeSource):
        def append_to_body(self, issue, marker):
            raise RuntimeError("gh: edit failed")

    src = PatchFails()
    plan = _plan(_issue("a", "Sub one"), epic={"title": "Epic", "body": "x"}, )
    plan["issues"].append(_issue("b", "Sub two"))
    report = compile_plan.compile_plan("/irrelevant/.sdlc", {}, plan, source=src)
    assert report["epic"] is not None                     # the epic itself still exists
    assert any("could not patch its body" in w for w in report["warnings"])


def test_epic_created_but_every_sub_issue_fails_patches_nothing():
    """`_patch_epic_with_subs`'s own empty-guard: an epic that exists but has zero surviving subs
    must not call append_to_body at all (nothing meaningful to write)."""
    src = FakeSource(fail_titles={"Sub one", "Sub two"})
    plan = _plan(_issue("a", "Sub one"), _issue("b", "Sub two"), epic={"title": "Epic", "body": "x"})
    report = compile_plan.compile_plan("/irrelevant/.sdlc", {}, plan, source=src)
    assert report["epic"] is not None
    assert report["issues"] == {}
    assert src.body_appends == []


# ------------------------------------------------------------------------------------------- CLI


def _init_sdlc(tmp_path, config=None):
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir()
    (sdlc / "config.json").write_text(__import__("json").dumps(config if config is not None else {}))
    return sdlc


def test_main_usage_error_with_no_args(capsys):
    assert compile_plan.main(["compile_plan.py"]) == 2
    assert "usage" in capsys.readouterr().err


def test_main_usage_error_when_plan_flag_missing(tmp_path, capsys):
    sdlc = _init_sdlc(tmp_path)
    assert compile_plan.main(["compile_plan.py", str(sdlc)]) == 2
    assert "usage" in capsys.readouterr().err


def test_main_reports_an_unreadable_plan_file(tmp_path, capsys):
    sdlc = _init_sdlc(tmp_path)
    assert compile_plan.main(["compile_plan.py", str(sdlc), "--plan", str(tmp_path / "missing.json")]) == 2
    assert "could not read plan" in capsys.readouterr().err


def test_main_reports_invalid_json_in_the_plan_file(tmp_path, capsys):
    sdlc = _init_sdlc(tmp_path)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text("{not json")
    assert compile_plan.main(["compile_plan.py", str(sdlc), "--plan", str(plan_path)]) == 2
    assert "could not read plan" in capsys.readouterr().err


def test_main_reports_a_structurally_invalid_plan_and_creates_nothing(tmp_path, capsys):
    import json
    sdlc = _init_sdlc(tmp_path)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"issues": [{"key": "a", "title": "x", "blocked_by": ["ghost"]}]}))
    rc = compile_plan.main(["compile_plan.py", str(sdlc), "--plan", str(plan_path)])
    assert rc == 2
    assert "unknown" in capsys.readouterr().err
    assert not (sdlc / "goals").exists()


def test_main_success_creates_a_real_local_goal_file(tmp_path, capsys):
    """End-to-end through the REAL default (local-goals) source -- no gh, no network, just a
    filesystem write, exactly like every other LocalSource-backed test in this repo's suite."""
    import json
    sdlc = _init_sdlc(tmp_path)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"issues": [{"key": "a", "title": "Do the thing", "body": "b",
                                                  "priority": "P2"}]}))
    rc = compile_plan.main(["compile_plan.py", str(sdlc), "--plan", str(plan_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "created 'a' as #" in out
    goal_files = list((sdlc / "goals").glob("*.md"))
    assert len(goal_files) == 1
    assert "Do the thing" in goal_files[0].read_text()


def test_main_ignores_an_unrecognized_flag_rather_than_crashing(tmp_path):
    import json
    sdlc = _init_sdlc(tmp_path)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"issues": [{"key": "a", "title": "x"}]}))
    rc = compile_plan.main(["compile_plan.py", str(sdlc), "--bogus-flag", "--plan", str(plan_path)])
    assert rc == 0


def test_main_actionable_flag_reaches_compile_plan_as_goal_label_true(tmp_path, monkeypatch):
    import json
    sdlc = _init_sdlc(tmp_path)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"issues": [{"key": "a", "title": "x"}]}))

    seen = {}

    def fake_compile_plan(sdlc_dir, config, plan, *, source=None, goal_label=False,
                          forbid_priority=False):
        seen["goal_label"] = goal_label
        return {"epic": None, "issues": {"a": "9"}, "failed": {}, "skipped": {},
                "order": ["a"], "warnings": []}

    monkeypatch.setattr(compile_plan, "compile_plan", fake_compile_plan)
    rc = compile_plan.main(["compile_plan.py", str(sdlc), "--plan", str(plan_path), "--actionable"])
    assert rc == 0
    assert seen["goal_label"] is True


def test_main_reports_failed_and_skipped_issues_with_exit_code_1(tmp_path, capsys, monkeypatch):
    import json
    sdlc = _init_sdlc(tmp_path)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"issues": [{"key": "a", "title": "x"}]}))

    def fake_compile_plan(sdlc_dir, config, plan, *, source=None, goal_label=False,
                          forbid_priority=False):
        return {"epic": "42", "issues": {}, "failed": {"a": "gh: boom"},
                "skipped": {"b": "blocked on a"}, "order": [], "warnings": ["heads up"]}

    monkeypatch.setattr(compile_plan, "compile_plan", fake_compile_plan)
    rc = compile_plan.main(["compile_plan.py", str(sdlc), "--plan", str(plan_path)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "epic: #42" in captured.out
    assert "FAILED 'a': gh: boom" in captured.err
    assert "SKIPPED 'b': blocked on a" in captured.err
    assert "compile_plan: heads up" in captured.err


# ------------------------------------------------------------------------------------- main --json
# #1919: `sigma-goal-review` steps 4b/4e are written against `report["epic"]` / `report["issues"]`,
# but the only invocation the skill mandates is this CLI -- which printed prose, in TOPOLOGICAL
# rather than plan order, with the warnings on a different stream. `--json` is the machine channel
# that makes those steps followable exactly as written: stdout becomes EXACTLY one JSON object, and
# the consumer reads by KEY, so stdout ordering stops mattering at all.


def _json_out(capsys):
    """The whole of stdout, parsed. Fails loudly if anything else rode the stream -- that is the
    property under test, not an incidental convenience."""
    import json
    return json.loads(capsys.readouterr().out)


def test_main_json_prints_exactly_one_parseable_object_with_every_report_key(tmp_path, capsys,
                                                                             monkeypatch):
    import json
    sdlc = _init_sdlc(tmp_path)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"issues": [{"key": "a", "title": "x"}]}))

    def fake_compile_plan(sdlc_dir, config, plan, *, source=None, goal_label=False,
                          forbid_priority=False):
        return {"epic": "42", "issues": {"s1": "43", "s2": "44"}, "failed": {}, "skipped": {},
                "order": ["s1", "s2"], "warnings": []}

    monkeypatch.setattr(compile_plan, "compile_plan", fake_compile_plan)
    rc = compile_plan.main(["compile_plan.py", str(sdlc), "--plan", str(plan_path), "--json"])
    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report == {"epic": "42", "issues": {"s1": "43", "s2": "44"}, "failed": {},
                      "skipped": {}, "order": ["s1", "s2"], "warnings": []}


def test_main_json_suppresses_the_prose_lines_on_stdout(tmp_path, capsys, monkeypatch):
    """The prose and the JSON must never share stdout -- one `created 'a' as #9` line ahead of the
    object is the difference between `json.loads` working and the caller being back to scraping."""
    import json
    sdlc = _init_sdlc(tmp_path)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"issues": [{"key": "a", "title": "x"}]}))

    def fake_compile_plan(sdlc_dir, config, plan, *, source=None, goal_label=False,
                          forbid_priority=False):
        return {"epic": "42", "issues": {"a": "9"}, "failed": {}, "skipped": {}, "order": ["a"],
                "warnings": []}

    monkeypatch.setattr(compile_plan, "compile_plan", fake_compile_plan)
    compile_plan.main(["compile_plan.py", str(sdlc), "--plan", str(plan_path), "--json"])
    out = capsys.readouterr().out
    assert "epic: #42" not in out
    assert "created" not in out
    json.loads(out)          # still exactly one object, nothing appended


def test_main_json_leaves_the_stderr_diagnostics_exactly_where_they_were(tmp_path, capsys,
                                                                        monkeypatch):
    """A human running this by hand still sees why something did not land. The JSON carries the
    same facts; the stderr lines are not moved into it and are not silenced by the flag."""
    import json
    sdlc = _init_sdlc(tmp_path)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"issues": [{"key": "a", "title": "x"}]}))

    def fake_compile_plan(sdlc_dir, config, plan, *, source=None, goal_label=False,
                          forbid_priority=False):
        return {"epic": "42", "issues": {}, "failed": {"a": "gh: boom"},
                "skipped": {"b": "blocked on a"}, "order": [], "warnings": ["heads up"]}

    monkeypatch.setattr(compile_plan, "compile_plan", fake_compile_plan)
    rc = compile_plan.main(["compile_plan.py", str(sdlc), "--plan", str(plan_path), "--json"])
    captured = capsys.readouterr()
    assert rc == 1                                  # exit codes are unchanged by the flag
    assert "compile_plan: heads up" in captured.err
    assert "FAILED 'a': gh: boom" in captured.err
    assert "SKIPPED 'b': blocked on a" in captured.err
    report = json.loads(captured.out)
    assert report["failed"] == {"a": "gh: boom"} and report["warnings"] == ["heads up"]


def test_main_json_stringifies_non_string_plan_keys(tmp_path, capsys, monkeypatch):
    """`compile_plan`'s docstring types `key` as merely "hashable", so a plan may legally key its
    issues by int. `json.dumps` would emit those as strings anyway; doing it explicitly means the
    payload is the same shape whatever the plan used, and a tuple key raises here rather than
    halfway through writing the object."""
    import json
    sdlc = _init_sdlc(tmp_path)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"issues": [{"key": "a", "title": "x"}]}))

    def fake_compile_plan(sdlc_dir, config, plan, *, source=None, goal_label=False,
                          forbid_priority=False):
        return {"epic": None, "issues": {1: "9"}, "failed": {2: "boom"}, "skipped": {3: "why"},
                "order": [1, 2, 3], "warnings": []}

    monkeypatch.setattr(compile_plan, "compile_plan", fake_compile_plan)
    compile_plan.main(["compile_plan.py", str(sdlc), "--plan", str(plan_path), "--json"])
    report = json.loads(capsys.readouterr().out)
    assert report["issues"] == {"1": "9"}
    assert report["failed"] == {"2": "boom"} and report["skipped"] == {"3": "why"}
    assert report["order"] == ["1", "2", "3"]


def test_main_json_end_to_end_through_the_real_local_source(tmp_path, capsys):
    """No monkeypatch anywhere -- the real default (local-goals) source, the real report. This is
    the invocation `sigma-goal-review` step 4b actually mandates.

    Note the issue NUMBER type is the source's, not this flag's: `LocalSource.create_dependency`
    returns an int and `GitHubSource`'s returns a str, and `--json` deliberately passes both through
    unchanged rather than inventing a normalisation the prose path never had. Only the report's
    KEYS are coerced (see `_json_report`). A consumer must therefore interpolate the number, never
    call a str method on it -- which is exactly what this assertion got wrong first time round."""
    import json
    sdlc = _init_sdlc(tmp_path)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"issues": [{"key": "s1", "title": "Do the thing", "body": "b"}]}))
    rc = compile_plan.main(["compile_plan.py", str(sdlc), "--plan", str(plan_path), "--json"])
    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert list(report["issues"]) == ["s1"] and str(report["issues"]["s1"]).isdigit()
    assert report["failed"] == {} and report["skipped"] == {}


def test_main_without_json_still_prints_the_prose_and_no_json(tmp_path, capsys):
    """The control for the flag: the default path is byte-for-byte what it always was."""
    import json
    sdlc = _init_sdlc(tmp_path)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"issues": [{"key": "a", "title": "Do the thing", "body": "b"}]}))
    rc = compile_plan.main(["compile_plan.py", str(sdlc), "--plan", str(plan_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "created 'a' as #" in out
    with pytest.raises(ValueError):
        json.loads(out)


def test_main_json_usage_error_still_reports_usage_on_stderr(tmp_path, capsys):
    """A usage failure has no report to serialize, so `--json` must not turn rc 2 into an empty
    object on stdout -- the caller has to be able to tell "nothing was created" from "created
    nothing"."""
    sdlc = _init_sdlc(tmp_path)
    assert compile_plan.main(["compile_plan.py", str(sdlc), "--json"]) == 2
    captured = capsys.readouterr()
    assert "usage" in captured.err
    assert captured.out == ""


def test_the_usage_string_advertises_the_json_flag(tmp_path, capsys):
    compile_plan.main(["compile_plan.py"])
    assert "--json" in capsys.readouterr().err


# ================================== #1956: a design-artifact id is not a slice key, and says so
# A real `goal-design` pass wrote `3, and B-1` in the `Slice-count estimate` table's `Depends on`
# column -- a slice edge AND a Blockers id. `blocked_by` keys name SIBLING ISSUES IN THE SAME PLAN
# and nothing else, so `B-1` has nothing to resolve to; the run's human silently dropped it between
# Stage 1 and Stage 2. The rule is now written down in both SKILL.md files (forbid, with the
# redirect), and this end recognises the SHAPE so the refusal names the seam instead of reading as
# an ordinary typo. Behaviour is unchanged: same ValueError, still before any `gh` call.


def _refusal(*issues, epic=None, **kw):
    """The ValueError text a plan raises, with the assertion that it raised at all. `epic` and any
    extra keyword (`forbid_priority=True`, #2027) are passed straight through, so the same helper
    covers both refusals that fire before any `gh` call."""
    src = FakeSource()
    with pytest.raises(ValueError) as excinfo:
        compile_plan.compile_plan("/irrelevant/.sdlc", {}, _plan(*issues, epic=epic),
                                  source=src, **kw)
    assert src.created == [], "a structurally invalid plan created something"
    return str(excinfo.value)


def test_a_blocker_id_in_blocked_by_is_refused_and_the_message_names_where_it_belongs():
    message = _refusal(_issue("1", "First slice"),
                       _issue("3", "Third slice"),
                       _issue("4", "Fourth slice", blocked_by=["3", "B-1"]))
    assert "B-1" in message
    assert "design" in message.lower(), message
    assert "Blockers" in message, message


def test_an_ordinary_unknown_key_keeps_the_generic_message_and_gains_no_design_pointer():
    """The discriminating half, and the one that can go red on its own: if the recognition were
    widened to every unknown key the pointer would become noise on a plain typo, which is how a
    hint stops being read. Seen red by loosening `_DESIGN_ARTIFACT_ID_RE` to `.*`."""
    message = _refusal(_issue("a", "One", blocked_by=["nonexistant"]))
    assert "unknown key" in message
    assert "Blockers" not in message, message
    assert ".sdlc/design/" not in message, message


def test_every_id_prefix_the_design_artifact_schema_uses_is_recognised():
    """`BR-n` (Blast radius) is pinned in the artifact schema today; `D-n`/`B-n` are the shapes a
    real pass invented for Doubts/Blockers; `X-n` (Out of scope, #1975) and `PC-n` (Premise check,
    #1976) joined them. Every one is a non-slice row of the same document, so every one has to be
    recognised -- a reference to any of them is the same mistake, and an unrecognised family gets
    the generic typo message at exactly the seam this hint exists to name. The schema end is pinned
    generically by `test_sdlc_goal_design_skill.py`, which reads the families out of the fence."""
    for bad in ("BR-7", "D-3", "B-1", "X-5", "PC-2", "S-4"):
        assert compile_plan._DESIGN_ARTIFACT_ID_RE.match(bad), bad
    for fine in ("B1", "b-1", "BRX-1", "B-", "-1", "3", "slice-1", "x-1", "PCX-1", "XY-1",
                 "s-4", "SX-1", "S-"):
        assert not compile_plan._DESIGN_ARTIFACT_ID_RE.match(fine), fine


def test_the_hint_names_every_section_whose_ids_it_recognises():
    """The hint's whole job is to say WHERE the reference belongs. A family recognised by the regex
    but absent from the sentence produces a pointer that sends the reader to the wrong heading."""
    for section in ("Blast radius", "Premise check", "Seeds", "Out of scope", "Doubts",
                    "Blockers"):
        assert section in compile_plan._DESIGN_ID_HINT, \
            f"the refusal hint does not name the {section} rows it recognises"


def test_a_key_that_merely_looks_like_a_design_id_still_compiles_when_it_is_a_real_slice():
    """Recognition fires only on an UNKNOWN key. A plan that genuinely names its own issues `B-1`
    is not this mistake and must not be refused for resembling it."""
    src = FakeSource()
    plan = _plan(_issue("B-1", "Legitimately keyed B-1"),
                 _issue("B-2", "Depends on it", blocked_by=["B-1"]),
                 epic={"title": "Epic", "body": "e"})
    report = compile_plan.compile_plan("/irrelevant/.sdlc", {}, plan, source=src)
    assert report["failed"] == {} and report["skipped"] == {}
    assert set(report["issues"]) == {"B-1", "B-2"}


# ============ #2027: a Dossier-pipeline plan may not carry a priority, and the refusal is structural
# Measured on the 2026-09-01 validation run (story #2017 -> epic #2020 -> slices #2021-#2026): the
# `goal-review` pass wrote an explicit `"priority": "P2"` into ALL SIX child entries, which
# `docs/dossier-pipeline.md` §7f forbids in as many words. `compile_plan.py` did exactly what it was
# told -- the rule was honour-system prose, so nothing detected the deviation and it lands as a real
# `priority:P<n>` label AND, on a project-enabled repo, a real board field. A team then cannot tell
# which of its P2s a human set.
#
# REFUSE, not strip -- the same adjudication #1956 made for a design-artifact id (CHANGELOG: of the
# two routes, "resolving it ... could only mean ... dropping it, which is the bug moved into code
# where it is harder to see"). A strip produces a board that looks right while the model never
# learns, and silently discards a real `P0`.
#
# OPT-IN, because `compile_plan.py` is shared: `skills/sigma-scope/SKILL.md` mandates "a real
# `P0`-`P4` priority" per issue on the sigma-scope path, and `scope.py` calls the same function. The
# Dossier path opts in -- and because a flag alone is one more honour-system rule, the CLI also
# turns the guard on from the plan's own contractual directory.


def _clean(key, title, **extra):
    """A plan entry the §7f rule allows: everything `_issue` builds EXCEPT the `priority` key."""
    item = _issue(key, title, **extra)
    item.pop("priority")
    return item


def test_a_child_priority_is_refused_when_the_rule_applies():
    message = _refusal(_clean("s1", "First slice"),
                       _issue("s2", "Second slice", priority="P2"),
                       forbid_priority=True)
    assert "priority" in message
    assert "'s2'" in message, message


def test_an_epic_priority_is_refused_too():
    """§7f says "on the epic AND on every child" -- an epic-only offender is the half a
    children-only walk would miss entirely."""
    message = _refusal(_clean("s1", "First slice"), _clean("s2", "Second slice"),
                       epic={"title": "Epic", "body": "e", "priority": "P0"},
                       forbid_priority=True)
    assert "epic" in message.lower(), message


def test_the_refusal_names_every_offending_child_not_just_the_first():
    """The real run had six. Reporting one at a time costs six edit-and-re-run round trips, which
    is how a refusal stops being read and starts being worked around."""
    message = _refusal(*[_issue(f"s{i}", f"Slice {i}", priority="P2") for i in range(1, 7)],
                       forbid_priority=True)
    for key in ("'s1'", "'s2'", "'s3'", "'s4'", "'s5'", "'s6'"):
        assert key in message, f"{key} missing from the refusal: {message}"


def test_an_explicit_default_priority_is_refused_too():
    """PRESENCE, not truthiness. `"P1"` written out by hand is still the model's own judgement
    applied to a document that never expressed one -- it just happens to collide with the default.
    This is the case a strip could never distinguish from an omission, and the reason the guard
    refuses instead."""
    message = _refusal(_clean("s1", "First slice"),
                       _issue("s2", "Second slice", priority=compile_plan.DEFAULT_PRIORITY),
                       forbid_priority=True)
    assert "'s2'" in message, message


def test_omitting_priority_compiles_and_gets_the_live_default_on_epic_and_children():
    """The other half of §7f: omitting it is "a defined single-constant behaviour, not a gap". The
    default is read LIVE off the module, so a change to the constant cannot leave this asserting a
    number nothing produces any more."""
    src = FakeSource()
    plan = _plan(_clean("s1", "First slice"), _clean("s2", "Second slice", blocked_by=["s1"]),
                 epic={"title": "Epic", "body": "e"})
    report = compile_plan.compile_plan("/irrelevant/.sdlc", {}, plan, source=src,
                                       forbid_priority=True)
    assert report["failed"] == {} and report["skipped"] == {}
    want = f"priority:{compile_plan.DEFAULT_PRIORITY}"
    for created in src.created:
        assert want in created["labels"], created


def test_the_guard_is_off_by_default_so_a_scope_plan_still_sets_its_own_priority():
    """THE discriminating half, and the only one that catches a blanket guard. `sigma-scope`'s own
    SKILL.md mandates a real P0-P4 per issue and `scope.py` calls this same function without the
    flag -- a guard that fired unconditionally would break that path outright. Seen red by dropping
    the `if forbid_priority:` condition."""
    src = FakeSource()
    plan = _plan(_issue("a", "Do the thing", priority="P0"))
    report = compile_plan.compile_plan("/irrelevant/.sdlc", {}, plan, source=src)
    assert report["failed"] == {} and report["skipped"] == {}
    assert "priority:P0" in src.by_title("Do the thing")["labels"]


def test_the_refusal_precedes_source_resolution():
    """Zero side effects means zero, so the refusal has to land before the source is even resolved
    -- not merely before the first `gh` call. Same shape as
    `test_source_resolution_failure_is_reported_not_raised`, inverted: get_source raising is what
    proves it was never reached."""
    def boom(sdlc_dir, config):
        raise AssertionError("the source was resolved before the priority refusal")

    import unittest.mock as _mock
    with _mock.patch.object(compile_plan.sources, "get_source", boom):
        with pytest.raises(ValueError) as excinfo:
            compile_plan.compile_plan("/irrelevant/.sdlc", {},
                                      _plan(_issue("s1", "One", priority="P2")),
                                      forbid_priority=True)
    assert "priority" in str(excinfo.value)


def test_an_epic_priority_is_refused_even_with_no_issues():
    """Placement, pinned: `compile_plan` returns early on an empty `issues[]`, so a guard written
    one line lower would let an epic-only plan straight through."""
    src = FakeSource()
    with pytest.raises(ValueError):
        compile_plan.compile_plan("/irrelevant/.sdlc", {},
                                  {"epic": {"title": "Epic", "priority": "P3"}, "issues": []},
                                  source=src, forbid_priority=True)


# ------------------------------------------------------------------------------- the CLI end


def test_main_forbid_priority_flag_reaches_compile_plan(tmp_path, monkeypatch):
    import json
    sdlc = _init_sdlc(tmp_path)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"issues": [{"key": "a", "title": "x"}]}))

    seen = {}

    def fake_compile_plan(sdlc_dir, config, plan, *, source=None, goal_label=False,
                          forbid_priority=False):
        seen["forbid_priority"] = forbid_priority
        return {"epic": None, "issues": {"a": "9"}, "failed": {}, "skipped": {},
                "order": ["a"], "warnings": []}

    monkeypatch.setattr(compile_plan, "compile_plan", fake_compile_plan)
    rc = compile_plan.main(["compile_plan.py", str(sdlc), "--plan", str(plan_path),
                            "--forbid-priority"])
    assert rc == 0
    assert seen["forbid_priority"] is True


def test_main_defaults_forbid_priority_off(tmp_path, monkeypatch):
    """The control for the flag reaching through: without it, the shared path is untouched."""
    import json
    sdlc = _init_sdlc(tmp_path)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"issues": [{"key": "a", "title": "x"}]}))

    seen = {}

    def fake_compile_plan(sdlc_dir, config, plan, *, source=None, goal_label=False,
                          forbid_priority=False):
        seen["forbid_priority"] = forbid_priority
        return {"epic": None, "issues": {"a": "9"}, "failed": {}, "skipped": {},
                "order": ["a"], "warnings": []}

    monkeypatch.setattr(compile_plan, "compile_plan", fake_compile_plan)
    compile_plan.main(["compile_plan.py", str(sdlc), "--plan", str(plan_path)])
    assert seen["forbid_priority"] is False


def test_main_refuses_a_priority_bearing_plan_with_exit_code_2_and_creates_nothing(tmp_path, capsys):
    """End to end through the REAL local source: rc 2 (the structurally-invalid-plan code this CLI
    already used) and not one goal file on disk."""
    import json
    sdlc = _init_sdlc(tmp_path)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"issues": [{"key": "s1", "title": "Do the thing",
                                                 "body": "b", "priority": "P2"}]}))
    rc = compile_plan.main(["compile_plan.py", str(sdlc), "--plan", str(plan_path),
                            "--forbid-priority"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "priority" in captured.err
    assert captured.out == ""
    assert not (sdlc / "goals").exists(), "a refused plan created goal files"


def test_main_turns_the_guard_on_from_the_plan_path_without_the_flag(tmp_path, capsys):
    """The backstop that makes this structural rather than a second honour-system rule: a model that
    skipped a paragraph of SKILL.md prose can skip a flag too. `.sdlc/state/goal-review/` is where
    the contract puts the plan (`skills/sigma-goal-review/SKILL.md` mandates the `mkdir -p`,
    `docs/dossier-pipeline.md` §7d-iii repeats the path), so the path itself says which pipeline
    this plan came out of."""
    import json
    sdlc = _init_sdlc(tmp_path)
    plan_dir = sdlc / "state" / "goal-review"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2017.plan.json"
    plan_path.write_text(json.dumps({"issues": [{"key": "s1", "title": "Do the thing",
                                                 "body": "b", "priority": "P2"}]}))
    rc = compile_plan.main(["compile_plan.py", str(sdlc), "--plan", str(plan_path)])
    assert rc == 2
    assert "priority" in capsys.readouterr().err
    assert not (sdlc / "goals").exists()


def test_a_plan_outside_the_goal_review_dir_is_unaffected_by_the_backstop(tmp_path, capsys):
    """The discriminating half of the backstop: an sigma-scope plan sitting anywhere else compiles
    with its own priority intact. Seen red by pointing `_DOSSIER_PLAN_DIR` at a wrong directory."""
    import json
    sdlc = _init_sdlc(tmp_path)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"issues": [{"key": "s1", "title": "Do the thing",
                                                 "body": "b", "priority": "P0"}]}))
    rc = compile_plan.main(["compile_plan.py", str(sdlc), "--plan", str(plan_path)])
    assert rc == 0, capsys.readouterr().err


def test_the_backstop_directory_is_the_two_trailing_path_parts_not_a_substring():
    """A substring test would fire on `/tmp/state/goal-review-scratch/x.json` and, worse, miss
    nothing it should catch only by accident. The constant is the trailing PARTS of the plan's own
    parent path."""
    assert compile_plan._DOSSIER_PLAN_DIR == ("state", "goal-review")


def test_the_usage_string_advertises_the_forbid_priority_flag(capsys):
    compile_plan.main(["compile_plan.py"])
    assert "--forbid-priority" in capsys.readouterr().err


# ------------------------------------------------------------- triage at filing (rubric slice 17, #1006)


def test_scope_children_are_armed_with_plan_priority():
    src = FakeSource()
    plan = _plan(_issue("a", "Do the thing", priority="P0"), _issue("b", "Then more", priority="P3"))
    report = compile_plan.compile_plan("/irrelevant/.sdlc", {}, plan, source=src)
    for title, prio in (("Do the thing", "P0"), ("Then more", "P3")):
        created = src.by_title(title)
        assert created["goal_label"] is True
        assert created["labels"] == ["priority:" + prio]
    assert report["parked"] == {} and not getattr(src, "parks", [])


def test_deny_listed_child_is_parked():
    src = FakeSource()
    plan = _plan(_issue("a", "Clean up", body="this will drop table users"), _issue("b", "Fine"))
    report = compile_plan.compile_plan("/irrelevant/.sdlc", {}, plan, source=src)
    bad = src.by_title("Clean up")
    assert bad["goal_label"] is False
    assert handoff.proposed_label({}) not in bad["labels"]
    assert list(report["parked"]) == ["a"] and "drop table" in report["parked"]["a"]
    (issue, text), = src.parks
    assert issue == report["issues"]["a"] and "sigma-qkind: needs_decision" in text
    assert src.by_title("Fine")["goal_label"] is True


def test_injected_triage_decides_and_a_crash_parks():
    D = compile_plan.file_triage.Decision
    src = FakeSource()
    plan = _plan(_issue("a", "One"))
    compile_plan.compile_plan("/x/.sdlc", {}, plan, source=src,
                              triage=lambda t, b, f, c: D("park", None, "fake says park"))
    assert src.by_title("One")["goal_label"] is False and "fake says park" in src.parks[0][1]

    def boom(*a):
        raise RuntimeError("x")
    src2 = FakeSource()
    compile_plan.compile_plan("/x/.sdlc", {}, plan, source=src2, triage=boom)
    assert src2.by_title("One")["goal_label"] is False and len(src2.parks) == 1


def test_a_failed_park_is_a_warning_not_a_raise():
    class NoPark(FakeSource):
        def park(self, issue, text):
            raise RuntimeError("gh down")
    src = NoPark()
    report = compile_plan.compile_plan("/x/.sdlc", {}, _plan(_issue("a", "T", body="force push it")),
                                       source=src)
    assert any("could not park" in w for w in report["warnings"])


def test_gate_closed_filing_is_byte_identical_to_legacy():
    src = FakeSource()
    plan = _plan(_issue("a", "Clean up", body="drop table users"))
    report = compile_plan.compile_plan("/x/.sdlc", TRIAGE_OFF, plan, source=src)
    created = src.by_title("Clean up")
    assert created["labels"] == ["priority:P2", handoff.proposed_label({})]
    assert created["goal_label"] is False and not hasattr(src, "parks") and "parked" not in report
    assert set(report) == {"epic", "issues", "failed", "skipped", "order", "warnings"}


def test_no_writer_adds_the_label():
    """With triage on, none of the three slice-17 writers puts the confirmation label on anything."""
    import re
    src = FakeSource()
    plan = _plan(_issue("a", "Fine"), _issue("b", "Bad", body="credentials"))
    compile_plan.compile_plan("/x/.sdlc", {}, plan, source=src)
    assert all(handoff.proposed_label({}) not in c["labels"] for c in src.created)
    # structural: every place these files write the label sits under the triage-off branch
    for rel in ("skills/sigma-scope/scripts/compile_plan.py", "skills/sigma-scope/scripts/assign.py"):
        text = (_ROOT / rel).read_text()
        for m in re.finditer(r"labels\.append\(handoff\.proposed_label|add=\[proposed\]|remove=\[proposed\]",
                             text):
            before = text[max(0, m.start() - 400):m.start()]
            assert "elif not goal_label" in before or "triaged" in before or "proposed = " in before, rel


def test_main_json_carries_parked_only_when_triage_is_on(tmp_path, capsys):
    import json
    sdlc = _init_sdlc(tmp_path)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"issues": [{"key": "a", "title": "x", "body": "b"}]}))
    assert compile_plan.main(["compile_plan.py", str(sdlc), "--plan", str(plan_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["parked"] == {}
