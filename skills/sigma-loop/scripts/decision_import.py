#!/usr/bin/env python3
"""Scan past decisions for a later import: extract and report only, writes nothing.

Sources: Claude Code transcript files (an AskUserQuestion call paired with its answer; no other tool
result is read), and issue-comment spans (`sigma:unpark-qa` answers and decision-record lines) when
`decision_rubric.import.comments` is true. No model is called. Usage: decision_import.py scan SDLC_DIR [--profile DIR ...]
"""
import argparse
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import sys

_HERE = pathlib.Path(__file__).resolve().parent
SOURCES = ("transcript", "unpark-qa", "decision-span")
SAMPLE_LINES = 10
CLIP = 60
_PAIR = re.compile(r'"((?:[^"\\]|\\.)*)"="((?:[^"\\]|\\.)*)"')


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / ("%s.py" % name))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _fid(*parts):
    return hashlib.sha1("\x1f".join(str(p) for p in parts).encode("utf-8", "replace")).hexdigest()[:16]


def _text(v, limit=400):
    return " ".join(str(v).split())[:limit] if isinstance(v, (str, int, float)) else ""


def _cand(source, when, question, options, choice, reason, issue, cid):
    return {"source": source, "when": when, "question": question, "options": options,
            "choice": choice, "reason": reason, "issue": issue, "id": cid, "superseded": False}


def _folder_name(sdlc_dir):
    return re.sub(r"[^A-Za-z0-9]", "-", str(pathlib.Path(sdlc_dir).resolve().parent))


def _profiles(config, limits, profile_dirs):
    if profile_dirs:
        return list(profile_dirs)
    if limits["profile_dirs"]:
        return list(limits["profile_dirs"])
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return [base] if base else []


def _answers(content):
    if isinstance(content, list):
        content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    if not isinstance(content, str):
        return {}
    return {q: a for q, a in _PAIR.findall(content)}


def _scan_file(path, name, stats, room):
    """Candidates from one transcript. Only AskUserQuestion tool_use blocks and the tool_result with
    the same id are looked at; every other line's content is dropped unread."""
    pending, out = {}, []
    with open(path, "r", encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            try:
                rec = json.loads(line)
                msg = rec["message"] if isinstance(rec, dict) else None
                content = msg.get("content") if isinstance(msg, dict) else None
                if not isinstance(content, list):
                    if rec is None or not isinstance(rec, dict):
                        raise ValueError("not an object")
                    continue
                when = _text(rec.get("timestamp"), 40)
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "tool_use" and part.get("name") == "AskUserQuestion":
                        qs = (part.get("input") or {}).get("questions")
                        pending[part.get("id")] = (n, when, qs if isinstance(qs, list) else [])
                    elif part.get("type") == "tool_result" and part.get("tool_use_id") in pending:
                        at, asked_when, qs = pending.pop(part["tool_use_id"])
                        answers = _answers(part.get("content"))
                        for i, q in enumerate(qs):
                            if not isinstance(q, dict):
                                continue
                            text = _text(q.get("question"))
                            if text not in answers:
                                continue
                            opts = [_text(o.get("label"), 120) for o in (q.get("options") or [])
                                    if isinstance(o, dict)]
                            out.append(_cand("transcript", when or asked_when, text, opts,
                                             _text(answers[text]), "", None, _fid(name, at, i, text)))
                            if len(out) >= room:
                                return out
            except Exception:
                stats["skipped"] += 1
    return out


def _span(body, legacy, marker_start, marker_end):
    at, sp = legacy.find_marker(body, marker_start)
    if at == -1:
        return None
    stop, _e = legacy.find_marker(body, marker_end, at + len(sp))
    return body[at + len(sp): stop if stop != -1 else len(body)]


def _from_comment(c, issue, legacy, bs):
    body = c.get("body") or ""
    span = _span(body, legacy, bs.UNPARK_QA_START, bs.UNPARK_QA_END)
    if span is None:
        return []
    cid, when = str(c.get("id") or ""), _text(c.get("created_at"), 40)
    if "Decision record" in span:
        f = {}
        for line in span.splitlines():
            m = re.match(r"^- (kind|choice|reason): (.*)$", line.strip())
            if m:
                f[m.group(1)] = _text(m.group(2))
        return [_cand("decision-span", when, f.get("kind", ""), [], f.get("choice", ""),
                      f.get("reason", ""), issue, "%s#0" % cid)]
    out, ask = [], ""
    for line in span.splitlines():
        line = line.strip()
        if line.startswith("_") and line.endswith("_") and len(line) > 2:
            ask = _text(line[1:-1])
        elif line.startswith("- **") and "** — " in line:
            out.append(_cand("unpark-qa", when, ask or _text(line[4:line.index("**", 4)]), [],
                             _text(line.split("** — ", 1)[1]), "", issue, "%s#%d" % (cid, len(out))))
            ask = ""
    return out


def _mark_superseded(cands):
    groups = {}
    for c in cands:
        groups.setdefault((c["issue"], c["question"].lower()), []).append(c)
    for g in groups.values():
        g.sort(key=lambda c: c["when"])
        latest = g[-1]["choice"]
        for c in g[:-1]:
            c["superseded"] = c["choice"] != latest


def scan(sdlc_dir, config, profile_dirs=None, issues=None, comments_fn=None):
    """-> {"candidates", "stats", "refusals"}. Never raises on bad input; reads only."""
    cfgmod = _load("decision_rubric_cfg")
    limits = cfgmod.import_limits(config)
    stats = {s: {"files_read": 0, "candidates": 0, "skipped": 0, "superseded": 0} for s in SOURCES}
    refusals, cands = [], []
    cap = limits["max_candidates"]
    full = False
    for prof in _profiles(config, limits, profile_dirs):
        folder = pathlib.Path(prof) / "projects" / _folder_name(sdlc_dir)
        try:
            files = sorted(folder.glob("*.jsonl"))
        except OSError:
            files = []
        for f in files:
            if full:
                break
            try:
                if f.stat().st_size > limits["max_file_bytes"]:
                    stats["transcript"]["skipped"] += 1
                    refusals.append("%s skipped: over the size cap of %d bytes" % (f.name, limits["max_file_bytes"]))
                    continue
                got = _scan_file(f, f.name, stats["transcript"], cap - len(cands))
                stats["transcript"]["files_read"] += 1
            except Exception:
                stats["transcript"]["skipped"] += 1
                continue
            cands += got
            full = len(cands) >= cap
    if not full and limits["comments"]:
        try:
            legacy, bs, src = _load("legacy"), _load("blocker_scan"), _load("sources")
            read = comments_fn or src.fetch_comments
            for issue in (issues or [])[: limits["max_issues"]]:
                if full:
                    break
                stats["unpark-qa"]["files_read"] += 1
                for c in read(config, issue):
                    for cand in _from_comment(c, issue, legacy, bs) if isinstance(c, dict) else []:
                        if len(cands) >= cap:
                            full = True
                            break
                        cands.append(cand)
                    if full:
                        break
        except Exception:
            stats["unpark-qa"]["skipped"] += 1
    if full:
        refusals.append("candidate cap of %d reached: scan stopped, raise decision_rubric.import.max_candidates" % cap)
    _mark_superseded(cands)
    for c in cands:
        stats[c["source"]]["candidates"] += 1
        stats[c["source"]]["superseded"] += 1 if c["superseded"] else 0
    return {"candidates": cands, "stats": stats, "refusals": refusals}


def candidates(sdlc_dir, config, profile_dirs=None, issues=None, comments_fn=None):
    return scan(sdlc_dir, config, profile_dirs, issues, comments_fn)["candidates"]


def _clip(s, n=CLIP):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="verb", required=True)
    s = sub.add_parser("scan", help="report past-decision candidates (writes nothing)")
    s.add_argument("sdlc_dir")
    s.add_argument("--profile", action="append", default=[], metavar="DIR",
                   help="Claude Code profile directory (repeatable)")
    args = p.parse_args(argv)
    try:
        config = _load("state").load_config(args.sdlc_dir)
    except Exception as exc:  # noqa: BLE001
        print("decision_import: %s" % exc, file=sys.stderr)
        return 1
    out = scan(args.sdlc_dir, config, args.profile or None)
    for name in SOURCES:
        st = out["stats"][name]
        print("%s: files_read=%d candidates=%d skipped=%d superseded=%d" % (
            name, st["files_read"], st["candidates"], st["skipped"], st["superseded"]))
    for r in out["refusals"]:
        print("refused: " + r)
    for c in out["candidates"][:SAMPLE_LINES]:
        print("  - [%s] %s -> %s%s" % (c["source"], _clip(c["question"]), _clip(c["choice"]),
                                       " (superseded)" if c["superseded"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
