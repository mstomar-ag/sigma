# What is new in 1.0.14

This page lists every change in this release in grouped, readable form, each with its issue number. The same
changes are in the `Unreleased` section of the [CHANGELOG](../CHANGELOG.md) in the order a maintainer reads them.
Start with [Upgrading from 1.0.13](#upgrading-from-1013) if you only want to know what changes for you.

Two new guides came with this release: [Upkeep](upkeep.md) and [the Decision Rulebook](decision-rulebook.md).

## Upgrading from 1.0.13

What changes by default after you update the plugin:

- **The blocker priority rule is on.** `discovery.blocker_promotion.mode` now reads `always` when it is absent. In
  GitHub discovery mode a blocker's priority is never below the priority of what it blocks. The first reconcile tick
  after the update prints one stderr line with the number of blockers it found (it can read 0 when the board pass has already raised them), then raises them (raise-only,
  one comment each). Writes per tick are bounded to 10, so a repository with many violations is repaired over several
  ticks. Set `discovery.blocker_promotion.mode` to `off` to keep the old behaviour exactly. See
  [backlog.md](backlog.md) and [label-model.md](label-model.md).
- **An explicit `"smart"` keeps the blocker priority rule off.** A config that already sets
  `discovery.blocker_promotion.mode` to `"smart"` keeps that value: only the board pass runs, and the reconcile repair
  and the block-time raise are skipped. The first reconcile tick prints one stderr line saying so, and `/sigma-doctor`
  shows a not-OK "blocker priority rule inactive" row. To turn the rule on, change that one word to `"always"` (or
  remove the key).
- **The picker orders by tier first.** A goal carrying `sdlc:blocking` now sorts ahead of its tier peers, and no
  longer ahead of a higher tier. This only matters if you had blockers below the tier of what they block; the rule
  above removes that case.
- **Board back-fill stops after a refused project call** (`project.backfill_max_refusals`, default 1) (#1402).
- **Claim, define open and the merge review read** fall back to REST when GraphQL is refused (#1398).

What stays opt-in and unchanged:

- Upkeep stays off. The window model replaces the per-unit polling only for repositories that already enabled
  `upkeep.enabled`; the `triggers.*` and `auto.*` keys are still read for one release and no longer schedule. See
  [Upkeep](upkeep.md). A repository with upkeep off sees no change.
- The Rulebook stays off, every part, including the new fixes below. A closed gate is unchanged in every one of them.
- Your `.sdlc/config.json` is never rewritten by an update. New keys need to be copied from the
  template only if you want to change them.

## Upkeep

- **Upkeep runs on a 12-hour window instead of a per-unit poll (#1387).** Only a completed sweep closes the window.
  A killed, cut-off, busy or failed-to-spawn sweep leaves an attempt stamp that holds off a re-trigger for
  `upkeep.retry_minutes` (default 60, 1 to 10080) only. New `upkeep.window_hours` (default 12, 1 to 8760). When the
  window has passed, the first session start on a machine (a detached sweep, never waited for) or the watcher tick
  starts one bounded sweep of every unit behind its base through the existing per-unit engine. A unit already
  current costs nothing, a second sweep backs off, a refused or parked unit does not stop the rest, and units
  over the total cap are listed as unreached. A closed window is one small file read. The session-start line and
  `/sigma-doctor` show the last sweep and the next window, with a warning after two windows without a sweep.
- **A sweep where every unit failed is an attempt (#1418).** It leaves an attempt stamp and the window stays open;
  a sweep with some units refused or parked still closes it.
- **A future-dated stamp no longer bypasses the retry window (#1419).** A completed-sweep time dated ahead counts as
  none and is not carried into an attempt stamp, so a recent attempt still holds off `retry_minutes`.
- **A unit branch can be declared the integration base (#1389).** `work.integration_base` names a unit branch that is
  also `work.base`, so upkeep accepts it. Undeclared unit bases are still refused.
- **Opening a unit on a fresh clone works (#1397).** When no local branch has the base's name, the unit branch is cut
  from the remote-tracking ref instead of failing with a missing source ref.
- **A unit opened by `define open` is no longer closed by the next registry sync (#1396).** The closed rule now also
  requires that the unit's own `feature/<name>` branch is absent from the remote.
- **`ack --all` acks only what the filed issue listed (#1412).** The listed shas are written only when the
  direct-commits issue is actually filed; when every listed sha is gone, `ack --all` says so and writes nothing.
- **Hard stops no longer refuse the rebase and landing pass on a unit branch whose record exists only on the default
  branch (#1393).** The pass declares its own branch; undeclared force-pushes still stop.

## Rulebook hardening and fixes

All of these are inert while the relevant Rulebook gate is closed.

- **The applier builds its board source once per tick (#1392).** A board that refused the project read is no longer
  retried for each ask in the tick.
- **The applier stamps each applied ask when its own gesture lands (#1390).** In a long tick an owner `no` posted soon
  after the applied comment is no longer outside the veto window.
- **The applier does not act on an issue that is no longer parked (#1395).** It reads the issue's labels first; if the
  parked label is gone, the ask closes as answered with a fixed text and nothing is applied; a failed read leaves
  the ask pending. Reusing the answered state for this closed ask is provisional.
- **Publishing recorded decisions skips a registered unit branch (#1391).** The check now reads the branch of every
  repository in the unit record, and one unreadable record no longer hides the rest.

## Blocker priority rule

An epic in seven slices. The user-facing description is in [backlog.md](backlog.md) and [label-model.md](label-model.md).

- **A blocker's priority is never below what it blocks (#1406).** One shared rule raises a blocker to the most urgent
  thing it blocks, through chains, reports a cycle once and reads an unknown priority as lowest. On by default; an
  explicit `off` is unchanged. A goal awaiting confirmation is never a blocker.
- **A blocker is filed at the blocked goal's priority and never keeps the confirmation label (#1408).** Doctor names
  an open blocker that still carries the label or has no priority.
- **The rank is applied where the blocked and blocking marks are written (#1407).** The auto-unpark sweep, blocker
  resolution (including the precheck park path) and hand-off filing raise the blocker in the same batch.
- **The reconcile tick repairs blockers ranked below what they block (#1410).** Bounded to 10 writes and 200 edges per
  tick; the board field write is skipped when GraphQL budget is low. The count and last repair time are kept in
  `.sdlc/state/blocker-repair.meta.json`, and doctor shows one row.
- **The picker orders by the blocking graph (#1409).** Tier first, then a blocking goal ahead of its tier peers, on the
  label and board paths. With `blocking_priority_override` off the order is unchanged.
- **Follow-ups (#1415).** A blocking cycle raises every member that transitively blocks something more urgent,
  whichever member the walk enters through; filing a blocker and recording a block strip the legacy confirmation label
  from the open blocker.
- **Documented and proven end to end (#1411).** Scenario tests drive every worked case through the real writers,
  repair, filing, picker and reconcile tick.

## Wiring, GraphQL and REST fixes

- **Claim, define open and the merge review read fall back to REST when GraphQL is refused (#1398).** A label swap
  whose GraphQL id lookup fails writes through the REST label helpers; `define open` lists labels through REST when
  `gh label list` fails and names a rate limit instead of the login when that is the cause; under
  `require_review: "approval"` an unreadable review decision no longer parks a PR that carries a `sigma:approve`
  comment (provisional).
- **A refused owner lookup names the GraphQL rate limit (#1400).** A bare `unknown owner type` from a `gh project`
  call now says it is usually the GraphQL rate limit and keeps the phrase the doctor matches.
- **Back-fill stops after a refused project call (#1402).** `project.backfill_max_refusals` (default 1).
- **Ten small wiring fixes (#1403).** A park no longer prints a failed-label line when the REST fallback landed it;
  failed applier asks are named; the comment scan honours its time budget; landing refusals keep the reason;
  `ack --all` acks only the listed commits; `work.base` accepts a remote-prefixed name; the board dry run shows the
  apply order; a rebased outcome drops conflict text; the reconcile tick limit scales with the watcher interval.
- **Release on a never-claimed goal is a no-op (#1394).** It returns False and writes nothing when the state read
  succeeded and the issue does not carry the in-progress label.
- **A goal whose open blocker the board mirror cannot see is held at pick (#1420).** Read live once per pick: open
  holds, closed releases, unreadable fails open. The precheck reports such a ref as `blocker_unresolved:<n>`.
- **The precheck no longer reports a closed blocker as unresolved (#1422).** A closed one is dropped from
  `blocker_unresolved`; an open or unreadable one is still reported.
- **A project board no longer turns a gh login blip into a login instruction (#1399).** The session-start check
  retries once and shows the short could-not-confirm step.
- **Doctor reads repository visibility through a real owner/name (#1401).** It uses `discovery.github.repo`, else
  the slug parsed from the origin remote.

## Doctor rows added or changed

- `upkeep window`: window hours, last sweep, next window, unreached units; fails after two windows without a
  completed sweep (#1387).
- Blocker hygiene: an open blocker that still carries the legacy confirmation label or has no priority (#1408).
- `blockers ranked below what they block`: count and last repair, flagged when violations persist for two reconcile
  intervals (#1410). Absent when the rule is `off`.
- Owner-lookup rate-limit wording (#1400) and the public-repository check (#1401).

## Wording fixes

- **After the REST fallbacks (#1413).** The release no-op message says the goal may never have been claimed, and the
  review gate docstring and comment describe the unreadable-decision case with an approve directive.
- **The pick-time dependency hold docstring (#1421)** describes the live blocker read: a ref the mirror lacks is read
  live first, and only an unreadable one is claimed.
