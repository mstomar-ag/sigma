"""#1395: the applier does not act on an issue that is no longer parked."""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
import test_applier as TA  # noqa: E402

AP, T0, MIN = TA.AP, TA.T0, TA.MIN


class LabelSrc(TA.Src):
    def __init__(self, labels=("sdlc:parked",), boom=False):
        super().__init__()
        self.labels, self.boom, self.reads = list(labels), boom, []

    def _read_issue(self, goal, fields, comment_limit=None):
        self.reads.append(goal)
        if self.boom:
            raise RuntimeError("read refused")
        return {"labels": [{"name": n} if goal % 2 else n for n in self.labels]}


def _ask_file(sd):
    d = pathlib.Path(sd) / "autonomy"
    return json.loads(next(d.glob("ask-*.json")).read_text())


def test_unparked_issue_is_closed_without_a_gesture(tmp_path):
    sd, cfg, _ = TA._setup(tmp_path)
    TA._ask(sd, cfg)
    src = LabelSrc(labels=("sdlc:goal",))
    acts = AP.tick(sd, cfg, T0 + 61 * MIN, source=src, comments_fn=lambda i: [])
    assert [a["result"] for a in acts] == ["skipped"]
    assert src.swaps == [] and src.notes == []
    ask = _ask_file(sd)
    assert ask["state"] == "answered" and ask["answer"] == "already unparked" and ask.get("answered_at")
    assert AP.tick(sd, cfg, T0 + 62 * MIN, source=src, comments_fn=lambda i: []) == []


def test_parked_issue_still_applies_with_string_or_dict_labels(tmp_path):
    for issue in (7, 8):
        sd, cfg, _ = TA._setup(tmp_path / str(issue))
        TA._ask(sd, cfg, issue=issue)
        src = LabelSrc()
        acts = AP.tick(sd, cfg, T0 + 61 * MIN, source=src, comments_fn=lambda i: [])
        assert [a["result"] for a in acts] == ["done"] and len(src.swaps) == 1


def test_read_failure_fails_and_leaves_ask_pending(tmp_path):
    sd, cfg, _ = TA._setup(tmp_path)
    TA._ask(sd, cfg)
    src = LabelSrc(boom=True)
    acts = AP.tick(sd, cfg, T0 + 61 * MIN, source=src, comments_fn=lambda i: [])
    assert [a["result"] for a in acts] == ["failed"]
    assert src.swaps == [] and src.notes == []
    assert _ask_file(sd)["state"] == "pending"


def test_closed_gate_reads_nothing(tmp_path):
    sd, cfg, _ = TA._setup(tmp_path)
    TA._ask(sd, cfg)
    src = LabelSrc(labels=())
    assert AP.tick(sd, {}, T0 + 61 * MIN, source=src) == []
    assert src.reads == []
