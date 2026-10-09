"""#917: the shipped config template carries the disabled `upkeep` block, pinned both ways to the gate.

PLANNED TESTS of the pull-request red-to-green gate. Every test here reads the template through
`upkeep_support.template_cfg()`, which asserts the block exists, so before the block is written each one fails
by AssertionError and never by KeyError. The file is bound by its whole-file hash and is not edited after the red
verify."""
import copy

import upkeep_support as support


def test_block_present():
    """The scaffolded config has a disabled `upkeep` block and a string note beside it."""
    cfg = support.template_cfg()
    assert cfg["upkeep"].get("enabled") is False, "the shipped upkeep block must be disabled"
    assert "_upkeep" in cfg and isinstance(cfg["_upkeep"], str), "the upkeep block needs its sibling _upkeep note"


def test_gate_keys_in_template():
    """Every key the gate reads is in the scaffolded block (a discoverability pin)."""
    cfg = support.template_cfg()
    missing, _extra = support.key_gaps(cfg["upkeep"], support.gate().SCHEMA)
    assert not missing, "keys the gate reads are missing from the template upkeep block: %s -- add them to config.json.tmpl" % missing


def test_no_extra_keys():
    """The scaffolded block carries no key the gate does not read."""
    cfg = support.template_cfg()
    _missing, extra = support.key_gaps(cfg["upkeep"], support.gate().SCHEMA)
    assert not extra, "the template upkeep block has keys the gate does not read: %s" % extra


def test_defaults_equal():
    """The scaffolded values are exactly the gate's defaults."""
    cfg = support.template_cfg()
    assert support.flat(cfg["upkeep"]) == support.gate().DEFAULTS


def test_template_block_valid():
    """The shipped block reads closed and clean, and opens only when `enabled` is flipped."""
    cfg = support.template_cfg()
    g = support.gate()
    shipped = g.read(cfg)
    assert shipped.enabled is False and shipped.problems == ()
    flipped = copy.deepcopy(cfg)
    flipped["upkeep"]["enabled"] = True
    reading = g.read(flipped)
    assert reading.enabled is True and reading.problems == ()


def test_control_pin_gaps():
    """The pin sees a missing key and an extra key, and ignores an underscore note."""
    cfg = support.template_cfg()
    block = copy.deepcopy(cfg["upkeep"])
    del block["auto"]["floor"]
    block["auto"]["flor"] = 3
    keys = support.gate().SCHEMA
    assert support.key_gaps(block, keys) == (["auto.floor"], ["auto.flor"])
    block["units"]["_note"] = "a note is not a key"
    assert support.key_gaps(block, keys) == (["auto.floor"], ["auto.flor"])
    assert support.key_gaps(cfg["upkeep"], keys) == ([], [])


def test_note_phrases():
    """The note names both switches, the machine variable, and both typo rules."""
    note = support.template_cfg()["_upkeep"]
    wanted = ("RESERVED", "work.rebase_upkeep", "SIGMA_UPKEEP_JOB", "the JSON boolean true", "FAILS CLOSED",
              "reads as ON", "reads as OFF")
    assert [w for w in wanted if w not in note] == []
