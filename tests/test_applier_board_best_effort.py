"""#1302: the label swap gates the applier; the board card move is best effort."""
import importlib.util
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
import test_applier as TA  # noqa: E402
import test_applier_veto_reader as TV  # noqa: E402

AP, AU, DS = TA.AP, TA.AU, TA.DS
T0, MIN = TA.T0, TA.MIN


class BoardFail(TV.BoardSrc):
    def _set_board_status(self, issue, status, warn=True):
        self.cards.append((issue, status))
        return False


class SwapFail(TV.BoardSrc):
    def _swap_labels(self, issue, add, remove):
        raise RuntimeError("swap did not land")


def _autos(sd, cfg):
    store = DS._store(sd, cfg)
    recs = [json.load(open(os.path.join(store, n))) for n in os.listdir(store) if n.endswith(".json")]
    return [r for r in recs if r.get("how") == "autonomous"]


def _pending(tmp_path, src):
    sd, cfg, _ = TA._setup(tmp_path)
    TA._ask(sd, cfg)
    return sd, cfg


def test_board_failure_after_swap_still_completes_once(tmp_path):
    src = BoardFail()
    sd, cfg = _pending(tmp_path, src)
    acts = AP.tick(sd, cfg, T0 + 61 * MIN, source=src)
    assert [a["result"] for a in acts] == ["done"]
    assert len(src.swaps) == 1 and len(src.notes) == 1 and len(_autos(sd, cfg)) == 1
    ask = AP._asks(sd)[0]
    assert ask["state"] == "applied" and ask.get("board_failed")
    assert AP.tick(sd, cfg, T0 + 62 * MIN, source=src) == []
    assert len(src.swaps) == 1 and len(src.notes) == 1 and len(_autos(sd, cfg)) == 1


def test_swap_failure_writes_nothing_and_stays_pending(tmp_path):
    src = SwapFail()
    sd, cfg = _pending(tmp_path, src)
    acts = AP.tick(sd, cfg, T0 + 61 * MIN, source=src)
    assert [a["result"] for a in acts] == ["failed"]
    assert src.notes == [] and _autos(sd, cfg) == []
    assert AP._asks(sd)[0]["state"] == "pending"


def test_veto_repark_with_board_failure_completes(tmp_path):
    class Stuck(TV.BoardSrc):
        def _set_board_status(self, issue, status, warn=True):
            return status != "Parked"
    sd, cfg, src = TV._applied_world(tmp_path, Stuck())
    acts = TV._tick(sd, cfg, src, [TV._comment("boss", "no", 65)])
    assert [(a["kind"], a["result"]) for a in acts] == [("veto", "done")]
    ask = TV._state(sd)
    assert ask["reparked"] and ask["state"] == "vetoed"
    assert len(src.swaps) == 1 and len(src.notes) == 1


def test_closed_gate_is_identical(tmp_path):
    src = BoardFail()
    sd, cfg = _pending(tmp_path, src)
    cfg["rulebook"]["autonomy"]["applier"]["enabled"] = False
    before = AP._asks(sd)
    assert AP.tick(sd, cfg, T0 + 61 * MIN, source=src) == []
    assert AP._asks(sd) == before and src.swaps == [] and src.notes == []
