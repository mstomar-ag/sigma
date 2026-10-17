# Contributing to Sigma Loop

Thank you for your interest in Sigma Loop. Outside contributions are not accepted yet; issues are welcome.
The rest of this page is for maintainers and for when that changes. Please open an issue before substantial
work so maintainers can confirm the scope. When outside contributions open, a DCO sign-off (a `Signed-off-by` line on each commit) will be required.

## Development setup

Sigma Loop needs Python 3.10 or newer. CI runs the full suite weekly and on demand on Linux with Python 3.10, 3.11, 3.12 and 3.13, and on
macOS with Python 3.12 only (nightly: the suite minus its heaviest files; pull requests and merges to main: a short smoke set, one macOS Python 3.12 leg); macOS on other Python versions is untested. Install the test dependencies:

```sh
python -m pip install pytest pytest-xdist pyyaml
```

Run the suite before opening a pull request:

```sh
python -m pytest tests -q -n 4 -p no:cacheprovider
python3 tools/rename_check.py
```

The second command exits 1 and prints each file and line while any tracked path or file still carries
the retired skill prefix (every skill and command starts with `sigma-`); CI runs it on every pull
request. A branch cut before the rename lands fixes itself by taking the new paths and running the
rename tool once more, from the repository root:

```sh
python3 tools/rename_prefix.py
```

It refuses (exit 2, nothing moved) if the new folder already exists; `git mv` the files your branch
added under the old folder name into it by hand, then run it again.

## Working rules

Shipped code is stdlib-only. Every guard must be deliberately seen red once before its green run.
The five design properties — reliability, scalability, resiliency, safety, and liveness — and the
full agent rules are defined in [AGENTS.md](AGENTS.md). Agents must emit status according to the
[output contract](docs/output-contract.md).

Work lands through `sdlc/*` branches and pull requests. Nobody commits directly to a `feature/*`
branch. Keep changes focused, add meaningful tests, and update the relevant documentation.
