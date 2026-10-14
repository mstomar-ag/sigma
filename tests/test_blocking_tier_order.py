"""#1409: the picker orders by the blocking graph -- tier first, then a blocker before its tier peers.

A blocker is never below what it blocks (the writers raise it), so a blocking label never needs to
jump a higher tier. Covers the label path and the board path."""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_github_project as tgp
import test_sources as ts

BLOCK = "sdlc:blocking"


def _issue(n, prio=None, *extra):
    labels = [{"name": "sdlc:goal"}] + [{"name": e} for e in extra]
    if prio:
        labels.append({"name": "priority:" + prio})
    return {"number": n, "labels": labels}


def _label_src(issues, override=True):
    """Label-path source over a mutable issue list, with real AND semantics on the queried labels."""
    def run(a):
        if ts._issues_verb(a) != "list":
            return ""
        want = ts._rest_labels(a)
        return json.dumps([i for i in issues
                           if all(w in {l["name"] for l in i["labels"]} for w in want)])
    cfg = {"discovery": {"source": "github", "blocking_priority_override": override}}
    return ts._mod("sources").GitHubSource(cfg, run=run)


def _drain(src, issues):
    out = []
    for _ in range(len(issues)):
        n = src.next_pending()
        if n is None:
            break
        out.append(int(n))
        issues[:] = [i for i in issues if i["number"] != int(n)]
    return out


# ---- label path ----

def test_label_blocker_raised_to_p0_beats_eleven_waiting_p0s():
    issues = [_issue(n, "P0") for n in range(1, 12)] + [_issue(50, "P0", BLOCK)]
    assert _label_src(issues).next_pending() == "50"


def test_label_p3_blocker_does_not_pass_a_waiting_p0():
    issues = [_issue(9, "P0"), _issue(3, "P3", BLOCK)]
    assert _label_src(issues).next_pending() == "9"


def test_label_blocker_precedes_older_same_tier_goals():
    issues = [_issue(1, "P1"), _issue(2, "P1"), _issue(30, "P1", BLOCK)]
    assert _label_src(issues).next_pending() == "30"


def test_label_blocker_beyond_the_pool_is_still_merged_by_tier():
    """The blocking query is a union: a blocker absent from the base pool still takes part."""
    base = [_issue(1, "P1"), _issue(2, "P0")]
    blk = _issue(30, "P1", BLOCK)

    def run(a):
        if ts._issues_verb(a) != "list":
            return ""
        return json.dumps([blk] if BLOCK in ts._rest_labels(a) else base)
    cfg = {"discovery": {"source": "github", "blocking_priority_override": True}}
    src = ts._mod("sources").GitHubSource(cfg, run=run)
    pend = src._pending_by_label(set())
    assert [p["number"] for p in pend] == [2, 30, 1]


def test_label_override_off_is_the_plain_pick_key_order():
    issues = [_issue(9, "P0"), _issue(3, "P3", BLOCK), _issue(4, "P1"), _issue(5, "P1", BLOCK)]
    assert _drain(_label_src(issues, override=False), issues) == [9, 4, 5, 3]


def test_label_override_on_orders_tier_then_blocker():
    issues = [_issue(9, "P0"), _issue(3, "P3", BLOCK), _issue(4, "P1"), _issue(5, "P1", BLOCK)]
    assert _drain(_label_src(issues), issues) == [9, 5, 4, 3]


def test_label_parked_and_blocked_blockers_are_never_picked():
    issues = [_issue(9, "P2"),
              _issue(1, "P0", BLOCK, "sdlc:parked"),
              _issue(2, "P0", BLOCK, "sdlc:blocked")]
    assert _label_src(issues).next_pending() == "9"


def test_label_a_blocked_blocker_yields_to_the_deepest_unblocked_one():
    issues = [_issue(9, "P0"),
              _issue(2, "P0", BLOCK, "sdlc:blocked"),    # blocks #9 but itself waits on #3
              _issue(3, "P0", BLOCK)]
    assert _label_src(issues).next_pending() == "3"


def test_label_no_stale_snapshot_between_picks():
    issues = [_issue(9, "P0"), _issue(3, "P0")]
    src = _label_src(issues)
    assert src.next_pending() == "3"
    issues[:] = [_issue(9, "P0"), _issue(3, "P0", "sdlc:blocked"), _issue(7, "P0", BLOCK)]
    assert src.next_pending() == "7"


# ---- board path ----

def _board(cards, override=True):
    return tgp._src_override(tgp._pworld(cards), override=override)


def test_board_blocker_raised_to_p0_beats_eleven_waiting_p0s():
    cards = [tgp._ready(n, "P0") for n in range(1, 12)]
    cards.append(tgp._ready(50, "P0", ("sdlc:goal", BLOCK)))
    assert _board(cards).next_pending() == "50"


def test_board_p3_blocker_does_not_pass_a_waiting_p0():
    cards = [tgp._ready(9, "P0"), tgp._ready(3, "P3", ("sdlc:goal", BLOCK))]
    assert _board(cards).next_pending() == "9"


def test_board_blocker_precedes_same_tier_cards_even_when_lower_on_the_board():
    cards = [tgp._ready(1, "P1"), tgp._ready(2, "P1"), tgp._ready(30, "P1", ("sdlc:goal", BLOCK))]
    assert _board(cards).next_pending() == "30"


def test_board_override_off_ignores_the_blocking_label():
    cards = [tgp._ready(1, "P1"), tgp._ready(30, "P1", ("sdlc:goal", BLOCK))]
    assert _board(cards, override=False).next_pending() == "1"


def test_board_blocked_blocker_is_not_picked():
    cards = [tgp._ready(9, "P0"), tgp._ready(2, "P0", ("sdlc:goal", BLOCK, "sdlc:blocked"))]
    assert _board(cards).next_pending() == "9"
