#!/usr/bin/env python3
"""Per-category autonomy thresholds (goal 1221; library only; no main).

The optional file `.sdlc/categories.json` (schema `sigma/categories@1`) groups repository areas into
categories. A category may carry a `levels` block with the same keys as
`rulebook.autonomy.levels`; precedence is the global levels first, then the first matching
category, key by key. Fail-safe: nothing here raises. A wrong schema reads as no categories; an
invalid entry (missing or duplicate id, unknown threshold key, wrong-typed value) is dropped alone.

SCALE: one small committed file, read whole per call.
"""
import json
import os

SCHEMA = "sigma/categories@1"
FILENAME = "categories.json"
KEYS = ("l1_min_answers", "l2_min_answers", "l3_min_answers", "l4_min_answers",
        "l2_agreement", "l3_agreement", "l4_agreement")


def path(sdlc_dir):
    return os.path.join(str(sdlc_dir), FILENAME)


def exists(sdlc_dir):
    try:
        return os.path.isfile(path(sdlc_dir))
    except Exception:
        return False


def _norm(area):
    a = area if isinstance(area, str) else ""
    return a[5:] if a.startswith("area:") else a


def _good(k, v):
    if isinstance(v, bool):
        return False
    if k.endswith("agreement"):
        return isinstance(v, (int, float)) and 0 < v <= 1
    return isinstance(v, int) and v >= 1


ANY_AREA = "*"      # areas ['*'] covers every area, 'repo' included; an exact area wins

_COUNTS = ("l1_min_answers", "l2_min_answers", "l3_min_answers", "l4_min_answers")
_RATES = ("l2_agreement", "l3_agreement", "l4_agreement")


def order_ok(levels):
    """False when the thresholds present break l1 <= l2 <= l3 <= l4 or l2_agreement <= l3_agreement <= l4_agreement."""
    for chain in (_COUNTS, _RATES):
        vals = [levels[k] for k in chain if k in levels]
        if any(a > b for a, b in zip(vals, vals[1:])):
            return False
    return True


def _strs(v):
    return isinstance(v, list) and all(isinstance(x, str) and x for x in v)


def _entry(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
        return None
    areas = raw.get("areas")
    if not _strs(areas):
        return None
    qkinds = raw.get("qkinds")
    if qkinds is not None and not _strs(qkinds):
        return None
    levels = raw.get("levels", {})
    if not isinstance(levels, dict) or any(k not in KEYS or not _good(k, v) for k, v in levels.items()):
        return None
    if not order_ok(levels):
        return None
    title = raw.get("title")
    words = raw.get("match_words")
    if words is not None and not _strs(words):
        return None
    extra = {"match_words": list(words)} if words else {}
    return dict(extra, **{"id": raw["id"], "title": title if isinstance(title, str) else "",
            "areas": [_norm(a) for a in areas], "qkinds": list(qkinds) if qkinds is not None else None,
            "levels": dict(levels)})


def load(sdlc_dir):
    """The valid categories, in file order. [] for anything unreadable. Never raises."""
    try:
        with open(path(sdlc_dir), encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or data.get("schema") != SCHEMA or not isinstance(data.get("categories"), list):
            return []
        out, seen = [], set()
        for raw in data["categories"]:
            e = _entry(raw)
            if e is None or e["id"] in seen:
                continue
            seen.add(e["id"])
            out.append(e)
        return out
    except Exception:
        return []


def dropped(sdlc_dir):
    """`[(id, reason)]` for entries dropped for breaking the threshold order (a doctor note). Never raises."""
    try:
        with open(path(sdlc_dir), encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or data.get("schema") != SCHEMA or not isinstance(data.get("categories"), list):
            return []
        out = []
        for raw in data["categories"]:
            lv = raw.get("levels") if isinstance(raw, dict) else None
            if (isinstance(lv, dict) and all(k in KEYS and _good(k, v) for k, v in lv.items())
                    and not order_ok(lv) and isinstance(raw.get("id"), str)):
                out.append((raw["id"], "thresholds break the order l1 <= l2 <= l3 <= l4 or l2 <= l3 <= l4 agreement"))
        return out
    except Exception:
        return []


def resolve(sdlc_dir, qkind, area):
    """The first category covering the pair (an exact area before a '*' category), or None. Never raises."""
    try:
        a = _norm(area)
        cats = [c for c in load(sdlc_dir) if c["qkinds"] is None or qkind in c["qkinds"]]
        for c in cats:
            if a in c["areas"]:
                return c
        for c in cats:
            if ANY_AREA in c["areas"]:
                return c
    except Exception:
        pass
    return None
