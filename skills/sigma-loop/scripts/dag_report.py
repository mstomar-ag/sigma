"""The dependency map's read-only report (#1954). Library; `dag.py report` is its CLI and `/sigma-dag` its skill.

`report(sdlc, goal=None, as_json=False, now=None)` returns `(text, exit_code)` from the STORED map only: it
reads the config, the map file, this month's history file and the doctor's two map rows and the map's source and lease holder (`dag_source`; local files; the
doctor's own functions, not a copy of their math). It runs no process, makes no GitHub call, rebuilds
nothing and writes nothing. Markdown by default; `as_json` gives schema `dag-report/1` carrying the same facts.

Sections: the state (mode, age, node and edge counts, usable), the doctor's freshness verdict and shadow
comparison line, stuck chains (most freeing first), active chains, cycles, chains completed this month, and
where the files live. With `goal`, the chain behind that goal instead (the `dag.py chain` text under a heading). With `feature`, one unit's
open members, each with its open blockers, and every open blocker outside the unit labelled (`unit_facts`, #2056).

When there is nothing to read, ONE line says why and gives the exact fix; the answer is never empty:
mode `off`; `discovery.dag.mode` not written (the automatic refresh and the doctor rows start only once it
is written); no map; a map that is not usable (stale, partial, wrong schema). A map that is usable but whose
mode key is unwritten is still reported, with a note.

Scale: one map read plus work linear in nodes and edges, as the verbs it composes.
"""
import importlib.util
import json
import pathlib
import time

_HERE = pathlib.Path(__file__).resolve().parent
DAG_PY = "python3 skills/sigma-loop/scripts/dag.py"
SCHEMA = "dag-report/1"


def _load(name, base=None):
    spec = importlib.util.spec_from_file_location(name, (base or _HERE) / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


dag_store = _load("dag_store")
dag_chain = _load("dag_chain")
dag_stuck = _load("dag_stuck")
dag_history = _load("dag_history")
dag_refresh = _load("dag_refresh")
dag_source = _load("dag_source")


def _config(sdlc):
    try:
        return dag_store.state.load_config(sdlc)
    except Exception:                                   # noqa: BLE001
        return {}


def _doctor_rows(config, sdlc, now):
    """The doctor's freshness and shadow rows, or None for a row it does not produce or cannot run."""
    out = {"freshness": None, "shadow": None}
    try:
        doc = _load("doctor", _HERE.parent.parent / "sigma-doctor" / "scripts")
        out["freshness"] = doc._dag_map_row(config, str(sdlc), now=now)
        out["shadow"] = doc._dag_shadow_row(config, str(sdlc), now=now)
    except Exception:                                   # noqa: BLE001 - the report does not need the doctor
        pass
    return out


def _fix(sdlc, config, now):
    """`(line, map, reason)`: the one fix line when there is nothing to read, else `(None, map, "ok")`."""
    if dag_store.mode(config) == "off":
        return ("dag: mode is off; set `discovery.dag.mode` to `shadow` or `on` in .sdlc/config.json, "
                "then run `%s rebuild`" % DAG_PY), None, "off"
    written = dag_refresh.active(config)
    m, why = dag_store.load(sdlc, config)
    if m is None and not written:
        return ("dag: `discovery.dag.mode` is not written in .sdlc/config.json; set it to `shadow` or `on` "
                "(the automatic refresh and the doctor rows start only once it is written), "
                "then run `%s rebuild`" % DAG_PY), None, why
    if m is None:
        return "dag: no usable map (%s); run `%s rebuild`" % (why, DAG_PY), None, why
    ok, reason = dag_store.usable(m, now, dag_store.ttl_minutes(config))
    if not ok and not written:
        return ("dag: `discovery.dag.mode` is not written in .sdlc/config.json and the map is %s; set the key "
                "(the automatic refresh and the doctor rows start only once it is written), "
                "then run `%s refresh --force`" % (reason, DAG_PY)), None, reason
    if not ok:
        return "dag: the map is not usable (%s); run `%s refresh --force`" % (reason, DAG_PY), None, reason
    return None, m, "ok"


def _facts(sdlc, config, m, now):
    t = dag_store.stamp(m)
    stuck = sorted(dag_chain.stuck(sdlc, now=now), key=lambda s: (-len(s["waiting"]), s["root"]))
    chains = dag_chain.chains(sdlc, now=now)
    cycles = sorted(sorted(s["cycle"]) for s in stuck if s["why"] == "cycle")
    try:
        month = dag_history.month_of(now)
        done = [r for r in dag_history.read_month(sdlc, month) if r.get("ev") == "chain-completed"]
    except Exception:                                   # noqa: BLE001
        month, done = "", []
    rows = _doctor_rows(config, sdlc, now)
    try:
        source = dag_source.facts(sdlc, config, now=now)
    except Exception:                                   # noqa: BLE001
        source = None
    files = ["`.sdlc/state/dag.json` (the map)", "`.sdlc/state/dag-history/` (this month's records)"]
    try:
        if _load("ledger").enabled(config):
            files.append("the ledger branch's `DAG.md` (every writer's history)")
    except Exception:                                   # noqa: BLE001
        pass
    return {
        "schema": SCHEMA,
        "state": {"mode": dag_store.mode(config), "written": dag_refresh.active(config),
                  "age_seconds": None if t is None else max(0, int(now - t)),
                  "nodes": len(m["nodes"]), "edges": len(m["edges"]), "usable": True},
        "source": source, "freshness": rows["freshness"], "shadow": rows["shadow"],
        "stuck": [{k: s[k] for k in ("root", "why", "waiting", "kind")} | {"line": dag_stuck.line(s)} for s in stuck],
        "chains": [{"members": c["members"], "ends": c["ends"], "depth": c["depth"]} for c in chains],
        "cycles": cycles,
        "completed": {"month": month, "chains": len(done), "members": sum(int(r.get("n") or 0) for r in done)},
        "files": files,
    }


def _ids(nums):
    return ", ".join("#%d" % n for n in nums)


def _markdown(f):
    s = f["state"]
    age = "unknown" if s["age_seconds"] is None else dag_refresh._age(s["age_seconds"])
    out = ["# Dependency map report", ""]
    if not s["written"]:
        out += ["Note: `discovery.dag.mode` is not written; the automatic refresh and the doctor rows start "
                "only once it is written.", ""]
    out += ["- mode: %s" % s["mode"], "- age: %s" % age,
            "- nodes: %d, edges: %d" % (s["nodes"], s["edges"]), "- usable: yes"]
    if f.get("source") and f["source"]["ledger"]:       # ledger off: nothing to say beyond the JSON facts
        out.append("- %s" % dag_source.line(f["source"]))
    if f["freshness"]:
        out.append("- freshness: %s%s" % (f["freshness"]["name"],
                                         "" if f["freshness"]["ok"] else " (needs: %s)" % f["freshness"]["fix"]))
    if f["shadow"]:
        out.append("- shadow: %s" % f["shadow"]["name"])
    out += ["", "## Stuck chains", ""] + (["- " + x["line"] for x in f["stuck"]] or ["- none"])
    out += ["", "## Active chains", ""]
    out += ["- %s (%s, depth %d)" % (_ids(c["members"]), "end " + _ids(c["ends"]) if c["ends"] else "no end",
                                     c["depth"]) for c in f["chains"]] or ["- none"]
    out += ["", "## Cycles", ""] + (["- " + _ids(c) for c in f["cycles"]] or ["- none"])
    c = f["completed"]
    out += ["", "## Completed this month", "",
            "- %d chain%s completed (%d members)" % (c["chains"], "" if c["chains"] == 1 else "s", c["members"])]
    out += ["", "## Where the files live", ""] + ["- " + x for x in f["files"]]
    return "\n".join(out) + "\n"


def unit_facts(m, unit):
    """`(facts, None)` for one unit of the stored map, or `(None, line)` when no node carries it. Members are the
    open issues with `unit`; each lists its OPEN blockers (edge blocker->member), marked outside when the blocker
    is not of the unit, and `outside` lists those blockers once. A closed blocker is never shown (#2056)."""
    nodes = {n["number"]: n for n in m["nodes"]}
    units = sorted({n["unit"] for n in m["nodes"] if n.get("unit")})
    if unit not in units:
        return None, "dag: no unit %r in the map; known units: %s" % (unit, ", ".join(units) or "none")
    members = sorted(n for n, d in nodes.items() if d.get("unit") == unit and d["kind"] == "issue" and d["state"] == "open")
    blocked_by = {n: [] for n in members}
    for e in m["edges"]:
        b, a = e["blocker"], e["blocked"]
        if a in blocked_by and b in nodes and nodes[b]["state"] == "open":
            blocked_by[a].append(b)

    def row(n):
        return {"number": n, "life": nodes[n]["life"], "unit": nodes[n].get("unit")}
    outside = {}
    for a, bl in blocked_by.items():
        for b in bl:
            if nodes[b].get("unit") != unit:
                outside.setdefault(b, row(b) | {"blocks": []})["blocks"].append(a)
    return {"schema": SCHEMA, "feature": unit,
            "members": [row(n) | {"blockers": [row(b) | {"outside": b in outside} for b in sorted(set(blocked_by[n]))]}
                        for n in members],
            "outside": [outside[b] | {"blocks": sorted(outside[b]["blocks"])} for b in sorted(outside)]}, None


def _unit_markdown(f):
    out = ["# Unit %s" % f["feature"], "", "## Members", ""]
    for r in f["members"]:
        bl = ", ".join("#%d%s" % (b["number"], " (outside unit)" if b["outside"] else "") for b in r["blockers"])
        out.append("- #%d (%s): %s" % (r["number"], r["life"], "waits on " + bl if bl else "no open blockers"))
    out += ["", "## Outside blockers", ""]
    out += ["- #%d (unit %s, %s) holds %s" % (o["number"], o["unit"] or "none", o["life"], _ids(o["blocks"]))
            for o in f["outside"]] or ["- none"]
    return "\n".join(out) + "\n"


def report(sdlc, goal=None, as_json=False, now=None, rows=None, chain_text=None, feature=None):
    """`(text, exit_code)`. `rows` ({"freshness", "shadow"}) replaces the doctor's rows (tests); `chain_text`
    renders a chain (default: `dag.py`'s own). `feature` is the unit view: unknown unit exits 2 with one line,
    a unit with no open member says so (exit 0)."""
    now = time.time() if now is None else now
    config = _config(sdlc)
    line, m, reason = _fix(sdlc, config, now)
    if line:
        return (json.dumps({"schema": SCHEMA, "state": None, "fix": line, "reason": reason}, indent=2,
                           sort_keys=True) if as_json else line), 0
    if feature is not None:
        f, why = unit_facts(m, feature)
        if f is None:
            return why, 2
        if not f["members"]:
            return (json.dumps(f | {"note": "no open members"}, indent=2, sort_keys=True) if as_json
                    else "dag: unit %s has no open members" % feature), 0
        return (json.dumps(f, indent=2, sort_keys=True) if as_json else _unit_markdown(f)), 0
    if goal is not None:
        try:
            res = dag_chain.chain(sdlc, goal, now=now)
        except dag_chain.ChainError as exc:
            return str(exc), exc.code
        if as_json:
            return json.dumps(res, indent=2, sort_keys=True), 0
        text = (chain_text or _load("dag")._chain_text)(res)
        return "# Dependency chain for #%d\n\n%s\n" % (goal, text), 0
    f = _facts(sdlc, config, m, now)
    if rows:
        f.update(rows)
    return (json.dumps(f, indent=2, sort_keys=True) if as_json else _markdown(f)), 0
