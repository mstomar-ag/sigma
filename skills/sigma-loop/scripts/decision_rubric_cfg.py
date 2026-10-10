"""Total reader for the `decision_rubric` config block (library only; no main).

Every part is OFF unless its own `enabled` is the JSON boolean true. The reader never raises: a
wrong-typed block, part or `enabled` closes the WHOLE block and `closed_reason()` names the key.
Keys that start with an underscore are notes and are ignored.
"""

PARTS = ("records", "hard_stops", "autonomy", "drift", "rulebook")
_KEY = "decision_rubric"


class RubricConfig:
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
            return RubricConfig()
        if not isinstance(block, dict):
            return RubricConfig(reason="decision_rubric must be an object")
        enabled = []
        for part in PARTS:
            sub = block.get(part)
            if sub is None:
                continue
            if not isinstance(sub, dict):
                return RubricConfig(reason="decision_rubric.%s must be an object" % part)
            flag = sub.get("enabled")
            if flag is None or flag is False:
                continue
            if flag is not True:
                return RubricConfig(reason="decision_rubric.%s.enabled must be true or false" % part)
            enabled.append(part)
        return RubricConfig(enabled)
    except Exception:  # total by contract
        return RubricConfig(reason="decision_rubric could not be read")


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
                "set decision_rubric.records.repo_visibility to public")
    return None
