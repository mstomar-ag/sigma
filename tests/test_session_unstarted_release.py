"""#1529: a session that ends (or dies) must not leave its claimed-but-never-started goals
labelled in-progress. Only goals listed in THAT session's own marker, and only those with no work
record and no agent marker, are released, through the same `_release` the over-capacity path uses.
Fake gh transport throughout; no network."""
import importlib.util, json, os, pathlib, subprocess, sys, tempfile, time

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"
sys.path.insert(0, str(S))


def _loop():
    spec = importlib.util.spec_from_file_location("loop_1529", S / "loop.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def _base(d, source="github", extra=None):
    base = pathlib.Path(d) / ".sdlc"; (base / "state").mkdir(parents=True)
    cfg = {"discovery": {"source": source, "github": {"repo": "acme/widget"}},
           "action_log": {"enabled": True}, "ledger": {"enabled": True, "actor": "me"},
           "budget": {"max_iterations": 10}}
    cfg.update(extra or {})
    (base / "config.json").write_text(json.dumps(cfg))
    (base / "state" / "STATE.md").write_text("iteration: 0\nrun_iteration: 0\nlast_run: none\n")
    (base / "ledger" / "entries").mkdir(parents=True)
    return str(base)


def _gh(calls, goals):
    def run(args):
        calls.append(list(args))
        if len(args) > 2 and args[0] == "issue" and args[1] == "view":
            return json.dumps({"state": "OPEN", "labels": [{"name": "sdlc:goal"},
                                                           {"name": "sdlc:in-progress"}]})
        return ""
    return run


def _wire(lp, base, calls):
    cfg = lp.state.load_config(base)
    src = lp.sources.GitHubSource(cfg, run=_gh(calls, []), sdlc_dir=base)
    lp.sources.get_source = lambda *a, **k: src
    return src


def _dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"]); p.wait(); return p.pid


def _register(lp, base, pid, goals, gen="g1"):
    lp.session_start(base, pid, generation=gen)
    for g in goals:
        lp._session_claim(base, pid, g)


def _comments(calls):
    out = []
    for c in calls:
        if len(c) > 1 and "comment" in c[:3] and "--body" in c:
            out.append(c[c.index("--body") + 1])
        elif len(c) > 1 and c[0] == "api" and "-f" in c and any("comments" in str(x) for x in c):
            out.append(" ".join(map(str, c)))
    return out


def _released_log(lp, base, goal):
    return [e for e in lp.actionlog.read_goal(base, goal) if e["kind"] == "released"]


def _mark_started(lp, base, goal):
    lp.work._save(base, goal, {"branch": "sdlc/%s" % goal})


def test_session_end_releases_every_unstarted_goal_with_comment_and_log():
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _base(d); calls = []; _wire(lp, base, calls)
        _register(lp, base, os.getpid(), ["11", "12"])
        lp.session_end(base, os.getpid(), generation="g1")
        bodies = " ".join(_comments(calls))
        logs = {g: _released_log(lp, base, g) for g in ("11", "12")}
        rel = [e for e in lp.ledger.read_all(base) if e["kind"] == "release"]
        assert "claimed but not started: session ended" in bodies
        assert all(len(v) == 1 for v in logs.values()), logs
        assert "session ended" in (logs["11"][0].get("reason") or "")
        assert logs["11"][0].get("actor")
        assert {e["goal"] for e in rel} == {"11", "12"}
        assert not lp._session_marker_path(base, os.getpid()).exists()


def test_session_end_leaves_a_started_goal_alone():
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _base(d); calls = []; _wire(lp, base, calls)
        _register(lp, base, os.getpid(), ["21", "22", "23"])
        _mark_started(lp, base, "21")                       # worktree/work record
        assert lp.agent_start(base, "22", os.getpid(), lp.state.load_config(base))  # heartbeat
        lp.session_end(base, os.getpid(), generation="g1")
        assert _released_log(lp, base, "21") == [] and _released_log(lp, base, "22") == []
        assert len(_released_log(lp, base, "23")) == 1


def test_a_bare_session_end_with_a_wrong_generation_releases_nothing():
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _base(d); calls = []; _wire(lp, base, calls)
        _register(lp, base, os.getpid(), ["31"])
        lp.session_end(base, os.getpid())                   # bare: not the owner
        lp.session_end(base, os.getpid(), generation="other")
        assert _released_log(lp, base, "31") == [] and _comments(calls) == []
        assert lp._session_marker_path(base, os.getpid()).exists()


def test_a_release_failure_never_raises_out_of_session_end():
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _base(d)
        class Boom:
            def release(self, *a, **k): raise RuntimeError("gh down")
        lp.sources.get_source = lambda *a, **k: Boom()
        _register(lp, base, os.getpid(), ["41"])
        lp.session_end(base, os.getpid(), generation="g1")  # must not raise
        assert not lp._session_marker_path(base, os.getpid()).exists()


def test_local_source_session_end_releases_nothing_and_is_unchanged():
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, source="local-goals"); calls = []
        called = []
        lp.sources.get_source = lambda *a, **k: called.append(1) or None
        _register(lp, base, os.getpid(), ["0001.md"])
        lp.session_end(base, os.getpid(), generation="g1")
        assert called == [] and lp.actionlog.read_goal(base, "0001.md") == []
        assert not lp._session_marker_path(base, os.getpid()).exists()
        assert not list((pathlib.Path(base) / "ledger" / "entries").glob("*.jsonl"))


def _age(path, minutes):
    t = time.time() - minutes * 60
    os.utime(path, (t, t))


def test_dead_session_past_grace_is_released_by_the_next_prune():
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _base(d); calls = []; _wire(lp, base, calls)
        pid = _dead_pid(); _register(lp, base, pid, ["51", "52"]); _mark_started(lp, base, "52")
        _age(lp._session_marker_path(base, pid), 20)
        lp._prune_dead_session_entries(base, lp.state.load_config(base))
        assert len(_released_log(lp, base, "51")) == 1
        assert "claimed but not started: session ended" in " ".join(_comments(calls))   # #1530
        assert "session ended" in (_released_log(lp, base, "51")[0].get("reason") or "")
        assert _released_log(lp, base, "52") == []          # started: untouched
        assert not lp._session_marker_path(base, pid).exists()


def test_dead_session_within_grace_is_untouched():
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _base(d); calls = []; _wire(lp, base, calls)
        pid = _dead_pid(); _register(lp, base, pid, ["61"])
        _age(lp._session_marker_path(base, pid), 5)
        lp._prune_dead_session_entries(base, lp.state.load_config(base))
        assert _released_log(lp, base, "61") == []
        assert lp._session_marker_path(base, pid).exists()


def test_grace_is_configurable_and_defaults_to_fifteen_minutes():
    lp = _loop()
    assert lp.session_release_grace_seconds({}) == 15 * 60
    assert lp.session_release_grace_seconds({"session_release": {"grace_minutes": 2}}) == 120
    assert lp.session_release_grace_seconds({"session_release": {"grace_minutes": "x"}}) == 15 * 60


def test_live_session_is_never_released_by_prune():
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _base(d); calls = []; _wire(lp, base, calls)
        _register(lp, base, os.getpid(), ["71"])
        _age(lp._session_marker_path(base, os.getpid()), 60)
        lp._prune_dead_session_entries(base, lp.state.load_config(base))
        assert _released_log(lp, base, "71") == [] and _comments(calls) == []


def test_cli_next_batch_then_session_end_releases_the_unworked_picks():
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, extra={"parallel": {"goals": {"enabled": True, "max_concurrent": 2}}})
        calls = []
        issues = [{"number": n, "labels": [{"name": "sdlc:goal"}]} for n in (81, 82)]
        def run(args):
            calls.append(list(args))
            if len(args) > 1 and args[0] == "api" and str(args[1]).startswith("repos/") \
                    and "comments" not in str(args[1]):
                return json.dumps(issues)
            if args[:2] == ["issue", "view"]:
                return json.dumps({"state": "OPEN", "labels": [{"name": "sdlc:in-progress"}]})
            if args[:2] == ["issue", "list"]:
                return json.dumps(issues)
            return ""
        cfg = lp.state.load_config(base)
        src = lp.sources.GitHubSource(cfg, run=run, sdlc_dir=base)
        lp.sources.get_source = lambda *a, **k: src
        pid = str(os.getpid())
        gen = None
        lp.main(["loop.py", "start", base, "--session-pid", pid, "--session-generation", "g9"])
        lp.main(["loop.py", "next-batch", base, "--session-pid", pid, "--session-generation", "g9"])
        held = [g for _p, _q, fl in lp._session_entries(base) for g in fl]
        assert held, "next-batch registered nothing; scenario is vacuous"
        lp.main(["loop.py", "session-end", base, "--session-pid", pid, "--session-generation", "g9"])
        assert all(len(_released_log(lp, base, g)) == 1 for g in held)


def test_failed_release_of_a_dead_session_keeps_the_entry_and_a_later_pick_retries():
    """#1530: host unreachable -> entry kept; the next pass releases once, then removes it."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _base(d); calls = []; real = _wire(lp, base, calls)
        pid = _dead_pid(); _register(lp, base, pid, ["71", "72"])
        _age(lp._session_marker_path(base, pid), 20)
        class Down:
            def release(self, *a, **k): raise RuntimeError("host unreachable")
        lp.sources.get_source = lambda *a, **k: Down()
        lp._prune_dead_session_entries(base, lp.state.load_config(base))
        assert lp._session_marker_path(base, pid).exists()
        assert _released_log(lp, base, "71") == [] and _comments(calls) == []
        lp.sources.get_source = lambda *a, **k: real
        lp._prune_dead_session_entries(base, lp.state.load_config(base))
        assert not lp._session_marker_path(base, pid).exists()
        assert len(_released_log(lp, base, "71")) == 1 and len(_released_log(lp, base, "72")) == 1
        n = len(_comments(calls))
        lp._prune_dead_session_entries(base, lp.state.load_config(base))
        assert len(_comments(calls)) == n == 2               # no double comment
