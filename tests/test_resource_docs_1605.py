"""#1605 - the resource guard docs name what they must, and the evidence files carry the required fields."""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
EVID = ROOT / "docs" / "launch" / "evidence"


def test_parallelism_section_names_hold_workers_and_the_not_covered_list():
    text = (ROOT / "docs" / "parallelism.md").read_text()
    section = text[text.index("### The resource guard"):]
    section = section[:section.index("Two things genuinely grow")]
    for needle in ("HOLD", "{workers}", "What the guard cannot cover", "Windows", "shed", "uncalibrated"):
        assert needle in section, needle


def test_calibration_evidence_files_carry_the_required_fields_and_a_commit():
    for name in ("macos", "linux"):
        text = (EVID / ("resource-guard-%s-calibration.md" % name)).read_text()
        assert re.search(r"Commit: [0-9a-f]{7,40}", text), name
        for field in ("Peak footprint per verify", "Hold events", "Machine reached exhaustion", "Status:"):
            assert field in text, (name, field)


def test_every_test_named_in_the_controls_table_exists():
    text = (EVID / "resource-guard-controls.md").read_text()
    for path, name in re.findall(r"(tests/test_\w+\.py)::(\w+)", text):
        assert ("def %s(" % name) in (ROOT / path).read_text(), (path, name)


def test_parallelism_documents_the_cold_start_bound_and_the_seeding_lever():
    """#1816: the cold-start behaviour and how an operator seeds a ring are written down."""
    text = (ROOT / "docs" / "parallelism.md").read_text()
    section = text[text.index("- **Cold start.**"):]
    section = section[:section.index("- **What the guard cannot cover.**")]
    for needle in ("divided by the derived session cost", "released when it ends", "observed.json", "SIGMA_RESOURCE_DIR"):
        assert needle in section, needle
