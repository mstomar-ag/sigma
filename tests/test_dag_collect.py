"""#1611: the full read for the stored dependency map, against a scripted repository (no network).

Every guard here was run red once against its own deliberate break (the `control:` line in each
docstring names the break)."""
import importlib.util
import json
import math
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
S = ROOT / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


dc = _mod("dag_collect")
ds = dc.dag_store
dag_cli = _mod("dag")

NOW = 1_800_000_000.0
REPO = "owner/repo"
CONFIG = {"discovery": {"github": {"repo": REPO, "assignee": "me-user"}, "dag": {"mode": "on"}}}


def _issue(number, labels=(), body="", state="open", pr=False, merged=False):
    it = {"number": number, "title": "fixture issue %d" % number, "body": body, "state": state,
          "labels": [{"name": n} for n in labels], "assignees": [], "updated_at": "2026-10-01T00:00:00Z"}
    if pr:
        it["pull_request"] = {"merged_at": "2026-10-01T00:00:00Z" if merged else None}
    return it


class Fake:
    """A scripted repository: list endpoint filters by label and paginates; single reads by number."""

    def __init__(self, issues, prs=None, fail=None):
        self.issues = {i["number"]: i for i in issues}
        self.prs = prs or {}                    # number -> {"state":..., "merged":...}
        self.fail = fail                        # callable(args) -> raise
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        if self.fail:
            self.fail(args)
        ep = args[1]
        fields = {}
        for i, a in enumerate(args):
            if a == "-f":
                k, _, v = args[i + 1].partition("=")
                fields[k] = v
        m = re.search(r"/(issues|pulls)/(\d+)$", ep)
        if m and m.group(1) == "issues":
            return json.dumps(self.issues[int(m.group(2))])
        if m:
            return json.dumps(self.prs[int(m.group(2))])
        want = [x for x in fields.get("labels", "").split(",") if x]
        rows = [i for i in self.issues.values() if i["state"] == "open"
                and all(w in [l["name"] for l in i["labels"]] for w in want)]
        rows.sort(key=lambda i: i["number"])
        per, page = int(fields["per_page"]), int(fields["page"])
        return json.dumps(rows[(page - 1) * per:page * per])

    def lists(self):
        return [c for c in self.calls if re.search(r"/issues$", c[1])]


def _by_num(rep):
    return {n["number"]: n for n in rep["map"]["nodes"]}


def _world():
    """Goal 1 blocked by: parked 2, follow-up-only 3, third-party 4, open PR 5; and 6 outside."""
    return Fake([
        _issue(1, ["sdlc:goal"], "Blocked by: #2\nBlocked by: #3\nBlocked by: #4\nBlocked by: #5"),
        _issue(2, ["sdlc:parked", "priority:P1"], ""),
        _issue(3, ["sdlc:followup"], ""),
        _issue(4, ["sdlc:blocking"], ""),
        _issue(5, [], "", pr=True),
    ], prs={5: {"state": "open", "merged": False}})


def test_blockers_outside_the_goal_label_are_nodes(tmp_path):
    """A parked, follow-up-only, third-party-labelled and pull-request blocker all appear, with the
    right life and state.
    control: fetch only the goal label (label_sets reduced to the goal) -> parked/follow-up absent."""
    rep = dc.collect(tmp_path, CONFIG, _world(), NOW)
    nodes = _by_num(rep)
    assert nodes[2]["life"] == "parked" and nodes[2]["prio"] == "P1" and nodes[2]["in_corpus"]
    assert nodes[3]["life"] == "needs-label" and nodes[3]["in_corpus"]
    assert nodes[4]["life"] == "blocking"
    assert nodes[5]["kind"] == "pr" and nodes[5]["state"] == "open" and not nodes[5]["in_corpus"]
    assert nodes[1]["life"] == "goal"
    assert {(e["blocker"], e["blocked"]) for e in rep["map"]["edges"]} == {(2, 1), (3, 1), (4, 1), (5, 1)}


def test_no_assignee_on_any_list_request(tmp_path):
    """The configured assignee never reaches a request.
    control: pass the configured assignee into list_issues -> an `assignee=` argument appears."""
    run = _world()
    dc.collect(tmp_path, CONFIG, run, NOW)
    assert len(run.lists()) == 5
    for c in run.calls:
        assert not any(str(a).startswith("assignee") for a in c)
        assert "me-user" not in " ".join(c)


def test_call_count_is_pages_plus_outside_reads_capped_at_25(tmp_path):
    """G goals, B blocked, P parked, F follow-ups: list calls = sum ceil(n/100) (at least one page
    per label set); outside reads stop at 25 and the remainder is `unread`.
    control: remove the cap (OUTSIDE_MAX huge) -> reads exceed 25 and `unread` is empty."""
    G, B, P, F = 230, 90, 3, 0
    issues, n = [], 1000
    for count, lab in ((G, "sdlc:goal"), (B, "sdlc:blocked"), (P, "sdlc:parked"), (F, "sdlc:followup")):
        for _ in range(count):
            n += 1
            issues.append(_issue(n, [lab]))
    refs = list(range(1, 31))                           # 30 outside numbers, all open issues
    issues[0]["body"] = "\n".join("Blocked by: #%d" % r for r in refs)
    issues += [_issue(r) for r in refs]
    run = Fake(issues)
    rep = dc.collect(tmp_path, CONFIG, run, NOW)
    pages = sum(max(1, math.ceil(c / 100)) for c in (G, B, P, F, 0))
    assert rep["calls"]["list"] == pages == 3 + 1 + 1 + 1 + 1
    assert rep["calls"]["read"] == 25
    assert sorted(rep["unread"]) == list(range(1, 6))   # newest first: 30..6 read, 5..1 unread
    assert all(_by_num(rep)[r]["state"] == "unknown" for r in rep["unread"])
    assert _by_num(rep)[30]["state"] == "open"
    assert not rep["partial"]


@pytest.mark.parametrize("pr,expect", [
    ({"state": "closed", "merged": True}, "merged"),
    ({"state": "closed", "merged": False}, "closed-unmerged"),
    ({"state": "open", "merged": False}, "open"),
    (None, "unknown"),
])
def test_pull_request_rule(tmp_path, pr, expect):
    """merged resolves, closed-unmerged does not, open is open, unreadable is unknown.
    control: treat any closed PR as merged -> the closed-unmerged case goes red."""
    run = Fake([_issue(1, ["sdlc:goal"], "Blocked by: #9"), _issue(9, [], "", pr=True)],
               prs={9: pr} if pr else {})
    rep = dc.collect(tmp_path, CONFIG, run, NOW)
    node = _by_num(rep)[9]
    assert node["kind"] == "pr" and node["state"] == expect
    g = dc.dag_core.build([dc.dag_core.Node(n["number"], n["kind"], n["state"], n["life"], n["prio"])
                           for n in rep["map"]["nodes"]],
                          [(e["blocker"], e["blocked"], e["source"]) for e in rep["map"]["edges"]])
    assert g.resolved(9) == (expect == "merged")


def test_resolved_outside_node_is_cached_seven_days(tmp_path):
    """A resolved outside node in the previous map is not read again inside seven days, and is read
    again after."""
    run = Fake([_issue(1, ["sdlc:goal"], "Blocked by: #9"), _issue(9, state="closed")])
    first = dc.collect(tmp_path, CONFIG, run, NOW)
    assert first["calls"]["read"] == 1
    prev = dict(first["map"])
    run2 = Fake([_issue(1, ["sdlc:goal"], "Blocked by: #9"), _issue(9, state="closed")])
    again = dc.collect(tmp_path, CONFIG, run2, NOW + 3 * 86400, prev)
    assert again["calls"]["read"] == 0 and _by_num(again)[9]["state"] == "closed"
    later = dc.collect(tmp_path, CONFIG, run2, NOW + 8 * 86400, prev)
    assert later["calls"]["read"] == 1


def _prev_with(read_at, state="closed", header_ok=None):
    """A saved map whose header is fresh at `header_ok` and whose node 9 was read at `read_at`."""
    node = {"number": 9, "kind": "issue", "state": state, "life": "none", "prio": None,
            "in_corpus": False}
    if read_at:
        node["read_at"] = read_at
    h = {"repo": REPO, "built_at": header_ok, "refreshed_at": header_ok, "last_ok_at": header_ok,
         "last_error": None, "partial": False, "cursor": None, "etag": None, "checked_at": None}
    return {"header": h, "nodes": [node], "edges": []}


def test_outside_node_stores_its_own_read_stamp(tmp_path):
    """control: drop read_at from the outside node -> the stamp is absent."""
    run = Fake([_issue(1, ["sdlc:goal"], "Blocked by: #9"), _issue(9, state="closed")])
    rep = dc.collect(tmp_path, CONFIG, run, NOW)
    assert _by_num(rep)[9]["read_at"] == dc._iso(NOW)


def test_expiry_follows_the_node_not_the_header(tmp_path):
    """A map refreshed a minute ago still re-reads a node last read 8 days ago.
    control: _cached uses the header last_ok_at again -> zero reads, this goes red."""
    now = NOW + 8 * 86400
    prev = _prev_with(dc._iso(NOW), header_ok=dc._iso(now - 60))
    run = Fake([_issue(1, ["sdlc:goal"], "Blocked by: #9"), _issue(9, state="closed")])
    rep = dc.collect(tmp_path, CONFIG, run, now, prev)
    assert rep["calls"]["read"] == 1


def test_reopened_issue_is_seen_open_after_expiry(tmp_path):
    """control: header-based expiry keeps the stale closed node -> state stays closed."""
    now = NOW + 8 * 86400
    prev = _prev_with(dc._iso(NOW), header_ok=dc._iso(now - 60))
    run = Fake([_issue(1, ["sdlc:goal"], "Blocked by: #9"), _issue(9, state="open")])
    rep = dc.collect(tmp_path, CONFIG, run, now, prev)
    assert _by_num(rep)[9]["state"] == "open"


def test_unexpired_node_is_carried_with_its_original_stamp(tmp_path):
    """control: refresh read_at on carry-forward -> the stamp changes and expiry never fires."""
    now = NOW + 3 * 86400
    stamp = dc._iso(NOW)
    prev = _prev_with(stamp, header_ok=dc._iso(now - 60))
    run = Fake([_issue(1, ["sdlc:goal"], "Blocked by: #9"), _issue(9, state="closed")])
    rep = dc.collect(tmp_path, CONFIG, run, now, prev)
    assert rep["calls"]["read"] == 0 and _by_num(rep)[9]["read_at"] == stamp


def test_node_without_a_read_stamp_is_read_again(tmp_path):
    """An older saved file has no read_at: re-read once rather than trust it.
    control: treat a missing stamp as fresh -> zero reads."""
    prev = _prev_with(None, header_ok=dc._iso(NOW - 60))
    run = Fake([_issue(1, ["sdlc:goal"], "Blocked by: #9"), _issue(9, state="closed")])
    rep = dc.collect(tmp_path, CONFIG, run, NOW, prev)
    assert rep["calls"]["read"] == 1 and _by_num(rep)[9]["read_at"] == dc._iso(NOW)


def test_empty_goal_list_with_other_lists_non_empty_keeps_the_file(tmp_path):
    """One list answering empty while the others do not is a short answer, not a repository state.
    control: reduce the guard to the all-empty check -> the map is published without the goals."""
    p, good = _published(tmp_path, _world())
    only_parked = Fake([_issue(2, ["sdlc:parked"]), _issue(3, ["sdlc:followup"])])
    rep = dc.refresh(tmp_path, CONFIG, only_parked, NOW + 10)
    assert rep["status"] == "kept" and rep["last_error"] == "EmptyList"
    assert p.read_bytes() == good


def _published(tmp_path, run):
    rep = dc.refresh(tmp_path, CONFIG, run, NOW)
    assert rep["status"] == "saved"
    return ds.path(tmp_path), ds.path(tmp_path).read_bytes()


def test_failed_list_leaves_file_byte_identical(tmp_path):
    """A transport failure on any list keeps the old file and reports the class.
    control: save the half-built result on failure -> the bytes change."""
    p, good = _published(tmp_path, _world())

    def boom(args):
        if "labels=sdlc:parked" in args:
            raise RuntimeError("network down")
    rep = dc.refresh(tmp_path, CONFIG, Fake(_world().issues.values(), fail=boom), NOW + 10)
    assert rep["status"] == "kept" and rep["last_error"] == "GhApiError"
    assert p.read_bytes() == good


def test_empty_backlog_publishes_a_usable_empty_map(tmp_path):
    """Every list read fine and all came back empty: that is a repository state, not an error (#1933).
    control: restore the EmptyResult branch -> the file is kept and last_error is set."""
    _published(tmp_path, _world())
    rep = dc.refresh(tmp_path, CONFIG, Fake([]), NOW + 10)
    assert rep["status"] == "saved" and rep["last_error"] is None and not rep["partial"]
    m, why = ds.load(tmp_path, CONFIG)
    assert why == "ok" and m["nodes"] == [] and m["edges"] == []
    assert m["header"]["last_error"] is None and m["header"]["partial"] is False
    assert ds.usable(m, NOW + 10, 60) == (True, "ok")


def test_failed_list_on_an_empty_backlog_still_keeps_the_file(tmp_path):
    """The exception path is unchanged: one list raising publishes nothing, empty or not.
    control: treat a raised list as empty -> the empty map replaces the good one."""
    p, good = _published(tmp_path, _world())

    def boom(args):
        if "labels=sdlc:parked" in args:
            raise RuntimeError("network down")
    rep = dc.refresh(tmp_path, CONFIG, Fake([], fail=boom), NOW + 10)
    assert rep["status"] == "kept" and rep["last_error"] == "GhApiError"
    assert p.read_bytes() == good


def test_non_list_payload_is_an_error_not_an_empty_list(tmp_path):
    p, good = _published(tmp_path, _world())
    rep = dc.refresh(tmp_path, CONFIG, lambda a: json.dumps({"message": "Not Found"}), NOW + 10)
    assert rep["status"] == "kept" and rep["last_error"] and p.read_bytes() == good


def test_cap_marks_partial_and_usable_refuses(tmp_path):
    """A list that reaches the cap marks the map partial; the picker's gate refuses it.
    control: ignore the cap clause (partial = False) -> usable accepts the map."""
    assert dc.LIST_CAP == 2000
    run = Fake([_issue(n, ["sdlc:goal"]) for n in range(1, 251)])
    rep = dc.refresh(tmp_path, CONFIG, run, NOW, cap=250)
    assert rep["partial"] and rep["status"] == "saved"
    m, why = ds.load(tmp_path, CONFIG)
    assert why == "ok" and m["header"]["partial"] is True
    assert ds.usable(m, NOW, 60) == (False, "partial")
    rep2 = dc.refresh(tmp_path, CONFIG, run, NOW, cap=251)
    assert not rep2["partial"]
    assert ds.usable(ds.load(tmp_path, CONFIG)[0], NOW, 60) == (True, "ok")


def test_mode_off_touches_nothing(tmp_path):
    cfg = {"discovery": {"github": {"repo": REPO}, "dag": {"mode": "off"}}}
    run = _world()
    rep = dc.refresh(tmp_path / ".sdlc", cfg, run, NOW)
    assert rep["status"] == "off" and run.calls == [] and not (tmp_path / ".sdlc").exists()


def test_saved_map_is_numbers_and_codes_only(tmp_path):
    p, _ = _published(tmp_path, _world())
    text = p.read_text()
    assert "fixture issue" not in text and "Blocked by" not in text
    m, why = ds.load(tmp_path, CONFIG)
    assert why == "ok" and m["header"]["repo"] == REPO and m["header"]["last_error"] is None


def test_rebuild_verb_prints_call_counts(tmp_path, monkeypatch, capsys):
    (tmp_path / "config.json").write_text(json.dumps(CONFIG))
    dcm = dag_cli._load("dag_collect")
    monkeypatch.setattr(dag_cli, "_load", lambda name: dcm if name == "dag_collect" else _mod(name))
    real = dcm.refresh
    monkeypatch.setattr(dcm, "refresh", lambda sdlc: real(sdlc, CONFIG, _world(), NOW))
    assert dag_cli.main(["rebuild", "--sdlc", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "saved" in out and "list 5 read 1 pull 1" in out
