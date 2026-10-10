#!/usr/bin/env python3
"""Canonical, immutable receipt primitives for durable merge observations.

The receipt objects deliberately contain only GitHub facts and durable Sigma ownership.
Local outboxes may track delivery, but they are never accepted as ownership proof.
"""
import argparse
import hashlib
import json
import os
import pathlib
import tempfile
import re
import subprocess
import sys
import tarfile
import io
import datetime
import importlib.util


_RELEASE_MANIFEST = pathlib.Path(__file__).with_name("release_manifest.py")
_release_spec = importlib.util.spec_from_file_location("release_manifest", _RELEASE_MANIFEST)
release_manifest = importlib.util.module_from_spec(_release_spec)
_release_spec.loader.exec_module(release_manifest)


_PARENT = ("schema_version", "ownership_key", "canonical_repository_id", "owner_kind", "owner_id",
           "goal", "head_ref", "base_ref", "pr_number", "pr_node_id", "creating_writer", "pr_created_at")
_WRITERS = ("work.pr", "unit_completion._draft", "verify_merge.ensure_landing_pr")


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"


def ownership_key(facts):
    parts = (facts["canonical_repository_id"], facts["owner_kind"], facts["owner_id"],
             facts["head_ref"], facts["pr_node_id"])
    if not all(isinstance(part, str) and part for part in parts):
        raise ValueError("receipt ownership facts are required")
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def landing_owner_id(branch):
    """The ONE owner-id rule for a unit landing receipt: the unit's branch (`feature/<unit>`), never the bare unit
    name. Both landing observers (the rebase landing helper and unit completion) must key one PR identically, or one
    landing is recorded twice. Chosen as the branch form because `verify_merge`'s persisted parent receipt path
    and its test pin are already keyed that way."""
    if not isinstance(branch, str) or not branch:
        raise ValueError("a unit landing owner id needs the unit branch")
    return branch


def parent_receipt(facts):
    required = set(_PARENT) - {"ownership_key", "schema_version"}
    if set(facts) - set(_PARENT) or required - set(facts):
        raise ValueError("parent receipt has missing or mutable fields")
    if facts.get("owner_kind") not in ("goal", "unit") or facts.get("creating_writer") not in _WRITERS:
        raise ValueError("invalid receipt owner or writer")
    value = dict(facts)
    key = ownership_key(value)
    if value.get("ownership_key") not in (None, key):
        raise ValueError("receipt ownership_key does not match immutable facts")
    value["ownership_key"] = key
    value["schema_version"] = 1
    return canonical_json(value)


def child_receipt(parent, facts):
    fields = {"schema_version": 1, "ownership_key": parent["ownership_key"],
              "canonical_repository_id": parent["canonical_repository_id"], "pr_number": parent["pr_number"],
              "pr_node_id": parent["pr_node_id"], "merge_sha": facts.get("merge_sha"),
              "github_merged_at": facts.get("github_merged_at")}
    # Round-8 review finding: length-only checking (`len(...) in (40, 64)`) let a correctly-sized
    # but non-hex value through -- the same class of gap round 5 fixed in `coverage_scan`. This
    # value reaches a filesystem path unsanitized in work.py's `_observe_confirmed_merge`
    # (`receipts/v1/<key>/merges/<merge_sha>.json`), so it gets the same hex-shape check every
    # other merge_sha use in this file already has.
    if not isinstance(fields["merge_sha"], str) or not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", fields["merge_sha"]):
        raise ValueError("invalid merge SHA")
    if not isinstance(fields["github_merged_at"], str) or not fields["github_merged_at"]:
        raise ValueError("missing GitHub merged timestamp")
    return canonical_json(fields)


def write_immutable(path, data):
    """Create a receipt once; retries can only adopt byte-identical contents."""
    path = pathlib.Path(path)
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError("immutable receipt conflict")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".receipt-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
        try:
            os.link(temp, path)
        except FileExistsError:
            if path.read_bytes() != data:
                raise ValueError("immutable receipt conflict")
            return False
        finally:
            if os.path.exists(temp): os.unlink(temp)
        return True
    finally:
        if os.path.exists(temp): os.unlink(temp)


def observation_keys(ownership, merge_sha):
    if not isinstance(ownership, str) or not isinstance(merge_sha, str):
        raise ValueError("ownership key and merge SHA are required")
    digest = lambda suffix: hashlib.sha256(f"{ownership}\0{merge_sha}\0{suffix}".encode()).hexdigest()
    return digest("merged-entry"), digest("merge-observed")


def _feature_landing_parent_path(sdlc_dir, facts):
    """The one serialized receipt boundary used by sigma-rebase's landing flow."""
    if not isinstance(sdlc_dir, str) or not sdlc_dir:
        raise ValueError("feature landing receipt needs an sdlc directory")
    return pathlib.Path(sdlc_dir) / "ledger" / "receipts" / "v1" / ownership_key(facts) / "parent.json"


def feature_landing_receipt(request):
    """Serve the narrow cross-skill receipt protocol for feature landings.

    ``sigma-rebase`` is intentionally a process client of this script rather than a Python
    importer.  The protocol accepts canonical facts and returns only stable serialized facts:
    whether the immutable parent matches, its ownership key, and (when requested) its two
    merge-observation keys.  Keeping receipt bytes and key derivation here ensures a feature
    landing cannot fork the goal-landing receipt contract in a second skill.
    """
    if not isinstance(request, dict):
        raise ValueError("feature landing receipt request must be an object")
    action, sdlc_dir, facts = request.get("action"), request.get("sdlc_dir"), request.get("facts")
    if action not in ("write-parent", "check-parent", "observation-keys"):
        raise ValueError("unknown feature landing receipt action")
    if not isinstance(facts, dict):
        raise ValueError("feature landing receipt facts must be an object")
    parent = parent_receipt(facts)
    key = ownership_key(facts)
    path = _feature_landing_parent_path(sdlc_dir, facts)
    if action == "write-parent":
        write_immutable(path, parent)
        return {"ownership_key": key, "valid": True}
    if action == "check-parent":
        try:
            valid = path.read_bytes() == parent
        except OSError:
            valid = False
        return {"ownership_key": key, "valid": valid}
    # "observation-keys" (round 4, B2): a PURE function of the canonical facts, deliberately NOT
    # gated on a locally persisted receipt matching.  Requiring one made a landing PR this
    # invocation did not itself create -- an already-open PR from a prior session, or one that
    # predates this feature -- record its merge NOWHERE, silently.  Cycle-6 already ruled this
    # shape once for work.py: the core's own facts are gated by ledger.enabled/journal_on, never
    # by receipt ownership; the receipt stays the separate, optional proof repository-wide
    # acceptance needs (#2680/#2683).
    merge_sha = request.get("merge_sha")
    if not isinstance(merge_sha, str) or not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", merge_sha):
        raise ValueError("feature landing merge SHA is invalid")
    entry_key, observation_key = observation_keys(key, merge_sha)
    return {"ownership_key": key, "valid": True, "entry_key": entry_key,
            "observation_key": observation_key}


def trusted_signature(status_output, trusted_signers):
    """Accept only an exact full VALIDSIG fingerprint in the configured allowlist."""
    if not isinstance(status_output, str) or not isinstance(trusted_signers, (list, tuple, set)):
        return False
    allowed = {str(value).strip().lower() for value in trusted_signers
               if re.fullmatch(r"[0-9a-fA-F]{40}", str(value).strip())}
    for line in status_output.splitlines():
        match = re.match(r"\[GNUPG:\]\s+VALIDSIG\s+([0-9A-Fa-f]{40})\b", line)
        if match and match.group(1).lower() in allowed:
            return True
    return False


class ReceiptIndex(dict):
    """A join index that keeps malformed candidates visible rather than silently losing them."""
    def __init__(self):
        super().__init__()
        self.invalid = set()


def _join(parent):
    return (parent.get("canonical_repository_id"), parent.get("pr_node_id"),
            parent.get("pr_number"), parent.get("head_ref"))


def receipt_index(root):
    """One pass over canonical parent objects; collisions remain visible to the caller."""
    index = ReceiptIndex()
    for path in sorted(pathlib.Path(root).glob("*/parent.json"), key=lambda item: str(item)):
        try:
            raw = path.read_bytes(); parent = json.loads(raw)
            join = _join(parent) if isinstance(parent, dict) else None
            expected = parent_receipt({key: value for key, value in parent.items() if key != "schema_version"})
            if (raw != expected or path.parent.name != parent.get("ownership_key")):
                if join and all(value is not None for value in join):
                    index.invalid.add(join)
                continue
            index.setdefault(join, []).append(parent)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            continue
    return index


def classify_receipt(index, pr):
    join = (pr.get("repository"), pr.get("node_id"), pr.get("number"), pr.get("head_ref"))
    candidates = index.get(join, [])
    if not candidates:
        if join in getattr(index, "invalid", set()):
            return "invalid"
        return "missing/unowned"
    return "valid-owned" if len(candidates) == 1 else "conflicting"


def _jsonl_records(paths, label, required):
    result = []
    for directory in paths:
        if required and not directory.is_dir():
            raise ValueError("pinned snapshot is missing synced %s" % label)
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.jsonl"), key=lambda item: item.as_posix()):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                raise ValueError("cannot read %s" % label) from exc
            for line in lines:
                if not line.strip(): continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError("%s contains malformed JSON" % label) from exc
                if not isinstance(value, dict):
                    raise ValueError("%s contains unsafe record" % label)
                result.append(value)
    return result


def _pinned_measurements(root, journal_root):
    """Join remote-pinned entries to the author machine's deliberately local review journal."""
    entries_directory = root / "entries"
    entries = _jsonl_records((entries_directory,), "synced entries", required=True)
    local = pathlib.Path(journal_root) / ".sdlc" if journal_root is not None else None
    journal_dirs = (() if local is None else (local / "events", local / "ledger" / "events"))
    events = _jsonl_records(journal_dirs, "local journal events", required=False)
    # The entries stream is REMOTE-PINNED and shared, so its `actor` values enumerate every
    # machine that has written to this repository.  The review journal is deliberately local to
    # the invoking machine, so a cohort spanning more than one actor is one this machine cannot
    # measure: the other actors' review events are simply not here to be counted.
    actors = sorted({row.get("actor") for row in entries if isinstance(row.get("actor"), str)})
    return {"entries": entries, "events": events,
            "entry_files": len(list(entries_directory.glob("*.jsonl"))),
            "event_files": sum(len(list(path.glob("*.jsonl"))) for path in journal_dirs),
            "journal_authority": "local-author-machine",
            "entry_actors": actors,
            "journal_covers_cohort": len(actors) <= 1}


def _sink_observed(measurements, parent, pr):
    """Report each required observation sink independently for one receipt-bound PR."""
    entry_key, _ = observation_keys(parent["ownership_key"], pr["merge_sha"])
    # Ledger entries predate the typed journal and retain ``pr`` as a string.
    # GitHub's snapshot is necessarily numeric, so compare their canonical text.
    entries = [row for row in measurements["entries"] if row.get("kind") == "merged"
               and str(row.get("pr")) == str(pr["number"])]
    matching_entries = [row for row in entries if row.get("merged_entry_key") == entry_key]
    reviews = [row for row in measurements["events"] if row.get("kind") == "review_posted" and
               row.get("pr") == pr["number"] and row.get("head_sha") == pr.get("head_sha")]
    return {
        "merged": len(matching_entries) == 1,
        "review": any(isinstance(row.get("brief_hash"), str) and
                      re.fullmatch(r"[0-9a-f]{64}", row["brief_hash"])
                      for row in reviews),
    }


def coverage_scan(snapshot_root, prs, require_measurements=False, journal_root=None):
    """Classify a finite PR snapshot and measure merged/review coverage independently.

    Receipt validity remains a single classification because it is an ownership and integrity
    property.  The two runtime observations are deliberately separate: a ledger ``merged`` row
    and a bound ``review_posted`` event answer different questions and must never be collapsed
    into one optimistic "observed" percentage.
    """
    root = pathlib.Path(snapshot_root)
    receipts = root / "receipts" / "v1" if (root / "receipts" / "v1").exists() else root
    if not receipts.is_dir():
        raise ValueError("pinned receipt snapshot is missing")
    measurements = _pinned_measurements(root, journal_root) if require_measurements else None
    index = receipt_index(receipts)
    counts = {"observed-valid-owned": 0, "owned-unobserved-pending": 0,
              "missing/unowned": 0, "conflicting": 0, "invalid": 0}
    sink_counts = {"merged": 0, "review": 0}
    rows = []
    for pr in prs:
        classification = classify_receipt(index, pr) if isinstance(pr, dict) else "invalid"
        sink_observations = {"merged": None, "review": None}
        if classification == "valid-owned":
            parent = index[(pr.get("repository"), pr.get("node_id"), pr.get("number"), pr.get("head_ref"))][0]
            merge_sha = pr.get("merge_sha")
            # Finding 1 (round 5): validated HERE, not only by the one caller
            # (`github_merged_pr_snapshot`) that happened to check it first. `coverage_scan` is
            # also reached directly from the `coverage` CLI's --snapshot/--prs, which parses an
            # operator-supplied JSON file with no validation of its own -- and pathlib's `/`
            # operator discards every earlier segment when the right side looks absolute, so an
            # unchecked `merge_sha` could point `child_path` at an arbitrary file on the host.
            if not isinstance(merge_sha, str) or not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", merge_sha):
                counts["invalid"] += 1
                rows.append({"pr": pr, "classification": "invalid", "sink_observations": sink_observations})
                continue
            child_path = receipts / parent["ownership_key"] / "merges" / (merge_sha + ".json")
            try:
                child = json.loads(child_path.read_text(encoding="utf-8"))
                expected = child_receipt(parent, {"merge_sha": merge_sha, "github_merged_at": pr.get("merged_at")})
                if child_path.read_bytes() != expected:
                    classification = "invalid"
                elif measurements is not None:
                    sink_observations = _sink_observed(measurements, parent, pr)
                    for sink, observed in sink_observations.items():
                        if observed:
                            sink_counts[sink] += 1
                    classification = ("observed-valid-owned" if all(sink_observations.values())
                                      else "owned-unobserved-pending")
                else:
                    classification = "observed-valid-owned"
            except FileNotFoundError:
                classification = "owned-unobserved-pending"
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                classification = "invalid"
        counts[classification] += 1
        rows.append({"pr": pr, "classification": classification,
                     "sink_observations": sink_observations})
    result = {"receipt_snapshot": str(root), "counts": counts, "full_cohort": len(rows), "rows": rows}
    if measurements is not None:
        total = len(rows)
        result["sink_coverage"] = {
            sink: {"observed": observed, "total": total,
                   "rate": (observed / total if total else None)}
            for sink, observed in sink_counts.items()
        }
        result["synced_entries_files"] = measurements["entry_files"]
        result["entry_authority"] = "remote-pinned"
        result["local_journal_event_files"] = measurements["event_files"]
        result["journal_authority"] = measurements["journal_authority"]
        result["entry_actors"] = measurements["entry_actors"]
        result["journal_covers_cohort"] = measurements["journal_covers_cohort"]
    return result


def pagination_manifest(pages, max_pages=200, resume=None):
    """Pure bounded page recorder; callers reissue `resume.last_input_cursor` before advancing."""
    if not isinstance(max_pages, int) or max_pages < 1:
        raise ValueError("invalid coverage page cap")
    prior_count = max(0, len(resume.get("pages", [])) - 1) if isinstance(resume, dict) else 0
    if len(pages) + prior_count > max_pages:
        raise ValueError("coverage page cap exceeded")
    recorded, identities = [], []
    for number, page in enumerate(pages, 1):
        if not isinstance(page, dict) or not isinstance(page.get("rows"), list):
            raise ValueError("malformed coverage page")
        if len(page["rows"]) > 100:
            raise ValueError("coverage page exceeds fixed size 100")
        identity = [(row.get("number"), row.get("merged_at"), row.get("head_ref"), row.get("head_sha"), row.get("merge_sha"))
                    for row in page["rows"] if isinstance(row, dict)]
        if len(identity) != len(page["rows"]):
            raise ValueError("malformed coverage row")
        digest = hashlib.sha256(canonical_json(identity)).hexdigest()
        recorded.append({"page": number, "input_cursor": page.get("input_cursor"),
                         "end_cursor": page.get("end_cursor"), "identity_digest": digest})
        identities.extend(identity)
    overall = hashlib.sha256(canonical_json(sorted(identities, key=lambda row: (row[1], row[0])))).hexdigest()
    if resume and resume.get("pages"):
        prior = resume["pages"][-1]
        if not recorded or recorded[0]["input_cursor"] != prior.get("input_cursor") or recorded[0]["identity_digest"] != prior.get("identity_digest"):
            raise ValueError("coverage resume identity/order drift")
    return {"page_size": 100, "max_pages": max_pages, "pages": recorded,
            "identity_digest": overall, "full_cohort": len(identities)}


def _run_git(repo, args, run=None):
    """Run a fixed-argument git command and make a failed remote read a loud refusal."""
    command = ["git", "-C", str(repo), *args]
    if run is not None:
        result = run(command)
        if isinstance(result, tuple):
            code, output = result
            if code:
                raise ValueError("git %s failed: %s" % (" ".join(args), output))
            return str(output).strip()
        return str(result).strip()
    process = subprocess.run(command, capture_output=True, text=True)
    if process.returncode:
        raise ValueError("git %s failed: %s" % (" ".join(args), (process.stderr or process.stdout).strip()))
    return process.stdout.strip()


def resolve_remote_snapshot(repo, remote="origin", branch="sdlc-ledger", run=None):
    """Fetch once and pin the receipt authority to the exact remote commit it returned."""
    _run_git(repo, ["fetch", "--no-tags", remote, branch], run)
    commit = _run_git(repo, ["rev-parse", remote + "/" + branch], run)
    if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", commit):
        raise ValueError("remote receipt snapshot is not a Git commit SHA")
    tree = _run_git(repo, ["rev-parse", commit + "^{tree}"], run)
    if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", tree):
        raise ValueError("remote receipt snapshot has no Git tree")
    return {"commit": commit, "tree": tree, "remote": remote, "branch": branch}


def materialize_snapshot(repo, commit, destination):
    """Extract only the pinned receipt tree, avoiding a mutable worktree as scan authority."""
    proc = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", commit],
                          capture_output=True)
    if proc.returncode:
        raise ValueError("cannot read pinned receipt snapshot: " + proc.stderr.decode("utf-8", "replace").strip())
    root = pathlib.Path(destination)
    root.mkdir(parents=True, exist_ok=True)
    try:
        archive = tarfile.open(fileobj=io.BytesIO(proc.stdout))
        for member in archive.getmembers():
            target = root / member.name
            if member.issym() or member.islnk() or not target.resolve().is_relative_to(root.resolve()):
                raise ValueError("pinned receipt archive contains an unsafe path")
        # Python 3.9 is still a supported host.  Its tarfile lacks the later `filter=`
        # safeguard, so extract only after the explicit complete-member validation above.
        for member in archive.getmembers():
            archive.extract(member, root)
    except (tarfile.TarError, OSError) as exc:
        raise ValueError("cannot materialize pinned receipt snapshot") from exc
    return root


def _receipt_history_paths(snapshot_root):
    """Validate every receipt's canonical bytes before asking Git to attest to its history."""
    receipts = pathlib.Path(snapshot_root) / "receipts" / "v1"
    if not receipts.is_dir():
        raise ValueError("pinned receipt snapshot is missing")
    parents, paths = {}, []
    for path in sorted(receipts.rglob("*.json"), key=lambda item: item.as_posix()):
        relative = path.relative_to(snapshot_root).as_posix()
        parts = path.relative_to(receipts).parts
        try:
            raw = path.read_bytes(); value = json.loads(raw)
            if len(parts) == 2 and parts[1] == "parent.json":
                expected = parent_receipt({key: item for key, item in value.items() if key != "schema_version"})
                if raw != expected or parts[0] != value.get("ownership_key"):
                    raise ValueError("receipt parent bytes are not canonical")
                parents[parts[0]] = value; paths.append(relative)
            elif len(parts) == 3 and parts[1] == "merges":
                paths.append(relative)
            else:
                raise ValueError("receipt snapshot has an unsafe JSON path")
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise ValueError("receipt snapshot has unsafe raw receipt bytes") from exc
    for relative in paths:
        parts = pathlib.PurePosixPath(relative).relative_to("receipts/v1").parts
        if len(parts) != 3: continue
        parent = parents.get(parts[0])
        try:
            raw = (pathlib.Path(snapshot_root) / relative).read_bytes(); child = json.loads(raw)
            expected = child_receipt(parent, {"merge_sha": child.get("merge_sha"),
                                               "github_merged_at": child.get("github_merged_at")})
            if parent is None or raw != expected or parts[2] != child.get("merge_sha") + ".json":
                raise ValueError("receipt child bytes are not canonical")
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise ValueError("receipt snapshot has unsafe raw receipt bytes") from exc
    if not paths:
        raise ValueError("pinned receipt snapshot has no receipt history")
    return paths


def verify_receipt_history(repo, snapshot_root, release_anchor, pinned_commit, trusted_signers, run=None):
    """Require each receipt's post-release, signed, untouched Git introduction.

    Canonical JSON proves only a file's current shape.  The ledger authority also requires the
    file to have appeared after the signed release anchor, in a trusted signed commit, with its
    exact blob unchanged throughout the pinned branch history.
    """
    _run_git(repo, ["merge-base", "--is-ancestor", release_anchor, pinned_commit], run)
    paths = _receipt_history_paths(snapshot_root)
    for relative in paths:
        introduced = _run_git(repo, ["log", "--format=%H", "--diff-filter=A",
                                     release_anchor + ".." + pinned_commit, "--", relative], run).splitlines()
        if len(introduced) != 1 or not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", introduced[0]):
            raise ValueError("receipt does not have one post-release introduction")
        signature = _run_git(repo, ["verify-commit", "--raw", introduced[0]], run)
        if not trusted_signature(signature, trusted_signers):
            raise ValueError("receipt introduction commit is not trusted")
        initial_blob = _run_git(repo, ["rev-parse", introduced[0] + ":" + relative], run)
        pinned_blob = _run_git(repo, ["rev-parse", pinned_commit + ":" + relative], run)
        if (not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", initial_blob) or
                initial_blob != pinned_blob):
            raise ValueError("receipt blob differs from its signed introduction")
        if _run_git(repo, ["log", "--format=%H", introduced[0] + ".." + pinned_commit,
                           "--", relative], run):
            raise ValueError("receipt history mutated after its introduction")
    return len(paths)


def _github_repository(remote_url):
    """Translate only an unambiguous github.com origin URL into owner/name."""
    value = remote_url.strip()
    # Should-fix (round 5): this pattern had a DOUBLED backslash (`github\\.com`), which regex
    # reads as a literal backslash character followed by an unescaped `.` (matching any char) --
    # it could never match a real "git@github.com:..." URL. Every caller mocks
    # `github_repository_from_origin` in tests, which is why this was invisible.
    match = re.fullmatch(r"(?:git@github\.com:|ssh://git@github\.com/|https://github\.com/)([^/\s]+)/([^/\s]+?)(?:\.git)?/?", value)
    if not match:
        raise ValueError("origin is not an unambiguous github.com repository")
    return match.group(1) + "/" + match.group(2)


def github_repository_from_origin(repo, run=None):
    return _github_repository(_run_git(repo, ["remote", "get-url", "origin"], run))


_GITHUB_QUERY = """query($owner:String!,$name:String!,$cursor:String) {
 repository(owner:$owner,name:$name) { id nameWithOwner pullRequests(first:100,after:$cursor,states:MERGED,orderBy:{field:UPDATED_AT,direction:DESC}) {
  pageInfo { hasNextPage endCursor }
  nodes { id number headRefName headRefOid baseRefName mergedAt mergeCommit { oid } }
 } } }"""


def _github_request(query, variables, token, request=None):
    """Issue one GraphQL read through `gh`, the GitHub client the rest of the kit already uses.

    `gh api graphql` rather than a direct HTTPS call, because the core must hold no network
    module outside its five reviewed pairs: `tests/test_no_network_in_core.py` is a strict guard
    with no baseline (#2584), and #2575 removed the last unlisted core `urllib.request` user.
    Reaching for urllib here would have put one straight back.

    The token keeps its original promise -- it is handed to the child in its ENVIRONMENT as
    `GH_TOKEN`, never on a command line, so it cannot leak through `ps` or a shell history. With
    no token `gh` uses its own stored auth, which is what every other GitHub read in the kit does.
    """
    if request is not None:
        return request(query, variables, token)
    environment = dict(os.environ)
    if isinstance(token, str) and token:
        environment["GH_TOKEN"] = token
    process = subprocess.run(["gh", "api", "graphql", "--input", "-"],
                             input=json.dumps({"query": query, "variables": variables}),
                             capture_output=True, text=True, env=environment, timeout=30)
    if process.returncode:
        raise ValueError("GitHub cohort query failed")
    try:
        data = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("GitHub cohort query failed") from exc
    if not isinstance(data, dict) or data.get("errors"):
        raise ValueError("GitHub cohort query returned an error")
    return data


def github_merged_pr_snapshot(repository, since, until, token=None, request=None, max_pages=200):
    """Return a finite, bounded GitHub snapshot of every merged `sdlc/*` PR in the fixed interval."""
    if not isinstance(max_pages, int) or max_pages < 1:
        raise ValueError("invalid GitHub page cap")
    start, end = release_manifest._utc(since), release_manifest._utc(until)
    if start >= end:
        raise ValueError("GitHub cohort interval is invalid")
    owner, name = repository.split("/", 1) if repository.count("/") == 1 else (None, None)
    if not owner or not name:
        raise ValueError("invalid GitHub repository")
    cursor, pages, cohort, seen, canonical_id = None, [], [], set(), None
    for number in range(1, max_pages + 1):
        reply = _github_request(_GITHUB_QUERY, {"owner": owner, "name": name, "cursor": cursor}, token, request)
        repo = ((reply.get("data") or {}).get("repository") if isinstance(reply, dict) else None)
        pulls = repo.get("pullRequests") if isinstance(repo, dict) else None
        if not isinstance(repo, dict) or not isinstance(repo.get("id"), str) or not isinstance(pulls, dict):
            raise ValueError("GitHub cohort response is unsafe")
        if canonical_id is None: canonical_id = repo["id"]
        if canonical_id != repo["id"] or repo.get("nameWithOwner") != repository:
            raise ValueError("GitHub cohort repository identity drift")
        nodes, info = pulls.get("nodes"), pulls.get("pageInfo")
        if not isinstance(nodes, list) or len(nodes) > 100 or not isinstance(info, dict):
            raise ValueError("GitHub cohort page is unsafe")
        rows = []
        for raw in nodes:
            if not isinstance(raw, dict): raise ValueError("GitHub cohort row is unsafe")
            node_id, ref, head, merged, sha = (raw.get("id"), raw.get("headRefName"), raw.get("headRefOid"),
                                               raw.get("mergedAt"), ((raw.get("mergeCommit") or {}).get("oid")))
            if isinstance(ref, str) and not ref.startswith("sdlc/"):
                continue
            # A MERGED PR whose `mergeCommit` GitHub cannot resolve is real, immutable history --
            # measured on this repository, #684 and #681 (both merged 2026-08-10) come back with
            # `mergedAt` set and `mergeCommit: null`.  Treating that as unsafe aborted the WHOLE
            # snapshot on 2 rows out of 870, so the acceptance measurement could never complete
            # here at all.  Such a row keeps its place in the denominator and carries
            # `merge_sha=None`: receipts are keyed BY merge SHA, so it can never match a child and
            # can never be counted observed -- it stays honestly unobserved instead of fatal.
            if sha is not None and not (isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", sha)):
                raise ValueError("GitHub cohort row is unsafe")
            if not (isinstance(node_id, str) and isinstance(ref, str) and ref.startswith("sdlc/") and
                    isinstance(head, str) and re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", head) and
                    isinstance(merged, str) and
                    isinstance(raw.get("number"), int) and isinstance(raw.get("baseRefName"), str)):
                raise ValueError("GitHub cohort row is unsafe")
            if node_id in seen: raise ValueError("GitHub cohort pagination duplicated a PR")
            seen.add(node_id)
            row = {"repository": canonical_id, "node_id": node_id, "number": raw["number"],
                   "head_ref": ref, "head_sha": head, "base_ref": raw["baseRefName"], "merge_sha": sha, "merged_at": merged}
            rows.append(row)
            stamp = release_manifest._utc(merged)
            if start <= stamp < end: cohort.append(row)
        end_cursor, has_next = info.get("endCursor"), info.get("hasNextPage")
        if not isinstance(has_next, bool) or (has_next and not isinstance(end_cursor, str)):
            raise ValueError("GitHub cohort cursor is unsafe")
        pages.append({"input_cursor": cursor, "end_cursor": end_cursor, "rows": rows})
        if not has_next:
            return {"canonical_repository_id": canonical_id, "prs": cohort,
                    "pagination": pagination_manifest(pages, max_pages=max_pages)}
        cursor = end_cursor
    raise ValueError("GitHub cohort page cap exceeded")


def fetch_release_tag(repo, tag, run=None):
    """Fetch the signed tag to a private ref, so a local tag can never supply rollout authority."""
    if not isinstance(tag, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", tag) or ".." in tag or tag.endswith("/"):
        raise ValueError("release manifest has an unsafe tag name")
    ref = "refs/sdlc/rollout-tags/" + tag
    _run_git(repo, ["fetch", "--no-tags", "origin", "refs/tags/%s:%s" % (tag, ref)], run)
    return ref


#: Receipt sharing is NOT YET SUPPORTED (#2680).  Against real git it fails four independent ways --
#: GPG status read from stdout although `verify-commit --raw` writes it to stderr, receipt history
#: checked for ancestry across the orphan `sdlc-ledger` branch and a code commit, and a publish retry
#: that acknowledges a pre-rebase SHA with a rebase that drops the signature -- and none of it was
#: caught because every test stubs git.  Until #2680 lands, `work._receipt_sharing_enabled` refuses
#: the config and `measured_rollout` refuses outright.  THE single source of truth for both.
RECEIPT_SHARING_SUPPORTED = False
_RECEIPT_SHARING_ISSUE = "#2680"


def measured_rollout(repo, trusted_signers, now=None, token=None, request=None, run=None):
    """Measure acceptance from the signed remote release and GitHub facts; fixtures cannot reach here."""
    if not RECEIPT_SHARING_SUPPORTED:
        # The command that makes a CLAIM refuses before running any git, so no partial measurement
        # can be read as a verdict.
        raise ValueError("receipt-backed rollout acceptance is not supported yet (see %s)" % _RECEIPT_SHARING_ISSUE)
    pinned = resolve_remote_snapshot(repo, "origin", branch="sdlc-ledger", run=run)
    current = now or datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    with tempfile.TemporaryDirectory(prefix="sigma-receipt-snapshot-") as directory:
        root = materialize_snapshot(repo, pinned["commit"], directory)
        path = root / "releases" / "receipt-publication-v1.json"
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ValueError("pinned receipt authority has no rollout manifest") from exc
        manifest = release_manifest.parse_payload(raw)
        tag_ref = fetch_release_tag(repo, manifest["release_tag"], run)
        release_manifest.verify_release_authority(repo, raw, trusted_signers, run, tag_ref=tag_ref)
        receipt_history_count = verify_receipt_history(repo, root, manifest["release_commit_sha"],
                                                       pinned["commit"], trusted_signers, run)
        start = manifest["receipt_publication_rollout_at"]
        end = (release_manifest._utc(start) + datetime.timedelta(days=14)).isoformat().replace("+00:00", "Z")
        github = github_merged_pr_snapshot(github_repository_from_origin(repo, run), start, end, token, request)
        if github["canonical_repository_id"] != manifest["canonical_repository_id"]:
            raise ValueError("GitHub repository does not match signed rollout manifest")
        result = coverage_scan(root, github["prs"], require_measurements=True, journal_root=repo)
    eligible = release_manifest.acceptance_eligible(start, start, end, current)
    safe = rollout_acceptance_safe(result)
    result.update({"receipt_authority": "remote-pinned", "receipt_snapshot": pinned["commit"],
                   "receipt_snapshot_commit": pinned["commit"], "receipt_snapshot_tree": pinned["tree"],
                   "rollout_at": start, "rollout_until": end, "measured_at": current,
                   "receipt_history_count": receipt_history_count, "github_pagination": github["pagination"],
                   "acceptance_eligible": bool(eligible and safe)})
    return result


def rollout_acceptance_safe(result):
    """A nonempty cohort needs >=95% in EACH sink; malformed/conflicting rows still refuse.

    THE REVIEW SINK IS ONLY MEASURABLE ON A SINGLE-AUTHOR-MACHINE REPOSITORY.  The cohort comes
    from the org-wide GitHub snapshot and the `merged` sink from the shared, remote-pinned entries
    stream, but `review_posted` events are read from the INVOKING MACHINE's local `.sdlc` journal
    only (`_pinned_measurements`).  Where a second machine has written to this repository its
    review events are absent here, so the review rate would read low however faithfully that
    machine posted.  That is not a coverage defect and must never be certified as one, so a
    multi-actor cohort REFUSES acceptance rather than reporting a number it cannot stand behind.
    Narrowing the denominator instead was rejected: it would make the rate trivially 1.0.

    Defining a cross-machine collection authority is the real fix and is deliberately NOT done
    here -- it needs a machine identity this codebase does not have (`ledger.py`: extending the
    live-writer probe cross-machine "would need a machine-identity"), which is a far larger
    change than this goal.
    """
    counts = result.get("counts") if isinstance(result, dict) else None
    coverage = result.get("sink_coverage") if isinstance(result, dict) else None
    cohort = result.get("full_cohort") if isinstance(result, dict) else None
    sink_safe = isinstance(coverage, dict) and isinstance(cohort, int) and all(
        isinstance(coverage.get(sink), dict) and
        coverage[sink].get("total") == cohort and
        isinstance(coverage[sink].get("observed"), int) and
        coverage[sink]["observed"] * 100 >= cohort * 95
        for sink in ("merged", "review")
    )
    return (isinstance(counts, dict) and result.get("journal_covers_cohort") is True and
            isinstance(result.get("full_cohort"), int) and
            result["full_cohort"] > 0 and all(counts.get(name) == 0 for name in ("conflicting", "invalid")) and
            isinstance(counts.get("observed-valid-owned"), int) and
            sink_safe)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Receipt-backed merge observation tools")
    sub = parser.add_subparsers(dest="command")
    coverage = sub.add_parser("coverage", help="measure a pinned receipt snapshot cohort")
    source = coverage.add_mutually_exclusive_group(required=True)
    source.add_argument("--snapshot", help="local pinned receipt fixture root (never acceptance authority)")
    source.add_argument("--ledger-repo", help="project Git repository; fetches and pins origin/sdlc-ledger")
    coverage.add_argument("--remote", default="origin", help="receipt authority remote when --ledger-repo is used")
    coverage.add_argument("--branch", default="sdlc-ledger", help="receipt authority branch when --ledger-repo is used")
    coverage.add_argument("--prs", help="JSON file containing a bounded fixture/cohort row set")
    coverage.add_argument("--trusted-signer", action="append",
                          help="full release-tag fingerprint; with --ledger-repo and no --prs runs the measured rollout")
    coverage.add_argument("--now", help="UTC measurement instant for reproducible rollout coverage")
    coverage.add_argument("--github-token-env", default="GITHUB_TOKEN",
                          help="environment variable containing the GitHub token for a measured rollout")
    acceptance = sub.add_parser("acceptance", help="measure the signed, remote 14-day GitHub rollout cohort")
    acceptance.add_argument("--ledger-repo", required=True, help="actual project checkout with origin/sdlc-ledger")
    acceptance.add_argument("--trusted-signer", action="append", required=True)
    acceptance.add_argument("--now", help="UTC measurement instant, for reproducible audits")
    acceptance.add_argument("--github-token-env", default="GITHUB_TOKEN", help="environment variable containing the GitHub token")
    feature_landing = sub.add_parser("feature-landing", help="serve the serialized feature-landing receipt protocol")
    args = parser.parse_args(argv)
    if args.command == "coverage":
        try:
            if args.ledger_repo and args.trusted_signer and not args.prs:
                token = os.environ.get(args.github_token_env)
                print(json.dumps(measured_rollout(args.ledger_repo, args.trusted_signer,
                                                  now=args.now, token=token), sort_keys=True))
                return
            if not args.prs:
                raise ValueError("coverage needs --prs for a fixture scan, or --trusted-signer for measured rollout coverage")
            prs = json.loads(pathlib.Path(args.prs).read_text(encoding="utf-8"))
            if not isinstance(prs, list): raise ValueError("PR input must be a JSON list")
            if args.snapshot:
                result = coverage_scan(args.snapshot, prs)
                result.update({"receipt_authority": "local-fixture", "acceptance_eligible": False})
            else:
                pinned = resolve_remote_snapshot(args.ledger_repo, args.remote, args.branch)
                with tempfile.TemporaryDirectory(prefix="sigma-receipt-snapshot-") as directory:
                    root = materialize_snapshot(args.ledger_repo, pinned["commit"], directory)
                    result = coverage_scan(root, prs)
                result["receipt_snapshot"] = pinned["commit"]
                result.update({"receipt_authority": "remote-pinned", "receipt_snapshot_commit": pinned["commit"],
                               "receipt_snapshot_tree": pinned["tree"], "acceptance_eligible": False})
            print(json.dumps(result, sort_keys=True))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            parser.error(str(exc))
    elif args.command == "acceptance":
        try:
            token = os.environ.get(args.github_token_env)
            print(json.dumps(measured_rollout(args.ledger_repo, args.trusted_signer, now=args.now, token=token), sort_keys=True))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            parser.error(str(exc))
    elif args.command == "feature-landing":
        try:
            print(json.dumps(feature_landing_receipt(json.load(sys.stdin)), sort_keys=True))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            parser.error(str(exc))


if __name__ == "__main__":
    main()
