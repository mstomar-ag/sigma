"""The doctor's public-records warning reads the real repository visibility (REST), not only the config."""
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _doctor():
    p = ROOT / "skills/sigma-doctor/scripts/doctor.py"
    spec = importlib.util.spec_from_file_location("doctor_realvis", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _cfg(vis="private", enabled=True):
    return {"rulebook": {"records": {"enabled": enabled, "repo_visibility": vis}}}


class _Run:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


WARN = "public repository with records on"


def test_real_public_warns_when_config_says_private(tmp_path):
    d = _doctor()
    run = _Run("false\n")
    # private flag false == public
    out = d._rulebook_state(_cfg("private"), tmp_path, run=run)
    assert WARN in out
    assert any(c[:2] == ["gh", "api"] and "--jq" in c and ".private" in c for c in run.calls)


def test_real_private_does_not_warn_even_if_config_says_public(tmp_path):
    out = _doctor()._rulebook_state(_cfg("public"), tmp_path, run=_Run("true\n"))
    assert WARN not in out


def test_unreadable_falls_back_to_config_and_says_so(tmp_path):
    d = _doctor()
    for reply in ("", "garbage", RuntimeError("boom"), d._RawFailure("no gh")):
        out = d._rulebook_state(_cfg("public"), tmp_path, run=_Run(reply))
        assert WARN in out and "configured value" in out
        out = d._rulebook_state(_cfg("private"), tmp_path, run=_Run(reply))
        assert WARN not in out and "configured value" in out


def test_no_run_means_no_read_and_unchanged_text(tmp_path):
    d = _doctor()
    out = d._rulebook_state(_cfg("private"), tmp_path)
    assert WARN not in out and "configured value" not in out


def test_records_off_makes_no_call(tmp_path):
    run = _Run("false\n")
    out = _doctor()._rulebook_state(_cfg("private", enabled=False), tmp_path, run=run)
    assert run.calls == [] and WARN not in out


def test_closed_gate_text_identical(tmp_path):
    d = _doctor()
    cfg = {"rulebook": {"enabled": False}}
    run = _Run("false\n")
    assert d._rulebook_state(cfg, tmp_path, run=run) == d._rulebook_state(cfg, tmp_path)
    assert run.calls == []
