"""#2660 — the mandatory control: "where core's goals live after publication" (board, labels,
`.sdlc/config.json`, and the routing rule) proven end to end against a scratch repository that
carries none of this project's own vocabulary.

WHAT THIS DRIVES. A real `/sigma-init`, the public-repository adoption block from
`skills/sigma-setup/references/public-repo.md` (Task 2) run EXACTLY as the doc prints it, and then
the documented `sigma-loop` cycle (`loop.py start` -> `next` -> `agent-start` -> `work.py start` ->
`phase_report.py` -> `work.py commit/pr/merge` -> `loop.py record done` (REFUSED while the PR is
open, #232) -> `loop.py record review` -> the human's `pr merge` -> `loop.py reconcile-merges`,
which records `done`, closes the issue and releases the checkout) against a stateful, PATH-installed fake `gh` and a real bare git remote. Every gh
CLI call the shipped scripts make is modelled by the fake; a call the fake does not recognise exits
1, is written to `FAKE_GH_UNHANDLED`, and every test that runs the fixture asserts that log is
empty (M9 below is exactly why: a call the product's own fail-open code swallows can leave the
cycle green while quietly not testing what it looks like it tests).

THE FAKE'S OWN JQ. `--jq` is implemented by the fake itself, in Python, against its own JSON — never
by shelling out to `/usr/bin/jq` (see `_JQ_EXPRS`). It supports only the small closed set the
shipped CLIs actually pass. A missing key or an explicit `null` renders as an EMPTY LINE, matching
real `gh`'s template rendering (confirmed against `work.py:2041-2042`'s own `if not number:` -- that
check depends on an empty string, not the four characters `null`, to detect "no existing PR yet").

MEASURED FACTS (recorded once at implementation time, this repo's own Mac, in the fashion §4 of the
plan already established):

  M-impl-1 (test 1, red before Task 2): before `skills/sigma-setup/references/public-repo.md`
    existed, this test's own `read_text()` raised `FileNotFoundError`. Green once the doc was
    written.
  M-impl-2 (test 3, the self-heal mutation -- §4 M3/M16's own corrected order): with `_ensure_labels`
    replaced by `return` for the REFILL call only, and the second-clone probe run AFTER the refill
    (never before it), the three assertions the plan names went red: `sdlc:in-progress` did not
    exist in the fake's label set again, issue 2 did not carry it, and `CLAIM-LABEL-WRITE-FAILED #2`
    appeared on stderr. The other three assertions ("issue 1 lost the label at deletion", "the
    refill printed `2`", "goal 1 still reached `MERGED`") stayed green, exactly as measured in the
    plan's own §4 M16. Restoring `_ensure_labels` returned all six to green.
  M-impl-3 (test 6, the loud-not-silent refusal): with the fake's `refuse_label_create` UNSET, the
    `CLAIM-LABEL-WRITE-FAILED #1` marker assertion went red (the claim landed silently instead).
    Restoring the refusal returned it to green.
  M-impl-4 (test 7, the no-labels refusal): with the `labels` line kept in the doc block, the human's
    filing succeeded and this test's own refusal assertion went red (no refusal to observe). Removing
    it (the test's own gesture) is what makes it green.
  M-impl-5 (test 9, the `_apply_default` mutant, §4 M14's own corrected control): with the
    `if d.get(key) is None:` guard in `setup.py::_apply_default` removed (so it writes
    unconditionally), a SECOND `configure` call after the profile turned `ledger.enabled` back to
    `true`. Restoring the guard returned it to `false`.
  M-impl-6 (test 10, the fake's own unknown-label check): with that check removed from the fake's
    `issue create`/`issue edit --add-label` handling, this test's own duplicate-label/unknown-label
    assertions went red, AND (as the plan's own §4 M13 predicts) test 7 went red too, because the
    filing it depends on no longer refuses. Both restored to green together.

RUNTIME, MEASURED (three consecutive runs, this Mac): 60.94s, 60.29s, 60.57s wall-clock for this
whole module -- a few tenths to about a second OVER the plan's own <60s target, not under it. The
primary-sequence fixture (with the second-clone probe) alone measured 28.33s, close to the plan's
own §4 M15 number (25.6s); the gap is almost entirely `_BACKLOG_READ_RETRIES`' own empty-read
backoff (F4, already a named, accepted follow-up this goal does not fix) across the module's six
`next`/`next --skip` calls, plus ordinary machine variance. Reported as measured rather than forced
to match, per RELIABILITY -- the difference is real and worth a human decision (accept the margin,
or land F4 first), not something to hide by weakening a test.

THE HARD PLAN GATE IS OFF IN THE TEMPLATE (`gates.hard_plan_gate` is unset/false in
`config.json.tmpl`), so `work.py pr`'s plan-on-branch check never fires here and no plan file is
copied into any worktree for this fixture.

NOT PROVEN HERE (stated, not silently assumed -- doc D7(3)/plan §"Task 5 test 1"): the doc's §5 (the
board -- only branch (b), an owner that already has boards, is exercised; branch (a)'s
`project create` auto-create is not modelled by the fake) and §6 (a defect in Sigma itself --
this goal's control makes no `handoff.py`/routing call at all). None of this has yet been run
against real GitHub; that is #2588's gate.
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import textwrap

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SDLC_INIT = ROOT / "skills" / "sigma-init" / "scripts" / "sdlc_init.py"
SETUP = ROOT / "skills" / "sigma-setup" / "scripts" / "setup.py"
LOOP_DIR = ROOT / "skills" / "sigma-loop" / "scripts"
LOOP = LOOP_DIR / "loop.py"
WORK = LOOP_DIR / "work.py"
PHASE_REPORT = LOOP_DIR / "phase_report.py"
PUBLIC_REPO_DOC = ROOT / "skills" / "sigma-setup" / "references" / "public-repo.md"

#: The doc's own marker line (D7/Task 2) -- the fence this test extracts is the one whose FIRST
#: line inside it is exactly this text.
MARKER_LINE = "# public-repository adoption: run from the repository root, after /sigma-init"

#: The generic repo name and verify command substituted for the doc's two placeholders (D7: "Each
#: placeholder appears exactly once").
FAKE_REPO = "acme/public-core"
FAKE_VERIFY_CMD = "true"

#: The nine lifecycle labels `_LABEL_COLORS` creates (sources.py:791-806), spelled out here so this
#: file stands alone and never has to import `sources.py` to know its own vocabulary.
LIFECYCLE_LABELS = (
    "sdlc:goal", "sdlc:in-progress", "sdlc:parked", "sdlc:blocked", "sdlc:blocking",
    "sdlc:needs-label", "sdlc:designed", "sdlc:needs-unit",
    "sdlc:needs-triage",
)

#: D7(3)/Task 5 test 1: Task 2's own one-time authoring grep, permanently re-run here in Python
#: against the SHIPPED doc's full text, case-insensitively -- stricter than
#: `tests/test_no_private_names.py`, which only scans for the PRD Appendix B vocabulary and would
#: not catch any of these six.
_FORBIDDEN_PATTERNS = (
    re.compile(r"#[0-9]{3,}"),
    re.compile(r"private", re.IGNORECASE),
    re.compile(r"superpowers", re.IGNORECASE),
    re.compile(r"/Users/", re.IGNORECASE),
    re.compile(r"swapnil", re.IGNORECASE),
    # The org slug, personal handles and the retired skill prefix all stay forbidden: the public
    # brand is `sigma-` (#523), so the company name in any spelling is forbidden in the shipped doc.
    re.compile(r"Agrim", re.IGNORECASE),
)


# --------------------------------------------------------------------------------------------------
# The fake `gh`: a stateful script on PATH (B2). Written into <tmp>/bin/gh at runtime, mode 0755,
# `#!{sys.executable}`. State lives in FAKE_GH_STATE (JSON); every call is appended to FAKE_GH_LOG,
# and any call this fake does not model is ALSO appended to FAKE_GH_UNHANDLED and exits 1.
# --------------------------------------------------------------------------------------------------

_FAKE_GH_BODY = r'''
import json
import os
import re
import subprocess
import sys
import urllib.parse

STATE_PATH = os.environ["FAKE_GH_STATE"]
LOG_PATH = os.environ["FAKE_GH_LOG"]
UNHANDLED_PATH = os.environ["FAKE_GH_UNHANDLED"]


def load_state():
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f)


def log_call(argv):
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(argv) + "\n")


def unhandled(argv, note=""):
    with open(UNHANDLED_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps({"argv": argv, "note": note}) + "\n")
    sys.stderr.write("FAKE-GH-UNHANDLED " + " ".join(argv) + ((" -- " + note) if note else "") + "\n")
    sys.exit(1)


def parse(args):
    """(positionals, flags, multi) -- `--name value` / `--name=value` / bare `--flag`, `-f`/`-F`
    (repeatable, collected into multi["f"]), `--label`/`--add-label`/`--remove-label` (repeatable,
    collected under their own key in multi), and `-X value` (folded into flags["method"])."""
    pos, flags, multi = [], {}, {}
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-f", "-F") and i + 1 < len(args):
            multi.setdefault("f", []).append(args[i + 1]); i += 2; continue
        if a in ("--label", "--add-label", "--remove-label") and i + 1 < len(args):
            multi.setdefault(a[2:], []).append(args[i + 1]); i += 2; continue
        if a == "-X" and i + 1 < len(args):
            flags["method"] = args[i + 1]; i += 2; continue
        if a.startswith("--"):
            name = a[2:]
            if "=" in name:
                k, _, v = name.partition("="); flags[k] = v; i += 1; continue
            if i + 1 < len(args) and not args[i + 1].startswith("-"):
                flags[name] = args[i + 1]; i += 2; continue
            flags[name] = "true"; i += 1; continue
        pos.append(a); i += 1
    return pos, flags, multi


def resolve_placeholders(text, repo):
    owner, name = repo.split("/", 1)
    return text.replace("{owner}/{repo}", repo).replace("{owner}", owner).replace("{repo}", name)


def git(state, *args):
    return subprocess.run(["git", "--git-dir", state["remote_git_dir"], *args],
                           capture_output=True, text=True)


def git_rev_parse(state, ref):
    proc = git(state, "rev-parse", ref)
    return proc.stdout.strip() if proc.returncode == 0 else None


#: The closed, small set of `--jq` expressions the shipped CLIs actually pass (per the plan's own
#: "what the fake models"). A `null`/missing value renders as an EMPTY LINE, matching real gh's own
#: template rendering -- never the four-character text `null` a raw `jq -r` would print.
_JQ_KEYS = {
    ".number": "number", ".default_branch": "default_branch", ".viewerPermission": "viewerPermission",
    ".nameWithOwner": "nameWithOwner", ".permissions": "permissions", ".allow_auto_merge": "allow_auto_merge", ".state": "state",
    ".login": "login",
}


def emit(obj, jq_expr, argv):
    if not jq_expr:
        print(json.dumps(obj)); return
    if jq_expr == ".[0].number":
        val = (obj[0].get("number") if obj else None)
    elif jq_expr in _JQ_KEYS:
        val = obj.get(_JQ_KEYS[jq_expr]) if isinstance(obj, dict) else None
    else:
        unhandled(argv, "unsupported --jq expression %r" % jq_expr); return
    if val is None:
        print("")
    elif isinstance(val, bool):
        print("true" if val else "false")
    elif isinstance(val, (dict, list)):
        print(json.dumps(val))                  # gh renders an object/array `--jq` result as JSON
    else:
        print(val)


def _as_comment(c):
    return c if isinstance(c, dict) else {"body": c, "assoc": "OWNER"}


def pr_obj(state, number):
    pr = state["prs"][number]
    head_sha = git_rev_parse(state, "refs/heads/" + pr["head"]) or ""
    return {
        "number": int(number), "state": pr["state"], "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN", "statusCheckRollup": [], "headRefOid": head_sha,
        "isCrossRepository": False, "autoMergeRequest": None,
        "nameWithOwner": state["repo"],
        # #232: the review gate's own reads -- `comments,author` for `sigma:` directives and
        # `reviewDecision,latestReviews` for formal reviews. Comments are what `pr comment` stored.
        "comments": [{"body": c["body"], "author": {"login": state["login"]},
                      "authorAssociation": c["assoc"]}                  # #635: who the comment is from
                     for c in map(_as_comment, pr.get("comments", []))],
        "author": {"login": state["login"]}, "reviewDecision": None, "latestReviews": [],
    }


def pr_rest_obj(state, number):
    """The single-PR REST shape (`gh api repos/{owner}/{repo}/pulls/<n>`), as `work.py`'s own
    merge observation and `unit_completion.py`'s already-merged landing branch both call it --
    a genuinely NEW call this fixture never had to model until #2577 (and its own round-9
    unit-completion wiring) started making it during a normal, both-stores-on merge. Distinct
    from `pr_obj`'s GraphQL-shaped `gh pr view --json` response above: real field names
    (`node_id`, `merge_commit_sha`, `head.ref`, not `headRefOid`), because that is the shape the
    real endpoint returns and the shape `_receipt_parent_facts` reads.

    A fast-forward merge in this fixture (`cmd_pr`'s `merge` subcommand, `git update-ref`) never
    creates a distinct merge commit, so `merge_commit_sha` is the same as the fast-forwarded head
    SHA -- correct for how this fixture actually merges, not a real GitHub squash/merge-commit
    shape, which this fixture does not simulate.

    #895 4a-1: the seven migrated PR reads (`gh_api.view_pr_gh`) read THIS shape too, so it carries
    what GitHub's REST pull carries for them: lowercase `state` (`open`/`closed`; MERGED is
    `closed` + `merged: true` + `merged_at`), `user.login`, `auto_merge` (null: this fixture never
    arms), `title`/`body`, and `head.repo`/`base.repo.full_name` (same repo: no fork)."""
    pr = state["prs"][number]
    head_sha = git_rev_parse(state, "refs/heads/" + pr["head"]) or ""
    owner, name = state["repo"].split("/", 1)
    merged = pr["state"] == "MERGED"
    return {
        "number": int(number), "node_id": "PR_%s" % number,
        "state": "open" if pr["state"] == "OPEN" else "closed",
        "merged": merged, "title": pr.get("title", ""), "body": pr.get("body", ""),
        "user": {"login": state["login"], "type": "User"}, "auto_merge": None,
        "created_at": "2026-01-01T00:00:00Z",
        "merged_at": "2026-01-02T00:00:00Z" if merged else None,
        "closed_at": "2026-01-02T00:00:00Z" if pr["state"] != "OPEN" else None,
        "merge_commit_sha": head_sha if merged else None,
        "head": {"ref": pr["head"], "sha": head_sha,
                 "repo": {"full_name": state["repo"], "node_id": "R_%s" % name}},
        "base": {"ref": pr["base"], "repo": {"full_name": state["repo"], "node_id": "R_%s" % name}},
        # #895 4a-2: the merge gate's REST read. Same answer `pr_obj` gives gh: MERGEABLE + CLEAN.
        "mergeable": True, "mergeable_state": "clean",
    }


def pr_rest_comments(state, number, page, per_page):
    """One REST `issues/<n>/comments` page for a PR -- the ONE comments-read shape, serving both
    readers: #875's `work._find_evidence_marker` (`id`, `body`) and #895 4a-1's `gh_api.pr_comments`
    (the review gate's marker scan: `user.login`, `author_association` = the association the comment
    was posted with). What `pr comment` stored, oldest first; an id-less legacy row is not served."""
    pr = state["prs"].get(number) or {}
    rows = [{"id": c["id"], "node_id": "IC_%s" % c["id"], "user": {"login": state["login"]},
             "body": c["body"], "created_at": "2026-01-01T00:%02d:00Z" % (i % 60),
             "author_association": c.get("assoc", "OWNER")}
            for i, c in enumerate(map(_as_comment, pr.get("comments", []))) if c.get("id")]
    return rows[(page - 1) * per_page: page * per_page]


def issue_rest_obj(state, number):
    issue = state["issues"][number]
    return {
        "number": int(number), "state": issue["state"],
        "labels": [{"name": l} for l in issue["labels"]],
        "title": issue.get("title", ""), "body": issue.get("body", ""),
        "assignees": [{"login": a} for a in issue.get("assignees", [])],
    }


def cmd_label(state, argv, pos, flags):
    sub = pos[0]
    if sub == "create":
        name = pos[1]
        if state.get("refuse_label_create"):
            sys.stderr.write("HTTP 403: Resource not accessible by integration "
                              "(https://api.github.com/repos/%s/labels)\n" % state["repo"])
            sys.exit(1)
        if name in state["labels"]:
            sys.stderr.write("HTTP 422: Validation Failed (label %r already exists; use "
                              "`--force` to update its color and description)\n" % name)
            sys.exit(1)
        state["label_seq"] += 1
        state["labels"][name] = {"id": "LABEL_%d" % state["label_seq"], "color": flags.get("color", "")}
        save_state(state); return
    if sub == "delete":
        name = pos[1]
        if name not in state["labels"]:
            sys.stderr.write("HTTP 404: Not Found (label %r)\n" % name)
            sys.exit(1)
        del state["labels"][name]
        for issue in state["issues"].values():
            if name in issue["labels"]:
                issue["labels"].remove(name)
        save_state(state); return
    unhandled(argv, "unmodeled label subcommand")


def _split_labels(multi, key):
    out = []
    for item in multi.get(key, []):
        out.extend(x for x in item.split(",") if x)
    return out


def cmd_issue(state, argv, pos, flags, multi):
    sub = pos[0]
    if sub == "create":
        wanted = _split_labels(multi, "label")
        for l in wanted:
            if l not in state["labels"]:
                sys.stderr.write("could not add label: %r not found\n" % l)
                sys.exit(1)
        number = str(state["issue_seq"]); state["issue_seq"] += 1
        assignee = flags.get("assignee")
        assignees = [state["login"]] if assignee == "@me" else ([assignee.lstrip("@")] if assignee else [])
        state["issues"][number] = {"labels": wanted, "state": "open", "title": flags.get("title", ""),
                                    "body": flags.get("body", ""), "assignees": assignees}
        save_state(state)
        print("https://github.com/%s/issues/%s" % (state["repo"], number))
        return
    if sub == "edit":
        number = pos[1]
        add = _split_labels(multi, "add-label")
        remove = _split_labels(multi, "remove-label")
        for l in add:
            if l not in state["labels"]:
                sys.stderr.write("could not add label: %r not found\n" % l)
                sys.exit(1)
        issue = state["issues"].get(number)
        if issue is not None:
            for l in add:
                if l not in issue["labels"]:
                    issue["labels"].append(l)
            for l in remove:
                if l in issue["labels"]:
                    issue["labels"].remove(l)
        save_state(state); return
    if sub == "view":
        number = pos[1]
        issue = state["issues"].get(number)
        if issue is None:
            sys.stderr.write("HTTP 404: Not Found (issue %s)\n" % number); sys.exit(1)
        fields = (flags.get("json") or "").split(",") if flags.get("json") else []
        obj = {}
        for f in fields:
            if f == "state":
                obj["state"] = "CLOSED" if issue["state"] == "closed" else "OPEN"
            elif f == "labels":
                obj["labels"] = [{"name": l} for l in issue["labels"]]
            elif f == "number":
                obj["number"] = int(number)
            elif f == "body":
                obj["body"] = issue.get("body", "")
            elif f == "assignees":
                obj["assignees"] = [{"login": a} for a in issue.get("assignees", [])]
        emit(obj, flags.get("jq"), argv); return
    if sub == "close":
        number = pos[1]
        issue = state["issues"].get(number)
        if issue is not None:
            issue["state"] = "closed"
        save_state(state); return
    if sub == "comment":
        save_state(state); return
    if sub == "list":
        want_label = flags.get("label")
        want_state = flags.get("state", "open")
        out = []
        for number, issue in state["issues"].items():
            if want_label and want_label not in issue["labels"]:
                continue
            if want_state != "all" and issue["state"] != want_state:
                continue
            out.append({"number": int(number), "state": "CLOSED" if issue["state"] == "closed" else "OPEN",
                        "labels": [{"name": l} for l in issue["labels"]]})
        print(json.dumps(out)); return
    unhandled(argv, "unmodeled issue subcommand")


def cmd_pr(state, argv, pos, flags):
    sub = pos[0]
    if sub == "view":
        number = pos[1]
        if number not in state["prs"]:
            sys.stderr.write("HTTP 404: Not Found (pull request %s)\n" % number); sys.exit(1)
        full = pr_obj(state, number)
        fields = (flags.get("json") or "").split(",") if flags.get("json") else []
        obj = {f: full.get(f) for f in fields}
        emit(obj, flags.get("jq"), argv); return
    if sub == "merge":
        number = pos[1]
        pr = state["prs"].get(number)
        if pr is None:
            sys.stderr.write("HTTP 404: Not Found (pull request %s)\n" % number); sys.exit(1)
        head_sha = git_rev_parse(state, "refs/heads/" + pr["head"])
        if not head_sha:
            sys.stderr.write("gh: pull request head ref %r not found on the remote\n" % pr["head"])
            sys.exit(1)
        base_ref = "refs/heads/" + pr["base"]
        base_sha = git_rev_parse(state, base_ref)
        if base_sha:
            check = git(state, "merge-base", "--is-ancestor", base_sha, head_sha)
            if check.returncode != 0:
                sys.stderr.write("gh: %s is not mergeable into %s (not a fast-forward)\n"
                                  % (pr["head"], pr["base"]))
                sys.exit(1)
        git(state, "update-ref", base_ref, head_sha)
        pr["state"] = "MERGED"
        # #895 4b-1: ONE merge routine serves both `gh pr merge` (its CLI line) and the REST
        # `PUT pulls/N/merge` (cmd_api dispatches here with `rest_put`; its JSON reply).
        reply = (json.dumps({"merged": True, "sha": head_sha, "message": "Pull Request successfully merged"})
                 if flags.get("rest_put") else "Merged pull request #%s" % number)
        save_state(state)
        print(reply)
        return
    if sub == "comment":
        pr = state["prs"].get(pos[1])
        if pr is not None:
            cid = 1 + max([c.get("id", 0) for p in state["prs"].values()
                           for c in map(_as_comment, p.get("comments", []))] or [0])
            pr.setdefault("comments", []).append({"id": cid, "body": flags.get("body", ""),
                                                  "assoc": os.environ.get("FAKEGH_ASSOC", "OWNER")})
            save_state(state)
            print("https://github.com/%s/pull/%s#issuecomment-%d" % (state["repo"], pos[1], cid)); return
        save_state(state); return
    unhandled(argv, "unmodeled pr subcommand")


def cmd_repo(state, argv, pos, flags):
    if pos[0] == "view":
        fields = (flags.get("json") or "").split(",") if flags.get("json") else []
        obj = {}
        if "nameWithOwner" in fields:
            obj["nameWithOwner"] = state["repo"]
        if "viewerPermission" in fields:
            obj["viewerPermission"] = state.get("viewer_permission", "ADMIN")
        if "owner" in fields or "name" in fields:
            owner, name = state["repo"].split("/", 1)
            obj["owner"] = {"login": owner}; obj["name"] = name
        emit(obj, flags.get("jq"), argv); return
    unhandled(argv, "unmodeled repo subcommand")


def cmd_project(state, argv, pos, flags):
    if pos[0] == "list":
        print(json.dumps({"projects": state["projects"], "totalCount": len(state["projects"])}))
        return
    unhandled(argv, "unmodeled project subcommand (branch (a) auto-create is not modelled)")


def _graphql_label_lookup(state, doc, argv):
    fields = dict(re.findall(r'(\w+): label\(name: "((?:[^"\\]|\\.)*)"\)', doc))
    out = {}
    for alias, name in fields.items():
        name = name.replace('\\"', '"').replace("\\\\", "\\")
        lbl = state["labels"].get(name)
        out[alias] = {"id": lbl["id"], "name": name} if lbl else None
    print(json.dumps({"data": {"repository": out}}))


def _graphql_issue_id(state, doc, argv):
    m = re.search(r"issue\(number: (\d+)\)", doc)
    number = m.group(1)
    issue = state["issues"].get(number)
    node = {"id": "ISSUE_%s" % number} if issue is not None else None
    print(json.dumps({"data": {"repository": {"issue": node}}}))


def _label_name_for_id(state, node_id):
    for name, lbl in state["labels"].items():
        if lbl["id"] == node_id:
            return name
    return None


def _issue_number_for_id(state, node_id):
    if node_id.startswith("ISSUE_"):
        n = node_id[len("ISSUE_"):]
        if n in state["issues"]:
            return n
    return None


def _graphql_mutation(state, doc, argv):
    add_m = re.search(r'a: addLabelsToLabelable\(input: \{labelableId: "([^"]+)", labelIds: \[([^\]]*)\]\}\)', doc)
    rem_m = re.search(r'r: removeLabelsFromLabelable\(input: \{labelableId: "([^"]+)", labelIds: \[([^\]]*)\]\}\)', doc)
    result = {}
    for kind, m in (("a", add_m), ("r", rem_m)):
        if not m:
            continue
        labelable_id, ids_text = m.group(1), m.group(2)
        number = _issue_number_for_id(state, labelable_id)
        if number is None:
            sys.stderr.write("Could not resolve to a node with the global id of %r\n" % labelable_id)
            sys.exit(1)
        ids = re.findall(r'"([^"]+)"', ids_text)
        names = []
        for node_id in ids:
            name = _label_name_for_id(state, node_id)
            if name is None:
                sys.stderr.write("Could not resolve to a node with the global id of %r\n" % node_id)
                sys.exit(1)
            names.append(name)
        issue = state["issues"][number]
        if kind == "a":
            for name in names:
                if name not in issue["labels"]:
                    issue["labels"].append(name)
        else:
            for name in names:
                if name in issue["labels"]:
                    issue["labels"].remove(name)
        result[kind] = {"clientMutationId": None}
    save_state(state)
    print(json.dumps({"data": result}))


def cmd_api(state, argv, pos, flags, multi):
    endpoint = resolve_placeholders(pos[0], state["repo"])
    method = flags.get("method", "").upper()
    if endpoint == "graphql":
        doc = ""
        for item in multi.get("f", []):
            if item.startswith("query="):
                doc = item[len("query="):]; break
        stripped = doc.strip()
        if stripped.startswith("mutation"):
            _graphql_mutation(state, doc, argv)
        elif "label(name:" in doc:
            _graphql_label_lookup(state, doc, argv)
        elif "issue(number:" in doc:
            _graphql_issue_id(state, doc, argv)
        elif "reviewThreads" in doc:
            # #232: the review gate's unresolved-thread count; this fake models no threads.
            print(json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}}))
        else:
            unhandled(argv, "unmodeled graphql document shape")
        return
    if endpoint == "user":
        emit({"login": state["login"]}, flags.get("jq"), argv); return
    repo = state["repo"]
    if endpoint == "repos/%s/labels" % repo and method in ("", "GET"):
        # #230: `ensure_labels_report`'s REST read of the repo's labels (paginated, 100 per page).
        fields = {}
        for item in multi.get("f", []):
            k, _, v = item.partition("="); fields[k] = v
        per_page, page = int(fields.get("per_page", "30")), int(fields.get("page", "1"))
        names = sorted(state["labels"])
        emit([{"name": n, "color": state["labels"][n].get("color", "")}
              for n in names[(page - 1) * per_page: page * per_page]], flags.get("jq"), argv); return
    m = re.match(r"^repos/%s/issues/(\d+)/comments(?:\?(.*))?$" % re.escape(repo), endpoint)
    if m and (method == "GET" or (method == "" and not multi.get("f"))):
        # (real gh api infers POST from -f/-F fields, so a field call is not a read)
        # #875: the paged issue-comments read `work._find_evidence_marker` scans (GET only); #895
        # 4a-1: also `gh_api.pr_comments`, whose paging arrives as `--method GET -f per_page=100 -f
        # page=k` (a GET's fields become the query string), so read both.
        q = dict(kv.partition("=")[::2] for kv in (m.group(2) or "").split("&") if kv)
        if method == "GET":
            for item in multi.get("f", []):
                k, _, v = item.partition("="); q[k] = v
        try:
            per_page, page = max(1, int(q.get("per_page", "30"))), max(1, int(q.get("page", "1")))
        except ValueError:
            per_page, page = 30, 1
        emit(pr_rest_comments(state, m.group(1), page, per_page), flags.get("jq"), argv); return
    if endpoint == "repos/%s" % repo:
        ladder = ("pull", "triage", "push", "maintain", "admin")     # #895 4b-2: REST `permissions`
        top = {"READ": 0, "TRIAGE": 1, "WRITE": 2, "MAINTAIN": 3, "ADMIN": 4}[state.get("viewer_permission", "ADMIN")]
        obj = {"default_branch": state["default_branch"],
               "allow_auto_merge": state.get("allow_auto_merge", True),
               "permissions": {k: i <= top for i, k in enumerate(ladder)}}
        emit(obj, flags.get("jq"), argv); return
    m = re.match(r"^repos/%s/pulls\?head=%s:(.+)$" % (re.escape(repo), re.escape(repo.split("/")[0])), endpoint)
    if m:
        head = m.group(1)
        matches = [pr_obj(state, n) for n, pr in state["prs"].items() if pr["head"] == head]
        emit(matches, flags.get("jq"), argv); return
    m = re.match(r"^repos/%s/commits/[0-9a-f]{40}/(check-runs|status)$" % re.escape(repo), endpoint)
    if m and method == "GET":
        # #895 4a-2: the gate's rollup reads for the pull's head. This fixture has no CI, exactly as
        # `pr_obj`'s empty `statusCheckRollup`; the combined `state` is `pending` like real GitHub's
        # empty set, and must never be read.
        emit({"total_count": 0, "check_runs": []} if m.group(1) == "check-runs"
             else {"total_count": 0, "state": "pending", "statuses": []}, flags.get("jq"), argv); return
    m = re.match(r"^repos/%s/pulls/(\d+)/merge$" % re.escape(repo), endpoint)
    if m and method == "PUT":
        # #895 4b-1: the code-goal merge. A `sha` that is not the current head is GitHub's documented
        # 409 (DERIVED, UNMEASURED live); otherwise the SAME `sub == "merge"` body runs.
        fields = dict(item.partition("=")[::2] for item in multi.get("f", []))
        pr = state["prs"].get(m.group(1))
        head = git_rev_parse(state, "refs/heads/" + pr["head"]) if pr is not None else None
        if pr is not None and "sha" in fields and fields["sha"] != head:
            sys.stderr.write("gh: Head branch was modified. Review and try the merge again. (HTTP 409)\n")
            sys.exit(1)
        cmd_pr(state, argv, ["merge", m.group(1)], {"rest_put": "true"}); return
    m = re.match(r"^repos/%s/pulls/(\d+)/reviews$" % re.escape(repo), endpoint)
    if m and method == "GET":
        # #895 4a-2 PR B: the review gate's REST reviews read. This fixture has no reviewers: one empty page.
        emit([], flags.get("jq"), argv); return
    m = re.match(r"^repos/%s/pulls/(\d+)$" % re.escape(repo), endpoint)
    if m and method in ("", "GET"):
        number = m.group(1)
        pr = state["prs"].get(number)
        if pr is None:
            sys.stderr.write("HTTP 404: Not Found (pull request %s)\n" % number); sys.exit(1)
        emit(pr_rest_obj(state, number), flags.get("jq"), argv); return
    if endpoint == "repos/%s/pulls" % repo and method != "GET":
        fields = {}
        for item in multi.get("f", []):
            k, _, v = item.partition("="); fields[k] = v
        number = str(state["pr_seq"]); state["pr_seq"] += 1
        state["prs"][number] = {"state": "OPEN", "base": fields.get("base", ""),
                                 "head": fields.get("head", ""), "title": fields.get("title", "")}
        save_state(state)
        emit(pr_obj(state, number), flags.get("jq"), argv); return
    if re.match(r"^repos/%s/issues/\d+$" % re.escape(repo), endpoint) and method not in ("PATCH",):
        number = endpoint.rsplit("/", 1)[-1]
        issue = state["issues"].get(number)
        if issue is None:
            sys.stderr.write("HTTP 404: Not Found (issue %s)\n" % number); sys.exit(1)
        emit(issue_rest_obj(state, number), flags.get("jq"), argv); return
    if re.match(r"^repos/%s/issues/\d+$" % re.escape(repo), endpoint) and method == "PATCH":
        number = endpoint.rsplit("/", 1)[-1]
        fields = {}
        for item in multi.get("f", []):
            k, _, v = item.partition("="); fields[k] = v
        issue = state["issues"].get(number)
        if issue is not None and fields.get("state") == "closed":
            issue["state"] = "closed"
        save_state(state); return
    # #895: the REST write endpoints the migrated sites use (gh_api.py).
    def _rest_fields():
        out, lists = {}, {}
        for item in multi.get("f", []):
            k, _, v = item.partition("=")
            if k.endswith("[]"):
                lists.setdefault(k[:-2], []).append(v)
            else:
                out[k] = v
        return out, lists
    if endpoint == "repos/%s/issues" % repo and method == "POST":
        fields, lists = _rest_fields()
        wanted = lists.get("labels", [])
        for l in wanted:
            if l not in state["labels"]:
                sys.stderr.write("HTTP 422: Validation Failed (label %r not found)\n" % l); sys.exit(1)
        number = str(state["issue_seq"]); state["issue_seq"] += 1
        state["issues"][number] = {"labels": wanted, "state": "open", "title": fields.get("title", ""),
                                    "body": fields.get("body", ""), "assignees": []}
        save_state(state)
        emit(issue_rest_obj(state, number), flags.get("jq"), argv); return
    m = re.match(r"^repos/%s/issues/(\d+)/comments$" % re.escape(repo), endpoint)
    # (real gh infers POST from -f/-F fields: #878's merge note posts this way, with no --method)
    if m and (method == "POST" or (method == "" and multi.get("f"))):
        pr = state["prs"].get(m.group(1))
        if pr is not None:
            # #895 4b-2: a PR comment (post_review) -- the SAME store `pr_rest_comments` reads back.
            fields, _ = _rest_fields()
            cid = 1 + max([c.get("id", 0) for p in state["prs"].values()
                           for c in map(_as_comment, p.get("comments", []))] or [0])
            pr.setdefault("comments", []).append({"id": cid, "body": fields.get("body", ""),
                                                  "assoc": os.environ.get("FAKEGH_ASSOC", "OWNER")})
            save_state(state)
            emit({"id": cid, "html_url": "https://github.com/%s/pull/%s#issuecomment-%d"
                  % (state["repo"], m.group(1), cid)}, flags.get("jq"), argv); return
        issue = state["issues"].get(m.group(1))
        if issue is None:
            sys.stderr.write("HTTP 404: Not Found (issue %s)\n" % m.group(1)); sys.exit(1)
        fields, _ = _rest_fields()
        issue.setdefault("comments", []).append(fields.get("body", ""))
        save_state(state)
        emit({"body": fields.get("body", "")}, flags.get("jq"), argv); return
    m = re.match(r"^repos/%s/issues/(\d+)/labels$" % re.escape(repo), endpoint)
    if m and method == "POST":
        issue = state["issues"].get(m.group(1))
        if issue is None:
            sys.stderr.write("HTTP 404: Not Found (issue %s)\n" % m.group(1)); sys.exit(1)
        _, lists = _rest_fields()
        for l in lists.get("labels", []):
            if l not in state["labels"]:
                sys.stderr.write("HTTP 422: Validation Failed (label %r not found)\n" % l); sys.exit(1)
            if l not in issue["labels"]:
                issue["labels"].append(l)
        save_state(state)
        emit([{"name": l} for l in issue["labels"]], flags.get("jq"), argv); return
    m = re.match(r"^repos/%s/issues/(\d+)/labels/(.+)$" % re.escape(repo), endpoint)
    if m and method == "DELETE":
        issue = state["issues"].get(m.group(1))
        name = urllib.parse.unquote(m.group(2))
        if issue is None or name not in issue["labels"]:
            sys.stderr.write("HTTP 404: Label does not exist\n"); sys.exit(1)
        issue["labels"].remove(name)
        save_state(state)
        emit([{"name": l} for l in issue["labels"]], flags.get("jq"), argv); return
    m = re.match(r"^repos/%s/issues/(\d+)/assignees$" % re.escape(repo), endpoint)
    if m and method == "POST":
        issue = state["issues"].get(m.group(1))
        if issue is None:
            sys.stderr.write("HTTP 404: Not Found (issue %s)\n" % m.group(1)); sys.exit(1)
        _, lists = _rest_fields()
        for a in lists.get("assignees", []):
            if a not in issue.setdefault("assignees", []):
                issue["assignees"].append(a)
        save_state(state)
        emit(issue_rest_obj(state, m.group(1)), flags.get("jq"), argv); return
    m = re.match(r"^repos/%s/labels/(.+)$" % re.escape(repo), endpoint)
    if m and method in ("", "GET"):
        name = urllib.parse.unquote(m.group(1))
        if name not in state["labels"]:
            sys.stderr.write("HTTP 404: Not Found (label %r)\n" % name); sys.exit(1)
        emit({"name": name, "color": state["labels"][name].get("color", "")}, flags.get("jq"), argv); return
    if endpoint == "repos/%s/issues" % repo:
        fields = {}
        for item in multi.get("f", []):
            k, _, v = item.partition("="); fields[k] = v
        want_labels = set(fields.get("labels", "").split(",")) if fields.get("labels") else None
        want_state = fields.get("state", "open")
        want_assignee = fields.get("assignee")
        out = []
        for number, issue in sorted(state["issues"].items(), key=lambda kv: int(kv[0])):
            if want_state != "all" and issue["state"] != want_state:
                continue
            if want_labels and not want_labels.issubset(set(issue["labels"])):
                continue
            if want_assignee and want_assignee not in issue.get("assignees", []):
                continue
            out.append({"number": int(number), "state": "open", "labels": [{"name": l} for l in issue["labels"]],
                        "assignees": [{"login": a} for a in issue.get("assignees", [])],
                        "title": issue.get("title", ""), "body": issue.get("body", "")})
        print(json.dumps(out)); return
    m = re.match(r"^repos/%s/rules/branches/([^/?]+)(?:\?.*)?$" % re.escape(repo), endpoint)
    if m and method in ("", "GET"):
        emit([], flags.get("jq"), argv); return           # #895 4b-2: modelled; this fixture has no rulesets
    m = re.match(r"^repos/%s/branches/([^/]+)/protection$" % re.escape(repo), endpoint)
    if m:
        sys.stderr.write("HTTP 404: Branch not protected\n"); sys.exit(1)
    m = re.match(r"^repos/%s/git/refs/heads/(.+)$" % re.escape(repo), endpoint)
    if m and method == "DELETE":
        branch = m.group(1)
        if not git_rev_parse(state, "refs/heads/" + branch):
            sys.stderr.write("HTTP 422: Reference does not exist\n"); sys.exit(1)
        git(state, "update-ref", "-d", "refs/heads/" + branch)
        return
    unhandled(argv, "unmodeled api endpoint")


def main():
    argv = sys.argv[1:]
    log_call(argv)
    state = load_state()
    if not argv:
        unhandled(argv, "empty invocation")
    verb = argv[0]
    pos, flags, multi = parse(argv[1:])
    if verb == "label":
        cmd_label(state, argv, pos, flags)
    elif verb == "issue":
        cmd_issue(state, argv, pos, flags, multi)
    elif verb == "pr":
        cmd_pr(state, argv, pos, flags)
    elif verb == "repo":
        cmd_repo(state, argv, pos, flags)
    elif verb == "project":
        cmd_project(state, argv, pos, flags)
    elif verb == "api":
        cmd_api(state, [verb, *argv[1:]], pos, flags, multi)
    elif verb == "auth" and pos[:1] == ["status"]:
        # #229: /sigma-init's preflight reads `gh auth status`; a logged-in classic token.
        print("github.com\n  Logged in to github.com account fake (keyring)\n"
              "  - Active account: true\n  - Token: gho_****\n"
              "  - Token scopes: 'read:org', 'repo', 'workflow'")
    else:
        unhandled(argv, "unmodeled verb")


if __name__ == "__main__":
    main()
'''


def _write_fake_gh(bin_dir):
    """Writes the fake to `<bin_dir>/gh`, mode 0755, `#!{sys.executable}`."""
    path = bin_dir / "gh"
    path.write_text("#!%s\n" % sys.executable + _FAKE_GH_BODY, encoding="utf-8")
    path.chmod(0o755)
    return path


def _init_state(state_path, remote_git_dir, repo=FAKE_REPO):
    state = {
        "repo": repo,
        "remote_git_dir": str(remote_git_dir),
        "labels": {}, "label_seq": 0,
        "issues": {}, "issue_seq": 1,
        "prs": {}, "pr_seq": 100,
        "projects": [{"number": 14, "id": "PVT_unrelated", "title": "Unrelated Board"}],
        "login": "public-core-bot",
        "default_branch": "main",
        "allow_auto_merge": True,
        "viewer_permission": "ADMIN",
        "refuse_label_create": False,
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")


def _read_state(state_path):
    return json.loads(state_path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------------------------------
# The world: a real bare remote + clone, a scratch env built from scratch (never inherited), and the
# fake `gh` on PATH first.
# --------------------------------------------------------------------------------------------------


def _make_env(bin_dir, home_dir, state_path, log_path, unhandled_path):
    py_dir = str(pathlib.Path(sys.executable).resolve().parent)
    return {
        "PATH": os.pathsep.join([str(bin_dir), py_dir, "/usr/bin", "/bin"]),
        "HOME": str(home_dir),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "Test Author", "GIT_AUTHOR_EMAIL": "author@example.com",
        "GIT_COMMITTER_NAME": "Test Author", "GIT_COMMITTER_EMAIL": "author@example.com",
        "PYTHONDONTWRITEBYTECODE": "1",
        "SETUP": str(SETUP),
        "LOOP": str(LOOP_DIR),
        "FAKE_GH_STATE": str(state_path),
        "FAKE_GH_LOG": str(log_path),
        "FAKE_GH_UNHANDLED": str(unhandled_path),
    }


def _make_repo_world(root, repo=FAKE_REPO):
    """A real bare remote + clone with one seed commit on `main`, a fake `gh` on PATH, and a fresh
    scratch env. No adoption yet -- `_adopt()` does that."""
    remote_dir = root / "remote.git"
    clone_dir = root / "clone"
    bin_dir = root / "bin"; bin_dir.mkdir()
    home_dir = root / "home"; home_dir.mkdir()
    state_path = root / "gh_state.json"
    log_path = root / "gh_log.jsonl"; log_path.write_text("", encoding="utf-8")
    unhandled_path = root / "gh_unhandled.jsonl"; unhandled_path.write_text("", encoding="utf-8")
    _write_fake_gh(bin_dir)
    _init_state(state_path, remote_dir, repo=repo)
    env = _make_env(bin_dir, home_dir, state_path, log_path, unhandled_path)

    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote_dir)],
                    env=env, capture_output=True, text=True, check=True)
    subprocess.run(["git", "clone", str(remote_dir), str(clone_dir)],
                    env=env, capture_output=True, text=True, check=True)
    (clone_dir / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "-A"], clone_dir, env)
    _git(["commit", "-m", "seed"], clone_dir, env)
    _git(["push", "-u", "origin", "main"], clone_dir, env)
    return {
        "root": root, "remote_dir": remote_dir, "clone_dir": clone_dir, "bin_dir": bin_dir,
        "state_path": state_path, "log_path": log_path, "unhandled_path": unhandled_path,
        "env": env, "fake_gh": bin_dir / "gh", "repo": repo,
    }


def _git(args, cwd, env):
    proc = subprocess.run(["git", *args], cwd=str(cwd), env=env, capture_output=True, text=True)
    assert proc.returncode == 0, "git %s failed:\n%s\n%s" % (args, proc.stdout, proc.stderr)
    return proc


def _cli(argv, cwd, env, check=True):
    proc = subprocess.run([sys.executable, *[str(a) for a in argv]], cwd=str(cwd), env=env,
                          capture_output=True, text=True)
    if check:
        assert proc.returncode == 0, "%s exited %d\nSTDOUT:\n%s\nSTDERR:\n%s" % (
            argv, proc.returncode, proc.stdout, proc.stderr)
    return proc


def _fakegh(world, args, cwd=None, check=True, assoc=None):
    env = dict(world["env"], **({"FAKEGH_ASSOC": assoc} if assoc else {}))
    proc = subprocess.run([sys.executable, str(world["fake_gh"]), *args], cwd=str(cwd or world["clone_dir"]),
                          env=env, capture_output=True, text=True)
    if check:
        assert proc.returncode == 0, "fake gh %s exited %d\nSTDOUT:\n%s\nSTDERR:\n%s" % (
            args, proc.returncode, proc.stdout, proc.stderr)
    return proc


def _extract_adoption_block(doc_text):
    """The one fenced `bash` block whose first line is exactly `MARKER_LINE` (D7)."""
    fences = re.findall(r"```bash\n(.*?)```", doc_text, re.DOTALL)
    matching = [f for f in fences if f.splitlines()[0].strip() == MARKER_LINE]
    assert len(matching) == 1, "expected exactly one adoption fence, found %d" % len(matching)
    return matching[0]


def _substitute_placeholders(block_text, repo, verify_cmd):
    return block_text.replace("<owner/name>", repo).replace("<your test command>", verify_cmd)


def _remove_labels_line(block_text):
    """Test 7: strip the `labels` call, first proving it was there exactly once (so the test cannot
    pass vacuously against a doc that never had the line at all)."""
    target = 'python3 "$SETUP" labels .sdlc'
    lines = block_text.splitlines()
    matches = [l for l in lines if l.strip() == target]
    assert len(matches) == 1, matches
    kept = [l for l in lines if l.strip() != target]
    return "\n".join(kept) + "\n"


def _run_adoption_block(block_text, cwd, env, repo=FAKE_REPO, verify_cmd=FAKE_VERIFY_CMD):
    script = _substitute_placeholders(block_text, repo, verify_cmd)
    proc = subprocess.run(["bash", "-c", script], cwd=str(cwd), env=env, capture_output=True, text=True)
    assert proc.returncode == 0, "adoption block failed:\nSTDOUT:\n%s\nSTDERR:\n%s" % (proc.stdout, proc.stderr)
    return proc


def _adopt(world):
    """`sdlc_init.py`, then the doc's own adoption block, run exactly as written (D7/M10)."""
    clone_dir, env = world["clone_dir"], world["env"]
    _cli([SDLC_INIT, clone_dir], clone_dir, env)
    block = _extract_adoption_block(PUBLIC_REPO_DOC.read_text(encoding="utf-8"))
    _run_adoption_block(block, clone_dir, env)


def _turn_both_stores_on(clone_dir):
    """B4's negative control (test 5): TWO keys, never the `_turn_the_journal_on` shape
    `tests/test_core_only_writes.py` uses for its own single-key opt-in."""
    cfg_path = clone_dir / ".sdlc" / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert cfg["ledger"]["enabled"] is False, "the profile must ship the ledger off"
    assert cfg["journal"]["enabled"] is False, "the profile must ship the journal off"
    cfg["ledger"]["enabled"] = True
    cfg["journal"]["enabled"] = True
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def _create_label(world, name, color="d4c5f9", check=True):
    return _fakegh(world, ["label", "create", name, "--repo", world["repo"], "--color", color], check=check)


def _delete_label(world, name, check=True):
    return _fakegh(world, ["label", "delete", name, "--repo", world["repo"]], check=check)


def _file_goal(world, title, body="body text", labels="sdlc:goal,priority:P1", assignee="@me", check=True):
    if body == "body text":
        body += ("\n\n## Done when\n- [ ] The feature output contains hello.\n"
                 "- [ ] The configured verification succeeds.\n"
                 "- [ ] The PR lands before the goal is recorded done.\n")
    return _fakegh(world, ["issue", "create", "--repo", world["repo"], "--label", labels,
                           "--assignee", assignee, "--title", title, "--body", body], check=check)


def _issue_labels(world, number):
    return sorted(_read_state(world["state_path"])["issues"][number]["labels"])


def _label_exists(world, name):
    return name in _read_state(world["state_path"])["labels"]


def _issue_state(world, number):
    return _read_state(world["state_path"])["issues"][number]["state"]


def _pr_state(world, number):
    return _read_state(world["state_path"])["prs"][number]["state"]


def _only_pr_number(world):
    prs = list(_read_state(world["state_path"])["prs"].keys())
    assert len(prs) == 1, prs
    return prs[0]


def _remote_show(world, ref):
    return subprocess.run(["git", "--git-dir", str(world["remote_dir"]), "show", ref],
                          capture_output=True, text=True)


# --------------------------------------------------------------------------------------------------
# The sequence, copied from `sigma-loop` SKILL.md, no stronger (see the module docstring).
# --------------------------------------------------------------------------------------------------


def _run_sequence(world, run_probe):
    """Runs steps 1-9, with the mid-run deletion / goal-2 filing / refill sitting between the claim
    (step 2's `next`) and `agent-start` -- and, when `run_probe`, the second-clone probe right after
    the refill and ITS OWN snapshot (never before it -- see the module docstring / plan §"The
    sequence" for why order here is load-bearing). Returns a dict of observations."""
    clone_dir, env = world["clone_dir"], world["env"]
    sdlc = clone_dir / ".sdlc"
    pid = str(os.getpid())
    obs = {}

    _cli([LOOP, "start", sdlc, "--session-pid", pid], clone_dir, env)
    nxt = _cli([LOOP, "next", sdlc, "--session-pid", pid], clone_dir, env)
    assert nxt.stdout.strip() == "1", nxt.stdout
    obs["next_stderr"] = nxt.stderr
    obs["issue1_labels_after_claim"] = _issue_labels(world, "1")

    _delete_label(world, "sdlc:in-progress")
    obs["issue1_labels_after_deletion"] = _issue_labels(world, "1")

    _file_goal(world, title="Test goal 2")
    refill = _cli([LOOP, "next", sdlc, "--session-pid", pid, "--skip", "1"], clone_dir, env)
    # THE SNAPSHOT, TAKEN THE INSTANT THIS CALL RETURNS -- before the probe below can touch the
    # label again (the round-3 fix; see the module docstring).
    obs["refill_stdout"] = refill.stdout.strip()
    obs["refill_stderr"] = refill.stderr
    obs["label_exists_after_refill"] = _label_exists(world, "sdlc:in-progress")
    obs["issue2_labels_after_refill"] = _issue_labels(world, "2")

    if run_probe:
        probe_clone = world["root"] / "probe-clone"
        subprocess.run(["git", "clone", str(world["remote_dir"]), str(probe_clone)],
                        env=env, capture_output=True, text=True, check=True)
        _cli([SDLC_INIT, probe_clone], probe_clone, env)
        block = _extract_adoption_block(PUBLIC_REPO_DOC.read_text(encoding="utf-8"))
        _run_adoption_block(block, probe_clone, env)
        _cli([LOOP, "start", probe_clone / ".sdlc", "--session-pid", pid], probe_clone, env)
        probe = _cli([LOOP, "next", probe_clone / ".sdlc", "--session-pid", pid], probe_clone, env)
        obs["probe_stdout"] = probe.stdout.strip()
        obs["probe_stderr"] = probe.stderr

    _cli([LOOP, "agent-start", sdlc, "1", "--pid", pid], clone_dir, env)
    _cli([WORK, "start", sdlc, "1", "--session-pid", pid], clone_dir, env)

    worktree = sdlc / "work" / "1"
    _cli([LOOP_DIR / "acceptance.py", "record", sdlc, "1"], clone_dir, env)
    target = worktree / ".sdlc" / "acceptance" / "1.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(sdlc / "acceptance" / "1.md", target)
    # #684: this control is about the review/merge gates on the public profile; the SDLC phase record
    # has its own tests (tests/test_phase_record_gate.py), so it is switched off with the documented lever.
    cfg_path = pathlib.Path(sdlc) / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg.setdefault("gates", {})["phase_record"] = {"enabled": False}
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    _cli([PHASE_REPORT, "start", sdlc, "1", "implement", "--model", "sonnet"], clone_dir, env)
    _cli([PHASE_REPORT, "end", sdlc, "1", "implement"], clone_dir, env)

    (worktree / "feature.txt").write_text("hello\n", encoding="utf-8")

    _cli([LOOP, "verify", sdlc, "1"], clone_dir, env)
    _cli([WORK, "commit", sdlc, "1", "--message", "sdlc: test goal 1"], clone_dir, env)
    _cli([WORK, "pr", sdlc, "1", "--no-tests",
          "Bootstrap fixture writes text only; shell verification is retained."], clone_dir, env)
    pr_number = _only_pr_number(world)
    obs["pr_number"] = pr_number

    # #232 (3): on the shipped defaults (`auto_merge: off`, `require_review: changes`) the review
    # gate must RUN before the PR is left for a human -- a reviewer's `sigma:block` parks the merge.
    _fakegh(world, ["pr", "comment", pr_number, "--repo", world["repo"],
                    "--body", "sigma:block the change has no test"], cwd=clone_dir)
    blocked = _cli([WORK, "merge", sdlc, "1"], clone_dir, env)
    obs["blocked_merge_stdout"] = blocked.stdout.strip()
    # #635: a stranger's `sigma:approve` / `sigma:unblock` must NOT clear that block (public repo: anyone
    # can comment). Only the owner's approve below does.
    for stranger_marker in ("sigma:approve", "sigma:unblock"):
        _fakegh(world, ["pr", "comment", pr_number, "--repo", world["repo"],
                        "--body", stranger_marker], cwd=clone_dir, assoc="NONE")
    stranger = _cli([WORK, "merge", sdlc, "1"], clone_dir, env)
    obs["stranger_merge_stdout"] = stranger.stdout.strip()
    obs["stranger_merge_stderr"] = stranger.stderr
    _fakegh(world, ["pr", "comment", pr_number, "--repo", world["repo"],
                    "--body", "sigma:approve"], cwd=clone_dir)
    merge = _cli([WORK, "merge", sdlc, "1"], clone_dir, env)
    obs["merge_stdout"] = merge.stdout.strip()

    # #232 (1): the documented gesture for that line is `record review`; `record done` is REFUSED
    # while the PR is open -- the control that a PR nobody merged never yields a closed issue.
    refused = _cli([LOOP, "record", sdlc, "1", "done"], clone_dir, env, check=False)
    obs["record_done_rc"] = refused.returncode
    obs["record_done_stderr"] = refused.stderr
    obs["issue1_state_after_refused_done"] = _issue_state(world, "1")

    review = _cli([LOOP, "record", sdlc, "1", "review"], clone_dir, env)
    obs["record_stderr"] = review.stderr
    obs["issue1_state_before_merge"] = _issue_state(world, "1")
    obs["issue1_labels_awaiting_merge"] = _issue_labels(world, "1")
    obs["pr_state_before_merge"] = _pr_state(world, pr_number)
    obs["remote_has_feature_before_merge"] = _remote_show(world, "main:feature.txt").returncode == 0
    obs["worktree_exists_before_merge"] = worktree.exists()

    # A goal awaiting merge is not pickable: `next` must not serve goal 1 again. #255 (8): the
    # claim's `sdlc:in-progress` overlay alone already hides the issue from `_fetch_pending`, so
    # with it in place this asserted nothing about the LOCAL skip (`_awaiting_merge_skip`). Drop
    # the overlay first -- a label lost in between is exactly the case the local skip exists for
    # -- so only the work record's `awaiting_merge` flag can keep goal 1 from being served again.
    _fakegh(world, ["issue", "edit", "1", "--repo", world["repo"],
                    "--remove-label", "sdlc:in-progress"], cwd=clone_dir)
    obs["issue1_labels_before_repick"] = _issue_labels(world, "1")
    repick = _cli([LOOP, "next", sdlc, "--session-pid", pid], clone_dir, env)
    obs["repick_stdout"] = repick.stdout.strip()

    # #232 (2): a reconcile pass while the PR is still open changes nothing.
    early = _cli([LOOP, "reconcile-merges", sdlc], clone_dir, env)
    obs["early_reconcile_stdout"] = early.stdout.strip()
    obs["issue1_state_after_early_reconcile"] = _issue_state(world, "1")

    _fakegh(world, ["pr", "merge", pr_number, "--repo", world["repo"], "--squash"], cwd=clone_dir)
    obs["issue1_state_after_human_merge"] = _issue_state(world, "1")

    late = _cli([LOOP, "reconcile-merges", sdlc], clone_dir, env)
    obs["late_reconcile_stdout"] = late.stdout.strip()
    again = _cli([LOOP, "reconcile-merges", sdlc], clone_dir, env)
    obs["again_reconcile_stdout"] = again.stdout.strip()

    obs["issue1_state_final"] = _issue_state(world, "1")
    obs["pr_state_after_merge"] = _pr_state(world, pr_number)
    remote_feature = _remote_show(world, "main:feature.txt")
    assert remote_feature.returncode == 0, remote_feature.stderr
    obs["remote_feature_txt"] = remote_feature.stdout
    obs["worktree_exists_after_merge"] = worktree.exists()
    obs["issue1_labels_final"] = _issue_labels(world, "1")
    obs["unhandled_log"] = world["unhandled_path"].read_text(encoding="utf-8")
    return obs


# --------------------------------------------------------------------------------------------------
# Module-scoped fixtures: the sequence runs ONCE per profile (primary / on-case), and every test
# that shares one only reads what it recorded.
# --------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def primary_world(tmp_path_factory):
    root = tmp_path_factory.mktemp("primary")
    world = _make_repo_world(root)
    _adopt(world)
    assert _label_exists(world, "priority:P1")   # #230: the adoption block's `labels` created it
    _file_goal(world, title="Test goal 1")
    world["obs"] = _run_sequence(world, run_probe=True)
    return world


@pytest.fixture(scope="module")
def on_case_world(tmp_path_factory):
    root = tmp_path_factory.mktemp("oncase")
    world = _make_repo_world(root)
    _adopt(world)
    _turn_both_stores_on(world["clone_dir"])
    assert _label_exists(world, "priority:P1")   # #230: the adoption block's `labels` created it
    _file_goal(world, title="Test goal 1")
    world["obs"] = _run_sequence(world, run_probe=False)
    return world


# --------------------------------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------------------------------


def test_the_documented_adoption_block_is_what_the_control_runs():
    text = PUBLIC_REPO_DOC.read_text(encoding="utf-8")
    block = _extract_adoption_block(text)
    assert block.count("<owner/name>") == 1
    assert block.count("<your test command>") == 1
    assert 'python3 "$SETUP" labels .sdlc' in block
    for key in ("ledger", "journal", "knowledge_graph", "auto_merge"):
        assert key in block, key
    for pattern in _FORBIDDEN_PATTERNS:
        assert not pattern.search(text), "forbidden pattern %r found in the shipped doc" % pattern.pattern


def test_a_goal_goes_from_filed_to_merged_on_the_public_profile(primary_world):
    """#232 acceptance, on the shipped defaults: the issue's final state and the PR's final state
    agree with the documented flow (done means merged). RED against the pre-#232 code, which
    closed the issue at `record done` while the PR was still OPEN (and never ran the review gate
    under `auto_merge: off`, so the `sigma:block` below did not park)."""
    obs = primary_world["obs"]
    assert "sdlc:goal" in obs["issue1_labels_after_claim"]
    assert "sdlc:in-progress" in obs["issue1_labels_after_claim"]
    # The review gate ran on defaults: a block parks, an approve lets the PR be left for a human.
    assert obs["blocked_merge_stdout"].startswith("PARK:"), obs["blocked_merge_stdout"]
    assert "sigma:block" in obs["blocked_merge_stdout"]
    assert obs["stranger_merge_stdout"].startswith("PARK:"), obs["stranger_merge_stdout"]   # #635
    assert "ignoring a sigma:approve comment" in obs["stranger_merge_stderr"]
    assert obs["merge_stdout"].startswith("clean and safe")
    assert "review gate passed" in obs["merge_stdout"]
    assert obs["merge_stdout"].endswith("leaving PR #%s for a human" % obs["pr_number"])
    # `record done` is refused while the PR is open, and the issue stays open.
    assert obs["record_done_rc"] == 4
    assert "PR #%s" % obs["pr_number"] in obs["record_done_stderr"]
    assert "record" in obs["record_done_stderr"] and "review" in obs["record_done_stderr"]
    assert obs["issue1_state_after_refused_done"] == "open"
    # `record review`: open issue, still a goal, still claimed; PR open; nothing on main yet.
    assert obs["issue1_state_before_merge"] == "open"
    assert "sdlc:goal" in obs["issue1_labels_awaiting_merge"]
    # The claim's overlay stays (the goal is still in flight); `next` does not serve goal 1 again.
    assert "sdlc:in-progress" in obs["issue1_labels_awaiting_merge"]
    assert "sdlc:in-progress" not in obs["issue1_labels_before_repick"]
    assert "sdlc:goal" in obs["issue1_labels_before_repick"]
    assert obs["repick_stdout"] != "1", obs["repick_stdout"]
    assert obs["pr_state_before_merge"] == "OPEN"
    assert obs["remote_has_feature_before_merge"] is False
    assert obs["worktree_exists_before_merge"] is True
    assert obs["issue1_state_after_early_reconcile"] == "open"
    assert obs["early_reconcile_stdout"] == ""
    # The human merge alone does not close it in this fixture (no closing keyword processing);
    # the reconcile pass observes the merge and closes it, exactly once.
    assert obs["issue1_state_after_human_merge"] == "open"
    assert obs["late_reconcile_stdout"] == "1 done (PR #%s merged)" % obs["pr_number"]
    assert obs["again_reconcile_stdout"] == ""
    assert obs["issue1_state_final"] == "closed"
    assert obs["pr_state_after_merge"] == "MERGED"
    assert obs["remote_feature_txt"] == "hello\n"
    assert obs["worktree_exists_after_merge"] is False
    assert not any(l.startswith("sdlc:") for l in obs["issue1_labels_final"])
    assert obs["unhandled_log"] == ""


def test_a_deleted_lifecycle_label_comes_back_and_the_next_claim_lands(primary_world):
    obs = primary_world["obs"]
    # All read from the snapshot taken the instant the refill returns (before the probe runs).
    assert "sdlc:in-progress" not in obs["issue1_labels_after_deletion"]
    assert obs["refill_stdout"] == "2"
    assert obs["label_exists_after_refill"] is True
    assert "sdlc:in-progress" in obs["issue2_labels_after_refill"]
    assert "CLAIM-LABEL-WRITE-FAILED" not in obs["refill_stderr"]
    assert obs["pr_state_after_merge"] == "MERGED"
    # KNOWN LIMIT (F6): a second clone double-picks a goal the first clone is still holding, because
    # the self-heal restores the label's existence, never its presence on an already-claimed issue.
    # See D5. A positive pin of the limitation itself, not a red/green pair.
    assert obs["probe_stdout"] == "1"
    assert "CLAIM-LABEL-WRITE-FAILED" not in obs["probe_stderr"]
    assert obs["unhandled_log"] == ""


def test_the_public_profile_writes_nothing_under_events_or_ledger(primary_world):
    sdlc = primary_world["clone_dir"] / ".sdlc"
    assert not (sdlc / "events").exists()
    # `knowledge_graph.enabled` ships false regardless of this profile -- no control, see the plan.
    assert not (sdlc / "knowledge").exists()
    assert sorted(p.name for p in (sdlc / "ledger").iterdir()) == ["README.md"]
    assert not (sdlc / "ledger" / "entries").exists()
    assert primary_world["obs"]["unhandled_log"] == ""


def test_the_census_sees_both_stores_when_ledger_and_journal_are_on(on_case_world):
    sdlc = on_case_world["clone_dir"] / ".sdlc"
    pairs = set()
    for p in (sdlc / "ledger" / "entries").glob("*.jsonl"):
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                e = json.loads(line)
                pairs.add((e.get("kind"), str(e.get("goal"))))
    assert {("claimed", "1"), ("claimed", "2"), ("done", "1")}.issubset(pairs), pairs

    sigs = set()
    for p in (sdlc / "events").glob("*.jsonl"):
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                e = json.loads(line)
                kind = e.get("kind")
                sigs.add("%s:%s" % (kind, e["state"]) if kind == "phase" and e.get("state") else kind)
    assert {"phase:start", "phase:end", "verify"}.issubset(sigs), sigs

    assert not (sdlc / "ledger" / ".git").exists()
    assert on_case_world["obs"]["pr_state_after_merge"] == "MERGED"
    assert on_case_world["obs"]["unhandled_log"] == ""


def test_a_refused_label_create_is_loud_not_silent(tmp_path):
    world = _make_repo_world(tmp_path)
    _adopt(world)
    assert _label_exists(world, "priority:P1")   # #230: the adoption block's `labels` created it
    _delete_label(world, "sdlc:in-progress")
    state = _read_state(world["state_path"])
    state["refuse_label_create"] = True
    world["state_path"].write_text(json.dumps(state), encoding="utf-8")
    _file_goal(world, title="Test goal 1")

    sdlc = world["clone_dir"] / ".sdlc"
    proc = _cli([LOOP, "next", sdlc, "--session-pid", str(os.getpid())], world["clone_dir"], world["env"])
    assert proc.stdout.strip() == "1"
    assert "CLAIM-LABEL-WRITE-FAILED #1" in proc.stderr
    assert not _label_exists(world, "sdlc:in-progress")
    assert "sdlc:in-progress" not in _issue_labels(world, "1")
    assert world["unhandled_path"].read_text(encoding="utf-8") == ""


def test_without_the_label_bootstrap_the_first_filing_is_refused(tmp_path):
    world = _make_repo_world(tmp_path)
    clone_dir, env = world["clone_dir"], world["env"]
    _cli([SDLC_INIT, clone_dir], clone_dir, env)
    block = _extract_adoption_block(PUBLIC_REPO_DOC.read_text(encoding="utf-8"))
    block = _remove_labels_line(block)
    _run_adoption_block(block, clone_dir, env)

    proc = _fakegh(world, ["issue", "create", "--repo", world["repo"], "--label", "sdlc:goal,priority:P1",
                           "--assignee", "@me", "--title", "t", "--body", "b"], check=False)
    assert proc.returncode != 0
    assert "could not add label: 'sdlc:goal' not found" in proc.stderr
    assert _read_state(world["state_path"])["issues"] == {}


def test_an_owner_with_existing_boards_degrades_loudly_and_the_goal_still_lands(primary_world):
    obs = primary_world["obs"]
    assert "board mirroring OFF this run" in obs["next_stderr"]
    calls = [json.loads(l) for l in primary_world["log_path"].read_text(encoding="utf-8").splitlines() if l.strip()]
    assert not any(c[:2] == ["project", "create"] for c in calls), calls
    assert obs["pr_state_after_merge"] == "MERGED"


def test_the_profile_survives_a_setup_rerun(tmp_path):
    world = _make_repo_world(tmp_path)
    _adopt(world)
    sdlc = world["clone_dir"] / ".sdlc"
    _cli([SETUP, "configure", sdlc, "--repo", world["repo"], "--verify", FAKE_VERIFY_CMD],
        world["clone_dir"], world["env"])
    cfg = json.loads((sdlc / "config.json").read_text(encoding="utf-8"))
    assert cfg["ledger"]["enabled"] is False
    assert cfg["work"]["auto_merge"] == "off"


def test_the_fake_refuses_what_gh_refuses(tmp_path):
    world = _make_repo_world(tmp_path)

    r1 = _create_label(world, "sdlc:goal")
    assert r1.returncode == 0
    r2 = _create_label(world, "sdlc:goal", color="ffffff", check=False)
    assert r2.returncode != 0
    assert "already exists" in r2.stderr

    r3 = _fakegh(world, ["issue", "create", "--repo", world["repo"], "--label", "sdlc:doesnotexist",
                         "--title", "t", "--body", "b"], check=False)
    assert r3.returncode != 0
    assert "not found" in r3.stderr

    r4 = _file_goal(world, title="t", labels="sdlc:goal")
    number = r4.stdout.strip().rsplit("/", 1)[-1]

    r5 = _fakegh(world, ["issue", "edit", number, "--add-label", "sdlc:doesnotexist",
                         "--repo", world["repo"]], check=False)
    assert r5.returncode != 0
    assert "not found" in r5.stderr

    old_id = _read_state(world["state_path"])["labels"]["sdlc:goal"]["id"]
    r6 = _delete_label(world, "sdlc:goal")
    assert r6.returncode == 0
    assert "sdlc:goal" not in _issue_labels(world, number)

    r7 = _create_label(world, "sdlc:goal")
    assert r7.returncode == 0
    new_id = _read_state(world["state_path"])["labels"]["sdlc:goal"]["id"]
    assert new_id != old_id

    r8 = _fakegh(world, ["nonsense", "verb"], check=False)
    assert r8.returncode != 0
    unhandled_lines = [l for l in world["unhandled_path"].read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(unhandled_lines) == 1


# --------------------------------------------------------------------------------------------------
# #875: the fake's `pr comment` URL + the paged issue-comments read (story #809, PC-7/BR-5/BR-6).
# --------------------------------------------------------------------------------------------------

_COMMENT_ID_RE = r"(?:comments?/|issuecomment-)(\d+)"  # the pattern text of work.py post_review


def _seed_pr(world, number="101"):
    state = _read_state(world["state_path"])
    state["prs"][number] = {"head": "h%s" % number, "base": "main", "state": "OPEN",
                           "title": "t", "body": "", "comments": []}
    world["state_path"].write_text(json.dumps(state), encoding="utf-8")


def _pr_comment(world, number, body):
    return _fakegh(world, ["pr", "comment", number, "--body", body]).stdout.strip()


def _unhandled_lines(world):
    return [l for l in world["unhandled_path"].read_text(encoding="utf-8").splitlines() if l.strip()]


def test_pr_comment_prints_a_parseable_url_with_the_stored_integer_id(tmp_path):
    import re
    world = _make_repo_world(tmp_path)
    _seed_pr(world)
    out = _pr_comment(world, "101", "hello")
    m = re.search(_COMMENT_ID_RE, out)
    assert m, "no comment URL on stdout: %r" % out
    cid = int(m.group(1))
    assert cid > 0
    stored = _read_state(world["state_path"])["prs"]["101"]["comments"]
    assert [c["id"] for c in stored] == [cid] and stored[0]["body"] == "hello"
    assert _unhandled_lines(world) == []


def test_pr_comment_ids_are_unique_and_increasing_across_prs(tmp_path):
    import re
    world = _make_repo_world(tmp_path)
    _seed_pr(world, "101"); _seed_pr(world, "102")
    ids = []
    for n, body in (("101", "a"), ("102", "b"), ("101", "c"), ("102", "d")):
        ids.append(int(re.search(_COMMENT_ID_RE, _pr_comment(world, n, body)).group(1)))
    assert ids == sorted(set(ids)) and len(ids) == 4
    # legacy state: a bare-string comment and an id-less dict still yield a positive id
    state = _read_state(world["state_path"])
    state["prs"]["103"] = {"head": "h", "base": "main", "state": "OPEN", "title": "t", "body": "",
                           "comments": ["legacy", {"body": "idless", "assoc": "OWNER"}]}
    world["state_path"].write_text(json.dumps(state), encoding="utf-8")
    assert int(re.search(_COMMENT_ID_RE, _pr_comment(world, "103", "new")).group(1)) > max(ids)
    assert _unhandled_lines(world) == []


def _read_comments_page(world, number, per_page, page):
    ep = "repos/%s/issues/%s/comments?per_page=%s&page=%s" % (world["repo"], number, per_page, page)
    return json.loads(_fakegh(world, ["api", ep]).stdout)


def test_pr_comments_paged_read_honours_page_and_per_page(tmp_path):
    world = _make_repo_world(tmp_path)
    _seed_pr(world)
    for i in range(5):
        _pr_comment(world, "101", "body %d" % i)
    pages = [_read_comments_page(world, "101", 2, n) for n in (1, 2, 3, 4)]
    assert [len(p) for p in pages] == [2, 2, 1, 0]
    flat = [row for p in pages for row in p]
    assert [r["body"] for r in flat] == ["body %d" % i for i in range(5)]
    ids = [r["id"] for r in flat]
    assert ids == sorted(set(ids)) and all(isinstance(i, int) for i in ids)
    assert all({"id", "body", "user", "author_association"} <= set(r) for r in flat)  # real REST keys
    assert len(_read_comments_page(world, "101", 100, 1)) == 5  # _find_evidence_marker's gesture
    assert _read_comments_page(world, "999", 100, 1) == []
    assert _unhandled_lines(world) == []


def test_find_evidence_marker_pages_to_termination_against_the_fake(tmp_path):
    import importlib.util
    scripts = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        spec = importlib.util.spec_from_file_location("work_875", scripts / "work.py")
        work = importlib.util.module_from_spec(spec); spec.loader.exec_module(work)
    finally:
        sys.path.remove(str(scripts))
    world = _make_repo_world(tmp_path)
    _seed_pr(world)
    state = _read_state(world["state_path"])
    marker = "<!-- sigma-review-evidence:abc123 -->"
    state["prs"]["101"]["comments"] = [
        {"id": i, "body": (marker if i == 230 else "c%d" % i), "assoc": "OWNER"} for i in range(1, 251)]
    world["state_path"].write_text(json.dumps(state), encoding="utf-8")

    def run(cwd, argv):
        assert argv[0] == "gh"
        return _fakegh(world, argv[1:]).stdout

    found = work._find_evidence_marker(run, str(world["clone_dir"]), 101, "abc123")
    assert found is not None and found["id"] == 230
    assert work._find_evidence_marker(run, str(world["clone_dir"]), 101, "nope") is None
    assert _unhandled_lines(world) == []


def test_issue_comments_read_does_not_swallow_a_field_post(tmp_path):
    """`gh api <endpoint> -f body=x` is a POST (real gh infers it from fields); the fake's
    comments READ branch must not answer it (#875), and since #878 the POST branch records it:
    the product's post-merge issue note is sent exactly this way."""
    world = _make_repo_world(tmp_path)
    _seed_pr(world)
    ep = "repos/%s/issues/999/comments" % world["repo"]                    # neither a PR nor an issue
    r = _fakegh(world, ["api", ep, "-f", "body=x"], check=False)
    assert r.returncode != 0 and r.stdout.strip() != "[]"                  # issue 999 does not exist
    assert len(_unhandled_lines(world)) == 0                                # modelled: a 404, not a gap


def test_a_pr_comment_posted_over_rest_is_read_back_with_id_and_owner_association(tmp_path):
    """#895 4b-2: post_review's `gh api .../issues/<pr>/comments --method POST` lands in the SAME store
    `pr_rest_comments` serves, with an id and the OWNER association, so a posted sigma:approve/sigma:block
    is readable by the review gate; the reply carries {id, html_url}."""
    world = _make_repo_world(tmp_path)
    _seed_pr(world)
    ep = "repos/%s/issues/101/comments" % world["repo"]
    reply = json.loads(_fakegh(world, ["api", ep, "--method", "POST", "-f", "body=sigma:approve"]).stdout)
    assert reply["id"] > 0 and reply["html_url"].endswith("#issuecomment-%d" % reply["id"])
    back = _read_comments_page(world, "101", 100, 1)
    assert [(c["id"], c["body"], c["author_association"]) for c in back] == [(reply["id"], "sigma:approve", "OWNER")]
    assert _unhandled_lines(world) == []


def test_issue_comment_field_post_without_method_is_recorded(tmp_path):
    """#878: the post-merge note is `gh api <issue comments> -f body=...` with no --method. It must
    land on the issue (modelled), and a bare GET of the same endpoint must still read, not post."""
    world = _make_repo_world(tmp_path)
    state = _read_state(world["state_path"])
    state["issues"]["7"] = {"labels": [], "state": "open", "title": "t", "body": "", "assignees": []}
    world["state_path"].write_text(json.dumps(state), encoding="utf-8")
    ep = "repos/%s/issues/7/comments" % world["repo"]
    r = _fakegh(world, ["api", ep, "-f", "body=hello"])
    assert json.loads(r.stdout) == {"body": "hello"}
    assert _read_state(world["state_path"])["issues"]["7"]["comments"] == ["hello"]
    assert _unhandled_lines(world) == []
    assert json.loads(_fakegh(world, ["api", ep]).stdout) == []             # a plain read posts nothing
    assert _read_state(world["state_path"])["issues"]["7"]["comments"] == ["hello"]
