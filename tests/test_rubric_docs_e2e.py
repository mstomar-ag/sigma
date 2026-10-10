"""Slice 21 of story 988 (#1011): the docs describe no confirmation queue, and one offline end-to-end
run covers every part of the decision rubric on a throwaway repository."""
import importlib.util
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

DOCS = [
    "AGENTS.md", "README.md", "docs/label-model.md", "docs/agent-rules-detail.md", "docs/board.md",
    "docs/branching-model.md", "docs/dossier-pipeline.md",
    "skills/sigma-promote/SKILL.md", "skills/sigma-promote/references/selection.md",
    "skills/sigma-unpark/SKILL.md", "skills/sigma-scope/SKILL.md", "skills/sigma-define/SKILL.md",
    "skills/sigma-setup/references/public-repo.md", "skills/sigma-goal-review/references/confirm.md",
    "skills/sigma-loop/SKILL.md", "skills/sigma-loop/references/filing.md",
]
#: a paragraph that names the old label must also say it is gone
LEGACY = re.compile(r"legacy|retired|no longer|leftover|migrat|old ", re.I)
STALE = (
    "`--queue actionable` is the default posture",
    "removing `sdlc:needs-confirmation`",
    "approval gate",
    "approval queue",
    "awaiting your confirmation",
)


def _paragraphs(text):
    return [p for p in re.split(r"\n\s*\n", text) if p.strip()]


def test_docs_describe_no_confirmation_queue():
    bad = []
    for rel in DOCS:
        text = (ROOT / rel).read_text(encoding="utf-8")
        for para in _paragraphs(text):
            low = para.lower()
            if "needs-confirmation" in low and not LEGACY.search(para):
                bad.append("%s: %s" % (rel, " ".join(para.split())[:90]))
            for phrase in STALE:
                if phrase in para:
                    bad.append("%s: stale phrase %s" % (rel, phrase))
    assert not bad, "\n".join(bad)


def test_readme_names_queued_as_the_filing_default():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "`--queue queued` is the default posture" in text
    assert "`--queue actionable` is the default posture" not in text


def _e2e():
    path = ROOT / "tools" / "rubric_e2e.py"
    assert path.exists(), "tools/rubric_e2e.py is missing"
    spec = importlib.util.spec_from_file_location("rubric_e2e", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_e2e_run_covers_every_part(tmp_path):
    m = _e2e()
    out = m.run(str(tmp_path / "repo"), switches="on")
    log = "\n".join(out["log"]).lower()
    for part in ("records", "classifier", "ladder", "judge", "triage", "migration"):
        assert part in log, part
    assert out["failed"] == []


def test_e2e_all_off_matches_baseline(tmp_path):
    m = _e2e()
    base = m.run(str(tmp_path / "a"), switches="baseline")
    off = m.run(str(tmp_path / "b"), switches="off")
    on = m.run(str(tmp_path / "c"), switches="on")
    assert off["digest"] == base["digest"]
    assert on["digest"] != base["digest"]


def test_e2e_refuses_a_non_empty_foreign_directory(tmp_path):
    m = _e2e()
    (tmp_path / "keep.txt").write_text("x")
    p = subprocess.run([sys.executable, str(ROOT / "tools" / "rubric_e2e.py"), "run", str(tmp_path)],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert p.returncode == 2
    assert (tmp_path / "keep.txt").exists()
