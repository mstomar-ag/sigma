"""feature_owner.py -- who may file work against a unit (#1479, epic #1464, story #1427).

#1469 recorded `authorized`. #1477 stopped a pick WIDENING a unit into a repo its owner never
named. Neither of them ever refused anything on the strength of the grant: `is_authorized` was
defined, rendered and called by nothing. This is the level that calls it.

THE FOUR PROPERTIES, AND EACH IS A SEPARATE CLASS OF TEST HERE:

  1. AN UNAUTHORISED DIRECT FILING LANDS INERT, AND THE OWNER IS TOLD. Both halves are asserted
     separately and on the RECORDED CALLS -- the labels handed to `create_dependency`, and the
     ledger entry's own `to` field -- never on an outcome string, because an outcome string is the
     one thing a broken gate can still get right.
  2. `authorized: true` MAKES EVERY ISSUE UNDER THAT UNIT DIRECTLY ACTIONABLE, follow-ups included.
     Asserted over a SECOND filing, because "per unit, not per issue" is a claim about the second
     one.
  3. ABSENT MEANS FALSE, and so does the string `"false"`, and so does the string `"true"`.
  4. OWNERSHIP IS INFERRED AT MOST ONCE. Asserted by inferring, then trying again as somebody else.

WRITTEN AGAINST A MUTATION RUN, inheriting `tests/test_feature_propagate.py`'s discipline whole:
`_mod_with` asserts its target exists and replaces EVERY occurrence, and every mutation test asserts
the mutant's BEHAVIOUR differs on a concrete input rather than merely that the suite went red.
"""
import importlib.util
import json
import pathlib
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"
P = SCRIPTS / "feature_owner.py"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _mod():
    return _load("feature_owner")


def _registry():
    return _load("feature_registry")


def _ledger():
    return _load("ledger")


def _mod_with(old, new):
    """The module rebuilt with a source substitution applied. Both of
    `tests/test_feature_registry.py::_mod_with`'s guards, for both of its reasons."""
    src = P.read_text(encoding="utf-8")
    assert old in src, "mutation target has drifted out of the source: %r" % (old,)
    namespace = {"__name__": "feature_owner_variant", "__file__": str(P)}
    exec(compile(src.replace(old, new), str(P), "exec"), namespace)          # noqa: S102 - test-only
    return types.SimpleNamespace(**namespace)


HERE = "org/here"
THERE = "org/there"
UNIT = "int-contract"
BOARD = "@here-owner"
UNIT_OWNER = "@unit-owner"
CONFIG = {"work": {"enabled": True, "remote": "origin"},
          "discovery": {"source": "github", "github": {"repo": HERE}},
          "ledger": {"enabled": True, "actor": "stranger"},
          # these tests pin the pre-triage proposal label path; the triage path is in test_handoff (#1003)
          "ai_filed": {"triage": {"enabled": False}}}


def _entry(**over):
    base = {"title": "Manifest duration contract", "owner": UNIT_OWNER, "open": True,
            "parent": None, "tracking_issue": None,
            "repos": {HERE: {"branch": "feature/int-contract", "owner": BOARD,
                             "authorized": False, "goals": [2871]}}}
    base.update(over)
    return base


def _sdlc(tmp_path, adopted=True, ledger_on=True, actor="stranger", config=None):
    sdlc = tmp_path / ".sdlc"
    (sdlc / "state").mkdir(parents=True, exist_ok=True)
    sdlc.joinpath("config.json").write_text(
        json.dumps(dict(config or CONFIG, ledger={"enabled": bool(ledger_on), "actor": actor})),
        encoding="utf-8")
    if adopted:
        (sdlc / "features").mkdir(parents=True, exist_ok=True)
    return sdlc


def _seed(sdlc, name=UNIT, entry=None):
    return _registry().write_unit(sdlc / "features", name, entry if entry is not None else _entry())


def _notes(sdlc):
    return [e for e in _ledger().read_all(str(sdlc)) if e.get("kind") == "note"]


def _reaches(sdlc, who):
    """How many notes the person whose gh login is `who` ACTUALLY RETRIEVES. The behaviour, not the
    string: `ledger.addressed_to` is what `ledger.mine` and `autowatch._find_candidate` both call,
    so this is the only assertion that distinguishes "addressed" from "delivered"."""
    return len(_ledger().addressed_to(_notes(sdlc), who))


def _gh(login="stranger"):
    """The `(argv) -> stdout` gh runner `ledger.actor` and `feature_owner.whoami` both take.
    Injected in every filing test, so no test resolves a real GitHub identity."""
    return lambda argv: login


# --------------------------------------------------------------------------- the policy, alone


def test_the_board_owner_may_file_directly():
    v = _mod().may_file(_entry(), HERE, BOARD)
    assert v.allowed is True and v.reason == _mod().BOARD_OWNER


def test_a_stranger_may_not_and_the_ask_is_addressed_to_the_board_owner():
    v = _mod().may_file(_entry(), HERE, "@nobody")
    assert v.allowed is False and v.reason == _mod().NOT_AN_OWNER
    assert v.owner == BOARD


def test_where_the_two_owners_disagree_the_board_owner_wins():
    """The unit's own owner, filing on a board somebody else owns. A unit owner deciding the unit
    touches another component is a scoping call; putting a PICKABLE issue on that board is handing
    somebody work they never agreed to."""
    v = _mod().may_file(_entry(), HERE, UNIT_OWNER)
    assert v.allowed is False and v.reason == _mod().BOARD_OWNER_WINS
    assert v.owner == BOARD


def test_the_unit_owner_may_file_where_no_board_owner_is_recorded():
    entry = _entry()
    entry["repos"][HERE]["owner"] = None
    v = _mod().may_file(entry, HERE, UNIT_OWNER)
    assert v.allowed is True and v.reason == _mod().UNIT_OWNER


def test_an_entry_with_no_owner_at_all_refuses_nobody():
    entry = _entry(owner=None)
    entry["repos"][HERE]["owner"] = None
    v = _mod().may_file(entry, HERE, "@anyone")
    assert v.allowed is True and v.reason == _mod().NO_OWNER


def test_a_repo_that_could_not_be_named_refuses_nobody():
    """A refusal caused by OUR configuration gap is the one nobody can act on. `feature_sync` draws
    the identical line for its own `NO_REPO`: report the gap, record nothing, hold nothing."""
    v = _mod().may_file(_entry(), None, "@nobody")
    assert v.allowed is True and v.reason == _mod().NO_SLUG


def test_the_claim_writes_nothing_once_the_unit_already_has_an_owner(tmp_path):
    """The early-out, and it is what keeps the claim off every pick after a unit's first."""
    sdlc = _sdlc(tmp_path)
    _seed(sdlc)
    report = _mod().claim_at_pick(str(sdlc), "2879", UNIT, HERE, "@somebody-else")
    assert report["claimed"] == []
    assert _registry().read(sdlc / "features")[UNIT]["owner"] == UNIT_OWNER


def test_a_unit_nobody_has_recorded_refuses_nobody():
    v = _mod().may_file(None, HERE, "@anyone")
    assert v.allowed is True and v.reason == _mod().NO_ENTRY


def test_an_unknown_actor_is_not_a_refusal():
    """A gate that cannot name the creator must not refuse on their behalf -- and it is the arm the
    pick gate uses to decide whether the author is worth a network read at all."""
    v = _mod().may_file(_entry(), HERE, None)
    assert v.allowed is True and v.reason == _mod().NO_ACTOR


def test_the_at_sign_and_the_casing_are_not_two_people():
    """`docs/branching-model.md`'s own entry writes `"owner": "@unit-owner"`, `owners.parse` strips
    the `@` off every CODEOWNERS line, and GitHub logins are case-insensitively unique. Three
    spellings, one person -- comparing them raw makes one person three."""
    assert _mod().may_file(_entry(), HERE, "Here-Owner").allowed is True


def test_two_people_nobody_can_name_are_not_the_same_person():
    """The blank arm of `same_actor`, and it is the one that would silently grant: an unresolvable
    actor matching an unrecorded owner would make every gate pass on every malformed entry."""
    m = _mod()
    assert m.same_actor("  ", None) is False
    assert m.same_actor("@", "") is False
    assert m.same_actor("@amy", "AMY") is True


# --------------------------------------------------------------------------- the grant


def test_authorized_true_lets_a_stranger_file():
    entry = _entry()
    entry["repos"][HERE]["authorized"] = True
    v = _mod().may_file(entry, HERE, "@nobody")
    assert v.allowed is True and v.reason == _mod().AUTHORIZED


def test_an_absent_authorized_is_false():
    entry = _entry()
    del entry["repos"][HERE]["authorized"]
    assert _mod().may_file(entry, HERE, "@nobody").allowed is False


def test_the_string_false_does_not_grant():
    entry = _entry()
    entry["repos"][HERE]["authorized"] = "false"
    assert _mod().may_file(entry, HERE, "@nobody").allowed is False


def test_the_string_true_does_not_grant_either():
    entry = _entry()
    entry["repos"][HERE]["authorized"] = "true"
    assert _mod().may_file(entry, HERE, "@nobody").allowed is False


def test_the_grant_is_read_under_the_key_the_registry_actually_uses():
    """THE TRAP #1477 LEFT BEHIND. `feature_registry.is_authorized` does an EXACT `repos.get(repo)`,
    and #1477 then made every other slug comparison case-insensitive. Asking it with a slug spelled
    differently than the key returns False silently -- a grant that reads as ungranted."""
    entry = _entry()
    entry["repos"] = {"Org/Here": {"branch": None, "owner": BOARD, "authorized": True,
                                   "goals": []}}
    assert _registry().is_authorized(entry, HERE) is False          # the raw call: silently inert
    assert _mod().authorized(entry, HERE) is True                   # asked under the real key


def test_the_grant_is_per_repo_not_per_unit_wide():
    entry = _entry()
    entry["repos"][THERE] = {"branch": None, "owner": "@there-owner", "authorized": True,
                             "goals": []}
    assert _mod().may_file(entry, HERE, "@nobody").allowed is False
    assert _mod().may_file(entry, THERE, "@nobody").allowed is True


# --------------------------------------------------------------------------- inference


def test_ownership_is_inferred_once_and_never_re_inferred():
    entry = _registry().normalise_entry({"repos": {HERE: {"goals": []}}})
    assert _mod().claim(entry, HERE, "@first") == ["owner", "repos.%s.owner" % HERE]
    assert entry["owner"] == "@first" and entry["repos"][HERE]["owner"] == "@first"
    assert _mod().claim(entry, HERE, "@second") == []
    assert entry["owner"] == "@first" and entry["repos"][HERE]["owner"] == "@first"


def test_inference_never_creates_a_repo_the_unit_does_not_name():
    """Adding a repo is #1477's refusal, not this level's. Claiming ownership of a board the unit
    never named would widen the unit through the back door."""
    entry = _registry().normalise_entry({"owner": None, "repos": {THERE: {"goals": []}}})
    assert _mod().claim(entry, HERE, "@first") == ["owner"]
    assert HERE not in entry["repos"]


def test_a_declared_owner_is_never_overwritten_by_inference():
    entry = _registry().normalise_entry(_entry())
    assert _mod().claim(entry, HERE, "@somebody-else") == []
    assert entry["owner"] == UNIT_OWNER and entry["repos"][HERE]["owner"] == BOARD


def test_an_unnameable_actor_infers_nothing():
    entry = _registry().normalise_entry({"repos": {HERE: {"goals": []}}})
    assert _mod().claim(entry, HERE, "  ") == []
    assert entry["owner"] is None


def test_claim_at_pick_writes_the_owner_under_the_units_lock(tmp_path):
    sdlc = _sdlc(tmp_path)
    _seed(sdlc, entry={"title": "t", "owner": None, "open": True,
                       "repos": {HERE: {"branch": None, "goals": [2879]}}})
    report = _mod().claim_at_pick(str(sdlc), "2879", UNIT, HERE, "@first")
    assert report["claimed"] == ["owner", "repos.%s.owner" % HERE]
    entry = _registry().read(sdlc / "features")[UNIT]
    assert entry["owner"] == "@first" and entry["repos"][HERE]["owner"] == "@first"
    again = _mod().claim_at_pick(str(sdlc), "2880", UNIT, HERE, "@second")
    assert again["claimed"] == []
    assert _registry().read(sdlc / "features")[UNIT]["owner"] == "@first"


# --------------------------------------------------------------------------- the pick gate


class _Source:
    """The surface the pick gate needs, and nothing else."""

    def __init__(self, author="@nobody", comments=(), landed=True):
        self.author, self.comments, self.landed = author, list(comments), landed
        self.marked, self.notes, self.author_reads = [], [], []

    def fetch_author(self, goal):
        self.author_reads.append(str(goal))
        return self.author

    def mark_needs_confirmation(self, goal):
        self.marked.append(str(goal))
        return self.landed

    def note(self, goal, text):
        self.notes.append((str(goal), text))

    def fetch_comments_strict(self, goal):
        return {"comments": [{"body": b} for b in self.comments]}


def test_an_issue_a_stranger_filed_is_held_at_the_pick(tmp_path):
    sdlc, source = _sdlc(tmp_path), _Source(author="nobody")
    _seed(sdlc)
    gate = _mod().gate_at_pick(str(sdlc), source, "2879", CONFIG, UNIT, cwd=str(tmp_path))
    assert gate.proceed is False and gate.marked is True
    assert gate.outcome == _mod().NOT_AN_OWNER
    assert source.marked == ["2879"]
    assert len(source.notes) == 1 and _mod().OWNER_MARKER in source.notes[0][1]
    # THE ASK IS RETRIEVED BY THE PERSON, not merely written with their name on it. `verdict.owner`
    # is the registry's `@here-owner`; every consumer of `to` compares against a bare login.
    assert _reaches(sdlc, "here-owner") == 1
    assert _reaches(sdlc, "@here-owner") == 1        # the registry spelling reaches them too


def test_the_hold_names_the_author_beside_the_recorded_owners(tmp_path):
    """THE DIAGNOSABILITY REQUIREMENT. Both sides of the comparison are GitHub handles, and a
    project that spells one of them differently would hold every issue its real account opens. A
    comment naming only the refusal reads as "Sigma stopped"; one naming both reads as "these
    two strings are one person"."""
    sdlc, source = _sdlc(tmp_path), _Source(author="nobody")
    _seed(sdlc)
    _mod().gate_at_pick(str(sdlc), source, "2879", CONFIG, UNIT, cwd=str(tmp_path))
    body = source.notes[0][1]
    assert "nobody" in body and BOARD in body and UNIT_OWNER in body
    assert "nobody" in _notes(sdlc)[0]["why"]


def test_the_long_hold_text_is_posted_exactly_once(tmp_path):
    """THE BOUND THE OLD IDEMPOTENCY WAS PROTECTING, and it still holds. However many times a goal
    goes round this gate, the table-and-explanation body appears on the issue ONCE -- what a second
    cycle gets is a different, shorter comment about a different fact (below)."""
    sdlc, source = _sdlc(tmp_path), _Source(author="nobody")
    _seed(sdlc)
    _mod().gate_at_pick(str(sdlc), source, "2879", CONFIG, UNIT, cwd=str(tmp_path))
    first = source.notes[0][1]
    assert "Sigma has set this goal aside" in first
    timeline = [first]
    for _ in range(3):
        again = _Source(author="nobody", comments=list(timeline))
        _mod().gate_at_pick(str(sdlc), again, "2879", CONFIG, UNIT, cwd=str(tmp_path))
        timeline.append(again.notes[0][1])
    assert [t for t in timeline if "Sigma has set this goal aside" in t] == [first]


def test_a_promotion_that_did_not_clear_the_hold_is_not_silent(tmp_path):
    """#1569, AND IT IS THE HALF THAT COST THE OPERATOR A CYCLE EACH TIME. `_flag` was idempotent
    against the issue's timeline and `_tell` was gated on it, so the second refusal posted no
    comment and wrote no ledger line -- the person did the thing the comment named, the goal was
    demoted again, and nothing said so.

    THE SECOND ARRIVAL IS EVIDENCE OF A PROMOTION, which is why announcing it is a signal and not
    spam: `mark_needs_confirmation` drops `sdlc:goal` in the same atomic swap, and both queues
    refuse the result, so this gate cannot run again until somebody puts membership back."""
    sdlc, source = _sdlc(tmp_path), _Source(author="nobody")
    _seed(sdlc)
    _mod().gate_at_pick(str(sdlc), source, "2879", CONFIG, UNIT, cwd=str(tmp_path))
    assert len(source.notes) == 1 and len(_notes(sdlc)) == 1
    again = _Source(author="nobody", comments=[source.notes[0][1]])
    gate = _mod().gate_at_pick(str(sdlc), again, "2879", CONFIG, UNIT, cwd=str(tmp_path))
    assert gate.proceed is False and gate.marked is True
    assert len(again.notes) == 1                      # the issue says so
    assert len(_notes(sdlc)) == 2                     # and so does the ledger
    assert _reaches(sdlc, "here-owner") == 2          # and the owner RETRIEVES both


def test_the_second_comment_says_which_act_did_not_clear_it(tmp_path):
    """An owner reading "still held" for the second time needs to know it is the second time, and
    which gesture was the one that did not work."""
    sdlc, source = _sdlc(tmp_path), _Source(author="nobody")
    _seed(sdlc)
    _mod().gate_at_pick(str(sdlc), source, "2879", CONFIG, UNIT, cwd=str(tmp_path))
    again = _Source(author="nobody", comments=[source.notes[0][1]])
    _mod().gate_at_pick(str(sdlc), again, "2879", CONFIG, UNIT, cwd=str(tmp_path))
    body = again.notes[0][1]
    assert _mod().OWNER_MARKER in body                # a third cycle still finds a watermark
    assert "Promoted, and set aside again" in body
    assert "does not change what this gate measures" in body
    assert "repos.%s.authorized = true" % HERE in body
    assert "AGAIN" in _notes(sdlc)[1]["why"]


def test_an_issue_the_board_owner_filed_passes(tmp_path):
    sdlc, source = _sdlc(tmp_path), _Source(author="here-owner")
    _seed(sdlc)
    gate = _mod().gate_at_pick(str(sdlc), source, "2879", CONFIG, UNIT, cwd=str(tmp_path))
    assert gate.proceed is True and source.marked == [] and source.notes == []


def test_an_authorised_unit_never_even_reads_the_author(tmp_path):
    """Per unit, not per issue -- so on a granted unit the question is settled before the author
    matters, and the network read is not paid."""
    entry = _entry()
    entry["repos"][HERE]["authorized"] = True
    sdlc, source = _sdlc(tmp_path), _Source(author="nobody")
    _seed(sdlc, entry=entry)
    gate = _mod().gate_at_pick(str(sdlc), source, "2879", CONFIG, UNIT, cwd=str(tmp_path))
    assert gate.proceed is True and gate.outcome == _mod().AUTHORIZED
    assert source.author_reads == []


def test_a_unit_with_no_owner_proceeds_and_takes_its_owner_from_the_author(tmp_path):
    """THE INFERENCE, at the one point a GitHub login is already in hand. The pick still proceeds --
    a unit nobody owned refuses nobody -- and the name recorded is by construction in the same
    namespace as every comparison that will later be made against it."""
    entry = _entry(owner=None)
    entry["repos"][HERE]["owner"] = None
    sdlc, source = _sdlc(tmp_path), _Source(author="first-mover")
    _seed(sdlc, entry=entry)
    gate = _mod().gate_at_pick(str(sdlc), source, "2879", CONFIG, UNIT, cwd=str(tmp_path))
    assert gate.proceed is True and gate.outcome == _mod().NO_OWNER
    assert source.author_reads == ["2879"]
    on_disk = _registry().read(sdlc / "features")[UNIT]
    assert on_disk["owner"] == "first-mover"
    assert on_disk["repos"][HERE]["owner"] == "first-mover"
    # and it happens ONCE: a later goal by somebody else does not re-name it
    again = _Source(author="second-mover")
    _mod().gate_at_pick(str(sdlc), again, "2880", CONFIG, UNIT, cwd=str(tmp_path))
    assert _registry().read(sdlc / "features")[UNIT]["owner"] == "first-mover"
    assert again.author_reads == ["2880"]          # read, because the gate now has a decision
    assert again.marked == ["2880"]                # and it refuses: the unit has an owner now


def test_a_unit_nobody_has_recorded_is_not_named_by_this_gate(tmp_path):
    """`NO_ENTRY` returns above the inference, so the gate never CREATES a registry entry -- that is
    `sync_at_pick`'s job. The cost is one pick's delay in naming a brand-new unit's owner."""
    sdlc, source = _sdlc(tmp_path), _Source(author="first-mover")
    gate = _mod().gate_at_pick(str(sdlc), source, "2879", CONFIG, "never-seen", cwd=str(tmp_path))
    assert gate.proceed is True and gate.outcome == _mod().NO_ENTRY
    assert source.author_reads == []
    assert not (sdlc / "features" / "units").exists()


def test_an_author_that_cannot_be_read_is_not_a_refusal(tmp_path):
    class _Blind(_Source):
        def fetch_author(self, goal):
            raise RuntimeError("gh: 503")

    sdlc = _sdlc(tmp_path)
    _seed(sdlc)
    gate = _mod().gate_at_pick(str(sdlc), _Blind(), "2879", CONFIG, UNIT, cwd=str(tmp_path))
    assert gate.proceed is True and gate.outcome == _mod().NO_ACTOR


def test_a_source_with_no_surface_is_a_clean_no_op(tmp_path):
    sdlc = _sdlc(tmp_path)
    _seed(sdlc)
    gate = _mod().gate_at_pick(str(sdlc), object(), "2879", CONFIG, UNIT, cwd=str(tmp_path))
    assert gate.proceed is True and gate.outcome == _mod().NO_SURFACE


def test_a_gate_that_cannot_mark_the_issue_writes_nothing_durable(tmp_path):
    """`feature_labels`' REFUSED_WRITE_FAILED rule verbatim: a write that did not land is not
    evidence of a state, so no comment and no ledger entry may claim one."""
    sdlc, source = _sdlc(tmp_path), _Source(author="nobody", landed=False)
    _seed(sdlc)
    gate = _mod().gate_at_pick(str(sdlc), source, "2879", CONFIG, UNIT, cwd=str(tmp_path))
    assert gate.proceed is False and gate.marked is False
    assert source.notes == [] and _notes(sdlc) == []


def test_a_goal_declaring_no_unit_passes_at_zero_cost(tmp_path):
    sdlc, source = _sdlc(tmp_path), _Source()
    gate = _mod().gate_at_pick(str(sdlc), source, "2879", CONFIG, None, cwd=str(tmp_path))
    assert gate.proceed is True and gate.outcome == _mod().NO_UNIT


def test_a_project_with_no_registry_passes_at_zero_cost(tmp_path):
    sdlc, source = _sdlc(tmp_path, adopted=False), _Source()
    gate = _mod().gate_at_pick(str(sdlc), source, "2879", CONFIG, UNIT, cwd=str(tmp_path))
    assert gate.proceed is True and gate.outcome == _mod().NOT_ADOPTED


def test_the_gate_never_raises(tmp_path):
    class _Boom:
        def fetch_author(self, goal):
            raise RuntimeError("boom")

        def mark_needs_confirmation(self, goal):
            raise RuntimeError("boom")

        def note(self, goal, text):
            raise RuntimeError("boom")

        def fetch_comments_strict(self, goal):
            raise RuntimeError("boom")

    sdlc = _sdlc(tmp_path)
    _seed(sdlc)
    assert _mod().gate_at_pick(str(sdlc), _Boom(), "2879", CONFIG, UNIT,
                               cwd=str(tmp_path)).proceed is True


# --------------------------------------------------------------------------- the filing gate


class _FilingSource:
    """`create_tracked_issue`'s surface, recording what would have been sent."""

    def __init__(self, unit=UNIT, number="61"):
        self.unit, self.number = unit, number
        self.created, self.notes, self.labels_attached = [], [], []

    def fetch_body_labels(self, goal):
        return {"body": "Feature: %s\n" % self.unit if self.unit else "", "labels": []}

    def create_dependency(self, title, body, assignee, labels=(), goal_label=True):
        self.created.append({"title": title, "body": body, "labels": list(labels),
                             "goal_label": goal_label})
        return self.number

    def attach_label(self, issue, label):
        self.labels_attached.append((str(issue), label))
        return True

    def note(self, goal, text):
        self.notes.append((str(goal), text))


def _handoff():
    return _load("handoff")


def _file_one(sdlc, source, goal="2879", login=None, **over):
    kwargs = {"same_area": True, "immediately_actionable": True, "blocks_goal": False,
              "source": source, "dedup": False}
    kwargs.update(over)
    cfg = json.loads((sdlc / "config.json").read_text(encoding="utf-8"))
    kwargs.setdefault("run", _gh(login or (cfg.get("ledger") or {}).get("actor") or "stranger"))
    return _handoff().create_tracked_issue(str(sdlc), cfg, goal, "engine", "a finding", **kwargs)


def test_an_unauthorised_direct_filing_lands_inert_with_a_ledger_entry_to_the_owner(tmp_path):
    """THE DEFINITION OF DONE, asserted on the recorded call and on the ledger. `goal_label=False`
    plus `sdlc:needs-confirmation` IS "inert until promoted" -- no new machinery."""
    sdlc, source = _sdlc(tmp_path, actor="stranger"), _FilingSource()
    _seed(sdlc)
    report = _file_one(sdlc, source)
    sent = source.created[0]
    assert sent["goal_label"] is False
    assert "sdlc:needs-confirmation" in sent["labels"]
    assert report["issue"] == "61"                       # nobody was blocked from raising it
    # `create_tracked_issue` writes its own self-addressed note for a same-area filing; the
    # OWNERSHIP note is the one addressed to somebody else, and it is the one under test.
    assert _reaches(sdlc, "here-owner") == 1        # retrieved, not merely addressed
    ask = _ledger().addressed_to(_notes(sdlc), "here-owner")
    assert "stranger" in ask[0]["why"] and "61" in ask[0]["why"]   # who asked, about what


def test_an_authorised_unit_files_directly_and_so_does_its_follow_up(tmp_path):
    """PER UNIT, NOT PER ISSUE. The second filing is the one that proves it."""
    entry = _entry()
    entry["repos"][HERE]["authorized"] = True
    sdlc, source = _sdlc(tmp_path, actor="stranger"), _FilingSource()
    _seed(sdlc, entry=entry)
    _file_one(sdlc, source, goal="2879")
    _file_one(sdlc, source, goal="2880")
    for sent in source.created:
        assert sent["goal_label"] is True
        assert "sdlc:needs-confirmation" not in sent["labels"]
    assert _reaches(sdlc, "here-owner") == 0        # nobody was asked for permission
    assert _reaches(sdlc, "stranger") == 2          # only handoff's own self-addressed notes


def test_the_board_owner_filing_on_their_own_board_is_not_held(tmp_path):
    sdlc, source = _sdlc(tmp_path, actor="here-owner"), _FilingSource()
    _seed(sdlc)
    _file_one(sdlc, source)
    assert source.created[0]["goal_label"] is True
    assert "sdlc:needs-confirmation" not in source.created[0]["labels"]


def test_a_filing_from_a_goal_declaring_no_unit_is_untouched(tmp_path):
    sdlc, source = _sdlc(tmp_path, actor="stranger"), _FilingSource(unit=None)
    _seed(sdlc)
    _file_one(sdlc, source)
    assert source.created[0]["goal_label"] is True


def test_the_filing_gate_never_breaks_a_filing(tmp_path):
    """A gate that cannot answer must not cost the finding. The registry directory is replaced by a
    FILE, which is the shape that makes every read below it fail."""
    sdlc, source = _sdlc(tmp_path, adopted=False, actor="stranger"), _FilingSource()
    (sdlc / "features").write_text("not a directory", encoding="utf-8")
    report = _file_one(sdlc, source)
    assert report["issue"] == "61" and source.created[0]["goal_label"] is True


# ------------------------------------------------------- the bypass: a caller-supplied label


def test_a_caller_supplied_feature_label_is_gated_too(tmp_path):
    """THE BYPASS. `reconcile_labels` returns `unit=None` while LEAVING the caller's
    `feature:<name>` label in `labels`, so `handoff track --label feature:<x>` created an issue
    DECLARING the unit while the gate was asked about None and answered "no question arises".
    Measured on the recorded call before the fix: `goal_label=True`, no `sdlc:needs-confirmation`,
    zero warnings, nobody told."""
    sdlc, source = _sdlc(tmp_path, actor="stranger"), _FilingSource(unit=None)
    _seed(sdlc)
    _file_one(sdlc, source, extra_labels=("feature:%s" % UNIT,))
    sent = source.created[0]
    assert sent["goal_label"] is False
    assert "sdlc:needs-confirmation" in sent["labels"]
    assert "feature:%s" % UNIT in sent["labels"]     # it still declares what was asked for
    assert _reaches(sdlc, "here-owner") == 1


def test_a_rival_caller_label_is_gated_on_what_the_issue_will_declare(tmp_path):
    """The other `unit=None` return: the goal inherited unit A, the caller named unit B, the caller
    wins and B is what lands on the board. Filing into a unit the goal does not belong to is
    exactly the unilateral scoping call §12 says is not the filer's to make."""
    sdlc, source = _sdlc(tmp_path, actor="stranger"), _FilingSource(unit="some-other-unit")
    _seed(sdlc)
    _file_one(sdlc, source, extra_labels=("feature:%s" % UNIT,))
    assert source.created[0]["goal_label"] is False
    assert _reaches(sdlc, "here-owner") == 1


def test_every_declared_unit_is_asked_and_the_first_refusal_wins(tmp_path):
    """Two rival labels is a state `features.read` refuses outright, so there is no right one to
    pick -- and checking only the first would let the gate be walked past by typing a second."""
    sdlc, source = _sdlc(tmp_path, actor="stranger"), _FilingSource(unit=None)
    _seed(sdlc, name="unowned", entry={"title": "t", "owner": None, "open": True, "repos": {}})
    _seed(sdlc)
    _file_one(sdlc, source, extra_labels=("feature:unowned", "feature:%s" % UNIT))
    assert source.created[0]["goal_label"] is False


def test_declared_units_answers_what_the_issue_will_carry():
    stamp = _load("feature_stamp")
    assert stamp.declared_units(["priority:P1", "feature:billing"], None) == ["billing"]
    assert stamp.declared_units(["priority:P1", "feature:billing"], "voice") == ["voice"]
    assert stamp.declared_units(["feature:a", "feature:A", "feature:b"], None) == ["a", "b"]
    assert stamp.declared_units([], None) == []


# ------------------------------------------------------- the ask has to be RETRIEVABLE


def test_a_note_written_from_the_registry_spelling_reaches_the_login(tmp_path):
    """B2. `verdict.owner` is the registry's `@here-owner`; `ledger.addressed_to` -- which
    `ledger.mine` and `autowatch._find_candidate` both call -- matches against `ledger.actor`, a
    bare login. A notification that is written and cannot be received is worse than none."""
    sdlc, source = _sdlc(tmp_path), _Source(author="nobody")
    _seed(sdlc)
    _mod().gate_at_pick(str(sdlc), source, "2879", CONFIG, UNIT, cwd=str(tmp_path))
    assert _reaches(sdlc, "here-owner") == 1
    assert _notes(sdlc)[0]["to"] == "here-owner"     # stored in the ledger's own namespace


def test_addressed_to_retrieves_an_entry_already_on_disk_in_the_registry_spelling(tmp_path):
    """The read-side half, and why one change repairs five writers: entries written `@handle` by
    older versions of `feature_doc`/`feature_propagate`/`feature_sync`/`unit_completion` are
    already on disk, and a write-side fix alone leaves them permanently unreachable."""
    led = _ledger()
    entries = [{"kind": "note", "to": "@Here-Owner"}, {"kind": "note", "to": "someone-else"}]
    assert len(led.addressed_to(entries, "here-owner")) == 1
    assert led.addressed_to(entries, "") == []
    assert led.addressed_to([{"kind": "note", "to": None}], None) == []


# ------------------------------------------------------- the identity namespace


def test_whoami_never_invents_a_name_when_gh_cannot_answer():
    """B3. gh, or nothing -- never `$USER`, never the literal sentinel."""
    def boom(argv):
        raise RuntimeError("gh: could not authenticate")
    assert _mod().whoami(boom) == ""
    assert _mod().whoami(lambda argv: "  alice-gh \n") == "alice-gh"


def test_whoami_takes_no_config_so_a_nickname_cannot_reach_it():
    """Structural, not a promise: no configured value is more true than the authenticated account,
    so there is no parameter through which one could arrive."""
    import inspect
    assert list(inspect.signature(_mod().whoami).parameters) == ["run"]
    assert _mod().whoami(lambda argv: "alice-gh") == "alice-gh"


def test_the_sentinel_this_module_refuses_is_the_one_ledger_actor_produces(monkeypatch):
    """PINNED AGAINST THE REAL BEHAVIOUR, not against reading the source -- the same move
    `test_feature_propagate` makes for `cross_repo`'s verdict vocabulary. `ledger.actor` substitutes
    a synthetic NAME for an absent one, which defeats `actor_key`'s blank-refuses-blank invariant
    one layer up unless this module refuses that exact string."""
    led = _ledger()
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("LOGNAME", raising=False)
    led.reset_actor_cache()

    def boom(argv):
        raise RuntimeError("no gh")
    assert led.actor({}, boom) == _mod().UNRESOLVED_ACTOR
    led.reset_actor_cache()


def test_two_accounts_gh_could_not_resolve_are_not_each_other():
    m = _mod()
    assert m.actor_key(m.UNRESOLVED_ACTOR) == ""
    assert m.same_actor(m.UNRESOLVED_ACTOR, m.UNRESOLVED_ACTOR) is False
    assert m.may_file(_entry(owner=m.UNRESOLVED_ACTOR), HERE, m.UNRESOLVED_ACTOR).reason \
        != m.UNIT_OWNER


def test_an_owner_recorded_as_the_sentinel_is_no_owner():
    m = _mod()
    entry = _entry(owner=m.UNRESOLVED_ACTOR)
    entry["repos"][HERE]["owner"] = m.UNRESOLVED_ACTOR
    v = m.may_file(entry, HERE, "@anyone")
    assert v.allowed is True and v.reason == m.NO_OWNER


def test_a_gh_blip_does_not_refuse_the_owners_own_filing(tmp_path):
    """The wrong-refusal direction, on a STOCK install. `NO_ACTOR` -- "a gate that cannot say WHO
    must not refuse" -- was unreachable from the filing gate while `ledger.actor` supplied it a
    name it had invented."""
    sdlc, source = _sdlc(tmp_path, actor=""), _FilingSource()
    _seed(sdlc)

    def boom(argv):
        raise RuntimeError("gh: 503")
    _file_one(sdlc, source, run=boom)
    assert source.created[0]["goal_label"] is True
    assert "sdlc:needs-confirmation" not in source.created[0]["labels"]
    assert _reaches(sdlc, "here-owner") == 0


def test_no_owner_is_inferred_from_a_name_github_did_not_give(tmp_path):
    """The permanent-poisoning direction. Inference happens once, so a name this machine invented
    would hold every issue that person really opens, at every pick, forever. An author the source
    cannot report is no name at all."""
    class _Blind(_Source):
        def fetch_author(self, goal):
            raise RuntimeError("gh: 503")

    entry = _entry(owner=None)
    entry["repos"][HERE]["owner"] = None
    sdlc = _sdlc(tmp_path)
    _seed(sdlc, entry=entry)
    gate = _mod().gate_at_pick(str(sdlc), _Blind(), "2879", CONFIG, UNIT, cwd=str(tmp_path))
    assert gate.proceed is True
    assert _registry().read(sdlc / "features")[UNIT]["owner"] is None


def test_the_claim_refuses_a_name_it_was_not_given():
    m = _mod()
    for bad in (None, "", "   ", m.UNRESOLVED_ACTOR, 7):
        entry = _registry().normalise_entry({"repos": {HERE: {"goals": []}}})
        assert m.claim(entry, HERE, bad) == []
        assert entry["owner"] is None


# ------------------------------------------------------- what the refusal SAYS


def test_the_board_owner_wins_comment_does_not_say_they_own_neither(tmp_path):
    """B4. The unit's own owner was told they "own neither", three lines above a table naming them
    the unit owner, and then sent looking for a spelling mismatch that does not exist."""
    sdlc, source = _sdlc(tmp_path), _Source(author="unit-owner")
    _seed(sdlc)
    gate = _mod().gate_at_pick(str(sdlc), source, "2879", CONFIG, UNIT, cwd=str(tmp_path))
    assert gate.outcome == _mod().BOARD_OWNER_WINS
    body = source.notes[0][1]
    assert "owns neither" not in body
    assert "owns unit" in body and "board belongs to somebody else" in body


def test_the_report_warning_is_the_third_channel_and_says_the_same_thing(tmp_path):
    """THE THIRD CHANNEL. `_why` branched from the start, `_because` was taught, and this one was
    written out a third time and said the false sentence to the operator running the session. One
    function, three subjects."""
    entry = _entry()
    sdlc, source = _sdlc(tmp_path, actor="unit-owner"), _FilingSource()
    _seed(sdlc, entry=entry)
    report = _file_one(sdlc, source, login="unit-owner")
    said = [w for w in report["warnings"] if "as a proposal" in w]
    assert len(said) == 1
    assert "owns neither" not in said[0]
    assert "owns unit" in said[0] and "board belongs to somebody else" in said[0]


def test_only_one_place_branches_on_the_board_owner_wins_arm():
    """STRUCTURAL, so a FOURTH channel cannot be added by writing the sentence out again. The only
    `BOARD_OWNER_WINS` comparison outside the constant's own definition and `_may_file`'s decision
    is `refusal_clause`'s.

    `promote.py` IS NOW THE FOURTH CHANNEL and is held to the same rule (#1569). It renders a
    refusal for the operator running `/sigma-promote`, and the way it was allowed to do that was by
    CALLING `refusal_clause` -- which is what this module's docstring says a fourth channel may
    do -- so the constant must not appear there either."""
    src = (SCRIPTS / "feature_owner.py").read_text(encoding="utf-8")
    branches = [ln.strip() for ln in src.splitlines()
                if "BOARD_OWNER_WINS" in ln and ln.strip().startswith(("if ", "elif "))]
    assert branches == ["if verdict.reason == BOARD_OWNER_WINS:"]
    for sibling in ("handoff.py", "promote.py"):
        assert "BOARD_OWNER_WINS" not in (SCRIPTS / sibling).read_text(encoding="utf-8")
    assert "refusal_clause" in (SCRIPTS / "gate_hold.py").read_text(encoding="utf-8")   # #1005: moved from promote


# ------------------------------------------------- the remedy has to be one you can perform


def test_no_channel_offers_promote_alone_as_a_way_to_clear_the_hold(tmp_path):
    """#1569. `/sigma-promote` swaps two labels; this gate reads none of them, so the goal came back
    and the next pick set it aside again. All three channels used to name it as a remedy -- the
    issue comment offered it as the FIRST of "three ways to clear it"."""
    sdlc, source = _sdlc(tmp_path), _Source(author="nobody")
    _seed(sdlc)
    _mod().gate_at_pick(str(sdlc), source, "2879", CONFIG, UNIT, cwd=str(tmp_path))
    comment, note = source.notes[0][1], _notes(sdlc)[0]["why"]
    assert "three ways to clear it" not in comment.lower()
    assert "does not clear this hold" in comment
    assert "/sigma-promote will not" in note
    # and the remedy it DOES name is the one that changes what the gate measures
    for said in (comment, note):
        assert "repos.%s.authorized = true" % HERE in said


def test_the_remedy_survives_the_ledgers_two_hundred_character_cap(tmp_path):
    """`ledger._sanitize_free_text` caps `why` from the END, and the previous wording put the
    remedy last: a real note ran to ~247 characters, so the operator's one actionable sentence was
    exactly the part the cap ate. Asserted on what is ON DISK, not on what was passed in."""
    sdlc, source = _sdlc(tmp_path), _Source(author="nobody")
    _seed(sdlc)
    _mod().gate_at_pick(str(sdlc), source, "2879", CONFIG, UNIT, cwd=str(tmp_path))
    stored = _notes(sdlc)[0]["why"]
    assert len(stored) <= _ledger().FREE_TEXT_CAP
    assert UNIT in stored and "repos.%s.authorized = true" % HERE in stored
    assert "/sigma-promote will not" in stored


def test_the_filing_report_names_the_registry_route_too(tmp_path):
    """The third channel, and the one the operator running the session reads."""
    sdlc, source = _sdlc(tmp_path, actor="stranger"), _FilingSource()
    _seed(sdlc)
    report = _file_one(sdlc, source)
    stored = _notes(sdlc)[0]["why"]
    assert "authorized = true" in stored and "/sigma-promote will not" in stored
    assert any("as a proposal" in w for w in report["warnings"])


# ------------------------------------------------- #1575: the sibling infers its own board owner


def _propagated(**over):
    """WHAT LANDS IN A SIBLING. `feature_propagate` copies the WHOLE entry, unit owner included --
    the owner inferred in the repo it came from -- and the receiving board has nobody recorded."""
    entry = _entry(**over)
    entry["repos"][HERE]["owner"] = None
    return entry


def test_a_propagated_owner_does_not_suppress_the_siblings_own_inference(tmp_path):
    """#1575. `may_file`'s actor-free pass answers `NO_ACTOR`, not `NO_OWNER`, the moment EITHER
    owner is recorded -- and a propagated entry always records one. The arm that infers sat behind
    that answer, so the sibling never recorded a board owner of its own and every issue the foreign
    owner did not open was held, permanently, for a person in another repository."""
    sdlc, source = _sdlc(tmp_path), _Source(author="sibling-first")
    _seed(sdlc, entry=_propagated())
    gate = _mod().gate_at_pick(str(sdlc), source, "2879", CONFIG, UNIT, cwd=str(tmp_path))
    assert gate.proceed is True and gate.outcome == _mod().BOARD_OWNER
    assert source.marked == [] and source.notes == [] and _notes(sdlc) == []
    on_disk = _registry().read(sdlc / "features")[UNIT]
    assert on_disk["repos"][HERE]["owner"] == "sibling-first"
    assert on_disk["owner"] == UNIT_OWNER      # the foreign unit owner is never overwritten


def test_the_sibling_names_its_own_board_owner_once_and_then_holds_for_them(tmp_path):
    """The inference is still ONCE, and what it buys is a hold somebody local can clear: the second
    author is refused, and the ask is addressed to the sibling's own board owner rather than to the
    person in the repository the unit came from."""
    sdlc = _sdlc(tmp_path)
    _seed(sdlc, entry=_propagated())
    _mod().gate_at_pick(str(sdlc), _Source(author="sibling-first"), "2879", CONFIG, UNIT,
                        cwd=str(tmp_path))
    later = _Source(author="passer-by")
    gate = _mod().gate_at_pick(str(sdlc), later, "2880", CONFIG, UNIT, cwd=str(tmp_path))
    assert gate.proceed is False and gate.outcome == _mod().NOT_AN_OWNER
    assert _registry().read(sdlc / "features")[UNIT]["repos"][HERE]["owner"] == "sibling-first"
    assert _reaches(sdlc, "sibling-first") == 1
    assert _reaches(sdlc, "unit-owner") == 0


def test_the_propagated_unit_owner_is_recorded_as_the_board_owner_when_they_arrive_first(tmp_path):
    """The other order, and it must not double-name anybody: if the unit's own owner is the first to
    file in the sibling, they become that board's owner and the entry is coherent afterwards."""
    sdlc, source = _sdlc(tmp_path), _Source(author="unit-owner")
    _seed(sdlc, entry=_propagated())
    gate = _mod().gate_at_pick(str(sdlc), source, "2879", CONFIG, UNIT, cwd=str(tmp_path))
    assert gate.proceed is True
    on_disk = _registry().read(sdlc / "features")[UNIT]
    assert on_disk["owner"] == UNIT_OWNER and on_disk["repos"][HERE]["owner"] == "unit-owner"


def test_a_board_the_unit_does_not_list_is_never_named_by_the_sibling(tmp_path):
    """The back door #1477 closed at the front stays closed: inference fills a board the unit
    already names, and creates none."""
    sdlc, source = _sdlc(tmp_path), _Source(author="passer-by")
    entry = _entry()
    entry["repos"] = {THERE: {"branch": None, "owner": UNIT_OWNER, "authorized": False,
                              "goals": []}}
    _seed(sdlc, entry=entry)
    _mod().gate_at_pick(str(sdlc), source, "2879", CONFIG, UNIT, cwd=str(tmp_path))
    on_disk = _registry().read(sdlc / "features")[UNIT]
    assert HERE not in on_disk["repos"]
    assert _mod()._claimable_board(on_disk, HERE) is None


def test_an_empty_repos_block_names_no_board(tmp_path):
    """An empty block is the ordinary state of a freshly recorded unit; there is no board entry to
    put an owner on, and nothing is created."""
    entry = _entry()
    entry["repos"] = {}
    assert _mod()._claimable_board(entry, HERE) is None
    assert _mod()._claimable_board(None, HERE) is None


# ------------------------------------------------- the predicate `/sigma-promote` asks


def test_would_hold_answers_exactly_what_the_gate_answers(tmp_path):
    """The promote-side predicate and the pick-side gate must not be able to disagree: one refuses a
    promotion the other would honour (a dead end), or honours one the other undoes (a silent no-op).
    Asserted on the same registry, arm for arm."""
    m = _mod()
    cases = [(_entry(), "nobody", m.NOT_AN_OWNER),          # a stranger -- held
             (_entry(), "here-owner", None),                # the board owner -- not held
             (_entry(), "unit-owner", m.BOARD_OWNER_WINS),  # the restrictive arm -- held
             (_entry(), "", None),                          # no actor -- refuses nobody
             (_propagated(), "passer-by", None)]            # #1575: the pick would name the board
    for i, (entry, author, expected) in enumerate(cases):
        sdlc = _sdlc(tmp_path / str(i))
        _seed(sdlc, entry=entry)
        verdict = m.would_hold(str(sdlc), CONFIG, UNIT, author, cwd=str(tmp_path / str(i)))
        gate = m.gate_at_pick(str(sdlc), _Source(author=author), "2879", CONFIG, UNIT,
                              cwd=str(tmp_path / str(i)))
        assert (verdict.reason if verdict else None) == expected
        assert gate.proceed is (verdict is None)


def test_would_hold_refuses_nobody_where_it_cannot_answer(tmp_path):
    """FAIL-OPEN ON EVERY AXIS -- a wrong refusal inside /sigma-promote is the dead end that command
    exists to remove, while a missing one costs only what shipped before it."""
    m = _mod()
    unadopted = _sdlc(tmp_path / "u", adopted=False)
    assert m.would_hold(str(unadopted), CONFIG, UNIT, "nobody", cwd=str(tmp_path / "u")) is None
    adopted = _sdlc(tmp_path / "a")
    _seed(adopted)
    assert m.would_hold(str(adopted), CONFIG, None, "nobody", cwd=str(tmp_path / "a")) is None
    assert m.would_hold(str(adopted), CONFIG, "never-seen", "nobody",
                        cwd=str(tmp_path / "a")) is None
    assert m.would_hold(str(adopted), CONFIG, UNIT, None, cwd=str(tmp_path / "a")) is None
    assert m.would_hold(None, CONFIG, UNIT, "nobody") is None


def test_would_hold_names_the_repo_it_answered_about(tmp_path):
    """So the refusal `/sigma-promote` prints can name `repos.<repo>.authorized` without resolving
    the slug a second time -- `tell_at_filing`'s lesson, one module over."""
    sdlc = _sdlc(tmp_path)
    _seed(sdlc)
    verdict = _mod().would_hold(str(sdlc), CONFIG, UNIT, "nobody", cwd=str(tmp_path))
    assert verdict.repo == HERE and verdict.owner == BOARD


def test_every_channel_renders_the_same_clause(tmp_path):
    """The three subjects, one fact. Asserted across the ledger note, the issue comment and the
    filing report rather than on the function alone -- a shared helper nobody calls is not shared."""
    m = _mod()
    verdict = m.may_file(_entry(), HERE, UNIT_OWNER)
    assert verdict.reason == m.BOARD_OWNER_WINS
    for subject in ("dana", "this account"):
        clause = m.refusal_clause(verdict, UNIT, HERE, subject)
        assert clause.startswith(subject) and "owns unit" in clause and "board belongs" in clause
    quoted = m.refusal_clause(verdict, UNIT, HERE, "x", quote=m._code)
    assert "`%s`" % UNIT in quoted and "`%s`" % HERE in quoted


def test_the_verdict_names_the_repo_it_was_answered_about():
    """So no channel has to resolve the slug a second time -- `tell_at_filing` used to, through a
    runner it had no business choosing."""
    m = _mod()
    assert m.may_file(_entry(), HERE, "@nobody").repo == HERE
    assert m.may_file(_entry(), None, "@nobody").repo is None
    assert m.may_file(None, HERE, "@nobody").repo is None


def test_the_blocker_upgrade_warning_is_withdrawn_when_ownership_reverses_it(tmp_path):
    """#1393's warning says "filed as an actionable goal, not a proposal". Ownership then files it
    as a proposal. Two contradictory statements in one report, and the false one came first."""
    sdlc, source = _sdlc(tmp_path, actor="stranger"), _FilingSource()
    _seed(sdlc)
    report = _file_one(sdlc, source, immediately_actionable=False, blocks_goal=True)
    assert source.created[0]["goal_label"] is False
    assert not any("as an actionable goal, not a proposal" in w for w in report["warnings"])
    assert any("ALSO blocked behind it" in w for w in report["warnings"])


def test_the_blocker_upgrade_still_fires_when_ownership_permits(tmp_path):
    """The other direction: an internal incoherence is still Sigma's to resolve, and it still
    says so. Ownership must not have silently deleted a warning that is true."""
    entry = _entry()
    entry["repos"][HERE]["authorized"] = True
    sdlc, source = _sdlc(tmp_path, actor="stranger"), _FilingSource()
    _seed(sdlc, entry=entry)
    report = _file_one(sdlc, source, immediately_actionable=False, blocks_goal=True)
    assert source.created[0]["goal_label"] is True
    assert any("as an actionable goal, not a proposal" in w for w in report["warnings"])


def test_the_owner_is_named_once_not_twice(tmp_path):
    """`ownership.owner` already carries the registry's `@`; the f-string prepended another."""
    sdlc, source = _sdlc(tmp_path, actor="stranger"), _FilingSource()
    _seed(sdlc)
    report = _file_one(sdlc, source)
    said = [w for w in report["warnings"] if "has been asked through the ledger" in w]
    assert len(said) == 1 and "@@" not in said[0] and BOARD in said[0]


# ------------------------------------------------------- the runner contracts


def test_the_filing_gate_keeps_its_two_runners_apart(tmp_path):
    """TWO CONTRACTS, TWO PARAMETERS. `repo_slug` answers `(cwd, argv)`; `whoami` answers `(argv)`.
    One `run` fed to both did not bypass a caller's runner, it MISAPPLIED it -- and with
    `discovery.github.repo` unset (the real install path) each arity failure landed on ALLOW: a gh
    runner gave `no-slug`, a git runner gave `no-actor`. Here both are injected and both are used,
    so the gate reaches a real refusal."""
    sdlc = _sdlc(tmp_path, config={"work": {"remote": "origin"},
                                   "discovery": {"source": "github", "github": {"repo": ""}}})
    _seed(sdlc)
    git_calls, gh_calls = [], []

    def git(cwd, argv):
        git_calls.append(list(argv))
        return "git@github.com:%s.git" % HERE

    def gh(argv):
        gh_calls.append(list(argv))
        return "nobody"

    cfg = json.loads((sdlc / "config.json").read_text(encoding="utf-8"))
    verdict = _mod().gate_at_filing(str(sdlc), cfg, "2879", UNIT, gh_run=gh, run=git,
                                    cwd=str(tmp_path))
    assert verdict.allowed is False and verdict.reason == _mod().NOT_AN_OWNER
    assert verdict.repo == HERE
    assert git_calls == [["git", "remote", "get-url", "origin"]]     # the slug came from git
    assert gh_calls == [["api", "user", "-q", ".login"]]             # the actor came from gh


def test_the_filing_caller_names_the_gh_runner_explicitly():
    """`handoff` has only a gh runner. Passing it as `run=` would put it where `repo_slug` reads,
    which is how the misapplication happened; the parameter name is what makes it unrepresentable."""
    src = (SCRIPTS / "handoff.py").read_text(encoding="utf-8")
    assert "gate_at_filing(sdlc_dir, config, goal, unit, gh_run=run)" in src
    assert "gate_at_filing(sdlc_dir, config, goal, unit, run=run)" not in src


# ------------------------------------------------------- the not-recorded invariant


def test_a_unit_is_not_named_by_a_goal_that_would_not_be_recorded_under_it(tmp_path):
    """The invariant the claim carried on `work.start`'s path (`report["recorded"]`) and had to
    carry with it. `loop._next` short-circuits this gate behind #1477's -- but that one FAILS OPEN,
    and `_record_goal` then refuses the write anyway, so without this a unit could be named, once
    and permanently, by a goal never recorded under it."""
    entry = _entry(owner=None)
    entry["repos"] = {THERE: {"branch": None, "owner": None, "authorized": False, "goals": []}}
    sdlc, source = _sdlc(tmp_path), _Source(author="passer-by")
    _seed(sdlc, entry=entry)
    gate = _mod().gate_at_pick(str(sdlc), source, "2879", CONFIG, UNIT, cwd=str(tmp_path))
    assert gate.proceed is True
    assert _registry().read(sdlc / "features")[UNIT]["owner"] is None


def test_a_unit_that_lists_this_repo_is_still_named(tmp_path):
    """The other direction: the guard must not also stop the ordinary first naming."""
    entry = _entry(owner=None)
    entry["repos"][HERE]["owner"] = None
    sdlc, source = _sdlc(tmp_path), _Source(author="first-mover")
    _seed(sdlc, entry=entry)
    _mod().gate_at_pick(str(sdlc), source, "2879", CONFIG, UNIT, cwd=str(tmp_path))
    assert _registry().read(sdlc / "features")[UNIT]["owner"] == "first-mover"


# ------------------------------------------------------- the third exact-match site


def _sibling_gate(tmp_path, registry_repos, decision_repos, name="g"):
    """Drive `work.sibling_gate` over a hand-written landing record and registry entry.

    `run` answers `gh repo view` with this repo's slug and `gh pr list` with a valid empty array --
    NOT with garbage: an unreadable reply makes the gate wait rather than answer, and a probe that
    observes a crash observes the crash rather than the guard."""
    work, cross = _load("work"), _load("cross_repo")
    root = tmp_path / name
    sdlc = _sdlc(root)
    _seed(sdlc, entry=_entry(repos=registry_repos))
    (sdlc / "state" / "landing").mkdir(parents=True, exist_ok=True)
    (sdlc / "state" / "landing" / "2879.json").write_text(json.dumps({
        "schema": cross.RECORD_SCHEMA, "goal": "2879", "unit": UNIT, "outcome": cross.TIER_1,
        "cross_repo": True,
        "repos": {k: {"verdict": "granted"} for k in decision_repos},
    }), encoding="utf-8")
    work._save(str(sdlc), "2879", {"worktree": str(root), "branch": "sdlc/2879",
                                   "base": "feature/int-contract", "remote": "origin", "pr": ""})

    def run(cwd, argv):
        return "[]" if "pr" in argv and "list" in argv else HERE
    return work.sibling_gate(str(sdlc), CONFIG, "2879", run=run, sleep=lambda s: None)


def test_the_sibling_gate_compares_slugs_case_insensitively(tmp_path):
    """The third site of the shape this goal already fixed twice, and it is TWO comparisons, not
    one: `here not in repos` decides membership and `r != here` decides which repos are siblings.
    `here` is DERIVED from `gh repo view`; `decision["repos"]` keys are human-typed registry keys
    since #1477 stopped the machine widening `repos`. A casing difference made the merge readiness
    gate refuse a correctly-configured cross-repo unit, and `merge()` inherits that refusal."""
    ok, why = _sibling_gate(tmp_path,
                            {HERE: {"branch": "b1", "goals": []},
                             THERE: {"branch": "b2", "goals": []}},
                            ["Org/Here", "Org/There"])
    assert ok is False                          # it still refuses -- for a REAL reason
    assert "disagree about which repos are involved" not in why     # membership: not a mismatch
    assert "Org/There" in why and "Org/Here" not in why             # siblings: only the other one


def test_the_sibling_gate_finds_a_branch_recorded_under_another_casing(tmp_path):
    """The third comparison on the same path: the sibling's key comes from the DECISION record and
    the branch is looked up in the REGISTRY entry -- two human-typed stores, so the lookup has to
    ask under the spelling that is actually there."""
    ok, why = _sibling_gate(tmp_path,
                            {HERE: {"branch": "b1", "goals": []},
                             "Org/There": {"branch": "feature/int-contract", "goals": []}},
                            [HERE, "org/there"], name="h")
    assert ok is False
    assert "records no branch" not in why       # the branch WAS found, under the other casing
    assert "no open PR" in why                  # it got as far as asking about the sibling's PR


# --------------------------------------------------------------------------- mutants


def test_treating_a_truthy_authorized_as_a_grant_is_a_mutant_this_suite_kills():
    """`bool("false")` is True. The mutant grants on the exact string that says it did not."""
    entry = _entry()
    entry["repos"][HERE]["authorized"] = "false"
    mutant = _mod_with("return registry.is_authorized(entry, sync.repo_key(repos, repo))",
                       "return bool((repos.get(repo) or {}).get('authorized'))")
    assert mutant.authorized(entry, HERE) is True                    # the bug
    assert _mod().authorized(entry, HERE) is False


def test_letting_the_unit_owner_override_the_board_owner_is_a_mutant_this_suite_kills():
    """The rule is the RESTRICTIVE one. The mutant lets a unit owner put pickable work on a board
    somebody else owns."""
    mutant = _mod_with(
        "        if board:\n            return Verdict(False, BOARD_OWNER_WINS, board, repo)",
        "        if False:\n            return Verdict(False, BOARD_OWNER_WINS, board, repo)")
    assert mutant.may_file(_entry(), HERE, UNIT_OWNER).allowed is True     # the bug
    assert _mod().may_file(_entry(), HERE, UNIT_OWNER).allowed is False


def test_re_inferring_the_owner_is_a_mutant_this_suite_kills():
    """"It happens once" is the whole of the inference rule -- an owner reassignable by whoever
    picks up next is a lease, not an owner."""
    entry = _registry().normalise_entry({"repos": {HERE: {"goals": []}}})
    mutant = _mod_with('if not _named(entry.get("owner")):\n        entry["owner"] = who',
                       'if True:\n        entry["owner"] = who')
    mutant.claim(entry, HERE, "@first")
    mutant.claim(entry, HERE, "@second")
    assert entry["owner"] == "@second"                                     # the bug
    entry2 = _registry().normalise_entry({"repos": {HERE: {"goals": []}}})
    _mod().claim(entry2, HERE, "@first")
    _mod().claim(entry2, HERE, "@second")
    assert entry2["owner"] == "@first"


def test_asking_the_grant_under_our_own_spelling_is_a_mutant_this_suite_kills():
    """The silently-inert lookup #1477's case-insensitive slugs made possible."""
    entry = _entry()
    entry["repos"] = {"Org/Here": {"branch": None, "owner": BOARD, "authorized": True, "goals": []}}
    mutant = _mod_with("return registry.is_authorized(entry, sync.repo_key(repos, repo))",
                       "return registry.is_authorized(entry, repo)")
    assert mutant.authorized(entry, HERE) is False                         # the bug
    assert _mod().authorized(entry, HERE) is True


def test_reaching_the_inference_only_on_the_no_owner_arm_is_a_mutant_this_suite_kills(tmp_path):
    """#1575, AS THE CODE STOOD. The mutant is the shipped behaviour: with the board half's own arm
    disabled, a propagated entry never gets a local board owner and the sibling refuses forever."""
    mutant = _mod_with("if _claimable_board(entry, repo) and _claim_here(",
                       "if False and _claim_here(")
    bad = _sdlc(tmp_path / "bad")
    _seed(bad, entry=_propagated())
    gate = mutant.gate_at_pick(str(bad), _Source(author="sibling-first"), "2879", CONFIG, UNIT,
                               cwd=str(tmp_path / "bad"))
    assert gate.proceed is False                                           # the bug
    assert _registry().read(bad / "features")[UNIT]["repos"][HERE]["owner"] is None
    good = _sdlc(tmp_path / "good")
    _seed(good, entry=_propagated())
    assert _mod().gate_at_pick(str(good), _Source(author="sibling-first"), "2879", CONFIG, UNIT,
                               cwd=str(tmp_path / "good")).proceed is True
    assert _registry().read(good / "features")[UNIT]["repos"][HERE]["owner"] == "sibling-first"


def test_deciding_before_the_claim_lands_is_a_mutant_this_suite_kills(tmp_path):
    """`claim_at_pick` writes through `feature_sync.amend`, to a copy IT read under the unit's lock
    -- so the entry in hand here is not the one that changed. The mutant keeps the stale object and
    refuses on it, which is the worst of both: the owner is recorded and the author is refused for
    not being them."""
    mutant = _mod_with("        entry = registry.read(features_dir).get(unit)\n",
                       "        entry = entry\n")
    bad = _sdlc(tmp_path / "bad")
    _seed(bad, entry=_propagated())
    gate = mutant.gate_at_pick(str(bad), _Source(author="sibling-first"), "2879", CONFIG, UNIT,
                               cwd=str(tmp_path / "bad"))
    assert gate.proceed is False                                           # the bug
    assert _registry().read(bad / "features")[UNIT]["repos"][HERE]["owner"] == "sibling-first"
    good = _sdlc(tmp_path / "good")
    _seed(good, entry=_propagated())
    assert _mod().gate_at_pick(str(good), _Source(author="sibling-first"), "2879", CONFIG, UNIT,
                               cwd=str(tmp_path / "good")).proceed is True


def test_treating_a_named_board_as_claimable_is_a_mutant_this_suite_kills(tmp_path):
    """`_claimable_board` is `claim`'s repo-half guard asked WITHOUT writing, and the two sides
    score it differently -- which is stated rather than hidden. On the WRITE side a loosened version
    is invisible: `_needs_owner` and `claim`'s own absence guard both refuse it a second time, so no
    byte changes. On `would_hold`'s side it IS the answer, and the loosened version reads a board
    that already has an owner as "a pick would name one and proceed" -- after which /sigma-promote
    stops refusing the promotion it exists to refuse. That is where this scores it."""
    mutant = _mod_with(
        'return key if isinstance(one, dict) and not _named(one.get("owner")) else None',
        "return key if isinstance(one, dict) else None")
    sdlc = _sdlc(tmp_path)
    _seed(sdlc)
    assert mutant.would_hold(str(sdlc), CONFIG, UNIT, "nobody", cwd=str(tmp_path)) is None  # the bug
    assert _mod().would_hold(str(sdlc), CONFIG, UNIT, "nobody",
                             cwd=str(tmp_path)).reason == _mod().NOT_AN_OWNER


def test_going_silent_on_the_second_cycle_is_a_mutant_this_suite_kills(tmp_path):
    """#1569's other half, and the mutant is again the shipped behaviour: `_flag` returned False on
    a timeline that already carried the marker, `_tell` was gated on it, and the re-demotion after a
    performed remedy produced no comment and no ledger line at all."""
    mutant = _mod_with("        source.note(goal, again_text if again else text)\n"
                       "        return FLAG_AGAIN if again else FLAG_FIRST",
                       "        if again:\n            return FLAG_NONE\n"
                       "        source.note(goal, text)\n        return FLAG_FIRST")
    for module, silent in ((mutant, True), (_mod(), False)):
        root = tmp_path / ("bad" if silent else "good")
        sdlc, first = _sdlc(root), _Source(author="nobody")
        _seed(sdlc)
        module.gate_at_pick(str(sdlc), first, "2879", CONFIG, UNIT, cwd=str(root))
        again = _Source(author="nobody", comments=[first.notes[0][1]])
        module.gate_at_pick(str(sdlc), again, "2879", CONFIG, UNIT, cwd=str(root))
        assert (again.notes == []) is silent
        assert (len(_notes(sdlc)) == 1) is silent


def test_ledgering_a_refusal_that_was_never_marked_is_a_mutant_this_suite_kills(tmp_path):
    sdlc, source = _sdlc(tmp_path), _Source(author="nobody", landed=False)
    _seed(sdlc)
    mutant = _mod_with("    if not marked:\n", "    if False:\n")
    mutant.gate_at_pick(str(sdlc), source, "2879", CONFIG, UNIT, cwd=str(tmp_path))
    assert len(_notes(sdlc)) == 1                                          # the bug
    sdlc2, source2 = _sdlc(tmp_path / "b"), _Source(author="nobody", landed=False)
    _seed(sdlc2)
    _mod().gate_at_pick(str(sdlc2), source2, "2879", CONFIG, UNIT, cwd=str(tmp_path / "b"))
    assert _notes(sdlc2) == []


# --------------------------------------------------------------------------- the wiring


def test_the_loop_refuses_the_pick_of_an_issue_a_stranger_filed(tmp_path, monkeypatch):
    """`loop._next`'s wiring, through the REAL `_next`: the goal is never claimed, never marked in
    progress, and comes back inert. A gate is only worth anything where it is actually consulted."""
    import types as _types

    import pytest as _pytest
    loop = _load("loop")
    sdlc = _sdlc(tmp_path)
    _seed(sdlc)
    source = _Source(author="nobody")
    source.next_pending = lambda skip=(): None if skip else "2879"
    source.mark_in_progress = lambda goal: _pytest.fail("the goal was claimed anyway")
    monkeypatch.setattr(loop, "feature_labels", _types.SimpleNamespace(
        attach_at_pick=lambda *a, **k: _types.SimpleNamespace(proceed=True, unit=UNIT)))
    monkeypatch.setattr(loop, "_scope_ok_at_pick", lambda *a, **k: True)
    monkeypatch.setattr(loop, "_reconcile_sweep", lambda *a, **k: None)
    monkeypatch.setattr(loop, "_auto_unpark_sweep", lambda *a, **k: set())
    monkeypatch.setattr(loop, "_auto_reclaim_stale_claims", lambda *a, **k: set())
    monkeypatch.setattr(loop, "_emit_run_stop_once", lambda *a, **k: None)
    kind, goal = loop._next(str(sdlc), source, json.loads(
        (sdlc / "config.json").read_text(encoding="utf-8")))
    assert (kind, goal) == ("DONE", None)
    assert source.marked == ["2879"]


def test_a_loop_whose_owner_gate_cannot_load_still_picks(tmp_path, monkeypatch):
    """FAIL-OPEN. The filing gate is unaffected by anything that happens here, so a gate that cannot
    answer must not stop a queue."""
    loop = _load("loop")
    sdlc = _sdlc(tmp_path)
    _seed(sdlc)
    monkeypatch.setattr(loop, "_feature_owner", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert loop._owner_ok_at_pick(str(sdlc), _Source(), "2879", CONFIG, UNIT) is True


def test_a_goal_already_held_for_scope_is_not_also_held_for_ownership(tmp_path, monkeypatch):
    """ONE PICK, ONE REFUSAL. `_next` short-circuits the `and`, so a goal refused by #1477's scope
    gate never reaches this one -- two comments and two ledger entries for one pick would be the
    over-reporting both gates were written to avoid."""
    import types as _types
    loop = _load("loop")
    sdlc = _sdlc(tmp_path)
    _seed(sdlc)
    source = _Source(author="nobody")
    source.next_pending = lambda skip=(): None if skip else "2879"
    monkeypatch.setattr(loop, "feature_labels", _types.SimpleNamespace(
        attach_at_pick=lambda *a, **k: _types.SimpleNamespace(proceed=True, unit=UNIT)))
    monkeypatch.setattr(loop, "_scope_ok_at_pick", lambda *a, **k: False)
    monkeypatch.setattr(loop, "_reconcile_sweep", lambda *a, **k: None)
    monkeypatch.setattr(loop, "_auto_unpark_sweep", lambda *a, **k: set())
    monkeypatch.setattr(loop, "_auto_reclaim_stale_claims", lambda *a, **k: set())
    monkeypatch.setattr(loop, "_emit_run_stop_once", lambda *a, **k: None)
    loop._next(str(sdlc), source, json.loads(
        (sdlc / "config.json").read_text(encoding="utf-8")))
    assert source.author_reads == [] and source.notes == [] and _notes(sdlc) == []


def test_the_filing_path_consults_the_owner_gate():
    src = (SCRIPTS / "handoff.py").read_text(encoding="utf-8")
    assert "feature_owner" in src


def test_the_pick_gate_is_the_only_inference_site():
    """One inference, one call site. `work.start` deliberately has none -- it has no GitHub identity
    in hand, and every way of giving it one was measurably worse (see `_claim_here`)."""
    owner = (SCRIPTS / "feature_owner.py").read_text(encoding="utf-8")
    assert owner.count("claim_at_pick(sdlc_dir, goal, unit, repo, author)") == 1
    assert "claim_at_pick" not in (SCRIPTS / "work.py").read_text(encoding="utf-8")


def test_github_source_can_name_the_author_of_an_issue():
    sources = _load("sources")
    assert hasattr(sources.GitHubSource, "fetch_author")


def test_the_author_is_read_from_the_author_field():
    """WHO OPENED IT, never who it was handed to. An assignee is somebody the work was given to;
    the rule is about who created it."""
    sources = _load("sources")
    # #895: REST first -- the issue's author is REST's `user`, never `assignee`/`assignees`.
    gh = sources.GitHubSource({"discovery": {"source": "github", "github": {"repo": "o/r"}}},
                              run=lambda args: json.dumps({"user": {"login": "wrote-it", "type": "User"},
                                                           "assignee": {"login": "assigned-to"},
                                                           "assignees": [{"login": "assigned-to"}],
                                                           "body": "", "comments": 0}))
    assert gh.fetch_author("42") == "wrote-it"


def _rest_429_then(view_out):
    def run(args):
        if args[0] == "api":
            exc = RuntimeError("gh api failed")
            exc.hint = "gh: API rate limit exceeded (HTTP 429)"
            raise exc
        return view_out
    return run


def test_an_author_gh_did_not_name_degrades_rather_than_raising():
    sources = _load("sources")
    cfg = {"discovery": {"source": "github", "github": {"repo": "o/r"}}}
    # REST answered with no `user` (a deleted account): degrades to "".
    gh = sources.GitHubSource(cfg, run=lambda args: json.dumps({"user": None, "body": "", "comments": 0}))
    assert gh.fetch_author("42") == ""
    # #895: malformed JSON still degrades on the one-call `gh issue view` fallback path.
    assert sources.GitHubSource(cfg, run=_rest_429_then("not json")).fetch_author("42") == ""
    assert sources.GitHubSource(cfg, run=_rest_429_then("{}")).fetch_author("42") == ""


def test_section_fifteen_describes_the_cross_repo_hazard_and_the_real_remedy():
    """#1575 and #1569. §15 documented the SINGLE-repo naming cost and said nothing about the
    cross-repo one, which is worse -- the owner it held work for is in another repository. And
    nothing anywhere said that promoting does not clear either hold."""
    doc = (ROOT / "docs" / "branching-model.md").read_text(encoding="utf-8")
    limitations = doc.split("## 15.")[1]
    assert "RECEIVING repo" in limitations and "different repository" in limitations
    assert "#1575" in limitations
    assert "does not clear an ownership or scope hold on its own" in limitations
    assert "authorized = true" in limitations


def test_the_label_contract_documents_the_third_producer():
    doc = (ROOT / "docs" / "label-model.md").read_text(encoding="utf-8")
    assert "2b-iii" in doc
    assert "feature_owner.gate_at_pick" in doc
