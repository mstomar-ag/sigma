"""Secret-shaped redaction shared by the scripts/ backlog collectors (mirror + cross-check).

Location-only capture rule: never emit a matched secret VALUE — each match becomes a typed
placeholder, not the value. THIS file is the one pattern set: `hooks/research_capture.py` loads it by
path for web-research excerpts (the same way `hooks/time_track.py` loads its sibling scripts), and
the publish leak gate `tools/leak_scan.py` imports `SHAPE_RULES` below as its own secret rules — so
there is no second copy to keep in sync. Best-effort (pattern-based, not a guarantee) — which is why
anything this feeds (the board mirror, `.sdlc/knowledge/`) is ALSO gitignored (defense in depth)."""
import re
import string

#: `SHAPE_RULES` — every single-regex secret shape the publish leak gate detects, `(name, rx)`, with
#: the gate's contract: the secret VALUE is group 1 when the rx has a group, else the whole match.
#: `tools/leak_scan.py::_scrub_rules` IMPORTS this tuple (and Refuses when it is absent); it is never
#: copied there, and `scrub()` applies it via `_SECRET_PATTERNS` below. The gate keeps its own
#: post-filters (`_is_real_value`: a digit and a letter, not an identifier, not a placeholder) — a
#: gate's false-positive budget differs from a redactor's.
#:
#: Two edits vs. the gate's original text (#2718 D2): `url-password`'s scheme is bounded
#: `[a-z][a-z0-9+.-]{0,31}://` — unbounded it rescanned the hyphen run from every word boundary
#: (measured: 6,802 ms on a 100 KB `the-quick-brown-fox-` slug, 17,306 ms on `a-` x 50,000; bounded,
#: the whole scrub() on the same inputs ~36-45 ms) and the research hook scrubs WHOLE web bodies; and
#: `gitlab-token` is `{20,}` not `{20}`, so a longer token's tail no longer survives a redaction.
#: Exact-length rules (`npm-token` `{36}`, `google-key` `{35}`) redact only the first n characters
#: of a longer run and leave its tail — the same behaviour as the gate, which matches this regex.
#: Order: the specific `sk-` prefixes (anthropic, openai) before the generic `sk-key` so the label
#: names the provider (they cannot overlap: `sk-[A-Za-z0-9]{20,}` stops at the hyphen).
#:
#: GATE-ONLY, deliberately not here -- each is a rule `tools/leak_scan.py` implements and tests:
#: `key-body` (a private key body missing its header or END: line counts, a PRIVATE-header anchor
#: or a DER first line; a redactor would eat legitimate base64 in transcripts); `secret-file`
#: (`_SECRET_FILE`, a path rule, meaningless on text); `opaque-binary` / `oversize` / `unreadable`
#: (a whole-file verdict: the gate names the file and fails); and
#: `config-credential` (`_CONFIG_CRED`, keyed on the file's suffix, which runtime text has no notion
#: of). Concatenated or split token spellings are covered by NEITHER file (out of scope, stated in
#: the gate's docstring).
#: DEFERRED (follow-up issue, filed at #2718's retro): SSH2 `---- BEGIN SSH2 ENCRYPTED PRIVATE KEY
#: ----` (its unterminated form needs its own body walk — SSH2 bodies carry `Comment:` headers the
#: closed header list halts on; half of it would be a silent half-guarantee), PuTTY
#: `PuTTY-User-Key-File-N:` (no END marker; a plain regex would eat prose up to a later
#: `Private-MAC:`), base64-wrapped PEM (`LS0tLS1CRUdJTi...` needs the gate's decode check to tell a
#: wrapped private key from a wrapped public certificate). #2722 residuals: URL passwords are IN
#: (`url-password`); re-encoded secrets and entropy detection are OUT — the gate has no rule for
#: them, so there is nothing to share.
SHAPE_RULES = (
    ("slack-token", re.compile(r"xox[abposr]-[A-Za-z0-9-]{10,}")),
    ("github-pat", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}")),
    ("openai-key", re.compile(r"(?<![\w-])sk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{20,}")),
    ("sk-key", re.compile(r"(?<![\w-])sk-[A-Za-z0-9]{20,}")),
    ("google-key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("stripe-key", re.compile(r"(?<![\w-])[sr]k_live_[0-9a-zA-Z]{16,}")),
    ("npm-token", re.compile(r"(?<![\w-])npm_[A-Za-z0-9]{36}")),
    ("pypi-token", re.compile(r"(?<![\w-])pypi-AgE[A-Za-z0-9_-]{40,}")),
    ("gitlab-token", re.compile(r"(?<![\w-])glpat-[A-Za-z0-9_-]{20,}")),
    ("huggingface-token", re.compile(r"(?<![\w-])hf_[A-Za-z0-9]{20,}")),
    ("slack-webhook", re.compile(r"hooks\.slack\.com/[s]ervices/(T[A-Z0-9]{6,}/B[A-Z0-9]{6,}/[A-Za-z0-9]{16,})")),
    ("url-password", re.compile(r"(?i)\b[a-z][a-z0-9+.-]{0,31}://[^\s/:@'\"]*:([^\s/'\"]{6,})@"
                                 r"(?:[a-z0-9_.-]+\.[a-z]{2,}|\d{1,3}(?:\.\d{1,3}){3}|[a-z0-9_-]+)(?![\w.-])")),
    ("credential-assignment", re.compile(
        r"(?i)(?:api[_-]?key|secret[_-]?key|private[_-]?key|client[_-]?secret|access[_-]?token|"
        r"auth[_-]?token|bot[_-]?token|secret[_-]?access[_-]?key|password|passwd|pwd|secret|token)"
        r"[\"']?\s*[:=]\s*[\"']([^\"'\s]{12,})[\"']")),
)


def _replacement(name, rx):
    """The `re.sub` replacement for a SHAPE_RULES entry: the typed placeholder for a whole-match rule;
    for a grouped rule a callable that rewrites ONLY group 1's span, so the scheme and host of a URL,
    the webhook host path, and an assignment's key and quotes survive around the placeholder."""
    label = "[REDACTED:%s]" % name
    if not rx.groups:
        return label
    return lambda m: m.group(0)[:m.start(1) - m.start(0)] + label + m.group(0)[m.end(1) - m.start(0):]

#: NO \b anchors on the shape tokens — the prefixes (AKIA / gh[pousr]_ / eyJ…) are specific enough to
#: stand alone, and a leading \b would let a secret GLUED to a preceding word char (e.g. "id=AKIA…")
#: slip through. Bare "token" is deliberately absent (it over-redacts ordinary prose like "token
#: budget"); a real `token: <value>` assignment is caught by the key:value rule instead.
_SECRET_PATTERN_SPECS = (
    ("private-key", re.compile(r"-----BEGIN[ A-Z]*PRIVATE KEY(?: BLOCK)?-----.*?-----END[ A-Z]*PRIVATE KEY(?: BLOCK)?-----", re.DOTALL),
     "[REDACTED:private-key]"),
    #: Fallback for a key whose END marker never arrives — truncated at the source, or cut mid-capture.
    #: It has to consume the BODY, not just the header: replacing the header alone published every line
    #: underneath it. Whitespace is `(?:\\[rn]|\s)` because the dominant carriers serialize first —
    #: json.dumps()/str() turn the key's newlines into literal \n ESCAPES, and a real-whitespace pattern
    #: stops dead at the first one. Proc-Type:/DEK-Info: are the RFC1421 encrypted-PEM headers that sit
    #: between header and body; that list is closed and colon-anchored (Subject:/Version:/Comment: are
    #: deliberately absent — they open ordinary prose). The body is a run of long base64 lines, so
    #: `{16,}` stops at short prose and a document merely DISCUSSING the marker keeps its text; the
    #: `{1,15}` tail then takes a mid-line truncation fragment ONLY at end-of-input.
    #:
    #: WHAT THIS DOES AND DOES NOT COVER — the residual is NOT bounded to a fixed number of characters:
    #: consumption walks recognized headers and long base64 runs and STOPS at the first sub-16-char run
    #: or any non-base64 byte inside the body. Everything from that gap onward SURVIVES, however much
    #: of it there is. So a CANONICAL unterminated key — a clean body cut short at the source, which is
    #: the case this fallback exists for — is consumed in full; a MANGLED body (a foreign byte or a
    #: short run partway down) is redacted only as far as the gap, and complete key lines after it are
    #: published. Terminated keys never reach here at all: the DOTALL BEGIN..END pattern above owns
    #: them outright. The same stop rule is why an UNRECOGNIZED header line (e.g. `Comment:`) between
    #: BEGIN and the body halts consumption immediately and leaves the whole body — accepted on
    #: purpose, because the alternative is open-ended header matching that would eat ordinary prose.
    #: `(?: BLOCK)?` admits the PGP armor header (`-----BEGIN PGP PRIVATE KEY BLOCK-----`); its
    #: UNTERMINATED form stops here too, at its `Version:`/`Comment:` armor lines (same accepted
    #: cost, pinned by test) — the terminated form is owned outright by the DOTALL pattern above.
    #: Both costs are pinned by test so the next reader meets the real behavior, not a comforting
    #: bound. Widening the run rule, or opening the header list, is a deliberate decision with its own
    #: false-positive price — not a typo to fix in passing.
    #: No possessive/atomic groups: those are 3.11+, and CI still runs 3.10.
    ("private-key", re.compile(r"-----BEGIN[ A-Z]*PRIVATE KEY(?: BLOCK)?-----"
                r"(?:(?:\\[rn]|\s)*(?:Proc-Type|DEK-Info):[^\n]{0,120})*"
                r"(?:(?:\\[rn]|\s)*[A-Za-z0-9+/=]{16,})*"
                r"(?:(?:\\[rn]|\s)*[A-Za-z0-9+/=]{1,15}(?=(?:\\[rn]|\s)*$))?"),
     "[REDACTED:private-key]"),
    ("aws-key", re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}"), "[REDACTED:aws-key]"),
    ("gh-token", re.compile(r"gh[pousr]_[0-9A-Za-z]{20,}"), "[REDACTED:gh-token]"),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[REDACTED:jwt]"),
    ("auth", re.compile(r"(?i)\b(?:bearer|basic|digest)\s+[A-Za-z0-9+/=._\-]{8,}"), "[REDACTED:auth]"),
    #: The two #629 redactor-only rules run BEFORE the gate's shapes: the shape rule `credential-assignment`
    #: (quoted, `{12,}`) stops at an escaped quote and would leave a tail the suffix rule could then no
    #: longer repair, so the value must be consumed whole first.
    #: An HTTP-style `Authorization` header: the scheme word is open-ended (NTLM, Negotiate, AWS4-...), so
    #: everything after the separator up to the end of the line is the credential. Over-redacts a prose
    #: line that merely starts with the header name; accepted, fail toward redaction.
    ("authorization-header", re.compile(r"(?i)(authorization[\"']?[ \t]*(?::=|=>|[:=])[ \t]*)[^\n]+"),
     r"\1[REDACTED]"),
    #: Keyed on the key-name SUFFIX with no `\b`: `GITHUB_TOKEN=` / `DB_PASSWORD=` have no word boundary
    #: before `TOKEN`/`PASSWORD` (`_` is a word character), so an anchored pattern let every env-style
    #: name through (#629). The prefix is left in the text, so the key name survives the redaction.
    #: Separators `=`, `:`, `:=`, `=>`. A quoted value runs to its closing quote (escape-aware) or,
    #: unterminated, to end of line, so spaces inside it leave no tail; anything else (including a
    #: serialised-twice `\"value\"`, values holding `&`, `<`, `>` and a stray quote) is a run of
    #: non-space characters (min 4). Linear: no leading character class, no nested quantifier. A name
    #: with a trailing suffix (`token_file=`, `max_tokens=`) is deliberately NOT matched, so counters
    #: and paths survive.
    ("credential-assignment-suffix", re.compile(r"(?i)(api[_-]?key|secret[_-]?key(?:[_-]?base)?|private[_-]?key|client[_-]?secret|"
                r"access[_-]?(?:token|key)|auth|credentials?|token|secret|password|passwd|pwd|"
                r"passphrase)(?:\\*[\"'])?\]?\s*(?::=|=>|[:=])\s*"
                r"(?:(?:token|api[_-]?key|bearer|basic|digest)[ \t]+)?"
                r"(?:\"{3}[^\n]+|'{3}[^\n]+|\"(?:\\.|[^\"\\\n])+\"?|'(?:\\.|[^'\\\n])+'?|\S{4,})"),
     r"\1: [REDACTED]"),
    #: The gate's shapes, before the generic key:value rule so each keeps its provider label.
    *((name, rx, _replacement(name, rx)) for name, rx in SHAPE_RULES),
    #: The original anchored rule stays, UNCHANGED, as the commit gate's rule: the gate shares this table
    #: (`COMMIT_SHAPE_RULES`) and a gate's false-positive budget differs from a redactor's -- the wider
    #: rules above roughly double the tracked-tree hits on ordinary code (a variable assigned from a call).
    ("credential-assignment", re.compile(r"(?i)\b(api[_-]?key|secret[_-]?key|private[_-]?key|client[_-]?secret|"
                r"access[_-]?token|authorization|token|secret|password|passwd|pwd)\b[\"']?\s*[:=]\s*"
                r"[\"']?[^\s\"'<>&]{4,}"),
     r"\1: [REDACTED]"),
)

# Public only to the commit boundary: rule and regular expression, never a matching span/value.
# `scrub()` below still owns the text-redaction behaviour.
# Redactor-only (#629): too wide for a commit gate, which would refuse ordinary code such as a line that
# builds a header or assigns a variable from a call. The gate keeps the original anchored rules.
_REDACTOR_ONLY = frozenset({"authorization-header", "credential-assignment-suffix"})
COMMIT_SHAPE_RULES = tuple((name, rx) for name, rx, _replacement_ in _SECRET_PATTERN_SPECS
                           if name not in _REDACTOR_ONLY)
_SECRET_PATTERNS = tuple((rx, replacement) for _name, rx, replacement in _SECRET_PATTERN_SPECS)

# These are public synthetic values deliberately used in Sigma's own redaction tests.  The list is
# exact rather than prefix/path based: it cannot become a general bypass for a real credential.
COMMIT_FIXTURE_VALUES = frozenset({"AKIAIOSFODNN7EXAMPLE"})  # leak-scan: allow aws-key public test fixture


#: Gate-only exact values the anchored `credential-assignment` rule may carry without refusing: Sigma's
#: OWN redaction markers -- `[REDACTED]`, what `scrub()` writes for that rule, and `[REDACTED:<name>]` for
#: every rule in the table -- so a scrubbed transcript or fixture line can be committed. Exact and
#: unquoted (trailing closers only): no structural `${...}` / `{{...}}` / `<...>` form. DELIBERATELY
#: narrower than `tools/leak_scan.py`'s `_PLACEHOLDER_VALUE`, which this file cannot import: that is a
#: publish gate, and a gate's false-positive budget differs from a redactor's and from each other's.
COMMIT_PLACEHOLDER_VALUES = frozenset({"[REDACTED]"} | {"[REDACTED:%s]" % name
                                                        for name, _rx, _repl in _SECRET_PATTERN_SPECS})

#: The CLOSED set of suffixes whose files get the expression and type exemptions below (#961). Every
#: other file -- config, data, docs, an unknown or missing suffix, a case variant such as `a.PY` -- reads
#: an unquoted value as a literal, so it keeps every hit but a boolean/null word or a marker. Closed so
#: that a file nobody listed fails closed; widening it is a deliberate one-line change a test pins.
COMMIT_CODE_SUFFIXES = frozenset((".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts",
                                  ".cts", ".vue", ".svelte", ".java", ".kt", ".kts", ".scala", ".groovy",
                                  ".gradle", ".go", ".rs", ".rb", ".php", ".cs", ".swift", ".dart", ".c",
                                  ".h", ".cc", ".cpp", ".hpp", ".m", ".mm", ".lua", ".ex", ".exs"))


def commit_code_path(path):
    """True only when the basename has a stem and its last suffix, exactly as git stores it (so
    case-sensitive), is in `COMMIT_CODE_SUFFIXES`. `None` or an empty path is not a code path."""
    if not path:
        return False
    stem, dot, suffix = str(path).rsplit("/", 1)[-1].rpartition(".")
    return bool(dot and stem) and "." + suffix in COMMIT_CODE_SUFFIXES


# The gate-only value filter for the anchored `credential-assignment` rule (#961). That rule takes ANY
# 4+ character run after a credential-named key, so a call, an env read, a type annotation and a
# boolean were refused as credential values and green goals parked. The rule stays byte-identical
# (`scrub()` and both scanners share it); this judges only the gate's own matches of it, in order, and
# every check fails CLOSED -- an unrecognised shape keeps its hit:
#   1. a quoted value directly after the separator always hits (read from the LINE: the rule's own
#      optional quote hides it);
#   2. a secret-like token anywhere after the separator keeps the hit (`_last_secret_like_end`);
#   3. an assigning `=` after the separator whose right side starts a quoted literal or wraps keeps the
#      hit (`_last_assigned_literal_start`): an annotated, chained or keyword-argument default;
#   4. only then may the WHOLE value run be exempt: a boolean/null word or a marker in every file, an
#      expression or (after `:`) a closed-set type in a code file only. Numbers and bare identifiers
#      never are;
#   5. an exempt value still hits when its statement continues onto the next line (`_continues`): the
#      literal of a wrapped call, fallback or collection sits on a line with no key, which no rule sees.
# Linear by construction: each whole-value pattern is a fullmatch whose adjacent runs share no
# character, so a failed match cannot be re-split, and the three line scans run once per line, lazily.
_ASSIGNMENT_SEPARATOR = re.compile(r"[\"']?\s*([:=])\s*([\"']?)")
_CLOSERS = r"[,;:)}\]]*"
_BOOLEAN_OR_NULL = re.compile(r"(?:True|False|true|false|None|null|nil|undefined)" + _CLOSERS)
_PLACEHOLDER = re.compile("(?:%s)" % "|".join(re.escape(v) for v in sorted(COMMIT_PLACEHOLDER_VALUES))
                          + _CLOSERS)
#: An identifier head that starts a call, a subscript or an attribute read, then expression characters
#: only, then an optional `}` tail. Every closer but `}` is already a body character, so the closers are
#: folded in: a separate closer run after the body let a failed match try every split of a `)` run
#: (6.9 s measured on one 32 KB line, against 1.3 ms folded).
_EXPRESSION = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:[(\[]|\.[A-Za-z_])[A-Za-z0-9_.()\[\],;:?!=*+-]*"
                         r"(?:\}[,;:)}\]]*)?")
#: Exact names, never "a capitalised word": a YAML value such as `Summer` must stay refused.
_TYPE_NAME = r"(?:str|bytes|bool|int|float|Any|SecretStr|string|boolean|number|any|unknown|String)"
_TYPE = re.compile(_TYPE_NAME + r"(?:\|(?:" + _TYPE_NAME + r"|None|null|undefined))*" + _CLOSERS)
#: An assigning `=` -- not part of `==`, `!=`, `<=`, `>=` or `=>` -- whose right side, after spaces and
#: opening parentheses, STARTS a quoted literal of any length (optional b/r/u/f prefix, so a triple
#: quote too) or wraps (the end of the line, or a lone continuation backslash). A carriage return ends
#: the line too: rows split on `\n` alone, so a CRLF file's row keeps its `\r`, and `$` never matches
#: before one (a wrapped CRLF line passed, #961 review). A lookahead, not `\s*$`: that rescans a space
#: run from every position `[\s(]*` gives back, quadratic (0.8 s measured on one 16 KB run).
_ASSIGNED_LITERAL = re.compile(r"(?<![=!<>])=(?![=>])[\s(]*(?:\\?(?=\r|$)|[bBrRuUfF]{0,2}[\"'`])")
_WORD_SEGMENT = re.compile(r"[A-Za-z0-9_]+")
#: A quoted literal, escape-aware, paired left to right from the start of the line. An unterminated one
#: runs to the end of the line (fail closed), so an attempt at a quote never fails and never rescans.
_QUOTED_LITERAL = re.compile(r'"((?:[^"\\]|\\.)*)(?:"|\\?$)|\'((?:[^\'\\]|\\.)*)(?:\'|\\?$)'
                             r'|`((?:[^`\\]|\\.)*)(?:`|\\?$)', re.DOTALL)
#: Hyphenated provider prefixes (`sk-`, `glpat-`, `xoxb-`) split into segments the other clauses judge;
#: `hf_`/`npm_` are absent because `hf_hub_download(` is ordinary code and their real tokens are long
#: random runs the 24-run clause refuses.
_SECRET_PREFIXES = ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_", "sk_live_", "sk_test_",
                    "rk_live_", "rk_test_", "AKIA", "ASIA", "AIza", "eyJ", "xox")
_ALNUM_RUN = re.compile(r"[A-Za-z0-9]{24,}")
_UPPER_SNAKE = re.compile(r"[A-Z][A-Z0-9_]*")
#: Each ASCII letter -> "a" and each ASCII digit -> "0": a token's letter/digit shape.
_SHAPE = str.maketrans(string.ascii_letters + string.digits, "a" * 52 + "0" * 10)
_HIT, _CODE_ONLY, _EXEMPT = "hit", "code-only", "exempt"


def _secret_like(token):
    """True when one token looks like a credential value: a known provider prefix; 3+ letter/digit
    alternations between adjacent characters (a random run alternates constantly); a run of 24+ ASCII
    letters and digits (an underscore breaks it, so a long snake_case name passes); or 6+ characters
    holding a letter and a digit, unless the whole token is an upper-snake name (`HS256`, an env name)."""
    if token.startswith(_SECRET_PREFIXES):
        return True
    shape = token.translate(_SHAPE)
    if shape.count("a0") + shape.count("0a") >= 3:       # neither pair can overlap itself: exact count
        return True
    if len(token) >= 24 and _ALNUM_RUN.search(token):
        return True
    return len(token) >= 6 and "a" in shape and "0" in shape and not _UPPER_SNAKE.fullmatch(token)


def _last_secret_like_end(text):
    """The largest end offset of a secret-like token on the line, or -1, in ONE pass: every
    `[A-Za-z0-9_]+` segment, then every whitespace-separated word of every quoted literal. A word in a
    literal counts at the literal's END, so a literal that straddles a separator counts as after it."""
    last = -1
    for match in _WORD_SEGMENT.finditer(text):
        if _secret_like(match.group()):
            last = match.end()
    for match in _QUOTED_LITERAL.finditer(text):
        if match.end() > last:
            body = match.group(1) or match.group(2) or match.group(3) or ""
            if any(_secret_like(word) for word in body.split()):
                last = match.end()
    return last


def _last_assigned_literal_start(text):
    """The largest start offset of an assigning `=` that assigns a quoted or wrapped literal, or -1.
    One `finditer`: an attempt fails at once anywhere but an `=`, and an `=`'s `[\\s(]*` run stops at
    the next `=`, so no two attempts rescan the same characters."""
    last = -1
    for match in _ASSIGNED_LITERAL.finditer(text):
        last = match.start()
    return last


#: One token of the continuation scan: a quoted literal paired as `_QUOTED_LITERAL` pairs it, its group
#: the closing quote (empty or a lone backslash when it runs to the end of the line unterminated); a
#: comment start (`//`, `/*`, or `#` that opens the line or follows whitespace, so `this.#x` is code);
#: a bracket; or a `?`. Every attempt fails at once on any other character, so one `finditer` is linear.
_LINE_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*("|\\?$)|\'(?:[^\'\\]|\\.)*(\'|\\?$)|`(?:[^`\\]|\\.)*(`|\\?$)'
                         r"|//|/\*|(?:^|(?<=\s))#|[(\[{)\]}?]", re.DOTALL)
_OPENER_OF = {")": "(", "]": "[", "}": "{"}
#: The operators that, ending a statement's code, carry the next line's operand into the value: a
#: backslash continues the line; a trailing `=` assigns what follows (an annotated, chained or keyword
#: default); `??`, `||`, `or` and `?:` return their right operand when the left is empty, and `&&` and
#: `and` when it is set; `?`, `:` and `if`/`else` start a conditional branch; `+` concatenates; `do`,
#: with or without `|params|`, opens a Ruby or Elixir block whose body is a default. A `:` counts only
#: after a `?` (a ternary or `?:`): alone it ends a Python block opener or a label. Never `,`: the next
#: line is then another argument or element, and an argument that holds the value leaves its own
#: bracket open. Never `.`: it ends a docstring sentence (`- <key>: Optional[str].`, measured in
#: site-packages) far more often than it continues a member chain, which formatters start on the next
#: line. Never `!` (a postfix operator that ends a Swift, Kotlin or TypeScript statement), a lone `|` (a
#: Markdown table's border) or an arithmetic, comparison or bitwise operator, whose result is computed,
#: not the next line's literal. Every alternative is at most 52 characters, so a search that starts 64
#: characters before the end is O(1).
_TRAILING_OPERATOR = re.compile(r"(?:\\|=|\?|:|\+|\|\||&&"
                                r"|(?<![\w.$])(?:or|and|if|else|do(?:[ \t]{0,8}\|[^|]{0,40}\|)?))\Z")


def _continuation_scan(text):
    """`(last_open, open_quote, last_question, trailing)` for one line's code, in ONE pass: the offset
    of the last opener no later closer of its kind closes, the start of a quoted literal left open at
    the end, the last `?` outside literals, and the start of a `_TRAILING_OPERATOR` at the code's end
    (-1 for each when absent). The code stops at a comment's start; trailing spaces and a CRLF row's
    `\\r` are dropped. A closer of another kind is skipped, so a mismatch leaves the opener open."""
    openers, open_quote, last_question, code_end = [], -1, -1, len(text)
    for match in _LINE_TOKEN.finditer(text):
        lexeme = match.group()
        if lexeme[0] in "\"'`":
            if (match.group(1) or match.group(2) or match.group(3) or "\\") == "\\":
                open_quote = match.start()
        elif lexeme[0] in "/#":
            code_end = match.start()
            break
        elif lexeme == "?":
            last_question = match.start()
        elif lexeme in "([{":
            openers.append((lexeme, match.start()))
        elif openers and openers[-1][0] == _OPENER_OF[lexeme]:
            openers.pop()
    code = text[:code_end].rstrip()
    trailing = _TRAILING_OPERATOR.search(code, max(0, len(code) - 64))
    return (openers[-1][1] if openers else -1, open_quote, last_question,
            trailing.start() if trailing else -1)


def _continues(text, start, line_scan):
    """True when the statement whose value starts at `start` continues onto the next line: an opener
    after `start` is left open, a quoted literal opened after it runs past the end, or its code ends in
    a continuation operator (a `:` only after a `?` that follows `start`). Every offset lies before the
    code's end, so a match inside a comment that starts before `start` continues nothing."""
    if "continuation" not in line_scan:
        line_scan["continuation"] = _continuation_scan(text)
    last_open, open_quote, last_question, trailing = line_scan["continuation"]
    if last_open >= start or open_quote >= start:
        return True
    return trailing >= start and (text[trailing] != ":" or last_question >= start)


def _anchored_verdict(text, match, line_scan):
    """`_HIT`, `_CODE_ONLY` (cleared in a code file only) or `_EXEMPT` (cleared in every file) for one
    match of the anchored rule. `line_scan` caches the three per-line scans, so a line with k anchored
    matches costs one pass of each plus O(k), not k passes. An exempt value whose statement continues
    onto the next line keeps its hit: the gate judges one line, and the next one has no key."""
    sep = _ASSIGNMENT_SEPARATOR.match(text, match.end(1))
    if not sep or sep.group(2) or sep.end() >= match.end():
        return _HIT                     # quoted, or a match this cannot parse: refuse
    if "secret" not in line_scan:
        line_scan["secret"] = _last_secret_like_end(text)
    if line_scan["secret"] > sep.end():
        return _HIT
    if "assigned" not in line_scan:
        line_scan["assigned"] = _last_assigned_literal_start(text)
    if line_scan["assigned"] >= sep.end():
        return _HIT
    value = text[sep.end():match.end()]
    if _BOOLEAN_OR_NULL.fullmatch(value) or _PLACEHOLDER.fullmatch(value):
        verdict = _EXEMPT
    elif _EXPRESSION.fullmatch(value) or (sep.group(1) == ":" and _TYPE.fullmatch(value)):
        verdict = _CODE_ONLY
    else:
        return _HIT
    return _HIT if _continues(text, sep.end(), line_scan) else verdict


def commit_secret_findings(text):
    """Value-free `(rule, column, code_exempt)` findings for one added source line -- the commit gate's
    own view, which knows no path. `code_exempt` marks an anchored `credential-assignment` hit that
    only a CODE-file exemption clears; the caller, which knows the path, drops it for a code file
    (`commit_secret_hits` below, or work.py per staged path).

    Matches in the exact synthetic fixture list are excluded so the repository can maintain its
    redaction controls. An identical `(rule, column)` is reported once (both same-named rules hit a
    quoted literal at its key, which printed one location twice), keeping the stricter finding."""
    findings, index, line_scan = [], {}, {}
    for name, rx in COMMIT_SHAPE_RULES:
        for match in rx.finditer(text):
            # Assignment rules capture the key name before the credential value; checking every
            # captured span makes the exemption apply to the documented value, never its context.
            if any(re.search(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(value),
                             match.group(0)) for value in COMMIT_FIXTURE_VALUES):
                continue
            code_exempt = False
            # The anchored rule is the one whose group 1 (the key) starts the match; the quoted
            # SHAPE_RULES instance captures the VALUE in group 1, so it is never filtered here.
            if name == "credential-assignment" and rx.groups and match.start(1) == match.start():
                verdict = _anchored_verdict(text, match, line_scan)
                if verdict == _EXEMPT:
                    continue
                code_exempt = verdict == _CODE_ONLY
            key = (name, match.start() + 1)
            if key in index:
                if not code_exempt:
                    findings[index[key]] = (name, key[1], False)
                continue
            index[key] = len(findings)
            findings.append((name, key[1], code_exempt))
    return findings


def commit_secret_hits(text, path=None):
    """Return value-free `(rule, column)` hits for one added source line.

    The commit gate must say why and where it refused without echoing a credential. `path` is git's raw
    repo-relative path: a code file's expression and type values pass. A missing path is not a code
    path, so a caller that cannot say which file the line came from gets only the boolean/null and
    marker exemptions -- fail closed."""
    code = commit_code_path(path)
    return [(rule, column) for rule, column, code_exempt in commit_secret_findings(text)
            if not (code_exempt and code)]


def scrub(text):
    """Redact secret-shaped substrings, never emitting the matched value. Returns text unchanged when
    empty/None-ish (callers pass strings)."""
    if not text:
        return text
    for pat, repl in _SECRET_PATTERNS:
        text = pat.sub(repl, text)
    return text


#: The leak scan's secret set only: the labelled specific shapes (`_SECRET_PATTERNS` names below) plus
#: `SHAPE_RULES`. Excludes the redactor-only rules (`auth`, generic key:value, authorization header),
#: which rewrite ordinary prose (plain words).
_SHAPE_SPEC_NAMES = frozenset({"private-key", "aws-key", "gh-token", "jwt"})
_SHAPE_SUBS = tuple((rx, repl) for name, rx, repl in _SECRET_PATTERN_SPECS if name in _SHAPE_SPEC_NAMES) \
    + tuple((rx, _replacement(name, rx)) for name, rx in SHAPE_RULES)


def scrub_shapes(text):
    """Like `scrub`, but applies ONLY the leak scan's SHAPE_RULES -- none of the redactor-only rules
    (`auth`, key:value) that rewrite ordinary prose. Ordinary text comes back byte-identical."""
    if not text:
        return text
    for rx, repl in _SHAPE_SUBS:
        text = rx.sub(repl, text)
    return text
