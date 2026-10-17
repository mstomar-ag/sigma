# Quality metrics: measure a goal against its merge-base

`quality_delta.py` answers one question about a goal: for each metric you configured, is the head better,
the same or worse than the merge-base, and by how much. It is opt-in (nothing runs until `quality.metrics`
exists), it runs only commands you wrote, and when it cannot measure it says `unmeasured` rather than
guessing. This is PRD-0008 R-01.

## The gesture

```
python3 <installed-sigma>/skills/sigma-loop/scripts/quality_delta.py <goal>
```

Run it from inside the goal's worktree, after the work is committed. `<goal>` is the same id or `.md` file
name the work record uses (`1635` and `1635.md` are one goal). `-h` and `--help` print usage and touch nothing.

Output, one line per metric after a header that names both commits (so a later waiver can be bound to the
exact head):

```
base=<merge-base sha> head=<head sha>
<name>: base=<number> head=<number> delta=<head minus base> better|same|worse
<name>: unmeasured (<one-line reason>)
```

When the whole run cannot be measured (no work record, no merge-base, a `.sdlc/config.json` at the
merge-base that cannot be read, not in a git working tree, run from a worktree that is not the goal's) the
output is the header with a dash for what is unknown, then one line for the whole run, and no metric lines:

```
base=- head=-
unmeasured: <one-line reason>
```

`quality_delta.py <goal>` must be run from that goal's own worktree, on its own branch: the work record's
`worktree` and `branch` are compared with where it runs, and a mismatch is `unmeasured`, not a measurement of
whatever head happens to be checked out.

`same` is exact equality. There is no tolerance here. Exit 0 for every measured or unmeasured outcome; exit 2
for a refusal (malformed `quality` block, unsafe goal name, a host that cannot stop a process group), with
nothing on stdout and the reason on stderr, e.g. `quality_delta.py: REFUSED [bad-metric] metrics[1]: ...`.
When the merge-base config has no metrics the only line is `no metrics configured`.

## Configuration

The block lives in `.sdlc/config.json`. It is read **as committed at the merge-base** (`git ls-tree` and
`git cat-file` on the fork point), never from the head's file, so a goal cannot add a command to itself or
loosen the bar it is judged by. Commit `.sdlc/config.json` for it to apply. In Sigma's own repository that
file is gitignored, so self-hosting measures nothing and prints `no metrics configured`.

```json
{
  "quality": {
    "metrics": [
      {"name": "lint-findings", "command": ["python3", "tools/count_lint.py"], "direction": "lower", "scope": "repo"},
      {"name": "changed-lines", "command": ["python3", "tools/count_lines.py"], "direction": "lower",
       "scope": "changed-files", "timeout_seconds": 120}
    ],
    "memory_floor_fraction": 0.15,
    "env_drop": ["MY_SERVICE_LOGIN"]
  }
}
```

| Field | Meaning |
|---|---|
| `name` | letters, digits, `.`, `_`, `-`; at most 64 characters; unique |
| `command` | a non-empty list of strings, run as an argument list with **no shell** |
| `direction` | `lower` or `higher` is better |
| `scope` | `repo` (the whole checkout) or `changed-files` (see below) |
| `required` | optional boolean, validated and carried for the later gate (R-02); `quality_delta.py` does not act on it |
| `timeout_seconds` | optional, above 0 and at most 3600, default 300, per command |
| `memory_floor_fraction` | optional, above 0 and at most 0.9, default 0.15 |
| `env_drop` | optional list of environment variable names to remove from every command's environment |

An unknown key, a wrong type or a bad value is a refusal naming the entry index. A `_`-prefixed key in the
`quality` block (a comment) is ignored.

## What a command must do

Print a number as the **last non-empty line of stdout**. Exit 0. Nothing else is read (stderr is never parsed).
The line must match `[+-]?digits[.digits][e[+-]digits]` with at most three exponent digits, ASCII only: no
underscores, no `nan`, `inf`, no other script's digits, no `1e999`. The command runs with the checkout as its
working directory, once at the merge-base and once at the head, each in a scratch worktree of its own.

`changed-files` scope: the paths changed between the merge-base and the head that exist at **both** commits are
written NUL-separated to `.sigma-quality-files` at the root of each scratch worktree, and the environment
variable `SIGMA_QUALITY_FILES` holds that absolute path (never argv, so names with spaces or newlines are safe).
No changed files at both commits is `unmeasured`. For `repo` scope the variable is unset.

## When it says `unmeasured`

A command that exits non-zero, is not found, times out, prints more than 1 MiB of stdout (the last line would be
lost), or prints no valid number, at **either** commit. Uncommitted work in the goal worktree (modified or staged
tracked files, untracked source files outside `.sdlc`): the head scratch is the committed HEAD, so measuring it
would print a false `same`; commit first. Untracked plan, research and acceptance copies under `.sdlc` do not
count, and neither does a modified **tracked** file under `.sdlc` (the exclude covers every path there). Also: no work record or an unresolved base, no merge-base, a `.sdlc/config.json` at the merge-base that is
not a regular file or not readable JSON, a probe that cannot read memory, memory below the floor, a scratch
worktree held by a live run (it abstains rather than fight over it).

## Environment

Each command starts from `bounded_run.unattended_env()` (no repository-location variables, no prompts) with
credential-shaped names removed, case-insensitively: `SSH_AUTH_SOCK`; any name ending `_TOKEN`, `_SECRET`,
`_PASSWORD`, `_PASSWD`, `_PASS`, `_PAT`, `_KEY` or `_CREDENTIALS`; any name containing `AUTHTOKEN`; any name
starting `AWS_`, `ANTHROPIC_` or `OPENAI_`; and the exact names `PGPASSWORD`, `MYSQL_PWD`, `DATABASE_URL` and
`KUBECONFIG`; plus everything in `quality.env_drop`. That includes `GH_TOKEN` and
`GITHUB_TOKEN`.

Two limits, stated plainly. It is a **denylist**: a credential with an unusual name survives, and
`quality.env_drop` is the lever. And `HOME` is kept, so `~/.config/gh`, `~/.netrc` and `~/.ssh` remain readable
by a metric command. A metric command is code you chose to run; treat it as you would a verify command.

## Limits

- **Memory.** Before anything runs, and again before each command, the host's available memory is read (Linux:
  `/proc/meminfo` MemAvailable; macOS: `vm_stat` free + inactive + speculative pages, run with a 10 s timeout).
  These are host figures, **not** a container's cgroup limit. Below `memory_floor_fraction` of total memory (15
  percent by default, a judgement, not a measurement), or unreadable: no command runs and every metric is
  `unmeasured`. A dip mid-run marks that metric and every later one `unmeasured` and keeps the ones already
  measured. The check and the spawn are not atomic (a named ceiling).
- **Git ceiling.** Reads (merge-base, config, status, diff) have a fixed 30 s limit each; creating a scratch
  worktree has its own 600 s limit. A tree whose checkout needs longer is `unmeasured` with git's timeout as the
  reason, every run; these two numbers are constants, not derived from `timeout_seconds`.
- **Time.** One command at a time per goal, twice per metric. Wall time is bounded by `2 x sum(timeout + 2 s grace)`
  plus the git work. Two goals run two commands at once, guarded only by the per-command probe.
- **Output.** At most 1 MiB of stdout per command is kept; more is `unmeasured`.
- **Process group.** On timeout the command's whole process group is stopped (SIGTERM, then SIGKILL after 2 s).
  A grandchild that calls `setsid` leaves the group and **escapes** the kill; this is covered by a test and not
  fixed. If `quality_delta.py` itself is killed, a lifeline process stops the command's group.
- **Platforms.** POSIX only. A host that cannot run a command in a process group of its own refuses with
  `not supported here` (exit 2).
- **Scale.** Scratch checkouts cost O(tree size) per side; finding the merge-base is a history walk. Neither
  grows with commit count. Ten times the metrics is ten times the wall time at the same memory.

## Where it writes, and how it recovers

It writes only under the **main checkout's** `.sdlc/state/quality_delta/` (found through git's common dir, so the
same place from any worktree), one pair `<goal>-base` and `<goal>-head` plus an owner marker each, and removes
them when it finishes, on every path including an error. The directory is gitignored state, not a tracked or
root-checkout file, so it never makes `git pull`, `git checkout` or `work.py start` refuse. It also records one
timing line the way every Sigma script does.

A crash, a SIGKILL or a reboot leaves the scratch behind with its owner marker. **Recovery needs no human**: the
next run reads the marker and, if that process is dead, removes the leftover before measuring. A live owner makes
the run abstain. The manual lever, if you want it: delete `.sdlc/state/quality_delta/<goal>-*` and run again.
Running it twice is safe.

It never imports `work.py`, never calls `gh`, never uses the network, and sends no signal except through the
shared runner.
