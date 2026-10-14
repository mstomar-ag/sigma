"""#1392: one board source is built per tick, not one per ask."""
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
import test_applier as TA  # noqa: E402

AP, T0, MIN = TA.AP, TA.T0, TA.MIN


def _count(monkeypatch, made, boom=False):
    real = AP._load

    class Counted(TA.Src):
        def __init__(self, config, run=None):
            super().__init__()
            made.append(self)
            if boom:
                raise RuntimeError("project list refused")

    def load(name):
        if name != "sources":
            return real(name)
        return types.SimpleNamespace(GitHubSource=Counted)

    monkeypatch.setattr(AP, "_load", load)


def _world(tmp_path, n):
    sd, cfg, _ = TA._setup(tmp_path)
    for i in range(n):
        TA._ask(sd, cfg, issue=7 + i)
    return sd, cfg


def test_three_asks_build_one_source(tmp_path, monkeypatch):
    made = []
    _count(monkeypatch, made)
    sd, cfg = _world(tmp_path, 3)
    acts = AP.tick(sd, cfg, T0 + 61 * MIN, comments_fn=lambda i: [])
    assert [a["result"] for a in acts] == ["done"] * 3
    assert len(made) == 1 and len(made[0].swaps) == 3


def test_refused_construction_is_tried_once_and_each_ask_fails(tmp_path, monkeypatch):
    made = []
    _count(monkeypatch, made, boom=True)
    sd, cfg = _world(tmp_path, 3)
    acts = AP.tick(sd, cfg, T0 + 61 * MIN, comments_fn=lambda i: [])
    assert [a["result"] for a in acts] == ["failed"] * 3
    assert len(made) == 1


def test_injected_source_is_used_as_is(tmp_path, monkeypatch):
    made = []
    _count(monkeypatch, made)
    sd, cfg = _world(tmp_path, 2)
    src = TA.Src()
    AP.tick(sd, cfg, T0 + 61 * MIN, source=src, comments_fn=lambda i: [])
    assert made == [] and len(src.swaps) == 2


def test_closed_gate_and_no_actions_build_nothing(tmp_path, monkeypatch):
    made = []
    _count(monkeypatch, made)
    sd, cfg = _world(tmp_path, 2)
    assert AP.tick(sd, {}, T0 + 61 * MIN) == []
    assert AP.tick(sd, cfg, T0 + 1 * MIN, comments_fn=lambda i: []) == []
    assert AP.tick(sd, cfg, T0 + 61 * MIN, apply_actions=False, comments_fn=lambda i: [])
    assert made == []
