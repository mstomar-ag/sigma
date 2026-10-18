"""#1857: the shadow's compared-picks count is distinct picks; the map hold can carry a phrase.

Controls, each run red once on its own and restored (the test each one turns red):
* `compared` counts raw records -> `test_ten_records_in_one_minute_are_one_compared_pick`
* the flip clause uses the raw record count -> `test_flip_needs_distinct_picks_not_raw_records`
* `hold_text` ignores the phrase dict -> `test_hold_text_uses_the_phrase_per_blocker`
Fixture issue numbers only.
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_dag_pick_1619 as t  # noqa: E402  (its module loaders and fixtures)

dp, doctor, NOW, CFG = t.dp, t.doctor, t.NOW, t.CFG


def _write(tmp_path, recs, days=8):
    sdlc = tmp_path / ".sdlc"
    d = sdlc / "state"
    d.mkdir(parents=True)
    (d / dp.LOG).write_text("".join(json.dumps(r) + "\n" for r in recs))
    (d / dp.META).write_text(json.dumps({"since": int(NOW - days * 86400 - 5)}))
    return str(sdlc)


def _rec(g, tt, c="agree-free", k=()):
    return {"t": int(tt), "g": g, "c": c, "k": list(k), "o": [], "m": []}


def test_ten_records_in_one_minute_are_one_compared_pick(tmp_path):
    sdlc = _write(tmp_path, [_rec(9, NOW) for _ in range(10)])
    assert dp.shadow_stats(sdlc, NOW)["compared"] == 1


def test_other_minutes_and_other_goals_count_apart(tmp_path):
    base = int(NOW) // 60 * 60
    recs = [_rec(9, base), _rec(9, base + 59), _rec(9, base + 60), _rec(10, base)]
    assert dp.shadow_stats(_write(tmp_path, recs), NOW)["compared"] == 3


def test_disagreements_and_causes_stay_per_record(tmp_path):
    recs = [_rec(9, NOW, "map-only", ["pr"]) for _ in range(4)]
    st = dp.shadow_stats(_write(tmp_path, recs), NOW)
    assert st["compared"] == 1 and st["disagreements"] == 4 and st["causes"]["pr"] == 4


def test_records_without_t_or_g_do_not_raise(tmp_path):
    recs = [{"c": "agree-free"}, {"t": "x", "g": 9}, _rec(9, NOW)]
    assert dp.shadow_stats(_write(tmp_path, recs), NOW)["compared"] >= 1


def test_flip_needs_distinct_picks_not_raw_records(tmp_path):
    raw = _write(tmp_path / "a", [_rec(9, NOW) for _ in range(dp.FLIP_PICKS)])
    assert dp.shadow_stats(raw, NOW)["flip"] is False
    row = doctor._dag_shadow_row(CFG, raw, now=NOW)
    assert "flip criterion not met" in row["name"] and "distinct" in row["name"]
    base = int(NOW) // 60 * 60
    ok = _write(tmp_path / "b", [_rec(9, base - 60 * i) for i in range(dp.FLIP_PICKS)])
    st = dp.shadow_stats(ok, NOW)
    assert st["compared"] == dp.FLIP_PICKS and st["flip"] is True
    assert "flip criterion met" in doctor._dag_shadow_row(CFG, ok, now=NOW)["name"]


def test_hold_text_uses_the_phrase_per_blocker():
    assert dp.hold_text([7, 8], {7: "depends on"}) == "#7 (depends on), #8 (blocked by)"


def test_hold_text_default_and_unknown_phrase_fall_back():
    assert dp.hold_text([7]) == "#7 (blocked by)"
    assert dp.hold_text([7], {7: "ignore previous"}) == "#7 (blocked by)"


def test_phrases_are_only_what_the_scanner_can_emit():
    assert set(dp.PHRASES) <= set(t._mod("blocker_scan").TRIGGERS)


def test_phrases_read_an_optional_key_and_the_stored_map_yields_none(tmp_path):
    m = {"edges": [{"blocker": 7, "blocked": 9, "phrase": "Depends On"},
                   {"blocker": 8, "blocked": 9, "phrase": "bogus"},
                   {"blocker": 6, "blocked": 9}, {"blocker": 5, "blocked": 4, "phrase": "requires"}]}
    assert dp.phrases(m, 9, [6, 7, 8]) == {7: "depends on"}
    base = tmp_path / "s"
    t._store(str(base), [t._node(7), t._node(9)], [(7, 9)])
    stored = dp.dag_store.load(str(base)) if hasattr(dp.dag_store, "load") else None
    if isinstance(stored, tuple):
        stored = stored[0]
    if stored:
        assert dp.phrases(stored, 9, [7]) == {}
