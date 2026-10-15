"""#1514: the host part of a ledger entry file name is never the raw host name."""
import importlib.util
import json
import pathlib
import platform

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load(rel, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ledger = _load("skills/sigma-loop/scripts/ledger.py", "ledger_1514")
doctor = _load("skills/sigma-doctor/scripts/doctor.py", "doctor_1514")
HOST = "alicesmacbook"


def _pin(monkeypatch):
    monkeypatch.setattr(platform, "node", lambda: HOST)


def test_default_name_is_plain_hash_not_host(monkeypatch, tmp_path):
    _pin(monkeypatch)
    cfg = {"ledger": {"enabled": True}}
    name = ledger.entry_file(tmp_path, "bo", config=cfg).name
    import hashlib
    assert name == "bo-%s.%d.jsonl" % (hashlib.sha256(HOST.encode()).hexdigest()[:8], __import__("os").getpid())
    assert "alice" not in name and "macbook" not in name
    assert ledger._host_token() == ledger._host_token({})


def test_alias_is_used_and_sanitised(monkeypatch):
    _pin(monkeypatch)
    cfg = {"ledger": {"host_alias": "Team-A.1 !"}}
    assert ledger._host_token(cfg) == "teama1"
    assert ledger._host_token({"ledger": {"host_alias": "x" * 40}}) == "x" * 12
    assert ledger._host_token({"ledger": {"host_alias": "---"}}) == ledger._host_token()
    assert ledger._host_token({"ledger": {"host_alias": 7}}) == ledger._host_token()


def test_salt_differs_per_repo_and_is_stable(monkeypatch):
    _pin(monkeypatch)
    def cfg(repo, salt=True):
        return {"ledger": {"salt_host_token": salt}, "discovery": {"github": {"repo": repo}}}
    a, b = ledger._host_token(cfg("o/a")), ledger._host_token(cfg("o/b"))
    assert a != b and a == ledger._host_token(cfg("O/a"))
    assert ledger._host_token(cfg("o/a", salt=False)) == ledger._host_token()
    assert HOST not in a and len(a) == 8


def test_append_uses_alias_in_file_and_id(monkeypatch, tmp_path):
    _pin(monkeypatch)
    cfg = {"ledger": {"enabled": True, "host_alias": "pool7"}}
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    e = ledger.append(tmp_path, cfg, "note", None, text="hi", run=lambda a: "bo")
    files = list((tmp_path / "ledger" / "entries").glob("*.jsonl"))
    assert files and all("-pool7." in f.name for f in files)
    assert ":pool7." in e["id"] and HOST not in e["id"]
    assert ledger.my_writer(cfg).endswith(":pool7.%d" % __import__("os").getpid())


def test_old_shape_files_still_read_and_attributed(tmp_path):
    d = tmp_path / "ledger" / "entries"
    d.mkdir(parents=True)
    line = json.dumps({"id": "bo:x.1:1", "ts": "2026-01-01T00:00:00Z", "actor": "bo", "kind": "note",
                       "goal": None, "text": "old"})
    for n in ("bo-4242.jsonl", "bo-alicesmacbook.4242.jsonl", "bo-0123abcd.4242.jsonl"):
        (d / n).write_text(line + "\n")
    assert len(ledger.read_all(tmp_path)) == 3
    assert {p.name for p in ledger.files_for(d, "bo")} == {
        "bo-4242.jsonl", "bo-alicesmacbook.4242.jsonl", "bo-0123abcd.4242.jsonl"}


def _led(tmp_path, names):
    d = tmp_path / "ledger" / "entries"
    d.mkdir(parents=True)
    for n in names:
        (d / n).write_text("")
    return tmp_path


def test_raw_host_scan(tmp_path):
    base = _led(tmp_path, ["bo-4242.jsonl", "bo-0123abcd.9.jsonl", "bo-pool7.9.jsonl",
                           "bo-alicesmacbook.9.jsonl", "notes.jsonl"])
    assert doctor._raw_host_entry_files(base, {}) == ["bo-alicesmacbook.9.jsonl", "bo-pool7.9.jsonl"]
    assert doctor._raw_host_entry_files(base, {"ledger": {"host_alias": "pool7"}}) == ["bo-alicesmacbook.9.jsonl"]


def _rows(base, cfg, public):
    class R:
        def __call__(self, args):
            return "false" if args[0] == "gh" and public else ("true" if args[0] == "gh" else "")
    (base / "config.json").write_text(json.dumps(cfg))
    return [r for r in doctor.check(str(base), run=R()) if r["name"] == "ledger entries carry no raw host name"]


def test_doctor_row_public_vs_private(tmp_path):
    base = _led(tmp_path, ["bo-alicesmacbook.9.jsonl"])
    cfg = {"ledger": {"enabled": True}}
    pub = _rows(base, cfg, True)
    assert pub and pub[0]["ok"] is False and "host_alias" in pub[0]["fix"]
    priv = _rows(base, cfg, False)
    assert priv and priv[0]["ok"] is True


def test_doctor_no_row_when_clean_or_ledger_off(tmp_path):
    base = _led(tmp_path, ["bo-0123abcd.9.jsonl"])
    assert _rows(base, {"ledger": {"enabled": True}}, True) == []
    base2 = _led(tmp_path / "x", ["bo-alicesmacbook.9.jsonl"])
    assert _rows(base2, {"ledger": {"enabled": False}}, True) == []
