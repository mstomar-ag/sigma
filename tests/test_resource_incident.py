"""#1914 - the resource guard records who held the memory when the machine went critical.

Hermetic: the process table is FAKED (a list of dicts), no real process is read, signalled or started; no model,
network or gh call. State goes to a temp folder (SIGMA_RESOURCE_DIR); no test sets SIGMA_RESOURCE_MODE.
Controls (break one clause, see red): AC-3 fails when the scrub call is removed; AC-4 when the prune drops newest
first; the atomic test when the file is written straight to its final name; AC-2 when snapshot reads the
environment; the debounce test when `note_transition` ignores the prior state."""
import importlib.util
import json
import os
import pathlib
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import resource_guard as guard  # noqa: E402
import resource_lease  # noqa: E402
import resource_liveness as live  # noqa: E402

GIB = 1 << 30
def _leaky():
    return "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


LEAKY = _leaky()
PLANTED = "planted-env-value-1914-zzz"


def _row(pid, rss, cmd="python tool.py", ppid=1, cwd="/work/repo", age=60):
    return {"pid": pid, "ppid": ppid, "rss": rss, "age": age, "cwd": cwd, "cmd": cmd}


def _incidents(directory):
    folder = pathlib.Path(directory) / "incidents"
    return sorted(folder.glob("*.json")) if folder.is_dir() else []


def _only(directory):
    files = _incidents(directory)
    assert len(files) == 1, files
    return json.loads(files[0].read_text())


def _sample(available):
    return guard.Sample(total=16 * GIB, available=available, swap_used=0, swap_total=0, swapouts=0, swapouts_at=0.0,
                        pressure=1, cpu_count=8, load1=0.0)


@pytest.fixture
def state(tmp_path, monkeypatch):
    folder = tmp_path / "state"
    folder.mkdir()
    monkeypatch.setenv("SIGMA_RESOURCE_DIR", str(folder))
    return folder


# ------------------------------------------------------------------------------------------ AC-1
def test_critical_transition_writes_one_file_with_the_largest_trees(state):
    table = [_row(10, 100), _row(11, 900, cmd="node big.js", ppid=10, cwd="/w/a", age=7), _row(12, 500)]
    path = guard.note_transition(str(state), False, True, table=table, limit=2, now=1_700_000_000)
    assert path and os.path.dirname(path) == str(state / "incidents")
    record = _only(state)
    assert [p["pid"] for p in record["processes"]] == [11, 12]                  # largest first, cut at the limit
    top = record["processes"][0]
    assert (top["ppid"], top["rss"], top["age"], top["cwd"], top["cmd"]) == (10, 900, 7, "/w/a", "node big.js")


def test_a_second_critical_sample_writes_nothing(state):
    """Debounce: only the non-critical -> critical transition writes. Control: ignore `was_critical`."""
    table = [_row(10, 100)]
    assert guard.note_transition(str(state), True, True, table=table) is None
    assert guard.note_transition(str(state), False, False, table=table) is None
    assert guard.note_transition(str(state), True, False, table=table) is None
    assert _incidents(state) == []
    assert guard.note_transition(str(state), False, True, table=table, now=1_700_000_000)
    assert len(_incidents(state)) == 1


def test_refresh_writes_on_the_transition_only(state, monkeypatch, tmp_path):
    """The watcher's refresh: roomy -> critical writes once, the next critical sample writes nothing."""
    box = {"sample": _sample(12 * GIB)}
    monkeypatch.setattr(resource_lease, "take_sample", lambda: box["sample"])
    monkeypatch.setattr(guard, "snapshot", lambda limit=20, table=None: [{"pid": 1, "rss": 5}])
    sdlc = tmp_path / "sdlc"
    sdlc.mkdir()
    cfg = {"resource": {"mode": "enforce"}}
    on = {"SIGMA_RESOURCE_DIR": str(state), "SIGMA_RESOURCE_MODE": "enforce"}   # an env dict: the shell's is untouched
    live.refresh(str(sdlc), cfg, now=1_700_000_000.0, append=lambda *a, **k: None, env=on)
    assert _incidents(state) == []
    box["sample"] = _sample(1 * GIB)
    live.refresh(str(sdlc), cfg, now=1_700_000_100.0, append=lambda *a, **k: None, env=on)
    assert len(_incidents(state)) == 1
    live.refresh(str(sdlc), cfg, now=1_700_000_200.0, append=lambda *a, **k: None, env=on)
    assert len(_incidents(state)) == 1


# ------------------------------------------------------------------------------------------ AC-2
def test_no_environment_value_reaches_the_file(state, monkeypatch):
    """Control: let `snapshot` copy os.environ into the record and the planted value appears."""
    monkeypatch.setenv("SIGMA_PLANTED_1914", PLANTED)
    table = [dict(_row(10, 100), env={"SIGMA_PLANTED_1914": PLANTED}, environ=PLANTED)]
    guard.note_transition(str(state), False, True, table=table, now=1_700_000_000)
    text = _incidents(state)[0].read_text()
    assert PLANTED not in text
    assert "env" not in _only(state)["processes"][0]


# ------------------------------------------------------------------------------------------ AC-3
def test_a_secret_in_a_command_line_is_written_redacted(state):
    """Control: remove the scrub call and the token is written verbatim."""
    table = [_row(10, 100, cmd="tool --token %s --go" % LEAKY)]
    guard.note_transition(str(state), False, True, table=table, now=1_700_000_000)
    text = _incidents(state)[0].read_text()
    assert LEAKY not in text
    assert "--token" in text


def test_the_command_line_is_cut_at_200_characters(state):
    guard.note_transition(str(state), False, True, table=[_row(10, 100, cmd="x" * 900)], now=1_700_000_000)
    assert len(_only(state)["processes"][0]["cmd"]) <= 200


# ------------------------------------------------------------------------------------------ AC-4
def test_prune_removes_the_oldest_until_twenty_remain(state):
    folder = state / "incidents"
    folder.mkdir()
    old = ["20260101T0000%02d-0.json" % n for n in range(25)]
    for name in old:
        (folder / name).write_text("{}")
    guard.note_transition(str(state), False, True, table=[_row(10, 1)], now=1_900_000_000)
    left = sorted(p.name for p in folder.glob("*.json"))
    assert len(left) == 20
    assert not set(old[:6]) & set(left)                                          # the six oldest are gone
    assert set(old[7:]) <= set(left)                                             # the newest old ones stay
    assert any(name > old[-1] for name in left)                                  # and the new one is kept


# ------------------------------------------------------------------------------------------ atomic write
def test_the_file_appears_by_rename_only(state):
    """Control: write straight to the final name; `_replace` is then never called and a failure leaves a file."""
    seen = []

    def spy(src, dst):
        seen.append((os.path.exists(dst), json.loads(pathlib.Path(src).read_text())))
        raise OSError("rename refused")

    got = guard.write_incident(str(state), {"processes": [{"pid": 1}]}, now=1_700_000_000, _replace=spy)
    assert got is None                                                           # the failure is swallowed
    assert seen and seen[0][0] is False and seen[0][1]["processes"]              # complete before it is visible
    assert _incidents(state) == []                                               # no half-written final file
    assert [p for p in (state / "incidents").iterdir()] == []                    # and no tmp left behind


# ------------------------------------------------------------------------------------------ AC-6
def test_six_hundred_processes_fit_in_64_kib(state):
    table = [_row(100 + n, 10_000 + n, cmd="python -c " + "y" * 400, cwd="/very/long/" + "d" * 150) for n in range(600)]
    guard.note_transition(str(state), False, True, table=table, limit=600, now=1_700_000_000)
    path = _incidents(state)[0]
    assert path.stat().st_size <= 64 * 1024
    record = json.loads(path.read_text())
    assert record["truncated"] is True and 0 < len(record["processes"]) < 600
    assert record["processes"][0]["rss"] == 10_599                               # the largest survive the trim


def test_an_unsupported_platform_records_unavailable(state, monkeypatch):
    monkeypatch.setattr(guard, "process_table", lambda: None)
    path = guard.note_transition(str(state), False, True, now=1_700_000_000)
    assert path and _only(state)["processes"] == "unavailable"


def test_a_failing_write_never_raises(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert guard.note_transition(str(blocker), False, True, table=[_row(1, 1)]) is None


# ------------------------------------------------------------------------------------------ AC-5
def test_a_real_shed_writes_one_incident_naming_the_lease(tmp_path, monkeypatch):
    """Through the verify meter: `shed_after` critical samples create the stop file and one incident."""
    import test_resource_verify_1601 as base
    verify = base._fresh("resource_verify")
    lease = verify._sibling("resource_lease")
    folder = tmp_path / "shedstate"
    monkeypatch.setenv("SIGMA_RESOURCE_DIR", str(folder))
    crit = _sample(1 * GIB)
    monkeypatch.setattr(lease, "take_sample", lambda: crit)
    monkeypatch.setattr(lease.guard, "process_table", lambda: [_row(10, 100)])
    meter = verify._Meter(lease, "lease-xyz", 1.0, (str(folder / "shed" / "lease-xyz.stop"), {}, 1))
    meter.check_shed()
    meter.check_shed()
    assert _only(folder)["lease"] == "lease-xyz"


def test_shed_incident_names_the_lease_id(state):
    """The meter's shed path calls `record_incident` with the lease id; read it back from the file."""
    guard.record_incident(str(state), "shed", lease_id="lease-abc", table=[_row(10, 100)], now=1_700_000_000)
    record = _only(state)
    assert record["lease"] == "lease-abc" and record["why"] == "shed"


# ------------------------------------------------------------------------------------------ slots line
def _log():
    spec = importlib.util.spec_from_file_location("log_1914", ROOT / "skills" / "sigma-log" / "scripts" / "log.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_incident_line_shows_for_the_newest_file_under_an_hour(state):
    log = _log()
    folder = state / "incidents"
    folder.mkdir()
    now = time.time()
    older, newer = folder / "a-0.json", folder / "b-0.json"
    for path, age in ((older, 1800), (newer, 600)):
        path.write_text("{}")
        os.utime(path, (now - age, now - age))
    on = {"SIGMA_RESOURCE_DIR": str(state), "SIGMA_RESOURCE_MODE": "enforce"}   # an env dict, not the shell's
    tail = log.incident_tail({}, now=now, env=on)
    assert tail.startswith("incident 10m ago: ") and str(newer) in tail
    os.utime(newer, (now - 3700, now - 3700))
    os.utime(older, (now - 3800, now - 3800))
    assert log.incident_tail({}, now=now, env=on) == ""                          # nothing under an hour: nothing
    os.utime(newer, (now - 60, now - 60))
    assert log.incident_tail({"resource": {"mode": "off"}}, now=now, env={"SIGMA_RESOURCE_DIR": str(state)}) == ""


def test_the_incident_line_is_silent_without_a_folder(state):
    assert _log().incident_tail({}, now=time.time(), env={"SIGMA_RESOURCE_DIR": str(state)}) == ""
