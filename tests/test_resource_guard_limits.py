"""#1598 — resource_guard: derived limits, the `resource` config block, hysteresis, hold.json.

Controls: the byte-literal scan and each hysteresis clause have a `*_control` test that breaks that one thing
(adds a `4 * GiB` limit; swaps a clause for its naive form) and asserts the check goes red.
"""
import ast
import importlib.util
import json
import math
import os
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills" / "sigma-loop" / "scripts" / "resource_guard.py"
TEMPLATE = ROOT / "skills" / "sigma-init" / "templates" / "config.json.tmpl"
DOC = ROOT / "docs" / "configuration.md"
GIB = 1024 ** 3
MIB = 1024 ** 2


def guard():
    spec = importlib.util.spec_from_file_location("resource_guard_limits_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def smp(g, total=24 * GIB, available=12 * GIB, cpus=8, pressure=1, load1=0.5, swapouts=0, at=0.0):
    return g.Sample(total=total, available=available, swap_used=0, swap_total=0, swapouts=swapouts,
                    swapouts_at=at, pressure=pressure, cpu_count=cpus, load1=load1)


# ------------------------------------------------------------------------------------------ scaling + scan
@pytest.mark.parametrize("total", [8 * GIB, 24 * GIB, 128 * GIB])
def test_memory_limits_scale_with_total(total):
    g = guard()
    one = g.derive(smp(g, total=total, available=total // 2))
    two = g.derive(smp(g, total=2 * total, available=total))
    assert one.reserve == pytest.approx(total * 0.20, abs=1)
    assert one.share_cap == pytest.approx(total * 0.60, abs=1)
    assert two.reserve == pytest.approx(2 * one.reserve, abs=2)
    assert two.share_cap == pytest.approx(2 * one.share_cap, abs=2)
    assert one.uncalibrated is True


@pytest.mark.parametrize("cpus,want", [(2, 1), (14, 2), (64, 8)])
def test_cpu_reserve_scales_with_cores(cpus, want):
    g = guard()
    limits = g.derive(smp(g, cpus=cpus))
    assert limits.cpu_reserve == want
    assert limits.load_hold == pytest.approx(1.0 * cpus)


def _large_constants(source):
    """Every constant-foldable integer expression >= 1 MiB in `source` (names resolved from module-level ints)."""
    tree = ast.parse(source)
    names = {}

    def value(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.Name) and node.id in names:
            return names[node.id]
        if isinstance(node, ast.BinOp):
            left, right = value(node.left), value(node.right)
            if left is None or right is None:
                return None
            ops = {ast.Mult: lambda a, b: a * b, ast.Pow: lambda a, b: a ** b if b < 80 else None,
                   ast.LShift: lambda a, b: a << b if b < 80 else None, ast.Add: lambda a, b: a + b}
            fn = ops.get(type(node.op))
            return fn(left, right) if fn else None
        return None

    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            got = value(stmt.value)
            if got is not None:
                names[stmt.targets[0].id] = got
    found = []
    lines = source.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Constant, ast.BinOp)):
            got = value(node)
            # the C-int pid ceiling of `footprint` is a bound on an identifier, not a resource limit
            if got is not None and got >= MIB and "pid > 2 ** 31 - 1" not in lines[getattr(node, "lineno", 1) - 1]:
                found.append((getattr(node, "lineno", 0), got))
    return found


def test_source_has_no_byte_literal_of_a_mebibyte_or_more():
    assert _large_constants(SCRIPT.read_text(encoding="utf-8")) == []


def test_byte_literal_scan_control_goes_red_on_a_4_gib_literal():
    bad = SCRIPT.read_text(encoding="utf-8") + "\nGiB = 1 << 30\nLIMIT = 4 * GiB\n"
    assert _large_constants(bad), "the scan must see 4 * GiB"


# ------------------------------------------------------------------------------------------------- config
def test_null_or_absent_config_is_derived_without_notes():
    g = guard()
    assert set(g.DEFAULTS) and all(v is None for v in g.DEFAULTS.values())
    for cfg in (None, {}, dict(g.DEFAULTS)):
        resolved, notes = g.resolve_config(cfg)
        assert resolved == g.DEFAULTS and notes == []


def test_valid_override_is_honoured():
    g = guard()
    limits = g.derive(smp(g), {"reserve_fraction": 0.25, "max_share_fraction": 0.5, "cpu_reserve_cores": 3,
                               "load_hold_ratio": 1.5})
    assert limits.reserve == pytest.approx(24 * GIB * 0.25, abs=1)
    assert limits.share_cap == pytest.approx(24 * GIB * 0.5, abs=1)
    assert limits.cpu_reserve == 3 and limits.load_hold == pytest.approx(12.0) and limits.notes == []


BAD = [-0.2, True, False, "0.2", float("nan"), 0, 1, 1.5, float("inf"), -1, [], {}]


@pytest.mark.parametrize("bad", BAD, ids=repr)
def test_twelve_bad_fraction_values_give_derived_plus_a_note(bad):
    g = guard()
    limits = g.derive(smp(g), {"reserve_fraction": bad})
    assert limits.reserve == pytest.approx(24 * GIB * 0.20, abs=1)
    assert len(limits.notes) == 1 and "reserve_fraction" in limits.notes[0]


@pytest.mark.parametrize("key,bad", [("mode", "loud"), ("mode", 3), ("shed", "all"), ("cpu_reserve_cores", 0),
                                      ("cpu_reserve_cores", 1.5), ("load_hold_ratio", -1),
                                      ("swapout_hold_mb_per_s", float("nan")), ("max_goals", True),
                                      ("max_verify_workers", "2"), ("hold_alert_minutes", 0)])
def test_other_bad_values_read_as_derived_with_a_note(key, bad):
    g = guard()
    resolved, notes = g.resolve_config({key: bad})
    assert resolved[key] is None and len(notes) == 1 and key in notes[0]


def test_non_object_block_and_unknown_key_are_noted():
    g = guard()
    assert g.resolve_config("x")[0] == g.DEFAULTS and g.resolve_config("x")[1]
    resolved, notes = g.resolve_config({"bogus": 1})
    assert resolved == g.DEFAULTS and any("bogus" in n for n in notes)


# -------------------------------------------------------------------------------------------- headroom
def test_headroom_is_the_smaller_of_free_room_and_share_room():
    g = guard()
    s = smp(g, total=20 * GIB, available=10 * GIB)  # reserve 4, share cap 12
    assert g.derive(s).headroom == 6 * GIB
    # unmaterialised leases come off the free room; all leases come off the share room
    limits = g.derive(s, leases=[(2 * GIB, False), (3 * GIB, True)])
    assert limits.headroom == min(10 * GIB - 4 * GIB - 2 * GIB, 12 * GIB - 5 * GIB)
    assert g.derive(s, leases=[(10 * GIB, True)]).headroom == 2 * GIB


# --------------------------------------------------------------------------------- pressure + critical
def test_pressure_two_blocks_goal_but_admits_one_verify_worker():
    g = guard()
    limits = g.derive(smp(g, pressure=2))
    assert limits.goal_block and not limits.critical
    assert not limits.admits("goal")
    assert limits.admits("verify", running=0) and not limits.admits("verify", running=1)
    assert guard().derive(smp(g, pressure=1)).admits("goal")


def test_pressure_four_and_low_available_are_each_critical():
    g = guard()
    assert g.derive(smp(g, pressure=4)).critical
    low = g.derive(smp(g, total=20 * GIB, available=1 * GIB))  # reserve 4 GiB, half = 2 GiB
    assert low.critical and not low.admits("verify", running=0)
    assert not g.derive(smp(g, total=20 * GIB, available=3 * GIB)).critical


def test_psi_absent_changes_nothing_and_high_psi_blocks_goals():
    g = guard()
    assert g.derive(smp(g, pressure=None)).goal_block is False
    assert g.derive(smp(g, pressure={"some": 1.0, "full": 0.0})).goal_block is False
    assert g.derive(smp(g, pressure={"some": 80.0, "full": 0.0})).goal_block is True


# ------------------------------------------------------------------------------------------ hysteresis
def _series(g, limits, headrooms, gap=3.0):
    """Samples whose headroom (over the derived limits) is each value, `gap` seconds apart."""
    return [smp(g, total=20 * GIB, available=limits_reserve(limits) + h, at=i * gap) for i, h in enumerate(headrooms)]


def limits_reserve(limits):
    return limits.reserve


COST, SESSION = 2 * GIB, None


def _limits(g):
    return g.derive(smp(g, total=20 * GIB, available=10 * GIB))


def _decide(g, headrooms, prior, gap=3.0, **kw):
    limits = _limits(g)
    return g.decide(_series(g, limits, headrooms, gap), limits, COST, prior, **kw)


def test_dip_then_hover_between_lines_stays_held_until_resume_samples_clear_the_resume_line():
    g = guard()
    limits = _limits(g)
    hold, resume = COST, COST + limits.session_cost
    mid = (hold + resume) // 2
    state = "open"
    state = g.decide(_series(g, limits, [5 * GIB, 5 * GIB, 0]), limits, COST, state).state
    assert state == "hold"
    for tail in ([0, mid], [0, mid, mid], [mid, mid, mid]):
        assert g.decide(_series(g, limits, tail), limits, COST, state).state == "hold"
    assert g.decide(_series(g, limits, [mid, mid, resume]), limits, COST, state).state == "hold"
    assert g.decide(_series(g, limits, [resume, resume, resume]), limits, COST, state).state == "open"


def _check_lowest_of_three(g):
    # one transient dip inside the last three must hold, even though the newest sample is healthy
    assert _decide(g, [5 * GIB, 0, 5 * GIB], "open").state == "hold"
    assert _decide(g, [5 * GIB, 5 * GIB, 5 * GIB], "open").state == "open"


def _check_resume_median(g):
    big = 8 * GIB
    # the higher of the lowest two = median of three: two low samples keep the hold though the newest is high
    assert _decide(g, [0, 0, big], "hold", resume_samples=1).state == "hold"
    assert _decide(g, [0, big, big], "hold", resume_samples=1).state == "open"


def _check_consecutive_count(g):
    big = 8 * GIB
    assert _decide(g, [big], "hold").state == "hold"          # one sample is not resume_samples
    assert _decide(g, [big, big, big], "hold", gap=0.5).state == "hold"   # closer than sample_gap
    assert _decide(g, [big, big, big], "hold", gap=3.0).state == "open"


def test_lowest_of_three_clause():
    _check_lowest_of_three(guard())


def test_lowest_of_three_control():
    g = guard()
    g._hold_level = lambda values: values[-1]
    with pytest.raises(AssertionError):
        _check_lowest_of_three(g)


def test_resume_uses_higher_of_lowest_two():
    _check_resume_median(guard())


def test_resume_median_control():
    g = guard()
    g._resume_level = lambda values: values[-1]
    with pytest.raises(AssertionError):
        _check_resume_median(g)


def test_resume_needs_consecutive_spaced_samples():
    _check_consecutive_count(guard())


def test_consecutive_count_control():
    g = guard()
    g._spaced_run = lambda times, need, gap: True
    with pytest.raises(AssertionError):
        _check_consecutive_count(g)


def test_critical_holds_and_mode_off_or_observe_never_hold():
    g = guard()
    s = smp(g, pressure=4)
    limits = g.derive(s)
    assert g.decide([s], limits, COST, "open").state == "hold"
    for mode in ("off", "observe"):
        lim = g.derive(s, {"mode": mode})
        assert g.decide([s], lim, COST, "open").state == "open"


# ---------------------------------------------------------------------------------------------- swap rate
def _swap_hist(g, rates_mb, gap=10.0):
    page = g.PAGE_SIZE
    out, count = [], 0
    for i, rate in enumerate(rates_mb):
        if i:
            count += int(rate * 1e6 * gap / page)
        out.append(smp(g, available=12 * GIB, swapouts=count, at=i * gap))
    return out


def test_swap_rate_never_holds_by_default():
    g = guard()
    hist = _swap_hist(g, [0, 500, 500])
    limits = g.derive(hist[-1])
    d = g.decide(hist, limits, COST, "open")
    assert d.state == "open" and d.swap_rate > 100


def test_swap_rate_holds_when_configured_and_resumes_at_a_quarter():
    g = guard()
    limits = g.derive(smp(g), {"swapout_hold_mb_per_s": 40})
    hot = _swap_hist(g, [0, 100, 100])
    assert g.decide(hot, limits, COST, "open").state == "hold"
    warm = _swap_hist(g, [0, 20, 20])       # below 40, above a quarter (10)
    assert g.decide(warm, limits, COST, "hold").state == "hold"
    cool = _swap_hist(g, [0, 5, 5])
    assert g.decide(cool, limits, COST, "hold").state == "open"


# ------------------------------------------------------------------------------------------------ hold.json
def test_hold_path_follows_env(tmp_path):
    g = guard()
    assert g.hold_path({"SIGMA_RESOURCE_DIR": str(tmp_path)}) == str(tmp_path / "hold.json")
    p = g.hold_path({"XDG_STATE_HOME": str(tmp_path)})
    assert p == str(tmp_path / "sigma" / "resource" / "hold.json")
    assert g.hold_path({"HOME": str(tmp_path)}).endswith(os.path.join(".local", "state", "sigma", "resource", "hold.json"))


def test_hold_json_round_trip_has_the_contract_fields(tmp_path):
    g = guard()
    path = str(tmp_path / "d" / "hold.json")
    g.write_hold(path, g.Decision("hold", "memory", "memory", 123.0, 140.0))
    rec, notes = g.read_hold(path)
    assert notes == [] and set(rec) == {"state", "since", "reason", "resource", "retry_after"}
    assert rec["state"] == "hold" and rec["since"] == 123.0 and rec["resource"] == "memory"
    assert not [n for n in os.listdir(os.path.dirname(path)) if n != "hold.json"]


def test_writer_killed_mid_write_leaves_the_old_file_and_no_partial_target(tmp_path):
    g = guard()
    path = str(tmp_path / "hold.json")
    g.write_hold(path, g.Decision("hold", "memory", "memory", 1.0, None))

    def die(src, dst):
        raise KeyboardInterrupt("killed between write and rename")

    with pytest.raises(KeyboardInterrupt):
        g.write_hold(path, g.Decision("open", "ok", None, 2.0, None), _replace=die)
    rec, notes = g.read_hold(path)
    assert rec["state"] == "hold" and notes == []
    fresh = str(tmp_path / "new" / "hold.json")
    with pytest.raises(KeyboardInterrupt):
        g.write_hold(fresh, g.Decision("hold", "memory", "memory", 2.0, None), _replace=die)
    rec, notes = g.read_hold(fresh)
    assert rec["state"] == "open" and notes == []


@pytest.mark.parametrize("body", ['{"state": "ho', "", "[]", '{"state": "bogus"}', "\x00\x01"])
def test_truncated_or_corrupt_hold_reads_as_open_plus_a_note(tmp_path, body):
    g = guard()
    path = tmp_path / "hold.json"
    path.write_text(body)
    rec, notes = g.read_hold(str(path))
    assert rec["state"] == "open" and len(notes) == 1 and "hold.json" in notes[0]


# ------------------------------------------------------------------------------------------- docs parity
def _doc_keys():
    return set(re.findall(r"^\| `resource\.(\w+)`", DOC.read_text(encoding="utf-8"), re.M))


def test_documented_keys_equal_the_code_defaults():
    g = guard()
    assert _doc_keys() == set(g.DEFAULTS)


def test_every_documented_row_says_derived_and_uncalibrated():
    rows = [l for l in DOC.read_text(encoding="utf-8").splitlines() if l.startswith("| `resource.")]
    assert rows and all("derived" in r and "uncalibrated" in r for r in rows)


def test_template_carries_the_resource_block_all_null():
    g = guard()
    block = json.loads(TEMPLATE.read_text(encoding="utf-8"))["resource"]
    assert block == g.DEFAULTS and all(math.isnan(0.0) is False and v is None for v in block.values())


# ------------------------------------------------------------------------------------------------ #1714
def test_repeat_hold_keeps_the_original_since():
    g = guard()
    first = _decide(g, [-GIB, -GIB, -GIB], "open", now=100.0)
    again = _decide(g, [-GIB, -GIB, -GIB], "hold", now=200.0, prior_since=first.since)
    assert first.state == again.state == "hold"
    assert again.since == first.since == 100.0


def test_empty_history_is_open_not_a_bare_value_error():
    g = guard()
    for prior in ("open", "hold"):
        assert g.decide([], _limits(g), COST, prior, now=5.0).state == "open"


def test_failed_hold_write_leaves_no_temp_and_names_are_per_process(tmp_path):
    g = guard()
    path = str(tmp_path / "hold.json")

    def die(src, dst):
        raise OSError("boom")

    with pytest.raises(OSError):
        g.write_hold(path, g.Decision("open", "ok", None, 2.0, None), _replace=die)
    assert os.listdir(tmp_path) == []
    seen = []
    g.write_hold(path, g.Decision("open", "ok", None, 2.0, None), _replace=lambda s, d: (seen.append(s), os.replace(s, d)))
    assert seen[0] == path + "." + str(os.getpid()) + ".tmp"


def test_oversized_cpu_reserve_reads_as_derived_with_a_note():
    g = guard()
    limits = g.derive(smp(g, cpus=8), {"cpu_reserve_cores": 8})
    assert limits.cpu_reserve == 1
    assert any("cpu_reserve_cores" in n for n in limits.notes)
    ok = g.derive(smp(g, cpus=8), {"cpu_reserve_cores": 7})
    assert ok.cpu_reserve == 7 and not ok.notes
