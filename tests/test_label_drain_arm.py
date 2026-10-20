"""#1577: `label_drain.py arm` -- arm the unarmed members of a run label and pull in open blockers.

A stateful fake `gh` transport serves the reads `plan` needs and APPLIES the writes (label swap by
GraphQL, REST label add, comment, assignee), so a second run sees the first run's result. Every call is
recorded; the tests assert on the recorded writes, never on the fake.
"""
import importlib.util
import json
import pathlib
import re
import subprocess
import sys

import pytest

import gqlfake

ROOT = pathlib.Path(__file__).resolve().parents[1]
S = ROOT / "skills" / "sigma-loop" / "scripts"
LBL = "validation-run-1"
DW = "## Done when\n- [ ] a\n- [ ] b\n- [ ] c\n"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _issue(n, *labels, body=DW, state="open", assignees=()):
    return {"number": n, "title": "issue %d" % n, "body": body, "state": state,
            "labels": [{"name": x} for x in labels], "assignees": [{"login": a} for a in assignees],
            "user": {"login": "someone"}}


def _repo(tmp_path, assignee="alice", extra=None):
    base = tmp_path / ".sdlc"
    (base / "state").mkdir(parents=True)
    gh = {"repo": "acme/widget"}
    if assignee:
        gh["assignee"] = assignee
    cfg = {"discovery": {"source": "github", "github": gh}, "budget": {"max_iterations": 10}}
    cfg.update(extra or {})
    (base / "config.json").write_text(json.dumps(cfg))
    (base / "state" / "STATE.md").write_text("iteration: 0\nrun_iteration: 0\nlast_run: none\n")
    return str(base)


class Fake:
    """Stateful gh. `missing_labels`: label names whose existence lookup answers 404."""

    def __init__(self, issues, missing_labels=()):
        self.by = {i["number"]: i for i in issues}
        self.calls = []
        self.missing = set(missing_labels)

    def _names(self, n):
        return {l["name"] for l in self.by[n]["labels"]}

    def _set(self, n, names):
        self.by[n]["labels"] = [{"name": x} for x in sorted(names)]

    def __call__(self, args):
        args = [str(a) for a in args]
        self.calls.append(args)
        m = re.fullmatch(r"repos/[^/]+/[^/]+/labels/(.+)", args[1]) if args[0] == "api" else None
        if m:                                    # #1939: label_exists is a REST GET; answered before gqlfake's
            import urllib.parse
            if urllib.parse.unquote(m.group(1)) in self.missing:
                exc = RuntimeError("HTTP 404 Not Found")
                exc.hint = "gh: Not Found (HTTP 404)"
                raise exc
            return json.dumps({"name": m.group(1)})
        swaps = []
        g = gqlfake.swap(args, calls=swaps)
        if g is not None:
            for s in swaps:                      # ["issue","edit",N,"--repo",R,"--add-label",name]
                n = int(s[2])
                names = self._names(n)
                (names.add if s[-2] == "--add-label" else names.discard)(s[-1])
                self._set(n, names)
            return g
        if args[0] != "api":
            return ""
        path = args[1]
        method = args[args.index("--method") + 1] if "--method" in args else "GET"
        if path == "user":
            return json.dumps({"login": "alice"})
        m = re.fullmatch(r"repos/[^/]+/[^/]+/issues/(\d+)/(labels|comments|assignees)", path)
        if m and method == "POST":
            n, kind = int(m.group(1)), m.group(2)
            fields = [args[i + 1] for i, a in enumerate(args) if a == "-f"]
            if kind == "labels":
                self._set(n, self._names(n) | {f.split("=", 1)[1] for f in fields})
                return json.dumps([{"name": x} for x in self._names(n)])
            if kind == "assignees":
                logins = [f.split("=", 1)[1] for f in fields]
                self.by[n]["assignees"] += [{"login": x} for x in logins]
                return json.dumps({"assignees": self.by[n]["assignees"]})
            return json.dumps({"id": 1, "html_url": "x"})
        m = re.fullmatch(r"repos/[^/]+/[^/]+/issues/(\d+)(/comments)?", path)
        if m and method == "GET":
            n = int(m.group(1))
            if n not in self.by:
                raise RuntimeError("HTTP 404")
            if m.group(2):
                return "[]"
            return json.dumps(dict(self.by[n], comments=0))
        params = gqlfake.rest_list_params(args)
        assert params is not None, args
        want = [w for w in params.get("labels", "").split(",") if w]
        hits = [dict(i, comments=0) for i in self.by.values() if i["state"] == params.get("state", "open")
                and all(w in self._names(i["number"]) for w in want)]
        per, page = int(params["per_page"]), int(params["page"])
        return json.dumps(hits[(page - 1) * per:page * per])

    def writes(self):
        out = []
        for a in self.calls:
            if a[:2] == ["api", "graphql"]:
                if "mutation" in " ".join(a).lower():
                    out.append(a)
            elif a[0] == "api":
                if "--method" in a and a[a.index("--method") + 1] != "GET":
                    out.append(a)
            elif " ".join(a[:2]) not in ("issue view", "issue list", "repo view"):
                out.append(a)
        return out

    def comments(self):
        return [a for a in self.calls if a[0] == "api" and a[1].endswith("/comments") and "POST" in a]

    def body_patches(self):
        return [a for a in self.calls if a[0] == "api" and re.search(r"/issues/\d+$", a[1])
                and a[a.index("--method") + 1 if "--method" in a else 0] in ("PATCH", "PUT")]


def _arm(tmp_path, issues, apply=False, assignee="alice", extra=None, **fake_kw):
    ld = _mod("label_drain")
    base = _repo(tmp_path, assignee, extra)
    fk = Fake(issues, **fake_kw)
    return ld, base, fk, ld.arm(base, LBL, apply=apply, run=fk)


def _acts(res, n=None):
    return [(a["action"], a["detail"]) for a in res["actions"] if n is None or a["issue"] == str(n)]


def _unarmed(n=1, *extra, **kw):
    return _issue(n, LBL, "sdlc:blocked", *extra, **kw)


# --- dry-run / apply -------------------------------------------------------------------------------

def test_dry_run_prints_the_actions_and_makes_no_write_call(tmp_path):
    ld, base, fk, res = _arm(tmp_path, [_unarmed(1)])
    assert res["actions"] and all(a["result"] == "would" for a in res["actions"])
    assert fk.writes() == []
    assert "DRY-RUN" in ld.render_arm(res)


def test_apply_swaps_labels_and_adds_priority_and_assignee(tmp_path):
    ld, base, fk, res = _arm(tmp_path, [_unarmed(1)], apply=True)
    names = fk._names(1)
    assert "sdlc:goal" in names and "sdlc:blocked" not in names and "priority:P2" in names
    assert LBL in names                                   # the drain label is never removed
    assert [a["login"] for a in fk.by[1]["assignees"]] == ["alice"]
    assert all(a["result"] == "done" for a in res["actions"])
    assert fk.comments() == [] and fk.body_patches() == []


def test_second_apply_computes_zero_actions(tmp_path):
    ld, base, fk, res = _arm(tmp_path, [_unarmed(1), _unarmed(2)], apply=True)
    assert res["actions"]
    again = ld.arm(base, LBL, apply=True, run=fk)
    assert again["actions"] == [] and again["refused"] == []


# --- pulled blockers -------------------------------------------------------------------------------

def test_pulled_blocker_gets_goal_priority_drain_label_and_one_comment(tmp_path):
    member = _issue(1, "sdlc:goal", LBL, assignees=("alice",), body="Blocked by #7\n\n" + DW)
    blocker = _issue(7, "sdlc:blocked")
    ld, base, fk, res = _arm(tmp_path, [member, blocker], apply=True)
    names = fk._names(7)
    assert {"sdlc:goal", "priority:P2", LBL} <= names and "sdlc:blocked" not in names
    cs = fk.comments()
    assert len(cs) == 1 and "body=Pulled into %s as a prerequisite of #1." % LBL in cs[0]
    assert fk._names(1) == {"sdlc:goal", LBL}              # the member that was already armed is untouched
    again = ld.arm(base, LBL, apply=True, run=fk)
    assert again["actions"] == [] and len(fk.comments()) == 1


def test_arm_pulled_blockers_false_leaves_the_blocker_alone(tmp_path):
    member = _issue(1, "sdlc:goal", LBL, assignees=("alice",), body="Blocked by #7\n\n" + DW)
    ld, base, fk, res = _arm(tmp_path, [member, _issue(7)], apply=True,
                             extra={"label_drain": {"arm_pulled_blockers": False}})
    assert res["actions"] == [] and fk.writes() == [] and res["notes"]


def test_a_failed_label_add_posts_no_comment(tmp_path):
    member = _issue(1, "sdlc:goal", LBL, assignees=("alice",), body="Blocked by #7\n\n" + DW)
    ld = _mod("label_drain")
    base = _repo(tmp_path)
    fk = Fake([member, _issue(7)])

    def run(args):
        if args[0] == "api" and args[1].endswith("/7/labels") and "POST" in args:
            raise RuntimeError("HTTP 422")
        return fk(args)

    res = ld.arm(base, LBL, apply=True, run=run)
    assert any(a["result"] == "failed" for a in res["actions"]) and fk.comments() == []


# --- refusals: no write at all ---------------------------------------------------------------------

@pytest.mark.parametrize("issue,verdict", [
    (_issue(1, LBL, "epic"), "upper-tier"),
    (_issue(1, LBL, "sdlc:parked"), "parked"),
    (_issue(1, LBL, assignees=("bob",)), "assigned-elsewhere"),
    (_issue(1, LBL, "sdlc:goal", assignees=("bob",)), "assigned-elsewhere"),
    (_issue(1, LBL, "sdlc:goal", "feature:x", assignees=("alice",), body="Feature: y\n\n" + DW), "held-by-gate"),
])
def test_refused_issue_is_reported_and_receives_no_write(tmp_path, issue, verdict):
    ld, base, fk, res = _arm(tmp_path, [issue], apply=True)
    assert [r["verdict"] for r in res["refused"]] == [verdict]
    assert res["refused"][0]["reason"] and res["actions"] == [] and fk.writes() == []


# --- an armed member with no assignee (#1807) -----------------------------------------------------

def _armed_unassigned(n=1, *extra):
    return _issue(n, LBL, "sdlc:goal", *extra)


def test_armed_unassigned_member_is_not_refused_and_gets_only_the_assignee(tmp_path):
    ld, base, fk, res = _arm(tmp_path, [_armed_unassigned(1)], apply=True)
    assert res["refused"] == []
    assert _acts(res, 1) == [("assign", "alice")]
    assert [a for a in fk.writes() if a[0] == "api" and "labels" in a[1]] == []
    assert fk._names(1) == {LBL, "sdlc:goal"}
    assert [a["login"] for a in fk.by[1]["assignees"]] == ["alice"]


def test_armed_unassigned_member_dry_run_writes_nothing(tmp_path):
    ld, base, fk, res = _arm(tmp_path, [_armed_unassigned(1)])
    assert _acts(res, 1) == [("assign", "alice")] and fk.writes() == []


def test_armed_unassigned_member_is_left_alone_when_no_assignee_is_configured(tmp_path):
    ld, base, fk, res = _arm(tmp_path, [_armed_unassigned(1)], apply=True, assignee=None)
    assert res["refused"] == [] and res["actions"] == [] and fk.writes() == []


def test_a_second_apply_on_an_armed_unassigned_member_writes_nothing(tmp_path):
    ld, base, fk, res = _arm(tmp_path, [_armed_unassigned(1)], apply=True)
    n = len(fk.writes())
    res2 = ld.arm(base, LBL, apply=True, run=fk)
    assert res2["actions"] == [] and len(fk.writes()) == n


# --- priority config -------------------------------------------------------------------------------

def test_existing_priority_is_kept(tmp_path):
    ld, base, fk, res = _arm(tmp_path, [_unarmed(1, "priority:P0")], apply=True)
    assert fk._names(1) >= {"priority:P0", "sdlc:goal"} and "priority:P2" not in fk._names(1)


def test_unset_priority_takes_the_configured_default(tmp_path):
    ld, base, fk, res = _arm(tmp_path, [_unarmed(1)], apply=True, extra={"label_drain": {"default_priority": "P3"}})
    assert "priority:P3" in fk._names(1)


@pytest.mark.parametrize("bad", [3, None, ["P1"], "", "two words"])
def test_wrong_typed_default_priority_reads_as_p2(tmp_path, bad):
    ld, base, fk, res = _arm(tmp_path, [_unarmed(1)], apply=True, extra={"label_drain": {"default_priority": bad}})
    assert "priority:P2" in fk._names(1)
    assert _mod("label_drain").arm_settings({"label_drain": "x"}) == {"default_priority": "P2",
                                                                       "arm_pulled_blockers": True}


def test_a_missing_priority_label_is_never_created(tmp_path):
    ld, base, fk, res = _arm(tmp_path, [_unarmed(1)], apply=True, missing_labels={"priority:P2"})
    assert "priority:P2" not in fk._names(1) and "sdlc:goal" in fk._names(1)
    assert res["notes"] and not any(a[0] == "label" for a in fk.calls)


# --- no body edit anywhere, the transport, the CLI ------------------------------------------------

def test_no_scenario_edits_a_body_or_deletes(tmp_path):
    member = _issue(1, "sdlc:goal", LBL, assignees=("alice",), body="Blocked by #7\n\n" + DW)
    ld, base, fk, res = _arm(tmp_path, [member, _issue(7), _unarmed(3), _issue(4, LBL, "epic")], apply=True)
    assert fk.body_patches() == []
    assert not [a for a in fk.calls if "DELETE" in a or "delete" in a]


def test_cli_dry_run_then_apply(tmp_path, capsys):
    ld = _mod("label_drain")
    base = _repo(tmp_path)
    fk = Fake([_unarmed(1)])
    assert ld.main(["label_drain.py", "arm", base, LBL], run=fk) == 0
    out = capsys.readouterr().out
    assert "DRY-RUN" in out and "dry run: pass --apply" in out and fk.writes() == []
    assert ld.main(["label_drain.py", "arm", base, LBL, "--apply"], run=fk) == 0
    assert "APPLIED" in capsys.readouterr().out and "sdlc:goal" in fk._names(1)
    assert ld.main(["label_drain.py", "plan", base, LBL, "--apply"], run=fk) == 2


def test_cli_help_names_arm():
    out = subprocess.run([sys.executable, str(S / "label_drain.py"), "--help"], capture_output=True, text=True)
    assert out.returncode == 0 and "arm" in out.stdout and "--apply" in out.stdout
