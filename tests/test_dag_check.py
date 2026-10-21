"""#1617: the cheap change check for the stored dependency map, against a scripted repository.

Nothing here touches the network or `gh`. The fake emits exactly the shapes captured from the real CLI
(tests/fixtures/gh_api_include/): the header block of `200_etag.txt` and the whole of `304.txt`, with
only the ETag value and the body swapped. Each guard names the break that turns it red in its docstring
(`control:`); every one was run once and restored.
"""
import hashlib
import importlib.util
import json
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
S = ROOT / "skills" / "sigma-loop" / "scripts"
FIX = ROOT / "tests" / "fixtures" / "gh_api_include"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


dk = _mod("dag_check")
dc = dk.dag_collect
ds = dc.dag_store
loop = _mod("loop")

NOW = 1_800_000_000.0
REPO = "owner/repo"
CONFIG = {"discovery": {"github": {"repo": REPO}, "dag": {"mode": "on"}}}
OFF = {"discovery": {"github": {"repo": REPO}, "dag": {"mode": "off"}}}

_200 = (FIX / "200_etag.txt").read_text(encoding="utf-8")
_304 = (FIX / "304.txt").read_text(encoding="utf-8")
_HEAD200 = _200.split("\n\n", 1)[0]


def _iso(epoch):
    return dc._iso(epoch)


class World:
    """A scripted repository. `run` is the stdout-only runner; `get` is the conditional one."""

    def __init__(self):
        self.issues = {}
        self.prs = {}
        self.tick = 0
        self.get_calls = []
        self.run_calls = []
        self.board_prio = {}              # number -> label the LIST shows without the issue changing
        self.fail = None

    def add(self, number, labels=(), body="", state="open", pr=False):
        it = {"number": number, "title": "fixture issue %d" % number, "body": body, "state": state,
              "labels": [{"name": n} for n in labels], "assignees": [], "updated_at": "2026-10-01T00:00:00Z"}
        if pr:
            it["pull_request"] = {}
            self.prs.setdefault(number, {"state": "open", "merged": False})
        self.issues[number] = it
        return it

    def touch(self, number):
        self.tick += 1
        self.issues[number]["updated_at"] = _iso(NOW + 10 + self.tick)

    def edit(self, number, labels=None, body=None, state=None):
        it = self.issues[number]
        if labels is not None:
            it["labels"] = [{"name": n} for n in labels]
        if body is not None:
            it["body"] = body
        if state is not None:
            it["state"] = state
        self.touch(number)

    def _shown(self, it):
        extra = self.board_prio.get(it["number"])
        if not extra:
            return it
        out = dict(it)
        out["labels"] = list(it["labels"]) + [{"name": extra}]
        return out

    # stdout-only runner (the full read and the single reads)
    def __call__(self, args):
        self.run_calls.append(list(args))
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
        rows = [self._shown(i) for i in self.issues.values() if i["state"] == "open"]
        rows = [i for i in rows if all(w in [l["name"] for l in i["labels"]] for w in want)]
        rows.sort(key=lambda i: i["number"])
        per, page = int(fields["per_page"]), int(fields["page"])
        return json.dumps(rows[(page - 1) * per:page * per])

    # the conditional runner: (returncode, stdout, stderr), the fixture shapes
    def get(self, args):
        self.get_calls.append(list(args))
        if self.fail:
            self.fail(args)
        fields = {}
        for i, a in enumerate(args):
            if a == "-f":
                k, _, v = args[i + 1].partition("=")
                fields[k] = v
        sent = None
        if "-H" in args:
            sent = args[args.index("-H") + 1].split(": ", 1)[1]
        since = fields.get("since", "")
        rows = sorted((i for i in self.issues.values() if i["updated_at"] >= since),
                      key=lambda i: (i["updated_at"], i["number"]))
        per, page = int(fields.get("per_page", 100)), int(fields.get("page", 1))
        rows = rows[(page - 1) * per:page * per]
        body = json.dumps(rows)
        etag = 'W/"%s"' % hashlib.sha256(body.encode()).hexdigest()
        if sent == etag:
            text = re.sub(r'(?m)^Etag: .*$', "Etag: " + etag.replace("W/", ""), _304)
            return 1, text, "gh: HTTP 304\n"
        head = re.sub(r'(?m)^Etag: .*$', "Etag: " + etag, _HEAD200)
        return 0, head + "\n\n" + body, ""


def _base():
    w = World()
    w.add(1, ["sdlc:goal"], "Blocked by #2\nBlocked by #4\nBlocked by #5")
    w.add(2, ["sdlc:goal", "priority:P1"], "")
    w.add(3, ["sdlc:parked"], "")
    w.add(4, [], "", state="closed")
    w.add(5, [], "", pr=True)
    return w


def _sdlc(tmp_path, config=CONFIG):
    d = tmp_path / ".sdlc"
    (d / "state").mkdir(parents=True)
    (d / "config.json").write_text(json.dumps(config))
    return d


def _built(tmp_path, w, config=CONFIG):
    d = _sdlc(tmp_path, config)
    rep = dc.refresh(d, config, run=w, now=NOW)
    assert rep["status"] == "saved", rep
    w.run_calls.clear()
    return d


class Refresh:
    def __init__(self):
        self.calls = []

    def __call__(self, sdlc, config, **kw):
        self.calls.append(kw)
        return {"status": "saved"}


def _check(d, w, config=CONFIG, now=NOW + 100, refresh=None):
    return dk.check(d, config, get=w.get, run=w, now=now, refresh=refresh or Refresh())


def _norm(m):
    nodes = []
    for n in m["nodes"]:
        n = dict(n)
        n.pop("read_at", None)
        nodes.append(n)
    return {"nodes": sorted(nodes, key=lambda n: n["number"]),
            "edges": sorted((e["blocker"], e["blocked"], e["source"]) for e in m["edges"])}


# ------------------------------------------------------------------ 304

def test_304_is_one_request_no_change_and_checked_at_advances(tmp_path):
    """After one 200, the repeat with the stored ETag is a 304: exactly one request, the nodes and
    edges untouched, `checked_at` moved to now.
    control: treat 304 as a failure (raise in the not-modified branch) -> status error, no advance."""
    w = _base()
    d = _built(tmp_path, w)
    first = _check(d, w, now=NOW + 100)
    assert first["status"] == "applied"
    before, _ = ds.load(d, CONFIG)
    w.get_calls.clear()
    res = _check(d, w, now=NOW + 200)
    assert res["status"] == "not-modified" and len(w.get_calls) == 1 and w.run_calls == []
    after, _ = ds.load(d, CONFIG)
    assert after["header"]["checked_at"] == _iso(NOW + 200) != before["header"]["checked_at"]
    assert after["nodes"] == before["nodes"] and after["edges"] == before["edges"]
    assert any(a.startswith("If-None-Match: W/") for a in w.get_calls[0])


def test_first_request_uses_the_stamp_minus_overlap_as_the_cursor(tmp_path):
    w = _base()
    d = _built(tmp_path, w)
    _check(d, w)
    args = w.get_calls[0]
    since = args[args.index("since=" + _iso(NOW - dk.OVERLAP_SECONDS))] if ("since=" + _iso(NOW - dk.OVERLAP_SECONDS)) in args else None
    assert since is not None
    assert not any(a.startswith("If-None-Match") for a in args)
    for want in ("state=all", "sort=updated", "direction=asc", "per_page=100"):
        assert want in args


# ------------------------------------------------------------------ the table: check-then-apply == rebuild

def _s_label_add(w):
    w.edit(2, ["sdlc:goal", "priority:P0"])


def _s_label_remove(w):
    w.edit(2, ["sdlc:goal"])


def _s_comment_only(w):
    w.touch(1)


def _s_close(w):
    w.edit(2, state="closed")


def _s_reopen(w):
    w.edit(4, ["sdlc:goal"], state="open")


def _s_new_issue(w):
    w.add(6, ["sdlc:goal"], "Blocked by #1")
    w.touch(6)


def _s_body_edit(w):
    w.edit(1, body="Blocked by #3")


def _s_label_leaves_corpus(w):
    w.edit(3, [])


def _s_label_leaves_corpus_but_keeps_priority(w):
    """#1994: an issue that leaves the lists but stays open keeps the priority, life and assignees a rebuild reads."""
    w.edit(2, ["priority:P2", "sdlc:in-progress"])
    w.issues[2]["assignees"] = [{"login": "dana"}]


def _s_pr_merged(w):
    w.prs[5] = {"state": "closed", "merged": True}
    w.touch(5)


def _s_new_outside_ref(w):
    w.add(9, [], "")
    w.edit(1, body="Blocked by #2\nBlocked by #9")


SCENARIOS = [_s_label_add, _s_label_remove, _s_comment_only, _s_close, _s_reopen, _s_new_issue,
             _s_body_edit, _s_label_leaves_corpus, _s_label_leaves_corpus_but_keeps_priority, _s_pr_merged, _s_new_outside_ref]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda f: f.__name__[3:])
def test_check_then_apply_equals_a_full_rebuild(tmp_path, scenario):
    """Map after check-then-apply == a fresh full read of the same final state (read stamps aside).
    control: ignore the closed state when applying (treat every item as open) -> `close` goes red."""
    w = _base()
    d = _built(tmp_path, w)
    scenario(w)
    res = _check(d, w)
    assert res["status"] in ("applied", "not-modified"), res
    got, _ = ds.load(d, CONFIG)
    rebuilt = dc.collect(_sdlc(tmp_path / "other"), CONFIG, w, NOW + 100)["map"]
    assert _norm(got) == _norm(rebuilt)


def test_apply_changes_something_for_every_scenario_but_the_comment(tmp_path):
    """The table is not vacuous: a scenario that changes state changes the map."""
    for scenario in (_s_label_add, _s_close, _s_new_issue, _s_pr_merged):
        w = _base()
        d = _built(tmp_path / scenario.__name__, w)
        before, _ = ds.load(d, CONFIG)
        scenario(w)
        _check(d, w)
        after, _ = ds.load(d, CONFIG)
        assert _norm(after) != _norm(before), scenario.__name__


# ------------------------------------------------------------------ failure falls back

@pytest.mark.parametrize("how", ["get", "run"])
def test_any_failure_changes_nothing_and_says_so_on_stderr(tmp_path, capsys, how):
    """A raising transport (on the conditional get, or on a follow-up read) leaves the file
    byte-identical, never raises, and prints one stderr line.
    control: let the exception escape `check` -> pytest sees the exception."""
    w = _base()
    d = _built(tmp_path, w)
    _s_pr_merged(w)
    raw = ds.path(d).read_bytes()

    def boom(args):
        raise RuntimeError("boom")

    get = boom if how == "get" else w.get
    run = w if how == "get" else boom
    res = dk.check(d, CONFIG, get=get, run=run, now=NOW + 100, refresh=Refresh())
    assert res["status"] == "error"
    assert ds.path(d).read_bytes() == raw
    err = capsys.readouterr().err
    assert err.count("dag_check:") == 1 and "existing path" in err


def test_a_server_error_status_is_a_failure_not_a_change(tmp_path, capsys):
    w = _base()
    d = _built(tmp_path, w)
    raw = ds.path(d).read_bytes()
    get = lambda args: (1, "HTTP/2.0 502 Bad Gateway\n\n{}", "gh: Bad Gateway (HTTP 502)\n")  # noqa: E731
    res = dk.check(d, CONFIG, get=get, run=w, now=NOW + 100, refresh=Refresh())
    assert res["status"] == "error" and ds.path(d).read_bytes() == raw


def test_the_pick_hook_never_raises(tmp_path, monkeypatch, capsys):
    """`loop._dag_check_pick` swallows even a broken module.
    control: remove the try/except in the hook -> this raises."""
    d = _built(tmp_path, _base())

    def boom(*a, **k):
        raise RuntimeError("module broke")

    monkeypatch.setattr(loop, "_load", boom)
    loop._dag_check_pick(d, CONFIG, None)


# ------------------------------------------------------------------ too many changes

def test_more_than_500_changed_requests_a_full_refresh(tmp_path):
    """501 changed issues: nothing is applied, a forced full refresh is requested once.
    control: raise the limit to 600 -> the check applies and no refresh is requested."""
    w = _base()
    d = _built(tmp_path, w)
    for n in range(100, 601):
        w.add(n, ["sdlc:goal"], "")
        w.touch(n)
    raw = ds.path(d).read_bytes()
    rf = Refresh()
    res = _check(d, w, refresh=rf)
    assert res["status"] == "refresh-requested"
    assert len(rf.calls) == 1 and rf.calls[0].get("force") is True
    assert ds.path(d).read_bytes() == raw
    assert len(w.get_calls) <= 6


def test_exactly_500_changed_is_applied(tmp_path):
    w = _base()
    d = _built(tmp_path, w)
    for n in range(100, 600):
        w.add(n, ["sdlc:goal"], "")
        w.touch(n)
    rf = Refresh()
    res = _check(d, w, refresh=rf)
    assert res["status"] == "applied" and rf.calls == []
    got, _ = ds.load(d, CONFIG)
    assert len(got["nodes"]) >= 500


# ------------------------------------------------------------------ the documented gap

def test_a_board_field_only_priority_change_is_not_seen_by_the_check(tmp_path):
    """Known gap: a priority changed only on the board field moves no `updated_at`, so the check
    answers 304 / no change and the map keeps the old value.
    The next test is its pair: the next full read sees it."""
    w = _base()
    d = _built(tmp_path, w)
    _check(d, w, now=NOW + 100)
    w.board_prio[1] = "priority:P0"                     # the issue itself does not change
    res = _check(d, w, now=NOW + 200)
    assert res["status"] == "not-modified"
    got, _ = ds.load(d, CONFIG)
    assert {n["number"]: n["prio"] for n in got["nodes"]}[1] is None


def test_the_next_full_read_sees_the_board_priority_change(tmp_path):
    w = _base()
    d = _built(tmp_path, w)
    w.board_prio[1] = "priority:P0"
    assert dc.refresh(d, CONFIG, run=w, now=NOW + 300)["status"] == "saved"
    got, _ = ds.load(d, CONFIG)
    assert {n["number"]: n["prio"] for n in got["nodes"]}[1] == "P0"


# ------------------------------------------------------------------ off, no map, busy, caps

def test_mode_off_makes_no_request_and_touches_nothing(tmp_path):
    d = _sdlc(tmp_path, OFF)
    w = _base()
    before = sorted(p.name for p in d.rglob("*"))
    res = dk.check(d, OFF, get=w.get, run=w, now=NOW, refresh=Refresh())
    assert res["status"] == "off" and w.get_calls == [] and w.run_calls == []
    assert sorted(p.name for p in d.rglob("*")) == before


def test_no_map_makes_no_request_and_creates_nothing(tmp_path):
    d = _sdlc(tmp_path)
    w = _base()
    before = sorted(p.name for p in d.rglob("*"))
    res = dk.check(d, CONFIG, get=w.get, run=w, now=NOW, refresh=Refresh())
    assert res["status"] == "no-map" and w.get_calls == []
    assert sorted(p.name for p in d.rglob("*")) == before


def test_a_busy_refresh_lock_skips_the_check(tmp_path):
    w = _base()
    d = _built(tmp_path, w)
    with ds.refresh_lock(d, CONFIG) as held:
        assert held == "held"
        res = _check(d, w)
    assert res["status"] == "busy" and w.get_calls == []


def test_outside_reads_are_capped_and_the_rest_stay_unknown(tmp_path):
    """Past `OUTSIDE_MAX` new outside references are not read: they are stored unknown, as a full read does.
    control: drop the cap -> more reads than the cap."""
    w = _base()
    d = _built(tmp_path, w)
    extra = list(range(200, 200 + dk.dag_collect.OUTSIDE_MAX + 5))
    for n in extra:
        w.add(n, [], "")
    w.edit(1, body="\n".join("Blocked by #%d" % n for n in extra))
    res = _check(d, w)
    assert res["status"] == "applied"
    reads = [c for c in w.run_calls if re.search(r"/issues/\d+$", c[1])]
    assert len(reads) == dk.dag_collect.OUTSIDE_MAX
    got, _ = ds.load(d, CONFIG)
    states = {n["number"]: n["state"] for n in got["nodes"]}
    assert sum(1 for n in extra if states[n] == "unknown") == 5
    assert all(states[n] == "unknown" for n in sorted(extra)[:5])


# ------------------------------------------------------------------ the pick hook

def test_pick_hook_with_mode_off_or_no_map_makes_no_call_and_writes_nothing(tmp_path):
    """Mode off, and mode on without a map: the hook is a no-op (no request, no file, no output)."""
    for cfg, name in ((OFF, "off"), (CONFIG, "nomap")):
        d = _sdlc(tmp_path / name, cfg)
        before = sorted(p.name for p in d.rglob("*"))
        w = _base()

        class Src:
            _run = staticmethod(w)

        loop._dag_check_pick(d, cfg, Src)
        assert w.get_calls == [] and w.run_calls == []
        assert sorted(p.name for p in d.rglob("*")) == before


def test_both_picks_call_the_hook_once_each():
    text = (S / "loop.py").read_text()
    assert len(re.findall(r"^\s+_dag_check_pick\(sdlc_dir, config, source\)", text, re.M)) == 2


def test_pick_hook_runs_a_check_when_a_map_exists(tmp_path, monkeypatch):
    w = _base()
    d = _built(tmp_path, w)
    seen = []

    class Stub:
        @staticmethod
        def check(sdlc, config, **kw):
            seen.append((str(sdlc), kw))

    monkeypatch.setattr(loop, "_load", lambda name: Stub)

    class Src:
        _run = staticmethod(w)

    loop._dag_check_pick(d, dict(CONFIG, discovery=dict(CONFIG["discovery"], source="github")), Src)
    assert len(seen) == 1 and seen[0][0] == str(d) and seen[0][1]["run"] is Src._run


def test_the_module_has_no_direct_file_write_of_its_own():
    """The map is saved only through dag_store.save (no new write site)."""
    text = (S / "dag_check.py").read_text()
    assert "open(" not in text and "write_text" not in text and "atomic_write" not in text
