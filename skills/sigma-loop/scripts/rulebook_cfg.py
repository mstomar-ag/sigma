"""Total reader for the `rulebook` config block (library only; no main).

Every part is OFF unless its own `enabled` is the JSON boolean true. The reader never raises: a
wrong-typed block, part or `enabled` closes the WHOLE block and `closed_reason()` names the key.
Keys that start with an underscore are notes and are ignored.
"""

PARTS = ("records", "hard_stops", "autonomy", "drift", "rules")
_KEY = "rulebook"


class RulebookConfig:
    def __init__(self, enabled=(), reason=None):
        self._enabled = frozenset(enabled)
        self._reason = reason

    def part_enabled(self, name):
        return self._reason is None and name in self._enabled

    def closed_reason(self):
        return self._reason


def load(config):
    try:
        block = config.get(_KEY) if isinstance(config, dict) else None
        if block is None:
            return RulebookConfig()
        if not isinstance(block, dict):
            return RulebookConfig(reason="rulebook must be an object")
        enabled = []
        for part in PARTS:
            sub = block.get(part)
            if sub is None:
                continue
            if not isinstance(sub, dict):
                return RulebookConfig(reason="rulebook.%s must be an object" % part)
            flag = sub.get("enabled")
            if flag is None or flag is False:
                continue
            if flag is not True:
                return RulebookConfig(reason="rulebook.%s.enabled must be true or false" % part)
            enabled.append(part)
        return RulebookConfig(enabled)
    except Exception:  # total by contract
        return RulebookConfig(reason="rulebook could not be read")


def visibility(config):
    try:
        v = config[_KEY]["records"]["repo_visibility"]
    except Exception:
        return "private"
    return "public" if v == "public" and isinstance(v, str) else "private"


def public_repo_warning(config, remote_is_public):
    """Text only when the remote is known public and the setting says private; unknown never warns."""
    if remote_is_public is True and visibility(config) == "private":
        return ("records.repo_visibility is private but the repository looks public; "
                "set rulebook.records.repo_visibility to public")
    return None


#: `rulebook.import` bounds the read-only past-decision scan (`decision_import.py`). It is NOT
#: a part and no gate reads it: a scan only reports. `comments` (default false) lets the scan read
#: issue comments over the network; `max_issues` bounds how many issues that touches.
#: `max_file_bytes` is a streaming-safe ceiling (PROVISIONAL, 500 MB); the real bound is
#: `time_budget_s` (PROVISIONAL, 60), a total per scan, and a file it cuts short is reported partial.
IMPORT_DEFAULTS = {"max_file_bytes": 500000000, "max_candidates": 500, "max_issues": 50,
                   "time_budget_s": 60, "comments": False, "profile_dirs": [], "project_dirs": [],
                   "classifier": "table", "model": "haiku",
                   "max_budget_usd_per_call": 0.50, "max_budget_usd_per_run": 1.0,  # budgets PROVISIONAL; the CLI checks the cap after a turn
                   "timeout_s": 60,  # per model-classifier call, seconds (#1310); longer is allowed
                   "require_repo_cwd": False}  # keep only transcript answers whose cwd is inside the repository (#1477)


def import_limits(config):
    """The import bounds, total: a missing or wrong-typed key falls back to its default."""
    out = dict(IMPORT_DEFAULTS, profile_dirs=[], project_dirs=[])
    try:
        sub = config["rulebook"]["import"]
        if not isinstance(sub, dict):
            return out
        for key in ("max_file_bytes", "max_candidates", "max_issues", "time_budget_s"):
            v = sub.get(key)
            if isinstance(v, int) and not isinstance(v, bool) and v > 0:
                out[key] = v
        if sub.get("comments") is True:
            out["comments"] = True
        if sub.get("require_repo_cwd") is True:
            out["require_repo_cwd"] = True
        if sub.get("classifier") == "model":
            out["classifier"] = "model"
        if isinstance(sub.get("model"), str) and sub["model"].strip():
            out["model"] = sub["model"].strip()
        for key in ("max_budget_usd_per_call", "max_budget_usd_per_run", "timeout_s"):
            v = sub.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v < float("inf"):
                out[key] = float(v)
        dirs = sub.get("profile_dirs")
        if isinstance(dirs, list):
            out["profile_dirs"] = [d for d in dirs if isinstance(d, str) and d]
        dirs = sub.get("project_dirs")
        if isinstance(dirs, list):
            out["project_dirs"] = [d for d in dirs if isinstance(d, str) and d]
    except Exception:
        pass
    return out
