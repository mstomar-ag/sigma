"""#933: the unit landing approval and the unattended guard (feature_land_approval), proven offline.

Every test builds its own project directory under tmp_path; nothing touches the network, a model or the real tree.
`test_concurrent_consumers_smoke` is a labeled SMOKE test of a probabilistic race; the deterministic control at the
exact seam is `test_marker_already_present_denies_before_any_merge`."""
import importlib.util
import pathlib
import threading

import pytest

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"
SRC = SCRIPTS / "feature_land_approval.py"
HEAD = "a" * 40
OTHER = "b" * 40
SLUG = "owner/repo"
OPEN = {"upkeep": {"enabled": True}}


def load(edit=None):
    assert SRC.exists(), "feature_land_approval.py does not exist yet"
    text = SRC.read_text(encoding="utf-8")
    if edit:
        assert edit[0] in text, "mutation target drifted: %r" % (edit[0],)
        text = text.replace(*edit)
    spec = importlib.util.spec_from_loader("fla_variant", loader=None)
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = str(SRC)
    exec(compile(text, str(SRC), "exec"), mod.__dict__)
    return mod


def tree(root):
    return sorted((p.relative_to(root).as_posix(), p.read_bytes() if p.is_file() else None) for p in root.rglob("*"))


def approve(mod, sdlc, unit="voice", head=HEAD, now=1000, **kw):
    return mod.approve(OPEN, sdlc, unit, SLUG, head, now=now, **kw)


def test_closed_gate_is_byte_identical(tmp_path):
    mod = load()
    (tmp_path / "keep.txt").write_text("x")
    before = tree(tmp_path)
    for cfg in ({}, None, {"upkeep": {"enabled": "true"}}, {"upkeep": {"enabled": False}}):
        out = mod.approve(cfg, tmp_path, "voice", SLUG, HEAD, now=1)
        assert out.get("closed") is True, out
        out = mod.consume(cfg, tmp_path, "voice", SLUG, HEAD, now=1)
        assert out.get("closed") is True, out
        out = mod.authorize(cfg, tmp_path, "voice", SLUG, HEAD, argv=[], environ={}, now=1)
        assert out.get("closed") is True, out
    assert tree(tmp_path) == before


def test_approve_writes_bound_record_in_fresh_state_dir(tmp_path):
    mod = load()
    out = approve(mod, tmp_path, ttl_seconds=600)
    assert out["ok"] is True, out
    files = list((tmp_path / "state" / "unit-approvals").iterdir())
    assert len(files) == 1 and "voice" not in files[0].name, files
    text = files[0].read_text()
    for needle in (SLUG, HEAD, "voice"):
        assert needle in text
    assert mod.consume(OPEN, tmp_path, "voice", SLUG, HEAD, now=1100)["ok"] is True


def test_approve_refuses_bad_inputs(tmp_path):
    mod = load()
    assert approve(mod, tmp_path, unit="../x")["ok"] is False
    assert approve(mod, tmp_path, head="main")["ok"] is False
    assert mod.approve(OPEN, tmp_path, "voice", "no slash", HEAD, now=1)["ok"] is False
    assert approve(mod, tmp_path, ttl_seconds=0)["ok"] is False
    assert approve(mod, tmp_path, ttl_seconds=10 ** 9)["ok"] is False
    assert not (tmp_path / "state").exists()


def test_single_use_marker_exists_when_consume_returns(tmp_path):
    mod = load()
    approve(mod, tmp_path)
    assert mod.consume(OPEN, tmp_path, "voice", SLUG, HEAD, now=1001)["ok"] is True
    assert list((tmp_path / "state" / "unit-approvals").glob("*.used")), "marker must exist before the merge call"
    again = mod.consume(OPEN, tmp_path, "voice", SLUG, HEAD, now=1002)
    assert again["ok"] is False and "used" in again["reason"]


def test_marker_already_present_denies_before_any_merge(tmp_path):
    """Deterministic control at the seam: the marker is created between approve and consume."""
    mod = load()
    approve(mod, tmp_path)
    d = tmp_path / "state" / "unit-approvals"
    rec = next(d.glob("*.json"))
    (d / (rec.stem + ".used")).write_text("")
    assert mod.consume(OPEN, tmp_path, "voice", SLUG, HEAD, now=1001)["ok"] is False


def test_concurrent_consumers_smoke(tmp_path):
    mod = load()
    approve(mod, tmp_path)
    results = []
    gate = threading.Barrier(8)

    def go():
        gate.wait()
        results.append(mod.consume(OPEN, tmp_path, "voice", SLUG, HEAD, now=1001)["ok"])
    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(True) == 1, results


def test_binding_unit_slug_head(tmp_path):
    mod = load()
    approve(mod, tmp_path)
    d = tmp_path / "state" / "unit-approvals"
    assert mod.consume(OPEN, tmp_path, "voice", SLUG, OTHER, now=1001)["ok"] is False
    assert mod.consume(OPEN, tmp_path, "voice", "owner/other", HEAD, now=1001)["ok"] is False
    assert mod.consume(OPEN, tmp_path, "audio", SLUG, HEAD, now=1001)["ok"] is False
    assert not list(d.glob("*.used")), "a denied consume must not burn it"
    assert mod.consume(OPEN, tmp_path, "VOICE", SLUG, HEAD, now=1001)["ok"] is True   # the unit fold


def test_expiry(tmp_path):
    mod = load()
    approve(mod, tmp_path, ttl_seconds=60)
    assert mod.consume(OPEN, tmp_path, "voice", SLUG, HEAD, now=1060)["ok"] is False
    assert not list((tmp_path / "state" / "unit-approvals").glob("*.used")), "a denied consume must not burn it"


def test_malformed_records_deny(tmp_path):
    mod = load()
    approve(mod, tmp_path)
    rec = next((tmp_path / "state" / "unit-approvals").glob("*.json"))
    for body in ("", "[]", "{", '{"unit": "voice"}', '{"unit":"voice","slug":"owner/repo","head":"%s","expires_at":"soon"}' % HEAD):
        rec.write_text(body)
        assert mod.consume(OPEN, tmp_path, "voice", SLUG, HEAD, now=1001)["ok"] is False, body
        assert not list(rec.parent.glob("*.used")), "a denied consume must not burn it"


def test_mismatched_record_burns_nothing(tmp_path):
    mod = load()
    approve(mod, tmp_path)
    rec = next((tmp_path / "state" / "unit-approvals").glob("*.json"))
    rec.write_text('{"unit":"audio","slug":"owner/repo","head":"%s","expires_at":99999}' % HEAD)
    out = mod.consume(OPEN, tmp_path, "voice", SLUG, HEAD, now=1001)
    assert out["ok"] is False and "match" in out["reason"]
    assert not list(rec.parent.glob("*.used"))


def test_used_message_says_new_head_not_approve_again(tmp_path):
    mod = load()
    approve(mod, tmp_path)
    assert mod.consume(OPEN, tmp_path, "voice", SLUG, HEAD, now=1001)["ok"] is True
    msg = mod.consume(OPEN, tmp_path, "voice", SLUG, HEAD, now=1002)["reason"]
    assert "already used" in msg and "new head" in msg and "approve verb" not in msg, msg


def test_empty_fingerprint_value_is_unattended(tmp_path):
    mod = load()
    for name in ("SIGMA_RUN_ID", "SIGMA_AUTOWATCH_HOP", "SIGMA_SESSION_GENERATION"):
        out = mod.authorize(OPEN, tmp_path, "voice", SLUG, HEAD, argv=FLAG, environ={name: ""}, now=1)
        assert out["ok"] is False, name


def _outside(tmp_path):
    out = tmp_path / "outside"
    out.mkdir()
    return out


def test_symlinked_state_dir_refused(tmp_path):
    mod = load()
    sdlc = tmp_path / "sdlc"; sdlc.mkdir()
    out = _outside(tmp_path)
    (sdlc / "state").symlink_to(out)
    assert approve(mod, sdlc)["ok"] is False
    assert mod.consume(OPEN, sdlc, "voice", SLUG, HEAD, now=1001)["ok"] is False
    assert list(out.rglob("*")) == []


def test_symlinked_approvals_dir_refused(tmp_path):
    mod = load()
    sdlc = tmp_path / "sdlc"; (sdlc / "state").mkdir(parents=True)
    out = _outside(tmp_path)
    (sdlc / "state" / "unit-approvals").symlink_to(out)
    assert approve(mod, sdlc)["ok"] is False
    assert mod.consume(OPEN, sdlc, "voice", SLUG, HEAD, now=1001)["ok"] is False
    assert list(out.rglob("*")) == []


def test_symlinked_record_path_refused(tmp_path):
    mod = load()
    sdlc = tmp_path / "sdlc"
    out = _outside(tmp_path)
    target = out / "victim.txt"; target.write_text("keep")
    rec, _ = mod.approval_path(sdlc, SLUG, HEAD, "voice")
    rec.parent.mkdir(parents=True)
    rec.symlink_to(target)
    assert approve(mod, sdlc)["ok"] is False
    assert target.read_text() == "keep"
    assert mod.consume(OPEN, sdlc, "voice", SLUG, HEAD, now=1001)["ok"] is False
    assert not list(rec.parent.glob("*.used"))


FLAG = ["--user-requested", "voice"]


def test_attended_consent_needs_no_approval(tmp_path):
    mod = load()
    out = mod.authorize(OPEN, tmp_path, "voice", SLUG, HEAD, argv=FLAG, environ={}, now=1)
    assert out["ok"] is True and out["mode"] == "attended", out
    assert not (tmp_path / "state").exists()


def test_flag_for_another_unit_or_prefix_is_not_consent(tmp_path):
    mod = load()
    for argv in ([], ["--user-requested", "audio"], ["--user-requested=voice"], ["--user-requested", "voice2"],
                 ["--user-requested"], ["x", "--user-requested", "VOICE "]):
        out = mod.authorize(OPEN, tmp_path, "voice", SLUG, HEAD, argv=argv, environ={}, now=1)
        assert out["ok"] is False, argv


def test_driven_launcher_fingerprint_denies_consent(tmp_path):
    mod = load()
    for env in ({"SIGMA_RUN_ID": "autowatch-1-2"}, {"SIGMA_AUTOWATCH_HOP": "1"}, {"SIGMA_RUN_ID": "session-9"},
                {"SIGMA_SESSION_GENERATION": "3"}):
        out = mod.authorize(OPEN, tmp_path, "voice", SLUG, HEAD, argv=FLAG, environ=env, now=1)
        assert out["ok"] is False and "approval" in out["reason"], (env, out)


def test_driven_with_approval_lands_once(tmp_path):
    mod = load()
    approve(mod, tmp_path)
    env = {"SIGMA_RUN_ID": "autowatch-1-2"}
    first = mod.authorize(OPEN, tmp_path, "voice", SLUG, HEAD, argv=[], environ=env, now=1001)
    assert first["ok"] is True and first["mode"] == "approval", first
    second = mod.authorize(OPEN, tmp_path, "voice", SLUG, HEAD, argv=[], environ=env, now=1002)
    assert second["ok"] is False


def review(login, state="APPROVED", commit=HEAD):
    return {"author": {"login": login}, "state": state, "commit": {"oid": commit}}


def test_native_review_mode(tmp_path):
    mod = load()
    approve(mod, tmp_path)           # a marker approval must NOT satisfy the stronger mode
    ok = mod.native_review(OPEN, pr_author="me", head=HEAD, reviews=[review("you")])
    assert ok["ok"] is True
    for reviews in ([], [review("me")], [review("you", "COMMENTED")], [review("you", commit=OTHER)], [review("you", "CHANGES_REQUESTED")]):
        out = mod.native_review(OPEN, pr_author="me", head=HEAD, reviews=reviews)
        assert out["ok"] is False, reviews
    assert "land attended" in mod.native_review(OPEN, pr_author="me", head=HEAD, reviews=[review("me")])["reason"]
    out = mod.authorize(OPEN, tmp_path, "voice", SLUG, HEAD, argv=[], environ={"SIGMA_RUN_ID": "session-1"},
                        now=1001, require_native_review=True, pr_author="me", reviews=[review("me")])
    assert out["ok"] is False and not list((tmp_path / "state" / "unit-approvals").glob("*.used"))


def test_module_is_offline_and_deletes_nothing():
    text = load.__globals__["SRC"].read_text(encoding="utf-8")
    for token in ("subprocess", "urllib", "socket", "os.remove", "unlink", "rmtree", "os.replace", "gh_api", "--delete", "DELETE"):
        assert token not in text, token
    assert "SIGMA_" not in text.replace("SIGMA_RUN_ID", "").replace("SIGMA_AUTOWATCH_HOP", "").replace("SIGMA_SESSION_GENERATION", "")


def test_record_copied_to_another_heads_name_denies(tmp_path):
    """The file name is only an address; the fields inside are what bind the approval."""
    mod = load()
    approve(mod, tmp_path)
    src, _ = mod.approval_path(tmp_path, SLUG, HEAD, "voice")
    dst, _ = mod.approval_path(tmp_path, SLUG, OTHER, "voice")
    dst.write_text(src.read_text())
    assert mod.consume(OPEN, tmp_path, "voice", SLUG, OTHER, now=1001)["ok"] is False
