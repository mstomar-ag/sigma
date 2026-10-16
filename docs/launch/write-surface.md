# Write-surface inventory

The `check` command ratchets tracked Python and shell write sites. Control: in a temporary tracked shell file add `gh label delete legacy`, run `python3 tools/readiness/write_surface.py check . docs/launch/write-surface.json`, and see it fail as a new `gh-label` site; remove the line before the green run.

SCANNER BLIND SPOT (#895 slice 3a): the scan counts only literal `gh ...` argv. An issue write migrated onto `skills/sigma-loop/scripts/gh_api.py` (comment, create, edit body, close, add/remove label, add assignee over `gh api`) leaves its old row and shows up only as a call to a `gh_api` helper, so a NEW REST write added through a helper is not seen by `check`. `gh_api.py` `_default_run` has no row: it is a generic runner whose argument list the scanner cannot see, and since #926 the scanner no longer borrows another function's list for it. The write helpers that now have callers are listed in the landing note below and each caller gets its own row. Rows for the 17 migrated sites were removed from this table; `sources.py` `create_dependency` keeps its `gh-label` and `fs-write` rows (the `label create` step stays). `tests/test_issue_creation_boundary.py` additionally pins the callers of the create helper.
SLICE 4b-3 (#895): `close_pr_gh` joins that set, so `work.py` `merge_design` (the REST PUT, previously a `gh-pr` row) and `close_design` (comment POST and close PATCH) each have a `gh-api-write` row. The `finish` `gh-pr` row is gone: `finish` has no `gh` call of its own; the scanner resolves variables by NAME file-wide and had matched `finish`'s `args` against `close_design`'s old `args = ["gh", "pr", "close", ...]`, so renaming that variable also lifts the worktree-remove call to a second `git-destructive` count.
SLICE 4b-2 (#895): `comment_pr` and `merge_pr_gh` are in the scanner's `gh_api` write-helper set, so `work.py` `post_review` (the review comment POST) and `merge` (the REST PUT) each have a `gh-api-write` row; before this the PUT was invisible. The JSON is hand-edited (the `scan` command would overwrite the hand-written gate texts with `ungated`).
LANDING HELPERS (#931): `merge_pr_pinned` and `create_pr_nondraft` are in the scanner's `gh_api` write-helper set, so the first caller gets its own row; until then this note names them (explicit merge method, repository and 40-hex head pin validated before any call; no auto-merge, no admin flag, no branch-delete option; no caller yet), and `tests/test_write_surface.py` pins those words in this file because the ratchet checks presence, not text.

| Path | Function | Rule | Count | Gate | Risk |
|---|---|---|---:|---|---|
| .sdlc/research/1142-measure.py | history | fs-rmtree | 1 | research measurement script, run by hand; removes only the temporary directory it created with `tempfile.mkdtemp()` | high |
| .sdlc/research/1142-measure.py | reroot | fs-rmtree | 1 | research measurement script, run by hand; removes only the temporary directory it created with `tempfile.mkdtemp()` | high |
| .sdlc/research/1142-measure.py | reroot | fs-write | 2 | research measurement script, run by hand; writes only inside the temporary directory it created | medium |
| .sdlc/research/292-benchmark.py | main | git-ref-write | 1 | research benchmark script, run by hand against a temporary scratch repository it creates and removes; the update-ref only points a remote-tracking name inside that scratch repository | high |
| evals/bench/arms/common.py | isolated_env | fs-write | 2 | operator-run harness only: refuses CI and background runs; touches only directories it created under an empty scratch root | medium |
| evals/bench/arms/common.py | remove_tree | fs-rmtree | 2 | operator-run harness only: refuses CI and background runs; touches only directories it created under an empty scratch root; removes only a run, attempt or workdir child directory it was handed | high |
| evals/bench/arms/matched.py | _replace_tree | fs-remove | 1 | operator-run harness only: refuses CI and background runs; touches only directories it created under an empty scratch root; replaces only the arm's own run workdir | high |
| evals/bench/arms/sigma.py | extract_tar | fs-write | 3 | operator-run harness only: refuses CI and background runs; touches only directories it created under an empty scratch root; writes only the three export trees after validating the whole archive | medium |
| evals/bench/bench.py | __enter__ | fs-write | 1 | operator-run harness only: opens (append mode, never truncating) the results file's own .lock file beside it, to refuse a second invocation | medium |
| evals/bench/bench.py | _run_locked | fs-rmtree | 1 | operator-run harness only: refuses CI and background runs; touches only directories it created under an empty scratch root; removes only the temp directory of the one claude --version call | high |
| evals/bench/bench.py | _run_locked | fs-write | 1 | operator-run harness only: refuses CI and background runs; takes an exclusive lock on the results file first; creates its scratch root and per-run directories (all removed after scoring) under an empty scratch root | medium |
| evals/bench/bench.py | _write_json_atomic | fs-write | 1 | ungated | medium |
| evals/bench/bench.py | facts | fs-rmtree | 1 | dry run only: removes the temp export directory it created | high |
| evals/bench/bench.py | run | fs-write | 1 | ungated | medium |
| evals/bench/launcher/sigma_bench_launcher.py | _launch | fs-rmtree | 1 | operator-run launcher, refuses without the owner's config; writes only inside the config's scratch_root; removes only the --dry-run profile it just made with mkdtemp | high |
| evals/bench/launcher/sigma_bench_launcher.py | alert_to | fs-write | 1 | operator-run launcher, refuses without the owner's config; writes only inside the config's scratch_root; one line per refusal or trip into launcher-alerts.log, bounded at 1 MiB | medium |
| evals/bench/launcher/sigma_bench_launcher.py | fresh_profile | fs-write | 2 | operator-run launcher, refuses without the owner's config; writes only inside the config's scratch_root; --dry-run only: a fresh mkdtemp profile with four empty directories | medium |
| evals/bench/launcher/sigma_bench_launcher.py | trip | fs-write | 1 | operator-run launcher, refuses without the owner's config; writes only inside the config's scratch_root; the latch file written when the real plugin surface changed during a run | medium |
| evals/regression/live.py | write_row | fs-remove | 2 | operator-run live regression entrypoint (slice 1, never starts a model): writes only one NOT RUN result row per refusal under the results directory (atomic temp then replace, temp removed in finally) and creates that directory; the exact-value redaction pass runs before the write | high |
| evals/regression/live.py | write_row | fs-write | 2 | operator-run live regression entrypoint (slice 1, never starts a model): writes only one NOT RUN result row per refusal under the results directory (atomic temp then replace, temp removed in finally) and creates that directory; the exact-value redaction pass runs before the write | medium |
| evals/regression/record.py | write_atomic | fs-remove | 2 | operator-run evaluation builder: writes only the single output file it is told (atomic temp then replace) and creates its parent directory; never touches the run dir inputs; also called by control.py run --record to write matrix.json (a second caller) | high |
| evals/regression/record.py | write_atomic | fs-write | 2 | operator-run evaluation builder: writes only the single output file it is told (atomic temp then replace) and creates its parent directory; never touches the run dir inputs; also called by control.py run --record to write matrix.json (a second caller) | medium |
| evals/regression/spend_ledger.py | _ensure_dir | fs-write | 1 | operator-run evaluation spend ledger: creates only the ledger directory it is handed (mode 0700, exist_ok); never deletes, renames or replaces a ledger file; refuses without fcntl | medium |
| evals/safety/sinks.py | _sdlc | fs-write | 2 | operator-run check (evals/safety/check.py), also run by the CI test job, and its tests; every write is inside a tempfile.TemporaryDirectory scratch tree or scratch git repository that is removed on exit, and nothing under the real .sdlc is touched | medium |
| evals/safety/sinks.py | commit | fs-write | 3 | operator-run check (evals/safety/check.py), also run by the CI test job, and its tests; every write is inside a tempfile.TemporaryDirectory scratch tree or scratch git repository that is removed on exit, and nothing under the real .sdlc is touched | medium |
| evals/safety/sinks.py | probe_comment | fs-write | 2 | operator-run check (evals/safety/check.py), also run by the CI test job, and its tests; every write is inside a tempfile.TemporaryDirectory scratch tree or scratch git repository that is removed on exit, and nothing under the real .sdlc is touched | medium |
| hooks/gate_state.py | _open_child | fs-write | 1 | ungated | medium |
| hooks/gate_state.py | _prune | fs-remove | 1 | ungated | high |
| hooks/gate_state.py | _stripe_lock | fs-remove | 2 | ungated | high |
| hooks/gate_state.py | _stripe_lock | fs-write | 1 | ungated | medium |
| hooks/research_capture.py | main | fs-write | 2 | ungated | medium |
| hooks/time_track.py | handle | fs-write | 2 | ungated | medium |
| skills/sigma-define/scripts/define.py | _step_registry | fs-write | 1 | ungated | medium |
| skills/sigma-define/scripts/define.py | main | fs-write | 1 | ungated | medium |
| skills/sigma-doctor/scripts/board_migrate.py | seed_ready | gh-project | 1 | ungated | medium |
| skills/sigma-doctor/scripts/migrate.py | _write | fs-remove | 2 | ungated | high |
| skills/sigma-init/scripts/init_flow.py | _write_json | fs-write | 1 | ungated | medium |
| skills/sigma-init/scripts/sdlc_init.py | scaffold | fs-write | 2 | ungated | medium |
| skills/sigma-init/scripts/sdlc_init.py | scaffold_codex | fs-remove | 2 | ungated | high |
| skills/sigma-init/scripts/sdlc_init.py | scaffold_cursor_rules | fs-write | 2 | ungated | medium |
| skills/sigma-init/scripts/sdlc_init.py | scaffold_demo | fs-write | 2 | ungated | medium |
| skills/sigma-init/scripts/sdlc_init.py | scaffold_github | fs-write | 2 | ungated | medium |
| skills/sigma-init/scripts/sdlc_init.py | scaffold_vision | fs-write | 2 | ungated | medium |
| skills/sigma-init/scripts/setup_wizard.py | _write_cache | fs-write | 2 | ungated | medium |
| skills/sigma-init/scripts/setup_wizard.py | write_dismissed | fs-write | 2 | ungated | medium |
| skills/sigma-init/scripts/verify_detect.py | _atomic_write_json | fs-remove | 2 | ungated | high |
| skills/sigma-kg/scripts/kg.py | _write_refresh_record | fs-write | 2 | ungated | medium |
| skills/sigma-kg/scripts/kg.py | gap_log | fs-write | 1 | ungated | medium |
| skills/sigma-kg/scripts/kg.py | gap_resolve | fs-write | 1 | ungated | medium |
| skills/sigma-kg/scripts/kg.py | retain_web | fs-write | 1 | ungated | medium |
| skills/sigma-kg/scripts/kg.py | warn | fs-write | 2 | ungated | medium |
| skills/sigma-kg/scripts/kg.py | write_note | fs-write | 2 | ungated | medium |
| skills/sigma-loop/scripts/acceptance.py | record | fs-remove | 1 | ungated | high |
| skills/sigma-loop/scripts/acceptance.py | record | fs-write | 2 | ungated | medium |
| skills/sigma-loop/scripts/actionlog.py | append | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/agent_watch.py | _save_cursor | fs-write | 2 | ungated | medium |
| skills/sigma-loop/scripts/autowatch.py | _record_spend | fs-write | 2 | ungated | medium |
| skills/sigma-loop/scripts/backlog_check.py | _dense_channel | fs-write | 2 | ungated | medium |
| skills/sigma-loop/scripts/channel_notify.py | _real_post | network-post | 1 | http(s) loopback URL; allow_remote_webhook is exactly true for remote delivery | high |
| skills/sigma-loop/scripts/channel_notify.py | _save_cursor | fs-write | 2 | ungated | medium |
| skills/sigma-loop/scripts/claim_refs.py | _push | git-push | 1 | library only: called solely by the claim verbs (create, renew, reclaim, delete), always through the explicit --force-with-lease=<ref>:<expected> form on refs/sigma/claims/ (never a plain force); no caller exists in this slice; slice 3 gates it on claims.mode=git | high |
| skills/sigma-loop/scripts/codex_runtime.py | lock_file | fs-write | 1 | Codex supervisor opt-in only; creates and holds one per-checkout lock file, refuses if the OS lock cannot be acquired | medium |
| skills/sigma-loop/scripts/coexist.py | _mark | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/coexist.py | backup_features | fs-remove | 1 | ungated | high |
| skills/sigma-loop/scripts/coexist.py | backup_features | fs-rmtree | 1 | ungated | high |
| skills/sigma-loop/scripts/coexist.py | backup_features | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/coexist.py | clear_watch_owner | fs-remove | 1 | ungated | high |
| skills/sigma-loop/scripts/coexist.py | write_owner | fs-remove | 2 | ungated | high |
| skills/sigma-loop/scripts/coexist.py | write_owner | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/coexist.py | write_watch_owner | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/comment_watch.py | _save_cursor | fs-write | 2 | ungated | medium |
| skills/sigma-loop/scripts/cross_repo.py | _record | fs-write | 2 | ungated | medium |
| skills/sigma-loop/scripts/diff_revert.py | cleanup | fs-remove | 1 | ungated | high |
| skills/sigma-loop/scripts/diff_revert.py | cleanup | fs-rmtree | 1 | ungated | high |
| skills/sigma-loop/scripts/diff_revert.py | cleanup | git-destructive | 1 | ungated | high |
| skills/sigma-loop/scripts/diff_revert.py | run | fs-write | 3 | ungated | medium |
| skills/sigma-loop/scripts/drift_watch.py | _stamp | fs-write | 2 | ungated | medium |
| skills/sigma-loop/scripts/feature_backup.py | _atomic_leased_push | git-destructive | 1 | upkeep.enabled; the caller that decides to back up holds the gate (the engine's gate query), and restore and prune query it themselves; no entry point is registered | high |
| skills/sigma-loop/scripts/feature_backup.py | _atomic_leased_push | git-push | 1 | upkeep.enabled; the caller that decides to back up holds the gate (the engine's gate query), and restore and prune query it themselves; no entry point is registered | high |
| skills/sigma-loop/scripts/feature_backup.py | _delete_chunk | git-destructive | 1 | upkeep.enabled; the caller that decides to back up holds the gate (the engine's gate query), and restore and prune query it themselves; no entry point is registered | high |
| skills/sigma-loop/scripts/feature_backup.py | _delete_chunk | git-push | 1 | upkeep.enabled; the caller that decides to back up holds the gate (the engine's gate query), and restore and prune query it themselves; no entry point is registered | high |
| skills/sigma-loop/scripts/feature_doc.py | _atomic_write_bytes | fs-remove | 2 | ungated | high |
| skills/sigma-loop/scripts/feature_doc.py | _atomic_write_bytes | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/feature_judge.py | _acquire_spend_lock | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/feature_judge.py | _record_spend | fs-write | 2 | ungated | medium |
| skills/sigma-loop/scripts/feature_land.py | _pull_request | gh-api-write | 1 | upkeep.enabled and the landing verb; creates one non-draft pull request for the verified head only after verify, the tip re-reads and the unattended approval check passed; never merges | medium |
| skills/sigma-loop/scripts/feature_land.py | _scratch_verify | git-destructive | 1 | upkeep.enabled and the landing verb; removes only the scratch worktree this call created in a private temporary directory | high |
| skills/sigma-loop/scripts/feature_land_merge.py | do_merge | gh-api-write | 1 | upkeep.enabled and an explicit merge flag; explicit land; attended, or one unconsumed unit approval bound to PR head SHA; verify green; head-pinned; pending record written first; explicit merge method; no auto-merge, no admin flag, no branch-delete flag | high |
| skills/sigma-loop/scripts/feature_propagate.py | _store | fs-write | 2 | ungated | medium |
| skills/sigma-loop/scripts/feature_propagate.py | _write_remote | gh-api-write | 1 | granted verdict | high |
| skills/sigma-loop/scripts/feature_rebase.py | _clear_blocked | fs-remove | 1 | work.rebase_upkeep | high |
| skills/sigma-loop/scripts/feature_rebase.py | _clear_parked | fs-remove | 1 | upkeep.enabled | high |
| skills/sigma-loop/scripts/feature_rebase.py | _drop_worktree | fs-rmtree | 1 | work.rebase_upkeep | high |
| skills/sigma-loop/scripts/feature_rebase.py | _drop_worktree | git-destructive | 1 | work.rebase_upkeep | high |
| skills/sigma-loop/scripts/feature_rebase.py | _forget | fs-write | 1 | upkeep.enabled | medium |
| skills/sigma-loop/scripts/feature_rebase.py | _mark_blocked | fs-write | 2 | work.rebase_upkeep | medium |
| skills/sigma-loop/scripts/feature_rebase.py | _mark_parked | fs-write | 2 | upkeep.enabled | medium |
| skills/sigma-loop/scripts/feature_rebase.py | _pushed | git-destructive | 1 | work.rebase_upkeep | high |
| skills/sigma-loop/scripts/feature_rebase.py | _pushed | git-push | 1 | work.rebase_upkeep | high |
| skills/sigma-loop/scripts/feature_rebase.py | _rebase_feature | fs-write | 1 | work.rebase_upkeep | medium |
| skills/sigma-loop/scripts/feature_rebase.py | _remember | fs-write | 2 | work.rebase_upkeep | medium |
| skills/sigma-loop/scripts/feature_rebase.py | _notice | gh-api-write | 1 | upkeep.enabled | medium |
| skills/sigma-loop/scripts/feature_rebase.py | _settle_parks | gh-api-write | 3 | upkeep.enabled | medium |
| skills/sigma-loop/scripts/feature_rebase.py | _upkeep | fs-write | 1 | work.rebase_upkeep | medium |
| skills/sigma-loop/scripts/feature_rebase.py | _write_runtime_acks | fs-write | 2 | upkeep.enabled | medium |
| skills/sigma-loop/scripts/feature_rebase.py | ack | fs-write | 2 | work.rebase_upkeep | medium |
| skills/sigma-loop/scripts/feature_rebase.py | mark_push_refused | fs-write | 2 | work.rebase_upkeep | medium |
| skills/sigma-loop/scripts/feature_rebase.py | push_refused | fs-remove | 1 | work.rebase_upkeep | high |
| skills/sigma-loop/scripts/feature_registry.py | _atomic_write_text | fs-remove | 2 | ungated | high |
| skills/sigma-loop/scripts/feature_registry.py | _atomic_write_text | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/feature_sync.py | _acquire | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/feature_sync.py | recover | fs-remove | 1 | explicit `feature_sync.py recover --discard`; renames Sigma's own recovery copy of the registry sheet aside, never deletes it | medium |
| skills/sigma-loop/scripts/feature_upkeep_job.py | __call__ | fs-write | 1 | upkeep.enabled (machine door, the job is started only by the gated decision step); refreshes only the job's own heartbeat file from the supervisor's poll clock | low |
| skills/sigma-loop/scripts/feature_upkeep_job.py | _touch | fs-write | 1 | upkeep.enabled (machine door); touches only the job's own heartbeat file under the runtime state directory, an OS error is swallowed | low |
| skills/sigma-loop/scripts/feature_upkeep_landing.py | finish | fs-remove | 1 | upkeep.enabled and a merged read-back verdict; removes only the one record of the unit it was handed inside the pending-landing store, after the unit key passes the name guard | high |
| skills/sigma-loop/scripts/feature_upkeep_landing.py | prune | fs-remove | 1 | upkeep.enabled caller passes keep_days; removes only record-shaped and temp-shaped files older than the age limit in the pending-landing store, never a symlink, never outside it | high |
| skills/sigma-loop/scripts/feature_upkeep_launcher.py | _acquire_lock | fs-write | 1 | opt-in; conflicts.resolve agent; creates only the empty lock file beside the per-machine spend store under the local state directory | medium |
| skills/sigma-loop/scripts/feature_upkeep_launcher.py | _save_records | fs-remove | 1 | opt-in; conflicts.resolve agent; atomic replace of the one per-machine spend file under the local state directory, flock-guarded, pruned to 24 hours | high |
| skills/sigma-loop/scripts/feature_upkeep_launcher.py | _save_records | fs-write | 1 | opt-in; one JSON file under the local state directory, flock-guarded, pruned to 24 hours | medium |
| skills/sigma-loop/scripts/feature_upkeep_launcher.py | make_scratch | fs-write | 2 | opt-in upkeep, conflicts.resolve agent, machine opt-in; writes only under a scratch root it created; never inside the repository or home | medium |
| skills/sigma-loop/scripts/feature_upkeep_launcher.py | remove_scratch | fs-rmtree | 1 | removes only the run directory it created, checked by realpath under its own scratch root | high |
| skills/sigma-loop/scripts/feature_upkeep_pass.py | ack_union | fs-remove | 2 | upkeep.enabled (project door); unlinks only its own temp file beside the acks file after a failed write | high |
| skills/sigma-loop/scripts/feature_upkeep_pass.py | ack_union | fs-write | 1 | upkeep.enabled (project door, the decorator is the first action); writes only the one runtime acks file under state/upkeep, atomically (temp file beside it, then replace) | medium |
| skills/sigma-loop/scripts/feature_upkeep_pass.py | rewrite | git-destructive | 1 | upkeep.enabled (project door, run_unit_pass is gated and so is upkeep_pass); removes only the throwaway worktree the same call created, never a branch or ref | high |
| skills/sigma-loop/scripts/feature_upkeep_resolution.py | post_comment | gh-api-write | 1 | upkeep.enabled and a finding the engine filed; integer finding id and explicit owner/name repository; text passes the wording check; one comment, never edited or closed here | medium |
| skills/sigma-loop/scripts/feature_upkeep_resolution.py | prune | fs-remove | 1 | upkeep.enabled caller passes keep_days; removes only record-shaped files in the engine's own resolutions store, never a symlink, never outside it | high |
| skills/sigma-loop/scripts/feature_upkeep_level2.py | _write_attempts | fs-remove | 1 | opt-in; upkeep.enabled and conflicts.resolve agent; atomic replace of the one per-unit attempts file under the local state directory, bounded entry count | high |
| skills/sigma-loop/scripts/feature_upkeep_level2.py | _write_attempts | fs-write | 2 | opt-in; upkeep.enabled and conflicts.resolve agent; one per-unit attempts file under the local state directory, written to a temporary name then replaced | medium |
| skills/sigma-loop/scripts/feature_rebase.py | _default_level2_plan | fs-write | 1 | opt-in; upkeep.enabled, conflicts.resolve agent and both conflicts.agent spend keys set; creates three empty directories under the system temp directory for the resolver export, scratch and review, never in the work tree | medium |
| skills/sigma-loop/scripts/feature_upkeep_level2.py | _sweep_write | fs-remove | 1 | opt-in; upkeep.enabled and conflicts.resolve agent; atomic replace of the one sweep counter file under the local state directory | high |
| skills/sigma-loop/scripts/feature_upkeep_level2.py | _sweep_write | fs-write | 2 | opt-in; upkeep.enabled and conflicts.resolve agent; the one sweep counter file under the local state directory, written to a temporary name then replaced | medium |
| skills/sigma-loop/scripts/feature_upkeep_level2.py | export_tree | fs-write | 1 | opt-in; upkeep.enabled and conflicts.resolve agent; copies tracked files into a fresh scratch tree made by this pass, never into the work tree | medium |
| skills/sigma-loop/scripts/feature_upkeep_level2.py | resolve_stop | fs-rmtree | 1 | opt-in; upkeep.enabled and conflicts.resolve agent; removes only the scratch directory this pass created for the resolver, never the work tree or a ref | high |
| skills/sigma-loop/scripts/feature_upkeep_level2.py | resolve_stop | fs-write | 2 | opt-in; upkeep.enabled and conflicts.resolve agent; writes resolved file text back into the unit worktree for the stop being resolved, checked by proof and review before any push | high |
| skills/sigma-loop/scripts/feature_upkeep_review.py | prune | fs-remove | 1 | opt-in; upkeep.enabled caller passes keep_days; removes only manifest-shaped regular files older than the age limit under the review store, never a symlink, never outside it | high |
| skills/sigma-loop/scripts/feature_upkeep_review.py | write_manifest | fs-write | 1 | opt-in; upkeep.enabled; one create-once manifest per unit under the review store in the local state directory | medium |
| skills/sigma-loop/scripts/gh_api.py | _write_cache | fs-remove | 2 | probe cache (cache_dir injected, unwired) and the read breaker / shared bounded fallback log (reads and writes, cap 200, no text/argv/body) under .sdlc/state, written only with an sdlc_dir, after state.refuse_symlinks vets the file and its .tmp (#708, #895) | low |
| skills/sigma-loop/scripts/gh_api.py | _write_cache | fs-write | 2 | probe cache (cache_dir injected, unwired) and the read breaker / shared bounded fallback log (reads and writes, cap 200, no text/argv/body) under .sdlc/state, written only with an sdlc_dir, after state.refuse_symlinks vets the file and its .tmp (#708, #895) | low |
| skills/sigma-loop/scripts/goal_state_prune.py | sweep | fs-remove | 4 | goal is terminal (newest action-log internal row is recorded done), nothing of it younger than 7 days, no work record, no live agent marker; regular non-symlink files directly under owned state dirs only; run_stop markers by 30-day age (#458) | medium |
| skills/sigma-loop/scripts/ledger.py | _maybe_prune_journal | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/ledger.py | append | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/ledger.py | main | fs-write | 2 | ungated | medium |
| skills/sigma-loop/scripts/ledger.py | prune_journal | fs-remove | 1 | ungated | high |
| skills/sigma-loop/scripts/liveness_prune.py | _try_remove_lock | fs-remove | 1 | claim lock only: mtime older than max(claim lease TTL, 1 h), no alive or unknown agent marker, goal in no live session's in_flight, this process wins a non-blocking flock opened O_NOFOLLOW (never O_CREAT), the locked inode is still the path's inode, mtime re-checked under the lock (#464) | medium |
| skills/sigma-loop/scripts/liveness_prune.py | sweep | fs-remove | 1 | claims/<goal>.claimed only: regular non-symlink file in a real claims dir, older than 30 days, no work record, no alive or unknown agent marker; capped by --limit and a wall-clock budget (#464) | medium |
| skills/sigma-loop/scripts/logroll.py | _acquire | fs-remove | 1 | log rotation lock only: removes a `<log>.rotating` lock file older than 60 s, left by a crashed rotator; never a log; best-effort, never raises | medium |
| skills/sigma-loop/scripts/logroll.py | _move | fs-remove | 1 | log rotation only: one os.replace of a caller-named log to its own `.N` predecessor in the same directory; atomic, the oldest generation is overwritten by design; best-effort, never raises | medium |
| skills/sigma-loop/scripts/logroll.py | rotate | fs-remove | 1 | log rotation lock only: releases the `<log>.rotating` lock this call took; never a log; best-effort, never raises | medium |
| skills/sigma-loop/scripts/loop.py | _end_if_owner | fs-remove | 2 | ungated | high |
| skills/sigma-loop/scripts/loop.py | _ensure_claimed | fs-write | 2 | ungated | medium |
| skills/sigma-loop/scripts/loop.py | _ensure_ledger_delivery | fs-write | 2 | ungated | medium |
| skills/sigma-loop/scripts/loop.py | _ensure_unit_tracking | fs-write | 2 | ungated | medium |
| skills/sigma-loop/scripts/loop.py | _merge_reconcile_lock | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/loop.py | _phase_marker_end | fs-remove | 1 | ungated | high |
| skills/sigma-loop/scripts/loop.py | _prune | fs-remove | 1 | ungated | high |
| skills/sigma-loop/scripts/loop.py | _prune_dead_session_entries | fs-remove | 1 | ungated | high |
| skills/sigma-loop/scripts/loop.py | _reconcile_stamp | fs-write | 2 | ungated | medium |
| skills/sigma-loop/scripts/loop.py | _reserve_goal_slot | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/loop.py | _session_claim | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/loop.py | _try_acquire_claim_lock | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/loop.py | _write | fs-remove | 2 | ungated | high |
| skills/sigma-loop/scripts/loop.py | _write | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/loop.py | agent_end | fs-rmtree | 1 | ungated | high |
| skills/sigma-loop/scripts/loop.py | agent_reclaim | fs-remove | 1 | ungated | high |
| skills/sigma-loop/scripts/loop.py | agent_start | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/loop.py | reclaim_stale_claim_lock | fs-remove | 1 | ungated | high |
| skills/sigma-loop/scripts/loop.py | session_start | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/loop.py | verify_goal | fs-write | 2 | ungated | medium |
| skills/sigma-loop/scripts/loop.py | write_session_heartbeat | fs-remove | 2 | ungated | high |
| skills/sigma-loop/scripts/loop.py | write_session_heartbeat | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/managed_settings.py | record_enrolment | fs-remove | 1 | enrolment marker: unique tmp file renamed over (os.replace) or cleaned up; written only after a valid policy file was parsed, best-effort, never raises | low |
| skills/sigma-loop/scripts/managed_settings.py | record_enrolment | fs-write | 2 | enrolment marker (state dir and git dir), written once after a valid policy file was parsed; local, no network | low |
| skills/sigma-loop/scripts/managed_settings.py | unenroll | fs-remove | 1 | operator-run `managed_settings.py unenroll`: removes only the enrolment markers; local, idempotent | low |
| skills/sigma-loop/scripts/merge_observation.py | materialize_snapshot | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/merge_observation.py | write_immutable | fs-remove | 2 | ungated | high |
| skills/sigma-loop/scripts/merge_observation.py | write_immutable | fs-write | 2 | ungated | medium |
| skills/sigma-loop/scripts/mirror.py | _write | fs-write | 3 | ungated | medium |
| skills/sigma-loop/scripts/phase_report.py | write_marker | fs-write | 2 | ungated | medium |
| skills/sigma-loop/scripts/pipeline.py | main | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/pipeline.py | propose_from_discovery | fs-write | 2 | ungated | medium |
| skills/sigma-loop/scripts/pipeline.py | propose_goals | fs-write | 2 | ungated | medium |
| skills/sigma-loop/scripts/reconcile.py | _write | fs-remove | 1 | ungated | high |
| skills/sigma-loop/scripts/reconcile.py | _write | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/release_manifest.py | publish_to_ledger_branch | git-push | 1 | ungated | high |
| skills/sigma-loop/scripts/release_manifest.py | write_once | fs-remove | 1 | ungated | high |
| skills/sigma-loop/scripts/release_manifest.py | write_once | fs-write | 2 | ungated | medium |
| skills/sigma-loop/scripts/retention.py | _unlink_streams | fs-remove | 1 | retention gate: only a closed-as-done goal's own log and witness file, both older than the window, no fresh owner marker, no symlink, size and mtime re-checked before the unlink; best-effort, never raises | medium |
| skills/sigma-loop/scripts/review_context.py | _atomic_bytes | fs-remove | 2 | ungated | high |
| skills/sigma-loop/scripts/review_context.py | _atomic_bytes | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/slack_commands_listen.py | _atomic_write_text | fs-remove | 2 | ungated | high |
| skills/sigma-loop/scripts/slack_commands_listen.py | _atomic_write_text | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/slack_commands_listen.py | _log | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/slack_commands_listen.py | acquire_single_instance | fs-remove | 2 | ungated | high |
| skills/sigma-loop/scripts/slack_commands_listen.py | acquire_single_instance | fs-write | 3 | ungated | medium |
| skills/sigma-loop/scripts/slack_commands_listen.py | cut_worktree | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/slack_commands_listen.py | release_single_instance | fs-remove | 2 | ungated | high |
| skills/sigma-loop/scripts/sources.py | _apply_custom_fields | gh-project | 2 | discovery.source == github; board writes require project.enabled | medium |
| skills/sigma-loop/scripts/sources.py | _archive_card | graphql-mutation | 1 | discovery.source == github; board writes require project.enabled | medium |
| skills/sigma-loop/scripts/sources.py | _ensure_board | gh-project | 1 | discovery.source == github; board writes require project.enabled | medium |
| skills/sigma-loop/scripts/sources.py | _ensure_labels | gh-label | 1 | discovery.source == github; board writes require project.enabled | medium |
| skills/sigma-loop/scripts/sources.py | _ensure_priority_field | gh-project | 1 | discovery.source == github; board writes require project.enabled | medium |
| skills/sigma-loop/scripts/sources.py | _ensure_status_field | gh-project | 1 | discovery.source == github; board writes require project.enabled | medium |
| skills/sigma-loop/scripts/sources.py | _gh_json | gh-label | 1 | discovery.source == github; board writes require project.enabled | medium |
| skills/sigma-loop/scripts/sources.py | _graphql | graphql-mutation | 1 | discovery.source == github; board writes require project.enabled | medium |
| skills/sigma-loop/scripts/sources.py | _issue_add_assignees | gh-api-write | 1 | discovery.source == github; REST issue write on the goal issue, reached only from the loop's own claim, park and close paths | medium |
| skills/sigma-loop/scripts/sources.py | _issue_add_labels | gh-api-write | 1 | discovery.source == github; REST issue write on the goal issue, reached only from the loop's own claim, park and close paths | medium |
| skills/sigma-loop/scripts/sources.py | _issue_close | gh-api-write | 1 | discovery.source == github; REST issue write on the goal issue, reached only from the loop's own claim, park and close paths | medium |
| skills/sigma-loop/scripts/sources.py | _issue_comment | gh-api-write | 1 | discovery.source == github; REST issue write on the goal issue, reached only from the loop's own claim, park and close paths | medium |
| skills/sigma-loop/scripts/sources.py | _issue_create | gh-api-write | 1 | discovery.source == github; REST issue write on the goal issue, reached only from the loop's own claim, park and close paths | medium |
| skills/sigma-loop/scripts/sources.py | _issue_edit_body | gh-api-write | 1 | discovery.source == github; REST issue write on the goal issue, reached only from the loop's own claim, park and close paths | medium |
| skills/sigma-loop/scripts/sources.py | _issue_remove_label | gh-api-write | 1 | discovery.source == github; REST issue write on the goal issue, reached only from the loop's own claim, park and close paths | medium |
| skills/sigma-loop/scripts/sources.py | _run_gh | gh-label | 1 | discovery.source == github; board writes require project.enabled | medium |
| skills/sigma-loop/scripts/sources.py | _set_board_status | gh-project | 1 | discovery.source == github; board writes require project.enabled | medium |
| skills/sigma-loop/scripts/sources.py | _swap_labels | graphql-mutation | 1 | discovery.source == github; board writes require project.enabled | medium |
| skills/sigma-loop/scripts/sources.py | _sync_backlog | gh-project | 1 | discovery.source == github; board writes require project.enabled | medium |
| skills/sigma-loop/scripts/sources.py | _write_board_phase | gh-project | 1 | discovery.source == github; board writes require project.enabled | medium |
| skills/sigma-loop/scripts/sources.py | _write_priority_field | gh-project | 1 | discovery.source == github; board writes require project.enabled | medium |
| skills/sigma-loop/scripts/sources.py | create_dependency | fs-write | 2 | discovery.source == github; board writes require project.enabled | medium |
| skills/sigma-loop/scripts/sources.py | create_dependency | gh-label | 1 | discovery.source == github; board writes require project.enabled | medium |
| skills/sigma-loop/scripts/sources.py | ensure_labels_report | gh-label | 1 | discovery.source == github; board writes require project.enabled | medium |
| skills/sigma-loop/scripts/sources.py | note | fs-write | 1 | discovery.source == github; board writes require project.enabled | medium |
| skills/sigma-loop/scripts/sources.py | note | gh-api-write | 1 | discovery.source == github; board writes require project.enabled | medium |
| skills/sigma-loop/scripts/sources.py | note | gh-issue | 1 | discovery.source == github; board writes require project.enabled | medium |
| skills/sigma-loop/scripts/spend_approval.py | _counter_write | fs-remove | 1 | opt-in (spend_approval.enabled); writes only the one per-machine use counter file under the local state directory (atomic temp then replace); never deletes another writer's file; a failed write denies the grant | medium |
| skills/sigma-loop/scripts/spend_approval.py | _counter_write | fs-write | 2 | opt-in (spend_approval.enabled); writes only the one per-machine use counter file under the local state directory (atomic temp then replace); never deletes another writer's file; a failed write denies the grant | medium |
| skills/sigma-loop/scripts/state.py | _patch_cursor | fs-remove | 1 | ungated | high |
| skills/sigma-loop/scripts/state.py | _queue | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/state.py | _state_file | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/state.py | atomic_write_text | fs-remove | 2 | ungated | high |
| skills/sigma-loop/scripts/state.py | claim_run_stop | fs-write | 2 | ungated | medium |
| skills/sigma-loop/scripts/state.py | phase_end_lock | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/state.py | reanchor_content | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/state.py | refuse_symlinks | fs-write | 1 | creates missing parent directories under .sdlc only, one level at a time, after an lstat symlink refusal (#708) | medium |
| skills/sigma-loop/scripts/state.py | session_locked | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/state.py | session_write | fs-remove | 2 | ungated | high |
| skills/sigma-loop/scripts/supervise_daemon.py | _capture_codex | fs-remove | 2 | Codex host only; removes only the fixed .sdlc/state/supervisor.agent-last transient message before launch and after inspection under the per-checkout OS lock | high |
| skills/sigma-loop/scripts/supervise_daemon.py | _codex_main | fs-write | 2 | Codex host only; state directory creation follows explicit host selection, and supervisor log/tail writes run under the per-checkout OS lock after enabled-plugin preflight | medium |
| skills/sigma-loop/scripts/supervise_daemon.py | main | fs-write | 4 | ungated | medium |
| skills/sigma-loop/scripts/sync.py | _aside | fs-write | 1 | operator-run `sync.py bootstrap` only (#1142), and only after `_shares_history` proves the ops branch shares no commit with the remote's; creates `.sdlc/state/orphaned/<stamp>/<channel>/` to hold files the re-root will not publish, never overwriting an earlier copy | medium |
| skills/sigma-loop/scripts/sync.py | _ensure_gitattributes | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/sync.py | _ensure_lines | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/sync.py | _knowledge_lock | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/sync.py | _prune | fs-remove | 3 | ungated | high |
| skills/sigma-loop/scripts/sync.py | _publish_knowledge | git-destructive | 1 | ungated | high |
| skills/sigma-loop/scripts/sync.py | _push_with_retry | git-push | 1 | ungated | high |
| skills/sigma-loop/scripts/sync.py | _record_push | git-ref-write | 1 | after a push of HEAD to `<remote>/<branch>` succeeded; sets the local remote-tracking ref to exactly that commit, which a narrow-refspec push does not move (#1142) | high |
| skills/sigma-loop/scripts/sync.py | _reroot | fs-remove | 2 | operator-run `sync.py bootstrap` only (#1142), and only after `_shares_history` proves the ops branch shares no commit with the remote's; removes only its own `.sdlc/state/orphaned/*/ledger/PENDING` markers: a superseded one, and its own once the settle step completes | high |
| skills/sigma-loop/scripts/sync.py | _reroot | fs-write | 1 | operator-run `sync.py bootstrap` only (#1142), and only after `_shares_history` proves the ops branch shares no commit with the remote's; writes the `.sdlc/state/orphaned/<stamp>/ledger/PENDING` marker (the old tip's hash) that lets the next bootstrap finish an interrupted settle step | medium |
| skills/sigma-loop/scripts/sync.py | _reroot | git-ref-write | 1 | operator-run `sync.py bootstrap` only (#1142), and only after `_shares_history` proves the ops branch shares no commit with the remote's; writes `refs/sigma/orphaned/<channel>/<stamp>`, a local backup of the old tip that is never pushed | high |
| skills/sigma-loop/scripts/sync.py | _resume_reroot | fs-remove | 1 | operator-run `sync.py bootstrap` only (#1142); removes a `.sdlc/state/orphaned/<stamp>/ledger/PENDING` marker after finishing the settle step it records | high |
| skills/sigma-loop/scripts/sync.py | _union_note | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/sync.py | _write_knowledge_gitignore | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/sync.py | _write_team | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/sync.py | bootstrap | fs-write | 2 | ungated | medium |
| skills/sigma-loop/scripts/sync.py | init | fs-write | 2 | ungated | medium |
| skills/sigma-loop/scripts/sync.py | init | git-destructive | 1 | ungated | high |
| skills/sigma-loop/scripts/sync.py | publish_receipt | git-push | 1 | ungated | high |
| skills/sigma-loop/scripts/tier_escalation.py | write_floor | fs-remove | 1 | ungated | high |
| skills/sigma-loop/scripts/tier_escalation.py | write_floor | fs-write | 2 | ungated | medium |
| skills/sigma-loop/scripts/timing_store.py | _sweep | fs-remove | 2 | ungated | high |
| skills/sigma-loop/scripts/timing_store.py | append | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/timing_store.py | append_session | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/timing_store.py | maybe_prune | fs-write | 2 | ungated | medium |
| skills/sigma-loop/scripts/triage.py | _atomic_write_text | fs-remove | 1 | ungated | high |
| skills/sigma-loop/scripts/triage.py | _atomic_write_text | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/triage.py | _ensure_arbitrary_labels | gh-label | 1 | ungated | medium |
| skills/sigma-loop/scripts/upstream.py | _file_upstream | gh-api-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/upstream.py | _remember | fs-write | 2 | ungated | medium |
| skills/sigma-loop/scripts/upstream.py | _spill | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/watch.py | clear_inbox | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/watch.py | tick | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/watch_classify.py | save_cursor | fs-write | 2 | ungated | medium |
| skills/sigma-loop/scripts/watch_daemon.py | _run | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/watch_daemon.py | acquire_decision_mutex | fs-remove | 2 | ungated | high |
| skills/sigma-loop/scripts/watch_daemon.py | acquire_decision_mutex | fs-write | 3 | ungated | medium |
| skills/sigma-loop/scripts/watch_daemon.py | cleanup | fs-remove | 2 | ungated | high |
| skills/sigma-loop/scripts/watch_daemon.py | release_decision_mutex | fs-remove | 1 | ungated | high |
| skills/sigma-loop/scripts/watch_daemon.py | rotate_log | fs-remove | 1 | ungated | high |
| skills/sigma-loop/scripts/watch_daemon.py | take_over | fs-remove | 2 | ungated | high |
| skills/sigma-loop/scripts/watch_daemon.py | take_over | fs-write | 2 | ungated | medium |
| skills/sigma-loop/scripts/watch_daemon.py | touch_heartbeat | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/witness.py | record | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/work.py | _clear_completed_merge_deliveries | fs-remove | 1 | ungated | high |
| skills/sigma-loop/scripts/work.py | _close_issue_the_base_cannot | gh-api-write | 1 | work.enabled; base branch cannot close the issue | high |
| skills/sigma-loop/scripts/work.py | _create_exclusive | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/work.py | _delete_remote_branch | gh-api-write | 1 | work.enabled; merged PR cleanup; non-empty goal prefix; never base/default branch | high |
| skills/sigma-loop/scripts/work.py | _repair_review_post_effects | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/work.py | _save | fs-write | 2 | ungated | medium |
| skills/sigma-loop/scripts/work.py | _try_union_changelog | fs-write | 1 | CHANGELOG-only provably lossless conflicts; the heading-aware placement runs only under upkeep.enabled with conflicts.resolve mechanical or agent, else the legacy union | medium |
| skills/sigma-loop/scripts/work.py | _write_merge_delivery | fs-remove | 2 | ungated | high |
| skills/sigma-loop/scripts/work.py | _write_merge_delivery | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/work.py | close_design | gh-api-write | 1 | ungated (risk-reducing direction); a REST comment POST then PATCH state=closed via gh_api.close_pr_gh (ONE `gh pr close` fallback per the write policy); no retry; an ambiguous outcome is reconciled by a REST read, never counted as closed without a positive CLOSED (#895 slice 4b-3) | high |
| skills/sigma-loop/scripts/work.py | finish | fs-remove | 2 | ungated | high |
| skills/sigma-loop/scripts/work.py | finish | fs-rmtree | 1 | ungated | high |
| skills/sigma-loop/scripts/work.py | finish | git-destructive | 2 | ungated | high |
| skills/sigma-loop/scripts/work.py | merge | gh-pr | 1 | work.enabled; work.auto_merge != off; merge rights; fresh verify evidence and CLEAN PR; the one remaining site is the `--auto` arm (GraphQL, only while graphql_available); the direct merge goes through gh_api.merge_pr_gh (REST PUT pulls/N/merge with the vetted head sha, invisible to this scanner) since #895 slice 4b-1 | high |
| skills/sigma-loop/scripts/work.py | merge | gh-api-write | 1 | work.enabled; work.auto_merge != off; merge rights; fresh verify evidence and CLEAN PR; the direct merge is the REST PUT pulls/N/merge with the vetted head sha via gh_api.merge_pr_gh (non-idempotent; an unknown outcome is READ, never re-merged), counted since #895 slice 4b-2 | high |
| skills/sigma-loop/scripts/work.py | merge_design | gh-api-write | 1 | work.enabled; work.auto_merge != off; one non-idempotent REST PUT pulls/N/merge pinned to the vetted head sha via gh_api.merge_pr_gh (ONE `gh pr merge` fallback only on a primary rate limit while GraphQL is available); no retry; an ambiguous outcome is reconciled by a REST read of the PR, never counted as merged without a positive MERGED (#895 slice 4b-3) | high |
| skills/sigma-loop/scripts/work.py | post_review | gh-api-write | 1 | evidence-bound to this goal's review generation and head; one non-idempotent REST comment POST via gh_api.comment_pr (ONE `gh pr comment` fallback only on a primary rate limit while GraphQL is available); an ambiguous outcome parks and reconciles by the immutable marker, never re-posts (#895 slice 4b-2) | medium |
| skills/sigma-loop/scripts/work.py | pr | gh-api-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/work.py | pr | git-push | 1 | ungated | high |
| skills/sigma-loop/scripts/work.py | prune_terminal_review_copies | fs-rmtree | 1 | ungated | high |
| skills/sigma-loop/scripts/work.py | prune_terminal_review_generations | fs-remove | 3 | ungated | high |
| skills/sigma-loop/scripts/work.py | prune_terminal_review_generations | fs-rmtree | 1 | ungated | high |
| skills/sigma-loop/scripts/work.py | rebase | git-destructive | 3 | ungated | high |
| skills/sigma-loop/scripts/work.py | rebase | git-push | 2 | ungated | high |
| skills/sigma-loop/scripts/work.py | review_evidence | fs-write | 1 | ungated | medium |
| skills/sigma-loop/scripts/worktree_prune.py | _atomic_write | fs-remove | 2 | the replace over the sweep's own journal or seen file, and unlinking its own temp file next to it; nothing else (#465) | low |
| skills/sigma-loop/scripts/worktree_prune.py | _atomic_write | fs-write | 2 | the sweep's own state only (state/worktree-prune/<goal>.json journal and state/worktree-prune-seen.json); tmp file then os.replace, under the sweep flock (#465) | low |
| skills/sigma-loop/scripts/worktree_prune.py | _drop_journal | fs-remove | 1 | unlinks only state/worktree-prune/<goal>.json, the sweep's own journal, under the sweep flock; missing_ok (#465) | low |
| skills/sigma-loop/scripts/worktree_prune.py | _heal | fs-rmtree | 1 | debris of the sweep's OWN interrupted removal only: a journal exists whose record, branch, path and HEAD still match, the path is exactly <project root>/<worktree_dir>/<goal> and not a symlink, git no longer registers it, and every remaining entry is regenerable or byte-identical to the blob at that path in the journalled HEAD (read in the main repo; a foreign file keeps it); re-checked immediately before the delete; under the sweep flock (#465) | high |
| skills/sigma-prd-intake/scripts/intake.py | _write_json | fs-remove | 1 | ungated | high |
| skills/sigma-prd-intake/scripts/intake.py | _write_json | fs-write | 2 | ungated | medium |
| skills/sigma-radar/scripts/radar.py | record | fs-write | 1 | ungated | medium |
| skills/sigma-rebase/scripts/conflict_walk.py | _resolve_to_stage | git-destructive | 1 | ungated | high |
| skills/sigma-rebase/scripts/rebase_brief.py | _write_context_store | fs-remove | 1 | ungated | high |
| skills/sigma-rebase/scripts/rebase_brief.py | _write_context_store | fs-write | 1 | ungated | medium |
| skills/sigma-rebase/scripts/rebase_brief.py | attempt_rebase | git-destructive | 1 | ungated | high |
| skills/sigma-rebase/scripts/rebase_brief.py | attended_rebase | fs-write | 1 | upkeep.enabled (the legacy call is untouched while closed); creates only the unit's rebase-lock directory under state, then holds the per-unit lock for the one attempt | medium |
| skills/sigma-rebase/scripts/rebase_brief.py | clear_context_snapshots | fs-remove | 1 | ungated | high |
| skills/sigma-rebase/scripts/rebase_brief.py | push_branch | git-destructive | 1 | ungated | high |
| skills/sigma-rebase/scripts/rebase_brief.py | push_branch | git-push | 1 | ungated | high |
| skills/sigma-rebase/scripts/verify_merge.py | _write_delivery | fs-remove | 1 | human input() confirmation | high |
| skills/sigma-rebase/scripts/verify_merge.py | _write_delivery | fs-write | 1 | human input() confirmation | medium |
| skills/sigma-rebase/scripts/verify_merge.py | ensure_landing_pr | gh-pr | 2 | human input() confirmation | medium |
| skills/sigma-rebase/scripts/verify_merge.py | merge_pr | gh-pr | 1 | human input() confirmation | high |
| skills/sigma-scope/scripts/assign.py | execute | fs-write | 2 | ungated | medium |
| skills/sigma-scope/scripts/scope.py | main | fs-write | 2 | ungated | medium |
| skills/sigma-setup/scripts/setup.py | ensure_ignore | fs-write | 2 | ungated | medium |
| skills/sigma-setup/scripts/setup.py | write_cfg | fs-remove | 2 | ungated | high |
| skills/sigma-status/scripts/merge_queue_enable.py | create_merge_queue_ruleset | gh-api-write | 1 | exact --yes-enable-merge-queue admin consent | high |
| skills/sigma-status/scripts/merge_queue_enable.py | patch_auto_merge | gh-api-write | 1 | exact --yes-enable-merge-queue admin consent | high |
| tools/build_public_tree.py | _drop_partial | fs-rmtree | 1 | ungated | high |
| tools/build_public_tree.py | _export_repository | fs-rmtree | 1 | ungated | high |
| tools/build_public_tree.py | _main | fs-rmtree | 1 | ungated | high |
| tools/build_public_tree.py | _main | fs-write | 2 | ungated | medium |
| tools/build_public_tree.py | _make_private_dir | fs-write | 1 | ungated | medium |
| tools/build_public_tree.py | _rename_export | fs-remove | 1 | ungated | high |
| tools/build_public_tree.py | _unpublish_rejected | git-destructive | 1 | ungated | high |
| tools/build_public_tree.py | _write_new | fs-remove | 2 | ungated | high |
| tools/build_public_tree.py | _write_new | fs-write | 1 | ungated | medium |
| tools/build_public_tree.py | finalise_report | fs-remove | 1 | ungated | high |
| tools/build_public_tree.py | main | fs-rmtree | 1 | ungated | high |
| tools/build_public_tree.py | materialise | fs-write | 2 | ungated | medium |
| tools/build_public_tree.py | write_report | fs-remove | 1 | ungated | high |
| tools/goal433_attack.py | main | fs-write | 14 | explicit evidence harness; refuses an output outside the requested repository, writes fixtures only in its TemporaryDirectory, and tests/test_goal433_attack.py verifies the documented invocation | medium |
| tools/kg_control.py | _repo | fs-write | 7 | ungated | medium |
| tools/kg_control.py | _write_builder | fs-write | 2 | ungated | medium |
| tools/kg_control.py | main | fs-write | 2 | ungated | medium |
| tools/kg_control.py | run | fs-write | 3 | ungated | medium |
| tools/onboarding_control.py | _drive_local_goal | fs-write | 2 | ungated | medium |
| tools/onboarding_control.py | _edit_config | fs-write | 1 | ungated | medium |
| tools/onboarding_control.py | _env | fs-write | 1 | ungated | medium |
| tools/onboarding_control.py | _fresh_files | fs-write | 2 | ungated | medium |
| tools/onboarding_control.py | _local_goal_work | fs-write | 2 | ungated | medium |
| tools/onboarding_control.py | _scripted_phases | fs-write | 5 | ungated | medium |
| tools/onboarding_control.py | _stub_gh | fs-write | 1 | ungated | medium |
| tools/onboarding_control.py | _write_transcripts | fs-write | 2 | ungated | medium |
| tools/onboarding_control.py | host_install | fs-write | 2 | ungated | medium |
| tools/onboarding_control.py | main | fs-rmtree | 1 | ungated | high |
| tools/onboarding_control.py | main | fs-write | 1 | ungated | medium |
| tools/onboarding_control.py | run_github | fs-write | 7 | ungated | medium |
| tools/onboarding_control.py | run_github | git-push | 1 | ungated | high |
| tools/onboarding_control.py | run_github_process | fs-write | 15 | ungated | medium |
| tools/onboarding_control.py | run_github_process | git-push | 1 | ungated | high |
| tools/onboarding_control.py | run_local | fs-write | 3 | ungated | medium |
| tools/onboarding_control.py | run_readme_gestures | fs-write | 1 | ungated | medium |
| tools/readiness/baseline.py | snapshot | fs-write | 2 | explicit snapshot command; empty destination | medium |
| tools/readiness/baseline.py | snapshot | git-destructive | 1 | explicit snapshot command; empty destination; detached push-disabled clone | medium |
| tools/readiness/bench_tasks.py | __init__ | fs-write | 3 | explicit verify; a pass-through launcher script inside a fresh temporary directory | medium |
| tools/readiness/bench_tasks.py | _verify_command | fs-write | 1 | explicit verify --json PATH; caller-supplied output path | medium |
| tools/readiness/bench_tasks.py | _write_manifest | fs-write | 1 | check and verify with a hidden root write a copy of manifest.json inside a fresh temporary directory; the explicit lock command rewrites the manifest's environment record in place | medium |
| tools/readiness/bench_tasks.py | build_manifest | fs-write | 1 | explicit build-manifest command; manifest.json beside the task directories only | medium |
| tools/readiness/bench_tasks.py | export_tree | fs-write | 1 | explicit verify --external or hidden-from-pr (a fresh directory under the caller-supplied --scratch path) or materialize (the task's own git-ignored repo/); one commit fetched read-only, no history kept | medium |
| tools/readiness/bench_tasks.py | harness_accepts | fs-write | 2 | check and verify; stand-in directories inside a fresh temporary directory | medium |
| tools/readiness/bench_tasks.py | harness_validates | fs-write | 2 | check and verify with a hidden root; stand-in directories inside a fresh temporary directory | medium |
| tools/readiness/bench_tasks.py | hidden_from_pr | fs-write | 5 | explicit hidden-from-pr command; one hidden bundle under the hidden root, outside the repository | medium |
| tools/readiness/bench_tasks.py | materialize_task | fs-rmtree | 1 | explicit materialize command; removes only the tree it just fetched, when its digest is not the recorded one | high |
| tools/readiness/bench_tasks.py | materialize_task | fs-write | 1 | explicit materialize command; the task's own git-ignored repo/ (refused if it exists) and, with --record-digest, its fetch.json | medium |
| tools/readiness/bench_tasks.py | seal | fs-write | 1 | explicit seal command; the named task's task.json only | medium |
| tools/readiness/bench_tasks.py | verify_external | fs-write | 1 | explicit verify --external; fresh directories under the caller-supplied --scratch path | medium |
| tools/readiness/bench_tasks.py | write_lock | fs-write | 1 | explicit lock command; environment.lock beside the manifest and the manifest's environment record; the environment is built under the caller-supplied --scratch path | medium |
| tools/readiness/blast_radius.py | main | fs-write | 2 | explicit baseline or assert command; caller-supplied --json path; reads GitHub over REST only | medium |
| tools/readiness/blast_radius_drive.py | drive | fs-write | 6 | explicit drive command; refuses an existing workdir; caller-supplied --baseline-out path | medium |
| tools/readiness/blast_radius_drive.py | drive | gh-issue | 1 | explicit drive command; OWNER/sigma-drill- fullmatch; repository read back and must be private, not a fork or archived; refuses under CI; files one goal issue; never run by Sigma | high |
| tools/readiness/blast_radius_drive.py | drive | gh-pr | 1 | explicit drive command; same repository checks; one approve comment on the goal's own PR | high |
| tools/readiness/blast_radius_drive.py | drive | git-push | 1 | explicit drive command; same repository checks; origin guard; one setup push only when the repository is empty, never forced | high |
| tools/readiness/dead_vs_idle.py | broken_hook | fs-remove | 1 | only with --break-hook; unlinks the symlink it just made to the one hook file inside the empty scratch workdir, never the real hook | medium |
| tools/readiness/dead_vs_idle.py | broken_hook | fs-write | 2 | only with --break-hook; writes one mutated copy of the hook and symlinks into the empty scratch workdir it is given, never the real hook | medium |
| tools/readiness/dead_vs_idle.py | main | fs-write | 3 | explicit control command; writes only the JSON record at the path it is given | medium |
| tools/readiness/dead_vs_idle.py | run | fs-write | 3 | explicit control command; writes only inside the empty scratch workdir it is given (repo, home, daemon log); signals only the pid it spawned | medium |
| tools/readiness/drills.py | _kill_after_fixture_merge | fs-write | 2 | ungated | medium |
| tools/readiness/drills.py | _real_fixture | fs-write | 9 | ungated | medium |
| tools/readiness/drills.py | _scratch_sdlc | fs-write | 2 | ungated | medium |
| tools/readiness/drills.py | evidence | fs-write | 1 | ungated | medium |
| tools/readiness/drills.py | main | fs-write | 4 | ungated | medium |
| tools/readiness/drills.py | run_d1 | fs-write | 1 | ungated | medium |
| tools/readiness/drills.py | run_d2 | fs-rmtree | 1 | ungated | high |
| tools/readiness/drills.py | run_d2 | fs-write | 1 | ungated | medium |
| tools/readiness/drills.py | run_d3 | fs-write | 1 | ungated | medium |
| tools/readiness/drills.py | run_d4 | fs-write | 2 | ungated | medium |
| tools/readiness/egress_capture.py | main | fs-write | 1 | explicit summarize command; caller-supplied JSON path | medium |
| tools/readiness/exposure_scan.py | _write | fs-write | 3 | explicit exposure scan; caller-supplied evidence path | medium |
| tools/readiness/exposure_scan.py | main | fs-write | 1 | explicit tracked scan with --propose; caller-supplied draft path; hashes only | medium |
| tools/readiness/exposure_scan.py | scan_refs | git-destructive | 1 | explicit refs scan; local tag listing is read-only | low |
| tools/readiness/flake_census.py | run | fs-write | 1 | explicit run command; creates the caller-supplied output directory (pytest writes the JUnit XML into it); refuses to overwrite an observation | medium |
| tools/readiness/growth_audit.py | main | fs-write | 1 | ungated | medium |
| tools/readiness/injection_drill.py | _write_json | fs-write | 1 | explicit file subcommand; caller-supplied snapshot path that must not exist; validated sigma-drill- repository; declared --max-usd | medium |
| tools/readiness/injection_drill.py | file_payloads | gh-api-write | 1 | explicit file subcommand; OWNER/sigma-drill- name fullmatch; repository read back and must be private; declared --max-usd; never run by Sigma | high |
| tools/readiness/mutation_sample.py | _restore | fs-remove | 1 | explicit sample command; deletes only mutmut's cache and coverage data inside the clean frozen clone it was given, never a tracked file | medium |
| tools/readiness/mutation_sample.py | _write | fs-remove | 1 | explicit sample command; replaces only its own temporary file beside --out | medium |
| tools/readiness/mutation_sample.py | _write | fs-write | 1 | explicit sample command; caller-supplied --out evidence path | medium |
| tools/readiness/review_units.py | main | fs-write | 1 | explicit --json PATH; caller-supplied output path | medium |
| tools/readiness/scale_probe.py | main | fs-write | 3 | explicit probe command; refuses a non-empty workdir; writes only the JSON record at the path it is given | medium |
| tools/readiness/scale_probe.py | probe_claims | fs-write | 3 | explicit probe command; writes only synthetic lock files inside the empty scratch workdir it is given | medium |
| tools/readiness/scale_probe.py | probe_slots | fs-write | 2 | explicit probe command; writes only synthetic action-log files inside the empty scratch workdir it is given | medium |
| tools/readiness/seed_defects.py | apply | fs-write | 1 | explicit apply command; manifest.json beside the patches, outside the clone; detached clean clone only | medium |
| tools/readiness/shared_paths.py | _prepare | fs-write | 2 | explicit scan with samples; scratch repository inside a fresh temporary directory; fake HOME | medium |
| tools/readiness/shared_paths.py | main | fs-write | 3 | explicit scan command; caller-supplied --json, --table and --fixture-out paths | medium |
| tools/readiness/shared_paths.py | registry_probe | fs-write | 2 | explicit scan with samples; scratch directory inside the temporary work directory | medium |
| tools/readiness/shared_paths.py | sample_run | fs-rmtree | 1 | explicit scan with samples; removes only the temporary directory it created | high |
| tools/readiness/shared_paths.py | sample_run | fs-write | 1 | explicit scan with samples; fake HOME inside a fresh temporary directory | medium |
| tools/readiness/write_surface.py | main | fs-write | 1 | ungated | medium |
