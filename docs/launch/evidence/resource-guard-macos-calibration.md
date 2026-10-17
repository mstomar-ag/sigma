# Resource guard calibration: macos

Status: PARTIAL. One guarded verify was attempted on a real macOS host. The guard held it and nothing ran, so no
peak footprint was measured. The `off` arm was not run. The defaults stay flagged uncalibrated.

- Commit: 23910d6e0f48 (the code the run used)
- Date: 2026-10-17
- Host: a real macOS host, 14 CPUs, 24 GiB total memory (25769803776 bytes); total for the run is the host's own total

## Run (reproducible)

Call `resource_verify.run("verify", command, root, 600, goal, sample_gap=0.5)` from
`skills/sigma-loop/scripts/resource_verify.py` with `resource.mode` at `enforce` and the command a pytest run using
the `{workers}` token over `tests/test_resource_guard_limits.py` and `tests/test_resource_verify_1601.py`. Read-only
memory sampling (`resource_guard.sample`) was taken before and after. Then repeat with `resource.mode` at `off` and
raise N; stop the `off` arm yourself the moment the machine swaps heavily.

## Measured (enforce, one verify, attempted three times over about two minutes)

The host was already busy with other work: about 4.4 GiB available, swap 21.4 of 22.5 GB used, memory pressure level
2, one-minute load 8 to 10. Every attempt returned a hold with reason "memory: no room above the reserve" and
retry_after 4.0 seconds. The proving command was not started, so it was not measured.

## Required fields (fill per arm)

| Field | enforce | off |
| --- | --- | --- |
| Concurrent verifies (N) | 1 attempted, 0 admitted | not run |
| Peak footprint per verify (bytes) | not measured (held before any process started) | not run |
| Hold events (count, reasons) | 3, "memory: no room above the reserve" | not run |
| Machine reached exhaustion (yes/no) | no (nothing was started by the run) | not run |

## What this supports

The memory gate refused correctly on a host that was close to exhaustion before the run began. It does not support
any default ratio: no admitted run exists, so the five defaults are unchanged and the doctor text keeps
"uncalibrated". A calibrated run needs a quieter host and the `off` arm; a follow-up is filed.
