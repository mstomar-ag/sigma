"""Independent test review (#1964): does every acceptance criterion have a test that proves it, and
did a test the goal changed survive with the goal's production code reverted?

    python3 review_tests.py check <sdlc_dir> <goal> --review <file|->

`--review` is the independent reviewer's output. It carries one line per criterion:
`TEST-MAP AC-<n>: <test id>[, <test id>...]`, `TEST-MAP AC-<n>: NONE` or
`TEST-MAP AC-<n>: UNTESTABLE: <why>`, plus `SURVIVOR-OK <test id>: <why>` for a survivor the
reviewer judges need not fail without the change. Prints one `BLOCKING:` line per finding, then
`TEST-REVIEW: block|pass|partial - ...` last. Exit 1 on block, 0 on pass or partial, 2 on usage.

The reviewer is whatever `reviewer.py resolve` picked: this module judges its text, it never runs a
reviewer. Criterion ids are #535's legacy rule (an `AC-<n>:` prefix is kept, every other bullet
takes its position), so #535 landing changes no format here. Survivors come from `diff_revert`'s
`changed_survivors` in the verify evidence, not from mutation testing (#568).

`partial` is never a pass: it means the survivor clause could not be judged (no evidence, a
diff-revert that measured nothing, or evidence older than the field) and says why.
"""
import ast
import importlib.util
import json
import pathlib
import re
import sys

#: Tolerates the markdown a reviewer wraps lines in (bullets, quotes, bold, backticks), the same
#: decoration reviewer.py's verdict regex tolerates -- but never a newline: an empty `TEST-MAP AC-1:`
#: must not take the next line as its value.
_MAP_LINE = re.compile(r"(?im)^[ \t>*_`+-]*TEST-MAP[ \t*_`]+(AC-\d+)[ \t*_`]*:[ \t*_`]*(.*?)[ \t*_`]*$")
#: The id ends at a colon followed by space or end of line, so `a.py::Class::test` stays whole.
_OK_LINE = re.compile(r"(?im)^[ \t>*_`+-]*SURVIVOR-OK[ \t*_`]+(\S+?)[ \t*_`]*:(?:[ \t]+|$)(.*?)[ \t*_`]*$")
#: `NONE` whatever follows it: an explanation may name a real test that does not prove the criterion.
_NONE = re.compile(r"(?i)^NONE\b")
_EXPLICIT_ID = re.compile(r"^(AC-\d+):\s*(.*)$")
_UNTESTABLE = re.compile(r"(?i)^UNTESTABLE\b[\s:]*(.*)$")
_LABELS = ("Red-green proof", "Flake check", "Diff-revert")


def _load(name):
    spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def criteria(sdlc_dir, goal):
    """[(id, text)] from the recorded acceptance -- the same record, and the same branch fallback,
    the review brief reads, so the brief and this check always number alike. Raises OSError or
    ValueError when there is no valid record."""
    out = []
    for position, item in enumerate(_load("review_context").recorded_acceptance(sdlc_dir, goal)["criteria"], 1):
        explicit = _EXPLICIT_ID.match(item)
        out.append(explicit.groups() if explicit else ("AC-%d" % position, item))
    return out


def evidence(sdlc_dir, goal):
    """-> ("present", data) | ("absent", why) | ("unreachable", why). A reason never carries a path:
    it is printed into a brief that `reviewer.py check` refuses if it names the scratch path."""
    try:
        path = _load("state").evidence_path(sdlc_dir, goal)
    except ValueError:
        return "unreachable", "unsafe goal name"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "absent", "no verify evidence yet"
    except (OSError, ValueError) as exc:
        return "unreachable", "evidence unreadable (%s)" % type(exc).__name__
    return ("present", data) if isinstance(data, dict) else ("unreachable", "evidence is not an object")


def _root(sdlc_dir, goal):
    """The tree the mapped tests must exist in: the goal's worktree, else the project root."""
    try:
        rec = _load("work")._record(sdlc_dir, goal) or {}
        if rec.get("worktree") and pathlib.Path(rec["worktree"]).is_dir():
            return pathlib.Path(rec["worktree"])
    except Exception:                                 # noqa: BLE001 - no record is the drop-in case
        pass
    return pathlib.Path(sdlc_dir).resolve().parent


def _exists(root, node):
    """A pytest id `path::[Class::]name[params]` names a real function in a real file under `root`.
    An id with no `::` (a non-pytest suite) only needs its file, which must be a test path by
    `tamper_scan._TEST_PATH` -- `README.md` is no test. Inherited methods read missing."""
    rel, _, rest = node.partition("::")
    root = pathlib.Path(root).resolve()
    path = (root / rel).resolve()
    if root not in path.parents or not path.is_file():
        return False
    if not rest:
        return bool(_load("tamper_scan")._TEST_PATH.search(rel))
    parts = rest.split("::")
    parts[-1] = parts[-1].split("[", 1)[0]
    try:
        body = ast.parse(path.read_text(encoding="utf-8")).body
    except (OSError, SyntaxError, ValueError):
        return False
    for i, name in enumerate(parts):
        kinds = (ast.FunctionDef, ast.AsyncFunctionDef) if i == len(parts) - 1 else (ast.ClassDef,)
        found = next((n for n in body if isinstance(n, kinds) and n.name == name), None)
        if found is None:
            return False
        body = found.body
    return True


def parse_map(text):
    """-> ({id: value}, {ids with two different values})."""
    mapped, conflicts = {}, set()
    for cid, value in _MAP_LINE.findall(text or ""):
        if cid in mapped and mapped[cid] != value:
            conflicts.add(cid)
        mapped.setdefault(cid, value)
    return mapped, conflicts


def check(sdlc_dir, goal, text, root=None):
    """-> (outcome, lines): outcome is block, pass or partial; the last line is `TEST-REVIEW: ...`."""
    root = root or _root(sdlc_dir, goal)
    findings, notes = [], []
    try:
        crit = criteria(sdlc_dir, goal)
    except (OSError, ValueError) as exc:
        crit = []
        # The class, never the text: an OSError names an absolute path, and this line can be posted
        # to a public PR.
        findings.append("the acceptance record is missing or invalid (%s): nothing to map; record it "
                        "with `acceptance.py record .sdlc <goal>`" % type(exc).__name__)
    ids = [cid for cid, _ in crit]
    for cid in sorted({c for c in ids if ids.count(c) > 1}):
        findings.append("duplicate criterion id %s in the acceptance record" % cid)
    mapped, conflicts = parse_map(text)
    for cid in sorted(conflicts):
        findings.append("conflicting TEST-MAP lines for %s" % cid)
    for cid in dict.fromkeys(ids):
        value = mapped.get(cid)
        untestable = _UNTESTABLE.match(value or "")
        if cid in conflicts:
            continue
        if value is None:
            findings.append("%s has no TEST-MAP line" % cid)
        elif _NONE.match(value):
            findings.append("%s is mapped NONE: no test proves it" % cid)
        elif untestable:
            if untestable.group(1).strip():
                notes.append("%s untestable: %s" % (cid, untestable.group(1).strip()))
            else:
                findings.append("%s is UNTESTABLE with no reason" % cid)
        else:
            tests = [t.strip("`") for t in re.split(r"[,\s]+", value) if t.strip("`")]
            if not any(_exists(root, t) for t in tests):
                findings.append("%s maps only to tests that do not exist: %s" % (cid, ", ".join(tests)))
    unknown = sorted(set(mapped) - set(ids))
    if unknown:
        notes.append("ignored TEST-MAP lines for ids the record does not have: %s" % ", ".join(unknown))

    state, ev = evidence(sdlc_dir, goal)
    revert = ev.get("diff_revert") if state == "present" else None
    not_judged = None
    if state != "present":
        not_judged = "%s: %s" % (state, ev)
    elif not isinstance(revert, dict) or revert.get("verdict") in (None, "absent"):
        not_judged = "diff-revert measured nothing: %s" % ((revert or {}).get("reason") or "no verdict")
    elif not isinstance(revert.get("changed_survivors"), list):
        not_judged = "the evidence predates changed_survivors; re-run `loop.py verify`"
    else:
        accepted = {t: why.strip() for t, why in _OK_LINE.findall(text or "") if why.strip()}
        for test in revert["changed_survivors"]:
            if test in accepted:
                notes.append("%s accepted by the reviewer: %s" % (test, accepted[test]))
            else:
                findings.append("%s passes with the goal's production code reverted, and the goal "
                                "changed it: it does not test the change" % test)

    lines = ["BLOCKING: " + f for f in findings] + ["note: " + n for n in notes]
    if findings:
        outcome, last = "block", "TEST-REVIEW: block"
    elif not_judged:
        outcome, last = "partial", "TEST-REVIEW: partial - survivor clause not judged (%s)" % not_judged
    else:
        outcome, last = "pass", "TEST-REVIEW: pass"
    return outcome, lines + [last]


def gate(sdlc_dir):
    """-> (on, unreadable_reason). Off unless `review.test_review` is exactly true."""
    try:
        cfg = json.loads((pathlib.Path(sdlc_dir) / "config.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False, None
    except (OSError, ValueError):
        return False, "config.json is unreadable"
    if not isinstance(cfg, dict):
        return False, "config.json is not an object"
    review = cfg.get("review")
    return isinstance(review, dict) and review.get("test_review") is True, None


def judge(sdlc_dir, goal, text):
    """-> (refusal, partial): `refusal` is None or why an approving review verdict may not stand;
    `partial` is the `TEST-REVIEW: partial ...` line when the survivor clause was not judged, for the
    caller to record beside the verdict. Both None while `review.test_review` is off. `text` is the
    reviewer's full output; None means the caller had none to give."""
    on, unreadable = gate(sdlc_dir)
    if unreadable:
        return ("test review: %s, so whether review.test_review is on cannot be told; no approval is "
                "recorded until it parses" % unreadable), None
    if not on:
        return None, None
    if text is None:
        return ("test review (review.test_review is on): no reviewer output to check; pass "
                "--review-output <file holding the reviewer's full output>"), None
    outcome, lines = check(sdlc_dir, goal, text)
    if outcome == "block":
        return "test review blocks this approval (review.test_review):\n" + "\n".join(lines), None
    return None, (lines[-1] if outcome == "partial" else None)


def refusal(sdlc_dir, goal, text):
    """`judge`'s refusal alone."""
    return judge(sdlc_dir, goal, text)[0]


def _evidence_lines(sdlc_dir, goal):
    state, ev = evidence(sdlc_dir, goal)
    if state != "present":
        return ["- %s: %s (%s)" % (label, state, ev) for label in _LABELS]
    out = []
    first, flake, revert = (ev.get(k) for k in ("test_first", "flake", "diff_revert"))
    if isinstance(first, dict):
        out.append("- Red-green proof: present - " + (
            "planned tests green (%d nodes)" % len(first.get("nodes") or {}) if first.get("passed")
            else "NOT proven: %s" % (first.get("error") or "no observed green")))
    else:
        out.append("- Red-green proof: absent (this verify did not record it)")
    if isinstance(flake, dict):
        out.append("- Flake check: present - verdict %s%s" % (
            flake.get("verdict"), "; " + flake["disagreement"] if flake.get("disagreement") else ""))
    else:
        out.append("- Flake check: absent (this verify did not record it)")
    if isinstance(revert, dict):
        changed = revert.get("changed_survivors")
        out.append("- Diff-revert: present - verdict %s; kills: %s; survivors: %s; survivors on changed "
                   "lines: %s" % (revert.get("verdict"), ", ".join(revert.get("kills") or []) or "none",
                                  ", ".join(revert.get("survivors") or []) or "none",
                                  ", ".join(changed) or "none" if isinstance(changed, list)
                                  else "not recorded"))
    else:
        out.append("- Diff-revert: absent (this verify did not record it)")
    return out


def _goal_diff(sdlc_dir, goal):
    """The goal branch's diff against its own base (the text `work.py` scans at merge); "" when unreadable."""
    import subprocess
    rec = _load("work")._record(sdlc_dir, goal) or {}
    done = subprocess.run(["git", "diff", "%s...HEAD" % (rec.get("base") or "HEAD")], cwd=_root(sdlc_dir, goal),
                          capture_output=True, text=True, timeout=60)
    return done.stdout if done.returncode == 0 else ""


def monitor_lines(sdlc_dir, goal):
    """#1999: brief lines naming monitor files this goal deleted or removed lines from; [] when none or on any
    fault (fail-open: the brief never breaks on this advisory)."""
    try:
        found = _load("tamper_scan").monitor_changes(_goal_diff(sdlc_dir, goal))
    except Exception:                                 # noqa: BLE001 - advisory
        return []
    if not found:
        return []
    return ["- Monitor files touched (advisory; state a reason for each removal): " + "; ".join(
        "%s (%s)" % (f["path"], "deleted" if f["deleted"] else "-%d lines" % f["removed"]) for f in found)]


def facts(sdlc_dir, goal):
    """The review brief's `## Test-trust facts` section (code-review and pr-review)."""
    lines = ["## Test-trust facts"]
    try:
        crit = criteria(sdlc_dir, goal)
        lines.append("- Acceptance criteria: present")
        lines += ["  - %s: %s" % c for c in crit]
    except FileNotFoundError:
        lines.append("- Acceptance criteria: absent (no record)")
    except (OSError, ValueError) as exc:
        lines.append("- Acceptance criteria: unreachable (%s)" % type(exc).__name__)
    lines += _evidence_lines(sdlc_dir, goal)
    lines += monitor_lines(sdlc_dir, goal)
    if gate(sdlc_dir)[0]:
        lines.append(
            "Test review is BLOCKING here (`review.test_review`). Begin your output with one line per "
            "criterion: `TEST-MAP AC-<n>: <pytest id>[, <pytest id>...]`, `TEST-MAP AC-<n>: NONE`, or "
            "`TEST-MAP AC-<n>: UNTESTABLE: <why>`; ids are relative to the repo root. Name only a test "
            "that would fail if the criterion were not met. A criterion with no such test is a blocking "
            "finding, and so is each survivor on changed lines unless you write `SURVIVOR-OK <test id>: "
            "<why it need not fail without the change>`.")
    return "\n".join(lines)


USAGE = "usage: review_tests.py check <sdlc_dir> <goal> --review <file|->"


def main(argv):
    if argv in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    if len(argv) != 5 or argv[0] != "check" or argv[3] != "--review":
        print(USAGE, file=sys.stderr)
        return 2
    sdlc_dir, goal, source = argv[1], argv[2], argv[4]
    try:
        text = sys.stdin.read() if source == "-" else pathlib.Path(source).read_text(encoding="utf-8")
    except OSError as exc:
        print("review_tests: cannot read %s (%s)" % (source, exc), file=sys.stderr)
        return 2
    outcome, lines = check(sdlc_dir, goal, text)
    print("\n".join(lines))
    return 1 if outcome == "block" else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
