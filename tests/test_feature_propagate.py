"""feature_propagate.py -- the registry entry copied to sibling repos, one-directionally
(#1477, epic #1464, story #1427).

#1473 records a goal under its unit in THIS repo. This is the half that reaches the OTHER ones:
§7.1 requires every participating repo to carry the WHOLE entry -- both repos' branches and both
repos' goals -- so that somebody who later loses access to one repo still sees the whole picture
instead of half of it.

THREE PROPERTIES, AND EACH IS A SEPARATE CLASS OF TEST HERE:

  1. THE COPY IS WHOLE. `feature_registry.write_unit` replaces a unit's entry rather than merging
     it, so a half entry is not merely lossy -- it ERASES. Both directions are pinned: a shard
     written on the pick path must not erase the chart sheet's other repos, and a document
     propagated into a sibling must not erase the goals that sibling recorded itself.
  2. DISCOVERY FLOWS ONE WAY. Propagation reaches the repos `repos` already names. A goal picked in
     a repo that is NOT named is a scope expansion -- the unit owner's decision -- so it lands inert
     with a ledger entry to that owner and `repos` is UNCHANGED. The registry refusal and the
     `sdlc:needs-confirmation` overlay are asserted separately, because they live in different
     layers and either one alone is the bug.
  3. MISSING ACCESS LOSES NOTHING AND BLOCKS NOTHING. A sibling that cannot be written is ledgered
     to that repo's owner, the local end still lands, and the pick carries on.

WRITTEN AGAINST A MUTATION RUN, inheriting `tests/test_feature_sync.py`'s discipline whole:
`_mod_with` asserts its target exists and replaces EVERY occurrence, and every mutation test asserts
the mutant's BEHAVIOUR differs on a concrete input rather than merely that the suite went red.
"""
import base64
import importlib.util
import json
import pathlib
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"
P = SCRIPTS / "feature_propagate.py"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _mod():
    return _load("feature_propagate")


def _registry():
    return _load("feature_registry")


def _sync():
    return _load("feature_sync")


def _ledger():
    return _load("ledger")


def _mod_with(old, new):
    """The module rebuilt with a source substitution applied. Both of
    `tests/test_feature_registry.py::_mod_with`'s guards, for both of its reasons."""
    src = P.read_text(encoding="utf-8")
    assert old in src, "mutation target has drifted out of the source: %r" % (old,)
    namespace = {"__name__": "feature_propagate_variant", "__file__": str(P)}
    exec(compile(src.replace(old, new), str(P), "exec"), namespace)          # noqa: S102 - test-only
    return types.SimpleNamespace(**namespace)


HERE = "org/here"
THERE = "org/there"
UNIT = "int-contract"
CONFIG = {"work": {"enabled": True, "remote": "origin"},
          "discovery": {"source": "github", "github": {"repo": HERE}}}


def _entry(**over):
    """A full cross-repo entry -- both repos, both branches, both goal lists."""
    base = {"title": "Manifest duration contract", "owner": "@unit-owner", "open": True,
            "parent": None, "tracking_issue": "org/here#3100",
            "repos": {HERE: {"branch": "feature/int-contract", "owner": "@here-owner",
                             "authorized": True, "goals": [2871]},
                      THERE: {"branch": "feature/int-contract", "owner": "@there-owner",
                              "authorized": False, "goals": [456]}}}
    base.update(over)
    return base


def _sdlc(tmp_path, adopted=True, ledger_on=True, config=None):
    sdlc = tmp_path / ".sdlc"
    (sdlc / "state").mkdir(parents=True, exist_ok=True)
    sdlc.joinpath("config.json").write_text(
        json.dumps(dict(config or CONFIG, ledger={"enabled": bool(ledger_on), "actor": "tester"})),
        encoding="utf-8")
    if adopted:
        (sdlc / "features").mkdir(parents=True, exist_ok=True)
    return sdlc


def _seed(sdlc, name=UNIT, entry=None):
    return _registry().write_unit(sdlc / "features", name, entry if entry is not None else _entry())


def _grant(sdlc, goal="2879", repos=(THERE,), verdict="granted", unit=UNIT):
    """The landing decision #1472 records at pick. Written as the file, not through `cross_repo`,
    so this suite pins the CONTRACT between the two modules rather than one module's own helpers."""
    path = sdlc / "state" / "landing" / (str(goal) + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema": "sigma/landing@1", "goal": goal, "unit": unit, "outcome": "tier-1",
        "cross_repo": True,
        "repos": {r: {"verdict": verdict, "reason": None, "detail": ""} for r in repos},
    }), encoding="utf-8")
    return path


def _doc_bytes(name, entry):
    return _registry().dumps(_registry().document({name: entry}))


def _gh(remote=None, fail=(), sha="deadbeef"):
    """The `(cwd, argv) -> stdout` runner `work.py` injects. `remote` maps a repo slug to the
    document that repo's `units/<name>.json` already holds (None = 404, the file is absent)."""
    calls = []
    remote = dict(remote or {})

    def run(cwd, argv):
        line = " ".join(str(a) for a in argv)
        calls.append(line)
        for token, exc in fail:
            if token in line:
                raise exc
        if "-X PUT" in line:
            return json.dumps({"commit": {"sha": "0" * 40}})
        if "/contents/" in line:
            slug = line.split("repos/", 1)[1].split("/contents/", 1)[0]
            body = remote.get(slug)
            if body is None:
                raise RuntimeError("gh: Not Found (HTTP 404)")
            return json.dumps({"sha": sha, "encoding": "base64",
                               "content": base64.b64encode(body.encode("utf-8")).decode("ascii")})
        return ""

    run.calls = calls
    return run


def _puts(run):
    return [c for c in run.calls if "-X PUT" in c]


def _put_text(run, index=0):
    """The document one PUT actually sent, decoded back out of its base64 `content` field."""
    call = _puts(run)[index]
    field = [p for p in call.split(" -f ") if p.startswith("content=")][0]
    return base64.b64decode(field[len("content="):]).decode("utf-8")


def _put_document(run, index=0):
    return json.loads(_put_text(run, index))


def _propagate(sdlc, goal="2879", unit=UNIT, run=None, config=None, **kw):
    return _mod().propagate_at_pick(str(sdlc), config or CONFIG, goal, unit,
                                    run=run or _gh(), cwd=str(sdlc.parent), **kw)


def _notes(sdlc):
    return [e for e in _ledger().read_all(str(sdlc)) if e.get("kind") == "note"]


# --------------------------------------------------------------------------- 1. the copy is WHOLE


def test_the_propagated_document_carries_both_repos_branches_and_goals(tmp_path):
    """§7.1 in one assertion. The document that reaches the sibling describes the WHOLE unit, not
    the half this repo happens to own."""
    sdlc = _sdlc(tmp_path)
    _seed(sdlc)
    _grant(sdlc)
    run = _gh()
    report = _propagate(sdlc, run=run)
    sent = _put_document(run)["features"][UNIT]
    assert sorted(sent["repos"]) == sorted([HERE, THERE])
    assert sent["repos"][HERE]["goals"] == [2871]
    assert sent["repos"][THERE]["goals"] == [456]
    assert sent["repos"][HERE]["branch"] == "feature/int-contract"
    assert report["copies"][THERE]["outcome"] == _mod().COPIED


def test_the_propagated_bytes_are_the_ones_write_unit_would_have_written(tmp_path):
    """ONE SERIALISER. What lands in a sibling has to be byte-for-byte what `write_unit` lays down
    for the same entry -- otherwise that repo's `read()` and ours answer differently about one unit
    for no reason but spelling, and the "does it already say exactly this?" comparison never
    settles, rewriting the file on every pick forever."""
    sdlc = _sdlc(tmp_path)
    _seed(sdlc)
    _grant(sdlc)
    run = _gh()
    _propagate(sdlc, run=run)
    sent = _put_text(run)
    written = _registry().write_unit(tmp_path / "elsewhere", UNIT,
                                     json.loads(sent)["features"][UNIT])
    assert written.read_text(encoding="utf-8") == sent


def test_a_grant_is_not_carried_between_repos_even_for_a_third_repos_key(tmp_path):
    """`authorized` is never propagated IN EITHER DIRECTION, and the laundering path is why. Copying
    `org/here: authorized true` into org/there looks harmless -- it grants nothing there -- but
    org/there's copy is itself propagated back, and a `merge_entry` that filled an absent grant would
    hand org/here a grant nobody in org/here ever gave. The grant lives where it was given."""
    sdlc = _sdlc(tmp_path)
    _seed(sdlc)
    _grant(sdlc)
    run = _gh()
    _propagate(sdlc, run=run)
    sent = _put_document(run)["features"][UNIT]
    assert sent["repos"][HERE]["authorized"] is False


def test_a_sibling_holding_goals_we_never_saw_keeps_every_one_of_them(tmp_path):
    """THE ERASURE CASE, from the remote side. That repo recorded goal 999 itself and has not
    propagated it back yet. Copying our entry over theirs would delete it -- a half entry written
    into somebody else's repo, which is worse than not writing at all."""
    sdlc = _sdlc(tmp_path)
    mine = _entry()
    mine["repos"][HERE]["goals"] = [2871, 2879]          # the goal this pick just recorded
    _seed(sdlc, entry=mine)
    _grant(sdlc)
    theirs = _entry()
    theirs["repos"][THERE]["goals"] = [456, 999]         # a goal THAT repo recorded itself
    run = _gh(remote={THERE: _doc_bytes(UNIT, theirs)})
    _propagate(sdlc, run=run)
    sent = _put_document(run)["features"][UNIT]
    assert sent["repos"][THERE]["goals"] == [456, 999]
    assert sent["repos"][HERE]["goals"] == [2871, 2879]


def test_a_shard_written_on_the_pick_path_never_erases_the_sheets_other_repos(tmp_path):
    """THE ERASURE CASE, from the local side, and it is the one `write_unit` documents as the
    caller's non-optional obligation. The chart sheet knows both repos; only a shard is written;
    `read()` REPLACES a unit's entry with its shard. A pick that wrote what it knew -- its own repo
    -- would silently drop the sibling, and the loss becomes permanent at the next fold."""
    sdlc = _sdlc(tmp_path)
    _registry().write_index(sdlc / "features", {UNIT: _entry()})
    assert not _registry().units_dir(sdlc / "features").exists()
    _sync().sync_at_pick(str(sdlc), CONFIG, "2879", UNIT,
                         run=_sheet_runner(), cwd=str(tmp_path))
    after = _registry().read(sdlc / "features")[UNIT]
    assert sorted(after["repos"]) == sorted([HERE, THERE])
    assert after["repos"][THERE]["goals"] == [456]
    assert after["repos"][HERE]["goals"] == [2871, 2879]


def _sheet_runner(branches=("feature/int-contract",), url="git@github.com:org/here.git"):
    def run(cwd, argv):
        line = " ".join(str(a) for a in argv)
        if "ls-remote" in line:
            return "\n".join("%040d\trefs/heads/%s" % (i, b) for i, b in enumerate(branches))
        if "remote get-url" in line:
            return url
        return ""
    return run


def test_the_merge_never_overwrites_a_value_the_destination_already_states(tmp_path):
    """§12.2's ruling, applied to a foreign repo: propagation NARROWS nothing and rewrites nothing
    it did not establish. It fills gaps and unions goals; a title, a branch or an owner the
    destination already states is theirs."""
    m = _mod()
    theirs = {"title": "their title", "owner": "@theirs", "open": False,
              "repos": {THERE: {"branch": "feature/theirs", "owner": "@there-owner",
                                "goals": [456]}}}
    merged = m.merge_entry(theirs, _entry())
    assert merged["title"] == "their title"
    assert merged["owner"] == "@theirs"
    assert merged["open"] is False
    assert merged["repos"][THERE]["branch"] == "feature/theirs"
    assert merged["repos"][HERE]["branch"] == "feature/int-contract"


def test_the_merge_fills_what_the_destination_leaves_empty(tmp_path):
    m = _mod()
    merged = m.merge_entry({"repos": {THERE: {"goals": [456]}}}, _entry())
    assert merged["title"] == "Manifest duration contract"
    assert merged["owner"] == "@unit-owner"
    assert merged["repos"][THERE]["branch"] == "feature/int-contract"
    assert merged["repos"][THERE]["goals"] == [456]


def test_a_closed_unit_is_neither_reopened_nor_closed_by_a_copy(tmp_path):
    """`open` is a human's decision recorded where they took it. The destination's answer stands
    whenever it HAS one -- and "has one" cannot be read off the normalised value, because
    `normalise_entry` applies the `open: true` default, so an absent key and a stated `true` look
    identical afterwards. A first copy into a repo with no file yet would then reopen a unit our own
    entry says is closed."""
    m = _mod()
    assert m.merge_entry({"open": True, "repos": {THERE: {"goals": [456]}}},
                         _entry(open=False))["open"] is True
    assert m.merge_entry({"open": False, "repos": {THERE: {"goals": [456]}}},
                         _entry(open=True))["open"] is False
    assert m.merge_entry({}, _entry(open=False))["open"] is False
    assert m.merge_entry({"repos": {THERE: {"goals": [456]}}}, _entry(open=False))["open"] is False


def test_the_destinations_own_goal_order_is_kept_first(tmp_path):
    """`feature_registry._goals` de-duplicates in FIRST-SEEN order and its docstring says the
    sequence IS pick order -- part of what the backup remembers. So the destination's own order
    survives and ours is appended; reversing it would rewrite that repo's history of the unit to
    match ours."""
    m = _mod()
    merged = m.merge_entry({"repos": {THERE: {"goals": [999]}}},
                           {"repos": {THERE: {"goals": [456, 999]}}})
    assert merged["repos"][THERE]["goals"] == [999, 456]


def test_an_authorization_grant_is_never_propagated_into_a_repo(tmp_path):
    """§7.3's grant is the BOARD OWNER's, recorded where they gave it. Copying `authorized: true`
    into a repo would let one repo's registry hand itself permission in another -- through the one
    field the registry exists to be authoritative about."""
    m = _mod()
    merged = m.merge_entry({"repos": {THERE: {"goals": [456]}}}, _entry())
    assert merged["repos"][HERE]["authorized"] is False
    assert merged["repos"][THERE]["authorized"] is False
    kept = m.merge_entry({"repos": {THERE: {"goals": [], "authorized": True}}}, _entry())
    assert kept["repos"][THERE]["authorized"] is True


def test_a_sibling_already_holding_exactly_this_is_not_rewritten(tmp_path):
    """A committed file that churns on every pick is a diff in every adopter's repo. Nothing is
    written when the destination already says exactly this."""
    sdlc = _sdlc(tmp_path)
    _seed(sdlc)
    _grant(sdlc)
    run = _gh(remote={THERE: _doc_bytes(UNIT, _entry())})
    report = _propagate(sdlc, run=run)
    assert _puts(run) == []
    assert report["copies"][THERE]["outcome"] == _mod().IDENTICAL


def test_the_repo_this_pick_is_in_is_never_a_propagation_target(tmp_path):
    sdlc = _sdlc(tmp_path)
    _seed(sdlc)
    _grant(sdlc, repos=(HERE, THERE))
    run = _gh()
    report = _propagate(sdlc, run=run)
    assert report["siblings"] == [THERE]
    assert all(("repos/" + HERE + "/contents/") not in c for c in run.calls)


# --------------------------------------------------------------------------- 2. one-directional


def test_a_goal_from_an_unlisted_repo_leaves_repos_unchanged(tmp_path):
    """THE STRUCTURAL HALF, and it sits in the registry write rather than in the label overlay: a
    `work.py start` run by hand reaches the write and never reaches the loop's gate, so the field
    the registry is authoritative about has to be defended where it is written."""
    sdlc = _sdlc(tmp_path)
    _seed(sdlc, entry={"title": "t", "owner": "@unit-owner", "open": True,
                       "repos": {THERE: {"branch": "feature/int-contract", "owner": "@there-owner",
                                         "goals": [456]}}})
    report = _sync().sync_at_pick(str(sdlc), CONFIG, "2879", UNIT,
                                  run=_sheet_runner(), cwd=str(tmp_path))
    entry = _registry().read(sdlc / "features")[UNIT]
    assert sorted(entry["repos"]) == [THERE]
    assert entry["repos"][THERE]["goals"] == [456]
    assert _sync().SCOPE_EXPANSION in [d["kind"] for d in report["divergences"]]
    assert report["recorded"] is False


def test_a_scope_expansion_reaches_the_unit_owner_through_the_ledger(tmp_path):
    sdlc = _sdlc(tmp_path, ledger_on=True)
    _seed(sdlc, entry={"title": "t", "owner": "@unit-owner", "open": True,
                       "repos": {THERE: {"branch": "feature/int-contract", "goals": [456]}}})
    _sync().sync_at_pick(str(sdlc), CONFIG, "2879", UNIT, run=_sheet_runner(), cwd=str(tmp_path))
    notes = _notes(sdlc)
    # #1574: written `@unit-owner` by the registry, STORED in the ledger's own namespace (a bare,
    # case-folded login) -- which is the only spelling every consumer of `to` can match.
    assert any(e.get("to") == "unit-owner" and HERE in (e.get("why") or "") for e in notes)


def test_a_unit_that_names_no_repos_yet_is_not_a_scope_expansion(tmp_path):
    """The first pick of a fresh unit records its repo normally. An entry naming NO repos has made
    no statement about scope; one naming `org/there` and not `org/here` has."""
    sdlc = _sdlc(tmp_path)
    _seed(sdlc, entry={"title": "t", "owner": "@unit-owner", "open": True, "repos": {}})
    report = _sync().sync_at_pick(str(sdlc), CONFIG, "2879", UNIT,
                                  run=_sheet_runner(), cwd=str(tmp_path))
    entry = _registry().read(sdlc / "features")[UNIT]
    assert entry["repos"][HERE]["goals"] == [2879]
    assert _sync().SCOPE_EXPANSION not in [d["kind"] for d in report["divergences"]]
    assert report["recorded"] is True


class _Source:
    """The label surface the gate needs, and nothing else."""

    def __init__(self, comments=(), landed=True):
        self.comments, self.landed = list(comments), landed
        self.marked, self.notes = [], []

    def mark_needs_confirmation(self, goal):
        self.marked.append(str(goal))
        return self.landed

    def note(self, goal, text):
        self.notes.append((str(goal), text))

    def fetch_comments_strict(self, goal):
        return {"comments": [{"body": b} for b in self.comments]}


def _unlisted(tmp_path, **kw):
    sdlc = _sdlc(tmp_path, **kw)
    _seed(sdlc, entry={"title": "t", "owner": "@unit-owner", "open": True,
                       "repos": {THERE: {"branch": "feature/int-contract", "goals": [456]}}})
    return sdlc


def test_a_goal_from_an_unlisted_repo_lands_inert(tmp_path):
    """`sdlc:needs-confirmation` STANDS ALONE (docs/label-model.md §2) -- the goal gives up
    membership and waits for its owner. The pick is refused, so nothing is built for it."""
    sdlc, source = _unlisted(tmp_path), _Source()
    gate = _mod().gate_at_pick(str(sdlc), source, "2879", CONFIG, UNIT, cwd=str(tmp_path))
    assert gate.proceed is False
    assert gate.outcome == _mod().EXPANSION
    assert source.marked == ["2879"]


def test_the_scope_expansion_is_flagged_on_the_issue_once(tmp_path):
    sdlc, source = _unlisted(tmp_path), _Source()
    _mod().gate_at_pick(str(sdlc), source, "2879", CONFIG, UNIT, cwd=str(tmp_path))
    assert len(source.notes) == 1
    assert _mod().SCOPE_MARKER in source.notes[0][1]
    again = _Source(comments=[source.notes[0][1]])
    _mod().gate_at_pick(str(sdlc), again, "2879", CONFIG, UNIT, cwd=str(tmp_path))
    assert again.notes == []


def test_a_listed_repo_passes_the_gate_untouched(tmp_path):
    sdlc, source = _sdlc(tmp_path), _Source()
    _seed(sdlc)
    gate = _mod().gate_at_pick(str(sdlc), source, "2879", CONFIG, UNIT, cwd=str(tmp_path))
    assert gate.proceed is True
    assert source.marked == [] and source.notes == []


def test_a_goal_declaring_no_unit_passes_the_gate_at_zero_cost(tmp_path):
    sdlc, source = _sdlc(tmp_path), _Source()
    gate = _mod().gate_at_pick(str(sdlc), source, "2879", CONFIG, None, cwd=str(tmp_path))
    assert gate.proceed is True and gate.outcome == _mod().NO_UNIT


def test_a_project_with_no_registry_passes_the_gate_at_zero_cost(tmp_path):
    sdlc, source = _sdlc(tmp_path, adopted=False), _Source()
    gate = _mod().gate_at_pick(str(sdlc), source, "2879", CONFIG, UNIT, cwd=str(tmp_path))
    assert gate.proceed is True and gate.outcome == _mod().NOT_ADOPTED


def test_a_gate_that_cannot_mark_the_issue_writes_nothing_durable(tmp_path):
    """The transient direction, and `feature_labels`' REFUSED_WRITE_FAILED rule verbatim: a write
    that did not land is not evidence of a state, so no comment and no ledger entry may claim one.
    It also bounds the noise -- the goal keeps `sdlc:goal`, so it is re-picked, and a note written
    here would be written again on every pick until the write started landing. The pick is still
    refused: the goal is not ours to widen whatever the label did."""
    sdlc, source = _unlisted(tmp_path, ledger_on=True), _Source(landed=False)
    gate = _mod().gate_at_pick(str(sdlc), source, "2879", CONFIG, UNIT, cwd=str(tmp_path))
    assert gate.proceed is False
    assert gate.marked is False
    assert source.notes == []
    assert _notes(sdlc) == []


def test_a_source_with_no_label_surface_is_a_clean_no_op(tmp_path):
    """`LocalSource` has none of these methods. A PARTIAL surface must fail as a no-op, never as an
    `AttributeError` raised inside the pick loop."""
    sdlc = _unlisted(tmp_path)
    gate = _mod().gate_at_pick(str(sdlc), object(), "2879", CONFIG, UNIT, cwd=str(tmp_path))
    assert gate.proceed is True
    assert gate.outcome == _mod().EXPANSION


def test_the_gate_never_raises(tmp_path):
    sdlc = _unlisted(tmp_path)

    class _Boom:
        def mark_needs_confirmation(self, goal):
            raise RuntimeError("boom")

        def note(self, goal, text):
            raise RuntimeError("boom")

        def fetch_comments_strict(self, goal):
            raise RuntimeError("boom")

    gate = _mod().gate_at_pick(str(sdlc), _Boom(), "2879", CONFIG, UNIT, cwd=str(tmp_path))
    assert gate.proceed is False


# --------------------------------------------------------------------------- 3. missing access


def test_a_sibling_with_no_recorded_verdict_is_ledgered_and_never_written(tmp_path):
    """NO WRITE TO A FOREIGN REPO WITHOUT A MEASURED `granted`. #1472 refuses every verdict from an
    unpinned identity, so inheriting its answer is what keeps a drifted `gh` account from ever
    committing into a stranger's repository."""
    sdlc = _sdlc(tmp_path)
    _seed(sdlc)
    run = _gh()
    report = _propagate(sdlc, run=run)
    assert run.calls == []
    assert report["copies"][THERE]["outcome"] == _mod().HELD
    assert report["copies"][THERE]["reason"] == _mod().NO_DECISION
    assert any(e.get("to") == "there-owner" for e in _notes(sdlc))


def test_a_denied_sibling_is_ledgered_to_that_repos_owner(tmp_path):
    sdlc = _sdlc(tmp_path)
    _seed(sdlc)
    _grant(sdlc, verdict="denied")
    report = _propagate(sdlc)
    assert report["copies"][THERE]["reason"] == _mod().NOT_GRANTED
    notes = _notes(sdlc)
    assert any(e.get("to") == "there-owner" and UNIT in (e.get("why") or "") for e in notes)


def test_an_unknown_verdict_is_not_a_denial_and_is_still_not_a_write(tmp_path):
    sdlc = _sdlc(tmp_path)
    _seed(sdlc)
    _grant(sdlc, verdict="unknown")
    run = _gh()
    report = _propagate(sdlc, run=run)
    assert _puts(run) == []
    assert report["copies"][THERE]["reason"] == _mod().NOT_GRANTED


def test_a_write_that_failed_is_ledgered_rather_than_dropped(tmp_path):
    sdlc = _sdlc(tmp_path)
    _seed(sdlc)
    _grant(sdlc)
    run = _gh(fail=[("-X PUT", RuntimeError("gh: Resource protected by branch protection"))])
    report = _propagate(sdlc, run=run)
    assert report["copies"][THERE]["outcome"] == _mod().HELD
    assert report["copies"][THERE]["reason"] == _mod().WRITE_FAILED
    assert any("branch protection" in (e.get("why") or "") for e in _notes(sdlc))


def test_an_unreadable_sibling_is_never_overwritten_blind(tmp_path):
    """A read that FAILED is not "the file is absent". Writing on the strength of it would replace a
    document nobody managed to look at."""
    sdlc = _sdlc(tmp_path)
    _seed(sdlc)
    _grant(sdlc)
    run = _gh(fail=[("/contents/", RuntimeError("gh: Bad gateway (HTTP 502)"))])
    report = _propagate(sdlc, run=run)
    assert _puts(run) == []
    assert report["copies"][THERE]["reason"] == _mod().UNREADABLE


def test_a_destination_record_this_version_cannot_read_is_never_clobbered(tmp_path):
    """`parse` degrades an unknown schema to "nothing readable" -- correct for a READER, and a
    licence to overwrite for nobody. Merging into that `{}` would produce our entry verbatim and
    write it over a document a FUTURE version of this tool wrote, which is the half-entry failure
    §7.1 forbids, performed on the one file that exists to survive."""
    sdlc = _sdlc(tmp_path)
    _seed(sdlc)
    _grant(sdlc)
    future = json.dumps({"schema": "sigma/features@2", "features": {UNIT: {"repos": {}}}})
    run = _gh(remote={THERE: future})
    report = _propagate(sdlc, run=run)
    assert _puts(run) == []
    assert report["copies"][THERE]["outcome"] == _mod().HELD
    assert report["copies"][THERE]["reason"] == _mod().UNREADABLE


def test_a_destination_naming_a_different_unit_at_that_path_is_never_clobbered(tmp_path):
    sdlc = _sdlc(tmp_path)
    _seed(sdlc)
    _grant(sdlc)
    run = _gh(remote={THERE: _doc_bytes("something-else", _entry())})
    report = _propagate(sdlc, run=run)
    assert _puts(run) == []
    assert report["copies"][THERE]["reason"] == _mod().UNREADABLE


def test_the_destinations_own_spelling_of_the_unit_key_is_kept(tmp_path):
    """`feature_registry._read_unit_file`'s rule, borrowed whole: two casings were never two units,
    and the FILE's own spelling is what its author wrote. A copy is not the place to correct it."""
    sdlc = _sdlc(tmp_path)
    mine = _entry()
    mine["repos"][HERE]["goals"] = [2871, 2879]
    _seed(sdlc, entry=mine)
    _grant(sdlc)
    run = _gh(remote={THERE: _doc_bytes("Int-Contract", _entry())})
    _propagate(sdlc, run=run)
    assert list(_put_document(run)["features"]) == ["Int-Contract"]


def test_an_absent_sibling_file_is_created_rather_than_read_as_a_failure(tmp_path):
    sdlc = _sdlc(tmp_path)
    _seed(sdlc)
    _grant(sdlc)
    run = _gh()
    report = _propagate(sdlc, run=run)
    assert len(_puts(run)) == 1
    assert " -f sha=" not in _puts(run)[0]
    assert report["copies"][THERE]["outcome"] == _mod().COPIED


def test_an_existing_sibling_file_is_updated_with_its_own_blob_sha(tmp_path):
    sdlc = _sdlc(tmp_path)
    _seed(sdlc)
    _grant(sdlc)
    stale = _entry()
    stale["repos"][THERE]["goals"] = []
    run = _gh(remote={THERE: _doc_bytes(UNIT, stale)}, sha="cafe1234")
    _propagate(sdlc, run=run)
    assert " -f sha=cafe1234" in _puts(run)[0]


def test_a_missing_sibling_never_blocks_the_half_that_could_be_written(tmp_path):
    """Two siblings, one reachable. The reachable one still lands."""
    third = "org/third"
    sdlc = _sdlc(tmp_path)
    entry = _entry()
    entry["repos"][third] = {"branch": "feature/int-contract", "owner": "@third-owner",
                             "authorized": False, "goals": [77]}
    _seed(sdlc, entry=entry)
    _grant(sdlc, repos=(THERE,), verdict="granted")
    run = _gh()
    report = _propagate(sdlc, run=run)
    assert report["copies"][THERE]["outcome"] == _mod().COPIED
    assert report["copies"][third]["outcome"] == _mod().HELD
    assert _put_document(run)["features"][UNIT]["repos"][third]["goals"] == [77]


def test_the_same_unreachable_sibling_is_not_raised_twice_for_one_goal(tmp_path):
    """#1472's N3, inherited: a goal is re-picked as a matter of course, and an unconditional raise
    puts the same note in front of the same owner every time."""
    sdlc = _sdlc(tmp_path)
    _seed(sdlc)
    _propagate(sdlc)
    first = len(_notes(sdlc))
    report = _propagate(sdlc)
    assert len(_notes(sdlc)) == first
    assert report["raise_suppressed"] is True


def test_a_changed_entry_is_a_new_ask_and_does_raise_again(tmp_path):
    sdlc = _sdlc(tmp_path)
    _seed(sdlc)
    _propagate(sdlc)
    first = len(_notes(sdlc))
    grown = _entry()
    grown["repos"][HERE]["goals"] = [2871, 3000]
    _seed(sdlc, entry=grown)
    _propagate(sdlc)
    assert len(_notes(sdlc)) > first


# --------------------------------------------------------------------------- 4. costs nothing


def test_a_project_with_no_registry_spends_nothing(tmp_path):
    sdlc = _sdlc(tmp_path, adopted=False)
    run = _gh()
    report = _propagate(sdlc, run=run)
    assert report["outcome"] == _mod().NOT_ADOPTED
    assert run.calls == []


def test_a_unit_naming_only_this_repo_spends_nothing(tmp_path):
    sdlc = _sdlc(tmp_path)
    _seed(sdlc, entry={"title": "t", "owner": "@unit-owner", "open": True,
                       "repos": {HERE: {"branch": "feature/int-contract", "goals": [2871]}}})
    run = _gh()
    report = _propagate(sdlc, run=run)
    assert report["outcome"] == _mod().NO_SIBLINGS
    assert run.calls == []


def test_a_goal_declaring_no_unit_spends_nothing(tmp_path):
    sdlc = _sdlc(tmp_path)
    run = _gh()
    report = _propagate(sdlc, unit=None, run=run)
    assert report["outcome"] == _mod().NO_UNIT
    assert run.calls == []


def test_propagation_never_raises(tmp_path):
    sdlc = _sdlc(tmp_path)
    _seed(sdlc)
    _grant(sdlc)
    report = _propagate(sdlc, run=_gh(fail=[("gh", RuntimeError("boom"))]))
    assert report["outcome"] in (_mod().PROPAGATED, _mod().FAILED)


def test_an_unsafe_unit_name_never_becomes_a_path_in_a_foreign_repo(tmp_path):
    m = _mod()
    with pytest.raises(ValueError):
        m.sibling_path("../../etc/passwd")
    assert m.sibling_path(UNIT) == ".sdlc/features/units/int-contract.json"


def test_two_casings_of_one_unit_address_one_shard_in_the_sibling(tmp_path):
    """#1672. The address is the sibling's, so a second spelling is a second file in a repository we
    do not own -- and the two are never reconciled, because neither repo's tooling has any reason to
    look for the other one.

    Asserted on the derived STRING for the reason `feature_sync`'s equivalent gives: this project's
    development host has a case-insensitive filesystem that would collapse the two files and hide
    the whole defect. The Linux hosts where it bites do not."""
    m = _mod()
    assert m.sibling_path("Voice") == m.sibling_path("voice") == ".sdlc/features/units/voice.json"
    assert m.sibling_path("VOICE") == m.sibling_path("vOiCe") == ".sdlc/features/units/voice.json"


def test_the_sibling_shard_filename_is_unit_paths_own_and_not_a_second_fold(tmp_path):
    """THE DELEGATION IS LOAD-BEARING, WHICH IS THE HALF A CASING ASSERTION CANNOT REACH (#1672).

    A faithful local `name.lower() + UNIT_SUFFIX` here would satisfy every other test in this file
    and still be the defect the issue names: a SECOND definition of the fold, free to drift from
    `feature_registry.unit_path` the way three previous rounds of this bug already drifted. So this
    asserts the delegate was CALLED and that its answer is what gets used -- the filename is read
    off `unit_path`'s return, not rebuilt beside it.

    The sentinel is a `PureWindowsPath` on purpose. `sibling_path` joins with an explicit `/`
    because the GitHub Contents API addresses POSIX paths and `pathlib` on Windows would hand it
    backslashes; on a POSIX host `str(shard)` and the explicit join render identically, so that
    reasoning is untestable without a path flavour that separates them."""
    m = _mod()
    seen = []

    def spy(features_dir, name):
        seen.append((str(features_dir), name))
        return pathlib.PureWindowsPath(r"C:\somewhere\else\sentinel.json")

    m.registry.unit_path = spy
    got = m.sibling_path("Voice")
    assert seen == [(str(pathlib.Path(".sdlc") / "features"), "Voice")], seen
    assert got == ".sdlc/features/units/sentinel.json", got
    assert "\\" not in got, "a Windows separator reached a GitHub Contents API path: %r" % (got,)


def test_a_mixed_case_unit_reads_and_writes_the_FOLDED_address_in_the_sibling(tmp_path):
    """THE WHOLE PASS, NOT THE HELPER (#1672). The three tests above pin `sibling_path`'s own
    return; this one pins that the pass actually ADDRESSES what that return says, so a future change
    cannot break the composition while every piece still passes its own test in isolation.

    Asserted on the request lines because `_gh` keys its fake remote by repo SLUG, not by path -- it
    answers any contents path for a repo with the same body, which is exactly why an address bug is
    invisible to every other test in this file.

    The second half is the transition this fix was accused of breaking and measured not to: the
    sibling's own `units/voice-contract.json` (which is what ITS `unit_path` writes) is what the
    folded address reads, so the goals that repo recorded for itself survive into the merged
    document. Against the unfolded address that file was never opened and those goals were lost."""
    m = _mod()
    unit = "Voice-Contract"
    repos = {HERE: {"branch": "feature/voice-contract", "owner": "@here-owner",
                    "authorized": True, "goals": [1672]},
             THERE: {"branch": "feature/voice-contract", "owner": "@there-owner",
                     "authorized": False, "goals": []}}
    theirs = _entry(repos={THERE: dict(repos[THERE], goals=[9999])})
    sdlc = _sdlc(tmp_path)
    _seed(sdlc, name=unit, entry=_entry(repos=repos))
    _grant(sdlc, unit=unit)
    run = _gh(remote={THERE: _doc_bytes(unit, theirs)})
    report = _propagate(sdlc, unit=unit, run=run)

    folded = "units/voice-contract.json"
    contents = [c for c in run.calls if "/contents/" in c]
    assert contents, run.calls
    for call in contents:
        assert folded in call, "the pass addressed an unfolded shard: %r" % (call,)
        assert "units/Voice-Contract.json" not in call, call

    assert report["copies"][THERE]["outcome"] == m.COPIED
    sent = _put_document(run)["features"]
    key = [k for k in sent if k.lower() == unit.lower()][0]
    assert sent[key]["repos"][THERE]["goals"] == [9999], "the sibling's own goals were dropped"
    assert sent[key]["repos"][HERE]["goals"] == [1672], "our own half stopped reaching them"


def test_a_sibling_unit_name_ending_in_uppercase_LOCK_still_addresses_a_file(tmp_path):
    """The fold happens AFTER the legal-name guard, and inheriting `unit_path` whole is what keeps
    that ordering from being rearranged here. `is_unit_name` rejects `voice.lock` because git
    rejects that ref and ACCEPTS `voice.LOCK` because git accepts it -- so a fold applied before the
    guard would turn an accepted name into the spelling of a rejected one."""
    m = _mod()
    assert m.sibling_path("voice.LOCK") == ".sdlc/features/units/voice.lock.json"
    with pytest.raises(ValueError):
        m.sibling_path("voice.lock")


# --------------------------------------------------------------------------- 5. mutants


def test_copying_our_entry_over_theirs_is_a_mutant_this_suite_kills(tmp_path):
    """The loosening edge: the merge replaced by a blind copy. The mutant writes a document that
    erases the goals the sibling recorded itself -- the exact half-entry §7.1 forbids."""
    sdlc = _sdlc(tmp_path)
    mine = _entry()
    mine["repos"][HERE]["goals"] = [2871, 2879]
    _seed(sdlc, entry=mine)
    _grant(sdlc)
    theirs = _entry()
    theirs["repos"][THERE]["goals"] = [456, 999]
    remote = {THERE: _doc_bytes(UNIT, theirs)}
    mutant = _mod_with("merged = merge_entry(theirs, mine)", "merged = merge_entry({}, mine)")
    run = _gh(remote=remote)
    mutant.propagate_at_pick(str(sdlc), CONFIG, "2879", UNIT, run=run, cwd=str(sdlc.parent))
    sent = json.loads(_put_text(run))
    assert sent["features"][UNIT]["repos"][THERE]["goals"] == [456]      # 999 erased: the bug
    run2 = _gh(remote=remote)
    _propagate(sdlc, run=run2)
    assert _put_document(run2)["features"][UNIT]["repos"][THERE]["goals"] == [456, 999]


def test_treating_an_unknown_verdict_as_permission_is_a_mutant_this_suite_kills(tmp_path):
    """`unknown` is not a soft `granted`. The mutant writes into a repo nobody measured."""
    sdlc = _sdlc(tmp_path)
    _seed(sdlc)
    _grant(sdlc, verdict="unknown")
    mutant = _mod_with('if verdict != GRANTED:', 'if verdict == DENIED:')
    run = _gh()
    mutant.propagate_at_pick(str(sdlc), CONFIG, "2879", UNIT, run=run, cwd=str(sdlc.parent))
    assert len(_puts(run)) == 1                                          # the write: the bug
    run2 = _gh()
    _propagate(sdlc, run=run2)
    assert _puts(run2) == []


# --------------------------------------------------------------------------- 6. the wiring


def _work_runner(sdlc, branches=("feature/int-contract",), body="Feature: int-contract",
                 labels=(), gh=None):
    """`work.start()`'s own `(cwd, argv) -> stdout` runner: the one issue read #1467 added, the
    branch measurement #1473 makes, and this goal's contents calls. Everything else is silent."""
    contents = gh or _gh()

    def run(cwd, argv):
        line = " ".join(str(a) for a in argv)
        if "/contents/" in line or "-X PUT" in line:
            return contents(cwd, argv)
        if "issues/" in line:
            return json.dumps({"number": 2879, "title": "t", "body": body,
                               "labels": [{"name": n} for n in labels]})
        if "ls-remote" in line:
            return "\n".join("%040d\trefs/heads/%s" % (i, b) for i, b in enumerate(branches))
        if "remote get-url" in line:
            return "git@github.com:org/here.git"
        run.calls.append(line)
        return ""

    run.calls = []
    run.contents = contents
    return run


def test_start_propagates_the_entry_it_has_just_recorded(tmp_path):
    """THE WIRING, END TO END, and the ORDER is the assertion. `_sync_registry` records this goal
    under the unit; propagation then copies the RESULT. Reversed, the sibling would receive an entry
    missing the very goal whose pick triggered the copy."""
    work = _load("work")
    sdlc = _sdlc(tmp_path)
    _seed(sdlc)
    _grant(sdlc)
    run = _work_runner(sdlc)
    out = work.start(str(sdlc), CONFIG, "2879", run=run)
    assert "feature/int-contract" in out
    sent = _put_document(run.contents)["features"][UNIT]
    assert sent["repos"][HERE]["goals"] == [2871, 2879]
    assert sent["repos"][THERE]["goals"] == [456]


def test_the_propagation_path_adds_no_issue_read_of_its_own(tmp_path):
    """The same requirement `feature_sync` carries, on the same pick: `work.start()` receives only a
    goal id, #1467 added exactly ONE `gh api repos/<slug>/issues/<n>`, and this must not be a
    second. The unit is handed in, so there is no code path here that could make the read."""
    work = _load("work")
    sdlc = _sdlc(tmp_path)
    _seed(sdlc)
    _grant(sdlc)
    seen = []
    run = _work_runner(sdlc)
    real = run

    def counting(cwd, argv):
        line = " ".join(str(a) for a in argv)
        if "issues/" in line:
            seen.append(line)
        return real(cwd, argv)

    work.start(str(sdlc), CONFIG, "2879", run=counting)
    assert seen == ["gh api repos/org/here/issues/2879"]


def test_start_surfaces_a_held_sibling_on_its_own_result_line(tmp_path):
    """`report["note"]` with no consumer is decoration. A half of this unit that now needs a person
    belongs on the line the operator is already reading."""
    work = _load("work")
    sdlc = _sdlc(tmp_path)
    _seed(sdlc)
    out = work.start(str(sdlc), CONFIG, "2879", run=_work_runner(sdlc))
    assert "propagation:" in out and THERE in out


def test_a_scope_expansion_refuses_the_pick_in_the_loop(tmp_path, monkeypatch):
    """`loop._next`'s wiring: the goal is never claimed, never marked in progress, and comes back
    inert. Asserted through the real `_next`, because the gate is only worth anything at the point
    where it is actually consulted."""
    loop = _load("loop")
    sdlc = _unlisted(tmp_path)
    source = _Source()
    source.next_pending = lambda skip=(): None if skip else "2879"
    source.mark_in_progress = lambda goal: pytest.fail("the goal was claimed anyway")
    monkeypatch.setattr(loop, "feature_labels", types.SimpleNamespace(
        attach_at_pick=lambda *a, **k: types.SimpleNamespace(proceed=True, unit=UNIT)))
    monkeypatch.setattr(loop, "_reconcile_sweep", lambda *a, **k: None)
    monkeypatch.setattr(loop, "_auto_unpark_sweep", lambda *a, **k: set())
    monkeypatch.setattr(loop, "_auto_reclaim_stale_claims", lambda *a, **k: set())
    monkeypatch.setattr(loop, "_emit_run_stop_once", lambda *a, **k: None)
    kind, goal = loop._next(str(sdlc), source, json.loads(
        (sdlc / "config.json").read_text(encoding="utf-8")))
    assert (kind, goal) == ("DONE", None)
    assert source.marked == ["2879"]


def test_a_loop_whose_gate_cannot_load_still_picks(tmp_path, monkeypatch):
    """FAIL-OPEN, and it costs nothing: `feature_sync._record_goal` refuses to widen `repos`
    whatever reaches it, so a gate that cannot answer changes nothing about the registry and must
    not stop a queue."""
    loop = _load("loop")
    sdlc = _unlisted(tmp_path)
    monkeypatch.setattr(loop, "_feature_propagate", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert loop._scope_ok_at_pick(str(sdlc), _Source(), "2879", CONFIG, UNIT) is True


# --------------------------------------------------------------------------- 7. the contracts


def test_the_verdict_vocabulary_is_the_one_cross_repo_measures(tmp_path):
    """BORROWED BY VALUE, NOT BY IMPORT (`cross_repo` loads `work`, `work` loads this module, and
    `_load` has no cycle-breaking cache) -- so the agreement is pinned by a test instead. A drift
    here would make every sibling look unmeasured, which holds everything and writes nothing: the
    safe direction, and still wrong."""
    m, cross = _mod(), _load("cross_repo")
    assert (m.GRANTED, m.DENIED, m.UNKNOWN) == (cross.GRANTED, cross.DENIED, cross.UNKNOWN)
    assert m.SCHEMA != cross.RECORD_SCHEMA          # two records, two schemas, never one file


def test_a_landing_record_in_a_schema_this_code_cannot_read_grants_nothing(tmp_path):
    sdlc = _sdlc(tmp_path)
    _seed(sdlc)
    path = _grant(sdlc)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema"] = "sigma/landing@2"
    path.write_text(json.dumps(payload), encoding="utf-8")
    run = _gh()
    report = _propagate(sdlc, run=run)
    assert _puts(run) == []
    assert report["copies"][THERE]["reason"] == _mod().NO_DECISION


def test_needs_confirmation_gives_up_membership_and_stands_alone(tmp_path):
    """`docs/label-model.md` §2: `sdlc:needs-confirmation` STANDS ALONE. One atomic swap -- two hand
    edits can leave an issue carrying two membership labels at once -- and the board move follows the
    label write in both directions, so a card never claims a transition the labels did not make."""
    gh = _load("sources").GitHubSource({"discovery": {"source": "github",
                                                      "github": {"repo": "o/r"}}},
                                       run=lambda args: "")
    swaps, moves = [], []
    gh._ensure_labels = lambda: None
    gh._swap_labels_best_effort = lambda goal, add=(), remove=(), what="": (
        swaps.append((sorted(add), sorted(remove))) or True)
    gh._set_board_status = lambda goal, status: moves.append(status)
    assert gh.mark_needs_confirmation("42") is True
    assert swaps == [(["sdlc:needs-confirmation"],
                      sorted(["sdlc:goal", "sdlc:in-progress", "sdlc:blocked"]))]
    assert moves == ["Backlog"]
    assert "sdlc:parked" not in swaps[0][1]         # a park is a human's, never ours to clear

    gh2 = _load("sources").GitHubSource({"discovery": {"source": "github",
                                                       "github": {"repo": "o/r"}}},
                                        run=lambda args: "")
    later = []
    gh2._ensure_labels = lambda: None
    gh2._swap_labels_best_effort = lambda *a, **k: False
    gh2._set_board_status = lambda goal, status: later.append(status)
    assert gh2.mark_needs_confirmation("42") is False
    assert later == []


# --------------------------------------------------------------------------- 8. the review round


def _puts_to(run, repo):
    """Every PUT aimed at `repo`, read off the EMITTED ARGV. The assertions below are on the argv
    and never on an outcome string, because in the bug this pins the outcome was `copied` and read
    as a success -- the only thing that said otherwise was which repo the call named."""
    return [c for c in _puts(run) if ("repos/%s/contents/" % repo) in c]


def test_an_unresolvable_slug_never_puts_into_our_own_repo(tmp_path):
    """F1. `repo_slug` returns None when `discovery.github.repo` is not a slug AND `git remote
    get-url` fails. `r != None` is true of every key, so the repo this pick is STANDING IN joined
    its own sibling list and was committed into -- on its default branch, through the Contents API,
    outside the PR flow. The grant gate does not save it: `cross_repo` measures every repo the entry
    names, ours included, so `granted` for ourselves is the ordinary recorded state."""
    config = {"work": {"enabled": True, "remote": "origin"},
              "discovery": {"source": "github", "github": {"repo": ""}}}
    sdlc = _sdlc(tmp_path, config=config)
    _seed(sdlc)
    _grant(sdlc, repos=(HERE, THERE))

    def no_remote(cwd, argv):
        line = " ".join(str(a) for a in argv)
        if "remote get-url" in line:
            raise RuntimeError("fatal: No such remote 'origin'")
        return _gh()(cwd, argv)

    calls = []
    run = lambda cwd, argv: (calls.append(" ".join(str(a) for a in argv)), no_remote(cwd, argv))[1]
    run.calls = calls
    report = _propagate(sdlc, run=run, config=config)
    assert _puts(run) == []                                  # nothing anywhere, on the argv
    assert _puts_to(run, HERE) == []                          # and above all not into ourselves
    assert report["outcome"] == _mod().NO_SLUG
    assert report["repo"] is None
    assert sorted(report["copies"]) == sorted([HERE, THERE])
    assert all(c["reason"] == _mod().NO_SLUG for c in report["copies"].values())
    assert "no-slug" in report["note"]


def test_an_unresolvable_slug_tells_the_operator_and_not_every_repos_owner(tmp_path, capsys):
    """The split `feature_sync` already draws for `NO_REPO`: a finding about THIS checkout's own
    configuration goes to whoever is running the pick. Ledgering it would put "set
    discovery.github.repo" in front of the owners of every repo the unit names, none of whom can."""
    config = {"work": {"enabled": True, "remote": "origin"},
              "discovery": {"source": "github", "github": {"repo": ""}}}
    sdlc = _sdlc(tmp_path, config=config)
    _seed(sdlc)
    run = _gh(fail=[("remote get-url", RuntimeError("fatal: No such remote"))])
    _propagate(sdlc, run=run, config=config)
    assert _notes(sdlc) == []
    assert "discovery.github.repo" in capsys.readouterr().err


def test_a_case_mismatched_slug_is_the_same_repo_everywhere(tmp_path):
    """F2. GitHub `owner/name` is case-insensitively unique, and this PR is what makes it bite: the
    machine no longer widens `repos`, so every key after the first is HUMAN-TYPED and then
    exact-matched against a machine-derived slug. Before the fold, `Org/Here` vs `org/here` refused
    the pick, stripped `sdlc:goal` from every goal in that repo -- and put our own repo back into
    `siblings`, which fires F1's PUT."""
    config = {"work": {"enabled": True, "remote": "origin"},
              "discovery": {"source": "github", "github": {"repo": "Org/Here"}}}
    sdlc = _sdlc(tmp_path, config=config)
    _seed(sdlc)
    _grant(sdlc, repos=(THERE,))
    run = _gh()
    report = _propagate(sdlc, run=run, config=config)
    assert _puts_to(run, HERE) == []                          # argv-level: never into ourselves
    assert _puts_to(run, "Org/Here") == []
    assert report["siblings"] == [THERE]
    assert len(_puts_to(run, THERE)) == 1

    source = _Source()
    gate = _mod().gate_at_pick(str(sdlc), source, "2879", config, UNIT, cwd=str(tmp_path))
    assert gate.proceed is True                               # not a scope expansion
    assert source.marked == []

    report = _sync().sync_at_pick(str(sdlc), config, "2879", UNIT,
                                  run=_sheet_runner(), cwd=str(tmp_path))
    entry = _registry().read(sdlc / "features")[UNIT]
    assert sorted(entry["repos"]) == sorted([HERE, THERE])     # no second casing minted
    assert entry["repos"][HERE]["goals"] == [2871, 2879]
    assert report["recorded"] is True


def test_a_case_mismatched_destination_key_is_not_duplicated_either(tmp_path):
    """The same ruling on the write into a foreign file: `merge_entry` lands on the key the
    destination already uses rather than adding ours beside it."""
    m = _mod()
    merged = m.merge_entry({"repos": {"Org/There": {"goals": [456]}}}, _entry())
    assert sorted(merged["repos"]) == sorted([HERE, "Org/There"])
    assert merged["repos"]["Org/There"]["goals"] == [456]


def test_the_gate_note_is_not_repeated_on_a_re_pick(tmp_path):
    """F5. Measured before the fix: 1/2/3 ledger notes over three refused picks, while the comment
    was correctly idempotent. Re-picks are ordinary -- a human running `/sigma-promote` before
    editing `repos` is exactly the order the flag comment invites."""
    sdlc = _unlisted(tmp_path, ledger_on=True)
    seen = []
    for _ in range(3):
        source = _Source(comments=list(seen))
        _mod().gate_at_pick(str(sdlc), source, "2879", CONFIG, UNIT, cwd=str(tmp_path))
        seen += [t for _g, t in source.notes]
    assert len(seen) == 1
    assert len(_notes(sdlc)) == 1


def test_the_gate_note_and_the_flag_comment_land_together_or_not_at_all(tmp_path):
    """The watermark is the comment, so an unreadable timeline makes the note LATE rather than
    duplicated -- `feature_labels._flag`'s own trade. The LABEL still lands, so the goal is inert
    whichever way the two messages went."""
    sdlc = _unlisted(tmp_path, ledger_on=True)

    class _Blind(_Source):
        def fetch_comments_strict(self, goal):
            raise RuntimeError("gh: Bad gateway (HTTP 502)")

    source = _Blind()
    gate = _mod().gate_at_pick(str(sdlc), source, "2879", CONFIG, UNIT, cwd=str(tmp_path))
    assert gate.proceed is False and gate.marked is True
    assert source.notes == [] and _notes(sdlc) == []
    ok = _Source()
    _mod().gate_at_pick(str(sdlc), ok, "2879", CONFIG, UNIT, cwd=str(tmp_path))
    assert len(ok.notes) == 1 and len(_notes(sdlc)) == 1


def test_a_second_unit_key_in_the_destinations_file_is_not_deleted(tmp_path):
    """F7. That key is malformed by `_read_unit_file`'s own rule and nothing READABLE is lost by
    dropping it -- but an unannounced deletion in a tree we do not own is exactly what this module's
    stated rule refuses. Rebuilding around it costs nothing."""
    sdlc = _sdlc(tmp_path)
    mine = _entry()
    mine["repos"][HERE]["goals"] = [2871, 2879]
    _seed(sdlc, entry=mine)
    _grant(sdlc)
    theirs = {"schema": _registry().SCHEMA,
              "features": {UNIT: _entry(), "sibling-unit": _entry(title="somebody else's")}}
    run = _gh(remote={THERE: json.dumps(theirs)})
    _propagate(sdlc, run=run)
    sent = _put_document(run)["features"]
    assert sorted(sent) == sorted([UNIT, "sibling-unit"])
    assert sent["sibling-unit"]["title"] == "somebody else's"


def test_a_field_added_inside_at1_is_dropped_by_a_copy(tmp_path):
    """F6, pinned so the limit is KNOWN rather than discovered. `parse` -> `normalise_entry` is a
    whitelist, so the "never clobbered" refusal covers a schema-STRING bump and nothing narrower: a
    field a newer Sigma adds inside `sigma/features@1` does not survive the round trip -- THIS
    module is where that costs most, because the round trip is over somebody else's file, so an older
    peer erases the field rather than merely failing to read it.

    The consequence is recorded beside `feature_registry.SCHEMA`, and #2261 (B-1 of
    `.sdlc/design/2253.md`) is where it was accepted rather than avoided: `priority` was added inside
    `@1` on the team's call, so this test's key list grows by one and the limit it pins is unchanged.
    An addition still has to answer which loss is worse for that field -- silently missing on old
    installs, or an old install refusing the whole registry."""
    sdlc = _sdlc(tmp_path)
    mine = _entry()
    mine["repos"][HERE]["goals"] = [2871, 2879]
    _seed(sdlc, entry=mine)
    _grant(sdlc)
    future = _entry()
    future["milestone"] = "v2"
    future["repos"][THERE]["reviewer"] = "@someone"
    run = _gh(remote={THERE: _doc_bytes(UNIT, future)})
    _propagate(sdlc, run=run)
    sent = _put_document(run)["features"][UNIT]
    assert "milestone" not in sent
    assert "reviewer" not in sent["repos"][THERE]
    assert sorted(sent) == ["open", "owner", "parent", "priority", "repos", "title",
                            "tracking_issue"]


def test_the_promoter_named_in_the_residue_is_the_one_that_writes_the_label(tmp_path):
    """F3. Both the docstring and `docs/label-model.md` used to name `sources._promote_blockers`,
    which is #900's PRIORITY promotion and writes no lifecycle label. The real promoter is
    `blockers.classify`'s `proposed_label` arm. Asserted against the code rather than by reading it,
    so the pointer in the contract document cannot rot silently."""
    blockers = _load("blockers")
    state = {"labels": ["sdlc:needs-confirmation", "sdlc:followup"], "assignees": []}
    assert blockers.classify(state, "sdlc:goal", "sdlc:needs-confirmation", "sdlc:parked",
                             "sdlc:followup", "me") == blockers.ROUTED   # slice 18: the proposal arm is gone
    src = (SCRIPTS / "feature_propagate.py").read_text(encoding="utf-8")
    doc = (ROOT / "docs" / "label-model.md").read_text(encoding="utf-8")
    for text in (src, doc):
        assert "blockers.classify" in text
        assert "sources._promote_blockers" not in text or "is NOT the promoter" in text or \
            "not** `sources._promote_blockers`" in text


# ------------ #1568: no local entry proceeds, and the arm that changed that was withdrawn
#
# The header used to read '"no local entry" is not evidence that the unit is new' — the
# thesis of the fix, not of the code. #1645 withdrew that fix: §14 pushes the branch first and
# declares the unit last, so "live branch, empty registry" is the state of every brand-new
# unit's first pick, and the arm refused every first adoption. #1568 is reopened and these
# tests pin the behaviour that must hold while it is.


def test_the_first_pick_of_a_brand_new_unit_is_never_refused(tmp_path):
    """#1568's fix was WITHDRAWN and this test is why. It asked the remote whether the unit's branch
    already existed, treating a live branch plus an empty local registry as an expansion.

    That state is exactly what this project's own documented adoption order produces. §14 pushes the
    branch FIRST (step 1), creates the registry THIRD (step 3), and declares the unit LAST (step 4) --
    so on the first pick of a brand-new unit the branch is live, the registry is adopted, and no entry
    names the unit. Measured before the withdrawal: that pick had `sdlc:goal` stripped by
    `mark_needs_confirmation` and was set aside, so following the documentation refused its own first
    adoption.

    No local evidence separates "this unit is new" from "this unit lives elsewhere and does not list
    us": both show an empty registry and a live branch. A gate that blocks every adoption is strictly
    worse than the gate that could not fire, which is what #1568 originally reported -- so the
    reported bug stands, reopened, and this pins the behaviour that must not regress while it does."""
    sdlc, source = _sdlc(tmp_path), _Source()
    # no seed: the registry is adopted and empty, and the branch is already on the remote (§14 step 1)
    gate = _mod().gate_at_pick(str(sdlc), source, "2879", CONFIG, UNIT,
                               run=_sheet_runner(branches=("feature/" + UNIT,)), cwd=str(tmp_path))
    assert gate.proceed is True, "the documented adoption path must not refuse its own first pick"
    assert gate.outcome == _mod().NO_ENTRY
    assert source.marked == [], "nothing may strip sdlc:goal from a unit's first goal"


def test_the_no_entry_path_asks_the_remote_and_git_nothing_at_all(tmp_path):
    """WHAT THE WITHDRAWAL ACTUALLY REMOVED, counted rather than described.

    The arm's cost was one `ls-remote` per considered goal, on the path taken by every first pick of
    every new unit — and §6e's cost table does not price it, because it is not supposed to exist.
    Asserting the verdict alone would go green again the moment somebody reinstated the arm and
    made it fail open; asserting the CALL LIST is empty is what makes reinstating it visible."""
    calls = []

    def recording(cwd, argv):
        calls.append(" ".join(str(a) for a in argv))
        return ""

    sdlc, source = _sdlc(tmp_path), _Source()
    gate = _mod().gate_at_pick(str(sdlc), source, "2879", CONFIG, UNIT,
                               run=recording, cwd=str(tmp_path))
    assert gate.outcome == _mod().NO_ENTRY and gate.proceed is True
    assert calls == [], "the no-entry arm reaches for the remote again (#1568/#1645): %r" % (calls,)
    assert source.marked == [], "nothing is written on the create path"


def test_an_offline_laptop_cannot_change_the_no_entry_verdict(tmp_path):
    """The property the test above buys, stated as the outcome an adopter cares about: a laptop with
    no network still gets its first pick.

    THIS TEST IS VACUOUS BY CONSTRUCTION, AND THAT IS THE POINT -- `broken` raises on everything and
    is never called at all, because the verdict is decided before any runner is reached. It was NOT
    vacuous while the arm existed: it then exercised a real `ls-remote` failure and a fail-open
    branch. Kept, paired with the call-count test above so the vacuity is asserted rather than
    assumed, because the failure it guards against is the one an offline adopter meets first."""
    def broken(cwd, argv):
        raise OSError("no network, and no local git either")

    sdlc, source = _sdlc(tmp_path), _Source()
    gate = _mod().gate_at_pick(str(sdlc), source, "2879", CONFIG, UNIT, run=broken, cwd=str(tmp_path))
    assert gate.proceed is True, "not knowing must never be a refusal"
    assert gate.outcome == _mod().NO_ENTRY
    assert source.marked == []
