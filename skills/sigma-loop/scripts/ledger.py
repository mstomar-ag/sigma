#!/usr/bin/env python3
"""Team coordination ledger: an append-only record of what the loop actually did.

WHY PER-AUTHOR-PROCESS FILES. Every writing PROCESS owns exactly one file
(`ledger/entries/<actor>-<host>.<pid>.jsonl`) and never touches anyone else's, so two people
running the loop against one repo cannot conflict on a write — the "team view" is the UNION of
those files, computed on read. The obvious alternative (one shared file everyone appends to)
needs a lock this kit does not have, and git would turn every concurrent append into a merge
conflict. The instance token, not just the actor, is load-bearing: the actor resolves to the
authenticated `gh` login, and several parallel loops can share one login (F10) — without the
pid, two such loops would collide on the SAME file and could mint the same `id`; without the
host hash (#540), so would two MACHINES authenticating as one bot account.

THE COUNT IS NOT A LEAK. One file per writing process means MANY files per actor: every run
adds one, `read_all()` unions them, and nothing here ever compacts them. Hundreds of files for
a single actor is the designed steady state — an external audit of one adopter machine found
175 for one actor after two and a half weeks — so a reader who expects a single file per actor
will mistake a healthy directory for a fault. Nothing is ever read back by filename: `read_all()`
globs every `*.jsonl` under the stream directory and attributes each line by the `actor` FIELD
inside it, so the file count costs correctness nothing.

WHAT IT IS FOR. The review queue answers "what stopped?" for one person on one machine
(and it is gitignored, so nobody else ever sees it). The ledger answers "what has the team
done, and what is waiting on whom?" — it is committed, it carries a timestamp and an actor
on every line, and a hand-off entry names the person who has to act.

Default OFF: with no `ledger` block in config.json every entry point is a no-op, so a repo
that has not opted in behaves exactly as it did before this module existed. Zero deps.
"""
import calendar
import datetime
import hashlib
import importlib.util
import json
import os
import pathlib
import platform
import re

try:                    # portable output: force UTF-8 so the plugin's own non-ASCII (arrows, em-dashes)
    import sys as _sys  # doesn't garble to '?' or crash on a non-UTF-8 console (the Windows cp1252
    _sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")   # default); a stream without
    _sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")   # reconfigure is left as-is
except Exception:
    pass
import subprocess
import sys
import time

#: Every kind a ledger line may carry. Small on purpose — an open vocabulary would make the
#: team view unreadable within a week. `merged` records a PR the loop actually landed. `merge-armed`
#: (F26/#344) is the separate, honest kind for the moment `work.py merge` calls `gh pr merge --auto`:
#: that call only ARMS GitHub's auto-merge, it does not confirm the PR landed — a later-failing check
#: or a cancelled auto-merge can still mean it never does. Logging that moment as `merged` was a false
#: "landed" claim in TEAM.md's shared view. `work.py merge` writes `merged` only after its GitHub
#: receipt confirms the PR landed, carrying the stable key that prevents duplicate observations.
#: Sibling pin: tests/test_ledger.py::test_vocabulary_constants_match_spec_table (this file) and
#: contract/vocabulary.json's "entries_kinds" -- issue #298. Update both by hand.
KINDS = ("claimed", "done", "parked", "failed", "handoff", "ack", "release", "note", "merged", "merge-armed", "acceptance")

#: Lifecycle of a hand-off, from the point of view of the person it is addressed TO.
STATES = ("open", "accepted", "deferred", "declined", "resolved")

#: Kinds that belong in the shared/team view even with no explicit addressee. `claimed` is shared so
#: the team view records WHO started a ticket and WHEN (it pairs with `done` to show start→finish);
#: `merged`/`merge-armed` are shared because a landed-or-armed PR is a team event; `note` stays
#: personal unless it names a `to`.
SHARED_KINDS = ("claimed", "done", "parked", "failed", "handoff", "ack", "release", "merged", "merge-armed")

#: Optional fields, all free-form except `state` (validated) — additive by design: an older
#: reader ignores a field it does not know rather than failing. `pr` carries a pull-request number.
#: `reclaimed_actor` (PR #1235 review, Finding 1/3) is written ONLY by a `release` entry that an
#: AUTOMATED sweep (`loop.py`'s `_auto_reclaim_stale_claims`) writes on behalf of a claim's
#: ORIGINAL holder, who is not the process performing the write — see `_held()`'s own docstring
#: for why the terminal-entry `actor` field alone (always self-attributed to the writing process,
#: never forged) cannot carry this. Sourced only from the ledger's OWN prior `claimed` entry (via
#: `expired_claims()`'s returned actor), never from arbitrary/external input — an already-`
#: _safe_name()`-bounded short id, same trust level as `area`/`to`/`issue`, so it gets no
#: free-text scrub (see `append()`'s field-by-field treatment below).
#:
#: `run_id` (#1121, follow-up from #841's own review): this run/process-tree's identity, sourced
#: by every writer from `state.run_identity()` (`SIGMA_RUN_ID`, #498's existing per-worker-tree
#: id — see that function's own docstring for why an env var, not anything filesystem-shared, is
#: the one axis that tells two CONCURRENT processes of the SAME actor apart). `_held()` uses this
#: to scope its stale-terminal-entry guard by (actor, run_id) rather than actor alone — see that
#: function's own docstring for the exact race this closes. Unlike `reclaimed_actor`/`area`/`to`/
#: `issue`, this is NOT always sourced from our own code: `state.run_identity()`'s own docstring
#: notes `supervise_daemon.py` "respects an id set by an outer launcher", so like `ref` (#385) it gets the
#: `_sanitize_free_text(..., cap=BOUNDED_ID_CAP)` treatment in `append()` below, not the raw-write
#: path the fully-internal enum/id fields get.
#:
#: `reclaimed_run` (PR #1269 review, blocking finding 1): the SAME "name the target explicitly"
#: pattern `reclaimed_actor` already uses, one level finer -- the ORIGINAL claim's own `run_id`,
#: never the release entry's own (that stays `run_id` above, always self-attributed to the
#: writer). Closes the gap `reclaimed_actor` alone left open: the auto-reclaim sweep's own
#: release could still wipe a live re-claim by the SAME actor's OWN second, concurrent process,
#: because actor-only scoping cannot tell two same-actor processes apart. Sourced only from the
#: ledger's own prior `claimed` entry (via `expired_claim_run_ids()`'s returned run_id, mirroring
#: `reclaimed_actor`'s own `expired_claims()` sourcing), same trust tier as `reclaimed_actor` --
#: no free-text scrub needed, see `append()`'s field-by-field treatment below.
#:
#: `autowatch_hop` (#1319, skills/sigma-loop/scripts/autowatch.py): how many autowatch-triggered
#: `/sigma-loop` runs already precede this entry in an unattended wake-and-work chain -- the field
#: `ledger.autowatch.hop_limit` is checked against. Like `run_id`, it is sourced from OUTSIDE this
#: plugin's own code (an outer launcher's environment -- here `SIGMA_AUTOWATCH_HOP`, which
#: autowatch.py sets on the driven subprocess's environment before launching it, exactly mirroring
#: `state.run_identity()`'s own `SIGMA_RUN_ID` precedent), not a hard-coded constant any one
#: call site controls -- see `append()`'s own env-default-fill below for why every entries-stream
#: write inside that process tree inherits it automatically, with no other call site (handoff.py,
#: loop.py, ...) needing to know autowatch exists at all. Same trust tier as `run_id`/`ref` --
#: `_sanitize_free_text(..., cap=BOUNDED_ID_CAP)` treatment in `append()` below, not the raw-write
#: path the fully-internal enum/id fields get.
OPTIONAL_FIELDS = ("area", "to", "issue", "priority", "why", "state", "ref", "pr", "reclaimed_actor",
                    "run_id", "reclaimed_run", "autowatch_hop", "merged_entry_key", "acceptance_sha256")

ENTRIES, EVENTS = "entries", "events"
STREAMS = (ENTRIES, EVENTS)

#: The event-stream analogue of KINDS. Deliberately separate from KINDS/SHARED_KINDS
#: (untouched, per spec A.1) so the entries vocabulary a lead reads in TEAM.md can never
#: be diluted by adding a journal kind here.
#: Sibling pin: tests/test_ledger.py::test_vocabulary_constants_match_spec_table (this file) and
#: contract/vocabulary.json's "event_kinds" -- issue #298. Update both by hand.
#:
#: issue #1030: "model_choice" added -- ALSO a sibling pin against a downstream reader's own,
#: separately hand-copied event-kind set (it can never `import` this one -- the private side may
#: not import the core, tests/test_import_boundary.py) and against the contract's own events
#: golden fixture (one line per kind). Three copies of one vocabulary, none of them optional to
#: update together.
EVENT_KINDS = ("phase", "gate", "verify", "slice", "spend", "retro", "park", "scan", "run_stop",
               "model_choice", "merge_observed", "review_posted", "ci_observed")

#: Per-kind field whitelist for the events stream — the events-stream equivalent of
#: OPTIONAL_FIELDS. A closed list per kind, not one shared list, because event kinds do not
#: share a field namespace (e.g. `gate.verdict` vs `retro.grade`) the way entries kinds do.
EVENT_FIELDS = {
    "phase": ("phase", "state", "ms", "tokens_in", "tokens_out", "attempt_id",
              "model", "unpriced_turns"),
    # issue #272: `decision_id` joins `why` on `gate` -- the denial's decision id as a REAL,
    # structured field, not only reconstructible by regexing `why`'s free text (the read side had
    # two unfixable failure modes: an id with internal whitespace truncates to its first word, and
    # a path component containing a colon anchors the extraction at the wrong colon, landing a
    # silently-wrong non-NULL id). Only `decision_gate.py`'s own `_emit_decision_event` (the deny
    # path) ever passes it; `why` stays as-is for the human-readable message.
    "gate": ("gate", "verdict", "cycle", "why", "decision_id"),
    "verify": ("ok", "exit", "ms", "command_sha256", "absent"),
    "slice": ("slice", "wave", "mode", "files_declared", "ms"),
    "spend": ("phase", "model", "tokens_in", "tokens_out", "cost_cents", "attempt_id"),
    "retro": ("grade", "debt_count", "lessons_count"),
    # #953/#1185: decision_tier is OPTIONAL and additive -- only a park whose reason_class is
    # needs_decision, irreversible, or genuine free-text unknown (loop.py's _record(), gated on
    # decision_tier.resolve() returning non-None -- see loop.py's _DECISION_TIER_REASON_CLASSES
    # and _reason_class_matched for exactly which parks that is) ever passes it. Every other
    # reason_class -- including the "unknown"-classed but MECHANICAL "could not compute
    # mergeability" park text, which loop.py's _record() excludes even though "unknown" itself is
    # in the gate -- and every park record written before #953, simply omits the key (append()'s
    # `value not in (None, "")` skip already makes an absent/None field a no-op, same as `why`).
    "park": ("reason_class", "why", "decision_tier"),
    "scan": ("category", "file", "count"),
    # #547: a single run-level event at a drain's terminal branch (a budget-exhaustion signal).
    # Mirrors `park`'s original (reason_class, why) shape exactly -- reason_class is the machine why
    # ('budget' vs 'backlog-empty'), why the human detail (e.g. which ceiling tripped). Deliberately
    # does NOT also mirror #953's later `decision_tier` addition to `park` -- a run stop is never a
    # decision awaiting a human, so there is nothing for decision_tier.resolve() to classify here.
    # Emitted by
    # loop.py's run_loop at its DONE/BUDGET branches with an EMPTY goal, which is precisely how a
    # downstream reader tells run-level stops from per-goal ones (a null goal).
    "run_stop": ("reason_class", "why"),
    # issue #1030: written by predict.py's own resolve()/resolve_step() (never agent-typed prose in
    # a SKILL.md -- see that file's own _emit_model_choice), shelling out to `loop.py emit ... kind
    # model_choice --model <tier> --signal <signal>`. Deliberately ONLY these two fields -- `effort`/
    # `phase` exist on the separate, pre-existing `loop.py log ... model_choice` ACTIONLOG kind
    # (actionlog.AGENT_KINDS), a different vocabulary this issue does not touch. #2828 adds a second
    # writer: `loop.py escalate` records a tier escalation here, signal `escalated: <gate> send-back
    # at <tier>` (skills/sigma-loop/scripts/tier_escalation.py).
    "model_choice": ("model", "signal"),
    "merge_observed": ("observation_key", "subject_kind", "subject", "pr", "merge_sha"),
    "review_posted": ("observation_key", "brief_hash", "evidence_id", "comment_id", "pr", "head_sha", "verdict"),
    "ci_observed": ("observation_key", "pr", "head_sha", "gate_verdict", "checks_total", "checks_truncated", "checks"),
}

# Public typed contract.  The per-kind maps above drive serialization while this map is the
# fail-closed fact boundary used by all direct writers.
EVENT_SCHEMAS = {
    "merge_observed": {"required": EVENT_FIELDS["merge_observed"]},
    "review_posted": {"required": EVENT_FIELDS["review_posted"]},
    "ci_observed": {"required": EVENT_FIELDS["ci_observed"]},
}
# Contract readers accept historical `merged` rows written before #2577.  New engine writes still
# require the key at `append()` below; when a historical row carries the field, the generated
# contract validates its shape rather than silently accepting a malformed claimed identity.
ENTRY_SCHEMAS = {"merged": {"fields": {"merged_entry_key": {
    "type": "string", "pattern": "^[0-9a-f]{64}$",
    "error": "merged_entry_key must be 64 lowercase hexadecimal characters",
}}}, "acceptance": {"fields": {"acceptance_sha256": {
    "type": "string", "pattern": "^[0-9a-f]{64}$",
    "error": "acceptance_sha256 must be 64 lowercase hexadecimal characters",
}}}}

#: Every EVENT_KINDS value must have a whitelist entry — append() indexes EVENT_FIELDS[kind]
#: directly (not .get(kind, ())) so a future EVENT_KINDS addition with no matching whitelist
#: entry raises immediately at import time instead of silently dropping every field it writes.
assert set(EVENT_KINDS) == set(EVENT_FIELDS), "EVENT_KINDS and EVENT_FIELDS have drifted apart"

#: #141: the closed set of EVENTS-stream fields that carry agent-/hook-authored free prose —
#: append() flatten+scrub+caps exactly these, for every kind, at the one chokepoint every write
#: path already funnels through. Nothing else in EVENT_FIELDS is prose: everything else is an
#: enum, a count, a duration, a bool, or (`verify.command_sha256`) a hash.
EVENT_FREE_TEXT_FIELDS = {
    # Observed transcript model IDs are the same unbounded vendor-owned strings as `spend.model`.
    # Keep them scrubbed and capped even though phase_report.py is the only writer today.
    "phase": ("model",),
    "gate": ("why",),
    "park": ("why",),
    "spend": ("model",),
    "run_stop": ("why",),          # #547: same prose treatment as park.why (flatten+scrub+cap)
    # issue #1030: `model` mirrors `spend.model`'s own classification exactly -- same field name,
    # same free-text treatment, despite a narrower value domain (a tier name, not a full
    # model-version string); enforcing a new tier-name enum here would need a vocabulary constant
    # ledger.py does not own (predict.py's tier list belongs to a deliberately decoupled skill).
    # `signal` is semantically an explanation (gate.why's role), not an id/path
    # (EVENT_BOUNDED_ID_FIELDS's role) -- free-text by ROLE, even though in practice it is always
    # one of predict.py's own short fixed-pattern matches today; classifying by today's
    # implementation accident instead of the field's actual role would be the wrong call if that
    # pattern list ever grows a longer phrase.
    "model_choice": ("model", "signal"),
}

#: #141 amendment B: the completeness half of the guard below. A field NOT listed in
#: EVENT_FREE_TEXT_FIELDS is not automatically "safe" — the two look identical in Python (both are
#: plain `str`), so a type check alone can never catch a forgotten prose field. Every field of
#: every kind must be named HERE or above, on purpose, or the assertion below fails at import time
#: instead of a future story shipping an uncapped/unscrubbed/newline-tolerant field for years with
#: green tests.
#:
#: POST-REVIEW FIX: this used to be where the story stopped — a field named here was "safe", full
#: stop, no further check. An independent PR review BLOCKED #249 by proving that was never enough:
#: it proved every field LABELLED prose-or-not, but never that a "non-prose" label was actually
#: TRUE. `ms`/`tokens_in`/`tokens_out`/`cost_cents`/`cycle`/`debt_count`/`lessons_count` were plain
#: CLI strings with zero numeric validation anywhere on the write path, and `scan.file`/
#: `slice.slice` were "safe" purely BY CONVENTION (a filesystem path and a plan-authored id,
#: "neither expected to carry ... a secret shape" — an expectation, not a check). A payload with no
#: literal newline sailed through every one of those untouched. Every field named here now ALSO
#: carries a declared VALUE TYPE below (EVENT_NUMERIC_FIELDS / EVENT_BOOL_FIELDS /
#: EVENT_ENUM_FIELDS / EVENT_BOUNDED_ID_FIELDS), and `_assert_non_prose_fields_are_typed` (run at
#: import time, same as the guard below) fails closed if one is missing. `append()` enforces
#: numeric/bool at its chokepoint; `loop.py`'s `_validate_event` enforces the same thing again,
#: earlier, at the CLI, with a usable refusal instead of a silent sanitize.
EVENT_NON_PROSE_FIELDS = {
    "phase": ("phase", "state", "ms", "tokens_in", "tokens_out", "attempt_id",
              "unpriced_turns"),
    # issue #272: `decision_id` is a bounded-id (EVENT_BOUNDED_ID_FIELDS below), NOT free text --
    # it is the point of this field that it never gets `why`'s FREE_TEXT_CAP prose treatment (a
    # deep repo-relative path prefix can push the id itself past that 200-char cap; the shorter,
    # dedicated BOUNDED_ID_CAP has no such neighbour to be crowded out by).
    "gate": ("gate", "verdict", "cycle", "decision_id"),
    "verify": ("ok", "exit", "ms", "command_sha256", "absent"),
    "slice": ("slice", "wave", "mode", "files_declared", "ms"),
    "spend": ("phase", "tokens_in", "tokens_out", "cost_cents", "attempt_id"),
    "retro": ("grade", "debt_count", "lessons_count"),
    "park": ("reason_class", "decision_tier"),       # #953: decision_tier joins reason_class -- same
                                                       # short, closed-vocabulary shape, so it gets the
                                                       # same non-prose treatment, not `why`'s free-text one.
    "scan": ("category", "file", "count"),
    "run_stop": ("reason_class",),          # #547
    "merge_observed": ("observation_key", "subject_kind", "subject", "pr", "merge_sha"),
    "review_posted": ("observation_key", "brief_hash", "evidence_id", "comment_id", "pr", "head_sha", "verdict"),
    "ci_observed": ("observation_key", "pr", "head_sha", "gate_verdict", "checks_total", "checks_truncated", "checks"),
}

def _assert_event_fields_classified(event_kinds, event_fields, free_text_fields, non_prose_fields):
    """The completeness check itself, as a function rather than inlined at import time — so a test
    can call it directly against a deliberately-INCOMPLETE copy of the field maps (simulating a
    future story that adds a free-text field and forgets to classify it) and prove the guard
    actually fires, without needing to add a second real member to module-level EVENT_KINDS just
    to exercise the failure path. Called once below, at import time, against the real constants."""
    assert set(free_text_fields) <= set(event_kinds), \
        "EVENT_FREE_TEXT_FIELDS names a kind that isn't in EVENT_KINDS"
    assert set(non_prose_fields) <= set(event_kinds), \
        "EVENT_NON_PROSE_FIELDS names a kind that isn't in EVENT_KINDS"
    for kind in event_kinds:
        prose = set(free_text_fields.get(kind, ()))
        safe = set(non_prose_fields.get(kind, ()))
        assert not (prose & safe), \
            f"{kind!r}: a field cannot be both prose and declared-safe: {prose & safe}"
        real = set(event_fields[kind])
        assert prose <= real, f"EVENT_FREE_TEXT_FIELDS[{kind!r}] names a field not in EVENT_FIELDS"
        assert safe <= real, f"EVENT_NON_PROSE_FIELDS[{kind!r}] names a field not in EVENT_FIELDS"
        unclassified = real - (prose | safe)
        assert not unclassified, (
            f"EVENT_FIELDS[{kind!r}] has unclassified field(s) {unclassified} — declare each one in "
            "EVENT_FREE_TEXT_FIELDS (prose: flatten+scrub+cap) or EVENT_NON_PROSE_FIELDS (safe as-is); "
            "an unclassified field is a classification bug, not a safe default")


_assert_event_fields_classified(EVENT_KINDS, EVENT_FIELDS, EVENT_FREE_TEXT_FIELDS, EVENT_NON_PROSE_FIELDS)

#: POST-REVIEW FIX (the four buckets EVENT_NON_PROSE_FIELDS now has to account for). Every field
#: named in EVENT_NON_PROSE_FIELDS above must land in EXACTLY ONE of these — the completeness half
#: of `_assert_non_prose_fields_are_typed` below fails at import time if a field is in neither, and
#: the mutual-exclusion half fails if a field is claimed by two.
#:
#:   - numeric: must parse as a whole number (`_looks_numeric`). Enforced twice: `loop.py`'s
#:     `_validate_event` refuses a bad value at the CLI (exit 2, nothing written); `append()`
#:     sanitizes (never raises) for every other call site.
#:   - bool: must be an actual bool or a recognised spelling (`_looks_bool`). Same two enforcement
#:     points as numeric.
#:   - enum: already vocabulary-checked elsewhere — PHASE_KINDS/GATE_KINDS/VERDICTS/RETRO_GRADES in
#:     `loop.py`'s `_validate_event` for the fields an agent can type; `category`/`mode` are
#:     produced only by deterministic code (discovery-scan's fixed categories, `slices.dispatch`'s
#:     two dispatch modes), never agent-typed, so there is nothing to enforce at a CLI they never
#:     reach. Written as-is, same as before this story.
#:   - bounded_id: `slice`/`file`/`command_sha256` — ids and short paths, not prose, but no longer
#:     "safe by convention" either. Capped SHORT (BOUNDED_ID_CAP, not FREE_TEXT_CAP) and scrubbed,
#:     the same treatment as prose, closing the `slice.slice` gap the review flagged (an agent-
#:     authored plan `id` landing verbatim with no length/shape check at all).
EVENT_NUMERIC_FIELDS = {
    "phase": ("ms", "tokens_in", "tokens_out", "unpriced_turns"),
    "gate": ("cycle",),
    "verify": ("exit", "ms"),
    "slice": ("wave", "files_declared", "ms"),
    "spend": ("tokens_in", "tokens_out", "cost_cents"),
    "retro": ("debt_count", "lessons_count"),
    "scan": ("count",),
    "merge_observed": ("pr",),
    "review_posted": ("comment_id", "pr"),
    "ci_observed": ("pr", "checks_total"),
}

EVENT_BOOL_FIELDS = {
    "verify": ("ok", "absent"),
    "ci_observed": ("checks_truncated",),
}

EVENT_ENUM_FIELDS = {
    "phase": ("phase", "state"),
    "gate": ("gate", "verdict"),
    "slice": ("mode",),
    "spend": ("phase",),
    "retro": ("grade",),
    "park": ("reason_class", "decision_tier"),   # #953: decision_tier.py's own tier vocabulary
                                                   # (autonomous/escalate_l1/escalate_l0) is a small
                                                   # closed set exactly like REASON_CLASSES -- same
                                                   # enum bucket, same enforcement (bounded-id scrub+cap
                                                   # in append(), never a raised vocabulary check, per
                                                   # #136's deliberate reason_class precedent above).
    "scan": ("category",),
    "run_stop": ("reason_class",),          # #547: reason_class is an enum (REASON_CLASSES)
    "merge_observed": ("subject_kind",),
    "review_posted": ("verdict",),
    "ci_observed": ("gate_verdict",),
}

EVENT_BOUNDED_ID_FIELDS = {
    "phase": ("attempt_id",),
    "spend": ("attempt_id",),
    "verify": ("command_sha256",),
    "slice": ("slice",),
    "scan": ("file",),
    # issue #272: an author-typed `.sdlc/decisions.json` `id` -- same shape of value as `slice.slice`
    # (a short, plan-authored token), so it gets the identical bounded-id scrub+cap treatment rather
    # than `why`'s prose one.
    "gate": ("decision_id",),
    "merge_observed": ("observation_key", "subject", "merge_sha"),
    "review_posted": ("observation_key", "brief_hash", "evidence_id", "head_sha"),
    "ci_observed": ("observation_key", "head_sha", "checks"),
}


def _assert_non_prose_fields_are_typed(event_kinds, non_prose_fields, type_maps):
    """The TYPE half of the guard, layered on top of `_assert_event_fields_classified` above. That
    guard already proves every field is LABELLED prose-or-not; it never proves a "non-prose" label
    is TRUE — a reviewer demonstrated by execution that `tokens_in`/`cycle`/`debt_count` (all
    "safe" by that label) were plain CLI strings with zero shape enforcement. This confirms every
    field named in `non_prose_fields` also has exactly one declared VALUE TYPE, and fails at IMPORT
    TIME — not years later in production — the moment a new field ships with a label but no type.

    `type_maps` is `{type_name: {kind: (field, ...)}}`; called once below, at import time, against
    the real constants, and directly by tests against a deliberately-broken copy to prove the
    failure path fires (mirroring `_assert_event_fields_classified`'s own test-injection shape)."""
    for type_name, type_map in type_maps.items():
        assert set(type_map) <= set(event_kinds), \
            f"{type_name!r} type map names a kind that isn't in EVENT_KINDS"
    for kind in event_kinds:
        safe = set(non_prose_fields.get(kind, ()))
        owner = {}
        for type_name, type_map in type_maps.items():
            fields = set(type_map.get(kind, ()))
            assert fields <= safe, (
                f"{type_name!r}[{kind!r}] names field(s) {fields - safe} not declared non-prose "
                f"for {kind!r} in EVENT_NON_PROSE_FIELDS")
            for field in fields:
                assert field not in owner, (
                    f"{kind!r}.{field!r} is classified as both {owner[field]!r} and {type_name!r} "
                    "— a field must have exactly one VALUE TYPE")
                owner[field] = type_name
        untyped = safe - set(owner)
        assert not untyped, (
            f"EVENT_NON_PROSE_FIELDS[{kind!r}] has field(s) {untyped} with no declared VALUE TYPE — "
            "add each to EVENT_NUMERIC_FIELDS, EVENT_BOOL_FIELDS, EVENT_ENUM_FIELDS, or "
            "EVENT_BOUNDED_ID_FIELDS; an untyped field is a classification bug, not a safe default")


_assert_non_prose_fields_are_typed(EVENT_KINDS, EVENT_NON_PROSE_FIELDS, {
    "numeric": EVENT_NUMERIC_FIELDS, "bool": EVENT_BOOL_FIELDS,
    "enum": EVENT_ENUM_FIELDS, "bounded_id": EVENT_BOUNDED_ID_FIELDS,
})

#: #141: the length every declared prose field is capped to, after flatten+scrub.
FREE_TEXT_CAP = 200

#: POST-REVIEW FIX: the length a declared bounded-identifier field (slice/file/command_sha256) is
#: capped to, after flatten+scrub — deliberately shorter than FREE_TEXT_CAP. These are ids and
#: short paths, not prose: a legitimate `slice` id, a repo-relative file path, or a sha256 hex
#: digest (64 chars) is always well under 120; anything longer is already suspicious.
BOUNDED_ID_CAP = 120

#: Round-5 review finding 2: `merge_observed.subject` and `ci_observed.checks[].name` are
#: schema-validated (contract/validate.py) up to 256 chars, but were run through the generic
#: BOUNDED_ID_CAP (120, meant for short plan-authored ids) -- silently truncating an
#: already-validated fact to a DIFFERENT, shorter one, with no error anywhere, and the
#: truncated value still passes validate.py on read-back. `observation_key`/`merge_sha` in the
#: same EVENT_BOUNDED_ID_FIELDS entries are fixed-length hex hashes (64/40 chars) that never
#: reach 120 in the first place, so only these two need the schema's own, wider bound.
EVENT_SCHEMA_STRING_CAP = 256

#: Digits a numeric field may carry. A syntactic "parses as an int" check is NOT a safety check:
#: an arbitrary secret base-10-encoded as one integer parses cleanly, so without a bound it would
#: skip the scrubber and both caps and land raw, recoverable byte-for-byte with int(v).to_bytes().
#: 20 digits clears a signed 64-bit int, so every real duration, token count, cost and cycle fits
#: with room to spare.
#:
#: WHAT THIS DOES NOT CLOSE, stated plainly because an earlier version of this comment claimed it
#: did and that was false. A per-field bound does nothing against a payload SPLIT ACROSS fields: a
#: `phase` event carries three numeric fields, so ~24 bytes — enough for a whole AWS key — round-
#: trips in one legitimate `emit` call, and successive events cost nothing. That channel is
#: inherent to "a bounded numeric field trusts its own digits" and no cap can remove it.
#:
#: The accepted risk, stated at its real size. A first version of this note argued the gap was
#: cheap to accept because an agent that wants to exfiltrate can write
#: `.sdlc/ledger/events/<actor>-<host>.<pid>.jsonl` directly with a shell redirect anyway, so
#: closing the CLI
#: channel buys nothing. Review showed that reasoning is wrong, and the correction matters more
#: than the mechanics: it assumes the blast radius is the writer's own machine. It is not. The
#: events stream is PUSHED to the shared `sdlc-ledger` ops branch by the documented default path
#: (`sync.py bootstrap` — "pushes it so the team can see it"), and a downstream manager dashboard
#: reads it team-wide. There is currently no config that keeps it local: doctor reports that even
#: `share: false` "still publishes with the ledger, same as share: true", because the write side of
#: that flag is unimplemented (#244).
#:
#: So the honest statement is: a chunked secret entered through the SANCTIONED `loop.py emit` CLI
#: — the exact command an agent is told to run — reaches a shared team branch and a manager
#: dashboard whose readers trust it as sanitised. That is a different threat class from #140's
#: allowlist precedent, which is about an agent lying to ITSELF on one machine. A raw shell
#: redirect is unstoppable from in-process and genuinely out of scope; this is not that.
#:
#: It is still not fixed HERE, because no per-field bound can fix it — a `phase` event has three
#: numeric fields and successive events are free — and tightening the cap to a few digits would
#: break real values (a 27-hour `ms`, a seven-figure token count) while leaving the channel open.
#: The real mitigations are elsewhere and owned: #244 makes `share: false` actually keep events
#: local, and #248 covers whether ingested data can be trusted at all. Recorded on #141 so it is a
#: known accepted risk with the right size attached, not an inherited "local, so low-stakes".
NUMERIC_DIGIT_CAP = 20

#: The storability bound for every numeric field a downstream store maps to a column (issue #787). The digit cap
#: above bounds an ENCODED value's *length* (the #141 secret-bandwidth limit); it does NOT bound its
#: *magnitude* — 20 digits reaches 10**20-1, which overruns a signed 64-bit column. Every numeric
#: column the ledger feeds is at most 64-bit (`ms`/`tokens_*`/`cost_cents` are BIGINT, and #787
#: widened `cycle`/`exit_code` from 32-bit INTEGER to BIGINT so the guarantee is one width, not
#: three), so a value outside this range is a record a downstream ingester can never INSERT — it would
#: stall that writer's stream permanently. `_looks_numeric` refuses it at BOTH shared enforcement
#: points. Plain module constants, not a schema cross-import: a downstream reader must stay
#: extractable.
_INT64_MIN = -(2 ** 63)
_INT64_MAX = 2 ** 63 - 1

#: Controlled vocabularies from spec §A.3. PHASE_KINDS/GATE_KINDS/REASON_CLASSES exist for
#: downstream consumers (ingest, docs, future validation) but are NOT enforced by append() in
#: #136 — see the "unknown phase/gate/reason_class" decision in the append() docstring.
#: VERDICTS IS enforced (issue requires it).
#: Sibling pin (all four below): tests/test_ledger.py::test_vocabulary_constants_match_spec_table
#: (this file) and contract/vocabulary.json's "phase_kinds"/"gate_kinds"/"verdicts"/
#: "reason_classes" -- issue #298. Update both sides by hand.
PHASE_KINDS = ("goal", "research", "plan", "plan_review", "implement", "review", "retro")
GATE_KINDS = ("plan_review", "code_review", "post_review", "merge", "decision", "alignment",
              "verify", "risk_security", "risk_contract", "risk_migration", "risk_release",
              "risk_debug", "test_trust")
VERDICTS = ("pass", "block", "warn", "absent")
REASON_CLASSES = ("irreversible", "needs_decision", "merge_conflict", "failing_check",
                   "no_evidence", "dependency", "review_cap", "budget", "backlog-empty",
                   "quota", "unknown",
                   # #2521: a run_stop reason -- the orchestrating session's own deliberate,
                   # healthy context-bound retirement, sibling to "budget"/"backlog-empty" (#547)
                   # for that SAME event kind, never a park reason_class. See loop.py's
                   # _handoff_reason and a downstream reader's budget metric for why this is
                   # deliberately EXCLUDED from budget-exhaustion counting despite sharing the
                   # vocabulary.
                   "handoff")

#: #140: spec §A.3's `retro.grade` vocabulary, mirrored from `sigma-retro/SKILL.md` §3's
#: achieved/partial/diverged bullets — it had no Python home before this. Like
#: PHASE_KINDS/GATE_KINDS above, documented but deliberately NOT enforced by append() itself;
#: `loop.py emit` is what validates a `retro` event's `--grade` value against it.
#: Sibling pin: tests/test_ledger.py::test_retro_grades_matches_sdlc_retro_skill_prose (this
#: file) and contract/vocabulary.json's "retro_grades" -- issue #298. Update both by hand.
RETRO_GRADES = ("achieved", "partial", "diverged")

_ACTOR_CACHE = {}


# --------------------------------------------------------------------------- config


def _config(sdlc_dir):
    """Read config.json. Deliberately a 1-line duplicate of state.load_config rather than an
    import: this module stays usable (and testable) without pulling the run-state layer in."""
    return json.loads((pathlib.Path(sdlc_dir) / "config.json").read_text())


def settings(config):
    """The `ledger` block, or `{}`. Total: a non-object config or block (a hand-edited `"ledger": true`)
    reads as absent instead of raising on `.get`."""
    block = config.get("ledger") if isinstance(config, dict) else None
    return block if isinstance(block, dict) else {}


def enabled(config):
    """Strict `is True` — a truthy string or a stray 1 does not silently switch a team
    coordination surface on."""
    return settings(config).get("enabled") is True


#: a crashed claimer's lock auto-expires; goals live hours, not days. Was loop.py-local
#: (`_DEFAULT_LEASE_TTL_HOURS`) until `work.py start()` (F10.5/#374) also needed it — moved here,
#: the shared home for lease/claim logic, rather than duplicated a second time.
DEFAULT_LEASE_TTL_HOURS = 12


def lease_ttl_seconds(config):
    """`ledger.lease.ttl_hours` (default `DEFAULT_LEASE_TTL_HOURS`) as seconds for
    `open_claims`/`open_claims_detailed`'s `ttl_seconds`, or None for "never expire" (config
    `0`/`false`). One place to read this setting so `loop.py`'s `_lease()` and `work.py`'s
    resume-safety guard can never drift on what "expired" means."""
    hours = (settings(config).get("lease") or {}).get("ttl_hours", DEFAULT_LEASE_TTL_HOURS)
    return float(hours) * 3600 if hours else None


# Three read helpers for the old feature name lived here until #2574/S1-G3, and all three are
# DELETED rather than left as no-ops. `journal_settings()`/`journal_enabled()` below replace the
# config pair — keeping a second, differently-named reader of the same setting is exactly the trap
# a future reader falls into. The share-routing helper (the writer-side half of a local-vs-shared
# decision, mirrored by a downstream reader's own copy, now also gone) has no replacement at all:
# PRD §6.2 removes publishing outright (a journal line leaves the machine only through a separate,
# opt-in shipper), so there is no second destination left to choose between.
# All three are pinned gone by `tests/test_ledger.py`.


# ------------------------------------------------------------------- the journal switch

#: The keys an org's managed-settings file may lock, as the CORE recognises them (#2574/S1-G3).
#:
#: S1-G3 CREATES this constant; it does not rename one. `managed_settings.py`'s own "Lockable keys"
#: section was PROSE with no constant behind it, and the only other executable list lived on the
#: far side of the import boundary `tests/test_import_boundary.py` enforces — the core may never
#: import the private side. So the core needed its own, and this is it; `managed_settings.py`'s
#: prose now points here.
#:
#: Any downstream reader that keeps its own lockable-key list pins it to this one by hand.
LOCKABLE_KEYS = ("work.require_review", "gates.hard_plan_gate", "journal.enabled")

#: How long a managed-settings lease stays fresh. PRD §6.2: a `refreshed_at` older than this turns
#: the journal off, and the stated Ceiling is that a policy writer dead longer than this "stops the
#: journal until it is revived". That sentence only means something under TERMINAL semantics — see
#: `journal_on()`.
JOURNAL_LEASE_DAYS = 7

#: `str(path) -> ((st_mtime_ns, st_size), (status, locked, refreshed_at))`. Caches the FILE READ
#: only, never the resolved boolean: `config` is a caller-supplied argument, and folding it into
#: the key would make a changed config read stale.
_MANAGED_READ_CACHE = {}

_MANAGED_SETTINGS_MODULE = None
_MANAGED_SETTINGS_LOAD_ATTEMPTED = False


def _managed_settings_module():
    """Lazily load the sibling `managed_settings.py` — the same `spec_from_file_location` idiom
    `work.py:82` already uses for this exact module, and `_scrub_module()` below uses for
    `hooks/research_capture.py`. Invisible to `tests/test_import_boundary.py`'s ast-based guard
    (it parses real `ast.Import`/`ast.ImportFrom` nodes; this produces neither), so the boundary
    test stays untouched.

    Cached after the FIRST attempt, success or failure — one load per process. Fails open to
    `None`; `journal_on()` then reads `not-adopted` and falls through to config, which is the same
    answer every unadopted checkout already gets."""
    global _MANAGED_SETTINGS_MODULE, _MANAGED_SETTINGS_LOAD_ATTEMPTED
    if _MANAGED_SETTINGS_LOAD_ATTEMPTED:
        return _MANAGED_SETTINGS_MODULE
    _MANAGED_SETTINGS_LOAD_ATTEMPTED = True
    try:
        path = pathlib.Path(__file__).resolve().parent / "managed_settings.py"
        spec = importlib.util.spec_from_file_location("managed_settings", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _MANAGED_SETTINGS_MODULE = mod
    except Exception:                       # noqa: BLE001 - fail-open; see docstring
        _MANAGED_SETTINGS_MODULE = None
    return _MANAGED_SETTINGS_MODULE


def journal_settings(config):
    """The `journal` config block, or `{}` when there is none.

    `journal` is the ONLY key read. #2574/S1-G3 renamed this feature and read the old key for one
    release; #2706 dropped that read before the public snapshot, so a config that holds only the
    old key now reads as off (the shipped default), and nothing is migrated or announced.

    ISINSTANCE GUARDS BOTH SIDES. A hand-edited `"journal": "on"` or a non-dict config would
    otherwise raise `AttributeError` on `.get(...)`, and the journal switch sits at the TOP of
    `append()`, so the blast radius would be EVERY phase boundary."""
    cfg = config if isinstance(config, dict) else {}
    block = cfg.get("journal")
    return block if isinstance(block, dict) else {}


def journal_enabled(config):
    """The CONFIG half of the switch on its own, strict `is True` — mirroring `enabled()`, because
    a truthy string or a stray 1 must not switch a write surface on silently. Callers deciding
    whether to write want `journal_on()`, which also consults org policy."""
    return journal_settings(config).get("enabled") is True


def _parse_iso8601(value):
    """An ISO-8601 stamp to epoch seconds, or None when it is not one. Never raises.

    `Z` is normalised to `+00:00` (Python < 3.11's `fromisoformat` rejects the literal `Z`), and a
    naive stamp is read as UTC — the managed-settings writer's own documented format
    (`managed_settings.py`'s "File format (version 1)") is UTC with a trailing `Z`."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text[-1] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    try:
        return parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def _lease_fresh(refreshed_at, now=None, days=JOURNAL_LEASE_DAYS):
    """Is a managed-settings lease still inside its window? Never raises; absent, unparseable or
    FUTURE-dated all read as stale.

    KEYS ON THE `refreshed_at` FIELD, NEVER ON `stat().st_mtime`. A `cp`, a `git checkout` or a
    restored backup resets mtime, and an mtime-based lease would then renew a dead policy writer's
    policy forever — the file would look fresh every time it was copied. `.sdlc/managed-settings.json`
    is exactly the kind of file a device-management tool rewrites wholesale.

    A FUTURE stamp is not "fresh" either: a corrected clock or a restored backup would otherwise
    hold the lease open indefinitely. Same `0 <= t - parsed` shape as `timing_store.maybe_prune`'s
    own stamp guard, for the same reason."""
    parsed = _parse_iso8601(refreshed_at)
    if parsed is None:
        return False
    t = now if now is not None else time.time()
    return 0 <= t - parsed < days * 86400


def _managed_read_cached(sdlc_dir):
    """`managed_settings.read()` behind a per-path, stat-keyed cache. -> (status, locked, refreshed_at).

    One `stat` per `append()` (~19 µs measured on Darwin 25.6.0/APFS — not a claim about a network
    mount or a spinning disk), instead of a full read-and-parse. This is a per-CALL constant, not a
    per-item cost: 10x and 100x more events, goals or repos leave it unchanged.

    Caches the FILE READ, never the resolved boolean — `journal_on`'s `config` argument is supplied
    by the caller and folding it into the key would make a changed config read stale. Keyed on the
    resolved path string, so two checkouts in one process can never answer for each other.

    Never raises: any failure degrades to `not-adopted`, which is what every unadopted checkout
    already resolves to, so the caller falls through to config."""
    managed = _managed_settings_module()
    if managed is None:
        return ("not-adopted", None, None)
    try:
        path = pathlib.Path(sdlc_dir) / managed.MANAGED_SETTINGS_FILENAME
        key = str(path)
        try:
            st = path.stat()
            fingerprint = (st.st_mtime_ns, st.st_size)
        except OSError:
            fingerprint = None
        cached = _MANAGED_READ_CACHE.get(key)
        if cached is not None and fingerprint is not None and cached[0] == fingerprint:
            return cached[1]
        result = managed.read(str(sdlc_dir))
        _MANAGED_READ_CACHE[key] = (fingerprint, result)
        return result
    except Exception:                       # noqa: BLE001 - the switch never raises; see docstring
        return ("not-adopted", None, None)


def journal_on(sdlc_dir, config):
    """THE journal switch. Org policy first, then the local config. Never raises.

    PRD §6.2's three steps, in order:

      1. a REFUSING managed-settings status (`access-revoked`, `locked-key-unverifiable`) -> OFF.
         An org that has revoked this member, or a file nobody can verify, does not get a journal.
      2. status `ok` AND `journal.enabled` in `locked` -> the LOCKED value, but only while the
         lease is fresh. The lock is PER KEY: a `locked` dict carrying other keys leaves the
         journal to the config branch below.
      3. otherwise -> `journal_enabled(config)`.

    A STALE LEASE IS TERMINAL OFF, NOT A FALL-THROUGH (plan Delta-1). §6.2's lease bullet says a
    `refreshed_at` older than 7 days "turns the journal off", and its stated Ceiling says a
    policy writer dead for more than 7 days "stops the journal until it is revived". Under
    fall-through semantics neither sentence means anything on a repo whose config says `true` — the
    config would simply turn it back on and nothing would stop. The cost is named and accepted: a
    dead policy writer darkens the journal until revived, and doctor's journal row says "off because the managed
    settings are N days old" so the state names itself rather than going quiet (PRD §6.1).

    NEVER RAISES, on BOTH branches. The managed read degrades to `not-adopted`; the config branch
    is isinstance-guarded inside `journal_settings()`. This sits at the top of `append()`, which
    runs at every phase boundary, so an exception here would not cost one write, it would cost the
    boundary."""
    managed = _managed_settings_module()
    status, locked, refreshed_at = _managed_read_cached(sdlc_dir)
    if managed is not None and status in managed.REFUSING_STATUSES:
        return False
    if managed is not None and status == managed.STATUS_OK \
            and isinstance(locked, dict) and "journal.enabled" in locked:
        return _lease_fresh(refreshed_at) and locked["journal.enabled"] is True
    return journal_enabled(config)


# --------------------------------------------------------------------------- paths


def ledger_dir(sdlc_dir):
    return pathlib.Path(sdlc_dir) / "ledger"


def local_events_dir(sdlc_dir):
    """THE journal directory: the one destination for the EVENTS stream (#2574/S1-G3; #244 created
    it as one of two). A SIBLING of `ledger_dir()`, deliberately NOT inside it (spec §A.2): unlike
    `.sdlc/ledger/`, this path carries no dependency on a ledger worktree existing or having been
    gitignored via `/sigma-ledger`'s `ensure_ignore()`, because it is never part of that worktree at
    all. It gets its own `RUNTIME_IGNORES` entry instead (`setup.py`).

    Local and gitignored is the POINT, not an implementation detail: PRD §6.2 deletes publishing
    ("never published — git cannot hold events"), so no event written here is ever staged, committed
    or pulled by a teammate. Matches a downstream reader's own `base / "events"` glob byte-for-byte
    — that reader now reads this path unconditionally, so the writer's destination
    must equal the reader's, not merely resemble it."""
    return pathlib.Path(sdlc_dir) / "events"


def entries_dir(sdlc_dir, stream=ENTRIES):
    if stream not in STREAMS:
        raise ValueError(f"unknown ledger stream {stream!r} (expected one of {', '.join(STREAMS)})")
    return ledger_dir(sdlc_dir) / stream


def _host_token():
    """A short, stable per-MACHINE component of the writer identity (#540).

    A pid alone stopped being unique the moment two machines shared one login. Several hosts
    authenticating as the same bot account all resolve to the same `who`, and a fresh container pid
    namespace hands out low pids, so two of them routinely draw the SAME pid — at which point their
    filenames, their entry `id`s and their `my_writer()` strings were all byte-identical. The
    `entries/*.jsonl merge=union` attribute lands both files' lines with no conflict and exit 0, so
    nothing anywhere surfaced the collision; it showed up only as a hand-off that never arrived.

    Derived from the hostname rather than a uuid persisted under `.sdlc/state/`: this is a pure
    function needing no I/O, so it cannot race two concurrent processes into two different answers,
    cannot fail on a read-only or missing state dir, and needs no migration for existing clones —
    all properties this module's fail-open posture wants. The cost is honest: two hosts that have
    been given the SAME hostname AND draw the same pid still collide. That is a much narrower window
    than today's (any two containers, since the default container hostname is the container id), and
    a persisted per-clone uuid is the strictly stronger option if that window ever bites.

    Never raises — an unresolvable hostname hashes the empty string, which is stable, so the worst
    case degrades to exactly the pre-#540 behavior rather than to an error."""
    try:
        name = platform.node() or ""
    except Exception:                # noqa: BLE001 - identity must never be what breaks an append
        name = ""
    return hashlib.sha256(name.encode("utf-8", "replace")).hexdigest()[:8]


def _instance_token():
    """`<host>.<pid>` — the ONE writer-instance string, shared verbatim by the filename
    (`entry_file`), the entry `id` (`append`) and the read-side writer identity (`my_writer`), so
    the three can never drift apart again. The `.` separator is deliberate: `files_for()` splits a
    filename on its LAST `-` to recover the actor, and an actor name may itself contain `-`, so
    adding a second `-`-delimited segment would have made that split ambiguous. A `.` keeps the
    actor recoverable by the same exact-match rule as before."""
    return f"{_host_token()}.{os.getpid()}"


def entry_file(sdlc_dir, who, stream=ENTRIES):
    """F10: the filename carries the WRITING PROCESS, not just the actor. The per-author-file design
    assumed one writer per file, but the actor resolves to the authenticated `gh` login — and several
    parallel loops under a single shared account (all loops authenticate as the same user) all
    resolve to the SAME `who`. Two concurrent appends to one file then read the same `_line_count`
    and mint the same `id` (`shared:1 == shared:1`), breaking the monotonic-per-author guarantee the
    watcher-resume cursor + `open_claims` lease rely on. The pid makes concurrent writers land in
    different files instead — `read_all()`/`render()` already union EVERY `*.jsonl` under the stream
    dir and attribute by the `actor` FIELD inside each entry (never the filename), so THOSE needed no
    change. `sync.py`'s `publish()`/`bootstrap()` are the exception: they name a specific file to
    stage rather than reading via `read_all()`, so they go through `files_for()` below to find every
    file a given actor (any writer instance) has written.

    #540 extends the same reasoning one dimension out: a pid distinguishes two processes on ONE
    machine, but not two machines sharing a login, so the writer instance is `<host>.<pid>` (see
    `_host_token`). Legacy `<actor>-<pid>.jsonl` files stay readable — nothing re-reads by
    filename, and `files_for()` accepts both shapes.

    #2574/S1-G3: EVENTS RESOLVES TO `local_events_dir()` UNCONDITIONALLY, and the `local=` parameter
    #244 added is DELETED rather than kept as a no-op (plan Delta-5). Destination used to key on the
    old `share` setting; publishing is gone, so there is exactly one destination and no decision to
    express. A vestigial flag that routes nothing reads like a choice that still exists, and the
    next person to see it would reasonably wire it back up. The filename shape itself —
    `{safe_name}-{instance}.jsonl` — is what it always was; only the parent directory is fixed now.
    ENTRIES is untouched: it lands under `entries_dir(sdlc_dir, ENTRIES)` exactly as before."""
    base = local_events_dir(sdlc_dir) if stream == EVENTS else entries_dir(sdlc_dir, stream)
    return base / f"{_safe_name(who)}-{_instance_token()}.jsonl"


def files_for(directory, who):
    """Every file `who` has ever written into a ledger stream directory (an `entries_dir()`/
    `events_dir()` result) — one per writing process (see `entry_file()`), plus a pre-#337 bare
    `<who>.jsonl` if one is still sitting there unpublished from before this plugin version.
    `sync.py`'s `publish()`/`bootstrap()` need this: unlike `read_all()`, which safely unions every
    `*.jsonl` in the directory, they must name exactly which files are (or might be) `who`'s to
    stage — never a teammate's.

    Matches the safe name exactly, never as a prefix: a naive `<safe>-*` glob would also match a
    DIFFERENT actor whose safe name happens to start with `<safe>-` (e.g. "team" matching
    "team-bot"'s files), so each candidate is split on its trailing `-<pid>` and the remainder is
    compared for equality first.

    #488: the comparison is CASE-INSENSITIVE. `pathlib.Path.glob()` is a pure Python string match
    that never consults the real filesystem's own case sensitivity, even where the filesystem IS
    case-insensitive (macOS APFS default, Windows NTFS) — so the old case-sensitive
    `{safe}-*.jsonl` glob could miss a file that a case-insensitive `open()` elsewhere in this
    module (`entry_file()`, via `append()`) happily wrote into, because `ledger.actor` in
    config.json is a hand-typed string whose casing can drift between runs (e.g. unset, so an
    earlier run fell back to `gh api user`'s lowercase login; a later hand-typed override uses
    different casing). Comparing case-insensitively closes that gap in BOTH directions, without
    needing to know which casing is "canonical": on a case-insensitive filesystem, two names that
    only differ by case were never two different actors to begin with. Real cross-actor collisions
    stay excluded by the same exact-match (post-lowercasing) guard above; for a `gh`-resolved
    actor that exact match is backed by GitHub's own case-insensitive username-uniqueness, and for
    a hand-typed `ledger.actor` override it carries the same trust this module already places in
    config.json elsewhere — unchanged by this fix, not a new assumption."""
    d = pathlib.Path(directory)
    if not d.is_dir():
        return []
    safe = _safe_name(who)
    safe_lower = safe.lower()
    out = []
    for p in d.glob("*.jsonl"):
        name, _, instance = p.stem.rpartition("-")
        if name.lower() == safe_lower and _is_instance_token(instance):
            out.append(p)
    legacy = d / f"{safe}.jsonl"
    if legacy.exists():
        out.append(legacy)
    return sorted(out)


def _is_instance_token(text):
    """Does this trailing filename segment name a writer INSTANCE? `<host>.<pid>` (#540) or a bare
    `<pid>` (pre-#540, still on disk in any clone that has not re-published since). Anything else
    means the `-` we split on belongs to the actor's own name, not to a writer instance, so the file
    is not a match — which is what keeps `files_for("team")` from claiming `team-bot`'s files."""
    host, dot, pid = str(text).rpartition(".")
    return text.isdigit() if not dot else bool(host) and pid.isdigit()


def _safe_name(who):
    """A GitHub login can contain a hyphen but never a path separator; be defensive anyway so
    a bad config value can never write outside the entries directory."""
    keep = [c for c in str(who) if c.isalnum() or c in "-_."]
    return ("".join(keep).strip(".-_") or "unknown")[:64]


# --------------------------------------------------------------------------- actor


def _run_gh(args):
    proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def actor(config, run=None):
    """Who is writing. Config wins; else the authenticated account; else the shell user.
    Never raises and never blocks — an unresolvable actor writes as `unknown` rather than
    losing the entry."""
    configured = (settings(config).get("actor") or "").strip()
    if configured:
        return _safe_name(configured)
    key = id(run) if run else "default"
    if key in _ACTOR_CACHE:
        return _ACTOR_CACHE[key]
    resolved = ""
    try:
        resolved = (run or _run_gh)(["api", "user", "-q", ".login"])
    except Exception:
        resolved = ""
    resolved = resolved or os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown"
    _ACTOR_CACHE[key] = _safe_name(resolved)
    return _ACTOR_CACHE[key]


def reset_actor_cache():
    """Tests (and a long-lived process whose auth changed) need to re-resolve."""
    _ACTOR_CACHE.clear()


# --------------------------------------------------------------------------- free-text sanitizing (#141)

_SCRUB_MODULE = None
_SCRUB_LOAD_ATTEMPTED = False


def _scrub_module():
    """Lazily importlib-load `hooks/research_capture.py` so `ledger.py` reuses its `_scrub`/
    `_SECRET_PATTERNS` — the one scrubber in the repo — instead of duplicating the pattern table
    (two tables is exactly the drift this repo has already been bitten by). This is the REVERSE
    direction of `hooks/decision_gate.py`'s own cross-load (that file loads THIS module the other
    way, at its `_emit_decision_event`): both are the same `importlib.util.spec_from_file_location`
    sibling-load idiom, and both are invisible to `tests/test_import_boundary.py`'s ast-based guard
    (that checker only parses real `ast.Import`/`ast.ImportFrom` nodes; `spec_from_file_location`
    produces neither — confirmed against the guard's own module docstring).

    Cached after the FIRST attempt, success or failure — one load per process, not one per call.
    Fails open to `None` (never raises) if `hooks/research_capture.py` is missing or broken in some
    checkout; the caller then treats scrub as a no-op and degrades to flatten+cap only. That
    degrade is a real privacy reduction on its own (flatten/cap are plain string ops right here in
    `ledger.py` and never touch this loader, so the worst case becomes "<=200 chars, single line,
    unredacted" instead of unbounded raw text) — but it must never be SILENT: one line to stderr,
    the same idiom `safe_append` already uses for its own non-fatal failures (see that function's
    "ledger: entry skipped (non-fatal): ..." message), so a broken install is visible in the log
    even though no run ever breaks because of it."""
    global _SCRUB_MODULE, _SCRUB_LOAD_ATTEMPTED
    if _SCRUB_LOAD_ATTEMPTED:
        return _SCRUB_MODULE
    _SCRUB_LOAD_ATTEMPTED = True
    try:
        import importlib.util
        # ledger.py lives at skills/sigma-loop/scripts/ledger.py; hooks/ is a sibling of skills/ at
        # the repo root — four `.parent`s up from this file, matching decision_gate.py's own
        # two-`.parent`s-up-then-down-into-skills/sigma-loop/scripts symmetry in the other direction.
        repo_root = pathlib.Path(__file__).resolve().parent.parent.parent.parent
        path = repo_root / "hooks" / "research_capture.py"
        spec = importlib.util.spec_from_file_location("research_capture", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _SCRUB_MODULE = mod
    except Exception as exc:                                    # noqa: BLE001 - fail-open by design
        print(f"ledger: scrub unavailable, degrading to flatten+cap only (non-fatal): {exc}",
              file=sys.stderr)
        _SCRUB_MODULE = None
    return _SCRUB_MODULE


def _sanitize_free_text(value, cap=FREE_TEXT_CAP):
    """flatten -> scrub -> cap, IN THAT EXACT ORDER — never reorder this. An independent review
    proved by execution that a secret whose match starts near char 195 of a 215-char string is
    fully redacted when scrub runs BEFORE the 200-char cap, but capping first truncates the match
    mid-pattern and leaves an unredacted FRAGMENT (e.g. the literal `AKIAI...`) inside the capped
    text, because the scrubber never gets to see the whole shape once it's been cut off. Flattening
    first (not last) matters too: it turns a raw multi-line block into one line before either the
    scrubber or the cap sees it, so a cap can never land mid-multi-line-secret in a way flatten
    would otherwise have prevented.

    `cap` defaults to FREE_TEXT_CAP (declared prose). POST-REVIEW FIX: the bounded-identifier and
    the numeric/bool-fallback call sites in `append()` below pass `cap=BOUNDED_ID_CAP` — same
    function, same order, just a shorter ceiling for a value that's an id/path/miscoerced-field
    rather than prose.

    Never raises on its own: a missing/broken scrub module (see `_scrub_module`) degrades to
    flatten+cap only, and a scrub call that itself raises is caught here too — this function is
    called from inside `append()`'s field-writing loop, which must stay exception-safe for the
    fail-open call sites (a hook's `deny`, an autonomous park) that route through it. `str(value)`
    never raises regardless of what lands here — a dict, an int, bytes, `None` all stringify
    cleanly, which is exactly why sanitizing (rather than coercing) is the safe fallback for a
    declared-numeric/bool field that turns out not to hold one."""
    text = str(value).replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    mod = _scrub_module()
    if mod is not None:
        try:
            text = mod._scrub(text)
        except Exception as exc:                                # noqa: BLE001 - fail-open by design
            print(f"ledger: scrub failed, degrading to flatten+cap only (non-fatal): {exc}",
                  file=sys.stderr)
    return text[:cap]


_BOOL_SPELLINGS = {"true", "false", "1", "0", "yes", "no"}


def _looks_numeric(value):
    """True when `value` parses cleanly as a whole number — an int (or bool, its subclass) as-is,
    or a string shaped like one. Shared by `append()`'s chokepoint sanitizer below AND `loop.py`'s
    `_validate_event` CLI refusal (which reaches in directly — the same cross-module idiom
    `_scrub_module` above already uses in the other direction, importing `hooks/research_capture`'s
    `_scrub`) so a numeric field's definition of "valid" can never drift between the two
    enforcement points. Never raises.

    TWO independent bounds, not belt-and-braces:

    NUMERIC_DIGIT_CAP bounds an encoded value's *length*. Parsing as an int says nothing about what
    the digits ENCODE: base-10-encoding a secret as one giant integer —
    `str(int.from_bytes(b"AKIA...|ghp_...", "big"))`, 118 digits — passes a purely syntactic check,
    so without the cap it skips the scrubber and lands raw, and `int(v).to_bytes()` reads it straight
    back off disk. The cap closes that channel's bandwidth below anything useful and closes the plain
    unbounded-length hole (50k digits written verbatim) in the same stroke, INCLUDING a small-
    magnitude value padded to great length (`"0" * 50000 + "5"` parses to 5 yet is 50k chars).

    The signed-64-bit RANGE check bounds *magnitude* (issue #787). It is the storability guarantee:
    the shared contract is that a value the CLI accepts is a value the ledger carries end to end, and
    every numeric column a downstream store maps is at most 64-bit. The signed-64-bit maximum is 19
    digits (2**63-1 == 9223372036854775807), so ANY 20-digit value exceeds it (a digit count cannot
    express the bound: some 19-digit values overrun it too), so a purely digit-counting bound let an
    in-spec CLI write
    produce a record a downstream ingester could never INSERT — permanently stalling that writer's stream.
    Counting digits cannot express this bound (`cycle`/`exit_code` were even narrower, 32-bit, before
    #787 widened them to BIGINT), so the range check carries it and the cap keeps its own length job.
    Both live HERE, in the one function both enforcement points share, so neither can drift."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return False
    if not (_INT64_MIN <= parsed <= _INT64_MAX):
        return False
    return len(str(value).strip().lstrip("+-").replace("_", "")) <= NUMERIC_DIGIT_CAP


def _looks_bool(value):
    """True for an actual bool, or a string spelled like one (`_BOOL_SPELLINGS`, case-insensitive).
    Same sharing rationale as `_looks_numeric`. Never raises."""
    if isinstance(value, bool):
        return True
    return isinstance(value, str) and value.strip().lower() in _BOOL_SPELLINGS


def reject_newline(value, label):
    """The hard-reject-a-raw-newline rule shared by every synchronous, agent-typed CLI verb that
    must REFUSE (not merely flatten) a multi-line value: `loop.py`'s `_validate_event` and
    `work.py`'s `main()` post-review branch hand-wrote this identically twice before this — the
    same "guard duplicated at call sites instead of chokepointed" shape #141's whole story is
    about. A third CLI verb now has one shared place to call instead of a third drifting copy.

    `append()` itself must NEVER call this: it flattens instead of rejecting (see
    `_sanitize_free_text`), because it also sits behind the three deterministic, fail-open call
    sites (a hook's `deny`, an autonomous park) that must never raise on bad input.

    Returns the refusal message (already naming `label`), or `None` when `value` has no raw
    newline. Never raises — `str(value)` handles anything."""
    if "\n" in str(value):
        return f"newline not allowed in {label} (the events stream is single-line only)"
    return None


# --------------------------------------------------------------- journal retention (30 days)

#: How long a journal file is kept. Bounded growth is a design-time requirement, not an operational
#: surprise. The prior art copied is `timing_store.py`'s own retention (`prune`/`maybe_prune`/
#: `_prune_if_due`) — append-only, pruned to a rolling window on the write side, fail-open
#: throughout — with ONE deliberate deviation, documented on `_maybe_prune_journal`.
JOURNAL_RETENTION_DAYS = 30

#: The sweep runs at most this often across ALL callers, throttled by a stamp file. Every hook and
#: every `phase_report.py` invocation is a fresh process, so "once per process" alone would mean
#: "on every event" for them.
JOURNAL_PRUNE_INTERVAL_SECONDS = 86400

#: One `stat` of the stamp per PROCESS; the stamp then decides whether a sweep runs.
_journal_pruned_this_process = False


def _is_link(path):
    """A symlink — or, on Windows, any reparse point: a junction reports as a plain directory to
    `is_symlink()` on Python 3.11, and a junction is exactly what a hostile checkout can plant.

    Ported from `timing_store.py` rather than imported: this module is cross-loaded by
    `hooks/decision_gate.py` and by `doctor.py` through `spec_from_file_location`, and giving it a
    new sibling dependency for six lines would make every one of those loads fragile for nothing."""
    try:
        if path.is_symlink():
            return True
        if os.name == "nt":
            return getattr(os.lstat(path), "st_reparse_tag", 0) != 0
    except OSError:
        return False
    return False


def _refuse_journal_links(path, sdlc_dir):
    """Raise unless `path` is a plain path inside the journal directory: no component from it up
    to `local_events_dir(sdlc_dir)` may be a link, and its real path must resolve inside that
    directory's real path.

    The sweep `unlink`s and the stamp `write_text`s, and BOTH follow links. A link planted inside
    `.sdlc/events/` — committable on macOS/Linux, a junction on Windows — would otherwise turn a
    phase boundary into deleting or overwriting a file outside the journal, exit 0, silently.
    Components ABOVE the journal directory (`.sdlc`, the project) are the host's business and are
    not checked, exactly as `timing_store._refuse_links` scopes itself to the timing store."""
    root = local_events_dir(sdlc_dir).absolute()
    q = pathlib.Path(path).absolute()
    while True:
        if _is_link(q):
            raise ValueError(f"{q} is a link; the journal never writes through a link")
        if q == root:
            break
        if q.parent == q:
            raise ValueError(f"{path} is outside the journal directory")
        q = q.parent
    real_root = pathlib.Path(os.path.realpath(root))
    real = pathlib.Path(os.path.realpath(path))
    if real_root not in real.parents and real != real_root:
        raise ValueError(f"{path} resolves outside the journal directory")


def prune_journal(sdlc_dir, keep_days=JOURNAL_RETENTION_DAYS, now=None):
    """Drop journal files older than the window. -> the names removed (sorted), possibly empty.

    SCOPE IS `.sdlc/events/` ONLY, NEVER `.sdlc/ledger/events/` (F-2/D-5). `sync.py` stages the
    latter into the ops-branch worktree, where those files are git-TRACKED; a retention sweep
    reaching them would create staged deletions in a live worktree and destroy a teammate's pull.
    Widening the scope later is additive; narrowing it after the fact would not be.

    THE WRITING PROCESS'S OWN FILE IS NEVER A CANDIDATE, keyed on the writer-instance suffix
    (`-<host>.<pid>.jsonl`) and NOT on mtime: a long-running loop process's file can easily predate
    the window while still being the file this very append is about to write into.

    NEVER `mkdir`s and never raises — see `_maybe_prune_journal` for why the first of those is
    load-bearing, and `timing_store.prune` for the second (retention runs on the write path, and a
    failed sweep must cost a little disk, never an event).

    SCALABILITY, measured against THIS function on Darwin 25.6.0/APFS, warm cache, median of 3:
    2,591 files -> 23.6 ms; 25,910 files (10x) -> 278.8 ms. Growth is linear in file count, so 100x
    projects to roughly 2.8 s — projected, not measured.

    That is ~3x the plan's own 7.5-8.7 ms figure, and the difference is real rather than noise: the
    plan measured a bare `glob('*.jsonl') + stat`, while this function also pays a `sorted()`, a
    `name.endswith` and an `lstat` per file for `_is_link`. The link check is a security
    requirement (S1), not an optimisation target, so the honest number is the one above.

    Acceptable once per process behind a 24h stamp; NOT acceptable per append, which is why the
    process flag and the stamp are both load-bearing rather than belt-and-braces. Not a claim about
    a network mount or a spinning disk.

    RETENTION RESETS `seq`, so a journal `id` is no longer unique for a project's lifetime — and
    the ingest side's `id@ts` key is what absorbs that. Deleting a file removes the only record of
    how far its per-file counter got, so a later process that draws the same pid on the same host
    mints `<actor>:<host>.<pid>:1` again. MEASURED, by pinning `_instance_token` to model pid reuse
    and ingesting both rounds: nothing is lost, because the downstream ingester's seen-set keys on
    `id@ts` rather than `id`, and the second round ingested cleanly (3 event rows -> 6). Written down because
    that `@ts` suffix is now load-bearing for a reason that did not exist before #2574: before
    retention, ids never repeated, so the suffix was belt-and-braces. Do not "simplify" it away."""
    root = local_events_dir(sdlc_dir)
    # A linked journal root would make every "inside" path resolve elsewhere; the sweep DELETES,
    # so a link anywhere on its path means it does nothing at all.
    if _is_link(root):
        return []
    cutoff = (now if now is not None else time.time()) - keep_days * 86400
    mine = f"-{_instance_token()}.jsonl"
    removed = []
    try:
        candidates = sorted(root.glob("*.jsonl"))
    except OSError:
        return []
    for path in candidates:
        try:
            if path.name.endswith(mine):
                continue
            if _is_link(path) or not path.is_file():
                continue
            if path.stat().st_mtime >= cutoff:
                continue
            _refuse_journal_links(path, sdlc_dir)
            path.unlink()
            removed.append(path.name)
        except (OSError, ValueError):
            continue
    return sorted(removed)


def _maybe_prune_journal(sdlc_dir, now=None):
    """The sweep, if the stamp says it is due; else nothing. -> the sweep's result, or None when it
    did not run. Fail-open: a stamp that cannot be read or written costs at most one extra sweep.

    THE ONE DELIBERATE DEVIATION FROM `timing_store.maybe_prune` (F-1). That function does
    `stamp.parent.mkdir(parents=True, exist_ok=True)` before writing its stamp. Copying it here
    would CREATE `.sdlc/events/` on a journal-off install, because prune runs before the gate and
    `append()` is called unconditionally by `phase_report.py` and `loop.py` on a core-only install.
    Acceptance bullet 1 — "a fresh `/sigma-init` config writes zero journal bytes" — would then be
    false on the shipped default forever. So: an early return on a missing directory, and NO
    `mkdir` anywhere in this module's retention path. Control C-5 breaks exactly this.

    THE STAMP LIVES INSIDE THE JOURNAL DIRECTORY, not in `store_dir()` where `timing_store` keeps
    its own. That is what makes the early return sufficient: the stamp's parent IS the directory
    whose absence we just returned on, so by the time it is written the directory certainly exists
    and no `mkdir` is ever needed. The cost is that `.sdlc/events/` holds one non-`.jsonl` file —
    every reader glob and the sweep itself are `*.jsonl`, so nothing reads it by accident, but a
    count taken with `iterdir()` rather than a `*.jsonl` glob would see it."""
    root = local_events_dir(sdlc_dir)
    try:
        if not root.exists():
            return None
    except OSError:
        return None
    # A linked journal root: neither swept (`prune_journal` refuses it too) nor stamped.
    if _is_link(root):
        return None
    t = now if now is not None else time.time()
    stamp = root / ".last-prune"
    # A linked stamp is hostile (`write_text` follows it): neither write nor sweep. A stamp dated
    # in the FUTURE is not "recently swept" either — a corrected clock or a restored backup would
    # otherwise suppress retention forever (`timing_store.py`'s own C3).
    if _is_link(stamp):
        return None
    try:
        if stamp.exists() and 0 <= t - stamp.stat().st_mtime < JOURNAL_PRUNE_INTERVAL_SECONDS:
            return None
    except OSError:
        pass
    removed = prune_journal(sdlc_dir, now=t)
    try:
        _refuse_journal_links(stamp, sdlc_dir)
        stamp.write_text(str(int(t)), encoding="utf-8")
        os.utime(stamp, (t, t))
    except (OSError, ValueError):
        pass
    return removed


def _prune_journal_if_due(sdlc_dir):
    """Once per process reaches the stamp check; the stamp decides whether a sweep runs. Set the
    flag BEFORE the call, not after: a raising sweep must not leave it clear and retry on every
    subsequent write for the life of the process."""
    global _journal_pruned_this_process
    if _journal_pruned_this_process:
        return
    _journal_pruned_this_process = True
    try:
        _maybe_prune_journal(sdlc_dir)
    except Exception:                       # noqa: BLE001 - retention never breaks a write
        pass


# --------------------------------------------------------------------------- write


def _is_sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", value)


def _is_key(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)


def _validate_typed_event(kind, fields):
    """Reject incomplete or malformed observation facts before the journal is opened."""
    schema = EVENT_SCHEMAS.get(kind)
    if not schema:
        return
    missing = [name for name in schema["required"] if name not in fields or fields[name] in (None, "")]
    if missing:
        raise ValueError(f"{kind} missing required fields: {', '.join(missing)}")
    if kind == "merge_observed":
        valid = (_is_key(fields["observation_key"]) and fields["subject_kind"] in ("goal", "branch")
                 and isinstance(fields["subject"], str) and 1 <= len(fields["subject"]) <= 256
                 and isinstance(fields["pr"], int) and fields["pr"] > 0 and _is_sha(fields["merge_sha"]))
    elif kind == "review_posted":
        valid = (_is_key(fields["observation_key"]) and _is_key(fields["brief_hash"])
                 and isinstance(fields["evidence_id"], str)
                 and re.fullmatch(r"[A-Za-z0-9_-]{16,128}", fields["evidence_id"] or "")
                 and isinstance(fields["comment_id"], int) and fields["comment_id"] > 0
                 and isinstance(fields["pr"], int) and fields["pr"] > 0 and _is_sha(fields["head_sha"])
                 and fields["verdict"] in ("approve", "block", "unblock"))
    else:
        checks = fields["checks"]
        valid = (_is_key(fields["observation_key"]) and isinstance(fields["pr"], int) and fields["pr"] > 0
                 and _is_sha(fields["head_sha"]) and fields["gate_verdict"] in ("pass", "warn", "block")
                 and isinstance(fields["checks_total"], int) and 0 <= fields["checks_total"] <= 100000
                 and isinstance(fields["checks_truncated"], bool) and isinstance(checks, list)
                 and len(checks) <= 50 and fields["checks_total"] >= len(checks)
                 and fields["checks_truncated"] == (fields["checks_total"] > 50)
                 and all(isinstance(item, dict) and set(item) == {"name", "conclusion"}
                         and isinstance(item["name"], str) and 1 <= len(item["name"]) <= 256
                         and item["conclusion"] in ("pass", "fail", "pending") for item in checks))
    if not valid:
        raise ValueError(f"invalid typed {kind} payload")


def append(sdlc_dir, config, kind, goal, run=None, now=None, stream=ENTRIES, **fields):
    """Append one line to THIS actor's file on the given stream. Returns the entry, or None
    when the ledger is off.

    `id` is `<actor>:<seq>` where seq counts this file's existing lines — monotonic per
    `(actor, stream)`, which is exactly what a watcher needs for a resume cursor, and needs no
    shared counter.

    `stream` defaults to `ENTRIES` (the pre-#136 behavior, byte-for-byte). `EVENTS` is a
    separate closed vocabulary (`EVENT_KINDS`/`EVENT_FIELDS`) written to its own per-actor file,
    so it can never collide with or dilute the entries kinds a lead reads in TEAM.md.

    Unknown `phase`/`gate`/`reason_class` VALUE does not raise — only `kind` (both streams) and
    `verdict` (events/`gate` only) are enforced, matching the issue's Done criteria verbatim.
    `PHASE_KINDS`/`GATE_KINDS`/`REASON_CLASSES` are the documented vocabulary but tightening
    them to enforced-at-write is a scope expansion later work can add, the same way `STATES`
    already does for entries, without another signature change.

    #2574/S1-G3: THE EVENTS GATE IS `journal_on(sdlc_dir, config)` — the journal switch, which is
    org policy first (a managed-settings lock and its 7-day lease) and then the local config's
    `journal.enabled` (the only key read; see `journal_settings`). It replaces the config
    read #244 had already decoupled from `enabled(config)` (the
    ledger's own flag); that decoupling survives unchanged — the journal writes with the team
    ledger off entirely, and ENTRIES stays gated on `enabled(config)` alone, untouched by the
    journal.

    DESTINATION NO LONGER ROUTES. Every EVENTS write lands in `local_events_dir(sdlc_dir)`
    (`.sdlc/events/`), whatever the config says. The old `share` setting — which chose between that
    path and the shared `entries_dir(sdlc_dir, EVENTS)` inside the ledger's own git worktree — is
    DELETED, with its routing helper, `entry_file(local=)` and `sync.py`'s staging of the
    events stream. PRD §6.2: "never published … git cannot hold events", so publishing is removed
    rather than defaulted off. `.sdlc/ledger/events/` is still READ for one release (doctor,
    `phase_report.py`'s crash repair, `time_report.py`, and a downstream reader, all unioning
    both directories), so an adopter's existing history stays visible; nothing writes it.

    That deletion also retires the "RESIDUAL, ACCEPTED, NOT SILENT" paragraph that stood here: the
    residual — a journal-on + `ledger.enabled: false` config writing into the ledger
    worktree's directory because destination keyed only on `share` — existed *because* destination
    keyed on `share`. With one destination, and that destination a gitignored sibling of
    `.sdlc/ledger/`, the combination it described cannot arise. Pinned by executed tests
    (`tests/test_ledger.py`'s EVENTS section), not merely asserted here.

    #2574/S1-G3: JOURNAL RETENTION RUNS FIRST, BEFORE THE STREAM VALIDATION AND BEFORE EITHER
    GATE. Placed after the gate it would never run on a journal-OFF install, which is the only
    case §6.2's retention exists for — a developer who turns the journal off is exactly the person
    whose old events nobody will ever come back for. That placement is what control C-4b breaks.
    It costs one `stat` per process (the flag), then one `stat` of the stamp, then at most one
    sweep per 24h across every process on the machine."""
    _prune_journal_if_due(sdlc_dir)
    if stream not in STREAMS:
        raise ValueError(f"unknown ledger stream {stream!r} (expected one of {', '.join(STREAMS)})")

    if stream == ENTRIES:
        if not enabled(config):
            return None
        if kind not in KINDS:
            raise ValueError(f"unknown ledger kind {kind!r} (expected one of {', '.join(KINDS)})")
        state = fields.get("state")
        if state is not None and state not in STATES:
            raise ValueError(f"unknown ledger state {state!r} (expected one of {', '.join(STATES)})")
        field_whitelist = OPTIONAL_FIELDS
        if kind == "merged":
            key = fields.get("merged_entry_key")
            if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
                raise ValueError("merged_entry_key must be 64 lowercase hexadecimal characters")
        if kind == "acceptance" and "acceptance_sha256" in fields:
            key = fields["acceptance_sha256"]
            if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
                raise ValueError("acceptance_sha256 must be 64 lowercase hexadecimal characters")
        # #1319: fill `autowatch_hop` from the process environment when the caller didn't pass one
        # explicitly — mirrors `state.run_identity()`'s own `SIGMA_RUN_ID` precedent (see
        # OPTIONAL_FIELDS' own comment above). autowatch.py sets `SIGMA_AUTOWATCH_HOP` on the
        # driven `/sigma-loop` subprocess's environment before launching it; every entries-stream
        # write ANYWHERE in that process tree (handoff.py, loop.py, ...) then automatically carries
        # the chain's hop count with no other call site needing to know autowatch exists — the same
        # "one shared chokepoint, not N call-site edits" reasoning this module already applies to
        # `run_id`. An explicit `autowatch_hop=` kwarg from the caller always wins; this only fills
        # a gap, never overrides.
        if "autowatch_hop" not in fields:
            env_hop = os.environ.get("SIGMA_AUTOWATCH_HOP")
            if env_hop:
                fields = {**fields, "autowatch_hop": env_hop}
    else:  # EVENTS
        if not journal_on(sdlc_dir, config):
            return None
        if kind not in EVENT_KINDS:
            raise ValueError(f"unknown event kind {kind!r} (expected one of {', '.join(EVENT_KINDS)})")
        if kind == "gate":
            verdict = fields.get("verdict")
            if verdict is not None and verdict not in VERDICTS:
                raise ValueError(f"unknown event verdict {verdict!r} (expected one of {', '.join(VERDICTS)})")
        if kind == "phase":
            state = fields.get("state")
            if state is not None and state not in ("start", "end"):
                raise ValueError(f"unknown phase state {state!r} (expected start or end)")
        _validate_typed_event(kind, fields)
        field_whitelist = EVENT_FIELDS[kind]

    who = actor(config, run)
    path = entry_file(sdlc_dir, who, stream)
    path.parent.mkdir(parents=True, exist_ok=True)
    seq = _line_count(path) + 1

    entry = {
        # F10: the writer instance rides in `id` too, not just the filename — `watch_classify.py`'s
        # cursor tracks a per-WRITER (not per-actor) high-water seq, and needs it to key on. Every
        # existing `id` consumer (`_seq()` here and in watch_classify.py, and the 3 call sites that
        # just print `entry["id"]` verbatim) takes the LAST `:`-segment or the whole string, so a
        # 3-part id is a no-op for all of them — confirmed by reading every "id" reference in the
        # plugin. #540 puts the host INSIDE that middle segment rather than adding a fourth one, so
        # both `_writer()` copies (which take the first two `:`-segments) keep working untouched.
        "id": f"{who}:{_instance_token()}:{seq}",
        "ts": _stamp(now),
        "actor": who,
        "kind": kind,
        "goal": str(goal),
    }
    for name in field_whitelist:
        value = fields.get(name)
        if value not in (None, ""):
            if stream == ENTRIES and name == "to":
                # #1574: THE ADDRESS IS CANONICALISED HERE, ONCE, FOR EVERY WRITER -- the same
                # "cap+scrub lives HERE, never at a call site" rule the #141 comment below states,
                # applied to the one field whose SPELLING decides whether a person is reached
                # rather than merely written about.
                #
                # Measured before this line existed: six sites address a note to a unit's owner
                # (`feature_doc`, `feature_sync`, `feature_propagate`, `unit_completion` and
                # `feature_owner` through their own `_tell`, plus `cross_repo._raise_to`), and FIVE
                # of them wrote the registry's raw `@handle`. Only `feature_owner` normalised, and
                # it did so at its own call site -- which is precisely the "safe by convention, one
                # call site at a time" pattern this loop already exists to end, and which drifted
                # the moment a second writer was added. Normalising at the chokepoint means a
                # SEVENTH writer cannot get it wrong, and no `_tell` has to remember.
                #
                # `address_key`, never a second rule: it is the ONE definition of when two
                # spellings are one person, and `addressed_to` already matches through it on the
                # READ side -- which is what still rescues the raw-`@handle` entries already on
                # disk, and is why this is belt AND braces rather than a replacement for it.
                #
                # AN ADDRESS THAT NORMALISES TO NOTHING IS DROPPED, not written blank. `"@"`, a
                # whitespace string and a non-string all reduce to `""`, and an entry carrying
                # `to: ""` is addressed to "two people nobody can name" -- which `address_key`'s
                # own docstring refuses to treat as anybody. Dropping the field makes the entry
                # honestly unaddressed, which every consumer already handles.
                value = address_key(value)
                if not value:
                    continue
            # #141: cap+scrub lives HERE, once, for every caller — never at a call site. #141
            # scoped it to stream == EVENTS only, on the theory the ENTRIES stream's own `why`
            # (hand-offs/notes a lead reads in TEAM.md) was out of that story's scope. It wasn't
            # safe to leave unscrubbed: `why` is committed byte-for-byte to the shared
            # `sdlc-ledger` branch AND rendered into TEAM.md, so a `handoff.py open ... --why "<secret>"` (a
            # sanctioned command) lands a secret/client string in version control. Same
            # flatten->scrub->cap treatment as EVENTS' free-text fields, gated on the one ENTRIES
            # field that is actually prose — see OPTIONAL_FIELDS: `area`/`to`/`issue`/`priority`/
            # `state`/`pr` are short enums/ids ALWAYS sourced from our own code (operator/CLI-typed
            # or a hard-coded constant in every existing caller), never externally-authored.
            #
            # `ref` is the one exception (#385, POST-REVIEW FIX): comment_watch.py is the first
            # ENTRIES caller to source a field from something outside this plugin's own control at
            # all — a GitHub comment's opaque node id. "Safe by convention" is exactly the pattern
            # EVENT_BOUNDED_ID_FIELDS below was hardened against after two earlier review blocks on
            # the events stream; `ref` gets the identical enforcement here rather than trusting the
            # next ENTRIES caller to also be well-behaved.
            if stream == ENTRIES and name == "why":
                value = _sanitize_free_text(value)
            elif stream == ENTRIES and name in ("ref", "run_id", "autowatch_hop"):
                # `run_id` (#1121) joins `ref` (#385) here for the identical reason: sourced from
                # OUTSIDE this plugin's own code (`SIGMA_RUN_ID` — an outer launcher's value,
                # per `state.run_identity()`'s own docstring), not a hard-coded constant one of our
                # own call sites always controls — see OPTIONAL_FIELDS' own comment above.
                # `autowatch_hop` (#1319) joins them for the same reason — sourced from
                # `SIGMA_AUTOWATCH_HOP`, an outer launcher's env var, not a call-site constant.
                value = _sanitize_free_text(value, cap=BOUNDED_ID_CAP)
            elif stream == EVENTS:
                if name in EVENT_FREE_TEXT_FIELDS.get(kind, ()):
                    value = _sanitize_free_text(value)
                elif kind == "ci_observed" and name == "checks":
                    # #5 (review round 4): `name` inside each check is externally sourced (a
                    # GitHub check-run name), so it gets the same bounded-id scrub every other
                    # externally sourced string in this module gets. `conclusion` is a closed enum
                    # already validated above, so it is left untouched. EVENT_SCHEMA_STRING_CAP
                    # (round 5), not BOUNDED_ID_CAP: the schema allows 256, and a real CI matrix
                    # job name routinely exceeds 120.
                    value = [dict(item, name=_sanitize_free_text(item["name"], cap=EVENT_SCHEMA_STRING_CAP))
                             for item in value]
                elif kind == "merge_observed" and name == "subject":
                    # Round 5: same defect, same fix -- the schema allows 256, and `subject` is a
                    # goal id or a (possibly long) branch name, not a short plan-authored token.
                    value = _sanitize_free_text(value, cap=EVENT_SCHEMA_STRING_CAP)
                elif name in EVENT_BOUNDED_ID_FIELDS.get(kind, ()):
                    # POST-REVIEW FIX: `slice`/`file`/`command_sha256` were "safe by convention"
                    # only — an agent-authored plan `id` (`slice.slice`) landed verbatim with no
                    # length/shape check at all. Same treatment as prose, just a SHORTER cap
                    # (BOUNDED_ID_CAP, not FREE_TEXT_CAP): these are ids/paths, not prose, and a
                    # legitimate one is always short — closes the gap with enforcement, not habit.
                    value = _sanitize_free_text(value, cap=BOUNDED_ID_CAP)
                elif name in EVENT_ENUM_FIELDS.get(kind, ()):
                    # POST-REVIEW FIX (round 3): the enum bucket had NO enforcement here at all.
                    # `gate.verdict` and `phase.state` raise below, and `loop.py`'s `_validate_event`
                    # vocabulary-checks the agent-typed ones — but that is a CLI-only helper, so a
                    # direct `append()` call could put 175 chars of secret-laden prose into
                    # `scan.category` / `slice.mode` / `retro.grade` / `park.reason_class` /
                    # `phase.phase` and have it written raw. No shipped caller does (they all pass
                    # hard-coded constants), which is exactly the "safe by convention, one level
                    # further out" pattern that caused the two earlier blocks. A vocabulary check
                    # here would reverse #136's deliberate decision to leave phase/gate/reason_class
                    # unvalidated, so this is the floor instead: an enum can never carry prose,
                    # whatever a future caller passes.
                    value = _sanitize_free_text(value, cap=BOUNDED_ID_CAP)
                elif name in EVENT_NUMERIC_FIELDS.get(kind, ()) and _looks_numeric(value):
                    # POST-REVIEW FIX (round 3): the predicate normalised the string
                    # (`.replace("_", "")`) but the RAW value was written, so
                    # "1_2_3_4_5_6_7_8_9_0_1_2_3_4_5_6_7_8_9_0" passed a 20-digit check and landed
                    # at 39 characters. Re-stringify through int() so what is stored is what was
                    # checked — which also normalises leading zeros, padding and sign whitespace.
                    # Same shape of bug as the two before it: a predicate applied to one
                    # representation, enforcement applied to another.
                    if not isinstance(value, bool):
                        value = type(value)(int(value)) if isinstance(value, int) else str(int(value))
                elif name in EVENT_NUMERIC_FIELDS.get(kind, ()):
                    # POST-REVIEW FIX (THE LEAK): a declared-numeric field that does not actually
                    # hold a number must never be written raw — this is the reviewer's exact repro
                    # (`tokens_in`/`cycle`/`debt_count` as a secret-bearing string with no literal
                    # newline, sailing through untouched because nothing validated the VALUE, only
                    # the field's presence in a "safe" list). `append()` must NEVER raise for this
                    # — the three deterministic fail-open call sites (a hook's `deny`, an
                    # autonomous park) depend on it — so sanitize (scrub+cap) rather than reject:
                    # it can neither leak the value nor lose the event. `loop.py`'s
                    # `_validate_event` enforces the same rule again, earlier, at the CLI, where a
                    # loud refusal is possible and safe.
                    #
                    # The cap here is NUMERIC_DIGIT_CAP, not FREE_TEXT_CAP, and that is the second
                    # half of the fix. Scrubbing does not touch a digit string, so a base-10-encoded
                    # secret sanitized at the 200-char prose cap still survived — 118 digits is ~49
                    # recoverable bytes. A field declared numeric can never legitimately need more
                    # room than a number, so it does not get prose's allowance.
                    value = _sanitize_free_text(value, cap=NUMERIC_DIGIT_CAP)
                elif name in EVENT_BOOL_FIELDS.get(kind, ()) and not _looks_bool(value):
                    # Same rationale as the numeric branch, for the (currently CLI-unreachable,
                    # but append()-reachable) bool fields `verify.ok` / `verify.absent`. A bool
                    # needs even less room than a number, so it borrows the same tight cap.
                    value = _sanitize_free_text(value, cap=NUMERIC_DIGIT_CAP)
            entry[name] = value
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def safe_append(sdlc_dir, kind, goal, config=None, **fields):
    """The form the loop calls. A ledger problem must never break a run, so everything —
    including reading config.json — is inside the guard."""
    try:
        cfg = config if config is not None else _config(sdlc_dir)
        return append(sdlc_dir, cfg, kind, goal, **fields)
    except Exception as exc:                                    # noqa: BLE001 - fail-open by design
        print(f"ledger: entry skipped (non-fatal): {exc}", file=sys.stderr)
        return None


def _line_count(path):
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def _stamp(now=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now if now is not None else time.time()))


# --------------------------------------------------------------------------- read


def read_all(sdlc_dir, stream=ENTRIES):
    """The team view: the union of every author's file on the given stream, oldest first.
    Reads one stream, `entries` by default. A malformed line is skipped, never fatal — one
    bad append must not blind the whole team.

    Every tolerance rule below is the same rule a downstream reader's ledger parser uses (#101),
    which reimplements this function and documents each guard AND why the obvious narrower catch is
    wrong. #175 proved by execution that this function still died on six inputs the reader survives,
    each crash killing the WHOLE team view rather than costing one line — contradicting the docstring
    above. The guards, mirrored exactly (never narrowed):
      - `utf-8-sig` + `errors="replace"`: a plain-`utf-8` read raises `UnicodeDecodeError` (a
        ValueError, NOT an OSError, so the `except OSError` misses it) on one invalid byte, and a
        BOM on line 1 silently eats that record. `utf-8-sig` strips a leading BOM; `errors="replace"`
        turns an invalid byte into U+FFFD instead of aborting the whole read.
      - `except (ValueError, RecursionError)` around `json.loads`: a deeply nested line raises
        `RecursionError` (a RuntimeError, NOT a ValueError), which `except ValueError` alone lets
        propagate out of `read_all`, discarding every record already read. The tripping depth is
        interpreter-dependent, which is exactly why the TYPE is caught, not a threshold.
      - the sort key is `_sort_key` (below): the raw `(ts, actor, seq)` tuple raises `TypeError`
        the moment two records disagree on `ts`'s type, and `ValueError` on an absurd `id` tail —
        each fatal for the whole sort, not one line.
      - the directory walk is wrapped in `try/except OSError`: `Path.exists()` swallows only
        ENOENT/ENOTDIR/EBADF/ELOOP, so a directory the process cannot stat (mode 000) still escaped
        the old `if not base.exists()` gate. `glob()` already returns [] for a missing directory, so
        the existence check was never needed."""
    out = []
    base = entries_dir(sdlc_dir, stream)
    try:
        paths = sorted(base.glob("*.jsonl"))
    except OSError:
        return out
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if isinstance(item, dict) and item.get("kind"):
                out.append(item)
    out.sort(key=_sort_key)
    return out


def _seq(entry):
    """The tail of `id` after the last ':', 0 if not all-digits. Faithful port of
    a downstream reader's `_seq` (divergence 4): `tail.isdigit()` is NOT enough to make `int()` safe.
    Since 3.9.14/3.10.7/3.11 (the CVE-2020-10735 fix) `int()` refuses an over-long digit string and
    raises ValueError, and a superscript like `"²"` is `isdigit()`-True yet `int()`-invalid on every
    interpreter. Catching beats comparing against the digit limit: the threshold is not a constant
    (any caller can move it with `sys.set_int_max_str_digits`). An absurd id tail degrades this one
    record's `seq` to 0, ordered by its real `ts`, rather than taking out the whole sort."""
    ident = str(entry.get("id", ""))
    tail = ident.rsplit(":", 1)[-1]
    if not tail.isdigit():
        return 0
    try:
        return int(tail)
    except ValueError:
        return 0


def _sort_str(value):
    """Coerce a sort-key field to str so mixed-type `ts`/`actor` values across records can still be
    compared — the same rule as a downstream reader's `_sort_str`. Missing/None/"" all collapse to "" (sorts first),
    matching this function's historical default-first ordering without its cross-type TypeError."""
    return "" if value in (None, "") else str(value)


def _sort_key(entry):
    """The whole `(ts, actor, seq)` key, guarded as one unit — the same rule as a downstream reader's `_sort_key`.
    Every crash this read path has had was a sort key raising on some value nobody enumerated (a
    mixed type, then an absurd id tail); enumerating the next one is a losing game, so an unsortable
    record sorts first instead of destroying the read."""
    try:
        return (_sort_str(entry.get("ts")), _sort_str(entry.get("actor")), _seq(entry))
    except Exception:
        return ("", "", 0)


def team(entries):
    """Everything the whole team should see: anything addressed to someone, plus outcomes."""
    return [e for e in entries if e.get("to") or e.get("kind") in SHARED_KINDS]


def address_key(value):
    """The comparison spelling of a `to` field -- the ONE rule for when two names are one person.

    #1479: `to` is written from two stores whose spellings differ. `actor()` and
    `owners.owner_of()` produce a BARE login (`owners.parse` strips the `@` off every CODEOWNERS
    line); the branching-model registry's `owner` fields are GitHub HANDLES, and the design's own
    example entries write `"owner": "@unit-owner"`. Both reach `append(to=...)`, and every consumer
    of the field -- this module's `addressed_to`/`mine`, `autowatch._find_candidate`,
    `watch_classify`, `triage` -- matched EXACTLY. Measured: a note written `to="@here-owner"` was
    invisible to the person whose login is `here-owner`, so the request looked routed and was not.
    A notification that is written and cannot be received is worse than none.

    #1574: every one of those consumers now compares through THIS function, and `append` writes
    through it as well. That closes the half `addressed_to` alone could not: `watch_classify`
    decides whether a session is WOKEN and does not go through `addressed_to`, so an entry
    `feature_owner` had already normalised to a bare, CASE-FOLDED login (`here-owner`) still failed
    its exact match against a mixed-case GitHub login (`Here-Owner`) -- `actor()` does not fold
    case, and nothing else did either. Normalising one side of a comparison is not a fix; both
    sides go through one rule or neither does.

    Two normalisations, and no more. The leading `@` is dropped, because it is a rendering
    convention and never part of a login. The case is folded, because GitHub logins are
    case-insensitively unique -- the same ruling `files_for` already records for actor filenames,
    and `feature_sync.same_repo` for slugs. Anything that is not a string, or is blank once
    stripped, is "" -- which `addressed_to` then matches against nothing, INCLUDING another "".
    Two people nobody can name are not the same person."""
    if not isinstance(value, str):
        return ""
    got = value.strip()
    if got.startswith("@"):
        got = got[1:].strip()
    return got.casefold()


def addressed_to(entries, who):
    """Every entry addressed to `who`. Matched through `address_key`, NOT by string equality.

    THE NORMALISATION IS ON THE READ SIDE ON PURPOSE, and it is why one change repairs five
    writers. `feature_doc`, `feature_owner`, `feature_propagate`, `feature_sync` and
    `unit_completion` all address their asks with a registry `owner` field, and entries written by
    older versions of every one of them are already on disk in the `@handle` spelling. Fixing this
    at the write side alone would leave those permanently unreachable; fixing it here retrieves
    them, and any future writer that forgets, for free.

    #1574 ADDED THE WRITE SIDE TOO, and did NOT move this. `append` now canonicalises `to` at its
    own chokepoint, so what lands on disk from here on is already the comparison spelling -- but
    every entry written before that is not, and this is the only thing that reaches them. The two
    halves answer different questions: the writer decides what is STORED, this decides who a
    stored value MEANS, and a log that outlives its writers needs the second whatever the first
    does."""
    key = address_key(who)
    return [e for e in entries if key and address_key(e.get("to")) == key]


def handoff_key(entry):
    """What NAMES a hand-off — the issue when there is one, else the goal — for PAIRING/display
    purposes (e.g. backlog_check.py's #532 blocked-by finding reads this for its `ref`). This is
    NOT the settlement-matching key: see `settlement_key()` below for what actually decides whether
    an `ack` answers a given hand-off (#533). A local backlog has no issue numbers but still needs a
    stable label, which is all this function has ever promised."""
    return str(entry.get("issue") or entry.get("goal"))


def settlement_key(entry):
    """The actual pairing identity `outstanding()`/`unanswered()`/`handoff_states()`/`render()`
    match a hand-off against an ack with (#533) — NOT `handoff_key()`, whose own value stays
    unchanged and is still used elsewhere (see its docstring).

    github mode (entry carries an `issue`): `str(issue)` — byte-identical to `handoff_key()`'s own
    value for this case, so every existing exact-string/exact-dict assertion in this codebase over
    issue-keyed entries is unaffected.

    Issue-less (local) mode: `(str(goal), entry.get("area"))` — AREA-QUALIFIED, UNCONDITIONALLY (not
    just when two hand-offs on one goal would otherwise collide): a goal blocked on two areas is two
    independent hand-offs, not one. This tuple is never rendered directly — `render()` prints
    `entry.get("issue")` for the issue column and looks entries up in a dict keyed by this value, so
    the shape only has to be internally consistent, not human-readable. A bare string (github mode)
    and a 2-tuple (issue-less mode) can never collide as dict/set keys, so the two shapes coexist in
    the same settled-set/states-dict without a discriminator tag.

    An ack with NO area (`entry.get("area")` is `None`) matches a hand-off in ANY area on the same
    goal — see `_is_settled()` — deliberately: it is what keeps EVERY ack this kit has ever
    WRITTEN (no ack writer emits `area`) replaying to the identical settlement outcome on old ledger
    history, and keeps the common one-hand-off-per-goal workflow zero-friction.

    RESIDUAL (state this, do not silently narrow the promise further than it is): `(goal, area)`
    does NOT separate two hand-offs from the SAME goal to the SAME area — that shape is still
    ambiguous, narrowed but not eliminated, the same way `handoff_key()`'s bare-goal collapse always
    was before this fix existed at all."""
    issue = entry.get("issue")
    if issue:
        return str(issue)
    return (str(entry.get("goal")), entry.get("area"))


def _no_identity(entry):
    """True iff `entry` carries NEITHER `issue` nor `goal` — meaningful only once
    `settlement_key(entry)` is already known to return a tuple (i.e. `issue` is already falsy),
    since an issue-bearing entry can never be identity-less. Without this guard, `settlement_key()`
    manufactures a `(str(None), area)` key from nothing, which can spuriously match an equally
    identity-less hand-off (#562) — exactly the case `outstanding()` explicitly skipped before
    #533's `settlement_key()` rewrite (`entry.get("issue") or entry.get("goal")` bottoming out to
    `None`, guarded there with `is not None`). Both `_settled_index()` and `_ack_states_index()`
    below call this — the guard lives once, not duplicated per index builder."""
    return entry.get("goal") is None


def _settled_index(entries):
    """O(n), one pass: split TERMINAL (`declined`/`resolved`) acks into (1) `issues`, a flat set of
    github-mode settlement keys (exact-by-issue, no fallback -- plain `in` suffices) and (2)
    `by_goal`, an issue-less index `{goal: set(areas)}` with `None` as the area-less-ack sentinel
    (an ack with no area settles EVERY area on that goal — see `settlement_key()`'s own docstring
    for why). `_is_settled()` below is the ONE place the area-less-fallback rule is evaluated
    against this index, for both `outstanding()` and (indirectly, via `unanswered()`) the answer
    question. (#561: replaces #533's `any(_settlement_matches(key, sk) for sk in settled)` scan —
    O(hand-offs x acks) — with an O(1)-average lookup per hand-off, restoring the O(n) shape the
    pre-#533 single-key set had. #562: `_no_identity()` skips a fully identity-less ack instead of
    indexing it under a manufactured `("None", ...)` key — see that function's own docstring.)"""
    issues, by_goal = set(), {}
    for e in entries:
        if e.get("kind") != "ack" or e.get("state") not in ("declined", "resolved"):
            continue
        key = settlement_key(e)
        if isinstance(key, tuple):
            if _no_identity(e):
                continue
            goal, area = key
            by_goal.setdefault(goal, set()).add(area)
        else:
            issues.add(key)
    return issues, by_goal


def _is_settled(key, issues, by_goal):
    """The area-less-fallback rule, evaluated once against the index `_settled_index()` builds:
    issue mode is a plain set membership test; issue-less mode is settled iff its OWN area was
    acked, or a `None`-keyed (area-less) ack ever covered the whole goal."""
    if isinstance(key, tuple):
        goal, area = key
        areas = by_goal.get(goal)
        return areas is not None and (area in areas or None in areas)
    return key in issues


def _ack_states_index(entries):
    """O(n), one pass over every ack that carries a `state` (not just terminal ones —
    `handoff_states()` shows in-progress states too, unlike the terminal-only index above).
    `issue_states` is a flat {key: state}, naturally last-write-wins since `entries` is already in
    read order. Issue-less acks split further into `area_states` ({(goal, area): (seq, state)}) and
    `wildcard_states` ({goal: (seq, state)}) — an area-less ack updates EVERY area on its goal at
    once, so it cannot be folded into a single per-(goal, area) map at build time (which areas even
    exist is a property of the HAND-OFFS, not the acks); each track instead carries a running `seq`
    so `handoff_states()` below can compare recency between the two tracks at lookup time and keep
    #533's own "latest wins, area-specific or area-less either way" contract exactly. (#561: O(n)
    replacement for the old explicit hand-offs x acks nested loop. #562: `_no_identity()` skips a
    fully identity-less ack — same hole, same guard as `_settled_index()`, see its docstring.)"""
    issue_states, area_states, wildcard_states = {}, {}, {}
    seq = 0
    for e in entries:
        if e.get("kind") != "ack" or not e.get("state"):
            continue
        key = settlement_key(e)
        if isinstance(key, tuple):
            if _no_identity(e):
                continue
            seq += 1
            goal, area = key
            if area is None:
                wildcard_states[goal] = (seq, e["state"])
            else:
                area_states[(goal, area)] = (seq, e["state"])
        else:
            seq += 1
            issue_states[key] = e["state"]
    return issue_states, area_states, wildcard_states


def handoff_states(entries):
    """{settlement_key(hand-off): the latest matching ack's state}, absent where nobody has
    answered. For each hand-off, the LAST (by read order) matching ack's state wins — latest wins,
    regardless of whether that latest match is area-specific or an area-less fallback. An
    issue-less goal with two hand-offs to different areas therefore shows two independent states;
    an area-less ack still fans out to (and can overwrite the state of) every hand-off on its goal,
    exactly like `outstanding()`'s settled index below. O(n) (#561) via `_ack_states_index()`.

    A lead has to tell "nobody has even looked at this" from "someone took it and is working on it".
    Both are outstanding — the blocker is still real until it is `resolved` — but only the first one
    needs chasing, and a bare count cannot say which is which."""
    issue_states, area_states, wildcard_states = _ack_states_index(entries)
    out = {}
    for entry in entries:
        if entry.get("kind") != "handoff":
            continue
        key = settlement_key(entry)
        if isinstance(key, tuple):
            goal, area = key
            candidates = [c for c in (area_states.get((goal, area)), wildcard_states.get(goal))
                          if c is not None]
            if candidates:
                out[key] = max(candidates, key=lambda c: c[0])[1]     # later seq wins
        elif key in issue_states:
            out[key] = issue_states[key]
    return out


def unanswered(entries):
    """Outstanding hand-offs nobody has replied to at all — the ones that are actually stuck."""
    states = handoff_states(entries)
    return [e for e in outstanding(entries) if settlement_key(e) not in states]


def outstanding(entries):
    """Hand-offs nobody has closed out. A hand-off is settled once a terminal ack (`declined` or
    `resolved`) matches it under `settlement_key()`'s area-qualified pairing rule (#533); `deferred`
    deliberately stays outstanding — a promise to look later is not a resolution. O(n) (#561) via
    `_settled_index()`."""
    issues, by_goal = _settled_index(entries)
    return [e for e in entries
            if e.get("kind") == "handoff" and not _is_settled(settlement_key(e), issues, by_goal)]


def counts(entries):
    out = {kind: 0 for kind in KINDS}
    for entry in entries:
        if entry.get("kind") in out:
            out[entry["kind"]] += 1
    return out


# --------------------------------------------------------------------------- claim lease


def _epoch(ts):
    """A ledger UTC stamp back to epoch seconds; -1 if unparseable, so a claim with no readable
    timestamp is treated as ancient (expirable) rather than immortal."""
    try:
        return calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return -1


def pid_alive(pid):
    """True iff `pid` names a live process on THIS machine, RIGHT NOW. Used to tell a live
    sibling process's claim (do not touch — see `open_claims_detailed()`) apart from a dead one
    safe to reclaim, without waiting the full `ledger.lease.ttl_hours` (default 12h) for a crash
    the machine itself could already confirm.

    `os.kill(pid, 0)` sends no signal, only probes: `ProcessLookupError` means genuinely gone (the
    kernel has no such pid) -> False. `PermissionError` means the kernel found a live process at
    that pid owned by someone else -> still True, existence is what this asks, not ownership (in
    practice this process's own prior pids are always the same OS user, so this branch is a
    defensive fallback, not the expected path). Any OTHER exception (a platform where signal 0
    behaves differently, e.g. some Windows paths) fails toward True — "maybe still alive, don't
    reclaim yet" — because the existing TTL fallback is the real backstop for anything this check
    cannot resolve; the risk of guessing False and reclaiming a still-live sibling's goal (Girijesh's
    exact bug) is far worse than the risk of guessing True and waiting out the TTL like today.

    SINGLE-MACHINE ONLY, BY DESIGN — stated scope for this feature (one person, one machine, many
    of their own issues; see #374/sigma-parallel-autoupdate-plan.md). A pid recorded by a
    DIFFERENT machine under the same actor is meaningless to probe here (pids are a per-machine
    namespace: `ProcessLookupError` would wrongly read as "dead" for a claim that is very much
    alive on its own machine). Extending this cross-machine would need a machine-identity
    dimension in the writer string itself, not a change to this function — not attempted here."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:                # noqa: BLE001 - fail toward "assume alive", see docstring
        return True


def _writer(entry):
    """The claim's writer identity: `who:pid` for a post-#337 3-part `id`, or bare `actor` for a
    legacy 2-part id (no pid to distinguish — there was only ever one writer file per actor then).
    Mirrors watch_classify.py's own `_writer()` exactly, kept as an independent copy rather than
    imported — matching this module's existing per-module id-parsing precedent (`_seq()` below is
    ALSO duplicated in watch_classify.py and in a downstream reader, on purpose, so each
    stays free of a cross-module dependency for a five-line pure function)."""
    parts = str(entry.get("id", "")).split(":")
    if len(parts) >= 3:
        return f"{parts[0]}:{parts[1]}"
    return entry.get("actor", "")


def my_writer(config):
    """This process's own writer identity, in the exact shape `_writer()` parses out of a live
    entry's `id` — what `open_claims_detailed()`'s per-claim writer is compared against to tell
    "my own current process" apart from "a different process of mine" (F10/#337 already made every
    entry this process writes carry this identity; this is the read-side counterpart). Computed
    fresh every call, never cached — `os.getpid()` is the one thing riskier to cache than `actor`."""
    return f"{actor(config)}:{_instance_token()}"


def writer_pid(writer):
    """The pid embedded in a `writer` string from `open_claims_detailed()`, or None for a legacy
    bare-actor writer (pre-#337, no pid to extract) or anything else unparseable. A `gh` login can
    never itself contain `:` (GitHub's own username rules), so everything after the last `:` is the
    writer instance, never part of an actor name that happens to look like one. That instance is
    `<host>.<pid>` (#540) or a bare `<pid>` (pre-#540) — both still appear in a ledger mid-upgrade,
    since entries written by an older clone stay in the shared branch forever."""
    _, sep, instance = str(writer).rpartition(":")
    if not sep:
        return None
    host, dot, pid = instance.rpartition(".")
    if dot:
        return int(pid) if host and pid.isdigit() else None
    return int(instance) if instance.isdigit() else None


def writer_host(writer):
    """The host component of a `writer` string, or None for any pre-#540 writer (bare actor, or
    `actor:pid` with no host to extract). `None` means "cannot tell which machine", which every
    caller must treat as the legacy case rather than as a mismatch."""
    _, sep, instance = str(writer).rpartition(":")
    if not sep:
        return None
    host, dot, pid = instance.rpartition(".")
    return host if dot and host and pid.isdigit() else None


def claim_belongs_to_me(holder_actor, holder_writer, me, my_writer, live_worker_check=None):
    """True iff an open claim (`holder_actor`, `holder_writer` — a value from
    `open_claims_detailed()`) is safe for ME (`me`/`my_writer`, from `actor(config)`/
    `my_writer(config)`) to treat as already mine: not another actor's work, and not a DIFFERENT,
    still-live process of my OWN actor (the exact race that let a routine's fresh invocation
    blindly re-attach to another session's in-flight worktree — #374). `_next()` (loop.py) and
    `work.py start()` both call this, so the decision is made exactly once, never reimplemented
    per call site.

    Three cases:
      - a different actor entirely -> False, always (unchanged from pre-#374 behavior).
      - my own CURRENT process's writer -> True, trivially (resuming my own crash/restart).
      - a DIFFERENT writer of the SAME actor: a legacy (pre-#337) claim has no pid to check
        (`writer_pid` is None) and is, degenerately, always "mine" — there was only ever one writer
        file per actor then, so no OTHER writer this claim could belong to -> True, matching
        pre-#374 behavior for the transitional legacy case exactly, no regression. A post-#337,
        pid-bearing writer whose pid is confirmed ALIVE is NOT mine (False) — a genuine live
        sibling, no corroboration needed. A post-#337 writer whose pid is confirmed DEAD is where
        #1197 matters: that pid is ALWAYS the short-lived `loop.py` invocation that wrote the
        claim, not the goal's actual worker — it reads dead within moments of every acquisition,
        whether the goal is genuinely abandoned or being actively worked by a long-running
        subagent (see loop.py's `_goal_has_registered_worker`). So a dead picker pid alone is
        no longer decisive: `live_worker_check`, when given, is consulted — LAZILY, only on this
        one path, never for the three cases above — as the tie-breaker. It returning True means a
        genuine long-lived worker for THIS EXACT goal is still alive by some other, corroborating
        signal (a registered `agent_start` marker FOR THIS GOAL — deliberately not a wider,
        goal-agnostic "is any session managing `.sdlc` at all" signal; a caller that fed one of
        those in here once let an unrelated session's own liveness permanently block reclaiming a
        different, genuinely crashed goal — see loop.py's `_claimed_goal_has_live_worker`
        docstring for the reproduced failure), so the claim is NOT safe to reclaim (False) despite
        the dead picker pid. It returning False, or `live_worker_check` being
        omitted entirely (every pre-#1197 call site, and any caller that has no such signal to
        offer), reclaims exactly as before (True) — purely additive, byte-identical default. The
        existing lease TTL (already applied upstream, inside `open_claims_detailed`'s own
        `ttl_seconds` filtering) remains the backstop for anything neither signal can resolve."""
    if holder_actor != me:
        return False
    if holder_writer == my_writer:
        return True
    # #540: a pid only means something on the machine that issued it. Once the writer carries a
    # host, a claim from a DIFFERENT one is never mine and must never be liveness-checked here —
    # `pid_alive` would be answering about some unrelated local process, and a "dead" answer would
    # hand me a claim another machine is actively working. Same fail-toward-"not mine" posture the
    # docstring above sets out for an unresolvable pid. A writer with no host is pre-#540 and falls
    # through to exactly the old behavior.
    holder_host = writer_host(holder_writer)
    if holder_host is not None and holder_host != _host_token():
        return False
    pid = writer_pid(holder_writer)
    if pid is None:
        return True                          # legacy 2-part claim: degenerately always "mine"
    if pid_alive(pid):
        return False                         # the picker itself is still running -- unambiguously not mine
    # #1197: the picker pid is confirmed dead, but that pid was never the goal's real worker (see
    # docstring) -- give a caller-supplied, genuinely long-lived liveness signal the final say
    # before treating this as safe to reclaim.
    if live_worker_check is not None and live_worker_check():
        return False                         # a real worker for this goal is still alive elsewhere
    return True                              # dead picker, no corroborating live worker -> reclaim


def _held(entries, now=None, ttl_seconds=None):
    """{goal: (actor, writer, ts)} for goals someone has `claimed` and whose lease has not yet
    ended — by a terminal SDLC outcome (done/parked/failed), or (#841) an explicit `release` of a
    claim that was never started, which is NOT a terminal outcome (the goal itself stays exactly
    as pending as it was before it was ever claimed) but ends the lease the same way the other
    three do. The shared computation behind `open_claims()` (actor only, the original,
    still-used-elsewhere contract) and `open_claims_detailed()` (F10.5/#374, adds writer so a
    caller can tell two of the SAME actor's own concurrent processes apart, not just two different
    actors). One computation, two views — never duplicate the state machine itself, or the two
    would silently drift the way F10's review found a downstream reader's independent reimplementation
    already had.

    The latest lifecycle entry per goal wins, so a re-claim after a failure re-opens the lease under
    whoever took it last. With `ttl_seconds` set, a claim older than that is treated as EXPIRED — a
    crashed claimer must never lock a goal for the whole team forever; `now` defaults to wall-clock
    and is injectable for tests. `entries` must be oldest-first (read_all guarantees it).

    POST-REVIEW FIX (PR #1107): a terminal-kind entry only clears `held[goal]` when its OWN actor
    still matches whoever `held[goal]` currently says holds the claim (or nothing is currently
    held — the no-op case `.pop(goal, None)` already covers). Previously this popped by GOAL alone,
    with no check of which actor/claim a terminal entry corresponds to. Reproduced: entries
    `[claimed(A), claimed(B), release(A)]` in that exact order — B legitimately re-claimed the goal
    (A's own claim gone stale/dead), then A's own delayed/stale `release` call physically lands
    AFTER B's claim in the sorted ledger — used to make `open_claims()` return `{}`, silently
    wiping B's live, currently-open claim out of the shared view even though B is actively working
    it; a THIRD actor could then legitimately `next`-pick the same goal B already holds, a genuine
    double-claim hazard. Now A's stale `release` finds `held[goal]`'s actor is B, not A, and leaves
    it untouched. Applied uniformly to done/parked/failed/release, not just release: the pop logic
    is shared code, and a stale done/parked/failed from a dead actor landing after a legitimate
    re-claim is the identical bug (narrower window in practice, since those are normally
    self-issued by whoever is currently finishing their OWN work, but nothing in this codebase
    enforces that — every entries-stream write self-attributes to `actor(config, run)`, the
    CURRENT process's own resolved identity, and no caller can write a done/parked/failed/release
    on a NAMED different actor's behalf, so there is no legitimate cross-actor scenario this could
    break; confirmed by reading every `_record`/`_release` call site in loop.py).

    PR #1235 REVIEW FIX (Finding 1/3): that last claim stopped being true the moment
    `loop.py`'s `_auto_reclaim_stale_claims` (#1198) started calling `_release()` on a DIFFERENT
    actor's stale claim — an automated crash-recovery sweep, by definition, runs as whichever
    actor's process happens to execute it, not as the original (possibly long-dead) claimant. Its
    `release` entry's own `actor` field is still, correctly, self-attributed to the SWEEP's actor
    (an honest audit trail: this records who actually performed the reclaim) — but that means the
    actor-scoped check above, alone, could never end the ORIGINAL claimant's lease: a cross-actor
    reclaim left `held[goal]` unchanged forever, re-detected as "expired" on every subsequent read
    and re-released (with a fresh duplicate GitHub audit comment) on every subsequent call. Fixed
    by a SECOND, narrower path into the SAME pop, scoped to `release` entries alone: one may also
    name, via `reclaimed_actor`, the specific actor whose claim it is ending on that actor's
    behalf (never on a NAMED different actor's behalf for done/parked/failed — a genuinely
    completed/blocked/broken goal is always self-reported by whoever was doing the work, so that
    half of the original claim still holds). This is not a re-opening of the #1107 hazard: the
    check is still against `current[0]` — whoever the LEDGER's own replay currently says holds the
    claim at this point in the ordered history — so a legitimate different actor who re-claimed
    the goal AFTER the stale one (and before this release entry) still leaves `current[0]`
    pointing at THEM, not at `reclaimed_actor`, and the pop correctly does not fire. See
    `test_a_reclaimed_actor_release_does_not_wipe_a_different_actors_live_reclaim`
    (tests/test_ledger.py) for the exact race proven safe.

    #1121 REVIEW FOLLOW-UP (from #841's own review): the actor-scoped guard above is still
    coarser than the real hazard — it cannot tell apart two DIFFERENT, concurrent PROCESSES
    authenticated as the exact SAME nominal actor (e.g. two independent sigma sessions both
    running as one person's own `gh` login). Same shape as the #1107 repro, just same-actor instead of
    cross-actor: `[claimed(A, run=1), claimed(A, run=2), release(A, run=1)]` — A's own SECOND
    process legitimately re-claims the goal (A's first process gone stale/dead), then A's first
    process's own delayed/stale `release` lands after it; actor-only scoping cannot distinguish
    these two `A`s and wipes the live reclaim exactly like the #1107 bug did.

    Fixed by additionally scoping the primary (non-`reclaimed_actor`) branch by `run_id` — #498's
    `state.run_identity()`/`SIGMA_RUN_ID`, the one per-process-tree identity that already
    exists for exactly this class of problem (see `loop.py`'s own `verify_goal` for its other,
    independent use of the same mechanism) — tracked in a side dict (`held_run`) that mirrors
    `held`'s own lifecycle but is NEVER part of THIS function's *returned* shape: `open_claims()`,
    `open_claims_detailed()`, `expired_claims()`, and (outside this plugin) a downstream reader's
    differential tests all unpack `_held()`'s values as a 3-tuple today, so widening what gets
    RETURNED here would be a breaking change to every one of them for a distinction only the
    pop-decision below needs (`_held_full()`, below, exposes `held_run` to the one internal
    caller — `expired_claim_run_ids()` — that legitimately needs it without touching this
    contract). `run_id` degrades to exactly today's actor-only behavior — no regression for a
    caller that never sets `SIGMA_RUN_ID` — whenever EITHER side lacks one: a legacy/
    unattributed claim or terminal entry gives this function nothing to compare, so it falls back
    to the pre-#1121 actor-only answer rather than refusing a legitimate release.
    See `test_two_concurrent_processes_of_the_same_actor_do_not_race_via_a_stale_terminal_entry`
    (tests/test_ledger.py) for the exact race proven safe.

    PR #1269 REVIEW FIX (blocking finding 1): the `reclaimed_actor` branch just above was, until
    this fix, run-scoped NOT AT ALL — reasoned (incorrectly) as: an automated reclaim sweep's own
    `release` entry is, by construction, a DIFFERENT process (and so a different `run_id`) than
    the stale claim it is ending, so comparing `run_id` there would make the path never fire
    again, reopening #1235. That reasoning conflated two different run_ids: the WRITER's own
    (`entry.get("run_id")`, the sweep's — correctly never compared here, or #1235 WOULD reopen)
    and the STALE CLAIM's own (what `reclaimed_actor` already names explicitly for the actor
    axis). `reclaimed_run` closes exactly that gap, the same "name the target, not the writer"
    pattern `reclaimed_actor` itself already uses, one level finer: a release naming
    `reclaimed_actor=amy, reclaimed_run="run-1"` only pops the lease if `held_run[goal]` is STILL
    "run-1" — if amy's own SECOND, concurrent process has since re-claimed the goal under
    "run-2" (a live re-claim landing in the real window `_auto_reclaim_stale_claims` opens
    between reading `expired_claims()`/`expired_claim_run_ids()` and finally writing its release —
    loop.py's own docstring on that function names this window explicitly), `current_run` is
    "run-2", `reclaimed_run` is "run-1", they disagree, and the pop correctly does not fire — the
    identical (actor, run) discipline the primary branch already applies, just sourced from the
    NAMED target instead of the entry's own writer. Degrades to the pre-fix, actor-only answer
    whenever `reclaimed_run` is absent (an older release entry, or a caller that never threaded
    `expired_claim_run_ids()` through) — same no-regression contract every other run_id axis here
    already has. See `test_a_stale_reclaimed_actor_release_does_not_wipe_the_same_actors_live_
    reclaim_by_another_run` and `test_run_id_scoping_never_reopens_the_1235_cross_actor_
    reclaimed_actor_path` (both tests/test_ledger.py) for the two races proven safe together."""
    return _held_full(entries, now, ttl_seconds)[0]


def _held_full(entries, now=None, ttl_seconds=None):
    """`_held()`'s own implementation, plus the `held_run` side-channel it needs for its pop
    decision but has never returned (see `_held()`'s own docstring for why widening its RETURNED
    3-tuple shape would break every existing caller). Returns `(held, held_run)` — `held_run` is
    ONLY consumed by `expired_claim_run_ids()` below, the one legitimate caller that needs to
    expose a goal's CURRENT run_id to code outside this module (`loop.py`'s
    `_auto_reclaim_stale_claims`, to source `reclaimed_run` for its own release — see
    `OPTIONAL_FIELDS`' docstring). Not called `_held()` itself so every existing 3-tuple-unpacking
    caller (`open_claims()`, `open_claims_detailed()`, `expired_claims()`) stays untouched."""
    held = {}                                    # goal -> (actor, writer, ts)  -- THE returned shape
    held_run = {}                                 # goal -> run_id, #1121 side-channel, never returned
    for entry in entries:
        goal = entry.get("goal")
        if not goal:
            continue
        kind = entry.get("kind")
        if kind == "claimed":
            held[goal] = (entry.get("actor"), _writer(entry), entry.get("ts", ""))
            held_run[goal] = entry.get("run_id") or None
        elif kind in ("done", "parked", "failed", "release"):
            # the claimer finished, gave up, or (#841) explicitly released a claim that was never
            # started — `release` was declared in KINDS/SHARED_KINDS from the start for exactly
            # this, but nothing wrote it until loop.py's own `release` verb existed. Without this,
            # a goal one actor released back to the pool stayed unpickable by a DIFFERENT actor
            # (claim_belongs_to_me() is unconditionally False for another actor) until the TTL
            # expired, even though the label/board-status half of the claim was already undone.
            #
            # Actor-scoped (see POST-REVIEW FIX above): only end the lease if it is still THIS
            # entry's actor holding it — a stale/delayed terminal entry from an actor who no
            # longer holds the claim must never clear a DIFFERENT actor's live re-claim. #1121:
            # additionally run_id-scoped (see that section above) — same idea, one level finer,
            # for two concurrent processes sharing one actor. The `reclaimed_actor` branch (PR
            # #1235 review fix, `release` only) is the one sanctioned exception to actor-scoping:
            # an automated reclaim sweep names the ORIGINAL claimant explicitly, sourced from the
            # ledger's own prior `claimed` entry, never from arbitrary input — see
            # `OPTIONAL_FIELDS`' own docstring above. PR #1269 review fix: that branch is now ALSO
            # run-scoped, via `reclaimed_run` — the STALE claim's own run, never the release
            # entry's own `run_id` (comparing against the writer's own run would reopen #1235;
            # see `_held()`'s own docstring for the exact distinction).
            current = held.get(goal)
            entry_run = entry.get("run_id") or None
            current_run = held_run.get(goal)
            run_matches = current_run is None or entry_run is None or current_run == entry_run
            reclaimed_run = entry.get("reclaimed_run") or None
            reclaimed_run_matches = (
                current_run is None or reclaimed_run is None or current_run == reclaimed_run
            )
            if current is None or (current[0] == entry.get("actor") and run_matches) or (
                kind == "release" and current[0] == entry.get("reclaimed_actor")
                and reclaimed_run_matches
            ):
                held.pop(goal, None)             # lease released (or already clear — no-op)
                held_run.pop(goal, None)
    if ttl_seconds:
        cutoff = (now if now is not None else time.time()) - ttl_seconds
        held = {g: v for g, v in held.items() if _epoch(v[2]) >= cutoff}
        held_run = {g: v for g, v in held_run.items() if g in held}
    return held, held_run


def open_claims(entries, now=None, ttl_seconds=None):
    """{goal: actor} for goals someone has `claimed` and not yet released with a terminal outcome
    (done/parked/failed). This is the light lease the loop reads so two people running against one
    board don't start the same goal — the ledger's answer to "who has this right now?". See
    `_held()` for the shared state machine; see `open_claims_detailed()` for the writer-aware
    sibling a caller needs to tell two of ONE actor's own concurrent processes apart."""
    return {goal: actor for goal, (actor, _writer, _ts) in _held(entries, now, ttl_seconds).items()}


def open_claims_detailed(entries, now=None, ttl_seconds=None):
    """{goal: (actor, writer)} — `open_claims()`'s sibling, writer-aware (F10.5/#374). `writer` is
    `actor:pid` for a claim written post-#337, or bare `actor` for a legacy claim — see `_writer()`.
    Exists because a claim held by "my own actor" is not always safe to treat as "mine to resume":
    a DIFFERENT, still-live process of that same actor (two concurrent loops sharing one gh login)
    holding the SAME goal is exactly the race that made a routine blindly resume another session's
    in-flight worktree. `_next()`/`work.py start()` compare against `my_writer(config)`, not just
    `actor(config)`, to tell the three cases apart: my own current process (resume), a different
    but still-alive writer of mine (skip, treat like another actor), or a dead/unreachable one
    (reclaim — the existing TTL fallback above already covers the case liveness can't resolve)."""
    return {goal: (actor, writer) for goal, (actor, writer, _ts) in _held(entries, now, ttl_seconds).items()}


def expired_claims(entries, now=None, ttl_seconds=None):
    """{goal: (actor, writer, ts)} for OPEN claims aged past `ttl_seconds` — the mirror image of
    `open_claims`/`open_claims_detailed`'s own TTL handling: those two DROP an aged-out claim from
    their result (a caller reading "who currently holds this" correctly sees nothing); this
    returns exactly what got dropped, so a caller that needs to know WHICH goal to act on (#1198:
    `loop.py`'s `_auto_reclaim_stale_claims`, strip the goal's GitHub `in_progress_label` and put
    it back in the pool) can tell "expired" from "never claimed" and from "claimed but still
    fresh" — `open_claims()` alone collapses the last two into an identical empty/absent result.

    A claim that already ended terminally (done/parked/failed) or was explicitly `release`d is not
    "expired", full stop — it is simply not held at all, and `_held()` has already removed it from
    contention before the TTL cutoff below ever runs, exactly as it does for `open_claims` itself.

    `ttl_seconds` falsy (config `ledger.lease.ttl_hours` `0`/`false`, "never expire") returns `{}`
    unconditionally, the same "nothing to reclaim" answer `_held()`'s own `if ttl_seconds:` guard
    gives `open_claims` — there is no TTL to have aged past. `now` defaults to wall-clock and is
    injectable for tests, matching every other TTL-aware function in this module."""
    if not ttl_seconds:
        return {}
    held = _held(entries, now=now, ttl_seconds=None)      # every OPEN claim, regardless of age
    cutoff = (now if now is not None else time.time()) - ttl_seconds
    return {goal: v for goal, v in held.items() if _epoch(v[2]) < cutoff}


def expired_claim_run_ids(entries, now=None, ttl_seconds=None):
    """{goal: run_id} for the SAME set `expired_claims()` returns, one call later — PR #1269
    review, blocking finding 1: `loop.py`'s `_auto_reclaim_stale_claims` needs the STALE claim's
    OWN `run_id` (never the sweep's own) to thread through `_release()` as `reclaimed_run`, so
    `ledger._held()`'s `reclaimed_actor` pop branch can tell a truly-expired claim apart from the
    SAME actor's own live re-claim by a different, concurrent process — see `_held()`'s own
    docstring for the exact mechanism and `OPTIONAL_FIELDS`' docstring for why `reclaimed_run` is
    sourced this way, mirroring `reclaimed_actor`'s own `expired_claims()` sourcing exactly.

    A goal whose stale claim never carried a `run_id` (legacy/unattributed, or any caller that
    never set `SIGMA_RUN_ID`) is simply absent here — `_release()` then writes
    `reclaimed_run=None`, and `_held()`'s own `reclaimed_run_matches` degrades to the pre-#1269
    actor-only answer exactly as documented, no regression. `ttl_seconds` falsy returns `{}`
    unconditionally, matching `expired_claims()`'s own no-op there."""
    if not ttl_seconds:
        return {}
    held, held_run = _held_full(entries, now=now, ttl_seconds=None)   # every OPEN claim, any age
    cutoff = (now if now is not None else time.time()) - ttl_seconds
    return {goal: held_run.get(goal) for goal, v in held.items() if _epoch(v[2]) < cutoff}


# --------------------------------------------------------------------------- render


def render(entries, recent=25):
    """The human view a lead reads instead of opening five files. Regenerated, never
    hand-edited — so a merge conflict on it is resolved by re-running, not by hand."""
    lines = [
        "# Team ledger",
        "",
        "_Generated from `.sdlc/ledger/entries/*.jsonl`. Do not hand-edit — regenerate with_",
        "_`ledger.py render <sdlc_dir> --write`._",
        "",
    ]
    open_ones = outstanding(entries)
    states = handoff_states(entries)
    lines += ["## Waiting on someone", ""]
    if open_ones:
        lines += ["| when | from | to | priority | issue | state | what |",
                  "|---|---|---|---|---|---|---|"]
        for entry in open_ones:
            # F19: EVERY interpolated field goes through `_cell()`, not just `why`. `to`/
            # `priority`/`issue` arrive as free text from a sanctioned CLI call
            # (`handoff.py open ... --to "rae | INJECT ## header"`) with no enum to constrain
            # them the way `kind`/an ack's `state` have -- a stray `|` splits the row into extra
            # columns and a stray newline opens a new markdown line inside a file the whole team
            # reads and nobody hand-edits. `actor`/`ts` are construction-safe today (`_safe_name`,
            # `_stamp`) and `state` here is always a validated STATES value, but render() reads
            # entries straight off disk and is the last line of defense before this lands in
            # committed TEAM.md -- it must not depend on every upstream caller staying
            # disciplined forever, so escape uniformly rather than case-by-case.
            lines.append(
                "| {ts} | {actor} | {to} | {priority} | {issue} | {state} | {why} |".format(
                    ts=_cell(entry.get("ts", "")),
                    actor=_cell(entry.get("actor", "")),
                    to=_cell(entry.get("to", "")),
                    priority=_cell(entry.get("priority", "-")),
                    issue=_cell(entry.get("issue", "-")),
                    # `open` = nobody has replied. Shown per row because a count of "outstanding"
                    # cannot distinguish a stuck hand-off from one someone is already working.
                    # settlement_key(), not handoff_key() -- #533: area-qualified for issue-less rows.
                    state=_cell(states.get(settlement_key(entry), "**open — no reply**")),
                    why=_cell(entry.get("why") or entry.get("goal", "")),
                )
            )
    else:
        lines.append("_Nothing is blocked on another person._")
    lines += ["", "## Recent activity", ""]
    shared = team(entries)
    if shared:
        lines += ["| when | who | did | goal | detail |", "|---|---|---|---|---|"]
        for entry in shared[-recent:][::-1]:
            # F19: same reasoning as the table above -- every field through `_cell()`, not just
            # `goal`/`why`.
            lines.append(
                "| {ts} | {actor} | {kind} | {goal} | {detail} |".format(
                    ts=_cell(entry.get("ts", "")),
                    actor=_cell(entry.get("actor", "")),
                    kind=_cell(entry.get("kind", "")),
                    goal=_cell(entry.get("goal", "")),
                    detail=_cell(entry.get("why") or entry.get("state") or ""),
                )
            )
    else:
        lines.append("_No entries yet._")
    return "\n".join(lines) + "\n"


def _cell(text):
    """Keep a free-text value from breaking the markdown table it lands in. Splits on
    `str.splitlines()` rather than replacing a literal `"\\n"` -- a bare `\\r` (or `\\r\\n`/`\\v`/
    `\\f`/the rest of CommonMark's line-terminator set) sailed through a `\\n`-only replace
    untouched and reopened the exact F19/#346 injected-heading symptom (#454), the identical gap
    watch_classify.py's own independent `_cell()` copy had and fixed in #427/#449 -- mirrored here
    verbatim so the two copies do not silently diverge on what they guarantee."""
    return " ".join(str(text).replace("|", "\\|").splitlines()).strip()


# --------------------------------------------------------------------------- CLI

#: #541: every flag that a CLI parsed by `_flags()` below ever hands a real value to -- which is NOT
#: only this module's own `append`/`mine`. `handoff.py`'s open, track and ack verbs are all
#: CONSUMERS of this same parser (handoff.py:329/:346/:395), so handoff's vocabulary belongs here
#: too. Scoping this set to ledger's own names left 7 of handoff's 12 value-taking flags still
#: swallowing a `--`-leading value, and `--title` is the one a human actually sees: a hand-off whose
#: title text began with `--` filed a real GitHub issue literally TITLED "true".
#: The `append()` names come from OPTIONAL_FIELDS itself rather than being re-listed, so those two
#: can never drift; handoff's are named explicitly because they are another module's vocabulary, and
#: an end-to-end test pins the coupling instead (tests/test_handoff.py's `--title` case).
#: Over-inclusion is the SAFE direction: a name here only ever means "consume the next token", and
#: every verb still validates what it actually received.
_VALUE_FLAGS = frozenset(OPTIONAL_FIELDS) | {"actor", "goal", "title", "label", "body-file",
                                             "queue", "assignee", "blocks"}


def _flags(argv):
    """`--name value` / bare `--flag` (-> `"true"`) scanner, plus `--name=value` (unambiguous for
    ANY flag) and unconditional-consume for this module's own known value-taking flags
    (`_VALUE_FLAGS`) -- #541: a value that itself starts with '--' (e.g. `--why "--the CLI is
    missing a --verbose flag"`) used to be silently swallowed: the flag landed on the `"true"`
    sentinel and the value's own text was misparsed as a SECOND, garbage flag. A flag name is never
    legitimately whitespace-bearing -- that shape is always leaked prose from a value the old
    heuristic failed to consume, never a flag a caller meant to pass, so it is dropped instead of
    kept as a nonsense key."""
    out = {}
    i = 0
    while i < len(argv):
        token = argv[i]
        if token.startswith("--"):
            name, eq, value = token[2:].partition("=")
            if eq:                                       # --name=value: always unambiguous
                if " " not in name:                       # same never-a-real-flag rule as below
                    out[name] = value
            elif name in _VALUE_FLAGS:                    # known value-taking flag: consume unconditionally
                if i + 1 < len(argv):
                    out[name] = argv[i + 1]
                    i += 2
                    continue
                out[name] = "true"                        # nothing left to consume
            elif i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                out[name] = argv[i + 1]
                i += 2
                continue
            elif " " not in name:
                out[name] = "true"
            # else: whitespace in `name` -- never a real flag; drop rather than keep a nonsense key
        i += 1
    return out


def report_publish_state(sdlc_dir, config):
    """#1599: after a HAND-TYPED write, say on stderr whether the team can actually see it.

    LAZY-LOADED, and it has to be. `sync.py` imports THIS module at its own top level, so a
    module-level import here would be circular; loading it inside the one function that needs it is
    the identical shape `loop.py`'s `_ensure_watcher` already uses for the same module and the same
    reason. The duplicate `ledger` instance that creates is harmless — every identity this module
    mints (`_host_token`, `_instance_token`, `actor` from config) is a pure function of the process
    and the config, so both instances agree on which file is being written.

    CALLED ONLY FROM `main()`, never from `append()`. `safe_append()` is invoked from all over
    `loop.py` — a claim, an outcome, a park, a journal event — so publishing inside the writer
    would turn one autonomous run into a push per entry. The interactive CLI verb is the boundary
    this belongs on, because that is the boundary a PERSON is standing at.

    Fail-open to silence: a `sync.py` that will not even load must not turn a successful write into
    an error."""
    try:
        spec = importlib.util.spec_from_file_location("sync", pathlib.Path(__file__).resolve().parent / "sync.py")
        sync = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sync)
        said = sync.publish_after_write(sdlc_dir, config)
    except Exception:                   # noqa: BLE001 - the entry is already written; never raise here
        return
    if said:
        print(said, file=sys.stderr)


USAGE = ("usage: ledger.py append <dir> <kind> <goal> [--to X --issue N --priority P "
         "--why TEXT --state S --area A --ref R] | render <dir> [--write] | "
         "mine <dir> [--actor X] | summary <dir>")


def main(argv):
    if argv[1:] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    if len(argv) >= 5 and argv[1] == "append":
        sdlc_dir, kind, goal = argv[2], argv[3], argv[4]
        flags = _flags(argv[5:])
        if flags.get("issue", "").isdigit():
            flags["issue"] = int(flags["issue"])
        config = _config(sdlc_dir)
        try:
            entry = append(sdlc_dir, config, kind, goal, **flags)
        except ValueError as exc:
            print(f"ledger: {exc}", file=sys.stderr)
            return 2
        print(entry["id"] if entry else "OFF (config: \"ledger\": {\"enabled\": true})")
        report_publish_state(sdlc_dir, config)
        return 0
    if len(argv) >= 3 and argv[1] == "render":
        sdlc_dir = argv[2]
        text = render(read_all(sdlc_dir))
        if "write" in _flags(argv[3:]):
            target = ledger_dir(sdlc_dir) / "TEAM.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            print(f"wrote {target}")
        else:
            print(text, end="")
        return 0
    if len(argv) >= 3 and argv[1] == "mine":
        sdlc_dir = argv[2]
        flags = _flags(argv[3:])
        who = flags.get("actor") or actor(_config(sdlc_dir))
        mine = addressed_to(read_all(sdlc_dir), who)
        for entry in mine:
            print(f"{entry.get('ts', '')} {entry.get('kind', '')} from {entry.get('actor', '')} "
                  f"issue={entry.get('issue', '-')} priority={entry.get('priority', '-')} "
                  f"{entry.get('why', '')}".rstrip())
        if not mine:
            print(f"nothing addressed to {who}")
        return 0
    if len(argv) >= 3 and argv[1] == "summary":
        entries = read_all(argv[2])
        tally = counts(entries)
        still_open, no_reply = outstanding(entries), unanswered(entries)
        print(f"ledger: {len(entries)} entries | "
              + ", ".join(f"{k} {v}" for k, v in tally.items() if v)
              + f" | outstanding hand-offs: {len(still_open)}"
              + (f" ({len(no_reply)} with NO reply)" if no_reply else " (all answered)"
                 if still_open else ""))
        return 0
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
