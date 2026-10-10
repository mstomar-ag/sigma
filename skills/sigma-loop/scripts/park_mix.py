#!/usr/bin/env python3
"""Park-mix reader and confirmation-label census (#991, decision-rubric slice 1). READ-ONLY.

It measures; it gates nothing. Three readers, all pure over their inputs:

- `read_events` / `mix_from_events`: park events in the local journal (`.sdlc/events/*.jsonl`),
  counted by their recorded `reason_class`.
- `mix_from_comments`: the same count taken from Sigma park or fail comments, for a repo whose
  ledger is off. Only a comment starting with a Sigma park prefix counts; the text after the prefix
  is classified by `loop._reason_class`, the one classifier, never a copy.
- `census`: issues carrying the needs-confirmation label, split by who filed them. A goal held by
  a scope or ownership gate is a gate hold, not an AI filing, so those markers win over the
  follow-up label. Human-typed versus kit-filed is inferred from kit labels alone (provisional).

CLI: `park_mix.py report <sdlc_dir>` prints JSON and writes nothing.
"""
import importlib.util
import json
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent

SCOPE_MARKER = "<!-- sigma:feature-scope-expansion -->"
OWNER_MARKER = "<!-- sigma:feature-ownership -->"
POPULATIONS = ("followup_label", "scope_marker", "ownership_flag", "human_typed", "other")


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def read_events(sdlc_dir):
    """Every park event under `<sdlc_dir>/events/*.jsonl`, oldest file first. Malformed lines and
    unreadable files are skipped; a missing directory is an empty list."""
    out = []
    try:
        paths = sorted((pathlib.Path(sdlc_dir) / "events").glob("*.jsonl"))
    except OSError:
        return out
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                item = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if isinstance(item, dict):
                out.append(item)
    return out


def _bump(counts, key):
    counts[key] = counts.get(key, 0) + 1


def mix_from_events(events):
    counts = {}
    for ev in events or []:
        if isinstance(ev, dict) and ev.get("kind") == "park":
            _bump(counts, str(ev.get("reason_class") or "unknown"))
    return counts


def _body(item):
    return (item.get("body") or "") if isinstance(item, dict) else str(item or "")


def mix_from_comments(comments):
    sources = _load("sources")
    loop = _load("loop")
    counts = {}
    for item in comments or []:
        text = _body(item)
        for prefix in sources.OFFBOARD_COMMENT_PREFIXES:
            if text.startswith(prefix):
                _bump(counts, loop._reason_class(text[len(prefix):]))
                break
    return counts


def _label_names(issue):
    return {(l.get("name") if isinstance(l, dict) else str(l or "")) for l in issue.get("labels") or []}


def census(issues, config):
    handoff = _load("handoff")
    proposed = handoff.proposed_label(config)
    counts = {key: 0 for key in POPULATIONS}
    for issue in issues or []:
        names = _label_names(issue)
        if proposed not in names:
            continue
        text = _body(issue) + "\n" + "\n".join(_body(c) for c in issue.get("comments") or [])
        if SCOPE_MARKER in text:
            counts["scope_marker"] += 1
        elif OWNER_MARKER in text:
            counts["ownership_flag"] += 1
        elif handoff.FOLLOWUP_LABEL in names:
            counts["followup_label"] += 1
        elif any(n == handoff.DEPENDENCY_LABEL or n.startswith("priority:") for n in names):
            counts["other"] += 1
        else:
            counts["human_typed"] += 1
    return counts


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help"):
        sys.stdout.write("usage: park_mix.py report <sdlc_dir>\n")
        return 0
    if len(argv) != 2 or argv[0] != "report":
        sys.stderr.write("usage: park_mix.py report <sdlc_dir>\n")
        return 2
    events = read_events(argv[1])
    sys.stdout.write(json.dumps({"events": mix_from_events(events)}, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
