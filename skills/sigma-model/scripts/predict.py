#!/usr/bin/env python3
"""Model auto-selection — predict the portable tier a goal deserves, so the loop runs its phases at
a capability matched to the work instead of one-size-fits-all. Deterministic regex over the goal
text (like the intent hook), so it's zero-dep, hermetically testable, and never drifts. Returns one
of the shared tier labels: haiku | sonnet | opus | fable; ``host-model`` resolves a Codex tier to a
real model ID and reasoning effort before dispatch.

Predict ONCE from the goal; the loop then runs that goal's phases at the returned tier (the design:
"the rest of the steps will be executed with that model"). Conflicts resolve UPWARD to the more
capable tier — over-powering a mislabelled goal is cheaper than under-powering a hard one.

THAT PREMISE IS FALSE AT EXACTLY ONE TIER, and #2564 is the bill for it: `fable` is priced ABOVE
opus ($10/$50 per 1M vs $5/$25), so promoting a goal to it is not cheap over-powering, it is the
most expensive answer available. One creative stem anywhere in title+body was enough, and an
unattended overnight run exhausted an account's credits. A PRICE CEILING therefore sits over the
whole router (`model_selection_max_tier`, default `opus`, so fable is unreachable unless a repo
opts in) — see `_TIER_PRICE_ORDER` and `predict_with_reason`'s own `max_tier`. Upward resolution is
unchanged BELOW the ceiling; the ceiling is what makes it safe to keep. This can
escalate a mundane phrasing hard: "fix the 401 unauthorized response formatting" reaches opus/high
on the `authoriz` signal alone, well past what "response formatting" alone would suggest — accepted,
not a bug (#595): a goal mentioning authorization for ANY reason is treated as authorization-
adjacent, never assumed benign, and `_PATTERNS`' high-to-low ordering means that signal always wins
ties within one string.

Two axes, one gate (`model_selection: "auto"`): the MODEL tier (which brain) and the reasoning
EFFORT (how hard it thinks: low|medium|high). Resolution is available at two granularities —
per GOAL (`resolve`, the ceiling for the goal's phases) and per STEP (`resolve-step`, so a
mechanical step inside a hard goal — run the tests, watch a job, lint — can run on a cheaper
tier/effort than the goal's ceiling). Step conflicts resolve upward within the step text.

RECORDING THE CHOICE (issue #1030). `resolve`/`resolve_step` don't only predict — given a `goal`
identifier, each also shells out to `loop.py emit ... model_choice --model <tier> --signal
<signal>` before returning, so the tier reaches the team ledger unconditionally rather than
depending on a calling skill's SKILL.md remembering a second instruction (see `_emit_model_choice`
for the full reasoning, and why this is the one place in this module that is NOT zero-dep/pure —
`predict`/`predict_with_reason`/`predict_effort` remain exactly that). `why` (below) is the
deliberate exception: still pure, still writes nothing, on purpose.

A HAIKU SIGNAL COUNTS ONLY IN THE TITLE (#2827). The upward-resolution premise has a mirror image
one tier down: a real issue body almost always quotes a code comment, names a docstring or cites a
lint, so scanning the body for haiku stems routed non-trivial goals DOWN to the cheapest tier on one
body word (a P1 multi-module fix ran at haiku on `comment` alone; the predecessor's census of its own
two boards found 24 open goals routed that way -- not measured on Sigma's). Under-powering is the
costly error, so haiku is the one tier read from the TITLE only; opus/fable stems keep scanning
title+body. The title is the text's FIRST LINE -- the shape `loop.py` builds at pick time
(`"<title>\n\n<body>"`) and the one fallback gesture the skills document prints -- so a single-line
string is all title and routes exactly as before. A goal FILE path is read by `_read_goal`, which
puts the file's own title on that first line. `resolve_step` keeps whole-text haiku scanning: a
mechanical step's downgrade is what that verb is for.

A BODY-ONLY OPUS/FABLE SIGNAL NEEDS TWO OR MORE HITS, OR A TITLE HIT (#390). Upward resolution has
the opposite failure mode one tier UP: the predecessor's census of its own two boards (its
measurement, not Sigma's -- Sigma's boards were not measured) found 61 of 167 open goals routed to
opus, 58 of those 61 from a BODY word, and a hand-read of 21 found 16 false positives -- a doc-path
citation, a test path, a `feature/...` branch literal, "authorization" meaning spend approval, a JSON
key named `complexity`, `migrat` in a retrospective mention of a migration that already shipped. Each
of those shares one root cause: the signal sits inside something that is not a description of the
work, and appears exactly ONCE. A real migration or concurrency issue, by contrast, carries its
signal as the subject of the issue -- repeated, not incidental. So a body-only opus/fable match now
needs TWO OR MORE hits (distinct match OCCURRENCES, via `finditer`, not distinct literal text --
"migrat" repeated as migrate/migration/migrating is three hits of the identical stem, and REQUIRING
different literal text would misroute exactly the true-positive case this exists to protect); a TITLE
hit still wins outright on ONE occurrence, mirroring #2827's own title-always-wins shape. A match
lying entirely inside a backtick code span or a path-shaped token (non-whitespace, a slash,
non-whitespace -- see `_excluded_spans`/`_within_excluded_span`) never counts at all, in title or
body, because a citation is not a description of the work either. `why` now also reports the hit
count.

PER-REPO SIGNAL EXCLUDES (issue #1601). A signal can be a creative term on most repos and an
ordinary DOMAIN NOUN on a few: a storytelling pipeline has a "narrative stage" and a "prose
renderer", and `vision` collides with Sigma's OWN vision-first vocabulary (`/sigma-vision`, the
north-star doc), so "align the retry logic with the north-star vision doc" reached `fable`. #350
fixed the same class by NARROWING the global pattern (bare `story` dropped, `storytell` kept); that
remedy does not generalize, because those terms are genuinely creative elsewhere -- no one global
pattern can be right for every repo, which leaves the PER-REPO axis as the only correct one.
`model_selection_signal_excludes` in `.sdlc/config.json` therefore lets a repo neutralize named
signals locally; see `_is_excluded` for what an entry keys on and `predict_with_reason` for the
fall-through. Ships empty, so a repo that configures nothing is byte-identical (pinned by
test_no_config_is_byte_identical_to_the_pre_1601_router, over the whole signal vocabulary).

Worth fixing but NOT worth redesigning, because the harm is asymmetric. In interactive `/sigma-goal`
the prediction is ADVISORY -- the phases run inline on the session model and the tier is only
surfaced, so a wrong answer costs a misleading line. In `/sigma-loop` under `model_selection: "auto"`
it is REAL: the resolved tier is the goal's ceiling and each phase is dispatched as a subagent AT
that tier, so a false `fable` genuinely runs implementation and review on the creative tier. A
bounded per-repo opt-out buys back the real half; the LLM classifier named below stays the answer to
the general problem, and is deliberately NOT what this is.

ponytail: heuristic with a known ceiling. Upgrade path = swap `predict` for an LLM classifier behind
the same signature if the keyword rubric ever proves too coarse; the config flag + tests stay put."""
import re, sys, pathlib, subprocess, os


def _model_selection(cfg):
    """Codex-only opt-in; the historical Claude gate remains the default."""
    env = os.environ
    codex = (env.get("SIGMA_HOST") == "codex" or
             (env.get("SIGMA_HOST") != "claude" and
              (env.get("CODEX_THREAD_ID") or env.get("CODEX_SESSION_ID")) and
              not (env.get("CLAUDECODE") or env.get("CLAUDE_CODE_SESSION_ID"))))
    overrides = cfg.get("model_selection_host_overrides")
    if codex and isinstance(overrides, dict) and overrides.get("codex") in ("auto", "off"):
        return overrides["codex"]
    return cfg.get("model_selection") or "off"

# Ordered high -> low; first tier whose signal appears wins (so hard beats trivial on a mixed goal).
_PATTERNS = [
    # `\b` anchors the whole alternation, so every bare term here is blind to a PREFIXED form: there
    # is no word boundary between the "n" and the "s" of "insecure", nor between "re" and "architect"
    # in "rearchitect". Naming the DEFECT rather than the property is how these goals are usually
    # filed, so the blind spot hit the common case — and hyphenating restored the boundary, making
    # "Re-architect" and "Rearchitect" route differently for no reason a reader could predict.
    # #543 fixed `secur`; #575 finished the sweep.
    #
    # The prefix is admitted PER TERM, never globally, because `un-` does not mean one thing:
    #   * on a security term it names a defect to FIX — "unsecured"/"unauthorized" is harder work,
    #     so opus is right;
    #   * on a capability term it can name the mere ABSENCE of the property — "uncomplex" is not a
    #     complexity goal at all, so `complex` deliberately keeps its bare boundary.
    # ("unscalable" reads the first way, not the second: it means the system does not scale.)
    # Each widening was dictionary-swept (/usr/share/dict/words) for what it newly admits; every hit
    # was a negation or repetition of the intended term, no unrelated word. Kept in lockstep with the
    # effort list below for the SECURITY cluster — see that list's own policy note.
    # Non-capturing groups because these patterns are only ever truth-tested, never read for groups.
    #
    # #595 added two more, same method: `authoriz` -> `authori[sz]` for the British -ise spelling
    # (authorised/authorisation) — the dictionary has no -ise form of this word at all, so the sweep
    # instead confirms no UNRELATED word is newly admitted (swept both bare and `un`-prefixed, since
    # the live pattern is `(?:un)?authori[sz]`: three obscure hits, "authorish"/"authorism"/
    # "unauthorish", all plainly on-topic, none a real risk of a false trigger). `migrat` ->
    # `(?:re|un)?migrat` to also admit `un-` ("unmigrated" — not yet migrated, work still needed —
    # the same defect-to-fix shape the security cluster's `un-` has, not the absence-of-property
    # shape `complex` deliberately keeps bare): swept, the only newly-admitted form is
    # "unmigrating"; every other `[prefix]migrat` dictionary word (emigrate/immigrate/transmigrate/
    # nonemigration/...) uses a DIFFERENT prefix and stays correctly excluded.
    #
    # #1627 added a THIRD, differently-shaped widening: `auth[nz]?\b` admits the bare abbreviation
    # itself (auth/authn/authz) rather than a prefix of an existing full word — `authenticat`/
    # `authori[sz]` above never fire on "the auth token refresh path" because neither "authenticat"
    # nor "authori" appears in it at all. This term is the ONLY one in either list carrying its own
    # TRAILING `\b`, scoped to just this one alternative (every sibling term keeps the existing
    # no-trailing-boundary shape) — without it, bare `auth` would match as a PREFIX inside
    # author/authoring/authored/authoritative/authigenic, none of them security-adjacent. Swept
    # against /usr/share/dict/words: `\bauth[nz]?\b` admits ZERO real words (neither "auth" nor
    # "authn"/"authz" is itself a dictionary entry), the narrowest possible widening for this case.
    ("opus",  r"\b((?:re|un)?migrat|(?:re)?architect|redesign|(?:in|un)?secur|(?:un)?authenticat|"
              r"(?:un)?authori[sz]|auth[nz]?\b|crypto|concurren|distribut|race condition|"
              r"performance|breaking change|complex|(?:un)?scalab|scaling|multi-service|"
              r"data loss|backward compat|threat model|financial|payment)"),
    # #1780: bare `prose` collided with this repo's OWN SDLC term of art — "prose-invoked"/
    # "prose-gated"/"prose-dependent"/"prose-only"/"prose-driven" (and more; the corpus of
    # hyphenated `prose-*` compounds already in this tree's own skills/tests keeps growing) all mean
    # "driven by an agent reading a SKILL.md, not by code", nothing about creative writing. #1601's
    # `model_selection_signal_excludes` is the general per-repo answer to a domain collision like
    # this, but this repo's own `.sdlc/config.json` is gitignored runtime state (never a committable
    # PR diff), so a global code narrowing is what actually ships here — the same shape #350 used to
    # drop bare `story` while keeping `storytell`. Enumerating each observed suffix
    # (-invoked/-gated/-dependent/-only/-driven/-inclusive/-shaped/...) would recreate the exact bug
    # for the next compound nobody enumerated yet; `prose(?!-)` instead excludes the whole SHAPE —
    # "prose" immediately followed by a hyphen — which covers every existing compound and every
    # future one without a per-suffix list. Swept against /usr/share/dict/words: no dictionary word
    # contains a hyphen at all, so this can only ever exclude a HYPHENATED compound, never a bare
    # dictionary word — "prose" on its own (a genuinely creative "purple prose") still fires exactly
    # as before, and so does "prosecution" (#1625's own separate, still-open collision: no hyphen
    # follows "prose" there either, so this fix does not touch it — different shape, different
    # issue). Accepted, not risk-free: a genuinely creative but hyphenated "prose-poem" on another
    # repo would fall through this one alternative — but `storytell`/`marketing copy`/`tagline`/
    # `creative writing` remain live in the same tier, so a real creative-writing repo still reaches
    # fable through them.
    ("fable", r"\b(vision|narrativ|storytell|blog|marketing copy|prose(?!-)|tagline|"
              r"creative writing)"),
    # #2446: `comment(?:s|ed|ing)?\b` scopes a TRAILING boundary to just this one alternative
    # (every sibling term here keeps the existing no-trailing-boundary shape), the same shape as
    # `auth[nz]?\b` above (#1627) -- one alternative, its own trailing `\b`, nothing else touched.
    # Without it, bare `comment` matches as a PREFIX inside any longer identifier: it fired inside
    # the filename `comment_watch.py` (goal #2443's own issue text), silently downgrading a real
    # goal to the cheapest tier. The explicit inflection list keeps "comment"/"comments"/
    # "commented"/"commenting" all still matching byte-identically; "commentary"/"commentator"/
    # "commenter" (ordinary English words, swept against /usr/share/dict/words) no longer do.
    ("haiku", r"\b(typo|renam|whitespace|reformat|formatting|lint|docstring|changelog|spelling|"
              r"indentation|dead code|comment(?:s|ed|ing)?\b)"),
]
_DEFAULT = "sonnet"

#: PRICE order, low to high, from skills/sigma-loop/rates/anthropic_list_prices.csv ($1/$2/$4/$10 per 1M in,
#: for the ids in _CLAUDE_TIER_MODELS).
#: It coincides with CAPABILITY order for the first three and diverges at `fable`, and that one
#: divergence is issue #2564 in full: `_PATTERNS` resolves conflicts upward on capability because
#: "over-powering a mislabelled goal is cheaper than under-powering a hard one" (module docstring),
#: which is true for opus at 2x sonnet and false for fable at 5x. `_PATTERNS`' own ordering is
#: therefore NOT this list and must not be derived from it.
_TIER_PRICE_ORDER = ("haiku", "sonnet", "opus", "fable")
#: Pricing-coverage reference ONLY (#2735, Q1-G6): the concrete `claude-*` id each tier alias
#: resolved to in Claude Code transcripts on this host, as OBSERVED on 2026-09-27 -- not what
#: dispatch sends. `predict`/`resolve`/`resolve-step` still return the bare tier labels above; the
#: Claude Code host resolves `opus`/`sonnet` on its own side, and nothing in skills/ reads this map
#: at runtime. tests/test_phase_report.py enforces that every value here has all five token kinds
#: effective now in the core rate card, so a lapsed or missing row fails a test instead of printing
#: `cost: unavailable`. Update it when an alias moves; the test cannot observe the host.
_CLAUDE_TIER_MODELS = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-5",
    "opus": "claude-opus-5-5",
    "fable": "claude-fable-5-1",
}
#: The default price ceiling. `opus`, not `fable`: the incident in #2564 was an unattended overnight
#: run on stock config that exhausted an account's credits after one creative stem in a goal's body
#: promoted it to the 5x tier, so a ceiling that had to be switched on would not have prevented it.
_MAX_TIER_DEFAULT = "opus"
#: `.sdlc/config.json` key holding this repo's ceiling (#2564). A FLAT sibling of `model_selection`
#: for the same reason `_SIGNAL_EXCLUDES_KEY` is: that key is a bare string, and nesting it would
#: break every existing config.
_MAX_TIER_KEY = "model_selection_max_tier"

#: `.sdlc/config.json` key holding this repo's excluded signals (issue #1601). A FLAT sibling of
#: `model_selection` rather than a nested field, because that key is a bare string ("off"/"auto")
#: and turning it into an object would break every existing config; the file's own `_<name>` comment
#: convention documents it in the same place (`_model_selection_options` already does).
_SIGNAL_EXCLUDES_KEY = "model_selection_signal_excludes"


def _cfg(sdlc_dir):
    """`.sdlc/config.json` as a mapping, or `{}` for anything unreadable. Best-effort by contract:
    every caller here is on the path of a goal the loop already picked, so a missing dir or a
    hand-edited config with a stray comma must degrade to "unconfigured", never raise."""
    import json
    try:
        cfg = json.loads((pathlib.Path(sdlc_dir) / "config.json").read_text())
    except Exception:
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _host_model_cfg(sdlc_dir):
    """Read dispatch configuration strictly: a missing file is unconfigured, but a malformed one
    must refuse before a host call can silently use a stale default mapping."""
    import json
    path = pathlib.Path(sdlc_dir) / "config.json"
    try:
        cfg = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid host-model config ({exc})") from None
    if not isinstance(cfg, dict):
        raise ValueError("invalid host-model config (expected an object)")
    return cfg


def signal_excludes(cfg):
    """The normalized excludes tuple from a parsed config mapping. `()` for anything malformed.

    Fails SAFE in one direction only: junk yields no exclusions, so a config typo leaves the router
    exactly as it was rather than silently neutralizing a signal (or raising into `resolve`, which
    runs for every goal the loop picks). Entries are lowercased and stripped, and BLANK ENTRIES ARE
    DROPPED -- `"" in anything` is True, so a trailing comma or a blank line in a hand-edited list
    would otherwise exclude every signal and flatten the whole router to `_DEFAULT`. A bare string
    is honoured as a one-entry list (the obvious mis-write of `["vision"]`); doing nothing at all
    there is the worst outcome for a knob whose failure mode is already invisible."""
    try:
        raw = cfg.get(_SIGNAL_EXCLUDES_KEY)
    except AttributeError:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(e.strip().lower() for e in raw if isinstance(e, str) and e.strip())


def max_tier(cfg):
    """The normalized price ceiling from a parsed config mapping (#2564), else `_MAX_TIER_DEFAULT`.

    Fails SAFE in one direction only, exactly as `signal_excludes` does: junk leaves the DEFAULT
    ceiling in place rather than uncapping the router or raising into `resolve`, which runs for
    every goal the loop picks. A cap is a single value, so — unlike `signal_excludes` — a bare LIST
    is junk rather than a charitable one-entry read: `["fable"]` names no tier.

    Stripped and lowercased, so a hand-edited `"FABLE"` or `" haiku "` works; anything that is not
    one of `_TIER_PRICE_ORDER`'s four literals is a typo, and a typo must not silently uncap."""
    try:
        raw = cfg.get(_MAX_TIER_KEY)
    except AttributeError:
        return _MAX_TIER_DEFAULT
    if not isinstance(raw, str):
        return _MAX_TIER_DEFAULT
    tier = raw.strip().lower()
    return tier if tier in _TIER_PRICE_ORDER else _MAX_TIER_DEFAULT


def _price_rank(tier):
    """`tier`'s index in `_TIER_PRICE_ORDER`. Raises `ValueError` on an unknown tier, deliberately:
    the config path is already normalized by `max_tier`, so the only way to reach this with junk is
    a direct Python caller passing it — and, per `_is_excluded`'s own stated policy for the same
    situation, such a caller should hear about it rather than be silently defaulted."""
    return _TIER_PRICE_ORDER.index(tier)


def _is_excluded(signal, excludes):
    """Does a configured entry name this SIGNAL?

    WHAT AN ENTRY KEYS ON, and why (#1601). An entry names the router's signal -- the alternation
    branch -- not a word in the goal text, and not a phrase. The three candidates, and what each
    gets wrong:
      * a PHRASE ("north-star vision") under-blocks: only that wording is suppressed, and the next
        goal that phrases it differently misroutes again;
      * a TIER (a "fable off" switch) over-blocks: a genuinely creative goal on a narrative-heavy
        repo could then never reach `fable` at all;
        (#2564 added a tier-granularity knob anyway — `model_selection_max_tier` — and this
        paragraph is NOT a ruling against it. The objection here is to using a tier switch to solve
        a DOMAIN VOCABULARY problem, where a signal is the right granularity. That other knob
        answers a different question, PRICE, and the over-block named here is precisely its intent:
        it is reversed by one config line on the repos that want fable. Both knobs are live, both
        are honoured, and they compose without interacting — the ceiling reads only the finished
        scan's result.)
      * a SIGNAL neutralizes one branch wherever it fires, leaving the tier's OTHER branches live --
        so that same repo still reaches `fable` via `storytell`/`blog`/`marketing copy`/`tagline`.
    That last one is the granularity the excluded repo actually wants, so it is what this keys on.

    Comparison is case-insensitive containment in BOTH directions, because the string a user can
    write and the literal the router matched are rarely the same one:
      * entry contained in signal -- one stem covers every prefixed form a pattern admits (`secur`
        covers the `insecur` that `(?:in|un)?secur` actually matches), instead of making the repo
        enumerate `authoriz`/`unauthoriz`/`authoris`/`unauthoris` by hand;
      * signal contained in entry -- a repo can write the natural word ("narrative") without having
        to know the router's internal stem ("narrativ", which is what `why` prints).
    The accepted cost is the same in every direction: an excluded signal is excluded for the whole
    repo, including on a goal where it WAS meant creatively. That is the deliberate trade -- it is
    opt-in, per repo, and declared by the person who knows their own vocabulary.

    Entries are stripped and lowercased HERE as well as in `signal_excludes`, which is not a
    duplicated guard but two different jobs at two different doors: `signal_excludes` parses the
    config SHAPE (list-vs-string, non-string junk) and can only protect the config path, while this
    normalizes the COMPARISON and so protects every caller, including a direct Python one that built
    the list itself. A blank entry is the reason the second door matters at all: `"" in anything` is
    True, so one empty string would exclude every signal on every goal and silently flatten the
    router to `_DEFAULT` -- the loudest possible failure, from the quietest possible typo.

    `signal` is always a real match literal and never empty (no `_PATTERNS` branch can match the
    empty string), so there is deliberately no guard for that case -- a guard the one call site
    cannot reach is dead code, and dead code that looks like a safety check is worse than none.
    A non-string ENTRY is likewise not defended here: the config path is already type-filtered by
    `signal_excludes`, and a direct Python caller passing junk should hear about it."""
    s = (signal or "").lower()
    for entry in excludes:
        e = entry.strip().lower()
        if e and (e in s or s in e):
            return True
    return False

# Effort is its own axis: a hard-model goal can still contain low-effort steps. First match wins.
#
# POLICY (#575) — this list is a deliberate SUBSET of the model list above, not a stale copy of it,
# because the two axes answer different questions: the MODEL tier asks how much capability the
# domain demands (stakes, blast radius if it goes wrong), the EFFORT tier asks how much reasoning
# the work itself demands. They diverge in BOTH directions, on purpose:
#   * `debug` / `root cause` / `diagnos` are high effort but NOT opus — following a fault through a
#     system is deep reasoning, not specialist capability. (Proof this list was never just a copy.)
#   * `financial` / `payment` / `multi-service` / `complex` / `scalab` / `scaling` / `performance`
#     are opus but NOT automatically high effort — they raise the cost of being wrong without
#     making the individual change harder to reason about. A one-line price-rounding fix is still
#     one line; it just deserves the better model. `complex` names the SYSTEM's property, not the
#     change's own difficulty, which is why it belongs here rather than being self-contradictory:
#     when a complex-system change genuinely IS hard reasoning too, the effort axis catches that
#     independently (e.g. "debug the complex scheduling logic" reaches high effort via `debug`).
# This split is at its most visible on one pair (#595, decided, not changed): "diagnose the latency
# regression" is sonnet/high (`diagnos` names the deep-reasoning verb, no opus-list term appears);
# "fix the performance regression" is opus/medium (`performance` names the high-stakes domain, not
# an effort-list term) — the SAME underlying problem, phrased two ways, at opposite ends of BOTH
# axes. Not a bug: each phrasing correctly triggers a different word on a different axis. Moving the
# performance/complex/scaling cluster to smooth over one contrived pair would touch load-bearing,
# already-deliberate categorization on no new evidence it is wrong for real goals — documenting the
# pair here is the smaller, honest fix.
# The SECURITY cluster is the exception, and is kept aligned across both lists: `(?:in|un)?secur`,
# `(?:un)?authoriz`, `(?:un)?authenticat` and `crypto` all name ONE domain. #543 established that a
# security goal reaching opus at medium effort is two lists disagreeing, not a distinction being
# drawn — and leaving `secur`'s three siblings out of this list was that same accident, one term
# wider ("Fix the authorization bug" came back opus/medium). Pinned by test, both directions.
_EFFORT_PATTERNS = [
    # #1627: `auth[nz]?\b` joins `authenticat`/`authori[sz]` here too, same reasoning as _PATTERNS'
    # own comment above this list's sibling — the security cluster is kept aligned across BOTH
    # lists by policy (this list's own module comment, just above), and a bare "auth"/"authn"/
    # "authz" goal is exactly as much high-effort reasoning as the full-word forms already are.
    ("high", r"\b((?:re|un)?migrat|(?:re)?architect|redesign|(?:in|un)?secur|(?:un)?authori[sz]|"
             r"(?:un)?authenticat|auth[nz]?\b|crypto|threat model|concurren|race condition|"
             r"distribut|breaking change|data loss|backward compat|debug|root.?cause|diagnos)"),
    # #1627: `run (the )?tests?` required "the" to sit IMMEDIATELY before "test(s)", so "run THE
    # FULL test suite" fell through to the medium default -- effort measures REASONING difficulty,
    # not scope (this list's own module comment above: a one-line payment fix is opus but still
    # only medium effort), and running the full suite takes no more reasoning than running a
    # subset. `(full )?` is the narrowest widening that admits exactly the issue's own named
    # example: it sits only between the optional "the" and "test(s)", so "run a full audit of the
    # test coverage" and "the full test suite passed overnight" (full elsewhere in the sentence,
    # not immediately before test(s)) correctly stay at the medium default -- pinned by test.
    # #2446: mirrors `_PATTERNS`' own `comment(?:s|ed|ing)?\b` fix directly above -- see that
    # comment for the full reasoning. Kept in lockstep because `comment` is one of the terms this
    # list shares verbatim with `_PATTERNS` today, the same alignment policy this list's own module
    # comment already applies to the security cluster.
    ("low",  r"\b(typo|renam|whitespace|reformat|formatting|lint|docstring|changelog|spelling|"
             r"dead code|comment(?:s|ed|ing)?\b|run (the )?(full )?tests?|re-?run|watch|watcher|"
             r"monitor|poll|status check)"),
]
_EFFORT_DEFAULT = "medium"

# Portable tiers are the ledger's stable vocabulary.  A Codex dispatch needs an actual host model
# and effort instead; these are the model IDs this Codex host exposes at the time #2638 shipped.
_CODEX_HOST_MODELS = {
    "haiku": {"model": "gpt-5.6-luna", "effort": "low"},
    "sonnet": {"model": "gpt-5.6-terra", "effort": "medium"},
    "opus": {"model": "gpt-6-astra", "effort": "high"},
    "fable": {"model": "gpt-6-astra", "effort": "high"},
}
_CODEX_DEFAULT_TIER = "sonnet"
_HOST_MODEL_OVERRIDE_KEY = "model_host_overrides"
_HOST_MODEL_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_HOST_MODEL_EFFORTS = frozenset(("low", "medium", "high"))
# A config override can choose among this release's actual Codex catalog, but cannot turn an
# unverified string into a dispatch argument.  Adding a newly released ID is a plugin change so
# its selection is reviewed and tested before it reaches a customer machine.
_CODEX_ALLOWED_MODELS = frozenset((
    "gpt-5.5", "gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-6-astra",
))


def _validate_host_model_pair(host, tier, pair):
    """Return a dispatchable pair or reject every malformed configured entry."""
    if not isinstance(pair, dict):
        raise ValueError(f"invalid {host} override for {tier}: expected an object")
    if set(pair) != {"model", "effort"}:
        raise ValueError(f"invalid {host} override for {tier}: expected only model and effort")
    model, effort = pair.get("model"), pair.get("effort")
    if not isinstance(model, str) or not _HOST_MODEL_ID.fullmatch(model):
        raise ValueError(f"invalid {host} model for {tier}")
    if model.lower() in _CODEX_HOST_MODELS or model.lower() == "off":
        raise ValueError(f"invalid {host} model for {tier}: portable tier or off is not a model ID")
    if model not in _CODEX_ALLOWED_MODELS:
        raise ValueError(f"invalid {host} model for {tier}: not an approved Codex model ID")
    if effort not in _HOST_MODEL_EFFORTS:
        raise ValueError(f"invalid {host} effort for {tier}")
    return {"model": model, "effort": effort}


def resolve_host_model(host, tier, config=None, *, effort=None):
    """Return the concrete Codex model/effort for one portable tier.

    Refuse rather than returning ``tier`` for an unknown host or tier: a portable label is never a
    valid Codex model ID, and silently passing it through defeats this resolver's purpose.  When
    a caller supplies the portable selection's ``effort``, preserve it exactly: host adaptation
    translates the model name, it does not make a second effort decision.  An override's effort
    remains the backward-compatible default for older callers that only have a tier.
    """
    host = (host or "").strip().lower()
    tier = (tier or "").strip().lower()
    if host != "codex":
        raise ValueError(f"unsupported host: {host!r}")
    # `resolve` deliberately prints `off` when automatic tier selection is disabled. That must
    # still produce a concrete model for a Codex dispatch, rather than inheriting the parent's
    # arbitrary selection. The versioned ordinary-work default stays overrideable as `sonnet`.
    if tier == "off":
        tier = _CODEX_DEFAULT_TIER
    try:
        pair = dict(_CODEX_HOST_MODELS[tier])
    except KeyError:
        raise ValueError(f"unknown model tier: {tier!r}") from None
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise ValueError("invalid host-model config (expected an object)")
    overrides = config.get(_HOST_MODEL_OVERRIDE_KEY, {})
    if not isinstance(overrides, dict):
        raise ValueError(f"invalid {_HOST_MODEL_OVERRIDE_KEY}: expected an object")
    for configured_host in overrides:
        if configured_host != host:
            raise ValueError(f"invalid model host override {configured_host!r}")
    host_overrides = overrides.get(host, {})
    if not isinstance(host_overrides, dict):
        raise ValueError(f"invalid {host} override: expected an object")
    for configured_tier, override in host_overrides.items():
        if not isinstance(configured_tier, str) or configured_tier not in _CODEX_HOST_MODELS:
            raise ValueError(f"invalid {host} override tier {configured_tier!r}")
        _validate_host_model_pair(host, configured_tier, override)
    if tier in host_overrides:
        pair = dict(host_overrides[tier])
    if effort is not None:
        if effort not in _HOST_MODEL_EFFORTS:
            raise ValueError(f"invalid {host} effort for {tier}")
        pair["effort"] = effort
    return _validate_host_model_pair(host, tier, pair)


def predict_effort_location(goal_text):
    """`(effort, signal, where, hits)` -- the effort axis with the tier axis's own safeguards for
    `high` (#1950): a match lying wholly inside a code span or a path-shaped token never counts; a hit
    in the TITLE (text up to the first `\n`) wins on one occurrence; a body-only hit needs
    `_BODY_MULTI_MIN_HITS` or more occurrences. Short of that the scan falls through to `low`, whose
    rules are unchanged (any occurrence). `(default, None, None, 0)` with no signal."""
    t = (goal_text or "").lower()
    nl = t.find("\n")
    title_end = len(t) if nl < 0 else nl
    spans = _excluded_spans(t)
    for tier, pat in _EFFORT_PATTERNS:
        if tier != "high":
            m = re.search(pat, t)
            if m:
                return tier, m.group(0), ("title" if m.start() < title_end else "body"), 1
            continue
        body_hits = []
        for m in re.finditer(pat, t):
            if _within_excluded_span(m.start(), m.end(), spans):
                continue
            if m.start() < title_end:
                return tier, m.group(0), "title", 1
            body_hits.append(m)
        if len(body_hits) >= _BODY_MULTI_MIN_HITS:
            return tier, body_hits[0].group(0), "body", len(body_hits)
    return _EFFORT_DEFAULT, None, None, 0


def predict_effort(goal_text):
    """Return the reasoning effort (low|medium|high) for a goal/step text. Deterministic."""
    return predict_effort_location(goal_text)[0]


def predict_with_reason(goal_text, excludes=(), max_tier=_MAX_TIER_DEFAULT):
    """Return `(tier, signal)` — the tier, and the LITERAL text that triggered it (issue #880).

    `predict()` used to run this loop and drop `re.search`'s match on the floor, so the tier was
    recorded everywhere and the reason nowhere. `signal` is `match.group(0)`: the actual substring
    that fired ("migrat", "secur"), never a paraphrase of the rule — a reader can check it against
    the goal text themselves, which a restated rule does not allow.

    `None` when no pattern matched and the FLOOR applies -- `_DEFAULT` only when `max_tier` is at
    or above `_DEFAULT`'s own price rank; a cap BELOW `_DEFAULT` binds the no-match case too (code
    review, #2564: the line used to name `_DEFAULT` unconditionally, which stopped being accurate
    the moment this function gained a cap that can sit under it -- `test_cap_also_binds_the_
    default_tier` pins the binding). "Nothing matched" is still the honest answer either way;
    manufacturing a reason for a defaulted or floored tier would be worse than recording none.

    `max_tier` (issue #2564): the PRICE ceiling, defaulting to `_MAX_TIER_DEFAULT` so the guard is
    on for every caller including a direct Python one. The scan is UNTOUCHED -- it runs exactly as
    it did, and the ceiling reads only its result, so this composes with `excludes` rather than
    interacting with it. When the winning tier is priced above the ceiling the TIER clamps to
    `min(_DEFAULT, cap)` and THE SIGNAL THAT FIRED IS KEPT -- returning `None` there would make
    `('sonnet', None)` mean two different things (see the paragraph above), blank out the literal
    `model_selection_signal_excludes`' own documented discovery gesture tells a repo to copy from
    `why`, and leave every capped goal's `model_choice` ledger event with no reason at all, since
    `loop.py` adds `signal` to that event only when it is truthy.

    Clamping to `_DEFAULT` rather than FALLING THROUGH to the next tier is the load-bearing choice,
    and it is the opposite of what `excludes` does one level down. Fall-through reproduces #2564's
    own expected table, and sends "rewrite the marketing copy for the pricing page and rename the
    CTA constant" to HAIKU -- a 10x capability drop, two tiers below `_DEFAULT`. The router cannot
    distinguish that from "fix the typo in the blog post title": both are one fable stem plus one
    haiku stem, and a haiku stem in the TITLE -- the only place one counts since #2827 -- is still
    common enough on a real goal that fall-through would misroute. Clamping keeps this module's
    own upward-resolution invariant true, erring at 2x on a
    trivial goal instead of at 0.5x on a hard one.

    `_PATTERNS` is ordered high-to-low, so on a mixed goal ("fix the typo in the security module"
    → opus) the signal returned is the one that WON. Reporting a losing signal beside the winning
    tier would be actively misleading, which is why a test pins exactly that case.

    `excludes` (issue #1601, optional): signals this repo has declared to be domain vocabulary --
    see `_is_excluded` for what an entry names. Still PURE: the list is passed in, never read from
    disk here, so this keeps the zero-dep/hermetic contract the module docstring states;
    `resolve`/`resolve_step`/`main` are the ones that read config and hand it down.

    An excluded signal FALLS THROUGH -- the scan resumes rather than stopping -- and it resumes
    INSIDE THE SAME TIER FIRST, which is why this is `finditer` and not `re.search`. `re.search`
    returns the leftmost match only, so "rewrite the narrative storytelling guide" with `narrativ`
    excluded would report the excluded signal and, skipping to the next tier on that basis, drop a
    genuinely creative goal to sonnet. Only when NO branch of a tier survives does the next tier get
    its turn, in the same high-to-low order -- so exclusion never reorders the tiers, it only
    removes a reason to stop. With no excludes, `finditer`'s first match IS `re.search`'s match and
    every call returns on it, which is what makes the unconfigured default byte-identical.
    """
    return predict_with_location(goal_text, excludes, max_tier)[:2]


#: The one tier whose stems count only inside the title (#2827) -- see the module docstring.
_TITLE_ONLY_TIERS = ("haiku",)

#: The tiers whose stems, found ONLY in the body, need 2+ hits rather than 1 (#390) -- see the
#: module docstring. A title hit still wins on one occurrence, for these exactly as for any other
#: tier; this constant only ever narrows the BODY-alone path.
_BODY_MULTI_TIERS = ("opus", "fable")
#: The minimum body-only occurrence count #390 requires. A named constant rather than a bare `2`
#: so the one number the module docstring, this comment and the loop below must agree on lives in
#: exactly one place.
_BODY_MULTI_MIN_HITS = 2

#: A code span: a triple-backtick fenced block (DOTALL, non-greedy, so it closes at the NEXT triple
#: backtick rather than swallowing the rest of the text) or a single-backtick inline span that does
#: not cross a newline (an unterminated inline backtick is therefore never treated as open-ended --
#: it simply matches nothing, exactly as an unterminated fence would confuse a reader too).
_CODE_SPAN = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)
#: A path-shaped token (#390's own issue text, verbatim) -- non-whitespace, a slash, non-
#: whitespace. This is deliberately wider than a filesystem path: `feature/auth-billing-security`
#: (a branch literal) and `and/or` both match it too, which is the accepted, documented trade the
#: issue itself makes (`\S+/\S+`, not a narrower filename-shaped pattern) -- a citation is not a
#: description of the work either way. Matched as "a whitespace-delimited token with a slash that
#: has a character on each side" (`"/" in token[1:-1]`), the same token set `\S+/\S+` selects but
#: in one linear pass: the regex form is quadratic on a long slash-free blob (measured 57 s at
#: 200 KB when its guard test was run red against the regex form), and pick time reads whole issue bodies.
_TOKEN = re.compile(r"\S+")


def _excluded_spans(t):
    """Every `(start, end)` span in `t` that a signal match must NOT lie wholly inside (#390): a
    code span or a path-shaped token. Computed once per `predict_with_location` call over the WHOLE
    text (title and body both), so a citation in the title is excluded exactly as one in the body
    is -- there is no reason a doc-path or a branch literal describes the work any more just
    because it happens to sit on the first line."""
    spans = [(m.start(), m.end()) for m in _CODE_SPAN.finditer(t)]
    spans += [(m.start(), m.end()) for m in _TOKEN.finditer(t) if "/" in m.group(0)[1:-1]]
    return spans


def _within_excluded_span(start, end, spans):
    """Does the signal match `[start, end)` lie ENTIRELY inside one of `spans`? Containment, not
    overlap (#390) -- `s <= start and end <= e` -- so a genuine prose mention that merely sits
    NEXT TO a path or code span (not inside one) still counts. A match straddling a span's boundary
    (starts inside, ends outside, or vice versa) is not contained by definition and so counts too;
    that is deliberate, not a gap -- a signal is either wholly a citation or it is not one at all."""
    return any(s <= start and end <= e for s, e in spans)


def predict_with_location(goal_text, excludes=(), max_tier=_MAX_TIER_DEFAULT, *,
                          haiku_anywhere=False):
    """`(tier, signal, where, hits)` -- `predict_with_reason`'s pair plus WHERE the signal was
    found (`"title"`/`"body"`, `None` with no signal) and HOW MANY qualifying occurrences backed
    the decision (#390; always `0` alongside `None`, always `>= 1` alongside a signal).

    A `_TITLE_ONLY_TIERS` match counts only when it lies wholly inside the title (#2827); one found
    only in the body is skipped exactly as an excluded signal is, so the scan falls through to the
    default. `haiku_anywhere=True` restores whole-text scanning, for `resolve_step` alone.

    A `_BODY_MULTI_TIERS` match found in the TITLE still wins on one occurrence, exactly like any
    other tier (#390 only narrows the BODY-alone path -- see the module docstring for why a title
    hit is trusted where a lone body hit is not). Found only in the BODY, it needs
    `_BODY_MULTI_MIN_HITS` or more qualifying occurrences before the tier wins; short of that the
    scan falls through to the next tier, exactly as an excluded signal does. "Occurrences" are
    `finditer`'s own non-overlapping matches -- NOT distinct literal text: a real migration issue
    that only ever writes the stem "migrat" (migrate/migration/migrating all match identically)
    still needs to satisfy the threshold on repetition alone, so requiring different literal text
    per hit would misroute exactly the true-positive case this exists to protect.

    Both `_TITLE_ONLY_TIERS` and `_BODY_MULTI_TIERS` share one more gate, applied first: a match
    lying ENTIRELY inside a code span or a path-shaped token (`_excluded_spans`/
    `_within_excluded_span`, #390) is skipped like an excluded signal, in title or body alike --
    a citation is not a description of the work.

    The title is the text up to the first `\n`, taken LITERALLY: a leading blank line is an empty
    title, never stripped, because stripping would promote the body's first line to title (the very
    bug #2827 removed from `loop.py`'s own pick-time call). Spans are computed on the LOWERCASED
    text, the one the patterns scan, so a case fold that changes length cannot misalign them."""
    t = (goal_text or "").lower()
    nl = t.find("\n")
    title_end = len(t) if nl < 0 else nl
    cap = _price_rank(max_tier)
    floor = _TIER_PRICE_ORDER[min(_price_rank(_DEFAULT), cap)]
    excluded_spans = _excluded_spans(t)
    for tier, pat in _PATTERNS:
        title_only = tier in _TITLE_ONLY_TIERS and not haiku_anywhere
        body_multi = tier in _BODY_MULTI_TIERS
        body_hits = []
        for m in re.finditer(pat, t):
            if title_only and m.end() > title_end:
                break               # finditer is left-to-right: every later match is body too
            lit = m.group(0)
            if _is_excluded(lit, excludes):
                continue
            if _within_excluded_span(m.start(), m.end(), excluded_spans):
                continue
            in_title = m.start() < title_end
            if body_multi and not in_title:
                body_hits.append(m)
                continue
            where = "title" if in_title else "body"
            return (floor if _price_rank(tier) > cap else tier), lit, where, 1
        if body_multi and len(body_hits) >= _BODY_MULTI_MIN_HITS:
            lit = body_hits[0].group(0)
            return (floor if _price_rank(tier) > cap else tier), lit, "body", len(body_hits)
    return floor, None, None, 0


def predict(goal_text, excludes=(), max_tier=_MAX_TIER_DEFAULT):
    """Return the model tier (haiku|sonnet|opus|fable) for a goal from its text. Deterministic.

    Signature and return type unchanged by #880 — every existing caller (`resolve`, `resolve_step`,
    both CLI verbs) is untouched; the reason is available beside it via `predict_with_reason`.
    #1601 adds `excludes`, defaulted so every one of those callers stays a pure re-dispatch; #2564
    adds `max_tier` on the same terms, defaulted to the CEILING rather than to "uncapped" so a
    caller that forgets it is guarded, not exposed."""
    return predict_with_reason(goal_text, excludes, max_tier)[0]


def _emit_model_choice(sdlc_dir, goal, tier, signal):
    """Best-effort, code-driven ledger write (issue #1030) -- NEVER raises. Shells out to
    `loop.py emit <sdlc_dir> <goal> model_choice --model <tier> [--signal <signal>]`, the same
    sanctioned CLI verb a SKILL.md instruction would otherwise ask an agent to remember -- moving
    the call in here means it happens every time `resolve`/`resolve_step` run, not only when a
    calling skill's own prose reminds an agent to also run it (the exact pattern #1013 found
    failing: `sigma-retro`'s SKILL.md carried an identical "also call `loop.py emit ... retro`"
    line, and zero retro events were ever recorded across 559 real ledger files).

    Subprocess, not an in-process import of ledger.py/loop.py: this module stays zero-dep and
    hermetically testable (see the module docstring), and `skills/sigma-model` has no Python
    coupling to `skills/sigma-loop` today -- shelling out to the sibling skill's own CLI mirrors the
    same relative-path convention `sigma-loop`'s own SKILL.md already uses in the OTHER direction
    (`${CLAUDE_SKILL_DIR}/../sigma-model/scripts/predict.py`), rather than introducing the first
    cross-skill Python import.

    `loop.py emit` already degrades gracefully with no ledger/journal configured (prints an
    OFF/skip line to ITS OWN stdout -- captured, never leaked into this process's -- and exits 0)
    and never lets an OSError escape past itself. This function's own `try/except` covers the
    SUBPROCESS INVOCATION itself (a missing interpreter, a moved/missing loop.py, a hang) with the
    identical fail-open contract, so a bare `tmp_path` fixture with no `.sdlc` layout at all is
    exactly as safe as a fully configured repo -- nothing here ever raises past this function."""
    loop_py = pathlib.Path(__file__).resolve().parent.parent.parent / "sigma-loop" / "scripts" / "loop.py"
    argv = [sys.executable, str(loop_py), "emit", str(sdlc_dir), str(goal), "model_choice",
            "--model", tier]
    if signal:
        argv += ["--signal", signal]
    try:
        subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except Exception:
        pass   # fail-open: a write failure (or no ledger/journal configured) must never surface here


def resolve(goal_text, sdlc_dir=".sdlc", goal=None):
    """The tier to run this goal's phases at, honoring config. Returns a tier only when
    `model_selection` is 'auto'; otherwise None. Inline work then uses the session model; a Codex
    dispatcher that still starts a child maps its explicit `off` fallback with `host-model`.

    `goal` (issue #1030, optional): the goal's own identifier (issue number / file path / stem) --
    passed through to the `model_choice` ledger event this now writes internally, as `loop.py
    emit`'s own `<goal>` argument. `None` (the default, and every caller before this issue) skips
    the write outright -- there is no identifier to key the ledger record on, so nothing is even
    attempted; this is also exactly what keeps every existing direct-Python-call test in
    tests/test_model_predict.py (none of which pass this new kwarg) at ZERO subprocess attempts,
    the strongest form of "fail open" for that fixture. The CLI (`main`, the `resolve` verb) passes
    `argv[2]` UNCHANGED -- the original identifier, before `_read()` substitutes local-mode file
    content for it -- which is the one place both values are still simultaneously available, and
    needs no SKILL.md prose change: `"$goal"` is already what today's `resolve "$goal" .sdlc`
    invocation passes as `goal_text`, so `main` reusing that same value as the identifier is
    transparent to every existing caller (`sigma-loop`, `sigma-goal`).

    #1601: the same config read also supplies `model_selection_signal_excludes`, so the tier the
    loop runs at and the `signal` it records are both computed AFTER this repo's own vocabulary is
    taken out -- recording an excluded signal would leave a downstream model-tier metric joining on a
    reason that never applied."""
    cfg = _cfg(sdlc_dir)
    if _model_selection(cfg) != "auto":
        return None
    tier, signal = predict_with_reason(goal_text, signal_excludes(cfg), max_tier(cfg))
    # The portable choice becomes an advisory ledger event below.  Validate its Codex dispatch
    # mapping first so a malformed override cannot leave a false "model_choice" audit trail that
    # suggests a phase may run when its real host command must refuse.
    resolve_host_model("codex", tier, _host_model_cfg(sdlc_dir),
                       effort=predict_effort(goal_text))
    if goal is not None:
        _emit_model_choice(sdlc_dir, goal, tier, signal)
    return tier


def resolve_profile(goal_text, sdlc_dir=".sdlc", goal=None):
    """Return the single portable ``{model, effort}`` selection for either host.

    The classifier owns both axes.  Claude consumes this pair directly; Codex translates only
    ``model`` through :func:`resolve_host_model` and must be passed this exact ``effort``.  ``off``
    preserves the existing no-auto-selection state while giving a dispatched Codex child its
    versioned ordinary-work effort.
    """
    tier = resolve(goal_text, sdlc_dir, goal=goal)
    if tier is None:
        tier, effort = "off", _CODEX_HOST_MODELS[_CODEX_DEFAULT_TIER]["effort"]
    else:
        effort = predict_effort(goal_text)
    resolve_host_model("codex", tier, _host_model_cfg(sdlc_dir), effort=effort)
    return {"model": tier, "effort": effort}


#: #2020: the reviewer phases (loop vocabulary and the underscore spellings) and the flat config key
#: for their floor. A FLAT sibling of `model_selection`, like the ceiling key above.
_REVIEWER_PHASES = ("plan-review", "plan_review", "review", "code-review", "code_review",
                    "pr-review", "pr_review")
_REVIEWER_FLOOR_KEY = "model_selection_reviewer_floor"
_REVIEWER_FLOOR_DEFAULT = "sonnet"


def reviewer_floor(cfg):
    """The configured reviewer floor tier; junk or an unknown tier reads as the default."""
    try:
        raw = cfg.get(_REVIEWER_FLOOR_KEY)
    except AttributeError:
        return _REVIEWER_FLOOR_DEFAULT
    tier = raw.strip().lower() if isinstance(raw, str) else None
    return tier if tier in _TIER_PRICE_ORDER else _REVIEWER_FLOOR_DEFAULT


def phase_tier(tier, phase, cfg):
    """THE single seam for the reviewer floor (#2020): the tier `phase` runs at, given the tier
    resolved for the goal. Under `auto`, a reviewer phase resolves to max(tier, floor); signals
    raise it, never lower it. The ceiling (`model_selection_max_tier`) wins over the floor, so the
    floor is clamped to it first. `off`/absent: returned unchanged (the floor cannot lower cost
    there). Every other phase is returned unchanged."""
    if _model_selection(cfg) != "auto" or phase not in _REVIEWER_PHASES:
        return tier
    floor = _TIER_PRICE_ORDER[min(_price_rank(reviewer_floor(cfg)), _price_rank(max_tier(cfg)))]
    return max(tier, floor, key=_price_rank)


def resolve_phase(phase, tier, sdlc_dir=".sdlc"):
    """The tier for `phase` given the goal's tier, or None when `model_selection` is not auto."""
    cfg = _cfg(sdlc_dir)
    if _model_selection(cfg) != "auto":
        return None
    return phase_tier(tier, phase, cfg)


def resolve_step(step_text, sdlc_dir=".sdlc", goal=None):
    """Per-STEP resolution: {model, effort} honoring the same config gate, else None.
    Lets a mechanical step inside a hard goal run cheaper than the goal's ceiling.

    `goal` (issue #1030, optional): same contract as `resolve`'s own `goal` parameter, with one
    difference worth being explicit about -- step TEXT can never double as a goal identifier the
    way a goal-level call's own text can (in github mode, `resolve`'s `goal_text` already IS the
    bare issue number; a plan step's text never is), so the CLI's `resolve-step` verb needs a
    genuinely NEW trailing argument to supply it (see `main`, and sigma-loop's own SKILL.md). The
    `signal` computed for the emit call is used ONLY for that call and then discarded -- NOT added
    as a third key to this function's own returned dict, which stays exactly {"model", "effort"}
    (test_resolve_step_gated_by_config asserts exact dict equality; a third key would break it).

    #1601: honors `model_selection_signal_excludes` exactly as `resolve` does. The EFFORT axis is
    deliberately left alone -- `_EFFORT_PATTERNS` shares no term with the fable cluster this issue
    is about, and widening the knob to a second axis on no evidence it is wrong there would be
    scope the issue does not have."""
    cfg = _cfg(sdlc_dir)
    if _model_selection(cfg) != "auto":
        return None
    tier, signal, _, _ = predict_with_location(step_text, signal_excludes(cfg), max_tier(cfg),
                                               haiku_anywhere=True)   # #2827: step semantics kept
    effort = predict_effort(step_text)
    resolve_host_model("codex", tier, _host_model_cfg(sdlc_dir), effort=effort)
    if goal is not None:
        _emit_model_choice(sdlc_dir, goal, tier, signal)
    return {"model": tier, "effort": effort}


def _read(arg):
    """`arg` expanded to file content when it names a real file, else `arg` itself unchanged.

    Issue #1627: `Path(arg).exists()` calls `stat()` under the hood, and `stat()` can raise
    `OSError` for an `arg` that was never meant as a path at all -- `ENAMETOOLONG` once a single
    path component clears the OS's `NAME_MAX` (confirmed at ~255 bytes on this platform), which
    real prose clears easily (a GitHub issue's title+body, or a genuine plan step description, is
    routinely hundreds of characters with no filesystem meaning whatsoever). `Path.exists()`'s own
    `_ignore_error` only swallows ENOENT/ENOTDIR/EBADF/ELOOP -- not ENAMETOOLONG -- so this used to
    propagate uncaught, silently crashing every CLI verb that calls this function (`resolve`,
    `resolve-step`, `why`, the bare verb) the moment a caller passed real prose instead of a short
    phrase or a genuine file path. `ValueError` is caught too for the same reason (e.g. an
    embedded NUL byte, which `pathlib` itself rejects on some platforms) -- either way, "this was
    never a real path" is the correct, non-crashing answer: return `arg` unchanged, exactly as the
    non-existent-path branch already does."""
    try:
        p = pathlib.Path(arg)
        return p.read_text(encoding="utf-8", errors="ignore") if p.exists() else arg
    except (OSError, ValueError):
        return arg


#: Mirrors `skills/sigma-loop/scripts/frontmatter.py`'s `_FENCE` byte for byte (this module cannot
#: import it -- skills do not import each other's Python), CRLF included.
_FM_FENCE = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)
_FM_TITLE = re.compile(r"^[ \t]*title[ \t]*:(.*)$", re.MULTILINE)


def _read_goal(arg):
    """`_read` for the GOAL verbs (`resolve`, `why`, the bare verb): a real goal FILE comes back as
    `"<title>\n\n<raw file text>"`, so the title sits on the first line where #2827 looks for
    it.

    The title is taken exactly as `sources.LocalSource.fetch_title_body` takes it -- the frontmatter
    `title:` value (the LAST such line, as `frontmatter.parse` keeps it; `.strip()` then
    `.strip('"')`), else the file's stem when there is no fence, no key or an empty value -- so the
    documented local fallback (`resolve "$goal"`) and the tier the pick recorded cannot disagree on
    the TITLE (lines split on `\n` only, where `frontmatter.parse` uses `splitlines()` -- a
    difference only a U+2028-style separator inside the fence can reach). The BODY still differs, as
    it did before #2827: the raw text, frontmatter included, stays the body here, so opus/fable scan
    every byte they scanned before (and now the stem too), while the pick-time read strips the
    frontmatter -- an opus stem only in, say, `done_when:` reaches opus here and not at pick.
    Anything that is not a readable file -- prose, a long string that raises ENAMETOOLONG, a
    directory -- is returned unchanged, exactly as `_read` returns it."""
    try:
        p = pathlib.Path(arg)
        if not p.is_file():
            return _read(arg)
        text = p.read_text(encoding="utf-8", errors="ignore")
    except (OSError, ValueError):
        return arg
    fence = _FM_FENCE.match(text)
    keys = _FM_TITLE.findall(fence.group(1)) if fence else []
    title = keys[-1].strip().strip('"') if keys else ""
    return f"{title or p.stem}\n\n{text}"


USAGE = ("usage: predict.py '<goal>' [sdlc_dir] | resolve '<goal>' [sdlc_dir] | "
         "resolve-profile '<goal>' [sdlc_dir] | "
         "resolve-step '<step>' [sdlc_dir] [goal] | "
         "resolve-phase <phase> <tier> [sdlc_dir] | why '<goal>' [sdlc_dir] | "
         "host-model codex <tier-or-off> [.sdlc] [--effort low|medium|high]")


def main(argv):
    # The host-model command translates a portable tier into real Codex dispatch arguments. It is deliberately
    # ungated by model_selection: callers already hold a tier and must either map it explicitly or
    # fail before dispatch, never pass that tier through as a model ID.
    if argv[1:] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    if len(argv) >= 4 and argv[1] == "host-model":
        try:
            tail = argv[4:]
            effort = None
            if "--effort" in tail:
                i = tail.index("--effort")
                if i + 1 >= len(tail) or tail.count("--effort") != 1:
                    raise ValueError("host-model --effort needs one value")
                effort = tail[i + 1]
                del tail[i:i + 2]
            if len(tail) > 1:
                raise ValueError("host-model accepts one optional .sdlc directory")
            pair = resolve_host_model(argv[2], argv[3],
                                      _host_model_cfg(tail[0] if tail else ".sdlc"), effort=effort)
        except ValueError as exc:
            print(f"predict.py: {exc}", file=sys.stderr)
            return 2
        print(f"model={pair['model']} effort={pair['effort']}")
        return 0
    if len(argv) >= 3 and argv[1] == "resolve-profile":
        sdlc_dir = argv[3] if len(argv) > 3 else ".sdlc"
        try:
            profile = resolve_profile(_read_goal(argv[2]), sdlc_dir, goal=argv[2])
        except ValueError as exc:
            print(f"predict.py: {exc}", file=sys.stderr)
            return 2
        print(f"model={profile['model']} effort={profile['effort']}")
        return 0
    # resolve: config-gated tier for the loop ("off" when model_selection isn't auto).
    # Output stays a bare tier for backward compatibility with existing callers.
    # issue #1030: `goal=argv[2]` reuses the ORIGINAL (pre-_read) value as the ledger identifier --
    # in github mode that IS already the bare issue number `_read` would return unchanged anyway;
    # in local mode it is the goal file's own path. Either way this is the one place both the raw
    # identifier and its `_read()`-derived text are simultaneously available, and no existing
    # invocation of this verb changes shape: `"$goal"` was already argv[2]. GitHub callers that
    # must classify title+body rather than the bare issue number can pass the number as an
    # optional fourth positional argument, preserving attribution without changing old calls.
    if len(argv) >= 3 and argv[1] == "resolve":
        if argv[2] == "-" and len(argv) < 5:
            print("predict.py: `resolve -` reads stdin and needs the goal id as the 4th argument "
                  "(resolve - <sdlc_dir> <goal>)", file=sys.stderr)
            return 2
        sdlc_dir = argv[3] if len(argv) > 3 else ".sdlc"
        goal_id = argv[4] if len(argv) > 4 else argv[2]
        try:
            tier = resolve(sys.stdin.read() if argv[2] == "-" else _read_goal(argv[2]), sdlc_dir,
                           goal=goal_id)       # `-`: text on stdin, never in shell quotes (#713)
        except ValueError as exc:
            print(f"predict.py: {exc}", file=sys.stderr)
            return 2
        print(tier or "off")
        return 0
    # resolve-phase: the goal's tier floored for a reviewer phase (#2020), or `off`.
    if len(argv) >= 4 and argv[1] == "resolve-phase":
        if argv[3] not in _TIER_PRICE_ORDER:
            print(f"predict.py: unknown tier {argv[3]!r}", file=sys.stderr)
            return 2
        print(resolve_phase(argv[2], argv[3], argv[4] if len(argv) > 4 else ".sdlc") or "off")
        return 0
    # resolve-step: the per-step pair — `model=<tier> effort=<low|medium|high>` or `off`.
    # issue #1030: an OPTIONAL 4th positional argument is the goal id -- step text can never double
    # as one the way resolve's own goal_text can, so this is a genuinely new argument, not a reuse
    # of an existing one. Omitted (every invocation before this issue) -> goal stays None -> the
    # emit attempt inside resolve_step is skipped, byte-identical to pre-#1030 behavior.
    if len(argv) >= 3 and argv[1] == "resolve-step":
        sdlc_dir = argv[3] if len(argv) > 3 else ".sdlc"
        goal = argv[4] if len(argv) > 4 else None
        try:
            pair = resolve_step(_read(argv[2]), sdlc_dir, goal=goal)
        except ValueError as exc:
            print(f"predict.py: {exc}", file=sys.stderr)
            return 2
        print(f"model={pair['model']} effort={pair['effort']}" if pair else "off")
        return 0
    # why: the tier AND the literal text that triggered it (#880), for `loop.py log model_choice
    # --model <tier> --signal <signal>`. Ungated by `model_selection` on purpose — this answers
    # "what would be chosen and why", which is worth asking whether or not auto is on, and it
    # writes nothing, so an exploratory call has no side effect.
    # issue #1601: an OPTIONAL trailing sdlc_dir (default `.sdlc`, same as the two verbs above), so
    # `why` answers with the excludes THIS repo declared. `why` is the diagnostic someone runs to
    # check an exclusion took effect; one that disagreed with `resolve` would send exactly the
    # person already confused looking in the wrong place. Ungated by `model_selection` like the rest
    # of this verb -- the repo's vocabulary is true whether or not auto-selection is switched on --
    # and still side-effect-free: this reads config, it still records nothing.
    if len(argv) >= 3 and argv[1] == "why":
        _c = _cfg(argv[3] if len(argv) > 3 else ".sdlc")
        # #2827: `in=<title|body>` sits BEFORE `signal=` so the signal stays the free-text tail a
        # repo copies (signals contain spaces: `dead code`, `race condition`). #390: `hits=<n>`
        # sits between them, same reason -- never inside the free-text tail.
        tier, signal, where, hits = predict_with_location(_read_goal(argv[2]), signal_excludes(_c),
                                                          max_tier(_c))
        print(f"model={tier} in={where} hits={hits} signal={signal}" if signal
              else f"model={tier} signal=")
        # #1950: the effort axis on a second line, same field order, so the first line a caller
        # parses is byte-identical to before.
        effort, e_signal, e_where, e_hits = predict_effort_location(_read_goal(argv[2]))
        print(f"effort={effort} in={e_where} hits={e_hits} signal={e_signal}" if e_signal
              else f"effort={effort} signal=")
        return 0
    if len(argv) < 2 or argv[1] in ("resolve", "resolve-step", "resolve-phase", "why"):
        print(USAGE, file=sys.stderr)
        return 2
    # The bare verb takes the same optional trailing sdlc_dir (#1601). It is /sigma-model's headline
    # command, so a recommendation that ignored the excludes the loop honors would be contradicted
    # by the loop moments later. Config-gating is still none of its business: like `why`, it answers
    # "what would be chosen", not "what is switched on".
    _c = _cfg(argv[2] if len(argv) > 2 else ".sdlc")
    print(predict(_read_goal(argv[1]), signal_excludes(_c), max_tier(_c)))
    return 0


if __name__ == "__main__":
    # The script-time floor: this run's wall time, recorded to the session resolved at exit. The
    # store lives with the loop skill's scripts, reached by path the way `loop_py` is above.
    import importlib.util as _ilu, pathlib as _pl
    _spec = _ilu.spec_from_file_location(
        "timing_store",
        _pl.Path(__file__).resolve().parent.parent.parent / "sigma-loop" / "scripts" / "timing_store.py")
    _ts = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_ts)
    sys.exit(_ts.timed_main(main, sys.argv, "predict"))
