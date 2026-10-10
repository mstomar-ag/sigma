"""Tests for evals/safety/check.py (#1049, slice 1 of #1048, story #806).

Each guard in check.py has a red control here: the check is handed a doctored scrub module (a
`SimpleNamespace` with an edited spec list) and must name the shape. The subprocess tests run the
gesture the module docstring documents, copied out of the docstring, never a stronger one.
"""
import ast
import hashlib
import importlib.util
import re
import shlex
import subprocess
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRUB_PATH = ROOT / "skills" / "sigma-loop" / "scripts" / "scrub.py"
CHECK_PATH = ROOT / "evals" / "safety" / "check.py"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_REAL = _load(SCRUB_PATH, "_t_scrub")
_C = _load(CHECK_PATH, "_t_check")


def _check():
    return _C


def _fake(specs=None, **over):
    """A scrub stand-in: only the live spec list varies unless an override is passed."""
    ns = types.SimpleNamespace(
        _SECRET_PATTERN_SPECS=tuple(_REAL._SECRET_PATTERN_SPECS if specs is None else specs),
        SHAPE_RULES=_REAL.SHAPE_RULES,
        COMMIT_SHAPE_RULES=_REAL.COMMIT_SHAPE_RULES,
        COMMIT_FIXTURE_VALUES=_REAL.COMMIT_FIXTURE_VALUES,
        _REDACTOR_ONLY=_REAL._REDACTOR_ONLY,
        commit_secret_hits=_REAL.commit_secret_hits,
    )
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


def _without(index):
    specs = list(_REAL._SECRET_PATTERN_SPECS)
    del specs[index]
    return specs


def _index(name, nth=0):
    hits = [i for i, s in enumerate(_REAL._SECRET_PATTERN_SPECS) if s[0] == name]
    return hits[nth]


def _row(name, nth=0):
    return [e for e in _C.TABLE if e[0] == name][nth]


def _with_row(name, **cols):
    """TABLE with the first row for `name` replaced; cols are gen/expect/span/hash."""
    out = []
    done = False
    for e in _C.TABLE:
        if e[0] == name and not done:
            done = True
            e = (e[0], cols.get("hash", e[1]), cols.get("gen", e[2]),
                 cols.get("expect", e[3]), cols.get("span", e[4]))
        out.append(e)
    return tuple(out)


def _gesture():
    """The documented line, extracted from the module docstring, as an argv list."""
    for line in _C.__doc__.splitlines():
        if line.strip().startswith("python3 evals/safety/check.py"):
            return shlex.split(line.strip())
    raise AssertionError("no gesture line in the docstring")


def _run(argv, cwd):
    argv = [sys.executable] + argv[1:]
    return subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, timeout=120)


def _scratch_tree(tmp_path, scrub_edit=None, check_edit=None, with_scrub=True):
    (tmp_path / "evals" / "safety").mkdir(parents=True)
    text = CHECK_PATH.read_text()
    if check_edit:
        edited = check_edit(text)
        assert edited != text, "check.py mutation did not take effect"
        text = edited
    (tmp_path / "evals" / "safety" / "check.py").write_text(text)
    if with_scrub:
        (tmp_path / "skills" / "sigma-loop" / "scripts").mkdir(parents=True)
        text = SCRUB_PATH.read_text()
        if scrub_edit:
            edited = scrub_edit(text)
            assert edited != text, "scrub.py mutation did not take effect"
            text = edited
        (tmp_path / "skills" / "sigma-loop" / "scripts" / "scrub.py").write_text(text)


# ---------------------------------------------------------------- the real tree


def test_clean_real_list_has_no_findings():
    assert _check().run(_REAL, _check().TABLE) == []


def test_pin_hash_definition_is_pinned():
    c = _check()
    expected = hashlib.sha256(b"2:abc").hexdigest()[:10]
    assert c.pin_hash(re.compile("abc", re.I)) == expected
    assert len(expected) == 10
    # the interpreter-added unicode bit must not move it; a real flag must
    assert c.pin_hash(re.compile("abc", re.I | re.U)) == expected
    assert c.pin_hash(re.compile("abc")) != expected


# ---------------------------------------------------------------- two-way pin


def test_live_spec_removed_is_named_with_hash():
    c = _check()
    i = _index("aws-key")
    rx = _REAL._SECRET_PATTERN_SPECS[i][1]
    found = c.check_two_way(c.live_specs(_fake(_without(i))), c.TABLE)
    assert len(found) == 1
    assert "aws-key" in found[0] and c.pin_hash(rx) in found[0]


def test_duplicate_name_told_apart_by_hash():
    c = _check()
    first, second = _index("private-key", 0), _index("private-key", 1)
    h0 = c.pin_hash(_REAL._SECRET_PATTERN_SPECS[first][1])
    h1 = c.pin_hash(_REAL._SECRET_PATTERN_SPECS[second][1])
    assert h0 != h1
    found = c.check_two_way(c.live_specs(_fake(_without(second))), c.TABLE)
    assert len(found) == 1 and "private-key" in found[0] and h1 in found[0] and h0 not in found[0]
    # a lost duplicate: two identical live specs against one pinned entry (and the reverse)
    dup = list(_REAL._SECRET_PATTERN_SPECS) + [_REAL._SECRET_PATTERN_SPECS[0]]
    found = c.check_two_way(c.live_specs(_fake(dup)), c.TABLE)
    assert len(found) == 1 and h0 in found[0]
    short = tuple(_C.TABLE) + (_C.TABLE[0],)
    found = c.check_two_way(c.live_specs(_fake()), short)
    assert len(found) == 1 and h0 in found[0]


def test_live_spec_added_unpinned_is_named():
    c = _check()
    extra = ("brand-new-shape", re.compile(r"zzz[0-9]{9}"), "[REDACTED]")
    found = c.check_two_way(c.live_specs(_fake(list(_REAL._SECRET_PATTERN_SPECS) + [extra])), c.TABLE)
    assert len(found) == 1 and "brand-new-shape" in found[0] and c.pin_hash(extra[1]) in found[0]


def test_edited_regex_is_missing_and_unpinned():
    c = _check()
    i = _index("aws-key")
    name, rx, repl = _REAL._SECRET_PATTERN_SPECS[i]
    edited = list(_REAL._SECRET_PATTERN_SPECS)
    new_rx = re.compile(rx.pattern.replace("16", "17"))
    assert new_rx.pattern != rx.pattern
    edited[i] = (name, new_rx, repl)
    found = c.check_two_way(c.live_specs(_fake(edited)), c.TABLE)
    assert len(found) == 2
    assert any(c.pin_hash(rx) in f for f in found) and any(c.pin_hash(new_rx) in f for f in found)


# ---------------------------------------------------------------- subset


def test_shape_rules_not_subset_is_named():
    c = _check()
    ghost = tuple(_REAL.SHAPE_RULES) + (("ghost-shape", re.compile("q")),)
    found = c.check_shape_subset(_fake(SHAPE_RULES=ghost))
    assert len(found) == 1 and "ghost-shape" in found[0]
    # same name, mutated pattern: a name-only subset test would pass this
    mutated = tuple((n, re.compile(rx.pattern + "x") if n == "npm-token" else rx)
                    for n, rx in _REAL.SHAPE_RULES)
    found = c.check_shape_subset(_fake(SHAPE_RULES=mutated))
    assert len(found) == 1 and "npm-token" in found[0]
    assert c.check_shape_subset(_fake()) == []


# ---------------------------------------------------------------- generators


def test_generator_list_loses_entry_is_named():
    c = _check()
    live = c.live_specs(_fake())
    found = c.check_generators(live, _with_row("jwt", gen=None))
    assert len(found) == 1 and "jwt" in found[0]


def test_generator_producing_exempt_value_is_named():
    c = _check()
    exempt = next(iter(_REAL.COMMIT_FIXTURE_VALUES))
    found = c.check_generators(c.live_specs(_fake()), _with_row("aws-key", gen=lambda r: "x " + exempt),
                               exempt=_REAL.COMMIT_FIXTURE_VALUES)
    assert any("aws-key" in f and "exempt" in f for f in found)


def test_generator_drift_unmatched_value_is_named():
    c = _check()
    i = _index("aws-key")
    name, rx, repl = _REAL._SECRET_PATTERN_SPECS[i]
    new_rx = re.compile(r"NEVER-MATCHES-[0-9]{40}")
    edited = list(_REAL._SECRET_PATTERN_SPECS)
    edited[i] = (name, new_rx, repl)
    table = _with_row("aws-key", hash=c.pin_hash(new_rx))
    found = c.check_generators(c.live_specs(_fake(edited)), table)
    assert len(found) == 1 and "aws-key" in found[0] and "drift" in found[0]


def test_empty_span_group_is_named():
    c = _check()
    rx = re.compile(r"(a*)b")
    live = c.live_specs(_fake([("odd-shape", rx, "x")]))
    table = (("odd-shape", c.pin_hash(rx), lambda r: "b", "accepted", 1),)
    found = c.check_generators(live, table)
    assert len(found) == 1 and "odd-shape" in found[0] and "span" in found[0]


# ---------------------------------------------------------------- commit outcomes


def test_commit_expect_disagreement_is_named():
    c = _check()
    found = c.check_commit_outcomes(_fake(), _with_row("gh-token", expect="accepted"))
    assert len(found) == 1 and "gh-token" in found[0]
    blind = _fake(commit_secret_hits=lambda line: [])
    found = c.check_commit_outcomes(blind, c.TABLE)
    assert any("gh-token" in f for f in found) and any("authorization-header" in f for f in found)
    assert not any("credential-assignment-suffix" in f for f in found)


def test_redactor_only_entries_carry_measured_outcomes():
    c = _check()
    assert _row("authorization-header")[3] == "refused-by:credential-assignment"
    assert _row("credential-assignment-suffix")[3] == "accepted"
    assert {"authorization-header", "credential-assignment-suffix"} == set(_REAL._REDACTOR_ONLY)
    assert c.check_commit_outcomes(_fake(), c.TABLE) == []


def test_generators_are_deterministic():
    c = _check()
    for entry in c.TABLE:
        assert c.generate(entry) == c.generate(entry)
    assert len({c.generate(e) for e in c.TABLE}) == len(c.TABLE)


# ---------------------------------------------------------------- output


def test_no_finding_line_contains_a_generated_value(monkeypatch, capsys):
    c = _check()
    flipped = tuple((n, h, g, "accepted" if x == "refused" else "refused", s) for n, h, g, x, s in c.TABLE)
    monkeypatch.setattr(c, "TABLE", flipped)
    assert c.main([]) == 1
    out = capsys.readouterr()
    shape = re.compile(r"^check\.py: FINDING \S+ \S+ [0-9a-f]{10}: ")
    lines = out.out.splitlines()
    assert lines and all(shape.match(line) for line in lines)
    for entry in c.TABLE:
        value = c.generate(entry)
        for piece in (value, value[-10:]):
            assert piece not in out.out and piece not in out.err


def test_main_exit_codes(capsys, tmp_path):
    c = _check()
    assert c.main([]) == 0
    assert re.match(r"^check\.py: ok\b", capsys.readouterr().out)
    # refusals: exit 2, nothing on stdout
    broken_import = tmp_path / "broken.py"
    broken_import.write_text("raise RuntimeError('x')\n")
    no_attr = tmp_path / "noattr.py"
    no_attr.write_text("X = 1\n")
    bad_spec = tmp_path / "badspec.py"
    bad_spec.write_text("_SECRET_PATTERN_SPECS = (('a',),)\nSHAPE_RULES = ()\n")
    for path in (tmp_path / "missing.py", broken_import, no_attr, bad_spec):
        assert c.main([], scrub_path=path) == 2
        got = capsys.readouterr()
        assert got.out == "" and got.err.startswith("check.py: REFUSED")


def test_malformed_table_refuses(monkeypatch, capsys):
    c = _check()
    monkeypatch.setattr(c, "TABLE", c.TABLE[:1] + (("x", "y"),))
    assert c.main([]) == 2
    got = capsys.readouterr()
    assert got.out == "" and "REFUSED" in got.err
    bad = ((c.TABLE[0][0], c.TABLE[0][1], c.TABLE[0][2], "maybe", 0),)
    monkeypatch.setattr(c, "TABLE", bad)
    assert c.main([]) == 2


_NEEDED = ("_SECRET_PATTERN_SPECS", "SHAPE_RULES", "COMMIT_SHAPE_RULES", "commit_secret_hits",
           "COMMIT_FIXTURE_VALUES")


def test_missing_scrub_attribute_refuses_exit_2(monkeypatch, capsys, tmp_path):
    c = _check()
    for attr in _NEEDED:
        src = "\n".join("%s = ()" % a for a in _NEEDED if a not in (attr, "commit_secret_hits"))
        if attr != "commit_secret_hits":
            src += "\ncommit_secret_hits = lambda text: []"
        stub = tmp_path / ("no_%s.py" % attr)
        stub.write_text(src + "\n")
        assert c.main([], scrub_path=stub) == 2, attr
        got = capsys.readouterr()
        assert got.out == "" and got.err.startswith("check.py: REFUSED"), attr


def test_wrong_type_scrub_attribute_refuses(capsys):
    c = _check()
    for attr, bad in (("SHAPE_RULES", 5), ("COMMIT_SHAPE_RULES", None), ("commit_secret_hits", "x"),
                      ("COMMIT_FIXTURE_VALUES", 7)):
        try:
            c.run(_fake(**{attr: bad}), c.TABLE)
        except c.Refusal:
            continue
        raise AssertionError("no refusal for bad %s" % attr)


def test_unexpected_probe_exception_is_exit_2(monkeypatch, capsys):
    c = _check()

    def boom(text):
        raise AttributeError("probe")
    monkeypatch.setattr(c, "load_scrub", lambda path: _fake(commit_secret_hits=boom))
    assert c.main([]) == 2
    got = capsys.readouterr()
    assert got.out == "" and got.err.startswith("check.py: REFUSED")


def test_refused_by_finding_names_rule_and_no_value(monkeypatch, capsys):
    c = _check()
    monkeypatch.setattr(c, "TABLE", _with_row("authorization-header", expect="accepted"))
    assert c.main([]) == 1
    out = capsys.readouterr().out
    assert "refused-by:credential-assignment" in out
    value = c.generate(_row("authorization-header"))
    assert value not in out and value[-10:] not in out


# ---------------------------------------------------------------- the documented gesture


def test_gesture_green_on_real_tree():
    done = _run(_gesture(), ROOT)
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.startswith("check.py: ok")


def test_gesture_red_when_live_spec_removed(tmp_path):
    drop = "\n_SECRET_PATTERN_SPECS = _SECRET_PATTERN_SPECS[:2] + _SECRET_PATTERN_SPECS[3:]\n"
    _scratch_tree(tmp_path, scrub_edit=lambda t: t + drop)
    done = _run(_gesture(), tmp_path)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "aws-key" in done.stdout


def test_gesture_red_when_table_entry_dropped(tmp_path):
    marker = 'if __name__ == "__main__":'
    cut = "TABLE = TABLE[:2] + TABLE[3:]\n"
    _scratch_tree(tmp_path, check_edit=lambda t: t.replace(marker, cut + marker))
    done = _run(_gesture(), tmp_path)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "aws-key" in done.stdout


def test_gesture_refuses_when_scrub_missing(tmp_path):
    _scratch_tree(tmp_path, with_scrub=False)
    done = _run(_gesture(), tmp_path)
    assert done.returncode == 2 and done.stdout == "" and "REFUSED" in done.stderr


def _gesture_exit_2(tmp_path, drop):
    tail = "\n" + drop + "\n"
    _scratch_tree(tmp_path, scrub_edit=lambda t: t + tail)
    done = _run(_gesture(), tmp_path)
    assert done.returncode == 2 and done.stdout == "" and "REFUSED" in done.stderr, done.stdout + done.stderr
    assert "Traceback" not in done.stderr


def test_gesture_refuses_when_shape_rules_deleted(tmp_path):
    _gesture_exit_2(tmp_path, "del SHAPE_RULES")


def test_gesture_refuses_when_commit_hits_deleted(tmp_path):
    _gesture_exit_2(tmp_path, "del commit_secret_hits")


def test_gesture_refuses_when_commit_shape_rules_deleted(tmp_path):
    _gesture_exit_2(tmp_path, "del COMMIT_SHAPE_RULES")


def test_gesture_refuses_when_fixture_values_deleted(tmp_path):
    _gesture_exit_2(tmp_path, "del COMMIT_FIXTURE_VALUES")


def test_help_works():
    done = _run(_gesture() + ["--help"], ROOT)
    assert done.returncode == 0 and "usage" in done.stdout.lower()


# ---------------------------------------------------------------- self-checks on the new files


def test_new_files_pass_commit_secret_hits():
    for path in (CHECK_PATH, Path(__file__)):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            assert _REAL.commit_secret_hits(line) == [], "%s line %d" % (path.name, n)


def test_new_files_clean_under_leak_scan():
    leak = _load(ROOT / "tools" / "leak_scan.py", "_t_leak")
    rules, owner = leak._scrub_rules(), leak.origin_owner()
    for path in (CHECK_PATH, Path(__file__)):
        assert leak.scan_text(path.read_text(), rules, owner) == [], path.name


_BANNED_CALLS = {"open", "mkdir", "rmtree", "write", "write_text", "write_bytes", "unlink", "touch",
                 "run", "Popen", "system", "makedirs"}
_BANNED_MODULES = {"subprocess", "shutil", "tempfile"}


def _banned(source):
    """Names the ast finds that write, spawn, or create directories (never a substring grep)."""
    hits = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            f = node.func
            # a bare `run(...)` is check.py's own entry point; only `x.run(...)` can be a spawn
            name = f.id if isinstance(f, ast.Name) and f.id != "run" else \
                f.attr if isinstance(f, ast.Attribute) else ""
            if name in _BANNED_CALLS:
                hits.append(name)
        elif isinstance(node, ast.Import):
            hits += [a.name for a in node.names if a.name.split(".")[0] in _BANNED_MODULES]
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in _BANNED_MODULES:
            hits.append(node.module)
    return hits


def test_check_py_stays_write_free():
    assert _banned("import subprocess\nopen('f', 'w')\np.mkdir()\nshutil.rmtree(p)\nf.write('x')\n") \
        == ["subprocess", "open", "mkdir", "rmtree", "write"]
    assert _banned(CHECK_PATH.read_text()) == []
