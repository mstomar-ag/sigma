# Running Sigma on a public repository

Sigma's own public repository is intended to run on this profile once it publishes — proven so
far only against a scripted `gh` and a real git remote, not yet against a live GitHub run; see the
end of this doc. Use it for any public repository — a
public remote with outside contributors, where goals live in the repository's own issues, labels
and optional board, none of it stored in files. Because none of it is stored in files, the profile
is applied once per clone. It is an opt-in layer on top of `/sigma-setup`'s own defaults; the
`/sigma-init` scaffold's shipped default is unchanged.

## The profile

Four keys, each with the reason it is set this way:

- `ledger.enabled: false` — the team ledger is a git ops-branch coordination tool, not a data feed.
  On a public repository, or a public fork, every participating clone pushing its ledger entries to
  a shared branch publishes each contributor's GitHub login, a per-machine id, and a timestamped
  claim/done/park trail. The real reason for the ruling is to stop every contributor from pushing
  ledger commits to a public remote.
- `journal.enabled: false` — already ships off by default; listed so the profile is complete.
- `knowledge_graph.enabled: false` — already ships off by default; listed so the profile is
  complete.
- `work.auto_merge: "off"` — a human merges a public repository's pull requests; nothing here is
  ever armed to merge unattended.

**What is lost with the ledger off:** the claim and outcome mirror; the ledger inbox on the picker,
hand-offs, and autowatch; the ledger half of two degradations that otherwise fall back to a single
channel (a failed claim-label write is reported on stderr only, and a withheld kit finding is kept
only in `.sdlc/state/withheld/`).

**What is kept:** the `sdlc:in-progress` label, the only cross-machine claim signal that survives
with the ledger off.

Apply the profile with this block, run from the repository root right after `/sigma-init`:

```bash
# public-repository adoption: run from the repository root, after /sigma-init
python3 "$SETUP" configure .sdlc --repo <owner/name> --verify "<your test command>"
# After reviewing that command, keep its shell permission local to this clone.
git config --local sigma.allowRepositoryShellCommands true
python3 "$SETUP" ignore . --scope tracked
python3 "$SETUP" labels .sdlc
python3 - <<'PY'
import json, pathlib
cfg_path = pathlib.Path(".sdlc/config.json")
cfg = json.loads(cfg_path.read_text())
profile = {
    "ledger": {"enabled": False},
    "journal": {"enabled": False},
    "knowledge_graph": {"enabled": False},
    "work": {"auto_merge": "off"},
}
for key, value in profile.items():
    cfg.setdefault(key, {}).update(value)
cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
print("public-repository profile applied:", json.dumps(profile))
PY
```

`$SETUP` is the same variable `SKILL.md` already defines. `configure`'s own summary line prints
`ledger: enabled=True` before this block's heredoc turns it back off — that line is correct at the
moment it prints, and the heredoc's own printed line is the one that reflects the profile actually
applied. The Git-local trust line is deliberately separate from `.sdlc/config.json`: a clone or
committed profile cannot enable repository-controlled shell commands by itself. Run it only after
reviewing the test command; it applies to every linked worktree of this clone's repository.

Skip only step 6's `sync.py bootstrap` line — it creates and pushes the ledger branch, which this
profile keeps off. Still run `/sigma-doctor`, step 6's OTHER call: it confirms this profile's own
state (ledger correctly off, `work.enabled`, verify) just as well as it confirms the default
profile's. Where step 7's summary would normally say the ledger is up and pushed, say instead that
the ledger is off by design, because nothing was bootstrapped.

Re-running `/sigma-setup` later keeps these values: a deliberate `false`/`"off"` is never one of the
`null`-only defaults `configure` is free to overwrite.

## Labels

`labels` creates the ten lifecycle labels (`sdlc:goal`, `sdlc:in-progress`, `sdlc:parked`,
`sdlc:blocked`, `sdlc:blocking`, the legacy `sdlc:needs-confirmation`, `sdlc:needs-label`, `sdlc:designed`,
`sdlc:needs-unit`, `sdlc:needs-triage`) and `priority:P0`–`priority:P3`. They must exist before
anyone can file a goal, because GitHub refuses to apply a label that does not exist. It prints one
line per label (`created`, `existed`, or `FAILED: <reason>`) and `labels ensured on <repo>` only
when every one was measured present; if any failed it exits non-zero naming them, and `loop.py
start` refuses to start for the same reason. An existing label is read back, never rewritten.

The loop recreates any of the ten labels' EXISTENCE on its next claim, park or offboard — but only
that. Deleting `sdlc:in-progress` strips it from EVERY issue that currently carries it; the label
comes back globally on the next claim/park/offboard, but an already-claimed goal stays unlabelled
until it finishes, and until then a SECOND CLONE of this repository (same login, its own `init` plus
this adoption plus `start`/`next`) can silently double-pick that same goal — no error, no warning,
just the same issue number twice (measured). Deleting `sdlc:goal` is worse: `loop.py next` then
prints `DONE` as if the backlog were empty, and no claim or park recreates it, because there is no
claim to trigger the self-heal (measured); the next `loop.py start` does, and `next` says on stderr
that 0 issues carry it. Deleting `sdlc:parked` is worse the same way, by code, not
measured: a parked issue's own label never comes back on its own, and it drops out of every queue
that filters on it.

Recovery, every time, by hand: `gh label create sdlc:<name> --repo <owner/name>` if the label itself
is gone, then `gh issue edit <N> --add-label sdlc:<name> --repo <owner/name>` for the affected
issue(s) — and for a double-picked goal, agreeing with whoever else is running it before either side
calls it done.

`priority:P4` is created on first use by `/sigma-triage` and `handoff.py`; P0–P3 already exist.
Unprioritised goals are still picked, after prioritised ones. When no open issue carries `sdlc:goal`
at all, `loop.py next` still prints `DONE`, and says on stderr that 0 issues carry the label.

`area:*` labels are yours. `feature:*` labels come only through `/sigma-define` — see
`docs/label-model.md` §12.

If this account cannot create labels, the claim still happens but prints
`CLAIM-LABEL-WRITE-FAILED`. Cross-session exclusion is degraded until the label exists.

## Filing and landing a goal

```bash
gh issue create --repo <owner/name> --label "sdlc:goal,priority:P1" --assignee @me \
  --title "<goal title>" --body "<...>"
```

The default `discovery.github.assignee: "@me"` picks only goals assigned to the account running the
loop.

Then the sequence a goal follows once landed: `work.py merge` runs the post-PR review gate and ends
`… — review gate passed (require_review: changes) — auto_merge is off, leaving PR #N for a human`;
`loop.py record .sdlc <goal> review` keeps the goal's issue open (card in QC) and the worktree kept —
`record done` is refused while the PR is unmerged, because done means merged; a human merges the PR;
the next `loop.py next`, the watch tick, or `loop.py reconcile-merges .sdlc` observes the merge,
records `done`, closes the issue and releases the worktree.

**A goal's issue closes only after its pull request merged.** Every goal awaiting a merge keeps one
worktree until the merge is observed — the doc's own per-goal growth to watch.

## The board

The board is optional; the loop runs on issues and labels either way.

- **The owner has no Projects boards.** The first claim's status write creates `<name> — SDLC`,
  rewrites its Status field to the kit's seven lanes, and adds a P0–P4 Priority field. The
  maintainer's `gh` token needs the `project` scope — `gh auth refresh -s project`.
- **The owner already has boards.** Sigma refuses to create one, to avoid duplicating an
  existing board, and prints `board mirroring OFF this run …` on stderr every run. Wire an existing
  board manually: `gh project create --owner <owner> --title "<name> — SDLC"`;
  `gh project field-create <number> --owner <owner> --name Priority --data-type SINGLE_SELECT
  --single-select-options "P0,P1,P2,P3,P4"`; give the Status field the seven lane options in the
  GitHub UI (`gh` has no field-edit command) — see `docs/label-model.md` §8a for what each lane
  means; then set `discovery.github.project.number` (plus `.owner` when the board's owner differs
  from the repository's owner).

Without a board — or without a `Ready` lane on one that exists — the loop keeps the label queue, and
cards move only to columns that actually exist.

## A defect in Sigma itself

A finding about Sigma's own internals — one that cites a path under the plugin's install root,
under `skills/`, `hooks/` or `.claude-plugin/`, and under a directory this project does not have —
is routed away from this board automatically; see `skills/sigma-loop/references/filing.md`. It fires
only when every one of these holds: the handoff config isn't set to file locally; the checkout is
not self-hosted; the finding's own text cites a genuinely kit-shaped path; it is not a repeat and is
under the per-goal cap; `ledger.handoff.upstream_repo` names an `owner/name`; and cross-repo access
to that repository comes back granted. It is configured with `ledger.handoff.upstream_repo`.

With the ledger off, a withheld finding is kept only in `.sdlc/state/withheld/` — there is no ledger
note to fall back on. A finding that DOES route arrives on the target repository with no labels and
no assignee: the write is a plain REST issue-create call that deliberately applies none of the
adopter's own taxonomy. Triage it exactly like any hand-filed goal — apply `sdlc:goal` (plus a
priority) and assign it.

## What is proven

`tests/test_public_bootstrap_control.py` drives the profile above, the labels section, and filing
and landing a goal end to end against a scripted `gh` and a real git remote. It does not drive the
board section — only the "owner already has boards" branch is exercised; the auto-create branch is
not modelled by the scripted `gh` — and it makes no call into "a defect in Sigma itself" at all.
None of this has yet been run against GitHub itself.
