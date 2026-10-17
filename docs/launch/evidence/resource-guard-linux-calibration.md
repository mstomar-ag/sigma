# Resource guard calibration: linux

Status: MEASURED, both arms, in a memory-limited container. The defaults stay flagged uncalibrated: one container and
one workload do not fix a ratio.

- Commit: 186b79753432 plus the #1772 change to `skills/sigma-loop/scripts/resource_guard.py` and
  `skills/sigma-loop/scripts/resource_verify.py` (the code the run used)
- Date: 2026-10-17
- Host: a Linux container (an official minimal Python 3.9 image) with a 2 GiB memory limit and no extra swap
  (cgroup v2 `memory.max` 2147483648); total memory for the run is the cgroup limit used as total (the host had
  16 GiB). The container was started and removed by the run.

## Run (reproducible)

Save the driver from `docs/launch/evidence/resource-guard-macos-calibration.md` as `cal.py` next to the checkout, then:

```
docker run --rm --memory=2g --memory-swap=2g -v CHECKOUT:/repo:ro -v DRIVER_DIR:/cal:ro python:3.9-slim \
  python3 /cal/cal.py /repo enforce 4 700 12
docker run --rm --memory=2g --memory-swap=2g -v CHECKOUT:/repo:ro -v DRIVER_DIR:/cal:ro python:3.9-slim \
  python3 /cal/cal.py /repo off 4 700 12
```

(Replace CHECKOUT with the absolute path of the checkout and DRIVER_DIR with the absolute path of the folder holding `cal.py`.) Four verifies of 700 MiB each want 2.8 GiB in a 2 GiB container.

## Measured

| Field | enforce | off |
| --- | --- | --- |
| Concurrent verifies (N) | 4, all admitted in turn | 4, all admitted at once |
| Peak footprint per verify (bytes) | 744186880 to 745034752 | 744078336 and 744120320 (the two that finished; the other two were killed before a sample) |
| Hold events (count, reasons) | 24, "memory: headroom below one verify unit" and "no observation of verify yet and one unit is already running" | 0 |
| Machine reached exhaustion (yes/no) | no: all 4 exited 0, minimum available 1.25 GiB of the 2 GiB limit | yes: 2 of 4 were killed by the kernel (exit 137) |

## Two gaps this run found, both fixed in the same change

1. The sample took the host's memory as the total even inside a container, so the 20 percent reserve was 3.3 GiB of a
   2 GiB container and every verify was held forever (first attempt: 60 holds each, nothing ran). The cgroup limit is now
   the total when it is smaller.
2. A minimal image has no `ps`, so the footprint meter silently read 0 and the learned cost stayed unmeasured. It now
   reads the process table from `/proc` when `ps` is missing.

## What this supports

With the cgroup limit as total, the guard turned a run that the kernel kills (2 of 4) into one that completes (4 of 4).
It does not support any default ratio, so the doctor text keeps "uncalibrated".
