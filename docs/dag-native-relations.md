# Research: the host's native blocked-by relations as a read-only edge source

Status: research note, no product code. Part of the dependency-map epic (#1606). Question: could
GitHub's own issue relations be an extra, read-only source of edges next to the text markers, the
markers staying primary because they work outside GitHub too?

**Recommendation: do not build it in this release.** Reasons are in section 6. The decision is
cheap to revisit: section 5 lists what the map would need.

## 1. What was measured and what was not

Real reads were made against this project's own public repository with the loop's token (scopes
`repo`, `project`, `read:org`, `workflow`), on 17 Oct 2026. Every number below is labelled
*measured* or *documented*. The token is shared with other sessions on the same machine, so a
change in the remaining-request counter between two calls is NOT attributable to one call: the
cost figures are upper bounds from a small number of calls, not a benchmark.

**Not measured: a response with at least one native relation in it.** Every open issue in the run
label has zero relations, so every read below returned an empty list. Creating a relation would be
a write to the host, and the offline lane does not make one. Item shapes in section 2 therefore
come from the host's published API description, not from a read. A slice that builds this must
first take one real non-empty read on a scratch repository.

## 2. Endpoints

All are `GET`, read-only, under `repos/{owner}/{repo}/issues/{number}/`:

| Path | Meaning | Result |
|---|---|---|
| `dependencies/blocked_by` | issues that block this one | JSON array of issue objects (measured: `[]`, status 200) |
| `dependencies/blocking` | issues this one blocks | JSON array of issue objects (measured: `[]`, status 200) |
| `sub_issues` | children of this issue | JSON array (measured: `[]`, status 200); a parent/child tree, not a blocker, so it must NOT become an edge |

Also *measured*: the plain issue object carries `issue_dependencies_summary` with the integers
`blocked_by`, `blocking`, `total_blocked_by`, `total_blocking`, and `sub_issues_summary` with
`total`, `completed`, `percent_completed`. A list read of issues (the call the loop already makes)
therefore tells, for free, which issues have any relation at all: only those need a per-issue read.

*Documented, not measured:* each list item is a full issue object (`number`, `state`,
`repository_url`, ...); the list is paginated like other issue lists; relations may cross
repositories, so an item's repository must be compared with the repo being mapped, as
`blocker_scan` already skips other-repository references. Writes (`POST .../dependencies/blocked_by`
with a body naming an issue id) exist and are out of scope.

## 3. Permission and plan

*Measured:* a token with `repo` scope reads all three on a public repository; the repository
permission seen was admin. *Documented, not measured:* reading needs read access to issues; whether
a plan tier gates the relations feature on private repositories was not checked and no read was
attempted with a narrower token. A source that fails with 403 or 404 must read as "no native
edges", never as an error that stops the map.

## 4. Cost and conditional requests

*Measured:* a plain `GET` of `dependencies/blocked_by` returned status 200 with an `ETag` and
`Cache-Control: private, max-age=60`. Repeating it with `If-None-Match` and that ETag returned
status 304 with no body. Across three consecutive reads (200, 304, 200) the remaining-request
header fell by one per call, including the 304; on this shared token that is an upper bound on the
cost of one call, about one request each. The same ETag value was returned for the `dependencies/
blocking` and `sub_issues` reads of the same issue, so it identifies the issue, not the list; do not
use one issue's ETag to skip another read.

Consequence: a full native refresh costs one request per issue that the summary field says has a
relation, plus the list read the loop already pays. At 100 relation-bearing issues that is about
100 requests, 2 percent of an hourly 5000 budget; at 1000 it is 20 percent and needs the summary
filter plus ETags. This is arithmetic from the measured one-request cost, not a timing.

## 5. How a native edge would enter the map

The core takes `(blocker, blocked, source)` triples, and `source` today is the text phrase. A
native edge is a triple whose source is the literal `native`, next to `body` and `ledger`. The
stored map (schema `dag/1`) would need: `native` added to the closed list of edge sources; a header
field for the native change-check cursor; and `partial` set when a per-issue read fails.

**Conflict rule.** The text marker wins. The map keeps the union of edges (a native edge with no
marker still blocks, because the host asserted it), and a pair present in exactly one source is
reported as `disagree`, once per refresh, as a count in `dag.py show`. A marker never gets removed
because the host lacks the relation, and a native relation never gets removed because the text
lacks it. Reason: the markers are what agents write and what works off the host; the relation is
what a person clicked.

## 6. Why not now

1. Its only unique value is edges nobody wrote as text; none of the label's issues has one.
2. A non-empty response shape is unmeasured (section 1).
3. It adds a per-issue request class to the refresh and a second failure mode, for a source the
   rest of Sigma (local files, other hosts) cannot supply.

Build only when a real repository has relations that the text markers miss.

## Appendix: scratch spike (not part of the product)

Parses a native response into edges with the core's edge type, using a hand-written fixture shaped
like the documented list (it is not a captured response, see section 1):

```python
import dag_core

def native_edges(this, items, repo):
    """items = the parsed JSON of dependencies/blocked_by for issue `this`."""
    out = []
    for it in items:
        if not str(it.get("repository_url", "")).endswith("/" + repo):
            continue                      # other repository: skip, as blocker_scan does
        out.append((int(it["number"]), int(this), "native"))
    return out

def test_native_edges_enter_the_core():
    items = [{"number": 5, "repository_url": "x/acme/widget"},
             {"number": 9, "repository_url": "x/acme/other"}]
    edges = native_edges(7, items, "acme/widget")
    assert edges == [(5, 7, "native")]
    nodes = [dag_core.Node(5, "issue", "open", "", "P1"), dag_core.Node(7, "issue", "open", "", "P1")]
    g = dag_core.build(nodes, edges)
    assert g.blockers[7] == [5] and g.edges == [(5, 7, "native")]
```
