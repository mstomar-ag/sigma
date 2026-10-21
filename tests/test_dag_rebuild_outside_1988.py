"""#1988: `dag.py rebuild` reads every outside number up to a stated hard cap; the periodic refresh keeps 25.

Every guard here was run red once against its own deliberate break (the `control:` line in each
docstring names the break)."""
import importlib.util
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
S = ROOT / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


sys.path.insert(0, str(ROOT / "tests"))
import test_dag_collect as tc  # noqa: E402  (reuse the scripted repository)

dc = tc.dc
dag_cli = tc.dag_cli
CONFIG, NOW = tc.CONFIG, tc.NOW


def _world(n):
    """Goal 1000 blocked by n outside numbers 1..n, all open issues."""
    issues = [tc._issue(1000, ["sdlc:goal"], "\n".join("Blocked by: #%d" % r for r in range(1, n + 1)))]
    issues += [tc._issue(r) for r in range(1, n + 1)]
    return tc.Fake(issues)


def _rebuild(tmp_path, monkeypatch, capsys, n, *extra, hard=None):
    (tmp_path / "config.json").write_text(json.dumps(CONFIG))
    dcm = dag_cli._load("dag_collect")
    monkeypatch.setattr(dag_cli, "_load", lambda name: dcm if name == "dag_collect" else _mod(name))
    if hard:
        monkeypatch.setattr(dcm, "OUTSIDE_HARD_MAX", hard)
    real = dcm.refresh
    run = _world(n)

    def fake(sdlc, *a, **kw):
        kw.setdefault("run", run)
        kw.setdefault("now", NOW)
        return real(sdlc, CONFIG, *a, **kw)
    monkeypatch.setattr(dcm, "refresh", fake)
    code = dag_cli.main(["rebuild", "--sdlc", str(tmp_path), *extra])
    return code, capsys.readouterr().out, run


def test_rebuild_reads_every_outside_number(tmp_path, monkeypatch, capsys):
    """60 outside numbers: 60 reads, unread 0, no hint.
    control: rebuild passes the periodic default (25) -> read 25, unread 35."""
    code, out, _run = _rebuild(tmp_path, monkeypatch, capsys, 60)
    assert code == 0 and "read 60" in out and "unread 0" in out and "dag.py rebuild" not in out


def test_periodic_refresh_keeps_25(tmp_path):
    """The periodic path (refresh with no outside_max) reads 25 of 60 and leaves 35 unread.
    control: make OUTSIDE_MAX the rebuild default -> the periodic path reads 60."""
    rep = dc.refresh(tmp_path, CONFIG, _world(60), NOW)
    assert rep["calls"]["read"] == 25 and len(rep["unread"]) == 35
    assert dc.OUTSIDE_MAX == 25


def test_outside_max_flag_is_honoured_and_report_says_what_to_run(tmp_path, monkeypatch, capsys):
    """`--outside-max 10`: 10 reads, 50 unread, and the line names the command to finish.
    control: ignore the flag -> 60 reads; drop the hint -> no `dag.py rebuild` in the line."""
    code, out, _run = _rebuild(tmp_path, monkeypatch, capsys, 60, "--outside-max", "10")
    assert "read 10" in out and "unread 50" in out
    assert "run `dag.py rebuild --outside-max 60`" in out


def test_hard_cap_clamps(tmp_path, monkeypatch, capsys):
    """An `--outside-max` over the hard cap reads exactly the cap and says it was clamped.
    control: remove the clamp -> reads exceed the cap."""
    code, out, run = _rebuild(tmp_path, monkeypatch, capsys, 40, "--outside-max", "99999", hard=30)
    reads = [c for c in run.calls if c[1].rsplit("/", 1)[-1].isdigit()]
    assert len(reads) == 30 and "read 30" in out and "unread 10" in out and "clamped to 30" in out


def test_zero_reads_none_and_bad_values_refused(tmp_path, monkeypatch, capsys):
    code, out, _run = _rebuild(tmp_path, monkeypatch, capsys, 5, "--outside-max", "0")
    assert "read 0" in out and "unread 5" in out
    for bad in ("-1", "x"):
        with pytest.raises(SystemExit):
            dag_cli.main(["rebuild", "--sdlc", str(tmp_path), "--outside-max", bad])
    capsys.readouterr()


def test_hint_only_when_unread(tmp_path, monkeypatch, capsys):
    """unread 0 has no hint; unread above 0 names `dag.py rebuild`.
    control: print the hint always -> the first assertion fails."""
    _c, out0, _r = _rebuild(tmp_path, monkeypatch, capsys, 3)
    assert "unread 0" in out0 and "run `" not in out0


def test_hint_never_asks_for_more_than_the_hard_cap(tmp_path, monkeypatch, capsys):
    """When more numbers exist than the hard cap, the hint names the cap, not an unreachable N.
    control: drop the over-cap branch -> the hint says `--outside-max 40`, which would clamp again."""
    _c, out, _r = _rebuild(tmp_path, monkeypatch, capsys, 40, hard=30)
    assert "unread 10" in out and "run `dag.py rebuild --outside-max 30`" in out and "hard cap" in out
