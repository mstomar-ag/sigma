# Resolver and reviewer readiness rows

The doctor adds these rows only when the upkeep block is on and `conflicts.resolve` is `agent`. In every other case
(block absent, off, invalid, or another resolve mode) it adds nothing and the output is unchanged. The settings are read
through the gate reader in `skills/sigma-loop/scripts/feature_upkeep.py`, never from the raw block.

Rows, built by `_resolver_rows` in `skills/sigma-doctor/scripts/doctor.py`:

- resolver cli: the host CLI is on the path. Unless the run is cheap-only, it also answers a version probe through
  the bounded runner with a 5 second limit. No model is run. UNVERIFIED: the version flag was not run against a real
  binary; PROVISIONAL: the 5 second limit.
- resolver flags: counts the design flags still listed as unverified in `skills/sigma-loop/scripts/feature_upkeep_launcher.py`.
  The row fails only for an unverified flag the launcher puts on the command line; `--bare` is unverified, refused by name
  and never requested, so it is reported and the row passes.
- resolver model: fails only if the launcher catalog holds nothing but the placeholder id; the shipped catalog holds real ids.
- resolver configured: the model and caps read from `conflicts.agent`, or the exact reason the level behaves as off (a model tier missing from the catalog, no `claude` on the path).
- resolver spend: always passes; it reports finite caps when both `conflicts.agent` spend keys are set, else that no spend limit is configured (each run bounded by the timeout only).
- resolver spend today: the rolling 24 hour total from the launcher spend store (bounded read) against the daily ceiling.
- last resolution: level and verdict of the newest resolution record; no row when there is none.
- units parked on conflict: the count of park markers; no row when there are none.
- reviewer store: the store directory in `skills/sigma-loop/scripts/feature_upkeep_review.py` is absent or a plain directory.
- resolution age: liveness by age of the records in `skills/sigma-loop/scripts/feature_upkeep_resolution.py`. Fails when the oldest
  record is older than `backup.keep_days` (PROVISIONAL), which means the prune is not running. No row when there are no
  records. UNVERIFIED: parked findings have no store to read yet, so their age is not reported.

Known gap, reported separately: the existing plugin-list calls in the doctor run with no time bound. These rows do not
extend that gap and do not change those calls.
