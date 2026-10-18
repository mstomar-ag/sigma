# The dependency map miss test

`tools/dag_miss_test.py` checks that the stored dependency map, kept current by the cheap change
check, never decides differently from a fresh full read, and times a fresh process in both modes.

Run it only against a private clone. A JSON config file, never committed, names the one repository:

    {"repo": "<owner>/<name>"}

The tool exits 2 and runs nothing unless the clone's `origin` is exactly that repository, the
repository is not public (a repository named `sigma` or the origin of the checkout holding the tool
is refused) and `--confirm` is passed. It creates throwaway tickets there and closes exactly those at
the end, printing their numbers.

- R1: 40 or more rounds. Each applies one of six changes (label add, label remove, close, reopen,
  comment, a new ticket carrying a `Blocked by` marker) at P0 to P3, then compares the map's
  ordered decision with a fresh read. Any difference is a miss and exit 1.
- R2: per mode (`off`, `on`), three runs in fresh state directories; prints each time and the median.
- `--strategy wide` rewinds the cursor and re-lists the open P0 and P1 tickets; use it only after a
  miss.

The output holds numbers, change names and priorities, never ticket text. The offline tests prove
the harness against an in-memory fake. No live run has been made, so no timing or miss rate is
stated here; the owner-gated results are posted on the goal, not in this file.
