"""#1624: `dag_render.render`, the pure DAG.md text from merged dag history records.

Controls, each run red once on its own clause (see the dagmd test module for the git side):
  * latest observation wins -> plain last-in-input-order wins   -> the order-independence tests red
  * drop the sort of the records                                 -> the shuffled-bytes test red
  * drop the stuck-chain ordering (most freeing first)           -> the stuck-order test red
"""
import importlib.util
import json
import pathlib
import random

ROOT = pathlib.Path(__file__).resolve().parent.parent
S = ROOT / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


R = _mod("dag_render")


def add(t, a, b):
    return {"t": t, "ev": "edge-added", "a": a, "b": b}


def rem(t, a, b):
    return {"t": t, "ev": "edge-removed", "a": a, "b": b}


T1, T2, T3 = "2026-03-05T10:00:00Z", "2026-03-06T10:00:00Z", "2026-03-07T10:00:00Z"


def _section(text, name):
    parts = text.split("\n## ")
    for p in parts:
        if p.startswith(name):
            return p
    raise AssertionError("no section %s in %r" % (name, text))


def test_empty_history_renders_header_only():
    assert R.render([]) == "# DAG\n"


def test_shuffled_records_give_identical_bytes():
    recs = [add(T1, 1, 2), add(T1, 2, 3), rem(T2, 2, 3), add(T2, 5, 6), add(T3, 2, 3),
            {"t": T2, "ev": "node-resolved", "n": 6}, {"t": T3, "ev": "chain-completed", "chain": 9, "n": 3}]
    want = R.render(recs)
    rnd = random.Random(7)
    for _ in range(25):
        x = list(recs)
        rnd.shuffle(x)
        assert R.render(x) == want


def test_added_then_removed_shows_removed():
    text = R.render([rem(T2, 1, 2), add(T1, 1, 2), add(T1, 3, 4)])
    assert "#1" not in text.split("## Stuck")[-1].split("## Counts")[0]
    assert "#3" in _section(text, "Stuck")


def test_removed_then_added_shows_added():
    text = R.render([add(T2, 1, 2), rem(T1, 1, 2)])
    assert "#1" in _section(text, "Stuck")


def test_stuck_most_freeing_first_then_number():
    recs = [add(T1, 10, 11), add(T1, 10, 12), add(T1, 3, 4), add(T1, 20, 21), add(T1, 21, 22), add(T1, 21, 23)]
    s = _section(R.render(recs), "Stuck")
    order = [int(w[1:].rstrip(",:")) for w in s.split() if w.startswith("#")]
    roots = [n for n in order if n in (3, 10, 20)]
    assert roots == [20, 10, 3]            # 20 frees 4, 10 frees 2, 3 frees 1


def test_resolved_blocker_is_not_stuck_and_its_dependents_are_freed():
    recs = [add(T1, 1, 2), add(T1, 2, 3), {"t": T2, "ev": "node-resolved", "n": 1}]
    s = _section(R.render(recs), "Stuck")
    assert "#2" in s and "#1" not in s


def test_cycle_section_lists_members():
    text = R.render([add(T1, 1, 2), add(T1, 2, 3), add(T1, 3, 1), add(T1, 7, 8)])
    c = _section(text, "Cycles")
    assert "#1" in c and "#2" in c and "#3" in c and "#7" not in c


def test_completed_this_month_is_the_newest_records_month():
    recs = [{"t": "2026-02-01T00:00:00Z", "ev": "chain-completed", "chain": 5, "n": 2},
            {"t": T1, "ev": "chain-completed", "chain": 9, "n": 3}]
    c = _section(R.render(recs), "Completed")
    assert "#9" in c and "#5" not in c


def test_numbers_only_no_title_or_name_leaks():
    rec = add(T1, 1, 2)
    rec.update({"title": "SECRETTITLE", "actor": "dana", "who": "eli"})
    text = R.render([rec])
    assert "SECRETTITLE" not in text and "dana" not in text and "eli" not in text
    assert "SECRETTITLE" not in json.dumps(R.render([rec]))


def test_malformed_records_are_skipped():
    text = R.render([add(T1, 1, 2), "x", {"ev": "edge-added", "a": "z", "b": 2, "t": T1}, {"ev": "edge-added"}, None])
    assert "#1" in text


def test_counts_section():
    c = _section(R.render([add(T1, 1, 2), add(T1, 2, 3)]), "Counts")
    assert "open 3" in c and "edges 2" in c
