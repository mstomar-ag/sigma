"""#1044 — evals/skills/smoke.py on fixture trees only (never the real tree).

Every rule below was run red by breaking the matching line in smoke.py (see .sdlc/research/1044-controls.md).
"""
import importlib.util
import json
import pathlib

import pytest
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SMOKE = ROOT / "evals" / "skills" / "smoke.py"
README = ROOT / "evals" / "README.md"
spec = importlib.util.spec_from_file_location("smoke", SMOKE)
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)

NAMES = ("alpha", "beta")


def _tree(tmp_path, names=NAMES):
    """Return (skills, cards); every skill has SKILL.md and a described card."""
    skills, cards = tmp_path / "skills", tmp_path / "cards"
    cards.mkdir(parents=True, exist_ok=True)
    for n in names:
        (skills / n).mkdir(parents=True, exist_ok=True)
        (skills / n / "SKILL.md").write_text(f"# {n}\nGATE-{n}\n")
        _card(cards, n)
    return skills, cards


def _card(cards, name, **kw):
    d = {"skill": name, "kind": "described", "gates": [], "scripts": [], "artifacts": []}
    d.update(kw)
    (cards / f"{name}.json").write_text(json.dumps(d))


def _run(skills, cards, *extra):
    r = subprocess.run([sys.executable, str(SMOKE), "--root", str(skills), "--cards-dir", str(cards), *extra],
                       capture_output=True, text=True, cwd=ROOT)
    return r.returncode, r.stdout


def _script(tmp_path, skill="alpha", name="run.py", body="print('ok')"):
    d = tmp_path / "skills" / skill / "scripts"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(body)
    return f"skills/{skill}/scripts/{name}"


def _red(rc, out, *needles):
    assert rc == 1, out
    for n in needles:
        assert n in out, out


def test_clean_fixture_tree_is_green(tmp_path):
    rc, out = _run(*_tree(tmp_path))
    assert rc == 0 and "FINDING" not in out, out


def test_dir_without_card_is_red_and_named(tmp_path):
    skills, cards = _tree(tmp_path)
    (cards / "beta.json").unlink()
    _red(*_run(skills, cards), "skill beta: no card")


def test_card_without_dir_is_red_and_named(tmp_path):
    skills, cards = _tree(tmp_path)
    _card(cards, "ghost")
    _red(*_run(skills, cards), "card ghost")


def test_dir_without_skill_md_is_red_and_named(tmp_path):
    skills, cards = _tree(tmp_path)
    (skills / "beta" / "SKILL.md").unlink()
    rc, out = _run(skills, cards)
    _red(rc, out, "skill beta: no SKILL.md")
    assert "no card" not in out  # a card exists: reported once, for the missing file
    _card(cards, "beta", gates=["anything"])  # gate check is skipped, no crash
    assert "Traceback" not in _run(skills, cards)[1]


def test_plain_files_and_dot_dirs_are_ignored(tmp_path):
    skills, cards = _tree(tmp_path)
    (skills / "README.md").write_text("x")
    (skills / ".hidden").mkdir()
    rc, out = _run(skills, cards)
    assert rc == 0, out


def test_gate_in_kept_prefix_passes(tmp_path):
    skills, cards = _tree(tmp_path)
    _card(cards, "alpha", gates=["GATE-alpha"])
    assert _run(skills, cards)[0] == 0


def test_gate_beyond_kept_prefix_is_red(tmp_path):
    skills, cards = _tree(tmp_path)
    ss = smoke._structure()
    cut = ss.COMPACTION_TOKEN_CAP * ss.CHARS_PER_TOKEN
    (skills / "alpha" / "SKILL.md").write_text("x" * cut + "\nLATE-GATE\n")
    _card(cards, "alpha", gates=["LATE-GATE"])
    _red(*_run(skills, cards), "skill alpha", "LATE-GATE")


def test_producer_existing_path_or_agent_passes(tmp_path):
    skills, cards = _tree(tmp_path)
    rel = _script(tmp_path)
    _card(cards, "alpha", scripts=[{"path": rel, "role": "library"}],
          artifacts=[{"path": "a.md", "producer": rel}, {"path": "b.md", "producer": "agent"}])
    rc, out = _run(skills, cards)
    assert rc == 0, out


def test_producer_missing_path_is_red(tmp_path):
    skills, cards = _tree(tmp_path)
    _card(cards, "alpha", artifacts=[{"path": "a.md", "producer": "skills/alpha/scripts/nope.py"}])
    _red(*_run(skills, cards), "skill alpha", "nope.py")
    _card(cards, "alpha", artifacts=[{"path": "a.md", "producer": "../../etc/passwd"}])
    _red(*_run(skills, cards), "skill alpha")


def _exercised(tmp_path, cards, **entry):
    rel = _script(tmp_path)
    _card(cards, "alpha", kind="exercised", scripts=[dict({"path": rel, "role": "gesture"}, **entry)])
    return rel


def test_exercised_without_passing_fixture_is_finding(tmp_path):
    skills, cards = _tree(tmp_path)
    rel = _exercised(tmp_path, cards)
    _red(*_run(skills, cards), "skill alpha", "exercised")  # no fixture at all
    _exercised(tmp_path, cards, fixture=[sys.executable, rel], expect="NOT-PRINTED")
    _red(*_run(skills, cards), "NOT-PRINTED")
    _exercised(tmp_path, cards, fixture=[sys.executable, "-c", "raise SystemExit(3)"])
    _red(*_run(skills, cards), "exited 3")
    _exercised(tmp_path, cards, fixture=[sys.executable, "-c", "import time; time.sleep(30)"])
    _red(*_run(skills, cards, "--fixture-timeout", "0.5"), "timed out")
    _card(cards, "alpha", kind="exercised")  # claims exercised, runs nothing
    _red(*_run(skills, cards), "no fixture run passed")


def test_exercised_with_passing_fixture_is_green(tmp_path):
    skills, cards = _tree(tmp_path)
    rel = _exercised(tmp_path, cards)
    _exercised(tmp_path, cards, fixture=[sys.executable, rel], expect="ok")
    rc, out = _run(skills, cards)
    assert rc == 0, out


def test_described_needs_no_fixture(tmp_path):
    skills, cards = _tree(tmp_path)
    rel = _script(tmp_path)
    _card(cards, "alpha", kind="described", scripts=[{"path": rel, "role": "gesture"}])
    assert _run(skills, cards)[0] == 0


def test_unlisted_script_is_red(tmp_path):
    skills, cards = _tree(tmp_path)
    rel = _script(tmp_path)
    _script(tmp_path, name="helper.sh")
    _script(tmp_path, name="notes.txt")  # not py/sh: ignored
    nested = tmp_path / "skills/alpha/scripts/sub"
    nested.mkdir()
    (nested / "deep.py").write_text("")  # not recursive: ignored
    _card(cards, "alpha", scripts=[{"path": rel, "role": "library"}])
    rc, out = _run(skills, cards)
    _red(rc, out, "helper.sh")
    assert "notes.txt" not in out and "deep.py" not in out


def test_script_in_two_entries_is_red(tmp_path):
    skills, cards = _tree(tmp_path)
    rel = _script(tmp_path)
    _card(cards, "alpha", scripts=[{"path": rel, "role": "library"}])
    _card(cards, "beta", scripts=[{"path": rel, "role": "library"}])
    _red(*_run(skills, cards), "also listed")


def test_entry_without_file_is_red(tmp_path):
    skills, cards = _tree(tmp_path)
    _card(cards, "alpha", scripts=[{"path": "skills/alpha/scripts/gone.py", "role": "library"}])
    _red(*_run(skills, cards), "gone.py")
    for bad in ("/etc/hosts", "../x.py"):
        _card(cards, "alpha", scripts=[{"path": bad, "role": "library"}])
        _red(*_run(skills, cards), "skill alpha")


def test_library_role_exempt_from_fixture(tmp_path):
    skills, cards = _tree(tmp_path)
    rel = _script(tmp_path)
    _card(cards, "alpha", kind="exercised", scripts=[
        {"path": rel, "role": "library"}])
    rc, out = _run(skills, cards)
    assert "has no fixture" not in out  # only the exercised-without-passing-run finding remains
    _card(cards, "alpha", kind="pinned", scripts=[{"path": rel, "role": "library"}])
    assert _run(skills, cards)[0] == 0


def test_malformed_card_is_a_finding_not_a_traceback(tmp_path):
    skills, cards = _tree(tmp_path)
    (cards / "alpha.json").write_text("{not json")
    rc, out = _run(skills, cards)
    _red(rc, out, "alpha.json")
    assert "Traceback" not in out
    _card(cards, "alpha", kind="weird")
    _red(*_run(skills, cards), "bad kind")
    _card(cards, "alpha", skill="other")  # stem != skill field
    _red(*_run(skills, cards), "stem")


def test_each_direction_is_checked_separately(tmp_path):
    skills, cards = _tree(tmp_path)
    (skills / "beta").rename(tmp_path / "moved")
    rc, out = _run(skills, cards)
    _red(rc, out, "card beta")
    assert "no card" not in out


def test_output_has_row_footer_and_wall_time(tmp_path):
    rc, out = _run(*_tree(tmp_path))
    lines = out.splitlines()
    assert [l.split()[0] for l in lines[:len(NAMES)]] == list(NAMES)
    m = re.fullmatch(r"(\d+) skills checked, (\d+) findings, ([\d.]+)s", lines[-1])
    assert m and int(m.group(1)) == len(NAMES) and int(m.group(2)) == 0


def test_help_works():
    r = subprocess.run([sys.executable, str(SMOKE), "--help"], capture_output=True, text=True)
    assert r.returncode == 0
    for flag in ("--root", "--cards-dir", "--fixture-timeout"):
        assert flag in r.stdout


def _section():
    text = README.read_text(encoding="utf-8")
    i = text.index("## Per-skill smoke runner")
    return text[i:text.index("\n## ", i + 1)]


def test_no_hard_coded_skill_count(tmp_path):
    n = len(NAMES)
    out = _run(*_tree(tmp_path))[1]
    assert out.splitlines()[-1].startswith(f"{n} skills")
    for text in (_section(), SMOKE.read_text(encoding="utf-8")):
        assert not re.search(r"\b(42|43|44)\b", text)  # the real tree's count must never be typed
    section = re.sub(r"`[^`]*`|\d+\.\d+|#\d+", "", _section())  # code spans, versions, issue refs
    assert not re.findall(r"\b(?:[2-9]|\d\d+)\s+(?:skills|cards|directories)\b", section)


def test_readme_gesture_red_on_fake_probe_then_green(tmp_path):
    line = next(l for l in _section().splitlines() if l.startswith("python3 evals/skills/smoke.py"))
    assert "<skills>" in line and "<cards>" in line
    skills, cards = _tree(tmp_path)
    (skills / "sigma-probe").mkdir()
    cmd = line.replace("<skills>", str(skills)).replace("<cards>", str(cards)).replace("python3", sys.executable, 1)

    def run():
        r = subprocess.run(cmd.split(), capture_output=True, text=True, cwd=ROOT)
        return r.returncode, r.stdout
    _red(*run(), "sigma-probe")
    (skills / "sigma-probe").rmdir()
    assert run()[0] == 0


@pytest.mark.parametrize("shape", [
    {"artifacts": [{"name": "x"}]},   # artifact with no producer
    {"scripts": [{"role": "gesture"}]},  # script with no path
    {"scripts": ["run.py"]},          # script entry is a bare string
], ids=["artifact_no_producer", "script_no_path", "script_bare_string"])
def test_malformed_entry_is_a_finding_not_a_traceback_shapes(tmp_path, shape):
    skills, cards = _tree(tmp_path)
    _card(cards, "alpha", **shape)
    rc, out = _run(skills, cards)
    assert rc == 1 and "Traceback" not in out and "TypeError" not in out, out
    assert "skill alpha:" in out, out


@pytest.mark.parametrize("shape", [
    {"gates": 5}, {"gates": [1]}, {"gates": [[1]]}, {"gates": "zzzqqq"}, {"gates": None}, {"gates": [""]},
    {"artifacts": 5}, {"artifacts": None}, {"artifacts": [{"path": "x", "producer": 5}]},
    {"artifacts": [{"path": ["x"], "producer": "agent"}]},
    {"scripts": 5}, {"scripts": None},
    {"scripts": [{"path": ["a"], "role": "library"}]},
    {"scripts": [{"path": "a.py", "role": "library", "fixture": 5}]},
    {"scripts": [{"path": "a.py", "role": "library", "fixture": [1]}]},
    {"scripts": [{"path": "a.py", "role": "gesture", "fixture": ["true"], "expect": 5}]},
    {"scripts": [{"path": "a.py", "role": "gesture", "fixture": ["true"], "expect": None}]},
    {"scripts": [{"path": "a.py", "role": ["x"]}]},
    {"kind": ["x"]},
], ids=lambda s: json.dumps(s)[:40])
def test_odd_field_types_are_named_findings(tmp_path, shape):
    skills, cards = _tree(tmp_path)
    _card(cards, "alpha", **shape)
    rc, out = _run(skills, cards)
    assert rc == 1 and "Traceback" not in out and "Error" not in out, out
    assert "skill alpha:" in out, out


@pytest.mark.parametrize("text", ["null", "[1]", "5", '"s"'])
def test_non_object_card_is_a_finding(tmp_path, text):
    skills, cards = _tree(tmp_path)
    (cards / "alpha.json").write_text(text)
    rc, out = _run(skills, cards)
    assert rc == 1 and "Traceback" not in out and "alpha.json" in out, out


def test_fixture_non_utf8_stdout_is_not_a_traceback(tmp_path):
    skills, cards = _tree(tmp_path)
    rel = _exercised(tmp_path, cards)
    bad = "import sys; sys.stdout.buffer.write(bytes([0xFF, 0xFE]))"
    _exercised(tmp_path, cards, fixture=[sys.executable, "-c", bad], expect="NOT-PRINTED")
    _red(*_run(skills, cards), "NOT-PRINTED")
    _exercised(tmp_path, cards, fixture=[sys.executable, "-c", bad])
    rc, out = _run(skills, cards)
    assert rc == 0, out


def test_fixture_nul_in_argv_is_a_named_finding(tmp_path):
    skills, cards = _tree(tmp_path)
    _exercised(tmp_path, cards)
    _exercised(tmp_path, cards, fixture=[sys.executable, "-c", "pass" + chr(0)])
    _red(*_run(skills, cards), "fixture could not start")
