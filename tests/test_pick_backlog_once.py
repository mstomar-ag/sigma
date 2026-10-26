"""#2022: one `next` / `next-batch` call lists the backlog once, and a later call asks GitHub whether it changed.

Before: every candidate the pick looked at (a held one, a skipped one) called `next_pending` again, and each of
those re-listed the whole backlog. Now one pick scope (`GitHubSource.read_once`) reads each distinct list once and
every candidate filters that payload; across calls each list PAGE is requested with its own stored ETag through
`gh_api.conditional_get` and a 304 means the stored page.

The replay test (`test_replay_*`) came first: its expected orders were captured from the code BEFORE this change
(per-candidate list reads) and are pasted below, so they are the old behaviour, not the new one's own output.

Controls, each run red on its own and restored (the test each turns red):
* the hoist removed (`read_once` does not memoise)            -> test_one_next_lists_the_backlog_once_with_held_and_skipped
* page-1-only ETag (a page-1 304 reuses every stored page)    -> test_a_change_on_page_two_is_seen
* a 304 read as an empty page                                  -> test_a_second_call_sends_the_etag_and_a_304_reuses_the_cache
* the claim's label re-read removed (it no longer refreshes the labels the list saw) -> test_a_label_lost_after_the_list_is_caught_by_the_claim_reread

* the retry re-served the memoised list (no bypass, empty stored)   -> test_an_empty_first_list_is_re_read_by_the_retry_within_one_scope
* the claim's unconditional re-read, each clause of `_claimable_now` broken alone (goal label, in-progress,
  blocked, parked) and the whole re-read removed -> test_a_goal_changed_after_the_list_is_skipped_and_the_next_is_picked

No network and no real `gh`: the runner is an in-memory fake of the issues endpoint. Issue numbers are fixtures.
"""
import hashlib
import importlib.util
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_pick_dependency_gate as pg  # noqa: E402  (the .sdlc builder)

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"
REPO = "acme/widget"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Server:
    """The issues endpoint of one fixture repo: label AND filter, page/per_page, oldest first, ETag per page."""

    def __init__(self, issues):
        self.issues = issues
        self.requests = []          # every request: ("list", page, if_none_match) or ("issue", n) or ("other",)
        self.writes = []
        self.n304 = 0

    def _page(self, fields):
        labels = [x for x in fields.get("labels", "").split(",") if x]
        rows = [i for i in self.issues if all(l in [x["name"] for x in i["labels"]] for l in labels)]
        rows.sort(key=lambda i: i["number"])
        per, page = int(fields.get("per_page", 30)), int(fields.get("page", 1))
        return rows[(page - 1) * per: page * per]

    @staticmethod
    def _fields(args):
        out, it = {}, iter(args)
        for a in it:
            if a == "-f":
                k, _, v = next(it).partition("=")
                out[k] = v
        return out

    def _inm(self, args):
        for i, a in enumerate(args):
            if a == "-H" and args[i + 1].lower().startswith("if-none-match:"):
                return args[i + 1].split(":", 1)[1].strip()
        return None

    def _serve(self, args):
        endpoint = args[1]
        if endpoint.endswith("/issues") and "--method" in args and args[args.index("--method") + 1] == "GET":
            fields = self._fields(args)
            body = json.dumps(self._page(fields))
            etag = 'W/"%s"' % hashlib.sha1(body.encode()).hexdigest()[:12]
            self.requests.append(("list", int(fields.get("page", 1)), self._inm(args)))
            return etag, body
        return None, None

    def run(self, args):
        """The plain runner (`GitHubSource._raw_run`): stdout only."""
        args = list(args)
        if args[0] == "api":
            etag, body = self._serve(args)
            if body is not None:
                return body
            tail = args[1].rsplit("/", 1)[-1]
            if tail.isdigit():
                self.requests.append(("issue", int(tail)))
                row = next((i for i in self.issues if i["number"] == int(tail)), None)
                return json.dumps(row or {})
            if "--method" in args and args[args.index("--method") + 1] != "GET":
                self.writes.append(args)
            self.requests.append(("other",))
            return "{}"
        self.requests.append(("other",))
        if "edit" in args or "comment" in args:
            self.writes.append(args)
        return "{}"

    def run_i(self, args):
        """The `gh api -i` runner: `(returncode, stdout, stderr)` as `gh_api.run_gh` gives it."""
        args = list(args)
        etag, body = self._serve(args)
        if self._inm(args) == etag:
            self.n304 += 1
            return 1, 'HTTP/2.0 304 Not Modified\nEtag: %s\n\n' % etag, "gh: HTTP 304"
        return 0, 'HTTP/2.0 200 OK\nEtag: %s\n\n%s' % (etag, body), ""

    def lists(self):
        return [r for r in self.requests if r[0] == "list"]


def _issue(n, labels=(), body=""):
    names = ["sdlc:goal", *labels]
    return {"number": n, "body": body, "labels": [{"name": x} for x in names], "assignees": []}


def _backlog(count=12):
    """Fixture issues 1..count: a few parked, a few area-labelled, priorities mixed."""
    rows = []
    for n in range(1, count + 1):
        labels = []
        if n in (2, 9):
            labels.append("sdlc:parked")
        if n % 3 == 0:
            labels.append("area:fixture")
        if n in FEATURE_MEMBERS:
            labels.append("feature:" + FEATURE)
        if n in (5, 8):
            labels.append("priority:P0")
        elif n in (4, 7):
            labels.append("priority:P1")
        rows.append(_issue(n, labels))
    return rows


def _setup(tmp_path, issues, etag=False, scope=None, page_size=None):
    lp = pg._loop()
    base = pg._base(str(tmp_path), mirror=None, gh_mode=True)
    cfg = lp.state.load_config(base)
    server = Server(issues)
    src = lp.sources.GitHubSource(cfg, run=server.run, sdlc_dir=base)
    src._sleep = None
    if page_size:
        src._REST_PAGE_SIZE = page_size
    if etag:
        src._etag_run = server.run_i
    else:
        src._etag_run = None
    src.mark_in_progress = lambda goal: True       # the claim's own writes are not under test here
    if scope:
        src.label_scope = scope
    return lp, base, cfg, src, server


def _drive(lp, base, cfg, src, held=(), skip=(), batch=None):
    """The picks one `next` (or one `next-batch` of `batch` slots) makes, in order; held goals are
    declined by the dependency gate, exactly as a goal behind an open prerequisite is."""
    lp._map_dependency_hold = lambda sdlc_dir, source, goal, config, gate: (
        "fixture issue 99" if str(goal) in {str(h) for h in held} else None)
    lp._label_not_ready = lambda *a, **k: None
    if batch:
        return [p for p in lp.next_batch(base, src, cfg, max_concurrent=batch, extra_skip=set(skip))]
    return [lp._next(base, src, cfg, extra_skip=set(skip))]


# -- the replay: the pick order before this change, captured from the unmodified code -------------------

REPLAY = {
    "plain-next": (dict(), None, None),
    "held-and-skipped-next": (dict(held=(5, 8), skip=(4,)), None, None),
    "label-scoped-next": (dict(skip=(3,)), "area:fixture", None),
    "plain-batch": (dict(held=(5,), skip=(4,)), None, 4),
    "label-scoped-batch": (dict(held=(6,)), "area:fixture", 3),
    "feature-scoped-next": (dict(held=(5,)), None, None),
    "feature-scoped-batch": (dict(held=(5,)), None, 4),
}
FEATURE = "fixture-unit"
FEATURE_MEMBERS = (3, 5, 6, 9, 12)        # 9 is parked; 5 is the P0 member
EXPECTED = {
    "plain-next": [("goal", "5")],
    "held-and-skipped-next": [("goal", "7")],
    "label-scoped-next": [("goal", "6")],
    "plain-batch": [("goal", "8"), ("goal", "7"), ("goal", "1"), ("goal", "3")],
    "label-scoped-batch": [("goal", "3"), ("goal", "12"), ("DONE", None)],
    "feature-scoped-next": [("goal", "3")],
    "feature-scoped-batch": [("goal", "3"), ("goal", "6"), ("goal", "12"), ("DONE", None)],
}


@pytest.mark.parametrize("name", sorted(REPLAY))
def test_replay_pick_order_is_what_the_per_candidate_code_picked(tmp_path, name):
    kw, scope, batch = REPLAY[name]
    lp, base, cfg, src, server = _setup(tmp_path, _backlog(), scope=scope)
    if name.startswith("feature-"):
        src.scope_to_feature(FEATURE)
    picks = _drive(lp, base, cfg, src, batch=batch, **kw)
    print("COUNT", name, len(server.lists()))
    assert picks == EXPECTED[name]


# -- the saving: list requests per call ----------------------------------------------------------------------

def test_one_next_lists_the_backlog_once_with_held_and_skipped(tmp_path):
    lp, base, cfg, src, server = _setup(tmp_path, _backlog())
    picks = _drive(lp, base, cfg, src, held=(5, 8), skip=(4,))
    assert picks == [("goal", "7")]
    first = len(server.lists())
    lp2, base2, cfg2, src2, server2 = _setup(tmp_path / "again", _backlog())
    _drive(lp2, base2, cfg2, src2)
    assert first == len(server2.lists())       # three declined candidates cost no more than none


def test_a_batch_does_not_relist_per_candidate_or_per_slot(tmp_path):
    lp, base, cfg, src, server = _setup(tmp_path, _backlog())
    picks = _drive(lp, base, cfg, src, held=(5,), skip=(4,), batch=4)
    assert [p[1] for p in picks] == ["8", "7", "1", "3"]
    # Measured on this fixture: 24 list requests before the hoist, 10 after (the pick's own lists once, plus
    # the per-slot label sweeps, which read their own lists and are not part of the pick's payload).
    assert len(server.lists()) <= 10


def test_the_hoist_is_per_scope_not_forever(tmp_path):
    lp, base, cfg, src, server = _setup(tmp_path, _backlog())
    _drive(lp, base, cfg, src)
    n = len(server.lists())
    _drive(lp, base, cfg, src)
    assert len(server.lists()) == 2 * n        # a second call is a fresh read


# -- across calls: a stored ETag per page -----------------------------------------------------------------------

def _etag_file(base):
    return pathlib.Path(base) / "state" / "pick-backlog-etags.json"


def test_a_second_call_sends_the_etag_and_a_304_reuses_the_cache(tmp_path):
    lp, base, cfg, src, server = _setup(tmp_path, _backlog(), etag=True)
    assert _drive(lp, base, cfg, src) == [("goal", "5")]
    assert all(r[2] is None for r in server.lists())          # nothing stored yet: unconditional
    assert _etag_file(base).is_file()
    server.requests.clear()
    assert _drive(lp, base, cfg, src) == [("goal", "8")]      # 5 is this session's in-flight goal; same cached list
    sent = [r for r in server.lists() if r[2]]
    assert len(sent) >= 2 and server.n304 == len(sent)        # the pick's lists carried If-None-Match, all 304


def test_a_change_on_page_two_is_seen(tmp_path):
    issues = _backlog(5)
    lp, base, cfg, src, server = _setup(tmp_path, issues, etag=True, page_size=3)
    assert _drive(lp, base, cfg, src, skip=(1, 2, 3, 4)) == [("goal", "5")]
    assert {r[1] for r in server.lists()} == {1, 2}
    server.issues[4]["labels"].append({"name": "sdlc:parked"})   # issue 5 (page 2) is parked meanwhile
    server.requests.clear()
    assert _drive(lp, base, cfg, src, skip=(1, 2, 3, 4)) == [("DONE", None)]
    sent = {r[1]: r[2] for r in server.lists()}
    assert sent[1] and sent[2]                                 # page 2 had its own ETag, and it changed


def test_a_corrupt_etag_file_fails_open(tmp_path):
    lp, base, cfg, src, server = _setup(tmp_path, _backlog(), etag=True)
    _etag_file(base).write_text("{not json")
    assert _drive(lp, base, cfg, src) == [("goal", "5")]
    assert all(r[2] is None for r in server.lists())
    assert json.loads(_etag_file(base).read_text())            # rewritten whole


def test_the_etag_store_is_bounded(tmp_path):
    lp, base, cfg, src, server = _setup(tmp_path, _backlog(), etag=True)
    store = {"k%d" % i: {"1": {"etag": "W/\"x\"", "body": []}} for i in range(30)}
    _etag_file(base).write_text(json.dumps(store))
    _drive(lp, base, cfg, src)
    assert len(json.loads(_etag_file(base).read_text())) <= 8


def test_board_mode_reads_once_per_call_and_sends_no_etag(tmp_path):
    lp, base, cfg, src, server = _setup(tmp_path, _backlog(), etag=True)
    reads = []
    src._board_queue_read = lambda: reads.append(1) or None
    _drive(lp, base, cfg, src, held=(5, 8), skip=(4,))
    assert len(reads) == 1                                     # per candidate before; once now
    assert not [a for a in server.requests if a[0] == "board"]  # the GraphQL read has no ETag path


# -- the claim ---------------------------------------------------------------------------------------------------

def test_the_claim_still_rereads_the_labels_before_any_write(tmp_path):
    lp, base, cfg, src, server = _setup(tmp_path, _backlog(), scope="area:fixture")
    order = []
    real_fetch = src.fetch_body_labels
    src.fetch_body_labels = lambda g: order.append("read") or real_fetch(g)
    src.mark_in_progress = lambda g: order.append("write") or True
    assert _drive(lp, base, cfg, src) == [("goal", "3")]
    assert "read" in order and "write" in order
    assert order.index("read") < order.index("write")
    assert ("issue", 3) in server.requests


def test_a_label_lost_after_the_list_is_caught_by_the_claim_reread(tmp_path):
    lp, base, cfg, src, server = _setup(tmp_path, _backlog(), scope="area:fixture")
    real = src._backlog_list

    def listed(*a, **k):
        out = real(*a, **k)
        for row in server.issues:                         # between the list and the claim, 3 loses the label
            if row["number"] == 3:
                row["labels"] = [l for l in row["labels"] if l["name"] != "area:fixture"]
        return out
    src._backlog_list = listed
    claimed = []
    src.mark_in_progress = lambda g: claimed.append(g) or True
    assert _drive(lp, base, cfg, src) == [("goal", "6")]
    assert claimed == ["6"]


# -- the retry guard survives the hoist (#447 inside #2022) --------------------------------------------------------

def test_an_empty_first_list_is_re_read_by_the_retry_within_one_scope(tmp_path):
    lp, base, cfg, src, server = _setup(tmp_path, _backlog(3))
    src._BACKLOG_READ_RETRY_BASE = 0
    real, calls = src._fetch_issues_rest, []

    def flaky(labels, cap, assignee=None, **k):
        calls.append(1)
        return [] if len(calls) == 1 else real(labels, cap, assignee=assignee, **k)
    src._fetch_issues_rest = flaky
    with src.read_once():
        pending = src._fetch_pending([], set())[1]
    assert len(calls) == 2 and pending and pending[0]["number"] == 1     # a memoised [] would end as "drained"


# -- the claim re-reads the chosen goal before any write, unconditionally (#2022) -------------------------------

def _flip_after_list(src, server, number, add=None, drop=None):
    """Between the list read and the claim, issue `number` gains `add` / loses `drop` (another machine's write)."""
    real = src._backlog_list

    def listed(*a, **k):
        out = real(*a, **k)
        for row in server.issues:
            if row["number"] == number:
                row["labels"] = [l for l in row["labels"] if l["name"] != drop]
                if add:
                    row["labels"].append({"name": add})
        return out
    src._backlog_list = listed


PICKS = {                 # mode -> (scope label, feature, first pick, the next candidate)
    "unscoped": (None, False, "5", "8"),
    "feature": (None, True, "5", "3"),
    "label": ("area:fixture", False, "3", "6"),
}


@pytest.mark.parametrize("mode", sorted(PICKS))
@pytest.mark.parametrize("flip", ["sdlc:in-progress", "sdlc:parked", "sdlc:blocked", "-sdlc:goal"])
def test_a_goal_changed_after_the_list_is_skipped_and_the_next_is_picked(tmp_path, mode, flip):
    scope, feature, first, nxt = PICKS[mode]
    lp, base, cfg, src, server = _setup(tmp_path, _backlog(), scope=scope)
    if feature:
        src.scope_to_feature(FEATURE)
    if flip.startswith("-"):
        _flip_after_list(src, server, int(first), drop=flip[1:])
    else:
        _flip_after_list(src, server, int(first), add=flip)
    claimed = []
    src.mark_in_progress = lambda g: claimed.append(g) or True
    assert _drive(lp, base, cfg, src) == [("goal", nxt)]
    assert claimed == [nxt]                                              # the stale one got no write


def test_the_claim_reread_is_one_issue_get_per_claimed_goal_not_per_candidate(tmp_path):
    lp, base, cfg, src, server = _setup(tmp_path, _backlog())
    assert _drive(lp, base, cfg, src, held=(5, 8), skip=(4,)) == [("goal", "7")]
    # 5 and 8 were held: never re-read. 7 is read ONCE: the claim's re-read also serves `attach_at_pick`.
    assert [r for r in server.requests if r[0] == "issue"] == [("issue", 7)]


def _failing_read(src, fail):
    real = src._read_issue

    def read(goal, fields, comment_limit=None):
        if fail:
            raise RuntimeError("simulated read failure")
        return real(goal, fields, comment_limit=comment_limit)
    src._read_issue = read


def test_a_failed_claim_reread_fails_open_to_the_lists_verdict(tmp_path):
    lp, base, cfg, src, server = _setup(tmp_path, _backlog())
    _failing_read(src, True)
    assert _drive(lp, base, cfg, src) == [("goal", "5")]


def test_a_successful_claim_reread_lacking_the_goal_label_still_skips(tmp_path):
    lp, base, cfg, src, server = _setup(tmp_path, _backlog())
    _flip_after_list(src, server, 5, drop="sdlc:goal")
    _failing_read(src, False)
    assert _drive(lp, base, cfg, src) == [("goal", "8")]


def test_the_claim_reread_is_shared_with_attach_not_repeated(tmp_path):
    lp, base, cfg, src, server = _setup(tmp_path, _backlog())
    calls = []
    real = src._fetch_body_labels_live
    src._fetch_body_labels_live = lambda g: calls.append(g) or real(g)
    assert _drive(lp, base, cfg, src) == [("goal", "5")]
    assert calls == ["5"]
