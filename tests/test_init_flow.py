"""#236: `/sigma-init` is the ONE entry point -- `skills/sigma-init/scripts/init_flow.py` -- and
`/sigma-setup` is its alias.

The flow integrates the sibling goals, it does not re-implement them: preflight (#229), mode, the
verify command (#228), and in github mode the labels (#230), the board OFFER (#235), `assignee: @me`
and the ledger question; then a one-screen summary naming the next command.

Every subprocess test drives the documented gesture (`python3 init_flow.py <repo> [flags]`) against a
PATH-installed fake `gh` that logs every call -- the pattern `test_label_bootstrap.py` uses. A GitHub
`origin` must never reach the network, so `GIT_ALLOW_PROTOCOL=file` makes any https transport fail
fast (preflight reports the base branch CANNOT VERIFY), and pushes are redirected to a local bare
repository with `url.<bare>.pushInsteadOf` (so `git remote get-url origin` still reads the GitHub URL
mode detection needs). Skipped on Windows only where a shebang `gh` is needed; the in-process tests
below run everywhere.

THE CONTROL (AGENTS.md): `test_old_github_gesture_cannot_pick_the_labelled_issue` runs the OLD
`sdlc_init.py --github` gesture on a fresh repo and shows `loop.py next` does not pick the issue a
human labelled `sdlc:goal` (the config still says local-goals); the new flow's test picks it.
"""
import importlib.util
import json
import os
import pathlib
import subprocess
import sys

import pytest

import resource_mode

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-init" / "scripts"
FLOW = SCRIPTS / "init_flow.py"
SDLC_INIT = SCRIPTS / "sdlc_init.py"
SETUP = ROOT / "skills" / "sigma-setup" / "scripts" / "setup.py"
LOOP_DIR = ROOT / "skills" / "sigma-loop" / "scripts"
LOOP = LOOP_DIR / "loop.py"
PREFLIGHT = SCRIPTS / "preflight.py"
REPO = "acme/app"
GITHUB_URL = "https://github.com/%s.git" % REPO

LABELS = ("sdlc:goal", "sdlc:in-progress", "sdlc:parked", "sdlc:blocked", "sdlc:blocking",
          "sdlc:needs-label", "sdlc:designed", "sdlc:needs-unit",
          "sdlc:needs-triage", "priority:P0", "priority:P1", "priority:P2", "priority:P3")

posix_only = pytest.mark.skipif(os.name == "nt", reason="PATH fake `gh` is a shebang script")


_FAKE_GH = r'''
import json, os, re, sys

STATE, LOG, UNHANDLED = (os.environ["FAKE_GH_STATE"], os.environ["FAKE_GH_LOG"],
                         os.environ["FAKE_GH_UNHANDLED"])


def load():
    with open(STATE, encoding="utf-8") as f:
        return json.load(f)


def save(s):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(s, f)


def unhandled(argv, why):
    with open(UNHANDLED, "a", encoding="utf-8") as f:
        f.write(json.dumps({"argv": argv, "why": why}) + "\n")
    sys.stderr.write("fake gh: unhandled (%s): %r\n" % (why, argv))
    sys.exit(1)


def fields(argv):
    out, i = {}, 0
    while i < len(argv):
        if argv[i] in ("-f", "-F", "--raw-field", "--field") and i + 1 < len(argv):
            k, _, v = argv[i + 1].partition("="); out[k] = v; i += 2; continue
        i += 1
    return out


def flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else None


def issue_json(n, i):
    return {"number": int(n), "title": i.get("title", ""), "body": i.get("body", ""),
            "state": i.get("state", "open"), "labels": [{"name": l} for l in i["labels"]],
            "assignees": [{"login": a} for a in i.get("assignees", [])],
            "html_url": "https://github.com/%s/issues/%s" % (s["repo"], n)}


argv = sys.argv[1:]
with open(LOG, "a", encoding="utf-8") as f:
    f.write(json.dumps(argv) + "\n")
s = load()
repo = s["repo"]
if argv[:2] == ["auth", "status"]:
    print("github.com\n  Logged in to github.com account fake (keyring)\n  - Active account: true\n"
          "  - Token: gho_****\n  - Token scopes: 'read:org', 'repo', 'workflow'")
    sys.exit(0)
if argv[:2] == ["label", "create"]:
    if flag(argv, "--repo") not in (None, repo) or "--force" in argv:
        unhandled(argv, "label create: wrong repo or --force")
    if s.get("refuse_label_create"):
        sys.stderr.write("HTTP 403: Resource not accessible by integration\n"); sys.exit(1)
    name = argv[2]
    if name in s["labels"]:
        sys.stderr.write('label with name "%s" already exists\n' % name); sys.exit(1)
    s["labels"][name] = {"color": flag(argv, "--color") or ""}
    save(s); sys.exit(0)
if argv[:2] == ["issue", "edit"]:
    i = s["issues"][argv[2]]
    for k in ("--add-label",):
        for lab in (flag(argv, k) or "").split(","):
            if lab and lab not in i["labels"]:
                i["labels"].append(lab)
    for lab in (flag(argv, "--remove-label") or "").split(","):
        if lab in i["labels"]:
            i["labels"].remove(lab)
    save(s); sys.exit(0)
if argv[:2] == ["issue", "list"]:
    want = flag(argv, "--label")
    print(json.dumps([issue_json(n, i) for n, i in sorted(s["issues"].items())
                      if i.get("state", "open") == (flag(argv, "--state") or "open")
                      and (not want or want in i["labels"])])); sys.exit(0)
if argv[:2] == ["issue", "view"]:
    i = s["issues"][argv[2]]
    if flag(argv, "--jq") == ".state":
        print(i.get("state", "open").upper()); sys.exit(0)
    print(json.dumps(issue_json(argv[2], i))); sys.exit(0)
if argv[:3] == ["api", "graphql", "-f"] and argv[3].startswith("query="):
    doc = argv[3][len("query="):]
    names = sorted(s["labels"])
    m = re.search(r"issue\(number: (\d+)\) \{ id \}", doc)
    if m:
        print(json.dumps({"data": {"repository": {"issue": {"id": "I_%s" % m.group(1)}}}})); sys.exit(0)
    found = re.findall(r'(a\d+): label\(name: "([^"]+)"\)', doc)
    if found:
        repo_d = {alias: ({"id": "L_%d" % names.index(n), "name": n} if n in names else None)
                  for alias, n in found}
        print(json.dumps({"data": {"repository": repo_d}})); sys.exit(0)
    if doc.startswith("mutation"):
        for verb, node, ids in re.findall(r'(addLabelsToLabelable|removeLabelsFromLabelable)\(input: '
                                          r'\{labelableId: "I_(\d+)", labelIds: \[([^\]]*)\]', doc):
            i = s["issues"][node]
            for lid in re.findall(r'"L_(\d+)"', ids):
                lab = names[int(lid)]
                if verb.startswith("add") and lab not in i["labels"]:
                    i["labels"].append(lab)
                elif verb.startswith("remove") and lab in i["labels"]:
                    i["labels"].remove(lab)
        save(s); print(json.dumps({"data": {}})); sys.exit(0)
    unhandled(argv, "unmodelled graphql")
if argv[:2] in (["issue", "comment"],):
    s.setdefault("comments", []).append(argv); save(s); sys.exit(0)
if argv[:2] == ["issue", "close"]:
    s["issues"][argv[2]]["state"] = "closed"; save(s); sys.exit(0)
if argv and argv[0] == "api":
    endpoint = argv[1].replace("{owner}/{repo}", repo).split("?")[0]
    method = (flag(argv, "--method") or flag(argv, "-X") or "GET").upper()
    mw = re.fullmatch(r"repos/%s/issues/(\d+)(?:/(comments|labels)(?:/(.+))?)?" % re.escape(repo), endpoint)
    if mw and method != "GET" and mw.group(1) in s["issues"]:
        # #895 slice 3a: the REST issue WRITES (comment / close / label add+remove).
        i, f = s["issues"][mw.group(1)], fields(argv)
        if mw.group(2) == "comments" and method == "POST":
            s.setdefault("comments", []).append(["api", endpoint, f.get("body", "")])
        elif mw.group(2) is None and method == "PATCH":
            if f.get("state") == "closed":
                i["state"] = "closed"
            if "body" in f:
                i["body"] = f["body"]
        elif mw.group(2) == "labels" and method == "POST":
            for a, b in zip(argv, argv[1:]):
                if a == "-f" and b.startswith("labels[]=") and b[9:] not in i["labels"]:
                    i["labels"].append(b[9:])
        elif mw.group(2) == "labels" and method == "DELETE" and mw.group(3):
            from urllib.parse import unquote
            if unquote(mw.group(3)) in i["labels"]:
                i["labels"].remove(unquote(mw.group(3)))
        else:
            unhandled(argv, "unmodelled REST issue write")
        save(s); print("{}"); sys.exit(0)
    if endpoint == "graphql" or method != "GET":
        unhandled(argv, "only REST GETs are modelled for api")
    if endpoint == "user":
        print("fake" if flag(argv, "--jq") else json.dumps({"login": "fake"})); sys.exit(0)
    if endpoint == "users/" + repo.split("/")[0]:
        print("Organization" if flag(argv, "--jq") else json.dumps({"type": "Organization"}))
        sys.exit(0)
    f = fields(argv)
    per_page, page = int(f.get("per_page", "30")), int(f.get("page", "1"))
    if endpoint == "repos/%s/labels" % repo:
        items = [{"name": n, "color": v.get("color", "")} for n, v in s["labels"].items()]
    elif endpoint == "repos/%s/issues" % repo:
        want = set(f["labels"].split(",")) if f.get("labels") else set()
        who = f.get("assignee")
        items = [issue_json(n, i) for n, i in sorted(s["issues"].items(), key=lambda kv: int(kv[0]))
                 if i.get("state", "open") == f.get("state", "open")
                 and want.issubset(set(i["labels"]))
                 and (not who or who in i.get("assignees", []))]
    elif endpoint.startswith("repos/%s/issues/" % repo) and endpoint.count("/") == 4:
        n = endpoint.rsplit("/", 1)[1]
        if n not in s["issues"]:
            sys.stderr.write("HTTP 404: Not Found\n"); sys.exit(1)
        print(json.dumps(issue_json(n, s["issues"][n]))); sys.exit(0)
    else:
        unhandled(argv, "unmodelled endpoint")
    print(json.dumps(items[(page - 1) * per_page: page * per_page])); sys.exit(0)
unhandled(argv, "unmodelled verb")
'''


def _git(repo, *args, env=None):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=env)


def _world(tmp_path, issues=None, origin=GITHUB_URL, refuse=False, commit=True):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text("#!%s\n%s" % (sys.executable, _FAKE_GH), encoding="utf-8")
    gh.chmod(0o755)
    state = tmp_path / "gh_state.json"
    state.write_text(json.dumps({"repo": REPO, "labels": {}, "issues": issues or {},
                                 "refuse_label_create": refuse}), encoding="utf-8")
    log = tmp_path / "gh_log.jsonl"
    log.write_text("", encoding="utf-8")
    unh = tmp_path / "gh_unhandled.jsonl"
    unh.write_text("", encoding="utf-8")
    py_dir = str(pathlib.Path(sys.executable).resolve().parent)
    env = {"PATH": os.pathsep.join([str(bin_dir), py_dir, "/usr/bin", "/bin"]),
           "HOME": str(tmp_path), "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
           "GIT_ALLOW_PROTOCOL": "file", "GIT_TERMINAL_PROMPT": "0",
           "PYTHONDONTWRITEBYTECODE": "1", "FAKE_GH_STATE": str(state), "FAKE_GH_LOG": str(log),
           "FAKE_GH_UNHANDLED": str(unh),
           **{k: os.environ[k] for k in ("CLAUDE_CONFIG_DIR", "CODEX_HOME") if k in os.environ}}
    resource_mode.carry(env)    # #1759: a scrubbed env must not let the picker sample the real machine
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main", env=env)
    _git(repo, "config", "user.email", "t@t.test", env=env)
    _git(repo, "config", "user.name", "t", env=env)
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(bare), env=env)
    if origin:
        _git(repo, "remote", "add", "origin", origin, env=env)
        if origin.startswith("https://"):
            _git(repo, "config", "url.%s.pushInsteadOf" % bare.as_uri(), origin, env=env)
    if commit:
        (repo / "README.md").write_text("hello\n")
        _git(repo, "add", "README.md", env=env)
        _git(repo, "commit", "-q", "-m", "initial", env=env)
        if origin:
            _git(repo, "push", "-q", "origin", "main", env=env)
    return {"repo": repo, "sdlc": repo / ".sdlc", "env": env, "state": state, "log": log,
            "unhandled": unh, "bare": bare}


def _run(w, argv):
    return subprocess.run([sys.executable, *[str(a) for a in argv]], cwd=str(w["repo"]),
                          env=w["env"], capture_output=True, text=True, timeout=120)


def _calls(w):
    return [json.loads(l) for l in w["log"].read_text(encoding="utf-8").splitlines() if l.strip()]


def _cfg(w):
    return json.loads((w["sdlc"] / "config.json").read_text(encoding="utf-8"))


def _phase_record_off(sdlc):
    """#684: this test is about something other than the SDLC phase record (that has its own
    tests/test_phase_record_gate.py), so it uses the documented lever: `gates.phase_record.enabled: false`."""
    path = pathlib.Path(sdlc) / "config.json"
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg.setdefault("gates", {})["phase_record"] = {"enabled": False}
    path.write_text(json.dumps(cfg), encoding="utf-8")


def _gh_state(w):
    return json.loads(w["state"].read_text(encoding="utf-8"))


def _cmd_file(w, command):
    path = w["repo"].parent / "verify-cmd.txt"
    path.write_text(command + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------- the example goal (no scaffold)


def test_the_scaffolded_example_goal_is_never_picked(tmp_path):
    """Pins the SCAFFOLDED file's front-matter `status` field itself (the review's M22: a substring
    search also matched the body's own mention of `status: proposed`, so a mutant flipping the
    front matter to pending was never caught). Exactly one `status:` key, and it is `proposed`."""
    si = _load_flow()._si
    si.scaffold(str(tmp_path))
    text = (tmp_path / ".sdlc" / "goals" / "0001-example.md").read_text(encoding="utf-8")
    assert text.startswith("---\n"), text[:40]
    front = text[4:].split("\n---\n", 1)[0].splitlines()
    status = [l.split(":", 1)[1].strip() for l in front if l.split(":", 1)[0].strip() == "status"]
    assert status == ["proposed"], front


@posix_only
def test_a_fresh_init_leaves_nothing_pickable_without_demo(tmp_path):
    w = _world(tmp_path, origin=None)
    p = _run(w, [FLOW, w["repo"], "--mode", "local-goals", "--local-only", "--no-verify"])
    assert p.returncode == 0, p.stdout + p.stderr
    nxt = _run(w, [LOOP, "next", ".sdlc"])
    assert "0001-example" not in nxt.stdout, nxt.stdout
    assert nxt.stdout.strip().startswith("DONE"), nxt.stdout + nxt.stderr


# ---------------------------------------------------------------- local-goals end to end


@posix_only
def test_local_goals_flow_reaches_done(tmp_path):
    w = _world(tmp_path, origin=None)
    p = _run(w, [FLOW, w["repo"], "--mode", "local-goals", "--local-only", "--demo", "--no-verify"])
    out = p.stdout + p.stderr
    assert p.returncode == 0, out
    for section in ("1/5 preflight", "2/5 mode", "3/5 verify", "4/5 github", "5/5 summary"):
        assert section in p.stdout, p.stdout
    assert "local-goals" in p.stdout and "/sigma-loop" in p.stdout
    cfg = _cfg(w)
    assert cfg["discovery"]["source"] == "local-goals"
    assert cfg["work"]["enabled"] is False and cfg["work"].get("_enabled_why")
    assert _calls(w) == []                                # local-goals mode: no gh call at all
    goal = _run(w, [LOOP, "next", ".sdlc"]).stdout.strip()
    assert goal.endswith("0000-demo.md"), goal
    demo = w["sdlc"] / "goals" / "0000-demo.md"
    demo.write_text(demo.read_text().replace("verify_command: python3 ",
                                             "verify_command: %s " % sys.executable)
                    .replace("verify_command: python ", "verify_command: %s " % sys.executable))
    # This test intentionally executes the shipped demo command after the
    # operator chose --no-verify. Model the later, explicit local trust choice.
    _git(w["repo"], "config", "--local", "sigma.allowRepositoryShellCommands", "true", env=w["env"])
    (w["repo"] / "sigma-demo.md").write_text("Sigma ran this goal.\n")
    v = _run(w, [LOOP, "verify", ".sdlc", ".sdlc/goals/0000-demo.md"])
    assert v.returncode == 0, v.stdout + v.stderr
    _phase_record_off(w["sdlc"])
    d = _run(w, [LOOP, "record", ".sdlc", ".sdlc/goals/0000-demo.md", "done"])
    assert d.returncode == 0, d.stdout + d.stderr
    assert "status: done" in demo.read_text()


@posix_only
def test_local_only_choice_stops_the_work_enabled_nag(tmp_path):
    w = _world(tmp_path, origin=None)
    assert _run(w, [FLOW, w["repo"], "--mode", "local-goals", "--local-only", "--no-verify"]).returncode == 0
    s = _run(w, [LOOP, "start", ".sdlc", "--session-pid", str(os.getpid())])
    assert "work.enabled is off" not in s.stderr, s.stderr
    # CONTROL: the same config without the recorded decision still warns -- and names init.
    cfg = _cfg(w)
    del cfg["work"]["_enabled_why"]
    (w["sdlc"] / "config.json").write_text(json.dumps(cfg))
    s = _run(w, [LOOP, "start", ".sdlc", "--session-pid", str(os.getpid())])
    assert "work.enabled is off" in s.stderr and "/sigma-init" in s.stderr, s.stderr
    assert "/sigma-setup" not in s.stderr


# ---------------------------------------------------------------- github end to end


def _labelled_issue():
    return {"1": {"labels": [], "title": "Add a greeting", "assignees": ["fake"]}}


@posix_only
def test_github_flow_makes_only_label_writes_offers_the_board_and_reaches_done(tmp_path):
    w = _world(tmp_path, issues=_labelled_issue())
    cmd = _cmd_file(w, '"%s" -c "import sys; sys.exit(0)"' % sys.executable)
    p = _run(w, [FLOW, w["repo"], "--yes", "--verify-command-file", cmd])
    out = p.stdout + p.stderr
    assert p.returncode == 0, out
    cfg = _cfg(w)
    assert cfg["discovery"]["source"] == "github"            # --yes: detected from origin
    assert cfg["discovery"]["github"]["repo"] == REPO
    assert cfg["discovery"]["github"]["assignee"] == "@me"
    assert cfg["ledger"]["enabled"] is False                 # --yes never turns the ledger on
    assert not (cfg["discovery"]["github"].get("project") or {}).get("number")
    assert set(_gh_state(w)["labels"]) == set(LABELS)
    # the exact gh calls: preflight reads + REST label list + label writes; nothing else
    kinds = []
    for c in _calls(w):
        if c[:2] == ["auth", "status"]:
            kinds.append("auth")
        elif c[0] == "api" and c[1] == "users/acme":
            kinds.append("owner")
        elif c[0] == "api" and c[1] == "repos/%s/labels" % REPO:
            kinds.append("labels-get")
        elif c[:2] == ["label", "create"]:
            kinds.append("label-create")
        else:
            kinds.append("OTHER %r" % c)
    assert [k for k in kinds if k.startswith("OTHER")] == [], kinds
    assert kinds.count("label-create") == len(LABELS)
    assert not any("project" in " ".join(c) or "graphql" in c for c in _calls(w))
    assert "OFFER" in p.stdout and "board_setup.py" in p.stdout and "--board yes" in p.stdout
    assert w["unhandled"].read_text() == ""

    # a human triages the issue; the loop picks it, verify passes, done is recorded
    s = _gh_state(w)
    s["issues"]["1"]["labels"] = ["sdlc:goal"]
    w["state"].write_text(json.dumps(s))
    nxt = _run(w, [LOOP, "next", ".sdlc"])
    assert nxt.stdout.strip() == "1", nxt.stdout + nxt.stderr
    # init turns verify.enforce on, so verify refuses until the goal has an acceptance record
    s = _gh_state(w)
    s["issues"]["1"]["body"] = "## Done when\n- [ ] It works.\n- [ ] It refuses bad input.\n- [ ] It recovers.\n"
    w["state"].write_text(json.dumps(s))
    rec = _run(w, [LOOP_DIR / "acceptance.py", "record", ".sdlc", "1"])
    assert rec.returncode == 0, rec.stdout + rec.stderr
    v = _run(w, [LOOP, "verify", ".sdlc", "1"])
    assert v.returncode == 0, v.stdout + v.stderr
    _phase_record_off(w["sdlc"])
    d = _run(w, [LOOP, "record", ".sdlc", "1", "done"])
    assert d.returncode == 0, d.stdout + d.stderr
    assert _gh_state(w)["issues"]["1"]["state"] == "closed"
    assert w["unhandled"].read_text() == ""                  # every loop call was modelled, too


@posix_only
def test_github_rerun_is_idempotent(tmp_path):
    w = _world(tmp_path)
    first = _run(w, [FLOW, w["repo"], "--mode", "github", "--ledger", "no", "--board", "no",
                     "--no-verify"])
    assert first.returncode == 0, first.stdout + first.stderr
    before = _cfg(w)
    w["log"].write_text("")
    again = _run(w, [FLOW, w["repo"]])                        # answers are remembered
    assert again.returncode == 0, again.stdout + again.stderr
    assert _cfg(w) == before
    assert not any(c[:2] == ["label", "create"] for c in _calls(w))
    assert "OFFER" not in again.stdout                        # the declined board stays declined
    # ...because every answer was REMEMBERED, not because the github step was skipped
    assert "[ok] mode: github" in again.stdout and "board: declined" in again.stdout, again.stdout
    assert "[ask]" not in again.stdout
    assert any(c[:2] == ["api", "repos/%s/labels" % REPO] for c in _calls(w))   # labels re-checked


@posix_only
def test_github_label_failure_exits_nonzero_with_the_resume_command(tmp_path):
    w = _world(tmp_path, refuse=True)
    p = _run(w, [FLOW, w["repo"], "--mode", "github", "--ledger", "no", "--no-verify"])
    assert p.returncode == 1, p.stdout + p.stderr
    assert "sdlc:goal: FAILED" in p.stdout + p.stderr
    assert "Resume:" in p.stdout and "init_flow.py" in p.stdout


@posix_only
def test_github_mode_without_a_repo_is_refused(tmp_path):
    w = _world(tmp_path, origin=None)
    p = _run(w, [FLOW, w["repo"], "--mode", "github"])
    assert p.returncode == 2, p.stdout + p.stderr
    assert "--repo" in p.stdout + p.stderr
    cfg = _cfg(w) if (w["sdlc"] / "config.json").exists() else {}
    assert (cfg.get("discovery") or {}).get("source") != "github"


@posix_only
def test_unanswered_questions_are_printed_with_their_flags(tmp_path):
    """Codex / Cursor: no interactive question -- the flow prints each open one with its flag."""
    w = _world(tmp_path)
    p = _run(w, [FLOW, w["repo"]])
    assert p.returncode == 0, p.stdout + p.stderr
    assert "[ask] mode" in p.stdout and "--mode github" in p.stdout
    assert "default: github" in p.stdout                       # origin is GitHub
    assert _cfg(w)["discovery"]["source"] == "local-goals"      # nothing chosen for the user
    assert not any(c[:2] == ["label", "create"] for c in _calls(w))


@posix_only
def test_no_remote_is_a_work_question_not_a_failure(tmp_path):
    w = _world(tmp_path, origin=None)
    p = _run(w, [FLOW, w["repo"], "--mode", "local-goals", "--no-verify"])
    assert p.returncode == 0, p.stdout + p.stderr
    assert "DECISION" in p.stdout and "[ask] work" in p.stdout and "--local-only" in p.stdout
    assert _cfg(w)["work"]["enabled"] is True                   # nothing flipped for the user
    # a blocking problem that is NOT that open question still fails the run, with the resume line
    (tmp_path / "nocommit").mkdir()
    w2 = _world(tmp_path / "nocommit", origin=None, commit=False)
    p = _run(w2, [FLOW, w2["repo"], "--mode", "local-goals", "--local-only", "--no-verify"])
    assert p.returncode == 1 and "Resume:" in p.stdout, p.stdout + p.stderr


# ---------------------------------------------------------------- the control


@posix_only
def test_old_github_gesture_cannot_pick_the_labelled_issue(tmp_path):
    issues = {"1": {"labels": ["sdlc:goal"], "title": "Add a greeting", "assignees": ["fake"]}}
    w = _world(tmp_path, issues=issues)
    old = _run(w, [SDLC_INIT, w["repo"], "--github"])
    assert old.returncode == 0, old.stdout + old.stderr
    nxt = _run(w, [LOOP, "next", ".sdlc"])
    assert nxt.stdout.strip() != "1", nxt.stdout
    assert "0001-example" not in nxt.stdout                    # nor the placeholder
    # ...the new flow, on the same repository, makes the same issue pickable
    new = _run(w, [FLOW, w["repo"], "--mode", "github", "--ledger", "no", "--board", "no",
                   "--no-verify"])
    assert new.returncode == 0, new.stdout + new.stderr
    assert _run(w, [LOOP, "next", ".sdlc"]).stdout.strip() == "1"


# ---------------------------------------------------------------- board consent


def _load_flow():
    spec = importlib.util.spec_from_file_location("init_flow_under_test", FLOW)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_board_is_created_only_on_an_explicit_yes(tmp_path):
    flow = _load_flow()
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir()
    (sdlc / "config.json").write_text(json.dumps(
        {"discovery": {"source": "github", "github": {"repo": REPO}}}))
    calls = []

    def pins(argv):                                      # board_setup.py pins what it created
        calls.append(argv)
        cfg = json.loads((sdlc / "config.json").read_text())
        cfg["discovery"]["github"]["project"] = {"number": 3, "owner": "acme"}
        (sdlc / "config.json").write_text(json.dumps(cfg))
        return 0, "[ok] board created\n"
    flow.BOARD_RUNNER = pins
    lines, ok = flow.board_step(str(tmp_path), str(sdlc), answer=None, repo=REPO)
    assert calls == [] and ok and any("OFFER" in l for l in lines)
    lines, ok = flow.board_step(str(tmp_path), str(sdlc), answer="no", repo=REPO)
    assert calls == [] and ok
    lines, ok = flow.board_step(str(tmp_path), str(sdlc), answer="yes", repo=REPO)
    assert ok and len(calls) == 1 and calls[0][-1] == "--yes" and "create" in calls[0]
    assert json.loads((sdlc / "config.json").read_text())["discovery"]["github"]["project"]["enabled"]
    # a REMEMBERED yes runs nothing
    lines, ok = flow.board_step(str(tmp_path), str(sdlc), answer="yes", repo=REPO, how="kept")
    assert ok and len(calls) == 1
    flow.BOARD_RUNNER = lambda argv: (1, "[FAIL] project scope missing\n")
    lines, ok = flow.board_step(str(tmp_path), str(sdlc), answer="yes", repo=REPO)
    assert not ok and any("FAIL" in l for l in lines)


def test_board_yes_points_at_the_canonical_layout_and_runs_only_create(tmp_path):
    """#234: after `--board yes` the flow PRINTS the fields/views/verify gestures and the template
    alternative; the one board call it makes is still `create`."""
    flow = _load_flow()
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir()
    (sdlc / "config.json").write_text(json.dumps(
        {"discovery": {"source": "github", "github": {"repo": REPO}}}))
    calls = []

    def pins(argv):
        calls.append(argv)
        cfg = json.loads((sdlc / "config.json").read_text())
        cfg["discovery"]["github"]["project"] = {"number": 3, "owner": "acme"}
        (sdlc / "config.json").write_text(json.dumps(cfg))
        return 0, "[ok] board created\n"
    flow.BOARD_RUNNER = pins
    lines, ok = flow.board_step(str(tmp_path), str(sdlc), answer=None, repo=REPO)
    assert calls == [] and any("--template" in l and "docs/board.md" in l for l in lines), lines
    lines, ok = flow.board_step(str(tmp_path), str(sdlc), answer="yes", repo=REPO)
    text = "\n".join(lines)
    assert ok and len(calls) == 1 and "create" in calls[0]
    for verb in ("fields", "views", "verify"):
        assert "board_layout.py" in text and (" %s " % verb) in text, verb
    printed = [l for l in lines if "board_layout.py" in l]
    assert len(printed) == 3 and not any(l.rstrip().endswith("--yes") for l in printed)  # dry runs


def test_yes_never_answers_board_verify_or_work():
    flow = _load_flow()
    res = flow.resolve_answers({"yes": True}, answered={}, scaffolded={}, current={},
                               detected_mode="github")
    assert res["mode"] == ("github", "default") and res["ledger"] == ("no", "default")
    assert res["board"] == (None, "open") and "verify" not in res
    assert res["work"][1] == "open"


def test_yes_and_memory_never_override_config_in_process():
    """The resolution table itself: config.json (not init.json, not --yes) decides a set key."""
    flow = _load_flow()
    cur = {"mode": "local-goals", "work": "off", "ledger": "yes"}
    res = flow.resolve_answers({"yes": True}, answered={"mode": "github", "work": "on", "ledger": "no"},
                               scaffolded={}, current=cur, detected_mode="github")
    assert res["mode"] == ("local-goals", "kept") and res["work"] == ("off", "kept")
    assert res["ledger"] == ("yes", "kept")
    # a template value this flow scaffolded is still an OPEN question: --yes may answer it
    res = flow.resolve_answers({"yes": True}, answered={}, scaffolded={"mode": "local-goals"},
                               current={"mode": "local-goals"}, detected_mode="github")
    assert res["mode"] == ("github", "default")
    # a flag always wins
    res = flow.resolve_answers({"mode": "github"}, answered={}, scaffolded={}, current=cur,
                               detected_mode="local-goals")
    assert res["mode"] == ("github", "flag")
    # remembered values outside the closed sets are dropped, and a remembered repo is never read
    sdlc_memory = {"mode": "bogus", "board": "yes", "repo": "old/gone"}
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        (pathlib.Path(d) / "state").mkdir()
        (pathlib.Path(d) / "state" / "init.json").write_text(json.dumps(sdlc_memory))
        assert flow.load_memory(d) == ({"board": "yes"}, {})


# ---------------------------------------------------------------- setup.py configure


def test_configure_refuses_github_mode_with_no_repo(tmp_path):
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir()
    (sdlc / "config.json").write_text("{}")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    p = subprocess.run([sys.executable, str(SETUP), "configure", str(sdlc)],
                       capture_output=True, text=True)
    assert p.returncode == 2, p.stdout + p.stderr
    assert json.loads((sdlc / "config.json").read_text()) == {}


def test_configure_without_an_sdlc_dir_refuses_instead_of_crashing(tmp_path):
    p = subprocess.run([sys.executable, str(SETUP), "configure", str(tmp_path / ".sdlc"),
                        "--repo", REPO], capture_output=True, text=True)
    assert p.returncode == 2, p.stdout + p.stderr
    assert "Traceback" not in p.stderr and "/sigma-init" in p.stderr
    assert not (tmp_path / ".sdlc").exists()


def test_setup_init_is_an_alias_of_the_flow():
    p = subprocess.run([sys.executable, str(SETUP), "init", "--help"], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert "init_flow.py" in p.stdout


# ------------------------------------------------- #614: adoption never disables git hooks

#: The value earlier Sigma releases wrote (#218). Assembled: the public-surface guard rejects the
#: raw directory name.
_STALE_HOOKS = "." + "git" + "hooks"

#: The documented gesture (skills/sigma-init/SKILL.md flag table): the AC-5 control runs exactly it.
_DOCUMENTED = [FLOW, ".", "--mode", "local-goals", "--yes", "--no-verify"]


def _local_hooks_path(w):
    got = subprocess.run(["git", "-C", str(w["repo"]), "config", "--local", "--get",
                          "core.hooksPath"], capture_output=True, text=True, env=w["env"])
    return got.stdout.strip() if got.returncode == 0 else None


def _failing_hook(hooks_dir):
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "pre-commit"
    hook.write_text("#!/bin/sh\necho HOOK-RAN-614\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)


def _try_commit(w, name):
    (w["repo"] / name).write_text(name + "\n")
    subprocess.run(["git", "-C", str(w["repo"]), "add", name], check=True, env=w["env"],
                   capture_output=True)
    return subprocess.run(["git", "-C", str(w["repo"]), "commit", "-m", name],
                          capture_output=True, text=True, env=w["env"])


def _init_keeps_a_failing_pre_commit_hook_running(tmp_path, gesture):
    w = _world(tmp_path, origin=None)
    _failing_hook(w["repo"] / ".git" / "hooks")
    before = _try_commit(w, "before.txt")
    assert before.returncode != 0 and "HOOK-RAN-614" in before.stdout + before.stderr  # control
    argv = _DOCUMENTED if gesture == "documented" else [SETUP, "init", *_DOCUMENTED[1:]]
    p = _run(w, argv)
    assert (w["sdlc"] / "config.json").is_file(), p.stdout + p.stderr
    after = _try_commit(w, "after.txt")
    assert after.returncode != 0 and "HOOK-RAN-614" in after.stdout + after.stderr, after.stdout
    assert _local_hooks_path(w) is None


@posix_only
def test_documented_init_keeps_a_failing_pre_commit_hook_running(tmp_path):
    """#614 AC-1/AC-5: a failing pre-commit hook blocks a commit before adoption AND after it.
    Before #614 init wrote a hooksPath naming a directory nothing creates, so git ran no hooks and
    the second commit went through. Runs the documented gesture verbatim; `--yes --no-verify`
    without a remote ends in a preflight FAIL (rc 1), which must not matter -- a failed adoption
    wrote the key too. Not parametrized: the test-first gate credits a red only to an
    unambiguous node."""
    _init_keeps_a_failing_pre_commit_hook_running(tmp_path, "documented")


@posix_only
def test_setup_alias_keeps_a_failing_pre_commit_hook_running(tmp_path):
    """#614 AC-1 through /sigma-setup, an alias of the same flow."""
    _init_keeps_a_failing_pre_commit_hook_running(tmp_path, "setup-alias")


@posix_only
def test_documented_init_keeps_a_global_hook_path_in_force(tmp_path):
    """#614 (plan-review R7): an org-wide `core.hooksPath` in the GLOBAL config (a secret scanner)
    is not shadowed by adoption."""
    w = _world(tmp_path, origin=None)
    org = tmp_path / "orghooks"
    _failing_hook(org)
    gconf = tmp_path / "global.gitconfig"
    gconf.write_text("[core]\n\thooksPath = %s\n" % org, encoding="utf-8")
    w["env"]["GIT_CONFIG_GLOBAL"] = str(gconf)
    assert _try_commit(w, "before.txt").returncode != 0                              # control
    _run(w, _DOCUMENTED)
    after = _try_commit(w, "after.txt")
    assert after.returncode != 0 and "HOOK-RAN-614" in after.stdout + after.stderr, after.stdout


def _init_adopts_and_keeps(tmp_path, value, make_dir):
    w = _world(tmp_path, origin=None)
    if make_dir:
        (w["repo"] / value).mkdir(parents=True)
    _git(w["repo"], "config", "--local", "core.hooksPath", value, env=w["env"])
    p = _run(w, [FLOW, ".", "--mode", "local-goals", "--local-only", "--no-verify"])
    assert p.returncode == 0, p.stdout + p.stderr
    assert (w["sdlc"] / "config.json").is_file()
    assert _local_hooks_path(w) == value
    assert "core.hooksPath" not in p.stdout + p.stderr


@posix_only
def test_init_adopts_and_keeps_an_existing_hook_path(tmp_path):
    """#614 AC-2: a repository that already sets its own hooksPath (husky's) adopts normally and
    keeps it. Not parametrized: the test-first gate credits a red only to an unambiguous node."""
    _init_adopts_and_keeps(tmp_path, ".husky/_", True)


@posix_only
def test_init_adopts_and_keeps_a_hook_path_whose_directory_is_missing(tmp_path):
    """#614 AC-2: any value that is not the one Sigma wrote is the user's, even with its directory
    missing -- never unset."""
    _init_adopts_and_keeps(tmp_path, "missing-hooks", False)


@posix_only
def test_init_unsets_the_stale_key_and_says_so_once(tmp_path):
    """#614 AC-4: re-running /sigma-init in a repository an earlier release adopted removes the
    stale key (its directory missing) and says so in exactly one line; the hook runs again."""
    w = _world(tmp_path, origin=None)
    _failing_hook(w["repo"] / ".git" / "hooks")
    _git(w["repo"], "config", "--local", "core.hooksPath", _STALE_HOOKS, env=w["env"])
    assert _try_commit(w, "before.txt").returncode == 0      # control: the stale key disables it
    p = _run(w, _DOCUMENTED)
    assert _local_hooks_path(w) is None, p.stdout + p.stderr
    said = [l for l in p.stdout.splitlines() if "core.hooksPath" in l]
    assert len(said) == 1, p.stdout
    assert _try_commit(w, "after.txt").returncode != 0


@posix_only
def test_init_keeps_the_same_value_when_its_directory_exists(tmp_path):
    """The same spelling with the directory present is the adopter's own hook directory: kept,
    and nothing said about it."""
    w = _world(tmp_path, origin=None)
    (w["repo"] / _STALE_HOOKS).mkdir()
    _git(w["repo"], "config", "--local", "core.hooksPath", _STALE_HOOKS, env=w["env"])
    p = _run(w, _DOCUMENTED)
    assert _local_hooks_path(w) == _STALE_HOOKS
    assert "core.hooksPath" not in p.stdout + p.stderr


# ---------------------------------------------------------------- doctor points at init


def test_doctor_work_row_points_at_init():
    text = (ROOT / "skills" / "sigma-doctor" / "scripts" / "doctor.py").read_text(encoding="utf-8")
    assert "(or run /sigma-setup)" not in text
    assert '"work": {"enabled": true}  (or run /sigma-init)' in text


def test_the_pre_236_github_flag_means_github_mode():
    """`/sigma-init --github` (README, muscle memory) now SAYS github mode too -- the old scaffolder
    flag created labels but left the backlog local, which is the bug this goal closes."""
    flow = _load_flow()
    opts, target, err = flow.parse(["--github"])
    assert err is None and opts["mode"] == "github" and opts["github-templates"] is True
    assert flow.parse(["--github", "--mode", "local-goals"])[2]          # a contradiction is refused


# ---------------------------------------------------------------- review of PR #286: config.json is
# the single source of truth. `.sdlc/state/init.json` records only that a question was ANSWERED (so it
# is not asked again); it never re-applies a value over config.json, and `--yes` answers only what
# config.json and the flags leave open. Each test below is a port of a reviewer repro.


def _write_cfg(w, cfg):
    (w["sdlc"] / "config.json").write_text(json.dumps(cfg), encoding="utf-8")


def _proj(cfg):
    return ((cfg.get("discovery") or {}).get("github") or {}).get("project") or {}


@posix_only
def test_rerun_never_reverts_what_the_user_changed_in_config_since_answering(tmp_path):
    """rv286/stale.py: answered once, then changed through the documented gestures; the flow's own
    `Next:` re-run (`--demo`) must keep every one of those changes -- and say it kept them."""
    w = _world(tmp_path)
    first = _run(w, [FLOW, ".", "--mode", "github", "--work", "on", "--ledger", "yes", "--board", "no",
                     "--no-verify"])
    assert first.returncode == 0, first.stdout + first.stderr
    assert _run(w, [PREFLIGHT, "local-only", ".sdlc"]).returncode == 0
    cfg = _cfg(w)
    cfg["ledger"]["enabled"] = False
    cfg["discovery"]["source"] = "local-goals"
    cfg["discovery"]["github"]["project"]["enabled"] = True
    _write_cfg(w, cfg)
    before = _cfg(w)
    w["log"].write_text("")
    again = _run(w, [FLOW, ".", "--demo"])
    assert again.returncode == 0, again.stdout + again.stderr
    after = _cfg(w)
    assert after["work"] == before["work"] and after["work"]["enabled"] is False
    assert after["ledger"]["enabled"] is False                    # no watcher from a stale "yes"
    assert after["discovery"]["source"] == "local-goals"
    assert _proj(after)["enabled"] is True
    assert "kept" in again.stdout and "config.json wins" in again.stdout, again.stdout
    assert not any(c[:2] == ["label", "create"] for c in _calls(w))


@posix_only
def test_a_remembered_board_yes_never_creates_a_board_without_the_flag(tmp_path):
    w = _world(tmp_path)
    assert _run(w, [FLOW, ".", "--mode", "github", "--ledger", "no", "--board", "no",
                    "--no-verify"]).returncode == 0
    # a remembered "yes" (the pre-review flat format) -- and project.enabled still off in config
    (w["sdlc"] / "state" / "init.json").write_text(json.dumps(
        {"mode": "github", "board": "yes", "ledger": "no", "repo": REPO}))
    w["log"].write_text("")
    p = _run(w, [FLOW, "."])
    assert p.returncode == 0, p.stdout + p.stderr
    assert _proj(_cfg(w))["enabled"] is False
    assert not _proj(_cfg(w)).get("number")
    assert not any("projectsV2" in " ".join(c) or c[:2] == ["api", "user"] for c in _calls(w)), _calls(w)
    assert w["unhandled"].read_text() == ""


@posix_only
def test_a_remembered_repo_never_beats_config_or_origin(tmp_path):
    w = _world(tmp_path)
    assert _run(w, [FLOW, ".", "--mode", "github", "--ledger", "no", "--board", "no",
                    "--no-verify"]).returncode == 0
    (w["sdlc"] / "state" / "init.json").write_text(json.dumps(
        {"mode": "github", "board": "no", "ledger": "no", "repo": "old/gone"}))
    assert _run(w, [FLOW, "."]).returncode == 0
    assert _cfg(w)["discovery"]["github"]["repo"] == REPO          # config.json kept
    # no repo in config at all: the CURRENT origin wins over the remembered one
    cfg = _cfg(w)
    del cfg["discovery"]["github"]["repo"]
    cfg["discovery"]["source"] = "local-goals"
    _write_cfg(w, cfg)
    p = _run(w, [FLOW, ".", "--mode", "github"])
    assert p.returncode == 0, p.stdout + p.stderr
    assert _cfg(w)["discovery"]["github"]["repo"] == REPO
    assert w["unhandled"].read_text() == ""


@posix_only
def test_yes_on_a_repo_adopted_before_the_flow_changes_nothing(tmp_path):
    """rv286/yes_existing.py: adopted with the old scaffolder + `setup.py configure`, local-goals
    chosen by hand. `--yes` must not switch the source, write labels, or touch the ledger/board."""
    w = _world(tmp_path)
    assert _run(w, [SDLC_INIT, "."]).returncode == 0
    assert _run(w, [SETUP, "configure", ".sdlc", "--source", "local-goals"]).returncode == 0
    before = _cfg(w)
    w["log"].write_text("")
    p = _run(w, [FLOW, ".", "--yes"])
    assert p.returncode == 0, p.stdout + p.stderr
    after = _cfg(w)
    assert after["discovery"]["source"] == "local-goals"
    assert after["ledger"] == before["ledger"] and after["work"] == before["work"]
    assert _proj(after) == _proj(before)
    assert not any(c[:2] == ["label", "create"] for c in _calls(w))
    assert "kept" in p.stdout, p.stdout


@posix_only
@pytest.mark.parametrize("key", ["mode", "work", "ledger", "board"])
def test_yes_keeps_each_existing_setting(tmp_path, key):
    """One key at a time, each set to the value `--yes` (or the flow's own default) would NOT pick."""
    w = _world(tmp_path)
    assert _run(w, [SDLC_INIT, "."]).returncode == 0
    cfg = _cfg(w)
    gh = cfg["discovery"].setdefault("github", {})
    gh["repo"] = REPO
    if key == "mode":
        cfg["discovery"]["source"] = "local-goals"              # --yes would detect github
    else:
        cfg["discovery"]["source"] = "github"
    if key == "work":
        cfg["work"]["enabled"] = False
    if key == "ledger":
        cfg["ledger"] = {"enabled": True}                        # --yes would say no
    if key == "board":
        gh.setdefault("project", {})["enabled"] = True           # the flow would switch it off
        gh["project"]["number"] = None
    _write_cfg(w, cfg)
    before = _cfg(w)
    p = _run(w, [FLOW, ".", "--yes", "--no-verify"])
    assert p.returncode in (0, 1), p.stdout + p.stderr
    after = _cfg(w)
    assert after["discovery"]["source"] == before["discovery"]["source"]
    assert after["work"]["enabled"] == before["work"]["enabled"]
    was = (before.get("ledger") or {}).get("enabled")
    if was is not None:                                          # null is unset: --yes may answer it
        assert (after.get("ledger") or {}).get("enabled") == was
    assert _proj(after).get("enabled") == _proj(before).get("enabled")
    assert "kept" in p.stdout, p.stdout


@posix_only
def test_an_explicit_flag_still_changes_an_existing_setting(tmp_path):
    """The other half of "config wins": a flag on THIS run is the one way to change it."""
    w = _world(tmp_path)
    assert _run(w, [FLOW, ".", "--mode", "github", "--ledger", "no", "--board", "no",
                    "--no-verify"]).returncode == 0
    p = _run(w, [FLOW, ".", "--mode", "local-goals", "--ledger", "yes", "--local-only"])
    assert p.returncode == 0, p.stdout + p.stderr
    cfg = _cfg(w)
    assert cfg["discovery"]["source"] == "local-goals"
    assert cfg["ledger"]["enabled"] is True and cfg["work"]["enabled"] is False


@posix_only
def test_an_existing_assignee_is_never_clobbered(tmp_path):
    w = _world(tmp_path)
    assert _run(w, [SDLC_INIT, "."]).returncode == 0
    cfg = _cfg(w)
    cfg["discovery"].setdefault("github", {})["assignee"] = "alice"
    _write_cfg(w, cfg)
    p = _run(w, [FLOW, ".", "--mode", "github", "--ledger", "no", "--board", "no", "--no-verify"])
    assert p.returncode == 0, p.stdout + p.stderr
    assert _cfg(w)["discovery"]["github"]["assignee"] == "alice"


# ---------------------------------------------------------------- board: yes turns a pinned board on


def _main_inproc(w, argv, monkeypatch, board_runner):
    """init_flow.main in-process (so the board runner seam applies), against the same fake world."""
    import contextlib
    import io
    flow = _load_flow()
    flow.BOARD_RUNNER = board_runner
    fake_gh = str(w["repo"].parent / "bin" / "gh")
    real = flow._pf.real_runner
    # conftest's live-gh guard trips on an argv[0] named `gh`: run the SAME fake via the interpreter
    flow._si.PREFLIGHT_RUNNER = lambda argv, cwd=None, timeout=None: real(
        [sys.executable, fake_gh, *argv[1:]] if argv and argv[0] == "gh" else argv, cwd, timeout)
    for k, v in w["env"].items():
        monkeypatch.setenv(k, v)
    monkeypatch.chdir(w["repo"])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = flow.main(["init_flow.py", *[str(a) for a in argv]])
    return rc, buf.getvalue()


def _pinning_runner(w, calls, number=7):
    def run(argv):
        calls.append(argv)
        cfg = _cfg(w)
        cfg["discovery"]["github"]["project"].update(number=number, owner="acme")
        _write_cfg(w, cfg)
        # board_setup.py's real success tail when enabled is off (its `create`): the note, then done
        return 0, ("  note: discovery.github.project.enabled is not true, so the loop will not "
                   "mirror onto this board until it is.\nboard_setup: done -- board #%d is pinned\n"
                   % number)
    return run


@posix_only
def test_board_yes_turns_on_a_board_pinned_after_declining(tmp_path, monkeypatch):
    """rv286/board.py: declined, then pinned by hand (`board_setup.py create` pins number/owner only);
    `--board yes` must verify it (board_setup, which refuses an unreachable pinned board) and turn
    mirroring ON."""
    w = _world(tmp_path)
    p = _run(w, [SETUP, "init", ".", "--mode", "github", "--board", "no", "--ledger", "no", "--no-verify"])
    assert p.returncode == 0, p.stdout + p.stderr
    cfg = _cfg(w)
    cfg["discovery"]["github"]["project"].update(number=7, owner="acme")
    _write_cfg(w, cfg)
    calls = []
    rc, out = _main_inproc(w, [".", "--board", "yes"], monkeypatch, _pinning_runner(w, calls))
    assert rc == 0, out
    assert len(calls) == 1 and "create" in calls[0] and calls[0][-1] == "--yes"
    proj = _proj(_cfg(w))
    assert proj["number"] == 7 and proj["enabled"] is True
    assert not str(proj.get("_enabled_why") or "").startswith("off")
    # board_setup's "enabled is not true" note is false by the next line: this flow does not show it
    assert "is not true" not in out and "mirroring on" in out, out
    # an unreachable pinned board (board_setup refuses) leaves mirroring exactly as it was
    cfg = _cfg(w)
    cfg["discovery"]["github"]["project"]["enabled"] = False
    _write_cfg(w, cfg)
    rc, out = _main_inproc(w, [".", "--board", "yes"], monkeypatch,
                           lambda argv: (2, "board_setup: REFUSED -- the pinned board #7 is not one of acme's\n"))
    assert rc == 1 and "Resume:" in out, out
    assert _proj(_cfg(w))["enabled"] is False


@posix_only
def test_a_failed_board_yes_never_turns_project_enabled_on(tmp_path, monkeypatch):
    """Nothing turns mirroring on before board_setup succeeds, or after it fails -- including a
    partial failure that already pinned the board it created (INCOMPLETE, exit 1). (There is no
    restore: board_setup.py never writes `enabled`, so a failure has nothing to undo.)"""
    w = _world(tmp_path)
    assert _run(w, [FLOW, ".", "--mode", "github", "--ledger", "no", "--no-verify"]).returncode == 0
    assert _proj(_cfg(w))["enabled"] is False                      # the offer is open
    seen = []

    def failing(argv):
        seen.append(_proj(_cfg(w)).get("enabled"))                 # state board_setup would see
        return 2, "board_setup: REFUSED -- the gh token cannot create a board: project scope\n"
    rc, out = _main_inproc(w, [".", "--board", "yes"], monkeypatch, failing)
    assert rc == 1, out
    proj = _proj(_cfg(w))
    assert proj["enabled"] is False and not proj.get("number")
    assert seen == [False]                                         # never True with no number
    calls = []
    pin_then_fail = _pinning_runner(w, calls)

    def incomplete(argv):
        pin_then_fail(argv)                                        # pinned #7, then a step failed
        seen.append(_proj(_cfg(w)).get("enabled"))
        return 1, "board_setup: INCOMPLETE -- see [FAIL] above.\n"
    rc, out = _main_inproc(w, [".", "--board", "yes"], monkeypatch, incomplete)
    assert rc == 1 and "project.enabled left as it was (False)" in out, out
    proj = _proj(_cfg(w))
    assert proj["number"] == 7 and proj["enabled"] is False
    assert seen == [False, False]


def test_the_suppressed_board_note_is_board_setups_own_text():
    """The flow drops board_setup's "enabled is not true" note by its text: pin that text to the
    source, so a reworded note cannot slip back into the flow's success output unnoticed."""
    src = (SCRIPTS / "board_setup.py").read_text(encoding="utf-8")
    assert _load_flow().BOARD_NOT_ENABLED_NOTE in src


# ---------------------------------------------------------------- flags + the resume line


def test_repo_flag_must_be_owner_slash_name():
    flow = _load_flow()
    for bad in ("acme", "acme/app/extra", "https://github.com/acme/app", "acme/", "/app", "a b/c",
                "acme/..", "-x/app", "acme/app.git", "acme/app.GIT"):
        assert flow.parse(["--repo", bad])[2], bad
    assert flow.parse(["--repo", "acme-co/app.name_2"])[2] is None


def test_resume_keeps_the_verify_command_file_absolute(tmp_path, monkeypatch):
    flow = _load_flow()
    monkeypatch.chdir(tmp_path)
    line = flow.resume_command(".", [".", "--mode", "local-goals", "--verify-command-file", "cmd.txt"])
    assert str(tmp_path.resolve() / "cmd.txt") in line or str(tmp_path / "cmd.txt") in line, line
    assert " --mode local-goals " in line + " "


def test_resume_is_not_printed_on_windows_when_a_value_would_break_the_quoting(tmp_path, monkeypatch):
    flow = _load_flow()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(flow, "_windows", lambda: True)
    assert flow.resume_command(".", [".", "--verify-command-file", 'a"b.txt']) is None
    ok = flow.resume_command(".", [".", "--mode", "local-goals"])
    assert ok is not None and '"' in ok


@posix_only
def test_the_demo_hint_does_not_promise_a_board_that_is_off(tmp_path):
    w = _world(tmp_path)
    p = _run(w, [FLOW, ".", "--mode", "github", "--ledger", "no", "--board", "no", "--no-verify", "--demo"])
    assert p.returncode == 0, p.stdout + p.stderr
    assert "creates the board" not in p.stdout, p.stdout


@posix_only
def test_an_interrupted_scaffold_leaves_sigma_s_owner_marker(tmp_path, monkeypatch):
    """The owner marker is written BEFORE the scaffold, so the session wizard can tell an interrupted
    `/sigma-init` (Sigma's `.sdlc/`, no config.json) from another tool's bare `.sdlc/`."""
    w = _world(tmp_path, origin=None)
    flow = _load_flow()

    def boom(target):
        (pathlib.Path(target) / ".sdlc").mkdir(exist_ok=True)
        raise flow._si.RuntimeIgnoreWriteFailed("disk full")
    monkeypatch.setattr(flow._si, "scaffold", boom)
    for k, v in w["env"].items():
        monkeypatch.setenv(k, v)
    monkeypatch.chdir(w["repo"])
    assert flow.main(["init_flow.py", ".", "--mode", "local-goals"]) == 1
    owner = json.loads((w["sdlc"] / "state" / "owner.json").read_text())
    assert owner["plugin"] == "sigma"
    assert not (w["sdlc"] / "config.json").exists()


@posix_only
def test_the_next_line_demo_rerun_keeps_answers_and_queues_the_demo(tmp_path):
    """rv286/e2e_local.py: bare run -> answered -> `loop.py next` finds nothing -> the `Next:` line's
    own `--demo` re-run keeps every answer (and asks nothing) -> the loop picks the demo."""
    w = _world(tmp_path, origin=None)
    bare = _run(w, [FLOW, "."])
    assert bare.returncode == 0 and "[ask] mode" in bare.stdout, bare.stdout + bare.stderr
    ans = _run(w, [FLOW, ".", "--mode", "local-goals", "--local-only", "--no-verify"])
    assert ans.returncode == 0 and "--demo" in ans.stdout, ans.stdout + ans.stderr
    assert _run(w, [LOOP, "next", ".sdlc"]).stdout.strip().startswith("DONE")
    before = _cfg(w)
    demo = _run(w, [FLOW, ".", "--demo"])
    assert demo.returncode == 0 and "[ask]" not in demo.stdout, demo.stdout + demo.stderr
    assert _cfg(w)["discovery"] == before["discovery"] and _cfg(w)["work"] == before["work"]
    assert _run(w, [LOOP, "next", ".sdlc"]).stdout.strip().endswith("0000-demo.md")
    memory = json.loads((w["sdlc"] / "state" / "init.json").read_text())
    assert memory["answered"] == {"mode": "local-goals", "work": "off"}


# ---------------------------------------------------------------- review block #2: a scaffolded
# template value is OPEN only while config.json is exactly what the flow last wrote


def _fresh_scaffold(tmp_path):
    """A bare `/sigma-init` on a GitHub-origin repo: config.json scaffolded, mode/ledger still open."""
    w = _world(tmp_path)
    p = _run(w, [FLOW, "."])
    assert p.returncode == 0 and "[ask] mode" in p.stdout, p.stdout + p.stderr
    assert _cfg(w)["discovery"]["source"] == "local-goals"      # the template's value
    w["log"].write_text("")
    return w


def _assert_yes_changed_nothing(w, before, p):
    assert p.returncode == 0, p.stdout + p.stderr
    after = _cfg(w)
    assert after["discovery"]["source"] == before["discovery"]["source"] == "local-goals", p.stdout
    assert after["work"] == before["work"]
    was = before["ledger"].get("enabled")
    # a null ledger is "not yet decided" (the template says so), not a value: --yes may answer it
    # off -- never on. Any value config.json carries is kept.
    assert after["ledger"]["enabled"] == (False if was is None else was)
    assert not any(c[:2] == ["label", "create"] for c in _calls(w)), _calls(w)


@posix_only
def test_yes_after_setup_configure_local_goals_keeps_it(tmp_path):
    """Review block #2 (atkC2.py): `setup.py configure --source local-goals` leaves the template's
    own value, and `--yes` then switched the source to github and created 14 labels."""
    w = _fresh_scaffold(tmp_path)
    assert _run(w, [SETUP, "configure", ".sdlc", "--source", "local-goals"]).returncode == 0
    before = _cfg(w)
    _assert_yes_changed_nothing(w, before, _run(w, [FLOW, ".", "--yes", "--no-verify"]))


@posix_only
def test_yes_after_a_hand_edit_to_the_template_value_keeps_it(tmp_path):
    """Review block #2 (atk286.py case C): the user re-writes source local-goals by hand."""
    w = _fresh_scaffold(tmp_path)
    cfg = _cfg(w)
    cfg["discovery"]["source"] = "local-goals"
    (w["sdlc"] / "config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    before = _cfg(w)
    _assert_yes_changed_nothing(w, before, _run(w, [FLOW, ".", "--yes", "--no-verify"]))


@posix_only
def test_yes_after_a_byte_identical_rewrite_keeps_it(tmp_path):
    """A write that leaves the very same bytes (a revert, or a tool re-saving the file) is still a
    write: the fingerprint carries the mtime too, so this is explicit, not re-opened."""
    w = _fresh_scaffold(tmp_path)
    path = w["sdlc"] / "config.json"
    original = path.read_bytes()
    path.write_text("{}", encoding="utf-8")
    path.write_bytes(original)
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))   # coarse-clock safe
    assert path.read_bytes() == original
    before = _cfg(w)
    _assert_yes_changed_nothing(w, before, _run(w, [FLOW, ".", "--yes", "--no-verify"]))


@posix_only
def test_yes_with_a_corrupt_init_json_keeps_everything(tmp_path):
    w = _fresh_scaffold(tmp_path)
    (w["sdlc"] / "state" / "init.json").write_text("{not json", encoding="utf-8")
    before = _cfg(w)
    _assert_yes_changed_nothing(w, before, _run(w, [FLOW, ".", "--yes", "--no-verify"]))


@posix_only
def test_yes_on_an_untouched_scaffold_still_fills_the_open_questions(tmp_path):
    """The control for the four above: nothing touched config.json since the flow wrote it, so the
    template values are still open and `--yes` takes the detected mode (github) and ledger no."""
    w = _fresh_scaffold(tmp_path)
    p = _run(w, [FLOW, ".", "--yes", "--no-verify"])
    assert p.returncode == 0, p.stdout + p.stderr
    cfg = _cfg(w)
    assert cfg["discovery"]["source"] == "github" and cfg["ledger"]["enabled"] is False
    assert sum(1 for c in _calls(w) if c[:2] == ["label", "create"]) == len(LABELS)


def test_scaffolded_values_are_open_only_while_config_matches_the_recorded_fingerprint(tmp_path):
    """The seam itself, in-process (runs on every OS): `load_memory` returns the scaffolded
    template values only when config.json still fingerprints to what init.json recorded."""
    flow = _load_flow()
    sdlc = tmp_path
    (sdlc / "state").mkdir()
    cfg = sdlc / "config.json"
    cfg.write_text(json.dumps({"discovery": {"source": "local-goals"}}), encoding="utf-8")
    memory = {"answered": {}, "scaffolded": {"mode": "local-goals"},
              "config": flow.config_fingerprint(cfg)}
    (sdlc / "state" / "init.json").write_text(json.dumps(memory), encoding="utf-8")
    assert flow.load_memory(str(sdlc)) == ({}, {"mode": "local-goals"})
    st = cfg.stat()
    os.utime(cfg, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))       # touched, same bytes
    assert flow.load_memory(str(sdlc)) == ({}, {})
    memory["config"] = flow.config_fingerprint(cfg)
    (sdlc / "state" / "init.json").write_text(json.dumps(memory), encoding="utf-8")
    assert flow.load_memory(str(sdlc)) == ({}, {"mode": "local-goals"})
    del memory["config"]                                                     # no fingerprint
    (sdlc / "state" / "init.json").write_text(json.dumps(memory), encoding="utf-8")
    assert flow.load_memory(str(sdlc)) == ({}, {})
    memory["config"] = flow.config_fingerprint(cfg)
    (sdlc / "state" / "init.json").write_text(json.dumps(memory), encoding="utf-8")
    cfg.unlink()                                                             # no config.json
    assert flow.load_memory(str(sdlc)) == ({}, {})


# ---------------------------------------------------------------- #615: a committed verify command, no local trust
# `sigma.allowRepositoryShellCommands` is git-LOCAL (#422): not committed, not cloned. A repo whose committed
# config already carries `verify.command` (every migrated repo, every teammate's clone) used to get `[ok] verify`
# from init and a refusal at `loop.py verify`. Init must say so and print the one gesture instead.

_TRUST_KEY = "sigma.allowRepositoryShellCommands"


def _committed_verify_clone(tmp_path, command):
    """The teammate scenario, end to end: one checkout commits `.sdlc/config.json` with a verify command and
    enforce on; a second checkout (a fresh clone, so no git-local config) is the teammate's."""
    w = _world(tmp_path, origin=None)
    assert _run(w, [FLOW, w["repo"], "--mode", "local-goals", "--local-only", "--no-verify"]).returncode == 0
    cfg = _cfg(w)
    cfg["verify"] = {"command": command, "enforce": True}
    _write_cfg(w, cfg)
    _git(w["repo"], "add", "-f", ".sdlc/config.json", env=w["env"])
    _git(w["repo"], "commit", "-q", "-m", "commit the verify command", env=w["env"])
    clone = tmp_path / "teammate"
    _git(tmp_path, "clone", "-q", str(w["repo"]), str(clone), env=w["env"])
    t = dict(w, repo=clone, sdlc=clone / ".sdlc")
    (t["sdlc"] / "goals").mkdir(exist_ok=True)
    (t["sdlc"] / "goals" / "0001-example.md").write_text("---\nstatus: pending\n---\nexample\n")
    return t


def _rerun_init(t):
    return _run(t, [FLOW, t["repo"], "--mode", "local-goals", "--local-only"])


def _verify_section(out):
    return out.split("sigma-init: 3/5 verify")[1].split("sigma-init: 4/5")[0]


@posix_only
def test_teammate_clone_with_a_committed_verify_command_is_told_to_trust_it(tmp_path):
    """AC-1 / AC-4: the clone has the committed command and no trust. Init must NOT print an ok verify line,
    must print the exact gesture, and running THAT gesture must make `loop.py verify` pass."""
    cmd = "%s -c 'print(1)'" % sys.executable
    t = _committed_verify_clone(tmp_path, cmd)
    refused = _run(t, [LOOP, "verify", ".sdlc", "0001-example"])
    assert refused.returncode == 2 and "requires explicit operator trust" in refused.stdout + refused.stderr
    p = _rerun_init(t)
    assert p.returncode == 0, p.stdout + p.stderr
    section = _verify_section(p.stdout)
    assert "[ok] verify" not in section, section
    assert "[trust] verify" in section, section
    gesture = [l.strip() for l in section.splitlines() if l.strip().startswith("git -C ")]
    assert len(gesture) == 1 and gesture[0].endswith("config --local %s true" % _TRUST_KEY), section
    assert "open:" in p.stdout and "do not re-run init" in p.stdout
    assert "[ask] verify" not in p.stdout                  # there is no flag that answers it
    # Run the gesture exactly as printed.
    subprocess.run(gesture[0], shell=True, check=True, env=t["env"], cwd=str(t["repo"]))
    ok = _run(t, [LOOP, "verify", ".sdlc", "0001-example"])
    assert ok.returncode == 0, ok.stdout + ok.stderr
    again = _verify_section(_rerun_init(t).stdout)
    assert "[ok] verify" in again and "[trust]" not in again, again


@posix_only
def test_trust_line_never_puts_the_repository_command_inside_the_gesture(tmp_path):
    """A committed command is hostile input: it must not read as part of a flag, an [ask] machine part or the
    gesture. It appears once, alone, as a JSON string."""
    hostile = "echo x -> --verify 1:abc `id` $(id) '\"; touch pwned"
    t = _committed_verify_clone(tmp_path, hostile)
    section = _verify_section(_rerun_init(t).stdout)
    lines = [l for l in section.splitlines() if "--verify 1:abc" in l]
    assert len(lines) == 1 and lines[0].strip() == json.dumps(hostile), lines
    assert not any(" -> " in l for l in section.splitlines() if "[trust]" in l or l.strip().startswith("git -C"))


@posix_only
def test_a_linked_worktree_shares_the_trust_the_gesture_grants(tmp_path):
    """Goal worktrees run `loop.py verify`; the key is read where the operator set it."""
    t = _committed_verify_clone(tmp_path, "%s -c 'print(1)'" % sys.executable)
    flow = _load_flow()
    wt = tmp_path / "goal-wt"
    _git(t["repo"], "worktree", "add", "-q", "-b", "goal", str(wt), env=t["env"])
    sdlc = str(wt / ".sdlc")
    os.makedirs(sdlc, exist_ok=True)
    (pathlib.Path(sdlc) / "config.json").write_text((t["sdlc"] / "config.json").read_text())
    saved = dict(os.environ)
    os.environ.update(t["env"])
    try:
        before = flow.verify_step(str(wt), sdlc, {})[0]
        _git(t["repo"], "config", "--local", _TRUST_KEY, "true", env=t["env"])
        after = flow.verify_step(str(wt), sdlc, {})[0]
    finally:
        os.environ.clear(); os.environ.update(saved)
    assert any("[trust] verify" in l for l in before) and not any("[ok] verify" in l for l in before)
    assert any("[ok] verify" in l for l in after) and not any("[trust]" in l for l in after)


def test_trust_line_for_a_non_git_directory_does_not_offer_a_gesture_that_cannot_work(tmp_path):
    flow = _load_flow()
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir()
    (sdlc / "config.json").write_text(json.dumps({"verify": {"command": "pytest -q", "enforce": True}}))
    lines = flow.verify_step(str(tmp_path), str(sdlc), {})[0]
    text = "\n".join(lines)
    assert "[ok] verify" not in text and "not a Git worktree" in text and "git -C" not in text, text


def test_trust_line_withholds_a_gesture_for_a_path_with_a_control_character(tmp_path):
    flow = _load_flow()
    d = tmp_path / "a\nb"
    d.mkdir()
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    sdlc = d / ".sdlc"
    sdlc.mkdir()
    (sdlc / "config.json").write_text(json.dumps({"verify": {"command": "pytest -q", "enforce": True}}))
    text = "\n".join(flow.verify_step(str(d), str(sdlc), {})[0])
    assert "[ok] verify" not in text and "git -C" not in text and "control character" in text, text

# ---------------------------------------------------------------- #625: never clobber a bad config

_BAD_CONFIGS = [("truncated", b'{"a": 1, "discovery": {"source": "gith'), ("array", b"[1, 2]"),
                ("string", b'"hello"'), ("null", b"null"), ("empty", b""),
                ("not-utf8", b'{"a": "\xff\xfe"}')]


@pytest.mark.parametrize("name,raw", _BAD_CONFIGS, ids=[n for n, _ in _BAD_CONFIGS])
def test_corrupt_config_is_refused_and_left_byte_identical(tmp_path, name, raw):
    """The documented gesture (`init_flow.py <repo> --mode local-goals`) on a present-but-bad
    config.json: exit 2, the file named with the lever, bytes identical, nothing else written."""
    w = _world(tmp_path, origin=None)
    w["sdlc"].mkdir()
    cfg = w["sdlc"] / "config.json"
    cfg.write_bytes(raw)
    # the bare gesture SKILL.md prints, and a flagged one
    for argv in ([FLOW, w["repo"]], [FLOW, w["repo"], "--mode", "local-goals", "--no-verify"]):
        r = _run(w, argv)
        assert r.returncode == 2, r.stdout + r.stderr
        assert "REFUSED" in r.stderr and "config.json" in r.stderr and "mv .sdlc/config.json .sdlc/config.json.broken-$(date +%s)" in r.stderr, r.stderr
        assert cfg.read_bytes() == raw
        assert sorted(p.name for p in w["sdlc"].iterdir()) == ["config.json"]  # no scaffold, no tmp


@pytest.mark.parametrize("name,raw", _BAD_CONFIGS, ids=[n for n, _ in _BAD_CONFIGS])
def test_apply_config_refuses_unreadable_config_directly(tmp_path, name, raw):
    flow = _load_flow()
    (tmp_path / "config.json").write_bytes(raw)
    res = {"mode": ("local-goals", "flag"), "work": (None, "open"), "ledger": (None, "open"),
           "board": (None, "open")}
    raised = None
    try:
        flow.apply_config(str(tmp_path), res, "")
    except Exception as exc:                            # asserted below, so the red is an assertion
        raised = exc
    assert type(raised).__name__ == "ConfigUnreadable", raised
    assert "config.json" in str(raised)
    assert (tmp_path / "config.json").read_bytes() == raw
    assert [p.name for p in tmp_path.iterdir()] == ["config.json"]


def test_valid_config_still_updated_and_written_atomically(tmp_path):
    flow = _load_flow()
    assert hasattr(flow, "read_config_strict")
    (tmp_path / "config.json").write_text(json.dumps({"keep": {"me": 1}, "discovery": {"source": "github"}}))
    res = {"mode": ("local-goals", "flag"), "work": (None, "open"), "ledger": (None, "open"),
           "board": (None, "open")}
    flow.apply_config(str(tmp_path), res, "")
    cfg = json.loads((tmp_path / "config.json").read_text())
    assert cfg["keep"] == {"me": 1} and cfg["discovery"]["source"] == "local-goals"
    assert [p.name for p in tmp_path.iterdir()] == ["config.json"]              # no tmp left behind
    assert flow.read_config_strict(tmp_path / "missing.json") == {}              # absent is not corrupt


def test_setup_configure_refuses_corrupt_config(tmp_path):
    w = _world(tmp_path, origin=None)
    w["sdlc"].mkdir()
    for raw in (b'{"a": 1', b"[1]"):
        (w["sdlc"] / "config.json").write_bytes(raw)
        r = _run(w, [SETUP, "configure", w["sdlc"], "--source", "local-goals"])
        assert r.returncode == 2 and "REFUSED" in r.stderr and "Traceback" not in r.stderr, r.stderr
        assert (w["sdlc"] / "config.json").read_bytes() == raw


def test_setup_write_cfg_is_atomic(tmp_path, monkeypatch):
    """The new content reaches config.json only through os.replace: a failure at the rename leaves
    the old file whole and no temp file behind (a truncate-in-place writer cannot pass this)."""
    spec = importlib.util.spec_from_file_location("setup_under_test", SETUP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    path = tmp_path / "config.json"
    path.write_text('{"old": true}\n')
    real = os.replace

    def boom(*a, **k):
        raise OSError("simulated crash at the rename")
    monkeypatch.setattr(os, "replace", boom)
    failure = None
    try:
        mod.write_cfg(str(tmp_path), {"new": 1})
    except OSError as exc:
        failure = exc
    assert failure is not None, "the new content reached config.json without os.replace"
    assert json.loads(path.read_text()) == {"old": True}
    assert [p.name for p in tmp_path.iterdir()] == ["config.json"]
    monkeypatch.setattr(os, "replace", real)
    mod.write_cfg(str(tmp_path), {"new": 1})
    assert json.loads(path.read_text()) == {"new": 1}


def test_a_missing_target_directory_says_how_to_create_the_repository(tmp_path, capsys):
    """#1246: `init_flow.py <missing dir>` printed only the path. Name the commands that make it a
    repository and the re-run."""
    flow = _load_flow()
    target = tmp_path / "nope"
    assert flow.main(["init_flow.py", str(target)]) == 2
    err = capsys.readouterr().err
    assert "does not exist" in err and "mkdir" in err and "git init" in err and "re-run /sigma-init" in err
