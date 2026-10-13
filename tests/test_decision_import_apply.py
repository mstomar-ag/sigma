"""Goal 1226: plan and apply for past-decision import. Synthetic candidates, no network."""
import importlib.util
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"


def _mod(name, path=None):
    spec = importlib.util.spec_from_file_location(name + "_t1226", path or SCRIPTS / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


DI, DS, AU, RD, RB = (_mod(n) for n in ("decision_import", "decision_store", "autonomy", "rule_draft", "rulebook"))
INIT = _mod("init_flow", ROOT / "skills/sigma-init/scripts/init_flow.py")


def _cfg(records=True, autonomy=True, visibility="private", rulebook=False):
    return {"rulebook": {
        "records": {"enabled": records, "repo_visibility": visibility},
        "autonomy": {"enabled": autonomy, "levels": {"l1_min_answers": 2, "l2_min_answers": 4,
                                                      "l2_agreement": 0.75, "l3_min_answers": 5,
                                                      "l3_agreement": 0.75}},
        "rules": {"enabled": rulebook}}}


def _c(i, q="should we merge the docs change", choice="merge", when=None, issue=7, superseded=False, opts=()):
    return {"source": "transcript", "when": when or "2026-10-01T10:%02d:00Z" % i, "question": q,
            "options": list(opts), "choice": choice, "reason": "because", "issue": issue,
            "id": "c%03d" % i, "superseded": superseded}


def _sd(tmp_path):
    p = tmp_path / ".sdlc"
    p.mkdir(exist_ok=True)
    return str(p)


def _files(sd):
    return sorted(str(p.relative_to(sd)) for p in pathlib.Path(sd).rglob("*") if p.is_file())


def test_plan_and_scan_write_nothing(tmp_path):
    sd, cfg = _sd(tmp_path), _cfg()
    out = DI.plan(sd, cfg, [_c(i) for i in range(5)])
    assert out["write"] == 5 and _files(sd) == []


def test_apply_writes_marked_records_and_second_apply_writes_nothing(tmp_path):
    sd, cfg = _sd(tmp_path), _cfg()
    cands = [_c(i) for i in range(5)]
    first = DI.apply(sd, cfg, cands)
    assert first["records"] == 5 and first["watch"] == 4        # first of the pair has nothing earlier
    recs = DS.list_by(sd, cfg)
    assert len(recs) == 5 and all(r["how"] == "imported" and r["source"] and r["original_time"] for r in recs)
    assert all(not ({"login", "user", "author", "person"} & set(r)) for r in recs)
    watches = AU._facts(sd, "watch-")
    assert len(watches) == 4 and all(w["how"] == "imported" and w["would"] == "merge" for w in watches)
    before = _files(sd)
    second = DI.apply(sd, cfg, cands)
    assert second["records"] == 0 and second["watch"] == 0 and _files(sd) == before


def test_closed_records_gate_writes_nothing_and_says_so(tmp_path, capsys):
    sd = _sd(tmp_path)
    assert DI.apply(sd, _cfg(records=False), [_c(i) for i in range(4)]) == {"gate": False, "records": 0, "watch": 0, "note": ""}
    assert _files(sd) == []
    assert DI.plan(sd, _cfg(records=False), [_c(1)])["gate"] is False


def test_cli_closed_gate_message_and_help(tmp_path):
    sd = _sd(tmp_path)
    (pathlib.Path(sd) / "config.json").write_text("{}")
    run = lambda *a: subprocess.run([sys.executable, str(SCRIPTS / "decision_import.py"), *a],
                                    capture_output=True, text=True, timeout=60)
    r = run("apply", sd)
    assert r.returncode == 1 and "closed" in r.stdout and "nothing written" in r.stdout
    assert "closed" in run("plan", sd).stdout
    assert "plan" in run("--help").stdout and "apply" in run("--help").stdout


def test_superseded_and_empty_choice_not_written(tmp_path):
    sd, cfg = _sd(tmp_path), _cfg()
    out = DI.apply(sd, cfg, [_c(1, superseded=True), _c(2, choice=""), _c(3)])
    assert out["records"] == 1


def test_unknown_kind_and_repo_area(tmp_path):
    sd, cfg = _sd(tmp_path), _cfg()
    DI.apply(sd, cfg, [_c(1, q="what colour is the bikeshed")])
    r = DS.list_by(sd, cfg)[0]
    assert (r["qkind"], r["area"]) == ("unknown", "repo")
    pl = DI.plan(sd, cfg, [_c(2, q="another odd thing", issue=8)])
    assert pl["unknown"] == 1


def test_kind_table_and_declared_kind():
    qk = ("merge_conflict", "unknown", "needs_decision")
    assert DI.kind_of(_c(1, q="Resolve this merge conflict how?"), qk) == "merge_conflict"
    assert DI.kind_of(_c(1, q="needs_decision"), qk) == "needs_decision"
    assert DI.kind_of(_c(1, q="zzz"), qk) == "unknown"


def test_area_from_categories_and_match_words(tmp_path):
    sd, cfg = _sd(tmp_path), _cfg()
    (pathlib.Path(sd) / "categories.json").write_text(json.dumps({"schema": "sigma/categories@1", "categories": [
        {"id": "c1", "areas": ["billing"]},
        {"id": "c2", "areas": ["docs"], "match_words": ["readme"]}]}))
    DI.apply(sd, cfg, [_c(1, q="should we change the billing flow"), _c(2, q="should we edit the README"),
                       _c(3, q="should we touch the docs")])
    by = {r["source"]: r["area"] for r in DS.list_by(sd, cfg)}
    assert sorted(by.values()) == ["billing", "docs", "repo"]


def test_replay_majority_of_earlier_only_and_ties_skipped():
    rows = [{"qkind": "k", "area": "a", "when": "2026-10-01T10:0%d:00Z" % i, "fp": "f%d" % i, "choice": c}
            for i, c in enumerate(["x", "x", "y", "z"])]
    out = DI.replay(rows)
    assert "f0" not in out and out["f1"] == "x" and out["f2"] == "x"
    assert "f3" not in out or out["f3"] == "x"
    assert DI.replay([dict(rows[0], choice="a"), dict(rows[1], choice="b"), dict(rows[2], choice="c")]).get("f2") is None


def test_imported_counts_for_level_1_and_2_but_never_above(tmp_path):
    assert AU.IMPORTED_LEVEL_CAP == 2
    sd, cfg = _sd(tmp_path), _cfg()
    DI.apply(sd, cfg, [_c(i) for i in range(20)])
    assert AU.level(sd, "needs_decision", "repo", cfg) == 2     # thresholds would allow 3 on live answers
    pl = DI.plan(str(tmp_path / "other"), cfg, [_c(i) for i in range(20)])
    assert [p["level"] for p in pl["pairs"]] == [2]
    # the same evidence as live answers does reach level 3
    sd2 = str(tmp_path / "live" / ".sdlc")
    for i in range(20):
        DS.append(sd2, {"how": "human", "qkind": "needs_decision", "area": "repo", "choice": "merge"}, cfg)
        AU.record_watch(sd2, "needs_decision", "repo", "merge", "merge", cfg, machine="m")
    assert AU.level(sd2, "needs_decision", "repo", cfg) == 3


def test_imported_comparisons_do_not_lift_live_answers_above_level_2(tmp_path):
    """Live answers alone would allow level 3, but every comparison on file is a replayed one: stays at 2."""
    sd, cfg = _sd(tmp_path), _cfg()
    DI.apply(sd, cfg, [_c(i) for i in range(20)])
    for _ in range(6):
        DS.append(sd, {"how": "human", "qkind": "needs_decision", "area": "repo", "choice": "merge"}, cfg)
    assert AU.level(sd, "needs_decision", "repo", cfg) == 2


def test_readout_shows_imported_separately_and_absent_when_none(tmp_path):
    sd, cfg = _sd(tmp_path), _cfg()
    DI.apply(sd, cfg, [_c(i) for i in range(3)])
    row = AU.readout(sd, cfg)["kinds"][0]
    assert row["imported"] == 3 and "(3 imported)" in AU.describe(row)
    sd2 = str(tmp_path / "b" / ".sdlc")
    DS.append(sd2, {"how": "human", "qkind": "quota", "area": "repo", "choice": "wait"}, cfg)
    row2 = AU.readout(sd2, cfg)["kinds"][0]
    assert "imported" not in row2 and "imported" not in AU.describe(row2)
    assert "imported" not in AU.learning_line(sd2, "quota", dict(cfg))


def test_cold_start_line_mentions_imported(tmp_path):
    sd, cfg = _sd(tmp_path), _cfg()
    cfg["rulebook"]["autonomy"]["levels"]["l1_min_answers"] = 50
    DI.apply(sd, cfg, [_c(i) for i in range(3)])
    assert "3 imported" in AU.learning_line(sd, "needs_decision", cfg)


def test_public_visibility_drops_free_text_and_comparisons(tmp_path):
    sd, cfg = _sd(tmp_path), _cfg(visibility="public")
    out = DI.apply(sd, cfg, [_c(i) for i in range(3)])
    assert out["records"] == 3 and out["watch"] == 0 and "public" in out["note"]
    assert all("choice" not in r and "reason" not in r for r in DS.list_by(sd, cfg))
    assert AU._facts(sd, "watch-") == []


def test_autonomy_closed_writes_records_only(tmp_path):
    sd, cfg = _sd(tmp_path), _cfg(autonomy=False)
    out = DI.apply(sd, cfg, [_c(i) for i in range(3)])
    assert out["records"] == 3 and out["watch"] == 0 and not (pathlib.Path(sd) / "autonomy").exists()


def test_rule_draft_counts_imported_evidence_and_stays_proposed(tmp_path):
    sd, cfg = _sd(tmp_path), _cfg(rulebook=True)
    cfg["rulebook"]["autonomy"]["owners"] = ["boss"]
    DI.apply(sd, cfg, [_c(i) for i in range(40)])
    rows = RD.list_patterns(sd, cfg)
    assert [(r["qkind"], r["area"], r["option"]) for r in rows] == [("needs_decision", "repo", "merge")]
    out = RD.draft(sd, "needs_decision", "repo", cfg)
    assert out["status"] == "drafted"
    assert [r["status"] for r in RB.load(sd, cfg)] == ["proposed"]
    sd2 = str(tmp_path / "few" / ".sdlc")
    DI.apply(sd2, cfg, [_c(i) for i in range(4)])
    assert RD.draft(sd2, "needs_decision", "repo", cfg)["status"] == "refused"
    assert RB.load(sd2, cfg) == []


def test_closed_gate_paths_unchanged_without_imports(tmp_path):
    sd = _sd(tmp_path)
    cfg = _cfg()
    DS.append(sd, {"how": "human", "qkind": "quota", "area": "repo", "choice": "wait"}, cfg)
    row = AU.readout(sd, cfg)["kinds"][0]
    assert row == {"qkind": "quota", "area": "repo", "level": 0, "answers": 1, "comparisons": 0,
                   "agreement": None, "vetoes": 0, "paused": False, "capped": False,
                   **{k: row[k] for k in row if k.startswith(("next_", "answers_n", "comparisons_n", "agreement_n"))}}
    closed = _cfg(records=False)
    assert DS.list_by(sd, closed) == [] and RD.list_patterns(sd, closed) == []
    assert AU.readout(sd, _cfg(autonomy=False)) == {"enabled": False}
    assert AU.level(sd, "quota", "repo", closed) == 0 and AU.learning_line(sd, "quota", closed) is not None
    assert "imported" not in AU.learning_line(sd, "quota", closed)


def test_init_lists_optional_import_only_when_records_open():
    assert INIT._records_open({"rulebook": {"records": {"enabled": True}}}) is True
    for bad in ({}, {"rulebook": []}, {"rulebook": {"records": {"enabled": "yes"}}}):
        assert INIT._records_open(bad) is False
    src = (ROOT / "skills/sigma-init/scripts/init_flow.py").read_text()
    assert "never run by init" in src and "reads only local files" in src


def test_match_words_survive_categories_load(tmp_path):
    sd = _sd(tmp_path)
    (pathlib.Path(sd) / "categories.json").write_text(json.dumps({"schema": "sigma/categories@1", "categories": [
        {"id": "c", "areas": ["a"], "match_words": ["w"]}, {"id": "d", "areas": ["b"], "match_words": [1]}]}))
    got = _mod("categories").load(sd)
    assert [(c["id"], c["match_words"]) for c in got] == [("c", ["w"])]
