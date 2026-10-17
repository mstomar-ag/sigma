# Resource guard calibration: macos

Status: MEASURED, one admitted `enforce` arm and one `off` arm. The `off` arm was not driven to exhaustion (see below).
The five defaults stay flagged uncalibrated: one host and one workload do not fix a ratio.

- Commit: 186b79753432 (the code the run used)
- Date: 2026-10-17
- Host: a real macOS host, 14 CPUs, 24 GiB total memory (25769803776 bytes); total for the run is the host's own total.
  Before each arm: about 8.7 GiB available, swap 14.9 of 16.1 GB used and not moving, memory pressure level 1, load 1.2.

## Run (reproducible)

Save the driver below as `cal.py` and run it from a checkout, one arm per call (the arguments are the repository,
the mode, the number of concurrent verifies, the MiB each touches, and the seconds each holds it):

```
python3 cal.py . enforce 6 2048 12
python3 cal.py . off 3 2048 12
```

Each verify is a separate process calling `resource_verify.run("verify", ...)` with a proving command that touches the
given memory and sleeps. A held verify retries after its `retry_after`. A watcher samples `resource_guard.sample`
every half second for the minimum available memory and the maximum swap used.

## Measured

| Field | enforce | off |
| --- | --- | --- |
| Concurrent verifies (N) | 6 attempted, all 6 admitted in turn (never more than one or two at once), 2 GiB each | 3, all admitted at once, 2 GiB each |
| Peak footprint per verify (bytes) | 2164181392 to 2164951488 (about 2.0 GiB for a 2 GiB touch) | 2164427152 to 2169031056 |
| Hold events (count, reasons) | 30, "memory: headroom below one verify unit" and "no observation of verify yet and one unit is already running" | 0 |
| Machine reached exhaustion (yes/no) | no: minimum available 6.7 GiB, swap used did not grow | no: minimum available 6.8 GiB, swap used did not grow (see below) |

The `off` arm used 3 x 2 GiB = 6 GiB against about 8.7 GiB available, on purpose: swap was already 93 percent used, so
a demand above the available memory would have made the host swap heavily. So this record shows what the guard does
(it serialises the verifies, learns the 2.0 GiB unit after the first one, and never let available memory fall below
6.7 GiB) and not what the machine does without it. The Linux record has the exhaustion arm, in a container where
exhaustion is safe.

## What this supports

The gate admits when there is room, holds when there is not, and measures the right footprint per verify (the
learned unit matched the 2 GiB touch within 3 percent). It does not support any default ratio, so the doctor text
keeps "uncalibrated". Reproducing the exhaustion arm on a host that can afford to swap is a decision for its owner.

## Driver

```
"""Calibration driver: N concurrent guarded verifies (N separate processes), each touching MB MiB for HOLD seconds.
usage: cal.py REPO MODE N MB HOLD"""
import json, os, subprocess, sys, tempfile, threading, time
repo, mode, N, MB, HOLD = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
sys.path.insert(0, os.path.join(repo, "skills", "sigma-loop", "scripts"))
import resource_verify as rv, resource_guard as g
CMD = ("python3 -c \"import time;b=bytearray(%d*1048576);[b.__setitem__(i,1) for i in range(0,len(b),4096)];"
       "time.sleep(%d)\" # {workers}" % (MB, HOLD))
if len(sys.argv) > 6:   # child: one verify, retried while held
    i = int(sys.argv[6]); holds, reasons, t0 = 0, set(), time.time()
    while time.time() - t0 < 240:
        o = rv.run("verify", CMD, repo, 120, "cal%d" % i, config={"mode": mode}, sample_gap=0.5)
        if o.hold:
            holds += 1; reasons.add(o.hold.reason.split(":")[0] + ": " + o.hold.reason.split(" ")[1]); time.sleep(min(o.hold.retry_after, 5)); continue
        print(json.dumps({"i": i, "peak": o.peak, "rc": o.returncode, "workers": o.workers, "holds": holds, "reasons": sorted(reasons)})); sys.exit(0)
    print(json.dumps({"i": i, "never_admitted": True, "holds": holds, "reasons": sorted(reasons)})); sys.exit(0)
os.environ["SIGMA_RESOURCE_DIR"] = tempfile.mkdtemp(prefix="cal-")
before = g.sample().as_dict(); mx = {"swap_used": 0, "min_avail": before["available"], "pressure": 0}
stop = threading.Event()
def watch():
    while not stop.wait(0.5):
        s = g.sample(); mx["min_avail"] = min(mx["min_avail"], s.available); mx["swap_used"] = max(mx["swap_used"], s.swap_used); mx["pressure"] = s.pressure if isinstance(s.pressure, dict) else max(mx["pressure"], s.pressure or 0)
threading.Thread(target=watch, daemon=True).start()
t0 = time.time()
ps = [subprocess.Popen([sys.executable, __file__, repo, mode, str(N), str(MB), str(HOLD), str(i)], stdout=subprocess.PIPE, text=True) for i in range(N)]
res = [json.loads(p.communicate()[0]) for p in ps]; stop.set()
print(json.dumps({"mode": mode, "N": N, "MB": MB, "wall_s": round(time.time() - t0, 1), "before": before, "after": g.sample().as_dict(), "watch": mx, "results": sorted(res, key=lambda r: r["i"])}, indent=1))
```
