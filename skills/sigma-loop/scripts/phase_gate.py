#!/usr/bin/env python3
"""The phase-record gate (#684): a goal is not `done` until the action log shows each SDLC phase.

WHY. The loop's one-subagent-per-phase rule was prose. A live run had one worker do every phase inline
and `record done` accepted it with no research, plan, plan-review or retro on record. Dispatch happens on
the HOST's side and no code here can see it, so this module does the one thing Python can do on every
host and in every mode: it reads the goal's own action-log file and REFUSES `done` unless the record
shows, in order, research, plan, plan-review (an approving verdict), implement (started after that
verdict), review (an approving verdict) and retro.

WHAT IT PROVES, AND WHAT IT DOES NOT. Rows are written by `phase_report.py start|end`, by
`work.py record-plan-review` and `work.py record-review`, and by `loop.py waive-phases`. A maker can call
those verbs itself, exactly as it can call `record-plan-review` today; so the record proves that a phase
boundary and a verdict were RECORDED, in order, bound to the plan's bytes. For a verdict whose route is
`subagent` (the host can dispatch) it also proves every phase named its own agent id, no id served two
phases, and, where the host's transcript store is readable at record time, that a transcript for each
phase's agent id exists. A reviewer resumed for a second verdict counts as the same agent (use a fresh one). On an `inline` route (the host cannot spawn one) none of that applies; the
record says INLINE and `loop.py phases` shows it. It does not
prove the phase's work was good. Research and retro are boundary-proof only. The review verdict is bound
to no code state (the verify gate refuses an edit after its green verify). The reviewer route comes from the
review config and environment, so whoever can edit them can also turn the different-agent checks off, exactly
as they can turn the gate off. When the transcript store
is not readable the verdict is stored `unverified` and `loop.py phases` says so.

LEVER. `loop.py waive-phases <sdlc> <goal> research,retro --reason ...` waives research and retro only
(plan, plan-review, implement and review can never be waived), recorded as `phase_waived` rows and
printed. `gates.phase_record.enabled: false` switches the gate off. Absent key = off (an adopted repo is
not retro-fitted); `/sigma-init` ships it true. Gate on with `action_log.enabled` not true REFUSES: it
cannot verify, and a half-guarantee is worse than a refusal.
"""
import importlib.util
import json
import os
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent

REQUIRED = ("research", "plan", "plan_review", "implement", "review", "retro")
WAIVABLE = ("research", "retro")
#: The verdict words stored on `verdict` rows. plan_review stores work.py's own mapped values.
PLAN_REVIEW_APPROVING = ("pass", "warn")
REVIEW_VERDICTS = {"APPROVE": "approve", "SEND-BACK": "send-back", "BLOCK": "block"}
REVIEW_APPROVING = ("approve",)


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def gate_on(config):
    """`gates.phase_record` through work.py's one truth table (`hard_plan_gate_on`). Absent = off."""
    work = _load("work")
    return work.hard_plan_gate_on(work._gate_block(config, "phase_record")) \
        if work._gate_block(config, "phase_record") is not None else False


def _cmd(script, args):
    return f'python3 "${{CLAUDE_SKILL_DIR}}/scripts/{script}" {args}'


_LEVER = ("lever: for a trivial goal, `loop.py waive-phases <sdlc> <goal> research,retro --reason "
          "\"<why>\"` (recorded, visible; only research and retro can be waived); the whole gate is "
          "`gates.phase_record.enabled: false` in .sdlc/config.json")


def _rows(sdlc_dir, goal):
    return _load("actionlog").read_goal(sdlc_dir, goal)


def _file_ok(path):
    try:
        return path is not None and pathlib.Path(path).stat().st_size > 0
    except OSError:
        return False


def verify_agent(agent_id):
    """('transcript'|'unverified', None) or (None, reason-to-refuse). Refuses only when the host's
    transcript store IS readable here and holds no transcript for `agent_id`."""
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID")
    if not sid:
        return "unverified", None
    pr = _load("phase_report")
    if pr.find_claude_session_dir(sid) is None:
        return "unverified", None
    if pr.find_claude_agent_transcript(sid, agent_id) is None:
        return None, (f"agent id {agent_id!r} has no transcript in this session's subagent store: "
                      "pass the id of the subagent that actually ran this phase")
    return "transcript", None


def status(sdlc_dir, config, goal):
    """-> {"rows": [(phase, ok, detail, fix)], "notes": [...]}. `ok` False rows are what `refusal`
    reports. Pure read; never raises on a malformed log (a bad row is skipped by `read_goal`)."""
    work = _load("work")
    rc = _load("review_context")
    rows = [r for r in _rows(sdlc_dir, goal)             # a malformed row is skipped, never a crash
            if all(isinstance(r.get(k, ""), str) for k in ("phase", "agent_id", "verdict", "state"))]
    waived = {r.get("phase") for r in rows if r.get("kind") == "phase_waived"} & set(WAIVABLE)
    why = {r.get("phase"): r.get("reason", "") for r in rows if r.get("kind") == "phase_waived"}
    last_end, first_start, verdict = {}, {}, {}
    for i, r in enumerate(rows):
        if r.get("kind") == "phase" and r.get("phase") in REQUIRED:
            if r.get("state") == "end":
                last_end[r["phase"]] = (i, r)
            elif r["phase"] not in first_start:
                first_start[r["phase"]] = i
        elif r.get("kind") == "verdict":
            verdict[r.get("phase")] = (i, r)
    ok_plan = [i for i, r in enumerate(rows) if r.get("kind") == "verdict"
               and r.get("phase") == "plan_review" and r.get("verdict") in PLAN_REVIEW_APPROVING]
    first_ok_plan, last_ok_plan = (ok_plan[0], ok_plan[-1]) if ok_plan else (None, None)
    out, notes = [], []
    start = lambda ph: _cmd("phase_report.py", f"start <sdlc> <goal> {ph} --model <tier> --pid \"$PPID\"")
    end = lambda ph: _cmd("phase_report.py", f"end <sdlc> <goal> {ph} --agent-id <agentId> --pid \"$PPID\"")
    for ph in REQUIRED:
        if ph in waived:
            out.append((ph, True, f"WAIVED: {why.get(ph) or 'no reason'}", ""))
            notes.append(f"{ph} waived ({why.get(ph) or 'no reason'})")
            continue
        if ph not in last_end:
            out.append((ph, False, "no phase end recorded",
                        f"dispatch ONE subagent for {ph}; bracket it: {start(ph)} ... {end(ph)}"))
            continue
        detail, fix, ok = "recorded", "", True
        if ph in ("research", "plan"):
            sub = "research" if ph == "research" else "plans"
            f = rc.phase_doc_file(sdlc_dir, goal, sub)
            if not _file_ok(f):
                ok, detail, fix = False, f"no non-empty .sdlc/{sub}/<goal>.md artifact", \
                    (f"the {ph} subagent must file .sdlc/{sub}/<goal-stem>.md in the MAIN checkout "
                     "(a copy only in the goal worktree does not count)")
        if ph in ("plan_review", "review"):
            vi, v = verdict.get(ph, (None, None))
            approving = PLAN_REVIEW_APPROVING if ph == "plan_review" else REVIEW_APPROVING
            if v is None:
                ok, detail = False, "no verdict recorded"
                fix = (_cmd("work.py", "record-plan-review <sdlc> <goal> --verdict SOUND|SOUND-WITH-REFINEMENTS|FIX-FIRST "
                            "--plan-sha256 <brief sha> --agent-id <reviewer agentId>") if ph == "plan_review" else
                       _cmd("work.py", "record-review <sdlc> <goal> --verdict APPROVE|SEND-BACK|BLOCK "
                            "--agent-id <reviewer agentId>"))
            elif v.get("verdict") not in approving:
                ok, detail = False, f"last verdict is {v.get('verdict')!r}, not approving"
                fix = "fix what the reviewer found, then run a fresh independent review and record it"
            elif (rv := _load("reviewer")) and (failed := rv.failed_receipt(sdlc_dir, goal, ph)):
                ok, detail = False, f"the independent reviewer failed after the approving verdict ({failed[0]})"
                fix = (f"park the goal (`--qkind reviewer_down`), fix the reviewer, and re-run `reviewer.py run` "
                       f"for {ph}; an earlier approval is not restored by a failed re-run")
            elif live := rv.running_receipt(sdlc_dir, goal, ph):
                ok, detail = False, "the independent reviewer is still running"
                fix = f"wait for it: `{rv._wait_gesture(sdlc_dir, goal, ph, live.get('run_id'))}`"
            else:
                detail = f"verdict {v.get('verdict')} via {v.get('route') or 'unknown'} route" + (
                    f", agent {v['agent_id']} ({v.get('verified') or 'unverified'})" if v.get("agent_id") else "")
                if v.get("route") == "subagent":
                    if not v.get("agent_id"):
                        ok, detail, fix = False, "subagent route but no reviewer agent id", \
                            "re-record with --agent-id <reviewer agentId>"
                    elif any(r.get("agent_id") == v["agent_id"] and r.get("phase") != ph
                             for r in rows if r.get("kind") in ("phase", "verdict")):
                        ok, detail, fix = False, "the reviewer agent id is also another phase's agent id " \
                            "(the maker is the checker)", "dispatch a FRESH subagent for the review"
                    if v.get("verified") == "unverified":
                        notes.append(f"{ph}: reviewer id could not be checked against the host's transcript store")
                if v.get("route") == "inline" and v.get("waiver"):
                    notes.append(f"{ph}: INLINE by owner decision: {v.get('waiver_reason') or 'no reason recorded'}")
                elif v.get("route") == "inline":
                    notes.append(f"{ph}: INLINE route (the host cannot spawn an independent reviewer)")
                if ok and ph == "plan_review":
                    plan = rc.phase_doc_file(sdlc_dir, goal, "plans")
                    if plan and v.get("plan_hash") and rc.plan_sha256(plan) != v["plan_hash"]:
                        ok, detail, fix = False, "the plan was edited after its review", \
                            "run a fresh plan-review of the current plan and record that verdict"
        if ok and ph == "implement" and first_ok_plan is not None:
            first = first_start.get("implement", last_end["implement"][0])
            if first < first_ok_plan:
                ok, detail, fix = False, "implement started BEFORE the plan-review verdict", \
                    "the claim is plan before code; run the loop in order (re-run implement after review)"
        out.append((ph, ok, detail, fix))
    # One subagent PER PHASE (#684 root cause). On a host that can dispatch (either verdict's route is
    # `subagent`), every recorded phase must name its own agent id, and no id may serve two phases:
    # this is what stops one worker doing the phases inline and calling `phase_report` back to back.
    dispatching = any(v is not None and v[1].get("route") == "subagent" for v in verdict.values())
    if dispatching:
        seen = {}
        for i, (ph, ok, detail, fix) in enumerate(out):
            if not ok or ph in waived or ph not in last_end:
                continue
            aid = last_end[ph][1].get("agent_id")
            if last_end[ph][1].get("verified") == "missing":
                out[i] = (ph, False, f"agent id {aid} has no transcript in this session's subagent store",
                          f"pass the id of the subagent that actually ran {ph}: {end(ph)}")
            elif not aid:
                out[i] = (ph, False, "ended with no agent id: this host dispatches subagents, so the phase "
                          "must run as its own subagent", f"dispatch a subagent for {ph}; {end(ph)}")
            elif aid in seen:
                out[i] = (ph, False, f"agent id {aid} also ran {seen[aid]} (one subagent per phase)",
                          f"dispatch a FRESH subagent for {ph}")
            else:
                seen[aid] = ph
    # Order: the chain research -> plan -> approved plan-review -> implement -> approved review -> retro.
    def at(ph):
        return last_end[ph][0] if ph in last_end else None
    rv = verdict.get("review")
    chain = (("research", at("research"), "plan", at("plan"), "research"),
             ("plan", at("plan"), "plan-review's approving verdict", last_ok_plan, "plan"),
             ("implement", at("implement"), "the review verdict", rv[0] if rv else None, "implement"),
             ("the review verdict", rv[0] if rv else None, "retro", at("retro"), "review"))
    for a_name, a, b_name, b, ph in chain:
        if a is not None and b is not None and a > b:
            for i, row in enumerate(out):
                if row[0] == ph and row[1]:
                    out[i] = (ph, False, f"{a_name} was recorded AFTER {b_name}",
                              "re-run the phases in order: a change after its review needs a fresh review")
    return {"rows": out, "notes": notes}


def start_refusal(sdlc_dir, goal, phase):
    """Refusal text when `phase_report.py start implement` runs before an approving plan-review verdict is
    on the record (the product claim: plan before code), else None. Only with the gate on and the action log
    on; any other phase starts freely. Refusing HERE, not only at `done`, lets the maker fix it before any
    code is written; a live run recorded the verdict seconds after starting implement."""
    if phase != "implement":
        return None
    config = _load("state").load_config(sdlc_dir)
    if not gate_on(config) or not _load("actionlog").enabled(config):
        return None
    if any(r.get("kind") == "verdict" and r.get("phase") == "plan_review"
           and r.get("verdict") in PLAN_REVIEW_APPROVING for r in _rows(sdlc_dir, goal)):
        return None
    return ("no approving plan-review verdict is recorded for this goal, so implement may not start (plan "
            "before code): run the plan-review, record its verdict with `work.py record-plan-review "
            "<sdlc> <goal> --verdict SOUND|SOUND-WITH-REFINEMENTS --plan-sha256 <brief sha> --agent-id "
            "<reviewer agentId>`, then start implement (plan-review cannot be waived)")


def refusal(sdlc_dir, config, goal):
    """None when the gate is off or satisfied; else the refusal text (names each fix and the lever)."""
    if not gate_on(config):
        return None
    if not _load("actionlog").enabled(config):
        return ("gates.phase_record is on but action_log.enabled is not true, so the phases cannot be "
                "verified: set action_log.enabled to true, or switch the gate off with "
                "gates.phase_record.enabled: false (REFUSED rather than passed unverified)")
    try:
        bad = [r for r in status(sdlc_dir, config, goal)["rows"] if not r[1]]
    except Exception as exc:                  # noqa: BLE001 - an unreadable record is a refusal, never a pass
        return (f"the SDLC phase record could not be read ({type(exc).__name__}: {exc}); repair or move "
                f".sdlc/state/log/<goal>.jsonl and re-record the phases ({_LEVER})")
    if not bad:
        return None
    lines = ["the SDLC phase record is incomplete (README: every goal runs its seven phases):"]
    for ph, _, detail, fix in bad:
        lines.append(f"  - {ph}: {detail}" + (f" -> {fix}" if fix else ""))
    lines.append("  " + _LEVER)
    return "\n".join(lines)


def format_status(sdlc_dir, config, goal):
    s = status(sdlc_dir, config, goal)
    out = []
    for ph, ok, detail, fix in s["rows"]:
        out.append(f"{'ok     ' if ok else 'MISSING'} {ph}: {detail}")
    nxt = next((r for r in s["rows"] if not r[1]), None)
    out.append(f"NEXT: {nxt[0]} -> {nxt[3] or nxt[2]}" if nxt else "NEXT: all phases recorded; run verify, then `record done`")
    out += [f"note: {n}" for n in s["notes"]]
    return "\n".join(out)


def record_verdict(sdlc_dir, config, goal, phase, verdict, agent_id="", plan_hash="", reason="", project_rules=""):
    """Write a `verdict` row (phase plan_review|review). Raises ValueError to refuse."""
    actionlog = _load("actionlog")
    if not actionlog.enabled(config):
        raise ValueError("action_log.enabled is not true: the verdict cannot be recorded in the action log")
    reviewer = _load("reviewer")
    route = reviewer.resolve(sdlc_dir).get("mechanism") or "inline"
    if verdict in (PLAN_REVIEW_APPROVING if phase == "plan_review" else REVIEW_APPROVING):
        # F02: a process/command route needs the runner's own receipt; a failed reviewer is no approval.
        refusal = reviewer.independent_run_refusal(sdlc_dir, goal, phase, plan_hash)
        if refusal:
            raise ValueError(refusal)
    verified = ""
    if agent_id:
        verified, bad = verify_agent(agent_id)
        if bad:
            raise ValueError(bad)
    if route == "subagent" and not agent_id:
        raise ValueError("the reviewer route here is `subagent`: pass --agent-id <the reviewer subagent's id>")
    if route == "subagent" and verdict in (PLAN_REVIEW_APPROVING if phase == "plan_review" else REVIEW_APPROVING):
        refusal = reviewer.subagent_transcript_refusal(sdlc_dir, goal, phase, verified)
        if refusal:
            raise ValueError(refusal)
    why = reviewer.waiver_reason(sdlc_dir, goal, phase, plan_hash) if route == "inline" else None
    row = actionlog.append(sdlc_dir, goal, "verdict", "loop", phase=phase, verdict=verdict, route=route,
                           waiver="accept-inline" if why else None, waiver_reason=why or None,
                           agent_id=agent_id or None, plan_hash=plan_hash or None,
                           verified=verified or None, project_rules=project_rules or None)
    return row


def waive(sdlc_dir, config, goal, phases, reason):
    actionlog = _load("actionlog")
    if not str(reason or "").strip():
        raise ValueError("--reason is required: a waiver is recorded and visible, never silent")
    bad = [p for p in phases if p not in WAIVABLE]
    if bad or not phases:
        raise ValueError(f"only {', '.join(WAIVABLE)} can be waived (not {', '.join(bad) or 'nothing'}): "
                         "plan, plan-review, implement and review always run")
    if not actionlog.enabled(config):
        raise ValueError("action_log.enabled is not true: the waiver cannot be recorded")
    for p in phases:
        actionlog.append(sdlc_dir, goal, "phase_waived", "loop", phase=p, reason=reason)
    return phases


def main(argv):
    if len(argv) >= 2 and argv[1] in ("-h", "--help"):
        print("usage: phase_gate.py status <sdlc_dir> <goal>   (the library behind `loop.py phases`)")
        return 0
    if len(argv) >= 4 and argv[1] == "status":
        cfg = _load("state").load_config(argv[2])
        print(format_status(argv[2], cfg, argv[3]))
        return 0
    print("usage: phase_gate.py status <sdlc_dir> <goal>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
