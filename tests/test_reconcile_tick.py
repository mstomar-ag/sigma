"""#2294: `reconcile.py`'s AUTOMATIC-tier sweep (and `_reconcile_sweep`'s own TTL gate) only ever
fires from inside `loop.py`'s `next_batch`/`_next` -- an idle repo with nobody driving
`/sigma-loop` gets zero board sanitation no matter how the TTL is configured (PC-4,
.sdlc/design/2287.md). `reconcile_tick.py` gives it a second, independent, wall-clock-driven
caller -- these tests prove it reuses `loop._reconcile_sweep` (and therefore its existing
`discovery.reconcile.mode`/`ttl_minutes` gate, TTL watermark, and fail-open contract) exactly,
rather than re-deriving any of that here."""
import importlib.util
import json
import pathlib

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


reconcile_tick = _mod("reconcile_tick")
loop = reconcile_tick.loop


def _sdlc(tmp_path, config=None):
    d = tmp_path / ".sdlc"
    (d / "state").mkdir(parents=True)
    (d / "config.json").write_text(json.dumps(config if config is not None else {}))
    return d


def _with_reconcile(d, mode="on", ttl_minutes=60):
    cfg = json.loads((d / "config.json").read_text())
    cfg.setdefault("discovery", {})["reconcile"] = {"mode": mode, "ttl_minutes": ttl_minutes}
    (d / "config.json").write_text(json.dumps(cfg))
    return cfg


class FakeReconcile:
    """Mirrors test_loop.py's own `FakeReconcile` shape exactly (see
    test_reconcile_sweep_runs_when_enabled_and_due et al.) -- reconcile_tick.py is exercised
    through the SAME seam `_reconcile_sweep` already is, not a second, independently-mocked one."""

    def __init__(self, actions=None, raise_exc=None):
        self.calls = []
        self._actions = actions if actions is not None else []
        self._raise = raise_exc

    def sweep_reconcile(self, sdlc_dir, config, apply=False, run=None):
        self.calls.append((sdlc_dir, apply))
        if self._raise:
            raise self._raise
        return {"actions": self._actions}


def _patch_reconcile(monkeypatch, fake):
    real_load = loop._load
    monkeypatch.setattr(loop, "_load", lambda n: fake if n == "reconcile" else real_load(n))


# ------------------------------------------------------------------ tick()


def test_tick_calls_reconcile_sweep_with_apply_true_reusing_config(tmp_path, monkeypatch):
    d = _sdlc(tmp_path)
    _with_reconcile(d)
    fake = FakeReconcile(actions=[{"issue": "42", "result": "done"}])
    _patch_reconcile(monkeypatch, fake)
    summary = reconcile_tick.tick(str(d))
    assert fake.calls == [(str(d), True)]     # apply=True, same sdlc_dir -- no re-derived config
    assert "#42" in summary


def test_tick_is_a_cheap_noop_when_reconcile_mode_is_off(tmp_path, monkeypatch):
    """Default off -- no `reconcile.py` import, no `gh` calls, matching the byte-identical-when-off
    discipline every other opt-in mechanism in this kit holds (mirrors test_loop.py's
    test_reconcile_mode_defaults_to_off_and_imports_nothing). Proven structurally: `loop._load` is
    made to raise if reconcile_tick even asks for the reconcile module."""
    d = _sdlc(tmp_path)   # discovery.reconcile.mode unset -> defaults to "off"

    def boom(name):
        raise AssertionError(f"must not _load({name!r}) when reconcile.mode is off")

    monkeypatch.setattr(loop, "_load", boom)
    assert reconcile_tick.tick(str(d)) == ""


def test_tick_stays_off_on_an_unrecognised_mode_value(tmp_path, monkeypatch):
    d = _sdlc(tmp_path)
    _with_reconcile(d, mode="always")
    monkeypatch.setattr(loop, "_load", lambda name: (_ for _ in ()).throw(AssertionError("no")))
    assert reconcile_tick.tick(str(d)) == ""


def test_tick_is_idempotent_within_the_shared_ttl_window(tmp_path, monkeypatch):
    """The SAME TTL watermark `_reconcile_sweep` already writes -- calling `tick()` twice in quick
    succession must sweep once, exactly as two calls to `_reconcile_sweep` itself already do
    (test_loop.py: test_reconcile_sweep_is_throttled_by_the_ttl_watermark). This is what makes it
    safe to call on every 900s watch.sh tick without amplifying `gh` cost."""
    d = _sdlc(tmp_path)
    _with_reconcile(d, ttl_minutes=60)
    fake = FakeReconcile(actions=[])
    _patch_reconcile(monkeypatch, fake)
    reconcile_tick.tick(str(d))
    reconcile_tick.tick(str(d))
    assert len(fake.calls) == 1


def test_tick_fails_open_when_the_underlying_sweep_raises(tmp_path, monkeypatch, capsys):
    """A sweep failure must never crash the watch.sh loop -- matches agent_watch.py/comment_watch.py's
    own fail-open contract (and reuses `_reconcile_sweep`'s own, already tested at
    test_reconcile_sweep_fails_open_when_the_sweep_raises)."""
    d = _sdlc(tmp_path)
    _with_reconcile(d)
    fake = FakeReconcile(raise_exc=RuntimeError("simulated transient gh failure"))
    _patch_reconcile(monkeypatch, fake)
    assert reconcile_tick.tick(str(d)) == ""       # no raise
    assert "reconcile sweep failed non-fatally" in capsys.readouterr().err


# ------------------------------------------------------------------ CLI


def test_main_cli_runs_a_tick_and_prints_the_summary(tmp_path, capsys, monkeypatch):
    d = _sdlc(tmp_path)
    _with_reconcile(d)
    fake = FakeReconcile(actions=[{"issue": "7", "result": "done"}])
    _patch_reconcile(monkeypatch, fake)
    assert reconcile_tick.main(["reconcile_tick.py", str(d)]) == 0
    assert "#7" in capsys.readouterr().out


def test_main_cli_is_quiet_when_reconcile_mode_is_off(tmp_path, capsys):
    d = _sdlc(tmp_path)
    assert reconcile_tick.main(["reconcile_tick.py", str(d)]) == 0
    assert capsys.readouterr().out.strip() == ""


def test_main_cli_is_never_fatal_even_when_tick_raises(monkeypatch, capsys):
    """Matches agent_watch.py/comment_watch.py's own main() contract exactly -- a watcher tick is
    never fatal, an unexpected exception prints non-fatally and returns 1, never a raw traceback."""
    def boom(_sdlc_dir):
        raise RuntimeError("boom")

    monkeypatch.setattr(reconcile_tick, "tick", boom)
    assert reconcile_tick.main(["reconcile_tick.py", "/nonexistent"]) == 1
    assert "tick failed (non-fatal): boom" in capsys.readouterr().err


def test_main_cli_defaults_sdlc_dir_when_omitted(tmp_path, monkeypatch, capsys):
    """Matches agent_watch.py/comment_watch.py's own `argv[1] if len(argv) > 1 else ".sdlc"` idiom."""
    seen = []
    monkeypatch.setattr(reconcile_tick, "tick", lambda d: seen.append(d) or "")
    reconcile_tick.main(["reconcile_tick.py"])
    assert seen == [".sdlc"]


def test_tick_board_backfill_gate_closed_is_silent(tmp_path):
    """#1305: a local-goals source (or a board that is off) adds nothing to the summary and runs no gh."""
    calls = []
    d = _sdlc(tmp_path)
    assert reconcile_tick.tick(d, config={}, run=lambda a: calls.append(a) or "") == ""
    assert calls == []


def test_tick_runs_the_board_backfill_only_when_the_board_is_enabled(tmp_path, monkeypatch):
    """#1305: github source + project enabled -> the watcher back-fill runs and is reported."""
    d = _sdlc(tmp_path)
    cfg = {"discovery": {"source": "github", "github": {"project": {"enabled": True}}}}
    seen = []
    real = reconcile_tick._load("sources")
    monkeypatch.setattr(real.GitHubSource, "backfill_board", lambda self, cap=None: seen.append(1) or 3)
    monkeypatch.setattr(reconcile_tick, "_load", lambda name: real)
    assert "3 board card(s) backfilled" in reconcile_tick.tick(d, config=cfg, run=lambda a: "[]")
    off = {"discovery": {"source": "github", "github": {"project": {"enabled": False}}}}
    seen.clear()
    reconcile_tick.tick(d, config=off, run=lambda a: "[]")
    assert seen == []
