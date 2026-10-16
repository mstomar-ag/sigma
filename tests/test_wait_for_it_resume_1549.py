"""#1549: a dependency park kept parked by a "wait for it" decision (an applied rule) is resumed by the
sweep when its last open blocker closes; a person's keep-parked marker still wins."""
import json

import test_applier as T
import test_auto_unpark as TA

AP, AU = T.AP, T.AU
T0, MIN = T.T0, T.MIN
_mod = TA._mod
PARK = "Parked by Sigma — needs human review: waiting on #7"


def _applied_note(tmp_path):
    """Drive the real applier tick: a dependency rule 'wait for it' applies after its window."""
    cfg = T._cfg()
    sd = T._sd(tmp_path)
    T._train(sd, cfg, qkind="dependency", area="repo", level=3)
    T._rule(sd, cfg, qkind="dependency", area="repo", option="wait for it")
    for _ in range(8):
        AU.record_watch(sd, "dependency", "repo", "wait for it", "wait for it", cfg, now=T0, machine="m1")
    AP.open_for_park(sd, cfg, 42, "dependency", source=T.Src(), now=T0)
    src = T.Src()
    acts = AP.tick(sd, cfg, T0 + 61 * MIN, source=src)
    assert [(a["kind"], a["result"]) for a in acts] == [("keep-parked", "done")]
    assert src.swaps == []
    return [text for _i, text in src.notes]


def _sweep(comments, states):
    au = _mod("auto_unpark")
    issue = TA._parked_issue(42, body="blocked by #7")
    run = TA._sweep_runner(states=states, comments={"42": comments})
    src = au.sources.GitHubSource(TA._config(), run=run)
    return au.compute_unpark_actions(".sdlc", TA._config(), src, [issue], run=run)


def test_applied_wait_for_it_rule_posts_the_waiting_note_and_the_sweep_resumes_it(tmp_path):
    notes = _applied_note(tmp_path)
    assert any(_mod("auto_unpark").WAITING_MARKER in n for n in notes)
    actions, resolved = _sweep([PARK] + notes, {"7": "CLOSED"})
    assert [a["action"] for a in actions] == ["swap-label"] and "sdlc:parked" in actions[0]["remove"]
    assert resolved == {"42": ["7"]}


def test_blocker_still_open_keeps_it_parked(tmp_path):
    actions, resolved = _sweep([PARK] + _applied_note(tmp_path), {"7": "OPEN"})
    assert actions == [] and resolved == {}


def test_a_persons_keep_parked_marker_wins(tmp_path):
    au = _mod("auto_unpark")
    actions, resolved = _sweep([PARK] + _applied_note(tmp_path) + [au.keep_parked_comment("on purpose")],
                               {"7": "CLOSED"})
    assert actions == [] and resolved == {}


def test_a_newer_park_cancels_the_waiting_note(tmp_path):
    actions, _ = _sweep([PARK] + _applied_note(tmp_path) + [PARK], {"7": "CLOSED"})
    assert actions == []


def test_a_park_without_the_note_is_still_never_resumed():
    assert _sweep([PARK], {"7": "CLOSED"})[0] == []


def test_waiting_note_only_for_the_dependency_keep_parked_gesture(tmp_path):
    cfg = T._cfg()
    sd = T._sd(tmp_path)
    T._train(sd, cfg, qkind="needs_decision", area="repo", level=3)
    T._rule(sd, cfg, qkind="needs_decision", area="repo", option="leave it parked")
    for _ in range(8):
        AU.record_watch(sd, "needs_decision", "repo", "leave it parked", "leave it parked", cfg, now=T0, machine="m1")
    AP.open_for_park(sd, cfg, 42, "needs_decision", source=T.Src(), now=T0)
    src = T.Src()
    AP.tick(sd, cfg, T0 + 61 * MIN, source=src)
    assert src.notes and all(_mod("auto_unpark").WAITING_MARKER not in t for _i, t in src.notes)
