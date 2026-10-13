"""#1342: a public repository with records on keeps free text out of issue text.

The unpark half is driven through the real `resolve` verb function; the applier half through `tick`;
the doctor half through the rulebook readout. Every closed-gate / private case is compared against
a hand-written literal of the pre-#1342 bytes, never against the code under test.
"""
import copy
import json

import test_unpark as tu
import test_applier as ta
import test_decision_record as tdr

_mod = tu._mod
SECRET = "ZEBRA-SECRET-42 rotate the vault"


def _cfg_for(visibility, enabled=True):
    cfg = tu._config()
    rec = {"enabled": enabled}
    if visibility is not None:
        rec["repo_visibility"] = visibility
    cfg["rulebook"] = {"records": rec}
    return cfg


def _resolve(tmp_path, cfg, decision="unpark"):
    u = _mod("unpark")
    run = tu._runner(views={"5": tu._view(comments=[tu._park("x")])})
    u.resolve(str(tmp_path), cfg, 5, {"decision": "start it again", "why": SECRET}, decision, run=run,
              questions_asked=[{"id": "why", "ask": "What is the reason?"}])
    bodies = tdr._bodies(run, 5)
    edits = [c[c.index("--body") + 1] for c in run.calls if c[:2] == ["issue", "edit"] and "--body" in c]
    return bodies, edits


def test_public_with_records_posts_keys_only_in_body_and_comment(tmp_path):
    bodies, edits = _resolve(tmp_path, _cfg_for("public"))
    assert bodies and edits
    for text in bodies + edits:
        assert "ZEBRA" not in text and "start it again" not in text
        assert "- **why** — recorded privately" in text and "What is the reason?" in text


def test_public_keep_parked_marker_drops_the_reason(tmp_path):
    bodies, edits = _resolve(tmp_path, _cfg_for("public"), decision="keep-parked")
    assert bodies and all("ZEBRA" not in t for t in bodies + edits)


def test_private_and_closed_gate_are_byte_identical_to_before(tmp_path):
    for n, cfg in enumerate((_cfg_for("private"), _cfg_for(None), _cfg_for("public", enabled=False),
                             tu._config())):
        d = tmp_path / str(n)
        d.mkdir()
        bodies, edits = _resolve(d, cfg)
        assert bodies and edits
        for text in bodies + edits:
            assert "- **why** — " + SECRET in text
            assert "recorded privately" not in text
        # the exact fenced block, byte for byte
        u = _mod("unpark")
        want = "\n".join([u.backlog_check.UNPARK_QA_START, u.QA_HEADING, "",
                          "- **decision** — start it again", "_What is the reason?_",
                          "- **why** — " + SECRET, "", u.backlog_check.UNPARK_QA_END])
        assert bodies[0].startswith(want)


def _applier_text(tmp_path, records_on, visibility):
    sd, cfg, rid = ta._setup(tmp_path)
    ta._ask(sd, cfg)
    cfg = copy.deepcopy(cfg)
    cfg["rulebook"]["records"] = {"enabled": records_on}
    if visibility:
        cfg["rulebook"]["records"]["repo_visibility"] = visibility
    acts = ta.AP.tick(sd, cfg, ta.T0 + 61 * ta.MIN, source=ta.Src())
    return acts[0]["text"], rid


def test_applier_text_omits_option_only_when_public_and_records_on(tmp_path):
    for sub, on, vis, omit in (("a", True, "public", True), ("b", True, "private", False),
                               ("c", True, None, False)):
        d = tmp_path / sub
        d.mkdir()
        text, rid = _applier_text(d, on, vis)
        base = "Applied automatically by rule %s (needs_decision)" % rid
        if omit:
            assert text == base + ". No answer arrived within the window. Reply no to veto."
        else:
            assert text == base + ": merge. No answer arrived within the window. Reply no to veto."


def test_doctor_warns_only_on_public_with_records(tmp_path):
    import importlib.util, pathlib
    p = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-doctor" / "scripts" / "doctor.py"
    spec = importlib.util.spec_from_file_location("doctor_t1342", p)
    doc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(doc)
    warn = "answers are posted as keys only"
    assert warn in doc._rulebook_state(_cfg_for("public"), tmp_path)
    assert warn not in doc._rulebook_state(_cfg_for("private"), tmp_path)
    assert warn not in doc._rulebook_state(_cfg_for("public", enabled=False), tmp_path)
