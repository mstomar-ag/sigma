#!/usr/bin/env python3
"""Draft a proposed rule from a learned pattern, tested on held-back decisions (decision rubric, goal 1138 of story 988).

Verbs: `list <sdlc_dir>` prints the question kind and area pairs whose derived autonomy level is at
least 2 and whose human answers agree on one option at or above the agreement threshold. `draft
<sdlc_dir> <qkind> <area>` builds a rule from that pair and hands it to `rulebook.propose`, so it
lands as PROPOSED only: approval stays with the owner through `rulebook.approve`. This module never
approves and never writes a file of its own.

- Held-back exam: the pair's human answers are split deterministically by a hash of the record id
  (stable under re-ordering). The majority option of one part drafts the rule; the other part is
  the exam, and the draft passes only when the held-out answers also agree with it at the
  threshold. Fail closed: a held-out set smaller than `min_held_out`, a tie, a missing option
  (public records drop the free-text choice; on public visibility `list` and `draft` say so plainly and exit 1), a hard-stop kind, a level below 2, or a pair that
  already has a rule in the rulebook means NO draft.
- Gated by both existing gates: `decision_rubric.rulebook.enabled` and `decision_rubric.autonomy.enabled`
  (and the records part for the data). Closed means inert: lists nothing, drafts nothing, writes nothing.
- Repository-owned: the rule has no person field; the source record ids are linked in its reason.
- Thresholds, read from `decision_rubric.rulebook.draft` (a wrong-typed value reads as its default):
  `min_agreement` (0 to 1, default 0.90, PROVISIONAL) and `min_held_out` (whole number of at least 1,
  default 5, PROVISIONAL).

SCALE: one store listing and one level derivation per pair; a draft links at most MAX_LINKED ids.
"""
import argparse
import collections
import hashlib
import importlib.util
import json
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
DEFAULTS = {"min_agreement": 0.90, "min_held_out": 5}
MIN_LEVEL = 2
MAX_LINKED = 50


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def settings(config):
    try:
        sub = config["decision_rubric"]["rulebook"]["draft"]
        sub = sub if isinstance(sub, dict) else {}
    except Exception:
        sub = {}
    a, h = sub.get("min_agreement"), sub.get("min_held_out")
    return {"min_agreement": a if isinstance(a, (int, float)) and not isinstance(a, bool) and 0 < a <= 1
            else DEFAULTS["min_agreement"],
            "min_held_out": h if isinstance(h, int) and not isinstance(h, bool) and h >= 1
            else DEFAULTS["min_held_out"]}


def _open(config):
    try:
        return (_load("rulebook").enabled(config) and _load("autonomy").enabled(config)
                and _load("decision_store").enabled(config))
    except Exception:
        return False


PUBLIC_REASON = ("cannot draft here: record visibility is public, which drops the recorded choice "
                 "(free text), so no option can be read; draft from a repo on private visibility")


def _public(config):
    """True when the gates are open and records are stored on public visibility. Never raises."""
    try:
        return _open(config) and _load("decision_store")._visibility(config) == "public"
    except Exception:
        return False


def split(ids):
    """-> (draft_ids, held_out_ids), by the parity of a hash of each id; order-free and deterministic."""
    draft, held = [], []
    for i in sorted(ids):
        (draft if hashlib.sha256(i.encode("utf-8")).digest()[0] % 2 == 0 else held).append(i)
    return draft, held


def _human(sdlc_dir, qkind, area, config):
    recs = _load("decision_store").list_by(sdlc_dir, config, qkind=qkind, area=area)
    # An imported record of kind unknown has no real kind behind it (#1239): it never feeds a draft.
    return {r["id"]: r["choice"] for r in recs if isinstance(r, dict) and r.get("how") in ("human", "imported")
            and not (r.get("how") == "imported" and r.get("qkind") == "unknown")
            and isinstance(r.get("id"), str) and isinstance(r.get("choice"), str) and r["choice"]}


def _majority(choices):
    """-> (option, share) for a unique majority; (None, 0.0) for an empty set or a tie."""
    if not choices:
        return None, 0.0
    top = collections.Counter(choices).most_common(2)
    if len(top) > 1 and top[0][1] == top[1][1]:
        return None, 0.0
    return top[0][0], top[0][1] / len(choices)


def _blocked(qkind, config):
    try:
        return qkind not in _load("qkind").QKINDS or _load("hard_stop").is_hardstop_kind(qkind, None, config)
    except Exception:
        return True


def list_patterns(sdlc_dir, config):
    """Pairs at level >= 2 whose answers agree on one option at the threshold. [] when closed. Never raises."""
    try:
        if not _open(config):
            return []
        cfg, au = settings(config), _load("autonomy")
        pairs = sorted({(r.get("qkind"), r.get("area")) for r in
                        _load("decision_store").list_by(sdlc_dir, config)
                        if isinstance(r.get("qkind"), str) and isinstance(r.get("area"), str)})
        rows = []
        for q, a in pairs:
            if _blocked(q, config):
                continue
            lvl = au.level(sdlc_dir, q, a, config)
            opt, share = _majority(list(_human(sdlc_dir, q, a, config).values()))
            if lvl >= MIN_LEVEL and opt is not None and share >= cfg["min_agreement"]:
                rows.append({"qkind": q, "area": a, "option": opt, "agreement": share, "level": lvl})
        return rows
    except Exception:
        return []


def _refuse(reason):
    return {"status": "refused", "reason": reason}


def draft(sdlc_dir, qkind, area, config, now=None):
    """Draft one PROPOSED rule for the pair, or refuse. Never approves. Never raises."""
    try:
        if not _open(config):
            return _refuse("closed: rulebook, autonomy and records parts must all be enabled")
        if _public(config):
            return _refuse(PUBLIC_REASON)
        if not isinstance(qkind, str) or not isinstance(area, str) or _blocked(qkind, config):
            return _refuse("question kind is unknown or a hard-stop kind")
        if _load("autonomy").level(sdlc_dir, qkind, area, config) < MIN_LEVEL:
            return _refuse("level below %d" % MIN_LEVEL)
        rb = _load("rulebook")
        if any(r.get("qkind") == qkind and r.get("area") == area for r in rb.load(sdlc_dir, config)):
            return _refuse("the pair already has a rule")
        cfg, answers = settings(config), _human(sdlc_dir, qkind, area, config)
        d_ids, h_ids = split(list(answers))
        opt, share = _majority([answers[i] for i in d_ids])
        if opt is None or share < cfg["min_agreement"]:
            return _refuse("no option reaches the agreement threshold in the drafting part")
        if len(h_ids) < cfg["min_held_out"]:
            return _refuse("exam: held-out set has %d answers, need %d" % (len(h_ids), cfg["min_held_out"]))
        held = sum(1 for i in h_ids if answers[i] == opt) / len(h_ids)
        if held < cfg["min_agreement"]:
            return _refuse("exam failed: held-out agreement %.2f below %.2f" % (held, cfg["min_agreement"]))
        linked = (d_ids + h_ids)[:MAX_LINKED]
        reason = "learned from %d recorded decisions (%d held-out, agreement %.2f): %s%s" % (
            len(answers), len(h_ids), held, ", ".join(linked), " ..." if len(answers) > len(linked) else "")
        path = rb.propose(sdlc_dir, {"qkind": qkind, "area": area, "option": opt, "reason": reason}, config, now=now)
        if path is None:
            return _refuse("the rulebook refused the proposal")
        return {"status": "drafted", "qkind": qkind, "area": area, "option": opt, "sources": linked,
                "held_out": len(h_ids), "held_out_agreement": held, "path": path}
    except Exception as exc:  # noqa: BLE001 - fail closed
        return _refuse("unreadable: %s" % type(exc).__name__)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="verb", required=True)
    sub.add_parser("list", help="learned patterns at level 2 or more").add_argument("sdlc_dir")
    d = sub.add_parser("draft", help="propose a rule for one pair (never approves)")
    d.add_argument("sdlc_dir")
    d.add_argument("qkind")
    d.add_argument("area")
    args = p.parse_args(argv)
    try:
        config = _load("state").load_config(args.sdlc_dir)
    except Exception as exc:  # noqa: BLE001
        print("rule_draft: %s" % exc, file=sys.stderr)
        return 1
    if args.verb == "list":
        if _public(config):
            print("rule_draft: " + PUBLIC_REASON, file=sys.stderr)
            return 1
        print(json.dumps(list_patterns(args.sdlc_dir, config), indent=2))
        return 0
    out = draft(args.sdlc_dir, args.qkind, args.area, config)
    print(json.dumps(out, indent=2))
    return 0 if out["status"] == "drafted" else 1


if __name__ == "__main__":
    sys.exit(main())
