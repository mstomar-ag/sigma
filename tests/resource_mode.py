"""Carry the test session's resource-guard mode into an env a test builds from scratch. No test lives here.

#1759 makes the resource guard resolve to `enforce` when neither SIGMA_RESOURCE_MODE nor `resource.mode`
names a mode. `conftest.py`'s autouse fixture sets SIGMA_RESOURCE_MODE to `DEFAULT` so a loaded developer
machine cannot change an unrelated test's result (#1600), but a test that hands a subprocess a scrubbed env
dict, or one copied from `os.environ` at import time, drops that value: the child then samples the REAL
machine and the picker answers HOLD whenever the host is busy.

`carry(env)` reads SIGMA_RESOURCE_MODE at CALL time, so it follows whatever the running test set (the
fixture's `off`, or a gate test's own `enforce`). When nothing set it -- a module-scoped fixture runs
before any function-scoped autouse fixture -- it writes `DEFAULT`, the same value the fixture would have."""
import os

KEY = "SIGMA_RESOURCE_MODE"
#: the test session's mode; `conftest.py` sets exactly this for every test.
DEFAULT = "off"


def carry(env):
    """Return `env` (mutated in place) with the current SIGMA_RESOURCE_MODE, or `DEFAULT`, copied in."""
    env[KEY] = os.environ.get(KEY, DEFAULT)
    return env
