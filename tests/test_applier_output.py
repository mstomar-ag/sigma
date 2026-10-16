"""#1564: a tick prints only actions taken on issues; ask bookkeeping is silent unless --verbose."""
import importlib.util
import pathlib

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _applier(monkeypatch, acts, opened=()):
    spec = importlib.util.spec_from_file_location("applier_t1564", SCRIPTS / "applier.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    monkeypatch.setattr(m, "tick", lambda *a, **k: acts)
    monkeypatch.setattr(m, "_read_config", lambda d: {})
    monkeypatch.setattr(m, "enabled", lambda c: True)
    monkeypatch.setattr(m, "sync", lambda *a, **k: list(opened))
    monkeypatch.setattr(m, "_load", lambda name: type("S", (), {"GitHubSource": staticmethod(lambda c: None)}))
    return m


def _run(m, *flags):
    return m.main(["applier.py", *flags, "x"])


def test_one_apply_prints_one_applied(monkeypatch, capsys):
    m = _applier(monkeypatch, [{"kind": "unpark", "result": "done"}])
    assert _run(m) == 0
    assert capsys.readouterr().out == "applier: 1 applied\n"


def test_arm_is_not_counted_as_applied(monkeypatch, capsys):
    m = _applier(monkeypatch, [{"kind": "arm", "result": "done"}, {"kind": "unpark", "result": "done"}])
    _run(m)
    assert capsys.readouterr().out == "applier: 1 applied\n"


def test_veto_prints_vetoed_only(monkeypatch, capsys):
    m = _applier(monkeypatch, [{"kind": "veto", "result": "done"}])
    _run(m)
    assert capsys.readouterr().out == "applier: 1 vetoed\n"


def test_sync_opened_asks_are_silent_without_verbose(monkeypatch, capsys):
    m = _applier(monkeypatch, [], opened=["a1", "a2"])
    _run(m)
    assert capsys.readouterr().out == ""


def test_verbose_prints_opened_and_arm_counts(monkeypatch, capsys):
    m = _applier(monkeypatch, [{"kind": "arm", "result": "done"}], opened=["a1"])
    _run(m, "--verbose")
    out = capsys.readouterr().out
    assert "applier: 1 ask(s) opened for existing parks" in out
    assert "applier: 1 ask(s) armed" in out
    assert "applied" not in out


def test_sync_verb_still_prints_opened(monkeypatch, capsys):
    m = _applier(monkeypatch, [], opened=["a1"])
    m.main(["applier.py", "sync", "x"])
    assert capsys.readouterr().out == "applier: 1 ask(s) opened for existing parks\n"


def test_no_action_tick_is_empty(monkeypatch, capsys):
    m = _applier(monkeypatch, [])
    _run(m)
    assert capsys.readouterr().out == ""
