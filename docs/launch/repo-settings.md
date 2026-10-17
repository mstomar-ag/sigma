# Public repository settings

The exact commands the **owner** runs against the NEW public repository, and the read-only command that
proves they took. Nothing in Sigma runs these commands: changing a repository's settings is the owner's
action, and `tools/readiness/verify_repo_settings.py` sends GET requests only. The launch it serves is
defined in [`definition.md`](definition.md).

## Status

Item 4 of the goal that wrote this page (#398) is **outstanding and owner-gated**: no one has yet applied
these commands to a rehearsal repository and committed the verifier's all-PASS output. Until that happens,
the request bodies below come from GitHub's REST reference (see "Documentation checked") and have not been
applied to any repository. The verifier's tests run on recorded response shapes, not on a live
repository: `tests/fixtures/repo_settings/*.json` are reconstructions of the documented shapes, not captures
of a real repository, and must not be cited as evidence that the settings work.

## Why: a pull request merged while CI was still running

This is what an unprotected default branch does, measured on this project:

- On 2026-10-01, 2 of the last 12 merges landed before their CI finished (CI takes about 19 minutes), because
  the default branch had no branch protection.
- On 2026-10-03 an automated goal slot (its merge time and the completion times of its five checks were read from the pull request afterwards) merged a pull request while all five CI legs were still pending. The
  loop's merge command (`work.py merge`) does not wait for CI by itself: it asks GitHub whether the pull
  request is mergeable, and GitHub answers `CLEAN` when no check is *required*, however many are still
  running. The repository had no required checks, so a pending pull request looked mergeable.

Required checks turn that `CLEAN` into `BLOCKED` until the checks pass. That is the fix, and the only place it
can live is the repository's own settings.

## What each setting changes in the loop

| Setting | What the loop does differently |
|---|---|
| Required checks (five legs) | `mergeStateStatus` is `BLOCKED` while a leg is pending, so `work.py merge` re-reads the pull request for about 7.5 minutes, then arms GitHub's `--auto` merge if `allow_auto_merge` is on, and otherwise parks (see the `allow_auto_merge` row). It no longer lands a pending pull request. A leg that fails goes to the bounded CI-repair path instead of merging. |
| `enforce_admins` on | An administrator's merge obeys the required checks too. With it off, GitHub lets an administrator merge past a required check that has not passed, and the loop usually runs as the owner's own token, so the protection might not bind it. This page has not measured that bypass; `enforce_admins` is set so it cannot arise. |
| No required reviews | The loop reviews and clears its own pull requests with a `sigma:approve` comment; it cannot supply a human approval, so a required review would park every goal. |
| `allow_auto_merge` on | Required for the arm step above. Off, the loop tries a direct merge after the wait, GitHub refuses it while a check is pending, and the goal parks instead of landing when CI turns green. These rows describe the loop with `work.auto_merge` set to `protected` or `always`; under the shipped default `off` the loop leaves the pull request for a human after its review gate. This setting is not part of the original list of launch protections; it exists because of how the loop waits. |
| No force push, no deletion | The default branch cannot be rewritten or removed. The loop only force-pushes its own `sdlc/*` branches. |
| Secret scanning and push protection | GitHub blocks a push that contains a recognised secret. Nothing in the loop changes. |
| Private vulnerability reporting | The channel `SECURITY.md` names for reports. Nothing in the loop changes. |
| Default workflow token `read`, cannot approve pull requests | Workflows start with a read-only token and cannot approve a pull request. `ci.yml` declares no permissions of its own and needs only read access. |
| Maintainers team with `maintain` (an `admin` grant also passes the check) | A team with the rights to triage and merge without admin. `.github/CODEOWNERS` names two people and one address, not the team; the team is created in GitHub and membership is never written to a file. |

`strict` ("require branches to be up to date") is left off: with a 19-minute CI it would make every merge race
the next one. `work.py merge` already rebases a pull request that GitHub reports `BEHIND`.

## Before you start

- You run these as an **administrator** of the new repository, with the GitHub CLI authenticated
  (`gh auth status`). Reading `security_and_analysis` and the private-vulnerability-reporting status needs
  admin rights; without them the verifier says so and FAILs, it does not guess.
- `OWNER/NAME` below is the new public repository, `Agrim-Intelligence/sigmaloop` (the name `definition.md` and
  `definition.json` give; `OWNER` is `Agrim-Intelligence`). Replace it in every command; the placeholder is kept so the
  commands stay checked against the verifier's own output. The default branch is `main`; if it is not, replace `main` too. The verifier reads only branch names of letters, digits, `_`, `.` and `-`; a default branch with a `/` in its name is refused with exit `2`.
- The team commands need `OWNER` to be an **organisation**. A repository owned by a person has no teams, and the
  verifier's team check can never pass for it. Create the maintainers team under the new repository's own
  organisation (pass `--team <slug>` to the verifier if it is not named `sigma-maintainers`).
- Secret scanning and push protection are offered on public repositories. On a private rehearsal repository
  they may need a paid GitHub security product, in which case the PATCH is refused; this page has not
  measured which plans allow it.

## The required check names

CI requires ONE check: job `test` of `.github/workflows/ci.yml` (no job `name:`, no matrix, so GitHub names it
`test`). It runs a subset of the suite on macOS with Python 3.12 for pull requests and merges to main. The five
launch-supported cells of `definition.md` run the full suite in job `full`, weekly and on demand; that job is not
required, so a break only it can see is found up to a day after the merge. The Windows workflow is an experimental cell and is not required. `tests/test_repo_settings_doc.py` derives the names from the workflow
and fails if this list and the workflow disagree.

<!-- required-checks:begin -->
- `test`
<!-- required-checks:end -->

A check name only exists on GitHub after it has run once, and GitHub accepts a required name it has never seen.
Protect the branch after the first CI run on the new repository, or a typo in a name makes every pull request
wait forever.

## The commands

Run each block, then run the verifier (below). Blocks 1 to 6 set values, so a second run should change nothing further (not measured); block 7's first line fails if the team already exists.

### 1. Branch protection on the default branch

Requires the five checks, applies to administrators, requires no reviews, forbids force pushes and deletion.
The request body is the REST "Update branch protection" body: `required_status_checks` (with `strict` and
`contexts`), `enforce_admins`, `required_pull_request_reviews` and `restrictions` are all required keys, and
`null` for the last two means "none".

```sh
echo '{"required_status_checks":{"strict":false,"contexts":["test"]},"enforce_admins":true,"required_pull_request_reviews":null,"restrictions":null,"allow_force_pushes":false,"allow_deletions":false}' | gh api -X PUT repos/OWNER/NAME/branches/main/protection --input -
```

### 2. Auto-merge allowed

```sh
gh api -X PATCH repos/OWNER/NAME -F allow_auto_merge=true
```

### 3. Secret scanning

```sh
echo '{"security_and_analysis":{"secret_scanning":{"status":"enabled"}}}' | gh api -X PATCH repos/OWNER/NAME --input -
```

### 4. Push protection

```sh
echo '{"security_and_analysis":{"secret_scanning_push_protection":{"status":"enabled"}}}' | gh api -X PATCH repos/OWNER/NAME --input -
```

### 5. Private vulnerability reporting

```sh
gh api -X PUT repos/OWNER/NAME/private-vulnerability-reporting
```

### 6. Default workflow token read-only

```sh
gh api -X PUT repos/OWNER/NAME/actions/permissions/workflow -f default_workflow_permissions=read -F can_approve_pull_request_reviews=false
```

### 7. The maintainers team and its rights

Create the team (skip this line if it exists), then give it `maintain` on the repository. Add members in
GitHub's own interface; do not record membership in a file.

```sh
gh api -X POST orgs/OWNER/teams -f name=sigma-maintainers -f privacy=closed
gh api -X PUT orgs/OWNER/teams/sigma-maintainers/repos/OWNER/NAME -f permission=maintain
```

## Verify, read-only

```sh
python3 tools/readiness/verify_repo_settings.py OWNER/NAME
```

It prints `PASS` or `FAIL` for each setting, and under every `FAIL` the command from this page that fixes it.
Exit `0` means every setting passes, `1` means at least one fails on an answer it could judge, `2` means it could not judge (bad arguments, a
refused request, a failed `gh` call, an unreadable fixture, an unusable answer, or a repository it cannot read). `--help` exits `0`.

It sends only `gh api --include --method GET` requests, to five endpoints: the repository, the default branch's
protection, private vulnerability reporting, the workflow permissions and the repository's teams. Any other
method or path is refused before anything is sent, and a test plants a POST to prove it. A 404 or 403 means
"not set, or the token lacks admin rights" and is reported as a FAIL that says so; any other answer
(a 401, a 429, a server error, a body of the wrong shape) is not judged and exits `2`. It reads the first 30
teams of the repository, so a team beyond that reads as missing.

To see its output without GitHub, run it on a recorded response set. This makes no GitHub call:

```sh
python3 tools/readiness/verify_repo_settings.py OWNER/NAME --fixture tests/fixtures/repo_settings/unprotected.json
python3 tools/readiness/verify_repo_settings.py OWNER/NAME --fixture tests/fixtures/repo_settings/protected.json
```

The first exits `1` with a `FAIL` and a fix command for each broken setting; the second exits `0`.

## Documentation checked

Verified against GitHub's REST reference (API version 2022-11-28) on **2026-10-03**:

- Branch protection (get and update): <https://docs.github.com/en/rest/branches/branch-protection?apiVersion=2022-11-28>
- Repositories (get, and update with `security_and_analysis` and `allow_auto_merge`): <https://docs.github.com/en/rest/repos/repos?apiVersion=2022-11-28>
- Private vulnerability reporting (check, enable): <https://docs.github.com/en/rest/repos/repos?apiVersion=2022-11-28#check-if-private-vulnerability-reporting-is-enabled-for-a-repository>
- Actions workflow permissions for a repository: <https://docs.github.com/en/rest/actions/permissions?apiVersion=2022-11-28>
- Teams (create a team, add or update a team's repository permissions): <https://docs.github.com/en/rest/teams/teams?apiVersion=2022-11-28>
- Listing a repository's teams (`GET repos/{owner}/{repo}/teams`) is in the repositories reference above.

The read paths were also checked against live GET responses of an existing repository on the same date.

## What this does not cover

- **Rulesets.** The verifier reads classic branch protection. A repository protected by a ruleset instead
  reads as `FAIL`.
- **A rehearsal run.** See "Status": the commands are unapplied until the owner runs them on a rehearsal
  repository.
- **A required check that the PUT accepts but never matches.** See the note under the check names.
- **`contexts` versus `checks`.** The command in block 1 sends the legacy `contexts` list, which the REST reference lists as a required key of the request body next to the newer `checks` (name plus `app_id`). It is unmeasured whether GitHub accepts `checks` alone, or both together, and the reference marks `contexts` as being phased out; the rehearsal run decides it. The verifier reads either field.
