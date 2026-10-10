#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Scan and ratchet tracked GitHub, git, and destructive filesystem writes.

USAGE: write_surface.py scan REPO --json OUT | render INVENTORY
EXIT: 0 = inventory rendered; 1 = ratchet findings; 2 = bad arguments.
"""

import argparse
import ast
import json
import shlex
import subprocess
from pathlib import Path


RISK = {"gh-issue": "medium", "gh-pr": "medium", "gh-label": "medium",
        "gh-project": "medium", "gh-api-write": "medium", "graphql-mutation": "medium",
        "git-push": "high", "git-destructive": "high", "git-ref-write": "high", "fs-remove": "high",
        "fs-rmtree": "high", "fs-write": "medium", "network-post": "high"}
_GH_ACTIONS = {"issue": {"close", "reopen", "edit", "comment", "create", "delete", "transfer", "lock"},
               "pr": {"create", "merge", "close", "comment", "review", "edit", "ready"},
               "label": {"create", "edit", "delete"},
               "project": {"item-edit", "item-add", "item-archive", "item-delete", "field-create", "create", "link", "copy", "edit", "delete"}}
_REMOVE_METHODS = {"unlink", "rmdir"}
_PUSH_DESTRUCTIVE_FLAGS = {"-d", "--delete", "-f", "--force", "--force-with-lease",
                           "--force-if-includes", "--mirror", "--prune"}


def _push_destructive(args):          # tokens after `push`
    return any(a in _PUSH_DESTRUCTIVE_FLAGS or a.startswith(("--delete=", "--force-with-lease="))
               for a in args)


def _lead(node):                      # literal text an argv element STARTS with; never name-resolved
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return _lead(node.values[0]) if node.values else ""
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mod, ast.Add)):
        return _lead(node.left)
    return None


def _refspec_destructive(call):       # colon-empty `:dst` deletes dst; `+src:dst` forces
    leads = []
    for arg in list(call.args) + [kw.value for kw in call.keywords]:
        leads.extend(_lead(e) for e in (arg.elts if isinstance(arg, (ast.List, ast.Tuple)) else [arg]))
    return any(t and t.startswith((":", "+")) for t in leads)


def _rest_verb(token):                # `-X DELETE`, `--method=DELETE`, `-XDELETE` -> the verb
    if token.startswith(("-x", "--method=")):
        return token.rsplit("=", 1)[-1].removeprefix("-x")
    return token


_GH_API_WRITES = {"comment_issue", "add_labels", "remove_label", "create_issue", "close_issue", "edit_issue", "add_assignees",
                  "create_pr", "merge_pr", "merge_pr_pinned", "create_pr_nondraft"}


def _is_gh_api_write_call(func):
    if not (isinstance(func, ast.Attribute) and func.attr in _GH_API_WRITES):
        return False
    owner = func.value
    if isinstance(owner, ast.Call):       # _load("gh_api").merge_pr(...)
        return any(isinstance(a, ast.Constant) and a.value == "gh_api" for a in owner.args)
    return _call_name(owner).rsplit(".", 1)[-1] == "gh_api"
_WRITE_METHODS = {"write_text", "write_bytes", "mkdir", "touch"}


def _metadata(path, function, rule):
    """Return the gate and risk for this specific call site, never just its file."""
    site = (path, function, rule)
    known = {
        ("skills/sigma-loop/scripts/feature_propagate.py", "_write_remote", "gh-api-write"):
            ("granted verdict", "high"),
        ("skills/sigma-status/scripts/merge_queue_enable.py", "patch_auto_merge", "gh-api-write"):
            ("exact --yes-enable-merge-queue admin consent", "high"),
        ("skills/sigma-status/scripts/merge_queue_enable.py", "create_merge_queue_ruleset", "gh-api-write"):
            ("exact --yes-enable-merge-queue admin consent", "high"),
        ("skills/sigma-loop/scripts/work.py", "merge_design", "gh-pr"):
            ("work.enabled; work.auto_merge != off", "high"),
        ("skills/sigma-loop/scripts/work.py", "close_design", "gh-pr"):
            ("ungated", "high"),
        ("skills/sigma-loop/scripts/work.py", "merge", "gh-pr"):
            ("work.enabled; work.auto_merge != off; merge rights; fresh verify evidence and CLEAN PR", "high"),
        ("skills/sigma-loop/scripts/work.py", "finish", "gh-pr"):
            ("work.enabled; confirmed merged PR", "high"),
        ("skills/sigma-loop/scripts/work.py", "_delete_remote_branch", "gh-api-write"):
            ("work.enabled; merged PR cleanup; non-empty goal prefix; never base/default branch", "high"),
        ("skills/sigma-loop/scripts/channel_notify.py", "_real_post", "network-post"):
            ("http(s) loopback URL; allow_remote_webhook is exactly true for remote delivery", "high"),
        ("skills/sigma-loop/scripts/work.py", "_close_issue_the_base_cannot", "gh-api-write"):
            ("work.enabled; base branch cannot close the issue", "high"),
        ("skills/sigma-loop/scripts/feature_sync.py", "recover", "fs-remove"):
            ("explicit `feature_sync.py recover --discard`; renames Sigma's own recovery copy of the "
             "registry sheet aside, never deletes it", "medium"),
        ("skills/sigma-loop/scripts/feature_upkeep_resolution.py", "post_comment", "gh-api-write"):
            ("upkeep.enabled and a finding the engine filed; integer finding id and explicit owner/name repository; "
             "text passes the wording check; one comment, never edited or closed here", "medium"),
        ("skills/sigma-loop/scripts/feature_upkeep_resolution.py", "prune", "fs-remove"):
            ("upkeep.enabled caller passes keep_days; removes only record-shaped files in the engine's own resolutions "
             "store, never a symlink, never outside it", "high"),
        ("tools/readiness/baseline.py", "snapshot", "fs-write"):
            ("explicit snapshot command; empty destination", "medium"),
        ("tools/readiness/baseline.py", "snapshot", "git-destructive"):
            ("explicit snapshot command; empty destination; detached push-disabled clone", "medium"),
        ("tools/readiness/egress_capture.py", "main", "fs-write"):
            ("explicit summarize command; caller-supplied JSON path", "medium"),
        ("tools/readiness/exposure_scan.py", "_write", "fs-write"):
            ("explicit exposure scan; caller-supplied evidence path", "medium"),
        ("tools/readiness/exposure_scan.py", "main", "fs-write"):
            ("explicit tracked scan with --propose; caller-supplied draft path; hashes only", "medium"),
        ("tools/readiness/exposure_scan.py", "scan_refs", "git-destructive"):
            ("explicit refs scan; local tag listing is read-only", "low"),
        ("tools/readiness/review_units.py", "main", "fs-write"):
            ("explicit --json PATH; caller-supplied output path", "medium"),
        ("tools/readiness/injection_drill.py", "_write_json", "fs-write"):
            ("explicit file subcommand; caller-supplied snapshot path that must not exist; "
             "validated sigma-drill- repository; declared --max-usd", "medium"),
        ("tools/readiness/injection_drill.py", "file_payloads", "gh-api-write"):
            ("explicit file subcommand; OWNER/sigma-drill- name fullmatch; repository read back and "
             "must be private; declared --max-usd; never run by Sigma", "high"),
        ("tools/readiness/shared_paths.py", "main", "fs-write"):
            ("explicit scan command; caller-supplied --json, --table and --fixture-out paths", "medium"),
        ("tools/readiness/shared_paths.py", "_prepare", "fs-write"):
            ("explicit scan with samples; scratch repository inside a fresh temporary directory; fake HOME",
             "medium"),
        ("tools/readiness/shared_paths.py", "registry_probe", "fs-write"):
            ("explicit scan with samples; scratch directory inside the temporary work directory", "medium"),
        ("tools/readiness/shared_paths.py", "sample_run", "fs-write"):
            ("explicit scan with samples; fake HOME inside a fresh temporary directory", "medium"),
        ("tools/readiness/shared_paths.py", "sample_run", "fs-rmtree"):
            ("explicit scan with samples; removes only the temporary directory it created", "high"),
        ("tools/readiness/mutation_sample.py", "_write", "fs-write"):
            ("explicit sample command; caller-supplied --out evidence path", "medium"),
        ("tools/readiness/mutation_sample.py", "_write", "fs-remove"):
            ("explicit sample command; replaces only its own temporary file beside --out", "medium"),
        ("tools/readiness/mutation_sample.py", "_restore", "fs-remove"):
            ("explicit sample command; deletes only mutmut's cache and coverage data inside the clean "
             "frozen clone it was given, never a tracked file", "medium"),
        ("tools/readiness/bench_tasks.py", "_verify_command", "fs-write"):
            ("explicit verify --json PATH; caller-supplied output path", "medium"),
        ("tools/readiness/bench_tasks.py", "build_manifest", "fs-write"):
            ("explicit build-manifest command; manifest.json beside the task directories only", "medium"),
        ("tools/readiness/bench_tasks.py", "export_tree", "fs-write"):
            ("explicit verify --external or hidden-from-pr (a fresh directory under the caller-supplied --scratch path) "
             "or materialize (the task's own git-ignored repo/); one commit fetched read-only, no history kept",
             "medium"),
        ("tools/readiness/bench_tasks.py", "harness_accepts", "fs-write"):
            ("check and verify; stand-in directories inside a fresh temporary directory", "medium"),
        ("tools/readiness/bench_tasks.py", "hidden_from_pr", "fs-write"):
            ("explicit hidden-from-pr command; one hidden bundle under the hidden root, outside the "
             "repository", "medium"),
        ("tools/readiness/bench_tasks.py", "__init__", "fs-write"):
            ("explicit verify; a pass-through launcher script inside a fresh temporary directory", "medium"),
        ("tools/readiness/bench_tasks.py", "_write_manifest", "fs-write"):
            ("check and verify with a hidden root write a copy of manifest.json inside a fresh temporary directory; "
             "the explicit lock command rewrites the manifest's environment record in place", "medium"),
        ("tools/readiness/bench_tasks.py", "write_lock", "fs-write"):
            ("explicit lock command; environment.lock beside the manifest and the manifest's environment record; "
             "the environment is built under the caller-supplied --scratch path", "medium"),
        ("tools/readiness/bench_tasks.py", "harness_validates", "fs-write"):
            ("check and verify with a hidden root; stand-in directories inside a fresh temporary directory",
             "medium"),
        ("tools/readiness/bench_tasks.py", "materialize_task", "fs-rmtree"):
            ("explicit materialize command; removes only the tree it just fetched, when its digest is not the "
             "recorded one", "high"),
        ("tools/readiness/bench_tasks.py", "materialize_task", "fs-write"):
            ("explicit materialize command; the task's own git-ignored repo/ (refused if it exists) and, with "
             "--record-digest, its fetch.json", "medium"),
        ("tools/readiness/bench_tasks.py", "verify_external", "fs-write"):
            ("explicit verify --external; fresh directories under the caller-supplied --scratch path", "medium"),
        ("tools/readiness/bench_tasks.py", "seal", "fs-write"):
            ("explicit seal command; the named task's task.json only", "medium"),
        ("tools/readiness/blast_radius.py", "main", "fs-write"):
            ("explicit baseline or assert command; caller-supplied --json path; reads GitHub over REST only",
             "medium"),
        ("tools/readiness/blast_radius_drive.py", "drive", "fs-write"):
            ("explicit drive command; refuses an existing workdir; caller-supplied --baseline-out path", "medium"),
        ("tools/readiness/blast_radius_drive.py", "drive", "gh-issue"):
            ("explicit drive command; OWNER/sigma-drill- fullmatch; repository read back and must be private, "
             "not a fork or archived; refuses under CI; files one goal issue; never run by Sigma", "high"),
        ("tools/readiness/blast_radius_drive.py", "drive", "gh-pr"):
            ("explicit drive command; same repository checks; one approve comment on the goal's own PR",
             "high"),
        ("tools/readiness/blast_radius_drive.py", "drive", "git-push"):
            ("explicit drive command; same repository checks; origin guard; one setup push only when the "
             "repository is empty, never forced", "high"),
        ("tools/readiness/seed_defects.py", "apply", "fs-write"):
            ("explicit apply command; manifest.json beside the patches, outside the clone; detached clean clone only",
             "medium"),
    }
    if site in known:
        return known[site]
    if path == "skills/sigma-loop/scripts/feature_rebase.py":
        gate = "work.rebase_upkeep"
    elif path == "skills/sigma-loop/scripts/sources.py":
        gate = "discovery.source == github; board writes require project.enabled"
    elif path == "skills/sigma-rebase/scripts/verify_merge.py":
        gate = "human input() confirmation"
    elif path == "skills/sigma-loop/scripts/feature_backup.py":
        gate = "upkeep.enabled; the caller that decides to back up holds the gate (the engine's gate query), and restore and prune query it themselves; no entry point is registered"
    else:
        gate = "ungated"
    risk = RISK[rule]
    if site in {
        ("skills/sigma-rebase/scripts/verify_merge.py", "merge_pr", "gh-pr"),
        ("skills/sigma-loop/scripts/sources.py", "complete", "gh-issue"),
        ("skills/sigma-loop/scripts/sources.py", "release", "gh-issue"),
    }:
        risk = "high"
    return gate, risk


def _shell_rules(line):
    """Classify one executable shell command; comments and quoted examples are not commands."""
    if not line.strip() or line.lstrip().startswith("#"):
        return set()
    try:
        words = shlex.split(line, comments=True)
    except ValueError:
        return set()
    if not words:
        return set()
    if words[:3] == ["gh", "label", "delete"]:
        return {"gh-label"}
    if len(words) >= 3 and words[:2] == ["gh", "issue"] and words[2] in _GH_ACTIONS["issue"]:
        return {"gh-issue"}
    if len(words) >= 3 and words[:2] == ["gh", "pr"] and words[2] in _GH_ACTIONS["pr"]:
        return {"gh-pr"}
    if len(words) >= 3 and words[:2] == ["gh", "project"] and words[2] in _GH_ACTIONS["project"]:
        return {"gh-project"}
    if words[:2] == ["gh", "api"] and any(_rest_verb(w.lower()) in {"post", "patch", "put", "delete"} for w in words):
        return {"gh-api-write"}
    if words[:2] == ["git", "push"]:
        if _push_destructive(words[2:]) or any(w.startswith((":", "+")) for w in words[2:]):
            return {"git-push", "git-destructive"}
        return {"git-push"}
    if words[:2] == ["git", "update-ref"]:
        if any(w in ("-d", "--delete") for w in words[2:]):
            return {"git-destructive"}
        return {"git-ref-write"}
    if words and words[0] == "git" and any(w in {"-D", "--hard", "remove", "rm", "tag"} for w in words[1:]):
        return {"git-destructive"}
    if words and words[0] == "rm" and any(w.startswith("-r") or w.startswith("-R") for w in words[1:]):
        return {"fs-rmtree"}
    return set()


def _call_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return (parent + "." if parent else "") + node.attr
    return ""


def _strings(node, values):
    """Literal strings reachable from an actual call argument, never comments/docstrings."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.Name):
        return values.get(node.id, [])
    found = []
    for child in ast.iter_child_nodes(node):
        found.extend(_strings(child, values))
    return found


def _values(tree):
    values = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            if node.value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    values[target.id] = _strings(node.value, values)
    return values


def _rules_for_call(node, values):
    name = _call_name(node.func)
    strings = [s for arg in list(node.args) + [kw.value for kw in node.keywords]
               for s in _strings(arg, values)]
    tokens = [s.lower() for s in strings]
    rules = set()
    command_call = name in {"subprocess.run", "subprocess.Popen", "run", "git", "_run", "_run_gh", "_retry_gh", "_gh_json"} or name.endswith("._run")
    command = tokens[:]
    if command_call:
        if "gh" in command:
            command = command[command.index("gh") + 1:]
        if name.endswith("._run") or name in {"_run", "_run_gh", "_gh_json"}:
            command = tokens
        if len(command) >= 2 and command[0] in _GH_ACTIONS and command[1] in _GH_ACTIONS[command[0]]:
            rules.add("gh-" + ("pr" if command[0] == "pr" else command[0]))
        if command and command[0] == "api" and any(_rest_verb(x) in {"post", "patch", "put", "delete"} for x in command):
            rules.add("gh-api-write")
    if ("graphql" in command or name.endswith("_graphql")) and any("mutation" in s.lower() for s in strings):
        rules.add("graphql-mutation")
    git_runner = name in {"git", "gitc", "_git", "_run_git"}
    git = []
    if command_call and "git" in tokens:
        git = tokens[tokens.index("git") + 1:]
    elif git_runner:
        git = tokens
    elif command_call and command and command[0] in {"branch", "reset", "worktree", "tag", "update-ref"}:
        # These verbs occur only in git's CLI grammar; `run` is the repository's git/gh runner.
        git = command
    if git:
        if "push" in git:
            rules.add("git-push")
            if _push_destructive(git[git.index("push") + 1:]) or _refspec_destructive(node):
                rules.add("git-destructive")
        if any(x in git for x in ("-d", "-D", "--hard", "remove", "rm", "tag", "--force", "--force-with-lease")) or any(x.startswith("--force-with-lease=") for x in git):
            rules.add("git-destructive")
        if "update-ref" in git:
            if any(x in ("-d", "--delete") for x in git):
                rules.add("git-destructive")
            else:
                rules.add("git-ref-write")      # a plain update-ref creates or moves a ref (#960)
    if _is_gh_api_write_call(node.func):
        rules.add("gh-api-write")
    if name == "shutil.rmtree":
        rules.add("fs-rmtree")
    elif name.startswith("os.") and name.split(".")[-1] in {"unlink", "remove", "rmdir", "replace", "rename"}:
        rules.add("fs-remove")
    elif name.split(".")[-1] in _REMOVE_METHODS:
        rules.add("fs-remove")
    elif name.startswith("os.") and name.split(".")[-1] in {"mkdir", "makedirs", "link", "symlink"}:
        rules.add("fs-write")
    elif name.split(".")[-1] in _WRITE_METHODS:
        rules.add("fs-write")
    return rules


def _functions(path, text):
    if path.suffix != ".py":
        return [(0, len(text.splitlines()) + 1, "<script>")]
    tree = ast.parse(text, filename=str(path))
    found = [(0, len(text.splitlines()) + 1, "<module>")]
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.append((node.lineno, getattr(node, "end_lineno", node.lineno), node.name))
    return found


def scan_paths(root, paths):
    root = Path(root)
    found = []
    for path in paths:
        path = Path(path)
        text = path.read_text(encoding="utf-8", errors="replace")
        groups = {}
        owners = _functions(path, text)
        if path.suffix == ".sh":
            for line in text.splitlines():
                for rule in _shell_rules(line):
                    key = (path.relative_to(root).as_posix(), "<script>", rule)
                    groups[key] = groups.get(key, 0) + 1
        else:
            tree = ast.parse(text, filename=str(path))
            values = _values(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                matching = [(start, name) for start, end, name in owners if start <= node.lineno <= end]
                function = max(matching, default=(0, "<module>"))[1]
                rules = _rules_for_call(node, values)
                if (path.relative_to(root).as_posix() == "skills/sigma-loop/scripts/channel_notify.py"
                        and function == "_real_post" and _call_name(node.func) in {"urllib.request.urlopen", "opener.open"}):
                    rules.add("network-post")
                for rule in rules:
                    key = (path.relative_to(root).as_posix(), function, rule)
                    groups[key] = groups.get(key, 0) + 1
        for (relpath, function, rule), count in groups.items():
            gate, risk = _metadata(relpath, function, rule)
            found.append({"path": relpath, "function": function, "rule": rule, "count": count,
                          "gate": gate, "risk": risk})
    return sorted(found, key=lambda item: (item["path"], item["function"], item["rule"]))


def _paths(root):
    root = Path(root)
    result = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True)
    if result.returncode:
        return [p for p in root.rglob("*") if p.suffix in {".py", ".sh"} and "tests" not in p.parts]
    return [root / raw.decode() for raw in result.stdout.split(b"\0") if raw and Path(raw.decode()).suffix in {".py", ".sh"} and not raw.decode().startswith("tests/")]


def ratchet(root, inventory):
    entries = json.loads(Path(inventory).read_text()).get("entries", [])
    messages = []
    for item in entries:
        label = str(item.get("path", "<unknown>")) + ":" + str(item.get("function", "<unknown>"))
        for field in ("path", "function", "rule", "count", "gate", "risk"):
            if field not in item or item[field] is None:
                messages.append("missing %s %s" % (field, label))
        if item.get("risk") not in {"low", "medium", "high"}:
            messages.append("invalid risk " + label)
        if not isinstance(item.get("count"), int) or item.get("count", 0) < 1:
            messages.append("invalid count " + label)
    actual = {(e["path"], e["function"], e["rule"]): e for e in scan_paths(root, _paths(root))}
    expected = {(e["path"], e["function"], e["rule"]): e for e in entries}
    for key, item in actual.items():
        if key not in expected:
            messages.append(f"new write site {item['path']}:{item['function']} {item['rule']} -- add it to docs/launch/write-surface.json with its gate")
    for key, item in expected.items():
        label = f"{item['path']}:{item['function']} {item['rule']}"
        if not item.get("gate"):
            messages.append(f"empty gate {label}")
        elif key not in actual:
            messages.append(f"stale entry {label}")
        elif item.get("count") != actual[key]["count"]:
            messages.append(f"count differs {label}")
    return messages


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("scan"); scan.add_argument("repo"); scan.add_argument("--json", required=True)
    render = sub.add_parser("render"); render.add_argument("inventory")
    check = sub.add_parser("check"); check.add_argument("repo"); check.add_argument("inventory")
    args = parser.parse_args(argv)
    if args.command == "scan":
        Path(args.json).write_text(json.dumps({"entries": scan_paths(args.repo, _paths(args.repo))}, indent=2) + "\n")
        return 0
    if args.command == "check":
        messages = ratchet(args.repo, args.inventory)
        print("\n".join(messages))
        return 1 if messages else 0
    entries = json.loads(Path(args.inventory).read_text()).get("entries", [])
    print("# Write-surface inventory\n\n"
          "The `check` command ratchets tracked Python and shell write sites. Control: in a temporary "
          "tracked shell file add `gh label delete legacy`, run `python3 tools/readiness/write_surface.py "
          "check . docs/launch/write-surface.json`, and see it fail as a new `gh-label` site; remove the "
          "line before the green run.\n\n| Path | Function | Rule | Count | Gate | Risk |\n|---|---|---|---:|---|---|")
    for e in entries: print("| {path} | {function} | {rule} | {count} | {gate} | {risk} |".format(**e))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
