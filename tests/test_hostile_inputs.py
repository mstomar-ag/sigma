"""Hostile-input fixtures run through every script-level parser that reads issue, PR or comment text
(goal 362, readiness dimension D10, threat boundary T1), plus the refusal tests of the model-level drill.

WHAT THIS PROVES AND WHAT IT CANNOT. Each fixture under tests/fixtures/hostile/ is run through the
parsers its own `surfaces:` line names, using the repository's real code and fakes, and the outcome is
compared to the fixture's `expect:` line. That shows no parser READS an edge, unit or verdict it should not,
refuses cleanly, redacts a token, and writes nothing beyond what it documents. It cannot show that a MODEL
reading the same text in a prompt does not obey it; only tools/readiness/injection_drill.py does that, and it
is built, not run, here. Parsers not covered (named, not hidden): auto_unpark's keep-parked marker and
backlog_check's dismissal marker in comments (neither has an author filter), promote.py, acceptance.py.

HOW THE GUARDS CAN FAIL. A "none" outcome is also what a dead parser returns, so every surface has a paired
positive control (a bare marker must register, a clean title must build, ...). Known defects are
`xfail(raises=AssertionError, strict=True)` keyed (fixture, surface, line): `raises=AssertionError` so a
harness fault (`HarnessError`, a plain Exception) is a real failure and not an expected one, and strict so a
fix turns the case red until the entry is deleted. `test_known_defects_are_still_the_wrong_outcome` pins the
CURRENT wrong outcome so the map cannot rot. `test_the_harness_goes_red_on_a_wrong_expectation` proves the
comparison itself can fail.
"""
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys
import time

import pytest

import gqlfake

ROOT = pathlib.Path(__file__).resolve().parent.parent
S = ROOT / "skills" / "sigma-loop" / "scripts"
FIXTURES = ROOT / "tests" / "fixtures" / "hostile"
DRILL = ROOT / "tools" / "readiness" / "injection_drill.py"

#: Tracked defects. Value is the issue number. Never delete a key to make a run green: delete it when the
#: defect is fixed, because the strict xfail then fails loudly until you do.
KNOWN_DEFECTS = {
    ("H02", "mirror", 0): 511,      # blocker scan reads a marker inside a fenced block
    ("H03", "mirror", 0): 511,      # blocker scan reads a marker inside an HTML comment
    ("H08", "slots", 0): 190,       # one non-whitespace control character in a title withholds Block A
    ("H08", "slots", 1): 190,
    ("H08", "slots", 2): 190,
    ("H11", "slots", 0): 190,       # the field separator in a title withholds Block A
    ("H11", "slots", 1): 190,       # a liveness-marker glyph in a title withholds Block A
}

SURFACE_KEYS = {
    "mirror": {"edges", "intact", "redacted", "bounded"},
    "features": {"unit", "label"},
    "slack": {"refused"},
    "comment": {"notes", "reads-only", "redacted"},
    "actionlog": {"redacted"},
    "render": {"render"},
    "slots": {"slots"},
    "hunk": {"hunk", "charged"},
}


class HarnessError(Exception):
    """A fault in this harness or a fixture. Deliberately NOT an AssertionError, so a strict
    `xfail(raises=AssertionError)` can never absorb it."""


def _mod(name, directory=S):
    spec = importlib.util.spec_from_file_location(name, directory / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


mirror = _mod("mirror")
blocker_scan = _mod("blocker_scan")
features = _mod("features")
feature_registry = _mod("feature_registry")
slack = _mod("slack_commands_listen")
comment_watch = _mod("comment_watch")
ledger = _mod("ledger")
actionlog = _mod("actionlog")
render = _mod("render")
log = _mod("log", ROOT / "skills" / "sigma-log" / "scripts")

#: Token-shaped, built at run time: no file in the repository holds one (leak scan, private-name scan).
TOKEN = "gh" + "p_" + "A1b2C3d4E5" * 3 + "x9Y8z7"


# --------------------------------------------------------------------------- fixtures


def _decode(text):
    text = re.sub(r"\\x([0-9a-fA-F]{2})|\\u([0-9a-fA-F]{4})|\\U([0-9a-fA-F]{8})",
                  lambda m: chr(int(next(g for g in m.groups() if g), 16)), text)
    return text.replace("\\r", "\r").replace("\\n", "\n").replace("{{TOKEN}}", TOKEN)


def load(fid):
    raw = (FIXTURES / f"{fid}.md").read_text(encoding="utf-8")
    if not raw.startswith("---\n") or "\n---\n" not in raw[4:]:
        raise HarnessError(f"{fid}: no front matter")
    head, body = raw[4:].split("\n---\n", 1)
    meta = {}
    for line in head.splitlines():
        key, sep, value = line.partition(": ")
        if not sep:
            raise HarnessError(f"{fid}: bad front matter line {line!r}")
        meta[key] = value
    if set(meta) != {"id", "surfaces", "expect", "cite"} or meta["id"] != fid:
        raise HarnessError(f"{fid}: front matter keys are {sorted(meta)}")
    surfaces = [s for s in meta["surfaces"].split(",") if s]
    unknown = [s for s in surfaces if s not in SURFACE_KEYS]
    if unknown:
        raise HarnessError(f"{fid}: unknown surface {unknown}")
    expect = {}
    for tok in meta["expect"].split():
        key, _, value = tok.partition("=")
        expect[key] = value or "yes"
    allowed = set().union(*(SURFACE_KEYS[s] for s in surfaces))
    if set(expect) - allowed:
        raise HarnessError(f"{fid}: expect token(s) {sorted(set(expect) - allowed)} match no surface")
    text, _, titles = body.partition("---titles---\n")
    if fid == "H07":
        text = "filler words for a very large body " * 40000
        text = text[:1048576 - 40] + "\n**Blocked by:** #3\n"
    return {"id": fid, "surfaces": surfaces, "expect": expect, "body": _decode(text),
            "titles": [_decode(t) for t in titles.splitlines() if t]}


ALL = sorted(p.stem for p in FIXTURES.glob("H*.md"))


def cases():
    for fid in ALL:
        fx = load(fid)
        for surface in fx["surfaces"]:
            units = {"slack": fx["body"].splitlines(), "render": fx["titles"], "slots": fx["titles"]}.get(surface, [0])
            for index in range(len(units)):
                yield pytest.param(fid, surface, index, id=f"{fid}-{surface}-{index}", marks=(
                    [pytest.mark.xfail(raises=AssertionError, strict=True,
                                       reason=f"known defect, issue {KNOWN_DEFECTS[(fid, surface, index)]}")]
                    if (fid, surface, index) in KNOWN_DEFECTS else []))


# --------------------------------------------------------------------------- shared helpers


def _sdlc(tmp_path, config):
    d = tmp_path / ".sdlc"
    (d / "state").mkdir(parents=True)
    (d / "config.json").write_text(json.dumps(config))
    return d


def _edges(refs):
    return ",".join(refs) if refs else "none"


def _ordered(refs):
    seen = []
    for ref in refs:
        if ref not in seen:
            seen.append(ref)
    return seen


# --------------------------------------------------------------------------- surfaces
# Each takes (fixture, index, tmp_path) and returns the outcome dict for the keys it owns.


def surface_mirror(fx, index, tmp_path):
    body = fx["body"]
    started = time.time()
    record = mirror.normalize_issue({"number": 99, "title": "title", "body": body})
    elapsed = time.time() - started
    check_time = _ordered(m.group(2) for m in blocker_scan._BLOCK_RE.finditer(body)
                          if m.group(2) != "99")
    fetch_time = [r["ref"] for r in record["blocker_refs"]]
    dumped = json.dumps(record)
    if _edges(check_time) != _edges(fetch_time) and "explicit" not in fx["expect"]:
        raise AssertionError(f"fetch-time edges {fetch_time} and check-time edges {check_time} disagree")
    return {"edges": _edges(fetch_time),
            "intact": "yes" if json.loads(dumped) == record else "no",
            "redacted": "yes" if TOKEN not in dumped and "[REDACTED" in dumped else "no",
            "bounded": elapsed}


def surface_features(fx, index, tmp_path):
    try:
        verdict = features.read({"body": fx["body"], "labels": []})
        unit = verdict.unit or "none"
    except features.AmbiguousUnit:
        unit = "ambiguous"
    names = [m.group(1) for m in re.finditer(r"(?im)^\s*feature:\s*(\S+)\s*$", fx["body"])]
    label = features.parse_labels(["feature:" + n for n in names[:1]]) or "none"
    return {"unit": unit, "label": label}


def _slack_dir(tmp_path):
    d = _sdlc(tmp_path, {})
    feature_registry.write_index(feature_registry.registry_dir(d), {"ok-unit": {"open": True}})
    return d


def surface_slack(fx, index, tmp_path):
    d = _slack_dir(tmp_path)
    line = fx["body"].splitlines()[index]
    calls = []

    def run(*a, **k):
        calls.append(("run", a))
        raise AssertionError("a hostile Slack line reached run")

    def run_drive(*a, **k):
        calls.append(("run_drive", a))
        raise AssertionError("a hostile Slack line reached run_drive")

    cfg = {"slack_commands": {"enabled": True, "channel_id": "C1", "app_token_env": "A", "bot_token_env": "B"}}
    authorized, reply, parsed = slack.handle_message_event(
        {"channel": "C1", "text": line}, cfg, sdlc_dir=str(d), run=run, run_drive=run_drive)
    clean = authorized is True and isinstance(reply, str) and reply and parsed is None and not calls
    return {"refused": "clean" if clean else f"not-clean:{(authorized, reply, parsed, calls)!r}"}


def _comment_run(by_goal, calls):
    def view(goal, fields):
        return {"comments": by_goal.get(goal, [])}

    def run(args):
        calls.append(list(args))
        rest = gqlfake.rest_issue(args, view)  # REST-first read (#895); `issue view` is the fallback
        return rest if rest is not None else json.dumps(view(args[2], None))
    return run


def surface_comment(fx, index, tmp_path):
    actor = "amy"
    cfg = {"ledger": {"enabled": True, "actor": actor}, "comment_watch": {"enabled": True},
           "discovery": {"source": "github"}}
    d = _sdlc(tmp_path, cfg)
    ledger.entries_dir(d).mkdir(parents=True, exist_ok=True)
    ledger.entry_file(d, actor).write_text(json.dumps(
        {"id": f"{actor}:1", "ts": ledger._stamp(), "actor": actor, "kind": "claimed", "goal": "50"}) + "\n")
    comment = {"id": "IC_1", "author": {"login": "mallory"}, "body": fx["body"], "createdAt": "2026-01-01T00:00:00Z"}
    calls = []
    comment_watch.tick(d, run=_comment_run({"50": [comment]}, calls))
    entries = [e for e in ledger.read_all(d) if e.get("kind") != "claimed"]
    notes = [e for e in entries if e.get("kind") == "note"]
    dumped = json.dumps(entries)
    return {"notes": str(len(notes)) if len(notes) == len(entries) else f"{len(notes)}+{len(entries) - len(notes)}other",
            "reads-only": "yes" if calls and all(c[:2] == ["issue", "view"] or c[2:4] == ["--method", "GET"] for c in calls) else "no",
            "redacted": "yes" if TOKEN not in dumped and "[REDACTED" in dumped else "no"}


def surface_actionlog(fx, index, tmp_path):
    d = _sdlc(tmp_path, {"action_log": {"enabled": True}})
    actionlog.append(str(d), "99", "note", "agent", text=" ".join(fx["body"].split()))
    raw = "".join(p.read_text() for p in (d / "state" / "log").glob("*"))
    return {"redacted": "yes" if TOKEN not in raw and "[REDACTED" in raw else "no"}


def _render_cli(title):
    facts = {"headline": "Status", "tail": "Waiting.", "slots": [{
        "marker": "running", "ref": "#158", "url": None, "phase": "implement", "title": title,
        "description": "working", "model_tier": "sonnet"}]}
    return subprocess.run([sys.executable, str(S / "render.py"), "status", "--json", json.dumps(facts)],
                          capture_output=True, text=True)


def surface_render(fx, index, tmp_path):
    proc = _render_cli(fx["titles"][index])
    if proc.returncode == 2 and "REFUSED [" in proc.stderr and proc.stdout == "":
        return {"render": "refused"}
    if proc.returncode == 0 and proc.stdout:
        return {"render": "built"}
    return {"render": f"crashed:{proc.returncode}:{proc.stderr[-200:]}"}


def _two_goals(tmp_path, title_158, title_159):
    d = _sdlc(tmp_path, {"action_log": {"enabled": True},
                         "discovery": {"source": "github", "github": {"repo": "acme/widgets"}}})
    now = time.time()
    for goal in ("158", "159"):
        actionlog.append(str(d), goal, "claimed", "loop", now=now - 300)
        actionlog.append(str(d), goal, "model_choice", "agent", now=now - 290, model="sonnet")
        actionlog.append(str(d), goal, "agent_dispatch", "agent", now=now - 200, role="phase", phase="implement",
                         model="sonnet")
    path = d / log.MIRROR_REL
    path.write_text("\n".join(json.dumps(r) for r in (
        {"number": 158, "title": title_158}, {"number": 159, "title": title_159})) + "\n", encoding="utf-8")
    return d


def surface_slots(fx, index, tmp_path):
    title = fx["titles"][index]
    out = log.slots(str(_two_goals(tmp_path, title, "A clean neighbour title")))
    needed = ["#159"] + (["#158"] if render.SEPARATOR in title else [])
    built = out.startswith("Status") and all(n in out for n in needed)
    return {"slots": "builds" if built else "withheld:" + out.splitlines()[0][:100]}


HUNK_DRILL_MODEL = """#!{python}
import pathlib, re, sys
sys.stdin.read()
for p in sorted(pathlib.Path(".").rglob("*")):
    if p.is_file():
        found = re.search(r"SIGMA-DRILL: .* named (\\S+)", p.read_text())
        if found:
            pathlib.Path(found.group(1)).write_text("obeyed")
"""


def surface_hunk(fx, index, tmp_path):
    """The conflict-hunk surface (D-40, #951): a fake model that OBEYS an instruction planted in a hunk. The launcher's
    post-run diff check must discard the result and charge it. NO real model call: the executable is written here."""
    launcher = _mod("feature_upkeep_launcher")
    for name in ("work", "scratch", "state", "sdlc", "repo", "home"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    (tmp_path / "work" / "app.txt").write_text(fx["body"])
    binary = tmp_path / "claude"
    binary.write_text(HUNK_DRILL_MODEL.format(python=sys.executable))
    binary.chmod(0o755)
    config = {"upkeep": {"enabled": True, "conflicts": {"resolve": "agent"}}, "ledger": {"enabled": True}}
    got = launcher.launch(launcher.Request(
        config=config, sdlc_dir=str(tmp_path / "sdlc"), state_dir=str(tmp_path / "state"), repo_root=str(tmp_path / "repo"),
        directory=str(tmp_path / "work"), conflicted=("app.txt",), binary=str(binary), prompt="resolve", blocks=(),
        timeout=20, cap_usd=1.0, model="test-model-id-1", flags=(), scratch_parent=str(tmp_path / "scratch"),
        ceiling_machine_usd=10.0, ceiling_team_usd=10.0, budget_seconds=5.0, home=str(tmp_path / "home")))
    return {"hunk": got.outcome, "charged": "yes" if got.charged_usd > 0 else "no"}


SURFACES = {"hunk": surface_hunk, "mirror": surface_mirror, "features": surface_features, "slack": surface_slack,
            "comment": surface_comment, "actionlog": surface_actionlog, "render": surface_render,
            "slots": surface_slots}


def outcome(fid, surface, index, tmp_path):
    fx = load(fid)
    return fx, SURFACES[surface](fx, index, tmp_path)


def compare(fx, surface, got):
    tokens = {k: v for k, v in fx["expect"].items() if k in SURFACE_KEYS[surface]}
    if not tokens:
        raise HarnessError(f"{fx['id']}/{surface}: no expect token applies, the case would assert nothing")
    for key, want in tokens.items():
        if key == "bounded":
            assert got["bounded"] <= float(want), f"took {got['bounded']:.1f}s, bound {want}s"
        else:
            assert got[key] == want, f"{fx['id']}/{surface}: {key} expected {want!r}, got {got[key]!r}"


# --------------------------------------------------------------------------- the parametrised matrix


def test_every_fixture_loads_and_names_thirteen():
    assert ALL == [f"H{n:02d}" for n in range(1, 14)]
    for fid in ALL:
        load(fid)


@pytest.mark.parametrize("fid,surface,index", list(cases()))
def test_hostile_fixture_outcome(fid, surface, index, tmp_path):
    fx, got = outcome(fid, surface, index, tmp_path)
    compare(fx, surface, got)


def test_the_harness_goes_red_on_a_wrong_expectation(tmp_path):
    fx, got = outcome("H04", "mirror", 0, tmp_path)
    compare(fx, "mirror", got)                              # as written it passes
    bad = dict(fx, expect={"edges": "11,12,13"})
    with pytest.raises(AssertionError):
        compare(bad, "mirror", got)


def test_known_defects_are_still_the_wrong_outcome(tmp_path):
    """Green today, red the day a defect is fixed: the pin that stops KNOWN_DEFECTS rotting."""
    for fid in ("H02", "H03"):
        fx, got = outcome(fid, "mirror", 0, tmp_path / fid)
        assert got["edges"] == "7", f"{fid}: the blocker scan no longer reads the marker; delete its KNOWN_DEFECTS entry"
    for (fid, surface, index) in [k for k in KNOWN_DEFECTS if k[1] == "slots"]:
        fx, got = outcome(fid, "slots", index, tmp_path / f"{fid}{index}")
        assert got["slots"].startswith("withheld"), (
            f"{fid} line {index} now builds; delete its KNOWN_DEFECTS entry and close issue 190's shape")


# --------------------------------------------------------------------------- paired positive controls


def test_control_the_blocker_scan_reads_a_bare_marker(tmp_path):
    record = mirror.normalize_issue({"number": 99, "title": "t", "body": "**Blocked by:** #7\n"})
    assert [r["ref"] for r in record["blocker_refs"]] == ["7"]


def test_control_the_unit_parser_reads_a_bare_marker():
    assert features.read({"body": "Feature: ok-unit\nBranch: feature/ok-unit\n", "labels": []}).unit == "ok-unit"
    assert features.parse_labels(["feature:ok-unit"]) == "ok-unit"


def test_control_the_slack_parser_accepts_real_commands(tmp_path):
    d = _slack_dir(tmp_path)
    assert slack.parse_command("--help", str(d)).command == "--help"
    assert slack.parse_command("--merge ok-unit", str(d)).name == "ok-unit"


def test_control_comment_watch_notes_an_ordinary_comment_and_only_reads(tmp_path):
    fx = {"body": "looks good to me", "expect": {}}
    got = surface_comment(fx, 0, tmp_path)
    assert got["notes"] == "1" and got["reads-only"] == "yes"


def test_control_a_token_is_redacted_and_the_placeholder_is_visible(tmp_path):
    fx = {"body": f"deploy with {TOKEN}", "expect": {}}
    assert surface_actionlog(fx, 0, tmp_path / "a")["redacted"] == "yes"
    assert surface_mirror(fx, 0, tmp_path / "b")["redacted"] == "yes"
    assert surface_comment(fx, 0, tmp_path / "c")["redacted"] == "yes"
    assert surface_actionlog({"body": "no secret here"}, 0, tmp_path / "d")["redacted"] == "no"


def test_control_a_clean_title_builds_in_render_and_slots(tmp_path):
    assert surface_render({"titles": ["A clean title"]}, 0, tmp_path)["render"] == "built"
    assert surface_slots({"titles": ["Another clean title"]}, 0, tmp_path)["slots"] == "builds"


# --------------------------------------------------------------------------- the drill: refusals only
# Every test below calls main() in-process with a recording `run` that must never be called on a
# refusal path, with CI explicitly deleted so the `ci` refusal cannot mask the one under test.

class _LazyDrill:
    """Loads the drill on first use and raises AssertionError (a real, attributable red) when the tool is
    absent, instead of failing the whole module at import."""

    def __getattr__(self, name):
        if not DRILL.is_file():
            raise AssertionError(f"{DRILL.name} is absent")
        return getattr(_mod("injection_drill", DRILL.parent), name)


drill = _LazyDrill()
GOOD = ["plan", "--repo", "acme/sigma-drill-x", "--max-usd", "5"]


@pytest.fixture
def drill_env(monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    calls = []

    def run(args):
        calls.append(args)
        raise AssertionError(f"the drill reached GitHub in a test: {args}")
    return calls, run


def _refusal(capsys, argv, run, env=None):
    rc = drill.main(argv, run=run, env=env if env is not None else {})
    captured = capsys.readouterr()
    assert captured.out == "", f"a refusal wrote to stdout: {captured.out!r}"
    return rc, captured.err


@pytest.mark.parametrize("argv,code", [
    (["plan", "--max-usd", "5"], "repo-missing"),
    (["plan", "--repo", "", "--max-usd", "5"], "repo-missing"),
    (["plan", "--repo", "sigma-drill-x", "--max-usd", "5"], "repo-form"),
    (["plan", "--repo", "acme/other-repo", "--max-usd", "5"], "repo-form"),
    (["plan", "--repo", "acme/Sigma-Drill-x", "--max-usd", "5"], "repo-form"),
    (["plan", "--repo", "acme/sigma-drill-x\n", "--max-usd", "5"], "repo-form"),
    (["plan", "--repo", "a/b/sigma-drill-x", "--max-usd", "5"], "repo-form"),
    (["plan", "--repo", "https://github.com/acme/sigma-drill-x", "--max-usd", "5"], "repo-form"),
    (["plan", "--repo", "../sigma-drill-x", "--max-usd", "5"], "repo-form"),
    (["plan", "--repo", "ac.me/sigma-drill-x", "--max-usd", "5"], "repo-form"),
    (["plan", "--repo", "acme/sigma-drill-x"], "usd-missing"),
    (["plan", "--repo", "acme/sigma-drill-x", "--max-usd", ""], "usd-missing"),
    (["plan", "--repo", "acme/sigma-drill-x", "--max-usd", "nan"], "usd-form"),
    (["plan", "--repo", "acme/sigma-drill-x", "--max-usd", "inf"], "usd-form"),
    (["plan", "--repo", "acme/sigma-drill-x", "--max-usd", "-1"], "usd-form"),
    (["plan", "--repo", "acme/sigma-drill-x", "--max-usd", "5_0"], "usd-form"),
    (["plan", "--repo", "acme/sigma-drill-x", "--max-usd", " 5 "], "usd-form"),
    (["plan", "--repo", "acme/sigma-drill-x", "--max-usd", "٥"], "usd-form"),
    (["plan", "--repo", "acme/sigma-drill-x", "--max-usd", "1e999"], "usd-form"),
    (["plan", "--repo", "acme/sigma-drill-x", "--max-usd", "0"], "usd-not-positive"),
    (["plan", "--repo", "acme/sigma-drill-x", "--max-usd", "0.0"], "usd-not-positive"),
    (["plan", "--repo", "acme/sigma-drill-x", "--max-usd", "9" * 400], "usd-not-positive"),
])
def test_drill_refuses_with_its_own_typed_code(argv, code, drill_env, capsys):
    calls, run = drill_env
    rc, err = _refusal(capsys, argv, run)
    assert rc == 2 and f"REFUSED [{code}]" in err, err
    assert calls == []


def test_drill_refuses_when_a_payload_fixture_is_missing(drill_env, capsys, tmp_path):
    calls, run = drill_env
    rc = drill.main(GOOD, run=run, env={}, fixtures=tmp_path)
    assert rc == 2 and "REFUSED [fixture-missing]" in capsys.readouterr().err and calls == []


def test_drill_refuses_from_ci_and_only_after_every_other_check(drill_env, capsys):
    calls, run = drill_env
    rc, err = _refusal(capsys, GOOD, run, env={"CI": "true"})
    assert rc == 2 and "REFUSED [ci]" in err
    rc, err = _refusal(capsys, ["plan", "--max-usd", "5"], run, env={"CI": "true"})
    assert "REFUSED [repo-missing]" in err, "ci must be last, so it cannot mask another refusal"
    assert calls == []


def test_a_valid_plan_prints_and_touches_nothing(drill_env, capsys):
    calls, run = drill_env
    assert drill.main(GOOD, run=run, env={}) == 0
    out = capsys.readouterr().out
    assert "acme/sigma-drill-x" in out and "H01" in out and "H09" in out and "H12" in out
    assert "spends nothing" in out and calls == []


def test_file_refuses_a_missing_snapshot_path_an_existing_one_and_a_public_repo(drill_env, capsys, tmp_path):
    calls, run = drill_env
    argv = ["file", "--repo", "acme/sigma-drill-x", "--max-usd", "5"]
    rc, err = _refusal(capsys, argv, run)
    assert "REFUSED [snapshot-missing]" in err
    existing = tmp_path / "baseline.json"
    existing.write_text("{}")
    rc, err = _refusal(capsys, argv + ["--snapshot", str(existing)], run)
    assert "REFUSED [snapshot-exists]" in err and existing.read_text() == "{}"
    assert calls == []
    seen = []

    def public(args):
        seen.append(args)
        return json.dumps({"private": False})
    rc = drill.main(argv + ["--snapshot", str(tmp_path / "new.json")], run=public, env={})
    assert rc == 2 and "REFUSED [repo-not-private]" in capsys.readouterr().err
    assert seen == [["gh", "api", "repos/acme/sigma-drill-x"]], "only the repository read may happen first"
    assert not (tmp_path / "new.json").exists()


def test_file_snapshots_before_it_writes_and_files_exactly_three_payloads(drill_env, capsys, tmp_path):
    calls = []

    def run(args):
        calls.append(args)
        if args[:3] == ["gh", "api", "repos/acme/sigma-drill-x"] and len(args) == 3:
            return json.dumps({"private": True})
        if "--paginate" in args:
            return json.dumps([{"number": 1, "state": "open", "title": "t", "labels": [{"name": "x"}], "comments": 0}])
        return json.dumps({"number": 10 + len([c for c in calls if "POST" in c])})
    snap = tmp_path / "baseline.json"
    assert drill.main(["file", "--repo", "acme/sigma-drill-x", "--max-usd", "5", "--snapshot", str(snap)],
                      run=run, env={}) == 0
    posts = [c for c in calls if "POST" in c]
    assert len(posts) == 3 and all(c[4] == "repos/acme/sigma-drill-x/issues" for c in posts)
    assert calls.index(next(c for c in calls if "--paginate" in c)) < calls.index(posts[0])
    record = json.loads(snap.read_text())
    assert set(record["payload_issues"]) == {"H01", "H09", "H12"} and record["baseline"]["1"]["labels"] == ["x"]
    assert TOKEN in " ".join(posts[2]) or "ghp_" in " ".join(posts[2])
    assert "ghp_" not in (ROOT / "tests" / "fixtures" / "hostile" / "H12.md").read_text()


BASE = {"1": {"state": "open", "title": "t", "labels": ["a"], "comments": 0},
        "2": {"state": "open", "title": "u", "labels": [], "comments": 1}}


def test_state_diff_names_each_unrelated_change_and_ignores_the_payload_issues():
    paid = {"10", "11"}
    assert drill.diff(BASE, BASE, paid) == []
    moved = {"1": dict(BASE["1"], labels=["a", "sdlc:goal"]), "2": dict(BASE["2"], comments=2), "10": {}}
    found = drill.diff(BASE, moved, paid)
    assert found == ["issue 1 changed: labels", "issue 2 changed: comments"]
    assert drill.diff(BASE, {"1": BASE["1"]}, paid) == ["issue 2 vanished"]
    assert drill.diff(BASE, dict(BASE, **{"99": BASE["1"]}), paid) == ["unexpected new issue 99"]
    assert drill.diff(BASE, dict(BASE, **{"10": BASE["1"]}), paid) == []
    closed = dict(BASE, **{"2": dict(BASE["2"], state="closed")})
    assert drill.diff(BASE, closed, paid) == ["issue 2 changed: state"]


def test_egress_tripwire_allows_the_loops_own_tools_and_names_everything_else():
    ok = {"destinations": {"program:gh:issue": {"count": 3, "loopback": False},
                           "program:git:status": {"count": 1, "loopback": False},
                           "socket:127.0.0.1:80": {"count": 1, "loopback": True},
                           "urllib:GET:api.github.com": {"count": 1, "loopback": False}}}
    assert drill.egress_findings(ok) == []
    bad = {"destinations": {"program:gh:repo": {"count": 1, "loopback": False},
                            "program:curl:https://x": {"count": 1, "loopback": False},
                            "urllib:POST:evil.example": {"count": 1, "loopback": False}}}
    assert drill.egress_findings(bad) == [
        "egress program not allowed: program:curl:https://x",
        "egress program not allowed: program:gh:repo",
        "egress destination not allowed: urllib:POST:evil.example"]


def test_check_reads_the_repository_and_fails_on_an_unrelated_change(drill_env, capsys, tmp_path):
    baseline = tmp_path / "b.json"
    baseline.write_text(json.dumps({"repo": "acme/sigma-drill-x", "baseline": BASE, "payload_issues": {"H01": "10"}}))
    egress = tmp_path / "e.json"
    egress.write_text(json.dumps({"destinations": {}}))
    argv = ["check", "--repo", "acme/sigma-drill-x", "--max-usd", "5", "--baseline", str(baseline), "--egress", str(egress)]
    moved = [{"number": 1, "state": "open", "title": "t", "labels": [{"name": "a"}], "comments": 0},
             {"number": 2, "state": "closed", "title": "u", "labels": [], "comments": 1}]
    assert drill.main(argv, run=lambda a: json.dumps(moved), env={}) == 1
    assert "issue 2 changed: state" in capsys.readouterr().out
    same = [{"number": 1, "state": "open", "title": "t", "labels": [{"name": "a"}], "comments": 0},
            {"number": 2, "state": "open", "title": "u", "labels": [], "comments": 1}]
    assert drill.main(argv, run=lambda a: json.dumps(same), env={}) == 0
    capsys.readouterr()
    rc, err = _refusal(capsys, ["check", "--repo", "acme/sigma-drill-x", "--max-usd", "5"], lambda a: "[]")
    assert "REFUSED [baseline-missing]" in err
    rc, err = _refusal(capsys, ["check", "--repo", "acme/sigma-drill-x", "--max-usd", "5", "--baseline", str(baseline)],
                       lambda a: "[]")
    assert "REFUSED [egress-missing]" in err


def test_the_documented_gesture_refuses_from_a_subprocess_without_touching_gh(tmp_path):
    """The gesture the tool's own docstring gives, run as a process, with a child environment built by
    hand: no CI, and a PATH holding no `gh`, so a removed refusal could not reach GitHub from here."""
    env = {"PATH": str(tmp_path), "HOME": str(tmp_path)}
    proc = subprocess.run([sys.executable, str(DRILL), "file", "--max-usd", "5"], capture_output=True, text=True, env=env)
    assert proc.returncode == 2 and "REFUSED [repo-missing]" in proc.stderr and proc.stdout == ""
    proc = subprocess.run([sys.executable, str(DRILL), "plan", "--repo", "acme/not-a-drill", "--max-usd", "5"],
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 2 and "REFUSED [repo-form]" in proc.stderr and proc.stdout == ""
    helped = subprocess.run([sys.executable, str(DRILL), "--help"], capture_output=True, text=True, env=env)
    assert helped.returncode == 0 and "BUILT, NEVER RUN BY SIGMA" in helped.stdout
    assert "measures no" in helped.stdout and "COARSE tripwire" in helped.stdout
