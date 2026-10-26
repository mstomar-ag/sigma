# Stopping, and the spare-iteration loop

The detail behind step 7 of [`../SKILL.md`](../SKILL.md): closing the loop on a knowledge-graph
gap when the backlog is empty, and what to print and report at STOP.

---

7. Loop.

**A degraded pick is not an empty backlog.** `next` → `DEGRADED <epoch>` is a rate-limited or incomplete read: never run it as a goal and never take it for `DONE`. `loop.py wait-degraded <epoch>` waits once (bounded), then pick again; a second `DEGRADED` ends the run with `LOOP STOP: degraded`, reported as such.

**Self-improving (optional, gated):** when the backlog is empty (`next` → `DONE`) but the knowledge
graph is enabled and `kg.py gap list .sdlc` shows open gaps **and** budget remains, you may close the
loop instead of stopping: take the oldest gap, research it, write the finding to
`.sdlc/knowledge/analysis/`, refresh the graph (`/sigma-kg`), then mark it filled —
`python3 "${CLAUDE_SKILL_DIR}/../sigma-kg/scripts/kg.py" gap resolve "<the gap>" .sdlc`. **One gap per
spare iteration, only within budget, and park (never force)** anything that needs a human. This is how
the graph fills what it didn't know — turn it off by leaving the KG disabled.

At STOP, print one machine-readable line FIRST — `LOOP STOP: backlog-empty`, `LOOP STOP: budget`, or
`LOOP STOP: handoff` (#2521 — see below) — then report: N done, M parked, K failed. (Unattended overnight? The user can wrap this loop
in `scripts/supervise_daemon.py` — it relaunches through usage-limit resets and crashes with zero
polling; you never invoke it yourself mid-session.) If anything parked or failed, point the user to the items —
`.sdlc/state/review-queue.md` in local mode, or the issues labelled `sdlc:parked` (the **Blocked**
column on the board) in github mode. **`decision_tier: "auto"`** (off by default — see the README's
feature-flag table) annotates each `needs_decision`/`irreversible`/free-text (`unknown`) park with an
`autonomous`/`escalate_l1`/`escalate_l0` tier (`decision_tier.py`, per #818's escalation pyramid),
shown as a `- tier:` line in the review queue and in the park comment — advisory only, a triage hint
for whoever reads the queue, never a routing decision the loop itself acts on.
Parking is always correct over forcing an irreversible action to "finish" a goal.

**On a `handoff` stop** (`config.json` → `handoff`, on by default at 20 goals or the
context threshold): this is a deliberate, healthy checkpoint, never a problem — the orchestrating session is being
retired ON PURPOSE, before its own accumulated context becomes the next 900k-token compaction. No
goal is abandoned: `loop.py`'s own `_next()` only checks this ceiling BETWEEN claims, immediately
before claiming the next one. It counts completed goals plus goals still running in this session.
Stop refilling slots, wait for every already-dispatched goal to finish, and record each outcome
before retiring the orchestrator. Nothing below the gate mutates a goal it refuses.

The `HANDOFF` call printed a **successor brief** on stderr (`SUCCESSOR BRIEF:` through `open
decisions:`): the resume command with this run's flags, live slots, open decisions. Relay it
verbatim, last, always — even under `supervise_daemon.py`, which relaunches by itself; whether anyone
reads it cannot be known. The successor runs in a **new host session**, never this conversation.
Who starts it on each host: `docs/running-the-loop.md`, "Hand-off: who starts the next session".
