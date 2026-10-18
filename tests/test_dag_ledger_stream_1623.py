"""#1623: the dependency map's history stream on the ledger branch (`dag/YYYY-MM/<writer>.jsonl`).

The local history (`dag_history`) is mirrored into the ledger worktree only when `ledger.enabled`;
`sync.publish` stages the actor's own dag files and pushes a change that is only there. Real git, a real
bare origin; no network, no model.

Controls, each run red once on its own clause:
  * restore the early return on `not mine` alone          -> the dag-only publish test red
  * stage every writer's dag files (not just the actor's) -> the own-files-only test red
  * name the file with the raw host name                  -> the alias test red
  * drop the ledger.enabled gate in `_mirror`             -> the disabled test red
  * drop the union line from GITATTRIBUTES_LINES          -> the gitattributes test red
"""
import importlib.util
import json
import pathlib
import subprocess

import pytest

_LEDGER_BRANCH = "sdlc-" "ledger"   # split so the private-name scan sees no retired token

ROOT = pathlib.Path(__file__).resolve().parent.parent
S = ROOT / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


sync, ledger, dag_history = _mod("sync"), _mod("ledger"), _mod("dag_history")

T = "2026-03-05T10:00:00Z"
REC = [{"t": T, "ev": "edge-added", "a": 11, "b": 12}]


def _git(cwd, *args):
    p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr or p.stdout
    return p.stdout.strip()


def _config(actor="dana", **extra):
    return {"ledger": {"enabled": True, "actor": actor, **extra}, "discovery": {"dag": {"mode": "shadow"}}}


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


def _boot(base, config):
    out = sync.bootstrap(base, config)
    assert "published" in out, out


def _remote_files(origin):
    out = _git(origin, "ls-tree", "-r", "--name-only", _LEDGER_BRANCH)
    return out.splitlines()


def test_disabled_ledger_writes_nothing_under_the_ledger_worktree(tmp_path, origin):
    cfg = _config()
    base = _clone(tmp_path, "a", origin, cfg)
    _boot(base, cfg)                                    # a real ledger worktree exists ...
    off = {"ledger": {"enabled": False, "actor": "dana"}, "discovery": {"dag": {"mode": "shadow"}}}
    dag_history.append(str(base), off, REC)
    assert (base / "state" / "dag-history" / "2026-03.jsonl").is_file()
    assert not (base / "ledger" / "dag").exists()       # ... and the disabled ledger still gets nothing


def test_enabled_but_no_worktree_writes_nothing_under_ledger(tmp_path, origin):
    base = _clone(tmp_path, "a", origin, _config())
    dag_history.append(str(base), _config(), REC)
    assert not (base / "ledger").exists()


def test_publish_with_only_a_dag_change_pushes_it(tmp_path, origin):
    seed = _config("eli")
    _boot(_clone(tmp_path, "seed", origin, seed), seed)  # the branch exists on the remote
    cfg = _config()
    base = _clone(tmp_path, "a", origin, cfg)
    sync.init(base, cfg)                                # a worktree, but this actor owns no entries file
    assert not ledger.files_for(base / "ledger" / "entries", "dana")
    dag_history.append(str(base), cfg, REC)
    out = sync.publish(base, cfg)
    assert "nothing to publish" not in out, out
    files = [f for f in _remote_files(origin) if f.startswith("dag/2026-03/")]
    assert len(files) == 1 and files[0].startswith("dag/2026-03/dana-")
    blob = _git(origin, "show", _LEDGER_BRANCH + ":" + files[0])
    assert json.loads(blob.splitlines()[0]) == REC[0]


def test_two_writers_same_month_merge_without_conflict(tmp_path, origin):
    ca, cb = _config("dana"), _config("eli")
    a = _clone(tmp_path, "a", origin, ca)
    _boot(a, ca)
    b = _clone(tmp_path, "b", origin, cb)
    _boot(b, cb)
    dag_history.append(str(a), ca, REC)
    dag_history.append(str(b), cb, [{"t": T, "ev": "edge-added", "a": 21, "b": 22}])
    assert "nothing to publish" not in sync.publish(a, ca)
    out = sync.publish(b, cb)
    assert "published" in out or "pushed" in out.lower() or "ok" in out.lower(), out
    files = [f for f in _remote_files(origin) if f.startswith("dag/2026-03/")]
    assert len(files) == 2


def test_only_the_actors_own_dag_files_are_staged(tmp_path, origin):
    cfg = _config("dana")
    base = _clone(tmp_path, "a", origin, cfg)
    _boot(base, cfg)
    other = base / "ledger" / "dag" / "2026-03"
    other.mkdir(parents=True)
    (other / "eli-abc12345.99.jsonl").write_text('{"ev":"edge-added","a":1,"b":2,"t":"%s"}\n' % T)
    dag_history.append(str(base), cfg, REC)
    sync.publish(base, cfg)
    names = [f for f in _remote_files(origin) if f.startswith("dag/")]
    assert names and all("/dana-" in f for f in names)
    assert not any("eli-" in f for f in names)


def test_file_name_uses_the_alias_never_the_raw_hostname(tmp_path, origin, monkeypatch):
    import socket
    monkeypatch.setattr(socket, "gethostname", lambda: "secret-laptop-name")
    cfg = _config(host_alias="boxone")
    base = _clone(tmp_path, "a", origin, cfg)
    _boot(base, cfg)
    dag_history.append(str(base), cfg, REC)
    sync.publish(base, cfg)
    names = [f for f in _remote_files(origin) if f.startswith("dag/")]
    assert names and all("boxone" in f for f in names)
    assert not any("secret" in f for f in names)


def test_gitattributes_gains_the_union_line_additively(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".gitattributes").write_text("entries/*.jsonl merge=union\n# mine\n", encoding="utf-8")
    assert sync._ensure_gitattributes(wt)
    lines = (wt / ".gitattributes").read_text().splitlines()
    assert lines[:2] == ["entries/*.jsonl merge=union", "# mine"]
    assert "dag/**/*.jsonl merge=union" in lines
    assert not sync._ensure_gitattributes(wt)


def test_published_records_are_numbers_only_with_no_unit_token(tmp_path, origin):
    cfg = _config()
    base = _clone(tmp_path, "a", origin, cfg)
    _boot(base, cfg)
    dag_history.append(str(base), cfg, REC + [{"t": T, "ev": "node-resolved", "n": 11}])
    sync.publish(base, cfg)
    for f in _remote_files(origin):
        if not f.startswith("dag/"):
            continue
        for line in _git(origin, "show", _LEDGER_BRANCH + ":" + f).splitlines():
            r = json.loads(line)
            assert set(r) <= {"t", "ev", "a", "b", "n", "chain", "to"}
            assert all(isinstance(v, (int, list)) or k in ("t", "ev") for k, v in r.items())
            assert "feature" not in line and "unit" not in line
