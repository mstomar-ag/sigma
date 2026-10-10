"""Support for the #935 REST-merge tests. No test lives here.

Holds a purpose-built fake `gh` (own script, a real local clone plus bare remote, real two-parent merge commits,
`auto_merge` in its PR object), an unmatched-call-refusing runner, the matcher that sees a REST merge, and the chat-spy
installer. The older fakes are left alone: the public-bootstrap one merges by fast-forward only and exits 1 on a PUT."""
import json
import os
import pathlib
import re
import subprocess
import sys

REPO = "acme/app"

_FAKE = r'''
import json, os, re, subprocess, sys
STATE, UNMATCHED = os.environ["FAKE_GH_STATE"], os.environ["FAKE_GH_UNMATCHED"]
state = json.load(open(STATE))
argv = sys.argv[1:]

def refuse(note):
    with open(UNMATCHED, "a") as f:
        f.write(json.dumps({"argv": argv, "note": note}) + "\n")
    sys.stderr.write("FAKE-GH-UNMATCHED " + " ".join(argv) + "\n")
    sys.exit(1)

def git(*a, check=False):
    p = subprocess.run(["git", "--git-dir", state["remote_git_dir"], *a], capture_output=True, text=True)
    return p

def rev(ref):
    p = git("rev-parse", "--verify", ref)
    return p.stdout.strip() if p.returncode == 0 else None

def fields():
    out, i = {}, 0
    while i < len(argv):
        if argv[i] in ("-f", "-F") and i + 1 < len(argv):
            k, _, v = argv[i + 1].partition("="); out[k] = v; i += 2
        else:
            i += 1
    return out

def method():
    for i, a in enumerate(argv):
        if a in ("--method", "-X") and i + 1 < len(argv):
            return argv[i + 1].upper()
    return "GET"

def fail(code, msg):
    sys.stderr.write("HTTP %d: %s\n" % (code, msg)); sys.exit(1)

if len(argv) < 2 or argv[0] != "api":
    refuse("not a gh api call")
ep, repo = argv[1], state["repo"]
m = re.match(r"^repos/%s/pulls/(\d+)$" % re.escape(repo), ep)
if m and method() == "GET":
    n = m.group(1); pr = state["prs"].get(n) or fail(404, "Not Found")
    head = rev("refs/heads/" + pr["head"]) or ""
    merged = pr["state"] == "MERGED"
    print(json.dumps({"number": int(n), "state": "closed" if merged else "open", "merged": merged,
                      "merged_at": "2026-01-02T00:00:00Z" if merged else None,
                      "merge_commit_sha": pr.get("merge_commit_sha"),
                      "auto_merge": pr.get("auto_merge"),
                      "head": {"ref": pr["head"], "sha": head}, "base": {"ref": pr["base"]}}))
    sys.exit(0)
m = re.match(r"^repos/%s/pulls/(\d+)/merge$" % re.escape(repo), ep)
if m and method() == "PUT":
    n = m.group(1); pr = state["prs"].get(n) or fail(404, "Not Found")
    f = fields(); how = f.get("merge_method", "merge")
    if how not in ("merge", "squash"):
        refuse("merge method not modelled: " + how)
    head, base = rev("refs/heads/" + pr["head"]), rev("refs/heads/" + pr["base"])
    if not head or not base:
        fail(404, "Not Found")
    if f.get("sha") and f["sha"] != head:
        fail(409, "Head branch was modified. Review and try the merge again.")
    t = git("merge-tree", "--write-tree", base, head)
    if t.returncode != 0:
        fail(405, "Pull Request is not mergeable")
    tree = t.stdout.split()[0]
    parents = ["-p", base, "-p", head] if how == "merge" else ["-p", base]
    c = git("commit-tree", tree, *parents, "-m", "Merge pull request #%s" % n)
    if c.returncode != 0:
        fail(500, "commit-tree failed")
    sha = c.stdout.strip()
    git("update-ref", "refs/heads/" + pr["base"], sha)
    pr["state"], pr["merge_commit_sha"] = "MERGED", sha
    json.dump(state, open(STATE, "w"))
    print(json.dumps({"sha": sha, "merged": True, "message": "Pull Request successfully merged"}))
    sys.exit(0)
m = re.match(r"^repos/%s/commits/([0-9a-f]{40})$" % re.escape(repo), ep)
if m and method() == "GET":
    p = git("rev-list", "--parents", "-n", "1", m.group(1))
    if p.returncode != 0:
        fail(404, "Not Found")
    parts = p.stdout.split()
    print(json.dumps({"sha": parts[0], "parents": [{"sha": x} for x in parts[1:]]}))
    sys.exit(0)
refuse("unmodelled call")
'''


def _git(args, cwd, env):
    proc = subprocess.run(["git", *args], cwd=str(cwd), env=env, capture_output=True, text=True)
    assert proc.returncode == 0, "git %s failed: %s" % (args, proc.stderr)
    return proc.stdout.strip()


def git_has_merge_tree():
    """`git merge-tree --write-tree` needs git 2.38; the fake cannot build a merge commit without it."""
    p = subprocess.run(["git", "--version"], capture_output=True, text=True)
    nums = re.findall(r"\d+", p.stdout)[:2]
    return tuple(int(x) for x in nums) >= (2, 38)


def make_world(root, repo=REPO):
    """A bare remote, a clone with one seed commit on main, and the fake gh script. Identity comes from env only."""
    root = pathlib.Path(root)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(root), "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_CONFIG_SYSTEM": os.devnull, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    remote, clone = root / "remote.git", root / "clone"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)], env=env,
                   capture_output=True, check=True)
    subprocess.run(["git", "clone", str(remote), str(clone)], env=env, capture_output=True, check=True)
    (clone / "seed.txt").write_text("seed\n")
    _git(["add", "-A"], clone, env)
    _git(["commit", "-m", "seed"], clone, env)
    _git(["push", "-u", "origin", "main"], clone, env)
    script = root / "fake_gh.py"
    script.write_text(_FAKE)
    world = {"root": root, "env": env, "remote": remote, "clone": clone, "script": script, "repo": repo,
             "state": root / "state.json", "unmatched": root / "unmatched.jsonl"}
    world["unmatched"].write_text("")
    world["state"].write_text(json.dumps({"repo": repo, "remote_git_dir": str(remote), "prs": {}}))
    return world


def open_pr(world, number, head, files, base="main", auto_merge=None):
    """Push `head` (cut from base) with one new commit per file, and register PR `number`."""
    clone, env = world["clone"], world["env"]
    _git(["checkout", "-q", "-B", head, "origin/" + base], clone, env)
    for name, text in files.items():
        (clone / name).write_text(text)
        _git(["add", "-A"], clone, env)
        _git(["commit", "-m", "add " + name], clone, env)
    _git(["push", "-q", "origin", head], clone, env)
    _git(["checkout", "-q", "main"], clone, env)
    st = json.loads(world["state"].read_text())
    st["prs"][str(number)] = {"head": head, "base": base, "state": "OPEN", "auto_merge": auto_merge}
    world["state"].write_text(json.dumps(st))


def advance_base(world, name, text, base="main"):
    """A commit on base after the PR branched, so a merge is a true two-parent merge, not a fast-forward."""
    clone, env = world["clone"], world["env"]
    _git(["checkout", "-q", base], clone, env)
    (clone / name).write_text(text)
    _git(["add", "-A"], clone, env)
    _git(["commit", "-m", "advance " + name], clone, env)
    _git(["push", "-q", "origin", base], clone, env)


def remote_rev(world, ref):
    return _git(["--git-dir", str(world["remote"]), "rev-parse", ref], world["root"], world["env"])


def remote_parents(world, sha):
    out = _git(["--git-dir", str(world["remote"]), "rev-list", "--parents", "-n", "1", sha], world["root"],
               world["env"])
    return out.split()[1:]


def fake_proc(world, args):
    """Run the fake once; a CompletedProcess (never raises)."""
    env = dict(world["env"], FAKE_GH_STATE=str(world["state"]), FAKE_GH_UNMATCHED=str(world["unmatched"]))
    return subprocess.run([sys.executable, str(world["script"]), *args], env=env, capture_output=True, text=True)


def fake_run(world):
    """A `run(args) -> stdout` for `gh_api` that goes through the fake; a non-zero exit raises like `gh` does."""
    def run(args):
        p = fake_proc(world, list(args))
        if p.returncode != 0:
            raise RuntimeError("gh " + " ".join(args) + " failed: " + p.stderr.strip())
        return p.stdout
    return run


def unmatched_calls(world):
    return [json.loads(l)["argv"] for l in world["unmatched"].read_text().splitlines() if l.strip()]


class UnmatchedCall(RuntimeError):
    """An unmatched call, raised as a failed `gh` would be (non-zero), never answered with an empty string."""


def strict_runner(handlers):
    """Same `(cwd, argv) -> stdout` contract as the older `_runner`: first substring match wins, an Exception
    response raises. Unlike it, an unmatched call is recorded in `.unmatched` and raises `UnmatchedCall`."""
    calls, unmatched = [], []

    def run(cwd, argv):
        line = " ".join(str(a) for a in argv)
        calls.append(line)
        for token, resp in handlers:
            if token in line:
                if isinstance(resp, Exception):
                    raise resp
                return resp(line) if callable(resp) else resp
        unmatched.append(line)
        raise UnmatchedCall("unmatched call: " + line)

    run.calls, run.unmatched = calls, unmatched
    return run


_REST_MERGE = re.compile(r"pulls/\d+/merge\b")


def rest_merges(calls):
    """Every call that lands a pull request, by any spelling: the CLI `pr merge`, a local `git merge`, or the REST
    `PUT pulls/<n>/merge` (either flag spelling). A GET of the same path is a read and is not a merge."""
    out = []
    for c in calls:
        if "pr merge" in c or c.startswith("git merge"):
            out.append(c)
        elif _REST_MERGE.search(c) and re.search(r"(--method[ =]|-X[ =]?)PUT\b", c):
            out.append(c)
    return out


def spy_landing(monkeypatch, verify_merge, gh_api=None):
    """Record, and never perform, any landing. Sees the four verify_merge names, `gh_api.merge_pr` and
    `merge_pr_pinned` when a `gh_api` is given, and -- the seam every module instance shares, so a REST merge from an
    entry nobody named still shows -- any `gh` argv that reaches `subprocess.run` (answered as a failed gh)."""
    calls = []

    def named(name):
        def fn(*a, **k):
            calls.append(name)
            return {"outcome": "created", "number": 1, "why": "stub -- must never be reached"}
        return fn

    for name in ("ensure_landing_pr", "merge_pr", "verify_and_offer_merge", "_interactive_decide"):
        monkeypatch.setattr(verify_merge, name, named(name))
    if gh_api is not None:
        monkeypatch.setattr(gh_api, "merge_pr", named("gh_api.merge_pr"))
        monkeypatch.setattr(gh_api, "merge_pr_pinned", named("gh_api.merge_pr_pinned"))
    real = subprocess.run

    def argv_spy(args, *a, **k):
        if isinstance(args, (list, tuple)) and args and str(args[0]) == "gh":
            calls.append(" ".join(str(x) for x in args))
            return subprocess.CompletedProcess(args, 1, "", "spy: a landing is never performed")
        return real(args, *a, **k)

    monkeypatch.setattr(subprocess, "run", argv_spy)
    return calls
