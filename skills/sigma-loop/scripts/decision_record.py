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


def build(gesture, goal, qkind_, choice, reason, how, rule_id=None):
    """A plain record dict. An unusable kind reads `unknown`; an empty choice gets a placeholder."""
    rec = {"gesture": gesture,
           "qkind": qkind_ if qkind_ in qkind.QKINDS else "unknown",
           "choice": _flat(choice) or "(none recorded)",
           "reason": _flat(reason),
           "how": how,
           "issue": int(goal) if str(goal).isdigit() else (str(goal) if goal else None)}
    if rule_id:
        rec["rule_id"] = str(rule_id)
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


def write_all(sdlc_dir, record, config, source, text="", poster=None):
    """Store record first, then the comment (`text` followed by the fenced decision span).

    Returns `{"id", "error", "commented"}`. Closed gate: nothing is written or posted. A store error
    leaves `id` None and the text of the error in `error`; the comment is still posted."""
    out = {"id": None, "error": None, "commented": False}
    if not enabled(config):
        return out
    try:
        out["id"] = decision_store.append(sdlc_dir, record, config)
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
