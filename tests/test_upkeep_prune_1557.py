"""#1557: the upkeep sweep prunes old backup refs by itself (keep_days, newest keep_last per unit), once per sweep,
bounded and idempotent; a failed prune never fails the sweep and is retried by the next one; the doctor shows it."""
import json
import pathlib
import subprocess
import sys

import test_upkeep_sched as T
import test_upkeep_unseen_units_1493 as U

OLD = ["2020010%dT000000Z" % d for d in range(1, 9)]       # eight stamps, years older than keep_days
YOUNG = "29990101T000000Z"                                   # later than now: kept, takes no slot


def cmd(_u):
    return [sys.executable, "-c", "print('{\"outcome\": \"current\", \"unit_tip\": null}')"]


def bare_refs(tmp_path):
    out = subprocess.run(["git", "for-each-ref", "--format=%(refname)", "refs/sigma/backup/"],
                         cwd=str(tmp_path / "origin.git"), capture_output=True, text=True, check=True).stdout
    return sorted(out.split())


def seeded(tmp_path, stamps=OLD):
    root, sdlc = U.repo(tmp_path)
    for s in stamps:
        U.git(root, "push", "origin", "HEAD:refs/sigma/backup/u/" + s)
    return root, sdlc


def sweep(sdlc, root, **kw):
    return T.job().run_sweep(T.open_config(), sdlc, str(root), "r1", 600, environ=T.ENV, command=cmd,
                             list_units=lambda _s: [], now=2_000_000, **kw)


def prune_doc(sdlc):
    return json.loads(pathlib.Path(sdlc, "state/upkeep/prune.json").read_text())


def test_old_refs_are_pruned_and_the_newest_keep_last_stay(tmp_path):
    root, sdlc = seeded(tmp_path)
    out = sweep(sdlc, root)
    assert out["outcome"] == "swept" and out["sweep"]["prune"]["outcome"] == "pruned", out
    assert bare_refs(tmp_path) == ["refs/sigma/backup/u/" + s for s in OLD[-5:]]
    doc = prune_doc(sdlc)
    assert doc["outcome"] == "pruned" and doc["deleted"] == 3 and doc["listed"] == 8


def test_a_young_ref_is_kept(tmp_path):
    root, sdlc = seeded(tmp_path, OLD + [YOUNG])
    sweep(sdlc, root)
    assert "refs/sigma/backup/u/" + YOUNG in bare_refs(tmp_path)


def test_a_failed_delete_leaves_the_sweep_swept_and_the_next_sweep_retries(tmp_path):
    root, sdlc = seeded(tmp_path)
    B = T.job()._sibling("feature_backup")

    def failing(cwd, argv, *a, **k):
        if "--delete" in argv or any(str(x).startswith(":") for x in argv):
            raise RuntimeError("refused")
        return B.sync._run(cwd, argv, *a, **k)
    out = sweep(sdlc, root, prune_run=failing)
    assert out["outcome"] == "swept" and out["sweep"]["prune"]["outcome"] != "pruned", out
    assert prune_doc(sdlc)["outcome"] != "pruned" and len(bare_refs(tmp_path)) == 8
    sweep(sdlc, root)
    assert len(bare_refs(tmp_path)) == 5 and prune_doc(sdlc)["outcome"] == "pruned"


def test_a_raising_prune_does_not_touch_the_sweep(tmp_path):
    root, sdlc = seeded(tmp_path)

    def boom(*a, **k):
        raise OSError("boom")
    out = sweep(sdlc, root, prune=boom)
    assert out["outcome"] == "swept" and out["sweep"]["prune"]["outcome"] == "failed"
    assert prune_doc(sdlc)["outcome"] == "failed" and len(bare_refs(tmp_path)) == 8


def test_gate_closed_means_no_prune_and_no_file(tmp_path):
    root, sdlc = seeded(tmp_path)
    called = []
    T.job().run_sweep({}, sdlc, str(root), "r1", 600, environ=T.ENV, command=cmd, list_units=lambda _s: [],
                      now=2_000_000, prune=lambda *a, **k: called.append(1))
    assert not called and not pathlib.Path(sdlc, "state/upkeep/prune.json").exists()
    assert len(bare_refs(tmp_path)) == 8


def test_prune_runs_once_per_sweep_and_not_after_a_lost_counter(tmp_path):
    root, sdlc = seeded(tmp_path)
    calls = []
    sweep(sdlc, root, prune=lambda *a, **k: calls.append(1) or {"outcome": "nothing", "listed": 0, "deleted": 0})
    assert calls == [1]


def rows(doctor, sdlc, now):
    got = doctor._upkeep_rows(pathlib.Path(sdlc), T.open_config(), lambda argv: "", lambda _n: "/bin/git", True, True, now)
    return [r for r in got if r["name"].startswith("upkeep backups")]


def test_doctor_row_shows_count_last_prune_and_never(tmp_path):
    root, sdlc = seeded(tmp_path)
    doctor = T._doctor()
    r = rows(doctor, sdlc, 1000)
    assert len(r) == 1 and "never" in r[0]["name"] and r[0]["ok"]
    sweep(sdlc, root)
    r = rows(doctor, sdlc, 2_000_000)
    assert len(r) == 1 and "8 listed" in r[0]["name"] and "pruned" in r[0]["name"] and r[0]["ok"], r
    pathlib.Path(sdlc, "state/upkeep/prune.json").write_text(json.dumps({"outcome": "failed", "listed": 2, "at": 5}))
    r = rows(doctor, sdlc, 10)
    assert r[0]["ok"] is False and r[0]["fix"]


def test_a_spent_cap_skips_the_prune_1632(tmp_path):
    root, sdlc = seeded(tmp_path)
    calls = []
    ticks = iter([0, 10 ** 6, 10 ** 6, 10 ** 6])
    out = T.job().run_sweep(T.open_config(), sdlc, str(root), "r1", 600, environ=T.ENV, command=cmd,
                            list_units=lambda _s: [], now=2_000_000, beat=lambda: next(ticks),
                            prune=lambda *a, **k: calls.append(1) or {"outcome": "pruned"})
    assert out["outcome"] == "swept" and not calls and out["sweep"]["prune"]["outcome"] == "skipped", out
    assert not pathlib.Path(sdlc, "state/upkeep/prune.json").exists() and len(bare_refs(tmp_path)) == 8


def test_the_beat_is_touched_during_the_prune_1632(tmp_path):
    import os
    root, sdlc = seeded(tmp_path)
    B = T.job()._sibling("feature_backup")
    beat = pathlib.Path(sdlc, T.job().sched.BEAT_REL)
    seen = []

    def watching(cwd, argv, *a, **k):
        seen.append(os.path.getmtime(beat))
        os.utime(beat, (1, 1))
        return B.sync._run(cwd, argv, *a, **k)
    out = sweep(sdlc, root, prune_run=watching)
    assert out["sweep"]["prune"]["outcome"] == "pruned" and len(seen) >= 2, seen
    assert all(m > 1 for m in seen), seen
