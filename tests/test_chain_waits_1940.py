"""#1940: Chain reads three more dependency wordings, each an UNCONFIRMED wait (never an edge).

  slice-chain        "S2a, S2b and S2c must precede S2d", read only when EVERY code resolves through the
                     `<code> (#N)` map written in the same text; one unresolved code reads nothing.
  slice-on           "S4b on S4a (#N)": the number after the second code.
  waiting-on         "waiting on #N", COMMENTS only, from a trusted author (the trust set of #1902).
  gates              "#N gates #M", COMMENTS only; PROVISIONAL (owner to ratify): both ends are read as waits.
  reversed-waits-on  "the story #N waits on" at a clause end; not when a reference follows.

Documented limits (docs/chain.md): a slice code with no issue number, and a ruling named in words, read nothing.
Controls (each seen red, then restored byte-identical): the all-codes-resolve clause; the trusted-author gate on
waiting-on; the reference-follows guard on reversed-waits-on; each rule removed outright.
Fixture issue numbers are written as 'fixture issue 8' in prose; no network (a counting fake serves REST GETs)."""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_chain_waits_1902 as t19  # noqa: E402

cw = t19._cw()

MAP = "S2a (#12), S2b (#13), S2c (#14), S2d (#15). "


def f(text, comment=False):
    return cw.find(text, comment=comment)


# --- slice-chain ----------------------------------------------------------------------------------------------

def test_slice_chain_all_codes_resolve():
    got = f(MAP + "S2a, S2b, S2c must precede S2d.")
    assert got == [("12", "slice-chain"), ("13", "slice-chain"), ("14", "slice-chain")]


@pytest.mark.parametrize("sentence", [
    "S2a, S2b and S2c precede S2d.",
    "S2a, S2b, and S2c must land before S2d.",
    "S2a & S2b & S2c must be merged before S2d.",
])
def test_slice_chain_wordings(sentence):
    assert {r for r, form in f(MAP + sentence) if form == "slice-chain"} == {"12", "13", "14"}


def test_slice_chain_one_unresolved_code_reads_nothing():
    text = "S2a (#12), S2b (#13), S2d (#15). S2a, S2b, S2c must precede S2d."      # S2c has no number
    assert f(text) == []


def test_slice_chain_without_any_map_reads_nothing():
    assert f("S2a, S2b, S2c must precede S2d.") == []


def test_slice_chain_negated_reads_nothing():
    assert f(MAP + "S2a, S2b, S2c need not precede S2d.") == []


# --- slice-on -------------------------------------------------------------------------------------------------

def test_slice_on_reads_the_numbered_code():
    got = f("S4b on S4a (#31); S4a starts only once the port has landed.")
    assert ("31", "slice-on") in got


def test_slice_on_without_the_number_reads_nothing():
    assert f("S4b on S4a; S4a starts only once the port has landed.") == []


# --- waiting-on (comments only) -------------------------------------------------------------------------------

def test_waiting_on_in_a_comment():
    assert f("Still waiting on #21 before the paid run.", comment=True) == [("21", "waiting-on")]


def test_waiting_on_never_from_a_body():
    assert f("Still waiting on #21 before the paid run.") == []


def test_waiting_on_negated():
    assert f("We are not waiting on #21 any more.", comment=True) == []


# --- gates (comments only, provisional: both ends) ------------------------------------------------------------

def test_gates_reads_both_ends_in_a_comment():
    assert f("#21 gates #22.", comment=True) == [("21", "gates"), ("22", "gates")]


def test_gates_never_from_a_body():
    assert f("#21 gates #22.") == []


def test_gates_negated():
    assert f("#21 does not gate #22.", comment=True) == []


# --- reversed-waits-on ----------------------------------------------------------------------------------------

def test_reversed_waits_on_at_a_clause_end():
    assert f("This is what the story #23 waits on.") == [("23", "reversed-waits-on")]


def test_reversed_not_when_a_reference_follows():
    assert f("The story #23 waits on #24.") == []
    assert f("The story #23 waits on, per #24, the port.") == []


def test_reversed_negated():
    assert f("This is what the story #23 does not wait on.") == []


# --- trust, through collect (the same set as #1902) -----------------------------------------------------------

def _wait_forms(tmp_path, body, comments):
    issues = [t19._goal(1, body=body, comments=comments)] + \
             [t19._goal(n) for n in (21, 22, 23, 24)]
    _, p = t19._plan(tmp_path, "live", issues, [])
    return {(i["ref"], i["form"], i["by"]) for i in p["unconfirmed_waits"] if i["goal"] == 1}


def test_waiting_on_trusted_comment_is_an_unconfirmed_wait(tmp_path):
    got = _wait_forms(tmp_path, "Plain body.", [t19._c("waiting on #21")])
    assert (21, "waiting-on", "owner") in got


def test_waiting_on_untrusted_comment_ignored(tmp_path):
    got = _wait_forms(tmp_path, "Plain body.", [t19._c("waiting on #21", login="stranger", assoc="NONE")])
    assert got == set()


def test_gates_trusted_and_untrusted(tmp_path):
    ok = _wait_forms(tmp_path, "Plain body.", [t19._c("#21 gates #22")])
    assert {(21, "gates", "owner"), (22, "gates", "owner")} <= ok
    no = _wait_forms(tmp_path / "u", "Plain body.", [t19._c("#21 gates #22", login="stranger", assoc="NONE")])
    assert no == set()


def test_body_wording_waiting_on_is_not_read_from_the_body(tmp_path):
    assert _wait_forms(tmp_path, "waiting on #21", []) == set()


def test_forms_are_listed():
    assert {"slice-chain", "slice-on", "waiting-on", "gates", "reversed-waits-on"} <= set(cw.FORMS)


def test_phantom_blocker_control_a_fixture_number_in_prose_is_no_edge(tmp_path):
    """A fixture number named in docs-like prose stays harmless: no form matches, so nothing is read."""
    got = _wait_forms(tmp_path, "See fixture issue 8 for the background; it gates nothing here.", [])
    assert got == set()
