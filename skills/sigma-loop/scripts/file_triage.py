"""File triage core (rulebook, slice 14, #999). Pure library, wired into filing and the migration.

`decide(title, body, flags, config) -> Decision(kind, priority, reason, matched, qkind)` for one AI-filed issue.
kind is `arm` (ready for the loop at `priority`), `park` (an ordinary park with `reason`), or `queue` (triage is
switched off: the old behaviour, no priority). It is total: any error parks. Priority is P0-P4 or None, never
a blocking value. The decision reads nothing about who filed it (login-free): `flags` is only inspected for
`human_confirmed`, `priority`, `bucket` and `park` (a declared question kind, #1703).

Order: triage off -> queue; invalid buckets -> park; deny-list -> park (it can only park, even for a
human-confirmed filing); a declared `park` kind or a question title (`triage.question_patterns`, TITLE only, `[]`
disables) -> park with that `qkind`; human-confirmed -> arm at its own plan priority; else a bucket -> arm at that bucket's
priority. Deny-list: the hard-stop classifier (default classes forced on here), configurable words, spend phrases (`triage.spend_phrases`, [] disables), secret
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

#: `matched` (#1498): the deny-list class that parked the filing (`hard-stop word`, `spend phrase`, `secret`,
#: `hard-stop class`, `permission marker`), else None. Callers declare a hard-stop kind from it.
#: `qkind` (#1703): the declared question kind of a park for a person (declared path or title detector), else None.
Decision = collections.namedtuple("Decision", "kind priority reason matched qkind", defaults=(None, None))

DEFAULT_BUCKETS = {"urgent": "P2", "routine": "P3", "unclear": "P4"}
DEFAULT_HUMAN_PRIORITY = "P1"
DEFAULT_DENY_WORDS = ("irreversible", "force push", "production data", "drop table", "delete all",
                      "private key", "credentials")
DEFAULT_SPEND_PHRASES = ("paid run", "rent a GPU", "new vendor", "paid API", "cloud GPU benchmark")
_PRIO = re.compile(r"P[0-4]")
_FENCE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_NUMWORD = (r"(?:one|two|three|four|five|six|seven|eight|nine|ten|twenty|thirty|forty|fifty|sixty|seventy|eighty"
            r"|ninety|hundred|thousand)")
#: One optional nationality between a number and "dollars" ("40 US dollars"); only dollars take it.
_DOLLAR_ADJ = r"(?:(?:US|U\.S\.|Canadian|Australian|New\s+Zealand|Hong\s+Kong|Singapore)\s+)?"
_AMOUNT = (r"(?:(?:\$|US\$|USD\s?|€|£|¥|EUR\s?|GBP\s?|CHF\s?|CAD\s?|INR\s?)\s?\d[\d,]*(?:\.\d+)?\s?[kK]?\b"
           r"|\b\d[\d,]*(?:\.\d+)?\s*(?:" + _DOLLAR_ADJ + r"dollars?|USD|bucks|euros?|EUR|GBP|CHF|CAD|INR|AUD|NZD|JPY|CNY|RMB|SEK|NOK|DKK|SGD|HKD|KRW|MXN|BRL|ZAR|PLN|AED|yen|quid|pounds?(?:\s+sterling)?)\b"
           r"|\b\d[\d,]*(?:\.\d+)?\s?[kK]?\s?[€£¥$]"
           r"|\b" + _NUMWORD + r"(?:[- ]" + _NUMWORD + r")*\s+(?:" + _DOLLAR_ADJ + r"dollars?|bucks|euros?|yen|quid|pounds?(?:\s+sterling)?)\b)")
_COMPUTE = r"\b(?:GPUs?|instances?|clusters?|credits|compute|pods?|cloud|runs?|nodes?)\b"
_AMOUNT_FOR_COMPUTE = re.compile(_AMOUNT + r"[^.\n]{0,60}" + _COMPUTE + "|" + _COMPUTE + r"[^.\n]{0,60}" + _AMOUNT,
                                 re.IGNORECASE)
_SECRET = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:token|secret|password|api[_-]?key)\s*[:=]\s*\S{8,}",
                     re.IGNORECASE)
_URGENT = re.compile(r"\b(unsafe|vulnerab\w*|exploit\w*|data corrupt\w*|deadlock\w*|every run|silently wrong)\b",
                     re.IGNORECASE)
_ROUTINE = re.compile(r"\b(tech.?debt|cleanup|edge case|polish|follow.?up|cosmetic|nice.to.have)\b", re.IGNORECASE)


def _park(reason, matched=None, qkind=None):
    return Decision("park", None, reason, matched, qkind)


#: Default question titles (#1703): "Question", "Decide", "Should" at the start, or a trailing "?". Title only.
DEFAULT_QUESTION_PATTERNS = (r"^\s*question\b", r"^\s*decide\b", r"^\s*should\b", r"\?\s*$")


def _is_question(title, tri):
    pats = tri.get("question_patterns")
    if not (isinstance(pats, (list, tuple)) and all(isinstance(p, str) for p in pats)):
        pats = DEFAULT_QUESTION_PATTERNS
    try:
        return any(re.search(p, title, re.IGNORECASE) for p in pats)
    except re.error:
        return any(re.search(p, title, re.IGNORECASE) for p in DEFAULT_QUESTION_PATTERNS)


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


def is_off(config):
    """True exactly when triage is switched off (`ai_filed.triage.enabled` is the JSON false), the one
    state in which a writer keeps its pre-triage behaviour. Never raises; a broken config reads as on."""
    return decide("", "", {}, config).kind == "queue"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


#: A deny word counts only in action position (#1519, structural rule, fail closed). The text is split into
#: clauses; a clause holding the word is descriptive only when its deny word sits in backticks or quotes, follows
#: a noun-phrase marker ("detector for"), the clause is passive ("are read by"), or it opens with a protective
#: verb ("redact") or a determiner with no action verb before the word. A clause opening with an action verb, or
#: with anything unrecognised, holds.
_CLAUSES = re.compile(r"(?<=[.!?;,:])\s+|\n+|\s+(?:and then|then|and|or|but|&|after that|so that|so|by|via|using|-+)\s+",
                      re.IGNORECASE)
_QUOTED = re.compile(r"`[^`\n]*`|\"[^\"\n]*\"|(?<!\w)'[^'\n]*'(?!\w)")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_LEAD = re.compile(r"^(?:please\s+)?(?:(?:we|i|you)\s+(?:should|will|must|can|could|need to|have to)\s+)?",
                   re.IGNORECASE)
_ACTION = frozenset((
    "delete drop remove wipe purge erase destroy truncate overwrite rotate revoke reset force push run deploy "
    "publish release migrate rename rebase merge send print dump leak expose share commit upload copy use spend "
    "rent buy pay disable enable execute apply perform do make take start launch kill terminate scrub "
    "deleting dropping removing wiping purging erasing destroying truncating overwriting rotating revoking "
    "resetting forcing pushing running deploying publishing releasing migrating sending printing dumping "
    "exposing sharing committing uploading copying using spending executing applying performing").split())
_PROTECT = frozenset(("redact redacts mask masks detect detects document documents describe describes gate "
                      "gates prevent prevents refuse refuses warn warns guard guards protect protects classify "
                      "classifies flag flags report reports list lists explain explains mention mentions note "
                      "log logs test tests check checks verify verifies assert asserts").split())
_HANDOFF = frozenset("to we you i when before while if until once after".split())
_COPULA = frozenset("is are be was were being should must will can could".split())
_DETERMINER = frozenset(("the a an this that these those it its they their which what when where if how why all "
                         "any each every some no one there").split())
_NP_HEAD = re.compile(r"\b(?:detector|detection|detecting|redaction|redacting|mentions?|rule|rules|check|checks|"
                      r"handling|scan|scanner|filter|docs?|documentation|tests?|gate|warning|guard)\s+"
                      r"(?:for|of|about)\s+(?:\w+\s+){0,2}$", re.IGNORECASE)
_PASSIVE = re.compile(r"\b(?:are|is) (?:read|listed|reported|detected|logged|redacted|masked|documented)\b",
                      re.IGNORECASE)
_WORD = re.compile(r"[a-z']+")


_NAMING = frozenset("make call name label term mention list rename".split())
_FILLER = frozenset("the a an this that these those just please now also then".split())
_NOUN_HEAD = frozenset(("command commands word words phrase phrases term terms rule rules flag flags string strings "
                        "text name names keyword keywords token tokens verb verbs").split())
_BE = frozenset("is are was were be being".split())
_NEGATION = frozenset("not never no don't dont without doesn't isn't aren't".split())
_DESCRIBING = frozenset("for of about first second third again other another same called named".split())
_PRONOUN = frozenset("it all any some one".split())
_MODAL = frozenset("should must will can could next now let's i'll we'll you'll ok okay afterwards afterward".split())


def _quoted_holds(clause, at, span_at):
    """True when a deny word inside a quoted span follows an action verb or hand-off word (#1704)."""
    before = _WORD.findall(clause[:span_at])
    if not before:
        return not (_PASSIVE.search(clause) or any(w in _COPULA for w in _WORD.findall(clause[at:])))
    opener = before[0]
    while before and before[-1] in _FILLER:
        before.pop()
    if not before:
        return opener in _FILLER and opener not in _DETERMINER     # "Then/Just/Please `x`" holds; "The `x` guard" does not
    last = before[-1]
    if last in _NAMING:
        return False
    if last in _ACTION or last in _HANDOFF or last in _MODAL:     # before the determiners: "when" and "if" are both
        return True
    if last in _PRONOUN and (len(before) == 1 or before[-2] in _ACTION | _HANDOFF | _MODAL | _FILLER):
        return not (_PASSIVE.search(clause) or any(w in _COPULA for w in _WORD.findall(clause[at:])))   # "Do it `x`", "All `x` main" (#1785)
    if last in _NOUN_HEAD or last in _NEGATION or last in _DETERMINER or last in _BE:
        return False
    return last not in _DESCRIBING     # an unrecognised word before the span holds (#1755)


def _descriptive(clause, at, deny):
    """True when the deny word at `at` in this clause is described rather than done."""
    prefix = clause[:at]
    before = _WORD.findall(prefix.lower())
    if any(w in _ACTION for w in before) or (before and before[-1] in _HANDOFF):
        return False
    if "to" in before:  # a purpose clause ("redact X to nuke Y") is action unless it names a protective verb
        nxt = before[len(before) - before[::-1].index("to"):][:1]
        if nxt and nxt[0] not in _PROTECT and nxt[0] not in _DETERMINER:
            return False
    if _NP_HEAD.search(prefix) or _PASSIVE.search(clause):
        return True
    words = _WORD.findall(_LEAD.sub("", _BULLET.sub("", clause)).lower())
    if not words:
        return False
    first = words[0]
    if first in _ACTION:
        return False
    if first in _PROTECT:
        return True
    if first in _DETERMINER:
        return not any(w in _ACTION or w in _COPULA for w in _WORD.findall(clause[:at + len(deny)].lower()))
    return False


def _quoted_deny(clause, w):
    """True when a quoted span holds the deny word in action position (#1704)."""
    low = clause.lower()
    for m in _QUOTED.finditer(clause):
        at = low.find(w, m.start(), m.end())
        if at >= 0 and _quoted_holds(low, at, m.start()):
            return True
    return False


def _flat(s):
    """Hyphen and underscore read as a space, one for one (#1734)."""
    return s.replace("-", " ").replace("_", " ")


def _action_sentence(text, word):
    """-> the (trimmed) first clause holding `word` in action position, else None."""
    w = _flat(word.lower())
    if not w:
        return None
    for clause in _CLAUSES.split(text):
        shown = clause
        clause = _flat(clause)  # same length, so the reported text below is the original clause
        if _quoted_deny(clause, w):
            return " ".join(shown.split())[:100]
        low = _QUOTED.sub(" ", clause).lower()
        at = low.find(w)
        if at < 0:
            continue
        if _descriptive(low, at, w):
            continue
        return " ".join(shown.split())[:100]
    return None


def _deny(title, body, blk):
    """-> (reason, class) when the deny-list matches, else None. Raises on a broken helper (caller parks)."""
    status = _load("spend_approval").parse_marker(body)[0]
    if status != "none":
        return "first body line carries a recorded-permission marker", "permission marker"
    text = "%s\n%s" % (title, body)
    words = blk.get("triage", {}).get("deny_words") if isinstance(blk.get("triage"), dict) else None
    if not (isinstance(words, (list, tuple)) and all(isinstance(w, str) for w in words)):
        words = DEFAULT_DENY_WORDS
    for w in words:
        hit = _action_sentence(text, w)
        if hit:
            return "hard-stop word: %s (in: %s)" % (w, hit), "hard-stop word"
    tri = blk.get("triage") if isinstance(blk.get("triage"), dict) else {}
    phrases = tri.get("spend_phrases")
    if not (isinstance(phrases, (list, tuple)) and all(isinstance(w, str) for w in phrases)):
        phrases = DEFAULT_SPEND_PHRASES
    for ph in phrases:
        if ph.strip() and re.search(r"\b" + r"\s+".join(map(re.escape, ph.split())) + r"\b", text, re.IGNORECASE):
            return "spend phrase: " + ph, "spend phrase"
    if phrases and _AMOUNT_FOR_COMPUTE.search(_FENCE.sub(" ", text)):
        return "spend phrase: money amount for compute", "spend phrase"
    if _SECRET.search(text):
        return "secret-shaped text", "secret"
    res = _load("hard_stop").classify(text, {}, {"hard_stops": {"enabled": True}})
    # A shell parse failure on free prose (an odd apostrophe) is not a stop: the text is not a command.
    # Every other cannot-tell (detector error, not text) and every matched class still parks.
    if res.cls == "cannot-tell" and res.pattern_id == "parse-error":
        return None
    if res.cls is not None:
        return "hard-stop class: " + str(res.cls), "hard-stop class"
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
        return _park(why[0], why[1])
    flags = flags if isinstance(flags, dict) else {}
    declared = flags.get("park")
    if isinstance(declared, str) and declared != "unknown" and declared in _load("qkind").QKINDS:
        return _park("declared question", None, declared)
    if _is_question(title, tri):
        return _park("question for a person", None, "needs_decision")
    if flags.get("human_confirmed") is True:
        pri = flags.get("priority")
        if isinstance(pri, str) and _PRIO.fullmatch(pri):
            return Decision("arm", pri, "human-confirmed filing keeps its plan priority")
        return _park("human-confirmed filing has no valid plan priority")
    name = _bucket(title, body, flags)
    if name not in v["buckets"]:
        return _park("unknown bucket: %s" % name)
    return Decision("arm", v["buckets"][name], "bucket " + name)
