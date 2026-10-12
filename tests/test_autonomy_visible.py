"""Goal 1222: the learning state is visible in the readout, the doctor row and the question list."""
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOOP = ROOT / "skills" / "sigma-loop" / "scripts"
DOCTOR = ROOT / "skills" / "sigma-doctor" / "scripts"


def _mod(name, base=LOOP):
    p = base / (name + ".py")
    spec = importlib.util.spec_from_file_location("v1222_" + name, p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


A = _mod("autonomy")
DS = _mod("decision_store")
U = _mod("unpark")
D = _mod("doctor", DOCTOR)


def _cfg(enabled=True):
    au = {"enabled": enabled, "levels": {"l1_min_answers": 10, "l2_min_answers": 30, "l2_agreement": 0.9}}
    return {"decision_rubric": {"records": {"enabled": True}, "autonomy": au}}


def _answer(sd, cfg, n, qkind="needs_decision", area="repo"):
    for _ in range(n):
        DS.append(sd, {"how": "human", "qkind": qkind, "area": area, "choice": "a"}, cfg)


def _watch(sd, cfg, n, would="a", actual="a", qkind="needs_decision", area="repo"):
    for _ in range(n):
        A.record_watch(sd, qkind, area, would, actual, cfg)


def _doctor_lines(cfg, sd):
    return D._autonomy_state(cfg, sd).split("\n")[1:]


def test_readout_rows_carry_progress(tmp_path):
    cfg = _cfg()
    _answer(tmp_path, cfg, 12)
    _watch(tmp_path, cfg, 2)
    row = A.readout(tmp_path, cfg)["kinds"][0]
    assert (row["answers"], row["comparisons"], row["agreement"]) == (12, 2, 1.0)
    assert (row["next_level"], row["answers_needed"], row["comparisons_needed"], row["agreement_needed"]) == (2, 30, 30, 0.9)


def test_readout_below_level_one_needs_the_first_threshold(tmp_path):
    cfg = _cfg()
    _answer(tmp_path, cfg, 7)
    row = A.readout(tmp_path, cfg)["kinds"][0]
    assert (row["next_level"], row["answers_needed"]) == (1, 10)


def test_no_records_one_line(tmp_path):
    assert _doctor_lines(_cfg(), tmp_path) == ["      learning has not started, every question is asked"]


def test_below_level_one_wording(tmp_path):
    cfg = _cfg()
    _answer(tmp_path, cfg, 7)
    assert _doctor_lines(cfg, tmp_path) == ["      needs_decision / repo: 7 of 10 answers, still asking"]


def test_level_one_with_too_few_comparisons_wording(tmp_path):
    cfg = _cfg()
    _answer(tmp_path, cfg, 12)
    _watch(tmp_path, cfg, 2)
    assert _doctor_lines(cfg, tmp_path) == [
        "      needs_decision / repo: level 1, level 2 needs 30 answers (have 12), "
        "30 comparisons (have 2), 90% agreement (have 100%)"]


def test_capped_hard_stop_kind_wording(tmp_path):
    cfg = _cfg()
    qk = sorted(_mod("hard_stop").DEFAULT_HARDSTOP_KINDS)[0]
    _answer(tmp_path, cfg, 12, qkind=qk)
    assert _doctor_lines(cfg, tmp_path) == [
        "      %s / repo: hard-stop kind, never learns beyond level 2, always asked" % qk]
    assert A.learning_line(tmp_path, qk, cfg) == "%s: it never learns beyond level 2 and is always asked" % qk


def test_unpark_line_below_level_one_has_the_count(tmp_path):
    cfg = _cfg()
    _answer(tmp_path, cfg, 7)
    assert A.learning_line(tmp_path, "needs_decision", cfg) == (
        "the rulebook is still learning and will keep asking this kind (7 of 10 answers)")
    assert "(0 of 10 answers)" in A.learning_line(tmp_path, "other_kind", cfg)
    _answer(tmp_path, cfg, 3)
    assert A.learning_line(tmp_path, "needs_decision", cfg) is None


class _Src:
    def _swap_labels(self): pass
    def _repo_args(self): pass
    def _run(self): pass

    def _read_issue(self, number, fields):
        return {"number": number, "title": "T", "body": "", "state": "OPEN", "labels": [],
                "comments": [{"body": U.sources.PARK_COMMENT_PREFIX + "changes requested on the API shape"}]}


def test_brief_and_render_carry_the_line_only_when_open(tmp_path):
    cfg = _cfg()
    _answer(tmp_path, cfg, 7)
    full = dict(cfg, discovery={"source": "github", "github": {"repo": "acme/widget"}})
    on = U.brief(tmp_path, full, 5, source=_Src())
    assert on["learning"].endswith("(7 of 10 answers)")
    assert "  the rulebook is still learning" in U.render_brief(on)


def test_gate_closed_is_byte_identical(tmp_path):
    closed = _cfg(enabled=False)
    _answer(tmp_path, closed, 7)
    assert A.readout(tmp_path, closed) == {"enabled": False}
    assert D._autonomy_state(closed, tmp_path) == "off (default)"
    assert A.learning_line(tmp_path, "needs_decision", closed) is None
    full = dict(closed, discovery={"source": "github", "github": {"repo": "acme/widget"}})
    b = U.brief(tmp_path, full, 5, source=_Src())
    assert "learning" not in b
    assert "learning" not in U.render_brief(b)
