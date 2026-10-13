"""#1338: `loop.py verify` must refuse BEFORE running the proving command when `verify.enforce`
is on and the goal has no acceptance record -- the same gate `work.py pr` applies. Otherwise the
evidence is stamped against a missing record, and recording one afterwards changes
`acceptance_sha256`, so the evidence goes stale and the whole verify must be re-run."""
import importlib.util, json, pathlib, subprocess

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


loop = _load("loop")
state = _load("state")


def _setup(tmp_path, enforce, with_record, work=True):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "--local",
                    "sigma.allowRepositoryShellCommands", "true"], check=True)
    sdlc = tmp_path / ".sdlc"
    (sdlc / "goals").mkdir(parents=True); (sdlc / "state").mkdir()
    marker = tmp_path / "ran.txt"
    cfg = {"verify": {"command": f"echo x >> {marker}", "enforce": enforce}, "work": {"enabled": work}}
    (sdlc / "config.json").write_text(json.dumps(cfg))
    (sdlc / "goals" / "0001.md").write_text("---\nid: 0001\nstatus: pending\n---\nx\n")
    if with_record:
        (sdlc / "acceptance").mkdir()
        (sdlc / "acceptance" / "0001.md").write_text(
            '---\nkind: "acceptance"\ngoal: "0001"\nverify_command: null\n---\n'
            "## Done when\n- [ ] it works\n- [ ] it is tested\n- [ ] it is documented\n")
    return str(sdlc), "0001", marker


def test_enforce_on_without_record_refuses_before_running_command(tmp_path, capsys):
    sdlc, goal, marker = _setup(tmp_path, True, False)
    assert loop.verify_goal(sdlc, goal) == 4
    assert not marker.exists()
    assert "acceptance.py record" in capsys.readouterr().err


def test_enforce_on_with_record_runs_once_and_digest_unchanged(tmp_path):
    sdlc, goal, marker = _setup(tmp_path, True, True)
    before = state.acceptance_module().digest(sdlc, goal)
    assert loop.verify_goal(sdlc, goal) == 0
    assert marker.read_text().count("x") == 1
    assert state.acceptance_module().digest(sdlc, goal) == before is not None


def test_enforce_off_without_record_is_unchanged(tmp_path):
    sdlc, goal, marker = _setup(tmp_path, False, False)
    assert loop.verify_goal(sdlc, goal) == 0
    assert marker.exists()


def test_enforce_on_work_off_without_record_is_unchanged(tmp_path):
    sdlc, goal, marker = _setup(tmp_path, True, False, work=False)
    assert loop.verify_goal(sdlc, goal) == 0
    assert marker.exists()
