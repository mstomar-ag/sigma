"""#1303: a park whose GraphQL label swap fails must still land (REST fallback), and must not exit 0
when it cannot.

Drives the documented gesture exactly as written -- `loop.py record <N> parked "<why>" --qkind <kind>`
-- against a stateful PATH-installed fake `gh` (the same pattern as test_label_bootstrap.py). The fake
rate-limits EVERY `api graphql` call (the exhaustion episode) and models REST label writes; the
`FAKE_GH_REST_LABELS` switch makes those fail too. Measured RED against the pre-fix code: exit 0 and
the issue still carried `sdlc:goal`.
"""
import json
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOOP = ROOT / "skills" / "sigma-loop" / "scripts" / "loop.py"
REPO = "acme/app"
posix_only = pytest.mark.skipif(os.name == "nt", reason="PATH fake `gh` is a shebang script")

_FAKE_GH = r'''
import json, os, sys, urllib.parse
STATE = os.environ["FAKE_GH_STATE"]
with open(os.environ["FAKE_GH_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
s = json.load(open(STATE))
argv = sys.argv[1:]
def save():
    json.dump(s, open(STATE, "w"))
if argv[:2] == ["repo", "view"]:
    print(json.dumps({"owner": {"login": "acme"}, "name": "app"})); sys.exit(0)
if argv[:1] == ["label"]:
    sys.stderr.write('label already exists\n'); sys.exit(1)
if argv[:1] == ["api"]:
    ep = argv[1].replace("{owner}/{repo}", "acme/app")
    method = argv[argv.index("--method") + 1].upper() if "--method" in argv else "GET"
    if ep == "graphql":
        sys.stderr.write("GraphQL: API rate limit exceeded for user (HTTP 403)\n"); sys.exit(1)
    base = "repos/acme/app/"
    if ep == base + "labels":
        print(json.dumps([{"name": n} for n in s["all_labels"]])); sys.exit(0)
    if ep.startswith(base + "issues/") and "/labels" in ep:
        if s.get("rest_labels_fail"):
            sys.stderr.write("HTTP 403: API rate limit exceeded (secondary)\n"); sys.exit(1)
        if method == "POST":
            for i, a in enumerate(argv):
                if a == "-f" and argv[i + 1].startswith("labels[]="):
                    n = argv[i + 1][len("labels[]="):]
                    if n not in s["labels"]: s["labels"].append(n)
            save(); print("[]"); sys.exit(0)
        if method == "DELETE":
            n = urllib.parse.unquote(ep.rsplit("/", 1)[1])
            s["labels"] = [l for l in s["labels"] if l != n]
            save(); print("[]"); sys.exit(0)
    if ep.endswith("/comments") and method == "POST":
        s["comments"] += 1; save(); print(json.dumps({"id": 1, "html_url": "x"})); sys.exit(0)
    if ep == base + "issues/42":
        print(json.dumps({"number": 42, "body": "", "title": "t", "state": "open",
                          "labels": [{"name": l} for l in s["labels"]]})); sys.exit(0)
    print("{}"); sys.exit(0)
sys.stderr.write("fake gh: unmodeled %r\n" % (argv,)); sys.exit(1)
'''

ALL = ["sdlc:goal", "sdlc:in-progress", "sdlc:parked", "sdlc:blocked", "sdlc:blocking",
       "sdlc:needs-label", "sdlc:designed", "sdlc:needs-unit", "sdlc:needs-triage",
       "priority:P0", "priority:P1", "priority:P2", "priority:P3"]


def _world(tmp_path, rest_fail=False):
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text("#!%s\n%s" % (sys.executable, _FAKE_GH), encoding="utf-8"); gh.chmod(0o755)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"labels": ["sdlc:goal", "sdlc:in-progress"], "all_labels": ALL,
                                 "comments": 0, "rest_labels_fail": rest_fail}))
    log = tmp_path / "log.jsonl"; log.write_text("")
    repo = tmp_path / "repo"; repo.mkdir()
    sdlc = repo / ".sdlc"; sdlc.mkdir()
    (sdlc / "config.json").write_text(json.dumps(
        {"discovery": {"source": "github", "github": {"repo": REPO}}}))
    py_dir = str(pathlib.Path(sys.executable).resolve().parent)
    env = {"PATH": os.pathsep.join([str(bin_dir), py_dir, "/usr/bin", "/bin"]), "HOME": str(tmp_path),
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
           "PYTHONDONTWRITEBYTECODE": "1", "FAKE_GH_STATE": str(state), "FAKE_GH_LOG": str(log)}
    subprocess.run(["git", "init", "-q", str(repo)], env=env, check=True)
    return {"repo": repo, "sdlc": sdlc, "env": env, "state": state, "log": log}


def _record(w, status="parked"):
    # the documented gesture: loop.py record <N> parked "<why>" --qkind <kind>
    return subprocess.run([sys.executable, str(LOOP), "record", str(w["sdlc"]), "42", status,
                           "needs a human call", "--qkind", "needs_decision"],
                          cwd=str(w["repo"]), env=w["env"], capture_output=True, text=True)


def _state(w):
    return json.loads(w["state"].read_text())


def _calls(w):
    return [json.loads(l) for l in w["log"].read_text().splitlines() if l.strip()]


def _rest_label_writes(w):
    return [c for c in _calls(w) if c[:1] == ["api"] and "/labels" in c[1] and "--method" in c
            and c[c.index("--method") + 1].upper() in ("POST", "DELETE")]


@posix_only
def test_park_under_graphql_exhaustion_lands_via_rest(tmp_path):
    """THE CONTROL: GraphQL always rate-limited, REST healthy -> the issue ends parked and off the pick list."""
    w = _world(tmp_path)
    p = _record(w)
    assert p.returncode == 0, p.stdout + p.stderr
    labels = _state(w)["labels"]
    assert "sdlc:parked" in labels, labels
    assert "sdlc:goal" not in labels and "sdlc:in-progress" not in labels, labels


@posix_only
def test_park_whose_rest_fallback_also_fails_exits_nonzero_with_one_clear_message(tmp_path):
    w = _world(tmp_path, rest_fail=True)
    p = _record(w)
    assert p.returncode != 0, p.stdout + p.stderr
    msgs = [l for l in p.stderr.splitlines() if "park label" in l and "still pickable" in l]
    assert len(msgs) == 1, p.stderr
    assert " 42:" in msgs[0]
    assert "sdlc:goal" in _state(w)["labels"]
    assert _state(w)["comments"] >= 1                       # the reason comment still landed


@posix_only
def test_failed_transition_is_unchanged_no_rest_fallback(tmp_path):
    """`failed` shares _offboard but is not the park transition: byte-identical, no REST label write."""
    w = _world(tmp_path)
    p = _record(w, "failed")
    assert p.returncode == 0, p.stdout + p.stderr
    assert _rest_label_writes(w) == []
    assert "still pickable" not in p.stderr


def test_healthy_swap_never_touches_the_rest_fallback():
    """No-failure path: a landed swap makes no REST label write and returns True."""
    sys.path.insert(0, str(ROOT / "skills" / "sigma-loop" / "scripts"))
    import sources
    calls = []
    gh = sources.GitHubSource({"discovery": {"source": "github", "github": {"repo": REPO}}},
                              run=lambda a: calls.append(list(a)) or "{}")
    gh._labels_ready = True
    gh._swap_labels_best_effort = lambda *a, **k: True
    gh._set_board_status = lambda *a, **k: True
    gh.note = lambda *a, **k: None
    assert gh.park("42", "why") is True
    assert calls == []
