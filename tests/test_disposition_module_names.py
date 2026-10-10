"""#1069: module names written in the launch disposition records must be files that exist.

A record that names a module without its real prefix sends a reader to a file that is not there.
Paths with a slash must exist exactly; bare names must match the basename of some repository file."""
import json
import os
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
RECORDS = sorted((ROOT / "docs" / "launch" / "dispositions").glob("*.json"))
_NAME = re.compile(r"[A-Za-z0-9_./-]+\.py\b")


def _basenames():
    names = set()
    for dirpath, dirs, files in os.walk(ROOT):
        rel = pathlib.Path(dirpath).relative_to(ROOT).as_posix()
        dirs[:] = [d for d in dirs if d != ".git" and f"{rel}/{d}".lstrip("./") != ".sdlc/work"]
        names.update(f for f in files if f.endswith(".py"))
    return names


def missing_modules(text, basenames, root=ROOT):
    bad = set()
    for name in set(_NAME.findall(text)):
        if "/" in name:
            if not (root / name).is_file():
                bad.add(name)
        elif name not in basenames:
            bad.add(name)
    return sorted(bad)


def test_every_module_named_in_a_disposition_record_exists():
    assert RECORDS
    base = _basenames()
    for rec in RECORDS:
        text = json.dumps(json.loads(rec.read_text(encoding="utf-8")))
        assert missing_modules(text, base) == [], rec.name


def test_the_check_rejects_a_missing_name():
    base = _basenames()
    assert missing_modules("see upkeep_resolution.py", base) == ["upkeep_resolution.py"]
    assert missing_modules("see skills/sigma-loop/scripts/nope.py", base) == ["skills/sigma-loop/scripts/nope.py"]
    assert missing_modules("see feature_upkeep_resolution.py", base) == []
