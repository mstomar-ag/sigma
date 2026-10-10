#!/usr/bin/env python3
"""Safety check core: a pinned shape table, two-way against the live redaction specs (#1049, slice 1
of #1048, story #806).

The documented gesture, run from the repository root:

    python3 evals/safety/check.py

Exit 0: the live spec list and the table below agree both ways, SHAPE_RULES is a subset of the
live list, every generator produces a value its own live regex matches, and every commit-gate
outcome is the pinned one. Exit 1: one line per finding,
`check.py: FINDING <kind> <shape> <hash>: <detail>`, naming the shape and the first ten hex
characters of its pattern hash, never a value. Exit 2: `check.py: REFUSED: <reason>` on stderr and
nothing on stdout, when scrub.py cannot be loaded, an attribute the check reads is absent or of the
wrong type, probing it raises, or this table is malformed. A refusal is never a pass.

What it adds beyond per-shape tests: a two-way pin keyed by shape name plus pattern hash, as a
multiset, so a removed spec, an unpinned new spec, an edited regex and a lost duplicate are all
seen. Limits, stated rather than hidden. The two assignment entries do not leak when removed
alone, because the suffix entry redacts the same text, so only the pin sees them removed and a
behavioural probe cannot. The duplicated-name pairs (the private-key pair and the assignment pair)
are told apart only by the pin; the commit-outcome check works by rule name. It pins the table,
compares it both ways, asserts the subset, matches each generated value against its live rule and
checks the commit outcome. It does not check what the redactor or any sink does with a value.

The pin hash: the first 10 hex characters of sha256 over the UTF-8 text `<flags>:<pattern>`, where
flags are the regex flags masked to ignore-case, dot-all, multi-line and verbose. When a regex is
edited the finding prints the new hash, so the table update is one line.

Every generated value is built at run time from fragments with a seeded generator, so no
secret-shaped string sits on any source line and the output is deterministic.

Cost: O(shapes). One regex search and one generator call per entry and a few commit-gate lines
each, well under a second (not measured on CI). Linear at 10x and 100x. Reads one file, writes
nothing, spawns nothing, no network.
"""
import argparse
import collections
import hashlib
import importlib.util
import random
import re
import sys
from pathlib import Path

_FLAG_MASK = re.I | re.S | re.M | re.X
_DEFAULT_SCRUB = Path(__file__).resolve().parents[2] / "skills" / "sigma-loop" / "scripts" / "scrub.py"

_AL = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no look-alike characters
_UP = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"                           # no I or O: the exempt value is impossible
_B64 = _AL + "+/"
_DASH = "-" * 5


class Refusal(Exception):
    pass


def _f(rng, n, alphabet=_AL):
    return "".join(rng.choice(alphabet) for _ in range(n))


def _pem(rng, end):
    lines = [_DASH + "BEGIN PRIV" + "ATE KEY" + _DASH] + [_f(rng, 64, _B64) for _ in range(3)]
    if end:
        lines.append(_DASH + "END PRIV" + "ATE KEY" + _DASH)
    return "\n".join(lines)


def gen_pem_closed(rng):
    return _pem(rng, True)


def gen_pem_open(rng):
    return _pem(rng, False)


def gen_aws(rng):
    return "AK" + "IA" + _f(rng, 16, _UP)


def gen_gh(rng):
    return "gh" + "p_" + _f(rng, 30)


def gen_jwt(rng):
    return "ey" + "J" + _f(rng, 12) + "." + _f(rng, 12) + "." + _f(rng, 12)


def gen_auth(rng):
    return "Bear" + "er " + _f(rng, 24)


def gen_authz_header(rng):
    return "Author" + "ization: NTLM " + _f(rng, 20)


def gen_suffix(rng):
    return "DB_PASS" + "WORD=" + _f(rng, 16)


def gen_slack_tok(rng):
    return "xo" + "xb-" + _f(rng, 24)


def gen_github_pat(rng):
    return "github_" + "pat_" + _f(rng, 30)


def gen_anthropic(rng):
    return "sk-" + "ant-" + _f(rng, 30)


def gen_openai(rng):
    return "sk-" + "proj-" + _f(rng, 40)


def gen_sk(rng):
    return "sk-" + _f(rng, 30)


def gen_google(rng):
    return "AI" + "za" + _f(rng, 35, _AL + "_-")


def gen_stripe(rng):
    return "sk_" + "live_" + _f(rng, 24)


def gen_npm(rng):
    return "np" + "m_" + _f(rng, 36)


def gen_pypi(rng):
    return "pypi-" + "Ag" + "E" +_f(rng, 50)


def gen_gitlab(rng):
    return "gl" + "pat-" + _f(rng, 24)


def gen_hf(rng):
    return "h" + "f_" + _f(rng, 30)


def gen_webhook(rng):
    return ("hooks.slack.com/" + "serv" + "ices/T" + _f(rng, 8, _UP) + "/B" + _f(rng, 8, _UP)
            + "/" + _f(rng, 24))


def gen_url_pw(rng):
    return "post" + "gres://svc:" + _f(rng, 14) + "@example.com:5432/db"


def gen_quoted_assign(rng):
    return "api_" + "key = '" + _f(rng, 20) + "'"


def gen_plain_assign(rng):
    return "pass" + "word=" + _f(rng, 16)


# One row per live entry, in live order: (name, hash10, generator, commit_expect, span).
# commit_expect: `refused` (the entry's own rule fires), `accepted` (no rule fires), or
# `refused-by:<rule>` (a sibling fires and nothing under the entry's own name).
# span: 0 when the whole match is the planted value, 1 when group 1 is.
TABLE = (
    ("private-key", "bef2919910", gen_pem_closed, "refused", 0),
    ("private-key", "b7a938c42e", gen_pem_open, "refused", 0),
    ("aws-key", "fdecb0fe59", gen_aws, "refused", 0),
    ("gh-token", "397e46c453", gen_gh, "refused", 0),
    ("jwt", "78c5b26c17", gen_jwt, "refused", 0),
    ("auth", "60806861dc", gen_auth, "refused", 0),
    ("authorization-header", "d571ec4e8a", gen_authz_header, "refused-by:credential-assignment", 0),
    ("credential-assignment-suffix", "0fc647763e", gen_suffix, "accepted", 0),
    ("slack-token", "a38a5766b2", gen_slack_tok, "refused", 0),
    ("github-pat", "a142a50c10", gen_github_pat, "refused", 0),
    ("anthropic-key", "164bf99d68", gen_anthropic, "refused", 0),
    ("openai-key", "81773c677b", gen_openai, "refused", 0),
    ("sk-key", "892082db00", gen_sk, "refused", 0),
    ("google-key", "350c31ae33", gen_google, "refused", 0),
    ("stripe-key", "2cfc6638b2", gen_stripe, "refused", 0),
    ("npm-token", "d62ed4c567", gen_npm, "refused", 0),
    ("pypi-token", "67b55eaf33", gen_pypi, "refused", 0),
    ("gitlab-token", "ec3a111263", gen_gitlab, "refused", 0),
    ("huggingface-token", "95b1a9b6ea", gen_hf, "refused", 0),
    ("slack-webhook", "8c8ea108cd", gen_webhook, "refused", 1),
    ("url-password", "39c6b9f67d", gen_url_pw, "refused", 1),
    ("credential-assignment", "da04c030e6", gen_quoted_assign, "refused", 1),
    ("credential-assignment", "3e01982892", gen_plain_assign, "refused", 0),
)


def pin_hash(rx):
    text = "%d:%s" % (rx.flags & _FLAG_MASK, rx.pattern)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]


def load_scrub(path):
    path = Path(path)
    if not path.is_file():
        raise Refusal("scrub.py not found")
    try:
        spec = importlib.util.spec_from_file_location("_sigma_safety_scrub", str(path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as exc:  # any failure to load is a refusal, never a pass
        raise Refusal("scrub.py failed to import (%s)" % type(exc).__name__)
    return mod


def live_specs(scrub):
    """The live list as `[(name, hash10, rx)]`; absent or malformed is a Refusal."""
    specs = getattr(scrub, "_SECRET_PATTERN_SPECS", None)
    if specs is None:
        raise Refusal("scrub.py has no _SECRET_PATTERN_SPECS")
    out = []
    try:
        for spec in specs:
            name, rx = spec[0], spec[1]
            if len(spec) != 3 or not isinstance(name, str) or not hasattr(rx, "pattern"):
                raise ValueError
            out.append((name, pin_hash(rx), rx))
    except (TypeError, ValueError, IndexError):
        raise Refusal("a live spec is not (name, compiled regex, replacement)")
    return out


def validate_table(table):
    for row in table:
        if not isinstance(row, tuple) or len(row) != 5:
            raise Refusal("table row is not a 5-tuple")
        name, digest, _gen, expect, span = row
        if not (isinstance(name, str) and isinstance(digest, str) and span in (0, 1)):
            raise Refusal("table row has a bad name, hash or span")
        if expect not in ("refused", "accepted") and not (
                isinstance(expect, str) and expect.startswith("refused-by:") and len(expect) > 11):
            raise Refusal("table row has an unknown commit_expect")


def _find(kind, name, digest, detail):
    return "%s %s %s: %s" % (kind, name, digest, detail)


def check_two_way(live, table):
    pinned = collections.Counter((row[0], row[1]) for row in table)
    seen = collections.Counter((name, digest) for name, digest, _rx in live)
    out = [_find("unpinned", n, h, "live spec has no table entry") for (n, h) in (seen - pinned)]
    out += [_find("missing", n, h, "table entry has no live spec") for (n, h) in (pinned - seen)]
    return out


def check_shape_subset(scrub):
    live = {(n, rx.pattern, rx.flags) for n, rx, _r in scrub._SECRET_PATTERN_SPECS}
    return [_find("shape-subset", n, pin_hash(rx), "SHAPE_RULES entry is not in the live list")
            for n, rx in scrub.SHAPE_RULES if (n, rx.pattern, rx.flags) not in live]


def generate(row):
    return row[2](random.Random(row[0] + ":" + row[1]))


def check_generators(live, table, exempt=()):
    by_key = {(n, h): rx for n, h, rx in live}
    out = []
    for row in table:
        name, digest, gen, _expect, span = row
        if not callable(gen):
            out.append(_find("no-generator", name, digest, "entry has no generator"))
            continue
        text = generate(row)
        if any(v in text for v in exempt):
            out.append(_find("exempt-value", name, digest, "generated value contains an exempt fixture"))
        rx = by_key.get((name, digest))
        if rx is None:
            continue  # the two-way pin reports it
        m = rx.search(text)
        if m is None:
            out.append(_find("drift", name, digest, "generated value no longer matches its live regex"))
        elif span > rx.groups or not m.group(span):
            out.append(_find("empty-span", name, digest, "span group %d is empty on the generated value" % span))
    return out


def _outcome(name, rules):
    if not rules:
        return "accepted"
    return "refused" if name in rules else "refused-by:" + ",".join(sorted(rules))


def check_commit_outcomes(scrub, table):
    out = []
    for row in table:
        name, digest, gen, expect, _span = row
        if not callable(gen):
            continue
        rules = set()
        for line in generate(row).splitlines():
            rules.update(rule for rule, _col in scrub.commit_secret_hits(line))
        got = _outcome(name, rules)
        if got != expect:
            out.append(_find("commit-outcome", name, digest, "expected %s, measured %s" % (expect, got)))
    return out


def validate_scrub(scrub):
    """Every attribute the check reads from scrub must exist with the right type, else Refusal."""
    for attr in ("SHAPE_RULES", "COMMIT_SHAPE_RULES"):
        val = getattr(scrub, attr, None)
        if not isinstance(val, (tuple, list)):
            raise Refusal("scrub.py has no usable %s" % attr)
        for item in val:
            if not (isinstance(item, (tuple, list)) and len(item) == 2 and isinstance(item[0], str)
                    and hasattr(item[1], "pattern")):
                raise Refusal("scrub.py %s entry is not (name, compiled regex)" % attr)
    if not callable(getattr(scrub, "commit_secret_hits", None)):
        raise Refusal("scrub.py has no callable commit_secret_hits")
    fixtures = getattr(scrub, "COMMIT_FIXTURE_VALUES", None)
    if not isinstance(fixtures, (set, frozenset, tuple, list)) or not all(isinstance(v, str) for v in fixtures):
        raise Refusal("scrub.py has no usable COMMIT_FIXTURE_VALUES")


def run(scrub, table):
    live = live_specs(scrub)
    validate_scrub(scrub)
    validate_table(table)
    return (check_two_way(live, table) + check_shape_subset(scrub)
            + check_generators(live, table, scrub.COMMIT_FIXTURE_VALUES)
            + check_commit_outcomes(scrub, table))


def main(argv=None, scrub_path=None):
    argparse.ArgumentParser(description="Pin the live redaction shapes and check them two ways.").parse_args(argv)
    try:
        scrub = load_scrub(scrub_path or _DEFAULT_SCRUB)
        findings = run(scrub, TABLE)
    except Refusal as exc:
        print("check.py: REFUSED: %s" % exc, file=sys.stderr)
        return 2
    except Exception as exc:  # probing a doctored scrub must never read as findings
        print("check.py: REFUSED: probing scrub.py failed (%s)" % type(exc).__name__, file=sys.stderr)
        return 2
    for item in findings:
        print("check.py: FINDING " + item)
    if findings:
        return 1
    print("check.py: ok - %d live specs, %d table entries, %d shape rules" % (
        len(live_specs(scrub)), len(TABLE), len(scrub.SHAPE_RULES)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
