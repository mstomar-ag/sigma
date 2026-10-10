#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Per-skill smoke runner (#1044, slice 1 of #1043): every skill directory has a card, every card a directory.

Reads `<root>/*/` (every child directory, listed here and NOT through `skill_structure.skill_dirs`, so a
directory that function skips is still seen) and `<cards-dir>/*.json`, compares them in BOTH directions,
and reports findings. Exit 1 on any finding, else 0. Read-only on the tree, stdlib only.

Card: {"skill", "kind": exercised|pinned|described, "gates": [str],
       "scripts": [{"path", "role": gesture|library, "fixture"?, "expect"?}],
       "artifacts": [{"path", "producer"}]}
Rules (each is a finding naming the skill and the item):
  * directory without card; card without directory; card file stem != its `skill` field;
    directory without SKILL.md (reported once; that skill's gate check is then skipped).
  * every `gates` string must lie in the kept prefix of SKILL.md (first COMPACTION_TOKEN_CAP*CHARS_PER_TOKEN chars).
  * an artifact `producer` is an existing repo path or the literal `agent`.
  * `exercised` needs a gesture script whose `fixture` runs and passes; `described` is the honest label otherwise.
  * `fixture` is a shell-free argv list (JSON list of strings), run with cwd = the repo root (parent of --root);
    passing = exit 0 and, if the entry gives `expect`, that substring in stdout. A run longer than
    --fixture-timeout seconds (default 60) is a finding, never a hang.
  * every *.py / *.sh directly in `<skill>/scripts/` (not recursive) appears in exactly one script entry;
    an entry whose file is missing, or listed twice across entries, is a finding. role `gesture` needs a
    fixture on an exercised card; role `library` is exempt.
  * card paths must be relative and free of `..` (never touches the filesystem outside the repo root).
Malformed JSON / bad kind / bad role are findings, not tracebacks. Paths in script/artifact entries are
relative to the repo root (the parent of --root).
Not wired into the real-tree pytest gate: the bare `python3 evals/skills/smoke.py` stays red until cards exist.
"""
import argparse
import importlib.util
import json
import pathlib
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
KINDS = ("exercised", "pinned", "described")
ROLES = ("gesture", "library")


def _structure():
    # sys.path[0] is evals/skills when run as a script, so load the sibling module by path.
    spec = importlib.util.spec_from_file_location("skill_structure", HERE.parent / "skill_structure.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _bad_path(p):
    if not isinstance(p, str) or not p:
        return True
    q = pathlib.PurePosixPath(p)
    return q.is_absolute() or ".." in q.parts


def _shape(card):
    """Every violation of the card's field types, as short strings; empty = safe to check downstream."""
    bad = []
    def lst(k):
        v = card.get(k, [])
        if not isinstance(v, list):
            bad.append(f"{k} is not a list")
            return []
        return v
    for g in lst("gates"):
        if not (isinstance(g, str) and g):
            bad.append(f"gate {g!r} is not a non-empty string")
    for a in lst("artifacts"):
        if not (isinstance(a, dict) and isinstance(a.get("path"), str) and isinstance(a.get("producer"), str)):
            bad.append(f"artifact {a!r} needs string path and producer")
    for e in lst("scripts"):
        if not (isinstance(e, dict) and isinstance(e.get("path"), str)):
            bad.append(f"script entry {e!r} needs a string path")
            continue
        if e.get("role") not in ROLES:
            bad.append(f"script {e['path']} bad role {e.get('role')!r}")
        fx = e.get("fixture", [])
        if not (isinstance(fx, list) and all(isinstance(x, str) for x in fx)):
            bad.append(f"script {e['path']} fixture is not a list of strings")
        if not isinstance(e.get("expect", ""), str):
            bad.append(f"script {e['path']} expect is not a string")
    return bad


def _run_fixture(argv, cwd, timeout, expect):
    if not (isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)):
        return "fixture is not a non-empty list of strings"
    try:
        r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return f"fixture timed out after {timeout}s"
    except (OSError, ValueError) as e:
        return f"fixture could not start: {e}"
    if r.returncode != 0:
        return f"fixture exited {r.returncode}"
    if expect is not None and expect not in r.stdout:
        return f"fixture stdout lacks expected text {expect!r}"
    return None


def check(root, cards_dir, timeout=60):
    """Return (rows, findings): rows = [(skill, kind)], findings = [str]."""
    ss = _structure()
    keep = ss.COMPACTION_TOKEN_CAP * ss.CHARS_PER_TOKEN
    root, repo = pathlib.Path(root).resolve(), pathlib.Path(root).resolve().parent
    dirs = sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
    cards, out = {}, []
    cdir = pathlib.Path(cards_dir)
    for f in sorted(cdir.glob("*.json")) if cdir.is_dir() else []:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            assert isinstance(d, dict)
        except (ValueError, AssertionError, OSError):
            out.append(f"card {f.name}: malformed JSON (not an object)")
            continue
        name = d.get("skill") if isinstance(d.get("skill"), str) else f.stem
        if name != f.stem:
            out.append(f"card {f.name}: stem does not match its skill field {name!r}")
        if name in cards:
            out.append(f"card {name}: two cards for one skill")
        cards[name] = d
    for name in sorted(set(cards) - set(dirs)):
        out.append(f"card {name}: no skills directory of that name")
    rows, seen = [], {}
    for s in dirs:
        card = cards.get(s)
        has_md = (root / s / "SKILL.md").is_file()
        if card is None:
            out.append(f"skill {s}: no card")
        if not has_md:
            out.append(f"skill {s}: no SKILL.md")
        if card is None:
            rows.append((s, "none"))
            continue
        kind = card.get("kind")
        if not isinstance(kind, str):
            kind = None
        bad = _shape(card)
        for b in bad:
            out.append(f"skill {s}: {b}")
        if kind not in KINDS:
            out.append(f"skill {s}: bad kind {kind!r}")
        rows.append((s, kind if kind in KINDS else "bad"))
        if bad:
            continue
        if has_md:
            prefix = (root / s / "SKILL.md").read_text(encoding="utf-8", errors="replace")[:keep]
            for g in card.get("gates") or []:
                if g not in prefix:
                    out.append(f"skill {s}: gate {g!r} not in kept prefix of SKILL.md")
        for a in card.get("artifacts") or []:
            prod = a.get("producer") if isinstance(a, dict) else None
            if prod == "agent":
                continue
            if _bad_path(prod):
                out.append(f"skill {s}: producer {prod!r} is not a relative repo path or 'agent'")
            elif not (repo / prod).exists():
                out.append(f"skill {s}: producer {prod} does not exist")
        passing = False
        for e in card.get("scripts") or []:
            p = e.get("path") if isinstance(e, dict) else None
            if _bad_path(p):
                out.append(f"skill {s}: script path {p!r} is missing, absolute or contains '..'")
                continue
            if e.get("role") not in ROLES:
                out.append(f"skill {s}: script {p} bad role {e.get('role')!r}")
            if p in seen:
                out.append(f"skill {s}: script {p} also listed by {seen[p]}")
            seen[p] = s
            if not (repo / p).is_file():
                out.append(f"skill {s}: script {p} has no file")
                continue
            if kind == "exercised" and e.get("role") == "gesture":
                if "fixture" not in e:
                    out.append(f"skill {s}: gesture script {p} has no fixture")
                else:
                    err = _run_fixture(e["fixture"], repo, timeout, e.get("expect"))
                    passing = passing or err is None
                    if err:
                        out.append(f"skill {s}: script {p}: {err}")
        if kind == "exercised" and not passing:
            out.append(f"skill {s}: kind exercised but no fixture run passed (label it described)")
        listed = {e.get("path") for e in card.get("scripts") or [] if isinstance(e, dict)}
        sd = root / s / "scripts"
        for f in sorted(sd.iterdir()) if sd.is_dir() else []:
            rel = f"{root.name}/{s}/scripts/{f.name}"
            if f.is_file() and f.suffix in (".py", ".sh") and rel not in listed:
                out.append(f"skill {s}: script {rel} in no card entry")
    return rows, out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(REPO / "skills"), help="skills directory (default: <repo>/skills)")
    ap.add_argument("--cards-dir", default=str(HERE / "cards"), help="directory of <skill>.json cards")
    ap.add_argument("--fixture-timeout", type=float, default=60, help="seconds per fixture before it is a finding (default 60)")
    a = ap.parse_args(argv)
    t0 = time.monotonic()
    rows, findings = check(a.root, a.cards_dir, a.fixture_timeout)
    for s, k in rows:
        print(f"{s}  {k}")
    for f in findings:
        print(f"FINDING {f}")
    print(f"{len(rows)} skills checked, {len(findings)} findings, {time.monotonic() - t0:.2f}s")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
