"""#1948: an issue that looks like an epic but carries no upper-tier label is not armed.

Pure shape predicate, the apply refusal, the migration skip, and the doctor row. In-memory fakes, no network."""
import importlib.util
import json
import pathlib

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"
spec = importlib.util.spec_from_file_location("promote_1948", S / "promote.py")
promote = importlib.util.module_from_spec(spec)
spec.loader.exec_module(promote)
PROPOSED, GOAL, PARKED = "sdlc:needs-confirmation", "sdlc:goal", "sdlc:parked"

SLICES = "Intro text.\n\n## Slices\n\n- [ ] #11 first piece\n- [ ] #12 second piece\n"


def test_title_forms_are_epic_shaped():
    for title in ("EPIC (P0): rollout work", "Epic: rollout work", "[EPIC] rollout work", "  epic - rollout"):
        assert promote.epic_shaped(title, ""), title


def test_body_slice_list_is_epic_shaped():
    assert promote.epic_shaped("Rollout work", SLICES)
    for head in ("Children", "Child issues", "Sub-issues"):
        assert promote.epic_shaped("Rollout work", "## %s\n* #3\n* #4\n" % head), head
    assert promote.epic_shaped("Rollout work", "### Slices\n1. #3 one\n2. #4 two\n")


def test_ordinary_goals_are_not_epic_shaped():
    assert promote.epic_shaped("Fix the epic-sized typo in the docs", "") is None
    assert promote.epic_shaped("Rollout work", "## Slices\n\n- [ ] #11 only one\n") is None
    assert promote.epic_shaped("Rollout work", "## Slices\n\nTBD, nothing yet.\n") is None
    assert promote.epic_shaped("Rollout work", "## Notes\n\n- #11\n- #12\n") is None
    assert promote.epic_shaped("Epicenter cleanup", "") is None
    assert promote.epic_shaped("", "") is None


class FakeSource:
    goal_label, proposed_label, parked_label = GOAL, PROPOSED, PARKED
    goal_blocked_label = "sdlc:blocked"
    needs_label_label = "sdlc:needs-label"
    repo = "acme/widget"
    project_enabled = False
    board_active = False
    col = {"ready": "Ready", "backlog": "Backlog"}

    def __init__(self):
        self.store, self.swaps = {}, []

    def add(self, number, *labels, title="t", body=""):
        self.store[number] = {"title": title, "labels": list(labels), "body": body}

    def overlay_labels(self):
        return ()

    def _repo_args(self):
        return []

    def _run(self, args):
        return "[]"

    def _read_issue(self, number, fields, comment_limit=None):
        i = self.store[int(str(number))]
        return {"labels": [{"name": l} for l in i["labels"]], "state": "OPEN", "stateReason": None,
                "author": {"login": "dana"}, "body": i["body"], "title": i["title"], "comments": []}

    def _swap_labels(self, number, add=(), remove=(), **kw):
        self.swaps.append((int(str(number)), tuple(add), tuple(remove)))
        return True

    def _issue_comment(self, number, body):
        pass

    def _set_board_status(self, number, column):
        return True


def _apply(src, n=7):
    (r,) = promote.promote(".sdlc", {}, [n], source=src)["results"]
    return r


def test_apply_refuses_unlabelled_epic_by_title_and_names_the_route():
    src = FakeSource()
    src.add(7, PROPOSED, title="EPIC (P0): rollout work")
    r = _apply(src)
    assert r["outcome"] == "skipped" and "epic" in r["detail"] and "label" in r["detail"]
    assert src.swaps == []


def test_apply_refuses_unlabelled_epic_by_slice_list():
    src = FakeSource()
    src.add(7, PROPOSED, title="Rollout work", body=SLICES)
    r = _apply(src)
    assert r["outcome"] == "skipped" and src.swaps == []


def test_labelled_epic_keeps_the_upper_tier_message():
    src = FakeSource()
    src.add(7, PROPOSED, "epic", title="EPIC: rollout work", body=SLICES)
    r = _apply(src)
    assert r["outcome"] == "skipped" and "upper tier" in r["detail"] and src.swaps == []


def test_ordinary_goal_still_arms():
    src = FakeSource()
    src.add(7, PROPOSED, title="Tidy the docs", body="cleanup of old text")
    r = _apply(src)
    assert r["outcome"] == "promoted" and src.swaps and src.swaps[0][1] == (GOAL,)


def test_migration_skips_unlabelled_epic_and_never_arms(tmp_path):
    src = FakeSource()
    src.add(1, PROPOSED, title="EPIC: rollout work")
    src.store[1]["body"] = SLICES
    src.store[2] = dict(src.store[1], title="Tidy the docs", body="cleanup")
    src.store[2]["labels"] = [PROPOSED]
    src.store[1]["labels"] = [PROPOSED]

    def _run(*extra):
        return promote.main(["promote.py", "migrate-confirmation", str(tmp_path), *extra], source=_Listing(src))
    _run("--apply")        # exit 1 is expected: the unarmed epic keeps its label, so the run is incomplete
    assert all(s[0] != 1 or GOAL not in s[1] for s in src.swaps)
    assert any(s[0] == 2 and GOAL in s[1] for s in src.swaps)


class _Listing:
    """Wraps FakeSource with the list read the migration makes."""
    def __init__(self, src):
        self._s = src

    def __getattr__(self, name):
        return getattr(self._s, name)

    def _run(self, args):
        kv = {args[i + 1].split("=", 1)[0]: args[i + 1].split("=", 1)[1]
              for i, a in enumerate(args) if a == "-f"}
        rows = [{"number": n, "title": i["title"], "state": "open", "body": i["body"],
                 "labels": [{"name": l} for l in i["labels"]]} for n, i in sorted(self._s.store.items())
                if PROPOSED in i["labels"]]
        return json.dumps(rows if kv.get("page") == "1" else [])


# --- doctor row ---------------------------------------------------------------------------------
def _doc():
    p = S.parent.parent / "sigma-doctor" / "scripts" / "doctor.py"
    sp = importlib.util.spec_from_file_location("doctor_1948", p)
    d = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(d)
    return d, p


def _shaped(rows):
    from test_unpark import _runner, _config
    d, _ = _doc()
    run = _runner(by_label={None: json.dumps(rows)})
    return d._epic_shaped_issues(_config(), {"repo": "acme/widget"}, lambda argv: run(argv[1:]))


def _row(n, title="Plain goal", body="", labels=()):
    return {"number": n, "title": title, "body": body, "labels": [{"name": l} for l in labels]}


def test_doctor_lists_only_unlabelled_epic_shaped_issues():
    rows = [_row(5, "EPIC: x"), _row(6, "Plain goal"), _row(7, "EPIC: y", labels=["epic"]),
            _row(8, "Rollout", SLICES)]
    nums, partial = _shaped(rows)
    assert nums == [5, 8] and partial is False


def test_doctor_reports_nothing_when_none():
    assert _shaped([_row(1), _row(2)]) == ([], False)


def test_doctor_row_is_wired_warn_only_and_bounded():
    d, p = _doc()
    src = p.read_text()
    i = src.index('"no open issue looks like an epic without its upper-tier label"')
    row = src[i - 400:i + 900]
    assert "es = _epic_shaped_issues(cfg, gh_disc, run)" in src and "if es is not None and es[0]:" in src and "[:10]" in row and "more" in row and "False" in row


def test_label_drain_verdict_calls_an_unlabelled_epic_upper_tier(tmp_path):
    import test_label_drain_plan as tp
    issues = [tp._goal(1, body=SLICES), tp._goal(2)]
    p, calls = tp._plan(tmp_path, issues)
    assert tp._by(p, 1)["verdict"] == "upper-tier" and "looks like an epic" in tp._by(p, 1)["detail"]
    assert tp._by(p, 2)["verdict"] == "armed"


def test_a_hyphenated_epic_word_is_not_an_epic_title():
    assert promote.epic_shaped("Epic-sized typo fix", "") is None
