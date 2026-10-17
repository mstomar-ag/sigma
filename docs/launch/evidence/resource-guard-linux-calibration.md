# Resource guard calibration: linux

Status: NOT RUN. No measurement exists yet; the defaults stay flagged uncalibrated.

- Commit: 07baf5adbc32 (the code the run must use is the commit recorded when it is run)
- Date: not run
- Host: a Linux container with a memory limit (cgroup v2 memory.max); total memory for the run is the cgroup limit used as total

## Run (reproducible)

Run N concurrent `loop.py verify` calls on the same repository with `resource.mode` at `enforce`, then again at
`off`, each proving command a memory-heavy pytest run using the `{workers}` token. Use N large enough to exceed
the machine's memory without the guard. Stop the `off` arm yourself the moment the machine swaps heavily.

## Required fields (fill per arm)

| Field | enforce | off |
| --- | --- | --- |
| Concurrent verifies (N) | not run | not run |
| Peak footprint per verify (bytes) | not run | not run |
| Hold events (count, reasons) | not run | not run |
| Machine reached exhaustion (yes/no) | not run | not run |
