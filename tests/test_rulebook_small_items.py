"""#1345 small rulebook items: the hard-stop net follows the Rulebook reader's whole-block close, and the own-branch
carve-out for registered unit branches is pinned against the real registry write surface."""
import importlib.util
import pathlib

import pytest

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


G = _load("hard_stop_guard")
H = _load("hard_stop")
CFG = _load("rulebook_cfg")
R = _load("feature_registry")

PUSH_MAIN = "git push --force origin main"
BAD = ("yes", 1, 0, "", [], {})


def _cfg(**parts):
    block = {"hard_stops": {"enabled": True}}
    block.update(parts)
    return {"rulebook": block}


@pytest.mark.parametrize("bad", BAD)
def test_wrong_typed_sibling_enabled_closes_net(bad):
    cfg = _cfg(records={"enabled": bad})
    assert CFG.load(cfg).closed_reason() == "rulebook.records.enabled must be true or false"
    assert G.enabled(cfg) is False
    assert not G.guard(PUSH_MAIN, {"branch": "main"}, cfg).blocked


@pytest.mark.parametrize("bad", BAD)
def test_wrong_typed_own_enabled_closes_net(bad):
    cfg = {"rulebook": {"hard_stops": {"enabled": bad}}}
    assert G.enabled(cfg) is False
    assert not G.guard(PUSH_MAIN, {"branch": "main"}, cfg).blocked


def test_wrong_typed_block_closes_net():
    assert G.enabled({"rulebook": "on"}) is False


def test_true_opens_and_well_typed_siblings_do_not_close():
    assert G.enabled(_cfg()) is True
    assert G.enabled(_cfg(records={"enabled": False}, autonomy=None)) is True
    assert G.guard(PUSH_MAIN, {"branch": "main"}, _cfg()).blocked


def test_closed_gate_unchanged():
    for cfg in (None, {}, {"rulebook": {}}, {"rulebook": {"hard_stops": {"enabled": False}}}):
        assert G.enabled(cfg) is False
        assert G.guard(PUSH_MAIN, {"branch": "main"}, cfg).blocked is False


@pytest.fixture
def registry(tmp_path):
    d = tmp_path / "features"
    R.write_unit(d, "voice", {"title": "t", "owner": "@o", "open": True, "parent": None,
                              "tracking_issue": None, "priority": None, "repos": {}})
    return R.read(d)


def test_force_push_to_registered_unit_branch_not_flagged(registry):
    """Pinning: item 2 did not reproduce; the carve-out already holds for the real registry shape."""
    cfg = _cfg()
    for text in ("git push --force-with-lease origin HEAD:refs/heads/feature/voice",
                 "git push --force origin feature/voice", "git push origin +feature/voice"):
        for ctx in ({"registry": registry}, {"registry": registry, "branch": "feature/voice"}):
            assert not G.guard(text, ctx, cfg).blocked, (text, ctx)
            assert H.classify(text, ctx, cfg).pattern_id == "own-branch"


def test_force_push_to_unregistered_branch_flagged(registry):
    cfg = _cfg()
    for text in ("git push --force origin feature/other", PUSH_MAIN):
        assert G.guard(text, {"registry": registry}, cfg).blocked
    assert G.guard("git push --force origin feature/voice", {"registry": None}, cfg).blocked
