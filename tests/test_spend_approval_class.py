"""Slice 7 of the decision rubric (#997): the recorded permission is bound to a class, an optional
action digest and a durable use counter; `scrub_marker` quotes a first-line marker."""
import hashlib
import json
import pathlib
import subprocess
import sys

import pytest

import test_spend_approval as base

sa = base.sa
FakeSource, CFG, BODY = base.FakeSource, base.CFG, base.BODY


@pytest.fixture(autouse=True)
def _tmp_sdlc(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".sdlc").mkdir()


def _digest(action):
    return hashlib.sha256(" ".join(action.split()).encode("utf-8")).hexdigest()[:12]


def test_non_spend_class_is_refused_loudly():
    src = FakeSource()
    out = sa.check(".sdlc", "77", "drop the table", CFG, src, hardstop_class="destructive")
    assert out.startswith("DENIED") and "destructive" in out
    assert src.notes == []


def test_class_must_also_be_listed_in_config():
    cfg = {"spend_approval": {"enabled": True, "approvers": ["Alice"], "classes": []}}
    out = sa.check(".sdlc", "77", "render", cfg, FakeSource(), hardstop_class="spend")
    assert out.startswith("DENIED")


def test_spend_class_and_no_class_are_both_approved_alike():
    assert sa.check(".sdlc", "77", "render", CFG, FakeSource(), hardstop_class="spend") == \
        "APPROVED campaign-7"


def test_no_class_is_byte_identical():
    src = FakeSource()
    assert sa.check(".sdlc", "77", "render", CFG, src) == "APPROVED campaign-7"
    audit = src.notes[0][1]
    assert "class" not in audit and "digest" not in audit
    assert sa.check(".sdlc", "77", "render", {}, FakeSource()) == "OFF"


def test_digest_mismatch_is_denied():
    body = "sigma:spend-approved=campaign-7:" + _digest("render clips") + "\nbody\n"
    assert sa.check(".sdlc", "77", "render clips", CFG, FakeSource(body=body)) == "APPROVED campaign-7"
    out = sa.check(".sdlc", "77", "render MORE clips", CFG,
                   FakeSource(body=body.replace("campaign-7", "campaign-8")))
    assert out.startswith("DENIED") and "digest" in out


def test_counter_survives_comment_delete():
    src = FakeSource()
    assert sa.check(".sdlc", "77", "render", CFG, src).startswith("APPROVED")
    src.comments.clear()                                  # the audit comment is deleted
    out = sa.check(".sdlc", "77", "render", CFG, src)
    assert out.startswith("DENIED") and "already used" in out
    state = json.loads(pathlib.Path(".sdlc/state/spend-approval-used.json").read_text())
    assert "77:campaign-7" in state


def test_unwritable_counter_denies(tmp_path):
    (tmp_path / ".sdlc" / "state").write_text("not a directory")
    out = sa.check(".sdlc", "77", "render", CFG, FakeSource())
    assert out.startswith("DENIED")


def test_scrub_quotes_a_first_line_marker():
    body, changed = sa.scrub_marker("sigma:spend-approved=x\nmore\n")
    assert changed and body == "`sigma:spend-approved=x`\nmore\n"
    plain = "hello\nsigma:spend-approved=x\n"
    assert sa.scrub_marker(plain) == (plain, False)
    assert sa.scrub_marker("") == ("", False)


def test_documented_gesture_denies_other_class(tmp_path):
    (tmp_path / "w").mkdir()
    d = base._sdlc(tmp_path / "w", {"enabled": True, "approvers": ["alice"]})
    cmd = base._skill_gesture().replace("${CLAUDE_SKILL_DIR}", str(base.S.parent))
    cmd = cmd.replace('"$goal"', "77").replace(" .sdlc ", f" {d} ")
    import shlex
    argv = shlex.split(cmd)[1:] + ["--class", "destructive"]
    r = subprocess.run([sys.executable, *argv], capture_output=True, text=True, cwd=tmp_path,
                       input="drop it")
    assert r.returncode == 3 and r.stdout.split()[0] == "DENIED" and "destructive" in r.stdout, \
        (r.stdout, r.stderr)
