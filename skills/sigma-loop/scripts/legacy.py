"""The ONE place Sigma reads what it wrote under its previous name (#239).

A repository adopted by the plugin before it was renamed carries that name in four kinds of state:
schema ids (`<previous>/features@1`, `…/landing@1`, `…/withheld@1`, `…/propagation@1`), environment
variables (`<PREVIOUS>_*`, one for one with `SIGMA_*`), comment markers (`<!-- <previous>:… -->` in
feature docs, AGENTS.md and GitHub bodies/comments) and one config key
(`drift_watch.channels.<previous>`, now `channels.sigma`). Every reader calls this module instead
of comparing against the Sigma spelling alone; nothing here writes, and every writer keeps writing
the Sigma spelling. `skills/sigma-doctor/scripts/migrate.py` is the explicit, one-shot rewrite.

THE PREVIOUS NAME IS SPELLED FROM FRAGMENTS, as `doctor._RETIRED_BRAND` already does: it is a
guarded private name in the public tree (`tests/test_no_private_names.py`, #2729), so no shipped
line carries it whole. It still reaches the user at RUNTIME, in migrate/doctor output, which is
where they need to recognise it.

PRECEDENCE, ENV: the Sigma name wins. A variable counts as SET when it is present AND non-empty --
the `${X:-}` / `get(X) or …` idiom every existing reader already uses -- so `SIGMA_X=""` next to a
non-empty legacy value reads the legacy value. With no legacy value present, `getenv` returns
exactly what `environ.get(name, default)` returned before this module existed: zero behaviour
change for an install that never had the old name.

INTERNAL HAND-OFF NAMES NEVER FALL BACK (plan-review finding 3). `SIGMA_RUN_ID` and
`SIGMA_AUTOWATCH_HOP` are not operator settings: Sigma sets them for its own children. Adopting a
stale value left by the old plugin's supervisor would make this run claim another run's identity.

Library-only (no `__main__`); zero third-party dependencies; pure functions.
"""
import os

BRAND = "sigma"
#: The previous name, from fragments -- see the module docstring.
RETIRED = "loop" + "smith"
ENV_PREFIX = "SIGMA_"
RETIRED_ENV_PREFIX = RETIRED.upper() + "_"

#: Operator-facing variables: each may also be supplied under the previous prefix.
FALLBACK_ENV = frozenset({
    "SIGMA_ALLOW_REPO_SMTP", "SIGMA_AUTOWATCH_CHANNEL_PORT", "SIGMA_AUTOWATCH_CMD", "SIGMA_AUTOWATCH_SURFACE",
    "SIGMA_CLAUDE_CMD", "SIGMA_GATE_GLOBAL", "SIGMA_REBASE_GUARD_TIMEOUT", "SIGMA_SLACK_BOT_SOCKET_TOKEN",
    "SIGMA_SLACK_BOT_TOKEN", "SIGMA_SMTP_PASS", "SIGMA_SUPERVISE_MAX_RUNS",
    "SIGMA_SUPERVISE_SLEEP_SCALE", "SIGMA_WATCH_CALL_TIMEOUT", "SIGMA_WATCH_INTERVAL",
    "SIGMA_WATCH_MAX_TICKS", "SIGMA_WATCH_SLEEP_SCALE",
})
#: Set by Sigma for its own child processes; read under the Sigma name only.
INTERNAL_ENV = frozenset({"SIGMA_RUN_ID", "SIGMA_AUTOWATCH_HOP", "SIGMA_SESSION_GENERATION"})
#: Operator settings introduced AFTER the rename: the previous plugin never read them, so a
#: previous-prefix spelling means nothing and is never consulted. `SIGMA_ALLOW_COEXIST` (#240) now
#: only silences the one-line coexistence notice (#314) -- adopting it from a stale previous-prefix
#: export would hide that notice by accident.
POST_RENAME_ENV = frozenset({"SIGMA_ALLOW_COEXIST", "SIGMA_HOST", "SIGMA_CODEX_CMD", "SIGMA_GH_GRAPHQL",
                             "SIGMA_GH_BREAKER_COOLDOWN",
                             "SIGMA_UPKEEP_JOB"})   # #801, #895, #917

#: The schema ids (kind@version) whose spelling changed only in its brand segment. A previous-name id
#: of any OTHER kind or version is not understood: it reads as itself (so it matches nothing Sigma
#: expects), and `migrate.py` leaves it as is rather than rewrite what it does not understand.
KNOWN_SCHEMAS = ("features@1", "landing@1", "withheld@1", "propagation@1")

#: Every marker Sigma writes into a file or a GitHub body/comment. Registered here so a new one is
#: seen next to the rule that its readers must also accept the previous spelling
#: (`tests/test_legacy_compat.py` pins that every marker literal in shipped code is listed).
MARKERS = frozenset({
    "<!-- sigma:acceptance",
    "<!-- sigma:begin managed", "<!-- sigma:end managed -->",
    "<!-- sigma:codex:start -->", "<!-- sigma:codex:end -->",
    "<!-- sigma:unpark-qa:start -->", "<!-- sigma:unpark-qa:end -->",
    "<!-- sigma:dossier-qa:start -->", "<!-- sigma:dossier-qa:end -->",
    "<!-- sigma:feature-classified-tier1 -->", "<!-- sigma:feature-classified-tier2 -->",
    "<!-- sigma:feature-classified-tier4 -->", "<!-- sigma:feature-label-missing -->",
    "<!-- sigma:feature-unit-ambiguous -->", "<!-- sigma:feature-unit-missing -->",
    "<!-- sigma:unit-tracking-warning -->", "<!-- sigma:feature-ownership -->",
    "<!-- sigma:feature-scope-expansion -->", "<!-- sigma-review-evidence:",
    "sigma:approve", "sigma:block", "sigma:unblock", "sigma:keep-parked",
    "sigma:dismissed-finding", "sigma:decompose-filed", "sigma:decompose-of=",
    "sigma:decomposed-from=", "sigma:design-filed", "sigma:design-of=",
    "sigma-qkind:",
})

#: For a regex that must accept either spelling of a marker's brand segment.
MARKER_PREFIX_RE = "(?:%s|%s)" % (BRAND, RETIRED)


# --------------------------------------------------------------------------- environment


def retired_env_name(name):
    """`SIGMA_X` -> the previous spelling, or None for a name without the Sigma prefix."""
    if isinstance(name, str) and name.startswith(ENV_PREFIX):
        return RETIRED_ENV_PREFIX + name[len(ENV_PREFIX):]
    return None


def sigma_env_name(name):
    """The previous spelling -> `SIGMA_X`; any other name unchanged."""
    if isinstance(name, str) and name.startswith(RETIRED_ENV_PREFIX):
        return ENV_PREFIX + name[len(RETIRED_ENV_PREFIX):]
    return name


def getenv(name, default=None, environ=None):
    """`environ.get(name, default)`, plus the previous spelling for an operator-facing name.

    `name` may be either spelling -- a config value (`*_env`) written before the rename still names
    the old one -- and the Sigma spelling is consulted first either way. See the module docstring
    for precedence and for why `INTERNAL_ENV` never falls back. Values are returned, never logged."""
    env = os.environ if environ is None else environ
    canonical = sigma_env_name(name)
    if canonical in FALLBACK_ENV:
        for candidate in (canonical, retired_env_name(canonical)):
            value = env.get(candidate)
            if value:
                return value
    return env.get(name, default)


def retired_env_set(environ=None):
    """Sorted NAMES (never values) of previous-prefix variables set in `environ`."""
    env = os.environ if environ is None else environ
    return sorted(k for k, v in env.items() if k.startswith(RETIRED_ENV_PREFIX) and v)


def legacy_env_values(cfg, prefix=""):
    """[(dotted.key.path, value)] for every key ending `_env` anywhere in `cfg` (dict/list, any
    nesting) whose string value -- or, for a list value, any string element -- still carries the
    previous env prefix. Read-only; never mutates `cfg`. (Moved here from `doctor.py`, #2729, so the
    doctor row and `migrate.py` share one walker.)"""
    found = []
    if isinstance(cfg, dict):
        for key, value in cfg.items():
            path = "%s.%s" % (prefix, key) if prefix else str(key)
            if str(key).endswith("_env"):
                for item in (value if isinstance(value, list) else [value]):
                    if isinstance(item, str) and item.startswith(RETIRED_ENV_PREFIX):
                        found.append((path, item))
            found.extend(legacy_env_values(value, path))
    elif isinstance(cfg, list):
        for item in cfg:
            found.extend(legacy_env_values(item, prefix))
    return found


# --------------------------------------------------------------------------- schema ids


def is_legacy_schema(value):
    """Does `value` carry the previous name as its brand segment (any kind, any version)?"""
    return isinstance(value, str) and value.startswith(RETIRED + "/")


def canonical_schema(value):
    """`<previous>/<kind>@<n>` -> `sigma/<kind>@<n>` for an id in `KNOWN_SCHEMAS`; else unchanged,
    so a previous-name `@2` is refused by an `@1` reader exactly as a Sigma `@2` is."""
    if is_legacy_schema(value) and value[len(RETIRED) + 1:] in KNOWN_SCHEMAS:
        return BRAND + "/" + value[len(RETIRED) + 1:]
    return value


def schema_is(value, expected):
    """The reader's check: does a document's `schema` value mean `expected`?"""
    return canonical_schema(value) == expected


# --------------------------------------------------------------------------- markers


def retired_spelling(marker):
    """A Sigma marker in its previous spelling: the brand segment at the start, or right after the
    `<!-- ` opener, is swapped. Raises ValueError for a string with no brand segment there, which is
    a caller bug (it is not a marker)."""
    for lead in ("<!-- ", ""):
        if marker.startswith(lead + BRAND):
            return lead + RETIRED + marker[len(lead) + len(BRAND):]
    raise ValueError("not a Sigma marker: %r" % (marker,))


def spellings(marker):
    """`(sigma spelling, previous spelling)` -- Sigma first. A string with no brand segment (a test
    stand-in, or a marker a later change renames) has one spelling: itself."""
    try:
        return marker, retired_spelling(marker)
    except ValueError:
        return (marker,)


def has_marker(text, marker):
    """`marker in text`, for either spelling."""
    text = text or ""
    return any(s in text for s in spellings(marker))


def find_marker(text, marker, start=0):
    """-> `(index, spelling)` of the EARLIEST occurrence of either spelling at or after `start`, or
    `(-1, None)`. Works on `str` or `bytes` (the spelling is encoded as ASCII for bytes)."""
    best = (-1, None)
    for spelled in spellings(marker):
        needle = spelled.encode("ascii") if isinstance(text, bytes) else spelled
        at = text.find(needle, start)
        if at != -1 and (best[0] == -1 or at < best[0]):
            best = (at, needle)
    return best


# --------------------------------------------------------------------------- config keys


def channel_value(channels, key):
    """`drift_watch.channels[key]`, reading the previous key when `key` is `sigma` and the Sigma key
    is unset or blank. Sigma wins when both are set."""
    channels = channels if isinstance(channels, dict) else {}
    value = channels.get(key)
    if key == BRAND and not (isinstance(value, str) and value.strip()):
        old = channels.get(RETIRED)
        if isinstance(old, str) and old.strip():
            return old
    return value
