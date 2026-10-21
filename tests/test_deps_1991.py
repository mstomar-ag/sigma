"""#1991: `deps.py report` takes `--label` more than once and `--sheet` prints a review sheet.

Controls (each seen red on its own): drop the dedupe; drop the sort; put `mention` before `weak`; put `weak` before
`comment-edge`; let `--sheet` change the JSON path; drop the review-aid sentence; let `--chain` mix with `--label`.
"""
import importlib.util, json, pathlib

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


deps = _mod("deps")

BODY = "Blocked by: #20\nsee also #21 for context\nthis needs #22 first"


class _Source:
    repo = "o/r"
    goal_label = "sdlc:goal"

    def append_to_body(self, goal, marker):
        raise AssertionError("report never writes")

    def _read_issue(self, goal, fields, comment_limit=None):
        body = BODY if str(goal) == "8" else ""
        return {"title": "t%s" % goal, "body": body, "state": "OPEN",
                "comments": [{"body": "Blocked by #23"}] if str(goal) == "8" else []}


def _wire(monkeypatch, members):
    monkeypatch.setattr(deps, "_open_source", lambda sdlc: _Source())
    monkeypatch.setattr(deps, "_label_members", lambda src, lab: list(members[lab]))
    monkeypatch.setattr(deps, "_chain_members", lambda sdlc, goal: [8])


def test_two_labels_give_the_union_once_in_number_order(monkeypatch, capsys):
    _wire(monkeypatch, {"a": [9, 8, 3], "b": [3, 5]})
    assert deps.main(["deps.py", "report", "/x", "--label", "a", "--label", "b"]) == 0
    assert [json.loads(l)["goal"] for l in capsys.readouterr().out.splitlines()] == ["3", "5", "8", "9"]


def test_json_lines_are_unchanged_without_sheet(monkeypatch, capsys):
    _wire(monkeypatch, {"a": [8]})
    assert deps.main(["deps.py", "report", "/x", "--label", "a"]) == 0
    out = capsys.readouterr().out
    assert out == json.dumps(deps.report(_Source(), 8), ensure_ascii=False, sort_keys=True) + "\n"


def test_the_sheet_orders_refs_comment_edge_then_weak_then_mention(monkeypatch, capsys):
    _wire(monkeypatch, {"a": [8]})
    assert deps.main(["deps.py", "report", "/x", "--label", "a", "--sheet"]) == 0
    out = capsys.readouterr().out
    assert not out.lstrip().startswith("{")
    pos = [out.index("#%s (%s" % (n, k)) for n, k in (("23", "comment-edge"), ("22", "weak"), ("21", "mention"))]
    assert pos == sorted(pos)
    assert "#20 (" not in out and "1 ref(s) already read as an edge" in out


def test_the_sheet_says_it_is_a_review_aid_and_only_mark_writes(monkeypatch, capsys):
    _wire(monkeypatch, {"a": [8]})
    deps.main(["deps.py", "report", "/x", "--label", "a", "--sheet"])
    out = capsys.readouterr().out
    assert "review aid" in out and "only `deps.py mark`" in out


def test_label_and_chain_do_not_mix_and_a_bare_flag_is_usage(monkeypatch, capsys):
    _wire(monkeypatch, {"a": [8]})
    assert deps.main(["deps.py", "report", "/x", "--label", "a", "--chain", "8"]) == 2
    assert deps.main(["deps.py", "report", "/x", "--label"]) == 2
    assert deps.main(["deps.py", "report", "/x", "--sheet"]) == 2
