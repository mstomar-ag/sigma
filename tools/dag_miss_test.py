#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""The pick-time miss test (R1) and the fresh-child timing run (R2) for the stored dependency map (#1618).

USAGE
    python3 tools/dag_miss_test.py --config FILE --clone DIR --confirm [--only r1|r2|all]
                                   [--rounds N] [--runs N] [--strategy plain|wide]
    python3 tools/dag_miss_test.py --help

WHAT IT DOES. R1 applies one of six mutations (label add, label remove, close, reopen, comment, a
hand-filed ticket carrying a `Blocked by` marker) to a ticket at each of P0..P3, cycling so every
mutation meets every priority, and after each one runs a shadow pick: the stored map is brought up to
date with `dag_check.check`, its decision (open `sdlc:goal` tickets not blocked by an open ticket, ordered
by priority then number) is compared with the decision of a fresh full read into a scratch state
directory. Any difference is a MISS. The comparison covers the whole ordered list, which is stricter than
its first three entries. R2 times, per mode (`off` = a full read each pick, `on` = stored map plus
`dag_check.check`), the pick after a change, in fresh scratch state directories, and prints each time
and the median. `--strategy wide` rewinds the map's cursor and re-lists the open P0 and P1 tickets
before each pick; use it only after R1 reports a miss.

SAFETY. The config file (JSON, `{"repo": "<owner>/<name>"}`, never committed) names the only repository
the tool may touch. It exits 2 and runs nothing unless the clone's `origin` URL is exactly that repository,
the repository is not a public one (a repository named `sigma`, or the origin of the checkout this file
lives in, is refused), and `--confirm` is given. Every ticket it creates is recorded and closed at the
end, and printed by number. The output holds numbers, mutation names and priorities, never ticket text.

EXIT. 0 ok, 1 a miss or a failure, 2 a refusal or a usage error.
"""
import argparse
import contextlib
import importlib.util
import json
import pathlib
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"
MUTATIONS = ("label-add", "label-remove", "close", "reopen", "comment", "hand-filed")
PRIORITIES = ("P0", "P1", "P2", "P3")
GOAL, BLOCKED = "sdlc:goal", "sdlc:blocked"
MIN_ROUNDS = 40
WIDE_SECONDS = 3600
TITLE = "dag miss test ticket"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


dag_check = _load("dag_check")
dag_collect = dag_check.dag_collect
dag_store = dag_collect.dag_store
dag_core = dag_collect.dag_core
gh_api = dag_collect.gh_api


class Refusal(Exception):
    """The target is not one the tool may touch."""


# ------------------------------------------------------------------ the target check

def parse_remote(url):
    """`owner/name` (lower case) from an ssh, https or scp-style remote URL, or None."""
    u = (url or "").strip().rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    if "://" in u:
        path = u.split("://", 1)[1].partition("/")[2]
    elif ":" in u:
        path = u.split(":", 1)[1]
    else:
        path = u
    parts = [p for p in path.split("/") if p]
    return "/".join(parts[-2:]).lower() if len(parts) >= 2 else None


def _origin(directory):
    try:
        p = subprocess.run(["git", "-C", str(directory), "remote", "get-url", "origin"],
                           capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout.strip() if p.returncode == 0 else None


def check_target(config_path, clone, own_origin=None):
    """-> the configured `owner/name`, or raises `Refusal`. Nothing is run before this passes."""
    try:
        cfg = json.loads(pathlib.Path(config_path).read_text(encoding="utf-8"))
        repo = cfg["repo"]
    except (OSError, ValueError, KeyError, TypeError):
        raise Refusal("the config file is missing or has no repo key")
    want = parse_remote(repo) if isinstance(repo, str) else None
    if not want or want != repo.strip().lower():
        raise Refusal("the config repo must read owner/name")
    own = parse_remote(own_origin if own_origin is not None else _origin(ROOT) or "")
    if want.split("/")[1] == "sigma" or want == own:
        raise Refusal("the configured repository is public; use a private clone")
    have = parse_remote(_origin(clone) or "")
    if have != want:
        raise Refusal("the clone's remote is not the configured repository")
    return repo.strip()


# ------------------------------------------------------------------ backends

class RealBackend:
    """`gh api` REST against the declared repository only."""

    def __init__(self, repo):
        self.repo, self.created = repo, []

    def now(self):
        return time.time()

    def run(self, args):
        return gh_api._default_run(args)

    def get(self, args):
        return gh_api.run_gh(args)

    def _write(self, method, tail, *fields):
        args = ["api", "repos/%s/%s" % (self.repo, tail), "--method", method]
        for f in fields:
            args += ["-f", f]
        rc, out, err = gh_api.run_gh(args)
        if rc != 0:
            raise RuntimeError("write failed (exit %d)" % rc)
        return json.loads(out) if out.strip() else {}

    def create(self, title, labels, body):
        n = self._write("POST", "issues", "title=" + title, "body=" + body,
                        *["labels[]=" + x for x in labels])["number"]
        self.created.append(n)
        return n

    def add_label(self, n, label):
        self._write("POST", "issues/%d/labels" % n, "labels[]=" + label)

    def remove_label(self, n, label):
        try:
            self._write("DELETE", "issues/%d/labels/%s" % (n, urllib.parse.quote(label, safe="")))
        except RuntimeError:
            pass                                          # already absent

    def close(self, n):
        self._write("PATCH", "issues/%d" % n, "state=closed")

    def reopen(self, n):
        self._write("PATCH", "issues/%d" % n, "state=open")

    def comment(self, n, text):
        self._write("POST", "issues/%d/comments" % n, "body=" + text)


# ------------------------------------------------------------------ the shadow pick

def _config(repo):
    return {"discovery": {"github": {"repo": repo}, "dag": {"mode": "on"}}}


def _sdlc(base, repo):
    d = pathlib.Path(base) / ".sdlc"
    (d / "state").mkdir(parents=True)
    cfg = _config(repo)
    (d / "config.json").write_text(json.dumps(cfg))
    return d, cfg


def _resolved(n):
    return n is not None and ((n["kind"] == "issue" and n["state"] == "closed")
                              or (n["kind"] == "pr" and n["state"] == "merged"))


def decision(m):
    """Numbers of the open goal tickets no open ticket blocks, ordered by priority then number."""
    nodes = {n["number"]: n for n in m["nodes"]}
    held = {e["blocked"] for e in m["edges"] if not _resolved(nodes.get(e["blocker"]))}
    rank = {p: i for i, p in enumerate(PRIORITIES)}
    ok = [n for n in nodes.values() if n.get("in_corpus") and n["state"] == "open"
          and n["life"] == "goal" and n["number"] not in held]
    return [n["number"] for n in sorted(ok, key=lambda n: (rank.get(n.get("prio"), 9), n["number"]))]


def _load_map(sdlc, cfg):
    m, why = dag_store.load(sdlc, cfg)
    if m is None:
        raise RuntimeError("map unusable (%s)" % why)
    return m


def _build(backend, sdlc, cfg):
    rep = dag_collect.refresh(sdlc, cfg, run=backend.run, now=backend.now())
    if rep["status"] != "saved":
        raise RuntimeError("full read not saved (%s)" % rep["status"])


def fresh_decision(backend):
    with tempfile.TemporaryDirectory(prefix="dag-miss-fresh-") as base:
        sdlc, cfg = _sdlc(base, backend.repo)
        _build(backend, sdlc, cfg)
        return decision(_load_map(sdlc, cfg))


def _widen(sdlc, cfg):
    m = _load_map(sdlc, cfg)
    h = m["header"]
    at = dag_store.parse_ts(h.get("cursor") or h.get("last_ok_at"))
    if at is not None:
        h["cursor"] = dag_collect._iso(at - WIDE_SECONDS)
    h["etag"] = None
    ok, why = dag_store.save(sdlc, m, cfg)
    if not ok:
        raise RuntimeError("widen not saved (%s)" % why)


def _relist(backend, sdlc, cfg):
    """Re-list the open P0 and P1 tickets and make the map agree with them."""
    m = _load_map(sdlc, cfg)
    labels = dict(dag_collect.label_sets(cfg))
    seen, nodes = set(), {n["number"]: n for n in m["nodes"]}
    have = {(e["blocker"], e["blocked"]) for e in m["edges"]}
    for p in ("P0", "P1"):
        for it in gh_api.list_issues(backend.run, backend.repo, [GOAL, "priority:" + p], 2000):
            n, names = it["number"], dag_collect._names(it)
            seen.add(n)
            nodes[n] = {"number": n, "kind": "issue", "state": "open", "in_corpus": True,
                        "life": dag_collect._life(names, labels, "sdlc:in-progress"),
                        "prio": dag_collect._prio(names, "priority:", {}),
                        "updated_at": it.get("updated_at")}
            for a, b, _p in dag_core.edges_from("%s\n%s" % (it.get("title") or "", it.get("body") or ""), n):
                if (a, b) not in have:
                    have.add((a, b))
                    m["edges"].append({"blocker": a, "blocked": b, "source": "body"})
    for n in nodes.values():
        if n.get("prio") in ("P0", "P1") and n["state"] == "open" and n["number"] not in seen:
            n["state"], n["life"] = "closed", "none"
    m["nodes"] = [nodes[k] for k in sorted(nodes)]
    ok, why = dag_store.save(sdlc, m, cfg)
    if not ok:
        raise RuntimeError("relist not saved (%s)" % why)


def map_decision(backend, sdlc, cfg, strategy="plain"):
    if strategy == "wide":
        _widen(sdlc, cfg)
    res = dag_check.check(sdlc, cfg, get=backend.get, run=backend.run, now=backend.now())
    if res["status"] in ("error", "busy", "no-map", "off", "refresh-requested"):
        raise RuntimeError("change check did not run (%s)" % res["status"])
    if strategy == "wide":
        _relist(backend, sdlc, cfg)
    return decision(_load_map(sdlc, cfg))


# ------------------------------------------------------------------ mutations

def _seed(backend):
    return [backend.create(TITLE, [GOAL, "priority:" + p], "") for p in PRIORITIES]


def apply_mutation(backend, kind, ticket, prio):
    if kind == "label-add":
        backend.add_label(ticket, BLOCKED)
    elif kind == "label-remove":
        backend.remove_label(ticket, BLOCKED)
    elif kind == "close":
        backend.close(ticket)
    elif kind == "reopen":
        backend.reopen(ticket)
    elif kind == "comment":
        backend.comment(ticket, "note")
    else:
        backend.create(TITLE, [GOAL, "priority:" + prio], "Blocked by #%d" % ticket)


def r1(backend, rounds=MIN_ROUNDS, strategy="plain"):
    tickets = _seed(backend)
    rows = []
    with tempfile.TemporaryDirectory(prefix="dag-miss-map-") as base:
        sdlc, cfg = _sdlc(base, backend.repo)
        _build(backend, sdlc, cfg)
        for r in range(rounds):
            kind, p = MUTATIONS[r % len(MUTATIONS)], (r // len(MUTATIONS)) % len(PRIORITIES)
            apply_mutation(backend, kind, tickets[p], PRIORITIES[p])
            same = map_decision(backend, sdlc, cfg, strategy) == fresh_decision(backend)
            rows.append((r + 1, kind, PRIORITIES[p], "hit" if same else "miss"))
    return {"rows": rows, "rounds": len(rows), "misses": sum(1 for x in rows if x[3] == "miss"),
            "strategy": strategy}


# ------------------------------------------------------------------ R2

def r2(backend, runs=3):
    tickets = _seed(backend)
    times, dirs = {"off": [], "on": []}, []
    with contextlib.ExitStack() as stack:
        for mode in ("off", "on"):
            for i in range(runs):
                base = stack.enter_context(tempfile.TemporaryDirectory(prefix="dag-miss-r2-"))
                dirs.append(base)
                sdlc, cfg = _sdlc(base, backend.repo)

                def pick():
                    if mode == "on" and dag_store.load(sdlc, cfg)[0] is not None:
                        dag_check.check(sdlc, cfg, get=backend.get, run=backend.run, now=backend.now())
                    else:
                        _build(backend, sdlc, cfg)
                    return decision(_load_map(sdlc, cfg))[:3]

                pick()                                    # the child's first pick, untimed
                p = i % len(PRIORITIES)
                apply_mutation(backend, MUTATIONS[(i + 2) % len(MUTATIONS)], tickets[p], PRIORITIES[p])
                apply_mutation(backend, "hand-filed", tickets[p], PRIORITIES[p])
                t0 = time.perf_counter()
                pick()
                times[mode].append(time.perf_counter() - t0)
    return {"off": times["off"], "on": times["on"], "dirs": dirs,
            "median": {m: statistics.median(times[m]) for m in times}}


# ------------------------------------------------------------------ output and cleanup

def render_r1(res):
    lines = ["R1 strategy %s" % res["strategy"], "round  mutation       priority  result"]
    lines += ["%5d  %-13s  %-8s  %s" % row for row in res["rows"]]
    lines.append("R1 total: %d rounds, %d miss" % (res["rounds"], res["misses"]))
    if res["misses"] and res["strategy"] == "plain":
        lines.append("R1 case: a miss under the plain strategy; rerun with --strategy wide")
    elif res["misses"]:
        lines.append("R1 case: misses remain under the wide strategy")
    else:
        lines.append("R1 case: no miss")
    return "\n".join(lines)


def render_r2(res):
    out = []
    for mode in ("off", "on"):
        out.append("R2 %s: %s median %.4f" % (mode, " ".join("%.4f" % t for t in res[mode]),
                                              res["median"][mode]))
    return "\n".join(out)


def cleanup(backend):
    """Close exactly the tickets this run created. -> the numbers closed (a failure is raised after
    the rest were tried)."""
    closed, failed = [], []
    for n in list(backend.created):
        try:
            backend.close(n)
            closed.append(n)
        except Exception:                                 # noqa: BLE001 - keep closing the rest
            failed.append(n)
    if failed:
        raise RuntimeError("could not close tickets: %s" % ", ".join(map(str, failed)))
    return closed


def _parser():
    ap = argparse.ArgumentParser(prog="dag_miss_test.py", description=__doc__.split("\n\n", 1)[0],
                                 epilog="Exit: 0 ok, 1 a miss or a failure, 2 a refusal or usage error.")
    ap.add_argument("--config", required=True, help="JSON file naming the one repository to touch")
    ap.add_argument("--clone", required=True, help="a clone whose origin is that repository")
    ap.add_argument("--confirm", action="store_true", help="agree to create and close tickets there")
    ap.add_argument("--only", choices=("r1", "r2", "all"), default="all")
    ap.add_argument("--rounds", type=int, default=MIN_ROUNDS, help="R1 rounds, at least 40")
    ap.add_argument("--runs", type=int, default=3, help="R2 runs per mode")
    ap.add_argument("--strategy", choices=("plain", "wide"), default="plain")
    return ap


def main(argv=None, backend_factory=None):
    args = _parser().parse_args(argv)
    if not args.confirm:
        print("refused: pass --confirm; this creates and closes tickets on the configured repository",
              file=sys.stderr)
        return 2
    if args.rounds < MIN_ROUNDS or args.runs < 1:
        print("refused: --rounds is at least %d and --runs at least 1" % MIN_ROUNDS, file=sys.stderr)
        return 2
    try:
        repo = check_target(args.config, args.clone)
    except Refusal as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return 2
    backend = (backend_factory or RealBackend)(repo)
    rc = 0
    try:
        if args.only in ("r1", "all"):
            res = r1(backend, args.rounds, args.strategy)
            print(render_r1(res))
            rc = 1 if res["misses"] else rc
        if args.only in ("r2", "all"):
            print(render_r2(r2(backend, args.runs)))
    except Exception as exc:                              # noqa: BLE001 - class name only, never ticket text
        print("failed: %s" % type(exc).__name__)
        rc = 1
    finally:
        try:
            for n in cleanup(backend):
                print("closed ticket %d" % n)
        except Exception as exc:                          # noqa: BLE001
            print("cleanup failed: %s" % exc)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
