#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run the resource-guard controls: apply each listed break to a TEMP COPY and require its test to go red (#1769).

Usage: ``python3 tools/resource_controls.py [--only ID]``. Breaks are read from
``tests/resource_controls/controls.json`` (exact-text patches, one per row of
``docs/launch/evidence/resource-guard-controls.md``). The checkout is never written: ``skills/`` and ``tests/`` are
copied into a temporary folder, the break is applied there, and the named test runs there. Exit 0 only when every
selected break is applied exactly once AND its test fails; a patch that does not apply, or a test that stays green,
exits 1. No network."""
import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = ROOT / "tests" / "resource_controls" / "controls.json"


def load(spec=SPEC):
    return json.loads(pathlib.Path(spec).read_text(encoding="utf-8"))["controls"]


def apply_break(copy_root, control):
    """Apply one exact-text patch under `copy_root`. -> None, or a reason it did not apply."""
    target = pathlib.Path(copy_root) / control["file"]
    if not target.is_file():
        return "file missing: %s" % control["file"]
    text = target.read_text(encoding="utf-8")
    if text.count(control["old"]) != 1:
        return "old text occurs %d times (need exactly 1) in %s" % (text.count(control["old"]), control["file"])
    target.write_text(text.replace(control["old"], control["new"]), encoding="utf-8")
    return None


def _pytest(copy, test):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-x", test],
                          cwd=str(copy), env=env, capture_output=True, text=True, timeout=300)


def _failed_line(out, test):
    """True when pytest reported the named test as FAILED (not a collection/setup ERROR)."""
    node = test.split("[")[0]
    for line in out.splitlines():
        if line.startswith("FAILED "):
            rest = line[len("FAILED "):]
            if rest == node or rest.startswith((node + " ", node + "[")):
                return True
    return False


def run_control(control, root=ROOT):
    """-> (ok, detail). ok only when the unbroken test passes AND, once broken, it reports a FAILED line."""
    with tempfile.TemporaryDirectory(prefix="sigma-controls-") as tmp:
        copy = pathlib.Path(tmp) / "copy"
        shutil.copytree(pathlib.Path(root), copy,
                        ignore=shutil.ignore_patterns(".git", ".sdlc", "__pycache__", ".pytest_cache"))
        base = _pytest(copy, control["test"])
        if base.returncode != 0:
            return False, "baseline not green (pytest exit %d): %s" % (
                base.returncode, (base.stdout + base.stderr)[-200:].strip())
        problem = apply_break(copy, control)
        if problem:
            return False, "did not apply: " + problem
        done = _pytest(copy, control["test"])
        if done.returncode == 0:
            return False, "test stayed green under the break"
        if done.returncode != 1 or not _failed_line(done.stdout, control["test"]):
            return False, "no FAILED line for the test (pytest exit %d), error not a red: %s" % (
                done.returncode, (done.stdout + done.stderr)[-200:].strip())
        return True, "red as required"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", help="run one control id")
    args = parser.parse_args(argv)
    controls = [c for c in load() if not args.only or c["id"] == args.only]
    if not controls:
        print("resource_controls: no control selected")
        return 1
    failed = 0
    for control in controls:
        ok, detail = run_control(control)
        print("%s %s: %s" % ("RED-OK" if ok else "FAIL", control["id"], detail))
        failed += 0 if ok else 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
