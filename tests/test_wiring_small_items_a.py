"""#1403 items 1-4: small wiring fixes (park message, applier retry line, comment-scan budget,
landing refusal reasons). One small test per item."""
import importlib.util
import pathlib

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _park_source(sources, rest_ok):
    src = object.__new__(sources.GitHubSource)
    src.parked_label, src.goal_label = "sdlc:parked", "sdlc:goal"
    src.in_progress_label, src.goal_blocked_label = "sdlc:in-progress", "sdlc:blocked"
    src.col = {"parked": "Parked", "blocked": "Blocked"}
    src._ensure_labels = lambda: None
    src._swap_labels = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("graphql down"))
    src._park_labels_rest = lambda goal: rest_ok
    src._repair_partial_park = lambda goal: None
    src.note = lambda goal, comment: None
    src._set_board_status = lambda *a, **k: True
    return src


def test_item1_park_silent_when_rest_fallback_lands(capsys):
    src = _park_source(_mod("sources"), rest_ok=True)
    assert src._offboard(7, "why", rest_fallback=True) is True
    assert "label write failed" not in capsys.readouterr().err


def test_item1_park_reports_when_both_fail(capsys):
    src = _park_source(_mod("sources"), rest_ok=False)
    assert src._offboard(7, "why", rest_fallback=True) is False
    assert "label write failed" in capsys.readouterr().err


def test_item1_park_without_fallback_still_reports(capsys):
    src = _park_source(_mod("sources"), rest_ok=True)
    src._offboard(7, "why", rest_fallback=False)
    assert "label write failed" in capsys.readouterr().err


def _applier_main(monkeypatch, acts):
    applier = _mod("applier")
    monkeypatch.setattr(applier, "tick", lambda *a, **k: acts)
    monkeypatch.setattr(applier, "_read_config", lambda d: {})
    return applier.main(["applier.py", "x"])


def test_item2_failed_asks_are_named_once_on_stderr(monkeypatch, capsys):
    rc = _applier_main(monkeypatch, [{"result": "failed"}, {"result": "failed"}, {"result": "done"}])
    cap = capsys.readouterr()
    assert rc == 0
    assert "applier: 2 asks failed and will be retried" in cap.err
    assert "1 answer(s) applied or vetoed" in cap.out


def test_item2_no_failures_prints_nothing_extra(monkeypatch, capsys):
    _applier_main(monkeypatch, [{"result": "done"}])
    cap = capsys.readouterr()
    assert cap.err == ""
    assert cap.out == "applier: 1 answer(s) applied or vetoed\n"


def test_item3_comment_scan_stops_at_the_time_budget(tmp_path):
    di = _mod("decision_import")
    ticks = iter(range(0, 1000, 100))          # each clock() call is 100 s later
    reads = []

    def read(config, issue):
        reads.append(issue)
        return []

    cfg = {"rulebook": {"import": {"comments": True, "time_budget_s": 150, "max_issues": 50}}}
    res = di.scan(str(tmp_path / ".sdlc"), cfg, profile_dirs=[], issues=list(range(1, 11)),
                  comments_fn=read, clock=lambda: next(ticks))
    assert len(reads) < 10
    assert any("time_budget_s" in r and "comment" in r for r in res["refusals"])


def test_item4_landing_refusal_keeps_the_pass_reason(tmp_path):
    from test_feature_land_front import World
    w = World(tmp_path)
    w.rebase_pass = lambda *a, **k: {"outcome": "no-base", "why": "work.base is not set, so nothing to measure"}
    out = w.land()
    assert out["outcome"] == "refused:pass-no-base"
    assert "work.base is not set" in out["detail"]
    assert out["detail"].startswith("the upkeep pass did not leave the unit current")
