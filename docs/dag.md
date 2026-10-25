# The dependency map

The full reference for the dependency map, Sigma Chain and how dependencies are read from issue text is [the dependency map and Sigma Chain](dependency-map-and-chain.md); this page keeps the detail it links to.

Sigma keeps one stored map of which goals and pull requests wait on which. This page says what the map
is, what it is not, how to switch it on, how to ask it questions, what it costs and where it is blind.
Every key and default is in the [configuration reference](configuration.md); the research on the host's
own relations is in [dag-native-relations.md](dag-native-relations.md) and the test that checks the
map against live text is in [dag-miss-test.md](dag-miss-test.md).

## What the map is, and is not

It is a graph of numbers: nodes (a goal or a pull request, its open or resolved state, its life, its
priority rank) and edges (blocker, blocked, and a code for where the edge was read). It is rebuilt from
GitHub on a schedule, stored in one local file, and read by the picker, `/sigma-status`, the doctor
and the commands below.

It is not a label writer. The `sdlc:blocked` and `sdlc:blocking` overlays (see the
[label model](label-model.md)) stay what the existing reconciler writes; the map is derived from
text and labels and writes none of them. It is not a second source of truth: when the map is missing,
stale, partial or unreadable, every reader falls back to exactly what it did before. It holds no
titles, no label text, no author and no comment text.

The root-cause line is the map's answer to "why is nothing pickable?". When a pick finds nothing, the
summary names the person-decision that holds the most work, one line per root, most freeing first. The
shape (each number is written with a hash sign and the issue number): `<a>, <b> wait on <root>'s decision; /sigma-unpark <root>`
for a parked root, `... /sigma-promote <root>` for an unarmed or unmanaged one, `... PR <root> is open`
for a pull-request root, and `<a>, <b> wait on each other` for a cycle. The word is "decision" for every
cause of a hold.

## Modes and keys

| Key | Default | Meaning |
|---|---|---|
| `discovery.dag.mode` | `shadow` | `off`, `shadow` or `on`. Anything else, including `OFF`, reads as `shadow`. |
| `discovery.dag.ttl_minutes` | `60` | Refresh cadence; a map older than twice this is not used unless a change check ran in the last ten minutes. |
| `discovery.dag.order` | `chain` | `chain` puts the goal that unblocks the most work first on both pick queues; `off` leaves the old order. |
| `discovery.dag.cycle_comment` | `true` | Whether a saved refresh posts one comment per distinct cycle on its lowest-numbered open member. |

- `off`: reads nothing, writes nothing, creates no file. The doctor row is absent.
- `shadow` (the default): the picker still decides exactly as before. At every pick Sigma also asks the
  map and appends one numbers-only record per compared candidate to the shadow log. Writing `shadow`
  or `on` explicitly also starts the automatic refresh, the doctor row and the session-start age line.
- `on`: the pick holds a goal whose open blocker the map knows about, when the map is usable and a
  change check ran in the last ten minutes. Otherwise it says why on stderr and takes the old path.

Longer descriptions of each key, including the edge sources and the guards on a new `Blocked by` line,
are in the [configuration reference](configuration.md).

## Reading the map

`/sigma-dag` is the one gesture: it runs `dag.py report` (`<N>` for the chain behind a goal, `--html` for the
offline pages) and prints the stored map's state, stuck and active chains, cycles and what finished this month,
or one line with the exact fix when there is nothing to read. It reads the stored file and writes nothing.
The verbs below are what it is built from. All of these read the stored file and write nothing. Run them from the project root, or pass
`--sdlc <dir>` to point at another `.sdlc`. Outputs below are from a six-node fixture map, with
issue numbers replaced by placeholders.

```
python3 skills/sigma-loop/scripts/dag.py show
dag: mode shadow, age 1m, nodes 6, edges 6, usable
python3 skills/sigma-loop/scripts/dag.py line
dependency map: age 1m, 6 nodes, 6 edges
```

`dag.py rebuild` and `dag.py refresh [--force]` read GitHub (needs gh) and publish the map; they are the
only verbs here that touch the network. `dag.py history [--month YYYY-MM] [--all]` prints the local
history records, with `--all` adding every writer's records from the ledger worktree. `dag.py rebuild` reads every outside number up to 500 (`--outside-max N`, hard cap 2,000, larger values clamped) while the periodic refresh keeps 25, and its report prints `unread N` with the command to run when N is not 0.

## See a chain: one HTML page per chain

`dag.py html` draws a chain so a person can see its shape. It reads only the stored map, makes no GitHub call
and writes one self-contained page per chain.

```
python3 skills/sigma-loop/scripts/dag.py html --goal <N> [--titles <file>] [--out <dir>]
python3 skills/sigma-loop/scripts/dag.py html --all [--titles <file>] [--out <dir>]
```

- **A chain** is one goal and the open prerequisites it waits on: the members Sigma Chain's plan reads from the
  map (the page calls the plan's own closure code, not a copy). `--all` writes a page for every end goal (the
  `ends` of `dag.py chains`) plus `index.html`: end goal, open members, depth, what holds it, how many need a
  decision and how far a run can get. Two end goals that share a prerequisite are two pages; a whole
  connected group is never drawn as one picture.
- **The picture.** Blockers on the left, the work that waits on them to the right, one column per depth, laid out
  in Python (an edge that crosses columns runs in a channel, the order in a column is swept to cut crossings).
  A header names the goal, members, depth, what holds the chain and the forecast line; a legend lists the
  states and edge styles; click a member for its number, title, state, why it is held, what it waits on and what
  it frees. Zoom and pan with the buttons, the wheel and a drag; `Fit` shows the whole chain. Light and dark
  themes follow the system, with a `Theme` button.
- **The work that waits on the goal.** A `--goal` page also draws the goal's open dependents to its right, muted and
  with edges tagged `holds`, at most 40 (the rest are counted on the page); the heading says how many wait on it.
- **States** use the plan's own markers (`ready now`, `after #N`, `needs decision`, `someone else's`,
  `checkpoint`, `not workable`) plus `closed` (hidden behind "show closed", on the left), `outside` (an open pull
  request) and `cycle`. From the map alone only a member's life is known, so the markers are the map's reading;
  the live plan also sees assignees, checkpoints and not-a-goal labels, and the page says so. Edges: solid for
  one written in an issue body, dotted for any other recorded source, dashed for an unconfirmed wait (live plan
  only: a map page has none).
- **Titles.** The map holds no titles and never will; they come from a file beside it. `dag.py titles` reads the
  open issues' titles once (REST, one call per 100 issues, at most 50 calls: 10 at 1,000 open issues, the cap at
  5,000 and over, and it says how many were left out) and writes .sdlc/state/dag-titles.json atomically; a failed
  listing keeps the old file. Only numbers in the map are kept, and a closed member's title is carried over from the
  earlier file. `dag.py html` reads that file when it exists and makes no GitHub call; `--titles <file>`, a JSON
  object `{"<number>": "<title>"}` (or a list of `{number, title}`, 4 MiB at most), wins when given. When the file's
  `read_at` is older than the map's `built_at`, the page says so. Run `titles` before `html`. A title is shortened to
  100 characters and any URL is removed. No issue body is read or written.
- **Assignees.** The map keeps each member's assignees (from the issue listings the refresh already reads: no extra
  call; at most 20 per member, each a plain login or dropped). A member's node and its panel show them, or
  "unassigned" for an empty list; a map written before this field draws "assignee unknown (map predates this
  field)", because an absent field is not an empty one. A node cached from a map written before assignees existed keeps none, so it reads "assignee unknown" until a refresh lists its issue again (listed members every refresh; closed outside nodes up to seven days). An open outside issue (named by an edge, carrying none of the five list labels) keeps its assignees, priority and life from the one read it already costs; a closed or merged one has none of the three. A login is scrubbed like a title and is text, never a link.
- **Index controls.** Each index row carries the top priority among its open members and the set of their
  assignees (with `unassigned` and `unknown` as filter values). Sort by priority, open members or goal number;
  filter by assignee and by priority. One inline function, `applyView`, does it; no network, no external file.
- **Index growth.** Per row about 220 bytes (the row plus its four data attributes) and about 50 bytes per
  distinct assignee login in the filter list, measured on synthetic rows: 100 chains 22 KB, 1,000 chains 222 KB
  (10x), 10,000 chains 2.26 MB (100x). Linear in chains and logins; the page filters in the browser, so a 100x
  index is also a 100x DOM. Not measured on a real board.
- **Offline.** One file, inline SVG, CSS and a little JavaScript; no library, no network at view time, no link.
- **Where it goes.** `<sdlc>/state/dag-html/` by default (gitignored). `--out` inside a git checkout that does not
  ignore it is refused, so a page never dirties the root checkout. An earlier page is overwritten, never deleted.
- **Bounds.** A page is readable to 80 open members (`MEMBER_CAP`); a larger chain is still written, with a notice
  at the top. Measured on one synthetic shape (shallow, no tangle): 80 members 58 KB, 400 members 248 KB in
  0.06 s, 1,000 members 612 KB in 0.35 s; a tangled chain was not measured. `tests/test_dag_html.py` bounds a
  400-member page at 1.5 MB. `--all` costs the sum of its chains. A map that is not fresh is still drawn, with the
  reason at the top. Exit 0 written, 2 the goal is unknown, closed or a pull request, 3 no readable map, 4 mode off.

## The chain query contract

`dag.py chain <goal> --json` prints the full chain of a goal: every unresolved transitive blocker,
blockers first and the goal last. It is the interface other tools should call.

- Schema `dag-chain/1`. Changes are additive only: a key may be added, never renamed, removed or
  re-typed, and a closed vocabulary may gain a value but never lose or re-mean one. A consumer must
  ignore keys it does not know.
- Top-level keys: `schema`, `goal`, `mode`, `source` (`map` or `map+verified`), `map_built_at`,
  `map_age_seconds`, `closure_id`, `complete`, `members`, `waves`, `decisions`, `cycles`, `counts`,
  and `verify` only when asked.
- `members` are in topological order; ties go to the more urgent priority, then the lower number. A
  shared blocker is listed once. `waves` group members that have no path between them.
- `decisions` lists the members that need a person, with a `why` (`parked`, `unarmed`, `unmanaged`,
  `cross-repo`, `unknown`, `cycle`) and a hint.
- `closure_id` is a hash of the sorted number, state and life of every unresolved member. A caller that
  polls compares it: a member added, resolved or changed in life changes it; nothing else does.
- `--verify` re-reads each open member live (at most 50 reads, blockers first), drops those now
  resolved and says so under `verify`. Past the cap or on an unreadable member `complete` is false.
  This needs gh. `--include-resolved` lists resolved blockers too.
- Exit codes: `0` a chain, `2` the goal is not in the map, `3` the map is unusable (missing, stale,
  partial, unreadable), `4` the mode is `off`.

Seen on the fixture: the plain form prints a header, one line per wave and one line per decision; the
`--json` form had the keys above, `schema` equal to `dag-chain/1`, `source` equal to `map`, four waves
of one member each, one `parked` decision, and `complete` true. A goal that is not in the map printed
`dag: goal 99 is not in the map` and exited 2; a missing map printed `dag: map unusable (missing)` and
exited 3.

## Unit view

`dag.py report --feature <unit>` (and `--json`) lists one unit's open members. Each member shows its open
blockers, and a blocker outside the unit is marked `(outside unit)` and listed again under "Outside
blockers", so a member held from outside never reads free. A closed blocker is not shown. A unit the map does
not know exits 2 with one line naming the units it does know; a known unit with no open member says so and
exits 0. `dag.py html --feature <unit>` writes one page, `unit-<unit>.html`, with the outside blockers drawn
muted and in the legend. Both read the stored map only; `--feature` cannot be combined with `--goal`.

## Stuck chains

`dag.py stuck` lists decision roots holding other work, most freeing first; `dag.py chains` lists
connected groups of two or more open members (`--rank longest` or `--rank biggest`, `--json` for both).
A chain is stuck only when a leaf of its open-blocker subgraph needs a person; a chain whose leaves
are ordinary open goals is not stuck, the loop picks the leaf. The same lines appear in the pick summary,
in `/sigma-status` and in the doctor row (as a count of stuck roots).

## Cycles

A cycle is a set of goals that wait on each other, so none can start. The map finds them on every
refresh. They are reported (pick summary, status, doctor, `decisions` with `why` equal to `cycle`) and
held; a `Blocked by` line that would close a cycle is refused before it is written. With
`discovery.dag.cycle_comment` on, a saved refresh posts one comment per distinct cycle on its
lowest-numbered open member (numbers and a fixed instruction only, with a marker that stops a repeat).
Turn it off and the cycle is still reported, only the comment is not posted.

## From shadow to on

The flip from `shadow` to `on` is an operator act, made in a release after 1.0.17, never automatic.
The criterion is on the doctor row `dag shadow`:

```
dag shadow: <n> compared picks, <n> disagreements (pr <n>, comment-only <n>, cross-repo <n>, unknown <n>), <n> days in shadow, flip criterion met|not met (needs >=7 days, >=200 distinct picks, 0 unknown)
```

Read it this way. `compared picks` counts distinct (goal, minute) pairs, not log lines.
A disagreement is a pick where the old path and the map differ about a blocker, split by cause: `pr`
(a pull request blocker the board mirror does not see), `comment-only` (an edge written only in a
comment), `cross-repo` (a blocker in another repository) and `unknown` (no cause found). Causes other
than `unknown` are explained; the criterion holds when the log spans at least 7 days, holds at least
200 compared picks and has no `unknown` disagreement. Flip when the row says `met` and you have read the
explained causes and accept them. The row measures and gates nothing by itself. The separate row
`dag map: mode ..., age ..., N nodes, N edges, N cycles` is a liveness row: it fails when the last good
refresh is over three TTLs old while a loop or watcher is live, or when the last two refreshes errored,
and its fix is `dag.py refresh --force`.

## Privacy

Only numbers, booleans, timestamps and codes from closed sets are stored; a title, a label text, an
author or a comment phrase handed to the writer never reaches disk. Where things live:

- `.sdlc/state/dag.json`: the map (gitignored with the rest of `.sdlc/state/`).
- `.sdlc/state/dag-shadow.jsonl`: the shadow log, rotated at 1 MiB.
- `.sdlc/state/dag-history/`: monthly append-only history files, never pruned by Sigma.
- With the ledger on only: the ledger branch carries `dag/` history files, and a generated `DAG.md`
  at its root, rendered from every writer's records.

## What a refresh costs

Measured by the author of the graph core and recorded as a comment on the dependency-map epic (the
recorded measurement; it is not a test or a file in this repository): graph build, closure and components
on one laptop-class Mac, a single run of each, in seconds.

| Nodes | Chain | Fan-in | Fan-out | Dense random with cycles |
|---|---|---|---|---|
| 3,000 | 0.003 | 0.003 | 0.003 | 0.017 |
| 30,000 | 0.048 | 0.038 | 0.036 | 0.385 |

Not measured: cycles, order and waves separately; any other machine; repeated runs; the cost of each
stage at pick time; and the number of GitHub reads a refresh makes against a real repository. Those
are unmeasured, not small. One fixture measurement of the earlier unpark pass: with 25 blocked
fixture issues, 25 comment reads and 25 state reads with the map off, and 0 and 0 with it on; the
whole-sweep call count falls only by the unpark pass.

## Limits

- Edges that exist only in a comment, and weak phrases, are not in the map; they show up as `comment-only`
  disagreements in shadow. `deps.py report` lists them per goal (`deps.py report <sdlc_dir> --label <label>` or `--chain <N>` does a whole label or chain in one process, one JSON line per member), and `deps.py mark` writes one into the body as
  a `Blocked by` line, which the map reads. The map also reads `blocked on` and `blocked until`, and the
  follow-up forms (`follow-up to` and its variants, with nothing but spaces before the number), from a body.
- A priority change made only on the board is seen only by the hourly read.
- Each label list is read up to 2,000 issues. Reaching the cap marks the map partial, and the picker
  ignores a partial map.
- The label queue sorts the 200 oldest matching issues, so a deeper backlog cannot see a newer chain
  root until older goals drain.
- The refresh is one writer at a time by design; a sibling refresher reads the existing file and does
  not wait.

## The snapshot on the ledger branch

With `ledger.enabled` true and the mode not `off`, each save of the stored map also writes `dag/map.json` in the ledger worktree: the same fields as `.sdlc/state/dag.json` plus a host token, whole file, last writer wins. `sync.py publish` pushes it, and `DAG.md` is built from it (with `Built by` and `Snapshot age` lines) instead of from history alone. A missing, damaged or older local map loads the newer snapshot; with the ledger off nothing is written or read there. Measured only by offline fake and real-git tests. Each history record carries `u` (the source issue's `updated_at`) and an `id`, a short hash of the event, its numbers and `u`; `dag.py history --all` and the other ledger readers apply each id once, and the snapshot records `history_through`, the newest id it includes, so a reader applies only later entries. An older record without an id gets the same one computed on read.

### One refresher per repository

With the same two conditions, a machine that is due first takes a lease (`dag/lease/<writer>.json` on the ledger branch, one file per writer, valid for the TTL plus a margin of the larger of five minutes and 10 percent of the TTL), reads GitHub, and publishes the snapshot. Another machine folds the snapshot in and makes no GitHub call while the snapshot is younger than the TTL or an unexpired lease is held by someone else; two machines that write at once both see the same two files after the next pull and agree on the earliest. A holder that dies simply lapses and the next due machine takes over. `dag.py refresh --force` and `dag.py rebuild` read on this machine regardless and take the lease. With the ledger off nothing changes and no lease is written.

Reads per hour per repository, computed on the test scenario (two machines, a 60 minute TTL, a tick every five minutes for two hours): before, each machine reads once an hour, 2 in total per hour (4 collections in the two hours); after, the team reads once an hour (2 collections in the two hours). The live number across real machines is not measured. Scale: the lease directory holds at most one small file per writer, so it grows with machines, not issues.

### Who refreshes, on the doctor and the report

With the ledger on, the doctor's `dag map` row and `dag.py report` end with the map's source (`ledger` when the snapshot was newer than this machine's own file, else `local`), the snapshot's own age and builder, and the lease holder with time remaining, marked `(this machine refreshes)` when it is you. The row fails, naming `dag.py refresh --force`, when the snapshot is over three TTLs old, no unexpired lease exists and a watcher heartbeat or loop session is live; a snapshot that old under a live holder is a slow refresher and is not failed (provisional, owner to ratify). Local files only, no GitHub call. With the ledger off the row is unchanged.
