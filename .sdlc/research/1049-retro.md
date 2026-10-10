# 1049 retro (advisory)

Grade: **achieved** (slice 1 of 4 only; no sinks, hostile inputs or CI yet, as scoped).

## Intent vs shipped
- Pinned table in `evals/safety/check.py`, one entry per live redaction spec (23 specs, 23 entries, 14 shape rules): achieved. The real tree exits 0; `tests/test_safety_check.py` has 33 passing tests.
- Two-way compare keyed by name plus pattern hash, as a multiset, so the duplicated names (private-key, assignment) are told apart and a lost duplicate is seen: achieved.
- SHAPE_RULES subset assertion and the pinned commit outcome for the two redactor-only shapes: achieved.
- Generators assemble values from fragments at run time with a seeded generator; no secret-shaped string on any source line; the exempt-fixture (D-3) check exists: achieved, with the caveat below on an empty exempt set.
- Red control per guard (removed live spec, unpinned spec, dropped generator), exit 1 naming the shape, never a value: achieved. Review sent the change back once (exit-2 coverage on missing scrub attributes, a silent D-3 disarm, a docstring overclaim); a fresh reviewer approved the fix.

## Residual debt
- An empty `COMMIT_FIXTURE_VALUES` passes validation, so the D-3 guard can be vacuous without a signal.
- The docstring says the check "writes nothing"; a bytecode cache write on import is not covered by that wording.
- The refusal test does not assert the named missing attribute, only the refusal.
- The plan listed 23 test selectors and 33 shipped; the extra tests are covered by behaviour, not by the written plan.
- Slice 4 will hit the documented-gestures name collision: two tracked scripts are called `check.py` and the doc test resolves by file name only.
- Timing is unmeasured on CI and on the repo floor of Python 3.10 (measured locally on 3.9.6).
- Nothing yet exercises sinks, hostile inputs or CI (slices 2-4), so what any sink does with a value is unchecked.
