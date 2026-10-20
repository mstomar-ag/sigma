"""The public regression scenario for Sigma Chain on a tangled backlog (offline: a stateful fake `gh`).

WHAT IT IS FOR. A real run of the chain on a large private backlog showed the plan and the driver over-promise:
dependencies written in comments or in loose prose are invisible, a checkpoint issue reads as work, follow-ups
awaiting the owner are armed on approve, a paid step is claimed, and nothing says how far a run will really get.
This module reproduces each of those shapes on a SYNTHETIC backlog of 20 generic issues
(`tests/fixtures/chain_regression/issues.json`) and states the right answer for it once, in
`tests/fixtures/chain_regression/reference.json`. It lets a change to Chain be checked for the whole class of
gaps in minutes, before a release is cut.

HOW IT DRIVES. The same fake as `tests/e2e/test_chain_scenario.py` (`ChainFake`, through that module's
`Scenario`), the same verbs through `chain.main(argv)`, in the order docs/chain.md documents. A scripted OWNER answers
the sheet from the reference. Scripted GOAL SESSIONS close or park each goal the pick claims, as the reference says.
The pick is a fake that behaves like the loop's scoped pick but judges a goal ready by the REFERENCE's true
dependencies, not by what Chain read: a goal Chain wrongly thinks is ready is never claimable here, so a missed
dependency shows as a missing edge and as a wrong stop, never as a lucky pass. After a stop whose root the reference
names a resolution for (a person unparks it), the scripted world applies it and the run goes on.

THE ASSERTIONS WERE WRITTEN FOR THE FINAL INTENDED BEHAVIOUR. On the engine as it stood when this was written
(1.0.17) `test_regression_scenario_end_to_end` was RED, by design; `criteria()` reports each failed criterion with its
reason, so a red run is a checklist. Nothing is marked xfail and nothing is skipped: with the dependency-reading,
checkpoint, confirmation, closed-prerequisite, hold and forecast changes in, both end goals and the forecast
(`test_forecast_equals_actual_run`) are green.

THE CHECKER IS CONTROLLED TOO. A synthetic perfect outcome passes `criteria()`, and breaking it one clause at a time
turns exactly that clause red (`test_checker_controls`): a checker never seen red proves nothing.

Limits, stated: the fake serves REST reads and writes only (no search endpoint, no timeline); the pick is a fake of
the loop's scoped pick, not the loop; goal sessions do no work, they close or park. A run on a live repository is
not measured here.
"""
import contextlib
import io
import json
import pathlib
import re
import subprocess
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import test_chain_arm as tca  # noqa: E402
import test_chain_scenario as tcs  # noqa: E402
import test_label_answer as tla  # noqa: E402
from test_label_drain_arm import DW  # noqa: E402

FIXTURES = HERE.parent / "fixtures" / "chain_regression"
REPO = "acme/widget"
MAX_CYCLES = 40
FORECAST_LINE = re.compile(r"can finish (\d+) of (\d+) before it stops on (.+)")
CRITERIA = ("reached", "owner", "members", "dependencies", "phantoms", "checkpoint", "awaiting", "paid", "stops",
            "closed", "outside", "scratch")


def reference():
    return json.loads((FIXTURES / "reference.json").read_text(encoding="utf-8"))


# --- the backlog ----------------------------------------------------------------------------------------------

def build_issues(marked=False, hide=()):
    """-> the fixture as fake-gh issues. `marked=True` puts the canonical `Blocked by` line for every reference
    dependency at the top of the blocked issue's body, which is what `deps.py mark` would write (used by controls
    only). `hide=((blocked, blocker), ...)` then removes every sentence and comment that names that blocker, so the
    wait is written nowhere."""
    spec = json.loads((FIXTURES / "issues.json").read_text(encoding="utf-8"))
    ref = reference()
    out = []
    for s in spec["issues"]:
        body = s["body"] + ("\n\n" + DW if s["done_when"] else "\n")
        hidden = {b for a, b in hide if a == s["number"]}
        if marked:
            body = "".join("**Blocked by:** #%d\n" % d["blocker"] for d in ref["dependencies"]
                           if d["blocked"] == s["number"]) + body
        for b in hidden:
            body = "\n".join(l for l in body.splitlines() if "#%d" % b not in l) + "\n"
        d = tla.issue(s["number"], *s["labels"], assignees=tuple(s["assignees"]), state=s["state"], body=body)
        d["title"] = s["title"]
        if s.get("state_reason"):
            d["state_reason"] = s["state_reason"]
        kept = [c for c in s["comments"] if not any("#%d" % b in c["body"] for b in hidden)]
        d["_comments"] = [{"body": c["body"], "user": {"login": c["login"]}, "author_association": c["assoc"],
                           "created_at": "2026-01-%02dT00:00:00Z" % (k + 1)} for k, c in enumerate(kept)]
        out.append(d)
    return out


class RegFake(tca.ChainFake):
    """The chain fake, plus the two moves a person or a goal session makes in the world."""

    def unpark(self, n):
        self._set(n, self._names(n) - {"sdlc:parked"})

    def park(self, n, kind):
        self._set(n, self._names(n) | {"sdlc:parked"})
        self.by[n]["_comments"].append({
            "body": "Parked by Sigma: scripted goal session.\nsigma-qkind: %s" % kind, "user": {"login": "alice"},
            "author_association": "OWNER", "created_at": "2026-02-%02dT00:00:00Z" % (len(self.by[n]["_comments"]) + 1)})


class RefPick:
    """The loop's scoped pick as the driver sees it: label scoped, leased, assigned to the runner, one claim per
    cycle, in the record's order, never a parked or upper-tier goal, and a goal waits while any REFERENCE dependency
    of it is open (not what Chain read)."""

    def __init__(self, fk, base, goal, ref):
        self.fk, self.base, self.goal, self.runner = fk, base, goal, ref["runner"]
        self.waits = {}
        for d in ref["dependencies"]:
            self.waits.setdefault(d["blocked"], []).append(d["blocker"])
        self.claims, self.labels = [], []

    def __call__(self, label, skip, **_kw):
        self.labels.append(label)
        rec = json.loads(pathlib.Path(self.base, "state", "chains", "c%d.json" % self.goal).read_text())
        order = [r["number"] for r in rec["members"]]
        order += [n for n in self.fk.by if n not in order]
        for n in order:
            issue = self.fk.by[n]
            names = {x["name"] for x in issue["labels"]}
            logins = {a["login"] for a in issue["assignees"]}
            if issue["state"] == "closed" or (label and label not in names) or n in self.claims \
                    or str(n) in skip or "sdlc:goal" not in names or names & {"story", "sdlc:parked"} \
                    or self.runner not in logins \
                    or any(self.fk.by[w]["state"] != "closed" for w in self.waits.get(n, [])):
                continue
            self.claims.append(n)
            return [n]
        return []


class RegScenario(tcs.Scenario):
    """`tcs.Scenario` for any end goal: the scratch run, driven only through argv."""

    def __init__(self, tmp_path, goal, ch=None, issues=None):
        issues = issues or build_issues()
        super().__init__(tmp_path, ch, issues=issues)
        self.goal, self.ref = goal, reference()
        self.fk = RegFake(issues)
        cfg = json.loads(self.cfg_path.read_text())
        cfg["hard_stops"] = {"enabled": True}
        cfg["spend_approval"] = {"enabled": True, "approvers": ["alice"]}
        cfg["ledger"] = {"enabled": False}
        self.cfg_path.write_text(json.dumps(cfg))
        self.pick = RefPick(self.fk, self.base, goal, self.ref)

    def step(self, verb, *rest, log=True, **kw):
        argv = ["chain.py", verb, self.base, str(self.goal), *rest]
        kw.setdefault("pick", self.pick if verb == "run" else None)
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            self.rc = self.ch.main(argv, run=self.fk, now=tcs.NOW + len(self.gestures), **kw)
        self.out = buf.getvalue() + err.getvalue()
        if log:
            shown = ["<sdlc_dir>" if a == self.base else a for a in argv[1:]]
            shown = ["<file>" if a.endswith("answers.json") else a for a in shown]
            line = "chain.py " + " ".join(shown)
            if not self.gestures or self.gestures[-1] != line:
                self.gestures.append(line)
        return self.rc

    def record(self):
        return json.loads(pathlib.Path(self.base, "state", "chains", "c%d.json" % self.goal).read_text())


# --- the scripted owner ---------------------------------------------------------------------------------------

def _choose(options, words):
    """The first option, in the order of `words`, that contains a word; None when none does."""
    low = [o.lower() for o in options]
    for w in words:
        for o, lo in zip(options, low):
            if w in lo:
                return o
    return None


def owner_answers(questions, rec, ref):
    """-> (answers {id: answer}, unanswerable [id]) for the open questions, from the reference only.

    Order of authority: the exact id; the cap; the member the id ends in; the kind; and, for a question that asks the
    owner to confirm a wait the engine could not read (it names both ends of a hard reference dependency that is not
    an edge), the reference's confirm words. A question none of these answers is returned as unanswerable: the
    reference must be extended for it, and the scenario fails by name until it is."""
    own, answers, left = ref["owner"], {}, []
    edges = {(e["from"], e["to"]) for e in rec.get("edges") or []}
    for q in questions:
        qid, tail = q["id"], q["id"].split(":", 1)[-1]
        got = None
        if qid in own["by_id"]:
            got = _choose(q["options"], own["by_id"][qid])
        elif q["kind"] == "cap":
            got = own["cap"]
        elif tail.isdigit() and tail in own["by_member"]:
            got = _choose(q["options"], own["by_member"][tail])
        elif q["kind"] in own["by_kind"]:
            got = _choose(q["options"], own["by_kind"][q["kind"]])
        else:
            text = qid + " " + q["ask"]
            seen = {int(n) for n in re.findall(r"#(\d+)", text)}
            if any({d["blocked"], d["blocker"]} <= seen and (d["blocker"], d["blocked"]) not in edges
                   for d in ref["dependencies"]):
                got = _choose(q["options"], own["confirm_a_wait"])
        if got is None:
            left.append(qid)
        else:
            answers[qid] = got
    return answers, left


# --- the scenario ---------------------------------------------------------------------------------------------

def _stop(sc, closed_since):
    run = sc.record()["run"]
    detail = run.get("reason") or ""
    return {"reason": run.get("stuck") or run.get("state"), "detail": detail,
            "mentions": sorted({int(n) for n in re.findall(r"#(\d+)", detail)}), "closed": sorted(closed_since)}


def _act(sc, n):
    """The scripted goal session for claimed goal `n`: close it, or park it with the reference's kind."""
    mode = sc.ref["sessions"].get(str(n), sc.ref["sessions"]["default"])
    if mode == "close":
        sc.close(n)
        return True
    sc.fk.park(n, mode.split(":", 1)[1])
    return False


def run_scenario(tmp_path, goal, ch=None, marked=False, hide=(), with_forecast=None):
    """Plan, sheet, answer, approve, arm, run to the end for one end goal -> an outcome dict (never raises on a
    refusal: `reached` says how far it got, and `criteria` says what that means)."""
    sc = RegScenario(tmp_path, goal, ch, issues=build_issues(marked, hide))
    spawned = []
    real_run, real_popen = subprocess.run, subprocess.Popen

    def note_run(argv, *a, **k):
        spawned.append(argv if isinstance(argv, str) else " ".join(map(str, argv)))
        return real_run(argv, *a, **k)

    def note_popen(argv, *a, **k):
        spawned.append(argv if isinstance(argv, str) else " ".join(map(str, argv)))
        return real_popen(argv, *a, **k)

    initial = {n: sorted(sc.fk._names(n)) for n in sc.fk.by}
    out = {"goal": goal, "reached": [], "refused": None, "sheet": "", "forecast": None, "after_defaults": None,
           "answers": {}, "unanswerable": [], "plan": None, "claims": [], "closed_by_session": [], "stops": [],
           "cycles": 0, "initial": initial, "final": None, "spawned": spawned, "files": [], "writes": [],
           "claims_labels": []}
    sub = pytest.MonkeyPatch()
    sub.setattr(subprocess, "run", note_run)
    sub.setattr(subprocess, "Popen", note_popen)
    try:
        _drive(sc, out, with_forecast)
    finally:
        sub.undo()
    out["final"] = {n: sorted(sc.fk._names(n)) for n in sc.fk.by}
    out["state"] = {n: sc.fk.by[n]["state"] for n in sc.fk.by}
    out["claims"] = list(sc.pick.claims)
    out["claims_labels"] = sorted(set(sc.pick.labels))
    out["writes"] = [list(w) for w in sc.fk.writes()]
    out["files"] = sorted(str(p.relative_to(tmp_path)) for p in pathlib.Path(tmp_path).rglob("*") if p.is_file())
    out["gestures"] = list(sc.gestures)
    return out


def _drive(sc, out, with_forecast):
    ref = sc.ref

    def stage(name, verb, *rest, log=True):
        if sc.step(verb, *rest, log=log) != 0:
            out["refused"] = "%s: exit %s: %s" % (name, sc.rc, sc.out.strip()[-400:])
            return False
        out["reached"].append(name)
        return True

    if not stage("plan", "plan"):
        return
    out["plan"] = sc.record()
    if with_forecast:
        sc.step("plan", "--forecast", log=False)
        out["forecast"] = sc.out
        sc.step("plan", log=False)
    if not stage("sheet", "sheet"):
        return
    out["sheet"] = sc.out
    if not stage("answer-defaults", "answer", "--defaults"):
        return
    config = sc.ch._load("state").load_config(sc.base)
    rec = sc.record()
    qs, openq = sc.ch.open_questions(rec, config)
    out["after_defaults"] = {"state": rec.get("state"), "open": [{"id": q["id"], "ask": q["ask"]} for q in openq]}
    answers, left = owner_answers(openq, rec, ref)
    out["answers"], out["unanswerable"] = answers, left
    if answers:
        f = sc.tmp / "answers.json"
        f.write_text(json.dumps(answers))
        if not stage("answer", "answer", "--answers", str(f)):
            return
    if not stage("approve", "approve") or not stage("arm", "arm"):
        return
    segment = []
    last = None
    for _ in range(MAX_CYCLES):
        before = len(sc.pick.claims)
        sc.step("run")
        out["cycles"] += 1
        run = sc.record()["run"]
        for n in sc.pick.claims[before:]:
            if _act(sc, n):
                out["closed_by_session"].append(n)
                segment.append(n)
        if run["state"] == "done":
            break
        if run["state"] in ("stuck", "capped"):
            stop = _stop(sc, segment)
            out["stops"].append(stop)
            segment = []
            root = next((r for r in ref["resolutions"] if int(r) in stop["mentions"]), None)
            if root is None or (stop["reason"], root) == last:
                break
            last = (stop["reason"], root)
            getattr(sc.fk, ref["resolutions"][root])(int(root))
    out["reached"].append("run")
    sc.step("status", log=True)
    out["status_rc"] = sc.rc


# --- the criteria ---------------------------------------------------------------------------------------------

def _blocks(sheet):
    """The sheet's decision blocks and its other non-order lines, each as one string."""
    out, cur = [], None
    for line in sheet.splitlines():
        if re.match(r"\s+\d+\. \[", line):
            cur = [line]
            out.append(cur)
        elif cur is not None and line.startswith("     "):
            cur.append(line)
        else:
            cur = None
            if line.strip() and not re.match(r"\s+\d+\. #\d+ ", line):
                out.append([line])
    return ["\n".join(b) for b in out]


def _mentions(text):
    return {int(n) for n in re.findall(r"#(\d+)", text)}


def criteria(out, ref=None):
    """-> {criterion: [failure, ...]} for an outcome. Pure. An empty list is a pass; each criterion is judged alone
    so that a red run reads as a checklist, and the first failure does not hide the rest."""
    ref = ref or reference()
    goal, chain = out["goal"], ref["chains"][str(out["goal"])]
    bad = {c: [] for c in CRITERIA}
    reached = out["reached"]
    if reached[-1:] != ["run"]:
        bad["reached"].append("the scenario stopped after %s: %s" % (reached[-1] if reached else "nothing",
                                                                      out["refused"]))
    if out["unanswerable"]:
        bad["owner"].append("the sheet asks questions the reference has no answer for: %s" % out["unanswerable"])
    plan = out["plan"] or {}
    members = {r["number"] for r in plan.get("members") or []}
    want = set(chain["members"])
    if want - members:
        bad["members"].append("planned members miss %s" % sorted(want - members))
    if members & set(ref["outside"]):
        bad["members"].append("planned members hold goals outside the chain: %s" % sorted(members & set(ref["outside"])))
    edges = {(e["from"], e["to"]) for e in plan.get("edges") or []}
    asked = _blocks(out["sheet"])
    for d in ref["dependencies"]:
        if d["blocked"] not in want | {goal}:
            continue
        if (d["blocker"], d["blocked"]) in edges:
            continue
        if any(d["blocker"] in _mentions(b) and _mentions(b) & ({d["blocked"]} | set(d.get("via") or [])) for b in asked):
            continue
        bad["dependencies"].append("#%d waits on #%d (%s): neither an edge in the plan nor a question on the sheet"
                                   % (d["blocked"], d["blocker"], d["form"]))
    for p in ref["not_dependencies"]:
        if (p["blocker"], p["blocked"]) in edges:
            bad["phantoms"].append("#%d is not a wait of #%d, yet the plan holds that edge" % (p["blocker"], p["blocked"]))
    claims = out["claims"]
    for n in ref["checkpoints"]:
        if n in claims:
            bad["checkpoint"].append("the checkpoint #%d was claimed as work" % n)
        if "sdlc:goal" in out["final"].get(n, []) and "sdlc:goal" not in out["initial"].get(n, []):
            bad["checkpoint"].append("the checkpoint #%d was armed (given sdlc:goal)" % n)
    asked_defaults = (out["after_defaults"] or {}).get("open") or []
    for n, how in ref["awaiting_confirmation"].items():
        n = int(n)
        if n in want | {goal}:
            if not any(n in _mentions(q["id"] + " " + q["ask"]) or q["id"].endswith(":%d" % n) for q in asked_defaults):
                bad["awaiting"].append("`answer --defaults` left no open question about the follow-up awaiting "
                                       "confirmation #%d: a default decided for the owner" % n)
        got = "sdlc:goal" in out["final"].get(n, []) and "sdlc:goal" not in out["initial"].get(n, [])
        if got and how != "promote":
            bad["awaiting"].append("#%d was made workable (sdlc:goal) though the owner's answer is %r" % (n, how))
        if how != "promote" and n in claims:
            bad["awaiting"].append("#%d was claimed though the owner's answer is %r" % (n, how))
    for n in ref["paid"]:
        if n in claims:
            bad["paid"].append("the paid goal #%d was claimed though the owner answered 'do not'" % n)
    got_stops = out["stops"]
    if len(got_stops) != len(chain["stops"]):
        bad["stops"].append("expected %d stop(s), the run had %d: %s" % (
            len(chain["stops"]), len(got_stops), [(s["reason"], s["mentions"]) for s in got_stops]))
    for want_stop, got in zip(chain["stops"], got_stops):
        if want_stop["root"] not in got["mentions"]:
            bad["stops"].append("a stop should name #%d (%s); it said %s: %r" % (
                want_stop["root"], want_stop["why"], got["reason"], got["detail"]))
        elif got["reason"] not in want_stop["reasons"]:
            bad["stops"].append("the stop at #%d has reason %r, expected one of %s" % (
                want_stop["root"], got["reason"], want_stop["reasons"]))
        if sorted(got["closed"]) != sorted(want_stop["closed"]):
            bad["closed"].append("before the stop at #%d the sessions closed %s, expected %s" % (
                want_stop["root"], sorted(got["closed"]), sorted(want_stop["closed"])))
    outside = set(ref["outside"])
    if outside & set(claims):
        bad["outside"].append("goals outside the chain were claimed: %s" % sorted(outside & set(claims)))
    for n in outside:
        if out["final"].get(n) != out["initial"].get(n) or out["state"].get(n) != "open":
            bad["outside"].append("the outside goal #%d was changed" % n)
    if out["claims_labels"] and out["claims_labels"] != ["chain:%d" % goal]:
        bad["outside"].append("the pick was scoped to %s, not chain:%d" % (out["claims_labels"], goal))
    for w in out["writes"]:
        if w[0] == "api" and not str(w[1]).startswith("repos/%s/" % REPO) and "graphql" not in str(w[1]):
            bad["scratch"].append("a write left the scratch repository: %s" % " ".join(map(str, w[:3])))
    if out["spawned"]:
        bad["scratch"].append("a process was started: %s" % out["spawned"][:2])
    ledger = [f for f in out["files"] if "ledger" in f.lower()]
    if ledger:
        bad["scratch"].append("ledger files appeared: %s" % ledger[:3])
    stray = [f for f in out["files"] if not f.startswith(".sdlc/") and f != "answers.json"]
    if stray:
        bad["scratch"].append("files appeared outside the scratch .sdlc: %s" % stray[:3])
    if out.get("forecast") is not None:
        bad["forecast"] = forecast_failures(out["forecast"], out, chain)
    return bad


def forecast_failures(text, out, chain):
    """The forecast line must equal the actual first segment of the run: goals finished, members, and the root."""
    m = FORECAST_LINE.search(text or "")
    if not m:
        return ["`plan --forecast` printed no line of the form 'can finish K of N before it stops on <reason>'"]
    first = chain["stops"][0]
    k, n, why = int(m.group(1)), int(m.group(2)), m.group(3)
    errs = []
    got_k = len(out["stops"][0]["closed"]) if out["stops"] else len(out["closed_by_session"])
    if k != got_k:
        errs.append("forecast says %d goals finish, the run finished %d before its first stop" % (k, got_k))
    if n != len(out["plan"]["members"]):
        errs.append("forecast counts %d members, the plan holds %d" % (n, len(out["plan"]["members"])))
    if "#%d" % first["root"] not in why:
        errs.append("forecast names %r as the first stop, the reference root is #%d" % (why, first["root"]))
    return errs


def failed(bad):
    return {c: v for c, v in bad.items() if v}


def render_failed(bad):
    return "\n".join("[%s] %s" % (c, "\n    ".join(v)) for c, v in failed(bad).items())


# --- the tests ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("goal", [1, 11])
def test_regression_scenario_end_to_end(tmp_path, goal):
    """Plan, sheet, answer, approve, arm, run to the end for an end goal; every criterion of the reference holds.

    RED on 1.0.17 by design (see the module docstring); it goes green when the dependency-reading, checkpoint,
    awaiting-confirmation, closed-prerequisite and hold slices have landed."""
    out = run_scenario(tmp_path, goal)
    bad = criteria(out)
    assert not failed(bad), "chain regression scenario for end goal #%d:\n%s" % (goal, render_failed(bad))


@pytest.mark.parametrize("goal", [1, 11])
def test_forecast_equals_actual_run(tmp_path, goal):
    out = run_scenario(tmp_path, goal, with_forecast=True)
    errs = forecast_failures(out["forecast"], out, reference()["chains"][str(goal)])
    assert not errs, errs


def perfect(goal):
    """A synthetic outcome that meets the reference on every criterion (for the checker's own controls)."""
    ref = reference()
    chain = ref["chains"][str(goal)]
    nums = {int(i["number"]) for i in json.loads((FIXTURES / "issues.json").read_text())["issues"]}
    initial = {n: [] for n in nums}
    final = {n: [] for n in nums}
    state = {n: "open" for n in nums}
    deps = [d for d in ref["dependencies"] if d["blocked"] in set(chain["members"]) | {goal}]
    closed = [n for s in chain["stops"] for n in s["closed"]]
    final.update({n: ["sdlc:goal"] for n in closed})
    for n in closed:
        state[n] = "closed"
    awaiting = [int(n) for n in ref["awaiting_confirmation"] if int(n) in chain["members"]]
    return {"goal": goal, "reached": ["plan", "sheet", "answer-defaults", "answer", "approve", "arm", "run"],
            "refused": None, "sheet": "", "forecast": None,
            "after_defaults": {"state": "awaiting-answers",
                               "open": [{"id": "confirm:%d" % n, "ask": "follow-up #%d is awaiting you" % n}
                                        for n in awaiting]},
            "answers": {}, "unanswerable": [],
            "plan": {"members": [{"number": n} for n in chain["members"]],
                     "edges": [{"from": d["blocker"], "to": d["blocked"], "tag": "explicit"} for d in deps]},
            "claims": list(closed), "closed_by_session": list(closed),
            "stops": [{"reason": s["reasons"][0], "detail": "#%d is stuck" % s["root"], "mentions": [s["root"]],
                       "closed": list(s["closed"])} for s in chain["stops"]],
            "cycles": 5, "initial": initial, "final": final, "state": state, "spawned": [],
            "files": [".sdlc/state/chains/c%d.json" % goal, ".sdlc/config.json"], "writes": [],
            "claims_labels": ["chain:%d" % goal]}


def _int_keys(o):
    for k in ("initial", "final", "state"):
        o[k] = {int(n): v for n, v in o[k].items()}
    return o


@pytest.mark.parametrize("goal", [1, 11])
def test_checker_controls(goal):
    """The checker passes the synthetic perfect outcome, and each clause broken ALONE turns its own criterion red
    (and no unrelated one): a checker never seen red proves nothing."""
    ref = reference()
    chain = ref["chains"][str(goal)]
    assert not failed(criteria(perfect(goal), ref)), render_failed(criteria(perfect(goal), ref))
    checkpoint, paid, outside = ref["checkpoints"][0], ref["paid"][0], ref["outside"][0]
    leave_out = [int(n) for n, how in ref["awaiting_confirmation"].items()
                 if how != "promote" and int(n) in chain["members"]]

    def drop_edge(o):
        o["plan"]["edges"] = o["plan"]["edges"][1:]

    def add_phantom(o):
        p = ref["not_dependencies"][0]
        o["plan"]["edges"].append({"from": p["blocker"], "to": p["blocked"], "tag": "explicit"})

    def drop_member(o):
        o["plan"]["members"] = o["plan"]["members"][1:]

    def claim_checkpoint(o):
        o["claims"].append(checkpoint)

    def arm_checkpoint(o):
        o["final"][checkpoint] = ["sdlc:goal"]

    def defaults_decide(o):
        o["after_defaults"]["open"] = []

    def arm_left_out(o):
        o["final"][leave_out[0]] = ["sdlc:goal"]

    def claim_left_out(o):
        o["claims"].append(leave_out[0])

    def claim_paid(o):
        o["claims"].append(paid)

    def wrong_root(o):
        o["stops"][0]["mentions"] = [999]

    def wrong_reason(o):
        o["stops"][0]["reason"] = "claimed"

    def a_stop_is_missing(o):
        o["stops"] = o["stops"][1:]

    def wrong_closed(o):
        o["stops"][0]["closed"] = o["stops"][0]["closed"][1:]

    def plan_outside(o):
        o["plan"]["members"].append({"number": outside})

    def close_outside(o):
        o["state"][outside] = "closed"

    def claim_outside(o):
        o["claims"].append(outside)

    def touch_outside(o):
        o["final"][outside] = ["chain:%d" % goal]

    def wrong_scope(o):
        o["claims_labels"] = ["chain:999"]

    def escape_write(o):
        o["writes"] = [["api", "repos/other/repo/issues/1/labels", "--method", "POST"]]

    def spawn(o):
        o["spawned"] = ["git status"]

    def ledger(o):
        o["files"].append(".sdlc/ledger/x.jsonl")

    def stray(o):
        o["files"].append("elsewhere.txt")

    def unanswered(o):
        o["unanswerable"] = ["thing:1"]

    def stopped_early(o):
        o["reached"] = o["reached"][:-1]
        o["refused"] = "approve: exit 2"

    cases = [("dependencies", drop_edge, ()), ("phantoms", add_phantom, ()), ("members", drop_member, ()),
             ("members", plan_outside, ()),
             ("checkpoint", claim_checkpoint, ()), ("checkpoint", arm_checkpoint, ()),
             ("paid", claim_paid, ()), ("stops", wrong_root, ()), ("stops", wrong_reason, ()),
             ("stops", a_stop_is_missing, ("closed",)), ("closed", wrong_closed, ()),
             ("outside", claim_outside, ()), ("outside", touch_outside, ()), ("outside", close_outside, ()),
             ("outside", wrong_scope, ()),
             ("scratch", escape_write, ()), ("scratch", spawn, ()), ("scratch", ledger, ()),
             ("scratch", stray, ()), ("owner", unanswered, ()), ("reached", stopped_early, ())]
    if leave_out:
        cases += [("awaiting", defaults_decide, ()), ("awaiting", arm_left_out, ()), ("awaiting", claim_left_out, ())]
    for expect, fn, extra in cases:
        o = _int_keys(json.loads(json.dumps(perfect(goal))))
        fn(o)
        got = failed(criteria(o, ref))
        assert expect in got, "end goal %d: breaking %s did not turn %s red: %s" % (goal, fn.__name__, expect, got)
        assert set(got) <= {expect, *extra}, "%s broke more than %s: %s" % (fn.__name__, expect, sorted(got))


def test_checker_counts_a_question_as_finding_a_wait():
    """A wait that is not an edge passes when the sheet asks about it (a decision block or a warning line naming both
    ends, or the blocker and the closed prerequisite it hides behind), and fails when only the ordered member list
    names the pair, because that list shows what the engine already holds."""
    ref = reference()
    base = perfect(1)
    base["plan"]["edges"] = [e for e in base["plan"]["edges"] if (e["from"], e["to"]) not in {(9, 3), (16, 2)}]
    assert len(failed(criteria(base, ref))["dependencies"]) == 2
    order_only = "Order, deepest first:\n  1. #9 armed\n  2. #3 unarmed; waits on #7 and #9\n"
    assert len(failed(criteria(dict(base, sheet=order_only), ref))["dependencies"]) == 2
    block = ("Decisions (2 open of 2):\n  1. [wait:3] #3 may wait on #9: does it?\n     options: it waits / no\n"
             "Warning: #16 is open and is the follow-up of closed #15, which #2 waited on\n")
    assert "dependencies" not in failed(criteria(dict(base, sheet=block), ref))
    via = "Warning: #16 is open and is the follow-up of closed #15\n"          # names the blocker and the closed link
    got = failed(criteria(dict(base, sheet=via), ref))["dependencies"]
    assert len(got) == 1 and "#3 waits on #9" in got[0], got
    for lone in ("  1. [wait:9] something about #9 alone\n     options: yes / no\n",     # the blocker alone
                 "  1. [wait:3] something about #3 alone\n     options: yes / no\n"):   # the blocked issue alone
        assert len(failed(criteria(dict(base, sheet=lone), ref))["dependencies"]) == 2, lone
    one = "Decisions (1 open of 1):\n  1. [wait:3] #3 may wait on #9: does it?\n     options: it waits / no\n"
    got = failed(criteria(dict(base, sheet=one), ref))["dependencies"]
    assert len(got) == 1 and "#2 waits on #16" in got[0], got


def test_ref_pick_honors_reference_dependencies(tmp_path):
    """The fake pick claims a goal only when it is open, labelled, armed, assigned to the runner, not parked, not
    skipped and every REFERENCE dependency of it is closed; it never leases twice. Drained with every claim closed,
    it claims exactly the goals that can run without a person, a landing or another login."""
    ref = reference()
    fk = RegFake(build_issues())
    state = tmp_path / "state" / "chains"
    state.mkdir(parents=True)
    (state / "c1.json").write_text(json.dumps({"members": [{"number": n} for n in sorted(fk.by)]}))
    for n in fk.by:
        if n not in ref["outside"]:                      # the outside goals are armed but not in the chain
            fk._set(n, fk._names(n) | {"chain:1"})
    pick = RefPick(fk, str(tmp_path), 1, ref)
    assert pick("chain:1", {"5"}) != [5], "a skipped goal was claimed"
    pick.claims.clear()

    def drain():
        got = []
        while True:
            one = pick("chain:1", set())
            if not one:
                return got
            got += one
            fk.by[one[0]]["state"] = "closed"

    fk.by[18]["state"] = "closed"                        # the other login's #9 now waits on nothing, yet is not ours
    fk.by[14]["state"] = "closed"                        # finished elsewhere: armed and labelled, but closed
    a, b = pick("chain:1", set()), pick("chain:1", set())
    assert a and b and a != b, "a goal was leased twice"
    for n in a + b:
        fk.by[n]["state"] = "closed"
    first = a + b + drain()
    assert sorted(first) == [5, 7, 10], first
    fk.by[9]["state"] = "closed"                         # and with #9 done, the unarmed #3 waits on nothing either
    assert drain() == [], "an unarmed goal was claimed"
    assert sorted(pick.claims) == [5, 7, 10] and len(set(pick.claims)) == len(pick.claims), "leased twice"
    assert pick.labels and set(pick.labels) == {"chain:1"}
    fk.unpark(8)
    assert "sdlc:parked" not in fk._names(8)
    assert drain() == [8, 4], "the unparked goal, then the goal that waited on it, are claimable"
    fk.park(14, "needs_decision")
    assert "sdlc:parked" in fk._names(14) and "sigma-qkind: needs_decision" in fk.by[14]["_comments"][-1]["body"]


def test_hide_removes_every_mention_of_the_blocker():
    """`hide` leaves no sentence, marker or comment of the blocked issue that names the blocker, and touches no other."""
    plain, hidden = {d["number"]: d for d in build_issues(marked=True)}, {d["number"]: d
                                                                          for d in build_issues(marked=True, hide=((3, 9),))}
    assert "#9" in plain[3]["body"] + json.dumps(plain[3]["_comments"])
    assert "#9" not in hidden[3]["body"] + json.dumps(hidden[3]["_comments"])
    assert "#7" in hidden[3]["body"], "a different blocker was removed"
    assert all(plain[n]["body"] == hidden[n]["body"] and plain[n]["_comments"] == hidden[n]["_comments"]
               for n in plain if n != 3)
    other = {d["number"]: d for d in build_issues(marked=True, hide=((3, 18),))}
    assert "#18" in other[9]["body"], "hiding a pair stripped the blocker from an issue that is not the blocked one"


def test_control_a_hidden_dependency_is_reported(tmp_path):
    """The end-to-end criteria see a wait that nothing says. Every other dependency is written as the canonical
    `Blocked by` line (what `deps.py mark` writes), and the one wait of #3 on #9 is written nowhere: whatever the
    engine reads, it cannot find that edge and cannot ask about it, so the dependencies criterion must name it."""
    out = run_scenario(tmp_path, 1, marked=True, hide=((3, 9),))
    bad = criteria(out)
    assert any("#3 waits on #9" in line for line in bad["dependencies"]), bad["dependencies"]
    # #9 is reachable only through that wait, so its own wait on #18 goes unseen with it: nothing else is missing
    assert all(line.startswith(("#3 waits on #9", "#9 waits on #18")) for line in bad["dependencies"]), \
        bad["dependencies"]


def test_marked_dependencies_close_the_edge_criteria(tmp_path):
    """With every dependency written as the canonical line, the plan holds each edge and no phantom: the
    dependency, member and phantom criteria are green on any engine that reads that form. What stays red there, on
    the engine as it stood, is the sibling slices' share (awaiting, paid, checkpoint): this test does not pin it."""
    for goal in (1, 11):
        out = run_scenario(tmp_path / str(goal), goal, marked=True)
        bad = criteria(out)
        for name in ("reached", "owner", "members", "dependencies", "phantoms", "outside", "scratch"):
            assert not bad[name], "end goal %d, %s: %s" % (goal, name, bad[name])
        assert out["stops"], "the run never stopped, so the scripted world was not exercised"
    # the scripted world applied the reference's resolution after the stop at #8 (a person unparked it), and the
    # goal was then claimed and closed by its session
    assert 8 in run_scenario(tmp_path / "again", 1, marked=True)["closed_by_session"]


@pytest.mark.parametrize("goal", [1, 11])
def test_gesture_is_the_documented_one(tmp_path, goal):
    """The verbs the scenario ran, in order, are the block docs/chain.md documents (copied from the doc, not typed
    here), with only the end goal's number differing. Holds on any engine that gets to `status`."""
    out = run_scenario(tmp_path, goal)
    ran = [re.sub(r"(<sdlc_dir>) %d$" % goal, r"\1 1", re.sub(r"(<sdlc_dir>) %d " % goal, r"\1 1 ", g))
           for g in out["gestures"]]
    assert ran == tcs._documented_gesture(), ran


def test_forecast_parser():
    """The forecast line is parsed the way the engine is to print it: 'can finish K of N before it stops on <why>'."""
    ref = reference()
    out = perfect(1)
    chain = ref["chains"]["1"]
    assert not forecast_failures("a run from alice can finish 2 of 11 before it stops on #8 (a decision)", out, chain)
    for text, what in (("a run from alice can finish 3 of 11 before it stops on #8", "goals finish"),
                       ("a run from alice can finish 2 of 12 before it stops on #8", "members"),
                       ("a run from alice can finish 2 of 11 before it stops on #9", "first stop"),
                       ("nothing useful", "printed no line")):
        errs = forecast_failures(text, out, chain)
        assert errs and what in errs[0], (text, errs)


def test_owner_answers_from_the_reference_only():
    """The owner answers by id, kind and member; a question it has no reference answer for is named, never guessed."""
    ref = reference()
    rec = {"edges": []}
    qs = [{"id": "label_set:c1", "kind": "label_set", "ask": "", "options": ["arm them", "leave them"]},
          {"id": "dependency:8", "kind": "dependency", "ask": "", "options": ["work around it", "wait for it"]},
          {"id": "irreversible:10", "kind": "irreversible", "ask": "", "options": ["go ahead", "do not"]},
          {"id": "cap:c1", "kind": "cap", "ask": "", "options": []},
          {"id": "novel:17", "kind": "novel", "ask": "", "options": ["close it", "promote it", "leave it out"]},
          {"id": "novel:18", "kind": "novel", "ask": "", "options": ["close it", "promote it", "leave it out"]},
          {"id": "novel:99", "kind": "novel", "ask": "", "options": ["a", "b"]}]
    got, left = owner_answers(qs, rec, ref)
    assert got == {"label_set:c1": "arm them", "dependency:8": "wait for it", "irreversible:10": "do not",
                   "cap:c1": "50", "novel:17": "promote it", "novel:18": "leave it out"}
    assert left == ["novel:99"]
    qs = [{"id": "wait:3", "kind": "wait", "ask": "#3 may wait on #9: does it?", "options": ["no", "it waits"]}]
    assert owner_answers(qs, rec, ref) == ({"wait:3": "it waits"}, [])
    assert owner_answers(qs, {"edges": [{"from": 9, "to": 3}]}, ref) == ({}, ["wait:3"])


# --- #1940: five more dependency wordings, each read through the documented `plan` and `sheet` verbs ------------

#: (form, goal 1's body, a trusted comment on goal 1, the issue number the plan must hold goal 1 waiting on)
WORDINGS_1940 = [
    ("slice-chain", "Fixture slices: S2a (#2), S2b (#3), S2c (#4), S2d (#1). S2a, S2b and S2c must precede S2d.", None, 2),
    ("slice-on", "Fixture slices: S4b on S4a (#2); S4a is a fixture.", None, 2),
    ("waiting-on", "Plain body.", "Still waiting on #2 before the paid run.", 2),
    ("gates", "Plain body.", "#2 gates #1.", 2),
    ("reversed-waits-on", "Plain body. This is what the story #2 waits on.", None, 2),
]


@pytest.mark.parametrize("form,body,comment,blocker", WORDINGS_1940, ids=[w[0] for w in WORDINGS_1940])
def test_1940_wording_is_an_unconfirmed_wait_on_the_sheet(tmp_path, form, body, comment, blocker):
    """Goal 1 carries the wording; the documented `chain.py plan` reads it as an unconfirmed wait (never an edge
    the pick gate holds) and `chain.py sheet` lists it, with the form named."""
    issues = [tla.issue(1, "sdlc:goal", assignees=("alice",), body=body + "\n\n" + DW,
                        comments=[comment] if comment else ()),
              tla.issue(2, "sdlc:goal", assignees=("alice",), body="Fixture issue 2.\n\n" + DW),
              tla.issue(3, "sdlc:goal", assignees=("alice",), body="Fixture issue 3.\n\n" + DW),
              tla.issue(4, "sdlc:goal", assignees=("alice",), body="Fixture issue 4.\n\n" + DW)]
    sc = RegScenario(tmp_path, 1, issues=issues)
    assert sc.step("plan") == 0, sc.out
    rec = sc.record()
    got = {(i["goal"], i["ref"], i["form"]) for i in rec["unconfirmed_waits"]}
    assert (1, blocker, form) in got, got
    assert sc.step("sheet") == 0, sc.out
    assert "Unconfirmed waits" in sc.out and "(%s;" % form in sc.out, sc.out
