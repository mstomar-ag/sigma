"""Goal 1477: imported records name their origin, plan/apply count per folder, an optional cwd
filter, and `remove` backs out one folder. Driven through the CLI on a temp git repo."""
import importlib.util
import json
import os
import pathlib
import re
import stat
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills/sigma-loop/scripts/decision_import.py"
spec = importlib.util.spec_from_file_location("decision_import_t1477", SCRIPT)
DI = importlib.util.module_from_spec(spec)
spec.loader.exec_module(DI)

OTHER, THIRD = "-srv-other-work", "-srv-third-work"


def _cfg(visibility="private", **imp):
    return {"rulebook": {"records": {"enabled": True, "repo_visibility": visibility},
                         "autonomy": {"enabled": True, "levels": {"l1_min_answers": 1}},
                         "import": imp}}


def _ask(tid, q, cwd, ts="2026-10-01T10:00:00Z"):
    return json.dumps({"type": "assistant", "timestamp": ts, "cwd": cwd, "message": {"content": [
        {"type": "tool_use", "id": tid, "name": "AskUserQuestion", "input": {"questions": [
            {"question": q, "options": [{"label": "merge"}, {"label": "wait"}]}]}}]}})


def _ans(tid, q, a="merge"):
    return json.dumps({"type": "user", "timestamp": "2026-10-01T10:01:00Z", "message": {"content": [
        {"type": "tool_result", "tool_use_id": tid,
         "content": 'User has answered your questions: "%s"="%s". You can now continue.' % (q, a)}]}})


def _world(tmp_path, cfg=None):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    sd = repo / ".sdlc"
    sd.mkdir()
    (sd / "config.json").write_text(json.dumps(cfg or _cfg()))
    prof = tmp_path / "prof"
    own = prof / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(repo.resolve()))
    for folder, tag, cwd in ((own, "own", str(repo.resolve())), (prof / "projects" / OTHER, "oth", "/srv/other/work"),
                             (prof / "projects" / THIRD, "thr", str(repo.resolve()) + "/sub")):
        folder.mkdir(parents=True)
        q = "should we merge change %s" % tag
        (folder / "s1.jsonl").write_text("\n".join([_ask("t" + tag, q, cwd), _ans("t" + tag, q)]) + "\n")
    return repo, sd, prof


def _fake_gh(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    gh = d / "gh"
    gh.write_text("#!/bin/sh\necho 'fake gh: no network in this test' >&2\nexit 1\n")
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    return d


def _cli(tmp_path, repo, *args):
    bindir = tmp_path / "bin"
    if not bindir.exists():
        _fake_gh(tmp_path)
    env = {"PATH": "%s:/usr/bin:/bin" % bindir, "HOME": str(tmp_path)}
    r = subprocess.run([sys.executable, str(SCRIPT), *args], cwd=repo, env=env, capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def _records(sd):
    out = []
    for p in sorted((sd / "decisions").glob("*.json")):
        out.append(json.loads(p.read_text()))
    return out


def _watch(sd):
    return [json.loads(p.read_text()) for p in sorted((sd / "autonomy").glob("watch-*.json"))] \
        if (sd / "autonomy").is_dir() else []


def _apply_two(tmp_path, cfg=None):
    repo, sd, prof = _world(tmp_path, cfg)
    rc, out, err = _cli(tmp_path, repo, "apply", str(sd), "--profile", str(prof),
                        "--project-dir", OTHER, "--project-dir", THIRD)
    assert rc == 0, err
    return repo, sd, prof, out


def test_records_and_watch_files_name_their_origin(tmp_path):
    repo, sd, prof, _ = _apply_two(tmp_path)
    recs = _records(sd)
    assert len(recs) == 3
    by = {r["origin"]["folder"]: r["origin"] for r in recs}
    assert set(by) == {re.sub(r"[^A-Za-z0-9]", "-", str(repo.resolve())), OTHER, THIRD}
    o = by[OTHER]
    assert o == {"kind": "transcript", "profile": "prof", "folder": OTHER, "file": "s1.jsonl",
                 "line": 1, "cwd": "/srv/other/work"}
    marked = [w for w in _watch(sd) if w.get("how") == "imported"]
    assert marked and all(isinstance(w.get("origin"), dict) and w["origin"]["folder"] for w in marked)


def test_comment_candidates_carry_issue_and_comment_id(tmp_path):
    repo, sd, prof = _world(tmp_path, _cfg(comments=True))
    bs = DI._load("blocker_scan")
    body = "%s\n_should we merge x_\n- **a** — merge\n%s" % (bs.UNPARK_QA_START, bs.UNPARK_QA_END)
    out = DI.scan(str(sd), json.loads((sd / "config.json").read_text()), [str(prof)], issues=[5],
                  comments_fn=lambda c, i: [{"id": 77, "body": body, "created_at": "2026-10-01T10:00:00Z"}])
    com = [c for c in out["candidates"] if c["source"] != "transcript"]
    assert com and com[0]["origin"] == {"kind": "comment", "issue": 5, "comment_id": "77"}


def test_public_repository_stores_hashes_only(tmp_path):
    repo, sd, prof, _ = _apply_two(tmp_path, _cfg("public"))
    recs = _records(sd)
    assert recs
    blob = json.dumps(recs)
    for raw in (OTHER, "/srv/other/work", '"prof"', str(repo.resolve())):
        assert raw not in blob
    o = [r for r in recs if r["origin"]["folder"] == DI._fid(OTHER)][0]["origin"]
    assert o["profile"] == DI._fid("prof") and o["cwd"] == DI._fid("/srv/other/work")


def test_plan_and_apply_print_per_folder_counts_and_flag_extras(tmp_path):
    repo, sd, prof = _world(tmp_path)
    args = ("--profile", str(prof), "--project-dir", OTHER)
    rc, plan_out, _e = _cli(tmp_path, repo, "plan", str(sd), *args)
    assert rc == 0 and "folder %s (extra): 1 candidates, 1 new records" % OTHER in plan_out
    own = re.sub(r"[^A-Za-z0-9]", "-", str(repo.resolve()))
    assert "folder %s: 1 candidates, 1 new records" % own in plan_out
    rc, apply_out, _e = _cli(tmp_path, repo, "apply", str(sd), *args)
    assert "folder %s (extra): 1 candidates, 1 new records" % OTHER in apply_out
    rc, again, _e = _cli(tmp_path, repo, "apply", str(sd), *args)
    assert "folder %s (extra): 1 candidates, 0 new records" % OTHER in again


def test_filter_keeps_only_cwd_inside_the_repo_and_reports_dropped(tmp_path):
    repo, sd, prof = _world(tmp_path)
    base = ("plan", str(sd), "--profile", str(prof), "--project-dir", OTHER, "--project-dir", THIRD)
    rc, out, _e = _cli(tmp_path, repo, *base, "--only-in-repo")
    assert "would write 2 records" in out and "dropped 1 answers whose cwd is outside this repository" in out
    (sd / "config.json").write_text(json.dumps(_cfg(require_repo_cwd=True)))
    rc, out2, _e = _cli(tmp_path, repo, *base)
    assert "would write 2 records" in out2 and "dropped 1" in out2


def test_filter_off_output_is_unchanged(tmp_path):
    repo, sd, prof = _world(tmp_path)
    cfg = json.loads((sd / "config.json").read_text())
    out = DI.scan(str(sd), cfg, [str(prof)], project_dirs=[OTHER, THIRD])
    off = DI.scan(str(sd), cfg, [str(prof)], project_dirs=[OTHER, THIRD], only_in_repo=False)
    assert "dropped" not in out and out == off and len(out["candidates"]) == 3
    rc, text, _e = _cli(tmp_path, repo, "plan", str(sd), "--profile", str(prof), "--project-dir", OTHER)
    assert "dropped" not in text and "would write 2 records (0 unknown); plan writes nothing" in text


def test_remove_dry_run_then_apply_deletes_only_the_first_folder(tmp_path):
    repo, sd, prof, _ = _apply_two(tmp_path)
    nrec, nwatch = len(_records(sd)), len(_watch(sd))
    rc, dry, _e = _cli(tmp_path, repo, "remove", str(sd), "--project-dir", OTHER)
    assert rc == 0 and "would remove 1 records" in dry and "dry run" in dry
    assert len(_records(sd)) == nrec and len(_watch(sd)) == nwatch
    rc, done, _e = _cli(tmp_path, repo, "remove", str(sd), "--project-dir", OTHER, "--apply")
    assert rc == 0 and "removed 1 records" in done
    left = _records(sd)
    assert len(left) == nrec - 1 and OTHER not in {r["origin"]["folder"] for r in left}
    assert all(w["origin"]["folder"] != OTHER for w in _watch(sd) if w.get("how") == "imported")
    assert any(r["origin"]["folder"] == THIRD for r in left)


def test_remove_profile_filter_and_public_hash_match(tmp_path):
    repo, sd, prof, _ = _apply_two(tmp_path, _cfg("public"))
    rc, out, _e = _cli(tmp_path, repo, "remove", str(sd), "--project-dir", OTHER, "--profile", "nope")
    assert "would remove 0 records" in out
    rc, out, _e = _cli(tmp_path, repo, "remove", str(sd), "--project-dir", OTHER, "--profile", str(prof), "--apply")
    assert "removed 1 records" in out


def test_remove_never_touches_records_without_origin(tmp_path):
    repo, sd, prof, _ = _apply_two(tmp_path)
    for p in (sd / "decisions").glob("*.json"):
        d = json.loads(p.read_text())
        if d["origin"]["folder"] == OTHER:
            d.pop("origin")
            p.write_text(json.dumps(d))
    rc, out, _e = _cli(tmp_path, repo, "remove", str(sd), "--project-dir", OTHER, "--apply")
    assert "removed 0 records" in out and "1 legacy records have no origin; not removed" in out
    assert len(_records(sd)) == 3


def test_remove_requires_a_project_dir(tmp_path):
    repo, sd, prof = _world(tmp_path)
    rc, _o, err = _cli(tmp_path, repo, "remove", str(sd))
    assert rc != 0 and "project-dir" in err


def test_record_build_preserves_origin():
    rd = DI._load("decision_record")
    assert rd.build("import", 1, "unknown", "c", "", "imported", origin={"kind": "comment"})["origin"] == {"kind": "comment"}
    assert "origin" not in rd.build("import", 1, "unknown", "c", "", "imported")


def test_import_limits_default_and_wrong_type():
    cm = DI._load("rulebook_cfg")
    assert cm.import_limits({})["require_repo_cwd"] is False
    assert cm.import_limits({"rulebook": {"import": {"require_repo_cwd": "yes"}}})["require_repo_cwd"] is False
    assert cm.import_limits({"rulebook": {"import": {"require_repo_cwd": True}}})["require_repo_cwd"] is True


def test_remove_leaves_watch_files_without_origin(tmp_path):
    repo, sd, prof, _ = _apply_two(tmp_path)
    stripped = 0
    for p in (sd / "autonomy").glob("watch-*.json"):
        d = json.loads(p.read_text())
        if d.get("how") == "imported" and d["origin"]["folder"] == OTHER:
            d.pop("origin")
            p.write_text(json.dumps(d))
            stripped += 1
    assert stripped
    before = len(_watch(sd))
    rc, out, _e = _cli(tmp_path, repo, "remove", str(sd), "--project-dir", OTHER, "--apply")
    assert len(_watch(sd)) == before and "legacy comparisons" in out
