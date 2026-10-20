#!/usr/bin/env python3
"""dag.py -- the dependency map's command line (#1610, #1611).

  dag.py show [--sdlc DIR]      mode, age and counts of the stored map
  dag.py rebuild [--sdlc DIR]   read GitHub once, publish the map, print the call counts by class
  dag.py refresh [--force] [--sdlc DIR]
                                refresh the map when due (or always with --force), within the call cap
                                and time budget derived from SIGMA_WATCH_CALL_TIMEOUT (#1612)
  dag.py line [--sdlc DIR]      one line, the map's age and size; reads one file, no network, and
                                prints nothing when mode is off or there is no map

  dag.py history [--month YYYY-MM] [--all] [--sdlc DIR]
                                that month's history records, one JSON line each (default: the current
                                month); opens only that month's file, no network (#1622). `--all` reads every
                                writer's files in the ledger worktree, any month (#1624)

  dag.py chain GOAL [--json] [--verify] [--include-resolved] [--sdlc DIR]
                                every unresolved transitive blocker of GOAL, in order (schema
                                `dag-chain/1`, see dag_chain.py); read-only
  dag.py chains [--rank longest|biggest] [--json] [--sdlc DIR]
                                connected groups of two or more open members
  dag.py stuck [--json] [--sdlc DIR]
                                decision roots holding other work, most freeing first

  dag.py html (--goal N | --all) [--out DIR] [--titles FILE] [--sdlc DIR]
                                one self-contained, offline HTML page per chain (a goal and its open
                                prerequisites, the set `chain.py plan` reads), plus an index with `--all`;
                                default `<sdlc>/state/dag-html/`; reads only the stored map, no network
                                (#1924, see dag_html.py)

  dag.py report [--goal N] [--md | --json] [--sdlc DIR]
                                the whole map in one read-only report: state, the doctor's freshness and
                                shadow lines, stuck and active chains, cycles, chains done this month, where
                                the files live; `--goal N` is the chain behind N. Stored map only, no network,
                                writes nothing; with nothing to read it prints one line with the exact fix
                                (#1954, see dag_report.py, `/sigma-dag`)

`chain`, `chains` and `stuck` exit 0 answered, 2 goal unknown to the map, 3 map unusable (the line
says why), 4 mode off (one line). `--verify` re-reads open members live, at most 50.

With `discovery.dag.mode` off, both verbs print `dag: off` and touch nothing. `rebuild` runs
`dag_collect.refresh` (under the refresh lock; a failed or empty read leaves the old file as it was)
and exits 1 when nothing was published. `show` only reads: it never creates the state directory, never takes the refresh lock and never calls `gh`.
"""
import argparse
import importlib.util
import os
import pathlib
import sys
import time

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def _age(seconds):
    s = max(0, int(seconds))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size:
            return f"{s // size}{unit}"
    return f"{s}s"


def show(sdlc):
    ds = _load("dag_store")
    try:
        config = ds.state.load_config(sdlc)
    except Exception:
        config = {}
    md = ds.mode(config)
    if md == "off":
        return "dag: off"
    m, why = ds.load(sdlc, config)
    if m is None:
        return f"dag: mode {md}, {why}"
    t = ds.stamp(m)
    age = _age(time.time() - t) if t is not None else "unknown"
    ok, reason = ds.usable(m, time.time(), ds.ttl_minutes(config))
    line = (f"dag: mode {md}, age {age}, nodes {len(m['nodes'])}, edges {len(m['edges'])}, "
            f"{'usable' if ok else 'not usable (' + reason + ')'}")
    return line + ", nothing to plan" if ok and not m["nodes"] else line


def rebuild(sdlc):
    """`(line, exit_code)`: the outcome and the call counts by class (list, read, pull)."""
    dc = _load("dag_collect")
    try:                                                # #1957: take the lease, then read here regardless
        cfg = dc.dag_store.state.load_config(sdlc)
        if dc.dag_store.mode(cfg) != "off":
            _load("dag_lease").acquire(sdlc, cfg, time.time(), force=True)
    except Exception:
        pass
    rep = dc.refresh(sdlc)
    if rep["status"] == "off":
        return "dag: off", 0
    c = rep["calls"]
    line = (f"dag rebuild: {rep['status']}, calls list {c['list']} read {c['read']} pull {c['pull']}, "
            f"unread {len(rep['unread'])}")
    if rep["partial"]:
        line += ", partial"
    if rep["last_error"]:
        line += f", last_error {rep['last_error']}"
    return line, (0 if rep["status"] in ("saved", "busy") else 1)


def refresh(sdlc, force=False):
    """`(line, exit_code)` for the policy refresh (`dag_refresh.refresh_if_due`)."""
    dr = _load("dag_refresh")
    try:
        config = dr.dag_store.state.load_config(sdlc)
    except Exception:
        config = {}
    res = dr.refresh_if_due(sdlc, config, force=force)
    if res["status"] == "off":
        return "dag: off", 0
    out = "dag refresh: %s (%s)" % (res["status"], res["why"])
    if res.get("last_error"):
        out += ", last_error %s" % res["last_error"]
    return out, (1 if res["status"] in ("kept", "error") else 0)


def history(sdlc, month=None, all_writers=False):
    """`(text, exit_code)`: one month's records, one JSON line each. `all_writers` (#1624) reads every
    writer's files in the ledger worktree instead, in any month; nothing when the ledger is off."""
    import json
    dh = _load("dag_history")
    try:
        config = dh._load("dag_store").state.load_config(sdlc)
    except Exception:
        config = {}
    if dh._off(sdlc, config):
        return "dag: off", 0
    if all_writers:
        try:
            sync, ledger = dh._load("sync"), dh._load("ledger")
            if not ledger.enabled(config) or not sync.is_worktree(sdlc):
                return "dag history: ledger is off or has no worktree", 0
            recs = dh.read_all(sync.worktree(sdlc))
        except Exception as exc:                            # noqa: BLE001
            return "dag history: %s" % type(exc).__name__, 1
        return "\n".join(json.dumps(r, sort_keys=True, separators=(",", ":")) for r in recs), 0
    try:
        recs = dh.read_month(sdlc, month or dh.month_of(time.time()))
    except ValueError as exc:
        return "dag history: %s" % exc, 2
    return "\n".join(json.dumps(r, sort_keys=True, separators=(",", ":")) for r in recs), 0


def _ids(nums):
    return ", ".join("#%d" % n for n in nums)


def _chain_text(c):
    lines = ["chain for #%d (%s, %s, %s): %d members, %s"
             % (c["goal"], c["mode"], c["source"], c["closure_id"], len(c["members"]),
                "complete" if c["complete"] else "INCOMPLETE")]
    for i, w in enumerate(c["waves"]):
        lines.append("  wave %d: %s" % (i, _ids(w)))
    for d in c["decisions"]:
        lines.append("  decision #%d (%s): %s" % (d["number"], d["why"], d["hint"]))
    for cy in c["cycles"]:
        lines.append("  cycle: %s" % _ids(cy))
    if c.get("verify", {}).get("capped"):
        lines.append("  verify: capped, not every member was re-read")
    return "\n".join(lines)


def _query(verb, args, sdlc):
    """`(text, exit_code)` for chain / chains / stuck."""
    import json
    dc = _load("dag_chain")
    try:
        if verb == "chain":
            res = dc.chain(sdlc, args.goal, include_resolved=args.include_resolved, verify=args.verify)
            text = _chain_text(res)
        elif verb == "chains":
            res = dc.chains(sdlc, args.rank)
            text = "\n".join("chain: %s (end %s, depth %d)" % (_ids(c["members"]), _ids(c["ends"]), c["depth"])
                             for c in res) or "chains: none"
        else:
            res = dc.stuck(sdlc)
            text = "\n".join("%s wait on #%d's %s; %s" % (_ids(s["waiting"]), s["root"], s["why"], s["hint"])
                             for s in res) or "stuck: none"
    except dc.ChainError as exc:
        return str(exc), exc.code
    return (json.dumps(res, indent=2, sort_keys=True) if args.json else text), 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dag.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="verb", required=True)
    sp = sub.add_parser("show", help="print mode, age and counts of the stored map")
    sp.add_argument("--sdlc", default=None, help="the .sdlc directory (default: ./.sdlc)")
    rp = sub.add_parser("rebuild", help="read GitHub and publish the map; print call counts by class")
    rp.add_argument("--sdlc", default=None, help="the .sdlc directory (default: ./.sdlc)")
    fp = sub.add_parser("refresh", help="refresh the map when due (--force: now)")
    fp.add_argument("--force", action="store_true", help="refresh even when the map is fresh")
    fp.add_argument("--sdlc", default=None, help="the .sdlc directory (default: ./.sdlc)")
    lp = sub.add_parser("line", help="print the map's age and size on one line (no network)")
    lp.add_argument("--sdlc", default=None, help="the .sdlc directory (default: ./.sdlc)")
    yp = sub.add_parser("history", help="print one month of the map's history records (no network)")
    yp.add_argument("--month", default=None, help="YYYY-MM (default: the current month)")
    yp.add_argument("--all", action="store_true", help="every writer's records from the ledger worktree")
    yp.add_argument("--sdlc", default=None, help="the .sdlc directory (default: ./.sdlc)")
    cp = sub.add_parser("chain", help="every unresolved transitive blocker of a goal, in order")
    cp.add_argument("goal", type=int)
    cp.add_argument("--json", action="store_true", help="print the dag-chain/1 object")
    cp.add_argument("--verify", action="store_true", help="re-read open members live (cap 50)")
    cp.add_argument("--include-resolved", action="store_true", help="list resolved blockers too")
    cp.add_argument("--sdlc", default=None, help="the .sdlc directory (default: ./.sdlc)")
    hp = sub.add_parser("chains", help="connected groups of two or more open members")
    hp.add_argument("--rank", choices=("longest", "biggest"), default="biggest")
    hp.add_argument("--json", action="store_true")
    hp.add_argument("--sdlc", default=None, help="the .sdlc directory (default: ./.sdlc)")
    tp = sub.add_parser("stuck", help="decision roots holding other work, most freeing first")
    tp.add_argument("--json", action="store_true")
    tp.add_argument("--sdlc", default=None, help="the .sdlc directory (default: ./.sdlc)")
    wp = sub.add_parser("html", help="write one offline HTML page per chain (and an index with --all)")
    which = wp.add_mutually_exclusive_group(required=True)
    which.add_argument("--goal", type=int, help="the page of one goal and its open prerequisites")
    which.add_argument("--all", action="store_true", help="a page per end goal, and an index")
    wp.add_argument("--out", default=None, help="the output directory (default: <sdlc>/state/dag-html)")
    wp.add_argument("--titles", default=None, help="a JSON file of titles, {\"<number>\": \"<title>\"}; the map holds none")
    wp.add_argument("--sdlc", default=None, help="the .sdlc directory (default: ./.sdlc)")
    pp = sub.add_parser("report", help="the stored map in one read-only report (markdown, or --json)")
    pp.add_argument("--goal", type=int, default=None, help="the chain behind this goal instead")
    fmt = pp.add_mutually_exclusive_group()
    fmt.add_argument("--md", action="store_true", help="markdown (the default)")
    fmt.add_argument("--json", action="store_true", help="the dag-report/1 object")
    pp.add_argument("--sdlc", default=None, help="the .sdlc directory (default: ./.sdlc)")
    args = ap.parse_args(argv)
    sdlc = args.sdlc or os.path.join(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd(), ".sdlc")
    if args.verb in ("chain", "chains", "stuck"):
        out, code = _query(args.verb, args, sdlc)
        print(out)
        return code
    if args.verb == "report":
        out, code = _load("dag_report").report(sdlc, args.goal, args.json, chain_text=_chain_text)
        print(out, end="" if out.endswith("\n") else "\n")
        return code
    if args.verb == "html":
        lines, code = _load("dag_html").run(sdlc, args.goal, args.all, args.out, args.titles)
        print("\n".join(lines))
        return code
    if args.verb == "history":
        out, code = history(sdlc, args.month, args.all)
        if out:
            print(out)
        return code
    if args.verb == "rebuild":
        line, code = rebuild(sdlc)
        print(line)
        return code
    if args.verb == "refresh":
        out, code = refresh(sdlc, args.force)
        print(out)
        return code
    if args.verb == "line":
        out = _load("dag_refresh").line(sdlc)
        if out:
            print(out)
        return 0
    print(show(sdlc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
