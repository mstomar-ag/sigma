# Deep pass — Architecture (L4: AI-authored from the codebase)

Loaded on demand by `sigma-vision` for a deep Architecture pass. Fills the **## Architecture** section
of `.sdlc/context/north-star.md` as a **numbered, checkable rule list** (plan-review enforces these).
Unlike the tiers above, **you draft this from the code**, then the user approves — never a blank page,
never a unilateral redefinition. Horizon: revisit when major structural changes land.

## Draft FROM the codebase (the whole point)
Read the repo and synthesize its *current* shape — do not invent an ideal:
1. **Structure** — the file / module / service tree: what the top-level units are and how they're
   organized.
2. **Key files** — README, build / config files, entry points — for the stack and how it's wired.
3. **Recent history** — `git log` over the recent window — for structural or quality changes in flight.
4. **Existing rules** — any `CLAUDE.md` / `AGENTS.md` / architecture doc already in the repo (link to
   them; do not duplicate).

From those, extract:
- the **layers** and the **dependency direction** (what may import or call what);
- the **boundaries + contracts** that cross modules (naming, interfaces, invariants);
- where the **quality gates** live (tests, checks, CI);
- any **existing violations** of the apparent rules (flag them, don't bury them).
- candidate **legacy paths**: you may list them as a suggestion; never write `design.zones` (the owner declares it, see `docs/change-zones.md`).

## Turn it into rules
Write **## Architecture** as a numbered, checkable list a plan can be judged against — e.g. "the UI
layer holds no business logic", "dependencies point inward; no sibling imports across modules", "every
public function has a test". Keep the stack itself in `.sdlc/project.md`; the rules here are what
plan-review enforces. Link to (don't copy) any existing `CLAUDE.md` / `AGENTS.md` / architecture doc.

## Surface for approval
Present it as "here's the shape I found — does this match your intent?" If the code has drifted from
the intent in the tiers above, **flag the drift** and let the user reconcile the code or revise the
rule. Draft, don't dictate. Never overwrite a rule the user has ratified without asking.
