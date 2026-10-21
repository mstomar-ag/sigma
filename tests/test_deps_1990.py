"""#1990: `deps.report` shows a two-way wait (`reverse`) and a quoted marker (`quoted`).

Controls (each seen red on its own): drop the read bound; flag reverse on a plain mention; drop the
own-marker-line exemption; drop the subject check (quote every body edge).

#1995 (direction): a weak ref whose own text says it waits on the goal is `likely-dependent`, never
`reverse`; a closed ref is not reverse-read and is counted; `prerequisite` is a reverse-only phrase.
Controls: drop the weak->likely branch; drop the closed skip; drop the prerequisite set; drop the
direction check (flag reverse on a mention-only reading).
"""
import importlib.util, json, pathlib

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


deps = _mod("deps")
blocker_scan = _mod("blocker_scan")


class _Source:
    def __init__(self, issues, bad=(), closed=()):
        self.issues, self.bad, self.reads, self.closed = issues, set(bad), [], set(closed)

    def _read_issue(self, goal, fields, comment_limit=None):
        self.reads.append(str(goal))
        if str(goal) in self.bad:
            raise RuntimeError("unreadable")
        body, comments = self.issues.get(str(goal), ("", []))
        return {"title": "fixture issue %s" % goal, "body": body, "state": "CLOSED" if str(goal) in self.closed else "OPEN",
                "comments": [{"body": c} for c in comments]}


def _refs(out):
    return {r["ref"]: r for r in out["refs"]}


def test_a_two_way_pair_is_flagged_on_both_sides():
    src = _Source({"8": ("Blocked by: #9", []), "9": ("", ["the later stage starts only once #8 is closed"])})
    assert _refs(deps.report(src, "8"))["9"]["reverse"] is True
    # #1995: from 9's side the ref 8 is only a weak read, so the same text is `likely-dependent`, not `reverse`.
    other = _refs(deps.report(src, "9"))["8"]
    assert other["likely-dependent"] is True and "reverse" not in other


def test_a_one_way_wait_is_not_flagged_and_a_plain_mention_does_not_count():
    src = _Source({"8": ("Blocked by: #9", []), "9": ("see also #8 for context", [])})
    assert _refs(deps.report(src, "8"))["9"]["reverse"] is False


def test_weak_phrasing_on_the_other_side_counts():
    src = _Source({"8": ("Blocked by: #9", []), "9": ("this needs #8 first", [])})
    assert _refs(deps.report(src, "8"))["9"]["reverse"] is True


def test_the_extra_reads_are_bounded_and_the_rest_listed_unchecked():
    body = " ".join("Blocked by: #%d" % n for n in range(20, 35))
    src = _Source({"8": (body, [])})
    out = deps.report(src, "8")
    assert src.reads == ["8"] + [str(n) for n in range(20, 30)]
    assert out["reverse_reads"] == deps.REVERSE_READ_MAX == 10
    assert out["reverse_unchecked"] == [str(n) for n in range(30, 35)]
    assert "reverse" not in _refs(out)["34"] and "reverse" in _refs(out)["20"]


def test_a_mention_only_ref_is_never_read():
    src = _Source({"8": ("see #9", [])})
    out = deps.report(src, "8")
    assert src.reads == ["8"] and out["reverse_reads"] == 0 and out["reverse_unchecked"] == []


def test_an_unreadable_ref_is_null_and_the_report_still_returns():
    src = _Source({"8": ("Blocked by: #9 and Blocked by: #10", []), "10": ("needs #8", [])}, bad=["9"])
    r = _refs(deps.report(src, "8"))
    assert r["9"]["reverse"] is None and r["10"]["reverse"] is True


def test_an_epic_sentence_quoting_its_children_marker_is_quoted():
    body = "Slices S2 and S3 carry `Blocked by: #12`, so the epic itself waits on nothing."
    assert _refs({"refs": deps.analyse("5", "t", body, [])})["12"]["quoted"] is True
    body = "The first slice carries Blocked by: #12 in its body."
    assert deps.analyse("5", "t", body, [])[0]["quoted"] is True
    body = "Part #7 carries Blocked by: #12."
    assert deps.analyse("5", "t", body, [])[0]["quoted"] is True


def test_the_goals_own_marker_line_and_plain_prose_are_not_quoted():
    own = deps.analyse("5", "t", "Scope.\nS2 carries the rest.\n**Blocked by:** #12", [])[0]
    assert own["quoted"] is False
    plain = deps.analyse("5", "t", "This is Blocked by #12 until then.", [])[0]
    assert plain["quoted"] is False


def test_a_previous_sentence_naming_a_slice_does_not_quote_the_next():
    r = deps.analyse("5", "t", "S2 carries the parser. Blocked by: #12", [])[0]
    assert r["quoted"] is False


def test_only_body_edges_carry_the_key():
    refs = _refs({"refs": deps.analyse("5", "t", "touches #13, needs #14", ["Blocked by #15"])})
    assert all("quoted" not in refs[k] for k in ("13", "14", "15"))


# ---- #1995 ----------------------------------------------------------------------------------------

_DEPENDENT_PHRASINGS = [
    "Dependent: needs #9 (blocked on it)",
    "the gate named by: requires #9",
    "this directly unlocks, after #9",
    "Dependent (waiting on #9): blocked on it",
]


def test_a_weak_ref_that_says_it_waits_on_the_goal_is_likely_dependent_not_reverse():
    for text in _DEPENDENT_PHRASINGS:
        src = _Source({"8": (text, []), "9": ("Blocked by: #8", [])})
        r = _refs(deps.report(src, "8"))["9"]
        assert r["read_as"] == "weak", text
        assert r["likely-dependent"] is True and "reverse" not in r, text


def test_a_weak_ref_that_does_not_wait_back_has_neither_flag_value():
    src = _Source({"8": ("needs #9", []), "9": ("see also #8", [])})
    r = _refs(deps.report(src, "8"))["9"]
    assert not r.get("likely-dependent") and r["reverse"] is False


def test_an_edge_ref_keeps_reverse_true_and_no_likely_dependent():
    src = _Source({"8": ("Blocked by: #9", []), "9": ("Blocked by: #8", [])})
    r = _refs(deps.report(src, "8"))["9"]
    assert r["reverse"] is True and "likely-dependent" not in r


def test_a_real_two_way_wait_worded_as_a_prerequisite_is_reverse():
    src = _Source({"8": ("Blocked by: #9", []), "9": ("#8 stays a stage-2 prerequisite", [])})
    assert _refs(deps.report(src, "8"))["9"]["reverse"] is True
    src = _Source({"8": ("Blocked by: #9", []), "9": ("Prerequisite: #8", [])})
    assert _refs(deps.report(src, "8"))["9"]["reverse"] is True
    src = _Source({"8": ("Blocked by: #9", []), "9": ("a comment-free body with a prerequisite for #8 nowhere", [])})
    assert _refs(deps.report(src, "8"))["9"]["reverse"] is False


def test_a_prerequisite_of_a_third_issue_is_not_a_wait_on_the_goal():
    src = _Source({"8": ("Blocked by: #9", []), "9": ("#8 is a prerequisite of #10", [])})
    assert _refs(deps.report(src, "8"))["9"]["reverse"] is False


def test_a_closed_ref_is_not_reverse_read_and_is_counted():
    src = _Source({"8": ("Blocked by: #9 and Blocked by: #10", []), "9": ("Blocked by: #8", []),
                   "10": ("Blocked by: #8", [])}, closed=["9"])
    out = deps.report(src, "8")
    r = _refs(out)
    assert "reverse" not in r["9"] and r["10"]["reverse"] is True
    assert out["reverse_skipped_closed"] == 1


def test_the_sheet_says_likely_dependent_in_words_and_prints_the_closed_count():
    src = _Source({"8": ("needs #9. Blocked by: #10", []), "9": ("Blocked by: #8", []), "10": ("", [])}, closed=["10"])
    text = "\n".join(deps.sheet_lines(deps.report(src, "8")))
    assert "probably waits on this goal; the weak read is likely the wrong way round" in text
    assert "reverse_skipped_closed" not in text and "1 closed ref(s) not reverse-read" in text


def test_the_edge_parser_output_is_unchanged_by_the_reverse_only_phrases():
    # Pinned: `analyse` (the forward parse) never reads `prerequisite` as a wait, and the phrase tables
    # blocker_scan owns do not mention it. The reverse-only set lives in deps.py alone.
    body = "#8 stays a stage-2 prerequisite. Prerequisite: #12. Blocked by: #13. needs #14"
    got = deps.analyse("5", "t", body, ["prerequisites #15"])
    assert json.dumps(got, sort_keys=True) == json.dumps([
        {"ref": "13", "read_as": "edge", "where": ["body"], "snippets": [got[0]["snippets"][0]], "quoted": False},
        {"ref": "14", "read_as": "weak", "where": ["body"], "snippets": [got[1]["snippets"][0]]},
        {"ref": "8", "read_as": "mention", "where": ["body"], "snippets": [got[2]["snippets"][0]]},
        {"ref": "12", "read_as": "mention", "where": ["body"], "snippets": [got[3]["snippets"][0]]},
        {"ref": "15", "read_as": "mention", "where": ["comment"], "snippets": [got[4]["snippets"][0]]}],
        sort_keys=True)
    for table in (blocker_scan.TRIGGERS, blocker_scan.EXPLICIT_TRIGGERS):
        assert "prerequisite" not in repr(table).lower()
