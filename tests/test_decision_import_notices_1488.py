"""#1488: a project folder missing under every profile is `skipped`, plan/apply print scan notices, apply flags a partial import."""
import importlib.util
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("decision_import_t1488", ROOT / "skills/sigma-loop/scripts/decision_import.py")
DI = importlib.util.module_from_spec(spec)
spec.loader.exec_module(DI)

X, Y = "-srv-demo-work-x", "-srv-demo-work-y"
CFG = {"rulebook": {"records": {"enabled": True, "repo_visibility": "private"}, "autonomy": {"enabled": False},
                    "import": {"comments": False}}}


def _world(tmp_path, cfg=CFG):
    sd = tmp_path / "proj" / ".sdlc"
    sd.mkdir(parents=True)
    (sd / "config.json").write_text(json.dumps(cfg))
    a, b = tmp_path / "prof_a", tmp_path / "prof_b"
    for prof, names in ((a, [Y]), (b, [X, Y])):
        for n in names:
            (prof / "projects" / n).mkdir(parents=True)
            (prof / "projects" / n / "t.jsonl").write_text("")
    return str(sd), str(a), str(b)


def _run(capsys, verb, sd, *rest):
    rc = DI.main([verb, sd, *rest])
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def _args(a, *more):
    return ["--profile", a, "--project-dir", X, "--project-dir", Y, *more]


def test_missing_folder_is_skipped_in_all_three_verbs(tmp_path, capsys):
    sd, a, _ = _world(tmp_path)
    for verb in ("scan", "plan", "apply"):
        rc, out, err = _run(capsys, verb, sd, *_args(a))
        text = out + err
        lines = [ln for ln in text.splitlines() if X in ln and "not found" in ln]
        assert rc == 0 and len(lines) == 1, verb
        assert lines[0].startswith("skipped: ") and "the other folders were read" in lines[0] and a in lines[0]
        assert "refused" not in text, verb
        if verb == "scan":
            assert "transcript: files_read=1" in out  # Y was read


def test_plan_and_apply_notice_goes_to_stderr(tmp_path, capsys):
    sd, a, _ = _world(tmp_path)
    for verb in ("plan", "apply"):
        _, out, err = _run(capsys, verb, sd, *_args(a))
        assert "skipped: project folder" in err and "skipped:" not in out


def test_folder_found_under_any_profile_gives_no_notice(tmp_path, capsys):
    sd, a, b = _world(tmp_path)
    for verb in ("scan", "plan", "apply"):
        rc, out, err = _run(capsys, verb, sd, "--profile", a, "--profile", b, "--project-dir", X, "--project-dir", Y)
        assert rc == 0 and "skipped:" not in out + err and "not found" not in out + err


def test_unusable_reference_still_says_refused(tmp_path, capsys):
    sd, a, _ = _world(tmp_path)
    out = DI.scan(sd, CFG, [a], project_dirs=[".."])
    assert any(r.startswith("project folder") and "refused" in r for r in out["refusals"])
    _, text, _ = _run(capsys, "scan", sd, "--profile", a, "--project-dir", "..")
    assert "refused: project folder reference '..' refused" in text and "skipped:" not in text


def test_no_notices_leaves_output_clean(tmp_path, capsys):
    sd, a, b = _world(tmp_path)
    for verb in ("scan", "plan", "apply"):
        _, out, err = _run(capsys, verb, sd, "--profile", b, "--project-dir", Y)
        assert err == "" and "partial import" not in out and "refused" not in out and "skipped:" not in out


def test_apply_flags_a_partial_import(tmp_path, capsys, monkeypatch):
    sd, a, b = _world(tmp_path)
    real = DI.scan
    ticks = iter([0.0] + [1000.0] * 50)
    monkeypatch.setattr(DI, "scan", lambda *p, **k: real(*p, clock=lambda: next(ticks), **k))
    rc, out, err = _run(capsys, "apply", sd, "--profile", b, "--project-dir", Y)
    assert rc == 0
    assert "partial import: 1 files not read, raise rulebook.import.time_budget_s and run apply again" in out
    assert "time budget reached" in err


def test_cli_scenario_with_fake_gh(tmp_path, capsys, monkeypatch):
    sd, a, _ = _world(tmp_path, dict(CFG, rulebook=dict(CFG["rulebook"], **{"import": {"comments": True}})))
    rc, out, err = _run_with_lister(capsys, sd, a)
    assert rc == 0 and "skipped: project folder" in err and "refused" not in out + err


def _run_with_lister(capsys, sd, a):
    rc = DI.main(["plan", sd, *_args(a)], lister=lambda config, limits: [], comments_fn=lambda c, i: [])
    cap = capsys.readouterr()
    return rc, cap.out, cap.err
