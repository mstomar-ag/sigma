#!/usr/bin/env python3
"""Rulebook file and the propose-approve flow (decision rubric, slice 13 of story 988; library only; no main).

A rule says: for a question kind in a repository area, recommend this option. Rules belong to the
repository, never a person: the schema is a closed key set, so a rule scoped to a login (or any other
person field) is invalid and never acts. Rules are RECOMMEND AND PRE-FILL ONLY: `match` never decides.

- `propose` and `import_rules` add rules as `proposed` (a reviewable diff of one committed JSON file,
  plus `park_line` for the single park that asks the owner). Hand-written and imported rules take the
  same road: nothing starts approved.
- `approve` is refused unless `autonomy.is_owner` says yes (the one disclosed read of a login as
  authority, allow-listed in the login scan with `autonomy.is_owner`); the approver is not stored.
  Approval stamps a time and a seal (a hash of the rule's content). The seal is tamper evidence, not
  authentication: a rule whose status says approved but whose seal does not match its content (a
  hand edit) is read as proposed and never acts.
- `match` returns a recommendation only for exactly one sealed rule; none or several mean ask. A
  hard-stop kind never matches.
- `due_for_reask` lists approved rules whose confirmation is older than `rulebook.reask_days`.
- Everything is inert unless `decision_rubric.rulebook.enabled` is the JSON boolean true: no file is
  written, nothing matches.

SCALE: one file of human-approved rules, read whole (small by construction); writes are a single
atomic replace and assume one writer at a time (a concurrent edit is a normal git merge of one file).
"""
import datetime
import hashlib
import importlib.util
import json
import os
import pathlib
import tempfile
import uuid

_HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_PATH = ".sdlc/rulebook.json"
DEFAULT_REASK_DAYS = 90
FIELDS = ("id", "qkind", "area", "option", "reason", "status", "created", "confirmed", "seal")
_REQUIRED = ("id", "qkind", "area", "option", "status")
_SEALED = ("id", "qkind", "area", "option", "reason", "created", "confirmed")
STATUSES = ("proposed", "approved")


class Match:
    """A recommendation. `mode` is always recommend: a rule never acts by itself."""

    def __init__(self, rule_id, option):
        self.rule_id = rule_id
        self.option = option
        self.mode = "recommend"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _sub(config):
    try:
        sub = config["decision_rubric"]["rulebook"]
        return sub if isinstance(sub, dict) else {}
    except Exception:
        return {}


def enabled(config):
    try:
        return _sub(config).get("enabled") is True and _load("decision_rubric_cfg").load(config).part_enabled("rulebook")
    except Exception:
        return False


def _path_ok(p):
    if not isinstance(p, str) or not p.strip() or os.path.isabs(p):
        return False
    return ".." not in pathlib.PurePosixPath(p.replace("\\", "/")).parts


def settings(config):
    """`path` and `reask_days`; a wrong-typed or unsafe value reads as its default."""
    sub = _sub(config)
    days = sub.get("reask_days")
    days = days if isinstance(days, int) and not isinstance(days, bool) and days >= 1 else DEFAULT_REASK_DAYS
    path = sub.get("path")
    return {"path": path if _path_ok(path) else DEFAULT_PATH, "reask_days": days}


def _file(sdlc_dir, config):
    """The rulebook file: the configured path is relative to the repository root (parent of the state dir)."""
    return os.path.join(os.path.dirname(os.path.abspath(str(sdlc_dir))), settings(config)["path"])


def _now(now):
    return now or datetime.datetime.now(datetime.timezone.utc)


def _seal(rule):
    body = json.dumps({k: rule.get(k) for k in _SEALED}, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def validate(rule):
    """-> (ok, reason). Closed key set: any key outside FIELDS (a person scope included) is invalid."""
    if not isinstance(rule, dict):
        return False, "a rule must be an object"
    extra = sorted(str(k) for k in rule if k not in FIELDS)
    if extra:
        return False, "unknown rule field(s): %s (rules are repository-scoped; no person fields)" % ", ".join(extra)
    for k in _REQUIRED:
        if not isinstance(rule.get(k), str) or not rule[k].strip():
            return False, "rule field %s must be a non-empty string" % k
    for k in ("reason", "created", "confirmed", "seal"):
        if k in rule and not isinstance(rule[k], str):
            return False, "rule field %s must be a string" % k
    if rule["qkind"] not in _load("qkind").QKINDS:
        return False, "unknown question kind %r" % rule["qkind"]
    if rule["status"] not in STATUSES:
        return False, "status must be proposed or approved"
    return True, None


def _read_raw(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        rules = data.get("rules") if isinstance(data, dict) else None
        return [r for r in rules if isinstance(r, dict)] if isinstance(rules, list) else []
    except (OSError, ValueError):
        return []


def _effective(rule):
    out = dict(rule)
    if rule["status"] == "approved" and not (rule.get("confirmed") and rule.get("seal") == _seal(rule)):
        out["status"] = "proposed"
    return out


def load(sdlc_dir, config):
    """Valid rules with their EFFECTIVE status (an unsealed approved rule reads as proposed)."""
    if not enabled(config):
        return []
    return [_effective(r) for r in _read_raw(_file(sdlc_dir, config)) if validate(r)[0]]


def _write(path, rules):
    folder = os.path.dirname(path)
    if os.path.islink(folder):
        raise OSError("rulebook folder is a link")
    os.makedirs(folder, exist_ok=True)
    text = json.dumps({"rules": rules}, indent=2, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(dir=folder, prefix="._rulebook", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def propose(sdlc_dir, draft, config, now=None):
    """Add `draft` as a PROPOSED rule; returns the file path, or None when closed or the draft is invalid."""
    if not enabled(config) or not isinstance(draft, dict):
        return None
    t = _now(now)
    rule = {k: draft[k] for k in ("qkind", "area", "option", "reason") if k in draft}
    extra = [k for k in draft if k not in ("qkind", "area", "option", "reason")]
    rule.update(id="rule-%s-%s" % (t.strftime("%Y%m%dT%H%M%S"), uuid.uuid4().hex[:6]), status="proposed",
                created=t.isoformat())
    ok, _ = validate(rule)
    if extra or not ok:
        return None
    path = _file(sdlc_dir, config)
    _write(path, _read_raw(path) + [rule])
    return path


def import_rules(sdlc_dir, drafts, config, now=None):
    """Imported rules go through `propose` one by one; returns the new ids (invalid drafts are skipped)."""
    if not enabled(config) or not isinstance(drafts, (list, tuple)):
        return []
    before = {r["id"] for r in load(sdlc_dir, config)}
    for d in drafts:
        propose(sdlc_dir, d, config, now=now)
    return [r["id"] for r in load(sdlc_dir, config) if r["id"] not in before]


def park_line(rule_id):
    """The text of the single park that asks the owner to approve `rule_id` (declares the owner_hold kind)."""
    q = _load("qkind")
    return "Rule %s is proposed in the rulebook and needs owner approval.\n%s" % (rule_id, q.render_line("owner_hold"))


def approve(sdlc_dir, rule_id, login, config, now=None):
    """Approve a proposed rule. PermissionError unless the closed gate is open and `login` is an owner
    (`autonomy.is_owner`); KeyError for an unknown or invalid rule. The approver is not recorded."""
    if not enabled(config) or not _load("autonomy").is_owner(login, config):
        raise PermissionError("rule approval is refused")
    path = _file(sdlc_dir, config)
    rules = _read_raw(path)
    for r in rules:
        if r.get("id") == rule_id and validate(r)[0]:
            r["status"] = "approved"
            r["confirmed"] = _now(now).isoformat()
            r["seal"] = _seal(r)
            _write(path, rules)
            return None
    raise KeyError(rule_id)


def match(qkind, area, option, rules):
    """The one sealed approved rule for this kind and area, as a recommendation; else None (ask).
    `option` is None or the list of options on offer (the rule's option must be among them)."""
    try:
        if _load("hard_stop").is_hardstop_kind(qkind, None, {}):
            return None
        hits = [r for r in rules if isinstance(r, dict) and r.get("status") == "approved"
                and r.get("qkind") == qkind and r.get("area") == area
                and (option is None or r.get("option") in option)]
        return Match(hits[0]["id"], hits[0]["option"]) if len(hits) == 1 else None
    except Exception:
        return None


def prefill(sdlc_dir, qkind, area, option, config):
    """A pre-fill suggestion {recommend, rule, mode} or None. Never decides."""
    m = match(qkind, area, option, load(sdlc_dir, config))
    return {"recommend": m.option, "rule": m.rule_id, "mode": m.mode} if m else None


def _parse(text):
    try:
        d = datetime.datetime.fromisoformat(text)
        return d if d.tzinfo else d.replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


def due_for_reask(rules, now, config):
    """Ids of approved rules confirmed more than `rulebook.reask_days` ago (an unreadable time counts as due)."""
    if not enabled(config):
        return []
    limit = datetime.timedelta(days=settings(config)["reask_days"])
    out = []
    for r in rules:
        if isinstance(r, dict) and r.get("status") == "approved":
            t = _parse(r.get("confirmed"))
            if t is None or now - t > limit:
                out.append(r.get("id"))
    return out
