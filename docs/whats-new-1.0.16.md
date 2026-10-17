# What is new in 1.0.16

This page lists every change in this release in grouped, readable form, each with its issue number. The same
changes are in the `1.0.16` section of the [CHANGELOG](../CHANGELOG.md) in the order a maintainer reads them.
Start with [Upgrading from 1.0.15](#upgrading-from-1015) if you only want to know what changes for you.

The main theme is the resource guard, described in [parallelism](parallelism.md): limits derived from the machine,
admission for verify runs and model sessions, and a hold the picker can return. The second theme is
[label drains](label-drain.md): confining a run to one label. The [Decision Rulebook](decision-rulebook.md),
filing triage and [Upkeep](upkeep.md) also changed.

## Upgrading from 1.0.15

Steps 1 to 3 apply to everyone. Steps 4 to 7 apply only if you use the feature named. Script paths are under
`skills/sigma-loop/scripts/`.

1. **The resource guard is now on by default.** The mode is read by `resource_guard.resolve_mode`: the
   `SIGMA_RESOURCE_MODE` environment variable if it is `enforce`, `observe` or `off`, then `resource.mode` in
   `.sdlc/config.json`, then `enforce` (#1759). The config template ships `resource.mode` as `null`, so an untouched
   install resolves to `enforce`. Model-session spawns, `loop.py verify` and the watcher sample step used to default
   to off and now follow `enforce`. This means a verify run or a model session can be held (a verify exits 5 with
   `RESOURCE-HOLD`, and the picker can print `HOLD`) when the machine has no room. All limits are uncalibrated (see
   [Resource guard](#resource-guard)).
2. **To turn the guard off or only watch it,** set `resource.mode` to `off` or `observe` in `.sdlc/config.json`, or
   export `SIGMA_RESOURCE_MODE=off` or `SIGMA_RESOURCE_MODE=observe`. The environment variable wins over the config.
   With `observe` a would-be hold is logged and the work is admitted. With `off` nothing is leased, sampled or
   written.
3. **Restart sessions, and stop a running watcher** so it picks up the new code: `touch .sdlc/state/watch.stop`,
   wait for it to exit, remove the file, then start the watcher again. A leftover `watch.stop` makes the next
   watcher exit at once.
4. **If you use the upkeep agent level,** its spend keys are now optional. `conflicts.agent.max_budget_usd_per_unit`
   and `spend_ceiling_usd_per_day` set to `null` mean no limit, and the level can launch (#1554, #1694). Set them if
   you want a cap. A launch also needs `upkeep.conflicts.agent.credential_var` (default `null`) to name an
   environment variable that holds the model login; with it unset every agent-level launch parks (#1691, #1713).
   The live run with a real model has not been done.
5. **If you file AI issues,** a filing that reaches `ai_filed.max_per_goal` now parks the goal's lowest-priority
   filing, and a requested priority of P0 or P1 bypasses the per-goal cap (`ai_filed.cap_bypass_priority`, default
   P1) (#1697, #1553). Filings that mention euro, pound or dollar amounts near a compute noun, and a quoted deny
   word after more lead-ins, now park instead of arming (#1710, #1734, #1756, #1755, #1726, #1704).
6. **If the applier is on,** a quota park that names a reset time is now held until then, instead of a fixed 24
   hours (#1563), and the tick prints fewer lines (#1564). Run `python3 unpark.py report <dir>` and
   `python3 applier.py <dir> --dry-run` before you rely on it.
7. **If you use the supervisor,** its session lease now lasts 24 hours (#1737).

What stays unchanged:

- Your `.sdlc/config.json` is never rewritten by an update.
- `rulebook.rules.auto_approve` is off by default (#1555), and the label-drain, `design.zones` and `quality.metrics`
  keys are absent by default (#1540, #1635).
- With the Rulebook gates closed, run-label output is unchanged (#1581, #1743).

## Resource guard

The guard is documented in [parallelism](parallelism.md) with its controls table and two measured calibration
records: one on a real macOS host and one in a 2 GiB memory-limited Linux container (#1772). The Linux run found two
gaps, fixed in this release: a container's total memory is now its cgroup limit (before, the host's memory made every
verify wait forever), and the footprint meter reads `/proc` when `ps` is not installed. The five default ratios are
still uncalibrated: two records are evidence, not a basis for changing them. Treat every threshold as a starting value.

- **Samples and derived limits (#1597, #1598).** `resource_guard.py sample [--json]` reads memory, swap, pressure and
  CPU with stdlib-only readers (ctypes on macOS; `/proc` and cgroup v2 on Linux), plus a per-process `footprint(pid)`.
  Any other platform raises `Unsupported` and the command exits 3. `derive` and `decide` turn a reading into a memory
  reserve, share cap and CPU reserve as ratios of the machine. They hold on the lowest of three samples and resume on
  the higher of the lowest two, and `hold.json` is written by atomic rename. The `resource` config block is all
  `null` (derived) and is documented in [configuration](configuration.md).
- **One mode resolver for every surface (#1759).** `resolve_mode` serves the picker, verify, sessions, watcher,
  doctor and log. Order: environment, `resource.mode`, then `enforce`.
- **Verify runs under admission (#1601, #1768).** With the mode `enforce` or `observe`, `loop.py verify` runs the
  proving command in its own process group with a lifeline and the `verify.timeout_minutes` budget, keeps a 64 KiB
  output tail, passes the granted worker count in a `{workers}` token (integer only), `SIGMA_VERIFY_WORKERS` and
  `PYTEST_XDIST_AUTO_NUM_WORKERS`, and records the tree's peak footprint per worker as the learned cost. A hold exits
  5 with `RESOURCE-HOLD` on stderr, writes no evidence and runs nothing. A command list shares the single `timeout`
  budget, and a verify held because a literal `-n N` cannot fit is written to the waiting table so new goal starts do
  not take its memory. The xdist variable was checked against pytest-xdist 3.8 only.
- **Shed under critical pressure (#1602, #1769).** With `resource.shed` at its default `verify`, a number of critical
  samples in a row stop the verify process group Sigma started (TERM, a grace, then KILL). The result is
  `RESOURCE-HOLD (shed)`, exit 5, with no evidence, and the goal is not failed. No session is signalled and nothing
  is paused. `resource.shed_after` sets the number (default 3; a bad value reads as 3 with a note).
  `tools/resource_controls.py` applies each listed guard break to a temporary copy in CI and requires its test to
  fail (five breaks, run once locally; the CI run is unmeasured). It matches a FAILED line on the full test node id,
  so a test whose name is a prefix of another's cannot pass for it (#1787).
- **The lease table (#1599, #1715, #1722, #1733).** `resource_lease.py admit|release|status|wait` keeps a lease table
  under an `fcntl.flock` lock. Each admit reclaims stale leases (dead pid or past ttl), counts the unmaterialised
  remainder of every lease, and grants the largest fitting count or holds with a retry time (exit 10). Costs are the
  p90 of the last 32 measured footprints per kind, with outliers above 4x the median excluded and logged. A verify
  gets at least one worker when one fits. State is in the XDG state folder beside `hold.json`. It is POSIX only.
  - A lease whose pid was reused (start time differs from the one recorded) is reclaimed as stale. `wait --kind goal`
    holds while a started goal's verify is waiting, like `admit`. Releasing a lease file with no id no longer raises.
    `wait --max` rejects nan and negative values with exit 2 (#1715).
  - Reclaiming a reused-pid lease no longer measures the foreign process into the cost ring. Linux process start is
    in seconds, so the 2 s tolerance means seconds on both platforms. `wait` applies the same pressure and
    `max_goals` gates as `admit`. A lease record without a goal no longer raises (#1722).
  - The CLI reads the project's `resource` block for mode, ttl and limits instead of its built-in defaults (#1733).
- **Model sessions take a `session` lease (#1603, #1738, #1758, #1737, #1740).** The judge, the supervisor (Claude
  and Codex) and the upkeep launcher share `resource_guard.session_lease`, which releases on exit, exception or
  timeout. A hold becomes each site's own refusal, pause-and-relaunch or deferral. An advisory reviewer spawn (cursor,
  codex, `review.command`) takes the lease first; a hold records the existing failed run (`failure: held`), starts
  nothing and prints no verdict, so a hold cannot read as an approval (#1738). The autowatch drive of `claude -p`
  asks for admission too; a hold records a `deferred` note, starts nothing and does not count a hop (#1758). The
  spawn ratchet also matches string commands such as `claude -p`. A ratchet test fails on a new host-CLI spawn that
  is not gated or listed. A daemon thread renews a held lease (default every ttl/3) so a long session is not
  reclaimed as expired (#1740). The supervisor passes a 24-hour ttl in both call sites, and a dead pid is still
  reclaimed at once (#1737). With the mode `off` none of this writes anything.
- **`HOLD` from the picker (#1600).** Before any claim, `loop.py next-batch` and `next` take one resource sample.
  Slots become the lesser of `parallel.goals.max_concurrent` and how many goal-cost units the headroom admits. With
  none, the picker prints `HOLD` on stdout and `HOLD (reason; retry ~Ns; held since ...)` on stderr, exit 0,
  claiming nothing and spending no budget. `observe` logs the would-be hold and admits. An unsupported platform or a
  guard error under `enforce` admits one goal with a one-line notice. The supervisor reads `HOLD` as a clamped sleep
  (30 to 900 seconds) that does not consume attempts. The programmatic `run_loop` driver is not gated.
- **Liveness in log and doctor (#1604, #1786).** `log.py slots` appends `admission HELD 12m: memory` while `hold.json`
  is held. The watcher tick makes one more bounded call, `resource_guard.py sample --record`, only when the mode is
  `enforce` or `observe`. It refreshes `last.json` and `hold.json` and reclaims stale leases. With `ledger.enabled`, a
  hold start and end each write one note, at most one per `hold_alert_minutes` (default 10, uncalibrated). The
  doctor shows the resource rows whenever the resolved mode is not `off` (an empty config resolves to `enforce`).
- **Small guard fixes (#1714).** A hold keeps its original start on repeat ticks, an empty history reads as open,
  `hold.json` is written through a per-process temporary name removed on failure, and a `cpu_reserve_cores` at or
  above the core count reads as derived plus a note.
- **The verify shed follows the same mode resolver (#1794).** With `SIGMA_RESOURCE_MODE=observe` a critical machine
  no longer stops a verify; the shed reads the mode the same way as every other surface.

## Label drains

A run label confines a run to the goals that carry it. See [label drain](label-drain.md).

- **`--label NAME` on `loop.py next` and `next-batch` (#1575).** The run is confined to goals carrying both the goal
  label and that label, across units, and each goal is still based on its own unit branch. A missing, reserved,
  unknown or `--feature`-combined label is refused with exit 2 before any backlog read.
- **`label_drain.py plan|status <sdlc_dir> <label>` (#1576, #1578).** Read-only. It lists the open members with a
  verdict each (armed, unarmed, parked with its kind and question, upper-tier, assigned-elsewhere, held-by-gate). It
  pulls their open blockers transitively (depth 8, at most 50 reads; a pull request, another repository or an
  unreadable ref counts as unresolved), warns on cross-unit edges, cycles, a missing `## Done when` and a declared
  paid step, prints the number of calls made, and refuses more than 200 members. `plan` also returns `questions` and
  `ready`: each parked member's own question (an undeclared park first gets "which kind"), members no gate can
  classify, pulled blockers a person must decide on, the arming route once per label (closed-list kind `label_set`),
  and a spend question only for a paid step the hard-stop net recognises. It stays read-only.
- **`label_drain.py arm <sdlc_dir> <label> [--apply]` (#1577).** Dry run by default. With `--apply` each unarmed issue
  gets the goal label (parked and blocked labels dropped in one swap), a priority label only if it has none
  (`label_drain.default_priority`, default P2) and the configured assignee only if it has none. A pulled blocker also
  gets the run label and one comment (`label_drain.arm_pulled_blockers`, default true). Upper-tier, parked, gate-held
  and other-assignee issues are refused with a reason. No body is edited and no label is created or removed.
- **`label_drain.py answer <sdlc_dir> <label> --answers <file.json> [--dry-run]` (#1580).** The file maps question
  ids from `plan` to options; an unknown id or an option outside the bank refuses the whole file (exit 2, no write).
  Per answer the effect comes first (`arm`, `unpark.py resolve`, or the single-use `sigma:spend-approved=<token>`
  marker only on approver-authored paid members for `go ahead`), then one decision record with scope `label:<name>`.
  A failed effect writes no record. Questions left out stay open and a repeated answer records once.
- **A drain pulls blockers, refuses to start on open questions and stops as `label-held` (#1579).** Under `--label`
  only: a held-on ref outside the label is armed and pulled in (once per ref, cap 50), `next` prints the
  `label_drain.py` verb to run when members are unarmed or questions are open, and a run with open but unpickable
  members stops `label-held` instead of `backlog-empty`. Unscoped and `--feature` runs are unchanged.
- **Recorded decisions are honoured (#1732, #1725, #1751).** `plan` drops questions that already have a recorded
  answer and recomputes `ready`. `next|next-batch --label` counts the plan's real open questions and no longer
  refuses on unarmed members once a `leave them` decision is recorded. A recovered run-label answer whose arming
  landed before the record now records a goal: the lowest open member carries it (#1725). With no open member left
  it records with no issue, prints one line and attempts no comment (#1751).
- **Run labels learn by kind (#1581, #1743).** `plan` and `answer` record the arming question under
  `label-kind:<kind>` (kind from the label prefix or `label_drain.kinds`), show an approved rule's option first as
  recommended, and add a history line per question when records are on. The answer record carries `rule_id`,
  `shown` and `accepted`, and public repositories hash the label name. The recommendation also needs the records
  gate: with records closed and rules open the plan is byte-identical to the old output, and the history line is only
  for the label-set question (#1743).
- **Documented and measured (#1582, #1761).** The page covers the run label from arming to the stop, and `--label`
  appears in the loop and goal skills with one pinned guarantee sentence. The GitHub call counts first came from a
  counting fake transport (#1582). The page now also holds call counts and wall times from one real run on a
  throwaway private repository (plan, arm, answer, one pick with and without `--label`), names what was not
  measured, and a docs test keeps the table consistent (#1761).

## Rulebook and applier

- **The applier can say it is held, and acts less noisily (#1699, #1564, #1362).** When evidence behind the default
  branch holds it, a real tick and `--dry-run` print `applier: held - <reason>`, and the doctor row adds
  `HELD: <reason>` until a later tick runs (#1699). The tick prints `applier: N applied` and `applier: N vetoed` on
  separate lines, each only when N is above zero; ask bookkeeping prints only with `--verbose` (#1564). At level 4,
  an apply whose ask gesture does not match the rule option's gesture now comments and is not counted by the spot
  check (#1362).
- **`applier.py --dry-run` with the applier off (#1545, #1695).** It prints one line saying the applier is off (#1545)
  and then the same `would ...` lines the enabled applier would, treating that one gate as open and writing nothing
  (#1695). Output with the applier on is unchanged.
- **Fewer reads of asks that cannot move (#1360, #1702).** A pending ask older than twice its window is read for an
  owner `no` once per window, recording `veto_read_at` (#1360; provisional: a `no` on a stuck ask can be honoured up
  to one window late). An ask with no matching rule, a level below 3, a hard-stop kind or the daily cap reached is
  throttled from its first tick (#1702).
- **A quota park keeps its reset time (#1563, #1641, #1547).** A quota park that names a reset time or retry-after
  records `reset_at` on the park and the ask, and the ask is held until then; with none named it waits `window_minutes`
  (#1563). A reset time read back from a park comment is bounded like the parser's: more than 7 days past the park
  reads as unknown (#1641). A `keep-parked` on a quota park answered "wait it out" no longer posts the sweep opt-out
  marker, but the sweep does not resume a quota park (#1547, provisional).
- **Resuming a held park (#1549, #1630, #1639).** A dependency park kept parked by a "wait for it" decision posts a
  waiting note, and the sweep resumes only a park carrying it, when its last blocker closes (#1549). A park
  re-applied by label only after a waiting note is not resumed; the signal is the newest `sdlc:parked` label event
  (#1630, provisional). That read now follows event pages (at most 10 of 100) instead of giving up on a full first
  page (#1639).
- **Rules (#1546, #1555, #1696).** `rule_draft.py rules <dir>` lists every approved rule (id, kind, area, option,
  action, last confirmed), and the doctor's rulebook row counts approved rules per action (#1546).
  `rulebook.rules.auto_approve` (default false) lets the watcher tick, `rule_draft.py list` and `suggest` draft and
  approve a rule for every learned pair with a majority option, as the first configured owner. The applier applies it
  after the window, an owner `no` holds it until reconfirmed, hard-stop kinds are never touched, and the doctor warns
  while it is on (#1555). Retiring an auto-approved rule records its pair in `retired_pairs` in the rulebook file, so
  `rule_draft.auto_approve` does not redraft it; a hand `draft` or `approve` clears the record (#1696).
- **Reports and bulk declare (#1561, #1562).** `unpark.py report <sdlc_dir>` is read-only. It shows every parked or
  blocked issue with its declared or suggested kind, area, matching rule and gesture, the pair's level, and whether
  the applier would act now, later or never (and why), then one total per outcome; `--json` for tools (#1561).
  `unpark.py declare <dir> --from-suggestions [--kind K] [--dry-run]` declares every undeclared park whose suggested
  kind is confident. `irreversible` and `unknown` are never bulk-declared, and the suggester counts `delete`,
  `drop table`, `force push` and `destructive` only in action position (#1562).
- **Secrets in records and requests (#1558, #1631, #1698).** Decision records scrub secret shapes from their free
  text before the file is written, and the import summary prints how many values were scrubbed (#1558). A secret
  straddling the 300-character cut of a record's choice or reason is scrubbed before the cut (#1631). The import
  classifier scrubs the question and option labels before the 300-character clip, so a key in a past question never
  leaves the machine (#1698).

## Filing triage

- **Currency in spend phrases (#1710, #1734, #1756).** Euro and pound amounts (symbol, code or word) near a compute
  noun park as spend, like dollar amounts (#1710). A currency sign after the number ("40€", "30£", also CHF, ¥, CAD,
  INR, yen, quid) parks like the sign-first forms (#1734). `40$`, `40 $`, `40 AUD`, `500 JPY` and similar
  three-letter codes next to a compute word park too (#1756). Non-money units such as `40 MB`, amounts inside a
  fenced block and amounts with no compute noun still arm. "force-push" and "force_push" now match the "force push"
  hard-stop word, and the parked text still quotes the original clause.
- **Quoted deny words (#1704, #1726, #1755).** A command in quotes after an action verb, a hand-off word or a
  purpose clause (redact X to nuke Y) parks again (#1704). Lead-ins such as let's, I'll, afterwards and ok park like
  the unquoted form (#1726). Later, maybe, yes, go ahead, instead, let us, I'd and we'd before a quoted dangerous
  command park (#1755). A naming verb, a noun head such as command or word, a negation, a determiner or a copula
  still reads as description.
- **Questions park instead of arming (#1703).** A filing or migration leftover that asks a person to decide parks.
  `ai_filed.triage.question_patterns` (title regexes, `[]` disables) and `handoff.py track --needs-person <kind>`
  declare a question, and `promote.py migrate-confirmation` parks them too.
- **The per-goal cap (#1697, #1553).** Below `ai_filed.cap_bypass_priority`, a filing that hits `ai_filed.max_per_goal`
  parks the goal's lowest-priority filing (newest among equals), which can be an armed one that then gets the
  parked-at-filing comment. Equal priorities still park the last filed. `handoff.py open --priority P1` bypasses the
  per-goal cap like `track`; `ai_filed.max_per_run` still parks it. Counter entries gain `issue` and `prio` (#1697).
  A requested priority of P0 or P1 is armed (at its bucket priority) past a reached per-goal cap while the counter is
  readable (key `ai_filed.cap_bypass_priority`, default P1, invalid reads as P1). Lower priorities and the per-run
  cap still park, with a reason naming `ai_filed.max_per_goal` or `max_per_run` (#1553).
- **The doctor reports an unreadable AI-filed counter without an `ai_filed` block (#1731).** A missing or non-object
  `ai_filed` counts as empty for the "AI-filed counter is readable" and "AI-filed triage config is valid" rows.
  JSON-false triage stays silent.
- **A nationality before "dollars" is spend (#1795).** `40 US dollars`, `40 Canadian dollars` and `forty US dollars`
  next to a compute word now park like the other currency forms.

## Upkeep

- **The agent level can launch (#1554, #1573, #1692, #1716).**
  - Slice 1: the probed resolver flags are confirmed (only `--bare` stays refused), the model catalog holds real
    ids, and the two spend keys are optional (null means no limit; a set cap or ceiling is enforced as before). The
    doctor names the mode. Tested against a stand-in command only; the live run is owner-gated and not done (#1554).
  - The level resolves in one scratch session per unit: a history-less, remote-less copy re-entered at each
    conflicted commit, with base, ours, theirs, commit messages and earlier stops as context, edit, build and test
    allowed, a network wall where the platform has one (the doctor says which), a whole-session wall clock, and the
    same write-back and proof gates. The plain export remains for plans without `session` (#1573).
  - In session mode the protected-path, structural, merge-stop and count refusals no longer apply (the proofs and
    `timeout_minutes` are the gate). Add/add and delete/modify stops go to the session and are applied as `git add`
    or `git rm`. A refused proof is fed back to the same session before parking and the park names why the last try
    failed. The attempt record is keyed by unit tip and base tip, so a moved base starts fresh and a run cut off by
    the sweep cap is not counted. Legacy mode is unchanged (#1692).
  - When the integration side deleted a file the unit changed and the session keeps the delete, every unit-added line
    must be elsewhere in the resolved tree or listed in `dropped-by-delete.txt` and approved by the independent
    review; the list goes in the stamped commit message and the upkeep record. When the unit deleted a file the
    integration side changed, every integration-added line must be elsewhere, and no list can save it (#1716).
  - The dropped-by-delete list marks where it was cut (`...` on a clipped line, `... (N more lines)` on the record),
    trimmed lines under 4 characters never count as moved elsewhere, a file under a symlinked parent directory is
    refused, and a path deleted at one stop that a later stop touches parks as `deleted-path-touched-again` (#1741).
- **Authentication and confinement (#1691, #1693, #1713).**
  - `upkeep.conflicts.agent.credential_var` (default null) names one environment variable holding the model login. It
    is the only credential forwarded (never `GH_TOKEN`, `GITHUB_TOKEN`, `SSH_AUTH_SOCK` or `GIT_*`). A launch runs a
    bounded `claude auth status` in the scrubbed environment first and parks with "resolver cli is not logged in in
    the scrubbed environment" when logged out. The doctor gains a `resolver auth` row. Not run against a real CLI
    (#1691).
  - Where `sandbox-exec` exists (macOS) the launch is wrapped in a profile that denies reads of credential
    directories and writes outside the session, and limits network to port 443 (not filtered by host). Where no wall
    exists the session runs without the Bash tool. The doctor row `resolver session confinement` says which.
    Provisional: no live run yet; this corrects the #1573 entry's claim of a wall (#1693).
  - The auth probe runs under the same wall as the session where one exists, plain elsewhere. The docs and the doctor
    fix text state that with `credential_var` unset every agent-level launch parks until it is set. The no-login
    end-to-end test checks the logged-out state and the untouched branch tip. The wall stays unverified against a
    live session (#1713).
- **The sweep (#1557, #1632, #1700).** The sweep prunes backup refs past `backup.keep_days` by itself, keeping the
  newest `backup.keep_last` per unit, once per sweep and bounded. A failed prune never fails the sweep and is retried,
  and the doctor shows the backup count and last prune (#1557). The prune is skipped when the sweep cap is already
  spent, the sweep beat is touched before every git call during it, and it gets the sweep's remaining cap as a
  deadline, recorded as outcome timeout and retried by the next sweep (#1632, #1700).
- **`feature_rebase.py preview` (#1559).** A sweep-level dry run. It lists every unit the sweep would consider
  (`upkeep.units.include` and `exclude` applied) with a predicted outcome (current, fast-forward, clean rebase,
  conflict with its files, or refused with the reason), from the remote-tracking refs, with `--fetch` as the only
  network step. It moves no ref, pushes nothing, makes no worktree and works with the upkeep gate closed. The conflict
  answer is a merge probe, so a rebase may still differ.
- **The land verb measures the sibling half of a pair (#1552).** `feature_land` used to pass no landed measurement, so
  a unit with a second repository was always refused. It now reads the sibling's pull requests and proceeds only when
  one merged. An unmerged or unreadable answer still refuses, and the refusal names the goal recorded for that
  repository and the remedy.

## Loop, work and reviewer

- **Blocker repair (#1706, #1707, #1560).** Repair no longer re-reads the same ten newest outside blockers every tick:
  a blocker read and found at its rank (or raised) is skipped for ten minutes, so older ones are reached, and unread
  blockers are counted in the repair state and the doctor row (#1706). The preview applies the same ownership test as
  the repair: with hard stops on, a blocker Sigma does not own is listed as skipped, and the doctor row names the
  refusal instead of blaming label permissions (#1707). `reconcile.py blockers <sdlc_dir>` is a read-only,
  mode-independent preview. It lists each blocker the repair would raise (current and new priority, the goal it
  blocks), the blocking cycles and the outside or unreadable blockers, and writes nothing;
  `GitHubSource.repair_blocker_ranks` gains `preview=False` for it (#1560).

## CI tiers and changes that came from main

- **Change zones (#1540).** `design.zones` in `.sdlc/config.json` (absent by default, owner-declared, last matching
  glob wins) and the read-only `skills/sigma-loop/scripts/zones.py classify <path>...`, which prints each file's zone,
  whether it is new at the branch's fork point, and the one rule that applies (`docs/change-zones.md`).
  `/sigma-vision` may suggest candidate legacy paths but never writes the key.
- **`quality_delta.py <goal>` (#1635).** It measures each configured quality metric at the merge-base and at the head.
  `quality.metrics` is opt-in (absent by default) and read from the config as committed at the merge-base. Each
  command runs once per side in a scratch worktree, one at a time, as an argv with no shell, in its own process group
  (killed whole on timeout) and with credential-shaped variables removed, behind a memory probe derived from the host.
  It prints base, head, delta and `better`, `same`, `worse` or `unmeasured`. A failed or timed-out command or
  uncommitted work is `unmeasured`, never `same`. See [quality delta](quality-delta.md).
- **`outcomes.py report` (#1636).** A read-only `sigma-log` verb that reads the action log and one `git log` pass. For
  each mature goal it prints the reverts and `fix`-subject commits touching its files within 14 days. Its first line
  says it is a proxy, it prints no rate, and it prints `unmeasured` with the reason below 10 mature goals, with the log
  off, in a shallow clone or for a window of 14 days or less. The window is capped at the action-log retention.
  Shared files (a fixed list plus any file in over a fifth of the commits) attribute nothing. It writes no file and
  uses no network.

## Doctor rows added or changed

- Seven `resource` rows: platform, mode, admission (with the "held with 0 leases: other software" wording), sample age
  against the watcher bound, stale leases, `max_concurrent` against the derived cap, and a `-n` pin against the derived
  workers (#1604). They show whenever the resolved mode is not `off` (#1786).
- Applier row adds `HELD: <reason>` (#1699).
- Rulebook row counts approved rules per action and names the verb (#1546), and warns while `auto_approve` is on
  (#1555). The doctor also lists answers, level and rule coverage per label kind (#1581).
- Backup count and last prune (#1557).
- `resolver auth` (#1691) and `resolver session confinement` (#1693). The doctor fix text says an unset
  `credential_var` parks every launch (#1713). `resolver flags` no longer fails while the only unverified flag is
  `--bare` (#1694). The doctor names the agent-level mode (#1554).
- The blocker priority row counts unread blockers (#1706) and names the ownership refusal (#1707).
- "AI-filed counter is readable" and "AI-filed triage config is valid" read a missing `ai_filed` block as empty
  (#1731).

## Wording and docs fixes

- **The Decision Rulebook migration section (#1709).** It no longer says rebase upkeep findings still get the legacy
  confirmation label while triage is on. It names the triage-off exception, and a docs-claim test pins it.
- **The upkeep confinement docs (#1712).** They list what the macOS wall does not cover: the profile allows everything
  by default, so the keychain and mach services stay reachable, reads outside the listed credential directories stay
  open, and writes to the temporary areas stay open. The dead `SESSION_ARGV` constant is removed; the doctor flag check
  reads `session_argv(True)`.
- **The agent conflict level with no spend key set (#1694).** The upkeep docs say it runs with no spend limit; set
  `max_budget_usd_per_unit` (and optionally `spend_ceiling_usd_per_day`) to cap it.
- **The resource guard docs (#1605, #1772).** `docs/parallelism.md` has the controls table and the macOS and Linux
  calibration records, both measured; the five defaults stay uncalibrated.
- **The label spend cap and the Rulebook doc (#1744).** The spend cap is asserted at exactly level 2, the spend answer
  path never raises the level, and the doc says earlier `repo`-area arming answers no longer count toward a label-kind pair.
