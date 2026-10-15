"""#1469: migrate-confirmation keeps the board-card result instead of dropping it."""
import test_promote_migrate as base

promote = base.promote
PROPOSED, GOAL, PARKED = base.PROPOSED, base.GOAL, base.PARKED


class BoardSource(base.FakeSource):
    """A fake whose board is active; `landing` is the set of columns a card move succeeds for."""
    col = {"ready": "Ready", "backlog": "Backlog", "parked": "Parked", "blocked": "Blocked"}

    def __init__(self, landing=(), board_active=True, ready_lane="Ready"):
        super().__init__()
        self.landing, self.board_active, self.lane = set(landing), board_active, ready_lane
        self.moves = []

    def _ready_lane(self):
        return self.lane if self.board_active else None

    def _set_board_status(self, number, column, warn=True):
        self.moves.append((int(str(number)), column))
        self.writes.append(("board", int(str(number))))
        return column in self.landing


def _two(src):
    src.add(1, PROPOSED, "priority:P1", title="arm me", body="please add a flag")
    src.add(2, PROPOSED, "priority:P1", title="park me", comments=[promote._OWNER_MARKER + " held"])
    return src


def _rows(rep):
    return {r["number"]: r for r in rep["items"]}


def test_unmoved_arm_has_a_note_a_count_and_is_incomplete(tmp_path):
    src = _two(BoardSource())
    rep = promote.migrate(src, {}, apply=True, sdlc_dir=str(tmp_path))
    row = _rows(rep)["1"]
    assert row["card_unmoved"] is True
    assert any("'Ready'" in n and "did not move" in n for n in row["notes"])
    assert rep["cards_unmoved"] == 1 and rep["complete"] is False
    out = promote.render_migrate(rep)
    assert "1 armed card(s) not moved to 'Ready': #1" in out and "move them by hand" in out


def test_cli_exits_nonzero_and_prints_summary(tmp_path, capsys):
    src = _two(BoardSource())
    rc = promote.main(["promote.py", "migrate-confirmation", str(tmp_path), "--apply"], source=src)
    out = capsys.readouterr().out
    assert rc == 1 and "not moved to 'Ready': #1" in out
    assert GOAL in src.store[1]["labels"]                       # the label still landed


def test_moved_arm_is_complete(tmp_path):
    src = _two(BoardSource(landing={"Ready", "Parked"}))
    rep = promote.migrate(src, {}, apply=True, sdlc_dir=str(tmp_path))
    assert rep["cards_unmoved"] == 0 and rep["complete"] is True
    assert "not moved" not in promote.render_migrate(rep)


def test_park_moves_card_to_parked(tmp_path):
    src = _two(BoardSource(landing={"Ready", "Parked"}))
    promote.migrate(src, {}, apply=True, sdlc_dir=str(tmp_path))
    assert [m for m in src.moves if m[0] == 2] == [(2, "Parked")]


def test_park_falls_back_to_blocked(tmp_path):
    src = _two(BoardSource(landing={"Ready", "Blocked"}))
    rep = promote.migrate(src, {}, apply=True, sdlc_dir=str(tmp_path))
    assert [m for m in src.moves if m[0] == 2] == [(2, "Parked"), (2, "Blocked")]
    assert "notes" not in _rows(rep)["2"] and rep["complete"] is True


def test_park_with_neither_lane_gets_a_note_but_stays_complete(tmp_path):
    src = _two(BoardSource(landing={"Ready"}))
    rep = promote.migrate(src, {}, apply=True, sdlc_dir=str(tmp_path))
    row = _rows(rep)["2"]
    assert any("Parked" in n and "Blocked" in n for n in row["notes"])
    assert rep["complete"] is True and rep["cards_unmoved"] == 0     # labels decide eligibility for a park


def test_label_queue_board_keeps_complete_but_notes_the_card(tmp_path):
    src = _two(BoardSource(landing=(), ready_lane=None))
    rep = promote.migrate(src, {}, apply=True, sdlc_dir=str(tmp_path))
    assert rep["complete"] is True and rep["cards_unmoved"] == 1
    assert any("did not move" in n for n in _rows(rep)["1"]["notes"])


def test_no_board_is_unchanged(tmp_path):
    src = _two(BoardSource(landing=(), board_active=False))
    rep = promote.migrate(src, {}, apply=True, sdlc_dir=str(tmp_path))
    assert src.moves == [(1, "Ready")]                        # arm call as before, no new park call
    assert all("notes" not in r or not any("move" in n for n in r["notes"]) for r in rep["items"])
    assert rep["complete"] is True and rep["cards_unmoved"] == 0
    assert "not moved" not in promote.render_migrate(rep)
    assert not any("card_unmoved" in r for r in rep["items"])
