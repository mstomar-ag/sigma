"""#1010 (decision rubric, slice 20): `promote.py migrate-confirmation`.

Everything runs against an in-memory fake source (no network, no `gh`): a REST-shaped list endpoint with real
paging, a live per-issue read, an atomic label swap, and a recording of every write."""
import importlib.util
import pathlib

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


promote = _mod("promote")
PROPOSED, GOAL, PARKED = "sdlc:needs-confirmation", "sdlc:goal", "sdlc:parked"


class FakeSource:
    goal_label, proposed_label, parked_label = GOAL, PROPOSED, PARKED
    repo = "acme/widget"
    project_enabled = False
    col = {"ready": "Ready", "backlog": "Backlog"}

    def __init__(self, stale_list=False):
        self.store, self.writes, self.swaps, self.comments, self.pages = {}, [], [], [], []
        self.unreadable, self.stale_list, self._snapshot = set(), stale_list, None

    def add(self, number, *labels, title="t", body="", state="open", comments=(), author="dana"):
        self.store[number] = {"number": number, "title": title, "labels": list(labels), "body": body,
                              "state": state, "comments": list(comments), "author": author}

    def overlay_labels(self):
        return ()

    def _repo_args(self):
        return []

    def _run(self, args):
        import json
        kv = {args[i + 1].split("=", 1)[0]: args[i + 1].split("=", 1)[1]
              for i, a in enumerate(args) if a == "-f"}
        self.pages.append(int(kv["page"]))
        if self.stale_list and self._snapshot is None:
            self._snapshot = {n: dict(i) for n, i in self.store.items()}
        data = self._snapshot if self.stale_list else self.store
        want = [l for l in kv.get("labels", "").split(",") if l]
        rows = [i for _, i in sorted(data.items()) if all(l in i["labels"] for l in want)
                and (kv["state"] == "all" or i["state"] == kv["state"])]
        per, page = int(kv["per_page"]), int(kv["page"])
        return json.dumps([{"number": i["number"], "title": i["title"], "state": i["state"], "body": i["body"],
                            "labels": [{"name": l} for l in i["labels"]]}
                           for i in rows[(page - 1) * per: page * per]])

    def _read_issue(self, number, fields, comment_limit=None):
        n = int(str(number))
        if n in self.unreadable:
            raise RuntimeError("simulated read failure")
        i = self.store[n]
        return {"labels": [{"name": l} for l in i["labels"]], "state": i["state"].upper(),
                "stateReason": "COMPLETED" if i["state"] == "closed" else None,
                "author": {"login": i["author"]}, "body": i["body"],
                "comments": [{"body": c} for c in i["comments"]]}

    def _swap_labels(self, number, add=(), remove=(), **kw):
        n = int(str(number))
        self.writes.append(("swap", n))
        self.swaps.append((n, tuple(add), tuple(remove)))
        labels = self.store[n]["labels"]
        for l in add:
            if l not in labels:
                labels.append(l)
        self.store[n]["labels"] = [l for l in labels if l not in remove]
        return True

    def _issue_comment(self, number, body):
        self.writes.append(("comment", int(str(number))))
        self.comments.append((int(str(number)), body))
        self.store[int(str(number))]["comments"].append(body)

    def _set_board_status(self, number, column):
        self.writes.append(("board", int(str(number))))
        return True


def _seed(src):
    src.add(1, PROPOSED, "sdlc:followup", "priority:P2", title="tidy the docs", body="cleanup of old text")
    src.add(2, PROPOSED, "priority:P1", title="human idea", body="please add a flag")
    src.add(3, PROPOSED, "priority:P1", title="gated", comments=[promote_markers()[0] + " held"])
    return src


def promote_markers():
    return promote._OWNER_MARKER, promote._SCOPE_MARKER


CFG = {}


def _run(src, apply=False, tmp="."):
    return promote.migrate(src, CFG, apply=apply, sdlc_dir=str(tmp))


def test_dry_run_changes_nothing(tmp_path):
    src = _seed(FakeSource())
    rep = _run(src, apply=False, tmp=tmp_path)
    assert src.writes == []
    assert rep["total"] == 3 and rep["covered"] == 3 and not rep["apply"]
    assert sorted(i["outcome"] for i in rep["items"]) == ["would-arm", "would-arm", "would-park"]
    assert all(PROPOSED in l["labels"] for l in src.store.values())


def test_apply_comments_which_and_why(tmp_path):
    src = _seed(FakeSource())
    _run(src, apply=True, tmp=tmp_path)
    assert sorted(n for n, _ in src.comments) == [1, 2, 3]
    by = dict(src.comments)
    assert promote.MIGRATE_MARKER in by[1] and "armed" in by[1] and "followup" in by[1]
    assert "armed" in by[2] and "P1" in by[2] and "human" in by[2]
    assert "parked" in by[3] and "owner_hold" in by[3] and "ownership" in by[3]
    assert GOAL in src.store[1]["labels"] and GOAL in src.store[2]["labels"]
    assert PARKED in src.store[3]["labels"]


def test_swap_never_leaves_a_pair(tmp_path):
    src = _seed(FakeSource())
    _run(src, apply=True, tmp=tmp_path)
    assert len(src.swaps) == 3                      # exactly one mutation per issue
    for n, add, remove in src.swaps:
        assert PROPOSED in remove and (GOAL in add or PARKED in add)   # removal rides with the addition
    for i in src.store.values():
        assert PROPOSED not in i["labels"]
        assert not (GOAL in i["labels"] and PARKED in i["labels"])


def test_second_run_is_a_noop(tmp_path):
    src = _seed(FakeSource())
    _run(src, apply=True, tmp=tmp_path)
    before = (len(src.writes), len(src.swaps), len(src.comments))
    again = _run(src, apply=True, tmp=tmp_path)
    assert again["total"] == 0 and again["covered"] == 0
    assert (len(src.writes), len(src.swaps), len(src.comments)) == before


def test_second_run_with_a_stale_list_is_a_noop(tmp_path):
    src = _seed(FakeSource(stale_list=True))
    _run(src, apply=True, tmp=tmp_path)
    before = len(src.writes)
    again = _run(src, apply=True, tmp=tmp_path)     # the list still shows all three
    assert again["total"] == 3 and again["covered"] == 3
    assert {i["outcome"] for i in again["items"]} == {"noop"}
    assert len(src.writes) == before


def test_open_blocker_issue_is_parked_not_armed(tmp_path):
    src = FakeSource()
    src.add(50, "sdlc:goal")                        # open blocker
    src.add(51, PROPOSED, "priority:P1", body="Blocked by: #50")
    src.add(52, PROPOSED, "priority:P1", body="Blocked by: #53")
    src.add(53, state="closed")
    rep = _run(src, apply=True, tmp=tmp_path)
    out = {i["number"]: i for i in rep["items"]}
    assert out["51"]["outcome"] == "parked" and PARKED in src.store[51]["labels"]
    assert GOAL not in src.store[51]["labels"] and "dependency" in dict(src.comments)[51]
    assert out["52"]["outcome"] == "armed" and GOAL in src.store[52]["labels"]


def test_closed_issues_are_untouched(tmp_path):
    src = FakeSource()
    src.add(7, PROPOSED, "priority:P1", state="closed")
    rep = _run(src, apply=True, tmp=tmp_path)
    assert src.writes == [] and src.store[7]["labels"] == [PROPOSED, "priority:P1"]
    assert rep["items"][0]["outcome"] == "closed" and rep["covered"] == 1 and rep["total"] == 1


def test_migration_pages_past_the_fetch_cap(tmp_path):
    src = FakeSource()
    for n in range(1, 251):
        src.add(n, PROPOSED, "priority:P1", title="t%d" % n)
    rep = _run(src, apply=False, tmp=tmp_path)
    assert rep["total"] == 250 and rep["covered"] == 250 and len(rep["items"]) == 250
    assert max(src.pages) >= 3 and rep["complete"]


def test_report_says_covered_n_of_m(tmp_path, capsys):
    src = FakeSource()
    for n in range(1, 251):
        src.add(n, PROPOSED, "priority:P1")
    rc = promote.main(["promote.py", "migrate-confirmation", str(tmp_path)], source=src)
    assert rc == 0 and "covered 250 of 250" in capsys.readouterr().out
    src.unreadable.add(9)
    rc = promote.main(["promote.py", "migrate-confirmation", str(tmp_path)], source=src)
    out = capsys.readouterr().out
    assert rc == 1 and "covered 249 of 250" in out


def test_default_flag_is_dry_run_and_apply_is_explicit(tmp_path, capsys):
    src = _seed(FakeSource())
    assert promote.main(["promote.py", "migrate-confirmation", str(tmp_path)], source=src) == 0
    assert src.writes == [] and "DRY-RUN" in capsys.readouterr().out
    assert promote.main(["promote.py", "migrate-confirmation", str(tmp_path), "--apply", "--dry-run"],
                        source=src) == 2 and src.writes == []
    assert promote.main(["promote.py", "migrate-confirmation", str(tmp_path), "--apply"], source=src) == 0
    assert src.writes


def test_triage_off_refuses_without_any_call(tmp_path):
    src = _seed(FakeSource())
    rep = promote.migrate(src, {"ai_filed": {"triage": {"enabled": False}}}, apply=True, sdlc_dir=str(tmp_path))
    assert rep["refused"] and src.writes == [] and src.pages == []


def test_markers_match_their_modules():
    assert promote._OWNER_MARKER == _mod("feature_owner").OWNER_MARKER
    assert promote._SCOPE_MARKER == _mod("feature_propagate").SCOPE_MARKER
    assert promote._FOLLOWUP_LABEL == _mod("handoff").FOLLOWUP_LABEL


def test_classify_population():
    c = promote.classify_population
    assert c({"labels": [], "comments": [promote._OWNER_MARKER]}) == "ownership_hold"
    assert c({"labels": [], "comments": [promote._SCOPE_MARKER]}) == "scope_hold"
    assert c({"labels": ["sdlc:followup"], "comments": []}) == "followup"
    assert c({"labels": ["x"], "comments": []}) == "human"


def test_plan_for_human_without_priority_parks_and_followup_clamps():
    p = promote.plan_for({"title": "x", "body": "", "labels": [], "comments": []}, CFG)
    assert isinstance(p, promote.Park)
    f = promote.plan_for({"title": "x", "body": "", "labels": ["sdlc:followup", "priority:P0"], "comments": []}, CFG)
    assert isinstance(f, promote.Arm) and f.priority in ("P2", "P3", "P4")
