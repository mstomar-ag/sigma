"""#722 Part 1: a parsed, code-enforced spend/irreversible-action permission marker.

`loop.py spend-approval <dir> <goal> --action "<text>"` is the ONE place an agent learns whether the
`SKILL.md` "NEVER run one unattended" park is exempted for THIS goal. It reads the issue BODY's
`sigma:spend-approved=<label>` marker (never a comment), requires the issue author to be in
`spend_approval.approvers`, posts a durable audit comment (marker line, author, action, body hash)
BEFORE answering APPROVED, and is single-use per goal+label. Everything else fails closed.

Part 2 (a real external-$ cap) is deliberately NOT here and not tested: nothing in this file can
spend anything. Every source is a fake; no gh, no network.

Run-the-control (AGENTS.md): every DENIED test below was seen red against a deliberately broken
`spend_approval.py` (guard removed) before being trusted; the CLI tests run the exact gesture the
docs print, copied out of running.md rather than hand-written here.
"""
import importlib.util
import json
import pathlib
import re
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
S = ROOT / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


sa = _mod("spend_approval")

MARKER_LINE = "sigma:spend-approved=campaign-7"
BODY = MARKER_LINE + "\n\nRender the clips.\n\nDone when rendered.\n"
CFG = {"spend_approval": {"enabled": True, "approvers": ["Alice"]}}


class FakeSource:
    """The duck-typed surface `check` needs: `fetch_issue_for_approval`, `fetch_comment_bodies`,
    `note`. `note` appends to the comment list so a SECOND call sees the used-marker, exactly as a
    real issue timeline would."""
    def __init__(self, body=BODY, author="alice", comments=None, issue_error=None,
                 comments_error=None, note_error=None, updated_at="2026-10-05T10:00:00Z",
                 comments_after_note=None, association="OWNER"):
        self.association = association
        self.body, self.author, self.updated_at = body, author, updated_at
        self.comments = list(comments or [])
        self.issue_error, self.comments_error, self.note_error = issue_error, comments_error, note_error
        self.comments_after_note = comments_after_note
        self.notes = []

    def fetch_issue_for_approval(self, goal):
        if self.issue_error:
            raise self.issue_error
        return {"body": self.body, "author": self.author, "updated_at": self.updated_at,
                "author_association": self.association}

    def fetch_comment_bodies(self, goal):
        if self.comments_error:
            raise self.comments_error
        return list(self.comments)

    def note(self, goal, text):
        if self.note_error:
            raise self.note_error
        self.notes.append((goal, text))
        self.comments.append(text)
        if self.comments_after_note is not None:
            self.comments.append(self.comments_after_note)


@pytest.fixture(autouse=True)
def _isolated_counter(tmp_path, monkeypatch):
    """The use counter writes under `.sdlc/state` of the working directory: keep it off the repo."""
    monkeypatch.chdir(tmp_path)


def run(src, config=CFG, action="render 40 clips on RunPod", goal="77"):
    return sa.check(".sdlc", goal, action, config, src)


# ---- parse_marker: FIRST LINE ONLY (the security decision; see parse_marker's docstring) -------

def test_parse_marker_happy_and_html_comment_wrapped():
    assert sa.parse_marker(BODY) == ("ok", "campaign-7", MARKER_LINE)
    assert sa.parse_marker("<!-- sigma:spend-approved=x1 -->\nrest")[:2] == ("ok", "x1")
    assert sa.parse_marker("<!--sigma:spend-approved=x1-->")[:2] == ("ok", "x1")


def test_parse_marker_absent():
    assert sa.parse_marker("just prose, spend-approved is not a marker")[0] == "none"
    assert sa.parse_marker("")[0] == "none"
    assert sa.parse_marker(None)[0] == "none"


@pytest.mark.parametrize("first_line", [
    "sigma:spend-approved=",                    # empty label
    "sigma:spend-approved= x",                  # space before label
    "sigma:spend-approved=x y",                 # label with a space
    "sigma:spend-approved=-x",                  # leading punctuation
    "sigma:spend-approved=" + "a" * 65,         # too long
    "sigma:spend-approved=x; rm -rf",           # trailing junk
    "see sigma:spend-approved=x for details",   # mid-sentence, not a declaration
    "- sigma:spend-approved=x",                 # list-item quoting
    "> sigma:spend-approved=x",                 # blockquote quoting
    "    sigma:spend-approved=x",               # indented code
    "\tsigma:spend-approved=x",
    " \tsigma:spend-approved=x",                 # a tab after spaces expands to column 4+
    "  \tsigma:spend-approved=x",
    "\t sigma:spend-approved=x",
    "<!-- sigma:spend-approved=x",              # half a comment wrapper
    "sigma:spend-approved=x -->",
    "`sigma:spend-approved=x`",                 # inline code
    "\u00a0sigma:spend-approved=x",             # NBSP is not ASCII indentation
    "\ufeffsigma:spend-approved=x",             # BOM
    "sigma:spend-approved=x\u200b",             # zero-width trailing char
    "text\u2028sigma:spend-approved=x",         # not a line break to GitHub
    "<!-- a --><!-- sigma:spend-approved=x -->",
])
def test_parse_marker_malformed_first_line_is_not_ok(first_line):
    assert sa.parse_marker(first_line + "\nrest")[0] == "malformed"


@pytest.mark.parametrize("body", [
    "Render the clips.\n" + MARKER_LINE,            # second line: not honoured
    "\n" + MARKER_LINE,                             # leading blank line: not the first line
    "```\n" + MARKER_LINE + "\n```",               # fenced
    "~~~\n" + MARKER_LINE,
    "<!--\n" + MARKER_LINE + "\n-->",              # inside a comment
    "<pre>\n" + MARKER_LINE + "\n</pre>",
    "`\n" + MARKER_LINE + "\n`",                   # multi-line inline code span
    "<!-- a --><!--\n" + MARKER_LINE + "\n-->",    # comment re-opened on the same line
    "<!-- x\n--><!--\n" + MARKER_LINE + "\n-->",
    "<pre>x</pre><pre>\n" + MARKER_LINE + "\n</pre>",
    "| a |\n|---|\n" + MARKER_LINE,                 # table row
    "1. ```\n   " + MARKER_LINE + "\n   ```",
    "<!--\n```\n-->\n```\n-->\n" + MARKER_LINE,  # every shape review rounds 1-5 found
])
def test_a_marker_anywhere_but_the_first_line_is_never_honoured(body):
    """Each of these was `ok` against some earlier, line-scanning parser (five review rounds). The
    first line has no preceding context, so none of them can ever be a grant."""
    assert sa.parse_marker(body)[0] == "none"


def test_up_to_three_leading_spaces_and_trailing_whitespace_are_tolerated():
    for first in ("   " + MARKER_LINE, MARKER_LINE + " \t "):
        assert sa.parse_marker(first + "\nrest")[:2] == ("ok", "campaign-7")


def test_parse_marker_crlf_and_cr_tolerant():
    assert sa.parse_marker(MARKER_LINE + "\r\nb")[:2] == ("ok", "campaign-7")
    assert sa.parse_marker(MARKER_LINE + "\rb")[:2] == ("ok", "campaign-7")


def test_a_second_mention_later_in_the_body_does_not_void_the_first_line_marker():
    body = MARKER_LINE + "\n\nTo approve, write sigma:spend-approved=<label> on line 1.\n" + MARKER_LINE
    assert sa.parse_marker(body)[:2] == ("ok", "campaign-7")


def test_parse_marker_is_linear_on_pathological_input():
    import time
    t0 = time.monotonic()
    for n in (10_000, 200_000):
        sa.parse_marker(" >" * n + "x\n" + "- " * n + "```" + "<!--" * n)
        sa.parse_marker("sigma:spend-approved=" + "a" * n)
    assert time.monotonic() - t0 < 2.0


# ---- check: the exemption -----------------------------------------------------------------------

def test_approved_posts_audit_comment_before_answering():
    src = FakeSource()
    out = run(src)
    assert out == "APPROVED campaign-7"
    assert len(src.notes) == 1
    goal, text = src.notes[0]
    assert goal == "77"
    # who approved what, when: marker text, author, the action, body hash, timestamp, used-marker
    assert MARKER_LINE in text and "@" not in text.replace("<!--", "")      # no @-mention pings
    assert "alice" in text and "render 40 clips on RunPod" in text
    assert "sigma:spend-approval-used=campaign-7" in text
    assert re.search(r"sha256:[0-9a-f]{64}", text) and "2026-10-05T10:00:00Z" in text
    assert re.search(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", text)


def test_author_match_is_case_insensitive():
    assert run(FakeSource(author="ALICE")).startswith("APPROVED")


def test_single_use_second_call_denied():
    src = FakeSource()
    assert run(src).startswith("APPROVED")
    out = run(src)
    assert out.startswith("DENIED") and "already used" in out
    assert len(src.notes) == 1


def test_new_label_reauthorises():
    src = FakeSource()
    assert run(src).startswith("APPROVED")
    src.body = BODY.replace("campaign-7", "campaign-8")
    assert run(src) == "APPROVED campaign-8"


def test_per_goal_a_body_without_marker_is_denied():
    src = FakeSource(body="Render the clips.\n")
    out = run(src)
    assert out.startswith("DENIED") and "no sigma:spend-approved" in out
    assert src.notes == []


# ---- check: every fail-closed branch ------------------------------------------------------------

@pytest.mark.parametrize("cfg", [
    None, {}, {"spend_approval": None}, {"spend_approval": {}},
    {"spend_approval": {"enabled": False, "approvers": ["alice"]}},
    {"spend_approval": {"enabled": "true", "approvers": ["alice"]}},   # truthy but not `is True`
    {"spend_approval": {"enabled": 1, "approvers": ["alice"]}},
])
def test_off_unless_enabled_is_exactly_true(cfg):
    src = FakeSource()
    assert run(src, config=cfg) == "OFF"
    assert src.notes == []


@pytest.mark.parametrize("approvers", [None, [], "alice", ["  "], [None], [3], {"alice": 1}])
def test_enabled_without_valid_approvers_denies(approvers):
    src = FakeSource()
    out = run(src, config={"spend_approval": {"enabled": True, "approvers": approvers}})
    assert out.startswith("DENIED") and src.notes == []


def test_approvers_with_one_non_string_entry_denies_even_if_author_listed():
    src = FakeSource()
    out = run(src, config={"spend_approval": {"enabled": True, "approvers": ["alice", 3]}})
    assert out.startswith("DENIED") and src.notes == []


def test_non_approver_author_denied():
    src = FakeSource(author="some-bot")
    out = run(src)
    assert out.startswith("DENIED") and "approvers" in out and src.notes == []


@pytest.mark.parametrize("association", ["CONTRIBUTOR", "NONE", "FIRST_TIME_CONTRIBUTOR", "", None])
def test_untrusted_author_association_denied_even_if_listed(association):
    # the approver login matches, but GitHub does not vouch for the author: an authorization must
    # not be honoured from a drive-by account on a public repository (fail closed, no audit post)
    src = FakeSource(association=association)
    out = run(src)
    assert out.startswith("DENIED") and "association" in out and src.notes == []


@pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_trusted_author_associations_approve(association):
    assert run(FakeSource(association=association)).startswith("APPROVED")


def test_unreadable_author_denied():
    assert run(FakeSource(author="")).startswith("DENIED")
    assert run(FakeSource(author=None)).startswith("DENIED")


def test_marker_only_in_a_comment_is_never_honoured():
    src = FakeSource(body="Render the clips.\n", comments=[MARKER_LINE])
    out = run(src)
    assert out.startswith("DENIED") and src.notes == []


@pytest.mark.parametrize("body", [
    "x\nsigma:spend-approved=\n", "sigma:spend-approved=\nrest", "sigma:spend-approved=a b",
    "```\n" + MARKER_LINE + "\n```",
])
def test_bad_markers_denied(body):
    src = FakeSource(body=body)
    assert run(src).startswith("DENIED") and src.notes == []


@pytest.mark.parametrize("action", ["", "   ", None, "\n"])
def test_empty_action_denied(action):
    src = FakeSource()
    assert run(src, action=action).startswith("DENIED") and src.notes == []


def test_issue_read_failure_denied():
    assert run(FakeSource(issue_error=RuntimeError("gh down"))).startswith("DENIED")


def test_comment_read_failure_denied_without_posting():
    src = FakeSource(comments_error=RuntimeError("rate limit"))
    assert run(src).startswith("DENIED") and src.notes == []


def test_audit_post_failure_denied_so_no_unaudited_grant():
    src = FakeSource(note_error=RuntimeError("403"))
    out = run(src)
    assert out.startswith("DENIED") and "audit" in out


def test_audit_comment_not_visible_on_reread_denies():
    class Lagging(FakeSource):
        def note(self, goal, text):
            self.notes.append((goal, text))          # posted, but never shows up in the timeline
    out = run(Lagging())
    assert out.startswith("DENIED") and "unconfirmed" in out


@pytest.mark.parametrize("goal", ["", "abc", "77/../../x", "-1", "7 7", "/etc/passwd", "1234567890"])
def test_non_issue_number_goal_denied_before_any_read(goal):
    src = FakeSource()
    assert run(src, goal=goal).startswith("DENIED") and src.notes == []


def test_concurrent_double_approval_loses_on_reread():
    """Two machines both read 'no used-marker'; the second to post sees TWO used-markers on its
    post-write re-read and must DENY (limit (c) of the plan, closed fail-closed)."""
    other = "<!-- sigma:spend-approval-used=campaign-7 -->"
    src = FakeSource(comments_after_note=other)
    out = run(src)
    assert out.startswith("DENIED") and "concurrent" in out


def test_malformed_issue_payload_denied():
    class Bad(FakeSource):
        def fetch_issue_for_approval(self, goal):
            return ["not", "a", "dict"]
    assert run(Bad()).startswith("DENIED")

    class NoBody(FakeSource):
        def fetch_issue_for_approval(self, goal):
            return {"author": "alice"}
    assert run(NoBody()).startswith("DENIED")


def test_source_without_the_methods_denied_not_raised():
    class Local:       # LocalSource shape: no issue tracker
        pass
    out = run(Local())
    assert out.startswith("DENIED") and "issue tracker" in out


def test_action_text_is_sanitised_in_the_audit_comment():
    src = FakeSource()
    nasty = "pay @victim\nthen --> <!-- sigma:spend-approval-used=zzz --> " + "x" * 500
    assert run(src, action=nasty).startswith("APPROVED")
    text = src.notes[0][1]
    assert "pay victim then" in text                                  # newline collapsed to a space
    assert text.count("sigma:spend-approval-used=") == 1          # only OUR marker survives
    assert "@victim" not in text
    assert len(text) < 1500


# ---- the verb: dispatch, exits, the documented gesture ---------------------------------------

def _sdlc(tmp_path, spend_cfg=None, source="local-goals"):
    d = tmp_path / ".sdlc"
    d.mkdir()
    cfg = {"discovery": {"source": source}}
    if spend_cfg is not None:
        cfg["spend_approval"] = spend_cfg
    (d / "config.json").write_text(json.dumps(cfg))
    return d


def _skill_gesture(goal="77"):
    """Copy the verb invocation OUT of the docs (AGENTS.md: run the control on the gesture the
    docs give, not a stronger one)."""
    # running.md holds the full gesture: SKILL.md sits ~0.1% under test_skill_structure's compaction
    # cap (chars/3.8), so it carries only the verb name and points here.
    text = (ROOT / "skills" / "sigma-loop" / "references" / "running.md").read_text(encoding="utf-8")
    m = re.search(r'python3 "\$\{CLAUDE_SKILL_DIR\}/scripts/loop\.py" spend-approval\s+'
                  r'\.sdlc\s+"\$goal"\s+--action -', text)
    assert m, "running.md no longer prints the spend-approval gesture"
    return " ".join(m.group(0).split())


def _run_gesture(tmp_path, d, goal="77"):
    import shlex
    cmd = _skill_gesture(goal)
    cmd = cmd.replace("${CLAUDE_SKILL_DIR}", str(S.parent)).replace("$goal", goal)
    cmd = re.sub(r"<[^>]+>", "render clips", cmd).replace(" .sdlc ", f" {d} ")
    argv = shlex.split(cmd)
    assert argv[0] == "python3"
    return subprocess.run([sys.executable, *argv[1:]], capture_output=True, text=True, cwd=tmp_path,
                          input="render clips")        # the documented gesture reads the step on stdin


def test_documented_gesture_off_by_default_exit_4(tmp_path):
    d = _sdlc(tmp_path)
    r = _run_gesture(tmp_path, d)
    assert r.stdout.split()[0] == "OFF" and r.returncode == 4, (r.stdout, r.stderr)


def test_documented_gesture_denied_exit_3_on_a_local_backlog(tmp_path):
    d = _sdlc(tmp_path, {"enabled": True, "approvers": ["alice"]})
    r = _run_gesture(tmp_path, d)
    assert r.stdout.split()[0] == "DENIED" and r.returncode == 3, (r.stdout, r.stderr)


def test_verb_without_action_flag_is_denied_not_approved(tmp_path):
    d = _sdlc(tmp_path, {"enabled": True, "approvers": ["alice"]})
    r = subprocess.run([sys.executable, str(S / "loop.py"), "spend-approval", str(d), "77"],
                       capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == 3 and r.stdout.split()[0] == "DENIED", (r.stdout, r.stderr)


def test_verb_approved_exit_0_via_loop_main(tmp_path, monkeypatch):
    loop = _mod("loop")
    d = _sdlc(tmp_path, {"enabled": True, "approvers": ["alice"]})
    src = FakeSource()
    monkeypatch.setattr(loop.sources, "get_source", lambda *_a, **_k: src)
    assert loop.main(["loop.py", "spend-approval", str(d), "77", "--action", "render clips"]) == 0
    assert len(src.notes) == 1


def test_verb_stdout_first_word_is_the_machine_contract(tmp_path, monkeypatch, capsys):
    loop = _mod("loop")
    d = _sdlc(tmp_path, {"enabled": True, "approvers": ["alice"]})
    monkeypatch.setattr(loop.sources, "get_source", lambda *_a, **_k: FakeSource())
    loop.main(["loop.py", "spend-approval", str(d), "77", "--action", "render clips"])
    assert capsys.readouterr().out.split()[0] == "APPROVED"


def test_verb_does_not_arm_a_claim():
    """`_ARMS_CLAIM` verbs auto-claim a goal; asking whether a spend is approved must not."""
    loop = _mod("loop")
    assert "spend-approval" not in loop._ARMS_CLAIM


def test_verb_listed_in_usage():
    text = (S / "loop.py").read_text(encoding="utf-8")
    assert "spend-approval <dir> <goal> --action" in text


# ---- docs: the NEVER prose names the verb in all three places ---------------------------------

@pytest.mark.parametrize("rel", ["skills/sigma-loop/SKILL.md",
                                 "skills/sigma-loop/references/running.md", "README.md"])
def test_never_prose_names_the_exemption(rel):
    text = (ROOT / rel).read_text(encoding="utf-8")
    assert "spend-approval" in text, f"{rel}: the NEVER rule no longer names the exemption verb"
    if not rel.endswith("SKILL.md"):        # SKILL.md is size-capped; it names the verb and points
        assert "sigma:spend-approved=" in text


def test_template_documents_the_config_key_default_off():
    tmpl = json.loads((ROOT / "skills/sigma-init/templates/config.json.tmpl")
                      .read_text(encoding="utf-8"))
    assert tmpl["spend_approval"] == {"enabled": False, "approvers": [], "classes": ["spend"]}
    assert "_spend_approval" in tmpl


# ---- GitHubSource REST readers (no gh: a recording fake `run`) --------------------------------

def _gh_source(responses):
    src = _mod("sources")
    calls = []

    def run(args):
        calls.append(args)
        return responses(args)
    s = src.GitHubSource({"discovery": {"github": {"repo": "o/r"}}}, run=run)
    return s, calls


def test_fetch_issue_for_approval_is_rest_one_call_and_reads_user_login():
    s, calls = _gh_source(lambda a: json.dumps(
        {"body": BODY, "user": {"login": "alice"}, "updated_at": "T", "author_association": "OWNER"}))
    got = s.fetch_issue_for_approval("77")
    assert got == {"body": BODY, "author": "alice", "updated_at": "T", "author_association": "OWNER"}
    assert calls == [["api", "repos/o/r/issues/77"]]            # REST, never `issue view --json`


@pytest.mark.parametrize("raw", ["", "not json", "[]"])
def test_fetch_issue_for_approval_raises_on_garbage(raw):
    s, _ = _gh_source(lambda a: raw)
    with pytest.raises(Exception):
        s.fetch_issue_for_approval("77")


def test_fetch_comment_bodies_reads_every_paginated_page():
    two_pages = json.dumps([{"body": "a"}, {"body": "b"}]) + json.dumps([{"body": "used-on-page-2"}])
    s, calls = _gh_source(lambda a: two_pages)
    assert s.fetch_comment_bodies("77") == ["a", "b", "used-on-page-2"]
    assert calls == [["api", "--paginate", "repos/o/r/issues/77/comments"]]


def test_fetch_comment_bodies_raises_on_garbage_so_the_check_denies():
    s, _ = _gh_source(lambda a: "{broken")
    with pytest.raises(Exception):
        s.fetch_comment_bodies("77")
    assert run(type("S", (FakeSource,), {"fetch_comment_bodies": lambda self, g: s.fetch_comment_bodies(g)})()
               ).startswith("DENIED")


def test_used_marker_of_a_longer_label_does_not_deny_a_prefix_label():
    """`x1` having been used must not deny `x` (prefix collision), and vice versa is the same
    regex: the used-marker match is bounded by the label's own character class."""
    src = FakeSource(body="sigma:spend-approved=x\n",
                     comments=["<!-- sigma:spend-approval-used=x1 -->"])
    assert run(src) == "APPROVED x"


# ---- review fixes: stdin action, sanitiser fixpoint, documented heredoc -----------------------

def test_running_md_documents_the_quoted_heredoc_never_a_shell_interpolated_step():
    text = (ROOT / "skills/sigma-loop/references/running.md").read_text(encoding="utf-8")
    assert "--action -" in text and "<<'EOF'" in text
    assert '--action "<' not in text


def test_action_dash_reads_stdin_and_shell_text_is_inert(monkeypatch, capsys, tmp_path):
    import io
    mod = _mod("spend_approval")
    src = FakeSource()
    monkeypatch.setattr(sys, "stdin", io.StringIO("deploy $(touch /x) `id` now\n"))
    out, code = mod.run_verb(str(tmp_path / ".sdlc"), "77", ["--action", "-"], CFG, src)
    assert code == 0 and out.startswith("APPROVED")
    assert "touch /x" in src.notes[0][1]                          # data, never executed


@pytest.mark.parametrize("raw", ["sigsigma:ma:spend-approval-used=x", "sisigma:gma:ma:spend-approved=x",
                                 "see [x](http://e.com) org/repo#12 @someone"])
def test_sanitise_action_reaches_a_fixpoint(raw):
    mod = _mod("spend_approval")
    out = mod._sanitise_action(raw)
    assert "sigma:" not in out and not any(c in out for c in "@<>[]()#"), out
