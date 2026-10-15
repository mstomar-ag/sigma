"""#1517: the confirmation migration never arms an upper-tier item; `promote.py apply` refuses one.

In-memory fake source, no network: the real verb is driven through `promote.main`."""
import importlib.util
import pathlib

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"
spec = importlib.util.spec_from_file_location("promote", S / "promote.py")
promote = importlib.util.module_from_spec(spec)
spec.loader.exec_module(promote)
PROPOSED, GOAL, PARKED = "sdlc:needs-confirmation", "sdlc:goal", "sdlc:parked"
REASON = "upper tier: label removed, not armed"


class FakeSource:
    goal_label, proposed_label, parked_label = GOAL, PROPOSED, PARKED
    goal_blocked_label = "sdlc:blocked"
    needs_label_label = "sdlc:needs-label"
    repo = "acme/widget"
    project_enabled = False
    board_active = False
    col = {"ready": "Ready", "backlog": "Backlog"}

    def __init__(self):
        self.store, self.swaps, self.comments = {}, [], []

    def add(self, number, *labels, title="t", body="", state="open"):
        self.store[number] = {"number": number, "title": title, "labels": list(labels), "body": body,
                              "state": state}

    def overlay_labels(self):
        return ()

    def _repo_args(self):
        return []

    def _run(self, args):
        import json
        kv = {args[i + 1].split("=", 1)[0]: args[i + 1].split("=", 1)[1]
              for i, a in enumerate(args) if a == "-f"}
        want = [l for l in kv.get("labels", "").split(",") if l]
        rows = [i for _, i in sorted(self.store.items()) if all(l in i["labels"] for l in want)
                and (kv["state"] == "all" or i["state"] == kv["state"])]
        per, page = int(kv["per_page"]), int(kv["page"])
        return json.dumps([{"number": i["number"], "title": i["title"], "state": i["state"], "body": i["body"],
                            "labels": [{"name": l} for l in i["labels"]]}
                           for i in rows[(page - 1) * per: page * per]])

    def _read_issue(self, number, fields, comment_limit=None):
        i = self.store[int(str(number))]
        return {"labels": [{"name": l} for l in i["labels"]], "state": i["state"].upper(),
                "stateReason": None, "author": {"login": "dana"}, "body": i["body"], "comments": []}

    def _swap_labels(self, number, add=(), remove=(), **kw):
        n = int(str(number))
        self.swaps.append((n, tuple(add), tuple(remove)))
        labels = self.store[n]["labels"]
        labels.extend(l for l in add if l not in labels)
        self.store[n]["labels"] = [l for l in labels if l not in remove]
        return True

    def _issue_comment(self, number, body):
        self.comments.append((int(str(number)), body))

    def _set_board_status(self, number, column):
        return True

    def _state(self, number):
        return self._read_issue(number, [])


def _run(tmp_path, src, *extra):
    return promote.main(["promote.py", "migrate-confirmation", str(tmp_path), *extra], source=src)


def test_epic_dry_then_apply_removes_label_only(tmp_path, capsys):
    src = FakeSource()
    src.add(1, PROPOSED, "epic", "sdlc:followup", title="big thing")
    assert _run(tmp_path, src) == 0
    out = capsys.readouterr().out
    assert REASON in out and src.swaps == []
    assert _run(tmp_path, src, "--apply") == 0
    out = capsys.readouterr().out
    assert REASON in out
    assert src.swaps == [(1, (), (PROPOSED,))]
    assert src.store[1]["labels"] == ["epic", "sdlc:followup"]
    assert src.comments == []


def test_every_upper_tier_label_and_case(tmp_path):
    src = FakeSource()
    for n, lab in enumerate(["story", "Dossier", "SPEC", "Epic"], start=1):
        src.add(n, PROPOSED, lab)
    _run(tmp_path, src, "--apply")
    for n in range(1, 5):
        assert GOAL not in src.store[n]["labels"] and PROPOSED not in src.store[n]["labels"]
        assert PARKED not in src.store[n]["labels"]


def test_plain_item_still_armed(tmp_path):
    src = FakeSource()
    src.add(1, PROPOSED, "sdlc:followup", title="tidy the docs", body="cleanup of old text")
    _run(tmp_path, src, "--apply")
    assert GOAL in src.store[1]["labels"] and PROPOSED not in src.store[1]["labels"]


def test_apply_refuses_epic_and_story_in_one_line():
    for lab in ("epic", "story"):
        src = FakeSource()
        src.add(7, PROPOSED, lab)
        out = promote.promote(".sdlc", {}, [7], source=src)
        (r,) = out["results"]
        assert r["outcome"] == "skipped" and "\n" not in r["detail"] and lab in r["detail"]
        assert src.swaps == []


def test_demote_unaffected_for_epic():
    src = FakeSource()
    src.add(7, GOAL, "epic")
    out = promote.promote(".sdlc", {}, [7], source=src, demote=True)
    assert out["results"][0]["outcome"] != "skipped" or "upper tier" not in out["results"][0]["detail"]
