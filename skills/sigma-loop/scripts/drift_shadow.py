"""Shadow-mode wiring of the drift judge into the park path (rulebook, goal 1229).

When a question is parked with a declared kind and `rulebook.drift.enabled` is the JSON true,
`shadow_for_park` reads the reference documents named in `rulebook.drift.documents`
(repository-relative paths), calls the existing `drift_judge.judge` once for that park, and stores
the verdict in `<sdlc>/state/drift-shadow.json`. It never parks, blocks or changes a label, and it
never raises. Closed gate, a hard-stop kind or an undeclared kind: nothing is read or written.

Unverified: the real model call goes through `feature_judge.ask_claude`; its cost per run has not
been measured. Defaults are provisional.
"""
import hashlib
import importlib.util
import json
import os
import pathlib
import time

_HERE = pathlib.Path(__file__).resolve().parent
STORE = "drift-shadow.json"
MAX_TOTAL_CHARS = 40000       # PROVISIONAL: clip for all documents together
LOCK_TIMEOUT_S = 20           # PROVISIONAL: bounded wait for the store lock
TRANSIENT = ("call_failed", "lock_unusable", "spend_unreadable", "ceiling_reached")   # judged again next time
MAX_RECORDS = 200             # PROVISIONAL: the store keeps the newest N verdicts
_SCHEMA = {"type": "object", "properties": {
    "kind": {"type": "string", "enum": ["consistent", "not_covered", "conflicts"]},
    "quote": {"type": "string"}}, "required": ["kind"], "additionalProperties": False}


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _block(config):
    try:
        b = config["rulebook"]["drift"]
        return b if isinstance(b, dict) else {}
    except Exception:
        return {}


def _limit(config):
    v = _block(config).get("max_total_chars")
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else MAX_TOTAL_CHARS


def read_documents(root, config):
    """-> (docs, unreachable, configured). `docs` is [(name, text)] clipped to a total size;
    `unreachable` lists configured names that are absolute, escape the root, or cannot be read."""
    names = _block(config).get("documents")
    names = [n for n in names if isinstance(n, str) and n.strip()] if isinstance(names, list) else []
    docs, bad, left = [], [], _limit(config)
    try:
        base = pathlib.Path(root).resolve()
    except Exception:
        return [], names, names
    for n in names:
        try:
            p = pathlib.Path(n)
            full = (base / p).resolve()
            if p.is_absolute() or ".." in p.parts or base not in full.parents:
                raise ValueError("outside the repository")
            text = full.read_text(encoding="utf-8")
        except Exception:
            bad.append(n)
            continue
        docs.append((n, text[:max(left, 0)]))
        left -= len(text)
    return docs, bad, names


def default_call(prompt, model, max_budget_usd):
    """The existing model call path (`feature_judge.ask_claude`), adapted to the judge's shape."""
    payload, cost = _load("feature_judge").ask_claude(prompt, _SCHEMA, model=model,
                                                      max_budget_usd=max_budget_usd)
    payload = payload if isinstance(payload, dict) else {}
    return {"kind": payload.get("kind"), "quote": payload.get("quote"), "cost_usd": cost}


def _path(sdlc_dir):
    return pathlib.Path(sdlc_dir) / "state" / STORE


def _read(sdlc_dir):
    try:
        recs = json.loads(_path(sdlc_dir).read_text(encoding="utf-8")).get("records")
        return [r for r in recs if isinstance(r, dict)]
    except Exception:
        return []


def _save(sdlc_dir, records):
    path = _path(sdlc_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(STORE + ".tmp")
    tmp.write_text(json.dumps({"records": records[-MAX_RECORDS:]}), encoding="utf-8")
    os.replace(str(tmp), str(path))


def park_key(issue, question):
    return "%s:%s" % (str(issue).lstrip("#"), hashlib.sha1(str(question).encode("utf-8")).hexdigest()[:12])


def get(sdlc_dir, issue, question):
    """The stored verdict record for this issue and park, or None."""
    key = park_key(issue, question)
    return next((r for r in _read(sdlc_dir) if r.get("key") == key), None)


def shadow_for_park(sdlc_dir, config, issue, qkind_, question, call=None, now=None):
    """Judge one park in shadow mode. Returns the stored record, or None when nothing was done
    (gate closed, hard-stop or undeclared kind, already judged). Never raises."""
    try:
        if not _load("rulebook_cfg").load(config).part_enabled("drift"):
            return None
        if not isinstance(qkind_, str) or qkind_ not in _load("qkind").QKINDS:
            return None
        if _load("hard_stop").is_hardstop_kind(qkind_, None, config):
            return None
        if get(sdlc_dir, issue, question) is not None:
            return None
        now = time.time() if now is None else now
        root = pathlib.Path(sdlc_dir).resolve().parent
        docs, bad, configured = read_documents(root, config)
        rec = {"key": park_key(issue, question), "issue": str(issue), "qkind": qkind_, "ts": now,
               "documents_read": [n for n, _ in docs], "documents_unreachable": bad,
               "verdict": "not_judged", "quote": None, "document": None, "reason": None}
        if not configured:
            rec["reason"] = "no_documents_configured"
        elif not docs:
            rec["reason"] = "documents_unreachable"
        else:
            v = _load("drift_judge").judge(question, docs, config, sdlc_dir,
                                           call=call or default_call, now=now)
            rec["verdict"], rec["quote"], rec["reason"] = v.kind, v.quote, v.reason
            if v.kind == "conflicts":
                dj = _load("drift_judge")
                rec["document"] = next((n for n, t in docs if dj.verify_quote(v.quote, t)), None)
        if rec["reason"] in TRANSIENT:
            return rec                                  # not final: nothing stored, so the next park judges again
        lock_dir = _path(sdlc_dir).parent / "drift-shadow.lock.d"
        with _load("decision_store")._lock(str(lock_dir), LOCK_TIMEOUT_S) as held:
            if not held:
                return None                             # no lock, no write: the next park tries again
            if get(sdlc_dir, issue, question) is None:
                _save(sdlc_dir, _read(sdlc_dir) + [rec])
        return rec
    except Exception:
        return None
