#!/usr/bin/env python3
"""Levels 3 and 4: the act-and-tell applier tick (decision rubric, slice 19 of story 988).

One more watch tick. A parked question that nobody answered inside its window is answered by the
approved rule that already pre-fills it, and the rule id is told on the issue. Everything here is
OFF unless `decision_rubric.autonomy.applier.enabled` is the JSON boolean true AND the autonomy and
rulebook parts are open; closed, `tick` returns `[]` and writes nothing, and the watch step spawns
nothing.

- An ask (one question waiting on a person) is one small file `<state>/autonomy/ask-<id>.json`
  (`open_ask`, `answer`). PROVISIONAL: no earlier slice stores asks, so this module owns the record.
- A person's answer inside the window always wins. Past the window an ask is applied only when the
  kind is not a hard-stop kind, `autonomy.level` is at least 3, exactly one sealed approved rule
  matches (`rulebook.match`), and the rolling 24 hour cap is not spent.
- A "no" after an applied answer is a veto: `autonomy.veto` drops the kind one level (and may pause it).
- Level 3 acts and tells (the comment names the rule id). Level 4 is silent only when
  `silent_allowed`: the kind is listed in `allow_silent` and the rule is an approved sealed rule
  (approval already required `autonomy.is_owner`). Every `spot_check_one_in`-th ask still tells.
- `apply` goes only through the atomic gestures: one `swap-label` through `triage.apply_actions`
  (the unpark shape the sweep uses) or `promote.promote`.
- A dead tick shows by age: the tick writes `applier-heartbeat.json`; `tick_is_dead` is true when it
  is missing or older than `stale_after_intervals` watch intervals. The goal simply stays parked.

SCALE: one file per open or recent ask, read by one listing per tick; applied or answered asks older
than a day are pruned by embedded time. The apply loop is serial and bounded by the daily cap.
No model call, no network except the two gestures through the injected source.
"""
import datetime
import hashlib
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import uuid

_HERE = pathlib.Path(__file__).resolve().parent
_DIR = "autonomy"
_HEARTBEAT = "applier-heartbeat.json"
_PREFIX = "ask-"
_PRUNE_AFTER = datetime.timedelta(days=2)
_DAY = datetime.timedelta(days=1)

DEFAULTS = {"allow_silent": [], "window_minutes": 1440, "spot_check_one_in": 5,
            "max_auto_applied_per_day": 3, "stale_after_intervals": 3}
DEFAULT_INTERVAL_SECONDS = 900
GESTURES = ("unpark", "promote")

#: Test seam: `level_fn(sdlc_dir, qkind, area, config) -> int`. None means `autonomy.level`.
level_fn = None


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _sub(config):
    try:
        sub = config["decision_rubric"]["autonomy"]["applier"]
        return sub if isinstance(sub, dict) else {}
    except Exception:
        return {}


def enabled(config):
    try:
        if _sub(config).get("enabled") is not True:
            return False
        return _load("autonomy").enabled(config) and _load("rulebook").enabled(config)
    except Exception:
        return False


def _int(v, default, low=1):
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= low else default


def settings(config):
    """The five keys; a wrong-typed value reads as its default."""
    sub = _sub(config)
    silent = sub.get("allow_silent")
    ok = isinstance(silent, list) and all(isinstance(k, str) and k for k in silent)
    return {"allow_silent": list(silent) if ok else [],
            "window_minutes": _int(sub.get("window_minutes"), DEFAULTS["window_minutes"]),
            "spot_check_one_in": _int(sub.get("spot_check_one_in"), DEFAULTS["spot_check_one_in"]),
            "max_auto_applied_per_day": _int(sub.get("max_auto_applied_per_day"),
                                             DEFAULTS["max_auto_applied_per_day"], low=0),
            "stale_after_intervals": _int(sub.get("stale_after_intervals"), DEFAULTS["stale_after_intervals"])}


def _parse(text):
    try:
        t = datetime.datetime.fromisoformat(str(text))
        return t if t.tzinfo else t.replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def _folder(sdlc_dir):
    return os.path.join(str(sdlc_dir), _DIR)


def _write_atomic(path, body):
    folder = os.path.dirname(path)
    if os.path.islink(folder):
        raise OSError("autonomy folder is a link")
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix="._applier", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(body, sort_keys=True) + "\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _ask_path(sdlc_dir, aid):
    return os.path.join(_folder(sdlc_dir), _PREFIX + aid + ".json")


def open_ask(sdlc_dir, issue, qkind, area, options, config, now, gesture="unpark"):
    """Record one question waiting on a person; returns its id, or None when closed or unusable."""
    try:
        if not enabled(config) or gesture not in GESTURES or not isinstance(qkind, str) or not qkind:
            return None
        if not isinstance(options, list) or not options or not all(isinstance(o, str) for o in options):
            return None
        aid = "%s-%s" % (now.strftime("%Y%m%dT%H%M%S"), uuid.uuid4().hex[:6])
        _write_atomic(_ask_path(sdlc_dir, aid), {"id": aid, "issue": issue, "qkind": qkind,
                                                  "area": area if isinstance(area, str) else "",
                                                  "options": options, "gesture": gesture,
                                                  "opened": now.isoformat(), "state": "pending"})
        return aid
    except Exception:
        return None


def _asks(sdlc_dir):
    out = []
    folder = _folder(sdlc_dir)
    try:
        names = sorted(n for n in os.listdir(folder) if n.startswith(_PREFIX) and n.endswith(".json"))
    except OSError:
        return out
    for n in names:
        path = os.path.join(folder, n)
        try:
            if os.path.islink(path):
                continue
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("id"), str) and data["id"] == n[len(_PREFIX):-5]:
            out.append(data)
    return out


def answer(sdlc_dir, aid, text, config, now):
    """A person's answer. Before the ask is applied it is kept and nothing is applied; after it is
    applied, `no` is a veto. Returns the new state or None. Never raises."""
    try:
        if not enabled(config) or not isinstance(text, str):
            return None
        ask = next((a for a in _asks(sdlc_dir) if a["id"] == aid), None)
        if ask is None:
            return None
        if ask.get("state") == "applied":
            if text.strip().lower() != "no":
                return "applied"
            ask.update(vetoed_at=now.isoformat())
        elif ask.get("state") == "pending":
            ask.update(answer=text, answered_at=now.isoformat(), state="answered")
        else:
            return ask.get("state")
        _write_atomic(_ask_path(sdlc_dir, aid), ask)
        return ask["state"] if ask["state"] != "applied" else "veto-pending"
    except Exception:
        return None


def window_open(ask, config, now):
    """True while the question is still inside its window (nobody may be overridden yet)."""
    opened = _parse(ask.get("opened")) if isinstance(ask, dict) else None
    if opened is None:
        return True  # cannot tell: treat as open, the safe reading
    return now < opened + datetime.timedelta(minutes=settings(config)["window_minutes"])


def _rule_is_approved(rule):
    return isinstance(rule, dict) and rule.get("status") == "approved" and bool(rule.get("confirmed")) \
        and bool(rule.get("seal"))


def silent_allowed(qkind, config, rule=None):
    """Level 4 may act without telling only for a kind listed in `allow_silent`, never a hard-stop
    kind, and only on a rule an owner confirmed (an approved sealed rule: approval needs `is_owner`)."""
    try:
        if not isinstance(qkind, str) or qkind not in settings(config)["allow_silent"]:
            return False
        if _load("hard_stop").is_hardstop_kind(qkind, None, config):
            return False
        return _rule_is_approved(rule)
    except Exception:
        return False


def _interval(config):
    try:
        v = config["ledger"]["watch"]["interval_seconds"]
        return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else DEFAULT_INTERVAL_SECONDS
    except Exception:
        return DEFAULT_INTERVAL_SECONDS


def tick_is_dead(heartbeat, config, now):
    """True when there is no heartbeat or it is older than `stale_after_intervals` watch intervals."""
    if not isinstance(heartbeat, datetime.datetime):
        return True
    limit = datetime.timedelta(seconds=_interval(config) * settings(config)["stale_after_intervals"])
    return now - heartbeat > limit


def read_heartbeat(sdlc_dir):
    try:
        with open(os.path.join(_folder(sdlc_dir), _HEARTBEAT), encoding="utf-8") as fh:
            return _parse(json.load(fh).get("time"))
    except (OSError, ValueError, AttributeError):
        return None


def _level(sdlc_dir, qkind, area, config):
    fn = level_fn
    return fn(sdlc_dir, qkind, area, config) if fn else _load("autonomy").level(sdlc_dir, qkind, area, config)


def _spot_check(aid, config):
    n = settings(config)["spot_check_one_in"]
    return int(hashlib.sha256(aid.encode("utf-8")).hexdigest()[:8], 16) % n == 0


def _applied_in_last_day(asks, now):
    return sum(1 for a in asks if a.get("state") == "applied"
               and (_parse(a.get("applied_at")) or now - 2 * _DAY) > now - _DAY)


def tick(sdlc_dir, config, now, source=None, run=None, apply_actions=True):
    """Plan, and unless `apply_actions` is false apply, one pass. Returns the actions (dicts with a
    `result` of would, done, failed or skipped). Closed gate: `[]`, nothing read, nothing written."""
    if not enabled(config):
        return []
    actions = []
    try:
        asks = _asks(sdlc_dir)
        cap = settings(config)["max_auto_applied_per_day"]
        used = _applied_in_last_day(asks, now)
        rules = _load("rulebook").load(sdlc_dir, config)
        hs = _load("hard_stop")
        for ask in asks:
            if ask.get("state") == "applied" and ask.get("vetoed_at") and not ask.get("veto_logged"):
                actions.append({"kind": "veto", "ask": ask["id"], "issue": ask.get("issue"),
                                "qkind": ask.get("qkind"), "area": ask.get("area", ""), "rid": ask.get("rid")})
                continue
            if ask.get("state") != "pending" or ask.get("answer") is not None:
                continue  # a person's answer wins; nothing else is pending
            qkind, area = ask.get("qkind"), ask.get("area", "")
            if not isinstance(qkind, str) or hs.is_hardstop_kind(qkind, None, config):
                continue
            if window_open(ask, config, now) or used >= cap:
                continue
            if _level(sdlc_dir, qkind, area, config) < 3:
                continue
            m = _load("rulebook").match(qkind, area, ask.get("options"), rules)
            if m is None:
                continue
            rule = next((r for r in rules if r.get("id") == m.rule_id), None)
            level = _level(sdlc_dir, qkind, area, config)
            silent = level >= 4 and silent_allowed(qkind, config, rule) and not _spot_check(ask["id"], config)
            text = "" if silent else ("Applied automatically by rule %s (%s): %s. No answer arrived within "
                                      "the window. Reply no to veto." % (m.rule_id, qkind, m.option))
            actions.append({"kind": ask.get("gesture") if ask.get("gesture") in GESTURES else "unpark",
                            "ask": ask["id"], "issue": ask.get("issue"), "qkind": qkind, "area": area,
                            "option": m.option, "rule_id": m.rule_id, "level": level, "silent": silent,
                            "text": text})
            used += 1
        if apply_actions:
            for a in actions:
                a["result"] = apply(a, sdlc_dir, config, now, source=source, run=run)
        else:
            for a in actions:
                a["result"] = "would"
        _write_atomic(os.path.join(_folder(sdlc_dir), _HEARTBEAT), {"time": now.isoformat()})
        _prune(sdlc_dir, asks, now)
    except Exception as exc:  # a tick never raises
        actions.append({"kind": "error", "error": type(exc).__name__, "result": "failed"})
    return actions


def _prune(sdlc_dir, asks, now):
    for a in asks:
        t = _parse(a.get("applied_at") or a.get("answered_at"))
        if t is not None and a.get("state") in ("answered", "vetoed") and now - t > _PRUNE_AFTER:
            try:
                os.unlink(_ask_path(sdlc_dir, a["id"]))
            except OSError:
                continue


def apply(action, sdlc_dir, config, now, source=None, run=None):
    """Perform one action; returns done, failed or skipped. Never raises. The only GitHub writes are
    the atomic swap through `triage.apply_actions` (unpark) or `promote.promote`, then one comment."""
    try:
        if not enabled(config):
            return "skipped"
        ask = next((a for a in _asks(sdlc_dir) if a["id"] == action.get("ask")), None)
        if ask is None:
            return "skipped"
        if action["kind"] == "veto":
            _load("autonomy").veto(sdlc_dir, action.get("rid"), config, now=now)
            ask.update(state="vetoed", veto_logged=True)
            _write_atomic(_ask_path(sdlc_dir, ask["id"]), ask)
            return "done"
        if ask.get("state") != "pending":
            return "skipped"
        if source is None:
            source = _load("sources").GitHubSource(config, run=run)
        issue = action["issue"]
        if action["kind"] == "promote":
            res = _load("promote").promote(sdlc_dir, config, [issue], source=source, run=run, apply=True)
            if not all(r.get("outcome") == "promoted" for r in res.get("results", [])) or not res.get("results"):
                return "failed"
        else:
            triage = _load("triage")
            add = [source.goal_label]
            remove = [source.parked_label, source.goal_blocked_label, source.in_progress_label]
            swap = {"action": "swap-label", "issue": issue, "detail": triage._swap_detail(add, remove),
                    "add": add, "remove": remove, "result": None, "error": None}
            steps = [swap]
            if getattr(source, "project_enabled", False):
                steps.append({"action": "set-status", "issue": issue, "detail": source.col["ready"],
                              "result": None, "error": None})
            if not all(s["result"] == "done" for s in triage.apply_actions(source, steps, True)):
                return "failed"
        rid = None
        try:
            rec = {"how": "autonomous", "qkind": action["qkind"], "area": action.get("area", ""),
                   "choice": action["option"], "reason": "applied after the window by rule " + action["rule_id"],
                   "rule_id": action["rule_id"], "issue": issue}
            rid = _load("decision_store").append(sdlc_dir, rec, config, now=now)
        except Exception:
            rid = None
        ask.update(state="applied", applied_at=now.isoformat(), rid=rid, rule_id=action["rule_id"],
                   silent=bool(action["silent"]))
        _write_atomic(_ask_path(sdlc_dir, ask["id"]), ask)
        if action["text"]:
            try:
                source.note(issue, action["text"])
            except Exception:
                pass  # the gesture landed; an audit line is not worth reporting it as failed
        return "done"
    except Exception:
        return "failed"


def _read_config(sdlc_dir):
    try:
        with open(os.path.join(str(sdlc_dir), "config.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


USAGE = "usage: applier.py [sdlc_dir]"


def main(argv):
    if argv[1:] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    sdlc_dir = argv[1] if len(argv) > 1 else ".sdlc"
    try:
        acts = tick(sdlc_dir, _read_config(sdlc_dir), datetime.datetime.now(datetime.timezone.utc))
        done = [a for a in acts if a.get("result") == "done"]
        if done:
            print("applier: %d answer(s) applied or vetoed" % len(done))
    except Exception as exc:  # noqa: BLE001 - a watcher tick is never fatal
        print("applier: tick failed (non-fatal): %s" % type(exc).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
