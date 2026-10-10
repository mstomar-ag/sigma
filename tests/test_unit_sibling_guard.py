"""#934 (part C, slice 7): the unit-keyed cross-repo sibling lookup and its refusal guard.

The guard answers one question for the landing engine: has every other repository's half of this unit landed.
It refuses when a sibling has not, and when the lookup itself cannot answer (fail closed on error, not on absence).
With the upkeep gate closed it does nothing at all."""
import importlib.util
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOOP = ROOT / "skills" / "sigma-loop" / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, LOOP / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


cross_repo = _load("cross_repo")
work = _load("work")
registry = _load("feature_registry")

UNIT, HERE, OTHER = "int-contract", "acme/app", "acme/lib"
OPEN = {"upkeep": {"enabled": True}}
CLOSED = {}


def _sdlc(tmp_path):
    d = tmp_path / ".sdlc"
    (d / "state").mkdir(parents=True)
    return str(d)


def _unit(d, repos=None, name=UNIT):
    repos = repos if repos is not None else {
        HERE: {"branch": "feature/" + name, "goals": [1]},
        OTHER: {"branch": "feature/" + name, "goals": [2]}}
    registry.write_unit(registry.registry_dir(d), name, {"repos": repos})


def _landing(d, goal, outcome="tier-1", unit=UNIT, repos=None, name=None, raw=None):
    folder = pathlib.Path(d) / "state" / "landing"
    folder.mkdir(parents=True, exist_ok=True)
    rec = {"schema": cross_repo.RECORD_SCHEMA, "goal": str(goal), "unit": unit, "outcome": outcome,
           "cross_repo": outcome in ("tier-1", "tier-2"),
           "repos": repos if repos is not None else {HERE: "granted", OTHER: "granted"}}
    (folder / (name or "%s.json" % goal)).write_text(raw if raw is not None else json.dumps(rec),
                                                     encoding="utf-8")


def _guard(d, landed, config=OPEN, unit=UNIT, here=HERE):
    return work.unit_sibling_guard(d, config, unit, here, landed=landed)


def test_sibling_landed_passes(tmp_path):
    d = _sdlc(tmp_path)
    _unit(d)
    _landing(d, 1)
    calls = []

    def landed(repo, branch):
        calls.append((repo, branch))
        return True
    assert _guard(d, landed) == (True, "")
    assert calls == [(OTHER, "feature/" + UNIT)]


def test_sibling_not_landed_refuses_and_names_it(tmp_path):
    d = _sdlc(tmp_path)
    _unit(d)
    _landing(d, 1)
    ok, why = _guard(d, lambda repo, branch: False)
    assert ok is False
    assert OTHER in why and UNIT in why and "not landed" in why


def test_lookup_that_cannot_answer_refuses(tmp_path):
    d = _sdlc(tmp_path)
    _unit(d)
    _landing(d, 1)

    def boom(repo, branch):
        raise RuntimeError("network down")
    for landed in (boom, lambda r, b: None, lambda r, b: "yes", lambda r, b: 1, None):
        ok, why = _guard(d, landed)
        assert ok is False and OTHER in why


def test_unreadable_or_foreign_records_refuse(tmp_path):
    for raw in ("{not json", json.dumps({"schema": "other@1", "unit": UNIT}), json.dumps([1])):
        d = _sdlc(tmp_path / ("r%d" % abs(hash(raw))))
        _unit(d)
        _landing(d, 1, raw=raw, name="9.json")
        ok, why = _guard(d, lambda r, b: True)
        assert ok is False and "9.json" in why


def test_flagged_record_for_the_unit_refuses(tmp_path):
    d = _sdlc(tmp_path)
    _unit(d)
    _landing(d, 1, outcome="flagged")
    ok, why = _guard(d, lambda r, b: True)
    assert ok is False and "flagged" in why


def test_tier_two_half_pair_is_still_checked(tmp_path):
    d = _sdlc(tmp_path)
    _unit(d)
    _landing(d, 1, outcome="tier-2")
    assert _guard(d, lambda r, b: False)[0] is False


def test_absence_is_not_an_error(tmp_path):
    # no records at all, no registry: not applicable, zero lookups
    d = _sdlc(tmp_path)
    assert _guard(d, lambda r, b: 1 / 0) == (True, "")
    # a single-repo unit with only inert records
    _unit(d, repos={HERE: {"branch": "feature/" + UNIT, "goals": [1]}})
    _landing(d, 1, outcome="not-cross-repo", repos={})
    assert _guard(d, lambda r, b: 1 / 0) == (True, "")


def test_other_units_records_are_ignored(tmp_path):
    d = _sdlc(tmp_path)
    _unit(d, repos={HERE: {"branch": "feature/" + UNIT, "goals": [1]}})
    _landing(d, 5, unit="other-unit", outcome="flagged")
    assert _guard(d, lambda r, b: 1 / 0) == (True, "")


def test_sibling_without_a_recorded_branch_refuses(tmp_path):
    d = _sdlc(tmp_path)
    _unit(d, repos={HERE: {"branch": "feature/x", "goals": [1]}, OTHER: {"goals": [2]}})
    _landing(d, 1)
    ok, why = _guard(d, lambda r, b: True)
    assert ok is False and "branch" in why


def test_repo_spelling_is_compared_case_insensitively(tmp_path):
    d = _sdlc(tmp_path)
    _unit(d)
    _landing(d, 1)
    seen = []
    assert _guard(d, lambda r, b: seen.append(r) or True, here="Acme/App") == (True, "")
    assert seen == [OTHER]


def test_record_ceiling_refuses_rather_than_scanning_unbounded(tmp_path):
    d = _sdlc(tmp_path)
    _unit(d)
    for n in range(1, 5):
        _landing(d, n)
    ok, why = cross_repo.unit_sibling_check(d, UNIT, HERE, lambda r, b: True, max_records=3)
    assert ok is False and "3" in why
    assert cross_repo.unit_sibling_check(d, UNIT, HERE, lambda r, b: True, max_records=4) == (True, "")
    assert cross_repo.MAX_UNIT_LOOKUP_RECORDS == 5000


def test_symlinked_record_refuses(tmp_path):
    d = _sdlc(tmp_path)
    _unit(d)
    _landing(d, 1)
    folder = pathlib.Path(d) / "state" / "landing"
    (folder / "2.json").symlink_to(folder / "1.json")
    ok, why = _guard(d, lambda r, b: True)
    assert ok is False and "2.json" in why


def test_never_raises_on_hostile_inputs(tmp_path):
    d = _sdlc(tmp_path)
    for unit, here in ((None, HERE), ("", HERE), (["x"], HERE), (UNIT, None), (UNIT, ["a"])):
        ok, why = _guard(d, lambda r, b: True, unit=unit, here=here)
        assert ok is False and why
    assert work.unit_sibling_guard(None, OPEN, UNIT, HERE, landed=None)[0] is False


def test_gate_closed_does_nothing(tmp_path):
    d = _sdlc(tmp_path)
    _unit(d)
    _landing(d, 1, outcome="flagged")
    before = sorted(str(p) for p in pathlib.Path(d).rglob("*"))
    for config in (CLOSED, {"upkeep": {"enabled": "true"}}, {"upkeep": {"enabled": 1}}, None, "x"):
        assert work.unit_sibling_guard(d, config, UNIT, HERE, landed=lambda r, b: 1 / 0) == (True, "")
    assert sorted(str(p) for p in pathlib.Path(d).rglob("*")) == before


def test_lookup_writes_nothing_and_leaves_landing_store_alone(tmp_path):
    d = _sdlc(tmp_path)
    _unit(d)
    _landing(d, 1)
    snap = {p: p.read_bytes() for p in pathlib.Path(d).rglob("*") if p.is_file()}
    _guard(d, lambda r, b: True)
    assert {p: p.read_bytes() for p in pathlib.Path(d).rglob("*") if p.is_file()} == snap


def test_registration_and_exempt_set():
    names = {g["function"] for g in work.ENFORCEMENT_GATES}
    assert "unit_sibling_guard" in names
    row = [g for g in work.ENFORCEMENT_GATES if g["function"] == "unit_sibling_guard"][0]
    assert row["enabled_by"] == ("upkeep.enabled",)
    # S-60: the pure goal-keyed check stays unwired and exempt; the new guard does not call it.
    exempt = {name for name, _ in work.ENFORCEMENT_EXEMPT}
    assert {"sibling_gate", "_sibling_gate"} <= exempt and "unit_sibling_guard" not in exempt
    import inspect
    src = inspect.getsource(work.unit_sibling_guard) + inspect.getsource(cross_repo.unit_sibling_check)
    assert "sibling_gate(" not in src
