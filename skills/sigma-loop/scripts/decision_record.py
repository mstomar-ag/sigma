"""Record writers' shared core (decision rubric, slice 8 of story 988; library only; no main).

A human gesture (unpark, keep-parked, promote, ack) or the automatic sweep writes ONE decision record
to the repository's store (`decision_store`) and ONE attributed issue comment that carries the same
id. Everything here is inert unless `decision_rubric.records.enabled` is the JSON boolean true; a
caller whose gate is closed keeps its old code path and its old bytes.

Attribution is by gesture and `how` (`human` or `autonomous`), never by person: no login is read or
written. The comment's decision lines sit INSIDE the fenced span `blocker_scan.strip_unpark_qa`
removes, so free text such as "waiting on the sign-off, see an issue number" plants no blocker edge.
Posting goes through the source's own comment method (the existing gh helper path), so this module
adds no direct gh call site. A store failure never blocks the comment: the error is reported back.
"""
import importlib.util
import pathlib

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


decision_store = _load("decision_store")
blocker_scan = _load("blocker_scan")
qkind = _load("qkind")
ledger_pointer = _load("ledger_pointer")

#: gesture -> how. The sweep is the only autonomous writer.
GESTURES = {"unpark": "human", "keep-parked": "human", "promote": "human", "demote": "human",
            "ack": "human", "sweep": "autonomous"}


def enabled(config):
    return decision_store.enabled(config)


def how_for(gesture):
    if gesture not in GESTURES:
        raise ValueError("unknown gesture %r" % (gesture,))
    return GESTURES[gesture]


def _flat(text, limit=300):
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[:limit - 1].rstrip() + "…"


#: The stated area of a record whose issue carries no `area:<x>` label: the whole repository.
REPO_AREA = "repo"


def area_of(labels):
    """The first `area:<x>` label's `<x>`, else `repo`. Labels may be names or `{"name": ...}`."""
    for label in (labels or []):
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str) and name.startswith("area:") and name[5:].strip():
            return name[5:].strip()
    return REPO_AREA


def build(gesture, goal, qkind_, choice, reason, how, rule_id=None, area=None):
    """A plain record dict. An unusable kind reads `unknown`; an empty choice gets a placeholder."""
    rec = {"gesture": gesture,
           "qkind": qkind_ if qkind_ in qkind.QKINDS else "unknown",
           "choice": _flat(choice) or "(none recorded)",
           "reason": _flat(reason),
           "how": how,
           "issue": int(goal) if str(goal).isdigit() else (str(goal) if goal else None)}
    if rule_id:
        rec["rule_id"] = str(rule_id)
    if area:
        rec["area"] = str(area)
    return rec


def _fenced(rid, record, visibility):
    shown = decision_store.redact(record, visibility)
    parts = ["Decision record `%s`" % rid, "kind: %s" % shown.get("qkind"),
             "how: %s" % shown.get("how"), "gesture: %s" % shown.get("gesture")]
    if shown.get("rule_id"):
        parts.append("rule: %s" % shown["rule_id"])
    lines = ["- " + p for p in parts]
    for key in decision_store.FREE_TEXT:
        if shown.get(key):
            lines.append("- %s: %s" % (key, shown[key]))
    return "\n".join([blocker_scan.UNPARK_QA_START] + lines + [blocker_scan.UNPARK_QA_END])


def point(sdlc_dir, rid, record, config):
    """Hand a stored record to the ledger pointer writer (inert when its gate is closed). Never raises."""
    try:
        ledger_pointer.write_pointer(sdlc_dir, [(rid, record.get("qkind"), record.get("how"))], config)
    except Exception:                                   # noqa: BLE001 - a ledger problem never touches the gesture
        pass


def _would_pick(sdlc_dir, record, config):
    """The option a sealed approved rule gives, else the unique most common earlier human choice of
    the same kind and area once `l1_min_answers` of them exist, else None."""
    au, rb = _load("autonomy"), _load("rulebook")
    kind, area = record.get("qkind"), record.get("area") or REPO_AREA
    m = rb.match(kind, area, None, rb.load(sdlc_dir, config), config) if rb.enabled(config) else None
    if m:
        return m.option
    earlier = [r.get("choice") for r in decision_store.list_by(sdlc_dir, config, qkind=kind)
               if r.get("how") == "human" and (r.get("area") or REPO_AREA) == area
               and isinstance(r.get("choice"), str) and r.get("choice")]
    if len(earlier) < au.settings(config)["l1_min_answers"]:
        return None
    counts = {}
    for c in earlier:
        counts[c] = counts.get(c, 0) + 1
    top = sorted(counts.values(), reverse=True)
    if len(top) > 1 and top[0] == top[1]:
        return None
    return max(counts, key=counts.get)


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception:                                   # noqa: BLE001 - the watch is best-effort
        return None


def would_for(sdlc_dir, record, config):
    """What the rulebook or the earlier human answers would have chosen for this record, or None.
    Call BEFORE the record is appended so the answer is not in its own majority. Skipped (None) for a
    non-human record, closed autonomy, or public record visibility (the watch file would carry the
    free-text choice). Best-effort: never raises."""
    try:
        if record.get("how") != "human" or not _load("autonomy").enabled(config):
            return None
        if decision_store._visibility(config) == "public":
            return None
        return _would_pick(sdlc_dir, record, config)
    except Exception:                                   # noqa: BLE001 - a watch problem never touches the gesture
        return None


def record_comparison(sdlc_dir, record, would, config):
    """AFTER a successful append: write what `would` chose next to what the person chose. Never raises."""
    try:
        if would is None:
            return
        _load("autonomy").record_watch(sdlc_dir, record.get("qkind"), record.get("area") or REPO_AREA,
                                       would, record.get("choice"), config)
    except Exception:                                   # noqa: BLE001
        pass


def write_all(sdlc_dir, record, config, source, text="", poster=None):
    """Store record first, then the comment (`text` followed by the fenced decision span).

    Returns `{"id", "error", "commented"}`. Closed gate: nothing is written or posted. A store error
    leaves `id` None and the text of the error in `error`; the comment is still posted."""
    out = {"id": None, "error": None, "commented": False}
    if not enabled(config):
        return out
    try:
        would = _safe(would_for, sdlc_dir, record, config)
        out["id"] = decision_store.append(sdlc_dir, record, config)
        point(sdlc_dir, out["id"], record, config)
        if out["id"]:
            _safe(record_comparison, sdlc_dir, record, would, config)
    except Exception as exc:                            # noqa: BLE001 - reported, never raised
        out["error"] = "decision record not stored: %s" % exc
    rid = out["id"] or "(not stored)"
    body = ((text.rstrip() + "\n\n") if text else "") + _fenced(rid, record,
                                                              decision_store._visibility(config))
    number = record.get("issue")
    try:
        (poster or source._issue_comment)(number, body)
        out["commented"] = True
    except Exception as exc:                            # noqa: BLE001 - audit trail is best-effort
        out["error"] = (out["error"] + "; " if out["error"] else "") + "comment not posted: %s" % exc
    return out
