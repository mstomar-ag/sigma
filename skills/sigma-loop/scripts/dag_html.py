"""One readable, offline HTML page per chain, from the stored dependency map (#1924). Library; `dag.py html` is its CLI.

A chain is ONE goal plus the open prerequisites it waits on: exactly the members `chain.py plan N` reads from the
map, because the page calls the same code (`chain.map_closure`, the plan's map half, and `chain_forecast`, the
plan's marker rule). It is never a whole weakly connected component: two end goals glued by a shared prerequisite
are two pages. `--all` writes a page for every end goal (an open issue that no open goal waits on and that waits on
something open, as `dag.py chains` lists them) and an index page.

What a page holds, and only this: numbers, the optional titles the caller passes, the map's life, priority rank and
unit token, the forecast markers and the edges. The map holds no title, label text or body, so a title comes only
from `--titles FILE` (a JSON object `{"<number>": "<title>"}`, or a list of `{"number", "title"}`; 4 MiB at most) and
a page without it names members by number. A title is scrubbed: any URL and the letters `http` are neutralised, so
no page carries a link or a network reference, and it is cut at 100 characters. No issue body is ever read here.

Reads the stored map only, makes no GitHub call (it never imports a network helper on this path), and writes only
`<sdlc>/state/dag-html/` or `--out`. An `--out` inside a git checkout that does not ignore it is refused (a page
must never make the root checkout dirty), and an existing page of an earlier run is overwritten, never deleted.
A map that is not fresh (older than twice its TTL, partial) is still drawn, with the reason at the top: the page
is a view, and says what it is a view of.

States (colour, and the same word written in the node): the plan's own markers `ready now`, `after #N`,
`needs decision`, `someone else's`, `checkpoint`, `not workable`, plus `closed` (a resolved prerequisite, drawn
muted on the left), `outside` (an open pull request the plan lists as external) and `cycle` (a member of a
dependency cycle, which the plan refuses). From the map alone only the life is known, so the markers are the map's
reading; the page says so, and that the live plan also sees assignees, checkpoints and not-a-goal labels. Edges:
explicit (an issue body) solid, unconfirmed (a wait read from prose, live plan only) dashed, ledger (any other
recorded source) dotted.

THE PLAN PAGE (`chain.py html`, `run_plan`). The map holds no wait written in prose or a comment, so a map page
cannot show one. The plan page draws the STORED plan record instead (what `chain.py plan` wrote and `sheet` reads):
every member, done prerequisite and external, every edge with its tag, and the sheet's questions. The questions are
parsed out of `chain.render_sheet`'s own text (`read_sheet`), so their number, wording and recorded answers cannot
differ from the sheet's, and a sheet whose shape is not recognised is refused (3), not guessed. A question is badged
on the member its id names (`kind:N`; both of `kind:A-B`; every member of a grouped one; the end goal for a question
about the whole chain). No record is refused (2) naming `chain.py plan`; no GitHub call; output `chain-plan-<N>.html`.
The record does not say which member a closed prerequisite freed, so those carry edges only where a closed-gate
warning names the dependents.

Layout, in Python: one column per depth (blockers left, the work that waits on them to the right), closed and
outside prerequisites in a first column; an edge crossing columns passes through a dummy slot in each, so it runs in
a channel and not through a node; the order within a column is a barycentre sweep, down then up, keeping the
ordering with the fewest crossings seen. Then inline SVG, CSS variables with light and dark themes, a side panel
filled by a little vanilla JS on click, and zoom and pan by the SVG viewBox. No library, no CDN, nothing fetched.

Bounds. `MEMBER_CAP` (80) open members is the readable ceiling: a chain over it is still written, with a notice at
the top. Titles are read from at most 4 MiB. Cost per page is O(passes x edges x depth) for the layout and
O(members + edges) for the rest; dummy slots are skipped (long edges drawn direct, and said so) past
`DUMMY_CAP`. Measured on one synthetic shape (shallow, no tangle): 400 members 248 KB in 0.06 s, 1,000 members 612 KB in
0.35 s; a tangled chain is not measured, and `tests/test_dag_html.py` bounds a 400-member page at 1.5 MB. `--all` is
the sum of its chains, one page each.

THE WORK THAT WAITS ON THE GOAL (#1986). Beyond the prerequisites, a `--goal` page also draws the goal's open
dependents, transitive, in columns to its right: open issues only (never a closed one or a pull request), nearest
first and ties by number, drawn muted as state `waiting`, each edge from the goal or another dependent tagged
`holds`. The heading says how many open issues wait on the goal. `DEPENDENT_CAP` (40) is the most it draws; the
count left out is written on the page ("N more open issues wait on #G and are not drawn (cap 40)"), never silently
dropped. The prerequisite half is untouched: `open`, `depth`, `decisions`, `who` and the held-by list count only the
members `chain.map_closure` returns. Size: the dependents add at most 40 nodes and their edges (about 25 KB) to a page.
At 10x (400 dependents) and at 100x (4,000) the page still draws 40 and writes one count, so its size does not grow;
the walk costs O(open dependents + their edges), a few milliseconds at 4,000 (reasoned from the loop, not timed).
"""
import collections
import html
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import textwrap
import time

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


dag_chain = _load("dag_chain")
chain = _load("chain")
chain_forecast = chain.chain_forecast
dag_store = dag_chain.dag_store
dag_core = dag_chain.dag_core

DEPENDENT_CAP = 40              # open dependents of the goal a page draws; the rest are counted and written, not drawn
MEMBER_CAP = 80                 # open members a page draws readably; over it the page still writes, with a notice
DUMMY_CAP = 6000                # layout slots for long edges; past it they are drawn direct, and the page says so
TITLES_MAX_BYTES = 4 * 1024 * 1024
TITLE_CHARS = 100
UNASSIGNED, UNKNOWN = "(unassigned)", "(unknown)"    # index tokens: not login-shaped, so no login can collide
SWEEPS, SWEEPS_BIG = 8, 2
BIG_SEGMENTS = 3000
NODE_W, NODE_H, DUMMY_H, GAP_Y, COL_GAP, MARGIN = 220, 78, 14, 14, 84, 28
BADGE_R, BADGE_MAX = 9, 8       # a question badge on a node: its radius, and how many a node draws before "+n"
EXIT = {"ok": 0, "refused": 2, "unusable": 3, "off": 4}

CLASS = {"ready now": "ready", "after": "after", "needs decision": "decision", "someone else's": "else",
         "checkpoint": "check", "not workable": "stuck", "closed": "closed", "outside": "outside", "cycle": "cycle",
         "paid gate": "paid", "parked": "parked", "not a goal": "notgoal", "awaiting owner": "awaiting"}
PLAN_WORDS = ("paid gate", "parked", "not a goal", "awaiting owner")      # states only a plan page (`chain.py html`) names
LEGEND = (("ready now", "can be claimed now"), ("after", "waits on a member still open"),
          ("needs decision", "parked, unarmed or not a goal"), ("someone else's", "claimed or assigned elsewhere"),
          ("checkpoint", "a gate, never worked"), ("not workable", "held, blocked or unreadable"),
          ("paid gate", "asks for paid work; never defaulted"), ("parked", "parked, waits for a decision"),
          ("not a goal", "blocks others but is not a goal"), ("awaiting owner", "waits for its owner to confirm it"),
          ("closed", "resolved prerequisite"), ("outside", "open pull request outside the chain"),
          ("cycle", "in a dependency cycle"))
EDGES = (("explicit", "written in an issue body"), ("unconfirmed", "read from prose, not confirmed"),
         ("ledger", "any other recorded source"))


class HtmlError(Exception):
    """A request that writes nothing. `code` is the exit code (2, 3 or 4); `str(self)` is the line."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------------ inputs

def clean_title(text):
    """A title safe for the page: one line, no URL, no `http`, at most TITLE_CHARS characters."""
    t = re.sub(r"(?i)\b[a-z][a-z0-9+.-]*://\S*", "(link)", str(text))
    t = re.sub(r"(?i)http", "ht\u200btp", t)
    t = " ".join("".join(c if c.isprintable() else " " for c in t).split())
    return t if len(t) <= TITLE_CHARS else t[:TITLE_CHARS - 1].rstrip() + "\u2026"


def _clean_login(x):
    """A login as text: a plain login stays as it is; anything else is scrubbed like a title, has no space, and is
    cut to the login length. Never a link."""
    if isinstance(x, str) and dag_store._LOGIN.fullmatch(x):
        return x
    return "-".join(clean_title(x).split())[:39]


def read_titles(path):
    """`{number: title}` from a JSON file, bounded; raises HtmlError 2 on anything but the two documented shapes."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(TITLES_MAX_BYTES + 1)
    except OSError as exc:
        raise HtmlError(2, "dag html: cannot read the titles file (%s)" % type(exc).__name__)
    if len(raw) > TITLES_MAX_BYTES:
        raise HtmlError(2, "dag html: the titles file is over %d bytes; refusing rather than truncating"
                        % TITLES_MAX_BYTES)
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise HtmlError(2, "dag html: the titles file is not JSON")
    if isinstance(doc, dict):
        pairs = list(doc.items())
    elif isinstance(doc, list):
        pairs = [(i.get("number"), i.get("title")) for i in doc if isinstance(i, dict)]
    else:
        raise HtmlError(2, "dag html: the titles file must be an object or a list of {number, title}")
    out = {}
    for k, v in pairs:
        try:
            n = int(str(k).lstrip("#"))
        except ValueError:
            continue
        if isinstance(v, str) and v.strip():
            out[n] = clean_title(v)
    return out


def check_out(out_dir):
    """Refuse an output directory inside a git checkout that does not ignore it. Local git only; no checkout, or
    no git, means there is nothing tracked to dirty."""
    probe = pathlib.Path(os.path.realpath(str(out_dir)))
    anchor = probe
    while not anchor.exists() and anchor != anchor.parent:
        anchor = anchor.parent
    try:
        top = subprocess.run(["git", "-C", str(anchor), "rev-parse", "--show-toplevel"], capture_output=True,
                             text=True, timeout=20)
        if top.returncode != 0:
            return
        ign = subprocess.run(["git", "-C", top.stdout.strip(), "check-ignore", "-q", "--", str(probe / "index.html")],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return
    if ign.returncode != 0:
        raise HtmlError(2, "dag html: %s is inside a git checkout and is not ignored; pages would dirty it. Use the "
                           "default under .sdlc/state/ (add `.sdlc/state/` to .gitignore) or a path outside the "
                           "repository" % out_dir)


def _open(sdlc, now):
    """`(map, mode, config, ok, why)`; raises HtmlError 4 (off) or 3 (no readable map). A map that is merely not
    fresh is returned with `ok` False and the reason: the page is a view and says so."""
    config = dag_chain._config(sdlc)
    md = dag_store.mode(config)
    if md == "off":
        raise HtmlError(EXIT["off"], "dag: off")
    m, why = dag_store.load(sdlc, config)
    if m is None:
        raise HtmlError(EXIT["unusable"], "dag: map unusable (%s)" % why)
    ok, reason = dag_store.usable(m, now, dag_store.ttl_minutes(config))
    return m, md, config, ok, reason


def _age(seconds):
    s = max(0, int(seconds))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size:
            return "%d%s" % (s // size, unit)
    return "%ds" % s


# ------------------------------------------------------------------ the model of one chain

def _marker_text(marker, after):
    if marker != "after":
        return marker
    return "after " + ", ".join("#%d" % a for a in after[:2]) + (" +%d" % (len(after) - 2) if len(after) > 2 else "")


def build(m, config, goal, titles=None, now=None, fresh=(True, "ok")):
    """The model of the chain for `goal`: nodes, edges, the forecast and the header facts. Raises HtmlError 2 when
    `goal` is not in the map, is closed, or is a pull request."""
    now = time.time() if now is None else now
    titles = titles or {}
    if goal not in {n["number"] for n in m["nodes"]}:
        raise HtmlError(EXIT["refused"], "dag: goal %d is not in the map" % goal)
    try:
        out, g, esrc, body = chain.map_closure(m, goal, strict=False, now=now)
    except chain.ChainRefused as exc:
        raise HtmlError(EXIT["refused"], str(exc))
    if g.nodes[goal].kind != "issue":
        raise HtmlError(EXIT["refused"], "dag: #%d is a pull request, not a goal" % goal)
    fc = chain_forecast.forecast(out, config)
    marks = {x["number"]: x for x in fc["members"]}
    amap = {n["number"]: n["assignees"] for n in m["nodes"] if isinstance(n.get("assignees"), list)}
    nodes = {}
    for r in out["members"]:
        mk = marks[r["number"]]
        nodes[r["number"]] = {"n": r["number"], "m": mk["marker"], "after": mk["after"], "why": mk["why"],
                              "life": r["life"], "prio": r["prio"], "unit": r.get("unit"),
                              "depth": r["depth"],
                              "who": sorted({_clean_login(a) for a in amap[r["number"]]}) if r["number"] in amap else None}
    for n in out["done"]:
        nodes[n] = {"n": n, "m": "closed", "after": [], "why": "resolved", "life": None,
                    "prio": g.nodes[n].prio, "unit": None, "depth": 0}
    for e in out["externals"]:
        n = int(e["ref"].lstrip("#"))
        nodes[n] = {"n": n, "m": "outside", "after": [], "why": "an open pull request, outside the chain's goals",
                    "life": None, "prio": None, "unit": None, "depth": 0}
    for c in body["cycles"]:
        for n in c:
            if n not in nodes:
                nodes[n] = {"n": n, "m": "cycle", "after": [], "why": "in a dependency cycle; the plan refuses it",
                            "life": None, "prio": g.nodes[n].prio, "unit": None, "depth": 0}
    drawn = set(nodes)
    # The work that waits on the goal: open issues only, nearest first (ties by number), at most DEPENDENT_CAP drawn.
    dist, level, order = {goal: 0}, [goal], []
    while level:
        nxt = sorted({d for c in level for d in g.dependents[c]
                      if d not in dist and d not in drawn and g.nodes[d].kind == "issue" and g.nodes[d].state == "open"})
        for d in nxt:
            dist[d] = dist[level[0]] + 1
        order += nxt
        level = nxt
    waiting, left_out = len(order), max(0, len(order) - DEPENDENT_CAP)
    for n in order[:DEPENDENT_CAP]:
        nodes[n] = {"n": n, "m": "waiting", "k": "waiting", "after": [], "why": "waits on #%d, directly or through "
                    "another issue" % goal, "life": g.nodes[n].life, "prio": g.nodes[n].prio, "unit": None,
                    "depth": nodes[goal]["depth"] + dist[n]}
    holders = {goal} | {n for n, d in nodes.items() if d["m"] == "waiting"}
    drawn = set(nodes)
    edges = sorted({(a, b): "holds" if a in holders and nodes[b]["m"] == "waiting"
                    else "explicit" if esrc.get((a, b)) == "body" else "ledger"
                    for a, b, _s in g.edges if a in drawn and b in drawn
                    and not (nodes[b]["m"] == "waiting" and a not in holders)}.items())
    waits, frees = collections.defaultdict(list), collections.defaultdict(list)
    for (a, b), _t in edges:
        waits[b].append(a)
        frees[a].append(b)
    for n, d in nodes.items():
        d["waits"], d["frees"] = sorted(waits[n]), sorted(frees[n])
        d["t"] = titles.get(n)
    open_rows = [d for d in nodes.values() if d["m"] not in ("closed", "outside", "cycle", "waiting")]
    stops = [{"n": s["number"], "m": s["marker"], "why": s["why"]} for s in fc["stops"]]
    ranked = [d["prio"] for d in open_rows if d["prio"]]
    top = min(ranked, key=chain._rank) if ranked else ""
    who = sorted({UNKNOWN if d["who"] is None else UNASSIGNED if not d["who"] else a
                  for d in open_rows for a in (d["who"] or [None])})
    return {"goal": goal, "nodes": nodes, "edges": [(a, b, t) for (a, b), t in edges], "stops": stops,
            "forecast": fc["line"], "notes": fc["notes"], "reason": fc["reason"], "finish": len(fc["finish"]),
            "waiting": waiting, "left_out": left_out, "open": len(open_rows), "closed": len(out["done"]), "outside": len(out["externals"]),
            "cycles": body["cycles"], "depth": max([d["depth"] for d in open_rows] or [0]),
            "decisions": sum(1 for d in open_rows if d["m"] == "needs decision"), "top": top, "who": who,
            "over_cap": len(open_rows) > MEMBER_CAP, "titled": bool(titles),
            "built_at": out["map_built_at"], "age": out["map_age_seconds"], "fresh": fresh, "mode": None}


# ------------------------------------------------------------------ the layout

def _inversions(ys):
    """Pairs i < j with ys[i] > ys[j] (a Fenwick tree over the ranks)."""
    rank = {v: i + 1 for i, v in enumerate(sorted(set(ys)))}
    tree, total = [0] * (len(rank) + 1), 0
    for seen, y in enumerate(ys):
        r, below = rank[y], 0
        i = r
        while i:
            below += tree[i]
            i -= i & -i
        total += seen - below
        i = r
        while i <= len(rank):
            tree[i] += 1
            i += i & -i
    return total


def crossings(layers, segs):
    """Edge crossings between adjacent columns for the column order `layers`. `segs` is [(u, v)] with v one
    column right of u. Segments sharing an end do not cross."""
    pos = {k: i for layer in layers for i, k in enumerate(layer)}
    col = {k: c for c, layer in enumerate(layers) for k in layer}
    by = collections.defaultdict(list)
    for u, v in segs:
        by[col[u]].append((pos[u], pos[v]))
    total = 0
    for pairs in by.values():
        pairs.sort()
        total += _inversions([y for _x, y in pairs])
    return total


def _barycentre(layers, nbrs, c, pos):
    def key(item):
        i, k = item
        ns = [pos[x] for x in nbrs.get(k, ())]
        return (sum(ns) / len(ns) if ns else float(i), i)
    return [k for _i, k in sorted(enumerate(layers[c]), key=key)]


def layout(cols, edges, rank, sweeps=None):
    """Place the nodes. `cols` maps number -> column, `edges` is [(blocker, blocked)] and `rank` maps number ->
    priority rank (the starting order within a column). -> {layers, segs, paths (edge -> list of keys), width,
    height, xy (key -> (x, y, h)), crossings, direct (long edges drawn direct)}. A key is a number or a dummy
    slot `("d", blocker, blocked, column)`. An edge that does not run left to right (a cycle's) is `direct`."""
    ncol = max(cols.values(), default=-1) + 1
    layers = [sorted((n for n, c in cols.items() if c == i), key=lambda n: (rank.get(n, 0), n))
              for i in range(ncol)]
    paths, direct = {}, []
    forward = [(a, b) for a, b in edges if cols[a] < cols[b]]
    direct += [(a, b) for a, b in edges if cols[a] >= cols[b]]
    slots = sum(cols[b] - cols[a] - 1 for a, b in forward)
    simple = slots > DUMMY_CAP
    segs = []
    for a, b in forward:
        if simple and cols[b] - cols[a] > 1:
            direct.append((a, b))
            continue
        ks = [a] + [("d", a, b, c) for c in range(cols[a] + 1, cols[b])] + [b]
        for i, k in enumerate(ks[1:-1], 1):
            layers[cols[a] + i].append(k)
        paths[(a, b)] = ks
        segs += list(zip(ks, ks[1:]))
    succ, pred = collections.defaultdict(list), collections.defaultdict(list)
    for u, v in segs:
        succ[u].append(v)
        pred[v].append(u)
    best, best_c = [list(x) for x in layers], crossings(layers, segs)
    for _ in range(sweeps if sweeps is not None else (SWEEPS_BIG if len(segs) > BIG_SEGMENTS else SWEEPS)):
        for c in range(1, ncol):
            pos = {k: i for i, k in enumerate(layers[c - 1])}
            layers[c] = _barycentre(layers, pred, c, pos)
        for c in range(ncol - 2, -1, -1):
            pos = {k: i for i, k in enumerate(layers[c + 1])}
            layers[c] = _barycentre(layers, succ, c, pos)
        now_c = crossings(layers, segs)
        if now_c < best_c:
            best, best_c = [list(x) for x in layers], now_c
    layers = best
    heights = [sum(NODE_H if isinstance(k, int) else DUMMY_H for k in lay) + GAP_Y * max(0, len(lay) - 1)
               for lay in layers]
    tall = max(heights, default=0)
    xy = {}
    for c, lay in enumerate(layers):
        y = MARGIN + (tall - heights[c]) / 2
        for k in lay:
            h = NODE_H if isinstance(k, int) else DUMMY_H
            xy[k] = (MARGIN + c * (NODE_W + COL_GAP), y, h)
            y += h + GAP_Y
    return {"layers": layers, "segs": segs, "paths": paths, "direct": direct, "xy": xy, "crossings": best_c,
            "width": 2 * MARGIN + ncol * NODE_W + max(0, ncol - 1) * COL_GAP, "height": 2 * MARGIN + tall}


def _edge_d(layout_, a, b):
    """The SVG path of one edge: through its dummy slots, with horizontal tangents."""
    xy = layout_["xy"]
    ks = layout_["paths"].get((a, b))
    if ks is None:
        ax, ay, ah = xy[a]
        bx, by, bh = xy[b]
        x0, y0, x1, y1 = ax + NODE_W, ay + ah / 2, bx, by + bh / 2
        dx = max(60.0, abs(x1 - x0) / 2)
        return "M%.1f %.1f C%.1f %.1f %.1f %.1f %.1f %.1f" % (x0, y0, x0 + dx, y0, x1 - dx, y1, x1, y1)
    pts = []
    for i, k in enumerate(ks):
        x, y, h = xy[k]
        if isinstance(k, int):
            pts.append((x + NODE_W if i == 0 else x, y + h / 2))
        else:
            pts.append((x + NODE_W / 2, y + h / 2))
    d = "M%.1f %.1f" % pts[0]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        dx = (x1 - x0) / 2
        d += " C%.1f %.1f %.1f %.1f %.1f %.1f" % (x0 + dx, y0, x1 - dx, y1, x1, y1)
    return d


# ------------------------------------------------------------------ the page

_THEME = {
    "light": {"bg": "#fbfbfa", "panel": "#ffffff", "ink": "#1b1f23", "muted": "#5b6168", "line": "#d6d8da",
              "edge": "#6b7076", "accent": "#2457c5",
              "ready": ("#dcf3e3", "#2f8f4e"), "after": ("#e3ebfa", "#3b6fc4"), "decision": ("#fdecc8", "#b7791f"),
              "else": ("#eadcf7", "#7a4cb0"), "check": ("#d5f0f0", "#1f8a8a"), "stuck": ("#fadadd", "#c0392b"),
              "closed": ("#eeeeec", "#9a9a96"), "outside": ("#fbfbfa", "#8a8a86"), "cycle": ("#fadadd", "#8e1f14"),
              "paid": ("#f9dcec", "#c2358a"), "parked": ("#dde3ea", "#5a6b7d"), "notgoal": ("#efe2d2", "#8a5a2b"),
              "awaiting": ("#eaf0c8", "#7a8a1c")},
    "dark": {"bg": "#14171a", "panel": "#1b1f23", "ink": "#e6e8ea", "muted": "#a0a5aa", "line": "#31363b",
             "edge": "#9aa0a6", "accent": "#7fa6f2",
             "ready": ("#16361f", "#4fbf74"), "after": ("#1b2a48", "#6c9be8"), "decision": ("#40300f", "#e0a53a"),
             "else": ("#2e2044", "#b18be0"), "check": ("#0f3636", "#4fc3c3"), "stuck": ("#451a1f", "#ef7a70"),
             "closed": ("#25282b", "#6d7277"), "outside": ("#14171a", "#7a7f84"), "cycle": ("#451a1f", "#ff9a8f"),
             "paid": ("#43182f", "#e57bb5"), "parked": ("#262d35", "#8fa1b4"), "notgoal": ("#3b2b1a", "#d0a070"),
             "awaiting": ("#323a12", "#b8c74a")},
}


def _vars(name):
    t = _THEME[name]
    out = ["--%s:%s" % (k, v) for k, v in t.items() if isinstance(v, str)]
    out += ["--%s-f:%s;--%s-s:%s" % (k, v[0], k, v[1]) for k, v in t.items() if isinstance(v, tuple)]
    return ";".join(out)


CSS = """
:root{%(light)s;color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){%(dark)s;color-scheme:dark}}
:root[data-theme=dark]{%(dark)s;color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
a{color:var(--accent)}
header.top{padding:18px 24px 12px;border-bottom:1px solid var(--line)}
h1{margin:0 0 4px;font-size:21px;font-weight:650}
h1 small{font-weight:400;color:var(--muted);font-size:14px}
.facts{display:flex;flex-wrap:wrap;gap:6px 22px;color:var(--muted);margin:4px 0 8px}
.facts b{color:var(--ink);font-weight:600}
.held,.fc{margin:4px 0}
.notice{margin:8px 0;padding:8px 12px;border-left:4px solid var(--decision-s);background:var(--decision-f);border-radius:4px}
.legend{display:flex;flex-wrap:wrap;gap:6px 16px;margin-top:10px;font-size:12.5px;color:var(--muted)}
.legend span{display:inline-flex;align-items:center;gap:6px}
.sw{width:14px;height:14px;border-radius:3px;border:2px solid;display:inline-block}
.ln{width:26px;height:0;border-top:2px solid var(--edge);display:inline-block}
.ln.unconfirmed{border-top-style:dashed}.ln.ledger{border-top-style:dotted}
main{display:grid;grid-template-columns:minmax(0,1fr) 330px;min-height:68vh}
#stage{position:relative;overflow:hidden;border-right:1px solid var(--line)}
#g{width:100%%;height:72vh;min-height:420px;display:block;cursor:grab;touch-action:none}
#g.drag{cursor:grabbing}
#tools{position:absolute;top:10px;left:10px;display:flex;gap:6px;z-index:2}
#tools label{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:3px 10px;display:flex;align-items:center;gap:6px}
button{font:inherit;background:var(--panel);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:3px 10px;cursor:pointer}
button:hover{border-color:var(--accent)}
aside{padding:16px 18px;background:var(--panel);overflow:auto;max-height:72vh}
aside h2{font-size:16px;margin:0 0 6px}
dl{margin:10px 0 0}dt{color:var(--muted);font-size:12px;margin-top:10px;text-transform:uppercase;letter-spacing:.04em}
dd{margin:2px 0 0}
dd button{display:block;width:100%%;text-align:left;margin:3px 0}
.n rect{fill:var(--c-f);stroke:var(--c-s);stroke-width:1.6;rx:7}
.n text{fill:var(--ink);font-size:12px;pointer-events:none}
.n .num{font-weight:700;font-size:13.5px}.n .mk{fill:var(--muted);font-size:10.5px;font-weight:400}
.n{cursor:pointer;outline:none}.n:hover rect,.n:focus rect{stroke-width:2.6}
.n.sel rect{stroke-width:3.4;stroke:var(--accent)}
.n.goal rect{stroke-width:2.6}
.n .tag{fill:var(--accent);font-size:10px;font-weight:700}
.c-ready{--c-f:var(--ready-f);--c-s:var(--ready-s)}.c-after{--c-f:var(--after-f);--c-s:var(--after-s)}
.c-decision{--c-f:var(--decision-f);--c-s:var(--decision-s)}.c-else{--c-f:var(--else-f);--c-s:var(--else-s)}
.c-check{--c-f:var(--check-f);--c-s:var(--check-s)}.c-stuck{--c-f:var(--stuck-f);--c-s:var(--stuck-s)}
.c-closed{--c-f:var(--closed-f);--c-s:var(--closed-s)}.c-closed text{fill:var(--muted)}
.c-outside{--c-f:var(--outside-f);--c-s:var(--outside-s)}.c-outside rect{stroke-dasharray:5 4}
.c-cycle{--c-f:var(--cycle-f);--c-s:var(--cycle-s)}.c-cycle rect{stroke-dasharray:2 3}
.c-paid{--c-f:var(--paid-f);--c-s:var(--paid-s)}.c-parked{--c-f:var(--parked-f);--c-s:var(--parked-s)}
.c-notgoal{--c-f:var(--notgoal-f);--c-s:var(--notgoal-s)}.c-awaiting{--c-f:var(--awaiting-f);--c-s:var(--awaiting-s)}
.c-waiting{--c-f:var(--closed-f);--c-s:var(--closed-s)}.c-waiting rect{stroke-dasharray:3 3}.n.c-waiting{opacity:.62}
.e.holds{stroke-dasharray:1 5;opacity:.45}
.sw.c-outside{border-style:dashed}.sw{background:var(--c-f);border-color:var(--c-s)}
.e{fill:none;stroke:var(--edge);stroke-width:1.4;opacity:.7}
.e.unconfirmed{stroke-dasharray:6 4}.e.ledger{stroke-dasharray:2 4}
.e.ce,.n.cl{display:none}.show-closed .e.ce,.show-closed .n.cl{display:inline}
.e.hot{stroke:var(--accent);stroke-width:2.6;opacity:1}.e.cool{opacity:.14}
#arr path{fill:var(--edge)}
.view{display:flex;flex-wrap:wrap;gap:8px 18px;margin:12px 24px 0;color:var(--muted)}
table{border-collapse:collapse;margin:16px 24px;width:calc(100%% - 48px)}
th,td{text-align:left;padding:7px 12px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:12.5px}td.num{text-align:right;font-variant-numeric:tabular-nums}
.hint{font-size:12.5px}
main.plan{grid-template-columns:minmax(0,1fr) 420px}
#qs h2{font-size:16px;margin:0 0 2px}#qs .sub{margin:0 0 8px;color:var(--muted);font-size:12.5px}
#qs ul{list-style:none;margin:0 0 14px;padding:0}
.q{padding:7px 9px;margin:0 0 6px;border:1px solid var(--line);border-left:4px solid var(--decision-s);border-radius:6px;cursor:pointer;font-size:13px}
.q b{font-weight:600;display:block}.q span{display:block;color:var(--muted);font-size:12px;margin-top:2px}
.q.answered{border-left-color:var(--ready-s)}.q.answered b{color:var(--muted)}
.q.on{background:var(--decision-f);border-color:var(--accent);border-left-color:var(--accent)}
.q:focus-visible{outline:2px solid var(--accent)}
.qh{margin:10px 0 6px;color:var(--muted);font-size:12.5px;font-weight:600}
.n .qb{cursor:pointer}.n .qb circle{fill:var(--accent);stroke:var(--bg);stroke-width:1.5}
.n .qb text{fill:var(--bg);font-size:10.5px;font-weight:700;text-anchor:middle;pointer-events:none}
.n .qb.done circle{fill:var(--closed-s)}.n .qb.on circle{stroke:var(--ink);stroke-width:2.5}
.n .qm{fill:var(--muted);font-size:10.5px}
.n.qhi rect{stroke:var(--accent);stroke-width:3.4}
.sheethead div{margin:1px 0}.more{margin:6px 24px 24px}.more summary{cursor:pointer;font-weight:600;margin:8px 0}
.more pre{margin:6px 0 12px;padding:10px 12px;border:1px solid var(--line);border-radius:6px;background:var(--panel);
overflow:auto;white-space:pre-wrap;font:12.5px/1.45 ui-monospace,Menlo,monospace}
@media (max-width:900px){main,main.plan{grid-template-columns:1fr}aside{max-height:none}#stage{border-right:0}}
@media print{#tools,aside{display:none}main{display:block}}
"""

JS = r"""
(function(){
var D=JSON.parse(document.getElementById('data').textContent),svg=document.getElementById('g');
var head=document.getElementById('p-head'),body=document.getElementById('p-body'),hint=document.getElementById('p-hint');
var base=svg.viewBox.baseVal,B={x:base.x,y:base.y,w:base.width,h:base.height},V={x:B.x,y:B.y,w:B.w,h:B.h},sel=null;
function apply(){svg.setAttribute('viewBox',V.x+' '+V.y+' '+V.w+' '+V.h);}
function px(){var m=svg.getScreenCTM();return m?m.a:1;}
function zoom(f,cx,cy){var w=Math.min(B.w*4,Math.max(B.w/20,V.w/f)),h=V.h*w/V.w;
 if(cx===undefined){cx=V.x+V.w/2;cy=V.y+V.h/2;}
 V.x=cx-(cx-V.x)*w/V.w;V.y=cy-(cy-V.y)*h/V.h;V.w=w;V.h=h;apply();}
function fit(){V={x:B.x,y:B.y,w:B.w,h:B.h};apply();}
var cl=document.getElementById('zcl'),HID={x:B.x,y:B.y,w:B.w,h:B.h};
if(cl){cl.onchange=function(){var a=svg.dataset.closed.split(' ').map(Number);
 svg.classList.toggle('show-closed',cl.checked);B=cl.checked?{x:a[0],y:a[1],w:a[2],h:a[3]}:HID;fit();};}
function showClosed(){if(cl&&!cl.checked){cl.checked=true;cl.onchange();}}
document.getElementById('zin').onclick=function(){zoom(1.3);};
document.getElementById('zout').onclick=function(){zoom(1/1.3);};
document.getElementById('zfit').onclick=fit;
document.getElementById('zth').onclick=function(){var r=document.documentElement,
 d=r.getAttribute('data-theme')==='dark'||(!r.getAttribute('data-theme')&&matchMedia('(prefers-color-scheme: dark)').matches);
 r.setAttribute('data-theme',d?'light':'dark');};
svg.addEventListener('wheel',function(e){e.preventDefault();var m=svg.getScreenCTM().inverse(),p=svg.createSVGPoint();
 p.x=e.clientX;p.y=e.clientY;p=p.matrixTransform(m);zoom(e.deltaY<0?1.15:1/1.15,p.x,p.y);},{passive:false});
var drag=null;
svg.addEventListener('pointerdown',function(e){if(e.target.closest('.n'))return;drag={x:e.clientX,y:e.clientY};
 svg.classList.add('drag');svg.setPointerCapture(e.pointerId);});
svg.addEventListener('pointermove',function(e){if(!drag)return;var k=px();V.x-=(e.clientX-drag.x)/k;V.y-=(e.clientY-drag.y)/k;
 drag={x:e.clientX,y:e.clientY};apply();});
function end(){drag=null;svg.classList.remove('drag');}
svg.addEventListener('pointerup',end);svg.addEventListener('pointercancel',end);
var Q=D.questions||[],QI={},qel={};
Q.forEach(function(q){QI[q.n]=q;});
document.querySelectorAll('#qs .q').forEach(function(li){qel[+li.dataset.q]=li;});
function label(n){return D.nodes[n].label||('#'+n);}
function name(n){var d=D.nodes[n];return label(n)+(d.t?' '+d.t:'');}
function btn(n,tag){var b=document.createElement('button');
 b.textContent=name(n)+' ('+D.nodes[n].mk+')'+(tag&&tag!=='explicit'?' \u00b7 '+tag:'');
 b.onclick=function(){select(n,true);};return b;}
function row(k,v){var t=document.createElement('dt'),d=document.createElement('dd');t.textContent=k;
 if(v&&v.nodeType){d.appendChild(v);}else{d.textContent=v;}body.appendChild(t);body.appendChild(d);}
function list(ns,edge){var f=document.createElement('div');if(!ns.length){f.textContent='nothing in this chain';}
 ns.forEach(function(n){f.appendChild(btn(n,(D.tags||{})[edge(n)]));});return f;}
function clearQ(){Object.keys(qel).forEach(function(k){qel[k].classList.remove('on');});
 svg.querySelectorAll('.n.qhi').forEach(function(g){g.classList.remove('qhi');});
 svg.querySelectorAll('.qb.on').forEach(function(g){g.classList.remove('on');});}
function markQ(nums){clearQ();var first=null;
 nums.forEach(function(k){var li=qel[k];if(li){li.classList.add('on');if(!first)first=li;}
  svg.querySelectorAll('.qb[data-q="'+k+'"]').forEach(function(g){g.classList.add('on');});
  (QI[k]?QI[k].nodes:[]).forEach(function(n){var g=svg.querySelector('.n[data-n="'+n+'"]');if(g)g.classList.add('qhi');});});
 if(first&&first.scrollIntoView)first.scrollIntoView({block:'nearest'});}
function about(n){return Q.filter(function(q){return q.nodes.indexOf(n)>=0;}).map(function(q){return q.n;});}
function select(n,pan,only){
 if(sel!==null){var o=svg.querySelector('.n[data-n="'+sel+'"]');if(o)o.classList.remove('sel');}
 svg.querySelectorAll('.e').forEach(function(e){e.classList.remove('hot','cool');});
 if(n===null){sel=null;head.textContent='Select a member';hint.hidden=false;body.textContent='';clearQ();return;}
 sel=n;var d=D.nodes[n],el=svg.querySelector('.n[data-n="'+n+'"]');
 if(el&&el.classList.contains('cl'))showClosed();
 if(el)el.classList.add('sel');
 svg.querySelectorAll('.e').forEach(function(e){var a=+e.dataset.a,b=+e.dataset.b;
  e.classList.add(a===n||b===n?'hot':'cool');});
 head.textContent=name(n);hint.hidden=true;body.textContent='';
 row('State',d.mk);row('Why',d.why||'nothing holds it');
 (d.ex||[]).forEach(function(r){row(r[0],r[1]);});
 if('who' in d)row('Assignees',d.who===null?'assignee unknown (map predates this field)':d.who.length?d.who.join(', '):'unassigned');
 if(d.life)row('Life',d.life);if(d.prio)row('Priority',d.prio);if(d.unit)row('Unit',d.unit);
 var qs=about(n);if(qs.length)row('Questions','number '+qs.join(', ')+' in the list');
 row('Waits on',list(d.waits,function(m){return m+'>'+n;}));row('Frees',list(d.frees,function(m){return n+'>'+m;}));
 if(n===D.goal)row('Role','the end of this chain');
 markQ(only?[only]:qs);
 if(pan&&el){var k=px();if(k<0.9){V.w=V.w*k/0.9;V.h=V.h*k/0.9;}
  var r=el.getBBox();V.x=r.x+r.width/2-V.w/2;V.y=r.y+r.height/2-V.h/2;apply();}}
function selectQ(k){var q=QI[k];if(!q)return;
 if(q.nodes.length){select(q.nodes[0],true,k);}else{select(null,false);markQ([k]);}}
svg.querySelectorAll('.n').forEach(function(g){var n=+g.dataset.n;
 g.addEventListener('click',function(e){e.stopPropagation();select(sel===n?null:n,false);});
 g.addEventListener('keydown',function(e){if(e.key==='Enter'||e.key===' '){e.preventDefault();select(n,false);}});});
svg.querySelectorAll('.qb').forEach(function(g){
 g.addEventListener('click',function(e){e.stopPropagation();selectQ(+g.dataset.q);});});
Object.keys(qel).forEach(function(k){var li=qel[k];li.onclick=function(){selectQ(+k);};
 li.addEventListener('keydown',function(e){if(e.key==='Enter'||e.key===' '){e.preventDefault();selectQ(+k);}});});
document.addEventListener('keydown',function(e){if(e.key==='Escape')select(null,false);});
fit();
})();
"""


def _esc(s):
    return html.escape(str(s), quote=True)


def _shown(d):
    """The word a node carries: a plan page's own state word, else the forecast marker."""
    return d.get("word") or _marker_text(d["m"], d["after"])


def _data_json(model):
    nodes = {}
    for n, d in model["nodes"].items():
        nodes[str(n)] = {"t": d["t"], "mk": _shown(d), "why": d["why"], "life": d["life"], "prio": d["prio"],
                         "unit": d["unit"], "waits": d["waits"], "frees": d["frees"], "label": d.get("label"),
                         "ex": d.get("ex") or []}
        if "who" in d:                     # members only; null is "unknown", [] is "unassigned"
            nodes[str(n)]["who"] = d["who"]
    tags = {"%d>%d" % (a, b): t for a, b, t in model["edges"] if t != "explicit"}
    doc = {"goal": model["goal"], "nodes": nodes, "tags": tags,
           "questions": [{"n": q["n"], "id": q["ident"], "nodes": q["nodes"]} for q in model.get("questions") or []]}
    return json.dumps(doc, sort_keys=True).replace("<", "\\u003c")


def _lines(text, width, count):
    return textwrap.wrap(text, width, max_lines=count, placeholder="\u2026") if text else []


def _place_closed(model, xy, left):
    """Put the closed prerequisites in a column at `left`, each near the mean height of what it frees (the one
    ordering that keeps its edges short); -> the height the column needs."""
    nodes = model["nodes"]
    mid = lambda n: xy[n][1] + xy[n][2] / 2         # noqa: E731
    shut = sorted((n for n, d in nodes.items() if d["m"] == "closed"),
                  key=lambda n: (sum(mid(f) for f in nodes[n]["frees"]) / max(1, len(nodes[n]["frees"])), n))
    y = MARGIN
    for n in shut:
        want = sum(mid(f) for f in nodes[n]["frees"]) / max(1, len(nodes[n]["frees"])) - NODE_H / 2
        y = max(y, want)
        xy[n] = (left, y, NODE_H)
        y += NODE_H + GAP_Y
    return y + MARGIN


def _badges_svg(n, x, y, badges):
    """The numbered question badges of one node, along its top edge, left to right."""
    out = []
    for i, (q, done) in enumerate(badges[:BADGE_MAX]):
        cx, cy = x + 14 + i * (2 * BADGE_R + 3), y - 3
        out.append('<g class="qb%s" data-q="%d" data-n="%d"><title>Question %d</title><circle cx="%.1f" cy="%.1f" '
                   'r="%d"/><text x="%.1f" y="%.1f">%d</text></g>'
                   % (" done" if done else "", q, n, q, cx, cy, BADGE_R, cx, cy + 3.8, q))
    if len(badges) > BADGE_MAX:
        out.append('<text class="qm" x="%.1f" y="%.1f">+%d</text>'
                   % (x + 14 + BADGE_MAX * (2 * BADGE_R + 3) - BADGE_R, y, len(badges) - BADGE_MAX))
    return out


def _node_svg(d, goal, xy, closed_blockers=0, badges=()):
    x, y, _h = xy
    cls = d.get("k") or CLASS[d["m"]]
    shown = _shown(d)
    label = d.get("label") or "#%d" % d["n"]
    lines = _lines(d["t"] or "", 31, 2) or [" ".join(p for p in (d["life"], d["prio"], d["unit"]) if p)[:36]]
    out = ['<g class="n c-%s%s%s" data-n="%d" tabindex="0" role="button" aria-label="%s">'
           % (cls, " goal" if d["n"] == goal else "", " cl" if d["m"] == "closed" else "", d["n"], _esc("%s, %s" % (label, shown))),
           "<title>%s</title>" % _esc("%s %s (%s)" % (label, d["t"] or "", shown)),
           '<rect x="%.1f" y="%.1f" width="%d" height="%d"/>' % (x, y, NODE_W, NODE_H),
           '<text class="num" x="%.1f" y="%.1f">%s<tspan class="mk" dx="6"> %s</tspan></text>'
           % (x + 10, y + 18, _esc(label), _esc(shown))]
    if closed_blockers:
        out.append('<text class="mk" x="%.1f" y="%.1f" text-anchor="end">\u2713 %d closed</text>'
                   % (x + NODE_W - 9, y + NODE_H - 6, closed_blockers))
    if d["n"] == goal:
        out.append('<text class="tag" x="%.1f" y="%.1f" text-anchor="end">GOAL</text>' % (x + NODE_W - 9, y + 18))
    for i, ln in enumerate(lines):
        out.append('<text x="%.1f" y="%.1f">%s</text>' % (x + 10, y + 34 + 14 * i, _esc(ln)))
    if d.get("who") is not None and "who" in d and d["m"] not in ("closed", "outside", "cycle"):
        line = ", ".join(d["who"]) or "unassigned"
        out.append('<text class="mk" x="%.1f" y="%.1f">%s</text>'
                   % (x + 10, y + 66, _esc(line if len(line) <= 24 else line[:23] + "\u2026")))
    out += _badges_svg(d["n"], x, y, list(badges))
    out.append("</g>")
    return "".join(out)


def _legend(model):
    """The colour and edge legend: a state is listed when a node carries it (the first three always)."""
    counts = collections.Counter(d.get("word") or d["m"] for d in model["nodes"].values())
    text = dict(LEGEND, **model.get("legend_text", {}))
    out = "".join('<span><i class="sw c-%s"></i>%s (%d): %s</span>' % (CLASS[k], _esc(k), counts[k], _esc(text[k]))
                  for k, _v in LEGEND if counts[k] or k in ("ready now", "after", "needs decision"))
    if counts["waiting"]:
        out += '<span><i class="sw c-waiting"></i>waiting (%d): open work that waits on the goal</span>' % counts["waiting"]
    edges = EDGES + ((("holds", "waits on the goal (drawn muted)"),) if counts["waiting"] else ())
    return out + "".join('<span><i class="ln %s"></i>%s: %s</span>' % (k, k, v) for k, v in edges)


MAP_HINT = ("Waits written in prose or comments are not in the map, so they are not drawn here. "
            "`chain.py html <sdlc_dir> <goal>` draws the whole plan, with its questions.")


def _header(model, repo):
    fc_notes = "".join('<div class="fc">%s</div>' % _esc(n) for n in model["notes"])
    stops = model["stops"]
    if stops:
        shown = "; ".join("#%d %s (%s)" % (s["n"], s["m"], s["why"]) for s in stops[:6])
        held = "Held by: %s%s." % (shown, " and %d more" % (len(stops) - 6) if len(stops) > 6 else "")
    else:
        held = "Nothing holds it: every member can be worked, in order."
    notices = []
    if model["over_cap"]:
        notices.append("This chain has %d open members, over the page cap of %d. It is written in full and may be "
                       "hard to read; plan a nearer goal with --goal." % (model["open"], MEMBER_CAP))
    if model["cycles"]:
        notices.append("Dependency cycle among %s: the plan refuses this chain until one blocker line is dropped. "
                       "The cycle members are drawn in the first column." %
                       ", ".join("#%d" % n for c in model["cycles"] for n in c))
    ok, why = model["fresh"]
    if not ok:
        notices.append("The map is not fresh (%s); this page shows it as stored, age %s. Refresh it with dag.py "
                       "refresh --force." % (why, _age(model["age"]) if model["age"] is not None else "unknown"))
    if not model["titled"]:
        notices.append("No titles were given (the map holds none), so members are named by number. Pass "
                       "--titles FILE to add them.")
    if model.get("left_out"):
        notices.append("%d more open issues wait on #%d and are not drawn (cap %d)."
                       % (model["left_out"], model["goal"], DEPENDENT_CAP))
    if model.get("simple"):
        notices.append("Layout simplified: this chain is too large for channel routing, so long edges are drawn "
                       "straight and may cross nodes.")
    legend = _legend(model)
    goal = model["nodes"][model["goal"]]
    facts = ("<span><b>%d</b> open members</span><span>depth <b>%d</b></span><span><b>%d</b> closed prerequisites"
             "</span><span><b>%d</b> need a decision</span>%s<span>map built <b>%s</b>, age <b>%s</b></span>"
             % (model["open"], model["depth"], model["closed"], model["decisions"],
                "<span><b>%d</b> open issues wait on it</span>" % model["waiting"] if model.get("waiting") else "",
                _esc(_stamp(model["built_at"])), _age(model["age"]) if model["age"] is not None else "unknown"))
    return ('<header class="top"><h1>Chain for #%d%s <small>%s</small></h1><div class="facts">%s</div>'
            '<div class="held">%s</div><div class="fc">%s</div>%s<div class="hint">%s</div>%s<div class="legend">%s</div>'
            '</header>'
            % (model["goal"], " " + _esc(goal["t"]) if goal.get("t") else "", _esc(repo or ""), facts,
               _esc(held), _esc(model["forecast"]) + ". Assumes every goal it claims merges; the cap is not modelled.",
               fc_notes, _esc(MAP_HINT), "".join('<div class="notice">%s</div>' % _esc(n) for n in notices), legend))


def _stamp(epoch):
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(epoch)) if epoch else "unknown"


def render_page(model, repo=None):
    """The whole page of one chain, as text. Pure: the same model gives the same bytes."""
    nodes = model["nodes"]
    shown = {n: d for n, d in nodes.items() if d["m"] != "closed"}
    gutter = any(d["m"] in ("outside", "cycle") for d in shown.values())
    cols = {n: (0 if d["m"] in ("outside", "cycle") else d["depth"] + (1 if gutter else 0)) for n, d in shown.items()}
    rank = {n: chain._rank(d["prio"]) if d["prio"] else chain._UNPRIORITISED for n, d in shown.items()}
    lay = layout(cols, [(a, b) for a, b, _t in model["edges"] if a in shown and b in shown], rank)
    model["simple"] = any(cols[a] < cols[b] for a, b in lay["direct"])
    model["crossings"] = lay["crossings"]
    xy, width, height = dict(lay["xy"]), lay["width"], lay["height"]
    left = -(NODE_W + COL_GAP)          # closed prerequisites: a hidden column left of everything
    closed_h = _place_closed(model, xy, left)
    edges = "".join('<path class="e %s%s" data-a="%d" data-b="%d" d="%s" marker-end="url(#arr)"/>'
                    % (t, " ce" if nodes[a]["m"] == "closed" else "", a, b, _edge_d(dict(lay, xy=xy), a, b))
                    for a, b, t in model["edges"])
    ordered = sorted(nodes.values(), key=lambda d: (xy[d["n"]][0], xy[d["n"]][1], d["n"]))
    badges = model.get("badges", {})
    body = "".join(_node_svg(d, model["goal"], xy[d["n"]], sum(1 for w in d["waits"] if nodes[w]["m"] == "closed"),
                             badges.get(d["n"], ())) for d in ordered)
    with_closed = "%d 0 %d %d" % (left, width - left, max(height, closed_h))
    svg = ('<svg id="g" viewBox="0 0 %d %d" data-closed="%s" role="img" aria-label="Chain for #%d: blockers on the '
           'left, the work that waits on them on the right"><defs><marker id="arr" viewBox="0 0 10 10" refX="9" '
           'refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z"/></marker></defs>'
           '<g>%s</g><g>%s</g></svg>' % (width, height, with_closed, model["goal"], edges, body))
    css = CSS % {"light": _vars("light"), "dark": _vars("dark")}
    plan = model.get("plan")
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Chain %s#%d</title>'
            '<style>%s</style></head><body>%s<main%s><div id="stage"><div id="tools"><button id="zin" title="Zoom in">+'
            '</button><button id="zout" title="Zoom out">\u2212</button><button id="zfit" title="Show the whole '
            'chain">Fit</button><button id="zth" title="Switch light and dark">Theme</button>%s</div>%s</div>'
            '<aside>%s<h2 id="p-head">Select a member</h2><p id="p-hint">Click a member to see what it waits on, '
            'what it frees and why it is held. Drag to pan, scroll or + and \u2212 to zoom, Fit shows the whole '
            'chain.</p><dl id="p-body"></dl></aside></main>%s'
            '<script type="application/json" id="data">%s</script><script>%s</script></body></html>\n'
            % ("plan for " if plan else "for ", model["goal"], css,
               _plan_header(model, repo) if plan else _header(model, repo), ' class="plan"' if plan else "",
               '<label><input type="checkbox" id="zcl"> show %d closed</label>' % model["closed"] if model["closed"] else "",
               svg, _questions_html(model) if plan else "", _more_html(model) if plan else "", _data_json(model), JS))


INDEX_JS = r"""
function applyView(rows, sort, who, prio) {
  var rank = function(t) { var m = /^P(\d+)$/.exec(t); return m ? +m[1] : 99; };
  var out = rows.filter(function(r) { return (!who || r.who.indexOf(who) >= 0) && (!prio || r.top === prio); });
  out.sort(function(a, b) {
    var d = 0;
    if (sort === 'priority') d = rank(a.top) - rank(b.top);
    else if (sort === 'open') d = b.open - a.open;
    return d || a.goal - b.goal;
  });
  return out;
}
(function() {
  var tb = document.querySelector('tbody'), ctl = {};
  ['sort', 'who', 'prio'].forEach(function(k) { ctl[k] = document.getElementById(k); });
  var trs = [].slice.call(tb.rows).filter(function(tr) { return tr.dataset.goal; });
  var rows = trs.map(function(tr) {
    return {goal: +tr.dataset.goal, open: +tr.dataset.open, top: tr.dataset.top,
            who: tr.dataset.who.split(' ').filter(Boolean), tr: tr};
  });
  function go() {
    var v = applyView(rows, ctl.sort.value, ctl.who.value, ctl.prio.value);
    trs.forEach(function(t) { t.hidden = true; });
    v.forEach(function(r) { r.tr.hidden = false; tb.appendChild(r.tr); });
  }
  ['sort', 'who', 'prio'].forEach(function(k) { ctl[k].addEventListener('change', go); });
})();
"""


def render_index(rows, repo, stamp_built, age, fresh, titled):
    """The index page: one row per chain, linking its page."""
    notice = ""
    if not fresh[0]:
        notice += '<div class="notice">The map is not fresh (%s); the pages show it as stored.</div>' % _esc(fresh[1])
    if not titled:
        notice += '<div class="notice">No titles were given (the map holds none). Pass --titles FILE to add them.</div>'
    trs = []
    for r in rows:
        held = "; ".join("#%d %s" % (s["n"], s["m"]) for s in r["stops"][:4]) or "nothing"
        trs.append('<tr data-goal="%d" data-open="%d" data-top="%s" data-who="%s"><td><a href="%s">#%d</a>%s</td><td class="num">%d%s</td><td class="num">%d</td><td>%s</td>'
                   '<td class="num">%d</td><td>%s</td></tr>'
                   % (r["goal"], r["open"], _esc(r.get("top", "")), _esc(" ".join(r.get("who", []))),
                      _esc(r["file"]), r["goal"], " " + _esc(r["title"]) if r["title"] else "", r["open"],
                      " (over the cap)" if r["over_cap"] else "", r["depth"], _esc(held), r["decisions"],
                      _esc("%d of %d" % (r["finish"], r["open"]))))
    people = sorted({w for r in rows for w in r.get("who", [])})
    tops = sorted({r["top"] for r in rows if r.get("top")}, key=chain._rank)
    opt = lambda v, label: '<option value="%s">%s</option>' % (_esc(v), _esc(label))
    controls = ('<div class="view"><label>Sort <select id="sort">%s%s%s</select></label>'
                '<label>Assignee <select id="who">%s</select></label>'
                '<label>Priority <select id="prio">%s</select></label></div>'
                % (opt("open", "open members"), opt("priority", "priority"), opt("goal", "goal number"),
                   opt("", "any") + "".join(opt(w, w.strip("()")) for w in people),
                   opt("", "any") + "".join(opt(t, t) for t in tops)))
    css = CSS % {"light": _vars("light"), "dark": _vars("dark")}
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            'content="width=device-width,initial-scale=1"><title>Chains</title><style>%s</style></head><body>'
            '<header class="top"><h1>Chains <small>%s</small></h1><div class="facts"><span><b>%d</b> chains</span>'
            '<span>map built <b>%s</b>, age <b>%s</b></span></div>%s</header>%s<table><thead><tr><th>End goal</th>'
            '<th>Open members</th><th>Depth</th><th>Held by</th><th>Need a decision</th><th>A run can finish</th>'
            '</tr></thead><tbody>%s</tbody></table><script>%s</script></body></html>\n'
            % (css, _esc(repo or ""), len(rows), _esc(stamp_built), age, notice, controls,
               "".join(trs) or '<tr><td colspan="6">No open end goal waits on anything open.</td></tr>',
               INDEX_JS))


# ------------------------------------------------------------------ the plan page (`chain.py html`)

_ITEM = re.compile(r"^  (\d+)\. \[([^\]]+)\] ")
_SECTION = re.compile(r"^  ([A-Z][^:]*\(\d+[,)].*)$")     # a heading inside the decision block, e.g. the prose waits'
_APPLIES = re.compile(r"^applies to \d+ questions: (.*)$")


def read_sheet(text):
    """Split the printed sheet into what the plan page repeats: `head` (the lines after the title, to the first
    blank), `decisions` (the heading of the question block), `items` (each question as the sheet numbers and words
    it: number, id, first line, the lines under it, applies-to ids), `unconfirmed` and `writes` (blocks, verbatim).
    The page takes its questions from the sheet's own text, so it cannot number or word one differently; a sheet
    whose shape this does not recognise raises HtmlError 3 rather than guess."""
    lines = text.rstrip("\n").split("\n")
    try:
        i = next(k for k, ln in enumerate(lines) if ln.startswith("Decisions ("))
        j = next(k for k, ln in enumerate(lines) if ln == "Writes that follow approval:")
    except StopIteration:
        raise HtmlError(EXIT["unusable"], "chain html: the sheet has no decision block; the page cannot be built")

    def until_blank(start):
        out = []
        for ln in lines[start:]:
            if not ln.strip():
                break
            out.append(ln)
        return out
    items, section = [], None
    for ln in lines[i + 1:j]:
        if not ln.strip():
            continue
        m = _ITEM.match(ln)
        if m:
            items.append({"n": int(m.group(1)), "ident": m.group(2), "first": ln.strip(), "more": [], "ids": [],
                          "section": section})
            continue
        h = _SECTION.match(ln)
        if h:
            section = h.group(1)
        elif ln.startswith("     ") and items:
            items[-1]["more"].append(ln.strip())
            a = _APPLIES.match(ln.strip())
            if a:
                items[-1]["ids"] = [x.strip() for x in a.group(1).split(",") if x.strip()]
        else:
            raise HtmlError(EXIT["unusable"], "chain html: a line of the sheet's decision block is not recognised: %r"
                            % ln[:60])
    un = next((k for k, ln in enumerate(lines) if ln.startswith("Unconfirmed waits (")), None)
    return {"head": [ln.strip() for ln in until_blank(1)], "decisions": lines[i].rstrip(":"), "items": items,
            "unconfirmed": until_blank(un) if un is not None else [], "writes": until_blank(j + 1)}


def _subjects(ident, ids, drawn, goal):
    """The nodes a question is about: `kind:N` the member N, `kind:A-B` both, a grouped question every member its
    ids name, and a question about the whole chain (`cap:c12`, `label_set:c12`) the end goal."""
    if ident.startswith("group:"):
        out = []
        for i in ids:
            out += [n for n in _subjects(i, (), drawn, goal) if n not in out]
        return out
    tail = ident.split(":", 1)[1] if ":" in ident else ""
    pair = re.fullmatch(r"(\d+)-(\d+)", tail)
    nums = [int(pair.group(1)), int(pair.group(2))] if pair else [int(tail)] if tail.isdigit() else None
    return [goal] if nums is None else [n for n in nums if n in drawn]


def _plan_state(r):
    """-> (class, word) for a member the sheet puts in its own block, else (None, None): then the forecast marker
    names it. Same precedence as the sheet's blocks: a checkpoint, then a paid step, then the verdict."""
    if r["verdict"] == "checkpoint":
        return "check", "checkpoint"
    if r.get("spend"):
        return "paid", "paid gate"
    return {"not-a-goal": ("notgoal", "not a goal"), "awaiting-owner": ("awaiting", "awaiting owner"),
            "parked": ("parked", "parked")}.get(r["verdict"], (None, None))


def _ref_label(ref):
    return clean_title(ref)[:28]


def build_plan(rec, config, sheet, titles=None, now=None):
    """The model of one chain from its STORED plan record, in the shape `render_page` draws: every member (deep
    ones too), the done prerequisites, the externals, every edge with its tag, and the sheet's numbered questions
    with the nodes each is about. The forecast is the sheet's own call, so a member's marker is the sheet's."""
    titles = titles or {}
    goal = rec["goal"]
    held = chain._held(rec, True)
    fc = chain_forecast.forecast(rec, config, None, held, chain._declined(rec), held - chain._held(rec))
    marks = {x["number"]: x for x in fc["members"]}
    nodes = {}
    for r in rec["members"]:
        mk = marks[r["number"]]
        k, word = _plan_state(r)
        ex = [["Verdict", r["verdict"]]] + ([["Kind", r["kind"]]] if r.get("kind") else [])
        if r.get("spend"):
            ex.append(["Paid step", "asks for paid work; a run starts it only after the answer to irreversible:%d"
                       % r["number"]])
        ck = r.get("checkpoint")
        if isinstance(ck, dict):
            ex.append(["Checkpoint", "; ".join(str(ck[f]) for f in ("state", "unit", "why") if ck.get(f))])
        ex.append(["Forecast", _marker_text(mk["marker"], mk["after"])])
        nodes[r["number"]] = {"n": r["number"], "m": mk["marker"], "after": mk["after"], "why": mk["why"],
                              "life": None, "prio": r.get("prio"), "unit": r.get("unit"), "depth": r["depth"],
                              "k": k, "word": word, "ex": ex, "label": "#%d" % r["number"]}
    for n in rec.get("done") or []:
        nodes.setdefault(n, {"n": n, "m": "closed", "after": [], "why": "resolved", "life": None, "prio": None,
                             "unit": None, "depth": 0, "label": "#%d" % n})
    edges = {(e["from"], e["to"]): e["tag"] if e["tag"] in ("explicit", "unconfirmed") else "ledger"
             for e in rec.get("edges") or [] if e["from"] in nodes and e["to"] in nodes}
    for w in rec.get("closed_gates") or []:          # the one place the record says what a closed prerequisite freed
        for b in w.get("dependents") or []:
            if w["prerequisite"] in nodes and b in nodes:
                edges.setdefault((w["prerequisite"], b), "explicit")
    for i, x in enumerate(rec.get("externals") or []):
        key = -(i + 1)
        nodes[key] = {"n": key, "m": "outside", "after": [], "why": "outside the chain (%s)" % x.get("kind"),
                      "life": None, "prio": None, "unit": None, "depth": 0, "label": _ref_label(x["ref"]),
                      "ex": [["Kind", str(x.get("kind"))]]}
        for b in x.get("needed_by") or []:
            if b in nodes:
                edges[(key, b)] = "explicit"
    waits, frees = collections.defaultdict(list), collections.defaultdict(list)
    for (a, b) in sorted(edges):
        waits[b].append(a)
        frees[a].append(b)
    for n, d in nodes.items():
        d["waits"], d["frees"] = sorted(waits[n]), sorted(frees[n])
        d["t"] = titles.get(n)
    members = [d for n, d in nodes.items() if n in marks]
    qs, badges = [], collections.defaultdict(list)
    for it in sheet["items"]:
        ans = it["first"].endswith("]") and "[answered: " in it["first"]
        about = _subjects(it["ident"], it["ids"], set(nodes), goal)
        qs.append(dict(it, answered=ans, nodes=about))
        for n in about:
            badges[n].append((it["n"], ans))
    return {"goal": goal, "nodes": nodes, "edges": [(a, b, t) for (a, b), t in sorted(edges.items())],
            "stops": [{"n": s["number"], "m": s["marker"], "why": s["why"]} for s in fc["stops"]],
            "forecast": fc["line"], "notes": fc["notes"], "reason": fc["reason"], "finish": len(fc["finish"]),
            "open": len(members), "closed": len(rec.get("done") or []), "outside": len(rec.get("externals") or []),
            "cycles": [], "depth": max([d["depth"] for d in members] or [0]),
            "decisions": sum(1 for d in members if d["m"] == "needs decision"),
            "over_cap": len(members) > MEMBER_CAP, "titled": bool(titles), "built_at": None, "age": None,
            "fresh": (True, "ok"), "mode": None, "questions": qs, "badges": dict(badges),
            "legend_text": {"needs decision": "unarmed, upper-tier, or kept waiting by an answer"},
            "plan": {"head": sheet["head"], "decisions": sheet["decisions"], "unconfirmed": sheet["unconfirmed"],
                     "writes": sheet["writes"], "complete": bool(rec.get("complete")),
                     "stops": [str(x) for x in rec.get("stops") or []],
                     "unconfirmed_edges": any(t == "unconfirmed" for t in edges.values())}}


def _plan_header(model, repo):
    p = model["plan"]
    goal = model["nodes"][model["goal"]]
    notices = []
    if not p["complete"]:
        notices.append("This plan is INCOMPLETE%s. Anything it could not read is not drawn; do not approve it as it "
                       "stands." % (": " + "; ".join(p["stops"][:3]) if p["stops"] else ""))
    if model["over_cap"]:
        notices.append("This chain has %d open members, over the page cap of %d. It is written in full and may be "
                       "hard to read." % (model["open"], MEMBER_CAP))
    if not model["titled"]:
        notices.append("No titles were given, so members are named by number. Pass --titles FILE to add them.")
    if model.get("simple"):
        notices.append("Layout simplified: this chain is too large for channel routing, so long edges are drawn "
                       "straight and may cross nodes.")
    hint = "Every member of the stored plan is drawn. Numbered badges mark the questions below; click one, or the " \
           "question, to find the other."
    if p["unconfirmed_edges"]:
        hint += " Dashed edges are waits read from prose or comments, not confirmed."
    if model["closed"]:
        hint += (" Closed prerequisites sit behind the toggle without edges, except where a warning ties one to a "
                 "member: the stored plan does not record which member each one freed.")
    return ('<header class="top"><h1>Chain plan for #%d%s <small>%s</small></h1><div class="sheethead">%s</div>%s'
            '<div class="hint">%s</div><div class="legend">%s</div></header>'
            % (model["goal"], " " + _esc(goal["t"]) if goal.get("t") else "", _esc(repo or ""),
               "".join("<div>%s</div>" % _esc(ln) for ln in p["head"]),
               "".join('<div class="notice">%s</div>' % _esc(n) for n in notices), _esc(hint), _legend(model)))


def _questions_html(model):
    """The questions panel: each as the sheet prints it (number, id, ask, any recorded answer, options, note)."""
    qs, parts, section = model["questions"], [], None
    for q in qs:
        if q.get("section") and q["section"] != section:        # a heading the sheet prints inside its decisions
            parts.append('<li class="qh">%s</li>' % _esc(q["section"]))
        section = q.get("section")
        parts.append('<li class="q%s" data-q="%d" tabindex="0" role="button"><b>%s</b>%s</li>'
                     % (" answered" if q["answered"] else "", q["n"], _esc(q["first"]),
                        "".join("<span>%s</span>" % _esc(x) for x in q["more"])))
    items = "".join(parts)
    return ('<section id="qs"><h2>%s</h2><p class="sub">%s</p><ul>%s</ul></section>'
            % ("%d question%s before anything is armed" % (len(qs), "" if len(qs) == 1 else "s") if qs
               else "No questions before anything is armed", _esc(model["plan"]["decisions"]), items))


def _more_html(model):
    p = model["plan"]
    out = []
    for lines in (p["unconfirmed"], ["Writes that follow approval:"] + p["writes"]):
        if len(lines) > 1:
            out.append("<details><summary>%s</summary><pre>%s</pre></details>"
                       % (_esc(lines[0].rstrip(":")), _esc("\n".join(lines[1:]))))
    return '<section class="more">%s</section>' % "".join(out) if out else ""


def run_plan(sdlc, goal, out_dir=None, titles_path=None, now=None):
    """`(lines, exit_code)`: write the page of the STORED plan record of `goal` to `chain-plan-<goal>.html` under
    `out_dir` (default `<sdlc>/state/dag-html`). No GitHub call. No record, an unreadable one, or a record the
    sheet cannot print is refused (2) and nothing is written."""
    now = time.time() if now is None else now
    path = chain._record_path(sdlc, goal)
    if not pathlib.Path(path).is_file():
        return ["chain.py html: no plan record for #%d (%s); run `python3 %s plan %s %d` first"
                % (goal, path, _HERE / "chain.py", sdlc, goal)], EXIT["refused"]
    try:
        rec = chain._read_record(path)
        config = chain._load("state").load_config(sdlc)
        titles = read_titles(titles_path) if titles_path else {}
        out = pathlib.Path(out_dir) if out_dir else pathlib.Path(sdlc) / "state" / "dag-html"
        check_out(out)
        model = build_plan(rec, config, read_sheet(chain.render_sheet(rec, now, config)), titles, now)
    except chain.ChainRefused as exc:
        return [str(exc)], EXIT["refused"]
    except HtmlError as exc:
        return [str(exc)], exc.code
    os.makedirs(out, exist_ok=True)
    name = out / ("chain-plan-%d.html" % goal)
    dag_store.state.atomic_write_text(name, render_page(model))
    return ["wrote %s (#%d, %d members, %d questions)" % (name, goal, model["open"], len(model["questions"]))] \
        + (["  note: #%d has %d open members, over the page cap of %d" % (goal, model["open"], MEMBER_CAP)]
           if model["over_cap"] else []), EXIT["ok"]


# ------------------------------------------------------------------ the verb

def end_goals(m):
    """Every end goal of the map: an open issue no open goal waits on, with an open prerequisite (what
    `dag.py chains` lists as `ends`), sorted."""
    g = dag_chain._graph(m)
    ends = {n for part in dag_chain._ends_and_sets(g) for n in part["ends"] if g.nodes[n].kind == "issue"}
    return sorted(ends)


def run(sdlc, goal=None, all_=False, out_dir=None, titles_path=None, now=None):
    """`(lines, exit_code)`: write the page of `goal`, or every chain's page and the index. Writes only under
    `out_dir` (default `<sdlc>/state/dag-html`); no GitHub call."""
    now = time.time() if now is None else now
    if (goal is None) == (not all_):
        return ["dag html: give exactly one of --goal N or --all"], EXIT["refused"]
    try:
        m, _md, config, ok, why = _open(sdlc, now)
        titles = read_titles(titles_path) if titles_path else {}
        out = pathlib.Path(out_dir) if out_dir else pathlib.Path(sdlc) / "state" / "dag-html"
        check_out(out)
        goals = [goal] if not all_ else end_goals(m)
        models = [build(m, config, n, titles, now, (ok, why)) for n in goals]
    except HtmlError as exc:
        return [str(exc)], exc.code
    repo = m["header"].get("repo")
    os.makedirs(out, exist_ok=True)
    lines, rows = [], []
    for model in models:
        name = "chain-%d.html" % model["goal"]
        dag_store.state.atomic_write_text(out / name, render_page(model, repo))
        lines.append("wrote %s (#%d, %d open members, depth %d)" % (out / name, model["goal"], model["open"],
                                                                   model["depth"]))
        if model["over_cap"]:
            lines.append("  note: #%d has %d open members, over the page cap of %d" % (model["goal"], model["open"],
                                                                                         MEMBER_CAP))
        rows.append({"file": name, "goal": model["goal"], "title": model["nodes"][model["goal"]]["t"],
                     "open": model["open"], "depth": model["depth"], "stops": model["stops"],
                     "decisions": model["decisions"], "finish": model["finish"], "over_cap": model["over_cap"],
                     "top": model["top"], "who": model["who"]})
    if all_:
        rows.sort(key=lambda r: (-r["open"], r["goal"]))
        t = dag_store.stamp(m)
        dag_store.state.atomic_write_text(
            out / "index.html", render_index(rows, repo, _stamp(dag_store.parse_ts(m["header"].get("built_at"))),
                                             _age(now - t) if t is not None else "unknown", (ok, why), bool(titles)))
        lines.append("wrote %s (%d chains)" % (out / "index.html", len(rows)))
    return lines, EXIT["ok"]
