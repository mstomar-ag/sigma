"""#943 -- the heading-aware CHANGELOG union (`work._union_headed`), a sibling of `_union_diff3`.

`_union_diff3` is left byte-identical. `work.rebase` is the one place that reads the upkeep gate
(`feature_upkeep.conflict_level`) and hands `_union_rescue` the sibling only while it is open, so
the three default-on callers (the in-pass goal replay, `ensure_fresh`, `_reconcile_behind`) inherit
the choice. Real git throughout: the point is where the entry lands in the pushed file.
"""
import hashlib
import inspect
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_feature_rebase as tfr  # noqa: E402
from test_feature_rebase import FEATURE, UNIT, World, _git, _load, _write  # noqa: E402

_ORIGINAL_UNION_DIFF3_SHA = "7149d016eb17d3d309c63ff864aeb496c0efb5041207885969bf07962b28e83b"
CUT = "—"
SEED = "# Changelog\n\n## Unreleased\n\n## 1.0.3 %s 2026-01-01\n\n- old\n" % CUT
GOAL = SEED.replace("## Unreleased\n\n", "## Unreleased\n\n- **goal entry**\n\n")
RELEASED = SEED.replace("## Unreleased\n\n", "## Unreleased\n\n## 1.0.4 %s 2026-02-02\n\n- cut entry\n\n" % CUT)

OPEN = {"upkeep": {"enabled": True, "conflicts": {"resolve": "mechanical"}}}
OPEN_AGENT = {"upkeep": {"enabled": True, "conflicts": {"resolve": "agent"}}}
NEAR = [{}, {"upkeep": {}}, {"upkeep": {"enabled": True}},
        {"upkeep": {"enabled": True, "conflicts": {"resolve": "off"}}},
        {"upkeep": {"enabled": False, "conflicts": {"resolve": "mechanical"}}},
        {"upkeep": {"enabled": "true", "conflicts": {"resolve": "mechanical"}}},
        {"upkeep": {"enabled": True, "conflicts": {"resolve": "bogus"}}}]


def _merged(*pairs):
    """A diff3 conflict text: (ours, theirs) line-lists under the Unreleased heading."""
    out = "# Changelog\n\n## Unreleased\n\n"
    for ours, theirs in pairs:
        out += "<<<<<<< HEAD\n%s||||||| base\n=======\n%s>>>>>>> sdlc/1-x\n" % (
            "".join(x + "\n" for x in ours), "".join(x + "\n" for x in theirs))
    return out + "\n## 1.0.3\n"


def _nonblank(text):
    return sorted(ln for ln in text.split("\n") if ln.strip())


def _world(tmp_path):
    world = World(tmp_path).build(feature_commits=(("CHANGELOG.md", SEED.rstrip("\n"), "seed changelog"),))
    path = world.goal_branch(11, files=(("CHANGELOG.md", GOAL.rstrip("\n")),))
    _git(world.local, "checkout", "-q", FEATURE)
    _write(world.local / "CHANGELOG.md", RELEASED)
    _git(world.local, "commit", "-qam", "cut 1.0.4")
    _git(world.local, "push", "-q", "origin", FEATURE)
    _git(world.local, "checkout", "-q", tfr.INTEGRATION)
    tfr._register_agent(world.sdlc, 11, 999999)
    return world, path


def _remote_changelog(world):
    return _git(world.local, "--git-dir", str(world.remote), "show", "sdlc/11:CHANGELOG.md") + "\n"


def _drive(caller, world, cfg, monkeypatch):
    work = _load("work")
    sdlc = str(world.sdlc)
    if caller == "rebase":
        return work.rebase(sdlc, cfg, "11")
    if caller == "ensure_fresh":
        return work.ensure_fresh(sdlc, cfg, "11")
    if caller == "reconcile":
        monkeypatch.setattr(work, "gate", lambda *a, **k: (True, "CLEAN", {}))
        monkeypatch.setattr(work.state, "reanchor_content", lambda *a, **k: None)
        return work._reconcile_behind(sdlc, cfg, "11")
    assert caller == "in-pass"
    m = _load("feature_rebase")
    report = {"replayed": [], "conflicts": [], "skipped": []}
    m._replay_goals(sdlc, cfg, "99", UNIT, FEATURE, [11], m._run, report, str(world.local), "origin")
    return report


CALLERS = ["rebase", "ensure_fresh", "reconcile", "in-pass"]


def _under(text, entry):
    head = None
    for ln in text.split("\n"):
        if ln.startswith("## "):
            head = ln
        if ln == entry:
            return head
    return None


# --- the function itself -------------------------------------------------------------------------


def test_release_cut_shape_files_the_entry_under_unreleased():
    work = _load("work")
    text = _merged((["## 1.0.4 %s d" % CUT, "", "- cut entry", ""], ["- **goal entry**", ""]))
    legacy, ok1 = work._union_diff3(text)
    headed, ok2 = work._union_headed(text)
    assert ok1 and ok2
    assert _under(legacy, "- **goal entry**").startswith("## 1.0.4")
    assert _under(headed, "- **goal entry**") == "## Unreleased"
    assert _under(headed, "- cut entry").startswith("## 1.0.4")
    assert "\n- **goal entry**\n\n## 1.0.4" in headed          # a blank line keeps the heading apart


def test_same_lines_as_the_legacy_union_placement_only():
    work = _load("work")
    for pairs in (
        [(["## 1.0.4 %s d" % CUT, "", "- cut entry", ""], ["- **goal entry**", ""])],
        [(["## 1.0.4 %s d" % CUT, "", "- cut entry"], ["- **goal entry**"])],
        [(["- upstream entry"], ["- unit entry"])],
        [(["- same"], ["- same"])],
    ):
        text = _merged(*pairs)
        legacy, _ = work._union_diff3(text)
        headed, ok = work._union_headed(text)
        assert ok and _nonblank(headed) == _nonblank(legacy), pairs


def test_no_heading_in_ours_keeps_the_legacy_order():
    work = _load("work")
    text = _merged((["- upstream entry"], ["- unit entry"]))
    assert work._union_headed(text) == work._union_diff3(text)


@pytest.mark.parametrize("ours,theirs", [
    (["## [1.0.4] - d", "", "- c"], ["- e"]),                 # bracketed release heading
    (["## 1.0.4 %s d" % CUT, "", "- c"], ["## Extra", "- e"]),  # unknown heading on the unit side
    (["## Whatever", "", "- c"], ["- e"]),                    # unknown top section
    (["[1.0.4]: https://example.invalid/x"], ["- e"]),        # link footer
    (["## 1.0.4 %s d" % CUT, "- c"], ["## 1.0.5 %s d" % CUT, "- e"]),  # the unit adds a heading too
])
def test_unrecognised_shapes_park(ours, theirs):
    assert _load("work")._union_headed(_merged((ours, theirs))) == (None, False)


def test_a_heading_in_ours_with_no_unreleased_section_above_parks():
    work = _load("work")
    text = ("# Changelog\n\n## 1.0.3 %s d\n\n<<<<<<< HEAD\n## 1.0.4 %s d\n||||||| base\n=======\n- e\n"
            ">>>>>>> x\n" % (CUT, CUT))
    assert work._union_headed(text) == (None, False)
    text2 = "<<<<<<< HEAD\n## 1.0.4 %s d\n||||||| base\n=======\n- e\n>>>>>>> x\n" % CUT
    assert work._union_headed(text2) == (None, False)


def test_edited_base_and_two_way_markers_park_like_the_legacy_union():
    work = _load("work")
    edited = ("## Unreleased\n<<<<<<< HEAD\n- a\n||||||| base\n- orig\n=======\n- b\n>>>>>>> x\n")
    two_way = "<<<<<<< HEAD\n- a\n=======\n- b\n>>>>>>> x\n"
    for text in (edited, two_way, "no conflict here\n"):
        assert work._union_headed(text) == (None, False) == work._union_diff3(text)


def test_dedup_empties_the_commit_the_same_way_as_the_legacy_union():
    work = _load("work")
    text = _merged((["- same", ""], ["- same", ""]))
    headed, ok = work._union_headed(text)
    assert ok and headed.count("- same") == 1


def test_union_diff3_is_unchanged_and_the_sibling_is_a_separate_function():
    work = _load("work")
    src = inspect.getsource(work._union_diff3)
    assert hashlib.sha256(src.encode()).hexdigest() == _ORIGINAL_UNION_DIFF3_SHA
    assert work._union_headed is not work._union_diff3
    assert inspect.signature(work._try_union_changelog).parameters["union"].default is work._union_diff3
    assert inspect.signature(work._union_rescue).parameters["union"].default is work._union_diff3


# --- the gate, and the three default-on callers ----------------------------------------------------


def test_union_for_reads_part_a_gate_and_nothing_else():
    work = _load("work")
    assert work._union_for(OPEN) is work._union_headed
    assert work._union_for(OPEN_AGENT) is work._union_headed
    for cfg in NEAR + [None, "x", 3]:
        assert work._union_for(cfg) is work._union_diff3, cfg


@pytest.mark.parametrize("caller", CALLERS)
def test_closed_gate_is_byte_identical_to_legacy_through_each_caller(tmp_path, monkeypatch, caller):
    for n, extra in enumerate([{}, NEAR[2], NEAR[4]]):
        sub = tmp_path / str(n)
        sub.mkdir()
        world, _ = _world(sub)
        cfg = tfr._cfg()
        cfg.update(extra)
        _drive(caller, world, cfg, monkeypatch)
        out = _remote_changelog(world)
        assert _under(out, "- **goal entry**").startswith("## 1.0.4"), (extra, out)


@pytest.mark.parametrize("caller", CALLERS)
def test_open_gate_lands_the_entry_under_unreleased_through_each_caller(tmp_path, monkeypatch, caller):
    world, _ = _world(tmp_path)
    cfg = tfr._cfg()
    cfg.update(OPEN)
    _drive(caller, world, cfg, monkeypatch)
    out = _remote_changelog(world)
    assert _under(out, "- **goal entry**") == "## Unreleased", out
    assert _under(out, "- cut entry").startswith("## 1.0.4"), out
    assert _nonblank(out) == _nonblank(GOAL.replace("## Unreleased", "## Unreleased\n## 1.0.4 %s 2026-02-02\n- cut entry" % CUT))


def test_open_gate_unrecognised_shape_parks_and_leaves_the_remote_alone(tmp_path):
    world, path = _world(tmp_path)
    _git(world.local, "checkout", "-q", FEATURE)
    _write(world.local / "CHANGELOG.md", RELEASED.replace("## 1.0.4 %s 2026-02-02" % CUT, "## [1.0.4] - 2026-02-02"))
    _git(world.local, "commit", "-qam", "bracket heading")
    _git(world.local, "push", "-q", "origin", FEATURE)
    _git(world.local, "checkout", "-q", tfr.INTEGRATION)
    before = world.tip("sdlc/11")
    cfg = tfr._cfg()
    cfg.update(OPEN)
    out = _load("work").rebase(str(world.sdlc), cfg, "11")
    assert out.startswith("rebase deferred"), out
    assert world.tip("sdlc/11") == before
    assert not (path / ".git").is_dir() or _git(path, "status", "--porcelain").count("UU") == 0


def test_open_gate_same_lines_conflict_still_unions_in_legacy_order(tmp_path):
    """No heading on the upstream side: the sibling and the legacy union agree exactly."""
    world = World(tmp_path).build(feature_commits=(("CHANGELOG.md", SEED.rstrip("\n"), "seed"),))
    world.goal_branch(11, files=(("CHANGELOG.md", GOAL.rstrip("\n")),))
    _git(world.local, "checkout", "-q", FEATURE)
    _write(world.local / "CHANGELOG.md", SEED.replace("## Unreleased\n\n", "## Unreleased\n\n- **upstream entry**\n\n"))
    _git(world.local, "commit", "-qam", "upstream entry")
    _git(world.local, "push", "-q", "origin", FEATURE)
    _git(world.local, "checkout", "-q", tfr.INTEGRATION)
    cfg = tfr._cfg()
    cfg.update(OPEN)
    out = _load("work").rebase(str(world.sdlc), cfg, "11")
    assert out.startswith("rebased"), out
    text = _remote_changelog(world)
    assert _under(text, "- **upstream entry**") == _under(text, "- **goal entry**") == "## Unreleased"


def test_headed_output_survives_reanchor_and_done_refusal(tmp_path):
    import test_rebase_reanchors_fingerprint as rr
    state = _load("state")
    root, _g = rr._repo(tmp_path)
    sdlc = root / ".sdlc"
    sdlc.mkdir()
    (root / "code.py").write_text("x = 2\n")
    rr._evidence(sdlc, "77", root, "HEAD", state.content_fingerprint(str(root), "HEAD", ".sdlc"))
    headed, ok = _load("work")._union_headed(_merged((["## 1.0.4 %s d" % CUT, "", "- c", ""], ["- e", ""])))
    assert ok
    (root / "CHANGELOG.md").write_text(headed)
    assert state.done_refusal(str(sdlc), "77") is not None
    assert state.reanchor_content(str(sdlc), "77") is True
    assert state.done_refusal(str(sdlc), "77") is None
