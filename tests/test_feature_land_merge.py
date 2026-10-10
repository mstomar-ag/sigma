"""#938: the landing engine's back half (feature_land_merge), proven offline with fakes and local bare remotes.

Nothing here reaches a network, a model or a hosting service. Controls (each seen red once by mutating the source):
the guard moved before the tip re-read and the record, the head pin dropped, the record written after the call, the
create-once marker check removed, and the gate decorator removed."""
import importlib.util
import json
import pathlib
import subprocess

import pytest

import attempt_trap
from attempt_trap import AttemptTrap
from test_feature_land_front import World, TIP, NEWTIP, BASE, SLUG, CONFIG, SCRIPTS, load as load_front

SRC = SCRIPTS / "feature_land_merge.py"
MC = "d" * 40
BRANCH = "feature/voice"


def load_merge(edit=None):
    text = SRC.read_text(encoding="utf-8")
    if edit:
        assert edit[0] in text, "mutation target drifted: %r" % (edit[0],)
        text = text.replace(*edit)
    spec = importlib.util.spec_from_loader("feature_land_merge_variant", loader=None)
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = str(SRC)
    exec(compile(text, str(SRC), "exec"), mod.__dict__)
    return mod


def landing_mod():
    spec = importlib.util.spec_from_file_location("ful2", SCRIPTS / "feature_upkeep_landing.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class MWorld(World):
    """The front-half fakes plus a merge: `mode` is ok | conflict (409, still open) | lost (no status, merged) |
    lost_unread (no status, read-back fails)."""

    def __init__(self, tmp_path, mode="ok", parents=None, **kw):
        super().__init__(tmp_path, **kw)
        self.mode, self.puts, self.pending_at_call, self.merged = mode, [], None, False
        self.parents = parents or [BASE, TIP]
        self.gone = False
        self.recorded = []

    def run(self, argv, cwd):
        if self.gone and self.merged and "ls-remote" in " ".join(argv) and "feature/voice" in " ".join(argv):
            return 0, "", ""
        return super().run(argv, cwd)

    def gh(self, args):
        a = " ".join(args)
        if "--method PUT" in a:
            self.gh_calls.append(list(args))
            self.puts.append(list(args))
            self.pending_at_call = landing_mod().record_path(self.sdlc, "voice").exists()
            if self.mode == "conflict":
                raise RuntimeError("HTTP 409: Head branch was modified")
            self.merged = self.mode in ("ok", "lost", "lost_unread")
            if self.mode.startswith("lost"):
                raise OSError("connection reset")
            return json.dumps({"sha": MC, "merged": True})
        if "commits/" in a:
            self.gh_calls.append(list(args))
            return json.dumps({"parents": [{"sha": p} for p in self.parents]})
        if "pulls/7" in a and "POST" not in a:
            self.gh_calls.append(list(args))
            if self.mode == "lost_unread" and self.merged:
                raise OSError("read failed")
            body = {"state": "closed" if self.merged else "open", "merged": self.merged, "head": {"sha": TIP},
                    "merge_commit_sha": MC if self.merged else None, "auto_merge": None}
            return json.dumps(body)
        return super().gh(args)

    def merge(self, **kw):
        kw.setdefault("record", lambda number: self.recorded.append(number))
        return self.land(merge=True, **kw)


def pending(w):
    return landing_mod().read_record(w.sdlc, "voice")


# ------------------------------------------------------------------------------------------------ the happy path

def test_merge_is_head_pinned_method_merge_and_record_precedes_the_call(tmp_path):
    w = MWorld(tmp_path)
    out = w.merge()
    assert out["outcome"] == "merged" and out["pr"] == 7 and out["head"] == TIP
    assert len(w.puts) == 1
    put = " ".join(w.puts[0])
    assert "sha=" + TIP in put and "merge_method=merge" in put
    assert "squash" not in put and "auto_merge" not in put and "DELETE" not in put
    assert w.pending_at_call is True
    assert pending(w) is None                      # deleted on merged
    assert w.recorded == [7] and out["recorded"] is True


def test_default_land_is_still_a_rehearsal_and_never_puts(tmp_path):
    w = MWorld(tmp_path)
    assert w.land()["outcome"] == "rehearsal"
    assert w.puts == []


def test_closed_gate_attempts_and_writes_nothing(tmp_path):
    before = sorted(p.as_posix() for p in tmp_path.rglob("*"))
    with AttemptTrap() as trap:
        out = load_merge().complete({}, tmp_path / ".sdlc", "voice", slug=SLUG, branch=BRANCH, base="main", head=TIP,
                                    base_tip=BASE, number=7, argv=[], environ={}, gh_run=None, read_tip=None)
    assert out.get("closed") is True
    assert (trap.processes, trap.models, trap.network, trap.writes) == ([], [], [], [])
    assert sorted(p.as_posix() for p in tmp_path.rglob("*")) == before


def test_control_gate_decorator_removed_is_seen(tmp_path):
    broken = load_merge(('if not verdict["open"]:', 'if False:'))
    with pytest.raises(Exception):
        broken.complete({}, tmp_path / ".sdlc", "voice", slug=SLUG, branch=BRANCH, base="main", head=TIP,
                        base_tip=BASE, number=7, argv=[], environ={}, gh_run=None, read_tip=None)


# ------------------------------------------------------------------------------------------------ the last gates

def test_moved_unit_tip_refuses_before_guard_record_and_call(tmp_path):
    w = MWorld(tmp_path, tips=[TIP, TIP, TIP, NEWTIP])
    out = w.merge()
    assert out["outcome"] == "refused:tip-moved"
    assert w.puts == [] and pending(w) is None


def test_guard_denial_leaves_no_record_and_no_call(tmp_path):
    w = MWorld(tmp_path)
    mod = load_merge()
    out = mod.complete(CONFIG, w.sdlc, "voice", slug=SLUG, branch=BRANCH, base="main", head=TIP, base_tip=BASE,
                       number=7, argv=[], environ={}, gh_run=w.gh, read_tip=lambda b: TIP if b == BRANCH else BASE)
    assert out["outcome"] == "refused:guard" and w.puts == [] and pending(w) is None


def test_unattended_approval_is_consumed_once_and_the_merge_proceeds(tmp_path):
    w = MWorld(tmp_path)
    spec = importlib.util.spec_from_file_location("fla3", SCRIPTS / "feature_land_approval.py")
    appr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(appr)
    assert appr.approve(CONFIG, w.sdlc, "voice", SLUG, TIP, ttl_seconds=86400, now=0)["ok"]
    out = w.merge(argv=(), now=1)
    assert out["outcome"] == "merged"
    again = appr.consume(CONFIG, w.sdlc, "voice", SLUG, TIP, now=2)
    assert again["ok"] is False                      # single use: the marker was taken before the call


def test_control_guard_before_the_tip_reread_is_seen(tmp_path):
    """CONTROL: with the guard moved ahead of the re-read, a moved tip spends the approval; the real order does not."""
    w = MWorld(tmp_path)
    spec = importlib.util.spec_from_file_location("fla4", SCRIPTS / "feature_land_approval.py")
    appr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(appr)
    appr.approve(CONFIG, w.sdlc, "voice", SLUG, TIP, ttl_seconds=86400, now=0)
    reads = lambda b: NEWTIP
    kw = dict(slug=SLUG, branch=BRANCH, base="main", head=TIP, base_tip=BASE, number=7, argv=[], environ={},
              gh_run=w.gh, read_tip=reads, now=1)
    assert load_merge().complete(CONFIG, w.sdlc, "voice", **kw)["outcome"] == "refused:tip-moved"
    assert appr.peek(CONFIG, w.sdlc, "voice", SLUG, TIP, now=1)["ok"] is True      # not spent
    broken = load_merge(("    if read_tip(branch) != head or read_tip(base) != base_tip:\n        return _refuse(\"tip-moved\", \"a tip moved after verify; nothing was approved or merged\")\n", ""))
    broken.complete(CONFIG, w.sdlc, "voice", **kw)
    assert appr.peek(CONFIG, w.sdlc, "voice", SLUG, TIP, now=1)["ok"] is False    # spent: the control bites


def test_record_that_cannot_be_written_means_no_call(tmp_path):
    w = MWorld(tmp_path)
    store = w.sdlc / "state"
    store.mkdir(parents=True)
    (store / "unit-landings").write_text("a file where the store directory should be")
    out = w.merge()
    assert out["outcome"].startswith("refused") and w.puts == []


def test_control_record_after_the_call_is_seen(tmp_path):
    """CONTROL: if the record were written after the call, the fake sees no pending file at the moment of the PUT."""
    text = (SCRIPTS / "feature_upkeep_landing.py").read_text(encoding="utf-8")
    assert text.index("started = begin(") < text.index("call = do_merge()")


# ------------------------------------------------------------------------------------------------ the read-back

def test_moved_head_at_the_host_is_refused_and_still_open(tmp_path):
    w = MWorld(tmp_path, mode="conflict")
    out = w.merge()
    assert out["outcome"] == "refused:status-409"
    assert pending(w)["outcome"] == "refused" and w.recorded == []


def test_lost_acknowledgment_with_merged_readback_is_merged(tmp_path):
    w = MWorld(tmp_path, mode="lost")
    assert w.merge()["outcome"] == "merged"


def test_lost_acknowledgment_unreadable_is_unconfirmed_and_pending_stays(tmp_path):
    w = MWorld(tmp_path, mode="lost_unread")
    out = w.merge()
    assert out["outcome"] == "unconfirmed:readback-failed"
    assert pending(w)["outcome"] == "unconfirmed"      # unsettled: the record stays
    assert w.land()["outcome"] == "refused:pending-landing"


def test_base_moved_after_the_read_is_merged_with_warning(tmp_path):
    w = MWorld(tmp_path, parents=["e" * 40, TIP])
    out = w.merge()
    assert out["outcome"] == "merged-with-warning" and out["recorded"] is True
    assert pending(w)["outcome"] == "merged-with-warning"


def test_merged_other_head_is_unconfirmed(tmp_path):
    w = MWorld(tmp_path, parents=[BASE, NEWTIP])
    assert w.merge()["outcome"] == "unconfirmed:merged-other-head" and w.recorded == []


# ------------------------------------------------------------------------------------------------ record once

def test_marker_is_create_once(tmp_path):
    mod = load_merge()
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir()
    assert mod.take_marker(sdlc, "voice", 7) is True
    assert mod.take_marker(sdlc, "voice", 7) is False
    assert mod.take_marker(sdlc, "voice", 8) is True


def test_second_completion_records_nothing_more(tmp_path):
    w = MWorld(tmp_path)
    assert load_merge().take_marker(w.sdlc, "voice", 7)
    out = w.merge()
    assert out["outcome"] == "merged" and out["recorded"] is False and w.recorded == []


def test_control_marker_check_removed_is_seen(tmp_path):
    w = MWorld(tmp_path)
    broken = load_merge(("    except FileExistsError:\n        return False\n", "    except FileExistsError:\n        return True\n"))
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir(exist_ok=True)
    assert broken.take_marker(sdlc, "voice", 7) and broken.take_marker(sdlc, "voice", 7)   # the second wrongly records


def test_recorder_failure_never_fails_the_landing(tmp_path):
    w = MWorld(tmp_path)
    def boom(number):
        raise RuntimeError("x")
    out = w.merge(record=boom)
    assert out["outcome"] == "merged" and out["recorded"] is False


# ------------------------------------------------------------------------------------------------ the branch check

def test_branch_gone_after_merge_is_reported_and_never_recreated(tmp_path):
    w = MWorld(tmp_path, gone=True)
    w.gone = True
    out = w.merge()
    assert out["outcome"] == "merged" and out["branch_gone"] is True and TIP in out["detail"]
    assert not [c for c in w.git_calls if c[:1] == ["git"] and ("push" in c or "update-ref" in c or "branch" in c)]


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def test_local_bare_remotes_prove_the_branch_check_and_that_no_ref_moves(tmp_path):
    with_branch, without = tmp_path / "a.git", tmp_path / "b.git"
    work = tmp_path / "w"
    git("init", "--bare", "-q", str(with_branch))
    git("init", "--bare", "-q", str(without))
    git("init", "-q", str(work))
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-q", "-m", "x", cwd=work)
    sha = git("rev-parse", "HEAD", cwd=work)
    git("push", "-q", str(with_branch), "HEAD:refs/heads/feature/voice", cwd=work)
    git("push", "-q", str(with_branch), "HEAD:refs/heads/main", cwd=work)
    (tmp_path / "x").mkdir()
    w = MWorld(tmp_path / "x", parents=[sha, sha])
    state = {"remote": with_branch}

    def read_tip(name):
        out = git("ls-remote", str(state["remote"]), "refs/heads/" + name)
        return out.split()[0] if out else None

    def gh(args):
        a = " ".join(args)
        if "--method PUT" in a:
            state["remote"] = without                   # a server-side delete of the branch after the merge
        return w.gh(args)
    before = git("show-ref", cwd=with_branch)
    w.tips = [TIP]
    out = load_merge().complete(CONFIG, w.sdlc, "voice", slug=SLUG, branch=BRANCH, base="main", head=sha,
                                base_tip=sha, number=7, argv=["--user-requested", "voice"], environ={},
                                gh_run=gh, read_tip=read_tip, record=lambda n: None)
    assert out["outcome"] == "merged" and out["branch_gone"] is True
    assert git("show-ref", cwd=with_branch) == before     # the engine moved no ref in the remote it was handed


# ------------------------------------------------------------------------------------------------ static shape

def test_merge_calls_live_only_in_the_back_half():
    front = (SCRIPTS / "feature_land.py").read_text(encoding="utf-8").split('"""', 2)[2]
    back = SRC.read_text(encoding="utf-8")
    assert "merge_pr_pinned" in back and "merge_pr_pinned" not in front and "merge_pr(" not in back
    for banned in ("--admin", "--delete-branch", "auto_merge=", "--auto", "delete_branch"):
        assert banned not in back.split('"""', 2)[2]


def test_control_dropped_head_pin_is_refused_by_the_helper(tmp_path):
    """CONTROL: the helper refuses an empty pin, so a variant that passes none cannot reach the host."""
    w = MWorld(tmp_path)
    broken = load_merge(("merge_pr_pinned(gh_run, slug, number, head, merge_method=MERGE_METHOD)",
                         "merge_pr_pinned(gh_run, slug, number, '', merge_method=MERGE_METHOD)"))
    out = broken.complete(CONFIG, w.sdlc, "voice", slug=SLUG, branch=BRANCH, base="main", head=TIP, base_tip=BASE,
                          number=7, argv=["--user-requested", "voice"], environ={}, gh_run=w.gh,
                          read_tip=lambda b: TIP if b == BRANCH else BASE)
    assert w.puts == [] and not out["outcome"].startswith("merged")


# ------------------------------------------------------------------------------------------------ verify_merge mapping

def load_verify_merge():
    spec = importlib.util.spec_from_file_location("vm938", SCRIPTS.parent.parent / "sigma-rebase" / "scripts" / "verify_merge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("config", [{}, {"upkeep": {"enabled": False}}, {"upkeep": {"enabled": "true"}}])
def test_verify_merge_closed_gate_is_byte_identical_and_loads_no_engine(config, capsys):
    vm = load_verify_merge()
    loaded = []
    real = vm._load
    vm._load = lambda name, directory=None: (loaded.append(name), real(name, directory))[1]
    asked = []
    assert vm._engine_route(config, ".sdlc", "feature/voice", lambda: asked.append(1) or True) is None
    assert loaded == ["feature_upkeep"] and asked == []        # only the gate was read; no prompt, no engine
    assert capsys.readouterr() == ("", "")
    assert vm.USAGE == "usage: verify_merge.py land <sdlc_dir> [branch]"


def test_verify_merge_open_gate_routes_and_maps_outcomes(capsys):
    vm = load_verify_merge()
    real, calls = vm._load, []

    class Engine:
        outcome = "merged"
        def land(self, config, sdlc_dir, unit, **kw):
            calls.append((unit, kw["merge"], kw["argv"]))
            return {"outcome": Engine.outcome}
    vm._load = lambda name, directory=None: Engine() if name == "feature_land" else real(name, directory)
    assert vm._engine_route(CONFIG, ".sdlc", "feature/voice", lambda: True) == 0
    assert calls == [("voice", True, ["--user-requested", "voice"])]
    Engine.outcome = "refused:guard"
    assert vm._engine_route(CONFIG, ".sdlc", "feature/voice", lambda: True) == 1
    Engine.outcome = "unconfirmed:readback-failed"
    assert vm._engine_route(CONFIG, ".sdlc", "feature/voice", lambda: True) == 3
    assert vm._engine_route(CONFIG, ".sdlc", "sdlc/938", lambda: True) is None       # not a unit branch: old path
    assert vm._engine_route(CONFIG, ".sdlc", "feature/voice", lambda: False) == 0    # declined: nothing runs
    assert len(calls) == 3
