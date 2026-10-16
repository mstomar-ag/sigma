"""A seeded card that vanishes before the final snapshot is a reported failure, not a KeyError;
a card with null content is counted in the breakdown as 'other'."""
import json, sys, pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_board_migrate import _mod, _world2, _seedable, OPTIONS  # noqa: E402

ARGV = ["x", "--owner", "acme", "--project", "8", "--apply"]


def _deleting(run):
    """Wrap the fake so a card is deleted from the board as soon as it is seeded."""
    def wrapped(args):
        out = run(args)
        if args[:3] == ["gh", "project", "item-edit"]:
            gone = args[args.index("--id") + 1]
            run.state["cards"] = [c for c in run.state["cards"] if c["id"] != gone]
        return out
    wrapped.state = run.state
    return wrapped


def test_a_seeded_card_deleted_before_the_final_snapshot_fails_cleanly(capsys):
    m = _mod()
    run = _deleting(_world2(OPTIONS, [_seedable(1), _seedable(2, status="Done")]))
    assert m.main(ARGV, run=run) == 1
    out = capsys.readouterr().out
    assert "FAILED" in out and "PVTI_1" in out
    assert "run this command again" in out.lower()


def test_null_content_cards_are_counted_as_other(capsys):
    m = _mod()
    run = _world2(OPTIONS, [_seedable(1), _seedable(2, status="Done")])
    inner = run

    def run2(args):
        out = inner(args)
        q = next((a for a in args if a.startswith("query=")), "")
        if "items(" in q:
            data = json.loads(out)
            nodes = data["data"]["repositoryOwner"]["projectV2"]["items"]["nodes"]
            nodes[1]["content"] = None
            out = json.dumps(data)
        return out
    run2.state = inner.state
    run2.calls = inner.calls
    assert m.main(ARGV, run=run2) == 0
    assert "board: 2 card(s) (1 issues, 0 pull requests, 0 drafts, 1 other)" in capsys.readouterr().out


def test_no_other_part_when_every_card_has_content(capsys):
    m = _mod()
    run = _world2(OPTIONS, [_seedable(1)])
    assert m.main(ARGV, run=run) == 0
    assert "board: 1 card(s) (1 issues, 0 pull requests, 0 drafts)." in capsys.readouterr().out
