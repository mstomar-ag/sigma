"""#1624: DAG.md on the ledger branch, `dag.py history --all`, and the doctor's `dag ledger` row.

Real git, a real bare origin; no network, no model. Controls, each run red once on its own clause:
  * drop the `regenerate` of DAG.md after a rebase           -> the conflict test red
  * drop the `ledger.enabled` gate in `_write_dag`           -> the ledger-off test red
  * drop the mode-off gate in `_write_dag`                   -> the mode-off test red
  * drop the corrupt-line skip in `read_all`                 -> the history --all test red
"""
import importlib.util
import json
import os
import pathlib
import subprocess
import sys

import pytest

import resource_mode

ROOT = pathlib.Path(__file__).resolve().parent.parent
S = ROOT / "skills" / "sigma-loop" / "scripts"


def _mod(name, base=S):
    spec = importlib.util.spec_from_file_location(name, base / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


sync, ledger, dag_history, dag_render = _mod("sync"), _mod("ledger"), _mod("dag_history"), _mod("dag_render")
doctor = _mod("doctor", ROOT / "skills" / "sigma-doctor" / "scripts")

T = "2026-03-05T10:00:00Z"
T2 = "2026-03-06T10:00:00Z"


def _git(cwd, *args):
    p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr or p.stdout
    return p.stdout.strip()


def _config(actor="dana", mode="shadow", enabled=True):
    return {"ledger": {"enabled": enabled, "actor": actor}, "discovery": {"dag": {"mode": mode}}}


def _clone(tmp_path, name, origin, config):
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    for k, v in (("user.email", "t@example.com"), ("user.name", "T"), ("commit.gpgsign", "false")):
        _git(repo, "config", k, v)
    _git(repo, "remote", "add", "origin", str(origin))
    (repo / ".gitignore").write_text(".sdlc/\n", encoding="utf-8")
    _git(repo, "add", "--", ".gitignore")
    _git(repo, "commit", "-q", "-m", "base")
    base = repo / ".sdlc"
    (base / "state").mkdir(parents=True)
    (base / "config.json").write_text(json.dumps(config), encoding="utf-8")
    return base


@pytest.fixture
def origin(tmp_path):
    o = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(o)], check=True)
    return o


def _boot(base, cfg):
    assert "published" in sync.bootstrap(base, cfg)


def _rec(t, ev, **kw):
    return dict({"t": t, "ev": ev}, **kw)


def _remote_dag(origin):
    return _git(origin, "show", "sdlc-ledger:DAG.md")  # the ledger branch


def test_two_clones_later_observation_wins_in_published_dag_md(tmp_path, origin):
    ca, cb = _config("dana"), _config("eli")
    a = _clone(tmp_path, "a", origin, ca)
    _boot(a, ca)
    b = _clone(tmp_path, "b", origin, cb)
    _boot(b, cb)
    dag_history.append(str(a), ca, [_rec(T, "edge-added", a=1, b=2), _rec(T, "edge-added", a=7, b=8)])
    dag_history.append(str(b), cb, [_rec(T2, "edge-removed", a=1, b=2)])
    sync.publish(b, cb)                                  # the LATER observation lands first
    sync.publish(a, ca)
    text = _remote_dag(origin)
    stuck = text.split("## Stuck chains")[1].split("##")[0]
    assert "#7" in stuck and "#1" not in stuck
    assert text == dag_render.render(dag_history.read_all(a / "ledger")).strip()   # _git strips


def test_rebase_conflict_on_dag_md_is_regenerated_from_merged_files(tmp_path, origin):
    ca, cb = _config("dana"), _config("eli")
    a = _clone(tmp_path, "a", origin, ca)
    _boot(a, ca)
    b = _clone(tmp_path, "b", origin, cb)
    _boot(b, cb)
    dag_history.append(str(a), ca, [_rec(T, "edge-added", a=1, b=2)])
    dag_history.append(str(b), cb, [_rec(T, "edge-added", a=5, b=6)])
    sync.publish(a, ca)
    out = sync.publish(b, cb)                            # b's DAG.md conflicts with a's, then is rebuilt
    assert "nothing" not in out, out
    text = _remote_dag(origin)
    assert "#1 holds 1" in text and "#5 holds 1" in text
    names = _git(origin, "ls-tree", "-r", "--name-only", "sdlc-ledger").splitlines()  # the ledger branch
    assert len([n for n in names if n.startswith("dag/")]) == 2


def test_ledger_off_generates_nothing_and_no_doctor_row(tmp_path, origin):
    cfg = _config(enabled=False)
    base = _clone(tmp_path, "a", origin, cfg)
    wt = base / "ledger"
    (wt / "dag" / "2026-03").mkdir(parents=True)
    (wt / "dag" / "2026-03" / "dana-abc12345.1.jsonl").write_text(json.dumps(_rec(T, "edge-added", a=1, b=2)) + "\n")
    assert sync._write_dag(wt, cfg) is False
    assert not (wt / "DAG.md").exists()
    assert doctor._dag_ledger_row(cfg, str(base)) is None


def test_mode_off_generates_nothing_and_no_doctor_row(tmp_path, origin):
    cfg = _config()
    base = _clone(tmp_path, "a", origin, cfg)
    _boot(base, cfg)
    dag_history.append(str(base), cfg, [_rec(T, "edge-added", a=1, b=2)])
    off = _config(mode="off")
    assert sync._write_dag(base / "ledger", off) is False
    assert not (base / "ledger" / "DAG.md").exists()
    assert doctor._dag_ledger_row(off, str(base)) is None


def test_no_dag_files_means_no_empty_dag_md_and_no_row(tmp_path, origin):
    cfg = _config()
    base = _clone(tmp_path, "a", origin, cfg)
    _boot(base, cfg)
    assert sync._write_dag(base / "ledger", cfg) is False
    assert not (base / "ledger" / "DAG.md").exists()
    assert doctor._dag_ledger_row(cfg, str(base)) is None


def test_doctor_row_counts_writers_and_age(tmp_path, origin):
    cfg = _config()
    base = _clone(tmp_path, "a", origin, cfg)
    _boot(base, cfg)
    d = base / "ledger" / "dag" / "2026-03"
    d.mkdir(parents=True)
    for name in ("dana-aaaa1111.1", "dana-aaaa1111.2", "eli-bbbb2222.9"):
        (d / (name + ".jsonl")).write_text(json.dumps(_rec(T, "edge-added", a=1, b=2)) + "\n")
    (d / "eli-bbbb2222.9.jsonl").write_text(json.dumps(_rec(T2, "edge-added", a=3, b=4)) + "\n")
    row = doctor._dag_ledger_row(cfg, str(base), now=1772755200 + 3600)   # 2026-03-06T00:00:00Z + 1h
    assert row is not None
    assert "2 writers" in str(row) and "newest record" in str(row)


def test_history_all_reads_every_writer_and_skips_corrupt_lines(tmp_path, origin, capsys):
    cfg = _config()
    base = _clone(tmp_path, "a", origin, cfg)
    _boot(base, cfg)
    d = base / "ledger" / "dag"
    (d / "2026-02").mkdir(parents=True)
    (d / "2026-03").mkdir()
    (d / "2026-02" / "eli-bbbb2222.9.jsonl").write_text(json.dumps(_rec(T, "edge-added", a=3, b=4)) + "\n{not json\n")
    (d / "2026-03" / "dana-aaaa1111.1.jsonl").write_text("garbage\n" + json.dumps(_rec(T2, "edge-added", a=1, b=2)) + "\n[1]\n")
    dag = _mod("dag")
    out, code = dag.history(str(base), all_writers=True)
    assert code == 0
    got = [json.loads(x) for x in out.splitlines()]
    assert sorted(r["a"] for r in got) == [1, 3]


def test_history_all_with_ledger_off_says_so(tmp_path, origin):
    cfg = _config(enabled=False)
    base = _clone(tmp_path, "a", origin, cfg)
    out, code = _mod("dag").history(str(base), all_writers=True)
    assert code == 0 and "ledger is off" in out


def test_history_all_cli_flag_runs_in_a_scrubbed_env(tmp_path, origin):
    cfg = _config(enabled=False)
    base = _clone(tmp_path, "a", origin, cfg)
    env = resource_mode.carry({"PATH": os.environ.get("PATH", "")})
    p = subprocess.run([sys.executable, str(S / "dag.py"), "history", "--all", "--sdlc", str(base)],
                       capture_output=True, text=True, env=env)
    assert p.returncode == 0 and "ledger is off" in p.stdout, p.stderr
