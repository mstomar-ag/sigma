#!/usr/bin/env python3
"""dag_titles.py -- `dag.py titles`: read the open issues' titles once and keep them in a file the HTML pages use (#1987).

The stored dependency map holds no titles, by design. `dag.py html` therefore named members by number unless the
caller made a `--titles` file by hand. This verb makes that file: `<sdlc>/state/dag-titles.json`,

    {"schema": "dag-titles/1", "read_at": "<iso>", "titles": {"<number>": "<title>"}}

It is the one network verb of the pair; `dag.py html` only reads the file and never calls GitHub. Titles are never
written into the map or into a repository file.

Reads: `gh_api.list_issues` (REST, 100 per page), unlabelled, open issues. Titles are scrubbed with
`dag_html.clean_title` (the same cut as `--titles`). Only numbers that are map nodes are kept, so the file cannot
outgrow the map; a closed member is no longer listed, so its title is carried over from the earlier file (no call).

Calls: ceil(open / 100), at most TITLES_LIST_CAP / 100 = 50. At 10x (1,000 open issues) that is 10 calls; at 100x
(10,000 open) the cap binds at 50 calls and the run says how many issues were left out, so members past the cap are
named by number. One REST read per page, the cheap `core` resource, never GraphQL. Not measured against a real
repository here.

Write: one atomic replace of that file, after the whole listing succeeded: a failed listing leaves the old file
byte-identical. `discovery.dag.mode` off prints `dag: off` and touches nothing.
"""
import importlib.util
import json
import pathlib
import time

_HERE = pathlib.Path(__file__).resolve().parent
TITLES_LIST_CAP = 5000
FILE = "dag-titles.json"
SCHEMA = "dag-titles/1"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


dag_html = _load("dag_html")
dag_store = dag_html.dag_store
gh_api = _load("gh_api")


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def run(sdlc, now=None, run=None):
    """`(lines, exit_code)`: 0 written, 1 listing failed (old file kept), 3 no readable map, 4 mode off."""
    now = time.time() if now is None else now
    try:
        m, _md, config, _ok, _why = dag_html._open(sdlc, now)
    except dag_html.HtmlError as exc:
        return [str(exc)], exc.code
    members = {n.get("number") for n in m.get("nodes") or [] if isinstance(n, dict)}
    path = pathlib.Path(sdlc) / "state" / FILE
    gh = ((config.get("discovery") or {}).get("github") or {})
    repo = gh.get("repo") or "" if isinstance(gh, dict) else ""
    calls = []

    def counted(args):
        calls.append(1)
        return (run or gh_api._default_run)(args)

    try:
        got = gh_api.list_issues(counted, repo, [], TITLES_LIST_CAP)
    except Exception as exc:                            # noqa: BLE001 - the class name is the line
        return ["dag titles: listing failed (%s); %s kept as it was" % (type(exc).__name__, FILE)], 1
    titles = {}
    try:
        old = dag_html.read_titles(str(path)) if path.exists() else {}
    except dag_html.HtmlError:
        old = {}
    listed = set()
    for it in got:
        if isinstance(it, dict) and isinstance(it.get("number"), int) and it["number"] in members:
            listed.add(it["number"])
            if isinstance(it.get("title"), str) and it["title"].strip():
                titles[it["number"]] = dag_html.clean_title(it["title"])
    kept = 0
    for n, t in old.items():
        if n in members and n not in listed:
            titles[n] = t
            kept += 1
    doc = {"schema": SCHEMA, "read_at": _iso(now), "titles": {str(n): titles[n] for n in sorted(titles)}}
    path.parent.mkdir(parents=True, exist_ok=True)
    dag_store.state.atomic_write_text(path, json.dumps(doc, indent=1, sort_keys=True) + "\n")
    lines = ["dag titles: %d titles written to %s (%d calls, %d carried over from the earlier file)"
             % (len(titles), path, len(calls), kept)]
    if len(got) >= TITLES_LIST_CAP:
        lines.append("  note: the listing hit its cap of %d issues; any open member past it was left out and is "
                     "named by number" % TITLES_LIST_CAP)
    return lines, 0
