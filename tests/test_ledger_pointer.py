"""Slice 9 of the decision rubric: the ledger pointer (batched, unaddressed, compactable)."""
import importlib.util
import json
import os
import pathlib
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"
DAY = 86400.0
T0 = 1790000000.0        # a fixed UTC instant


def _mod(name):
    p = SCRIPTS / (name + ".py")
    spec = importlib.util.spec_from_file_location("t_" + name, p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _cfg(pointer=None, ledger=True, records=True, extra=None):
    ptr = {"enabled": True}
    ptr.update(pointer or {})
    cfg = {"decision_rubric": {"records": {"enabled": records, "pointer": ptr}},
           "ledger": {"enabled": ledger, "actor": "writer-a"}}
    cfg.update(extra or {})
    return cfg


def _ids(*names, how="autonomous", qkind="needs_decision"):
    return [{"id": n, "qkind": qkind, "how": how} for n in names]


def _tree(root):
    out = {}
    for dp, _dn, fn in os.walk(root):
        for f in fn:
            p = os.path.join(dp, f)
            out[p] = open(p, "rb").read()
    return out


def _entries(sdlc):
    led = _mod("ledger")
    return led.read_all(sdlc)


def test_pointer_has_only_id_kind_how(tmp_path):
    lp, cfg = _mod("ledger_pointer"), _cfg()
    res = lp.write_pointer(tmp_path, _ids("20261011T120000-aaaa1111"), cfg, now=T0)
    assert res == {"written": 1, "queued": 0}
    (e,) = _entries(tmp_path)
    assert set(e) <= {"id", "ts", "actor", "kind", "goal", "ref", "why"}
    assert e["kind"] == "note" and e["ref"] == lp.REF
    assert e["why"] == "20261011T120000-aaaa1111|needs_decision|autonomous"
    assert lp.parse(e) == [("20261011T120000-aaaa1111", "needs_decision", "autonomous")]


def test_free_text_item_fields_are_dropped(tmp_path):
    lp, cfg = _mod("ledger_pointer"), _cfg()
    item = {"id": "r1", "qkind": "needs_decision", "how": "human", "reason": "secret prose", "choice": "x"}
    lp.write_pointer(tmp_path, [item], cfg, now=T0)
    raw = "".join(p.read_text() for p in (tmp_path / "ledger" / "entries").glob("*.jsonl"))
    assert "secret prose" not in raw and '"choice"' not in raw and '"reason"' not in raw


def test_malformed_items_are_dropped_not_raised(tmp_path):
    lp, cfg = _mod("ledger_pointer"), _cfg()
    bad = [{"id": "../x", "qkind": "needs_decision", "how": "human"},
           {"id": "r1", "qkind": "not-a-kind", "how": "human"},
           {"id": "r2", "qkind": "unknown", "how": "robot"}, 7, None]
    assert lp.write_pointer(tmp_path, bad, cfg, now=T0) == {"written": 0, "queued": 0}
    assert _entries(tmp_path) == []


def test_batches_to_the_daily_limit(tmp_path):
    lp, cfg = _mod("ledger_pointer"), _cfg({"max_writes_per_day": 1})
    assert lp.write_pointer(tmp_path, _ids("r1"), cfg, now=T0)["written"] == 1
    second = lp.write_pointer(tmp_path, _ids("r2", "r3"), cfg, now=T0 + 60)
    assert second == {"written": 0, "queued": 2}
    assert len(_entries(tmp_path)) == 1
    nxt = lp.write_pointer(tmp_path, [], cfg, now=T0 + DAY)      # next UTC day flushes the queue
    assert nxt == {"written": 1, "queued": 0}
    got = [t[0] for e in _entries(tmp_path) for t in lp.parse(e)]
    assert got == ["r1", "r2", "r3"]


def test_a_larger_daily_limit_writes_each_time(tmp_path):
    lp, cfg = _mod("ledger_pointer"), _cfg({"max_writes_per_day": 2})
    assert lp.write_pointer(tmp_path, _ids("r1"), cfg, now=T0)["written"] == 1
    assert lp.write_pointer(tmp_path, _ids("r2"), cfg, now=T0 + 1)["written"] == 1
    assert lp.write_pointer(tmp_path, _ids("r3"), cfg, now=T0 + 2)["written"] == 0


def test_a_long_batch_is_split_under_the_text_cap(tmp_path):
    lp, cfg = _mod("ledger_pointer"), _cfg()
    names = ["20261011T1200%02d-%08x" % (i % 60, i) for i in range(30)]
    res = lp.write_pointer(tmp_path, _ids(*names), cfg, now=T0)
    assert res["queued"] == 0 and res["written"] > 1
    got = [t[0] for e in _entries(tmp_path) for t in lp.parse(e)]
    assert got == names


def test_pointer_is_unaddressed(tmp_path):
    lp, cfg = _mod("ledger_pointer"), _cfg()
    lp.write_pointer(tmp_path, _ids("r1"), cfg, now=T0)
    (e,) = _entries(tmp_path)
    assert "to" not in e
    led = _mod("ledger")
    assert led.addressed_to([e], "writer-a") == [] and led.addressed_to([e], "anyone") == []


def test_ledger_off_writes_zero_bytes(tmp_path):
    lp = _mod("ledger_pointer")
    for cfg in (_cfg(ledger=False), _cfg(records=False), _cfg({"enabled": False}),
                _cfg({"enabled": "true"}), {}, {"decision_rubric": 3}):
        before = _tree(tmp_path)
        assert lp.write_pointer(tmp_path, _ids("r1"), cfg, now=T0) is None
        assert lp.compact_pointers(tmp_path, now=T0, config=cfg) == 0
        assert _tree(tmp_path) == before == {}


def test_settings_read_defaults_for_wrong_types():
    lp = _mod("ledger_pointer")
    assert lp.settings({}) == {"enabled": False, "max_writes_per_day": 1, "compact_after_days": 14}
    s = lp.settings(_cfg({"max_writes_per_day": True, "compact_after_days": 0}))
    assert s["max_writes_per_day"] == 1 and s["compact_after_days"] == 14


def _seed(led, sdlc, actor, host, pid, lines):
    d = led.entries_dir(sdlc, led.ENTRIES)
    d.mkdir(parents=True, exist_ok=True)
    p = d / ("%s-%s.%s.jsonl" % (actor, host, pid))
    p.write_text("".join(json.dumps(l, sort_keys=True) + "\n" for l in lines))
    old = T0 - 100 * DAY
    os.utime(p, (old, old))
    return p


def _ptr(lp, actor, host, pid, seq, rid):
    return {"id": "%s:%s.%s:%d" % (actor, host, pid, seq), "ts": "2026-06-01T00:00:00Z", "actor": actor,
            "kind": "note", "goal": lp.GOAL, "ref": lp.REF, "why": "%s|unknown|human" % rid}


def test_compaction_folds_own_files_only(tmp_path):
    lp, led = _mod("ledger_pointer"), _mod("ledger")
    cfg, host = _cfg(), led._host_token()
    own1 = _seed(led, tmp_path, "writer-a", host, 900001, [_ptr(lp, "writer-a", host, 900001, 1, "r1")])
    own2 = _seed(led, tmp_path, "writer-a", host, 900002, [_ptr(lp, "writer-a", host, 900002, 1, "r2")])
    mixed = _seed(led, tmp_path, "writer-a", host, 900003,
                  [_ptr(lp, "writer-a", host, 900003, 1, "r3"),
                   {"id": "writer-a:%s.900003:2" % host, "ts": "2026-06-01T00:00:00Z", "actor": "writer-a",
                    "kind": "claimed", "goal": "5"}])
    foreign = _seed(led, tmp_path, "writer-b", host, 900004, [_ptr(lp, "writer-b", host, 900004, 1, "r4")])
    other_host = _seed(led, tmp_path, "writer-a", "ffffffff", 900005,
                       [_ptr(lp, "writer-a", "ffffffff", 900005, 1, "r5")])
    live = _seed(led, tmp_path, "writer-a", host, os.getpid(), [_ptr(lp, "writer-a", host, os.getpid(), 1, "r6")])
    fresh = _seed(led, tmp_path, "writer-a", host, 900007, [_ptr(lp, "writer-a", host, 900007, 1, "r7")])
    os.utime(fresh, (T0, T0))
    keep = {p: p.read_bytes() for p in (foreign, other_host, live, fresh)}
    own_before = len(led.files_for(led.entries_dir(tmp_path, led.ENTRIES), "writer-a"))

    assert lp.compact_pointers(tmp_path, now=T0, config=cfg) == 2
    assert not own1.exists() and not own2.exists()
    assert {p: p.read_bytes() for p in keep} == keep                       # foreign etc. untouched
    assert [json.loads(l)["kind"] for l in mixed.read_text().splitlines()] == ["claimed"]
    rollup = [p for p in led.files_for(led.entries_dir(tmp_path, led.ENTRIES), "writer-a")
              if p.stem.endswith(".0")]
    assert len(rollup) == 1
    rolled = [t[0] for l in rollup[0].read_text().splitlines() for t in lp.parse(json.loads(l))]
    assert sorted(rolled) == ["r1", "r2", "r3"]
    assert len(led.files_for(led.entries_dir(tmp_path, led.ENTRIES), "writer-a")) == own_before - 2 + 1
    assert lp.compact_pointers(tmp_path, now=T0, config=cfg) == 0           # idempotent
    assert sorted(t[0] for l in rollup[0].read_text().splitlines() for t in lp.parse(json.loads(l))) == rolled


def test_pointer_file_count(tmp_path):
    lp, led = _mod("ledger_pointer"), _mod("ledger")
    assert lp.pointer_file_count(tmp_path) == 0
    _seed(led, tmp_path, "writer-a", "aaaaaaaa", 1, [])
    _seed(led, tmp_path, "writer-b", "bbbbbbbb", 2, [])
    assert lp.pointer_file_count(tmp_path) == 2


def test_lost_pointer_loses_no_record(tmp_path, monkeypatch):
    lp, ds = _mod("ledger_pointer"), _mod("decision_store")
    cfg = _cfg()
    rid = ds.append(tmp_path, {"how": "human", "qkind": "needs_decision", "choice": "a", "reason": "b"}, cfg)
    rid = rid if isinstance(rid, str) else rid.get("id")
    led = lp._load("ledger")
    monkeypatch.setattr(led, "append", lambda *a, **k: (_ for _ in ()).throw(OSError("ledger down")))
    res = lp.write_pointer(tmp_path, _ids(rid), cfg, now=T0)
    assert res == {"written": 0, "queued": 1}                                # kept for the next try
    assert ds.get(tmp_path, rid, cfg)["choice"] == "a"                       # the record is intact


def test_template_documents_the_pointer_keys():
    tmpl = (ROOT / "skills" / "sigma-init" / "templates" / "config.json.tmpl").read_text(encoding="utf-8")
    block = json.loads(tmpl)["decision_rubric"]["records"]["pointer"]
    assert block == {"enabled": False, "max_writes_per_day": 1, "compact_after_days": 14}
    note = json.loads(tmpl)["_decision_rubric"]
    for key in ("pointer", "max_writes_per_day", "compact_after_days"):
        assert "`%s`" % key in note


def test_doctor_reports_pointer_file_count(tmp_path):
    import importlib.util as iu
    p = ROOT / "skills" / "sigma-doctor" / "scripts" / "doctor.py"
    spec = iu.spec_from_file_location("t_doctor", p)
    doc = iu.module_from_spec(spec)
    spec.loader.exec_module(doc)
    lp = _mod("ledger_pointer")
    cfg = _cfg()
    lp.write_pointer(tmp_path, _ids("r1", "r2"), cfg, now=T0)
    lp.write_pointer(tmp_path, _ids("r3"), cfg, now=T0)
    assert "ledger pointer on: 1 ledger entry files, 1 ids queued" in doc._decision_rubric_state(cfg, tmp_path)
    assert "ledger pointer" not in doc._decision_rubric_state(_cfg({"enabled": False}), tmp_path)
