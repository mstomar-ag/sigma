# Changelog

All notable changes to Sigma are recorded here, newest first.

## Unreleased
- **`chain.py plan <sdlc_dir> --longest [--by freeing] [--json]` offers the top three end goals to work on first (#1591).** From the stored dependency map only: an end goal is an unresolved issue nothing unresolved waits on; the order is priority tier, deeper chain, more members, oldest `updated_at` of any member (the map stores no creation time; provisional, a node without one sorts last), then number, and `--by freeing` puts the member count first. An end in or behind a dependency cycle is listed as excluded with the cycle. It writes no file and calls no GitHub; mode `off` or an unusable map is refused with exit 2. `chain.auto_longest` is documented (default off, nothing reads it yet, the watcher does not honour it). `chain.py plan <sdlc_dir> <N>` is unchanged.
- **The write-surface scan sees `state.atomic_write_text`, so `chain.py::answer` is listed (#1842).** The scanner only matched `os.makedirs`-style calls, which is why `chain.py plan` was listed and `answer` was not; `atomic_write_text` is now an fs-write rule, the inventory gains `answer` (its gate names the GitHub reach through the unpark resolve path) and nine other local-record writers the scan had missed, and the `plan` gate no longer says the scanner cannot see `answer`. Local scan only; no behaviour change.
- **The dependency-map insert no longer reads an unknown node as "no cycle", and the unchecked list is locked (#1844).** `closing_members` over a usable stored map now answers undecided (`None`, so the live walk or the unchecked record follows) when the blocker or the goal is missing from it, never `[]`. `record_unchecked` and `take_unchecked` run under their own lock file `state/dag-unchecked.lock` (not the refresh lock), so concurrent records keep both pairs and a pair recorded during a take is not lost. Without `fcntl` the refresh lock yields `open` and `insert` has no mutual exclusion: stated in the module docstring, and two interleaved inserts leave a valid map (the later save may drop the earlier node until the next refresh).
- **The dependency map keeps a local history, `dag_history.py` (#1622).** Every saved map appends what changed since the one it replaced (edge added or removed, node resolved or deleted, chain completed, chain superseded by a merge or split) to `.sdlc/state/dag-history/YYYY-MM.jsonl`: numbers, event names and timestamps only, append-only, never pruned or deleted by Sigma, nothing when mode is `off`, and a history failure never fails the save. `dag.py history --month YYYY-MM` prints one month and opens no other file; the doctor adds a `dag history` row and warns above 1 MB in one month. Sized at about 100 bytes a record (50 changes a day for a year is about 1.8 MB); the 30,000-node diff time is measured by the test.
- **`chain.py plan` rejects a non-ASCII digit goal argument with the usage line, exit 2 (#1838).** It used to pass the `isdigit()` guard and raise a traceback from `int()`.
- **A filed goal enters the stored dependency map at once, and a `Blocked by` that would close a cycle is refused, `dag_insert.py` (#1616).** `GitHubSource.create_dependency` inserts the new issue and the edges of its body; `append_to_body` of a `Blocked by #N` marker first asks whether the edge closes a cycle (stored map, else a live walk of at most 25 reads), raises `CycleRefused` naming every member and writes nothing; over the cap the write is allowed and the pair is kept in `state/dag-unchecked.json` (numbers only, at most 200) until the next saved refresh clears it. A busy lock, a missing map or an insertion error is a quiet skip or one stderr line and never fails the filing; mode `off` loads and touches nothing. Not measured against a live repository; the read counts are arithmetic.
- **Dependency cycles are reported and held, `dag_cycles.py` (#1615).** Two goals that block each other were held but the pick summary said they "become pickable by themselves"; with a stored map holding the cycle it now says the cycle never releases by itself and prints one `dependency cycle: #A, #B` line per distinct cycle, `/sigma-status` adds a segment per cycle and the doctor a `dag cycles` row. After a refresh that saved the map, one comment per distinct cycle is posted on its lowest-numbered open member (numbers and a fixed instruction only, a marker stops a repeat, at most 20 cycles per refresh); `discovery.dag.cycle_comment: false` and mode `off` post nothing. Members are never claimed and no label is written.
- **Chain cost estimate and spend cap, library `chain_spend.py` (#1586).** `estimate` gives p50 and p90 cost per goal (cents) from the `spend` events of earlier non-member goals, or `no estimate: only <n> measured goals` and no number under 3. `spent` sums member goals' `spend` events since the approval stamp, by goal number. `admit` checks `chain.max_usd`, `chain.max_goals` (default members plus `chain.max_added`, 5) and `chain.max_minutes` (unset); fail closed on an unreadable ledger with a cap set, an unpriced phase counts at the per-phase p90, and no p90 with a cap set refuses. The cap is checked before each claim and can be overshot by the cost of the goals in flight. Library only; nothing calls it yet. Measured: one `spent` scan took 1.5 ms on 1,000 records and 13.9 ms on 10,000 (local, one run). A spend or phase event of a member goal whose stamp cannot be parsed counts toward the cap, so an undatable record cannot hide spend.
- **Root-cause lines for stuck chains in the pick, `status` and the doctor row (#1614).** New `dag_stuck.py` renders one line per decision root from the stored dependency map, most freeing first: `#X, #Y wait on #R's decision; /sigma-unpark #R` (first five numbers, then `and N more`; an unarmed root says `/sigma-promote #R`, a pull-request root `PR #R is open`, a cycle `#a, #b wait on each other`). A pick that finds nothing pickable prints these lines instead of "They become pickable by themselves" only when every held goal waits on a person's decision or a cycle; otherwise the old text stays. `status` adds `stuck chains: N` with the top five, and the `dag map` doctor row adds the stuck-root count when it is not zero. Local file only, no `gh` call; mode `off` or an unusable map prints exactly what it printed before. `dag_chain.stuck` entries gain `kind` (and `cycle` for a cycle). Not measured against a real repository here.
- **The chain decision sheet: `chain.py sheet <sdlc_dir> <chain>` and `chain.py answer <sdlc_dir> <chain> --answers <file>|--defaults` (#1585).** `sheet` reads only the local chain record and prints every decision the chain needs together: end goal, open members, depth, units, source and age, order, done prerequisites, what the chain runs ahead of, cross-unit edges, the numbered questions with the recommended answer first, the exact writes that follow approval, then `Nothing has been changed yet.` It makes no GitHub call. The questions are the label drain's own list over the member set plus three chain-only ones (a cross-unit edge, an end goal that cannot be armed, and the cap, which has no default yet); a question the data already answers is not asked, and more than 25 are grouped by kind (`group:<kind>`). `answer` records answers in the chain record and hands a parked member's answer to the unpark resolve path, so the issue gets the same recorded Q&A block as `/sigma-unpark`; answers are bank options (the cap a whole number), never free text, so nothing a person typed is posted. `--defaults` accepts every recommended answer except a question with none (the cap, a spend question); the state becomes `approved` only when no blocking question is open and the plan is complete. `--partial` is refused for now. `chain.py plan` now also records `planned_at`, each member's unit, spend flag and `answered`, and keeps earlier answers on a re-plan.
- **The chain plan: `chain.py plan <sdlc_dir> <N> [--json]` (#1584).** Lists every open prerequisite behind one goal, deepest first, from the stored dependency map in one local read, or, when the map is off, stale or lacks the goal, from the label drain's seed closure with one REST read per unknown reference (bound `chain.max_members`, default 100 from the map, at most 50 live; over the bound is refused with the count, a cycle is refused naming every member). Each member carries the label drain's verdict (from the map only the life is known, so only armed, unarmed, parked and unknown) and a parked member its kind; the report adds explicit or ledger edges, depth, cross-unit edges, external prerequisites (other repository, third-party issue, pull request), closed prerequisites as done, priority inversions and the number of higher-priority goals it runs ahead of (reported, never written), and the weak needs/after mentions ignored (not knowable from the map). GET-only toward GitHub; the one write is the local record `.sdlc/state/chains/c<N>.json`, numbers and labels only. Measured with a counting fake: a live plan of K members uses K single GETs plus one list read, a map plan none; local wall time only, network wall time was not measured.
- **Cheap change check for the stored dependency map, `dag_check.py` and `gh_api.conditional_get` (#1617).** Once at the start of a pick, when `discovery.dag.mode` is not `off` and a map exists, one `issues?state=all&since=<cursor>` request goes out with the stored ETag (`gh api -i`; a 304 exits 1 and means not modified, no quota used, `checked_at` advances). A 200 applies each changed issue or pull request to the map in place (state, life, priority, body edges, merged flag, new issue, outside references under the existing cap) and equals a full read of the same final state; more than 500 changed asks for a full refresh instead; any failure leaves the map untouched, prints one stderr line and the pick takes today's path. Known gaps, left to the hourly full read: a priority changed only on the board field, and a deleted or transferred issue. Fixtures are real captured output; the direct-call ratchet baseline is unchanged. Not measured against a real repository here.
- **The chain query: `dag.py chain|chains|stuck` and `dag_chain.py`, schema `dag-chain/1` (#1613).** `dag.py chain GOAL [--json] [--verify] [--include-resolved]` lists every unresolved transitive blocker of one goal from the stored map, blockers first and the goal last, ties by priority rank then number, with waves, a decision (parked, unarmed, unmanaged, cross-repo, unknown, cycle) and its hint for each member that needs a person, and a `closure_id` that changes when a member is added, resolved or changes life. `--verify` re-reads up to 50 open members live through the REST helper; past the cap the answer says so and is marked incomplete. `chains` lists the connected groups of two or more open members and `stuck` names each decision root with what waits on it. Read-only; exit 0 answered, 2 goal unknown, 3 map unusable, 4 mode off.
- **The stored dependency map expires each outside node on its own read time, and an empty goal list never replaces a good map (#1829).** Every node read outside the lists carries `read_at`; the seven-day cache honours that stamp instead of the header's `last_ok_at` (a node with no stamp is read again once), and a refresh whose goal list is empty while another list is not keeps the old file with `last_error` `EmptyList` (provisional rule, owner to ratify).
- **The native-relations note names the map store's real edge sources (#1827).** Section 5 listed `ledger`, which the store does not have; it now lists `body`, `comment`, `timeline` and `other` and says an unlisted `native` would be stored as `other`. A test keeps the note and the store's list in step.
- **Dependency map refresh policy, liveness and an age line (#1612).** New `dag_refresh.py`: `refresh_if_due` refreshes the stored map after `discovery.dag.ttl_minutes` plus a stable per-clone offset of up to 10 percent, within a call cap and time budget derived from `SIGMA_WATCH_CALL_TIMEOUT`, from three triggers (a duty inside the reconcile tick, so `TICK_CALLS` stays at eight; the first pick of a process; `dag.py refresh [--force]`). Doctor row `dag map` fails after three TTLs with a live loop or watcher, or after two refreshes in a row errored; `dag.py line` and the session-start hook print the age and size with no network call. The automatic parts run only when `discovery.dag.mode` is written as `shadow` or `on`; mode `off` is unchanged. Not measured against a real repository here.
- **Research note on the host's native blocked-by relations as a read-only edge source (#1628).** `docs/dag-native-relations.md` records the read-only endpoints, measured cost and conditional-request behaviour, the conflict rule and a recommendation (do not build yet); no product code.
- **A pick trace failure never replaces a pick's result (#1815).** `Recorder.end` and the call-count hook now drop any exception, so a failure building or serialising the trace line cannot replace what `loop._next` returns; the duplicated counter line in the source's runner seam is removed, and the write-surface inventory lists the stored map's two writes.
- **The full read for the stored dependency map: `dag_collect.py` and `dag.py rebuild` (#1611).** Five open-issue lists (goal, blocked, blocking, parked, follow-up) through the shared REST fetch with no assignee filter and a 2,000 cap per list (a hit marks the map `partial`, which `usable` refuses), life from `blockers.classify`, priority from the repository's aliases and prefix, edges from the core extractor plus outstanding ledger hand-offs. References outside the lists are read newest first, 25 per refresh, resolved ones cached seven days, the rest reported `unread`; a merged pull request resolves, a closed-unmerged one does not. A failed list or an empty answer leaves the old file byte-identical. Nothing reads the map yet; the private-clone call count and wall time are owner-gated and not yet taken.
- **The stored dependency map: file, schema and off switch, `dag_store.py` and `dag.py show` (#1610).** `.sdlc/state/dag.json` (schema `dag/1`) is published atomically and holds numbers and codes only, no titles; `load` names a missing, truncated, corrupt or other-schema file and never raises; one refresher at a time through a non-blocking lock that fails open without `fcntl`; `usable` has separate schema, built, partial and age clauses. New keys `discovery.dag.mode` (default `shadow`; `off` reads and writes nothing), `ttl_minutes`, `order` and `cycle_comment`. Nothing reads the map yet.
- **A pure dependency-graph core, `dag_core.py` (#1609).** One standard-library, no-I/O module builds the blocker graph as an object: unresolved closure, blocker-first order and waves, each blocking cycle reported once whichever node a walk starts at, depth, open dependents, components and leaves. Every walk is iterative, so a 5,000-deep chain needs no recursion limit; output is byte-identical for any input order. A merged pull request resolves, a closed-unmerged one does not, and an unknown state never resolves. A library module with no command yet; nothing calls it.
- **Opt-in pick trace: where a pick's `gh` calls and seconds go (#1607).** `SIGMA_PICK_TRACE=1` or `loop.py next --trace` appends one JSON line per pick to `.sdlc/state/pick-trace.jsonl` (rotated at 1 MB) with per-stage call counts by class (list, read, write, graphql, project) and seconds, counted once at the single runner seam; `loop.py pick-trace-report <dir>` prints per-stage median and p90. Off by default: no file and no extra call. The private-clone measurement is owner-gated and not yet taken.
- **The stored dependency map skips other-repository references (#1608).** `blocker_scan.extract_refs` now calls `is_cross_repo` for every match, so `owner/repo#N` and `repo#N` are no longer stored as local blocker edges; the board mirror schema is `board-mirror/v3`, so an existing mirror is refetched once.

## 1.0.16 — 2026-10-17 — the resource guard: machine-derived limits, admission for verify and model sessions, a lease table, holds and shedding, calibrated on macOS and Linux; label drains with blockers and up-front questions; Rulebook learning by label kind; and the filing-triage and upkeep fixes
- **The verify shed follows the one resource-mode resolver (#1794).** `SIGMA_RESOURCE_MODE=observe` no longer lets a critical machine stop a verify, and `enforce` in the environment arms the shed even when the config says `off`.
- **Spend triage recognises a nationality before "dollars" (#1795).** `40 US dollars`, `40 Canadian dollars` and `forty US dollars` next to a compute word now park as a spend phrase; without a compute word, or inside a fenced block, they still arm.
- **The resource guard is calibrated on a real macOS host and in a 2 GiB Linux container (#1772).** Both evidence records are measured. The runs found two Linux gaps, fixed here: a container's total memory is its cgroup limit (the host's memory made every verify wait forever), and the footprint meter reads `/proc` when `ps` is not installed. The five defaults stay uncalibrated.
- **Doctor shows the resource rows on a default install (#1786).** The rows appear whenever the resolved resource mode is not `off` (an empty config resolves to `enforce`), instead of only when a mode was named or parallel goals were on.
- The controls runner matches a FAILED line on the full test node id, so a test whose name is a prefix of another's can no longer pass for it (#1787).
- **Spend triage recognises a dollar sign and more currency codes after the number (#1756).** `40$`, `40 $`, `40 AUD`, `500 JPY` and similar three-letter codes next to a compute word now park as a spend phrase; non-money units such as `40 MB` and amounts inside a fenced block still arm.
- **The lease CLI reads the `resource` config block (#1733).** `resource_lease.py admit|release|status|wait` takes its mode, ttl and limits from the project's `resource` block, like the other resource callers, instead of its built-in defaults only.
- **The session lease is renewed while its session runs (#1740).** `resource_guard.session_lease` starts a daemon renewer thread (default every ttl/3, stopped before the release), so a long session is no longer reclaimed as expired.
- **The autowatch drive now takes a `session` lease (#1758).** A driven `claude -p` session asks for admission like the other model-session spawns; a hold records a `deferred` note, starts nothing and does not count a hop. The spawn ratchet also matches string commands such as `claude -p`. Off unless `resource.mode` or `SIGMA_RESOURCE_MODE` is `observe` or `enforce`.
- **`resource.shed_after` and a controls runner (#1769).** The number of critical samples before a verify run is shed is now configurable (default 3, unchanged when unset; a bad value reads as 3 with a note), and `tools/resource_controls.py` applies each listed resource-guard break to a temp copy in CI and requires its test to fail (five breaks, run once locally; the CI run is unmeasured).
- **Label drain page carries a measured real run (#1761).** `docs/label-drain.md` now holds call counts and wall times from one run on a throwaway private repository (plan, arm, answer, one pick with and without `--label`), names what was not measured, and a docs test keeps the table consistent.
- **Verify admission has one total deadline and records a literal-workers hold as waiting (#1768).** A command list now shares the single `timeout` budget (each command gets what is left; none left is a timeout outcome), and a verify held because a literal `-n N` cannot fit is written to the waiting table so new goal starts do not take its memory.
- **One resource mode resolver for every surface (#1759).** `resource_guard.resolve_mode` (environment, then `resource.mode`, then `enforce`) now serves the picker, verify, sessions, watcher, doctor and log; model-session spawns, verify and the watcher sample step, which defaulted to off, now follow the `enforce` default, and the docs state it.
- **A quoted deny word now holds the filing unless the word before it is descriptive (#1755).** Lead-ins such as later, maybe, yes, go ahead, instead, let us, I'd and we'd before a quoted dangerous command park; a naming verb, a noun head such as command or word, a negation, a determiner or a copula still reads as description.
- **Label drains are documented and measured (#1582).** `docs/label-drain.md` covers the run label from arming to the stop, `--label` appears in the loop and goal skills with one pinned guarantee sentence, and the page's GitHub call counts come from a counting fake transport (the real scratch-repository run is unmeasured).
- **The advisory reviewer spawn (cursor, codex, `review.command`) now takes a `session` lease first (#1738).** `reviewer.py run` uses `resource_guard.session_lease` like the other model-session sites; a hold records the existing failed run (`failure: held`), starts nothing and prints no verdict, so a hold can never read as an approval. Off unless `resource.mode` or `SIGMA_RESOURCE_MODE` is `observe` or `enforce`; off writes nothing.
- **Label drain honours recorded decisions (#1732).** `label_drain.py plan` drops questions that already have a recorded answer and recomputes `ready`, so it agrees with `answer`; `loop.py next|next-batch --label` counts the plan's real open questions and no longer refuses on unarmed members once a `leave them` decision is recorded. With no recorded decision the output is unchanged.
- **Delete-resolution follow-ups (#1741).** The dropped-by-delete list marks where it was cut (`...` on a clipped line, `... (N more lines)` on the record), trimmed lines under 4 characters never count as moved elsewhere, a file under a symlinked parent directory is refused, and a path deleted at one stop that a later stop touches parks as `deleted-path-touched-again`.
- **A quoted deny word after a first-person or adverb lead-in now holds the filing too (#1726).** Lead-ins such as let's, I'll, afterwards and ok before a quoted dangerous command park like the unquoted form.
- **Label spend-cap test and doc note (#1744).** The spend cap is asserted at exactly level 2 and the spend answer path is shown never to raise the level; the Rulebook doc says earlier `repo`-area arming answers no longer count toward a label-kind pair.
- **A recovered run-label answer now records a goal (#1725).** When the arming effect landed before the record, the rebuilt question had no members, so the record had no issue and its decision comment was not posted; the lowest open member now carries it.
- **A recovered run-label answer with no open member left now records without a comment (#1751).** The record is stored with no issue, one line says why, and no comment is attempted, so no comment error text appears.
- **Label recommendation also needs the records gate (#1743).** With records closed and rules open the run-label plan is now byte-identical to the old output, and the history line is only for the label-set question.
- **Run labels learn by kind (#1581).** `label_drain.py plan` and `answer` record the arming question under `label-kind:<kind>` (kind from the label prefix or `label_drain.kinds`), show an approved rule's option first as recommended and add a history line per question when records are on; the answer record carries `rule_id`, `shown` and `accepted`, public repositories hash the label name, and the doctor lists answers, level and rule coverage per label kind. With the gates closed the output is unchanged.
- **The resource guard reports its own liveness (#1604).** `log.py slots` appends `admission HELD 12m: memory` while `hold.json` is held; the watcher tick makes one more bounded call, `resource_guard.py sample --record`, only when `resource.mode` is `enforce` or `observe` (it refreshes `last.json` and `hold.json` and reclaims stale leases); `/sigma-doctor` gains seven `resource` rows (platform, mode, admission with the "held with 0 leases: other software" wording, sample age against the watcher bound, stale leases, `max_concurrent` against the derived cap, a `-n` pin against the derived workers), shown only when `parallel.goals.enabled` or a `resource.mode` is set; with `ledger.enabled` a hold start and end each write one note, at most one per `hold_alert_minutes` (default 10, uncalibrated).
- **`loop.py next-batch|next` can answer `HOLD` when the machine has no room (#1600).** Before any claim the picker takes one resource sample: slots become the lesser of `parallel.goals.max_concurrent` and how many goal-cost units the headroom admits, and with none it prints `HOLD` on stdout and `HOLD (reason; retry ~Ns; held since ...)` on stderr, exit 0, claiming nothing and spending no budget. `resource.mode: observe` logs the would-be hold and admits; `off` skips the check; an unsupported platform or a guard error under `enforce` admits one goal with a one-line notice. The supervisor reads `HOLD` as a clamped sleep (30 to 900 seconds) that does not consume attempts. `SIGMA_RESOURCE_MODE` overrides the mode (the test suite sets `off`). The programmatic `run_loop` driver is not gated. Thresholds are uncalibrated.
- **The supervisor session lease now lasts 24h, not the 6h default (#1737).** A live supervised session longer than 6h was reclaimed as expired, freeing its slot while it still ran; both supervisor call sites now pass the ttl, and a dead pid is still reclaimed at once.
- **Every Sigma-spawned model session now takes a `session` lease first (#1603).** The judge, the supervisor (Claude and Codex) and the upkeep launcher share `resource_guard.session_lease`, which releases on exit, exception or timeout; a hold becomes each site's own refusal, pause-and-relaunch or deferral. Off unless `resource.mode` (upkeep config) or `SIGMA_RESOURCE_MODE` is `observe` or `enforce`; off writes nothing. A ratchet test fails on a new host-CLI spawn that is not gated or listed.
- **`resource_lease.py` follow-ups (#1722).** Reclaiming a lease whose pid was reused no longer measures the foreign process into the cost ring; Linux process start is in seconds (ticks divided by the clock rate) so the 2 s tolerance means seconds on both platforms; `wait` applies the same pressure and `max_goals` gates as `admit`; a lease record without a goal no longer raises.
- The resource guard keeps a hold's original start on repeat ticks, reads an empty history as open, writes `hold.json` through a per-process temp name removed on failure, and reads a `cpu_reserve_cores` at or above the core count as derived plus a note (#1714).
- **`resource_lease.py` lease-table fixes (#1715).** A lease whose pid was reused by another process (start time differs from the one recorded at admit) is reclaimed as stale; `wait --kind goal` holds while a started goal's verify is waiting, matching `admit`; releasing a lease file with no id no longer raises KeyError; `wait --max` rejects nan and negative values with a usage error (exit 2).
- **Session upkeep resolves a delete when no line is lost without a record (#1716).** When the integration side deleted a file the unit changed and the session keeps the delete, every unit-added line must be elsewhere in the resolved tree or listed in `dropped-by-delete.txt` and approved by the independent review; the list goes in the stamped commit message and the upkeep record. When the unit deleted a file the integration side changed, every integration-added line must be elsewhere, and no list can save it. Resolutions with no delete are unchanged.
- Filing triage now parks euro and pound amounts (symbol, code or word) near a compute noun as spend, like dollar amounts; fenced blocks and amounts with no compute noun still arm (#1710).
- **A quoted or backticked deny word after an action verb now holds the filing (#1704).** A command in quotes after a verb, a hand-off word or a purpose clause (redact X to nuke Y) parks again; quoted mentions after a noun head or a naming verb still arm.
- The Decision Rulebook doc's migration section no longer says rebase upkeep findings still get the legacy confirmation label while triage is on; it names the triage-off exception, and a docs-claim test pins it (#1709).
- **A label drain pulls open blockers, refuses to start on open questions and stops as `label-held` (#1579).** Under `--label` only: a held-on ref outside the label is armed and pulled in (once per ref, cap 50), `next` prints the `label_drain.py` verb to run when members are unarmed or questions are open, and a run with open but unpickable members stops `label-held` instead of `backlog-empty`. Unscoped and `--feature` runs are unchanged.
- **The upkeep confinement docs list what the macOS wall does not cover (#1712).** The profile allows everything by default, so the keychain and mach services stay reachable, reads outside the listed credential directories stay open, and writes to the temp areas stay open. The dead `SESSION_ARGV` constant is removed; the doctor flag check now reads `session_argv(True)`.
- **The applier now says when evidence behind the default branch holds it (#1699).** A real tick and `--dry-run` print `applier: held - <reason>` instead of nothing or `would behind`, and the doctor applier row adds `HELD: <reason>` until a later tick runs; pull to clear. Output with current evidence is unchanged.
- **`resource_lease.py admit|release|status|wait` keeps a lease table over the resource guard (#1599).** Under an `fcntl.flock` lock each admit reclaims stale leases (dead pid or past ttl), counts the unmaterialised remainder of every lease, and grants the largest fitting count or holds with a retry time (exit 10); costs are the p90 of the last 32 measured footprints per kind, outliers above 4x the median excluded and logged, and a verify gets at least one worker when one fits. State is in the XDG state folder beside `hold.json`; admission is paused, never a process. Not called by anything yet (operator-run); POSIX only.
- **The upkeep docs say what the agent conflict level does with no spend key set (#1694).** It runs with no spend limit; set `max_budget_usd_per_unit` (and optionally `spend_ceiling_usd_per_day`) to cap it. The doctor row `resolver flags` no longer fails while the only unverified flag is `--bare`, which the resolver never requests.
- **The resolver and reviewer can now authenticate (#1691).** New key `upkeep.conflicts.agent.credential_var` (default null) names one environment variable holding the model login; it is the only credential forwarded (never `GH_TOKEN`, `GITHUB_TOKEN`, `SSH_AUTH_SOCK` or `GIT_*`). A launch runs a bounded `claude auth status` in the scrubbed environment first and parks with "resolver cli is not logged in in the scrubbed environment" when logged out; the doctor gains a `resolver auth` row. Not run against a real CLI: the live run is owner-gated (Awaiting).
- The resource guard is documented in `docs/parallelism.md` with its controls table and the macOS and Linux calibration records, where the macOS record is a partial run (one guarded verify, held by the memory gate on an already loaded host, no footprint measured) and the Linux record stays NOT RUN, so the defaults stay uncalibrated (#1605).
- **`label_drain.py answer <sdlc_dir> <label> --answers <file.json> [--dry-run]` applies a person's answers to the up-front questions (#1580).** The file maps question ids from `plan` to options; an unknown id or an option outside the bank refuses the whole file (exit 2, no write). Per answer the effect comes first (`arm` for the arming route, `unpark.py resolve` for a parked member, the single-use `sigma:spend-approved=<token>` marker only on approver-authored paid members for `go ahead`, the others named), then one decision record with scope `label:<name>`; a failed effect writes no record. Questions left out stay open, `plan` is re-run to print what is open and `ready`, and a repeated answer records once.
- **`label_drain.py arm <sdlc_dir> <label> [--apply]` arms the unarmed members of a run label and pulls in their open blockers (#1577).** Dry run by default; with `--apply` each unarmed issue gets the goal label (parked and blocked labels dropped in one swap), a priority label only if it has none (`label_drain.default_priority`, default P2) and the configured assignee only if it has none, via the triage action pair. A pulled blocker also gets the run label and one comment (`label_drain.arm_pulled_blockers`, default true). Upper-tier, parked, gate-held and other-assignee issues are refused with a reason; no body is edited, no label created or removed.
- **The upkeep resolver session is confined, or has no Bash (#1693).** Where `sandbox-exec` exists (macOS) the launch is wrapped in a profile that denies reads of credential directories and writes outside the session, and limits network to port 443 (not filtered by host); where no wall exists the session runs without the Bash tool. The doctor row `resolver session confinement` is computed from the same function and says which. PROVISIONAL: no live run yet (owner-gated); this corrects the #1573 entry's claim of a wall.
- **`label_drain.py plan|status <sdlc_dir> <label>` shows what a run label would drain, read-only (#1576).** It lists the open members with a verdict each (armed, unarmed, parked with its kind and question, upper-tier, assigned-elsewhere, held-by-gate), pulls their open blockers transitively (depth 8, at most 50 reads; a pull request, another repository or an unreadable ref counts as unresolved, never satisfied), warns on cross-unit edges, cycles, a missing `## Done when` and a declared paid step, prints the number of calls made, and refuses more than 200 members. It never writes and never removes the label.
- **The applier no longer applies silently when the ask's gesture does not match the rule option's gesture (#1362).** At level 4 such an apply now comments (and is not counted by the spot check); matching applies and a closed gate are unchanged.
- **Retiring an auto-approved rule now sticks for its pair (#1696).** `rulebook.retire` of a rule with `auto: true` (while `rulebook.rules.auto_approve` is on) records the pair in a `retired_pairs` list in the rulebook file, and `rule_draft.auto_approve` no longer redrafts it; a hand `draft` or `approve` of the pair clears the record. With the gate off, or for a hand-approved rule, the file is written exactly as before.
- The newest-parked-label-event read now follows event pages (at most 10 pages of 100) instead of giving up on a full first page, so a label-only re-park on an issue with 100 or more events is no longer resumed by the sweep; a timeline longer than the cap still reads as no signal (#1639).
- **The applier tick prints only actions taken on issues (#1564).** It reports `applier: N applied` and `applier: N vetoed` on separate lines, each only when N is above zero; ask bookkeeping (opened, armed) no longer counts as applied and prints only with the new `--verbose` flag (the `sync` verb still prints its opened count).
- `loop.py next` and `next-batch` take `--label NAME` (#1575): the run is confined to goals carrying both the goal label and that label, across units, and each goal is still based on its own unit branch; a missing, reserved, unknown, or `--feature`-combined label is refused with exit 2 before any backlog read.
- The upkeep sweep skips its backup prune when the sweep cap is already spent, and touches the sweep beat before every git call during the prune, so a slow remote does not make it look dead; the prune also gets the sweep's remaining cap as a deadline, so no git call starts once it is spent and the default runner's call is cut at it, recorded as outcome timeout and retried by the next sweep (#1632, #1700).
- **The upkeep auth probe runs under the session wall, and the docs say the default parks (#1713).** `auth_status` takes the wall's argv prefix; the launcher preflight and the doctor `resolver auth` row run `claude auth status` inside the same confinement as the session where one exists (macOS), plain elsewhere. `docs/upkeep-part-b.md` and the doctor fix text now state that with `credential_var` unset every agent-level launch parks until it is set. The no-login end-to-end test now checks the logged-out state and the untouched branch tip for real. The wall stays unverified against a live session.
- A quota reset time read back from a park comment is now bounded like the parser's (#1641): one more than 7 days after the park counts as unknown, so the ordinary window applies.
- A park re-applied by label only (no Sigma park comment) after a waiting note is no longer resumed by the sweep when its blocker closes; the signal is the newest `sdlc:parked` label event, and an unreadable event keeps the old behaviour (provisional, owner to ratify) (#1630).
- A quota park that names a reset time or retry-after now records `reset_at` on the park and the ask, and the ask is held until then; when no reset is named it waits the ordinary window (`window_minutes`), replacing the fixed 24 hours (#1563).
- **Upkeep's agent level resolves in one scratch session per unit (#1573).** One history-less, remote-less copy re-entered at each conflicted commit, with base/ours/theirs, commit messages and earlier stops as context, edit/build/test allowed, a network wall where the platform has one (the doctor says which), a whole-session wall clock, and the same write-back and proof gates; the plain export remains for plans without `session`.
- **A verify that runs into sustained critical pressure is stopped, not failed (#1602).** With `resource.shed` at its default `verify`, three critical samples in a row stop the verify process group Sigma started (TERM, a grace, then KILL); the result is `RESOURCE-HOLD (shed)`, exit 5, with no evidence and the goal not failed. No session is signalled and nothing is paused.
- **`unpark.py report <sdlc_dir>` shows what the applier would do (#1561).** Read-only: every parked or blocked issue with its declared or suggested kind, area, matching rule and gesture, the pair's level, and whether the applier would act now, later (window) or never (and why), then one total per outcome; `--json` for tools.
- **The applier reads stuck pending asks at most once a window (#1360).** A pending ask older than twice its window is read for an owner `no` once per window instead of every tick, recording `veto_read_at` on the ask; younger and applied asks are read every tick as before (provisional, owner to ratify: a `no` on a stuck ask can be honoured up to one window late).
- **The `ai_filed` per-goal cap parks the lowest priority, and `handoff.py open --priority` is forwarded (#1697).** Below `ai_filed.cap_bypass_priority`, a filing that hits `ai_filed.max_per_goal` parks the goal's lowest-priority filing (newest among equals), which can be an armed one that then gets the parked-at-filing comment; equal priorities still park the last filed. `open --priority P1` now bypasses the per-goal cap like `track`; `ai_filed.max_per_run` still parks it. Counter entries gain `issue` and `prio`.
- A secret straddling the 300-character cut of a decision record's choice or reason is now scrubbed before the cut, so no fragment survives, and the import summary still counts it (#1631).
- **Bulk-declare park kinds, and a suggester that reads clauses (#1562).** `unpark.py declare <dir> --from-suggestions [--kind K] [--dry-run]` declares every undeclared park whose suggested kind is confident, one `[declared]` or `[skipped]` line each; `irreversible` and `unknown` are never bulk-declared. The suggester counts `delete`, `drop table`, `force push` and `destructive` only in action position (the #1519 clause rule), so a park that merely mentions the word is no longer suggested irreversible.
- **Upkeep's agent level can launch (#1554, slice 1).** The probed resolver flags are confirmed (only `--bare` stays refused), the model catalog holds real ids, and `conflicts.agent.max_budget_usd_per_unit` / `spend_ceiling_usd_per_day` are optional: null means no limit (no `--max-budget-usd`, no ceiling check; a set cap or ceiling is enforced as before). The doctor names the mode. Tested against a stand-in command only; the live run with a real model is owner-gated and not done.
- A `keep-parked` on a quota park answered "wait it out" no longer posts the sweep opt-out marker, like a dependency "wait for it", but the sweep does not resume a quota park; every other route still does (provisional, owner to ratify) (#1547).
- The upkeep sweep now prunes backup refs past `backup.keep_days` by itself, keeping the newest `backup.keep_last` per unit, once per sweep and bounded; a failed prune never fails the sweep and is retried next time, and `/sigma-doctor` shows the backup count and last prune (#1557).
- **The rulebook can approve its own learned rules (#1555).** `rulebook.rules.auto_approve` (default false) lets the watcher tick, `rule_draft.py list` and `suggest` draft and approve a rule for every learned pair with a majority option, as the first configured owner, at any level; the applier applies it after the window, an owner `no` holds it until reconfirmed, hard-stop kinds are never touched, and the doctor warns while it is on.
- `rule_draft.py rules <dir>` lists every approved rule (id, kind, area, option, action, last confirmed), and the doctor's rulebook row counts approved rules per action and names the verb (#1546).

- Decision records scrub secret shapes (the leak scan's set) from their free text before the file is written, and the import summary prints how many values were scrubbed (#1558).
- **`applier.py --dry-run` shows the plan with the applier off (#1695).** With `rulebook.autonomy.applier.enabled` not true it prints the off line and then the same `would ...` lines the enabled applier would, planning with that one gate treated as open and writing nothing (no heartbeat, prune, ask write or GitHub write); output with the applier on is unchanged.
- **`applier.py --dry-run` says when the applier is off (#1545).** With `rulebook.autonomy.applier.enabled` not true it prints one line saying the applier is off and exits 0, instead of printing nothing; output with the applier on is unchanged.
- **The land verb measures the sibling half of a pair itself (#1552).** `feature_land` used to pass no landed measurement, so any unit with a second repository was always refused as "nothing was supplied to measure". It now reads the sibling's pull requests and proceeds only when one merged; an unmerged or unreadable answer still refuses, and the refusal names the goal recorded for that repository and the remedy.
- An AI-filed issue whose requested priority (`--priority`) is P0 or P1 is armed (at its bucket priority) past a reached per-goal `ai_filed` cap while the counter is readable (key `ai_filed.cap_bypass_priority`, default P1, invalid reads as P1); lower requested priorities and the per-run cap still park, with a reason naming `ai_filed.max_per_goal` / `max_per_run` (#1553).
- **`reconcile.py blockers <sdlc_dir>` previews the blocker priority repair (#1560).** Read-only and mode-independent: it lists each blocker the repair would raise (current and new priority, the goal it blocks), the blocking cycles and the outside or unreadable blockers, and writes nothing; `GitHubSource.repair_blocker_ranks` gains `preview=False` for it.
- **`feature_rebase.py preview` is a sweep-level dry run (#1559).** It lists every unit the sweep would consider (`upkeep.units.include`/`exclude` applied) with a predicted outcome (current, fast-forward, clean rebase, conflict with its files, or refused with the reason), from the remote-tracking refs, with `--fetch` as the only network step. It moves no ref, pushes nothing, makes no worktree and works with the upkeep gate closed; the conflict answer is a merge probe, so a rebase may still differ.

- **A dependency park kept parked by a "wait for it" decision is resumed when its last blocker closes (#1549).** The decision (a person's answer or an applied rule) now posts a waiting note; the sweep resumes only a park carrying it, as an autonomous unpark, and the keep-parked opt-out marker still wins.

- **`resource_guard.py sample [--json]` reads the machine's memory, swap, pressure and CPU (#1597).** New stdlib-only readers (ctypes on macOS; /proc and cgroup v2 on Linux) return a typed `Sample` plus a per-process `footprint(pid)`; any other platform or an unreadable total raises `Unsupported` and the command exits 3. Nothing calls it yet; no process is spawned and no file written.

- **Session-mode upkeep resolves every conflict shape a person could (#1692).** In session mode the protected-path, structural, merge-stop and count refusals no longer apply (the proofs and `timeout_minutes` are the gate), add/add and delete/modify stops are given to the session and applied as `git add` or `git rm`, a refused proof is fed back to the same session before parking and the park names why the last try failed, and the attempt record is keyed by unit tip + base tip so a moved base starts fresh and a run cut off by the sweep cap is not counted. Legacy mode is unchanged.
- **`resource_guard.py` derives limits from the machine (#1598).** `derive` and `decide` turn a reading into a memory reserve, share cap and CPU reserve as ratios of the machine, hold on the lowest of three samples and resume on the higher of the lowest two, and `hold.json` is written by atomic rename; the new `resource` config block is all-null (derived) and documented in `docs/configuration.md`.
- **`loop.py verify` runs under admission when `resource.mode` is `enforce` or `observe` (#1601).** The proving command runs in its own process group with a lifeline and the `verify.timeout_minutes` budget, output kept to a 64 KiB tail, a `{workers}` token (integer only) plus `SIGMA_VERIFY_WORKERS` and `PYTEST_XDIST_AUTO_NUM_WORKERS` carry the granted worker count, and the tree's peak footprint per worker is recorded as the learned cost; a hold exits 5 with `RESOURCE-HOLD` on stderr, writes no evidence and runs nothing. Unset or `off` is unchanged. The xdist variable was checked against pytest-xdist 3.8 only.
- **The import model classifier scrubs the question and option labels before sending them (#1698).** `build_request` runs both through the secret-shape scrub before the 300-character clip, so a key in a past question never leaves the machine, not even as a clipped fragment; the cache key and ordinary prompts are unchanged.
- **The applier reads pending asks that cannot apply at most once a window (#1702).** An ask with no matching rule, a level below 3, a hard-stop kind or the daily cap reached is throttled like a stuck one, from its first tick rather than after two windows; an ask that could apply is still read every tick, and an owner `no` on a throttled ask lands within one window.
- Blocker priority repair no longer re-reads the same ten newest outside blockers every tick: a blocker read and found at its rank (or raised) is skipped for ten minutes, so older ones are reached; unread blockers are counted in the repair state and the doctor row (#1706).
- **`label_drain.py plan` asks every up-front question in one pass (#1578).** `plan` now returns `questions` and `ready` (and a numbered section in text): each parked member's own question (an undeclared park first gets "which kind"), members no gate can classify, pulled blockers a person must decide on, the arming route once per label (new closed-list kind `label_set`, with a bank entry whose options none unpark), and a spend question only for a paid step the hard-stop net recognises, printed with the single-use, no-dollar-cap sentence. Every text comes from the question bank; `plan` stays read-only and exits 0 either way.
- The blocker repair preview applies the same ownership test as the repair: with hard stops on, a blocker Sigma does not own is listed as skipped, not as to raise, and the doctor row names the refusal instead of blaming label permissions (#1707).
- A filing or migration leftover that asks a person to decide now parks instead of arming: `ai_filed.triage.question_patterns` (title regexes, `[]` disables) and `handoff.py track --needs-person <kind>` declare a question, and `promote.py migrate-confirmation` parks them too (#1703).
- **Filing triage reads a currency sign after the number and hyphenated hard-stop words (#1734).** "Spend 40€ on cloud credits" and "30£ of GPU time" (also CHF, ¥, CAD, INR, yen, quid) now park as spend like the sign-first forms; the compute-noun and fenced-block rules are unchanged. "force-push" and "force_push" now match the "force push" hard-stop word, and the parked text still quotes the original clause.
- **The doctor now reports an unreadable AI-filed counter even when the config has no `ai_filed` block (#1731).** A missing or non-object `ai_filed` counts as empty for the "AI-filed counter is readable" and "AI-filed triage config is valid" rows, so a corrupt counter file no longer hides behind a default config; JSON-false triage stays silent.
- **Change zones (#1540).** `design.zones` in `.sdlc/config.json` (absent by default, owner-declared, last matching glob wins) and the read-only `skills/sigma-loop/scripts/zones.py classify <path>...`, which prints each file's zone, whether it is new at the branch's fork point, and the one rule that applies (`docs/change-zones.md`). `/sigma-vision` may suggest candidate legacy paths but never writes the key.
- **`quality_delta.py <goal>` measures each configured quality metric at the merge-base and at the head (#1635, PRD-0008 R-01).** Opt-in `quality.metrics` (absent by default), read from the config as committed at the merge-base; each command runs once per side in a scratch worktree, one at a time, as an argv with no shell, in its own process group (killed whole on timeout) and with credential-shaped variables removed, behind a memory probe derived from the host. Prints base, head, delta and `better`/`same`/`worse`/`unmeasured`; a failed or timed-out command or uncommitted work is `unmeasured`, never `same`. See `docs/quality-delta.md`.
- **`outcomes.py report` counts reverts and quick fixes per merged goal (#1636).** A read-only `sigma-log` verb that reads the action log and one `git log` pass and prints, for each mature goal, the reverts and `fix`-subject commits touching its files within 14 days. Its first line says it is a proxy, it prints no rate, and it prints `unmeasured` with the reason below 10 mature goals, with the log off, in a shallow clone or for a window of 14 days or less; the window is capped at the action-log retention. Shared files (a fixed list plus any file in over a fifth of the commits) attribute nothing. It writes no file and uses no network.

## 1.0.15 — 2026-10-16 — the Rulebook learns and acts: declared question kinds, live comparisons, applier asks and vetoes, importance-ranked rule suggestions, upkeep union merge and agent conflict level, claim release, and ledger host privacy
- The applier opens no ask for a park a person already answered (keep-parked, unpark or promote), so a sync after a rule is approved never turns an earlier human decision into a rule action (found by the acceptance run).
- The goal-replay conflict cause probe now has a per-pass time budget (three guard timeouts, after which findings keep the original wording), and the rebase's reported text in the finding is sanitised and capped; a probe/replay disagreement is documented as unknown (#1537).
- **Upkeep's agent level is hardened (#1536).** A sweep child that finds its sweep counter missing, inactive or from another run now fails closed (the single-unit pass is unchanged), a resolver result that drops a line the integration side added is refused as `base-line-dropped` naming a few lines, and the doctor's `resolver configured` row fails while the model still resolves to the placeholder id.
- The goal-replay conflict finding now says whether the upkeep pass caused it: a read-only `git merge-tree` of the goal tip against the unit's previous tip reports it as pre-existing (with the previous tip, backup ref and conflicted paths), introduced by the pass, or unknown, and only the introduced case says the pass brought the conflict forward (#1494).
- The upkeep sweep note now ends `:units=<n>` (units that ran, `0` for an empty sweep), the doctor upkeep units row skips branches named in `upkeep.units.exclude` and its remedy names that list, and a test pins that a unit differing from its branch only in case is registered (#1535).
- **A live session can no longer sit on claimed goals it never starts (#1531).** On a GitHub source, any session's pick releases a goal another live session has held longer than `session_release.hold_minutes` (default 30, `0` = off) with no work record and no agent marker, with a `claimed but not started` comment; started goals and goals inside the hold are kept. `next-batch` limits its picks to the session's free slots.
- **Upkeep's agent conflict level is fed from configuration (#1516).** `upkeep.conflicts.agent` adds model, per-unit budget, daily spend ceiling, timeout, sweep unit limit and reviewer keys; the level behaves as off until both spend keys are set, a reached ceiling parks the unit as `spend ceiling reached`, a resolver result that drops a unit line is refused with the attempt in the finding, and the doctor reports configuration, spend today and the last resolution. The launcher's unverified flags and placeholder model catalog are unchanged, so a real run is still refused until the owner-gated probe.
- The upkeep sweep now says what it cannot see: the job record gains `units_listed`, `unregistered` (remote `feature/*` branches with no registry record) and the registry reader's `notes`, and the doctor shows an upkeep units row naming each unmaintained remote feature branch, also when no `.sdlc/features/` exists (#1493).
- Upkeep findings are now filed at the lowest AI bucket priority (P4 by default) instead of the human default P1; a triage park writes that priority too, and `promote.py migrate-confirmation` swaps a followup's priority label for the bucket priority when it arms it (#1484).
- The upkeep engine now records the per-unit state file (`.sdlc/state/upkeep/units/<unit>.json`) at its single exit on every pass, under the unit lock, and the job no longer records the same outcome a second time (#1492).
- With triage on, a rebase-upkeep finding (goal-branch conflict, parked conflict, direct commits, would-drop refusal) is now filed parked with a declared question kind (`merge_conflict` or `needs_decision`), a park event and the lowest triage priority instead of P1, and no longer carries the legacy confirmation label, so the doctor no longer reports it as a leftover; with triage off nothing changes (#1490).
- **`rule_draft.py suggest` ranks which question kind and area pairs deserve a rule (#1513).** It scores each pair from parks, hours parked, goals blocked and answer agreement, names the majority option and whether one any-area rule covers it, never lists hard-stop or unknown kinds, and `--propose N` drafts the top N as proposals (never approved); the doctor's rulebook row lists the top uncovered pairs when rules are on.
- The source-error downgrade-to-park declares `quota` for a quota or budget failure and `needs_decision` otherwise, and the stale-worktree escalation park declares `needs_decision` (#1524).
- Filing triage holds a deny word only in an action clause: a clause that describes redaction, detection, gating or documentation before the deny word no longer parks, and the park reason now quotes the matched clause. Secret-shaped text and spend amounts still hold (#1519).
- **Upkeep's mechanical level resolves two append-only shapes (#1515).** A dated `## YYYY-MM` log and a bullet-list file that both sides only added to are unioned (per section in date order; base order then the unit's new items); an edit or removal on either side still parks.
- A config that sets `discovery.blocker_promotion.mode` to `"smart"` now gets a one-time skip line from the reconcile tick and a not-OK "blocker priority rule inactive" doctor row naming the one-word fix to `"always"`; what smart, always and off do is unchanged (#1489).
- `board_migrate.py --apply` now snapshots every card by project item id (issues, pull requests, drafts), fails if any card's Status changed during the option write, and prints counts that add up to the board (moved cards shown as a subset); an existing lane prints no kept-their-Status claim (#1491).
- **Ledger entry file names can use an alias or a per-repository salted host token, and the doctor warns about raw host names on a public repository (#1514).** `ledger.host_alias` and `ledger.salt_host_token` (both off by default, which changes no file name) replace or salt the hash of the host name in `entries/<actor>-<host>.<pid>.jsonl`; older files stay read and attributed, and the doctor's `ledger entries carry no raw host name` row reads the local copy of the ledger branch.
- An unstamped legacy answers block is no longer counted for a newer stamped park, and a quota wait-it-out ask is applied only after its recorded reset time (#1522).

- **The doctor row for `blocking_priority_override` describes the within-tier order (#1509).** It said a blocking goal sorts ahead of every other issue regardless of priority tier; it now says a blocking goal goes first within its own tier and a blocker is raised to the tier of what it blocks.
- **Session end and dead sessions now release goals they claimed but never started (#1529).** On a GitHub source, `loop.py session-end` hands back each goal in its own session marker that has no work record and no agent marker, through the same release as the over-capacity path (comment `claimed but not started: session ended`, ledger `release`, action-log `released`); a dead session's entry does the same on the next pick or start after `session_release.grace_minutes` (default 15). Started and live-session goals, a mismatched generation, and the local source are untouched.

- A needs_decision closing answer is now normalised through the bank (case, spacing, a trailing recommended marker) into the stored `answer`; other text is `free` and never learnable (#1521).

- **The applier now asks for parks that already exist, never asks twice for one park, and counts its window from when a rule became eligible (#1499).** `applier.py` runs a bounded sync (20 reads per tick) before each tick, an ask in any state covers its park, a new park or an unpark supersedes or closes the old ask (superseded asks are pruned after two days), an ask opened below level 3 is armed with one notice comment and a fresh window, the applied record carries its gesture and answer, and `applier.py <dir> --dry-run` prints the plan and writes nothing. Closed, nothing changes.
- The upkeep session line now says `window open, retry after HH:MM (last attempt HH:MM)` when a recent attempt stamp holds an open window; the line and the trigger share one decision function, `feature_upkeep_window.decide` (#1507).
- A merge parked for a missing approval now says the PR is not approved and how to approve it, instead of blaming GraphQL; the failed-read (budget) park keeps its wording (#1506).

- `promote.py migrate-confirmation` now arms an item whose only hold is an open blocker (`sdlc:goal` plus the `sdlc:blocked` overlay, reason `armed (held by #N)`) instead of parking it; the pick-time dependency hold and auto-unpark take it from there (#1518).

- **The confirmation migration no longer arms upper-tier items (#1517).** `promote.py migrate-confirmation` removes the retired label from an item labelled epic, story, dossier or spec and reports `upper tier: label removed, not armed` (no goal label, no park); `promote.py apply` refuses such an item with one line.
- **Rulebook levels pool machines, survive retention and recover from vetoes (#1500).** A pair's comparisons count together across machines (a machine with too few comparisons no longer caps it, a disagreeing one with enough still does), the ladder reads the newest `l4_min_answers` comparisons whatever their age and pruning keeps them, a veto's one-level drop wears off after `veto_recover_after` agreeing answers, and a missing area reads `repo`.
- **Blocker repair no longer writes to merged pull requests, builds edges from prose, or lets the dry pass change the cache (#1475).** The four blocker-priority writers drop any blocker that is not OPEN (MERGED and CLOSED resolve) and never raise a pull request, whole-body scans use only blocked by / depends on / depends upon, the dry pass writes nothing, and outside blockers beyond the 10-read cap are reported on stderr and in the result (`unread`).
- **The import `folders read:` line lists only folders actually found (#1523).** A project folder skipped as not found under any profile no longer appears in the line; it stays in the `skipped:` note.
- **An optional shared weekly and monthly spend cap covers every paid model call (#1502).** `rulebook.spend.weekly_usd` and `rulebook.spend.monthly_usd` (both unset by default, which changes nothing) cap the real cost of the import classifier and the drift judge together over a rolling 7 and 30 days; a call that would run past a cap is refused with one line, counted in `.sdlc/state/spend-cap.jsonl`, and shown by the doctor with the reset time.
- **Import docs show a project-folder form that works in bash and zsh, and a scan prints the folders it read (#1503).** The rulebook import section and the `--project-dir` help say one folder per flag (or a quoted array), and `scan`, `plan` and `apply` print a `folders read:` line on stderr.
- **Import calls a missing project folder "skipped", and plan and apply print scan notices (#1488).** A `--project-dir` found under none of the given profiles now reads `skipped: ... the other folders were read` instead of `refused`; `plan` and `apply` print the scan notices on stderr, and `apply` adds a `partial import: N files not read` line when a time budget, size cap or candidate cap cut the import short.
- **A hand-typed blocker on a third-party issue is reported, not raised, when hard stops are on (#1468).** The blocker-priority writer checks ownership before any write, so a foreign issue gets no priority label, no park and no comment, one stderr line names it, and the reconcile count leaves it out; `note()` returns False on a guard refusal, and a Sigma follow-up label now counts as Sigma's own.
- **The drift judge records which documents it did not see (#1481).** A document past the `max_total_chars` budget is no longer passed on empty and listed as read: it is named in `documents_clipped_out`, a cut-short one in `documents_truncated`, the prompt carries no empty block, and the doctor warns when the documents exceed the budget.
- **Headless judge calls no longer read the caller's project (#1472).** The import classifier, drift judge and unit assigner/validator now run `claude -p` in a fresh empty temporary directory (refused under an instruction-file ancestor), without `CLAUDE_PROJECT_DIR`, and with `--setting-sources user`, `--strict-mcp-config` and `--no-session-persistence` when the installed CLI lists them; the privacy and rulebook docs state what each call sends.
- **Imported decisions name their origin, count per folder, can be filtered by working directory, and one folder's import can be removed (#1477).** Each imported record and watch comparison carries an `origin` (hashed on a public repository), `plan` and `apply` print per-folder counts with extra folders flagged, `--only-in-repo` or `rulebook.import.require_repo_cwd` keeps only answers whose cwd is inside the repository, and `decision_import.py remove --project-dir DIR [--apply]` deletes that folder's imports (dry run by default; records without an origin are counted, never deleted).
- **The model import classifier charges a cut-off call its real cost and stops at the first budget cut-off (#1471).** The CLI checks `--max-budget-usd` only after a turn, so the default 0.05 cut off every call yet billed about 3 times the charge; a failed call now costs its envelope cost, the first cut-off stops the run with a message naming `rulebook.import.max_budget_usd_per_call`, the drift judge charges known cost on an invalid verdict, and the default is 0.50.
- **A goal cut from a unit while another pass rewrites it no longer carries the old unit commits (#1483).** The cut tip is now read from the new worktree's own HEAD and recorded for every unit goal, with the upkeep opt-in off too, so the goal's first rebase replays only its own commits; when the pick-time pass finds the unit's rebase lock held, the start re-checks the unit tip right after the cut, moves the still-empty goal branch onto it if it moved, and says so on the pick line (no wait; provisional). The busy wording, the lock-timeout comment and `docs/upkeep.md` now describe this.
- **The acceptance record now reaches the goal branch, and landed root copies are cleaned up (#1474).** `work.py commit` copies the root acceptance record into the worktree, `work.py pr` refuses a branch missing it, a confirmed merge removes untracked root plan, research and acceptance files byte-identical to what landed (differing or unlanded ones are reported, never removed), and doctor reports old untracked records and modified registry files.
- **`define.py open` cuts the unit branch from the remote base and records the owner (#1504).** The branch is now cut from `<remote>/<base>` after a fetch (the rule `work.py start` uses) instead of the local base ref, so a checkout whose local base is behind no longer opens the unit on stale code; a base missing on the remote is refused with one line, the output names the commit, and the unit record carries the active login as owner plus one repository entry (branch, owner). If the login cannot be read the owner stays empty and one line says so.
- **`migrate-confirmation` parks a rebase upkeep finding instead of arming it (#1473).** The finding now carries a `<!-- sigma:upkeep-finding -->` body marker, the migration classes it as its own `upkeep_finding` population (the marker, or the older body line) and parks it as `merge_conflict` or `needs_decision`, so it no longer gains `sdlc:goal`.
- **A rate-limited GitHub read no longer turns the doctor's `gh auth`, `gh project scope` and pinned-board rows MISSING (#1485).** `gh auth status` failing now retries once and asks REST before saying "not logged in" (only a REST 401 does), and a bare "unknown owner type" is confirmed over REST before the board is called gone; both read "(cannot verify)" with wait-for-the-reset advice and no `gh auth login` or create-a-board command.
- **Rulebook learning uses one comparison key, and every option declares its meaning (#1495).** Answers, rules, comparisons and drafts now share the route answer, so ruled pairs can reach level 2; each bank option says unpark, keep parked or only recommend; a re-park is asked again and one park counts once; `autonomy.py rekey` and `rule_draft.py show` are new.
- **`migrate-confirmation --apply` no longer reports complete when an armed card stayed off the Ready lane (#1469).** The card move's result was dropped, so a spent GraphQL budget left cards in Backlog under a green run; each such row now carries a note, the summary counts and names them, a board-queue repo exits non-zero, and a migrated park moves its card to Parked (falling back to Blocked).
- **The two automatic blocker writers read explicit edges only (#1467).** The reconcile-tick repair and the board-pass promotion took edges from the broad pattern, so a sentence like "after #N lands" or "needs #N" raised an unrelated issue, wrote a board field and a comment on it, and stripped its legacy confirmation label. They now read only "blocked by", "depends on" and "depends upon" (unpark Q&A text excluded) plus ledger hand-offs; weak-word mentions are reported on stderr and never written.
- **Filing triage holds a plain money amount for compute as spend (#1511).** A body that names a dollar amount near GPU, instance, cluster, credits, compute, pod or run time (in digits or words, either order) parks with the spend class; amounts inside fenced blocks stay a miss, and an empty `triage.spend_phrases` still disables the check.
- **A merge or empty commit's direct-commits ack survives the upkeep replay (#1470).** It has no patch-id, so the ack now also stores a rewrite-stable `rewrite_key` (author, author date, message, parent count), matched after `--rebase-merges` re-creates the commit, and a clean rewrite carries acks forward in both passes.
- **A follow-up is no longer folded into a duplicate that belongs to a different unit (#1505).** The file-time duplicate search keeps such a match as related and names it in the new issue's body; same-unit duplicates still become a comment.
- **An approved rule is shown as the recommendation, and agreement is explained (#1496).** `unpark.py brief` adds `recommendation` and `pair` (only with the rules part open and the option on offer), `resolve` records `rule_id`, `shown` and `accepted`, the autonomy row adds `misses` and `majority_share` with shown and not-shown agreement apart (level 3 judges the not-shown), `rule_draft.py list --why` and `due` and the doctor explain skipped pairs and per-rule acceptance, and the applier needs a rule's own live comparisons at level 3. Closed gates leave output unchanged.
- **Every park declares a kind, and deny-list filings are hard stops (#1498).** Sigma's own park sites pass an explicit kind and `loop.py record parked` without `--qkind` stays undeclared (one stderr line suggests a kind; it never guesses from free text). One resolver, `qkind.resolve`, reads the newest kind line from a Sigma park comment or a later OWNER/MEMBER/COLLABORATOR declaration and ignores the rest; new `unpark.py declare`, `list --undeclared` and `resolve --qkind`. Filing triage declares the first hard-stop kind for deny-word, spend, secret and class matches and writes `sigma-hardstop: <class>` on the park, which the decision record copies into `hardstop_class`.
- **Docs state that a merged goal branch is deleted on the remote (#1512).** The branch-shape section of the branching model now names the delete after `work.py merge`, the guards that refuse it, and that it cannot be turned off.
- The ledger docs now say that changing `ledger.host_alias` or `ledger.salt_host_token` re-roots this machine (one-time; shared aliases unsupported), and doctor warns when `salt_host_token` is true but `discovery.github.repo` is unset (#1528).
- The Rulebook and upkeep enable steps now say to remove a leftover `.sdlc/state/watch.stop` before starting the watcher, and doctor adds a "no leftover watcher stop file" row only when that file exists and no watcher is running; with the file absent doctor output is unchanged (#1510).
- Switching `discovery.blocker_promotion.mode` from `"smart"` to `"always"` no longer lets the smart skip marker swallow the one-time first-run preview, and the doctor row reads "first repair has not run" until the first always tick runs instead of a zero count; always-from-fresh and smart are unchanged (#1526).
- `gh auth` no longer reads "not logged in" when both `gh auth status` attempts and the REST fallback fail on the network (timeout, connection error); the doctor row now says cannot verify, names the network failure and offers no interactive login (#1527).
- `board_migrate.py --apply` no longer raises when a seeded card is deleted before the final snapshot: it prints a failure, asks the operator to run the migration again and exits 1, and cards with null content are counted as `other` so the board breakdown adds up (#1525).
- The per-unit upkeep record is now written even when the base tip cannot be read (the base tip is stored as null), and a `current` pass whose report has no usable tip refreshes the stored unit tip from the checkout; the landed-unit skip is documented as the only writer on its path (#1532).
- The `archive_after_days` template note, the record-ceiling warning and the write-surface gate no longer tell a reader to run an archive verb that does not exist; they say `archive` is a library function nothing calls automatically (#1508).

## 1.0.14 — 2026-10-15 — the Decision Rulebook and upkeep documented, a 12-hour upkeep window, the blocker priority rule, and the fixes from the third validation pass

- **A person's free-text answer stays out of the applier ask file on a public repository (#1429).** The ask file kept the answer verbatim and the autonomy folder is published with the records; with `repo_visibility` public it now stores `recorded privately`, a private repository is unchanged.
- **A raised blocker's board Priority field is written on the block-recording path too (#1431).** The card lookup ran before the board was resolved (`project item-list None`), so the label was raised and the field never; the board is now resolved first (`sync=False`, one card), and a board that cannot be resolved still leaves label and comment.
- **`work.base` written with the remote's name works in the upkeep pass, the job child, the scheduler and landing (#1425).** `origin/main` already meant `main` in `work.py`; the pass now strips the configured remote the same way, through one `feature_rebase.base_of` helper, instead of fetching the missing ref `origin/origin/main`.
- **A refused board back-fill holds off the next ticks (#1426).** The #1402 counter lived on one source object and every watcher tick builds a new one, so it never fired and each tick still spent the full retry ladder on the refused `gh project` call. A refusal is now remembered in `state/backfill-refused.json`; back-fill makes no project call for `project.backfill_pause_minutes` (default 30) or until a project call is answered.
- **The manual upkeep command runs with no goal when none is given (#1427).** `feature_rebase.py upkeep <sdlc_dir> <unit>` passed the placeholder `-` as the goal, so a finding was filed through a bogus issue `#-` (failed park, label and board steps, a 404 and "from goal `-`" in the duplicate comment). It now passes no goal, which takes the existing explicit-unit filing path.
- **The reconcile tick no longer re-reads the same ten closed issues forever (#1428).** Blockers outside the goal set are read newest first, a ref seen closed is remembered for seven days (a small state file, bounded) and costs no read on later passes, so a newer legacy-labelled or low-ranked blocker is reached; the cap of ten reads per pass is unchanged and the rule off is untouched.
- **Published decisions are never committed onto a `feature/` branch, bound or not (#1424).** The unit-branch guard matched only a branch named in the unit record, and `define open` writes the record with empty `repos`, so a decision made on a freshly opened unit's branch was committed there; any `feature/` branch is now skipped with the existing `unit-branch` reason.
- **Upkeep runs on a 12-hour window instead of a per-unit poll (#1387).** Only a COMPLETED sweep closes the window; a sweep that was killed, cut off before its first unit, found busy or failed to spawn leaves an attempt stamp that holds off a re-trigger for `upkeep.retry_minutes` (default 60, a whole number from 1 to 10080) only. New `upkeep.window_hours` (default 12, a whole number from 1 to 8760; a wrong type closes the block loudly). When the window has passed, the first session start on a machine (a detached sweep, never waited for) or the watcher tick starts ONE bounded sweep of every unit behind its base through the existing per-unit engine; a unit already current costs nothing, a second sweep backs off as a refusal, a refused or parked unit does not stop the rest, and units over the total cap are listed as unreached. A closed window is one small file read, with no git or GitHub call. The session-start line and `/sigma-doctor` show the last sweep and the next window (a warning after two windows without a sweep). The `triggers.*` and `auto.*` keys stay readable for one release but no longer schedule. With `upkeep.enabled` false or no machine opt-in nothing changes.
- **A sweep where every unit failed is an attempt (#1418).** It leaves an attempt stamp and the 12-hour window stays open; a sweep with some units refused or parked still closes it.
- **A future-dated upkeep stamp no longer bypasses the retry window (#1419).** A completed-sweep time dated ahead counts as none and is not carried into an attempt stamp, so a recent attempt still holds off `upkeep.retry_minutes`.
- **A unit branch can be declared the integration base (#1389).** `work.integration_base` names a unit branch that is also `work.base`, so upkeep accepts it instead of refusing it as a unit base; undeclared unit bases are still refused.
- **Opening a unit on a fresh clone works (#1397).** When no local branch has the base's name, the unit branch is cut from the remote-tracking ref instead of failing with a missing source ref; a present local base pushes exactly as before.
- **A unit opened by define-open is no longer closed by the next registry sync (#1396).** The closed rule now also requires that the unit's own `feature/<name>` branch is absent from the remote, so a unit whose record holds no branch but whose branch exists stays open and its record is not rewritten.
- **Hard stops no longer refuse the rebase and landing pass on a unit branch whose record exists only on the default branch (#1393).** With hard stops on, the pass declares its own branch instead of being stopped; undeclared force-pushes still stop.
- **The applier builds its board source once per tick (#1392).** Each ask in a tick built its own source, so a board that refused the project read was retried per ask; the tick now builds one and shares it, and a refused build still fails each ask as before. A closed gate is unchanged.
- **Applier stamps each applied ask when its own gesture lands (#1390).** The applied time was the tick's start for every ask, so in a long tick an owner `no` posted soon after the applied comment could fall outside the veto window and be missed; each ask is now stamped from a clock that advances through the tick, and the veto window runs from that moment. A closed gate is unchanged.
- **The applier no longer acts on an issue that is no longer parked (#1395).** Before applying, it reads the issue's labels; if the parked label is gone it closes the ask as answered with a fixed "already unparked" text and applies nothing, and a failed read leaves the ask pending for the next tick. PROVISIONAL: reusing the answered state for this closed ask is a stand-in until the owner ratifies the encoding. A closed gate is unchanged.
- **Publishing recorded decisions now skips a registered unit branch (#1391).** The unit-branch check read a top-level branch key that real unit records do not have, so it never matched and a decision commit could land on a unit branch; it now reads the branch of every repo in the unit record, and one unreadable record no longer hides the rest.
- **A blocker's priority is never below what it blocks (#1406).** One shared rule raises a blocker to the most urgent thing it blocks, through chains, reports a cycle once and reads an unknown priority as lowest; it is on by default (`discovery.blocker_promotion.mode` now defaults to `always`, an explicit `off` is unchanged). A goal awaiting confirmation is never a blocker, a non-goal blocker is read with one bounded read, and an unwritable or unreadable blocker is reported rather than skipped.
- **A blocker is filed at the blocked goal's priority and never keeps the confirmation label (#1408).** Filing a blocker raises its priority to the blocked goal's (never lowers it; an unreadable goal files as before), the blocker sweeps remove a leftover legacy confirmation label with one comment, and `/sigma-doctor` names an open blocker that still carries the label or has no priority. Mode `off` changes nothing.
- **The blocker rank is applied where the blocked and blocking marks are written (#1407).** `GitHubSource.raise_blocker_priority` raises one blocker to the rank of the goal it was just made to block (raise-only, idempotent, one comment, a read failure reported and skipped); the auto-unpark sweep, blocker resolution (including the precheck park path) and hand-off filing call it in the same batch. `discovery.blocker_promotion.mode: off` makes no call at all.
- **The reconcile tick repairs blockers ranked below what they block (#1410).** `GitHubSource.repair_blocker_ranks` reads open goal and blocked-goal issues through REST, finds every blocking edge (explicit phrases plus the ledger) whose blocker ranks below what it blocks, raises it (raise-only, idempotent, one comment), strips a legacy confirmation label from a blocker and removes the blocking mark from a closed blocker or one that blocks nothing; bounded to 10 writes and 200 edges per tick (provisional), the board field write is skipped when GraphQL budget is low, a cross-repository blocker is reported. The first run prints a dry count; the count and last repair time are kept in `.sdlc/state/blocker-repair.meta.json` and doctor shows one row. `discovery.blocker_promotion.mode: off` makes no call and writes no state.
- **The picker orders by the blocking graph (#1409).** Tier first, then a blocking-labelled goal ahead of its tier peers, on both the label and board paths; a blocker no longer jumps a higher tier, since blockers are raised to the tier they block. With `blocking_priority_override` off the order is unchanged.
- **Blocker priority rule follow-ups (#1415).** A blocking cycle now raises every member that transitively blocks something more urgent, whichever member the walk enters through; filing a blocker and recording a block now also strip the legacy confirmation label from the open blocker (an ownership-held filing no longer keeps it). Membership still follows the existing verdict rules. With the mode off nothing changes.
- **The blocker priority rule is documented and proven end to end (#1411).** Scenario tests drive every worked case through the real writers, repair, filing, picker and reconcile tick; the backlog guide, label model and config template now describe the rule as on by default with an explicit `off`, the one-way ratchet and the one-time rollout line.
- **Claim, define open and the merge review read fall back to REST when GraphQL is refused (#1398).** A label swap whose GraphQL id lookup fails now writes through the existing REST label helpers (no retry ladder), `define` open lists labels through REST when `gh label list` fails and names a rate limit instead of the login when that is the cause, and under `require_review: approval` an unreadable review decision no longer parks a PR that carries a `sigma:approve` comment (provisional; no directive still parks). Healthy GraphQL paths are unchanged.
- **A refused owner lookup names the GraphQL rate limit (#1400).** A bare `unknown owner type` from a `gh project` call now says it is usually the GraphQL rate limit (check `gh api rate_limit`, wait for the reset, retry) and keeps the phrase the doctor matches; the rate-limit wording and other errors are unchanged.
- **Back-fill stops after a refused project call (#1402).** The watcher tick skips the board back-fill once a `gh project` call was refused (including the rate-limited "unknown owner type"), even when the REST counter reads plenty, until a project call is answered; `project.backfill_max_refusals` (default 1) sets the threshold.
- **Ten small wiring fixes (#1403).** A park no longer prints a failed-label line when the REST fallback landed it; failed applier asks are named; the comment scan honours its time budget; landing refusals keep the reason; ack --all acks only the listed commits; work.base accepts a remote-prefixed name; the board dry run shows the apply order; a rebased outcome drops conflict text; the reconcile tick limit scales with the watcher interval.
- **Release on a never-claimed goal is a no-op (#1394).** `GitHubSource.release` now returns False and writes nothing (no label call, no comment, no board move) when the state read succeeded and the issue does not carry the in-progress label; a failed read still falls through to the old release behaviour.
- **`ack --all` acks only what the filed issue listed (#1412).** The listed shas are now written only when the direct-commits issue is actually filed, so a commit landing after the filing is not swept in by a later pass; when every listed sha is gone, `ack --all` says so and writes nothing.
- **Wording after the REST fallbacks (#1413).** The release no-op message now says the goal may never have been claimed, and the review gate docstring and comment describe the unreadable-decision case with an approve directive. The duplicate #1398 line is removed.
- **A goal whose open blocker the board mirror cannot see is held at pick (#1420).** A declared prerequisite missing from the mirror (other owner, no goal label, aged out) is now read live, once per pick; open holds the goal, closed releases it, and an unreadable one still fails open. The precheck reports such a ref as `blocker_unresolved:<n>` instead of skipping it.
- **The pick-time dependency hold docstring describes the live blocker read (#1421).** It no longer says an unknown prerequisite is always claimed: a ref the mirror lacks is read live first, and only an unreadable one is claimed.
- **The precheck no longer reports a closed blocker as unresolved (#1422).** A declared prerequisite the board mirror does not carry is read live when a transport is supplied; a closed one is dropped from `blocker_unresolved`, while an open or unreadable one is still reported.
- **A project board no longer turns a gh login blip into a login instruction (#1399).** When the session-start check cannot confirm the login and the board scope row fails with it, it now retries once and shows the short could-not-confirm step instead of advising a fresh login; scope alone, or any other failing row, behaves as before.
- **Doctor reads repository visibility through a real owner/name (#1401).** The public-records check now uses `discovery.github.repo`, else the slug parsed from the origin remote (host aliases included), instead of a placeholder `gh` could not expand; the old call remains the last fallback.

## 1.0.13 — 2026-10-14 — upkeep and Rulebook hardening from the two stress-test passes: backups, applier, landing, board back-fill, session start

- **Level-4 spot check is a counter, not a hash sample (#1337).** With the applier on and a silent-allowed kind at level 4, every `spot_check_one_in`-th silent-eligible apply per kind and area now comments (a count kept in the autonomy folder, advanced only by applies that landed, never by a dry run); before, a hash of the ask id decided, so a run of silent applies could go unobserved for long. A closed gate is unchanged.
- **A wrong-typed Rulebook value now closes the hard-stop net too (#1345).** `hard_stop_guard.enabled` follows the Rulebook reader's whole-block close, so a mistyped `enabled` on any part no longer leaves the net on; a force push to a registered unit branch was already carved out and is now pinned by tests, and `docs/configuration.md` gains rollout notes on the per-run spend ceiling and `require_review: "approval"` without GraphQL.
- **Public records keep free text out of issue text (#1342).** With `rulebook.records` on and `repo_visibility` public, the unpark answer block records each answer as `recorded privately` (keys and question text stay), the keep-parked marker drops its reason, and the applier tick comment omits the rule option; the doctor rulebook row warns when records are on in a public repository. A closed gate or private visibility is byte-identical to before.
- **Four loop wordings and plumbing fixes (#1346).** The engine-route landing question no longer says verification passed before anything ran (the closed path is unchanged), the managed-block regeneration message no longer asserts an edit, `record-subagent-review` writes a missing resolution file inside the state dir from the resolver (never overwriting one), and `work.py pr --no-tests` with no reason prints usage and exits 2.
- **Imported past decisions agree on a normalised shape, not on free-text labels (#1307).** `decision_import.py` replay compares resume-or-keep, accepted-recommended and option role when a decision has options, falling back to the exact choice otherwise, so a pair can reach level 2; stored imported records gain an enumerated `shape`, and a closed rulebook gate is unchanged.
- **A classifier timeout in `decision_import.py` is no longer stored as unknown (#1310).** A row the model classifier gave no answer for is skipped, so a re-run retries it, and `apply` reports the count (`timed_out`); the per-call limit is `rulebook.import.timeout_s` (default 60, longer allowed), and a timeout now kills the whole process group, not only the direct child. The closed gate is unchanged.
- **Doctor ledger rows say join, not create, when the remote ledger branch is already known (#1318).** Local tracking-ref probe, no network; when it cannot tell, the wording is unchanged.
- **Landing a cross-repository unit is refused before the upkeep pass, any push or any PR (#1339).** `feature_land` now asks the sibling guard up front and, when the other half cannot be measured as landed, refuses with `sibling` and says to land the other half first or each half by hand; the check inside the back half stays, a supplied landed measurement passes as before, and a closed upkeep gate changes nothing.
- **A person's `no` inside the applier window is honoured (#1332).** An owner's exact `no` on a pending ask now cancels it before the applier acts, and a veto `no` is judged by the comment's own time, so a tick that runs late (up to `stale_after_intervals` watch intervals past the veto period) still records it; a `no` made after the period is ignored.
- **A drift-judge timeout is no longer read as a verdict (#1336).** A call that returns no valid kind (timeout, killed) now yields `not_judged` (`call_failed`), is charged the per-call budget against the daily ceiling, and is not stored, so the next park judges again; when the ceiling is reached the doctor rulebook row says `drift judge paused: daily ceiling reached at HH:MM`. Off by default; no change with the drift gate closed.
- **A failed merge call no longer blocks a unit's next landing (#1333).** `land` now settles an earlier landing record through the read-back and refuses only while one is still pending or unconfirmed; a refused or merged-with-warning record passes, stale records are pruned, and the doctor row names the real outcome and the exact `land .sdlc feature/<unit>` gesture and ignores settled records. A closed upkeep gate changes nothing.
- **`decision_import.py` keeps the kind of comment-sourced decisions and one resume answer (#1309).** An unpark answer now takes the kind from the issue's `sigma-qkind` line instead of `unknown`, and `start it again now` is stored as `start it again`; with `rulebook.import.comments` off nothing changes.
- **`decision_import.py` now reads issue comments from the command line (#1308).** With `rulebook.import.comments` on, scan, plan and apply list the goal-labelled issues and pass them to the scan; with it off nothing is fetched.
- **The autonomy readout and the question-list learning line use the category thresholds in force (#1317).** When `.sdlc/categories.json` has a category covering a pair, "level N needs X answers" and "of X answers" now show that category's numbers instead of the global ones; with no categories file every line is unchanged.
- **Landing a unit no longer hands the unit name to upkeep as a goal (#1340).** `feature_land` now passes no goal to the rebase pass, and the pass reports and files a finding with no goal instead of reading `issues/<unit>` or failing on `int()`; with the upkeep gate closed nothing changes.
- **The unpark skill names the rule approve step (#1326).** One sentence now says `rule_draft.py draft` then `rule_draft.py approve <state-folder> <rule id>`, so an agent can find the approval verb.
- **Upkeep shows why a unit-branch base is refused and also refuses remote-qualified spellings (#1327).** The scheduler's drift note and the job record now say "base is a unit branch" instead of reading like an empty base, and `origin/feature/x` or `refs/heads/feature/x` as `work.base` is refused like `feature/x`; default-branch bases are unchanged.
- **`define.py open` no longer commits the unit record on a feature branch (#1325).** When a unit branch is checked out, or the branch cannot be read, the record is still written but left uncommitted and the one-line note names the exact `git add` and `git commit` commands; any other branch commits as before.
- **`/sigma-doctor` warns when a running watcher started from an older build than the installed plugin (#1315).** The watcher now records its plugin version in `state/watch.build` at startup (removed with its pid on exit; the pid and heartbeat formats are unchanged); a live watcher with an older or missing build gets a row naming both builds. After updating the plugin, stop running watchers (`touch .sdlc/state/watch.stop`); the next trigger restarts them on the new build.
- **Upkeep refuses a unit branch as `work.base` (#1311).** The scheduler offers no unit, the older pass ends `no-base`, and the unit pass is skipped, with the reason "base is a unit branch; upkeep needs the default branch"; default-branch bases and a closed gate are unchanged.
- **The upkeep job's unit push keeps a backup and stops after a refusal (#1300).** The rewritten unit now goes through the atomic backup push, so the replaced tip is kept under `refs/sigma/backup/`; and when the older pass reports direct-commits or any other failure outcome, the unit pass is skipped (`unit_pass` reads `skipped`) while the job record keeps the older pass's real outcome. A closed gate changes nothing.
- **`rule_draft.py approve` turns a proposed rule into an approved one (#1306).** An owner runs `approve <sdlc_dir> <rule_id>`; it refuses with one line for an unknown id, an already approved rule, a non-owner, an unresolved login or a closed gate, and stores no login.
- **A failed board card move no longer fails an applied unpark or veto re-park (#1302).** The label swap is the gating step; once it lands the ask is marked applied and the record and comment are written once, with `board_failed` kept on the ask so the failure stays visible.
- **Human decisions and the rulebook file are published by a local commit (#1313).** With `records.publish` on, a person-run unpark or rule approval commits (never pushes) the records folder, the autonomy folder and the rulebook file, and skips goal worktrees, unit branches and a detached HEAD.
- **The applier carries out the rule's option (#1301).** A rule whose option reads as leaving the issue parked now leaves labels and the board card alone and still writes the record and the comment; other options unpark as before.
- **`/sigma-doctor` warns when the configured repo or project owner differs from origin (#1314).** A `discovery.github.repo` or `project.owner` that names another repo than the origin remote gets a row naming both; a match, an unset value or an unreadable origin adds nothing.
- **A person's no after an applied ask reaches the veto through the real verbs (#1189).** Keep-parked and demote on an issue whose ask was applied inside its window record the veto; a failure never blocks the gesture.

- **A vetoed applied ask moves the board card back to Parked (#1201).** On a board-active repo the veto re-park now moves the card as well as the labels.
- **A follow-up matching only a mid-score closed issue is now filed (#1217).** Closed matches fold a follow-up away only at `dedup.closed_duplicate_threshold` (default 0.8); weaker ones are reported as related. Open-issue folding is unchanged.
- **`define.py open` commits the unit record; doctor flags records other machines cannot see (#1312).** Open writes the unit's record when it has none, then commits just that path on the current branch (never a push); if it cannot write or commit, it prints the exact command. A new doctor row warns on an untracked record or a remote `feature/*` branch with no record, and appears only where `.sdlc/features/` exists.
- **A park whose label write fails no longer exits 0 and leaves the issue pickable (#1303).** `loop.py record <N> parked` now falls back to REST label writes when the GraphQL swap fails, and exits non-zero with one clear message if the label still cannot be applied; other transitions are unchanged.
- **Rebase-upkeep findings are no longer armed as goals (#1331).** The parked-conflict and direct-commit issues now file as proposals that need confirmation even when filing triage is on (new default-off `upkeep_finding` keyword on `create_tracked_issue`), the direct-commit issue is closed by a later pass that finds no unacknowledged direct commits, and its body no longer cites a tracker issue number.
- **The unit pass no longer runs after an upkeep engine that refused or did not look (#1330).** `run_engine` now hands over to the unit pass only after `current` or `no-branch`; `disabled`, `busy`, `remote-unreadable`, `no-base`, `no-unit`, `not-adopted` and every failure word report `unit_pass: skipped`, so a unit is no longer force-pushed lease-only with no backup behind a refusal.
- **Unpark records a decision only for a gesture that landed (#1304).** A failed label write no longer writes the decision record, and `keep-parked` or `unpark` on an issue that is not parked is refused with nothing recorded.
- **A verb about one issue no longer back-fills the whole board (#1305).** It cards only its own issue; the watcher tick back-fills up to `project.backfill_cap` (default 20) cards per tick and skips when GraphQL remaining is under `project.backfill_min_graphql` (default 1000).
- **`loop.py verify` refuses before running when `verify.enforce` and `work.enabled` are on and the goal has no acceptance record (#1338).** It exits 4 and names `acceptance.py record`, the same gate `work.py pr` applies, so recording the acceptance after verifying no longer invalidates the evidence and forces a re-run; with enforce off nothing changes.
- **Session start is bounded, cheap, and honest about a failed gh login check (#1334).** The SessionStart hook now has a 20 s timeout; its wizard check no longer runs the ~30 issue-list hygiene scans (those stay in `/sigma-doctor`); a failed `gh auth` check is retried once, cached for 2 minutes, and reported as "could not confirm the gh login" instead of "run gh auth login". Measured with a stubbed runner on a github-mode config: 33 gh/git calls before, 4 after (not measured against a live large repo).
- **Pick-time upkeep now honours `upkeep.units.include` / `exclude` (#1335).** An excluded unit is no longer fast-forwarded (or given a backup ref) when a goal in it is picked; defaults still admit every unit.
- **Doctor flags a config with no `gates.phase_record` key (#1343).** An absent key means the gate is off, so a repo adopted before the gate merges without phase records; the new "phase-record gate configured" row fails until the key is set (any value, including an explicit off, passes) and names the config line to add.
- **Five small upkeep fixes (#1344).** Docs state that outside merges wait for the next watcher tick; the job run directly on Windows refuses with a non-zero exit; a lease refusal in the unit push is recorded as `lease-refused`, not `failed`; `conflicts.resolve: agent` is documented and shown by the doctor as not available yet; a pure fast-forward push keeps the lease but writes no backup ref.
- **Landing a unit branch warns when its tree has upkeep off but the base has it on (#1341).** `verify_merge.py land` reads the remote base's `.sdlc/config.json` read-only and prints one stderr warning naming the disagreement; the route is unchanged, an unreadable base config is silent, and with the gate on in the tree or off in both nothing changes.
- **Each unpark answer takes its own park's kind (#1354).** The comments import now stamps an answer with the nearest preceding `sigma-qkind` line instead of the issue's last one (an answer with none before it keeps the old last-line fallback), and the model classifier keeps a kind the comment declared instead of overwriting it; with `rulebook.import.comments` closed nothing changes.
- **The past-decision comment import also reads parked issues, and a lister failure is reported (#1355).** With `rulebook.import.comments` on, the issue lister adds a second list for the configured parked label (unpark answers live on parked issues); a lister failure prints one line to stderr, stdout unchanged, and gate closed is unchanged.
- **The doctor's upkeep readiness row no longer claims the machine lacks the opt-in (#1316).** The row reads the variable from the doctor's own shell and the watcher records no environment, so a missing variable now reads as a check of this shell only, not as a statement about the watcher.
- **A board migration puts Ready before Parked, a rate limit is named as one, and doctor shows progress (#1319).** `board_migrate.py` now inserts the Ready lane ahead of a Parked lane (appended when there is none); a rate-limited `gh project` call fails with one rate-limit message instead of "unknown owner type"; `doctor.py check` prints one stderr line per slow check group only on a tty or with `--progress`, so captured output is unchanged. Provisional; park and tick progress lines are a follow-up.
- **The classifier timeout kill no longer hangs on the pipe read or ignores an interrupt (#1353).** After the group kill the read of the child's output now has its own 5-second bound and gives up on the partial output, and an interrupt during the call kills the child's process group before re-raising. The pidfile test polls for the file instead of failing on a slow machine.
- **Doctor reads the real repository visibility for the public-records warning (#1363).** With records on, `/sigma-doctor features` makes one REST read of the repository's private flag and warns when it is public whatever `repo_visibility` says; without gh or on a failed read it uses the configured value and says so. The session wizard makes no extra read.
- **The level-4 spot-check slot is decided per action at apply time (#1361).** The applier used to plan comment-versus-silent for the whole batch as if every apply landed, so after a mid-batch failure with `spot_check_one_in` 2 several silent applies could run in a row; each action now reads the landed count just before it applies, and a failed apply no longer shifts the later slots. A dry run still shows the planned slots and a closed gate is unchanged.

## 1.0.12 — 2026-10-13 — rulebook categories file, cold-start visibility, past-decision import, upkeep trigger and session report, drift judge in shadow, hard-stop net, and the confirmation-queue migration

- **The upkeep scheduler's drift measurement works with its default git runner (#1258).** The scheduler handed the drift module a runner that prepended `git` to arguments that already began with `git`, so every real measurement failed as `git-failed` and the watch tick, session-start report and merge trigger never found a due unit; one adapter now reconciles the two conventions. The pick-time pass is unchanged.
- **The model classifier no longer caches an error or timeout as `unknown` (#1249).** A call that raises or returns nothing is neither cached nor stored, so the next `apply` retries that candidate; an answer outside the closed lists is still cached as `unknown`, and a failed call still counts the per-call ceiling.
- **`decision_import.py` can classify past decisions with a small model, opt-in (#1243).** `decision_rubric.import.classifier` is `"table"` (default, unchanged) or `"model"`; with `"model"` and the records gate open, `apply` sends only the clipped question and option labels, accepts only a listed kind and area (else `unknown`), caches by fingerprint so a second run calls nothing, and stops at a per-call and per-run dollar ceiling (PROVISIONAL, reported). `plan` sends nothing.
- **`decision_import.py --project-dir` takes a dash-leading folder name in two words (#1244).** `--project-dir -srv-demo-work-repo` now works for `scan`, `plan` and `apply`, which build one shared option set; a real option after the flag is still not swallowed.
- **The guard's park now logs the original action text (#1240).** Where the guard has the action text (a verdict from `guard()`), its `permission_use` entry carries that exact text instead of the verdict reason, so a refused-then-parked action no longer blocks `record done`; a park with no action text (the issue-write chokepoint) keeps the reason text. A closed gate changes nothing.
- **`promote.py migrate-confirmation` no longer reports a correct run as FAILED when the label list read lags (#1268).** Each of the run's own issues the verify list still shows as labelled is read directly by number, and counts as remaining only if that read still shows the retired label; one extra read per apparently remaining issue, no extra writes. PROVISIONAL: issues outside the slice are listed as read, not re-read.
- **Imported records of kind `unknown` no longer feed rule drafts (#1239).** `rule_draft` skips records marked `how: imported` whose kind is `unknown` when collecting evidence, so many agreeing keyword-table misses give no draft and no list row; live `unknown` records and imported records of a known kind behave as before, and a closed gate changes nothing.
- **Wiring follow-ups from review notes (#1238).** `promote.py migrate-confirmation --unit` matches case-insensitively; an empty `--unit` or a `--unit`/`--only` slice that matches no issue exits non-zero with one clear line; `--limit` counts only open issues. A transient drift-judge failure (call failed, lock or spend file unusable, ceiling reached) is no longer stored as a final verdict, so the next park judges again, and the drift store write takes the decision store's file lock. The `decision_import.py` scan checks its deadline after a set number of bytes as well as lines (`CHECK_BYTES`, PROVISIONAL 8 MB).
- **`decision_import.py plan` and `apply` turn past decisions into records (#1226).** `plan` prints per kind and area what would be written, how many are unknown and the level the evidence alone reaches (capped at 2); `apply` writes records marked `how: imported` (source fingerprint, original time) through the real store plus replayed watch comparisons, idempotently, and writes nothing while the records part is closed. Imported evidence counts toward levels 1 and 2 and `rule_draft` (a draft is still PROPOSED), never above level 2; the doctor and cold-start lines show `N imported`; `sigma init` mentions the import as an optional, never-run task when records are enabled. PROVISIONAL, owner to ratify: kinds come from a fixed keyword table in code, not a model, so the classifier opt-in, budget ceilings and cache of the issue text are not built; public visibility writes no comparisons.
- **`loop.py log` takes two new kinds, `action` and `permission_use`, each with a required `--action` text (#1228).** The record-done hard-stop net had no entry carrying text to read; now an `action` entry that classifies to a hard-stop class refuses `record done` (exit 4) until a `permission_use` or park entry carries the same text, and the guard's own park writes one `permission_use` entry. A closed `decision_rubric.hard_stops.enabled` gate changes nothing. PROVISIONAL: entries exist only with `action_log.enabled` true; with it off the net reads nothing.
- **The drift judge now runs in shadow mode when a question is parked with a declared kind (#1229, story #988).** With `decision_rubric.drift.enabled` true it reads the reference documents named in `decision_rubric.drift.documents` (repository-relative, escapes and unreadable paths reported as unreachable, text clipped), judges once per issue and park through the existing call path and ceilings, and stores the verdict (conflict with quote and document, consistent, or not judged with a typed reason) in the state folder. It never parks, blocks or changes a label; a hard-stop or undeclared kind is not judged; a closed gate changes nothing. PROVISIONAL, owner to ratify: the verdict lives in a state file, not a comment.
- **`promote.py migrate-confirmation` is safe to run over a whole board in slices (#1227).** New `--unit NAME`, `--only N,N` and `--limit K` filters; the dry run prints each issue's destination, population and reason; issue writes are paced (`ai_filed.migration.max_writes_per_minute`, default 30) and a rate-limit answer pauses and resumes; an apply re-reads the labelled set and fails if an open issue in the slice still carries the retired label; an already-parked issue stays parked. The doctor row now counts the leftovers and names the verb. PROVISIONAL: pacing counts a swap, a comment and a card move as one write each.
- **`decision_import.py scan` now reads long transcripts: streaming, pre-filtered, time-budgeted, with `--project-dir` (#1237).** The per-file cap defaults to 500000000 bytes and the real bound is a total `time_budget_s` (default 60) under `decision_rubric.import`; a file the budget cuts short is reported as partially read with its size and the line it reached, lines are tested by substring before any JSON is parsed, the report totals bytes read, and a repeatable `--project-dir` scans extra project folders (an unknown one is reported). PROVISIONAL, owner to ratify: the 500 MB ceiling and the 60 s budget.
- **`decision_import.py scan` reports past decisions for a later import (#1223).** Read-only: pairs a Claude Code `AskUserQuestion` call with its answer from transcript files (no other tool result is read) and, when `decision_rubric.import.comments` is true, reads `sigma:unpark-qa` answers and decision-record lines from issue comments. Stable ids, reversals marked `superseded`, malformed input skipped and counted, file-size and candidate caps from `decision_rubric.import` with a plain refusal. Writes nothing; no gate involved.
- **Merge now triggers one upkeep due-check on the machine that merged (#1225).** After `work.py merge` lands a PR, and only with `upkeep.enabled` true, it runs `feature_upkeep_sched.py decision` once through `run_with_timeout.py`; a unit that is due starts the existing upkeep job under the same cooldown, claim and lock. A closed gate makes no call and no output; a failure or timeout is logged once and never changes the merge result. The doctor shows `upkeep last start` (trigger, tick or pick). PROVISIONAL, owner to ratify: no pick path calls the scheduler today, so `pick` is reserved; a trigger pass does not rewrite the tick status file.
- **The exported tree's skip-pin test no longer fails on the export's own explicit skips (#1203, #1220).** In the exported tree `tests/test_no_skip_hooks.py` failed deterministically: the pin walked the whole definition of each guard test, so the `mark.skip(...)` CALL of the exact decorator the export transform writes into five named tests counted as a skip call in the body. The pin now judges decorators and bodies separately, matches the transform's decorator by EXACT text (reason included), and a new test runs the pin's own check on the real transform's output for each of the five tests (green) and on variations (a different reason, an extra marker, a skip call or early return in the body, a `pytestmark`: each red). Not covered, and now stated: the owner's pattern-file scan (`leak_refs.py --patterns`) does not join a name wrapped over a line break; only the derived source-name scan does (two wrapped forms passed the pattern-file scan in the end-to-end verification). Measured: exported tree red before (1 failed), green after; the full exported suite is re-run separately by the verifier.
- **PR `test` job timeout 45 -> 60 minutes (CI).** The macOS `test` job took 29-35 minutes on earlier PR runs and was cancelled at the 45-minute cap on #1212's PR run (annotation: "exceeded the maximum execution time of 45m0s"; started 06:52 UTC, cancelled 07:37 UTC), a docs-only change, so the margin was gone. The owner chose 60 on 2026-10-12; the new figure is a margin, not a measurement. Trimming the PR subset (a parallel job for the slowest files) is the follow-up, not done here. The `full` job's 90-minute cap is unchanged.
- **Measured results, published (docs only).** `docs/launch/evidence/measured-results.md` gathers the
  maintainers' measurements (live-drill cost, validation sessions, reviews, mutation, flake census, gates,
  coverage, drills, nightly CI, export rehearsal) with each figure's source and date, and says "not measured"
  where it was not. It makes no comparative claim. The README links it and gains a "Known limits" list:
  unattended use runs on your host account, and budgets are per clone, so supervisor relaunches each start with
  a fresh budget.
- **Local verification evidence for the public export at `de188d1fb5dd`.** Added
  `docs/launch/evidence/export-verify-de188d1fb5dd.md`: two identical VERIFIED builds (1235 files), the
  plain whole-suite pytest run on the exported tree on macOS (1 failed, 16870 passed, 30 skipped), and the
  planted-fake controls. It finds one failing guard test in the export and a line-wrap gap in the private
  pattern scan; the failing guard test was fixed in #1236 and a second section records the re-run on
  `ba7a420a` (16682 passed, 0 failed, with the `test` job's own steps). The pattern-scan gap is not fixed here.
  Local runs only, not GitHub CI.

- **The ledger retries a failed push on the next tick, not on the next entry (#1210).**
  - **What was wrong.** When a push failed, `sync.py publish` kept the commit locally. Every later tick then
    reported `nothing to publish` until some new entry was staged, so the "will retry next tick" it had
    printed was false.
  - **What publish does now.** For an actor that owns an entries file, it pushes whenever the ledger
    branch is ahead of its remote-tracking ref. It also pushes when that ref is absent on a configured
    remote name, which happens when the first push after a bootstrap failed.
  - **What has not changed.**
    - A caught-up clone still makes no push or fetch.
    - A clone with no remote configured, or with a path or URL where a remote name belongs, keeps today's
      behaviour: it pushes only alongside a new commit, because no tracking ref can ever exist there.
  - **The cost.** While a commit stays stranded, every idle tick now does work it previously skipped while
    falsely reporting `nothing to publish`:
    - against a server that rejects every push: 5 pushes, 4 fetches and 4 rebases (the bound
      `PUSH_ATTEMPTS` already set for ticks with a new entry);
    - against an unreachable remote: 1 push attempt and 1 fetch attempt;
    - against a remote branch that was deleted and recreated with unrelated history: 1 push and 1 fetch,
      and the tick now says `publish refused: … shares no history`.
  - **Other fixes in this change.**
    - A failing `git add` or `git diff --cached` (a held `index.lock`, say) now returns `publish deferred: …`
      instead of raising, as the commit already did (#2449).
    - Two existing hints are now true: `/sigma-doctor`'s "`sync.py publish .sdlc` publishes now", and a
      hand-typed write's "`sync.py publish .sdlc` will retry it".

## 1.0.11 — 2026-10-12 — an owner's "no" reply vetoes an auto-applied answer, copyright holder, and a Python 3.10 diagnose fix

- **The public export's own suite and gates pass on the exported tree (#1203).** The default export left out `.sdlc/` but shipped tests and inventories that read it: 8 tests failed on every platform, `write_surface.py check` and `exposure_scan.py tracked` exited 1, so a fresh public repository's CI would have been red on day one. The builder now rewrites a few exported files itself (`export_transforms`, listed in the report under `export.transforms`): stale rows leave the shipped write-surface inventory and exposure allowlist, and five tests carry an explicit skip in the exported copy only. The source holds no switch (a first design with a marker file was forgeable and was dropped): the skip set is pinned in `tests/test_export_transforms.py`, and a hand-written marker changes nothing in the private tree. Measured and not measured: see the PR body.
- **Copyright holder is now "Agrim Intelligence Private Limited" everywhere it is stated.** `LICENSE`, `NOTICE`, and the `author` / `owner` of `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json` (were an individual's name). `docs/launch/evidence/legal.md` records, as owner decisions made without counsel on 2026-10-12, the entity name and that there are no obligations to predecessor users; the name cells stay OPEN (owner chose to hold the name until the German register entry is looked at). No version bump. Not legal advice; nothing measured beyond the tests.
- **The documented `invocation.py diagnose .sdlc <id>` gesture no longer says `refused` on Python 3.10 and 3.11.** `run_adapter` built the adapter's private run directory from the sdlc path as given, so a relative `.sdlc` gave a relative executable path, which Python 3.10/3.11 resolve against the child's `cwd` (the run directory) and not the caller's, so the spawn failed `FileNotFoundError`; 3.12 and later resolve it from the caller. The path is now absolute. Cause found by reproducing in `python:3.10` and `python:3.11`; `tests/test_invocation.py::test_the_documented_install_and_diagnose_gesture_works_as_written` was red there before and is green after. The same code path serves `authorize` with a relative sdlc dir.
- **The public export no longer names the private source repository, and a build is REJECTED if it does (#1204).** The rehearsal found the source's own `OWNER/NAME` in 13 exported files that no scan flagged: the patterns file never held that name. `tools/build_public_tree.py` now derives the names (`--repo` and every remote's GitHub URL) and adds the builder rule `source-slug` over every exported file and path. Closed: the name in any file type or path, in any case; the bare repo name when it is 8+ characters with a `-`, `_`, `.` or digit; any non-alphanumeric neighbour (`x-NAME`, `NAME_y`, `a.OWNER/NAME`); text is read after NFKC with zero-width, soft-hyphen and bidi characters dropped and `\/`, `%2F`, `&#47;`, `&#x2F;`, `&sol;`, `\x2f`, `\u002f` read as `/`; a line break between two adjacent lines is dropped, next to the slash or inside the name; a remote of another owner than `public_repo`'s is refused `source-slug-owner-mismatch` unless `--repo` names the source; a build with no derivable name is refused `source-slug-unknown`; the output belt redacts the name; a report directory inside `OUT`/`OUT.rejected` is refused `report-dir-inside-out`; a build that derived the name from a remote alone prints a WARNING (a real export always passes `--repo`, now in the documented gesture). A sibling or team handle that is legitimate is dispositioned by exact reviewed line (4 shipped files today: the maintainers team handle and three evidence files naming throwaway drill repositories); a path hit has no line and cannot be. The 13 files were fixed in the source (examples say `OWNER/NAME`; fixtures use a made-up name); `docs/launch/evidence/330-control.md` and `flake-*.json` are REDACTED EVIDENCE (a pasted assertion output and a run URL that no longer resolves). Not covered: base64, hex or double-encoded text, look-alike letters from other scripts, a name spread over three or more lines, a short plain bare repo name, a fork under the same owner when `--repo` is left out, a `--sdlc include` tree (its `.sdlc` records name the source and the build is REJECTED until dispositioned). Measured: the new tests are red on the old builder (22 of 27 cases at the previous head), mutation of the join is red, the builder on the fixed tree is VERIFIED with 0 hits. Also pinned: no conftest hook can skip by rule and no skip/xfail marker may sit on the guard tests (`tests/test_no_skip_hooks.py`; ordinary skips are not pinned). Not measured: a real go-public build with `pr-merged` review (every export built so far used `--review-level clean`).
- **Public-snapshot rehearsal evidence for f92a7528a26b (#397, #473, part of #471).** `docs/launch/evidence/rehearsal-f92a7528a26b.md` records a local-only rehearsal: two builds give the identical tree and export commit, all scans 0, a planted fake reference is REJECTED, the export pushed as one orphan commit to a private throwaway, the install control GREEN. It also records that the default export's own suite is red on all five Docker legs and on macOS (#1203) and that the export names the private source slug in 13 files (#1204). GitHub CI legs: not run, spend decision pending.

- **An owner's `no` comment now vetoes an applied answer (#1200, story #988).** The applier tick reads the comments of each applied ask inside its window (`read_vetoes`); a comment newer than the apply, from an `autonomy.owners` login, that is `no` (trimmed, any case) calls `applier.answer`, and the veto action now also parks the issue again with a comment, once. Reads nothing while the applier gate is closed or in a dry run. PROVISIONAL, owner to ratify: the veto period is the ask's own window. Cost: up to 3 requests per applied ask per tick, bounded by the window and the daily cap.
- **#300 live run with the marketplace-installed plugin: demo goal and a filed goal both `done` in one `/sigma-loop` session.** Third dated subsection of `docs/launch/evidence/live-onboarding-2026-10-12.md` and `docs/onboarding-control.md`. The owner's normal profile and auth, no `--plugin-dir`; the session's init event reported plugin `sigmaloop` 1.0.9 (the installed copy, not main at 1.0.10). Measured: init 25 s ($0.148), loop 240 s ($1.873 host-reported list-price estimate), 14 captured `phase_report.py end` lines (sum $0.76), two `record done` refusals recovered by the model (#1206, #1199). The second goal was hand-written. Not measured: the marketplace install from empty in a live session, 1.0.10, Linux, Codex, Cursor, outside people. Evidence only, no product change.

- **Legal fact sheet records owner decisions made without counsel (#343, D13).** On 2026-10-12 the owner decided to go public
  without counsel; `docs/launch/evidence/legal.md` now reads `OWNER DECISION (no counsel consulted)` with the accepted risk where
  the owner's statements decide a cell, and `OPEN:` where they do not (legal entity name for the copyright line, the public name after a first-pass register search that found a live word mark in Germany, Codex and Cursor catalogue terms, vendor brand guidelines). `NOTICE` is now a plain third-party-notices file,
  `README.md` carries a non-affiliation paragraph, and `CONTRIBUTING.md` says a DCO sign-off will be required when contributions
  open. Not legal advice.
- **The commit gate stops refusing non-literal `credential-assignment` values (#961).** `work.py commit`
  refused any 4+ character run after a credential-named key, so a call, an env read, a type annotation
  and a boolean parked green goals at their last step. A gate-only filter (`scrub.commit_secret_findings`)
  now passes a whole boolean or null word and Sigma's own redaction markers in every file, and an
  expression or (after `:`) a closed-set type name only in a code file (`scrub.COMMIT_CODE_SUFFIXES`,
  case-sensitive). A quoted value, a bare identifier or number as the whole value, an assigning `=`
  later on the line that starts a quoted literal, any secret-like token after the separator, and a key
  line whose statement continues onto the next line (an opener or a quoted literal left open, or a
  trailing `\`, `=`, `??`, `||`, `&&`, `?:`, `+`, `or`, `and` and the like, so a formatter's wrapped
  call keeps the old refusal) stay refused; only the first word is judged as the value, so a number can
  still pass as a code file's annotated default. Measured, of the lines this filter would otherwise
  pass, the continuation check refuses 0 of 30 in this repository, 2 of 91 in the Python standard
  library and 23 of 1,442 in a `site-packages` tree, each refused before #961 too. A quoted literal is
  reported once, not twice. The rule table, `scrub()`, the publish leak gate and the exposure
  scanner are unchanged. New `work.allow_secret_content`: an OPERATOR ruling that clears only
  `credential-assignment`, by exact line hash (`work.py line-hash <file> <line>` prints it without
  showing the line) or by exact path (wide: it clears future literals in that file too); a reason is
  required and a malformed entry is ignored and counted. The refusal tells the agent to remove the
  literal or park, names the allowlist as the operator's, and no longer offers a hand commit.
  Residual misses, tracked by #978 and listed in the threat model: a quoted literal with no
  secret-like token that no assigning `=` starts passes at any length anywhere later on the line once
  the first word is a code file's expression or type name (a call argument, a getenv default, a
  fallback after `||`, `??` or `or`, a `+` concatenation, a ternary branch, a block default) and, in
  any file, a boolean or null word or a redaction marker (the check judges tokens, so the 24-character
  bound holds only for an unbroken letter/digit run; measured, 2,000 draws per shape: passphrases joined
  by spaces or hyphens passed 100%, no-digit values with symbols 99.8%); so do a number or identifier
  as a code file's annotated default, and a literal on the next line under a key line that looks
  complete (a leading `?:`, `or` or `.` on that next line, a key line ending in `.`, or a YAML
  continuation: one line cannot show it, and the old gate refused such a key line only because it
  refused every value).

## 1.0.10 — 2026-10-12 — rubric wiring, upkeep deletion guard fix, applier asks
- **Live onboarding drills recorded (#300, #301).** `docs/launch/evidence/live-onboarding-2026-10-12.md` and a dated section of `docs/onboarding-control.md`: one headless live-model run on Claude Code in local-goals mode and one in github mode against a throwaway private repository, with host-reported tokens and cost, all 20 `phase_report.py end` lines and every refusal verbatim. Evidence only, no product change. Measured: filed goal `done` locally in 286 s ($2.018), github goal PR merged and reconciled (440 s loop, $1.480, merge to reconcile 12 s); the demo goal FAILED (#1196) and the github goal needed three refusals and `--no-tests` (#1197). Not measured: Linux, Codex, Cursor, marketplace install in a live session, outside people.
- **Live onboarding drills recorded (#300, #301).** `docs/launch/evidence/live-onboarding-2026-10-12.md` and a dated section of `docs/onboarding-control.md`: one headless live-model run on Claude Code in local-goals mode and one in github mode against a throwaway private repository, with host-reported tokens and cost, all 20 `phase_report.py end` lines and every refusal verbatim. Evidence only, no product change. Measured: filed goal `done` locally in 286 s ($2.018), github goal PR merged and reconciled (440 s loop, $1.480, merge to reconcile 12 s); the demo goal FAILED (#1196) and the github goal needed three refusals and `--no-tests` (#1197). A clean-repo re-run (Makefile `test: true`, origin/main cc92a834) took the demo goal to `done` in 160 s ($1.244 host-reported list-price estimate) after two phase-order refusals (#1199). Not measured: Linux, Codex, Cursor, marketplace install in a live session, outside people, a continuous install-to-done clock.
- **Live onboarding drills recorded (#300, #301).** `docs/launch/evidence/live-onboarding-2026-10-12.md` and a dated section of `docs/onboarding-control.md`: one headless live-model run on Claude Code in local-goals mode and one in github mode against a throwaway private repository, with host-reported tokens and cost, all 28 `phase_report.py end` lines (12 local, 8 github, 8 vanilla re-run) and every refusal verbatim. Evidence only, no product change. Measured: filed goal `done` locally in 286 s ($2.018), github goal PR merged and reconciled (440 s loop, $1.480, merge to reconcile 12 s); the demo goal FAILED (#1196) and the github goal needed four refusal messages (three groups) and `--no-tests` (#1197). A clean-repo re-run (Makefile `test: true`, origin/main cc92a834) took the demo goal to `done` in 160 s ($1.244 host-reported list-price estimate) after two phase-order refusals (#1199). Not measured: Linux, Codex, Cursor, marketplace install in a live session, outside people, a continuous install-to-done clock.

- **The applier's ask is opened at park and closed at answer (#1178, story #988).** `applier.py` gains `open_for_park` (a park with a declared, non-hard-stop kind opens one ask when an approved rule matches the issue's `area:*` label, else nothing; a pending ask is reused) and `close_for_issue` (unpark, keep-parked and promote answer the issue's pending asks; the applier's own promote does not). Called from `loop.py record parked`, the park at filing, `unpark.py` and `promote.py`, each guarded so a failure never blocks the gesture. Gates closed changes nothing. PROVISIONAL, owner to ratify: a learned pattern is served by the same approved-rule match the tick uses.
- **Every human decision record now also leaves a watch comparison (#1177, story #988).** `decision_record.write_all` and the ack record in `handoff.py` compute what a sealed rule (else the unique majority of earlier human answers of the same kind and area, once `l1_min_answers` exist) would have chosen BEFORE the append, and after a successful append write it next to the actual choice with `autonomy.record_watch`, so the autonomy ladder climbs through real gestures. Inert with autonomy or records closed, for autonomous records, and on public record visibility (PROVISIONAL, mirrors the draft refusal); errors are swallowed. Cost per gesture: one rule match, one `list_by` read and one small file.
- **PR `test` job runs the plain onboarding control against a wall-time budget and the planted regressions (#879, slice 5 of #809).** `evals/regression/budget.py` runs `python3 tools/onboarding_control.py` and fails when it exceeds `ceiling_seconds` in `evals/regression/budget.json` (set from measured CI job logs, see the PR); `pytest tests/test_onboarding_control.py -k planted` applies `drop-review-recording` and `skip-plan-review` to a scratch copy and demands the plain gesture exit 1 naming the review chain and the plan-review probe. Adds minutes to the required `test` job (measured on the PR).

- **Model routing reads the work (#390, part of #389).** A body-only opus/fable signal needs two hits (title still wins on one; matches inside backtick spans or path-shaped tokens never count), `predict.py why` prints `hits=`, `loop.py lane-tier` re-resolves the ceiling from Research's lane (can raise to opus; an escalated floor still wins), and the timing-store probe strips the coverage environment. The predecessor measured 61 of 167 open goals routed to opus, 58 of them from one body word, 16 of 21 hand-read cases false positives (the predecessor's measurements, not Sigma's); Not measured: Sigma's own boards. Ships off (`model_selection: auto` opt-in).
- **First-run docs: attended review fallback, `--verify N:ID` second invocation, trivial-goal path, init artifacts, silent `reconcile-merges` (#612, part 1).** README sections plus two instruction lines; the claims are held to code (`WAIVABLE`, `RUNTIME_IGNORES`, the `config.json.tmpl` `require_review` default, `verify_detect._ID_HEX`) by `tests/test_first_run_docs.py`, each control seen red against a mutated README. Measured: full verify 16477 passed. Not measured: the live Claude Code auto-mode refusal (taken from the first-run report, not reproduced), and the host-refusal park is instruction text, not code.
- **test(slack): give the single-instance race test a settle bound that survives a loaded runner.** CI (PR #1186, macOS 3.12) failed `assert len(alive) == 1` with all 15 racers still alive at the 8 s settle deadline, every one later printing `LOSER: ... already running`; the exactly-one-`WON` assertion had passed, so this was a too-tight bound, not a double win or a hang. The settle bound is now 60 s (still returns early once one racer is left), the winner sleeps 120 s and is killed after the snapshot. No product code changed. Sensitivity: with `acquire_single_instance` made to always return `(True, "")` in a scratch edit the test goes red (15 winners, DOUBLE WIN); with the lock, 224 passed in the file and 14 of 14 targeted runs passed, 8 of them under 6 busy loops. Not measured: the loaded-runner timing itself.
- **The export builder checks the issue-chooser repository link against the public repository (#606).** The goal asked for a build-time substitution, but `.github/ISSUE_TEMPLATE/config.yml` has named the public repository since #699, so there is nothing to substitute and this repository's own chooser keeps working; what was missing was a guard. `tools/build_public_tree.py` now reads every `github.com/<owner>/<repo>` in the exported config (https and ssh forms, `.git` and trailing punctuation stripped, case-insensitive) and compares it with `public_repo` in `docs/launch/definition.json`; any other repository, a config naming none, or an unreadable definition REJECTS the export (`scans.issue_template_slug`, rules `issue-template-slug-mismatch`/`-unverifiable`). Measured: a real export of this repository at its current main (scratch clone, builder from this change) is VERIFIED with `equal`; the same export with the link pointed at another repository is REJECTED on line 4; six deliberate breaks of the check (comparison, no-link rule, case fold, `.git` strip, trailing-dot strip, host anchor) each turned `pytest tests/test_build_public_tree.py -k issue_template` red. Not proven: a bare `owner/repo` with no `github.com`, other hosts (`api.github.com`, `raw.githubusercontent.com`), and the sibling templates in that directory, are not read; `orgs/`, `sponsors/` links in the file would be rejected until the check is edited; nothing was run against the real public repository (it does not exist yet).
- **Two standing rules from #954's landing retrospective (AGENTS.md).** "Run the control" now asks for one control per clause of a compound or ordered guard, and a test a docstring cites as a guard's proof must go red under that clause's own mutant (#954's order clause passed every test under a set-membership mutant; #1156). A new rule says nothing Sigma writes in the root checkout may block a person's ordinary git operation, and a design that writes there names the clearing gesture and its actor (#954, #1161). Docs only.
- **Two standing rules from #954's retrospective: one control per guard clause, and no root-checkout files that make git or a start refuse (#1156, #1161).** AGENTS.md's "Run the control" now says to break each clause of a guard on its own, and to weaken an ordering to plain membership, seeing a test go red each time. #954's order check passed all 24 tests in `tests/test_dirty_root_registry.py` when weakened that way. A new rule says Sigma does not leave files in the root checkout that make `git pull`, `git checkout` or `work.py start` refuse. A design that cannot avoid writing there names the one command that clears it and who runs it; the untracked-copies case is still open (#1161). Docs only.
- **Linux nightly: the three failing `bounded_run` tests were test bugs, not a product defect (#1153).** Two timeout tests used an injected clock that jumped to 1e6 once the child wrote its pidfile; under load the deadline was then computed from 1e6, so the run waited the full 60 s and reported `ok` (CI: `seconds=60.0037`). The clock's first call now always reads 0. The flood-memory test read `ru_maxrss`, which on Linux inherits the parent's high-water RSS (Docker, 900 MiB parent: child `ru_maxrss` 932568 KiB, `VmHWM` 8312 kB); on Linux it now reads `VmHWM`. No product code changed. Measured in Docker (Ubuntu 24.04 aarch64, `pytest tests -q -n 4`): origin/main 5 failed including these three; this branch 3 failed, none a `bounded_run` test. Not proven: a real `full` run, x86_64, Python 3.11; two `without_flock` tests and one slack race test failed in Docker and were not investigated.
- **Shared-paths registry vets the #1152 `sync.py` writers, so main is green again (refs #1142, #1152).** `_save_uncommitted` (names TEAM.md only to skip it; copies aside under `.sdlc/state/orphaned/`), `_reroot_guarded` (the existing knowledge flock) and `bootstrap` (aside dirs, own entries file, TEAM.md via the already vetted `_write_team`) are added to the `vetted_writers` of `.sdlc/ledger/TEAM.md` and `.sdlc/state` in `tests/fixtures/predecessor_written_paths.json`, each with its reason. None writes a file the predecessor writes. The reasons are static reading, not a run against a predecessor install (UNMEASURED); the `.sdlc/ledger/TEAM.md` entry stays `compatible: false` (#515).
- **The session-claim race tests wait 60 s, not 10 s, for their two forked writers.** Main's CI run 4857377 failed `test_session_claim_race_loses_a_goal_without_a_lock_guard` on a loaded macOS runner because two fork processes (each re-loading `loop.py`) had not finished in 10 s; `_session_locked` is a blocking flock around a short critical section released in `finally`, with no deadlock found by reading. Both this test and `test_session_claim_and_release_race_do_not_lose_updates` keep the assertion that both writers finished. Sensitivity unchanged: with the `_session_locked` call removed from `_session_claim`, both go red 3 of 3 runs. Not measured: the loaded-runner timing itself (UNMEASURED; inferred from the CI failure).
- **Upkeep no longer refuses a base deletion of a path the branch only inherited (#1146).** `dropped_paths` now excuses a deleted path the branch carries byte-identical to the fork point (two extra guarded reads, only when the replay deletes something); a path the branch added or changed is still refused, and any failure to find one fork point keeps the old refusal. PROVISIONAL, owner to ratify: a base revert of work already on the base at the fork point now reads as inherited and is not refused (documented limit, `docs/branching-model.md` §3b and §15).
- **Rule drafting says plainly it cannot draft on public record visibility (#1149).** Public visibility drops the free-text choice, so `rule_draft.py` found no option and yielded nothing silently. `draft` now refuses with a clear reason and `list` prints it on stderr and exits 1. PROVISIONAL, owner to ratify: refuse rather than add a surviving field to the record writers. Gates closed is unchanged.
- **Draft a rule from a learned pattern, tested on held-back decisions (#1138, story #988).** New `rule_draft.py` with `list` (kind and area pairs at autonomy level 2 or more whose answers agree at a threshold) and `draft` (majority option, source record ids linked, handed to `rulebook.propose` so it stays proposed; approval is the owner's, unchanged). Answers are split deterministically by a hash of the record id; the draft must also hold on the held-out part, else no draft (fail closed, also on a small set, a tie, a hard-stop kind or an existing rule). Inert unless the rulebook, autonomy and records parts are all enabled. PROVISIONAL thresholds: `rulebook.draft.min_agreement` 0.90, `min_held_out` 5.
- **A folded finding still records its triage verdict (#1139).** Triage already runs before the duplicate search; when the search folds a finding into an open issue, a parked verdict (force-push proposal, secret-shaped body) is now also commented on that issue. A clean finding folds with the same bytes as before.
- **Spend phrases park an AI-filed issue at filing (#1135, story #988).** `ai_filed.triage.spend_phrases` (default: paid run, rent a GPU, new vendor, paid API, cloud GPU benchmark; empty list disables) parks a filing whose prose names a paid action, reason naming the phrase. A first-line recorded-permission marker still parks as before. Triage off is unchanged.
- **A park at filing keeps its triage reason and declared question kind (#1136, story #988).** A deny-list or per-goal-cap park of a just-created issue is now recognised as Sigma's own (scoped to that one issue), so the hard-stop guard no longer replaces its text with a generic reason; the issue is still created with its priority label before it is parked. A foreign issue stays guarded (tested).
- **Init no longer creates the retired `sdlc:needs-confirmation` label; the CHANGELOG union leaves no blank line between bullets (#1140).** An existing label stays inert, and a queued proposal creates it on demand when filed (`GitHubSource.ensure_label`). `_union_headed` now drops a side's trailing blank lines when the other side's bullets follow (`_union_diff3` stays byte-identical).
- **Hard-stop patterns for three shapes, and the unknown-command decision (#1137).** `hard_stop.py` now stops a service deploy (an `up` or `redeploy` of the hosting CLI) and a cloud IAM attach of an administrator policy under `production_change`, and reading the shared cloud credentials file under `access_secrets`. PROVISIONAL, owner to ratify: an unknown command Sigma cannot undo (`shred`, `mkfs`, `wipefs`, `dd` writing a device) is `cannot-tell`; any other unknown command stays permitted. Gate closed is unchanged.
- **Decision records carry an area and a declared kind (#1133, story #988).** Every record writer (unpark, keep-parked, promote, demote, ack, sweep) now sets `area` from the issue's first `area:<x>` label, else the stated value `repo`; promote and demote record `needs_decision` instead of `unknown` (PROVISIONAL, awaiting owner ratification). With `decision_rubric.records.enabled` not true nothing changes: same comment bytes, no store file (tested).
- **Decision record writers now hand each stored record to the ledger pointer writer (#1134, story 988).** `decision_record.write_all` and the ack record in `handoff.py` call `decision_record.point` after a stored id; it is inert and never raises when the pointer gate is closed (zero bytes written). Batching and the daily budget stay the pointer library's. Tested on a real promote, ack and the sweep path; not run against a live ledger remote (UNMEASURED).
- **The nightly `full` job runs the safety check too (follow-up to #1052).** `python3 evals/safety/check.py` is now a step in `full`, right after its quality gate, so the Ubuntu 3.10-3.13 legs run it as well as the macOS PR leg. `tests/test_ci_matrix.py` used to forbid this; it now requires the step in both jobs, and a control that removes it from `full` goes red. Not measured: the step's wall time on the Ubuntu legs (5.7 s locally on macOS).

- **Resolver CLI probe finding record (#948; refs #941).** `docs/launch/evidence/resolver-cli-probe.md` records the real headless probe of the installed command line for the upkeep resolver: the design-flag table, settings, hooks, credential and billing behaviour, standard input, the budget-flag overshoot and exit code, killed-run metering, and the export-or-worktree choice, with what is still open. Docs only: no product code, flag table or model catalog changed.

- **`work.py start` no longer refuses on the registry files its own sync wrote (#954).** Once the
  registry is committed (branching-model §14), every pick rewrites `.sdlc/features/units/<unit>.json`
  and `<unit>.md` in the root checkout, and the next start refused on that dirt and suggested
  `git stash`. Every shard and page write now records a provenance chain in gitignored
  `.sdlc/state/features/provenance/` (git blob ids, written under a per-file lock before the
  replace), and the dirty-root guard exempts a registry file only when its bytes and its committed
  version are both in that chain, the committed one first. Hand edits, an edit Sigma then wrote over,
  staged files, mode changes, `index.json` and anything unproven still refuse, and the refusal now
  names each file (up to 10), prints §15's commit gesture, and no longer suggests `git stash`.
  Cost: one extra `git status --porcelain=v2 --untracked-files=no -- .sdlc/features` only when every
  tracked root edit is a registry file, nothing on a clean root (§6e's 4 and 17 are unchanged);
  measured 33.4 ms against 44.5 ms for the existing v1 status (median of 7, 1,236 tracked files,
  macOS, git 2.46.2). A registry write costs about 0.8 ms more with a short chain and about 4.3 ms
  at the 256-entry cap (median and mean of 300 writes, macOS APFS); a guard check with 20 and 200
  dirty, proven shards took 110 ms and 335 ms. Limits: a root already dirty from an earlier version
  refuses once until committed; `core.autocrlf`/clean filters, SHA-256 repositories and a `.sdlc`
  below the git toplevel get no exemption; with no `fcntl` a lost chain entry costs a spurious
  refusal. Verified with real-git tests (the issue's repro, the documented relative `.sdlc` CLI
  gesture, two deterministic concurrency seams and a multi-process smoke run), each seen red against
  the old code or a weakened build. Not measured: Windows, and a live multi-slot loop on an adopter.

- **PR `onboarding-control` job runs the plain onboarding control against a wall-time budget and the planted regressions (#879, slice 5 of #809).** `evals/regression/budget.py` runs `python3 tools/onboarding_control.py` and fails when it exceeds `ceiling_seconds` in `evals/regression/budget.json` (300 s, about 3.2x the max of 5 CI runs on the macOS PR cell: 58.6 to 95.1 s; the weekly Ubuntu and older-Python legs do not run it and were not measured); `pytest tests/test_onboarding_control.py -k planted` applies `drop-review-recording` and `skip-plan-review` to a scratch copy and demands the plain gesture exit 1 naming the review chain and the plan-review probe. Runs as its own `onboarding-control` job so the required `test` job does not grow.

- **Ledger bootstrap joins, never forks, the remote ops branch in a single-branch clone (#1142).**
  - **The defect:** in a clone whose `remote.origin.fetch` omits the `sdlc-ledger` branch (`git clone
    --single-branch`, or `--depth N`), `sync.py bootstrap` started a second, unrelated `sdlc-ledger`
    from the empty tree. Every publish was then rejected non-fast-forward while saying "will retry next
    tick". Seen in the field against a 325-commit remote branch.
  - **Fetches name their destination:** every ops-branch fetch (`init`, `pull`, the publish retry,
    `publish_receipt`, both knowledge verbs, and `merge_observation.resolve_remote_snapshot`) uses
    `+refs/heads/<b>:refs/remotes/<r>/<b>`. A URL or path `ledger.remote` and a glob branch name are
    refused by name.
  - **`init` refuses rather than fork:** it asks `git ls-remote` before starting a branch, and REFUSES
    (exit 1, nothing created) when the remote has the branch or a configured remote cannot be read.
  - **Unrelated histories are never rebased:** `publish` and `pull` return `refused: … shares no
    history …`, or `deferred` when a shallow cut hides the answer. A fixed fetch alone would have
    grafted the orphan via `-X theirs`, overwriting the shared README/TEAM.md.
  - **Re-running `sync.py bootstrap .sdlc` re-roots such a clone.**
    - This clone's own entries files (by host token, or legacy `<actor>-<pid>` name) are published,
      with same-named ones unioned.
    - Every other path takes the branch's copy, with an uncommitted local copy saved first.
    - Another machine's files leave `entries/` for `.sdlc/state/orphaned/`.
    - The old tip is kept at `refs/sigma/orphaned/<channel>/<stamp>`.
    - A re-root interrupted after its reset is finished by the next bootstrap.
    - It retries once when two first-ever bootstraps race.
  - **Recording the pushed tip:** after a successful push, publish sets the remote-tracking ref to
    the pushed commit. A narrow-refspec push never moves it, and a stale ref read as every published
    commit still unpushed.
  - **Doctor:** the team-ledger row says `DIVERGED`, or names a fetch refspec that cannot see the branch.
  - **Cost:** one `git merge-base` per pull and per rejected push, 11.7 ms on a 32,500-commit branch. The
    115.6 ms root walk runs only when merge-base finds no base.
  - **Re-root cost:** re-root plus publish took 1.07 s onto a branch of 1,000 entries files and 7.53 s
    onto one of 10,000. That is roughly linear; it includes the TEAM.md render and the push.
  - **How measured:** `.sdlc/research/1142-measure.py history` and `reroot`, with git 2.46.2 on one
    laptop at load average 2.5 to 3.4. CI was not measured.
  - **The re-root is operator-run, never automatic.** A deleted-and-recreated remote branch looks the
    same, and republishing onto it is a human's call.

- **PR `test` job timeout 30 -> 45 minutes (CI).** The macOS `test` job ran 24-28 minutes on recent main pushes and was cancelled at the 30-minute cap on #1052's and #1129's PR runs (annotation: "exceeded the maximum execution time of 30m0s"), so the cap no longer left headroom. The measured run times are from the Actions API; the new 45 is a margin, not a measurement. The `full` job's 90-minute cap is unchanged.

## 1.0.9 — 2026-10-11 — REST-first GitHub, the board skip in cloud sessions, and eval additions

- **`note()` is REST-first (#895, slice 8; refs #895).** `GitHubSource.note()` posts through `gh_api.comment_issue`
  (`POST issues/N/comments`); the GraphQL-first `gh issue comment` attempt is gone (none at all in a cloud session) and
  the `sources.py` ratchet drops 20 to 19 (TOTAL 37 to 36). The bounded retry is kept but is a REST re-POST only, for a
  server 5xx or an ambiguous transport failure, classified from gh stderr and the cause chain (never the comment body).
  Behaviour change: secondary rate limits and "unknown owner type" errors, which the old path retried, now raise at once
  with NO retry (`loop.py note` prints FAILED / exit 1); active-account drift (401/403/404) was never retried and still
  raises at once; a primary rate limit gets `comment_issue`'s single policy fallback. There is no Sigma-side
  timeout on this path; a gh-reported timeout in stderr is classified transport and retried over REST; a silent hang
  blocks. UNMEASURED. The duplicate-on-retry ruling is retained, REST-only, and deliberately
  contradicts `comment_issue`'s own "may have landed" stance. NOT run in a real Claude Code cloud session (UNMEASURED).
- **Regression evidence matrix, `matrix.py check` and `control.py run --record` (#1059, slice 4 of #808).** New
  `matrix.py check` reads `matrix.json` against `catalogue.json`/`gates.json` and fails on a catalogue
  (row, gate) pair with no entry, a red entry without run id/date/commit/pin, a commit that is not a 40-hex object or
  lacks the row/gate, a stale pin, and pending/live mismatches; shallow clones and `--skip-objects` print UNVERIFIED,
  not a pass. `control.py run --record` writes only gates it saw red (atomically, via `record.write_atomic`) and removes
  stale evidence for anything else. The regression README documents the gestures and what a record does not prove.

- **Regression control, `control.py run` (#1058, slice 3 of #808).** New `evals/regression/control.py`: extracts
  `git archive HEAD` into a temp directory (the working tree is never touched; uncommitted changes are not tested),
  runs each needed live gate once unmutated (already red: exit 3), applies each catalogue row's mutation to a fresh
  copy and demands every live `must_be_red_by` gate go red, then removes the copy in `finally`. A gate exiting 0 is
  exit 1 naming row and gate; pytest 5 / crash / timeout is exit 6 (never counted as red); mutator exits 10-12 pass
  through; `pending` prints and exits 0, or 4 with `--strict`. `safety-check` is now registered live. `--record` is
  #1059. Model-free, no network. Measured: the real catalogue (7 rows, 7 live gates, 2 pending lines) ran 123 s wall, all red, exit 0, python 3.12 on one laptop; CI not measured.

- **Safety check sink probes (#1050, slice 2 of #806).** `python3 evals/safety/check.py` now plants each pinned shape
  (built at run time) through the action log, the ledger, the comment path and the review-block reason, at the start
  and straddling each cap, and through the real `work.commit` refusal in a scratch repository, with the pinned outcome
  per redactor-only shape. Fix found by the probe: `comment_watch._excerpt` cut the raw body at 160 before scrubbing,
  leaving a fragment of a secret that straddled the cut; it now flattens, scrubs, then cuts.

- **Smoke runner wall-time line and ceiling (#1047, slice 4 of #805; refs #1043).** `evals/skills/smoke.py` prints
  `smoke wall: N.NNs, ceiling M.MMs` and fails when N exceeds `ceiling_seconds` in the new `evals/skills/budget.json` (1.0s, from three CI job logs: 0.09s, 0.14s, 0.10s);
  a CI step runs the bare gesture before pytest so the number is in the job log.

- **Regression gate set and catalogue (#1057, slice 2 of #808).** New `evals/regression/gates.json` (live gates by
  selection, `skill-smoke`/`safety-check`/`layer1-control` registered `pending` with a reason), `catalogue.json` (one
  row per mutation form with its `must_be_red_by` gates) and `catalogue.py check`, a read-only schema checker run in
  the fast suite: rejects an unknown gate id, an empty `must_be_red_by`, a row whose only gate is pending, and a row
  that drifts from `mutators.py`. Test-first and phase-record suites are deliberately not listed (measured green).
- **Reviewer briefs REST-first, honest cloud limits, tighter prose ratchet (#895, slice 7; refs #895).**
  `review_context.py` briefs now say `gh api repos/{owner}/{repo}/pulls/N` (diff Accept header) and `.../issues/N`
  instead of `gh pr diff` / `gh issue view`. The `work.py` merge `mechanism` string (and the regenerated
  `docs/enforcement.md` row) now states the real cloud limits: a pending required check, any PR line comment and
  `require_review: approval` park; `merge_rights` needs the token to receive `permissions` (UNMEASURED).
  `tests/test_no_direct_gh_prose.py` pins each allowlist entry's `noun verb` and scans 4-space indented blocks.
  `docs/cloud-sessions.md` gains "What remains (#895 slice 7)": the 37-site audit, the `GitHubSource.note()` finding
  (first attempt is GraphQL-backed `gh issue comment`; REST only after a transient failure; probable cloud blocker,
  UNMEASURED, not fixed) and the deferred sibling-PR read. `review-post-pr` token ceiling moved 6305 to 6310. Ratchet
  baseline stays 37. NOT run in a real Claude Code cloud session.
- **Loop-path skill prose is REST-first (#895, slice 6; refs #895).** The agent instructions in sigma-loop
  (SKILL, running, picking, landing), sigma-goal, sigma-retro, sigma-model, sigma-triage, sigma-scope, sigma-context
  and sigma-radar now read and label issues with `gh api repos/{owner}/{repo}/...` instead of `gh issue view|list|edit`
  and `gh pr view`; `Bash(gh api *)` is added to those skills' `allowed-tools` (old grants kept). New
  `tests/test_no_direct_gh_prose.py` keeps bare `gh issue|pr|project|label <verb>` out of skill prose (allowlisted
  exceptions carry a reason) and checks the grant. `docs/cloud-sessions.md` gains "Running the loop in a Claude Code
  cloud session" with owner smoke-test steps. The `Bash(gh api *)` grant is as broad as the gh issue/pr grants beside it (not narrowed; per-endpoint allowlist is the follow-up). NOT run in a real Claude Code cloud session. Slice 7 (below) closed the
  `review_context.py` and `mechanism` items.

- **Cards for the prose-only skills; the per-skill smoke runner is now a gate (#1046, slice 3 of #805; refs #1043).** One
  `pinned` card per skill with no script (24 today, derived from disk), each pinning a verbatim sentence from its own
  `SKILL.md`; `tests/test_skill_smoke.py` runs `python3 evals/skills/smoke.py` on the real tree. Measured: bare run exit 0,
  0.08 s locally on one machine; not measured in CI (budget is slice 4).
- **Board gate when GraphQL is unavailable (#895, slice 5; refs #895).** New `GitHubSource.board_active`
  (`project.enabled` AND `gh_api.graphql_available()`): when GraphQL is off (`CLAUDE_CODE_REMOTE`,
  `SIGMA_GH_GRAPHQL=off`) every Projects v2 call (card moves, field writes, archive, backlog seeding, the Phase
  field, the board-queue pick) is skipped, never raised and never parks a goal, with ONE stderr notice per process
  (not per run: `phase_report.py` is a fresh process per phase boundary). `sdlc:*` labels stay the source of truth;
  promote/unpark/auto_unpark read the gate so no false "card did not move" hint. Doctor and `/sigma-status` say so.
  Not gated: `_swap_labels` and other label GraphQL (residual), and the operator tools `board_migrate`,
  `board_setup`, `board_layout`. With GraphQL available behaviour is unchanged (existing board suites pass
  unmodified). Measured: a recording-fake test only. NOT tested in a real cloud session. Details:
  `docs/cloud-sessions.md`.
- **Six runner seams go through `gh_api` (#895, slice 3b; refs #895).** `ledger._run_gh`, `feature_owner._run_gh`,
  `cross_repo._run_gh`, `status._github_counts` (x2) and `board_setup.Board.gh` now use the new `gh_api.run_gh`
  (rc/out/err, a timeout is 124) and `gh_api.gh_argv`. These were already `gh api` calls: a spelling change, not a
  migration to REST. A timeout now exists where none did (ledger 15 s, a judgement and unmeasured; feature_owner
  120 s; cross_repo 30 s). `close_pr_gh` also rejects a 200 body with `merged: true`. NOT yet REST: every `label
  create` site (sources x3, triage x1 = slice 3c), `sources.note()` (owner ruling), `define.py` x2, `status.py`'s
  no-sources fallback, `doctor.py`, the board sites (slice 5), `work.py` x3, `verify_merge.py` x3. Nothing is tested
  live. Ratchet (printed `scan()`): TOTAL 43 -> 37. Details: `docs/cloud-sessions.md`.
- **Design-PR merge and close go REST (#895, slice 4b-3; refs #895).** goal-review's `merge-design` is
  `gh_api.merge_pr_gh` (REST `PUT pulls/N/merge`, pinned to the head sha the design-identity check vetted; ONE
  `gh pr merge` fallback on a primary rate limit only) and `close-design` is `gh_api.close_pr_gh` (comment POST,
  then `PATCH state=closed`). No retry any more (a merge is not idempotent). After a failure that may have landed
  the outcome is reconciled by a REST read of the PR's `state`: only a positive MERGED/CLOSED counts, and an empty,
  truncated, malformed or unreadable answer reads "outcome unconfirmed", never success (the old "open list came
  back empty" inference is gone). `verify_merge.py` (ready/create/merge, the attended rebase tail) is NOT moved and
  stays GraphQL, so that tail still cannot run in a cloud session. UNMEASURED live: every body and status here is
  from GitHub's docs or fakes. Ratchet (printed `scan()`): work.py 5 -> 3, TOTAL 45 -> 43. Write-surface:
  `merge_design`/`close_design` become `gh-api-write` rows (`close_pr_gh` joins the scanner's set). Details:
  `docs/cloud-sessions.md`.
## 1.0.8 — 2026-10-11 — per-session run budgets and REST-first merges

- **A cloud session can reach the merge PUT (#895, slice 4b-2; refs #895).** The three GraphQL reads that stopped a
  `CLAUDE_CODE_REMOTE` merge are REST: `merge_rights` reads `GET repos/{owner}/{repo}` `.permissions` (fails closed
  on a missing, malformed or all-false object), `protection()` unions classic protection with
  `rules/branches/<base>` rulesets (a repo enforcing only through rulesets no longer reads as "not protected"),
  and `post_review` posts its comment with `gh_api.comment_pr` (`POST issues/N/comments`; ONE `gh pr comment`
  fallback on a primary rate limit only, never in a cloud session). STILL NOT possible in a cloud
  session: unresolved review threads can not be read (without GraphQL the review gate makes one REST read of the PR's line
  comments: none => proceed, any or unreadable => refuses; with GraphQL nothing changes), `require_review: approval`
  (`reviewDecision` unreadable, parks), the `--auto` arm (a check pending after 450s parks), and `merge_design`,
  `close_design`, `verify_merge.py` stay GraphQL. `protection()` cannot see bypass actors (it can over-report "enforces"), `evaluate`-mode rulesets,
  merge queue, deployments/workflows/signatures/thread resolution, or more than 100 rules. UNMEASURED live: the
  cloud token receiving `permissions`, and the rules read, comment POST and PUT through the proxy. Ratchet
  (printed `scan()`): work.py 6 -> 5, TOTAL 46 -> 45. Write-surface: `comment_pr` and `merge_pr_gh` join the
  scanner's REST-write set; `post_review` and `merge` get `gh-api-write` rows. Details: `docs/cloud-sessions.md`.
- **Level 1 union handles a unit changelog entry across a release heading (#1103).** When the base moved the old Unreleased entries under a new dated version heading, the heading-aware union now keeps the base's sections as they are and files the unit's new entries under Unreleased above that release section, in order, dropping any the base already has; other shapes still park.
- **Write-surface scanner resolves names per function (#926).** `tools/readiness/write_surface.py` no longer lets the last assignment to a name in the module win for every function: a local `args` list resolves to that function's own assignment, then the enclosing function's, then the module's. Six inventory rows that existed only through another function's list are removed (`work.py` `finish` `gh-pr`, the `sources.py` `_gh_json`, `_run_gh`, `fetch_issues_rest` `gh-label` rows and its `_graphql` mutation row, the `gh_api.py` `_default_run` row) and `finish` now counts 2 `git-destructive` calls, its real `git worktree remove` included.
- **Branch upkeep close-out: routing text, stated limits and a recorded hosted run.** The scaffolded project template, Cursor rule and Codex block now carry one identical sentence routing "rebase, catch up or land a unit" to the `sigma-rebase` skill (new adopters only; `docs/branching-model.md` section 13f says how an existing adopter picks it up), the rebase skill and a new enforcement row state that landing is user-requested and deliberately not on the irreversible-actions list, and `docs/launch/evidence/upkeep-hosted-run.md` records the sanitized result of one validation on a private throwaway repository (init, two-goal unit, backup, restore, prune, guarded landing, mechanical conflict level, scheduler job). Not covered there: a real-model resolver or reviewer, the chat door against a chat service, and branch rulesets or bypass actors; the scale figures remain reasoned, not measured.
- **Branch upkeep part B is documented and its local run recorded (#953).** New `docs/upkeep-part-b.md` explains the three conflict levels (`upkeep.conflicts.resolve`: off, mechanical, agent), the landing engine, the opt-in audit and what was and was not proven; `docs/launch/evidence/upkeep-part-b-run.md` records one offline run through every level on a local bare remote with a fake resolver and reviewer, produced by `tests/test_upkeep_partb_run.py` so the record cannot drift. The real-model run is stated as not run (model catalog entry is a placeholder, flags unverified, no credential or cap chosen), as are the chat door, a real-model resolver and reviewer, and branch rulesets. The config table row for `conflicts.mechanical_without_verify` no longer says it is unused. Level 2 stays unreachable in the shipped engine until its plan seam is supplied. Off by default; no behaviour change.
- **Never-delete guard sees `update-ref` with a message flag, `push --mirror` and string-form commands (#958).** A new parity test ties the guard to the write-surface scanner and pins the remaining known limits by name.
- **Tests pin `update-ref --stdin` and the bare delete flag in the write-surface scanner (#1066).** Test-only; seen red against a broken classifier.
- **One issue per parked conflict id (#1104).** Before filing a park finding, upkeep reads the open issues once (newest 100) for the same conflict id; if one exists it files nothing and adds one short comment (a counter and the time) at most once a day. A changed conflict id files a new issue that names the one it supersedes (the old one is left open), and a later clean pass comments on an issue found this way that it resolved, without closing it. Needs `upkeep.enabled` and a resolvable repository; the gate closed is unchanged.
- **Upkeep gate follow-ups (#965).** The decorator now refuses a machine-door signature with a rest parameter at decoration, its docstring no longer overclaims about a positional environment mapping (a closed gate ignores it), the config template note says an unknown key far from a known one is described by position, type and length, and the test attempt trap also records socket bind, truncate, chmod, chown and utime, with its scope stated.
- **Config readers for the ledger, work, drift watch and action log blocks are total on a truthy non-object block (#966).** A hand-edited `"ledger": true` now reads as absent instead of raising; the upkeep gate's ledger test and the template discoverability pin were updated to match.
- **Upkeep pass follow-ups.** With the upkeep gate open, the goal rebase in the work script now re-anchors the evidence fingerprint after a successful rebase, as the behind-PR path already did; the loss check reads the runtime acknowledgement file only while the gate is open, so a file left behind after the gate is closed is ignored; and the attended rebase's push-lease tip read is inside its never-raises contract, returning a failed outcome instead of raising. With the gate closed, the rebase paths are unchanged. Still open: no shipped caller yet supplies the pass's rewrite and push callables.
- **Upkeep job environment is a strict allowlist.** The detached upkeep job and its engine child now receive only a named list of variables (path, locale, home, ssh agent, and the named GitHub credential variables); lookalike `GH_` and `GITHUB_` names, cloud keys and model keys are dropped, and the branching docs list the exact set. With the upkeep gate closed nothing changes.
- **Upkeep now says when it has no integration branch.** A repository set up with the default init flow has an empty `work.base`, so the upkeep pass ended `no-base` and did nothing without a hint. The pass report now says `work.base` is empty and where to set it, and `/sigma-doctor` shows an "upkeep integration branch" row when upkeep is on and the base is unset. With upkeep off or the base set, output is unchanged. Init still leaves the base empty, because an empty base also means the branch the loop started on for goal work.
- **Backup prune re-read now sees a late remote-tracking head (#1064).** The pre-delete re-read also asks for each
  backup's `<name>/HEAD` form, which a tail pattern of the name cannot return, so a `refs/remotes/<name>/HEAD` that
  appears after the first listing keeps its backup out of the prune. The residual window between that re-read and the
  delete push is stated in `docs/branching-model.md` as unmeasured. Gate closed: unchanged.
- **The bounded runner always ends its lifeline, even when the group-gone confirmation raises (#1065).** A raising confirmation now ends the lifeline as unconfirmed, so the sentinel stops the group instead of being left running.

- **Head-pinned merge helper: dot-only repository segments and sha-less replies refused (#1067).** The `gh_api` repository
  pattern now rejects a dot-only owner or name segment, and `merge_pr_pinned` raises on a merge reply that carries no
  full hex sha, so the landing reads the pull request back and reports it as not merged instead of trusting the reply.

- **Unit sibling guard: a foreign file no longer blocks every lookup, and the landing engine now calls the guard (#1068).** `cross_repo._unit_landing_records` skips (and names on stderr) a file in the landing folder that is readable but does not parse as a landing record; symlinks, unreadable files and the record ceiling still refuse. `feature_land_merge.complete` (and `feature_land.land`) take an optional `landed(repo, branch)` and run `work.unit_sibling_guard` after the tip re-read and before the approval guard, refusing `refused:sibling` without spending an approval; with no measurement supplied a unit that names another repository refuses, a single-repo unit is unchanged, and the closed upkeep gate stays byte-identical.
- **Disposition record names the real resolution module (#1069).** `docs/launch/dispositions/945.json` called the module without its `feature_` prefix; it now names `feature_upkeep_resolution.py`, and `tests/test_disposition_module_names.py` fails when any disposition record names a `.py` file that does not exist.
- **Pending-landing record no longer blocks a retry after a crash between begin and merge (#1070).** A pending record with no recorded call whose pull request reads back open and unmerged now settles as `refused` (`crash-before-merge`) on the next landing, so `begin` may overwrite it instead of waiting for the 14-day prune. A record whose lost call was recorded stays unconfirmed.
- **Upkeep follow-ups: runtime ack file self-heals, descriptor checked before the proof (#1071).**
  A runtime acknowledgement file cut short by an interrupted write is now rewritten as a whole document on the next
  resolved push, even when that push has nothing new to acknowledge. The missing-backup-descriptor refusal now runs before
  the Level 1 proof, so a replay that can never be pushed no longer spends the verify command.
- **Reviewer route: a truncated brief blocks, manifest generation is explicit (#1072).** `feature_upkeep_review.review` now returns `block` (nothing launched, no manifest) when any brief cap would cut or drop input; `make_manifest` no longer takes a placeholder generation, the store assigns it.
- **Two load-flaky tests fixed at their seams (#1073).**
  The hung-gh kill test started its one-second clock at spawn, before the stub had recorded its child, and bounded the second call by wall time; the clock now starts when the stub reports ready and the second call is checked by spawn count. The near-enabled matrix for the job entry point loaded its script inside the trap, so a cold bytecode cache looked like a write; scripts now load before the trap opens. Each has a deterministic control. No timeout was raised.
- **Changelog pins survive a release commit (#1074).** Audit of every test that reads the shipped changelog: the upkeep drift entry pin looked only at Unreleased plus one section, so it broke at the second release; the backup slice pin read only the text above one fixed heading. Both now read the whole file, a new `tests/test_changelog_release_layout.py` lists each reader with whether it survives, and replays the release layout once and twice against every pin, with a control showing the old window shape goes red. Tests only; no product change.
- **Upkeep scheduler follow-ups: killed-job note, in-unit budget, lock window stated (#1075).** A job killed outright is now closed as `failed` and noted by the watcher on the next tick; the tick budget is checked inside a unit's measurement, not only between units; the lock-probe window is documented in `docs/running-unattended.md`.
- **Upkeep pass entry point** — `run_unit_pass` now supplies the pass its rewrite (replay onto the base tip in a scratch worktree) and a lease-guarded push; it is gated by `upkeep.enabled`, so with the gate closed nothing changes.
- **Upkeep ack read gated** — with the branch-upkeep gate closed, the per-unit runtime ack file under the rebase state is no longer read, matching the upkeep acks file; gate open is unchanged.
- **Config template note corrected**: the `_upkeep_conflicts` note in the scaffolded config now says Level 1 acts on `upkeep.conflicts.mechanical_without_verify` today (it lets a changelog conflict resolve with no verify command set) instead of saying a later release reads it.
- **Pre-push private-term guard.** The guard that refuses a push carrying denied patterns is now a tracked, tested tool, `tools/prepush_scan.py`, with an install step in `docs/prepush-guard.md`. It reads its patterns from a file you supply, scans only the commits a push adds (a merge of the default branch no longer pulls upstream commits into the scan), reports hits by location and pattern line number without echoing the matched text, and refuses a missing or empty patterns file.
- **Upkeep landing: a young never-called pending record is no longer settled as crash-before-merge** - `settle` leaves a record younger than 600 seconds pending (unconfirmed `landing-in-flight`) so another process mid-landing is not refused; older records settle and retry as before.
- **Config template `_upkeep` note matches what runs today.** The note no longer claims nothing else runs from the upkeep block; it names the scheduler, the conflicts note and the three opt-ins, and a test pins the wording.
- **Upkeep pass entry point**: its remote and branch are now refused when they start with a dash or are not valid ref names, and the detached upkeep job calls it for its unit (opt-in doors only; gate closed changes nothing).
- **Fixed a load-sensitive upkeep launcher test**: the kill test now starts its wall-clock budget only after the fake resolver has reported its child, with a deterministic slow-start control (#1099).
- **Upkeep job: one path owns the unit rewrite** — when the older rebase pass moves a unit, the new unit pass skips it in that run; `valid_ref` is tested to never be looser than `git check-ref-format`.
- **Drift measure: `--end-of-options` before the refs in its history read (#1063).** The `git log` in `feature_upkeep_drift.walk` now passes the marker before the included and excluded refs, so a ref name is never parsed as an option even if the existing dash and whitespace refusal were bypassed. The `rev-parse` call takes no ref and is unchanged. The argv test asserts the marker precedes the ref, and a mutant that drops it is seen red.
- **Write-surface row for the offline rubric end-to-end run script (#1108).** Registers its throwaway-repo writes, which slice 21 left out.
- **Shared-path registry catch-up (#1108, part of #988).** Eight vetted-writer additions in the predecessor written-paths fixture for the new applier config read, the triage finish step in `handoff.py` and the spend-approval counter, so the shared-path guard is green again.
- **Decision rubric, slice 21: docs rewrites and the offline rubric end-to-end run script (#1011, story #988).** The label model, README, skills and instruction files no longer describe a confirmation queue (the label is legacy; triage arms or parks at filing; the README names queued as the filing default). The run script (`run` verb) drives records, classifier, ladder, judge, triage and migration offline on a throwaway repository; with every switch off it matches a run with no rubric config.
- **Decision rubric, slice 20: `promote.py migrate-confirmation` (#1010, story #988).** A one-time verb that moves every issue still carrying the confirmation label to armed or an ordinary park through the rubric triage, the blocker predicate and the one-mutation label swap; dry run by default (`--apply` to write), pages past the 200-issue window, prints `covered N of M` and exits non-zero when N is below M, leaves closed issues untouched, and is a no-op on a second run. Nothing else changes.
- **Decision rubric, slice 18: readers treat the confirmation label as legacy-only (#1008, story #988).** The blocker policy no longer has a confirmation row, status counts leftovers only, doctor flags a leftover as an advisory check, the board Needs a human view lists parked and blocked, and `ledger.handoff.proposed_label` is documented as a read-only legacy alias; promote still lists a leftover and triage still strips it from a picked issue.
- **Decision rubric, applier prune follow-up (#1107, part of #988).** The applier tick now also removes applied asks with no pending veto once older than `autonomy.applier.prune_after_days` (default 30, at least 1), so they no longer accumulate; a recent applied ask stays for the rolling daily cap. Closed gate unchanged.
- **Decision rubric, applier follow-up (#1106, part of #988).** The applier now writes the ask as applied right after the gesture and before the decision record and the comment, so a failed status write no longer repeats them on the next tick; with the applier gate closed nothing changes.
- **Decision rubric, slice 19: levels 3 and 4, the act-and-tell applier (#1009, part of #988).** New library `applier.py` and one extra watch step through the timed-call runner: a parked question nobody answered inside `window_minutes` is answered by its approved, sealed rule through the atomic unpark or promote gesture and the rule id is commented; a person's answer inside the window wins, a no afterwards is a veto (drops a level), hard-stop kinds never reach it, `max_auto_applied_per_day` bounds it, level 4 is silent only for kinds in `allow_silent` (every `spot_check_one_in`-th still comments), and the doctor warns when the tick is older than `stale_after_intervals` watch intervals. OFF unless `decision_rubric.autonomy.applier.enabled` is true; closed behaviour is byte-identical.
- **Decision rubric, rulebook follow-up (#1105, part of #988).** `rulebook.match` now takes the repository config and passes it to the hard-stop kind check, so a configured extra hard-stop kind is never matched by a rule; it also ignores rules that fail the schema or are approved without a matching seal, so a direct caller cannot bypass the seal. Optional argument; existing callers and closed behaviour are unchanged.
- **Decision rubric, slice 13: rulebook and propose-approve flow (#1007, part of #988).** New library `rulebook.py`: repository-scoped rules (a person field makes a rule invalid) proposed as a reviewable file diff plus one owner park, approved only through `autonomy.is_owner`, matched as a recommendation only (none or several matches ask), with re-ask after `rulebook.reask_days`. OFF unless `decision_rubric.rulebook.enabled` is true; closed behaviour is byte-identical.
- **Decision rubric, slice 12: autonomy levels 0 to 2 and watch recorder (#1004, part of #988).** New library `autonomy.py`: a level per question kind and area derived from human answers and watch agreement (lowest machine wins, a veto drops one level, hard-stop kinds capped at 2), watch records pruned after `watch.retention_days`, `is_owner` falling back to `spend_approval.approvers`, and a doctor readout. OFF unless `decision_rubric.autonomy.enabled` is true; closed behaviour is byte-identical.
- **Decision rubric, slice 17: scope output and demote (#1006, story #988).** With triage on (the default) the scope plan compiler arms each child at its plan priority or parks it when the deny-list matches (report gains `parked`), start-now never promotes a parked child, and demote parks with a reason and question kind; `ai_filed.triage.enabled` false keeps the old output exactly.
- **Decision rubric, slice 16: gate holds are parks (#1005, story #988).** The scope and ownership pick gates now park a held goal with the declared kind scope_hold or owner_hold instead of writing the confirmation label (new library `gate_hold.py`); `/sigma-unpark` lists, asks about and refuses to release a hold the registry still names, and the automatic sweep leaves them alone. `ai_filed.triage.enabled` false restores the old label write exactly.
- **Decision store hardening (#1082, part of #988).** The archive folds records by file name stem, ids starting with the archive prefix are rejected, and `get`, `list_by` and `reindex` now require a config and return nothing when the part is closed.
- **Decision rubric, slice 8: record writers (#1000, part of #988).** New library `decision_record.py`; unpark, promote, ack and the automatic sweep write a store record and an issue comment carrying the same id (decision lines fenced so trigger words plant no blocker; the sweep records `autonomous`). A store failure still posts the comment. OFF unless `decision_rubric.records.enabled` is true; closed output is byte-identical.
- **Decision rubric, slice 15: filing path (#1003, story #988).** `create_tracked_issue` now arms an AI-filed follow-up or upkeep finding at its triage bucket priority, or parks it with a declared question kind and a park event (ownership-refused filings park as owner_hold; no needs-confirmation label), with per-goal, per-run and depth caps backed by the new bounded `ai_filed_counter.py`; set `ai_filed.triage.enabled` to false for the old output exactly.
- **Decision rubric, slice 14: file triage core, pure and unwired (#999, story #988).** New library `file_triage.py`: `decide` returns arm, park or queue (never a blocking priority; login-free), a deny-list can only park, `validate_buckets` rejects any bucket at or above the human default; new `ai_filed` template block (triage on by default, buckets P2, P3, P4). Nothing calls it yet.
- **Decision rubric, slice 9: ledger pointer (#1001, part of #988).** New library `ledger_pointer.py`: an unaddressed ledger note (reserved ref `decision-pointer`) carrying only record id, question kind and how, batched to `records.pointer.max_writes_per_day` per writer per UTC day with the rest queued, a `compact_pointers` verb that folds only this writer's own old pointer lines into one rollup file, `pointer_file_count`, and a doctor readout. OFF unless `records.pointer.enabled` is true (and records and the ledger are on); closed writes zero bytes.
- **Decision rubric, slice 10: hard-stop classifier wired at Sigma's own chokepoints (#1002, part of #988).** New library `hard_stop_guard.py` with `guard`, `is_own`, `guard_issue_write` and `record_done_net`, called at the head of work commit, pr and rebase pushes, the unit-branch upkeep push, the issue release, complete, park and note writes, and `loop.py record done`; a matched class or a cannot-tell parks, a `sdlc/` or registered unit lease push and Sigma's own merge are never classified, `gates.irreversible_actions` becomes the legacy alias of the class toggles, and with `decision_rubric.hard_stops.enabled` closed (the default) every step makes exactly the calls it made before.
- **Decision rubric, slice 5: decisions store (#996, part of #988).** New library `decision_store.py`: one exclusive id-named file per recorded decision in a committed records folder, public-visibility redaction of free text, a rebuildable uncommitted index under a bounded-wait lock, a person-run archive verb, and doctor warnings above the file ceiling and for old untracked records. OFF unless `decision_rubric.records.enabled` is true; repository-based, never person-specific.
- **Hard-stop classifier: close pattern gaps (#1079, follow-up to #995).** An owned-branch push no longer hides a second class in the same command; split-flag `rm`, remote branch deletion, `git branch -D` and `find -delete` classify as destroy; quotes also redact `KEY=val` prefixes and `-p` arguments.
- **Decision rubric, slice 7: class-bound recorded permission and marker scrub (#997, story #988).** `spend_approval.check` takes `hardstop_class`: only spend is honoured (config `spend_approval.classes`, default spend, can only narrow) and any other class is DENIED with a reason; the marker may carry an optional action digest, each use is counted in `.sdlc/state/spend-approval-used.json` so a deleted audit comment cannot re-arm it, and `scrub_marker` quotes a first-line marker. No class given: output unchanged.
- **Decision rubric, slice 11: drift judge in shadow mode (#998, story #988).** New library `skills/sigma-loop/scripts/drift_judge.py` (`judge`, `verify_quote`, `ceiling_ok`) taking an injected model call; off by default, no call without a configured model and daily ceiling, fail-closed lock, own spend file, nonce-delimited documents, conflicts only with a verified quote. Cost of a real run is not yet measured.
- **Reference documents key for the reviewer brief: `context.documents` and `context.inline_max_bytes` (#993, decision rubric slice 3).** Extra repo-relative documents join the brief with a per-document present, absent or unreachable state, parent-escaping paths are refused with a reason, oversize bodies become pointers, and an explicit empty list says no documents were named. Absent key: brief byte-identical.
- **Decision rubric, slice 2: config skeleton (#992, part of #988).** New total reader `decision_rubric_cfg.py` (`load`, `visibility`, `public_repo_warning`), an OFF-by-default `decision_rubric` template block with `records.repo_visibility`, and a doctor features row; a wrong-typed value closes the whole block with a named reason.
- **Decision rubric, slice 1: measure and pin, read-only (#991).** New `skills/sigma-loop/scripts/park_mix.py` counts park events and park comments by reason class and censuses needs-confirmation issues by population (gate holds are never counted as AI-filed); `park_mix.py report <sdlc_dir>` writes nothing. Tests pin the current filing label sets. No behaviour change.
- **Decision rubric, slice 6: hard-stop classifier core, pure and unwired (#995, story #988).** New library `skills/sigma-loop/scripts/hard_stop.py`: closed classes with per-class toggles, a class registry (`register_class`), raise-only `raise_to`, `cannot-tell` on any error, own-branch carve-out (`is_own`), `is_hardstop_kind`, and scrubbed quotes. Off unless `hard_stops.enabled` is true; nothing calls it yet.
- **Decision rubric, slice 4: question-kind declaration (#994, refs #988).** New `qkind.py` (closed `QKINDS` list incl. `scope_hold` and `owner_hold`); `loop.py record <dir> <goal> parked "<text>" --qkind <kind>` appends a `sigma-qkind: <kind>` machine line to the park comment (unknown kind: exit 2, nothing recorded) and the unpark brief prefers the declared kind. Without the flag nothing changes.
- **The code-goal merge goes REST first (#895, slice 4b-1; refs #895).** `work.merge()`'s direct merge is now
  `gh_api.merge_pr_gh`: REST `PUT pulls/N/merge` with `merge_method` and the vetted head as `sha` (was `gh pr merge
  N --<m> --match-head-commit SHA`), with ONE `gh pr merge` fallback on a primary rate limit only, and only while
  GraphQL is available; never a retry. Success is ONLY a reply carrying `merged: true`: an empty, `null`,
  non-object or `merged: false` 2xx is an UNKNOWN outcome, never a landing. An unknown outcome (5xx, timeout,
  secondary rate limit, malformed 2xx, a fallback failure with no `HTTP nnn`) is never re-sent and never sent via
  GraphQL: it is read back with up to 3 REST `pulls/N` reads (5s apart, bounded 10s); MERGED lands normally
  (line gains `(outcome reconciled: ...)`), CLOSED parks, still OPEN/UNKNOWN returns `merge outcome unknown for
  PR #N (...)`, routed to `record review` (awaiting merge: the merge-reconcile pass records done if it landed,
  doctor's 3-day stuck-merge alarm if not). A definite refusal (409 head moved, 405, 401/403/404/422, a primary
  rate limit without GraphQL) parks as before, with no read. Without GraphQL (`SIGMA_GH_GRAPHQL=off` or
  `CLAUDE_CODE_REMOTE`) a required check still pending after gate()'s own 450s REST wait is NOT armed (`--auto` is
  GraphQL): `PARK: required checks still pending after 450s and auto-merge cannot be armed without GraphQL`; no
  extra wait, so the documented 22.5-minute worst case is unchanged. GraphQL available: the arm path is
  unchanged. HONESTY: D2/D3 are reachable today only where GraphQL works (`SIGMA_GH_GRAPHQL=off` exercises the
  `graphql_available()` branches); a real `CLAUDE_CODE_REMOTE` session still stops earlier, at `merge_rights`'
  GraphQL `viewerPermission` read (`PR #N opened -- could not determine merge rights`), so a cloud session
  cannot merge yet. Host cut mid-PUT: nothing is recorded locally; the lever is `loop.py record review` (a re-run
  of `work.py merge` against a PR that did merge is NOT measured and may park it). Ratchet (printed `scan()`): work.py 7 -> 6,
  TOTAL 47 -> 46. Deferred to slice 4b-2: the `merge_rights` permission read, `protection()`'s `nameWithOwner`
  read, `post_review`'s `gh pr comment`, `merge_design`'s `gh pr merge`, `close_design`'s `gh pr close`,
  `verify_merge.py` (ready/create/merge); `reviewDecision` stays GraphQL by decision; R5 `pr list --head`.
  UNMEASURED: the REST merge was never run against real GitHub or in a real cloud session; the PUT 2xx body,
  405/409 statuses and bodies, the 429 body, CLI `--match-head-commit` vs REST `sha` equivalence, merge-queue
  behaviour of the CLI fallback and real merge latency all come from GitHub docs or fakes. Measured: the new
  tests in `tests/test_gh_api.py` and `tests/test_work.py`; no timing claim made.

- **A second session's `loop.py start` no longer re-anchors a live session's run (#955).** Every
  start used to reset the one checkout-wide STATE.md run cursor, so session B's `start` voided
  session A's passing verify (`record done` exit 4, "verify evidence predates this run"), restarted
  A's `max_minutes` clock, zeroed A's `max_tokens`/`max_codex_raw_tokens` spend or handed it to B,
  dropped A's in-flight phase from every budget, and let A emit a duplicate `run_stop`. Measured
  before the fix (origin/main 66b1f1b): the 15 new tests in `test_run_cursor_955.py` failed by
  assertion; on 5f25b99 the issue's own CLI gestures printed a goal path where A had spent 1100 of
  `max_tokens 1000`, then `record done` exited 4. Now each start (`loop.begin_run`: CLI `start`,
  `run_loop`, `assign._start_drain`) also stamps a `run` block in its own session marker with the
  same instant; budget gates and the `run_stop` dedupe read the caller's own block, verify freshness
  anchors to the goal's live holder(s), and phase/spend credits go to the holders (spend naming no
  goal, or a goal no live session holds, to every live session). STATE.md is still written byte for
  byte as before, so a single loop, a goal no live session holds, and a dead session's goals behave
  as they did. `start-run` takes `--session-pid` (bare: the only live session, or none with a stderr
  line when several are live). Budgets are per session, so **N concurrent sessions may together
  spend N x each ceiling**, as N sequential runs could. Marker writers now keep every key they do not
  pass (D-1), and a credit never refreshes a marker's mtime (the lease-TTL liveness backstop).
  Measured 2026-10-10 on an Apple M1, synthetic markers, this code: a marker is 4.1 KB at 141
  credited Claude attempts and 112 KB at the 4096-attempt cap (256 KB when a Codex map is full too;
  today's markers are 42-111 bytes); one credit 0.9 ms at 141 attempts, 3.9 ms at the cap (11 ms with
  both maps full); a live-session scan (once per `done_refusal` and per credit) 0.2 / 2.2 / 10.6 ms
  for 1 / 10 / 100 sessions at 140 attempts and 1.0 / 7.4 / 70 ms at the cap; a `spend` naming no
  goal 10 / 105 ms with 10 / 100 live sessions at 140 attempts and 48 / 706 ms at the cap. Past 4096
  attempts a holder's credit refuses loudly. Not measured: a network filesystem, Linux.
  Residuals: (R1) a dead holder whose pid is reused reads live until the lease TTL (12 h default) and
  its run start keeps anchoring that goal, bounded by #498's run id and #1897's fingerprint; (R2) a
  goal in no live `in_flight` (`/sigma-goal`'s arming path, a goal after `record review`) keeps the
  checkout anchor, so a concurrent start still voids its evidence, as before; (R3) `/sigma-doctor`'s
  budget row still windows events from the checkout's latest start; (R4) crashed sessions'
  heartbeat files are still unpruned; (R5) a bare `start-run` with one live session resets that
  session even when the caller is not it, as before.

## 1.0.7 — 2026-10-11 — doctor preflight checks and small fixes

- **Doctor preflight: missing verify interpreter and branch-creation rulesets (#974).** `/sigma-doctor` now adds a
  MISSING row when the first word of `verify.command` is not on PATH (or an absolute interpreter path is not an
  executable file), and, with `work.enabled` and `gh` present, one MISSING row per ruleset that restricts creating or
  updating `<branch_prefix>*` branches while `current_user_can_bypass` is neither `always` nor `exempt` for the running account, naming the
  ruleset. Both are silent when fine; an unreadable or malformed GitHub reply gives one advisory "could not check" row.
  New `landing_preflight.py` library, plus read-only `gh_api.ruleset` and `gh_api.org_ruleset`. Commands starting with
  `cd`, variables, `~`, command substitution or shell wrappers are never checked. **Not seen live:** the real GitHub
  reply shape is faked in tests from the documented fields.
- **Per-skill smoke cards for the script-bearing skills (#1045, slice 2 of #1043).** New `evals/skills/cards/<skill>.json`
  for every skill with a `scripts/*.py|sh` file; `library` stems across them equal `test_script_help.LIBRARY_ONLY`. Only
  skills whose every gesture has a passing read-only fixture are `exercised`; the rest are `pinned` (a verbatim
  `SKILL.md` gate) and artifact producers are an existing script or `agent`. The bare `python3 evals/skills/smoke.py`
  stays red for prose-only skills until slice 3 and is not a test gate.
- **Regression mutation engine (#1056, slice 1 of #808).** New `evals/regression/mutators.py` (stdlib only):
  `python3 evals/regression/mutators.py apply <regression> --form <form> --root <dir>` breaks one named function body
  or table entry in a COPY of the tree, resolved with `ast` and never by line number. Regressions: `skip-plan-review`
  and `drop-review-recording` (form `code`), `disable-secret-scan` (`drop-pattern`, `drop-redactor-only`,
  `whole-table`, `commit-noop`), `double-context` (`words`, appends the measured words so
  `python3 evals/phase_context_budget.py` exits 1). Exit 10 anchor missing, 11 ambiguous, 12 mutation changed
  nothing, each named on stderr; `anchors` lists every anchor. Reading `drop-redactor-only` as dropping the
  `authorization-header` entry of `_SECRET_PATTERN_SPECS` is an open point for #806. One new write-surface
  inventory row (`_write_text`). Not measured: Windows, Python 3.10.

- **Live regression entrypoint, slice 1: refusal ladder, NOT RUN rows, credential-safe logging (#884, refs #810).**
  New `evals/regression/live.py` (stdlib only). Run bare it exits 2 with one `live.py: REFUSED [no-credential]: ...`
  line on stderr, nothing on stdout, and one `sigma.regression-result/v1` NOT RUN row under
  `<repo root>/.sdlc/eval/results/` (anchored to the repo root, not the cwd; `--results-dir` overrides). The
  ladder is `no-credential`, `no-opt-in` (`SIGMA_REGRESSION_LIVE` in 1/true/yes/on), `no-cap` (dollars, from
  `--cap-usd`/`SIGMA_REGRESSION_CAP_USD`, optional `--belt-usd`), `unsupported-platform`, `pull-request-event`,
  `subscription-in-gated-mode` (gated = `CI`, `GITHUB_ACTIONS` or `--release-gate`); a run that passes all of them
  ends in the terminal `REFUSED [not-implemented]`, exit 2, never a silent exit 0. **The module starts no model in
  this slice** (AST tests ban process-spawning imports and calls; a fake `claude` on `PATH` is never run). Held
  credentials are redacted by exact value (raw, quoted, url-quoted, base64; values under 8 characters are not)
  before the shared scrubber. Deviation from the #810 design's "every log line through the scrubber": the
  scrubber is not run over a whole line because its `credential-assignment-suffix` rule rewrites the fixed
  `[no-credential]:` prefix; lines are fixed text plus a processed variable part. Row schema is a contract for
  #819/#820; on a NOT RUN row `cost` is null and `representative` is false, and a reader summing spend must skip
  null-cost rows, never count them as zero. Not exercised: Windows and the real hosted-runner behaviour.
  Measured: the 119 cases in `tests/test_regression_live.py` pass on Python 3.9; no timing claim made.
- **`github/process` variant in the onboarding control (#878, slice 4 of #809).** `tools/onboarding_control.py`
  drives Sigma's own writers end to end (phase boundaries, plan-review, a negative probe that must be
  refused with no PR in the fake store, a test-first red-then-green leg, the review chain with `post-review`,
  merge, done), builds a record with `evals/regression/record.py` and judges it with `check.py`: GREEN, or
  `RED at assert:<first failing property id>`. It is in the default `--variant all`; `--mode local --variant
  process` exits 2; it requires `python3 -m pytest` and exits 2 with `precondition missing` without it.
  `record.py` now records the plan-review gate's `pass`/`warn` as `approve` (`block` and unknown words verbatim;
  owner ruling 2026-10-10). The fake gh now models a field-only `POST` to issue comments. Measured on one macOS machine:
  the variant alone about 35 s, the whole plain gesture about 125 s; no CI figure.

## 1.0.6 — 2026-10-10 — branch upkeep (opt-in; off by default)

- **Upkeep part B, slice 10: Level 2 end to end against a fake (#951).** A new library, `feature_upkeep_level2.py`, runs eligibility and limits (protected paths, structural and merge-commit stops, file, hunk, line, stop and attempt limits), a plain export of the stopped tree, the capped resolver through the launcher, an engine-side write-back that refuses leftover markers, the level-2 stamp, the existing proof gate, the reviewer, and the existing atomic backup push; attempts and charges are recorded. Off by default and reachable only with the upkeep gate open, `conflicts.resolve` set to `agent` and a plan factory the shipped engine does not set, so with the gate closed every existing path is unchanged. The chat `--rebase` prompt and unit-lock bound change under the gate only (provisional). No real model call was made: every test drives a fake executable, the model flags beyond the confirmed table stay UNVERIFIED, and the limits are provisional and unmeasured.
- Upkeep part C, slice 10: chat `--unsafe-merge` routes through the landing engine and the unit approval when unit upkeep is on, records the requester and maps every engine outcome to a reply; closed gate unchanged; threat-model row TM-14 rewritten (#939).
- **Upkeep part B, slice 9: the independent reviewer route (#950).** A new library, `feature_upkeep_review.py`, with no caller yet and off by default. It runs a second headless session through the resolver launcher with a brief of base, ours, theirs and resolved contents and no resolver transcript, refuses the inline and subagent mechanisms, accepts only a strict verdict object (anything else, an unreadable reply and a timeout are block), neutralises the reasons, runs the leak gate over the brief, writes a write-once manifest under its own unit-keyed store with a pruner, and rejects a verdict when the tree moved. It can only add a refusal. The route is labelled verified false; it was run only against a fake command, never the real one.
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
- **The design-PR list and the review gate's CHANGES_REQUESTED half go REST first (#895, slice 4a-2 PR B; refs
  #895).** `work._find_design_pr` (merge_design / close_design) reads `gh_api.open_prs_for_head_gh` (list,
  `pulls/N`, `pulls/N/files`; ONE `gh pr list` fallback built in `gh_api`), and `work.review_gate` reads
  `gh_api.pr_changes_requested` (`pulls/N/reviews`, ONE `gh pr view --json reviews` fallback). `reviewDecision`
  stays one GraphQL `gh pr view` (REST cannot derive APPROVED) and `_unresolved_threads` stays GraphQL; the
  residual read is skipped when GraphQL is unavailable. THREE BEHAVIOUR CHANGES: (1) `require_review: changes`
  now fails CLOSED on an unreadable reviews read (it used to return pass); (2) in changes mode an unreadable or
  unavailable `reviewDecision` no longer returns pass and skips `_unresolved_threads`, which now still runs;
  (3) a CHANGES_REQUESTED followed by a COMMENTED from the same reviewer now blocks (stricter than
  `latestReviews`). Approval mode with GraphQL unavailable still parks, even with a `sigma:approve` comment (a
  known limitation). Fail closed: a design list of 30 or more rows, a non-list or blank body, a malformed
  detail or fallback row, an unknown review state, a full review page at the 10-page cap and a decisive review
  without `submitted_at`/`id` all raise; only a genuine `[]` is "no PR". Known divergence: `head=<owner>:` hides
  a fork PR from another owner (DERIVED), where the old list showed it and the identity check refused it. Also
  fixed: the `[:120]` slice in two `gh_api` error messages cut a tuple, not the string. Costs: design 1 + 2N
  calls, reviews `floor(n/100) + 1`. MEASURED: `{owner}` expands inside the query string from the cwd, and list
  rows carry null `mergeable`/`changed_files` while `pulls/N` carries them. DERIVED/UNMEASURED: review and files
  shapes and ordering, DISMISSED, the 429 body, any cloud run, latency. Ratchet 48 -> 47 by the printed
  `scan()` (work.py 8 -> 7). Remaining on #895: R5, writes (4b/4c). Verified with injected-runner tests only.
- **The merge gate and doctor's landing-PR row go REST first (#895, slice 4a-2 PR A; refs #801).**
  `work.gate()` (R1) now reads `gh_api.view_pr_gh` with two new whitelisted fields, `mergeable` and
  `mergeStateStatus`, then the new `gh_api.pr_check_rollup_gh` (check-runs, then commit statuses) for exactly
  the pull's own head; doctor's `_landing_pr_unverifiable` (R10) uses the new `gh_api.open_pr_for_branch_gh`
  and reads the rollup only for a CONFLICTING PR. One GraphQL fallback each (`gh pr view` / `gh pr list`,
  built inside `gh_api`), on a rate limit, 5xx or transport failure only, never in a cloud session. Fail
  closed: an absent or unrecognised `mergeable` / `mergeable_state` is an error, never CLEAN or BEHIND (BEHIND
  force-push-rebases); `mergeable_state: unknown` reads UNKNOWN; a completed check run with no conclusion, a
  truncated page (paged by `total_count`, capped at 10 / 5 pages) and a missing `statuses` list are errors; a
  fallback rollup for another head is refused. The gate keeps its posture (park), doctor its (no alarm; an
  unread rollup is never an empty one). The rollup read is retried on the gate's UNKNOWN budget for
  transient failures only, so one 502 in a cloud session does not park, while a permission 403 parks at once.
  Stated precedence change: a rollup still unreadable after its retries parks `could not read PR state` and
  hides STALE HEAD, UNKNOWN, CONFLICTING and BEHIND (a BEHIND PR then parks instead of rebasing; still fail
  closed). Costs (derived, not timed): 3 REST calls per clean gate round against 1 GraphQL call; with the
  breaker open, 2 GraphQL calls per round, not 1; doctor 2 calls per open unit, 4 if conflicted. MEASURED:
  orchestrator parity on 25 PRs (`mergeable` true/null and `mergeable_state` blocked/unknown agree 25/25;
  check-run names/states 25/25; `detailsUrl` on 2 PRs / 7 runs) and kubernetes/kubernetes statuses (2 PRs).
  UNMEASURED: `mergeable: false`, six `mergeable_state` values, the in-progress check-run shape, the first-GET
  null rate on open PRs, a real 429, latency, any cloud-session run, shape mixing across rounds, the
  `checks:read` 403, and GraphQL's EXPECTED contexts (required status contexts not yet reported), which REST
  `commits/<sha>/status` never returns, so a BLOCKED PR waiting only on one parks instead of arming (fails
  closed). R4 decided, not changed: under `require_review: approval` the `gh pr view --json
  reviewDecision,latestReviews` read stays (REST cannot derive APPROVED, MEASURED), so approval mode parks in a
  cloud session, never passes; under `changes` a failed read still passes and skips the CHANGES_REQUESTED and
  unresolved-thread checks (unchanged blind spot; only a `sigma:block` comment still blocks there). Ratchet
  50 -> 48 by the printed `scan()` (work.py 9 -> 8, doctor 6 -> 5). Remaining on #895: PR B (R9 design-PR
  list, R4's `latestReviews` half), then R5; `_unresolved_threads` stays GraphQL; PR writes are 4b/4c.
  Verified with injected-runner tests only; no live GitHub or cloud-session run.
- **Seven PR READ sites go REST first (#895, slice 4a-1; refs #801).** New `gh_api.view_pr_gh` (a REST
  `pulls/<n>` read, plus paged `issues/<n>/comments` for the marker scan) and `gh_api.pr_for_branch_gh` (one
  REST `pulls?head=<owner>:<branch>` read) replace `gh pr view` in `work.merge_rights`,
  `work._comment_directive`, `work.post_review`, `work._open_pr_refusal`, `work._pr_merged`,
  `doctor._stray_commits_after_merge` and `rebase_brief.pr_description`. Same fallback policy as the issue
  reads: at most one `gh pr view` fallback, only on a rate limit, 5xx or transport failure, never in a cloud
  session or with `SIGMA_GH_GRAPHQL=off`, never retried; issue and PR reads share one breaker and one
  bounded log. Each site keeps its posture on the REST and the fallback path alike: merge rights, the
  marker scan and `_pr_merged` fail closed, `post_review` parks, `_open_pr_refusal`, doctor and rebase_brief
  fail open. A fallback `gh pr view` that exits 0 with empty, non-JSON, non-object output, or an object
  missing a requested field, raises (never reads as an empty PR, which would mean "not a fork" and "no
  comments"). The field mapping is a closed
  whitelist (an unmapped field is refused before any call); a deleted fork reads as cross-repo; a comment
  list over 3000 raises and parks rather than truncate. Parity is UNMEASURED against live gh (the mapping is
  derived from GitHub's documented shapes). Named differences: the marker scan costs `2 + floor(n/100)` REST
  calls (was one GraphQL call); bot logins stay REST-spelled `x[bot]` on both sides of the same-author
  comparison; doctor's `head=<owner>:` lookup cannot see a fork PR on a same-named branch and assumes gh's
  open-first preference; a REST pull with no `auto_merge` key makes `finish`'s open-PR refusal fail open (a
  minor loosening on a reply GitHub is not known to send). The direct-`gh` ratchet drops from 57 to 50 sites
  (measured by `scan()`: work.py 14 -> 9, doctor 7 -> 6, rebase_brief 1 -> 0; the two small adapters that wrap `gh api` use a `_GH` alias, which the ratchet's rule 3 does not count by construction, so counted by intent the total is 52). Still open on #895
  after this slice: `reviewDecision` (R4), the sibling and design-PR lists (R5, R9), every PR write (W1-W8,
  including `--auto`) and the GraphQL review-thread read (the merge gate R1 and doctor's landing-PR list R10
  moved in slice 4a-2 PR A, above). Verified with injected-runner
  tests only; no live GitHub or cloud-session run.
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
- **Regression checker (#877, slice 2 of #870).** `python3 evals/regression/check.py <record> <properties.json>` reads one
  `record.py` record and prints `PASS`, `FAIL` or `NOT EVALUABLE` per property (outcome, process, cost, quality, safety
  families, listed in `evals/regression/properties.json`). Exit 0 when every property passes (a soft `NOT EVALUABLE` is
  allowed); exit 1 on any `FAIL` or any hard property whose source stream is absent; exit 2 refuses a broken input
  (unreadable or malformed record, wrong schema, malformed property list) so it is never read as a regression. Process
  properties read INTERNAL action-log rows only; a property naming an agent-writable kind is refused. One recorded run and
  three mutated copies (plan-review verdict, one phase end, review marker each dropped) are committed under
  `evals/regression/fixtures/` as controls of the checker itself. Honest limit: the evidence is call-existence from a
  synthetic run, nothing here was measured against a live run.
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
