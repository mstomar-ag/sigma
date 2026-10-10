"""Decision rubric slice 16: the two pick gates park instead of writing the confirmation label.

With `ai_filed.triage.enabled` not exactly false (the default) a held goal is parked with the declared
kind scope_hold or owner_hold; `/sigma-unpark` lists it, asks about it and refuses to release it while
the registry still holds it; the automatic sweep leaves it alone. With triage exactly false the old
label write is untouched (the pins in test_feature_owner and test_feature_propagate)."""
import importlib.util
import json
import pathlib

import test_feature_owner as fo
import test_feature_propagate as fp
import test_promote as tp
import test_unpark as tu
import test_auto_unpark as ta

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"
OPEN = {k: v for k, v in fo.CONFIG.items() if k != "ai_filed"}
CLOSED = dict(OPEN, ai_filed={"triage": {"enabled": False}})


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class _ParkSource:
    """The gate surface with a park and NO confirmation-label method."""

    def __init__(self, author="nobody"):
        self.author, self.parked, self.notes = author, [], []

    def fetch_author(self, goal):
        return self.author

    def park(self, goal, reason, tier=None):
        self.parked.append((str(goal), reason))

    def note(self, goal, text):
        self.notes.append((str(goal), text))

    def fetch_comments_strict(self, goal):
        return {"comments": [{"body": b} for _g, b in self.notes]}


class _BothSource(_ParkSource):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.marked = []

    def mark_needs_confirmation(self, goal):
        self.marked.append(str(goal))
        return True


def test_scope_gate_parks_with_declared_kind(tmp_path):
    sdlc, src = fp._unlisted(tmp_path), _ParkSource()
    gate = fp._mod().gate_at_pick(str(sdlc), src, "2879", fp.CONFIG, fp.UNIT, cwd=str(tmp_path))
    assert gate.proceed is False and gate.marked is True
    assert gate.outcome == fp._mod().EXPANSION
    assert len(src.parked) == 1 and src.parked[0][0] == "2879"
    assert "sigma-qkind: scope_hold" in src.parked[0][1]
    assert fp._mod().SCOPE_MARKER in src.notes[0][1]


def test_owner_gate_parks_with_declared_kind(tmp_path):
    sdlc, src = fo._sdlc(tmp_path), _ParkSource(author="nobody")
    fo._seed(sdlc)
    gate = fo._mod().gate_at_pick(str(sdlc), src, "2879", OPEN, fo.UNIT, cwd=str(tmp_path))
    assert gate.proceed is False and gate.marked is True
    assert gate.outcome == fo._mod().NOT_AN_OWNER
    assert len(src.parked) == 1 and "sigma-qkind: owner_hold" in src.parked[0][1]
    assert fo._mod().OWNER_MARKER in src.notes[0][1]


def test_a_closed_gate_still_writes_the_label_and_never_parks(tmp_path):
    sdlc, src = fo._sdlc(tmp_path), _BothSource(author="nobody")
    fo._seed(sdlc)
    gate = fo._mod().gate_at_pick(str(sdlc), src, "2879", CLOSED, fo.UNIT, cwd=str(tmp_path))
    assert gate.proceed is False and src.marked == ["2879"] and src.parked == []
    sdlc2, src2 = fp._unlisted(tmp_path / "b"), _BothSource()
    fp._mod().gate_at_pick(str(sdlc2), src2, "2879", dict(fp.CONFIG, **CLOSED), fp.UNIT,
                           cwd=str(tmp_path))
    assert src2.marked == ["2879"] and src2.parked == []


def test_a_source_with_only_the_label_write_keeps_it_when_open(tmp_path):
    """Never silently nothing: no park method means the old write, whatever the config says."""
    sdlc, src = fp._unlisted(tmp_path), fp._Source()
    gate = fp._mod().gate_at_pick(str(sdlc), src, "2879", fp.CONFIG, fp.UNIT, cwd=str(tmp_path))
    assert gate.proceed is False and src.marked == ["2879"]


def test_gate_surface_probe_finds_park(tmp_path):
    """The probe must accept a source that can park and cannot write the label; otherwise removing
    the label method makes the gate fail OPEN (proceed True)."""
    src = _ParkSource()
    assert fp._mod()._has_surface(src, fp.CONFIG) is True
    assert fo._mod()._has_surface(src, OPEN) is True
    assert fp._mod()._has_surface(object(), fp.CONFIG) is False
    sdlc = fp._unlisted(tmp_path)
    gate = fp._mod().gate_at_pick(str(sdlc), src, "2879", fp.CONFIG, fp.UNIT, cwd=str(tmp_path))
    assert gate.proceed is False


def test_no_parked_plus_label_pair():
    """Against the real source: the park is ONE swap that adds the parked label and removes the
    membership labels, and nothing adds the confirmation label."""
    sources = _mod("sources")
    gh = sources.GitHubSource({"discovery": {"source": "github", "github": {"repo": "o/r"}}},
                              run=lambda args: "")
    swaps = []
    gh._ensure_labels = lambda: None
    gh._swap_labels_best_effort = lambda goal, add=(), remove=(), what="": (
        swaps.append((sorted(add), sorted(remove))) or True)
    gh._set_board_status = lambda *a, **k: True
    gh.note = lambda goal, text: None
    assert _mod("gate_hold").park_for_gate(gh, "42", "scope_hold", "outside the unit") is True
    assert swaps == [(["sdlc:parked"], sorted(["sdlc:goal", "sdlc:in-progress", "sdlc:blocked"]))]
    assert all("sdlc:needs-confirmation" not in a + r for a, r in swaps)


def test_park_for_gate_refuses_a_non_gate_kind_and_reports_a_failed_write():
    g = _mod("gate_hold")
    try:
        g.park_for_gate(_ParkSource(), "1", "needs_decision", "x")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass

    class Boom(_ParkSource):
        def park(self, goal, reason, tier=None):
            raise RuntimeError("down")
    assert g.park_for_gate(Boom(), "1", "owner_hold", "x") is False


def test_park_enabled_is_off_only_for_the_exact_false():
    g = _mod("gate_hold")
    assert g.park_enabled({}) and g.park_enabled(None) and g.park_enabled({"ai_filed": 3})
    assert g.park_enabled({"ai_filed": {"triage": {"enabled": "false"}}})
    assert not g.park_enabled({"ai_filed": {"triage": {"enabled": False}}})


# ---------------------------------------------------------------- unpark: lists, asks, guards

PARK_TEXT = "outside the unit\n" + "sigma-qkind: owner_hold"


def _gate_view(labels=("sdlc:parked", tp.LABEL), kind="owner_hold", author="a-stranger"):
    comments = [tu._park("held by a gate\nsigma-qkind: %s" % kind)] if kind else [tu._park("x")]
    return json.dumps({"number": 5, "title": "g", "body": "", "state": "OPEN",
                       "labels": [{"name": n} for n in labels],
                       "author": {"login": author},
                       "comments": [{"body": c} for c in comments]})


def test_unpark_lists_gate_holds():
    u = _mod("unpark")
    issue = {"number": 5, "title": "g", "body": "",
             "labels": [{"name": "sdlc:parked"}, {"name": tp.LABEL}]}
    run = tu._runner(by_label={"sdlc:parked": json.dumps([issue])})
    rows = u.list_parked(".sdlc", tu._config(), run=run)["issues"]
    assert [r["number"] for r in rows] == ["5"] and rows[0]["state"] == "sdlc:parked"


def test_the_brief_asks_the_gate_hold_questions():
    u = _mod("unpark")
    for kind in ("scope_hold", "owner_hold"):
        run = tu._runner(views={"5": _gate_view(kind=kind)})
        b = u.brief(".sdlc", tu._config(), 5, run=run)
        assert b["reason_class"] == kind
        ids = [q["id"] for q in b["questions"]]
        assert ids[-1] == "resume" and len(ids) >= 2 and kind in u.QUESTIONS


def test_unpark_refuses_an_unfixed_gate_hold(tmp_path):
    """The wasted-pick loop: unparking without the registry edit returns the goal to the board for
    exactly one pick, which sets it aside again."""
    u = _mod("unpark")
    run = tu._runner(views={"5": _gate_view()})
    r = u.resolve(tp._sdlc(tmp_path), tu._config(), 5, {"resume": "start it again"}, "unpark", run=run)
    assert r["outcome"] == "failed" and "unit ownership still holds it" in r["detail"]
    assert not any(str(a).startswith("query=mutation") for c in run.gql_calls for a in c)


def test_unpark_proceeds_once_the_registry_allows_it(tmp_path):
    u = _mod("unpark")
    run = tu._runner(views={"5": _gate_view()})
    sdlc = tp._sdlc(tmp_path, entry=tp._entry(authorized=True))
    r = u.resolve(sdlc, tu._config(), 5, {"resume": "start it again"}, "unpark", run=run)
    assert r["outcome"] == "unparked"


def test_unpark_of_an_ordinary_park_is_untouched_and_keep_parked_is_never_refused(tmp_path):
    u = _mod("unpark")
    run = tu._runner(views={"5": _gate_view(kind=None)})
    assert u.resolve(tp._sdlc(tmp_path), tu._config(), 5, {"resume": "x"}, "unpark",
                     run=run)["outcome"] == "unparked"
    run2 = tu._runner(views={"5": _gate_view()})
    assert u.resolve(tp._sdlc(tmp_path / "b"), tu._config(), 5, {"resume": "leave it parked"},
                     "keep-parked", run=run2)["outcome"] == "kept-parked"


# ---------------------------------------------------------------- the sweep

def test_auto_unpark_skips_gate_holds():
    au = _mod("auto_unpark")
    issues = [ta._blocked_issue(42, body="blocked by #7 until the base lands")]
    inner = ta._label_aware_sweep_runner(
        states={"7": "CLOSED"},
        comments={"42": ["held by a gate\nsigma-qkind: scope_hold"]})

    def run(args):
        out = inner(args)
        if "/comments" in " ".join(str(a) for a in args) and out.startswith("["):
            out = json.dumps([dict(c, author_association="OWNER") for c in json.loads(out)])
        return out
    source = au.sources.GitHubSource(ta._config(), run=run)
    actions, resolved = au.compute_unpark_actions(".sdlc", ta._config(), source, issues, run=run)
    assert actions == [] and resolved == {}
    plain = ta._label_aware_sweep_runner(states={"7": "CLOSED"})
    source2 = au.sources.GitHubSource(ta._config(), run=plain)
    actions2, _ = au.compute_unpark_actions(".sdlc", ta._config(), source2, issues, run=plain)
    assert actions2, "control: the same issue without a declared gate kind is still resumed"
