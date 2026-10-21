"""#1990: `deps.report` shows a two-way wait (`reverse`) and a quoted marker (`quoted`).

Controls (each seen red on its own): drop the read bound; flag reverse on a plain mention; drop the
own-marker-line exemption; drop the subject check (quote every body edge).
"""
import importlib.util, pathlib

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


deps = _mod("deps")


class _Source:
    def __init__(self, issues, bad=()):
        self.issues, self.bad, self.reads = issues, set(bad), []

    def _read_issue(self, goal, fields, comment_limit=None):
        self.reads.append(str(goal))
        if str(goal) in self.bad:
            raise RuntimeError("unreadable")
        body, comments = self.issues.get(str(goal), ("", []))
        return {"title": "fixture issue %s" % goal, "body": body, "state": "OPEN",
                "comments": [{"body": c} for c in comments]}


def _refs(out):
    return {r["ref"]: r for r in out["refs"]}


def test_a_two_way_pair_is_flagged_on_both_sides():
    src = _Source({"8": ("Blocked by: #9", []), "9": ("", ["the later stage starts only once #8 is closed"])})
    assert _refs(deps.report(src, "8"))["9"]["reverse"] is True
    assert _refs(deps.report(src, "9"))["8"]["reverse"] is True


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
