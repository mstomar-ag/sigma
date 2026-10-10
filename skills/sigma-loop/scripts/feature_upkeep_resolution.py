"""The stamp and the resolution record for a unit conflict the engine settled itself (part B, slice 4).

A library, loaded by path; no CLI, and NOTHING CALLS IT YET: the caller is the Level 1 loop of a later slice, which sits
behind the upkeep gate. This module is not a reader of the project config block and carries no gate of its own; the
only configuration it touches is the ledger's, handed through to the ledger's own writer.

THE STAMP. One trailer line in the commit BODY, after a blank line, with a lowercase key:
`sigma-resolution: <level> <run id>` (level 1 or 2, run id 12 lowercase hex). The subject is never touched, because the
pull-request arrival classifier reads the subject alone and anchors `(#N)` to its end; a stamp that joins the subject
(no blank line) defeats that anchor and the commit reads as DIRECT. The blank line is built here, never assumed. The
stamp carries no number, no closing-keyword shape and no substring of a registered marker. It is a FIND AID (`git log
--grep`), not authentication: a hand-forged stamp looks identical, so nothing may trust it.

THE STAMPING COMMIT. Made at the stop, before `git rebase --continue` (which keeps a commit made at the stop unchanged,
measured). It keeps the ORIGINAL author's name, email and timestamp, read from `REBASE_HEAD`: a plain commit at a stop
takes the configured identity and the current time (measured), which would misattribute the human's work and break the
authorship key the proof gate pairs commits by. D-42 (PROVISIONAL): the author-flags form ships; the amend-with-trailer
form, whose git floor was never checked, does not. The commit passes `--no-verify` and `--no-gpg-sign`: an adopter's hook
or a signing key that prompts must not run or stall in an unattended replay. Measured: `--continue` runs only the
prepare-commit-msg and post-commit hooks and never signs, and the engine's commit runs exactly those two and never signs,
so it adds nothing the continuation would not have done; a plain commit would run all four hooks and ask the signer.

THE RECORD. One JSON document per resolution under `state/upkeep/resolutions/`, written atomically with a symlink
refusal, read with a size cap, and pruned by age. It is posted to the ledger as one UNADDRESSED note (so autowatch and the
webhook stay out) and, when a prior park filed a finding for the same conflict, as a comment on that finding. With no
finding nothing person-facing is created (D-21): the record and the ledger note are always written.
"""
import collections
import importlib.util
import json
import os
import pathlib
import re
import secrets
import time

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


_LOADED = {}


def _sibling(name):
    """A sibling module, loaded once per process."""
    if name not in _LOADED:
        _LOADED[name] = _load(name)
    return _LOADED[name]


STAMP_KEY = "sigma-resolution"
LEVELS = (1, 2)
RUN_ID_CHARS = 12
SCHEMA_ID = "upkeep-resolution/1"
#: Below the `.sdlc` directory. ONE constant (a join would make a segment equal the gate's block name).
STORE_REL = "state/upkeep/resolutions"
SUFFIX = ".json"
MAX_RECORD_CHARS = 65536
MAX_FILES = 200
MAX_PRUNE_SCAN = 100000
NOTE_PREFIX = "upkeep-"
NOTE_REF = "upkeep:resolved:"

_RUN_ID = re.compile(r"[0-9a-f]{12}")
_HEX40 = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_RAW_DATE = re.compile(r"[0-9]+ [+-][0-9]{4}")
_TRAILER_LINE = re.compile(r"[A-Za-z][A-Za-z0-9-]*: \S")
_RECORD_NAME = re.compile(r"\.?(?P<unit>.+)-(?P<run>[0-9a-f]{12})\.json(?:\.tmp)?")
#: A closing keyword followed by an issue ref: the shape GitHub acts on. Same shape as the repository's own pin.
_CLOSING = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b[\s:]*#\d+", re.I)
_NUMBERED = re.compile(r"#[0-9]")
_URL_NUMBER = re.compile(r"/(?:issues|pull|pulls)/[0-9]", re.I)

WriteResult = collections.namedtuple("WriteResult", "ok reason path")


class StampError(ValueError):
    """The stamp, its message or its commit cannot be made safely. Never swallowed: a half-stamped resolution must stop."""


# --------------------------------------------------------------------------- the stamp


def new_run_id():
    """12 lowercase hex characters from the system's random source."""
    return secrets.token_hex(RUN_ID_CHARS // 2)


def stamp_line(level, run_id):
    """`sigma-resolution: <level> <run id>`. Refuses a level outside LEVELS and a run id that is not 12 lowercase hex."""
    if type(level) is not int or level not in LEVELS:
        raise StampError("level must be 1 or 2")
    if not (isinstance(run_id, str) and _RUN_ID.fullmatch(run_id)):
        raise StampError("run id must be %d lowercase hex characters" % RUN_ID_CHARS)
    return "%s: %d %s" % (STAMP_KEY, level, run_id)


def wording_problems(text):
    """Why `text` may not appear in a stamp, a record or a comment -> list of short reasons, empty when clean.

    No `#` followed by a digit (which also covers every closing keyword, `owner/repo#N` and a bare reference), no
    issue or pull-request URL path, and no substring of any registered marker in either spelling. Plain-text checks,
    not parsing: a marker reader uses a plain substring test, so a quoted marker is not inert."""
    legacy = _sibling("legacy")
    text = text if isinstance(text, str) else ""
    found = []
    if _NUMBERED.search(text):
        found.append("issue-number")
    if _CLOSING.search(text):
        found.append("closing-keyword")
    if _URL_NUMBER.search(text):
        found.append("issue-url")
    if any(legacy.has_marker(text, marker) for marker in sorted(legacy.MARKERS)):
        found.append("marker")
    return found


def stamp_message(message, level, run_id):
    """The commit message with the stamp appended: after a blank line, or as one more line of a trailing trailer block.

    The first paragraph is the subject and is never a trailer block, so a one-line message always gets its blank line.
    Raises StampError for an empty message or a stamp the wording check refuses."""
    line = stamp_line(level, run_id)
    problems = wording_problems(line)
    if problems:
        raise StampError("the stamp fails the wording check: " + ", ".join(problems))
    text = str(message or "").replace("\r\n", "\n").rstrip()
    if not text.strip():
        raise StampError("an empty message has no subject to keep")
    paragraphs = text.split("\n\n")
    last = paragraphs[-1].split("\n")
    if len(paragraphs) > 1 and all(_TRAILER_LINE.match(item) for item in last):
        return text + "\n" + line + "\n"
    return text + "\n\n" + line + "\n"


# --------------------------------------------------------------------------- the stamping commit


def _fields(run, cwd, ref):
    out = str(run(cwd, ["git", "log", "-1", "--format=%an%x00%ae%x00%ad", "--date=raw", ref]) or "").rstrip("\n")
    parts = out.split("\0")
    if len(parts) != 3 or not parts[0] or not parts[1] or not _RAW_DATE.fullmatch(parts[2]):
        raise StampError("could not read the authorship of " + ref)
    return tuple(parts)


def original_authorship(run, cwd, ref="REBASE_HEAD"):
    """(name, email, raw date) of the commit being replayed. `<`, `>` and a line break in a name or email are refused:
    they cannot be carried in `--author` without changing who it names."""
    name, email, date = _fields(run, cwd, ref)
    if any(ch in value for value in (name, email) for ch in "<>\n"):
        raise StampError("the original author cannot be carried through --author")
    return name, email, date


def authorship_problems(run, cwd, original, new):
    """The authorship fields (`name`, `email`, `date`) that differ between two commits -> list, empty when they match."""
    before, after = _fields(run, cwd, original), _fields(run, cwd, new)
    return [label for label, a, b in zip(("name", "email", "date"), before, after) if a != b]


def stamp_commit(run, cwd, level, run_id, ref="REBASE_HEAD"):
    """Commit the staged resolution at the stop with the original's message plus the stamp and the original's author.

    -> the new commit's sha. Refuses while any path is still unmerged, and an empty result is left to git's own refusal
    (the emptiness check lives in `conflict_state`; the caller asks it first). The committer is the engine, which is true.
    Call it BEFORE `git rebase --continue`, which keeps the commit unchanged. Not for a merge stop."""
    if _sibling("conflict_state").conflicted_paths(run, cwd):
        raise StampError("unmerged paths remain; resolve and stage them before the stamping commit")
    name, email, date = original_authorship(run, cwd, ref)
    message = stamp_message(str(run(cwd, ["git", "log", "-1", "--format=%B", ref]) or ""), level, run_id)
    run(cwd, ["git", "commit", "--no-verify", "--no-gpg-sign", "--cleanup=verbatim",
              "--author=%s <%s>" % (name, email), "--date=" + date, "-m", message])
    return str(run(cwd, ["git", "rev-parse", "HEAD"]) or "").strip()


# --------------------------------------------------------------------------- the record


def store_path(sdlc_dir, run_id, unit):
    """The one place a unit NAME and a run id become the path of a resolution record. Refuses what
    `feature_registry.unit_path` refuses and folds the name the same way, AFTER the refusal."""
    registry = _sibling("feature_registry")
    if not (isinstance(unit, str) and registry.is_unit_name(unit)):
        raise registry.InvalidUnitName("%r is not a unit name, so it has no resolution record" % (unit,))
    if not (isinstance(run_id, str) and _RUN_ID.fullmatch(run_id)):
        raise ValueError("run id must be %d lowercase hex characters" % RUN_ID_CHARS)
    return pathlib.Path(sdlc_dir) / STORE_REL / ("%s-%s%s" % (registry.unit_key(unit), run_id, SUFFIX))


def make_record(unit, level, run_id, original_tip, new_tip, backup_ref, files, checks, model=None, cost_usd=None,
                verdict=None, route=None, at=None):
    """A resolution record. `files` maps each conflicted path to the sha256 of its resolved blob; `checks` maps a check
    name to a short result word. The last four are Level 2 only. Raises ValueError for any field out of shape."""
    if type(level) is not int or level not in LEVELS:
        raise ValueError("level must be 1 or 2")
    if not (isinstance(run_id, str) and _RUN_ID.fullmatch(run_id)):
        raise ValueError("run id must be %d lowercase hex characters" % RUN_ID_CHARS)
    for label, tip in (("original_tip", original_tip), ("new_tip", new_tip)):
        if not (isinstance(tip, str) and _HEX40.fullmatch(tip)):
            raise ValueError(label + " must be a full commit hash")
    if not (isinstance(backup_ref, str) and 0 < len(backup_ref) <= 200):
        raise ValueError("backup_ref must be a short ref name")
    if not (isinstance(files, dict) and 0 < len(files) <= MAX_FILES
            and all(isinstance(k, str) and k and isinstance(v, str) and _HEX64.fullmatch(v) for k, v in files.items())):
        raise ValueError("files must map 1 to %d paths to sha256 digests" % MAX_FILES)
    if not (isinstance(checks, dict) and len(checks) <= 50
            and all(isinstance(k, str) and 0 < len(k) <= 60 and isinstance(v, str) and len(v) <= 60 for k, v in checks.items())):
        raise ValueError("checks must map short names to short result words")
    record = {"schema": SCHEMA_ID, "unit": unit, "level": level, "run_id": run_id, "original_tip": original_tip,
              "new_tip": new_tip, "backup_ref": backup_ref, "files": dict(files), "checks": dict(checks),
              "at": int(time.time() if at is None else at)}
    if level == 2:
        record.update({"model": model, "cost_usd": cost_usd, "verdict": verdict, "route": route})
    return record


def _strings(value, skip_keys=()):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if key not in skip_keys:
                if isinstance(key, str):
                    yield key
                yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def record_wording_problems(record):
    """The wording check over every text of a record except the conflicted paths (data, not prose)."""
    found = []
    for text in _strings({k: v for k, v in record.items() if k != "files"}):
        found += [p for p in wording_problems(text) if p not in found]
    return found


def write_record(sdlc_dir, record):
    """Write one record atomically -> WriteResult(ok, reason, path). `reason` is `bad-unit`, `wording` or `unwritable`; a
    failure of the file system is a result, never an exception. Refuses a symlink anywhere on the path."""
    state = _sibling("state")
    try:
        path = store_path(sdlc_dir, record.get("run_id"), record.get("unit"))
    except ValueError:
        return WriteResult(False, "bad-unit", None)
    if record_wording_problems(record):
        return WriteResult(False, "wording", None)
    text = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
    if len(text) > MAX_RECORD_CHARS:
        return WriteResult(False, "too-large", None)
    try:
        state.refuse_symlinks(sdlc_dir, path.relative_to(pathlib.Path(sdlc_dir)), create_parents=True)
        state.atomic_write_text(path, text)
    except OSError:
        return WriteResult(False, "unwritable", None)
    return WriteResult(True, None, path)


def read_record(sdlc_dir, unit, run_id):
    """The record, or None when it is absent, too large, not JSON or not of this schema. Never raises on file content."""
    try:
        path = store_path(sdlc_dir, run_id, unit)
        if path.is_symlink() or path.stat().st_size > MAX_RECORD_CHARS:
            return None
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    return doc if isinstance(doc, dict) and doc.get("schema") == SCHEMA_ID else None


def prune(sdlc_dir, keep_days, now=None):
    """Remove record files (and the leftover temporary of a crashed write) older than `keep_days` -> how many were removed.
    Touches only names of the record shape, never follows or removes a symlink, scans at most MAX_PRUNE_SCAN entries and
    skips what it cannot stat or remove. `keep_days` is the caller's (the gate module owns the setting)."""
    if type(keep_days) is not int or keep_days < 1:
        raise ValueError("keep_days must be a positive whole number")
    cutoff = (time.time() if now is None else now) - keep_days * 86400
    base = pathlib.Path(sdlc_dir) / STORE_REL
    removed = 0
    try:
        with os.scandir(base) as entries:
            for seen, entry in enumerate(entries):
                if seen >= MAX_PRUNE_SCAN:
                    break
                if not _RECORD_NAME.fullmatch(entry.name):
                    continue
                try:
                    if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                        continue
                    if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                        os.unlink(entry.path)
                        removed += 1
                except OSError:
                    continue
    except OSError:
        return 0
    return removed


# --------------------------------------------------------------------------- the ledger note and the comment


def _summary(record):
    return "level %d resolution %s: %d file(s), checks %s" % (
        record["level"], record["run_id"], len(record["files"]),
        ",".join("%s=%s" % item for item in sorted(record["checks"].items())) or "none")


def ledger_note(sdlc_dir, config, record):
    """One UNADDRESSED `note`: no recipient, so autowatch and the webhook never pick it up. Keyed by the folded unit name,
    with a ref carrying the first 12 characters of the new tip. In process, never through the loop's note command.
    -> the entry, or None when the ledger is off, the text fails the wording check or the append failed (the ledger's
    own writer never raises)."""
    registry = _sibling("feature_registry")
    why = _summary(record)
    if wording_problems(why) or not registry.is_unit_name(record["unit"]):
        return None
    return _sibling("ledger").safe_append(sdlc_dir, "note", NOTE_PREFIX + registry.unit_key(record["unit"]),
                                          config=config, ref=NOTE_REF + record["new_tip"][:12], why=why)


def comment_body(record):
    """The text of the comment on a finding. Carries no number of its own."""
    lines = ["The upkeep engine resolved this conflict itself (level %d, run %s)." % (record["level"], record["run_id"]),
             "Backup ref taken before the rewrite: " + record["backup_ref"],
             "Resolved files (sha256 of each resolved blob):"]
    lines += ["- %s %s" % (digest[:16], path) for path, digest in sorted(record["files"].items())]
    lines.append("Checks: " + (", ".join("%s=%s" % item for item in sorted(record["checks"].items())) or "none"))
    return "\n".join(lines)


def post_comment(gh_run, finding, repo, record, sdlc_dir=None):
    """Comment the record on the finding a prior park filed -> {"posted": bool, "reason": str|None}.

    `finding` must be a positive integer and `repo` an explicit `owner/name`; the text must pass the wording check.
    NOT idempotent (the REST write may have landed): the caller posts once, just before closing the finding. With no
    finding the caller does not call this, and nothing person-facing exists."""
    gh_api = _sibling("gh_api")
    if type(finding) is not int or finding < 1:
        return {"posted": False, "reason": "bad-finding"}
    if not (isinstance(repo, str) and _REPO.fullmatch(repo)):
        return {"posted": False, "reason": "bad-repo"}
    body = comment_body(record)
    if wording_problems(body):
        return {"posted": False, "reason": "wording"}
    try:
        gh_api.comment_issue(gh_run, finding, body, repo, sdlc_dir=sdlc_dir)
    except gh_api.GhApiError:
        return {"posted": False, "reason": "gh-failed"}
    return {"posted": True, "reason": None}
