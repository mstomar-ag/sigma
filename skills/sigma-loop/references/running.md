# Running one goal

## Context-efficient inspection

Locate before opening: use `rg --files` for paths and `rg -n` for symbols or claims.
Read only the relevant lines with a bounded range; expand outward only when the
boundary is unclear. Do not dump whole files or broad search output into context.
For bulky tests or diagnostics, save full output to a scratch file, then inspect
its exit status and focused matches or a bounded tail. Do not hide failures, and
keep secrets out of the scratch file. This is guidance, not a measured savings claim.

The detail behind step 3 of [`../SKILL.md`](../SKILL.md): resolving the model tier, running
each phase as its own subagent, the maker-is-never-the-checker rule, the no-phase-commits
rule, matching ceremony to the lane, cutting the worktree (3a) and running slice waves (3b).

---

Then **recall prior art** — if the knowledge graph is enabled, run the `sigma-context`
pre-flight to pull a cited brief from the graph + past issues + conventions, so the goal starts
informed by history instead of a flushed window (no-op when the KG is off).
**Match the model to the goal.** **The goal-level `model_choice` ledger event is now written
automatically, in code, at pick time (#1627)** — `loop.py`'s own `_next()` (the one chokepoint
every picker already passes through) reads the goal's real title+body and calls `predict.py why`
+ records the result itself, with no agent involvement. **Read the tier IT resolved from the
pick's own stderr** — a line shaped `sigma: model tier for <goal> resolved to <tier>
(signal=<signal>)` — rather than recomputing it yourself, **especially in github mode**: `"$goal"`
there is the bare issue number, and a manual `predict.py resolve "$goal" .sdlc` classifies that
literal digit string, not the issue's real text, landing the unsignalled `sonnet` default no
matter what the goal actually says. That is the exact bug #1627 fixed for the automatic call;
recomputing by hand reintroduces it. In LOCAL mode `"$goal"` genuinely is the goal's own file
path, so `predict.py resolve "$goal" .sdlc` there still resolves correctly — but reading the
already-printed stderr line works in EITHER mode and needs no branching. If the stderr line is
missing (`model_selection` isn't `"auto"`, or the pick predates this line reaching stdout in your
log), fall back to running `resolve` yourself: in local mode `resolve "$goal" .sdlc` as before; in
github mode, fetch the issue's real text FIRST, title on the first line
(`gh issue view "$goal" --json title,body --jq '.title + "\n\n" + (.body // "")'`, POSIX
shell — a haiku signal counts only in the title, #2827) and pass that text as the argument, never
the bare `"$goal"`. Whatever tier you end up with
(`haiku`/`sonnet`/`opus`/`fable`) is the GOAL's ceiling; `signal` (from either the stderr line or
your own fallback `why`/`resolve` call) is the literal text the regex matched (`migrat`, `secur`)
— WHY it's that tier, which no reader can check without it (#880). You do not need to separately
record the goal-level LEDGER choice — that already happened. **You DO still need to log this same
tier+signal to the LOCAL action-log copy yourself** (a different destination this issue did not
automate — `.sdlc/state/log/`, not the ledger): `python3 "${CLAUDE_SKILL_DIR}/scripts/loop.py" log
.sdlc "$goal" model_choice --model <tier> --signal <signal>`. **Use the SAME tier+signal you just
established above — never a fresh, separate `why "$goal"` call here**: in github mode that bare
`"$goal"` is exactly the wrong input this whole paragraph exists to steer you away from, and
logging a second, differently-computed value here would silently contradict the one just
recorded. A defaulted tier matches nothing and correctly carries no signal — pass it empty or
omit it. **Per-step `resolve-step` below is UNCHANGED** — it stays prose-invoked, since a plan
step's free text has no structured, goal-agnostic artifact code could read on its own.
**`agent_dispatch --role phase/goal-slot/slice` below stays prose-invoked:** whether a phase ran as its own dispatched subagent or inline is the host's decision and no code here sits inside it. A phase that silently regresses to inline breaks the maker≠checker discipline, so since #684 the gate is on the OTHER side: `loop.py record ... done` refuses a goal whose action log lacks the phases (below). Detecting the gap after the fact stays tracked in #1779. The goal-slot tier is captured at pick time, like the phase tier (`references/picking.md` step 1a).
Then run **each phase as its own subagent** with that host's model override. Before dispatch, hold
the canonical portable pair (`<tier-or-off>`, `<effort>`) selected for this goal; Claude's Task tool
accepts the tier directly: pass that exact Task `model` selector to
`phase_report.py start --requested-model <selector>` too, so the step banner can name what was
requested. **Also pass the canonical `<effort>` to Claude.** On Codex, resolve the same known tier
without replacing its effort: `python3 "${CLAUDE_SKILL_DIR}/../sigma-model/scripts/predict.py" host-model codex "<tier-or-off>" .sdlc --effort "<effort>"`.
It prints `model=<id> effort=<effort>`; Codex uses the selected `<effort>`, never its tier default;
pass those exact values as the Codex subagent's `model`
and reasoning-effort arguments. Never pass an Anthropic tier string as a Codex model ID. The
resolver refuses an unknown or malformed mapping; choose an approved Codex ID in
`model_host_overrides.codex`, or update the plugin when the catalog changes, instead of guessing.
Pass that returned ID to
`phase_report.py start --host-model <id>` as well; `end` checks every observed turn and refuses a
mismatch. On Codex, set `fork_turns="none"` for every phase and
pass the goal, phase, worktree and relevant artifact paths in the prompt; the default copies the
maker's conversation into the child. Bracket each phase-subagent dispatch itself:
`python3 "${CLAUDE_SKILL_DIR}/scripts/loop.py" log .sdlc "$goal" agent_dispatch --role phase --phase
<phase> --model <tier>` right after dispatching it, `agent_done --role phase --phase <phase>
--result <...>` right when it returns. **`--model <tier>` is now REQUIRED on this call (#2514) —
`loop.py log` refuses a `--role phase` dispatch with no `--model`.** `<tier>` here is the SAME
value you already read from `model_choice` above, never a fresh resolve at this call site — the
tier is already known by the time you dispatch. One subagent PER PHASE, not one for the whole goal: that is
what keeps a reviewer phase a **sibling** of the maker phase it checks (fresh context, no nesting),
never a continuation of it — see the maker≠checker rule below. Artifacts pass between phases through
the filesystem (`.sdlc/plans/`, the worktree diff, the issue timeline), not shared context. If it
is supported by the host, keep static skill instructions and tool definitions ahead of the varying
goal/phase details so prompt caches can reuse the stable prefix; Sigma does not control the
host's cache settings. Do not paste whole prior phase transcripts into a child prompt. Have each
phase return its verdict, changed artifact paths and any unresolved risk; keep full evidence in
durable artifacts that the next phase can inspect. Preserve each plan step's verification and the
fresh full proof required in the completion turn. `loop.py verify` remains mandatory and must run
the configured command; a prior manual test run never replaces it. Avoid duplicate manual
full-suite invocations when the source and proving command have not changed; repeat when code
changes or a concrete flaky failure leaves the result uncertain. This keeps independent review
and required gates intact while avoiding duplicate
model turns and full-suite reruns.
**Phases are recorded and enforced (#684), tier `off` included.** Per phase: `phase_report.py start ... <phase> --model <tier|off>`
before dispatch, `phase_report.py end ... <phase> --agent-id <agentId>` after (banner, cost line, action-log row). Independent
verdicts carry the reviewer's own agent id: `work.py record-plan-review <dir> <goal> --verdict SOUND|SOUND-WITH-REFINEMENTS|FIX-FIRST
--plan-sha256 <brief sha> --agent-id <id>` and `work.py record-review <dir> <goal> --verdict APPROVE|SEND-BACK|BLOCK --agent-id <id>`.
`record done` is refused until research, plan, an approved plan-review, implement (started after it), an approved review and retro are
recorded; `loop.py phases <dir> <goal>` lists them and the next step (run it after each phase, obey NEXT; `start implement` is refused before the plan-review verdict). Trivial-goal lever: `loop.py waive-phases <dir> <goal>
research,retro --reason "<why>"` (recorded; nothing else waivable). Gate: `gates.phase_record.enabled`.
If model selection prints `off` (the default), pass `off` to the Codex resolver. It selects the
versioned ordinary-work mapping (`sonnet` → Terra), never the current parent-session model.
**Per-STEP downgrade:** once the plan exists, resolve each plan
step too — `python3 "${CLAUDE_SKILL_DIR}/../sigma-model/scripts/predict.py" resolve-step "<step
text>" .sdlc "$goal"` prints `model=<tier> effort=<low|medium|high>` (the trailing `"$goal"` is
new, #1030 -- resolve-step's own step text can never double as a goal identifier the way a
goal-level call's text can, so this is the one call in this skill that needs it spelled out
explicitly; resolve-step now also records the choice internally, see sigma-model's own SKILL.md)
— log its portable tier and effort too (`python3
"${CLAUDE_SKILL_DIR}/scripts/loop.py" log .sdlc "$goal" model_choice --model <tier> --effort <effort>
--phase <phase>`) — and run a MECHANICAL step (run the
tests, a watcher/poll, lint) in a subagent at ITS cheaper tier/effort instead of the goal
ceiling. Before a Codex step dispatch, resolve the `resolve-step` result's `<tier>` while preserving
its `<effort>` with `python3 "${CLAUDE_SKILL_DIR}/../sigma-model/scripts/predict.py" host-model codex "<tier-or-off>" .sdlc --effort "<effort>"` and
pass its returned model ID and effort to Codex. On other hosts, pass the tier and, where supported,
the resolve-step effort. For that step's `phase_report.py step` banner, pass the exact dispatched
model ID with `--host-model <id>` instead of the ordinary `--phase-model`, as well as its portable
`--model <tier>`; otherwise the banner honestly reports the host model as unrecorded.
Never run a step ABOVE the goal ceiling — except the tier a `loop.py escalate` answer names
(below), which becomes the new ceiling. Then read the goal and
run it through the full SDLC (goal → research → plan → plan-review →
implement → review) — each phase via its **executor** (the `superpowers`/`code-review` companion on
Claude if installed, else Sigma's portable `sigma-brainstorm`/`sigma-research`/`sigma-plan`/
`sigma-implement`/`sigma-review`/`sigma-verify`; each skill's resolution header picks). `$goal` is a **file path** in local mode (read the file) or a **GitHub issue
number** in github mode (`gh issue view "$goal"` to read it).

**P1 GOAL records acceptance before Research or code.** Run
`python3 "${CLAUDE_SKILL_DIR}/scripts/acceptance.py" record .sdlc "$goal"`.
It captures 3–7 checkable statements from `## Done when`. If the section is absent,
draft a file with that section and repeat with `--draft <file>`; the command posts drafted
GitHub criteria as an issue comment before writing the record. Optionally supply
`--verify-command '<command>'` (local frontmatter is inherited). Never put secrets in it.
A refusal stops P1 until the source/draft is repaired. Keep the resulting
`.sdlc/acceptance/<goal-stem>.md` unchanged; copy it into the goal worktree and commit it
with plan/research. This step applies regardless of companion or portable executor.

**The maker must not be the checker (`config.review.independent`, default on).**
Every review gate — plan-review, the pre-PR code review, the post-PR review at step 6 — is asked to
run as a **fresh subagent that never saw the maker's context**. Dispatching it and waiting on it is the same blocking
dependency `loop.py verify`/`work.py merge` are — see the REQUIRED PATTERN at step 6, which covers
this dispatch too: your very next action depends on its verdict, so dispatch it foreground/
blocking rather than inventing a separate "wait" step. Give it the PROJECT, not the author:
`python3 "${CLAUDE_SKILL_DIR}/scripts/review_context.py" brief .sdlc "$goal" --for
plan-review|code-review|pr-review [--artifact <path|PR#>] > "/tmp/brief-$(basename "$goal" .md).md"`
assembles the pack into that file (`basename`: `$goal` is a path in local mode) — north-star +
conventions + contracts + the goal + a pointer to the artifact — and you hand the subagent **only
that**. So it re-derives blast radius from the whole repo and can *disagree*, instead of
rubber-stamping the plan/diff it just wrote (a lower-tier maker does). A diff-only reviewer cannot see what a small change breaks two files
away; the brief's whole-repo grounding is the point. Where the host has no subagents this is **not**
a degradation — run `python3 "${CLAUDE_SKILL_DIR}/scripts/reviewer.py" resolve .sdlc` and use the
mechanism it names (a fresh `process`, or the operator's `command`); every host asks for an
author-blind reviewer by its own route. Only a machine where the resolver returns `inline` reviews
inline, and that verdict must say so. With
`review.independent: false` reviews run inline as before (the maker reviews its own work — only for a
trivial solo repo).

**Record the plan-review verdict before Implement** (`gates.plan_review`). The dispatching site —
never the dispatched reviewer — records it from the main checkout (3a), before anything edits the
plan, against the brief file the dispatch gesture above wrote. It only READS that file; never
rebuild the brief here, because a rebuild hashes the edited plan and the check could never fail:

```
python3 "${CLAUDE_SKILL_DIR}/scripts/work.py" record-plan-review .sdlc "$goal" --verdict <VERDICT> \
  --plan-sha256 "$(awk '/^Plan sha256: /{print $3; exit}' "/tmp/brief-$(basename "$goal" .md).md")"
```

`<VERDICT>` is the reviewer's SOUND, SOUND-WITH-REFINEMENTS or FIX-FIRST; a FIX-FIRST is recorded
too. This is the one writer of the verdict and the one emitter of its `plan_review` journal gate
(never also `loop.py emit` one). It refuses a sha the plan no longer holds, so record a
SOUND-WITH-REFINEMENTS as returned, before applying any refinement to the plan file. Copy the plan
to the branch unchanged; after any edit of either copy, re-review and re-record. With
`gates.plan_review.enabled`, `work.py pr` refuses to push unless an approving verdict is recorded
for the exact bytes of the plan on the branch (`landing.md`).

**A PR review posts only generation-bound evidence.** For `pr-review`, create the generation with
`python3 "${CLAUDE_SKILL_DIR}/scripts/review_context.py" brief .sdlc "$goal" --for pr-review
--artifact "PR#$pr" --output ".sdlc/state/review-manifests/$goal.json"`, then load its immutable
paths using `source <(python3 "${CLAUDE_SKILL_DIR}/scripts/work.py" review-paths .sdlc "$goal"
--manifest ".sdlc/state/review-manifests/$goal.json" --format sh)`. Run `python3
"${CLAUDE_SKILL_DIR}/scripts/reviewer.py" resolve .sdlc > "$REVIEW_RESOLUTION"`. For a resolved
`process` or `command`, run `python3 "${CLAUDE_SKILL_DIR}/scripts/work.py" run-resolved-review .sdlc
"$goal" --manifest "$REVIEW_MANIFEST" --resolution "$REVIEW_RESOLUTION"`. For a resolved
`subagent`, hand only `$REVIEW_BRIEF` to a fresh host subagent, then run `python3
"${CLAUDE_SKILL_DIR}/scripts/work.py" record-subagent-review .sdlc "$goal" --manifest
"$REVIEW_MANIFEST" --resolution "$REVIEW_RESOLUTION" --verdict approve|block|unblock --reason
"<reviewer result>"`. Then run `python3 "${CLAUDE_SKILL_DIR}/scripts/work.py" review-evidence .sdlc "$goal" --manifest
"$REVIEW_MANIFEST" --review-result "$REVIEW_RESULT"`. Only then post `work.py post-review .sdlc
"$goal" --evidence "$REVIEW_EVIDENCE" --verdict approve`, or the same evidence argument with
`--verdict block --reason "<the issues>"`. To clear a prior loop block on the same revision, publish
a fresh generation (every publication is new), have its reviewer return `unblock`, and post that
evidence with `--verdict unblock`; a block's own evidence cannot be reused. The CLI and writer
boundary reject a post-review call without that evidence. An `inline` route passes its verdict
explicitly to `run-resolved-review` with `--verdict` and `--reason`. An ambiguous post is recovered
with `work.py reconcile-review-post .sdlc "$goal" --evidence "$REVIEW_EVIDENCE"`, never by posting
again. See `landing.md` for all three.

**A block redispatches the fix to a fresh subagent — never resume the one that got blocked, or
the one that blocked it.** This specializes "one subagent PER PHASE" above to the fix→re-review
cycle any review gate (plan-review, pre-PR/code-review, post-PR/pr-review) or a failing `loop.py
verify` can start: the fix is still the SAME phase for `phase_report.py`/`loop.py log
agent_dispatch --role phase` purposes (a fix after a blocked post-PR review is still `phase:
implement`; a fix after a blocked plan-review is still `phase: plan`) — never a new phase name —
but it runs exactly like that phase's first attempt: fresh context, sibling not continuation,
never resume the blocked subagent's own conversation. Hand the fix subagent only the minimum it
needs: the reviewer's (or verify's) own blocking findings, the plan path
(`.sdlc/plans/<goal-stem>.md`), and the worktree path — never the maker's prior transcript, which
a fresh subagent by construction does not have anyway. The re-review that follows is equally
fresh — never the same reviewer subagent asked to look again — and is handed only its own prior
verdict (the block reason just posted or recorded) plus a live pointer to what changed since:
for `plan-review`/`code-review`, re-run the dispatch gesture above for that gate, redirect included
(a fresh `/tmp/brief-<stem>.md` pointing at the current plan file or worktree diff, and the brief a
re-reviewed plan's verdict is recorded against); for `pr-review`, publish a fresh `--output`
generation, whose brief already resolves to the live `gh pr diff <PR#>` — never the earlier
review's transcript pasted in by hand. Step 6's worked example (the
post-PR cycle) carries a code-enforced cap (`work.max_review_cycles`); plan-review and
pre-PR/code-review have no equivalent counter.

**A send-back the tier cannot converge escalates the tier; it never parks on "budget" (#2828).**
Token budget and tier size are not park reasons: neither is a human decision. The trigger is
mechanical, not a judgment the struggling tier makes about itself: run the verb **before any park
or fail after a review send-back, and at the latest on the second send-back at the same tier**:
`python3 "${CLAUDE_SKILL_DIR}/scripts/loop.py" escalate .sdlc "$goal" <tier> --after plan-review`
(`--after code-review` or `--after pr-review` for those gates; `--after` is required). Read the
first word of its stdout:

- `ESCALATE <next> effort=<e>` (exit 0) — re-dispatch the SAME phase fresh at `<next>` (haiku →
  sonnet → opus) and effort `<e>`, with the reviewer's findings, the plan path and the worktree
  path, exactly like any fix above. The verb has already recorded `model_choice` with the signal
  `escalated: <gate> send-back at <T>` in the action log when that is on (it ships on from
  `/sigma-init`) and in the journal when that is on (it ships off); pass `<next>` to
  `phase_report.py start --model` and `agent_dispatch --role phase --model`. `<next>` is this goal's
  ceiling from here on, for every later phase too — the router under-estimated the goal. Before
  each later phase dispatch (and on any resume), take the higher of the pick-time tier and
  `python3 "${CLAUDE_SKILL_DIR}/scripts/loop.py" escalate .sdlc "$goal" --show` (prints the
  escalated tier clamped to the current `model_selection_max_tier`, or `none`). The verb itself also reads that floor back, so passing the pick-time
  tier again still climbs rather than repeating a rung.
- `CEILING <T>` (exit 3) — no tier above `<T>` within `model_selection_max_tier` (`fable` is never
  an escalation target). Keep the existing fix → re-review cycle at `<T>`; only a fix that still
  does not resolve after a couple of rounds is "a failure you cannot resolve" (above) — park or
  fail it rather than cycling indefinitely.
- `OFF` (exit 3) — `model_selection` is not `"auto"`, so no tier is routed and there is none to
  raise (Claude phases inherit the session model; Codex maps `off` to its ordinary-work model, as
  above); the same fix → re-review cycle, then the same rule, applies.

The ladder has two rungs, so a goal escalates at most twice: the floor lives in
`.sdlc/state/escalation/<goal>.json`, control state written whatever the ledger, journal and
action-log settings are. A `model_host_overrides.codex` entry the resolver refuses (checked before
anything is written, as `resolve` does) makes the verb exit 2 with nothing recorded: fix it, re-run. On the
post-PR cycle `work.max_review_cycles` still parks at its cap whatever the tier. **Per host:**
Claude passes `<next>` as the Task tool's `model` (and `<e>` as its effort); Codex maps it with
`host-model codex --effort <e>` as described above and preserves `<e>`. A host with no per-subagent model override (Cursor)
cannot re-dispatch at another tier: the verb still decides and records, and the operator switches
the session model, or the goal is handled as at `CEILING`. The escalation is a real decision there; the re-dispatch is not enforceable. **On
Codex, a redispatched fix or re-review also sets `fork_turns="none"`** — the default copies the
parent's own conversation into the child, which is exactly the large, stale context this rule
exists to avoid — and passes the goal, phase, worktree and the artifact paths above in the prompt
instead.

**No phase subagent commits — only `work.py commit` does, once, at step 6.** This binds every
dispatched phase subagent (research, plan, plan-review, implement, review, retro, and each
individual slice under 3b), not only Implement, and it does not travel from one dispatch prompt
to the next on its own: **say it in each one**, the same way the brief above is handed to every
reviewer. The reason is `commit()`'s own short-circuit: it stages with `git add -A`, reads back
`git diff --cached --name-only`, and returns `"nothing to commit"` the instant that is empty —
BEFORE `_secret_refusal` (the credential scan) ever runs. A commit any phase made earlier in the
SAME worktree, by any means outside `work.py`, has already emptied that staged diff by the time
step 6 calls `commit()`: `git add -A` finds nothing new to stage, the scan never inspects what
that commit contains, and a credential inside it reaches the PR exactly as if the guard did not
exist. This shipped once already (#1756): telling only the Implement dispatch prompt left Review
and Retro free to run a bare `git commit` in the same worktree, and one of them did — caught safe
only because a human happened to re-check the diff by hand, not because anything here stopped it.
A phase that wants a local checkpoint leaves the worktree DIRTY and says so in its own handoff; it
never stages or commits anything itself.

**Match the ceremony to the goal** — after Research, resolve the lane it measured. In **local mode**
run `python3 "${CLAUDE_SKILL_DIR}/scripts/discovery.py" lane "$goal"`; in **github mode** the goal is
an issue number with no frontmatter, so read the lane from Research's phase note on the issue
timeline you already fetched. Either way an unsized goal is **`medium`** — unknown gets more rigour,
not less. On **small**, plan in a few lines and keep the retro to one; on **large**, work the design
out before planning and consider splitting it into several goals. **Plan-Review runs in full at every lane**.
The lane is a *starting* call: if implementation shows the goal is bigger, escalate and say so in the
phase note. **Park instead of forcing through**
if you hit any of:
- a hard checkpoint / a decision only the user can make,
- an **irreversible or expensive action** (deploy, delete, overwrite, spend, migrate) — NEVER
  run one unattended unless `python3 "${CLAUDE_SKILL_DIR}/scripts/loop.py" spend-approval .sdlc
  "$goal" --action -` (step on stdin, quoted `<<'EOF'` heredoc) prints
  `APPROVED` (needs `sigma:spend-approved=<label>`; add `--class <name>` when the step has one: only
  `spend` is honoured, any other class is DENIED); else park. Never self-grant,
- a failure you cannot resolve — record THIS one as `failed` (see step 6): parked means
  "needs a human decision", failed means "needs a fix"; the queue separates the two.

**3a. Register you're driving this goal, THEN cut the worktree if enabled.** Regardless of
`config.work.enabled`, first register that you're the one driving this goal right now:
`python3 "${CLAUDE_SKILL_DIR}/scripts/loop.py" agent-start .sdlc "$goal" --pid $PPID` — `$PPID` is
YOUR OWN stable process id, captured once (same contract `--session-pid` already documents), not
any individual command's own. **A nonzero exit on ANY host means task ownership was not
established: stop this goal before `work.py start`; another live task may own its worktree** --
Claude refuses the same way when a live, different pid is already registered. This local
marker file is what a second session's picker cross-checks before treating your claim as an
abandoned one (#1197): a picker's own process always exits within moments of making a pick, long
before your real work here starts, so nesting this call behind `work.enabled` would leave a
fresh `/sigma-init` install (where `work.enabled` ships `null` -- not yet decided, #2255 -- and
reads as off either way until `/sigma-setup` or a human turns it on) with no way to tell a
live worker from a dead one. `agent_watch.enabled` (also off by default) gates a SEPARATE,
genuinely opt-in dead-agent NOTIFY tick only — never this write.
With `config.work.enabled` on: `python3 "${CLAUDE_SKILL_DIR}/scripts/work.py" start .sdlc
"$goal" --session-pid "$PPID"`. **Pass `--session-pid "$PPID"` — the same value you just gave
`agent-start --pid`, and the same one every `loop.py` call takes.** It is how `work.py` knows the
live agent marker it is about to read is YOURS: this command runs as a short-lived child process,
so without being told it reads your own registration as a second session in your worktree,
refuses the resume, and silently skips the feature-branch rebase upkeep on every pick (#1687).
It cuts a fresh worktree and branch from `<remote>/<base>` — which **is** the goal-start
rebase: nothing to replay, so it cannot conflict or strand a half-applied tree in an unattended
run. `<base>` is resolved **per goal**, for **GitHub-backed goals only** (github discovery, and
a goal whose id is an issue number): the unit the issue declares (`feature/<name>`, from a
`feature:<name>` label or a `Feature:` body marker), else `work.base`, else the branch the loop
was started on. In local-goals mode nothing is read and `work.base` is always the answer. Read
the line it prints — it names the base it actually cut from. **If that line says the declaration
could not be read**, the base is a fallback, not an answer (`base_resolved: false` in the work
record): check `gh auth status` first, and if this project uses `feature:` units, park the goal
rather than opening a PR against a base nobody resolved. If it uses none, the configured base
was the right answer anyway.
**If this command exits 4, STOP this goal and do NOT `record` it** (#2009). Exit 4 means it was
resuming an existing worktree, found it behind its base, and could not bring it forward — so
every phase after this one would read stale code, which is what makes a resumed goal produce a
wrong research dossier and a wrong plan. It has already moved the goal out of the claimed state
for you: a genuine conflict is **parked** (a person has to resolve it), and a transient failure
**releases the claim** so the next pick simply retries — a second consecutive transient parks
instead, so an outage reaches a human rather than spinning. Recording anything on top of that
would undo a transition that is already correct. Every other exit code is unchanged, and the
other `REFUSED — …` lines this command can print are still exit 0.
Do **every edit for this goal inside that worktree**; the human's checkout must never move,
and never change branch (it would rewrite `.sdlc/goals/` underneath you). Bookkeeping is the
exception and stays in the MAIN checkout — keep passing `loop.py`/`ledger.py` the same `.sdlc`
path as always, never the worktree's stale copy of it. `loop.py verify` finds the worktree by
itself. Feature off → work in the checkout exactly as before (the agent-start registration above
still runs either way).

**3b. Independent slices? Run the wave — don't queue it.** Once the plan exists, if it declared
slices in `.sdlc/plans/<goal-stem>.slices.json`, compute the dispatch plan:
`python3 "${CLAUDE_SKILL_DIR}/scripts/slices.py" plan .sdlc "$goal"` (on Codex add `--host codex
--goal-worktree <absolute-path> --tier <tier-or-off> --effort <effort>` using the goal worktree from `work.py start`). Before every Codex
slice dispatch, resolve its goal or downgraded-step tier with its canonical effort: `python3 "${CLAUDE_SKILL_DIR}/../sigma-model/scripts/predict.py" host-model codex "<tier-or-off>" .sdlc --effort "<effort>"` and pass the returned model ID and effort to the subagent. Also give `slices.py plan` that source tier as
`--tier <tier-or-off>`: it resolves the mapping itself before printing a `dispatch: session` command, so a raw or stale model ID cannot enter its fresh interactive Codex process.
planning when `work.enabled` is off; run that goal as one unit. Without a goal worktree and readable
manifest, the Codex session line refuses to give a runnable command.
It groups the runnable slices
into **waves** — each wave mutually non-conflicting by declared files, capped at
`parallel.max_concurrent`. Do ONE wave at a time: dispatch each slice as a **subagent** (fresh
context). The plan's `isolation: worktree` is a requirement, not proof the host provided it. If
Codex's subagent tool shares the checkout, run those slices sequentially; concurrent work
requires a distinct worktree for each slice and a verified landing path. For each dispatched slice:
log the dispatch
(`python3 "${CLAUDE_SKILL_DIR}/scripts/loop.py" log .sdlc "$goal" agent_dispatch --thread
<slice-id> --role slice --phase implement --model <tier>`) and register it for the death-watch the same way 3a
does, one level down (`python3 "${CLAUDE_SKILL_DIR}/scripts/loop.py" agent-start .sdlc "$goal"
--pid $PPID --thread <slice-id>` — giving intra-goal slice parallelism its own
independently-tracked pid per thread). **`--model <tier>` is now REQUIRED on this dispatch too
(#2514) — `loop.py log` refuses a `--role slice` dispatch with no `--model`.** `<tier>` here is the
GOAL's own ceiling tier (the same `model_choice` value resolved at the top of this doc) unless
this specific slice's plan step was itself downgraded via `resolve-step`, in which case pass that
resolved tier instead. Land the wave — logging each landed slice first (`agent_done
--thread <slice-id> --role slice --result <...>`) — then re-run `plan` for the next. **Never**
dispatch a slice with an unattended `claude -p` or `codex exec` — a second worker on one `.sdlc`
breaks every state file here. A slice marked `dispatch: session` will not fit one subagent's
context: **print the plan's interactive Claude or Codex command and let the human start it** —
never start it yourself. No manifest, or `parallel.enabled` off → run the goal as one unit,
exactly as before.
