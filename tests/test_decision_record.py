"""Decision rubric slice 8: record writers (decision_record + unpark, promote, ack, sweep)."""
import json, pathlib, importlib.util

import test_unpark as tu
import test_promote as tp
import test_auto_unpark as ta

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def _on(cfg, **records):
    cfg = dict(cfg)
    cfg["decision_rubric"] = {"records": {"enabled": True, **records}}
    return cfg


def _bodies(run, number=None):
    out = []
    for c in run.calls:
        if c[:2] == ["issue", "comment"] and (number is None or str(c[2]) == str(number)):
            out.append(c[c.index("--body") + 1])
    return out


def _ids(sdlc):
    store = pathlib.Path(sdlc) / "decisions"
    return sorted(p.stem for p in store.glob("*.json") if not p.name.startswith("_"))


class Src:
    def __init__(self, fail=False):
        self.posted, self.fail = [], fail

    def _issue_comment(self, n, text):
        if self.fail:
            raise RuntimeError("down")
        self.posted.append((str(n), text))


def test_how_for_and_build():
    d = _mod("decision_record")
    assert d.how_for("sweep") == "autonomous"
    assert d.how_for("unpark") == d.how_for("promote") == d.how_for("ack") == "human"
    try:
        d.how_for("nope"); assert False
    except ValueError:
        pass
    r = d.build("unpark", 5, "bogus", "", "why", "human")
    assert r["qkind"] == "unknown" and r["choice"] and r["issue"] == 5 and r["how"] == "human"
    assert "login" not in r and "user" not in r


def test_unpark_writes_store_and_comment(tmp_path):
    u = _mod("unpark")
    run = tu._runner(views={"5": tu._view(labels=("sdlc:parked",), comments=[tu._park("x")])})
    cfg = _on(tu._config())
    res = u.resolve(str(tmp_path), cfg, 5, {"decision": "start it again"}, "unpark", run=run)
    assert res["outcome"] == "unparked"
    ids = _ids(tmp_path)
    assert len(ids) == 1
    assert any(ids[0] in b for b in _bodies(run, 5))
    rec = json.loads((tmp_path / "decisions" / (ids[0] + ".json")).read_text())
    assert rec["how"] == "human" and rec["gesture"] == "unpark"


def test_sweep_records_autonomous(tmp_path):
    au = _mod("auto_unpark")
    issue = ta._blocked_issue(42, body="blocked by #7 until the base lands")
    run = ta._label_aware_sweep_runner(
        by_label={"sdlc:parked": "[]", "sdlc:blocked": json.dumps([issue])}, states={"7": "CLOSED"})
    res = au.sweep_unpark(str(tmp_path), _on(ta._config()), apply=True, run=run)
    assert res["unparked"] == ["42"]
    ids = _ids(tmp_path)
    assert len(ids) == 1
    rec = json.loads((tmp_path / "decisions" / (ids[0] + ".json")).read_text())
    assert rec["how"] == "autonomous" and rec["gesture"] == "sweep"
    assert any(ids[0] in b for b in _bodies(run, 42))


def test_promote_records_and_comments(tmp_path):
    p = _mod("promote")
    run = tp._runner(views={"5": tp._view("sdlc:needs-confirmation")})
    res = p.promote(str(tmp_path), _on(tp._config()), ["5"], run=run)
    assert [r["outcome"] for r in res["results"]] == ["promoted"]
    ids = _ids(tmp_path)
    assert len(ids) == 1 and any(ids[0] in b for b in _bodies(run, 5))


def test_ack_writes_the_store_record_only_when_open(tmp_path):
    h = _mod("handoff")
    h.acknowledge(str(tmp_path), {}, 9, "taken", why="ok", goal="g")
    assert _ids(tmp_path) == []
    h.acknowledge(str(tmp_path), _on({}), 9, "taken", why="ok", goal="g")
    ids = _ids(tmp_path)
    assert len(ids) == 1
    assert json.loads((tmp_path / "decisions" / (ids[0] + ".json")).read_text())["gesture"] == "ack"


def test_trigger_words_plant_no_blocker(tmp_path):
    d = _mod("decision_record")
    bc = _mod("backlog_check")
    src = Src()
    rec = d.build("unpark", 5, "needs_decision",
                  "waiting on the sign-off see #1234", "blocked by #77 depends on #78 requires #79", "human")
    out = d.write_all(str(tmp_path), rec, _on({}), src)
    assert out["id"] and out["error"] is None
    text = "\n".join(t for _n, t in src.posted)
    assert "#1234" in text
    assert not bc._BLOCK_RE.search(bc._blocker_haystack({"raw": text}))


def test_closed_block_is_byte_identical(tmp_path):
    u = _mod("unpark")
    p = _mod("promote")
    au = _mod("auto_unpark")
    d = _mod("decision_record")
    src = Src()
    out = d.write_all(str(tmp_path), d.build("promote", 5, "unknown", "c", "r", "human"), {}, src)
    assert out == {"id": None, "error": None, "commented": False} and src.posted == []
    assert not (tmp_path / "decisions").exists()
    # unpark with the gate closed: the comment is the bare block, as before
    run = tu._runner(views={"5": tu._view(labels=("sdlc:parked",), comments=[tu._park("x")])})
    u.resolve(str(tmp_path), tu._config(), 5, {"decision": "go"}, "unpark", run=run)
    assert _bodies(run, 5) == [u.render_block({"decision": "go"})]
    run = tp._runner(views={"5": tp._view("sdlc:needs-confirmation")})
    p.promote(str(tmp_path), tp._config(), ["5"], run=run)
    assert _bodies(run, 5) == [p.PROMOTE_COMMENT.format(goal="sdlc:goal")]
    assert not (tmp_path / "decisions").exists()


def test_store_failure_still_posts_comment(tmp_path):
    d = _mod("decision_record")
    src = Src()
    (tmp_path / "decisions").write_text("a file where the folder should be")
    out = d.write_all(str(tmp_path), d.build("promote", 5, "unknown", "c", "r", "human"),
                      _on({}), src, text="hello")
    assert out["id"] is None and out["error"] and out["commented"] is True
    assert src.posted and src.posted[0][1].startswith("hello")


def test_public_comment_drops_free_text(tmp_path):
    d = _mod("decision_record")
    src = Src()
    d.write_all(str(tmp_path), d.build("unpark", 5, "unknown", "secretchoice", "secretreason", "human"),
                _on({}, repo_visibility="public"), src)
    assert "secretchoice" not in src.posted[0][1] and "secretreason" not in src.posted[0][1]


def test_gh_sites_go_through_the_helper():
    for name in ("decision_record",):
        text = (S / (name + ".py")).read_text()
        assert "subprocess" not in text and '"gh"' not in text and "'gh'" not in text
        assert "gh issue" not in text and "gh api" not in text


# --- #1092: a record failure with the gate open never breaks the gesture or double-posts ----------

def _boom(*a, **k):
    raise RuntimeError("boom")


def test_unpark_record_build_error_is_swallowed_and_comment_posted(tmp_path):
    u = _mod("unpark")
    u.decision_record.build = _boom
    run = tu._runner(views={"5": tu._view(labels=("sdlc:parked",), comments=[tu._park("x")])})
    res = u.resolve(str(tmp_path), _on(tu._config()), 5, {"decision": "go"}, "unpark", run=run)
    assert res["outcome"] == "unparked" and "decision record failed" in res["detail"]
    assert _bodies(run, 5) == [u.render_block({"decision": "go"})]


def test_promote_record_build_error_is_swallowed_and_comment_posted(tmp_path):
    p = _mod("promote")
    p._feature("decision_record").build = _boom
    run = tp._runner(views={"5": tp._view("sdlc:needs-confirmation")})
    res = p.promote(str(tmp_path), _on(tp._config()), ["5"], run=run)
    r = res["results"][0]
    assert r["outcome"] == "promoted" and "decision record failed" in r["detail"]
    assert _bodies(run, 5) == [p.PROMOTE_COMMENT.format(goal="sdlc:goal")]


def _sweep(tmp_path, cfg):
    au = _mod("auto_unpark")
    issue = ta._blocked_issue(42, body="blocked by #7 until the base lands")
    run = ta._label_aware_sweep_runner(
        by_label={"sdlc:parked": "[]", "sdlc:blocked": json.dumps([issue])}, states={"7": "CLOSED"})
    return au, run, au.sweep_unpark(str(tmp_path), cfg, apply=True, run=run)


def test_sweep_never_posts_twice_when_record_write_raises_after_posting(tmp_path):
    au = _mod("auto_unpark")
    real = au._load

    class Fake:
        enabled = staticmethod(lambda c: True)
        build = staticmethod(lambda *a, **k: {})
        how_for = staticmethod(lambda g: "autonomous")

        @staticmethod
        def write_all(sdlc, rec, cfg, src, text="", poster=None):
            poster(42, text)
            raise RuntimeError("late")
    au._load = lambda n: Fake if n == "decision_record" else real(n)
    issue = ta._blocked_issue(42, body="blocked by #7 until the base lands")
    run = ta._label_aware_sweep_runner(
        by_label={"sdlc:parked": "[]", "sdlc:blocked": json.dumps([issue])}, states={"7": "CLOSED"})
    au.sweep_unpark(str(tmp_path), _on(ta._config()), apply=True, run=run)
    assert len(_bodies(run, 42)) == 1


def test_sweep_comment_is_byte_identical_with_gate_closed(tmp_path):
    au, run, res = _sweep(tmp_path, ta._config())
    assert res["unparked"] == ["42"]
    bodies = _bodies(run, 42)
    assert len(bodies) == 1
    assert bodies[0] == ("Auto-unparked by Sigma \u2014 blocker(s) #7 closed; re-added sdlc:goal "
                         "for re-examination.")
    assert "Decision record" not in bodies[0] and not (tmp_path / "decisions").exists()
