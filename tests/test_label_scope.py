"""#1575: `--label <name>` confines one loop run to the goals carrying a temporary label.

EXCLUSIVE membership is the label itself (AND-ed server-side onto the goal label), so a goal with
only the goal label and a goal with only the label are both unpickable. The fake `gh` transport
here honours `labels=` (it is the one in tests/test_feature_scope.py), which is what makes the
server-side half observable: a canned list would pass with the AND deleted.
"""
import json, tempfile

import pytest

import gqlfake
import registry_root as RR
from test_feature_scope import _mod, _issue, _gh, _base, _rest_field, _is_rest_issues_call

LBL = "validation-run-1"


def _source(lp, base, issues, calls=None, scope=LBL, **cfg_extra):
    cfg = lp.state.load_config(base)
    src = lp.sources.GitHubSource(cfg, run=_gh(issues, calls), sdlc_dir=base)
    src._BACKLOG_READ_RETRY_BASE = 0
    if scope:
        lp.sources.scope_to_label(src, scope)
    return cfg, src


def _all_four():
    """both, goal-only, label-only, neither."""
    both = _issue(10, LBL)
    goal_only = _issue(5)
    label_only = _issue(12, LBL)
    label_only["labels"] = [l for l in label_only["labels"] if l["name"] != "sdlc:goal"]
    neither = _issue(13)
    neither["labels"] = []
    return [goal_only, label_only, neither, both]       # the one that qualifies is LAST by order


def test_only_a_goal_carrying_both_labels_is_ever_picked():
    lp = _mod("loop")
    with tempfile.TemporaryDirectory() as d:
        base = _base(d)
        cfg, src = _source(lp, base, _all_four())
        assert lp._next(base, src, cfg) == ("goal", "10")
        assert src.next_pending(skip={"10"}) is None      # exclusive: no fallback to goal-only #5


def test_the_unscoped_run_would_have_picked_the_goal_only_issue():
    """Control for the above: without the flag #5 (oldest pickable) is the pick."""
    lp = _mod("loop")
    with tempfile.TemporaryDirectory() as d:
        base = _base(d)
        cfg, src = _source(lp, base, _all_four(), scope=None)
        assert lp._next(base, src, cfg) == ("goal", "5")


def test_the_scope_label_is_anded_onto_every_backlog_query():
    lp = _mod("loop")
    calls = []
    with tempfile.TemporaryDirectory() as d:
        base = _base(d)
        cfg, src = _source(lp, base, _all_four(), calls)
        src.next_pending()
    reads = [c for c in calls if _is_rest_issues_call(c)]
    assert reads and all(LBL in (_rest_field(c, "labels") or "").split(",") for c in reads)


def test_a_label_removed_between_the_list_read_and_the_claim_leaves_the_goal_untouched():
    lp = _mod("loop")
    issues = [_issue(10, LBL), _issue(11, LBL)]
    calls = []
    with tempfile.TemporaryDirectory() as d:
        base = _base(d)
        cfg = lp.state.load_config(base)
        inner = _gh(issues, calls)

        def run(args):
            args = list(args)
            # every list read is over; the claim's own read of #10 is the first single-issue read.
            # The label is gone by then, as if a human removed it in that window.
            if not _is_rest_issues_call(args) and any(str(a).endswith("/issues/10") for a in args):
                issues[0]["labels"] = [l for l in issues[0]["labels"] if l["name"] != LBL]
            return inner(args)
        src = lp.sources.GitHubSource(cfg, run=run, sdlc_dir=base)
        src._BACKLOG_READ_RETRY_BASE = 0
        lp.sources.scope_to_label(src, LBL)
        assert lp._next(base, src, cfg) == ("goal", "11")
    def mutates(c):
        text = " ".join(map(str, c))
        method = c[c.index("--method") + 1] if "--method" in c else "GET"
        return method.upper() != "GET" or any(w in text for w in ("--add-label", "--remove-label", "comment"))
    writes = [c for c in calls if mutates(c) and any(str(a).endswith("/issues/10") or str(a) == "10"
                                                      for a in c)]
    assert not writes, writes
    assert not [c for c in calls if mutates(c) and "10" in " ".join(map(str, c))]


def test_a_scoped_run_does_no_board_read_even_with_a_board_configured():
    lp = _mod("loop")
    src = lp.sources.GitHubSource({"discovery": {"source": "github", "github": {
        "repo": "acme/widget",
        "project": {"enabled": True, "owner": "o", "title": "t", "number": 1}}}},
        run=_gh([_issue(11, LBL)]))
    src._BACKLOG_READ_RETRY_BASE = 0
    board_reads = []
    src._board_queue = lambda: board_reads.append(1) or None
    src.next_pending()
    assert board_reads == [1]                       # unscoped: the board decides, as before
    src.scope_to_label(LBL)
    assert src.next_pending() == "11"
    assert board_reads == [1]                       # scoped: not read again


def test_an_unscoped_run_makes_the_golden_call_list():
    """Golden: the same two REST lists (goal, blocking lane), neither carrying a scope label, and the claim-time label bookkeeping adds
    nothing -- the scope costs nothing when it is not asked for."""
    lp = _mod("loop")
    calls = []
    with tempfile.TemporaryDirectory() as d:
        base = _base(d)
        cfg, src = _source(lp, base, [_issue(10, LBL)], calls, scope=None)
        assert src.label_scope is None and src._label_seen == {}
        src.next_pending()
        src.fetch_body_labels("10")
    assert src._label_seen == {}                    # nothing is recorded unscoped
    lists = [c for c in calls if _is_rest_issues_call(c)]
    assert [_rest_field(c, "labels") for c in lists] == ["sdlc:goal", "sdlc:goal,sdlc:blocking"]
    assert [c for c in calls if c not in lists][0][:2] == ["api", "repos/acme/widget/issues/10"]


# --- refusals ------------------------------------------------------------------------------------

REFUSALS = [
    ("bare", ["--label"], "needs a label name"),
    ("sdlc-namespace", ["--label", "sdlc:x"], "namespace"),
    ("feature-namespace", ["--label", "feature:x"], "namespace"),
    ("goal-label", ["--label", "sdlc:goal"], "namespace"),      # sdlc:* covers the default goal label
    ("with-feature", ["--label", LBL, "--feature", "voice"], "--feature"),
]


@pytest.mark.parametrize("name,flags,needle", REFUSALS, ids=[r[0] for r in REFUSALS])
@pytest.mark.parametrize("verb", ["next", "next-batch"])
def test_a_refusal_exits_two_with_one_line_and_makes_no_call(name, flags, needle, verb, monkeypatch, capsys):
    lp = _mod("loop")
    seen = []
    with tempfile.TemporaryDirectory() as d:
        base = _base(d)
        monkeypatch.setattr(lp.sources, "_run_gh", lambda a, binary="gh": seen.append(a) or "[]")
        assert lp.main(["loop.py", verb, base] + flags) == 2
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and needle in err[0], err
    assert seen == []


def test_a_custom_goal_label_is_refused_as_the_scope(monkeypatch, capsys):
    lp = _mod("loop")
    seen = []
    with tempfile.TemporaryDirectory() as d:
        base = _base(d)
        cfg = json.loads(open(base + "/config.json").read())
        cfg["discovery"]["github"]["goal_label"] = "wanted"
        open(base + "/config.json", "w").write(json.dumps(cfg))
        monkeypatch.setattr(lp.sources, "_run_gh", lambda a, binary="gh": seen.append(a) or "[]")
        assert lp.main(["loop.py", "next", base, "--label", "wanted"]) == 2
    assert "goal label" in capsys.readouterr().err and seen == []


def test_a_nonexistent_label_is_refused_and_no_backlog_is_read(monkeypatch, capsys):
    lp = _mod("loop")
    calls = []
    inner = _gh([_issue(10, LBL)], calls)
    monkeypatch.setattr(lp.sources, "_run_gh", lambda a, binary="gh": inner(a))
    monkeypatch.setattr(lp.sources.GitHubSource, "_BACKLOG_READ_RETRY_BASE", 0)
    monkeypatch.setattr(gqlfake, "_UNKNOWN_LABELS", {"ghost"})
    with tempfile.TemporaryDirectory() as d:
        base = _base(d)
        assert lp.main(["loop.py", "next", base, "--label", "ghost"]) == 2
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "does not exist" in err[0]
    assert not [c for c in calls if _is_rest_issues_call(c)]


def test_a_local_goals_backlog_is_refused_on_the_cli(capsys):
    lp = _mod("loop")
    import pathlib
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, discovery={"source": "local-goals"})
        (pathlib.Path(base) / "goals").mkdir()
        assert lp.main(["loop.py", "next", base, "--label", LBL]) == 2
    assert "local-goals" in capsys.readouterr().err


@pytest.mark.parametrize("verb", ["next", "next-batch"])
def test_the_cli_scopes_both_verbs(verb, monkeypatch, capsys):
    lp = _mod("loop")
    inner = _gh(_all_four())
    monkeypatch.setattr(lp.sources, "_run_gh", lambda a, binary="gh": inner(a))
    monkeypatch.setattr(lp.sources.GitHubSource, "_BACKLOG_READ_RETRY_BASE", 0)
    with tempfile.TemporaryDirectory() as d:
        base = _base(d)
        assert lp.main(["loop.py", verb, base, "--label", LBL]) == 0
    assert capsys.readouterr().out.split() == ["10"]


def test_the_value_survives_and_the_usage_documents_the_flag(capsys):
    lp = _mod("loop")
    assert lp._flags(["--label", LBL, "--skip", "1"]) == {"label": LBL, "skip": "1"}
    assert lp.main(["loop.py"]) == 2
    assert capsys.readouterr().err.count("--label") == 2


def test_the_cli_exit_two_gesture_via_subprocess():
    import subprocess, sys, pathlib
    script = pathlib.Path(__file__).resolve().parent.parent / "skills/sigma-loop/scripts/loop.py"
    with tempfile.TemporaryDirectory() as d:
        base = _base(d)
        r = subprocess.run([sys.executable, str(script), "next", base, "--label", "sdlc:x"],
                           capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and r.stdout == "" and len(r.stderr.strip().splitlines()) == 1


# --- base branch is each goal's own unit ----------------------------------------------------------

def test_goals_in_different_units_keep_their_own_unit_base(tmp_path):
    w = RR.World(tmp_path).build(units=("voice", "billing"))
    issues = {"1": {"body": "Feature: voice\n", "labels": [{"name": "feature:voice"}, {"name": LBL}]},
              "2": {"body": "Feature: billing\n", "labels": [{"name": "feature:billing"}, {"name": LBL}]}}
    plain = w.run

    def run(cwd, argv):
        argv = [str(a) for a in argv]
        if argv[:2] == ["gh", "api"] and argv[2].rsplit("/", 1)[-1] in issues:
            return json.dumps(issues[argv[2].rsplit("/", 1)[-1]])
        return plain(cwd, argv)
    w.run = run
    for goal in ("1", "2"):
        w.work.start(str(w.sdlc), RR.CONFIG, goal, run=run)
    cuts = [c for c in w.calls if "worktree add -b" in c]
    assert any(c.endswith("origin/feature/voice") for c in cuts), cuts
    assert any(c.endswith("origin/feature/billing") for c in cuts), cuts
