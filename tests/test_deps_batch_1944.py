"""#1944: `deps.py report --label L` / `--chain N` reads many goals in ONE process.

Members come from the pieces the label drain and the chain planner already use (the label listing and
`chain._resolve`), injected here so nothing touches the network. Each line of a batch is exactly the
single-goal `report()` object. Controls (each seen red on its own): skip the first member, skip the last
member, drop the error line, drop the --chain arm.
"""
import importlib.util, json, pathlib

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


deps = _mod("deps")


class _Source:
    repo = "o/r"
    goal_label = "sdlc:goal"

    def __init__(self, bad=()):
        self.bad, self.reads = set(bad), []

    def append_to_body(self, goal, marker):
        raise AssertionError("report never writes")

    def _read_issue(self, goal, fields, comment_limit=None):
        self.reads.append(str(goal))
        if str(goal) in self.bad:
            raise RuntimeError("unreadable")
        return {"title": "t%s" % goal, "body": "Follow-up to #99. Also #%s." % goal, "state": "OPEN",
                "comments": [{"body": "Blocked by #77"}]}


def _wire(monkeypatch, source, label=(), chain=()):
    monkeypatch.setattr(deps, "_open_source", lambda sdlc: source)
    monkeypatch.setattr(deps, "_label_members", lambda src, lab: list(label))
    monkeypatch.setattr(deps, "_chain_members", lambda sdlc, goal: list(chain))


def _lines(out):
    return [json.loads(l) for l in out.splitlines()]


def test_label_batch_is_one_call_and_each_line_equals_the_single_report(monkeypatch, capsys):
    src = _Source()
    _wire(monkeypatch, src, label=[8, 3, 5, 4])
    assert deps.main(["deps.py", "report", "/x", "--label", "wave"]) == 0
    got = _lines(capsys.readouterr().out)
    assert [g["goal"] for g in got] == ["3", "4", "5", "8"]          # ordered by issue number
    for g in got:
        assert g == deps.report(_Source(), g["goal"])
    # every member read, none skipped; the only other reads are #1990's reverse reads of the fake's refs
    assert {"3", "4", "5", "8"} <= set(src.reads) and set(src.reads) <= {"3", "4", "5", "8", "77", "99"}


def test_chain_batch_covers_the_goal_and_its_open_prerequisites_only(monkeypatch, capsys):
    _wire(monkeypatch, _Source(), label=[1, 2, 3], chain=[8, 6])      # a closed prerequisite is not in the list
    assert deps.main(["deps.py", "report", "/x", "--chain", "8"]) == 0
    assert [g["goal"] for g in _lines(capsys.readouterr().out)] == ["6", "8"]


def test_an_unreadable_member_prints_an_error_line_and_exits_2_without_skipping_others(monkeypatch, capsys):
    _wire(monkeypatch, _Source(bad={"4"}), label=[3, 4, 5])
    assert deps.main(["deps.py", "report", "/x", "--label", "wave"]) == 2
    got = _lines(capsys.readouterr().out)
    assert [g["goal"] for g in got] == ["3", "4", "5"]
    assert "error" in got[1] and "error" not in got[0] and "error" not in got[2]


def test_the_single_form_is_unchanged_and_usage_names_both_new_forms(monkeypatch, capsys):
    _wire(monkeypatch, _Source())
    assert deps.main(["deps.py", "report", "/x", "5"]) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and json.loads(out) == deps.report(_Source(), "5")
    assert "--label" in deps.USAGE and "--chain" in deps.USAGE


def test_a_batch_that_finds_no_members_exits_2_and_says_so(monkeypatch, capsys):
    _wire(monkeypatch, _Source(), label=[])
    assert deps.main(["deps.py", "report", "/x", "--label", "wave"]) == 2
    assert capsys.readouterr().out == ""


def test_label_members_uses_the_label_scope_and_the_shared_listing():
    seen = {}

    class Src(_Source):
        def label_exists(self, name): return True
        def scope_to_label(self, name): seen["scope"] = name

    fetched = []

    def fetch(run, repo, labels, cap):
        fetched.append((repo, labels))
        return [{"number": 9}, {"number": 2}]

    assert deps._label_members(Src(), "wave", fetch=fetch) == [2, 9]
    assert seen["scope"] == "wave" and fetched == [("o/r", ["wave"])]


def test_chain_members_are_the_planners_rows_not_a_second_walk():
    out = deps._chain_members("/x", 8, resolve=lambda sdlc, goal, cfg, run, now: (
        {"members": [{"number": 8}, {"number": 6}]}, None), config={})
    assert out == [6, 8]
