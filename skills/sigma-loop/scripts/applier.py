#!/usr/bin/env python3
"""Levels 3 and 4: the act-and-tell applier tick (rulebook, slice 19 of story 988).

One more watch tick. A parked question that nobody answered inside its window is answered by the
approved rule that already pre-fills it, and the rule id is told on the issue. Everything here is
OFF unless `rulebook.autonomy.applier.enabled` is the JSON boolean true AND the autonomy and
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
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import time
import uuid

_HERE = pathlib.Path(__file__).resolve().parent
_DIR = "autonomy"
_HEARTBEAT = "applier-heartbeat.json"
_PREFIX = "ask-"
_PRUNE_AFTER = datetime.timedelta(days=2)
_DAY = datetime.timedelta(days=1)

DEFAULTS = {"allow_silent": [], "window_minutes": 1440, "spot_check_one_in": 5,
            "max_auto_applied_per_day": 3, "stale_after_intervals": 3, "prune_after_days": 30}
DEFAULT_INTERVAL_SECONDS = 900
GESTURES = ("unpark", "promote", "keep-parked")


def _text_gesture(option):
    """Legacy text reading (kept for callers with no bank). The gesture a rule's option text means: `keep-parked` when it reads as leaving the issue
    parked (a leading no/don't, or keep/leave/stay/remain/hold with 'park'), else `unpark`
    (unknown text keeps the old meaning)."""
    t = option.lower().replace("'", "").strip() if isinstance(option, str) else ""
    words = t.split()
    if not words:
        return "unpark"
    if words[0] in ("no", "dont", "do") and (words[0] != "do" or words[1:2] == ["not"]):
        return "keep-parked"
    if "park" in t and "unpark" not in t and any(w in words for w in ("keep", "leave", "stay", "remain", "hold")):
        return "keep-parked"
    return "unpark"
def option_gesture(kind, option):
    """What the question bank says an option of `kind` means: `unpark`, `keep-parked`, or `none` (the
    rule only recommends and is never applied). None for text the bank does not list. Derived from the
    bank, never stored in a rule, so a rule's seal does not depend on it (#1495)."""
    try:
        return _load("unpark").option_gestures(kind).get(option)
    except Exception:
        return None


#: Set (process-wide, so a separately loaded copy of this module sees it) while `apply` runs its own
#: gesture, so that gesture does not close the ask it is applying.
_APPLYING_ENV = "SIGMA_APPLIER_APPLYING"

#: Test seam: `level_fn(sdlc_dir, qkind, area, config) -> int`. None means `autonomy.level`.
level_fn = None


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _sub(config):
    try:
        sub = config["rulebook"]["autonomy"]["applier"]
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
    """The six keys; a wrong-typed value reads as its default."""
    sub = _sub(config)
    silent = sub.get("allow_silent")
    ok = isinstance(silent, list) and all(isinstance(k, str) and k for k in silent)
    return {"allow_silent": list(silent) if ok else [],
            "window_minutes": _int(sub.get("window_minutes"), DEFAULTS["window_minutes"]),
            "spot_check_one_in": _int(sub.get("spot_check_one_in"), DEFAULTS["spot_check_one_in"]),
            "max_auto_applied_per_day": _int(sub.get("max_auto_applied_per_day"),
                                             DEFAULTS["max_auto_applied_per_day"], low=0),
            "stale_after_intervals": _int(sub.get("stale_after_intervals"), DEFAULTS["stale_after_intervals"]),
            "prune_after_days": _int(sub.get("prune_after_days"), DEFAULTS["prune_after_days"])}


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


def open_ask(sdlc_dir, issue, qkind, area, options, config, now, gesture="unpark", park=None, armed_at=False):
    """Record one question waiting on a person; returns its id, or None when closed or unusable.
    `park` names the park it answers (#1499); `armed_at` (an iso time or None) turns on arming: the window then
    counts from `armed_at`, and None means not yet armed. The default False keeps the old shape."""
    try:
        if not enabled(config) or gesture not in GESTURES or not isinstance(qkind, str) or not qkind:
            return None
        if not isinstance(options, list) or not options or not all(isinstance(o, str) for o in options):
            return None
        aid = "%s-%s" % (now.strftime("%Y%m%dT%H%M%S"), uuid.uuid4().hex[:6])
        body = {"id": aid, "issue": issue, "qkind": qkind, "area": area if isinstance(area, str) else "",
                "options": options, "gesture": gesture, "opened": now.isoformat(), "state": "pending"}
        if park:
            body["park"] = park
        if armed_at is not False:
            body["armed_at"] = armed_at
        _write_atomic(_ask_path(sdlc_dir, aid), body)
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
            # On a public repository the answer's free text must not be written: the autonomy folder is
            # published with the records, and nothing reads this text back (only the state matters).
            kept = "recorded privately" if _load("rulebook_cfg").visibility(config) == "public" else text
            ask.update(answer=kept, answered_at=now.isoformat(), state="answered")
        else:
            return ask.get("state")
        _write_atomic(_ask_path(sdlc_dir, aid), ask)
        return ask["state"] if ask["state"] != "applied" else "veto-pending"
    except Exception:
        return None


def _issue_key(v):
    return str(v).lstrip("#").strip()


def _supersede(sdlc_dir, ask, now):
    ask.update(state="superseded", superseded_at=now.isoformat())
    _write_atomic(_ask_path(sdlc_dir, ask["id"]), ask)


def _park_info(source, issue):
    """The labels, the declared kind and the newest park (id and time) of an open issue, read once through
    `source._read_issue`; None when the read fails. A missing comment list gives no park."""
    try:
        data = source._read_issue(issue, ["labels", "comments"]) or {}
        comments = [c for c in (data.get("comments") or []) if isinstance(c, dict)]
        u = _load("unpark")
        prefixes = u.sources.OFFBOARD_COMMENT_PREFIXES
        park_at, declared = None, None
        for c in comments:
            body = c.get("body") or ""
            if any(body.startswith(p) for p in prefixes):
                park_at = _parse(c.get("createdAt") or c.get("created_at")) or park_at
                declared = _load("qkind").parse_line(body) or declared
        return {"labels": data.get("labels"), "qkind": declared, "park": u.park_stamp(comments),
                "park_at": park_at}
    except Exception:
        return None


def _eligible(sdlc_dir, config, qkind_, area, option):
    try:
        return _level(sdlc_dir, qkind_, area, config) >= 3
    except Exception:
        return False


def open_for_park(sdlc_dir, config, issue, qkind_, source=None, now=None, info=None):
    """Open the ask for a park that declared `qkind_`, when an approved rule matches its area.
    Returns the ask id (an existing pending ask for the same park is reused) or None. Never raises.
    Closed gate: None before any read. The area is read best-effort from the issue's `area:*` label
    through `source._read_issue` (fallback `repo`); no match, a hard-stop kind or an undeclared kind
    opens nothing. An ask in any state for the same park covers it (#1499); a pending ask of another kind
    or opened before the newest park is superseded and a new one opened."""
    try:
        if not enabled(config):
            return None
        if not isinstance(qkind_, str) or qkind_ not in _load("qkind").QKINDS:
            return None
        if _load("hard_stop").is_hardstop_kind(qkind_, None, config):
            return None
        if qkind_ in ("unknown", "scope_hold", "owner_hold"):    # F-09, F-02(5); M-05 none options: the bank says
            return None
        now = now or datetime.datetime.now(datetime.timezone.utc)
        if info is None and source is not None:
            info = _park_info(source, issue)
        info = info or {}
        area = _load("decision_record").area_of(info.get("labels")) if info.get("labels") is not None else "repo"
        park, park_at = info.get("park"), info.get("park_at")
        mine = [a for a in _asks(sdlc_dir) if _issue_key(a.get("issue")) == _issue_key(issue)]
        for a in mine:                       # M-02: an applied, answered or vetoed ask covers its park
            if park and a.get("park") == park and a.get("state") not in ("pending", "superseded"):
                return None
        for a in mine:
            if a.get("state") != "pending":
                continue
            opened = _parse(a.get("opened"))
            stale = a.get("qkind") != qkind_ or (park_at is not None and opened is not None and opened < park_at) \
                or (park and a.get("park") and a.get("park") != park)
            if not stale:
                return a["id"]
            _supersede(sdlc_dir, a, now)
        rb = _load("rulebook")
        m = rb.match(qkind_, area, None, rb.load(sdlc_dir, config), config)
        if m is None:
            return None
        gesture = option_gesture(qkind_, m.option)
        if gesture not in GESTURES:                  # None: not a bank option; 'none': recommend only
            return None
        armed = now.isoformat() if _eligible(sdlc_dir, config, qkind_, area, m.option) else None
        return open_ask(sdlc_dir, issue, qkind_, area, [m.option], config, now, gesture=gesture, park=park,
                        armed_at=armed)
    except Exception:
        return None


#: The most issues one sync pass reads (one `_read_issue` each); the rest wait for the next tick.
SYNC_MAX_READS = 20


def sync(sdlc_dir, config, source, now=None, parked_fn=None):
    """Open an ask for every open parked issue that has none for its newest park, and close the asks that
    no longer apply (#1499). Returns the ids opened. Closed gate: `[]`, nothing read. Never raises. A failed
    list or read changes nothing; asks are only closed on a COMPLETE list. Reads at most `SYNC_MAX_READS`
    issues per pass (those with a pending ask go last)."""
    opened = []
    try:
        if not enabled(config):
            return opened
        now = now or datetime.datetime.now(datetime.timezone.utc)
        if parked_fn is None:
            au = _load("auto_unpark")
            parked_fn = au._fetch_parked_issues_status
        issues, complete = parked_fn(source)
        parked = {_issue_key(i.get("number")) for i in issues if isinstance(i, dict)}
        asks = _asks(sdlc_dir)
        rules = _load("rulebook").load(sdlc_dir, config)
        if complete:
            for a in asks:
                if a.get("state") != "pending" or a.get("answer") is not None:
                    continue
                gone = _issue_key(a.get("issue")) not in parked
                if not gone and _load("rulebook").match(a.get("qkind"), a.get("area", ""), a.get("options"),
                                                        rules) is None:
                    gone = True                                  # M-12: its rule was withdrawn
                if gone:
                    _supersede(sdlc_dir, a, now)
        asks = _asks(sdlc_dir)
        pending = {_issue_key(a.get("issue")) for a in asks if a.get("state") == "pending"}
        reads = 0
        # issues with no pending ask first (they may need one), then the rest: a re-park is seen by reading
        for i in sorted((i for i in issues if isinstance(i, dict)),
                        key=lambda i: (_issue_key(i.get("number")) in pending, str(i.get("number")))):
            n = i.get("number")
            if reads >= SYNC_MAX_READS:
                break
            reads += 1
            info = _park_info(source, n)
            if not info or not info.get("qkind"):
                continue
            aid = open_for_park(sdlc_dir, config, n, info["qkind"], source=source, now=now, info=info)
            mine = next((a for a in _asks(sdlc_dir) if a.get("id") == aid), None)
            if aid and mine and mine.get("opened") == now.isoformat():
                opened.append(aid)
    except Exception:
        return opened
    return opened


def close_for_issue(sdlc_dir, config, issue, text, now=None, refuse=False):
    """A person answered (unpark, keep-parked, promote): answer every pending ask of the issue with
    their text, so the person wins and the tick skips it. Skipped while the applier is applying its
    own gesture. `refuse` (#1189): the gesture is a person's no (keep parked, demote), so an ask
    already applied and still inside its veto window is routed to the veto. Never raises; closed
    gate: nothing read."""
    try:
        if not enabled(config) or os.environ.get(_APPLYING_ENV) or not isinstance(text, str):
            return None
        now = now or datetime.datetime.now(datetime.timezone.utc)
        for a in _asks(sdlc_dir):
            if _issue_key(a.get("issue")) != _issue_key(issue):
                continue
            if a.get("state") == "pending":
                answer(sdlc_dir, a["id"], text, config, now)
            elif refuse and a.get("state") == "applied" and not a.get("vetoed_at"):
                applied = _parse(a.get("applied_at"))
                period = datetime.timedelta(minutes=settings(config)["window_minutes"])
                if applied is not None and now <= applied + period:
                    answer(sdlc_dir, a["id"], "no", config, now)
    except Exception:
        return None
    return None


def window_open(ask, config, now):
    """True while the question is still inside its window (nobody may be overridden yet)."""
    opened = _parse(ask.get("opened")) if isinstance(ask, dict) else None
    if opened is None:
        return True  # cannot tell: treat as open, the safe reading
    if "armed_at" in ask:                    # #1499 M-03: the window counts from when the pair became eligible
        armed = _parse(ask.get("armed_at"))
        if armed is None:
            return True
        opened = max(opened, armed)
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


_SPOT = "spot-counter.json"


def _spot_read(sdlc_dir):
    """Silent-eligible applies so far per `qkind|area`; missing or unreadable reads as empty."""
    try:
        with open(os.path.join(_folder(sdlc_dir), _SPOT), encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items()
            if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) and v >= 0}


def _spot_check(counts, key, config):
    """Count one more silent-eligible apply under `key` (in `counts`); True when it must comment."""
    counts[key] = counts.get(key, 0) + 1
    return counts[key] % settings(config)["spot_check_one_in"] == 0


def _applied_in_last_day(asks, now):
    return sum(1 for a in asks if a.get("state") == "applied"
               and (_parse(a.get("applied_at")) or now - 2 * _DAY) > now - _DAY)


def _owner_no(comments, since, until, config, strict):
    """True when an owner's exact `no` was created after `since` (at or after when not `strict`) and,
    when `until` is given, by `until`. Judged by the comment's own time."""
    for c in comments or []:
        made = _parse(c.get("created_at"))
        if made is None or str(c.get("body", "")).strip().lower() != "no":
            continue
        if (made <= since if strict else made < since) or (until is not None and made > until):
            continue
        if _load("autonomy").is_owner(c.get("author"), config):
            return True
    return False


def read_vetoes(sdlc_dir, config, asks, now, comments_fn):
    """An owner's `no` comment is passed to `answer`. On a pending ask (created at or after it opened)
    it cancels the ask; on an applied ask (posted after `applied_at` and by `applied_at` + window) it
    records the veto the next planning pass turns into an action. The veto is judged by the comment's
    time, so a late tick still counts it; reading stops `stale_after_intervals` watch intervals past the
    period. Any other text, a non-owner or an older comment changes nothing. Never raises. PROVISIONAL:
    the veto period is the ask's own `window_minutes` after `applied_at`."""
    cfg = settings(config)
    period = datetime.timedelta(minutes=cfg["window_minutes"])
    grace = datetime.timedelta(seconds=_interval(config) * cfg["stale_after_intervals"])
    for ask in asks:
        try:
            if ask.get("state") == "pending" and ask.get("answer") is None:
                opened = _parse(ask.get("opened"))
                if opened is not None and _owner_no(comments_fn(ask.get("issue")), opened, None, config, False):
                    if answer(sdlc_dir, ask["id"], "no", config, now) == "answered":
                        ask.update(answer="no", answered_at=now.isoformat(), state="answered")
                continue
            applied = _parse(ask.get("applied_at"))
            if ask.get("state") != "applied" or ask.get("vetoed_at") or applied is None \
                    or now > applied + period + grace:
                continue
            if _owner_no(comments_fn(ask.get("issue")), applied, applied + period, config, True):
                if answer(sdlc_dir, ask["id"], "no", config, now) == "veto-pending":
                    ask["vetoed_at"] = now.isoformat()
        except Exception:
            continue


def _keys_only(config):
    """#1342: records part open AND repo declared public; any other state is today's text."""
    try:
        rc = _load("rulebook_cfg")
        return rc.load(config).part_enabled("records") and rc.visibility(config) == "public"
    except Exception:
        return False


def _loud_text(m, qkind, config):
    if _keys_only(config):   # #1342: public repo, records on: the option is free text
        return ("Applied automatically by rule %s (%s). No answer arrived within "
                "the window. Reply no to veto." % (m.rule_id, qkind))
    return ("Applied automatically by rule %s (%s): %s. No answer arrived within "
            "the window. Reply no to veto." % (m.rule_id, qkind, m.option))


def tick(sdlc_dir, config, now, source=None, run=None, apply_actions=True, comments_fn=None,
         monotonic_fn=time.monotonic, dry_run=False):
    """Plan, and unless `apply_actions` is false apply, one pass. Returns the actions (dicts with a
    `result` of would, done, failed or skipped). Closed gate: `[]`, nothing read, nothing written.
    `dry_run` (#1499) also skips the heartbeat and the prune, so nothing at all is written."""
    if not enabled(config):
        return []
    started = monotonic_fn()    # #1390: each ask is stamped when its own gesture lands, not at tick start
    clock = lambda: now + datetime.timedelta(seconds=monotonic_fn() - started)  # noqa: E731
    actions = []
    try:
        asks = _asks(sdlc_dir)
        if apply_actions and any((a.get("state") == "applied" and not a.get("vetoed_at"))
                                 or (a.get("state") == "pending" and a.get("answer") is None) for a in asks):
            if comments_fn is None and (source is None or run is not None):
                comments_fn = lambda issue: _load("sources").fetch_comments(config, issue, run=run)  # noqa: E731
            if comments_fn is not None:
                read_vetoes(sdlc_dir, config, asks, now, comments_fn)
        cap = settings(config)["max_auto_applied_per_day"]
        used = _applied_in_last_day(asks, now)
        rules = _load("rulebook").load(sdlc_dir, config)
        hs = _load("hard_stop")
        stored = _spot_read(sdlc_dir)
        counts = dict(stored)
        eligible = []
        loud = []   # #1361: the comment text each action would carry if its slot turns out to be a comment
        for ask in asks:
            if ask.get("state") == "applied" and ask.get("vetoed_at") and not ask.get("veto_logged"):
                actions.append({"kind": "veto", "ask": ask["id"], "issue": ask.get("issue"),
                                "qkind": ask.get("qkind"), "area": ask.get("area", ""), "rid": ask.get("rid")})
                eligible.append(None)
                loud.append("")
                continue
            if ask.get("state") != "pending" or ask.get("answer") is not None:
                continue  # a person's answer wins; nothing else is pending
            qkind, area = ask.get("qkind"), ask.get("area", "")
            if not isinstance(qkind, str) or hs.is_hardstop_kind(qkind, None, config):
                continue
            if "armed_at" in ask and ask.get("armed_at") is None:       # #1499 M-03: arm, never apply, this tick
                am = _load("rulebook").match(qkind, area, ask.get("options"), rules)
                if am is not None and _level(sdlc_dir, qkind, area, config) >= 3:
                    actions.append({"kind": "arm", "ask": ask["id"], "issue": ask.get("issue"), "qkind": qkind,
                                    "area": area, "rule_id": am.rule_id, "level": _level(sdlc_dir, qkind, area, config),
                                    "rule_confirmed": next((r.get("confirmed") for r in rules
                                                            if r.get("id") == am.rule_id), None)})
                    eligible.append(None)
                    loud.append("")
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
            silent = False
            key = None
            if level >= 4 and silent_allowed(qkind, config, rule):
                key = "%s|%s" % (qkind, area)
                silent = not _spot_check(counts, key, config)
            text = "" if silent else _loud_text(m, qkind, config)
            actions.append({"kind": ask.get("gesture") if ask.get("gesture") in GESTURES else "unpark",
                            "ask": ask["id"], "issue": ask.get("issue"), "qkind": qkind, "area": area,
                            "option": m.option, "rule_id": m.rule_id, "level": level, "silent": silent,
                            "text": text})
            eligible.append(key)
            loud.append(text if not silent else _loud_text(m, qkind, config))
            used += 1
        if apply_actions:
            done = dict(stored)   # only applies that landed advance the counter
            moved = False
            refused = False
            if source is None and actions:    # #1392: one source per tick; a refusal fails each ask once, unretried
                try:
                    source = _load("sources").GitHubSource(config, run=run)
                except Exception:
                    refused = True
            for a, key, text in zip(actions, eligible, loud):
                if key is not None:   # #1361: the slot is decided now, from the landed count
                    a["silent"] = (done.get(key, 0) + 1) % settings(config)["spot_check_one_in"] != 0
                    a["text"] = "" if a["silent"] else text
                a["result"] = "failed" if refused else apply(a, sdlc_dir, config, now, source=source, run=run, clock=clock)
                if key is not None and a["result"] == "done":
                    done[key] = done.get(key, 0) + 1
                    moved = True
            if moved:
                _write_atomic(os.path.join(_folder(sdlc_dir), _SPOT), done)
        else:
            for a in actions:
                a["result"] = "would"
        if not dry_run:
            _write_atomic(os.path.join(_folder(sdlc_dir), _HEARTBEAT), {"time": now.isoformat()})
            _prune(sdlc_dir, asks, now, config)
    except Exception as exc:  # a tick never raises
        actions.append({"kind": "error", "error": type(exc).__name__, "result": "failed"})
    return actions


def _prune(sdlc_dir, asks, now, config=None):
    """Remove answered and vetoed asks older than two days, and applied asks (no pending veto) older
    than `prune_after_days` (default 30, at least 1 so the rolling 24h cap count is never cut)."""
    applied_after = datetime.timedelta(days=settings(config or {})["prune_after_days"])
    for a in asks:
        t = _parse(a.get("applied_at") or a.get("answered_at") or a.get("superseded_at"))
        if t is None:
            continue
        old = (a.get("state") in ("answered", "vetoed", "superseded") and now - t > _PRUNE_AFTER) or (
            a.get("state") == "applied" and not a.get("vetoed_at") and now - t > applied_after)
        if old:
            try:
                os.unlink(_ask_path(sdlc_dir, a["id"]))
            except OSError:
                continue


def apply(action, sdlc_dir, config, now, source=None, run=None, clock=None):
    """Perform one action; returns done, failed or skipped. Never raises. The only GitHub writes are
    the atomic swap through `triage.apply_actions` (unpark) or `promote.promote`, then one comment."""
    try:
        if not enabled(config):
            return "skipped"
        ask = next((a for a in _asks(sdlc_dir) if a["id"] == action.get("ask")), None)
        if ask is None:
            return "skipped"
        if action["kind"] == "veto":
            if source is None:
                source = _load("sources").GitHubSource(config, run=run)
            if not ask.get("reparked") and ask.get("gesture") != "keep-parked":
                # An applied unpark that is vetoed must not leave the work running: park it again.
                add, remove = [source.parked_label], [source.goal_label, source.in_progress_label]
                swap = {"action": "swap-label", "issue": ask.get("issue"), "result": None, "error": None,
                        "detail": _load("triage")._swap_detail(add, remove), "add": add, "remove": remove}
                steps = [swap]
                if getattr(source, "board_active", False):      # #1201: the apply moved the card to Ready
                    steps.append({"action": "set-status", "issue": ask.get("issue"),
                                  "detail": source.col["parked"], "result": None, "error": None})
                res = _load("triage").apply_actions(source, steps, True)
                if not res or res[0]["result"] != "done":
                    return "failed"                 # the label swap gates; the card move is best effort (#1302)
                if not all(r["result"] == "done" for r in res):
                    ask.update(board_failed=True)
                ask.update(reparked=True)
                _write_atomic(_ask_path(sdlc_dir, ask["id"]), ask)
                try:
                    source.note(ask.get("issue"), "Vetoed by an owner: parked again, and the rule's "
                                "autonomy level is lowered.")
                except Exception:
                    pass
            _load("autonomy").veto(sdlc_dir, action.get("rid"), config, now=now)
            ask.update(state="vetoed", veto_logged=True)
            _write_atomic(_ask_path(sdlc_dir, ask["id"]), ask)
            return "done"
        if ask.get("state") != "pending":
            return "skipped"
        if source is None:
            source = _load("sources").GitHubSource(config, run=run)
        issue = action["issue"]
        if action["kind"] == "arm":                 # #1499 M-03: the window starts now, and one notice says so
            if ask.get("armed_at") is not None:
                return "skipped"
            at = max([t for t in (_parse(ask.get("opened")), _parse(action.get("rule_confirmed")), now) if t])
            ask.update(armed_at=at.isoformat())
            _write_atomic(_ask_path(sdlc_dir, ask["id"]), ask)
            due = at + datetime.timedelta(minutes=settings(config)["window_minutes"])
            try:
                source.note(issue, "Rule %s will apply after %s unless you answer or reply no."
                            % (action["rule_id"], due.strftime("%Y-%m-%d %H:%M UTC")))
            except Exception:
                pass
            return "done"
        # #1395: a person may have unparked it since the ask opened; the read failing keeps the ask pending.
        # A promote starts from a proposed issue, not a parked one, so only the other gestures check.
        data = {"labels": []} if action["kind"] == "promote" else source._read_issue(issue, ["labels"])
        if not isinstance(data, dict) or not isinstance(data.get("labels"), list):
            return "failed"
        names = {(l.get("name") if isinstance(l, dict) else l) for l in data["labels"]}
        if action["kind"] != "promote" and source.parked_label not in names:
            ask.update(state="answered", answer="already unparked", answered_at=now.isoformat())
            _write_atomic(_ask_path(sdlc_dir, ask["id"]), ask)
            return "skipped"
        board_failed = False
        if action["kind"] == "promote":
            os.environ[_APPLYING_ENV] = "1"      # its own gesture must not close the ask it applies
            try:
                res = _load("promote").promote(sdlc_dir, config, [issue], source=source, run=run, apply=True)
            finally:
                os.environ.pop(_APPLYING_ENV, None)
            if not all(r.get("outcome") == "promoted" for r in res.get("results", [])) or not res.get("results"):
                return "failed"
        elif action["kind"] != "keep-parked":      # #1301: keep-parked touches no label and no card
            if ask.get("qkind") in ("scope_hold", "owner_hold"):
                return "skipped"                    # F-02(5): a gate hold needs the registry edit, never an unpark here
            if ask.get("qkind") == "dependency":    # F-02(4): skip while a named blocker is still open
                try:
                    bf = _load("unpark").brief(sdlc_dir, config, issue, source=source, run=run)
                except Exception:
                    return "failed"
                if any(b.get("open") for b in (bf.get("blockers") or [])):
                    return "skipped"
            triage = _load("triage")
            add = [source.goal_label]
            remove = [source.parked_label, source.goal_blocked_label, source.in_progress_label]
            swap = {"action": "swap-label", "issue": issue, "detail": triage._swap_detail(add, remove),
                    "add": add, "remove": remove, "result": None, "error": None}
            steps = [swap]
            if getattr(source, "board_active", False):
                steps.append({"action": "set-status", "issue": issue, "detail": source.col["ready"],
                              "result": None, "error": None})
            res = triage.apply_actions(source, steps, True)
            if not res or res[0]["result"] != "done":
                return "failed"                     # the label swap gates; the card move is best effort (#1302)
            board_failed = not all(s["result"] == "done" for s in res)
        # Status first (#1106): a failed write returns failed before any record or comment, so the
        # retry repeats only the idempotent gesture and never a duplicate record or comment.
        ask.update(state="applied", applied_at=(clock() if clock else now).isoformat(), rid=None, rule_id=action["rule_id"],
                   silent=bool(action["silent"]))
        if action["kind"] != "promote" and board_failed:
            ask.update(board_failed=True)       # visible, never retried: the swap already landed
        _write_atomic(_ask_path(sdlc_dir, ask["id"]), ask)
        try:
            rec = _load("decision_record").build(
                action["kind"], issue, action["qkind"], action["option"],
                "applied after the window by rule " + action["rule_id"], "autonomous",
                rule_id=action["rule_id"], area=action.get("area", ""), answer=action["option"])
            ask["rid"] = _load("decision_store").append(sdlc_dir, rec, config, now=now)
            if ask["rid"] is not None:
                _write_atomic(_ask_path(sdlc_dir, ask["id"]), ask)
        except Exception:
            pass  # applied stays recorded; a missing record id is accepted by the veto path
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


USAGE = "usage: applier.py [sdlc_dir] [--dry-run] | applier.py sync [sdlc_dir]"


def _dry_run(sdlc_dir, config):
    """Print each action the next pass would take, with its rule, level and effective window; write nothing."""
    window = settings(config)["window_minutes"]
    acts = tick(sdlc_dir, config, datetime.datetime.now(datetime.timezone.utc), apply_actions=False, dry_run=True)
    for a in acts:
        if a.get("kind") == "error":
            print("applier dry-run: error %s" % a.get("error"))
            continue
        print("applier dry-run: would %s #%s rule %s level %s window %sm" % (
            a.get("kind"), str(a.get("issue")).lstrip("#"), a.get("rule_id", a.get("rid", "-")),
            a.get("level", "-"), window))
    return 0


def main(argv):
    args = argv[1:]
    if args in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    dry = "--dry-run" in args
    args = [x for x in args if x != "--dry-run"]
    do_sync = bool(args) and args[0] == "sync"
    if do_sync:
        args = args[1:]
    sdlc_dir = args[0] if args else ".sdlc"
    try:
        config = _read_config(sdlc_dir)
        if dry:
            return _dry_run(sdlc_dir, config) if enabled(config) else 0
        if enabled(config):                    # #1499: existing parks get their ask before the tick plans
            try:
                opened = sync(sdlc_dir, config, _load("sources").GitHubSource(config))
                if opened or do_sync:
                    print("applier: %d ask(s) opened for existing parks" % len(opened))
            except Exception:                  # noqa: BLE001 - best effort, the tick still runs
                pass
        if do_sync:
            return 0
        acts = tick(sdlc_dir, config, datetime.datetime.now(datetime.timezone.utc))
        done = [a for a in acts if a.get("result") == "done"]
        if done:
            print("applier: %d answer(s) applied or vetoed" % len(done))
        failed = [a for a in acts if a.get("result") == "failed"]
        if failed:                                      # #1403: a failed ask stays pending; say so
            print("applier: %d asks failed and will be retried" % len(failed), file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - a watcher tick is never fatal
        print("applier: tick failed (non-fatal): %s" % type(exc).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
