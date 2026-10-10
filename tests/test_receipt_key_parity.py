"""#932 (part C, slice 4): both unit-landing observers key one PR identically while the upkeep gate is open.

With the gate closed the unit-completion observer still keys by the bare unit name (byte-identical to before);
with it open both observers take the unit's branch through `merge_observation.landing_owner_id`, and a landing
already recorded under the bare key is not recorded twice."""
import importlib.util
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOOP = ROOT / "skills" / "sigma-loop" / "scripts"
REBASE = ROOT / "skills" / "sigma-rebase" / "scripts"
UNIT, BRANCH, SHA = "int-contract", "feature/int-contract", "a" * 40


def _load(name, directory):
    spec = importlib.util.spec_from_file_location(name, directory / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


uc = _load("unit_completion", LOOP)
mo = _load("merge_observation", LOOP)
vm = _load("verify_merge", REBASE)

OPEN = {"upkeep": {"enabled": True}, "ledger": {"enabled": True, "actor": "t"}}
CLOSED = {"ledger": {"enabled": True, "actor": "t"}}
PR = json.dumps({"number": 41, "node_id": "PR_41", "created_at": "2026-01-01T00:00:00Z",
                 "merged_at": "2026-08-22T00:00:00Z", "merge_commit_sha": SHA, "head": {"ref": BRANCH},
                 "base": {"ref": "main", "repo": {"node_id": "R_1"}}})


def _run(cwd, argv):
    assert argv[0] == "gh"
    return PR


def _sdlc(tmp_path):
    d = tmp_path / ".sdlc"
    (d / "state").mkdir(parents=True)
    return str(d)


def _unit_side(d, config):
    return uc._record_unit_landing_merge(_run, str(pathlib.Path(d).parent), d, config, None, UNIT,
                                         "acme/app", BRANCH, 41)


def _rebase_side(d, config):
    return vm.record_merge(d, config, BRANCH, 41, "why", run=_run, cwd=str(pathlib.Path(d).parent))


def _keys(owner):
    facts = {"canonical_repository_id": "R_1", "owner_kind": "unit", "owner_id": owner, "goal": None,
             "head_ref": BRANCH, "base_ref": "main", "pr_number": 41, "pr_node_id": "PR_41",
             "creating_writer": "x", "pr_created_at": "2026-01-01T00:00:00Z"}
    return mo.observation_keys(mo.ownership_key(facts), SHA)[0]


def _merged(d):
    return [e for e in uc.ledger.read_all(d) if e.get("kind") == "merged"]


def _deliveries(d):
    return sorted(p.name for p in (pathlib.Path(d) / "state" / "feature-merge-deliveries").glob("*.json"))


def test_helper_is_the_branch_form_and_refuses_empty():
    assert mo.landing_owner_id(BRANCH) == BRANCH
    for bad in ("", None, 3):
        try:
            mo.landing_owner_id(bad)
        except ValueError:
            continue
        raise AssertionError("accepted %r" % (bad,))


def test_parity_one_entry_key_from_both_observers_when_open(tmp_path):
    d = _sdlc(tmp_path)
    _unit_side(d, OPEN)
    assert _deliveries(d) == [_keys(BRANCH) + ".json"]
    _rebase_side(d, OPEN)
    assert len(_merged(d)) == 1
    assert _merged(d)[0]["merged_entry_key"] == _keys(BRANCH)
    assert _deliveries(d) == [_keys(BRANCH) + ".json"]


def test_parity_in_the_other_order(tmp_path):
    d = _sdlc(tmp_path)
    _rebase_side(d, OPEN)
    _unit_side(d, OPEN)
    assert len(_merged(d)) == 1 and _merged(d)[0]["merged_entry_key"] == _keys(BRANCH)


def test_closed_gate_keeps_the_bare_unit_key(tmp_path):
    d = _sdlc(tmp_path)
    _unit_side(d, CLOSED)
    assert _deliveries(d) == [_keys(UNIT) + ".json"]
    assert _merged(d)[0]["merged_entry_key"] == _keys(UNIT) != _keys(BRANCH)


def test_closed_gate_variants_all_keep_the_bare_key(tmp_path):
    for i, block in enumerate(({"enabled": False}, {"enabled": "true"}, {"enabled": 1}, {"enabled": True, "bogus": 1})):
        (tmp_path / str(i)).mkdir()
        d = _sdlc(tmp_path / str(i))
        _unit_side(d, dict(CLOSED, upkeep=block))
        assert _deliveries(d) == [_keys(UNIT) + ".json"], block


def test_legacy_unit_side_alone_writes_nothing_new(tmp_path):
    d = _sdlc(tmp_path)
    _unit_side(d, CLOSED)
    before = _merged(d)
    _unit_side(d, OPEN)
    assert _merged(d) == before and _deliveries(d) == [_keys(UNIT) + ".json"]


def test_dormant_twin_uses_the_shared_rule():
    src = (LOOP / "unit_completion.py").read_text(encoding="utf-8")
    assert src.count("landing_owner_id(") == 2
