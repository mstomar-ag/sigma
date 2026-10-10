import importlib.util
import json
import pathlib

import pytest


SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"
spec = importlib.util.spec_from_file_location("merge_observation", SCRIPTS / "merge_observation.py")
merge_observation = importlib.util.module_from_spec(spec)
if spec.loader:
    try:
        spec.loader.exec_module(merge_observation)
    except FileNotFoundError:
        pass


def test_parent_receipt_is_canonical_and_rejects_changed_immutable_facts(tmp_path):
    facts = {"canonical_repository_id": "R_1", "owner_kind": "goal", "owner_id": "2577",
             "goal": "2577", "head_ref": "sdlc/2577", "base_ref": "main", "pr_number": 7,
             "pr_node_id": "PR_1", "creating_writer": "work.pr", "pr_created_at": "2026-01-01T00:00:00Z"}
    data = merge_observation.parent_receipt(facts)
    assert data.endswith(b"\n") and b" " not in data
    path = tmp_path / "parent.json"
    merge_observation.write_immutable(path, data)
    merge_observation.write_immutable(path, data)
    with pytest.raises(ValueError, match="immutable"):
        merge_observation.write_immutable(path, data.replace(b'"main"', b'"other"'))


def test_observation_keys_are_stable_and_distinct():
    ownership = "a" * 64
    merge = "b" * 40
    assert merge_observation.observation_keys(ownership, merge) == merge_observation.observation_keys(ownership, merge)
    assert merge_observation.observation_keys(ownership, merge)[0] != merge_observation.observation_keys(ownership, merge)[1]


def test_receipt_publish_path_is_closed_over_canonical_receipt_names():
    sync_spec = importlib.util.spec_from_file_location("sync", SCRIPTS / "sync.py")
    sync = importlib.util.module_from_spec(sync_spec); sync_spec.loader.exec_module(sync)
    key = "a" * 64
    assert sync.receipt_relative_path(f"receipts/v1/{key}/parent.json") == f"receipts/v1/{key}/parent.json"
    with pytest.raises(ValueError):
        sync.receipt_relative_path("../entries/anything")


def test_receipt_publish_requires_the_configured_full_signer_on_its_own_commit():
    sync_spec = importlib.util.spec_from_file_location("sync", SCRIPTS / "sync.py")
    sync = importlib.util.module_from_spec(sync_spec); sync_spec.loader.exec_module(sync)
    signer = "aabbccddeeff00112233445566778899aabbccdd"
    config = {"ledger": {"receipt_authority": {"trusted_signers": [signer]}}}

    def git(_path, args):
        if args[:3] == ["log", "-1", "--format=%H"]:
            return "b" * 40
        if args[:2] == ["verify-commit", "--raw"]:
            return "[GNUPG:] VALIDSIG " + signer.upper() + " 2026-01-01"
        raise AssertionError(args)

    assert sync.verify_receipt_commit("ledger", "receipts/v1/" + "b" * 64 + "/parent.json",
                                      config, git) == "b" * 40
    with pytest.raises(ValueError, match="trusted signer"):
        sync.verify_receipt_commit("ledger", "receipts/v1/" + "b" * 64 + "/parent.json",
                                   {"ledger": {"receipt_authority": {"trusted_signers": []}}}, git)


def test_receipt_publish_does_not_push_when_the_signer_authority_is_unconfigured(tmp_path):
    sync_spec = importlib.util.spec_from_file_location("sync", SCRIPTS / "sync.py")
    sync = importlib.util.module_from_spec(sync_spec); sync_spec.loader.exec_module(sync)
    sdlc = tmp_path / ".sdlc"; ledger_root = sdlc / "ledger"; ledger_root.mkdir(parents=True)
    (ledger_root / ".git").write_text("gitdir: ignored\n")
    relative = "receipts/v1/" + "a" * 64 + "/parent.json"
    target = ledger_root / relative; target.parent.mkdir(parents=True); target.write_text("{}\n")
    calls = []

    def git(_path, args):
        calls.append(args)
        if args[:1] == ["hash-object"]:
            return "b" * 40
        if args[:1] == ["add"]:
            return ""
        if args[:2] == ["diff", "--cached"]:
            return ""
        if args[:2] == ["rev-parse", "HEAD"]:
            return "c" * 40
        raise AssertionError(args)

    result = sync.publish_receipt(sdlc, relative, config={"ledger": {"enabled": True}}, run=git)
    assert "no full trusted signer" in result
    assert not any(args[:1] == ["push"] for args in calls)


def test_template_makes_receipt_authority_an_explicit_opt_in():
    template = json.loads((SCRIPTS.parent.parent / "sigma-init" / "templates" / "config.json.tmpl").read_text())
    ledger = template["ledger"]
    assert ledger["receipt_sharing"] is False
    assert ledger["receipt_authority"]["trusted_signers"] == []


def test_signature_status_requires_exact_trusted_full_fingerprint():
    status = "[GNUPG:] VALIDSIG AABBCCDDEEFF00112233445566778899AABBCCDD 2026-01-01"
    assert merge_observation.trusted_signature(status, ["aabbccddeeff00112233445566778899aabbccdd"])
    assert not merge_observation.trusted_signature(status, ["AABBCCDD"])


def test_receipt_index_classifies_missing_valid_and_conflicting_parents(tmp_path):
    parent = {"canonical_repository_id": "R", "owner_kind": "goal", "owner_id": "1", "goal": "1",
              "head_ref": "sdlc/1", "base_ref": "main", "pr_number": 1, "pr_node_id": "P",
              "creating_writer": "work.pr", "pr_created_at": "2026-01-01T00:00:00Z"}
    key = merge_observation.ownership_key(parent)
    root = tmp_path / "receipts" / "v1" / key; root.mkdir(parents=True)
    (root / "parent.json").write_bytes(merge_observation.parent_receipt(parent))
    index = merge_observation.receipt_index(tmp_path / "receipts" / "v1")
    assert merge_observation.classify_receipt(index, {"repository": "R", "node_id": "P", "number": 1, "head_ref": "sdlc/1"}) == "valid-owned"
    assert merge_observation.classify_receipt(index, {"repository": "R", "node_id": "X", "number": 2, "head_ref": "sdlc/2"}) == "missing/unowned"


def test_coverage_scan_reports_pinned_snapshot_counts(tmp_path):
    result = merge_observation.coverage_scan(tmp_path, [{"repository": "R", "node_id": "X", "number": 1, "head_ref": "sdlc/1"}])
    assert result["receipt_snapshot"] == str(tmp_path)
    assert result["counts"]["missing/unowned"] == 1


def test_pagination_manifest_refuses_resume_drift_and_counts_1070_rows():
    rows = [{"number": i, "merged_at": "2026-01-01T00:00:00Z", "head_ref": "sdlc/x", "merge_sha": "a" * 40} for i in range(1070)]
    pages = [{"input_cursor": str(i), "end_cursor": str(i + 1), "rows": rows[i * 100:(i + 1) * 100]} for i in range(11)]
    manifest = merge_observation.pagination_manifest(pages)
    assert manifest["full_cohort"] == 1070 and len(manifest["pages"]) == 11
    with pytest.raises(ValueError, match="fixed size"):
        merge_observation.pagination_manifest([{"rows": rows[:101]}])
    with pytest.raises(ValueError, match="cap"):
        merge_observation.pagination_manifest(pages, max_pages=10)
    with pytest.raises(ValueError, match="drift"):
        merge_observation.pagination_manifest([{**pages[-1], "rows": []}], resume=manifest)


def test_coverage_requires_a_canonical_matching_child_and_preserves_malformed_parent_as_invalid(tmp_path):
    parent = {"canonical_repository_id": "R", "owner_kind": "goal", "owner_id": "1", "goal": "1",
              "head_ref": "sdlc/1", "base_ref": "main", "pr_number": 1, "pr_node_id": "P",
              "creating_writer": "work.pr", "pr_created_at": "2026-01-01T00:00:00Z"}
    key = merge_observation.ownership_key(parent)
    path = tmp_path / "receipts" / "v1" / key
    path.mkdir(parents=True)
    (path / "parent.json").write_bytes(merge_observation.parent_receipt(parent))
    pr = {"repository": "R", "node_id": "P", "number": 1, "head_ref": "sdlc/1",
          "merge_sha": "b" * 40, "merged_at": "2026-01-02T00:00:00Z"}
    assert merge_observation.coverage_scan(tmp_path, [pr])["counts"]["owned-unobserved-pending"] == 1
    child = path / "merges" / ("b" * 40 + ".json")
    child.parent.mkdir()
    child.write_bytes(merge_observation.child_receipt(json.loads((path / "parent.json").read_text()),
                                                               {"merge_sha": "b" * 40, "github_merged_at": pr["merged_at"]}))
    assert merge_observation.coverage_scan(tmp_path, [pr])["counts"]["observed-valid-owned"] == 1
    child.write_text("{}\n")
    assert merge_observation.coverage_scan(tmp_path, [pr])["counts"]["invalid"] == 1


def test_remote_snapshot_refuses_bad_ref_and_pins_a_fetched_sha():
    calls = []
    def git(command):
        calls.append(command)
        args = command[3:]
        if args[0] == "fetch": return ""
        if args[-1].endswith("^{tree}"): return "b" * 40
        return "a" * 40
    assert merge_observation.resolve_remote_snapshot("repo", run=git)["commit"] == "a" * 40
    assert calls[0][3:6] == ["fetch", "--no-tags", "origin"]
    with pytest.raises(ValueError, match="not a Git commit"):
        merge_observation.resolve_remote_snapshot("repo", run=lambda command: "not-a-sha")


def test_measured_rollout_accepts_only_a_remote_signed_nonempty_finished_cohort(tmp_path, monkeypatch):
    """Removing any authority/interval/receipt control must turn this acceptance red.

    STUB-BASED SMOKE TEST, kept as #2680's starting point. It stubs git -- the signature arrives as
    stdout and `merge-base --is-ancestor` always succeeds -- which is exactly why it passed while the
    real path cannot work (#2680). It enables the unsupported path explicitly; it is NOT evidence
    that receipt sharing works, and #2680 replaces it with real-git controls."""
    monkeypatch.setattr(merge_observation, "RECEIPT_SHARING_SUPPORTED", True)
    manifest = {"canonical_repository_id": "R_1", "release_tag": "v1.2.0",
                "release_commit_sha": "b" * 40,
                "receipt_publication_rollout_at": "2026-01-01T00:00:00Z"}
    data = merge_observation.release_manifest.payload(manifest)
    snapshot = tmp_path / "snapshot"
    (snapshot / "releases").mkdir(parents=True)
    (snapshot / "releases" / "receipt-publication-v1.json").write_bytes(data)
    parent = {"canonical_repository_id": "R_1", "owner_kind": "goal", "owner_id": "1", "goal": "1",
              "head_ref": "sdlc/1", "base_ref": "main", "pr_number": 1, "pr_node_id": "P",
              "creating_writer": "work.pr", "pr_created_at": "2026-01-01T00:00:00Z"}
    key = merge_observation.ownership_key(parent)
    receipt = snapshot / "receipts" / "v1" / key; receipt.mkdir(parents=True)
    (receipt / "parent.json").write_bytes(merge_observation.parent_receipt(parent))
    pr = {"repository": "R_1", "node_id": "P", "number": 1, "head_ref": "sdlc/1", "head_sha": "c" * 40,
          "base_ref": "main", "merge_sha": "b" * 40, "merged_at": "2026-01-02T00:00:00Z"}
    child = receipt / "merges" / ("b" * 40 + ".json"); child.parent.mkdir()
    child.write_bytes(merge_observation.child_receipt(json.loads((receipt / "parent.json").read_text()), {"merge_sha": "b" * 40,
                                                                 "github_merged_at": pr["merged_at"]}))
    entry_key, _ = merge_observation.observation_keys(key, pr["merge_sha"])
    (snapshot / "entries").mkdir()
    (snapshot / "entries" / "writer.jsonl").write_text(json.dumps({"kind": "merged", "pr": 1,
        "merged_entry_key": entry_key}) + "\n")
    (tmp_path / ".sdlc" / "events").mkdir(parents=True)
    (tmp_path / ".sdlc" / "events" / "writer.jsonl").write_text(json.dumps({"kind": "review_posted", "pr": 1,
        "head_sha": pr["head_sha"], "brief_hash": "d" * 64}) + "\n")
    monkeypatch.setattr(merge_observation, "resolve_remote_snapshot",
                        lambda *a, **kw: {"commit": "c" * 40, "tree": "d" * 40,
                                           "remote": "origin", "branch": "sdlc-ledger"})
    monkeypatch.setattr(merge_observation, "materialize_snapshot", lambda *a, **kw: snapshot)
    monkeypatch.setattr(merge_observation, "fetch_release_tag", lambda *a, **kw: "refs/sdlc/test-tag")
    monkeypatch.setattr(merge_observation, "github_repository_from_origin", lambda *a, **kw: "owner/repo")
    monkeypatch.setattr(merge_observation.release_manifest, "verify_release_authority",
                        lambda *a, **kw: manifest)
    monkeypatch.setattr(merge_observation, "verify_receipt_history", lambda *a, **kw: 2)
    monkeypatch.setattr(merge_observation, "github_merged_pr_snapshot",
                        lambda *a, **kw: {"canonical_repository_id": "R_1", "prs": [pr],
                                           "pagination": {"pages": [{"page": 1}]}})
    result = merge_observation.measured_rollout(str(tmp_path), ["aabbccddeeff00112233445566778899aabbccdd"],
                                                now="2026-01-15T00:00:00Z")
    assert result["acceptance_eligible"] is True
    assert result["receipt_authority"] == "remote-pinned"
    assert result["receipt_snapshot_commit"] == "c" * 40


def test_acceptance_mutations_cannot_accept_an_incomplete_or_unobserved_cohort():
    """These are deliberate red controls for the tempting `True`/partial-coverage mutations."""
    rollout = "2026-01-01T00:00:00Z"
    end = "2026-01-15T00:00:00Z"
    assert not merge_observation.release_manifest.acceptance_eligible(rollout, rollout, end, "2026-01-14T23:59:59Z")
    assert not merge_observation.rollout_acceptance_safe({"full_cohort": 1, "counts": {
        "observed-valid-owned": 0, "owned-unobserved-pending": 1, "missing/unowned": 0,
        "conflicting": 0, "invalid": 0}})
    one = {"full_cohort": 1, "counts": {"observed-valid-owned": 1,
           "owned-unobserved-pending": 0, "missing/unowned": 0, "conflicting": 0, "invalid": 0},
           "journal_covers_cohort": True,
           "sink_coverage": {"merged": {"observed": 1, "total": 1},
                             "review": {"observed": 1, "total": 1}}}
    assert merge_observation.rollout_acceptance_safe(one)
    ninety_five_each = {"full_cohort": 20, "counts": {"observed-valid-owned": 19,
                        "owned-unobserved-pending": 1, "missing/unowned": 0,
                        "conflicting": 0, "invalid": 0},
                        "journal_covers_cohort": True,
                        "sink_coverage": {"merged": {"observed": 19, "total": 20},
                                          "review": {"observed": 19, "total": 20}}}
    assert merge_observation.rollout_acceptance_safe(ninety_five_each)
    ninety_five_each["sink_coverage"]["review"]["observed"] = 18
    assert not merge_observation.rollout_acceptance_safe(ninety_five_each)
    # A fully-observed cohort the local journal cannot speak for is REFUSED, not certified: the
    # review sink reads only this machine's events, so a second actor's postings are absent here.
    ninety_five_each["sink_coverage"]["review"]["observed"] = 19
    assert merge_observation.rollout_acceptance_safe(ninety_five_each)   # non-vacuity: 19/20 passes
    ninety_five_each["journal_covers_cohort"] = False
    assert not merge_observation.rollout_acceptance_safe(ninety_five_each)


def test_github_snapshot_pages_all_merged_sdlc_prs_and_filters_the_fixed_window():
    replies = [
        {"data": {"repository": {"id": "R_1", "nameWithOwner": "owner/repo", "pullRequests": {
            "nodes": [{"id": "P1", "number": 1, "headRefName": "sdlc/1", "headRefOid": "c" * 40, "baseRefName": "main",
                       "mergedAt": "2026-01-02T00:00:00Z", "mergeCommit": {"oid": "a" * 40}}],
            "pageInfo": {"hasNextPage": True, "endCursor": "next"}}}}},
        {"data": {"repository": {"id": "R_1", "nameWithOwner": "owner/repo", "pullRequests": {
            "nodes": [{"id": "P2", "number": 2, "headRefName": "feature/not-a-goal", "headRefOid": "d" * 40, "baseRefName": "main",
                       "mergedAt": "2026-01-16T00:00:00Z", "mergeCommit": {"oid": "b" * 40}}],
            "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}]
    snapshot = merge_observation.github_merged_pr_snapshot("owner/repo", "2026-01-01T00:00:00Z",
        "2026-01-15T00:00:00Z", token="test", request=lambda *args: replies.pop(0))
    assert [row["node_id"] for row in snapshot["prs"]] == ["P1"]
    assert snapshot["prs"][0]["head_sha"] == "c" * 40
    assert len(snapshot["pagination"]["pages"]) == 2


def test_measured_coverage_requires_keyed_merged_entry_and_review_evidence(tmp_path):
    parent = {"canonical_repository_id": "R", "owner_kind": "goal", "owner_id": "1", "goal": "1",
              "head_ref": "sdlc/1", "base_ref": "main", "pr_number": 1, "pr_node_id": "P",
              "creating_writer": "work.pr", "pr_created_at": "2026-01-01T00:00:00Z"}
    key = merge_observation.ownership_key(parent); receipt = tmp_path / "receipts" / "v1" / key
    receipt.mkdir(parents=True); parent_data = json.loads(merge_observation.parent_receipt(parent))
    (receipt / "parent.json").write_bytes(merge_observation.parent_receipt(parent))
    pr = {"repository": "R", "node_id": "P", "number": 1, "head_ref": "sdlc/1", "head_sha": "c" * 40,
          "merge_sha": "b" * 40, "merged_at": "2026-01-02T00:00:00Z"}
    child = receipt / "merges" / ("b" * 40 + ".json"); child.parent.mkdir()
    child.write_bytes(merge_observation.child_receipt(parent_data, {"merge_sha": "b" * 40,
                                                                      "github_merged_at": pr["merged_at"]}))
    entry_key, _ = merge_observation.observation_keys(key, pr["merge_sha"])
    (tmp_path / "entries").mkdir(); (tmp_path / "events").mkdir()
    (tmp_path / "entries" / "writer.jsonl").write_text(json.dumps({"kind": "merged", "pr": 1,
        "merged_entry_key": entry_key}) + "\n")
    (tmp_path / "events" / "writer.jsonl").write_text(json.dumps({"kind": "review_posted", "pr": 1,
        "head_sha": pr["head_sha"], "brief_hash": "d" * 64}) + "\n")
    # A legacy remote events directory is not journal authority.  No local author journal means
    # this cohort row must under-count instead of being accepted from a stale shared fixture.
    missing = merge_observation.coverage_scan(tmp_path, [pr], require_measurements=True, journal_root=tmp_path)
    assert missing["counts"]["owned-unobserved-pending"] == 1
    assert missing["sink_coverage"] == {
        "merged": {"observed": 1, "total": 1, "rate": 1.0},
        "review": {"observed": 0, "total": 1, "rate": 0.0},
    }
    (tmp_path / ".sdlc" / "events").mkdir(parents=True)
    (tmp_path / ".sdlc" / "events" / "writer.jsonl").write_text(json.dumps({"kind": "review_posted", "pr": 1,
        "head_sha": pr["head_sha"], "brief_hash": "d" * 64}) + "\n")
    scan = merge_observation.coverage_scan(tmp_path, [pr], require_measurements=True, journal_root=tmp_path)
    assert scan["counts"]["observed-valid-owned"] == 1
    assert scan["sink_coverage"] == {
        "merged": {"observed": 1, "total": 1, "rate": 1.0},
        "review": {"observed": 1, "total": 1, "rate": 1.0},
    }
    assert scan["entry_authority"] == "remote-pinned"
    assert scan["journal_authority"] == "local-author-machine"
    (tmp_path / ".sdlc" / "events" / "writer.jsonl").write_text(json.dumps({"kind": "review_posted", "pr": 1,
        "head_sha": pr["head_sha"], "brief_hash": "not-a-hash"}) + "\n")
    malformed = merge_observation.coverage_scan(tmp_path, [pr], require_measurements=True, journal_root=tmp_path)
    assert malformed["counts"]["owned-unobserved-pending"] == 1
    assert malformed["sink_coverage"]["merged"]["rate"] == 1.0
    assert malformed["sink_coverage"]["review"]["rate"] == 0.0


def test_measured_coverage_accepts_the_string_pr_written_by_the_ledger(tmp_path):
    """The shared entry writer keeps historical ``pr`` fields as strings."""
    parent = {"canonical_repository_id": "R", "owner_kind": "goal", "owner_id": "1", "goal": "1",
              "head_ref": "sdlc/1", "base_ref": "main", "pr_number": 1, "pr_node_id": "P",
              "creating_writer": "work.pr", "pr_created_at": "2026-01-01T00:00:00Z"}
    key = merge_observation.ownership_key(parent); receipt = tmp_path / "receipts" / "v1" / key
    receipt.mkdir(parents=True); parent_data = json.loads(merge_observation.parent_receipt(parent))
    (receipt / "parent.json").write_bytes(merge_observation.parent_receipt(parent))
    pr = {"repository": "R", "node_id": "P", "number": 1, "head_ref": "sdlc/1", "head_sha": "c" * 40,
          "merge_sha": "b" * 40, "merged_at": "2026-01-02T00:00:00Z"}
    child = receipt / "merges" / ("b" * 40 + ".json"); child.parent.mkdir()
    child.write_bytes(merge_observation.child_receipt(parent_data, {"merge_sha": "b" * 40,
                                                                      "github_merged_at": pr["merged_at"]}))
    entry_key, _ = merge_observation.observation_keys(key, pr["merge_sha"])
    (tmp_path / "entries").mkdir()
    (tmp_path / "entries" / "writer.jsonl").write_text(json.dumps({"kind": "merged", "pr": "1",
        "merged_entry_key": entry_key}) + "\n")
    (tmp_path / ".sdlc" / "events").mkdir(parents=True)
    (tmp_path / ".sdlc" / "events" / "writer.jsonl").write_text(json.dumps({"kind": "review_posted", "pr": 1,
        "head_sha": pr["head_sha"], "brief_hash": "d" * 64}) + "\n")
    scan = merge_observation.coverage_scan(tmp_path, [pr], require_measurements=True, journal_root=tmp_path)
    assert scan["counts"]["observed-valid-owned"] == 1


def test_receipt_history_authority_requires_signed_immutable_post_anchor_introductions(tmp_path):
    root = tmp_path / "snapshot"; receipt = root / "receipts" / "v1" / ("a" * 64); receipt.mkdir(parents=True)
    parent = {"canonical_repository_id": "R", "owner_kind": "goal", "owner_id": "1", "goal": "1",
              "head_ref": "sdlc/1", "base_ref": "main", "pr_number": 1, "pr_node_id": "P",
              "creating_writer": "work.pr", "pr_created_at": "2026-01-01T00:00:00Z"}
    parent_bytes = merge_observation.parent_receipt(parent)
    parent_data = json.loads(parent_bytes); receipt = root / "receipts" / "v1" / parent_data["ownership_key"]
    (root / "receipts" / "v1" / ("a" * 64)).rmdir(); receipt.mkdir()
    parent_path = receipt / "parent.json"; parent_path.write_bytes(parent_bytes)
    child = receipt / "merges" / ("b" * 40 + ".json"); child.parent.mkdir()
    child.write_bytes(merge_observation.child_receipt(parent_data, {"merge_sha": "b" * 40,
                                                                      "github_merged_at": "2026-01-02T00:00:00Z"}))
    anchor, pinned, intro, blob = "a" * 40, "b" * 40, "c" * 40, "d" * 40
    def git(command):
        args = command[3:]
        if args[:2] == ["merge-base", "--is-ancestor"]: return ""
        if args[:1] == ["log"] and "--diff-filter=A" in args: return intro
        if args[:2] == ["verify-commit", "--raw"]: return "[GNUPG:] VALIDSIG AABBCCDDEEFF00112233445566778899AABBCCDD"
        if args[:1] == ["rev-parse"]: return blob
        if args[:1] == ["log"]: return ""
        raise AssertionError(args)
    assert merge_observation.verify_receipt_history("repo", root, anchor, pinned,
        ["aabbccddeeff00112233445566778899aabbccdd"], git) == 2
    def mutated(command):
        args = command[3:]
        if args[:1] == ["log"] and "--diff-filter=A" not in args:
            return "e" * 40
        return git(command)
    with pytest.raises(ValueError, match="mutated"):
        merge_observation.verify_receipt_history("repo", root, anchor, pinned,
                                                  ["aabbccddeeff00112233445566778899aabbccdd"], mutated)


def test_coverage_cli_is_the_measured_rollout_gesture(monkeypatch, capsys):
    expected = {"acceptance_eligible": True, "full_cohort": 1}
    monkeypatch.setattr(merge_observation, "measured_rollout", lambda *args, **kwargs: expected)
    assert merge_observation.main(["coverage", "--ledger-repo", "repo", "--trusted-signer",
                                   "aabbccddeeff00112233445566778899aabbccdd"]) is None
    assert json.loads(capsys.readouterr().out) == expected


def test_a_second_actor_in_the_shared_entries_refuses_the_locally_measured_review_sink(tmp_path):
    """A cohort spanning two machines cannot be certified from ONE machine's review journal.

    Measured on the maintainers' repository while writing it: `.sdlc/ledger/entries/` already
    carries two distinct actors, so this is the repository's current state,
    not a hypothetical. The other actor's `review_posted` events live on that other machine, so
    the review rate read here would understate reality however faithfully that machine posted.
    """
    parent = {"canonical_repository_id": "R", "owner_kind": "goal", "owner_id": "1", "goal": "1",
              "head_ref": "sdlc/1", "base_ref": "main", "pr_number": 1, "pr_node_id": "P",
              "creating_writer": "work.pr", "pr_created_at": "2026-01-01T00:00:00Z"}
    key = merge_observation.ownership_key(parent); receipt = tmp_path / "receipts" / "v1" / key
    receipt.mkdir(parents=True); parent_data = json.loads(merge_observation.parent_receipt(parent))
    (receipt / "parent.json").write_bytes(merge_observation.parent_receipt(parent))
    pr = {"repository": "R", "node_id": "P", "number": 1, "head_ref": "sdlc/1", "head_sha": "c" * 40,
          "merge_sha": "b" * 40, "merged_at": "2026-01-02T00:00:00Z"}
    child = receipt / "merges" / ("b" * 40 + ".json"); child.parent.mkdir()
    child.write_bytes(merge_observation.child_receipt(parent_data, {"merge_sha": "b" * 40,
                                                                   "github_merged_at": pr["merged_at"]}))
    entry_key, _ = merge_observation.observation_keys(key, pr["merge_sha"])
    (tmp_path / "entries").mkdir()
    (tmp_path / ".sdlc" / "events").mkdir(parents=True)
    (tmp_path / ".sdlc" / "events" / "writer.jsonl").write_text(json.dumps({"kind": "review_posted",
        "pr": 1, "head_sha": pr["head_sha"], "brief_hash": "d" * 64}) + "\n")

    def scan_with(*actors):
        (tmp_path / "entries" / "writer.jsonl").write_text("".join(
            json.dumps({"kind": "merged", "pr": 1, "merged_entry_key": entry_key, "actor": actor})
            + "\n" for actor in actors))
        return merge_observation.coverage_scan(tmp_path, [pr], require_measurements=True,
                                               journal_root=tmp_path)

    # Non-vacuity: one actor is fully observed AND accepted, so the refusal below is about the
    # second actor and not about a cohort this gate rejects no matter what.
    single = scan_with("bob")
    assert single["entry_actors"] == ["bob"]
    assert single["journal_covers_cohort"] is True
    assert single["sink_coverage"]["review"]["rate"] == 1.0
    assert merge_observation.rollout_acceptance_safe(single) is True

    both = scan_with("bob", "alice")
    assert both["entry_actors"] == ["alice", "bob"]
    assert both["journal_covers_cohort"] is False
    # The sinks still read fully observed -- it is the AUTHORITY that fails, not the coverage.
    assert both["sink_coverage"]["review"]["rate"] == 1.0
    assert merge_observation.rollout_acceptance_safe(both) is False


def test_a_merged_pr_with_no_resolvable_merge_commit_stays_in_the_cohort_instead_of_aborting():
    """GitHub returns `mergeCommit: null` for some genuinely merged PRs, and that is not fatal.

    Measured against the live repository while fixing this: #684 and #681 (merged 2026-08-10)
    both come back `mergedAt` set / `mergeCommit: null`, and the old strict row check raised
    `GitHub cohort row is unsafe` on them -- aborting the entire snapshot over 2 rows out of 870,
    so the acceptance measurement could never complete here. Such a row keeps its place in the
    denominator with `merge_sha=None`; because receipts are keyed BY merge SHA it can never match
    a child, so it stays honestly unobserved rather than silently dropped.
    """
    replies = [
        {"data": {"repository": {"id": "R_1", "nameWithOwner": "owner/repo", "pullRequests": {
            "nodes": [{"id": "P1", "number": 684, "headRefName": "sdlc/audit-collect",
                       "headRefOid": "c" * 40, "baseRefName": "main",
                       "mergedAt": "2026-01-02T00:00:00Z", "mergeCommit": None},
                      {"id": "P2", "number": 2, "headRefName": "sdlc/2", "headRefOid": "d" * 40,
                       "baseRefName": "main", "mergedAt": "2026-01-03T00:00:00Z",
                       "mergeCommit": {"oid": "a" * 40}}],
            "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}]
    snapshot = merge_observation.github_merged_pr_snapshot("owner/repo", "2026-01-01T00:00:00Z",
        "2026-01-15T00:00:00Z", token="test", request=lambda *args: replies.pop(0))
    # Non-vacuity: BOTH rows survive, so the row with a merge SHA proves the walk still works.
    assert [row["number"] for row in snapshot["prs"]] == [684, 2]
    assert snapshot["prs"][0]["merge_sha"] is None
    assert snapshot["prs"][1]["merge_sha"] == "a" * 40

    # A malformed (rather than absent) merge SHA is still a loud refusal.
    bad = [{"data": {"repository": {"id": "R_1", "nameWithOwner": "owner/repo", "pullRequests": {
        "nodes": [{"id": "P3", "number": 3, "headRefName": "sdlc/3", "headRefOid": "c" * 40,
                   "baseRefName": "main", "mergedAt": "2026-01-02T00:00:00Z",
                   "mergeCommit": {"oid": "not-a-sha"}}],
        "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}]
    with pytest.raises(ValueError, match="row is unsafe"):
        merge_observation.github_merged_pr_snapshot("owner/repo", "2026-01-01T00:00:00Z",
            "2026-01-15T00:00:00Z", token="test", request=lambda *args: bad.pop(0))


def test_receipt_backed_rollout_acceptance_is_refused_until_2680():
    """Receipt sharing is known-broken against real git (#2680: signatures read from stdout,
    ancestry checked across unrelated histories, a stale post-rebase SHA). The acceptance
    measurement is the command that makes a CLAIM, so it refuses outright -- and before it runs a
    single git command, so no partial measurement can be mistaken for a verdict."""
    calls = []
    with pytest.raises(ValueError, match="not supported yet.*#2680"):
        merge_observation.measured_rollout("repo", ["a" * 40], run=lambda argv: calls.append(argv) or "")
    assert calls == [], calls


# --- Review round 5 (author-blind Claude subagent, generation a80caf88 at 0e8736d6) -------------------

def test_coverage_scan_refuses_a_merge_sha_that_escapes_the_receipts_tree(tmp_path, monkeypatch):
    """Finding 1. `coverage_scan` joined `str(merge_sha) + ".json"` into a path under the receipts
    tree with NO format check inside the function itself -- only `github_merged_pr_snapshot`
    validated it, at ONE caller. `coverage_scan` is also reached directly from the `coverage` CLI's
    --snapshot/--prs, which parses an operator-supplied JSON file with no validation at all, so an
    absolute-path-shaped `merge_sha` reaches `child_path` unchecked. pathlib's own `/` operator
    discards every earlier segment when the right side looks absolute, so
    `receipts / key / "merges" / ("/etc/passwd" + ".json")` becomes `/etc/passwd.json` --
    outside the receipts tree entirely.

    A classification-only assertion is not enough here: a planted file that merely fails the
    byte-for-byte receipt comparison classifies "invalid" whether or not it was ever opened, so
    that outcome cannot distinguish "rejected before touching the filesystem" from "opened, then
    rejected" -- exactly the gap that let this bug hide behind a passing-looking result. Instead,
    record every path this process actually reads and assert the escaping one is never among them.
    """
    parent = {"canonical_repository_id": "R", "owner_kind": "goal", "owner_id": "1", "goal": "1",
              "head_ref": "sdlc/1", "base_ref": "main", "pr_number": 1, "pr_node_id": "P",
              "creating_writer": "work.pr", "pr_created_at": "2026-01-01T00:00:00Z"}
    key = merge_observation.ownership_key(parent)
    path = tmp_path / "receipts" / "v1" / key
    path.mkdir(parents=True)
    (path / "parent.json").write_bytes(merge_observation.parent_receipt(parent))
    outside = tmp_path / "outside-the-receipts-tree.json"
    outside.write_text('{"planted": "should never be opened"}')
    escaping_sha = str(outside).rsplit(".json", 1)[0]   # so + ".json" reconstructs `outside`

    opened = []
    real_read_text, real_read_bytes = pathlib.Path.read_text, pathlib.Path.read_bytes
    def spy_read_text(self, *a, **k):
        opened.append(self); return real_read_text(self, *a, **k)
    def spy_read_bytes(self, *a, **k):
        opened.append(self); return real_read_bytes(self, *a, **k)
    monkeypatch.setattr(pathlib.Path, "read_text", spy_read_text)
    monkeypatch.setattr(pathlib.Path, "read_bytes", spy_read_bytes)

    pr = {"repository": "R", "node_id": "P", "number": 1, "head_ref": "sdlc/1",
          "merge_sha": escaping_sha, "merged_at": "2026-01-02T00:00:00Z"}
    result = merge_observation.coverage_scan(tmp_path, [pr])
    assert result["counts"]["invalid"] == 1, result["counts"]
    assert outside not in opened, "a merge_sha-controlled path outside the receipts tree was opened"
    assert any(path.name in str(p) for p in opened), "the control never even reached a file read"


def test_coverage_scan_still_classifies_a_real_merge_sha_correctly(tmp_path):
    """Non-vacuity for the control above: a real 40-char hex merge SHA still classifies normally,
    so the refusal is about the escaping shape, not about `coverage_scan` rejecting everything."""
    parent = {"canonical_repository_id": "R", "owner_kind": "goal", "owner_id": "1", "goal": "1",
              "head_ref": "sdlc/1", "base_ref": "main", "pr_number": 1, "pr_node_id": "P",
              "creating_writer": "work.pr", "pr_created_at": "2026-01-01T00:00:00Z"}
    key = merge_observation.ownership_key(parent)
    path = tmp_path / "receipts" / "v1" / key
    path.mkdir(parents=True)
    (path / "parent.json").write_bytes(merge_observation.parent_receipt(parent))
    pr = {"repository": "R", "node_id": "P", "number": 1, "head_ref": "sdlc/1",
          "merge_sha": "b" * 40, "merged_at": "2026-01-02T00:00:00Z"}
    assert merge_observation.coverage_scan(tmp_path, [pr])["counts"]["owned-unobserved-pending"] == 1


def test_github_repository_parses_every_real_origin_url_shape(tmp_path):
    """Should-fix (round 5). `_github_repository`'s pattern had a doubled backslash
    (`r"git@github\\\\.com:"`), so the regex matched a LITERAL backslash character before an
    unescaped `.` (matching any char) rather than an escaped literal dot -- it could never match
    any real github.com URL. Every existing caller mocks `github_repository_from_origin` (see
    `test_measured_rollout_...` above), which is exactly why this was invisible: nothing exercised
    the real regex. Currently masked in production only by RECEIPT_SHARING_SUPPORTED=False gating
    its sole caller (measured_rollout); fixed now, cheaply, before #2680 flips that flag and hits
    this on the very first real repository."""
    for url, expected in [
        ("git@github.com:owner/repo.git", "owner/repo"),
        ("ssh://git@github.com/owner/repo.git", "owner/repo"),
        ("https://github.com/owner/repo.git", "owner/repo"),
        ("https://github.com/owner/repo", "owner/repo"),
        ("https://github.com/owner/repo/", "owner/repo"),
    ]:
        assert merge_observation._github_repository(url) == expected, url
    with pytest.raises(ValueError):
        merge_observation._github_repository("https://gitlab.com/owner/repo")


def test_child_receipt_refuses_a_correctly_sized_non_hex_merge_sha(tmp_path):
    """Round-8 review finding. `child_receipt` checked `len(merge_sha) in (40, 64)` only, not
    charset -- the same class of gap round 5 fixed in `coverage_scan`. `work.py`'s
    `_observe_confirmed_merge` builds `receipts/v1/<key>/merges/<merge_sha>.json` straight from the
    same value `child_receipt` just accepted, so a correctly-SIZED but non-hex value (e.g. a path
    with `../` segments, 40 chars) passed this gate and would have escaped the receipts tree.
    Unreachable today (gated behind RECEIPT_SHARING_SUPPORTED=False, #2680), but the same
    defensive posture this file already applies elsewhere, applied consistently."""
    parent = {"ownership_key": "a" * 64, "canonical_repository_id": "R", "pr_number": 1, "pr_node_id": "P"}
    traversal = ("../" * 8 + "x" * 16)   # 40 chars, matches the length check, not hex
    assert len(traversal) == 40
    with pytest.raises(ValueError, match="invalid merge SHA"):
        merge_observation.child_receipt(parent, {"merge_sha": traversal, "github_merged_at": "2026-01-01T00:00:00Z"})
    # Non-vacuity: a real hex SHA of the same length still works.
    child = merge_observation.child_receipt(parent, {"merge_sha": "b" * 40, "github_merged_at": "2026-01-01T00:00:00Z"})
    assert json.loads(child)["merge_sha"] == "b" * 40


def test_931_landing_writer_is_allowlisted_and_an_unknown_writer_is_not():
    import importlib.util, pathlib
    path = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts" / "merge_observation.py"
    spec = importlib.util.spec_from_file_location("merge_observation_931", path)
    mo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mo)
    assert "verify_merge.land_unit" in mo._WRITERS
    assert {"work.pr", "unit_completion._draft", "verify_merge.ensure_landing_pr"} <= set(mo._WRITERS)
    facts = dict(canonical_repository_id="r", owner_kind="unit", owner_id="feature/u", goal=None, head_ref="feature/u",
                 base_ref="main", pr_number=1, pr_node_id="PR_1", creating_writer="verify_merge.land_unit",
                 pr_created_at="2026-01-01T00:00:00Z")
    assert mo.parent_receipt(facts)
    facts["creating_writer"] = "somebody.else"
    import pytest
    with pytest.raises(ValueError):
        mo.parent_receipt(facts)
