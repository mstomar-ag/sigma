"""File triage core (decision rubric, slice 14, #999). Pure library, UNWIRED: nothing imports it yet.

`decide(title, body, flags, config) -> Decision(kind, priority, reason)` for one AI-filed issue.
kind is `arm` (ready for the loop at `priority`), `park` (an ordinary park with `reason`), or `queue` (triage is
switched off: the old behaviour, no priority). It is total: any error parks. Priority is P0-P4 or None, never
a blocking value. The decision reads nothing about who filed it (login-free): `flags` is only inspected for
`human_confirmed`, `priority` and `bucket`.

Order: triage off -> queue; invalid buckets -> park; deny-list -> park (it can only park, even for a
human-confirmed filing); human-confirmed -> arm at its own plan priority; else a bucket -> arm at that bucket's
priority. Deny-list: the hard-stop classifier (default classes forced on here), configurable words, secret
shapes, and a recorded-permission marker on the first body line (the existing first-line parser).

`validate_buckets(config)` -> {"ok", "buckets", "reason"}: each bucket must be P0-P4 and rank strictly below the
human default (`ai_filed.human_default_priority`).

Config: the whole config (key `ai_filed`) or the `ai_filed` block itself (PROVISIONAL shape until wiring).
Triage is ON unless `ai_filed.triage.enabled` is exactly false; any other wrong type parks. Defaults below are
PROVISIONAL and every one is overridable. Stdlib only; no process, network or model call.
"""
import collections
import importlib.util
import pathlib
import re

_HERE = pathlib.Path(__file__).resolve().parent

Decision = collections.namedtuple("Decision", "kind priority reason")

DEFAULT_BUCKETS = {"urgent": "P2", "routine": "P3", "unclear": "P4"}
DEFAULT_HUMAN_PRIORITY = "P1"
DEFAULT_DENY_WORDS = ("irreversible", "force push", "production data", "drop table", "delete all",
                      "private key", "credentials")
_PRIO = re.compile(r"P[0-4]")
_SECRET = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:token|secret|password|api[_-]?key)\s*[:=]\s*\S{8,}",
                     re.IGNORECASE)
_URGENT = re.compile(r"\b(unsafe|vulnerab\w*|exploit\w*|data corrupt\w*|deadlock\w*|every run|silently wrong)\b",
                     re.IGNORECASE)
_ROUTINE = re.compile(r"\b(tech.?debt|cleanup|edge case|polish|follow.?up|cosmetic|nice.to.have)\b", re.IGNORECASE)


def _park(reason):
    return Decision("park", None, reason)


def _block(config):
    if not isinstance(config, dict):
        return {}
    inner = config.get("ai_filed")
    if isinstance(inner, dict):
        return inner
    return config if ("triage" in config or "buckets" in config) else {}


def validate_buckets(config):
    """-> {"ok": bool, "buckets": {name: priority}, "reason": str | None}. Never raises."""
    try:
        blk = _block(config)
        human = blk.get("human_default_priority", DEFAULT_HUMAN_PRIORITY)
        if not (isinstance(human, str) and _PRIO.fullmatch(human)):
            return {"ok": False, "buckets": {}, "reason": "ai_filed.human_default_priority must be P0-P4"}
        given = blk.get("buckets", {})
        if not isinstance(given, dict):
            return {"ok": False, "buckets": {}, "reason": "ai_filed.buckets must be an object"}
        out = dict(DEFAULT_BUCKETS)
        for name, val in given.items():
            if not name.startswith("_"):
                out[name] = val
        for name, val in out.items():
            if not (isinstance(val, str) and _PRIO.fullmatch(val)):
                return {"ok": False, "buckets": {}, "reason": "ai_filed.buckets.%s must be P0-P4" % name}
            if int(val[1]) <= int(human[1]):
                return {"ok": False, "buckets": {},
                        "reason": "ai_filed.buckets.%s (%s) must rank below the human default %s" % (name, val, human)}
        return {"ok": True, "buckets": out, "reason": None}
    except Exception:
        return {"ok": False, "buckets": {}, "reason": "ai_filed.buckets could not be read"}


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _deny(title, body, blk):
    """-> reason string when the deny-list matches, else None. Raises on a broken helper (caller parks)."""
    status = _load("spend_approval").parse_marker(body)[0]
    if status != "none":
        return "first body line carries a recorded-permission marker"
    text = "%s\n%s" % (title, body)
    words = blk.get("triage", {}).get("deny_words") if isinstance(blk.get("triage"), dict) else None
    if not (isinstance(words, (list, tuple)) and all(isinstance(w, str) for w in words)):
        words = DEFAULT_DENY_WORDS
    low = text.lower()
    for w in words:
        if w and w.lower() in low:
            return "hard-stop word: " + w
    if _SECRET.search(text):
        return "secret-shaped text"
    cls = _load("hard_stop").classify(text, {}, {"hard_stops": {"enabled": True}}).cls
    if cls is not None:
        return "hard-stop class: " + str(cls)
    return None


def _bucket(title, body, flags):
    named = flags.get("bucket") if isinstance(flags, dict) else None
    if isinstance(named, str):
        return named
    text = "%s\n%s" % (title, body)
    u, r = bool(_URGENT.search(text)), bool(_ROUTINE.search(text))
    return "urgent" if u and not r else "routine" if r and not u else "unclear"


def decide(title, body, flags, config):
    """See the module docstring. Never raises."""
    try:
        return _decide(title if isinstance(title, str) else "", body if isinstance(body, str) else "", flags, config)
    except Exception:
        return _park("triage could not decide")


def _decide(title, body, flags, config):
    blk = _block(config)
    tri = blk.get("triage", {})
    if not isinstance(tri, dict):
        return _park("ai_filed.triage must be an object")
    enabled = tri.get("enabled", True)
    if enabled is False:
        return Decision("queue", None, "triage is off")
    if enabled is not True:
        return _park("ai_filed.triage.enabled must be true or false")
    v = validate_buckets(config)
    if not v["ok"]:
        return _park(v["reason"])
    why = _deny(title, body, blk)
    if why:
        return _park(why)
    flags = flags if isinstance(flags, dict) else {}
    if flags.get("human_confirmed") is True:
        pri = flags.get("priority")
        if isinstance(pri, str) and _PRIO.fullmatch(pri):
            return Decision("arm", pri, "human-confirmed filing keeps its plan priority")
        return _park("human-confirmed filing has no valid plan priority")
    name = _bucket(title, body, flags)
    if name not in v["buckets"]:
        return _park("unknown bucket: %s" % name)
    return Decision("arm", v["buckets"][name], "bucket " + name)
