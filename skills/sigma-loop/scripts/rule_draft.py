#!/usr/bin/env python3
"""Draft a proposed rule from a learned pattern, tested on held-back decisions (rulebook, goal 1138 of story 988).

Verbs: `list <sdlc_dir>` prints the question kind and area pairs whose derived autonomy level is at
least 2 and whose human answers agree on one option at or above the agreement threshold. `draft
<sdlc_dir> <qkind> <area>` builds a rule from that pair and hands it to `rulebook.propose`, so it
lands as PROPOSED only: approval stays with the owner through `rulebook.approve`. `approve <sdlc_dir> <rule_id>` hands one
proposed rule to `rulebook.approve` as the invoking owner (the login is read, never stored); `draft`
itself never approves, and this module never writes a file of its own.

- Held-back exam: the pair's human answers are split deterministically by a hash of the record id
  (stable under re-ordering). The majority option of one part drafts the rule; the other part is
  the exam, and the draft passes only when the held-out answers also agree with it at the
  threshold. Fail closed: a held-out set smaller than `min_held_out`, a tie, a missing option
  (public records keep only a bank option, so free text never drafts), a hard-stop kind, a level below 2, or a pair that
  already has a rule in the rulebook means NO draft.
- Gated by both existing gates: `rulebook.rules.enabled` and `rulebook.autonomy.enabled`
  (and the records part for the data). Closed means inert: lists nothing, drafts nothing, writes nothing.
- Repository-owned: the rule has no person field; the source record ids are linked in its reason.
- Thresholds, read from `rulebook.rules.draft` (a wrong-typed value reads as its default):
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
        sub = config["rulebook"]["rules"]["draft"]
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


SHAPE_NOTE = "level reached on normalised shape; no single option can be drafted"

MEANING = {"unpark": "this rule will UNPARK after the window",
           "keep-parked": "this rule will KEEP PARKED",
           "none": "this rule only recommends"}


def meaning(qkind, option):
    """The plain line for what a rule with `option` does (derived from the question bank), or None."""
    try:
        return MEANING.get(_load("applier").option_gesture(qkind, option))
    except Exception:
        return None


def split(ids):
    """-> (draft_ids, held_out_ids), by the parity of a hash of each id; order-free and deterministic."""
    draft, held = [], []
    for i in sorted(ids):
        (draft if hashlib.sha256(i.encode("utf-8")).digest()[0] % 2 == 0 else held).append(i)
    return draft, held


def _human(sdlc_dir, qkind, area, config):
    dr = _load("decision_record")
    recs = _load("decision_store").list_by(sdlc_dir, config, qkind=qkind, area=area)
    # An imported record of kind unknown has no real kind behind it (#1239): it never feeds a draft.
    # The key is the one comparison key (#1495), so a draft reads exactly as a hand-written rule would;
    # 'free' answers and promote/demote/ack gestures never feed one.
    return {r["id"]: dr.key_of(r) for r in recs if isinstance(r, dict) and r.get("how") in ("human", "imported")
            and not (r.get("how") == "imported" and r.get("qkind") == "unknown") and dr.is_park_answer(r)
            and isinstance(r.get("id"), str) and dr.key_of(r) and dr.key_of(r) not in dr.UNLEARNABLE_KEYS}


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
        return (qkind not in _load("qkind").QKINDS or qkind == "unknown"
                or _load("hard_stop").is_hardstop_kind(qkind, None, config))
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
                row = {"qkind": q, "area": a, "option": opt, "agreement": share, "level": lvl}
                if _shape_only(q, opt):
                    row["note"] = SHAPE_NOTE        # F-18: only listed rows with a draftable option stay silent
                rows.append(row)
        return rows
    except Exception:
        return []


def _shape_only(qkind, option):
    """True when the agreed key is not a bank option of the kind (a normalised shape), so no rule can name it."""
    try:
        return _load("unpark").bank_option(qkind, option) != option
    except Exception:
        return False


def _refuse(reason):
    return {"status": "refused", "reason": reason}


def draft(sdlc_dir, qkind, area, config, now=None):
    """Draft one PROPOSED rule for the pair, or refuse. Never approves. Never raises."""
    try:
        if not _open(config):
            return _refuse("closed: rulebook, autonomy and records parts must all be enabled")
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
        if _shape_only(qkind, opt):
            return _refuse(SHAPE_NOTE)
        linked = (d_ids + h_ids)[:MAX_LINKED]
        reason = "learned from %d recorded decisions (%d held-out, agreement %.2f): %s%s" % (
            len(answers), len(h_ids), held, ", ".join(linked), " ..." if len(answers) > len(linked) else "")
        path = rb.propose(sdlc_dir, {"qkind": qkind, "area": area, "option": opt, "reason": reason}, config, now=now)
        if path is None:
            return _refuse("the rulebook refused the proposal")
        return {"status": "drafted", "qkind": qkind, "area": area, "option": opt, "sources": linked,
                "held_out": len(h_ids), "held_out_agreement": held, "path": path,
                "meaning": meaning(qkind, opt)}
    except Exception as exc:  # noqa: BLE001 - fail closed
        return _refuse("unreadable: %s" % type(exc).__name__)


def _identity(config):
    """`cross_repo.identity` for whoever runs this: `{"login", "confirmed", "detail", ...}`. Never raises."""
    try:
        return _load("cross_repo").identity(config)
    except Exception as exc:  # noqa: BLE001
        return {"login": None, "confirmed": False, "detail": type(exc).__name__}


def _invoker(config):
    """The authenticated login of whoever runs this, or None."""
    return _identity(config).get("login")


def approve(sdlc_dir, rule_id, config):
    """Approve one proposed rule as the invoking owner. -> (ok, line). Never raises. The login is not stored."""
    try:
        rb = _load("rulebook")
        if not rb.enabled(config):
            return False, "closed: rulebook rules are not enabled; nothing written"
        rule = next((r for r in rb.load(sdlc_dir, config) if r.get("id") == rule_id), None)
        if rule is None:
            return False, "no valid rule with id %s" % rule_id
        if rule.get("status") == "approved":
            return False, "rule %s is already approved" % rule_id
        ident = _identity(config)
        login = ident.get("login")
        if not login:
            return False, "cannot resolve who is approving; rule %s stays proposed" % rule_id
        if ident.get("confirmed") is False:
            return False, ("cannot confirm who is approving (gh reports %s: %s); rule %s stays proposed"
                           % (login, ident.get("detail") or "identity unconfirmed", rule_id))
        if not _load("autonomy").is_owner(login, config):
            return False, rb.park_line(rule_id).replace("\n", " ")
        rb.approve(sdlc_dir, rule_id, login, config)
        said = meaning(rule.get("qkind"), rule.get("option"))
        return True, rule_id + (": " + said if said else "") + " (approved by %s)" % login
    except Exception as exc:  # noqa: BLE001 - fail closed
        return False, "unreadable: %s" % type(exc).__name__


def _owner_login(config):
    """-> (login, None) for a confirmed owner, else (None, refusal line). Never raises."""
    ident = _identity(config)
    login = ident.get("login")
    if not login:
        return None, "cannot resolve who is acting"
    if ident.get("confirmed") is False:
        return None, "cannot confirm who is acting (gh reports %s: %s)" % (login, ident.get("detail") or "identity unconfirmed")
    if not _load("autonomy").is_owner(login, config):
        return None, "only an owner may do this"
    return login, None


def due(sdlc_dir, config, now=None):
    """`[(rule_id, age_days or None)]` for approved rules past `rulebook.reask_days` (F-04). [] when closed."""
    try:
        import datetime
        rb = _load("rulebook")
        return rb.due_ages(rb.load(sdlc_dir, config), now or datetime.datetime.now(datetime.timezone.utc), config)
    except Exception:
        return []


def reconfirm(sdlc_dir, rule_id, config):
    """Re-stamp one approved rule's confirmation and seal as the invoking owner. -> (ok, line). Never raises."""
    try:
        rb = _load("rulebook")
        if not rb.enabled(config):
            return False, "closed: rulebook rules are not enabled; nothing written"
        rule = next((r for r in rb.load(sdlc_dir, config) if r.get("id") == rule_id), None)
        if rule is None:
            return False, "no valid rule with id %s" % rule_id
        if rule.get("status") != "approved":
            return False, "rule %s is not approved; use approve" % rule_id
        login, why = _owner_login(config)
        if why:
            return False, "%s; rule %s unchanged" % (why, rule_id)
        rb.approve(sdlc_dir, rule_id, login, config)
        return True, "%s reconfirmed (by %s)" % (rule_id, login)
    except Exception as exc:  # noqa: BLE001 - fail closed
        return False, "unreadable: %s" % type(exc).__name__


def retire(sdlc_dir, rule_id, config, reject=False):
    """Remove one rule as the invoking owner, so drafting is allowed again for its pair (M-11).
    `reject` refuses an approved rule (only a proposal is rejected). -> (ok, line). Never raises."""
    try:
        rb = _load("rulebook")
        if not rb.enabled(config):
            return False, "closed: rulebook rules are not enabled; nothing written"
        login, why = _owner_login(config)
        if why:
            return False, "%s; rule %s unchanged" % (why, rule_id)
        try:
            rb.retire(sdlc_dir, rule_id, login, config, only_proposed=reject)
        except KeyError:
            return False, "no rule with id %s" % rule_id
        except ValueError:
            return False, "rule %s is approved; use retire" % rule_id
        return True, "%s %s (by %s)" % (rule_id, "rejected" if reject else "retired", login)
    except Exception as exc:  # noqa: BLE001 - fail closed
        return False, "unreadable: %s" % type(exc).__name__


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="verb", required=True)
    sub.add_parser("list", help="learned patterns at level 2 or more").add_argument("sdlc_dir")
    d = sub.add_parser("draft", help="propose a rule for one pair (never approves)")
    d.add_argument("sdlc_dir")
    d.add_argument("qkind")
    d.add_argument("area")
    sh = sub.add_parser("show", help="print what one rule does: unpark, keep parked, or only recommends")
    sh.add_argument("sdlc_dir")
    sh.add_argument("rule_id")
    ap = sub.add_parser("approve", help="approve one proposed rule (owner only)")
    ap.add_argument("sdlc_dir")
    ap.add_argument("rule_id")
    du = sub.add_parser("due", help="approved rules older than reask_days, with their age")
    du.add_argument("sdlc_dir")
    for name, hp in (("reconfirm", "re-stamp one approved rule (owner only)"),
                     ("retire", "remove one rule (owner only)"),
                     ("reject", "remove one proposed rule (owner only)")):
        sp = sub.add_parser(name, help=hp)
        sp.add_argument("sdlc_dir")
        sp.add_argument("rule_id")
    args = p.parse_args(argv)
    try:
        config = _load("state").load_config(args.sdlc_dir)
    except Exception as exc:  # noqa: BLE001
        print("rule_draft: %s" % exc, file=sys.stderr)
        return 1
    if args.verb == "show":
        rule = next((r for r in _load("rulebook").load(args.sdlc_dir, config) if r.get("id") == args.rule_id), None)
        if rule is None:
            print("rule_draft: no valid rule with id %s" % args.rule_id, file=sys.stderr)
            return 2
        print("%s: %s / %s -> %s (%s); %s" % (rule["id"], rule["qkind"], rule["area"], rule["option"],
                                              rule["status"], meaning(rule["qkind"], rule["option"])))
        return 0
    if args.verb == "list":
        print(json.dumps(list_patterns(args.sdlc_dir, config), indent=2))
        return 0
    if args.verb == "due":
        for rid, age in due(args.sdlc_dir, config):
            print("%s: %s" % (rid, "confirmation unreadable" if age is None else "%d days since confirmed" % age))
        return 0
    if args.verb in ("reconfirm", "retire", "reject"):
        ok, line = (reconfirm(args.sdlc_dir, args.rule_id, config) if args.verb == "reconfirm"
                    else retire(args.sdlc_dir, args.rule_id, config, reject=args.verb == "reject"))
        print(line, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 2
    if args.verb == "approve":
        ok, line = approve(args.sdlc_dir, args.rule_id, config)
        print(line, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 2
    out = draft(args.sdlc_dir, args.qkind, args.area, config)
    print(json.dumps(out, indent=2))
    return 0 if out["status"] == "drafted" else 1


if __name__ == "__main__":
    sys.exit(main())
