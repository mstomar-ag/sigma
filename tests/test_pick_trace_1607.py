"""#1607: the pick trace. Counts runner calls at the one transport seam and times named stages of
`loop._next`; off by default. Each guard below was run red once against a deliberate break (see the
`control:` note in each docstring)."""
import importlib.util
import json
import pathlib
import types

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"
PICKING = S.parent / "references" / "picking.md"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _base(tmp_path):
    base = tmp_path / ".sdlc"
    (base / "state").mkdir(parents=True)
    (base / "config.json").write_text(json.dumps({"budget": {"max_iterations": 10}}))
    (base / "state" / "STATE.md").write_text("iteration: 0\nrun_iteration: 0\nlast_run: none\n")
    return base


def _source(sources, calls, base):
    def run(args):
        calls.append(list(args))
        return "{}"
    cfg = {"source": "github", "github": {"repo": "o/r"}}

    class Scripted(sources.GitHubSource):
        script = None

        def next_pending(self, skip=()):
            if self.script is not None:
                self.script("pending_fetch")
            return "5" if "5" not in {str(s) for s in skip} else None

    return Scripted(cfg, run=run, sdlc_dir=str(base))


# one scripted call of each class, reached through the source's real `_run`
MIX = {
    "list": ["issue", "list", "--json", "number"],
    "read": ["api", "repos/o/r/issues/1", "--method", "GET"],
    "write": ["api", "repos/o/r/issues/1/comments", "--method", "POST"],
    "graphql": ["api", "graphql", "-f", "query=x"],
    "project": ["project", "item-list", "1"],
}
PLAN = {   # stage -> classes it issues
    "reconcile": ["list", "read"],
    "auto_unpark": ["read"],
    "reclaim": ["write", "write", "graphql"],
    "release_unstarted": ["project"],
    "pending_fetch": ["list", "list", "graphql"],
    "dependency_hold": ["read", "read", "read"],
    "gates": ["graphql", "write"],
    "claim_writes": ["write", "project", "project"],
}


def _wire(loop, source, clock=None, advance=None):
    """Replace each stage's work with its scripted runner calls, through the real `source._run`."""
    def stage_fn(name, ret=None):
        def fn(*a, **k):
            for c in PLAN[name]:
                source._run(MIX[c])
            if clock is not None:
                clock[0] += advance.get(name, 0.0)
            return ret
        return fn
    source.script = lambda name: stage_fn(name)()
    loop._reconcile_sweep = stage_fn("reconcile")
    loop._auto_unpark_sweep = stage_fn("auto_unpark", frozenset())
    loop._auto_reclaim_stale_claims = stage_fn("reclaim", frozenset())
    loop._release_held_unstarted = stage_fn("release_unstarted", frozenset())
    loop._pick_dependency_hold = stage_fn("dependency_hold", None)
    loop.feature_labels.attach_at_pick = lambda *a, **k: types.SimpleNamespace(proceed=True)
    loop._unit_at_pick = lambda *a, **k: None
    loop._feature_scope_ok = lambda *a, **k: True
    loop._label_scope_ok = lambda *a, **k: True
    loop._scope_ok_at_pick = lambda *a, **k: (stage_fn("gates")(), True)[1]
    loop._owner_ok_at_pick = lambda *a, **k: True
    loop._claim = stage_fn("claim_writes")
    loop._check_cross_repo_at_pick = lambda *a, **k: None
    loop._predict_model_choice_at_pick = lambda *a, **k: None


def _pick(loop, base, source):
    return loop._next(str(base), source, loop.state.load_config(str(base)), session_pid=4242,
                      refresh_heartbeat=True, gate=False)


def _lines(base):
    p = base / "state" / "pick-trace.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_flag_unset_writes_no_file_and_adds_no_call(tmp_path, monkeypatch):
    """control: making the default flag-on (enabled() True) writes a file and this goes red."""
    monkeypatch.delenv("SIGMA_PICK_TRACE", raising=False)
    loop, sources = _mod("loop"), _mod("sources")
    base = _base(tmp_path)
    calls = []
    src = _source(sources, calls, base)
    _wire(loop, src)
    first = _pick(loop, base, src)
    off_calls = list(calls)
    assert first == ("goal", "5")
    assert not (base / "state" / "pick-trace.jsonl").exists()
    assert src.call_counter is None
    # the same pick with the trace ON makes the identical runner call list
    monkeypatch.setenv("SIGMA_PICK_TRACE", "1")
    calls2 = []
    base2 = _base(tmp_path / "b")
    src2 = _source(sources, calls2, base2)
    loop2 = _mod("loop")
    _wire(loop2, src2)
    _pick(loop2, base2, src2)
    assert calls2 == off_calls
    assert _lines(base2)


def test_per_stage_counts_equal_the_script(tmp_path, monkeypatch):
    """control: un-wrapping the seam (`_raw` calling the runner without the counter) goes red."""
    monkeypatch.setenv("SIGMA_PICK_TRACE", "1")
    loop, sources = _mod("loop"), _mod("sources")
    base = _base(tmp_path)
    calls = []
    src = _source(sources, calls, base)
    _wire(loop, src)
    assert _pick(loop, base, src) == ("goal", "5")
    (row,) = _lines(base)
    for stage, classes in PLAN.items():
        want = {}
        for c in classes:
            want[c] = want.get(c, 0) + 1
        if stage == "gates":
            want["read"] = want.get("read", 0) + 1   # #2022: the claim's unconditional label re-read (not scripted)
        assert row["stages"][stage]["calls"] == want, stage
    total = sum(sum(s["calls"].values()) for s in row["stages"].values())
    assert total == len(calls)                       # exactly once per runner call, none added


def test_a_project_retry_counts_each_runner_call_once(tmp_path):
    sources, pt = _mod("sources"), _mod("pick_trace")
    n = []

    def run(args):
        n.append(1)
        if len(n) < 2:
            raise RuntimeError("HTTP 502 bad gateway")
        return "{}"
    src = sources.GitHubSource({"source": "github", "github": {"repo": "o/r"}}, run=run)
    rec = pt.Recorder(tmp_path)
    src.call_counter = rec.count_args
    src._is_transient = lambda e: True
    src._RETRY_BASE = 0
    src._run(["project", "item-list", "1"])
    assert len(n) == 2 and rec.stages["other"]["project"] == 2


def test_stage_seconds_add_up_to_wall_time(tmp_path, monkeypatch):
    """control: a `mark` that does not charge elapsed time leaves the stages empty and goes red."""
    monkeypatch.setenv("SIGMA_PICK_TRACE", "1")
    loop, sources = _mod("loop"), _mod("sources")
    base = _base(tmp_path)
    clock = [100.0]
    advance = {"reconcile": 2.0, "auto_unpark": 1.0, "reclaim": 3.0, "release_unstarted": 0.5,
               "pending_fetch": 4.0, "dependency_hold": 1.5, "gates": 2.5, "claim_writes": 6.0}
    src = _source(sources, [], base)
    _wire(loop, src, clock, advance)
    loop.pick_trace._clock = lambda: clock[0]
    _pick(loop, base, src)
    (row,) = _lines(base)
    total = sum(s["seconds"] for s in row["stages"].values())
    assert row["wall"] > 0 and abs(total - row["wall"]) <= 0.05 * row["wall"]
    for stage, secs in advance.items():
        assert abs(row["stages"][stage]["seconds"] - secs) < 1e-6, stage


FIXTURE = [
    {"v": 1, "ts": 1, "wall": 3.0, "stages": {"reconcile": {"calls": {"list": 2}, "seconds": 1.0},
                                              "claim_writes": {"calls": {"write": 1}, "seconds": 2.0}}},
    {"v": 1, "ts": 2, "wall": 5.0, "stages": {"reconcile": {"calls": {"list": 4}, "seconds": 3.0},
                                              "claim_writes": {"calls": {"write": 3}, "seconds": 2.0}}},
    {"v": 1, "ts": 3, "wall": 9.0, "stages": {"reconcile": {"calls": {"list": 6, "read": 2}, "seconds": 5.0},
                                              "other": {"calls": {}, "seconds": 4.0}}},
]
GOLDEN = """pick trace: 3 pick(s)
stage               calls_p50  calls_p90   secs_p50   secs_p90
reconcile                 4.0        8.0      3.000      5.000
claim_writes              1.0        3.0      2.000      2.000
other                     0.0        0.0      0.000      4.000
wall                        -          -      5.000      9.000
"""


def test_report_golden_output(tmp_path):
    pt = _mod("pick_trace")
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "pick-trace.jsonl").write_text("".join(json.dumps(r) + "\n" for r in FIXTURE))
    assert pt.report(tmp_path) == GOLDEN
    assert "no picks recorded" in pt.report(tmp_path / "missing")


def test_report_verb_prints_the_report(tmp_path, capsys):
    loop = _mod("loop")
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "pick-trace.jsonl").write_text("".join(json.dumps(r) + "\n" for r in FIXTURE))
    assert loop.main(["loop.py", "pick-trace-report", str(tmp_path)]) == 0
    assert capsys.readouterr().out == GOLDEN


def test_trace_flag_forces_the_trace_on(monkeypatch):
    monkeypatch.delenv("SIGMA_PICK_TRACE", raising=False)
    pt = _mod("pick_trace")
    assert not pt.enabled({}, False) and pt.enabled({}, True) and pt.enabled({"SIGMA_PICK_TRACE": "1"})
    assert not pt.enabled({"SIGMA_PICK_TRACE": "0"})


def test_file_rotates_at_one_megabyte(tmp_path):
    """control: raising the threshold above the planted size leaves no `.1` and goes red."""
    pt = _mod("pick_trace")
    st = tmp_path / "state"
    st.mkdir()
    old = "x" * (1024 * 1024) + "\n"
    (st / "pick-trace.jsonl").write_text(old)
    rec = pt.Recorder(tmp_path)
    rec.end()
    assert (st / "pick-trace.jsonl.1").read_text() == old
    assert len((st / "pick-trace.jsonl").read_text().splitlines()) == 1
    rec2 = pt.Recorder(tmp_path)
    rec2.end()
    assert len((st / "pick-trace.jsonl").read_text().splitlines()) == 2    # below 1 MB: appends


def test_trace_holds_no_issue_text(tmp_path):
    pt = _mod("pick_trace")
    rec = pt.Recorder(tmp_path)
    rec.mark("gates")
    rec.count_args(["api", "repos/o/r/issues/1/comments", "--method", "POST", "-f", "body=SECRET TEXT"])
    rec.end()
    assert "SECRET" not in (tmp_path / "state" / "pick-trace.jsonl").read_text()


def test_parallelism_doc_documents_the_trace():
    """The detail left the orchestrator payload (picking.md) to hold its context ceiling (#1850)."""
    text = (S.parents[2] / "docs" / "parallelism.md").read_text()
    for needle in ("SIGMA_PICK_TRACE", "--trace", "pick-trace-report"):
        assert needle in text, needle


def test_a_serialiser_failure_leaves_the_pick_result_unchanged(tmp_path, monkeypatch):
    """#1815. control: remove the try/except in `Recorder.end` and this goes red (the raise from
    `loop._next`'s `finally` replaces the pick's result)."""
    monkeypatch.setenv("SIGMA_PICK_TRACE", "1")
    loop, sources = _mod("loop"), _mod("sources")
    base = _base(tmp_path)
    src = _source(sources, [], base)
    _wire(loop, src)

    def boom(*a, **k):
        raise ValueError("serialiser down")
    monkeypatch.setattr(loop.pick_trace.json, "dumps", boom)
    assert _pick(loop, base, src) == ("goal", "5")
    assert not _lines(base)


def test_a_failing_call_count_hook_is_dropped(tmp_path):
    """#1815. control: remove the try/except in `Recorder.count_args` and this goes red."""
    pt = _mod("pick_trace")
    rec = pt.Recorder(tmp_path)
    rec.stages = None   # any failure while counting, whatever its cause
    rec.count_args(["api", "x"])
    rec.end()
