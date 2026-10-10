# hello-sdlc — a worked Sigma walkthrough

A tiny project (`greeter.py` + `test_greeter.py`) with the SDLC kit already initialized
(`.sdlc/`), and one real goal queued: **add a `!` to the greeting**
([`.sdlc/goals/0001-add-exclaim.md`](.sdlc/goals/0001-add-exclaim.md)).

> You install nothing extra to run this. If the optional `superpowers` + `code-review` companions
> are already installed, Sigma uses them for the phases they cover; if not, Sigma's portable
> `sigma-*` executors run the same phases. Plan-review is always Sigma's own `sigma-plan-review`.

## Run it (interactive mode)
```
/sigma-goal .sdlc/goals/0001-add-exclaim.md
```
You'll be walked through the goal one gate at a time (you approve each). At the end, `greet()`
returns `"Hello, x!"`, `test_greeter.py` passes, and the goal is recorded `done`.

## Run it (autonomous mode)
```
/sigma-loop
```
Pulls the backlog and runs each pending goal unattended, parking anything that needs you to
`.sdlc/state/review-queue.md`. Then:
```
/sigma-status
```
```
backlog: 0 pending, 0 in-progress, 1 done, 0 parked, 0 failed | iteration 1 | review-queue: empty
```
(Recorded from `status.py` on a fresh copy of this directory after its one goal was recorded `done`.
Before the run the same line reads `0 proposed, 1 pending, ... 0 done ... | iteration 0`.)

## Note on state in git
This example **commits** `.sdlc/state/` so it's self-contained and runnable as a reference. In your
own repo there is nothing to do: `/sigma-init` git-ignores Sigma's runtime directories itself
(`.sdlc/state/` among them) -- that state is machine-written loop progress, not source.
