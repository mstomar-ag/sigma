"""goal_design: the RETROFIT half of `docs/dossier-pipeline.md` §5, contracted at §6 --
`loop.py`'s `design-check` verb, mirroring `decompose_check`'s own `file`-mode shape one section up
(tests/test_decompose_check.py). Before a picked goal spends a token: unless it already carries
`sdlc:designed`, park it and file ONE idempotency-guarded "Design #N" meta-issue instructing a
codebase-mapping design pass (`skills/sigma-goal-design/SKILL.md`). Opt-in (`goal_design.enabled`),
zero LLM, fail-open before the label/comment read, fail-CLOSED for that one read, off by default.
Hermetic, $0.

No conftest.py in this repo (tests/test_import_boundary.py's own docstring records why) — every
helper below is copied in from tests/test_decompose_check.py, not imported."""
import json
import pathlib
import importlib.util
import tempfile

import pytest
from skill_corpus import skill_corpus

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"
ROOT = pathlib.Path(__file__).resolve().parent.parent


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


_UNSET = object()   # sentinel: "no override given" -- None is itself one of the values the
                     # malformed-shape tests need fetch_comments_strict() to be able to return.


class _FakeSource:
    """Copied from tests/test_decompose_check.py's own `_FakeSource` (same rationale: enough
    surface for `design_check` to drive -- fetch_title_body/park/note/complete/fail/
    create_dependency/last_assignee_applied/issue_url/fetch_comments_strict -- without ever
    touching a real GitHubSource)."""
    def __init__(self, title="", body="", issue_number="90", comments=None, labels=None,
                 last_assignee_applied=True, create_dependency_error=None,
                 fetch_comments_strict_error=None, fetch_comments_strict_return=_UNSET,
                 note_error=None, note_error_on_call=None):
        self.calls = []
        self._title = title
        self._body = body
        self.issue_number = issue_number
        self._comments = comments if comments is not None else []
        self._labels = labels if labels is not None else []
        self.last_assignee_applied = last_assignee_applied
        self._create_dependency_error = create_dependency_error
        self._fetch_comments_strict_error = fetch_comments_strict_error
        self._fetch_comments_strict_return = fetch_comments_strict_return
        self._note_error = note_error
        self._note_error_on_call = note_error_on_call
        self._note_call_count = 0
        self.created = None

    def fetch_title_body(self, goal):
        self.calls.append(("fetch_title_body", goal))
        return {"title": self._title, "body": self._body}

    def fetch_comments_strict(self, goal):
        self.calls.append(("fetch_comments_strict", goal))
        if self._fetch_comments_strict_error:
            raise self._fetch_comments_strict_error
        if self._fetch_comments_strict_return is not _UNSET:
            return self._fetch_comments_strict_return
        return {"comments": self._comments, "labels": self._labels}

    def create_dependency(self, title, body, assignee, labels=(), goal_label=True):
        self.calls.append(("create_dependency", title, body, assignee, tuple(labels), goal_label))
        if self._create_dependency_error:
            raise self._create_dependency_error
        self.created = {"title": title, "body": body, "assignee": assignee, "labels": list(labels)}
        return self.issue_number

    def issue_url(self, goal):
        return f"https://example.invalid/issues/{goal}"

    def park(self, goal, reason): self.calls.append(("park", goal, reason))

    def note(self, goal, text):
        self._note_call_count += 1
        self.calls.append(("note", goal, text))
        if self._note_error and (self._note_error_on_call is None
                                  or self._note_call_count == self._note_error_on_call):
            raise self._note_error

    def complete(self, goal): self.calls.append(("complete", goal))
    def fail(self, goal, reason): self.calls.append(("fail", goal, reason))


def _sdlc(tmp_path, config):
    """Mirrors tests/test_decompose_check.py's `_sdlc()`."""
    base = tmp_path / ".sdlc"
    (base / "state").mkdir(parents=True)
    (base / "config.json").write_text(json.dumps(config))
    (base / "state" / "STATE.md").write_text("iteration: 0\nrun_iteration: 0\nlast_run: none\n")
    (base / "state" / "review-queue.md").write_text("# Q\n")
    return str(base)


def _cfg(base):
    return json.loads((pathlib.Path(base) / "config.json").read_text())


def _design_cfg(tmp_path, mode=None, extra=None):
    gd = {"enabled": True}
    if mode is not None:
        gd["mode"] = mode
    # pre-triage filing path pinned here; design children are converted by a later slice (#1003)
    cfg = {"goal_design": gd, "ledger": {"actor": "rae"}, "ai_filed": {"triage": {"enabled": False}}}
    if extra:
        cfg.update(extra)
    return _sdlc(tmp_path, cfg)


# A body long/structured enough that goal_size.classify would flag it -- used to prove
# decompose_check's DESIGN_OF_MARKER exemption is what saves it, not merely a short body.
_EPIC_BODY = (
    "## Phase 1: backend\nBuild the backend pieces.\n\n"
    "## Phase 2: frontend\nBuild the UI pieces.\n\n"
    "## Phase 3: migration\nMigrate the old data.\n\n"
    "## Verification\nRun the whole suite.\n"
)

_PLAIN_BODY = "Map #90 to the codebase and produce a design.\n"


# --------------------------------------------------------------------- config gate (OFF / fail-open)


def test_design_check_off_when_disabled_touches_nothing(tmp_path, capsys):
    lp = _mod("loop")
    base = _sdlc(tmp_path, {"goal_design": {"enabled": False}})
    src = _FakeSource(body=_PLAIN_BODY)
    assert lp.design_check(base, "1", _cfg(base), src) == "OFF"
    assert src.calls == []
    assert capsys.readouterr().err == ""


def test_design_check_off_when_key_absent_touches_nothing(tmp_path, capsys):
    lp = _mod("loop")
    base = _sdlc(tmp_path, {})
    src = _FakeSource(body=_PLAIN_BODY)
    assert lp.design_check(base, "1", _cfg(base), src) == "OFF"
    assert src.calls == []
    assert capsys.readouterr().err == ""


def test_design_check_off_when_enabled_key_absent_touches_nothing(tmp_path, capsys):
    lp = _mod("loop")
    base = _sdlc(tmp_path, {"goal_design": {}})
    src = _FakeSource(body=_PLAIN_BODY)
    assert lp.design_check(base, "1", _cfg(base), src) == "OFF"
    assert src.calls == []
    assert capsys.readouterr().err == ""


def test_design_check_off_on_falsy_malformed_config(tmp_path, capsys):
    lp = _mod("loop")
    for i, bogus in enumerate((False, 0, "", [])):
        base = _sdlc(tmp_path / str(i), {"goal_design": bogus})
        src = _FakeSource(body=_PLAIN_BODY)
        assert lp.design_check(base, "1", _cfg(base), src) == "OFF", bogus
        assert src.calls == [], bogus
        assert capsys.readouterr().err == "", bogus


def test_design_check_fails_open_on_truthy_malformed_config(tmp_path, capsys):
    lp = _mod("loop")
    for i, bogus in enumerate(("on", True, 5)):
        base = _sdlc(tmp_path / str(i), {"goal_design": bogus})
        src = _FakeSource(body=_PLAIN_BODY)
        assert lp.design_check(base, "1", _cfg(base), src) == "PROCEED", bogus
        assert src.calls == [], bogus
        err = capsys.readouterr().err
        assert err.strip() != "" and err.count("\n") == 1, bogus


# --------------------------------------------------------------------- first-line anchoring / exemption


def test_design_check_proceeds_for_a_design_of_meta_goal_marked_first_line(tmp_path):
    lp = _mod("loop")
    dg = _mod("design_goal")
    base = _design_cfg(tmp_path)
    body = f"{dg.DESIGN_OF_MARKER}#100\n\n" + _PLAIN_BODY
    src = _FakeSource(body=body)
    assert lp.design_check(base, "1", _cfg(base), src) == "PROCEED"
    assert src.calls == [("fetch_title_body", "1")]        # read, but never label-checked/filed


def test_design_check_proceeds_for_a_decompose_of_meta_goal_marked_first_line(tmp_path):
    """decompose_check's own bookkeeping meta-goal is exempt here too, symmetric with the one
    above -- neither pure meta-issue is itself design-worthy."""
    lp = _mod("loop")
    gs = _mod("goal_size")
    base = _design_cfg(tmp_path)
    body = f"{gs.DECOMPOSE_OF_MARKER}#100\n\n" + _PLAIN_BODY
    src = _FakeSource(body=body)
    assert lp.design_check(base, "1", _cfg(base), src) == "PROCEED"
    assert src.calls == [("fetch_title_body", "1")]


def test_design_check_does_not_exempt_a_decomposed_from_child(tmp_path):
    """The one deliberate asymmetry with decompose_check's own exemption set: a decompose CHILD is
    a real, independently-implementable slice with no blast-radius mapping of its own -- exempting
    it here would defeat design_check's whole coverage guarantee (contract §2's stated gap)."""
    lp = _mod("loop")
    gs = _mod("goal_size")
    base = _design_cfg(tmp_path)
    body = f"{gs.DECOMPOSED_FROM_MARKER}100\n\n" + _PLAIN_BODY
    src = _FakeSource(body=body, labels=[])

    result = lp.design_check(base, "1", _cfg(base), src)

    assert result != "PROCEED"                              # NOT exempt -- proceeds to the real check
    assert any(c[0] == "fetch_comments_strict" for c in src.calls)


def test_design_check_a_marker_in_the_title_only_does_not_exempt(tmp_path):
    lp = _mod("loop")
    dg = _mod("design_goal")
    base = _design_cfg(tmp_path)
    src = _FakeSource(title=f"{dg.DESIGN_OF_MARKER}#100 — my design", body=_PLAIN_BODY, labels=[])

    result = lp.design_check(base, "1", _cfg(base), src)

    assert any(c[0] == "fetch_comments_strict" for c in src.calls)   # classification actually ran
    assert result != "PROCEED"


def test_design_check_anchoring_is_crlf_tolerant(tmp_path):
    lp = _mod("loop")
    dg = _mod("design_goal")
    base = _design_cfg(tmp_path)
    body = f"{dg.DESIGN_OF_MARKER}#100\r\n\r\n" + _PLAIN_BODY.replace("\n", "\r\n")
    src = _FakeSource(body=body)
    assert lp.design_check(base, "1", _cfg(base), src) == "PROCEED"


# --------------------------------------------------------------------- already designed / already filed


def test_design_check_proceeds_when_sdlc_designed_label_present(tmp_path):
    lp = _mod("loop")
    base = _design_cfg(tmp_path)
    src = _FakeSource(body=_PLAIN_BODY, labels=[{"name": "sdlc:designed"}, {"name": "area:engine"}])

    result = lp.design_check(base, "7", _cfg(base), src)

    assert result == "PROCEED"
    assert not any(c[0] == "create_dependency" for c in src.calls)
    # single fetch -- fetch_comments_strict is not asked to do anything further once labelled
    assert len([c for c in src.calls if c[0] == "fetch_comments_strict"]) == 1


def test_design_check_idempotency_hit_parks_without_creating(tmp_path):
    lp = _mod("loop")
    dg = _mod("design_goal")
    base = _design_cfg(tmp_path)
    src = _FakeSource(body=_PLAIN_BODY,
                       comments=[{"body": "just a note"},
                                 {"body": f"already filed: {dg.DESIGN_FILED_MARKER}=#901"}])

    result = lp.design_check(base, "7", _cfg(base), src)

    assert result == "PARKED design already filed — see comments"
    assert not any(c[0] == "create_dependency" for c in src.calls)
    assert len([c for c in src.calls if c[0] == "park"]) == 1


# --------------------------------------------------------------------- fail-closed on an unreadable read


def test_design_check_strict_read_raising_fails_closed_never_proceeds(tmp_path):
    lp = _mod("loop")
    base = _design_cfg(tmp_path)
    src = _FakeSource(body=_PLAIN_BODY, fetch_comments_strict_error=RuntimeError("gh: rate limited"))

    result = lp.design_check(base, "7", _cfg(base), src)

    assert result == ("PARKED could not confirm whether this goal already carries sdlc:designed "
                       "— check labels/comments")
    assert not any(c[0] == "create_dependency" for c in src.calls)


@pytest.mark.parametrize("bogus", [None, [], {}, {"comments": []}, {"labels": []},
                                    {"labels": "x", "comments": []}, {"labels": [], "comments": "x"}])
def test_design_check_malformed_strict_read_fails_closed(tmp_path, bogus):
    """Every shape here is exactly as untrustworthy as the read raising outright -- a real
    GitHubSource always returns `{"comments": [...], "labels": [...]}`, but `fetch_comments_strict`
    is resolved via a bare `hasattr` off whatever source design_check is given, never guaranteed to
    be one. Collapsing any of these into "not designed, but also not confirmed" would be fine --
    what must NEVER happen is treating a malformed read as evidence design_check safely reached
    PROCEED or safely filed a fresh meta-issue on top of an unreadable label set."""
    lp = _mod("loop")
    base = _design_cfg(tmp_path)
    src = _FakeSource(body=_PLAIN_BODY, fetch_comments_strict_return=bogus)

    result = lp.design_check(base, "7", _cfg(base), src)

    assert result == ("PARKED could not confirm whether this goal already carries sdlc:designed "
                       "— check labels/comments")
    assert not any(c[0] == "create_dependency" for c in src.calls)


def test_design_check_degrades_to_park_when_source_has_no_issue_tracker_seam(capsys):
    """Mirrors test_file_mode_degrades_to_park_when_source_has_no_create_seam: a source offering
    neither fetch_comments_strict nor create_dependency (a LocalSource) cannot safely confirm
    "not already designed" nor file a tracked meta-issue -- degrade to a plain, honest park."""
    lp = _mod("loop")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"
        (base / "goals").mkdir(parents=True)
        (base / "state").mkdir()
        (base / "config.json").write_text(json.dumps(
            {"goal_design": {"enabled": True}, "budget": {"max_iterations": 100}}))
        (base / "state" / "STATE.md").write_text("iteration: 0\nrun_iteration: 0\nlast_run: none\n")
        (base / "state" / "review-queue.md").write_text("# Q\n")
        goal_path = base / "goals" / "0001.md"
        goal_path.write_text("---\nid: 0001\nstatus: pending\ntitle: t\n---\n" + _PLAIN_BODY)
        rc = lp.main(["loop.py", "design-check", str(base), str(goal_path)])
        assert rc == 0
        out = capsys.readouterr().out
        assert out.startswith("PARKED")
        assert "coverage check needs an issue tracker" in out


# --------------------------------------------------------------------- happy path filing


def test_design_check_happy_path_ordering_one_create_two_notes_marker_last_one_park(tmp_path):
    lp = _mod("loop")
    base = _design_cfg(tmp_path)
    src = _FakeSource(title="a goal with no design yet", body=_PLAIN_BODY, issue_number="901",
                       labels=[{"name": "area:engine"}, {"name": "priority:P0"}])

    result = lp.design_check(base, "7", _cfg(base), src)

    assert result.startswith("PARKED not yet designed")
    assert "design pass filed as #901" in result

    kinds = [c[0] for c in src.calls if c[0] in ("create_dependency", "note", "park")]
    assert kinds == ["create_dependency", "note", "note", "park"]

    notes = [c for c in src.calls if c[0] == "note"]
    assert "design-filed" not in notes[0][2]                # narrative note (create_tracked_issue)
    assert "design-filed" in notes[1][2]                    # OUR marker note is LAST

    assert len([c for c in src.calls if c[0] == "fetch_title_body"]) == 1     # not re-fetched

    create_call = next(c for c in src.calls if c[0] == "create_dependency")
    _, title, body, assignee, labels, goal_label = create_call
    assert title.startswith("Design #7:") and "a goal with no design yet" in title
    assert assignee == "rae"                                # same_area=True -> ledger.actor
    assert "area:engine" in labels and "priority:P0" in labels
    assert goal_label is True                               # immediately_actionable=True
    assert body.splitlines()[0] == "<!-- sigma:design-of=#7 -->"
    # create_tracked_issue's own FOLLOWUP_LABEL ("sdlc:followup") is applied unconditionally to
    # EVERY tracked issue it files, independent of this check -- an existing, already-bootstrapped
    # provenance label (docs/label-model.md: "came out of a retro / was filed by the loop"), not a
    # new label design_check itself asks for.
    assert set(labels) == {"area:engine", "priority:P0", "sdlc:followup"}


def test_design_check_no_extra_labels_are_ever_created(tmp_path):
    """Unlike decompose_check's `file` mode (`extra_labels=["sdlc:decompose"]`), design_check
    passes no `extra_labels` at all -- the only labels on the filed meta-issue are the inherited
    `area:`/`priority:` pair plus create_tracked_issue's own unconditional `sdlc:followup`
    provenance label. No NEW GitHub label is created by this check; `sdlc:designed` is written
    only by goal-review (#1826), never here."""
    lp = _mod("loop")
    base = _design_cfg(tmp_path)
    src = _FakeSource(body=_PLAIN_BODY, issue_number="901")

    lp.design_check(base, "7", _cfg(base), src)

    create_call = next(c for c in src.calls if c[0] == "create_dependency")
    labels = create_call[4]
    assert set(labels) == {"area:unknown", "priority:P1", "sdlc:followup"}
    assert "sdlc:designed" not in labels and "sdlc:decompose" not in labels


def test_design_check_create_fail_parks_with_warnings_and_never_posts_a_marker(tmp_path):
    lp = _mod("loop")
    base = _design_cfg(tmp_path)
    src = _FakeSource(body=_PLAIN_BODY, create_dependency_error=RuntimeError("gh: not authenticated"))

    result = lp.design_check(base, "7", _cfg(base), src)

    assert result.startswith("PARKED not yet designed — failed to file design goal")
    assert "not authenticated" in result
    assert result.endswith("— needs a human")
    assert not any(c[0] == "note" for c in src.calls)
    assert len([c for c in src.calls if c[0] == "park"]) == 1


def test_design_check_marker_post_failure_still_parks_with_the_warning_folded_in(tmp_path):
    lp = _mod("loop")
    base = _design_cfg(tmp_path)
    src = _FakeSource(body=_PLAIN_BODY, issue_number="901",
                       note_error=RuntimeError("gh: comment failed"), note_error_on_call=2)

    result = lp.design_check(base, "7", _cfg(base), src)

    assert result.startswith("PARKED not yet designed")
    assert "design pass filed as #901" in result
    assert "comment failed" in result
    notes = [c for c in src.calls if c[0] == "note"]
    assert len(notes) == 2
    assert "design-filed" in notes[1][2]


def test_design_check_unassigned_surfaces_in_the_park_detail(tmp_path):
    lp = _mod("loop")
    base = _design_cfg(tmp_path)
    src = _FakeSource(body=_PLAIN_BODY, issue_number="901", last_assignee_applied=False)

    result = lp.design_check(base, "7", _cfg(base), src)

    assert result.startswith("PARKED")
    assert "filed as #901 but unassigned — a human must assign it before any loop can see it" in result


def test_design_check_area_and_priority_default_when_the_goal_has_no_such_labels(tmp_path):
    lp = _mod("loop")
    base = _design_cfg(tmp_path)
    src = _FakeSource(body=_PLAIN_BODY, issue_number="901")     # no labels configured

    lp.design_check(base, "7", _cfg(base), src)

    create_call = next(c for c in src.calls if c[0] == "create_dependency")
    _, _title, _body, _assignee, labels, _goal_label = create_call
    assert "area:unknown" in labels
    assert "priority:P1" in labels                              # handoff.DEFAULT_PRIORITY


def test_park_survives_a_failure_after_source_park_has_already_landed(tmp_path, monkeypatch):
    """Mirrors test_decompose_check.py's identical-purpose test one section up: once the park
    mutation (`source.park`) has already gone out, a later bookkeeping failure inside `_record`
    must never downgrade the reported result to PROCEED."""
    lp = _mod("loop")
    base = _design_cfg(tmp_path)
    src = _FakeSource(body=_PLAIN_BODY, issue_number="901")

    def _boom(*a, **kw):
        raise RuntimeError("ledger boom")
    monkeypatch.setattr(lp.ledger, "safe_append", _boom)

    result = lp.design_check(base, "7", _cfg(base), src)

    assert result.split()[0] == "PARKED", \
        f"must never downgrade to PROCEED after the mutation landed, got: {result!r}"
    park_calls = [c for c in src.calls if c[0] == "park"]
    assert len(park_calls) == 1


# --------------------------------------------------------------------- mode: depth, not action


def test_absent_mode_key_behaves_as_full(tmp_path, capsys):
    lp = _mod("loop")
    base = _design_cfg(tmp_path)   # no "mode" key at all
    src = _FakeSource(body=_PLAIN_BODY, issue_number="901")

    lp.design_check(base, "7", _cfg(base), src)

    create_call = next(c for c in src.calls if c[0] == "create_dependency")
    assert "FULL" in create_call[2]
    assert capsys.readouterr().err == ""


def test_mode_lane_renders_lane_instructions(tmp_path):
    lp = _mod("loop")
    base = _design_cfg(tmp_path, mode="lane")
    src = _FakeSource(body=_PLAIN_BODY, issue_number="901")

    lp.design_check(base, "7", _cfg(base), src)

    create_call = next(c for c in src.calls if c[0] == "create_dependency")
    assert "LANE" in create_call[2]


def test_unrecognized_mode_never_warns_when_the_goal_is_already_designed(tmp_path, capsys):
    """`mode` picks the filed meta-issue's DEPTH, not whether design_check acts -- it must only be
    resolved (and only warn on a typo) once we know a meta-issue is actually about to be filed.
    An already-`sdlc:designed` goal never reaches that point, so a bogus `mode` in config must
    never spam a warning on every ordinary pick of an already-designed goal."""
    lp = _mod("loop")
    base = _design_cfg(tmp_path, mode="bogus-mode")
    src = _FakeSource(body=_PLAIN_BODY, labels=[{"name": "sdlc:designed"}])

    result = lp.design_check(base, "7", _cfg(base), src)

    assert result == "PROCEED"
    assert capsys.readouterr().err == ""


def test_unrecognized_mode_falls_back_to_lane_with_a_stderr_warning(tmp_path, capsys):
    lp = _mod("loop")
    base = _design_cfg(tmp_path, mode="bogus-mode")
    src = _FakeSource(body=_PLAIN_BODY, issue_number="901")

    result = lp.design_check(base, "7", _cfg(base), src)

    assert result.startswith("PARKED")                 # unlike decompose_check, mode never gates
    create_call = next(c for c in src.calls if c[0] == "create_dependency")
    assert "LANE" in create_call[2]                     # fell back to 'lane', not 'full'
    err = capsys.readouterr().err
    assert "bogus-mode" in err


# --------------------------------------------------------------------- CLI dispatch (local mode)


def test_design_check_cli_prints_off_when_disabled(capsys):
    lp = _mod("loop")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"
        (base / "goals").mkdir(parents=True)
        (base / "state").mkdir()
        (base / "config.json").write_text(json.dumps({"budget": {"max_iterations": 100}}))
        (base / "state" / "STATE.md").write_text("iteration: 0\nrun_iteration: 0\nlast_run: none\n")
        goal_path = base / "goals" / "0001.md"
        goal_path.write_text("---\nid: 0001\nstatus: pending\ntitle: t\n---\nx\n")
        rc = lp.main(["loop.py", "design-check", str(base), str(goal_path)])
        assert rc == 0
        assert capsys.readouterr().out.strip() == "OFF"


def test_design_check_is_in_the_usage_string(capsys):
    lp = _mod("loop")
    assert lp.main(["loop.py"]) == 2
    assert "design-check <dir> <goal>" in capsys.readouterr().err


# --------------------------------------------------------------------- design_goal.py (the meta-goal
# template + filed-marker helper design_check's file step drives)


def test_design_meta_body_first_line_is_the_marker():
    dg = _mod("design_goal")
    body = dg.render_meta_body("825", "full")
    assert body.splitlines()[0] == "<!-- sigma:design-of=#825 -->"


def test_design_meta_body_reads_the_marker_constant_live_not_a_hardcoded_copy():
    dg = _mod("design_goal")
    real_marker = dg.DESIGN_OF_MARKER
    dg.DESIGN_OF_MARKER = "totally-renamed-marker="
    try:
        body = dg.render_meta_body("825", "full")
        assert body.splitlines()[0] == "<!-- totally-renamed-marker=#825 -->"
    finally:
        dg.DESIGN_OF_MARKER = real_marker


def test_design_meta_body_documents_no_implementation_here():
    dg = _mod("design_goal")
    body = dg.render_meta_body("825", "full")
    assert "never implement" in body.lower()


def test_design_meta_body_documents_sdlc_designed_is_goal_review_only():
    dg = _mod("design_goal")
    body = dg.render_meta_body("825", "full")
    assert "sdlc:designed" in body
    assert "goal-review" in body
    assert "/sigma-unpark" in body


def test_design_meta_body_documents_the_design_artifact_path():
    dg = _mod("design_goal")
    body = dg.render_meta_body("825", "full")
    assert ".sdlc/design/825.md" in body


def test_design_meta_body_documents_skip_work_py():
    dg = _mod("design_goal")
    body = dg.render_meta_body("825", "full")
    assert "skip" in body.lower() and "work.py" in body


def test_design_meta_body_step_0_reconciliation_is_direct_reads_never_search():
    dg = _mod("design_goal")
    body = dg.render_meta_body("825", "full")
    assert "CLOSED" in body and "already done" in body
    assert "never search" in body.lower() or "never a search" in body.lower()


def test_design_meta_body_lower_number_wins_tie_break():
    dg = _mod("design_goal")
    body = dg.render_meta_body("825", "full")
    assert "lower-number-wins" in body.lower() or "lower number" in body.lower()


@pytest.mark.parametrize("mode,expect,exclude", [("full", "FULL", "LANE"), ("lane", "LANE", "FULL")])
def test_design_meta_body_mode_instructions_are_depth_specific(mode, expect, exclude):
    dg = _mod("design_goal")
    body = dg.render_meta_body("825", mode)
    assert expect in body
    assert exclude not in body


def test_filed_marker_comment_shape():
    dg = _mod("design_goal")
    text = dg.filed_marker_comment("901")
    assert dg.DESIGN_FILED_MARKER in text
    assert "#901" in text


def test_design_filed_marker_is_a_bare_substring_of_its_own_comment():
    dg = _mod("design_goal")
    assert dg.DESIGN_FILED_MARKER in dg.filed_marker_comment("901")


# --------------------------------------------------------------------- decompose_check symmetry
# (the one small, additive edit inside decompose_check itself, per the plan)


def test_decompose_check_proceeds_for_a_design_of_meta_goal_without_ever_classifying(tmp_path):
    """decompose_check must recognize design_check's own filed "Design #N" meta-issue as exempt
    too -- proven via a body that would otherwise unambiguously classify as oversized, so the
    exemption (not the body's length) is what saves it, mirroring
    test_decompose_check_proceeds_for_a_meta_goal_marked_first_line's own proof shape."""
    lp = _mod("loop")
    dg = _mod("design_goal")
    base = _sdlc(tmp_path, {"goal_decompose": {"enabled": True, "mode": "park"}})
    body = f"{dg.DESIGN_OF_MARKER}#100\n\n" + _EPIC_BODY     # would otherwise clearly flag
    src = _FakeSource(body=body)
    assert lp.decompose_check(base, "1", _cfg(base), src) == "PROCEED"
    assert src.calls == [("fetch_title_body", "1")]          # read, but never classified/parked


# --------------------------------------------------------------------- config-template discoverability


def test_goal_design_key_is_discoverable_in_the_scaffolded_config():
    tmpl_path = ROOT / "skills" / "sigma-init" / "templates" / "config.json.tmpl"
    tmpl = tmpl_path.read_text(encoding="utf-8")
    cfg = json.loads(tmpl)          # also proves the template is still valid JSON with the new key
    assert cfg.get("goal_design") == {"enabled": False, "mode": "full"}
    assert "_goal_design" in cfg
    explainer = cfg["_goal_design"]
    assert "idempotency-guarded" in explainer and "sigma:design-of" not in explainer
    assert "full" in explainer and "lane" in explainer
    assert "RETROFITS THE ENTIRE LIVE BACKLOG" in explainer


def test_scaffolded_default_config_is_off_end_to_end(tmp_path):
    lp = _mod("loop")
    tmpl_path = ROOT / "skills" / "sigma-init" / "templates" / "config.json.tmpl"
    tmpl_cfg = json.loads(tmpl_path.read_text(encoding="utf-8"))
    base = _sdlc(tmp_path, {"goal_design": tmpl_cfg["goal_design"]})
    src = _FakeSource(body=_PLAIN_BODY)
    assert lp.design_check(base, "1", _cfg(base), src) == "OFF"
    assert src.calls == []


def test_skill_documents_design_check_and_the_new_skill():
    skill = skill_corpus("sigma-loop")   # #1611: SKILL.md + references/*.md
    assert "design-check" in skill
    assert "sigma-goal-design" in skill


def test_sdlc_goal_design_skill_exists_and_documents_the_handoff():
    skill_path = ROOT / "skills" / "sigma-goal-design" / "SKILL.md"
    assert skill_path.is_file()
    text = skill_path.read_text(encoding="utf-8")
    assert "sdlc:designed" in text
    assert "goal-review" in text
    assert ".sdlc/design/" in text
