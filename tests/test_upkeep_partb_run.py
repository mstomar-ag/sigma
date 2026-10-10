"""#953: one end-to-end run of upkeep part B on a local scratch bare remote with a FAKE resolver, and the record that says so.

Every conflict level is driven through the shipped engine: real git in scratch repositories with a local bare remote, a
fake `claude` script written into the temporary directory (the Level 2 rig), a stand-in for the verify runner and a stand-in
for the issue filer. No real model, no network and no hosting service is involved, and nothing here shows that a real model
resolves or reviews a conflict. `transcript()` is the single source of the committed record
`docs/launch/evidence/upkeep-part-b-run.md`; a test compares the record's block to it. The documented gesture:
`python -m pytest tests/test_upkeep_partb_run.py -q`. The control: break the record (edit one word of its block) and the
last test goes red."""
import pathlib

import test_feature_rebase as base
import test_upkeep_level1 as l1
import test_upkeep_level2 as l2

ROOT = pathlib.Path(__file__).resolve().parent.parent
RECORD = ROOT / "docs" / "launch" / "evidence" / "upkeep-part-b-run.md"
BEGIN, END = "<!-- transcript:begin -->", "<!-- transcript:end -->"
FEATURE = l1.FEATURE


def transcript(tmp, monkeypatch):
    """Run the four scenarios under `tmp` -> (lines, facts). Only outcomes and counts are recorded: no ids, no times."""
    tmp = pathlib.Path(tmp)
    m = l1.mod()
    lines = ["remote: a local bare repository per scenario; resolver and reviewer: a fake script; no model, no hosting service"]
    facts = {}

    # 1. gate closed: the conflict is left exactly as it was before part B existed
    w = l2.conflict_world(l2.mk(tmp / "closed"))
    before = w.tip(FEATURE)
    report, spy = l1.run_pass(m, w, l1.cfg(open_=False))
    facts["closed"] = (report, spy, before, w)
    lines.append("1 gate closed: outcome=%s; pushes=%d; unit branch unchanged=%s" % (
        report["outcome"], len(spy.pushes()), w.tip(FEATURE) == before))

    # 2. Level 1: a changelog-only conflict, resolved mechanically
    w = l1.world(l2.mk(tmp / "level1"))
    before = w.tip(FEATURE)
    l1.verify_ok(m, monkeypatch)
    report, spy = l1.run_pass(m, w, l1.cfg())
    facts["level1"] = (report, spy, before, w)
    lines.append("2 level 1 (mechanical), changelog-only conflict: outcome=%s; level=%s; pushes=%d; backup kept=%s" % (
        report["outcome"], report["resolved"]["level"], len(spy.pushes()), bool(report["backup"])))

    # 3. Level 2: a source conflict, fake resolver and fake reviewer both agree
    rig = l2.Rig(l2.mk(tmp / "rig2"), monkeypatch)
    w = l2.conflict_world(l2.mk(tmp / "level2"))
    before = w.tip(FEATURE)
    rig.install(m, w)
    l1.verify_ok(m, monkeypatch)
    report, spy = l1.run_pass(m, w, l2.cfg())
    facts["level2"] = (report, spy, before, w, rig)
    lines.append("3 level 2 (agent), source conflict, fake resolver then fake reviewer approves: outcome=%s; level=%s; "
                 "calls=%s; pushes=%d; backup kept=%s" % (
                     report["outcome"], report["resolved"]["level"], "+".join(c["role"] for c in rig.calls()),
                     len(spy.pushes()), bool(report["backup"])))

    # 4. Level 2 again, the fake reviewer blocks: parked, nothing pushed
    rig = l2.Rig(l2.mk(tmp / "rig3"), monkeypatch)
    rig.mode({"review": {"verdict": "block", "reasons": ["drops the integration side"]}})
    w = l2.conflict_world(l2.mk(tmp / "blocked"))
    before = w.tip(FEATURE)
    rig.install(m, w)
    report, spy = l1.run_pass(m, w, l2.cfg())
    facts["blocked"] = (report, spy, before, w, rig)
    lines.append("4 level 2, fake reviewer blocks: outcome=%s; calls=%s; pushes=%d; unit branch unchanged=%s; reason=%s" % (
        report["outcome"], "+".join(c["role"] for c in rig.calls()), len(spy.pushes()), w.tip(FEATURE) == before,
        "reviewer-blocked" if "reviewer-blocked" in report["why"] else "other"))
    return lines, facts


def test_one_run_through_every_level_with_a_fake_resolver(tmp_path, monkeypatch):
    lines, f = transcript(tmp_path, monkeypatch)
    m = l1.mod()
    report, spy, before, w = f["closed"]
    assert not spy.pushes() and w.tip(FEATURE) == before and report["outcome"] != m.REBASED
    report, spy, before, w = f["level1"]
    assert report["outcome"] == m.REBASED and len(spy.pushes()) == 1 and w.tip(FEATURE) != before
    report, spy, before, w, rig = f["level2"]
    assert report["outcome"] == m.REBASED and [c["role"] for c in rig.calls()] == ["resolve", "review"]
    assert len(spy.pushes()) == 1 and w.tip(FEATURE) != before
    report, spy, before, w, rig = f["blocked"]
    assert not spy.pushes() and w.tip(FEATURE) == before and "reviewer-blocked" in report["why"]
    assert len(lines) == 5


def test_the_committed_record_is_this_run_and_says_what_was_not_covered(tmp_path, monkeypatch):
    lines, _facts = transcript(tmp_path, monkeypatch)
    text = RECORD.read_text(encoding="utf-8")
    assert BEGIN in text and END in text, "the record must wrap its transcript in the two markers"
    block = text.split(BEGIN)[1].split(END)[0].strip().removeprefix("```text").removesuffix("```").strip()
    assert block == "\n".join(lines), "regenerate the record's block from transcript()"
    prose = " ".join(text.replace(block, "").lower().split())
    for needed in ("fake", "not run", "real model", "chat", "ruleset", "catalog"):
        assert needed in prose, needed
