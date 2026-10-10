"""#937: the landing engine's front half (feature_land), proven offline with a fake git and a fake gh.

Nothing here reaches a network, a model or a real repository. Controls (each seen red once by mutating the source):
the closed gate that still works, the scheduler closure that gains an import, and the unattended check moved after
the push."""
import ast
import importlib.util
import json
import pathlib
import types

import pytest

import attempt_trap
from attempt_trap import AttemptTrap

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"
SRC = SCRIPTS / "feature_land.py"
TIP, NEWTIP, BASE = "a" * 40, "c" * 40, "b" * 40
SLUG = "owner/repo"
CONFIG = {"upkeep": {"enabled": True}, "work": {"base": "main"}, "verify": {"command": "true"},
          "discovery": {"github": {"repo": SLUG}}}


def load(edit=None):
    text = SRC.read_text(encoding="utf-8")
    if edit:
        assert edit[0] in text, "mutation target drifted: %r" % (edit[0],)
        text = text.replace(*edit)
    spec = importlib.util.spec_from_loader("feature_land_variant", loader=None)
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = str(SRC)
    exec(compile(text, str(SRC), "exec"), mod.__dict__)
    return mod


class World:
    """A fake git runner and a fake gh runner that record every call and refuse nothing they know."""

    def __init__(self, tmp_path, **kw):
        self.sdlc = tmp_path / ".sdlc"
        self.sdlc.mkdir(exist_ok=True)
        self.tips = [TIP]                      # successive answers for the unit branch; the last repeats
        self.ancestor_rc = 1
        self.remote_url = "https://github.com/owner/repo.git"
        self.rules, self.prs = [], []
        self.settings = {"allow_merge_commit": True, "delete_branch_on_merge": False}
        self.pr_list_rc = 0
        self.git_calls, self.gh_calls, self.passes, self.verifies = [], [], [], []
        self.view = None
        self.__dict__.update(kw)

    def run(self, argv, cwd):
        self.git_calls.append(list(argv))
        a = " ".join(argv)
        if argv[0] == "gh":
            if self.pr_list_rc:
                return self.pr_list_rc, "", "boom"
            return 0, json.dumps(self.prs), ""
        if "remote get-url" in a:
            return 0, self.remote_url + "\n", ""
        if "ls-remote" in a:
            if a.endswith("refs/heads/main"):
                return 0, "%s\trefs/heads/main\n" % BASE, ""
            tip = self.tips.pop(0) if len(self.tips) > 1 else self.tips[0]
            return 0, "%s\trefs/heads/feature/voice\n" % tip, ""
        if "rev-parse" in a:
            return 0, (BASE if "origin/main" in a else self.tips[0]) + "\n", ""
        if "is-ancestor" in a:
            return self.ancestor_rc, "", ""
        return 0, "", ""

    def gh(self, args):
        self.gh_calls.append(list(args))
        a = " ".join(args)
        if "rules/branches" in a:
            return json.dumps(self.rules)
        if "--method POST" in a:
            return json.dumps({"number": 7})
        if "pulls/7" in a:
            return json.dumps(self.view or {"state": "open", "merged": False, "head": {"sha": self.tips[-1]}})
        return json.dumps(self.settings)

    def rebase_pass(self, sdlc_dir, config, goal, unit, run=None, cwd=None, remote=None):
        self.passes.append(unit)
        return {"outcome": "current"}

    def verify(self, command, tree, seconds):
        self.verifies.append(command)
        return types.SimpleNamespace(outcome="ok")

    def land(self, mod=None, config=CONFIG, argv=("--user-requested", "voice"), environ=None, **kw):
        mod = mod or load()
        return mod.land(config, self.sdlc, "voice", argv=list(argv), environ={} if environ is None else environ,
                        run=self.run, gh_run=self.gh, rebase_pass=self.rebase_pass, verify=self.verify, **kw)

    def posts(self):
        return [c for c in self.gh_calls if "POST" in c]


def tree(root):
    return sorted((p.relative_to(root).as_posix(), p.read_bytes() if p.is_file() else None) for p in root.rglob("*"))


# ---------------------------------------------------------------------------------------------- the gate

@pytest.mark.parametrize("config", [{}, {"upkeep": {"enabled": "true"}}, {"upkeep": {"enabled": False}}])
def test_closed_gate_attempts_and_writes_nothing(tmp_path, config):
    before = tree(tmp_path)
    with AttemptTrap() as trap:
        out = load().land(config, tmp_path / ".sdlc", "voice", argv=[], environ={})
    assert out.get("closed") is True
    assert (trap.processes, trap.models, trap.network, trap.writes) == ([], [], [], [])
    assert tree(tmp_path) == before


def test_control_gate_that_does_not_close_is_seen(tmp_path):
    """CONTROL: with the gate check removed, the same closed call attempts a process, so the trap above can fail."""
    broken = load(('if not verdict["open"]:', 'if False:'))
    with AttemptTrap() as trap:
        try:
            broken.land({}, tmp_path / ".sdlc", "voice", argv=[], environ={})
        except Exception:
            pass
    assert trap.processes or trap.writes or trap.network


def test_enabled_gate_makes_attempts(tmp_path):
    """The fully enabled control: the real runner is vetoed by the trap, and the attempt is recorded."""
    (tmp_path / ".sdlc").mkdir()
    with AttemptTrap() as trap:
        out = load().land(CONFIG, tmp_path / ".sdlc", "voice", argv=[], environ={})
    assert trap.processes
    assert out["outcome"].startswith("refused:")


def test_gate_is_first_and_outermost():
    tree_ = ast.parse(SRC.read_text(encoding="utf-8"))
    public = [n for n in tree_.body if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")
              and n.name not in ("refuse", "main")]
    assert [n.name for n in public] == ["land"]
    assert [d.id for d in public[0].decorator_list] == ["_gate_first"]
    guard = next(n for n in tree_.body if isinstance(n, ast.FunctionDef) and n.name == "_gate_first")
    inner = next(n for n in ast.walk(guard) if isinstance(n, ast.FunctionDef) and n.name == "guarded")
    assert "evaluate" in ast.dump(inner.body[0])


# ---------------------------------------------------------------------------------------------- refusals

def reason(out):
    return out["outcome"]


def test_verify_not_configured(tmp_path):
    cfg = dict(CONFIG, verify={})
    assert reason(World(tmp_path).land(config=cfg)) == "refused:verify-not-configured"


def test_slug_mismatch(tmp_path):
    w = World(tmp_path, remote_url="https://github.com/other/thing.git")
    assert reason(w.land()) == "refused:slug-mismatch"
    assert w.passes == [] and w.gh_calls == []


def test_merge_queue_declines_before_verify_spend(tmp_path):
    w = World(tmp_path, rules=[{"type": "merge_queue"}])
    assert reason(w.land()) == "refused:merge-queue"
    assert w.verifies == [] and w.passes == []


def test_merge_commits_not_allowed(tmp_path):
    w = World(tmp_path, settings={"allow_merge_commit": False, "delete_branch_on_merge": False})
    assert reason(w.land()) == "refused:merge-commits-not-allowed"


def test_delete_branch_on_merge(tmp_path):
    w = World(tmp_path, settings={"allow_merge_commit": True, "delete_branch_on_merge": True})
    assert reason(w.land()) == "refused:delete-branch-on-merge"


def test_unknown_landed_state_refuses(tmp_path):
    w = World(tmp_path, pr_list_rc=1)
    out = w.land()
    assert reason(out) == "refused:unknown"
    assert w.passes == []


def test_already_landed_is_success_and_writes_nothing(tmp_path):
    w = World(tmp_path, ancestor_rc=0)
    before = tree(tmp_path)
    out = w.land()
    assert out["outcome"] == "already-landed"
    assert w.passes == [] and w.verifies == [] and w.posts() == []
    assert tree(tmp_path) == before


def test_pending_record_is_not_relanded(tmp_path):
    w = World(tmp_path)
    landing = importlib.util.spec_from_file_location("ful", SCRIPTS / "feature_upkeep_landing.py")
    mod = importlib.util.module_from_spec(landing)
    landing.loader.exec_module(mod)
    path = mod.record_path(w.sdlc, "voice")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema": mod.SCHEMA_ID, "unit": "voice"}))
    assert reason(w.land()) == "refused:pending-landing"


# ---------------------------------------------------------------------------------------------- the PR step

def pr(number, **kw):
    row = {"number": number, "state": "open", "draft": False, "merged_at": None, "created_at": "2026-01-0%dT00:00:00Z" % number,
           "head_sha": TIP, "base_ref": "main"}
    row.update(kw)
    return row


def test_rehearsal_creates_one_nondraft_pr_and_never_merges(tmp_path):
    w = World(tmp_path)
    out = w.land()
    assert out["outcome"] == "rehearsal" and out["pr"] == 7 and out["head"] == TIP
    assert w.passes == ["voice"] and w.verifies == ["true"]
    assert len(w.posts()) == 1 and "draft=false" in w.posts()[0]
    assert not [c for c in w.gh_calls if "PUT" in c or any(str(x).endswith("/merge") for x in c)]


def test_existing_open_pr_is_reused_and_closed_other_head_never_blocks(tmp_path):
    w = World(tmp_path, prs=[pr(3, state="closed", head_sha="d" * 40), pr(7)])
    assert w.land()["outcome"] == "rehearsal"
    assert w.posts() == []
    w = World(tmp_path, prs=[pr(3, state="closed", head_sha="d" * 40)])
    assert w.land()["outcome"] == "rehearsal" and len(w.posts()) == 1


def test_open_draft_refuses_naming_the_pr(tmp_path):
    w = World(tmp_path, prs=[pr(5, draft=True)])
    out = w.land()
    assert out["outcome"] == "refused:draft-open" and out["pr"] == 5 and w.posts() == []


def test_pr_not_open_at_head_refuses(tmp_path):
    w = World(tmp_path, view={"state": "open", "merged": True, "head": {"sha": TIP}})
    assert reason(w.land()) == "refused:pr-not-open-at-head"


# ---------------------------------------------------------------------------------------------- tips

def test_tip_that_keeps_moving_stops_after_three_attempts(tmp_path):
    shas = [("%040x" % n) for n in range(1, 40)]
    w = World(tmp_path, tips=shas)
    out = w.land()
    assert out["outcome"] == "refused:tip-kept-moving"
    assert len(w.passes) == 3 and len(w.verifies) == 3 and w.posts() == []


def test_tip_that_moves_once_restarts_from_the_pass(tmp_path):
    # reads: early tip, candidate (attempt 1), re-read (moved), candidate (attempt 2), re-read, final
    w = World(tmp_path, tips=[TIP, TIP, NEWTIP, NEWTIP, NEWTIP])
    out = w.land()
    assert out["outcome"] == "rehearsal" and len(w.passes) == 2 and out["head"] == NEWTIP


# ---------------------------------------------------------------------------------------------- unattended

def approve(w, head, now=0):
    mod = importlib.util.spec_from_file_location("fla", SCRIPTS / "feature_land_approval.py")
    m = importlib.util.module_from_spec(mod)
    mod.loader.exec_module(m)
    assert m.approve(CONFIG, w.sdlc, "voice", SLUG, head, ttl_seconds=86400, now=0)["ok"]
    return m


def test_unattended_without_approval_refuses_before_any_push(tmp_path):
    w = World(tmp_path)
    out = w.land(argv=[], environ={})
    assert out["outcome"] == "refused:unattended-no-approval"
    assert w.passes == [] and w.posts() == []
    assert not [c for c in w.git_calls if "push" in c]


def test_unattended_with_approval_rehearses_and_consumes_nothing(tmp_path):
    w = World(tmp_path)
    m = approve(w, TIP)
    assert w.land(argv=[], environ={}, now=5)["outcome"] == "rehearsal"
    assert not m.approval_path(w.sdlc, SLUG, TIP, "voice")[1].exists()


def test_driven_launcher_denies_the_consent_flag(tmp_path):
    w = World(tmp_path)
    assert w.land(environ={"SIGMA_RUN_ID": "x"})["outcome"] == "refused:unattended-no-approval"


def test_pass_that_moves_the_tip_voids_the_approval(tmp_path):
    w = World(tmp_path, tips=[TIP, NEWTIP])
    approve(w, TIP)
    out = w.land(argv=[], environ={}, now=5)
    assert out["outcome"] == "refused:tip-changed" and NEWTIP in out["detail"]


def test_control_unattended_check_after_the_pass_is_seen(tmp_path):
    """CONTROL: with the early approval check removed, a refused request reaches the pass (the push)."""
    broken = load(("    if unattended:\n        got = approval.peek", "    if False:\n        got = approval.peek"))
    w = World(tmp_path)
    w.land(mod=broken, argv=[], environ={})
    assert w.passes, "the control should have reached the pass"


# ---------------------------------------------------------------------------------------------- structure

SCHEDULER_ROOTS = ("watch_daemon", "watch", "agent_watch", "comment_watch", "reconcile_tick", "drift_tick",
                   "autowatch", "supervise_daemon", "channel_notify", "sync")


def references(source, stems):
    """The local script stems a source names: imports and any string constant equal to a stem or stem.py."""
    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value[:-3] if node.value.endswith(".py") else node.value
            found.add(text)
    return found & stems


def closure(sources, roots):
    stems = set(sources)
    seen, todo = set(), [r for r in roots if r in sources]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        todo.extend(references(sources[name], stems) - seen)
    return seen


def shipped_sources():
    return {p.stem: p.read_text(encoding="utf-8") for p in SCRIPTS.glob("*.py")}


def test_scheduler_closure_cannot_reach_the_engine():
    sources = shipped_sources()
    assert "feature_land" in sources
    reached = closure(sources, SCHEDULER_ROOTS)
    assert len(reached) > 5, "the closure walk found almost nothing; the test is blind"
    assert "feature_land" not in reached


def test_control_scheduler_closure_with_an_added_import_is_seen():
    """CONTROL: add the import to the scheduler's own daemon and the same walk reports the engine."""
    sources = shipped_sources()
    sources["watch_daemon"] += "\nimport feature_land\n"
    assert "feature_land" in closure(sources, SCHEDULER_ROOTS)
    sources = shipped_sources()
    sources["watch_daemon"] += '\n_load("feature_land")\n'
    assert "feature_land" in closure(sources, SCHEDULER_ROOTS)


def test_engine_names_no_merge_operation():
    text = SRC.read_text(encoding="utf-8")
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    for banned in ("merge_pr", "merge_pr_pinned", "--method PUT", "pulls/%d/merge"):
        assert banned not in code.split('"""', 2)[2], banned


def test_cli_verb_closed_gate_exits_4(tmp_path, capsys):
    (tmp_path / ".sdlc").mkdir()
    (tmp_path / ".sdlc" / "config.json").write_text("{}")
    assert load().main(["land", "voice", "--state-dir", str(tmp_path / ".sdlc"), "--rehearse"]) == 4
    assert "gate is closed" in capsys.readouterr().err
