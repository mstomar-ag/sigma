# Configuration reference

Every feature flag in `.sdlc/config.json` and its default. Back to the [README](../README.md).

## Feature flags at a glance

Defaults below are what `/sigma-init` scaffolds (`config.json.tmpl`). Not everything optional ships off — e.g. `discovery.dependency_gate`, `discovery.auto_unpark`, `review.independent`, `handoff`, `work.rebase_upkeep`, `work.enabled` and `work.require_review: "changes"` ship on (`verify.enforce` ships off and turns on when `/sigma-init` records a confirmed verify command), and so do the iteration, minute and token budgets (`max_codex_raw_tokens` ships `0`, off). In github mode `/sigma-init` asks about the ledger and `--yes` writes `false`; `/sigma-setup` writes `ledger.enabled` as `true` only where the key is still `null` or absent (for example after a bare local-goals init), and never overrides an explicit `true` or `false`. What holds each control, and where: [docs/enforcement.md](enforcement.md). `/sigma-doctor` prints this dashboard live (`doctor.py features`):

| Flag | Default | What it turns on |
|---|---|---|
| `model_selection: "auto"` | off | per-goal model ceiling + per-step model/effort downgrade |
| `verify: {"enforce": true}` | off until a command is confirmed (an empty command would refuse every `done`) | `record done` refused without fresh machine evidence (`loop.py verify`) |
| `gates.hard_plan_gate.enabled` | off | unplanned source refused: the PR push on every host (`work.py pr`, needs `work.enabled`), plus the edit itself on Claude Code (`hooks/plan_gate.sh`, `plan_freshness_hours` window) |
| `gates.plan_review.enabled` | off (`gates.phase_record`, on in a fresh `/sigma-init`, already makes `loop.py record done` and `work.py merge` refuse without an approved plan-review) | the PR push refused on every host (`work.py pr`, needs `work.enabled`) unless `work.py record-plan-review` recorded an approving plan-review verdict (SOUND / SOUND-WITH-REFINEMENTS) for the exact plan bytes on the branch; covers `.sdlc/plans/<stem>.md` only |
| `decision_tier: "auto"` | off | classify a `needs_decision`/`irreversible`/`unknown` park's detail text into an L0/L1/L2 escalation tier (`escalate_l0`/`escalate_l1`/`autonomous`, `decision_tier.py`) and surface it in the ledger `park` event, `review-queue.md`, and the park comment — **advisory only**, it never changes what the loop does with the parked goal |
| `.sdlc/pipeline.json` | absent | the bidirectional report card + `propose` (findings → groomable goals) |
| `ledger: {"enabled": true}` | unset (`null`, reads as off; `/sigma-setup` writes `true`) | the committed team ledger — claims and outcomes recorded per author, plus cross-area hand-off |
| `ledger.watch.interval_seconds` | 900 | how often `watch_daemon.py` pulls the ledger ops branch and refreshes the inbox |
| `ledger.publish_on_write` | on | a ledger write you TYPE (`ledger.py append`, `handoff.py open`/`track`/`ack`) publishes itself, and says on stderr whether the team can see it — set `false` to keep the push local and leave it to the watcher |
| `upkeep: {"enabled": true}` | off (the block is scaffolded with `enabled` false; an existing adopter copies it in) | keeps the old tip of a rewritten unit branch as a backup ref, adds the restore and prune commands, and with `ledger.enabled` and `SIGMA_UPKEEP_JOB=1` on one machine lets the ledger watcher start one bounded maintenance pass per tick. Run once end to end on a private throwaway repository, without a real model, a chat service or branch rulesets; see `docs/launch/evidence/upkeep-hosted-run.md` and `docs/branching-model.md` |
| `action_log: {"enabled": true}` | on | a full local, gitignored trace of loop activity per goal (`.sdlc/state/log/<goal>.jsonl`) — read via the `sigma-log` skill; never touches the shared ledger either direction |
| `agent_watch: {"enabled": true}` | off | background-agent-death watch — a claimed goal's registered pid confirmed dead notifies (email if `notify.email` is also configured, else always a ledger note); needs `ledger.enabled` too, since `watch_daemon.py` is what runs the check |
| `comment_watch: {"enabled": true}` | off | in-flight comment watch — a new comment on a claimed issue notifies the claimant via a ledger note (self-comments suppressed); needs `ledger.enabled` too, since `watch_daemon.py` is what runs the check, and github discovery (comments aren't a concept for local goal files) |
| `ledger.autowatch: {"enabled": true}` | off | ledger-triggered autonomous tick (core in `autowatch.py`): on a NEW unacked ledger hit matching `scope` (mentions/assignments/blockers) addressed to you, drives `/sigma-loop` scoped to that ONE flagged issue — never an open-ended `next-batch` pick — after every `preconditions` check passes (`require_gh_auth`, `no_concurrent_session`, an optional `load_average_ceiling`, an optional `spend_ceiling_tokens_per_week` against a local heuristic estimate that can't see other surfaces' spend); `hop_limit` caps chained autowatch-triggered runs; needs `ledger.enabled` too, since `watch_daemon.py`'s own tick runs the check. `surface` ("desktop" default, or "cli") picks which of the two adapters this machine runs — a Desktop scheduled task, or a CLI/Channels plugin. `channel_webhook_url` accepts only `http(s)` loopback hosts by default; set `allow_remote_webhook: true` only as an explicit remote-delivery opt-in. Setup steps for both: `skills/sigma-loop/AUTOWATCH.md`. |
| `parallel: {"enabled": true}` | off | a goal's independent slices run concurrently in waves (`max_concurrent`, default 3) from `.sdlc/plans/<goal>.slices.json` |
| `parallel: {"goals": {"enabled": true}}` | off | `next-batch` returns up to `max_concurrent` (default 3) BACKLOG GOALS at once for one person's own concurrent subagents, one worktree+PR each — size `max_concurrent` to memory, not just to open-issue count: see [Memory & sizing](parallelism.md#memory--sizing-read-before-running-many-goals-in-parallel) |
| `backlog_check: {"enabled": true}` | off | pre-work cross-check: parks a picked goal that duplicates / is obsoleted-by / is blocked-by other backlog items, before any token spend — includes a bounded comment-read fallback for a human-authored, comment-only dependency marker; a goal marked as a decomposition child/meta-goal (first-line `sigma:decomposed-from=`/`decompose-of=`) is exempt from the duplicate/obsolete/in-flight-similarity signals, never from an explicit blocker or a recorded hand-off; `/sigma-doctor` flags one that's still silently ignored |
| `goal_decompose: {"enabled": true}` | off | pre-work oversized-goal classifier: a zero-LLM check flags (`mode: "log"`, default) or parks (`mode: "park"`) a picked goal whose body reads like an epic, before any token spend; `mode: "file"` additionally files one idempotency-guarded "Decompose #N" meta-issue before parking (`max_children` caps its own later split); thresholds are corpus-calibrated against this repo's own issue history (see `goal_size.py`); a decomposition child/meta-goal is exempt by construction |
| `work: {"enabled": true}` | on (older configs may still hold unset/`null`, which reads as off) | one worktree + branch + PR per goal; your checkout never moves, and `verify_command` runs in the goal's own tree |
| `work.auto_merge` | `"off"` | `"protected"` merges only where the base *requires* checks/reviews; `"always"` merges any clean+safe PR. A fork or read-only repo never merges — it opens the PR and records `review`; the goal is `done` once the PR merges |
| `work.require_review` | `"changes"` | a real PR-review gate, independent of branch protection: `"changes"` parks on a Request-changes / unresolved thread; `"approval"` also requires an APPROVED PR before merging |
| `budget.max_iterations` / `max_minutes` / `max_tokens` / `max_codex_raw_tokens` | 20 / 480 / 100,000,000 / 0 (off) | goals-per-session / wall-clock / priced Claude-equivalent / measured Codex raw phase-token admission ceilings; each enforces only when set to a positive number |
| `knowledge_graph.enabled` | off | research capture + the self-improving graph |
| `SIGMA_GATE_GLOBAL=1` (env) | unset | restores the always-on prompt gate (the reminder in every repository, adopted or not) |

> For any zero-touch / unattended multi-issue run, turn `backlog_check.enabled: true` on — it is
> what makes a human-authored, comment-only dependency marker (bypassing `hand_off()`) actually
> honored, not just silently ignored.

## Per-category autonomy thresholds

The autonomy ladder reads one global set of thresholds, `decision_rubric.autonomy.levels`. A
repository can tune them per category with the optional file `.sdlc/categories.json`, a sibling of
`.sdlc/rulebook.json`. The file is opt-in: the template ships none, and with no file (or the autonomy
gate closed) behaviour and output are unchanged.

Schema `sigma/categories@1`: a `schema` string and a `categories` list. Each category has an `id`
(unique), a `title`, the `areas` it covers (the repository `area:*` names, with or without the
`area:` prefix), optional `qkinds` it is limited to, and an optional `levels` block using the same
keys as the global one (`l1_min_answers` to `l4_min_answers`, `l2_agreement` to `l4_agreement`).

Precedence, per threshold key: the global default, then the first category (in file order) whose
`areas` contain the pair's area and whose `qkinds`, when given, contain its kind. A category
overrides only the keys it names; a pair in no category behaves as before.

The categories reader never raises. A missing file, wrong schema
or unreadable JSON reads as no categories; an entry with a missing or duplicate id (the first wins),
an unknown threshold key or a wrong-typed value is dropped on its own. `autonomy.readout` and the
doctor autonomy row show the matched category and the thresholds in force once the file exists.
