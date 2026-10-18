"""#1625: the auto-unpark sweep reads the stored dependency map first and skips an `sdlc:blocked`
issue the map shows still waiting on an open explicit blocker, with no comment read and no state read.

The map acts only when `discovery.dag.mode` is written exactly `on`; any unusable clause, a goal the
map does not know, a free goal or any other mode leaves today's full check untouched. Fixture issue
numbers are plain integers."""
import json
import pathlib
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_auto_unpark as base  # noqa: E402

au = base._mod("auto_unpark")
qkind = base._mod("qkind")
ds = base._mod("dag_store")
NOW = time.time()
_REAL_FETCH, _REAL_OPEN = au.backlog_check._fetch_scrubbed_comments, au._ref_is_open


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _node(number, state="open"):
    return {"number": number, "kind": "issue", "state": state, "life": "goal", "prio": None,
            "in_corpus": True}


def _store(sdlc, nodes, edges, checked=NOW - 60, built=NOW - 60):
    h = {"repo": "acme/widget", "built_at": _iso(built), "refreshed_at": _iso(built),
         "last_ok_at": _iso(built), "last_error": None, "partial": False}
    if checked is not None:
        h["checked_at"] = _iso(checked)
    ok, why = ds.save(str(sdlc), {"header": h, "nodes": nodes, "edges": [
        {"blocker": a, "blocked": b, "source": "body"} for a, b in edges]})
    assert ok, why


def _cfg(mode):
    cfg = base._config()
    if mode is not None:
        cfg["discovery"]["dag"] = {"mode": mode}
    return cfg


class Rig:
    """Counts comment fetches and state reads, and records whether dag_pick was loaded."""

    def __init__(self, monkeypatch, states=None, comments=None):
        self.run = base._sweep_runner(states=states, comments=comments)
        self.source = au.sources.GitHubSource(base._config(), run=self.run)
        self.fetches = 0
        self.state_reads = 0
        real_fetch, real_open = _REAL_FETCH, _REAL_OPEN

        def fetch(*a, **k):
            self.fetches += 1
            return real_fetch(*a, **k)

        def is_open(source, ref, cache):
            if ref not in cache:
                self.state_reads += 1
            return real_open(source, ref, cache)

        monkeypatch.setattr(au.backlog_check, "_fetch_scrubbed_comments", fetch)
        monkeypatch.setattr(au, "_ref_is_open", is_open)

    def compute(self, sdlc, mode, issues):
        return au.compute_unpark_actions(str(sdlc), _cfg(mode), self.source, issues, run=self.run)


def _waiting_map(sdlc, goal, blocker):
    _store(sdlc, [_node(goal), _node(blocker)], [(blocker, goal)])


def test_open_map_blocker_skips_comment_and_state_reads(tmp_path, monkeypatch):
    rig = Rig(monkeypatch)
    _waiting_map(tmp_path, 201, 7)
    acts, resolved = rig.compute(tmp_path, "on", [base._blocked_issue(201, body="blocked by #7")])
    assert (acts, resolved) == ([], {})
    assert rig.fetches == 0 and rig.state_reads == 0 and rig.run.calls == []


def _golden_issues():
    return [
        base._blocked_issue(101, body="blocked by #5"),     # keep-parked marker comment
        base._blocked_issue(102, body="blocked by #6"),     # dismissal comment
        base._blocked_issue(103, body="blocked by #5"),     # gate-hold kind comment
        base._blocked_issue(104, body="blocked by #5"),     # plain closed blocker
    ]


def _golden_rig(monkeypatch):
    comments = {
        "101": [au.keep_parked_comment("on purpose")],
        "102": [au.backlog_check.dismiss_comment("blocked-by", 6, "not a real dependency")],
        "103": ["parked\n" + qkind.render_line("scope_hold")],
    }
    return Rig(monkeypatch, states={"5": "CLOSED"}, comments=comments)


def test_a_free_map_leaves_the_golden_set_identical_to_mode_off(tmp_path, monkeypatch):
    _store(tmp_path, [_node(n) for n in (101, 102, 103, 104)] + [_node(5, "closed"), _node(6)],
           [(5, 101), (5, 103), (5, 104)])
    off = _golden_rig(monkeypatch)
    expected = off.compute(tmp_path, "off", _golden_issues())
    on = _golden_rig(monkeypatch)
    got = on.compute(tmp_path, "on", _golden_issues())
    assert got == expected
    assert [a["issue"] for a in got[0]] != [] and "104" in got[1]
    assert (on.fetches, on.state_reads) == (off.fetches, off.state_reads) == (4, 2)


def test_a_blocker_only_in_a_comment_is_still_held(tmp_path, monkeypatch):
    rig = Rig(monkeypatch, comments={"301": ["blocked by #8"]})
    _store(tmp_path, [_node(301)], [])                      # the map knows 301 and holds nothing
    acts, resolved = rig.compute(tmp_path, "on", [base._blocked_issue(301)])
    assert (acts, resolved) == ([], {})
    assert rig.fetches == 1 and rig.state_reads == 1


def _unusable(sdlc, how):
    if how == "missing":
        return
    if how == "stale":
        _waiting_map(sdlc, 201, 7)
        _store(sdlc, [_node(201), _node(7)], [(7, 201)], built=NOW - 4 * 3600, checked=NOW - 3 * 3600)
    elif how == "no check":
        _store(sdlc, [_node(201), _node(7)], [(7, 201)], checked=None)
    elif how == "goal unknown":
        _store(sdlc, [_node(7)], [])
    elif how == "corrupt":
        d = pathlib.Path(sdlc) / "state"
        d.mkdir(parents=True, exist_ok=True)
        (d / "dag.json").write_text("{not json")


@pytest.mark.parametrize("how", ["missing", "stale", "no check", "goal unknown", "corrupt"])
def test_an_unusable_map_takes_the_full_check(tmp_path, monkeypatch, how):
    rig = Rig(monkeypatch)
    _unusable(tmp_path, how)
    acts, _ = rig.compute(tmp_path, "on", [base._blocked_issue(201, body="blocked by #7")])
    assert acts == [] and rig.fetches == 1 and rig.state_reads == 1


@pytest.mark.parametrize("mode", ["off", "shadow", "ON", "yes", None])
def test_any_mode_but_exactly_on_never_reads_the_map(tmp_path, monkeypatch, mode):
    rig = Rig(monkeypatch)
    _waiting_map(tmp_path, 201, 7)
    loaded = []
    real = au._load
    monkeypatch.setattr(au, "_load", lambda n: loaded.append(n) or real(n))
    acts, _ = rig.compute(tmp_path, mode, [base._blocked_issue(201, body="blocked by #7")])
    assert acts == [] and rig.fetches == 1 and rig.state_reads == 1
    assert "dag_pick" not in loaded


def test_a_parked_issue_is_never_skipped_by_the_map_and_never_resumed(tmp_path, monkeypatch):
    rig = Rig(monkeypatch)
    _waiting_map(tmp_path, 201, 7)          # the map shows an open blocker
    only = base._parked_issue(201, body="blocked by #7")
    both = base._parked_issue(202, body="blocked by #7", labels=("sdlc:parked", "sdlc:blocked"))
    _store(tmp_path, [_node(201), _node(202), _node(7)], [(7, 201), (7, 202)])
    acts, _ = rig.compute(tmp_path, "on", [only, both])
    assert acts == [] and rig.fetches == 2 and rig.state_reads == 1


def measure_25(tmp_path, monkeypatch, mode):
    """(comment fetches, state reads) for 25 blocked issues, each with its own open blocker."""
    rig = Rig(monkeypatch)
    goals = list(range(400, 425))
    _store(tmp_path, [_node(g) for g in goals] + [_node(g + 100) for g in goals],
           [(g + 100, g) for g in goals])
    issues = [base._blocked_issue(g, body="blocked by #%d" % (g + 100)) for g in goals]
    acts, _ = rig.compute(tmp_path, mode, issues)
    assert acts == []
    return rig.fetches, rig.state_reads


def test_25_blocked_issues_before_and_after(tmp_path, monkeypatch):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    before = measure_25(tmp_path / "a", monkeypatch, "off")
    after = measure_25(tmp_path / "b", monkeypatch, "on")
    print("25 issues: before (mode off) fetches=%d state_reads=%d; after (mode on) fetches=%d "
          "state_reads=%d" % (before + after))
    assert before == (25, 25) and after == (0, 0)
