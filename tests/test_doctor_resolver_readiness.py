"""#952: the doctor's resolver and reviewer readiness rows and the liveness-by-age row.

The rows exist only when the upkeep gate is open AND `conflicts.resolve` is `agent`. Everywhere else the doctor output is
byte-identical to before. UNVERIFIED: the resolver CLI's `--version` flag was never run against a real binary here."""
import copy
import importlib.util
import json
import os
import re
import time

import pytest

import unattended_support as support
import upkeep_support as S

DOCTOR = support.ROOT / "skills" / "sigma-doctor" / "scripts" / "doctor.py"
REFERENCE = support.ROOT / "skills" / "sigma-doctor" / "references" / "resolver-readiness.md"
ROW_WORDS = ("resolver cli", "resolver flags", "resolver model", "resolver spend", "reviewer store", "resolution age")


def doctor():
    spec = importlib.util.spec_from_file_location("doctor_resolver_readiness", DOCTOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert hasattr(module, "_resolver_rows"), "doctor.py has no _resolver_rows yet"
    return module


def config(resolve="agent", enabled=True):
    cfg = copy.deepcopy(S.template_cfg())
    cfg["upkeep"]["enabled"] = enabled
    cfg["upkeep"]["conflicts"]["resolve"] = resolve
    return cfg


def project(tmp_path, cfg):
    sdlc = tmp_path / "proj" / ".sdlc"
    sdlc.mkdir(parents=True, exist_ok=True)
    (sdlc / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    return sdlc


def rows(d, sdlc, cfg, which=lambda name: "/bin/" + name, cheap_only=True, now=None):
    return d._resolver_rows(sdlc, cfg, which, cheap_only, now)


def names(found):
    return [c["name"].lower() for c in found]


def test_open_and_agent_gives_every_readiness_row(tmp_path):
    d, cfg = doctor(), config()
    found = rows(d, project(tmp_path, cfg), cfg)
    text = " | ".join(names(found))
    for word in ROW_WORDS[:5]:
        assert word in text, (word, text)


def test_the_shipped_defaults_are_not_ready(tmp_path):
    """Placeholder model, unverified flags and no cap key in the gate schema: three failing rows, each with a fix."""
    d, cfg = doctor(), config()
    by = {c["name"].lower(): c for c in rows(d, project(tmp_path, cfg), cfg)}
    for word in ("resolver model", "resolver flags", "resolver spend"):
        row = next(c for k, c in by.items() if word in k)
        assert row["ok"] is False and row["fix"], word


def test_a_missing_cli_is_a_failing_row(tmp_path):
    d, cfg = doctor(), config()
    found = rows(d, project(tmp_path, cfg), cfg, which=lambda name: None)
    row = next(c for c in found if "resolver cli" in c["name"].lower())
    assert row["ok"] is False


@pytest.mark.parametrize("resolve,enabled", [("off", True), ("mechanical", True), ("agent", False)])
def test_closed_gate_or_other_mode_adds_nothing(tmp_path, resolve, enabled):
    d, cfg = doctor(), config(resolve, enabled)
    assert rows(d, project(tmp_path, cfg), cfg) == []


def test_an_invalid_block_adds_nothing(tmp_path):
    d, cfg = doctor(), config()
    cfg["upkeep"]["bogus_key_952"] = 1
    assert rows(d, project(tmp_path, cfg), cfg) == []


def test_check_output_is_byte_identical_when_the_gate_is_closed(tmp_path):
    """The whole `check()` list with the block closed equals the list for a config with no upkeep block at all, and no
    readiness row name appears. This is the doctor's documented gesture: `check(sdlc_dir, cheap_only=True)`."""
    d = doctor()
    closed = config("agent", enabled=False)
    absent = copy.deepcopy(closed)
    del absent["upkeep"]
    a = d.check(str(project(tmp_path / "a", closed)), run=lambda args: "", cheap_only=True, which=lambda n: None)
    b = d.check(str(project(tmp_path / "b", absent)), run=lambda args: "", cheap_only=True, which=lambda n: None)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert not any(w in n for n in names(a) for w in ROW_WORDS)


def test_cheap_only_never_runs_a_probe_and_a_full_run_bounds_it(tmp_path, monkeypatch):
    d, cfg = doctor(), config()
    sdlc = project(tmp_path, cfg)
    calls = []
    monkeypatch.setattr(d, "_bounded_run", lambda args, timeout=None: calls.append((list(args), timeout)) or "1.0")
    rows(d, sdlc, cfg, cheap_only=True)
    assert calls == []
    rows(d, sdlc, cfg, cheap_only=False)
    assert len(calls) == 1 and calls[0][1] is not None and calls[0][1] <= 10
    assert all("plugin" not in a for a in calls[0][0]), "never the plugin list"


def test_a_hung_probe_is_a_failing_row_not_an_exception(tmp_path, monkeypatch):
    d, cfg = doctor(), config()
    sdlc = project(tmp_path, cfg)
    monkeypatch.setattr(d, "_bounded_run", lambda args, timeout=None: d._RawFailure("claude: timed out after 5s"))
    found = rows(d, sdlc, cfg, cheap_only=False)
    row = next(c for c in found if "resolver cli" in c["name"].lower())
    assert row["ok"] is False


def _record(sdlc, name, age_seconds, now):
    store = sdlc / "state" / "upkeep" / "resolutions"
    store.mkdir(parents=True, exist_ok=True)
    path = store / name
    path.write_text("{}", encoding="utf-8")
    os.utime(path, (now - age_seconds, now - age_seconds))


def test_liveness_by_age(tmp_path):
    d, cfg = doctor(), config()
    sdlc = project(tmp_path, cfg)
    now = int(time.time())
    keep = cfg["upkeep"]["backup"]["keep_days"] * 86400
    assert not [c for c in rows(d, sdlc, cfg, now=now) if "resolution age" in c["name"].lower()]
    _record(sdlc, "u-aaaaaaaaaaaa.json", 3600, now)
    fresh = next(c for c in rows(d, sdlc, cfg, now=now) if "resolution age" in c["name"].lower())
    assert fresh["ok"] is True
    _record(sdlc, "u-bbbbbbbbbbbb.json", keep + 7200, now)
    old = next(c for c in rows(d, sdlc, cfg, now=now) if "resolution age" in c["name"].lower())
    assert old["ok"] is False and old["fix"]


def test_reviewer_store_that_is_not_a_directory_fails(tmp_path):
    d, cfg = doctor(), config()
    sdlc = project(tmp_path, cfg)
    (sdlc / "state" / "upkeep").mkdir(parents=True)
    (sdlc / "state" / "upkeep" / "reviews").write_text("x", encoding="utf-8")
    row = next(c for c in rows(d, sdlc, cfg) if "reviewer store" in c["name"].lower())
    assert row["ok"] is False


def test_reference_file_exists_and_its_paths_resolve():
    text = REFERENCE.read_text(encoding="utf-8")
    assert "UNVERIFIED" in text
    for path in re.findall(r"`([A-Za-z0-9_./-]+/[A-Za-z0-9_./-]+)`", text):
        assert (support.ROOT / path).exists(), path
    assert "](" not in text, "no bracket-paren sequences"
