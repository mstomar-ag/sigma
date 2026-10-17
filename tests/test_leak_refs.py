# SPDX-License-Identifier: MIT
"""`tools/leak_refs.py` (issue 282): the patterns-file scanner and dry-run rewriter for references
to a private repository in GitHub text.

OFFLINE. `gh` is always a fake: in-process tests inject `run=` (git still runs for real, in a
repository each test creates under tmp_path); CLI tests run `tools/leak_refs.py ...` as a child
from the repository root, under this pytest's interpreter (or `LEAK_REFS_TEST_PYTHON`), with a
`#!python` fake `gh` first on its PATH, built from the same fake functions below, so both paths
serve one fake. The only slug anywhere here is the
synthetic `acme-corp/secret-repo`; every patterns file is written by the test, and HOME is tmp_path,
so nothing here can read an owner's real patterns file.

NAMES. `test_cN_*` is control N (select one with `-k "test_cN_"`); every CLI test's name holds
`cli` (`-k cli` runs them all, which is what H0 runs under the system python3 through the
`LEAK_REFS_TEST_PYTHON` seam). Every expected hit reads `pattern 2`: line 1 of the patterns file is
a comment.
"""
import hashlib
import importlib.util
import inspect
import io
import json
import os
import pathlib
import re
import shlex
import stat
import subprocess
import sys
import uuid

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "leak_refs.py"
RUNBOOK = ROOT / "docs" / "publish-runbook.md"
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"
SLUG = "acme-corp/secret-repo"
REPO = "acme/demo"
URL12 = "https://github.com/" + SLUG + "/issues/12"
ISSUE_WORDS = "a private predecessor issue"
REPO_WORDS = "the private predecessor repository"
T0 = "2026-09-01T00:00:00Z"


# ------------------------------------------------------------------------------ loading

def _load_path(name, path):
    assert path.exists(), "%s is missing" % path.relative_to(ROOT)
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def lr():
    """A FRESH copy of the tool per test, so its cached consumer modules never leak across tests."""
    return _load_path("leak_refs_under_test", TOOL)


@pytest.fixture(scope="module")
def bc():
    """The real check-time consumer, for the cap-shift preconditions."""
    return _load_path("backlog_check_for_leak_refs", SCRIPTS / "backlog_check.py")


@pytest.fixture(autouse=True)
def pats(tmp_path, monkeypatch):
    """HOME in tmp_path, no patterns env, no git redirection, a git identity; the patterns file."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("SIGMA_LEAK_PATTERNS", "GIT_DIR", "GIT_WORK_TREE", "GIT_CEILING_DIRECTORIES",
                 "GIT_INDEX_FILE"):
        monkeypatch.delenv(name, raising=False)
    for name in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(name, "leak-refs test")
    for name in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(name, "leak-refs@example.invalid")
    path = tmp_path / "patterns.txt"
    path.write_text("# synthetic\n" + SLUG + "\n", encoding="utf-8")
    return path


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo)] + list(args), check=True, capture_output=True,
                          text=True).stdout


def mkrepo(tmp_path, name="repo"):
    repo = tmp_path / name
    repo.mkdir()
    git(repo, "init", "-q")
    return repo


def refused(rc, out, err, code):
    assert rc == 2, (rc, out, err)
    assert out == "", out
    assert "leak_refs: REFUSED [%s]" % code in err, err


def no_leak(*texts):
    for text in texts:
        assert "acme-corp" not in text and "secret-repo" not in text, text


# ------------------------------------------------------------------------------ the fake gh
# Plain functions, so the in-process fake calls them directly and the CLI fake is assembled from
# their source (`inspect.getsource`): ONE fake for both paths. `world` is JSON-serialisable; the CLI
# fake loads it from a file per call and saves it back, so writes persist across calls.

def _fake_gh_serve(world, argv, stdin):
    """Scripted failures first (`world["fail"]`: `match` is a substring of the argv plus stdin;
    `nth` an int, a list, or absent for always; `apply` performs the call before failing it)."""
    hay = " ".join(argv) + "\n" + (stdin or "")
    seen = world.setdefault("_seen", {})
    for i, spec in enumerate(world.get("fail", [])):
        if spec["match"] not in hay:
            continue
        seen[str(i)] = seen.get(str(i), 0) + 1
        nth = spec.get("nth")
        if nth is not None and seen[str(i)] != nth and not (isinstance(nth, list) and seen[str(i)] in nth):
            continue
        if spec.get("apply"):
            _fake_gh_do(world, argv, stdin)
        return spec.get("rc", 1), spec.get("out", ""), spec.get("err", "")
    return _fake_gh_do(world, argv, stdin)


def _fake_gh_do(world, argv, stdin):
    if argv[:1] != ["api"]:
        return 97, "", "UNEXPECTED argv %r" % (argv,)
    args = argv[1:]
    if args[:1] == ["graphql"]:
        return _fake_gh_graphql(world, stdin)
    endpoints = [a for a in args if a.startswith("repos/")]
    prefix = "repos/%s/" % world["repo"]
    if len(endpoints) != 1 or not endpoints[0].startswith(prefix):
        return 97, "", "UNEXPECTED endpoint %r" % (argv,)
    path, _, query = endpoints[0][len(prefix):].partition("?")
    if "-X" in args:
        return _fake_gh_write(world, args[args.index("-X") + 1], path, json.loads(stdin))
    if query:
        return _fake_gh_list(world, path, query)
    kind, obj = _fake_gh_find(world, path)
    if obj is None:
        return 1, "", "gh: Not Found (HTTP 404)"
    return 0, json.dumps(_fake_gh_view(world, kind, obj)), ""


def _fake_gh_count(world, number, override):
    table = world.get(override, {})
    if str(number) in table:
        return table[str(number)]
    return sum(1 for c in world["lists"]["issues/comments"]
               if c["issue_url"].endswith("/issues/%d" % number))


def _fake_gh_view(world, kind, obj):
    if kind == "item":
        view = dict(obj)
        view["comments"] = _fake_gh_count(world, obj["number"], "rest_comments")
        return view
    if kind == "review":
        return {"id": obj["databaseId"], "node_id": obj["id"], "body": obj["body"],
                "author_association": obj["authorAssociation"]}
    return obj


def _fake_gh_find(world, path):
    lists = world["lists"]
    m = re.fullmatch(r"issues/(\d+)", path)
    if m:
        return "item", next((o for o in lists["issues"] if o["number"] == int(m.group(1))), None)
    m = re.fullmatch(r"pulls/(\d+)/reviews/(\d+)", path)
    if m:
        return "review", next((r for r in world["reviews"] if r["pr"] == int(m.group(1))
                               and r["databaseId"] == int(m.group(2))), None)
    for pattern, key in ((r"issues/comments/(\d+)", "issues/comments"),
                         (r"pulls/comments/(\d+)", "pulls/comments"),
                         (r"comments/(\d+)", "comments"), (r"releases/(\d+)", "releases")):
        m = re.fullmatch(pattern, path)
        if m:
            return "row", next((o for o in lists[key] if o["id"] == int(m.group(1))), None)
    return None, None


def _fake_gh_list(world, path, query):
    if path not in world["lists"]:
        return 97, "", "UNEXPECTED list %s" % path
    params = dict(kv.split("=", 1) for kv in query.split("&") if "=" in kv)
    page, per = int(params.get("page", "1")), int(params.get("per_page", "30"))
    rows = world["lists"][path][(page - 1) * per:page * per]
    if path == "issues":
        rows = [_fake_gh_view(world, "item", r) for r in rows]
    gone = world.get("delete_after")
    if gone and gone["path"] == path and gone["page"] == page and not gone.get("done"):
        world["lists"][path].pop(gone["index"])        # a deletion behind the walk's cursor
        gone["done"] = True
    return 0, json.dumps(rows), ""


def _fake_gh_write(world, method, path, payload):
    kind, obj = _fake_gh_find(world, path)
    if obj is None:
        return 1, "HTTP/2.0 404 Not Found\n\n{}", "gh: Not Found (HTTP 404)"
    if method != ("PUT" if kind == "review" else "PATCH"):
        return 97, "", "UNEXPECTED method %s %s" % (method, path)
    for key, value in payload.items():
        obj[key] = value + world.get("mangle", "")
    world["writes"].append([method, path])
    return 0, ("HTTP/2.0 200 OK\nContent-Type: application/json\n\n"
               + json.dumps(_fake_gh_view(world, kind, obj))), ""


def _fake_gh_graphql(world, stdin):
    body = json.loads(stdin)
    m = re.match(r"# leak_refs:(\w+)", body.get("query", ""))
    if not m:
        return 97, "", "UNEXPECTED graphql without a name line"
    name = m.group(1)
    if name in world.get("gql", {}):
        return 0, json.dumps(world["gql"][name]), ""
    lists, delta = world["lists"], world.get("counts_delta", {})
    if name == "counts":
        issues = sum(1 for i in lists["issues"] if "pull_request" not in i)
        totals = {"issues": issues, "pullRequests": len(lists["issues"]) - issues,
                  "commitComments": len(lists["comments"]), "releases": len(lists["releases"])}
        data = {"repository": {k: {"totalCount": v + delta.get(k, 0)} for k, v in totals.items()}}
    elif name in ("items", "history"):
        ids = body["variables"]["ids"]
        nodes = [_fake_gh_node(world, i, name) for i in ids]
        if world.get("short") == name:
            nodes = nodes[:-1]
        if world.get("dup") == name and len(nodes) > 1:
            nodes[-1] = nodes[0]
        data = {"nodes": nodes}
    else:
        return 97, "", "UNEXPECTED graphql %s" % name
    data["rateLimit"] = world["rate"]
    return 0, json.dumps({"data": data}), ""


def _fake_gh_node(world, nid, name):
    override = world.get("node_override", {}).get(name, {})
    if nid in override:
        return override[nid]
    more = world.get("more", {})
    edits = {"pageInfo": {"hasNextPage": nid in more.get("revisions", [])},
             "nodes": world["edits"].get(nid, [])}
    for it in world["lists"]["issues"]:
        if it["node_id"] == nid:
            pr = "pull_request" in it
            node = {"__typename": "PullRequest" if pr else "Issue", "id": nid,
                    "comments": {"totalCount": _fake_gh_count(world, it["number"], "batch_comments")},
                    "userContentEdits": edits,
                    "timelineItems": {"pageInfo": {"hasNextPage": nid in more.get("renames", [])},
                                      "nodes": world["renames"].get(nid, [])}}
            if pr:
                node["reviews"] = {
                    "pageInfo": {"hasNextPage": nid in more.get("reviews", [])},
                    "nodes": [{"id": r["id"], "databaseId": r["databaseId"], "body": r["body"],
                               "authorAssociation": r["authorAssociation"]}
                              for r in world["reviews"] if r["pr"] == it["number"]]}
            return node
    for key, typename in (("issues/comments", "IssueComment"),
                          ("pulls/comments", "PullRequestReviewComment"),
                          ("comments", "CommitComment")):
        if any(o["node_id"] == nid for o in world["lists"][key]):
            return {"__typename": typename, "id": nid, "userContentEdits": edits}
    if any(r["id"] == nid for r in world["reviews"]):
        return {"__typename": "PullRequestReview", "id": nid, "userContentEdits": edits}
    return None


_FAKE_FUNCS = (_fake_gh_serve, _fake_gh_do, _fake_gh_count, _fake_gh_view, _fake_gh_find,
               _fake_gh_list, _fake_gh_write, _fake_gh_graphql, _fake_gh_node)

_FAKE_GH_MAIN = r'''
def _main():
    world_file, log_file = os.environ["FAKE_GH_WORLD"], os.environ["FAKE_GH_LOG"]
    argv = sys.argv[1:]
    stdin = sys.stdin.read() if "--input" in argv else None
    with open(world_file, encoding="utf-8") as f:
        world = json.load(f)
    rc, out, err = _fake_gh_serve(world, argv, stdin)
    with open(world_file, "w", encoding="utf-8") as f:
        json.dump(world, f)
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(argv) + ("  UNEXPECTED" if rc == 97 else "") + "\n")
    sys.stdout.write(out)
    sys.stderr.write(err)
    sys.exit(rc)


_main()
'''


def _fake_gh_script():
    return ("#!%s\nimport json\nimport os\nimport re\nimport sys\n\n\n" % sys.executable
            + "\n\n".join(inspect.getsource(f) for f in _FAKE_FUNCS) + _FAKE_GH_MAIN)


# ------------------------------------------------------------------------------ worlds

def item(n, body="clean body", title="clean title", pr=False, assoc="MEMBER"):
    kind = "pull" if pr else "issues"
    d = {"number": n, "id": 10000 + n, "node_id": ("PR_%d" if pr else "I_%d") % n, "title": title,
         "body": body, "author_association": assoc, "updated_at": T0,
         "html_url": "https://github.com/%s/%s/%d" % (REPO, kind, n)}
    if pr:
        d["pull_request"] = {"url": "https://api.github.com/repos/%s/pulls/%d" % (REPO, n)}
    return d


def comment(cid, n, body, assoc="MEMBER"):
    return {"id": cid, "node_id": "IC_%d" % cid, "body": body, "author_association": assoc,
            "updated_at": T0, "issue_url": "https://api.github.com/repos/%s/issues/%d" % (REPO, n),
            "html_url": "https://github.com/%s/issues/%d" % (REPO, n)}


def review_comment(cid, n, body, assoc="MEMBER"):
    return {"id": cid, "node_id": "PRRC_%d" % cid, "body": body, "author_association": assoc,
            "updated_at": T0, "pull_request_url": "https://api.github.com/repos/%s/pulls/%d" % (REPO, n),
            "html_url": "https://github.com/%s/pull/%d" % (REPO, n)}


def commit_comment(cid, body, assoc="MEMBER"):
    return {"id": cid, "node_id": "CC_%d" % cid, "body": body, "author_association": assoc,
            "updated_at": T0, "commit_id": "f" * 40, "html_url": "https://github.com/%s/commit/x" % REPO}


def release(rid, name="v1", body="notes"):
    return {"id": rid, "node_id": "RE_%d" % rid, "name": name, "body": body, "tag_name": "v1",
            "updated_at": T0, "html_url": "https://github.com/%s/releases/tag/v1" % REPO}


def review(rid, n, body, assoc="MEMBER"):
    return {"id": "PRR_%d" % rid, "databaseId": rid, "pr": n, "body": body, "authorAssociation": assoc}


def rev(diff, when="2026-09-02T00:00:00Z", deleted=None):
    return {"diff": diff, "editedAt": when, "deletedAt": deleted}


def mkworld(items=(), comments=(), review_comments=(), commit_comments=(), releases=(), reviews=(),
            edits=None, renames=None):
    return {"repo": REPO,
            "lists": {"issues": list(items), "issues/comments": list(comments),
                      "pulls/comments": list(review_comments), "comments": list(commit_comments),
                      "releases": list(releases)},
            "reviews": list(reviews), "edits": dict(edits or {}), "renames": dict(renames or {}),
            "fail": [], "gql": {}, "node_override": {}, "more": {}, "writes": [],
            "rate": {"cost": 1, "remaining": 4999, "resetAt": "2026-09-29T13:00:00Z"}}


def clean_world():
    return mkworld(items=[item(1), item(2, pr=True)], comments=[comment(501, 1, "a comment")],
                   reviews=[review(901, 2, "looks fine")], releases=[release(801)],
                   edits={"I_1": [rev("an older body")]})


def planted_world():
    return mkworld(
        items=[item(1, body="line one\nline two\nsee " + SLUG + " now"), item(2, pr=True),
               item(3, body="clean now"), item(4, title="about " + SLUG)],
        comments=[comment(501, 2, "cc " + SLUG)],
        review_comments=[review_comment(601, 2, "nit: " + SLUG)],
        commit_comments=[commit_comment(701, "from " + SLUG)],
        releases=[release(801, body="notes\n" + SLUG)],
        reviews=[review(901, 2, "approve; " + SLUG)],
        edits={"I_3": [rev("old text " + SLUG), rev("gone " + SLUG, deleted="2026-09-04T00:00:00Z")]},
        renames={"I_3": [{"createdAt": "2026-09-03T00:00:00Z", "previousTitle": "was " + SLUG}]})


PLANTED_LINES = (
    "issue 1 body line 3 pattern 2",
    "issue 4 title pattern 2",
    "pr 2 comment 501 line 1 pattern 2",
    "pr 2 review 901 body line 1 pattern 2",
    "pr 2 review-comment 601 line 1 pattern 2",
    "commit-comment 701 line 1 pattern 2",
    "release 801 body line 2 pattern 2",
    "history issue 3 body revision 2026-09-02T00:00:00Z line 1 pattern 2 [manual: web UI, delete the revision]",
    "history issue 3 title-rename 2026-09-03T00:00:00Z pattern 2 [manual: not deletable]",
)


# ------------------------------------------------------------------------------ running it

class Fake:
    """The in-process `run=`: gh from the world, git to the tool's own real wrapper unless scripted."""

    def __init__(self, lr, world=None, git_script=None):
        self.lr, self.world, self.git_script, self.log = lr, world, git_script, []

    def __call__(self, args, input_text=None, timeout=None):
        args = [str(a) for a in args]
        self.log.append((args, input_text))
        if args[0] == "gh":
            assert self.world is not None, "an unexpected gh call: %r" % (args,)
            return _fake_gh_serve(self.world, args[1:], input_text)
        if self.git_script is not None:
            scripted = self.git_script(args)
            if scripted is not None:
                return scripted
        return self.lr._real_run(args, input_text, timeout)

    def gh_calls(self):
        return [a for a, _ in self.log if a[0] == "gh"]

    def writes(self):
        return [a for a in self.gh_calls() if "-X" in a]


def run_main(lr, argv, world=None, git_script=None, sleeps=None):
    fake = Fake(lr, world, git_script)
    sleeps = [] if sleeps is None else sleeps
    rc = lr.main(["leak_refs.py"] + [str(a) for a in argv], run=fake, sleep=sleeps.append)
    return rc, fake


def cli(tmp_path, argv, world=None, stdin=None, env_extra=None):
    """The documented gesture, `python3 tools/leak_refs.py ...`, from the repository root, in an
    environment built from scratch: a fake gh first on PATH, HOME and GH_CONFIG_DIR in tmp, no token.
    The interpreter is `LEAK_REFS_TEST_PYTHON` when set (H0: the system python3), read per call."""
    assert TOOL.exists(), "tools/leak_refs.py is missing"
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    gh = bindir / "gh"
    gh.write_text(_fake_gh_script(), encoding="utf-8")
    gh.chmod(0o755)
    world_file, log_file = tmp_path / "world.json", tmp_path / "gh.log"
    if world is not None or not world_file.exists():
        world_file.write_text(json.dumps(world if world is not None else mkworld()), encoding="utf-8")
    pyx = os.environ.get("LEAK_REFS_TEST_PYTHON") or sys.executable
    env = {"PATH": "%s:%s:/usr/bin:/bin" % (bindir, os.path.dirname(pyx)), "HOME": str(tmp_path),
           "GH_CONFIG_DIR": str(tmp_path / "gh-config"), "FAKE_GH_WORLD": str(world_file),
           "FAKE_GH_LOG": str(log_file), "GIT_AUTHOR_NAME": "t", "GIT_COMMITTER_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    env.update(env_extra or {})
    proc = subprocess.run([pyx, "tools/leak_refs.py"] + [str(a) for a in argv], cwd=str(ROOT),
                          env=env, input=stdin, capture_output=True, text=True, timeout=300)
    log = log_file.read_text(encoding="utf-8").splitlines() if log_file.exists() else []
    assert not any("UNEXPECTED" in line for line in log), log
    return proc, log


def world_after(tmp_path):
    return json.loads((tmp_path / "world.json").read_text(encoding="utf-8"))


def P(lr, *extra):
    return lr.parse_patterns("# synthetic\n" + SLUG + "\n" + "".join(e + "\n" for e in extra))


def fill(n):
    """`n` characters of trigger-free filler ending in a space (so a word after it starts clean)."""
    return ("word " * (n // 5 + 1))[:n - 1] + " "


# ============================================================================== C2 patterns

def test_c2_no_source_refuses_even_with_a_home_patterns_file(lr, tmp_path, capsys):
    home_file = tmp_path / ".sigma-ops" / "leak-patterns.txt"
    home_file.parent.mkdir()
    home_file.write_text(SLUG + "\n", encoding="utf-8")
    inp = tmp_path / "in.txt"
    inp.write_text("see " + SLUG + "\n", encoding="utf-8")
    rc, _ = run_main(lr, ["text", inp])
    out, err = capsys.readouterr()
    refused(rc, out, err, "no-patterns-source")


def test_c2_cli_no_source(tmp_path):
    proc, log = cli(tmp_path, ["text", "-"], stdin="see " + SLUG + "\n")
    refused(proc.returncode, proc.stdout, proc.stderr, "no-patterns-source")
    assert log == []


def test_c2_cli_exported_variable_gesture(tmp_path, pats):
    """The runbook's exported-variable gesture, as a child: `SIGMA_LEAK_PATTERNS` set, no
    `--patterns`."""
    proc, log = cli(tmp_path, ["text", "-"], stdin="see " + SLUG + "\n",
                    env_extra={"SIGMA_LEAK_PATTERNS": str(pats)})
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "text stdin line 1 pattern 2" in proc.stdout
    no_leak(proc.stdout, proc.stderr)
    assert log == []


def test_c2_env_source_is_read(lr, pats, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SIGMA_LEAK_PATTERNS", str(pats))
    inp = tmp_path / "in.txt"
    inp.write_text("see " + SLUG + "\n", encoding="utf-8")
    rc, _ = run_main(lr, ["text", inp])
    assert rc == 1, capsys.readouterr()


def test_c2_zero_patterns(lr, tmp_path, capsys):
    p = tmp_path / "comments-only.txt"
    p.write_text("# one\n\n   \n# two\n", encoding="utf-8")
    inp = tmp_path / "in.txt"
    inp.write_text("anything\n", encoding="utf-8")
    rc, _ = run_main(lr, ["text", "--patterns", p, inp])
    out, err = capsys.readouterr()
    refused(rc, out, err, "zero-patterns")


def test_c2_pattern_compile_names_the_line_never_its_text(lr, tmp_path, capsys):
    p = tmp_path / "bad.txt"
    p.write_text("# c\nfine\nsecret-repo(\n", encoding="utf-8")
    rc, _ = run_main(lr, ["text", "--patterns", p, "-"])
    out, err = capsys.readouterr()
    refused(rc, out, err, "pattern-compile")
    assert "line 3" in err
    assert "secret-repo" not in err


def test_c2_pattern_matching_empty_is_refused(lr, tmp_path, capsys):
    p = tmp_path / "empty.txt"
    p.write_text("# c\nx*\n", encoding="utf-8")
    inp = tmp_path / "in.txt"
    inp.write_text("see x\n", encoding="utf-8")
    rc, _ = run_main(lr, ["text", "--patterns", p, inp])
    out, err = capsys.readouterr()
    refused(rc, out, err, "pattern-matches-empty")
    assert "line 2" in err


@pytest.mark.parametrize("which", ["missing", "directory"])
def test_c2_patterns_unreadable(lr, tmp_path, capsys, which):
    p = tmp_path / "nope.txt"
    if which == "directory":
        p.mkdir()
    rc, _ = run_main(lr, ["text", "--patterns", p, "-"])
    out, err = capsys.readouterr()
    refused(rc, out, err, "patterns-unreadable")


def test_c2_patterns_inside_a_work_tree(lr, tmp_path, capsys):
    repo = mkrepo(tmp_path)
    (repo / "sub").mkdir()
    p = repo / "sub" / "p.txt"
    p.write_text(SLUG + "\n", encoding="utf-8")
    inp = tmp_path / "in.txt"
    inp.write_text("see " + SLUG + "\n", encoding="utf-8")
    rc, _ = run_main(lr, ["text", "--patterns", p, inp])
    out, err = capsys.readouterr()
    refused(rc, out, err, "inside-work-tree")


@pytest.mark.parametrize("var", ["GIT_CEILING_DIRECTORIES", "GIT_DIR"])
def test_c2_the_git_env_is_scrubbed_for_the_probe(lr, tmp_path, monkeypatch, capsys, var):
    """Each of these, left in the probe's environment, turns a path inside a repository into
    `not a git repository`; the probe must not inherit them."""
    repo = mkrepo(tmp_path)
    (repo / "sub").mkdir()
    p = repo / "sub" / "p.txt"
    p.write_text(SLUG + "\n", encoding="utf-8")
    monkeypatch.setenv(var, str(repo.resolve()) if var == "GIT_CEILING_DIRECTORIES"
                       else str(tmp_path / "missing-git-dir"))
    inp = tmp_path / "in.txt"
    inp.write_text("see " + SLUG + "\n", encoding="utf-8")
    rc, _ = run_main(lr, ["text", "--patterns", p, inp])
    out, err = capsys.readouterr()
    refused(rc, out, err, "inside-work-tree")


@pytest.mark.parametrize("answer", [
    (128, "", "fatal: detected dubious ownership in repository at '/x'\n"),
    (127, "", "git: cannot be run\n"),
    (124, "", "git: timed out\n"),
])
def test_c2_an_unknown_probe_answer_fails_closed(lr, pats, tmp_path, capsys, answer):
    inp = tmp_path / "in.txt"
    inp.write_text("clean\n", encoding="utf-8")

    def script(args):
        return answer if "rev-parse" in args else None

    rc, _ = run_main(lr, ["text", "--patterns", pats, inp], git_script=script)
    out, err = capsys.readouterr()
    refused(rc, out, err, "work-tree-unknown")


# ============================================================================== C4 text mode

def test_c4_text_file_hit(lr, pats, tmp_path, capsys):
    inp = tmp_path / "in.txt"
    inp.write_text("clean\nsee " + SLUG + " here\n", encoding="utf-8")
    rc, _ = run_main(lr, ["text", "--patterns", pats, inp])
    out, err = capsys.readouterr()
    assert rc == 1, (out, err)
    assert "line 2 pattern 2" in out
    assert "hits: 1" in out and "hits by pattern line: 2=1" in out
    no_leak(out, err)


def test_c4_cli_text_stdin_hit(tmp_path, pats):
    proc, log = cli(tmp_path, ["text", "--patterns", pats, "-"], stdin="x\nsee " + SLUG + "\n")
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "text stdin line 2 pattern 2" in proc.stdout
    no_leak(proc.stdout, proc.stderr)
    assert log == []


def test_c4_text_clean(lr, pats, tmp_path, capsys):
    inp = tmp_path / "in.txt"
    inp.write_text("nothing to see\n", encoding="utf-8")
    rc, _ = run_main(lr, ["text", "--patterns", pats, inp])
    out, err = capsys.readouterr()
    assert rc == 0, (out, err)
    assert "hits: 0" in out


def test_c4_text_input_unreadable(lr, pats, tmp_path, capsys):
    rc, _ = run_main(lr, ["text", "--patterns", pats, tmp_path / "missing.txt"])
    out, err = capsys.readouterr()
    refused(rc, out, err, "input-unreadable")


def test_c4_in_process_stdin(lr, pats, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("see " + SLUG + "\n"))
    rc, _ = run_main(lr, ["text", "--patterns", pats, "-"])
    out, err = capsys.readouterr()
    assert rc == 1 and "text stdin line 1 pattern 2" in out
    no_leak(out, err)


# ============================================================================== C3 tree mode

def test_c3_tree_tracked_file(lr, pats, tmp_path, capsys):
    repo = mkrepo(tmp_path)
    (repo / "a.txt").write_text("x\nsee " + SLUG + "\n", encoding="utf-8")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-qm", "init")
    rc, _ = run_main(lr, ["tree", "--patterns", pats, "--root", repo])
    out, err = capsys.readouterr()
    assert rc == 1, (out, err)
    assert "tree a.txt line 2 pattern 2" in out
    no_leak(out, err)


def test_c3_tree_untracked_file(lr, pats, tmp_path, capsys):
    repo = mkrepo(tmp_path)
    (repo / "clean.txt").write_text("fine\n", encoding="utf-8")
    git(repo, "add", "clean.txt")
    git(repo, "commit", "-qm", "init")
    (repo / "b.txt").write_text("see " + SLUG + "\n", encoding="utf-8")
    rc, _ = run_main(lr, ["tree", "--patterns", pats, "--root", repo])
    out, err = capsys.readouterr()
    assert rc == 1, (out, err)
    assert "tree b.txt line 1 pattern 2" in out


def test_c3_tree_commit_message(lr, pats, tmp_path, capsys):
    repo = mkrepo(tmp_path)
    git(repo, "commit", "-q", "--allow-empty", "-m", "subject\n\nsee " + SLUG)
    sha = git(repo, "rev-parse", "HEAD").strip()
    rc, _ = run_main(lr, ["tree", "--patterns", pats, "--root", repo])
    out, err = capsys.readouterr()
    assert rc == 1, (out, err)
    assert "commit %s message line 3 pattern 2" % sha[:12] in out
    no_leak(out, err)


def test_c3_tree_path_hit_never_prints_the_path(lr, pats, tmp_path, capsys):
    repo = mkrepo(tmp_path)
    d = repo / "notes" / "acme-corp" / "secret-repo"
    d.mkdir(parents=True)
    (d / "x.txt").write_text("clean\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "init")
    rc, _ = run_main(lr, ["tree", "--patterns", pats, "--root", repo])
    out, err = capsys.readouterr()
    assert rc == 1, (out, err)
    assert "tree path entry 1 pattern 2" in out
    no_leak(out, err)


def test_c3_tree_clean_repo(lr, pats, tmp_path, capsys):
    repo = mkrepo(tmp_path)
    (repo / "a.txt").write_text("fine\n", encoding="utf-8")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-qm", "init")
    rc, _ = run_main(lr, ["tree", "--patterns", pats, "--root", repo])
    out, err = capsys.readouterr()
    assert rc == 0, (out, err)


def test_c3_tree_unborn_head_is_no_commits_not_a_failure(lr, pats, tmp_path, capsys):
    repo = mkrepo(tmp_path)
    (repo / "a.txt").write_text("fine\n", encoding="utf-8")
    rc, _ = run_main(lr, ["tree", "--patterns", pats, "--root", repo])
    out, err = capsys.readouterr()
    assert rc == 0, (out, err)


@pytest.mark.parametrize("verb", ["ls-files", "log"])
def test_c3_tree_git_failure_refuses(lr, pats, tmp_path, capsys, verb):
    repo = mkrepo(tmp_path)
    git(repo, "commit", "-q", "--allow-empty", "-m", "init")

    def script(args):
        return (1, "", "fatal: boom\n") if verb in args else None

    rc, _ = run_main(lr, ["tree", "--patterns", pats, "--root", repo], git_script=script)
    out, err = capsys.readouterr()
    refused(rc, out, err, "git-failed")


def test_c3_tree_root_must_be_a_toplevel(lr, pats, tmp_path, capsys):
    repo = mkrepo(tmp_path)
    (repo / "sub").mkdir()
    rc, _ = run_main(lr, ["tree", "--patterns", pats, "--root", repo / "sub"])
    out, err = capsys.readouterr()
    refused(rc, out, err, "root-not-toplevel")


def test_c3_tree_own_listing_must_hold_the_tool(lr, pats, capsys):
    def script(args):
        return (0, "README.md\0", "") if "ls-files" in args else None

    rc, _ = run_main(lr, ["tree", "--patterns", pats], git_script=script)
    out, err = capsys.readouterr()
    refused(rc, out, err, "listing-incomplete")


def test_c3_cli_tree(tmp_path, pats):
    repo = mkrepo(tmp_path)
    (repo / "a.txt").write_text("see " + SLUG + "\n", encoding="utf-8")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-qm", "init")
    proc, log = cli(tmp_path, ["tree", "--patterns", pats, "--root", repo])
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "tree a.txt line 1 pattern 2" in proc.stdout
    no_leak(proc.stdout, proc.stderr)
    assert log == []


def test_c3_tree_unreadable_file_refuses(lr, pats, tmp_path, capsys):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root reads a mode-000 file")
    repo = mkrepo(tmp_path)
    f = repo / "a.txt"
    f.write_text("see " + SLUG + "\n", encoding="utf-8")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-qm", "init")
    f.chmod(0)
    try:
        rc, _ = run_main(lr, ["tree", "--patterns", pats, "--root", repo])
    finally:
        f.chmod(0o644)
    out, err = capsys.readouterr()
    refused(rc, out, err, "file-unreadable")
    assert "a.txt" in err
    no_leak(err)


def test_c3_tree_tracked_file_missing_from_the_checkout_is_read_from_the_index(lr, pats, tmp_path,
                                                                               capsys):
    repo = mkrepo(tmp_path)
    (repo / "a.txt").write_text("x\nsee " + SLUG + "\n", encoding="utf-8")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-qm", "init")
    (repo / "a.txt").unlink()
    rc, _ = run_main(lr, ["tree", "--patterns", pats, "--root", repo])
    out, err = capsys.readouterr()
    assert rc == 1, (out, err)
    assert "tree a.txt (index) line 2 pattern 2" in out
    assert "scanned: 1 file(s)" in out
    no_leak(out, err)


@pytest.mark.parametrize("with_dir", [True, False])
def test_c3_tree_submodule_is_refused_not_skipped(lr, pats, tmp_path, capsys, with_dir):
    repo = mkrepo(tmp_path)
    git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    sha = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "update-index", "--add", "--cacheinfo", "160000,%s,vendor/sub" % sha)
    if with_dir:
        (repo / "vendor" / "sub").mkdir(parents=True)
    rc, _ = run_main(lr, ["tree", "--patterns", pats, "--root", repo])
    out, err = capsys.readouterr()
    refused(rc, out, err, "submodule")
    assert "vendor/sub" in err


# ============================================================================== C1 GitHub surfaces

def test_c1_planted_surfaces_each_reported(lr, pats, capsys):
    rc, fake = run_main(lr, ["scan", "--patterns", pats, "--repo", REPO], world=planted_world())
    out, err = capsys.readouterr()
    assert rc == 1, (out, err)
    for line in PLANTED_LINES:
        assert line in out, (line, out)
    assert "gone" not in out and "2026-09-04" not in out          # a deleted revision is skipped
    no_leak(out, err)
    assert "[redacted" not in out + err
    assert re.search(r"#\d", out + err) is None
    assert fake.writes() == []


def test_c1_cli_planted(tmp_path, pats):
    proc, log = cli(tmp_path, ["scan", "--patterns", pats, "--repo", REPO], world=planted_world())
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    for line in PLANTED_LINES:
        assert line in proc.stdout, (line, proc.stdout)
    no_leak(proc.stdout, proc.stderr)
    assert not any('"-X"' in line for line in log)


def test_c1_two_page_walk_reaches_page_two_reviews_and_history(lr, pats, capsys):
    """100 PRs on page 1, then 5 items: PR 101's review and issue 103's revision sit in the
    SECOND batch of their GraphQL reads."""
    items = [item(n, pr=True) for n in range(1, 101)]
    items += [item(101, pr=True)] + [item(n) for n in range(102, 106)]
    world = mkworld(items=items, reviews=[review(9101, 101, "ok " + SLUG)],
                    edits={"I_103": [rev("was " + SLUG)]})
    rc, fake = run_main(lr, ["scan", "--patterns", pats, "--repo", REPO], world=world)
    out, err = capsys.readouterr()
    assert rc == 1, (out, err)
    assert "pr 101 review 9101 body line 1 pattern 2" in out
    assert "history issue 103 body revision 2026-09-02T00:00:00Z line 1 pattern 2" in out
    items_batches = [i for a, i in fake.log if a[0] == "gh" and i and "# leak_refs:items" in i]
    assert len(items_batches) == 2
    assert fake.writes() == []


def test_c1_clean_repository_exits_zero(lr, pats, capsys):
    rc, fake = run_main(lr, ["scan", "--patterns", pats, "--repo", REPO], world=clean_world())
    out, err = capsys.readouterr()
    assert rc == 0, (out, err)
    assert "hits: 0" in out and "truncated: 0" in out
    assert fake.writes() == []


@pytest.mark.parametrize("only", ["revision", "title-rename"])
def test_c1_history_only_hits_exit_one(lr, pats, capsys, only):
    """The runbook's final gate state: nothing left but history the web UI must purge (or cannot).
    That is still a finding, exit 1, never a clean 0."""
    world = clean_world()
    if only == "revision":
        world["edits"] = {"I_1": [rev("older " + SLUG)]}
    else:
        world["renames"] = {"I_1": [{"createdAt": "2026-09-03T00:00:00Z", "previousTitle": "was " + SLUG}]}
    rc, fake = run_main(lr, ["scan", "--patterns", pats, "--repo", REPO], world=world)
    out, err = capsys.readouterr()
    assert rc == 1, (out, err)
    assert "hits: 1 (history 1)" in out and "truncated: 0" in out
    assert fake.writes() == []


@pytest.mark.parametrize("what,node,line", [
    ("reviews", "PR_2", "truncated pr 2 reviews (more than 100)"),
    ("revisions", "I_1", "truncated issue 1 revisions (more than 100)"),
    ("renames", "I_1", "truncated issue 1 title-renames (more than 100)"),
])
def test_c1_truncation_is_a_finding(lr, pats, capsys, what, node, line):
    world = clean_world()
    world["more"] = {what: [node]}
    rc, _ = run_main(lr, ["scan", "--patterns", pats, "--repo", REPO], world=world)
    out, err = capsys.readouterr()
    assert rc == 1, (out, err)
    assert line in out
    assert "truncated: 1" in out


def test_c1_gh_failure_stderr_is_redacted(lr, pats, capsys):
    world = clean_world()
    world["fail"] = [{"match": "issues?state=all", "rc": 1,
                      "err": "gh: cannot read " + SLUG + " (HTTP 502)\nsecond " + SLUG + "\n"}]
    rc, _ = run_main(lr, ["scan", "--patterns", pats, "--repo", REPO], world=world)
    out, err = capsys.readouterr()
    refused(rc, out, err, "gh-failed")
    assert "[redacted: pattern 2]" in err
    no_leak(err)


def test_c1_gh_failure_stderr_is_redacted_before_the_cut(lr, pats, capsys):
    """`acme-corp` ends at character 199 of the first stderr line: a cut at 200 before the redaction
    would leave it whole."""
    world = clean_world()
    world["fail"] = [{"match": "issues?state=all", "rc": 1, "err": "x" * 190 + SLUG + " tail\n"}]
    rc, _ = run_main(lr, ["scan", "--patterns", pats, "--repo", REPO], world=world)
    out, err = capsys.readouterr()
    refused(rc, out, err, "gh-failed")
    no_leak(err)


def test_c1_an_unexpected_exception_prints_its_type_only(lr, pats, monkeypatch, capsys):
    def boom(*_a, **_k):
        raise ValueError("secret " + SLUG)

    monkeypatch.setattr(lr, "find_hits", boom)
    rc, _ = run_main(lr, ["scan", "--patterns", pats, "--repo", REPO], world=clean_world())
    out, err = capsys.readouterr()
    refused(rc, out, err, "internal")
    assert "ValueError" in err
    no_leak(err)


def test_c1_repo_is_required(lr, pats, capsys):
    rc, fake = run_main(lr, ["scan", "--patterns", pats], world=clean_world())
    out, err = capsys.readouterr()
    refused(rc, out, err, "bad-repo")
    assert fake.gh_calls() == []


# ============================================================================== C7 gh seam, shapes

def _scan(lr, pats, world, *extra):
    return run_main(lr, ["scan", "--patterns", pats, "--repo", REPO] + list(extra), world=world)


def test_c7_failing_comments_page(lr, pats, capsys):
    world = clean_world()
    world["fail"] = [{"match": "issues/comments?", "rc": 1, "err": "gh: HTTP 502\n"}]
    rc, _ = _scan(lr, pats, world)
    out, err = capsys.readouterr()
    refused(rc, out, err, "gh-failed")
    assert "issues/comments page 1" in err


def test_c7_unparsable_page(lr, pats, capsys):
    world = clean_world()
    world["fail"] = [{"match": "issues/comments?", "rc": 0, "out": "<html>oops</html>"}]
    rc, _ = _scan(lr, pats, world)
    out, err = capsys.readouterr()
    refused(rc, out, err, "gh-failed")


def test_c7_page_cap_refuses_rather_than_truncating(lr, pats, capsys):
    world = mkworld(items=[item(n) for n in range(1, 206)])
    rc, fake = _scan(lr, pats, world, "--max-pages", "2")
    out, err = capsys.readouterr()
    refused(rc, out, err, "page-cap")
    assert "issues" in err


def test_c7_three_pages_walk_whole(lr, pats, capsys):
    world = mkworld(items=[item(n) for n in range(1, 206)] + [item(206, body="see " + SLUG)])
    rc, fake = _scan(lr, pats, world)
    out, err = capsys.readouterr()
    assert rc == 1 and "issue 206 body line 1 pattern 2" in out, (out, err)


def test_c7_dict_page_is_bad_shape(lr, pats, capsys):
    world = clean_world()
    world["fail"] = [{"match": "issues/comments?", "rc": 0, "out": '{"message": "nope"}'}]
    rc, _ = _scan(lr, pats, world)
    out, err = capsys.readouterr()
    refused(rc, out, err, "bad-shape")


def test_c7_empty_node_is_bad_shape(lr, pats, capsys):
    world = clean_world()
    world["node_override"] = {"history": {"I_1": {}}}
    rc, _ = _scan(lr, pats, world)
    out, err = capsys.readouterr()
    refused(rc, out, err, "bad-shape")


def test_c7_unexpected_typename_is_bad_shape(lr, pats, capsys):
    world = clean_world()
    node = _fake_gh_node(world, "I_1", "history")
    node["__typename"] = "Discussion"
    world["node_override"] = {"history": {"I_1": node}}
    rc, _ = _scan(lr, pats, world)
    out, err = capsys.readouterr()
    refused(rc, out, err, "bad-shape")


def test_c7_null_connection_is_bad_shape(lr, pats, capsys):
    world = clean_world()
    node = _fake_gh_node(world, "I_1", "history")
    node["userContentEdits"] = None
    world["node_override"] = {"history": {"I_1": node}}
    rc, _ = _scan(lr, pats, world)
    out, err = capsys.readouterr()
    refused(rc, out, err, "bad-shape")


@pytest.mark.parametrize("batch,node,key", [
    ("items", "PR_2", "reviews"), ("history", "I_1", "userContentEdits"),
    ("history", "I_1", "timelineItems"),
])
@pytest.mark.parametrize("page_info", [{}, {"hasNextPage": None}, {"hasNextPage": "false"}])
def test_c7_has_next_page_must_be_a_boolean(lr, pats, capsys, batch, node, key, page_info):
    """An absent or non-boolean `hasNextPage` is not read as `complete`."""
    world = clean_world()
    n = _fake_gh_node(world, node, batch)
    n[key]["pageInfo"] = page_info
    world["node_override"] = {batch: {node: n}}
    rc, _ = _scan(lr, pats, world)
    out, err = capsys.readouterr()
    refused(rc, out, err, "bad-shape")


def test_c7_nodes_shorter_than_ids_is_bad_shape(lr, pats, capsys):
    world = clean_world()
    world["short"] = "history"
    rc, _ = _scan(lr, pats, world)
    out, err = capsys.readouterr()
    refused(rc, out, err, "bad-shape")


def test_c7_each_requested_id_exactly_once(lr, pats, capsys):
    world = clean_world()
    world["dup"] = "history"
    rc, _ = _scan(lr, pats, world)
    out, err = capsys.readouterr()
    refused(rc, out, err, "bad-shape")


def test_c7_every_walked_item_needs_its_item_batch_node(lr, pats, capsys):
    world = clean_world()
    world["short"] = "items"
    rc, _ = _scan(lr, pats, world)
    out, err = capsys.readouterr()
    refused(rc, out, err, "bad-shape")


def test_c7_null_revision_text_is_bad_shape(lr, pats, capsys):
    world = clean_world()
    world["edits"] = {"I_1": [rev(None)]}
    rc, _ = _scan(lr, pats, world)
    out, err = capsys.readouterr()
    refused(rc, out, err, "bad-shape")


@pytest.mark.parametrize("key", ["issues", "commitComments", "releases"])
def test_c7_repository_counts_cross_check(lr, pats, capsys, key):
    world = clean_world()
    world["counts_delta"] = {key: 1}
    rc, _ = _scan(lr, pats, world)
    out, err = capsys.readouterr()
    refused(rc, out, err, "moved-during-scan")


def _per_item_world(rest=None, batch=None, n_comments=2):
    world = mkworld(items=[item(1)], comments=[comment(500 + i, 1, "c") for i in range(n_comments)])
    if rest is not None:
        world["rest_comments"] = {"1": rest}
    if batch is not None:
        world["batch_comments"] = {"1": batch}
    return world


def test_c7_per_item_rest_count_above_walk(lr, pats, capsys):
    rc, _ = _scan(lr, pats, _per_item_world(rest=3))          # (REST, walked, batch) = (3, 2, 2)
    out, err = capsys.readouterr()
    refused(rc, out, err, "moved-during-scan")


def test_c7_per_item_batch_count_above_walk(lr, pats, capsys):
    rc, _ = _scan(lr, pats, _per_item_world(batch=3))         # (2, 2, 3)
    out, err = capsys.readouterr()
    refused(rc, out, err, "moved-during-scan")


def test_c7_per_item_deletion_behind_the_cursor(lr, pats, capsys):
    world = _per_item_world(n_comments=150)
    world["delete_after"] = {"path": "issues/comments", "page": 1, "index": 0}
    rc, _ = _scan(lr, pats, world)
    out, err = capsys.readouterr()
    refused(rc, out, err, "moved-during-scan")


def test_c7_comment_on_an_unwalked_item(lr, pats, capsys):
    world = clean_world()
    world["lists"]["issues/comments"].append(comment(599, 77, "orphan"))
    rc, _ = _scan(lr, pats, world)
    out, err = capsys.readouterr()
    refused(rc, out, err, "moved-during-scan")


def test_c7_budget_stop(lr, pats, capsys):
    world = clean_world()
    world["rate"] = {"cost": 3, "remaining": 5, "resetAt": "2026-09-29T13:00:00Z"}
    rc, _ = _scan(lr, pats, world)
    out, err = capsys.readouterr()
    refused(rc, out, err, "budget")
    assert "2026-09-29T13:00:00Z" in err


def test_c7_graphql_errors_are_a_failure(lr, pats, capsys):
    world = clean_world()
    world["gql"] = {"counts": {"errors": [{"message": "Something went wrong"}]}}
    rc, _ = _scan(lr, pats, world)
    out, err = capsys.readouterr()
    refused(rc, out, err, "gh-failed")


def test_c7_graphql_goes_through_stdin(lr, pats, capsys):
    rc, fake = _scan(lr, pats, clean_world())
    capsys.readouterr()
    for args, stdin in fake.log:
        if args[:3] == ["gh", "api", "graphql"]:
            assert args == ["gh", "api", "graphql", "--input", "-"]
            assert stdin and json.loads(stdin)["query"].startswith("# leak_refs:")


def test_c7_real_wrapper_timeout_and_missing_binary(lr):
    rc, _out, _err = lr._real_run([sys.executable, "-c", "import time; time.sleep(5)"], None, 0.5)
    assert rc == 124
    rc, _out, _err = lr._real_run(["leak-refs-no-such-binary-%s" % uuid.uuid4().hex], None, 5)
    assert rc == 127


# ============================================================================== C5 rewrite

def test_c5_whole_reference_link(lr):
    plan = lr.plan_edit("see [the plan](" + URL12 + ") today", P(lr))
    assert plan["after"] == "see " + ISSUE_WORDS + " today"
    assert plan["reasons"] == []


def test_c5_whole_reference_url(lr):
    plan = lr.plan_edit("see " + URL12 + ".", P(lr))
    assert plan["after"] == "see " + ISSUE_WORDS + "."
    assert plan["reasons"] == []


def test_c5_whole_reference_owner_ref(lr):
    plan = lr.plan_edit("see " + SLUG + "#12 now", P(lr))
    assert plan["after"] == "see " + ISSUE_WORDS + " now"
    assert plan["reasons"] == []


def test_c5_bare_slug(lr):
    plan = lr.plan_edit("from " + SLUG + " days", P(lr))
    assert plan["after"] == "from " + REPO_WORDS + " days"
    assert plan["reasons"] == []


def test_c5_autolink_loses_its_angle_brackets(lr):
    plan = lr.plan_edit("see <" + URL12 + "> now", P(lr))
    assert plan["after"] == "see " + ISSUE_WORDS + " now"
    assert "<" not in plan["after"]
    assert plan["reasons"] == []


def test_c5_backtick_quoted_url_keeps_its_code_span(lr):
    """A URL in a code span: the rewrite ends before the closing backtick, so the `#34` and
    `@octocat` code spans after it stay code, never a live link and a mention."""
    text = "context: `" + URL12 + "` then `#34` and `@octocat`"
    plan = lr.plan_edit(text, P(lr))
    assert plan["after"] == "context: `" + ISSUE_WORDS + "` then `#34` and `@octocat`", plan["after"]
    assert plan["reasons"] == [], plan["reasons"]


@pytest.mark.parametrize("left,right", [
    ("`", "`"), ("**", "**"), ("_", "_"), ("~~", "~~"), ('"', '"'), ("'", "'"), ("| ", "|"),
    ("[", "]"), ("(", ")"),
])
def test_c5_url_ends_before_a_closing_delimiter(lr, left, right):
    text = "a " + left + URL12 + right + " b"
    plan = lr.plan_edit(text, P(lr))
    assert plan["after"] == "a " + left + ISSUE_WORDS + right + " b", plan["after"]
    assert plan["reasons"] == [], plan["reasons"]


def test_c5_owner_ref_leaves_a_leading_emphasis_delimiter(lr):
    plan = lr.plan_edit("_" + SLUG + "#12_ done", P(lr))
    assert plan["after"] == "_" + ISSUE_WORDS + "_ done", plan["after"]
    assert plan["reasons"] == [], plan["reasons"]


@pytest.mark.parametrize("quote", ['"', "'"])
def test_c5_url_in_an_html_attribute_is_manual(lr, quote):
    """Rewriting an attribute value is never planned: the span sits inside an HTML tag."""
    text = "<a href=%s%s%s>the plan</a> and `#34`" % (quote, URL12, quote)
    plan = lr.plan_edit(text, P(lr))
    assert "delimiter" in plan["reasons"], plan


def test_c5_delimiter_belt_code_span_change(lr):
    """The belt behind the URL fix: a pattern whose own match takes the closing backtick still
    cannot move `#34` out of its code span; the edit goes manual."""
    text = "context: `" + URL12 + "` then `#34`"
    plan = lr.plan_edit(text, P(lr, r"issues/12`"))
    assert "delimiter" in plan["reasons"], plan


@pytest.mark.parametrize("extra,text", [
    (r"secret-repo\*", "**" + SLUG + "**"),                   # splits a `**` run
    (r"repo\| x", "| " + SLUG + "| x |"),                      # takes a table-cell pipe
    (r"repo\"", '"' + SLUG + '" and #34'),                     # takes one quote of a pair
    (r"repo\]", "[" + SLUG + "] x"),                           # takes one bracket of a pair
])
def test_c5_delimiter_belt_span_takes_a_delimiter(lr, extra, text):
    plan = lr.plan_edit(text, P(lr, extra))
    assert "delimiter" in plan["reasons"], plan


def test_c5_blocked_by_marker_stays_an_edit_and_counts_one_edge(lr):
    plan = lr.plan_edit("**Blocked by:** " + SLUG + "#12", P(lr))
    assert plan["reasons"] == []
    assert plan["edges_removed"] == 0           # #1608: `owner/repo#N` was never a stored local edge


def _qa(lr, x):
    bs = lr._consumers().blocker_scan
    return bs.UNPARK_QA_START + x + bs.UNPARK_QA_END


def test_c5_blocker_edge_plain(lr):
    plan = lr.plan_edit("**Blocked by:** " + SLUG + "#12 and #34", P(lr))
    assert "blocker-edge" in plan["reasons"]


def test_c5_blocker_edge_qa_join(lr):
    text = "**Blocked by:** " + SLUG + "#12" + _qa(lr, "answer") + " #34"
    assert "blocker-edge" in lr.plan_edit(text, P(lr))["reasons"]


def test_c5_blocker_edge_qa_copy(lr):
    text = "**Blocked by:** " + SLUG + "#12 and #34\n" + _qa(lr, "it depends on #34")
    assert "blocker-edge" in lr.plan_edit(text, P(lr))["reasons"]


def test_c5_blocker_edge_duplicate(lr):
    text = "Blocked by #34\n**Blocked by:** " + SLUG + "#12 and #34"
    assert "blocker-edge" in lr.plan_edit(text, P(lr))["reasons"]


def test_c5_blocker_edge_extract_refs_live(lr, monkeypatch):
    bs = lr._consumers().blocker_scan
    real = bs.extract_refs

    def wrapped(text, self_ref=None):
        extra = [{"phrase": "blocked by", "ref": "99"}] if "predecessor" in (text or "") else []
        return real(text, self_ref) + extra

    monkeypatch.setattr(bs, "extract_refs", wrapped)
    assert "blocker-edge" in lr.plan_edit("see " + SLUG + "#12", P(lr))["reasons"]


def _edges(bc, body, comments=()):
    rec = bc.mirror.normalize_issue({"number": 1, "title": "t", "body": body})
    doc = bc._docs_from_records([rec])[0]
    extra = bc._cap_join_excerpts(bc._strip_offboard_prefixes(bc._filter_dismissal_comments(list(comments))))
    return sorted(bc._referenced_blocker_refs(doc, extra))


def _assert_cap_shift(lr, bc, before, body=None, comments_other=()):
    """`before` is the edited text: the issue body when `body` is None, else one comment on `body`.
    Precondition: the REAL check-time consumer gains an edge from the edit; the whole-text views
    do not see it (no `blocker-edge`); invariant (d) does (`cap-shift`)."""
    plan = lr.plan_edit(before, P(lr))
    after = plan["after"]
    if body is None:
        gained = (_edges(bc, before, comments_other), _edges(bc, after, comments_other))
    else:
        gained = (_edges(bc, body, [before]), _edges(bc, body, [after]))
    assert gained[0] == [] and gained[1] != [], gained
    assert "blocker-edge" not in plan["reasons"], plan["reasons"]
    assert "cap-shift" in plan["reasons"], plan["reasons"]


def test_c5_cap_shift_cap_straddle(lr, bc):
    head = URL12 + "\n"
    before = head + fill(497 - len(head)) + "needs #1234 tail " + fill(100)
    assert before.index("needs") == 497
    _assert_cap_shift(lr, bc, before)


def test_c5_cap_shift_end_enters(lr, bc):
    bs = lr._consumers().blocker_scan
    head = URL12 + "\n" + bs.UNPARK_QA_START
    before = head + fill(490 - len(head)) + bs.UNPARK_QA_END + " " + fill(100)
    assert before.index(bs.UNPARK_QA_END) == 490
    _assert_cap_shift(lr, bc, before, comments_other=["this needs #34 first"])


def test_c5_cap_shift_prefix_slack(lr, bc):
    fail_prefix = bc.sources.FAIL_COMMENT_PREFIX            # the real one, not the tool's copy
    head = fail_prefix + URL12 + " "
    before = head + fill(560 - len(head)) + "needs #34 end"
    assert before.rindex("needs") == 560
    _assert_cap_shift(lr, bc, before, body="plain body")


def test_c5_cap_shift_start_leaves(lr, bc):
    bs = lr._consumers().blocker_scan
    head = " ".join([SLUG] * 8) + "\n"
    before = head + fill(460 - len(head)) + bs.UNPARK_QA_START + "answer text" + bs.UNPARK_QA_END + " " + fill(200)
    plan = lr.plan_edit(before, P(lr))
    assert plan["after"].index(bs.UNPARK_QA_START) == 564
    _assert_cap_shift(lr, bc, before, comments_other=["this needs #34 first"])


def test_c5_cap_shift_join_line(lr, bc):
    bs = lr._consumers().blocker_scan
    before = bs.UNPARK_QA_END + " https://github.com/" + SLUG + " #34"
    _assert_cap_shift(lr, bc, before, body="this needs " + bs.UNPARK_QA_START)


def test_c5_cap_shift_join_line_retired_spelling(lr, bc):
    """The same join with the end marker in the plugin's previous spelling, built at run time."""
    bs = lr._consumers().blocker_scan
    retired_end = bs.legacy.retired_spelling(bs.UNPARK_QA_END)
    assert retired_end != bs.UNPARK_QA_END
    before = retired_end + " https://github.com/" + SLUG + " #34"
    _assert_cap_shift(lr, bc, before, body="this needs " + bs.UNPARK_QA_START)


def test_c5_cap_shift_scrub_changes_the_text(lr):
    key = "s" + "k-" + "Ab12" * 6                           # a key shape, built from fragments
    plan = lr.plan_edit("see " + SLUG + ", key " + key, P(lr))
    assert plan["reasons"] == ["cap-shift"], plan["reasons"]


def test_c5_cap_shift_park_prefix_short_comment(lr):
    """Short, but its excerpt is not its whole text: the prefix makes it cut (conservative)."""
    text = lr._OFFBOARD[0] + SLUG + " is done, see #34"
    plan = lr.plan_edit(text, P(lr))
    assert plan["reasons"] == ["cap-shift"], plan["reasons"]


def test_c5_short_uncut_edit_with_a_number_is_fine(lr):
    plan = lr.plan_edit(SLUG + " is done, see #34", P(lr))
    assert plan["reasons"] == [], plan["reasons"]


def test_c5_marker_in_span(lr):
    plan = lr.plan_edit("[sigma:dismissed-finding](" + URL12 + ")", P(lr))
    assert plan["reasons"] == ["marker-in-span"], plan["reasons"]


def test_c5_pattern_left(lr):
    plan = lr.plan_edit(SLUG + " zeta", P(lr, "repository zeta"))
    assert "pattern-left" in plan["reasons"]


@pytest.mark.parametrize("text", ["cc @" + SLUG, "<" + SLUG + ">"])
def test_c5_token_added(lr, text):
    assert "token-added" in lr.plan_edit(text, P(lr))["reasons"]


def test_c5_offboard_prefixes_pinned_to_sources(lr):
    sources = _load_path("sources_for_leak_refs", SCRIPTS / "sources.py")
    assert lr._OFFBOARD == tuple(sources.OFFBOARD_COMMENT_PREFIXES)


def test_c5_consumers_pinned(lr):
    c = lr._consumers()
    for attr in ("_BLOCK_RE", "_EXPLICIT_BLOCK_RE", "strip_unpark_qa", "extract_refs",
                 "UNPARK_QA_START", "UNPARK_QA_END"):
        assert hasattr(c.blocker_scan, attr), attr
    assert hasattr(c.blocker_scan.legacy, "find_marker") and hasattr(c.blocker_scan.legacy, "MARKERS")
    assert callable(c.scrub) and c._EXCERPT_CHARS == 500
    assert lr._consumers() is c                                   # loaded once, then cached


@pytest.mark.parametrize("stub", [None, "scrub = lambda t: t\n_EXCERPT_CHARS = 500\n"])
def test_c5_consumers_unavailable_refuse(lr, pats, tmp_path, monkeypatch, capsys, stub):
    fake_mirror = tmp_path / "mirror-stub.py"
    if stub is not None:
        fake_mirror.write_text(stub, encoding="utf-8")
    monkeypatch.setattr(lr, "_MIRROR_PATH", fake_mirror)
    (tmp_path / "ops").mkdir()
    rc, fake = run_main(lr, ["rewrite", "--patterns", pats, "--repo", REPO, "--out",
                             tmp_path / "ops" / "dry.md"], world=planted_world())
    out, err = capsys.readouterr()
    refused(rc, out, err, "blocker-scan-unavailable")


@pytest.mark.parametrize("rep", ["see #12", "ask @someone", "sigma:approve", SLUG, "  "])
def test_c5_unsafe_replacement_refused(lr, pats, tmp_path, capsys, rep):
    (tmp_path / "ops").mkdir()
    rc, fake = run_main(lr, ["rewrite", "--patterns", pats, "--repo", REPO, "--out",
                             tmp_path / "ops" / "dry.md", "--replacement", rep], world=planted_world())
    out, err = capsys.readouterr()
    refused(rc, out, err, "replacement-unsafe")
    assert fake.gh_calls() == []


def _dry_world():
    return mkworld(
        items=[item(1, body="see [the plan](" + URL12 + ") today"),
               item(2, body="**Blocked by:** " + SLUG + "#12"),
               item(3, pr=True), item(4, title="about " + SLUG)],
        comments=[comment(501, 3, "cc " + SLUG, assoc="CONTRIBUTOR")],
        releases=[release(801, body="notes: " + SLUG)],
        edits={"I_1": [rev("older " + SLUG)]})


def _manifest(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    i = max(k for k, line in enumerate(lines) if line == "# manifest")
    return json.loads(lines[i + 1])


def test_c5_dry_run_file_counts_and_manifest(lr, pats, tmp_path, capsys):
    (tmp_path / "ops").mkdir()
    out_file = tmp_path / "ops" / "dry.md"
    rc, fake = run_main(lr, ["rewrite", "--patterns", pats, "--repo", REPO, "--out", out_file],
                        world=_dry_world())
    out, err = capsys.readouterr()
    assert rc == 1, (out, err)
    assert stat.S_IMODE(os.stat(str(out_file)).st_mode) == 0o600
    assert "~/ops/dry.md" in out and str(tmp_path) not in out
    no_leak(out, err)
    assert "[redacted" not in out + err
    assert "edits 3" in out
    assert "manual 2" in out and "not-owned 1" in out and "title 1" in out
    assert "blocker edges removed 0" in out     # #1608: the only marker names another repository
    assert "history revisions to purge 1" in out and "revisions the apply will add 2" in out
    assert fake.writes() == []
    m = _manifest(out_file)
    assert m["schema"] == "sigma.leak-refs/1" and m["repo"] == REPO
    by = {(e["surface"], e["id"], e["field"]): e for e in m["edits"]}
    assert set(by) == {("issue", 10001, "body"), ("issue", 10002, "body"), ("release", 801, "body")}
    assert by[("issue", 10001, "body")]["after"] == "see " + ISSUE_WORDS + " today"
    assert by[("issue", 10002, "body")]["after"] == "**Blocked by:** " + ISSUE_WORDS
    for e in m["edits"]:
        assert e["sha256_before"] == hashlib.sha256(e["before"].encode("utf-8")).hexdigest()
        assert e["sha256_after"] == hashlib.sha256(e["after"].encode("utf-8")).hexdigest()
    text = out_file.read_text(encoding="utf-8")
    assert "pr 3 comment 501" in text and "not-owned" in text
    assert "issue 4 title" in text


def test_c5_dry_run_out_inside_a_work_tree_is_refused(lr, pats, tmp_path, capsys):
    repo = mkrepo(tmp_path)
    out_file = repo / "dry.md"
    rc, fake = run_main(lr, ["rewrite", "--patterns", pats, "--repo", REPO, "--out", out_file],
                        world=_dry_world())
    out, err = capsys.readouterr()
    refused(rc, out, err, "inside-work-tree")
    assert not out_file.exists()
    assert fake.gh_calls() == []


def test_c5_dry_run_out_inside_this_repository_is_refused(lr, pats, capsys):
    out_file = ROOT / ("leak-refs-test-%s.md" % uuid.uuid4().hex)
    try:
        rc, fake = run_main(lr, ["rewrite", "--patterns", pats, "--repo", REPO, "--out", out_file],
                            world=_dry_world())
        out, err = capsys.readouterr()
        refused(rc, out, err, "inside-work-tree")
        assert not out_file.exists()
    finally:
        if out_file.exists():
            out_file.unlink()


def test_c5_dry_run_never_overwrites(lr, pats, tmp_path, capsys):
    (tmp_path / "ops").mkdir()
    out_file = tmp_path / "ops" / "dry.md"
    out_file.write_text("keep me", encoding="utf-8")
    rc, _ = run_main(lr, ["rewrite", "--patterns", pats, "--repo", REPO, "--out", out_file],
                     world=_dry_world())
    out, err = capsys.readouterr()
    refused(rc, out, err, "out-exists")
    assert out_file.read_text(encoding="utf-8") == "keep me"


def test_c5_dry_run_needs_out(lr, pats, capsys):
    rc, fake = run_main(lr, ["rewrite", "--patterns", pats, "--repo", REPO], world=_dry_world())
    out, err = capsys.readouterr()
    refused(rc, out, err, "out-required")


def test_c5_dry_run_clean_writes_nothing(lr, pats, tmp_path, capsys):
    (tmp_path / "ops").mkdir()
    out_file = tmp_path / "ops" / "dry.md"
    rc, _ = run_main(lr, ["rewrite", "--patterns", pats, "--repo", REPO, "--out", out_file],
                     world=clean_world())
    out, err = capsys.readouterr()
    assert rc == 0, (out, err)
    assert not out_file.exists()


def test_c5_cli_dry_run(tmp_path, pats):
    (tmp_path / "ops").mkdir()
    out_file = tmp_path / "ops" / "dry.md"
    proc, log = cli(tmp_path, ["rewrite", "--patterns", pats, "--repo", REPO, "--out", out_file],
                    world=_dry_world())
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert stat.S_IMODE(os.stat(str(out_file)).st_mode) == 0o600
    assert "~/ops/dry.md" in proc.stdout
    no_leak(proc.stdout, proc.stderr)
    assert not any('"-X"' in line for line in log)


# ============================================================================== C6 apply

def _apply_world():
    return mkworld(
        items=[item(1, body="see " + URL12 + " for context"), item(2, pr=True)],
        comments=[comment(501, 1, "cc " + SLUG + " please")],
        reviews=[review(901, 2, "lgtm, like " + SLUG)],
        releases=[release(801, body="notes: " + SLUG)])


def _dry(lr, pats, tmp_path, world, capsys):
    (tmp_path / "ops").mkdir(exist_ok=True)
    out_file = tmp_path / "ops" / ("dry-%s.md" % uuid.uuid4().hex[:8])
    rc, _ = run_main(lr, ["rewrite", "--patterns", pats, "--repo", REPO, "--out", out_file], world=world)
    capsys.readouterr()
    assert rc == 1 and out_file.exists()
    return out_file


def _apply(lr, pats, world, manifest, *extra, sleeps=None):
    return run_main(lr, ["rewrite", "--apply", "--from", manifest, "--patterns", pats, "--repo", REPO]
                    + list(extra), world=world, sleeps=sleeps)


def test_c6_apply_writes_verifies_and_is_idempotent(lr, pats, tmp_path, capsys):
    world = _apply_world()
    manifest = _dry(lr, pats, tmp_path, world, capsys)
    sleeps = []
    rc, fake = _apply(lr, pats, world, manifest, sleeps=sleeps)
    out, err = capsys.readouterr()
    assert rc == 0, (out, err)
    assert len(re.findall(r": verified$", out, re.M)) == 4, out
    assert sorted(tuple(w) for w in world["writes"]) == sorted([
        ("PATCH", "issues/1"), ("PATCH", "issues/comments/501"), ("PUT", "pulls/2/reviews/901"),
        ("PATCH", "releases/801")])
    assert world["lists"]["issues"][0]["body"] == "see " + ISSUE_WORDS + " for context"
    assert sleeps.count(1.0) >= 3                              # paced between writes
    for args, stdin in fake.log:                               # the text rides stdin, never argv
        assert ISSUE_WORDS not in " ".join(args) and REPO_WORDS not in " ".join(args)
    no_leak(out, err)
    rc, fake = _apply(lr, pats, world, manifest)
    out, err = capsys.readouterr()
    assert rc == 0 and len(re.findall(r": already applied$", out, re.M)) == 4, (out, err)
    assert fake.writes() == []


def test_c6_changed_since_the_dry_run_is_skipped(lr, pats, tmp_path, capsys):
    world = _apply_world()
    manifest = _dry(lr, pats, tmp_path, world, capsys)
    world["lists"]["issues"][0]["body"] = "someone edited this, " + SLUG
    rc, fake = _apply(lr, pats, world, manifest)
    out, err = capsys.readouterr()
    assert rc == 1, (out, err)
    assert "issue 1 body: changed since the dry-run" in out
    assert ["PATCH", "issues/1"] not in world["writes"]


def test_c6_not_owned_at_apply_time_is_never_written(lr, pats, tmp_path, capsys):
    world = _apply_world()
    manifest = _dry(lr, pats, tmp_path, world, capsys)
    world["lists"]["issues/comments"][0]["author_association"] = "CONTRIBUTOR"
    rc, fake = _apply(lr, pats, world, manifest)
    out, err = capsys.readouterr()
    assert rc == 1, (out, err)
    assert "comment 501: not-owned" in out
    assert ["PATCH", "issues/comments/501"] not in world["writes"]


@pytest.mark.parametrize("how", ["removed", "HTTP 404", "HTTP 410"])
def test_c6_deleted_since_the_dry_run_is_a_skip_never_a_write(lr, pats, tmp_path, capsys, how):
    """An item deleted after the dry-run is skipped on every run (exit 1), never refused, never
    written; the rest of the file still applies."""
    world = _apply_world()
    manifest = _dry(lr, pats, tmp_path, world, capsys)
    if how == "removed":
        world["lists"]["issues/comments"] = []
    else:
        world["fail"] = [{"match": "api repos/acme/demo/issues/comments/501", "rc": 1,
                          "out": '{"message": "gone"}', "err": "gh: Gone (%s)\n" % how}]
    for run in (1, 2):
        rc, fake = _apply(lr, pats, world, manifest)
        out, err = capsys.readouterr()
        assert rc == 1, (run, out, err)
        assert "item 1 comment 501: deleted since the dry-run" in out, out
        assert ["PATCH", "issues/comments/501"] not in world["writes"]
        assert not any("issues/comments/501" in " ".join(w) for w in fake.writes())
    assert len(world["writes"]) == 3
    assert "deleted since the dry-run 1" in out


def test_c6_a_failed_read_that_is_not_a_deletion_still_refuses(lr, pats, tmp_path, capsys):
    world = _apply_world()
    manifest = _dry(lr, pats, tmp_path, world, capsys)
    world["fail"] = [{"match": "api repos/acme/demo/issues/comments/501", "rc": 1,
                      "err": "gh: Bad Gateway (HTTP 502)\n"}]
    rc, _ = _apply(lr, pats, world, manifest)
    out, err = capsys.readouterr()
    refused(rc, out, err, "gh-failed")


def test_c6_cli_apply_needs_from_before_any_gh_call(tmp_path, pats):
    proc, log = cli(tmp_path, ["rewrite", "--apply", "--patterns", pats, "--repo", REPO],
                    world=_apply_world())
    refused(proc.returncode, proc.stdout, proc.stderr, "apply-needs-from")
    assert log == []


def test_c6_a_failed_second_write_exits_2_and_a_rerun_finishes(lr, pats, tmp_path, capsys):
    world = _apply_world()
    manifest = _dry(lr, pats, tmp_path, world, capsys)
    world["fail"] = [{"match": "-X PATCH", "nth": 2, "rc": 1, "out": "HTTP/2.0 500 Internal\n\n{}",
                      "err": "gh: HTTP 500\n"}]
    rc, _ = _apply(lr, pats, world, manifest)
    out, err = capsys.readouterr()
    refused(rc, out, err, "gh-failed")
    rc, _ = _apply(lr, pats, world, manifest)
    out, err = capsys.readouterr()
    assert rc == 0, (out, err)
    writes = [tuple(w) for w in world["writes"]]
    assert len(writes) == 4 and len(set(writes)) == 4          # each id written exactly once


def test_c6_a_lost_ack_reruns_to_already_applied(lr, pats, tmp_path, capsys):
    world = _apply_world()
    manifest = _dry(lr, pats, tmp_path, world, capsys)
    world["fail"] = [{"match": "-X PATCH", "nth": 1, "apply": True, "rc": 124, "err": "gh: timed out\n"}]
    rc, _ = _apply(lr, pats, world, manifest)
    out, err = capsys.readouterr()
    refused(rc, out, err, "gh-failed")
    rc, _ = _apply(lr, pats, world, manifest)
    out, err = capsys.readouterr()
    assert rc == 0, (out, err)
    assert "issue 1 body: already applied" in out
    writes = [tuple(w) for w in world["writes"]]
    assert len(writes) == 4 and len(set(writes)) == 4


def test_c6_rate_limit_retries_honour_retry_after(lr, pats, tmp_path, capsys):
    world = _apply_world()
    manifest = _dry(lr, pats, tmp_path, world, capsys)
    world["fail"] = [
        {"match": "-X PATCH repos/acme/demo/issues/1 ", "nth": 1, "rc": 1,
         "out": "HTTP/2.0 429 Too Many Requests\nRetry-After: 7\n\n{}", "err": "gh: HTTP 429\n"},
        {"match": "-X PATCH repos/acme/demo/issues/comments/501", "nth": 1, "rc": 1,
         "out": "HTTP/2.0 403 Forbidden\nRetry-After: 900\n\n{\"message\": \"secondary rate limit\"}",
         "err": "gh: HTTP 403\n"}]
    sleeps = []
    rc, _ = _apply(lr, pats, world, manifest, sleeps=sleeps)
    out, err = capsys.readouterr()
    assert rc == 0, (out, err)
    assert 7 in sleeps and 300 in sleeps and 900 not in sleeps


def test_c6_rate_limit_retries_are_bounded(lr, pats, tmp_path, capsys):
    world = _apply_world()
    manifest = _dry(lr, pats, tmp_path, world, capsys)
    world["fail"] = [{"match": "-X PATCH repos/acme/demo/issues/1 ", "rc": 1,
                      "out": "HTTP/2.0 429 Too Many Requests\n\n{}", "err": "gh: HTTP 429\n"}]
    sleeps = []
    rc, _ = _apply(lr, pats, world, manifest, sleeps=sleeps)
    out, err = capsys.readouterr()
    refused(rc, out, err, "rate-limited")
    assert sleeps.count(60) == 3


def test_c6_max_writes_stops_and_names_the_rest(lr, pats, tmp_path, capsys):
    world = _apply_world()
    manifest = _dry(lr, pats, tmp_path, world, capsys)
    rc, _ = _apply(lr, pats, world, manifest, "--max-writes", "1")
    out, err = capsys.readouterr()
    assert rc == 1, (out, err)
    assert "3 edit(s) remain" in out
    assert len(world["writes"]) == 1


def test_c6_verify_mismatch(lr, pats, tmp_path, capsys):
    world = _apply_world()
    manifest = _dry(lr, pats, tmp_path, world, capsys)
    world["mangle"] = "\r\n"
    rc, _ = _apply(lr, pats, world, manifest)
    out, err = capsys.readouterr()
    assert rc == 1, (out, err)
    assert "verify mismatch" in out


def _rewrite_manifest(path, change):
    lines = path.read_text(encoding="utf-8").splitlines()
    i = max(k for k, line in enumerate(lines) if line == "# manifest")
    m = json.loads(lines[i + 1])
    change(m)
    lines[i + 1] = json.dumps(m)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _hand_edit(m):
    e = m["edits"][0]
    e["after"] = e["after"] + " (see #7)"
    e["sha256_after"] = hashlib.sha256(e["after"].encode("utf-8")).hexdigest()


@pytest.mark.parametrize("change,code", [
    (_hand_edit, "manifest-invariant"),
    (lambda m: m["edits"][0].update(sha256_before="0" * 64), "manifest-invariant"),
    (lambda m: m.update(repo="other/repo"), "manifest-repo"),
    (lambda m: m.update(schema="something/else"), "manifest-schema"),
    (lambda m: m["edits"][0].update(field="title"), "manifest-schema"),
])
def test_c6_a_bad_manifest_is_refused_whole(lr, pats, tmp_path, capsys, change, code):
    world = _apply_world()
    manifest = _dry(lr, pats, tmp_path, world, capsys)
    _rewrite_manifest(manifest, change)
    rc, fake = _apply(lr, pats, world, manifest)
    out, err = capsys.readouterr()
    refused(rc, out, err, code)
    assert fake.gh_calls() == []


def test_c6_a_truncated_or_missing_manifest_is_refused(lr, pats, tmp_path, capsys):
    world = _apply_world()
    manifest = _dry(lr, pats, tmp_path, world, capsys)
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(text[:text.index("# manifest")], encoding="utf-8")
    rc, fake = _apply(lr, pats, world, manifest)
    out, err = capsys.readouterr()
    refused(rc, out, err, "manifest-unreadable")
    rc, fake = _apply(lr, pats, world, tmp_path / "ops" / "missing.md")
    out, err = capsys.readouterr()
    refused(rc, out, err, "manifest-unreadable")
    assert fake.gh_calls() == []


def test_c6_cli_apply_one_edit(tmp_path, pats):
    world = mkworld(items=[item(1, body="see " + SLUG + " there")])
    (tmp_path / "ops").mkdir()
    out_file = tmp_path / "ops" / "dry.md"
    proc, _ = cli(tmp_path, ["rewrite", "--patterns", pats, "--repo", REPO, "--out", out_file],
                  world=world)
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    proc, log = cli(tmp_path, ["rewrite", "--apply", "--from", out_file, "--patterns", pats,
                               "--repo", REPO])
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "issue 1 body: verified" in proc.stdout
    assert world_after(tmp_path)["lists"]["issues"][0]["body"] == "see " + REPO_WORDS + " there"
    no_leak(proc.stdout, proc.stderr)


# ============================================================================== C8 the CLI seam

def test_c8_cli_seam(tmp_path, monkeypatch):
    record = tmp_path / "record.txt"
    fake_python = tmp_path / "fake-python"
    fake_python.write_text("#!/bin/sh\nprintf '%%s\\n' \"$@\" > %s\n" % shlex.quote(str(record)),
                           encoding="utf-8")
    fake_python.chmod(0o755)
    monkeypatch.setenv("LEAK_REFS_TEST_PYTHON", str(fake_python))
    cli(tmp_path, ["text", "-"], stdin="")
    assert record.exists(), "the CLI helper ignored LEAK_REFS_TEST_PYTHON"
    assert record.read_text(encoding="utf-8").splitlines()[:2] == ["tools/leak_refs.py", "text"]


def test_c8_cli_help_exits_zero(tmp_path):
    proc, _ = cli(tmp_path, ["--help"])
    assert proc.returncode == 0, proc.stderr
    assert "scan" in proc.stdout and "rewrite" in proc.stdout


# ============================================================================== the runbook

def test_runbook_gestures_parse(lr):
    """Every `python3 tools/leak_refs.py` line in the runbook parses with the tool's own parser, and
    together they show every verb, so an empty or drifted page fails."""
    assert RUNBOOK.exists(), "docs/publish-runbook.md is missing"
    parser = lr.build_parser()
    seen = set()
    for line in RUNBOOK.read_text(encoding="utf-8").splitlines():
        for m in re.finditer(r"python3 tools/leak_refs\.py ([^|#;&>`]*)", line):
            args = shlex.split(m.group(1))
            parser.parse_args(args)                             # SystemExit on a bad gesture
            if args[0] == "rewrite" and "--apply" in args:
                assert "--from" in args, line
                seen.add("rewrite --apply --from")
            else:
                seen.add(args[0])
    assert {"scan", "tree", "text", "rewrite", "rewrite --apply --from"} <= seen, seen
