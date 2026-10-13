"""Goal 1306: an `approve` verb on rule_draft turns a proposed rule into an approved, sealed one."""
import datetime
import importlib.util
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"
T0 = datetime.datetime(2026, 10, 11, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _mod(name):
    spec = importlib.util.spec_from_file_location(name + "_t1306", SCRIPTS / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


RD, RB = _mod("rule_draft"), _mod("rulebook")


def _cfg(enabled=True):
    return {"rulebook": {"records": {"enabled": True}, "rules": {"enabled": enabled},
                         "autonomy": {"enabled": True, "owners": ["boss"]}}}


def _setup(tmp_path, monkeypatch, login="boss", cfg=None, propose=True):
    sd, cfg = str(tmp_path / ".sdlc"), cfg or _cfg()
    (tmp_path / ".sdlc").mkdir()
    (tmp_path / ".sdlc" / "config.json").write_text(json.dumps(cfg))
    monkeypatch.setattr(RD, "_invoker", lambda config: login)
    path = None
    if propose:
        path = RB.propose(sd, {"qkind": "needs_decision", "area": "docs", "option": "merge", "reason": "r"},
                          _cfg(), now=T0)
    return sd, path


def _rules(path):
    return json.loads(pathlib.Path(path).read_text())


def _rid(path):
    return _rules(path)[0]["id"] if isinstance(_rules(path), list) else _rules(path)["rules"][0]["id"]


def test_approve_by_id_makes_rule_match(tmp_path, monkeypatch, capsys):
    sd, path = _setup(tmp_path, monkeypatch)
    rid = _rid(path)
    assert RD.main(["approve", sd, rid]) == 0
    assert rid in capsys.readouterr().out
    rules = RB.load(sd, _cfg())
    assert rules[0]["status"] == "approved" and rules[0]["confirmed"] and rules[0]["seal"]
    assert RB.match("needs_decision", "docs", "merge", rules, _cfg())


def test_unknown_id_refused_one_line(tmp_path, monkeypatch, capsys):
    sd, path = _setup(tmp_path, monkeypatch)
    assert RD.main(["approve", sd, "rule-nope"]) == 2
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1 and "Traceback" not in err
    assert RB.load(sd, _cfg())[0]["status"] == "proposed"


def test_already_approved_refused_and_unchanged(tmp_path, monkeypatch, capsys):
    sd, path = _setup(tmp_path, monkeypatch)
    rid = _rid(path)
    assert RD.main(["approve", sd, rid]) == 0
    before = pathlib.Path(path).read_bytes()
    capsys.readouterr()
    assert RD.main(["approve", sd, rid]) == 2
    assert "already approved" in capsys.readouterr().err
    assert pathlib.Path(path).read_bytes() == before


def test_non_owner_refused_with_hold_line(tmp_path, monkeypatch, capsys):
    sd, path = _setup(tmp_path, monkeypatch, login="stranger")
    before = pathlib.Path(path).read_bytes()
    assert RD.main(["approve", sd, _rid(path)]) == 2
    err = capsys.readouterr().err
    assert "needs owner approval" in err
    assert pathlib.Path(path).read_bytes() == before


def test_unresolved_login_refused(tmp_path, monkeypatch, capsys):
    sd, path = _setup(tmp_path, monkeypatch, login=None)
    assert RD.main(["approve", sd, _rid(path)]) == 2
    assert "cannot resolve" in capsys.readouterr().err
    assert RB.load(sd, _cfg())[0]["status"] == "proposed"


def test_closed_gate_writes_nothing(tmp_path, monkeypatch, capsys):
    sd, path = _setup(tmp_path, monkeypatch)
    rid = _rid(path)
    before = pathlib.Path(path).read_bytes()
    (tmp_path / ".sdlc" / "config.json").write_text(json.dumps(_cfg(enabled=False)))
    assert RD.main(["approve", sd, rid]) == 2
    assert "closed" in capsys.readouterr().err
    assert pathlib.Path(path).read_bytes() == before


def test_no_person_field_after_approval(tmp_path, monkeypatch):
    sd, path = _setup(tmp_path, monkeypatch)
    assert RD.main(["approve", sd, _rid(path)]) == 0
    assert "boss" not in pathlib.Path(path).read_text()


def test_config_template_names_the_verb():
    text = (ROOT / "skills" / "sigma-init" / "templates" / "config.json.tmpl").read_text()
    assert "rule_draft.py approve" in text


def test_unpark_skill_names_the_draft_then_approve_verbs():
    """Goal 1326: the unpark skill tells an agent the verbs, in order, so approval is discoverable."""
    text = (ROOT / "skills" / "sigma-unpark" / "SKILL.md").read_text(encoding="utf-8")
    assert "rule_draft.py draft" in text
    assert "rule_draft.py approve <state-folder> <rule id>" in text
    assert text.index("rule_draft.py draft") < text.index("rule_draft.py approve")
