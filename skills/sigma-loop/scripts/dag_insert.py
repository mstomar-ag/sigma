"""Instant insertion into the stored map, and a refusal to write a cycle (#1616).

The stored map (`dag_store`) is rebuilt by a refresh once per TTL. Between two refreshes a goal Sigma
files itself would be absent from it, and a `Blocked by` line Sigma appends could close a cycle nobody
sees until the next refresh. This module closes both gaps from the filing path.

`insert(sdlc, config, number, ...)` -> `inserted`, `off`, `nomap`, `busy` or `error`. Under the refresh
lock (`dag_store.refresh_lock`, non-blocking) it loads the stored map, adds or refreshes the node for
`number` (an open issue, `in_corpus` true) and adds its edges: those parsed from `body` with
`dag_core.edges_from` plus the explicit `blockers` (each means `(blocker, number)`) and `blocked`
(each means `(number, blocked)`). A busy lock or no stored map is a quiet skip: the next refresh reads
the issue from GitHub. Mode `off` touches nothing and creates no file. It never raises: an internal
failure prints ONE stderr line and returns `error`. `life=None` keeps an existing node's life and
gives a new node `goal`.

A node (blocker or goal) the usable map has never seen is `None`, not `[]`: absence is not a verdict.

Without `fcntl` (Windows) the refresh lock yields `open`: `insert` then has NO mutual exclusion, so two
interleaved inserts can each save a map built from the same read and the later save drops the earlier
node. The file is still a valid map (every save is atomic) and the next refresh restores what was lost.

`closing_members(sdlc, config, blocker, blocked, ...)` answers "would the edge (blocker, blocked)
close a cycle?": the sorted member list when it would, `[]` when it would not (also in mode `off`,
which reads nothing), `None` when it cannot be decided. With a usable stored map the answer is a walk
over open dependents; with none, a bounded LIVE walk of the blocker's own blockers through `reader`
(`reader(number)` -> issue body text, `""` for a closed issue; or `run=` for the `gh_api` reader),
at most `LIVE_READ_CAP` reads. Reaching `blocked` is a cycle. The cap or a failed read is `None`.

`CycleRefused(ValueError)` carries `.members`; its message names every member as `#N`.

`record_unchecked` / `take_unchecked` keep `state/dag-unchecked.json`: number pairs only, at most
`UNCHECKED_CAP` (oldest dropped), written atomically. Each read-modify-write runs under its own lock
(`state/dag-unchecked.lock`, never the refresh lock, so `insert` cannot deadlock with it), waited for at
most `UNCHECKED_WAIT` seconds: two concurrent records keep both pairs and a pair recorded during a take
lands in the taken list or the file, never neither. `dag_refresh` clears it after a SAVED refresh,
because the refreshed map and `dag_cycles` then examine those edges; the file cannot grow past the cap.

Scale: `insert` is one read and one rewrite of the whole map file, serialised on the refresh lock, so
at 100x (about 20,000 nodes, a few MB) a filing pays one parse and one write of that file. The live walk
is at most 25 requests per `Blocked by` line, only while no usable map exists. Not measured against a
live repository; the counts are arithmetic. Numbers are the only thing kept from any body read.
"""
import contextlib
import importlib.util
import json
import os
import pathlib
import re
import sys
import time

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


dag_store = _load("dag_store")
dag_core = _load("dag_core")

LIVE_READ_CAP = 25
UNCHECKED_CAP = 200
UNCHECKED_FILE = "dag-unchecked.json"
UNCHECKED_LOCK = "dag-unchecked.lock"           # its own file: never the refresh lock `insert` already holds
UNCHECKED_WAIT = 5.0                            # seconds a record/take waits for a sibling, then goes unlocked
_PRIO = re.compile(r"^priority:(P[0-4])$")


class CycleRefused(ValueError):
    """The `Blocked by` line would close a dependency cycle; nothing was written."""

    def __init__(self, blocker, blocked, members):
        self.blocker, self.blocked, self.members = blocker, blocked, list(members)
        super().__init__("Blocked by #%d on #%d would close a dependency cycle: %s -- remove one Blocked by "
                         "line among them" % (blocker, blocked, ", ".join("#%d" % n for n in self.members)))


def _config(sdlc, config):
    if config is None:
        try:
            config = dag_store.state.load_config(sdlc)
        except Exception:                               # noqa: BLE001
            config = {}
    return config


def prio_of(labels):
    """`P0`..`P4` from a `priority:Pn` label in `labels`, else None."""
    for name in labels or ():
        m = _PRIO.match(str(name))
        if m:
            return m.group(1)
    return None


def blockers_in(text, number):
    """The sorted distinct blocker numbers `text` declares for issue `number` (`[]` when none, or when
    `number` is not an integer). No I/O."""
    try:
        return sorted({a for a, _b, _p in dag_core.edges_from(text or "", int(str(number).lstrip("#")))})
    except Exception:                                   # noqa: BLE001
        return []


# ------------------------------------------------------------------ insertion

def _upsert_node(m, number, life, prio, unit):
    for n in m["nodes"]:
        if n.get("number") == number:
            n["kind"], n["state"], n["in_corpus"] = "issue", "open", True
            if life is not None:
                n["life"] = life
            if prio is not None:
                n["prio"] = prio
            if unit is not None:
                n["unit"] = unit
            return
    n = {"number": number, "kind": "issue", "state": "open", "life": life or "goal", "prio": prio,
         "in_corpus": True}
    if unit is not None:
        n["unit"] = unit
    m["nodes"].append(n)


def insert(sdlc, config, number, *, body=None, blockers=(), blocked=(), life=None, prio=None, unit=None,
           now=None):
    """Add issue `number` and its edges to the stored map. -> `inserted`, `off`, `nomap`, `busy`, `error`."""
    try:
        config = _config(sdlc, config)
        if dag_store.mode(config) == "off":
            return "off"
        number = int(str(number).lstrip("#"))
        with dag_store.refresh_lock(sdlc, config) as held:
            if held == "busy":
                return "busy"
            m, _why = dag_store.load(sdlc, config)
            if m is None:
                return "nomap"
            _upsert_node(m, number, life, prio, unit)
            have = {(e["blocker"], e["blocked"]) for e in m["edges"]}
            wanted = [(a, number) for a, _b, _p in dag_core.edges_from(body or "", number)]
            wanted += [(int(a), number) for a in blockers]
            wanted += [(number, int(b)) for b in blocked]
            for a, b in wanted:
                if a != b and (a, b) not in have:
                    have.add((a, b))
                    m["edges"].append({"blocker": a, "blocked": b, "source": "body"})
            ok, why = dag_store.save(sdlc, m, config=config)
            return "inserted" if ok else ("off" if why == "off" else "error")
    except Exception as exc:                            # noqa: BLE001 - a filing is never failed by the map
        try:
            sys.stderr.write("sigma: dependency map insert skipped (%s)\n" % type(exc).__name__)
        except Exception:                               # noqa: BLE001
            pass
        return "error"


# ------------------------------------------------------------------ the cycle guard

def default_reader(run, repo=None):
    """A reader over the `gh_api` issue read: the body of an open issue, `""` for any other state."""
    gh_api = _load("gh_api")

    def read(number):
        issue = gh_api.read_issue(run, int(number), ["body", "state"], repo)
        return (issue.get("body") or "") if str(issue.get("state") or "").upper() == "OPEN" else ""
    return read


def _from_map(m, blocker, blocked):
    nodes = [dag_core.Node(n["number"], n.get("kind", "issue"), n.get("state", "unknown"), n.get("life", ""),
                           n.get("prio")) for n in m["nodes"]]
    g = dag_core.build(nodes, [(e["blocker"], e["blocked"], e.get("source", "other")) for e in m["edges"]])
    if blocker not in g.nodes or blocked not in g.nodes:
        return None                                     # a node the map has never seen: undecided, not "no cycle"
    down = set(dag_core.open_dependents(g, blocked))
    if blocker not in down:
        return []
    up = set(dag_core.closure(g, blocker)) | {blocker}
    return sorted((down & up) | {blocker, blocked})


def _live(blocker, blocked, reader, cap):
    """Walk the blockers of `blocker` through `reader`. -> members, `[]`, or None (cap or failed read)."""
    parent, queue, reads = {blocker: None}, [blocker], 0
    while queue:
        cur = queue.pop(0)
        if reads >= cap:
            return None
        reads += 1
        try:
            text = reader(cur)
        except Exception:                               # noqa: BLE001
            return None
        for nxt in blockers_in(text, cur):
            if nxt == blocked:
                path, node = {blocked}, cur
                while node is not None:
                    path.add(node)
                    node = parent[node]
                return sorted(path)
            if nxt not in parent:
                parent[nxt] = cur
                queue.append(nxt)
    return []


def closing_members(sdlc, config, blocker, blocked, *, reader=None, run=None, repo=None, now=None,
                    cap=LIVE_READ_CAP):
    """Members of the cycle that adding `(blocker, blocked)` would close; `[]` none; None undecidable."""
    try:
        config = _config(sdlc, config)
        if dag_store.mode(config) == "off":
            return []
        blocker, blocked = int(blocker), int(blocked)
        if blocker == blocked:
            return []
        m, _why = dag_store.load(sdlc, config)
        if m is not None:
            ok, _r = dag_store.usable(m, time.time() if now is None else now, dag_store.ttl_minutes(config))
            if ok:
                return _from_map(m, blocker, blocked)
        if reader is None:
            if run is None:
                return None
            reader = default_reader(run, repo)
        return _live(blocker, blocked, reader, cap)
    except Exception:                                   # noqa: BLE001
        return None


# ------------------------------------------------------------------ the unchecked list

def _unchecked_path(sdlc):
    return pathlib.Path(sdlc) / "state" / UNCHECKED_FILE


def _pairs(sdlc):
    try:
        raw = json.loads(_unchecked_path(sdlc).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for p in raw if isinstance(raw, list) else []:
        if (isinstance(p, list) and len(p) == 2
                and all(isinstance(v, int) and not isinstance(v, bool) for v in p)):
            out.append([p[0], p[1]])
    return out


@contextlib.contextmanager
def _unchecked_lock(sdlc):
    """Hold the unchecked-list lock (a blocking `flock` on its own file, polled for at most `UNCHECKED_WAIT`
    seconds). Without `fcntl`, an unwritable directory or a sibling that never lets go it yields anyway:
    a list update must not fail a filing. The lock dies with its holder."""
    fd, fcntl = None, dag_store.fcntl
    if fcntl is not None:
        try:
            p = _unchecked_path(sdlc).parent / UNCHECKED_LOCK
            os.makedirs(p.parent, exist_ok=True)
            fd = open(p, "a+")
            end = time.monotonic() + UNCHECKED_WAIT
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= end:
                        break
                    time.sleep(0.01)
        except OSError:
            pass
    try:
        yield
    finally:
        if fd is not None:
            fd.close()


def record_unchecked(sdlc, blocker, blocked, config=None):
    """Remember an edge written without a cycle verdict. Numbers only; never raises; off writes nothing."""
    try:
        if dag_store.mode(_config(sdlc, config)) == "off":
            return False
        pair = [int(blocker), int(blocked)]
        p = _unchecked_path(sdlc)
        os.makedirs(p.parent, exist_ok=True)
        with _unchecked_lock(sdlc):
            pairs = [q for q in _pairs(sdlc) if q != pair] + [pair]
            dag_store.state.atomic_write_text(p, json.dumps(pairs[-UNCHECKED_CAP:], separators=(",", ":")) + "\n")
        return True
    except Exception:                                   # noqa: BLE001
        return False


def unchecked(sdlc):
    """The recorded pairs, `[]` when none. Read-only."""
    return _pairs(sdlc)


def drop_unchecked(sdlc, pairs):
    """Remove only `pairs` from the list; any other pair (one recorded since they were read) stays.
    Never raises."""
    try:
        gone = {tuple(q) for q in pairs}
        with _unchecked_lock(sdlc):
            left = [q for q in _pairs(sdlc) if tuple(q) not in gone]
            p = _unchecked_path(sdlc)
            if not left:
                try:
                    p.unlink()
                except OSError:
                    pass
            else:
                dag_store.state.atomic_write_text(p, json.dumps(left, separators=(",", ":")) + "\n")
        return True
    except Exception:                                   # noqa: BLE001
        return False


def take_unchecked(sdlc):
    """Return the recorded pairs and clear the list. Never raises."""
    try:
        with _unchecked_lock(sdlc):
            pairs = _pairs(sdlc)
            try:
                _unchecked_path(sdlc).unlink()
            except OSError:
                pass
        return pairs
    except Exception:                                   # noqa: BLE001
        return []
