"""#954: `work.py start` must not refuse on the registry files its own sync wrote, and must still
refuse on everything else.

`_dirty_root_refusal` (#2014) refuses a start while the ROOT checkout carries tracked, uncommitted
edits on `work.base`. Once a human commits `.sdlc/features/` (branching-model §14 step 3 says to),
every later pick's registry sync rewrites `units/<unit>.json` and `<unit>.md` in that root -- so the
NEXT start refused on Sigma's own write and told the operator to shelve it with git's own shelving
command, which is how 17 shelved changes buried real work.

The fix exempts a registry file only when its current bytes AND its committed bytes are both in
Sigma's own recorded write chain (`feature_provenance`). These tests drive real git end to end
(tests/registry_root.py) except where a fake runner is the only way to pin a call shape.

Every body runs inside `registry_root.drained(capfd)`: one captured-output section anywhere in a
verify run voids every red that run would record (plan §7, measured).
"""
import inspect
import json
import os
import re
import subprocess

import registry_root
from registry_root import PAGE, SHARD, World

V1 = "git status --porcelain"
HEADQ = "git rev-parse --abbrev-ref HEAD"
V2 = "git status --porcelain=v2 --untracked-files=no -- .sdlc/features"
#: `work._run` strips stdout, so a first line ` M <path>` reaches the guard as `M <path>` (BR-9).
STRIPPED_REGISTRY_DIRT = "M .sdlc/features/units/voice.json\n M .sdlc/features/voice.md"
ZEROS = "0" * 40


def _fake(answers):
    """An exact-argv runner: the joined argv is looked up; an Exception answer is raised; anything
    unlisted answers "" (git's honest output for most reads)."""
    calls = []

    def run(cwd, argv):
        line = " ".join(str(a) for a in argv)
        calls.append(line)
        answer = answers.get(line, "")
        if isinstance(answer, Exception):
            raise answer
        return answer

    run.calls = calls
    return run


def _sigma_dirt(w):
    """The issue's repro up to its third step: a pick, the §15 commit, a second pick. -> the root's
    raw porcelain, asserted to be exactly Sigma's two registry writes."""
    first = w.start("100")
    assert first.startswith("worktree"), first
    w.legacy_pick(100)
    w.commit_registry()
    second = w.start("101")
    assert second.startswith("worktree"), second
    w.legacy_pick(101)
    lines = w.porcelain().splitlines()
    assert lines == [" M " + SHARD, " M " + PAGE], lines
    return lines


def test_a_start_after_the_registry_commit_is_not_refused_by_its_own_sync(tmp_path, capfd):
    """AC-1, the issue's three steps verbatim, through `work.start` with the absolute `.sdlc`."""
    with registry_root.drained(capfd):
        w = World(tmp_path).build()
        _sigma_dirt(w)
        third = w.start("102")
        assert not third.startswith("REFUSED"), third
        assert third.startswith("worktree"), third
        assert w.work._record(str(w.sdlc), "102") is not None


def test_a_resume_after_the_registry_commit_is_not_refused_by_its_own_sync(tmp_path, capfd):
    """The guard runs before the resume branch too (`start()` checks it first), so a supervisor
    relaunch of an already-started goal was refused by the same dirt."""
    with registry_root.drained(capfd):
        w = World(tmp_path).build()
        _sigma_dirt(w)
        again = w.start("100")
        assert again.startswith("already started"), again


def test_an_ordinary_tracked_edit_still_refuses_beside_sigma_registry_dirt(tmp_path, capfd):
    """AC-2: proven registry dirt never launders a human's edit to an ordinary tracked file."""
    with registry_root.drained(capfd):
        w = World(tmp_path).build()
        _sigma_dirt(w)
        (w.root / "seed.txt").write_text("seed\nedited straight in root\n", encoding="utf-8")
        out = w.start("102")
        assert out.startswith("REFUSED") and "tracked" in out, out
        assert w.work._record(str(w.sdlc), "102") is None


def test_a_human_note_below_the_managed_block_still_refuses(tmp_path, capfd):
    """AC-2: §12 invites a human to write below the page's managed block. That edit is a human's,
    whatever file it sits in, and must still stop the start."""
    with registry_root.drained(capfd):
        w = World(tmp_path).build()
        _sigma_dirt(w)
        page = w.root / PAGE
        page.write_bytes(page.read_bytes() + b"\nA note a human wrote below the block.\n")
        out = w.start("102")
        assert out.startswith("REFUSED"), out
        assert w.work._record(str(w.sdlc), "102") is None


def test_a_human_shard_edit_that_sigma_then_wrote_over_still_refuses(tmp_path, capfd):
    """AC-2, the fold the dossier measured: `amend` folds an in-schema human edit into its own
    write. Sigma wrote the current bytes -- but on top of a human's, so HEAD is not in the chain
    that leads to them, and the start must refuse rather than vouch for the hidden edit."""
    with registry_root.drained(capfd):
        w = World(tmp_path).build()
        assert w.start("100").startswith("worktree")
        w.legacy_pick(100)
        w.commit_registry()
        shard = w.root / SHARD
        edited = shard.read_text(encoding="utf-8").replace('"authorized": false', '"authorized": true')
        assert '"authorized": true' in edited
        shard.write_text(edited, encoding="utf-8")
        w.sigma_shard_write("101")
        after = json.loads(shard.read_text(encoding="utf-8"))
        mine = after["features"]["voice"]["repos"][registry_root.REPO]
        assert mine["authorized"] is True and 101 in mine["goals"], mine
        assert w.porcelain().splitlines() == [" M " + SHARD]
        out = w.check()
        assert out is not None and out.startswith("REFUSED"), out


def _staged_or_moded(w, case):
    """One shard Sigma wrote after the §15 commit (chain [C, W1]), then `case` applied. -> the
    v2 entry, its columns asserted."""
    assert w.start("100").startswith("worktree")
    w.legacy_pick(100)
    w.commit_registry()
    committed = registry_root.blob_id(w.read(SHARD))
    w.sigma_shard_write("101")
    if case in ("staged", "staged-then-written"):
        registry_root.git(w.root, "add", SHARD)
    if case == "staged-then-written":
        w.sigma_shard_write("102")
    if case == "chmod":
        os.chmod(w.root / SHARD, 0o755)
    entry = w.v2_entry(SHARD)
    assert entry is not None and entry[0] == "1", entry
    xy, m_index, m_worktree, h_head, h_index = entry[1], entry[4], entry[5], entry[6], entry[7]
    assert h_head == committed, (h_head, committed)
    if case == "staged":
        assert xy == "M." and h_index == registry_root.blob_id(w.read(SHARD)), entry
    if case == "staged-then-written":
        assert xy == "MM" and h_index != h_head, entry
    if case == "chmod":
        assert xy == ".M" and m_index == "100644" and m_worktree == "100755", entry
    return entry


def test_a_staged_or_mode_changed_registry_file_still_refuses(tmp_path, capfd):
    """AC-2: Sigma never stages and never changes a mode, so a registry line that is staged (`M.`,
    `MM`) or carries a worktree mode change (`.M` with mI != mW) is a human's act, and refuses even
    though every byte involved is in Sigma's own chain. The chmod case needs `core.fileMode`, which
    git sets at init by probing the filesystem (false on Windows, where that line cannot occur)."""
    with registry_root.drained(capfd):
        cases = ["staged", "staged-then-written"]
        probe = World(tmp_path / "probe").build()
        if registry_root.git(probe.root, "config", "--get", "core.fileMode").strip() == "true":
            cases.append("chmod")
        for case in cases:
            w = World(tmp_path / case).build()
            _staged_or_moded(w, case)
            out = w.check()
            assert out is not None and out.startswith("REFUSED"), (case, out)


def test_a_hand_revert_to_an_older_sigma_state_is_not_laundered(tmp_path, capfd):
    """D1(b): C0 is Sigma's, C1 is committed, a human reverts the file to C0's bytes by hand, and
    Sigma then writes C2 from C0. Every one of those byte states is Sigma's, and a SET of them would
    vouch for HEAD=C1 and disk=C2 -- hiding the revert, which dropped goal 101. The chain is
    ordered and truncates at the pre-image, so HEAD is no longer behind the disk and it refuses."""
    with registry_root.drained(capfd):
        w = World(tmp_path).build()
        assert w.start("100").startswith("worktree")
        w.legacy_pick(100)
        older = w.read(SHARD)
        w.sigma_shard_write("101")
        w.commit_registry()
        (w.root / SHARD).write_bytes(older)
        w.sigma_shard_write("102")
        goals = json.loads(w.read(SHARD))["features"]["voice"]["repos"][registry_root.REPO]["goals"]
        assert goals == [100, 102], goals
        out = w.check()
        assert out is not None and out.startswith("REFUSED"), out


def test_the_refusal_names_each_path_and_its_commit_gesture_clears_it(tmp_path, capfd):
    """AC-2/AC-3: the refusal names the file it could not prove, prints §15's commit gesture, and
    that gesture -- run verbatim from the refusal's own text -- is enough to start again. It never
    advises git's shelving command."""
    with registry_root.drained(capfd):
        w = World(tmp_path).build()
        _sigma_dirt(w)
        page = w.root / PAGE
        page.write_bytes(page.read_bytes() + b"\nA human's note.\n")
        out = w.start("102")
        assert out.startswith("REFUSED"), out
        assert PAGE in out, out
        assert "stash" not in out.lower(), out
        gesture = re.search(r"`(git -C \S+ add \.sdlc/features && git -C \S+ commit -m [^`]+)`", out)
        assert gesture is not None, out
        proc = subprocess.run(gesture.group(1), shell=True, capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr or proc.stdout
        assert w.porcelain() == "", w.porcelain()
        again = w.start("102")
        assert again.startswith("worktree"), again


def test_the_dirty_root_code_never_uses_or_advises_git_stash(tmp_path, capfd):
    """AC-3 and #53: no `_dirty_root*` function in `work.py`, and nothing in the provenance module,
    names git's shelving command -- neither as a call nor as advice in a message."""
    with registry_root.drained(capfd):
        work = registry_root.module("work")
        names = sorted(n for n, v in vars(work).items()
                       if n.startswith("_dirty_root") and inspect.isfunction(v))
        assert "_dirty_root_refusal" in names, names
        sources = {n: inspect.getsource(getattr(work, n)) for n in names}
        provenance = registry_root.SCRIPTS / "feature_provenance.py"
        if provenance.is_file():
            sources[provenance.name] = provenance.read_text(encoding="utf-8")
        offenders = sorted(n for n, src in sources.items() if "stash" in src.lower())
        assert offenders == [], offenders


def test_no_extra_git_call_when_any_dirt_is_outside_the_registry(tmp_path, capfd):
    """The cost rule: dirt that is not all registry pays exactly today's two calls and refuses."""
    with registry_root.drained(capfd):
        w = World(tmp_path).layout()
        run = _fake({V1: "M .sdlc/features/units/voice.json\n M seed.txt", HEADQ: "main"})
        out = w.check(run)
        assert out is not None and out.startswith("REFUSED"), out
        assert run.calls == [V1, HEADQ], run.calls


def test_porcelain_v2_is_read_once_when_every_dirty_line_is_a_registry_file(tmp_path, capfd):
    """Registry-only dirt costs ONE more read, pathspec-limited to `.sdlc/features`, no matter how
    many paths are dirty."""
    with registry_root.drained(capfd):
        w = World(tmp_path).layout()
        v2 = "\n".join("1 .M N... 100644 100644 100644 %s %s %s" % (ZEROS, ZEROS, p)
                       for p in (SHARD, PAGE))
        run = _fake({V1: STRIPPED_REGISTRY_DIRT, HEADQ: "main", V2: v2})
        out = w.check(run)
        assert out is not None and out.startswith("REFUSED"), out
        assert run.calls == [V1, HEADQ, V2], run.calls


def test_a_first_status_line_that_lost_its_leading_space_still_parses(tmp_path, capfd):
    """BR-9: `work._run` strips, so line 1's ` M` arrives as `M `. Read with a fixed `[3:]` it
    would become `sdlc/features/...` -- not a registry path -- and the guard would refuse without
    ever looking."""
    with registry_root.drained(capfd):
        w = World(tmp_path).layout()
        run = _fake({V1: STRIPPED_REGISTRY_DIRT, HEADQ: "main", V2: ""})
        out = w.check(run)
        assert run.calls.count(V2) == 1, run.calls
        assert out is not None and SHARD in out, out


def test_a_failed_porcelain_v2_read_refuses(tmp_path, capfd):
    """The new read failing is today's refusal for this dirt, never a new way through."""
    with registry_root.drained(capfd):
        w = World(tmp_path).layout()
        run = _fake({V1: STRIPPED_REGISTRY_DIRT, HEADQ: "main",
                     V2: RuntimeError("fatal: index file corrupt")})
        out = w.check(run)
        assert out is not None and out.startswith("REFUSED"), out


def test_the_documented_cli_gesture_with_a_relative_sdlc_is_not_refused(tmp_path, capfd, monkeypatch):
    """AC-1 on the gesture the docs print (SKILL.md step 3a): `work.py start .sdlc <goal>` from
    the project root. `main` hands the RELATIVE `.sdlc` to every writer and to the checker, so a
    fix proven only with an absolute path would still refuse here."""
    with registry_root.drained(capfd) as out:
        w = World(tmp_path).build()
        rc, printed = w.cli("100", monkeypatch, out)
        assert rc == 0 and printed.startswith("worktree"), (rc, printed)
        w.legacy_pick(100)
        w.commit_registry()
        rc, printed = w.cli("101", monkeypatch, out)
        assert rc == 0 and printed.startswith("worktree"), (rc, printed)
        w.legacy_pick(101)
        lines = w.porcelain().splitlines()
        assert lines == [" M " + SHARD, " M " + PAGE], lines
        rc, printed = w.cli("102", monkeypatch, out)
        assert rc == 0, (rc, printed)
        assert "REFUSED" not in printed and printed.startswith("worktree"), printed
