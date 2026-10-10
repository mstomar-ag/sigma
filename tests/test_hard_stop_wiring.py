"""Decision rubric slice 10: the hard-stop classifier wired at Sigma-owned chokepoints (`hard_stop_guard.py`).

Closed (the default), every chokepoint behaves exactly as before; open, a matching action parks. Controls run once
each are named in the docstrings (remove the guard call, narrow the predicate to the prefix list, add merge to the
pattern table): a test whose docstring says "red only against the broken control" is not red on the shipped code.
"""
import ast
import importlib.util
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


G = _load("hard_stop_guard")
H = _load("hard_stop")
work = _load("work")
state = _load("state")
sources = _load("sources")

OPEN = {"work": {"enabled": True}, "decision_rubric": {"hard_stops": {"enabled": True}}}
CLOSED = {"work": {"enabled": True}}
PUSH_OWN = "git push --force-with-lease origin HEAD:sdlc/0001-x"


# -- the guard and the own predicate ---------------------------------------------------------------

def test_force_push_to_shared_branch_parks():
    v = G.guard("git push --force origin main", {"branch": "main"}, OPEN)
    assert v.blocked and v.cls == "rewrite_history" and v.pattern_id == "rewrite-force-push"
    assert "rewrite-force-push" in v.reason


def test_own_lease_push_passes():
    """Red only against the broken control (guard narrowed to ignore the own predicate), not on shipped code."""
    v = G.guard(PUSH_OWN, {"branch": "sdlc/0001-x"}, OPEN)
    assert not v.blocked


def test_upkeep_feature_push_is_own():
    """Red with the prefix-only default (no registry), green with the register rule."""
    text = "git push --force-with-lease=feature/voice:abc origin HEAD:refs/heads/feature/voice"
    registry = {"voice": {"name": "voice"}}
    assert not G.guard(text, {"branch": "feature/voice", "registry": registry}, OPEN).blocked
    assert G.guard(text, {"branch": "feature/voice", "registry": None}, OPEN).blocked
    assert G.is_own("feature/voice", registry, OPEN) and not G.is_own("feature/voice", {}, OPEN)


def test_unregistered_feature_push_parks():
    v = G.guard("git push --force origin feature/other", {"branch": "feature/other", "registry": {"voice": {}}}, OPEN)
    assert v.blocked and v.cls == "rewrite_history"


def test_auto_merge_is_not_classified():
    """Red when merge is added to the pattern table."""
    for text in ("gh pr merge 7 --auto --squash", "gh pr merge 7 --squash"):
        assert not G.guard(text, {"own_merge": True}, OPEN).blocked
        assert not G.guard(text, {}, OPEN).blocked
    for rows in H._PATTERNS.values():
        assert not any("merge" in rx.pattern.lower() for _pid, rx in rows if "git" not in rx.pattern)


def test_closed_guard_reads_nothing():
    class Boom:
        def __getattr__(self, name):
            raise AssertionError("closed gate touched the source: " + name)
    for cfg in (CLOSED, {"decision_rubric": {"hard_stops": {"enabled": "true"}}}, None):
        assert G.guard("rm -rf /", {}, cfg) == G.ALLOW
        if cfg is not None:
            assert G.guard_issue_write(Boom(), "5", "complete", config=cfg) == G.ALLOW
        assert G.record_done_net([{"kind": "x", "action": "rm -rf /"}], cfg) == []


# -- work.py chokepoints ---------------------------------------------------------------------------

def _sdlc(tmp_path, config):
    d = tmp_path / ".sdlc"
    (d / "state").mkdir(parents=True)
    (d / "config.json").write_text(json.dumps(config))
    state.start_run(str(d))
    return str(d)


def _started(sdlc_dir, branch="sdlc/0001-x"):
    wt = pathlib.Path(sdlc_dir) / "work" / "0001-x"
    wt.mkdir(parents=True, exist_ok=True)
    work._save(sdlc_dir, "0001-x.md", {"worktree": str(wt), "branch": branch, "base": "main",
                                       "remote": "origin", "pr": "7"})
    return "0001-x.md"


def _runner(diff=""):
    calls = []

    def run(cwd, argv):
        line = " ".join(str(a) for a in argv)
        calls.append(line)
        if "--name-status -z" in line:
            return "A\0a.py\0"
        if "diff --cached --name-only" in line:
            return "a.py"
        if line.startswith("git diff"):
            return diff
        return ""
    run.calls = calls
    return run


def test_closed_makes_identical_calls(tmp_path):
    """Gate closed or absent: the same commands in the same order, the same string."""
    out = []
    for i, cfg in enumerate((CLOSED, {**CLOSED, "decision_rubric": {"hard_stops": {"enabled": False}}})):
        d = _sdlc(tmp_path / str(i), cfg)
        goal = _started(d)
        run = _runner(diff="x\n+git commit --no-verify\n")
        out.append((work.commit(d, cfg, goal, run=run, message="m"), run.calls))
    assert out[0] == out[1]
    assert out[0][0] == "committed on sdlc/0001-x"
    assert out[0][1] == ["git add -A", "git diff --cached --name-only", "git commit -m m"]
    d = _sdlc(tmp_path / "p", CLOSED)
    rec = {"branch": "main"}
    assert work._hs_push_stop(d, CLOSED, rec, "git push --force origin main") == ""


def test_commit_open_refuses_tamper_in_the_staged_diff(tmp_path):
    d = _sdlc(tmp_path, OPEN)
    goal = _started(d)
    run = _runner(diff="+git commit --no-verify -m x\n")
    out = work.commit(d, OPEN, goal, run=run, message="m")
    assert out.startswith("REFUSED: hard stop") and "tamper-no-verify" in out
    assert not any(c.startswith("git commit") for c in run.calls)


def test_commit_open_ignores_an_unbalanced_quote_in_the_diff(tmp_path):
    d = _sdlc(tmp_path, OPEN)
    goal = _started(d)
    run = _runner(diff="+it's a \"quote\n")
    assert work.commit(d, OPEN, goal, run=run, message="m") == "committed on sdlc/0001-x"


def test_push_stop_passes_the_goal_branch_and_parks_a_shared_one(tmp_path):
    d = _sdlc(tmp_path, OPEN)
    assert work._hs_push_stop(d, OPEN, {"branch": "sdlc/0001-x"}, PUSH_OWN) == ""
    out = work._hs_push_stop(d, OPEN, {"branch": "main"}, "git push --force-with-lease origin HEAD:main")
    assert out.startswith("PARK: hard stop: rewrite_history (rewrite-force-push)")


def test_rebase_and_pr_and_reconcile_call_the_push_check():
    """Structural: every named push in work.py is preceded by the check (red when the guard call is removed)."""
    src = (SCRIPTS / "work.py").read_text()
    tree = ast.parse(src)
    for fn in ("rebase", "pr"):
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == fn)
        text = ast.get_source_segment(src, node)
        assert text.count("_hs_push_stop(") >= (2 if fn == "rebase" else 1)
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "commit")
    assert "_hs_diff_stop(" in ast.get_source_segment(src, node)
    rb = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_reconcile_behind")
    assert "rebase(" in ast.get_source_segment(src, rb)       # its lease push goes through rebase()


def test_feature_rebase_guards_before_the_push():
    src = (SCRIPTS / "feature_rebase.py").read_text()
    node = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "_rebase_pass")
    text = ast.get_source_segment(src, node)
    assert text.index("hsg.guard(") < text.index("_rebase_feature(")


# -- issue writes ----------------------------------------------------------------------------------

class Stub:
    goal_label, parked_label = "sdlc:goal", "sdlc:parked"

    def __init__(self, labels=(), body="", cfg=OPEN):
        self._hs_config = cfg
        self.labels, self.body, self.reads, self.parks = list(labels), body, 0, []

    def _read_issue(self, goal, fields, comment_limit=None):
        self.reads += 1
        return {"labels": [{"name": n} for n in self.labels], "body": self.body}

    def park(self, goal, reason, tier=None):
        # the guard's own park must not be guarded again
        assert sources.hsg.guard_issue_write(self, goal, "park") == G.ALLOW
        self.parks.append((goal, reason))


def test_foreign_issue_close_parks():
    s = Stub()
    v = G.guard_issue_write(s, "5", "complete")
    assert v.blocked and v.cls == "cannot-tell"
    assert sources.GitHubSource.complete(s, "5") is None
    assert len(s.parks) == 1 and "hard stop" in s.parks[0][1]


def test_guard_park_does_not_recurse():
    """Red when the guard's own park is guarded again: the park would read and park forever."""
    s = Stub()
    assert sources.hsg.blocks_issue_write(s, "5", "complete") is True
    assert s.reads == 1 and len(s.parks) == 1


def test_sigma_labelled_or_created_issue_is_allowed():
    assert not G.guard_issue_write(Stub(labels=["sdlc:goal"]), "5", "complete").blocked
    assert not G.guard_issue_write(Stub(labels=["sdlc:parked"]), "5", "note").blocked
    marked = Stub(body="Raised automatically by the SDLC loop from goal `x`.")
    assert not G.guard_issue_write(marked, "5", "release").blocked
    assert G.guard_issue_write(Stub(labels=["bug"]), "5", "note").blocked


def test_unreadable_issue_is_cannot_tell():
    s = Stub()
    s._read_issue = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
    assert G.guard_issue_write(s, "5", "complete").blocked


def test_via_guard_is_exempt():
    assert G.guard_issue_write(Stub(), "5", "park", via_guard=True) == G.ALLOW


# -- record-done net, level-2 cap, alias, docs -----------------------------------------------------

def test_record_done_refuses_unrecorded_stop():
    log = [{"kind": "agent_note", "action": "git push --force origin main"}]
    out = G.record_done_net(log, OPEN)
    assert out and "rewrite-force-push" in out[0]
    recorded = log + [{"kind": "park", "action": "git push --force origin main"}]
    assert G.record_done_net(recorded, OPEN) == []
    assert G.record_done_net(log, CLOSED) == []


def test_no_path_maps_a_stop_to_auto_apply():
    """Structural level-2 pin: no function in the scripts touches a hard-stop class and an auto-apply path at once."""
    offenders = []
    for path in sorted(SCRIPTS.glob("*.py")):
        src = path.read_text()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                text = (ast.get_source_segment(src, node) or "").lower()
                if ("hardstop_class" in text or "is_hardstop_kind" in text) and ("auto_apply" in text or "autoapply" in text):
                    offenders.append("%s:%s" % (path.name, node.name))
    assert offenders == []


def test_legacy_alias_folds_into_the_class_toggles():
    cfg = {**OPEN, "gates": {"irreversible_actions": ["deploy", "spend", "migrate", "overwrite"]}}
    assert not G.guard("rm -rf /var/data", {}, cfg).blocked           # delete removed -> destroy off
    assert G.guard("kubectl apply -f x.yaml", {}, cfg).blocked        # deploy kept
    named = {**cfg, "decision_rubric": {"hard_stops": {"enabled": True, "classes": {"destroy": True}}}}
    assert G.guard("rm -rf /var/data", {}, named).blocked             # classes wins over the alias
    assert G.guard("rm -rf /var/data", {}, OPEN).blocked              # no alias list: default on


def test_enforcement_doc_matches_generator():
    """The committed table carries the hard-stop row (byte equality with the generator is pinned in
    tests/test_enforcement_table.py)."""
    doc = (ROOT / "docs" / "enforcement.md").read_text()
    assert "decision_rubric.hard_stops.enabled" in doc
