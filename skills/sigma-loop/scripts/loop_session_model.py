#!/usr/bin/env python3
"""The loop session's model: chosen (config), observed (transcript) and checked (verdict).

The session that runs `/sigma-loop` spends its model on every orchestration step, so which model
that is matters. Three small pieces, all local and read-only:

* `effective(cfg)` - the model the supervisor passes as `--model`: `loop.session_model` when
  set, else `sonnet` when `model_selection` is `auto`, else None (no flag). A value outside
  `[A-Za-z0-9._-]+` raises ValueError; the caller refuses loudly.
* `observed(session_id, home)` - the model of the session's latest assistant turn, read from its
  main transcript the way `phase_report.py` reads a phase. None when the host exposes none.
* `verdict(observed, tiers)` - `ok`, `above-tier` (observed priced above EVERY dispatched tier)
  or `unavailable` with a named reason. Never a guess.

Usage: loop_session_model.py check [sdlc_dir] [--warn-only]
Prints one line (nothing at all with --warn-only unless the verdict is above-tier). Exit 0.
"""
import importlib.util
import json
import os
import pathlib
import re
import sys

USAGE = "usage: loop_session_model.py check [sdlc_dir] [--warn-only]"
_HERE = pathlib.Path(__file__).resolve().parent
_VALID = re.compile(r"[A-Za-z0-9._-]+")
_TIERS = ("haiku", "sonnet", "opus", "fable")


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def effective(cfg):
    """The `--model` value for the default session command, or None. Raises ValueError on junk."""
    cfg = cfg if isinstance(cfg, dict) else {}
    block = cfg.get("loop")
    raw = block.get("session_model") if isinstance(block, dict) else None
    if raw is None:
        return "sonnet" if cfg.get("model_selection") == "auto" else None
    if not isinstance(raw, str) or not _VALID.fullmatch(raw):
        raise ValueError(f"loop.session_model is not a valid model name: {raw!r}")
    return raw


def tier_of(model):
    """The price tier a model name belongs to, or None when it names none of the four."""
    low = str(model or "").lower()
    for tier in _TIERS:
        if tier in low:
            return tier
    return None


def observed(session_id, home=None):
    """Model of the latest assistant turn in the session's main transcript, else None."""
    try:
        pr = _load(_HERE / "phase_report.py", "phase_report")
        path = pr.find_claude_main_transcript(session_id, home)
        if path is None:
            return None
        last = None
        for turn in pr.iter_assistant_turns(path):
            last = turn["model"]
        return last
    except Exception:                                    # noqa: BLE001 - advisory, never raises
        return None


#: The read is bounded: only the newest MAX_STREAM_FILES files of each stream, and only the last MAX_STREAM_BYTES of each file.
#: A tier dispatched longer ago than that no longer counts, so an old high-tier dispatch cannot silence the warning for good;
#: the cost is at most MAX_STREAM_FILES * MAX_STREAM_BYTES * 2 bytes however long the history grows.
MAX_STREAM_FILES = 50
MAX_STREAM_BYTES = 256 * 1024


def _tail_lines(path):
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - MAX_STREAM_BYTES))
            data = handle.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[1:] if size > MAX_STREAM_BYTES else lines      # the first line of a cut read may be partial


def dispatched_tiers(sdlc_dir):
    """The tiers named by recent `model_choice` rows: the action log (`state/log`) and the events stream (`events`, where the
    pick-time record goes). Bounded by MAX_STREAM_FILES newest files per stream and MAX_STREAM_BYTES per file."""
    out = set()
    for sub in (("state", "log"), ("events",)):
        base = pathlib.Path(sdlc_dir).joinpath(*sub)
        try:
            files = sorted(base.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)[:MAX_STREAM_FILES]
        except OSError:
            continue
        for path in files:
            for line in _tail_lines(path):
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("kind") == "model_choice":
                    tier = tier_of(row.get("model"))
                    if tier:
                        out.add(tier)
    return out


def verdict(observed_model, tiers):
    """-> (state, text). state is `ok`, `above-tier` or `unavailable`."""
    if not observed_model:
        return "unavailable", "not exposed by this host"
    own = tier_of(observed_model)
    if own is None:
        return "unavailable", f"model {observed_model} names no known price tier"
    known = [t for t in tiers if t in _TIERS]
    if not known:
        return "ok", f"{observed_model}; no tier dispatched yet"
    if _TIERS.index(own) > max(_TIERS.index(t) for t in known):
        return "above-tier", (f"{observed_model} is priced above every dispatched tier "
                              f"({', '.join(sorted(known, key=_TIERS.index))}); set loop.session_model")
    return "ok", f"{observed_model}; within dispatched tiers"


def status(sdlc_dir, session_id=None, home=None):
    """-> (state, text) for this session; the doctor row and the session-start line both use it."""
    sid = session_id if session_id is not None else os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not sid:
        return "unavailable", "not exposed by this host"
    return verdict(observed(sid, home), dispatched_tiers(sdlc_dir))


def main(argv):
    if argv[1:] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    if len(argv) < 2 or argv[1] != "check":
        print(USAGE, file=sys.stderr)
        return 2
    args = [a for a in argv[2:] if a != "--warn-only"]
    state, text = status(args[0] if args else ".sdlc")
    if "--warn-only" in argv[2:] and state != "above-tier":
        return 0
    print(f"loop session model: {text}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
