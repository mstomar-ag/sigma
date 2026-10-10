#!/usr/bin/env python3
"""Gate holds as ordinary parks (decision rubric slice 16 of story 988). Library only: no main.

The two pick gates (`feature_propagate` for a scope expansion, `feature_owner` for an ownership
refusal) used to hand a goal to a human by writing the confirmation label. With AI-filed triage on
(`ai_filed.triage.enabled`, default true) they now PARK the goal instead, with the declared question
kind `scope_hold` or `owner_hold` written as the machine line inside the park comment. With triage
switched off (exactly false) nothing here changes the old behaviour.

This module is the one home of three things the writers, `/sigma-promote` and `/sigma-unpark` share:
  - `park_for_gate`: the park write, one atomic label swap through the source's own park;
  - `surface_mode`: which write a source can do, so a gate never fails open by accident;
  - `gate_hold_blocks`: the question "would the very next pick set this goal aside again?" asked
    through each gate's own predicate (moved here from promote, which now calls it).
"""
import importlib.util
import pathlib

_HERE = pathlib.Path(__file__).resolve().parent

PARK = "park"
LABEL = "label"
GATE_KINDS = ("scope_hold", "owner_hold")

#: Appended to a gate's flag comment when the goal was parked (the old text names a promotion, which
#: refuses a parked goal).
PARK_NOTE = ("\n\nThis goal is parked. After the registry edit, `/sigma-unpark` returns it to the "
             "board; it refuses while the registry still holds it.")

_CACHE = {}


def _load(name):
    if name not in _CACHE:
        spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _CACHE[name] = mod
    return _CACHE[name]


def park_enabled(config):
    """True unless `ai_filed.triage.enabled` is exactly the JSON boolean false. Never raises."""
    try:
        block = config.get("ai_filed") if isinstance(config, dict) else None
        tri = block.get("triage") if isinstance(block, dict) else None
        return not (isinstance(tri, dict) and tri.get("enabled") is False)
    except Exception:                                   # noqa: BLE001 - total by contract
        return True


def _callable(source, names):
    return all(callable(getattr(source, m, None)) for m in names)


def surface_mode(source, config, extra):
    """-> "park", "label" or None. `extra` are the methods the caller needs beside the write.
    Park mode needs `park`; a source that cannot park but can still write the confirmation label
    keeps the label write (never silently nothing); a source with neither is None, which the gates
    treat as the existing no-surface outcome."""
    if park_enabled(config) and _callable(source, ("park",) + tuple(extra)):
        return PARK
    if _callable(source, ("mark_needs_confirmation",) + tuple(extra)):
        return LABEL
    return None


def park_for_gate(source, goal, kind, detail):
    """Park `goal` with the declared gate kind on the comment. -> True unless the write raised.
    One call to the source's own park: the swap that adds the parked label also removes the goal
    membership labels, so no parked-plus-confirmation pair can result."""
    from_ = _load("qkind")
    if kind not in GATE_KINDS:
        raise ValueError("not a gate-hold kind: %r" % (kind,))
    text = "%s\n%s" % (" ".join(str(detail or "").split()), from_.render_line(kind))
    try:
        source.park(str(goal), text)
    except Exception:                                   # noqa: BLE001 - the refusal stands
        return False
    return True


def declared_gate_kind(comments):
    """The gate kind declared by the newest park comment that declares any kind, else None."""
    q = _load("qkind")
    found = None
    for body in comments or []:
        kind = q.parse_line(body or "")
        if kind:
            found = kind
    return found if found in GATE_KINDS else None


def scope_expansion(sdlc_dir, config, unit):
    """The repo this goal is in when that repo is NOT one the unit lists, else None."""
    registry = _load("feature_registry")
    sync = _load("feature_sync")
    features_dir = registry.registry_dir(sdlc_dir)
    if not features_dir.is_dir():
        return None
    raw = registry.read(features_dir).get(unit)
    if raw is None:
        return None
    repo = sync.repo_slug(config, sync._run, str(pathlib.Path(sdlc_dir).parent),
                          sync.DEFAULT_REMOTE)
    return repo if sync.is_scope_expansion(registry.normalise_entry(raw), repo) else None


def feature_hold(sdlc_dir, config, number, names, author, gesture="promote"):
    """The sentence naming a gate that would set this goal STRAIGHT BACK aside, or None. Scope first,
    then ownership (the pick's own order; a goal in an unlisted repo has no board owner there, so asking
    ownership first would print a reason the pick would not give). `gesture` is "promote" or "unpark"
    and only changes the words. NEVER RAISES; fails open on every axis. Behaviour moved
    unchanged from `promote._feature_hold`, whose docstring keeps the reasoning."""
    try:
        features = _load("features")
        try:
            unit = features.parse_labels(sorted(names))
        except features.AmbiguousUnit:
            return None
        if not unit:
            return None
        label = _load("feature_labels").label_for(unit)
        ing = "Unparking" if gesture == "unpark" else "Promoting"
        repo = scope_expansion(sdlc_dir, config, unit)
        if repo:
            return ("#%s carries %s and %s is not one of the repos that unit lists — adding one "
                    "expands the unit's scope, which is its owner's decision (§7.3). %s it "
                    "returns it to the board and the next pick sets it aside again — add %s to "
                    "that unit's repos in the registry, then %s."
                    % (number, label, repo, ing, repo, gesture))
        owner = _load("feature_owner")
        verdict = owner.would_hold(sdlc_dir, config, unit, author)
        if verdict is not None:
            return ("#%s carries %s and unit ownership still holds it: %s. %s it returns it "
                    "to the board and the next pick sets it aside again — set "
                    "repos.%s.authorized = true in the registry, or correct the owner recorded "
                    "there, then %s."
                    % (number, label,
                       owner.refusal_clause(verdict, unit, verdict.repo,
                                            "the account that opened it"),
                       ing, verdict.repo, gesture))
        return None
    except Exception:                                   # noqa: BLE001 - a wrong refusal is the wedge
        return None


def gate_hold_blocks(source, goal, config, sdlc_dir=None, names=None, author=None,
                     gesture="promote"):
    """The refusal sentence when a gate still holds `goal`, else None. Never raises.

    `names` (label names) and `author` are read from `source` when omitted. Fails open (None) when
    the project directory, the labels or the author cannot be had."""
    try:
        sdlc_dir = sdlc_dir or getattr(source, "sdlc_dir", None)
        if not sdlc_dir:
            return None
        if names is None:
            names = {(l.get("name") or "") if isinstance(l, dict) else str(l)
                     for l in (source._read_issue(goal, ["labels"]).get("labels") or [])}
        if author is None:
            author = source.fetch_author(goal) or ""
        return feature_hold(str(sdlc_dir), config, goal, names, author, gesture)
    except Exception:                                   # noqa: BLE001 - a wrong refusal is the wedge
        return None
