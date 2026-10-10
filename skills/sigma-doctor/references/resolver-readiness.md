# Resolver and reviewer readiness rows

The doctor adds these rows only when the upkeep block is on and `conflicts.resolve` is `agent`. In every other case
(block absent, off, invalid, or another resolve mode) it adds nothing and the output is unchanged. The settings are read
through the gate reader in `skills/sigma-loop/scripts/feature_upkeep.py`, never from the raw block.

Rows, built by `_resolver_rows` in `skills/sigma-doctor/scripts/doctor.py`:

- resolver cli: the host CLI is on the path. Unless the run is cheap-only, it also answers a version probe through
  the bounded runner with a 5 second limit. No model is run. UNVERIFIED: the version flag was not run against a real
  binary; PROVISIONAL: the 5 second limit.
- resolver flags: counts the design flags still listed as unverified in `skills/sigma-loop/scripts/feature_upkeep_launcher.py`.
  Any unverified flag keeps the row failing, because the launcher refuses those flags.
- resolver model: fails while the launcher catalog holds only the placeholder id.
- resolver spend: fails unless the upkeep settings carry finite caps. UNVERIFIED: the settings carry no cap key yet, so
  this row fails by design until a later slice adds one.
- reviewer store: the store directory in `skills/sigma-loop/scripts/feature_upkeep_review.py` is absent or a plain directory.
- resolution age: liveness by age of the records in `skills/sigma-loop/scripts/feature_upkeep_resolution.py`. Fails when the oldest
  record is older than `backup.keep_days` (PROVISIONAL), which means the prune is not running. No row when there are no
  records. UNVERIFIED: parked findings have no store to read yet, so their age is not reported.

Known gap, reported separately: the existing plugin-list calls in the doctor run with no time bound. These rows do not
extend that gap and do not change those calls.
