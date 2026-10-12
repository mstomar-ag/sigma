#!/usr/bin/env python3
"""Autonomy levels 0 to 2 and the watch recorder (decision rubric, slice 12 of story 988; library only; no main).

Per question kind and area a level from 0 to 4 is DERIVED from facts, never stored as a setting:
human-answered decision records (`decision_store`) give the count, and watch records (what the rule
would have chosen next to what the person chose) give the agreement. Levels 0 to 2 only record and
pre-fill; the higher levels are read by a later slice. Rules:

- Thresholds come from `decision_rubric.autonomy.levels.*`; a wrong-typed value reads as its default.
- Level 2 to 4 need as many watch comparisons as answers for that level, per machine; each machine
  gets a level and the LOWER one wins, so disagreement never raises trust.
- A veto drops one level (PROVISIONAL: once, not cumulatively); `veto_pause_consecutive` vetoes since
  the last agreeing comparison, or `veto_pause_total` in all, pause the kind at level 0.
- A hard-stop kind (`hard_stop.is_hardstop_kind`) or any record carrying a hardstop class is capped
  at level 2. The cap is fixed, not configurable.
- A missing, unreadable or malformed file never raises and never raises a level.
- Everything is inert unless `decision_rubric.autonomy.enabled` is the JSON boolean true: no file is
  written and the level is 0.

State is one file per fact under `<sdlc_dir>/autonomy/`, created exclusively (`watch-<id>.json`,
`veto-<id>.json`). It is about the repository and its areas; the machine tag is a short hash of the
host name. The only place a login is read as authority is `is_owner`, a disclosed exception.
Watch files older than `watch.retention_days` are pruned by embedded time, not file age, so a fresh
checkout does not reset it; vetoes are kept as history. SCALE: one small file per comparison, bounded
by retention; a level costs one listing of the folder plus one store read.
"""
import datetime
import hashlib
import importlib.util
import json
import os
import pathlib
import uuid

_HERE = pathlib.Path(__file__).resolve().parent
_DIR = "autonomy"
_STAMP = "_pruned"
_ID_FIELDS = ("watch-", "veto-")

DEFAULTS = {"l1_min_answers": 10, "l2_min_answers": 30, "l3_min_answers": 60, "l4_min_answers": 300,
            "l2_agreement": 0.90, "l3_agreement": 0.95, "l4_agreement": 0.99}
DEFAULT_PAUSE = {"veto_pause_consecutive": 3, "veto_pause_total": 20}
DEFAULT_RETENTION_DAYS = 30
HARDSTOP_CAP = 2


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _sub(config):
    try:
        sub = config["decision_rubric"]["autonomy"]
        return sub if isinstance(sub, dict) else {}
    except Exception:
        return {}


def enabled(config):
    try:
        return _sub(config).get("enabled") is True and _load("decision_rubric_cfg").load(config).part_enabled("autonomy")
    except Exception:
        return False


def _pos_int(v, default):
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 1 else default


def settings(config):
    """Thresholds, pause numbers and retention; a bad value reads as its default."""
    sub = _sub(config)
    lv = sub.get("levels") if isinstance(sub.get("levels"), dict) else {}
    out = {}
    for k, d in DEFAULTS.items():
        v = lv.get(k)
        if k.endswith("agreement"):
            ok = isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v <= 1
        else:
            ok = isinstance(v, int) and not isinstance(v, bool) and v >= 1
        out[k] = v if ok else d
    for k, d in DEFAULT_PAUSE.items():
        out[k] = _pos_int(sub.get(k), d)
    w = sub.get("watch") if isinstance(sub.get("watch"), dict) else {}
    out["retention_days"] = _pos_int(w.get("retention_days"), DEFAULT_RETENTION_DAYS)
    return out


def _machine():
    try:
        return hashlib.sha256(os.uname().nodename.encode("utf-8")).hexdigest()[:8]
    except Exception:
        return "unknown"


def _now(now):
    return now or datetime.datetime.now(datetime.timezone.utc)


def _dir(sdlc_dir):
    return os.path.join(str(sdlc_dir), _DIR)


def _area(area):
    return area if isinstance(area, str) else ""


def _write_new(sdlc_dir, prefix, body, now):
    folder = _dir(sdlc_dir)
    if os.path.islink(folder):
        return None
    os.makedirs(folder, exist_ok=True)
    fid = "%s%s-%s" % (prefix, now.strftime("%Y%m%dT%H%M%S"), uuid.uuid4().hex[:8])
    fd = os.open(os.path.join(folder, fid + ".json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(body, sort_keys=True) + "\n")
    return fid


def _facts(sdlc_dir, prefix):
    out = []
    folder = _dir(sdlc_dir)
    try:
        names = sorted(n for n in os.listdir(folder) if n.startswith(prefix) and n.endswith(".json"))
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
        if isinstance(data, dict):
            out.append(dict(data, _file=n))
    return out


def _pair(rec, qkind, area):
    return rec.get("qkind") == qkind and _area(rec.get("area")) == _area(area)


def record_watch(sdlc_dir, qkind, area, would, actual, config=None, now=None, machine=None):
    """Write what the rule would have chosen next to what was chosen; returns the file id, or None
    when the part is closed or the inputs are unusable. Never raises."""
    try:
        if not enabled(config) or not isinstance(qkind, str) or not qkind:
            return None
        if not isinstance(would, str) or not isinstance(actual, str):
            return None
        when = _now(now)
        fid = _write_new(sdlc_dir, "watch-", {"qkind": qkind, "area": _area(area), "would": would,
                                              "actual": actual, "machine": machine or _machine(),
                                              "time": when.isoformat()}, when)
        _maybe_prune(sdlc_dir, when, config)
        return fid
    except Exception:
        return None


def veto(sdlc_dir, rid, config=None, now=None, machine=None):
    """Record that an automatically applied answer (record `rid`) was rejected. The kind and area are
    taken from that record; an unknown record writes nothing and returns None. Never raises."""
    try:
        if not enabled(config):
            return None
        rec = _load("decision_store").get(sdlc_dir, rid, config)
        if not isinstance(rec, dict) or not isinstance(rec.get("qkind"), str):
            return None
        when = _now(now)
        return _write_new(sdlc_dir, "veto-", {"rid": rid, "qkind": rec["qkind"], "area": _area(rec.get("area")),
                                              "machine": machine or _machine(), "time": when.isoformat()}, when)
    except Exception:
        return None


def _ladder(answers, comparisons, agreement, cfg):
    lvl = 0
    if answers >= cfg["l1_min_answers"]:
        lvl = 1
    for n in (2, 3, 4):
        need = cfg["l%d_min_answers" % n]
        if answers >= need and comparisons >= need and agreement >= cfg["l%d_agreement" % n]:
            lvl = n
        else:
            break
    return lvl if lvl >= 1 else 0


def _state(sdlc_dir, qkind, area, config):
    cfg = settings(config)
    cat, has_file = None, False
    try:
        cats = _load("categories")
        has_file = cats.exists(sdlc_dir)
        cat = cats.resolve(sdlc_dir, qkind, area) if has_file else None
        if cat:
            cfg.update(cat["levels"])
    except Exception:
        cat = None
    store = _load("decision_store")
    recs = [r for r in store.list_by(sdlc_dir, config, qkind=qkind) if _pair(r, qkind, area)]
    answers = sum(1 for r in recs if r.get("how") == "human")
    watch = [w for w in _facts(sdlc_dir, "watch-") if _pair(w, qkind, area)]
    vetoes = sorted((v for v in _facts(sdlc_dir, "veto-") if _pair(v, qkind, area)),
                    key=lambda v: str(v.get("time")))
    by_machine = {}
    for w in watch:
        by_machine.setdefault(str(w.get("machine")), []).append(w)
    if by_machine:
        per = []
        for items in by_machine.values():
            agree = sum(1 for w in items if w.get("would") == w.get("actual"))
            per.append(_ladder(answers, len(items), agree / len(items), cfg))
        lvl = min(per)
        total_agree = sum(1 for w in watch if w.get("would") == w.get("actual"))
        agreement = total_agree / len(watch)
    else:
        lvl, agreement = (1 if answers >= cfg["l1_min_answers"] else 0), None
    last_ok = max((str(w.get("time")) for w in watch if w.get("would") == w.get("actual")), default="")
    consecutive = sum(1 for v in vetoes if str(v.get("time")) > last_ok)
    paused = bool(vetoes) and (consecutive >= cfg["veto_pause_consecutive"] or len(vetoes) >= cfg["veto_pause_total"])
    if vetoes:
        lvl = max(0, lvl - 1)
    if paused:
        lvl = 0
    capped = False
    try:
        hs = _load("hard_stop")
        if hs.is_hardstop_kind(qkind, None, config) or any(hs.is_hardstop_kind(qkind, r, config) for r in recs):
            capped = True
    except Exception:
        capped = True  # cannot tell: the safe reading
    if capped:
        lvl = min(lvl, HARDSTOP_CAP)
    row = {"qkind": qkind, "area": _area(area), "level": lvl, "answers": answers, "comparisons": len(watch),
           "agreement": agreement, "vetoes": len(vetoes), "paused": paused, "capped": capped}
    if has_file:  # shown only when the opt-in file exists; otherwise the row is unchanged
        row["category"] = cat["id"] if cat else None
        row["thresholds"] = {k: cfg[k] for k in DEFAULTS}
    return row


def level(sdlc_dir, qkind, area, config):
    """The derived level, 0 to 4. 0 when the part is closed or anything is unreadable. Never raises."""
    try:
        if not enabled(config) or not isinstance(qkind, str):
            return 0
        return _state(sdlc_dir, qkind, area, config)["level"]
    except Exception:
        return 0


def readout(sdlc_dir, config):
    """Per kind and area state for the doctor. `{"enabled": False}` when closed. Never raises."""
    try:
        if not enabled(config):
            return {"enabled": False}
        store = _load("decision_store")
        pairs = {(r.get("qkind"), _area(r.get("area"))) for r in store.list_by(sdlc_dir, config)}
        pairs |= {(f.get("qkind"), _area(f.get("area"))) for p in ("watch-", "veto-") for f in _facts(sdlc_dir, p)}
        rows = [_state(sdlc_dir, q, a, config) for q, a in sorted(pairs, key=str) if isinstance(q, str)]
        rows = [dict(r, **_need(r, config)) for r in rows]
        return {"enabled": True, "kinds": rows, "watch_records": len(_facts(sdlc_dir, "watch-")),
                "vetoes": len(_facts(sdlc_dir, "veto-"))}
    except Exception:
        return {"enabled": False, "error": "unreadable"}


def _need(row, config):
    """What the next level asks for, from the thresholds in force; all None at the top or at the cap."""
    lvl = row["level"]
    top = HARDSTOP_CAP if row["capped"] else 4
    if lvl >= top:
        return {"next_level": None, "answers_needed": None, "comparisons_needed": None, "agreement_needed": None}
    cfg, nxt = settings(config), lvl + 1
    if nxt == 1:
        return {"next_level": 1, "answers_needed": cfg["l1_min_answers"], "comparisons_needed": 0,
                "agreement_needed": None}
    return {"next_level": nxt, "answers_needed": cfg["l%d_min_answers" % nxt],
            "comparisons_needed": cfg["l%d_min_answers" % nxt], "agreement_needed": cfg["l%d_agreement" % nxt]}


NO_RECORDS_LINE = "learning has not started, every question is asked"
HARDSTOP_TEXT = "hard-stop kind, never learns beyond level 2, always asked"


def describe(row):
    """One plain line for a row of `readout`; the doctor and the question list share it."""
    head = "%s / %s: " % (row["qkind"], row["area"] or "-")
    if row["capped"]:
        return head + HARDSTOP_TEXT
    if row["paused"]:
        return head + "paused after vetoes, still asking"
    if row["level"] == 0:
        return head + "%d of %d answers, still asking" % (row["answers"], row["answers_needed"])
    if row["next_level"] is None:
        return head + "level %d, nothing further to learn" % row["level"]
    agree = "none yet" if row["agreement"] is None else "%d%%" % round(row["agreement"] * 100)
    return head + ("level %d, level %d needs %d answers (have %d), %d comparisons (have %d), %d%% agreement (have %s)"
                   % (row["level"], row["next_level"], row["answers_needed"], row["answers"],
                      row["comparisons_needed"], row["comparisons"], round(row["agreement_needed"] * 100), agree))


def learning_line(sdlc_dir, qkind, config):
    """One line for the question list when `qkind` is below level 1 or a hard-stop kind; None when the
    gate is closed or the kind has learned. Never raises."""
    try:
        out = readout(sdlc_dir, config)
        if not out.get("enabled") or not isinstance(qkind, str):
            return None
        rows = [r for r in out["kinds"] if r["qkind"] == qkind]
        if any(r["capped"] for r in rows) or _load("hard_stop").is_hardstop_kind(qkind, None, config):
            return "%s: it never learns beyond level 2 and is always asked" % qkind
        best = max(rows, key=lambda r: (r["level"], r["answers"]), default=None)
        if best is not None and best["level"] >= 1:
            return None
        have = best["answers"] if best else 0
        return "the rulebook is still learning and will keep asking this kind (%d of %d answers)" % (
            have, settings(config)["l1_min_answers"])
    except Exception:
        return None


def _parse_time(text):
    try:
        t = datetime.datetime.fromisoformat(str(text))
        return t if t.tzinfo else t.replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def prune_watch(sdlc_dir, now, config):
    """Remove watch files whose embedded time is older than the retention; returns the count. Vetoes
    stay. Links are skipped. Never raises."""
    try:
        if not enabled(config):
            return 0
        folder = _dir(sdlc_dir)
        if os.path.islink(folder):
            return 0
        cutoff = _now(now) - datetime.timedelta(days=settings(config)["retention_days"])
        removed = 0
        for w in _facts(sdlc_dir, "watch-"):
            when = _parse_time(w.get("time"))
            if when is not None and when < cutoff:
                try:
                    os.unlink(os.path.join(folder, w["_file"]))
                    removed += 1
                except OSError:
                    continue
        return removed
    except Exception:
        return 0


def _maybe_prune(sdlc_dir, now, config):
    """At most one sweep a day, behind a stamp file; a stamp that cannot be read costs one extra sweep."""
    stamp = os.path.join(_dir(sdlc_dir), _STAMP)
    try:
        last = _parse_time(open(stamp, encoding="utf-8").read().strip())
    except OSError:
        last = None
    if last is not None and now - last < datetime.timedelta(days=1):
        return
    prune_watch(sdlc_dir, now, config)
    try:
        with open(stamp, "w", encoding="utf-8") as fh:
            fh.write(now.isoformat())
    except OSError:
        pass


def _names(value):
    if isinstance(value, list) and value and all(isinstance(x, str) and x.strip() for x in value):
        return [x.strip().lower() for x in value]
    return []


def is_owner(login, config):
    """True when `login` is in `autonomy.owners` (case-insensitive). Owners unset (key absent) falls back
    to `spend_approval.approvers`; an empty or malformed list, or both empty, refuses. This is the only
    read of a login as authority anywhere in the rubric: a disclosed exception to the person-free rule."""
    try:
        if not isinstance(login, str) or not login.strip():
            return False
        sub = _sub(config)
        if "owners" in sub and sub["owners"] is not None:
            names = _names(sub["owners"])
        else:
            sa = config.get("spend_approval") if isinstance(config, dict) else None
            names = _names(sa.get("approvers")) if isinstance(sa, dict) else []
        return login.strip().lower() in names
    except Exception:
        return False
