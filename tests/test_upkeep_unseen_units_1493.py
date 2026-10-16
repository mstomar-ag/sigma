"""#1493: the sweep names the units it cannot see. A remote `feature/*` branch with no registry record is not
maintained; the job record and the doctor say so, also when the repository has no `.sdlc/features/` at all."""
import json
import pathlib
import subprocess
import sys

import test_upkeep_sched as T
import upkeep_support as S


def git(cwd, *argv):
    subprocess.run(["git", *argv], cwd=str(cwd), check=True, capture_output=True,
                   env={"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "HOME": str(cwd), "GIT_CONFIG_NOSYSTEM": "1",
                        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"})


def repo(tmp_path, branches=("feature/x",)):
    """-> (root, sdlc): a repo with a local bare `origin` carrying the given remote-tracking feature branches."""
    bare, root = tmp_path / "origin.git", tmp_path / "proj"
    git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    root.mkdir()
    git(root, "init", "-b", "main")
    (root / "f.txt").write_text("a\n")
    git(root, "add", "f.txt")
    git(root, "commit", "-m", "init")
    git(root, "remote", "add", "origin", str(bare))
    for b in branches:
        git(root, "branch", b)
    git(root, "push", "origin", "main", *branches)
    git(root, "fetch", "origin")
    sdlc = T.project(tmp_path)
    return root, sdlc


def run(tmp_path, sdlc, root, units=None):
    cmd = lambda _u: [sys.executable, "-c", "print('{\"outcome\": \"current\", \"unit_tip\": null}')"]     # noqa: E731
    kw = {} if units is None else {"list_units": lambda _s: units}
    out = T.job().run_sweep(T.open_config(), sdlc, str(root), "r1", 600, environ=T.ENV, command=cmd, now=2_000_000, **kw)
    return out, json.loads(pathlib.Path(sdlc, "state/upkeep/job.json").read_text())


def test_no_registry_remote_feature_branch_is_named_in_the_job_record(tmp_path):
    root, sdlc = repo(tmp_path)
    assert not pathlib.Path(sdlc, "features").exists()
    out, record = run(tmp_path, sdlc, root)
    assert out["outcome"] == "swept"
    assert record["sweep"]["units_listed"] == 0 and record["sweep"]["unregistered"] == ["x"], record
    assert record["sweep"]["counts"] == {} and record["sweep"]["unreached"] == [] and record["sweep"]["notes"] == []


def test_a_registered_unit_is_not_listed_as_unregistered(tmp_path):
    root, sdlc = repo(tmp_path, ("feature/x", "feature/y"))
    _, record = run(tmp_path, sdlc, root, units=["x"])
    assert record["sweep"]["unregistered"] == ["y"] and record["sweep"]["units_listed"] == 1


def test_an_unreadable_registry_record_appears_as_a_note(tmp_path):
    root, sdlc = repo(tmp_path)
    units = pathlib.Path(sdlc, "features/units")
    units.mkdir(parents=True)
    (units / "bad.json").write_text("{")
    _, record = run(tmp_path, sdlc, root)
    assert len(record["sweep"]["notes"]) == 1 and "bad.json" in record["sweep"]["notes"][0], record


def test_a_git_failure_gives_no_unregistered_and_no_exception(tmp_path):
    sdlc = T.project(tmp_path)                                           # not a git repository
    assert T.sched().unregistered_heads(sdlc, "origin", str(tmp_path)) == []
    assert T.sched().unregistered_heads(sdlc, "--bad", str(tmp_path)) == []


def test_the_doctor_row_names_the_unmaintained_branch_and_a_clean_repo_passes(tmp_path):
    root, sdlc = repo(tmp_path)
    doctor = T._doctor()
    rows = doctor._upkeep_rows(pathlib.Path(sdlc), T.open_config(), lambda argv: "", lambda _n: "/bin/git", True, True)
    row = [r for r in rows if r["name"].startswith("upkeep units:")]
    assert len(row) == 1 and not row[0]["ok"] and "feature/x" in row[0]["name"] and "0 registered" in row[0]["name"]
    git(root, "push", "origin", "--delete", "feature/x")
    git(root, "fetch", "--prune", "origin")
    rows = doctor._upkeep_rows(pathlib.Path(sdlc), T.open_config(), lambda argv: "", lambda _n: "/bin/git", True, True)
    assert [r["ok"] for r in rows if r["name"].startswith("upkeep units:")] == [True]


def test_a_closed_gate_adds_no_doctor_row(tmp_path):
    root, sdlc = repo(tmp_path)
    doctor = T._doctor()
    assert doctor._upkeep_rows(pathlib.Path(sdlc), S.template_cfg(), lambda argv: "", lambda _n: "/bin/git", True, True) == []


# ---- #1535 follow-ups

def _note_ref(units_run):
    seen = []
    record = {"unit": "window", "outcome": "swept", "sweep_job": True,
              "sweep": {"units_listed": 3, "counts": {"rebased": units_run} if units_run else {}}}
    ok = T.sched().write_outcome_note(T.open_config(), "/nonexistent", record, append=lambda *a, **k: seen.append(k) or {"ok": 1})
    assert ok
    return seen[0]["ref"]


def test_the_sweep_note_carries_the_number_of_units_swept(tmp_path):
    assert _note_ref(0).endswith(":units=0")
    assert _note_ref(2).endswith(":units=2")
    assert _note_ref(0) != _note_ref(2)


def test_a_unit_note_has_no_units_suffix():
    seen = []
    T.sched().write_outcome_note(T.open_config(), "/nonexistent", {"unit": "x", "outcome": "rebased", "unit_tip": "a" * 12},
                                 append=lambda *a, **k: seen.append(k) or {"ok": 1})
    assert "units=" not in seen[0]["ref"]


def test_a_unit_whose_name_differs_from_its_branch_only_in_case_is_registered(tmp_path):
    root, sdlc = repo(tmp_path, ("feature/foo", "feature/BAR"))
    _, record = run(tmp_path, sdlc, root, units=["Foo", "bar"])
    assert record["sweep"]["unregistered"] == [] and record["sweep"]["units_listed"] == 2, record


def test_the_doctor_remedy_names_the_exclude_list_and_an_excluded_branch_is_not_counted(tmp_path):
    root, sdlc = repo(tmp_path)
    doctor = T._doctor()
    args = (lambda argv: "", lambda _n: "/bin/git", True, True)
    row = [r for r in doctor._upkeep_rows(pathlib.Path(sdlc), T.open_config(), *args) if r["name"].startswith("upkeep units:")][0]
    assert "upkeep.units.exclude" in row["fix"], row
    cfg = T.open_config()
    cfg["upkeep"]["units"] = {"exclude": ["x"]}
    row = [r for r in doctor._upkeep_rows(pathlib.Path(sdlc), cfg, *args) if r["name"].startswith("upkeep units:")][0]
    assert row["ok"] and "0 remote feature" in row["name"], row
