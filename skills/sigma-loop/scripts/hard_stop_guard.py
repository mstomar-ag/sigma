"""Chokepoint wiring of the hard-stop classifier (decision rubric slice 10). Library only; no CLI.

Every function is a no-op while `decision_rubric.hard_stops.enabled` is not the JSON boolean true: it returns an
allowing Verdict before reading anything, so a chokepoint that calls it makes exactly the calls it made before.

  guard(action_text, context, config) -> Verdict   classify one concrete action at a Sigma-owned chokepoint
  is_own(branch, registry, config) -> bool          register-based own predicate (see hard_stop.is_own)
  guard_issue_write(source, issue, op, via_guard=False, config=None) -> Verdict
  record_done_net(log, config) -> list[str]         class-matching log entries with no recorded stop

Own predicate: a branch under `hard_stops.own_branch_prefixes` (default `sdlc/`) or `feature/<unit>` of a registered
unit is Sigma's own, so its lease push is not classified. A merge Sigma performs under `work.auto_merge` is own by
chokepoint, never by pattern: `merge` is not in the pattern table, and `context["own_merge"]` returns an allowing
verdict. Anything unreadable is cannot-tell, which blocks: an unreadable action is not a permitted one.

Legacy alias: `gates.irreversible_actions` (deploy, delete, overwrite, spend, migrate) maps to the class toggles
(production_change, destroy, rewrite_history, spend, production_change); an entry removed from that list turns its
class off unless `hard_stops.classes` names the class. `gates.on_block` other than `park` is ignored: a block parks.

LEVEL-2 CAP: nothing here maps a hard-stop class to an auto-apply path; a structural test pins that.
Stdlib only; no network or process module (hard_stop pins the same).
"""
import collections
import importlib.util
import pathlib

_HERE = pathlib.Path(__file__).resolve().parent

Verdict = collections.namedtuple("Verdict", "blocked cls pattern_id quote reason")
ALLOW = Verdict(False, None, None, "", "")

BODY_MARKER = "Raised automatically by the SDLC loop"
OWN_LABEL_KEYS = ("goal_label", "parked_label", "follow_up_label")
_ALIAS = {"deploy": "production_change", "delete": "destroy", "overwrite": "rewrite_history",
          "spend": "spend", "migrate": "production_change"}

_IN_GUARD = []          # non-empty while the guard performs its own park write (exempt: parking is the remedy)
_HS = None


def _hs():
    global _HS
    if _HS is None:
        spec = importlib.util.spec_from_file_location("hard_stop", _HERE / "hard_stop.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        _HS = m
    return _HS


def enabled(config):
    try:
        block = config.get("decision_rubric") if isinstance(config, dict) else None
        hs = block.get("hard_stops") if isinstance(block, dict) else None
        return isinstance(hs, dict) and hs.get("enabled") is True
    except Exception:
        return False


def is_own(branch, registry, config):
    return _hs().is_own(branch, registry, config)


def _effective(config, only=None):
    """The config the classifier sees: the legacy alias folded in, and `only` restricting the classes."""
    block = dict(config["decision_rubric"])
    hs = dict(block["hard_stops"])
    classes = dict(hs["classes"]) if isinstance(hs.get("classes"), dict) else {}
    gates = config.get("gates") if isinstance(config, dict) else None
    listed = gates.get("irreversible_actions") if isinstance(gates, dict) else None
    if isinstance(listed, list) and all(isinstance(x, str) for x in listed):
        for cls in set(_ALIAS.values()):
            if cls not in classes:
                classes[cls] = any(_ALIAS.get(e) == cls for e in listed)
    if only is not None:
        for name in _hs().registered():
            if name not in only:
                classes[name] = False
            elif name not in classes:
                classes[name] = True
    hs["classes"] = classes
    block["hard_stops"] = hs
    return {"decision_rubric": block}


def _reason(res):
    if res.cls == _hs().CANNOT_TELL:
        return "hard stop: cannot tell whether this action is safe (%s)" % (res.pattern_id or "unknown")
    return "hard stop: %s (%s)" % (res.cls, res.pattern_id)


def guard(action_text, context, config):
    """-> Verdict. Blocked for a matched class or cannot-tell; allowed otherwise. Never raises."""
    try:
        if not enabled(config):
            return ALLOW
        ctx = context if isinstance(context, dict) else {}
        if ctx.get("own_merge") is True:
            return ALLOW
        res = _hs().classify(action_text, ctx, _effective(config, ctx.get("only")))
        if res.cls is None:
            return ALLOW
        if res.cls == _hs().CANNOT_TELL and res.pattern_id == "parse-error" and ctx.get("prose") is True:
            return ALLOW      # a diff is prose, not a shell line: an unbalanced quote in it says nothing
        if res.cls != _hs().CANNOT_TELL and ctx.get("permitted") is True and _hs().is_spend_like(res.cls):
            return ALLOW      # the recorded spend permission (spend_approval) cleared a spend-like class
        return Verdict(True, res.cls, res.pattern_id, res.quote, _reason(res))
    except Exception:
        return Verdict(True, _hs().CANNOT_TELL, "error", "", "hard stop: cannot tell (guard error)")


def _sigma_created(source, info):
    labels = {(l.get("name") if isinstance(l, dict) else l) for l in (info.get("labels") or [])}
    own = {getattr(source, k, None) for k in OWN_LABEL_KEYS}
    own.discard(None)
    if labels & own:
        return True
    return BODY_MARKER in (info.get("body") or "")


def guard_issue_write(source, issue, op, via_guard=False, config=None):
    """-> Verdict for a destructive write to an existing `issue`. Allowed for a Sigma-labelled or Sigma-created issue;
    otherwise cannot-tell (blocked). `via_guard=True` (the guard's own park and note) is exempt, so it cannot recurse."""
    try:
        cfg = config if config is not None else getattr(source, "_hs_config", None)
        if via_guard or _IN_GUARD or not enabled(cfg) or issue is None:
            return ALLOW
        try:
            info = source._read_issue(issue, ["labels", "body"])
            created = _sigma_created(source, info)
        except Exception:
            created = False
        if created:
            return ALLOW
        return Verdict(True, _hs().CANNOT_TELL, "foreign-issue-" + str(op), "",
                       "hard stop: %s on an issue Sigma did not create or label (cannot tell)" % op)
    except Exception:
        return Verdict(True, _hs().CANNOT_TELL, "error", "", "hard stop: cannot tell (guard error)")


def park_issue(source, issue, verdict):
    """The guard's own park write: exempt from guarding (parking is the remedy). Best effort; never raises."""
    _IN_GUARD.append(1)
    try:
        source.park(issue, verdict.reason)
    except Exception:
        pass
    finally:
        _IN_GUARD.pop()


def blocks_issue_write(source, issue, op):
    """Source-method head helper: True when the write must be skipped (and the issue parked instead)."""
    v = guard_issue_write(source, issue, op)
    if not v.blocked:
        return False
    park_issue(source, issue, v)
    return True


_TEXT_KEYS = ("action", "command")


def _text_of(entry):
    for k in _TEXT_KEYS:
        if isinstance(entry.get(k), str) and entry[k]:
            return entry[k]
    return None


def _is_stop(entry):
    kind = str(entry.get("kind", ""))
    return "park" in kind or "permission" in kind


def record_done_net(log, config):
    """-> list of refusal lines: each log entry whose action text classifies to a hard-stop class and that has no
    recorded stop (a permission-use or park entry carrying the same action text). Empty when the gate is closed."""
    try:
        if not enabled(config):
            return []
        entries = [e for e in (log or []) if isinstance(e, dict)]
        stops = {_text_of(e) for e in entries if _is_stop(e)}
        out = []
        for e in entries:
            action = _text_of(e)
            if _is_stop(e) or action is None or action in stops:
                continue
            v = guard(action, {}, config)
            if v.blocked:
                out.append("%s: %s" % (v.pattern_id, v.quote))
        return out
    except Exception:
        return ["hard stop: record-done net could not read the log"]
