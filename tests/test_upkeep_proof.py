"""#917: the recording trap proves that a closed upkeep gate attempts, writes and spawns nothing.

PLANNED TESTS of the pull-request red-to-green gate. Every test here starts by asking `upkeep_support` for the
gate or the template, which assert they exist, so before the gate is written each one fails by AssertionError.
The file is bound by its whole-file hash and is not edited after the red verify. The tests that prove the trap
itself (green before the gate exists) live in `test_upkeep_harness.py`."""
import copy

import upkeep_support as support

OPEN_ENV = support.OPEN_ENV
NEAR_ENABLED = ("true", "yes", 1, 1.0, "True", "on", "1", None, [True], {"x": True}, 0, False)
STRICT = "return type(value) is bool"
#: (a loosened boolean check, the `enabled` value that the loosened check lets through)
MUTANTS = [("return bool(value)", "true"), ("return True", "true"),
           ("return value == True or value == False", 1), ("return isinstance(value, int)", 1),
           ("return value in (True, False, 1, 0)", 1)]


def project_cases():
    template = support.template_cfg()
    cases = [("empty", {}), ("template", copy.deepcopy(template))]
    for value in NEAR_ENABLED:
        cfg = copy.deepcopy(template)
        cfg["upkeep"]["enabled"] = value
        cases.append(("enabled=%r" % (value,), cfg))
    defects = [("floor above ceiling", ("auto", "floor", 50)), ("typo in drift_merges", ("triggers", "drift_merges", "autoo")),
               ("unknown key", ("auto", "flor", 3)), ("section not an object", ("units", None, [])),
               ("every_hours below the minimum interval", ("triggers", "every_hours", 1))]
    for name, (section, key, value) in defects:
        cfg = copy.deepcopy(template)
        cfg["upkeep"]["enabled"] = True
        if key is None:
            cfg["upkeep"][section] = value
        else:
            cfg["upkeep"][section][key] = value
        cases.append((name, cfg))
    for odd in (True, None, "x"):
        cases.append(("block=%r" % (odd,), {"upkeep": odd}))
    cases += [("config=[]", []), ("config='x'", "x")]
    return cases


def machine_cases():
    """Each case is (name, config, environ). Every project-closed case is also run with the other two opt-ins on."""
    template = support.template_cfg()
    open_cfg = {"upkeep": {"enabled": True}, "ledger": {"enabled": True}}
    cases = [("ledger absent", {"upkeep": {"enabled": True}}, OPEN_ENV)]
    for value in (None, False, "true", 1, 0, "yes"):
        cases.append(("ledger.enabled=%r" % (value,), {"upkeep": {"enabled": True}, "ledger": {"enabled": value}}, OPEN_ENV))
    for value in (True, "x", []):
        cases.append(("ledger=%r" % (value,), {"upkeep": {"enabled": True}, "ledger": value}, OPEN_ENV))
    cases.append(("variable unset", dict(open_cfg), {}))
    for value in ("", "0", "true", "yes", "on", " 1", "1 ", "11", 1, True):
        cases.append(("variable=%r" % (value,), dict(open_cfg), {"SIGMA_UPKEEP_JOB": value}))
    flipped = copy.deepcopy(template)
    flipped["upkeep"]["enabled"] = True
    cases.append(("template flipped on (ledger ships null)", flipped, OPEN_ENV))
    for name, cfg in project_cases():
        if isinstance(cfg, dict):
            cfg = dict(cfg, ledger={"enabled": True})
        cases.append(("project-closed: " + name, cfg, OPEN_ENV))
    return cases


def offences(tmp_path, monkeypatch, driver, cases):
    found = []
    for n, (name, config, environ) in enumerate(cases):
        result, trap, diff, _project = support.run_case(tmp_path / ("case%d" % n), monkeypatch, driver, config, environ)
        channels = {"processes": trap.processes, "models": trap.models, "network": trap.network, "writes": trap.writes}
        found += ["%s: %s %r" % (name, channel, seen) for channel, seen in channels.items() if seen]
        if diff != ([], [], []):
            found.append("%s: files changed %r" % (name, diff))
        if not (isinstance(result, dict) and result.get("closed") is True):
            found.append("%s: not a closed result: %r" % (name, result))
    return found


def test_closed_project_door(tmp_path, monkeypatch):
    """Under an empty config, the shipped template and every near-enabled config: no process, model, network or file."""
    g = support.gate()
    driver = support.drivers(g)[("upkeep_probe", "attempt_project")]
    cases = project_cases()
    assert len(cases) >= 20, "the case list must not shrink to nothing"
    assert offences(tmp_path, monkeypatch, driver, [(n, c, None) for n, c in cases]) == []


def test_closed_machine_door(tmp_path, monkeypatch):
    """The same for the machine door, with each of its three opt-ins withheld in turn."""
    g = support.gate()
    driver = support.drivers(g)[("upkeep_probe", "attempt_machine")]
    cases = machine_cases()
    assert len(cases) >= 40, "the case list must not shrink to nothing"
    assert offences(tmp_path, monkeypatch, driver, cases) == []


def _assert_every_channel(result, trap, diff, project):
    assert result == {"ran": True}
    assert trap.processes == [("subprocess.Popen", "upkeep-probe-tool"), ("subprocess.Popen", support.PROBE_MODEL)], trap.processes
    assert trap.models == [support.PROBE_MODEL], trap.models
    assert [event for event, _arg in trap.network] == ["socket.getaddrinfo"], trap.network
    assert str(project / ".sdlc" / "state" / "upkeep" / "probe.json") in trap.writes, trap.writes
    assert any(p.endswith("/.sdlc/state/upkeep/probe.json") for p in diff[0]), diff


def test_enabled_project_door(tmp_path, monkeypatch):
    """The fully enabled control records a process, a model launch, a network lookup and a new file, apart."""
    g = support.gate()
    driver = support.drivers(g)[("upkeep_probe", "attempt_project")]
    _assert_every_channel(*support.run_case(tmp_path, monkeypatch, driver, {"upkeep": {"enabled": True}}))


def test_enabled_machine_door(tmp_path, monkeypatch):
    """The same for the machine door, with all three opt-ins on."""
    g = support.gate()
    driver = support.drivers(g)[("upkeep_probe", "attempt_machine")]
    config = {"upkeep": {"enabled": True}, "ledger": {"enabled": True}}
    _assert_every_channel(*support.run_case(tmp_path, monkeypatch, driver, config, OPEN_ENV))


def test_control_broken_gate(tmp_path, monkeypatch):
    """A gate that runs the body anyway is caught by the trap: all four channels and a new file."""
    support.gate()
    broken = support.gate_with('if not verdict["open"]:', 'if False:')
    for n, (stem, door) in enumerate((("attempt_project", "project"), ("attempt_machine", "machine"))):
        driver = support.drivers(broken)[("upkeep_probe", stem)]
        result, trap, diff, _project = support.run_case(tmp_path / ("b%d" % n), monkeypatch, driver, {}, {})
        assert result == {"ran": True}, door
        assert trap.processes and trap.models and trap.network and trap.writes and diff[0], door


def test_control_mutants(tmp_path, monkeypatch):
    """Each way of loosening the boolean check opens the probe for a near-enabled value, and the trap is not quiet."""
    support.gate()
    escaped = []
    for n, (mutant, value) in enumerate(MUTANTS):
        broken = support.gate_with(STRICT, mutant)
        driver = support.drivers(broken)[("upkeep_probe", "attempt_project")]
        result, trap, diff, _project = support.run_case(tmp_path / ("m%d" % n), monkeypatch, driver,
                                                        {"upkeep": {"enabled": value}})
        if trap.quiet or result != {"ran": True} or not diff[0]:
            escaped.append((mutant, value))
    assert escaped == []


def test_drivers_registered():
    """Every registered entry point has a trap driver, and every driver is registered or the probe."""
    g = support.gate()
    registered = {(stem, fn) for stem, fns in support.REGISTERED_ENTRY_POINTS.items() for fn in fns}
    have = set(support.drivers(g)) - {("upkeep_probe", name) for name in support.PROBE_NAMES}
    assert have == registered, "register every shipped entry point in REGISTERED_ENTRY_POINTS and give it a driver"


def test_note_claim_pinned():
    """The template note says RESERVED exactly while no entry point is registered."""
    note = support.template_cfg()["_upkeep"]
    assert ("RESERVED" in note) == (not support.REGISTERED_ENTRY_POINTS), "rewrite the _upkeep note when a slice registers an entry point"


def test_pick_time_unchanged(tmp_path):
    """The pick-time call list is identical under every upkeep block, shipped or near-enabled, adopted or not."""
    template_block = support.template_cfg()["upkeep"]
    import test_docs as docs

    def calls(root, adopted, block):
        got = docs._calls_for_one_started_goal(root, adopted, upkeep=block)
        return [line.replace(str(root), "<root>") for line in got]
    wrong, n = [], 0
    for adopted in (False, True):
        base = calls(tmp_path / ("base%d" % adopted), adopted, None)
        assert len(base) == (17 if adopted else 4), len(base)
        for block in ({"enabled": True}, {"enabled": "true"}, 1, template_block, {"enabled": True, "auto": {"floor": 50}}):
            n += 1
            got = calls(tmp_path / ("case%d" % n), adopted, block)
            if adopted and support.script("feature_upkeep").enabled({"upkeep": block}):
                # The gate is OPEN and the goal is cut from a unit branch: goal 922 records the tip it was cut from, with
                # exactly ONE added read. Nothing else moves, and the closed gate above stays byte-identical.
                import collections
                extra = list((collections.Counter(got) - collections.Counter(base)).elements())
                if len(got) != len(base) + 1 or len(extra) != 1 or not extra[0].startswith("git rev-parse origin/feature/"):
                    wrong.append((adopted, repr(block)[:30]))
                continue
            if got != base:
                wrong.append((adopted, repr(block)[:30]))
    assert wrong == []
