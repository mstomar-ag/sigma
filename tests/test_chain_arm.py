"""#1587: `chain.py approve` and `chain.py arm` -- the only verb that labels and arms a chain's members.

A stateful fake `gh` records every call (no network). Before approval no GitHub write exists in any verb;
after it, `arm` creates the label `chain:<root>` if it is missing, adds it to every member, arms the unarmed
armable ones through the label drain's own arm machinery, and posts one fixed comment per armed member.

Controls (each clause alone, then restored): drop the approved-state gate; double-post on a re-run; arm an
upper-tier member; reassign an assigned issue.
"""
import json
import pathlib
import re
import sys
import urllib.parse

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import resource_mode  # noqa: E402,F401  (a test building its own subprocess env carries the mode from here)
import test_label_answer as tla  # noqa: E402
from test_label_drain_arm import DW, _mod, _repo  # noqa: E402

NOW = 1_800_000_000.0
LABEL = "chain:1"
SENTINEL = "SECRET-TITLE-TEXT"
READ_VERBS = ("issue view", "issue list", "repo view")


class HttpError(RuntimeError):
    """What the real runner raises: gh's stderr rides on `.hint`, which is all `gh_api` classifies from."""

    def __init__(self, text, hint):
        super().__init__(text)
        self.hint = hint


class Crash(BaseException):
    """A process death: nothing catches it on the way up, like a kill between two writes."""


def _chain():
    return _mod("chain")


class ChainFake(tla.Fake):
    """The answer fake plus label creation (`POST labels`), and a write counter that can die on write k."""

    def __init__(self, issues, existing_label=False, **kw):
        super().__init__(issues, **kw)
        self.missing = set() if existing_label else {LABEL}
        self.created = []
        self.write_count = 0
        self.crash_on = None

    @staticmethod
    def is_write(args):
        if args[0] == "api":
            return "--method" in args and args[args.index("--method") + 1] != "GET"
        return " ".join(args[:2]) not in READ_VERBS

    def __call__(self, args):
        args = [str(a) for a in args]
        if self.is_write(args):
            self.write_count += 1
            if self.crash_on == self.write_count:
                raise Crash("died before write %d" % self.write_count)
        if args[0] == "api" and re.fullmatch(r"repos/[^/]+/[^/]+/labels", args[1]) and "POST" in args:
            self.calls.append(args)
            name = next(a for a in args if a.startswith("name="))[5:]
            if name not in self.missing:
                raise RuntimeError("HTTP 422 already_exists")
            self.created.append(name)
            self.missing.discard(name)
            return json.dumps({"name": name})
        if args[0] == "api" and args[1].startswith("repos/") and urllib.parse.unquote(args[1]).endswith(
                "/labels/" + LABEL) and LABEL in self.missing:
            self.calls.append(args)
            raise HttpError("exit status 1", "gh: Not Found (HTTP 404)")
        return super().__call__(args)


def _backlog(bob=True):
    """Goal 1 (armed, assigned) waits on 2 (unarmed), 3 (unarmed, has a priority), 5 (unarmed, assigned to
    someone else) and 6 (an upper-tier story)."""
    made = [tla.issue(1, "sdlc:goal", assignees=("alice",), body="Blocked by #2\nBlocked by #3\nBlocked by #6\n"
                                                                   + ("Blocked by #5\n" if bob else "") + "\n" + DW),
            tla.issue(2, body=DW),
            tla.issue(3, "priority:P1", body=DW),
            tla.issue(6, "story", body=DW)]
    if bob:
        made.append(tla.issue(5, assignees=("bob",), body=DW))
    for i in made:
        i["title"] = SENTINEL
    return made


def _scene(tmp_path, approve=True, issues=None, **fk_kw):
    base = _repo(tmp_path, "alice")
    cp = pathlib.Path(base) / "config.json"
    cfg = json.loads(cp.read_text())
    cfg["discovery"]["dag"] = {"mode": "off"}
    cp.write_text(json.dumps(cfg))
    fk = ChainFake(issues if issues is not None else _backlog(), **fk_kw)
    _chain().plan(base, 1, run=fk, now=NOW)
    if approve:
        _chain().answer(base, 1, defaults=True, run=fk)
        res = _chain().answer(base, 1, {"cap:c1": "5"}, run=fk)
        assert res["state"] == "approved", res
    fk.calls.clear()
    fk.write_count = 0
    return base, fk


def _rec_path(base):
    return pathlib.Path(base) / "state" / "chains" / "c1.json"


def _rec(base):
    return json.loads(_rec_path(base).read_text())


def _put(base, rec):
    _rec_path(base).write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n")


def _field(args, key):
    return [a[len(key) + 1:] for i, a in enumerate(args) if i and args[i - 1] == "-f" and a.startswith(key + "=")]


def _writes(fk):
    """-> [(issue or None, kind, value)] for every write the fake saw, kinds label/comment/assignee/create."""
    out = []
    for a in fk.writes():
        path = a[1] if a[0] == "api" else ""
        m = re.fullmatch(r"repos/[^/]+/[^/]+/issues/(\d+)/(labels|comments|assignees)", path)
        if m:
            kind = {"labels": "label", "comments": "comment", "assignees": "assignee"}[m.group(2)]
            vals = _field(a, {"labels": "labels[]", "comments": "body", "assignees": "assignees[]"}[m.group(2)])
            out += [(int(m.group(1)), kind, v) for v in vals]
        elif re.fullmatch(r"repos/[^/]+/[^/]+/labels", path):
            out.append((None, "create", _field(a, "name")[0]))
        else:
            out.append((None, "other", " ".join(a[:3])))
    return out


def _sheet_writes(base):
    text = _chain().sheet(base, 1, now=NOW)
    block = text.split("Writes that follow approval:")[1].split("Nothing has been changed yet.")[0]
    labels = {int(n) for n in re.findall(r"#(\d+): add label chain:1", block)}
    arms = {int(n) for n in re.findall(r"#(\d+): arm it", block)}
    return text, labels, arms


def _names(fk, n):
    return fk._names(n)


# 1 -- nothing before approval ------------------------------------------------------------------------

def _states(tmp_path):
    """Every state a chain can be in before approval, each as (name, base, fake)."""
    out = []
    base, fk = _scene(tmp_path / "none", approve=False)
    _rec_path(base).unlink()
    out.append(("no record", base, fk))
    base, fk = _scene(tmp_path / "planned", approve=False)
    out.append(("planned", base, fk))
    base, fk = _scene(tmp_path / "awaiting", approve=False)
    _chain().answer(base, 1, {"label_set:c1": "arm them"}, run=fk)
    fk.calls.clear()
    assert _rec(base)["state"] == "awaiting-answers"
    out.append(("awaiting-answers", base, fk))
    base, fk = _scene(tmp_path / "forced")                        # everything answered, but the state is not
    rec = _rec(base)
    rec["state"] = "planned"
    _put(base, rec)
    out.append(("answered but planned", base, fk))
    base, fk = _scene(tmp_path / "incomplete")                    # approved on the record, plan incomplete
    rec = _rec(base)
    rec["complete"] = False
    _put(base, rec)
    out.append(("approved but incomplete", base, fk))
    return out


def test_no_write_before_approval(tmp_path):
    ch = _chain()
    for name, base, fk in _states(tmp_path):
        for dry in (False, True):
            with pytest.raises(ch.ChainRefused):
                ch.arm(base, 1, dry_run=dry, run=fk)
            argv = ["chain.py", "arm", base, "1"] + (["--dry-run"] if dry else [])
            assert ch.main(argv, run=fk) == 2, name
        assert fk.calls == [], "%s: arm touched GitHub before approval: %s" % (name, fk.calls[:3])
        # and the other verbs that exist before approval write nothing to GitHub either
        if name != "no record":
            ch.sheet(base, 1, now=NOW)
            if name in ("planned", "awaiting-answers"):
                with pytest.raises(ch.ChainRefused):
                    ch.approve(base, 1)
            ch.plan(base, 1, run=fk, now=NOW)
        assert fk.writes() == [], name
        assert fk.created == []


# 2 -- the writes are the sheet's list -----------------------------------------------------------------

def test_writes_match_sheet(tmp_path):
    ch = _chain()
    base, fk = _scene(tmp_path, issues=_backlog(bob=False))
    text, labels, arms = _sheet_writes(base)
    assert labels == {1, 2, 3, 6} and arms == {2, 3}, text
    assert "creates the label chain:1 if it is missing" in text
    dry = ch.arm(base, 1, dry_run=True, run=fk)
    assert fk.writes() == [] and fk.created == []
    shown = ch.render_arm(dry)
    assert {int(n) for n in re.findall(r"#(\d+): add label chain:1", shown)} == labels
    assert {int(n) for n in re.findall(r"#(\d+): arm it", shown)} == arms
    assert "create label chain:1" in shown and "DRY-RUN" in shown
    res = ch.arm(base, 1, run=fk)
    assert all(a["result"] == "done" for a in res["actions"]), res
    got = _writes(fk)
    assert {n for n, k, v in got if k == "label" and v == LABEL} == labels
    # the goal label goes through the label drain's one GraphQL mutation per armed member (no issue number in it)
    assert len([g for g in got if g[1] == "other" and "graphql" in g[2]]) == len(arms)
    assert {n for n in fk.by if "sdlc:goal" in _names(fk, n)} == arms | {1}
    assert {n for n, k, v in got if k == "comment"} == arms
    assert {n for n, k, v in got if k == "assignee"} == arms
    assert [(k, v) for n, k, v in got if k == "create"] == [("create", LABEL)]
    assert not [g for g in got if g[1] == "other" and "graphql" not in g[2]] and fk.body_patches() == []
    assert "priority:P2" in _names(fk, 2) and "priority:P1" in _names(fk, 3) and "priority:P2" not in _names(fk, 3)


# 3 -- idempotent --------------------------------------------------------------------------------------

def test_idempotent(tmp_path, capsys):
    ch = _chain()
    base, fk = _scene(tmp_path)
    first = ch.arm(base, 1, run=fk)
    assert first["actions"]
    fk.calls.clear()
    second = ch.arm(base, 1, run=fk)
    assert second["actions"] == [] and fk.writes() == [], _writes(fk)
    assert ch.main(["chain.py", "arm", base, "1"], run=fk) == 0
    assert "no actions needed" in capsys.readouterr().out
    assert ch.arm(base, 1, dry_run=True, run=fk)["actions"] == []
    assert fk.writes() == [] and fk.created == [LABEL]


# 4 -- a crash in the middle ----------------------------------------------------------------------------

def _outcome(fk):
    return ({n: sorted(fk._names(n)) for n in sorted(fk.by)},
            {n: fk.stored_comments(n) for n in sorted(fk.by)},
            {n: sorted(a["login"] for a in fk.by[n]["assignees"]) for n in sorted(fk.by)})


def test_crash_mid_arm(tmp_path):
    ch = _chain()
    base, fk = _scene(tmp_path / "clean")
    ch.arm(base, 1, run=fk)
    total, want = fk.write_count, _outcome(fk)
    assert total >= 6
    for k in range(1, total + 1):
        base, fk = _scene(tmp_path / ("k%d" % k))
        fk.crash_on = k
        with pytest.raises(Crash):
            ch.arm(base, 1, run=fk)
        fk.crash_on = None
        res = ch.arm(base, 1, run=fk)
        assert not any(a["result"] == "failed" for a in res["actions"]), (k, res)
        assert _outcome(fk) == want, "crash on write %d left a different end state" % k
        chain_label_posts = [w for w in _writes(fk) if w[1] == "label" and w[2] == LABEL]
        assert len(chain_label_posts) == len({w[0] for w in chain_label_posts}), "duplicate label add at k=%d" % k
        assert all(len(c) <= 1 for c in _outcome(fk)[1].values()), "duplicate comment at k=%d" % k
        assert fk.created == [LABEL]
        again = ch.arm(base, 1, run=fk)
        assert again["actions"] == []


# 5 -- upper tier and somebody else's issue -------------------------------------------------------------

def test_upper_tier_and_assignee(tmp_path):
    ch = _chain()
    base, fk = _scene(tmp_path)
    _text, _labels, arms = _sheet_writes(base)
    assert arms == {2, 3, 5}                       # the sheet cannot see who is assigned; arm says so
    res = ch.arm(base, 1, run=fk)
    assert "sdlc:goal" not in _names(fk, 6) and LABEL in _names(fk, 6)          # labelled, never armed
    assert "sdlc:goal" not in _names(fk, 5) and LABEL in _names(fk, 5)
    assert [a["login"] for a in fk.by[5]["assignees"]] == ["bob"]                # not reassigned
    assert fk.stored_comments(5) == [] and fk.stored_comments(6) == []
    assert [a["login"] for a in fk.by[2]["assignees"]] == ["alice"]
    assert {r["issue"]: r["verdict"] for r in res["refused"]} == {5: "assigned-elsewhere", 6: "upper-tier"}
    assert not [w for w in _writes(fk) if w[0] in (5, 6) and w[1] != "label"]
    assert "refused #6 (upper-tier)" in ch.render_arm(res)


# 6 -- the label and the comment ------------------------------------------------------------------------

def test_label_and_comment_shape(tmp_path):
    ch = _chain()
    base, fk = _scene(tmp_path / "missing")
    assert fk.created == []                                                      # approval alone creates nothing
    ch.arm(base, 1, run=fk)
    assert fk.created == [LABEL]                                                 # once, on arm
    sent = [w for w in _writes(fk) if w[1] == "comment"]
    assert sent and all(re.fullmatch(r"Sigma is arming this issue as part of the chain for #1\.", w[2])
                        for w in sent), sent
    for w in fk.writes():
        assert SENTINEL not in " ".join(w) and "issue 2" not in " ".join(w)
    for n in fk.by:
        assert all(SENTINEL not in c for c in fk.stored_comments(n))
    base, fk = _scene(tmp_path / "exists", existing_label=True)
    ch.arm(base, 1, run=fk)
    assert fk.created == [] and not [w for w in _writes(fk) if w[1] == "create"]
    assert all(LABEL in _names(fk, n) for n in (1, 2, 3, 5, 6))


def test_approve_requires_a_complete_sheet(tmp_path):
    ch = _chain()
    base, fk = _scene(tmp_path / "open", approve=False)
    with pytest.raises(ch.ChainRefused):
        ch.approve(base, 1)
    assert _rec(base)["state"] == "planned" and fk.writes() == []
    base, fk = _scene(tmp_path / "ok")
    rec = _rec(base)
    rec["state"] = "planned"
    _put(base, rec)
    assert ch.approve(base, 1)["state"] == "approved" and _rec(base)["state"] == "approved"
    assert fk.writes() == []
