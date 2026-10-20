"""chain_enact.py -- what `chain.py arm` does for the answers on the sheet that are not a label (library only).

The first pass of `arm`, before the chain label and the arming: for each member the person answered about,

  release it         `unpark.resolve(..., "unpark")`, the one atomic gesture `/sigma-unpark` uses (+goal, -parked in one
                     swap, then the Q&A block and one comment); an undeclared kind is declared with the guess the
                     sheet recommended. A member that is no longer parked is skipped.
  take it over       assign the runner, then remove the old assignee(s): one assignee per issue. Never for a member
                     that is in progress in another session.
  confirm a wait    `deps.mark`, the write every reader trusts (`Blocked by #B` in the body, one note); skipped when
                     the body already carries the edge.
  dismiss a wait     the dismissal marker `backlog_check.dismiss_comment`, posted as `loop.py note` posts it; skipped
                     when a comment already carries it.

Every step reads the member's CURRENT state first, so a second `arm` finds nothing to do (idempotent), and a step that
fails is reported and stops nothing else (a re-run finishes it). With `dry_run` nothing is called that writes: the
source is handed a read-only runner and each step is only described. Nothing here runs before approval: `arm` itself
refuses before approval.
"""
import importlib.util
import pathlib

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


chain_asks = _load("chain_asks")
label_drain = _load("label_drain")
UNPARK_ANSWER = "start it again"


def _act(kind, n, detail):
    return {"action": kind, "issue": str(n), "detail": detail, "result": None, "error": None}


def _logins(issue):
    return [str(a.get("login") if isinstance(a, dict) else a) for a in issue.get("assignees") or [] if a]


def _finish(act, dry_run, do):
    """Describe the step (dry run), or run `do()` -> None on success, an error text on a refusal."""
    if dry_run:
        act["result"] = "would"
        return act
    try:
        err = do()
    except Exception as exc:        # noqa: BLE001 - reported, never raised: a re-run completes the rest
        err = "%s: %s" % (type(exc).__name__, str(exc)[:200])
    if err:
        act["result"], act["error"] = "failed", err
    else:
        act["result"] = "done"
    return act


def enact(sdlc_dir, config, rec, source, rd, dry_run):
    """-> (actions, refused, notes). `rd` is the runner the reads go through (read-only for a dry run)."""
    gh_api, unpark = _load("gh_api"), _load("unpark")
    backlog_check, blocker_scan, deps = _load("backlog_check"), _load("blocker_scan"), _load("deps")
    repo = source.repo or None
    by = {r["number"]: r for r in rec["members"]}
    actions, refused, notes = [], [], []

    def read(n):
        try:
            return gh_api.view_issue(rd, n, repo, comments="all")
        except Exception:           # noqa: BLE001 - say so rather than guess
            refused.append({"issue": n, "verdict": "unreadable", "reason": "could not read its current state"})
            return None

    for n in sorted(chain_asks.released(rec)):
        issue = read(n)
        if issue is None:
            continue
        names = label_drain._names(issue)
        if str(issue.get("state") or "").lower() == "closed":
            notes.append("#%d is closed; not released" % n)
            continue
        if source.parked_label not in names and source.goal_blocked_label not in names:
            continue                                            # already released: nothing to do
        row = by[n]
        kind = row.get("kind_guess") if row.get("kind") == "undeclared" else None
        kind = kind if kind and kind != "unknown" else None

        def release(n=n, kind=kind):
            r = unpark.resolve(sdlc_dir, config, n, {unpark.CLOSING_QUESTION["id"]: UNPARK_ANSWER}, "unpark",
                               source=source, apply=True, kind=kind)
            return None if r["outcome"] == "unparked" else (r["detail"] or r["outcome"])
        actions.append(_finish(_act("unpark", n, "release it through the unpark gesture"), dry_run, release))

    if chain_asks.taken_over(rec):
        me = rec.get("runner") or source._assignee_login()      # #1935: who the plan was made for
    for n in sorted(chain_asks.taken_over(rec)):
        issue = read(n)
        if issue is None:
            continue
        if source.in_progress_label in label_drain._names(issue):
            refused.append({"issue": n, "verdict": "in-progress", "reason":
                            "it is in progress in another session now; it was not taken over"})
            continue
        if not me:
            refused.append({"issue": n, "verdict": "assigned-elsewhere", "reason":
                            "the login to assign could not be resolved; nothing was changed"})
            continue
        logins = _logins(issue)
        add_me = me.casefold() not in {x.casefold() for x in logins}
        others = [x for x in logins if x.casefold() != me.casefold()]
        if not add_me and not others:
            continue

        def take(n=n, add_me=add_me, others=others):
            if add_me:
                source._issue_add_assignees(n, [me])
            if others:
                source._issue_remove_assignees(n, others)
            return None
        actions.append(_finish(_act("take-over", n, "assign %s%s" % (me, ", remove %s" % ", ".join(others)
                                                                      if others else "")), dry_run, take))

    for a, b in sorted(chain_asks.confirmed_waits(rec)):
        issue = read(a)
        if issue is None:
            continue
        if any(int(i["ref"]) == b for i in blocker_scan.extract_refs(issue.get("body") or "", self_ref=a)):
            continue                                            # the body already carries the edge

        def confirm(a=a, b=b):
            verdict, detail = deps.mark(source, a, b, "confirmed at the chain sheet")
            return None if verdict in ("MARKED", "ALREADY") else "%s: %s" % (verdict, detail)
        actions.append(_finish(_act("confirm-wait", a, "write Blocked by #%d" % b), dry_run, confirm))

    for a, b in sorted(chain_asks.dismissed_waits(rec)):
        issue = read(a)
        if issue is None:
            continue
        said = {(k, r) for c in issue.get("comments") or [] if isinstance(c, dict)
                for k, r in backlog_check._DISMISS_RE.findall(str(c.get("body") or ""))}
        if ("blocked-by", str(b)) in said:
            continue                                            # already dismissed

        def dismiss(a=a, b=b):
            ok = source.note(a, backlog_check.dismiss_comment("blocked-by", b))
            return "the dismissal comment was not posted" if ok is False else None
        actions.append(_finish(_act("dismiss-wait", a, "dismiss the wait on #%d" % b), dry_run, dismiss))
    return actions, refused, notes
