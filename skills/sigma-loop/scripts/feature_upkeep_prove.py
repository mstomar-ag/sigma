"""Level 1 of unit conflict resolution, and the proof that gates its push (upkeep part B, slice 5).

WHAT IT IS. Pure orchestration over pieces that already exist: the heading-aware changelog union (`work`), the stop
checks and the pairing proof (`conflict_proof`), the stamping commit and the record (`feature_upkeep_resolution`), the
index readers (`conflict_state`) and the shared verify runner (`bounded_run`). It reads no configuration (the caller
passes the settings it needs), it never pushes, and it makes no model call and no network call. It is loaded by path
from `feature_rebase.py`, which owns the gate query and the push.

LEVEL 1 IS NARROW ON PURPOSE. A stop is Level 1 only when exactly one path is unmerged, that path is the changelog,
and a base stage exists for it. Anything else is not Level 1 and the caller parks the unit as it always did.

THE ORDER. At each stop: snapshot the index, union, check the result would not be an empty commit, check that no stage-0
entry outside the conflicted set moved, that the resolved blob holds no more marker lines than the original commit's and
no new whitespace finding, then the stamping commit (the original's author, hooks and signing off) and a continuation
whose hooks path points at a directory that does not exist. After the rebase completes, `prove` runs ONCE, before any
push: every original accounted for, no edit outside the conflicted path, then the verify command. Every refusal is a
list entry with a code; none of them is an exception.

THE CONTINUATION'S HOOKS. `git commit --no-verify` does not stop the hooks `rebase --continue` itself fires
(`post-rewrite` and friends), so the continuation runs with `core.hooksPath` set to a directory that does not exist, for
that one command. The reverse of the usual risk is the point: an unmeasured hook is not allowed to run in an unattended
replay.
"""
import hashlib
import importlib.util
import pathlib

_HERE = pathlib.Path(__file__).resolve().parent
_LOADED = {}

CHANGELOG = "CHANGELOG.md"
ROUNDS = 3                                   # stops one pass may resolve, mirroring `work.UNION_ROUNDS`
NO_HOOKS_DIRNAME = ".sigma-no-hooks"         # never created; git finds no hook in a directory that is not there

NOT_ELIGIBLE = "not-level-1"
UNION_DECLINED = "union-declined"
WOULD_EMPTY = "would-be-empty"
STAMP_FAILED = "stamp-failed"
AUTHORSHIP = "authorship-changed"
CONTINUE_FAILED = "continue-failed"
TOO_MANY_STOPS = "too-many-stops"
NO_VERIFY = "no-verify-command"
VERIFY_FAILED = "verify-failed"
NO_BACKUP = "no-backup-descriptor"
PROOF_UNREAD = "proof-unread"


def _sibling(name):
    if name not in _LOADED:
        spec = importlib.util.spec_from_file_location(name, _HERE / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _LOADED[name] = module
    return _LOADED[name]


def refusal(code, detail, **where):
    out = {"code": code, "detail": detail}
    out.update(where)
    return out


def _stage_blobs(run, path, name):
    """(base, ours, theirs) texts of one unmerged path, or None when any is unreadable."""
    try:
        return tuple(str(run(str(path), ["git", "show", ":%d:%s" % (n, name)]) or "") for n in (1, 2, 3))
    except Exception:                        # noqa: BLE001
        return None


def _append_text(run, path, name):
    """#1515: the append-only union of a single non-changelog conflicted file, or None (not that shape)."""
    blobs = _stage_blobs(run, path, name)
    return None if blobs is None else _sibling("conflict_proof").append_union(*blobs)


def conflicted_file(run, path, changelog=CHANGELOG):
    """The one file Level 1 can take at this stop -- the changelog, or (#1515) a file whose two sides purely appended
    in a recognised append-only shape -- else None. Exactly one unmerged path, with a base stage."""
    try:
        stages = _sibling("conflict_state").stage_map(run, str(path))
    except Exception:                        # noqa: BLE001
        return None
    if len(stages) != 1 or 1 not in next(iter(stages.values())):
        return None
    name = next(iter(stages))
    return name if name == changelog or _append_text(run, path, name) is not None else None


def eligible(run, path, changelog=CHANGELOG):
    """Exactly one unmerged path with a base stage: the changelog, or an append-only shape. A failed read is not eligible."""
    return conflicted_file(run, path, changelog) is not None


def _text(run, path, argv):
    """The command's output, or its failure text: `git ... --check` exits 1 WITH its findings, and the runner raises."""
    try:
        return str(run(str(path), argv) or "")
    except Exception as exc:                 # noqa: BLE001
        return str(exc)


def _union_appended(run, path, name):
    """Write and stage the append-only union of `name` -> True when it resolved (nothing is written otherwise)."""
    text = _append_text(run, path, name)
    if text is None:
        return False
    try:
        _sibling("state").atomic_write_text(str(pathlib.Path(path) / name), text)
        run(str(path), ["git", "add", "--", name])
    except Exception:                        # noqa: BLE001
        return False
    return True


def _one_stop(run, path, union, run_id, stops, files, stopped_at):
    """Resolve and stamp the stop the rebase is at -> a refusal dict, or None. `stopped_at` records whether the commit
    was made (past that point the conflict cannot be restored)."""
    state, proof, res = _sibling("conflict_state"), _sibling("conflict_proof"), _sibling("feature_upkeep_resolution")
    name = conflicted_file(run, path)
    if name is None:
        return refusal(NOT_ELIGIBLE, "the stop is not a single changelog conflict with a base stage")
    orig = str(run(str(path), ["git", "rev-parse", "REBASE_HEAD"]) or "").strip()
    unmerged = state.conflicted_paths(run, str(path))
    before = state.stage0_listing(run, str(path))
    try:
        original_text = str(run(str(path), ["git", "show", "REBASE_HEAD:" + name]) or "")
    except Exception:                        # noqa: BLE001 - deleted in the original: nothing to compare against
        original_text = ""
    original_check = _text(run, path, ["git", "show", "--check", "--format=", "REBASE_HEAD"])
    if name == CHANGELOG:
        done = _sibling("work")._try_union_changelog(str(path), run, union)
    else:
        done = _union_appended(run, path, name)
    if not done:
        return refusal(UNION_DECLINED, "the heading-aware union did not resolve the changelog")
    if state.empty_commit_about_to_land(run, str(path)):
        return refusal(WOULD_EMPTY, "the resolved commit would be empty and git would drop it")
    found = proof.only_conflicted_differ(before, state.stage0_listing(run, str(path)), unmerged)
    resolved = pathlib.Path(path) / name
    blob = resolved.read_text(encoding="utf-8", errors="replace")
    found += proof.check_markers({name: blob}, {name: original_text})
    found += proof.new_whitespace_findings(_text(run, path, ["git", "diff", "--cached", "--check"]), original_check)
    if found:
        return found[0]
    try:
        new = res.stamp_commit(run, str(path), 1, run_id)
    except Exception as exc:                 # noqa: BLE001
        return refusal(STAMP_FAILED, str(exc)[:200])
    stopped_at.append(new)
    try:
        changed = res.authorship_problems(run, str(path), "REBASE_HEAD", new)
    except Exception as exc:                 # noqa: BLE001
        return refusal(AUTHORSHIP, str(exc)[:200])
    if changed:
        return refusal(AUTHORSHIP, "the stamping commit changed: " + ", ".join(changed))
    stops[orig] = {"new_head": new} if name == CHANGELOG else {"new_head": new, "files": [name]}
    files[name] = hashlib.sha256(resolved.read_bytes()).hexdigest()
    return None


def resolve_stops(run, path, union, run_id, stopped, rounds=ROUNDS):
    """Level 1 over the stops of one rebase -> `{"stops", "files", "refusal", "stamped"}`; `refusal` is None when the
    rebase ran to completion. `stopped(run, path)` answers whether a rebase is still in progress."""
    stops, files, stamped = {}, {}, []
    out = {"stops": stops, "files": files, "refusal": None, "stamped": stamped}
    for _ in range(rounds):
        made = []
        out["file"] = conflicted_file(run, path) or CHANGELOG
        try:
            out["refusal"] = _one_stop(run, path, union, run_id, stops, files, made)
        except Exception as exc:             # noqa: BLE001 - an unread state is not a resolution
            out["refusal"] = refusal(NOT_ELIGIBLE, "the stop could not be read: " + str(exc)[:200])
        stamped.extend(made)
        if out["refusal"]:
            out["restorable"] = not made
            return out
        no_hooks = str(pathlib.Path(path) / NO_HOOKS_DIRNAME)
        try:
            run(str(path), ["git", "-c", "core.editor=true", "-c", "core.hooksPath=" + no_hooks,
                            "rebase", "--continue"])
            return out
        except Exception as exc:             # noqa: BLE001 - a later replayed commit may also conflict
            if not stopped(run, path):
                out["refusal"] = refusal(CONTINUE_FAILED, str(exc)[:200])
                out["restorable"] = False
                return out
    out["refusal"] = refusal(TOO_MANY_STOPS, "more than %d stops in one pass" % rounds)
    out["restorable"] = False
    return out


def prove(run, cwd, base_ref, before, after, stops, verify_command=None, verify_timeout=3600,
          allow_no_verify=False, worktree=None, run_verify=None):
    """The gate after the rebase and before the push -> `{"refusals": [..], "checks": {name: word}, "pairs": [..]}`.

    Reads the originals and the replayed range once, pairs them with the recorded stop keys, compares the added and
    removed line multisets on every path outside the changelog, then runs the verify command in the scratch worktree.
    With no verify command it proceeds only when `allow_no_verify` is true. Never raises."""
    proof = _sibling("conflict_proof")
    out = {"refusals": [], "checks": {}, "pairs": []}
    try:
        patch_fn = proof.git_patch_id_fn(cwd)
        originals = proof.read_commits(run, cwd, "%s..%s" % (base_ref, before), patch_fn)
        replayed = proof.read_commits(run, cwd, "%s..%s" % (base_ref, after))
        merge_base = str(run(cwd, ["git", "merge-base", before, base_ref]) or "").strip()
        upstream = proof.upstream_patch_ids(run, cwd, "%s..%s" % (merge_base, base_ref), patch_fn)
        paired = proof.pair_commits(originals, replayed, stops=stops, upstream=upstream)
    except Exception as exc:                 # noqa: BLE001 - an unread proof is a refusal, never a pass
        out["refusals"].append(refusal(PROOF_UNREAD, str(exc)[:200]))
        return out
    out["refusals"] += paired["refusals"]
    by_old = {c["sha"]: c for c in originals}
    by_new = {c["sha"]: c for c in replayed}
    out["pairs"] = [list(p) for p in paired["pairs"]]
    for old, new in paired["pairs"]:
        conflicted = (set((stops[old] or {}).get("files") or (CHANGELOG,))) if old in stops else set()   # Level 2 names its own
        out["refusals"] += proof.multiset_differences(by_old[old]["diff"], by_new[new]["diff"], conflicted)
    out["checks"]["pairing"] = "refused" if out["refusals"] else "passed"
    if out["refusals"]:
        return out
    if not (isinstance(verify_command, str) and verify_command.strip()):
        if not allow_no_verify:
            out["refusals"].append(refusal(NO_VERIFY, "no verify command is set and conflicts.mechanical_without_verify "
                                                      "is false"))
        out["checks"]["verify"] = "skipped"
        return out
    runner = run_verify or _sibling("bounded_run").run_verify
    try:
        result = runner(verify_command, str(worktree or cwd), verify_timeout)
    except Exception as exc:                 # noqa: BLE001
        out["refusals"].append(refusal(VERIFY_FAILED, str(exc)[:200]))
        out["checks"]["verify"] = "error"
        return out
    ok = getattr(result, "outcome", None) == _sibling("bounded_run").OK
    out["checks"]["verify"] = "passed" if ok else "failed"
    if not ok:
        out["refusals"].append(refusal(VERIFY_FAILED, "verify ended %s" % getattr(result, "outcome", "unknown")))
    return out
