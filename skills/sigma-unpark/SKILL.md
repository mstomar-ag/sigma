---
name: sigma-unpark
description: Resolve human-blocked goals and record the decision before unpark. Use for parked issues or /sigma-unpark.
allowed-tools: Bash(python3 *), Bash(gh issue *)
---

## Codex path resolution

Codex has no `CLAUDE_SKILL_DIR`. In each command, replace `${CLAUDE_SKILL_DIR}` with the
absolute directory of this installed `SKILL.md` (shown in the skill catalog); never run an
empty path. Claude keeps its provided value.

# sigma-unpark

Detailed selection triggers: [selection](references/selection.md).

A park is a question nobody answered.

Unparking without answering it is the worst outcome available: the goal gets picked, an agent
spends real effort rediscovering the same obstacle, and parks it again — with a fresh comment nobody
reads either. So in this skill **the answers are the artifact**, and the label change is a
consequence of having them.

Engine: `${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/unpark.py`.

**Not `auto_unpark`.** That sweep reverses the one park class it can *prove* is stale — a named
blocker that has since closed — and touches nothing else. This skill handles everything that needed
a person: a decision, an irreversible step, a failing check, free prose. They are complements. A
`keep-parked` decision here writes `auto_unpark`'s own opt-out marker, so a human decision recorded
through this path is never re-litigated by the automatic sweep.

## The flow

1. **List** — `python3 "${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/unpark.py" list .sdlc [--assignee X] [--undeclared]`
   Parked and blocked issues, oldest first. `--undeclared`: kindless parks; `declare .sdlc <issue> <kind>` sets one. Scope defaults to `discovery.github.assignee`, which
   ships empty; the output says which scope it used, so an empty list is never mistaken for "nothing
   is parked for me".

2. **Pick one** — the issue the user named, otherwise the oldest.

3. **Brief** — `python3 "${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/unpark.py" brief .sdlc <issue> --json`
   Returns why it was parked, what kind of park it is, every referenced blocker **with its live
   open/closed state**, any answers a previous round already recorded, and the questions still worth
   asking. Never ask something this payload already answers.

4. **Ask, one question at a time.** Use `AskUserQuestion` where the host has it, with the `options`
   the brief supplies. One question per turn — a wall of questions gets one answer. If the brief
   has `recommendation`, list that option first with "(recommended)" in the label only (never in the
   answer), and put `recommendation.rule_id` in the payload as a top-level `rule_id`.

5. **Close the loop.** The last question is always: *start it again now, or leave it parked with
   these notes?* Do not infer it from the earlier answers; ask it. Its id is **`resume`** — keep it
   distinct from every question-bank id, or one answer overwrites the other and a resumed interview
   drops the closing question entirely.

6. **Resolve** — write the answers file, then:
   ```
   python3 "${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/unpark.py" resolve .sdlc <issue> \
       --answers /tmp/answers.json --decision unpark|keep-parked [--dry-run]
   ```
   ```json
   { "answers":   { "route": "wait for it", "resume": "leave it parked" },
     "questions": [ { "id": "route", "ask": "#7 is still open. Do you want to wait for it, …" } ] }
   ```
   Pass `questions` too — a reader six weeks later needs to see what was *asked*, not just a bare
   key and an answer.

7. **Offer the next step**, in this order: work this goal now / move to the next parked item / stop.
   "Work it now" hands off to `/sigma-loop`, and **must** pass `--session-pid "$PPID"` on both
   `loop.py start` and `loop.py next`, read fresh from your own shell each call — without it a later
   continuation can silently re-dispatch a goal this hand-off already claimed (#1199).

## Writing the questions

The engine supplies a question **bank** keyed by the kind of park, so the common cases are already
worded and reviewable. When you have to compose one yourself, follow the same rules:

- **One sentence.** No clause stacking, no preamble.
- **Name the concrete thing** — `#1234`, `test_foo`, `the retry wrapper`. Never "the aforementioned".
- **No Sigma vocabulary.** Never say *reason class*, *decision tier*, *offboard*, *lifecycle*,
  *slot*. The person answering does not run the loop.
- **Ask for a decision or a fact**, never for approval of your analysis.
- **Offer the real options** when there are two or three. Free text otherwise.
- **Never re-ask** what `brief` already answered, or what a previous round recorded.

Good: *"#7 is still open. Do you want to wait for it, work around it, or drop this?"*
Bad: *"The dependency reason_class park indicates an unresolved external blocker — how would you
like to proceed with respect to the blocking relationship?"*

## Where the answers go

Both to a **comment** (the audit trail) and into the **issue body** (the copy the next agent
actually reads — hunting comments is exactly how context gets lost and a goal gets re-parked for a
reason already answered).

The body block is fenced in `<!-- sigma:unpark-qa:start -->` … `:end`, and that fence is
load-bearing: `backlog_check` strips the span before scanning for blocker phrases. Without it an
ordinary answer like *"waiting on the design sign-off, see #1234"* would plant a phantom blocker in
the body of the goal that was just unblocked. A real `Blocked by #99` anywhere **else** in the body
is still detected exactly as before.

A second round **replaces** the block rather than stacking another copy.

### Caveat you should know before using this skill

The fence protects everything **inside** the recorded block. It does not protect prose you write
**elsewhere** — a comment you type by hand, or text you paste into the issue body outside the block.

Blockers are detected from prose (`Blocked by: #7`, `depends on #7`), because a person writing in
the GitHub UI should not have to learn a syntax. That inference is imperfect in both directions and
**cannot be made exact with a pattern**:

- **Reliable:** `Blocked by: #N`, with nothing between the phrase and the number.
- **Correctly ignored:** a reference across a clause boundary — *"waiting on the sign-off, see
  #1234"*. A comma, semicolon or full stop between the two ends the match.
- **Still wrong:** *"after #40 lands"*, *"this needs #9 to land first"*. These read as dependencies
  even though they are not, because nothing about their shape says otherwise — only their meaning.

So: when you write a free-text answer that mentions an issue number, either keep it inside the
recorded block (which `resolve` does for you), or phrase it across a clause boundary. If a wrong
edge is created anyway, retire that one match permanently:

```bash
python3 "${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/backlog_check.py" dismiss-text blocked-by 40
```

or exempt the goal from the automatic sweep with `auto_unpark`'s keep-parked marker — which is
exactly what this skill's own `keep-parked` decision already writes for you.

## What each decision does

| Decision | Labels | Board | Record |
|---|---|---|---|
| `unpark` | one atomic swap: `+sdlc:goal`, `−sdlc:parked`/`−sdlc:blocked` | card → `Ready` | body + comment |
| `keep-parked` | **none** | unchanged | body + comment + `auto_unpark` opt-out marker |

To turn a decision into a standing rule, run `rule_draft.py draft`, then an owner runs `rule_draft.py approve <state-folder> <rule id>`.

An interrupted interview loses nothing: answers are recorded before the session can end, and `brief`
picks up where it left off.

## Notes

- GitHub discovery mode only.
- Refuses a closed issue — reopen it first.
- `--dry-run` names the exact swap and writes nothing.
- Exit codes: `0` done, `1` a read or write failed, `2` usage.
