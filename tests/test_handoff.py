import importlib.util
import json
import os
import pathlib

import pytest
from skill_corpus import skill_corpus

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


owners = _mod("owners")
handoff = _mod("handoff")
ledger = _mod("ledger")
feature_registry = _mod("feature_registry")
sources = _mod("sources")

CODEOWNERS = """\
# one human owns each area
*            @lead-person
/engine/     @eng-owner
/server/     @srv-owner
/ui/         @ui-owner
/quality/    @qa-owner
/docs/       @lead-person
*.tf         @infra-owner
"""

ON = {"ledger": {"enabled": True, "actor": "amy"}}
#: the same config with AI-filed triage switched off: the pre-triage filing path (slice 15, #1003).
ONL = dict(ON, ai_filed={"triage": {"enabled": False}})


def _project(tmp_path, config=None, codeowners=CODEOWNERS):
    sdlc = tmp_path / ".sdlc"
    (sdlc / "state").mkdir(parents=True)
    (sdlc / "config.json").write_text(json.dumps(config or ON))
    if codeowners is not None:
        (tmp_path / ".github").mkdir()
        (tmp_path / ".github" / "CODEOWNERS").write_text(codeowners)
    return sdlc


# ------------------------------------------------------------------ CODEOWNERS


def test_parse_drops_comments_and_strips_the_at_sign():
    rules = owners.parse(CODEOWNERS)
    assert rules[0] == ("*", ["lead-person"])
    assert ("/engine/", ["eng-owner"]) in rules
    assert all(not o.startswith("@") for _, os_ in rules for o in os_)


def test_parse_keeps_a_deliberately_unowned_pattern():
    assert owners.parse("/vendor/\n") == [("/vendor/", [])]


def test_last_matching_rule_wins_like_github():
    rules = owners.parse(CODEOWNERS)
    assert owners.for_path(rules, "engine/graph.py") == ["eng-owner"]
    assert owners.for_path(rules, "somewhere/else.md") == ["lead-person"]   # only the catch-all
    assert owners.for_path(rules, "infra/main.tf") == ["infra-owner"]       # later rule wins


def test_area_resolves_to_the_directory_owner_not_the_catch_all():
    rules = owners.parse(CODEOWNERS)
    assert owners.for_area(rules, "engine") == ["eng-owner"]
    assert owners.for_area(rules, "ui") == ["ui-owner"]


def test_unknown_area_falls_back_to_the_catch_all():
    assert owners.for_area(owners.parse(CODEOWNERS), "nonexistent") == ["lead-person"]


def test_config_override_beats_codeowners():
    rules = owners.parse(CODEOWNERS)
    cfg = {"ledger": {"owners": {"engine": "@someone-else"}}}
    assert owners.for_area(rules, "engine", cfg) == ["someone-else"]
    assert owners.for_area(rules, "ui", cfg) == ["ui-owner"]                # untouched areas unaffected


def test_owner_of_reads_the_file_and_takes_the_first_listed(tmp_path):
    _project(tmp_path, codeowners="/engine/ @first @second\n")
    assert owners.owner_of(tmp_path, "engine") == "first"


def test_no_codeowners_file_is_not_an_error(tmp_path):
    _project(tmp_path, codeowners=None)
    assert owners.load(tmp_path) == []
    assert owners.owner_of(tmp_path, "engine") is None


def test_owners_cli(tmp_path, capsys):
    _project(tmp_path)
    assert owners.main(["owners.py", str(tmp_path), "engine"]) == 0
    assert capsys.readouterr().out.strip() == "eng-owner"
    assert owners.main(["owners.py"]) == 2


def test_single_star_does_not_cross_a_directory_boundary():
    """CODEOWNERS `*` matches within one path segment only — unlike fnmatch's `*`, which matches
    anything including `/`. `engine/*` owns direct children, not deep descendants; `**` still
    crosses, for teams that opt into it (#355)."""
    rules = [("engine/*", ["eng-owner"])]
    assert owners.for_path(rules, "engine/a.py") == ["eng-owner"]
    assert owners.for_path(rules, "engine/a/b/c.py") == []

    double_star = [("engine/**", ["eng-owner"])]
    assert owners.for_path(double_star, "engine/a/b/c.py") == ["eng-owner"]


# ------------------------------------------------------------------ hand-off


class FakeSource:
    """Stands in for GitHubSource: records what would have been sent."""

    def __init__(self, number="61"):
        self.number = number
        self.created = None
        self.notes = []
        self.body_appends = []
        self.blocked_goals = []

    def issue_url(self, goal):
        return f"https://example.invalid/issues/{goal}"

    def create_dependency(self, title, body, assignee, labels=(), goal_label=True):
        self.created = {"title": title, "body": body, "assignee": assignee, "labels": list(labels),
                        "goal_label": goal_label}
        return self.number

    def note(self, goal, text):
        self.notes.append((goal, text))

    def append_to_body(self, goal, marker):
        self.body_appends.append((goal, marker))

    def mark_blocked(self, goal):
        self.blocked_goals.append(goal)


def test_handoff_opens_assigns_records_and_links(tmp_path):
    sdlc = _project(tmp_path)
    src = FakeSource()
    report = handoff.hand_off(sdlc, ONL, "0004-ui-restart.md", "engine",
                              "ui auto-restart needs an engine feature flag",
                              priority="P0", source=src)

    assert report["owner"] == "eng-owner" and report["issue"] == "61" and not report["warnings"]
    assert src.created["assignee"] == "eng-owner"
    assert "sdlc:dependency" in src.created["labels"] and "priority:P0" in src.created["labels"]
    assert "engine" in src.created["title"]
    assert "auto-restart" in src.created["body"] and "Done when:" in src.created["body"]

    entry = ledger.read_all(sdlc)[0]
    assert entry["kind"] == "handoff" and entry["to"] == "eng-owner"
    assert entry["issue"] == 61 and entry["priority"] == "P0" and entry["state"] == "open"

    goal, text = src.notes[0]
    assert "#61" in text and "@eng-owner" in text and "Parking" in text

    body_goal, marker = src.body_appends[0]
    assert body_goal == "0004-ui-restart.md" and marker == "**Blocked by:** #61"


def test_handoff_always_carries_sdlc_followup_even_without_the_caller_passing_it(tmp_path):
    """#1347: sdlc:followup must be a GUARANTEED label on every issue create_tracked_issue files,
    not an optional extra_labels pass-through -- a caller that forgets to type it (or never knew
    to) must not silently produce an unlabeled-as-AI-filed issue."""
    src = FakeSource()
    handoff.hand_off(_project(tmp_path), ON, "0004-ui-restart.md", "engine",
                      "ui auto-restart needs an engine feature flag", source=src)
    assert "sdlc:followup" in src.created["labels"]


def test_create_tracked_issue_always_carries_sdlc_followup_for_a_same_area_finding(tmp_path):
    src = FakeSource()
    handoff.create_tracked_issue(_project(tmp_path), ON, "0006-x.md", "engine",
                                  "found a nice-to-have", same_area=True,
                                  immediately_actionable=False, blocks_goal=False, source=src)
    assert "sdlc:followup" in src.created["labels"]


def test_create_tracked_issue_does_not_duplicate_sdlc_followup_if_the_caller_already_passed_it(tmp_path):
    src = FakeSource()
    handoff.create_tracked_issue(_project(tmp_path), ON, "0007-x.md", "engine", "found something",
                                  same_area=True, immediately_actionable=False, blocks_goal=False,
                                  extra_labels=["sdlc:followup", "model:bulk"], source=src)
    assert src.created["labels"].count("sdlc:followup") == 1
    assert "model:bulk" in src.created["labels"]


def test_hand_off_transitions_the_current_goal_to_blocked(tmp_path):
    """#1350: hand_off() always pins blocks_goal=True, so filing a cross-area dependency must
    transition the CURRENT (blocked) goal's own label immediately -- not deferred to a later
    park() call."""
    src = FakeSource()
    handoff.hand_off(_project(tmp_path), ON, "0004-ui-restart.md", "engine",
                      "ui auto-restart needs an engine feature flag", source=src)
    assert src.blocked_goals == ["0004-ui-restart.md"]


def test_create_tracked_issue_non_blocking_finding_never_transitions_the_goal(tmp_path):
    """blocks_goal=False must never touch the current goal's own label -- a merely-related finding
    is not a blocker, and mark_blocked firing here would be exactly the false-blocking bug this
    whole axis exists to prevent."""
    src = FakeSource()
    handoff.create_tracked_issue(_project(tmp_path), ON, "0008-x.md", "engine", "a related finding",
                                  same_area=True, immediately_actionable=False, blocks_goal=False,
                                  source=src)
    assert src.blocked_goals == []


def test_create_tracked_issue_does_not_call_mark_blocked_when_the_source_lacks_it(tmp_path):
    """LocalSource (and any other source without mark_blocked) must not raise -- this epic is
    GitHub-only in scope; a source with no mark_blocked degrades to exactly today's behavior."""
    class NoMarkBlocked:
        """Minimal, standalone -- no mark_blocked attribute at all, not even inherited."""
        def __init__(self):
            self.number = "61"

        def create_dependency(self, title, body, assignee, labels=(), goal_label=True):
            return self.number

        def note(self, goal, text):
            pass

        def append_to_body(self, goal, marker):
            pass

    src = NoMarkBlocked()
    assert not hasattr(src, "mark_blocked")
    report = handoff.create_tracked_issue(_project(tmp_path), ON, "0009-x.md", "engine", "why",
                                           same_area=True, immediately_actionable=True,
                                           blocks_goal=True, source=src)
    assert not report["warnings"]


def test_create_tracked_issue_same_area_blocking_finding_transitions_the_filing_goal(tmp_path):
    """#1358 review: mark_blocked is gated on `marker_written`, not on `not same_area` — a
    SAME-area blocker (`track --assignee same-area --blocks yes`, the CLI's own reachable
    combination) must transition the filing goal exactly like a cross-area hand_off() does. The
    three other same_area=True + blocks_goal=True tests in this file each use a source missing one
    of create_dependency/append_to_body/mark_blocked, so none of them actually reach this call —
    this is the one that does, with all three present."""
    src = FakeSource()
    handoff.create_tracked_issue(_project(tmp_path), ON, "0011-x.md", "engine",
                                  "found a same-area blocker while working this goal",
                                  same_area=True, immediately_actionable=True, blocks_goal=True,
                                  source=src)
    assert src.blocked_goals == ["0011-x.md"]


def test_create_tracked_issue_skips_mark_blocked_when_the_body_marker_never_landed(tmp_path):
    """If the machine-readable "Blocked by" marker never actually landed (append_to_body missing),
    the goal is NOT actually blocked in any enforceable sense -- transitioning its label to
    sdlc:blocked here would be actively misleading. mark_blocked must be gated on marker_written,
    not on blocks_goal alone."""
    class NoAppendToBody:
        """Has mark_blocked (recorded, to prove it's NOT called) but no append_to_body at all."""
        def __init__(self):
            self.number = "61"
            self.blocked_goals = []

        def create_dependency(self, title, body, assignee, labels=(), goal_label=True):
            return self.number

        def note(self, goal, text):
            pass

        def mark_blocked(self, goal):
            self.blocked_goals.append(goal)

    src = NoAppendToBody()
    assert not hasattr(src, "append_to_body")
    handoff.create_tracked_issue(_project(tmp_path), ON, "0010-x.md", "engine", "why",
                                  same_area=True, immediately_actionable=True, blocks_goal=True,
                                  source=src)
    assert src.blocked_goals == []


def test_handoff_narrative_wording_actually_matches_the_auto_skip_regex(tmp_path):
    """#376: an earlier version of the narrative said 'Blocked on' -- backlog_check.py's own
    _BLOCK_RE requires 'blocked by' (or depends on/needs/after/requires/waiting on), never 'on'.
    Import the real regex rather than hand-copying it, so this test breaks loudly if the two ever
    drift apart again instead of silently passing against a stale copy."""
    backlog_check = _mod("backlog_check")
    src = FakeSource()
    handoff.hand_off(_project(tmp_path), ON, "0005-x.md", "engine", "needs a flag", source=src)
    goal, narrative = src.notes[0]
    assert backlog_check._BLOCK_RE.search(narrative), narrative
    body_goal, marker = src.body_appends[0]
    assert backlog_check._BLOCK_RE.search(marker), marker


def test_handoff_narrative_does_not_claim_an_assignment_that_never_took(tmp_path):
    """F14/#338: create_dependency can open the issue unassigned after gh rejects the resolved owner
    (a team, most often) while still returning an issue number -- report["owner"] alone is then a
    stale signal, not proof the assignment happened. The narrative must trust
    source.last_assignee_applied, not just whether an owner was resolved."""
    class RejectedAssignee(FakeSource):
        last_assignee_applied = False

    src = RejectedAssignee()
    report = handoff.hand_off(_project(tmp_path), ON, "0006-x.md", "engine", "needs a flag", source=src)
    assert report["owner"] == "eng-owner" and report["issue"] == "61"     # still resolved, still opened
    goal, narrative = src.notes[0]
    assert "@eng-owner" not in narrative, narrative
    assert "assigned" not in narrative, narrative
    # #1358 review: a rejected assignee must not also silently disable mark_blocked -- the FILING
    # goal is genuinely blocked regardless of whether the new issue's own assignment took.
    assert src.blocked_goals == ["0006-x.md"]


def test_handoff_still_records_when_no_owner_is_declared(tmp_path):
    """#2395: hand_off() always pins immediately_actionable=True, so a cross-area hand-off with no
    resolvable owner now self-assigns to the filing actor instead of being left unassigned -- an
    unassigned sdlc:goal issue is invisible to every account's assignee-scoped discovery. The "no
    owner for area" warning is preserved so a human still knows CODEOWNERS/ledger.owners needs the
    area added; a second warning names the self-assign fallback."""
    sdlc = _project(tmp_path, codeowners="/server/ @srv-owner\n")
    src = FakeSource()
    report = handoff.hand_off(sdlc, ONL, "g.md", "engine", "needs a flag", source=src)
    assert report["owner"] == "amy"                             # #2395: self-assigned, not None
    assert any("no owner for area" in w for w in report["warnings"])
    assert any("self-assigning" in w for w in report["warnings"])
    assert src.created["assignee"] == "amy"                     # reaches create_dependency for real
    assert src.created["goal_label"] is True                    # unaffected -- this was already right
    entry = ledger.read_all(sdlc)[0]
    assert entry["kind"] == "handoff" and entry["to"] == "amy"  # visible to the team, addressed


def test_cross_area_actionable_follow_up_with_no_owner_self_assigns_and_stays_pickable(tmp_path):
    """#2395 repro (the reported shape): a cross-area FOLLOW-UP -- not a blocking hand-off --
    filed via create_tracked_issue(same_area=False, immediately_actionable=True, blocks_goal=False)
    for an area with no CODEOWNERS/ledger.owners match.

    Before the fix: the issue still correctly carried sdlc:goal (goal_label was never actually
    gated on ownership), but assignee stayed None. Given /sigma-setup's own default
    (discovery.github.assignee: "@me"), that made it invisible to EVERY account's assignee-scoped
    pick query -- carrying sdlc:goal and reachable by nobody's loop. "Unowned" had silently become
    "unpickable" too, which is the actual bug: not a missing label, an empty assignee.

    After the fix it self-assigns to the filing actor, so it is genuinely reachable by that
    actor's own loop at minimum, while still being flagged for a human to route correctly."""
    sdlc = _project(tmp_path, codeowners="/server/ @srv-owner\n")   # no rule matches `engine`
    src = FakeSource()
    report = handoff.create_tracked_issue(
        sdlc, ON, "g.md", "engine", "found a bug in the engine while working on the UI",
        same_area=False, immediately_actionable=True, blocks_goal=False, source=src)
    assert report["owner"] == "amy"
    assert src.created["assignee"] == "amy"
    assert src.created["goal_label"] is True                       # sdlc:goal -- already correct
    assert any("no owner for area" in w for w in report["warnings"])
    assert any("self-assigning" in w for w in report["warnings"])
    # a non-blocking finding writes kind="note", not "handoff" -- unaffected by this fix
    entry = ledger.read_all(sdlc)[0]
    assert entry["kind"] == "note" and entry["to"] == "amy"


def test_cross_area_actionable_follow_up_with_real_owner_is_unaffected(tmp_path):
    """Regression for #2395: the self-assign fallback must never override a REAL
    CODEOWNERS-resolved owner -- it only fires when report["owner"] is still falsy. The existing,
    correct case (a cross-area filing that DOES resolve an owner) is unchanged."""
    sdlc = _project(tmp_path)   # default CODEOWNERS: /engine/ -> eng-owner
    src = FakeSource()
    report = handoff.create_tracked_issue(
        sdlc, ON, "g.md", "engine", "found a bug in the engine while working on the UI",
        same_area=False, immediately_actionable=True, blocks_goal=False, source=src)
    assert report["owner"] == "eng-owner"
    assert src.created["assignee"] == "eng-owner"
    assert not any("self-assigning" in w for w in report["warnings"])
    assert not any("no owner for area" in w for w in report["warnings"])


def test_cross_area_queued_finding_with_no_owner_stays_unassigned(tmp_path):
    """#2395: the self-assign fallback is gated on immediately_actionable. A QUEUED
    (needs-confirmation) finding withholds sdlc:goal entirely, so no discovery query ever inspects
    its assignee -- there is nothing to repair, and report["owner"] stays None exactly as before
    this fix, matching this same scenario's pre-existing, still-documented degrade."""
    sdlc = _project(tmp_path, codeowners="/server/ @srv-owner\n")   # no rule matches `engine`
    src = FakeSource()
    report = handoff.create_tracked_issue(
        sdlc, ONL, "g.md", "engine", "found while working elsewhere",
        same_area=False, immediately_actionable=False, blocks_goal=False, source=src)
    assert report["owner"] is None
    assert src.created["assignee"] is None
    assert src.created["goal_label"] is False
    assert any("no owner for area" in w for w in report["warnings"])
    assert not any("self-assigning" in w for w in report["warnings"])


def test_handoff_survives_a_source_that_cannot_open_issues(tmp_path):
    sdlc = _project(tmp_path)

    class Local:
        pass

    report = handoff.hand_off(sdlc, ON, "g.md", "engine", "needs a flag", source=Local())
    assert report["issue"] is None
    assert any("cannot open issues" in w for w in report["warnings"])
    assert ledger.read_all(sdlc)[0]["to"] == "eng-owner"        # the addressee still lands


def test_handoff_survives_append_to_body_failing(tmp_path):
    """The comment (human-visible) and the body marker (machine-readable) are two independent
    channels -- one failing must not lose the other, and neither failing may block the park."""
    sdlc = _project(tmp_path)

    class BodyBroken(FakeSource):
        def append_to_body(self, goal, marker):
            raise RuntimeError("gh: edit failed")

    src = BodyBroken()
    report = handoff.hand_off(sdlc, ON, "g.md", "engine", "needs a flag", source=src)
    assert report["issue"] == "61"
    assert any("machine-readable" in w for w in report["warnings"])
    assert src.notes                                  # the human-visible comment still landed
    assert ledger.read_all(sdlc)                       # the park is never blocked


def test_handoff_degrades_honestly_when_the_source_has_no_append_to_body(tmp_path):
    """A source implementation that predates #376 (or a future non-GitHub source) simply doesn't
    have this method -- hand_off must not assume it does."""
    sdlc = _project(tmp_path)

    class NoBodyEdit:
        def __init__(self):
            self.notes = []

        def create_dependency(self, title, body, assignee, labels=()):
            return "61"

        def note(self, goal, text):
            self.notes.append((goal, text))

    src = NoBodyEdit()
    report = handoff.hand_off(sdlc, ONL, "g.md", "engine", "needs a flag", source=src)
    assert report["issue"] == "61" and not report["warnings"]
    assert src.notes                                   # the comment channel still works fine


def test_handoff_survives_a_failing_host(tmp_path):
    sdlc = _project(tmp_path)

    class Broken(FakeSource):
        def create_dependency(self, *a, **k):
            raise RuntimeError("gh: not authenticated")

    report = handoff.hand_off(sdlc, ON, "g.md", "engine", "needs a flag", source=Broken())
    assert report["issue"] is None
    assert any("not authenticated" in w for w in report["warnings"])
    assert ledger.read_all(sdlc)                                # the park is never blocked


def test_handoff_writes_nothing_when_the_ledger_is_off(tmp_path):
    sdlc = _project(tmp_path, config={"ledger": {"enabled": False}})
    report = handoff.hand_off(sdlc, {"ledger": {"enabled": False}}, "g.md", "engine", "why",
                              source=FakeSource())
    assert report["entry"] is None and report["issue"] == "61"   # the issue is still opened
    assert ledger.read_all(sdlc) == []


def test_dependency_label_is_configurable(tmp_path):
    sdlc = _project(tmp_path)
    cfg = {"ledger": {"enabled": True, "actor": "amy", "handoff": {"label": "needs:dep"}}}
    src = FakeSource()
    handoff.hand_off(sdlc, cfg, "g.md", "engine", "why", source=src)
    assert "needs:dep" in src.created["labels"]


def test_hand_off_degrades_when_source_resolution_itself_raises(tmp_path, monkeypatch):
    """hand_off()'s own source=None fallback: sources.get_source() raising must not raise up through
    hand_off -- create_tracked_issue's identical fallback then also can't open the issue, and reports
    why, exactly as if a source with no create_dependency had been passed."""
    sdlc = _project(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("bad discovery config")

    monkeypatch.setattr(handoff.sources, "get_source", boom)
    report = handoff.hand_off(sdlc, ON, "g.md", "engine", "needs a flag")
    assert report["issue"] is None
    assert any("no backlog source" in w for w in report["warnings"])
    assert ledger.read_all(sdlc)                                # the park is never blocked


# ------------------------------------------------------------------ create_tracked_issue (#462)


def test_create_tracked_issue_same_area_assigns_to_self_and_never_becomes_a_stuck_handoff(tmp_path):
    """#462: same_area=True is the self-assigned-follow-up case -- assigned to ledger.actor() (never
    CODEOWNERS), carries area:/priority: labels, carries the goal label iff immediately_actionable,
    and is recorded as kind="note" (never "handoff") addressed to the filer's own login -- so it can
    NEVER get stuck as a permanently-unanswered hand-off nobody was ever meant to ack."""
    sdlc = _project(tmp_path)
    src = FakeSource(number="61")
    report = handoff.create_tracked_issue(
        sdlc, ONL, "0007-x.md", "engine", "found a flaky retry while working this goal",
        same_area=True, immediately_actionable=True, blocks_goal=False, source=src)

    assert report["owner"] == "amy" and report["issue"] == "61" and not report["warnings"]
    assert src.created["assignee"] == "amy"
    assert "area:engine" in src.created["labels"] and "priority:P1" in src.created["labels"]
    assert src.created["goal_label"] is True                     # immediately_actionable=True
    # #233/#1348: an actionable issue is a real goal, NOT a proposal -- it must NOT carry the
    # needs-confirmation label.
    assert "sdlc:needs-confirmation" not in src.created["labels"]

    entries = ledger.read_all(sdlc)
    entry = entries[0]
    assert entry["kind"] == "note" and entry["to"] == "amy" and "state" not in entry
    assert ledger.outstanding(entries) == []                     # never a stuck, unanswerable hand-off

    # blocks_goal=False in this call -- no body marker (the false-blocking regression, unit-proven
    # again here at the FakeSource level for the same_area=True path specifically).
    assert src.body_appends == []


def test_create_tracked_issue_queued_does_not_carry_the_goal_label(tmp_path):
    """With triage OFF, immediately_actionable=False -> queued: filed, but NOT auto-picked --
    goal_label=False must reach create_dependency explicitly (unlike the True case, which relies on
    create_dependency's own default). Slice 15 (#1003): with triage on (the default) the same call is
    armed instead; see test_queued_flag_defers_to_triage."""
    sdlc = _project(tmp_path)
    src = FakeSource(number="62")
    handoff.create_tracked_issue(
        sdlc, ONL, "0008-x.md", "engine", "a non-urgent follow-up, not worth jumping the backlog",
        same_area=True, immediately_actionable=False, blocks_goal=False, source=src)
    assert src.created["goal_label"] is False
    # #233/#1348: withholding sdlc:goal is what makes it invisible to `loop.py next`; the DISTINCT
    # sdlc:needs-confirmation label is what makes the pending set queryable rather than
    # inferred-from-missing.
    assert "sdlc:needs-confirmation" in src.created["labels"]


def test_create_tracked_issue_degrades_when_no_backlog_source_is_configured(tmp_path, monkeypatch):
    """create_tracked_issue's own source=None fallback: sources.get_source() raising must not raise
    up through it -- the tracked issue is never opened, the ledger entry still lands, and the warning
    says why."""
    sdlc = _project(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("bad discovery config")

    monkeypatch.setattr(handoff.sources, "get_source", boom)
    report = handoff.create_tracked_issue(
        sdlc, ON, "g.md", "engine", "why", same_area=False, immediately_actionable=True,
        blocks_goal=True)
    assert report["issue"] is None
    assert any("no backlog source" in w for w in report["warnings"])
    assert ledger.read_all(sdlc)                                # the park is never blocked


def test_create_tracked_issue_survives_note_failing(tmp_path):
    """The human-visible note() comment and the machine-readable body marker are two independent
    channels for create_tracked_issue too (not just hand_off's blocks_goal=True case) -- one failing
    must not lose the other."""
    class NoteBroken(FakeSource):
        def note(self, goal, text):
            raise RuntimeError("gh: comment failed")

    sdlc = _project(tmp_path)
    src = NoteBroken()
    report = handoff.create_tracked_issue(
        sdlc, ON, "g.md", "engine", "why", same_area=False, immediately_actionable=True,
        blocks_goal=True, source=src)
    assert report["issue"] == "61"
    assert any("could not comment on the issue" in w for w in report["warnings"])
    assert src.body_appends                                     # the body-marker channel still landed


def test_create_tracked_issue_same_area_blocks_goal_warns_when_the_block_could_not_land(tmp_path):
    """#469: same_area=True's ONLY enforcement channel for blocks_goal=True is the body marker —
    the ledger fallback `(not same_area) and blocks_goal` never fires for same_area=True BY DESIGN
    (#466 review: kind="handoff" is reserved for a genuine cross-area dependency precisely so it can
    never become a permanently-unanswered entry nobody was ever meant to ack — see
    test_create_tracked_issue_same_area_assigns_to_self_and_never_becomes_a_stuck_handoff, unchanged
    by this fix). When the body marker never runs because no issue number exists (a source with no
    create_dependency, or one whose call raised), the caller's explicit blocks_goal=True silently did
    nothing — the generic "backlog source cannot open issues" warning never says the BLOCK itself
    failed, so a caller reading report["warnings"] (or the CLI's own stderr echo of it) has no way to
    tell "no issue, but still blocked" (the cross-area case, ledger-backed) from "no issue, and NOT
    blocked at all" (this one) apart from independently re-deriving the same_area/ledger-kind logic
    themselves."""
    sdlc = _project(tmp_path)

    class NoDependencies:
        """A source honestly lacking `create_dependency` — the duck-typed gate exists precisely for
        an implementation like this (a source that predates it, or one that simply cannot open
        issues at all), not a hypothetical."""

    report = handoff.create_tracked_issue(
        sdlc, ON, "g.md", "engine", "needs X before this can land",
        same_area=True, immediately_actionable=True, blocks_goal=True, source=NoDependencies())

    assert report["issue"] is None
    entries = ledger.read_all(sdlc)
    assert entries[0]["kind"] == "note"            # #466 invariant, unchanged: same_area is never "handoff"
    assert ledger.outstanding(entries) == []       # ...so still never a stuck, unanswerable entry
    assert any("blocks_goal" in w and "NOT actually blocked" in w for w in report["warnings"]), (
        f"the caller's explicit block silently failed with no specific warning: {report['warnings']}")


def test_create_tracked_issue_same_area_blocks_goal_warns_when_append_to_body_is_missing(tmp_path):
    """#726, the follow-up #469's own post-PR review found: #469's guard only ever checked
    `not report["issue"]`, which fires when NO issue exists to hold a marker at all -- but a source
    that HAS a working create_dependency and simply has no append_to_body (LocalSource's exact shape
    before #726, not a hypothetical) sails straight through untouched. report["issue"] is truthy
    (create_dependency really did succeed), the #466 ledger kind="note"/never-outstanding invariant
    holds, and yet the "Blocked by" marker was never written anywhere -- today, report["warnings"]
    comes back completely empty: a clean report for a block that did not happen."""
    sdlc = _project(tmp_path)

    class CreatesButCannotAppend:
        """Honestly has create_dependency (so report["issue"] gets set) but no append_to_body at
        all -- exactly LocalSource's own shape before #726."""

        def __init__(self):
            self.notes = []

        def create_dependency(self, title, body, assignee, labels=(), goal_label=True):
            return "42"

        def note(self, goal, text):
            self.notes.append((goal, text))

    src = CreatesButCannotAppend()
    report = handoff.create_tracked_issue(
        sdlc, ON, "g.md", "engine", "needs X before this can land",
        same_area=True, immediately_actionable=True, blocks_goal=True, source=src)

    assert report["issue"] == "42"                  # the dependency issue really was created
    entries = ledger.read_all(sdlc)
    assert entries[0]["kind"] == "note"              # #466 invariant, unchanged by this fix
    assert ledger.outstanding(entries) == []         # ...so still never a stuck, unanswerable entry
    assert report["warnings"], (
        "blocks_goal=True silently did not happen: no marker was ever written and no warning named "
        "the failure, so a caller reading report['warnings'] has no way to know the block never "
        "landed")
    assert any("blocks_goal" in w and "NOT actually blocked" in w for w in report["warnings"]), (
        f"warnings did not name the failed block: {report['warnings']}")


def test_create_tracked_issue_same_area_blocks_goal_actually_blocks_in_local_mode(tmp_path):
    """#726 direction (a): LocalSource gets a real append_to_body, so a same_area=True,
    blocks_goal=True call in local mode gets a WORKING enforcement channel, not merely a warning
    when it fails. The marker has to land in the real goal file's own body (after the frontmatter
    fence) because that is exactly what backlog_check._build_corpus() reads (via
    frontmatter.strip()) and _explicit_blockers() regexes -- proven here against the real regex, not
    a hand-copied stand-in."""
    base = tmp_path / ".sdlc"
    (base / "goals").mkdir(parents=True)
    config = {"discovery": {"source": "local-goals"}, "ai_filed": {"triage": {"enabled": False}}}
    (base / "config.json").write_text(json.dumps(config), encoding="utf-8")
    current = base / "goals" / "0001-current.md"
    current.write_text(
        '---\nid: 0001\ntitle: "current work"\nstatus: pending\n---\nbody text here\n',
        encoding="utf-8")

    src = _mod("sources").LocalSource(str(base))
    report = handoff.create_tracked_issue(
        str(base), config, str(current), "api", "needs X before this can land",
        same_area=True, immediately_actionable=True, blocks_goal=True, source=src)

    assert report["issue"] == 2 and not report["warnings"]    # the block landed; no warning needed
    text = current.read_text(encoding="utf-8")
    assert "**Blocked by:** #2" in text
    assert 'title: "current work"' in text and "body text here" in text   # frontmatter + body intact

    backlog_check = _mod("backlog_check")
    assert backlog_check._BLOCK_RE.search(_mod("frontmatter").strip(text))


# --- #1204: file-time duplicate search --------------------------------------------------------
# `create_tracked_issue` used to go straight from label assembly to `source.create_dependency`
# with no search between -- the reusable dedup engine (`dedup.py`, #917) existed but only ever ran
# at PICK time (`backlog_check.cross_check`, itself opt-in), never at FILE time. Two independent
# sessions filing the same root cause could each open their own issue and neither would know.
#
# Low-level tests below drive `create_tracked_issue` with `handoff._duplicate_search` monkeypatched
# to an exact, controlled pack -- isolating the three branches (duplicate / weak match / no match)
# from the real TF-IDF scoring `_duplicate_search` itself sits on top of. The end-to-end tests
# further down drive the REAL search (no monkeypatch), in both github and local-goals mode, and are
# the literal acceptance test: filing the identical follow-up text twice must produce ONE issue and
# ONE duplicate-context comment, never two issues.


def test_create_tracked_issue_duplicate_match_reuses_the_existing_issue_and_comments(tmp_path, monkeypatch):
    sdlc = _project(tmp_path)
    monkeypatch.setattr(handoff, "_duplicate_search", lambda *a, **k: {
        "schema": "brainstorm-dedup/v1",
        "candidates": [{"ref": "77", "score": 0.91, "state": "open", "strength": "duplicate",
                        "source": "mirror", "evidence": ["retry", "pool"]}],
        "degraded": []})
    src = FakeSource(number="61")
    report = handoff.create_tracked_issue(
        sdlc, ON, "0009-x.md", "engine", "already-seen root cause", same_area=True,
        immediately_actionable=True, blocks_goal=False, source=src)

    assert report["issue"] == "77" and report["duplicate_of"] == "77"
    assert src.created is None                              # no NEW issue was ever opened
    assert any(goal == "77" for goal, _ in src.notes)        # the existing issue got the context comment
    assert any("duplicate found" in w and "#77" in w for w in report["warnings"])
    # the ledger + narrative-comment machinery downstream of report["issue"] still treats #77
    # exactly as it would a freshly-opened issue -- same_area=True never becomes a stuck hand-off.
    assert ledger.read_all(sdlc)[0]["issue"] == 77
    # #2133: the narrative comment posted on the FILING goal (never the reused issue) must not
    # claim #77 was OPENED -- it was a file-time-duplicate-search MATCH, and reporting a match as
    # a creation is exactly the silent, exit-0 defect #2133 records.
    goal_note = next(text for goal, text in src.notes if goal == "0009-x.md")
    assert "matched existing #77" in goal_note
    assert "opened #77" not in goal_note


def test_create_tracked_issue_blocking_duplicate_match_also_says_matched_not_opened(
        tmp_path, monkeypatch):
    """#2133: the SAME bug, the `blocks_goal=True` narrative branch ("Blocked by a `area`
    dependency -- opened #N") -- reachable from `track --blocks yes` as well as from `hand_off()`/
    `open`, not just the non-blocking "Related ..." branch #2133's own evidence quoted."""
    sdlc = _project(tmp_path)
    monkeypatch.setattr(handoff, "_duplicate_search", lambda *a, **k: {
        "schema": "brainstorm-dedup/v1",
        "candidates": [{"ref": "77", "score": 0.91, "state": "open", "strength": "duplicate",
                        "source": "mirror", "evidence": ["retry", "pool"]}],
        "degraded": []})
    src = FakeSource(number="61")
    report = handoff.create_tracked_issue(
        sdlc, ON, "0009-x.md", "engine", "already-seen root cause", same_area=True,
        immediately_actionable=True, blocks_goal=True, source=src)
    assert report["duplicate_of"] == "77"
    goal_note = next(text for goal, text in src.notes if goal == "0009-x.md")
    assert "matched existing #77" in goal_note
    assert "opened #77" not in goal_note


def test_create_tracked_issue_weak_match_still_files_and_records_candidate_refs(tmp_path, monkeypatch):
    sdlc = _project(tmp_path)
    monkeypatch.setattr(handoff, "_duplicate_search", lambda *a, **k: {
        "schema": "brainstorm-dedup/v1",
        "candidates": [{"ref": "88", "score": 0.30, "state": "open", "strength": "related",
                        "source": "mirror", "evidence": ["pool"]}],
        "degraded": []})
    src = FakeSource(number="61")
    report = handoff.create_tracked_issue(
        sdlc, ON, "0010-x.md", "engine", "maybe related", same_area=True,
        immediately_actionable=True, blocks_goal=False, source=src)

    assert report["issue"] == "61" and report["duplicate_of"] is None
    assert src.created is not None                           # a real new issue WAS filed
    assert any("#88" in w and "0.3" in w for w in report["warnings"])


def test_create_tracked_issue_no_match_files_normally_with_no_extra_warnings(tmp_path, monkeypatch):
    sdlc = _project(tmp_path)
    monkeypatch.setattr(handoff, "_duplicate_search", lambda *a, **k: {
        "schema": "brainstorm-dedup/v1", "candidates": [], "degraded": []})
    src = FakeSource(number="61")
    report = handoff.create_tracked_issue(
        sdlc, ON, "0012-x.md", "engine", "genuinely new", same_area=True,
        immediately_actionable=True, blocks_goal=False, source=src)
    assert report["issue"] == "61" and report["duplicate_of"] is None and not report["warnings"]
    assert src.created is not None


def test_create_tracked_issue_dedup_false_never_calls_the_duplicate_search(tmp_path, monkeypatch):
    """#1204 review: the escape hatch itself, at the lowest level — `dedup=False` must skip
    `_duplicate_search` altogether, not merely discard whatever it would have returned. Proven by
    making the search explode if it is ever invoked at all."""
    sdlc = _project(tmp_path)

    def boom(*a, **k):
        raise AssertionError("_duplicate_search must not run when dedup=False")

    monkeypatch.setattr(handoff, "_duplicate_search", boom)
    src = FakeSource(number="61")
    report = handoff.create_tracked_issue(
        sdlc, ON, "0014-x.md", "engine", "why", same_area=True, immediately_actionable=True,
        blocks_goal=False, source=src, dedup=False)

    assert report["issue"] == "61" and report["duplicate_of"] is None and not report["warnings"]
    assert src.created is not None


def test_create_tracked_issue_dedup_false_fixes_the_decompose_boilerplate_collision(tmp_path):
    """The literal #1204 review finding, reproduced end to end at the handoff layer against the
    REAL, unmocked dedup/mirror engine: `decompose_goal.render_meta_body`'s template is ~99% fixed
    prose with only the parent id/title varying, so filing goal #303's meta-issue with the pre-fix
    default (`dedup=True`) against a corpus containing goal #101's ALREADY-FILED, wholly unrelated
    meta-issue reuses #101's issue — the exact wrong-parent-reuse bug. `dedup=False` (what loop.py's
    decompose file-mode caller now passes — see loop.py's `decompose_check`) must file #303's own
    new issue instead, against the identical corpus, with no mocking of the scoring itself."""
    sdlc = _project(tmp_path)
    cfg = {"ledger": {"enabled": True, "actor": "amy"},
           "discovery": {"source": "github", "github": {"repo": "acme/widget"}}}
    dg = _mod("decompose_goal")
    prior_body = dg.render_meta_body(101, 4)
    board = {"open": [{"number": 501, "title": "Decompose #101: payment gateway integration",
                       "body": prior_body, "state": "open",
                       "labels": [{"name": "sdlc:decompose"}]}]}

    def run(args):
        return "[]" if "--state closed" in " ".join(args) else json.dumps(board["open"])

    meta_title = "Decompose #303: dark mode theming for the settings page"
    meta_body = dg.render_meta_body(303, 4)

    buggy_report = handoff.create_tracked_issue(
        sdlc, cfg, "303", "unknown", "oversized goal — needs decomposition before implementation",
        same_area=True, immediately_actionable=True, blocks_goal=False,
        title=meta_title, body=meta_body, extra_labels=["sdlc:decompose"],
        source=FakeSource(number="900"), run=run)          # dedup defaults True -- the bug
    assert buggy_report["duplicate_of"] == "501", \
        "sanity check: without the fix this wrong-parent collision must still reproduce"

    src2 = FakeSource(number="900")
    fixed_report = handoff.create_tracked_issue(
        sdlc, cfg, "303", "unknown", "oversized goal — needs decomposition before implementation",
        same_area=True, immediately_actionable=True, blocks_goal=False,
        title=meta_title, body=meta_body, extra_labels=["sdlc:decompose"],
        source=src2, run=run, dedup=False)

    assert fixed_report["duplicate_of"] is None and fixed_report["issue"] == "900"
    assert src2.created is not None


def test_duplicate_search_fails_open_when_the_corpus_fetch_itself_raises(tmp_path, monkeypatch):
    """`_duplicate_search`'s own documented contract: ANY failure anywhere in the
    fetch-corpus/find-candidates chain degrades to an empty pack, never a raise -- proven directly
    against the real function (not a stand-in), with the failure injected at the corpus-fetch step
    a `gh` outage would actually hit."""
    sdlc = _project(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("gh: not authenticated")

    monkeypatch.setattr(handoff.mirror, "fetch_dependency_records", boom)
    pack = handoff._duplicate_search(sdlc, {"discovery": {"source": "github"}}, "t", "body text")
    assert pack == {"schema": "brainstorm-dedup/v1", "candidates": [], "degraded": ["error"]}


def test_create_tracked_issue_still_files_when_the_duplicate_search_itself_errors(tmp_path, monkeypatch):
    """The outer acceptance contract: a corpus read failure must cost the follow-up its dedup
    check, never its filing. Unlike the test above (which pins `_duplicate_search` in isolation),
    this drives the failure through the REAL, unmocked `_duplicate_search` and `create_tracked_issue`
    call path -- the injected failure is `dedup.find_candidates` itself (the one call
    `_duplicate_search`'s own try/except wraps), not `_duplicate_search` as a whole: replacing the
    whole function would also remove ITS internal safety net, proving nothing about that net."""
    sdlc = _project(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(handoff.dedup, "find_candidates", boom)
    src = FakeSource(number="61")
    report = handoff.create_tracked_issue(
        sdlc, ON, "0011-x.md", "engine", "why", same_area=True, immediately_actionable=True,
        blocks_goal=False, source=src)
    assert report["issue"] == "61" and report["duplicate_of"] is None
    assert src.created is not None


def test_duplicate_search_excludes_the_filing_goal_from_its_own_corpus(tmp_path, monkeypatch):
    """#1204 post-implementation fix: without `exclude_refs`, the CURRENT goal (itself a corpus
    member -- an open local file, or an open github issue) routinely shares vocabulary with a
    same-area follow-up filed FROM it ("the retry wrapper releases the pooled connection twice",
    filed from a goal titled "add backoff to the retry wrapper") -- measured at 0.72 against a real,
    non-degenerate fixture, comfortably past the 0.45 duplicate line, with no actual prior issue
    involved. `_duplicate_search` must hand `dedup.find_candidates` an `exclude_refs` covering the
    filing goal's own ref, or every same-area follow-up risks reading its own parent goal as an
    already-filed duplicate of itself."""
    captured = {}

    def spy_find_candidates(sdlc_dir, text, config=None, title="", exclude_refs=None, **k):
        captured["exclude_refs"] = exclude_refs
        return {"schema": "brainstorm-dedup/v1", "candidates": [], "degraded": []}

    monkeypatch.setattr(handoff.dedup, "find_candidates", spy_find_candidates)
    monkeypatch.setattr(handoff.mirror, "fetch_dependency_records", lambda *a, **k: None)
    sdlc = _project(tmp_path)
    src = FakeSource(number="61")
    handoff.create_tracked_issue(
        sdlc, ON, "0013-current.md", "engine", "why", same_area=True,
        immediately_actionable=True, blocks_goal=False, source=src)
    assert captured["exclude_refs"] == ["0013-current.md"]


def test_duplicate_search_local_mode_self_match_regression(tmp_path):
    """Reproduces the exact false positive `exclude_refs` above fixes, end to end and unmocked: a
    same-area follow-up that shares real vocabulary with the goal it's filed FROM must never read
    that goal's own file as an already-filed duplicate of itself."""
    base = tmp_path / ".sdlc"
    (base / "goals").mkdir(parents=True)
    config = {"discovery": {"source": "local-goals"}, "ai_filed": {"triage": {"enabled": False}}}
    (base / "config.json").write_text(json.dumps(config), encoding="utf-8")
    current = base / "goals" / "0001-retry.md"
    current.write_text(
        '---\nid: 0001\ntitle: "Add exponential backoff to the retry wrapper"\nstatus: pending\n'
        "---\nthe retry wrapper needs backoff before it retries a failed connection to the pool\n",
        encoding="utf-8")

    src = _mod("sources").LocalSource(str(base))
    report = handoff.create_tracked_issue(
        str(base), config, str(current), "engine",
        "the retry wrapper releases the pooled connection twice under contention",
        same_area=True, immediately_actionable=True, blocks_goal=False,
        title="Retry logic double-frees the connection pool", source=src)

    assert report["duplicate_of"] is None                    # NOT a duplicate of its own parent goal
    assert (base / "goals" / "0002-retry-logic-double-frees-the-connection-pool.md").exists()


def test_filing_the_same_followup_text_twice_in_local_mode_produces_one_issue_and_one_comment(tmp_path):
    """The literal #1204 acceptance test, local-goals mode: two DIFFERENT in-progress goals
    independently discover and file the exact same root cause. The first genuinely files a new
    issue; the second must find it (unmocked -- the real `_build_corpus`/dedup engine, over the
    real goal file the first call wrote to disk) and reuse it -- one goal file, one journal comment,
    never two goal files."""
    base = tmp_path / ".sdlc"
    (base / "goals").mkdir(parents=True)
    config = {"discovery": {"source": "local-goals"}, "ledger": {"enabled": True, "actor": "amy"}}
    (base / "config.json").write_text(json.dumps(config), encoding="utf-8")
    # deliberately no shared vocabulary with the follow-up OR with `_tracked_issue_body`'s own
    # boilerplate ("Raised automatically ... from goal ... Area ... What is needed") -- a tiny
    # (2-document) corpus makes IDF sensitive enough that even one coincidentally shared word (e.g.
    # a bystander title that itself contains "goal") can spike a bogus match.
    for name, title in (("0001-a.md", "Update onboarding tutorial screenshots"),
                        ("0002-b.md", "Polish the settings page icons")):
        (base / "goals" / name).write_text(
            f'---\nid: {name[:4]}\ntitle: "{title}"\nstatus: pending\n---\nunrelated body text\n',
            encoding="utf-8")

    src = _mod("sources").LocalSource(str(base))
    title = "Retry logic double-frees the connection pool"
    why = "the retry wrapper releases the pooled connection twice under contention"

    r1 = handoff.create_tracked_issue(
        str(base), config, str(base / "goals" / "0001-a.md"), "engine", why, same_area=True,
        immediately_actionable=True, blocks_goal=False, title=title, source=src)
    r2 = handoff.create_tracked_issue(
        str(base), config, str(base / "goals" / "0002-b.md"), "engine", why, same_area=True,
        immediately_actionable=True, blocks_goal=False, title=title, source=src)

    assert r1["duplicate_of"] is None                         # first filing: genuinely new
    assert r2["duplicate_of"] == r1["issue"]                  # second filing: found + reused the first
    assert r1["issue"] == r2["issue"]

    followups = [p for p in (base / "goals").glob("*.md") if p.name not in ("0001-a.md", "0002-b.md")]
    assert len(followups) == 1, f"expected exactly one filed follow-up, got {[p.name for p in followups]}"

    journal = (base / "journey" / followups[0].stem).with_suffix(".md")
    assert journal.read_text(encoding="utf-8").count("An automated duplicate search (#1204)") == 1


def test_filing_the_same_followup_text_twice_in_github_mode_produces_one_issue_and_one_comment(tmp_path):
    """The literal #1204 acceptance test, github mode: this is the real motivating shape (two
    people/sessions on the same board), driven through the REAL `mirror.fetch_dependency_records`
    + `dedup.find_candidates`, not a stand-in. A synthetic `gh issue list` responds with nothing on
    the first filing and, after it, with the just-filed issue -- exactly what the live board would
    reflect the second time an independent session searches it."""
    sdlc = _project(tmp_path)
    cfg = {"ledger": {"enabled": True, "actor": "amy"},
           "discovery": {"source": "github", "github": {"repo": "acme/widget", "assignee": "@me"}}}
    board = {"open": []}

    def run(args):
        return "[]" if "--state closed" in " ".join(args) else json.dumps(board["open"])

    title = "Retry logic double-frees the connection pool"
    why = "found while working the goal: the retry wrapper releases the pooled connection twice"

    src1 = FakeSource(number="501")
    r1 = handoff.create_tracked_issue(
        sdlc, cfg, "0001-a.md", "engine", why, same_area=True, immediately_actionable=False,
        blocks_goal=False, title=title, source=src1, run=run)
    assert r1["issue"] == "501" and r1["duplicate_of"] is None
    assert src1.created is not None                          # one issue opened

    # the board now reflects the just-filed issue -- queued, so sdlc:needs-confirmation, no sdlc:goal
    board["open"] = [{"number": 501, "title": title, "body": src1.created["body"], "state": "open",
                      "labels": [{"name": "sdlc:needs-confirmation"}]}]

    src2 = FakeSource(number="502")
    r2 = handoff.create_tracked_issue(
        sdlc, cfg, "0002-b.md", "engine", why, same_area=True, immediately_actionable=False,
        blocks_goal=False, title=title, source=src2, run=run)

    assert r2["duplicate_of"] == "501" and r2["issue"] == "501"
    assert src2.created is None                               # no second issue was ever opened
    # exactly one duplicate-context comment landed on the reused issue
    dup_comments = [t for goal, t in src2.notes if goal == "501" and "#1204" in t]
    assert len(dup_comments) == 1


def test_duplicate_search_not_scoped_by_assignee_or_goal_label_at_the_handoff_integration_level(tmp_path, monkeypatch):
    """Integration-level pin (unit-level coverage of `mirror.fetch_dependency_records` itself
    already lives in test_mirror.py): `_duplicate_search` must hand it BOTH labels
    (`discovery.github.goal_label` and the proposed label), never restricted to one, and the
    resulting corpus must not depend on `discovery.github.assignee` at all -- proven here by
    configuring an assignee filter and confirming a record that would fail it (no assignee field at
    all in the synthetic `gh` response) still surfaces as a duplicate."""
    sdlc = _project(tmp_path)
    cfg = {"ledger": {"enabled": True, "actor": "amy"},
           "discovery": {"source": "github",
                         "github": {"repo": "acme/widget", "assignee": "@me", "goal_label": "sdlc:goal"}}}
    calls = []

    def run(args):
        calls.append(list(args))
        # #1833: `fetch_dependency_records` now reaches `gh` through `sources.fetch_issues_rest`
        # (`gh api repos/{owner}/{repo}/issues -f state=... -f labels=...`), not `gh issue list`.
        if any(v == "state=closed" for v in args):
            return "[]"
        return json.dumps([{"number": 9, "title": "Retry logic double-frees the connection pool",
                            "body": "the retry wrapper releases the pooled connection twice",
                            "state": "open", "labels": [{"name": "sdlc:needs-confirmation"}]}])

    report = handoff.create_tracked_issue(
        sdlc, cfg, "0001-a.md", "engine", "the retry wrapper releases the pooled connection twice",
        same_area=True, immediately_actionable=False, blocks_goal=False,
        title="Retry logic double-frees the connection pool", source=FakeSource(number="61"), run=run)

    assert report["duplicate_of"] == "9"                     # found despite no --assignee match
    joined_calls = [" ".join(c) for c in calls]
    assert not any("assignee=" in c for c in joined_calls)
    assert any("labels=sdlc:goal" in c for c in joined_calls)
    assert any("labels=sdlc:needs-confirmation" in c for c in joined_calls)


# ------------------------------------------------------------------ #1203: a failed filing must not
# report success


class RaisesOnCreate(FakeSource):
    """Shape (a) from #1203: the create call itself raises."""
    def create_dependency(self, *a, **k):
        raise RuntimeError("gh: not authenticated")


class ReturnsNoneOnCreate(FakeSource):
    """Shape (b) from #1203: `gh` runs, produces no exception, but its stdout carried no issue
    number -- exactly `GitHubSource._create_issue`'s own `return number if number.isdigit() else
    None` when the create silently fails server-side. No exception ever crosses this boundary."""
    def create_dependency(self, *a, **k):
        return None


def test_create_tracked_issue_marks_issue_attempted_when_the_source_can_create(tmp_path):
    """`issue_attempted` is the CLI dispatcher's only way to tell 'we tried and it failed' apart
    from 'there was nothing to try' (no source, or a source honestly lacking create_dependency) --
    both leave report["issue"] as None, but only the first is a real filing failure."""
    sdlc = _project(tmp_path)
    report = handoff.create_tracked_issue(
        sdlc, ON, "g.md", "engine", "why", same_area=True, immediately_actionable=True,
        blocks_goal=False, source=FakeSource())
    assert report["issue_attempted"] is True and report["issue"] == "61"


def test_create_tracked_issue_issue_attempted_is_false_with_no_capable_source(tmp_path):
    sdlc = _project(tmp_path)

    class Local:
        pass

    report = handoff.create_tracked_issue(
        sdlc, ON, "g.md", "engine", "why", same_area=True, immediately_actionable=True,
        blocks_goal=False, source=Local())
    assert report["issue_attempted"] is False and report["issue"] is None


def test_create_tracked_issue_warns_on_shape_b_even_though_it_never_raised(tmp_path):
    """The corrected report's own headline finding: a `create_dependency` that returns None
    WITHOUT raising previously left report["warnings"] completely empty -- the one shape with no
    diagnostic at all. It must now say something, while create_tracked_issue itself still never
    raises (unchanged contract)."""
    sdlc = _project(tmp_path)
    report = handoff.create_tracked_issue(
        sdlc, ON, "g.md", "engine", "why", same_area=True, immediately_actionable=True,
        blocks_goal=False, source=ReturnsNoneOnCreate())
    assert report["issue"] is None and report["issue_attempted"] is True
    assert report["warnings"], "shape (b) must not be silent"


# --------------------------------------------------------- #1203 x #1204 composition: the #1204
# duplicate-reuse path (report["issue"] set by finding an EXISTING issue, never by calling
# create_dependency at all) must read as a SUCCESS to #1203's issue_attempted/exit-code check, not
# as a filing that was "attempted and failed".


def test_create_tracked_issue_duplicate_reuse_still_marks_issue_attempted_and_is_not_a_failure(
        tmp_path, monkeypatch):
    sdlc = _project(tmp_path)
    monkeypatch.setattr(handoff, "_duplicate_search", lambda *a, **k: {
        "schema": "brainstorm-dedup/v1",
        "candidates": [{"ref": "77", "score": 0.91, "state": "open", "strength": "duplicate",
                        "source": "mirror", "evidence": ["retry", "pool"]}],
        "degraded": []})
    report = handoff.create_tracked_issue(
        sdlc, ON, "0009-x.md", "engine", "already-seen root cause", same_area=True,
        immediately_actionable=True, blocks_goal=False, source=FakeSource(number="61"))

    assert report["issue"] == "77" and report["duplicate_of"] == "77"
    # a capable source resolved this filing (by reuse, not by a fresh create_dependency call) --
    # issue_attempted is True, exactly as it would be for a fresh creation, and report["issue"]
    # being truthy is what keeps the CLI dispatcher's `issue_attempted and not issue` check from
    # ever reading a duplicate reuse as a failed filing.
    assert report["issue_attempted"] is True


def test_cli_open_exits_zero_when_a_duplicate_is_found_and_reused(tmp_path, monkeypatch):
    sdlc = _project(tmp_path)
    monkeypatch.setattr(handoff.sources, "get_source", lambda *a, **k: FakeSource(number="61"))
    monkeypatch.setattr(handoff, "_duplicate_search", lambda *a, **k: {
        "schema": "brainstorm-dedup/v1",
        "candidates": [{"ref": "77", "score": 0.91, "state": "open", "strength": "duplicate",
                        "source": "mirror", "evidence": ["retry", "pool"]}],
        "degraded": []})
    rc = handoff.main(["handoff.py", "open", str(sdlc), "g.md", "--area", "engine",
                       "--why", "already-seen root cause"])
    assert rc == 0


def test_cli_track_exits_zero_when_a_duplicate_is_found_and_reused(tmp_path, monkeypatch):
    sdlc = _project(tmp_path)
    monkeypatch.setattr(handoff.sources, "get_source", lambda *a, **k: FakeSource(number="61"))
    monkeypatch.setattr(handoff, "_duplicate_search", lambda *a, **k: {
        "schema": "brainstorm-dedup/v1",
        "candidates": [{"ref": "77", "score": 0.91, "state": "open", "strength": "duplicate",
                        "source": "mirror", "evidence": ["retry", "pool"]}],
        "degraded": []})
    rc = handoff.main(["handoff.py", "track", str(sdlc), "g.md", "--area", "engine",
                       "--why", "already-seen root cause", "--queue", "queued",
                       "--assignee", "same-area", "--blocks", "no"])
    assert rc == 0


def test_cli_track_stdout_says_matched_existing_not_opened_on_a_duplicate_reuse(
        tmp_path, capsys, monkeypatch):
    """#2133: the literal line this issue's evidence quotes -- `tracked to <owner> as #N` -- must
    not read as a creation when `create_tracked_issue` actually reused an EXISTING issue the
    file-time duplicate search matched. This is the exact live-evidence shape from goal #1757: a
    caller reading four `-- opened #N` outcomes for what was really one filing and three reuses."""
    sdlc = _project(tmp_path)
    monkeypatch.setattr(handoff.sources, "get_source", lambda *a, **k: FakeSource(number="61"))
    monkeypatch.setattr(handoff, "_duplicate_search", lambda *a, **k: {
        "schema": "brainstorm-dedup/v1",
        "candidates": [{"ref": "77", "score": 0.91, "state": "open", "strength": "duplicate",
                        "source": "mirror", "evidence": ["retry", "pool"]}],
        "degraded": []})
    rc = handoff.main(["handoff.py", "track", str(sdlc), "g.md", "--area", "engine",
                       "--why", "already-seen root cause", "--queue", "queued",
                       "--assignee", "same-area", "--blocks", "no"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "matched existing #77" in out
    assert "as #77" not in out


def test_failed_cross_area_blocking_handoff_row_is_discoverable_via_outstanding(tmp_path):
    """AC: a ledger row written with issue:null must be discoverable by an existing report. A
    genuine cross-area blocking hand-off (same_area=False, blocks_goal=True) always writes
    kind="handoff" regardless of whether the issue itself landed -- ledger.outstanding() already
    surfaces it, issue number or not."""
    sdlc = _project(tmp_path)
    report = handoff.create_tracked_issue(
        sdlc, ON, "g.md", "engine", "needs X", same_area=False, immediately_actionable=True,
        blocks_goal=True, source=RaisesOnCreate())
    assert report["issue"] is None
    entries = ledger.read_all(sdlc)
    # append() omits a None-valued field entirely rather than storing a literal null -- the ledger
    # row genuinely has no "issue" key at all, not `"issue": null`.
    assert entries[0]["kind"] == "handoff" and entries[0].get("issue") is None
    assert entries[0] in ledger.outstanding(entries)


def test_failed_same_area_note_row_is_discoverable_via_addressed_to(tmp_path):
    """AC: same story for the same-area/non-blocking shape -- it writes kind="note" addressed to
    the filer themselves (ledger.actor() never returns empty), so a failed filing still shows up
    under ledger.addressed_to(entries, actor) / TEAM.md, not just as a vanished stderr line."""
    sdlc = _project(tmp_path)
    report = handoff.create_tracked_issue(
        sdlc, ON, "g.md", "engine", "found a flaky retry", same_area=True,
        immediately_actionable=True, blocks_goal=False, source=ReturnsNoneOnCreate())
    assert report["issue"] is None
    entries = ledger.read_all(sdlc)
    assert entries[0]["kind"] == "note" and entries[0]["to"] == "amy"
    assert entries[0] in ledger.addressed_to(entries, "amy")


def test_cli_open_exits_nonzero_when_create_dependency_raises(tmp_path, capsys, monkeypatch):
    sdlc = _project(tmp_path)
    monkeypatch.setattr(handoff.sources, "get_source", lambda *a, **k: RaisesOnCreate())
    rc = handoff.main(["handoff.py", "open", str(sdlc), "g.md", "--area", "engine",
                       "--why", "needs a flag"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "g.md" in err and "engine" in err and "eng-owner" in err


def test_cli_open_exits_nonzero_when_create_dependency_returns_none(tmp_path, capsys, monkeypatch):
    sdlc = _project(tmp_path)
    monkeypatch.setattr(handoff.sources, "get_source", lambda *a, **k: ReturnsNoneOnCreate())
    rc = handoff.main(["handoff.py", "open", str(sdlc), "g.md", "--area", "engine",
                       "--why", "needs a flag"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "g.md" in err and "engine" in err and "eng-owner" in err


def test_cli_open_still_exits_zero_when_the_source_cannot_open_issues_at_all(tmp_path, monkeypatch):
    """No regression to the pre-existing, deliberately-degraded local/no-gh mode: nothing was ever
    attempted, so there is nothing to report as a failed filing."""
    sdlc = _project(tmp_path)

    class Local:
        pass

    monkeypatch.setattr(handoff.sources, "get_source", lambda *a, **k: Local())
    assert handoff.main(["handoff.py", "open", str(sdlc), "g.md", "--area", "engine",
                         "--why", "needs a flag"]) == 0


def test_cli_track_exits_nonzero_when_create_dependency_raises(tmp_path, capsys, monkeypatch):
    sdlc = _project(tmp_path)
    monkeypatch.setattr(handoff.sources, "get_source", lambda *a, **k: RaisesOnCreate())
    rc = handoff.main(["handoff.py", "track", str(sdlc), "g.md", "--area", "engine",
                       "--why", "found something mid-goal", "--queue", "queued",
                       "--assignee", "same-area", "--blocks", "no"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "g.md" in err and "engine" in err and "amy" in err


def test_cli_track_exits_nonzero_when_create_dependency_returns_none(tmp_path, capsys, monkeypatch):
    sdlc = _project(tmp_path)
    monkeypatch.setattr(handoff.sources, "get_source", lambda *a, **k: ReturnsNoneOnCreate())
    rc = handoff.main(["handoff.py", "track", str(sdlc), "g.md", "--area", "engine",
                       "--why", "found something mid-goal", "--queue", "actionable",
                       "--assignee", "cross-area", "--blocks", "yes"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "g.md" in err and "engine" in err


def test_cli_track_still_exits_zero_when_the_source_cannot_open_issues_at_all(tmp_path, monkeypatch):
    sdlc = _project(tmp_path)

    class Local:
        pass

    monkeypatch.setattr(handoff.sources, "get_source", lambda *a, **k: Local())
    assert handoff.main(["handoff.py", "track", str(sdlc), "g.md", "--area", "engine",
                         "--why", "x", "--queue", "queued", "--assignee", "same-area",
                         "--blocks", "no"]) == 0


# ------------------------------------------------------------------ ack


def test_ack_records_the_state(tmp_path):
    sdlc = _project(tmp_path)
    entry = handoff.acknowledge(sdlc, ON, "61", "accepted", "picking it up after the current slice")
    assert entry["kind"] == "ack" and entry["issue"] == 61 and entry["state"] == "accepted"


def test_deferred_ack_does_not_settle_the_handoff(tmp_path):
    sdlc = _project(tmp_path)
    handoff.hand_off(sdlc, ON, "g.md", "engine", "why", source=FakeSource())
    handoff.acknowledge(sdlc, ON, "61", "deferred", "next week")
    assert len(ledger.outstanding(ledger.read_all(sdlc))) == 1
    handoff.acknowledge(sdlc, ON, "61", "resolved", "shipped")
    assert ledger.outstanding(ledger.read_all(sdlc)) == []


# ------------------------------------------------------------------ #533: --area on ack


def test_acknowledge_writes_the_area_when_given(tmp_path):
    sdlc = _project(tmp_path)
    entry = handoff.acknowledge(sdlc, ON, None, "resolved", "done", goal="g.md", area="engine")
    assert entry["area"] == "engine"


def test_acknowledge_omits_area_when_not_given(tmp_path):
    sdlc = _project(tmp_path)
    entry = handoff.acknowledge(sdlc, ON, "61", "accepted", "picking it up")
    assert "area" not in entry


# ------------------------------------------------------------------ CLI


def test_cli_open_requires_area_and_why(tmp_path, capsys):
    sdlc = _project(tmp_path)
    assert handoff.main(["handoff.py", "open", str(sdlc), "g.md"]) == 2
    assert "--area" in capsys.readouterr().err


def test_cli_open_reports_what_it_did(tmp_path, capsys, monkeypatch):
    sdlc = _project(tmp_path)
    monkeypatch.setattr(handoff.sources, "get_source", lambda *a, **k: FakeSource())
    assert handoff.main(["handoff.py", "open", str(sdlc), "g.md", "--area", "engine",
                         "--why", "needs a flag", "--priority", "P0"]) == 0
    out = capsys.readouterr().out
    assert "eng-owner" in out and "#61" in out and f"ledger amy:{ledger._instance_token()}:1" in out


def test_cli_open_preserves_a_why_value_that_starts_with_a_double_dash(tmp_path, monkeypatch):
    """#541 end-to-end: the issue's own repro through the real CLI path -- a hand-off's `--why` text
    that itself starts with '--' used to be silently replaced by the literal string "true" (ledger's
    shared `_flags()` parser could not tell "no value was given" from "the value looks flag-shaped").
    The reason now survives intact.

    `--title` rides the SAME assertion on purpose. `ledger._flags` is not only ledger's own parser:
    handoff's open/track/ack all call it, so the fix has to cover handoff's vocabulary too, not just
    the names ledger.append() happens to know. Before that, `--title` landed on the "true" sentinel
    and this hand-off filed a real GitHub issue literally TITLED "true" -- the flag most visible to
    a human, silently destroyed."""
    sdlc = _project(tmp_path)
    src = FakeSource()
    monkeypatch.setattr(handoff.sources, "get_source", lambda *a, **k: src)
    assert handoff.main(["handoff.py", "open", str(sdlc), "g.md", "--area", "engine",
                         "--title", "--verbose flag is missing from the CLI",
                         "--why", "--the CLI is missing a --verbose flag"]) == 0
    entry = ledger.read_all(sdlc)[-1]
    assert entry["why"] == "--the CLI is missing a --verbose flag"
    assert src.created["title"] == "--verbose flag is missing from the CLI"


def test_cli_track_requires_the_three_value_flags(tmp_path, capsys):
    """#462: --queue/--assignee/--blocks are REQUIRED value flags, never a bare boolean and never a
    default -- missing any one, or a bare `--flag` with no value (which _flags() would read as the
    string "true", not a valid enum member), is the same hard usage error as a misspelled value."""
    sdlc = _project(tmp_path)
    # missing all three
    assert handoff.main(["handoff.py", "track", str(sdlc), "g.md", "--area", "engine",
                         "--why", "found something mid-goal"]) == 2
    err = capsys.readouterr().err
    assert "--queue" in err and "--assignee" in err and "--blocks" in err

    # a misspelled value on just one of the three still refuses, exit 2, nothing written
    assert handoff.main(["handoff.py", "track", str(sdlc), "g.md", "--area", "engine",
                         "--why", "found something mid-goal", "--queue", "actionable",
                         "--assignee", "same-area", "--blocks", "maybe"]) == 2
    assert ledger.read_all(sdlc) == []

    # a bare --queue with no value is read as the string "true" by _flags() -- not a valid member
    assert handoff.main(["handoff.py", "track", str(sdlc), "g.md", "--area", "engine",
                         "--why", "x", "--queue", "--assignee", "same-area", "--blocks", "yes"]) == 2


def test_cli_track_reports_what_it_did(tmp_path, capsys, monkeypatch):
    """#462: a successful `track` invocation -- also exercises create_tracked_issue's own source=None
    resolution fallback (no explicit source= reaches create_tracked_issue from the CLI dispatcher, so
    this covers the same sources.get_source() call path open's CLI test covers for hand_off)."""
    sdlc = _project(tmp_path)
    monkeypatch.setattr(handoff.sources, "get_source", lambda *a, **k: FakeSource())
    assert handoff.main(["handoff.py", "track", str(sdlc), "g.md", "--area", "engine",
                         "--why", "found something mid-goal", "--queue", "queued",
                         "--assignee", "same-area", "--blocks", "no"]) == 0
    out = capsys.readouterr().out
    assert "amy" in out and "#61" in out and f"ledger amy:{ledger._instance_token()}:1" in out

    entry = ledger.read_all(sdlc)[0]
    assert entry["kind"] == "note" and entry["to"] == "amy"


def test_cli_track_body_file_round_trips_the_file_contents(tmp_path, monkeypatch):
    """#522: `--body-file` reads a file verbatim as the new issue's body -- the one way
    decompose_check's `file` mode (and the meta-goal it files) hands `track` a body longer than a
    CLI arg should carry."""
    sdlc = _project(tmp_path)
    captured = {}

    class CapturingSource(FakeSource):
        def create_dependency(self, title, body, assignee, labels=(), goal_label=True):
            captured["body"] = body
            return super().create_dependency(title, body, assignee, labels=labels, goal_label=goal_label)

    monkeypatch.setattr(handoff.sources, "get_source", lambda *a, **k: CapturingSource())
    body_path = tmp_path / "child-body.md"
    body_path.write_text("<!-- sigma:decomposed-from=#7 -->\nchild body content\nwith multiple lines\n")

    assert handoff.main(["handoff.py", "track", str(sdlc), "7", "--area", "engine",
                         "--why", "child issue", "--queue", "actionable",
                         "--assignee", "same-area", "--blocks", "no",
                         "--body-file", str(body_path)]) == 0
    # triage on (default): the file is kept verbatim and only the depth stamp follows it
    assert captured["body"].startswith(body_path.read_text().rstrip("\n"))
    assert captured["body"].rstrip().endswith("sigma-depth: 1")


def test_cli_track_body_file_missing_file_refuses_before_creating_anything(tmp_path, capsys, monkeypatch):
    """Read BEFORE any create, per the CLI convention (stderr + exit 2, precedent
    tests/test_handoff.py::test_cli_track_requires_the_three_value_flags) -- a missing --body-file
    must never half-file an issue with an empty/wrong body."""
    sdlc = _project(tmp_path)

    def boom(*a, **k):
        raise AssertionError("must never resolve a backlog source before the body file is read")

    monkeypatch.setattr(handoff.sources, "get_source", boom)
    missing = tmp_path / "does-not-exist.md"

    rc = handoff.main(["handoff.py", "track", str(sdlc), "g.md", "--area", "engine",
                       "--why", "x", "--queue", "actionable", "--assignee", "same-area",
                       "--blocks", "no", "--body-file", str(missing)])

    assert rc == 2
    assert "body-file" in capsys.readouterr().err.lower()
    assert ledger.read_all(sdlc) == []


def test_cli_track_body_file_non_utf8_refuses_before_creating_anything(tmp_path, capsys, monkeypatch):
    """#522 review fix 7: a file that fails to DECODE as UTF-8 must refuse the same way a missing
    file does (stderr + exit 2, nothing created) -- before this fix, `read_text(encoding="utf-8")`
    raising `UnicodeDecodeError` (not an `OSError` subclass) was uncaught, so a binary/wrongly-
    encoded --body-file crashed `track` with a raw traceback instead of a usable refusal."""
    sdlc = _project(tmp_path)

    def boom(*a, **k):
        raise AssertionError("must never resolve a backlog source before the body file is read")

    monkeypatch.setattr(handoff.sources, "get_source", boom)
    bad_file = tmp_path / "not-utf8.md"
    bad_file.write_bytes(b"\xff\xfe not valid utf-8 \x80\x81")

    rc = handoff.main(["handoff.py", "track", str(sdlc), "g.md", "--area", "engine",
                       "--why", "x", "--queue", "actionable", "--assignee", "same-area",
                       "--blocks", "no", "--body-file", str(bad_file)])

    assert rc == 2
    assert "body-file" in capsys.readouterr().err.lower()
    assert ledger.read_all(sdlc) == []


def test_cli_track_usage_strings_mention_body_file(capsys):
    sdlc_missing_area = "irrelevant"
    assert handoff.main(["handoff.py", "track", sdlc_missing_area, "g.md"]) == 2
    assert "--body-file" in capsys.readouterr().err
    assert handoff.main(["handoff.py"]) == 2
    assert "--body-file" in capsys.readouterr().err


def test_cli_ack_validates_the_state(tmp_path, capsys):
    sdlc = _project(tmp_path)
    assert handoff.main(["handoff.py", "ack", str(sdlc), "--issue", "61", "--state", "maybe"]) == 2
    assert "--state" in capsys.readouterr().err
    assert handoff.main(["handoff.py", "ack", str(sdlc), "--issue", "61", "--state", "declined"]) == 0
    assert capsys.readouterr().out.strip() == f"amy:{ledger._instance_token()}:1"


def test_cli_ack_requires_issue_or_goal(tmp_path, capsys):
    """F22/#347: before the fix, `--issue` was unconditionally required, so a local/issue-less
    hand-off had no valid invocation at all. Now either identifier is accepted, but at least one
    still must be given -- the error message must say so, not just complain about --issue."""
    sdlc = _project(tmp_path)
    assert handoff.main(["handoff.py", "ack", str(sdlc), "--state", "accepted"]) == 2
    err = capsys.readouterr().err
    assert "--issue" in err and "--goal" in err


def test_cli_ack_by_goal_settles_an_issueless_handoff(tmp_path, capsys):
    """F22/#347: a source that cannot open issues (no `gh`, or a local backlog) leaves the
    hand-off's `issue` field `None`, so `ledger.handoff_key()` keys it by `goal` instead. The loop
    only ever calls through `handoff.py ack` as a subprocess, so the fix has to be reachable from
    argv, not just from a direct Python call to acknowledge() -- this drives it exactly the way a
    real caller would and asserts the hand-off actually settles."""
    sdlc = _project(tmp_path)

    class Local:
        pass                                      # no create_dependency -- mirrors a local backlog

    report = handoff.hand_off(sdlc, ON, "g.md", "engine", "needs a flag", source=Local())
    assert report["issue"] is None                                  # confirms the issue-less setup
    assert len(ledger.outstanding(ledger.read_all(sdlc))) == 1       # open, and nothing can key it yet

    assert handoff.main(["handoff.py", "ack", str(sdlc), "--goal", "g.md",
                         "--state", "resolved", "--why", "handled locally"]) == 0
    assert capsys.readouterr().out.strip() == f"amy:{ledger._instance_token()}:2"     # :2 -- the handoff was :1
    assert ledger.outstanding(ledger.read_all(sdlc)) == []                # settled by goal alone


def test_cli_ack_by_goal_and_area_settles_only_that_areas_handoff(tmp_path, capsys):
    """#533 CLI twin of the ledger-level settlement test: two issue-less hand-offs on one goal to
    different areas, ack --area engine leaves exactly the ui one outstanding. Driven through argv
    like a real caller, not a direct acknowledge() call, so this proves the flag is actually WIRED
    end to end -- red-first on HEAD: --area is parsed by _flags() and silently dropped, so this
    settles BOTH instead of one."""
    sdlc = _project(tmp_path)

    class Local:
        pass                                      # no create_dependency -- mirrors a local backlog

    handoff.hand_off(sdlc, ON, "g.md", "engine", "needs a flag", source=Local())
    handoff.hand_off(sdlc, ON, "g.md", "ui", "needs a review", source=Local())
    assert len(ledger.outstanding(ledger.read_all(sdlc))) == 2

    assert handoff.main(["handoff.py", "ack", str(sdlc), "--goal", "g.md", "--area", "engine",
                         "--state", "resolved"]) == 0
    remaining = ledger.outstanding(ledger.read_all(sdlc))
    assert [h["area"] for h in remaining] == ["ui"]


def test_cli_ack_unmatched_area_still_writes_and_warns(tmp_path, capsys):
    """#533 amendment: ack validation NEVER refuses -- a typo'd --area still writes the ack (exit 0,
    stdout stays the bare entry id, matching this file's existing dangling-ack-writes-freely
    precedent at test_cli_ack_validates_the_state), just warns on stderr naming the live areas."""
    sdlc = _project(tmp_path)

    class Local:
        pass

    handoff.hand_off(sdlc, ON, "g.md", "engine", "needs a flag", source=Local())
    rc = handoff.main(["handoff.py", "ack", str(sdlc), "--goal", "g.md", "--area", "typo-area",
                       "--state", "resolved"])
    out, err = capsys.readouterr()
    assert rc == 0
    assert out.strip() == f"amy:{ledger._instance_token()}:2"
    assert "matched no outstanding hand-off" in err and "engine" in err
    entry = ledger.read_all(sdlc)[-1]
    assert entry["area"] == "typo-area"          # written anyway -- never refused


def test_cli_ack_area_less_on_a_multi_area_goal_warns_it_settled_more_than_one(tmp_path, capsys):
    """The other #533 warning shape: an area-LESS ack on a goal with more than one live area still
    settles all of them (the deliberate backward-compat fallback), but warns so the caller notices
    it was not as targeted as they may have intended."""
    sdlc = _project(tmp_path)

    class Local:
        pass

    handoff.hand_off(sdlc, ON, "g.md", "engine", "x", source=Local())
    handoff.hand_off(sdlc, ON, "g.md", "ui", "y", source=Local())
    rc = handoff.main(["handoff.py", "ack", str(sdlc), "--goal", "g.md", "--state", "resolved"])
    err = capsys.readouterr().err
    assert rc == 0
    assert "settled 2 hand-offs" in err and "--area" in err
    assert ledger.outstanding(ledger.read_all(sdlc)) == []       # still settles both -- just warns


def test_cli_ack_by_goal_warns_it_cannot_settle_an_issue_bearing_local_handoff(tmp_path, capsys):
    """#770: a `LocalSource` (no `discovery.source` in config) mints a REAL local integer issue id in
    `create_dependency()`, so the hand-off's ledger entry is issue-BEARING and `settlement_key()`
    keys it by `str(issue)`. `ack --goal` writes `issue=None`, whose key falls back to the
    `(goal, area)` tuple and can NEVER match — so the hand-off silently never settles, exit 0, empty
    stderr. The issue-LESS warnings above never look at it (they filter `not h.get("issue")`). Driven
    through argv like a real caller, so it proves the widened warn-path is WIRED end to end — red-first
    on HEAD: stderr is empty and the still-outstanding issue-bearing hand-off is never flagged."""
    import re

    sdlc = _project(tmp_path)
    sources = _mod("sources")
    (sdlc / "goals").mkdir()
    gpath = sdlc / "goals" / "0007-blocked.md"
    gpath.write_text('---\nid: 0007\ntitle: "blocked"\nstatus: pending\n---\n\nbody\n')

    local = sources.LocalSource(str(sdlc), ON)
    report = handoff.hand_off(sdlc, ON, str(gpath), "engine", "needs a flag", source=local)
    assert str(report["issue"]).isdigit()          # issue-BEARING, unlike the Local()-stub tests above
    assert len(ledger.outstanding(ledger.read_all(sdlc))) == 1

    rc = handoff.main(["handoff.py", "ack", str(sdlc), "--goal", str(gpath),
                       "--state", "resolved", "--why", "handled"])
    out, err = capsys.readouterr()
    assert rc == 0                                          # never refuses -- the ack still writes
    assert out.strip() == f"amy:{ledger._instance_token()}:2"   # :2 -- the handoff row was :1
    assert re.search(rf"#{report['issue']}\b", err)        # names the exact issue, word-bounded (#770 trap)
    assert "--issue" in err                                # points the caller at the settleable path


def test_cli_ack_usage_mentions_area(capsys):
    assert handoff.main(["handoff.py", "ack", "irrelevant"]) == 2
    assert "--area" in capsys.readouterr().err


def test_cli_usage(capsys):
    assert handoff.main(["handoff.py"]) == 2
    assert "usage: handoff.py" in capsys.readouterr().err


# ------------------------------------------------------------------ GitHubSource wiring


def _github_source(recorder):
    sources = _mod("sources")
    return sources.GitHubSource(
        {"discovery": {"github": {"repo": "acme/widget", "project": {"enabled": False}}}},
        run=recorder)


def _rest_recorder(fail=None, create_number=61, missing=(), assignable=None, me="octocat"):
    """A `gh` runner answering the REST issue writes via the `gqlfake.rest_write` shim (#895 slice 3a).
    `calls`: legacy-shaped `issue ...` records plus any non-REST argv (`label create`); `rest`: raw
    REST write argvs; `fail(args)` -> an exception to raise for that argv, else None."""
    import gqlfake
    calls, rest = [], []

    def recorder(args):
        exc = fail(args) if fail else None
        if exc is not None:
            raise exc
        if gqlfake.is_issue_write(args):
            rest.append(list(args))
            return gqlfake.rest_write(args, calls=calls, repo_args=("--repo", "acme/widget"),
                                      create_number=create_number, missing_labels=missing,
                                      assignable=assignable, me=me)
        calls.append(list(args))
        return ""
    recorder.calls, recorder.rest = calls, rest
    return recorder


def _err(text, status=None):
    exc = RuntimeError(text)
    exc.hint = text if status is None else "%s (HTTP %d)" % (text, status)
    return exc


def test_create_dependency_sends_assignee_goal_label_and_extras():
    rec = _rest_recorder()
    number = _github_source(rec).create_dependency(
        "[engine] dependency", "body", "eng-owner", labels=["sdlc:dependency", "priority:P0"])
    assert number == "61"
    assert rec.rest[0][:4] == ["api", "repos/acme/widget/issues", "--method", "POST"]      # REST create
    create = next(c for c in rec.calls if c[:2] == ["issue", "create"])
    assert "--assignee" not in create                        # create is always unassigned (F14 round 2)
    assert create.count("--label") == 3                      # goal label + the two extras
    assert "sdlc:goal" in create and "priority:P0" in create
    edit = next(c for c in rec.calls if c[:2] == ["issue", "edit"])
    assert edit[2] == "61" and "--add-assignee" in edit
    assert edit[edit.index("--add-assignee") + 1] == "eng-owner"
    # the label pre-step precedes the REST create (D3): REST never meets an absent non-feature label
    first_create = next(i for i, c in enumerate(rec.calls) if c[:2] == ["issue", "create"])
    assert [c[:2] for c in rec.calls[:first_create]].count(["label", "create"]) >= 2


def test_create_dependency_number_is_the_rest_number_and_none_when_absent_or_not_an_int():
    src = _mod("sources")
    gh = _github_source(lambda a: "")
    for resp, want in (({"number": 61}, "61"), ({}, None), ({"number": "61"}, None),
                       ({"number": True}, None), ("https://github.com/acme/widget/issues/9\n", "9"),
                       ("", None)):
        gh._issue_create = lambda t, b, l, _r=resp: _r
        assert gh._create_issue("t", "b", []) == want, resp


def test_create_dependency_returns_none_when_gh_says_nothing():
    assert _github_source(lambda args: "{}").create_dependency("t", "b", "who") is None


def test_create_dependency_tolerates_a_label_that_cannot_be_created():
    """The `label create` pre-step failing (swallowed) lets REST mint the NON-feature label: the create
    still goes through and returns the REST number (D3, accepted divergence; UNMEASURED on a live repo)."""
    rec = _rest_recorder(create_number=7,
                         fail=lambda a: _err("insufficient scope") if a[:2] == ["label", "create"] else None)
    assert _github_source(rec).create_dependency("t", "b", "who", labels=["x"]) == "7"
    assert any(c[:2] == ["issue", "create"] and "x" in c for c in rec.calls)


def test_feature_label_create_refused_without_rest_write():
    """Layer 2: a `feature:*` label that does not exist is refused BEFORE the REST create -- the only
    call made is the existence GET (a read); and a feature label that DOES exist is attached."""
    rec = _rest_recorder(missing={"feature:voice"})
    with pytest.raises(RuntimeError, match="refusing to create the label"):
        _github_source(rec).create_dependency("t", "b", None, labels=["feature:voice"])
    assert not any(c[0:2] == ["api", "repos/acme/widget/issues"] for c in rec.rest)
    assert rec.rest == [] or all("--method" in c and c[c.index("--method") + 1] == "GET" for c in rec.rest) or \
        not any(c[c.index("--method") + 1] in ("POST", "PATCH", "DELETE") for c in rec.rest)
    ok = _rest_recorder(create_number=5)
    assert _github_source(ok).create_dependency("t", "b", None, labels=["feature:voice"]) == "5"


def test_create_dependency_5xx_makes_exactly_one_create_call():
    """A 5xx / timeout on the create is ambiguous (it may have committed): NO gh fallback, NO retry --
    exactly one create call, and the error raises."""
    for status in (502, 503):
        rec = _rest_recorder(fail=lambda a, _s=status: _err("gh: Server Error", _s)
                             if a[:2] == ["api", "repos/acme/widget/issues"] and a[3] == "POST" else None)
        with pytest.raises(Exception, match="Server Error"):
            _github_source(rec).create_dependency("t", "b", "who")
        assert not any(c[:2] == ["issue", "create"] for c in rec.calls)      # no gh fallback
    attempts = []

    def rec2(a):
        if a[:2] == ["api", "repos/acme/widget/issues"] and a[3] == "POST":
            attempts.append(list(a))
            raise _err("gh: Server Error", 502)
        return ""

    with pytest.raises(Exception):
        _github_source(rec2).create_dependency("t", "b", "who")
    assert len(attempts) == 1                                                  # not retried


# ------------------------------------------------------------- F14/#338: rejected-assignee fallback

def test_create_dependency_never_creates_a_duplicate_issue_when_the_assignee_is_rejected():
    """Round 2 of independent review found the more serious bug: `gh issue create --assignee` is NOT
    atomic -- it runs `createIssue` then a SEPARATE `replaceActorsForAssignable` mutation, and when
    only the second one fails, the issue it already created is not rolled back, and its number is
    never printed to stdout. A combined call has no way to learn that orphan exists -- confirmed
    against real `gh`, not assumed -- so retrying unassigned on that failure (round 1 of this fix)
    created a SECOND, genuinely duplicate, permanently untracked issue every time. create_dependency
    now issues exactly ONE `issue create` call ever, always unassigned, and assigns as a separate
    step against the now-known issue number -- there is structurally no way for two issues to exist,
    and a rejected assignee just leaves the one issue unassigned with an explanatory comment."""
    rec = _rest_recorder()
    src = _github_source(rec)
    number = src.create_dependency("t", "b", "org/eng-team")     # a team slug: refused before any call
    assert number == "61"
    assert src.last_assignee_applied is False
    creates = [c for c in rec.calls if c[:2] == ["issue", "create"]]
    assert len(creates) == 1, f"expected exactly one issue ever created, got {len(creates)}: {creates}"
    assert "--assignee" not in creates[0]                       # never combined with create
    assert not any(c[c.index("--method") + 1] == "POST" and c[1].endswith("/assignees") for c in rec.rest)
    comment = next(c for c in rec.calls if c[:2] == ["issue", "comment"])
    assert comment[2] == "61" and "org/eng-team" in " ".join(comment)


def test_a_dropped_assignee_leaves_the_issue_unassigned_with_the_real_reason_in_the_comment():
    """REST silently ignores a non-assignable login; `add_assignees` verifies the response, raises, and
    create_dependency posts the 'Could not assign' note -- no duplicate issue, flag False."""
    rec = _rest_recorder(assignable={"someone-else"})
    src = _github_source(rec)
    assert src.create_dependency("t", "b", "ghost") == "61"
    assert src.last_assignee_applied is False
    note = next(c for c in rec.calls if c[:2] == ["issue", "comment"])[-1]
    assert "Could not assign @ghost" in note and len([c for c in rec.calls if c[:2] == ["issue", "create"]]) == 1


def test_get_user_403_for_me_leaves_unassigned_posts_the_403_and_never_posts_assignees():
    """D4 `@me`: the `GET user` lookup is permission-denied (Actions GITHUB_TOKEN) -> nothing is sent,
    no gh fallback (permission 403 never falls back), the comment names the real 403."""
    rec = _rest_recorder(fail=lambda a: _err("gh: Resource not accessible by integration", 403)
                         if a[:2] == ["api", "user"] else None)
    src = _github_source(rec)
    assert src.create_dependency("t", "b", "@me") == "61"
    assert src.last_assignee_applied is False
    assert not any(c[1].endswith("/assignees") for c in rec.rest)
    assert not any(c[:2] == ["issue", "edit"] for c in rec.calls)               # no gh fallback
    note = next(c for c in rec.calls if c[:2] == ["issue", "comment"])[-1]
    assert "403" in note


def test_create_dependency_with_no_owner_never_posts_an_assignment_note():
    """No assignee was ever attempted, so there is nothing to apologise for -- the note is specific
    to a REJECTED assignee, not a general "how did this issue get made" disclosure."""
    rec = _rest_recorder()
    src = _github_source(rec)
    assert src.create_dependency("t", "b", None) == "61"
    assert src.last_assignee_applied is False
    assert not any(c[:2] == ["issue", "comment"] for c in rec.calls)


def test_create_dependency_still_raises_when_issue_create_itself_fails():
    """A genuine, non-assignee-specific failure (auth broken, network down, ...) on the ONE `issue
    create` call must still surface -- create_dependency never wraps that call in its own
    try/except, so this has always been the behavior; pinned explicitly so a future change to the
    assignment step can't accidentally start swallowing it too."""
    rec = _rest_recorder(fail=lambda a: _err("gh: not authenticated", 401)
                         if a[:2] == ["api", "repos/acme/widget/issues"] and a[3] == "POST" else None)
    with pytest.raises(Exception, match="not authenticated"):
        _github_source(rec).create_dependency("t", "b", "eng-owner")


def test_create_dependency_note_uses_the_short_hint_not_the_whole_failed_command():
    """Independent review, round 1: the note used str(exc) wholesale. For a REAL gh failure (via
    _run_gh, not a test double's clean one-liner), str(exc) is the entire reconstructed command line
    -- 'gh issue edit 61 --repo ... --add-assignee org/eng-team failed: <hint>' -- not just the
    reason. _run_gh now attaches the short reason alone as exc.hint; the note must use that, not
    str(exc), whenever it's available."""
    def failing(args):
        if args[1].endswith("/assignees"):
            hint = "could not add assignees to issue: 'eng-team' is not an assignable user"
            exc = RuntimeError("gh " + " ".join(args) + " failed: " + hint)
            exc.hint = hint
            return exc
        return None

    rec = _rest_recorder(fail=lambda a: failing(a) if a[:1] == ["api"] and len(a) > 1 else None)
    number = _github_source(rec).create_dependency("[engine] dep", "body", "eng-team")
    assert number == "61"
    comment = next(c for c in rec.calls if c[:2] == ["issue", "comment"])
    note = comment[-1]
    assert "is not an assignable user" in note                  # the real, short reason survives
    assert "--repo" not in note and "issue edit" not in note    # the reconstructed command does not


def test_run_gh_attaches_the_short_hint_separately_from_the_full_message(monkeypatch):
    """The other half: _run_gh itself must actually set .hint, not just be assumed to."""
    src = _mod("sources")
    import subprocess

    def fake_run(cmd, capture_output, text):
        class P:
            returncode = 1
            stderr = "  could not add assignees to issue: 'x' is not an assignable user  \n"
        return P()

    monkeypatch.setattr(subprocess, "run", fake_run)
    try:
        src._run_gh(["issue", "create", "--body", "a very long body " * 20, "--assignee", "x"])
        assert False, "expected a RuntimeError"
    except RuntimeError as exc:
        assert exc.hint == "could not add assignees to issue: 'x' is not an assignable user"
        assert "a very long body" in str(exc)            # the full message is unchanged, still useful
        assert "a very long body" not in exc.hint         # but .hint alone stays short


# ------------------------------------------------------------------ #522: docs mention --body-file

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_readme_documents_track_body_file():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "--body-file" in readme


def test_skill_documents_track_body_file():
    skill = skill_corpus("sigma-loop")   # #1611: SKILL.md + references/*.md
    assert "--body-file" in skill


# ------------------------------------------------------------------ #533: agent-facing docs mention --area


def test_readme_documents_ack_area():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "--goal <goal> --area <area>" in readme


def test_loop_skill_documents_ack_area():
    skill = skill_corpus("sigma-loop")   # #1611: SKILL.md + references/*.md
    assert "--goal <goal> --area <area>" in skill


def test_ledger_skill_documents_ack_area():
    skill = (ROOT / "skills" / "sigma-ledger" / "SKILL.md").read_text(encoding="utf-8")
    assert "--goal <goal> --area <area>" in skill


# --- #623: a follow-up filed on a nightly run has to be ENQUEUE-READY ---------------------------
# `create_tracked_issue` already assigns and already labels priority:/area:/sdlc:goal. What was
# missing is the ability to say WHICH KIND of follow-up and AT WHICH TIER in one filing: `--label`
# took a single value, so `--label sdlc:followup,model:bulk` filed one literal label "a,b" and the
# issue landed without a usable model tier. The split lives in the `track` dispatch, NOT in
# `ledger._flags` — that parser is shared by every verb and pinned since #541; widening it would
# change how EVERY flag in the plugin parses a comma.

def test_cli_track_label_accepts_a_comma_separated_list(tmp_path, monkeypatch):
    """The nightly-run shape: class label AND model tier on one filing, so the goal the loop picks
    up overnight already knows what it is and what to run it on."""
    sdlc = _project(tmp_path)
    src = FakeSource()
    monkeypatch.setattr(handoff.sources, "get_source", lambda *a, **k: src)
    assert handoff.main(["handoff.py", "track", str(sdlc), "g.md", "--area", "engine",
                         "--why", "found something mid-goal", "--queue", "actionable",
                         "--assignee", "same-area", "--blocks", "no",
                         "--label", "sdlc:followup,model:bulk"]) == 0
    labels = src.created["labels"]
    assert "sdlc:followup" in labels and "model:bulk" in labels
    assert "sdlc:followup,model:bulk" not in labels      # never the joined string


def test_cli_track_label_accepts_a_comma_separated_list_via_local_source(tmp_path, monkeypatch):
    """#688 finding 4: every comma-split test above monkeypatches `FakeSource` (GitHub-shaped, see
    its own docstring "Stands in for GitHubSource"), and every `LocalSource.create_dependency` test
    in test_sources.py calls it directly with an already-split tuple -- bypassing this CLI
    dispatch's own comma-split logic entirely. Nothing exercised CLI-track + LocalSource +
    comma-separated --label together, the path a real local (non-github) project actually takes.
    Uses a REAL `sources.LocalSource` over the same `_project(tmp_path)` helper every other test in
    this file already shares, and asserts on the WRITTEN GOAL FILE's own `Labels:` trailer line --
    `LocalSource.create_dependency`'s documented mechanism for labels, since local mode has no label
    index to route them through otherwise (see that method's own docstring in sources.py)."""
    sdlc = _project(tmp_path)
    src = sources.LocalSource(str(sdlc))
    monkeypatch.setattr(handoff.sources, "get_source", lambda *a, **k: src)
    assert handoff.main(["handoff.py", "track", str(sdlc), "g.md", "--area", "engine",
                         "--why", "found something mid-goal", "--queue", "actionable",
                         "--assignee", "same-area", "--blocks", "no",
                         "--label", "sdlc:followup,model:bulk"]) == 0
    goal_files = list((sdlc / "goals").glob("*.md"))
    assert len(goal_files) == 1
    text = goal_files[0].read_text(encoding="utf-8")
    labels_lines = [l for l in text.splitlines() if l.startswith("Labels: ")]
    assert len(labels_lines) == 1
    labels_line = labels_lines[0]
    assert "sdlc:followup" in labels_line and "model:bulk" in labels_line
    assert "sdlc:followup,model:bulk" not in labels_line      # never the joined string


def test_cli_track_label_still_takes_a_single_value_and_trims_whitespace(tmp_path, monkeypatch):
    """One label is the same call it always was; a spaced list is the way a human actually types it,
    and an empty element (a trailing comma) must not become a nameless label."""
    sdlc = _project(tmp_path)
    src = FakeSource()
    monkeypatch.setattr(handoff.sources, "get_source", lambda *a, **k: src)
    assert handoff.main(["handoff.py", "track", str(sdlc), "g.md", "--area", "engine",
                         "--why", "w", "--queue", "queued", "--assignee", "same-area",
                         "--blocks", "no", "--label", "sdlc:followup, model:bulk,"]) == 0
    labels = src.created["labels"]
    assert "sdlc:followup" in labels and "model:bulk" in labels
    assert "" not in labels and all(l == l.strip() for l in labels)


def test_the_shared_flag_parser_is_left_alone_by_the_comma_split(tmp_path):
    """The split is the `track` dispatch's own business. `ledger._flags` is shared by every verb in
    the plugin and pinned since #541 — teaching IT about commas would silently change how every
    other flag parses one."""
    assert ledger._flags(["--label", "a,b"]) == {"label": "a,b"}


# --- #1820: `--target-unit NAME` on both `track` and `open` ------------------------------------

def _open_unit(**over):
    base = {"title": "t", "owner": None, "open": True, "parent": None, "tracking_issue": None,
            "repos": {}}
    base.update(over)
    return base


def test_cli_track_target_unit_stamps_the_named_open_unit(tmp_path, monkeypatch):
    sdlc = _project(tmp_path)
    feature_registry.write_index(feature_registry.registry_dir(sdlc), {"billing": _open_unit()})
    src = FakeSource()
    monkeypatch.setattr(handoff.sources, "get_source", lambda *a, **k: src)
    assert handoff.main(["handoff.py", "track", str(sdlc), "g.md", "--area", "engine",
                         "--why", "found something mid-goal", "--queue", "actionable",
                         "--assignee", "same-area", "--blocks", "no",
                         "--target-unit", "billing"]) == 0
    assert "Feature: billing" in src.created["body"]
    assert "Branch: feature/billing" in src.created["body"]


def test_cli_track_target_unit_naming_an_unknown_unit_files_without_it_and_warns(tmp_path, monkeypatch,
                                                                                 capsys):
    sdlc = _project(tmp_path)
    src = FakeSource()
    monkeypatch.setattr(handoff.sources, "get_source", lambda *a, **k: src)
    assert handoff.main(["handoff.py", "track", str(sdlc), "g.md", "--area", "engine",
                         "--why", "w", "--queue", "queued", "--assignee", "same-area",
                         "--blocks", "no", "--target-unit", "no-such-unit"]) == 0
    assert "Feature:" not in src.created["body"]
    err = capsys.readouterr().err
    assert "no-such-unit" in err and "OPEN" in err


def test_cli_track_target_unit_naming_a_closed_unit_is_refused(tmp_path, monkeypatch, capsys):
    sdlc = _project(tmp_path)
    feature_registry.write_index(feature_registry.registry_dir(sdlc),
                                 {"legacy-import": _open_unit(open=False)})
    src = FakeSource()
    monkeypatch.setattr(handoff.sources, "get_source", lambda *a, **k: src)
    assert handoff.main(["handoff.py", "track", str(sdlc), "g.md", "--area", "engine",
                         "--why", "w", "--queue", "queued", "--assignee", "same-area",
                         "--blocks", "no", "--target-unit", "legacy-import"]) == 0
    assert "Feature: legacy-import" not in src.created["body"]
    assert not any(str(l).lower() == "feature:legacy-import" for l in src.created["labels"])
    assert "legacy-import" in capsys.readouterr().err


def test_cli_track_omitting_target_unit_is_unaffected(tmp_path, monkeypatch):
    """Parity: `--target-unit` is optional, and every EXISTING `track` call (none of which pass it)
    must behave exactly as it always has."""
    sdlc = _project(tmp_path)
    src = FakeSource()
    monkeypatch.setattr(handoff.sources, "get_source", lambda *a, **k: src)
    assert handoff.main(["handoff.py", "track", str(sdlc), "g.md", "--area", "engine",
                         "--why", "w", "--queue", "queued", "--assignee", "same-area",
                         "--blocks", "no"]) == 0
    assert "Feature:" not in src.created["body"]


def test_cli_open_target_unit_stamps_the_named_open_unit(tmp_path, monkeypatch):
    sdlc = _project(tmp_path)
    feature_registry.write_index(feature_registry.registry_dir(sdlc), {"billing": _open_unit()})
    src = FakeSource()
    monkeypatch.setattr(handoff.sources, "get_source", lambda *a, **k: src)
    rc = handoff.main(["handoff.py", "open", str(sdlc), "g.md", "--area", "engine",
                       "--why", "needs a flag first", "--target-unit", "billing"])
    assert rc == 0
    assert "Feature: billing" in src.created["body"]
    assert "Branch: feature/billing" in src.created["body"]


# --- #2363: automatic classification when no explicit --target-unit was given -----------------
#
# `_auto_classify_unit` is gated on the SAME two attributes `no_dangling_goal_enabled`/
# `no_dangling_goal_core` the pick-time chain reads (`feature_labels._handle_no_unit_at_pick`),
# plus the registry being adopted -- `FakeSource` (used by every other test in this file) has
# neither attribute, so those tests stay byte-identical without any change here. The classifier's
# own 4-tier chain is tested in `test_feature_classify.py`; these tests pin only the WIRING: that
# an omitted `--target-unit` reaches `feature_classify`, and that a given one never does.

class _ClassifyingSource(FakeSource):
    no_dangling_goal_enabled = True
    no_dangling_goal_core = "core"


def test_cli_track_auto_classifies_under_the_catch_all_when_nothing_was_targeted(tmp_path, monkeypatch):
    sdlc = _project(tmp_path)
    feature_registry.write_index(feature_registry.registry_dir(sdlc), {"core": _open_unit()})
    src = _ClassifyingSource()
    monkeypatch.setattr(handoff.sources, "get_source", lambda *a, **k: src)
    assert handoff.main(["handoff.py", "track", str(sdlc), "g.md", "--area", "engine",
                         "--why", "found something mid-goal", "--queue", "actionable",
                         "--assignee", "same-area", "--blocks", "no"]) == 0
    assert "Feature: core" in src.created["body"]
    assert "Branch: feature/core" in src.created["body"]


def test_cli_open_auto_classifies_under_the_catch_all_when_nothing_was_targeted(tmp_path, monkeypatch):
    sdlc = _project(tmp_path)
    feature_registry.write_index(feature_registry.registry_dir(sdlc), {"core": _open_unit()})
    src = _ClassifyingSource()
    monkeypatch.setattr(handoff.sources, "get_source", lambda *a, **k: src)
    assert handoff.main(["handoff.py", "open", str(sdlc), "g.md", "--area", "engine",
                         "--why", "needs a flag first"]) == 0
    assert "Feature: core" in src.created["body"]


def test_cli_track_explicit_target_unit_always_wins_over_classification(tmp_path, monkeypatch):
    """The issue's own requirement: an explicit human --target-unit must ALWAYS win -- classification
    must never even be invoked when one was passed."""
    sdlc = _project(tmp_path)
    feature_registry.write_index(feature_registry.registry_dir(sdlc),
                                 {"core": _open_unit(), "billing": _open_unit()})
    src = _ClassifyingSource()
    monkeypatch.setattr(handoff.sources, "get_source", lambda *a, **k: src)
    assert handoff.main(["handoff.py", "track", str(sdlc), "g.md", "--area", "engine",
                         "--why", "w", "--queue", "queued", "--assignee", "same-area",
                         "--blocks", "no", "--target-unit", "billing"]) == 0
    assert "Feature: billing" in src.created["body"]
    assert "Feature: core" not in src.created["body"]


def test_classification_is_a_no_op_for_a_plain_fake_source(tmp_path, monkeypatch):
    """THE CONTROL: `FakeSource` has no `no_dangling_goal_enabled` at all -- the same source every
    OTHER test in this file already uses -- so this must remain exactly as unaffected as it always
    was."""
    sdlc = _project(tmp_path)
    feature_registry.write_index(feature_registry.registry_dir(sdlc), {"core": _open_unit()})
    src = FakeSource()
    monkeypatch.setattr(handoff.sources, "get_source", lambda *a, **k: src)
    assert handoff.main(["handoff.py", "track", str(sdlc), "g.md", "--area", "engine",
                         "--why", "w", "--queue", "queued", "--assignee", "same-area",
                         "--blocks", "no"]) == 0
    assert "Feature:" not in src.created["body"]


def test_classification_is_a_no_op_with_no_catch_all_configured(tmp_path, monkeypatch):
    class _NoCoreSource(FakeSource):
        no_dangling_goal_enabled = True
        no_dangling_goal_core = None

    sdlc = _project(tmp_path)
    feature_registry.write_index(feature_registry.registry_dir(sdlc), {"core": _open_unit()})
    src = _NoCoreSource()
    monkeypatch.setattr(handoff.sources, "get_source", lambda *a, **k: src)
    assert handoff.main(["handoff.py", "track", str(sdlc), "g.md", "--area", "engine",
                         "--why", "w", "--queue", "queued", "--assignee", "same-area",
                         "--blocks", "no"]) == 0
    assert "Feature:" not in src.created["body"]


def test_classification_is_a_no_op_with_no_adopted_registry(tmp_path, monkeypatch):
    """Same D-7 reasoning as `_handle_no_unit_at_pick`: an unadopted repo (no `.sdlc/features/` at
    all) must never be retroactively attributed, even with the config flags on."""
    sdlc = _project(tmp_path)
    assert not (sdlc / "features").is_dir()
    src = _ClassifyingSource()
    monkeypatch.setattr(handoff.sources, "get_source", lambda *a, **k: src)
    assert handoff.main(["handoff.py", "track", str(sdlc), "g.md", "--area", "engine",
                         "--why", "w", "--queue", "queued", "--assignee", "same-area",
                         "--blocks", "no"]) == 0
    assert "Feature:" not in src.created["body"]


# --- #2380: the live-judge opt-in, wired on top of the #2363 classification gate above ---------
#
# `feature_judge.py`'s own chain is tested in `tests/test_feature_judge.py`. These tests pin only
# the WIRING at THIS call site: `_auto_classify_unit` passes `judge=None` (byte-identical to
# before #2380) when `no_dangling_goal_live_judge_enabled` is off/absent, and
# `judge=feature_judge.live_judge` when it is on.

class _StubAdoptedDir:
    """A stand-in for the real `pathlib.Path` `feature_registry.registry_dir` normally returns --
    only `.is_dir()` is ever called on it by `_auto_classify_unit`, so only that is stubbed,
    rather than monkeypatching `pathlib.Path.is_dir` globally for the whole test."""
    def is_dir(self):
        return True


def test_auto_classify_unit_passes_judge_none_when_live_judge_disabled(monkeypatch):
    captured = {}

    class FakeClassify:
        @staticmethod
        def classify_for_filing(sdlc_dir, source, config, core, judge=None, issue_title=None,
                                 issue_body=None):
            captured["judge"] = judge
            captured["issue_title"] = issue_title
            captured["issue_body"] = issue_body
            return "core"

    class _AdoptedRegistry:
        @staticmethod
        def registry_dir(sdlc_dir):
            return _StubAdoptedDir()

    monkeypatch.setattr(handoff, "_feature_classify", lambda: FakeClassify)
    monkeypatch.setattr(handoff, "_feature_registry", lambda: _AdoptedRegistry)
    src = _ClassifyingSource()                        # no no_dangling_goal_live_judge_enabled at all
    assert src.no_dangling_goal_core == "core"
    result = handoff._auto_classify_unit("sdlc", {}, src)
    assert result == "core"
    assert captured["judge"] is None
    # #2383: omitted at this call site -> both default to `None`, threaded straight through.
    assert captured["issue_title"] is None
    assert captured["issue_body"] is None


def test_auto_classify_unit_threads_issue_title_body_through_to_classify_for_filing(monkeypatch):
    """#2383 (slice H): `_auto_classify_unit`'s own new `issue_title`/`issue_body` params must
    reach `classify_for_filing`'s same-named kwargs verbatim."""
    captured = {}

    class FakeClassify:
        @staticmethod
        def classify_for_filing(sdlc_dir, source, config, core, judge=None, issue_title=None,
                                 issue_body=None):
            captured["issue_title"] = issue_title
            captured["issue_body"] = issue_body
            return "core"

    class _AdoptedRegistry:
        @staticmethod
        def registry_dir(sdlc_dir):
            return _StubAdoptedDir()

    monkeypatch.setattr(handoff, "_feature_classify", lambda: FakeClassify)
    monkeypatch.setattr(handoff, "_feature_registry", lambda: _AdoptedRegistry)
    src = _ClassifyingSource()
    result = handoff._auto_classify_unit("sdlc", {}, src, issue_title="A new bug",
                                          issue_body="Repro steps here")
    assert result == "core"
    assert captured["issue_title"] == "A new bug"
    assert captured["issue_body"] == "Repro steps here"


def test_auto_classify_unit_routes_through_feature_judge_live_judge_when_enabled(monkeypatch):
    captured = {}
    sentinel = object()

    class FakeClassify:
        @staticmethod
        def classify_for_filing(sdlc_dir, source, config, core, judge=None, issue_title=None,
                                 issue_body=None):
            captured["judge"] = judge
            return "core"

    class FakeJudgeModule:
        live_judge = sentinel

    class _EnabledSource(_ClassifyingSource):
        no_dangling_goal_live_judge_enabled = True

    class _AdoptedRegistry:
        @staticmethod
        def registry_dir(sdlc_dir):
            return _StubAdoptedDir()

    monkeypatch.setattr(handoff, "_feature_classify", lambda: FakeClassify)
    monkeypatch.setattr(handoff, "_feature_judge", lambda: FakeJudgeModule)
    monkeypatch.setattr(handoff, "_feature_registry", lambda: _AdoptedRegistry)
    result = handoff._auto_classify_unit("sdlc", {}, _EnabledSource())
    assert result == "core"
    assert captured["judge"] is sentinel


def test_cli_track_with_live_judge_enabled_falls_back_to_the_catch_all_when_unconfirmed(
        tmp_path, monkeypatch):
    """A REAL end-to-end integration test through the ACTUAL `feature_judge.live_judge` --
    `ask_claude` is the ONLY thing mocked (the actual subprocess boundary), per this slice's own
    instructions; `assign`/`validate`/`live_judge` all run for real on top of it.

    #2383 changed WHY this falls back: before this slice, filing time had no content at all
    (`goal=None`, nothing to classify) and abstained unconditionally. Now real `heading`/`text`
    content DOES reach the judge via `_pending_issue` -- so this pins that even with real content
    flowing through, a validator REFUTE still correctly falls back to the configured catch-all
    rather than mis-attributing. The complementary case -- a real classification actually landing
    -- is `test_cli_track_with_live_judge_enabled_fires_a_real_filing_time_classification` below."""
    sdlc = _project(tmp_path)
    feature_registry.write_index(feature_registry.registry_dir(sdlc), {"core": _open_unit()})

    class _LiveJudgeEnabledSource(_ClassifyingSource):
        no_dangling_goal_live_judge_enabled = True
        no_dangling_goal_live_judge_rounds = 3
        no_dangling_goal_live_judge_spend_ceiling_usd_per_day = 5.0

    src = _LiveJudgeEnabledSource()
    monkeypatch.setattr(handoff.sources, "get_source", lambda *a, **k: src)

    real_fj = handoff._feature_judge()                 # loads the REAL feature_judge module

    def fake_ask_claude(prompt, json_schema, model="sonnet", timeout_s=60, max_budget_usd=0.50):
        if "verdict" in json_schema.get("properties", {}):
            return {"verdict": "REFUTE", "reasoning": "not confident"}, 0.02
        return {"tier_guess": "4", "reasoning": "genuinely unclear"}, 0.02

    monkeypatch.setattr(real_fj, "ask_claude", fake_ask_claude)

    assert handoff.main(["handoff.py", "track", str(sdlc), "g.md", "--area", "engine",
                         "--why", "found something mid-goal", "--queue", "actionable",
                         "--assignee", "same-area", "--blocks", "no"]) == 0
    assert "Feature: core" in src.created["body"]


def test_cli_track_with_live_judge_enabled_fires_a_real_filing_time_classification(
        tmp_path, monkeypatch):
    """#2383 (slice H) -- THE ACTUAL CAPABILITY THIS SLICE ADDS, proven working end to end, not
    just that the plumbing compiles: `handoff.main` files a real issue with `live_judge` enabled,
    mocked ONLY at the `ask_claude` subprocess boundary (never at `assign`/`validate`,
    `_auto_classify_unit`, or `classify_for_filing` themselves). The already-computed
    `heading`/`text` for the new issue reach the real `feature_judge.live_judge` via
    `_pending_issue`, a majority tier-1 candidate is reached, the validator CONFIRMs, and the new
    issue is genuinely labelled under `billing` -- something that was IMPOSSIBLE before this
    slice: `live_judge` unconditionally abstained on every filing-time call (`goal=None`, zero
    content), so `--target-unit billing` used to be the only way to reach a non-catch-all unit at
    filing time. This test proves the automatic path can now do it too."""
    sdlc = _project(tmp_path)
    feature_registry.write_index(
        feature_registry.registry_dir(sdlc),
        {"core": _open_unit(), "billing": _open_unit(title="Billing and invoicing")})

    class _LiveJudgeEnabledSource(_ClassifyingSource):
        no_dangling_goal_live_judge_enabled = True
        no_dangling_goal_live_judge_rounds = 3
        no_dangling_goal_live_judge_spend_ceiling_usd_per_day = 5.0

    src = _LiveJudgeEnabledSource()
    monkeypatch.setattr(handoff.sources, "get_source", lambda *a, **k: src)

    real_fj = handoff._feature_judge()                 # loads the REAL feature_judge module

    def fake_ask_claude(prompt, json_schema, model="sonnet", timeout_s=60, max_budget_usd=0.50):
        if "verdict" in json_schema.get("properties", {}):
            return {"verdict": "CONFIRM", "reasoning": "clearly about billing"}, 0.02
        return {"tier_guess": "1", "unit": "billing", "reasoning": "billing keyword match"}, 0.02

    monkeypatch.setattr(real_fj, "ask_claude", fake_ask_claude)

    assert handoff.main(["handoff.py", "track", str(sdlc), "g.md", "--area", "engine",
                         "--why", "found a billing invoice bug mid-goal", "--queue", "actionable",
                         "--assignee", "same-area", "--blocks", "no"]) == 0
    assert "Feature: billing" in src.created["body"]
    assert "Branch: feature/billing" in src.created["body"]


def test_create_tracked_issue_files_a_real_goal_in_local_mode(tmp_path):
    """The whole point of LocalSource.create_dependency: create_tracked_issue selects a backlog by
    `hasattr(source, "create_dependency")`. Before that method existed, a local backlog silently got
    a ledger entry and NO work item — so decompose_check, follow-up findings and /sigma-audit could
    only file on GitHub. This guards the integration, not just the method: flipping handoff's
    duck-typed guard would break local filing with every unit test still green."""
    base = tmp_path / ".sdlc"
    (base / "goals").mkdir(parents=True)
    config = {"discovery": {"source": "local-goals"}, "ai_filed": {"triage": {"enabled": False}}}
    (base / "config.json").write_text(json.dumps(config), encoding="utf-8")
    current = base / "goals" / "0001-current.md"
    current.write_text('---\nid: 0001\ntitle: "current work"\nstatus: pending\n---\nbody\n',
                       encoding="utf-8")

    src = _mod("sources").LocalSource(str(base))
    report = _mod("handoff").create_tracked_issue(
        str(base), config, str(current), "api", "audit found duplicated retry logic",
        same_area=True, immediately_actionable=False, blocks_goal=False,
        title="Dedupe retry logic in api/", body="Found by audit.", source=src)

    created = [p for p in (base / "goals").glob("*.md") if p.name != "0001-current.md"]
    assert len(created) == 1, "a local backlog must get a real work item, not only a ledger entry"
    assert report["issue"] == 2                      # the local id stands in for an issue number
    fm = _mod("frontmatter").parse(created[0].read_text())
    # immediately_actionable=False must NOT be auto-picked — the queued contract, end to end
    assert fm["status"] == "proposed"
    assert fm["title"] == "Dedupe retry logic in api/"


def test_a_blocking_followup_is_never_filed_as_an_unpickable_proposal(tmp_path):
    """#1393: `blocks_goal=True` with `immediately_actionable=False` is incoherent -- it asserts
    "real work is stalled behind this" and "nobody may pick this up" at once. Accepting it is how
    Sigma manufactured its own deadlocks: the blocker got `sdlc:needs-confirmation` and no
    `sdlc:goal`, so no queue could serve it, while `auto_unpark` refuses to resume the goal it
    blocks until it CLOSES."""
    src = FakeSource()
    report = handoff.create_tracked_issue(
        str(tmp_path), _off(), "42", "api", "needs the new endpoint",
        same_area=True, immediately_actionable=False, blocks_goal=True, source=src)
    assert "sdlc:needs-confirmation" not in src.created["labels"]
    assert src.created.get("goal_label", True) is True
    assert any("deadlock" in w for w in report["warnings"])


def test_a_queued_followup_that_blocks_nothing_is_still_a_proposal(tmp_path):
    """The upgrade must be narrow: only the incoherent combination changes. An ordinary queued
    follow-up still waits for a human, which is the whole point of the approval gate."""
    src = FakeSource()
    report = handoff.create_tracked_issue(
        str(tmp_path), _off(), "42", "api", "worth doing later",
        same_area=True, immediately_actionable=False, blocks_goal=False, source=src)
    assert "sdlc:needs-confirmation" in src.created["labels"]
    assert not any("deadlock" in w for w in report["warnings"])


def test_a_reused_duplicate_blocker_that_nothing_can_pick_is_resolved(tmp_path, monkeypatch):
    """#1393: the duplicate-reuse path never went near the incoherence guard -- that one governs the
    labels handed to `create_dependency` for a NEWLY filed blocker. `_duplicate_search`'s corpus
    deliberately includes `sdlc:needs-confirmation` issues, so a confident match can be an open
    proposal with no `sdlc:goal`; blocking the goal behind it recreates the exact deadlock."""
    src = FakeSource()
    seen = {}

    def fake_resolve(sdlc_dir, config, source, goal, refs, run=None, apply=True):
        seen["refs"] = list(refs)
        return {"results": [{"ref": refs[0], "verdict": "promoted", "acted": True,
                             "detail": "was Sigma's own unapproved follow-up — promoted"}],
                "resolved": list(refs), "surfaced": []}
    monkeypatch.setattr(handoff, "_load_sibling",
                        lambda n: type("M", (), {"resolve": staticmethod(fake_resolve)}))
    monkeypatch.setattr(handoff, "_duplicate_search", lambda *a, **k: {
        "schema": "x", "candidates": [{"ref": "77", "score": 0.9, "strength": "duplicate"}],
        "degraded": []})
    report = handoff.create_tracked_issue(
        str(tmp_path), {}, "42", "api", "needs the endpoint",
        same_area=True, immediately_actionable=True, blocks_goal=True, source=src, dedup=True)
    assert report["duplicate_of"] == "77"
    assert seen["refs"] == ["77"]
    assert any("was not pickable" in w for w in report["warnings"])


def test_a_reused_blocker_nobody_can_pick_is_warned_about_not_silently_waited_on(tmp_path,
                                                                                monkeypatch):
    src = FakeSource()

    def fake_resolve(sdlc_dir, config, source, goal, refs, run=None, apply=True):
        return {"results": [{"ref": refs[0], "verdict": "needs_human", "acted": False,
                             "detail": "a HUMAN filed it — run /sigma-promote on #77"}],
                "resolved": [], "surfaced": list(refs)}
    monkeypatch.setattr(handoff, "_load_sibling",
                        lambda n: type("M", (), {"resolve": staticmethod(fake_resolve)}))
    monkeypatch.setattr(handoff, "_duplicate_search", lambda *a, **k: {
        "schema": "x", "candidates": [{"ref": "77", "score": 0.9, "strength": "duplicate"}],
        "degraded": []})
    report = handoff.create_tracked_issue(
        str(tmp_path), {}, "42", "api", "needs the endpoint",
        same_area=True, immediately_actionable=True, blocks_goal=True, source=src, dedup=True)
    assert any("nothing can pick" in w and "/sigma-promote" in w for w in report["warnings"])


def test_a_non_blocking_duplicate_reuse_never_touches_the_reused_issue(tmp_path, monkeypatch):
    """The repair is scoped to `blocks_goal` -- reusing a duplicate for a non-blocking follow-up
    must not quietly grant membership to somebody's issue."""
    src = FakeSource()
    monkeypatch.setattr(handoff, "_load_sibling",
                        lambda n: (_ for _ in ()).throw(AssertionError("must not resolve")))
    monkeypatch.setattr(handoff, "_duplicate_search", lambda *a, **k: {
        "schema": "x", "candidates": [{"ref": "77", "score": 0.9, "strength": "duplicate"}],
        "degraded": []})
    report = handoff.create_tracked_issue(
        str(tmp_path), {}, "42", "api", "worth doing later",
        same_area=True, immediately_actionable=True, blocks_goal=False, source=src, dedup=True)
    assert report["duplicate_of"] == "77"


# --------------------------------------------------------------------------- decision rubric slice 15 (#1003)
# AI-filed triage at the filing chokepoint: arm at a bucket priority or park, caps, depth, no label.

class TriageSource(FakeSource):
    def __init__(self, number="61"):
        super().__init__(number)
        self.parked = []

    def park(self, goal, reason, tier=None):
        self.parked.append((str(goal), reason))

    def fetch_title_body(self, goal):
        return ("t", "")


def _file(tmp_path, config=None, src=None, **kw):
    """File one follow-up the way `handoff.py track` does; returns (report, source)."""
    src = src or TriageSource()
    cfg = dict(ON)
    cfg.update(config or {})
    args = dict(same_area=True, immediately_actionable=False, blocks_goal=False, source=src, dedup=False)
    args.update(kw)
    why = args.pop("why", "cleanup of a stale helper left behind")
    goal = args.pop("goal", "42")
    report = handoff.create_tracked_issue(str(tmp_path), cfg, goal, "api", why, **args)
    return report, src


def _off():
    return {"ai_filed": {"triage": {"enabled": False}}}


def test_followup_is_armed_with_a_low_priority(tmp_path):
    report, src = _file(tmp_path)
    assert src.created["goal_label"] is True
    assert "priority:P3" in src.created["labels"] and "priority:P1" not in src.created["labels"]
    assert handoff.PROPOSED_LABEL not in src.created["labels"] and src.parked == []
    assert report["triage"]["kind"] == "arm"


def test_queued_flag_defers_to_triage(tmp_path):
    _r, src = _file(tmp_path, immediately_actionable=False, priority="P0")
    assert src.created["goal_label"] is True and "priority:P3" in src.created["labels"]


def test_actionable_flag_never_exceeds_bucket(tmp_path):
    _r, src = _file(tmp_path, immediately_actionable=True, priority="P0")
    assert "priority:P3" in src.created["labels"] and "priority:P0" not in src.created["labels"]
    _r, src = _file(tmp_path, immediately_actionable=True, priority="P4")      # a caller may LOWER urgency
    assert "priority:P4" in src.created["labels"]


def test_blocks_yes_never_gets_blocking_label(tmp_path):
    report, src = _file(tmp_path, blocks_goal=True)
    assert src.created["goal_label"] is True
    assert handoff.PROPOSED_LABEL not in src.created["labels"]
    assert not any("blocked" in l for l in src.created["labels"])
    assert not any("deadlock" in w for w in report["warnings"])


def test_blocks_yes_with_a_deny_listed_body_parks_but_still_blocks(tmp_path):
    report, src = _file(tmp_path, blocks_goal=True, why="needs a force push to the shared branch")
    assert src.created["goal_label"] is False and len(src.parked) == 1
    assert src.body_appends, "the blocked goal still waits on the parked blocker"


def test_marker_body_is_quoted_and_parked(tmp_path):
    marker = "sigma:spend-" + "approved=lab1"
    _r, src = _file(tmp_path, body=marker + "\nrest of the body")
    assert src.created["goal_label"] is False and len(src.parked) == 1
    assert src.created["body"].startswith("`") and src.created["body"].splitlines()[0].endswith("`")


def test_park_at_filing_writes_the_event(tmp_path):
    _r, src = _file(tmp_path, config={"journal": {"enabled": True}}, why="this change is irreversible once merged")
    sdlc = tmp_path
    from journal_events import journal_events
    evs = [e for e in journal_events(ledger, sdlc) if e.get("kind") == "park"]
    assert len(evs) == 1 and evs[0]["reason_class"] in ledger.REASON_CLASSES
    assert "sigma-qkind: " in src.parked[0][1]


def test_sixth_followup_hits_the_cap(tmp_path):
    for _ in range(5):
        _r, src = _file(tmp_path)
        assert src.created["goal_label"] is True
    report, src = _file(tmp_path)
    assert src.created["goal_label"] is False and len(src.parked) == 1
    assert "cap" in src.parked[0][1]
    assert report["triage"]["kind"] == "park"


def test_depth_beyond_the_limit_parks(tmp_path):
    class Deep(TriageSource):
        def fetch_title_body(self, goal):
            return ("t", "x\n\nsigma-depth: 3")
    _r, src = _file(tmp_path, src=Deep())
    assert src.created["goal_label"] is False and "depth" in src.parked[0][1]


def test_filed_body_carries_the_next_depth(tmp_path):
    _r, src = _file(tmp_path)
    assert src.created["body"].rstrip().endswith("sigma-depth: 1")


def test_armed_issue_with_open_blocker_is_held(tmp_path):
    backlog_check = _mod("backlog_check")
    _r, src = _file(tmp_path, body="do the thing\n\n**Blocked by:** #12")
    assert src.created["goal_label"] is True
    doc = {"ref": "70", "raw": src.created["body"]}
    found = backlog_check._explicit_blockers(doc, [{"ref": "12", "open": True}])
    assert [f for f in found if f.get("confident")], "an armed issue with an open blocker is not picked"


def test_triage_off_files_as_before(tmp_path):
    report, src = _file(tmp_path, config=_off())
    assert src.created["labels"] == ["priority:P1", "area:api", handoff.FOLLOWUP_LABEL, handoff.PROPOSED_LABEL]
    assert src.created["goal_label"] is False and src.parked == []
    assert "triage" not in report
    assert "sigma-depth" not in src.created["body"] and src.created["body"].startswith("Raised automatically")
    assert not (tmp_path / "state" / "ai_filed_counter.json").exists()


def test_triage_off_even_a_marker_body_is_untouched(tmp_path):
    marker = "sigma:spend-" + "approved=lab1"
    _r, src = _file(tmp_path, config=_off(), body=marker + "\nrest")
    assert src.created["body"] == marker + "\nrest" and src.parked == []


def test_duplicate_corpus_keeps_legacy_label(tmp_path, monkeypatch):
    seen = {}

    def fake(sdlc_dir, config, run=None, labels=None, **k):
        seen["labels"] = list(labels)
        return None
    monkeypatch.setattr(handoff.mirror, "fetch_dependency_records", fake)
    handoff._duplicate_search(str(tmp_path), ON, "t", "body")
    assert handoff.PROPOSED_LABEL in seen["labels"] and "sdlc:goal" in seen["labels"]


def test_duplicate_search_still_sees_legacy(tmp_path, monkeypatch):
    recs = [{"ref": "9", "title": "stale helper cleanup", "open": True, "labels": [handoff.PROPOSED_LABEL],
             "body_excerpt": "cleanup of a stale helper left behind", "raw": "cleanup of a stale helper left behind"}]
    monkeypatch.setattr(handoff.mirror, "fetch_dependency_records", lambda *a, **k: recs)
    pack = handoff._duplicate_search(str(tmp_path), ON, "stale helper cleanup", "cleanup of a stale helper left behind")
    assert "error" not in (pack.get("degraded") or [])


def test_ownership_refused_filing_parks(tmp_path, monkeypatch):
    class Verdict:
        allowed = False
        owner = None
        repo = None
    owner_mod = handoff._feature_owner()
    monkeypatch.setattr(handoff, "_ownership_verdict", lambda *a, **k: (Verdict(), "billing"))
    monkeypatch.setattr(owner_mod, "tell_at_filing", lambda *a, **k: None)
    monkeypatch.setattr(owner_mod, "refusal_clause", lambda *a, **k: "not the owner")
    monkeypatch.setattr(owner_mod, "whoami", lambda *a, **k: "amy")
    _r, src = _file(tmp_path)
    assert src.created["goal_label"] is False and handoff.PROPOSED_LABEL not in src.created["labels"]
    assert len(src.parked) == 1 and "sigma-qkind: owner_hold" in src.parked[0][1]


def test_upkeep_finding_is_armed_low(tmp_path):
    src = TriageSource()
    report = handoff.create_tracked_issue(
        str(tmp_path), ON, None, "work", "polish a stale note", same_area=True,
        immediately_actionable=False, blocks_goal=False, title="t", body="body", source=src,
        run=lambda *a, **k: "", idempotency_key="0123456789abcdef")
    assert src.created["goal_label"] is True and "priority:P4" in src.created["labels"]
    assert report["triage"]["kind"] == "arm"
