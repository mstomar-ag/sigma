"""#1505: the file-time duplicate search must not fold a follow-up into an issue of another unit."""
import importlib.util
import json
import pathlib

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


handoff = _mod("handoff")
ON = {"ledger": {"enabled": True, "actor": "amy"}}


class UnitSource:
    """Records what would be sent; each issue number maps to a body declaring its unit."""

    def __init__(self, units, fail_on=()):
        self.units = units
        self.fail_on = set(fail_on)
        self.reads = []
        self.created = None
        self.notes = []

    def fetch_body_labels(self, ref):
        self.reads.append(str(ref))
        if str(ref) in self.fail_on:
            raise RuntimeError("transport blip")
        unit = self.units.get(str(ref))
        return {"body": "Feature: %s\nBranch: feature/%s" % (unit, unit) if unit else "",
                "labels": []}

    def issue_url(self, goal):
        return "https://example.invalid/issues/%s" % goal

    def create_dependency(self, title, body, assignee, labels=(), goal_label=True):
        self.created = {"title": title, "body": body}
        return "61"

    def note(self, goal, text):
        self.notes.append((goal, text))

    def append_to_body(self, goal, marker):
        pass

    def mark_blocked(self, goal):
        pass

    def attach_label(self, *a, **k):
        return True


def _project(tmp_path):
    sdlc = tmp_path / ".sdlc"
    (sdlc / "state").mkdir(parents=True)
    (sdlc / "config.json").write_text(json.dumps(ON))
    return sdlc


def _pack(monkeypatch):
    monkeypatch.setattr(handoff, "_duplicate_search", lambda *a, **k: {
        "schema": "brainstorm-dedup/v1", "degraded": [],
        "candidates": [{"ref": "77", "score": 0.91, "state": "open", "strength": "duplicate",
                        "source": "mirror", "evidence": ["retry"]}]})


def _file(tmp_path, src, goal="9"):
    return handoff.create_tracked_issue(
        _project(tmp_path), ON, goal, "engine", "pooled connection released twice",
        same_area=True, immediately_actionable=True, blocks_goal=False, source=src)


def test_cross_unit_duplicate_is_filed_and_named_as_related(tmp_path, monkeypatch):
    _pack(monkeypatch)
    src = UnitSource({"9": "alpha", "77": "beta"})
    report = _file(tmp_path, src)
    assert report["duplicate_of"] is None and report["issue"] == "61"
    assert src.created is not None and "Related: #77 (other unit)" in src.created["body"]
    assert not any(goal == "77" for goal, _ in src.notes)


def test_same_unit_duplicate_still_becomes_a_comment(tmp_path, monkeypatch):
    _pack(monkeypatch)
    src = UnitSource({"9": "alpha", "77": "alpha"})
    report = _file(tmp_path, src)
    assert report["duplicate_of"] == "77" and src.created is None
    assert any(goal == "77" for goal, _ in src.notes)


def test_no_filing_unit_is_unchanged_and_reads_no_candidate(tmp_path, monkeypatch):
    _pack(monkeypatch)
    src = UnitSource({"77": "beta"})
    report = _file(tmp_path, src)
    assert report["duplicate_of"] == "77" and src.created is None
    assert "77" not in src.reads


def test_candidate_read_failure_keeps_todays_behaviour(tmp_path, monkeypatch):
    _pack(monkeypatch)
    src = UnitSource({"9": "alpha", "77": "beta"}, fail_on=["77"])
    report = _file(tmp_path, src)
    assert report["duplicate_of"] == "77" and src.created is None


def test_cli_track_cross_unit_duplicate_files_a_new_issue(tmp_path, monkeypatch):
    _pack(monkeypatch)
    sdlc = _project(tmp_path)
    src = UnitSource({"9": "alpha", "77": "beta"})
    monkeypatch.setattr(handoff.sources, "get_source", lambda *a, **k: src)
    assert handoff.main(["handoff.py", "track", str(sdlc), "9", "--area", "engine",
                         "--why", "pooled connection released twice", "--queue", "actionable",
                         "--assignee", "same-area", "--blocks", "no"]) == 0
    assert src.created is not None and "Related: #77" in src.created["body"]
