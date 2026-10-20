# Draining a run label

A run label is a temporary, plain GitHub label you put on a set of goals so that one loop run works
through exactly that set and then stops. This page is the whole path on one page: what the label is,
how to ready it, how to run it, and what it costs in GitHub calls. The code is
`skills/sigma-loop/scripts/label_drain.py` (the readiness verbs) and `skills/sigma-loop/scripts/loop.py`
(the `--label` pick scope).

## The guarantee

> **While a `--label` run is active, no goal outside that label may be picked, and an open prerequisite of a member is never skipped silently.**

That sentence is quoted byte for byte in `skills/sigma-loop/SKILL.md`,
`skills/sigma-loop/references/picking.md`, `skills/sigma-goal/SKILL.md` and here, and a test in
`tests/test_docs.py` fails if any copy drifts. Quote it, never reword it.

## What a label is, and is not

- It is a label a person creates and a person deletes. Sigma never creates it and never removes it;
  `--label` on a name the repository does not have is refused before any query, because an unknown
  label lists as empty and would read as a drained backlog.
- It is not a unit of work. Names starting `sdlc:` or `feature:`, the goal label itself, and a bare
  `--label` are refused. `--label` together with `--feature` is refused: two scopes, no defined
  meaning. It needs a GitHub-backed backlog; local goal files carry no labels.
- A member is an open issue carrying the label. To be picked it must also carry the goal label. Issues
  that carry only the run label are invisible to the picker, which is why `arm` exists.
- It does not change where a goal's branch starts. A member that declares a unit is still based on that
  unit's branch, otherwise on the usual base (`docs/branching-model.md` section 6). The cross-unit
  warning below is the consequence.

## The verbs

All take the project's `.sdlc` directory and the label name.

- `label_drain.py plan <sdlc_dir> <label> [--json]` lists every open member with a verdict, the open
  prerequisites outside the label (with depth), the questions a person must answer, and whether the
  label is `ready`. It never writes. The label listing can trail a label that was just added, so when it is empty or its newest issue changed under 60 seconds ago, `plan` waits a few seconds (`SIGMA_LABEL_LAG_WAIT`, default 3, 0 allowed) and reads once more, merging by issue number, and says so in its output (one extra read, never a loop; #1943). `status` is the same read with a shorter view that also says which members are pickable now.
- `label_drain.py arm <sdlc_dir> <label> [--apply]` is a dry run unless `--apply`. It arms the members
  `plan` calls unarmed: adds the goal label, removes a parked or blocked label, adds a priority label
  only when the issue has none, and assigns the configured assignee. It never creates a label and
  never removes the run label.
  A member that is already armed but has no assignee (verdict `unassigned`) gets only the configured
  assignee, no other label change; with no assignee configured it is left alone. An issue assigned to
  someone else stays `assigned-elsewhere` and is refused.
- `label_drain.py answer <sdlc_dir> <label> --answers <file.json> [--dry-run]` applies a person's
  answers. The file maps question ids, exactly as `plan` prints them, to one of that question's
  options. An unknown id or an option outside the bank refuses the whole file with no write. Each
  answer applies its effect first and then writes one decision record and one comment. `--dry-run`
  prints every write and makes none. Answering a member's "which kind" question declares that kind on the
  member (`--dry-run` prints `would declare kind <kind> on` the member), so the next `plan` asks the
  member's own question.
- `loop.py next <sdlc_dir> --label <name>` (and `next-batch`) is the pick. Before the first claim it
  runs `plan` once and refuses to start while members are unarmed or questions are open; the refusal
  claims and writes nothing and names the command to run. The check is keyed to the label run, not to
  the checkout: a passed check records its label in `state/STATE.md` (`label_checked`), and later picks skip
  `plan` only while a run is started and that same label is the recorded one. Another label, or a
  checkout with no started run, runs `plan` again, so a second label is never picked unchecked.
  `loop.py start-run` clears the recorded label, so the first pick of a new run checks again.

## Arming defaults

The optional `label_drain` block in the project config has three keys: `default_priority` (default `P2`),
`arm_pulled_blockers` (default `true`) and `kinds` (default empty; maps exact run-label names to kinds, so labels
without a `:` or `/` prefix can share a kind). A wrong-typed value reads as the default. With
`arm_pulled_blockers` an open prerequisite outside the label is armed as well, given the run label and
one comment. An upper-tier issue, a parked one, one held by a gate, or one assigned to someone else is
refused with its reason and not written. Keys are listed in `docs/configuration.md`.

## Questions and recording the answers

`plan` asks only what can change what the drain does: a parked member's own question, a member no gate
can classify, a prerequisite owned by someone else or of an upper tier, the arming route (once), and
one spend question per member that declares a paid step. The wording comes from the shared question
bank, never improvised. Answers are recorded by `answer` under the label's scope, and the arming
question is recorded by label kind (the name up to the first `:` or `/`), so repeated answers can be
recommended later and are never applied until you answer. The record format and the approval
rules are in `docs/decision-rulebook.md`. On a public repository the label name is hashed in every
record and comment.

## Spend

A member whose body declares a paid step gets a spend question. Approving it writes a single-use
`sigma:spend-approved` marker on that issue, and only when the author is a listed spend approver.
Sigma does not meter external spend, so no dollar cap is enforced anywhere in this flow: the bound is
the single-use approval per item, not an amount. Read the paid-step warning in `plan` before you answer.

## Prerequisites, follow-ups and the stop

A prerequisite is read from the member's body with the same explicit dependency forms the pick gate and the
dependency map use: `Blocked by #N`, `depends on|upon #N`, `blocked on|until #N` and the tight follow-up forms
(`follow-up to #N` and its variants, with only spaces before the number). Weak words such as `needs` or
`after` make no prerequisite, and a reference to another repository keeps its member out of the ready set as
unresolved. Write a member's prerequisite as a `**Blocked by:** #N` line; `deps.py report` and `deps.py mark`
find and fix one stated only in prose or a comment. The full rules are in
[the dependency map and Sigma Chain](dependency-map-and-chain.md#how-a-dependency-is-read).

When a member is held by an open prerequisite outside the label, the run pulls that prerequisite into
the label (at most 50 per run) so it is picked first, and says so on stderr; one that cannot be pulled
stays out with its reason and the member stays held. Nothing is skipped silently. A pulled prerequisite that
is already armed but has no assignee gets the label, the fixed comment and the configured assignee; one
assigned to someone else is refused with that reason.

Work filed during the drain does not carry the label, so it is not picked by this run. When the run
stops, `loop.py` lists what was filed since the label's start check first passed (the drain start;
`start-run` sets it too) without the label, so a person can label it for a later run. With no drain start
recorded it lists nothing. The stop is `label-held` when open members remain that were not picked, with a reason
per member, and `backlog-empty` when the label has no open members; then the label can be deleted
by a person.

## Warnings in the plan

- Cross-unit: a member based on its unit's branch will not contain the change of a prerequisite from
  another unit until that unit lands.
- A cycle among members, a member with no valid `Done when` list (3 to 7 single-line items, the shape
  acceptance accepts), and a declared paid step are each named.
- Bounds: at most 200 members (more is refused with the count), at most 50 single-issue reads in the
  prerequisite walk, depth 8, and at most 20 parked members briefed; the rest report `not-read`.

## GraphQL budget

GitHub counts GraphQL points per account, so two watchers on one machine spend one allowance. Sigma keeps
that spend in one place:

- **The bucket.** `gh-graphql-bucket.json` in the user state folder (`SIGMA_RESOURCE_DIR`, else the XDG state
  home; the same folder as the resource guard's `hold.json`), never inside a repository. Keyed by account:
  `SIGMA_GH_ACCOUNT` if set, else one shared `default` bucket for the machine user.
- **Spacing.** One point refills every 2 seconds, up to a burst of 20: about 1800 points an hour when steady.
  A watcher tick may spend 5 of them. A call made through `sources._run_gh` that is GraphQL-shaped takes one
  point first; with none left it is not made and the error says how long to wait.
- **A refusal holds everyone.** The refusal's text is read once and `hold_until` is written, so every other
  process stops calling until then instead of finding out one refusal at a time.
- **Where the wait comes from.** Only the refused response: `Retry-After`, else `X-RateLimit-Reset`, else a
  bounded default (60 seconds secondary, 300 primary). `gh api rate_limit` is never the source; it can say
  thousands are left while a secondary limit holds the account. `gh` hides headers unless asked, so the gesture
  to see them is `gh api -i`; without them the default applies.
- **Secondary versus primary.** A secondary limit (an abuse guard, `Retry-After` in seconds) is named as one in
  the doctor board row ("secondary limit", with when to retry); a primary limit is the hourly allowance and
  keeps its reset-time wording.
- **Label lookup is REST.** The pick's label check (`GitHubSource.label_exists`) and `label_drain.py plan` use
  one `GET repos/{repo}/labels/{name}` (the core pool), so both work with GraphQL refused. Attaching or swapping
  a label still needs GraphQL node ids.
- **Damage fails open.** A missing, corrupt or locked bucket file never blocks a call. Unmeasured against a live
  secondary limit.

## Parallelism

`next-batch --label <name>` follows the same scope, and the goal-level parallelism settings in
`docs/parallelism.md` apply unchanged. Two processes sharing one `.sdlc` directory cannot claim the
same goal: the claim takes a per-goal lock first, so a second child skips what the first holds.

## Unattended use

A headless child is the existing loop command with `--label <name>` on its pick calls, run under the
same supervisor as any other unattended loop (`docs/running-the-loop.md`). Ready the label first with
`plan`, `answer` and `arm --apply`: an unready label makes the child stop at once with the refusal
line, claiming nothing. Whether a real overnight child passes the flag through its slash command text
is unmeasured.

## A drain, end to end

Before step 1: run every command from the directory that holds `.sdlc`, with `python3` and the script
path under `skills/sigma-loop/scripts/`. The repository comes from `discovery.github.repo` in
`.sdlc/config.json` (or the current git remote), and `gh` must be signed in. `"$PPID"` in step 5 is the
shell's parent process id, which marks the session that owns the claim; pass it as written.

1. A person creates the label and puts it on the issues, for example `validation-run-1`.
2. `label_drain.py plan .sdlc validation-run-1`. Read the verdicts, warnings and questions.
3. If `plan` printed no question, skip this step (an empty answers file is refused). Otherwise write an
   answers file mapping each printed question id to one of its options, for example
   `{"label_set:validation-run-1": "arm them"}`, and run `label_drain.py answer .sdlc validation-run-1 --answers answers.json --dry-run`,
   then again without `--dry-run`.
4. `label_drain.py arm .sdlc validation-run-1`, read the dry run, then add `--apply`.
5. `loop.py next .sdlc --label validation-run-1 --session-pid "$PPID"` (or start `/sigma-loop` with
   the flag). Repeat until it reports the stop.
6. A person deletes the label.

## What it costs: counted calls (fake transport)

These are counts from a counting fake transport, the stateful fake that `tests/test_label_drain_arm.py`
uses, driven by `tests/test_label_drain_calls.py`. They are call counts, not timings, and not rate-limit
figures. The same test fails if this table and the measured counts disagree.

The repository state is six issues. Members of the label: one armed and ready, two unarmed, one parked,
one holding a paid step and waiting on an open prerequisite. The sixth issue is that prerequisite, open
and outside the label. The `after arm and answer` row and the no-label row use the state after `arm --apply` and after the
parked member is answered; the other rows use the starting state.

| what | calls | reads | writes |
|---|---|---|---|
| plan | 4 | 4 | 0 |
| arm --apply | 16 | 10 | 6 |
| next --label, before arm | 5 | 5 | 0 |
| next --label, after arm and answer | 26 | 15 | 11 |
| next, no label | 21 | 10 | 11 |

Reading it: `plan` never writes. A refused start made five reads and no write. The pick inside the
label made five more reads than the pick without it. Both picks wrote eleven times, one of them the creation of the `sdlc:checkpoint` label, which this fake does not have
and a real repository has after its first pick; this page does not
break the other writes down further. The two picks chose different goals, so they are a cost comparison,
not an identical-work comparison.

## What it costs: one real run

One run on a throwaway private repository, each verb run once through a thin `gh` wrapper that logged
every call, so the counts are real GitHub calls and the times are wall-clock seconds for that one
run. One run is one sample: it shows the shape of the cost, not a spread, and no rate-limit figure was
taken. The repository state: five small issues. Members of the label: one armed and assigned, one
unarmed, one armed and held by an open prerequisite. The fourth issue is that prerequisite, open and
outside the label. The fifth is an unrelated armed goal outside the label.

| step | wall seconds | calls | reads | writes |
|---|---|---|---|---|
| plan | 5.5 | 3 | 3 | 0 |
| arm, dry run | 8.9 | 5 | 5 | 0 |
| arm --apply | 18.1 | 10 | 7 | 3 |
| answer (arming, apply) | 30.5 | 16 | 13 | 3 |
| next --label, one pick | 41.4 | 24 | 23 | 1 |
| next, no label, one pick | 20.6 | 12 | 11 | 1 |

Reading it: `plan` never writes. `arm --apply` made the three writes for the one unarmed member
(assign, goal label and priority label) and seven reads. The pick inside the label made twelve more
reads than the pick without it and wrote once, the claim. Both picks took the same member, after the
first claim was released. The label pick's time is that attempt only, after earlier attempts met the
machine's own admission hold.

Not measured: the call counts of a refused start, of a pick that pulls a prerequisite into the label,
of a drain past the first pick, and anything on a repository of real size. The first table above is a
call count from a counting fake transport and is a different thing from this run.

## Second reader

The page was followed once by a reader other than its author, who was given only this page and the
throwaway repository. What that reader had to guess is recorded in the pull request that added this
section, and each guess is fixed in the text above.
