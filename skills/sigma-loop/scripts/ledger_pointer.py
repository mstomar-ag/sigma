"""Ledger pointer for recorded decisions (decision rubric, slice 9 of story 988). Library only.

A pointer is an unaddressed ledger `note` whose `ref` is the reserved value `decision-pointer` and
whose text carries only `id|kind|how` triples (record id, question kind, autonomous or human).
No contract change: `note` is an existing kind and `ref`/`why` are existing optional fields. A pointer
is never the record: the records folder and the issue comment each carry the id, so a lost or stalled
pointer loses nothing.

Everything is OFF unless the records part is open AND `decision_rubric.records.pointer.enabled` is the
JSON boolean true AND the ledger itself is enabled; closed means zero bytes written, nothing queued.

Growth: a pointer is about 200 bytes, but the ledger names a file per writing process, so writes are
batched (at most `max_writes_per_day` per writer per UTC day; the rest wait in a small local queue)
and `compact_pointers` folds this writer's own old pointer lines into one rollup file. It never
touches another writer's file, another host's file, a live process's file or a non-pointer line.
Known limit (UNVERIFIED against a real remote): the publish step stages files that exist, so a file
removed here is not removed from the shared branch by this module; the rollup carries the same ids,
and a reader that unions files may then see a pointer twice.
"""
import datetime
import importlib.util
import json
import os
import re
import time

REF = "decision-pointer"
GOAL = "decision-pointer"
HOW = ("autonomous", "human")
DEFAULTS = {"enabled": False, "max_writes_per_day": 1, "compact_after_days": 14}
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_TEXT_CAP = 200          # the ledger's free-text cap; one entry never carries more
_STATE = "pointer-state.json"
_ROLLUP_PID = "0"        # never a real pid, so the rollup name parses as a writer instance
_HERE = os.path.dirname(os.path.abspath(__file__))
_MODS = {}


def _load(name):
    if name not in _MODS:
        spec = importlib.util.spec_from_file_location(name, os.path.join(_HERE, name + ".py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _MODS[name] = mod
    return _MODS[name]


def _cfg(config):
    try:
        sub = config["decision_rubric"]["records"]["pointer"]
        return sub if isinstance(sub, dict) else {}
    except Exception:
        return {}


def settings(config):
    """Pointer settings; a wrong-typed or out-of-range value reads as its documented default."""
    sub, out = _cfg(config), dict(DEFAULTS)
    out["enabled"] = sub.get("enabled") is True
    for key in ("max_writes_per_day", "compact_after_days"):
        v = sub.get(key)
        if isinstance(v, int) and not isinstance(v, bool) and v >= 1:
            out[key] = v
    return out


def is_open(config):
    """True only when the records part is open, the pointer key is true and the ledger is enabled."""
    try:
        if not _load("decision_rubric_cfg").load(config).part_enabled("records"):
            return False
        return settings(config)["enabled"] and _load("ledger").enabled(config)
    except Exception:
        return False


def _epoch(now):
    if now is None:
        return time.time()
    if isinstance(now, datetime.datetime):
        return now.timestamp()
    return float(now)


def _day(now):
    return time.strftime("%Y-%m-%d", time.gmtime(_epoch(now)))


def _items(ids):
    """Normalise to unique (id, kind, how) triples; anything malformed is dropped, never raised."""
    kinds = _load("qkind").QKINDS
    out, seen = [], set()
    for item in ids or ():
        try:
            if isinstance(item, dict):
                rid, kind, how = item.get("id"), item.get("qkind"), item.get("how")
            else:
                rid, kind, how = item
        except Exception:
            continue
        if (isinstance(rid, str) and _ID.match(rid) and kind in kinds and how in HOW
                and rid not in seen):
            seen.add(rid)
            out.append((rid, kind, how))
    return out


def _state_path(sdlc_dir):
    return os.path.join(str(sdlc_dir), "state", _STATE)


def _load_state(sdlc_dir):
    try:
        with open(_state_path(sdlc_dir), encoding="utf-8") as fh:
            raw = json.load(fh)
        return {"day": str(raw.get("day", "")), "writes": int(raw.get("writes", 0)),
                "queue": _items(raw.get("queue"))}
    except Exception:
        return {"day": "", "writes": 0, "queue": []}


def _save_state(sdlc_dir, state):
    path = _state_path(sdlc_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"day": state["day"], "writes": state["writes"],
                       "queue": [list(t) for t in state["queue"]]}, fh, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _chunks(items):
    """Pack triples into texts of at most the ledger text cap."""
    out, cur = [], ""
    for rid, kind, how in items:
        tok = "%s|%s|%s" % (rid, kind, how)
        if cur and len(cur) + 1 + len(tok) > _TEXT_CAP:
            out.append(cur)
            cur = ""
        cur = tok if not cur else cur + " " + tok
    if cur:
        out.append(cur)
    return out


def queued(sdlc_dir):
    """Number of record ids waiting for the next permitted write."""
    return len(_load_state(sdlc_dir)["queue"])


def write_pointer(sdlc_dir, ids, config, now=None):
    """Queue `ids` (dicts or triples with id, qkind, how) and, if today's write budget allows, write
    the whole queue as one batch. Returns None when closed (zero bytes written); else a dict with
    `written` (entries appended), `queued` (ids still waiting). Never raises: a ledger problem
    leaves the queue intact and the records untouched."""
    try:
        if not is_open(config):
            return None
        led = _load("ledger")
        state = _load_state(sdlc_dir)
        today = _day(now)
        if state["day"] != today:
            state["day"], state["writes"] = today, 0
        known = {t[0] for t in state["queue"]}
        state["queue"] += [t for t in _items(ids) if t[0] not in known]
        if not state["queue"]:
            return {"written": 0, "queued": 0}
        _save_state(sdlc_dir, state)             # the queue survives a failed append below
        if state["writes"] >= settings(config)["max_writes_per_day"]:
            return {"written": 0, "queued": len(state["queue"])}
        written = 0
        for text in _chunks(state["queue"]):
            if led.append(sdlc_dir, config, "note", GOAL, now=_epoch(now), ref=REF, why=text) is None:
                break
            written += 1
        if written == len(_chunks(state["queue"])):
            state["queue"], state["writes"] = [], state["writes"] + 1
        _save_state(sdlc_dir, state)
        return {"written": written, "queued": len(state["queue"])}
    except Exception:
        return {"written": 0, "queued": queued(sdlc_dir)}


def parse(entry):
    """The (id, kind, how) triples a pointer entry carries; [] for any other entry."""
    if not (isinstance(entry, dict) and entry.get("kind") == "note" and entry.get("ref") == REF):
        return []
    return _items([tuple(tok.split("|")) for tok in str(entry.get("why", "")).split() if tok.count("|") == 2])


def _is_pointer(line):
    try:
        return bool(parse(json.loads(line)))
    except Exception:
        return False


def pointer_file_count(sdlc_dir):
    """How many ledger entry files exist (the doctor's measure); 0 when there is no directory."""
    try:
        led = _load("ledger")
        return sum(1 for p in led.entries_dir(sdlc_dir, led.ENTRIES).glob("*.jsonl"))
    except Exception:
        return 0


def _own_files(led, base, who):
    """This actor's files written from THIS host, minus the rollup and any live process's file."""
    host = led._host_token()
    out = []
    for p in led.files_for(base, who):
        instance = p.stem.rpartition("-")[2]
        h, _, pid = instance.rpartition(".")
        if h != host or pid == _ROLLUP_PID or not pid.isdigit():
            continue
        if led.pid_alive(int(pid)):
            continue
        out.append(p)
    return out


def _publish(path, text):
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def compact_pointers(sdlc_dir, now=None, config=None):
    """Fold this writer's own pointer lines, in files older than `compact_after_days` whose process
    is gone, into one own rollup file; a source file left empty is removed, one with other lines is
    rewritten without the pointer lines. Foreign actors, other hosts and live processes are never
    touched. Idempotent and crash-safe (rollup first, dedupe by entry id). Returns the number of
    files removed."""
    try:
        led = _load("ledger")
        cfg = config if config is not None else led._config(sdlc_dir)
        if not is_open(cfg):
            return 0
        cutoff = _epoch(now) - settings(cfg)["compact_after_days"] * 86400
        base = led.entries_dir(sdlc_dir, led.ENTRIES)
        who = led.actor(cfg)
        plans = []
        for p in _own_files(led, base, who):
            if p.stat().st_mtime > cutoff:
                continue
            lines = [l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
            ptr = [l for l in lines if _is_pointer(l)]
            if ptr:
                plans.append((p, ptr, [l for l in lines if not _is_pointer(l)]))
        if not plans:
            return 0
        rollup = base / ("%s-%s.%s.jsonl" % (led._safe_name(who), led._host_token(), _ROLLUP_PID))
        have = rollup.read_text(encoding="utf-8").splitlines() if rollup.exists() else []
        seen = set()
        for l in have:
            try:
                seen.add(json.loads(l).get("id"))
            except Exception:
                pass
        merged = list(have)
        for _p, ptr, _rest in plans:
            for l in ptr:
                eid = json.loads(l).get("id")
                if eid not in seen:
                    seen.add(eid)
                    merged.append(l)
        _publish(str(rollup), "\n".join(merged) + "\n")
        removed = 0
        for p, _ptr, rest in plans:
            if rest:
                _publish(str(p), "\n".join(rest) + "\n")
            else:
                os.unlink(str(p))
                removed += 1
        return removed
    except Exception:
        return 0
