"""README/docs claims that the code contradicts stay corrected (#434).

Short test names on purpose: `loop.py verify` reads a red run's reason from pytest's one-line
summary, which pytest drops when the node id is too long for an 80-column line.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _check(ok, message):
    """One-line AssertionError: loop.py verify's red witness only reads a single-line reason."""
    if not ok:
        raise AssertionError(" ".join(str(message).split()))


def _prose_files(*globs):
    out = []
    for pattern in globs:
        out += sorted(ROOT.glob(pattern))
    return out


def _sentences(text):
    """Whitespace-normalised sentences; a version like `3.12` never ends one."""
    for para in re.split(r"\n\s*\n", text):
        para = " ".join(para.split())
        for s in re.split(r"(?<=[a-z0-9)`*])\.\s+(?=[A-Z*`(])|;\s+", para):
            yield s


def _ci_matrix():
    """{os family: set of python versions} parsed from ci.yml's `include:` cells."""
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    cells = re.findall(r'-\s*os:\s*(\w+)-latest\s*\n\s*python:\s*"(3\.\d+)"', workflow)
    out = {}
    for os_name, version in cells:
        out.setdefault(os_name, set()).add(version)
    assert out.get("ubuntu") and out.get("macos"), out
    return out


def _ci_version_sentences():
    """(file, sentence) for every sentence in README/docs that says CI runs on some Python versions."""
    for path in _prose_files("README.md", "docs/**/*.md"):
        for s in _sentences(path.read_text()):
            if re.search(r"\bCI\b\W{0,3}(?:\(GitHub Actions\)\s*)?runs?\b|\bruns? in CI\b", s) and re.search(r"\b3\.\d{1,2}\b", s) \
                    and re.search(r"Python|Linux|macOS", s) and "Windows" not in s \
                    and not re.search(r"\bno CI\b|have no|not covered|nothing checks", s):
                yield path.relative_to(ROOT), s


def _attributed_versions(sentence):
    """Versions per OS word: each version belongs to the nearest PRECEDING `Linux`/`macOS`."""
    found, current = {}, "any"
    for tok in re.finditer(r"Linux|macOS|\b3\.\d{1,2}\b", sentence):
        if tok.group(0) == "Linux":
            current = "ubuntu"
        elif tok.group(0) == "macOS":
            current = "macos"
        else:
            found.setdefault(current, set()).add(tok.group(0))
    return found


def test_ci_matrix_claims():
    """#434: the README's CI line said 3.10 + 3.12 while ci.yml runs four Linux versions + macOS 3.12.
    Derived from ci.yml, checked on EVERY README/docs sentence that mentions CI and Python versions."""
    matrix = _ci_matrix()
    union = set().union(*matrix.values())
    seen = list(_ci_version_sentences())
    _check(any("README.md" == str(f) for f, _ in seen), "README no longer states its CI Python versions")
    for path, sentence in seen:
        for os_name, versions in _attributed_versions(sentence).items():
            expected = union if os_name == "any" else matrix[os_name]
            _check(versions == expected, f"{path}: {os_name} {sorted(versions)} != ci.yml {sorted(expected)}: {sentence}")


_NEG = re.compile(r"\bnot\b|\bnever\b|\bno\b|n't|without merg|unless", re.I)
_UNMERGED_PR = re.compile(r"\b(?:closed|abandoned|unmerged|rejected|declined)\b(?:(?!merged\b).){0,50}"
                          r"\b(?:pull requests?|PRs?)\b|\b(?:pull requests?|PRs?)\b[^.]{0,30}"
                          r"\b(?:closed|abandoned|unmerged|rejected|declined)\b", re.I)
_RESOLVES = re.compile(r"resolv|unblock|releas|lift|clear|resum|drops? `?sdlc:blocked|blocker", re.I)


def test_unmerged_pr_blocker():
    """#434: a PR closed WITHOUT merging does not resolve a blocker (blocker_scan.closed_state)."""
    import sys
    sys.path.insert(0, str(ROOT / "skills/sigma-loop/scripts"))
    import blocker_scan
    assert blocker_scan.closed_state("CLOSED", "") is False
    assert blocker_scan.closed_state("MERGED", "") is True
    assert blocker_scan.closed_state("CLOSED", "COMPLETED") is True
    for path in _prose_files("README.md", "docs/**/*.md", "skills/**/SKILL.md"):
        for s in _sentences(path.read_text()):
            if _UNMERGED_PR.search(s) and _RESOLVES.search(s) and not _NEG.search(s):
                raise AssertionError(f"{path.relative_to(ROOT)}: says an unmerged PR resolves a blocker: {s}")
    backlog = " ".join((ROOT / "docs/backlog.md").read_text().split())
    sweep = backlog[backlog.index("`discovery.auto_unpark.mode` is"):][:1800]
    _check(re.search(r"merged (?:pull request|PR)", sweep), "sweep paragraph must say a merged pull request resolves")
    _check(re.search(r"(?:closed|abandoned)\b.{0,40}without merging.{0,40}\b(?:not|never)\b", sweep), "sweep paragraph must say a PR closed without merging does not resolve")


def test_managed_is_advisory():
    text = " ".join((ROOT / "docs/managed-settings.md").read_text().split())
    section = text[text.index("## Managed settings"):]
    end = section.find(" ## ", 5)
    section = section if end < 0 else section[:end]
    _check(re.search(r"advisory[^.]{0,40}\bwrite access\b", section), "managed-settings section must say advisory against write access")
    _check(re.search(r"delet\w*[^.]{0,30}falls? back to (?:the )?local config", section), "managed-settings section must say deleting falls back to local config")
    banned = r"not advisory|tamper|\benforced\b|cannot be (?:changed|edited|bypassed|deleted|overridden|removed)"
    _check(not re.search(banned + r"|can(?:not|'t) (?:change|edit|bypass|delete|remove|override)",
                         section, re.I), "managed-settings section contradicts 'advisory'")


# ---- owner decisions of 2026-10-06: plan-review claim, loop-gated manifests, contributions, coexistence ----

def _flat(path):
    return " ".join((ROOT / path).read_text(encoding="utf-8").split())


def test_plan_review_gate_claim():
    """README no longer says nothing checks plan-review; it names what the loop refuses and what it does not gate."""
    readme = _flat("README.md")
    _check("no code checks that it ran" not in readme, "README still says no code checks that plan-review ran")
    phases = _flat("docs/running-the-loop.md")
    step = phases[phases.index("4. **Plan-Review**"):phases.index("5. **Implement**")]
    for needle in ("loop.py record done", "work.py merge", "gates.phase_record", "not gated"):
        _check(needle in step, f"README plan-review step lacks {needle!r}")
    row = next(r for r in (ROOT / "docs/features.md").read_text(encoding="utf-8").splitlines()
               if r.startswith("| **Plan review before any edit**"))
    _check("no code checks that it ran" not in row and "phase_record" in row, "the features row is stale")
    flag = next(r for r in (ROOT / "docs/configuration.md").read_text(encoding="utf-8").splitlines()
                if r.startswith("| `gates.plan_review.enabled`"))
    _check("phase_record" in flag, "the flags row does not say phase_record already refuses done")


def test_manifest_says_loop_is_gated():
    import json
    plugin = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())["description"]
    market = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())["plugins"][0]["description"]
    for text in (plugin, market):
        _check("will not finish a goal without a recorded plan" in text, f"manifest description: {text[:60]!r}")
        _check("plan-gated 7-phase" not in text, "manifest still calls the whole tool plan-gated")


def test_outside_contributions_not_accepted():
    for path in ("README.md", "CONTRIBUTING.md"):
        text = _flat(path).lower()
        _check("outside contributions are not accepted yet" in text, f"{path} lacks the contributions sentence")
        _check("issues are welcome" in text, f"{path} does not say issues are welcome")


def test_coexistence_not_claimed():
    for path in ("README.md", "docs/launch/definition.md"):
        text = _flat(path)
        _check("not claimed at launch" in text and "predecessor" in text, f"{path} lacks the coexistence statement")
        _check("#616" in text, f"{path} lacks #616")


def test_rulebook_doc_says_when_the_legacy_label_is_still_written():
    text = _flat("docs/decision-rulebook.md")
    _check("except the rebase upkeep's own findings" not in text, "rulebook doc says upkeep findings still get the legacy label")
    _check("With triage on" in text and "ai_filed.triage.enabled: false" in text
           and "rebase upkeep findings still filed with the label" in text, "rulebook doc lacks the triage-off exception")
