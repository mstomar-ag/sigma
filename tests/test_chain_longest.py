"""#1591: `chain.py plan <sdlc_dir> --longest [--by freeing] [--json]`, the top three end goals to work on
first, from the stored map only.

Fixture issue numbers are plain numbers; none refers to a real issue. Every guard below was run red once
against its own deliberate break. The controls: drop the priority-rank clause from the key; drop the cycle
exclusion; make the verb write a file under the sdlc directory."""
import importlib.util
import json
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import resource_mode  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
S = ROOT / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ch = _mod("chain")
ds = _mod("dag_store")
dc = ch.dag_chain

NOW = 1_800_000_000.0


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _n(number, prio=None, state="open", updated=None):
    return {"number": number, "kind": "issue", "state": state, "life": "goal", "prio": prio,
            "in_corpus": True, "updated_at": _iso(updated) if updated else None}


def _line(top, depth, prio=None, updated=None):
    """A straight chain top <- top-1 <- ... of depth+1 nodes; returns (nodes, edges)."""
    nums = list(range(top - depth, top + 1))
    return ([_n(x, prio, updated=updated) for x in nums], [(a, b) for a, b in zip(nums, nums[1:])])


def _store(tmp_path, parts, mode=None):
    nodes, edges = [], []
    for ns, es in parts:
        nodes += ns
        edges += es
    sdlc = tmp_path / ".sdlc"
    (sdlc / "state").mkdir(parents=True, exist_ok=True)
    h = {"repo": "owner/repo", "built_at": _iso(NOW - 60), "refreshed_at": _iso(NOW - 60),
         "last_ok_at": _iso(NOW - 60), "last_error": None, "partial": False}
    ok, why = ds.save(sdlc, {"header": h, "nodes": nodes,
                             "edges": [{"blocker": a, "blocked": b, "source": "body"} for a, b in edges]})
    assert ok, why
    if mode:
        (sdlc / "config.json").write_text(json.dumps({"discovery": {"dag": {"mode": mode}}}))
    return sdlc


def _goals(sdlc, **kw):
    return [e["goal"] for e in dc.longest(sdlc, now=NOW, **kw)["ends"]]


def _snapshot(root):
    return sorted((str(p.relative_to(root)), p.read_bytes()) for p in root.rglob("*") if p.is_file())


def test_tier_then_length(tmp_path):
    """control: drop the rank tier from the key and the P0 chain sinks to the bottom."""
    sdlc = _store(tmp_path, [_line(10, 2, "P1"), _line(20, 4, "P1"), _line(30, 3, "P1"), _line(40, 2, "P0")])
    assert _goals(sdlc) == [40, 20, 30]
    assert dc.longest(sdlc, now=NOW)["candidates"] == 4
    sdlc = _store(tmp_path / "b", [_line(10, 2, "P1"), _line(20, 4, "P1"), _line(30, 3, "P1")])
    assert _goals(sdlc) == [20, 30, 10]


def test_tie_breaks(tmp_path):
    """Each tie clause alone: same tier and depth, members differ; then same members, age differs; then
    same age, number decides. control: each clause removed on its own turns one case red."""
    # depth 2 both; 100 has an extra side blocker (more members) though 110 is the lower... number higher
    wide_n, wide_e = _line(100, 2, "P1")
    wide_n.append(_n(95, "P1"))
    wide_e.append((95, 100))
    sdlc = _store(tmp_path / "m", [(wide_n, wide_e), _line(50, 2, "P1")])
    assert _goals(sdlc) == [100, 50]                     # members beat number
    # same members: older (smaller updated_at) first, missing last, even against a lower number
    old, mid = 1_700_000_000, 1_750_000_000
    sdlc = _store(tmp_path / "a", [_line(60, 1, "P1", mid), _line(70, 1, "P1", old), _line(50, 1, "P1")])
    assert _goals(sdlc) == [70, 60, 50]                  # age beats number; missing is last
    # the earliest stamp of any member counts, not the end's own
    ns, es = _line(80, 1, "P1", mid)
    ns[0]["updated_at"] = _iso(old - 1)
    sdlc = _store(tmp_path / "e", [(ns, es), _line(90, 1, "P1", old)])
    assert _goals(sdlc) == [80, 90]
    # all equal: lowest number
    sdlc = _store(tmp_path / "n", [_line(70, 1, "P1", old), _line(60, 1, "P1", old)])
    assert _goals(sdlc) == [60, 70]


def test_by_freeing(tmp_path):
    """control: `--by freeing` ignoring the member count gives the default order."""
    deep_n, deep_e = _line(10, 3, "P0")                  # 4 members
    wide_n = [_n(x, "P2") for x in (21, 22, 23, 24, 25, 26)] + [_n(20, "P2")]
    wide_e = [(x, 20) for x in (21, 22, 23, 24, 25, 26)]  # depth 1, 7 members
    sdlc = _store(tmp_path, [(deep_n, deep_e), (wide_n, wide_e)])
    assert _goals(sdlc) == [10, 20]
    assert _goals(sdlc, by="freeing") == [20, 10]


def test_top_three_only(tmp_path):
    sdlc = _store(tmp_path, [_line(10, 1), _line(20, 1), _line(30, 1), _line(40, 1)])
    out = dc.longest(sdlc, now=NOW)
    assert [e["goal"] for e in out["ends"]] == [10, 20, 30] and out["candidates"] == 4


def test_cycle_excluded(tmp_path, capsys):
    """control: without the exclusion the end behind the cycle is offered."""
    cyc = ([_n(1, "P0"), _n(2, "P0")], [(1, 2), (2, 1)])
    behind = ([_n(3, "P0")], [(2, 3)])
    sdlc = _store(tmp_path, [cyc, behind, _line(20, 1, "P1")])
    out = dc.longest(sdlc, now=NOW)
    assert [e["goal"] for e in out["ends"]] == [20]
    assert {x["goal"]: x["cycle"] for x in out["excluded"]} == {3: [1, 2]}
    assert ch.main(["chain.py", "plan", str(sdlc), "--longest"], now=NOW) == 0
    assert "excluded: #3" in capsys.readouterr().out


def test_read_only_no_start(tmp_path, capsys):
    """No gh call at all and no file written under the sdlc dir. control: a write makes it red."""
    sdlc = _store(tmp_path, [_line(10, 2, "P1"), _line(20, 1, "P0")])
    before = _snapshot(sdlc)

    def run(args, *a, **k):
        raise AssertionError("--longest must not call gh: %r" % (args,))
    for argv in (["--longest"], ["--longest", "--by", "freeing"], ["--longest", "--json"]):
        assert ch.main(["chain.py", "plan", str(sdlc)] + argv, run=run, now=NOW) == 0
    capsys.readouterr()
    assert _snapshot(sdlc) == before
    assert not (sdlc / "state" / "chains").exists()


def test_dag_off_refuses(tmp_path, capsys):
    sdlc = _store(tmp_path, [_line(10, 1)], mode="off")
    assert ch.main(["chain.py", "plan", str(sdlc), "--longest"], now=NOW) == 2
    assert "dag: off" in capsys.readouterr().err
    assert ch.main(["chain.py", "plan", str(tmp_path / "none"), "--longest"], now=NOW) == 2
    assert "map unusable" in capsys.readouterr().err


def test_json_and_cli_usage(tmp_path, capsys):
    sdlc = _store(tmp_path, [_line(10, 2, "P1")])
    assert ch.main(["chain.py", "plan", str(sdlc), "--longest", "--json"], now=NOW) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["by"] == "length" and out["ends"][0]["goal"] == 10 and out["ends"][0]["members"] == 3
    for bad in (["--longest", "--by"], ["--longest", "--by", "x"], ["--longest", "5"],
                ["--longest", "--longest"], ["--by", "freeing"]):
        assert ch.main(["chain.py", "plan", str(sdlc)] + bad, now=NOW) == 2, bad
        assert "usage: chain.py plan" in capsys.readouterr().err
    assert ch.main(["chain.py", "plan", "--longest"], now=NOW) == 2
    assert "usage: chain.py plan" in capsys.readouterr().err


def test_subprocess_gesture(tmp_path):
    """The documented gesture, run as a process."""
    sdlc = _store(tmp_path, [_line(10, 2, "P1")])
    env = resource_mode.carry({"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    r = subprocess.run([sys.executable, str(S / "chain.py"), "plan", str(sdlc), "--longest"],
                       capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=60)
    assert r.returncode in (0, 2), r.stderr      # the map is stale against the real clock: refused, not crashed
    assert "Traceback" not in r.stderr
