"""park_mix.py (#991, decision-rubric slice 1): the read-only park-mix reader and census, plus
characterisation pins of today's filing label sets so later slices can show what they changed."""
import importlib.util
import json
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parent.parent
LOOP = _ROOT / "skills" / "sigma-loop" / "scripts"
SCOPE = _ROOT / "skills" / "sigma-scope" / "scripts"


def _mod(name, where):
    spec = importlib.util.spec_from_file_location(name, where / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


park_mix = _mod("park_mix", LOOP)
handoff = _mod("handoff", LOOP)
sources = _mod("sources", LOOP)
loop = _mod("loop", LOOP)
compile_plan = _mod("compile_plan", SCOPE)

PREFIX = sources.PARK_COMMENT_PREFIX


def _seed_events(sdlc, rows):
    d = sdlc / "events"
    d.mkdir(parents=True)
    (d / "a.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")


def test_mix_counts_events_by_reason_class(tmp_path):
    sdlc = tmp_path / ".sdlc"
    _seed_events(sdlc, [
        {"kind": "park", "reason_class": "dependency"},
        {"kind": "park", "reason_class": "dependency"},
        {"kind": "park", "reason_class": "quota"},
        {"kind": "phase", "phase": "plan"},
    ])
    assert park_mix.mix_from_events(park_mix.read_events(sdlc)) == {"dependency": 2, "quota": 1}


def test_mix_reads_comments_when_ledger_off():
    texts = ["waiting on a rate limit reset", "irreversible schema drop", "needs a decision"]
    comments = [PREFIX + t for t in texts] + ["an ordinary human comment"]
    comments.append({"body": PREFIX + texts[0]})
    expect = {}
    for t in texts + [texts[0]]:
        c = loop._reason_class(t)
        expect[c] = expect.get(c, 0) + 1
    assert park_mix.mix_from_comments(comments) == expect


def test_census_separates_gate_held_human():
    cfg = {}
    nc = handoff.proposed_label(cfg)
    issues = [
        {"number": 1, "labels": [nc, handoff.FOLLOWUP_LABEL], "body": "x"},
        {"number": 2, "labels": [nc, handoff.FOLLOWUP_LABEL, "feature:v"],
         "comments": [{"body": "<!-- sigma:feature-scope-expansion --> held"}]},
        {"number": 3, "labels": [nc, "feature:v"],
         "comments": ["<!-- sigma:feature-ownership --> held"]},
        {"number": 4, "labels": [nc], "body": "typed by a person"},
        {"number": 5, "labels": [nc, "priority:P2"], "body": "plan"},
        {"number": 6, "labels": ["bug"], "body": "not a proposal"},
    ]
    assert park_mix.census(issues, cfg) == {
        "followup_label": 1, "scope_marker": 1, "ownership_flag": 1, "human_typed": 1, "other": 1}


def test_report_writes_nothing(tmp_path, capsys):
    sdlc = tmp_path / ".sdlc"
    _seed_events(sdlc, [{"kind": "park", "reason_class": "quota"}])
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert park_mix.main(["report", str(sdlc)]) == 0
    assert json.loads(capsys.readouterr().out)["events"] == {"quota": 1}
    after = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert before == after


class _Src:
    def __init__(self):
        self.created = []

    def create_dependency(self, title, body, assignee, labels=(), goal_label=True):
        self.created.append({"labels": list(labels), "goal_label": goal_label})
        return str(100 + len(self.created))


def test_compile_plan_label_set_is_pinned():
    plan = {"issues": [{"key": "a", "title": "t", "body": "b", "priority": "P1"}]}
    src = _Src()
    compile_plan.compile_plan("/x/.sdlc", {}, plan, source=src)
    assert src.created == [{"labels": ["priority:P1", "sdlc:needs-confirmation"], "goal_label": False}]
    src = _Src()
    compile_plan.compile_plan("/x/.sdlc", {}, plan, source=src, goal_label=True)
    assert src.created == [{"labels": ["priority:P1"], "goal_label": True}]


def _file(tmp_path, **flags):
    class Src(_Src):
        def issue_url(self, goal):
            return "u"

        def note(self, *a, **k):
            pass

        def append_to_body(self, *a, **k):
            pass

        def mark_blocked(self, *a, **k):
            pass

    src = Src()
    handoff.create_tracked_issue(str(tmp_path), {}, "42", "api", "a follow-up", same_area=True,
                                 blocks_goal=False, source=src, **flags)
    return src.created[0]


def test_upkeep_filing_label_set_is_pinned(tmp_path):
    # feature_rebase._file_issue calls create_tracked_issue with exactly these axes.
    got = _file(tmp_path, immediately_actionable=False)
    assert got["goal_label"] is False
    assert "sdlc:needs-confirmation" in got["labels"] and "sdlc:followup" in got["labels"]


def test_actionable_label_set_is_pinned(tmp_path):
    got = _file(tmp_path, immediately_actionable=True)
    assert got["goal_label"] is True
    assert "sdlc:needs-confirmation" not in got["labels"] and "sdlc:followup" in got["labels"]
