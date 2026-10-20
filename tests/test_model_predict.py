"""Model auto-selection predictor (sigma-model/predict.py): a deterministic goal->tier heuristic.
Pins each tier, the upward conflict-resolution rule, and the default so a wording change that
silently down-tiers hard work fails here."""
import pathlib, importlib.util, json

P = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-model" / "scripts" / "predict.py"


def _mod():
    spec = importlib.util.spec_from_file_location("predict", P)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def test_codex_model_override_routes_goal_and_step_without_changing_claude(tmp_path, monkeypatch):
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir()
    (sdlc / "config.json").write_text(json.dumps({"model_selection": "off",
        "model_selection_host_overrides": {"codex": "auto"}}))
    m = _mod()
    monkeypatch.setenv("CODEX_THREAD_ID", "task")
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("SIGMA_HOST", raising=False)
    monkeypatch.delenv("CODEX_SESSION_ID", raising=False)
    assert m.resolve("migrate the schema", str(sdlc)) == "opus"
    assert m.resolve_step("run the tests", str(sdlc))["model"] == "sonnet"
    monkeypatch.delenv("CODEX_THREAD_ID")
    monkeypatch.setenv("CLAUDECODE", "1")
    assert m.resolve("migrate the schema", str(sdlc)) is None
    assert m.resolve_step("run the tests", str(sdlc)) is None


def test_hard_goals_get_opus():
    p = _mod().predict
    for g in ("migrate the database schema", "redesign the auth architecture",
              "fix the race condition in the scheduler", "add payment processing"):
        assert p(g) == "opus", g


def test_trivial_goals_get_haiku():
    p = _mod().predict
    for g in ("fix a typo in the README", "rename the helper for clarity",
              "reformat the config", "remove dead code"):
        assert p(g) == "haiku", g


def test_creative_goals_get_fable():
    p = _mod().predict
    for g in ("draft the product vision", "write the launch blog narrative",
              "write the storytelling for the launch"):
        assert p(g, max_tier="fable") == "fable", g          # #2564: ceiling opted up


def test_ordinary_code_defaults_to_sonnet():
    p = _mod().predict
    for g in ("add a retry to the http client", "wire the new CLI flag", ""):
        assert p(g) == "sonnet", g


def test_conflict_resolves_upward():
    # a trivial word next to a hard one must NOT down-tier: hard wins.
    assert _mod().predict("fix the typo in the security module") == "opus"


def test_no_false_trigger_on_substrings():
    # #2564 code review: max_tier="fable" opted up on every call here -- under the DEFAULT cap a
    # genuine fable match is silently clamped to "sonnet", identical to a correct non-match, which
    # would make this assertion pass whether or not the regex still correctly avoids these
    # substrings. Opting the ceiling up restores the original regex-testing purpose exactly.
    p = _mod().predict
    assert p("update the revision history", max_tier="fable") == "sonnet"   # 'vision'/'story' are substrings — must not fire
    assert p("improve the provision logic", max_tier="fable") == "sonnet"


def test_agile_story_phrasing_is_not_fable():
    # bare 'story' is agile jargon (user story / story point / 'Story:' label), not creative
    # writing — must not route to the fable (creative) tier. Genuine storytelling still does,
    # via the 'storytell' pattern (see test_creative_goals_get_fable).
    # #2564 code review: max_tier="fable" opted up, same reasoning as test_no_false_trigger_on_
    # substrings above -- `!= "fable"` is vacuous under the default cap, since a real match would
    # clamp to "sonnet" and pass anyway.
    p = _mod().predict
    for g in ("Story: add pagination", "Implement the user story for checkout",
              "Add a story point field"):
        assert p(g, max_tier="fable") != "fable", g


# --- issue #1780: bare 'prose' collided with this repo's own SDLC term of art -------------------
# "prose-invoked"/"prose-gated"/"prose-dependent" (and every other hyphenated `prose-*` compound
# already in this tree) mean "driven by an agent reading a SKILL.md", nothing about creative
# writing — reproduced mechanically against issue #1703's own real fetched title+body
# (`('fable', 'prose')`, dropping to `('sonnet', None)` once "prose" is scrubbed). Same shape as
# `test_agile_story_phrasing_is_not_fable` above: a bare domain term of this repo's own jargon must
# not reach the creative tier, while the genuine creative signal stays live via 'storytell' etc.


def test_hyphenated_sdlc_prose_compounds_are_not_fable():
    # #2564 code review: max_tier="fable" opted up -- same vacuous-under-the-default-cap reasoning
    # as the two tests above.
    p = _mod().predict
    for g in ("agent_dispatch --phase observability stays prose-invoked",
              "the writer-side half was closed and the invocation left prose-gated",
              "that remains prose-dependent, same as resolve-step",
              "that is the same prose-only decay #1030's own guardrail cited",
              "a prose-driven fallback until the code path lands"):
        assert p(g, max_tier="fable") != "fable", g


def test_bare_prose_still_reaches_fable():
    """The narrowing in #1780 is scoped to the HYPHENATED shape only — genuine creative-writing
    "prose" (no hyphen following) must keep firing, exactly as `prose` in `test_creative_goals_get_
    fable`'s sibling terms already does."""
    p = _mod().predict
    for g in ("polish the prose in the landing page copy", "this reads as purple prose"):
        assert p(g, max_tier="fable") == "fable", g          # #2564: ceiling opted up


def test_prosecution_is_untouched_by_the_1780_narrowing():
    """`prose(?!-)` only blocks a following hyphen — it does not add a trailing `\\b`, so the
    unrelated substring collision inside "prosecution" (#1625's own separate, still-open issue)
    is neither fixed nor newly broken by this change."""
    p = _mod().predict
    assert p("Fix the prosecution case export", max_tier="fable") == "fable"   # #2564: opted up


def test_issue_1703_real_text_no_longer_misroutes_to_fable():
    """The exact reproduction #1780 reports: issue #1703's own real fetched title+body classified
    as ('fable', 'prose') before this fix, solely because of 'prose-invoked' appearing three times
    — scrubbing the literal substring "prose" dropped it to ('sonnet', None), proving "prose" was
    the sole trigger. Inlined verbatim rather than fetched live, so the test is hermetic."""
    import re
    m = _mod()
    text = (
        "Follow-up from #1627: agent_dispatch --phase observability stays prose-invoked\n"
        "## Follow-up from #1627: scope item 2 (`agent_dispatch --role phase --phase <phase>` "
        "observability) is not yet code-driven\n\n"
        "#1627's Scope section named two items. Item 1 (a code path calls `predict.py resolve`/"
        "`resolve-step` and records both) is fixed by #1627 for the goal-level half, with "
        "`resolve-step` staying prose-invoked by deliberate, documented scope decision (no "
        "structured, goal-agnostic plan-step artifact exists to automate against). Item 2 is "
        "untouched by #1627's diff.\n\n"
        "Closing this the same way #1627 closed `model_choice` would need either (a) a host-side "
        "hook Claude Code fires on subagent dispatch, or (b) accepting that this stays "
        "prose-invoked, same as `resolve-step`, and finding a DIFFERENT way to make the compliance "
        "rate observable after the fact."
    )
    assert text.count("prose") == 3
    assert m.predict_with_reason(text, m.signal_excludes({})) == ("sonnet", None)
    scrubbed = re.sub(r"(?i)prose", "", text)
    assert m.predict_with_reason(scrubbed, m.signal_excludes({})) == ("sonnet", None)


def test_main_reads_text_and_file(tmp_path):
    m = _mod()
    assert m.main(["predict.py", "migrate the tables"]) == 0
    f = tmp_path / "goal.md"; f.write_text("fix a typo")
    assert m.main(["predict.py", str(f)]) == 0
    assert m.main(["predict.py"]) == 2   # usage


def _sdlc(tmp_path, model_selection):
    import json
    base = tmp_path / ".sdlc"; base.mkdir(exist_ok=True)   # same tmp_path reused across calls in a test
    (base / "config.json").write_text(json.dumps({"model_selection": model_selection}))
    return str(base)


def test_resolve_gates_on_config(tmp_path):
    """resolve() returns a tier only under model_selection:auto — the loop stays unchanged when off."""
    m = _mod()
    assert m.resolve("migrate the schema", _sdlc(tmp_path, "auto")) == "opus"
    assert m.resolve("migrate the schema", _sdlc(tmp_path, "off")) is None
    assert m.resolve("migrate the schema", str(tmp_path / "missing")) is None   # no config → off


def test_resolve_cli_prints_off_when_disabled(tmp_path, capsys):
    m = _mod()
    m.main(["predict.py", "resolve", "add a retry", _sdlc(tmp_path, "off")])
    assert capsys.readouterr().out.strip() == "off"
    m.main(["predict.py", "resolve", "migrate the db", _sdlc(tmp_path, "auto")])
    assert capsys.readouterr().out.strip() == "opus"


def test_orchestrators_wire_model_selection():
    """The loop must call the resolver + dispatch a subagent; goal must surface it — so a refactor
    can't silently drop the wiring."""
    sk = pathlib.Path(__file__).resolve().parent.parent / "skills"
    loop = (sk / "sigma-loop" / "SKILL.md").read_text()
    goal = (sk / "sigma-goal" / "SKILL.md").read_text()
    assert "predict.py" in loop and "resolve" in loop and "subagent" in loop
    assert "predict.py" in goal and "model_selection" in goal


# --- effort axis + per-step resolution (0.6) ---

def test_effort_axis_low_medium_high():
    m = _mod()
    assert m.predict_effort("run the tests for the slice") == "low"
    assert m.predict_effort("watch the job status and poll") == "low"
    assert m.predict_effort("debug the race condition in the scheduler") == "high"
    assert m.predict_effort("add a retry to the http client") == "medium"


# --- issue #1627's second claimed blind spot: "full" breaks the low-effort adjacency rule --------
# REVISED DECISION, recorded here because the first pass got this backwards. `_EFFORT_PATTERNS`'
# low rule is `run (the )?tests?`, which requires "the" to sit IMMEDIATELY before "test"/"tests"
# with no wildcard between them; the word "full" sits exactly there and prevents a match, so
# "Run the full test suite and confirm green" fell through to the medium default. An earlier draft
# of this fix read the issue's own "-> effort medium, not low" as documenting that CORRECT,
# already-existing behavior and shipped only a pinning test -- technically accurate about what
# unpatched code returns, but wrong about which side of medium/low the issue actually wants: a
# fresh, independent code-review re-read "which the word 'full' breaks" as naming a DEFECT (the
# rule failing to recognize a full-suite run as the same mechanical, low-effort action a partial
# run already is), not a documented feature to preserve -- and the issue's own Definition of Done
# ("each test is seen RED against the current pattern before the fix lands") is literally
# unsatisfiable under the "already correct" reading, which is itself evidence that reading was
# wrong. This is also the more principled answer under this file's OWN documented effort-axis
# philosophy (see `_EFFORT_PATTERNS`' module comment above): effort measures REASONING difficulty,
# not scope -- running the FULL suite requires no more reasoning than running a subset, the same
# way a one-line payment fix is opus-tier but still only medium effort. Fixed by widening the
# adjacency to admit one more optional word, mirroring the issue's own named culprit exactly.


def test_full_test_suite_phrasing_reaches_low_effort():
    m = _mod()
    assert m.predict_effort("Run the full test suite and confirm green") == "low"


def test_the_full_word_widening_does_not_admit_unrelated_full_phrasings():
    """The narrowest widening that admits exactly what the issue names: `(full )?` sits ONLY
    between the optional "the" and "test(s)", so it does not turn every sentence merely containing
    "full" into a mechanical low-effort signal."""
    m = _mod()
    for g in ("run a full audit of the test coverage", "the full test suite passed overnight",
              "fully test the new endpoint", "a full rewrite of the retry logic"):
        assert m.predict_effort(g) == "medium", g


def test_resolve_step_gated_by_config(tmp_path):
    import json
    m = _mod()
    base = tmp_path / ".sdlc"; base.mkdir()
    base.joinpath("config.json").write_text(json.dumps({"model_selection": "off"}))
    assert m.resolve_step("run the tests", str(base)) is None          # off → None
    base.joinpath("config.json").write_text(json.dumps({"model_selection": "auto"}))
    pair = m.resolve_step("run the tests", str(base))
    assert pair == {"model": "sonnet", "effort": "low"}


def test_resolve_cli_output_stays_backward_compatible(tmp_path, capsys):
    import json
    m = _mod()
    base = tmp_path / ".sdlc"; base.mkdir()
    base.joinpath("config.json").write_text(json.dumps({"model_selection": "auto"}))
    assert m.main(["predict.py", "resolve", "fix a typo", str(base)]) == 0
    assert capsys.readouterr().out.strip() == "haiku"                  # bare tier, no pair
    assert m.main(["predict.py", "resolve-step", "fix a typo", str(base)]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "model=haiku effort=low"


def test_resolve_cli_github_text_uses_separate_issue_id_for_ledger(tmp_path, capsys, monkeypatch):
    m = _mod()
    calls = []
    monkeypatch.setattr(m.subprocess, "run", _capture_run(calls))
    base = _sdlc(tmp_path, "auto")
    assert m.main(["predict.py", "resolve", "migrate the database schema", base, "2514"]) == 0
    assert capsys.readouterr().out.strip() == "opus"
    assert len(calls) == 1
    argv, _ = calls[0]
    assert argv[2:6] == ["emit", base, "2514", "model_choice"]


# --- #543: security routing across the in- prefix, on BOTH axes ---------------------------------
# `\b(...|secure|...)` cannot match inside "insecure": `n` and `s` are both word characters, so
# there is no boundary between them. The most common way a security goal is actually phrased —
# naming the DEFECT rather than the property — therefore missed the opus tier entirely. Separately,
# `_EFFORT_PATTERNS` carried `securit` but not `secure`, so even a goal that DID reach opus could
# come back at medium effort: an internal inconsistency between the two lists, independent of the
# boundary question. Both directions contradict the module's own stated bias that over-powering is
# cheaper than under-powering.


def test_insecure_phrasing_routes_like_security_phrasing():
    p = _mod().predict
    for g in ("Fix the insecure default in the token store",
              "Fix the insecurity in the session handler",
              "Harden the security of the token store",
              "Make the token store secure by default"):
        assert p(g) == "opus", g


def test_security_phrasings_all_get_high_effort():
    """The two lists have to agree: a goal that reaches opus on the model axis must not come back
    at medium on the effort axis just because it said "secure" instead of "security"."""
    effort = _mod().predict_effort
    for g in ("Fix the insecure default in the token store",
              "Fix the insecurity in the session handler",
              "Harden the security of the token store",
              "Make the token store secure by default"):
        assert effort(g) == "high", g


def test_the_in_prefix_widening_stays_anchored_at_a_word_boundary():
    """Widening to `(in)?secur` admits exactly ONE prefix, not any prefix: "resecuring" still has no
    word boundary before its "secur", so it must not fire. Same discipline the revision/provision
    pin above enforces for the fable tier — asserted on BOTH axes, since both lists changed."""
    m = _mod()
    for g in ("schedule the resecuring of the vault", "update the revision history",
              "improve the provision logic"):
        assert m.predict(g) == "sonnet", g
        assert m.predict_effort(g) == "medium", g


# --- #575: the same boundary blindness, for the sibling terms ------------------------------------
# #543 fixed `secur` and stopped there. `\b` anchors the whole alternation, so EVERY term is blind
# to a prefixed form: "unauthorized"/"unauthenticated"/"unsecured" name a security DEFECT and missed
# opus entirely, and the same shape hits non-security terms ("Rearchitect", "Remigrate",
# "unscalable"). Hyphenating restores the boundary, which is why "Re-architect" already worked and
# "Rearchitect" did not — a routing difference nobody would predict from the goal text.
#
# The prefix is admitted PER TERM, never globally, because `un-` does not mean one thing. On a
# security term it names a defect to fix (harder work, correctly opus); on a capability term it can
# name the mere ABSENCE of the property — "uncomplex" is not a complexity goal at all. Each widening
# below was dictionary-swept (/usr/share/dict/words) for what it newly admits; see the PR body.


def test_prefixed_security_defect_phrasings_reach_opus_and_high():
    """The phrasing an engineer actually files: name the defect, not the property."""
    m = _mod()
    for g in ("Fix the unauthorized access bug",
              "Reject unauthenticated requests",
              "Fix the unsecured S3 bucket"):
        assert m.predict(g) == "opus", g
        assert m.predict_effort(g) == "high", g


def test_prefixed_non_security_terms_reach_their_own_tier():
    """Same mechanism, outside security. `re-` on architect/migrat is "do it again", `un-` on
    scalab is "it does not scale" — all three are the hard work the term exists to catch."""
    m = _mod()
    for g in ("Rearchitect the ingestion pipeline",
              "Remigrate the legacy tables",
              "The current design is unscalable"):
        assert m.predict(g) == "opus", g


def test_the_hyphenated_spelling_still_routes_the_same_way():
    """"Re-architect" already worked (the hyphen is a word boundary) and must keep working — the fix
    removes a difference between two spellings of one word, it does not trade one for the other."""
    m = _mod()
    for g in ("Re-architect the ingestion pipeline", "Rearchitect the ingestion pipeline"):
        assert m.predict(g) == "opus", g


def test_the_prefix_widening_does_not_admit_meaning_inverting_forms():
    """The guard that makes 'per term' real: `uncomplex` is the ABSENCE of complexity, not a
    complexity goal, so `complex` keeps its bare boundary and this stays at the default tier."""
    m = _mod()
    for g in ("the module is uncomplex and easy to follow", "update the revision history"):
        assert m.predict(g) == "sonnet", g
        assert m.predict_effort(g) == "medium", g


# --- #575: the security cluster is kept aligned across BOTH lists --------------------------------
# `_EFFORT_PATTERNS` carried `(?:in)?secur` but not its siblings, so "Fix the authorization bug"
# came back opus/MEDIUM — the same hardest-tier-middle-effort disagreement #543 named for `secure`,
# one term wider. `authoriz`, `authenticat` and `crypto` name the same domain as `secur`; a goal in
# that domain reaching opus at medium effort is a disagreement between two lists, not a distinction.


def test_the_whole_security_cluster_agrees_across_both_axes():
    m = _mod()
    for g in ("Fix the authorization bug in the API",
              "Fix the authentication bypass in the login flow",
              "Rotate the crypto keys used for session tokens",
              "Fix the unauthorized access bug"):
        assert m.predict(g) == "opus", g
        assert m.predict_effort(g) == "high", g


# --- issue #1627: bare `auth`/`authn`/`authz` were a blind spot the same shape as #575's own -----
# `_PATTERNS` already matched `authenticat` and `(?:un)?authori[sz]`, but named the DEFECT'S full
# word, never the bare abbreviation an engineer actually types when filing "the auth token refresh
# path" or "authn"/"authz" shorthand -- so these fell through to the unsignalled sonnet default,
# the same class of miss #543/#575 already fixed for the un-/re- prefix forms. `\bauth[nz]?\b` (a
# TRAILING boundary scoped to only this one alternative -- every sibling term in both lists keeps
# its existing no-trailing-boundary shape) is the narrowest widening that admits exactly
# auth/authn/authz: dictionary-swept against /usr/share/dict/words (US-English), it admits ZERO
# real words. Without the trailing boundary, bare `auth` would match as a PREFIX inside
# author/authoring/authored/authoritative/authigenic -- none of them security-adjacent -- which is
# exactly why the negative-control test below exists alongside the positive one, per this file's
# own established dictionary-sweep convention. Kept in lockstep across both lists (aligned with
# `authenticat`/`authori[sz]`), matching #575's own "security cluster is the exception, kept
# aligned across both lists" policy.


def test_bare_auth_authn_authz_reach_the_security_cluster():
    m = _mod()
    for g in ("Refactor the auth token refresh path", "fix authn bug", "review authz policy",
              "AUTH header missing"):
        assert m.predict(g) == "opus", g
        assert m.predict_effort(g) == "high", g


def test_predict_with_reason_reports_the_bare_auth_signal():
    m = _mod()
    assert m.predict_with_reason("Refactor the auth token refresh path") == ("opus", "auth")
    assert m.predict_with_reason("fix authn bug") == ("opus", "authn")
    assert m.predict_with_reason("review authz policy") == ("opus", "authz")


def test_bare_auth_does_not_admit_author_or_authoring():
    """The dictionary-sweep negative control this file's own convention requires alongside every
    widening (see #575's own comment block) -- without the trailing `\\b`, a regression that makes
    `auth` match unboundedly as a bare prefix would ship silently and misroute every goal that
    merely mentions an author, authorship, or authoring something."""
    m = _mod()
    for g in ("the author of this file", "authoring the doc", "authored by someone",
              "authoritative source for this claim", "an authentic reproduction",
              "authigenic minerals in the sediment"):
        assert m.predict(g) == "sonnet", g
        assert m.predict_effort(g) == "medium", g


def test_authorization_flow_still_routes_via_the_existing_authoriz_signal():
    """Proves the new bare-auth alternative doesn't shadow or reorder the pre-existing
    `(?:un)?authori[sz]` signal it now sits next to in the alternation."""
    m = _mod()
    assert m.predict_with_reason("authorization flow") == ("opus", "authoriz")


def _alternation_terms(pattern_string):
    """Extract the top-level (paren-depth-aware, bracket-class-aware) `|`-separated terms from one
    of predict.py's own `\\b(...)` pattern strings, as a set.

    Depth-aware because the security cluster's own `(?:in|un)?secur` has an internal `|` that a
    naive `.split("|")` would fragment into two garbage tokens — those two fragments happen to
    appear identically in both `_PATTERNS` and `_EFFORT_PATTERNS` today, so a naive split's set
    DIFFERENCE looks right by coincidence; this stays correct even once that coincidence stops
    holding.

    Class-aware (#605 fold) because this PR introduced the FIRST character class into the
    alternations (`authori[sz]`): inside `[...]`, `|` and `(`/`)` are literal characters, not
    alternation/grouping syntax — so a future widening that puts either inside a class (a typo, or
    a deliberate `[()]`-shaped class) would otherwise silently fragment that ONE term into garbage
    pieces, corrupting the very set-parity check this helper exists to guard. A backslash escapes
    whatever character follows it, inside or outside a class, so that character is never itself
    read as `(`/`)`/`|`/`[`/`]` either.

    Mirrors test_risk_detect.py's own `_secret_kv_pattern`/`_secret_token_pattern`
    alternation-extraction precedent, adapted for two Python-importable pattern strings (predict.py's
    `_PATTERNS`/`_EFFORT_PATTERNS`) rather than two shell scripts' source text."""
    s = pattern_string.strip()
    assert s.startswith(r"\b(") and s.endswith(")"), s
    inner = s[len(r"\b("):-1]
    terms, depth, in_class, current, i = [], 0, False, "", 0
    while i < len(inner):
        ch = inner[i]
        if ch == "\\" and i + 1 < len(inner):    # an escaped char is never special, in or out of a class
            current += inner[i:i + 2]
            i += 2
            continue
        if in_class:
            current += ch
            if ch == "]":
                in_class = False
        elif ch == "[":
            in_class = True
            current += ch
        elif ch == "(":
            depth += 1
            current += ch
        elif ch == ")":
            depth -= 1
            current += ch
        elif ch == "|" and depth == 0:
            terms.append(current)
            current = ""
        else:
            current += ch
        i += 1
    terms.append(current)
    return set(terms)


def test_alternation_terms_treats_a_pipe_inside_a_character_class_as_literal():
    """#605 fold: probes `_alternation_terms` directly against a SYNTHETIC pattern (not predict.py's
    real one), so this stays meaningful even though predict.py itself never happens to need a `|`
    inside a class today. Without bracket-class tracking, `fo[o|0]bar|plain` would fragment into
    THREE terms (the `|` inside `[o|0]` misread as a top-level alternation separator) instead of the
    correct two."""
    assert _alternation_terms(r"\b(fo[o|0]bar|plain)") == {"fo[o|0]bar", "plain"}


def test_the_effort_list_is_a_deliberate_subset_not_a_stale_copy():
    """#575's design decision, pinned so it cannot rot into an accident.

    The two axes answer different questions — the model tier asks how much CAPABILITY the domain
    demands, the effort tier how much REASONING the work demands — so the lists diverge in BOTH
    directions on purpose. If a future change makes them identical, or drops either direction, this
    fails and the decision gets re-made deliberately instead of drifting.

    #595: pinned by SET, not by SAMPLE. A sample pin (three example strings) let
    financial/multi-service/complex/performance/scaling drift into the effort-high list — or
    payment/(?:un)?scalab drift out of the model-only side — with CI green, because only
    payment/(?:un)?scalab/debug happened to be the ones sampled. The two set-differences below are
    asserted against the FULL documented lists, so any single term moving either direction fails
    here regardless of which term it is."""
    m = _mod()
    # index 0 of each list is assumed to be the opus/high entry throughout this file; guard that
    # assumption explicitly so a future reordering fails here with a clear message, not a silent
    # wrong-tier comparison below.
    assert m._PATTERNS[0][0] == "opus"
    assert m._EFFORT_PATTERNS[0][0] == "high"
    opus_terms = _alternation_terms(m._PATTERNS[0][1])
    effort_high_terms = _alternation_terms(m._EFFORT_PATTERNS[0][1])

    # opus WITHOUT high effort: high stakes if wrong, but not necessarily hard to reason about —
    # the full documented list (reviewer-verified), not a sample of it.
    assert opus_terms - effort_high_terms == {
        "(?:un)?scalab", "complex", "financial", "multi-service", "payment", "performance",
        "scaling"}
    # high effort WITHOUT opus: following a fault through a system is deep reasoning, not
    # specialist capability — the proof this list was never merely copied.
    assert effort_high_terms - opus_terms == {"debug", "diagnos", "root.?cause"}

    # one concrete example per direction, so the policy stays legible at a glance — the set
    # assertions above are what actually PROTECTS it from drift, these are just illustration.
    assert m.predict("debug the flaky checkout test") == "sonnet"
    assert m.predict_effort("debug the flaky checkout test") == "high"
    assert m.predict("add payment processing") == "opus"
    assert m.predict_effort("add payment processing") == "medium"


# --- #595 item 2: the capability-vs-reasoning split, pinned at its most awkward pair -------------
# "diagnose the latency regression" and "fix the performance regression" name the SAME underlying
# problem, phrased two ways, and land at opposite ends of both axes: `diagnos` is effort-high but
# not opus (sonnet/high); `performance` is opus but not effort-high (opus/medium). This is not a
# bug — it is the documented policy (_EFFORT_PATTERNS' own module comment) applied consistently:
# MODEL asks how much capability the DOMAIN demands ("performance" raises the cost of getting it
# wrong), EFFORT asks how much reasoning the WORK demands ("diagnos" names the deep-reasoning verb,
# a one-line perf fix is still one line). The two phrasings simply trigger different words on
# different axes. Decision (proposed in the PR body, reviewer adjudicates): DOCUMENT this pair
# explicitly rather than move the performance/complex/scaling cluster — moving it would touch load-
# bearing, already-deliberate categorization on a single contrived example, not new evidence the
# categorization is wrong for real goals. Pinned so the decision is checkable, not just a comment.


def test_the_diagnose_vs_fix_performance_pair_is_the_documented_divergence_not_a_bug():
    m = _mod()
    assert m.predict("diagnose the latency regression") == "sonnet"
    assert m.predict_effort("diagnose the latency regression") == "high"
    assert m.predict("fix the performance regression") == "opus"
    assert m.predict_effort("fix the performance regression") == "medium"


# --- #595 item 3: HTTP-401 prose escalates three tiers under the upward-resolution bias ----------
# "fix the 401 unauthorized response formatting" is mundane string-formatting work by intent, but
# `(?:un)?authoriz` fires (opus/high) ahead of `formatting` (haiku/low) — `_PATTERNS` is ordered
# high to low and the first match wins, so the security signal always outranks the formatting one
# in the SAME string. This is the module's own stated bias working as designed ("Conflicts resolve
# UPWARD... over-powering a mislabelled goal is cheaper than under-powering a hard one") — a goal
# that says "unauthorized" for ANY reason is treated as authorization-adjacent, not assumed benign.
# Noted for awareness, not changed: a code comment is enough here, pinned below so the decision
# reads as deliberate the next time someone re-derives it from scratch.


def test_401_formatting_phrasing_still_resolves_upward_to_opus_by_design():
    m = _mod()
    assert m.predict("fix the 401 unauthorized response formatting") == "opus"
    assert m.predict_effort("fix the 401 unauthorized response formatting") == "high"


# --- #595 item 4: two residual same-class gaps, each dictionary-swept like #575's own widenings --
# `/usr/share/dict/words` (US-English) has no `authoris*` entries at all beyond two obscure/archaic
# ones ("authorish", "authorism") — neither is the intended British spelling, but neither is an
# unrelated word either, so widening introduces no real noise. `authoriz` -> `authori[sz]` covers
# both spellings uniformly and is kept in lockstep across BOTH lists, same as every other security-
# cluster term (see the #575 section above). `migrat` swept similarly: `grep -iE "^[a-z]*migrat"`
# turns up only on-topic prefixed forms (emigrate/immigrate/transmigrate/... — a DIFFERENT verb,
# correctly still excluded since none start with "re"/"un") plus "unmigrating", confirming `un-` on
# `migrat` names a defect (not yet migrated, work still needed) the same way it does on the
# security cluster, not the mere-absence-of-a-property shape `complex` deliberately keeps bare.


def test_the_british_ise_spelling_of_authorized_reaches_the_security_cluster():
    m = _mod()
    for g in ("Fix the authorised access issue", "Review the authorisation flow"):
        assert m.predict(g) == "opus", g
        assert m.predict_effort(g) == "high", g


def test_unmigrated_reaches_opus_like_its_re_prefixed_sibling():
    m = _mod()
    assert m.predict("The unmigrated tables still use the old schema") == "opus"
    assert m.predict_effort("The unmigrated tables still use the old schema") == "high"


# --- issue #1627 finding 1 (plan-review): `_read()` crashes on any real goal's worth of text ------
# `_read(arg)` calls `pathlib.Path(arg).exists()` to decide whether `arg` is a file path to expand
# or plain text to classify as-is. On this OS, `Path.exists()` raises `OSError: [Errno 63] File
# name too long` (ENAMETOOLONG) once a path CANDIDATE exceeds roughly 255 bytes for a single
# component -- `Path.exists()`'s own `_ignore_error` helper only swallows ENOENT/ENOTDIR/EBADF/
# ELOOP, not ENAMETOOLONG, so the OSError propagates uncaught. Every short phrase this file's other
# tests use ("migrate the schema", "run the tests") stays well under that, so the crash was
# invisible until a caller hands `_read` a REAL goal's worth of prose -- exactly what an autonomous,
# code-driven pick-time caller does (issue #1627's own Task 2), and exactly what the real fetched
# title+body of issue #1627 itself is (2717 characters once the body markdown is included) --
# reproduced live, independently, against that real text before this test was written. Any CLI verb
# that calls `_read` (`resolve`, `resolve-step`, `why`, the bare verb) shares this exact crash.


def test_read_does_not_crash_on_text_longer_than_a_filename(tmp_path):
    m = _mod()
    # A real multi-sentence prose shape, not a short substitute that would pass without ever
    # exercising the bug -- and deliberately SLASH-FREE. `Path.exists()` only raises ENAMETOOLONG
    # when a single path COMPONENT (the text between `/` separators, or the whole string when it
    # has none) exceeds the OS limit; a text containing `/` (e.g. a real issue body mentioning
    # `.sdlc/config.json`) can split into several short components that each individually stay
    # under the limit even past 480 total characters -- confirmed directly: that shape does NOT
    # reproduce the crash, which is exactly why this fixture avoids it, to pin the bug
    # deterministically rather than by accident of where slashes happen to fall.
    long_text = (
        "Nothing invokes predict dot py, so model selection is decorative. Model selection is "
        "auto in the loop config and ledger enabled is true. Despite that, almost nothing calls "
        "the resolver anywhere in the real executed loop -- migrate the schema, rearchitect the "
        "pipeline, fix the authorization bug, run the full test suite and confirm green, again "
        "and again until this string is unambiguously longer than any real filesystem path "
        "component could be, with no slash anywhere in it at all."
    )
    assert len(long_text) > 255 and "/" not in long_text
    base = _sdlc(tmp_path, "auto")
    assert m.main(["predict.py", "why", long_text, base]) == 0
    assert m.main(["predict.py", "resolve", long_text, base]) == 0


# --- issue #1030: resolve/resolve_step record the choice to the ledger, code-driven, fail-open ---
#
# `goal=` is a NEW, optional keyword parameter on both `resolve` and `resolve_step` -- every test
# above this section calls them with the SAME positional args they always have (never `goal=`), so
# every one of them exercises the `goal is None` branch below and attempts ZERO subprocess calls,
# which is the strongest possible form of "fail open" for that fixture: nothing is even tried.
#
# `_capture_run` (not a raising double) proves that: it records every call it receives with no
# side effect of its own, so `assert calls == []`, checked in the test's own body outside any
# exception handler, fails loudly if `_emit_model_choice` is ever reached. A raising double was
# tried first and rejected on review -- `_emit_model_choice`'s own `except Exception: pass` fail-
# open contract (AssertionError IS an Exception) would swallow a raise from INSIDE that function
# exactly as it swallows a real failure, so a raising double proves nothing about whether the call
# was skipped versus attempted-and-swallowed. Recording, not raising, is what survives that.


def _capture_run(calls, returncode=0):
    def _fake(argv, **kwargs):
        calls.append((list(argv), kwargs))
        class _Result:
            pass
        r = _Result(); r.returncode = returncode; r.stdout = ""; r.stderr = ""
        return r
    return _fake


def test_resolve_without_goal_never_attempts_a_subprocess_call(tmp_path, monkeypatch):
    m = _mod()
    calls = []
    monkeypatch.setattr(m.subprocess, "run", _capture_run(calls))
    assert m.resolve("migrate the schema", _sdlc(tmp_path, "auto")) == "opus"
    assert calls == []


def test_resolve_step_without_goal_never_attempts_a_subprocess_call(tmp_path, monkeypatch):
    m = _mod()
    calls = []
    monkeypatch.setattr(m.subprocess, "run", _capture_run(calls))
    base = tmp_path / ".sdlc"; base.mkdir()
    base.joinpath("config.json").write_text('{"model_selection": "auto"}')
    assert m.resolve_step("run the tests", str(base)) == {"model": "sonnet", "effort": "low"}
    assert calls == []


def test_resolve_off_with_a_goal_still_never_attempts_a_subprocess_call(tmp_path, monkeypatch):
    """Even WITH a goal id, model_selection:off must skip the emit attempt too -- there is no real
    'choice' to record when the tier being returned is None (not actually going to run anything)."""
    m = _mod()
    calls = []
    monkeypatch.setattr(m.subprocess, "run", _capture_run(calls))
    assert m.resolve("migrate the schema", _sdlc(tmp_path, "off"), goal="42") is None
    assert calls == []


def test_resolve_with_a_goal_emits_model_choice_with_the_signal(tmp_path, monkeypatch):
    m = _mod()
    calls = []
    monkeypatch.setattr(m.subprocess, "run", _capture_run(calls))
    sdlc = _sdlc(tmp_path, "auto")
    tier = m.resolve("migrate the schema", sdlc, goal="42")
    assert tier == "opus"
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[0] == m.sys.executable
    assert argv[1].endswith(str(m.pathlib.Path("sigma-loop") / "scripts" / "loop.py"))
    assert m.pathlib.Path(argv[1]).name == "loop.py"
    assert argv[2:6] == ["emit", sdlc, "42", "model_choice"]
    assert "--model" in argv and argv[argv.index("--model") + 1] == "opus"
    assert "--signal" in argv and argv[argv.index("--signal") + 1] == "migrat"
    assert kwargs.get("timeout")   # a bounded wait, never an indefinite hang


def test_resolve_with_a_goal_and_no_signal_omits_the_signal_flag(tmp_path, monkeypatch):
    """A defaulted tier (no pattern matched) correctly carries no signal -- omitted, not an empty
    string, matching the skill's own documented convention ('pass it empty or omit it')."""
    m = _mod()
    calls = []
    monkeypatch.setattr(m.subprocess, "run", _capture_run(calls))
    sdlc = _sdlc(tmp_path, "auto")
    tier = m.resolve("add a retry to the http client", sdlc, goal="43")
    assert tier == "sonnet"
    argv, _ = calls[0]
    assert "--signal" not in argv
    assert argv[2:6] == ["emit", sdlc, "43", "model_choice"]


def test_resolve_step_with_a_goal_emits_but_return_shape_is_unchanged(tmp_path, monkeypatch):
    """The dict-equality hazard named in the plan: adding the emit call must not smuggle a third
    `signal` key into resolve_step's own return value -- test_resolve_step_gated_by_config (above,
    unmodified) asserts exact dict equality, so this would break it silently otherwise."""
    m = _mod()
    calls = []
    monkeypatch.setattr(m.subprocess, "run", _capture_run(calls))
    sdlc = _sdlc(tmp_path, "auto")
    pair = m.resolve_step("run the tests for the slice", sdlc, goal="42")
    assert pair == {"model": "sonnet", "effort": "low"}
    assert len(calls) == 1
    argv, _ = calls[0]
    assert argv[2:6] == ["emit", sdlc, "42", "model_choice"]
    assert "--model" in argv and argv[argv.index("--model") + 1] == "sonnet"
    assert "--signal" not in argv    # "run the tests for the slice" matches no _PATTERNS signal


def test_emit_helper_swallows_a_subprocess_exception(tmp_path, monkeypatch):
    """The fail-open contract's other half: not just 'no ledger configured' (loop.py's own OSError
    path handles that internally, inside the child process) but the SUBPROCESS INVOCATION ITSELF
    failing (a missing interpreter, a moved loop.py, a timeout) must not raise past resolve()."""
    m = _mod()
    def _raise(*_a, **_k):
        raise FileNotFoundError("python3 not found")
    monkeypatch.setattr(m.subprocess, "run", _raise)
    sdlc = _sdlc(tmp_path, "auto")
    assert m.resolve("migrate the schema", sdlc, goal="42") == "opus"
    assert m.resolve_step("run the tests", sdlc, goal="42") == {"model": "sonnet", "effort": "low"}


def test_emit_helper_swallows_a_timeout(tmp_path, monkeypatch):
    m = _mod()
    def _timeout(*_a, **_k):
        raise m.subprocess.TimeoutExpired(cmd="loop.py", timeout=10)
    monkeypatch.setattr(m.subprocess, "run", _timeout)
    sdlc = _sdlc(tmp_path, "auto")
    assert m.resolve("migrate the schema", sdlc, goal="42") == "opus"


def test_resolve_cli_verb_reuses_argv2_as_the_goal_identifier(tmp_path, monkeypatch):
    """main()'s own `resolve` dispatch must pass the ORIGINAL (pre-_read) argv[2] as the goal id --
    the one place both the raw identifier and its derived text are both still available. Zero
    SKILL.md prose change is needed for the goal-level call (sigma-loop's and sigma-goal's own
    `predict.py resolve "$goal" .sdlc` lines are unchanged) BECAUSE main() does this internally."""
    m = _mod()
    calls = []
    monkeypatch.setattr(m.subprocess, "run", _capture_run(calls))
    sdlc = _sdlc(tmp_path, "auto")
    assert m.main(["predict.py", "resolve", "migrate the schema", sdlc]) == 0
    assert len(calls) == 1
    argv, _ = calls[0]
    assert argv[2:6] == ["emit", sdlc, "migrate the schema", "model_choice"]


def test_resolve_step_cli_verb_needs_the_new_trailing_goal_argument(tmp_path, monkeypatch):
    """Unlike `resolve`, step text can never double as a goal id -- the CLI verb needs a genuinely
    NEW 4th positional argument, and this proves both halves: present -> emits with that id; absent
    -> the call is skipped exactly like every pre-#1030 invocation (backward compatible)."""
    m = _mod()
    calls = []
    monkeypatch.setattr(m.subprocess, "run", _capture_run(calls))
    sdlc = _sdlc(tmp_path, "auto")
    assert m.main(["predict.py", "resolve-step", "run the tests", sdlc, "77"]) == 0
    assert len(calls) == 1
    argv, _ = calls[0]
    assert argv[2:6] == ["emit", sdlc, "77", "model_choice"]

    calls.clear()
    assert m.main(["predict.py", "resolve-step", "run the tests", sdlc]) == 0   # no 4th arg
    assert calls == []


def test_why_never_calls_subprocess_run_at_all():
    """`why` stays pure -- explicitly documented as writing nothing, so an exploratory call has no
    side effect. Proven, not just read: swap `subprocess` itself for a call-recording stand-in and
    confirm the CLI verb never reaches it (main()'s `why` branch calls `predict_with_reason`
    directly, never `resolve`/`resolve_step`/the new emit helper)."""
    m = _mod()
    calls = []

    class _Recorder:
        def run(self, *a, **k):
            calls.append((a, k))

    orig = m.subprocess
    try:
        m.subprocess = _Recorder()
        assert m.main(["predict.py", "why", "migrate the schema"]) == 0
    finally:
        m.subprocess = orig
    assert calls == []


# --- #1601: per-repo signal excludes -------------------------------------------------------------
# The fable pattern's terms are creative signals IN GENERAL but ordinary domain nouns on specific
# repos: a storytelling pipeline has a "narrative stage" and a "prose renderer", and `vision`
# collides with Sigma's OWN vision-first vocabulary (`/sigma-vision`, the north-star doc), so
# "Align the retry logic with the north-star vision doc" routed to the creative tier. #350 fixed the
# same class by NARROWING the global pattern (bare `story` dropped, `storytell` kept); that remedy
# cannot generalize here, because `narrativ`/`prose`/`vision` are genuinely creative on most repos
# and genuinely mechanical on a few — no single global pattern is right for everyone, so the
# per-repo axis is the only one left. Config, not code.

def _sdlc_cfg(tmp_path, cfg, name=".sdlc"):
    """A .sdlc dir carrying exactly `cfg`. Distinct from `_sdlc` above (which only ever writes the
    one gate key) and parameterized by `name` so one test can hold two different configs."""
    import json
    base = tmp_path / name
    base.mkdir(exist_ok=True)
    (base / "config.json").write_text(json.dumps(cfg))
    return str(base)


def _legacy_predict_with_reason(m, goal_text):
    """`predict_with_reason` EXACTLY as it stood before #1601 — one `re.search` per tier, first hit
    wins, no exclusion step. The reference the no-config default is proved byte-identical against,
    so "nothing changed for a repo that configures nothing" is measured rather than asserted."""
    import re
    t = (goal_text or "").lower()
    for tier, pat in m._PATTERNS:
        hit = re.search(pat, t)
        if hit:
            return tier, hit.group(0)
    return m._DEFAULT, None


_REGEX_LEFTOVER = __import__("re").compile(r"[()\[\]?*+|\\.]")


def _literal_for(term):
    """Turn one alternation term from `_PATTERNS` into a plain string that MATCHES it, so the
    byte-identity corpus below is generated from the router's real vocabulary instead of a
    hand-copied list that can drift out of date. Handles exactly the regex constructs those
    patterns use today, and asserts nothing regex-shaped survives — a future widening that
    introduces a NEW construct fails loudly here instead of silently generating a probe that
    matches nothing (which would make every assertion below vacuously true)."""
    import re
    lit = re.sub(r"\((?:\?:)?[^)]*\)\?", "", term)     # optional prefix group -> the bare form
    lit = re.sub(r"\[([a-z])[a-z]*\]", r"\1", lit)     # character class -> its first member
    lit = lit.replace(".?", " ")                       # `root.?cause` -> "root cause"
    lit = re.sub(r"([a-z-])\?", r"\1", lit)            # trailing optional char -> keep it
    # #1627: a term-scoped TRAILING `\b` (only `auth[nz]?\b` carries one today) marks a real word
    # boundary, not a literal character — dropping it is correct for the PROBE text specifically,
    # since the probe is always embedded as `f"handle the {lit} case"` (see `_vocabulary_probes`),
    # so real word boundaries (the surrounding spaces) already exist at both ends without it.
    lit = re.sub(r"\\b$", "", lit)                     # trailing word-boundary marker -> dropped
    # #1780 introduced the FIRST negative lookahead in either list (`prose(?!-)`, so the term does
    # not fire immediately before a hyphenated compound of this repo's own SDLC jargon). Dropping it
    # is correct for the probe specifically the same way dropping the trailing `\b` already is: the
    # probe text (`f"handle the {lit} case"`) never puts a hyphen right after the literal, so the
    # assertion the lookahead makes is trivially satisfied there regardless of whether it survives
    # in the probe string — stripping it just keeps a real hyphen construct from tripping the
    # regex-leftover guard below.
    lit = re.sub(r"\(\?!-\)$", "", lit)                # trailing negative lookahead -> dropped
    assert not _REGEX_LEFTOVER.search(lit), f"unhandled regex construct in {term!r} -> {lit!r}"
    return lit


def _vocabulary_probes(m):
    """`[(tier, literal, goal_text)]` — one probe per alternation term across every model tier."""
    out = []
    for tier, pat in m._PATTERNS:
        for term in sorted(_alternation_terms(pat)):
            lit = _literal_for(term)
            out.append((tier, lit, f"handle the {lit} case"))
    return out


def test_the_probe_generator_covers_the_whole_router_vocabulary():
    """Guard the guard: an empty or tiny corpus would make every byte-identity assertion below pass
    on nothing. Pinned against the real term count, not a magic number."""
    m = _mod()
    probes = _vocabulary_probes(m)
    assert len(probes) == sum(len(_alternation_terms(p)) for _, p in m._PATTERNS)
    assert len(probes) > 30, len(probes)


def test_no_config_is_byte_identical_to_the_pre_1601_router():
    """THE compatibility pin. Across the router's entire signal vocabulary, a repo that configures
    nothing gets the same SIGNAL LITERAL it got before excludes existed, and the same tier — with
    the one documented exception #2564 introduced: the 8 fable probes now clamp to `_DEFAULT`,
    because fable is priced above opus and the default ceiling is opus. Their signal is unchanged,
    so this pin keeps full per-term discrimination in both cases; only the tier column moves, and
    only for the tier the ceiling names.

    `test_max_tier_fable_restores_the_legacy_router_exactly` carries the byte-identity guarantee
    forward for those 8 under an opted-up ceiling. Checked whether the caller omits the argument,
    passes an empty one, or passes entries that name no signal."""
    m = _mod()
    for tier, lit, goal in _vocabulary_probes(m):
        legacy = _legacy_predict_with_reason(m, goal)
        assert legacy == (tier, lit), goal                  # the probe really does fire its own term
        capped = (m._DEFAULT, lit) if tier == "fable" else legacy      # #2564: tier only, not signal
        assert m.predict_with_reason(goal) == capped, goal
        assert m.predict_with_reason(goal, ()) == capped, goal
        assert m.predict_with_reason(goal, ("zzzz-names-no-signal",)) == capped, goal
        assert m.predict(goal) == capped[0], goal
    for goal in ("", None, "add a retry to the http client", "fix the typo in the security module",
                 "Story: add pagination", "update the revision history"):
        legacy = _legacy_predict_with_reason(m, goal)       # none of these reach the fable tier
        assert legacy[0] != "fable", goal
        assert m.predict_with_reason(goal) == legacy, goal
        assert m.predict_with_reason(goal, ()) == legacy, goal


def test_the_reported_defect_still_reproduces_with_no_config():
    """The two reproductions from the issue, pinned as the UNCONFIGURED behaviour — deliberately
    left intact, because on most repos `narrativ`/`vision` really are creative signals. A repo opts
    out; the global default does not move."""
    m = _mod()
    assert m.predict_with_reason(
        "Wire the budget flowing format to narrative to screenplay to manifest",
        max_tier="fable") == ("fable", "narrativ")
    assert m.predict_with_reason(
        "Align the retry logic with the north-star vision doc",
        max_tier="fable") == ("fable", "vision")
    # #2564: with the DEFAULT ceiling the tier clamps but the signal is kept, so the literal this
    # issue's own opt-out workflow tells a repo to copy out of `why` is still recoverable.
    assert m.predict_with_reason(
        "Align the retry logic with the north-star vision doc") == ("sonnet", "vision")


def test_an_excluded_signal_falls_through_to_the_default():
    """The fix, at the API. The goal's ONLY creative signal is the repo's own domain noun, so with
    it excluded the goal resolves exactly as if the word were absent — which the issue measured as
    sonnet by deleting the word by hand."""
    m = _mod()
    assert m.predict_with_reason(
        "Wire the budget flowing format to narrative to screenplay to manifest",
        ("narrativ",)) == ("sonnet", None)
    assert m.predict_with_reason(
        "Align the retry logic with the north-star vision doc", ("vision",)) == ("sonnet", None)


def test_excluding_a_signal_is_not_excluding_the_tier():
    """The over-block guard, and the reason this keys on a SIGNAL rather than on a tier switch: the
    excluded repo can still reach fable through every OTHER creative signal. A "turn fable off"
    knob would make a genuinely creative goal on a narrative-heavy repo unreachable forever."""
    m = _mod()
    for goal in ("write the storytelling for the launch", "draft the launch blog",
                 "write the marketing copy for the release", "polish the tagline"):
        assert m.predict(goal, ("narrativ", "vision", "prose"), "fable") == "fable", goal


def test_an_excluded_signal_falls_through_within_the_same_tier():
    """The subtle one. `re.search` returns the LEFTMOST match, so a goal carrying both an excluded
    signal and a live one would report only the excluded one — and skipping straight to the next
    tier on that basis would drop a genuinely creative goal to sonnet. The scan must continue
    INSIDE the tier first, and the signal returned must be the one that actually survived."""
    m = _mod()
    assert m.predict_with_reason("rewrite the narrative storytelling guide",
                                 ("narrativ",), "fable") == ("fable", "storytell")
    # #2564: the ceiling composes with the exclusion rather than interacting with it -- the scan is
    # untouched, so the SURVIVING signal is still the one reported, only the tier is clamped.
    assert m.predict_with_reason("rewrite the narrative storytelling guide",
                                 ("narrativ",)) == ("sonnet", "storytell")


def test_an_excluded_signal_falls_through_to_a_lower_tier_not_only_the_default():
    """Fall-through means re-entering the ordered scan, not jumping to `_DEFAULT`: with `vision`
    excluded, a mechanical goal that also carries a haiku signal lands on haiku, not sonnet."""
    m = _mod()
    assert m.predict_with_reason("rename the vision renderer", ("vision",)) == ("haiku", "renam")


def test_an_excluded_signal_never_disturbs_a_higher_tier():
    """`_PATTERNS` is ordered high-to-low and exclusion does not reorder it: a goal that reaches
    opus keeps reaching opus whether or not a lower tier's signal is excluded."""
    m = _mod()
    assert m.predict_with_reason("migrate the narrative store", ("narrativ",)) == ("opus", "migrat")
    assert m.predict_with_reason("migrate the narrative store") == ("opus", "migrat")


def test_an_entry_excludes_a_signal_not_one_spelling_of_a_word():
    """WHAT THE EXCLUSION KEYS ON (#1601's central design choice).

    An entry names the router's SIGNAL — the alternation branch — not a word in the goal text and
    not a phrase. Keyed on a word, every morphology of the domain noun would need its own entry and
    the next goal that says "narratives" misroutes again; keyed on a phrase, only that phrase is
    suppressed. Keyed on the signal, the branch is neutralized wherever it fires."""
    m = _mod()
    for goal in ("regenerate the narrative stage", "regenerate the narratives index",
                 "stop narrativizing the log output", "NARRATIVE stage crash"):
        assert m.predict(goal, ("narrativ",)) == "sonnet", goal


def test_an_entry_may_be_written_as_the_router_stem_or_as_the_natural_word():
    """Containment runs in BOTH directions on purpose, because the string a user can write and the
    literal the router matched are rarely the same one.

    `signal ⊆ entry` lets a repo write the natural word ("narrative") without having to know the
    router's internal stem; `entry ⊆ signal` lets ONE stem cover every prefixed form a pattern
    admits, instead of enumerating `authoriz`/`unauthoriz`/`authoris`/`unauthoris` by hand."""
    m = _mod()
    goal = "regenerate the narrative stage"
    assert m.predict(goal, ("narrativ",)) == "sonnet"      # the stem `why` prints
    assert m.predict(goal, ("narrative",)) == "sonnet"     # the natural word
    assert m.predict(goal, ("NarraTive",)) == "sonnet"     # case-insensitive, both sides
    # entry ⊆ signal, across a prefix-admitting pattern: `insecur` is what actually matched.
    assert m.predict_with_reason("harden the insecure endpoint") == ("opus", "insecur")
    assert m.predict("harden the insecure endpoint", ("secur",)) != "opus"


def test_an_empty_entry_never_swallows_the_whole_vocabulary():
    """The failure this normalization exists to prevent: `"" in anything` is True, so an empty or
    whitespace entry — trivially produced by a trailing comma or a blank line in a hand-edited
    config — would exclude EVERY signal and silently flatten the whole router to sonnet."""
    m = _mod()
    for junk in ([""], ["   "], ["", "narrativ"], ["\t\n"]):
        assert m.predict("fix a typo in the README", junk) == "haiku", junk
        assert m.predict("migrate the database schema", junk) == "opus", junk


def test_signal_excludes_reads_the_documented_config_key():
    m = _mod()
    assert m.signal_excludes({"model_selection_signal_excludes": ["Narrativ", " vision "]}) == \
        ("narrativ", "vision")
    assert m.signal_excludes({}) == ()
    assert m.signal_excludes({"model_selection_signal_excludes": []}) == ()


def test_signal_excludes_degrades_to_unconfigured_on_junk_rather_than_raising():
    """Fail-safe direction: a malformed value must leave the router exactly as it was, never raise
    into a caller and never accidentally exclude something. `resolve` is called on every goal the
    loop picks — a config typo cannot be allowed to take the loop down."""
    m = _mod()
    for junk in (None, 7, {"a": 1}, True, [1, 2, None], [[], {}]):
        assert m.signal_excludes({"model_selection_signal_excludes": junk}) == (), junk
    assert m.signal_excludes(None) == ()
    assert m.signal_excludes("not a mapping") == ()
    # a bare string is the obvious mis-write of a one-entry list; honour it instead of silently
    # doing nothing, which is the worst outcome for a knob whose failure mode is invisible.
    assert m.signal_excludes({"model_selection_signal_excludes": "vision"}) == ("vision",)
    # junk MIXED with real entries keeps the real ones rather than discarding the whole list.
    assert m.signal_excludes({"model_selection_signal_excludes": ["vision", 7, None, ""]}) == \
        ("vision",)


def test_resolve_honors_the_repo_excludes(tmp_path):
    m = _mod()
    goal = "Align the retry logic with the north-star vision doc"
    plain = _sdlc_cfg(tmp_path, {"model_selection": "auto", "model_selection_max_tier": "fable"}, ".plain")
    tuned = _sdlc_cfg(tmp_path, {"model_selection": "auto", "model_selection_max_tier": "fable",
                                 "model_selection_signal_excludes": ["vision"]}, ".tuned")
    assert m.resolve(goal, plain) == "fable"
    assert m.resolve(goal, tuned) == "sonnet"


def test_resolve_step_honors_the_repo_excludes(tmp_path):
    """The step axis reads the same key — a plan step is where the domain noun appears most often
    ("render the narrative stage"), and resolve_step's effort half is untouched by exclusion."""
    m = _mod()
    tuned = _sdlc_cfg(tmp_path, {"model_selection": "auto", "model_selection_max_tier": "fable",
                                 "model_selection_signal_excludes": ["narrativ"]}, ".tuned")
    assert m.resolve_step("render the narrative stage", tuned) == {"model": "sonnet",
                                                                   "effort": "medium"}
    plain = _sdlc_cfg(tmp_path, {"model_selection": "auto", "model_selection_max_tier": "fable"}, ".plain")
    assert m.resolve_step("render the narrative stage", plain) == {"model": "fable",
                                                                   "effort": "medium"}


def test_the_recorded_signal_is_the_surviving_one_not_the_excluded_one(tmp_path, monkeypatch):
    """The ledger records WHY a goal ran where it did (#880/#1030). Recording a signal that was
    excluded — or recording one for a tier the exclusion changed — would make the model-tier
    effectiveness metric join on a reason that never applied."""
    m = _mod()
    calls = []
    monkeypatch.setattr(m.subprocess, "run", _capture_run(calls))
    tuned = _sdlc_cfg(tmp_path, {"model_selection": "auto", "model_selection_max_tier": "fable",
                                 "model_selection_signal_excludes": ["narrativ"]}, ".tuned")
    assert m.resolve("rewrite the narrative storytelling guide", tuned, goal="1601") == "fable"
    argv, _ = calls[0]
    assert argv[argv.index("--signal") + 1] == "storytell"

    calls.clear()
    assert m.resolve("Align the retry logic with the north-star vision doc",
                     _sdlc_cfg(tmp_path, {"model_selection": "auto",
                                          "model_selection_signal_excludes": ["vision"]},
                               ".v")) == "sonnet"


def test_why_reports_what_the_loop_would_actually_choose(tmp_path, capsys):
    """`why` is the diagnostic a user runs to check an exclusion took effect, so it must read the
    same config the loop does — a `why` that disagreed with `resolve` would send exactly the person
    already confused looking in the wrong place. Ungated by `model_selection` like the rest of the
    verb: the repo's vocabulary is true whether or not auto-selection is switched on."""
    m = _mod()
    goal = "Align the retry logic with the north-star vision doc"
    # #2564: both configs opt the ceiling up, so the variable under test stays the EXCLUDES.
    plain = _sdlc_cfg(tmp_path, {"model_selection_max_tier": "fable"}, ".plain")
    tuned = _sdlc_cfg(tmp_path, {"model_selection_max_tier": "fable",
                                 "model_selection_signal_excludes": ["vision"]}, ".tuned")
    assert m.main(["predict.py", "why", goal, plain]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "model=fable in=title hits=1 signal=vision"
    assert m.main(["predict.py", "why", goal, tuned]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "model=sonnet signal="


def test_the_bare_verb_agrees_with_why_and_with_resolve(tmp_path, capsys):
    """`predict.py '<goal>'` is the skill's headline command. If it ignored the excludes the loop
    honors, /sigma-model would print a recommendation the loop then contradicts."""
    m = _mod()
    goal = "regenerate the narrative stage"
    tuned = _sdlc_cfg(tmp_path, {"model_selection_max_tier": "fable",
                                 "model_selection_signal_excludes": ["narrativ"]}, ".tuned")
    assert m.main(["predict.py", goal, tuned]) == 0
    assert capsys.readouterr().out.strip() == "sonnet"
    assert m.main(["predict.py", goal, _sdlc_cfg(tmp_path, {"model_selection_max_tier": "fable"}, ".plain")]) == 0
    assert capsys.readouterr().out.strip() == "fable"


def test_the_cli_survives_a_missing_or_unreadable_sdlc_dir(tmp_path, capsys):
    """Both new config reads are best-effort: no `.sdlc`, or a config.json that isn't JSON, must
    behave exactly like an unconfigured repo rather than traceback out of a diagnostic verb.

    #2564 makes this a SAFETY property, not just a no-traceback one: an unreadable config must fail
    to the DEFAULT ceiling, never to an uncapped router. Hence `model=sonnet signal=vision` below —
    the creative signal still fires and is still reported, and the tier is still capped."""
    m = _mod()
    missing = str(tmp_path / "nope")
    assert m.main(["predict.py", "why", "draft the product vision", missing]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "model=sonnet in=title hits=1 signal=vision"
    broken = tmp_path / ".broken"; broken.mkdir()
    (broken / "config.json").write_text("{not json")
    assert m.main(["predict.py", "why", "draft the product vision", str(broken)]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "model=sonnet in=title hits=1 signal=vision"
    # valid JSON that is not an OBJECT: `.get` would not exist on it, so this reaches further into
    # the reader than a parse error does -- and `resolve`, which calls `.get` for the gate before
    # excludes are ever consulted, would traceback on every goal the loop picks.
    listy = tmp_path / ".listy"; listy.mkdir()
    (listy / "config.json").write_text("[1, 2]")
    assert m.main(["predict.py", "why", "draft the product vision", str(listy)]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "model=sonnet in=title hits=1 signal=vision"
    assert m.resolve("draft the product vision", str(listy)) is None
    assert m.main(["predict.py", "resolve", "draft the product vision", str(listy)]) == 0
    assert capsys.readouterr().out.strip() == "off"
    assert m.main(["predict.py", "draft the product vision", missing]) == 0
    assert capsys.readouterr().out.strip() == "sonnet"


def test_why_still_writes_nothing_when_excludes_are_configured(tmp_path):
    """`why`'s documented purity is about SIDE EFFECTS, not about ignorance of config: it may now
    read config.json, but it still records nothing."""
    m = _mod()
    calls = []

    class _Recorder:
        def run(self, *a, **k):
            calls.append((a, k))

    tuned = _sdlc_cfg(tmp_path, {"model_selection": "auto",
                                 "model_selection_signal_excludes": ["vision"]}, ".tuned")
    orig = m.subprocess
    try:
        m.subprocess = _Recorder()
        assert m.main(["predict.py", "why", "draft the product vision", tuned]) == 0
    finally:
        m.subprocess = orig
    assert calls == []


def test_the_excludes_knob_is_discoverable_in_the_scaffolded_config():
    """Same rule test_config_discoverability.py enforces for gates: a knob nobody can find in the
    one file every adopter opens has not shipped. The `_<name>` comment must also say what to put
    in it — the values are the router's own signal literals, which `why` prints."""
    tmpl = (pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-init" /
            "templates" / "config.json.tmpl").read_text(encoding="utf-8")
    import json
    cfg = json.loads(tmpl)
    assert cfg.get("model_selection_signal_excludes") == []      # present, and a no-op by default
    doc = cfg["_model_selection_signal_excludes"]
    assert "why" in doc and "signal" in doc


def test_the_skill_documents_the_excludes_knob():
    sk = (pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-model" / "SKILL.md"
          ).read_text(encoding="utf-8")
    assert "model_selection_signal_excludes" in sk


# --- issue #2446: bare `comment` matched as a PREFIX inside any longer identifier, because the ---
# haiku alternative's leading `\b` anchors only the START of the match and nothing anchors the END
# -- the same class of gap #1627's `auth[nz]?\b` fixed for `auth`, applied here to `comment`. It
# fired inside the filename `comment_watch.py` (goal #2443's own issue text lists it as one of 8
# script names run_with_timeout.py wraps), silently downgrading a real goal to the cheapest tier.
# `comment(?:s|ed|ing)?\b` scopes a trailing boundary to just this one alternative, with an explicit
# inflection list rather than a blanket boundary, so "comment"/"comments"/"commented"/"commenting"
# still match byte-identically while "comment_watch.py"/"commentary"/"commentator"/"commenter" no
# longer do. Same defect shape and same fix shape in `_EFFORT_PATTERNS`' "low" tier -- the two lists
# share this term verbatim, kept in lockstep the same way the security cluster already is.


def test_comment_does_not_match_as_a_bare_prefix_inside_a_filename():
    """The real regression (#2446), reproduced from goal #2443's own issue text: `comment_watch.py`
    is one of 8 script names #2443 lists by name, and #2443 is a new run_with_timeout.py wrapper
    across all 8 call sites -- not a comment/typo/rename-class change. This phrasing deliberately
    excludes the OTHER script names #2443's real text also mentions (`watch.sh`, `watch.py`,
    `agent_watch.py`) -- those independently fire the unrelated `watch` low-effort signal (by
    design, and unaffected by this fix), which would mask whether the `comment` fix specifically
    worked. Confirmed RED against the pre-fix code: predict()=='haiku', predict_effort()=='low',
    predict_with_reason()==('haiku','comment')."""
    m = _mod()
    goal = "Add a timeout wrapper for comment_watch.py"
    assert m.predict(goal) != "haiku", goal
    assert m.predict_effort(goal) != "low", goal
    assert m.predict_with_reason(goal) == ("sonnet", None), goal


def test_bare_comment_does_not_admit_commentary_commentator_or_commenter():
    """The dictionary-sweep negative control this file's own convention requires alongside every
    widening (see #1627's `test_bare_auth_does_not_admit_author_or_authoring`). `commentary` and
    `commentator` are ordinary English words -- not this repo's own jargon -- with a plausible
    collision in this exact product family (a storytelling/video pipeline where "add live
    commentary" or "build the commentator overlay" is a realistic sentence, not a contrived one).
    Confirmed RED against the pre-fix code: every phrase below currently resolves to haiku/low."""
    m = _mod()
    for g in ("write the commentary for the launch video", "build the commentator overlay",
              "loop in the commenter who filed this issue", "add live commentary to the player"):
        assert m.predict(g) == "sonnet", g
        assert m.predict_effort(g) == "medium", g


def test_comment_and_its_real_inflections_still_reach_haiku_and_low():
    """Preserves intent: the fix narrows to an explicit inflection list rather than a blanket
    trailing boundary, so the routine trivial-work phrasings must keep matching. Already true
    against the pre-fix code too (the bug only ever ADDS matches, never removes one) -- not a RED
    test, a regression-safety pin so the fix cannot accidentally narrow past the intended set."""
    m = _mod()
    for g in ("remove the dead comment", "clean up the comments", "delete the commented-out block",
              "stop commenting out the failing assertions"):
        assert m.predict(g) == "haiku", g
        assert m.predict_effort(g) == "low", g


def test_predict_with_reason_reports_the_inflected_comment_signal():
    """Mirrors #1627's own `test_predict_with_reason_reports_the_bare_auth_signal`: the reported
    signal is the literal text that matched, suffix included, not a paraphrase of the rule.
    Confirmed RED on the last two lines against the pre-fix code, which can only ever report the
    bare 7-character literal `comment` (no suffix group exists yet to consume `s`/`ed`/`ing`)."""
    m = _mod()
    assert m.predict_with_reason("remove the dead comment") == ("haiku", "comment")
    assert m.predict_with_reason("clean up the comments") == ("haiku", "comments")
    assert m.predict_with_reason("delete the commented-out block") == ("haiku", "commented")


# --- #2638: portable tiers must never be sent as Codex model IDs -------------------------------

def test_codex_host_model_mapping_uses_valid_model_ids_and_effort():
    """The four portable tiers are ledger vocabulary, not Codex API model IDs.  Pin the host
    dispatch choice so a future documentation-only edit cannot send `sonnet` (or any other Claude
    tier label) to Codex.  This is RED before `resolve_host_model()` exists."""
    m = _mod()
    assert m.resolve_host_model("codex", "haiku") == {
        "model": "gpt-5.6-luna", "effort": "low"}
    assert m.resolve_host_model("codex", "sonnet") == {
        "model": "gpt-5.6-terra", "effort": "medium"}
    assert m.resolve_host_model("codex", "opus") == {
        "model": "gpt-6-astra", "effort": "high"}
    assert m.resolve_host_model("codex", "fable") == {
        "model": "gpt-6-astra", "effort": "high"}


def test_codex_host_model_preserves_the_canonical_effort():
    """Host adaptation may translate the tier to an ID, never quietly reclassify effort.

    `diagnose` is intentionally sonnet/high: capability tier and reasoning effort are separate
    selection axes.  The same portable decision must reach Claude and Codex.
    """
    m = _mod()
    assert m.resolve_host_model("codex", "sonnet", effort="high") == {
        "model": "gpt-5.6-terra", "effort": "high"}
    assert m.main(["predict.py", "host-model", "codex", "sonnet", ".sdlc",
                   "--effort", "high"]) == 0


def test_resolve_profile_selects_one_tier_and_effort_for_both_hosts(tmp_path):
    """The portable profile, not a host default, is the cross-host selection contract."""
    m = _mod()
    sdlc = _sdlc_cfg(tmp_path, {"model_selection": "auto"})
    assert m.resolve_profile("diagnose the latency regression", sdlc) == {
        "model": "sonnet", "effort": "high"}
    assert m.resolve_host_model("codex", "sonnet", effort="high") == {
        "model": "gpt-5.6-terra", "effort": "high"}


def test_codex_host_model_off_uses_the_versioned_default(tmp_path):
    """Auto-selection off still needs a concrete Codex model, never the parent-session default."""
    assert _mod().resolve_host_model("codex", "off") == {
        "model": "gpt-5.6-terra", "effort": "medium"}
    sdlc = _sdlc_cfg(tmp_path, {"model_selection": "off"})
    assert _mod().resolve_profile("diagnose the latency regression", sdlc) == {
        "model": "off", "effort": "medium"}


def test_codex_host_model_mapping_refuses_unknown_tiers_and_hosts():
    """A failed mapping must be loud.  Falling back to the portable tier recreates the defect."""
    import pytest
    m = _mod()
    with pytest.raises(ValueError, match="unknown model tier"):
        m.resolve_host_model("codex", "not-a-tier")
    with pytest.raises(ValueError, match="unsupported host"):
        m.resolve_host_model("claude", "sonnet")


def test_host_model_cli_emits_the_dispatch_arguments(capsys):
    """Skills need a shell-safe, machine-readable gesture rather than copying a model map by eye."""
    m = _mod()
    assert m.main(["predict.py", "host-model", "codex", "sonnet"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "model=gpt-5.6-terra effort=medium"


def test_host_model_cli_honors_a_repo_codex_override(tmp_path, capsys):
    """A catalog change is an operator configuration update, not a reason to edit a skill."""
    import json
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir()
    sdlc.joinpath("config.json").write_text(json.dumps({
        "model_host_overrides": {"codex": {"sonnet": {
            "model": "gpt-6-astra", "effort": "high"}}}}))
    m = _mod()
    assert m.main(["predict.py", "host-model", "codex", "sonnet", str(sdlc)]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "model=gpt-6-astra effort=high"


def test_host_model_cli_refuses_a_broken_config_before_dispatch(tmp_path, capsys):
    """Unlike advisory tier prediction, dispatch resolution cannot ignore a malformed override file."""
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir()
    sdlc.joinpath("config.json").write_text("{not json")
    m = _mod()
    assert m.main(["predict.py", "host-model", "codex", "sonnet", str(sdlc)]) == 2
    assert "invalid host-model config" in capsys.readouterr().err


def test_host_model_remains_a_valid_bare_goal_text(tmp_path, capsys):
    """Adding a subcommand must not reserve a word that the headline goal classifier accepted."""
    m = _mod()
    assert m.main(["predict.py", "host-model", str(tmp_path / "missing")]) == 0
    assert capsys.readouterr().out.strip() == m.predict("host-model")


def test_host_model_refuses_a_malformed_configured_override():
    """Configured host models are dispatch input, so an unsafe spelling must fail before dispatch."""
    import pytest
    m = _mod()
    with pytest.raises(ValueError, match="invalid codex model"):
        m.resolve_host_model("codex", "sonnet", {
            "model_host_overrides": {"codex": {"sonnet": {
                "model": "gpt 5.6 terra", "effort": "medium"}}}})


def test_host_model_refuses_a_portable_tier_as_a_configured_model_id():
    """An override must not be able to put `sonnet` back into Codex's model argument."""
    import pytest
    m = _mod()
    with pytest.raises(ValueError, match="portable tier"):
        m.resolve_host_model("codex", "sonnet", {
            "model_host_overrides": {"codex": {"sonnet": {
                "model": "sonnet", "effort": "medium"}}}})
    with pytest.raises(ValueError, match="or off"):
        m.resolve_host_model("codex", "sonnet", {
            "model_host_overrides": {"codex": {"sonnet": {
                "model": "off", "effort": "medium"}}}})


def test_host_model_refuses_unapproved_or_ambiguous_configured_model_ids():
    """Overrides select a reviewed Codex catalog entry; they never become a raw model escape hatch."""
    import pytest
    m = _mod()
    for model in ("claude-opus-4-5", "future-unreviewed-model"):
        with pytest.raises(ValueError, match="approved Codex model ID"):
            m.resolve_host_model("codex", "sonnet", {
                "model_host_overrides": {"codex": {"sonnet": {
                    "model": model, "effort": "medium"}}}})
    with pytest.raises(ValueError, match="only model and effort"):
        m.resolve_host_model("codex", "sonnet", {
            "model_host_overrides": {"codex": {"sonnet": {
                "model": "gpt-5.6-terra", "effort": "medium", "extra": "ignored-before"}}}})


def test_auto_resolution_refuses_an_invalid_codex_mapping_before_recording_a_choice(tmp_path,
                                                                                     monkeypatch, capsys):
    """A bad host map must not write a model_choice that implies a dispatch can proceed."""
    import json, pytest
    m = _mod()
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir()
    sdlc.joinpath("config.json").write_text(json.dumps({
        "model_selection": "auto",
        "model_host_overrides": {"codex": {"sonnet": {
            "model": "claude-opus-4-5", "effort": "high"}}},
    }))
    calls = []
    monkeypatch.setattr(m.subprocess, "run", _capture_run(calls))
    with pytest.raises(ValueError, match="approved Codex model ID"):
        m.resolve("ordinary maintenance", sdlc, goal="42")
    with pytest.raises(ValueError, match="approved Codex model ID"):
        m.resolve_step("run the tests", sdlc, goal="42")
    assert calls == []
    assert m.main(["predict.py", "resolve", "ordinary maintenance", str(sdlc), "42"]) == 2
    assert "approved Codex model ID" in capsys.readouterr().err
    assert calls == []


def test_host_model_refuses_an_invalid_sibling_override():
    """One bad entry cannot be hidden by dispatching a different configured tier."""
    import pytest
    m = _mod()
    with pytest.raises(ValueError, match="invalid codex override tier"):
        m.resolve_host_model("codex", "sonnet", {
            "model_host_overrides": {"codex": {
                "unknown-tier": {"model": "gpt-6-astra", "effort": "high"}}}})
    with pytest.raises(ValueError, match="portable tier"):
        m.resolve_host_model("codex", "sonnet", {
            "model_host_overrides": {"codex": {
                "haiku": {"model": "haiku", "effort": "low"}}}})
    with pytest.raises(ValueError, match="invalid model host override"):
        m.resolve_host_model("codex", "sonnet", {
            "model_host_overrides": {"claude": "not-an-object"}})
    with pytest.raises(ValueError, match="model_host_overrides"):
        m.resolve_host_model("codex", "sonnet", {"model_host_overrides": None})
    with pytest.raises(ValueError, match="invalid codex override"):
        m.resolve_host_model("codex", "sonnet", {
            "model_host_overrides": {"codex": None}})


def test_codex_dispatch_docs_invoke_the_host_model_resolver():
    """The CLI is only a control if the gestures agents receive actually use it."""
    root = pathlib.Path(__file__).resolve().parent.parent / "skills"
    commands = {
        root / "sigma-model" / "SKILL.md":
            'scripts/predict.py" host-model codex',
        root / "sigma-loop" / "references" / "running.md":
            '../sigma-model/scripts/predict.py" host-model codex',
        root / "sigma-loop" / "references" / "picking.md":
            '../sigma-model/scripts/predict.py" host-model codex',
        root / "sigma-goal" / "SKILL.md":
            '../sigma-model/scripts/predict.py" host-model codex',
    }
    for path, command in commands.items():
        assert command in path.read_text(encoding="utf-8"), path
    running = (root / "sigma-loop" / "references" / "running.md").read_text(encoding="utf-8")
    assert '../sigma-model/scripts/predict.py" host-model codex' in running[
        running.index("**Per-STEP downgrade:"):]
    slices = running[running.index("**3b. Independent slices?"):]
    assert '../sigma-model/scripts/predict.py" host-model codex' in slices
    assert "--tier <tier-or-off>" in slices
    assert "`work.py start`). Before every Codex" in slices
    assert "into its fresh interactive Codex process rather than inheriting" not in slices
    loop = (root / "sigma-loop" / "SKILL.md").read_text(encoding="utf-8")
    assert "--host codex --goal-worktree <path> --tier <tier-or-off>" in loop
    assert ('../sigma-model/scripts/predict.py"\n   host-model codex "<tier-or-off>" .sdlc') in loop
    model_skill = (root / "sigma-model" / "SKILL.md").read_text(encoding="utf-8")
    assert "portable tier prediction is disabled" in model_skill
    assert "before every dispatched Codex phase, pass `off` to `host-model`" in model_skill
    assert "resolve-profile" in model_skill
    assert "canonical portable effort" in model_skill
    step_section = running[running.index("**Per-STEP downgrade:"):]
    assert "--effort <effort>" in step_section
    assert "uses the selected `<effort>`, never its tier default" in running
    readme = (root.parent / "docs/platform-support.md").read_text(encoding="utf-8")
    assert "`predict.py host-model codex` resolves them, or the explicit `off` fallback" in readme
    progress = (root / "sigma-loop" / "references" / "progress.md").read_text(encoding="utf-8")
    assert "a Codex phase may still be\nbe dispatched" not in progress
    assert "a Codex phase may still be\ndispatched" in progress
    output_detail = (root.parent / "docs" / "output-contract-detail.md").read_text(encoding="utf-8")
    assert "A dispatched\nCodex phase with `model_selection: off`" in output_detail


def test_host_model_override_is_discoverable_in_the_scaffolded_config():
    """A future Codex catalog change must have a documented, non-source-edit recovery path."""
    import json
    template = (pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-init" /
                "templates" / "config.json.tmpl")
    config = json.loads(template.read_text(encoding="utf-8"))
    assert config["model_host_overrides"] == {}
    assert "codex" in config["_model_host_overrides"]


# --- #2564: the fable tier is priced ABOVE opus, so "resolve upward" is false at exactly one tier -
# `_PATTERNS` resolves conflicts upward on CAPABILITY, which is sound for opus ($5/$25 per 1M) but
# not for fable ($10/$50) -- one creative stem anywhere in title+body promoted a typo fix to 5x
# sonnet, and an unattended overnight run exhausted an account's credits. The guard is a price
# CEILING (`model_selection_max_tier`, default `opus`), applied once after the scan: if the winning
# tier is priced above the cap, the tier clamps to `min(_DEFAULT, cap)` and THE SIGNAL THAT FIRED IS
# KEPT. Clamping to `_DEFAULT` rather than falling through to the next tier is the load-bearing
# choice -- see test_a_genuinely_creative_goal_is_not_downgraded_to_haiku for what fall-through does.


def test_issue_2564_creative_stems_in_trivial_goals_never_reach_fable():
    """The issue's own reproduction table, run through the Python API. Asserts the EXACT tier, not
    merely `!= "fable"`: rows 1-3 are what separates a correct `min(_DEFAULT, cap)` clamp from a
    mutant that clamps to the cap itself (which would return opus here, at 2.5x, on a typo fix).
    Row 4 is the issue's own control and must be untouched by this change."""
    m = _mod()
    for goal in ("Fix typo in the blog post title on the landing page",
                 "Rename the tagline constant to SLOGAN",
                 "Update the README to link to the product vision doc",
                 "Bump the version number in package.json"):        # control
        assert m.predict(goal) == "sonnet", goal


def test_every_fable_stem_is_unreachable_by_default():
    """Each fable term HARVESTED FROM THE LIVE PATTERN, never a hand-copied list that goes stale.

    Asserts `== "sonnet"` rather than `!= "fable"` on purpose: the negative form also passes against
    a router with the `("fable", ...)` branch deleted outright, which is not the same fix at all."""
    m = _mod()
    terms = sorted(_alternation_terms(dict(m._PATTERNS)["fable"]))
    assert len(terms) == 8, terms
    for term in terms:
        goal = f"handle the {_literal_for(term)} case"
        assert m.predict(goal) == "sonnet", goal


def test_the_cap_can_never_raise_the_resulting_price():
    """The design's strongest safety guarantee, and the only test that holds it: lowering the cap
    can never make a goal MORE expensive. Swept over the router's whole vocabulary x every cap."""
    m = _mod()
    rank = {t: i for i, t in enumerate(m._TIER_PRICE_ORDER)}
    for _tier, _lit, goal in _vocabulary_probes(m):
        uncapped = m.predict(goal, max_tier="fable")
        for cap in m._TIER_PRICE_ORDER:
            got = m.predict(goal, max_tier=cap)
            assert rank[got] <= rank[uncapped], (goal, cap, got, uncapped)
            assert rank[got] <= rank[cap], (goal, cap, got)


def test_a_genuinely_creative_goal_is_not_downgraded_to_haiku():
    """THE design decision, pinned so a future "fix" back to fall-through fails here.

    Falling through to the next tier when fable is capped reproduces the issue's own expected table
    -- and sends every goal below to HAIKU, a 10x capability drop two tiers under `_DEFAULT`. The
    router cannot tell these from "Fix typo in the blog post title": both are "fable stem + haiku
    stem". `loop.py` classifies title+body, so a haiku stem (changelog/docstring/comment/lint/typo/
    renam/formatting/dead code/whitespace) is present in almost any real issue body, which makes
    that the common outcome rather than the corner. Clamping to `_DEFAULT` keeps the module's own
    invariant true (predict.py:9-10: under-powering is the expensive error)."""
    m = _mod()
    for goal in ("Rewrite the marketing copy for the pricing page and rename the CTA constant",
                 "Draft the launch blog series and update the changelog",
                 "Write the product vision narrative; fix a typo in the intro"):
        assert m.predict(goal) == "sonnet", goal


def test_max_tier_fable_restores_the_legacy_router_exactly():
    """Carries forward the guarantee `test_no_config_is_byte_identical_to_the_pre_1601_router` can
    no longer make for the 8 fable probes: with the cap opted up, the router is byte-identical to
    the pre-#2564 one across its WHOLE vocabulary, on all five of that pin's assertions.

    The fable term count is LOAD-BEARING, not belt-and-braces -- do not trim it. Both sides of the
    comparison (`_vocabulary_probes` and `_legacy_predict_with_reason`) iterate the same live
    `_PATTERNS`, so deleting the `("fable", ...)` branch would remove it from both at once and this
    test would pass vacuously. The existing `len(probes) > 30` backstop does not catch it either:
    42 probes become 34."""
    m = _mod()
    assert len(_alternation_terms(dict(m._PATTERNS)["fable"])) == 8
    for tier, lit, goal in _vocabulary_probes(m):
        legacy = _legacy_predict_with_reason(m, goal)
        assert legacy == (tier, lit), goal                          # the probe fires its own term
        assert m.predict_with_reason(goal, max_tier="fable") == legacy, goal
        assert m.predict_with_reason(goal, (), "fable") == legacy, goal
        assert m.predict_with_reason(goal, ("zzzz-names-no-signal",), "fable") == legacy, goal
        assert m.predict(goal, max_tier="fable") == legacy[0], goal


def test_the_clamp_keeps_the_signal_that_fired():
    """`None` already means "no pattern matched and `_DEFAULT` applies" (predict.py:290-291). A
    clamp that returned `None` would make `('sonnet', None)` mean two things, blank out the literal
    #1601's opt-out workflow tells a repo to copy from `why`, and leave every capped goal's
    `model_choice` ledger event with no reason (loop.py:961-963 adds `signal` only `if signal`)."""
    m = _mod()
    assert m.predict_with_reason("Update the README to link to the product vision doc") == \
        ("sonnet", "vision")
    assert m.predict_with_reason("Draft the launch blog series") == ("sonnet", "blog")
    # a genuinely defaulted tier still carries None -- the clamp must not manufacture a signal
    assert m.predict_with_reason("add a retry to the http client") == ("sonnet", None)


def test_cap_also_binds_the_default_tier():
    """A cap BELOW `_DEFAULT` binds the defaulted path too, via `min(_DEFAULT, cap)`.

    This test does not by itself pin the `min`: a mutant returning a bare `cap` also passes it.
    test_issue_2564_creative_stems_in_trivial_goals_never_reach_fable's rows 1-3 (sonnet vs opus)
    are what separate the two."""
    m = _mod()
    assert m.predict("add a retry to the http client", max_tier="haiku") == "haiku"
    assert m.predict("migrate the payment database schema", max_tier="haiku") == "haiku"
    assert m.predict("migrate the payment database schema", max_tier="sonnet") == "sonnet"


def test_malformed_max_tier_falls_back_to_the_default_cap():
    """Fails SAFE, mirroring `signal_excludes`: junk leaves the DEFAULT ceiling in place rather than
    uncapping the router or raising into `resolve`, which runs for every goal the loop picks."""
    m = _mod()
    for junk in (None, 7, {"a": 1}, True, [], ["fable"], "gpt-5", "", "   "):
        assert m.max_tier({"model_selection_max_tier": junk}) == m._MAX_TIER_DEFAULT, junk
    assert m.max_tier(None) == m._MAX_TIER_DEFAULT
    assert m.max_tier("not a mapping") == m._MAX_TIER_DEFAULT
    assert m.max_tier({}) == m._MAX_TIER_DEFAULT
    assert m.max_tier({"model_selection_max_tier": "FABLE"}) == "fable"       # case-insensitive
    assert m.max_tier({"model_selection_max_tier": " haiku "}) == "haiku"     # stripped


def test_all_four_documented_cli_gestures_honour_the_cap(tmp_path, capsys, monkeypatch):
    """The control runs on the gesture the DOCS give. All four tier-producing invocations the
    skills actually prescribe -- the bare verb (sigma-model/SKILL.md:30), `why` (:86), `resolve`
    (:60, sigma-goal/SKILL.md:36) and `resolve-step` (sigma-loop/references/running.md:110) -- each
    asserted from real stdout. `resolve-step` dispatches a real subagent, so one that silently
    missed its `max_tier(cfg)` read would ship a plan step at fable with the whole suite green."""
    m = _mod()
    monkeypatch.setattr(m.subprocess, "run", _capture_run([]))
    goal = "Update the README to link to the product vision doc"
    plain = _sdlc_cfg(tmp_path, {"model_selection": "auto"}, ".plain")

    assert m.main(["predict.py", goal, plain]) == 0                          # bare verb
    assert capsys.readouterr().out.strip() == "sonnet"
    assert m.main(["predict.py", "why", goal, plain]) == 0                   # why
    assert capsys.readouterr().out.splitlines()[0] == "model=sonnet in=title hits=1 signal=vision"
    assert m.main(["predict.py", "resolve", goal, plain]) == 0               # resolve
    assert capsys.readouterr().out.strip() == "sonnet"
    assert m.main(["predict.py", "resolve-step",                             # resolve-step
                   "render the narrative stage", plain, "2564"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "model=sonnet effort=medium"

    # ...and the opt-up restores every one of them, through the same four gestures.
    up = _sdlc_cfg(tmp_path, {"model_selection": "auto",
                              "model_selection_max_tier": "fable"}, ".up")
    assert m.main(["predict.py", goal, up]) == 0
    assert capsys.readouterr().out.strip() == "fable"
    assert m.main(["predict.py", "why", goal, up]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "model=fable in=title hits=1 signal=vision"
    assert m.main(["predict.py", "resolve", goal, up]) == 0
    assert capsys.readouterr().out.strip() == "fable"
    assert m.main(["predict.py", "resolve-step", "render the narrative stage", up, "2564"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "model=fable effort=medium"


# --- #1950: the effort axis shares the tier axis's safeguards for `high` -------------------------
def _goal(title, body):
    return f"{title}\n\n{body}"


def test_1950_one_incidental_body_word_stays_medium():
    m = _mod()
    assert m.predict_effort(_goal("Add a retry", "Context: the auth team asked for this.")) == "medium"
    assert m.predict_effort_location(_goal("Add a retry", "see the architecture notes")) \
        == ("medium", None, None, 0)


def test_1950_two_body_hits_are_high_and_counted():
    m = _mod()
    text = _goal("Add a retry", "The auth layer is touched; the auth tokens too.")
    assert m.predict_effort_location(text) == ("high", "auth", "body", 2)


def test_1950_one_title_hit_is_high():
    m = _mod()
    assert m.predict_effort_location(_goal("Debug the retry loop", "details")) == ("high", "debug", "title", 1)
    assert m.predict_effort("debug the race condition in the scheduler") == "high"


def test_1950_code_span_and_path_hits_never_count():
    m = _mod()
    assert m.predict_effort(_goal("Add a retry", "uses `auth` and `auth` again and `migrate`")) == "medium"
    assert m.predict_effort(_goal("Add a retry", "see docs/architecture/auth.md and docs/architecture/auth.md")) == "medium"
    assert m.predict_effort(_goal("Fix `debug` flag", "x")) == "medium"


def test_1950_low_axis_keeps_its_rules_and_is_reached_after_a_rejected_high():
    m = _mod()
    assert m.predict_effort(_goal("Fix typo", "one auth mention")) == "low"
    assert m.predict_effort(_goal("Tidy", "fix the typo in the docs")) == "low"


def test_1950_why_prints_the_effort_signal_on_a_second_line(capsys):
    m = _mod()
    assert m.main(["predict.py", "why", _goal("Add a retry", "auth and auth again")]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[1] == "effort=high in=body hits=2 signal=auth"
    assert m.main(["predict.py", "why", _goal("Add a retry", "one auth")]) == 0
    assert capsys.readouterr().out.splitlines()[1] == "effort=medium signal="
