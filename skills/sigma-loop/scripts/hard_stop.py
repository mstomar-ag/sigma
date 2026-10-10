"""Hard-stop classifier core (decision rubric, slice 6, #995). Pure library, UNWIRED: nothing imports it yet.

`classify(action_text, context, config)` maps one concrete action text to a Result(cls, pattern_id, quote):
`cls` is a hard-stop class, None (nothing matched, or the gate is closed, or the action is Sigma's own),
or "cannot-tell". An error of any kind (non-string input, a shell parse failure, a raising detector, a bad
extra pattern) is "cannot-tell", NEVER None: an unreadable action is not a permitted one. A matched class
wins over a parse failure. `raise_to(prior, declared)` is a max-merge, so a caller can raise a verdict and
never lower it (order: None < "cannot-tell" < any class).

Classes: six default-on (`spend`, `destroy`, `rewrite_history`, `production_change`, `send_outside`,
`access_secrets`) and two default-off (`tamper_oversight`, `unvetted_execution`), each a config toggle
`hard_stops.classes.<name>`. The gate is `hard_stops.enabled`, exact JSON true; anything else classifies
nothing. `config` may be the `decision_rubric` block itself or a dict holding it under that key (PROVISIONAL
until the wiring slice fixes the shape). Patterns are shape-based and deliberately conservative.

EXTENSION POINT: `register_class(name, detector, spend_like=False)` adds a class. `detector(action_text,
context)` returns a pattern id (truthy) on a match and None otherwise; it may raise, which reads as
"cannot-tell". A registered class is on unless `hard_stops.classes.<name>` is false. Names must be new
identifiers; `raise_to` and `classify` refuse a class that is not registered. `spend_like` marks a class the
spend permission may clear (see `is_spend_like`). The later costly-model port registers one entry.

Own carve-out: a force push to a branch `is_own` reports (prefix `sdlc/` by default, or `feature/<unit>` for
a unit in the registry) is Sigma's routine lease push and returns None with pattern id `own-branch`.

Stdlib only; imports no network or process-spawning module (a test pins this). The scrubber and the registry
module are loaded lazily by path.
"""
import collections
import importlib.util
import pathlib
import re
import shlex

_HERE = pathlib.Path(__file__).resolve().parent

Result = collections.namedtuple("Result", "cls pattern_id quote")

CANNOT_TELL = "cannot-tell"
OWN_PATTERN_ID = "own-branch"
DEFAULT_ON = ("spend", "destroy", "rewrite_history", "production_change", "send_outside", "access_secrets")
DEFAULT_OFF = ("tamper_oversight", "unvetted_execution")
CLASSES = DEFAULT_ON + DEFAULT_OFF
DEFAULT_OWN_PREFIXES = ("sdlc/",)
DEFAULT_HARDSTOP_KINDS = ("irreversible",)
QUOTE_MAX = 120

_I = re.IGNORECASE
_PATTERNS = {
    "spend": (
        ("spend-pod", re.compile(r"\brunpodctl\s+(?:create|start)\b", _I)),
        ("spend-instance", re.compile(r"\b(?:run-instances|gpu\s+create)\b", _I)),
        ("spend-fal", re.compile(r"--backend[ =]fal\b|\bfal\.run\b", _I)),
    ),
    "destroy": (
        ("destroy-rm", re.compile(r"\brm\s+(?:-\w*[rR]\w*[fF]\w*|-\w*[fF]\w*[rR]\w*|--recursive)\b")),
        ("destroy-rm-split", re.compile(r"\brm\b(?=[^|;&\n]*\s(?:-\w*[rR]\w*|--recursive)(?:\s|$))(?=[^|;&\n]*\s(?:-\w*[fF]\w*|--force)(?:\s|$))")),
        ("destroy-remote-branch", re.compile(r"\bgit\s+push\b[^|;&\n]*?(?:\s--delete\b|\s-d\b|\s:[^\s:]\S*)")),
        ("destroy-branch-D", re.compile(r"\bgit\s+branch\b[^|;&\n]*?\s-\w*D\w*\b")),
        ("destroy-find", re.compile(r"\bfind\b[^|;&\n]*?\s-delete\b")),
        ("destroy-sql", re.compile(r"\b(?:drop\s+(?:table|database|schema)|truncate\s+table)\b", _I)),
        ("destroy-infra", re.compile(r"\b(?:terraform\s+destroy|gh\s+repo\s+delete|kubectl\s+delete|git\s+clean\s+-\w*f)", _I)),
    ),
    "rewrite_history": (
        ("rewrite-force-push", re.compile(r"\bgit\s+push\b[^|;&]*?(?:--force\b|--force-with-lease\b|--force-if-includes\b|\s-\w*f\w*\b|\s\+\S)")),
        ("rewrite-filter", re.compile(r"\bgit\s+(?:filter-branch|filter-repo)\b|\bgit\s+reset\s+--hard\b")),
    ),
    "production_change": (
        ("prod-apply", re.compile(r"\b(?:kubectl\s+(?:apply|rollout)|terraform\s+apply|helm\s+(?:upgrade|install))\b", _I)),
        ("prod-deploy", re.compile(r"\b(?:helm\s+upgrade|kubectl\s+apply|gh\s+workflow\s+run\s+\S*deploy)", _I)),
    ),
    "send_outside": (
        ("send-http", re.compile(r"\bcurl\b[^|;&]*?(?:-X\s*(?:POST|PUT|PATCH|DELETE)\b|\s(?:-d|--data\w*|-F|--form|-T|--upload-file)\b)", _I)),
        ("send-mail", re.compile(r"\b(?:sendmail|mailx?|scp|rsync)\b\s")),
    ),
    "access_secrets": (
        ("secrets-ssh", re.compile(r"\.ssh/(?:id_|authorized_keys|config)")),
        ("secrets-env", re.compile(r"\b(?:cat|less|more|head|tail|cp|source)\s+\S*\.env\b")),
        ("secrets-cli", re.compile(r"\b(?:gh\s+auth\s+token|gh\s+secret|security\s+find-generic-password|aws\s+configure\s+get)\b", _I)),
    ),
    "tamper_oversight": (
        ("tamper-no-verify", re.compile(r"--no-verify\b")),
        ("tamper-hooks", re.compile(r"\bcore\.hooksPath\b|\.git/hooks/", _I)),
    ),
    "unvetted_execution": (
        ("unvetted-pipe-shell", re.compile(r"\b(?:curl|wget)\b[^\n]*\|\s*(?:sudo\s+)?(?:ba|z)?sh\b", _I)),
        ("unvetted-eval-fetch", re.compile(r"\b(?:eval|source|bash|sh)\b[^\n]*[<$]\(\s*(?:curl|wget)\b", _I)),
    ),
}

_PUSH_RX = dict(_PATTERNS["rewrite_history"])["rewrite-force-push"]

# name -> {"detector": callable(text, context) -> pattern id | None, "spend_like": bool}
_REGISTRY = {}


def _builtin_detector(name):
    def detect(text, context):
        for pid, rx in _PATTERNS[name]:
            m = rx.search(text)
            if m:
                return pid, m.group(0)
        return None
    return detect


for _n in CLASSES:
    _REGISTRY[_n] = {"detector": _builtin_detector(_n), "spend_like": _n == "spend", "builtin": True}


def register_class(name, detector, spend_like=False):
    """Add a class (the extension point, see the module docstring). Refuses a bad or taken name and a non-callable."""
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise ValueError("hard_stop: class name must be a lowercase identifier")
    if name in _REGISTRY or name == CANNOT_TELL:
        raise ValueError("hard_stop: class already registered: " + name)
    if not callable(detector):
        raise ValueError("hard_stop: detector must be callable")
    _REGISTRY[name] = {"detector": detector, "spend_like": bool(spend_like), "builtin": False}


def registered():
    return tuple(_REGISTRY)


def is_spend_like(name):
    return bool(_REGISTRY.get(name, {}).get("spend_like"))


def _rank(v):
    if v is None:
        return 0
    if v in _REGISTRY:
        return 2
    return 1  # CANNOT_TELL and anything unknown read as cannot-tell


def raise_to(prior, declared):
    """Max-merge: the higher of the two, never lower. A name that is neither None, cannot-tell nor registered
    is refused (ValueError) when it is the declared value; an unknown prior reads as cannot-tell."""
    if declared is not None and declared != CANNOT_TELL and declared not in _REGISTRY:
        raise ValueError("hard_stop: unregistered class: %r" % (declared,))
    p = prior if _rank(prior) else None
    if p is not None and p != CANNOT_TELL and p not in _REGISTRY:
        p = CANNOT_TELL
    return declared if _rank(declared) > _rank(p) else p


def _block(config):
    if not isinstance(config, dict):
        return {}
    inner = config.get("decision_rubric")
    cfg = inner if isinstance(inner, dict) else config
    hs = cfg.get("hard_stops")
    return hs if isinstance(hs, dict) else {}


def _class_on(name, hs):
    classes = hs.get("classes")
    if isinstance(classes, dict) and name in classes:
        return classes[name] is True
    return name not in DEFAULT_OFF


def _own_prefixes(hs):
    p = hs.get("own_branch_prefixes")
    if isinstance(p, (list, tuple)) and all(isinstance(x, str) for x in p):
        return tuple(x for x in p if x)
    return DEFAULT_OWN_PREFIXES


def _unit_key(name):
    try:
        spec = importlib.util.spec_from_file_location("feature_registry", _HERE / "feature_registry.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.unit_key(name)
    except Exception:
        return None


def is_own(branch, registry, config):
    """True when Sigma owns `branch`: a configured prefix, or `feature/<unit>` for a unit in `registry` (the
    mapping `feature_registry.read` returns) when `hard_stops.own_registered_units` is not false. An
    unreadable registry is not-own for non-prefix branches."""
    if not isinstance(branch, str) or not branch:
        return False
    hs = _block(config)
    if any(branch.startswith(p) for p in _own_prefixes(hs)):
        return True
    if hs.get("own_registered_units", True) is False or not branch.startswith("feature/"):
        return False
    if not isinstance(registry, dict):
        return False
    want = _unit_key(branch[len("feature/"):])
    if want is None:
        return False
    return any(isinstance(k, str) and _unit_key(k) == want for k in registry)


def is_hardstop_kind(qkind, record, config):
    """Shared by the autonomy ladder and the filer: true for a kind in `hard_stops.hardstop_kinds` (default
    irreversible) or any record carrying a `hardstop_class`. Not gated by `hard_stops.enabled`: the cap is fixed."""
    kinds = _block(config).get("hardstop_kinds")
    if not (isinstance(kinds, (list, tuple)) and all(isinstance(k, str) for k in kinds)):
        kinds = DEFAULT_HARDSTOP_KINDS
    if isinstance(record, dict) and record.get("hardstop_class"):
        return True
    return isinstance(qkind, str) and qkind in kinds


_FLAG_VALUE = re.compile(r"(?i)(?<!\S)(--?(?:token|password|passwd|secret|api[-_]?key|auth\w*|key|p)(?:[= ]|\s+))\S+")
_ENV_PREFIX = re.compile(r"(?<!\S)([A-Za-z_][A-Za-z0-9_]*=)\S+")
_OPAQUE = re.compile(r"\b(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{16,}\b")


def _scrub_quote(span):
    try:
        spec = importlib.util.spec_from_file_location("scrub", _HERE / "scrub.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        span = mod.scrub(span)
    except Exception:
        return "[unquotable]"
    span = _FLAG_VALUE.sub(lambda m: m.group(1) + "[redacted]", span)
    span = _ENV_PREFIX.sub(lambda m: m.group(1) + "[redacted]", span)
    span = _OPAQUE.sub("[redacted]", span)
    return span[:QUOTE_MAX]


def _line_of(text, span):
    i = text.find(span)
    start = text.rfind("\n", 0, i) + 1 if i >= 0 else 0
    end = text.find("\n", start)
    return text[start:end if end >= 0 else len(text)]


def _push_target(tokens):
    """The destination branch of the last refspec in a `git push` token list, or None."""
    try:
        rest = tokens[tokens.index("push") + 1:]
    except ValueError:
        return None
    pos = [t for t in rest if not t.startswith("-")]
    if len(pos) != 2:
        return None
    ref = pos[1].lstrip("+")
    ref = ref.split(":")[-1]
    return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref


def _own_push(text, context, registry, config):
    """True only when every force-push segment of `text` targets an owned branch (or the context names one)."""
    branch = context.get("branch") if isinstance(context, dict) else None
    if branch:
        return is_own(branch, registry, config)
    pushes = [seg for seg in re.split(r"[;&|\n]+", text) if _PUSH_RX.search(seg)]
    if not pushes:
        return False
    for seg in pushes:
        try:
            if not is_own(_push_target(shlex.split(seg)), registry, config):
                return False
        except ValueError:
            return False
    return True


def classify(action_text, context, config):
    """-> Result(cls, pattern_id, quote). See the module docstring. Never raises."""
    try:
        return _classify(action_text, context, config)
    except Exception:
        return Result(CANNOT_TELL, "error", "")


def _classify(text, context, config):
    hs = _block(config)
    if hs.get("enabled") is not True:
        return Result(None, None, "")
    if not isinstance(text, str):
        return Result(CANNOT_TELL, "not-text", "")
    registry = context.get("registry") if isinstance(context, dict) else None
    extra = hs.get("extra_patterns")
    failed = False
    own_result = None
    for name, entry in list(_REGISTRY.items()):
        if not _class_on(name, hs):
            continue
        try:
            hit = entry["detector"](text, context)
            if not hit and isinstance(extra, dict) and isinstance(extra.get(name), (list, tuple)):
                for pat in extra[name]:
                    m = re.search(pat, text)
                    if m:
                        hit = ("extra-" + name, m.group(0))
                        break
        except Exception:
            failed = True
            continue
        if not hit:
            continue
        pid, span = hit if isinstance(hit, tuple) else (hit, hit if isinstance(hit, str) else "")
        if name == "rewrite_history" and pid == "rewrite-force-push" and _own_push(text, context, registry, config):
            own_result = Result(None, OWN_PATTERN_ID, _scrub_quote(span))
            rest = dict(_PATTERNS["rewrite_history"])
            m = rest["rewrite-filter"].search(text)
            if not m:
                continue
            pid, span = "rewrite-filter", m.group(0)
        return Result(name, pid, _scrub_quote(_line_of(text, span) if span in text else span))
    if failed:
        return Result(CANNOT_TELL, "detector-error", "")
    if own_result is not None:
        return own_result
    try:
        shlex.split(text)
    except ValueError:
        return Result(CANNOT_TELL, "parse-error", "")
    return Result(None, None, "")
