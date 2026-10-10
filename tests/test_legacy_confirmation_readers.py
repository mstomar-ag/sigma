"""Decision-rubric slice 18: the confirmation label is LEGACY-ONLY in every reader.

Nothing writes the label any more (a filed follow-up is already a goal), so each reader either still
tolerates a leftover (promote lists it, triage strips it, status counts it, doctor flags it) or has
dropped the row that treated it as a live state (blockers). The scan at the end keeps new label
mentions out of unowned script files.
"""
import importlib.util
import json
import pathlib
import re
import tempfile

import gqlfake  # noqa: F401  (puts tests/ helpers on the path, as every sibling test does)
import test_doctor as td
import test_promote as tp
import test_status as ts

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOOP = ROOT / "skills" / "sigma-loop" / "scripts"
LEGACY = "sdlc:needs-confirmation"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def test_legacy_labelled_issue_is_still_listed():
    p = tp._mod("promote")
    run = tp._runner(by_label={LEGACY: json.dumps([tp._issue(5, LEGACY)])})
    result = p.survey(".sdlc", tp._config(), run=run)
    assert [r["number"] for r in result["buckets"]["awaiting"]] == ["5"]


def test_blocker_policy_has_no_confirmation_row():
    b = _load(LOOP / "blockers.py", "blockers_s18")
    st = lambda *l: {"labels": set(l), "assignees": [], "closed": False}   # noqa: E731
    cls = lambda s: b.classify(s, "sdlc:goal", LEGACY, "sdlc:parked", "sdlc:followup", "me")  # noqa: E731
    # A leftover is read through its other labels: never PROMOTED or NEEDS_HUMAN any more.
    assert cls(st(LEGACY, "sdlc:followup")) not in (b.PROMOTED, b.NEEDS_HUMAN)
    assert cls(st(LEGACY)) == b.UNMANAGED
    assert cls(st(LEGACY, "sdlc:followup")) == b.ROUTED          # provenance still grants membership
    assert cls(st(LEGACY, "sdlc:parked")) == b.CHAINED          # a park still wins


def test_status_counts_legacy_only():
    with tempfile.TemporaryDirectory() as d:
        base = ts._gh_sdlc(d, assignee="@me")

        def run(args):
            if args[:3] == ["gh", "api", "user"]:
                return "me-login"
            table = {(LEGACY,): 2, (LEGACY, "sdlc:goal"): 1}
            return json.dumps([{"number": i} for i in range(table.get(ts._rest_labels(args), 0))])
        out = ts._status().summary(str(base), run=run)
        assert out["proposed"] == 1                 # leftovers that were never promoted
    with tempfile.TemporaryDirectory() as d:
        base = ts._gh_sdlc(d, assignee="@me")
        clean = ts._status().summary(str(base), run=lambda a: "me-login" if a[:3] == ["gh", "api", "user"] else "[]")
        assert clean["proposed"] == 0


def test_doctor_flags_a_leftover_label(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)
    monkeypatch.delenv("SIGMA_GH_GRAPHQL", raising=False)
    d = td._doc()
    seen = []

    def rest(args):
        p = gqlfake.rest_list_params(args[1:])
        seen.append(p["labels"])
        return json.dumps([{"number": 9, "labels": [{"name": LEGACY}]}]) if p["labels"] == LEGACY else "[]"
    assert d._legacy_confirmation_scan(td._CFG, {}, td._scan_run(rest)) == [9]
    assert LEGACY in seen
    assert d._legacy_confirmation_scan(td._CFG, {}, td._scan_run("[]")) == []
    # the alias is honoured, read-only
    cfg = {"ledger": {"handoff": {"proposed_label": "sdlc:custom"}}}
    assert d._legacy_confirmation_scan(td._CFG, cfg, td._scan_run(
        lambda a: json.dumps([{"number": 4}]) if gqlfake.rest_list_params(a[1:])["labels"] == "sdlc:custom"
        else "[]")) == [4]


def test_board_view_filter_updated():
    sources = _load(LOOP / "sources.py", "sources_s18")
    spec_mod = _load(ROOT / "skills" / "sigma-init" / "scripts" / "board_spec.py", "board_spec_s18")
    discovery = _load(LOOP / "discovery.py", "discovery_s18")
    cfg = {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}}
    src = sources.GitHubSource(cfg, run=lambda _a: "")
    spec = spec_mod.build(src, discovery.PRIORITIES)
    view = next(v for v in spec["views"] if v["name"] == spec_mod.NEEDS_HUMAN)
    assert src.parked_label in view["filter"] and src.goal_blocked_label in view["filter"]
    assert LEGACY not in view["filter"]


def test_legacy_alias_is_read_only():
    tmpl = json.loads((ROOT / "skills" / "sigma-init" / "templates" / "config.json.tmpl")
                      .read_text(encoding="utf-8"))
    note = tmpl["ledger"]["_handoff_proposed_label"]
    assert "LEGACY" in note and "read-only" in note
    # No reader slice 18 owns passes the alias to an add= list. `sources.mark_needs_confirmation` is
    # the gate-hold writer owned by slice 16 (its conversion to a park lands there), so it is skipped.
    for f in sorted(ROOT.glob("skills/**/scripts/*.py")):
        if f.name == "sources.py":
            continue
        src = f.read_text(encoding="utf-8")
        assert not re.search(r"add\s*=\s*\[[^\]]*proposed_label", src), f.name


def test_picked_legacy_issue_is_cleaned():
    triage = _load(LOOP / "triage.py", "triage_s18")
    state = {"42": {"labels": {LEGACY}, "assignees": set(), "body": ""}}
    plan = {"picked": [{"issue": 42, "priority": None, "model": None, "wave": 1}], "deferred": [],
            "edges": []}
    acts = triage._picked_actions(plan, state, None, "sdlc:goal", "sdlc:parked",
                                  proposed_label=LEGACY)
    assert acts[0]["add"] == ["sdlc:goal"] and acts[0]["remove"] == [LEGACY]


# The files that may still mention the legacy label: the owner files named in the design for the
# retirement slices (writers converted, readers kept legacy-inert). A mention anywhere else is a new
# unowned site and must be added here on purpose.
_OWNERS = frozenset("""
skills/sigma-define/scripts/define.py
skills/sigma-doctor/scripts/doctor.py
skills/sigma-loop/scripts/auto_unpark.py
skills/sigma-loop/scripts/blockers.py
skills/sigma-loop/scripts/feature_owner.py
skills/sigma-loop/scripts/feature_propagate.py
skills/sigma-loop/scripts/gate_hold.py
skills/sigma-loop/scripts/handoff.py
skills/sigma-loop/scripts/loop.py
skills/sigma-loop/scripts/mirror.py
skills/sigma-loop/scripts/park_mix.py
skills/sigma-loop/scripts/promote.py
skills/sigma-loop/scripts/reconcile.py
skills/sigma-loop/scripts/sources.py
skills/sigma-loop/scripts/triage.py
skills/sigma-loop/scripts/unpark.py
skills/sigma-scope/scripts/assign.py
skills/sigma-scope/scripts/compile_plan.py
skills/sigma-status/scripts/status.py
""".split())
_MENTION = re.compile(r"needs-confirmation|needs_confirmation|NEEDS_CONFIRMATION|proposed_label"
                      r"|PROPOSED_LABEL")


def unowned_label_sites(root):
    root = pathlib.Path(root)
    return sorted(str(f.relative_to(root)) for f in root.glob("skills/**/scripts/*.py")
                  if str(f.relative_to(root)) not in _OWNERS
                  and _MENTION.search(f.read_text(encoding="utf-8")))


def test_no_unowned_label_site_remains():
    assert unowned_label_sites(ROOT) == []


def test_planted_unowned_label_site_is_caught(tmp_path):
    d = tmp_path / "skills" / "sigma-x" / "scripts"
    d.mkdir(parents=True)
    (d / "planted.py").write_text("# adds sdlc:needs-confirmation\n", encoding="utf-8")
    assert unowned_label_sites(tmp_path) == ["skills/sigma-x/scripts/planted.py"]
