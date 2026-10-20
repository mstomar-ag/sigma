"""#1902: waits that no edge reader sees -- a dependency written in a comment, or in loose wording -- are
SHOWN by Chain's plan and sheet and are NEVER an edge.

No network: a counting fake `gh` serves REST GETs only. Controls (each seen red, then restored): the new
reading disabled outright; each loose form's pattern removed on its own; the trust test on the commenter; the
already-an-edge exclusion; the dismissal marker; Sigma's own comment heads; follow-up wording kept advisory;
a comment wait promoted to an edge (the phantom-blocker control); an unread comment making the plan complete.
Call counts are arithmetic and the counting fake's; network time is not measured."""
import importlib.util
import json
import pathlib
import re
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import gqlfake  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
S = ROOT / "skills" / "sigma-loop" / "scripts"
NOW = 1_800_000_000.0


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _cw():
    return _mod("chain_waits")


def _c(body, login="maintainer", assoc="OWNER"):
    return {"body": body, "login": login, "assoc": assoc}


def _issue(n, *labels, body="", comments=(), assignees=(), state="open"):
    return {"number": n, "title": "TITLE %d" % n, "body": body, "state": state,
            "labels": [{"name": x} for x in labels], "assignees": [{"login": a} for a in assignees],
            "user": {"login": "someone"}, "_comments": [c if isinstance(c, dict) else _c(c) for c in comments]}


def _goal(n, *extra, blocked_by=(), body="", **kw):
    lines = ["BODY %d" % n] + ["Blocked by #%d" % b for b in blocked_by] + [body]
    return _issue(n, "sdlc:goal", *extra, body="\n".join(lines), **kw)


def _repo(tmp_path, dag_mode=None):
    base = tmp_path / ".sdlc"
    (base / "state").mkdir(parents=True)
    disc = {"source": "github", "github": {"repo": "acme/widget"}}
    if dag_mode:
        disc["dag"] = {"mode": dag_mode}
    (base / "config.json").write_text(json.dumps({"discovery": disc, "budget": {"max_iterations": 10}}))
    (base / "state" / "STATE.md").write_text("iteration: 0\nrun_iteration: 0\nlast_run: none\n")
    return str(base)


def _fake(issues, calls=None, broken=()):
    """REST GETs only (a non-GET fails the test). `broken`: issue numbers whose comment page is a 500."""
    calls = calls if calls is not None else []
    by = {i["number"]: i for i in issues}

    def pub(i):
        return {k: v for k, v in i.items() if not k.startswith("_")} | {"comments": len(i["_comments"])}

    def run(args):
        args = [str(a) for a in args]
        calls.append(args)
        g = gqlfake.swap(args)
        if g is not None:
            return g
        assert args[0] == "api" and args[args.index("--method") + 1] == "GET", args
        m = re.fullmatch(r"repos/[^/]+/[^/]+/issues/(\d+)(/comments)?", args[1])
        if m:
            n = int(m.group(1))
            if n not in by:
                raise RuntimeError("HTTP 404")
            if m.group(2):
                if n in broken:
                    raise RuntimeError("HTTP 500")
                return json.dumps([{"node_id": "c%d" % k, "body": c["body"], "user": {"login": c["login"]},
                                    "author_association": c["assoc"], "created_at": "2026-01-%02dT00:00:00Z" % (k + 1)}
                                   for k, c in enumerate(by[n]["_comments"])])
            return json.dumps(pub(by[n]))
        params = gqlfake.rest_list_params(args)
        assert params is not None, args
        want = [w for w in params.get("labels", "").split(",") if w]
        hits = [i for i in issues if i["state"] == params.get("state", "open") and
                all(w in [l["name"] for l in i["labels"]] for w in want)]
        per, page = int(params["per_page"]), int(params["page"])
        return json.dumps([pub(i) for i in hits[(page - 1) * per:page * per]])

    run.calls = calls
    return run


def _save_map(base, nodes, edges):
    ds = _mod("dag_store")
    import time
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 60))
    h = {"repo": "acme/widget", "built_at": iso, "refreshed_at": iso, "last_ok_at": iso,
         "last_error": None, "partial": False}
    ok, why = ds.save(pathlib.Path(base), {"header": h, "nodes": nodes, "edges": [
        {"blocker": a, "blocked": b, "source": "body"} for a, b in edges]})
    assert ok, why


def _mnode(n, life="goal", state="open"):
    return {"number": n, "kind": "issue", "state": state, "life": life, "prio": None, "in_corpus": True}


def _plan(tmp_path, via, issues, edges, calls=None, broken=()):
    """Plan goal 1 live or from the map; `edges` are (blocker, blocked) pairs the MAP holds."""
    ch = _mod("chain")
    if via == "live":
        base = _repo(tmp_path, dag_mode="off")
    else:
        base = _repo(tmp_path)
        nums = sorted({i["number"] for i in issues} | {x for e in edges for x in e})
        _save_map(base, [_mnode(n) for n in nums], edges)
    return base, ch.plan(base, 1, run=_fake(issues, calls, broken), now=NOW)


def _chain_of(via):
    # goal 1 waits on 2 (a body edge); issue 5 is outside the chain
    return [(2, 1)] if via == "map" else []


def _items(p, kind="unconfirmed_waits"):
    return {(i["goal"], i["ref"]): i for i in p[kind]}


# --- the loose phrasings, one row each -------------------------------------------------------------------

#: (text, expected [(ref, form)] from `find`). The first six are the phrasings the issue lists.
PHRASINGS = [
    ("#12 (the port work) must land on main before this issue's change", [("12", "land-before")]),
    ("these plug into #12's port", [("12", "plug-into")]),
    ("depends on the rulings recorded in the discussion thread, see #12", [("12", "far")]),
    ("only once #4, #5 and #6 are closed", [("4", "only-once"), ("5", "list"), ("6", "list")]),
    ("blocked on #12 and #13", [("12", "explicit"), ("13", "list")]),
    ("starts only once #12 has landed", [("12", "only-once")]),
    ("**Blocked by:** #12 added", [("12", "explicit")]),
    ("hold on #12 before any paid run", [("12", "hold")]),
    ("#12 is now a blocker", [("12", "is-blocker")]),
    ("we are waiting for #12 and #13 to settle", [("12", "waiting-for"), ("13", "list")]),
    ("depends on #1, #2, and #3", [("1", "explicit"), ("2", "list"), ("3", "list")]),
    ("lands again once the port (#12) has merged", [("12", "once-done")]),
    ("#12 lands on main before this change", [("12", "lands-before")]),
    ("this slice sits behind the port work (#12)", [("12", "behind")]),
    ("What this waits on: #12.", [("12", "waits-on")]),
]

#: sentences that only NAME an issue: no form reads any of them (the phantom-blocker control)
JUST_NAMES = [
    "see #12 for the background", "related to #12", "same area as #12, different cause", "closes #12",
    "fixed in #12", "after Phase 2 (#12) went live we saw it", "tracked in #12", "cc #12 #13",
    "the list is #12, #13 and #14", "only acme/widget#12 matters here", "blocked on acme/other#12",
    "plug into the port, then see #12",
]


@pytest.mark.parametrize("text,expected", PHRASINGS)
def test_each_phrasing_is_read_as_its_form(text, expected):
    assert _cw().find(text) == expected


@pytest.mark.parametrize("text", JUST_NAMES)
def test_a_sentence_that_only_names_an_issue_is_read_as_nothing(text):
    assert _cw().find(text) == []


def test_every_form_has_a_row_in_the_table():
    seen = {f for _t, exp in PHRASINGS for _r, f in exp}
    # the #1940 forms have their own table in test_chain_waits_1940.py
    later = {"slice-chain", "slice-on", "waiting-on", "gates", "reversed-waits-on"}
    assert seen >= set(_cw().FORMS) - {"follow-up"} - later


def test_a_follow_up_form_in_a_comment_is_advisory():
    assert _cw().find("filed as a follow-up to #12") == [("12", "follow-up")]


def test_the_explicit_forms_are_left_out_of_a_body_reading():
    assert _cw().find("blocked on #12 and #13", explicit=False) == [("13", "list")]


def test_a_secret_shaped_ref_list_is_bounded():
    text = "only once " + ", ".join("#%d" % k for k in range(1, 200)) + " are closed"
    assert len(_cw().find(text)) <= _cw().MAX_MORE + 1


# --- the plan: live and from the map ---------------------------------------------------------------------

@pytest.mark.parametrize("via", ["live", "map"])
def test_a_comment_wait_by_the_assignee_is_an_unconfirmed_edge(tmp_path, via):
    issues = [_goal(1, blocked_by=[2], assignees=["pat"], comments=[_c("Blocked by: #5 added", "pat", "NONE")]),
              _goal(2), _goal(5)]
    _base, p = _plan(tmp_path, via, issues, _chain_of(via))
    it = _items(p)[(1, 5)]
    assert (it["by"], it["form"], it["where"], it["in_chain"]) == ("assignee", "explicit", "comment", True)
    assert sorted(m["number"] for m in p["members"]) == [1, 2, 5]
    assert {(e["from"], e["to"]): e["tag"] for e in p["edges"]} == {(2, 1): "explicit", (5, 1): "unconfirmed"}
    assert p["complete"] is True
    assert p["source"] == "live"        # the map holds no comment text, so a plan that finds one reads live
    if via == "map":
        assert "written only in comments or loose wording" in p["source_reason"]


@pytest.mark.parametrize("via", ["live", "map"])
@pytest.mark.parametrize("assoc", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_a_comment_wait_by_an_owner_is_unconfirmed(tmp_path, via, assoc):
    issues = [_goal(1, comments=[_c("hold on #5 before any paid run", "boss", assoc)]), _goal(5)]
    _base, p = _plan(tmp_path, via, issues, [])
    it = _items(p)[(1, 5)]
    assert (it["by"], it["form"]) == (assoc.lower(), "hold")


@pytest.mark.parametrize("via", ["live", "map"])
def test_a_comment_from_a_stranger_is_ignored_and_counted(tmp_path, via):
    issues = [_goal(1, comments=[_c("Blocked by: #5", "stranger", "NONE"),
                                 _c("Blocked by: #5", "visitor", "FIRST_TIME_CONTRIBUTOR")]), _goal(5)]
    _base, p = _plan(tmp_path, via, issues, [])
    assert p["unconfirmed_waits"] == [] and p["waits_read"]["untrusted_ignored"] == 2


@pytest.mark.parametrize("via", ["live", "map"])
def test_a_wait_the_body_already_holds_is_not_listed(tmp_path, via):
    issues = [_goal(1, blocked_by=[2], comments=[_c("Blocked by: #2 again")]), _goal(2)]
    _base, p = _plan(tmp_path, via, issues, _chain_of(via))
    assert p["unconfirmed_waits"] == []


def test_a_wait_the_map_holds_from_another_source_is_not_listed(tmp_path):
    issues = [_goal(1, comments=[_c("Blocked by: #2 again")]), _goal(2)]
    _base, p = _plan(tmp_path, "map", issues, [(2, 1)])        # the body says nothing; the map has the edge
    assert p["unconfirmed_waits"] == []


@pytest.mark.parametrize("via", ["live", "map"])
def test_follow_up_wording_in_a_comment_stays_advisory(tmp_path, via):
    issues = [_goal(1, comments=[_c("filed #6 as a follow-up to #5")]), _goal(5), _goal(6)]
    _base, p = _plan(tmp_path, via, issues, [])
    assert p["unconfirmed_waits"] == []
    assert _items(p, "weak_waits")[(1, 5)]["form"] == "follow-up"


@pytest.mark.parametrize("via", ["live", "map"])
def test_loose_body_wording_is_an_unconfirmed_edge(tmp_path, via):
    issues = [_goal(1, body="these plug into #5's port\nstarts only once #6 has landed"), _goal(5), _goal(6)]
    _base, p = _plan(tmp_path, via, issues, [])
    assert {(i["ref"], i["form"], i["where"]) for i in p["unconfirmed_waits"]} == {
        (5, "plug-into", "body"), (6, "only-once", "body")}
    assert p["weak_waits"] == []
    assert sorted(m["number"] for m in p["members"]) == [1, 5, 6]
    assert {(e["from"], e["to"]): e["tag"] for e in p["edges"]} == {(5, 1): "unconfirmed", (6, 1): "unconfirmed"}


@pytest.mark.parametrize("via", ["live", "map"])
def test_a_dismissed_wait_is_not_listed(tmp_path, via):
    bc = _mod("backlog_check")
    issues = [_goal(1, comments=[_c("Blocked by: #5"), _c(bc.dismiss_comment("blocked-by", 5, "not this goal"))]),
              _goal(5)]
    _base, p = _plan(tmp_path, via, issues, [])
    assert p["unconfirmed_waits"] == []


@pytest.mark.parametrize("via", ["live", "map"])
def test_a_dismissal_from_a_stranger_does_not_count(tmp_path, via):
    bc = _mod("backlog_check")
    issues = [_goal(1, comments=[_c("Blocked by: #5"),
                                 _c(bc.dismiss_comment("blocked-by", 5), "stranger", "NONE")]), _goal(5)]
    _base, p = _plan(tmp_path, via, issues, [])
    assert (1, 5) in _items(p)


@pytest.mark.parametrize("via", ["live", "map"])
def test_sigmas_own_comments_are_not_read_as_waits(tmp_path, via):
    park = "Parked by Sigma — needs human review: should we keep the column? #5 was mentioned"
    issues = [_goal(1, comments=[_c("Sigma is arming this issue as part of the chain for #5."),
                                 _c("Auto-unparked by Sigma — blocker(s) #5 closed; hold on #5"),
                                 _c("Dependency marker added to the body. hold on #5"),
                                 _c(park), _c("note <!-- sigma:keep-parked --> hold on #5")]), _goal(5)]
    _base, p = _plan(tmp_path, via, issues, [])
    assert p["unconfirmed_waits"] == [] and p["weak_waits"] == []


@pytest.mark.parametrize("via", ["live", "map"])
def test_the_park_prefix_is_stripped_but_the_reason_is_read(tmp_path, via):
    park = "Parked by Sigma — needs human review: hold on #5 until the ruling lands"
    _base, p = _plan(tmp_path, via, [_goal(1, comments=[_c(park)]), _goal(5)], [])
    assert (1, 5) in _items(p)


@pytest.mark.parametrize("via", ["live", "map"])
def test_a_member_other_than_the_end_goal_is_read_too(tmp_path, via):
    issues = [_goal(1, blocked_by=[2]), _goal(2, comments=[_c("#5 is now a blocker")]), _goal(5)]
    _base, p = _plan(tmp_path, via, issues, _chain_of(via))
    assert _items(p)[(2, 5)]["form"] == "is-blocker"


@pytest.mark.parametrize("via", ["live", "map"])
def test_a_wait_is_labelled_with_the_state_of_what_it_names(tmp_path, via):
    issues = [_goal(1, blocked_by=[2], comments=[_c("hold on #3 and #5 and #6")]), _goal(2),
              _issue(3, "sdlc:goal", state="closed"), _goal(5)]
    _base, p = _plan(tmp_path, via, issues, _chain_of(via))
    states = {k[1]: v["state"] for k, v in _items(p).items()}
    assert states[3] == "closed" and states[5] == "open"       # followed by the plan, so the plan knows what they are
    assert states[6] == "unknown"                  # no such issue: said so, not guessed
    assert p["complete"] is False                  # and the plan says a wait could not be read


def test_a_closed_node_in_the_map_is_labelled_closed(tmp_path):
    ch = _mod("chain")
    base = _repo(tmp_path)
    _save_map(base, [_mnode(1), _mnode(3, state="closed")], [])
    p = ch.plan(base, 1, run=_fake([_goal(1, comments=[_c("hold on #3")]), _goal(3, state="closed")]), now=NOW)
    assert _items(p)[(1, 3)]["state"] == "closed" and p["done"] == [3]


def test_an_unreadable_comment_page_makes_the_plan_incomplete(tmp_path):
    issues = [_goal(1, comments=[_c("hold on #5")]), _goal(5)]
    _base, p = _plan(tmp_path, "live", issues, [], broken=(1,))
    assert p["waits_read"]["not_read"] == [1] and p["complete"] is False


def test_an_unreadable_issue_from_the_map_makes_the_plan_incomplete(tmp_path):
    ch = _mod("chain")
    base = _repo(tmp_path)
    _save_map(base, [_mnode(1)], [])                      # the map knows #1; the fake has no such issue
    p = ch.plan(base, 1, run=_fake([]), now=NOW)
    assert p["source"] == "map" and p["complete"] is False
    assert "comments NOT read for #1" in ch.render(dict(p, record="r"))


def test_members_past_the_read_bound_are_named_and_the_plan_is_incomplete(tmp_path, monkeypatch):
    cw = _cw()
    nums = list(range(1, 6))
    issues = [_goal(1, blocked_by=[2, 3, 4, 5])] + [_goal(n) for n in nums[1:]]
    ch = _mod("chain")
    monkeypatch.setattr(ch.chain_waits, "MAX_READS", 3)
    base = _repo(tmp_path, dag_mode="off")
    p = ch.plan(base, 1, run=_fake(issues), now=NOW)
    assert p["waits_read"]["not_read"] == [4, 5] and p["complete"] is False
    assert cw.MAX_READS == 100


def test_findings_past_the_cap_are_counted_and_the_plan_is_incomplete(tmp_path, monkeypatch):
    ch = _mod("chain")
    monkeypatch.setattr(ch.chain_waits, "MAX_ITEMS", 2)
    issues = [_goal(1, comments=[_c("hold on #5, #6 and #7")]), _goal(5), _goal(6), _goal(7)]
    base = _repo(tmp_path, dag_mode="off")
    p = ch.plan(base, 1, run=_fake(issues), now=NOW)
    assert len(p["unconfirmed_waits"]) == 2 and p["waits_read"]["dropped"] == 1 and p["complete"] is False


def test_only_the_newest_comments_are_read_and_it_is_said(tmp_path, monkeypatch):
    ch = _mod("chain")
    monkeypatch.setattr(ch.chain_waits, "COMMENT_LIMIT", 2)
    issues = [_goal(1, comments=[_c("hold on #5"), _c("chatter"), _c("more chatter")]), _goal(5)]
    base = _repo(tmp_path, dag_mode="off")
    p = ch.plan(base, 1, run=_fake(issues), now=NOW)
    assert p["unconfirmed_waits"] == [] and p["waits_read"]["maybe_older_comments"] == [1]
    assert "only the newest 2 comments were read for #1" in ch.render(dict(p, record="r"))


def test_a_long_comment_is_read_to_the_bound_only(tmp_path):
    long = "x " * 1500 + "hold on #5"
    _base, p = _plan(tmp_path, "live", [_goal(1, comments=[_c(long)]), _goal(5)], [])
    assert p["unconfirmed_waits"] == []


def test_a_known_empty_issue_costs_no_extra_request(tmp_path):
    calls = []
    _plan(tmp_path, "live", [_goal(1), _goal(5)], [], calls)
    assert len(calls) == 1 + 1 and not any(a[1].endswith("/comments") for a in calls)    # the goal GET and the list


def test_calls_for_a_member_with_comments_are_bounded(tmp_path):
    calls = []
    _plan(tmp_path, "live", [_goal(1, comments=[_c("hello")])], [], calls)
    per_issue = [a[1] for a in calls if re.search(r"issues/1(/comments)?$", a[1])]
    assert len(per_issue) <= 1 + 3          # the closure's GET, then at most the issue and two comment pages


# --- the record, the printout and the sheet --------------------------------------------------------------

def _two_waits(tmp_path, via="live"):
    issues = [_goal(1, blocked_by=[2], comments=[_c("SECRET-COMMENT Blocked by: #5")]),
              _goal(2, comments=[_c("hold on #6 first")], body="only once #7 has landed"),
              _goal(5), _goal(6), _goal(7)]
    return _plan(tmp_path, via, issues, _chain_of(via))


@pytest.mark.parametrize("via", ["live", "map"])
def test_the_record_holds_numbers_and_codes_only(tmp_path, via):
    base, p = _two_waits(tmp_path, via)
    text = (pathlib.Path(base) / "state" / "chains" / "c1.json").read_text()
    assert "SECRET" not in text and "hold on" not in text
    doc = json.loads(text)
    assert doc["unconfirmed_waits"] == p["unconfirmed_waits"] and len(doc["unconfirmed_waits"]) == 3
    assert doc["weak_waits"] == [] and {i["form"] for i in doc["unconfirmed_waits"]} == {"explicit", "hold", "only-once"}


def test_the_plan_printout_lists_them(tmp_path):
    ch = _mod("chain")
    _base, p = _two_waits(tmp_path)
    out = ch.render(dict(p, record="r"))
    assert "unconfirmed waits (read as edges for this chain, not confirmed): 3" in out
    assert "#1 waits on #5 (explicit; in a comment by the owner; open, in the chain)" in out
    assert "#2 waits on #7 (only-once; in the body; open, in the chain)" in out
    assert "weak mentions (follow-up wording; never an edge): 0" in out


def test_the_sheet_has_one_block_for_all_unconfirmed_waits(tmp_path):
    ch = _mod("chain")
    base, _p = _two_waits(tmp_path)
    text = ch.sheet(base, 1, now=NOW)
    assert text.count("Unconfirmed waits (3)") == 1
    block = text.split("Unconfirmed waits (3)")[1].split("Decisions (")[0]
    assert "#1 waits on #5" in block and "#2 waits on #6" in block and "#2 waits on #7" in block
    assert "deps.py mark <sdlc_dir> <goal> <ref>" in block and "dismiss-text blocked-by <ref>" in block
    assert text.index("Unconfirmed waits") < text.index("Decisions (")
    # each wait on an OPEN issue in the chain is a question of its own under one heading (confirm or dismiss)
    decisions = text.split("Decisions (")[1].split("Writes that")[0]
    assert all("[wait:%s]" % w in decisions for w in ("1-5", "2-6", "2-7"))
    assert decisions.count("Unconfirmed waits (3, read from comments or loose wording)") == 1
    assert "Nothing has been changed yet." in text


def test_the_documented_gesture_prints_the_waits(tmp_path, capsys):
    """The gesture the docs give: `chain.py plan <sdlc_dir> <N>` then `chain.py sheet <sdlc_dir> <N>`."""
    ch = _mod("chain")
    base = _repo(tmp_path, dag_mode="off")
    run = _fake([_goal(1, comments=[_c("Blocked by: #5 added")]), _goal(5)])
    assert ch.main(["chain.py", "plan", base, "1"], run=run, now=NOW) == 0
    assert "#1 waits on #5" in capsys.readouterr().out
    assert ch.main(["chain.py", "sheet", base, "1"], run=run, now=NOW) == 0
    out = capsys.readouterr().out
    assert "Unconfirmed waits (1)" in out and "deps.py mark" in out


def test_the_sheet_has_no_block_when_there_is_nothing(tmp_path):
    ch = _mod("chain")
    base, _p = _plan(tmp_path, "live", [_goal(1), _goal(5)], [])
    assert "Unconfirmed waits" not in ch.sheet(base, 1, now=NOW)


def test_the_sheet_says_what_was_not_read_even_with_no_wait(tmp_path):
    ch = _mod("chain")
    base, _p = _plan(tmp_path, "live", [_goal(1, comments=[_c("hi")])], [], broken=(1,))
    text = ch.sheet(base, 1, now=NOW)
    assert "comments NOT read for #1" in text and "INCOMPLETE" in text


def test_a_record_without_the_new_fields_still_renders(tmp_path):
    ch = _mod("chain")
    base, _p = _plan(tmp_path, "live", [_goal(1)], [])
    path = pathlib.Path(base) / "state" / "chains" / "c1.json"
    doc = json.loads(path.read_text())
    for k in ("unconfirmed_waits", "weak_waits", "waits_read"):
        doc.pop(k)
    path.write_text(json.dumps(doc))
    assert "Unconfirmed waits" not in ch.sheet(base, 1, now=NOW)


def test_planning_writes_nothing_to_github(tmp_path):
    calls = []
    _two_waits_calls = [_goal(1, comments=[_c("Blocked by: #5")]), _goal(5)]
    _plan(tmp_path, "live", _two_waits_calls, [], calls)
    assert calls and all(a[0] == "api" and a[a.index("--method") + 1] == "GET" for a in calls)


# --- deps.py report: the loose phrasings read as weak ----------------------------------------------------

def test_deps_report_shows_loose_wording_as_weak_not_a_mention():
    deps = _mod("deps")
    refs = {r["ref"]: r["read_as"] for r in deps.analyse(
        "1", "t", "starts only once #5 has landed\nsee #6", ["hold on #7", "Blocked by: #8"])}
    assert refs == {"5": "weak", "6": "mention", "7": "weak", "8": "comment-edge"}


# --- the docs --------------------------------------------------------------------------------------------

def test_the_docs_describe_the_unconfirmed_waits():
    flat = " ".join((ROOT / p).read_text() for p in
                    ("docs/chain.md", "docs/dependency-map-and-chain.md", "skills/sigma-chain/SKILL.md"))
    for needle in ("unconfirmed wait", "weak mention", "deps.py mark", "dismiss-text", "chain_waits"):
        assert needle in flat, needle


# --- the chain follows the waits it reads (integration, #1908) ---------------------------------------------

def _deep_chain():
    """1 waits on 2 in prose, 2 on 3 in a trusted comment, 3 on 4 in prose: three levels the closure cannot see."""
    return [_goal(1, body="starts only once #2 has landed"),
            _goal(2, comments=[_c("hold on #3 before any paid run")]),
            _goal(3, body="#4 must land on main before this change"), _goal(4)]


def test_the_closure_follows_waits_level_by_level(tmp_path):
    _base, p = _plan(tmp_path, "live", _deep_chain(), [])
    assert sorted(m["number"] for m in p["members"]) == [1, 2, 3, 4]
    assert {(e["from"], e["to"]): e["tag"] for e in p["edges"]} == {
        (2, 1): "unconfirmed", (3, 2): "unconfirmed", (4, 3): "unconfirmed"}
    assert [m["number"] for m in p["members"]] == [4, 3, 2, 1] and p["complete"] is True


def test_a_wait_chain_deeper_than_the_rounds_makes_the_plan_incomplete(tmp_path, monkeypatch):
    ch = _mod("chain")
    monkeypatch.setattr(ch, "MAX_WAIT_ROUNDS", 1)
    base = _repo(tmp_path, dag_mode="off")
    p = ch.plan(base, 1, run=_fake(_deep_chain()), now=NOW)
    assert p["complete"] is False and any("go deeper than 1 levels" in s for s in p["stops"]), p["stops"]


def test_a_cycle_made_only_by_prose_is_refused_with_the_way_out(tmp_path):
    ch = _mod("chain")
    base = _repo(tmp_path, dag_mode="off")
    issues = [_goal(1, body="starts only once #2 has landed"), _goal(2, body="starts only once #1 has landed")]
    with pytest.raises(ch.ChainRefused) as exc:
        ch.plan(base, 1, run=_fake(issues), now=NOW)
    text = str(exc.value)
    assert "dependency cycle among #1, #2" in text and "read only from a comment or loose wording" in text
    assert "dismiss-text blocked-by" in text


def test_a_sentence_that_only_names_an_issue_adds_no_member_and_no_edge(tmp_path):
    """The phantom-blocker control, end to end: nothing JUST_NAMES lists may pull an issue into the chain."""
    body = "\n".join(JUST_NAMES)
    issues = [_goal(1, body=body, comments=[_c(t) for t in JUST_NAMES])] + [_goal(n) for n in range(2, 20)]
    _base, p = _plan(tmp_path, "live", issues, [])
    assert [m["number"] for m in p["members"]] == [1] and p["edges"] == [] and p["unconfirmed_waits"] == []


def test_a_dismissed_wait_is_not_followed(tmp_path):
    bc = _mod("backlog_check")
    issues = [_goal(1, body="starts only once #5 has landed",
                    comments=[_c(bc.dismiss_comment("blocked-by", 5, "not this goal"))]), _goal(5)]
    _base, p = _plan(tmp_path, "live", issues, [])
    assert [m["number"] for m in p["members"]] == [1] and p["unconfirmed_waits"] == []


def test_a_wait_on_a_closed_issue_is_a_done_prerequisite_not_a_member(tmp_path):
    issues = [_goal(1, body="starts only once #5 has landed"), _issue(5, "sdlc:goal", state="closed")]
    _base, p = _plan(tmp_path, "live", issues, [])
    assert [m["number"] for m in p["members"]] == [1] and p["done"] == [5]


def test_the_map_attempt_and_the_live_fallback_read_each_comment_page_once(tmp_path):
    calls = []
    issues = [_goal(1, blocked_by=[2], comments=[_c("hold on #5 first")]), _goal(2), _goal(5)]
    _plan(tmp_path, "map", issues, [(2, 1)], calls)
    pages = [a[1] for a in calls if a[1].endswith("issues/1/comments")]
    assert len(pages) == 1, pages
