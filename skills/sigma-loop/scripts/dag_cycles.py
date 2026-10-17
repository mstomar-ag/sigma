"""Dependency cycles, reported and held (#1615).

Two goals that block each other can never be picked: the picker already holds them (it never claims a
goal with an open prerequisite), but its summary used to promise they "become pickable by themselves",
which is false for a cycle. The stored map (`dag_store`) already knows the cycles; this module reads
them and says so.

`find(sdlc, config)` -> the distinct cycles, each a sorted tuple of member numbers, from the STORED map
only (one file read, no network). `[]` when `discovery.dag.mode` is `off`, and when the map is missing
or unusable. `line(cycle)` / `lines(sdlc, config)` are the one-line texts the pick summary, the status
line and the doctor share; they carry numbers and a fixed instruction, never a title.

`post(sdlc, config, run)` is the only write, and it writes comments and nothing else: no label, no
claim, no board field. One comment per distinct cycle, on its lowest-numbered OPEN member (a member
that is `unknown` or a pull request is not chosen; none open means no comment). The comment opens with
the marker `<!-- sigma:dag-cycle 3,7,9 -->`; before posting, the member's comments are read for that
exact marker, so a second refresh posts nothing and a changed member set (a new key) posts exactly one
new comment. It is silent when the mode is `off` or `discovery.dag.cycle_comment` is false, bounded to
`MAX_CYCLES` cycles per call, and never raises. The comment text is built from digits, commas, `#`
and `INSTRUCTION`; nothing a person wrote can enter it.

Cost: per cycle, one read of the member's comments (`1 + ceil(n/100)` requests for n comments) and, only
when the marker is absent, one POST. At 20 cycles that is at most 20 posts and about 40 reads per
refresh; it runs only after a refresh that saved a map, so once per TTL. A cycle already commented on
costs the read and nothing else. Not measured against a live repository; the counts are arithmetic.
The marker read is `view_issue(..., comments="all")`, so a marker is found however old it is.
"""
import importlib.util
import pathlib

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


dag_store = _load("dag_store")
dag_core = _load("dag_core")

MAX_CYCLES = 20
INSTRUCTION = "Remove one `Blocked by` line among these goals so one of them can be picked."
_OPEN_KIND = "issue"


def _config(sdlc, config):
    if config is None:
        try:
            config = dag_store.state.load_config(sdlc)
        except Exception:                               # noqa: BLE001
            config = {}
    return config


def _map(sdlc, config):
    config = _config(sdlc, config)
    if dag_store.mode(config) == "off":
        return None, config
    m, _why = dag_store.load(sdlc, config)
    return m, config


def _cycles_of(m):
    nodes = [dag_core.Node(n["number"], n["kind"], n["state"], n.get("life", ""), n.get("prio"))
             for n in m["nodes"]]
    g = dag_core.build(nodes, [(e["blocker"], e["blocked"], e.get("source", "other")) for e in m["edges"]])
    return dag_core.cycles(g), {n["number"]: n for n in m["nodes"]}


def find(sdlc, config=None):
    """The distinct cycles in the stored map, `[]` when there is none to read. Never raises."""
    try:
        m, _config_ = _map(sdlc, config)
        return [] if m is None else list(_cycles_of(m)[0])
    except Exception:                                   # noqa: BLE001 - a report that cannot be made is empty
        return []


def members(cycle):
    return ", ".join("#%d" % n for n in cycle)


def line(cycle):
    return "dependency cycle: %s -> remove one Blocked by line" % members(cycle)


def lines(sdlc, config=None):
    return [line(c) for c in find(sdlc, config)]


def marker(cycle):
    return "<!-- sigma:dag-cycle %s -->" % ",".join(str(n) for n in cycle)


def comment_body(cycle):
    return "%s\nThese goals block each other: %s.\n%s" % (marker(cycle), members(cycle), INSTRUCTION)


def _target(cycle, nodes):
    """The lowest-numbered member that is an OPEN issue, or None."""
    for n in sorted(cycle):
        rec = nodes.get(n) or {}
        if rec.get("state") == "open" and rec.get("kind") == _OPEN_KIND:
            return n
    return None


def _has_marker(gh_api, run, number, repo, key):
    issue = gh_api.view_issue(run, number, repo=repo, comments="all")
    return any(key in str((c or {}).get("body") or "") for c in (issue.get("comments") or [])
               if isinstance(c, dict))


def post(sdlc, config, run):
    """Comment once per distinct cycle. -> the list of issue numbers commented on. Never raises."""
    posted = []
    try:
        m, config = _map(sdlc, config)
        if m is None or not dag_store.cycle_comment(config):
            return posted
        gh_api = _load("gh_api")
        gh_cfg = ((config.get("discovery") or {}).get("github") or {}) if isinstance(config, dict) else {}
        repo = gh_cfg.get("repo") or (m.get("header") or {}).get("repo")
        found, nodes = _cycles_of(m)
        for cycle in found[:MAX_CYCLES]:
            try:
                target = _target(cycle, nodes)
                if target is None or _has_marker(gh_api, run, target, repo, marker(cycle)):
                    continue
                gh_api.comment_issue(run, target, comment_body(cycle), repo=repo)
                posted.append(target)
            except Exception:                           # noqa: BLE001 - one cycle's failure is not the next's
                continue
    except Exception:                                   # noqa: BLE001
        pass
    return posted
