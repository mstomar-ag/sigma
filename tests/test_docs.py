"""Lock the shipped prose docs.

The README's pipeline documentation: it must name all 7 phases, document both optional
companion plugins, and make clear they're an optional enhancement backed by the portable executors —
not hard dependencies. Stable anchors only (phase names, plugin names, documented terms) — not prose
wording.

And, since #1481, the no-direct-commits rule: `AGENTS.md` is where an agent working this repo reads
it, and `docs/branching-model.md` §3 is where its enforcement is described. Both are pinned because
the claim they make is FALSIFIABLE and was, in fact, false — §3 said "Nothing in Sigma enforces
this" for as long as it took #1476 to ship a check that measures the rule before every force-push.
A reader who believes the tool catches every violation is worse off than one who knows it catches
this one and not that one, so what is pinned is the DISTINCTION: the thing that detects, and the
thing that prevents."""
import pathlib
import re
import subprocess
import sys

import pytest
from skill_corpus import skill_corpus

ROOT = pathlib.Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text()
AGENTS = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
BRANCHING = (ROOT / "docs" / "branching-model.md").read_text(encoding="utf-8")
#: #2107 moved `sigma-goal-design`'s whole step 5 ("Write the artifact" -- the schema, the copyable
#: skeleton, every field's rules) wholesale, verbatim, into this reference file. Several CROSS-FILE
#: pins below compare that content against `docs/dossier-pipeline.md` and need the real thing, not
#: the short pointer paragraph SKILL.md's own body now carries in its place.
GOAL_DESIGN_ARTIFACT = (ROOT / "skills" / "sigma-goal-design" / "references" /
                        "writing-the-artifact.md").read_text(encoding="utf-8")

#: The module §15 now describes. Read here rather than imported: these tests are about whether the
#: PROSE still matches the source, so the source is a text to be measured, not a dependency to be
#: executed.
PROPAGATE = (ROOT / "skills" / "sigma-loop" / "scripts" /
             "feature_propagate.py").read_text(encoding="utf-8")

#: The contract wording, verbatim, as issue #1481 states it — and the ONE wording, everywhere. The
#: same bytes appear in `AGENTS.md`, in `docs/branching-model.md` §3 (quoted, not restated), in
#: `feature_rebase.py`'s module docstring, and in both files `/sigma-init` generates.
#: `tests/test_sdlc_init.py::_RULE` is deliberately the same constant: paraphrases are how a
#: cross-reference becomes false, which is what review caught on this very PR.
RULE = "Nobody commits directly to a feature branch. All work reaches it through `sdlc/*` goal"


def test_documents_all_seven_phases():
    for phase in ("Goal", "Research", "Plan", "Plan-Review", "Implement", "Review", "Retrospective"):
        assert phase in README, f"README omits phase: {phase}"


def test_distinguishes_shipped_plan_review_from_companions():
    assert "sigma-plan-review" in README                      # the gate this kit ships
    assert "superpowers" in README and "code-review" in README  # the optional companions


def test_documents_both_backlog_sources():
    assert "discovery" in README                      # the config knob that selects the source
    assert ".sdlc/goals/" in README                   # the local files source
    assert "GitHub issues" in README and "sdlc:goal" in README   # the github source + its label scheme


def test_documents_optional_knowledge_graph():
    assert "knowledge_graph" in README                       # the config toggle
    assert "graphify" in README                              # the default builder
    assert "off by default" in README or "opt-in" in README  # not on without consent
    assert ".sdlc/knowledge/" in README                      # the corpus (research + analysis)


def test_documents_companions_as_optional_with_portable_fallback():
    assert "optional" in README.lower()                       # companions are not required
    assert "claude-plugins-official" in README                # where to get them if you want them
    assert "portable" in README.lower()                       # the sigma-* executors that run without them


def test_agent_instructions_state_the_no_direct_commits_rule():
    assert RULE in AGENTS                                    # the rule itself, not a paraphrase
    assert "docs/branching-model.md" in AGENTS               # where the rationale and the gaps live


def test_agent_instructions_separate_what_is_enforced_from_what_is_not():
    """The half that makes this worth pinning. An agent told only "there is a check" will trust it."""
    assert "feature_rebase.py" in AGENTS                     # what detects a violation
    assert "branch protection" in AGENTS                     # what could actually prevent one
    assert "merge_method: rebase" in AGENTS                  # the blind spot with no local evidence
    assert "`current`" in AGENTS                             # the check is skipped on an up-to-date branch


def test_agent_instructions_say_the_skip_is_silent_not_merely_a_skip():
    """Measured in review: not behind -> outcome `current`, `direct` empty, no issue, and `clause()`
    returns "" because `current` is not in `IN_CLAUSE`. A reader told "it is skipped" still expects
    to be TOLD; the difference between that and silence is the whole value of the finding."""
    assert "`clause()` returns the empty string" in AGENTS   # the mechanism, named
    assert "the pick line says" in AGENTS                     # ...and its consequence for a person
    assert "not behind its base is never checked — and the skip is SILENT" in AGENTS


def test_agent_instructions_price_the_prevention_they_recommend():
    """Every copy of this rule sends the reader to host branch protection, and protecting `feature/*`
    in the ordinary way blocks the force-push upkeep ends in — measured in review as
    `outcome: failed, remote rejected ... non-fast-forward`, on every pick. Advice without its cost
    is how an adopter ends up with neither prevention nor detection."""
    assert "grant upkeep's actor a bypass" in AGENTS
    assert "turns this detection off" in AGENTS


def test_the_branching_model_prices_it_too_and_the_cite_resolves():
    """§13 is where `feature_rebase.switch()` now points for "feature branches stay force-pushable"
    (it pointed at §9, the managed block, which says nothing about protection). If §13 stops saying
    it, that cite is dangling again."""
    assert "grant upkeep's actor a bypass, or accept that upkeep stops" in BRANCHING
    assert "TURNS THE DETECTION OFF" in BRANCHING
    assert "## 13." in BRANCHING                             # the section the docstring cites by number


def test_the_branching_model_no_longer_claims_nothing_enforces_the_rule():
    """#1476 shipped the check; the doc predates it. This is the exact sentence that went stale."""
    assert "Nothing in Sigma enforces this" not in BRANCHING
    assert "arrived_through_a_pull_request" in BRANCHING     # named, so the claim is checkable
    assert "branch protection on the host, or nothing" in BRANCHING   # prevention, stated as such


# --------------------------------------------------------------------------- #1483: the rollout
#
# #1481 put the rule where an agent working THIS repo reads it, and into the two files
# `/sigma-init` generates. Neither reaches a repository that already has its own agent-instruction
# file and never runs the scaffolder — which is every repository that adopts the branching model
# after the fact. §14 now carries the block such a repository pastes, and the check that says
# whether it did.
#
# Everything below is scoped to that ONE subsection, sliced out by its two bounding headings,
# because the file-wide assertions above cannot see it: `RULE in BRANCHING` was already true from
# §3, so a §14 block that paraphrased the rule — the exact drift #1481 existed to close — would
# leave every one of them green.

#: The rollout subsection's own heading, and the sibling that follows it. The slice is bounded by
#: BOTH, which pins the ordering as well as the content: "Step 0, once per repository" reads as
#: step zero only while it precedes the per-unit gestures.
#:
#: The sibling moved when #1660 inserted *Defining a unit* between the two. It has to keep
#: naming the heading that IMMEDIATELY follows, or the slice quietly swallows the new subsection
#: and every content assertion below becomes satisfiable from the wrong prose.
_ROLLOUT_HEADING = "### Step 0, once per repository: put the rule where your agents read it"
_NEXT_HEADING = "### Defining a unit — the three kinds, and `/sigma-define`"

#: The needle the rollout tells a human to grep for — the rule's FIRST SENTENCE, not the whole
#: rule. Measured, not chosen: of the five files that carry the sentence, four wrap it after
#: "`sdlc/*` goal", so a `grep -F` for the whole thing matches one of our own files and misses the
#: other four. The trailing full stop is load-bearing in the other direction — see the two tests.
_GREP_NEEDLE = "Nobody commits directly to a feature branch."

#: Paths the rule scan does not walk: agent/loop scratch, caches, virtualenvs — and `tests/`,
#: whose two constants are ANCHORS rather than statements of the rule to a reader.
_RULE_SCAN_SKIP = frozenset({".git", ".claude", ".sdlc", ".venv", "venv", "__pycache__", "tests"})


def _files_stating_the_rule():
    """Every shipped file whose text carries the rule, DISCOVERED rather than listed.

    A hand-written list of the copies is the same object #1481 removed: it is right on the day it
    is written and silently wrong the day someone adds a sixth copy — and the sixth copy is exactly
    the one nobody checked. Discovery keys off `RULE`, which is why that constant stops at "goal":
    every copy wraps somewhere after it."""
    found = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.suffix not in (".md", ".py", ".tmpl", ".mdc"):
            continue
        rel = path.relative_to(ROOT)
        if _RULE_SCAN_SKIP & set(rel.parts):
            continue
        if RULE in path.read_text(encoding="utf-8", errors="ignore"):
            found.append(rel.as_posix())
    return found


def _rollout():
    """The §14 rollout subsection, sliced between its heading and the next one.

    Bounded by two literal headings rather than by a "next `###`" scan, because the block a reader
    copies CONTAINS a `##` heading of its own — a scan for the next heading would cut the slice at
    the sample's first line and stop just short of the sentence these tests exist to pin."""
    assert _ROLLOUT_HEADING in BRANCHING, "the §14 rollout subsection is gone"
    assert _NEXT_HEADING in BRANCHING, "the sibling that bounds the slice is gone"
    start = BRANCHING.index(_ROLLOUT_HEADING)
    end = BRANCHING.index(_NEXT_HEADING, start)
    return BRANCHING[start:end]


def test_the_rollout_block_sits_in_the_adoption_section_before_opening_a_unit():
    """Placement is the claim. A per-repository step that appears after the per-unit gestures is a
    step a reader has already skipped by the time they reach it.

    *Opening a unit* is asserted separately from `_NEXT_HEADING` now that the two are not the same
    heading: bounding the slice is one job, and "step zero precedes steps 1-4" is the other."""
    assert BRANCHING.index("## 14.") < BRANCHING.index(_ROLLOUT_HEADING)
    assert BRANCHING.index(_ROLLOUT_HEADING) < BRANCHING.index(_NEXT_HEADING)
    assert BRANCHING.index(_ROLLOUT_HEADING) < BRANCHING.index("### Opening a unit")


def test_the_adopter_paste_block_quotes_the_rule_verbatim():
    """The sixth copy of the sentence, and the first one that leaves this repository in someone
    else's file. It is pinned to the SAME constant as the other five, section-scoped, so a
    paraphrase here fails even though `RULE in BRANCHING` stays true from §3."""
    assert RULE in _rollout()


def test_the_paste_block_is_fenced_so_it_is_copied_whole_rather_than_read():
    """The block is text an adopter transplants, so its boundaries have to be unambiguous — and
    its own `##` heading would otherwise become a heading of THIS document, silently making §14
    end wherever the sample starts."""
    rollout = _rollout()
    assert "```markdown" in rollout
    open_at = rollout.index("```markdown")
    close_at = rollout.index("\n```", open_at + len("```markdown"))
    assert open_at < rollout.index(RULE) < close_at, "the rule sits outside the block being copied"


def test_the_paste_block_states_the_consequence_not_only_the_prohibition():
    """An adopter's agent obeys a rule it understands and routes around one it does not. What
    makes this one obeyable is that the cost lands on the person who broke it."""
    rollout = _rollout()
    assert "rewrites published history" in rollout            # why a rebase is dangerous at all
    assert "is where that work goes" in rollout               # ...and who pays, stated plainly
    assert "silence is not evidence" in rollout               # the skip, which reads as an all-clear
    assert "grant upkeep's actor a bypass" in rollout         # the prevention, priced (§13)


def test_the_rollout_names_every_host_convention_rather_than_assuming_one():
    """The kit ships to strangers on several agent hosts. Naming one file makes the instruction
    inapplicable to every repository that uses a different one."""
    rollout = _rollout()
    assert "`AGENTS.md`" in rollout
    assert "`.claude/CLAUDE.md`" in rollout
    assert rollout.count("CLAUDE.md") >= 2, "the bare and the .claude/ conventions are two files"
    assert ".cursor/rules/" in rollout


def test_the_rollout_gives_a_check_to_run_and_a_shape_to_record_it_in():
    """"Add the rule to each repo" with no check is a claim; with a grep it is a measurement. The
    checklist is the other half — the issue says the rollout must RECORD where each repo has it."""
    rollout = _rollout()
    assert "grep -Fq '" + _GREP_NEEDLE + "'" in rollout       # the exact command, runnable as-is
    assert "- [ ] `<repo>`" in rollout                        # a task list, so a box can be ticked
    assert "its own board" in rollout                         # where the edit itself is tracked


def test_the_prescribed_grep_matches_every_copy_of_the_rule_the_kit_ships():
    """The advice, executed against our own files. Four of the five wrap the rule's second
    sentence, so a needle any longer than the first sentence would match `feature_rebase.py`'s
    docstring and miss the other four — which is how "run this grep" becomes advice that reports a
    correctly-patched file as unpatched."""
    files = _files_stating_the_rule()
    assert len(files) >= 5, f"the rule should be in at least the five known files, found {files}"
    for rel in files:
        assert _GREP_NEEDLE in (ROOT / rel).read_text(encoding="utf-8"), \
            f"{rel} carries the rule but does not answer the grep the rollout prescribes"


def test_the_prescribed_needle_is_not_satisfied_by_the_heading_alone():
    """The full stop is what separates "this repository states the rule" from "this repository has
    a heading named after it" — and both `AGENTS.md` and the pasted block carry that heading."""
    assert _GREP_NEEDLE not in "## Nobody commits directly to a feature branch\n"
    assert "## Nobody commits directly to a feature branch\n" in AGENTS   # the heading is real


# ----------------------------------------------------- #1598: the gesture that has to be complete
#
# §14's *Opening a unit* is what an adopter follows literally, and for 1.4.0 and 1.4.1 it was
# three steps that left the registry permanently absent while the paragraph under them promised
# every goal would be "recorded against it". Measured live: the branch was cut from the unit, the
# label was attached, and `feature_sync` returned `not-adopted` — so the promise was false for the
# whole repository, with nothing on stderr saying so.
#
# What is pinned is the ORDER, not the prose. The registry step has to sit after the label and
# before the body marker, because the marker is what triggers the first pick and the first pick is
# the only chance that goal ever gets to be recorded (§15: nothing backfills it). A tidy-up that
# moved it to the end, or dropped it as "obvious", would restore the exact defect.

_OPENING_HEADING = "### Opening a unit"
_SUMMARY_HEADING = "### The one-line summary"

#: The gesture, as an adopter pastes it. `feature_sync` tests `.sdlc/features` with `is_dir()` and
#: `feature_registry` creates `units/` under it on the first write, so this is the whole of it —
#: a longer command would be a second, subtly different rule for the same thing.
_ADOPT = "mkdir -p .sdlc/features"


def _opening():
    """§14's *Opening a unit*, sliced between its heading and the summary table that follows it."""
    assert _OPENING_HEADING in BRANCHING, "§14's *Opening a unit* is gone"
    assert _SUMMARY_HEADING in BRANCHING, "the sibling that bounds the slice is gone"
    start = BRANCHING.index(_OPENING_HEADING)
    return BRANCHING[start:BRANCHING.index(_SUMMARY_HEADING, start)]


def test_opening_a_unit_creates_the_registry_and_does_it_before_the_body_marker():
    """The fix, pinned as an ordering rather than as a line count. Placement is the whole claim:
    after the label (which the marker's first pick needs), and before the marker (which is what
    causes that pick)."""
    opening = _opening()
    assert _ADOPT in opening, "§14 no longer tells an adopter to create `.sdlc/features/`"
    assert opening.index("gh label create") < opening.index(_ADOPT) < opening.index("Feature: <name>")


def test_opening_a_unit_says_what_the_missing_step_costs_and_that_it_is_not_recoverable():
    """Naming the step is not enough on its own: an adopter who already ran the old three steps
    needs to know that the goals picked since are gone from the record and why the kit will not
    guess them back. Both halves live in §15; §14 is where the person actually is."""
    opening = _opening()
    assert "never backfilled" in opening
    assert "not-adopted" in opening                      # what the sync really returned, named
    assert "#1576" in opening and "#1564" in opening     # why the kit does not do it for you


def test_the_summary_table_no_longer_reads_an_empty_show_as_an_empty_registry():
    """The second stale row. `feature_sync.py show .sdlc` printed `{}` for a unit that demonstrably
    existed and was already attaching labels, and the table offered it as the way to see the whole
    registry with nothing distinguishing "no units" from "no registry"."""
    summary = BRANCHING[BRANCHING.index(_SUMMARY_HEADING):]
    assert "`ls .sdlc/features`" in summary
    assert "not adopted" in summary


# ------------------------------------------------------ #1591: the walkthrough, held to being one
#
# `docs/how-branching-works.md` is a ROUTE through `docs/branching-model.md` — the order things
# happen in, and where each decision is written down. It is deliberately not a second explanation
# of the same machinery, because a second explanation is a second source of truth and the drift is
# only ever discovered by the reader who trusted the wrong one.
#
# "It carries no rules of its own" is exactly the class of promise #1481 exists because we could
# not keep. Every pin in this file above is a hand-written literal against a file that already
# says the thing; a NEW file restating any of §15's claims in its own words would be invisible to
# every one of them. So the promise is mechanical here, in two directions:
#
#   * it may not contain a phrase that is pinned as normative somewhere else (below), and
#   * it must actually route — a floor on section links, and every section carrying at least one.
#
# Only ONE sentence in the tree has automatic discovery (`RULE`, via `_files_stating_the_rule`).
# Everything else is listed, so this list is the enforcement and not a summary of it.

WALKTHROUGH_PATH = ROOT / "docs" / "how-branching-works.md"
WALKTHROUGH = WALKTHROUGH_PATH.read_text(encoding="utf-8")

#: Phrases the tree pins as NORMATIVE elsewhere — in the assertions above, or in
#: `docs/branching-model.md` §15's inventory of what the model does not do. A copy of any of them
#: here would be a rule with two homes and one test, which is the failure mode this file exists to
#: close. Matched case-insensitively: §15 shouts some of these and a lower-cased paste is the same
#: paste.
#:
#: Group 1 — literals asserted against `AGENTS.md` or `BRANCHING` earlier in this file.
#: Group 2 — §15's own claims, in §15's wording.
#: Group 3 — the contract's load-bearing defaults, which a walkthrough is most tempted to "just
#:           mention" and which are precisely the sentences that go stale.
_NORMATIVE_ELSEWHERE = (
    # Group 1
    RULE,
    _GREP_NEEDLE,
    "grant upkeep's actor a bypass",
    "turns the detection off",
    "turns this detection off",
    "arrived_through_a_pull_request",
    "branch protection on the host, or nothing",
    "`clause()` returns the empty string",
    "the pick line says",
    "not behind its base is never checked",
    "rewrites published history",
    "silence is not evidence",
    "Nothing in Sigma enforces this",
    # Group 2 — §15
    "the body wins",
    "absent means false",
    "only a real boolean",
    "depth 3",
    "no goals recorded",
    "is False even when the write plainly went in",
    "one-directional",
    "no later pass recovers it",
    "nothing merges a feature branch",
    "never on the working of it",
    "merge_method: rebase",
    "outside any pull request",
    # Group 3
    "remote is the truth",
    "never a delta",
    "at most three spaces",
    "sigma never creates",
    "sdlc:needs-label",
    "open: false",
    "`index.json` is derived",
)

#: The floor on routing. Chosen well below what the document ships with, so ordinary editing does
#: not trip it and gutting the links does. A page that stops linking has stopped being a route and
#: started being a rival.
_LINK_FLOOR = 25

#: How a link into the contract is written. The section number is always in the LINK TEXT as well,
#: so a reader still lands in the right place if an anchor is ever renamed out from under it.
_CITE = re.compile(r"\]\(branching-model\.md#([^)]+)\)")


def _slug(heading: str) -> str:
    """GitHub's heading anchor, near enough to check a document against itself.

    Lower-case; drop every character that is not a letter, a digit, a space, a hyphen or an
    underscore; spaces become hyphens. That is `github-slugger` for everything these headings
    contain — backticks, full stops, colons, angle brackets and asterisks all vanish, and an em
    dash vanishes leaving the two spaces around it as two hyphens.
    """
    kept = [c for c in heading.lower() if c.isalnum() or c in " -_"]
    return "".join(kept).strip().replace(" ", "-")


def _headings(text: str):
    """Every ATX heading OUTSIDE a fenced block.

    Fence tracking is the whole job: `branching-model.md` prints `## Notes`, `# int-contract` and
    a `## Nobody commits directly...` heading inside fences — the last of them is the block §14
    tells an adopter to paste — and a naive scan would mint anchors for all three, so a link to a
    heading that does not exist would pass.
    """
    out, fenced = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced or not line.startswith("#"):
            continue
        level = len(line) - len(line.lstrip("#"))
        if 1 <= level <= 6 and line[level:level + 1] == " ":
            out.append(line[level:].strip())
    return out


def test_the_walkthrough_carries_no_rule_of_its_own():
    """The deliverable. A future editor who pastes a sentence from the contract into the
    walkthrough gets a red test, not a second source of truth that nothing compares."""
    haystack = WALKTHROUGH.lower()
    pasted = [p for p in _NORMATIVE_ELSEWHERE if p.lower() in haystack]
    assert not pasted, (
        "docs/how-branching-works.md restates wording that is pinned as normative elsewhere; "
        "link to the section instead of repeating it: " + "; ".join(pasted)
    )


def test_the_walkthrough_routes_rather_than_explains():
    """A route is measurable: it is mostly links into the contract. This is the other half of the
    denylist — a document could pass that test by explaining everything in fresh words."""
    cites = _CITE.findall(WALKTHROUGH)
    assert len(cites) >= _LINK_FLOOR, (
        f"only {len(cites)} links into branching-model.md; a walkthrough that stops citing the "
        f"contract has become a rival to it"
    )


def test_every_walkthrough_section_sends_the_reader_somewhere():
    """Per-section, not just per-file: a floor over the whole document is satisfied by one dense
    table and a page of unsourced prose."""
    body = WALKTHROUGH.split("\n## ")
    for section in body[1:]:
        title = section.splitlines()[0]
        assert _CITE.search(section), f"walkthrough section '{title}' cites nothing"


def test_every_section_the_walkthrough_cites_actually_exists():
    """The anchors, resolved against the real headings. This is what catches the OTHER drift
    direction — the contract being renumbered or a section being folded away, leaving the
    walkthrough pointing at nothing. Nothing else in the tree would notice."""
    anchors = {_slug(h) for h in _headings(BRANCHING)}
    dangling = sorted({c for c in _CITE.findall(WALKTHROUGH) if c not in anchors})
    assert not dangling, (
        "docs/how-branching-works.md links to sections of branching-model.md that do not exist: "
        + ", ".join(dangling)
    )


def test_the_walkthrough_disclaims_its_own_authority():
    """A reader must not be able to finish it believing it is the contract. Pinned because it is
    the sentence an editor tidying the introduction would cut first."""
    assert "This document decides nothing." in WALKTHROUGH
    assert "the contract is right and this file has a bug" in WALKTHROUGH
    assert "the only one of the two that is authoritative" in WALKTHROUGH


def test_the_contract_and_the_agent_rules_both_point_at_the_walkthrough():
    """A route nobody is sent down is a file that rots unread. Both entry points carry it: the
    contract's own opening, for someone who landed on 1300 lines and wants the short way in, and
    `AGENTS.md`, for an agent that reads nothing else."""
    assert "how-branching-works.md" in BRANCHING
    assert BRANCHING.index("how-branching-works.md") < BRANCHING.index("## 2. The branch shape")
    assert "docs/how-branching-works.md" in AGENTS


# ------------------------- #1573: §15, and the commit the kit makes into somebody else's repository
#
# §15 said "Propagating an entry to a sibling repo is not part of what is described here" while
# #1477 had already shipped exactly that — and shipped it as a PUT through the Contents API with no
# `branch` argument, which is precisely what selects the destination's DEFAULT branch.
#
# It was never an AUTHORISATION defect, and the tests below are careful not to imply it was: the
# write needs a `granted` verdict, which means the access check measured push/admin/maintain for a
# confirmed identity, and propagation cannot hand a repository a permission it did not already have
# because `authorized` is never copied. What it is, is an **undisclosed automated commit to somebody
# else's default branch, outside any pull request** — denied by the one section an adopter reads to
# find out what the model does not do.
#
# §15 also contradicted ITSELF. #1479 added a bullet to the SAME list that rests on propagation
# being real ("asks addressed to `owner` by propagation ... land unaddressed"), four bullets above
# the one denying it existed. So both halves are pinned, because prose alone kept neither:
#
#   * AGAINST THE CODE — "the default branch" is true only for as long as `_write_remote` omits
#     `branch`, and "gated" only while the copy refuses on anything but `granted`. Add a `branch`
#     argument, or drop the verdict check, and §15's sentence is silently false with nothing else in
#     this tree noticing;
#   * AGAINST ITSELF — the two sentences that went stale may not come back, and the bullet that
#     depends on propagation being real is asserted in the same slice as the bullet that says it is.

_SECTION_15 = "## 15. Honest limitations, and the gaps that are deliberate"

#: The two sentences #1477 falsified. Pinned as literals, exactly as
#: `test_the_branching_model_no_longer_claims_nothing_enforces_the_rule` pins §3's: a stale claim
#: comes back by being pasted, not by being reinvented.
_WITHDRAWN = (
    "Propagating an entry to a sibling repo is not part of what is described here",
    "Each repo's registry holds what its own picks put there",
)


def _section_15():
    """§15, sliced from its heading to the sibling-contract footer that closes the document."""
    assert _SECTION_15 in BRANCHING, "§15 is gone, and the walkthrough links to its anchor"
    return BRANCHING[BRANCHING.index(_SECTION_15):]


def _function(source: str, name: str) -> str:
    """One top-level function's source, sliced from its `def` to the next one.

    Scoped rather than file-wide because every assertion below is about what ONE function does and
    does not do: `branch=` appears nowhere in this module today, but a file-wide `not in` would go
    green forever for the wrong reason the moment an unrelated helper mentioned it."""
    start = source.index("def %s(" % name)
    end = source.find("\ndef ", start)
    return source[start:end if end != -1 else len(source)]


def test_section_15_no_longer_denies_the_propagation_the_kit_ships():
    """The defect itself. Both sentences, against the WHOLE document rather than the slice — a
    withdrawn claim reappearing in §11 or §8 would be the same claim in a worse place."""
    for stale in _WITHDRAWN:
        assert stale not in BRANCHING, f"§15's withdrawn claim is back: {stale}"


def test_section_15_says_where_the_propagated_write_actually_lands():
    """The issue's done-when. The destination is the part a reader cares about, so it is pinned as
    three separate facts: whose branch, which branch, and that no pull request is involved."""
    section = _section_15()
    assert "default" in section.lower()
    assert "outside any pull request" in section
    assert "Contents API" in section
    assert "feature_propagate" in section, "the claim has to name the code that makes it checkable"


def test_section_15_says_the_write_is_gated_and_that_it_cannot_escalate():
    """The other half of honesty. A section that described the commit and omitted the gate would
    trade one wrong impression for a worse one — an adopter reading "it commits into your repo"
    with no `granted` beside it will conclude the kit writes wherever it likes."""
    section = _section_15()
    assert "`granted`" in section
    assert "`unknown`" in section, "the verdict that writes nothing is the point of having three"
    assert "`authorized`" in section and "never copied" in section


def test_section_15_does_not_contradict_the_bullet_that_relies_on_propagation():
    """The self-contradiction, mechanically. #1479's bullet treats propagation as a live mechanism;
    it and the bullet describing that mechanism now live in one slice, so a future editor removing
    either leaves the other visibly unsupported instead of quietly wrong."""
    section = _section_15()
    assert "asks addressed to `owner` by propagation" in section      # #1479's bullet, still there
    assert "COMMITS THE UNIT'S ENTRY INTO EVERY SIBLING REPO" in section   # ...and what it relies on


def test_the_default_branch_claim_is_still_true_of_the_code():
    """§15 says the write lands on the sibling's DEFAULT branch. That is not a design statement —
    it is a consequence of one omission in one `gh api` invocation, and adding the argument back
    would move every propagated commit to a different branch while §15 went on saying this."""
    write = _function(PROPAGATE, "_write_remote")
    assert '"-X", "PUT"' in write and "contents/%s" in write
    assert "branch=" not in write, (
        "feature_propagate._write_remote now sends a `branch` argument, so the write no longer "
        "lands on the sibling's default branch — docs/branching-model.md §15 says it does"
    )


def test_the_gate_the_section_describes_is_still_the_gate_in_the_code():
    """§15's "only on a `granted` verdict" is what makes this a disclosure fix rather than a
    security one. If the refusal ever softens, the section is describing a kit that no longer
    exists — in the permitting direction, which is the direction that matters."""
    copy_one = _function(PROPAGATE, "_copy_one")
    assert 'GRANTED = "granted"' in PROPAGATE
    assert "if verdict != GRANTED:" in copy_one
    assert "return _copy(HELD, NOT_GRANTED" in copy_one
    assert "if verdict is None:" in copy_one, "no recorded verdict holds, rather than proceeding"


def test_authorized_is_still_never_carried_across_by_the_copy():
    """§15's "`authorized` is the destination's own always and is never copied". `merge_entry` fills
    `branch` and `owner` from the incoming entry and deliberately does not fill this one; a third
    `_fill` would let one repo's registry grant itself the per-unit permission in another."""
    merge = _function(PROPAGATE, "merge_entry")
    assert '_fill(mine, theirs, "branch")' in merge
    assert '_fill(mine, theirs, "owner")' in merge
    assert '"authorized": False' in merge, "the destination's default is the safe state"
    assert '_fill(mine, theirs, "authorized")' not in merge
    assert 'theirs.get("authorized")' not in merge


def test_section_11_sends_a_cross_repo_reader_to_section_15_before_they_adopt():
    """§15 is the end of a 1300-line contract, and the person this concerns is reading §11. The
    pointer is a pointer, not a second explanation: it states the destination and cites the section
    that owns the rest."""
    eleven = BRANCHING[BRANCHING.index("## 11. Cross-repo"):BRANCHING.index("### 11a.")]
    assert "outside any pull request" in eleven
    assert "§15" in eleven


# ------------------- #1577: a section number an adopter cannot follow is worse than a sentence
#
# `branching-model.md` was written against a design specification that is NOT shipped with the kit,
# and five of its cross-references kept that document's numbering: three `§7.2` and two `§7.1`,
# pointing at sections no reader of this repository has. One of the same paragraph's cites was worse
# than dangling — `**§8 won**` RESOLVES here, to "The registry — `.sdlc/features/`", which says
# nothing about completion being human-only. A reader who follows it lands somewhere plausible and
# wrong, which is the failure mode a broken link at least does not have.
#
# The rule is now stated in the document's own header and pinned here, so the next cite written
# against the spec fails a test instead of shipping.

#: A section reference as this document writes them: `§8f`, `§2b-iii`, and the spec-numbered `§7.1`
#: shape that must no longer appear unqualified.
_SECTION_REF = re.compile(r"§(\d+(?:\.\d+)?[a-z]?(?:-[ivx]+)?)")

#: This document's own headings, by the number they are cited under. Derived from the headings
#: themselves rather than listed, because a listed set goes stale the moment a section is added.
_OWN_SECTION = re.compile(r"^#{2,4}\s+(\d+[a-z]?)\.", re.M)


#: What ends the scope a citation's qualifier has to share with it: a blank line, the start of the
#: NEXT list item, or the next table row. The list-item break is the one that was measured rather
#: than assumed — a plain blank-line paragraph swallows a whole bulleted list, and §15 is a bulleted
#: list in which one bullet legitimately cites `docs/label-model.md`. Without this, that bullet
#: excused a dangling cite three bullets away, and the mutant that restored the original defect
#: survived.
_ITEM_BREAK = re.compile(r"\n\n|\n[-*] |\n\| ")


def _citation_scope(text, index):
    """The paragraph, list item or table row a character sits in.

    Not per-file: a `.md` named anywhere would excuse every reference in the document. Not per-line
    either: "`docs/label-model.md` §2 defines ... that document's §2b-i" qualifies its second cite
    from an earlier sentence, and where that sentence wraps is arbitrary."""
    starts = [b.end() for b in _ITEM_BREAK.finditer(text, 0, index)]
    end = _ITEM_BREAK.search(text, index)
    return text[starts[-1] if starts else 0:end.start() if end else len(text)]


def test_every_section_reference_in_the_contract_can_be_followed():
    """Every `§N` resolves to a heading of THIS document, or its paragraph names the document it
    belongs to. Those are the only two forms the header admits, and the second is what keeps the
    legitimate `docs/label-model.md` cites legal without excusing a bare number."""
    own = set(_OWN_SECTION.findall(BRANCHING))
    dangling = []
    for match in _SECTION_REF.finditer(BRANCHING):
        if match.group(1) in own:
            continue
        if ".md" in _citation_scope(BRANCHING, match.start()):
            continue
        dangling.append("line %d: §%s" % (BRANCHING[:match.start()].count("\n") + 1, match.group(1)))
    assert not dangling, (
        "docs/branching-model.md cites sections that do not exist in it and name no other document: "
        + ", ".join(dangling))


def test_the_contract_says_which_document_its_section_numbers_belong_to():
    """The header clause the test above rests on. Without it the rule is a convention one editor
    knows; with it, the next person writing a cross-reference is told the rule before they write
    one — and told that the design spec is not citable, which is the specific mistake made five
    times."""
    header = BRANCHING[:BRANCHING.index("## 1. The one-paragraph version")]
    assert "Section numbers." in header
    assert "never cited by number" in header      # the spec is not a citable document


def test_the_completion_ruling_cites_the_section_that_actually_holds_it():
    """`**§8 won**` resolved to the registry section. The ruling is about completion, which is §13,
    and §13 is where the words "Completion is human-only by default" actually are — so the two are
    pinned against each other rather than against a number somebody could renumber."""
    eleven_a = BRANCHING[BRANCHING.index("### 11a."):BRANCHING.index("### 11b.")]
    assert "§7.2" not in eleven_a and "§8 won" not in eleven_a
    assert "§13" in eleven_a
    thirteen = BRANCHING[BRANCHING.index("## 13. Completion"):BRANCHING.index("## 14.")]
    assert "Completion is human-only by default" in thirteen


# ------------------- #1577: §6e says "measured", so something has to keep measuring it
#
# The table listed ONE `ls-remote` per started goal when the code makes two, and omitted the
# per-pick work three later goals added (#1476's rebase upkeep, #1477's scope gate, #1479's
# ownership gate). None of that was wrong when it was written — it went stale, silently, because
# nothing tied the number to the code. A table labelled *measured* that no longer is, is worse than
# no table: a reader budgets against it.
#
# So the load-bearing number is now pinned the same way the table says it was obtained. Every git
# and `gh` call on the start path goes through one injected runner, which is what makes this cheap:
# no git, no network, no fixture repository.

_LOOP_SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"


def _loop_script(name):
    """One loop script, loaded standalone — `_load` inside those modules resolves its own siblings,
    so nothing here has to build a package."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, _LOOP_SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_COST_CONFIG = {"work": {"enabled": True, "base": "main", "remote": "origin"},
                "discovery": {"source": "github", "github": {"repo": "acme/app"}}}


def _calls_for_one_started_goal(tmp_path, adopted, upkeep=None, hook=None):
    """`work.start()` for one goal declaring a unit -> every call it made, as strings.

    The answers are the ones that reach the EXPENSIVE branch of rebase upkeep — a feature branch
    behind its base — because that is the case §6e's row is about and the cheap branch would count
    fewer calls while the table went on claiming more."""
    import json
    work, registry = _loop_script("work"), _loop_script("feature_registry")
    issue = json.dumps({"number": 1467, "title": "t", "body": "Feature: voice-interview",
                        "labels": [{"name": "feature:voice-interview"}]})
    config = _COST_CONFIG if upkeep is None else dict(_COST_CONFIG, upkeep=upkeep)
    calls = []

    def run(_cwd, argv):
        line = " ".join(str(a) for a in argv)
        calls.append(line)
        if "api repos/" in line and "issues/" in line:
            return issue
        if "ls-remote" in line:
            return "abc123\trefs/heads/feature/voice-interview\n"
        if "rev-parse" in line:
            return "0" * 40
        return ""

    # #144: the tree guard reads git through its own config-pinned, time-bounded, UTF-8 reader
    # (`feature_rebase._git_read`), NOT the injected runner -- so it is recorded here explicitly,
    # answered "no change" like the rest, and the push path is what gets counted.
    rebase = work._feature_rebase()
    rebase._git_read = lambda _cwd, args: calls.append("git " + " ".join(map(str, args))) or ""
    sdlc = tmp_path / ("adopted" if adopted else "bare") / ".sdlc"
    (sdlc / "goals").mkdir(parents=True)
    (sdlc / "config.json").write_text(json.dumps(config), encoding="utf-8")
    if adopted:
        registry.registry_dir(str(sdlc)).mkdir(parents=True, exist_ok=True)
    if hook is not None:
        hook(rebase, calls)
    work.start(str(sdlc), config, "1467", run=run)
    return calls


def test_section_6e_still_counts_the_remote_reads_a_started_goal_actually_makes(tmp_path):
    """The number the table got wrong. TWO `ls-remote` passes per started goal, not one — the
    registry reconcile makes one and rebase upkeep makes its own — and the row now says so.

    Both halves are asserted, because each pins a different column: the count pins "paid per goal
    started", and the zero on an unadopted project pins the "gated on `.sdlc/features/`" answer,
    which is the column an adopter reads to decide whether any of this concerns them."""
    def ls_remotes(calls):
        return [c for c in calls if c.startswith("git ls-remote") and "feature/*" in c]
    assert len(ls_remotes(_calls_for_one_started_goal(tmp_path, adopted=True))) == 2
    assert ls_remotes(_calls_for_one_started_goal(tmp_path, adopted=False)) == []
    row = [l for l in BRANCHING.splitlines() if "ls-remote --heads" in l and l.startswith("|")]
    assert len(row) == 1 and "twice, not once" in row[0], row


def test_section_6e_still_counts_the_whole_pick_and_not_just_the_part_that_was_cheap(tmp_path):
    """The total, which is what a reader budgets against: 4 calls unadopted, 17 adopted (16 before
    #144 added the pre/post tree comparison), for one
    goal whose branch is behind its base. Pinned as the two numbers rather than as a delta so a
    change to either side is named, and pinned at all because the table's credibility rests on the
    word *measured* — which nothing was enforcing when three goals' worth of per-pick work went
    missing from it."""
    assert len(_calls_for_one_started_goal(tmp_path, adopted=False)) == 4
    assert len(_calls_for_one_started_goal(tmp_path, adopted=True)) == 17
    section = BRANCHING[BRANCHING.index("### 6e."):BRANCHING.index("## 7. At pick")]
    assert "**4 calls become 17**" in section
    assert "How this was measured" in section          # the method, so the next reader can redo it


def test_section_6e_names_the_per_pick_work_the_later_levels_added(tmp_path):
    """The omission itself. Three goals added per-pick work after the table was written, and a
    reader who budgeted against it was told about none of them. Each is pinned by the artefact a
    reader would actually look for, not by its issue number."""
    section = BRANCHING[BRANCHING.index("### 6e."):BRANCHING.index("## 7. At pick")]
    for missing in ("rebase upkeep",          # #1476
                    "scope gate",             # #1477
                    "ownership gate",         # #1479
                    "sibling registry copy"): # #1477's propagation half
        assert missing in section, f"§6e no longer prices {missing}"


# ------------------- #1633: §6 and §14 describing a base that stopped following a declaration
#
# #1571 put an adoption condition on base resolution: a `feature:*` declaration only retargets a
# goal's base where `.sdlc/features/` exists. Two statements did not move with it. §6's precedence
# block still opened "the unit THIS GOAL'S ISSUE declares → feature/<name>" with no condition on
# it — wrong for exactly the reader most likely to be consulting it, an adopter who has not created
# the directory yet — and §14's inventory of what the directory gates named the registry sync and
# the access check and not the base.
#
# Both are pinned the way §6e's cost table is: by DRIVING the behaviour, not by reading the function
# next to the prose. The precedence block is a claim about which branch a worktree is cut from, so
# the test cuts one, twice, and reads the argument `git worktree add` was actually given.

def _base_a_started_goal_is_cut_from(tmp_path, adopted):
    """The `<remote>/<base>` `work.start()` hands `git worktree add` for a goal declaring a unit.

    Deliberately NOT the record's `base` field or the human-readable summary line: the only thing
    that decides where the work lands is this argument, and a summary can agree with a record while
    both disagree with git."""
    add = [c for c in _calls_for_one_started_goal(tmp_path, adopted)
           if c.startswith("git worktree add -b sdlc/")]
    assert len(add) == 1, add
    return add[0].rsplit(" ", 1)[1]


def test_a_declaration_moves_the_base_only_where_the_model_was_adopted(tmp_path):
    """§6 line 0, as behaviour. The SAME issue, declaring the same unit, on two projects differing
    only by one directory — and the goal is cut from two different branches. The unadopted answer is
    the load-bearing one: it is `work.base`, unchanged, which is what makes adoption opt-in."""
    assert _base_a_started_goal_is_cut_from(tmp_path, adopted=False) == "origin/main"
    assert (_base_a_started_goal_is_cut_from(tmp_path, adopted=True)
            == "origin/feature/voice-interview")


def test_section_6_states_the_adoption_condition_in_the_precedence_block_itself(tmp_path):
    """Not merely somewhere in §6 — IN the block. The block is what gets read, quoted and pasted;
    a condition a paragraph below it is a condition the copy loses."""
    section = BRANCHING[BRANCHING.index("## 6. Base resolution"):BRANCHING.index("### 6a.")]
    block = section[section.index("```") + 3:section.index("```", section.index("```") + 3)]
    assert "`.sdlc/features/`" in block, block
    assert "the unit THIS GOAL'S ISSUE declares" in block      # still the first-choice rule
    assert "three lines of precedence under one condition" in section


def test_section_6_still_says_the_condition_gates_the_base_and_not_the_read(tmp_path):
    """The distinction #1571 was careful about and #1633 asked to be written down. Gating the READ
    would save one REST call and cost the only diagnostic that separates a project that meant to
    adopt from one that never wanted the model — so §6 says so, and §6e's rows depend on it being
    true. Measured rather than asserted: the unadopted pick still reads the issue."""
    section = BRANCHING[BRANCHING.index("## 6. Base resolution"):BRANCHING.index("### 6a.")]
    assert "gates the BASE, never the READ" in section
    reads = [c for c in _calls_for_one_started_goal(tmp_path, adopted=False)
             if c.startswith("gh api repos/") and "issues/" in c]
    assert len(reads) == 1, (
        "an unadopted project no longer reads the declaration, so §6's 'gates the BASE, never the "
        "READ' and §6e's ungated REST row are both false: %r" % (reads,))


def test_the_adoption_condition_is_still_the_one_in_the_code():
    """§6 names `work._declaration_moves_the_base` and §14 names the `is_dir()` test. The prose is
    true only while the predicate is a CONJUNCTION — relax it to `_reads_a_declaration` alone and
    every sentence added for #1633 is silently false again, in the permitting direction."""
    work_src = (ROOT / "skills" / "sigma-loop" / "scripts" / "work.py").read_text(encoding="utf-8")
    predicate = _function(work_src, "_declaration_moves_the_base")
    assert "return _reads_a_declaration(config, goal) and _adopted(sdlc_dir)" in predicate
    assert "return _feature_registry().registry_dir(sdlc_dir).is_dir()" in _function(work_src,
                                                                                    "_adopted")


def test_section_14_gating_inventory_names_base_resolution_first():
    """The omission itself. A reader who has not created the directory is reading this paragraph to
    find out what it costs them, and the answer that concerns them most is that their goals are cut
    from `work.base` — which the paragraph did not say at all."""
    fourteen = BRANCHING[BRANCHING.index("## 14. Adopting it"):BRANCHING.index("### Step 0,")]
    gates = fourteen[fourteen.index("**What the `.sdlc/features/` directory gates.**"):]
    gates = gates[:gates.index("\n\n")]
    assert "Base resolution" in gates and "§6" in gates
    assert gates.index("Base resolution") < gates.index("registry sync"), (
        "base resolution is the gated consequence an adopter meets first; it leads the list")
    assert "`work.base`" in gates                 # what they get instead, not merely that it gates


# ------------ #1645: the divergence table that called itself total, and the set that would know
#
# `feature_sync._WORDING`'s comment asserted the table was total AND argued that a partial one was
# safe, because widening `LEDGERED` over a missing row would raise `KeyError` and so fail loudly.
# Both halves were wrong at once: #1565 shipped `SHARD_UNREADABLE` with no row, and the `KeyError`
# came out of `_surface`'s loop — taking the whole pass down, so the operator got a traceback
# instead of the shard path AND lost every other divergence found on the same pass.
#
# `_surface` now degrades. The comment now says so. What is pinned here is the invariant the old
# comment was reaching for, measured against the set that can actually answer it: `DIVERGENCES` is
# NOT that set — it is missing `SHARD_UNREADABLE` and nothing reads it, which is how it went stale
# unnoticed — so the kinds are read off the `_div(...)` call sites instead.

FEATURE_SYNC = (ROOT / "skills" / "sigma-loop" / "scripts" /
                "feature_sync.py").read_text(encoding="utf-8")

#: A divergence as this module raises one: `_div(<CONSTANT>, ...)`. The constant, not the string —
#: every emission site names the module-level constant, which is what lets this resolve them.
_EMITTED = re.compile(r"_div\(\s*([A-Z][A-Z_]+)\s*,")


def _emitted_divergence_kinds():
    """Every divergence kind `feature_sync` actually hands `_div`, resolved to its wire string.

    Derived from the source rather than from `DIVERGENCES`, deliberately: the tuple is the thing
    that was wrong. A test that trusted it would have gone green through the entire #1565 defect."""
    module = _loop_script("feature_sync")
    names = sorted(set(_EMITTED.findall(FEATURE_SYNC)))
    assert names, "no `_div(<CONSTANT>, ...)` call sites found — the regex has drifted"
    return {name: getattr(module, name) for name in names}


def test_every_divergence_the_pass_can_raise_has_a_sentence():
    """The row #1565 forgot, and the next one. Not "every constant" — every kind a call site can
    actually produce, which is the set an operator can be shown."""
    module = _loop_script("feature_sync")
    unworded = sorted(name for name, kind in _emitted_divergence_kinds().items()
                      if kind not in module._WORDING)
    assert not unworded, (
        "feature_sync raises these divergence kinds with no `_WORDING` row, so an operator is told "
        "`<kind>: <detail>` and not what was found or how to repair it: " + ", ".join(unworded))


def test_an_unworded_divergence_costs_one_line_and_not_the_whole_pass():
    """#1645's fix, driven rather than read. A kind with no row must degrade — and, the half that
    actually mattered, the divergences AFTER it must still be surfaced. Asserting only that the
    call did not raise would pass against a `_surface` that swallowed the rest of the list."""
    import contextlib
    import io
    module = _loop_script("feature_sync")
    divergences = [{"kind": "a-kind-no-release-has-worded", "unit": "voice", "repo": "a/b",
                    "detail": "why"},
                   {"kind": module.BRANCH_ABSENT, "unit": "voice", "repo": "a/b",
                    "detail": "feature/voice"}]
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        module._surface("/nonexistent/.sdlc", "1467", divergences, {})
    said = stderr.getvalue()
    assert "a-kind-no-release-has-worded: why" in said          # degraded, not raised
    assert "does not exist on the remote yet" in said, (
        "the divergence after the unworded one was lost — which is the defect, not the ugly line")


def test_the_wording_table_no_longer_claims_a_totality_nothing_enforces():
    """The comment itself, as a literal. The claim came back once by being pasted; the sentence
    that replaced it names the mechanism a reader can check (`.get()`) instead of a guarantee."""
    comment = FEATURE_SYNC[:FEATURE_SYNC.index("_WORDING = {")]
    comment = comment[comment.rindex("#: ONE SENTENCE PER DIVERGENCE"):]
    assert "AND THE TABLE IS TOTAL" not in comment
    assert "`DIVERGENCES` ABOVE IS NOT THE SET TO CHECK A NEW KIND AGAINST" in comment
    assert "template = _WORDING.get(one[\"kind\"])" in FEATURE_SYNC, (
        "`_surface` reads the table with `[]` again, so a missing row takes the pass down and the "
        "comment above `_WORDING` is describing a kit that no longer exists")


# ------------------- #1568/#1645: the withdrawn arm, and the prose it left in three modules
#
# #1568 shipped an arm that asked the remote whether a declared unit's branch already existed and
# refused the pick when it did. #1645 withdrew it — the state it fired on is the state of every
# brand-new unit's first pick under §14's own adoption order, so it refused every first adoption.
#
# The code came back; the prose did not. `promote._feature_hold` carried a present-tense paragraph
# describing the arm as live and its own behaviour as a deliberate DIVERGENCE from it, in a module
# the withdrawal never touched. `_gate_at_pick` kept the arm's justification standing above the
# retraction with no marker. Two test docstrings described a remote read that no longer happens,
# and one of those tests went vacuous without saying so.
#
# What is pinned is the AGREEMENT rather than the wording, because the wording is downstream of it:
# two modules answer the same question about the same input, and while they agree there is no
# divergence for a paragraph to describe.

PROMOTE = (ROOT / "skills" / "sigma-loop" / "scripts" / "promote.py").read_text(encoding="utf-8")


def _adopted_registry_with_no_entry(tmp_path):
    """A project that adopted the model and records nothing under `voice` — the exact state §14
    step 3 leaves behind, one step before the unit is declared."""
    import json
    registry = _loop_script("feature_registry")
    sdlc = tmp_path / ".sdlc"
    (sdlc / "goals").mkdir(parents=True)
    config = {"discovery": {"source": "github", "github": {"repo": "acme/app"}},
              "work": {"remote": "origin"}}
    (sdlc / "config.json").write_text(json.dumps(config), encoding="utf-8")
    registry.registry_dir(str(sdlc)).mkdir(parents=True, exist_ok=True)
    return str(sdlc), config


def test_the_pick_gate_and_the_promotion_agree_on_a_unit_with_no_local_entry(tmp_path):
    """The invariant `promote._feature_hold`'s prose rests on, driven on both sides.

    `/sigma-promote` exists to refuse promotions the next pick would undo, so it must not hold what
    the gate proceeds on, and must not proceed on what the gate holds. While the #1568 arm existed
    they genuinely disagreed and the paragraph naming the divergence was correct; the withdrawal
    made them agree and left the paragraph behind."""
    propagate, promote = _loop_script("feature_propagate"), _loop_script("promote")
    sdlc, config = _adopted_registry_with_no_entry(tmp_path)

    def live_everywhere(_cwd, argv):
        line = " ".join(str(a) for a in argv)
        return ("abc\trefs/heads/feature/voice\n" if "ls-remote" in line
                else "git@github.com:acme/app.git")

    gate = propagate.gate_at_pick(sdlc, None, "1467", config, "voice",
                                  run=live_everywhere, cwd=str(tmp_path), remote="origin")
    assert (gate.proceed, gate.outcome) == (True, propagate.NO_ENTRY)
    assert promote._scope_expansion(sdlc, config, "voice") is None, (
        "the promotion holds a goal the pick would proceed on — the two have diverged again")


def test_the_withdrawn_arm_is_gone_from_the_code_and_from_the_prose():
    """The helper, the remote read, and the paragraph that described them. Literals, because a
    withdrawn behaviour comes back by being pasted from the issue that reported it."""
    assert "_unit_is_already_live" not in PROPAGATE
    gate = _function(PROPAGATE, "_gate_at_pick")
    assert "live_branches" not in gate, "the scope gate reads the remote again (#1568/#1645)"
    assert "if raw is None:\n        return Gate(True, NO_ENTRY, unit, None, False)" in gate
    hold = _function(PROMOTE, "_feature_hold")
    assert "ONE ARM OF THE SCOPE GATE IS DELIBERATELY NOT REPRODUCED" not in hold
    assert "THAT ARM WAS WITHDRAWN" in hold
    assert "See `_feature_hold` for the arm left out" not in PROMOTE


def test_the_gate_records_why_the_obvious_repair_was_tried_and_reverted():
    """Not decoration. #1568 is OPEN against the line the comment sits on, so the next reader is a
    person about to fix it — and the repair they will reach for first is the one that shipped and
    was reverted. The three facts that make that case are pinned; the wording around them is not."""
    gate = _function(PROPAGATE, "_gate_at_pick")
    assert "#1568 IS OPEN AGAINST THIS LINE AND ITS FIX WAS WITHDRAWN" in gate
    assert "§14 pushes the branch FIRST" in gate           # why the arm fired on every adoption
    assert "HOME registry" in gate                         # where the evidence would have to come from


# ------------------- #1649/#1650: what the contract says closes a goal's issue
#
# `docs/branching-model.md` is the document an adopter reads to find out what the model does. It
# described the base a goal is cut from, and never the consequence: GitHub honours a closing keyword
# only on a merge into the DEFAULT branch, so under this model no goal's PR can close its own issue.
# Before #1649 the body claimed one anyway and every issue landed through a feature branch stayed
# open; #1649 stopped the claim and made the merge perform the close. The contract said neither.
#
# §13a now does, and §15 records the one way it fails. Both are pinned against the code by DRIVING
# `_pr_body` on the two bases, because "what a PR body says" is exactly the kind of claim that was
# wrong for a whole release while four surfaces described it three ways.

WORK = (ROOT / "skills" / "sigma-loop" / "scripts" / "work.py").read_text(encoding="utf-8")

_PR_CONFIG = {"discovery": {"source": "github", "github": {"repo": "acme/app"}},
              "work": {"remote": "origin"}}


def _pr_body_on(base):
    """The PR body `work` writes for goal 1467 on `base`, with `main` as the default branch."""
    work = _loop_script("work")

    def run(_cwd, argv):
        return "main" if "default_branch" in " ".join(str(a) for a in argv) else ""

    return work._pr_body(".", run, "origin", base, "1467", _PR_CONFIG)


def _section_13a():
    return BRANCHING[BRANCHING.index("### 13a."):BRANCHING.index("## 14. Adopting it")]


def test_section_13a_matches_the_two_bodies_the_code_actually_writes():
    """The §13a claim, driven on both sides of the branch it turns on. The negative half is the
    load-bearing one: a `Closes` anywhere in the feature-branch body is the defect #1649 fixed,
    and it would read as harmless to anyone not holding this distinction."""
    assert "Closes #1467" in _pr_body_on("main")
    feature = _pr_body_on("feature/voice")
    assert "Refs #1467" in feature
    assert "Closes #" not in feature and "Fixes #" not in feature, (
        "a PR into a non-default base claims a close GitHub will not perform (#1649): %r" % feature)
    section = _section_13a()
    assert "Refs #1467" in section and "Closes #1467" in section     # both, as the measured pair
    assert "closing keyword" in section


def test_section_13a_names_the_moment_the_close_happens_and_the_code_still_picks_it():
    """§13a says the close happens when the merge is confirmed, not when the PR is opened or armed.
    That distinction is the whole safety argument — an armed `--auto` may never land — so it is
    asserted against the call site rather than against the docstring beside it."""
    section = _section_13a()
    assert "at the moment it confirms the merge landed" in section
    assert "was still open after the merge" in section and "sent a close request" in section
    assert "could not close #N" in section
    merge = _function(WORK, "merge")
    assert "closed = _close_issue_the_base_cannot(config, rec, goal, run)" in merge
    armed = merge[:merge.index("_close_issue_the_base_cannot")]
    assert '"--auto"' in armed, "the arm branch must still precede and return before the close"


def test_section_13a_explains_why_the_close_is_what_releases_a_dependent():
    """The reason an operator cares. #1649 and #1650 compound: without the close a blocker stays
    open, and #1650's refresh has nothing to see. §13a is the only place the contract joins them."""
    section = _section_13a()
    assert "Blocked by: #N" in section
    assert "re-reads" in section and "TTL" in section
    loop = (ROOT / "skills" / "sigma-loop" / "scripts" / "loop.py").read_text(encoding="utf-8")
    assert 'if holding and not cache["live"] and not cache.get("hold_refresh"):' in loop, (
        "the hold no longer forces a refresh before reporting (#1650), so §13a's 'on the very next "
        "pick' is false again")


def test_section_15_records_the_close_that_lands_a_merge_and_leaves_an_issue_open():
    """The gap §15 exists to catalogue -- now the retry-and-park recovery, not a bare "reported
    once and never retried" (#2615's plan-review round 3, finding 1, and its own rev 5 §15 sync):
    `record done`'s own `source.complete()` retries the merge-time close whenever the issue is
    still open, and `_record` parks the goal, never silently drops the local record, if that retry
    fails too. It is only a limitation while BOTH the merge-time close and that retry fail, so the
    fail-open (never fail-silent) shape is asserted, not assumed. Scoped to §15's OWN text, not
    `_section_15()`'s unbounded-to-end-of-document slice -- §16, §17 and §18 all follow §15 in this
    doc today, so that helper's slice also carries their text, and a phrase that drifted into one
    of those later sections would keep passing this test while no longer describing §15 itself."""
    section = BRANCHING[BRANCHING.index(_SECTION_15):
                         BRANCHING.index("## 16. Feature-level priority")]
    assert "record done" in section
    assert "retries" in section
    assert "parks" in section
    assert "§13a" in section
    close = _function(WORK, "_close_issue_the_base_cannot")
    assert "except Exception" in close, (
        "the close can now raise, so a landed merge can be reported as a failure and §15's bullet "
        "no longer describes the kit")


def test_section_15_names_the_registry_committer():
    """#1565's second half. The clobber fix (below) stops an unreadable shard from being replaced;
    it does not make anything commit the registry, and the doc used to say nothing about who does
    — "someone has to own staging the registry" is the issue's own words for a gap that carried no
    owner. Pinned as the concrete gesture and the reason automating it is undesigned rather than
    merely undone, because a reader who is only told "someone should commit this" has learned
    nothing actionable."""
    section = _section_15()
    flat = " ".join(section.split())          # the doc is hard-wrapped; match the sentence, not the line
    assert "The committer is the adopting repository's own maintainer, never Sigma" in flat
    assert "git add .sdlc/features && git commit" in flat
    assert "§8e" in section, "the section does not say which lock the git commit is NOT covered by"


def test_the_committer_claim_is_still_true_of_commit():
    """The doc says `work.commit()`'s `git add -A` never reaches the registry — checked against the
    function itself, not assumed, the same way `test_section_15_says_where_the_propagated_write_
    actually_lands` holds its own claim to `feature_propagate`. If `commit()` ever learns to stage
    `.sdlc/features` from the worktree (it structurally cannot reach the root checkout's copy
    anyway — see the module docstring), this is the assertion that goes red first."""
    body = _function(WORK, "commit")
    assert "features" not in body, (
        "work.commit() now mentions the registry; §15's claim that nothing here stages or commits "
        "it needs to be re-checked, not just re-read")


# ------------------- #1565: §8c described a write that raises, and not the one that refuses
#
# §8c is titled "Reading never fails. Writing fails on exactly one thing." That was true, and it
# stopped being the whole truth when #1565 gave the writer a second failure mode with a different
# SHAPE: `amend` REFUSES over an unreadable shard rather than raising, leaving the file untouched.
#
# The distinction is the point. To a reader an unreadable shard and an absent one are the same
# answer ("that unit is not known"), so a writer that believed the read would replace a hand-written
# entry with a one-goal stub and report success — which is what it did, 3/3, and §12 requires humans
# to hand-edit the file that this happens to.

def test_section_8c_describes_the_refusal_and_the_refusal_still_refuses(tmp_path):
    """Driven end to end against a real malformed shard, because every part of this claim is about
    what is left ON DISK afterwards — which no assertion about a return value can establish."""
    sync, registry = _loop_script("feature_sync"), _loop_script("feature_registry")
    sdlc = tmp_path / ".sdlc"
    units = registry.registry_dir(str(sdlc)) / "units"
    units.mkdir(parents=True)
    shard = units / "voice.json"
    corrupt = '{"title": "Voice", "owner": "@o", "authorized": true,,}'
    shard.write_text(corrupt, encoding="utf-8")

    import contextlib
    import io
    with contextlib.redirect_stderr(io.StringIO()):
        out = sync.amend(str(sdlc), "voice", lambda e: e.setdefault("goals", []).append("1467"))

    assert out["written"] is False and out["changed"] is False
    assert out.get("refused"), "an unreadable shard is being written over again (#1565)"
    assert shard.read_text(encoding="utf-8") == corrupt, "the hand-written entry was replaced"
    assert "Repair or delete that file" in out["refused"], "the refusal names no remedy"

    section = BRANCHING[BRANCHING.index("### 8c."):BRANCHING.index("### 8d.")]
    assert "It REFUSES on one other thing, and refusing is not raising." in section
    assert "byte-identical" in section
    assert "§12" in section, "the section does not say where the malformed JSON comes from"


def test_section_8c_still_names_the_one_thing_the_write_side_raises_on(tmp_path):
    """The original claim, kept honest alongside the new one. `write_unit` raises for a bad NAME and
    for nothing else — if it ever raised on unreadable content too, the refusal above would be
    unreachable and both halves of §8c would be wrong at once."""
    registry = _loop_script("feature_registry")
    features = tmp_path / "features"
    try:
        registry.write_unit(features, "../../etc/passwd", {})
    except ValueError as exc:
        assert "is not a unit name" in str(exc)
    else:
        raise AssertionError("write_unit accepted a name that is not a unit name")
    section = BRANCHING[BRANCHING.index("### 8c."):BRANCHING.index("### 8d.")]
    assert "**The write side raises on exactly one thing** — a name that is not a unit name" in section


# ------------------------------- #1660: how a unit of work is CREATED, and the skill that does it
#
# The contract described, at length, what a unit IS and what the loop does with one. It never said
# how one comes into existence — the whole of that was §14's *Opening a unit*, four raw commands
# under a heading, written for someone pasting them by hand because pasting them by hand was the
# only way. `/sigma-define` makes it not the only way, for three kinds of unit, so §14 now leads with
# the flow and keeps the commands as what the flow runs.
#
# Four claims in the new subsection are falsifiable, and each is held against the thing that decides
# it rather than against the prose beside it:
#
#   * one branch prefix for all three kinds  -> `features.BRANCH_PREFIX`, executed;
#   * the loop never mints a `feature:*` label -> `creates_a_feature_label`, executed on both arms;
#   * the order, and the never-create rule    -> stated ONCE each, elsewhere, and cited from here;
#   * the three kinds and the scope flag      -> the skill's own text and the flag in the code,
#                                                the moment either lands (see the two guards).

_DEFINE_HEADING = "### Defining a unit — the three kinds, and `/sigma-define`"

#: §2's list of what a unit may BE. Copied into §14 to say the three kinds are not a rival to it,
#: so the two copies are compared with whitespace normalised — §2's wraps mid-list.
_WHAT_A_UNIT_MAY_BE = "a feature, a shared bug, a refactor, a contract change"

#: The skill, and the two entry points the `--feature` scope flag lands in. All three are other
#: lanes' files: these tests read them, never write them.
_DEFINE_SKILL = ROOT / "skills" / "sigma-define" / "SKILL.md"
_SCOPED_ENTRYPOINTS = (_LOOP_SCRIPTS / "loop.py", ROOT / "skills" / "sigma-goal" / "SKILL.md")

#: The exclusivity guarantee, in ONE wording. #1663 settled that the contract states it and the two
#: entry points quote it rather than each writing their own, so this constant is the agreement:
#: `--feature` is worth nothing unless it is exclusive, and "exclusive" said three slightly
#: different ways is how a flag ends up preferential on one path and absolute on another.
_SCOPE_GUARANTEE = ("While a `--feature` run is active, no goal outside that unit may be picked.")


def _defining():
    """§14's *Defining a unit*, sliced between its heading and *Opening a unit* below it."""
    assert _DEFINE_HEADING in BRANCHING, "§14's *Defining a unit* is gone"
    start = BRANCHING.index(_DEFINE_HEADING)
    return BRANCHING[start:BRANCHING.index(_OPENING_HEADING, start)]


def _defining_flat():
    """The same slice, whitespace-normalised — the form every PROSE needle below is matched against.

    The document is hard-wrapped, so a sentence's line breaks move whenever a word ahead of it
    changes. A literal pin taken against the wrapped bytes fails on a reflow that changed nothing,
    which teaches the next editor to delete the pin rather than to fix the prose."""
    return " ".join(_defining().split())


def test_the_creation_flow_is_documented_and_sits_before_the_gesture_it_performs():
    """Order on the page is the claim, the same way it is for the rollout block. A reader who meets
    four shell commands first has already decided how units get made by the time the flow is
    mentioned."""
    assert BRANCHING.index("## 14.") < BRANCHING.index(_DEFINE_HEADING)
    assert BRANCHING.index(_DEFINE_HEADING) < BRANCHING.index(_OPENING_HEADING)
    assert BRANCHING.index(_OPENING_HEADING) < BRANCHING.index(_SUMMARY_HEADING)


def test_the_raw_gesture_no_longer_reads_as_the_normal_path():
    """The other half of it. *Opening a unit* is still the exact commands — but it names the flow
    that runs them BEFORE the first of them, so the anchor `how-branching-works.md` links straight
    into does not land a reader in the middle of the fallback believing it is the route."""
    opening = _opening()
    assert "/sigma-define" in opening
    assert opening.index("/sigma-define") < opening.index("git push"), (
        "*Opening a unit* reaches its first command before it says what performs them")


def test_the_three_kinds_are_named_and_are_the_whole_set():
    """A kind a reader cannot find is a kind they invent. All three, plus the totality — without
    which "feature, bug, refactor" reads as three examples of an open list."""
    defining = _defining()
    for kind in ("`feature`", "`bug`", "`refactor`"):
        assert kind in defining, f"§14's kinds table omits {kind}"
    assert "Three is the whole set" in _defining_flat()


def test_the_kinds_do_not_quietly_replace_section_2s_list_of_what_a_unit_may_be():
    """Two closed-looking lists of different lengths in one document is how a reader concludes the
    document contradicts itself. §2 answers *what may be a unit*; the kinds answer *which of three
    this is*. The copy is compared to §2's own, normalised, because §2's wraps mid-list."""
    defining = _defining()
    assert _WHAT_A_UNIT_MAY_BE in _defining_flat(), \
        "§14 no longer quotes §2's list before narrowing it"
    normalised = " ".join(BRANCHING.split())
    assert normalised.count(_WHAT_A_UNIT_MAY_BE) == 2, (
        "§2's list and §14's copy of it have drifted apart, or a third copy has appeared")
    assert "not a second list of what may be" in _defining_flat()
    assert "§2" in defining


def test_no_kind_gets_a_branch_prefix_of_its_own():
    """The claim most likely to be got wrong by a reader skimming a kinds table, so it is pinned in
    both directions: the code's single constant, executed, and the document's refusal to spell any
    other prefix in front of a unit name anywhere — including in a fenced command someone copies."""
    features = _loop_script("features")
    assert features.BRANCH_PREFIX == "feature/", (
        "the branch prefix is no longer a single constant; §14's claim that the kind does not steer "
        "the branch name is now unsourced")
    assert not re.search(r"refs/heads/(?!feature/)", BRANCHING), (
        "docs/branching-model.md tells someone to push a branch that is not a unit branch")
    assert not re.search(r"\b(?:bug|refactor)/<", BRANCHING), (
        "docs/branching-model.md spells a per-kind branch prefix")
    assert "there is no `bug/` branch and no `refactor/` branch" in _defining_flat()
    assert "`features.BRANCH_PREFIX`" in _defining()      # named, so the claim is checkable


def test_the_order_requirement_is_cited_from_the_new_subsection_and_not_restated():
    """This repo's most-repeated defect: one rule, written down twice, drifting into two rules. The
    order is §14's introduction's to state — it is the half carrying the measurement — so the
    subsection that USES it must link to it and stop."""
    defining = _defining()
    assert BRANCHING.count("is a requirement, not a suggestion") == 1, (
        "the order requirement now has two statements and one measurement")
    assert "is a requirement, not a suggestion" not in defining
    assert "*Opening a unit*" in defining                 # cited, by name
    assert "neither the order nor the reason is restated here" in _defining_flat()


def test_the_never_create_rule_is_cited_from_the_new_subsection_and_not_restated():
    """Same shape, different rule. §7 owns "Sigma never creates a `feature:` label" and the
    structural argument under it; §14 may say who the human is, and may not say the rule again."""
    defining = _defining()
    assert BRANCHING.count("never creates a `feature:` label") == 1
    assert "never creates a `feature:` label" not in defining
    assert "§7" in defining
    assert "The distinction is who chose the name**, never which process typed the command" \
        in _defining_flat()


def test_the_boundary_the_new_subsection_draws_is_still_the_boundary_in_the_code():
    """`/sigma-define` creating a label does not contradict §7, and the reason is that §7's refusal
    is scoped to the GitHub source's own runner. Executed on both arms, because the claim is about
    what that predicate answers — and a doc sentence about a predicate is exactly the kind of thing
    that stays on the page after the predicate stops agreeing with it."""
    feature_labels = _loop_script("feature_labels")
    assert feature_labels.creates_a_feature_label(
        ["label", "create", "feature:voice-interview", "--color", "1d76db"]) is True
    assert feature_labels.creates_a_feature_label(
        ["label", "create", "sdlc:goal", "--color", "1d76db"]) is False
    runner = _function(
        (_LOOP_SCRIPTS / "sources.py").read_text(encoding="utf-8"), "_run")
    assert "feature_labels.creates_a_feature_label(args)" in runner, (
        "the refusal has left the GitHub source's runner; §14's claim that `/sigma-define` is "
        "outside the scope of §7's rule no longer follows from anything")


def test_the_scope_flag_is_documented_as_a_selection_filter_and_nothing_more():
    """`--feature <name>` is added in sibling lanes (#1661, #1663) and stated here. Four things have
    to be on the page, and each is a way the flag has been got wrong somewhere before: the
    guarantee in the settled wording, that it is exclusive rather than preferential, that
    membership is the declaration pair rather than the label a member does not have yet, and what
    happens to the goals it excludes."""
    defining = _defining()
    flat = _defining_flat()
    assert "`--feature <name>`" in defining
    assert _SCOPE_GUARANTEE in flat, "§14 no longer states the guarantee in the settled wording"
    assert "/sigma-loop" in defining and "/sigma-goal" in defining
    assert "Exclusive, not preferential" in flat
    assert "never the label alone" in flat, (
        "§14 does not say membership is the declaration pair; scoping by label alone skips every "
        "member that has not been picked yet, which is most of them")
    assert "left untouched, unlabelled and uncommented-on" in flat, (
        "§14 does not say what a scoped run does to the goals it excludes, which is the half an "
        "operator needs before trusting it")


def test_the_scope_flag_the_contract_documents_is_the_flag_that_shipped():
    """Doc-vs-code, guarded on the code because the two land in different lanes of the same branch.

    The guard is one-directional on purpose: the moment `--feature` exists in either entry point
    this becomes an ordinary assertion, and it can never pass by the flag quietly disappearing —
    that direction is `_defining()`'s unconditional assertions above. What it compares is the
    GUARANTEE rather than the flag's spelling, byte for byte, because the two entry points agreed
    to quote this document instead of paraphrasing it, and a paraphrase is invisible to a test that
    only looks for `--feature`."""
    shipped = [p for p in _SCOPED_ENTRYPOINTS
               if p.exists() and "--feature" in p.read_text(encoding="utf-8")]
    if not shipped:
        import pytest
        pytest.skip("--feature has not landed in loop.py or sigma-goal yet (sibling lanes of #1660)")
    assert "`--feature <name>`" in _defining()
    for path in shipped:
        text = " ".join(path.read_text(encoding="utf-8").split())
        assert _SCOPE_GUARANTEE in text, (
            f"{path.relative_to(ROOT)} takes --feature and states its guarantee in words other "
            f"than the contract's; #1663 settled that there is one wording")


def test_the_contract_and_the_skill_agree_on_the_three_kinds():
    """The other cross-lane pin. `skills/sigma-define/` is another lane's file and is read, never
    written — but the day it lands, its kinds and the contract's have to be the same three, and it
    must not have invented a per-kind branch prefix that §14 says does not exist."""
    if not _DEFINE_SKILL.exists():
        import pytest
        pytest.skip("skills/sigma-define/SKILL.md has not landed yet (sibling lane of #1660)")
    skill = _DEFINE_SKILL.read_text(encoding="utf-8")
    for kind in ("feature", "bug", "refactor"):
        assert kind in skill, f"the skill does not offer the `{kind}` kind §14 documents"
    assert not re.search(r"\b(?:bug|refactor)/", skill), (
        "skills/sigma-define mints a per-kind branch prefix; §14 says the prefix does not vary")


def test_the_walkthrough_routes_to_the_creation_flow():
    """`how-branching-works.md` is where someone with an issue and no context actually starts, and
    Stage 0 is the only stage about a unit that does not exist yet. The anchor itself is resolved by
    `test_every_section_the_walkthrough_cites_actually_exists`; this is that the route is there at
    all, and that the walkthrough still carries none of the rules it routes to."""
    stage0 = WALKTHROUGH[WALKTHROUGH.index("## Stage 0"):WALKTHROUGH.index("## Stage 1")]
    assert "/sigma-define" in stage0
    assert _CITE.search(stage0)
    assert _slug(_DEFINE_HEADING) in _CITE.findall(WALKTHROUGH)


# ============================================================ #1823: the Dossier pipeline's docs
#
# The pipeline shipped across six issues and its only prose home was `docs/proposals/
# dossier-pipeline.md` — a document written BEFORE any of it was built, whose own closing section
# listed five decisions it "deliberately leaves open". All five were subsequently made, three of
# them differently from where it pointed, and every skill in the tree cited it by section number
# anyway. A proposal that outlives its own build is not documentation, it is a second source of
# truth that nothing compares against the first.
#
# So the proposal was PROMOTED, exactly as `branching-model.md` was: one contract at
# `docs/dossier-pipeline.md`, one route through it at `docs/how-the-dossier-pipeline-works.md`, and
# the proposal DELETED rather than left beside them. These pins hold all three halves of that:
#
#   * the contract exists and says it is shipped, not proposed,
#   * the walkthrough is subordinate to it — mechanically, the same two-direction test
#     `test_the_walkthrough_carries_no_rule_of_its_own` applies to the branching pair,
#   * and no stale reference to the proposal's old path survives anywhere in the tree,
#     which is the one failure that would recreate the two-documents problem by citation alone.

DOSSIER_PATH = ROOT / "docs" / "dossier-pipeline.md"
DOSSIER_WALK_PATH = ROOT / "docs" / "how-the-dossier-pipeline-works.md"
DOSSIER = DOSSIER_PATH.read_text(encoding="utf-8")
DOSSIER_WALK = DOSSIER_WALK_PATH.read_text(encoding="utf-8")

#: The path the proposal used to live at. Nothing in the tree may still name it: the file is gone,
#: so every surviving citation is a dead link that also implies a document a reader should go and
#: consult. Written in two pieces so this constant does not itself match the scan below.
_DEAD_PROPOSAL = "docs/proposals/" + "dossier-pipeline.md"

#: Extensions and directories the dead-reference scan walks. Wider than `_RULE_SCAN_SKIP`'s: this
#: one DOES walk `tests/`, because a stale path in a test's own docstring sends the next reader to
#: the same missing file.
_DEAD_SCAN_SKIP = frozenset({".git", ".claude", ".sdlc", ".venv", "venv", "__pycache__"})

#: How a link into the contract is written from the walkthrough.
_DOSSIER_CITE = re.compile(r"\]\(dossier-pipeline\.md#([^)]+)\)")

#: The floor on routing, chosen well below what the walkthrough ships with — same reasoning as
#: `_LINK_FLOOR` one block up: ordinary editing must not trip it, gutting the links must.
_DOSSIER_LINK_FLOOR = 40

#: Phrases `docs/dossier-pipeline.md` states NORMATIVELY — the rules, thresholds and refusals a
#: walkthrough is most tempted to "just mention", plus the ones pinned by name in the assertions
#: below. A copy of any of them in the route would be a rule with two homes and one test.
#: Matched case-insensitively, because a lower-cased paste is the same paste.
_DOSSIER_NORMATIVE_ELSEWHERE = (
    # the intake stage's own bounds
    "the cap applies to the answered half only",
    "an unclassified extra would dodge the cap",
    "engine-owned data",
    "at most 3",
    # the design pass's stopping rule and its publish route
    "exhaustive over the seeds, not over the repo",
    "the ceiling a pass obeys is read back rather than remembered",
    "one quiet round is the signal",
    "0 means the path is ignored",
    "never `git add -f`",
    "design the target, not the meta-issue",
    # the gate
    "fail-open before the label read, fail-closed after it",
    "park-and-defer",
    "lower-number-wins",
    "dedup is off deliberately",
    # the confirmation gate
    "the maker is never the checker",
    "there is no third verdict because the label has no third value",
    "the test is operational, not a feeling",
    "a guard nobody has watched fail is decoration",
    "sequencing belongs in `blocked_by` edges",
    "the only place `sdlc:designed` is ever written",
    "pure add",
    "the comment is the entire output",
    "a design blocker is not a slice",
    # the tickets, the unit question and the labels
    "stamp first and find out afterwards",
    "no deletion path",
    "minted at attach time, not bootstrapped",
    "never a raw label edit",
    "priority is omitted, deliberately",
    # and the rule that has one home for the whole repository
    RULE,
)


def _dead_reference_files():
    """Every shipped file still naming the deleted proposal, DISCOVERED rather than listed.

    Same reasoning as `_files_stating_the_rule`: a hand-written list of the citation sites is right
    on the day it is written and silently wrong the day somebody adds one back."""
    found = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.suffix not in (".md", ".py", ".tmpl", ".mdc", ".json"):
            continue
        rel = path.relative_to(ROOT)
        if _DEAD_SCAN_SKIP & set(rel.parts):
            continue
        if _DEAD_PROPOSAL in path.read_text(encoding="utf-8", errors="ignore"):
            found.append(rel.as_posix())
    return found


# ------------------------------------------------------------------ the contract exists, and ships

def test_the_dossier_contract_exists():
    assert DOSSIER_PATH.exists(), "docs/dossier-pipeline.md is missing"
    assert DOSSIER.startswith("# Sigma's Dossier pipeline")


def test_the_dossier_contract_declares_itself_shipped_not_proposed():
    """The single most load-bearing line in the promotion. `branching-model.md`'s own header is the
    precedent: **Audience:** / **Status:**, and a status that says the levels described are shipped.
    A document still saying PROPOSAL is one nobody has to obey."""
    header = DOSSIER[:DOSSIER.index("## 1.")]
    assert "**Audience:**" in header
    assert "**Status:**" in header
    # (#277: the previous name's release number is gone; the claim is that it ships, in this plugin)
    assert "**Status:** shipped." in header
    assert "PROPOSAL" not in header
    assert "**This is a contract, not a tour.**" in header


def test_the_dossier_contract_says_the_code_won_where_the_two_disagreed():
    """`branching-model.md` makes this claim in its own header and then keeps it in §15. The
    equivalent here is §12's table, which is the whole reason the proposal could be deleted rather
    than kept 'for the reasoning'."""
    assert "where the two disagreed, the code won" in DOSSIER
    limits = DOSSIER[DOSSIER.index("## 12."):]
    for settled in ("the Q&A shape", "the `goal_design` config shape", "park-and-defer"):
        assert settled in limits, f"§12 no longer records the proposal's settled decision: {settled}"


def test_the_dossier_contract_has_an_honest_limitations_section():
    """Every contract in `docs/` carries one, and it is the section that makes the rest credible."""
    assert "## 12. Honest limitations, and the gaps that are deliberate" in DOSSIER
    limits = DOSSIER[DOSSIER.index("## 12."):]
    # The three a reader is worst off not knowing, each measured elsewhere in this tree.
    assert "OFF by default" in limits            # the coverage claim is conditional
    assert "no resume" in limits                 # an interrupted intake starts over
    assert "prose, not edges" in limits          # the back-references nothing reads


def test_the_dossier_contract_draws_the_tiers_as_a_box_diagram():
    """`branching-model.md` §2 draws its branch shape; the taxonomy here is three tiers and the
    thing readers get wrong is which of them may be absent. Pinned as the DIAGRAM, because the
    prose around it is what an editor rewrites."""
    tiers = DOSSIER[DOSSIER.index("## 3."):DOSSIER.index("## 4.")]
    assert "┌" in tiers and "└" in tiers, "§3's tier boxes are gone"
    for tier in ("DOSSIER / STORY", "SPEC / EPIC", "SLICES / TECH GOALS"):
        assert tier in tiers, f"§3 no longer names the {tier} tier"
    assert "MAY NOT EXIST AT ALL" in tiers, (
        "§3 no longer says the Epic tier can be absent — the one shape a reader gets wrong")


def test_the_contract_mirrors_the_sweep_field_the_skill_mandates():
    """CROSS-FILE (#1952). `goal-design` §5 makes `Sweep: converged | capped` a mandatory header
    field and `goal-review` §1 reads it; §5d is the contract's own copy of that skeleton, and a
    skeleton missing the field is a second schema that disagrees with the first."""
    goal_design = GOAL_DESIGN_ARTIFACT   # #2107: this content lives wholesale in the reference file
    assert "**Sweep**" in goal_design, "the skill no longer mandates the Sweep field"
    schema = DOSSIER[DOSSIER.index("### 5d."):DOSSIER.index("### 5e.")]
    assert "**Sweep**" in schema, "§5d's skeleton omits the Sweep field the skill mandates"
    assert "converged" in schema and "capped" in schema


def test_the_contract_mirrors_the_lane_field_the_skill_mandates():
    """CROSS-FILE (#1954), the same shape as the `Sweep` pin above. `goal-design` §5 makes `Lane`
    mandatory so a short artifact can be read as proportionate rather than lazy; §5d is the
    contract's own copy of that skeleton, and a skeleton missing the field is a second schema
    disagreeing with the first."""
    goal_design = GOAL_DESIGN_ARTIFACT   # #2107: this content lives wholesale in the reference file
    assert "**Lane**" in goal_design, "the skill no longer mandates the Lane field"
    schema = DOSSIER[DOSSIER.index("### 5d."):DOSSIER.index("### 5e.")]
    assert "**Lane**" in schema, "§5d's skeleton omits the Lane field the skill mandates"
    for value in ("small", "medium", "large"):
        assert value in schema, "§5d does not name the %r lane" % value


def test_the_contract_mirrors_the_budget_field_the_skill_mandates():
    """CROSS-FILE (#2032), the same shape as the `Sweep` and `Lane` pins above. `goal-design` §5
    makes `Budget` a mandatory header field -- the fetched sweep-round ceiling, never a hand-typed
    one -- and §5d is the contract's own copy of that skeleton."""
    goal_design = GOAL_DESIGN_ARTIFACT   # #2107: this content lives wholesale in the reference file
    assert "**Budget**" in goal_design, "the skill no longer mandates the Budget field"
    schema = DOSSIER[DOSSIER.index("### 5d."):DOSSIER.index("### 5e.")]
    assert "**Budget**" in schema, "§5d's skeleton omits the Budget field the skill mandates"


def test_the_contract_mirrors_the_in_brief_requirement_the_skill_mandates():
    """CROSS-FILE (#2064), the same shape as the `Sweep`/`Lane`/`Budget` pins above. `goal-design`
    §5 makes `<n>-in-brief.md` a required sibling artifact, held to rules the main document is
    exempt from; §5d is the contract's own copy of that requirement, and both must agree it is
    required rather than one softening it into a courtesy."""
    goal_design = GOAL_DESIGN_ARTIFACT   # #2107: this content lives wholesale in the reference file
    assert "in-brief.md` is a required sibling" in goal_design, \
        "the skill no longer requires the in-brief companion artifact"
    schema = DOSSIER[DOSSIER.index("### 5d."):DOSSIER.index("### 5e.")]
    assert "in-brief.md` is a required sibling" in schema, \
        "§5d no longer requires the in-brief companion artifact the skill mandates"


def test_the_contract_mirrors_the_comment_carrying_the_in_brief_not_the_citations():
    """CROSS-FILE (#2064). The skill's comment rule stopped mandating 'every doubt and blocker in
    full' -- reproducing the main artifact's own file:line citation register in the one place most
    readers will ever look -- and told the author to carry the in-brief's plain-language content
    instead. §5e is the contract's own copy of that same comment rule."""
    goal_design = GOAL_DESIGN_ARTIFACT   # #2107: this content lives wholesale in the reference file
    comment = goal_design.split("Then link it from the source issue's own comments", 1)[1]
    assert "in-brief.md" in comment.split("Measure whether the file can reach", 1)[0], \
        "the skill's comment rule no longer says to carry the in-brief's content"
    schema = DOSSIER[DOSSIER.index("### 5e."):DOSSIER.index("### 5f.")]
    substance = schema.split("stands on its own", 1)[1].split("Measure whether the file", 1)[0]
    assert "in-brief.md" in substance, \
        "§5e's own comment-substance bullet no longer says to carry the in-brief's content"


def test_the_contract_says_a_one_slice_design_still_produces_a_ticket():
    """#1954's real blocker. §7d-ii said "skip the Epic entirely; `sdlc:designed` alone is the whole
    output" -- correct on the retrofit path, where `#<n>` IS the tech goal, and fatal on the Dossier
    path, where `#<n>` is `story`-labelled and nothing may ever pick it. The light path lands here,
    so it was unreachable by construction."""
    section = DOSSIER[DOSSIER.index("**ii. If the design concludes"):DOSSIER.index("**iii.")]
    flat = " ".join(section.split()).replace("*", "")
    assert "not the same as no ticket" in flat, \
        "§7d-ii still reads as producing nothing at all for a one-slice design"
    assert "Retrofit path" in section and "Dossier path" in section, \
        "§7d-ii does not split by path, which is the only thing that differs between the two"
    assert "2 tickets" in flat, "§7d-ii states no arithmetic for the light outcome"


def test_the_contract_mirrors_the_null_epic_resolution_the_skill_resolved():
    """CROSS-FILE (#1954), the same shape as the `Sweep` and `Lane` pins above. §9 and
    `goal-review` §5 resolve the SAME `<epic>` for the SAME question, and the light path is exactly
    where the old reading breaks: 4c's Dossier branch RUNS 4b, so "slicing happened", and
    `report["epic"]` still comes back `null` -- leaving the promotion question pointed at nothing.
    The skill was corrected to key off the report rather than off which branch ran; a contract that
    still keys off the branch is a second answer to a question that has one."""
    skill = skill_corpus("sigma-goal-review")   # #2108: SKILL.md + references/*.md
    resolution = " ".join(skill.split("## 5. Feature-ification", 1)[-1][:800].split())
    assert "where a plan produced one" in resolution, \
        "the skill's own resolution moved -- re-check this pin"
    nine = DOSSIER[DOSSIER.index("## 9. Feature-ification"):DOSSIER.index("## 10.")]
    flat = " ".join(nine.split())
    assert "if slicing happened" not in flat, \
        "§9 still resolves `<epic>` by whether slicing ran, which a one-row plan makes ambiguous"
    assert "null" in flat, \
        "§9 never covers the null-epic case the light path produces, so it answers only one of two"


def test_the_walkthrough_states_the_light_path_without_restating_the_rule():
    """The route doc is where a first-time reader meets the seven-ticket listing, so it is where
    'that is what THIS idea cost' has to be said. `test_the_walkthrough_carries_no_rule_of_its_own`
    is the other half: it may point at the rule, never repeat it."""
    assert "not what the door charges" in DOSSIER_WALK, \
        "the walkthrough leaves the seven-ticket listing as the only shape a reader sees"
    light = DOSSIER_WALK.split("not what the door charges", 1)[1][:1200]
    assert "dossier-pipeline.md#5b" in light and "dossier-pipeline.md#7d" in light, \
        "the walkthrough asserts the light path without routing to the sections that define it"


def test_every_contract_copy_of_the_outcome_comment_carries_the_sweep_marker():
    """CROSS-SECTION (#1952). §7d-v mandates the marker on the outcome comment, but §7c is where
    the contract writes that comment out in full -- and a template written in full without the
    marker is a contract that contradicts itself at the exact site #1952 asked the fact be
    carried. Every literal copy has to carry it, wherever a later section adds one. The
    walkthrough's
    own copy is deliberately NOT checked: it quotes what one real run actually commented before
    this rule existed, and editing a transcript to match a later rule would make it a fiction."""
    flat = re.sub(r"\s+", " ", DOSSIER)
    where = [m.start() for m in re.finditer(r"goal-review: CONFIRMED \(", flat)]
    assert where, "the contract no longer writes the template literally -- re-check this pin"
    bare = [flat[i:i + 150] for i in where if "sweep:" not in flat[i:i + 150]]
    assert not bare, "contract copies of the outcome comment with no sweep marker: %r" % bare


def test_the_contract_mirrors_the_doubt_and_blocker_ids():
    """CROSS-FILE (#1955). The ids are cited by `goal-review` 2a and recognised by
    `compile_plan._DESIGN_ARTIFACT_ID_RE`; a contract that does not mandate them leaves the one
    document a human reads silent about where they come from."""
    schema = DOSSIER[DOSSIER.index("### 5d."):DOSSIER.index("### 5e.")]
    assert "D-1" in schema and "B-1" in schema, "§5d mandates no ids on Doubts/Blockers"
    assert "Component" in schema, "§5d's blast-radius table has no component column"


def test_the_contract_says_which_path_resolves_goal_design_mode():
    """CROSS-FILE (#1958). `design_check` returns OFF before `mode` is read, so on the Product path
    nothing in the code reads it; §5b and §6a are where that has to be stated."""
    depth = DOSSIER[DOSSIER.index("### 5b."):DOSSIER.index("### 5c.")]
    assert "Product" in depth and "retrofit" in depth
    assert "design-check" in depth or "design_check" in depth, \
        "§5b never says which path resolves mode and which does not"


def test_the_dossier_contract_pins_the_same_artifact_headings_the_skill_mandates():
    """CROSS-FILE, and deliberately. `skills/sigma-goal-design/SKILL.md` step 5 pins the artifact's
    heading schema and `tests/test_sdlc_goal_design_skill.py` holds it there; the contract restating
    a DIFFERENT set would be a second schema with no test comparing them. This is that comparison."""
    goal_design = GOAL_DESIGN_ARTIFACT   # #2107: this content lives wholesale in the reference file
    # Both ends read out of the SKELETON each file presents as copyable, not out of the whole
    # document: a heading discussed in surrounding prose but absent from the skeleton is exactly
    # the drift this pin exists to catch, and a whole-file search cannot see it. Control: deleting
    # `## Out of scope` from the contract's §5d skeleton passed the earlier whole-file version,
    # because §5c and §5d's own prose name the heading four times between them.
    skill_skeleton = goal_design.split("```markdown", 1)[1].split("```", 1)[0]
    contract_skeleton = DOSSIER[DOSSIER.index("### 5d."):DOSSIER.index("### 5e.")]
    contract_skeleton = contract_skeleton.split("```markdown", 1)[1].split("```", 1)[0]
    for heading in ("## Intent", "## Premise check", "## Queries run", "## Seeds",
                    "## Blast radius",
                    "## Components", "## Out of scope", "## Doubts",
                    "## Blockers", "## Detailed design", "## Slice-count estimate", "## Handoff"):
        assert heading in skill_skeleton, f"the skill's skeleton no longer mandates {heading}"
        assert heading in contract_skeleton, \
            f"the contract's §5d skeleton omits the mandated heading {heading}"


def test_the_contract_mirrors_the_auditable_scope_call():
    """CROSS-FILE (#1975). `converged` is the pipeline's own completeness claim, and §5c is the one
    document a human reads to learn what it means. A contract that still describes the quiet round
    as a judgement the pass makes for itself leaves the honour system in the authoritative text
    while only the skill was fixed."""
    stopping = DOSSIER[DOSSIER.index("### 5c."):DOSSIER.index("### 5d.")]
    flat = " ".join(stopping.split()).replace("*", "")
    assert "AS WRITTEN" in flat, "§5c still judges the quiet round on an unrecorded scope call"
    assert "the round it was excluded in" in flat, "§5c's exclusions do not carry their round"
    assert "visible and disputable, not correct" in flat, \
        "§5c overclaims what recording the call buys"
    assert "## Out of scope" in stopping, "§5c names no heading for the exclusions"


def test_the_contract_mirrors_the_missing_section_being_a_violation():
    """CROSS-FILE (#1975). §7a is where the contract tells the reviewer what an ABSENT heading
    means, and it already says it for `Premise check`. Saying it for only one of the two headings
    §5d added leaves the other's omission reading as "nothing was excluded" in the authoritative
    text. Scoped to §7a: §5c and §5d both carry `Out of scope` and neither is this instruction."""
    reading = DOSSIER[DOSSIER.index("### 7a."):DOSSIER.index("### 7b.")]
    flat = " ".join(reading.split()).replace("*", "")
    assert "so is a missing `Out of scope`" in flat, \
        "§7a makes an absent Premise check a violation and an absent Out of scope good news"


def test_the_contract_mirrors_the_premise_check():
    """CROSS-FILE (#1976). The contract is where a falsified premise is either a first-class outcome
    of Stage 1 or an editorial aside; §5a is where the hypothesis rule already lives, so it is where
    the record half has to live too."""
    paths = DOSSIER[DOSSIER.index("### 5a."):DOSSIER.index("### 5b.")]
    flat = " ".join(paths.split()).replace("*", "")
    assert "## Premise check" in paths, "§5a verifies claims and still records nothing"
    for verdict in ("verified", "falsified", "unverifiable"):
        assert verdict in flat, f"§5a's premise check has no {verdict!r} verdict"
    assert "settled and different" in flat, \
        "the contract does not say why a falsified premise is neither a Doubt nor a Blocker"


# ------------------------------------------------ #1977: the REJECT branch, documented as an outcome
#
# The measured re-run (#1973 against #1942, same idea, same board, only the pipeline changed) ended
# in a REJECTED verdict: no `sdlc:designed`, no Epic, no children. That was the contract holding —
# B-1 was sustained on §7b's operational test after three conflicting board resolvers were verified
# in the tree, and 5 of 6 slices genuinely could not start. But the observable outcome is "an idea
# went in and no tickets came out", and neither document walked it: the contract named REJECT in one
# bullet of §7c and the walkthrough in one line of "when it stops and wants a human", while the only
# SHAPE either of them drew end to end was the happy one, finishing in an Epic and four children.
#
# An adopter meets that outcome on a first real idea, which is when adoption is decided. So the
# branch is now walked on both sides — §7g in the contract, and the second real run in the route —
# and these pins hold the four things a reader has to get from it: what a REJECT writes, what it
# deliberately does not, why zero tickets is the honest answer rather than a failure, and what
# discharges it.

def test_the_contract_walks_the_reject_branch_rather_than_naming_it():
    """§7c already listed REJECT as one of two verdicts. What it never said is what the verdict
    DOES — and a verdict whose only description is its name is exactly how "zero tickets" reads as
    a broken tool. Pinned as the four halves rather than as prose, because prose is what an editor
    rewrites and any one of the four missing puts the reader back where they started."""
    assert "### 7g. On REJECT" in DOSSIER, "the contract has no REJECT branch section"
    section = DOSSIER[DOSSIER.index("### 7g."):DOSSIER.index("## 8. The tickets")]
    flat = " ".join(section.split())
    # i — what it writes, and the four things it does not
    assert "loop.py note .sdlc <n>" in flat, "§7g never names the one write a REJECT performs"
    for absent in ("no Epic", "no children", "no board move", "stays parked"):
        assert absent in flat, f"§7g does not say a REJECT leaves {absent!r}"
    # ii — why that is right, not a failure
    assert "zero tickets" in flat, "§7g never names the outcome the reader actually sees"
    assert "--actionable" in flat, (
        "§7g argues for zero tickets without the mechanical reason — children are filed pickable")
    # iii — and what it is not a verdict on
    assert "does not mean" in flat, "§7g never bounds what a REJECT says about the idea"
    # iv — and the way out
    assert "What discharges it" in flat, "§7g never says what clears a sustained blocker"
    assert "§5a" in flat, "§7g does not route the re-run back to the design pass"


def test_the_verdict_list_routes_to_the_reject_branch():
    """The other direction of the same claim. A reader meets the verdict in §7c's two-bullet list,
    several hundred lines above §7g; a section nothing points at is a section nobody reaches, and
    §7c is the ONE place every reader of the verdict passes through."""
    verdicts = DOSSIER[DOSSIER.index("### 7c."):DOSSIER.index("### 7d.")]
    assert "§7g" in " ".join(verdicts.split()), (
        "§7c names REJECT without routing to the section that walks it")


def test_the_contract_records_that_a_rejects_only_record_now_retries_and_reports_loudly():
    """MEASURED, and nothing else in the tree says it (#1986). `sources.GitHubSource.note` now
    retries a transient `gh` error before giving up, and `loop.py note`'s CLI verb — previously a
    bare `try/except` that printed one non-fatal stderr line and always returned 0 — now prints
    `OK`/`FAILED` and returns non-zero on failure. Because a REJECT writes no label and no ticket,
    that comment is still the whole record; §12 is where the shape of ITS resilience belongs, and
    it is the half a reader cannot infer from §7g alone."""
    limits = DOSSIER[DOSSIER.index("## 12."):]
    flat = " ".join(limits.split())
    assert "retries" in flat and "loop.py note" in flat and "#1986" in flat, (
        "§12 no longer describes how a REJECT's one record survives a transient failure")
    assert "RAISES" in flat, (
        "§12 does not say the retried write raises on final failure rather than swallowing it")
    assert "OK" in flat and "FAILED" in flat and "non-zero exit" in flat, (
        "§12 does not say a REJECT's failed comment is now reported loudly, not silently")


def test_the_walkthrough_walks_the_run_that_produced_no_tickets():
    """The route doc is where a first-time reader forms their expectation of what a run looks like,
    and it had exactly one shape in it: Epic #2 with children #3-#6. This is the second real run —
    the one that ended in nothing — held to the same standard as the first: the numbers are there,
    they are quotable, and it routes rather than rules.

    Scoped to the section rather than to the file, and that is the point: TWO of the six pins
    below — the "maintainers' own board" phrase and the §7g route — already appear elsewhere in the document
    (the intro names the second run; the map table and the "when it stops" bullet both cite §7g),
    so a file-wide assertion on either of them stays green against a REJECT section that has been
    gutted. The other four (the quoted verdict, what it did not write, the arithmetic, the
    discharge) appear only here, so a file-wide assertion on those would go red. Scoping the whole
    test to the section is what makes all six of them measure this section rather than two of them
    measuring the rest of the file. `test_a_file_wide_reject_pin_would_miss_a_gutted_section` runs
    that control rather than asserting it in prose."""
    assert "## When Stage 2 says no" in DOSSIER_WALK, "the walkthrough has no REJECT section"
    section = DOSSIER_WALK[DOSSIER_WALK.index("## When Stage 2 says no"):
                           DOSSIER_WALK.index("## The other way in")]
    flat = " ".join(section.split())
    assert "maintainers' own board" in flat, "the walkthrough does not name the run that ended in no tickets"
    assert "goal-review: REJECTED" in flat, "the walkthrough never quotes the verdict line itself"
    assert "No sdlc:designed written, no Epic and no child created." in flat, (
        "the walkthrough quotes the verdict without what it did NOT write, which is the whole point")
    assert "five of the six slices were held" in flat, (
        "the walkthrough asserts the reject without the arithmetic that makes it honest")
    assert "WHAT WOULD DISCHARGE B-1" in flat, "the walkthrough leaves the reader with no way out"
    assert "dossier-pipeline.md#7g-on-reject" in flat, (
        "the REJECT walk explains the branch instead of routing to the section that owns it")


def test_a_file_wide_reject_pin_would_miss_a_gutted_section():
    """The control for the scoping decision above, executed rather than described.

    Delete the REJECT section from the walkthrough and two of that test's six pins are STILL
    satisfied by the rest of the file — the intro names the second run, and the map table plus the
    "when it stops" bullet both route to §7g. A file-wide test built on those two would stay green
    on a document whose REJECT walk is gone, which is the definition of decoration.

    It is a pin as well as a control: if the intro's link to the run, or the routes to §7g outside
    the section, are ever dropped, this goes red — and at that point the docstring above no longer
    describes the document, which is exactly when it should be re-read."""
    gutted = (DOSSIER_WALK[:DOSSIER_WALK.index("## When Stage 2 says no")]
              + DOSSIER_WALK[DOSSIER_WALK.index("## The other way in"):])
    flat = " ".join(gutted.split())
    for survives in ("maintainers' own board", "dossier-pipeline.md#7g-on-reject"):
        assert survives in flat, (
            f"{survives!r} no longer appears outside the REJECT section — the walkthrough has "
            "stopped pointing at the branch from anywhere else, and the scoping rationale on "
            "test_the_walkthrough_walks_the_run_that_produced_no_tickets is now stale")
    for section_only in ("goal-review: REJECTED", "five of the six slices were held",
                         "WHAT WOULD DISCHARGE B-1"):
        assert section_only not in flat, (
            f"{section_only!r} is quoted outside the REJECT section too; the walkthrough now has "
            "two homes for the same run")


# --------------------------------------------------------------- no stale reference to the deleted

def test_no_stale_proposal_reference_survives_anywhere_in_the_tree():
    """The deliverable of the promotion. Two documents that can drift is the failure this replaces,
    and a citation is enough to recreate it: a skill still citing the old path by section number
    sends a reader to a file that is not there and implies one that should be."""
    stale = _dead_reference_files()
    assert not stale, (
        "these files still cite the deleted proposal; point them at docs/dossier-pipeline.md "
        "(and at its NEW section numbers — the promotion renumbered them): " + ", ".join(stale)
    )


def test_the_shipped_skills_cite_the_contract_by_its_new_path():
    """The other direction of the same claim: it is not enough that nothing cites the dead path —
    the skills that used to have to cite it must still be citing SOMETHING, or the promotion
    silently dropped the pointer instead of moving it."""
    for rel in ("skills/sigma-dossier/SKILL.md",
                "skills/sigma-goal-design/SKILL.md",
                "skills/sigma-goal-review/SKILL.md",
                "skills/sigma-loop/SKILL.md",
                "skills/sigma-loop/scripts/design_goal.py",
                "skills/sigma-loop/scripts/loop.py"):
        # #1611 moved sigma-loop's design-check paragraph, and the citation inside it, into
        # references/picking.md. The claim here is that the pointer MOVED rather than being
        # dropped, so a SKILL.md entry is read as the whole skill. See tests/skill_corpus.py.
        text = (skill_corpus(rel.split("/")[1]) if rel.endswith("/SKILL.md")
                else (ROOT / rel).read_text(encoding="utf-8"))
        assert "docs/dossier-pipeline.md" in text, f"{rel} no longer points at the contract"


# ---------------------------------------------------------------- the walkthrough, held to being one

def test_the_dossier_walkthrough_disclaims_its_own_authority():
    """A reader must not be able to finish it believing it is the contract. The same three sentences
    `how-branching-works.md` is held to, for the same reason: they are what an editor tidying the
    introduction would cut first."""
    assert "This document decides nothing." in DOSSIER_WALK
    assert "the contract is right and this file has a bug" in DOSSIER_WALK
    assert "the only one of the two that is authoritative" in DOSSIER_WALK


def test_the_dossier_walkthrough_carries_no_rule_of_its_own():
    """A future editor who pastes a sentence from the contract into the walkthrough gets a red test,
    not a second source of truth that nothing compares."""
    haystack = DOSSIER_WALK.lower()
    pasted = [p for p in _DOSSIER_NORMATIVE_ELSEWHERE if p.lower() in haystack]
    assert not pasted, (
        "docs/how-the-dossier-pipeline-works.md restates wording that is normative in the "
        "contract; link to the section instead of repeating it: " + "; ".join(pasted)
    )


def test_the_dossier_walkthrough_routes_rather_than_explains():
    """The other half of the denylist — a document could pass that test by explaining everything in
    fresh words, which is the same second source of truth with different bytes."""
    cites = _DOSSIER_CITE.findall(DOSSIER_WALK)
    assert len(cites) >= _DOSSIER_LINK_FLOOR, (
        f"only {len(cites)} links into dossier-pipeline.md; a walkthrough that stops citing the "
        f"contract has become a rival to it"
    )


def test_every_dossier_walkthrough_section_sends_the_reader_somewhere():
    """Per-section, not just per-file: a floor over the whole document is satisfied by one dense
    table and a page of unsourced prose."""
    for section in DOSSIER_WALK.split("\n## ")[1:]:
        title = section.splitlines()[0]
        assert _DOSSIER_CITE.search(section), f"walkthrough section '{title}' cites nothing"


def test_every_section_the_dossier_walkthrough_cites_actually_exists():
    """The anchors, resolved against the real headings — the drift direction nothing else notices:
    the contract renumbered or a section folded away, leaving the walkthrough pointing at nothing."""
    anchors = {_slug(h) for h in _headings(DOSSIER)}
    dangling = sorted({c for c in _DOSSIER_CITE.findall(DOSSIER_WALK) if c not in anchors})
    assert not dangling, (
        "docs/how-the-dossier-pipeline-works.md links to sections of dossier-pipeline.md that do "
        "not exist: " + ", ".join(dangling)
    )


def test_the_dossier_walkthrough_walks_one_real_example_end_to_end():
    """The walkthrough's whole claim on a reader's attention is that it is a real run rather than an
    invented one, and a real run is falsifiable: the numbers have to be there and they have to be
    the same ones. #1 (story) -> #2 (epic, carrying the back-reference) -> #3-#6."""
    assert "dossier-e2e-v2-2026-08-28" in DOSSIER_WALK, "the run that produced the example is unnamed"
    assert "Originates from Story #1." in DOSSIER_WALK          # the epic's own back-reference
    assert "Part of epic #2." in DOSSIER_WALK                   # a child's
    assert "**Blocked by:** #3" in DOSSIER_WALK                 # the one real dependency edge
    for child in ("#3", "#4", "#5", "#6"):
        assert child in DOSSIER_WALK, f"the worked example drops child {child}"


def test_the_contract_and_the_agent_rules_both_point_at_the_dossier_walkthrough():
    """A route nobody is sent down is a file that rots unread. Both entry points carry it: the
    contract's own opening, for someone who landed on the contract and wants the short way in, and
    `AGENTS.md`, for an agent that reads nothing else."""
    assert "how-the-dossier-pipeline-works.md" in DOSSIER
    assert DOSSIER.index("how-the-dossier-pipeline-works.md") < DOSSIER.index("## 2. ")
    assert "docs/how-the-dossier-pipeline-works.md" in AGENTS


def test_the_agent_rules_carry_the_pipeline_and_point_at_its_contract():
    """`AGENTS.md` is what an agent working this repo actually reads. Its section is a SHORT VERSION
    plus a pointer — the same shape as its Labels and Branches sections — so what is pinned is that
    the pointer is there and that the four things an agent can get wrong unaided are named."""
    assert "## The Dossier pipeline — where work comes from" in AGENTS
    assert "docs/dossier-pipeline.md" in AGENTS
    section = AGENTS[AGENTS.index("## The Dossier pipeline"):AGENTS.index("## Branches and units")]
    assert "never `sdlc:goal`" in section                 # neither upper tier is ever picked
    assert "OFF by default" in section                    # the gate is opt-in
    assert "one writer and exactly one reader" in section  # who may write sdlc:designed
    assert "blocker edge" in section                       # the phantom-blocker hazard


# --------------------------------------------------- #1929: copyable tables, tree-wide

#: Every shipped surface that presents copyable markdown inside a fence: the skills an agent is told
#: to reproduce, and the top-level contracts a human copies out of.
#:
#: The exclusions are deliberate and each was measured. `docs/superpowers/**` is 75 ARCHIVED plan
#: documents, four of which fence a single table ROW as a substitution fragment
#: (`docs/superpowers/plans/2026-08-10-audit-skill.md:193` and three siblings) — a row fragment is
#: not a skeleton, and nothing local distinguishes one from a broken table, so sweeping them would
#: buy four permanent false positives over documents nobody copies. `CHANGELOG.md` is a record, not
#: an instruction: an entry documenting this very defect by quoting the broken skeleton would be a
#: false positive too. There is no `commands/` directory in this repo — an earlier draft of this
#: pin swept one anyway, and the per-glob assert below is what caught it.
#:
#: Globs are RELATIVE and carry no path-component filter, on purpose. The same earlier draft
#: excluded any path with `.sdlc` in its parts, which reads as a harmless no-op and is not: every
#: Sigma worktree lives under `.sdlc/work/<goal>/`, so inside one — which is where the loop
#: always runs the suite — it silently dropped EVERY docs file, while a plain checkout kept them.
#: A guard whose coverage depends on where it is checked out is not a guard.
_FENCED_MARKDOWN_GLOBS = ("skills/**/*.md", "docs/*.md")


def _tracked_files():
    """Repo-relative POSIX paths git is actually tracking, or None when that can't be determined.

    Same shape as `test_self_contained.py`'s own `_tracked_files()`, and here for the same reason
    (#1821, recurring as #2002): only the INDEX answers "what ships". A raw filesystem walk also
    reads whatever happens to be sitting in the working tree — and on any machine that has run
    `npm install` for the autowatch channel, `skills/**/*.md` matches 194 files of which 149 are
    vendored `node_modules` READMEs we neither own nor may edit. Those tripped the sweep below and
    turned `main` red on a developer's disk while CI and a fresh clone stayed green.

    The carve-out this deliberately is NOT: excluding `node_modules` by path component covers the
    directory it was filed for and nothing else, which is exactly how this defect class already
    came back once. Untracked clutter anywhere else — a one-off export dropped in `docs/`, a
    scratch copy of a skill — can never appear in `git ls-files`, so scanning the index closes the
    class rather than the instance."""
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"],
                             capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return {f for f in out.stdout.split("\0") if f} if out.returncode == 0 else None


def _shipped_markdown(pattern, tracked):
    """The glob's matches, narrowed to what git tracks — the INTERSECTION, deliberately, rather
    than handing `pattern` to `git ls-files` as a pathspec.

    Git's pathspec wildcards are not pathlib's: `*` crosses `/` there, so `docs/*.md` as a pathspec
    returns 88 files where the glob returns 8 (it would sweep up `docs/executor-parity/`,
    `docs/superpowers/plans/` and the rest). Swapping one matcher for the other would silently
    WIDEN what this guard covers while pretending to only exclude untracked files — a scope change
    smuggled into a bug fix. Intersecting keeps the glob semantics these globs were written
    against, byte for byte, and removes only what is untracked."""
    return [p for p in sorted(ROOT.glob(pattern))
            if p.relative_to(ROOT).as_posix() in tracked]


def _table_headers_without_a_delimiter(path):
    """Every fenced line that opens a markdown table but is not followed by a delimiter row.

    A table line both starts and ends with `|` and holds at least two of them — which is what keeps
    a shell continuation (`| head -5`) or an ASCII box edge out of the sweep. Only FENCED blocks are
    read: an ordinary table in running prose is rendered by the host, so a missing delimiter there
    is visible to the author immediately. A fenced one is not — it renders correctly as the code
    block it is, and only breaks once somebody does what the page told them to and copies it.
    """
    bad = []
    lines = path.read_text(encoding="utf-8").splitlines()
    # An odd number of fence markers leaves the toggle below stuck for the rest of the file, which
    # would SKIP every table after it and report nothing. Measured across all 53 files this sweep
    # reads: none is unbalanced and none uses a 4-backtick fence, so this is a tripwire on that
    # assumption, not a workaround for a known case.
    assert sum(1 for line in lines if line.strip().startswith("```")) % 2 == 0, \
        "%s has an unbalanced code fence — the sweep below cannot be trusted on it" % \
        path.relative_to(ROOT)
    infence = False
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if s.startswith("```"):
            infence = not infence
            i += 1
            continue
        if not (infence and s.startswith("|") and s.endswith("|") and s.count("|") >= 2):
            i += 1
            continue
        header = s
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        cells = [c.strip() for c in nxt.strip("|").split("|")] if nxt.startswith("|") else []
        if not cells or not all(re.fullmatch(r":?-+:?", c) for c in cells):
            bad.append("%s:%d  %s" % (path.relative_to(ROOT), i + 1, header))
        while i < len(lines) and lines[i].strip().startswith("|"):
            i += 1
    return bad


def test_every_copyable_table_in_the_shipped_prose_renders_when_copied():
    """The CLASS guard, not one skill's schema.

    A fenced table skeleton is an instruction to reproduce it. Without a `| --- |` delimiter row the
    copy renders on GitHub as a run of literal pipes, and the page that told you to write it looks
    correct the whole time — the fence renders fine as a fence. This has now been found twice in a
    fortnight, in two sibling skills (#1927 finding 3 in `sigma-goal-design`, #1929 in
    `sigma-research`), each time only by a human reading the rendered artifact. Pinning it per skill
    is O(skills) and pins nothing about the skill that has not been written yet; this sweep costs
    one pass over the shipped prose and is what a third occurrence hits.

    Column count is deliberately NOT the property — `|  |  |` and `| : | : |` have the right width
    and neither renders. Every delimiter cell must carry at least one `-`."""
    tracked = _tracked_files()
    if tracked is None:
        pytest.skip("not a git checkout (or git unavailable) — nothing to enumerate")
    bad = []
    for pattern in _FENCED_MARKDOWN_GLOBS:
        # Per-glob, not over the union: a rename that breaks ONE of them would otherwise narrow the
        # sweep silently while the test stayed green on whatever still matched. The non-vacuity
        # assert is what #2002 asked to keep once the sweep became tracked-only: a tracked-file
        # filter that accidentally matched nothing would otherwise go green for the wrong reason.
        paths = _shipped_markdown(pattern, tracked)
        assert paths, "%r matched nothing — the sweep has silently narrowed" % pattern
        for path in paths:
            bad += _table_headers_without_a_delimiter(path)
    assert not bad, (
        "these fenced table headers have no valid `| --- |` delimiter row, so copying them "
        "renders literal pipes rather than a table:\n  " + "\n  ".join(bad))


def test_the_copyable_table_sweep_reads_the_index_not_the_working_tree(tmp_path, monkeypatch):
    """The control on the fix above (#2002), run in both directions against the REAL helpers.

    A pin that only re-checked `git ls-files` output in isolation would stay green if
    `_shipped_markdown` were reverted to a bare `ROOT.glob()`, which is precisely the regression
    this exists to catch — so this drives `_shipped_markdown` itself, with the module's `ROOT`
    pointed at a synthetic repo (`test_self_contained.py`'s own reasoning for the same manoeuvre).

    Both directions matter, and the second is the one that makes it a guard rather than a mute:
    an UNTRACKED broken table must be invisible (the vendored `node_modules` READMEs that turned
    `main` red), and a TRACKED one must still be caught (a fix that excluded everything would also
    go green, and would be worse than the bug)."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)

    broken = "```\n| a | b |\n| c | d |\n```\n"          # header row, no `| --- |` delimiter
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "tracked.md").write_text(broken, encoding="utf-8")
    subprocess.run(["git", "add", "docs/tracked.md"], cwd=tmp_path, check=True)
    # Never added: stands in for `skills/**/node_modules/**/*.md` — present on disk, absent from
    # the index, and none of our business.
    (docs / "vendored.md").write_text(broken, encoding="utf-8")

    # Both helpers read the module's own ROOT, so repointing it is what makes them run against the
    # synthetic repo instead of this one.
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path, raising=True)

    tracked = _tracked_files()
    assert tracked == {"docs/tracked.md"}, tracked

    swept = _shipped_markdown("docs/*.md", tracked)
    names = [p.name for p in swept]
    assert names == ["tracked.md"], (
        "the sweep must read the index, not the working tree: %r" % names)

    # And the guard still bites on the tracked one — the direction a too-eager filter would break.
    assert _table_headers_without_a_delimiter(swept[0]), \
        "a tracked table with no delimiter row must still be reported"
    assert _table_headers_without_a_delimiter(docs / "vendored.md"), \
        "control on the control: the untracked file IS broken — it is excluded by tracking, " \
        "not because the detector cannot see it"


# ============= #1956: `Depends on` carries slice ids, and a Blocker dependency has somewhere to go
# The first real-codebase run produced a slice whose `Depends on` cell read `3, and B-1` -- a slice
# edge AND a Blockers id. That column is machine-read into `compile_plan.py` `blocked_by` keys,
# which name siblings in the same plan and nothing else, so the `B-1` was silently patched away by a
# human between Stage 1 and Stage 2. The rule now has three homes, and this is what holds them
# together: a rule stated in only one of them is how the two stages disagreed in the first place.


def _dossier_skill(name):
    return skill_corpus(name)   # #2107/#2108: SKILL.md + references/*.md, "the skill" not just the file


def test_the_contract_and_both_skills_agree_the_depends_on_column_carries_slice_ids_only():
    for where, text in (("the contract §5d", DOSSIER),
                        ("sigma-goal-design", _dossier_skill("sigma-goal-design")),
                        ("sigma-goal-review", _dossier_skill("sigma-goal-review"))):
        flat = re.sub(r"\s+", " ", text)
        # Singular so it matches both the schema's "carries slice ids" and the consumer's
        # "a token that is not a slice id" -- the same claim, stated from each end.
        assert "slice id" in flat, f"{where} does not state the column's grammar"
        assert "B-1" in flat, f"{where} does not name the id shape that broke it"


def test_the_contract_says_where_a_blocker_dependency_goes_instead_of_only_forbidding_it():
    """The issue asked for this half by name. The edge is written the other way round — in the
    design's own Blockers entry, naming the slice it gates — which is the exact form §7b's blocking
    bucket already adjudicates, so it is carried rather than dropped."""
    flat = re.sub(r"\s+", " ", DOSSIER)
    assert "Blockers entry" in flat, "the contract forbids the reference without redirecting it"
    assert "name the slice that cannot start" in flat, \
        "the redirect does not reuse §7b's own operational test, so the two can drift"


def test_the_contract_tells_stage_2_to_adjudicate_the_token_rather_than_drop_it():
    """§7d-iii is the step that actually builds the plan JSON, so it is the step that meets a
    violating cell. Forbidding at §5d alone leaves it with nothing to do but what the real run did."""
    assert "**iii. Otherwise, build the plan and compile it.**" in DOSSIER, "§7d-iii's anchor moved"
    s7d = DOSSIER.split("**iii. Otherwise, build the plan and compile it.**", 1)[1]
    s7d = s7d.split("**iv. Stamp", 1)[0]
    flat = re.sub(r"\s+", " ", s7d)
    assert "adjudicat" in flat, "§7d-iii does not route a non-slice token through the buckets"
    assert "never drop" in flat.lower(), "§7d-iii does not forbid the silent patch it was built on"


def test_7c_bounds_the_blocked_by_edge_to_an_item_another_slice_actually_carries():
    """§7c is the contract's own mirror of `sigma-goal-review` §2c, and §2c unbounded — *express it
    as a `blocked_by` edge*, full stop — is the instruction the real run followed into the seam. A
    `blocked_by` key names a SIBLING in the same plan, so an open item no slice carries has no edge
    that can be written at all. Bounding the skill and leaving the contract restating the hazard is
    the same split that let the two stages disagree; the contract is the half a human reads."""
    marker = "**A CONFIRM that leaves items open is only honest if they survive it.**"
    assert marker in DOSSIER, "§7c's carrying paragraph is gone"
    para = DOSSIER.split(marker, 1)[1].split("### 7d.", 1)[0]
    flat = re.sub(r"\s+", " ", para)
    assert "another slice" in flat.lower(), \
        "§7c still implies a `blocked_by` edge can name something that is not a sibling slice"
    assert "no edge to write" in flat.lower(), \
        "§7c does not say what carries an open item that no slice of this plan holds"


# ---------------------------------------------------------------- #2029: §4a-i of the label model
# §4a-i documents the SECOND route into the `{sdlc:in-progress}` orphan — a claim armed on a ticket
# that is not a goal — and its load-bearing sentence is a claim about the CODE: that the label has
# exactly one writer. That sentence is falsifiable and, like §3 of the branching model before it,
# is worth nothing unless something measures it. Both tests below were seen red before being
# trusted: the first against the document without §4a-i, the second against a deliberately added
# second writer.

LABEL_MODEL = (ROOT / "docs" / "label-model.md").read_text(encoding="utf-8")


def _section_4a_i():
    assert "### 4a-i." in LABEL_MODEL, "§4a-i's heading is gone"
    body = LABEL_MODEL.split("### 4a-i.", 1)[1].split("### 4b.", 1)[0]
    return re.sub(r"\s+", " ", body)


def test_the_label_model_names_the_claim_on_a_non_goal_as_a_route_into_the_orphan_state():
    """A reader who knows only §4a believes the orphan comes from a half-landed state change. It
    also came from a change that landed perfectly on the wrong ticket, which is a different repair
    and a different thing to look for."""
    flat = _section_4a_i()
    assert "not a goal" in flat or "no goal" in flat, "§4a-i does not name what made the claim wrong"
    assert "_ensure_claimed" in flat, "§4a-i does not name the caller that skipped the question"
    assert "one writer" in flat, "§4a-i drops the fact that makes the rest of it checkable"


def test_the_label_model_says_which_half_of_a_claim_is_gated_and_which_is_not():
    """The asymmetry IS the fix. A reader who takes '#2029 gated the claim' at face value would
    expect the ledger entry to disappear too, and would then read a legitimately-claimed non-goal
    as a journal bug. Both halves have to be stated together or neither is usable."""
    flat = _section_4a_i()
    assert "opposite directions" in flat, "§4a-i does not say the two halves fail differently"
    assert "ledger" in flat and "written whatever the answer" in flat, \
        "§4a-i does not say the LOCAL record still happens"
    assert "a read that failed is not a yes" in flat, \
        "§4a-i does not say which way an unreadable answer resolves"


def test_in_progress_still_has_exactly_one_writer_in_the_shipped_scripts():
    """§4a-i's load-bearing claim, measured against the tree rather than asserted in prose. Every
    other mention of `in_progress_label` in the kit REMOVES it; a second site that ADDS it would
    reopen the orphan route from a place `_arming_may_mark` does not sit in front of."""
    adds = []
    pattern = re.compile(r"add=\[[^\]]*in_progress_label|--add-label\"?,\s*[^)\n]*in_progress_label")
    for path in sorted((ROOT / "skills").rglob("*.py")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                adds.append((path.relative_to(ROOT).as_posix(), n, line.strip()))
    assert len(adds) == 1, f"expected exactly one writer of the in-progress label, found: {adds}"
    assert adds[0][0] == "skills/sigma-loop/scripts/sources.py", adds[0]


# ======================= #2027: §7f's priority rule is enforced, and the two invocations agree
# The 2026-09-01 validation run wrote `"priority": "P2"` into all six children of story #2017's
# plan. §7f forbade exactly that, in as many words, and nothing measured it -- so the rule was
# honour-system prose and the deviation was silent. The mandated invocation now carries
# `--forbid-priority`, which makes `compile_plan.py` refuse the plan before any `gh` call. The
# contract and the skill each print that command; a flag added to one and not the other is how a
# guard gets quietly disarmed, so both copies are pinned here against the SAME assertion.


def _compile_plan_invocations():
    """Every fenced `compile_plan.py` command the two documents actually tell a reader to run,
    DISCOVERED rather than listed -- a hand-written list of the two sites is right on the day it is
    written and silently wrong the day somebody adds a third."""
    found = []
    for where, text in (("docs/dossier-pipeline.md", DOSSIER),
                        ("skills/sigma-goal-review/SKILL.md",
                         skill_corpus("sigma-goal-review"))):   # #2108: SKILL.md + references/*.md
        for block in re.findall(r"```[a-z]*\n(.*?)```", text, re.S):
            if "compile_plan.py" in block and "--plan" in block:
                found.append((where, re.sub(r"\s*\\\s*\n\s*", " ", block)))
    return found


def test_both_documents_print_the_compile_plan_invocation_at_all():
    """The guard for the guard: if the fence shape ever changes, the assertions below would pass
    over an empty list and prove nothing."""
    wheres = {where for where, _ in _compile_plan_invocations()}
    assert wheres == {"docs/dossier-pipeline.md", "skills/sigma-goal-review/SKILL.md"}, wheres


def test_every_mandated_compile_plan_invocation_forbids_priority():
    for where, block in _compile_plan_invocations():
        assert "--forbid-priority" in block, (
            f"{where} prints a compile_plan.py invocation without --forbid-priority; §7f's rule is "
            f"back to being honour-system prose there:\n{block}")


def test_7f_says_the_rule_is_enforced_rather_than_merely_stated():
    """§7f is the home of the rule, so it is where a reader finds out whether anything checks it.
    Naming the flag here is what stops the two halves — the prose and the command — from drifting
    into a rule nobody runs."""
    assert "### 7f. Priority is omitted, deliberately" in DOSSIER, "§7f's heading moved"
    s7f = DOSSIER.split("### 7f. Priority is omitted, deliberately", 1)[1].split("### 7g.", 1)[0]
    flat = re.sub(r"\s+", " ", s7f)
    assert "--forbid-priority" in flat, "§7f does not name the flag that enforces it"
    assert "refus" in flat, "§7f does not say what the enforcement DOES"
    assert "#2027" in flat, "§7f does not name the run that proved the rule needed a guard"

# ================================================= #2028: the contract mirrors the seed record
# The stopping rule is stated in BOTH files by design, so a seed record added to the skill alone
# leaves the authoritative text describing a stopping rule judged against nothing.


def test_the_contract_mirrors_the_recorded_seed_set():
    """CROSS-FILE (#2028). §5c is where the contract states seed closure. A §5c that still defines
    it over an unrecorded set leaves the honour system in the authoritative text while only the
    skill was fixed -- exactly the split #1975 closed for the quiet round."""
    stopping = DOSSIER[DOSSIER.index("### 5c."):DOSSIER.index("### 5d.")]
    # Scoped to the seed-closure paragraph, not to §5c. The capped paragraph further down names
    # `## Seeds` too, so a section-wide check passes with seed closure still defined over the
    # pass's memory. Control: with the paragraph's own `written into the artifact as ## Seeds`
    # replaced by `held by the pass as it goes`, the section-wide form stayed GREEN.
    marker = "**Seed closure is judged against a written record, never against memory**"
    assert marker in stopping, "§5c states no written-record rule for seed closure"
    para = stopping.split(marker, 1)[1].split("\n\n", 1)[0]
    flat = " ".join(para.split()).replace("*", "")
    assert "## Seeds" in para, "§5c judges seed closure against no named record"
    assert "every" in flat and "artifact" in flat, \
        "§5c leaves the table looking like a capped-only section"
    assert "The banner's count IS the table's count" in flat, \
        "§5c leaves the capped banner's own number checkable against nothing"


def test_the_contract_mirrors_the_missing_seeds_table_being_a_violation():
    """CROSS-FILE (#2028). §7a is where the contract tells the reviewer what an ABSENT heading
    means. It already says it for `Premise check` and `Out of scope`; leaving `Seeds` out makes its
    omission read as "every seed was swept", which is the strongest claim in the document."""
    reading = DOSSIER[DOSSIER.index("### 7a."):DOSSIER.index("### 7b.")]
    flat = " ".join(reading.split()).replace("*", "")
    assert "so is a missing `Seeds`" in flat, \
        "§7a makes an absent Out of scope a violation and an absent Seeds good news"


def test_the_contract_still_says_the_budget_is_not_a_derived_number():
    """The honest half, and the reason this is a separate pin. #2028 shipped the RECORD the budget
    argument needs and deliberately did NOT change the number -- so the limits section must still
    concede the cap is chosen rather than measured. A record mistaken for a derivation is how a
    stated limit quietly disappears without anyone deciding it."""
    limits = DOSSIER.split("Judgement the pipeline does not make", 1)
    assert len(limits) == 2, "the contract's limits section moved -- re-check this pin"
    flat = " ".join(limits[1].split()).replace("*", "")
    assert "a stated cap, not a measured one" in flat, \
        "the contract no longer concedes the sweep budget is un-derived"
    assert "does not derive it" in flat, \
        "the limits section lets the seed record read as the derivation it is only a precondition for"


# ================================================= #2032: the contract mirrors the sweep-budget verb
# #2028 shipped the RECORD the budget argument needs but deliberately did not change the number.
# #2032 makes the number CONFIGURABLE (`goal_design.rounds`, `full` mode only, bounded) and
# CHECKABLE (`goal_design.py sweep-budget` is the one source both the design pass and `goal-review`
# read the ceiling from). These pins are the contract's own copy of that mechanism -- the same
# cross-file shape `test_the_contract_mirrors_the_sweep_field_the_skill_mandates` already applies.


def test_the_contract_mirrors_the_budget_check_goal_review_performs():
    """CROSS-FILE. `goal-review` §1 re-runs `sweep-budget` and compares it against the artifact's
    `Budget` field, reporting a mismatch as a schema violation rather than adjudicating it -- the
    fix for the hole the #2032 adversarial review found (a hand-typed `Budget` is indistinguishable
    from a fetched one). §7a is the contract's own copy of that check."""
    goal_review = skill_corpus("sigma-goal-review")   # #2108: SKILL.md + references/*.md
    assert "sweep-budget" in goal_review, "the skill no longer re-runs the budget verb"
    section = DOSSIER[DOSSIER.index("### 7a."):DOSSIER.index("### 7b.")]
    assert "sweep-budget" in section, "§7a does not mirror the skill's own budget re-check"
    flat = " ".join(section.split()).replace("*", "")
    assert "schema violation" in flat, "§7a states no consequence for a Budget mismatch"


def test_the_contract_mirrors_a_missing_budget_being_a_violation():
    """The same reading `Sweep`/`Seeds`/`Premise check`/`Out of scope` already have: silence about
    the field must not be read as the good, unconfigured case."""
    reading = DOSSIER[DOSSIER.index("### 7a."):DOSSIER.index("### 7b.")]
    flat = " ".join(reading.split()).replace("*", "")
    assert "missing `Budget` field is the identical failure a missing `Sweep`" in flat, \
        "§7a leaves an absent Budget field with no stated reading"


def test_goal_design_py_exists_and_matches_the_contracts_own_numbers():
    """CROSS-FILE. The contract states the unconfigured default (3) and the fixed lane figures
    (1/2/3) in prose; the live script is what a reader following #2032's mechanism actually runs,
    and a drift between the two would mean the docs describe a script that no longer behaves as
    written."""
    import importlib.util
    path = ROOT / "skills" / "sigma-goal-design" / "scripts" / "goal_design.py"
    assert path.is_file(), "the contract describes a script that does not exist"
    spec = importlib.util.spec_from_file_location("goal_design", path)
    goal_design = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(goal_design)
    assert goal_design.DEFAULT_FULL_ROUNDS == 3
    assert goal_design.LANE_ROUNDS == {"small": 1, "medium": 2, "large": 3}


