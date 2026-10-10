"""#936 (upkeep part C, slice 5): the read-back classifier and the pending-landing record.

The library has no caller, so a closed upkeep gate changes nothing; the closed-gate tests pin that. The controls named in
the plan (first-parent check, write-before-call, doctor never writes, closed-gate identity, key folding) were each broken
once, seen red, and restored."""
import importlib.util
import json
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"
sys.path.insert(0, str(ROOT / "tools"))

T = "a" * 40
B = "b" * 40
M = "c" * 40
OTHER = "d" * 40
OPEN = {"upkeep": {"enabled": True}}
CLOSED = {}


def _mod():
    spec = importlib.util.spec_from_file_location("feature_upkeep_landing_under_test", SCRIPTS / "feature_upkeep_landing.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


mod = _mod()
PRE = {"unmerged": True, "head": T, "base": B}
OK = {"kind": "ok", "status": 200}
ERR = {"kind": "error", "status": 405}
LOST = {"kind": "lost", "status": None}


def merged_pr(**kw):
    pr = {"merged": True, "state": "closed", "merge_commit_sha": M, "merged_at": "x", "auto_merge": None}
    pr.update(kw)
    return pr


def open_pr(**kw):
    pr = {"merged": False, "state": "open", "merge_commit_sha": None, "auto_merge": None}
    pr.update(kw)
    return pr


def tree(root):
    out = {}
    for p in sorted(pathlib.Path(root).rglob("*")):
        out[str(p.relative_to(root))] = p.read_bytes() if p.is_file() else None
    return out


# --------------------------------------------------------------------------- the classifier


def test_merged_needs_two_parents_second_is_head_first_is_base():
    assert mod.classify(PRE, OK, merged_pr(), [B, T]).outcome == "merged"


def test_first_parent_mismatch_is_a_warning_not_a_clean_merge():
    got = mod.classify(PRE, OK, merged_pr(), [OTHER, T])
    assert got == ("merged-with-warning", "base-moved")


def test_parent_count_not_two_with_second_parent_head_is_a_warning():
    assert mod.classify(PRE, OK, merged_pr(), [B, T, OTHER]).outcome == "merged-with-warning"


def test_merged_other_head_is_unconfirmed_never_clean():
    assert mod.classify(PRE, OK, merged_pr(), [B, OTHER]).outcome == "unconfirmed"
    assert mod.classify(PRE, OK, merged_pr(), [B]).outcome == "unconfirmed"


def test_merged_with_parents_unread_or_no_commit_is_unconfirmed():
    assert mod.classify(PRE, OK, merged_pr(), None) == ("unconfirmed", "parents-unread")
    assert mod.classify(PRE, OK, merged_pr(merge_commit_sha=None), [B, T]).outcome == "unconfirmed"


def test_lost_acknowledgment_with_merged_head_is_merged():
    assert mod.classify(PRE, LOST, merged_pr(), [B, T]).outcome == "merged"


def test_refused_needs_an_error_and_an_open_unmerged_pr():
    assert mod.classify(PRE, ERR, open_pr(), None) == ("refused", "status-405")
    assert mod.classify(PRE, ERR, open_pr(state="closed"), None).outcome == "unconfirmed"
    assert mod.classify(PRE, LOST, open_pr(), None).outcome == "unconfirmed"
    assert mod.classify(PRE, OK, open_pr(), None).outcome == "unconfirmed"


def test_armed_only_on_a_positive_read_back():
    assert mod.classify(PRE, OK, open_pr(auto_merge={"enabled_by": "x"}), None).outcome == "armed"
    for empty in (None, {}, True, "yes", []):
        assert mod.classify(PRE, OK, open_pr(auto_merge=empty), None).outcome != "armed"
    assert mod.classify(PRE, OK, None, None).outcome == "unconfirmed"
    assert mod.classify(PRE, ERR, open_pr(auto_merge={"x": 1}, state="closed"), None).outcome != "armed"


def test_no_pre_observation_or_failed_read_is_unconfirmed():
    for pre in (None, {}, {"unmerged": False, "head": T, "base": B}, {"unmerged": True, "head": "zz", "base": B}):
        assert mod.classify(pre, OK, merged_pr(), [B, T]).outcome == "unconfirmed"
    assert mod.classify(PRE, OK, None, None) == ("unconfirmed", "readback-failed")
    assert mod.classify(PRE, OK, {"merged": "true"}, None).outcome == "unconfirmed"


def test_classifier_is_total_over_garbage():
    for args in ((1, 2, 3, 4), (PRE, None, merged_pr(), "ab"), (PRE, 5, merged_pr(), [1, 2]), (PRE, OK, merged_pr(), [[]])):
        assert mod.classify(*args).outcome == "unconfirmed"


# --------------------------------------------------------------------------- key folding and the record


def test_two_casings_of_a_unit_are_one_record(tmp_path):
    assert mod.record_path(tmp_path, "Voice") == mod.record_path(tmp_path, "voice")
    assert mod.record_path(tmp_path, "voice").parent == tmp_path / "state" / "unit-landings"


def test_a_bad_unit_name_has_no_record(tmp_path):
    for bad in ("", "../x", "a/b", None, 3):
        with pytest.raises(ValueError):
            mod.record_path(tmp_path, bad)
    assert mod.begin(tmp_path, OPEN, "../x", 1, PRE, 10) == (False, "bad-unit", None)


def test_begin_writes_pending_and_merged_deletes(tmp_path):
    assert mod.begin(tmp_path, OPEN, "voice", 7, PRE, 100).ok
    doc = mod.read_record(tmp_path, "Voice")
    assert doc["outcome"] == "pending" and doc["head"] == T and doc["base"] == B and doc["pr"] == 7
    assert mod.finish(tmp_path, OPEN, "voice", mod.Verdict("merged", "verified"), 120).ok
    assert mod.read_record(tmp_path, "voice") is None
    assert not mod.record_path(tmp_path, "voice").exists()


def test_non_merged_outcomes_rewrite_the_record(tmp_path):
    mod.begin(tmp_path, OPEN, "voice", 7, PRE, 100)
    mod.finish(tmp_path, OPEN, "voice", mod.Verdict("refused", "status-405"), 130, pr=open_pr(), call=ERR)
    doc = mod.read_record(tmp_path, "voice")
    assert (doc["outcome"], doc["reason"], doc["last_read_at"], doc["call"]) == ("refused", "status-405", 130, ERR)


def test_record_is_written_before_the_call_and_a_crash_leaves_it(tmp_path):
    seen = {}

    def do_merge():
        seen["pending"] = mod.read_record(tmp_path, "voice")
        raise RuntimeError("crash mid call")

    got = mod.run_landing(tmp_path, OPEN, "voice", 7, PRE, do_merge, lambda n: None, lambda s: None, 100)
    assert seen["pending"]["outcome"] == "pending"
    assert got.called and got.outcome == "unconfirmed"
    assert mod.read_record(tmp_path, "voice")["outcome"] == "unconfirmed"


def test_unwritable_record_means_the_call_is_never_made(tmp_path):
    (tmp_path / "state").write_text("a file where the directory must go")
    called = []
    got = mod.run_landing(tmp_path, OPEN, "voice", 7, PRE, lambda: called.append(1) or OK, lambda n: None,
                          lambda s: None, 100)
    assert not called and not got.called and got.outcome == "refused"


def test_run_landing_merged_leaves_no_record(tmp_path):
    got = mod.run_landing(tmp_path, OPEN, "voice", 7, PRE, lambda: OK, lambda n: merged_pr(), lambda s: [B, T], 100)
    assert (got.outcome, got.called) == ("merged", True)
    assert mod.pending(tmp_path) == []


def test_settle_is_one_pr_read_and_one_commit_read(tmp_path):
    mod.begin(tmp_path, OPEN, "voice", 7, PRE, 100)
    reads = []
    got = mod.settle(tmp_path, OPEN, "voice", lambda n: reads.append(("pr", n)) or merged_pr(),
                     lambda s: reads.append(("commit", s)) or [B, T], 200)
    assert got.outcome == "merged" and reads == [("pr", 7), ("commit", M)]
    assert mod.read_record(tmp_path, "voice") is None


def test_settle_with_a_failing_reader_keeps_the_record(tmp_path):
    mod.begin(tmp_path, OPEN, "voice", 7, PRE, 100)

    def boom(_):
        raise OSError("down")
    got = mod.settle(tmp_path, OPEN, "voice", boom, boom, 300)
    assert got.outcome == "unconfirmed"
    assert mod.read_record(tmp_path, "voice")["last_read_at"] == 300


def test_settle_without_a_record_reads_nothing(tmp_path):
    def boom(_):
        raise AssertionError("must not read")
    assert mod.settle(tmp_path, OPEN, "voice", boom, boom, 1) is None


def test_prune_ages_out_only_stale_record_shaped_files(tmp_path):
    mod.begin(tmp_path, OPEN, "old", 1, PRE, 100)
    mod.begin(tmp_path, OPEN, "young", 2, PRE, 100)
    store = tmp_path / "state" / "unit-landings"
    (store / "notes.txt").write_text("keep")
    os.utime(store / "old.json", (1000, 1000))
    (store / "link.json").symlink_to(store / "young.json")
    os.utime(store / "link.json", (1000, 1000), follow_symlinks=False)
    assert mod.prune(tmp_path, OPEN, 14, now=1000 + 15 * 86400) == 1
    assert not (store / "old.json").exists() and (store / "notes.txt").exists() and (store / "link.json").is_symlink()
    with pytest.raises(ValueError):
        mod.prune(tmp_path, OPEN, 0)


def test_prune_removes_a_stale_real_mkstemp_temp_and_keeps_a_fresh_one(tmp_path):
    import tempfile
    mod.begin(tmp_path, OPEN, "keep", 1, PRE, 100)
    store = tmp_path / "state" / "unit-landings"
    made = []
    for _ in range(2):
        fd, name = tempfile.mkstemp(prefix=".", suffix=".tmp", dir=str(store))
        os.close(fd)
        made.append(pathlib.Path(name))
    os.utime(made[0], (1000, 1000))
    now = 1000 + 15 * 86400
    os.utime(made[1], (now, now))
    os.utime(store / "keep.json", (now, now))
    assert mod.prune(tmp_path, OPEN, 14, now=now) == 1
    assert not made[0].exists() and made[1].exists()


def test_a_refused_record_does_not_block_a_retry_but_a_pending_one_does(tmp_path):
    assert mod.begin(tmp_path, OPEN, "voice", 7, PRE, 100).ok
    assert mod.begin(tmp_path, OPEN, "voice", 8, PRE, 110) == (False, "unsettled-record", None)
    mod.finish(tmp_path, OPEN, "voice", mod.Verdict("refused", "status-405"), 120, pr=open_pr(), call=ERR)
    assert mod.begin(tmp_path, OPEN, "voice", 8, PRE, 200).ok
    doc = mod.read_record(tmp_path, "voice")
    assert (doc["pr"], doc["outcome"]) == (8, "pending")


def test_an_unsettled_record_is_not_overwritten_by_a_new_landing(tmp_path):
    mod.begin(tmp_path, OPEN, "voice", 7, PRE, 100)
    mod.finish(tmp_path, OPEN, "voice", mod.Verdict("unconfirmed", "lost"), 110, call=LOST)
    before = mod.read_record(tmp_path, "voice")
    assert mod.begin(tmp_path, OPEN, "voice", 8, PRE, 200) == (False, "unsettled-record", None)
    called = []

    def boom(_):
        raise OSError("down")
    got = mod.run_landing(tmp_path, OPEN, "voice", 8, PRE, lambda: called.append(1) or OK, boom, boom, 300)
    assert not called and not got.called and got.outcome == "refused"
    after = mod.read_record(tmp_path, "voice")
    assert after["pr"] == 7 and after["call"] == before["call"] and after["outcome"] == "unconfirmed"


def test_symlinked_store_is_refused(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "state" / "unit-landings").symlink_to(tmp_path / "elsewhere")
    assert mod.begin(tmp_path, OPEN, "voice", 1, PRE, 1).reason == "unwritable"
    assert list((tmp_path / "elsewhere").iterdir()) == []


# --------------------------------------------------------------------------- the gate


def test_closed_gate_is_identity_for_every_entry_point(tmp_path):
    def boom(*_):
        raise AssertionError("a reader or the call ran under a closed gate")
    for closed in (CLOSED, {"upkeep": {"enabled": "true"}}, {"upkeep": {"enabled": 1}}, None, {"upkeep": 3}):
        assert mod.begin(tmp_path, closed, "voice", 1, PRE, 1).reason == "gate-closed"
        assert mod.finish(tmp_path, closed, "voice", mod.Verdict("merged", "x"), 1).reason == "gate-closed"
        assert mod.settle(tmp_path, closed, "voice", boom, boom, 1) is None
        assert mod.prune(tmp_path, closed) == 0
        assert mod.doctor_row(tmp_path, closed, 10) is None
        got = mod.run_landing(tmp_path, closed, "voice", 1, PRE, boom, boom, boom, 1)
        assert not got.called
    assert tree(tmp_path) == {}


# --------------------------------------------------------------------------- the doctor row


def test_doctor_row_reports_oldest_age_and_gesture(tmp_path):
    mod.begin(tmp_path, OPEN, "newer", 2, PRE, 1000)
    mod.begin(tmp_path, OPEN, "older", 1, PRE, 400)
    row = mod.doctor_row(tmp_path, OPEN, 400 + 7200)
    assert row["ok"] is False and "older" in row["fix"] and "120 minute" in row["fix"] and "2 record" in row["fix"]
    assert "verify_merge.py land .sdlc older" in row["fix"]
    assert (ROOT / "skills" / "sigma-rebase" / "scripts" / "verify_merge.py").is_file()


def test_doctor_row_absent_with_nothing_pending(tmp_path):
    assert mod.doctor_row(tmp_path, OPEN, 10) is None


def test_doctor_row_never_writes(tmp_path, monkeypatch):
    mod.begin(tmp_path, OPEN, "voice", 1, PRE, 100)
    before = tree(tmp_path)
    state = mod._sibling("state")

    def no_write(*a, **k):
        raise AssertionError("the doctor path wrote")
    monkeypatch.setattr(state, "atomic_write_text", no_write)
    monkeypatch.setattr(os, "unlink", no_write)
    monkeypatch.setattr(os, "replace", no_write)
    assert mod.doctor_row(tmp_path, OPEN, 5000)["ok"] is False
    assert mod.pending(tmp_path)[0][0] == "voice"
    assert tree(tmp_path) == before


# --------------------------------------------------------------------------- structure


def test_new_store_does_not_overlap_a_predecessor_written_path():
    spec = importlib.util.spec_from_file_location("sp936", ROOT / "tools" / "readiness" / "shared_paths.py")
    sp = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = sp
    spec.loader.exec_module(sp)
    fixture = json.loads((ROOT / "tests" / "fixtures" / "predecessor_written_paths.json").read_text(encoding="utf-8"))
    text = json.dumps(fixture)
    assert "unit-landings" not in text
    for pattern in (".sdlc/state/unit-landings", ".sdlc/state/unit-landings/*.json"):
        assert not any(sp.overlaps(pattern, p) for p in _fixture_paths(fixture))


def _fixture_paths(node):
    if isinstance(node, str):
        if node.startswith(".sdlc"):
            yield node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield from _fixture_paths(k)
            yield from _fixture_paths(v)
    elif isinstance(node, list):
        for v in node:
            yield from _fixture_paths(v)


def test_disposition_for_the_new_store_is_in_the_audit_format():
    ga_spec = importlib.util.spec_from_file_location("ga936", ROOT / "tools" / "readiness" / "growth_audit.py")
    ga = importlib.util.module_from_spec(ga_spec)
    sys.modules[ga_spec.name] = ga
    ga_spec.loader.exec_module(ga)
    entries = json.loads((ROOT / "docs" / "launch" / "dispositions" / "936.json").read_text(encoding="utf-8"))
    assert {e["pattern"] for e in entries} == {".sdlc/state/unit-landings/", ".sdlc/state/unit-landings/<unit>.json"}
    for e in entries:
        assert set(ga._DISPOSITION_KEYS) <= set(e) and e["issue"] == "#936"
    rows = [{"pattern": e["pattern"]} for e in entries]
    assert ga.b6_disposition(rows, 936, {e["pattern"]: e for e in entries})["unresolved_patterns"] == []


def _doctor_rows(sdlc, config):
    (sdlc).mkdir(exist_ok=True)
    (sdlc / "config.json").write_text(json.dumps(config))
    spec = importlib.util.spec_from_file_location("doctor936", ROOT / "skills" / "sigma-doctor" / "scripts" / "doctor.py")
    doctor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(doctor)
    rows = doctor.check(str(sdlc), run=lambda *a, **k: (1, "", ""), cheap_only=True, which=lambda n: None)
    return [r["name"] for r in rows]


def test_doctor_emits_the_row_only_when_open_and_pending(tmp_path):
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir()
    mod.begin(sdlc, OPEN, "voice", 1, PRE, 100)
    name = "unit landing not left pending"
    assert name in _doctor_rows(sdlc, OPEN)
    assert name not in _doctor_rows(sdlc, {})                       # closed gate: no row
    assert name not in _doctor_rows(sdlc, {"upkeep": {"enabled": "true"}})
