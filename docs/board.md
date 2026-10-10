# The Sigma board: fields, six views, and how to build it (#234)

The board is where a person sees the loop's state without asking an agent. It has to pass the same
test as the [output contract](output-contract.md): from one glance, which goal, which phase, what is
happening now. One Status column and one default table (what board #17 had) cannot do that. This
page is the design, the runbook that builds it, and the acceptance checks.

The design lives in code, in one place: `skills/sigma-init/scripts/board_spec.py`. It is built from
the kit's own vocabulary at run time: the configured Status columns, `discovery.PRIORITIES`,
`phase_report.PHASE_TOKENS`, and the configured parked / blocked labels and the legacy needs-confirmation label. If you
rename a column in `project.columns`, the filters follow it. `board_layout.py spec <.sdlc>` prints
the spec as JSON.

## Fields

| Field | Type | Values | Who writes it |
|---|---|---|---|
| Status | single-select | Backlog, Ready, In Progress, QC, Done, Blocked, Parked (`project.columns`) | the loop, on every status write |
| Priority | single-select | P0, P1, P2, P3, P4 | the loop, from the `priority:*` label ([board-fields](board-fields.md)) |
| Phase | single-select | P1 GOAL .. P7 RETRO | the loop, at every phase start |
| Area | text | from `area:*` | **nobody yet**: the field is created empty (see "Not done here") |
| Model tier | text | the predicted tier | **nobody yet**: the field is created empty |

Built-in fields the views show: Title, Assignees, Labels, Linked pull requests, Sub-issues progress,
Milestone, Updated. Every board has them, and nothing here creates them.

**Priority is P0..P4, not the issue's P0..P3.** The options come from `discovery.PRIORITIES`, the
same list the loop's queue ranks by and the priority mirror writes. A four-option field would leave
every P4 goal blank on the board. #235 made the same ruling.

## The six views

1. **Board · by Status** (the default: the leftmost tab). A kanban with one column per Status lane.
   Cards show Priority, Phase, Assignees and Linked pull requests, sorted by Priority within each
   column. This is the glance view: each card's lane is its state, and its Priority and Phase say
   which goal and which phase. No filter, so Done stays visible. If Done grows too long, turn on
   GitHub's auto-archive workflow; this spec does not filter it out.
2. **Priority table**. A table of open items (`is:open`), grouped by Priority, with Title, Status,
   Phase, Area, Assignees, Linked pull requests and Updated. It answers "what should we work on":
   P0 first, the order the loop's queue ranks by.
3. **In flight**. A table filtered to `is:open status:"In Progress",QC,Blocked`, with Title, Phase,
   Linked pull requests, Assignees and Updated, sorted by Updated, newest first. This is the
   operator's live view, the board's version of Block A: what is being worked on, reviewed or stuck,
   most recently touched first.
4. **Needs a human**. A table filtered to
   `is:open label:sdlc:parked,sdlc:blocked,sdlc:needs-confirmation`, with Title, Priority, Status,
   Labels and Updated. It is the inbox for `/sigma-unpark`. The legacy needs-confirmation value stays
   in the filter only so leftovers on an older board still show until `/sigma-promote` migrates them.
   **This differs from the issue on purpose.** The issue asked for "Status in (Parked, Blocked) OR
   the legacy needs-confirmation label". A project filter ANDs its qualifiers and ORs only the values inside one
   qualifier, so an OR across two fields cannot be written. The labels are the source of truth that
   the Blocked and Parked lanes mirror, so one `label:` qualifier expresses the same set.
5. **v1.0 roadmap**. A roadmap filtered to `priority:P0,P1`, grouped by Milestone, with the
   milestones' due dates as markers, and Title, Status, Priority and Milestone in its table. This is
   the plan for v1.0. It shows only items that carry the `v1.0` milestone, which is why step (b)
   below exists.
6. **Epics**. A table filtered to `label:epic`, with Title, Sub-issues progress, Priority and
   Milestone. It shows each epic and how far through its sub-issues it is.

## What the API can and cannot do

Checked on 2026-09-29 by reading GitHub's GraphQL schema (`__schema` / `__type` introspection),
through a runner that refused any mutation. Evidence:
the schema-introspection output recorded on #234. Board #17 was read the same way
(`board17-readonly.json`, `board17-verify-readonly.txt`). **No mutation was sent to GitHub by this
goal.**

| Property | Settable? | How |
|---|---|---|
| Create a view with a name and layout (board, table, roadmap) | yes | `createProjectV2View(projectId, name, layout, configuration)` |
| Visible columns / card fields, in order | yes | `configuration: {visibleFieldIds: [...]}`. That is the configuration input's only member |
| Filter | yes, **but only by an update** | `updateProjectV2View(viewId, filter)`. The create input has no `filter` |
| Group by, board column field, sort, roadmap date fields and markers | **no** | no input exists. They can only be read (`groupByFields`, `verticalGroupByFields`, `sortByFields`) |
| Which view is the default (tab order) | **no** | no input exists |
| Delete a view | yes (`deleteProjectV2View`) | never called by Sigma |
| Create, enable or edit a workflow | **no** | the only workflow mutation is `deleteProjectV2Workflow` |
| Copy a board | `copyProjectV2(projectId, ownerId, title, includeDraftIssues)` | the schema describes it only as "Copy a project." What survives a copy is **not measured** (control (c) below) |
| Mark a board as a template | `markProjectV2AsTemplate` | organisation-owned boards only |

The older statement that "views cannot be created through the API" (#235's research) is half right:
the mutation exists, but the half of a view that makes these six different (grouping, sorting,
columns, the default) cannot be set through it. So `board_layout.py views` sets what it can and
prints each remaining property as an exact UI step. It prints a step only while the board still
reads differently, so it goes quiet once you have done it.

Not measured, because each would need a mutation on real GitHub: whether GitHub rejects two views
with the same name (the code never relies on a rejection: it refuses to touch a name that several
views answer to); whether `visibleFieldIds` accepts every built-in field on every layout; and the
exact filter-syntax acceptance for a quoted multi-word value such as `"In Progress"`. The owner's
first real run is that measurement. `verify` reads the result back and names anything that did not
take.

## The tools

```
board_layout.py fields <.sdlc> [--number N] [--owner O] [--yes]   # Priority, Phase, Area, Model tier
board_layout.py views  <.sdlc> [--number N] [--owner O] [--yes]   # the six views
board_layout.py verify <.sdlc> [--number N] [--owner O]           # read-only acceptance check
board_layout.py spec   <.sdlc>                                    # the spec as JSON, no gh call
```

(`python3 <installed-sigma>/skills/sigma-init/scripts/board_layout.py ...`; on Windows use `py -3` or `python`.)

- **A dry run by default.** Without `--yes`, nothing is written: the run reads the board and prints
  `[plan]` lines.
- **Only on your say-so.** It acts on the pinned board only if `board_setup.py create` made it
  (`project.setup_created`). For any other board, pass `--number N`.
- **Never deletes, never duplicates.** A view the spec does not name is never changed. A spec view
  that exists is matched by name (exact, else a single case-only variant), and only the properties
  that differ are updated. Columns you added to it are kept, after ours. If several views answer to
  one name, that view is `[REFUSED]` and none of them is changed. Fields follow the loop's rules
  from [board-fields](board-fields.md): an existing field is adopted, never renamed, and an option
  list is never rewritten. A missing option is a `[manual]` step.
- **Exit codes.** 0 done, or a dry run. 1 a `[FAIL]`: re-running is safe. 2 `[REFUSED]`: change the
  named thing by hand first. For `verify`, 1 means something differs.
- **Cost.** Each verb makes 3 reads: owner, board id, and one GraphQL query for fields, views and
  workflows. Measured read-only on board #17 (246 cards): 1.6–1.8s. `fields --yes` on a fresh board
  adds 4 mutations. `views --yes` adds 11 (six creates, plus five filter updates) and 1 re-read. The
  reads do not touch cards, so a board with 10x or 100x the cards costs the same. The reads see the
  first 50 fields (GitHub's cap) and the first 50 views.

## Runbook (OWNER ACTIONS)

Everything below writes to real GitHub, so the goal that built these tools did not run it. Run it
yourself, in order. Each step says what to record.

### (a) Build board #17

```bash
cd <your sigma checkout>
gh auth status                    # must list the 'project' scope; else: gh auth refresh -s project
S=skills/sigma-init/scripts
python3 $S/board_layout.py verify .sdlc --number 17          # before: expect 1 of 12 checks
python3 $S/board_layout.py fields .sdlc --number 17          # dry run: 4 [plan] lines
python3 $S/board_layout.py fields .sdlc --number 17 --yes
python3 $S/board_layout.py views  .sdlc --number 17          # dry run: 6 [plan] lines
python3 $S/board_layout.py views  .sdlc --number 17 --yes
```

Then do each `[manual]` line `views` printed, in the GitHub UI. On a fresh board there are six:
- Board · by Status: Column by Status, Sort by Priority ascending.
- Priority table: Group by Priority.
- In flight: Sort by Updated descending.
- v1.0 roadmap: Group by Milestone, and Markers → Milestones.
- Drag the "Board · by Status" tab to the far left.

Turn on the "Item closed" workflow too: `https://github.com/orgs/<owner>/projects/17/workflows` →
Item closed → Edit → Status: Done → Save and turn on workflow. Finally:

```bash
python3 $S/board_layout.py verify .sdlc --number 17          # after: expect 12 of 12, exit 0
```

**Record** the before and after `verify` output on #234. Any line still marked `[differs]` is a
measured gap in the API claims above. Say which property it was.

### (b) Put every open P0/P1 issue in the `v1.0` milestone

The roadmap view shows only items that have a milestone. Run this in bash (Linux, macOS, or Git Bash
on Windows). It **never overwrites** a milestone an issue already has.

```bash
REPO=Agrim-Intelligence/sigmaloop
M=$(gh api "repos/$REPO/milestones?state=all&per_page=100" --jq '.[] | select(.title=="v1.0") | .number')
echo "v1.0 milestone number: ${M:-<none>}"
# Only if that printed <none>:  gh api -X POST "repos/$REPO/milestones" -f title=v1.0
# Dry run: every open P0/P1 issue and its current milestone ("-" = none)
for P in P0 P1; do
  gh api --paginate "repos/$REPO/issues?state=open&labels=priority:$P&per_page=100" \
    --jq '.[] | select(.pull_request | not) | "\(.number)\t\(.milestone.title // "-")\t\(.title)"'
done
# Apply: only the issues with no milestone
for P in P0 P1; do
  gh api --paginate "repos/$REPO/issues?state=open&labels=priority:$P&per_page=100" \
    --jq '.[] | select(.pull_request | not) | select(.milestone == null) | .number'
done | sort -un | while read -r N; do
  gh api -X PATCH "repos/$REPO/issues/$N" -F milestone="$M" --silent && echo "set #$N"
done
```

PowerShell equivalent of the apply loop:

```powershell
$Repo = "Agrim-Intelligence/sigmaloop"; $M = <the number printed above>
"P0","P1" | ForEach-Object { gh api --paginate "repos/$Repo/issues?state=open&labels=priority:$_&per_page=100" --jq '.[] | select(.pull_request | not) | select(.milestone == null) | .number' } |
  Sort-Object -Unique | ForEach-Object { gh api -X PATCH "repos/$Repo/issues/$_" -F milestone=$M --silent; "set #$_" }
```

**Record** how many issues were set, and list any P0/P1 issue that already had a different
milestone, since those were left alone on purpose.

### (c) The copy control: what does `copyProjectV2` keep?

Do this after (a) passes. `gh project copy` is the bare `copyProjectV2` call. Do **not** use
`board_setup.py create --template` here: it adds fields and links a repo after copying, which would
hide what the copy itself carried.

```bash
gh project copy 17 --source-owner Agrim-Intelligence --target-owner <you or a scratch org> \
  --title "copy-probe-234"                 # prints the new board's URL; note its number C
mkdir -p /tmp/copy-probe/.sdlc
printf '%s\n' '{"discovery": {"source": "github", "github": {"repo": "Agrim-Intelligence/sigmaloop", "project": {"owner": "<target owner>"}}}}' > /tmp/copy-probe/.sdlc/config.json
python3 $S/board_layout.py verify /tmp/copy-probe/.sdlc --number C
gh api graphql -f query='query { <organization|user>(login: "<target owner>") { projectV2(number: C) {
  workflows(first: 30) { nodes { name enabled } } views(first: 20) { nodes { name layout filter } } } } }'
```

**Record on #234:** the full `verify` output (it lists each field and view, and whether filter,
columns, group, sort, column-by and the default survived), the workflow list (name + enabled) set
beside #17's, and whether Priority and Phase kept their options. Then delete the scratch board
(its Settings → Delete project) and `/tmp/copy-probe`. If the copy keeps everything, `/sigma-init`
can offer `board_setup.py create --template Agrim-Intelligence/17`. If it keeps less, the fallback
is the `fields` + `views` + manual-steps path, which works on any board.

### (d) The 15-minute reproduction (a second person, timed)

This is the acceptance test that the runbook is enough on its own. The second person must not be
the one who did (a).

1. They need `gh` logged in with the `project` scope, a Sigma checkout, and a repository whose
   `.sdlc/config.json` names it. A scratch repository is fine.
2. **Start the clock.** They create a fresh board: either `board_setup.py create <.sdlc> --title
   "repro-234" --yes`, or a blank board in the UI followed by `--number N` on every command below.
3. They follow (a) exactly as written, on their board: fields, views, the `[manual]` lines, the
   Item closed workflow.
4. **Stop the clock** when `board_layout.py verify` exits 0 (12 of 12).
5. **Record on #234:** their name, the date, the elapsed minutes, the final `verify` output, and
   every point where they hesitated or had to ask. Anything over 15 minutes, or any question they
   had to ask, is a defect in this page. Fix the page and time it again.

## `/sigma-init`

`/sigma-init --board yes` still makes exactly one board call, `board_setup.py create`. After it
succeeds, the flow **prints** the `fields`, `views` and `verify` commands (as dry runs) and runs
none of them. The board offer also prints the template alternative (`board_setup.py create
--template OWNER/N --yes`), with a note that what a copy keeps is not yet measured. See (c).

## Not done here

- **Area and Model tier have no writer yet.** They are created empty. Mirroring `area:*` and the
  predicted tier onto the card is separate work, like #233's Priority/Phase mirror.
- **Workflows.** No API can turn one on, so "Item closed" is a manual step. `verify` checks it.
- **Acceptance on board #17**: (a) to (d) above are owner actions and have not been run.
