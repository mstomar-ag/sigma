"""Doctor preflight helpers for landing work: a missing verify interpreter and a branch-creation ruleset (#974).

Library only, stdlib only, no CLI. Two pure checks the doctor calls and words as rows:

* `interpreter_missing(command, which)` -> the first word of a verify command when it cannot be found, else None.
  It is deliberately conservative: anything it cannot judge from the text alone (variables, `~`, command
  substitution, shell builtins and wrappers, compound commands, unparsable quoting, relative paths) says nothing,
  so a clean result means "no evidence of a problem", not "the command runs". A command that starts with `cd` is
  never checked.
* `ruleset_blockers(run, repo, prefix)` -> `(blockers, unknown)`. It reads the rules active on the probe branch
  `<prefix>probe`, and for each distinct ruleset that restricts creation or update it reads the ruleset and its
  `current_user_can_bypass`. A bypass of always or exempt is fine; anything else is a blocker, listed by NAME as
  `{"name", "bypass"}` (ids are never returned). `unknown` is True when any read failed, was malformed, or a
  blocker's bypass state could not be read.

HONESTY: the real GitHub reply shape (the `ruleset_id` / `ruleset_source_type` fields on a branch rule and the
`current_user_can_bypass` field on a ruleset) is ASSUMED from the documentation and faked in tests; it was not seen
live. Never raises out of `interpreter_missing`; `ruleset_blockers` reports failure through `unknown`.
"""
import importlib.util
import os
import pathlib
import shlex

_SKIP = frozenset((
    "cd", "export", "source", ".", "set", "exec", "test", "[", "time", "sudo", "!", "(", "{", "unset", "eval",
    "if", "for", "while", "until", "case", "function", "alias", "trap", "command", "builtin", "nohup", "nice",
    "timeout", "xargs", "stdbuf", "caffeinate", "echo", "printf", ":", "true", "false", "umask", "ulimit", "wait", "read", "return", "exit", "local",
))
_ODD = ("$", "~", "`")
_OK_BYPASS = ("always", "exempt")


def _is_assignment(word):
    name, eq, _ = word.partition("=")
    return bool(eq) and bool(name) and (name[0].isalpha() or name[0] == "_") and \
        all(c.isalnum() or c == "_" for c in name)


def interpreter_missing(command, which):
    """The first word of `command` when it provably cannot be run, else None. See the module docstring."""
    try:
        words = shlex.split(command or "")
        i = 0
        while i < len(words) and (_is_assignment(words[i]) or words[i] == "env"):
            i += 1
        if i >= len(words):
            return None
        word = words[i]
        if word.startswith("-") or word in _SKIP or any(ch in word for ch in _ODD) or word[0] in "({!":
            return None
        if "/" in word:
            if not word.startswith("/"):
                return None
            return None if (os.path.isfile(word) and os.access(word, os.X_OK)) else word
        return None if which(word) else word
    except Exception:                    # noqa: BLE001 - a check that cannot judge says nothing
        return None


def _load_gh_api():
    path = pathlib.Path(__file__).resolve().parent / "gh_api.py"
    spec = importlib.util.spec_from_file_location("gh_api", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def ruleset_blockers(run, repo, prefix):
    """`(blockers, unknown)` for branch creation under `prefix`. `run` is gh_api's runner (args without the binary)."""
    blockers, unknown = [], False
    try:
        gh = _load_gh_api()
        rules = gh.branch_rules(run, repo, "%sprobe" % prefix)
    except Exception:                    # noqa: BLE001
        return [], True
    seen = set()
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("type") not in ("creation", "update"):
            continue
        rid = rule.get("ruleset_id")
        source = rule.get("ruleset_source_type")
        org = rule.get("ruleset_source") if source == "Organization" else None
        key = (org, rid)
        if key in seen:
            continue
        seen.add(key)
        try:
            data = gh.org_ruleset(run, org, rid) if org else gh.ruleset(run, repo, rid)
            name = data.get("name")
            bypass = rule.get("current_user_can_bypass", data.get("current_user_can_bypass"))
            if not isinstance(name, str) or not name:
                unknown = True
                continue
            if not isinstance(bypass, str) or not bypass:
                unknown = True
                continue
        except Exception:                # noqa: BLE001
            unknown = True
            continue
        if bypass not in _OK_BYPASS:
            blockers.append({"name": name, "bypass": bypass})
    return blockers, unknown
