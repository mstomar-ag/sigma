# 1044 retro (advisory)

Grade: **achieved** (to its stated scope; not wired to the real gate, as the goal required).

## Intent vs shipped
- Runner `evals/skills/smoke.py` is stdlib-only, lists `skills/` children itself (not `skill_dirs`), compares both ways: achieved.
- No-card / no-dir / no-SKILL.md each red and named; gate-in-kept-prefix; producer path or `agent`; `exercised` without passing fixture is a finding; script one-entry rule: achieved, each rule seen red in `.sdlc/research/1044-controls.md`.
- README gesture (`--root`/`--cards-dir`) red on fake `sigma-probe`, green once removed; test extracts the line from the README at run time: achieved.
- Not wired into the real-tree pytest gate: as required. `tests/test_skill_smoke.py`: 50 passed on re-run.

## Residual debt
- No real cards: the bare README form is red on the real tree until slices 2-3 write them. Behaviour on the real tree is unmeasured.
- Nothing runs the smoke runner in CI or the pytest gate; until slice 3 a new skill directory without a card goes unnoticed.
- Fixture runs execute card-supplied argv from the repo root (timeout-bounded, shell-free); trust in card content is a review-time property only.
- Kept-prefix size is inherited from `evals/skill_structure.py` constants; drift in that cap is followed, not independently checked.
- Scope growth beyond the written criteria (malformed-card shape validation, `expect`, `--fixture-timeout`) is tested but not in the recorded acceptance.
