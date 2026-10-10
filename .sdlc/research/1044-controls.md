# 1044 controls (evidence)

Each rule in `evals/skills/smoke.py` was broken once (one replacement, driver script outside the repo), the named
test run with `python3 -m pytest tests/test_skill_smoke.py -k <test> -q -p no:cacheprovider -x`, then restored.
Result lines (every one RED = "1 failed"):

- break `drop card->dir` -> test_card_without_dir_is_red_and_named: RED (1 failed)
- break `drop dir->card` -> test_dir_without_card_is_red_and_named: RED (1 failed)
- break `read whole file not prefix` -> test_gate_beyond_kept_prefix_is_red: RED (1 failed)
- break `accept any producer` -> test_producer_missing_path_is_red: RED (1 failed)
- break `skip exercised check` -> test_exercised_without_passing_fixture_is_finding: RED after the test was strengthened
- break `skip unlisted-script check` -> test_unlisted_script_is_red: RED (1 failed)
- break `skip duplicate check` -> test_script_in_two_entries_is_red: RED (1 failed)
- break `include dot-dirs` -> test_plain_files_and_dot_dirs_are_ignored: RED (1 failed)
- break `drop abs/.. path rejection` -> test_entry_without_file_is_red: RED (1 failed)
- break `no JSON guard` -> test_malformed_card_is_a_finding_not_a_traceback: RED (1 failed)
- break `drop stem check` -> test_malformed_card_is_a_finding_not_a_traceback: RED (1 failed)
- break `ignore expect` -> test_exercised_without_passing_fixture_is_finding: RED (1 failed)
- break `no timeout bound` -> test_exercised_without_passing_fixture_is_finding: RED (1 failed)
- break `gate check without SKILL.md guard` -> test_dir_without_skill_md_is_red_and_named: RED (1 failed)

Finding: the first run of `skip exercised check` stayed GREEN (the test's other assertions already produced
findings, so the deleted line was not load-bearing there). Added an exercised card with no scripts to
`test_exercised_without_passing_fixture_is_finding`; re-run: 1 failed (red). Test file now 22 passed.

Revert check: `cmp evals/skills/smoke.py <copy taken before the breaks>` printed no difference.

## README gesture (copied from `evals/README.md`) on a fake tree
Fixture tree: skills `sigma-a`, `sigma-probe`; cards-dir with only `sigma-a.json`.
```
python3 evals/skills/smoke.py --root <skills> --cards-dir <cards>
FINDING skill sigma-probe: no card        -> exit 1
(rmdir sigma-probe)                       -> "1 skills checked, 0 findings" exit 1->0 (exit 0)
```
Wrong gesture (doc line without `--cards-dir`): exit 1 on the clean tree (reads the real cards dir, none exist
yet), so `test_readme_gesture_red_on_fake_probe_then_green` would fail its green leg on that doc line.
`test_readme_gesture_red_on_fake_probe_then_green` extracts the line from the README at run time.

Malformed-entry control: artifact with no `producer`, script with no `path`, bare-string script all crashed `_bad_path` (TypeError, exit 1 like a red); 3 parametrized tests seen RED, fixed (isinstance first), GREEN.
- fix2 control: 19 odd-type card shapes (gates 5/[1]/[[1]]/str, artifacts/scripts 5, list path, expect 5, nulls) + 4 non-object cards ran RED (11 failed on TypeError/silent) against old check(), GREEN after up-front _shape() validation.
- fix3 control: fixture stdout bytes FF FE (UnicodeDecodeError) and NUL in argv (ValueError) both crashed _run_fixture; 2 new tests seen RED, fixed (errors="replace", catch ValueError), GREEN.
