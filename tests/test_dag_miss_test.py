"""#1618: the offline harness for the pick-time miss test (R1) and the fresh-child timing run (R2).

Nothing here touches the network or `gh`. `FakeBackend` keeps issues in memory with the `since` query and
the ETag/304 shapes of the real conditional GET (the same captured fixtures `test_dag_check.py` uses).
Each guard names the break that turns it red (`control:`); the ones marked RUN were broken once and restored.
"""
import hashlib
import importlib.util
import json
import pathlib
import re
import subprocess

import pytest

GIT = "git" + "@github.com"

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures" / "gh_api_include"
TOOL = ROOT / "tools" / "dag_miss_test.py"


def _load():
    spec = importlib.util.spec_from_file_location("dag_miss_test", TOOL)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


dm = _load()
NOW = 1_800_000_000.0
_200 = (FIX / "200_etag.txt").read_text(encoding="utf-8")
_304 = (FIX / "304.txt").read_text(encoding="utf-8")
_HEAD200 = _200.split("\n\n", 1)[0]
REPO = "owner/private-clone"


def _iso(epoch):
    return dm.dag_collect._iso(epoch)


class FakeBackend:
    def __init__(self, repo=REPO):
        self.repo = repo
        self.issues = {}
        self.tick = 0
        self.created = []
        self.hide = None                  # callable(item) -> True: the `since` query does not show it
        self.get_calls = 0
        self.next_no = 100

    def now(self):
        return NOW + 10 + self.tick + 1

    def _touch(self, n):
        self.tick += 1
        self.issues[n]["updated_at"] = _iso(NOW + 10 + self.tick)

    def seed(self, number, labels=(), state="open", body=""):
        self.issues[number] = {"number": number, "title": "fixture issue %d" % number, "body": body,
                               "state": state, "labels": [{"name": n} for n in labels], "assignees": [],
                               "updated_at": "2026-10-01T00:00:00Z"}

    def create(self, title, labels, body):
        self.next_no += 1
        n = self.next_no
        self.seed(n, labels, body=body)
        self.issues[n]["title"] = title
        self._touch(n)
        self.created.append(n)
        return n

    def add_label(self, n, label):
        names = [l["name"] for l in self.issues[n]["labels"]]
        if label not in names:
            self.issues[n]["labels"].append({"name": label})
        self._touch(n)

    def remove_label(self, n, label):
        self.issues[n]["labels"] = [l for l in self.issues[n]["labels"] if l["name"] != label]
        self._touch(n)

    def close(self, n):
        self.issues[n]["state"] = "closed"
        self._touch(n)

    def reopen(self, n):
        self.issues[n]["state"] = "open"
        self._touch(n)

    def comment(self, n, text):
        self._touch(n)

    @staticmethod
    def _fields(args):
        out = {}
        for i, a in enumerate(args):
            if a == "-f":
                k, _, v = args[i + 1].partition("=")
                out[k] = v
        return out

    def run(self, args):
        ep, f = args[1], self._fields(args)
        m = re.search(r"/issues/(\d+)$", ep)
        if m:
            return json.dumps(self.issues[int(m.group(1))])
        want = [x for x in f.get("labels", "").split(",") if x]
        rows = [i for i in self.issues.values() if i["state"] == "open"
                and all(w in [l["name"] for l in i["labels"]] for w in want)]
        rows.sort(key=lambda i: i["number"])
        per, page = int(f["per_page"]), int(f["page"])
        return json.dumps(rows[(page - 1) * per:page * per])

    def get(self, args):
        self.get_calls += 1
        f = self._fields(args)
        sent = args[args.index("-H") + 1].split(": ", 1)[1] if "-H" in args else None
        rows = sorted((i for i in self.issues.values() if i["updated_at"] >= f.get("since", "")
                       and not (self.hide and self.hide(i))), key=lambda i: (i["updated_at"], i["number"]))
        per, page = int(f.get("per_page", 100)), int(f.get("page", 1))
        body = json.dumps(rows[(page - 1) * per:page * per])
        etag = 'W/"%s"' % hashlib.sha256(body.encode()).hexdigest()
        if sent == etag:
            return 1, re.sub(r'(?m)^Etag: .*$', "Etag: " + etag.replace("W/", ""), _304), "gh: HTTP 304\n"
        return 0, re.sub(r'(?m)^Etag: .*$', "Etag: " + etag, _HEAD200) + "\n\n" + body, ""


def _git_clone(tmp_path, url):
    d = tmp_path / "clone"
    d.mkdir()
    subprocess.run(["git", "init", "-q", str(d)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(d), "remote", "add", "origin", url], check=True, capture_output=True)
    return d


def _config(tmp_path, repo=REPO):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"repo": repo}))
    return p


def _argv(cfg, clone, *more):
    return ["--config", str(cfg), "--clone", str(clone), "--confirm", *more]


# ------------------------------------------------------------------ refusals

class Factory:
    def __init__(self, backend=None):
        self.calls, self.backend = [], backend or FakeBackend()

    def __call__(self, repo):
        self.calls.append(repo)
        return self.backend


@pytest.mark.parametrize("url", [GIT + ":owner/private-clone.git",
                                 "https://example.test/owner/private-clone.git",
                                 "ssh://git" + "@host-alias/Owner/Private-Clone"])
def test_matching_remote_forms_are_accepted(tmp_path, url):
    clone = _git_clone(tmp_path, url)
    assert dm.check_target(_config(tmp_path), clone) == REPO


def test_refuses_a_remote_that_is_not_the_configured_repo(tmp_path, capsys):
    """control (RUN): drop the remote comparison and this goes red (the harness runs)."""
    clone = _git_clone(tmp_path, GIT + ":owner/another-repo.git")
    fac = Factory()
    assert dm.main(_argv(_config(tmp_path), clone), backend_factory=fac) == 2
    assert fac.calls == [] and fac.backend.created == []
    assert "remote" in capsys.readouterr().err


def test_refuses_the_public_repository_even_when_configured(tmp_path, capsys):
    """control (RUN): drop the public-repo clause and this goes red; the remote clause alone passes it."""
    clone = _git_clone(tmp_path, GIT + ":someone/sigma.git")
    fac = Factory()
    assert dm.main(_argv(_config(tmp_path, "someone/sigma"), clone), backend_factory=fac) == 2
    assert fac.calls == []
    assert "public" in capsys.readouterr().err


def test_refuses_the_checkout_own_origin(tmp_path):
    """The tool's own checkout origin is public by definition; a config naming it is refused."""
    clone = _git_clone(tmp_path, GIT + ":someone/else.git")
    with pytest.raises(dm.Refusal, match="public"):
        dm.check_target(_config(tmp_path, "someone/else"), clone, own_origin="Someone/Else")


def test_refuses_missing_config_and_missing_confirm(tmp_path):
    clone = _git_clone(tmp_path, GIT + ":owner/private-clone.git")
    fac = Factory()
    assert dm.main(["--config", str(tmp_path / "none.json"), "--clone", str(clone), "--confirm"],
                   backend_factory=fac) == 2
    assert dm.main(["--config", str(_config(tmp_path)), "--clone", str(clone)], backend_factory=fac) == 2
    assert fac.calls == []


def test_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as e:
        dm.main(["--help"])
    assert e.value.code == 0
    assert "usage" in capsys.readouterr().out.lower()


# ------------------------------------------------------------------ R1

def test_r1_zero_misses_over_all_mutations_and_priorities():
    """The real `dag_check` keeps the map equal to a fresh read. control (RUN): make the comparison
    always equal -> the deliberate-miss test below goes red instead; make it always differ -> this one."""
    b = FakeBackend()
    res = dm.r1(b, rounds=40)
    assert res["rounds"] >= 40 and res["misses"] == 0
    combos = {(r[1], r[2]) for r in res["rows"]}
    assert combos == {(m, p) for m in dm.MUTATIONS for p in ("P0", "P1", "P2", "P3")}
    assert len(dm.MUTATIONS) == 6 and b.get_calls >= 40


def test_r1_reports_a_hidden_change_as_a_miss_and_exits_one(tmp_path, capsys):
    clone = _git_clone(tmp_path, GIT + ":owner/private-clone.git")
    b = FakeBackend()
    b.hide = lambda item: True
    assert dm.r1(b, rounds=40)["misses"] > 0
    b2 = FakeBackend()
    b2.hide = lambda item: True
    assert dm.main(_argv(_config(tmp_path), clone, "--only", "r1"), backend_factory=Factory(b2)) == 1
    assert "miss" in capsys.readouterr().out


def test_wide_strategy_recovers_hidden_top_priority_changes():
    hidden = lambda item: any(l["name"] in ("priority:P0", "priority:P1") for l in item["labels"])
    plain, wide = FakeBackend(), FakeBackend()
    plain.hide = wide.hide = hidden
    assert dm.r1(plain, rounds=40)["misses"] > 0
    assert dm.r1(wide, rounds=40, strategy="wide")["misses"] == 0


# ------------------------------------------------------------------ R2

def test_r2_three_runs_per_mode_with_fresh_state_dirs():
    res = dm.r2(FakeBackend(), runs=3)
    assert len(res["off"]) == 3 and len(res["on"]) == 3
    assert all(isinstance(t, float) and t >= 0 for t in res["off"] + res["on"])
    assert res["median"]["off"] == sorted(res["off"])[1] and res["median"]["on"] == sorted(res["on"])[1]
    assert len(set(res["dirs"])) == 6


# ------------------------------------------------------------------ cleanup

def test_cleanup_closes_exactly_the_created_tickets_and_prints_them(tmp_path, capsys):
    """control (RUN): skip the cleanup call and the created tickets stay open."""
    clone = _git_clone(tmp_path, GIT + ":owner/private-clone.git")
    b = FakeBackend()
    b.seed(7, ["sdlc:goal"])
    assert dm.main(_argv(_config(tmp_path), clone, "--only", "r1", "--rounds", "40"),
                   backend_factory=Factory(b)) == 0
    out = capsys.readouterr().out
    assert b.created and all(b.issues[n]["state"] == "closed" for n in b.created)
    assert b.issues[7]["state"] == "open"
    assert all(("ticket %d" % n) in out for n in b.created)


def test_cleanup_function_leaves_nothing_open():
    b = FakeBackend()
    dm.r1(b, rounds=12)
    assert any(b.issues[n]["state"] == "open" for n in b.created)
    closed = dm.cleanup(b)
    assert sorted(closed) == sorted(b.created) and all(b.issues[n]["state"] == "closed" for n in b.created)


# ------------------------------------------------------------------ hygiene

def test_source_and_output_hold_no_private_text(tmp_path, capsys):
    """control (RUN): add a home path or a ticket title to the source / table and this goes red."""
    src = TOOL.read_text(encoding="utf-8")
    doc = (ROOT / "docs" / "dag-miss-test.md").read_text(encoding="utf-8")
    for text in (src, doc):
        assert not re.search("/Us" + "ers/|/ho" + "me/|\\\\Us" + "ers\\\\", text)
        assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text.replace(GIT, "").replace("git" + "@host", ""))
        assert not re.search(r"https?://", text)
        assert not re.search("(?i)agr" + "im|ons" + "hot", text)
    assert not re.search(r"\d+(\.\d+)?\s?(ms|s|seconds|requests)\b", doc)
    clone = _git_clone(tmp_path, GIT + ":owner/private-clone.git")
    b = FakeBackend()
    dm.main(_argv(_config(tmp_path), clone, "--rounds", "40", "--runs", "3"), backend_factory=Factory(b))
    out = capsys.readouterr().out
    for n in b.created:
        t = b.issues[n]
        assert t["title"] not in out and (not t["body"] or t["body"] not in out)
    assert str(tmp_path) not in out
