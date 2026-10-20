"""#1968: `upkeep.conflicts.union_paths` names files whose two-sided pure additions the mechanical level unions line
by line. An unlisted file keeps the recognised append-only shapes only; a listed file with an edited or removed base
line still parks; a wrong type turns the whole upkeep block off. Offline: pure functions plus real git in scratch
repositories through the upkeep entry point, no model call and no network call."""
import copy

import pytest

import test_feature_rebase as base
import test_upkeep_level1 as lv
import upkeep_support as support
import git_identity_support  # noqa: E402

proof = support.script("conflict_proof")
prove = support.script("feature_upkeep_prove")
PROSE = "intro\nmiddle\nend\n"


@pytest.fixture(autouse=True)
def _scoped_git_identity(tmp_path_factory, monkeypatch):
    git_identity_support.scoped_git_identity(tmp_path_factory.mktemp("git-identity"), monkeypatch)


def config(paths):
    c = lv.cfg()
    c["upkeep"]["conflicts"]["union_paths"] = paths
    return c


def fake_run(b, o, t):
    blobs = {1: b, 2: o, 3: t}

    def run(cwd, argv):
        return blobs[int(argv[2].split(":")[1])]
    return run


# ------------------------------------------------------------------ the key

def test_default_is_empty_and_the_template_agrees():
    g = support.gate()
    assert g.DEFAULTS["conflicts.union_paths"] == []
    assert support.template_cfg()["upkeep"]["conflicts"]["union_paths"] == []
    assert g.read(lv.cfg()).settings["conflicts.union_paths"] == []


@pytest.mark.parametrize("bad", ["notes.txt", {"a": 1}, [1], ["ok", 2], None, True, [""]])
def test_a_wrong_type_turns_the_block_off(bad):
    reading = support.gate().read(config(bad))
    assert reading.enabled is False and any("conflicts.union_paths" in p for p in reading.problems), bad


def test_a_list_of_names_is_accepted_and_reaches_the_level1_options():
    g = support.gate()
    assert g.read(config(["notes.txt", "docs/x.md"])).settings["conflicts.union_paths"] == ["notes.txt", "docs/x.md"]
    m = lv.mod()
    assert tuple(m._level1_options(config(["notes.txt"]))["union_paths"]) == ("notes.txt",)
    assert tuple(m._level1_options(lv.cfg())["union_paths"]) == ()


# ------------------------------------------------------------------ the pure union

def test_line_union_keeps_base_order_then_ours_then_theirs_and_collapses_duplicates():
    got = proof.line_union(PROSE, "intro\nmiddle\nours\nend\n", "intro\ntheirs\nmiddle\nours\nend\n")
    assert got == "intro\nmiddle\nours\nend\ntheirs\n"


@pytest.mark.parametrize("o,t", [("intro\nMIDDLE\nend\nours\n", PROSE + "theirs\n"),
                                 ("intro\nend\nours\n", PROSE + "theirs\n"),
                                 (PROSE + "ours\n", "intro\nend\ntheirs\n"),
                                 (PROSE + "ours\n", "intro\nend\n"),          # theirs only removes: no addition to lose
                                 ("intro\nend\n", PROSE + "theirs\n")])
def test_line_union_refuses_an_edit_or_removal_on_either_side(o, t):
    assert proof.line_union(PROSE, o, t) is None


def test_append_text_consults_the_list():
    run = fake_run(PROSE, PROSE + "ours\n", PROSE + "theirs\n")
    assert prove._append_text(run, "/w", "notes.txt") is None
    assert prove._append_text(run, "/w", "notes.txt", ("other.txt",)) is None
    assert prove._append_text(run, "/w", "notes.txt", ("notes.txt",)) == PROSE + "ours\ntheirs\n"


def test_append_text_on_a_listed_path_still_parks_an_edit():
    run = fake_run(PROSE, "intro\nMIDDLE\nend\n", PROSE + "theirs\n")
    assert prove._append_text(run, "/w", "notes.txt", ("notes.txt",)) is None


# ------------------------------------------------------------------ real git

def scenario(tmp_path, monkeypatch, paths, feat, integ, name="notes.txt"):
    m = lv.mod()
    w = lv.world(tmp_path, base_files={name: PROSE}, feat={name: feat}, integ={name: integ})
    before = w.tip(lv.FEATURE)
    lv.verify_ok(m, monkeypatch)
    report, spy = lv.run_pass(m, w, config(paths))
    return m, w, before, report, spy


def test_a_listed_file_with_two_sided_additions_is_unioned_end_to_end(tmp_path, monkeypatch):
    m, w, before, report, spy = scenario(tmp_path, monkeypatch, ["notes.txt"], "intro\nmiddle\nunit\nend\n",
                                         "intro\nmiddle\nupstream\nend\n")
    assert report["outcome"] == m.REBASED and report["resolved"]["level"] == 1, report.get("why")
    text = base._git(w.local, "show", "origin/%s:notes.txt" % lv.FEATURE)
    assert text + "\n" == "intro\nmiddle\nupstream\nend\nunit\n"


def test_an_unlisted_file_parks_untouched(tmp_path, monkeypatch):
    m, w, before, report, spy = scenario(tmp_path, monkeypatch, [], "intro\nmiddle\nunit\nend\n",
                                         "intro\nmiddle\nupstream\nend\n")
    assert report["outcome"] == m.PARKED and w.tip(lv.FEATURE) == before and not spy.pushes()


def test_a_listed_file_with_an_edited_base_line_parks_untouched(tmp_path, monkeypatch):
    m, w, before, report, spy = scenario(tmp_path, monkeypatch, ["notes.txt"], "intro\nMIDDLE\nunit\nend\n",
                                         "intro\nmiddle\nupstream\nend\n")
    assert report["outcome"] == m.PARKED and w.tip(lv.FEATURE) == before and not spy.pushes()


def test_a_wrong_type_runs_no_level1_at_all(tmp_path, monkeypatch):
    m, w, before, report, spy = scenario(tmp_path, monkeypatch, "notes.txt", "intro\nmiddle\nunit\nend\n",
                                         "intro\nmiddle\nupstream\nend\n")
    assert w.tip(lv.FEATURE) == before and not spy.pushes() and report["outcome"] != m.REBASED
