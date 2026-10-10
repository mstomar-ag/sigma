"""#919: the drift walk parses git output defensively (non-ASCII digits, line separators inside a subject)."""
import upkeep_drift_support as S

SHA = "a" * 40


def test_non_ascii_digits_are_unknown_not_an_error():
    d = S.drift()
    out = SHA + "\t٣٤\tsubject (#1)\n"
    got = d.walk(lambda cwd, argv: out, ".", "main")
    assert (got.rows, got.reason) == ((), "git-failed"), got


def test_subject_with_unicode_line_separator_stays_one_row():
    d = S.drift()
    out = SHA + "\t1700000000\tone two (#2)\n"
    got = d.walk(lambda cwd, argv: out, ".", "main")
    assert (len(got.rows), got.reason) == (1, None), got
