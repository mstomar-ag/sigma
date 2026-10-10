# Changelog

All notable changes to Sigma are recorded here, newest first.

## Unreleased

- **Upkeep part B, slice 8: the resolver launcher (#949).** A new library, `feature_upkeep_launcher.py`, with no caller yet and off by default. It runs one capped headless session with an environment built from nothing, the prompt on stdin, only confirmed flags (the rest are labelled UNVERIFIED and refused), a group kill at the wall clock, a before-and-after hash of the directory, and charging: a normal run its metered cost, a killed or unmetered run the full per-run cap. Tested only against a fake executable; no real model call was made. `bounded_run.run_group` gains an optional `stdin_path`; the model SDK module joins the no-network list.
- Upkeep part A, slice 6 wiring: the pass re-reads tips, uses the engine runner and hooks policy, writes acks, limits re-anchoring, skips deleted goal branches and writes the ledger note (#1016).
- Upkeep part A, slice 8: user documentation and config reference for the scheduler, backups, restore and prune, the opt-in audit across every entry point, and one recorded local end-to-end run on a scratch bare remote (#924).
- Upkeep part A, slice 7: the scheduler, the detached bounded job, outcome notes and the doctor rows (#923).
- Upkeep part C, slice 5: read-back classifier and pending-landing record (#936).
- Upkeep part C, slice 8: the landing engine's front half, `feature_land.py land <unit>`, ends in a rehearsal and never merges (#937).
- Upkeep part C, slice 8: `feature_land_approval.peek` checks a unit approval without consuming it (#937).
- Upkeep part C, slice 8: the PR-creating writer and the scratch-worktree removal are registered in the write surface (#937).
- Upkeep part C, slice 9: the landing engine's back half, `feature_land_merge.py`, behind the upkeep gate and the explicit `--merge` flag: the guard as the last gate, a pending record before the call, a head-pinned merge, a read-back, record once and a branch-existence check; `verify_merge.py` maps engine outcomes only while the gate is open (#938).
- **Upkeep part B, slice 5: a changelog-only unit conflict is resolved by the heading-aware union (#947).** Behind the
  existing upkeep opt-in and `conflicts.resolve` set to `mechanical` or `agent` only, and with no model call. A unit rebase
  that stops on a conflict in the changelog alone is resolved, checked (no edit outside the file, no new marker or
  whitespace finding, every original commit accounted for, then the verify command), stamped with the original author kept,
  continued with repository hooks pointed away, and pushed only through the one atomic backup push; any refused check parks the
  unit instead, and a resolved replay is never pushed without a backup ref. After the push the acks of the original commits
  are recomputed for the replayed ones into a per-machine runtime file the ack reader unions in (the tracked store is never
  edited), the resolution record and ledger note are written, and a prior finding gets the record before it is closed. The
  guard budget refusal now names `SIGMA_WATCH_CALL_TIMEOUT` and its current value as the operator's lever. With the opt-in
  closed every existing path is unchanged.

- **Upkeep part B, slice 6: unit conflicts are parked, with one capped finding per conflict (#946).** Behind the
  existing upkeep opt-in only. A unit rebase that stops on a conflict nobody resolves now ends with the new `parked`
  outcome: nothing is pushed, a capped brief (files, commits, bytes and time; no network call; marker strings and kit paths
  neutralised) is rendered before the scratch worktree is dropped, and one finding is filed per conflict rather than per
  branch tip. The filing helper gains a no-goal path with a scoped idempotency key that looks up no unit from a goal, makes
  no metered classification, writes an unaddressed ledger note and tells no owner. The filed store keeps park slots as a
  record with issue number and a reused flag, a later clean pass comments then closes the finding through `gh_api`
  (never a reused one, never one a live slot still points at), and the doctor shows an age row from the park's own marker.
  With the opt-in closed every existing path is unchanged.

- Upkeep part C, slice 3: test substrate for REST merges (#935).
- **Upkeep part A, slice 6: the pass under the opt-in (#922).** Behind `upkeep.enabled` only; closed means
  every path is unchanged. The pass module gains the engine runner settings (no prompt, no editor, no ref
  updates by rebase, no signing, per-verb timeouts), the hooks policy (off for replays and clean pushes, on
  after a resolved conflict), the tip re-read with at most two restarts, the per-machine acks file, the
  re-anchor limit, the skip reason for a goal branch deleted after its pull request merged, and unaddressed
  ledger notes written in-process. A goal cut from a unit branch records the tip it was cut from, and a goal
  rebase replays with `--onto` from that tip (goals without a record replay the old way). The attended
  rebase door takes the unit lock in its caller and pushes with a lease on the exact tip it saw. With the
  gate open, picking such a goal makes one extra read.
- **Upkeep part C, slice 7: cross-repo unit check (#934).** A unit-keyed sibling lookup
  (`cross_repo.unit_sibling_check`) and its guard (`work.unit_sibling_guard`, registered in the
  enforcement table). It refuses a landing while another repository's half of the unit has not landed,
  and when the lookup cannot answer. Read-only, inert while the upkeep gate is closed, and nothing
  calls it yet.
- **Upkeep part B, slice 3: the proof gate's checks (#944).** A new library, `conflict_proof.py`, holds the checks that
  decide whether a machine-resolved unit rebase can be trusted, as pure functions that each return named refusals:
  a scoped conflict-marker and whitespace scan, a stage-0 baseline and per-path line-multiset comparison, commit
  pairing by stop record and authorship key that parks on ambiguity, and a fail-closed Python test counter. Nothing
  calls it yet, so nothing changes while the upkeep gate is closed.
- **Upkeep part B, slice 4: the stamp and the resolution record (#945).** A new library,
  `feature_upkeep_resolution.py`, with no caller yet. The stamp is one lowercase body trailer
  (`sigma-resolution: <level> <run id>`) built after a blank line so the pull-request arrival
  classifier still reads the title, and it carries no number, no closing keyword and no registered
  marker. The stamping commit is made at the stop with the original author name, email and date
  kept (a plain commit loses them; an authorship comparison refuses it), and `rebase --continue`
  keeps it unchanged. Measured: the continuation runs only two commit hooks and never signs, and
  the engine commit runs the same two. The record store is atomic, refuses symlinks and is pruned
  by age; one unaddressed ledger note and an optional finding comment follow. Off by default.
- **Upkeep part B, slice 2: a heading-aware CHANGELOG union, off by default (#943).** `work.py` gains
  `_union_headed`, a sibling of `_union_diff3` (left byte-identical): on a conflict where both sides
  only inserted, an entry added under `## Unreleased` stays under it when the base side cut a version
  heading at the same point, instead of landing under the version just cut. Every other shape (a
  bracketed or unknown heading, a link footer, a heading on the unit's side) parks as before.
  `work.rebase` reads the upkeep gate and passes the sibling to `_union_rescue` only while
  `upkeep.enabled` is true and `conflicts.resolve` is `mechanical` or `agent`; with the gate closed the
  in-pass goal replay, `ensure_fresh` and the merge gate's BEHIND remedy behave exactly as before.
- **Upkeep part A, slice 3: drift measure, per-unit state and the due rule (#919).** Two new
  library modules, `feature_upkeep_drift.py` and `feature_upkeep_state.py`. The first counts
  pull-request arrivals on a base branch and on a feature branch by author date over a bounded
  first-parent walk, derives the drift threshold from the window and burst rates and clamps it, and
  answers UNKNOWN, never zero, when the walk cap is hit, git fails, the repository is shallow or
  commits exist but none classify. The second keeps one atomically written state file per unit
  (unreadable state reads as overdue) and the due rule: a cooldown since the last attempt, a backstop
  since the last success and a longer backstop for dormant units; an attempt that cannot be recorded
  never starts a pass. Both take the settings of the upkeep gate, a growth disposition records the
  retention rule for the new store, and nothing calls them yet, so there is no behaviour change.
- **Receipt-key parity under the upkeep gate (#932, upkeep part C, slice 4).** One owner-id rule, the unit's
  branch, through a new shared helper in merge_observation.py. While the upkeep gate is open the
  unit-completion observer keys a landing by the branch like the rebase landing helper, so one landing
  is recorded once; a landing already recorded under the bare unit key keeps that key. With the gate
  closed nothing changes.
- **Shared "landed" predicate (#930, upkeep part C, slice 1).** New feature_landed.py: one function that says whether a
  unit's tip is landed on a base, as a structured verdict (LANDED, NOT_LANDED or UNKNOWN, with how, the base sha, the
  pull request and the merged head). Ancestry is checked first through an exit-code-preserving bounded runner, then a
  separate head-aware merged-pull-request read (paged, bounded, truncation never read as "none"); anything it cannot
  prove is UNKNOWN. Also a repository-slug helper that checks the remote names the same repository. The drift watcher
  adds an optional landed field only while the upkeep gate is open; with the gate closed nothing changes.
- **GitHub write plumbing in gh_api (#931, upkeep part C, slice 2).** New REST-only helpers with no caller yet: a pinned merge that requires an explicit method, repository and 40-hex head pin, commit parents, branch rules, repository settings, a typed non-draft pull request create, and a bounded runner that keeps the exit code and failure class. The landing writer joins the receipt allowlist; the doctor row and the cloud-sessions page name the degraded features. Inert unless the upkeep gate is open.



- **Backup refs for a rewritten unit tip: create, restore, prune (#921, upkeep part A, slice 5).** With
  `upkeep.enabled` true, the pick-time rebase pass now keeps the old tip of a unit branch as
  `refs/sigma/backup/<unit>/<UTC stamp>` in the same atomic push as the leased update (both refs land or neither; a
  name collision is refused, and a unit name over 220 bytes is refused as `name-too-long` before the replay). With it
  off the push is byte for byte what it was. New `feature_backup.py` also holds a `restore` command, leased on the tip
  you state, and a `prune` command that keeps the newest 5 backups per unit and removes only those older than 14 days,
  by exact prefix, with the `--delete` flag and one lease per ref; both are `feature_rebase.py` verbs behind the gate
  (exit 3 while it is closed). `backup.former_prefixes` is a new key that ships empty. The never-delete guard pins the
  two new sites, the write-surface inventory gains four rows, and the deletion rule (section 13b of the branching
  model), the README and the enforcement table carry the exception. Nothing prunes by itself yet.
- **Shared verify runner and unattended git runner (#920, upkeep part A, slice 4).** New bounded_run.py:
  a command run in a process group of its own under a wall-clock budget, the whole group stopped
  (SIGTERM, then SIGKILL) on overrun or when a stop file appears, a lifeline that stops the tree if the
  caller itself is killed, bounded output, a distinct no-command outcome, and a refusal unless the checkout
  is a scratch worktree with the git-local shell-command trust. New unattended_git.py: the rebase engine's
  runner contract with no prompts, editor or askpass helper, signing off, a hooks policy, a fetch that
  leaves the fetch record alone and starts no maintenance, and a required time limit per git verb. The
  doctor gains a bounded local-probe helper. Nothing calls any of it yet and `feature_sync._run` and the
  doctor's `_real_run` are unchanged, so there is no behaviour change.
- **Never-delete guard and write-surface ratchet widened ahead of backup refs (#918, upkeep part A,
  slice 2).** The guard now also flags `git push -d`, colon-empty and backup-namespace refspecs, a
  REST `DELETE` in any spelling, `update-ref -d`/`--delete` and `gh_api` delete helpers, and its
  exact-site pin grows by one reviewed site (`gh_api.py` `remove_label`, a label delete). The
  write-surface scanner now classes a push-scoped `--delete`, colon-empty or forced refspec as
  destructive and sees `update-ref --delete`, `--method=DELETE`/`-XDELETE` and the `gh_api` write
  helpers by name. One pinned sentence states what the guard enforces about `refs/sigma/backup/`.
  No inventory row changes and no runtime behaviour changes; nothing calls the new rules yet.
- **Upkeep gate, off by default (#917, upkeep part A, slice 1).** New `feature_upkeep.py`: the one
  total reader of a new `upkeep` config block (it never raises, `enabled` must be the JSON boolean
  `true`, and any invalid key closes the whole block), a machine opt-in variable `SIGMA_UPKEEP_JOB`
  (exactly `1`, plus `ledger.enabled`), and an entry-point decorator. The shipped config template
  gains a disabled `upkeep` block with notes, and a recording-trap test proves that while the gate
  is closed nothing is spawned, fetched, sent to a model or written. Nothing calls the gate yet, so
  there is no behaviour change; existing projects do not receive the block, and the pick-time switch
  `work.rebase_upkeep` is untouched.

- **`sigma-prd-intake`: a PRD door into the Dossier pipeline (#822, refs).** A PRD file of any
  shape becomes one cited Dossier per business outcome plus one `epic` umbrella. Every answer needs
  a verbatim PRD quote (>= 20 chars; the check proves it exists, not that it supports the answer);
  silent, vague or contradictory answers become `open_` questions; a repository source file is
  refused as the PRD. `dossier.py` gains an additive `prd_source` (a `### Source` block after the
  fence; output byte-identical when absent). Not built: stdin/issue-number PRDs and the fast lane,
  so #822 stays open. Measured: the new tests in `tests/test_prd_intake.py`; no timing claim made.
- **Seventeen issue WRITE sites go REST first (#895, slice 3a; refs #801).** Comment, create, edit body, close,
  add/remove label and add-assignee now call new `gh_api` helpers (`comment_issue`, `create_issue`,
  `edit_issue`, `close_issue`, `add_labels`, `remove_label`, `add_assignees`) through `GitHubSource._issue_*`
  wrappers. Sites: `dossier`, `blockers`, `promote`, `unpark` (2), `assign`, `triage` (3) and in `sources.py`
  `release` (2), `complete`, `append_to_body`, `create_dependency` (3) and the priority-label mirror (add plus removes); each
  keeps its failure arm, return value and message. Fallback policy, one table in code (`gh_api.WRITE_POLICY`):
  a REST write falls back to the matching `gh issue ...` at most once and is never retried; a primary rate
  limit falls back for any write, a 5xx only for idempotent writes (add/remove label, close, edit body,
  add-assignee), and a timeout or transport failure after send, a 5xx on a comment or create, 401/404/422,
  permission 403, a secondary limit, a proxy block, a refusal and anything unparsed never do; none in a cloud
  session or with `SIGMA_GH_GRAPHQL=off`. Writes are classified from the status and gh stderr only, never from
  the message that embeds the body, and use the shared fallback log but never the read breaker. `feature:*`
  labels are refused before any create or add-label unless they exist (a REST lookup that fails also
  refuses). Label minting divergence, accepted and pinned: REST can create a missing non-feature label
  (`priority:P*`, triage's arbitrary add-label) where `gh` failed. `@me` assignees resolve through one
  `GET user`; if that is refused (403), nothing is sent and `gh` is not tried; team slugs and silently
  dropped assignees are refused. `remove_label` still ignores a 404, now matched on the status alone (a
  vanished issue is swallowed like an absent label). NOT atomic: the priority-label mirror is now add then
  one remove per stale label, sequentially (was unordered parallel); a failed remove leaves two priority labels,
  and the loop retries it after a demotion but never removes the stale lower label after a promotion.
  Lifecycle label swaps are unchanged (still one GraphQL document, so still unavailable in a cloud session).
  The direct-`gh` ratchet drops from 74 to 57 sites (measured by `scan()`); `docs/launch/write-surface.json`
  was regenerated, and its scanner cannot see a write made through a `gh_api` helper. Still open on #895:
  `note()` (its owner-accepted duplicate-on-retry ruling conflicts with the no-retry rule), every
  `label create`, lifecycle swaps, board and PR writes. Verified with injected-runner tests only.
  Unmeasured: REST label auto-create; that a primary rate-limit rejection never partially executes a write;
  exact 404 bodies; secondary-limit wording; assignee drop behaviour and `GET user` cost; per-write request
  counts and latency against `gh issue ...`; a real cloud session.
- **Ten `gh issue list` reads go REST first (#895, slice 2c; refs #801).** New `gh_api.list_issues_gh`: a
  paged REST list (newest created first, like `gh issue list`), then at most one `gh issue list` fallback
  on rate limit, 5xx or transport errors, never in a cloud session, never on client errors or a bad page
  (empty, malformed or non-list output is an error, not an empty board). Sites: `auto_unpark` (2),
  `reconcile` census, `assign._active_members`, `doctor` (6, plus its census read); each keeps its fail-open
  arm, and a failed `auto_unpark` read still can never remove a label. Breaker and log are shared with
  `read_issue` when an `sdlc_dir` exists (so a rate-limited census can open the breaker for single-issue reads); doctor stays read-only and writes neither. The direct-`gh` ratchet
  drops from 84 to 74 sites (measured by `scan()`). Verified with injected-runner tests only. Unmeasured: a
  real cloud session; a 5000-cap board read is up to 50 sequential REST requests per read and per
  reconcile census tick (derived, not measured, against `SIGMA_WATCH_CALL_TIMEOUT` 120 s); doctor latency
  (~45 s per site, ~135 s multi-state, derived from the 15 s timeout); gh's newest-created default order
  (assumed, not re-verified); GraphQL points saved (#1829's figure, not re-measured).
- **Regression record builder (#874, slice 1 of #870).** `python3 evals/regression/record.py build <run_dir>` writes one
  `sigma.regression-run/v1` JSON record from a run's plan, research, plan-review verdict, verify state, journal, action log,
  review evidence, commit order and per-phase tokens. Action-log rows are filtered by the plugin's own `actionlog.INTERNAL_KINDS`
  (imported, never copied); each stream carries `source` and `present`, and a run directory with absent streams still builds.
  Phase ends are recorded as `call-existence` evidence only, and the record says so. Nothing is posted or run against a
  model; read cost is linear in journal and log size and was not benchmarked.
- **Ten more `gh issue view` reads go REST first (#895, slice 2b; refs #801).** `auto_unpark`, `blockers`,
  `promote`, `unpark`, `reconcile` (3 sites), `triage` (2) and `brainstorm` now call `gh_api.read_issue`
  (same fallback policy as slice 2a; each site keeps its own failure arm). `to_gh_shape` gains `number`
  and `title`, and reports a merged PR (REST `closed` + `pull_request.merged_at`) as `MERGED`, so a
  merged-PR blocker still reads resolved. `triage._resolve_missing_picks` and `brainstorm` have no
  `sdlc_dir`, so they write no breaker or log. The direct-`gh` ratchet drops from 94 to 84 sites
  (measured by `scan()`). Live read on 2026-10-10 of merged PR #928: `state closed`, `state_reason null`,
  `pull_request.merged_at 2026-10-09T17:46:12Z`. Verified with injected-runner tests. Unmeasured: a real
  cloud session, bot-author spelling beyond one public sample, call counts at scale, `unpark` latency on a
  comment-heavy issue, and an old closed issue with a null `state_reason`.

- **`python3 evals/golden/verify.py` (#881, slice 1 of #873).** Checks every golden task under `evals/golden/` (hashes, origin, hidden tests red on the start tree and green on the reference, reference diff against `allowed_paths.json`, optional naive patch). No tasks ship yet: with none it exits 0 and says `nothing verified`. CI runs it after the quality gate.

## 1.0.5 — 2026-10-09 — REST-first issue reads, a verify fix, and faster CI

- **CI runs the suite in parallel.** The one leg that runs on a PR (macOS 3.12) installs `pytest-xdist` and runs `pytest tests/ -n auto`: 21 min measured on a runner (PR #925) against 47-64 min serial. `workflow_dispatch` takes a `full` input (default true); `full=false` runs only the PR leg on any ref.

- **`sources.py` issue reads go REST first (#895, slice 2a; refs #801).** The seven reads
  (`fetch_comments`, `release`, `complete`, `fetch_author`, `fetch_body_labels`,
  `fetch_comments_strict`, `append_to_body`) now call `gh_api.read_issue`: a REST GET, then at most one
  `gh issue view` fallback on rate limit, 5xx or transport errors, never on client errors, a proxy
  block, or in a cloud session. A breaker (`.sdlc/state/gh-rest-breaker.json`, 3 failures, 300 s
  cool-down via `SIGMA_GH_BREAKER_COOLDOWN`) and a 200-entry fallback log (`gh-fallback.json`, no error
  text) are written only with an `sdlc_dir` and are #708 symlink-vetted. The strict comment read now
  reads every page instead of one `gh` call. The direct-`gh` ratchet drops from 101 to 94 sites
  (sources.py 35 -> 28, measured by `scan()`). Verified with injected-runner tests only. Unmeasured:
  real 429/403/5xx and transport stderr wording (inferred), any live cloud run, and live request
  cost. Measured once on a public bot-filed GitHub Skills exercise issue on 2026-10-09: issue author gh
  `app/github-actions` vs REST `github-actions[bot]` (mapped); comment author gh `github-actions` vs
  REST `github-actions[bot]` (not mapped yet); REST comment `node_id` equals gh's comment `id`.

- **CI no longer runs on every merge to `main`.** The workflow triggers are now `pull_request` (macOS 3.12, in parallel), the weekly schedule (full matrix) and `workflow_dispatch` (full matrix, on demand). A merge to `main` used to start a second full macOS run of code its PR had just tested.

- **Verify no longer fails a plan that selects a whole test file containing an expected failure.** The planned-tests check required every selected test to print `PASSED`, so a single `XFAIL` made `loop.py verify` exit 1 with "planned tests did not all pass" while the suite was green, and the resulting plan edit voided plan-review. `XFAIL`/`XPASS` now count as accounted for; `SKIPPED` still does not, and the error names the first unaccounted tests.

## 1.0.4 — 2026-10-09 — GitHub GraphQL capability check (cloud sessions, slice 1)

- **GitHub GraphQL capability check, detection and reporting only (#801, slice 1).** New
  `gh_api.py` (capability check plus a REST issue/PR helper nothing calls yet), a third proxy shape in
  `gh_session.graphql_unavailable`, an advisory `/sigma-doctor` row naming the features turned off, and
  a no-new-direct-`gh` ratchet test. No caller is migrated and cloud-session support is not claimed;
  migration is in follow-ups. See [cloud sessions](docs/cloud-sessions.md).

## 1.0.3 — 2026-10-08 — Codex-only autonomy parity

- **Codex autonomy parity (opt-in, #823).** A Codex-only supervised runner checks the enabled
  plugin's exact install, uses `codex exec --approve-for-me`, excludes duplicate Codex workers,
  bounds captured output, and handles child exit status alongside quota/stop text. Optional
  Codex review and model-selection overrides leave Claude's existing route untouched. See
  [the safe migration sequence](docs/codex-autonomy.md). The synthetic controls and independent
  review passed; a full Codex goal-to-merge run and actual quota exhaustion are not yet measured.

## 1.0.2 — 2026-10-07 — a page on running the loop unattended

- **Running unattended.** New `docs/running-unattended.md` records the permission setup a headless or overnight run needs, what was observed (the `$PPID` gesture is denied under `acceptEdits`), the workaround, and what is still open (#721). Documentation only; no code change since 1.0.1.

## 1.0.1 — 2026-10-07 — the opt-in spend-approval marker and the partial-park self-heal

- **Spend approval (opt-in).** `loop.py spend-approval <dir> <goal> --action "<step>"` is an audited, single-use exemption from the loop's "never run an irreversible or expensive action unattended" park. It is honoured only when `spend_approval.enabled` is true (off by default), the issue's first body line is exactly `sigma:spend-approved=<label>`, the issue author is in `spend_approval.approvers` and is OWNER, MEMBER or COLLABORATOR, and an audit comment posts first; anything else parks as before. It is a per-use go-ahead, not a dollar cap, and a loop under the operator's own login could write the marker itself (the audit comment is the trace), and the author check is on who opened the issue, so any repo writer can add the marker to an approver's issue (#722).
- **Partial park swap self-heals.** A park whose label swap added `sdlc:parked` but failed to remove `sdlc:goal` now repairs the goal+parked contradiction instead of leaving it for triage; the repair uses the same GraphQL budget whose exhaustion usually caused the failure, so expect a few seconds of backoff (#722).

## 1.0.0 — 2026-10-07 — the first public release

Sigma Loop 1.0.0 is the first public release: guardrails and an overnight autopilot for an AI coding agent,
run from your backlog on GitHub (issues and a Projects board) or from local goal files.

- A seven-phase SDLC for every goal: goal, research, plan, plan-review, implement, review and retrospective.
  Plan-review is an adversarial review of the plan before any edit.
- In the autonomous loop the phases are checked, not just asked: `loop.py record done` and `work.py merge` refuse
  a goal unless the action log shows research, plan, an approved plan-review bound to the plan's bytes, implement,
  an approved review and retro (`gates.phase_record`, on in a fresh `/sigma-init`). The record proves the phases and
  verdicts were recorded in order, not that the work was good; interactive work outside those commands is not gated.
- `/sigma-loop` drains a backlog autonomously with fresh phase agents, one worktree, branch and verified pull request
  per goal; `/sigma-goal` runs one goal with an approval gate at each phase boundary; `/sigma-doctor` checks the
  setup and prints the fix; `/sigma-init` and `/sigma-setup` scaffold and adopt the project layer.
- Work is tracked where it already lives: the `sdlc:*` label model on GitHub issues and a Projects board, plus a local
  action log (`/sigma-log`, `/sigma-status`) and one renderer that builds every status line.
- Supported at launch: Claude Code on Linux (Python 3.10 to 3.13) and macOS (Python 3.12), in local-goals and github
  modes; each is a cell CI runs. Codex, Cursor and Windows are experimental.
- Safe by default: nothing sends data off the machine, spawns a background process or consumes quota without the
  operator opting in. Released under the MIT licence.

<!-- release-notes:end -->

The detailed development log of everything folded into this release follows; it is not part of the release notes.

- **The default loop now records every SDLC phase, and `record done` is refused without them** (#684, class B3). A live
  run with model tiering `off` let one worker do every phase inline: no research, plan, plan-review or retro was recorded
  and the independent review lived only in prose. `phase_report.py start|end` now write phase rows to the action log,
  `work.py record-plan-review` (new `--agent-id`) and the new `work.py record-review` write verdict rows, and
  `loop.py record ... done` (every mode, local-only included) and `work.py merge` refuse unless research, plan, an approved
  plan-review bound to the plan's bytes, implement started after it, an approved review and retro are on record; where the
  host dispatches subagents each phase must name its own agent id. The refusal names each missing item, the command that
  records it and the lever. `loop.py phases <dir> <goal>` shows the state and the next step; `loop.py waive-phases` waives
  research and retro only (recorded, visible). `gates.phase_record.enabled` ships `true` from `/sigma-init`; an absent key
  is off, so an adopted repo is not retro-fitted. The record proves boundaries and verdicts were recorded in order, not that
  the work was good. The loop costs more: on one live task the default loop cost about $2.1 and 8 minutes against $0.88 and 3 minutes before (measured, three runs; the PR has the table).

- **`/sigma-init` no longer replaces a corrupt or non-object `.sdlc/config.json` with a fresh one** (#625). A truncated
  or hand-broken config, or one whose top level is an array, string or `null`, was read as `{}` and written back, so every
  key the user had was lost with no refusal. `/sigma-init` now stops with exit 2 before writing anything, names the file,
  what is wrong and the lever (repair it by hand, or back it up and delete it on purpose); `setup.py configure` refuses the
  same way instead of a traceback. Config writes in `setup.py` and the `--cursor` step are now temp file plus rename. A valid
  config behaves as before. No `.bak` is written because no path replaces an existing config. The write has no `fsync`, so a
  power loss can still leave an empty file; `preflight.set_local_only` still raises a traceback (no write) on a non-object config.
- **A repository that already has a verify command is told to trust it, once per checkout** (#615, epic #613). Sigma
  runs a repository-configured verify command only after the checkout grants Git-local trust (#422), and that trust is
  neither committed nor cloned, so a migrated repository or a teammate's fresh clone used to get `[ok] verify` from
  `/sigma-init` and then a refusal at `loop.py verify` (with `verify.enforce` on, every goal stalled). `/sigma-init` now
  prints a `[trust] verify` line with the command and the one gesture instead of `[ok]` (it does not grant the trust
  and no flag answers it), `/sigma-doctor` has a `verify command trusted in this checkout` row, the `decline` message
  no longer advises a hand edit that cannot run without trust, and `docs/upgrading.md` has a section on it and no longer
  says no migration is needed. The trust policy is unchanged. The issue proposed an `[ask] verify-trust` line naming
  `verify_detect.py confirm`; a `[trust]` line naming the Git gesture is used instead, because `[ask]` means "re-run
  with a flag" to hosts and `confirm` cannot trust a committed command that is not a detected candidate.

- **Adoption no longer switches off your git hooks** (#614, epic #613). `/sigma-init` (and `/sigma-setup`, the
  same flow, on every host) used to set a repository-local `core.hooksPath` naming a directory that nothing creates,
  so git ran none of the repository's hooks and a global `core.hooksPath` (an org secret scanner) was shadowed too,
  even when adoption then failed; a repository that already set its own value (husky) was refused. Adoption now never
  writes the key. Re-running `/sigma-init` in a repository an earlier release adopted removes the stale key and says
  so in one line; `/sigma-doctor` fails a row with `git config --local --unset core.hooksPath` while it is there;
  `docs/uninstall.md` and `tools/readiness/leftovers.py` cover it. `setup.py hooks` now refuses when the directory
  does not exist. Acceptance record 218 is amended.
- **The solution is named Sigma Loop; the plugin and marketplace are `sigmaloop`** (#524, epic #522). Install with
  `claude plugin install sigmaloop@sigmaloop` (or the Codex and in-session forms) after adding the public repository,
  `https://github.com/Agrim-Intelligence/sigmaloop`; the README Quickstart, `docs/uninstall.md`, `SECURITY.md`,
  `SUPPORT.md` and `CONTRIBUTING.md` carry the new name and install id, `docs/launch/definition.json` records the new
  `public_repo`, and the doctor's fallback marketplace repository, its tests and the leftovers checker follow. The
  README's `<SIGMA_REPO>` placeholder is gone: the install lines carry the real URL, and the onboarding control reads it
  from the definition and substitutes the checkout for it when it installs. **Existing installs stop updating.** An
  install recorded under the previous plugin id keeps working but the update key changed, so it receives nothing
  until it is reinstalled; `/sigma-doctor` now has a row for such an install that prints the exact uninstall,
  marketplace and install commands for the host and runs none of them (a fork whose recorded source is not the
  pre-launch repository does not fire; one added by a local path or a non-GitHub git URL cannot be told apart and fires if it kept the old id), and
  `docs/upgrading.md` has a "From the pre-launch name" section with the same steps; `.sdlc/` data is kept as is. **An
  install under the old id runs the old doctor, which shows no row after the rename, so it cannot warn its owner:
  the rename has to be announced.** The Codex lines were last run before the rename and were not re-run for the new
  id. The displayed name is "Sigma Loop"; the one-word spelling is an open owner decision. The leftover-name check also
  fails on the old install id outside `docs/upgrading.md` and five recorded history files.

- **Every skill and command now starts with `sigma-`** (#523, epic #522). The 42 skills `agrim-*` are
  `sigma-*` (`/agrim-loop` is `/sigma-loop`, `skills/agrim-doctor/` is `skills/sigma-doctor/`), the hook
  `hooks/agrim_gate.sh` is `hooks/sigma_gate.sh`, and every doc, test, generated table and script path that
  named them follows. There are no aliases: an old command name does nothing. `.sdlc/` data, the `sdlc:*`
  labels and the organisation name are unchanged, and nothing on GitHub (labels, issues, repository names)
  was renamed. `tools/rename_prefix.py` did the rename mechanically (whole-directory `git mv`, then a
  token-aware replacement; idempotent; also rewrites untracked files git would commit, such as plan drafts),
  and `tools/rename_check.py` is the guard: it exits 1 and names each file and line while any tracked path
  or file outside a narrow allowlist still carries the old prefix. CI runs it after the test suite. The
  allowlist is the organisation name and the owner's login, two private-name guard tests, the older
  entries of this changelog, and the recorded launch evidence files (captured at a named commit, so a
  rewrite would claim output that commit never produced). **Moving over.** (1) Rebase any open branch
  onto this change: every renamed path conflicts, so take the new path and re-run
  `python3 tools/rename_prefix.py` on your branch. (2) Delete the leftover folders git cannot
  remove from a checkout that already ran code (`rm -rf skills/agrim-*`; they hold only `__pycache__`).
  (3) Cursor: `scaffold_cursor_rules` never overwrites an existing rule file, so delete
  `.cursor/rules/sdlc.mdc` and `.cursor/rules/output-contract.mdc` and run `/sigma-init --cursor`; Codex:
  run `/sigma-init --codex` (it refreshes its block). (4) A host or script that calls
  `hooks/agrim_gate.sh` or `/agrim-*` by name must be pointed at the new name by its owner.
  The plugin and marketplace id (`sigma@sigma`) are renamed separately in #524.

- **The review-units tool cuts units at current main again, and the signal-seam test no longer depends on how it was launched**
  (#581, readiness dimension D1, `launch:next`). `tools/readiness/review_units.py` refused every commit after #408 deleted
  `install.sh`, a path it names for Tier A. The refusal stays the intended re-cut lever (a missing named path is never dropped
  silently); its docstring now gives the exact edit, and `install.sh` is out of the list (21 paths). `docs/launch/review-units.json`
  is re-cut at `c3faf6f23e12`: 16 Tier A units of 36,519 lines (was 17 of 36,018) and a 17-unit sample of 14,792 of 71,855 lines
  (was 10 units of 11,972 of 58,577; the new sha gives a different draw). The first cut is kept unchanged at
  `docs/launch/evidence/review-units-a5c615062313.json`. The S4 ceiling in `docs/launch/review-plan.md` is recomputed from the new
  line counts (43.73M, was 42.03M), with the old per-line rates, not re-metered. The seam test in
  `tests/test_slack_commands_listen.py` set its own SIGHUP and SIGTERM dispositions: it failed under `nohup` (inherited ignored
  SIGHUP, which the production code respects on purpose) and passes with or without it. No production code changed.

- **The benchmark task set is frozen: 15 tasks, with the 3 traps authored by an independent agent** (#355, readiness dimension D3).
  The owner amended the pre-registration on 2026-10-05, before any run: traps are written by a fresh subagent that saw only
  `docs/bench/trap-author-brief.md` and the fixture shape, labelled agent-authored in the manifest (`trap_authorship`, each task's
  `authorship`), the pre-registration, the evals README and each hidden bundle, and **not** claimed to be outside-human authored
  (home-field bias risk stated). `bench_tasks.py check` refuses a ready trap without that label and a frozen manifest is now
  checked without a hidden root unless `--frozen` is given. The total spend ceiling of $150 is recorded; no arm has run.

- **(Superseded by the frozen entry above.) The benchmark task set existed in draft: 8 external post-cutoff tasks and 4 internal tasks verified, 3 trap slots awaiting an
  outside author** (#355, readiness dimension D3; the issue's 30-task scope was reduced by the owner on 2026-10-03).
  `evals/bench/tasks/manifest.json` is in the format `evals/bench/bench.py` loads and says `"frozen": false`;
  `tools/readiness/bench_tasks.py` checks it (`check`), scores every task through the harness's own scoring functions in one
  environment resolved by `lock` into `environment.lock` (`verify`: hidden tests fail on the starting tree with pytest status 1 and pass on the reference fix) and fetches
  an external task's base tree (`materialize`). Measured once, on one CPython 3.12 on one macOS machine: all 12 non-trap tasks verified,
  hidden tests on the starting tree failed in every case (pytest exit status 1; 1 to 8 failing tests where pytest printed a count); every hidden run took under 4 seconds and every visible suite under 9. Hidden bundles live only under the
  operator's hidden root; per-file hashes in `task.json` let CI name a hidden file copied into the repository. Not frozen: the three
  traps need a person outside the Sigma team (`docs/bench/trap-author-brief.md`), and the freeze (commit sha recorded in the
  pre-registration) is a later step described in `docs/bench/task-sourcing.md`. The model's training cutoff is a month
  (Jun 2026) from Anthropic's models page; external pull requests were all created from 2026-07-02 on. Open owner decision: the
  matched-spend arm stops after one attempt on external tasks, whose visible tests already pass.
  Tests: `tests/test_bench_tasks.py`.

- **A fold by the old plugin no longer empties Sigma's registry without Sigma saying so and offering a way back** (#514,
  launch blocker class B1, loss of user data). Measured: the old plugin's `feature_sync.py fold`, run from a temporary
  copy of its installed scripts, reads a registry sheet in Sigma's schema as empty, exits 0, and writes `index.json`
  anyway, so Sigma then reads no units. Sigma cannot stop another program's write, so this is a mitigation, not a
  sheet that fold leaves alone. Sigma now keeps a copy of the sheet at `.sdlc/state/backup/index-sigma.json` (written on
  every Sigma write and whenever `loop.py start`, `claim`, `record` or the watcher start sees a Sigma sheet; it is a union
  of the units it has held, so a smaller sheet never shrinks it),
  says so loudly at every such verb when the sheet comes back in the old schema without units the copy holds, and adds
  `feature_sync.py recover <sdlc_dir>` (restores whole missing units; `--discard` sets the copy aside). Until then
  Sigma's own `fold` refuses and `migrate.py` does not convert that sheet. Nothing restores the tracked sheet unprompted,
  `read` is unchanged, and a unit record the old plugin overwrites directly, a fresh clone and a truncated sheet are not
  covered (`docs/upgrading.md`, "A fold by the old plugin"). Tests: `tests/test_registry_survives_predecessor_fold.py`
  (a real-fold control that skips with a named reason where the old plugin is not installed, and an always-running model).

- **The launch-readiness review plan is a committed document with thresholds, spend ceilings and a per-dimension evidence
  table** (#350). `docs/launch/review-plan.md` lists dimensions D0 to D13 with measurement, pass threshold and gating flag,
  the nine reviewer rules, the S0 to S10 order with a 75M processed-token ceiling and a 40M owner checkpoint (every estimate
  marked unmeasured, and what the review does when the ceiling binds), facts re-read on 2026-10-03, and, per dimension, the
  evidence that already exists on main against what is still to be produced. No dimension is scored.
  `tests/test_review_plan.py` keeps the plan, `docs/launch/scorecard.json` and the decision rule in step.

- **Hostile issue, PR and comment text is run through the script-level parsers, and the model-level drill is built** (#362,
  readiness dimension D10). Twelve fixtures under `tests/fixtures/hostile/` (prompt-injection text, fenced and commented
  markers, comma-joined refs, a path-traversal unit name, an indented marker, a 1 MB body built at test time, control
  characters, a forged verdict comment, shell metacharacters in a Slack command, a title holding the field separator, a
  token-shaped string built at run time) run through the blocker scan, the unit parser, the Slack command parser,
  `comment_watch`, the scrubbers and `render.py`, each against its own `expect:` line, with a paired positive control
  per surface so a dead parser cannot pass. Two real defects are pinned as strict expected failures: the blocker scan
  reads a marker inside a fenced block or an HTML comment (#511), and one non-whitespace control character, marker glyph
  or separator in a goal title withholds the whole status block (#190). `tools/readiness/injection_drill.py` refuses to
  run without `--repo OWNER/sigma-drill-NAME` and `--max-usd`, refuses from CI and on a public repository, and has not been
  run: no model call, no spend. The script-level tests cannot show that a model obeys injected text; only the drill can.
  Tests: `tests/test_hostile_inputs.py`.

- **Public repository settings have exact owner commands and a read-only verifier** (#398). `docs/launch/repo-settings.md`
  gives the `gh api` commands the owner runs on the new public repository: branch protection requiring the five CI legs
  (names taken from `.github/workflows/ci.yml` and asserted against it), no required reviews, administrators bound,
  auto-merge allowed, secret scanning, push protection, private vulnerability reporting, a read-only workflow token and the
  maintainers team. It also says why: with no required checks GitHub reports a pending pull request as mergeable, so the
  loop's `work.py merge` landed one while CI was still running. `tools/readiness/verify_repo_settings.py OWNER/NAME` reads
  five endpoints with GET only, prints PASS or FAIL per setting with the fix command, and exits 1 on any FAIL; any other
  method or path is refused before anything is sent. Tests: `tests/test_verify_repo_settings.py` and
  `tests/test_repo_settings_doc.py`, on reconstructed response shapes, no GitHub call. Not done: the commands have not been
  applied to any repository (the owner's rehearsal run, #398 item 4), so their request bodies are unmeasured.

- **Reviewer-independence claims say what the code proves, and no more** (#260). The independent-review row in `docs/enforcement.md` stays advice and now lists what the core does not prove (who called the host's task tool; a maker can record its own approving verdict; the route comes from config and session environment variables; the merge check reads the posted comment, not the evidence) and what it does bind (one PR, head and brief generation; evidence refused when the head or diff moved; result and evidence written once per generation; a post needs the current generation and an unchanged head). Sentences in the README, skills, doctor and config template that stated the maker rule as a guarantee now say it is asked, not proved. `tests/test_reviewer_independence_claims.py` pins the row and a short list of removed overclaims. No mechanism changed.
- **The benchmark harness has its three arms and its isolation rules** (#354). `evals/bench/bench.py run --arm` now
  runs A1 Sigma (a clean `git archive` export of a pinned commit, never the repository), A2 plain and A3 matched-spend
  (the plain agent retried in fresh workdirs until visible tests pass or spend reaches A1's spend for the task),
  and there is no predecessor arm. Every run gets a fresh `HOME`, `CLAUDE_CONFIG_DIR` and `CODEX_HOME`; the operator's
  real plugin directories are content-hashed and any change aborts the benchmark, keeping the paid rows in a results file
  marked `aborted` (an overrun used to write nothing); each run directory, with the hidden bundle's scoring copy, is
  deleted right after scoring and the scratch root must start empty; a hidden root inside the scratch root is refused;
  every launched command is time-bounded and its process group killed. Tests: `tests/test_bench_arms_isolation.py`, all
  against a fake `claude`, zero model spend. No real `claude` has been run: flag spelling, authentication in an empty
  profile and the launcher's containment are unmeasured, and the README lists the limits.

- **leak_scan reads a private key's first line in hex, and an encrypted PKCS8, PKCS12 or PGP first line** (#451, launch
  blocker class B2). `key-body` recognised a DER header only in base64, so a hex-encoded key, the hex first line of a PKCS1
  key, and the lone first line of an encrypted PKCS8, a PKCS12 or an armored PGP private key all scanned CLEAN through
  `python3 tools/leak_scan.py` (#433's undetected-shape run). One structural reader over decoded bytes now takes base64 and
  hex tokens (anywhere in a line, so #449's embedded keys and #450's UTF-16/UTF-32 text compose) and recognises PKCS1 / PKCS8 /
  SEC1 / DSA, encrypted PKCS8 (PBES2, PBES1, pkcs-12 PBE OIDs), PKCS12 (version 3 + data OID, DER or BER) and PGP secret-key
  packets whose material opens like a key's. Hex is a first-line recogniser, not a line-count class (hash lists made that +4 false
  positives on the corpus). Measured: tracked tree 0 -> 0; 97-file corpus 17 -> 17 `key-body` (output identical); 12 real shapes CLEAN -> caught and 5
  public lookalikes still clean; 134,509 files under a package prefix: 21 added, all private test keys. Unrecognised and stated in the
  contract: separated hex (`30:82`), `0x30, 0x82` byte lists, a body with no first line, first lines below the floors (24 base64 /
  36 hex characters; 32 / 44 for a PKCS12), PGP 2.x packets, base64 of whole PEM. Three public header literals in
  `tests/test_key_body*.py` were split so the tree still scans to 0. Tests: `tests/test_hex_der.py` (seen red before the code).

- **The benchmark go threshold is an exact paired test that fits 15 tasks** (#502). The old rule (lower 95% bootstrap
  bound of the paired difference at least -5 points) passed a result with every task tied and one with 2 wins and no
  losses, yet failed 3 wins, 1 loss and 11 ties (lower bound -13.3 points), so it did not discriminate at this size.
  `docs/bench/preregistration.md` now decides each comparison by an exact one-sided binomial test on the discordant
  tasks (alpha 0.05 and a margin of 0 tasks are recommended owner decisions), with outcomes GO, NO-GO and INCONCLUSIVE;
  INCONCLUSIVE is never a win. The old text is kept in the deviations log, marked superseded. The document states what the
  rule does not give: a wrong GO is at most 0.030 likely at 15 tasks when Sigma truly ties, but a true +20 point
  advantage passes only 0.176 of the time, so most real moderate advantages end INCONCLUSIVE, and no non-inferiority claim
  is available. `evals/bench/decision_rule.py` (stdlib only, exact fractions) prints the published tables;
  `python3 evals/bench/decision_rule.py --check docs/bench/preregistration.md` fails if they drift. Tests:
  `tests/test_bench_preregistration.py`, `tests/test_bench_decision_rule.py`. No benchmark task has run.

- **The launch definition claims macOS only on Python 3.12, what CI runs** (#493, launch blocker class B3). The supported
  table and its JSON twin listed macOS on Python 3.10-3.13 while CI runs macOS on 3.12 only, so three supported cells
  were claimed and never checked. They now list macOS on 3.12 and Linux on 3.10-3.13; the other macOS versions are
  stated as untested, not unsupported (a new `untested` list in the JSON). CONTRIBUTING no longer says Sigma
  "supports Python 3.10 and newer". A test derives the cells from the CI workflow and fails when the definition, its
  JSON twin, a docs table or a docs sentence of the form "macOS with Python ..." names a cell CI does not run; it was
  seen red on the old text. The definition stays `proposed` (unsigned) and the CI matrix is unchanged. The #330 entry
  below records the earlier claim and is left as history.

- **The README states the one coverage number that was measured, and says CI enforces none** (#194, readiness D8).
  The README once claimed an "85% coverage floor" that CI never enforced; #277 removed the claim, and this measures
  what it should have been. A full suite run under coverage.py (subprocess tracing on, Python 3.12, macOS, local) gave
  90.91% line coverage of `skills/` and `hooks/`: 34,370 of 37,806 statements, 11,569 tests passed. It is a lower
  bound: scripts that tests copy elsewhere and children started with a scrubbed environment are not traced, and branch
  coverage and every Python other than 3.12 were not measured. The same run on the Linux Python 3.12 CI leg took 2.1
  times as long as the plain leg (3,154 s against 1,481 s, near the 60-minute job limit) and a test with a 15-second
  subprocess timeout failed under tracing before any Linux percentage printed, so no per-PR coverage gate was added.
  A test fails if the README's figure and the recorded measurement disagree, or if CI starts enforcing coverage while
  they say it does not. A floor of 85% is recorded as a candidate only; a non-blocking scheduled Linux run is queued.

- **Review evidence of finished goals is pruned** (#459, B6 of #419). `loop.py record <goal> done` now removes that goal's
  review generations, manifest, posts and results once its work record is gone (the merge gate reads the PR's comments,
  never these files). Only a generation with exactly one owning goal is removed; parked, failed, running or re-claimed
  goals, a successor's manifest, links and unattributable evidence-less generations are left alone, at most 10 goals per
  call, and a crash is finished by the next call. Lever: `work.py prune-review-generations <sdlc_dir> [<goal> [--done]]`.
  Measurements, residuals and the unbounded `review-queue.md` decision: `docs/launch/retention-review-evidence.md`.

- **Pruners can no longer be walked out of `.sdlc` through a link, and the fixed-name state files have a stated
  size and owner** (#466, B6 of #419). A control now builds a decoy host configuration root reachable only through
  links and records and runs nine pruners over it (`retention`, `goal_state_prune`, `liveness_prune`, the session-marker
  prune in `loop.py`, `worktree_prune`, `logroll`, the review-copy cleanup in `work.py`, the event-journal sweep and the
  time-store sweep). Two escaped: the review-copy cleanup `rmtree`d a directory outside `.sdlc` when `.sdlc/evidence`
  was a link, and the session-marker prune (run by `loop.py start` and every pick) deleted matching files in a linked
  `state/sessions`. Both now refuse and the first prints one stderr line. `setup_wizard.write_dismissed` keeps only the
  8 known wizard check names (the reader already ignored the rest) and no longer raises on a non-string entry. The 26
  audit rows (singletons, spend files, `STATE.md`, three watch cursors, `inbox.md`, the embeddings cache) are
  each classified from their writer with measured sizes: `inbox.md` and the cursors are documented as unbounded
  between loop picks with a ceiling per item or key, and `embeddings.json` is capped at 2,000 entries whose bytes
  grow with the embedder's dimension (15.6 MB at 384 dimensions, 124.6 MB at 3072, fake embedder; 687 ms to parse
  on every pick at the larger size). Bounded inbox delivery and a byte ceiling for the cache are queued (#490, #491).
  Not measured: any live adopter, a real embedding provider, Linux or Windows.

- **Goal worktrees whose work provably landed can be reclaimed, and every other survivor is reported with
  its reason** (#465, B6 of #419). A worktree is a full checkout (903 tracked files, 18.5 MB here; 455 MB
  across the 14 on the measured machine, 3 of them for merged PRs) and nothing reclaimed one whose goal
  never reached `record done`. `python3 skills/agrim-loop/scripts/worktree_prune.py list-removable .sdlc`
  shows what would go and why each other tree stays; `... sweep .sdlc [--dry-run] [--limit N]` removes
  only the checkout, with a plain non-force `git worktree remove`, and only when the PR is positively
  merged, every commit in the tree was in it, the tree is clean (ignored files must be regenerable
  caches), the recorded branch is checked out, nothing alive owns the goal and it is not the caller's own
  tree. It never deletes a branch, a remote ref or the work record. A crash mid-removal is journalled
  and healed by the next sweep (one restore at most, then left for a human). `loop.py start` and
  `loop.py record ... done` run one bounded sweep only when `work.enabled` and `work.reclaim_merged_worktrees` are
  `true` (default `false`: its proof reads GitHub REST quota). Not measured: a 100,000-file repository,
  Linux or network filesystems, Windows (the sweep refuses without `fcntl`).

- **Claim locks and claim markers whose owner is dead are swept, and session and lock markers have a stated
  cap** (#464, B6 of #419). On the measured checkout `state/claims/` held 82 files after four days
  (47 `.lock`, 35 `.claimed`, about 20 a day) and nothing ever deleted one: `reclaim_stale_claim_lock`
  had no caller. `python3 skills/agrim-loop/scripts/liveness_prune.py sweep .sdlc [--dry-run]
  [--limit N]` (and, capped at 200 removals and 5 s, `loop.py start` and `loop.py record ... done`)
  now removes a claim lock only when it is older than the claim lease (at least an hour), its goal has no
  alive or unknown agent marker and is in no live session, and the sweep wins its `flock` (so a live
  holder is never touched); a `.claimed` marker goes after 30 days when no work record or live agent
  holds it. `_try_acquire_claim_lock` now re-checks that the lock it won is still the file at the path,
  closing an unlink-between-`open`-and-`flock` window where two pickers could both win. The 256-stripe
  and singleton `flock` files are capped, never deleted, and a test `SIGKILL`s a real holder of each to
  show it reads free at once; dead session entries and the Slack lock keep their existing reclaim.
  Not measured: inode cost at 10x or 100x on a real filesystem, network filesystems, Windows (the lock
  sweep refuses without `fcntl`).

- **Per-goal state records are removed once their goal is `done` and seven days old** (#458, B6 of #419).
  Nothing removed `state/verify`, `landing`, `propagation`, `escalation` or `unit-tracking`: on the
  measured checkout verify evidence alone was 3,929,772 bytes over 55 goals (median 16,468, max
  593,634), about 393 MB at 100x. `loop.py record ... done` now ends with a bounded sweep, and
  `python3 skills/agrim-loop/scripts/goal_state_prune.py sweep .sdlc [--dry-run] [--limit N]
  [--min-age-days N]` runs it by hand. A goal is pruned only when its action log's newest internal row
  is `recorded done` (needs `action_log.enabled`), nothing of it is younger than the window, it has no
  work record and no live agent marker; symlinks are never followed; `run_stop` markers older than 30
  days go too. Nothing is removed at `done` itself, because the verify record is a frozen contract kind
  read after `done`. `work`, `withheld` and `goal-review` are kept on purpose, with a stated ceiling
  and reclaim lever each, and all 29 patterns of the issue have a recorded disposition. Not measured:
  the families absent on the measured checkout.

- **The leak gate's `key-body` rule catches EC P-256, Ed25519 and truncated private-key bodies, and
  no longer exempts a private body under a public header** (#433; corrects the #277 entry below).
  #277's entry said `key-body` catches "a private key body with its header stripped". What it caught
  was 3 or more adjacent whole lines of 40+ base64 characters, each with upper case, lower case and
  a digit. So it missed a real EC P-256 SEC1 body (lines of 64, 64 and 36), an Ed25519 PKCS8 key
  (one 64-character line) and a truncated RSA paste (BEGIN, `Comment:`, 2 lines), whether the header
  was kept without an END, named in prose above the body, or gone. It exempted any body whose single
  line above was a `CERTIFICATE` or `PUBLIC KEY` header, a private one included, and it flagged real
  public PGP, SSH2, CSR, CRL and PKCS7 blocks. The rule now has three triggers. (1) Blocks: 3+ lines
  of 40+ with at most one line lacking upper case, lower case or a digit, or 2 mixed lines followed
  by a 28-39 character line; lengths count the base64 payload, not the `=` padding (a 28-character
  line ending in `=` is a 27-character tail, so a real legacy-encrypted EC body re-wrapped at 72
  columns, 72, 72 and 28 with `=`, is clean header-less). (2) A header anchor: a block that does not
  count, every line mixed, is flagged when a line naming a PRIVATE or SECRET `BEGIN <label>-----`
  (a header, or prose naming one) sits above it with at most 8 blank or `Key: value` lines between, each may carry a unified diff's `+`.
  (3) A DER first line: a line whose first 24 base64 or base64url characters decode to a private
  key's DER header (PKCS8, PKCS1 / DSA, SEC1), or OpenSSH's constant, after indentation and at most
  two runs of 1-3 symbols, `+` and `/` among them (a diff's `+`, a `//` with no space: missed until
  post-PR review 1); a longer run of `+` and `/` is read only when the whole line, run included, is
  40+ base64 characters and nothing else, so a run of 7 or more followed by other text on the line
  (`",`) is not caught (measured, post-PR review 2); this is the only way a header-less one-line Ed25519
  key can be told from a random token. The exemption is now the whole span: a standalone non-private
  `BEGIN` line, then only blank, `Key: value`, base64 or `...` lines, then the matching `END`; it
  applies to the block trigger only, never to a DER line. A lone CR now ends a line for every rule
  (4 of 133,124 files scanned hold one; output identical on all 4). Measured against a3c913c's rule,
  each with its population: the staged tree 0 -> 0 findings (826 files: 821 at origin/main f69d3e1,
  this change's 2 new test files and its 3 `.sdlc` phase documents); a generated 97-file corpus 34
  -> 17, 1 of them new (a P-256 body); the working trees of 71 sibling repositories (128,828
  decodable text files up to 4 MB) 39 -> 47: 15 new, of which 13 are private keys by their decoded
  DER structure (not reviewed by hand) and 2 are public certificate bodies in SAML XML, false
  positives, and 7 public CRL/CSR blocks no longer flagged; /opt/homebrew (99,772 files) 35 -> 50:
  18 new, of which 17 are private-looking test keys and fixtures in Python packages (not reviewed by
  hand) and 1 is a false positive, a header-less public CMP message in a Python string. An attack
  harness of 147 attempts: 68 got through a3c913c (one a false positive on a public PGP block), 32
  get through now, 21 of them aimed at other rules; every one also got through a3c913c, and none is
  a false positive. So the false-positive figure is 0 new false positives on this tree and on the
  corpus (the corpus's 1 new finding is its P-256-shaped body), and the 3 listed above on the wider
  scans; it is not "no false positive". Cost, CPU seconds, minimum of 3 runs at load 4-6, over the
  same 826-file staged tree: the whole gate 5.57 -> 5.49 s on Python 3.9.6 and 5.11 -> 5.05 s on
  3.12.13; `key-body` alone 0.26 -> 0.16 s (one run); the worst adversarial 4 MB file found (one
  line of repeated `BEGIN ` words above a one-line body) 1.1 s in `key-body` and 2.6 s for the whole
  file (a3c913c: 0.05 / 1.1 s); scrub.py's `private-key` rule, unchanged here, is not linear on a
  file of repeated unterminated private headers (7.6 s at 0.5 MB, a 4 MB file unfinished after 150
  s). Known ambiguities, all findings: 3+ raw base64 digests in a row; generic wrapped base64 (a
  binary MIME attachment, a 76-wide CSS data URI; a3c913c flags both too); a header-less public DER
  body (an XML certificate, a CMP message) or any other public DER opening `02 01 00|01 30|02|04`; a
  certificate with prose inside, or whose BEGIN or END line carries code; an SSH2 public block whose
  `Comment:` continues with a backslash; a unified diff that adds a certificate bundle (the span
  exemption is not diff-aware: a `+`-prefixed copy of a 145-certificate certifi bundle, 141 locations
  on a3c913c, 145 now); one 40+ mixed-case digest below prose naming a PRIVATE `BEGIN` header with
  only blank, `+` or `Key: value` lines between, by design of the anchor (a changelog sentence, a
  table row, a diff: 0 findings on a3c913c, 1 now; no file on this tree or in the corpora hits it
  except those planted probes). Not detected (each planted and run): hex bodies; base64url
  bodies other than a DER first line; a base64-wrapped PEM; a key inline in a JSON or shell string
  (`{"k": "<base64>"}`, `KEY='<base64>'`) or after text on its line (a DER-led key alone on its line
  inside one, a JSON array element or a multi-line shell string, is caught); a non-DER body (encrypted
  PKCS8, legacy-encrypted RSA or EC, PGP-private) every line of which carries a comment or quote
  prefix (`# `, `"`, `// `, `> `), even directly under its header (a3c913c missed it too); UTF-32 text with a little-endian byte-order mark (mis-decoded as UTF-16, nothing
  seen; without a mark, or big-endian, it is reported `opaque-binary`); a body indented with U+00A0,
  U+2003, U+3000, `\v` or `\f`; a body whose lines are separated by U+2028, U+2029 or NEL, except a
  DER first line that opens the file or follows an LF (flagged there whatever separates the rest;
  hidden only when one of these separators comes before it); a header-less non-DER body in a file whose lines end `\r\r\n` (each line is then
  followed by an empty one; a3c913c missed it too); a PuTTY `.ppk` key whose private part is 1-2 lines (Ed25519; an RSA
  one's 3+ line blocks are flagged); a slice of 1-2 lines with no tail;
  a slice with no DER start inside a matching public span; encrypted PKCS8, PKCS12 or PGP-private
  first lines; a body wrapped below 24 characters; a body wrapped below 40 with no DER first line;
  an all-lower-case body; more than 8 armor lines; a non-DER body under a lower-case `begin` (it
  does not anchor; a DER first line or a counted block under it is still caught). The gate's
  docstring keeps the full list. The allow marker waives a DER first line; there is no designed
  inline waiver for a header-less multi-line body: a marker on one of its lines splits the block, so
  a short body with no DER first line (3 lines, or lines of 64, 64 and 36) goes clean, a DER-led one
  (every real P-256 SEC1 body) stays flagged at its first line unless the marker is on that line, and
  a longer one stays flagged on its remaining lines, a side effect pinned by tests, not a feature; `ALLOW_PATHS` is the reliable
  waiver. The new test files test_key_body (84 tests, all red on a3c913c) and test_key_body_guards
  (148; green on a3c913c except 48, measured: one review pin, a lone P-256 PKCS8 first line, 24 of
  the 30 rows added after post-PR review 1: the `+` / `/` / `// ` / diff rows and one padded tail,
  and their control, whose mutation target a3c913c does not have; 8 of the 19 rows added after
  post-PR review 2: the five flagged `+` / `/` run rows and the three anchor digest rows; 14 of the
  25 rows added after post-PR review 3: the two marker rows flagged at a P-256 DER first line and the
  12 U+2028 / U+2029 / NEL rows flagged at a DER first line; the 18 `+` / `/` / diff rows are red on
  the PR's first head too) plant every shape through the documented
  gesture, with 9 mutation controls. Follow-up issues are listed in the PR.

- **Review units and seeded-defect recall: `tools/readiness/review_units.py` and
  `tools/readiness/seed_defects.py`** (#352). `review_units.py <repo> --sha SHA [--json PATH]` turns
  one frozen commit into review units of at most 3,000 lines (`readiness-units/v1`): every tracked
  file under `hooks/` plus 22 named files in full (Tier A), and a sample of the other `.py` and `.sh`
  files under `skills/`, `tools/` and `evals/` (Tier B), drawn whole file by whole file with
  `random.Random` seeded by the first 8 hex digits of the commit until 20% of the population's lines
  are in. The draw's 22% ceiling under-draws large files (`doctor.py`, 7.7% of Tier B, is drawn in
  15.3% of 3,000 seeds), a class over the limit offers its methods as split points (`sources.py` is
  one 4,300-line class), and a Tier A path missing at the commit exits 2.
  `docs/launch/review-units.json` is its output for the frozen commit `a5c615062313`, byte-identical
  on Python 3.9, 3.10, 3.12 and 3.13. `seed_defects.py apply <clone> <seed_dir>` plants every
  `*.patch` in a clean, detached clone as one atomic `git apply` (it refuses a seed directory inside
  the clone) and writes `manifest.json` (`readiness-seeds/v1`) with each seed's file and changed
  lines; `score <seed_dir> <findings.json>` matches a finding to a seed on the same file within 5
  lines, prints recall, and exits 0 at 80% or more, 1 below, 2 on bad input, naming a missed seed
  only as `NN-<class>` unless `--show-missed-locations`. `docs/launch/seeded-defects.md` is the
  protocol: who writes seeds, where they live, the rules, the 80% threshold (below it the review is
  re-run, not reported), and the known leak that an uncommitted seeded clone shows every seed to
  `git status` (owner decision pending, see #413). `doctor.py` is sampled, not in Tier A (owner
  decision pending, see #412). `tests/test_readiness_review_units.py` and `tests/test_readiness_seed_defects.py`
  cover both, with three example seeds in `tests/fixtures/readiness_seed_examples/`, and the
  write-surface inventory records the two new write sites (`review_units.py:main`,
  `seed_defects.py:apply`).

- **Release, pin, rollback and incident runbook** (#359). `docs/release.md`
  defines semantic-version release ownership, the first-public-release GO gate,
  release/tag steps, the pre-rename stop/repoint/verify procedure, and S1–S3
  incident response. `docs/launch/evidence/pin-rollback-2026-10-02.md` records
  three isolated Claude Code/Git probes honestly: no `v1.0.0` source is
  available yet, so no pin or rollback gesture is claimed. The publish runbook
  now describes fresh-public-snapshot publication rather than a visibility flip.

- **The launch GO/NO-GO rule and the `launch:blocker` classes are pre-registered, with a checker
  that computes the verdict from `main` alone** (#331, #429). `docs/launch/decision-rule.md` fixes
  the eight blocker classes (B1–B8; everything else is `launch:next`), the promote/demote rules,
  which dimensions gate (D12 Market is informational and blocks only through B3), and the rule: GO
  only when every gating dimension scores >= 3 with evidence, no open issue carries
  `launch:blocker`, `docs/launch/definition.json` is signed, and the named benchmark results file
  is on `main`. Its status is Proposed: the owner's merge is the sign-off.
  `docs/launch/scorecard.json` is the unscored skeleton. `tools/readiness/decide.py <repo_root>
  [--blockers-json PATH] [--repo OWNER/NAME]` (stdlib only) prints one line per reason, `info:`
  lines and `GO`/`NO-GO`; exit 0 GO, 1 NO-GO, 2 refused or malformed (`decide.py: REFUSED:` /
  `decide.py: MALFORMED:` on stderr, nothing on stdout), `--help` and usage errors included. The
  gating set is pinned in the checker, so a scorecard that flips a `gating` flag, drops, adds or
  renames a dimension or carries an unknown key is refused, not re-weighted; a missing definition
  is NO-GO (`definition missing`); a blocker list that cannot be read is exit 2, never "zero
  blockers"; open blockers are read with one read-only REST call (`gh api ... --paginate`), pull
  requests dropped. The verdict is read from `main` and the live REST list (#429): the scorecard,
  the definition, every evidence path and the benchmark file are read from the one commit
  `refs/remotes/origin/main` names (that exact ref, read once; a symbolic one is refused), never
  from the working tree, and nothing is imported from the checker's own folder (it is removed from
  the import path before anything but the builtins `sys` and `posix` is imported, so an untracked
  `json.py` beside it cannot print GO); every git call drops every inherited `GIT_*` variable and
  switches off global and system config, replace refs, lazy fetch, every built-in transport and
  (with `-c
  core.commitGraph=false`) the commit-graph file; every `gh` call drops every `GIT_*` variable,
  `GH_REPO`, `GH_HOST`, `GH_FORCE_TTY` and `CLICOLOR_FORCE` and pins `GH_PAGER` and `PAGER` empty
  and `NO_COLOR=1`, so a forced terminal cannot run `gh api` output through a pager (measured with
  gh 2.98.0); without `--repo` the blocker repository is `origin`'s URL as the checkout's own
  config writes it (no `insteadOf`), and with `--repo` it must be that repository when `origin`
  names one (else refused, so a same-commit fork cannot stand in) and is the operator's stated
  assertion when `origin` cannot name one; the
  live run refuses a `gh` that sends its requests to an `http_unix_socket` and a checkout whose
  `main` is not the commit REST reports (`git fetch origin`, then rerun); an offline
  `--blockers-json` run is never GO, because a file cannot show that no blocker is open; a missing,
  symbolic or unreadable `main` is refused (exit 2) instead of a verdict; git older than 2.32 is
  refused. CI runs it on Linux with Python 3.10, 3.11, 3.12 and 3.13 and on macOS with Python
  3.12; Python 3.9 was measured by hand, not CI-proven. It refuses to run on Windows (exit 2). Each
  of #429's seven defects, the two pre-PR review findings (a `GH_FORCE_TTY` pager forging the
  REST reads, a same-commit fork passed as `--repo`) and the post-PR review finding (an untracked
  stdlib-named module beside the checker) has a test seen red on the #331 checker and
  green on this one, every guard was broken once and its test seen red, and what the checker still
  trusts without defending — the checker file, the `python3`, `git` and `gh` on `PATH` (with `.`
  or the checkout root on `PATH`, whatever `gh`/`git` it finds there) and the
  interpreter's environment, the whole `.git` directory, `gh`'s config directory and credentials,
  network proxies and certificate authorities and their variables, and `--repo` when `origin`
  cannot name the repository — is stated on the page; the controls and gesture transcripts are in
  `docs/launch/evidence/429-controls.md`. Process rules the checker cannot see (a blocker names
  its class; removing the label is a demotion) are listed under their own heading.

- **A timed-out autowatch drive now stops the model's whole process tree** (#425). Before, the
  timeout killed only the direct model process; anything it had started kept running, and one
  holding the output pipe made the "timeout" wait until that process exited. The driven session
  now starts in a process group of its own. On timeout, or on SIGTERM/SIGHUP/Ctrl-C to the tick,
  the whole group gets SIGTERM, then up to 10s to exit, then SIGKILL, and the pipe drain is
  bounded at 5s. Further signals cannot cut that short. Afterwards every signal the tick
  received is re-delivered in order, so its own handling (dying by SIGTERM, KeyboardInterrupt, a
  handler it installed) still happens. If the tick is SIGKILLed (alone, or by a
  `killpg` of its group), a lifeline process sees its pipe close and runs the same escalation.
  Windows refuses the drive (`REFUSED [no-process-group]`, exit 2, recorded in the ledger).
  `tests/test_autowatch_process_group.py` covers each path with real process trees. Each test
  fails when the mechanism it guards is removed. Against the previous code all but two fail; the
  two guard behaviour the old code already had and the new code must keep (a group SIGKILL of
  the tick reaching the model, and leftovers of a normally finished model being left alone). Threat model TM-10 is now mitigated (residual: a descendant that
  calls `setsid` itself, or a SIGKILLed lifeline process).
- **A public-tree builder and an export verifier** (#397).
  `tools/build_public_tree.py` turns one reviewed commit into the tree of a fresh one-commit
  public repository: deterministic (same commit, same tree and export commit), scanned four ways,
  with its report outside every repository; `.sdlc/` is excluded by default, a default the owner
  may reverse with `--sdlc include`. `tools/verify_public_repo.py` proves over read-only REST that
  a pushed export is exactly that commit with CI green on every leg; the repository name is its
  argument. The public repository is a new one (this repository is not renamed), so nothing
  needs repointing. The builder refuses a non-private source repository unless the owner passes
  `--allow-public-source`, which warns and records the override in the report. The exposure baseline is reconciled in `docs/launch/exposure-allowlist.json`, the
  builder's own exceptions are in the new `docs/launch/public-tree-dispositions.json`. Runbook:
  `docs/public-snapshot.md`. The rehearsal is prepared and owner-run, not performed.
- **The launch definition is recorded: what ships, to whom, on which hosts** (#330).
  `docs/launch/definition.md` and its machine-readable twin `docs/launch/definition.json`
  (`launch-definition/v1`) fix what "launch" means so every readiness threshold can point at it: a
  fresh public snapshot repository named `Agrim-Intelligence/sigmaloop` (this repository
  is not renamed; the public repository is created new, by the owner, under its
  own name; the sequence is prepared in #397), version `1.0.0`; supported = Claude Code on
  macOS and Linux, Python 3.10-3.13, `local-goals` and `github` modes (launch-blocking
  requirements, not verified today: CI gates Ubuntu on 3.10-3.13 and macOS on 3.12 only, #338);
  experimental = Codex, Cursor, Windows (no recorded end-to-end run; Codex and Windows have the
  partial validation the page states);
  audience = individual developers and small teams on GitHub; out of scope = Slack listener,
  cross-repo units, managed settings. Status is `proposed`; merging the pull request is the owner's
  signature. The page lists what the rename repoints, led by a safety hazard: the loop's own
  `discovery.github.repo` in every `.sdlc/config.json`, which after the name takeover would point a
  running loop's reads, labels and comments at the public repository, so #359 must ship a
  stop-repoint-verify step before the rename. The rest: `ledger.handoff.upstream_repo`, clones'
  `origin` (and the `origin`-named remotes that follow it), `gh`'s `{owner}/{repo}` calls, the
  publish runbook's `--repo`, `docs/board.md`'s milestone-assignment commands,
  `contract/golden/config.json`, the CI badge, doctor's fallback `_MARKETPLACE_REPO`, and existing
  plugin installs' recorded marketplace source; and the runbook's stale visibility-flip assumption as
  work for the release goal (#359). `tests/test_launch_definition.py` checks every field in both
  directions (artifact, public repository, version and tag, audience, supported and experimental
  cells, out-of-scope list, Status line), the same signature #331's `decide.py` accepts (no
  duplicated JSON key, a plain-login `signed_by`, a real `signed_on` no later than today), an
  `owner/name` repository, and that the rename list names `discovery.github.repo`; its controls, including a one-copy change of each field, are in
  `docs/launch/evidence/330-control.md`.

- **`tools/leak_refs.py`: find, and plan the removal of, private-repository references before the
  visibility flip** (#282). A patterns file (`--patterns` or `SIGMA_LEAK_PATTERNS`, no default,
  refused inside any git work tree) drives four verbs: `scan` reads every issue and PR in every
  state (titles, bodies, conversation and review comments, review bodies), commit comments,
  releases, and their edit history and title renames; `tree` reads this checkout's files, paths and
  commit messages; `text` checks one file or stdin before it is posted; `rewrite` writes a dry-run
  (0600, outside every repository) and `rewrite --apply --from` applies it compare-and-skip, owned
  text only, with no new blocker edge, link or mention. Output is locations and pattern line
  numbers, never matched text; any `gh` failure, truncation-by-cap, bad shape or count that moved
  during the walk exits 2, never clean. The owner's steps, including the web-UI history purge the
  API cannot do, are in `docs/publish-runbook.md`. Tests: `tests/test_leak_refs.py` (offline).

- **Automatic classification can no longer drop the old plugin's post-conversion edit, and no
  Sigma writer can skip the one legacy-delta rule** (#326, review block #2 on PR #327). Tier-1
  classification reopened a closed unit by calling `feature_registry.write_unit` directly, so it
  skipped the rule `repair`, a pick, a claim and `set-priority` use: the old plugin's newer record
  value (e.g. priority P0) was dropped from disk, nothing printed. Reachable only with
  `discovery.no_dangling_goal.core` configured, the live judge enabled, and a closed unit the old
  plugin wrote a record for after the conversion. Now: (1) the reopen goes through
  `feature_sync.amend`, BEFORE the label is attached; a refusal attaches nothing, writes nothing and
  refuses the pick that pass (`tier1-reopen-refused`, naming the value and the recovery). (2)
  `write_unit` itself applies the rule (`feature_registry.delta_verdict`, moved from
  `feature_sync`) and raises `LegacyDeltaConflict` (not a `ValueError`) instead of discarding a
  newer record's value; `amend` and `repair` report it. It reads the unit's own file, and
  `index.json` only when that file is in the old schema. (3) `tests/test_feature_registry.py`
  inventories every `write_unit`/`write_index` caller and every module that names the registry's
  files and writes something (by AST and source scan); an unclassified new one fails. (4) A pick
  that discards a value from an older delta record reports a `legacy-delta-discard` divergence (on
  the pick's result line and in the ledger for the unit's owner), and both delta kinds now reach the
  result line. (5) `write_index` writes nothing when the bytes are unchanged, so a no-op fold no
  longer makes a newer delta record look older; a fold that changes `index.json` still does, and
  `docs/upgrading.md` says so. (6) Doctor shows a `legacy delta records` row and `status.py` a
  segment (count, how many would be refused, the repair command), each silent when there are none.
  (7) The model test gains `sigma close` and `sigma classify reopen`, a start with the unit closed,
  and a second invariant: a legacy delta record is never replaced silently (each dropped value
  printed, a newer one refused). Before the fix it found 43 failing sequences on the stand-in and 52
  on each real release (1.4.24, 1.4.25), the shortest being the reviewer's; after it none, over 287
  and 302 (stand-in, open and closed start) and 382 and 404 (real) distinct states. Docs:
  `docs/upgrading.md` now says the old plugin's own `fold` must not run once `index.json` is in
  Sigma's schema by any route (migrate, or any Sigma fold), and `skills/agrim-doctor/SKILL.md` no
  longer says a partial migrate can leave the complete, newer record. The symlink test skips where
  symlinks cannot be created.

- **A second migrate, or a Sigma pick, can no longer erase a unit the old plugin touched after the
  conversion** (#326, review block #1 on PR #327). After the conversion the old plugin's `pick` or
  `set-priority` writes a near-empty unit record in its own schema, which Sigma serves as a delta
  onto the `index.json` entry. A second `migrate.py --apply` (which Sigma's own notice, `repair`'s
  refusal and `docs/upgrading.md` told users to run) rewrote only that record's schema id, so it
  replaced the entry: Sigma served a title-less, owner-less unit with one goal and no grant, exit 0.
  Now: (1) `migrate.py` refuses to convert a unit record in the old schema when `index.json` is
  already Sigma's with an entry for that unit and the merge would serve something else (listed as
  refused, exit 2), naming `feature_sync.py repair`; the check is repeated under the unit's lock at
  write time. (2) The notice, `repair`'s refusal and the docs no longer send anyone to rerun migrate:
  a partial migrate keeps `index.json` in the old schema, so it leaves no delta. (3) A Sigma write to
  such a unit (`feature_sync.amend`: a pick, a claim, `set-priority`) applies `repair`'s rule: it
  prints each record value it drops, and when there is one and the record is at least as new as
  `index.json` it refuses (a `legacy-delta-conflict` divergence) instead of turning the old plugin's
  P0 into P1 in silence. The newer-than check is now `>=`, and its `git checkout` mtime caveat is
  documented. (4) After replacing `index.json`, `migrate.py` scans `units/` again and puts the
  index's previous bytes back if a record in the old schema appeared meanwhile. (5) A model-based
  test in `tests/test_coexist.py` runs every sequence of up to five steps (Sigma's migrate, dry run,
  pick, show, fold and repair; the old plugin's pick, set-priority and claim) from a fixture unit and
  checks after each step that nothing Sigma served is lost; it runs on a stand-in always and on the
  old plugin's real 1.4.24 and 1.4.25 code when `SIGMA_TEST_PREDECESSOR_GIT` is set. Before the fix
  it found 31 distinct failing sequences (after deduplicating equal states; the reviewer's is the
  shortest) on each of the three; after it, none, over 128 (stand-in) and 205 (real) distinct states. Dead
  `migrate._under_units` removed.

- **A partial migrate can no longer lose a unit record** (#326, the last blocking finding on #314's
  PR #319). `migrate.py --apply` wrote in path order, so `features/index.json` took Sigma's schema
  before `features/units/*.json`. A unit record refused in the same run (changed after it was read,
  or a symlink) stayed in the old plugin's schema. That record can be the complete, newer one,
  because the old plugin writes its record first and rebuilds its index only on demand. The reader
  then merged it as a delta under the older index entry, so its owner, priority, branch and grants
  were lost, `feature_sync.py repair` made the loss permanent, and a second migrate found nothing to
  do. Now: (1) unit records are written first and `index.json` last; (2) the index converts only if
  no unit record is left in the old schema when it is written. A record refused in the run, a
  symlinked one (refused already in the dry run) or one the old plugin created during the run keeps
  the index in the old schema, and the reader keeps reading the record whole. The index is listed as
  refused with the reason (exit 2), and a rerun converts both. (3) `repair` says what it discards: every non-empty record value
  the delta merge does not keep, one line each. When there is something to discard and the record
  is newer than `index.json` (file time), it refuses that unit (exit 2, nothing written) and names
  the ways forward. The recovery text no longer says "nothing is lost". (4) The reader comment, its
  notice and `docs/upgrading.md` now say a legacy-id record next to a Sigma index is a delta
  (#327 corrected an earlier wording that sent users to rerun migrate over one). The docs also warn against
  running the old plugin's own `feature_sync.py fold` after the conversion: its 1.4.25 release
  wrote an empty `index.json` on a converted repository (measured on a scratch repository). Tests in
  `tests/test_coexist.py` cover the reviewer's race on the stand-in and on the real previous plugin,
  the symlink case, a record created during the run, a clean run, and repair. Each guard was broken
  once and its test went red on 3.10 and 3.12.
- **The README is checked against what ships, and its init gestures are run** (#277, the review
  notes on #231). (1) Every `python3` gesture the README shows now reads `python3
  <installed-sigma>/...` -- after review this also covers the Cursor Quickstart (`~/sigma/...`), the
  `log.py` / `ledger.py` / keep-parked `loop.py note` gestures (`"${CLAUDE_PLUGIN_ROOT}/..."`, a
  variable only a host's skill or hook context sets), the watcher's bare `python3 watch_daemon.py`
  and `evals/run.py`, each of which exited 2 when copied from a user's repository. A test flags any
  `python3 <path>.py` in a code span whose path is not `<installed-sigma>/...`, absolute, or a
  `$VAR` path the same span assigns. The placeholder is defined once in the Quickstart, and the init
  subsections say to run them from the repository root. `tools/onboarding_control.py` reads a
  gesture's script path from the repository root, as a shell would, and runs every gesture under
  "What `/agrim-init` will ask you" and "If `/agrim-init` says you lack access". Run against the
  previous README, both confirm variants went RED at `verify confirm (README gesture)`. (2) The
  fine-grained token row shows `-s <required scopes>` and states the rule `preflight.py` applies
  (`repo`; `workflow` when work is on; `read:org` for an organization; `project` with a board). A
  test drives `preflight()` over every combination to derive that rule. (3) The `work.auto_merge` row
  no longer says a fork or read-only repository records `done`. It records `review`, and the goal
  is `done` once the PR merges. A test keeps any README or docs sentence from pairing a no-merge path
  with "records `done`". Also corrected: the `"auto_merge": false` example (now `"off"`), and the
  claim in the README and config template that a merge arms `--auto`. It lands with a direct
  `gh pr merge`. (4) Version numbers from the plugin's previous name are gone from shipped prose
  (`Since 1.0.9`, `pre-0.6`, `1.3.7`, `1.3.8`, `1.4.1`, `1.4.2`, `1.4.5`, `1.1.2`). The version
  control now covers any release-shaped version above `plugin.json`, and any lower one introduced
  by a release phrase. Exceptions need a stated reason in `VERSION_ALLOW`. (5) Only verified host
  claims remain. `codex-cli 0.154.0-alpha.6.2` installed `sigma@sigma` from
  `.claude-plugin/marketplace.json` into an isolated profile. Cursor plugin support is marked
  unverified. (6) `examples/hello-sdlc/README.md` shows the real `status.py` line, and a test
  checks it. The stale `.gitignore` tip is gone because init ignores runtime directories itself.
  (7) `tests/test_readme_first_run.py` also checks `/agrim-*` names in plain prose, and checks
  `claude plugin` / `codex plugin` syntax against the CLIs' help. (8) The `loop.py start` nag after
  `--local-only` was already fixed by #236 and has its own control, so nothing changed. (9) New
  `tools/leak_scan.py` is the leak gate that #231's acceptance and `scrub.py` name. It reports
  locations only, never values: 0 findings over all 614 tracked files, and each plant is red
  (`tests/test_leak_scan.py`). After review it scans EVERY tracked file (`.sdlc/` and `tests/`
  ship too; a first draft skipped them and missed a real home path in `.sdlc/plans/258.md`, now
  `<home>/…`); its secret rules are scrub.py's `SHAPE_RULES` plus `_SECRET_PATTERNS` (PEM keys, AWS
  `AKIA`/`ASIA` -- `ASIA` added to scrub.py itself -- classic `gh[pousr]_` tokens, JWTs, bearer
  auth) plus its own `config-credential` rule for config-like files (see below); the home rule also catches a
  bare `/Users/<name>`, `~<name>/` and the host-encoded `-Users-<name>-`; the owner-link rule
  catches ssh, `git@`, `raw.githubusercontent.com`, `api.github.com/repos` and userinfo spellings,
  allowing this repository and doctor.py's public slug. 98 lines of token-shaped test fixtures in 16
  files are now built from fragments, and a line may opt out of one rule only with an in-file
  allow marker that names the rule and a reason (the syntax is in the tool's docstring; a malformed
  marker is itself a finding). Measured: 4.1-4.3 s
  for the whole tree on Python 3.10 and 3.12. Second review: the credential rule borrowed from
  scrub.py began with `\b`, which cannot match after `_`, so `GITHUB_TOKEN=`, `OPENAI_API_KEY=`,
  `DB_PASSWORD=`, `aws_secret_access_key =` and every other prefixed key were missed. The gate's own
  `config-credential` rule now matches any key that ENDS in a credential word, quoted or not, in
  `.env*`, `.ini`, `.cfg`, `.conf`, `.toml`, YAML, JSON, `.properties`, `.npmrc`, Dockerfile
  `ENV`/`ARG` and shell scripts, with placeholders (`${VAR}`, `<...>`, `your-...`, `changeme`)
  allowed. New rules scrub.py's comment had claimed and the gate lacked: `secret-file` (`id_rsa`,
  `*.pem`, `*.key`, `*.p12`, `.netrc`, `credentials.json`, a non-example `.env`, ...), `key-body` (a
  private key body with its header stripped), and a named, exit-1 finding for any file it cannot
  scan (`opaque-binary`, `oversize`, `unreadable`) unless `ALLOW_PATHS` gives a reason. Latin-1 and
  UTF-16 text is decoded and scanned. A symlink is scanned as the target path git ships and never
  followed. Home paths now include WSL's `/mnt/c/Users/`, macOS's `/System/Volumes/Data/Users/`
  and lower-case `c:\users\`; owner links include `ssh.github.com:443` and `github.com:443`. A
  missing `git` exits 2, the summary counts allow-marked lines, and the docstring lists what is out
  of scope. scrub.py's comment now names only rules that exist, and a test keeps it that way. (10) The README's two blocker escapes are corrected:
  `backlog_check.py dismiss-text` only prints the marker, which `loop.py note` must post, and it
  downgrades one cross-check finding to advisory (GitHub mode only); the keep-parked opt-out is a
  `<!-- sigma:keep-parked -->` comment, not an `auto_unpark.py` command (that line exited 2). The
  onboarding control gains a `readme-usage` mode that checks every `python3 <installed-sigma>/...`
  README gesture against its script's `--help` usage, and asserts each executed init gesture's
  effect, not just its exit code. (11) The README no longer claims an "85% coverage floor": CI
  measures no coverage (#194); a test keeps a coverage claim out of shipped prose unless CI
  enforces one. `verify_detect.py` / `preflight.py` / `state.py` now print `<installed-sigma>`, the
  README's placeholder, instead of `<sigma>`. (12) The README's auto-unpark section said the
  sweep was opt-in and off by default, and that it drops `sdlc:parked` from a goal whose blocker
  closed. The scaffolded config ships `"mode": "on"` (`sources.DEFAULT_AUTO_UNPARK_MODE`), and since
  #1394 the sweep lifts only the `sdlc:blocked` overlay and never resumes a park. The section is
  rewritten from `auto_unpark.py`, and a test pins its stated default to the template's and the
  code's, so the two cannot drift again. The blocker-chain paragraph that said a goal is "parked"
  while the sweep resumes it now names the `sdlc:blocked` overlay. The `readme-usage` mode of
  `tools/onboarding_control.py` now finds a `python3 <script>.py` gesture in any spelling and is
  red for one whose path is not `<installed-sigma>/<shipped script>`. Its usage parser also reads a
  nested optional group (`[--apply [--replace-old-plugin]]`, `migrate.py` since #326) as one flag
  group, and `VERSION_ALLOW` gives a reason for `docs/upgrading.md`'s `1.4.25` (#326).

- **A Status option spelled differently from `project.columns` no longer makes a card move a
  silent no-op** (#280). This was confirmed on the fake with GitHub's default options. A read-only
  read on 2026-09-29 found both `In progress` and `In Progress` on real default-shaped boards,
  including one titled `<repo> — SDLC`, the loop's own default title. Before this fix, a board the loop created itself (`_ensure_board`) kept GitHub's
  `In progress`, while every pick looked up `In Progress`. `mark_in_progress` then wrote nothing
  and printed nothing. (1) That fresh-board path now renames `In progress` in the same id-preserving
  update that renames `Todo` to `Backlog`, so the built-in workflows stay on. (2) Status options are
  matched exact-first. Failing that, the one option that differs only in case or whitespace is used
  under the board's own spelling, in memory. Adopted boards are never renamed. `Ready` is the
  exception: it is matched exactly, because it decides the queue mode. A lane spelled `READY`,
  `ready` or `Ready ` keeps the label queue and prints one notice per run that names the lane and
  the two opt-ins (`project.columns.ready` set to that spelling, or `board_migrate.py`). (3) A
  column with no matching option prints one warning per run that names the column, its config key
  and the board's options. `Ready` is exempt, because a board without it is the label queue. So is
  a park that falls back to `Blocked`. (4) `/agrim-doctor` has a read-only row, **board Status
  options match the loop's columns**. It uses the loop's own resolver, so two lanes with one name
  count once, as they do in the loop. It shares one `gh project field-list` with the custom-fields
  row and is off under `cheap_only`. The init template's column map is pinned to the loop's
  defaults by a test. Details: `docs/board-fields.md`. Tests: `tests/test_board_status_spelling.py`.

- **Done-means-merged hardening** (#255, the review findings on #232). (1) The documented cost is
  now true. A merge-reconcile pass that closed a goal used to read its PR twice over REST and then
  make three `gh pr view` (GraphQL) calls in the checkout release, while the docs said "at most 10
  REST reads per pass, never GraphQL". It now makes one REST `pulls/<n>` read per goal and no
  GraphQL read: the close and the release reuse that read. Closing a goal adds one REST read only
  when the ledger or journal is on. The issue writes `source.complete()` makes are separate and not
  counted in that figure. (2) The pass holds a kernel lock (`state/merge-reconcile.lock`, `flock` or
  `msvcrt`), so a `next` and the watch tick can no longer both record `done`. A second pass skips
  and names the holder pid, and a host with no lock refuses loudly. (3) `record` clears the awaiting
  flag right after its terminal ledger entry, before the slow tail, so a pass killed by the tick
  timeout never records `done` again. The pass also stops starting goals after half of
  `SIGMA_WATCH_CALL_TIMEOUT`. (4) LIVENESS: `/agrim-doctor` has a `goals awaiting merge` row, not OK
  past 3 days of waiting or a day without a PR read. `/agrim-status` adds an `awaiting merge: N
  (oldest … for 3d 04h, last PR read …)` segment. `log.py` keeps a `review` goal in flight
  (`awaiting merge for 3d 04h`) where it used to count it as closed. (5) `record parked` or `record
  failed` on a goal awaiting merge clears the flag, so a later merge no longer records `done` over
  the decision. (6) `work.py finish` refuses a goal awaiting merge, armed PR included, where it used
  to delete the only record of the PR. (7) A failed close is retried quietly, and only the 3rd
  consecutive failure parks the goal, once. (8) The end-to-end repick check now drops the
  `sdlc:in-progress` label first, so it tests the local skip. (9) `run_loop` reports `review`
  apart from `parked`. Tests: `tests/test_merge_reconcile.py`, `tests/test_public_bootstrap_control.py`.

- **Ported two fixes the predecessor has not released: `release()` says when a `gh` call failed, and
  `pr-review` refuses a malformed `--artifact`** (#285). Both are in the predecessor's tree but in
  neither its released 1.4.26 nor Sigma; each was merged file by file with its tests.
  - `GitHubSource.release()` used to swallow a failed label removal or audit comment, so a release
    reported success while the label stayed attached and no comment was posted. Each failure now
    writes one `sigma:` line to stderr and is recorded in a new `release_warnings()` getter that
    resets on every call, as `read_degraded()` does. The return contract is unchanged (`True`
    released, `False` for the no-op on a closed or parked goal), so `_release()` in `loop.py`, the
    `release` verb and every test double behave as before. A failure still does not stop the
    release, and `_release()` still writes its ledger entry when the label stayed attached: the
    stderr line is the only surface today, because nothing reads the getter yet. Where Sigma differs
    from the predecessor: the warning quotes `gh`'s own stderr, whitespace collapsed and capped at
    200 characters, and the getter returns a copy. The predecessor formatted the whole exception,
    which for the audit comment carries the comment `--body` (a stale-resume reason); that text was
    bound for the issue anyway, but a log line should not carry it whole.
  - `review_context.py brief ... --for pr-review --artifact 2819 --output <path>` (no `PR#`) used to
    publish a manifest with no `pr`, `head_sha`, `base_ref` or `diff_sha256` and exit 0, and the
    mistake surfaced only at `work.py post-review`, after a whole review cycle. It now exits 2, names
    the required `PR#<N>` form and writes neither a manifest nor a generation directory;
    `--artifact "PR#2819"` still publishes. The check is in `publish_generation`, which takes an
    optional `phase`: `brief()` stays permissive (a bare number still prints a brief), and every
    other phase keeps its unbound manifest. The usage strings now spell `"PR#<N>" for pr-review`.
    Not closed here: `--for pr-review --output X` with no `--artifact` still writes an unbound
    manifest; that gap is a follow-up.
  - Not measured: a real `gh` failure against GitHub. Both fixes were exercised with a fake `gh`
    script and the test doubles.
- **Board mirroring survives a repo rename, the docs now say what the old `setup_created` marker
  does, and a Phase-field race recovers** (#308, follow-ups from the #233 review). (1) Since #233 the
  status path skipped board cards whose `content.repository` differs from `discovery.github.repo`.
  After a rename or transfer with a stale config, every card read as uncarded. The sync then tried
  `item-add` with an old-name issue URL, and on github.com such a URL does not resolve
  (`resource(url:)` is null, measured read-only, evidence recorded on #308), so board mirroring
  stopped. If `item-add` had returned the existing card instead, In Progress and Blocked cards would
  have been reset to Ready, which the fake reproduced. The configured name is now resolved with one
  `gh api repos/<repo>` read. GitHub answers with the current `full_name`. The read happens when a
  card names another repository or before the first `item-add`, whichever comes first, so a board
  holding none of our cards yet still gets new cards under the current name. A success is cached
  for the source's life. A failure is cached for 5 minutes, then retried, so a long-lived watcher
  is not stuck on one failed read. While the read fails, the strict rule stands and another repo's
  card is never written. (2) `docs/board-fields.md` promised that `board_setup.py create` would
  re-pin the pre-#233 bare-number marker, but `pin()` deletes it. That deletion is right and stays:
  the pre-#233 `pin()` kept the bare marker when only the owner changed, and so does a hand edit of
  `project.owner`, so the owner beside it cannot vouch for it. An upgrade would have made a
  hand-made board of another owner, reusing the number, read as Sigma's. The docs now say the truth:
  the bare form reads as not Sigma's and any re-pin drops it. To make that board's Priority column
  Sigma's, set `project.mirror_priority: true`. To create a separate board, first remove
  `project.number`, then run `board_setup.py create` without `--number` and with an unused
  `--title`; omitting the flag alone reuses the pin (#317). Existing cards are not migrated.
  (3) When two first phase starts both create the Phase field, the
  loser's create is refused ("Name has already been taken" on the fake; not measured on a live
  board). The loser now re-reads the card once and writes Phase into the winner's field, where
  before it printed a warning and skipped the write. Tests: `tests/test_board_phase.py`,
  `tests/test_board_setup.py`. The fake's `renames` models a renamed repo. None of it was run
  against a live board.
- **Declining the verify command no longer parks every github-mode merge** (#312). The onboarding
  control found it: a github-mode user who declines the verify command (the README offers it; the
  scaffold writes `verify.enforce: false` and no command) had every `work.py merge` parked on "no
  fresh verify evidence for this run" -- evidence `loop.py verify` cannot write with no command
  (it exits 3, NO-COMMAND). **Decision:** evidence is demanded when verify is REQUIRED, and
  `state.verify_required` is the one rule: `verify.enforce` on, or a verify command declared (goal
  frontmatter or config) even with enforce off. With neither, the merge proceeds to its review, CI
  and clean-state gates; with either, it parks exactly as before (stale, failed, foreign-run or
  missing evidence). `enforce_enabled` and `declared_verify_command` moved from loop.py to
  state.py so `record done` and the merge read one copy; `record done` still gates on the enforce
  half only, and its github path reaches `done` through the merge, so the merge never requires
  less. A park with enforce on and nothing to run now names the `verify_detect.py` fix instead of
  "run verify". `tools/onboarding_control.py`'s github no-command variant now runs the real work-ON
  path (no `--local-only`) and is green; the guard reverted turns it red through the documented
  gesture. Also: the `sdlc:blocking` overlay lane no longer retries a successful EMPTY read (a
  raised read is still retried) -- a stock github `next_pending` with no blocking issue measured
  3.005s before and under 10ms after, against the same fake runner; the backlog's own empty-read
  retry (#447) is unchanged. Nothing here was run against real GitHub.

- **The canonical board: fields and six views, applied and checked by a script** (#234). New
  `skills/agrim-init/scripts/board_spec.py` is the single definition of the board, built from the
  kit's own vocabulary: the configured Status lanes, `discovery.PRIORITIES`, `PHASE_TOKENS`, and the
  parked, blocked and needs-confirmation labels. It covers the fields (Priority P0..P4, Phase, Area,
  Model tier) and six views: Board · by Status, Priority table, In flight, Needs a human, v1.0
  roadmap and Epics. New `board_layout.py fields|views|verify|spec` applies it. It is a dry run
  until `--yes`, and it acts on the pinned board only when `board_setup.py` created it; any other
  board needs `--number N`. It never deletes or renames anything, and never duplicates a field or a
  view. If several views answer to one name, that view is refused. A drifted spec view gets only
  the differing properties, and columns you added are kept. **Measured, read-only schema
  introspection (2026-09-29):** the API can set a view's name, layout and visible columns
  (`createProjectV2View`) and its filter (`updateProjectV2View` only). It cannot set group-by,
  sort, the board's column field, roadmap markers or the default view, and it cannot create a
  workflow. Those are printed as exact UI steps, and only while the board reads differently.
  `verify` is the read-only acceptance check: 3 reads, 1.6–1.8s on board #17, exit 1 naming each
  difference. **Decision:** "Needs a human" filters on the three labels, because a project filter
  cannot OR across two fields. `/agrim-init --board yes` now prints the layout commands and the
  `--template` alternative, and still runs only `create`. `docs/board.md` has the design and the
  owner runbook: building board #17, setting the `v1.0` milestone on open P0/P1 issues, the
  `copyProjectV2` control and the timed 15-minute reproduction. **None of it has been run on real
  GitHub.** Area and Model tier have no writer yet.

- **The README Quickstart is now a control that runs on every push** (#237). `python3
  tools/onboarding_control.py` follows the README text -- the init script, the `/agrim-init`
  flags, the `verify_detect.py confirm .sdlc <n> <id>` gesture and the plugin install lines are
  parsed from README.md, not restated -- on a fresh repository to one goal `done`: local-goals
  mode with no remote, and github mode against a fake `gh` and a local bare origin, where the goal
  is `done` only after its PR merged (the review gate parks on `sigma:block`; `record done` is
  refused while the PR is open). Each mode runs twice: a `confirm` variant (a Makefile target,
  confirmed through the README gesture) and a `no-command` variant (nothing to confirm, the verify
  question left open) -- the only one that sees the default init scaffolds, since a confirm
  overwrites it. `/agrim-init`'s `[ask]` lines now end in one machine-readable shape, `-> --flag
  VALUE|VALUE ; ...`, which the control parses and answers by flag name, so a renamed flag is red;
  every init flag the README shows is checked against init_flow.py's parser, and every README
  command runs only as `python3 <existing Sigma script> <args without shell syntax>`. It writes a
  per-step timing log and a result JSON that records every gh call by kind. `--install` runs the
  README's `claude plugin` / `codex plugin` lines into an isolated profile and hashes the real one
  before and after (CI runs without it). Seen red: the original bug (template `enforce: true` with
  the scaffold's rewrites removed: both no-command variants red at `record done`),
  `write_verify` regressed, every `[ask]` flag renamed, README drift in any gesture, install line
  or init flag, a shell command in the README, and each assertion broken once. Recorded run, what it does not cover (a live model turn, real GitHub, live Codex and Cursor
  sessions, Windows -- #300-#303, #305) and an owner runbook for real GitHub:
  [docs/onboarding-control.md](docs/onboarding-control.md). Its gh call log found the loop's
  label self-heal making 20 needless `label create` calls per goal cycle (#304).
- **The board card shows each goal's Phase and Priority** (#233). Before this, only Status reached
  the board. Now every `phase_report.py start` writes the phase the loop is entering (`P1 GOAL` ..
  `P7 RETRO`, imported from `PHASE_TOKENS`) to a `Phase` field on the goal's card, and mirrors its
  Priority from the `priority:*` label. Every status write also mirrors the moved goal's Priority,
  using labels the run has already read (not onto a card moved to Done). On a pinned board
  (`project.enabled` + `project.number`) a missing Phase field is created once as a single-select
  with fixed options. **The label stays the source of truth:** a Priority field is created, and
  treated as a writer (#719's field-wins rule), only on Sigma's own board — one the loop or
  `board_setup.py` created (`project.setup_created`), or with the new opt-in
  `project.mirror_priority: true`. On any other board the loop never creates the column, never
  rewrites a label from it, and only fills a blank one, so a person's `priority:P1` -> `priority:P0`
  edit is never reverted. Fields are matched by one case-insensitive name rule in both the phase path
  and the backlog sync; ambiguous case-variants are never guessed. Extra options are kept, a
  missing option is reported and never appended, and nothing is renamed, recoloured or dropped. A
  card is matched by repository and number, so a multi-repo board's same-numbered card is never
  written (the status path's `item-list` read now skips other repos' rows too). **Cost:** one
  GraphQL read of the issue's own card per boundary, plus at most 2 writes. Measured read-only on
  board #17 (246 cards): 0.66–0.72s, against about 11.6s for the three whole-board reads it
  replaced; it does not grow with the board. The write is fail-open: it never changes a pick's or a
  start's exit code or banner, and prints at most one warning per run. Each `gh` call is
  time-bounded; an overrunning one is killed with its whole process group (POSIX) or tree (Windows,
  `taskkill /T`), is never retried as transient, and ends that boundary's board write. New config:
  `project.phase_field` (default `"Phase"`, `false` disables it) and `project.mirror_priority`.
  **Decision:** an unlabelled goal's Priority stays blank, not P3. "No priority" sorts after P4, and
  writing P3 would reorder the queue. Acceptance ran on the in-memory board fake only. The live run
  on board #17 was not executed; it is an owner runbook in
  [docs/board-fields.md](docs/board-fields.md). **Review fixes:** `project.owner: "@me"` is resolved
  to the viewer's login in the same GraphQL read and compared like any owner (it used to skip the
  owner check, so a phase start could write an org's same-numbered board instead of the user's own);
  an unresolvable viewer writes nothing. `project.setup_created` is now `{"number", "owner"}`
  (`board_setup.pin` drops it on an owner or number change; the old bare-number form reads as not
  Sigma's board). Ctrl-C during a board call kills `gh`'s process group before re-raising. An
  unparseable config on a repo with no board prints nothing. The backlog sync's lone case-variant
  `PRIORITY` adoption now fills blank cells on adopted boards where it wrote nothing before.

- **Ported the predecessor's 1.4.26: a haiku signal counts only in the title, and a review send-back
  escalates the tier instead of parking** (#283). Its two changes, merged file by file with their tests:
  - A haiku model-tier signal counts only in the goal's title: the text's first line, or a goal
    file's frontmatter `title:` (else its file stem). `predict.py` scanned title and body together, and
    the predecessor found that a real issue body almost always quotes a code comment, a docstring or a
    lint (its observation, not measured on Sigma's boards), so a non-trivial goal was routed to
    `haiku` by one body word. The predecessor counted 24 open goals across its two boards
    routed that way; that is its measurement, not one taken on Sigma's boards. A body-only haiku stem
    now gets the `sonnet` default; opus and fable stems still count anywhere; `resolve-step` is
    unchanged. `predict.py why` says where the signal was found (`model=haiku in=title signal=typo`),
    with the signal still the text after `signal=`. The documented GitHub fallback prints the title on
    its own first line (`gh issue view N --json title,body --jq '.title + "\n\n" + (.body // "")'`).
  - New `loop.py escalate <dir> <goal> <tier> --after plan-review|code-review|pr-review`, backed by
    `tier_escalation.py`. The first word of its output is the answer. `ESCALATE <next> effort=<e>`
    (exit 0) steps one rung up `haiku` → `sonnet` → `opus` within `model_selection_max_tier`. It
    records a `model_choice` event with the signal `escalated: <gate> send-back at <tier>` when the
    journal is on (it ships off) and, separately, a row in the action log when that is on (it ships
    on from `/agrim-init`). `CEILING <tier>` (exit 3) means no higher
    tier is allowed, so the usual fix cycle continues. `OFF` (exit 3) means `model_selection` is not
    `auto`. `fable` is never a target. The raised tier is control state in
    `.sdlc/state/escalation/<goal>.json`, written whatever the journal, ledger and action-log settings
    are, so a goal escalates at most twice even when a resumed caller repeats its pick-time tier, and
    `loop.py escalate <dir> <goal> --show` returns the raised ceiling. The `/agrim-loop` park list and
    `/agrim-plan-review` now say that budget or tier size is not a park reason and print the gesture,
    which a test runs. `README.md` and `docs/label-model.md` now say a send-back can raise a goal's tier
    after the pick, so the tier no longer comes from the goal text alone.
  - Where Sigma differs from the predecessor. The verb checks the Codex host mapping
    (`model_host_overrides.codex`) before it writes anything, as `predict.py resolve` does, and exits 2
    with nothing recorded when the mapping is refused. On Codex the `host-model` resolver's effort is
    the one to use, not `effort=`. `OFF` no longer claims the phases run at the session model, which
    is not true of a Codex dispatch. `tier_escalation.py` has no `__main__` stub: a direct call to the
    module would be a silent no-op, and the entry point is `loop.py escalate`. To keep
    `skills/agrim-loop/SKILL.md` under its size cap, the optional pipeline-report-card paragraph moved
    out (`references/landing.md` already carries it in full), the wake-and-work paragraph lost the
    clause `AUTOWATCH.md` already opens with, and the park-list paragraph is a shorter wording with
    the same trigger: escalate before any park after a review send-back, at the latest on the second
    send-back at one tier. `references/running.md` has the full text.
  - Scope, said plainly: the verb decides and records on every host, and the re-dispatch at the new
    tier is the host's. Claude and Codex can do it. Cursor has no per-subagent model override, so
    there the answer is advisory and the operator switches the session model. Not measured: a real
    host re-dispatch at an escalated tier, and the effect on Sigma's own boards. The escalation file is
    about 73 bytes plus one inode for each goal ever escalated (at most one per goal) and is never
    pruned, so 100x the goals is at most 100x those files; a call reads it by exact path. Inode and
    directory-scan cost were not measured. Pruning it once its goal is done is a follow-up, not done here.

- **Rebase upkeep keeps merge-commit landings instead of flattening them, and a locked unit has
  an exit** (#161, ported from the predecessor's #2756). A goal that landed on `feature/<unit>` as
  a "Merge pull request #N" commit was flattened by the next upkeep pass: a plain `git rebase`
  replayed its second-parent commits onto the first-parent line, where their subjects carry no PR
  trace, so every later pick refused the branch as carrying "commits no pull request accounts
  for" -- forever, with no way out inside Sigma (measured on a real host repo: 18 commits, 76
  behind `main`, five teammates' loops blocked). Upkeep and `/agrim-rebase` now run `git rebase
  --rebase-merges`, which recreates the merge with its subject intact; a squash-only branch
  replays exactly as before. For a branch that was already flattened, `feature_rebase.py ack .sdlc
  <unit> <sha>...|--all` records the confirmed commits in the tracked
  `.sdlc/features/rebase-acks/<unit>.json`, keyed by patch-id so the ack survives the rebase it
  unblocks, and read from the remote integration branch too so one landed ack frees every
  teammate. Only commits the check currently reports can be acked. The filed finding now names
  both real exits instead of a remedy that had no mechanism. **Sigma-only:** the #144 data-loss
  guard stays the outer guard -- an ack only lets the pass reach the replay, and a replay that
  would remove or roll back the branch's content is still refused before any push (acked commits
  plus a revert in the base: `would-drop`, remote unchanged), including when `--rebase-merges`
  puts merge commits in the replayed history. The predecessor's companion fix in the same
  release, `promote.py list` saying UNKNOWN for an unread queue (#2757), was already ported.

- The rebase loss guard (#144) is now exact about what a human decided and what the branch already
  had (#278, closes #144's two open review findings). **The conflict walker no longer exempts a
  whole file for a one-line resolution.** Resolving one conflicted hunk used to exempt the whole
  path from the guard, so a base revert that git had already merged outside the markers (300 of the
  branch's lines in the repro) was force-pushed. Now only a resolution that deletes the file
  (action `removed`, ABANDON on a file the base deleted) is exempt, and content resolutions stay
  guarded. The walker's option `[3]` now says what it does: "Take the base's version of the whole
  file (drops this branch's changes to it)", not "Abandon this hunk". **`rebase_brief.push_branch`
  now allows a local deletion that was never pushed.** It used to compare HEAD only with the
  remote tip, so a local `drop obsolete` commit was refused, and the advice (`git reset --keep
  <remote tip>`) threw away the local commits. It now also takes the pre-rebase head:
  `attempt_rebase` passes it, the walker reads the stopped rebase's `orig-head`, and the
  manual-recovery push reads `<branch>@{1}` when the branch reflog's newest entry is a rebase's own
  landing on that branch (`rebase (finish): …`, `pull <argv> (finish): …` for a one-go `pull
  --rebase`, or `rebase (continue) (finish): …`, measured against real git; older gits' `rebase
  finished: …` accepted, not measured). A loss that already exists between the remote tip and that
  head is exempt **only when it is the branch's own deliberate local DELETION**: the path is in
  the remote tip and gone from that head, a non-merge commit unique to the branch has a `D` for
  exactly that path, and the base has not touched the path since the remote tip. So a local `git
  rm` commit passes (amended or squashed too), but the same loss left by an earlier, never-pushed
  local rebase onto a base holding a revert is refused (it was force-pushed before) — including
  when a sibling branch commit edited the same file, which the first version of this rule took for
  the loss's author (review block #2: `x.txt` 359 → 60 lines force-pushed). **A rollback in that
  range is never exempt**, even the branch's own: the human confirms a deliberate one with the
  `git push --force-with-lease <remote> HEAD:<branch>` the refusal prints. With no base, no
  pre-rebase head, or a **shallow clone** (the refusal says so) nothing is exempt, and all of one
  push's guard reads share one wall-clock budget, `SIGMA_WATCH_CALL_TIMEOUT` (default 120s);
  running out refuses the push. Everything the rebase itself loses is still refused. The advice names the pre-rebase
  head. When that head is unknown, the advice points at `git reflog <branch>` and no longer at the
  remote tip. If a refusal cannot put the branch back (`git reset --keep` fails), the refusal is
  recorded in the git dir and every later push of that branch (`push_branch`, `work.rebase()`,
  `work.pr()`) is refused with the recovery command until HEAD is back at the pre-rebase head.
  **`work.rebase()` runs the same guard before its goal-branch force-push**, on both the plain path
  and the CHANGELOG union rescue, but only over the paths the goal changed since it forked. So a goal
  whose commit reached the base as a copy (a rebase-merge) that was later reverted is no longer
  replayed away silently. It returns `rebase refused, it would lose content: …` (classified
  `needs_decision`, not `merge_conflict`: nothing conflicts), pushes nothing and resets the worktree
  to its pre-rebase head. `docs/branching-model.md` §15 now states the fleet-wide cost of the
  conservative refusal: a reverted dependency bump blocks upkeep on every feature branch cut in
  that window, and each branch stays blocked until a person resolves it. Each fix has a real-git
  test, and each test was seen red against the old code and against a deliberately broken guard.
- **The plan-review verdict is a record: `work.py pr` can refuse a plan that was not reviewed**
  (#258). Until now the plan-review verdict was an optional journal event nothing read.
  - The plan-review brief now names the exact bytes under review: a `Plan file:` line and a
    `Plan sha256:` line (sha256 of the plan's raw bytes; the committed branch copy, pointed at with
    `git show`, when the main checkout has none).
  - New verb `work.py record-plan-review <sdlc> <goal> --verdict SOUND|SOUND-WITH-REFINEMENTS|FIX-FIRST
    --plan-sha256 <hex> [--reason <text>]`. The sha comes from the brief written at dispatch (the
    documented gestures read its first `Plan sha256:` line, never a rebuilt brief). It refuses,
    writing nothing, when any existing copy of the plan (main checkout, committed on the branch) no
    longer holds those bytes. It stores `{at, goal, plan, plan_hash, reviewer_route, verdict}` at
    `.sdlc/state/gates/<stem>.json` (atomic replace) only while `work.enabled` is on and that
    `.sdlc` holds the goal's work record; otherwise it keeps no file and says so. `work.py finish`
    prunes it with the work record.
  - It is now the single emitter of the `gate` journal event (`gate=plan_review`, verdict
    `pass|warn|block`, no new field); `agrim-plan-review` no longer tells the agent to `loop.py
    emit` one.
  - New opt-in `gates.plan_review.enabled` (ships OFF, not org-lockable, #174). With it on,
    `work.py pr` refuses to push unless an approving verdict (SOUND or SOUND-WITH-REFINEMENTS) is
    recorded for the exact bytes of the plan on the branch (the main checkout's copy when the branch
    carries none). A malformed record, an unreadable plan, or a recorded plan that no longer
    resolves fails closed. Not covered: `<stem>.slices.json`, design PRs, a goal with no plan (left
    to `gates.hard_plan_gate`), and pushes after `pr`'s own (a `work.py rebase` force-push,
    `merge()`). The record is agent-written: it proves a verdict was recorded, not who reviewed.
  - `docs/enforcement.md` regenerated: "Plan review before implementation" moves from `advice` to a
    `Python gate` row (`work.py` `_plan_review_refusal`). `/agrim-doctor` gains a
    `plan-review gate` row, including ON-but-not-enforced when `work.enabled` is off.

- **`/agrim-init` is the one entry point; `/agrim-setup` is its alias** (#236, folds in #186).
  - New `skills/agrim-init/scripts/init_flow.py` runs, in order: preflight (#229), mode
    (local-goals or github; github by default when `origin` is a GitHub repository), the verify
    command (#228), and in github mode the labels (#230), `assignee: @me`, the board OFFER (#235)
    and the ledger question; then a one-screen summary and the next command. It integrates the
    sibling scripts; it does not re-implement them.
  - Questions are flags, so Claude Code, Codex and Cursor run the same flow: `--mode`, `--repo`,
    `--local-only` / `--work on`, `--verify N:ID` / `--verify-command-file` / `--no-verify`,
    `--board yes|no`, `--ledger yes|no`. An unanswered one prints an `[ask]` line with its flag.
    `--yes` takes only the detected mode and ledger off, and only for a question config.json does
    not already answer; it never answers the board, the verify command or a work flip.
    `.sdlc/config.json` is the one source of truth: `.sdlc/state/init.json` records only that a
    question was answered, never re-applies a value, and never supplies the repository. A re-run
    keeps a setting changed since (`preflight.py local-only`, a hand edit) and prints
    `[kept] ... config.json wins`; only a flag on that run changes it. Measured in
    `tests/test_init_flow.py` on a fake `gh`: a bare re-run makes zero label writes and leaves
    config unchanged; `--yes` on a repository configured before the flow (local-goals, ledger on)
    makes zero label writes and changes no key (it made 14 label writes and switched the source
    before this review). A scaffolded template value is open only while config.json still matches
    the SHA-256 + mtime init.json recorded when the flow last wrote it; `setup.py configure
    --source local-goals` or a hand edit that leaves the template's own value is therefore kept
    (review block #2: `--yes` had switched it to github with 14 label writes). Measured the same
    way: zero label writes, source kept, after configure, a hand edit, a byte-identical re-save,
    and a corrupt init.json; an untouched scaffold still takes the detected mode. Exit 1
    on a failed step or a blocking preflight problem, with a `Resume:` line; exit 2 when refused
    before any write.
  - When the flow switches a repository to github mode, `discovery.github.project.enabled` is
    turned off until `--board yes`: the loop otherwise creates a board on its first github pick,
    so "no board without a yes" held for init and broke at the first `loop.py next`. `--board yes`
    turns it on only after `board_setup.py create --yes` succeeds and a board is pinned (also for a
    board pinned by hand after declining); a failure leaves it as it was.
  - `--repo` must be `OWNER/NAME` (a name ending `.git` is refused). After a successful
    `--board yes` the flow no longer relays board_setup's "project.enabled is not true" note that
    it makes false one line later. The session wizard's interrupted-scaffold fix prints the
    interpreter and quoted path through the shared helpers (was `python3` and an unquoted path). The `Resume:` line prints `--verify-command-file` absolute, and
    on Windows is withheld (as `board_setup.py` does) when a value carries `"`, `%`, `$`, a
    backtick or `!`. The demo's github hint no longer says the loop creates a board when mirroring
    is off.
  - The scaffolded example goal ships `status: proposed`; the loop's first pick on a fresh repo
    was "Example goal — delete me". `--demo` still queues a runnable demo.
  - `setup.py configure` refuses (exit 2, nothing written) github mode with no repository, and a
    missing `.sdlc/` (was a traceback); a repository config.json already names is kept, never
    refused for a missing origin and never swapped for `origin`. `setup.py detect` returns nothing
    for a non-GitHub origin (a GitLab ssh URL read as `o/r`). `setup.py init ...` forwards to the
    flow.
  - The setup wizard fires only in an adopted repository (`.sdlc/config.json`, and no other
    plugin's `state/owner.json`), in `setup_wizard.wizard_status()` so every host gets it (#186:
    it fired in every repository the user opened, where a decline could not be remembered). The
    one exception: a `.sdlc/` Sigma owns (init writes `state/owner.json` before it scaffolds) with
    no `config.json` is an interrupted `/agrim-init`, and the wizard says to re-run it.
  - `loop.py start` no longer warns about `work.enabled` off once `--local-only` (or
    `preflight.py local-only`) recorded the choice; the warning, the `record done` note and
    doctor's row point at `/agrim-init` instead of `/agrim-setup`.
  - The old `sdlc_init.py --github` still works and now says it does not switch the backlog to
    GitHub. The control, on a fresh repo with a labelled issue: the old gesture leaves the issue
    unpicked (and, before this change, picked the placeholder goal); the new flow picks it.

- **`/agrim-init` offers to create the GitHub Project board, and pins it** (#235). The loop only
  creates a board when the owner has none, so in any real organisation nothing ever wrote
  `discovery.github.project.number`. Now:
  - In github mode (`--github` or `discovery.source: github`, never in local-goals mode), init prints
    an OFFER block and makes no call. On Claude Code the agent asks yes/no. On Codex and Cursor the
    block carries the command to run.
  - New `skills/agrim-init/scripts/board_setup.py create <.sdlc> [--owner O] [--title T]
    [--template N|OWNER/N] [--number N] [--yes]`. Without `--yes` it only reads. With `--yes` it
    checks the gh `project` scope (preflight's check and per-host fix, #229), then creates
    `<repo> — SDLC` linked to the repository, or copies a template board and links it. It pins
    `project.number` + `project.owner` right away (atomic; no other key touched). It sets Status to
    the `project.columns` options, renaming GitHub's `Todo` / `In progress` with their ids kept so
    the built-in workflows stay on, and adds Priority `P0`..`P4` from `discovery.PRIORITIES`.
    Finally it reads back the "Item closed" workflow.
  - It refuses a title the owner already uses and prints the manual runbook (`--number N` adopts
    that board on purpose). A missing `project` scope is refused with the remediation. Any failed
    step exits 1 with the exact resume command, and a re-run reuses the pinned board.
  - GraphQL schema introspection (read-only) shows `createProjectV2View` exists, while no mutation
    creates or enables a workflow. When "Item closed" is off, board_setup prints the manual step and
    the `/projects/<n>/workflows` deep link. Whether `copyProjectV2` carries views and workflows
    could not be checked without a mutation; `references/board.md` has a 5-step check for the owner.
  - The loop honours a pinned number outside the first 100 boards (one `gh project view`, only
    then). A pinned number now wins over a title match.
  - New doctor row: `pinned board #N reachable`. It fails only when GitHub answers that the board
    does not exist. When the read itself fails (offline, rate limit, gh missing, no `project`
    scope) there is no row. The read is skipped under `cheap_only` (the SessionStart wizard).
  - Adopting a board does not change how the loop picks work (review of PR #279, block #2). The
    `Ready` lane is the loop's queue switch: with it, only a card in `Ready` is picked, so adding it
    to a board whose goal cards sit in other lanes stranded them and the loop read DONE (reproduced:
    pick `5` before adoption, nothing after). board_setup now adds `Ready` only to a board it
    created (`project.setup_created`) that has no card yet. Elsewhere it prints `[skip] Ready lane`
    and the explicit `board_migrate.py --owner O --project N --backlog <lane> --apply` step, which
    adds the lane and seeds it. A `Ready` the board already has is left as it is. A resume of our
    own still-empty board finishes it like a fresh one; once a loop tick carded goals on it, the
    resume is an adoption.
  - The loop's empty-`Ready` warning now counts every open, eligible goal card outside `Ready` (a
    human's own `Todo`, `Needs design`, no Status), not only Backlog cards, names each lane and the
    migrate command, and prints once per run.
  - Adopting a board a human built (`--number N`, or a pin) renames nothing (review of PR #279).
    Every existing option keeps its id, name, colour, description and position, and only the
    missing options are appended after them. Only our own empty board gets GitHub's `Todo` /
    `In progress` renamed. A lane differing only by case (`In progress`) is mapped in `project.columns`. A
    same-named field that is not single-select, a `p0`-style Priority variant, or unreadable
    option colours is REFUSED (exit 2) with the manual fix.
  - `sources._options_mutation` quotes every value it puts into GraphQL with JSON escaping, and
    sends an existing option's colour and description back instead of resetting them. The fields
    read is paginated (`per_page=100`, `--paginate`).
  - The Priority field is `P0`..`P4` (from `discovery.PRIORITIES`), not the issue's `P0`..`P3`.
    On Windows the resume command is double-quoted, and it is not printed when a value contains
    `"`, `%`, `$`, a backtick or `!`.
  - Cost, measured against a fake gh: a fresh create is 4 GraphQL calls (3 mutations) plus
    5 + ceil(boards/100) REST reads. A re-run on a finished board is 1 GraphQL read. The loop's
    steady state adds no calls.

- **Done now means merged** (#232, owner decision). On the shipped defaults (`work.auto_merge:
  "off"`, `work.require_review: "changes"`) a goal used to be recorded `done` and its issue closed
  while its PR was still open, and the review gate never ran. Now:
  - `loop.py record <dir> <goal> done` exits 4 (`REFUSED: PR #N …`) unless the goal's PR is merged,
    whatever `auto_merge` says. It reads the PR once, over REST. An open PR, or one it cannot read,
    points you to `record review`. A PR closed without merging points you to `record parked`.
    Goals with no PR, including local goals, are unchanged.
  - New non-terminal outcome, `record review` (awaiting merge). The issue stays open and keeps
    `sdlc:goal` and the claim's `sdlc:in-progress`. Its card moves to QC and it gets one note. The
    ledger claim ends, the checkout is kept, and `next` never serves the goal again.
  - New `loop.py reconcile-merges <dir>`. It also runs on every `next`/`next-batch` (inside the
    budget gate) and on the watch daemon's `reconcile_tick.py`. When a waiting goal's PR has merged,
    it replays the existing merge observation and records `done`, which closes the issue and
    releases the checkout. A PR closed without merging parks the goal. It is idempotent, and a
    close that fails is retried on the next pass.
  - Cost is bounded. Each pass reads at most 10 PRs, oldest-checked first. The automatic triggers
    skip a PR they re-read in the last 120 s. With nothing waiting, a pass makes no `gh` call.
    Measured with a fake runner: 10 PR reads per pass at both 50 and 500 waiting goals, with 3 ms
    and 14 ms of local overhead. At 100x the call count stays flat and close latency grows to
    ceil(N/10) passes.
  - `work.py merge` now runs the post-PR review gate under `auto_merge: "off"` too. A `sigma:block`
    parks the merge, and a clean line reads `… — review gate passed (require_review: changes) —
    auto_merge is off, leaving PR #N for a human`.
  - The `/agrim-loop` routing, `references/landing.md`, the README table, the public-repo guide
    and the generated `docs/enforcement.md` now describe this flow.
  - `tests/test_public_bootstrap_control.py` runs one goal end to end on the defaults, against a
    fake `gh` and a bare origin. It was red on the old code, and each guard was broken once and
    seen red.
- The README's first-run path is now executable as written (#231). The "older plugin" callout
  named a floor from the previous name's version numbering, which the shipped 1.0.0 plugin could never meet; it now states the 1.0.0
  floor that `AGENTS.md` sets and `/agrim-doctor` enforces, and the upgrade command
  `claude plugin update sigma@sigma` (the old `marketplace update` line only refreshed the
  listing). The Quickstart has one install per host (Claude Code, Codex, Cursor) behind a single
  `<SIGMA_REPO>` placeholder, names `/agrim-init` as the next step (with one line on when to add
  `/agrim-setup`), and adds "What `/agrim-init` will ask you" and "If `/agrim-init` says you lack
  access" with the exact commands the preflight prints. `--github` is no longer described as
  setting up the Projects board: it copies `.github/` templates and creates labels, and the loop
  creates the board. Rows marked "(opt-in)" that are on by default are relabelled, internal issue
  references are removed from the README, and no shipped file names the previous name's
  label-model version any more. `examples/hello-sdlc/README.md` no longer tells you to install a companion. The new
  `tests/test_readme_first_run.py` checks that every `/agrim-*` skill, repo script and
  `/agrim-init` flag the README names exists, and that the floor never exceeds the shipped version.
  Measured: a clean-HOME install from a local-path marketplace (`claude plugin marketplace add`,
  `claude plugin install sigma@sigma`, Claude Code 2.1.284) and the `/agrim-init` script gestures
  in a fresh repository. The Codex install lines follow Codex's published plugin CLI and were not
  run here.

- `tests/test_risk_detect.py` is no longer flaky on macOS (#244, #145). The root cause was in
  `skills/agrim-loop/scripts/risk-detect.sh`. It ran a per-command `LC_ALL=C grep` inside a
  process-substitution subshell. With Homebrew bash (linked to libintl), every locale assignment
  calls `setlocale()`, which calls into CoreFoundation, and CoreFoundation is not fork-safe. Under
  load, the forked subshell sometimes crashed with SIGSEGV, and because the script is fail-open,
  the crash looked like "no content hits". The script now sets `LC_ALL=C` once in the main shell
  and never assigns a locale variable again. It also prints `risk-detect: content scan incomplete`
  on stderr when the content scan dies before it finishes; it still exits 0 with valid JSON.
  Measured with the whole file at `-n 8` plus 12 busy loops: 17 of 110 runs failed before the fix
  and 0 of 160 after. Two new tests guard the fix, and both fail on the old script. One runs the
  script under xtrace and checks that the only locale assignment is the top-level pin. The other
  runs a copy of the script whose scan is killed partway through, and checks that it prints the
  stderr warning.
- Sigma now runs fully next to the plugin under its previous name and replaces it (#314, which
  reverses #240's refusal on the owner's direction; #251). With both installed and enabled, every
  Sigma skill, the loop and the watcher work normally: `/agrim-init`, `loop.py start`,
  `loop.py claim`/`record`, `watch_daemon.py` and its automatic start, and `migrate.py` proceed and
  print ONE notice line naming the exact uninstall command (`claude plugin uninstall <id>`, with
  `--scope` when the install has one; for Codex, the `config.toml` table to remove). Init, loop
  start and migrate always say it; the per-verb surfaces say it at most once per run
  (`.sdlc/state/coexist.notice`, 6 hours). `SIGMA_ALLOW_COEXIST=1` now only silences the notice.
  `/agrim-doctor`'s `coexistence` row is a WARN, never a failure; `coexist.py check` prints the
  cut-over steps and exits 0; the session-start hook adds one read-only line. In a repository the
  old plugin adopted, init and `loop.py start` print one `sigma: takeover:` line with the exact
  `migrate.py` dry-run command; `--apply` runs only when the user says yes. What stays impossible:
  two watchers on one `.sdlc` (the shared lock; a Sigma watcher that meets the old plugin's names
  it and the polite `watch.stop` lever, and never signals it), and `migrate.py --apply` while any
  watcher is live (exit 2, naming the same lever). A foreign `owner.json` is a notice, not a lock:
  init and loop start record Sigma as the owner. See `docs/upgrading.md`, "Switching over from the
  previous plugin".
  The one step that converts the registry now waits for the old plugin to stop. The old plugin
  reads a registry carrying Sigma's schema id as empty. So a goal it starts afterwards in a unit
  that exists only in `index.json` writes a record for that unit in its own schema, starting from
  nothing, and Sigma's `feature_sync.py show` and `fold` then served and saved that record in place
  of the full entry (title, owner, priority, tracking issue, branch and goals lost; `authorized`
  flipped to false). Five changes (#314, reviews of PR #319):
  - `migrate.py --apply` refuses (exit 2, nothing written, dry run shown) while the old plugin can
    still run on the repository. It prints the reason and the exact step:
    `claude plugin disable <id> --scope local` (checked against Claude Code's CLI in a fake home),
    or the Codex `config.toml` edit, then the rerun. `--replace-old-plugin` converts anyway, after a
    backup.
  - Before Sigma's first registry write on a repository where the old plugin can still run, it
    saves one copy of `.sdlc/features` to `.sdlc/state/backup/features-<time>/`. That directory is
    machine-local and git-ignored, and the copy is capped at 5,000 files or 64 MB. `migrate.py`
    never rewrites the copy. The check costs 0.08 ms per registry write (measured, macOS). The
    message gives the time it was taken and says to restore it only within the cut-over window
    (it predates every later write) and to prefer `feature_sync.py repair`.
  - The registry reader treats a unit record that still carries the old plugin's schema id, next to
    an `index.json` in Sigma's schema with an entry for that unit, as a DELTA onto that entry,
    never a replacement. The rule is keyed on the two schema ids only. A first version keyed on
    the record having no title/owner/tracking issue/priority/parent. That failed on the old
    plugin's very next pick, whose owner claim fills `owner`, and on its `define.py set-priority`:
    the record was served and folded again. The index entry now wins every field it has, the
    record only fills blanks, goals are unioned, a repository only the record names is added
    without a grant, and `authorized` is never taken from the record. `feature_sync.py fold`
    writes that merged view, and it refuses (exit 2, nothing written, loss and repair named) any
    result that loses a goal of any unit, or a field, repository or grant of a merged unit. The new
    `feature_sync.py repair` rewrites such records in Sigma's schema. Known edge: an ownership or
    priority change the old plugin makes after conversion is not applied while the entry has a
    value. Tested against the old plugin's real 1.4.25 pick, owner claim and `set-priority` code
    (opt-in: `SIGMA_TEST_PREDECESSOR_GIT`) and a stand-in that always runs.
  - `docs/upgrading.md` now also says what else the old plugin overwrites on a shared repository:
    Sigma-format withheld-findings indexes (dedup history and the upstream cap erased) and landing
    and propagation records (duplicates, not loss). These are why the old plugin is stopped first.
  - The docs, the notice, the takeover line and `coexist.py check` now give the order: stop the
    old plugin on the repository, migrate, then uninstall it. `coexist.py check` prints
    `claude plugin marketplace remove` with the marketplace from the plugin id. Every printed
    command is quoted for the platform (`list2cmdline` on Windows).
- Sigma detects the plugin under its previous name on the same repository (#240).
  `skills/agrim-loop/scripts/coexist.py` reads the Claude Code settings (`enabledPlugins` and
  hand-registered hooks, with local over project over user precedence, plus the managed settings;
  comments in settings files are accepted), Codex's `config.toml` (every TOML spelling of a plugin
  entry), the `.sdlc` owner markers, and the live watcher. As first written it refused (exit 2) on
  every write surface unless `SIGMA_ALLOW_COEXIST=1` was set; that never shipped in a release, and
  #314 above replaced it with the notice. Kept from #240: a plugin enabled but not installed on this
  machine is a note; hooks match by path, not by substring; a running watcher is identified as the
  old plugin's only by a directory named exactly the old name in its script path, never by the
  repository path it was given (on macOS and Windows, where the command line cannot be read, only
  beside another active signal); Sigma names a watcher it did not start instead of only reporting
  "already running"; new local state `.sdlc/state/owner.json` and `.sdlc/state/watch.owner`.
- `/agrim-init` now checks what the loop needs from git and `gh` before the first goal does
  (#229). The new `skills/agrim-init/scripts/preflight.py` (stdlib only) checks: a git repository,
  the `work.remote` remote (default `origin`, or which remotes exist), the base branch pushed
  there, `gh` installed, `gh auth status`, and the token's scopes: `repo`, `workflow`, `read:org`
  when the owner is an organization, `project` when a board is on. It parses both `gh` scope
  formats. A fine-grained or app token reports no scopes, so that case prints `CANNOT VERIFY`,
  never a pass. A directory that is not a git repository is refused before anything is written.
  Every other failure prints one line for each host (Claude Code, Codex, Cursor) with the exact
  command, plus what Sigma does meanwhile. When `work.enabled` is on but there is no remote (or no
  `gh`), init prints a decision: fix the cause, or turn `work.enabled` off. Two gestures act on
  it: `preflight.py local-only <sdlc>` and `use-remote <sdlc> <name>`. Nothing is switched off
  silently. `work.py start` now raises the same message in place of git's raw `fatal: 'origin'
  does not appear to be a git repository`. It asks only after the fetch has failed, so the
  measured call count on the success path is unchanged. `/agrim-doctor` runs the same checks as
  rows. Each fix comes from the check that failed, so with `gh` absent the fix is to install
  `gh`, where it used to say `gh auth login`. Every local git call is time-limited by
  `SIGMA_WATCH_CALL_TIMEOUT` (default 120s); the three network calls (`git ls-remote`,
  `gh auth status`, `gh api users/<owner>`) by the smaller of that and 15s, so a dead host reads
  `CANNOT VERIFY (timed out)` quickly. A call that runs over has its whole process tree killed
  (the Windows `taskkill` and the drain after it are bounded too). The SessionStart wizard
  (`cheap_only`) never runs `git ls-remote` or the owner lookup: with an unreachable ssh remote it
  used to stall about 75s in review; the test with a hanging `ls-remote` stub now measures 0.41s.
  Those two rows say "not checked here; run /agrim-doctor" and are never shown as a pass. A fresh
  `git init` with no commit still gets its remote checked and the DECISION printed. `gh auth
  status` reads the active account only (`--active`, with a fallback for older gh), so a stale
  second account no longer fails a valid one. A GitLab or Bitbucket remote gets "gh only supports
  GitHub hosts" and the local-only option, not `gh auth login -h gitlab.com`; a remote URL
  that cannot be parsed is not assumed to be github.com. Credentials in a remote URL
  (`user:token@`) are removed from any text shown. The SSO link names the real host (GitHub
  Enterprise too). `brew install` is suggested only when `brew` is on PATH. The wording now says
  that only opening a PR needs `gh`, and pushing works without it. Nothing prompts. The test suite now also guards `subprocess.Popen` against live `gh`
  calls, and child processes get an empty gh config. An ssh remote whose host is an
  `~/.ssh/config` alias (`git@github-work:o/r.git` with `Host github-work` -> `HostName
  github.com`, the common multi-account setup) is resolved through `ssh -G <host>` (local, no
  connection, at most 5s) and gh is asked about the real host; `ssh.github.com` (ssh over 443)
  reads as github.com. An alias nothing can resolve is `CANNOT VERIFY`, never a FAIL and never
  `gh auth login -h <alias>` (gh cannot log in to an alias), and a `(cannot verify)` row is never a
  SessionStart wizard step. A `ghu_` (GitHub App user) token is `CANNOT VERIFY` like other
  non-classic tokens, not "no scopes". A remote URL with more than two path segments (Bitbucket
  Server `/scm/o/r.git`, a GitLab subgroup, Azure DevOps) is treated as not GitHub and gets the
  DECISION. With `gh` absent and no `brew`/`winget`, the DECISION points at
  https://cli.github.com instead of "the commands above".

- `/agrim-init` now leaves a working verify command, and never leaves `verify.enforce` on with an
  empty command (#228). Before this, the shipped config refused `record done` for every goal,
  including the Quickstart demo. The new `skills/agrim-init/scripts/verify_detect.py` proposes a
  command by reading files only: pytest, `package.json` scripts.test, `go.mod`, `Cargo.toml`, a
  `Makefile` test target (its recipe is shown, as `package.json`'s script is) or a CI test step.
  Each candidate is printed with an id, a hash of its exact command. `verify_detect.py confirm
  <sdlc> <n> <id>` re-detects and records candidate `n`, turning enforce on, only if it still has
  that id. If the repository changed since the report, it refuses and stores nothing. So no
  repository text is ever pasted into a shell, and the stored command is exactly the one shown.
  Detection ignores hidden, cache and vendored directories and non-source files, so running pytest
  once, or the `AGENTS.md` that `--codex` writes, cannot change the candidates. The report is
  printed after every file `/agrim-init` writes. Its gestures name the scaffolded `.sdlc` by
  absolute, quoted path. A `.sdlc` that is a symlink is refused. `set .sdlc
  --command-file <file>` (or `-` for stdin) records your own command. `decline` keeps enforce off
  and records the reason. A CI step containing a shell metacharacter (`` ` $ ; & | < > ``) or a
  control character is never proposed: it is named by file only. Every printed line escapes
  control characters, so an ESC sequence in a repository file cannot repaint the terminal.
  Claude Code asks the user to choose. Codex and Cursor print the numbered candidates and the
  exact config line. Printed gestures and the demo's `verify_command` use the interpreter that is
  on PATH (`python3`, `python` or `py`). A goal's `verify_command: ''` now counts as empty in
  `loop.py`, as it already did in `/agrim-doctor`. A fresh config now ships `verify.enforce: false`, with the reason in `verify._why`. The
  `--demo` goal carries its own `verify_command`, so the Quickstart reaches `done`. The false
  instruction to fill in `.sdlc/project.md` is gone; the command is read only from goal
  `verify_command` or config `verify.command`. `/agrim-doctor` and the setup wizard flag enforce
  on with no command, with a one-line fix. `record done` now names a missing command rather than
  saying "run verify first".

- Feature-branch rebase upkeep no longer deletes branch content when the base holds a revert of the
  branch's own commits (#144). Before it pushes, upkeep now compares the branch tip's tree with the
  replayed tree. If any tracked path would disappear, or would be rolled back to a version the
  branch's own history already moved past (a reverted edit, or an undone rename), it refuses with
  the new `would-drop` outcome and pushes nothing. Base renames and ordinary base edits are allowed,
  and the branch's own deletions never count. The pick line says `was NOT rebased` and names the
  paths, a tracked issue is filed, and `/agrim-doctor` shows the unit as blocked until a clean pass
  clears it (not while `rebase_upkeep` is off or the unit is closed). `feature_rebase.py upkeep`
  exits 1 on it, and `rebase_brief.py rebase` runs the same check before its own force-push. The
  trade-offs: a plain upstream deletion, a move that rewrites past rename similarity, or a base
  reverting its own older change to a file the branch carries is refused the same way; a partial
  revert merged with other changes is not seen, and neither is a full base revert of a file the
  branch kept editing afterwards (the replay yields a version that never existed). The history read
  counts versions created by merge commits and the root commit, ignores chmod-only changes, pins its
  own git config so a user's `log.showRoot`/`log.diffMerges`/colour settings cannot switch it off,
  decodes paths as UTF-8, and fails closed after `SIGMA_REBASE_GUARD_TIMEOUT` seconds (default 120).
  The `agrim-rebase` skill's single push chokepoint, `rebase_brief.push_branch`, runs the same check
  against the commit the lease would overwrite, so `rebase_brief.py rebase`, Slack `--rebase`, the
  conflict walker's final push and its manual-recovery push all refuse a push that would lose
  content, naming the paths; only a path the walk resolved by deletion is that human's decision. See
  `docs/branching-model.md` §3b and §15.
- Upgrade path from the plugin's previous name (#239). A repository adopted under the previous
  name's 1.4.x releases now works under Sigma with no data loss. Sigma reads the old schema ids
  (features, landing, withheld and propagation records), the old feature-doc and Codex
  `AGENTS.md` markers, the old environment-variable prefix (`SIGMA_*` wins when both are set), the
  renamed `drift_watch.channels` key, and the old PR and issue markers, so an old `block` comment
  on an open PR still blocks it. One helper, `skills/agrim-loop/scripts/legacy.py`, does all of
  this reading.
- New: `skills/agrim-doctor/scripts/migrate.py`, a one-shot, idempotent rewrite of that state to
  Sigma's names. It is a dry run by default and writes only with `--apply`. It swaps text in place
  and checks each result by reading it back. It refuses (exit 2) anything it cannot rewrite with
  certainty, leaves history alone, lists environment variables by name only, and refuses to run
  while a watcher is live. See `docs/upgrading.md`.
- Cross-repository propagation no longer overwrites a sibling repository's registry file that
  still carries the old schema id.
- Labels exist before the first pick (#230). `/agrim-init --github` (when `origin` is on GitHub),
  `setup.py labels` and `loop.py start` in github mode now create the ten `sdlc:*` labels and
  `priority:P0`–`P3`. Each run reads the repository's labels once over REST and creates only the
  missing ones, so an existing label is never recoloured and a bootstrapped repo costs one read,
  no writes. Every label is reported as `created`, `existed` or `FAILED: <reason>`; "ensured" is
  printed only when all were measured present, and any failure exits non-zero naming the label
  (`loop.py start` refuses to start). Previously `setup.py labels` printed "ensured" even when
  every create had been refused. When no open issue carries `sdlc:goal`, `loop.py next` still
  prints a bare `DONE` on stdout and now says `0 issues carry sdlc:goal — label one to start` on
  stderr.

### Earlier internal 1.0.0 draft (dated 2026-09-29, kept as history)

The first release of the public core: a gated software development lifecycle for coding agents,
run from GitHub issues, with every phase reviewed before the next one starts.

- Seven gated phases for every goal: goal, research, plan, plan review, implement, review and
  retrospective. Each phase writes an artifact the next one reads, and a review verdict is needed
  before any code is edited.
- `/agrim-goal` runs one goal through all seven phases with an approval gate at each boundary, for
  supervised, end-to-end work.
- `/agrim-loop` drains a backlog autonomously: it claims goals, dispatches fresh phase agents, opens
  verified pull requests, and parks or continues each goal until stopped or out of budget.
- Work is tracked where it already lives: GitHub issues, a project board, and the `sdlc:*` label
  model (goal membership, in-progress, blocked, blocking, parked, needs-confirmation).
- `/agrim-doctor` checks the project setup, the dependencies and the host, and prints the fix
  command for anything not ready. `/agrim-init` scaffolds a project's `.sdlc/` layer and
  `/agrim-setup` adopts the kit into an existing repository.
- A local action log records what each goal did; `/agrim-log` reads it back as live status, and
  `/agrim-status` shows the backlog, the current iteration and the review queue.
- One renderer builds every status line, so a single glance names the goal, the phase and what is
  happening right now.
- Host-agnostic by design: the full pipeline is validated on Claude Code, and a Codex adapter runs
  the same skills and scripts there (a complete Codex goal through pull-request merge is still
  unverified). A Cursor adapter ships as experimental, not yet verified in a live Cursor session.
  Lifecycles live in the kit's own Python and in git, never in one host's hooks.
- Safe by default: nothing sends data off the machine, spawns a background process, or consumes
  quota without the operator opting in.
- Released under the MIT licence.
