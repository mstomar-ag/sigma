#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""One offline end-to-end run of every part of the decision rubric on a throwaway repository.

USAGE
    python3 tools/rubric_e2e.py run [REPO] [--switches on|off|baseline]
    python3 tools/rubric_e2e.py --help

WHAT IT DOES. Creates REPO (a path that does not exist yet or is an empty directory; with no REPO,
a fresh directory under the system temp dir), runs `git init` there, then drives six parts with fake
inputs: records (the decision-record writer), classifier (question kinds), ladder (the autonomy
level), judge (the drift judge with a fake model call), triage (the filing deny-list and buckets)
and migration (the retired-queue verb against an in-memory source). Nothing touches the network,
`gh` or a model, and nothing is ever deleted: the directory is left in place so you can look at it.

SWITCHES. `on` opens every decision_rubric part; `off` writes every part as disabled; `baseline`
supplies no decision_rubric block at all. The run prints one `DIGEST` line, a hash of every file the
run left in REPO plus every write the fake source saw (generated ids and times are masked). The
claim under test: the `off` digest equals the `baseline` digest, so a closed gate changes nothing.
Triage and migration are not rubric switches (triage is `ai_filed.triage.enabled`, on by default), so
they run the same way in all three modes.

EXIT. 0 when every step held, 1 when a step failed, 2 on a usage error or a refused directory.
"""
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"
MODES = ("on", "off", "baseline")
PARTS = ("records", "hard_stops", "autonomy", "drift", "rulebook")
SENTINEL = ".rubric-e2e"
QUOTE = "Releases are cut only by the repository owner."
_MASK = re.compile(r"\d{8}T\d{6}-[0-9a-f]{8}|\d{4}-\d\d-\d\dT[\d:.+\-]+|watch-[0-9A-Za-z_.\-]+")


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def config_for(mode):
    cfg = {"ai_filed": {"triage": {"enabled": True}}}
    if mode == "baseline":
        return cfg
    on = mode == "on"
    block = {p: {"enabled": on} for p in PARTS}
    block["autonomy"]["applier"] = {"enabled": False}
    if on:
        block["drift"].update(model="fake-model", spend_ceiling_usd_per_day=1.0)
    cfg["decision_rubric"] = block
    return cfg


class FakeSource:
    """In-memory stand-in for the issue host: reads, one atomic label swap, comments; logs every write."""
    goal_label, proposed_label, parked_label = "sdlc:goal", "sdlc:needs-confirmation", "sdlc:parked"
    repo = "example/throwaway"
    project_enabled = False
    col = {"ready": "Ready", "backlog": "Backlog"}

    def __init__(self):
        self.store, self.writes = {}, []

    def add(self, number, *labels, title="t", body=""):
        self.store[number] = {"number": number, "title": title, "labels": list(labels), "body": body,
                              "state": "open", "comments": [], "author": "someone"}

    def overlay_labels(self):
        return ()

    def _repo_args(self):
        return []

    def _run(self, args):
        kv = dict(args[i + 1].split("=", 1) for i, a in enumerate(args) if a == "-f")
        want = [x for x in kv.get("labels", "").split(",") if x]
        rows = [i for _, i in sorted(self.store.items()) if all(x in i["labels"] for x in want)]
        per, page = int(kv["per_page"]), int(kv["page"])
        return json.dumps([{"number": i["number"], "title": i["title"], "state": i["state"], "body": i["body"],
                            "labels": [{"name": x} for x in i["labels"]]} for i in rows[(page - 1) * per: page * per]])

    def _read_issue(self, number, fields, comment_limit=None):
        i = self.store[int(str(number))]
        return {"labels": [{"name": x} for x in i["labels"]], "state": "OPEN", "stateReason": None,
                "author": {"login": i["author"]}, "body": i["body"], "comments": [{"body": c} for c in i["comments"]]}

    def _swap_labels(self, number, add=(), remove=(), **kw):
        n = int(str(number))
        self.writes.append(["swap", n, sorted(add), sorted(remove)])
        labels = [x for x in self.store[n]["labels"] if x not in remove]
        self.store[n]["labels"] = labels + [x for x in add if x not in labels]
        return True

    def _issue_comment(self, number, body):
        self.writes.append(["comment", int(str(number)), _MASK.sub("<masked>", body)])
        self.store[int(str(number))]["comments"].append(body)

    def _set_board_status(self, number, column):
        self.writes.append(["board", int(str(number)), column])
        return True


def _records(repo, cfg, src, log):
    rec_mod = _mod("decision_record")
    src.add(7, title="a parked goal")
    rec = rec_mod.build("unpark", 7, "needs_decision", "ship it", "owner said so", rec_mod.how_for("unpark"))
    out = rec_mod.write_all(str(repo / ".sdlc"), rec, cfg, src, text="Released.")
    log.append("records: stored=%s commented=%s error=%s" % (bool(out["id"]), out["commented"], out["error"]))
    return {"records": {"stored": bool(out["id"]), "commented": out["commented"], "error": out["error"]}}


def _classifier(log):
    q = _mod("qkind")
    line = q.render_line("needs_decision")
    got = {"declared": q.parse_line("parked\n" + line), "mapped": q.from_reason_class("quota"),
           "unmapped": q.from_reason_class("backlog-empty")}
    ok = got == {"declared": "needs_decision", "mapped": "quota", "unmapped": "unknown"}
    log.append("classifier: %s %s" % (json.dumps(got, sort_keys=True), "ok" if ok else "FAILED"))
    return got, ok


def _ladder(repo, cfg, log):
    au = _mod("autonomy")
    sdlc = str(repo / ".sdlc")
    ids = [au.record_watch(sdlc, "needs_decision", "core", "keep", c, cfg) for c in ("keep", "keep", "drop")]
    lvl = au.level(sdlc, "needs_decision", "core", cfg)
    log.append("ladder: watch facts written=%d level=%s" % (sum(1 for i in ids if i), lvl))
    return {"watch_written": sum(1 for i in ids if i), "level": lvl}


def _judge(repo, cfg, log):
    dj = _mod("drift_judge")

    def fake_call(prompt, model, budget):
        return {"kind": "conflicts", "quote": QUOTE, "cost_usd": 0.01}

    v = dj.judge("May an agent cut a release?", [("rule.md", QUOTE)], cfg, str(repo), call=fake_call, now=1.0e9)
    log.append("judge: kind=%s reason=%s" % (v.kind, v.reason))
    return {"kind": v.kind, "reason": v.reason}


def _triage(cfg, log):
    ft = _mod("file_triage")
    rows = {}
    for name, title, body in (("routine", "tidy the docs", "cleanup of old text"),
                              ("urgent", "unsafe default", "this is a vulnerability"),
                              ("denied", "rewrite history", "needs a force push")):
        d = ft.decide(title, body, {}, cfg)
        rows[name] = [d.kind, d.priority]
    log.append("triage: %s" % json.dumps(rows, sort_keys=True))
    return rows


def _migration(repo, cfg, src, log):
    pr = _mod("promote")
    src.add(1, src.proposed_label, "sdlc:followup", "priority:P2", title="tidy the docs", body="cleanup of old text")
    src.add(2, src.proposed_label, "priority:P1", title="human idea", body="please add a flag")
    src.add(3, src.proposed_label, "priority:P1", title="risky", body="needs a force push")
    sdlc = str(repo / ".sdlc")
    start = len(src.writes)
    dry = pr.migrate(src, cfg, apply=False, sdlc_dir=sdlc)
    before = len(src.writes) - start
    app = pr.migrate(src, cfg, apply=True, sdlc_dir=sdlc)
    again = pr.migrate(src, cfg, apply=True, sdlc_dir=sdlc)
    held = len(src.writes)
    kept = sum(1 for i in src.store.values() if src.proposed_label in i["labels"])
    res = {"dry_writes": before, "outcomes": [r["outcome"] for r in app["items"]], "complete": app["complete"],
           "second_run_writes": len(src.writes) - held, "label_left": kept,
           "dry_outcomes": [r["outcome"] for r in dry["items"]]}
    ok = before == 0 and kept == 0 and again["total"] == 0 and res["complete"]
    log.append("migration: %s %s" % (json.dumps(res, sort_keys=True), "ok" if ok else "FAILED"))
    return res, ok


def _digest(repo, results, writes):
    h = hashlib.sha256()
    for path in sorted(repo.rglob("*")):
        rel = path.relative_to(repo)
        if ".git" in rel.parts or not path.is_file():
            continue
        h.update(str(rel).encode())
        h.update(_MASK.sub("<masked>", path.read_text(encoding="utf-8", errors="replace")).encode())
    h.update(json.dumps([results, writes], sort_keys=True).encode())
    return h.hexdigest()[:16]


def _names_only(repo):
    return sorted(str(p.relative_to(repo)) for p in repo.rglob("*") if ".git" not in p.relative_to(repo).parts)


def run(repo_path=None, switches="on"):
    """Run every part once. Returns {"log", "digest", "failed", "repo"}. Never deletes anything."""
    if switches not in MODES:
        raise ValueError("switches must be one of %s" % (MODES,))
    repo = pathlib.Path(repo_path) if repo_path else pathlib.Path(tempfile.mkdtemp(prefix="rubric-e2e-"))
    repo.mkdir(parents=True, exist_ok=True)
    if any(repo.iterdir()):
        raise FileExistsError("%s is not empty; give a new or empty directory" % repo)
    repo = repo.resolve()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, stdin=subprocess.DEVNULL, capture_output=True)
    (repo / SENTINEL).write_text("throwaway repository for tools/rubric_e2e.py\n", encoding="utf-8")
    (repo / ".sdlc").mkdir()
    cfg = config_for(switches)
    log, failed, src, results = ["switches: %s repo: %s" % (switches, repo)], [], FakeSource(), {}
    steps = (("records", lambda: (_records(repo, cfg, src, log), True)),
             ("classifier", lambda: _classifier(log)),
             ("ladder", lambda: (_ladder(repo, cfg, log), True)),
             ("judge", lambda: (_judge(repo, cfg, log), True)),
             ("triage", lambda: (_triage(cfg, log), True)),
             ("migration", lambda: _migration(repo, cfg, src, log)))
    for name, step in steps:
        try:
            results[name], ok = step()
            if not ok:
                failed.append(name)
        except Exception as exc:                        # noqa: BLE001 - a step failure is reported, not raised
            failed.append(name)
            log.append("%s: FAILED %s: %s" % (name, type(exc).__name__, exc))
    digest = _digest(repo, results, src.writes)
    log.append("files: %d" % len(_names_only(repo)))
    log.append("DIGEST %s" % digest)
    return {"log": log, "digest": digest, "failed": failed, "repo": str(repo)}


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help") or argv[1] != "run":
        print(__doc__)
        return 0 if len(argv) >= 2 and argv[1] in ("-h", "--help") else 2
    rest, mode, repo = argv[2:], "on", None
    while rest:
        a = rest.pop(0)
        if a == "--switches" and rest:
            mode = rest.pop(0)
        elif a.startswith("-") or repo is not None:
            print("usage: rubric_e2e.py run [REPO] [--switches on|off|baseline]", file=sys.stderr)
            return 2
        else:
            repo = a
    if mode not in MODES:
        print("--switches must be one of: %s" % ", ".join(MODES), file=sys.stderr)
        return 2
    try:
        out = run(repo, mode)
    except FileExistsError as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return 2
    print("\n".join(out["log"]))
    return 1 if out["failed"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
