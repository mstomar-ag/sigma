import importlib.util
import json
import os
import pathlib
import sys

import pytest

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ledger = _mod("ledger")

ON = {"ledger": {"enabled": True, "actor": "dana"}, "journal": {"enabled": True}}
OFF = {"ledger": {"enabled": False, "actor": "dana"}}


def _sdlc(tmp_path, config):
    d = tmp_path / ".sdlc"
    (d / "state").mkdir(parents=True)
    (d / "config.json").write_text(json.dumps(config))
    (d / "state" / "STATE.md").write_text(
        "# Loop State\niteration: 0\nrun_iteration: 0\nlast_run: none\n")
    return d


def _local_events(d):
    """Every event in the JOURNAL dir, parsed, in filename order.

    `ledger.read_all(stream=EVENTS)` cannot be used for this: it reads `entries_dir(d, EVENTS)`
    (`.sdlc/ledger/events/`), which #2574/S1-G3 stops writing. `read_all` deliberately keeps its
    one-stream-one-directory shape -- the four core readers that need both dirs (doctor,
    phase_report, time_report, a downstream reader) union them at the CALL site, so this test
    helper does the same rather than reaching for a union that does not exist."""
    out = []
    for path in sorted(ledger.local_events_dir(d).glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


# --------------------------------------------------------------------- default off


def test_absent_ledger_block_is_a_noop(tmp_path):
    d = _sdlc(tmp_path, {})
    assert ledger.append(d, {}, "note", "g.md") is None
    assert not (d / "ledger").exists()


def test_disabled_writes_nothing(tmp_path):
    d = _sdlc(tmp_path, OFF)
    assert ledger.append(d, OFF, "done", "g.md") is None
    assert not ledger.entries_dir(d).exists()


def test_enabled_is_strict_true_not_truthy():
    assert ledger.enabled({"ledger": {"enabled": "yes"}}) is False
    assert ledger.enabled({"ledger": {"enabled": 1}}) is False
    assert ledger.enabled({"ledger": {"enabled": True}}) is True


# --------------------------------------------------------------------- append


def test_append_writes_one_json_line_per_entry(tmp_path):
    d = _sdlc(tmp_path, ON)
    first = ledger.append(d, ON, "claimed", "0001-a.md")
    second = ledger.append(d, ON, "done", "0001-a.md")
    path = ledger.entry_file(d, "dana")
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["kind"] == "claimed"
    inst = ledger._instance_token()          # <host>.<pid> (#540), not the bare pid
    assert first["id"] == f"dana:{inst}:1" and second["id"] == f"dana:{inst}:2"  # monotonic per author


def test_entry_carries_the_core_fields(tmp_path):
    d = _sdlc(tmp_path, ON)
    e = ledger.append(d, ON, "note", "0002-b.md", now=0)
    assert e["actor"] == "dana" and e["kind"] == "note" and e["goal"] == "0002-b.md"
    assert e["ts"] == "1970-01-01T00:00:00Z"


def test_optional_fields_are_written_only_when_set(tmp_path):
    d = _sdlc(tmp_path, ON)
    e = ledger.append(d, ON, "handoff", "0003-c.md", to="rae", issue=61,
                      priority="P1", why="needs a flag", area="", ref=None)
    assert e["to"] == "rae" and e["issue"] == 61 and e["priority"] == "P1"
    assert "area" not in e and "ref" not in e                       # empty/None are dropped


def test_unknown_kind_and_state_are_rejected(tmp_path):
    d = _sdlc(tmp_path, ON)
    with pytest.raises(ValueError):
        ledger.append(d, ON, "banana", "g.md")
    with pytest.raises(ValueError):
        ledger.append(d, ON, "ack", "g.md", state="maybe")


def test_safe_append_never_raises_and_never_writes_when_off(tmp_path, capsys):
    d = _sdlc(tmp_path, ON)
    assert ledger.safe_append(d, "banana", "g.md") is None          # bad kind, swallowed
    assert "non-fatal" in capsys.readouterr().err
    assert ledger.safe_append(tmp_path / "nope", "note", "g.md") is None   # no config.json


def test_safe_append_loads_config_itself(tmp_path):
    d = _sdlc(tmp_path, ON)
    assert ledger.safe_append(d, "note", "g.md")["actor"] == "dana"


def test_safe_append_swallows_a_raising_append(tmp_path, capsys, monkeypatch):
    """#139 capstone: the primitive-level guarantee every site-level `*_survives_a_raising_ledger_
    append` test across loop.py/work.py/slices.py/pipeline.py/decision_gate.py already assumes —
    even a raise from `append()` ITSELF (not just a bad kind/config) never escapes `safe_append`."""
    d = _sdlc(tmp_path, ON)

    def raiser(*a, **k):
        raise RuntimeError("append broke")
    monkeypatch.setattr(ledger, "append", raiser)
    assert ledger.safe_append(d, "note", "g.md") is None
    assert "non-fatal" in capsys.readouterr().err


# --------------------------------------------------------------------- actor


def test_configured_actor_wins():
    assert ledger.actor({"ledger": {"actor": "chen"}}) == "chen"


def test_actor_falls_back_to_the_authenticated_account():
    ledger.reset_actor_cache()
    assert ledger.actor({"ledger": {}}, run=lambda args: "gh-login") == "gh-login"
    ledger.reset_actor_cache()


def test_actor_survives_a_broken_gh(monkeypatch):
    ledger.reset_actor_cache()
    monkeypatch.setenv("USER", "shelluser")

    def boom(_args):
        raise OSError("gh not installed")

    assert ledger.actor({"ledger": {}}, run=boom) == "shelluser"
    ledger.reset_actor_cache()


def test_actor_name_cannot_escape_the_entries_directory(tmp_path):
    d = _sdlc(tmp_path, {"ledger": {"enabled": True, "actor": "../../etc/passwd"}})
    e = ledger.append(d, {"ledger": {"enabled": True, "actor": "../../etc/passwd"}}, "note", "g")
    written = ledger.entry_file(d, e["actor"])
    assert ledger.entries_dir(d).resolve() in written.resolve().parents


# --------------------------------------------------------------------- files_for (#488)


def test_files_for_finds_a_file_written_under_a_different_case(tmp_path):
    """#488: a case-insensitive filesystem (macOS/Windows) resolves two differently-cased
    `open()`/`exists()` calls to the SAME dirent, case-PRESERVED under whichever casing wrote it
    first — but `pathlib.Path.glob()`'s pattern match is a plain Python string compare that never
    consults the filesystem's own case sensitivity, so a later, differently-cased query used to
    miss a file that genuinely exists. Constructed directly (write one real file under one casing,
    query with another) rather than relying on the host filesystem's own case-folding behavior, so
    this is deterministic on both a case-insensitive dev machine and case-sensitive CI
    (`ubuntu-latest`/ext4) alike."""
    d = _sdlc(tmp_path, ON)
    entries = ledger.entries_dir(d)
    entries.mkdir(parents=True, exist_ok=True)
    on_disk = entries / "dana-111.jsonl"
    on_disk.write_text(json.dumps({"id": "dana:111:1", "ts": "2026-01-01T00:00:00Z",
                                    "actor": "dana", "kind": "note", "goal": "g"}) + "\n")
    assert ledger.files_for(entries, "Dana") == [on_disk]


def test_files_for_is_symmetric_the_other_casing_direction_also_works(tmp_path):
    """The direction a canonical-login-resolution design would leave broken: a file written under
    a HAND-TYPED casing, found by a later query using a DIFFERENT casing. Proves the fix is a
    genuine case-insensitive match, not a one-directional canonicalization toward some "preferred"
    casing."""
    d = _sdlc(tmp_path, ON)
    entries = ledger.entries_dir(d)
    entries.mkdir(parents=True, exist_ok=True)
    on_disk = entries / "Dana-222.jsonl"
    on_disk.write_text(json.dumps({"id": "Dana:222:1", "ts": "2026-01-01T00:00:00Z",
                                    "actor": "Dana", "kind": "note", "goal": "g"}) + "\n")
    assert ledger.files_for(entries, "dana") == [on_disk]


def test_files_for_still_never_matches_a_different_actor_as_a_prefix(tmp_path):
    """The pre-existing "team" vs "team-bot" guarantee (files_for()'s own docstring) must survive
    the switch from a targeted `{safe}-*.jsonl` glob to a full `*.jsonl` scan + case-insensitive
    compare — still an exact match post-lowercasing, never a prefix, in either case."""
    d = _sdlc(tmp_path, ON)
    entries = ledger.entries_dir(d)
    entries.mkdir(parents=True, exist_ok=True)
    (entries / "team-bot-333.jsonl").write_text("{}\n")
    (entries / "TEAM-BOT-444.jsonl").write_text("{}\n")
    assert ledger.files_for(entries, "team") == []
    assert ledger.files_for(entries, "TEAM") == []


def test_files_for_still_ignores_a_non_pid_suffix(tmp_path):
    """The digit-pid guard (`pid.isdigit()`) must still exclude a same-named file whose suffix
    isn't a process id — unaffected by the case-insensitivity change, pinned now that the glob
    pattern itself widened from a targeted `{safe}-*.jsonl` to a full `*.jsonl` scan."""
    d = _sdlc(tmp_path, ON)
    entries = ledger.entries_dir(d)
    entries.mkdir(parents=True, exist_ok=True)
    (entries / "Dana-notapid.jsonl").write_text("{}\n")
    assert ledger.files_for(entries, "dana") == []


def test_files_for_still_finds_the_legacy_bare_file(tmp_path):
    """Pre-#337 bare `<who>.jsonl` compat path — left exact-case on purpose (see `files_for()`'s
    docstring: `.exists()` already resolves case-insensitively at the OS level on the filesystems
    this bug concerns, so there is nothing here for #488 to fix)."""
    d = _sdlc(tmp_path, ON)
    entries = ledger.entries_dir(d)
    entries.mkdir(parents=True, exist_ok=True)
    legacy = entries / "dana.jsonl"
    legacy.write_text("{}\n")
    assert ledger.files_for(entries, "dana") == [legacy]


def test_files_for_on_a_missing_directory_is_empty():
    assert ledger.files_for(pathlib.Path("/nonexistent/nope-488"), "dana") == []


# --------------------------------------------------------------------- read


def test_read_all_unions_every_author_oldest_first(tmp_path):
    d = _sdlc(tmp_path, ON)
    ledger.entries_dir(d).mkdir(parents=True, exist_ok=True)
    ledger.entry_file(d, "amy").write_text(
        json.dumps({"id": "amy:1", "ts": "2026-01-02T00:00:00Z", "actor": "amy",
                    "kind": "done", "goal": "a"}) + "\n")
    ledger.entry_file(d, "bo").write_text(
        json.dumps({"id": "bo:1", "ts": "2026-01-01T00:00:00Z", "actor": "bo",
                    "kind": "done", "goal": "b"}) + "\n")
    assert [e["actor"] for e in ledger.read_all(d)] == ["bo", "amy"]


def test_read_all_skips_malformed_lines_instead_of_failing(tmp_path):
    d = _sdlc(tmp_path, ON)
    ledger.append(d, ON, "note", "good.md")
    with ledger.entry_file(d, "dana").open("a") as f:
        f.write("{not json\n\n[]\n" + json.dumps({"no": "kind"}) + "\n")
    entries = ledger.read_all(d)
    assert len(entries) == 1 and entries[0]["goal"] == "good.md"


def test_read_all_on_a_repo_with_no_ledger(tmp_path):
    assert ledger.read_all(_sdlc(tmp_path, OFF)) == []


# ------------------------------------------------------ #175: read_all survives six malformed lines
# Each guard here is the same rule a downstream ledger reader uses; each of the six attacks
# below killed the ENTIRE team view (not one line) on origin/main, contradicting read_all's own
# "a malformed line is skipped, never fatal" docstring. Each test is mutation-proven: reverting the
# specific guard in ledger.read_all/_seq/_sort_key makes the anchoring test fail (see the plan).

_GOOD = ('{"id":"z:1","ts":"2026-01-01T00:00:00Z","actor":"z","kind":"note","goal":"good"}')


def _raw_entries(tmp_path, data):
    """Write raw bytes as the one *.jsonl file in an enabled ledger's entries dir, return sdlc dir.
    Bytes (not text) so a fixture can plant an invalid UTF-8 byte or a BOM the reader must survive."""
    d = _sdlc(tmp_path, ON)
    entries = ledger.entries_dir(d)
    entries.mkdir(parents=True, exist_ok=True)
    (entries / "a.jsonl").write_bytes(data)
    return d


def test_read_all_survives_an_invalid_utf8_byte(tmp_path):
    # Attack 1: a raw 0xff byte makes a plain-utf-8 read raise UnicodeDecodeError (a ValueError, not
    # OSError), aborting the whole read. utf-8-sig + errors="replace" degrades the byte, keeps going.
    d = _raw_entries(tmp_path, b'{"id":"z:2","kind":"note","actor":"z","ts":"t","goal":"x\xff"}\n'
                     + _GOOD.encode("utf-8") + b"\n")
    records = ledger.read_all(d)  # must not raise
    assert any(r.get("goal") == "good" for r in records)


def test_read_all_survives_a_deeply_nested_json_line(tmp_path, monkeypatch):
    # Attack 2: a deeply nested line raises RecursionError (a RuntimeError, NOT a ValueError), which
    # `except ValueError` alone lets propagate, discarding every record already read. Forced via
    # monkeypatch rather than a raw depth: whether a given depth raises is interpreter-dependent
    # (3.12's C parser parses depths 3.9 rejects), so a raw-depth fixture would be filtered by the
    # isinstance(dict) check on CI and pass even with RecursionError removed from the except clause
    # — mirroring a downstream reader's own recursion-error test.
    d = _raw_entries(tmp_path, b'{"deep":1}\n' + _GOOD.encode("utf-8") + b"\n")
    real = ledger.json.loads

    def loads(text, *a, **kw):
        if "deep" in text:
            raise RecursionError("maximum recursion depth exceeded")
        return real(text, *a, **kw)

    monkeypatch.setattr(ledger.json, "loads", loads)
    records = ledger.read_all(d)  # must not raise
    assert [r["id"] for r in records] == ["z:1"]


def test_read_all_survives_a_5000_digit_id_tail(tmp_path):
    # Attack 3: an id tail of 5000 digits makes int() raise ValueError on 3.9.14/3.10.7/3.11+ (the
    # CVE-2020-10735 digit limit), taking out the whole sort. Guarded in _seq. The crash itself is
    # interpreter-gated (py3.9.6 has no limit), so this asserts survival on every interpreter; when
    # the limit exists it additionally pins _seq degrading to 0 — the mutation anchor on CI.
    big = '{"id":"z:' + ("9" * 5000) + '","ts":"t","actor":"z","kind":"note","goal":"big"}'
    d = _raw_entries(tmp_path, _GOOD.encode("utf-8") + b"\n" + big.encode("utf-8") + b"\n")
    records = ledger.read_all(d)  # must not raise
    assert {r.get("goal") for r in records} == {"good", "big"}
    if hasattr(sys, "set_int_max_str_digits"):
        assert ledger._seq({"id": "z:" + ("9" * 5000)}) == 0


def test_seq_degrades_on_a_superscript_id_tail():
    # Attack 4: "²".isdigit() is True but int("²") raises ValueError on EVERY interpreter — the
    # deterministic mutation anchor for _seq's guard. Reverting `try/except ValueError` here raises.
    assert "\u00b2".isdigit() and ledger._seq({"id": "z:\u00b2"}) == 0


def test_read_all_survives_mixed_type_ts(tmp_path):
    # Attack 5: two records whose `ts` disagree on TYPE (str vs int) make the raw (ts, actor, seq)
    # sort tuple raise TypeError, fatal for the whole sort. _sort_key coerces through str().
    mixed = '{"id":"z:2","ts":123,"actor":"z","kind":"note","goal":"num"}'
    d = _raw_entries(tmp_path, _GOOD.encode("utf-8") + b"\n" + mixed.encode("utf-8") + b"\n")
    records = ledger.read_all(d)  # must not raise
    assert {r.get("goal") for r in records} == {"good", "num"}


def test_read_all_bom_on_first_line_is_not_eaten(tmp_path):
    # Attack 6 (the quiet one — a lost record, not a crash): a UTF-8 BOM prefixes line 1, and a
    # plain-utf-8 read leaves \ufeff on that line so json.loads rejects it and the record vanishes.
    # utf-8-sig strips the BOM, so BOTH records are returned.
    second = '{"id":"z:2","ts":"2026-01-02T00:00:00Z","actor":"z","kind":"note","goal":"second"}'
    d = _raw_entries(tmp_path, b"\xef\xbb\xbf" + (_GOOD + "\n" + second + "\n").encode("utf-8"))
    records = ledger.read_all(d)
    assert {r.get("goal") for r in records} == {"good", "second"}


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root reads mode-000 dirs, so the unreadable-directory case cannot arise")
def test_read_all_survives_an_unreadable_directory(tmp_path):
    # Directory-walk guard: Path.exists() swallows only ENOENT/ENOTDIR/EBADF/ELOOP, so a directory
    # the process cannot stat still escaped the old `if not base.exists()` gate. Chmod the PARENT
    # `ledger/` dir (NOT the leaf `entries/`: py3.9's glob swallows the leaf's own scandir error and
    # returns [] even unpatched, proving nothing — the reference chmods the parent, which raises out
    # of the unpatched read_all). The try/except OSError around glob() makes it degrade to [].
    d = _raw_entries(tmp_path, _GOOD.encode("utf-8") + b"\n")
    ledger_root = ledger.ledger_dir(d)
    os.chmod(ledger_root, 0o000)
    try:
        assert ledger.read_all(d) == []  # must not raise
    finally:
        os.chmod(ledger_root, 0o755)


def test_team_view_keeps_claims_addressed_and_outcomes(tmp_path):
    entries = [
        {"kind": "claimed", "goal": "a"},
        {"kind": "note", "goal": "b"},
        {"kind": "note", "goal": "c", "to": "rae"},
        {"kind": "parked", "goal": "d"},
    ]
    kinds = [(e["kind"], e.get("to")) for e in ledger.team(entries)]
    # `claimed` is shared so the team view records who started a ticket (pairs with `done` for
    # start→finish); a plain `note` stays local unless it is addressed to someone.
    assert kinds == [("claimed", None), ("note", "rae"), ("parked", None)]


def test_addressed_to_filters_by_recipient():
    entries = [{"kind": "handoff", "to": "rae"}, {"kind": "handoff", "to": "amy"}]
    assert ledger.addressed_to(entries, "rae") == [entries[0]]


def test_outstanding_closes_on_a_terminal_ack_only():
    handoff = {"kind": "handoff", "issue": 7, "goal": "g", "to": "rae"}
    assert ledger.outstanding([handoff]) == [handoff]
    assert ledger.outstanding([handoff, {"kind": "ack", "issue": 7, "state": "deferred"}]) == [handoff]
    assert ledger.outstanding([handoff, {"kind": "ack", "issue": 7, "state": "resolved"}]) == []
    assert ledger.outstanding([handoff, {"kind": "ack", "issue": 7, "state": "declined"}]) == []


def test_counts_tallies_by_kind():
    assert ledger.counts([{"kind": "done"}, {"kind": "done"}, {"kind": "parked"}])["done"] == 2


# --------------------------------------------------------------------- render


def test_render_lists_an_open_handoff_and_says_so_when_there_is_none():
    empty = ledger.render([])
    assert "Nothing is blocked on another person" in empty and "No entries yet" in empty
    out = ledger.render([{"ts": "2026-07-25T09:00:00Z", "actor": "amy", "kind": "handoff",
                          "goal": "g", "to": "rae", "issue": 61, "priority": "P1",
                          "why": "needs a flag"}])
    assert "rae" in out and "61" in out and "needs a flag" in out


def test_render_escapes_a_pipe_so_the_table_survives():
    out = ledger.render([{"ts": "t", "actor": "a", "kind": "note", "goal": "g",
                          "to": "b", "why": "one | two"}])
    assert "one \\| two" in out


def test_render_escapes_a_pipe_in_to_priority_and_issue_so_the_row_stays_well_formed():
    """F19: only `why`/`goal` used to go through `_cell()` -- a `|` in `to`/`priority`/`issue`
    (all free CLI text, no enum) split the row into extra columns instead of landing inside one
    cell. Repro is the issue's own: `handoff.py open ... --to "rae | INJECT ## header"`."""
    out = ledger.render([{"ts": "t", "actor": "amy", "kind": "handoff", "goal": "g",
                          "to": "rae | INJECT ## header", "priority": "P1 | X",
                          "issue": "61 | Y", "why": "w"}])
    section = out.split("## Recent activity")[0]          # the "Waiting on someone" table only
    row = next(line for line in section.splitlines() if line.startswith("| t |"))
    # An escaped `\|` still contains the character "|", so count DELIMITERS -- pipes not
    # preceded by the escaping backslash -- not raw occurrences of "|".
    assert row.replace("\\|", "").count("|") == 8         # 7 columns -> 8 delimiters, none smuggled in
    assert "rae \\| INJECT ## header" in row
    assert "P1 \\| X" in row
    assert "61 \\| Y" in row


def test_render_neutralizes_a_newline_in_to_so_it_cannot_inject_a_markdown_line():
    """The other half of F19: an unescaped newline in a free-text field lands in TEAM.md
    verbatim, letting a hand-off value inject its own markdown line (e.g. a fake heading) into a
    file the whole team reads and nobody hand-edits -- `_cell()` flattens it into the one cell
    instead."""
    out = ledger.render([{"ts": "t", "actor": "amy", "kind": "handoff", "goal": "g",
                          "to": "rae\n## INJECTED HEADER", "why": "w"}])
    assert not any(line.startswith("## INJECTED") for line in out.splitlines())  # no line of its own
    assert "rae ## INJECTED HEADER" in out                 # flattened into the one cell instead


def test_render_neutralizes_a_bare_carriage_return_in_to_so_it_cannot_inject_a_markdown_line():
    """#454: independent review of PR #449 (watch_classify.py's sibling `_cell()`, #427) found
    that ledger.py's ORIGINAL `_cell()` -- the one #449's copy was duplicated from -- only ever
    replaced a literal `\\n`, so a bare `\\r` sails through untouched and reopens the identical
    F19/#346 injected-heading symptom via a one-character delimiter swap in the payload:
    CommonMark -- and Python's own `str.splitlines()`, used here to reveal it, matching
    test_watch.py's own #427 `\\r` regression test -- treats a bare CR as a line terminator
    identical to LF. Same repro as the `\\n` test above with the delimiter swapped for `\\r`;
    must be neutralized exactly the same way."""
    out = ledger.render([{"ts": "t", "actor": "amy", "kind": "handoff", "goal": "g",
                          "to": "rae\r## INJECTED HEADER", "why": "w"}])
    assert not any(line.startswith("## INJECTED") for line in out.splitlines())  # no line of its own
    assert "rae ## INJECTED HEADER" in out                 # flattened into the one cell instead


def test_render_escapes_a_pipe_in_actor_and_kind_in_the_activity_table():
    """Same F19 gap in the second table -- `actor`/`kind` reached the row unescaped."""
    out = ledger.render([{"ts": "t", "actor": "amy | INJECT", "kind": "note | X", "goal": "g",
                          "to": "someone", "why": "w"}])
    row = next(line for line in out.splitlines() if line.startswith("| t |"))
    assert row.replace("\\|", "").count("|") == 6          # 5 columns -> 6 delimiters, none smuggled in
    assert "amy \\| INJECT" in row
    assert "note \\| X" in row


# --------------------------------------------------------------------- CLI


def test_cli_append_prints_the_entry_id(tmp_path, capsys):
    d = _sdlc(tmp_path, ON)
    assert ledger.main(["ledger.py", "append", str(d), "handoff", "g.md",
                        "--to", "rae", "--issue", "61", "--priority", "P0"]) == 0
    assert capsys.readouterr().out.strip() == f"dana:{ledger._instance_token()}:1"
    assert json.loads(ledger.entry_file(d, "dana").read_text())["issue"] == 61   # coerced to int


def test_cli_append_reports_when_the_ledger_is_off(tmp_path, capsys):
    d = _sdlc(tmp_path, OFF)
    assert ledger.main(["ledger.py", "append", str(d), "note", "g.md"]) == 0
    assert "OFF" in capsys.readouterr().out


def test_cli_append_rejects_a_bad_kind(tmp_path, capsys):
    d = _sdlc(tmp_path, ON)
    assert ledger.main(["ledger.py", "append", str(d), "banana", "g.md"]) == 2
    assert "unknown ledger kind" in capsys.readouterr().err


def test_cli_render_writes_team_md(tmp_path, capsys):
    d = _sdlc(tmp_path, ON)
    ledger.append(d, ON, "done", "g.md")
    assert ledger.main(["ledger.py", "render", str(d)]) == 0
    assert "# Team ledger" in capsys.readouterr().out
    assert ledger.main(["ledger.py", "render", str(d), "--write"]) == 0
    assert (ledger.ledger_dir(d) / "TEAM.md").read_text().startswith("# Team ledger")


def test_cli_mine_and_summary(tmp_path, capsys):
    d = _sdlc(tmp_path, ON)
    ledger.append(d, ON, "handoff", "g.md", to="rae", issue=61, why="needs a flag")
    assert ledger.main(["ledger.py", "mine", str(d), "--actor", "rae"]) == 0
    assert "needs a flag" in capsys.readouterr().out
    assert ledger.main(["ledger.py", "mine", str(d), "--actor", "nobody"]) == 0
    assert "nothing addressed to nobody" in capsys.readouterr().out
    assert ledger.main(["ledger.py", "summary", str(d)]) == 0
    out = capsys.readouterr().out
    assert "1 entries" in out and "outstanding hand-offs: 1" in out


def test_cli_usage(capsys):
    assert ledger.main(["ledger.py"]) == 2
    assert "usage: ledger.py" in capsys.readouterr().err


def test_flag_parser_handles_a_bare_switch():
    assert ledger._flags(["--write"]) == {"write": "true"}
    assert ledger._flags(["--to", "rae", "--write"]) == {"to": "rae", "write": "true"}


# --------------------------------------------------------------------- #541: _flags swallowed a
# value that itself starts with '--' -- the OLD heuristic ("consume the next token unless IT also
# starts with --") could never distinguish "no value was given" from "the value happens to start
# with a flag-like dash". `--why "--the CLI is missing a --verbose flag"` parsed to
# `{"why": "true", "the CLI is missing a --verbose flag": "true"}`: the real reason was replaced by
# the literal string "true", and its own text leaked in as a second, nonsense flag key. Fixed by (1)
# unconditionally consuming the next token for every known value-taking flag of every CLI that uses
# this parser -- `OPTIONAL_FIELDS` plus `actor` for ledger's own verbs, AND handoff.py's vocabulary,
# since handoff's open/track/ack all call this same function (see `_VALUE_FLAGS`' own note) --
# regardless of the value's shape, (2) `--name=value` syntax for ANY flag (unambiguous by
# construction), and (3) never keeping a whitespace-bearing "flag name" as a key, in EITHER form --
# that shape is always leaked prose from an unconsumed value, never a flag a caller meant to pass.


def test_flags_consumes_a_why_value_that_starts_with_a_double_dash_the_issues_own_repro():
    assert ledger._flags(["--why", "--the CLI is missing a --verbose flag"]) == {
        "why": "--the CLI is missing a --verbose flag"}


def test_flags_consumes_other_known_ledger_fields_that_start_with_a_double_dash():
    # OPTIONAL_FIELDS beyond `why` get the same unconditional-consume treatment -- not a special
    # case wired for one field name.
    assert ledger._flags(["--ref", "--looks-like-a-flag-but-isnt"]) == {
        "ref": "--looks-like-a-flag-but-isnt"}
    assert ledger._flags(["--area", "--engine"]) == {"area": "--engine"}


def test_flags_supports_name_equals_value_syntax_for_any_flag():
    # unambiguous by construction -- works even for a flag NOT in this module's known set.
    assert ledger._flags(["--why=--the CLI is missing a --verbose flag"]) == {
        "why": "--the CLI is missing a --verbose flag"}
    assert ledger._flags(["--totally-unregistered=--still works"]) == {
        "totally-unregistered": "--still works"}


def test_flags_never_keeps_a_whitespace_bearing_leaked_key():
    # direct pin of the "reject unknown keys" half of the fix: a token whose post-'--' text
    # contains a space is never a real flag name (no legitimate caller ever passes one) -- it is
    # always leaked prose from an unconsumed value, so it is dropped, not kept as a nonsense key.
    assert ledger._flags(["--this looks like leaked prose, not a flag"]) == {}


def test_flags_drops_the_leaked_key_end_to_end_for_an_unregistered_flag():
    # the full pre-fix cascade shape, for a flag name NOT in the known set (so the unconditional-
    # consume path does not apply and the old lookahead heuristic still runs first): the unknown
    # flag itself still lands on the "true" sentinel (unchanged, since nothing marks it as
    # value-taking), but the garbage second key that used to appear is gone.
    out = ledger._flags(["--notaknownflag", "--this text used to leak as a fake key"])
    assert out == {"notaknownflag": "true"}


def test_flags_drops_a_whitespace_bearing_key_in_the_eq_form_too():
    """#541 cycle 2: the `--name=value` branch bypassed the never-a-real-flag rule its
    space-separated sibling applies, so leaked prose that happened to contain '=' still landed
    as a whitespace-bearing key. Same shape in all four `_flags` copies, pinned in each."""
    assert ledger._flags(["--zzunknown", "--a b=c d"]) == {"zzunknown": "true"}


def test_flags_backward_compatible_for_unregistered_flags_and_normal_values():
    # nothing about the fix changes parsing for a flag outside the known set whose value does NOT
    # start with '--', or for a bare unknown flag followed by another flag -- both match the
    # pre-#541 heuristic exactly.
    assert ledger._flags(["--foo", "bar baz"]) == {"foo": "bar baz"}
    assert ledger._flags(["--foo", "--bar"]) == {"foo": "true", "bar": "true"}


# --------------------------------------------------------------------- loop integration


def test_loop_records_claim_and_outcome_when_enabled(tmp_path):
    loop = _mod("loop")
    d = _sdlc(tmp_path, ON)
    (d / "goals").mkdir()
    goal = d / "goals" / "0001-a.md"
    goal.write_text("---\nstatus: pending\n---\nbody\n")

    class Source:
        def next_pending(self, skip=()):
            return None if str(goal) in {str(s) for s in skip} else str(goal)

        def mark_in_progress(self, g):
            pass

        def complete(self, g):
            pass

        def park(self, g, reason):
            pass

    loop._next(str(d), Source(), {"ledger": ON["ledger"]})
    loop._record(str(d), Source(), str(goal), "done")
    kinds = [e["kind"] for e in ledger.read_all(d)]
    assert kinds == ["claimed", "done"]


def test_loop_writes_no_ledger_when_disabled(tmp_path):
    loop = _mod("loop")
    d = _sdlc(tmp_path, OFF)

    class Source:
        def park(self, g, reason):
            pass

    loop._record(str(d), Source(), "0001-a.md", "parked", "blocked on a decision")
    assert ledger.read_all(d) == []


def test_loop_maps_any_non_terminal_result_onto_parked(tmp_path):
    loop = _mod("loop")
    d = _sdlc(tmp_path, ON)

    class Source:
        def park(self, g, reason):
            pass

    loop._record(str(d), Source(), "0001-a.md", "whatever", "some reason")
    entry = ledger.read_all(d)[0]
    assert entry["kind"] == "parked" and entry["why"] == "some reason"


def test_loop_release_writes_a_release_ledger_entry_carrying_the_reason(tmp_path):
    """#841: `loop.py release` (the sanctioned claim-release verb) writes the `release` kind —
    declared in ledger.KINDS from the start but never written until this verb existed."""
    loop = _mod("loop")
    d = _sdlc(tmp_path, ON)

    class Source:
        def release(self, g, reason):
            pass

    loop._release(str(d), Source(), "0001-a.md", "skipped this run")
    entry = ledger.read_all(d)[0]
    assert entry["kind"] == "release" and entry["why"] == "skipped this run"


def test_loop_release_writes_no_ledger_when_disabled(tmp_path):
    loop = _mod("loop")
    d = _sdlc(tmp_path, OFF)

    class Source:
        def release(self, g, reason):
            pass

    loop._release(str(d), Source(), "0001-a.md", "skipped this run")
    assert ledger.read_all(d) == []


def test_loop_release_never_advances_the_iteration_cursor(tmp_path):
    """A release is not a completed iteration — the goal was never actually worked — so it must
    never consume a slot of `budget.max_iterations`, unlike `_record`'s own done/parked/failed."""
    loop = _mod("loop")
    state = _mod("state")
    d = _sdlc(tmp_path, ON)

    class Source:
        def release(self, g, reason):
            pass

    before = state.load_cursor(d)["iteration"]
    loop._release(str(d), Source(), "0001-a.md", "skipped this run")
    after = state.load_cursor(d)["iteration"]
    assert after == before


# --------------------------------------------------------------------- claim lease


def _e(actor, seq, kind, goal, ts="2026-07-27T09:00:00Z", id=None, **extra):
    entry = {"id": id or f"{actor}:{seq}", "ts": ts, "actor": actor, "kind": kind, "goal": str(goal)}
    entry.update(extra)
    return entry


def test_open_claims_holds_a_claimed_goal_and_names_the_holder():
    assert ledger.open_claims([_e("amy", 1, "claimed", "42")]) == {"42": "amy"}


def test_a_terminal_outcome_releases_the_claim():
    for outcome in ("done", "parked", "failed"):
        entries = [_e("amy", 1, "claimed", "42"), _e("amy", 2, outcome, "42")]
        assert ledger.open_claims(entries) == {}, outcome


def test_a_reclaim_after_a_failure_reopens_under_the_new_holder():
    entries = [_e("amy", 1, "claimed", "42"), _e("amy", 2, "failed", "42"),
               _e("bo", 1, "claimed", "42")]                       # bo retried it
    assert ledger.open_claims(entries) == {"42": "bo"}


def test_a_release_also_ends_the_open_claim():
    """#841: `release` is not a terminal SDLC outcome — unlike done/parked/failed, the goal
    itself stays exactly as pending as it was before it was ever claimed — but it must still end
    the LEASE the same way they do. Without this, a goal one actor released back to the pool
    would stay unpickable by a DIFFERENT actor (claim_belongs_to_me() is unconditionally False for
    another actor, regardless of writer liveness) until the lease's own TTL finally expired —
    `release` KIND was declared in ledger.KINDS from the start for exactly this, but nothing ever
    wrote it until #841's `loop.py release` verb."""
    entries = [_e("amy", 1, "claimed", "42"), _e("amy", 2, "release", "42")]
    assert ledger.open_claims(entries) == {}
    assert ledger.open_claims_detailed(entries) == {}


def test_a_reclaim_after_a_release_reopens_under_the_new_holder():
    """POST-REVIEW FIX: the original version of this test only asserted on the state AFTER bo's
    trailing `claimed` entry — which the `claimed` branch overwrites unconditionally regardless of
    what release actually did, so it passed even with a NO-OP release (verified: applying only the
    test diff onto pre-#841 source, before `_held()` learned about the `release` kind at all, this
    assertion alone still passed). Asserting the INTERMEDIATE state right after the release --
    before bo's claim exists at all -- makes this genuinely non-vacuous: if release did not end
    amy's lease, this first assertion fails on its own."""
    entries = [_e("amy", 1, "claimed", "42"), _e("amy", 2, "release", "42")]
    assert ledger.open_claims(entries) == {}                       # released first, lease actually ends
    entries = entries + [_e("bo", 1, "claimed", "42")]              # then bo claims it
    assert ledger.open_claims(entries) == {"42": "bo"}              # reopens under the new holder


def test_a_stale_release_cannot_wipe_a_legitimate_reclaim_by_a_different_actor():
    """PR #1107 review, Finding 2: `_held()` used to pop `held[goal]` on any terminal-kind entry
    (done/parked/failed/release) purely by GOAL, with no check of which actor/claim it corresponds
    to. Reproduced exact scenario: amy claims goal 42, bo legitimately re-claims it (amy's own
    claim went stale/dead), and THEN amy's own delayed/stale `release` call physically lands AFTER
    bo's claim in the sorted ledger (entries are oldest-first by construction, but a slow/queued
    caller can still land its write behind a newer one). Pre-fix this made `open_claims()` return
    `{}` — bo's live, currently-open claim silently vanished from the shared view, even though bo
    is actively working the goal, opening the door for a THIRD actor to legitimately re-pick the
    same goal bo already holds. The fix: a terminal entry only ends the lease it is CURRENTLY
    attributed to -- amy's `release` is stale relative to the CURRENT holder (bo), so it must
    leave bo's claim untouched."""
    entries = [_e("amy", 1, "claimed", "42"), _e("bo", 1, "claimed", "42"),
               _e("amy", 2, "release", "42")]                      # amy's stale release lands last
    assert ledger.open_claims(entries) == {"42": "bo"}              # bo's live claim survives intact
    assert ledger.open_claims_detailed(entries) == {"42": ("bo", "bo")}


def test_a_stale_terminal_outcome_from_a_former_holder_cannot_wipe_a_legitimate_reclaim_either():
    """The same actor-scoping bug applies uniformly to done/parked/failed, not just release -- the
    underlying `_held()` pop logic is shared code, so a stale done/parked/failed from an actor who
    no longer holds the claim is the identical hazard, just a narrower practical window (see
    `_held()`'s own docstring for why no legitimate caller relies on the old cross-actor pop)."""
    for outcome in ("done", "parked", "failed"):
        entries = [_e("amy", 1, "claimed", "42"), _e("bo", 1, "claimed", "42"),
                   _e("amy", 2, outcome, "42")]
        assert ledger.open_claims(entries) == {"42": "bo"}, outcome


def test_a_reclaimed_actor_release_ends_that_actors_own_lease_even_from_a_different_writer():
    """PR #1235 review, Finding 1/3: `_auto_reclaim_stale_claims` (#1198) calls `_release()` on a
    claim a DIFFERENT actor holds -- the sweep's own process writes the release entry, so its
    `actor` field is honestly self-attributed to the sweep (bo), never forged to the original
    claimant (amy). The plain actor-scoped check alone (`current[0] == entry.get('actor')`) can
    never end amy's lease then -- this is exactly the bug: 'bo' releasing 'amy's stale claim must
    still durably clear it. `reclaimed_actor`, naming amy explicitly, is the fix."""
    entries = [_e("amy", 1, "claimed", "42"),
               _e("bo", 1, "release", "42", reclaimed_actor="amy")]
    assert ledger.open_claims(entries) == {}


def test_a_reclaimed_actor_release_does_not_wipe_a_different_actors_live_reclaim():
    """The #1107 property must survive the new field: if a THIRD actor (carol) legitimately
    re-claimed the goal before the (delayed) reclaim-release for amy's original stale claim
    lands, that release must never clear carol's live claim just because it happens to name amy
    as `reclaimed_actor` -- `current[0]` at that point in the ledger's own ordered replay is
    carol, not amy, so the pop must not fire."""
    entries = [_e("amy", 1, "claimed", "42"), _e("carol", 1, "claimed", "42"),
               _e("bo", 1, "release", "42", reclaimed_actor="amy")]   # amy's stale reclaim, delayed
    assert ledger.open_claims(entries) == {"42": "carol"}             # carol's live claim survives
    assert ledger.open_claims_detailed(entries) == {"42": ("carol", "carol")}


def test_a_reclaimed_actor_release_never_ends_a_different_currently_held_claim():
    """A `release` naming a `reclaimed_actor` that is NOT who currently holds the goal (e.g. a
    stray/malformed entry) is a clean no-op, the same as an ordinary actor-mismatched release --
    it must never clear a claim that goal is not actually about."""
    entries = [_e("carol", 1, "claimed", "42"),
               _e("bo", 1, "release", "42", reclaimed_actor="amy")]   # names amy; carol holds it
    assert ledger.open_claims(entries) == {"42": "carol"}


def test_two_concurrent_processes_of_the_same_actor_do_not_race_via_a_stale_terminal_entry():
    """#1121: #841/#1107's actor-scoped `_held()` guard closed the CROSS-actor stale-release race
    but is still coarser than the real hazard -- it scopes by `actor` alone, so it cannot tell
    apart two DIFFERENT, concurrent PROCESSES authenticated as the exact SAME nominal actor (e.g.
    two independent sigma sessions both running as one person's own `gh` login). Reproduced exactly like
    #1107's own repro, just with the two claims sharing one actor instead of two different ones:
    amy's SECOND process legitimately re-claims goal 42 under a DIFFERENT `run_id` (#498's
    `state.run_identity()`/`SIGMA_RUN_ID` -- the one per-process-tree identity that already
    exists for exactly this class of problem), and THEN amy's FIRST process's own delayed/stale
    `release` lands after it in the sorted ledger. Actor-only scoping cannot distinguish these two
    `amy`s and wipes the live reclaim; (actor, run_id) scoping can and must not."""
    entries = [_e("amy", 1, "claimed", "42", run_id="run-1"),
               _e("amy", 2, "claimed", "42", run_id="run-2"),    # amy's OWN second process re-claims
               _e("amy", 3, "release", "42", run_id="run-1")]    # amy's first (stale) process releases
    assert ledger.open_claims(entries) == {"42": "amy"}                  # run-2's live claim survives
    assert ledger.open_claims_detailed(entries) == {"42": ("amy", "amy")}


def test_two_concurrent_processes_of_the_same_actor_same_race_for_done_parked_failed_too():
    """The same (actor, run_id)-scoping applies uniformly to done/parked/failed, not just release
    -- mirrors `test_a_stale_terminal_outcome_from_a_former_holder_cannot_wipe_a_legitimate_reclaim_either`
    for the cross-actor case, one run_id per amy-process instead of two different actors."""
    for outcome in ("done", "parked", "failed"):
        entries = [_e("amy", 1, "claimed", "42", run_id="run-1"),
                   _e("amy", 2, "claimed", "42", run_id="run-2"),
                   _e("amy", 3, outcome, "42", run_id="run-1")]
        assert ledger.open_claims(entries) == {"42": "amy"}, outcome


def test_no_run_id_on_either_side_degrades_to_exactly_todays_actor_only_scoping():
    """Acceptance criterion: with no run identity present anywhere (SIGMA_RUN_ID unset, the
    overwhelming common case for any deployment that hasn't opted in), behavior must be BYTE-
    IDENTICAL to the pre-#1121 actor-only guard -- no regression for a caller that never sets it.
    Same entries as `test_a_release_also_ends_the_open_claim`, just re-asserted here as the
    explicit no-run_id-anywhere control."""
    entries = [_e("amy", 1, "claimed", "42"), _e("amy", 2, "release", "42")]
    assert ledger.open_claims(entries) == {}


def test_a_run_id_on_only_one_side_still_degrades_to_actor_only_scoping():
    """A legacy claim (written before any caller set SIGMA_RUN_ID, so no `run_id` field at
    all) followed by a terminal entry that DOES carry one (or the reverse) has nothing on the
    OTHER side to compare against -- degrade to actor-only rather than refuse a legitimate release
    just because only one of the two entries happens to be run-attributed."""
    entries = [_e("amy", 1, "claimed", "42"), _e("amy", 2, "release", "42", run_id="run-9")]
    assert ledger.open_claims(entries) == {}
    entries2 = [_e("amy", 1, "claimed", "42", run_id="run-9"), _e("amy", 2, "release", "42")]
    assert ledger.open_claims(entries2) == {}


def test_run_id_scoping_never_reopens_the_1235_cross_actor_reclaimed_actor_path():
    """The `reclaimed_actor` sweep path (#1235) is a DELIBERATE, sanctioned cross-actor -- and
    necessarily cross-run -- exception: an automated reclaim sweep's own `release` entry is always
    self-attributed to the SWEEP's actor/run, never the original claimant's, so scoping THIS path
    by run_id too would make it never fire again (reopening #1235). It must keep working exactly
    as before, regardless of what run_id the sweep entry itself happens to carry."""
    entries = [_e("amy", 1, "claimed", "42", run_id="run-1"),
               _e("bo", 1, "release", "42", reclaimed_actor="amy", run_id="sweep-run")]
    assert ledger.open_claims(entries) == {}


def test_a_stale_reclaimed_actor_release_does_not_wipe_the_same_actors_live_reclaim_by_another_run():
    """PR #1269 review, blocking finding 1: the `reclaimed_actor` branch above closes #1235's
    CROSS-actor race but was still scoped by actor alone, so it could not tell apart two
    DIFFERENT, concurrent PROCESSES of the exact SAME actor -- the identical same-actor race
    `test_two_concurrent_processes_of_the_same_actor_do_not_race_via_a_stale_terminal_entry`
    already proved fixed for the PRIMARY (non-`reclaimed_actor`) branch, reachable here through
    the auto-reclaim SWEEP's own path instead: `_auto_reclaim_stale_claims` (loop.py) snapshots
    `ledger.expired_claims()` -- a stale view -- then runs a live-worker check plus several `gh`
    calls before finally writing its `release(reclaimed_actor=...)` entry, a real window. If
    amy's OWN live process re-claims the goal under a NEW `run_id` inside that window (two
    concurrent sessions sharing one login -- 'not hypothetical' per the review), the sweep's
    eventual release must not wipe the live re-claim just because it names amy as
    `reclaimed_actor`.

    `reclaimed_run` is the fix: it names the STALE claim's own run_id explicitly -- the same
    'name the target, not the writer' pattern `reclaimed_actor` itself already uses -- and is
    compared against `current_run`, NEVER against the release entry's own `run_id` (that would
    reopen #1235; see `test_run_id_scoping_never_reopens_the_1235_cross_actor_reclaimed_actor_path`
    right above, which must keep passing unchanged: a release with no `reclaimed_run` at all
    degrades to the pre-existing actor-only answer for that path, same as `run_id` already does
    for the primary branch when either side lacks one)."""
    entries = [_e("amy", 1, "claimed", "42", run_id="run-1"),
               _e("amy", 2, "claimed", "42", run_id="run-2"),      # amy's own live re-claim, new run
               _e("sweep", 1, "release", "42", reclaimed_actor="amy", reclaimed_run="run-1",
                  run_id="sweep-run")]                              # sweep releases the STALE run only
    assert ledger.open_claims(entries) == {"42": "amy"}             # run-2's live claim survives
    assert ledger.open_claims_detailed(entries) == {"42": ("amy", "amy")}


def test_claims_are_tracked_per_goal_independently():
    entries = [_e("amy", 1, "claimed", "42"), _e("bo", 1, "claimed", "7"),
               _e("amy", 2, "done", "42")]
    assert ledger.open_claims(entries) == {"7": "bo"}             # 42 released, 7 still held


def test_a_claim_past_its_ttl_is_treated_as_released():
    entries = [_e("amy", 1, "claimed", "42", ts="2026-07-27T00:00:00Z")]
    now = ledger._epoch("2026-07-27T13:00:00Z")                   # 13h later
    assert ledger.open_claims(entries, now=now, ttl_seconds=12 * 3600) == {}       # expired
    assert ledger.open_claims(entries, now=now, ttl_seconds=24 * 3600) == {"42": "amy"}   # still fresh
    assert ledger.open_claims(entries, now=now, ttl_seconds=None) == {"42": "amy"}         # no expiry


def test_expired_claims_returns_only_the_claim_past_its_ttl():
    """#1198: the mirror of `open_claims`'s own `ttl_seconds` filtering -- that DROPS an aged-out
    claim from its result; this returns exactly what got dropped, so a caller (`loop.py`'s
    `_auto_reclaim_stale_claims`) can tell WHICH goal needs its GitHub `in_progress_label`
    stripped, not just that the ledger itself has quietly stopped counting it as held."""
    entries = [_e("amy", 1, "claimed", "42", ts="2026-07-27T00:00:00Z"),    # 13h old -> expired
               _e("bo", 1, "claimed", "7", ts="2026-07-27T12:30:00Z")]      # 30m old -> still fresh
    now = ledger._epoch("2026-07-27T13:00:00Z")
    assert ledger.expired_claims(entries, now=now, ttl_seconds=12 * 3600) == \
        {"42": ("amy", "amy", "2026-07-27T00:00:00Z")}


def test_expired_claims_is_empty_with_no_ttl_configured():
    """`ttl_seconds` falsy (config `0`/`false`, "never expire") -> nothing can ever be expired --
    matches `_held()`'s own no-op there, and must never divide by zero or otherwise misbehave."""
    entries = [_e("amy", 1, "claimed", "42", ts="2026-07-27T00:00:00Z")]
    now = ledger._epoch("2026-08-27T00:00:00Z")     # a month later -- would be expired at ANY real TTL
    assert ledger.expired_claims(entries, now=now, ttl_seconds=None) == {}
    assert ledger.expired_claims(entries, now=now, ttl_seconds=0) == {}


def test_expired_claims_excludes_a_claim_that_already_ended_terminally():
    """A claim that finished (done/parked/failed) or was explicitly released is not "expired" --
    it is simply no longer open at all, so there is nothing here for a reclaim sweep to do."""
    entries = [_e("amy", 1, "claimed", "42", ts="2026-07-27T00:00:00Z"),
               _e("amy", 2, "done", "42", ts="2026-07-27T00:05:00Z")]
    now = ledger._epoch("2026-07-27T13:00:00Z")
    assert ledger.expired_claims(entries, now=now, ttl_seconds=12 * 3600) == {}


# --------------------------------------------------------------------- writer identity (#374)
# Two of ONE actor's own concurrent processes are no longer indistinguishable to the claim lease
# -- the exact gap that let a routine's fresh invocation blindly resume another still-running
# session's in-flight worktree (see sigma-parallel-autoupdate-plan.md #374).


def test_writer_is_actor_pid_for_a_3_part_id_but_falls_back_to_bare_actor_for_legacy():
    assert ledger._writer(_e("dana", 1, "claimed", "42", id="dana:111:1")) == "dana:111"
    assert ledger._writer(_e("dana", 1, "claimed", "42")) == "dana"          # legacy 2-part id
    assert ledger._writer({"actor": "dana"}) == "dana"                      # id missing entirely


def test_my_writer_is_actor_colon_this_writer_instance():
    assert ledger.my_writer({"ledger": {"actor": "dana"}}) == f"dana:{ledger._instance_token()}"


def test_writer_pid_extracts_the_pid_or_none_for_a_legacy_writer():
    assert ledger.writer_pid("dana:12345") == 12345
    assert ledger.writer_pid("dana") is None
    assert ledger.writer_pid("") is None


def test_pid_alive_is_true_for_this_process_and_false_for_an_unlikely_pid():
    assert ledger.pid_alive(os.getpid()) is True
    assert ledger.pid_alive(2**30) is False          # not a real pid on any sane system


def test_open_claims_detailed_exposes_the_writer_alongside_the_actor():
    entries = [_e("dana", 1, "claimed", "42", id="dana:111:1")]
    assert ledger.open_claims_detailed(entries) == {"42": ("dana", "dana:111")}
    assert ledger.open_claims(entries) == {"42": "dana"}                    # sibling view unaffected


def test_open_claims_detailed_ttl_and_release_semantics_match_open_claims_exactly():
    """The writer-detailed view must not silently diverge from the actor-only one on anything
    OTHER than the writer field itself -- same TTL expiry, same terminal-outcome release."""
    entries = [_e("dana", 1, "claimed", "42", id="dana:111:1", ts="2026-07-27T00:00:00Z")]
    now = ledger._epoch("2026-07-27T13:00:00Z")
    assert ledger.open_claims_detailed(entries, now=now, ttl_seconds=12 * 3600) == {}
    assert ledger.open_claims_detailed(entries, now=now, ttl_seconds=24 * 3600) == {
        "42": ("dana", "dana:111")}
    released = entries + [_e("dana", 2, "done", "42", id="dana:111:2")]
    assert ledger.open_claims_detailed(released) == {}


def test_claim_belongs_to_me_is_false_for_a_different_actor_regardless_of_writer():
    assert ledger.claim_belongs_to_me("amy", "amy:111", "dana", "dana:222") is False


def test_claim_belongs_to_me_is_true_for_my_own_current_writer():
    assert ledger.claim_belongs_to_me("dana", "dana:222", "dana", "dana:222") is True


def test_claim_belongs_to_me_is_true_for_a_legacy_same_actor_claim_no_regression():
    """A pre-#337 2-part-id claim has no pid to distinguish -- there was only ever one writer file
    per actor then, so a same-actor legacy claim stays resumable exactly like before this fix."""
    assert ledger.claim_belongs_to_me("dana", "dana", "dana", "dana:222") is True


def test_claim_belongs_to_me_is_false_for_a_live_sibling_process_of_my_own_actor():
    """THE regression this issue exists to close: a different, still-running process of MY OWN
    actor holding the claim must not read as 'mine to resume' just because the actor matches."""
    live_sibling_writer = f"dana:{os.getpid()}"          # this test process is, definitionally, alive
    assert ledger.claim_belongs_to_me("dana", live_sibling_writer, "dana", "dana:999999") is False


def test_claim_belongs_to_me_is_true_for_a_dead_sibling_process_of_my_own_actor():
    """A crashed sibling's claim is still safely reclaimable -- liveness-checking must not turn
    into a NEW way to wedge a goal forever; that is what the existing TTL fallback already covers,
    and this must not regress it for the common single-loop-crashed case."""
    assert ledger.claim_belongs_to_me("dana", "dana:2147483647", "dana", "dana:999999") is True


# ---- #1197: the writer pid this function checks is ALWAYS the short-lived picker CLI invocation
# that wrote the claim -- it reads "dead" within moments of every acquisition, whether the goal is
# genuinely abandoned or being actively worked by a long-running subagent. `live_worker_check` is
# an optional, lazily-invoked escape hatch a caller can pass so a dead PICKER pid is never confused
# with a dead WORKER -- consulted only on the one path where that distinction actually matters.


def test_claim_belongs_to_me_is_false_for_a_dead_sibling_when_a_live_worker_check_says_alive():
    assert ledger.claim_belongs_to_me(
        "dana", "dana:2147483647", "dana", "dana:999999",
        live_worker_check=lambda: True) is False


def test_claim_belongs_to_me_still_reclaims_a_dead_sibling_when_the_live_worker_check_says_no():
    assert ledger.claim_belongs_to_me(
        "dana", "dana:2147483647", "dana", "dana:999999",
        live_worker_check=lambda: False) is True


def test_claim_belongs_to_me_omits_the_live_worker_check_by_default_no_regression():
    """Callers that pass nothing -- every pre-#1197 call site, and every test above -- get
    byte-identical behavior. The parameter is purely additive, never required."""
    assert ledger.claim_belongs_to_me("dana", "dana:2147483647", "dana", "dana:999999") is True


def test_claim_belongs_to_me_never_calls_the_live_worker_check_for_a_legacy_claim():
    """A legacy (no-pid) claim is decided before liveness ever enters the picture -- the acceptance
    bar for #1197 is that a pre-existing legacy claim behaves EXACTLY as it does today, so the
    check must not even be consulted for it."""
    def boom():
        raise AssertionError("live_worker_check must not be called for a legacy claim")
    assert ledger.claim_belongs_to_me(
        "dana", "dana", "dana", "dana:222", live_worker_check=boom) is True


def test_claim_belongs_to_me_never_calls_the_live_worker_check_for_my_own_current_writer():
    def boom():
        raise AssertionError("live_worker_check must not be called for my own current writer")
    assert ledger.claim_belongs_to_me(
        "dana", "dana:222", "dana", "dana:222", live_worker_check=boom) is True


def test_claim_belongs_to_me_never_calls_the_live_worker_check_for_a_live_sibling_pid():
    """Only consulted on the dead-picker-pid path -- a writer pid that is ITSELF still alive is
    already unambiguously "not mine", no extra corroboration needed or wanted."""
    def boom():
        raise AssertionError("live_worker_check must not be called when the writer pid is alive")
    live_sibling_writer = f"dana:{os.getpid()}"          # this test process is, definitionally, alive
    assert ledger.claim_belongs_to_me(
        "dana", live_sibling_writer, "dana", "dana:999999", live_worker_check=boom) is False


def test_handoff_states_and_unanswered_separate_stuck_from_in_progress():
    """`outstanding` alone cannot tell a hand-off nobody has looked at from one someone has taken —
    both are still blocking, but only the first needs chasing. Found by a two-clone e2e: the summary
    line read the same before and after the recipient accepted."""
    handoff_a = {"kind": "handoff", "issue": 61, "goal": "a", "to": "bo"}
    handoff_b = {"kind": "handoff", "issue": 62, "goal": "b", "to": "bo"}
    entries = [handoff_a, handoff_b, {"kind": "ack", "issue": 61, "state": "accepted"}]
    assert ledger.handoff_states(entries) == {"61": "accepted"}
    assert ledger.outstanding(entries) == [handoff_a, handoff_b]        # accepted is not resolved
    assert ledger.unanswered(entries) == [handoff_b]                    # only 62 is truly stuck


def test_handoff_key_falls_back_to_the_goal_without_an_issue():
    assert ledger.handoff_key({"issue": 7, "goal": "g"}) == "7"
    assert ledger.handoff_key({"goal": "g"}) == "g"                     # local backlog: no issues


def test_render_shows_the_reply_state_per_row():
    handoff = {"ts": "t", "actor": "amy", "kind": "handoff", "goal": "g", "to": "bo", "issue": 61}
    assert "**open — no reply**" in ledger.render([handoff])
    answered = ledger.render([handoff, {"kind": "ack", "issue": 61, "state": "accepted"}])
    assert "| accepted |" in answered and "no reply" not in answered


# --------------------------------------------------------------------- #533: area-qualified settlement
# `handoff_key` (issue-or-goal) is also what SETTLED an issue-less hand-off: a goal handed off to TWO
# areas filed two hand-offs that both fall back to the same bare goal, so one terminal ack for either
# one settled BOTH. `settlement_key()` below is the tolerant matcher's identity function that fixes
# this -- `handoff_key` itself is untouched (still the PAIRING/display key backlog_check.py's #532 fix
# reads for its finding `ref`; see its own docstring). The matching logic itself moved from a per-call
# scan (`_settlement_matches()`/`_handoff_settled()`) to an O(n) index (`_settled_index()`/
# `_ack_states_index()`) in #561 — a performance fix only, the settlement RULES below are unchanged.


def _issueless_handoff(goal, area, to="bob"):
    return {"kind": "handoff", "goal": goal, "area": area, "to": to}


def test_outstanding_area_qualifies_issueless_handoffs_so_one_ack_does_not_collapse_both():
    """The issue's own repro, pinned as a regression: a goal handed off to two areas used to collapse
    to zero outstanding on ANY one terminal ack, because handoff_key falls back to the bare goal for
    both. Settlement now reads (goal, area); acking one area leaves the other outstanding."""
    engine = _issueless_handoff("g", "engine")
    ui = _issueless_handoff("g", "ui")
    entries = [engine, ui, {"kind": "ack", "goal": "g", "area": "engine", "state": "resolved"}]
    assert [h["area"] for h in ledger.outstanding(entries)] == ["ui"]


def test_outstanding_area_less_ack_still_settles_every_area_on_the_goal():
    """The deliberate backward-compat fallback: an ack with NO area (the shape every ack this kit has
    ever WRITTEN takes -- no ack writer emits `area`) settles every outstanding hand-off on that goal,
    exactly like pre-#533 -- old ledger history replays to an identical settlement outcome."""
    engine = _issueless_handoff("g", "engine")
    ui = _issueless_handoff("g", "ui")
    entries = [engine, ui, {"kind": "ack", "goal": "g", "state": "resolved"}]        # no area
    assert ledger.outstanding(entries) == []


def test_outstanding_still_settles_only_the_matching_issue_in_github_mode():
    """#533 must not touch github-mode precision: two hand-offs with DISTINCT issues, a terminal ack
    for one, only that one settles -- extends test_outstanding_closes_on_a_terminal_ack_only (which
    only ever used a single issue) to the multi-issue shape the new matcher must also preserve."""
    a = {"kind": "handoff", "issue": 61, "goal": "g", "to": "bo"}
    b = {"kind": "handoff", "issue": 62, "goal": "g", "to": "bo"}
    entries = [a, b, {"kind": "ack", "issue": 61, "state": "resolved"}]
    assert ledger.outstanding(entries) == [b]


def test_handoff_states_and_unanswered_split_by_area_for_issueless_handoffs():
    """The display-layer half of #533: after acking just `engine`, handoff_states shows `engine`
    accepted while `ui` stays absent (open); unanswered() -- built on the same matcher -- still lists
    the ui hand-off as truly stuck, mirroring the existing issue-mode split test above."""
    engine = _issueless_handoff("g", "engine")
    ui = _issueless_handoff("g", "ui")
    entries = [engine, ui, {"kind": "ack", "goal": "g", "area": "engine", "state": "accepted"}]
    states = ledger.handoff_states(entries)
    assert states[ledger.settlement_key(engine)] == "accepted"
    assert ledger.settlement_key(ui) not in states
    assert ledger.unanswered(entries) == [ui]            # accepted isn't stuck; ui is still unanswered


def test_render_shows_the_reply_state_per_area_for_issueless_handoffs():
    """Render-layer regression pin, beside test_render_shows_the_reply_state_per_row above: pre-fix,
    render() looked up the display state by the bare-goal handoff_key, so acking one area's hand-off
    made BOTH rows show 'accepted' -- wrong for the row that is still genuinely open."""
    engine = {"ts": "t", "actor": "amy", "kind": "handoff", "goal": "g", "to": "bob", "area": "engine"}
    ui = {"ts": "t", "actor": "amy", "kind": "handoff", "goal": "g", "to": "cara", "area": "ui"}
    ack = {"kind": "ack", "goal": "g", "area": "engine", "state": "accepted"}
    out = ledger.render([engine, ui, ack])
    engine_row = next(line for line in out.splitlines() if "| bob |" in line)
    ui_row = next(line for line in out.splitlines() if "| cara |" in line)
    assert "| accepted |" in engine_row
    assert "**open — no reply**" in ui_row


# --------------------------------------------------------------------- #561: O(n) settlement matcher
# #533's matcher scanned the whole settled-set per hand-off (outstanding()) or ran an explicit
# hand-offs x acks nested loop (handoff_states()) -- O(hand-offs x acks), found by the #558 review's
# perf probe. Fixed by indexing the settled/ack-state set by goal ({goal: set(areas)}, with `None` as
# the area-less-ack sentinel) instead of scanning it, restoring O(n). These two tests pin the
# trickiest behavior an index-based rewrite could get wrong -- recency ordering between an
# area-SPECIFIC ack and an area-LESS (wildcard) one on the same goal -- and must stay green
# UNCHANGED across the rewrite: #533's own contract is "latest wins by read order" regardless of
# which of the two shapes that latest update came from.


def test_handoff_states_a_later_wildcard_ack_overrides_an_earlier_specific_one_for_every_area():
    engine = _issueless_handoff("g", "engine")
    ui = _issueless_handoff("g", "ui")
    entries = [engine, ui,
               {"kind": "ack", "goal": "g", "area": "engine", "state": "accepted"},   # earlier, specific
               {"kind": "ack", "goal": "g", "state": "resolved"}]                     # later, area-less
    states = ledger.handoff_states(entries)
    assert states[ledger.settlement_key(engine)] == "resolved"   # the later wildcard wins over engine too
    assert states[ledger.settlement_key(ui)] == "resolved"       # ui was only ever touched by the wildcard
    assert ledger.outstanding(entries) == []                     # both settled by the later resolved


def test_handoff_states_a_later_specific_ack_overrides_an_earlier_wildcard_for_that_area_only():
    engine = _issueless_handoff("g", "engine")
    ui = _issueless_handoff("g", "ui")
    entries = [engine, ui,
               {"kind": "ack", "goal": "g", "state": "resolved"},                        # earlier, area-less
               {"kind": "ack", "goal": "g", "area": "engine", "state": "accepted"}]       # later, specific
    states = ledger.handoff_states(entries)
    assert states[ledger.settlement_key(engine)] == "accepted"   # the later specific ack wins for engine
    assert states[ledger.settlement_key(ui)] == "resolved"       # ui still reflects the (only) wildcard
    # outstanding(): engine is settled by the EARLIER wildcard (a terminal "resolved" is enough to
    # settle regardless of a later non-terminal "accepted" arriving afterward -- outstanding() only
    # asks "was a terminal ack ever recorded for this key", it does not un-settle on a later reply);
    # ui is settled by that same wildcard. Both outstanding() == [] either way here since the only
    # terminal ack (the wildcard "resolved") covers both areas regardless of ordering.
    assert ledger.outstanding(entries) == []


# --------------------------------------------------------------------- #562: the ack None-guard


def test_outstanding_never_lets_a_fully_identity_less_ack_settle_a_fully_identity_less_handoff():
    """#562: HEAD-before-#533 explicitly skipped an ack carrying NEITHER issue nor goal
    (`entry.get("issue") or entry.get("goal")` bottoms out to `None`, guarded with `is not None`
    before ever reaching the settled set). #533's settlement_key()-based rewrite dropped that
    guard: for such an ack, settlement_key() manufactures `(str(None), None)` = `("None", None)`
    instead of refusing to produce a key at all, and #561's O(n) index rewrite carried the same
    hole forward into `_settled_index()`/`_ack_states_index()`. A hand-off that is ITSELF
    identity-less (no issue, no goal) then spuriously "settles" against that manufactured key.

    Both entries below are constructible ONLY by hand-editing the ledger's JSONL (or reading a
    corrupted/malicious one) — `handoff.py`'s `hand_off()`/`acknowledge()` always pass a real
    `goal` positionally and `append()` takes it as a required positional, so no code path in this
    kit can ever WRITE either shape through the normal API. Pinned anyway: `read_all()` does no
    schema validation, so a hand-edited or corrupted ledger file reaches `outstanding()`/
    `handoff_states()` unfiltered — exactly the shape the original guard existed to make harmless.

    The issue's own probe, reproduced verbatim: handoff{goal: None} + ack{state: resolved, no
    goal} -> pre-#533: outstanding=[handoff]; #533/#561 (pre-#562-fix): outstanding=[]."""
    handoff = {"kind": "handoff", "goal": None, "to": "bob"}
    ack = {"kind": "ack", "state": "resolved"}                      # no "goal" key at all
    assert ledger.outstanding([handoff, ack]) == [handoff]
    assert ledger.handoff_states([handoff, ack]) == {}


def test_settlement_matcher_scales_linearly_not_quadratically(monkeypatch):
    """#561 perf regression pin: a deterministic operation-count check, NOT wall-clock timing (which
    flakes in CI). Counts calls to `settlement_key()` (monkeypatched to a counting wrapper) as the
    work proxy while running `outstanding()` + `handoff_states()` over two fixtures, one 4x the
    other's hand-off/ack count. Under #533's nested-scan matcher this ratio was ~16x (quadratic);
    the O(n) index-based matcher should cost ~4x (linear) -- the tolerance below (6x) sits well
    between the two so it catches a reintroduced O(n^2) shape without being sensitive to small
    constant-factor noise."""
    def _fixture(n):
        entries = []
        for i in range(n):
            goal = f"g{i}"
            entries.append({"kind": "handoff", "goal": goal, "area": "engine", "to": "bob"})
            entries.append({"kind": "handoff", "goal": goal, "area": "ui", "to": "cara"})
            entries.append({"kind": "ack", "goal": goal, "area": "engine", "state": "resolved"})
        return entries

    def _settlement_key_calls(n):
        calls = [0]
        entries = _fixture(n)
        with monkeypatch.context() as m:
            real = ledger.settlement_key
            def counting(entry):
                calls[0] += 1
                return real(entry)
            m.setattr(ledger, "settlement_key", counting)
            ledger.outstanding(entries)
            ledger.handoff_states(entries)
        return calls[0]

    small = _settlement_key_calls(50)
    large = _settlement_key_calls(200)                     # 4x the hand-off/ack count
    assert large <= small * 6                              # linear (~4x) passes; quadratic (~16x) does not


def test_summary_line_calls_out_unanswered_handoffs(tmp_path, capsys):
    d = _sdlc(tmp_path, ON)
    ledger.append(d, ON, "handoff", "g.md", to="bo", issue=61)
    ledger.main(["ledger.py", "summary", str(d)])
    assert "1 with NO reply" in capsys.readouterr().out
    ledger.append(d, ON, "ack", "g.md", issue=61, state="accepted")
    ledger.main(["ledger.py", "summary", str(d)])
    assert "all answered" in capsys.readouterr().out


# --------------------------------------------------------------------- events stream (#136)


def test_vocabulary_constants_match_spec_table():
    """Pins the literal CONTENT (order + case) of all SIX vocabulary constants against spec
    §A.3, verbatim. EVENT_KINDS is enforced by append() and so is indirectly covered elsewhere,
    but KINDS/PHASE_KINDS/GATE_KINDS/REASON_CLASSES are deliberately NOT enforced at write time
    (see plan Step 3) — without this test a typo, a dropped member, or a case flip drifts
    silently from the spec with nothing to catch it.

    KINDS added (issue #298, [E15.S4]): the entries-stream vocabulary had NO dedicated pin here
    before this goal — a `KINDS[0]` rename only incidentally failed five behavioral tests that
    happen to call `ledger.append(..., "claimed", ...)` literally, never asserted the spelling
    itself the way EVENT_KINDS/PHASE_KINDS/etc. already did. This is also the engine half of the
    sibling pin `contract/vocabulary.json`'s `"entries_kinds"` key names by hand.

    `model_choice` added (issue #1030): written by `skills/sigma-model/scripts/predict.py`'s own
    `resolve`/`resolve_step` shelling out to `loop.py emit`, not by a SKILL.md instruction -- see
    `loop.py`'s own `_EMIT_KINDS` for why it stays reliability class 2 downstream despite being code-driven."""
    assert ledger.KINDS == (
        "claimed", "done", "parked", "failed", "handoff", "ack", "release", "note", "merged", "merge-armed", "acceptance")
    assert ledger.EVENT_KINDS == (
        "phase", "gate", "verify", "slice", "spend", "retro", "park", "scan", "run_stop",
        "model_choice", "merge_observed", "review_posted", "ci_observed")
    assert ledger.PHASE_KINDS == ("goal", "research", "plan", "plan_review", "implement", "review", "retro")
    assert ledger.GATE_KINDS == (
        "plan_review", "code_review", "post_review", "merge", "decision", "alignment",
        "verify", "risk_security", "risk_contract", "risk_migration", "risk_release", "risk_debug",
            # #1937: the diff-only test-tamper gate. Written by work.py's merge() ONLY -- kept out
            # of loop.py's _EMIT_GATE_KINDS so an agent cannot hand-write its own verdict.
            "test_trust")
    assert ledger.VERDICTS == ("pass", "block", "warn", "absent")
    assert ledger.REASON_CLASSES == (
        "irreversible", "needs_decision", "merge_conflict", "failing_check",
        "no_evidence", "dependency", "review_cap", "budget", "backlog-empty",
            # #1242: a quota/rate-limit park (e.g. `source.complete()`'s downgrade-to-park path
            # from #1201, when the underlying failure is an exhausted GitHub GraphQL quota) gets
            # its own member instead of falling into "unknown" indistinguishably from a genuinely
            # unclassified park -- see loop.py's `_REASON_CLASS_RULES` for the needle that routes
            # it here.
            "quota", "unknown",
            # #2521: the run_stop reason for a deliberate, healthy session-retirement
            # checkpoint -- see loop.py's `_handoff_reason` and a downstream budget metric's own
            # guardrail for why it is deliberately excluded from budget-exhaustion counting.
            "handoff")


def test_retro_grades_matches_sdlc_retro_skill_prose():
    """#140: spec §A.3's `retro.grade` vocabulary (mirrors `sigma-retro/SKILL.md` §3's
    achieved/partial/diverged bullets) had no Python home until now — `emit` validates
    against this tuple even though `append()` itself still leaves the value open
    (same deliberately-deferred-enforcement pattern as PHASE_KINDS/GATE_KINDS above)."""
    assert ledger.RETRO_GRADES == ("achieved", "partial", "diverged")


def test_team_md_byte_identical_with_events_stream_present(tmp_path):
    d = _sdlc(tmp_path, ON)
    ledger.append(d, ON, "claimed", "g.md")
    before = ledger.render(ledger.read_all(d))
    ledger.append(d, ON, "phase", "g.md", stream="events", phase="implement", state="start")
    after = ledger.render(ledger.read_all(d))
    assert after == before


def test_unknown_event_kind_raises(tmp_path):
    d = _sdlc(tmp_path, ON)
    with pytest.raises(ValueError):
        ledger.append(d, ON, "banana", "g.md", stream="events")


def test_unknown_event_verdict_raises(tmp_path):
    d = _sdlc(tmp_path, ON)
    with pytest.raises(ValueError):
        # lowercase "block" is a valid verdict; "fail" is the trap — pipeline.py's uppercase
        # "FAIL" must not leak in as a valid lowercase alias.
        ledger.append(d, ON, "gate", "g.md", stream="events", gate="merge", verdict="fail")


def test_event_ids_monotonic_per_actor_and_stream(tmp_path):
    d = _sdlc(tmp_path, ON)
    e1 = ledger.append(d, ON, "claimed", "g.md")
    e2 = ledger.append(d, ON, "done", "g.md")
    e3 = ledger.append(d, ON, "phase", "g.md", stream="events", phase="implement", state="start")
    e4 = ledger.append(d, ON, "phase", "g.md", stream="events", phase="implement", state="end")
    inst = ledger._instance_token()
    assert e1["id"] == f"dana:{inst}:1" and e2["id"] == f"dana:{inst}:2"
    assert e3["id"] == f"dana:{inst}:1" and e4["id"] == f"dana:{inst}:2"  # independent per-stream counter


def test_default_stream_unchanged(tmp_path):
    d = _sdlc(tmp_path, ON)
    ledger.append(d, ON, "note", "g.md")
    assert ledger.entry_file(d, "dana").exists()
    # F10/#540: the filename now carries the writing INSTANCE too (<host>.<pid>), not just the
    # actor (see test_entry_file_is_per_actor_per_process below for the reason) — still under
    # entries_dir, still named after the actor.
    assert ledger.entry_file(d, "dana") == ledger.entries_dir(d) / f"dana-{ledger._instance_token()}.jsonl"
    assert len(ledger.read_all(d)) == 1


# --- F10: same-actor concurrency must not collide on `id` -------------------------------------


def test_entry_file_is_per_actor_per_process(tmp_path):
    d = _sdlc(tmp_path, ON)
    assert ledger.entry_file(d, "dana").name == f"dana-{ledger._instance_token()}.jsonl"


def test_concurrent_same_actor_writers_never_collide_on_id(tmp_path, monkeypatch):
    """The repro: several parallel loops resolve to the SAME actor (a shared gh login) and used to
    write the same file — two concurrent appends both read `_line_count` as 0 and both minted
    `dana:1`. Simulate two such writers (different pids, same actor) each appending: they must land
    in different files and never produce the same `id`."""
    d = _sdlc(tmp_path, ON)
    monkeypatch.setattr(os, "getpid", lambda: 111)
    e1 = ledger.append(d, ON, "claimed", "g.md")
    monkeypatch.setattr(os, "getpid", lambda: 222)
    e2 = ledger.append(d, ON, "claimed", "h.md")          # a "concurrent" writer, same actor
    assert e1["id"] != e2["id"]                            # the collision this finding is about
    assert e1["actor"] == e2["actor"] == "dana"             # same person, correctly attributed
    files = sorted(p.name for p in ledger.entries_dir(d).glob("*.jsonl"))
    host = ledger._host_token()
    assert files == [f"dana-{host}.111.jsonl", f"dana-{host}.222.jsonl"]   # never sharing a file
    all_entries = ledger.read_all(d)
    assert len(all_entries) == 2
    assert len({e["id"] for e in all_entries}) == 2         # both entries survive the union, no collision


def test_concurrent_same_actor_writers_each_still_get_their_own_monotonic_sequence(tmp_path, monkeypatch):
    d = _sdlc(tmp_path, ON)
    monkeypatch.setattr(os, "getpid", lambda: 111)
    a1 = ledger.append(d, ON, "claimed", "g.md")
    a2 = ledger.append(d, ON, "done", "g.md")
    monkeypatch.setattr(os, "getpid", lambda: 222)
    b1 = ledger.append(d, ON, "claimed", "h.md")
    # id embeds the writer instance too (not just the filename) — watch_classify.py's cursor keys
    # off it to tell these two writers apart (see test_watch.py's writer/cursor tests).
    host = ledger._host_token()
    assert a1["id"] == f"dana:{host}.111:1" and a2["id"] == f"dana:{host}.111:2"  # 111's own sequence
    assert b1["id"] == f"dana:{host}.222:1"                  # process 222 starts its own, in its own file


def test_event_fields_land_in_the_jsonl(tmp_path):
    d = _sdlc(tmp_path, ON)
    ledger.append(d, ON, "gate", "g.md", stream="events",
                  gate="merge", verdict="pass", cycle=2, why="looked fine")
    path = ledger.entry_file(d, "dana", "events")
    line = json.loads(path.read_text().strip().splitlines()[0])
    assert line["gate"] == "merge" and line["verdict"] == "pass"
    assert line["cycle"] == 2 and line["why"] == "looked fine"


def test_observation_event_schemas_reject_unkeyed_or_invalid_payloads(tmp_path):
    """Observation records are typed facts, never an arbitrary journal payload."""
    d = _sdlc(tmp_path, {"ledger": {"actor": "dana"}, "journal": {"enabled": True}})
    fields = {
        "observation_key": "a" * 64, "subject_kind": "goal", "subject": "2577",
        "pr": 1, "merge_sha": "b" * 40,
    }
    event = ledger.append(d, {"ledger": {"actor": "dana"}, "journal": {"enabled": True}},
                          "merge_observed", "2577", stream="events", **fields)
    assert event and event["observation_key"] == fields["observation_key"]
    with pytest.raises(ValueError, match="invalid typed merge_observed"):
        ledger.append(d, {"ledger": {"actor": "dana"}, "journal": {"enabled": True}},
                      "merge_observed", "2577", stream="events", **{**fields, "observation_key": "BAD"})


def test_merged_requires_a_stable_key(tmp_path):
    d = _sdlc(tmp_path, ON)
    with pytest.raises(ValueError, match="merged_entry_key"):
        ledger.append(d, ON, "merged", "2577", pr=1)
    entry = ledger.append(d, ON, "merged", "2577", pr=1, merged_entry_key="c" * 64)
    assert entry["merged_entry_key"] == "c" * 64


def test_phase_event_state_start_does_not_raise(tmp_path):
    d = _sdlc(tmp_path, ON)
    e = ledger.append(d, ON, "phase", "g.md", stream="events", phase="implement", state="start")
    assert e is not None and e["state"] == "start"


def test_unknown_stream_raises(tmp_path):
    d = _sdlc(tmp_path, ON)
    with pytest.raises(ValueError):
        ledger.append(d, ON, "note", "g.md", stream="bogus")
    with pytest.raises(ValueError):
        ledger.read_all(d, stream="bogus")


# ------------------------------------------------- EVENTS gate + destination (#2574/S1-G3, was #244)
# EVENTS-stream writes gate on `ledger.journal_on(sdlc_dir, config)` -- the journal switch: org
# policy first (a managed-settings lock, with its lease), then `journal.enabled` in the local
# config -- the only key read since #2706 dropped the one-release alias. ENTRIES is unaffected - still
# `ledger.enabled` alone.
#
# DESTINATION NO LONGER ROUTES. Every EVENTS write lands in `local_events_dir()`
# (`.sdlc/events/`, a sibling of `.sdlc/ledger/`, gitignored, never an ops branch). The old block's `share`
# and the `entries_dir(d, EVENTS)` destination it selected are DELETED, not deprecated: PRD §6.2
# ("Never published") removes publishing outright, because git cannot hold events. `.sdlc/ledger/
# events/` is still READ for one release (doctor, phase_report, time_report, a downstream
# reader all union both dirs), but nothing writes it.


def test_events_are_a_noop_when_ledger_on_but_the_journal_is_absent(tmp_path):
    """Bug A, the regression this gate has always existed to prevent, restated for the journal: a
    repo that already has ledger.enabled:true and has never opted the journal in must NOT start
    writing events just because emitters landed. ENTRIES, on the same config, is unaffected.
    `journal_on` is False with neither a `journal` nor an old-key block."""
    cfg = {"ledger": {"enabled": True, "actor": "dana"}}   # no journal and no old journal key at all
    d = _sdlc(tmp_path, cfg)
    assert ledger.append(d, cfg, "gate", "g.md", stream="events", gate="merge", verdict="pass") is None
    assert not ledger.local_events_dir(d).exists()
    assert not ledger.entries_dir(d, ledger.EVENTS).exists()   # nor the legacy shared destination
    assert ledger.append(d, cfg, "note", "g.md") is not None    # entries stream: unaffected


def test_events_write_nothing_when_the_journal_is_explicitly_off(tmp_path):
    """The shipped default. `journal.enabled: false` writes zero journal bytes -- and creates
    neither directory, which is what the census (tests/test_core_only_writes.py) measures from the
    outside."""
    cfg = {"ledger": {"enabled": True, "actor": "dana"}, "journal": {"enabled": False}}
    d = _sdlc(tmp_path, cfg)
    assert ledger.append(d, cfg, "gate", "g.md", stream="events", gate="merge", verdict="pass") is None
    assert not ledger.local_events_dir(d).exists()
    assert not ledger.entries_dir(d, ledger.EVENTS).exists()


def test_events_write_to_the_journal_dir_when_the_journal_is_on_but_the_ledger_is_off(tmp_path):
    """Spec §A.2's headline case, unchanged in spirit and simpler in mechanism: the journal keeps
    writing with the team ledger off entirely. No `share` key is involved any more -- there is no
    `share` key."""
    # `ledger.actor` (not `ledger.enabled`) is set so the writer resolves to a known name instead of
    # falling through to the real `gh`/shell identity -- `enabled(config)` stays False either way
    # since no `enabled` key is present.
    cfg = {"ledger": {"actor": "dana"}, "journal": {"enabled": True}}
    d = _sdlc(tmp_path, cfg)
    entry = ledger.append(d, cfg, "gate", "g.md", stream="events", gate="merge", verdict="pass")
    assert entry is not None
    assert (ledger.local_events_dir(d) / f"dana-{ledger._instance_token()}.jsonl").exists()
    assert not ledger.entries_dir(d, ledger.EVENTS).exists()

    cfg2 = {"ledger": {"enabled": False, "actor": "dana"}, "journal": {"enabled": True}}
    d2 = _sdlc(tmp_path.parent / (tmp_path.name + "-2"), cfg2)
    assert ledger.append(d2, cfg2, "gate", "g.md", stream="events", gate="merge", verdict="pass")
    assert (ledger.local_events_dir(d2) / f"dana-{ledger._instance_token()}.jsonl").exists()


@pytest.mark.parametrize("share", [True, False, "sure", 0, None])
def test_share_no_longer_routes_anything_whatever_value_it_carries(tmp_path, share):
    """THE T4 FLIP, stated as one assertion: a leftover `share` key -- any value, including the `true`
    that used to publish to the ops branch -- changes nothing. Every event lands in the local
    journal dir and the legacy shared dir is never created. Before #2574, `share: True` wrote
    `.sdlc/ledger/events/` and a teammate pulled it. (It sat in the journal's old block; since
    #2706 that block is not read at all, so the one place a stray `share` can still be read beside
    `enabled: true` is the `journal` block itself.)"""
    cfg = {"ledger": {"enabled": True, "actor": "dana"},
           "journal": {"enabled": True, "share": share}}
    d = _sdlc(tmp_path, cfg)
    assert ledger.append(d, cfg, "gate", "g.md", stream="events", gate="merge", verdict="pass")
    assert (ledger.local_events_dir(d) / f"dana-{ledger._instance_token()}.jsonl").exists()
    assert not ledger.entries_dir(d, ledger.EVENTS).exists()


def test_entry_file_no_longer_accepts_a_local_parameter(tmp_path):
    """Delta-5: the flag is DELETED, not kept as a no-op. A vestigial parameter that routes nothing
    is a future reader's trap -- it reads like a choice that still exists. EVENTS resolves to the
    journal dir unconditionally; ENTRIES is untouched."""
    d = _sdlc(tmp_path, ON)
    with pytest.raises(TypeError):
        ledger.entry_file(d, "dana", ledger.EVENTS, local=True)
    assert ledger.entry_file(d, "dana", ledger.EVENTS).parent == ledger.local_events_dir(d)
    assert ledger.entry_file(d, "dana", ledger.ENTRIES).parent == ledger.entries_dir(d, ledger.ENTRIES)


def test_the_three_old_name_helpers_are_gone(tmp_path):
    """All three read helpers for the old feature name are DELETED, and this pins it rather than
    letting a future edit quietly restore one.

    `telemetry_share_is_off` was the writer-side half of a routing decision that no longer exists;
    a downstream reader's mirror of it is deleted too (T2), and a reintroduced copy
    on either side would quietly restore a published destination. `telemetry_settings` /
    `telemetry_enabled` are replaced by `journal_settings` / `journal_enabled` -- a second,
    differently-named reader of the same setting is exactly the trap a single reader avoids."""
    assert not hasattr(ledger, "telemetry_share_is_off")
    assert not hasattr(ledger, "telemetry_settings")
    assert not hasattr(ledger, "telemetry_enabled")


def test_journal_enabled_is_strict_true_not_truthy():
    """The strictness `telemetry_enabled` used to own, now carried by its replacement."""
    assert ledger.journal_enabled({"journal": {"enabled": "yes"}}) is False
    assert ledger.journal_enabled({"journal": {"enabled": 1}}) is False
    assert ledger.journal_enabled({"journal": {"enabled": True}}) is True
    assert ledger.journal_enabled({}) is False
    assert ledger.journal_enabled(None) is False


def test_events_write_to_the_journal_dir_when_both_the_ledger_and_the_journal_are_on(tmp_path):
    """`ON` is this suite's "config nobody has touched" fixture (`journal.enabled: true`, no
    `share`). Before #2574 it resolved to the SHARED path; it now resolves to the journal dir, and
    `_local_events()` is what reads it back. (A stray `share` is `test_share_no_longer_routes_...`'s.)"""
    d = _sdlc(tmp_path, ON)
    entry = ledger.append(d, ON, "gate", "g.md", stream="events", gate="merge", verdict="pass")
    assert entry is not None
    assert _local_events(d)[0]["gate"] == "merge"
    assert (ledger.local_events_dir(d) / f"dana-{ledger._instance_token()}.jsonl").exists()
    assert not ledger.entries_dir(d, ledger.EVENTS).exists()


def test_entries_stream_unaffected_by_the_journal_switch(tmp_path):
    """ENTRIES routing (directory, gate) is untouched by `journal.enabled` - it lands at the same `entries_dir(d, "entries")` path it always has, gated on
    `ledger.enabled` alone."""
    cfg = {"ledger": {"enabled": True, "actor": "dana"}, "journal": {"enabled": True}}
    d = _sdlc(tmp_path, cfg)
    entry = ledger.append(d, cfg, "note", "g.md")
    assert entry is not None
    assert ledger.entry_file(d, "dana", "entries").exists()
    assert ledger.entries_dir(d, "entries") == ledger.ledger_dir(d) / "entries"   # unchanged shape


# --------------------------------------------------------------------- #141: privacy caps + scrubbing
# Cap+scrub lives once, inside append(), driven by the declared EVENT_FREE_TEXT_FIELDS map — every
# test below calls append() directly (never a call-site helper) so it proves the treatment at the one
# real chokepoint every write path (deterministic sites + emit + spend) already funnels through.


def _raw_bytes(d, actor="dana", stream="events"):
    """The written file's RAW bytes — not read_all()'s parsed view — so a scrub-bypass test that
    only checked the parsed dict could not miss a leak sitting in some OTHER part of the line."""
    return ledger.entry_file(d, actor, stream).read_bytes()


def test_append_scrubs_a_planted_pem_block_in_gate_why(tmp_path):
    d = _sdlc(tmp_path, ON)
    pem_body = "MIIBVwIBADANBgkq" + "hkiG9w0BAQEFAASCAT"
    secret = "-----BEGIN PRIVATE KEY-----\n" + pem_body + "\n-----END PRIVATE KEY-----"
    ledger.append(d, ON, "gate", "g.md", stream="events", gate="merge", verdict="block",
                  why=f"leaked pem: {secret}")
    disk = _raw_bytes(d).decode()
    assert "BEGIN PRIVATE KEY" not in disk and pem_body not in disk
    assert "[REDACTED" in disk


def test_append_scrubs_a_planted_aws_key_in_gate_why(tmp_path):
    d = _sdlc(tmp_path, ON)
    SECRET = "AK" "IAIOSFODNN7EXAMPLE"
    ledger.append(d, ON, "gate", "g.md", stream="events", gate="merge", verdict="block",
                  why=f"found a key {SECRET} in the diff")
    disk = _raw_bytes(d).decode()
    assert SECRET not in disk and "[REDACTED" in disk


def test_append_scrubs_a_planted_github_token_in_park_why(tmp_path):
    d = _sdlc(tmp_path, ON)
    SECRET = "gh" "p_ABCDEFGHIJ1234567890ABCD"
    ledger.append(d, ON, "park", "g.md", stream="events", reason_class="unknown",
                  why=f"blocked by a leaked token {SECRET}")
    disk = _raw_bytes(d).decode()
    assert SECRET not in disk and "[REDACTED" in disk


def test_append_scrubs_a_planted_jwt_in_spend_model(tmp_path):
    d = _sdlc(tmp_path, ON)
    SECRET = ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
              "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U")
    ledger.append(d, ON, "spend", "g.md", stream="events", model=f"sonnet {SECRET}")
    disk = _raw_bytes(d).decode()
    assert SECRET not in disk and "[REDACTED" in disk


def test_append_scrubs_a_planted_bearer_token_in_gate_why(tmp_path):
    d = _sdlc(tmp_path, ON)
    SECRET = "Be" "arer abcdef1234567890ABCDEF"
    ledger.append(d, ON, "gate", "g.md", stream="events", gate="merge", verdict="block",
                  why=f"request used {SECRET} against a locked-down endpoint")
    disk = _raw_bytes(d).decode()
    assert SECRET not in disk and "[REDACTED" in disk


def test_append_scrubs_a_planted_password_kv_in_gate_why(tmp_path):
    d = _sdlc(tmp_path, ON)
    SECRET = "Su" "perSecretValue123"
    ledger.append(d, ON, "gate", "g.md", stream="events", gate="merge", verdict="block",
                  why=f"config leaked password={SECRET} in a log line")
    disk = _raw_bytes(d).decode()
    assert SECRET not in disk and "[REDACTED" in disk


def test_append_caps_gate_why_at_200_chars_after_scrub(tmp_path):
    d = _sdlc(tmp_path, ON)
    why = "x" * 300     # no secret shape: pure length test
    e = ledger.append(d, ON, "gate", "g.md", stream="events", gate="merge", verdict="warn", why=why)
    assert len(e["why"]) <= 200


def test_append_flattens_a_raw_newline_in_park_why(tmp_path):
    """append() itself NEVER rejects — only sanitizes — for a caller that isn't the two agent-
    facing CLI verbs (those reject in loop.py's _validate_event, tested in test_loop.py). This is
    the guarantee the three deterministic, fail-open call sites (a hook's deny, an autonomous park,
    work.py's post-review) depend on."""
    d = _sdlc(tmp_path, ON)
    e = ledger.append(d, ON, "park", "g.md", stream="events", reason_class="unknown", why="a\nb")
    assert "\n" not in e["why"]


# --------------------------------------------------------------------- #953: park.decision_tier
# The categorizer (#952, decision_tier.py) is real but nothing calls it yet. #953 wires it into the
# park flow: the `park` EVENTS kind gains one new, OPTIONAL field so a `needs_decision` park can
# carry the tier decision_tier.resolve() computed, without disturbing any existing park record.


def test_park_event_accepts_an_optional_decision_tier_field(tmp_path):
    """The schema-extension half of #953: `park` events can now carry `decision_tier`, the same
    write path every other park field already uses — no new whitelist mechanism invented."""
    d = _sdlc(tmp_path, ON)
    e = ledger.append(d, ON, "park", "g.md", stream="events", reason_class="needs_decision",
                       why="PR #1 changes requested", decision_tier="escalate_l0")
    assert e["decision_tier"] == "escalate_l0"


def test_park_event_omits_decision_tier_when_not_passed(tmp_path):
    """The read half of the same round trip: a park event written WITHOUT decision_tier (every
    existing caller, and every non-needs_decision reason_class after #953) must not grow the key at
    all — proves the field is genuinely optional, not defaulted to an empty/null placeholder."""
    d = _sdlc(tmp_path, ON)
    e = ledger.append(d, ON, "park", "g.md", stream="events", reason_class="unknown", why="x")
    assert "decision_tier" not in e


def test_park_event_omits_decision_tier_when_explicitly_none(tmp_path):
    """Same proof, but for the shape `_record()` actually calls with on every OTHER reason_class:
    `decision_tier=None` passed explicitly (not simply absent from the call). append()'s existing
    `value not in (None, "")` gate must skip it exactly like it does for `why=None`."""
    d = _sdlc(tmp_path, ON)
    e = ledger.append(d, ON, "park", "g.md", stream="events", reason_class="merge_conflict",
                       why="stale head", decision_tier=None)
    assert "decision_tier" not in e


def test_park_event_decision_tier_survives_read_all_round_trip(tmp_path):
    """The full write -> disk -> read_all() round trip, not just append()'s in-memory return —
    proves the field actually persists to the per-actor jsonl file and comes back out."""
    d = _sdlc(tmp_path, ON)
    ledger.append(d, ON, "park", "g.md", stream="events", reason_class="needs_decision",
                  why="unresolved review thread", decision_tier="escalate_l1")
    events = [e for e in _local_events(d) if e["kind"] == "park"]
    assert len(events) == 1 and events[0]["decision_tier"] == "escalate_l1"


def test_park_event_decision_tier_is_scrubbed_and_capped_like_reason_class(tmp_path):
    """decision_tier is declared alongside reason_class in EVENT_ENUM_FIELDS (#953 follows the SAME
    enum-field convention `park.reason_class` already established, per that bucket's own
    round-3 fix: "an enum can never carry prose, whatever a future caller passes") — so a caller
    that (mis)uses it to smuggle prose gets the identical bounded-id scrub+cap, not silent trust."""
    d = _sdlc(tmp_path, ON)
    e = ledger.append(d, ON, "park", "g.md", stream="events", reason_class="needs_decision",
                       why="x", decision_tier="x" * 300)
    assert len(e["decision_tier"]) <= ledger.BOUNDED_ID_CAP


def test_decision_tier_is_declared_non_prose_and_enum_for_park():
    """Pins the classification itself (not just its downstream effect): decision_tier must live in
    EVENT_NON_PROSE_FIELDS['park'] and EVENT_ENUM_FIELDS['park'], exactly where reason_class already
    lives — the module-level completeness guards (`_assert_event_fields_classified`,
    `_assert_non_prose_fields_are_typed`) already fail the whole import if this drifts, but this
    test names the expectation explicitly so a future refactor sees why, not just that."""
    assert "decision_tier" in ledger.EVENT_FIELDS["park"]
    assert "decision_tier" in ledger.EVENT_NON_PROSE_FIELDS["park"]
    assert "decision_tier" in ledger.EVENT_ENUM_FIELDS["park"]


def test_entries_stream_why_is_scrubbed_flattened_and_capped(tmp_path):
    """F3: #141 originally scoped cap+scrub to stream == EVENTS only, leaving the ENTRIES stream's
    own `why` (hand-offs/notes a lead reads in TEAM.md) written byte-for-byte — but ENTRIES is
    committed + pushed to the shared `sdlc-ledger` branch and rendered into TEAM.md just the same,
    so a sanctioned `handoff.py open ... --why "<secret>"` landed a secret in version control. Same
    flatten->scrub->cap treatment as EVENTS' free-text fields now applies here too."""
    d = _sdlc(tmp_path, ON)
    SECRET = "AK" "IAIOSFODNN7EXAMPLE"
    why = f"blocked: {SECRET} for the deploy\nsee the log" + "x" * 300
    e = ledger.append(d, ON, "handoff", "g.md", why=why)
    assert SECRET not in e["why"]
    assert "[REDACTED:aws-key]" in e["why"]
    assert "\n" not in e["why"]                      # flattened
    assert len(e["why"]) <= 200                       # capped


def test_entries_stream_why_scrub_survives_the_committed_jsonl_and_rendered_team_md(tmp_path):
    """F3's stated verification: redaction holds in BOTH the persisted entry and render() output —
    the two places a secret in `why` was reaching (the committed per-actor jsonl, and TEAM.md)."""
    d = _sdlc(tmp_path, ON)
    SECRET = "AK" "IAIOSFODNN7EXAMPLE"
    ledger.append(d, ON, "handoff", "g.md", to="rae", issue=61, why=f"blocked by {SECRET}")
    persisted = ledger.entry_file(d, "dana").read_text(encoding="utf-8")
    assert SECRET not in persisted and "[REDACTED:aws-key]" in persisted
    rendered = ledger.render(ledger.read_all(d))
    assert SECRET not in rendered and "[REDACTED:aws-key]" in rendered


def test_entries_stream_ref_is_capped_and_scrubbed(tmp_path):
    """Plan-review R2 (#385): `ref` was assumed to already be "unscrubbed-but-capped" on the
    ENTRIES stream, like every other short OPTIONAL_FIELDS value -- FALSE. append()'s ENTRIES branch
    only ever sanitized `why` (see the test above); `ref` was written RAW AND UNBOUNDED. Every OTHER
    ENTRIES optional field is operator/CLI-typed or a hard-coded constant in every existing caller;
    comment_watch.py (#385) is the first to source `ref` from something outside this plugin's own
    control (a GitHub comment's opaque node id), so it needs the SAME flatten->scrub->cap enforcement
    EVENT_BOUNDED_ID_FIELDS already gets on the events stream, not trust that the next caller will
    also be well-behaved. Fails before the fix (SECRET and the full 300+ char value both present,
    uncapped); passes after (redacted, flattened, capped to BOUNDED_ID_CAP)."""
    d = _sdlc(tmp_path, ON)
    SECRET = "AK" "IAIOSFODNN7EXAMPLE"
    ref = f"{SECRET}-" + "x" * 300 + "\nsecond line"
    e = ledger.append(d, ON, "note", "g.md", ref=ref)
    assert SECRET not in e["ref"]
    assert "[REDACTED:aws-key]" in e["ref"]
    assert "\n" not in e["ref"]                        # flattened
    assert len(e["ref"]) <= ledger.BOUNDED_ID_CAP        # capped SHORT, not FREE_TEXT_CAP -- an id, not prose
    assert len(e["ref"]) < len(ref)                      # actually shorter than the raw input, not a no-op


def test_entries_stream_ref_scrub_survives_the_committed_jsonl(tmp_path):
    """R2's stated concern is specifically that `ref` is committed byte-for-byte to the shared,
    pushed `sdlc-ledger` branch -- redaction has to hold in the actual persisted line, not just the
    in-memory returned dict."""
    d = _sdlc(tmp_path, ON)
    SECRET = "AK" "IAIOSFODNN7EXAMPLE"
    ledger.append(d, ON, "note", "g.md", ref=f"{SECRET}" + "y" * 200)
    persisted = ledger.entry_file(d, "dana").read_text(encoding="utf-8")
    assert SECRET not in persisted
    assert "[REDACTED:aws-key]" in persisted


def test_entries_stream_ref_a_normal_comment_id_passes_through_intact(tmp_path):
    """The non-degenerate case: a REAL GitHub GraphQL comment id (short, no secret shape) must not
    be mangled by the new enforcement -- flatten/scrub/cap must be a no-op for well-formed input,
    only a backstop for malformed/oversized/secret-bearing input."""
    d = _sdlc(tmp_path, ON)
    e = ledger.append(d, ON, "note", "g.md", ref="IC_kwDOTE1deM8AAAABNe8Wcg")
    assert e["ref"] == "IC_kwDOTE1deM8AAAABNe8Wcg"


def test_scan_file_and_slice_slice_are_not_in_the_declared_set():
    """`file`/`slice` are ids and short paths, not prose — they must never get the PROSE cap/scrub
    treatment (FREE_TEXT_CAP=200, the `why`/`model` cap). Post-review fix: they are NOT unbounded
    either any more — see EVENT_BOUNDED_ID_FIELDS and BOUNDED_ID_CAP for their own, shorter,
    enforced cap+scrub, distinct from this prose set."""
    assert "file" not in ledger.EVENT_FREE_TEXT_FIELDS.get("scan", ())
    assert "slice" not in ledger.EVENT_FREE_TEXT_FIELDS.get("slice", ())
    assert "file" in ledger.EVENT_BOUNDED_ID_FIELDS.get("scan", ())
    assert "slice" in ledger.EVENT_BOUNDED_ID_FIELDS.get("slice", ())


def test_event_free_text_fields_is_the_declared_set():
    assert ledger.EVENT_FREE_TEXT_FIELDS == {
        "phase": ("model",), "gate": ("why",), "park": ("why",), "spend": ("model",), "run_stop": ("why",),
        "model_choice": ("model", "signal")}


def test_order_of_operations_scrub_before_cap_survives_a_late_secret(tmp_path):
    """THE load-bearing regression. An independent review proved by execution that an AWS-shaped
    key starting at char 195 of a 215-char string is fully redacted under flatten->scrub->cap, but
    a cap-then-scrub order leaves the literal fragment `AKIAI` in the data (the cap truncates the
    match before the scrubber ever sees the whole shape). This test uses the reviewer's exact case
    and MUST fail if someone reorders cap before scrub."""
    d = _sdlc(tmp_path, ON)
    SECRET = "AK" "IAIOSFODNN7EXAMPLE"          # 20 chars
    why = ("x" * 195) + SECRET               # secret starts at index 195, string is 215 chars total
    assert len(why) == 215 and why.index(SECRET) == 195
    e = ledger.append(d, ON, "gate", "g.md", stream="events", gate="merge", verdict="block", why=why)
    assert SECRET not in e["why"]
    assert "AKIAI" not in e["why"]           # the exact fragment a cap-then-scrub order would leak
    assert len(e["why"]) <= 200
    # the 200-char cap lands mid-way through the "[REDACTED:aws-key]" replacement token itself
    # (195 leading chars + a 5-char slice of the replacement) — redaction visibly started, which is
    # the whole point: the secret was replaced BEFORE the cap ever ran, not truncated as raw text.
    assert e["why"].endswith("[REDA")


def test_completeness_guard_fails_on_an_unclassified_field():
    """Amendment B: a type check alone cannot catch a forgotten prose field (a forgotten field is
    still a plain `str`, same as a properly-declared one) — only an explicit allowlist that fails
    CLOSED on a name in neither list can. Proved here by temporarily adding an unclassified `notes`
    field to a copy of retro's EVENT_FIELDS entry and confirming the same guard the module runs at
    import time rejects it, rather than silently defaulting it to safe."""
    import pytest as _pytest
    broken_fields = dict(ledger.EVENT_FIELDS)
    broken_fields["retro"] = broken_fields["retro"] + ("notes",)   # a future free-text field, forgotten
    with _pytest.raises(AssertionError, match="unclassified"):
        ledger._assert_event_fields_classified(
            ledger.EVENT_KINDS, broken_fields, ledger.EVENT_FREE_TEXT_FIELDS, ledger.EVENT_NON_PROSE_FIELDS)


def test_completeness_guard_passes_on_the_real_declared_maps():
    """The real, shipped maps must NOT trip the guard — proves the test above is exercising a real
    failure mode, not a check that always fails."""
    ledger._assert_event_fields_classified(
        ledger.EVENT_KINDS, ledger.EVENT_FIELDS, ledger.EVENT_FREE_TEXT_FIELDS, ledger.EVENT_NON_PROSE_FIELDS)


def test_sanitize_free_text_flattens_scrubs_and_caps_directly():
    """The helper, exercised directly (not just through append()) — Step 2 of the plan."""
    assert "\n" not in ledger._sanitize_free_text("a\nb")
    assert len(ledger._sanitize_free_text("x" * 300)) <= 200
    scrubbed = ledger._sanitize_free_text("key: AK" "IAIOSFODNN7EXAMPLE")
    assert "AK" "IAIOSFODNN7EXAMPLE" not in scrubbed and "[REDACTED" in scrubbed


def test_sanitize_free_text_never_raises_on_hostile_input_shapes():
    """Hard constraint: `_sanitize_free_text` must NEVER raise, whatever lands in it — `append()`
    sits behind the three deterministic, fail-open call sites (a hook's `deny`, an autonomous
    park) and must stay exception-safe no matter what a future caller hands it."""
    for value in (None, 42, True, b"\x00\x01raw-bytes", {"a": 1}, [1, 2, 3], "x" * (10 * 1024 * 1024)):
        result = ledger._sanitize_free_text(value)
        assert isinstance(result, str)
        assert len(result) <= ledger.FREE_TEXT_CAP
    # the shorter bounded-id cap must never raise either, for the same set of hostile shapes
    for value in (None, 42, b"\x00", {"a": 1}, [1, 2, 3]):
        result = ledger._sanitize_free_text(value, cap=ledger.BOUNDED_ID_CAP)
        assert isinstance(result, str)
        assert len(result) <= ledger.BOUNDED_ID_CAP


def test_sanitize_free_text_never_raises_when_the_scrub_module_is_unreachable(monkeypatch):
    """Fail-open even past the scrub load itself: a broken/missing hooks/research_capture.py must
    degrade to flatten+cap only, never raise — this is what keeps append() safe for the three
    fail-open deterministic call sites."""
    monkeypatch.setattr(ledger, "_scrub_module", lambda: None)
    result = ledger._sanitize_free_text("a\nb" + "x" * 300)
    assert "\n" not in result and len(result) <= 200


def test_scrub_module_load_failure_is_observable_on_stderr(monkeypatch, capsys):
    """Amendment C: a fail-open degrade to cap-only must never be SILENT — one line to stderr in
    the loader's except branch, matching safe_append's own idiom (ledger.py's
    'ledger: entry skipped (non-fatal): ...' message shape)."""
    monkeypatch.setattr(ledger, "_SCRUB_MODULE", None)
    monkeypatch.setattr(ledger, "_SCRUB_LOAD_ATTEMPTED", False)

    def boom(name, path):
        raise OSError("no such file")
    import importlib.util as _ilu
    monkeypatch.setattr(_ilu, "spec_from_file_location", boom)
    mod = ledger._scrub_module()
    assert mod is None
    err = capsys.readouterr().err
    assert "non-fatal" in err and "scrub" in err


def test_command_sha256_never_carries_the_raw_command_in_a_verify_event(tmp_path):
    """Regression pin (#139 already shipped this — no new logic here): the raw verify command
    string never appears anywhere in a `verify` event, only its sha256."""
    import hashlib
    d = _sdlc(tmp_path, ON)
    cmd = "echo super-secret-marker-xyz123"
    e = ledger.append(d, ON, "verify", "g.md", stream="events", ok=True, exit=0, ms=5,
                      command_sha256=hashlib.sha256(cmd.encode("utf-8")).hexdigest())
    assert json.dumps(e).find(cmd) == -1
    assert e["command_sha256"] == hashlib.sha256(cmd.encode("utf-8")).hexdigest()


def test_files_declared_is_an_int_not_a_list(tmp_path):
    """Regression pin (#139 already shipped this as a count): the events stream's `files_declared`
    is a plain int, never a list — a later refactor that started passing `s["files"]` (the list)
    instead of `len(s["files"])` would land a JSON array here, not a count."""
    d = _sdlc(tmp_path, ON)
    e = ledger.append(d, ON, "slice", "g.md", stream="events", slice="a", wave=1, mode="subagent",
                      files_declared=3)
    assert isinstance(e["files_declared"], int) and e["files_declared"] == 3


# ----------------------------------------------------------------- post-review fix: VALUE TYPES
# An independent PR review BLOCKED #249, proving by execution that the prose/non-prose binary above
# labels every field safe-or-not but never PROVES a "non-prose" label is true. `tokens_in`, `cycle`,
# `debt_count` etc. were plain CLI strings with zero shape enforcement anywhere on the write path —
# a payload with no literal newline sailed through untouched. The fix: every non-prose field now
# carries a declared VALUE TYPE (numeric/bool/enum/bounded-id), enforced — not just labelled — at
# the `append()` chokepoint (this section) and, redundantly, at the CLI (`test_loop.py`).


def test_event_numeric_fields_matches_the_non_prose_set_minus_bool_enum_and_bounded_id():
    """The four typed buckets (numeric/bool/enum/bounded_id) must partition EVENT_NON_PROSE_FIELDS
    exactly — proof the new fine-grained classification didn't silently drop or duplicate a field
    the coarse guard already confirmed was non-prose."""
    for kind in ledger.EVENT_KINDS:
        safe = set(ledger.EVENT_NON_PROSE_FIELDS.get(kind, ()))
        typed = (set(ledger.EVENT_NUMERIC_FIELDS.get(kind, ())) |
                 set(ledger.EVENT_BOOL_FIELDS.get(kind, ())) |
                 set(ledger.EVENT_ENUM_FIELDS.get(kind, ())) |
                 set(ledger.EVENT_BOUNDED_ID_FIELDS.get(kind, ())))
        assert typed == safe, f"{kind!r}: typed {typed} != non-prose {safe}"


def test_type_completeness_guard_fails_on_a_field_with_no_declared_type():
    """THE mutation proof. A type check alone can't catch a forgotten field (a forgotten numeric
    field is still a plain str, same as a properly-declared one) — only an explicit, exhaustive
    allowlist that fails CLOSED can. Mutate a copy of EVENT_NON_PROSE_FIELDS to add a field none of
    the four type maps mention, and confirm the guard the module runs at import time rejects it."""
    broken_non_prose = dict(ledger.EVENT_NON_PROSE_FIELDS)
    broken_non_prose["retro"] = broken_non_prose["retro"] + ("untyped_field",)
    with pytest.raises(AssertionError, match="no declared VALUE TYPE"):
        ledger._assert_non_prose_fields_are_typed(
            ledger.EVENT_KINDS, broken_non_prose,
            {"numeric": ledger.EVENT_NUMERIC_FIELDS, "bool": ledger.EVENT_BOOL_FIELDS,
             "enum": ledger.EVENT_ENUM_FIELDS, "bounded_id": ledger.EVENT_BOUNDED_ID_FIELDS})


def test_type_completeness_guard_passes_on_the_real_declared_maps():
    """The real, shipped maps must NOT trip the guard — proves the mutation test above is
    exercising a real failure mode, not a check that always fails."""
    ledger._assert_non_prose_fields_are_typed(
        ledger.EVENT_KINDS, ledger.EVENT_NON_PROSE_FIELDS,
        {"numeric": ledger.EVENT_NUMERIC_FIELDS, "bool": ledger.EVENT_BOOL_FIELDS,
         "enum": ledger.EVENT_ENUM_FIELDS, "bounded_id": ledger.EVENT_BOUNDED_ID_FIELDS})


def test_type_completeness_guard_fails_when_a_field_is_claimed_by_two_types():
    """A field must have EXACTLY one type — declaring it in two buckets is as much a
    classification bug as declaring it in none, so the guard must reject that too."""
    broken_numeric = dict(ledger.EVENT_NUMERIC_FIELDS)
    broken_numeric["retro"] = broken_numeric["retro"] + ("grade",)   # grade is already enum
    with pytest.raises(AssertionError):
        ledger._assert_non_prose_fields_are_typed(
            ledger.EVENT_KINDS, ledger.EVENT_NON_PROSE_FIELDS,
            {"numeric": broken_numeric, "bool": ledger.EVENT_BOOL_FIELDS,
             "enum": ledger.EVENT_ENUM_FIELDS, "bounded_id": ledger.EVENT_BOUNDED_ID_FIELDS})


def test_looks_numeric_accepts_ints_and_numeral_strings_rejects_everything_else():
    assert ledger._looks_numeric(10) is True
    assert ledger._looks_numeric("10") is True
    assert ledger._looks_numeric("-3") is True
    assert ledger._looks_numeric("x" * 200 + "AK" "IAIOSFODNN7EXAMPLE") is False
    assert ledger._looks_numeric(None) is False


def test_looks_numeric_enforces_signed_64bit_storability_bound():
    """#787: `_looks_numeric` is the single shared "valid numeric" definition for both loop.py's
    CLI `_validate_event` and append()'s sanitizer, so the contract it enforces IS the ledger's
    end-to-end storability guarantee. Every numeric column a downstream store maps is at most signed
    64-bit, so a value outside [-2**63, 2**63-1] is a record a downstream ingester could never INSERT.
    The old digit-only cap accepted up to 20 digits (10**20-1), overshooting the bound and letting
    an in-spec CLI write permanently stall a writer's stream. The boundary must be the int64 range,
    not a digit count."""
    assert ledger._looks_numeric(2**63 - 1) is True            # largest storable, 19 digits
    assert ledger._looks_numeric(str(2**63 - 1)) is True
    assert ledger._looks_numeric(2**63) is False               # one past -- unstorable
    assert ledger._looks_numeric(str(2**63)) is False
    assert ledger._looks_numeric(-(2**63)) is True             # signed 64-bit minimum, storable
    assert ledger._looks_numeric(str(-(2**63))) is True
    assert ledger._looks_numeric(str(-(2**63) - 1)) is False   # one below the minimum
    # a 10-digit value that overran the OLD 32-bit cycle/exit_code columns now fits their BIGINT
    assert ledger._looks_numeric(9999999999) is True
    assert ledger._looks_numeric("9999999999") is True


def test_a_numeric_field_stores_what_was_checked_not_what_was_typed(tmp_path):
    """The predicate normalised the string (`.replace("_", "")`) but the RAW value was written, so
    19 digits separated by 18 underscores passed a 20-digit check and would land at 37 characters.
    Third instance of one bug shape: a predicate applied to one representation, enforcement applied
    to another. #787 note: the value stays WITHIN signed 64-bit (19 digits, 1.23e18 < 2**63-1) so
    the added range check accepts it -- this test guards the underscore-normalisation bug, not the
    magnitude bound (that is test_looks_numeric_enforces_signed_64bit_storability_bound's job)."""
    d = _sdlc(tmp_path, ON)
    ledger.append(d, ON, "phase", "g.md", stream=ledger.EVENTS,
                  phase="plan", state="start", tokens_in="1_2_3_4_5_6_7_8_9_0_1_2_3_4_5_6_7_8_9")
    written = _local_events(d)[0]["tokens_in"]
    assert written == "1234567890123456789"
    assert len(written) <= ledger.NUMERIC_DIGIT_CAP
    assert "_" not in written


def test_an_enum_field_cannot_carry_prose_even_from_a_direct_append(tmp_path):
    """append() is the chokepoint, but the enum bucket had NO enforcement here — vocabulary checks
    live in loop.py's CLI-only _validate_event. No shipped caller passes anything but a constant,
    which is the same 'safe by convention' pattern that caused two earlier blocks, one level out."""
    d = _sdlc(tmp_path, ON)
    payload = "AK" "IAIOSFODNN7EXAMPLE and a whole paragraph of prose " + "x" * 200
    ledger.append(d, ON, "scan", "g.md", stream=ledger.EVENTS,
                  category=payload, file="a.py", count=1)
    written = _local_events(d)[0]["category"]
    assert "AK" "IAIOSFODNN7EXAMPLE" not in written        # scrubbed
    assert len(written) <= ledger.BOUNDED_ID_CAP        # and capped: an enum is never prose


def test_a_secret_base10_encoded_into_a_numeric_field_does_not_survive(tmp_path):
    """Parsing as an int is NOT a safety check. A secret encoded as one giant integer passes a
    purely syntactic _looks_numeric, so before NUMERIC_DIGIT_CAP it skipped the scrubber and both
    caps and landed raw — int(v).to_bytes() read it straight back off disk. Also pins the plain
    unbounded-length hole the same gap opened."""
    secret = b"AK" b"IAIOSFODNN7EXAMPLE|gh" b"p_ABCDEFGHIJ1234567890ABCD"
    encoded = str(int.from_bytes(secret, "big"))
    assert len(encoded) > ledger.NUMERIC_DIGIT_CAP           # the payload is only useful when long
    assert ledger._looks_numeric(encoded) is False           # so it can never take the raw path
    assert ledger._looks_numeric("7" * 50000) is False       # 50k digits is not a token count
    assert ledger._looks_numeric("9" * ledger.NUMERIC_DIGIT_CAP) is False    # #787: 20 nines
    #                                                        # (10**20-1) overrun signed 64-bit
    assert ledger._looks_numeric(2**63 - 1) is True                          # 19 digits, fits int64

    d = _sdlc(tmp_path, ON)
    ledger.append(d, ON, "phase", "g.md", stream=ledger.EVENTS,
                  phase="plan", state="start", tokens_in=encoded)
    written = _local_events(d)[0]["tokens_in"]
    assert encoded not in written
    # NUMERIC_DIGIT_CAP, not FREE_TEXT_CAP: scrubbing does not touch a digit string, so sanitizing
    # at the 200-char prose cap still left ~49 recoverable bytes of secret.
    assert len(written) <= ledger.NUMERIC_DIGIT_CAP
    assert int.from_bytes(secret, "big").to_bytes(len(secret), "big") not in written.encode()
    assert ledger._looks_numeric("3.5") is False


def test_looks_bool_accepts_real_bools_and_recognised_spellings():
    assert ledger._looks_bool(True) is True
    assert ledger._looks_bool(False) is True
    assert ledger._looks_bool("true") is True
    assert ledger._looks_bool("False") is True
    assert ledger._looks_bool("maybe") is False
    assert ledger._looks_bool("x" * 200 + "AK" "IAIOSFODNN7EXAMPLE") is False


def test_append_sanitizes_a_secret_bearing_non_numeric_value_in_a_declared_numeric_field(tmp_path):
    """THE LEAK, closed at the chokepoint (defense in depth beyond the CLI refusal — `append()` is
    reached directly by call sites that never go through the CLI: slices.py, pipeline.py,
    work.py's post_review, verify_goal). A declared-numeric field (`tokens_in`) given a secret-
    bearing, non-numeric, NEWLINE-FREE string — exactly the reviewer's repro shape — must be
    sanitised (scrub+cap), never written raw, and append() must never raise: the three
    deterministic fail-open call sites (a hook's `deny`, an autonomous park) depend on that."""
    d = _sdlc(tmp_path, ON)
    SECRET = "AK" "IAIOSFODNN7EXAMPLE"
    payload = "leaked key " + SECRET + (" filler" * 40)
    assert "\n" not in payload
    e = ledger.append(d, ON, "phase", "g.md", stream="events", phase="plan", state="start",
                      tokens_in=payload)
    assert SECRET not in e["tokens_in"]
    assert "[REDACTED" in e["tokens_in"]
    assert len(e["tokens_in"]) <= ledger.FREE_TEXT_CAP


def test_append_leaves_a_genuinely_numeric_value_untouched(tmp_path):
    """The other half: a legitimate numeric string must NOT be mangled by the new check — it is
    written through exactly as before, matching what `_flags` always hands `append()` (a string)."""
    d = _sdlc(tmp_path, ON)
    e = ledger.append(d, ON, "phase", "g.md", stream="events", phase="plan", state="start",
                      tokens_in="42")
    assert e["tokens_in"] == "42"


def test_append_never_raises_on_a_non_numeric_dict_or_bytes_in_a_numeric_field(tmp_path):
    """append() must never raise, whatever garbage lands in a declared-numeric field — a dict, a
    list, bytes, all sanitise cleanly instead of blowing up the fail-open call sites."""
    d = _sdlc(tmp_path, ON)
    for garbage in ({"a": 1}, [1, 2, 3], b"\x00\x01raw-bytes"):
        e = ledger.append(d, ON, "phase", "g.md", stream="events", phase="plan", state="start",
                          tokens_in=garbage)
        assert isinstance(e["tokens_in"], str)


def test_append_sanitizes_a_non_boolean_value_in_a_declared_bool_field(tmp_path):
    d = _sdlc(tmp_path, ON)
    SECRET = "AK" "IAIOSFODNN7EXAMPLE"
    e = ledger.append(d, ON, "verify", "g.md", stream="events", ok=f"nope {SECRET}", exit=1)
    assert SECRET not in e["ok"] and "[REDACTED" in e["ok"]


def test_append_leaves_a_real_bool_untouched(tmp_path):
    d = _sdlc(tmp_path, ON)
    e = ledger.append(d, ON, "verify", "g.md", stream="events", ok=True, exit=0)
    assert e["ok"] is True


# ----------------------------------------------------------------- post-review fix: bounded ids


def test_append_scrubs_and_caps_a_secret_bearing_slice_id(tmp_path):
    """Secondary finding: `slice.slice` was 'safe by convention' only — `slices.py` copies an
    agent-authored plan `id` verbatim with no length/shape check, so `slice="id-with-secret-
    AKIA..."` wrote unredacted at the append() layer. Now enforced like every other bounded
    identifier: scrub + a short cap (BOUNDED_ID_CAP), closing the gap with code, not convention."""
    d = _sdlc(tmp_path, ON)
    SECRET = "AK" "IAIOSFODNN7EXAMPLE"
    e = ledger.append(d, ON, "slice", "g.md", stream="events",
                      slice=f"id-with-secret-{SECRET}", wave=1, mode="subagent", files_declared=1)
    assert SECRET not in e["slice"]
    assert "[REDACTED" in e["slice"]
    assert len(e["slice"]) <= ledger.BOUNDED_ID_CAP


def test_append_caps_a_long_scan_file_path_at_the_bounded_id_cap(tmp_path):
    d = _sdlc(tmp_path, ON)
    e = ledger.append(d, ON, "scan", "(discovery-scan)", stream="events",
                      category="tech-debt", file="x" * 300, count=1)
    assert len(e["file"]) <= ledger.BOUNDED_ID_CAP


def test_append_caps_command_sha256_at_the_bounded_id_cap_but_leaves_a_real_hash_untouched(tmp_path):
    """A real sha256 hex digest (64 chars) is well under BOUNDED_ID_CAP (120) and has no secret
    shape, so it must survive byte-for-byte — the same regression pin as
    `test_command_sha256_never_carries_the_raw_command_in_a_verify_event`, now under the new
    bounded-id enforcement path instead of the old untouched-by-convention one."""
    import hashlib
    d = _sdlc(tmp_path, ON)
    digest = hashlib.sha256(b"echo hello").hexdigest()
    e = ledger.append(d, ON, "verify", "g.md", stream="events", ok=True, exit=0,
                      command_sha256=digest)
    assert e["command_sha256"] == digest


def test_bounded_id_cap_is_short_not_the_prose_cap():
    """Pins the two caps as deliberately different — BOUNDED_ID_CAP exists specifically because an
    id/path is not prose and should be capped shorter than FREE_TEXT_CAP."""
    assert ledger.BOUNDED_ID_CAP < ledger.FREE_TEXT_CAP


# ----------------------------------------------------------------- post-review fix: shared newline helper


def test_reject_newline_returns_none_for_a_clean_value():
    assert ledger.reject_newline("clean single line", "--reason") is None


def test_reject_newline_names_the_field_and_says_why():
    msg = ledger.reject_newline("a\nb", "--reason")
    assert msg is not None
    assert "--reason" in msg
    assert "newline" in msg
    assert "single-line" in msg


# --------------------------------------------------------------- #540: per-host writer identity
# Two hosts authenticated as the SAME login (one shared bot account, several machines / containers)
# both resolve to the same `actor`, and a fresh container pid namespace hands out low pids, so both
# regularly draw the SAME pid. Pre-#540 that made their writer identity identical in three places at
# once, and `entries/*.jsonl merge=union` lands both files' lines with no conflict and exit 0:
#   * `entry_file()` -> the same filename, so the two hosts' lines interleave in one file;
#   * the entry `id` -> the same `actor:pid` writer segment, so `watch_classify.classify()` skips
#     host B's seqs as "already seen" (that check runs BEFORE the signature check, so a hand-off
#     addressed to a teammate is dropped permanently, not merely deduped);
#   * `claim_belongs_to_me()` -> `holder_writer == my_writer` short-circuits True, re-opening the
#     #374 race ACROSS machines.
# Every test below drives the host component explicitly rather than trusting the real hostname, so
# they are hermetic and identical on a dev box and in CI.


def _as_host(monkeypatch, token):
    """Pin the per-host component, the way these tests already pin `os.getpid`.

    `raising=False` on purpose: without it these tests go red on a bare AttributeError, which
    proves only that a helper is missing. Letting the patch be a no-op against code that does not
    consult a host makes each test fail on the REAL mechanism instead — two hosts collapsing to one
    filename, one id, one writer identity."""
    monkeypatch.setattr(ledger, "_host_token", lambda config=None: token, raising=False)


def test_entry_file_differs_between_two_hosts_sharing_one_login_and_pid(tmp_path, monkeypatch):
    d = _sdlc(tmp_path, ON)
    monkeypatch.setattr(os, "getpid", lambda: 42)        # the colliding pid both hosts drew
    _as_host(monkeypatch, "aaaaaaaa")
    host_a = ledger.entry_file(d, "dana")
    _as_host(monkeypatch, "bbbbbbbb")
    host_b = ledger.entry_file(d, "dana")
    assert host_a != host_b, "both hosts would append to one file and union-merge would interleave them"


def _two_hosts_one_handoff_each(tmp_path, monkeypatch):
    """Each host is its OWN clone (its own `.sdlc`), which is the whole point: each writes seq 1
    into what it believes is its own file. Union-merge then lands both sets of lines in one
    directory, so the reader sees them together — modelled here by returning both entries."""
    monkeypatch.setattr(os, "getpid", lambda: 42)        # the colliding pid both hosts drew
    _as_host(monkeypatch, "aaaaaaaa")
    first = ledger.append(_sdlc(tmp_path / "hostA", ON), ON, "handoff", "0001-a.md", to="rae")
    _as_host(monkeypatch, "bbbbbbbb")
    second = ledger.append(_sdlc(tmp_path / "hostB", ON), ON, "handoff", "0002-b.md", to="rae")
    return first, second


def test_entry_ids_differ_between_two_hosts_sharing_one_login_and_pid(tmp_path, monkeypatch):
    first, second = _two_hosts_one_handoff_each(tmp_path, monkeypatch)
    # both are seq 1 of their own clone's file; only the writer segment can tell them apart
    assert first["id"] != second["id"]


def test_second_hosts_handoff_is_not_dropped_by_the_watcher_cursor(tmp_path, monkeypatch):
    """The consequence that actually loses work: `classify()` skips `seq <= cursor[writer][stream]`
    BEFORE the signature check, so once host A's seq 1 is in the cursor, host B's seq 1 is gone for
    good — the recipient never sees the hand-off, and no later tick can recover it."""
    classify = _mod("watch_classify")
    first, second = _two_hosts_one_handoff_each(tmp_path, monkeypatch)

    surfaced_a, cursor = classify.classify([first], dict(classify.EMPTY_CURSOR), "rae")
    assert [e["goal"] for e in surfaced_a] == ["0001-a.md"]
    surfaced_b, _ = classify.classify([second], cursor, "rae")
    assert [e["goal"] for e in surfaced_b] == ["0002-b.md"], "host B's hand-off never reached rae"


def test_claim_belongs_to_me_does_not_short_circuit_across_hosts(tmp_path, monkeypatch):
    """Same actor, same pid, different machine: that claim is emphatically NOT mine to resume."""
    monkeypatch.setattr(os, "getpid", lambda: 42)
    _as_host(monkeypatch, "aaaaaaaa")
    theirs = ledger.my_writer(ON)
    _as_host(monkeypatch, "bbbbbbbb")
    mine = ledger.my_writer(ON)
    assert theirs != mine
    assert ledger.claim_belongs_to_me("dana", theirs, "dana", mine) is False


def test_writer_pid_still_reads_the_pid_out_of_the_new_writer_shape(tmp_path, monkeypatch):
    """The host rides WITH the pid, so `claim_belongs_to_me`'s liveness check must still find it —
    otherwise every same-host sibling claim degrades to the legacy always-mine branch and #374
    quietly reopens locally while being fixed globally."""
    monkeypatch.setattr(os, "getpid", lambda: 4242)
    _as_host(monkeypatch, "aaaaaaaa")
    assert ledger.writer_pid(ledger.my_writer(ON)) == 4242


def test_files_for_finds_a_writer_file_carrying_the_host_component(tmp_path, monkeypatch):
    """`sync.py`'s publish/bootstrap stage exactly the files `files_for()` names — if the new
    filename shape falls out of that match, the ledger silently stops publishing."""
    d = _sdlc(tmp_path, ON)
    monkeypatch.setattr(os, "getpid", lambda: 42)
    _as_host(monkeypatch, "aaaaaaaa")
    ledger.append(d, ON, "note", "g.md")
    written = ledger.entry_file(d, "dana")
    assert ledger.files_for(ledger.entries_dir(d), "dana") == [written]


def test_files_for_still_refuses_a_different_actors_prefix_match(tmp_path, monkeypatch):
    """The exact-match guard the #488 docstring calls out ("team" must not match "team-bot") has to
    survive the extra segment."""
    d = _sdlc(tmp_path, ON)
    entries = ledger.entries_dir(d)
    entries.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(os, "getpid", lambda: 42)
    _as_host(monkeypatch, "aaaaaaaa")
    theirs = ledger.entry_file(d, "team-bot")
    theirs.write_text(json.dumps({"id": "team-bot:x:1", "ts": "2026-01-01T00:00:00Z",
                                   "actor": "team-bot", "kind": "note", "goal": "g"}) + "\n")
    assert ledger.files_for(entries, "team") == []


# --- sync._carry: the ops-branch attach must never destroy unpublished local entries -------------


def _sync_mod():
    import importlib.util, pathlib as _pl
    spec = importlib.util.spec_from_file_location(
        "sync", _pl.Path(__file__).resolve().parent.parent
        / "skills" / "sigma-loop" / "scripts" / "sync.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def test_carry_merges_a_directory_that_exists_on_both_sides(tmp_path):
    """The data-loss bug, reproduced. `init` moves an existing `.sdlc/ledger` aside, checks out the
    ops branch there, then carries the old contents back — and the ops branch ALWAYS ships an
    `entries/` directory. The old flat loop skipped any top-level name that already existed, so it
    skipped `entries` itself and `_prune` then deleted it: every unpublished entry gone, with the
    command reporting success. Measured on a real clone: 202 files / 263 entries destroyed by one
    `bootstrap`."""
    sync = _sync_mod()
    staging, dest = tmp_path / "_carry", tmp_path / "ledger"
    (staging / "entries").mkdir(parents=True)
    (staging / "entries" / "mine-1.jsonl").write_text('{"kind":"claimed"}\n')
    (staging / "entries" / "mine-2.jsonl").write_text('{"kind":"done"}\n')
    (dest / "entries").mkdir(parents=True)                       # the branch already has this dir
    (dest / "entries" / "teammate.jsonl").write_text('{"kind":"done"}\n')

    sync._carry(staging, dest)

    names = sorted(p.name for p in (dest / "entries").iterdir())
    assert names == ["mine-1.jsonl", "mine-2.jsonl", "teammate.jsonl"], names


def test_carry_never_clobbers_a_file_the_branch_already_publishes(tmp_path):
    """A colliding FILE keeps the branch's copy. The local one is unpublished by definition, and
    overwriting published content with it would be a different data loss in the other direction."""
    sync = _sync_mod()
    staging, dest = tmp_path / "_carry", tmp_path / "ledger"
    staging.mkdir(); dest.mkdir()
    (staging / "README.md").write_text("local")
    (dest / "README.md").write_text("published")

    sync._carry(staging, dest)
    assert (dest / "README.md").read_text() == "published"


def test_carry_moves_anything_the_branch_does_not_have(tmp_path):
    """The case that always worked, kept as the control."""
    sync = _sync_mod()
    staging, dest = tmp_path / "_carry", tmp_path / "ledger"
    staging.mkdir(); dest.mkdir()
    (staging / "notes.md").write_text("keep me")
    (staging / "events").mkdir()
    (staging / "events" / "mine.jsonl").write_text("{}\n")

    sync._carry(staging, dest)
    assert (dest / "notes.md").read_text() == "keep me"
    assert (dest / "events" / "mine.jsonl").exists()


def test_append_scrubs_a_secret_bearing_ci_check_name_in_ci_observed_checks(tmp_path):
    """#5 (review round 4). `ci_observed.checks` is externally sourced (GitHub check-run names),
    but its `name` field bypassed the scrubber entirely -- `elif kind == "ci_observed" and name ==
    "checks": value = value` -- while every other externally sourced string in this module is
    scrubbed. A token-shaped check name reached the journal verbatim. `conclusion` is a closed
    enum already validated, so it is left untouched; only `name` is scrubbed and capped."""
    d = _sdlc(tmp_path, ON)
    SECRET = "AK" "IAIOSFODNN7EXAMPLE"
    e = ledger.append(d, ON, "ci_observed", "g.md", stream="events",
                      observation_key="a" * 64, pr=7, head_sha="b" * 40, gate_verdict="pass",
                      checks_total=1, checks_truncated=False,
                      checks=[{"name": f"deploy-{SECRET}", "conclusion": "pass"}])
    assert SECRET not in json.dumps(e["checks"])
    assert "[REDACTED" in e["checks"][0]["name"]
    assert len(e["checks"][0]["name"]) <= ledger.BOUNDED_ID_CAP
    assert e["checks"][0]["conclusion"] == "pass"   # the closed enum survives untouched


def test_append_leaves_an_ordinary_ci_check_name_byte_identical(tmp_path):
    """Non-vacuity: a real check name with no secret shape is not mangled by the new scrub."""
    d = _sdlc(tmp_path, ON)
    e = ledger.append(d, ON, "ci_observed", "g.md", stream="events",
                      observation_key="a" * 64, pr=7, head_sha="b" * 40, gate_verdict="pass",
                      checks_total=1, checks_truncated=False,
                      checks=[{"name": "unit-tests / build (ubuntu-latest)", "conclusion": "fail"}])
    assert e["checks"][0]["name"] == "unit-tests / build (ubuntu-latest)"
    assert e["checks"][0]["conclusion"] == "fail"


# --- Review round 5 (author-blind Claude subagent, generation a80caf88 at 0e8736d6) -------------------

def test_append_never_truncates_a_schema_valid_merge_observed_subject(tmp_path):
    """The scrub-and-cap step ran AFTER `_validate_typed_event` had already accepted `subject`
    against its real schema bound (contract/validate.py: 1-256 chars), but capped it at the
    generic BOUNDED_ID_CAP (120) meant for short ids -- silently corrupting an already-validated
    fact with no error anywhere, and the truncated value still passes validate.py on read-back."""
    d = _sdlc(tmp_path, ON)
    subject = "feature/" + "x" * 200   # a real, if verbose, branch name -- schema-legal at 256
    assert 1 <= len(subject) <= 256
    e = ledger.append(d, ON, "merge_observed", "g.md", stream="events",
                      observation_key="a" * 64, subject_kind="branch", subject=subject,
                      pr=7, merge_sha="b" * 40)
    assert e["subject"] == subject, "a schema-valid subject was silently truncated"


def test_append_never_truncates_a_schema_valid_ci_check_name(tmp_path):
    """Same defect, the other field round 4's own #5 fix gave the wrong cap to: a real CI matrix
    check name (e.g. a long GitHub Actions job name) is schema-legal up to 256 chars."""
    d = _sdlc(tmp_path, ON)
    name = "build (ubuntu-latest, python " + "3.11" * 40 + ")"   # schema-legal, > 120 chars
    assert 1 <= len(name) <= 256
    e = ledger.append(d, ON, "ci_observed", "g.md", stream="events",
                      observation_key="a" * 64, pr=7, head_sha="b" * 40, gate_verdict="pass",
                      checks_total=1, checks_truncated=False,
                      checks=[{"name": name, "conclusion": "pass"}])
    assert e["checks"][0]["name"] == name, "a schema-valid check name was silently truncated"


# --------------------------------------------------------------------------- #895 slice 3b: seam via gh_api

def test_run_gh_delegates_to_gh_api_run_gh_with_a_short_cap(monkeypatch):
    """RED BEFORE THE CHANGE (delegation spy): returns stdout stripped, and passes timeout=15 (a hook reaches it)."""
    seen = []
    monkeypatch.setattr(ledger._gh_api(), "run_gh", lambda args, timeout=120: (seen.append((list(args), timeout)), (0, " me\n", ""))[1])
    assert ledger._run_gh(["api", "user"]) == "me" and seen == [(["api", "user"], 15)]


def test_run_gh_failure_is_an_empty_string_and_actor_falls_back(monkeypatch):
    """CHARACTERISATION (green today): rc != 0 is "", and `actor` then uses $USER."""
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: type("P", (), {"returncode": 1, "stdout": "x", "stderr": "e"})())
    assert ledger._run_gh(["api", "user"]) == ""
    monkeypatch.setenv("USER", "fallbackuser")
    monkeypatch.setattr(ledger, "_ACTOR_CACHE", {}, raising=False)
    assert ledger.actor({}) == "fallbackuser"


def test_run_gh_missing_binary_propagates(monkeypatch):
    """CHARACTERISATION (green today): `actor` is the one catcher, `_run_gh` itself does not swallow."""
    import subprocess

    def boom(argv, **kw):
        raise FileNotFoundError("gh")
    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(FileNotFoundError):
        ledger._run_gh(["api", "user"])
