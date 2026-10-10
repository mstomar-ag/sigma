#!/usr/bin/env python3
"""The unit landing approval and the unattended guard (upkeep part C, slice 6). Offline, stdlib only.

WHAT IT DECIDES. Whether a landing of one feature unit may proceed: attended (the person named this unit on THIS
invocation and no driven launcher is present) or approved (a per-unit approval record, made by an explicit verb, is
consumed exactly once). Nothing here merges, pushes, deletes, spawns a process or reaches the network; the caller does
the merge AFTER `authorize` returns ok, and the single-use marker is already on disk by then.

THE CARRIER (owner ruling). A record under `<sdlc>/state/unit-approvals/`, a fresh directory no predecessor wrote to.
The file name is a digest of repository slug, folded unit key and the verified head, so one approval fits exactly one
unit in one repository at one head, and no unit name appears in a path. The record repeats those three fields, and an
expiry; a record that does not parse, or whose fields do not match, denies. CONSUME writes `<digest>.used` with
O_CREAT|O_EXCL BEFORE returning, so of any number of concurrent callers exactly one wins. A denied consume (wrong head,
expired, malformed) burns nothing. Expired and used files are never deleted by this module: a delete-shaped call is
refused in this tree, so growth is one small file per approval a person made by hand (an operator may clear the
directory). Portability of O_EXCL outside POSIX local disks is unmeasured.

ATTENDANCE. The code can see a flag on this invocation's argv and launcher fingerprints; it cannot see who typed the
flag. The consent flag is the two tokens `--user-requested <unit>`; fingerprints (the three internal hand-off names
below) can only DENY consent, never grant it. An agent that fabricates the flag in an attended session is an accepted,
documented residual. In marker mode the approval is self-usable by anyone holding the project directory.

NATIVE REVIEW (optional, stronger). `native_review` wants an APPROVED review by someone other than the pull request
author, pinned to the verified head. A solo maintainer has no distinct approver, so it fails closed: land attended.
The `sigma:approve` comment is never an approval here.

OFF BY DEFAULT. Every entry point is behind the upkeep project gate; closed means nothing is read, written or created.
"""
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import sys
import functools
import time

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


registry = _load("feature_registry")
state = _load("state")
upkeep = _load("feature_upkeep")


def _gate_first(fn):
    """The project-door gate is each entry point's first action (a closed gate returns the closed verdict without
    calling the body). Written out here, not through the gate's decorator, so no shipped entry point is registered
    with the slice-1 trap harness yet: nothing calls these functions in this slice."""
    @functools.wraps(fn)
    def guarded(config, *args, **kwargs):
        verdict = upkeep.evaluate(config, "project")
        if not verdict["open"]:
            return {"closed": True, "door": "project", "missing": verdict["missing"], "problems": verdict["problems"]}
        return fn(config, *args, **kwargs)
    return guarded

STATE_PARTS = ("state", "unit-approvals")
CONSENT_FLAG = "--user-requested"
FINGERPRINTS = ("SIGMA_RUN_ID", "SIGMA_AUTOWATCH_HOP", "SIGMA_SESSION_GENERATION")
DEFAULT_TTL_SECONDS = 3600
MAX_TTL_SECONDS = 86400
_HEAD = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?\Z")
_SLUG = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}\Z")


def _deny(reason):
    return {"ok": False, "reason": reason}


def _check(unit, slug, head):
    if not (isinstance(unit, str) and registry.is_unit_name(unit)):
        return "not a unit name"
    if not (isinstance(slug, str) and _SLUG.match(slug)):
        return "repository slug must look like owner/name"
    if not (isinstance(head, str) and _HEAD.match(head)):
        return "head must be a full lowercase commit id"
    return None


def approval_path(sdlc_dir, slug, head, unit):
    """-> (record path, marker path). The unit goes through the one fold; the name carries only a digest."""
    key = registry.unit_key(unit)
    digest = hashlib.sha256("\0".join((slug, key, head)).encode("utf-8")).hexdigest()[:40]
    base = pathlib.Path(sdlc_dir).joinpath(*STATE_PARTS)
    return base / (digest + ".json"), base / (digest + ".used")


def _now(now):
    return int(time.time()) if now is None else now


@_gate_first
def approve(config, sdlc_dir, unit, slug, head, *, ttl_seconds=DEFAULT_TTL_SECONDS, now=None):
    """The explicit verb: record that a person approves landing `unit` at `head` for `ttl_seconds`."""
    why = _check(unit, slug, head)
    if why:
        return _deny(why)
    if not (isinstance(ttl_seconds, int) and not isinstance(ttl_seconds, bool) and 0 < ttl_seconds <= MAX_TTL_SECONDS):
        return _deny("ttl must be a whole number of seconds from 1 to %d" % MAX_TTL_SECONDS)
    record, _ = approval_path(sdlc_dir, slug, head, unit)
    try:
        state.refuse_symlinks(sdlc_dir, pathlib.Path(*STATE_PARTS, record.name), create_parents=True)
    except OSError:
        return _deny("approval path has a symlink component; refusing")
    created = _now(now)
    body = {"v": 1, "unit": registry.unit_key(unit), "slug": slug, "head": head,
            "created_at": created, "expires_at": created + ttl_seconds}
    state.atomic_write_text(record, json.dumps(body, sort_keys=True))
    return {"ok": True, "expires_at": body["expires_at"]}


@_gate_first
def consume(config, sdlc_dir, unit, slug, head, *, now=None):
    """Validate the record for exactly this unit, slug and head, then take the single-use marker (create-once)."""
    why = _check(unit, slug, head)
    if why:
        return _deny(why)
    record, marker = approval_path(sdlc_dir, slug, head, unit)
    try:
        state.refuse_symlinks(sdlc_dir, pathlib.Path(*STATE_PARTS, record.name))
        state.refuse_symlinks(sdlc_dir, pathlib.Path(*STATE_PARTS, marker.name))
    except OSError:
        return _deny("approval path has a symlink component; refusing")
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _deny("no readable approval for this unit at this head; run the approve verb")
    if not isinstance(data, dict):
        return _deny("approval record is malformed")
    expires = data.get("expires_at")
    if not (isinstance(expires, int) and not isinstance(expires, bool)):
        return _deny("approval record is malformed")
    if (data.get("unit"), data.get("slug"), data.get("head")) != (registry.unit_key(unit), slug, head):
        return _deny("approval record does not match this unit, repository and head")
    if _now(now) >= expires:
        return _deny("approval expired; run the approve verb again")
    try:
        fd = os.open(str(marker), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _deny("approval for this head was already used (single use); a landing needs a new head")
    except OSError as exc:
        return _deny("could not take the single-use marker: %s" % type(exc).__name__)
    os.close(fd)
    return {"ok": True}


@_gate_first
def native_review(config, *, pr_author, head, reviews):
    """Optional stronger mode: an APPROVED review by someone other than the author, pinned to `head`."""
    author = str(pr_author or "").lower()
    for item in reviews if isinstance(reviews, list) else ():
        if not isinstance(item, dict) or item.get("state") != "APPROVED":
            continue
        login = str((item.get("author") or {}).get("login") or "").lower()
        oid = (item.get("commit") or {}).get("oid")
        if login and login != author and oid == head:
            return {"ok": True}
    return _deny("no distinct approver: land attended")


def _attended(argv, environ, unit):
    tokens = list(argv or ())
    consent = any(a == CONSENT_FLAG and b == unit for a, b in zip(tokens, tokens[1:]))
    driven = any(name in (environ or {}) for name in FINGERPRINTS)
    return consent and not driven


@_gate_first
def authorize(config, sdlc_dir, unit, slug, head, *, argv, environ, now=None,
              require_native_review=False, pr_author=None, reviews=None):
    """The guard conjunction. Attended consent lands; anything else needs a consumed approval."""
    why = _check(unit, slug, head)
    if why:
        return _deny(why)
    if require_native_review:
        verdict = native_review(config, pr_author=pr_author, head=head, reviews=reviews)
        if not verdict["ok"]:
            return verdict
    if _attended(argv, environ, unit):
        return {"ok": True, "mode": "attended"}
    verdict = consume(config, sdlc_dir, unit, slug, head, now=now)
    if verdict["ok"]:
        return {"ok": True, "mode": "approval"}
    return _deny("unattended landing needs a unit approval: " + verdict["reason"])


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Create a short-lived, single-use approval to land one feature unit.")
    parser.add_argument("verb", choices=["approve"])
    parser.add_argument("sdlc_dir")
    parser.add_argument("unit")
    parser.add_argument("--slug", required=True, help="repository as owner/name")
    parser.add_argument("--head", required=True, help="verified full commit id of the unit tip")
    parser.add_argument("--ttl-minutes", type=int, default=DEFAULT_TTL_SECONDS // 60)
    args = parser.parse_args(argv)
    state = _load("state")
    try:
        config = state.load_config(args.sdlc_dir)
    except Exception as exc:
        print("refused: %s" % type(exc).__name__, file=sys.stderr)
        return 2
    out = approve(config, args.sdlc_dir, args.unit, args.slug, args.head, ttl_seconds=args.ttl_minutes * 60)
    if out.get("closed"):
        print("refused: the upkeep gate is closed", file=sys.stderr)
        return 4
    if not out["ok"]:
        print("refused: " + out["reason"], file=sys.stderr)
        return 3
    print("approved until epoch %d" % out["expires_at"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
