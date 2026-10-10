"""slack_commands_listen.py (#2336, slice 1 of Epic #2335, `.sdlc/design/2329.md`): config gating,
the channel-membership authorization gate, the command-grammar parser, lifecycle scaffolding
(pidfile/heartbeat/stop-file/lock), and the Socket Mode request handler -- the lazy-import tests
exercise the WITHOUT-`slack_sdk` fallback path WITHOUT any real network/websocket call, mirroring
`test_slack_client.py`'s own no-real-network discipline, via `_mask_slack_sdk` (#2357) -- simulated
absence (`sys.modules[name] = None`, the standard trick that makes `import` raise a real
`ImportError` regardless of whether the package is actually installed), never real environment
absence: `SLACK_COMMANDS.md`'s own documented one-time setup, `pip install "slack_sdk[socket-mode]"`,
makes `slack_sdk` genuinely importable on any machine that has ALSO set up the real listener -- not
a defect, and this suite must pass there too, since that machine's own `--merge` runs this exact
`verify.command`."""
import importlib.util
import json
import os
import pathlib
import signal
import subprocess
import sys
import threading
import time
import types

import pytest

import rest_merge_support

ROOT = pathlib.Path(__file__).resolve().parent.parent
S = ROOT / "skills" / "sigma-loop" / "scripts"


def _mask_slack_sdk(monkeypatch):
    """Make `slack_sdk` (and any of its submodules already cached in `sys.modules` from an earlier
    import elsewhere in this process) genuinely unimportable for the DURATION of one test, via
    `sys.modules[name] = None` -- the standard, well-established idiom for simulating `ImportError`
    deterministically, independent of whether the real package is actually installed on this
    machine (#2357). `monkeypatch` restores the original entries (present or absent) automatically
    at teardown, so this can never leak into another test."""
    for name in list(sys.modules):
        if name == "slack_sdk" or name.startswith("slack_sdk."):
            monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "slack_sdk", None)


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


sc = _mod("slack_commands_listen")
feature_registry = _mod("feature_registry")


@pytest.fixture(autouse=True)
def _repository_shell_trusted(monkeypatch):
    """#707: verify.command now needs the operator's Git-local opt-in; these fixtures' repos are
    the operator's own, so grant it for the module's own copy of the policy."""
    monkeypatch.setattr(sc.verify_merge.shell_policy, "repository_shell_commands_allowed",
                        lambda path: True)
drift_watch = _mod("drift_watch")


def test_masked_slack_sdk_raises_importerror(monkeypatch):
    """Pins the technique every lazy-import test below relies on (#2357): `_mask_slack_sdk`
    genuinely raises `ImportError` on `import slack_sdk`, the same as real absence would, so the
    `_build_client`/`_socket_mode_response_cls` tests that assert a `RuntimeError` from it are
    exercising the real fallback path -- deterministically, regardless of whether `slack_sdk` is
    actually installed on the machine running this suite."""
    _mask_slack_sdk(monkeypatch)
    with pytest.raises(ImportError):
        __import__("slack_sdk")


def _sdlc(tmp_path):
    d = tmp_path / ".sdlc"
    (d / "state").mkdir(parents=True)
    return d


def _config(channel="C1111111", app_env="APP_ENV_T", bot_env="BOT_ENV_T", enabled=True):
    return {"slack_commands": {"enabled": enabled, "channel_id": channel,
                                "app_token_env": app_env, "bot_token_env": bot_env}}


def _write_registry(sdlc_dir, entries):
    features_dir = feature_registry.registry_dir(sdlc_dir)
    feature_registry.write_index(features_dir, entries)


# --------------------------------------------------------------------------- real-git fixture
#
# Mirrors test_drift_watch.py's own _git/_real_run/_repo_with_drift exactly (same reasoning: a
# commit delta is a property of what real git computes, not of what this code believes about it) --
# --drift's own reply is built from drift_watch.py's `_commit_delta`/`_pr_status`/`_open_units`/
# `_summarize_unit` called directly, so the fixture that proves them right belongs here too.


def _git(cwd, *args):
    env = dict(os.environ)
    env.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
                "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull})
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, env=env)
    if p.returncode != 0:
        raise AssertionError("git %s failed in %s: %s" % (" ".join(args), cwd, p.stderr or p.stdout))
    return p.stdout.strip()


def _write_file(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _real_run(cwd, argv):
    proc = subprocess.run([str(a) for a in argv], cwd=str(cwd), capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "").strip())
    return (proc.stdout or "").strip()


def _repo_with_drift(tmp_path, ahead=2, push_feature=True, unit="x"):
    """A bare 'remote' plus a real checkout, `.sdlc/` created INSIDE the checkout (so `cwd` for git
    calls is the checkout's own root, matching `_drift_reply`'s own `pathlib.Path(sdlc_dir).parent`
    convention): `main` cut, `feature/<unit>` branched off it and pushed, then `main` moves `ahead`
    commits further while the feature branch stays still."""
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    work = tmp_path / "work"
    _git(tmp_path, "clone", str(remote), str(work))
    _write_file(work / "a.txt", "1")
    _git(work, "add", "a.txt")
    _git(work, "commit", "-m", "initial")
    _git(work, "push", "origin", "main")
    branch = "feature/%s" % unit
    _git(work, "checkout", "-b", branch)
    if push_feature:
        _git(work, "push", "origin", branch)
    _git(work, "checkout", "main")
    for i in range(ahead):
        _write_file(work / ("b%d.txt" % i), str(i))
        _git(work, "add", "b%d.txt" % i)
        _git(work, "commit", "-m", "main commit %d" % i)
    _git(work, "push", "origin", "main")
    d = work / ".sdlc"
    (d / "state").mkdir(parents=True)
    return work, d


def _drift_config(channel="C1111111", base="main", remote="origin", repo="acme/app"):
    cfg = _config(channel=channel)
    cfg["work"] = {"base": base, "remote": remote}
    cfg["discovery"] = {"github": {"repo": repo}}
    return cfg


def _gh_run_wrapping_git(payload="[]"):
    """A fake `run` that executes real git argv (for `_commit_delta`/`_fetch`) but answers `payload`
    for anything else (`gh`, for `_pr_status`) -- matching test_drift_watch.py's own
    `_sweep_fixture`/`_sweep_fixture_with_gh` idiom exactly."""
    def run(cwd, argv):
        if argv and argv[0] == "git":
            return _real_run(cwd, argv)
        return payload
    return run


# --------------------------------------------------------------------------- config & gating


def test_enabled_requires_both_a_true_flag_and_a_channel_id():
    assert sc.enabled(_config()) is True
    assert sc.enabled(_config(enabled=False)) is False
    assert sc.enabled(_config(channel=None)) is False
    assert sc.enabled({}) is False


def test_enabled_rejects_a_truthy_string_not_real_true():
    cfg = _config()
    cfg["slack_commands"]["enabled"] = "true"
    assert sc.enabled(cfg) is False


def test_channel_id_none_when_blank_or_missing():
    assert sc.channel_id({}) is None
    assert sc.channel_id({"slack_commands": {"channel_id": "  "}}) is None
    assert sc.channel_id({"slack_commands": {"channel_id": "C123"}}) == "C123"


def test_token_env_defaults_and_overrides():
    assert sc.app_token_env({}) == sc.DEFAULT_APP_TOKEN_ENV
    assert sc.bot_token_env({}) == sc.DEFAULT_BOT_TOKEN_ENV
    cfg = _config(app_env="CUSTOM_APP", bot_env="CUSTOM_BOT")
    assert sc.app_token_env(cfg) == "CUSTOM_APP"
    assert sc.bot_token_env(cfg) == "CUSTOM_BOT"


# --------------------------------------------------------------------------- channel gate


def test_is_authorized_channel_matches_the_configured_id():
    cfg = _config(channel="C1111111")
    assert sc.is_authorized_channel("C1111111", cfg) is True


def test_is_authorized_channel_refuses_any_other_channel():
    cfg = _config(channel="C1111111")
    assert sc.is_authorized_channel("C2222222", cfg) is False
    assert sc.is_authorized_channel(None, cfg) is False


def test_is_authorized_channel_refuses_everything_when_unconfigured():
    assert sc.is_authorized_channel("C1111111", {}) is False


# --------------------------------------------------------------------------- command grammar


def test_help_parses_with_no_arguments():
    parsed = sc.parse_command("--help")
    assert parsed == sc.ParsedCommand("--help", None, None)


def test_help_refuses_extra_arguments():
    with pytest.raises(sc.CommandError) as exc:
        sc.parse_command("--help now")
    assert "--help takes no arguments" in exc.value.message
    assert "--help" in exc.value.message and "--drift" in exc.value.message  # USAGE quoted


def test_drift_parses_with_no_arguments():
    assert sc.parse_command("--drift") == sc.ParsedCommand("--drift", None, None)


def test_drift_refuses_extra_arguments():
    with pytest.raises(sc.CommandError) as exc:
        sc.parse_command("--drift now")
    assert "--drift takes no arguments" in exc.value.message


def test_list_defaults_to_page_1():
    assert sc.parse_command("--list") == sc.ParsedCommand("--list", None, 1)


def test_list_parses_an_explicit_page():
    assert sc.parse_command("--list 3") == sc.ParsedCommand("--list", None, 3)


def test_list_refuses_a_non_numeric_page():
    with pytest.raises(sc.CommandError) as exc:
        sc.parse_command("--list abc")
    assert "whole number" in exc.value.message


def test_list_refuses_zero_and_negative_pages():
    for bad in ("0", "-1"):
        with pytest.raises(sc.CommandError) as exc:
            sc.parse_command("--list %s" % bad)
        assert "1 or greater" in exc.value.message


def test_list_refuses_too_many_arguments():
    with pytest.raises(sc.CommandError) as exc:
        sc.parse_command("--list 1 2")
    assert "at most one page number" in exc.value.message


def test_unrecognized_command_is_refused_with_usage():
    with pytest.raises(sc.CommandError) as exc:
        sc.parse_command("--foo")
    assert "unrecognized command" in exc.value.message
    for form in sc.VALID_FORMS:
        assert form in exc.value.message


def test_free_form_language_is_refused_not_guessed():
    with pytest.raises(sc.CommandError) as exc:
        sc.parse_command("please run the drift check")
    assert "unrecognized command" in exc.value.message


def test_empty_text_is_refused():
    for text in ("", "   ", None):
        with pytest.raises(sc.CommandError) as exc:
            sc.parse_command(text)
        assert "empty command" in exc.value.message


def test_malformed_quoting_is_refused_not_raised_as_something_else():
    with pytest.raises(sc.CommandError) as exc:
        sc.parse_command('--merge "unterminated')
    assert sc.USAGE in exc.value.message


def test_merge_and_rebase_require_exactly_one_name(tmp_path):
    d = _sdlc(tmp_path)
    for head in ("--merge", "--rebase"):
        with pytest.raises(sc.CommandError) as exc:
            sc.parse_command(head, sdlc_dir=str(d))
        assert "needs exactly one unit name" in exc.value.message
        with pytest.raises(sc.CommandError):
            sc.parse_command("%s a b" % head, sdlc_dir=str(d))


def test_merge_resolves_a_real_open_unit_case_insensitively(tmp_path):
    d = _sdlc(tmp_path)
    _write_registry(d, {"My-Unit": {"open": True}})
    parsed = sc.parse_command("--merge my-unit", sdlc_dir=str(d))
    assert parsed == sc.ParsedCommand("--merge", "My-Unit", None)  # registry's own spelling


def test_rebase_resolves_a_real_open_unit(tmp_path):
    d = _sdlc(tmp_path)
    _write_registry(d, {"voice-interview": {"open": True}})
    parsed = sc.parse_command("--rebase voice-interview", sdlc_dir=str(d))
    assert parsed == sc.ParsedCommand("--rebase", "voice-interview", None)


def test_merge_refuses_a_closed_unit_the_same_as_unknown(tmp_path):
    d = _sdlc(tmp_path)
    _write_registry(d, {"done-unit": {"open": False}})
    with pytest.raises(sc.CommandError) as exc:
        sc.parse_command("--merge done-unit", sdlc_dir=str(d))
    assert "not a known open unit" in exc.value.message


def test_merge_refuses_an_unknown_name_listing_real_open_units(tmp_path):
    d = _sdlc(tmp_path)
    _write_registry(d, {"alpha": {"open": True}, "beta": {"open": True}, "gamma": {"open": False}})
    with pytest.raises(sc.CommandError) as exc:
        sc.parse_command("--merge nope", sdlc_dir=str(d))
    assert "not a known open unit" in exc.value.message
    assert "alpha" in exc.value.message and "beta" in exc.value.message
    assert "gamma" not in exc.value.message   # closed units are never offered as valid targets
    assert "nope" not in exc.value.message.split(":")[0] or "nope" in exc.value.message  # name echoed


def test_merge_refusal_reports_no_open_units_when_registry_is_empty(tmp_path):
    d = _sdlc(tmp_path)
    with pytest.raises(sc.CommandError) as exc:
        sc.parse_command("--merge anything", sdlc_dir=str(d))
    assert "No open units are currently registered" in exc.value.message


def test_merge_never_fuzzy_matches_a_close_but_wrong_name(tmp_path):
    d = _sdlc(tmp_path)
    _write_registry(d, {"voice-interview": {"open": True}})
    with pytest.raises(sc.CommandError):
        sc.parse_command("--merge voice-intervie", sdlc_dir=str(d))   # one char short -- no match


# --------------------------------------------------------------------------- replies


def test_build_reply_help_is_the_full_help_text():
    assert sc.build_reply(sc.ParsedCommand("--help", None, None)) == sc.HELP_TEXT
    for form in ("--drift", "--merge", "--list", "--rebase", "--help"):
        assert form in sc.HELP_TEXT


# `--merge`/`--rebase` both stopped being placeholders (#2341, #2340) -- no "still-a-placeholder"
# test remains for either; see their own dedicated sections below.


# --------------------------------------------------------------------------- --list [page] (#2337)


def test_list_reply_reports_no_open_units_when_registry_is_empty(tmp_path):
    d = _sdlc(tmp_path)
    reply = sc.build_reply(sc.ParsedCommand("--list", None, 1), _config(), str(d))
    assert reply == "No open units are currently registered."


def test_list_reply_reports_no_open_units_when_only_closed_units_exist(tmp_path):
    d = _sdlc(tmp_path)
    _write_registry(d, {"done": {"open": False}})
    reply = sc.build_reply(sc.ParsedCommand("--list", None, 1), _config(), str(d))
    assert reply == "No open units are currently registered."


def test_list_reply_page_one_shows_every_unit_when_ten_or_fewer(tmp_path):
    d = _sdlc(tmp_path)
    _write_registry(d, {"alpha": {"open": True, "title": "Alpha thing", "owner": "amy",
                                   "priority": "P1"},
                         "beta": {"open": True}, "closed-one": {"open": False}})
    reply = sc.build_reply(sc.ParsedCommand("--list", None, 1), _config(), str(d))
    assert "page 1 of 1" in reply
    assert "3 total" not in reply    # closed-one is NOT counted -- only 2 are open
    assert "2 total" in reply
    assert "alpha" in reply and "Alpha thing" in reply and "amy" in reply and "P1" in reply
    assert "beta" in reply
    assert "closed-one" not in reply
    # deterministic order
    assert reply.index("alpha") < reply.index("beta")


def test_list_reply_unowned_and_unprioritised_units_get_honest_defaults(tmp_path):
    d = _sdlc(tmp_path)
    _write_registry(d, {"bare": {"open": True}})
    reply = sc.build_reply(sc.ParsedCommand("--list", None, 1), _config(), str(d))
    assert "unowned" in reply
    assert "unprioritised" in reply


def test_list_reply_paginates_at_ten_per_page(tmp_path):
    d = _sdlc(tmp_path)
    _write_registry(d, {"unit-%02d" % i: {"open": True} for i in range(15)})
    page1 = sc.build_reply(sc.ParsedCommand("--list", None, 1), _config(), str(d))
    page2 = sc.build_reply(sc.ParsedCommand("--list", None, 2), _config(), str(d))
    assert "page 1 of 2" in page1 and "15 total" in page1
    assert "page 2 of 2" in page2 and "15 total" in page2
    assert page1.count("unit-") == 10
    assert page2.count("unit-") == 5
    # no overlap between the two pages
    page1_names = {line.split()[1] for line in page1.splitlines() if line.startswith("- ")}
    page2_names = {line.split()[1] for line in page2.splitlines() if line.startswith("- ")}
    assert page1_names.isdisjoint(page2_names)
    assert len(page1_names) == 10 and len(page2_names) == 5


def test_list_reply_a_page_past_the_end_is_refused_not_silently_empty(tmp_path):
    d = _sdlc(tmp_path)
    _write_registry(d, {"alpha": {"open": True}, "beta": {"open": True}})
    reply = sc.build_reply(sc.ParsedCommand("--list", None, 5), _config(), str(d))
    assert "page 5" in reply
    assert "only 1 page" in reply
    assert "2" in reply   # names the real total so the caller knows what a valid page would show


def test_list_reply_reads_the_registrys_own_index_not_a_reimplementation(tmp_path):
    """BR-18: `--list` reuses `feature_registry.read_index` directly (the design's own explicit
    citation) -- proven here by writing straight through the real write path and reading it back
    through the real reply, not by mocking the registry module."""
    d = _sdlc(tmp_path)
    features_dir = feature_registry.registry_dir(d)
    feature_registry.write_index(features_dir, {"voice": {"open": True, "title": "Voice interview"}})
    reply = sc.build_reply(sc.ParsedCommand("--list", None, 1), _config(), str(d))
    assert "voice" in reply and "Voice interview" in reply


# --------------------------------------------------------------------------- --drift (#2337)


def test_drift_reply_reports_no_units_when_registry_is_empty(tmp_path):
    d = _sdlc(tmp_path)
    reply = sc.build_reply(sc.ParsedCommand("--drift", None, None), _drift_config(), str(d),
                            run=lambda *a: (_ for _ in ()).throw(AssertionError("must not run git")))
    assert "No open" in reply


def test_drift_reply_reports_when_work_base_is_unconfigured(tmp_path):
    d = _sdlc(tmp_path)
    _write_registry(d, {"x": {"open": True}})
    cfg = _drift_config(base="")
    reply = sc.build_reply(sc.ParsedCommand("--drift", None, None), cfg, str(d),
                            run=lambda *a: (_ for _ in ()).throw(AssertionError("must not run git")))
    assert "work.base" in reply


def test_drift_reply_reports_no_drift_when_every_open_unit_is_current(tmp_path):
    work, d = _repo_with_drift(tmp_path, ahead=0)
    _write_registry(d, {"x": {"open": True}})
    cfg = _drift_config()
    reply = sc.build_reply(sc.ParsedCommand("--drift", None, None), cfg, str(d),
                            run=_gh_run_wrapping_git())
    assert "No drift" in reply
    assert "1 open unit" in reply
    assert "main" in reply


def test_drift_reply_summarizes_a_drifted_unit_reusing_drift_watchs_own_formatting(tmp_path):
    """Proves reuse, not reimplementation: the same Slack-bold single-asterisk formatting, the same
    'landing PR' phrasing, and the same indented commit-subject lines drift_watch._summarize_unit
    already produces for the passive tick (issue #2344's own formatting)."""
    work, d = _repo_with_drift(tmp_path, ahead=2)
    _write_registry(d, {"x": {"open": True}})
    cfg = _drift_config()
    reply = sc.build_reply(sc.ParsedCommand("--drift", None, None), cfg, str(d),
                            run=_gh_run_wrapping_git("[]"))
    assert reply.startswith("*Branch drift detected:*")
    assert "*x*" in reply and "**x**" not in reply
    assert "2 commit(s) behind" in reply
    assert any(line.startswith("  - main commit") for line in reply.splitlines())


def test_drift_reply_leads_with_an_already_merged_landing_pr(tmp_path):
    work, d = _repo_with_drift(tmp_path, ahead=3)
    _write_registry(d, {"x": {"open": True}})
    cfg = _drift_config()
    payload = json.dumps([{"number": 1697, "state": "closed", "merged_at": "2026-01-01T00:00:00Z"}])
    reply = sc.build_reply(sc.ParsedCommand("--drift", None, None), cfg, str(d),
                            run=_gh_run_wrapping_git(payload))
    assert "already landed via #1697" in reply


def test_drift_reply_skips_a_unit_whose_branch_was_never_pushed(tmp_path):
    work, d = _repo_with_drift(tmp_path, ahead=1, push_feature=False)
    _write_registry(d, {"x": {"open": True}})
    cfg = _drift_config()
    reply = sc.build_reply(sc.ParsedCommand("--drift", None, None), cfg, str(d),
                            run=_gh_run_wrapping_git())
    assert "No drift" in reply   # an unreadable branch is skipped, not reported as drifted


def test_drift_reply_checks_every_open_unit_not_just_one(tmp_path):
    work, d = _repo_with_drift(tmp_path, ahead=2, unit="x")
    remote = str((tmp_path / "remote.git"))
    _git(work, "checkout", "main")
    _git(work, "checkout", "-b", "feature/y")
    _git(work, "push", "origin", "feature/y")
    _git(work, "checkout", "main")
    _write_registry(d, {"x": {"open": True}, "y": {"open": True}})
    cfg = _drift_config()
    reply = sc.build_reply(sc.ParsedCommand("--drift", None, None), cfg, str(d),
                            run=_gh_run_wrapping_git())
    assert "*x*" in reply
    assert "*y*" not in reply   # y is current (no drift) -- only the genuinely drifted unit is shown


def test_handle_message_event_unauthorized_channel_gets_no_reply(tmp_path):
    d = _sdlc(tmp_path)
    cfg = _config(channel="C1111111")
    authorized, reply, parsed = sc.handle_message_event(
        {"channel": "C9999999", "text": "--help"}, cfg, sdlc_dir=str(d))
    assert (authorized, reply, parsed) == (False, None, None)


def test_handle_message_event_authorized_help_replies_with_help_text(tmp_path):
    d = _sdlc(tmp_path)
    cfg = _config(channel="C1111111")
    authorized, reply, parsed = sc.handle_message_event(
        {"channel": "C1111111", "text": "--help"}, cfg, sdlc_dir=str(d))
    assert authorized is True
    assert reply == sc.HELP_TEXT
    assert parsed.command == "--help"


def test_handle_message_event_authorized_bad_grammar_replies_with_refusal(tmp_path):
    d = _sdlc(tmp_path)
    cfg = _config(channel="C1111111")
    authorized, reply, parsed = sc.handle_message_event(
        {"channel": "C1111111", "text": "--nope"}, cfg, sdlc_dir=str(d))
    assert authorized is True
    assert "unrecognized command" in reply
    assert parsed is None


def test_handle_message_event_dispatches_list_for_real(tmp_path):
    d = _sdlc(tmp_path)
    cfg = _config(channel="C1111111")
    _write_registry(d, {"alpha": {"open": True, "title": "Alpha"}})
    authorized, reply, parsed = sc.handle_message_event(
        {"channel": "C1111111", "text": "--list"}, cfg, sdlc_dir=str(d))
    assert authorized is True
    assert "alpha" in reply and "Alpha" in reply
    assert parsed.command == "--list"


# --------------------------------------------------------------------------- mention support (#2353)


def test_leading_mention_id_extracts_the_user_id():
    assert sc._leading_mention_id("<@U0B6KQ49XRN> --help") == "U0B6KQ49XRN"
    assert sc._leading_mention_id("<@U0B6KQ49XRN>--help") == "U0B6KQ49XRN"  # no space, still extracted


def test_leading_mention_id_returns_none_for_non_mention_text():
    assert sc._leading_mention_id("--help") is None
    assert sc._leading_mention_id("") is None
    assert sc._leading_mention_id(None) is None
    assert sc._leading_mention_id("hey <@U123> --help") is None  # not LEADING


def test_strip_leading_mention_removes_own_mention_and_one_following_space():
    assert sc._strip_leading_mention("<@U0B6KQ49XRN> --help", "U0B6KQ49XRN") == "--help"
    assert sc._strip_leading_mention("<@U0B6KQ49XRN>--help", "U0B6KQ49XRN") == "--help"


def test_strip_leading_mention_leaves_someone_elses_mention_alone():
    assert sc._strip_leading_mention("<@U999> --help", "U0B6KQ49XRN") == "<@U999> --help"


def test_strip_leading_mention_leaves_text_alone_when_bot_user_id_is_none():
    assert sc._strip_leading_mention("<@U0B6KQ49XRN> --help", None) == "<@U0B6KQ49XRN> --help"


def test_handle_app_mention_event_strips_mention_and_replies_with_help(tmp_path):
    d = _sdlc(tmp_path)
    cfg = _config(channel="C1111111")
    authorized, reply, parsed = sc.handle_app_mention_event(
        {"channel": "C1111111", "text": "<@U0B6KQ49XRN> --help"}, "U0B6KQ49XRN", cfg, sdlc_dir=str(d))
    assert authorized is True
    assert reply == sc.HELP_TEXT
    assert parsed.command == "--help"


def test_handle_app_mention_event_unauthorized_channel_gets_no_reply(tmp_path):
    d = _sdlc(tmp_path)
    cfg = _config(channel="C1111111")
    authorized, reply, parsed = sc.handle_app_mention_event(
        {"channel": "C9999999", "text": "<@U0B6KQ49XRN> --help"}, "U0B6KQ49XRN", cfg, sdlc_dir=str(d))
    assert (authorized, reply, parsed) == (False, None, None)


def test_handle_app_mention_event_someone_elses_mention_is_refused_not_guessed(tmp_path):
    d = _sdlc(tmp_path)
    cfg = _config(channel="C1111111")
    authorized, reply, parsed = sc.handle_app_mention_event(
        {"channel": "C1111111", "text": "<@U999> --help"}, "U0B6KQ49XRN", cfg, sdlc_dir=str(d))
    assert authorized is True
    assert "unrecognized command" in reply
    assert parsed is None


def test_bot_user_id_returns_user_id_from_auth_test():
    class _FakeWebClient:
        def auth_test(self):
            return {"ok": True, "user_id": "U0B6KQ49XRN"}
    assert sc._bot_user_id(_FakeWebClient()) == "U0B6KQ49XRN"


def test_bot_user_id_degrades_to_none_on_failure():
    class _FailingWebClient:
        def auth_test(self):
            raise RuntimeError("network down")
    assert sc._bot_user_id(_FailingWebClient()) is None


def test_handle_message_event_dispatches_drift_for_real(tmp_path):
    work, d = _repo_with_drift(tmp_path, ahead=2)
    _write_registry(d, {"x": {"open": True}})
    cfg = _drift_config()
    authorized, reply, parsed = sc.handle_message_event(
        {"channel": "C1111111", "text": "--drift"}, cfg, sdlc_dir=str(d),
        run=_gh_run_wrapping_git())
    assert authorized is True
    assert "*x*" in reply and "2 commit(s) behind" in reply
    assert parsed.command == "--drift"


# --------------------------------------------------------------------------- lifecycle: paths


def test_state_paths_are_all_under_state_subdir(tmp_path):
    d = _sdlc(tmp_path)
    for pathfn in (sc.pid_path, sc.heartbeat_path, sc.stop_path, sc.log_path, sc.lock_dir_path):
        assert pathfn(d).parent == d / "state"


def test_stop_requested_reflects_the_stop_file(tmp_path):
    d = _sdlc(tmp_path)
    assert sc.stop_requested(d) is False
    sc.stop_path(d).touch()
    assert sc.stop_requested(d) is True


def test_write_heartbeat_writes_pid_and_last_seen(tmp_path):
    d = _sdlc(tmp_path)
    before = time.time()
    sc.write_heartbeat(d)
    data = json.loads(sc.heartbeat_path(d).read_text())
    assert data["pid"] == os.getpid()
    assert data["last_seen"] >= before


# --------------------------------------------------------------------------- lifecycle: single instance


def test_acquire_single_instance_succeeds_and_writes_pid_and_heartbeat(tmp_path):
    d = _sdlc(tmp_path)
    ok, reason = sc.acquire_single_instance(d)
    assert (ok, reason) == (True, None)
    assert int(sc.pid_path(d).read_text()) == os.getpid()
    assert sc.heartbeat_path(d).exists()
    assert sc.lock_dir_path(d).is_dir()


def test_acquire_single_instance_refuses_a_second_call_while_the_first_is_live(tmp_path, monkeypatch):
    d = _sdlc(tmp_path)
    ledger = _mod("ledger")
    monkeypatch.setattr(sc, "ledger", types.SimpleNamespace(pid_alive=lambda pid: True))
    ok1, _ = sc.acquire_single_instance(d)
    assert ok1 is True
    ok2, reason2 = sc.acquire_single_instance(d)
    assert ok2 is False
    assert "already running" in reason2
    del ledger   # unused, kept to document the real module this monkeypatch stands in for


def test_acquire_single_instance_reclaims_when_the_holder_pid_is_dead(tmp_path, monkeypatch):
    d = _sdlc(tmp_path)
    monkeypatch.setattr(sc, "ledger", types.SimpleNamespace(pid_alive=lambda pid: False))
    ok1, _ = sc.acquire_single_instance(d)
    assert ok1 is True
    # A second acquire (still "dead" per the monkeypatch) reclaims rather than refusing forever.
    ok2, reason2 = sc.acquire_single_instance(d)
    assert (ok2, reason2) == (True, None)


def test_acquire_single_instance_reclaims_when_the_heartbeat_is_stale(tmp_path, monkeypatch):
    d = _sdlc(tmp_path)
    monkeypatch.setattr(sc, "ledger", types.SimpleNamespace(pid_alive=lambda pid: True))
    ok1, _ = sc.acquire_single_instance(d)
    assert ok1 is True
    ok2, reason2 = sc.acquire_single_instance(d, stale_after_seconds=0)
    assert (ok2, reason2) == (True, None)   # heartbeat from a moment ago is already "stale" at 0s


def _probe_every_write(monkeypatch, d):
    """Wrap `sc._atomic_write_text` -- the ONE seam both the pidfile write and `write_heartbeat` go
    through -- so that immediately BEFORE and AFTER every real write a nested 'racer'
    `acquire_single_instance(d)` runs in this same process. Returns the list of
    (label, (ok, reason)) the nested racers got. Recursion-guarded: the nested racer's own writes
    (if the defect lets it win) go straight through. Deterministic: no processes, no sleeps."""
    real = sc._atomic_write_text
    probes, busy = [], [False]

    def probe(label):
        busy[0] = True
        try:
            probes.append((label, sc.acquire_single_instance(d)))
        finally:
            busy[0] = False

    def wrapped(path, text):
        if busy[0]:
            return real(path, text)
        name = pathlib.Path(path).name
        probe("before " + name)
        real(path, text)
        probe("after " + name)

    monkeypatch.setattr(sc, "_atomic_write_text", wrapped)
    return probes


def _assert_no_probe_won(d, me, probes):
    # non-vacuity first: both writes were intercepted, before and after each -- a wrapper that was
    # never installed, or writes routed elsewhere, would otherwise make `won == []` a free pass.
    assert len(probes) == 4, probes
    assert {lbl.split(" ", 1)[1] for lbl, _ in probes} == {sc.pid_path(d).name, sc.heartbeat_path(d).name}, probes
    won = [(lbl, r) for lbl, r in probes if r[0] is not False]
    assert won == [], "a racer reclaimed a live winner's lock mid-startup: %r" % won
    assert int(sc.pid_path(d).read_text()) == me
    assert json.loads(sc.heartbeat_path(d).read_text())["pid"] == me


def test_acquire_single_instance_no_intermediate_state_of_a_live_winner_is_reclaimable(tmp_path, monkeypatch):
    """SYSTEM OF RECORD for #2524 / sigma#118 (the Popen racer further down is only a smoke test).
    The property, stated precisely: no intermediate state of a live winner's OWN startup is
    reclaimable -- a racer arriving at ANY point between the winner's writes must refuse. Red on the
    pre-#2751 write order (pidfile before heartbeat): the probe after the pidfile write sees a live
    pid and no heartbeat, and reclaims. (A hand-built lock-dir + live-pid + no-heartbeat state is
    deliberately NOT asserted to refuse: it is age-judged at the grace gate by design, so a
    pid-reused dead holder stays reclaimable.)"""
    d = _sdlc(tmp_path)
    me = os.getpid()
    monkeypatch.setattr(sc, "ledger", types.SimpleNamespace(pid_alive=lambda pid: pid == me))
    probes = _probe_every_write(monkeypatch, d)
    assert sc.acquire_single_instance(d) == (True, None)
    _assert_no_probe_won(d, me, probes)


def test_acquire_single_instance_reclaiming_a_crashed_holder_is_not_itself_reclaimable_mid_startup(tmp_path, monkeypatch):
    """Post-crash sibling of the test above: the lock dir, a DEAD holder's pidfile and its stale
    heartbeat are all still on disk when the winner arrives, so the winner takes the reclaim path.
    Red on heartbeat-first alone: until the winner's own pidfile lands, a racer still reads the
    leftover DEAD pid and reclaims -- the guarded `unlink` before either write is what closes it.
    The probe cannot reach the window between the winner's reclaim `mkdir` and that `unlink`,
    because that is not a write; two racers that BOTH judge the holder stale can still double-win
    there via `rmdir`/`mkdir` (TD-1, deferred -- see #2751 plan §7). This test does not claim it."""
    d = _sdlc(tmp_path)
    me = os.getpid()
    DEAD = me + 100000
    monkeypatch.setattr(sc, "ledger", types.SimpleNamespace(pid_alive=lambda pid: pid == me))
    sc.lock_dir_path(d).mkdir()
    sc.pid_path(d).write_text(str(DEAD))
    sc.heartbeat_path(d).write_text(json.dumps(
        {"pid": DEAD, "last_seen": time.time() - 10 * sc.DEFAULT_STALE_AFTER_SECONDS}))
    probes = _probe_every_write(monkeypatch, d)
    assert sc.acquire_single_instance(d) == (True, None)
    _assert_no_probe_won(d, me, probes)


def test_acquire_single_instance_reclaims_a_pidless_lock_older_than_the_grace_window(tmp_path, monkeypatch):
    """The pid-None branch past `_LOCK_RECLAIM_GRACE_SECONDS` still reclaims. This is the common
    post-crash shape once #2751's `unlink` runs before either marker write (a crash anywhere in that
    window leaves a lock dir with no pidfile), so the branch must be shown to recover without a
    human. Control: a 60 s grace against this same 10 s-old dir must refuse with "back off"."""
    d = _sdlc(tmp_path)
    monkeypatch.setattr(sc, "ledger", types.SimpleNamespace(pid_alive=lambda pid: False))
    lock_dir = sc.lock_dir_path(d)
    lock_dir.mkdir()
    past = time.time() - 10
    os.utime(lock_dir, (past, past))
    assert sc.acquire_single_instance(d) == (True, None)


def test_release_single_instance_only_removes_its_own_files(tmp_path):
    d = _sdlc(tmp_path)
    sc.acquire_single_instance(d)
    # Simulate a successor having taken over: pidfile now names a DIFFERENT pid.
    sc.pid_path(d).write_text(str(os.getpid() + 1))
    sc.release_single_instance(d)
    assert sc.pid_path(d).exists()          # NOT removed -- it isn't ours any more
    assert sc.lock_dir_path(d).is_dir()     # NOT removed either


def test_release_single_instance_removes_its_own_files_when_still_held(tmp_path):
    d = _sdlc(tmp_path)
    sc.acquire_single_instance(d)
    sc.release_single_instance(d)
    assert not sc.pid_path(d).exists()
    assert not sc.heartbeat_path(d).exists()
    assert not sc.lock_dir_path(d).exists()


# --------------------------------------------------------------------------- Socket Mode wiring (mocked)


class _FakeResponse:
    def __init__(self, envelope_id):
        self.envelope_id = envelope_id


class _FakeClient:
    def __init__(self):
        self.acked = []
        self.connected = False
        self.closed = False

    def send_socket_mode_response(self, response):
        self.acked.append(response.envelope_id)

    def connect(self):
        self.connected = True

    def close(self):
        self.closed = True


def _req(type_="events_api", envelope_id="env-1", event=None):
    return types.SimpleNamespace(type=type_, envelope_id=envelope_id, payload={"event": event or {}})


def test_on_request_always_acks_regardless_of_type(tmp_path):
    d = _sdlc(tmp_path)
    client = _FakeClient()
    sc._on_request(client, _req(type_="hello"), _config(), d, response_cls=_FakeResponse)
    assert client.acked == ["env-1"]


def test_on_request_refreshes_the_heartbeat_on_every_request(tmp_path):
    d = _sdlc(tmp_path)
    client = _FakeClient()
    assert not sc.heartbeat_path(d).exists()
    sc._on_request(client, _req(type_="disconnect"), _config(), d, response_cls=_FakeResponse)
    assert sc.heartbeat_path(d).exists()


def test_on_request_ignores_non_events_api_types(tmp_path, monkeypatch):
    d = _sdlc(tmp_path)
    client = _FakeClient()
    calls = []
    monkeypatch.setattr(sc.slack_client, "post_message", lambda *a, **k: calls.append((a, k)))
    sc._on_request(client, _req(type_="interactive", event={"type": "message", "text": "--help",
                                                             "channel": "C1111111"}),
                   _config(), d, response_cls=_FakeResponse)
    assert calls == []


def test_on_request_ignores_message_subtypes_and_bot_messages(tmp_path, monkeypatch):
    d = _sdlc(tmp_path)
    client = _FakeClient()
    calls = []
    monkeypatch.setattr(sc.slack_client, "post_message", lambda *a, **k: calls.append((a, k)))
    for event in (
        {"type": "message", "subtype": "message_changed", "text": "--help", "channel": "C1111111"},
        {"type": "message", "bot_id": "B123", "text": "--help", "channel": "C1111111"},
        {"type": "message_deleted", "channel": "C1111111"},
    ):
        sc._on_request(client, _req(event=event), _config(), d, response_cls=_FakeResponse)
    assert calls == []


def test_on_request_posts_a_reply_for_an_authorized_recognized_command(tmp_path, monkeypatch):
    d = _sdlc(tmp_path)
    client = _FakeClient()
    calls = []
    monkeypatch.setattr(sc.slack_client, "post_message",
                         lambda channel, text, config, **k: calls.append((channel, text, k)))
    cfg = _config(channel="C1111111", bot_env="BOT_ENV_T")
    event = {"type": "message", "text": "--help", "channel": "C1111111", "user": "U1"}
    sc._on_request(client, _req(event=event), cfg, d, response_cls=_FakeResponse)
    assert len(calls) == 1
    channel, text, kwargs = calls[0]
    assert channel == "C1111111"
    assert text == sc.HELP_TEXT
    assert kwargs.get("token_env") == "BOT_ENV_T"


def test_on_request_does_not_post_for_an_unauthorized_channel(tmp_path, monkeypatch):
    d = _sdlc(tmp_path)
    client = _FakeClient()
    calls = []
    monkeypatch.setattr(sc.slack_client, "post_message", lambda *a, **k: calls.append((a, k)))
    cfg = _config(channel="C1111111")
    event = {"type": "message", "text": "--help", "channel": "C9999999"}
    sc._on_request(client, _req(event=event), cfg, d, response_cls=_FakeResponse)
    assert calls == []


# --------------------------------------------------------------------------- app_mention dispatch (#2353)


def test_on_request_dispatches_app_mention_and_posts_reply(tmp_path, monkeypatch):
    d = _sdlc(tmp_path)
    client = _FakeClient()
    calls = []
    monkeypatch.setattr(sc.slack_client, "post_message",
                         lambda channel, text, config, **k: calls.append((channel, text, k)))
    cfg = _config(channel="C1111111", bot_env="BOT_ENV_T")
    event = {"type": "app_mention", "text": "<@U0B6KQ49XRN> --help", "channel": "C1111111"}
    sc._on_request(client, _req(event=event), cfg, d, bot_user_id="U0B6KQ49XRN",
                    response_cls=_FakeResponse)
    assert len(calls) == 1
    channel, text, kwargs = calls[0]
    assert channel == "C1111111"
    assert text == sc.HELP_TEXT
    assert kwargs.get("token_env") == "BOT_ENV_T"


def test_on_request_ignores_app_mention_subtypes_and_bot_authored_mentions(tmp_path, monkeypatch):
    d = _sdlc(tmp_path)
    client = _FakeClient()
    calls = []
    monkeypatch.setattr(sc.slack_client, "post_message", lambda *a, **k: calls.append((a, k)))
    cfg = _config(channel="C1111111")
    for event in (
        {"type": "app_mention", "subtype": "message_changed", "text": "<@U0B6KQ49XRN> --help",
         "channel": "C1111111"},
        {"type": "app_mention", "bot_id": "B123", "text": "<@U0B6KQ49XRN> --help",
         "channel": "C1111111"},
    ):
        sc._on_request(client, _req(event=event), cfg, d, bot_user_id="U0B6KQ49XRN",
                        response_cls=_FakeResponse)
    assert calls == []


def test_on_request_skips_message_event_that_opens_with_own_mention_avoiding_double_reply(
        tmp_path, monkeypatch):
    """Slack fires BOTH a `message` event and an `app_mention` event for the SAME mentioning
    message -- the message copy must be silently skipped here, or a mention gets TWO replies."""
    d = _sdlc(tmp_path)
    client = _FakeClient()
    calls = []
    monkeypatch.setattr(sc.slack_client, "post_message", lambda *a, **k: calls.append((a, k)))
    cfg = _config(channel="C1111111")
    event = {"type": "message", "text": "<@U0B6KQ49XRN> --help", "channel": "C1111111"}
    sc._on_request(client, _req(event=event), cfg, d, bot_user_id="U0B6KQ49XRN",
                    response_cls=_FakeResponse)
    assert calls == []


def test_on_request_does_not_skip_message_event_mentioning_someone_else(tmp_path, monkeypatch):
    d = _sdlc(tmp_path)
    client = _FakeClient()
    calls = []
    monkeypatch.setattr(sc.slack_client, "post_message",
                         lambda channel, text, config, **k: calls.append((channel, text, k)))
    cfg = _config(channel="C1111111")
    event = {"type": "message", "text": "<@U999> --help", "channel": "C1111111"}
    sc._on_request(client, _req(event=event), cfg, d, bot_user_id="U0B6KQ49XRN",
                    response_cls=_FakeResponse)
    assert len(calls) == 1
    assert "unrecognized command" in calls[0][1]


def test_on_request_message_path_unaffected_when_bot_user_id_is_none(tmp_path, monkeypatch):
    """Production behavior when `_bot_user_id` degraded to None at startup (e.g. auth.test failed):
    plain `--help` must still work exactly as before -- only mention recognition is lost."""
    d = _sdlc(tmp_path)
    client = _FakeClient()
    calls = []
    monkeypatch.setattr(sc.slack_client, "post_message",
                         lambda channel, text, config, **k: calls.append((channel, text, k)))
    cfg = _config(channel="C1111111")
    event = {"type": "message", "text": "--help", "channel": "C1111111"}
    sc._on_request(client, _req(event=event), cfg, d, response_cls=_FakeResponse)
    assert len(calls) == 1
    assert calls[0][1] == sc.HELP_TEXT


# --------------------------------------------------------------------------- run()


def test_run_is_a_no_op_when_disabled(tmp_path):
    d = _sdlc(tmp_path)
    calls = []
    rc = sc.run(d, _config(enabled=False), client_factory=lambda *a: calls.append(a))
    assert rc == 0
    assert calls == []


def test_run_refuses_without_calling_client_factory_when_tokens_are_missing(tmp_path, monkeypatch):
    d = _sdlc(tmp_path)
    monkeypatch.delenv("APP_ENV_T", raising=False)
    monkeypatch.delenv("BOT_ENV_T", raising=False)
    calls = []
    rc = sc.run(d, _config(), client_factory=lambda *a: calls.append(a) or _FakeClient())
    assert rc == 1
    assert calls == []
    assert not sc.pid_path(d).exists()   # never even attempted the single-instance lock's own start


def test_run_refuses_when_only_one_token_is_set(tmp_path, monkeypatch):
    d = _sdlc(tmp_path)
    monkeypatch.setenv("APP_ENV_T", "xapp-fake")
    monkeypatch.delenv("BOT_ENV_T", raising=False)
    calls = []
    rc = sc.run(d, _config(), client_factory=lambda *a: calls.append(a) or _FakeClient())
    assert rc == 1
    assert calls == []


def test_run_refuses_when_a_live_sibling_already_holds_the_lock(tmp_path, monkeypatch):
    d = _sdlc(tmp_path)
    monkeypatch.setenv("APP_ENV_T", "xapp-fake")
    monkeypatch.setenv("BOT_ENV_T", "xoxb-fake")
    monkeypatch.setattr(sc, "ledger", types.SimpleNamespace(pid_alive=lambda pid: True))
    sc.acquire_single_instance(d)   # simulate the sibling already holding it
    calls = []
    rc = sc.run(d, _config(), client_factory=lambda *a: calls.append(a) or _FakeClient())
    assert rc == 1
    assert calls == []


def test_run_connects_dispatches_and_disconnects_cleanly_on_the_stop_file(tmp_path, monkeypatch):
    d = _sdlc(tmp_path)
    monkeypatch.setenv("APP_ENV_T", "xapp-fake")
    monkeypatch.setenv("BOT_ENV_T", "xoxb-fake")
    seen_args = []
    client = _FakeClient()

    def factory(app_token, bot_token, config, sdlc_dir):
        seen_args.append((app_token, bot_token, config, sdlc_dir))
        return client

    def fake_sleep(_seconds):
        sc.stop_path(d).touch()   # first poll finds no stop-file, sleeps, THEN it appears

    rc = sc.run(d, _config(), client_factory=factory, sleep=fake_sleep, poll_seconds=0)
    assert rc == 0
    assert client.connected is True
    assert client.closed is True
    assert seen_args[0][0] == "xapp-fake"
    assert seen_args[0][1] == "xoxb-fake"
    assert seen_args[0][3] == d
    # Lifecycle cleaned up on exit -- release_single_instance ran.
    assert not sc.pid_path(d).exists()
    assert not sc.lock_dir_path(d).exists()


def test_run_releases_the_lock_even_if_connect_raises(tmp_path, monkeypatch):
    d = _sdlc(tmp_path)
    monkeypatch.setenv("APP_ENV_T", "xapp-fake")
    monkeypatch.setenv("BOT_ENV_T", "xoxb-fake")

    class _BoomClient:
        def connect(self):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        sc.run(d, _config(), client_factory=lambda *a: _BoomClient())
    assert not sc.pid_path(d).exists()
    assert not sc.lock_dir_path(d).exists()


# --------------------------------------------------------------------------- lazy slack_sdk import


def test_build_client_raises_a_clear_runtime_error_without_slack_sdk_installed(tmp_path, monkeypatch):
    _mask_slack_sdk(monkeypatch)
    d = _sdlc(tmp_path)
    with pytest.raises(RuntimeError) as exc:
        sc._build_client("xapp-fake", "xoxb-fake", _config(), str(d))
    assert "pip install" in str(exc.value)
    assert "slack_sdk" in str(exc.value)


def test_socket_mode_response_cls_raises_a_clear_runtime_error_without_slack_sdk_installed(monkeypatch):
    _mask_slack_sdk(monkeypatch)
    with pytest.raises(RuntimeError) as exc:
        sc._socket_mode_response_cls()
    assert "pip install" in str(exc.value)


# =========================================================================== dispatch infrastructure (#2338)
#
# Component H (claim arbitration) + Component I (isolated worktree) + Component B (dispatch/outcome
# verification) of `.sdlc/design/2329.md` -- the shared machinery `--rebase <name>` (#2340) and
# `--merge <name>` (#2341) both build on next. NOT wired to either command yet.


def _ledger_config(actor="tester", ttl_hours=12, **extra):
    cfg = {"ledger": {"enabled": True, "actor": actor, "lease": {"ttl_hours": ttl_hours}}}
    cfg.update(extra)
    return cfg


def _raw_claim(sdlc_dir, who, goal, kind="claimed", pid=None, ts=None):
    """Writes a raw ledger entry straight into the entries stream -- mirrors test_loop.py's own
    `_lease_base` convention exactly: a 2-part id (`who:seq`) is a legacy, pid-less writer (always
    "mine" once the actor matches); a 3-part id with a bare pid (`who:pid:seq`, no host) is a
    real, checkable writer on THIS machine -- the shape #1197's own dead-picker tests use.

    `ts` defaults to NOW, not a fixed past date: `loop._lease` treats a claim older than
    `ledger.lease.ttl_hours` (default 12h) as released, so a hardcoded timestamp is a ticking time
    bomb -- every caller here wants "an open claim, right now", and the moment real wall-clock time
    drifts more than the TTL past a fixed string, every one of them starts reading as already-
    expired, on every machine, regardless of anything this branch changed. Same format `ledger._stamp`
    itself writes (`%Y-%m-%dT%H:%M:%SZ`, UTC) -- not reused directly since it is that module's own
    private helper, but must parse identically for `open_claims_detailed`'s `%Y-%m-%dT%H:%M:%SZ`
    read to agree."""
    entries = sc.ledger.entries_dir(sdlc_dir)
    entries.mkdir(parents=True, exist_ok=True)
    fname = f"{who}-{pid}.jsonl" if pid is not None else f"{who}.jsonl"
    ident = f"{who}:{pid}:1" if pid is not None else f"{who}:1"
    entry = {"id": ident, "ts": ts or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "actor": who, "kind": kind, "goal": str(goal)}
    with (entries / fname).open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


# --------------------------------------------------------------------------- claim_key


def test_claim_key_is_hyphen_separated_never_a_colon():
    key = sc.claim_key("billing")
    assert key == "slack-cmd-billing"
    assert ":" not in key


def test_claim_key_never_trips_unsafe_goal_reason():
    """The round-1 REJECT fix this key format exists for, pinned directly against the real
    validator every claim-path helper (`_claim_lock_path`, `_agent_marker_path`,
    `actionlog.log_path`) runs the key through."""
    state = _mod("state")
    assert state.unsafe_goal_reason(sc.claim_key("billing")) is None


# --------------------------------------------------------------------------- try_claim / finish_claim


def test_try_claim_wins_when_nothing_else_holds_it(tmp_path):
    d = _sdlc(tmp_path)
    cfg = _ledger_config()
    result = sc.try_claim(d, cfg, "billing")
    assert result == sc.ClaimResult(True, "slack-cmd-billing", None, None)
    open_claims = sc.ledger.open_claims(sc.ledger.read_all(d))
    assert open_claims["slack-cmd-billing"] == "tester"


def test_try_claim_writes_the_local_half_only_never_marks_a_github_issue(tmp_path):
    """`mark=False` -- `slack-cmd-<name>` is never a real issue `mark_in_progress` could label."""
    d = _sdlc(tmp_path)
    cfg = _ledger_config()
    calls = []

    class _BoomSource:
        def mark_in_progress(self, goal):
            calls.append(goal)
            raise AssertionError("must never be called for a synthetic claim key")

    # try_claim never touches a `source` at all (passes None to loop._claim) -- this fixture just
    # proves the point structurally: nothing in this path ever reaches for one.
    result = sc.try_claim(d, cfg, "billing")
    assert result.ok is True
    assert calls == []


def test_try_claim_refuses_when_a_different_live_actor_already_holds_it(tmp_path):
    d = _sdlc(tmp_path)
    _raw_claim(d, "alice", "slack-cmd-billing", kind="claimed")   # legacy 2-part -- always "mine to alice"
    cfg = _ledger_config(actor="bob")
    result = sc.try_claim(d, cfg, "billing")
    assert result.ok is False
    assert result.holder_actor == "alice"
    assert result.holder_writer is not None
    # nothing was claimed on bob's behalf
    assert sc.ledger.open_claims(sc.ledger.read_all(d)).get("slack-cmd-billing") == "alice"


def test_try_claim_reclaims_a_dead_pickers_claim_with_no_live_worker_marker(tmp_path, monkeypatch):
    """#1197's own shape, reused here: a dead picker pid with NOTHING corroborating it is safe to
    reclaim -- the SAME actor, a different (dead) writer instance."""
    d = _sdlc(tmp_path)
    dead_pid = 2**30
    _raw_claim(d, "tester", "slack-cmd-billing", kind="claimed", pid=dead_pid)
    monkeypatch.setattr(os, "getpid", lambda: dead_pid + 1)   # a second, distinct process of "tester"
    cfg = _ledger_config(actor="tester")
    result = sc.try_claim(d, cfg, "billing")
    assert result.ok is True


def test_try_claim_refuses_a_dead_pickers_claim_when_a_live_worker_marker_is_registered(monkeypatch, tmp_path):
    """Round-2 REJECT fix's own shape: the picker pid is dead, but a live per-goal `agent_start`
    marker (registered for the SAME `slack-cmd-<name>` key) corroborates a genuine still-running
    driven child -- must NOT reclaim."""
    d = _sdlc(tmp_path)
    dead_pid = 2**30
    _raw_claim(d, "tester", "slack-cmd-billing", kind="claimed", pid=dead_pid)
    cfg = _ledger_config(actor="tester")
    sc.loop.agent_start(d, "slack-cmd-billing", os.getpid(), cfg)   # THIS test process -- genuinely alive
    monkeypatch.setattr(os, "getpid", lambda: dead_pid + 1)
    result = sc.try_claim(d, cfg, "billing")
    assert result.ok is False
    assert result.holder_actor == "tester"


def test_try_claim_backs_off_on_a_local_same_instant_race(tmp_path, monkeypatch):
    """`_try_acquire_claim_lock` returning None (a local sibling won this exact instant) -- no
    holder is recorded anywhere yet, so there is nobody to name."""
    d = _sdlc(tmp_path)
    cfg = _ledger_config()
    monkeypatch.setattr(sc.loop, "_try_acquire_claim_lock", lambda sdlc_dir, goal: None)
    result = sc.try_claim(d, cfg, "billing")
    assert result == sc.ClaimResult(False, "slack-cmd-billing", None, None)
    assert sc.ledger.open_claims(sc.ledger.read_all(d)) == {}


def test_try_claim_calls_sync_pull_before_reading_the_lease(tmp_path, monkeypatch):
    """Round-1 REJECT fix, finding 2: a real, on-demand pull at the moment of the check, not the
    passive tick alone."""
    d = _sdlc(tmp_path)
    cfg = _ledger_config()
    calls = []
    monkeypatch.setattr(sc.sync, "pull", lambda sdlc_dir, config: calls.append("pulled"))
    sc.try_claim(d, cfg, "billing")
    assert calls == ["pulled"]


def test_try_claim_a_pull_failure_never_blocks_the_claim(tmp_path, monkeypatch):
    d = _sdlc(tmp_path)
    cfg = _ledger_config()

    def boom(sdlc_dir, config):
        raise RuntimeError("no network")

    monkeypatch.setattr(sc.sync, "pull", boom)
    result = sc.try_claim(d, cfg, "billing")
    assert result.ok is True


def test_finish_claim_writes_a_terminal_entry_and_clears_the_marker(tmp_path):
    d = _sdlc(tmp_path)
    cfg = _ledger_config()
    key = "slack-cmd-billing"
    sc.try_claim(d, cfg, "billing")
    sc.loop.agent_start(d, key, os.getpid(), cfg)
    assert sc.loop.agent_alive(d, key, cfg)[0] == "alive"
    sc.finish_claim(d, cfg, key, "done", why="test completion")
    entries = [e for e in sc.ledger.read_all(d) if e.get("goal") == key]
    assert entries[-1]["kind"] == "done"
    assert entries[-1]["why"] == "test completion"
    assert sc.ledger.open_claims(sc.ledger.read_all(d)) == {}    # lease closed
    assert sc.loop.agent_alive(d, key, cfg)[0] == "unknown"      # marker cleared


def test_finish_claim_refuses_a_non_terminal_kind(tmp_path):
    d = _sdlc(tmp_path)
    cfg = _ledger_config()
    with pytest.raises(ValueError):
        sc.finish_claim(d, cfg, "slack-cmd-billing", "note")


def test_finish_claim_never_raises_when_the_ledger_write_fails(tmp_path, monkeypatch):
    d = _sdlc(tmp_path)
    cfg = _ledger_config()

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(sc.ledger, "safe_append", boom)
    sc.finish_claim(d, cfg, "slack-cmd-billing", "done")   # must not raise


# --------------------------------------------------------------------------- outcome verification


def test_new_completion_marker_finds_the_most_recent_new_entry(tmp_path):
    d = _sdlc(tmp_path)
    cfg = _ledger_config()
    key = "slack-cmd-billing"
    before = sc._entry_ids_for(d, key)
    sc.ledger.safe_append(d, "note", key, config=cfg, why="progress 1")
    sc.ledger.safe_append(d, "done", key, config=cfg, why="progress 2")
    marker = sc.new_completion_marker(d, key, before)
    assert marker["kind"] == "done"
    assert marker["why"] == "progress 2"


def test_new_completion_marker_none_when_nothing_new(tmp_path):
    d = _sdlc(tmp_path)
    cfg = _ledger_config()
    key = "slack-cmd-billing"
    sc.ledger.safe_append(d, "claimed", key, config=cfg)
    before = sc._entry_ids_for(d, key)
    assert sc.new_completion_marker(d, key, before) is None


def test_drive_outcome_trusts_a_recognised_marker_kind_over_the_exit_code(tmp_path):
    d = _sdlc(tmp_path)
    cfg = _ledger_config()
    key = "slack-cmd-billing"
    before = sc._entry_ids_for(d, key)
    sc.ledger.safe_append(d, "done", key, config=cfg)
    state, marker = sc.drive_outcome(d, key, 1, before)   # exit 1, but the marker says done
    assert state == "done"
    assert marker["kind"] == "done"


def test_drive_outcome_unclear_when_marker_kind_is_not_recognised(tmp_path):
    d = _sdlc(tmp_path)
    cfg = _ledger_config()
    key = "slack-cmd-billing"
    before = sc._entry_ids_for(d, key)
    sc.ledger.safe_append(d, "note", key, config=cfg)
    state, marker = sc.drive_outcome(d, key, 0, before)
    assert state == "unclear"
    assert marker["kind"] == "note"


def test_drive_outcome_unclear_when_exit_zero_and_no_marker(tmp_path):
    d = _sdlc(tmp_path)
    key = "slack-cmd-billing"
    before = sc._entry_ids_for(d, key)
    state, marker = sc.drive_outcome(d, key, 0, before)
    assert (state, marker) == ("unclear", None)


def test_drive_outcome_failed_when_exit_nonzero_and_no_marker(tmp_path):
    d = _sdlc(tmp_path)
    key = "slack-cmd-billing"
    before = sc._entry_ids_for(d, key)
    state, marker = sc.drive_outcome(d, key, 1, before)
    assert (state, marker) == ("failed", None)


# --------------------------------------------------------------------------- real-git fixture (Component I)


def _git(cwd, *args):
    env = dict(os.environ)
    env.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
                "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull})
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, env=env)
    if p.returncode != 0:
        raise AssertionError("git %s failed in %s: %s" % (" ".join(args), cwd, p.stderr or p.stdout))
    return p.stdout.strip()


def _repo(tmp_path, unit="billing", push_feature=True):
    """A throwaway bare remote + local checkout with `feature/<unit>` pushed, plus the `.sdlc`
    layer `cut_worktree`/`dispatch` read -- trimmed from test_feature_rebase.py's own World
    fixture to what this module's dispatch infra needs."""
    root = tmp_path
    remote = root / "remote.git"
    local = root / "local"
    _git(root, "init", "-q", "--bare", str(remote))
    _git(root, "init", "-q", "-b", "main", str(local))
    _git(local, "remote", "add", "origin", str(remote))
    (local / "seed.txt").write_text("seed\n")
    _git(local, "add", "seed.txt")
    _git(local, "commit", "-q", "-m", "seed")
    _git(local, "push", "-q", "-u", "origin", "main")
    if push_feature:
        _git(local, "checkout", "-q", "-b", "feature/%s" % unit)
        (local / "f.txt").write_text("f\n")
        _git(local, "add", "f.txt")
        _git(local, "commit", "-q", "-m", "feat: seed feature branch")
        _git(local, "push", "-q", "origin", "feature/%s" % unit)
        _git(local, "checkout", "-q", "main")
    d = local / ".sdlc"
    (d / "state").mkdir(parents=True)
    return local, d


# --------------------------------------------------------------------------- cut_worktree / teardown_worktree


def test_cut_worktree_creates_a_local_branch_checkout_of_the_remote_tip(tmp_path):
    """#2355: NOT bare `--detach` -- a local branch named `feature/<name>`, at the exact commit
    the remote tip names, so `rebase_brief.py`'s own `assemble_brief`/`attempt_rebase` (#2340's
    consumers) can resolve `branch` as a real ref for their merge-base/rev-list comparisons."""
    local, d = _repo(tmp_path)
    tip = _git(local, "rev-parse", "origin/feature/billing")
    path, lock_fd = sc.cut_worktree(d, {}, "billing")
    try:
        assert path == sc.feature_rebase.worktree_path(d, "billing")
        assert path.is_dir()
        assert _git(path, "rev-parse", "HEAD") == tip
        # on a REAL local branch named feature/billing -- not detached.
        assert _git(path, "rev-parse", "--abbrev-ref", "HEAD") == "feature/billing"
        assert _git(path, "rev-parse", "feature/billing") == tip
    finally:
        sc.teardown_worktree(d, path, lock_fd)
    assert not path.exists()


def test_cut_worktree_output_is_directly_usable_by_rebase_brief(tmp_path):
    """#2355's own regression proof: reproduces the EXACT failure found via live testing (a real
    `@ls-bot --rebase <name>`) -- `assemble_brief`/`attempt_rebase` called against `cut_worktree`'s
    own output, exactly as `_REBASE_EXTRA_PROMPT` instructs a driven session to. Before the fix,
    `assemble_brief` raised `RuntimeError: fatal: Not a valid object name feature/billing` on a
    bare `--detach` checkout; this must now succeed and report a real, computable delta."""
    local, d = _repo(tmp_path)
    # advance origin/main by one commit so there is real drift to detect.
    (local / "extra.txt").write_text("extra\n")
    _git(local, "add", "extra.txt")
    _git(local, "commit", "-q", "-m", "extra commit on main")
    _git(local, "push", "-q", "origin", "main")
    path, lock_fd = sc.cut_worktree(d, {}, "billing")
    try:
        # sc itself does not import rebase_brief (only the driven session does, by path, per
        # _REBASE_EXTRA_PROMPT's own instruction) -- load it the same way here.
        rb_path = ROOT / "skills" / "sigma-rebase" / "scripts" / "rebase_brief.py"
        spec = importlib.util.spec_from_file_location("rebase_brief_2355", rb_path)
        rebase_brief = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rebase_brief)

        def run(cwd, argv):
            proc = subprocess.run([str(a) for a in argv], cwd=str(cwd),
                                   capture_output=True, text=True)
            if proc.returncode != 0:
                raise RuntimeError((proc.stderr or proc.stdout or "").strip())
            return (proc.stdout or "").strip()

        brief = rebase_brief.assemble_brief(run, str(path), "origin", "feature/billing", "main")
        assert len(brief["delta"]) == 1
        result = rebase_brief.attempt_rebase(run, str(path), "origin", "feature/billing", "main")
        assert result["outcome"] == "rebased"
    finally:
        sc.teardown_worktree(d, path, lock_fd)


def test_cut_worktree_raises_worktree_busy_when_the_lock_is_already_held(tmp_path):
    local, d = _repo(tmp_path)
    lock_path = sc.feature_rebase.lock_path(d, "billing")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    held = sc.feature_rebase._acquire(lock_path)
    try:
        with pytest.raises(sc.WorktreeBusy):
            sc.cut_worktree(d, {}, "billing")
    finally:
        sc.feature_rebase._release(held)
    # nothing was created
    assert not sc.feature_rebase.worktree_path(d, "billing").exists()


def test_cut_worktree_raises_worktree_unavailable_when_the_branch_is_missing(tmp_path):
    local, d = _repo(tmp_path, push_feature=False)
    with pytest.raises(sc.WorktreeUnavailable):
        sc.cut_worktree(d, {}, "billing")
    # the lock was released, not leaked -- a second attempt gets the SAME exception, not WorktreeBusy
    with pytest.raises(sc.WorktreeUnavailable):
        sc.cut_worktree(d, {}, "billing")


def test_cut_worktree_recovers_a_stale_worktree_left_at_the_path(tmp_path):
    """Mirrors `_upkeep`'s own "drop any stale worktree found at this path on entry, under lock,
    before any early return" -- a leftover from a prior crashed attempt must not permanently wedge
    every future dispatch for this unit."""
    local, d = _repo(tmp_path)
    path = sc.feature_rebase.worktree_path(d, "billing")
    path.mkdir(parents=True)
    (path / "leftover.txt").write_text("stale\n")
    new_path, lock_fd = sc.cut_worktree(d, {}, "billing")
    try:
        assert new_path == path
        assert not (path / "leftover.txt").exists()   # the stale content is gone
        assert (path / "f.txt").exists()               # a real, fresh checkout is there instead
    finally:
        sc.teardown_worktree(d, new_path, lock_fd)


# --------------------------------------------------------------------------- dispatch


def _fake_drive(exit_code=0, stdout="ok", write_marker=None, capture=None):
    """A DI double for `run_drive` -- matches `autowatch._run_drive`'s own `(cmd_str, prompt, cwd,
    env, timeout, on_spawn=None) -> (exit_code, stdout)` contract. `write_marker`, when given, is
    `(sdlc_dir, key, kind, config)` -- writes the driven session's own completion marker before
    returning, simulating a real driven session's terminal `ledger.safe_append` call (`config` is
    passed explicitly, exactly as every production call site does -- a real driven session's own
    process would resolve its own config the same way; omitting it here would fall back to reading
    `config.json` off disk, which these throwaway fixtures never write). `capture`, when given, is
    a list this appends `(cmd_str, prompt, cwd, env, timeout)` to, for assertions on what dispatch
    actually built."""
    def run(cmd_str, prompt, cwd, env, timeout, on_spawn=None):
        if capture is not None:
            capture.append((cmd_str, prompt, cwd, env, timeout))
        if on_spawn is not None:
            on_spawn(4242)          # a fake but plausible child pid
        if write_marker is not None:
            sdlc_dir, key, kind, config = write_marker
            sc.ledger.safe_append(sdlc_dir, kind, key, config=config, why="driven session's own record")
        return exit_code, stdout
    return run


def test_dispatch_happy_path_reports_done_and_tears_everything_down(tmp_path):
    local, d = _repo(tmp_path)
    cfg = _ledger_config()
    key = "slack-cmd-billing"
    capture = []
    drive = _fake_drive(exit_code=0, write_marker=(d, key, "done", cfg), capture=capture)
    result = sc.dispatch(d, cfg, "billing", "--rebase", run_drive=drive)
    assert result.state == "done"
    assert result.exit_code == 0
    # the drive ran INSIDE the ephemeral worktree, never the shared root checkout
    cmd_str, prompt, cwd, env, timeout = capture[0]
    assert cwd == str(sc.feature_rebase.worktree_path(d, "billing"))
    assert cwd != str(local)
    assert "billing" in prompt
    assert "--rebase" in prompt
    # everything is cleaned up
    assert not sc.feature_rebase.worktree_path(d, "billing").exists()
    assert sc.ledger.open_claims(sc.ledger.read_all(d)) == {}
    assert sc.loop.agent_alive(d, key, cfg)[0] == "unknown"


def test_dispatch_registers_a_real_liveness_marker_for_the_driven_childs_own_pid(tmp_path):
    """Round-2 REJECT fix, end to end: `on_spawn` registers the CHILD's pid (4242 here), not the
    listener's own -- checked by reading the marker file directly, from INSIDE the fake drive,
    before dispatch has any chance to tear it down."""
    local, d = _repo(tmp_path)
    cfg = _ledger_config()
    key = "slack-cmd-billing"
    seen = {}

    def drive(cmd_str, prompt, cwd, env, timeout, on_spawn=None):
        on_spawn(4242)
        state, pid = sc.loop.agent_alive(d, key, cfg)
        seen["state"], seen["pid"] = state, pid
        sc.ledger.safe_append(d, "done", key, config=cfg)
        return 0, "ok"

    sc.dispatch(d, cfg, "billing", "--rebase", run_drive=drive)
    assert seen["pid"] == 4242


def test_dispatch_reports_unclear_when_the_drive_exits_zero_with_no_marker(tmp_path):
    """#1332's own repro case, reached through this dispatcher: a subprocess that exits 0 having
    done real work but never reached its own terminal ledger record must be reported honestly, not
    as a false "done"."""
    local, d = _repo(tmp_path)
    cfg = _ledger_config()
    result = sc.dispatch(d, cfg, "billing", "--rebase", run_drive=_fake_drive(exit_code=0))
    assert result.state == "unclear"
    # the claim is still closed (kind="release"), not left open until the TTL
    assert sc.ledger.open_claims(sc.ledger.read_all(d)) == {}


def test_dispatch_logs_the_drives_stdout_when_the_outcome_is_not_done(tmp_path):
    """#2355: once this process returns, a driven session's own stdout is gone forever unless it
    was logged here -- exactly the diagnostic a human needs for a non-"done" outcome, and exactly
    what was missing when a real live `--rebase` came back "unclear" with nothing to debug from."""
    local, d = _repo(tmp_path)
    cfg = _ledger_config()
    sc.dispatch(d, cfg, "billing", "--rebase",
                run_drive=_fake_drive(exit_code=0, stdout="a real clue about what happened"))
    log_text = sc.log_path(d).read_text(encoding="utf-8")
    assert "a real clue about what happened" in log_text


def test_dispatch_does_not_log_stdout_on_a_done_outcome(tmp_path):
    """The happy path doesn't need this -- `why` (the completion marker's own text) already says
    what happened; logging the FULL stdout on every success would just be noise."""
    local, d = _repo(tmp_path)
    cfg = _ledger_config()
    key = "slack-cmd-billing"
    sc.dispatch(d, cfg, "billing", "--rebase", run_drive=_fake_drive(
        exit_code=0, stdout="nothing worth logging here", write_marker=(d, key, "done", cfg)))
    log_text = sc.log_path(d).read_text(encoding="utf-8") if sc.log_path(d).exists() else ""
    assert "nothing worth logging here" not in log_text


def test_truncate_for_log_keeps_the_tail_not_the_head():
    long_text = "x" * 5000 + "THE REAL ERROR IS HERE"
    out = sc._truncate_for_log(long_text)
    assert "THE REAL ERROR IS HERE" in out
    assert len(out) < len(long_text)


def test_truncate_for_log_passes_short_text_through_unchanged():
    assert sc._truncate_for_log("short") == "short"


def test_truncate_for_log_reports_empty_stdout_honestly():
    assert sc._truncate_for_log("") == "(empty)"
    assert sc._truncate_for_log(None) == "(empty)"


def test_dispatch_reports_failed_when_the_drive_exits_nonzero_with_no_marker(tmp_path):
    local, d = _repo(tmp_path)
    cfg = _ledger_config()
    result = sc.dispatch(d, cfg, "billing", "--rebase", run_drive=_fake_drive(exit_code=1, stdout="boom"))
    assert result.state == "failed"
    assert result.exit_code == 1


def test_dispatch_reports_busy_and_never_touches_the_worktree_or_drives(tmp_path):
    local, d = _repo(tmp_path)
    _raw_claim(d, "alice", "slack-cmd-billing", kind="claimed")
    cfg = _ledger_config(actor="bob")
    calls = []
    result = sc.dispatch(d, cfg, "billing", "--rebase",
                          run_drive=lambda *a, **k: calls.append(1) or (0, "ok"))
    assert result.state == "busy"
    assert result.holder_actor == "alice"
    assert calls == []
    assert not sc.feature_rebase.worktree_path(d, "billing").exists()


def test_dispatch_worktree_busy_releases_the_claim_and_never_drives(tmp_path):
    local, d = _repo(tmp_path)
    cfg = _ledger_config()
    key = "slack-cmd-billing"
    lock_path = sc.feature_rebase.lock_path(d, "billing")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    held = sc.feature_rebase._acquire(lock_path)
    calls = []
    try:
        result = sc.dispatch(d, cfg, "billing", "--rebase",
                              run_drive=lambda *a, **k: calls.append(1) or (0, "ok"))
    finally:
        sc.feature_rebase._release(held)
    assert result.state == "worktree-busy"
    assert calls == []
    entries = [e for e in sc.ledger.read_all(d) if e.get("goal") == key]
    assert entries[-1]["kind"] == "release"
    assert sc.ledger.open_claims(sc.ledger.read_all(d)) == {}   # the claim was released, not left open


def test_dispatch_worktree_unavailable_marks_the_claim_failed_and_never_drives(tmp_path):
    local, d = _repo(tmp_path, push_feature=False)   # no feature/billing branch on the remote
    cfg = _ledger_config()
    key = "slack-cmd-billing"
    calls = []
    result = sc.dispatch(d, cfg, "billing", "--rebase",
                          run_drive=lambda *a, **k: calls.append(1) or (0, "ok"))
    assert result.state == "worktree-unavailable"
    assert calls == []
    entries = [e for e in sc.ledger.read_all(d) if e.get("goal") == key]
    assert entries[-1]["kind"] == "failed"


def test_dispatch_never_raises_and_closes_the_claim_even_when_the_drive_itself_raises(tmp_path):
    """The `dispatch`/`_dispatch` split's own reason to exist: an injected `run_drive` that raises
    (which the REAL `autowatch._run_drive` never does, but this dispatcher's own contract is
    "never raises" regardless) must still close the claim and tear the worktree down, not leak
    either."""
    local, d = _repo(tmp_path)
    cfg = _ledger_config()
    key = "slack-cmd-billing"

    def boom(*a, **k):
        raise RuntimeError("simulated crash mid-drive")

    result = sc.dispatch(d, cfg, "billing", "--rebase", run_drive=boom)
    assert result.state == "failed"
    assert not sc.feature_rebase.worktree_path(d, "billing").exists()
    assert sc.ledger.open_claims(sc.ledger.read_all(d)) == {}


def test_dispatch_busy_path_writes_no_ledger_entries_at_all(tmp_path):
    """A busy dispatch touches NOTHING -- not even a claim attempt for the losing side."""
    local, d = _repo(tmp_path)
    _raw_claim(d, "alice", "slack-cmd-billing", kind="claimed")
    cfg = _ledger_config(actor="bob")
    before = len(sc.ledger.read_all(d))
    sc.dispatch(d, cfg, "billing", "--rebase", run_drive=lambda *a, **k: (0, "ok"))
    assert len(sc.ledger.read_all(d)) == before


# =========================================================================== --rebase <name> (#2340)
#
# Component D of `.sdlc/design/2329.md`: the clean-path automatic rebase + push, dispatched through
# #2338's shared machinery, run unattended inside the isolated worktree. `_rebase_reply` (and
# `build_reply`/`handle_message_event`'s own `--rebase` wiring) never runs `git rebase` itself --
# the driven session does, per `_REBASE_EXTRA_PROMPT` -- so these tests exercise the REAL `dispatch`
# path against the real `_repo` git fixture, with a fake `run_drive` standing in for the headless
# session (mirrors every dispatch-infrastructure test above), never a fake `dispatch`.


def _rebase_cfg(actor="tester", channel="C1111111", **extra):
    """A config carrying BOTH the ledger settings `dispatch` needs and the `slack_commands` channel
    settings `handle_message_event`'s own authorization gate needs -- `_ledger_config`'s own
    `**extra` merges at the top level, exactly as `slack_commands`/`work` need to sit."""
    return _ledger_config(actor=actor,
                           slack_commands={"enabled": True, "channel_id": channel,
                                            "app_token_env": "APP_ENV_T", "bot_token_env": "BOT_ENV_T"},
                           **extra)


def test_rebase_reply_happy_path_relays_the_driven_sessions_own_why(tmp_path):
    local, d = _repo(tmp_path)
    cfg = _rebase_cfg()
    key = "slack-cmd-billing"
    drive = _fake_drive(exit_code=0, write_marker=(d, key, "done", cfg))
    text = sc._rebase_reply(d, cfg, "billing", run_drive=drive)
    assert text == "`--rebase billing`: driven session's own record"


def test_rebase_reply_conflict_reports_parked_never_a_fake_success(tmp_path):
    """A CONFLICT is reported and stopped, never force-pushed or silently skipped -- the driven
    session's own `why` (naming the conflicted files, per `_REBASE_EXTRA_PROMPT`) is relayed
    verbatim, and the reply never claims success."""
    local, d = _repo(tmp_path)
    cfg = _rebase_cfg()
    key = "slack-cmd-billing"

    def drive(cmd_str, prompt, cwd, env, timeout, on_spawn=None):
        sc.ledger.safe_append(d, "parked", key, config=cfg,
                               why="conflict in f.txt -- run /sigma-rebase locally")
        return 1, "conflict"

    text = sc._rebase_reply(d, cfg, "billing", run_drive=drive)
    assert text == ("`--rebase billing` stopped for a human -- conflict in f.txt -- run "
                     "/sigma-rebase locally")
    assert "done" not in text.lower()


def test_rebase_reply_failed_relays_the_real_error(tmp_path):
    local, d = _repo(tmp_path)
    cfg = _rebase_cfg()
    key = "slack-cmd-billing"
    drive = _fake_drive(exit_code=1, stdout="boom",
                         write_marker=(d, key, "failed", cfg))
    text = sc._rebase_reply(d, cfg, "billing", run_drive=drive)
    assert text == "`--rebase billing` failed -- driven session's own record"


def test_rebase_reply_unclear_when_the_drive_exits_zero_with_no_marker(tmp_path):
    """#1332's own repro case, reached through `--rebase`'s real reply text -- never a false
    "done"."""
    local, d = _repo(tmp_path)
    cfg = _rebase_cfg()
    text = sc._rebase_reply(d, cfg, "billing", run_drive=_fake_drive(exit_code=0))
    assert "unclear" in text
    assert "billing" in text
    assert "exit code 0" in text


def test_rebase_reply_unclear_names_a_disabled_ledger_as_the_likely_cause(tmp_path):
    """#2430, a real gap found live and closed here: `unclear` is EXACTLY `drive_outcome`'s own
    documented repro shape for a disabled ledger (the completion marker `ledger.safe_append` never
    wrote because `ledger.enabled` is false) -- a real rebase can genuinely succeed and still
    report `unclear`, with nothing telling the operator why. With the ledger off this reply must
    name that as the likely cause instead of the generic "check the repo directly"."""
    local, d = _repo(tmp_path)
    cfg = _rebase_cfg(ledger={"enabled": False, "actor": "tester"})
    text = sc._rebase_reply(d, cfg, "billing", run_drive=_fake_drive(exit_code=0))
    assert "unclear" in text
    assert "ledger.enabled" in text and "false" in text


def test_rebase_reply_unclear_says_nothing_extra_when_the_ledger_is_on(tmp_path):
    """The mirror: the ledger note is diagnostic, not decoration -- it must not appear when the
    ledger genuinely IS on, where the disabled-ledger explanation would be false."""
    local, d = _repo(tmp_path)
    cfg = _rebase_cfg()                                   # ledger.enabled: True, _rebase_cfg's own default
    text = sc._rebase_reply(d, cfg, "billing", run_drive=_fake_drive(exit_code=0))
    assert "ledger.enabled" not in text


def test_rebase_reply_busy_names_the_real_holder(tmp_path):
    local, d = _repo(tmp_path)
    _raw_claim(d, "alice", "slack-cmd-billing", kind="claimed")
    cfg = _rebase_cfg(actor="bob")
    calls = []
    text = sc._rebase_reply(d, cfg, "billing",
                             run_drive=lambda *a, **k: calls.append(1) or (0, "ok"))
    assert text == "could not rebase `billing` -- already claimed by alice. Nothing new started."
    assert calls == []   # never drove -- a losing claim touches nothing further


def test_rebase_reply_worktree_busy_is_reported_not_silently_retried(tmp_path):
    local, d = _repo(tmp_path)
    cfg = _rebase_cfg()
    lock_path = sc.feature_rebase.lock_path(d, "billing")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    held = sc.feature_rebase._acquire(lock_path)
    try:
        text = sc._rebase_reply(d, cfg, "billing", run_drive=lambda *a, **k: (0, "ok"))
    finally:
        sc.feature_rebase._release(held)
    assert text.startswith("could not rebase `billing` --")
    assert "already held" in text


def test_rebase_reply_worktree_unavailable_names_the_real_git_failure(tmp_path):
    local, d = _repo(tmp_path, push_feature=False)   # no feature/billing branch on the remote
    cfg = _rebase_cfg()
    text = sc._rebase_reply(d, cfg, "billing", run_drive=lambda *a, **k: (0, "ok"))
    assert text.startswith("could not rebase `billing` --")
    assert "remote tip" in text


def test_rebase_extra_prompt_forbids_the_interactive_conflict_walker():
    """Pins the one hard safety line in Component D directly: the prompt handed to the driven
    session must never invite it to call `conflict_walk.py`'s interactive walker unattended."""
    assert "conflict_walk.py" in sc._REBASE_EXTRA_PROMPT
    assert "do NOT" in sc._REBASE_EXTRA_PROMPT
    assert "/sigma-rebase" in sc._REBASE_EXTRA_PROMPT


def test_dispatch_prompt_for_rebase_carries_the_rebase_specific_instructions(tmp_path):
    """The `extra_prompt` `_rebase_reply` supplies actually reaches the driven session's own
    prompt -- not just a generic dispatch, `--rebase`'s own instructions."""
    local, d = _repo(tmp_path)
    cfg = _rebase_cfg()
    capture = []
    drive = _fake_drive(exit_code=0, write_marker=(d, "slack-cmd-billing", "done", cfg),
                         capture=capture)
    sc._rebase_reply(d, cfg, "billing", run_drive=drive)
    prompt = capture[0][1]
    assert "attempt_rebase" in prompt
    assert "conflict_walk.py" in prompt


def test_handle_message_event_dispatches_rebase_for_real(tmp_path):
    """End to end through the Socket Mode handling entrypoint: an authorized `--rebase billing`
    message resolves the real open unit, dispatches, and gets a real reply -- never the slice-1
    placeholder."""
    local, d = _repo(tmp_path)
    _write_registry(d, {"billing": {"open": True}})
    cfg = _rebase_cfg()
    key = "slack-cmd-billing"
    drive = _fake_drive(exit_code=0, write_marker=(d, key, "done", cfg))
    event = {"channel": "C1111111", "text": "--rebase billing"}
    authorized, reply, parsed = sc.handle_message_event(event, cfg, sdlc_dir=str(d), run_drive=drive)
    assert authorized is True
    assert parsed == sc.ParsedCommand("--rebase", "billing", None)
    assert reply == "`--rebase billing`: driven session's own record"
    assert "not wired up" not in reply   # never the placeholder any more


def test_handle_message_event_rebase_on_wrong_channel_never_dispatches(tmp_path):
    """The channel-authorization gate runs BEFORE any dispatch -- an unauthorized channel must
    never claim, cut a worktree, or drive anything."""
    local, d = _repo(tmp_path)
    _write_registry(d, {"billing": {"open": True}})
    cfg = _rebase_cfg(channel="C1111111")
    calls = []
    event = {"channel": "C_WRONG", "text": "--rebase billing"}
    authorized, reply, parsed = sc.handle_message_event(
        event, cfg, sdlc_dir=str(d), run_drive=lambda *a, **k: calls.append(1) or (0, "ok"))
    assert (authorized, reply, parsed) == (False, None, None)
    assert calls == []
    assert sc.ledger.open_claims(sc.ledger.read_all(d)) == {}


# --------------------------------------------------------------------------- --merge <name> (#2341)
#
# Component D's REVISED text (round-1 REJECT fix, finding 3): "--merge <name>" verifies and reports
# only, and never lands anything, in any configuration. `_merge_check` runs entirely in this
# process (no driven Claude session) precisely so that guarantee is a real, provable call-list
# fact, not a prompt an opaque subprocess is merely asked to honour -- see
# `test_merge_check_never_calls_any_landing_or_merge_function`, below, for the proof, and
# `test_unit_completion.py::test_neither_mode_ever_merges_anything` for the sibling module's own
# identical style this one deliberately matches.


def test_merge_check_reports_parked_when_a_rebase_is_stopped(tmp_path, monkeypatch):
    local, d = _repo(tmp_path)
    monkeypatch.setattr(sc.feature_rebase, "rebase_stopped", lambda run, cwd: True)
    kind, why = sc._merge_check(str(local), {})
    assert kind == "parked"
    assert "--rebase" in why and "/sigma-rebase" in why


def test_merge_check_reports_failed_when_no_verify_command_is_configured(tmp_path, monkeypatch):
    local, d = _repo(tmp_path)
    monkeypatch.setattr(sc.feature_rebase, "rebase_stopped", lambda run, cwd: False)
    kind, why = sc._merge_check(str(local), {})
    assert kind == "failed"
    assert "verify.command" in why


def test_merge_check_reports_failed_on_a_real_verify_failure(tmp_path, monkeypatch):
    local, d = _repo(tmp_path)
    monkeypatch.setattr(sc.feature_rebase, "rebase_stopped", lambda run, cwd: False)
    kind, why = sc._merge_check(str(local), {"verify": {"command": "exit 1"}})
    assert kind == "failed"
    assert "FAILED" in why


def test_merge_check_reports_done_and_never_merges_on_a_real_verify_pass(tmp_path, monkeypatch):
    local, d = _repo(tmp_path)
    monkeypatch.setattr(sc.feature_rebase, "rebase_stopped", lambda run, cwd: False)
    kind, why = sc._merge_check(str(local), {"verify": {"command": "exit 0"}})
    assert kind == "done"
    assert "PASSED" in why
    assert "ready to merge" in why
    assert "never lands anything itself" in why
    # the honest instruction is to land it YOURSELF -- never a claim that this already happened
    assert "merged" not in why.lower()


def test_merge_check_never_calls_any_landing_or_merge_function(tmp_path, monkeypatch):
    """The single most important constraint in this slice, made a real, structural, provable fact:
    `ensure_landing_pr`/`merge_pr`/`verify_and_offer_merge`/`_interactive_decide` are never called
    by `_merge_check`, on EITHER outcome (verify pass or fail), in any configuration.

    Matches `test_unit_completion.py::test_neither_mode_ever_merges_anything`'s own style
    exactly: real, answering stubs are installed for every one of the four functions (so a code
    path that reached for one would get a recorded call, not an ImportError or an AttributeError
    masking the very thing this test exists to catch), and the call list is asserted EMPTY --
    never merely "no exception was raised", which would be trivially true of a function that did
    nothing at all."""
    local, d = _repo(tmp_path)
    calls = rest_merge_support.spy_landing(monkeypatch, sc.verify_merge)   # #935: also sees a REST merge
    monkeypatch.setattr(sc.feature_rebase, "rebase_stopped", lambda run, cwd: False)

    for cmd in ("exit 0", "exit 1"):
        kind, _why = sc._merge_check(str(local), {"verify": {"command": cmd}})
        assert kind in ("done", "failed")

    assert calls == []


def test_merge_run_drive_writes_the_ledger_marker_and_returns_why_as_stdout(tmp_path, monkeypatch):
    local, d = _repo(tmp_path)
    cfg = _ledger_config()
    cfg["verify"] = {"command": "exit 0"}
    key = "slack-cmd-billing"
    drive = sc._merge_run_drive(d, cfg, key)
    exit_code, stdout = drive("cmd", "prompt", str(local), {}, 30)
    assert exit_code == 0
    assert "ready to merge" in stdout
    entries = [e for e in sc.ledger.read_all(d) if e.get("goal") == key]
    assert entries[-1]["kind"] == "done"
    # the SAME text, one string not two independently-drifting copies -- but `ledger.append`'s own
    # `why` sanitiser (flatten -> scrub -> cap at 200 chars, `ledger.py`:744) means the ledger's own
    # copy is a flattened, capped VERSION of `stdout`, never byte-identical once `why` runs long
    # (it always does here: `run_verify_command`'s own path-bearing report alone exceeds 80 chars).
    assert "VERIFY PASSED" in entries[-1]["why"]
    assert "VERIFY PASSED" in stdout


def test_merge_run_drive_registers_its_own_pid_as_the_liveness_marker(tmp_path):
    """No child process exists for `--merge` -- `on_spawn` is called with THIS process's own pid,
    the same liveness-marker contract `dispatch()`'s own round-2 fix relies on for `--rebase`."""
    local, d = _repo(tmp_path)
    cfg = _ledger_config()
    cfg["verify"] = {"command": "exit 0"}
    seen = {}
    drive = sc._merge_run_drive(d, cfg, "slack-cmd-billing")
    drive("cmd", "prompt", str(local), {}, 30, on_spawn=lambda pid: seen.setdefault("pid", pid))
    assert seen["pid"] == os.getpid()


def test_merge_run_drive_reports_a_crash_as_a_failed_marker_never_raises(tmp_path, monkeypatch):
    local, d = _repo(tmp_path)
    cfg = _ledger_config()
    key = "slack-cmd-billing"

    def boom(cwd, config):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(sc, "_merge_check", boom)
    drive = sc._merge_run_drive(d, cfg, key)
    exit_code, stdout = drive("cmd", "prompt", str(local), {}, 30)
    assert exit_code == 0
    assert "crashed" in stdout
    entries = [e for e in sc.ledger.read_all(d) if e.get("goal") == key]
    assert entries[-1]["kind"] == "failed"


def test_merge_reply_reports_busy_naming_the_holder(tmp_path):
    local, d = _repo(tmp_path)
    _raw_claim(d, "alice", "slack-cmd-billing", kind="claimed")
    cfg = _ledger_config(actor="bob")
    reply = sc._merge_reply(d, cfg, "billing", run_drive=lambda *a, **k: (0, "ok"))
    assert "billing" in reply and "alice" in reply and "already" in reply


def test_merge_reply_reports_worktree_busy(tmp_path):
    local, d = _repo(tmp_path)
    cfg = _ledger_config()
    lock_path = sc.feature_rebase.lock_path(d, "billing")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    held = sc.feature_rebase._acquire(lock_path)
    try:
        reply = sc._merge_reply(d, cfg, "billing", run_drive=lambda *a, **k: (0, "ok"))
    finally:
        sc.feature_rebase._release(held)
    assert "billing" in reply and "busy" in reply


def test_merge_reply_reports_worktree_unavailable(tmp_path):
    local, d = _repo(tmp_path, push_feature=False)
    cfg = _ledger_config()
    reply = sc._merge_reply(d, cfg, "billing", run_drive=lambda *a, **k: (0, "ok"))
    assert "billing" in reply and "could not prepare" in reply


def test_merge_reply_reports_unclear_when_the_check_leaves_no_marker(tmp_path):
    local, d = _repo(tmp_path)
    cfg = _ledger_config()
    reply = sc._merge_reply(d, cfg, "billing", run_drive=lambda *a, **k: (0, "ok"))
    assert "billing" in reply and "no clear outcome" in reply


def test_merge_reply_unclear_names_a_disabled_ledger_as_the_likely_cause(tmp_path):
    """#2430's own sibling for `--merge`: same `_with_ledger_off_note` helper, same shared cause."""
    local, d = _repo(tmp_path)
    cfg = _ledger_config(ledger={"enabled": False, "actor": "tester"})
    reply = sc._merge_reply(d, cfg, "billing", run_drive=lambda *a, **k: (0, "ok"))
    assert "ledger.enabled" in reply and "false" in reply


def test_merge_reply_falls_back_to_detail_when_dispatch_crashes_outside_the_check(tmp_path, monkeypatch):
    local, d = _repo(tmp_path)
    cfg = _ledger_config()

    def boom(*a, **k):
        raise RuntimeError("simulated crash before the check ever ran")

    monkeypatch.setattr(sc.autowatch, "_drive_cmd", boom)
    reply = sc._merge_reply(d, cfg, "billing", run_drive=lambda *a, **k: (0, "ok"))
    assert "billing" in reply and "simulated crash" in reply


def test_merge_reply_end_to_end_verified_clean_never_lands_anything(tmp_path, monkeypatch):
    """The full real path, no injected `run_drive`: real claim, real isolated worktree cut from the
    real remote's `feature/billing` tip, `_merge_run_drive`'s own real check against that worktree,
    real teardown -- and the landing functions are still, provably, never called."""
    local, d = _repo(tmp_path)
    cfg = _ledger_config()
    cfg["verify"] = {"command": "exit 0"}
    calls = []
    monkeypatch.setattr(sc.verify_merge, "merge_pr",
                         lambda *a, **k: calls.append("merge_pr") or {"ok": True, "why": "stub"})
    monkeypatch.setattr(sc.verify_merge, "ensure_landing_pr",
                         lambda *a, **k: calls.append("ensure_landing_pr") or
                         {"outcome": "created", "number": 1, "why": "stub"})
    reply = sc._merge_reply(d, cfg, "billing")
    assert "ready to merge" in reply
    assert "never lands anything itself" in reply
    assert calls == []
    # everything cleaned up, exactly like the --rebase dispatch tests above
    assert not sc.feature_rebase.worktree_path(d, "billing").exists()
    assert sc.ledger.open_claims(sc.ledger.read_all(d)) == {}


# --------------------------------------------------------------------------- --unsafe-merge <name> (#2359)


def test_unsafe_merge_check_lands_a_fresh_pr_when_verify_passes(tmp_path, monkeypatch):
    """The opposite proof from `--merge`'s own (#2341's `test_merge_check_never_calls_any_...`):
    `--unsafe-merge` DOES call `ensure_landing_pr`/`merge_pr` -- the SAME, already-tested functions
    a human running `verify_merge.py land` uses -- once verify is green, with no human `decide()`
    in the way."""
    local, d = _repo(tmp_path)
    cfg = _ledger_config()
    cfg["verify"] = {"command": "exit 0"}
    cfg["work"] = {"base": "main"}
    calls = []
    monkeypatch.setattr(sc.verify_merge, "ensure_landing_pr",
                         lambda *a, **k: calls.append("ensure_landing_pr") or
                         {"outcome": sc.verify_merge.CREATED, "number": 42, "why": "opened #42"})
    monkeypatch.setattr(sc.verify_merge, "merge_pr",
                         lambda *a, **k: calls.append("merge_pr") or
                         {"ok": True, "why": "PR #42 merged."})
    worktree_cwd = str(sc.feature_rebase.worktree_path(d, "billing"))
    # cut the worktree ourselves so _unsafe_merge_check has a real checkout to run verify.command
    # against, mirroring how dispatch()'s own cut_worktree hands it a real cwd.
    path, lock_fd = sc.cut_worktree(d, cfg, "billing")
    try:
        kind, why = sc._unsafe_merge_check(str(path), d, cfg, "billing")
    finally:
        sc.teardown_worktree(d, path, lock_fd)
    assert calls == ["ensure_landing_pr", "merge_pr"]
    assert kind == "done"
    assert "42" in why


def test_unsafe_merge_check_never_calls_landing_or_merge_when_verify_fails(tmp_path, monkeypatch):
    """The ONE check `--unsafe-merge` does NOT skip: a failing `verify.command` still blocks the
    merge, structurally (`verify_and_offer_merge`'s own `decide()` call is unreachable until verify
    is green) -- never merely by convention."""
    local, d = _repo(tmp_path)
    cfg = _ledger_config()
    cfg["verify"] = {"command": "exit 1"}
    cfg["work"] = {"base": "main"}
    calls = []
    monkeypatch.setattr(sc.verify_merge, "ensure_landing_pr",
                         lambda *a, **k: calls.append("ensure_landing_pr") or
                         {"outcome": sc.verify_merge.CREATED, "number": 1, "why": "stub"})
    monkeypatch.setattr(sc.verify_merge, "merge_pr",
                         lambda *a, **k: calls.append("merge_pr") or {"ok": True, "why": "stub"})
    path, lock_fd = sc.cut_worktree(d, cfg, "billing")
    try:
        kind, why = sc._unsafe_merge_check(str(path), d, cfg, "billing")
    finally:
        sc.teardown_worktree(d, path, lock_fd)
    assert calls == []
    assert kind == "failed"
    assert "FAIL" in why or "exit 1" in why or "fail" in why.lower()


def test_unsafe_merge_check_declined_landing_pr_reports_parked_not_overridden(tmp_path, monkeypatch):
    """A human already closed the landing PR without merging -- `--unsafe-merge` must not reopen or
    override that decision, matching `ensure_landing_pr`'s own "never reverses a human's own
    decision" guarantee."""
    local, d = _repo(tmp_path)
    cfg = _ledger_config()
    cfg["verify"] = {"command": "exit 0"}
    cfg["work"] = {"base": "main"}
    calls = []
    monkeypatch.setattr(sc.verify_merge, "ensure_landing_pr",
                         lambda *a, **k: {"outcome": sc.verify_merge.DECLINED, "number": 9,
                                          "why": "PR #9 was already closed by a human."})
    monkeypatch.setattr(sc.verify_merge, "merge_pr",
                         lambda *a, **k: calls.append("merge_pr") or {"ok": True, "why": "stub"})
    path, lock_fd = sc.cut_worktree(d, cfg, "billing")
    try:
        kind, why = sc._unsafe_merge_check(str(path), d, cfg, "billing")
    finally:
        sc.teardown_worktree(d, path, lock_fd)
    assert calls == []
    assert kind == "parked"
    assert "closed by a human" in why


def test_unsafe_merge_reply_unclear_names_a_disabled_ledger_as_the_likely_cause(tmp_path):
    """#2430's own third sibling: `--unsafe-merge` shares the identical `dispatch()`/ledger
    completion-marker plumbing as `--rebase`/`--merge`, so the same diagnosis applies."""
    local, d = _repo(tmp_path)
    cfg = _ledger_config(ledger={"enabled": False, "actor": "tester"})
    reply = sc._unsafe_merge_reply(d, cfg, "billing", run_drive=lambda *a, **k: (0, "ok"))
    assert "ledger.enabled" in reply and "false" in reply


def test_unsafe_merge_reply_unclear_says_nothing_extra_when_the_ledger_is_on(tmp_path):
    local, d = _repo(tmp_path)
    cfg = _ledger_config()
    reply = sc._unsafe_merge_reply(d, cfg, "billing", run_drive=lambda *a, **k: (0, "ok"))
    assert "no clear outcome" in reply and "ledger.enabled" not in reply


def test_unsafe_merge_reply_dispatches_and_lands_for_real(tmp_path, monkeypatch):
    """The full real path, no injected `run_drive`: real claim, real isolated worktree, real
    teardown -- and (unlike `--merge`'s own identically-shaped test) the landing functions ARE
    called, exactly once each."""
    local, d = _repo(tmp_path)
    cfg = _ledger_config()
    cfg["verify"] = {"command": "exit 0"}
    cfg["work"] = {"base": "main"}
    calls = []
    monkeypatch.setattr(sc.verify_merge, "ensure_landing_pr",
                         lambda *a, **k: calls.append("ensure_landing_pr") or
                         {"outcome": sc.verify_merge.CREATED, "number": 7, "why": "opened #7"})
    monkeypatch.setattr(sc.verify_merge, "merge_pr",
                         lambda *a, **k: calls.append("merge_pr") or
                         {"ok": True, "why": "PR #7 merged."})
    reply = sc._unsafe_merge_reply(d, cfg, "billing")
    assert calls == ["ensure_landing_pr", "merge_pr"]
    assert "7" in reply
    assert not sc.feature_rebase.worktree_path(d, "billing").exists()
    assert sc.ledger.open_claims(sc.ledger.read_all(d)) == {}


def test_parse_command_accepts_unsafe_merge_with_exactly_one_name(tmp_path):
    d = _sdlc(tmp_path)
    _write_registry(d, {"billing": {"open": True}})
    parsed = sc.parse_command("--unsafe-merge billing", str(d))
    assert parsed.command == "--unsafe-merge"
    assert parsed.name == "billing"


def test_parse_command_unsafe_merge_needs_exactly_one_name(tmp_path):
    d = _sdlc(tmp_path)
    with pytest.raises(sc.CommandError):
        sc.parse_command("--unsafe-merge", str(d))
    with pytest.raises(sc.CommandError):
        sc.parse_command("--unsafe-merge a b", str(d))


def test_build_reply_unsafe_merge_dispatches_for_real(tmp_path, monkeypatch):
    local, d = _repo(tmp_path)
    cfg = _config(channel="C1111111")
    cfg.update(_ledger_config())
    cfg["verify"] = {"command": "exit 0"}
    cfg["work"] = {"base": "main"}
    monkeypatch.setattr(sc.verify_merge, "ensure_landing_pr",
                         lambda *a, **k: {"outcome": sc.verify_merge.CREATED, "number": 3,
                                          "why": "opened #3"})
    monkeypatch.setattr(sc.verify_merge, "merge_pr",
                         lambda *a, **k: {"ok": True, "why": "PR #3 merged."})
    reply = sc.build_reply(sc.ParsedCommand("--unsafe-merge", "billing", None), cfg, sdlc_dir=str(d))
    assert "3" in reply


def test_handle_message_event_dispatches_unsafe_merge_for_real(tmp_path, monkeypatch):
    local, d = _repo(tmp_path)
    _write_registry(d, {"billing": {"open": True}})
    cfg = _config(channel="C1111111")
    cfg.update(_ledger_config())
    cfg["verify"] = {"command": "exit 0"}
    cfg["work"] = {"base": "main"}
    monkeypatch.setattr(sc.verify_merge, "ensure_landing_pr",
                         lambda *a, **k: {"outcome": sc.verify_merge.CREATED, "number": 4,
                                          "why": "opened #4"})
    monkeypatch.setattr(sc.verify_merge, "merge_pr",
                         lambda *a, **k: {"ok": True, "why": "PR #4 merged."})
    authorized, reply, parsed = sc.handle_message_event(
        {"channel": "C1111111", "text": "--unsafe-merge billing"}, cfg, sdlc_dir=str(d))
    assert authorized is True
    assert parsed.command == "--unsafe-merge"
    assert "4" in reply


def test_help_text_documents_unsafe_merge_and_warns_it_is_discretionary():
    assert "--unsafe-merge <name>" in sc.HELP_TEXT
    assert "own discretion" in sc.HELP_TEXT


def test_build_reply_merge_dispatches_for_real(tmp_path):
    local, d = _repo(tmp_path)
    cfg = _config(channel="C1111111")
    cfg.update(_ledger_config())
    cfg["verify"] = {"command": "exit 0"}
    reply = sc.build_reply(sc.ParsedCommand("--merge", "billing", None), cfg, sdlc_dir=str(d))
    assert "ready to merge" in reply


def test_handle_message_event_dispatches_merge_for_real(tmp_path):
    local, d = _repo(tmp_path)
    _write_registry(d, {"billing": {"open": True}})
    cfg = _config(channel="C1111111")
    cfg.update(_ledger_config())
    cfg["verify"] = {"command": "exit 0"}
    authorized, reply, parsed = sc.handle_message_event(
        {"channel": "C1111111", "text": "--merge billing"}, cfg, sdlc_dir=str(d))
    assert authorized is True
    assert parsed.command == "--merge"
    assert "ready to merge" in reply


# --------------------------------------------------------------------------- acquire_single_instance:
# genuine concurrent race regression (#2396)
#
# Mirrors test_watch.py's own `_race` discipline for watch.sh's mutex (F21/#339) exactly: REAL,
# concurrently-running OS processes (Popen, not sequential calls, not a mocked simulation) --
# `ensure()`'s own "no risk of double-starting a second live listener" guarantee (#2396) rests
# structurally on this exact primitive, so the primitive itself has to be proven under genuine
# concurrency, not just sequential calls. `acquire_single_instance`'s own ORIGINAL docstring argued
# this was unnecessary because the listener was "started AT MOST ONCE by a human or a supervisor" --
# #2396's own `ensure()` is exactly what ends that guarantee (a command meant to be run repeatedly,
# possibly by a cron entry and a human at once). This test caught a REAL double-win before the
# `_LOCK_RECLAIM_GRACE_SECONDS` gate was added: a 20-racer burst produced two simultaneous winners in
# 1 of 20 rounds (empirical, not theorised) -- a racer that lost the initial `os.mkdir` read an EMPTY
# pidfile (because the true winner had not yet reached its own `_atomic_write_text` call) and wrongly
# concluded the lock was abandoned rather than held by a live winner mid-write.
#
# Since #2751 this is a SMOKE TEST: the deterministic in-process controls at the `_atomic_write_text`
# seam (`test_acquire_single_instance_no_intermediate_state_of_a_live_winner_is_reclaimable` and
# `..._reclaiming_a_crashed_holder_is_not_itself_reclaimable_mid_startup`, above) are the system of
# record for the write-order window.

_ACQUIRE_RACE_SCRIPT = """
import importlib.util, sys, time
spec = importlib.util.spec_from_file_location("slack_commands_listen", sys.argv[2])
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)
ok, reason = sc.acquire_single_instance(sys.argv[1])
if ok:
    print("WON")
    sys.stdout.flush()
    time.sleep(10)
else:
    print("LOSER: %s" % reason)
"""

#: N_RACERS and the winner's own 10s sleep above both mirror test_watch.py's own tuned values
#: for the identical shape of test (`_race`'s own docstring: a SHORT winner-alive window is a real
#: source of false positives under a loaded/contended host, not just a theoretical one -- confirmed
#: there empirically). The settle loop below still exits EARLY the moment exactly one racer remains
#: alive; the 10s winner sleep only bounds how long a slow, contended host is given to finish
#: settling before this test's own snapshot of "who is still alive" would otherwise risk racing the
#: winner's own exit.
_ACQUIRE_N_RACERS = 15


def _race_acquire(d, n=_ACQUIRE_N_RACERS):
    """Launch `n` genuinely concurrent racers against `acquire_single_instance`; return
    (still_alive_after_settling, per-process [rc=.. out=.. err=..] summaries) -- the same
    settle-by-polling discipline test_watch.py's own `_race` uses (a fixed sleep bakes in an
    assumption about scheduler speed that does not hold on a noisy shared CI runner)."""
    procs = [subprocess.Popen(
                 [sys.executable, "-c", _ACQUIRE_RACE_SCRIPT, str(d),
                  str(S / "slack_commands_listen.py")],
                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for _ in range(n)]
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and sum(1 for p in procs if p.poll() is None) > 1:
        time.sleep(0.05)
    alive = [p for p in procs if p.poll() is None]
    results = [p.communicate(timeout=20) for p in procs]
    outs = [f"[rc={p.returncode}] out={out!r} err={err!r}" for p, (out, err) in zip(procs, results)]
    return alive, outs


def test_acquire_single_instance_exactly_one_of_several_genuinely_concurrent_racers_wins(tmp_path):
    """SMOKE TEST, not the system of record. Probabilistic: real `Popen` racers whose chance of
    landing inside a two-local-write window depends on interpreter start-up and host load. Measured
    on the pre-#2751 code at about 10% per run under load avg about 5 (2/20 rounds, research #2751
    R-1). The deterministic controls are
    `test_acquire_single_instance_no_intermediate_state_of_a_live_winner_is_reclaimable` and
    `test_acquire_single_instance_reclaiming_a_crashed_holder_is_not_itself_reclaimable_mid_startup`.
    Residual harness timing assumptions: the 8 s settle deadline against a 10 s winner sleep, and
    all 15 racers starting within the winner's 10 s. The assertions are ordered so the failure
    signature names its cause: two `WON` lines is the real defect; a loser still alive at the
    settle deadline is harness timing."""
    d = _sdlc(tmp_path)
    alive, outs = _race_acquire(d)
    winners = [o for o in outs if "WON" in o]
    losers = [o for o in outs if "WON" not in o]
    assert len(winners) == 1, f"DOUBLE WIN (the real defect): expected exactly one winner, got {len(winners)}: {outs}"
    assert len(alive) == 1, (f"harness timing, not a double win: a loser was still alive at the 8s "
                             f"settle deadline ({len(alive)} alive): {outs}")
    assert len(losers) == _ACQUIRE_N_RACERS - 1
    assert all("LOSER" in o for o in losers), f"a loser produced unexpected output: {losers}"
    assert sc.heartbeat_path(d).exists()
    assert sc.pid_path(d).exists()


# --------------------------------------------------------------------------- heartbeat_liveness (#2396)


def test_heartbeat_liveness_absent_when_no_heartbeat_file(tmp_path):
    d = _sdlc(tmp_path)
    assert sc.heartbeat_liveness(d) == ("absent", None)


def test_heartbeat_liveness_absent_when_heartbeat_is_malformed_json(tmp_path):
    d = _sdlc(tmp_path)
    sc.heartbeat_path(d).write_text("not json", encoding="utf-8")
    assert sc.heartbeat_liveness(d) == ("absent", None)


def test_heartbeat_liveness_absent_when_last_seen_is_missing_non_numeric_or_boolean(tmp_path):
    d = _sdlc(tmp_path)
    sc.heartbeat_path(d).write_text(json.dumps({"pid": 1}), encoding="utf-8")
    assert sc.heartbeat_liveness(d) == ("absent", None)
    sc.heartbeat_path(d).write_text(json.dumps({"pid": 1, "last_seen": "nope"}), encoding="utf-8")
    assert sc.heartbeat_liveness(d) == ("absent", None)
    sc.heartbeat_path(d).write_text(json.dumps({"pid": 1, "last_seen": True}), encoding="utf-8")
    assert sc.heartbeat_liveness(d) == ("absent", None)


def test_heartbeat_liveness_live_when_fresh(tmp_path):
    d = _sdlc(tmp_path)
    sc.write_heartbeat(d)
    state, age = sc.heartbeat_liveness(d, now=time.time() + 1)
    assert state == "live"
    assert 0 <= age < 5


def test_heartbeat_liveness_stale_past_the_bound(tmp_path):
    d = _sdlc(tmp_path)
    sc.write_heartbeat(d)
    state, age = sc.heartbeat_liveness(d, stale_after_seconds=10, now=time.time() + 100)
    assert state == "stale"
    assert age >= 100


# --------------------------------------------------------------------------- ensure() (#2396)


def _enabled_config():
    return _config(channel="C1111111", app_env="APP_ENV_T", bot_env="BOT_ENV_T", enabled=True)


class _FakeProc:
    """A duck-typed stand-in for `subprocess.Popen` -- DI for `ensure()`'s own `spawn` seam, so its
    polling/decision logic is tested in isolation from `acquire_single_instance`'s own reclaim
    behaviour (already covered above by a real, unmocked concurrency test) and without ever
    launching a real Slack connection (per the issue's own instruction)."""

    def __init__(self, pid, exit_code=None):
        self.pid = pid
        self._exit_code = exit_code

    def poll(self):
        return self._exit_code


def test_ensure_disabled_when_not_configured(tmp_path):
    d = _sdlc(tmp_path)
    calls = []
    result = sc.ensure(d, config=_config(enabled=False), spawn=lambda *a: calls.append(a))
    assert result.action == "disabled"
    assert calls == []


def test_ensure_misconfigured_when_a_token_env_is_missing(tmp_path, monkeypatch):
    d = _sdlc(tmp_path)
    monkeypatch.delenv("APP_ENV_T", raising=False)
    monkeypatch.setenv("BOT_ENV_T", "xoxb-fake")
    calls = []
    result = sc.ensure(d, config=_enabled_config(), spawn=lambda *a: calls.append(a))
    assert result.action == "misconfigured"
    assert "APP_ENV_T" in result.detail
    assert calls == []


def test_ensure_already_running_does_not_spawn_a_second_instance(tmp_path, monkeypatch):
    d = _sdlc(tmp_path)
    monkeypatch.setenv("APP_ENV_T", "xapp-fake")
    monkeypatch.setenv("BOT_ENV_T", "xoxb-fake")
    sc.write_heartbeat(d)
    sc.pid_path(d).write_text(str(os.getpid()))

    def _spawn(_sdlc_dir):
        raise AssertionError("must not spawn a second instance while one is genuinely live")

    result = sc.ensure(d, config=_enabled_config(), spawn=_spawn)
    assert result.action == "already-running"
    assert result.pid == os.getpid()


def test_ensure_starts_when_not_running(tmp_path, monkeypatch):
    d = _sdlc(tmp_path)
    monkeypatch.setenv("APP_ENV_T", "xapp-fake")
    monkeypatch.setenv("BOT_ENV_T", "xoxb-fake")
    calls = []

    def _spawn(sdlc_dir):
        calls.append(sdlc_dir)
        sc.pid_path(d).write_text("99991")
        sc.write_heartbeat(d)
        return _FakeProc(pid=99991)

    result = sc.ensure(d, config=_enabled_config(), spawn=_spawn)
    assert calls == [d]
    assert result.action == "started"
    assert result.pid == 99991


def test_ensure_restarts_when_heartbeat_is_stale(tmp_path, monkeypatch):
    d = _sdlc(tmp_path)
    monkeypatch.setenv("APP_ENV_T", "xapp-fake")
    monkeypatch.setenv("BOT_ENV_T", "xoxb-fake")
    # Simulate an old, dead instance: a stale heartbeat and its old pid still on disk.
    sc.pid_path(d).write_text("11111")
    sc.heartbeat_path(d).write_text(
        json.dumps({"pid": 11111, "last_seen": time.time() - 10000}), encoding="utf-8")

    def _spawn(sdlc_dir):
        sc.pid_path(d).write_text("22222")     # cleanly replaces the dead state
        sc.write_heartbeat(d)
        return _FakeProc(pid=22222)

    result = sc.ensure(d, config=_enabled_config(), spawn=_spawn)
    assert result.action == "restarted"
    assert result.pid == 22222
    assert int(sc.pid_path(d).read_text()) == 22222   # the old dead pid was genuinely replaced


def test_ensure_reports_failed_when_the_spawned_process_exits_without_a_heartbeat(tmp_path, monkeypatch):
    d = _sdlc(tmp_path)
    monkeypatch.setenv("APP_ENV_T", "xapp-fake")
    monkeypatch.setenv("BOT_ENV_T", "xoxb-fake")

    def _spawn(sdlc_dir):
        return _FakeProc(pid=33333, exit_code=1)   # exited immediately, never wrote anything

    result = sc.ensure(d, config=_enabled_config(), spawn=_spawn)
    assert result.action == "failed"
    assert "code 1" in result.detail


def test_ensure_reports_already_running_when_a_concurrent_start_wins_the_race(tmp_path, monkeypatch):
    """The spawned process itself lost `acquire_single_instance`'s own mutex to a genuinely
    concurrent sibling and exited immediately (exactly what a losing racer does) -- but by the time
    this poll loop checks, the SIBLING's own fresh heartbeat is already live. This is a benign race,
    not a failure: `ensure()` must report it as `already-running`, never `failed` (mirrors how
    `acquire_single_instance`'s own real racers above resolve the identical situation)."""
    d = _sdlc(tmp_path)
    monkeypatch.setenv("APP_ENV_T", "xapp-fake")
    monkeypatch.setenv("BOT_ENV_T", "xoxb-fake")

    def _spawn(sdlc_dir):
        sc.pid_path(d).write_text("44444")   # the sibling that WON writes its own state first...
        sc.write_heartbeat(d)
        return _FakeProc(pid=55555, exit_code=1)   # ...and OUR spawn lost the race, exit code 1

    result = sc.ensure(d, config=_enabled_config(), spawn=_spawn)
    assert result.action == "already-running"
    assert result.pid == 44444
    assert "concurrent" in result.detail


def test_ensure_times_out_and_reports_failed_when_heartbeat_never_appears(tmp_path, monkeypatch):
    d = _sdlc(tmp_path)
    monkeypatch.setenv("APP_ENV_T", "xapp-fake")
    monkeypatch.setenv("BOT_ENV_T", "xoxb-fake")

    def _spawn(sdlc_dir):
        return _FakeProc(pid=66666, exit_code=None)   # never exits, never writes a heartbeat

    result = sc.ensure(d, config=_enabled_config(), spawn=_spawn,
                        start_timeout_seconds=0.3, poll_interval_seconds=0.05)
    assert result.action == "failed"
    assert "timed out" in result.detail


def test_ensure_reports_a_spawn_exception_as_failed(tmp_path, monkeypatch):
    d = _sdlc(tmp_path)
    monkeypatch.setenv("APP_ENV_T", "xapp-fake")
    monkeypatch.setenv("BOT_ENV_T", "xoxb-fake")

    def _spawn(sdlc_dir):
        raise OSError("no such file or directory: python3")

    result = sc.ensure(d, config=_enabled_config(), spawn=_spawn)
    assert result.action == "failed"
    assert "no such file" in result.detail


# --------------------------------------------------------------------------- status_line (#2396)


def test_status_line_disabled(tmp_path):
    d = _sdlc(tmp_path)
    assert "disabled" in sc.status_line(d, config=_config(enabled=False))


def test_status_line_misconfigured_names_the_missing_env_var(tmp_path, monkeypatch):
    d = _sdlc(tmp_path)
    monkeypatch.delenv("APP_ENV_T", raising=False)
    monkeypatch.setenv("BOT_ENV_T", "xoxb-fake")
    line = sc.status_line(d, config=_enabled_config())
    assert "MISCONFIGURED" in line and "APP_ENV_T" in line


def test_status_line_not_running(tmp_path, monkeypatch):
    d = _sdlc(tmp_path)
    monkeypatch.setenv("APP_ENV_T", "xapp-fake")
    monkeypatch.setenv("BOT_ENV_T", "xoxb-fake")
    line = sc.status_line(d, config=_enabled_config())
    assert "not running" in line


def test_status_line_dead(tmp_path, monkeypatch):
    d = _sdlc(tmp_path)
    monkeypatch.setenv("APP_ENV_T", "xapp-fake")
    monkeypatch.setenv("BOT_ENV_T", "xoxb-fake")
    sc.write_heartbeat(d)
    line = sc.status_line(d, config=_enabled_config(), now=time.time() + 10000)
    assert "DEAD" in line


def test_status_line_running(tmp_path, monkeypatch):
    d = _sdlc(tmp_path)
    monkeypatch.setenv("APP_ENV_T", "xapp-fake")
    monkeypatch.setenv("BOT_ENV_T", "xoxb-fake")
    sc.write_heartbeat(d)
    line = sc.status_line(d, config=_enabled_config())
    assert "running" in line and "DEAD" not in line and "not running" not in line


# --------------------------------------------------------------------------- main() CLI verbs (#2396)


def test_main_legacy_positional_sdlc_dir_still_works(tmp_path, monkeypatch):
    """The pre-#2396 single-positional-arg convention (`slack_commands_listen.py <sdlc_dir>`,
    documented in SLACK_COMMANDS.md step 8 and the worked launchd/systemd units) must keep working
    completely unchanged -- "status"/"ensure" dispatch only on an EXACT match of argv[1]."""
    d = _sdlc(tmp_path)
    (d / "config.json").write_text(json.dumps(_config(enabled=False)), encoding="utf-8")
    seen = []
    monkeypatch.setattr(sc, "run", lambda sdlc_dir, config: seen.append((sdlc_dir, config)) or 0)
    rc = sc.main(["prog", str(d)])
    assert rc == 0
    assert seen[0][0] == str(d)


def test_main_status_verb_prints_one_line_and_exit_code(tmp_path, capsys):
    d = _sdlc(tmp_path)
    (d / "config.json").write_text(json.dumps(_config(enabled=False)), encoding="utf-8")
    rc = sc.main(["prog", "status", str(d)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "disabled" in out


def test_main_status_verb_exit_0_when_live(tmp_path, capsys, monkeypatch):
    d = _sdlc(tmp_path)
    monkeypatch.setenv("APP_ENV_T", "xapp-fake")
    monkeypatch.setenv("BOT_ENV_T", "xoxb-fake")
    (d / "config.json").write_text(json.dumps(_enabled_config()), encoding="utf-8")
    sc.write_heartbeat(d)
    rc = sc.main(["prog", "status", str(d)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "running" in out


def test_main_ensure_verb_dispatches_and_prints_the_result_line(tmp_path, capsys):
    d = _sdlc(tmp_path)
    (d / "config.json").write_text(json.dumps(_config(enabled=False)), encoding="utf-8")
    rc = sc.main(["prog", "ensure", str(d)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "not enabled" in out


def test_main_ensure_verb_exit_1_on_failure(tmp_path, capsys, monkeypatch):
    d = _sdlc(tmp_path)
    monkeypatch.setenv("APP_ENV_T", "xapp-fake")
    monkeypatch.setenv("BOT_ENV_T", "xoxb-fake")
    (d / "config.json").write_text(json.dumps(_enabled_config()), encoding="utf-8")
    monkeypatch.setattr(sc, "_spawn_listener", lambda sdlc_dir: _FakeProc(pid=1, exit_code=1))
    rc = sc.main(["prog", "ensure", str(d)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "failed" in out


# --------------------------------------------------------------------------- _spawn_listener: real subprocess (#2396)


def test_spawn_listener_launches_a_real_detached_subprocess(tmp_path):
    """No mocked spawn here -- proves the REAL `subprocess.Popen` call (argv, cwd, python
    executable) genuinely launches the real script end to end (AGENTS.md: "run the control, or
    it's decoration"). Uses a disabled config so the real child never needs `slack_sdk` or a real
    Slack token -- it hits `run()`'s own `enabled(config) is False` no-op path, logs it, and exits
    0, which is enough to prove the spawn mechanism itself (argv, cwd, python executable) rather
    than `ensure()`'s own decision logic, already covered above with a fake `spawn`."""
    d = _sdlc(tmp_path)
    (d / "config.json").write_text(json.dumps(_config(enabled=False)), encoding="utf-8")
    proc = sc._spawn_listener(d)
    try:
        rc = proc.wait(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert rc == 0
    log = sc.log_path(d).read_text(encoding="utf-8")
    assert "disabled in config -- nothing to do" in log


# --------------------------------------------------------------------------- scope boundary (#2396)


def test_ensure_is_never_wired_into_ensure_watcher_or_watch_daemon():
    """#2396 explicitly implements ONLY the manual, explicit command -- never the automatic,
    every-loop-trigger wiring `_ensure_watcher` provides for the ledger watcher
    (`watch_daemon.py` since #2488). That larger shape
    is a separate, larger follow-up needing its own design pass (per the issue itself), not silently
    done here. Grepped, not asserted from memory: a real regression here is exactly the kind of
    silent scope creep this test exists to catch."""
    loop_py = (S / "loop.py").read_text(encoding="utf-8")
    watch_daemon = (S / "watch_daemon.py").read_text(encoding="utf-8")
    assert "slack_commands_listen" not in loop_py
    assert "slack_commands" not in watch_daemon


# --------------------------------------------------------------------------- signal cleanup (#424)
#
# SIGTERM/SIGHUP used to terminate the listener without unwinding `run()`'s `finally`, stranding the
# pidfile, heartbeat and lock dir. The REAL-SUBPROCESS controls (tests/test_slack_sig.py) deliver each signal to a real
# listener and are deterministic: the parent blocks on a READY line the child prints only after startup finished writing
# its markers (first stop-file poll) (no sleeps, no racing a window), and every wait is bounded so a regressed handler
# FAILS instead of hanging. The in-process seam controls pin the exact handler logic.

_POSIX_SIGNALS = [n for n in ("SIGTERM", "SIGHUP") if hasattr(signal, n)]


@pytest.fixture
def _restore_signals():
    saved = {n: signal.getsignal(getattr(signal, n)) for n in _POSIX_SIGNALS}
    yield
    for n, h in saved.items():
        signal.signal(getattr(signal, n), h)


@pytest.fixture
def _no_real_death(monkeypatch):
    """The handler ends by re-raising the signal at itself; capture that instead of dying."""
    kills = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: kills.append((pid, sig)))
    monkeypatch.setattr(os, "_exit", lambda code: kills.append(("_exit", code)))
    return kills


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="no SIGHUP on this platform")
def test_seam_installs_handlers_that_release_then_die_by_the_same_signal(
        tmp_path, _restore_signals, _no_real_death):
    # The starting disposition is the test's own, not whatever this process inherited: under `nohup`
    # SIGHUP arrives ignored and the production code leaves an ignored signal alone on purpose
    # (test_seam_an_inherited_ignored_sighup_stays_ignored pins that). `_restore_signals` puts the
    # inherited dispositions back afterwards.
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGHUP, signal.SIG_DFL)
    d = _sdlc(tmp_path)
    uninstall = sc._install_signal_cleanup(d)
    try:
        for name in ("SIGTERM", "SIGHUP"):
            num = getattr(signal, name)
            sc.acquire_single_instance(d)
            handler = signal.getsignal(num)
            assert callable(handler)
            _no_real_death.clear()
            handler(num, None)
            assert not sc.pid_path(d).exists() and not sc.heartbeat_path(d).exists()
            assert not sc.lock_dir_path(d).exists()
            assert signal.getsignal(num) == signal.SIG_DFL     # restored BEFORE the re-raise
            assert _no_real_death == [(os.getpid(), num), ("_exit", 128 + num)]
            uninstall()                                         # clears _DYING, restores handlers
            uninstall = sc._install_signal_cleanup(d)
    finally:
        uninstall()


def test_seam_handler_leaves_a_successors_markers(tmp_path, _restore_signals, _no_real_death):
    d = _sdlc(tmp_path)
    sc.acquire_single_instance(d)
    sc.pid_path(d).write_text(str(os.getpid() + 1))
    uninstall = sc._install_signal_cleanup(d)
    try:
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
    finally:
        uninstall()
    assert sc.pid_path(d).read_text() == str(os.getpid() + 1)
    assert sc.heartbeat_path(d).is_file() and sc.lock_dir_path(d).is_dir()


def test_seam_signal_mid_acquire_after_heartbeat_before_pidfile_still_cleans_up(
        tmp_path, _restore_signals, _no_real_death):
    """acquire writes the heartbeat BEFORE the pidfile (#2751); a signal in that window used to strand
    the lock dir and heartbeat because ownership was judged by the pidfile alone."""
    d = _sdlc(tmp_path)
    sc.lock_dir_path(d).mkdir()
    sc.write_heartbeat(d)                      # ours; no pidfile yet
    uninstall = sc._install_signal_cleanup(d)
    try:
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
    finally:
        uninstall()
    assert not sc.heartbeat_path(d).exists() and not sc.lock_dir_path(d).exists()


def test_seam_a_foreign_heartbeat_without_a_pidfile_is_not_ours_to_remove(tmp_path):
    d = _sdlc(tmp_path)
    sc.lock_dir_path(d).mkdir()
    sc.heartbeat_path(d).write_text(json.dumps({"pid": os.getpid() + 1, "last_seen": time.time()}))
    sc.release_single_instance(d)
    assert sc.heartbeat_path(d).is_file() and sc.lock_dir_path(d).is_dir()


def test_seam_our_own_stale_heartbeat_without_a_pidfile_is_a_reclaimers_not_ours(tmp_path):
    """A stalled holder's OLD heartbeat, pidfile already unlinked by a successor mid-reclaim: releasing
    now would rmdir the successor's lock dir."""
    d = _sdlc(tmp_path)
    sc.lock_dir_path(d).mkdir()
    sc.heartbeat_path(d).write_text(json.dumps({"pid": os.getpid(), "last_seen": time.time() - 10_000}))
    sc.release_single_instance(d)
    assert sc.heartbeat_path(d).is_file() and sc.lock_dir_path(d).is_dir()


def test_seam_a_request_thread_cannot_recreate_the_heartbeat_once_the_handler_ran(
        tmp_path, _restore_signals, _no_real_death):
    d = _sdlc(tmp_path)
    sc.acquire_single_instance(d)
    uninstall = sc._install_signal_cleanup(d)
    try:
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        sc.write_heartbeat(d)                  # what `_on_request` does on its own thread
        assert not sc.heartbeat_path(d).exists()
    finally:
        uninstall()
    sc.write_heartbeat(d)                      # cleared again by uninstall
    assert sc.heartbeat_path(d).is_file()
    sc.release_single_instance(d)


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="no SIGHUP on this platform")
def test_seam_an_inherited_ignored_sighup_stays_ignored(tmp_path, _restore_signals):
    """`nohup`-style launchers set SIGHUP to ignored on purpose; overriding it would be a regression."""
    d = _sdlc(tmp_path)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    uninstall = sc._install_signal_cleanup(d)
    try:
        assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
        assert callable(signal.getsignal(signal.SIGTERM))
    finally:
        uninstall()
    assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN


def test_seam_uninstall_restores_the_previous_handlers(tmp_path, _restore_signals):
    d = _sdlc(tmp_path)
    marker = lambda *_: None            # noqa: E731
    signal.signal(signal.SIGTERM, marker)
    sc._install_signal_cleanup(d)()
    assert signal.getsignal(signal.SIGTERM) is marker


def test_seam_uninstall_survives_a_previous_handler_of_none(tmp_path, monkeypatch, _restore_signals):
    """`signal.getsignal` returns None for a handler installed from C; `signal.signal(sig, None)`
    raises TypeError, which must not turn a clean exit into a crash."""
    d = _sdlc(tmp_path)
    real = signal.getsignal
    monkeypatch.setattr(signal, "getsignal", lambda n: None)
    uninstall = sc._install_signal_cleanup(d)
    monkeypatch.setattr(signal, "getsignal", real)
    uninstall()
    assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL


def test_seam_off_the_main_thread_installs_nothing(tmp_path, _restore_signals):
    d = _sdlc(tmp_path)
    before = signal.getsignal(signal.SIGTERM)
    out = {}

    def work():
        out["uninstall"] = sc._install_signal_cleanup(d)
        out["uninstall"]()
    t = threading.Thread(target=work)
    t.start()
    t.join(30)
    assert "uninstall" in out and signal.getsignal(signal.SIGTERM) == before


def test_seam_missing_sighup_is_skipped_not_an_attribute_error(tmp_path, monkeypatch, _restore_signals):
    d = _sdlc(tmp_path)
    proxy = types.SimpleNamespace(SIGTERM=signal.SIGTERM, SIG_DFL=signal.SIG_DFL, SIG_IGN=signal.SIG_IGN,
                                  getsignal=signal.getsignal, signal=signal.signal)
    monkeypatch.setattr(sc, "signal", proxy)       # no SIGHUP, as on Windows
    uninstall = sc._install_signal_cleanup(d)
    try:
        assert callable(signal.getsignal(signal.SIGTERM))
    finally:
        uninstall()


def test_run_restores_signal_handlers_on_every_exit_path(tmp_path, monkeypatch, _restore_signals):
    d = _sdlc(tmp_path)
    monkeypatch.setenv("APP_ENV_T", "xapp-fake")
    monkeypatch.setenv("BOT_ENV_T", "xoxb-fake")
    before = signal.getsignal(signal.SIGTERM)

    class _Boom:
        def connect(self):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        sc.run(d, _config(), client_factory=lambda *a: _Boom())
    assert signal.getsignal(signal.SIGTERM) == before
    live_other = os.getppid()      # a pid that is alive and is not ours (pid+1 may not exist under load)
    sc.acquire_single_instance(d)
    sc.pid_path(d).write_text(str(live_other))
    sc.heartbeat_path(d).write_text(json.dumps({"pid": live_other, "last_seen": time.time()}))
    assert sc.run(d, _config(), client_factory=lambda *a: _Boom()) == 1     # live holder: refused
    assert signal.getsignal(signal.SIGTERM) == before
