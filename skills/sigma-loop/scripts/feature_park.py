"""Level 3 of unit-conflict handling: the pure parts of a PARK -- identity, brief and close rules.

A park is a unit rebase that stopped on a conflict nobody resolved. The unit stays where it is, the remote is
untouched, and a person gets ONE finding per conflict with a capped brief. Everything here is a pure function over
text, or a read-only git question asked through the kit's injected runner `run(cwd, argv) -> stdout`. Nothing in
this module reads configuration, writes a file, files an issue or closes one: the engine does those, behind the
upkeep gate. It joins no filesystem path, so it is not an address builder for the unit-key fold inventory.

CONFLICT IDENTITY is a hash of the unit key, the original commit's patch-id (for a merge, which has none: its subject
and its distance from the root) and the sorted conflicted paths. It is NOT the unit tip, which is what the older
filing key was, so one conflict is one finding however many times the branch tip moves.

THE BRIEF IS CAPPED on every axis it can grow on: files, commits per file, bytes per hunk excerpt, total bytes and
a wall-clock budget. It runs no `gh` call at all (the attended brief runs one per commit and has no cap), takes shas
rather than local branch names, and reads conflicted paths NUL-separated. Quoted text is untrusted: every registered
marker spelling is broken before it can be posted, and any path under a kit directory is withheld, because the
upstream router withholds a finding that cites one.

THE FILED-STORE VALUE for a park slot is a small record `{"fingerprint", "issue", "reused"}`. `fingerprint_of` and
`issue_of` also read the older plain-string value, which carries no issue number ("unknown issue").
"""
import hashlib
import importlib.util
import pathlib
import re
import time

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


legacy = _load("legacy")
conflict_state = _load("conflict_state")

SLOT_PREFIX = "park:"
MAX_FILES = 12
MAX_COMMITS_PER_FILE = 3
MAX_EXCERPT_BYTES = 1200
MAX_BRIEF_BYTES = 8000
BUDGET_SECONDS = 20.0
WITHHELD_PATH = "[a kit path, withheld]"
KIT_DIRS = ("skills", "hooks", ".claude-plugin")

_MARKER_LEAD = re.compile(r"(?i)(%s)(?=[:\-])" % legacy.MARKER_PREFIX_RE)
_KIT_PATH = re.compile(r"(?<![\w.-])(?:%s)/[^\s`'\"<>()]*" % "|".join(re.escape(d) for d in KIT_DIRS))


def conflict_id(unit, patch_id, subject, position, paths):
    """16 hex characters naming one conflict: stable across tip moves, different for a different commit or file set."""
    first = patch_id.strip() if isinstance(patch_id, str) and patch_id.strip() else "merge:%s:%s" % (subject, position)
    material = "\0".join([str(unit).lower(), first, *sorted(paths)])
    return hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()[:16]


def slot_for(cid):
    return SLOT_PREFIX + cid


def neutralise(text):
    """Break every marker spelling and every kit path in quoted text. Idempotent. A comment opener is broken too."""
    text = _MARKER_LEAD.sub(lambda m: m.group(1) + " ", str(text or ""))
    text = text.replace("<!--", "<! --")
    return _KIT_PATH.sub(WITHHELD_PATH, text)


def safe_path(path):
    """A conflicted path as it may appear in a posted text: withheld when it sits under a kit directory."""
    top = str(path).split("/", 1)[0]
    return WITHHELD_PATH if top in KIT_DIRS else neutralise(path)


def _excerpt(text):
    start = text.find("<<<<<<<")
    chunk = text[start if start >= 0 else 0:]
    raw = chunk.encode("utf-8", "replace")[:MAX_EXCERPT_BYTES]
    return raw.decode("utf-8", "ignore")


def build_brief(run, cwd, base_ref, unit, branch, original, paths, read_text, now=time.monotonic, budget=BUDGET_SECONDS):
    """The capped park brief as markdown, or "" when there is nothing to say. NEVER RAISES.

    `original` is `{"sha", "subject"}` of the commit being replayed; `paths` the conflicted paths; `read_text(path)`
    returns a file's current text (the conflict markers are in it). The caller renders this BEFORE the worktree is
    dropped. Every git question is bounded by the count caps, and the loop stops at the wall-clock budget."""
    started = now()
    lines = ["The replay of `%s` onto `%s` stopped at %s (%s)."
             % (neutralise(branch), neutralise(base_ref),
                ("`%s`" % str(original.get("sha", ""))[:12]) if original.get("sha") else "a commit", neutralise(original.get("subject", ""))),
             "", "Conflicted file(s): %d." % len(paths), ""]
    shown = sorted(paths)[:MAX_FILES]
    for path in shown:
        if now() - started > budget:
            lines.append("- (brief time budget reached; the remaining files are not described)")
            break
        lines.append("- `%s`" % safe_path(path))
        if path.split("/", 1)[0] in KIT_DIRS:
            continue
        try:
            log = str(run(cwd, ["git", "log", "-n", str(MAX_COMMITS_PER_FILE), "--format=%h %s", base_ref, "--", path]))
        except Exception:                 # noqa: BLE001 - a brief is best-effort
            log = ""
        for one in log.splitlines()[:MAX_COMMITS_PER_FILE]:
            lines.append("  - base side: %s" % neutralise(one.strip()))
        try:
            excerpt = _excerpt(str(read_text(path) or ""))
        except Exception:                 # noqa: BLE001
            excerpt = ""
        if excerpt:
            lines += ["", "  ```", *("  " + one for one in neutralise(excerpt).splitlines()), "  ```", ""]
    footer = ["- and %d more file(s) not listed" % (len(paths) - len(shown))] if len(paths) > len(shown) else []
    body = "\n".join(lines).rstrip() + "\n"
    tail = ("\n".join(footer) + "\n") if footer else ""
    room = MAX_BRIEF_BYTES - len(tail.encode("utf-8", "replace"))
    if len(body.encode("utf-8", "replace")) > room:
        body = body.encode("utf-8", "replace")[:room].decode("utf-8", "ignore").rstrip() + "\n(brief cut at its size cap)\n"
    return body + tail


# --------------------------------------------------------------------------- the filed-store value


def record(fingerprint, issue, reused):
    return {"fingerprint": fingerprint, "issue": issue, "reused": bool(reused)}


def fingerprint_of(value):
    """The fingerprint of a stored slot value, old (a string) or new (a record). None when it is neither."""
    if isinstance(value, dict):
        got = value.get("fingerprint")
        return got if isinstance(got, str) else None
    return value if isinstance(value, str) else None


def issue_of(value):
    """The issue number a stored slot value names, or None ("unknown issue": an old string value, or no number)."""
    if isinstance(value, dict):
        got = value.get("issue")
        if isinstance(got, bool):
            return None
        if isinstance(got, int):
            return got
        if isinstance(got, str) and got.isdigit():
            return int(got)
    return None


def closable(store, resolved):
    """-> `[(slot, issue)]` safe to close. A slot qualifies only when it is a park slot named in `resolved`, it names an
    issue, that issue was not REUSED (a reused duplicate belongs to someone else's work), and no live slot outside
    `resolved` still points at the same issue number."""
    resolved = set(resolved)
    live = {issue_of(v) for slot, v in store.items() if slot not in resolved and issue_of(v) is not None}
    out = []
    for slot in sorted(resolved):
        value = store.get(slot)
        issue = issue_of(value)
        if (slot.startswith(SLOT_PREFIX) and isinstance(value, dict) and issue is not None
                and not value.get("reused") and issue not in live):
            out.append((slot, issue))
    return out
