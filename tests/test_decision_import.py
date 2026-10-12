"""Decision import groundwork (#1223): extract and report only, nothing is written."""
import importlib.util
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills/sigma-loop/scripts/decision_import.py"
TOOL_MARKER = "TOOL_MARKER-TOOL-OUTPUT-9431"


def _mod():
    spec = importlib.util.spec_from_file_location("decision_import", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _repo(tmp_path):
    sdlc = tmp_path / "repo" / ".sdlc"
    sdlc.mkdir(parents=True)
    return sdlc


def _folder(profile, sdlc):
    root = str(sdlc.resolve().parent)
    d = pathlib.Path(profile) / "projects" / re.sub(r"[^A-Za-z0-9]", "-", root)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ask(tid, q, opts, ts="2026-10-01T10:00:00Z"):
    return json.dumps({"type": "assistant", "timestamp": ts, "message": {"content": [
        {"type": "tool_use", "id": tid, "name": "AskUserQuestion", "input": {"questions": [
            {"question": q, "options": [{"label": o} for o in opts]}]}}]}})


def _ans(tid, q, a, ts="2026-10-01T10:01:00Z"):
    return json.dumps({"type": "user", "timestamp": ts, "message": {"content": [
        {"type": "tool_result", "tool_use_id": tid,
         "content": 'User has answered your questions: "%s"="%s". You can now continue.' % (q, a)}]}})


def _other(tid):
    return json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": tid, "content": TOOL_MARKER}]}})


def _write(folder, name, lines):
    p = folder / name
    p.write_text("\n".join(lines) + "\n")
    return p


def _setup(tmp_path, lines, name="s1.jsonl"):
    sdlc = _repo(tmp_path)
    prof = tmp_path / "prof"
    _write(_folder(prof, sdlc), name, lines)
    return sdlc, [str(prof)]


def test_pairs_answer_and_ids_are_stable(tmp_path):
    m = _mod()
    sdlc, prof = _setup(tmp_path, [_ask("t1", "Use A or B?", ["A", "B"]), _ans("t1", "Use A or B?", "B")])
    a = m.candidates(str(sdlc), {}, prof)
    b = m.candidates(str(sdlc), {}, prof)
    assert len(a) == 1 and a[0]["choice"] == "B" and a[0]["options"] == ["A", "B"]
    assert a[0]["question"] == "Use A or B?" and a[0]["source"] == "transcript"
    assert [c["id"] for c in a] == [c["id"] for c in b] and a[0]["id"]


def test_other_tool_results_never_read(tmp_path):
    m = _mod()
    sdlc, prof = _setup(tmp_path, [
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "r1", "name": "Read", "input": {"file_path": "x"}}]}}),
        _other("r1"), _ask("t1", "Q?", ["a"]), _other("t9"), _ans("t1", "Q?", "a")])
    out = m.scan(str(sdlc), {}, prof)
    assert TOOL_MARKER not in json.dumps(out)
    assert len(out["candidates"]) == 1


def test_malformed_skipped_and_counted(tmp_path):
    m = _mod()
    sdlc, prof = _setup(tmp_path, ["{not json", "[1,2]", _ask("t1", "Q?", ["a"]), _ans("t1", "Q?", "a")])
    folder = _folder(prof[0], sdlc)
    (folder / "bad.jsonl").write_bytes(b"\xff\xfe\x00")
    out = m.scan(str(sdlc), {}, prof)
    st = out["stats"]["transcript"]
    assert len(out["candidates"]) == 1 and st["skipped"] >= 2 and st["files_read"] >= 1


def test_caps_refuse_plainly(tmp_path):
    m = _mod()
    lines = []
    for i in range(3):
        lines += [_ask("t%d" % i, "Q%d?" % i, ["a"]), _ans("t%d" % i, "Q%d?" % i, "a")]
    sdlc, prof = _setup(tmp_path, lines)
    cfg = {"decision_rubric": {"import": {"max_candidates": 2}}}
    out = m.scan(str(sdlc), cfg, prof)
    assert len(out["candidates"]) == 2 and any("candidate cap" in r for r in out["refusals"])
    cfg = {"decision_rubric": {"import": {"max_file_bytes": 10}}}
    out = m.scan(str(sdlc), cfg, prof)
    assert out["candidates"] == [] and any("size cap" in r for r in out["refusals"])
    assert out["stats"]["transcript"]["skipped"] == 1


def test_reversal_marks_earlier_superseded(tmp_path):
    m = _mod()
    sdlc, prof = _setup(tmp_path, [
        _ask("t1", "Which?", ["A", "B"], "2026-10-01T10:00:00Z"), _ans("t1", "Which?", "A", "2026-10-01T10:01:00Z"),
        _ask("t2", "Which?", ["A", "B"], "2026-10-02T10:00:00Z"), _ans("t2", "Which?", "B", "2026-10-02T10:01:00Z"),
        _ask("t3", "Same?", ["A"], "2026-10-01T11:00:00Z"), _ans("t3", "Same?", "A", "2026-10-01T11:01:00Z"),
        _ask("t4", "Same?", ["A"], "2026-10-02T11:00:00Z"), _ans("t4", "Same?", "A", "2026-10-02T11:01:00Z")])
    out = m.scan(str(sdlc), {}, prof)
    sup = {(c["question"], c["choice"]): c.get("superseded") for c in out["candidates"]}
    assert sup[("Which?", "A")] is True and sup[("Which?", "B")] is False
    assert sup[("Same?", "A")] is False
    assert out["stats"]["transcript"]["superseded"] == 1 and len(out["candidates"]) == 4


QA = ("<!-- sigma:unpark-qa:start -->\n## Unpark review\n\n_Which lane?_\n- **lane** — fast lane\n"
      "<!-- sigma:unpark-qa:end -->")
DEC = ("Decision record `abc`\n<!-- sigma:unpark-qa:start -->\n- Decision record `abc`\n- kind: scope\n"
       "- how: human\n- choice: keep parked\n- reason: waiting on design\n<!-- sigma:unpark-qa:end -->")


def _fake_comments(calls):
    def fn(config, goal, **kw):
        calls.append(goal)
        return [{"id": "IC_1", "author": "x", "body": QA, "created_at": "2026-10-03T00:00:00Z"},
                {"id": "IC_2", "author": "x", "body": DEC, "created_at": "2026-10-04T00:00:00Z"},
                {"id": "IC_3", "author": "x", "body": "plain", "created_at": "2026-10-05T00:00:00Z"}]
    return fn


def test_comment_sources_via_injected_reader(tmp_path):
    m = _mod()
    sdlc = _repo(tmp_path)
    cfg = {"decision_rubric": {"import": {"comments": True}}}
    calls = []
    out = m.scan(str(sdlc), cfg, [], issues=[7], comments_fn=_fake_comments(calls))
    by = {c["source"]: c for c in out["candidates"]}
    assert by["unpark-qa"]["question"] == "Which lane?" and by["unpark-qa"]["choice"] == "fast lane"
    assert by["unpark-qa"]["issue"] == 7 and by["unpark-qa"]["id"].startswith("IC_1")
    assert by["decision-span"]["choice"] == "keep parked" and by["decision-span"]["reason"] == "waiting on design"
    again = m.scan(str(sdlc), cfg, [], issues=[7], comments_fn=_fake_comments([]))
    assert [c["id"] for c in again["candidates"]] == [c["id"] for c in out["candidates"]]
    calls.clear()
    m.scan(str(sdlc), {}, [], issues=[7], comments_fn=_fake_comments(calls))
    assert calls == []


def test_import_limits_defaults_and_bad_types():
    cfg = importlib.util.spec_from_file_location("c", ROOT / "skills/sigma-loop/scripts/decision_rubric_cfg.py")
    mod = importlib.util.module_from_spec(cfg)
    cfg.loader.exec_module(mod)
    d = mod.import_limits({})
    assert d["max_file_bytes"] > 0 and d["max_candidates"] > 0 and d["comments"] is False
    assert mod.import_limits({"decision_rubric": {"import": "x"}}) == d
    assert mod.import_limits({"decision_rubric": {"import": {"max_candidates": "9"}}}) == d
    assert mod.import_limits({"decision_rubric": {"import": {"max_candidates": 5}}})["max_candidates"] == 5


def _tree(root):
    return sorted((str(p.relative_to(root)), p.stat().st_mtime_ns) for p in root.rglob("*"))


def test_cli_scan_counts_clipped_samples_and_writes_nothing(tmp_path):
    long_q = "Q" * 200 + "?"
    lines = []
    for i in range(12):
        lines += [_ask("t%d" % i, long_q + str(i), ["a"]), _ans("t%d" % i, long_q + str(i), "C" * 100)]
    sdlc, prof = _setup(tmp_path, lines)
    (sdlc / "config.json").write_text("{}")
    before = _tree(tmp_path)
    r = subprocess.run([sys.executable, str(SCRIPT), "scan", str(sdlc), "--profile", prof[0]],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert "files_read" in r.stdout and "candidates" in r.stdout and "superseded" in r.stdout
    samples = [l for l in r.stdout.splitlines() if l.startswith("  - ")]
    assert len(samples) == 10 and all(len(l) < 200 for l in samples)
    assert "Q" * 100 not in r.stdout
    assert _tree(tmp_path) == before


def test_help_exits_zero():
    r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
