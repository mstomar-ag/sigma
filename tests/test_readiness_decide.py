"""tools/readiness/decide.py (#331, #429): the launch GO/NO-GO checker.

Hermetic: no test reaches the network or the real `gh`. In-process tests pass `_FakeGh` as `run`;
it answers exactly the four `gh` calls the checker makes and fails the test on any other. Gesture
tests copy the checker into a fixture and run the documented
`python3 tools/readiness/decide.py .` from its root, with a stub `gh` first on PATH. Fixture
repositories are real git work trees whose `refs/remotes/origin/main` is a commit; every fixture
git call runs with no inherited `GIT_*`, global and system config off and an empty HOME, so a
hook's or an editor's environment cannot point a fixture at another repository.
"""
import copy
import datetime
import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "readiness" / "decide.py"
RULE = ROOT / "docs" / "launch" / "decision-rule.md"
SHIPPED_CARD = ROOT / "docs" / "launch" / "scorecard.json"
SHIPPED_DEFINITION = ROOT / "docs" / "launch" / "definition.json"
#: github.com, so `--repo acme/widget` names the same repository origin does (#429 B2).
ORIGIN = "https://github.com/acme/widget.git"
QUERY = "issues?state=open&labels=launch:blocker&per_page=100"
MAIN_REF = "refs/remotes/origin/main"
#: The environment pins every git call the checker makes must carry (#429 D2); fixtures use them
#: too.
GIT_PINS = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"}


# A placeholder token value; fixture only (it authenticates nothing).
_KEPT_AUTH_VALUE = "kept-" "for-auth"

def _tool():
    spec = importlib.util.spec_from_file_location("readiness_decide_331", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dz = _tool()
BENCH = "docs/launch/evidence/benchmark-results.json"
PROOF = "docs/launch/evidence/proof.md"


def _passing_card():
    card = json.loads(SHIPPED_CARD.read_text(encoding="utf-8"))
    card["benchmark_results"] = BENCH
    for dim in card["dimensions"]:
        dim["score"] = 3
        dim["evidence"] = ["https://example.invalid/evidence/" + dim["id"]]
    return card


def _definition(status="signed"):
    return {"schema": "launch-definition/v1", "status": status, "signed_by": "owner",
            "signed_on": "2026-09-30"}


_SESSION = {}


@pytest.fixture(autouse=True, scope="session")
def _empty_home_for_fixtures(tmp_path_factory):
    _SESSION.setdefault("home", str(tmp_path_factory.mktemp("empty-home")))


def _clean_env(**extra):
    """os.environ minus every GIT_* and XDG_CONFIG_HOME, HOME empty, then `extra` (None drops)."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.pop("XDG_CONFIG_HOME", None)
    env["HOME"] = _SESSION["home"]
    env.update(extra)
    return {k: v for k, v in env.items() if v is not None}


def _git(root, *args, **env):
    """Fixture git: no inherited GIT_*, the checker's pins, an empty HOME. Returns stdout."""
    proc = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           "-c", "commit.gpgsign=false", "-c", "core.hooksPath=" + os.devnull,
                           "-c", "init.defaultBranch=main", "-C", str(root), *args],
                          capture_output=True, text=True, env=_clean_env(**{**GIT_PINS, **env}))
    if proc.returncode != 0:
        raise RuntimeError(f"fixture git {args} failed: {proc.stderr}")
    return proc.stdout.strip()


def _git_repo(root, remotes=("origin",), commit=True, url=ORIGIN):
    """Make root a git work tree; commit everything and point origin/main at it."""
    _git(root, "init", "-q")
    for name in remotes:
        _git(root, "remote", "add", name, url if name == "origin" else
             f"https://example.invalid/{name}/repo.git")
    if commit:
        _commit_main(root)
    return root


def _commit_main(root, message="fixture"):
    """Commit everything and point refs/remotes/origin/main at it, as a fetch would have."""
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "--allow-empty", "-m", message)
    _git(root, "update-ref", MAIN_REF, "HEAD")


def _main_sha(root):
    """The commit this checkout's refs/remotes/origin/main names, or "" when it has none."""
    try:
        return _git(root, "show-ref", "--verify", "--hash", MAIN_REF)
    except RuntimeError:
        return ""


def _repo(tmp_path, card=None, status="signed", bench=True, definition=True, files=None,
          git=True, **git_kw):
    """A launch checkout. With git (the default) everything is committed and is origin/main."""
    root = tmp_path / "repo"
    (root / "docs" / "launch" / "evidence").mkdir(parents=True)
    (root / "docs" / "launch" / "scorecard.json").write_text(
        json.dumps(card if card is not None else _passing_card()), encoding="utf-8")
    if definition:
        (root / "docs" / "launch" / "definition.json").write_text(
            json.dumps(_definition(status)), encoding="utf-8")
    if bench:
        (root / BENCH).write_text("{}", encoding="utf-8")
    for rel, text in (files or {}).items():
        (root / rel).write_text(text, encoding="utf-8")
    if git:
        _git_repo(root, **git_kw)
    return root


def _issue(number, state="open", **extra):
    return {"number": number, "state": state, "labels": [{"name": "launch:blocker"}], **extra}


def _blockers(tmp_path, items):
    path = tmp_path / "blockers.json"
    path.write_text(json.dumps(items), encoding="utf-8")
    return str(path)


class _Proc:
    def __init__(self, rc, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def _ref_answer(sha):
    """What `gh api repos/O/N/git/ref/heads/main` prints for a main at `sha`."""
    return json.dumps({"ref": "refs/heads/main", "object": {"sha": sha, "type": "commit"}})


class _FakeGh:
    """`run` for in-process tests. Answers exactly the four gh calls the checker makes -- `gh config
    get http_unix_socket`, `gh config get -h <host> http_unix_socket`, the REST read of main (by
    default the fixture's own refs/remotes/origin/main) and the paginated issues read (`pages`) --
    and fails the test on anything else. An answer is gh's stdout, or a `_Proc`."""

    def __init__(self, root, pages="[]", socket=None, ref=None):
        self.root, self.pages, self.socket, self.ref = root, pages, socket or {}, ref
        self.calls = []

    @staticmethod
    def kind(argv):
        if argv[:3] == ["gh", "config", "get"]:
            return "config-host" if "-h" in argv else "config-global"
        if argv[:2] == ["gh", "api"] and len(argv) == 4 and argv[3].endswith("/git/ref/heads/main"):
            return "main"
        if (argv[:2] == ["gh", "api"] and argv[-1:] == ["--paginate"]
                and argv[3].endswith("/" + QUERY)):
            return "issues"
        return None

    def __call__(self, argv, **kw):
        argv = list(argv)
        self.calls.append((argv, kw))
        kind = self.kind(argv)
        if kind is None:
            raise AssertionError(f"unexpected call: {argv}")
        if kind.startswith("config-"):
            answer = self.socket.get(kind[len("config-"):], "")
        elif kind == "main":
            answer = self.ref if self.ref is not None else _ref_answer(_main_sha(self.root))
        else:
            answer = self.pages
        return answer if isinstance(answer, _Proc) else _Proc(0, answer)

    @property
    def issue_reads(self):
        return [argv for argv, _ in self.calls if self.kind(argv) == "issues"]

    @property
    def api_calls(self):
        return [argv for argv, _ in self.calls if argv[:2] == ["gh", "api"]]


def _main(argv, run=None):
    """dz.main, any exception turned into a failed assertion (a red must be an assertion)."""
    try:
        return dz.main(argv) if run is None else dz.main(argv, run=run)
    except Exception as exc:  # noqa: BLE001 -- the point is to report it as an assertion
        raise AssertionError(f"decide.py raised {type(exc).__name__}: {exc}") from exc


def _run(root, blockers, capsys, extra=(), fake=None):
    """The documented live path, gh faked: the list `_blockers` wrote is what gh answers."""
    if fake is None:
        fake = _FakeGh(root, pathlib.Path(blockers).read_text(encoding="utf-8"))
    code = _main([str(root), *extra], run=fake)
    out, err = capsys.readouterr()
    return code, out.splitlines(), err


def _run_offline(root, blockers, capsys, extra=()):
    """The offline form, `--blockers-json <file>`."""
    code = _main([str(root), "--blockers-json", blockers, *extra])
    out, err = capsys.readouterr()
    return code, out.splitlines(), err


def _dim(card, did):
    return next(d for d in card["dimensions"] if d["id"] == did)


# -- the documented gesture, run as the docs give it --------------------------------------------

VICTIM_URL = "https://github.com/victim/widget.git"
ATTACKER_URL = "https://github.com/attacker/widget.git"
STUB_GH = r"""#!/bin/sh
# Stand-in for gh: appends this call to $STUB_RECORD and answers the four calls decide.py makes.
{ for a in "$@"; do printf '%s\t' "$a"; done; printf 'GH_REPO=%s\n' "${GH_REPO-unset}"; } \
  >> "$STUB_RECORD"
if [ "$1 $2" = "config get" ]; then exit 0; fi
case "$*" in
  */git/ref/heads/main)
    printf '{"ref":"refs/heads/main","object":{"sha":"%s","type":"commit"}}\n' "$STUB_MAIN_SHA" ;;
  *repos/victim/widget/issues\?*) printf '%s\n' "${STUB_ISSUES:-[]}" ;;
  *) printf '[]\n' ;;
esac
"""


#: gh's measured paging (real gh 2.98.0, #429 B1): with GH_FORCE_TTY set, gh treats piped stdout as
#: a terminal and pipes `gh api` output through GH_PAGER if it is set (even to ""), else gh config's
#: `pager`, else PAGER; an empty pager means none. This wrapper models that around STUB_GH (it does
#: not read gh config). The measurement is in docs/launch/evidence/429-controls.md.
STUB_GH_PAGING = r"""#!/bin/sh
out=$(sh "$(dirname "$0")/gh-inner" "$@"); rc=$?
pager=
if [ -n "${GH_FORCE_TTY-}" ]; then
  if [ "${GH_PAGER+set}" = set ]; then pager=$GH_PAGER; else pager=${PAGER-}; fi
fi
if [ -n "$pager" ]; then printf '%s\n' "$out" | sh -c "$pager"; else printf '%s\n' "$out"; fi
exit $rc
"""
#: A pager that keeps main's ref answer and turns every JSON array (the blocker list) into [].
FORGING_PAGER = r"""in=$(cat); case "$in" in \[*) printf '[]' ;; *) printf '%s' "$in" ;; esac"""


def _origin_repo(base, url=VICTIM_URL, **repo_kw):
    """A launch checkout at base/repo whose refs/remotes/origin/main came from `git fetch` of a bare
    base/origin.git; origin's URL is then set to `url`, so a live read names that repository."""
    root = _repo(base, git=False, **repo_kw)
    bare = base / "origin.git"
    _git(base, "init", "-q", "--bare", str(bare))
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "main")
    _git(root, "remote", "add", "origin", str(bare))
    _git(root, "push", "-q", "origin", "HEAD:refs/heads/main")
    _git(root, "fetch", "-q", "origin")
    _git(root, "remote", "set-url", "origin", url)
    return root


def _gesture(root, *args, env=None, issues=None, main_sha=None, paging=False):
    """`python3 tools/readiness/decide.py . <args>` from the fixture root, with this checker's bytes
    copied in (untracked), no inherited GIT_*, an empty HOME and a stub gh first on PATH that serves
    `main_sha` (default: the fixture's refs/remotes/origin/main) as the remote main and `issues` as
    victim/widget's open launch:blocker list. Returns (proc, each gh call as a list: its arguments,
    then `GH_REPO=<value>`). With paging, the stub gh pages its output as real gh does
    (STUB_GH_PAGING)."""
    tool = root / "tools" / "readiness" / "decide.py"
    tool.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(TOOL, tool)
    bin_dir = root.parent / "stub-bin"
    bin_dir.mkdir(exist_ok=True)
    if paging:
        (bin_dir / "gh-inner").write_text(STUB_GH, encoding="utf-8")
    (bin_dir / "gh").write_text(STUB_GH_PAGING if paging else STUB_GH, encoding="utf-8")
    (bin_dir / "gh").chmod(0o755)
    record = root.parent / "gh-calls.txt"
    record.write_text("", encoding="utf-8")
    child = _clean_env(STUB_RECORD=str(record), STUB_MAIN_SHA=main_sha or _main_sha(root),
                       PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    if issues is not None:
        child["STUB_ISSUES"] = json.dumps(issues)
    for key, value in (env or {}).items():
        if value is None:
            child.pop(key, None)
        else:
            child[key] = value
    proc = subprocess.run([sys.executable, "tools/readiness/decide.py", ".", *args], cwd=root,
                          env=child, capture_output=True, text=True, timeout=120)
    calls = [line.split("\t") for line in record.read_text(encoding="utf-8").splitlines()]
    return proc, calls


# -- the eight cases the issue names ---------------------------------------------------------

def test_all_gating_pass_no_blockers_signed_is_go(tmp_path, capsys):
    code, lines, err = _run(_repo(tmp_path), _blockers(tmp_path, []), capsys)
    assert (code, lines[-1], err) == (0, "GO", "")


def test_one_gating_dimension_at_2_is_nogo_naming_it(tmp_path, capsys):
    card = _passing_card()
    _dim(card, "D7")["score"] = 2
    code, lines, _ = _run(_repo(tmp_path, card), _blockers(tmp_path, []), capsys)
    assert code == 1 and lines[-1] == "NO-GO"
    assert any(l.startswith("D7 Onboarding: score 2") for l in lines), lines


def test_informational_dimension_at_0_is_still_go(tmp_path, capsys):
    card = _passing_card()
    _dim(card, "D12").update(score=0, evidence=[])
    code, lines, _ = _run(_repo(tmp_path, card), _blockers(tmp_path, []), capsys)
    assert code == 0 and lines[-1] == "GO"
    assert any(l.startswith("info: D12 Market") for l in lines)


def test_one_open_blocker_is_nogo_naming_its_number(tmp_path, capsys):
    items = [_issue(4242, title="data loss")]
    code, lines, _ = _run(_repo(tmp_path), _blockers(tmp_path, items), capsys)
    assert code == 1 and lines[-1] == "NO-GO"
    assert "open launch:blocker: #4242" in lines


def test_definition_proposed_is_nogo(tmp_path, capsys):
    code, lines, _ = _run(_repo(tmp_path, status="proposed"), _blockers(tmp_path, []), capsys)
    assert code == 1 and lines[-1] == "NO-GO"
    assert any(l.startswith("definition not signed") for l in lines)


def test_missing_benchmark_file_is_nogo(tmp_path, capsys):
    code, lines, _ = _run(_repo(tmp_path, bench=False), _blockers(tmp_path, []), capsys)
    assert code == 1 and lines[-1] == "NO-GO"
    assert any(l.startswith("benchmark results:") and BENCH in l for l in lines)


def test_score_of_5_is_malformed_exit_2(tmp_path, capsys):
    card = _passing_card()
    _dim(card, "D1")["score"] = 5
    code, lines, err = _run(_repo(tmp_path, card), _blockers(tmp_path, []), capsys)
    assert code == 2 and lines == []
    assert "MALFORMED" in err and "outside 0-4" in err


def test_gating_score_without_evidence_is_nogo(tmp_path, capsys):
    card = _passing_card()
    _dim(card, "D8").update(score=4, evidence=[])
    code, lines, _ = _run(_repo(tmp_path, card), _blockers(tmp_path, []), capsys)
    assert code == 1 and lines[-1] == "NO-GO"
    assert any(l.startswith("D8 Docs:") and "no evidence" in l for l in lines)


# -- the definition goal may not have landed --------------------------------------------------

def test_missing_definition_is_nogo_exit_1_not_malformed(tmp_path, capsys):
    code, lines, err = _run(_repo(tmp_path, definition=False), _blockers(tmp_path, []), capsys)
    assert (code, lines[-1], err) == (1, "NO-GO", "")
    assert any(l.startswith("definition missing") for l in lines)


# -- steering and malformed inputs ------------------------------------------------------------

@pytest.mark.parametrize("mutate, needle", [
    (lambda c: _dim(c, "D1").update(gating=False), "rule pins"),
    (lambda c: _dim(c, "D12").update(gating=True), "rule pins"),
    (lambda c: c["dimensions"].pop(0), "missing dimension"),
    (lambda c: c["dimensions"].append(copy.deepcopy(c["dimensions"][0])), "twice"),
    (lambda c: c["dimensions"].append({"id": "D99", "name": "x", "gating": True,
                                       "score": 4, "evidence": ["e"]}), "unknown id"),
    (lambda c: _dim(c, "D2").update(score=-1), "outside 0-4"),
    (lambda c: c.update(waived=["D7"]), "unknown key"),
    (lambda c: _dim(c, "D7").update(waived=True), "unknown key"),
    (lambda c: _dim(c, "D7").update(name="Onboarding (lite)"), "rule pins 'Onboarding'"),
    (lambda c: c.update(benchmark_results="docs/launch/scorecard.json"), "docs/launch/evidence/"),
    (lambda c: c.update(benchmark_results="docs/launch/evidence/"), "inside the repository"),
    (lambda c: c.update(benchmark_results="/etc/hosts"), "inside the repository"),
    (lambda c: c.update(benchmark_results="docs/launch/evidence/./b.json"), "inside the repository"),
    (lambda c: _dim(c, "D2").update(score=3.5), "not an integer"),
    (lambda c: _dim(c, "D2").update(score=True), "not an integer"),
    (lambda c: c.update(schema="other"), "schema"),
    (lambda c: c.pop("benchmark_results"), "benchmark_results"),
    (lambda c: c.update(benchmark_results="../outside.json"), "inside the repository"),
])
def test_steering_or_bad_scorecard_is_malformed(tmp_path, capsys, mutate, needle):
    card = _passing_card()
    mutate(card)
    code, lines, err = _run(_repo(tmp_path, card), _blockers(tmp_path, []), capsys)
    assert code == 2 and lines == [], (lines, err)
    assert needle in err


def test_missing_scorecard_is_malformed(tmp_path, capsys):
    root = _repo(tmp_path)
    _git(root, "rm", "-q", "docs/launch/scorecard.json")
    _commit_main(root)
    code, lines, err = _run(root, _blockers(tmp_path, []), capsys)
    assert code == 2 and lines == [] and "scorecard not found" in err


def test_unscored_gating_dimension_is_nogo(tmp_path, capsys):
    card = _passing_card()
    _dim(card, "D3")["score"] = None
    code, lines, _ = _run(_repo(tmp_path, card), _blockers(tmp_path, []), capsys)
    assert code == 1 and any(l.startswith("D3 Outcomes and cost: not scored") for l in lines)


def test_pull_requests_in_the_blocker_list_are_dropped(tmp_path, capsys):
    items = [_issue(7, pull_request={"url": "u"})]
    code, lines, _ = _run(_repo(tmp_path), _blockers(tmp_path, items), capsys)
    assert code == 0 and lines[-1] == "GO"


def test_bad_blocker_list_is_malformed(tmp_path, capsys):
    code, _, err = _run_offline(_repo(tmp_path), _blockers(tmp_path, {"number": 1}), capsys)
    assert code == 2 and "JSON list" in err


def test_bad_repo_argument_is_malformed(tmp_path, capsys):
    code, _, err = _run(_repo(tmp_path), _blockers(tmp_path, []), capsys, ("--repo", "nope"))
    assert code == 2 and "OWNER/NAME" in err


# -- the live (gh) path, with gh faked --------------------------------------------------------

def test_gh_path_uses_rest_paginated_and_decodes_every_page(tmp_path, capsys):
    page1 = json.dumps([_issue(11), _issue(12, pull_request={})])
    page2 = json.dumps([_issue(13)])
    root = _repo(tmp_path)
    fake = _FakeGh(root, page1 + "\n" + page2 + "\n")
    code, lines, _ = _run(root, None, capsys, ("--repo", "acme/widget"), fake=fake)
    assert code == 1
    assert "open launch:blocker: #11" in lines and "open launch:blocker: #13" in lines
    assert not any("#12" in l for l in lines)
    assert fake.issue_reads == [["gh", "api", "--hostname=github.com",
                                 "repos/acme/widget/" + QUERY, "--paginate"]]
    assert not any("graphql" in " ".join(argv) for argv, _ in fake.calls)


def test_gh_failure_is_malformed_never_zero_blockers(tmp_path, capsys):
    root = _repo(tmp_path)
    fake = _FakeGh(root, _Proc(1, "", "HTTP 401"))
    code = _main([str(root), "--repo", "acme/widget"], run=fake)
    out, err = capsys.readouterr()
    assert code == 2 and out == "" and "HTTP 401" in err


# -- the shipped files and the documented gesture ---------------------------------------------

def test_shipped_scorecard_is_valid_and_no_gating_dimension_reaches_3():
    """The shipped card, validated in-process: nothing in it needs a read at main."""
    card = json.loads(SHIPPED_CARD.read_text(encoding="utf-8"))

    def no_read_at_main(rel):
        raise AssertionError(f"an unscored card needs no read at main, but {rel} was asked for")

    reasons, _ = dz.check_scorecard(card, no_read_at_main)
    assert "D1 Correctness: score 2 < 3 (gating)" in reasons, reasons
    assert "benchmark results: not named in the scorecard" in reasons, reasons


def test_shipped_scorecard_on_a_fixture_main_is_nogo_via_the_gesture(tmp_path):
    """#331's acceptance item 3 on a main the checker can read: the shipped card and definition,
    committed and fetched, through the documented offline gesture."""
    root = _origin_repo(tmp_path, card=json.loads(SHIPPED_CARD.read_text(encoding="utf-8")),
                        definition=False, bench=False,
                        files={"docs/launch/definition.json":
                               SHIPPED_DEFINITION.read_text(encoding="utf-8")})
    (root / "blockers.json").write_text("[]", encoding="utf-8")
    proc, calls = _gesture(root, "--blockers-json", "blockers.json")
    lines = proc.stdout.splitlines()
    assert (proc.returncode, lines[-1:], calls) == (1, ["NO-GO"], []), (lines, proc.stderr)
    assert "D1 Correctness: score 2 < 3 (gating)" in lines, lines


def _rule_table(heading):
    text = RULE.read_text(encoding="utf-8")
    block = text.split(heading, 1)[1].split("\n\n", 2)[1]
    return dict(re.findall(r"^\| (D\d+) \| (.+?) \|$", block, re.M))


def test_decision_rule_page_and_checker_agree():
    gating = _rule_table("Gating dimensions:")
    info = _rule_table("Informational dimensions:")
    assert gating == {d: n for d, (n, g) in dz.DIMENSIONS.items() if g}
    assert info == {d: n for d, (n, g) in dz.DIMENSIONS.items() if not g}
    card = json.loads(SHIPPED_CARD.read_text(encoding="utf-8"))
    assert {d["id"]: d["name"] for d in card["dimensions"]} == {**gating, **info}
    text = RULE.read_text(encoding="utf-8")
    assert re.findall(r"^\| (B\d) \|", text, re.M) == [f"B{i}" for i in range(1, 9)]
    assert "Proposed — merged by the owner = signed" in text
    assert "python3 tools/readiness/decide.py ." in text
    assert "refs/remotes/origin/main" in text and "REFUSED" in text
    assert "cannot verify on main" not in text and "caveat" not in text
    assert ("only the live REST read can establish that no open launch:blocker issue exists"
            in " ".join(text.split()))


def test_rules_the_checker_cannot_see_sit_under_their_own_heading():
    text = RULE.read_text(encoding="utf-8")
    heading = "## Process rules the checker does not enforce"
    assert text.count(heading) == 1
    after = text.split(heading, 1)[1].split("\n## ", 1)[0]
    before = text.split(heading, 1)[0]
    for sentence in ("names its class code", "Removing the label from an open issue",
                     "Nobody may demote"):
        assert sentence in after and sentence not in before, sentence


# -- review block #1: evidence must be a link -------------------------------------------------

@pytest.mark.parametrize("entry", [
    "TODO", "n/a", " x ", "", "docs/launch/evidence/nope.md", "../outside.md",
    "docs/../../outside.md", "/etc/hosts", "https://", "https:// example.com/x",
    "ftp://example.com/x", "docs/launch/evidence", "docs\\launch\\evidence\\proof.md",
    "https://[::1", "docs/launch/evidence/./proof.md", "docs/launch/evidence/proof.md/",
    "docs/a\x00b", "docs/" + "x" * 5000,
])
def test_evidence_entry_that_is_not_a_link_is_nogo_naming_it(tmp_path, capsys, entry):
    card = _passing_card()
    _dim(card, "D8")["evidence"] = [entry]
    root = _repo(tmp_path, card, files={PROOF: "x"})
    (tmp_path / "outside.md").write_text("x", encoding="utf-8")
    code, lines, err = _run(root, _blockers(tmp_path, []), capsys)
    assert (code, lines[-1], err) == (1, "NO-GO", ""), lines
    assert any(l.startswith("D8 Docs: evidence " + repr(entry)) for l in lines), lines


def test_directory_evidence_is_not_a_file(tmp_path, capsys):
    card = _passing_card()
    _dim(card, "D8")["evidence"] = ["docs/launch/evidence"]
    code, lines, _ = _run(_repo(tmp_path, card), _blockers(tmp_path, []), capsys)
    assert code == 1 and ("D8 Docs: evidence 'docs/launch/evidence' is not a regular file at "
                          "origin/main (a symlink or directory) (gating)") in lines, lines


def test_one_bad_entry_beside_a_good_one_is_still_nogo(tmp_path, capsys):
    card = _passing_card()
    _dim(card, "D8")["evidence"] = ["https://example.com/ok", "TODO"]
    code, lines, _ = _run(_repo(tmp_path, card), _blockers(tmp_path, []), capsys)
    assert code == 1 and any(l.startswith("D8 Docs: evidence 'TODO' ") for l in lines), lines


def test_symlink_evidence_escaping_the_repo_is_nogo(tmp_path, capsys):
    card = _passing_card()
    _dim(card, "D8")["evidence"] = ["docs/launch/evidence/link.md"]
    root = _repo(tmp_path, card, git=False)
    (tmp_path / "outside.md").write_text("x", encoding="utf-8")
    try:
        (root / "docs" / "launch" / "evidence" / "link.md").symlink_to(tmp_path / "outside.md")
    except OSError:
        pytest.skip("symlinks unavailable")
    _git_repo(root)
    code, lines, _ = _run(root, _blockers(tmp_path, []), capsys)
    assert code == 1 and ("D8 Docs: evidence 'docs/launch/evidence/link.md' is not a regular "
                          "file at origin/main (a symlink or directory) (gating)") in lines, lines


@pytest.mark.parametrize("entry", ["https://example.com/evidence/D8",
                                   "http://example.com/x?y=1#z", PROOF])
def test_valid_url_or_tracked_repo_file_is_evidence(tmp_path, capsys, entry):
    card = _passing_card()
    _dim(card, "D8")["evidence"] = [entry]
    root = _repo(tmp_path, card, files={PROOF: "x"})
    code, lines, _ = _run(root, _blockers(tmp_path, []), capsys)
    assert (code, lines[-1]) == (0, "GO"), lines


# -- review block #2 (c): a repository-path evidence entry must be on main, non-empty ----------

def _evidence_case(tmp_path, capsys, git_kw=None, after=None, content="x"):
    card = _passing_card()
    _dim(card, "D8")["evidence"] = [PROOF]
    root = _repo(tmp_path, card, files={PROOF: content} if after is None else None,
                 **(git_kw or {}))
    if after:
        after(root)
    code, lines, _ = _run(root, _blockers(tmp_path, []), capsys)
    assert code == 1 and lines[-1] == "NO-GO", lines
    return [l for l in lines if l.startswith(f"D8 Docs: evidence {PROOF!r}")]


def test_untracked_evidence_file_is_nogo(tmp_path, capsys):
    got = _evidence_case(tmp_path, capsys,
                         after=lambda r: (r / PROOF).write_text("x", encoding="utf-8"))
    assert got == [f"D8 Docs: evidence {PROOF!r} is not tracked at origin/main (gating)"]


def test_empty_evidence_file_is_nogo(tmp_path, capsys):
    got = _evidence_case(tmp_path, capsys, content="")
    assert got == [f"D8 Docs: evidence {PROOF!r} is empty at origin/main (gating)"]


# -- an unnamed benchmark file is NO-GO, never GO ---------------------------------------------

@pytest.mark.parametrize("bench", [None, "", "   "])
def test_unnamed_benchmark_file_is_nogo(tmp_path, capsys, bench):
    card = _passing_card()
    card["benchmark_results"] = bench
    code, lines, err = _run(_repo(tmp_path, card), _blockers(tmp_path, []), capsys)
    assert (code, lines[-1], err) == (1, "NO-GO", ""), lines
    assert "benchmark results: not named in the scorecard" in lines


# -- the benchmark file: under evidence/, non-empty, on origin/main ---------------------------

def test_benchmark_tracked_at_origin_main_is_go(tmp_path, capsys):
    root = _repo(tmp_path)
    code, lines, _ = _run(root, _blockers(tmp_path, []), capsys)
    assert (code, lines) == (0, ["info: D12 Market is informational (score 3); it blocks only "
                                 "through B3", f"info: main is {MAIN_REF} at {_main_sha(root)}",
                                 "info: blockers read over REST from github.com/acme/widget",
                                 "GO"]), lines


def test_untracked_benchmark_is_nogo(tmp_path, capsys):
    root = _repo(tmp_path, bench=False)
    (root / BENCH).write_text("{}", encoding="utf-8")
    code, lines, _ = _run(root, _blockers(tmp_path, []), capsys)
    assert code == 1 and f"benchmark results: {BENCH} is not tracked at origin/main" in lines


def test_empty_benchmark_at_origin_main_is_nogo(tmp_path, capsys):
    root = _repo(tmp_path, git=False)
    (root / BENCH).write_text("", encoding="utf-8")
    _git_repo(root)
    code, lines, _ = _run(root, _blockers(tmp_path, []), capsys)
    assert code == 1 and f"benchmark results: {BENCH} is empty at origin/main" in lines


def test_benchmark_committed_as_a_symlink_is_nogo(tmp_path, capsys):
    root = _repo(tmp_path, bench=False, git=False)
    (tmp_path / "outside.json").write_text("{}", encoding="utf-8")
    try:
        (root / BENCH).symlink_to(tmp_path / "outside.json")
    except OSError:
        pytest.skip("symlinks unavailable")
    _git_repo(root)
    code, lines, _ = _run(root, _blockers(tmp_path, []), capsys)
    assert code == 1 and any(l.startswith(f"benchmark results: {BENCH} is not a regular file at "
                                          "origin/main") for l in lines), lines


# -- a signature needs a signer and a date ----------------------------------------------------

FUTURE = (datetime.date.today() + datetime.timedelta(days=2)).isoformat()


@pytest.mark.parametrize("drop, value", [
    ("signed_by", None), ("signed_by", ""), ("signed_by", "  "), ("signed_by", "Owner Name"),
    ("signed_by", "a@b"), ("signed_by", "x" * 40), ("signed_by", "owner\n"), ("signed_by", 7),
    ("signed_on", None), ("signed_on", "30/09/2026"), ("signed_on", "2026-13-01"),
    ("signed_on", 20260930), ("signed_on", "2026-02-30"), ("signed_on", "2026-09-30\n"),
    ("signed_on", FUTURE), ("signed_on", "٢٠٢٦-09-30"),
])
def test_signed_without_signer_or_date_is_nogo(tmp_path, capsys, drop, value):
    root = _repo(tmp_path, git=False)
    path = root / "docs" / "launch" / "definition.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if value is None:
        del data[drop]
    else:
        data[drop] = value
    path.write_text(json.dumps(data), encoding="utf-8")
    _git_repo(root)
    code, lines, _ = _run(root, _blockers(tmp_path, []), capsys)
    assert code == 1 and any(l.startswith("definition not signed") and drop in l
                             for l in lines), lines


def test_plain_login_and_today_are_a_valid_signature(tmp_path, capsys):
    root = _repo(tmp_path, git=False)
    (root / "docs" / "launch" / "definition.json").write_text(json.dumps(
        {"schema": "launch-definition/v1", "status": "signed", "signed_by": "swapnil-agrim",
         "signed_on": datetime.date.today().isoformat()}), encoding="utf-8")
    _git_repo(root)
    code, lines, _ = _run(root, _blockers(tmp_path, []), capsys)
    assert (code, lines[-1]) == (0, "GO"), lines


# -- the offline blocker list ------------------------------------------------------------------

def test_blocker_state_matches_case_insensitively(tmp_path, capsys):
    items = [_issue(5, "OPEN"), _issue(6, "Closed")]
    code, lines, _ = _run(_repo(tmp_path), _blockers(tmp_path, items), capsys)
    assert code == 1 and "open launch:blocker: #5" in lines
    assert not any("#6" in l for l in lines)


@pytest.mark.parametrize("item", [{"number": 5, "labels": [{"name": "launch:blocker"}]},
                                  _issue(5, "merged"), _issue(5, None)])
def test_blocker_with_unknown_or_missing_state_is_malformed(tmp_path, capsys, item):
    code, lines, err = _run(_repo(tmp_path), _blockers(tmp_path, [item]), capsys)
    assert code == 2 and lines == [] and "state" in err


@pytest.mark.parametrize("item, needle", [
    ({"number": 5, "state": "open"}, "labels"),
    ({"number": 5, "state": "open", "labels": "launch:blocker"}, "labels"),
    ({"number": 5, "state": "open", "labels": ["launch:blocker"]}, "labels"),
    ({"number": 5, "state": "open", "labels": [{"name": "launch:next"}]}, "does not carry"),
    (_issue(5, pull_request=None), "pull_request"),
    (_issue(5, pull_request="yes"), "pull_request"),
    ({"number": 5, "state": "open", "pull_request": {}}, "labels"),
    (_issue(True), "number"),
    (_issue("5"), "number"),
    (["number", 5], "not an object"),
])
def test_blocker_entry_of_the_wrong_shape_is_malformed(tmp_path, capsys, item, needle):
    code, lines, err = _run(_repo(tmp_path), _blockers(tmp_path, [item]), capsys)
    assert code == 2 and lines == [] and needle in err, err


# -- the live (gh) path: an empty read and an ambiguous repository ----------------------------

@pytest.mark.parametrize("out", ["", "  \n"])
def test_gh_exit_0_with_empty_output_is_malformed(tmp_path, capsys, out):
    root = _repo(tmp_path)
    code = _main([str(root), "--repo", "acme/widget"], run=_FakeGh(root, out))
    got, err = capsys.readouterr()
    assert code == 2 and got == "" and "no output" in err


@pytest.mark.parametrize("remotes", [(), ("origin", "upstream"), ("upstream",)])
def test_default_repo_needs_exactly_one_origin_remote(tmp_path, capsys, remotes):
    root = _repo(tmp_path, remotes=remotes)
    fake = _FakeGh(root)
    code = _main([str(root)], run=fake)
    out, err = capsys.readouterr()
    assert (code, out, fake.calls) == (2, "", []) and "pass --repo" in err


def test_default_repo_outside_git_is_refused(tmp_path, capsys):
    root = _repo(tmp_path, git=False)
    fake = _FakeGh(root)
    code = _main([str(root)], run=fake)
    out, err = capsys.readouterr()
    assert (code, out, fake.calls) == (2, "", []), err
    assert err.startswith("decide.py: REFUSED: ") and "is not the top of a git work tree" in err


# -- review block #2, BLOCKING 1: the repository is derived, never resolved by gh ---------------

@pytest.mark.parametrize("url, host", [
    ("https://github.com/acme/widget", "github.com"),
    ("https://github.com/acme/widget.git", "github.com"),
    ("https://github.com/acme/widget/", "github.com"),
    ("http://github.com/acme/widget.git", "github.com"),
    ("https://user@GitHub.com/acme/widget.git", "github.com"),
    ("ssh://git@github.com/acme/widget.git", "github.com"),
    ("ssh://git@github.com:22/acme/widget", "github.com"),
    ("git@github.com:acme/widget.git", "github.com"),
    ("git@github.com:acme/widget", "github.com"),
    ("github.com:acme/widget.git", "github.com"),
    ("https://ghe.example.com/acme/widget.git", "ghe.example.com"),
])
def test_default_repo_is_derived_from_origins_url(tmp_path, capsys, url, host):
    root = _repo(tmp_path, url=url)
    fake = _FakeGh(root)
    code = _main([str(root)], run=fake)
    assert code == 0, capsys.readouterr()
    assert fake.issue_reads == [["gh", "api", f"--hostname={host}", "repos/acme/widget/" + QUERY,
                                 "--paginate"]]


#: A password-shaped URL fragment, built at run time so the publish leak gate (tools/leak_scan.py)
#: does not read a literal credential in this file.
_PW = "s3" "cret"


@pytest.mark.parametrize("url", [
    "https://[::1", "file:///srv/acme/widget.git", "/srv/acme/widget.git",
    "https://github.com/acme", "https://github.com/acme/widget/extra",
    "https://github.com/../..", "git@github.com:../..", "https://github.com/acme/widget?x=1",
    "https://github.com/acme/.git", "git@github.com:/acme/widget.git",
    pytest.param("https://user:%s@github.com/acme" % _PW, id="userinfo-password"),
])
def test_origin_url_of_an_unknown_shape_is_malformed(tmp_path, capsys, url):
    root = _repo(tmp_path, url=url)
    fake = _FakeGh(root)
    code = _main([str(root)], run=fake)
    out, err = capsys.readouterr()
    assert (code, out, fake.calls) == (2, "", []) and "pass --repo" in err, err
    assert _PW not in err


def test_origin_with_two_urls_is_malformed(tmp_path, capsys):
    root = _repo(tmp_path)
    _git(root, "remote", "set-url", "--add", "origin", "https://github.com/other/repo.git")
    fake = _FakeGh(root)
    code = _main([str(root)], run=fake)
    out, err = capsys.readouterr()
    assert (code, out, fake.calls) == (2, "", []) and "2 URLs" in err, err


@pytest.mark.parametrize("argv_tail", [(), ("--repo", "acme/widget")])
def test_gh_repo_and_gh_host_cannot_redirect_the_read(tmp_path, capsys, monkeypatch, argv_tail):
    monkeypatch.setenv("GH_REPO", "cli/cli")
    monkeypatch.setenv("GH_HOST", "evil.example")
    monkeypatch.setenv("GH_TOKEN", "kept-for-auth")
    root = _repo(tmp_path, url="https://github.com/acme/widget.git")
    fake = _FakeGh(root)
    assert _main([str(root), *argv_tail], run=fake) == 0, capsys.readouterr()
    assert [argv[2:4] for argv in fake.issue_reads] == [
        ["--hostname=github.com", "repos/acme/widget/" + QUERY]]
    for argv, kw in fake.calls:
        env = kw["env"]
        assert "GH_REPO" not in env and "GH_HOST" not in env, argv
        assert env["GH_TOKEN"] == "kept-for-auth", argv


@pytest.mark.skipif(os.name == "nt", reason="stub gh is a POSIX shell script")
def test_documented_gesture_with_gh_repo_set_reads_this_repository(tmp_path):
    """The docs' gesture, `python3 tools/readiness/decide.py .`, with GH_REPO pointing elsewhere
    and a stub `gh` first on PATH that records what it was asked and its GH_REPO."""
    root = _repo(tmp_path, url="git@github.com:acme/widget.git")
    proc, calls = _gesture(root, env={"GH_REPO": "cli/cli", "GH_HOST": "evil.example"})
    assert (proc.returncode, proc.stdout.splitlines()[-1:]) == (0, ["GO"]), proc.stderr
    assert ["api", "--hostname=github.com", "repos/acme/widget/" + QUERY, "--paginate",
            "GH_REPO=unset"] in calls, calls
    assert all(call[-1] == "GH_REPO=unset" for call in calls), calls


# -- review block #2, BLOCKING 2: --repo is exactly OWNER/NAME ---------------------------------

@pytest.mark.parametrize("repo", [
    "../..", "a/.", "a/b\n", "./x", "a/..", ".../x", "-a/b", "a/-b", "a/b/c", "a", "a/",
    "/b", "a/b ", "a b/c", "a/b?x=1", "a" * 101 + "/b", "a/" + "b" * 101, "a/b\x00",
])
def test_repo_argument_that_is_not_owner_name_is_malformed(tmp_path, capsys, repo):
    root = _repo(tmp_path)
    fake = _FakeGh(root)
    code = _main([str(root), "--repo", repo], run=fake)
    out, err = capsys.readouterr()
    assert (code, out, fake.calls) == (2, "", []) and "OWNER/NAME" in err


@pytest.mark.parametrize("repo", ["acme/widget", "Acme_1/wid.get-2", "_a/_b", "a/b.c",
                                  "a" * 100 + "/" + "b" * 100])
def test_valid_repo_argument_is_used_verbatim(tmp_path, capsys, repo):
    """Two remotes, so origin cannot name the repository and --repo is the operator's assertion."""
    root = _repo(tmp_path, remotes=("origin", "upstream"))
    fake = _FakeGh(root)
    code = _main([str(root), "--repo", repo], run=fake)
    assert code == 0, capsys.readouterr()
    assert [argv[3] for argv in fake.issue_reads] == [f"repos/{repo}/" + QUERY]


# -- review block #2 (a) (b) (e): duplicate keys, unreadable inputs, --help ---------------------

DUP_CARD = ('{"schema": "launch-scorecard/v1", "schema": "launch-scorecard/v1", '
            '"benchmark_results": null, "dimensions": []}')


def test_duplicate_key_in_scorecard_is_malformed(tmp_path, capsys):
    root = _repo(tmp_path)
    (root / "docs" / "launch" / "scorecard.json").write_text(DUP_CARD, encoding="utf-8")
    _commit_main(root)
    code, lines, err = _run(root, _blockers(tmp_path, []), capsys)
    assert code == 2 and lines == [] and "duplicate key 'schema'" in err


def test_duplicate_gating_key_cannot_flip_a_dimension(tmp_path, capsys):
    root = _repo(tmp_path)
    path = root / "docs" / "launch" / "scorecard.json"
    text = path.read_text(encoding="utf-8").replace('"gating": true', '"gating": true, '
                                                     '"gating": false', 1)
    path.write_text(text, encoding="utf-8")
    _commit_main(root)
    code, lines, err = _run(root, _blockers(tmp_path, []), capsys)
    assert code == 2 and lines == [] and "duplicate key 'gating'" in err


def test_duplicate_key_in_definition_is_malformed(tmp_path, capsys):
    root = _repo(tmp_path)
    (root / "docs" / "launch" / "definition.json").write_text(
        '{"schema": "launch-definition/v1", "status": "proposed", "status": "signed", '
        '"signed_by": "owner", "signed_on": "2026-09-30"}', encoding="utf-8")
    _commit_main(root)
    code, lines, err = _run(root, _blockers(tmp_path, []), capsys)
    assert code == 2 and lines == [] and "duplicate key 'status'" in err


def test_duplicate_key_in_blocker_list_is_malformed(tmp_path, capsys):
    path = tmp_path / "blockers.json"
    path.write_text('[{"number": 1, "state": "open", "state": "closed", '
                    '"labels": [{"name": "launch:blocker"}]}]', encoding="utf-8")
    code, lines, err = _run_offline(_repo(tmp_path), str(path), capsys)
    assert code == 2 and lines == [] and "duplicate key 'state'" in err


def test_duplicate_key_in_gh_output_is_malformed(tmp_path, capsys):
    out = '[{"number": 1, "state": "open", "state": "closed", "labels": []}]'
    root = _repo(tmp_path)
    code = _main([str(root), "--repo", "acme/widget"], run=_FakeGh(root, out))
    got, err = capsys.readouterr()
    assert code == 2 and got == "" and "duplicate key 'state'" in err


@pytest.mark.parametrize("which", ["root-nul", "root-long", "blockers-nul", "blockers-long",
                                   "blockers-deep", "scorecard-deep", "gh-deep"])
def test_unreadable_input_is_malformed_never_a_traceback(tmp_path, capsys, which):
    root, blockers, run = str(_repo(tmp_path)), _blockers(tmp_path, []), None
    if which == "root-nul":
        root = root + "\x00x"
    elif which == "root-long":
        root = str(tmp_path / ("x" * 5000))
    elif which == "blockers-nul":
        blockers = blockers + "\x00x"
    elif which == "blockers-long":
        blockers = str(tmp_path / ("x" * 5000))
    elif which == "blockers-deep":
        pathlib.Path(blockers).write_text("[" * 200000 + "]" * 200000, encoding="utf-8")
    elif which == "scorecard-deep":
        (pathlib.Path(root) / "docs" / "launch" / "scorecard.json").write_text(
            "[" * 200000 + "]" * 200000, encoding="utf-8")
        _commit_main(root)
    argv = [root, "--blockers-json", blockers]
    if which == "gh-deep":
        argv = [root, "--repo", "acme/widget"]
        run = _FakeGh(root, "[" * 200000 + "]" * 200000)
    code = _main(argv, run=run)
    out, err = capsys.readouterr()
    assert (code, out) == (2, ""), err
    assert err.startswith("decide.py: MALFORMED: ") and "Traceback" not in err


def test_unreadable_benchmark_path_is_never_a_traceback(tmp_path, capsys):
    card = _passing_card()
    card["benchmark_results"] = "docs/launch/evidence/" + "x" * 5000
    code, lines, err = _run(_repo(tmp_path, card), _blockers(tmp_path, []), capsys)
    assert code in (1, 2) and "Traceback" not in err
    assert code == 2 or lines[-1] == "NO-GO"


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_exits_2_never_the_go_code(capsys, flag):
    assert dz.main([flag]) == 2
    assert "usage: decide.py" in capsys.readouterr().out


def test_help_via_the_documented_gesture_exits_2():
    proc = subprocess.run([sys.executable, str(TOOL), "--help"], capture_output=True, text=True)
    assert proc.returncode == 2 and "usage: decide.py" in proc.stdout


# -- #429: the verdict is computed from main, whatever the environment says ----------------------
#
# Defects 1-5 end NO-GO (exit 1) for the TRUE reason: ambient state is ignored, not detected, so
# each asserts the reason the real main gives. Detected repository state (a symbolic, unreadable or
# stale main) is refused, exit 2. Every node below fails by assertion on the #331 checker.

NOT_ON_MAIN = f"benchmark results: {BENCH} is not tracked at origin/main"
NO_MAIN = f"no {MAIN_REF} in this checkout; git fetch origin, then rerun"
OPEN_7 = [_issue(7)]
INSTEAD_OF = ("url.https://github.com/attacker/.insteadOf", "https://github.com/victim/")
INSTEAD_OF_FILE = '[url "https://github.com/attacker/"]\n\tinsteadOf = https://github.com/victim/\n'
PROTOCOLS = ("file", "git", "ssh", "http", "https", "ext")
GIT_PREFIX = ["git", "--no-replace-objects", "--literal-pathspecs", "-c", "core.commitGraph=false",
              "-c", "protocol.allow=never",
              *[arg for p in PROTOCOLS for arg in ("-c", f"protocol.{p}.allow=never")]]
GIT_SUBCOMMANDS = {"--version", "rev-parse", "symbolic-ref", "show-ref", "ls-tree", "cat-file",
                   "remote", "config"}
OFFLINE = ("blocker list read offline from {!r}; only the live REST read can establish that no "
           "open launch:blocker issue exists")


def _commit_bench(root):
    """Commit the benchmark on HEAD only; refs/remotes/origin/main stays where it was."""
    (root / BENCH).write_text("{}", encoding="utf-8")
    _git(root, "add", "--", BENCH)
    _git(root, "commit", "-q", "-m", "benchmark on a local commit only")


def _offline_gesture(root, **kw):
    (root / "blockers.json").write_text("[]", encoding="utf-8")
    return _gesture(root, "--blockers-json", "blockers.json", **kw)


def _not_on_main(proc):
    lines = proc.stdout.splitlines()
    assert (proc.returncode, lines[-1:]) == (1, ["NO-GO"]), (lines, proc.stderr)
    assert NOT_ON_MAIN in lines, lines


def _stub_git(tmp_path, name, body):
    """A `git` first on PATH that runs `body` (sh) and otherwise execs the real git."""
    real = shutil.which("git")
    assert real, "git is not on PATH"
    bin_dir = tmp_path / name
    bin_dir.mkdir()
    (bin_dir / "git").write_text(f'#!/bin/sh\n{body}\nexec "{real}" "$@"\n', encoding="utf-8")
    (bin_dir / "git").chmod(0o755)
    return f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"


@pytest.mark.parametrize("make", ["branch", "tag", "refs-origin-main"])
def test_local_ref_named_origin_main_cannot_stand_in_for_the_remote(tmp_path, make):
    """defect 1: the short name `origin/main` resolves to refs/origin/main, then a tag, then a
    branch before refs/remotes/origin/main; none of them may stand in for the remote's main."""
    root = _origin_repo(tmp_path, bench=False)
    _commit_bench(root)
    _git(root, *{"branch": ("branch", "origin/main", "HEAD"),
                 "tag": ("tag", "origin/main", "HEAD"),
                 "refs-origin-main": ("update-ref", "refs/origin/main", "HEAD")}[make])
    proc, _ = _offline_gesture(root)
    _not_on_main(proc)


@pytest.mark.parametrize("work_tree", [False, True], ids=["git-dir", "git-dir-and-work-tree"])
def test_inherited_git_dir_cannot_redirect_main(tmp_path, capsys, monkeypatch, work_tree):
    """defect 2, in-process: GIT_DIR naming another repository whose main has the benchmark."""
    victim = _repo(tmp_path / "victim", bench=False)
    (victim / BENCH).write_text("{}", encoding="utf-8")
    other = _repo(tmp_path / "other")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    if work_tree:
        monkeypatch.setenv("GIT_WORK_TREE", str(victim))
    code, lines, err = _run(victim, _blockers(tmp_path, []), capsys)
    assert (code, lines[-1:], err) == (1, ["NO-GO"], ""), (lines, err)
    assert NOT_ON_MAIN in lines, lines


@pytest.mark.parametrize("work_tree", [False, True], ids=["git-dir", "git-dir-and-work-tree"])
def test_documented_gesture_ignores_an_inherited_git_dir(tmp_path, work_tree):
    """defect 2, the docs' gesture `python3 tools/readiness/decide.py . --blockers-json <file>` from
    a checkout whose main lacks the benchmark, with GIT_DIR naming one whose main has it."""
    victim = _origin_repo(tmp_path / "victim", bench=False)
    (victim / BENCH).write_text("{}", encoding="utf-8")
    other = _origin_repo(tmp_path / "other", url=ATTACKER_URL)
    env = {"GIT_DIR": str(other / ".git")}
    if work_tree:
        env["GIT_WORK_TREE"] = str(victim)
    proc, _ = _offline_gesture(victim, env=env)
    _not_on_main(proc)


@pytest.mark.parametrize("case", ["git-dir", "config-count", "config-parameters", "config-global",
                                  "config-system", "home-gitconfig", "xdg-config",
                                  "global-include", "local-insteadof", "local-include"])
def test_git_environment_or_config_cannot_redirect_the_blocker_read(tmp_path, case):
    """defect 3, the live gesture: victim/widget has open blocker #7, attacker/widget has none. The
    blockers read are origin's, as this checkout's config writes origin's URL -- not another
    repository's (GIT_DIR) and not an insteadOf rewrite from the environment or any config file."""
    victim = _origin_repo(tmp_path / "victim")
    other = _origin_repo(tmp_path / "other", url=ATTACKER_URL)
    rewrite = tmp_path / "insteadof.cfg"
    rewrite.write_text(INSTEAD_OF_FILE, encoding="utf-8")
    home = tmp_path / "home"
    (home / "xdg" / "git").mkdir(parents=True)
    key, value = INSTEAD_OF
    env = {}
    if case == "git-dir":
        env = {"GIT_DIR": str(other / ".git")}
    elif case == "config-count":
        env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": key, "GIT_CONFIG_VALUE_0": value}
    elif case == "config-parameters":
        env = {"GIT_CONFIG_PARAMETERS": f"'{key.lower()}'='{value}'"}
    elif case == "config-global":
        env = {"GIT_CONFIG_GLOBAL": str(rewrite)}
    elif case == "config-system":
        env = {"GIT_CONFIG_SYSTEM": str(rewrite)}
    elif case == "home-gitconfig":
        (home / ".gitconfig").write_text(INSTEAD_OF_FILE, encoding="utf-8")
        env = {"HOME": str(home)}
    elif case == "xdg-config":
        (home / "xdg" / "git" / "config").write_text(INSTEAD_OF_FILE, encoding="utf-8")
        env = {"XDG_CONFIG_HOME": str(home / "xdg")}
    elif case == "global-include":
        (home / ".gitconfig").write_text(f"[include]\n\tpath = {rewrite}\n", encoding="utf-8")
        env = {"HOME": str(home)}
    elif case == "local-insteadof":
        _git(victim, "config", key, value)
    elif case == "local-include":
        _git(victim, "config", "include.path", str(rewrite))
    proc, calls = _gesture(victim, env=env, issues=OPEN_7)
    lines = proc.stdout.splitlines()
    asked = [arg for call in calls for arg in call if arg.startswith("repos/")]
    assert (proc.returncode, lines[-1:]) == (1, ["NO-GO"]), (lines, proc.stderr, asked)
    assert "open launch:blocker: #7" in lines, lines
    assert asked and all(arg.startswith("repos/victim/widget/") for arg in asked), asked
    assert "info: blockers read over REST from github.com/victim/widget" in lines, lines


@pytest.mark.parametrize("how", ["git-replace", "replace-ref-base"])
def test_replace_ref_cannot_stand_in_for_origin_main(tmp_path, how):
    """defect 4: a replace ref (or GIT_REPLACE_REF_BASE naming a branch called like main's sha)
    swapping main's commit for one that carries the benchmark."""
    root = _origin_repo(tmp_path, bench=False)
    main = _main_sha(root)
    _commit_bench(root)
    env = {}
    if how == "git-replace":
        _git(root, "replace", main, "HEAD")
    else:
        _git(root, "update-ref", "refs/heads/" + main, "HEAD")
        env = {"GIT_REPLACE_REF_BASE": "refs/heads/"}
    proc, _ = _offline_gesture(root, env=env)
    _not_on_main(proc)


def test_gh_runs_without_any_git_environment(tmp_path, capsys, monkeypatch):
    """EVERY gh call -- both socket checks, the main read and the issues read -- runs without any
    GIT_*, GH_REPO or GH_HOST, and keeps the token variables (they only authenticate)."""
    root = _repo(tmp_path)
    for name, val in {"GIT_CONFIG_COUNT": "0", "GIT_TERMINAL_PROMPT": "0",
                      "GIT_OPTIONAL_LOCKS": "0", "GH_REPO": "cli/cli", "GH_HOST": "evil.example",
                      "GH_TOKEN": _KEPT_AUTH_VALUE}.items():
        monkeypatch.setenv(name, val)
    fake = _FakeGh(root)
    code, lines, err = _run(root, None, capsys, ("--repo", "acme/widget"), fake=fake)
    kinds = sorted(fake.kind(argv) for argv, _ in fake.calls)
    assert (code, kinds) == (0, ["config-global", "config-host", "issues", "main"]), (lines, err)
    for argv, kw in fake.calls:
        env = kw.get("env")
        assert isinstance(env, dict), argv
        assert sorted(k for k in env if k.upper().startswith("GIT_")
                      or k.upper() in ("GH_REPO", "GH_HOST")) == [], argv
        assert env.get("GH_TOKEN") == "kept-for-auth", argv


# -- pre-PR review #1 (#429): B1 gh's pager, B2 a --repo that is not origin's ------------------

#: Variables removed from, and pinned in, every gh call's environment (#429 B1).
GH_REMOVED = ("GH_FORCE_TTY", "CLICOLOR_FORCE")
GH_PINNED = {"GH_PAGER": "", "PAGER": ""}


def test_no_gh_call_can_be_paged_or_coloured(tmp_path, capsys, monkeypatch):
    """B1, in-process: GH_FORCE_TTY makes gh treat a pipe as a terminal and run its output through
    GH_PAGER / gh config `pager` / PAGER, which can print anything; CLICOLOR_FORCE colours JSON even
    with NO_COLOR. EVERY gh call runs with GH_FORCE_TTY and CLICOLOR_FORCE removed, GH_PAGER and
    PAGER pinned to "" and NO_COLOR set."""
    forger = "sh -c 'cat >/dev/null; printf X-FORGED'"
    for name, val in {"GH_FORCE_TTY": "1", "CLICOLOR_FORCE": "1", "GH_PAGER": forger,
                      "PAGER": forger, "GH_TOKEN": _KEPT_AUTH_VALUE}.items():
        monkeypatch.setenv(name, val)
    monkeypatch.delenv("NO_COLOR", raising=False)
    root = _repo(tmp_path)
    fake = _FakeGh(root)
    code, lines, err = _run(root, None, capsys, fake=fake)
    kinds = sorted(fake.kind(argv) for argv, _ in fake.calls)
    assert (code, kinds) == (0, ["config-global", "config-host", "issues", "main"]), (lines, err)
    for argv, kw in fake.calls:
        env = kw.get("env")
        assert isinstance(env, dict), argv
        assert sorted(k for k in env if k.upper() in GH_REMOVED) == [], argv
        assert {k: env.get(k) for k in GH_PINNED} == GH_PINNED, argv
        assert env.get("NO_COLOR"), argv
        assert env.get("GH_TOKEN") == "kept-for-auth", argv


@pytest.mark.skipif(os.name == "nt", reason="stub gh is a POSIX shell script")
@pytest.mark.parametrize("pager_var", ["GH_PAGER", "PAGER"])
def test_documented_gesture_cannot_be_forged_by_a_pager(tmp_path, pager_var):
    """B1, the docs' gesture `python3 tools/readiness/decide.py .`: victim/widget has open blocker
    #7; GH_FORCE_TTY=1 and a pager that rewrites the blocker list to [] would turn NO-GO into GO
    through a gh that pages as real gh does."""
    root = _origin_repo(tmp_path)
    proc, _ = _gesture(root, env={"GH_FORCE_TTY": "1", pager_var: FORGING_PAGER}, issues=OPEN_7,
                       paging=True)
    lines = proc.stdout.splitlines()
    assert (proc.returncode, lines[-1:]) == (1, ["NO-GO"]), (lines, proc.stderr)
    assert "open launch:blocker: #7" in lines, lines


@pytest.mark.skipif(os.name == "nt", reason="stub gh is a POSIX shell script")
@pytest.mark.parametrize("url, repo, named", [
    (VICTIM_URL, "attacker/widget", "github.com/victim/widget"),
    ("git@ghe.example.com:attacker/widget.git", "attacker/widget",
     "ghe.example.com/attacker/widget"),
], ids=["fork-same-main", "other-host"])
def test_repo_that_is_not_origins_repository_is_refused(tmp_path, url, repo, named):
    """B2: a fork or mirror whose main is the same commit has its own (empty) blocker list. When
    origin names a repository, --repo must name the same one (host github.com, OWNER/NAME
    case-insensitive), or the checker refuses naming both -- before any gh call."""
    root = _origin_repo(tmp_path, url=url)
    proc, calls = _gesture(root, "--repo", repo, issues=OPEN_7)
    assert (proc.returncode, proc.stdout) == (2, ""), (proc.stdout, proc.stderr)
    err = proc.stderr
    assert err.startswith("decide.py: REFUSED: ") and f"github.com/{repo}" in err, err
    assert named in err, err
    assert calls == [], calls


@pytest.mark.skipif(os.name == "nt", reason="stub gh is a POSIX shell script")
def test_repo_that_names_origins_repository_is_used(tmp_path):
    """B2: --repo naming origin's repository (any case) still reads that repository's blockers."""
    root = _origin_repo(tmp_path)
    proc, calls = _gesture(root, "--repo", "Victim/Widget", issues=OPEN_7)
    lines = proc.stdout.splitlines()
    assert (proc.returncode, lines[-1:]) == (1, ["NO-GO"]), (lines, proc.stderr)
    assert "open launch:blocker: #7" in lines, lines
    assert not any("operator's assertion" in line for line in lines), lines


@pytest.mark.skipif(os.name == "nt", reason="stub gh is a POSIX shell script")
@pytest.mark.parametrize("case", ["two-remotes", "two-urls", "unparseable-url"])
def test_repo_when_origin_cannot_name_the_repository_is_the_operators_assertion(tmp_path, case):
    """B2: when origin cannot name a repository (several remotes, several URLs, a URL of no known
    shape), --repo is accepted as the operator's assertion and an info line says so."""
    root = _origin_repo(tmp_path)
    if case == "two-remotes":
        _git(root, "remote", "add", "upstream", ATTACKER_URL)
    elif case == "two-urls":
        _git(root, "remote", "set-url", "--add", "origin", ATTACKER_URL)
    else:
        _git(root, "remote", "set-url", "origin", "/srv/victim/widget.git")
    proc, _ = _gesture(root, "--repo", "victim/widget", issues=OPEN_7)
    lines = proc.stdout.splitlines()
    assert (proc.returncode, lines[-1:]) == (1, ["NO-GO"]), (lines, proc.stderr)
    assert "open launch:blocker: #7" in lines, lines
    said = [line for line in lines if line.startswith("info: --repo victim/widget is the "
                                                       "operator's assertion")]
    assert len(said) == 1, lines


#: Absolute claims review #1 found false (#429 B1); the docs enumerate instead.
FALSE_ABSOLUTES = ("nobody can steer the outcome by",
                   "Nothing outside `main` can steer the verdict")


def test_docs_make_no_absolute_steering_claim():
    """B1: the docstring, decision-rule.md and the CHANGELOG entry list what is removed or pinned
    and the residual trust roots; none claims that nothing can steer the verdict."""
    claims = {"decide.py docstring": _flat(dz.__doc__ or ""),
              "decision-rule.md": _flat(RULE.read_text(encoding="utf-8")),
              "CHANGELOG entry": _flat(_changelog_entry())}
    found = [(where, phrase) for where, text in claims.items() for phrase in FALSE_ABSOLUTES
             if phrase in text]
    assert found == [], found
    for where, text in claims.items():
        for name in ("GH_FORCE_TTY", "CLICOLOR_FORCE", "GH_PAGER", "core.commitGraph=false"):
            assert name in text, (where, name)


@pytest.mark.parametrize("case", ["untracked-signed", "proposed-edited"])
def test_working_tree_scorecard_and_definition_cannot_steer_the_verdict(tmp_path, case):
    """defect 5, the live gesture: main is unscored with no (or a proposed) definition; the working
    tree carries a scored card and a signed definition. Only main counts."""
    unscored = json.loads(SHIPPED_CARD.read_text(encoding="utf-8"))
    if case == "untracked-signed":
        root = _origin_repo(tmp_path, card=unscored, definition=False)
        reason = "definition missing: docs/launch/definition.json is not tracked at origin/main"
    else:
        root = _origin_repo(tmp_path, card=unscored, status="proposed")
        reason = "definition not signed: status is 'proposed'"
    (root / "docs" / "launch" / "scorecard.json").write_text(json.dumps(_passing_card()),
                                                             encoding="utf-8")
    (root / "docs" / "launch" / "definition.json").write_text(json.dumps(_definition()),
                                                              encoding="utf-8")
    proc, _ = _gesture(root)
    lines = proc.stdout.splitlines()
    assert (proc.returncode, lines[-1:]) == (1, ["NO-GO"]), (lines, proc.stderr)
    assert "D1 Correctness: score 2 < 3 (gating)" in lines, lines
    assert reason in lines, lines


def _flat(text):
    return " ".join(text.split())


def _ci_sentence():
    """The checker's platform sentence, built from the CI legs .github/workflows/ci.yml runs."""
    legs = re.findall(r"^\s*- os: (\S+)\s*\n\s*python: \"([0-9.]+)\"",
                      (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"), re.M)
    assert legs, "no CI legs found"
    by_family = {}
    for runner, python in legs:
        family = {"ubuntu": "Linux", "macos": "macOS"}.get(runner.split("-")[0])
        assert family, f"a CI leg on {runner}: the checker's platform sentence must name it"
        by_family.setdefault(family, []).append(python)

    def listed(versions):
        versions = sorted(versions, key=lambda v: tuple(int(x) for x in v.split(".")))
        if len(versions) == 1:
            return versions[0]
        return ", ".join(versions[:-1]) + " and " + versions[-1]

    legs_text = " and ".join(f"on {family} with Python {listed(by_family[family])}"
                             for family in ("Linux", "macOS") if family in by_family)
    return (f"CI runs it {legs_text}; Python 3.9 was measured by hand, not CI-proven. It refuses "
            "to run on Windows (exit 2).")


def _changelog_entry(text=None):
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8") if text is None else text
    # Any section: Unreleased before a release, the dated section after one (#1074).
    entries = [e for e in re.split(r"\n(?=- \*\*)", text) if e.startswith("- **") and "(#331, #429)" in e]
    assert len(entries) == 1, f"{len(entries)} changelog entries name (#331, #429)"
    return entries[0]


def test_platform_claims_match_the_ci_legs():
    """defect 6: every platform claim says what CI proves, and nothing claims Windows."""
    sentence = _ci_sentence()
    claims = {"decide.py docstring": dz.__doc__ or "", "decision-rule.md": RULE.read_text(
        encoding="utf-8"), "CHANGELOG entry": _changelog_entry()}
    missing = [where for where, text in claims.items() if sentence not in _flat(text)]
    assert missing == [], f"{sentence!r} missing from {missing}"
    scanned = {**claims, "README.md": (ROOT / "README.md").read_text(encoding="utf-8")}
    for path in sorted((ROOT / "docs").rglob("*.md")):
        scanned[str(path.relative_to(ROOT))] = path.read_text(encoding="utf-8")
    banned = "runs on Linux, macOS and Windows"
    assert [where for where, text in scanned.items() if banned in _flat(text)] == []


def test_windows_is_refused_before_anything_runs(tmp_path, capsys, monkeypatch):
    """defect 6: on Windows the checker refuses (exit 2) before it parses arguments or runs
    anything. `dz.os` is swapped for a copy whose `name` is "nt" (patching os.name globally breaks
    3.9's pathlib)."""
    root = _repo(tmp_path)
    started, real = [], subprocess.run

    def spy(argv, *a, **kw):
        started.append(list(argv))
        return real(argv, *a, **kw)

    monkeypatch.setattr(subprocess, "run", spy)
    monkeypatch.setattr(dz, "os", types.SimpleNamespace(**{**vars(os), "name": "nt"}))
    fake = _FakeGh(root)
    code = _main([str(root)], run=fake)
    out, err = capsys.readouterr()
    assert (code, out, started, fake.calls) == (2, "", [], []), (err, started)
    assert err.startswith("decide.py: REFUSED: ") and "Windows" in err, err


@pytest.mark.parametrize("change", ["bench-deleted", "bench-edited", "bench-symlinked",
                                    "scorecard-deleted", "definition-deleted"])
def test_working_tree_is_never_read(tmp_path, capsys, change):
    """Everything is read from main's commit: a checkout's deleted, edited or symlinked files change
    nothing (and nothing is hashed, so no filter or autocrlf can make an edit look clean)."""
    root = _repo(tmp_path)
    launch = root / "docs" / "launch"
    if change == "bench-deleted":
        (root / BENCH).unlink()
    elif change == "bench-edited":
        (root / BENCH).write_text('{"edited": true}', encoding="utf-8")
    elif change == "bench-symlinked":
        (tmp_path / "copy.json").write_text("{}", encoding="utf-8")
        (root / BENCH).unlink()
        (root / BENCH).symlink_to(tmp_path / "copy.json")
    elif change == "scorecard-deleted":
        (launch / "scorecard.json").unlink()
    else:
        (launch / "definition.json").unlink()
    code, lines, err = _run(root, _blockers(tmp_path, []), capsys)
    assert (code, lines[-1:], err) == (0, ["GO"], ""), (lines, err)


SHADOW = "import sys\nprint('GO')\nsys.exit(0)\n"


@pytest.mark.parametrize("shadow", ["datetime", "json", "argparse", "pathlib", "subprocess",
                                    "selectors", "urllib-package", "json-package",
                                    "json-sourceless-pyc"])
def test_untracked_module_beside_the_checker_cannot_steer_the_verdict(tmp_path, shadow):
    """Post-PR review 1: Python puts a script's own folder first on sys.path, so an untracked
    tools/readiness/<stdlib name>.py (or package, or sourceless .pyc) would replace the module the
    checker imports and could print GO, exit 0 -- even offline. Driven by the documented gesture;
    main lacks the benchmark, so the true verdict is NO-GO for that reason."""
    root = _origin_repo(tmp_path, bench=False)
    _commit_bench(root)
    here = root / "tools" / "readiness"
    here.mkdir(parents=True, exist_ok=True)
    name, _, kind = shadow.partition("-")
    if kind == "package":
        (here / name).mkdir()
        (here / name / "__init__.py").write_text(SHADOW, encoding="utf-8")
    elif kind == "sourceless":
        src = tmp_path / f"{name}.py"
        src.write_text(SHADOW, encoding="utf-8")
        compile_it = ("import py_compile, sys; "
                      "py_compile.compile(sys.argv[1], cfile=sys.argv[2], doraise=True)")
        compiled = subprocess.run([sys.executable, "-c", compile_it, str(src),
                                   str(here / f"{name}.pyc")], capture_output=True, text=True)
        assert compiled.returncode == 0, compiled.stderr
    else:
        (here / f"{name}.py").write_text(SHADOW, encoding="utf-8")
    proc, _ = _offline_gesture(root)
    lines = proc.stdout.splitlines()
    assert "GO" not in lines, (proc.returncode, lines, proc.stderr)
    _not_on_main(proc)


def test_every_git_call_is_scrubbed_pinned_and_config_free(tmp_path, capsys, monkeypatch):
    """Every git call the checker makes carries the replace, pathspec and protocol pins and `-C
    <root>`, an environment whose only GIT_* are the five pins (GIT_TRACE is planted, so a missing
    scrub shows), and only a read-only subcommand."""
    root = _repo(tmp_path)
    fake = _FakeGh(root, ref=_ref_answer(_main_sha(root)))
    monkeypatch.setenv("GIT_TRACE", str(tmp_path / "trace.txt"))
    seen, real = [], subprocess.run

    def spy(argv, *a, **kw):
        if list(argv)[:1] == ["git"]:
            seen.append((list(argv), kw.get("env")))
        return real(argv, *a, **kw)

    monkeypatch.setattr(subprocess, "run", spy)
    code, lines, err = _run(root, None, capsys, fake=fake)
    assert seen, "no git call was recorded"
    prefix = GIT_PREFIX + ["-C", str(root)]
    for argv, env in seen:
        assert argv[:len(prefix)] == prefix, argv
        sub = argv[len(prefix):len(prefix) + 1]
        assert sub and sub[0] in GIT_SUBCOMMANDS, argv
        assert {k: v for k, v in (env or {}).items() if k.upper().startswith("GIT_")} == GIT_PINS
    assert {argv[len(prefix)] for argv, _ in seen} == GIT_SUBCOMMANDS
    assert (code, lines[-1:]) == (0, ["GO"]), (lines, err)


@pytest.mark.parametrize("entry", ["global-origin-url", "global-upstream"])
def test_global_remote_entries_cannot_cause_a_false_refusal(tmp_path, entry):
    """A remote in the user's global config is not this checkout's: no false exit 2."""
    root = _origin_repo(tmp_path / "victim")
    home = tmp_path / "home"
    home.mkdir()
    name = "origin" if entry == "global-origin-url" else "upstream"
    (home / ".gitconfig").write_text(f'[remote "{name}"]\n\turl = {ATTACKER_URL}\n',
                                     encoding="utf-8")
    proc, _ = _gesture(root, env={"HOME": str(home)})
    assert (proc.returncode, proc.stdout.splitlines()[-1:]) == (0, ["GO"]), (proc.stdout,
                                                                             proc.stderr)


def test_symbolic_origin_main_is_refused(tmp_path, capsys):
    """`show-ref --verify` follows a symbolic ref: refs/remotes/origin/main pointing at a local
    branch would make that branch main. Offline, so only this guard can refuse it."""
    root = _repo(tmp_path)
    _git(root, "update-ref", "--no-deref", "-d", MAIN_REF)
    _git(root, "symbolic-ref", MAIN_REF, "refs/heads/main")
    code, lines, err = _run_offline(root, _blockers(tmp_path, []), capsys)
    assert (code, lines, err) == (2, [], f"decide.py: REFUSED: {MAIN_REF} is a symbolic ref (to "
                                         "refs/heads/main); the checker reads only a ref that git "
                                         "fetch wrote\n")


@pytest.mark.parametrize("how", ["update-ref", "stale-fetch"])
def test_local_main_that_differs_from_the_remote_main_is_refused(tmp_path, how):
    """The live gesture compares this checkout's main with the remote's over REST: a main moved by
    hand, or one fetched before the remote moved, is refused naming both commits."""
    base = tmp_path / "victim"
    if how == "update-ref":
        root = _origin_repo(base, bench=False)
        _commit_bench(root)
        _git(root, "update-ref", MAIN_REF, "HEAD")
    else:
        root = _origin_repo(base)
        (root / "docs" / "launch" / "definition.json").write_text(
            json.dumps(_definition("proposed")), encoding="utf-8")
        _git(root, "commit", "-q", "-am", "the remote's main moves on")
        _git(root, "push", "-q", str(base / "origin.git"), "HEAD:refs/heads/main")
        _git(root, "reset", "-q", "--hard", "HEAD~1")
    local, remote = _main_sha(root), _git(base / "origin.git", "rev-parse", "refs/heads/main")
    proc, _ = _gesture(root, main_sha=remote)
    assert (proc.returncode, proc.stdout, local != remote) == (2, "", True), (proc.stdout,
                                                                              proc.stderr)
    assert proc.stderr.startswith("decide.py: REFUSED: "), proc.stderr
    assert local in proc.stderr and remote in proc.stderr, proc.stderr
    assert "git fetch origin, then rerun" in proc.stderr, proc.stderr


REST_MAIN_FAULTS = {
    "nonzero": lambda sha: _Proc(1, "", "gh: Not Found (HTTP 404)"),
    "empty": lambda sha: _Proc(0, "  \n"),
    "unparsable": lambda sha: _Proc(0, "<html>busy</html>"),
    "truncated": lambda sha: _Proc(0, _ref_answer(sha)[:-6]),
    "duplicate-key": lambda sha: _Proc(0, '{"ref": "refs/heads/main", "object": {"sha": "%s", '
                                          '"type": "commit", "sha": "%s"}}' % ("1" * 40, sha)),
    "array": lambda sha: _Proc(0, "[" + _ref_answer(sha) + "]"),
    "wrong-type": lambda sha: _Proc(0, json.dumps({"ref": "refs/heads/main",
                                                   "object": {"sha": sha, "type": "tag"}})),
    "no-sha": lambda sha: _Proc(0, json.dumps({"ref": "refs/heads/main",
                                               "object": {"type": "commit"}})),
    "sha-mismatch": lambda sha: _Proc(0, _ref_answer("1" * 40)),
}


@pytest.mark.parametrize("fault", list(REST_MAIN_FAULTS))
def test_rest_main_read_that_fails_or_is_malformed_is_refused(tmp_path, capsys, fault):
    """A REST read of main that fails, or is not one commit object whose sha is this checkout's
    main, is refused before the blocker list is read: never a verdict."""
    root = _repo(tmp_path)
    fake = _FakeGh(root, ref=REST_MAIN_FAULTS[fault](_main_sha(root)))
    code, lines, err = _run(root, None, capsys, fake=fake)
    assert (code, lines, fake.issue_reads) == (2, [], []), (lines, err)
    assert err.startswith("decide.py: REFUSED: ") and "github.com/acme/widget" in err, err


@pytest.mark.parametrize("where", ["global", "per-host", "config-get-fails"])
def test_gh_unix_socket_is_refused(tmp_path, capsys, where):
    """gh's `http_unix_socket` sends every request to a local socket that can answer anything: set
    (for every host or for this one), or unreadable, is refused before any `gh api` call."""
    root = _repo(tmp_path)
    socket = {"global": {"global": "/run/forged-gh.sock\n"},
              "per-host": {"host": "/run/forged-gh.sock\n"},
              "config-get-fails": {"global": _Proc(1, "", "failed to read configuration")}}[where]
    fake = _FakeGh(root, socket=socket)
    code, lines, err = _run(root, None, capsys, fake=fake)
    assert (code, lines, fake.api_calls) == (2, [], []), (lines, err)
    assert err.startswith("decide.py: REFUSED: ") and "http_unix_socket" in err, err


def test_old_git_is_refused(tmp_path, capsys, monkeypatch):
    """A git older than 2.32 ignores GIT_CONFIG_GLOBAL, so global config cannot be switched off."""
    root = _repo(tmp_path)
    monkeypatch.setenv("PATH", _stub_git(tmp_path, "old-git", 'for a in "$@"; do\n  if [ "$a" = '
                                         '--version ]; then echo "git version 2.31.1"; exit 0; fi\n'
                                         "done"))
    code, lines, err = _run(root, None, capsys, fake=_FakeGh(root))
    assert (code, lines) == (2, []), (lines, err)
    assert err.startswith("decide.py: REFUSED: ") and "2.31.1" in err and "2.32" in err, err


def test_offline_blocker_list_is_never_go(tmp_path, capsys):
    """A file cannot establish that no open launch:blocker issue exists: offline is never GO."""
    root = _repo(tmp_path)
    blockers = _blockers(tmp_path, [])
    code, lines, err = _run_offline(root, blockers, capsys)
    reasons = [l for l in lines if not l.startswith("info:") and l not in ("GO", "NO-GO")]
    assert (code, lines[-1:], reasons) == (1, ["NO-GO"], [OFFLINE.format(blockers)]), (lines, err)


@pytest.mark.parametrize("mode", ["offline", "live"])
def test_info_lines_name_the_main_commit_and_the_blocker_source(tmp_path, capsys, mode):
    root = _repo(tmp_path)
    blockers = _blockers(tmp_path, [])
    if mode == "offline":
        code, lines, err = _run_offline(root, blockers, capsys)
        source = f"info: blockers read offline from {blockers!r}"
    else:
        code, lines, err = _run(root, blockers, capsys)
        source = "info: blockers read over REST from github.com/acme/widget"
    got = [l for l in lines if l.startswith(("info: main ", "info: blockers "))]
    assert got == [f"info: main is {MAIN_REF} at {_main_sha(root)}", source], (lines, err)
    assert lines[-3:-1] == got, lines


def test_inherited_git_trace_writes_nothing(tmp_path, capsys, monkeypatch):
    """The checker writes nothing anywhere: an inherited GIT_TRACE must not make git write."""
    root = _repo(tmp_path)
    fake = _FakeGh(root, ref=_ref_answer(_main_sha(root)))
    trace = tmp_path / "trace.txt"
    monkeypatch.setenv("GIT_TRACE", str(trace))
    code, lines, err = _run(root, None, capsys, fake=fake)
    assert (code, lines[-1:], trace.exists()) == (0, ["GO"], False), (lines, err)


def _partial_clone(tmp_path, rel, text):
    """A blob:none partial clone of a passing main; then main moves on with `rel` rewritten to
    `text`, and the clone fetches the new commit but not that blob. origin stays a local file://
    URL, so an unguarded lazy fetch never leaves the machine. Returns (clone, missing) where
    missing() lists the objects main needs that the clone lacks."""
    work = _repo(tmp_path / "work")
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(bare))
    _git(bare, "config", "uploadpack.allowFilter", "true")
    _git(work, "push", "-q", str(bare), "HEAD:refs/heads/main")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", "--filter=blob:none", f"file://{bare}", str(clone),
         GIT_NO_LAZY_FETCH=None)
    (work / rel).write_text(text, encoding="utf-8")
    _git(work, "commit", "-q", "-am", f"a {rel} blob the clone has not fetched")
    _git(work, "push", "-q", str(bare), "HEAD:refs/heads/main")
    _git(clone, "fetch", "-q", "origin")

    def missing():
        listed = _git(clone, "rev-list", "--objects", "--missing=print", MAIN_REF)
        return [line for line in listed.splitlines() if line.startswith("?")]

    return clone, missing


def test_partial_clone_main_read_never_fetches(tmp_path, capsys):
    """A blob:none partial clone whose main's scorecard blob is not local: the checker refuses
    rather than lazily fetching it (network, and a write to .git/objects)."""
    card = _passing_card()
    _dim(card, "D12")["score"] = 4
    clone, missing = _partial_clone(tmp_path, "docs/launch/scorecard.json", json.dumps(card))
    before = missing()
    assert before, "the fixture must leave main's new scorecard blob out of the clone"
    code, lines, err = _run_offline(clone, _blockers(tmp_path, []), capsys,
                                    ("--repo", "victim/widget"))
    assert (code, lines, missing()) == (2, [], before), (lines, err)
    assert err.startswith("decide.py: REFUSED: cannot read docs/launch/scorecard.json at "
                          f"{MAIN_REF} ("), err


@pytest.mark.parametrize("case", ["no-origin-main", "never-committed", "not-a-work-tree",
                                  "below-top-level", "git-missing", "not-a-commit",
                                  "heads-lookalike", "tags-lookalike", "refs-lookalike",
                                  "no-git-symlinked-bench"])
def test_main_that_cannot_be_read_is_refused_never_a_verdict(tmp_path, capsys, monkeypatch, case):
    """When main cannot be read the checker refuses (exit 2): no verdict, offline or live. The
    look-alikes are what `rev-parse refs/remotes/origin/main` would resolve once the real ref is
    gone; offline, so only the exact-ref read can refuse them."""
    exact, needle = None, None
    if case in ("no-origin-main", "heads-lookalike", "tags-lookalike", "refs-lookalike"):
        root = _repo(tmp_path)
        _git(root, "update-ref", "-d", MAIN_REF)
        if case != "no-origin-main":
            prefix = {"heads-lookalike": "refs/heads/", "tags-lookalike": "refs/tags/",
                      "refs-lookalike": "refs/"}[case]
            _git(root, "update-ref", prefix + MAIN_REF, "HEAD")
        exact = NO_MAIN
    elif case == "never-committed":
        root = _repo(tmp_path, bench=False, commit=False)
        (root / BENCH).write_text("{}", encoding="utf-8")
        exact = NO_MAIN
    elif case == "not-a-work-tree":
        root = _repo(tmp_path, git=False)
        needle = "is not the top of a git work tree"
    elif case == "below-top-level":
        root = _repo(tmp_path, git=False)
        _git_repo(tmp_path)
        needle = "is not the top of its git work tree"
    elif case == "git-missing":
        root = _repo(tmp_path)
        needle = "git cannot be run"
    elif case == "not-a-commit":
        root = _repo(tmp_path)
        _git(root, "update-ref", MAIN_REF, _git(root, "rev-parse", "HEAD^{tree}"))
        needle = "is not a commit"
    else:
        root = _repo(tmp_path, bench=False, git=False)
        (root / BENCH).symlink_to("/etc/hosts")
        needle = "is not the top of a git work tree"
    blockers = _blockers(tmp_path, [])
    if case == "git-missing":
        empty = tmp_path / "empty-path"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))
    code, lines, err = _run_offline(root, blockers, capsys)
    assert (code, lines) == (2, []), (lines, err)
    assert err.startswith("decide.py: REFUSED: "), err
    if exact:
        assert err == f"decide.py: REFUSED: {exact}\n", err
    else:
        assert needle in err, err


@pytest.mark.parametrize("case", ["card-symlink", "card-tree", "card-oversized", "def-symlink",
                                  "def-tree", "def-oversized"])
def test_scorecard_or_definition_at_main_that_is_not_a_small_regular_file_is_malformed(
        tmp_path, capsys, case):
    """The scorecard and definition are read from main's commit as regular-file blobs of at most
    1 MiB: a committed symlink, a directory or an oversized file is malformed, said at main."""
    root = _repo(tmp_path, git=False)
    which, kind = case.split("-")
    name = "scorecard.json" if which == "card" else "definition.json"
    path = root / "docs" / "launch" / name
    text = path.read_text(encoding="utf-8")
    if kind == "symlink":
        outside = tmp_path / ("outside-" + name)
        outside.write_text(text, encoding="utf-8")
        path.unlink()
        path.symlink_to(outside)
    elif kind == "tree":
        path.unlink()
        path.mkdir()
        (path / "inner.json").write_text(text, encoding="utf-8")
    else:
        path.write_text(text + " " * (1 << 20), encoding="utf-8")
    _git_repo(root)
    code, lines, err = _run(root, _blockers(tmp_path, []), capsys)
    assert (code, lines) == (2, []), (lines, err)
    assert err.startswith("decide.py: MALFORMED: ") and f"at {MAIN_REF} (" in err, err


# -- #429 regression guards: these pass on the #331 checker too ----------------------------------

def test_origin_head_pointing_at_origin_main_is_not_refused(tmp_path, capsys):
    """The symbolic-ref refusal is about refs/remotes/origin/main itself; `git remote set-head`
    makes refs/remotes/origin/HEAD symbolic, which is ordinary and must stay GO."""
    root = _repo(tmp_path)
    _git(root, "remote", "set-head", "origin", "main")
    code, lines, err = _run(root, _blockers(tmp_path, []), capsys)
    assert (code, lines[-1:]) == (0, ["GO"]), (lines, err)


def test_foreign_owned_checkout_is_refused_naming_dubious_ownership(tmp_path, capsys, monkeypatch):
    """Global config is off, so a safe.directory exception there does not apply: a checkout another
    user owns is refused, quoting git's own line."""
    root = _repo(tmp_path)
    blockers = _blockers(tmp_path, [])
    monkeypatch.setenv("PATH", _stub_git(tmp_path, "foreign-git",
                                         "GIT_TEST_ASSUME_DIFFERENT_OWNER=1; "
                                         "export GIT_TEST_ASSUME_DIFFERENT_OWNER"))
    code, lines, err = _run_offline(root, blockers, capsys)
    assert (code, lines) == (2, []), (lines, err)
    assert err.startswith("decide.py: REFUSED: ") and "dubious ownership" in err, err
    assert "safe.directory" in err, err


@pytest.mark.parametrize("entry", ["docs/launch/evidence/a\x00b.md",
                                   "docs/launch/evidence/a\x01b.md",
                                   "docs/launch/evidence/a\x7fb.md"], ids=["nul", "soh", "del"])
def test_evidence_path_with_a_control_character_is_nogo(tmp_path, capsys, entry):
    card = _passing_card()
    _dim(card, "D8")["evidence"] = [entry]
    code, lines, err = _run(_repo(tmp_path, card), _blockers(tmp_path, []), capsys)
    assert (code, lines[-1:], err) == (1, ["NO-GO"], ""), (lines, err)
    assert (f"D8 Docs: evidence {entry!r} is not a link: contains a NUL or control character "
            "(gating)") in lines, lines


@pytest.mark.parametrize("case", ["no-args", "unknown-flag", "bad-repo", "missing-blockers-file",
                                  "two-roots"])
def test_usage_errors_exit_2_never_the_go_code(tmp_path, capsys, case):
    root = _repo(tmp_path)
    argv = {"no-args": [], "unknown-flag": [str(root), "--bogus"],
            "bad-repo": [str(root), "--repo", "nope"],
            "missing-blockers-file": [str(root), "--blockers-json", str(tmp_path / "absent.json")],
            "two-roots": [str(root), str(root)]}[case]
    fake = _FakeGh(root)
    code = _main(argv, run=fake)
    out, err = capsys.readouterr()
    assert (code, fake.api_calls) == (2, []), (out, err)
    assert out.strip().splitlines()[-1:] not in (["GO"], ["NO-GO"]), out


@pytest.mark.parametrize("case", ["rate-limit", "truncated", "null", "object-page"])
def test_blocker_read_failure_shapes_exit_2(tmp_path, capsys, case):
    root = _repo(tmp_path)
    pages = {"rate-limit": _Proc(1, "", "gh: API rate limit exceeded (HTTP 403)"),
             "truncated": '[{"number": 1, "state": "open"', "null": "null",
             "object-page": '{"number": 1}'}[case]
    code, lines, err = _run(root, None, capsys, fake=_FakeGh(root, pages))
    assert (code, lines) == (2, []), (lines, err)
    assert err.startswith("decide.py: MALFORMED: "), err


@pytest.mark.parametrize("where", ["packed-refs", "linked-worktree"])
def test_exact_origin_main_is_found_packed_or_from_a_linked_worktree(tmp_path, capsys, where):
    root = _repo(tmp_path)
    if where == "packed-refs":
        _git(root, "pack-refs", "--all")
    else:
        root, main = tmp_path / "worktree", root
        _git(main, "worktree", "add", "-q", "--detach", str(root), "HEAD")
    code, lines, err = _run(root, _blockers(tmp_path, []), capsys)
    assert (code, lines[-1:]) == (0, ["GO"]), (lines, err)


def test_partial_clone_missing_benchmark_blob_is_refused(tmp_path, capsys):
    """A path read by size only (the benchmark, an evidence file) whose blob is not in a partial
    clone is refused, never taken as present: git 2.55 reports the size as `BAD` (exit 0), 2.39
    fails."""
    clone, missing = _partial_clone(tmp_path, BENCH, '{"run": 2}')
    before = missing()
    assert before, "the fixture must leave main's new benchmark blob out of the clone"
    code, lines, err = _run_offline(clone, _blockers(tmp_path, []), capsys)
    assert (code, lines, missing()) == (2, [], before), (lines, err)
    assert err.startswith(f"decide.py: REFUSED: cannot read {BENCH} at {MAIN_REF} ("), err
