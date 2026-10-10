"""#950: the independent reviewer route, run only against a FAKE `claude` written into tmp_path. No real model, no network.

The route stays verified false: nothing here ran the real command line. The controls (each run once with the guarded line
broken, seen red, then restored) are the tests named in the module-level CONTROLS tuple. The gesture is the documented
one: `python -m pytest tests/test_upkeep_review.py -q`.
"""
import importlib.util
import json
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"
PATH = SCRIPTS / "feature_upkeep_review.py"
RESOLVER_MODEL = "test-model-id-1"
REVIEWER_MODEL = "test-model-id-2"
CAP, RESOLVER_CAP = 1.0, 2.0
CONTROLS = ("test_tree_changed_rejects_the_verdict", "test_validator_table", "test_inline_and_subagent_are_refused",
            "test_leak_gate_blocks_a_scratch_path", "test_closed_gate_has_no_effect")

FAKE = '''#!%(python)s
import json, os, pathlib, sys, subprocess, time
here = pathlib.Path(%(dir)r)
mode = json.loads((here / "mode.json").read_text())
stdin = sys.stdin.read()
(here / "marker.json").write_text(json.dumps({"argv": sys.argv[1:], "stdin": stdin, "cwd": os.getcwd()}))
d = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / "x"
d.mkdir(parents=True)
usage = {"input_tokens": 100000, "output_tokens": 0, "cache_read_input_tokens": 0,
         "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0}}
line = {"type": "assistant", "timestamp": "2026-09-01T00:00:00Z",
        "message": {"role": "assistant", "model": "claude-opus-5", "id": "m1", "usage": usage}}
(d / "s.jsonl").write_text(json.dumps(line) + "\\n")
if mode.get("sleep"):
    time.sleep(120)
print(mode.get("reply", ""))
'''

GOOD = json.dumps({"verdict": "approve", "reasons": ["keeps both sides"]})


def load(edit=None):
    src = PATH.read_text(encoding="utf-8")
    if edit:
        old, new = edit
        assert old in src, "mutation target has drifted out of the source: %r" % (old,)
        src = src.replace(old, new)
    spec = importlib.util.spec_from_file_location("feature_upkeep_review_t", PATH)
    module = importlib.util.module_from_spec(spec)
    module.__dict__["__file__"] = str(PATH)
    exec(compile(src, str(PATH), "exec"), module.__dict__)          # noqa: S102 - test-only
    return module


class Rig:
    def __init__(self, tmp, edit=None):
        self.tmp = tmp
        self.fakedir = tmp / "fake"
        self.fakedir.mkdir()
        self.binary = self.fakedir / "claude"
        self.binary.write_text(FAKE % {"python": sys.executable, "dir": str(self.fakedir)})
        self.binary.chmod(0o755)
        for name in ("sdlc", "state", "scratch", "repo", "home", "work"):
            (tmp / name).mkdir()
        self.tree = {"tree": "t1", "paths": {"a.txt": None}}
        self.mode({"reply": GOOD})
        self.mod = load(edit)

    def mode(self, mapping):
        (self.fakedir / "mode.json").write_text(json.dumps(mapping))

    def marker(self):
        path = self.fakedir / "marker.json"
        return json.loads(path.read_text()) if path.exists() else None

    def files(self):
        return [("a.txt", "base text", "ours text", "theirs text", "resolved text")]

    def request(self, **over):
        mod = self.mod
        config = {"upkeep": {"enabled": True, "conflicts": {"resolve": "agent"}}, "ledger": {"enabled": True}}
        files = self.files()
        launch = dict(sdlc_dir=str(self.tmp / "sdlc"), state_dir=str(self.tmp / "state"), repo_root=str(self.tmp / "repo"),
                      directory=str(self.tmp / "work"), binary=str(self.binary), flags=(), credential_var=None,
                      scratch_parent=str(self.tmp / "scratch"), rates=None, ceiling_machine_usd=10.0,
                      ceiling_team_usd=10.0, budget_seconds=5.0, home=str(self.tmp / "home"), timeout=20)
        fields = dict(config=config, unit="voice", base_sha="b" * 40, head_sha="h" * 40, tree_sha="t1", files=files,
                      pr_descriptions=["PR one"], reread=lambda: ("t1", {"a.txt": mod.sha256_text("resolved text")}),
                      launch=launch, cap_usd=CAP, resolver_cap_usd=RESOLVER_CAP, resolver_model=RESOLVER_MODEL,
                      model=REVIEWER_MODEL)
        fields.update(over)
        return mod.Request(**fields)

    def store(self):
        root = self.tmp / "sdlc" / "state" / "upkeep" / "reviews"
        return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.delenv("SIGMA_UPKEEP_JOB", raising=False)
    return Rig(tmp_path)


def test_closed_gate_has_no_effect(rig):
    for config in ({}, {"upkeep": {"enabled": True}}, {"upkeep": {"enabled": True, "conflicts": {"resolve": "mechanical"}}}):
        result = rig.mod.review(rig.request(config=config))
        assert result.outcome == "closed" and result.verified is False
    assert rig.marker() is None and rig.store() == []
    assert not any((rig.tmp / "state").iterdir())


@pytest.mark.parametrize("mechanism", ["inline", "subagent", "whatever"])
def test_inline_and_subagent_are_refused(rig, mechanism):
    result = rig.mod.review(rig.request(mechanism=mechanism))
    assert result.outcome == "refused"
    assert rig.marker() is None and rig.store() == []


def test_approve_through_a_fake_claude_with_the_right_brief(rig):
    result = rig.mod.review(rig.request())
    assert result.outcome == "approve", result
    assert result.verified is False and result.reasons == ("keeps both sides",)
    marker = rig.marker()
    for text in ("base text", "ours text", "theirs text", "resolved text", "PR one"):
        assert text in marker["stdin"]
    assert REVIEWER_MODEL in marker["argv"] and marker["argv"][:3] == ["-p", "--output-format", "json"]
    assert result.charged_usd == pytest.approx(0.5)
    assert len(rig.store()) == 1


def test_brief_has_no_resolver_transcript_input(rig):
    assert not any("transcript" in f or "resolver_out" in f for f in rig.mod.Request._fields)
    rig.mod.review(rig.request())
    assert "RESOLVER-SAID" not in rig.marker()["stdin"]


@pytest.mark.parametrize("reply,verdict", [
    ('{"verdict":"approve","reasons":[]}', "approve"),
    ('{"verdict":"block","reasons":["x"]}', "block"),
    ('{"verdict":"approve"}', "block"),
    ('{"verdict":"approve","reasons":[],"extra":1}', "block"),
    ('{"verdict":"Approve","reasons":[]}', "block"),
    ('{"verdict":true,"reasons":[]}', "block"),
    ('{"verdict":"approve","reasons":[1]}', "block"),
    ('{"verdict":"approve","reasons":"ok"}', "block"),
    ('{"verdict":"approve","verdict":"approve","reasons":[]}', "block"),
    ('{"verdict":"approve","reasons":[NaN]}', "block"),
    ('["approve"]', "block"),
    ('approve', "block"),
    ('', "block"),
    (None, "block"),
    (json.dumps({"result": '{"verdict":"approve","reasons":[]}'}), "approve"),
    (json.dumps({"result": 'approve'}), "block"),
    ('{"verdict":"approve","reasons":[' + ",".join(['"r"'] * 21) + ']}', "block"),
])
def test_validator_table(rig, reply, verdict):
    assert rig.mod.parse_reply(reply)[0] == verdict


def test_bad_json_through_the_session_is_block(rig):
    rig.mode({"reply": "not json at all"})
    assert rig.mod.review(rig.request()).outcome == "block"


def test_timeout_is_block_and_charged_the_cap(rig):
    rig.mode({"sleep": True})
    req = rig.request()
    req.launch["timeout"] = 1
    result = rig.mod.review(req)
    assert result.outcome == "block" and result.charged_usd == CAP


def test_reasons_are_neutralised(rig):
    reasons = rig.mod.neutralise(["fixes #12 <!-- x --> sigma:approve @bob `rm`\nline", "y" * 900])
    first = reasons[0]
    for bad in ("#12", "<!--", "-->", "sigma:", "@", "`", "\n"):
        assert bad not in first
    assert len(reasons[1]) == rig.mod.MAX_REASON_CHARS
    rig.mode({"reply": json.dumps({"verdict": "block", "reasons": ["closes #9 sigma:approve"]})})
    result = rig.mod.review(rig.request())
    assert result.outcome == "block" and "#9" not in result.reasons[0] and "sigma:" not in result.reasons[0]


def test_leak_gate_blocks_a_scratch_path(rig):
    leaked = [("a.txt", "b", "o", "t", "resolved")]
    reviewer = rig.mod._sibling("reviewer")
    ok, _ = reviewer._check_brief_text("text with /tmp/scratch/maker/notes in it", ("/tmp/scratch/maker",))
    assert ok is False
    # the module runs the same gate over the exact brief; a brief the gate rejects launches nothing and writes nothing
    rig.mod._LOADED["reviewer"] = type("R", (), {"_check_brief_text": staticmethod(lambda t, p: (False, ["leak"]))})
    result = rig.mod.review(rig.request(files=leaked))
    assert result.outcome == "block" and "leak gate" in result.reasons[0]
    assert rig.marker() is None and rig.store() == []


def test_leak_gate_drops_implementation_findings(rig):
    ok, findings = rig.mod._sibling("reviewer")._check_brief_text("a plain brief", ())
    assert ok and findings == []


def test_tree_changed_rejects_the_verdict(rig):
    result = rig.mod.review(rig.request(reread=lambda: ("t2", {"a.txt": rig.mod.sha256_text("resolved text")})))
    assert result.outcome == "block" and "changed" in result.reasons[0]
    result = rig.mod.review(rig.request(reread=lambda: ("t1", {"a.txt": "0" * 64})))
    assert result.outcome == "block"
    def boom():
        raise OSError("gone")
    assert rig.mod.review(rig.request(reread=boom)).outcome == "block"


def test_manifest_is_write_once_and_complete(rig):
    first = rig.mod.review(rig.request())
    second = rig.mod.review(rig.request())
    files = rig.store()
    assert [p.name for p in files] == ["1.json", "2.json"]
    doc = json.loads(files[0].read_text())
    assert first.manifest["generation"] == 1 and second.manifest["generation"] == 2
    assert doc["unit"] == "voice" and doc["base"] == "b" * 40 and doc["head"] == "h" * 40 and doc["tree"] == "t1"
    assert doc["paths"] == {"a.txt": rig.mod.sha256_text("resolved text")} and len(doc["brief_sha256"]) == 64
    path, why = rig.mod.write_manifest(str(rig.tmp / "sdlc"), "voice", dict(doc))
    assert path.name == "3.json"                       # an existing generation is never rewritten
    assert json.loads(files[0].read_text()) == doc


def test_generation_cap_blocks_another_review(rig):
    for _ in range(rig.mod.MAX_GENERATIONS):
        assert rig.mod.write_manifest(str(rig.tmp / "sdlc"), "voice", {"x": 1})[0] is not None
    assert rig.mod.review(rig.request()).outcome == "block"


def test_store_is_unit_keyed_and_refuses_a_bad_name(rig):
    assert rig.mod.review(rig.request(unit="../evil")).outcome == "refused"
    rig.mod.review(rig.request(unit="Voice"))
    assert (rig.tmp / "sdlc" / "state" / "upkeep" / "reviews" / "voice" / "1.json").exists()
    assert not (rig.tmp / "sdlc" / "state" / "review-generations").exists()


def test_prune_bounds_the_store(rig):
    rig.mod.review(rig.request())
    old = rig.store()[0]
    os.utime(old, (1, 1))
    keep = old.parent / "2.json"
    keep.write_text("{}")
    other = old.parent / "notes.txt"
    other.write_text("x")
    os.utime(other, (1, 1))
    assert rig.mod.prune(str(rig.tmp / "sdlc"), rig.request().config) == 1
    assert not old.exists() and keep.exists() and other.exists()
    assert rig.mod.prune(str(rig.tmp / "sdlc"), {}) == 0


def test_caps_and_models(rig):
    assert rig.mod.review(rig.request(cap_usd=RESOLVER_CAP)).outcome == "refused"
    assert rig.mod.review(rig.request(cap_usd=None)).outcome == "refused"
    cat = {"a": RESOLVER_MODEL, "b": REVIEWER_MODEL}
    launcher = rig.mod._sibling("feature_upkeep_launcher")
    assert rig.mod.pick_model(cat, RESOLVER_MODEL) == REVIEWER_MODEL
    assert rig.mod.pick_model({"a": RESOLVER_MODEL}, RESOLVER_MODEL) == launcher.PLACEHOLDER_MODEL
    assert rig.mod.pick_model(None, RESOLVER_MODEL) == launcher.PLACEHOLDER_MODEL
    result = rig.mod.review(rig.request(model=None))          # the shipped catalog is all placeholder: closed
    assert result.outcome == "refused" and rig.marker() is None


def test_unverified_flag_is_refused_before_a_spawn(rig):
    req = rig.request()
    req.launch["flags"] = ("--max-budget-usd",)
    result = rig.mod.review(req)
    assert result.outcome == "refused" and "UNVERIFIED" in result.reasons[0] and rig.marker() is None


def test_host_command_table_untouched_and_resolve_never_called():
    import ast
    spec = importlib.util.spec_from_file_location("reviewer_t", SCRIPTS / "reviewer.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "claude" not in mod._HOST_COMMANDS
    tree = ast.parse(PATH.read_text(encoding="utf-8"))
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "_HOST_COMMANDS" not in names
    reviewer_calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                      and "reviewer" in ast.dump(n.func.value)}
    assert reviewer_calls == {"_check_brief_text"}


# ------------------------------------------------------------------------------------------ #1072

def _launches_nothing(rig, result):
    assert result.outcome == "block" and "truncated" in result.reasons[0]
    assert rig.marker() is None and rig.store() == []


def test_truncated_file_blocks_the_review(rig):
    big = "x" * (rig.mod.MAX_FILE_CHARS + 1)
    _launches_nothing(rig, rig.mod.review(rig.request(files=[("a.txt", "b", "o", "t", big)])))


def test_dropped_files_block_the_review(rig):
    many = [("f%d.txt" % i, "b", "o", "t", "r") for i in range(rig.mod.MAX_FILES + 1)]
    _launches_nothing(rig, rig.mod.review(rig.request(files=many)))


def test_dropped_or_cut_pr_descriptions_block_the_review(rig):
    _launches_nothing(rig, rig.mod.review(rig.request(pr_descriptions=["p"] * (rig.mod.MAX_PR_COUNT + 1))))
    _launches_nothing(rig, rig.mod.review(rig.request(pr_descriptions=["p" * (rig.mod.MAX_PR_CHARS + 1)])))


def test_brief_budget_overflow_blocks_the_review(rig):
    chunk = "y" * rig.mod.MAX_FILE_CHARS
    files = [("f%d.txt" % i, chunk, chunk, chunk, chunk) for i in range(2 + rig.mod.MAX_BRIEF_CHARS // (4 * len(chunk)))]
    _launches_nothing(rig, rig.mod.review(rig.request(files=files)))


def test_untruncated_brief_still_reviews(rig):
    assert rig.mod.review(rig.request()).outcome == "approve"


def test_manifest_generation_is_assigned_by_the_store_not_the_builder(rig):
    import inspect
    assert "generation" not in inspect.signature(rig.mod.make_manifest).parameters
    prompt, blocks = rig.mod.build_brief(rig.files(), [])
    built = rig.mod.make_manifest("voice", "b" * 40, "h" * 40, "t1", rig.files(), prompt, blocks)
    assert "generation" not in built                    # no placeholder that could be mistaken for the real one
    result = rig.mod.review(rig.request())
    assert result.manifest["generation"] == 1 and rig.store()[0].name == "1.json"
