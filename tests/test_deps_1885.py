"""#1885: `deps.py` -- the whole-ticket dependency read and the marker writer.

`analyse` is pure and classifies every `#N` by what the machinery already does with it; `mark` writes
the canonical marker through the source's own guarded `append_to_body`. Controls: each `read_as` row
goes red if its branch is removed, and `mark` is seen to write nothing on ALREADY and REFUSED.
"""
import importlib.util, pathlib, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


deps = _mod("deps")


def _by_ref(refs):
    return {r["ref"]: r for r in refs}


def test_every_reference_is_classified_by_what_already_reads_it():
    refs = _by_ref(deps.analyse(
        "5", "Fix the cap", "Follow-up to #12. This also touches the code in #13.\n"
        "The rollout needs #14 looked at.",
        ["Blocked by #15", "see #16, and #5 is this issue", "acme/other#17 is elsewhere"]))
    assert {k: v["read_as"] for k, v in refs.items()} == {
        "12": "edge", "13": "mention", "14": "weak", "15": "comment-edge", "16": "mention"}
    assert refs["15"]["where"] == ["comment"]


def test_an_edge_in_the_body_outranks_a_mention_elsewhere():
    refs = _by_ref(deps.analyse("5", "t", "depends on #12", ["#12 is going well"]))
    assert refs["12"]["read_as"] == "edge" and refs["12"]["where"] == ["body", "comment"]


def test_an_unpark_answer_block_is_not_read():
    qa = deps.blocker_scan.UNPARK_QA_START + " waiting on #12 " + deps.blocker_scan.UNPARK_QA_END
    assert deps.analyse("5", "t", "scope.\n" + qa, []) == []


def test_snippets_are_bounded_and_scrubbed():
    body = "x " * 400 + "Follow-up to #12 " + "y " * 400
    snip = deps.analyse("5", "t", body, [])[0]["snippets"][0]
    assert "#12" in snip and len(snip) <= deps.SNIPPET_CHARS + 10


class _Source:
    def __init__(self, body, refuse=None):
        self.body, self.refuse, self.appended, self.notes = body, refuse, [], []

    states = {}

    def _read_issue(self, goal, fields, comment_limit=None):
        state = self.states.get(str(goal), "OPEN")
        if state == "ERR":
            raise RuntimeError("unreadable")
        return {"title": "t", "body": self.body, "state": state, "comments": []}

    def append_to_body(self, goal, marker):
        if self.refuse:
            raise ValueError(self.refuse)
        self.appended.append(marker)

    def note(self, goal, text):
        self.notes.append(text)


def test_mark_writes_the_canonical_marker_and_a_note_that_mints_no_edge():
    src = _Source("This can only start once the zone work in #12 is merged.")
    assert deps.mark(src, "5", "12", "the body says the zone work in #12 must merge first")[0] == "MARKED"
    assert src.appended == ["**Blocked by:** #12"]
    assert "#" not in src.notes[0]
    assert not deps.blocker_scan._BLOCK_RE.search(src.notes[0])


def test_mark_is_idempotent_on_an_existing_body_edge():
    src = _Source("Follow-up to #12.")
    assert deps.mark(src, "5", "12", "")[0] == "ALREADY"
    assert src.appended == [] and src.notes == []


def test_mark_refuses_a_cycle_and_a_self_reference_and_writes_nothing():
    src = _Source("text", refuse="would close a cycle: #5, #12")
    assert deps.mark(src, "5", "12", "")[0] == "REFUSED"
    assert deps.mark(src, "5", "5", "")[0] == "REFUSED"
    assert deps.mark(src, "5", "abc", "")[0] == "REFUSED"
    assert src.appended == [] and src.notes == []


def test_a_mention_in_the_body_is_upgraded_by_an_edge_in_a_comment():
    refs = _by_ref(deps.analyse("5", "t", "this touches #12", ["Blocked by #12"]))
    assert refs["12"]["read_as"] == "comment-edge"


def test_mark_refuses_a_closed_or_unreadable_reference_and_writes_nothing():
    for state in ("CLOSED", "ERR"):
        src = _Source("text")
        src.states = {"12": state}
        assert deps.mark(src, "5", "12", "")[0] == "REFUSED", state
        assert src.appended == [] and src.notes == []


def test_help_prints_usage_and_exits_zero(capsys):
    assert deps.main(["deps.py", "--help"]) == 0
    assert "usage" in capsys.readouterr().out


# -- #1984: mark never writes half a gesture on an issue Sigma did not create or label -------------------

_HS_OPEN = {"work": {"enabled": True}, "rulebook": {"hard_stops": {"enabled": True}}}


class _Guarded(_Source):
    """A fake whose issue reads carry labels, so the hard-stop guard can tell Sigma's issue from a foreign one."""
    def __init__(self, body, labels=(), cfg=_HS_OPEN):
        super().__init__(body)
        self._hs_config, self.labels, self.writes = cfg, list(labels), []
        self.goal_label = "sdlc:goal"

    def _read_issue(self, goal, fields, comment_limit=None):
        data = super()._read_issue(goal, fields, comment_limit)
        data["labels"] = [{"name": n} for n in self.labels]
        return data

    def append_to_body(self, goal, marker):
        self.writes.append("body")
        super().append_to_body(goal, marker)

    def note(self, goal, text):
        self.writes.append("note")
        super().note(goal, text)

    def park(self, goal, reason, tier=None):
        self.writes.append("park")


def test_mark_on_a_foreign_issue_is_refused_and_writes_nothing():
    src = _Guarded("A hand-filed ticket.")
    verdict, reason = deps.mark(src, "5", "12", "why")
    assert verdict == "REFUSED" and src.writes == [] and src.appended == [] and src.notes == []
    assert "by hand" in reason and "label" in reason


def test_mark_on_a_sigma_labelled_issue_still_writes_the_line_and_the_note():
    src = _Guarded("A ticket.", labels=["sdlc:goal"])
    assert deps.mark(src, "5", "12", "why")[0] == "MARKED"
    assert src.appended == ["**Blocked by:** #12"] and len(src.notes) == 1 and "park" not in src.writes


def test_mark_with_hard_stops_off_is_unchanged_on_an_unlabelled_issue():
    src = _Guarded("A ticket.", cfg={"work": {"enabled": True}})
    assert deps.mark(src, "5", "12", "why")[0] == "MARKED"


def test_append_to_body_on_a_foreign_issue_writes_nothing():
    sources = _mod("sources")
    s = _Guarded("A hand-filed ticket.")
    s._dag_blockers = lambda goal, marker: (None, [])
    s._issue_edit_body = lambda goal, body: s.writes.append("edit")
    import pytest
    with pytest.raises(ValueError, match="hard stop"):
        sources.GitHubSource.append_to_body(s, "5", "**Blocked by:** #12")
    assert s.writes == []          # no edit and no park: a refused write raises, it is never a silent skip
    own = _Guarded("t", labels=["sdlc:goal"])
    own._dag_blockers = lambda goal, marker: (None, [])
    own._issue_edit_body = lambda goal, body: own.writes.append("edit")
    sources.GitHubSource.append_to_body(own, "5", "x")
    assert own.writes == ["edit"]


# -- #1989: unmark takes a canonical `Blocked by` line back ---------------------------------------------
# Controls (each broken once, seen red): drop the foreign pre-check; remove prose lines too; skip the
# map removal (test_dag_insert_1989.py); remove a different ref's line (ref 12 vs 123).

class _Unmark(_Guarded):
    def __init__(self, body, labels=("sdlc:goal",), cfg=_HS_OPEN):
        super().__init__(body, labels, cfg)
        self.removed = []

    def remove_from_body(self, goal, ref):
        self.writes.append("remove")
        self.removed.append(ref)
        return 1


def test_unmark_removes_the_line_and_posts_a_note_with_the_reason():
    src = _Unmark("A ticket.\n\n**Blocked by:** #12\n")
    verdict, _detail = deps.unmark(src, "5", "12", "it was a two-way wait")
    assert verdict == "UNMARKED" and src.removed == ["12"]
    assert len(src.notes) == 1 and "two-way wait" in src.notes[0] and "#" not in src.notes[0]


def test_unmark_with_no_such_line_is_absent_and_writes_nothing():
    src = _Unmark("A ticket.\n\n**Blocked by:** #13\n")
    assert deps.unmark(src, "5", "12", "why")[0] == "ABSENT"
    assert src.writes == [] and src.notes == []


def test_unmark_does_not_confuse_a_longer_number_with_the_ref():
    src = _Unmark("A ticket.\n\n**Blocked by:** #123\n")
    assert deps.unmark(src, "5", "12", "why")[0] == "ABSENT" and src.writes == []


def test_unmark_reports_a_prose_edge_and_leaves_it_alone():
    src = _Unmark("This is blocked by #12 until the zone work lands.")
    verdict, detail = deps.unmark(src, "5", "12", "why")
    assert verdict == "ABSENT" and "prose" in detail and src.writes == [] and src.notes == []


def test_unmark_on_a_foreign_issue_is_refused_and_writes_nothing():
    src = _Unmark("A hand-filed ticket.\n\n**Blocked by:** #12\n", labels=())
    verdict, reason = deps.unmark(src, "5", "12", "why")
    assert verdict == "REFUSED" and src.writes == [] and src.notes == []
    assert "by hand" in reason and "label" in reason


def test_unmark_refuses_a_non_number_and_the_goal_itself():
    src = _Unmark("x\n\n**Blocked by:** #5\n")
    assert deps.unmark(src, "5", "x", "why")[0] == "REFUSED"
    assert deps.unmark(src, "5", "5", "why")[0] == "REFUSED" and src.writes == []


def test_unmark_failed_note_still_reports_unmarked():
    src = _Unmark("A ticket.\n\n**Blocked by:** #12\n")
    src.note = lambda goal, text: (_ for _ in ()).throw(RuntimeError("x"))
    verdict, detail = deps.unmark(src, "5", "12", "why")
    assert verdict == "UNMARKED" and "note failed" in detail


class _Gh(_Guarded):
    """Real `GitHubSource.remove_from_body` over a fake that records the body it would write."""
    def __init__(self, body, labels=("sdlc:goal",)):
        super().__init__(body, labels)
        self.sdlc_dir, self.edits = None, []

    def _issue_edit_body(self, goal, body):
        self.edits.append(body)


def _remove(body, ref="12", labels=("sdlc:goal",)):
    sources = _mod("sources")
    s = _Gh(body, labels)
    n = sources.GitHubSource.remove_from_body(s, "5", ref)
    return n, s


def test_remove_from_body_drops_only_the_line_and_the_blank_mark_added():
    n, s = _remove("A ticket.\n\nmore text\n\n**Blocked by:** #12\n")
    assert n == 1 and s.edits == ["A ticket.\n\nmore text\n"]


def test_remove_from_body_keeps_a_line_that_is_not_exactly_the_marker():
    body = "see **Blocked by:** #12 in prose\n**Blocked by:** #123\n**Blocked by:** #12 and more\n"
    n, s = _remove(body)
    assert n == 0 and s.edits == []


def test_remove_from_body_removes_every_identical_line():
    n, s = _remove("A.\n\n**Blocked by:** #12\n\n**Blocked by:** #12\n")
    assert n == 2 and s.edits == ["A.\n"]


def test_remove_from_body_on_a_foreign_issue_raises_and_writes_nothing():
    import pytest
    sources = _mod("sources")
    s = _Gh("A.\n\n**Blocked by:** #12\n", labels=())
    with pytest.raises(ValueError, match="hard stop"):
        sources.GitHubSource.remove_from_body(s, "5", "12")
    assert s.edits == [] and "park" not in s.writes


def test_unmark_cli_usage_names_unmark(capsys):
    assert deps.main(["deps.py", "--help"]) == 0
    assert "unmark" in capsys.readouterr().out


def test_remove_from_body_drops_the_stored_map_edge(tmp_path):
    import test_dag_insert_1616 as t
    sources = _mod("sources")
    sdlc = tmp_path / ".sdlc"
    t._store(sdlc, [t._node(5), t._node(12)], [t._edge(12, 5)])
    s = _Gh("A.\n\n**Blocked by:** #12\n")
    s.sdlc_dir = str(sdlc)
    s._dag_mod = sources.GitHubSource._dag_mod.__get__(s)
    assert sources.GitHubSource.remove_from_body(s, "5", "12") == 1
    assert t._edges(sdlc) == set()
