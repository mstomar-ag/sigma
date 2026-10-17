"""Print each file's change zone, whether it is new on this branch, and the one rule that applies.

    zones.py classify [--base REF] [--] <path>...

One output row per path, tab-separated: `<path>\\t<zone>\\t<state>\\t<rule>`.
  zone   legacy | modern | undeclared | unreachable     (from `design.zones` in `.sdlc/config.json`)
  state  new | existing | unknown                        (is the path absent at the branch's fork point)

READ-ONLY. Its only write is the kit-wide script-time record every script makes through
`timing_store.timed_main` (gitignored, under `.sdlc/state/time`); it calls no network and no `gh`; it runs a constant number of `git`
processes whatever the path count (one `rev-parse`, one `merge-base`, one batched `cat-file`).
Nothing infers zones: the owner declares them (docs/change-zones.md).

REFUSAL (exit 2, empty stdout, typed reason on stderr): a path outside the checkout, a path
containing a newline, carriage return or tab (the output is line- and tab-delimited), a `--base`
value starting with a dash, or malformed
`design.zones` (the reason names `design.zones[i]`). Everything else exits 0, including rows whose
state is `unknown` or whose zone is `unreachable`: those are stated gaps, never a silent pass.

A path that starts with `-` must follow `--`: `zones.py classify -- -odd.py`.
"""
import argparse
import fnmatch
import importlib.util
import json
import os
import pathlib
import subprocess
import sys

_HERE = pathlib.Path(__file__).resolve().parent

RULE_LEGACY = "legacy: change only what the goal needs; extend by sprout or wrap at one seam"
RULE_NEW = "follow sound design: structure for the forces the goal names"
RULE_UNDECLARED = "follow the surrounding code"
RULE_UNKNOWN = "unknown: merge-base unresolved; apply no zone rule"
RULE_UNREACHABLE = "unreachable: config unreadable; apply no zone rule"

GIT_TIMEOUT = 10


class Refusal(Exception):
    """A typed refusal: exit 2, nothing on stdout."""


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(args, cwd, stdin=None):
    """(rc, stdout) of one git call. rc is None when git could not run or timed out."""
    try:
        # bytes in and out: a path need not be UTF-8, and surrogateescape round-trips every byte
        p = subprocess.run([os.fsencode("git"), b"-C", os.fsencode(str(cwd)), *map(os.fsencode, args)],
                           input=None if stdin is None else os.fsencode(stdin),
                           capture_output=True, timeout=GIT_TIMEOUT)
        return p.returncode, os.fsdecode(p.stdout)
    except Exception:                   # noqa: BLE001 - git missing or wedged: typed as unknown by callers
        return None, ""


def _toplevel(cwd):
    rc, out = _git(["rev-parse", "--show-toplevel"], cwd)
    if rc != 0 or not out.strip():
        raise Refusal("not inside a git checkout: %s" % cwd)
    return os.path.realpath(out.strip())


def _normalise(arg, cwd, top):
    if "\n" in arg or "\r" in arg or "\t" in arg:
        raise Refusal("path contains a newline, carriage return or tab: the output is "
                      "line-oriented and tab-separated")
    p = arg.replace(os.sep, "/") if os.sep != "/" else arg      # a backslash is a name byte on POSIX
    full = p if os.path.isabs(p) else os.path.join(str(cwd), p)
    base = os.path.basename(full)
    if base in ("", ".", ".."):
        resolved = os.path.realpath(full)
    else:
        # git tracks a symlink ITSELF, so only the parent directory is resolved, never the last part
        resolved = os.path.join(os.path.realpath(os.path.dirname(full)), base)
    rel = os.path.relpath(resolved, top)
    if rel == ".." or rel.startswith(".." + os.sep) or rel == ".":
        raise Refusal("path is outside the checkout: %s" % arg)
    return rel.replace(os.sep, "/")


def _config(top):
    """(config dict | None). None = unreachable (missing, unreadable, malformed, or no root)."""
    try:
        ns = _load("north_star")
        root, why = ns.project_root(top)
        if root is None:
            return None
        state = _load("state")
        return state.load_config(pathlib.Path(root) / ".sdlc")
    except Exception:                   # noqa: BLE001 - every failure is the one typed state
        return None


def _tname(v):
    return "null" if v is None else type(v).__name__


def _validate(cfg):
    """The validated zone list (possibly empty). Refuses a malformed `design.zones`, naming the entry."""
    if "design" not in cfg:
        return []
    design = cfg["design"]
    if not isinstance(design, dict):
        raise Refusal("design must be an object, got %s" % _tname(design))
    if "zones" not in design:
        return []
    zones = design["zones"]
    if not isinstance(zones, list):
        raise Refusal("design.zones must be a list, got %s" % _tname(zones))
    out = []
    for i, e in enumerate(zones):
        where = "design.zones[%d]" % i
        if not isinstance(e, dict):
            raise Refusal("%s must be an object with glob and zone" % where)
        if set(e) != {"glob", "zone"}:
            raise Refusal("%s must have exactly the keys glob and zone, got %s" % (where, sorted(e)))
        if not isinstance(e["glob"], str) or not e["glob"]:
            raise Refusal("%s.glob must be a non-empty string" % where)
        if e["zone"] not in ("legacy", "modern"):
            raise Refusal("%s.zone must be legacy or modern" % where)
        out.append((e["glob"], e["zone"]))
    return out


def _match(zones, rel):
    """Zone of the LAST entry whose glob matches `rel` (case-sensitive; `*` crosses `/`)."""
    for glob, zone in reversed(zones):
        if fnmatch.fnmatchcase(rel, glob):
            return zone
    return "undeclared"


def _find_record_base(top, branch):
    """`<remote>/<base>` from the work record whose `branch` is `branch`, else None. The record is
    found by SCANNING `<common .sdlc>/state/work/*.json` (plain JSON): the branch prefix is configurable,
    so none is assumed."""
    try:
        root, _why = _load("north_star").project_root(top)
        if root is None:
            return None
        for f in sorted((pathlib.Path(root) / ".sdlc" / "state" / "work").glob("*.json")):
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(rec, dict) and rec.get("branch") == branch and rec.get("base"):
                return "%s/%s" % (rec.get("remote") or "origin", rec["base"])
    except Exception:                   # noqa: BLE001 - no record is the typed `unknown`
        return None
    return None


def _fork_point(top, base):
    """Commit id of `git merge-base HEAD <ref>`, or None (every failure reads as unknown)."""
    ref = base
    if ref is None:
        rc, out = _git(["symbolic-ref", "--short", "HEAD"], top)
        if rc != 0 or not out.strip():
            return None                 # detached or unborn HEAD
        ref = _find_record_base(top, out.strip())
        if ref is None:
            return None
    rc, out = _git(["merge-base", "HEAD", ref], top)
    fork = out.strip()
    return fork if rc == 0 and fork else None


_EXISTING_TYPES = ("blob", "tree", "commit", "submodule")
# A path that is a gitlink prints `submodule` when its commit is not in this object store and `commit`
# when it is (measured, git 2.55); a tree entry can only be a `commit` if it is a gitlink.
_GITLINK_TYPES = ("commit", "submodule")


def _newness(top, fork, rels):
    """{rel: new|existing|unknown}. ONE `cat-file --batch-check` over `<fork>:<path>` for each path and
    each of its parent directories. `missing` = new; a second token in blob|tree|commit|submodule =
    existing (a gitlink prints `<oid> submodule`, two fields); anything else, a non-zero rc, or a
    different line count = unknown for every path. A path under a gitlink is unknown, never new."""
    if fork is None:
        return {r: "unknown" for r in rels}
    seen = {}
    for r in rels:
        parts = r.split("/")
        for i in range(1, len(parts) + 1):
            seen["/".join(parts[:i])] = None        # insertion-ordered set: O(1) per key
    keys = list(seen)
    rc, out = _git(["cat-file", "--batch-check"], top,
                   stdin="".join("%s:%s\n" % (fork, k) for k in keys))
    lines = out.split("\n")[:-1] if out.endswith("\n") else out.split("\n")   # not splitlines(): \x0b, \x0c, \x85 are path bytes
    if rc != 0 or len(lines) != len(keys):
        return {r: "unknown" for r in rels}
    kind = {}
    for k, line in zip(keys, lines):
        # Only a `missing` line echoes the key we sent, and a path may hold spaces: compare the whole
        # line to `<key> missing`, never split it. An existing line is `<oid> <type> <size>`, no path.
        toks = line.split()
        if line == "%s:%s missing" % (fork, k):
            kind[k] = "missing"
        elif len(toks) >= 2 and toks[1] in _EXISTING_TYPES:
            kind[k] = toks[1]
        else:
            return {r: "unknown" for r in rels}
    res = {}
    for r in rels:
        parts = r.split("/")
        parents = ["/".join(parts[:i]) for i in range(1, len(parts))]
        if any(kind[p] in _GITLINK_TYPES for p in parents):
            res[r] = "unknown"
        else:
            res[r] = "new" if kind[r] == "missing" else "existing"
    return res


def _rule(zone, state):
    if state == "unknown":
        return RULE_UNKNOWN
    if state == "new" or zone == "modern":
        return RULE_NEW
    return RULE_LEGACY if zone == "legacy" else RULE_UNDECLARED


def classify(paths, cwd, base=None):
    if base is not None and base.startswith("-"):
        raise Refusal("--base must be a ref, not an option: %s" % base)
    top = _toplevel(cwd)
    rels = [_normalise(a, cwd, top) for a in paths]
    cfg = _config(top)
    zones = _validate(cfg) if cfg is not None else []
    state = _newness(top, _fork_point(top, base), rels)
    rows = []
    for rel in rels:
        if cfg is None:        # unreachable and unknown rows print ahead of any zone rule
            rows.append((rel, "unreachable", state[rel], RULE_UNREACHABLE))
        else:
            zone = _match(zones, rel)
            rows.append((rel, zone, state[rel], _rule(zone, state[rel])))
    return rows


def main(argv):
    ap = argparse.ArgumentParser(prog="zones.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("classify", help="print zone, state and rule per path")
    c.add_argument("--base", default=None, help="ref to take the fork point against")
    c.add_argument("paths", nargs="+", help="paths; put -- before any that starts with a dash")
    args = ap.parse_args(argv[1:])
    try:
        rows = classify(args.paths, os.getcwd(), args.base)
    except Refusal as exc:
        print("zones.py: REFUSED: %s" % exc, file=sys.stderr)
        return 2
    sys.stdout.buffer.write(os.fsencode("".join("\t".join(r) + "\n" for r in rows)))
    return 0


if __name__ == "__main__":
    _ts = _load("timing_store")
    sys.exit(_ts.timed_main(main, sys.argv, "zones"))
