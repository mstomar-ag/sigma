"""sigma-doctor: a setup check-up. doctor.check() audits only what THIS project's config makes relevant
(github board -> gh auth+scope; KG -> builder; vision-first -> north-star) and returns each check with
the exact one-line fix. The command runner is injectable so these are hermetic (no real gh/graphify)."""
import base64, json, os, pathlib, importlib.util, shutil, stat, subprocess, sys, tempfile, time

import pytest

import gqlfake

D = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-doctor" / "scripts" / "doctor.py"


# #895 slice 2c: doctor's label scans read REST first. These helpers key the fakes on the REST
# issues-LIST argv (`gh api repos/<r>/issues --method GET -f labels=..`), which is what a list read is.
def _is_list(args):
    return gqlfake.rest_list_params(args[1:]) is not None


def _list_label(args):
    return gqlfake.rest_list_params(args[1:]).get("labels")


def _list_state(args):
    return gqlfake.rest_list_params(args[1:]).get("state", "open")


def _rest_row(issue):
    """gh-shape census row -> REST issues-list row (state lower, closedAt -> closed_at)."""
    out = dict(issue)
    out["state"] = str(issue.get("state") or "OPEN").lower()
    out["closed_at"] = out.pop("closedAt", None)
    return out



def _doc():
    spec = importlib.util.spec_from_file_location("doctor", D)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


#: skills/sigma-doctor/scripts/doctor.py -> skills/sigma-loop/scripts/gh_session.py, mirroring
#: doctor.py's own `_load_loop_script` cross-load target (#78's shared proxy-block classifier).
GH_SESSION = D.parent.parent.parent / "sigma-loop" / "scripts" / "gh_session.py"


def _gh_session():
    spec = importlib.util.spec_from_file_location("gh_session", GH_SESSION)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


#: skills/sigma-doctor/scripts/doctor.py -> skills/sigma-loop/scripts/actionlog.py, same cross-load
#: shape as GH_SESSION above -- needed so local-action-log fixtures can reuse actionlog.py's own
#: `_stamp()` rather than a test-local reimplementation of its millisecond-timestamp format.
ACTIONLOG = D.parent.parent.parent / "sigma-loop" / "scripts" / "actionlog.py"


def _actionlog():
    spec = importlib.util.spec_from_file_location("actionlog", ACTIONLOG)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def _sdlc(d, cfg):
    base = pathlib.Path(d) / ".sdlc"; base.mkdir(parents=True)
    (base / "config.json").write_text(json.dumps(cfg))
    return str(base)


def _runner(gh_auth="", builder=""):
    """Fake command runner: canned stdout for the probes, '' = command unavailable/failed."""
    def run(args):
        if args[:3] == ["gh", "auth", "status"]:
            return gh_auth
        if len(args) >= 2 and args[1] == "--version":   # <builder> --version
            return builder
        return ""
    return run


def _by_name(checks):
    return {c["name"]: c for c in checks}


@pytest.fixture(autouse=True)
def _isolate_claude_config(monkeypatch, tmp_path_factory):
    """Hermetic by default — the promise this file's own docstring already makes, extended to the
    one input that is not injected through `run`. Since #1604 `check()` reads
    `~/.claude/plugins/installed_plugins.json` whenever nothing is passed, so without this every
    test here would inherit whatever the developer's machine happens to have installed: on a machine
    with a stale project-scope entry the new row goes (correctly) red and fails tests that have
    nothing to do with install scoping — measured, not hypothetical. Pointing `CLAUDE_CONFIG_DIR`
    (the same env var doctor honours for the real lookup) at an empty directory makes "no install
    records, row omitted" the default; a test that wants records passes `installed_plugins_path=`.
    """
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path_factory.mktemp("claude-config")))
    monkeypatch.delenv("CODEX_SESSION_ID", raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def test_flags_missing_gh_project_scope():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"discovery": {"source": "github", "github": {"project": {"enabled": True}}}})
        c = _by_name(d.check(base, run=_runner(gh_auth="Logged in. Token scopes: 'repo', 'workflow'")))
        assert c["gh auth"]["ok"] is True
        assert c["gh project scope"]["ok"] is False
        assert "gh auth refresh -s project" in c["gh project scope"]["fix"]


def test_passes_when_scope_present():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"discovery": {"source": "github", "github": {"project": {"enabled": True}}}})
        c = _by_name(d.check(base, run=_runner(gh_auth="scopes: 'repo', 'project'")))
        assert c["gh project scope"]["ok"] is True


# --- #78: "gh auth" distinguishes a Claude Code Remote session's proxy block from a real no/bad token ---


def test_gh_auth_check_default_failure_keeps_the_pre_existing_fix():
    """A plain "" failure — every existing fake `run`'s shape (nothing captured beyond pass/fail),
    and what a real "gh not installed" failure degrades to as well — must keep reporting the
    pre-#78 message unchanged: this is the ordinary no/bad-token case, not the proxy block."""
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"discovery": {"source": "github"}})
        c = _by_name(d.check(base, run=_runner()))              # default gh_auth=""
        assert c["gh auth"]["ok"] is False
        # #229: the login now names the host and the scopes the loop needs, then what Sigma does
        # meanwhile; still `gh auth login` because gh IS installed here (the fake `which`).
        assert c["gh auth"]["fix"].startswith("run: gh auth login -h github.com -s repo")


def test_gh_auth_check_diagnoses_a_claude_code_remote_session_proxy_block():
    """The actual fix: when the failed call's raw text (carried on a `_RawFailure`, exactly like the
    REAL runner would produce) is the App-connection-required proxy shape, the printed fix must name
    the real remediation, not `gh auth login`."""
    d = _doc()
    gh_session = _gh_session()
    proxy_text = ("GitHub access is not enabled for this session. An org admin must connect the "
                  "Claude GitHub App for this organization.")

    def run(args):
        if args[:3] == ["gh", "auth", "status"]:
            return d._RawFailure(proxy_text)
        return ""

    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"discovery": {"source": "github"}})
        c = _by_name(d.check(base, run=run))
        assert c["gh auth"]["ok"] is False
        assert c["gh auth"]["fix"].startswith(gh_session.REMEDIATION)
        assert "gh auth login" not in c["gh auth"]["fix"].replace("`gh auth login` will not", "")
        assert "claude github app" in c["gh auth"]["fix"].lower()


def test_gh_auth_check_diagnoses_the_graphql_pinned_ops_proxy_shape():
    """The OTHER confirmed proxy shape (#78) — `gh auth status` itself is GraphQL-backed and gets
    the pinned-ops 403, not the App-connection one — must be recognized too."""
    d = _doc()
    proxy_text = ("This GraphQL query (UserCurrent, sent by gh auth status) is not enabled for "
                  "this session - only the pinned set of PR-review operations is served.")

    def run(args):
        if args[:3] == ["gh", "auth", "status"]:
            return d._RawFailure(proxy_text)
        return ""

    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"discovery": {"source": "github"}})
        c = _by_name(d.check(base, run=run))
        assert c["gh auth"]["ok"] is False
        assert "claude github app" in c["gh auth"]["fix"].lower()


def test_gh_auth_check_still_passes_on_success():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"discovery": {"source": "github"}})
        c = _by_name(d.check(base, run=_runner(gh_auth="Logged in. Token scopes: 'repo', 'workflow'")))
        assert c["gh auth"]["ok"] is True
        assert c["gh auth"]["fix"] == ""


def test_raw_failure_is_falsy_and_string_equal_to_empty_but_carries_raw():
    """Pins `_RawFailure`'s own contract directly (doctor.py's module docstring): falsy, `==` "",
    safe wherever a plain "" already was, while still carrying the diagnostic text on `.raw`."""
    d = _doc()
    rf = d._RawFailure("some diagnostic text")
    assert not rf
    assert rf == ""
    assert bool(rf) is False
    assert rf.raw == "some diagnostic text"
    # every OTHER check's `bool(run(...))` / `"x" in run(...)` / `json.loads(run(...))` usage must
    # keep behaving exactly as it did against a plain "" — spot-check the two shapes actually used:
    assert "project" not in rf
    import json as _json
    try:
        _json.loads(rf)
        assert False, "an empty string must still fail to parse as JSON, unchanged"
    except _json.JSONDecodeError:
        pass


def test_real_run_returns_a_raw_failure_carrying_the_real_stderr(tmp_path):
    """End-to-end proof for the PRODUCTION runner (not just the DI-level tests above): a genuinely
    failing subprocess call returns something falsy (the unchanged contract every other check relies
    on) that also carries the real combined stdout+stderr on `.raw`."""
    d = _doc()
    script = tmp_path / "fake-gh"
    script.write_text("#!/bin/sh\necho 'boom' >&2\nexit 1\n")
    script.chmod(0o755)
    result = d._real_run([str(script)])
    assert result == ""
    assert bool(result) is False
    assert "boom" in result.raw


def test_real_run_bounds_a_hung_github_project_call_from_the_operator_timeout(monkeypatch):
    """A stalled Project API read must degrade to a falsy timeout result, never hang doctor."""
    d = _doc()
    calls = []

    def hung(argv, **kwargs):
        calls.append((argv, kwargs))
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

    monkeypatch.setenv("SIGMA_WATCH_CALL_TIMEOUT", "2")
    monkeypatch.setattr(d.subprocess, "run", hung)
    result = d._real_run(["gh", "project", "item-list", "17", "--owner", "acme"])

    assert calls[0][1]["timeout"] == 2
    assert not result
    assert "timed out" in result.raw.lower()


def test_real_run_bounds_a_hung_github_project_graphql_call(monkeypatch):
    """ProjectV2 workflow reads share the same finite board-call budget."""
    d = _doc()
    calls = []

    def hung(argv, **kwargs):
        calls.append((argv, kwargs))
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

    monkeypatch.setenv("SIGMA_WATCH_CALL_TIMEOUT", "2")
    monkeypatch.setattr(d.subprocess, "run", hung)
    result = d._real_run(["gh", "api", "graphql", "-f", "query={ viewer { projectsV2 { nodes { id } } } }"])

    assert calls[0][1]["timeout"] == 2
    assert not result
    assert "timed out" in result.raw.lower()


def test_real_run_bounds_a_hung_github_issue_list_call(monkeypatch):
    """A stalled GitHub backlog scan must also degrade instead of hanging doctor."""
    d = _doc()
    calls = []

    def hung(argv, **kwargs):
        calls.append((argv, kwargs))
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

    monkeypatch.setenv("SIGMA_WATCH_CALL_TIMEOUT", "2")
    monkeypatch.setattr(d.subprocess, "run", hung)
    result = d._real_run(["gh", "issue", "list", "--repo", "acme/widget"])

    assert calls[0][1]["timeout"] == 2
    assert not result
    assert "timed out" in result.raw.lower()


def test_real_run_keeps_local_doctor_probes_uncapped(monkeypatch):
    """The Project API cap must not reclassify a slow local diagnostic as a missing tool."""
    d = _doc()
    calls = []

    class Completed:
        returncode = 0
        stdout = "installed\n"
        stderr = ""

    def local(argv, **kwargs):
        calls.append((argv, kwargs))
        return Completed()

    monkeypatch.setenv("SIGMA_WATCH_CALL_TIMEOUT", "2")
    monkeypatch.setattr(d.subprocess, "run", local)
    assert d._real_run(["codex", "plugin", "list", "--json"]) == "installed\n"
    assert "timeout" not in calls[0][1]


def test_flags_missing_kg_builder():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"knowledge_graph": {"enabled": True, "builder": "graphify"}})
        c = _by_name(d.check(base, run=_runner(builder="")))         # graphify --version fails
        assert c["graphify installed"]["ok"] is False
        assert "pip install graphifyy" in c["graphify installed"]["fix"]


def test_flags_graphify_skill_package_version_mismatch_with_the_repair_command():
    """#132: a zero exit is not healthy when graphify itself names a stale installed skill."""
    d = _doc()
    observed = ("warning: skill is from graphify 0.8.14, package is 0.8.39. "
                "Run 'graphify install' to update.\ngraphify 0.8.39\n")
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"knowledge_graph": {"enabled": True, "builder": "graphify"}})
        c = _by_name(d.check(base, run=_runner(builder=observed)))
        row = c["graphify skill/package versions match"]
        assert row["ok"] is False
        assert "graphify install" in row["fix"]


# --------------------------------------------------------------------------- issue #1562
# `auto_refresh: true` used to be completely inert AND completely silent. The state this repo was
# measured in on 2026-09-02 -- auto_refresh on, a 526-document corpus, `graph: not built` -- produced
# no error, no warning and no doctor row anywhere, which is precisely the LIVENESS failure AGENTS.md
# names: a component that has DIED must be distinguishable from one with nothing to do. Wiring the
# trigger without this row would leave the more damaging half in place, because on a host with no
# LLM backend (this one) the refresh fails on EVERY goal and something has to say so.


def test_flags_auto_refresh_on_but_the_graph_was_never_built():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"knowledge_graph": {"enabled": True, "builder": "graphify",
                                             "auto_refresh": True}})
        c = _by_name(d.check(base, run=_runner(builder="graphify 1.0")))
        row = c["knowledge graph auto-refresh is working"]
        assert row["ok"] is False
        assert "auto_refresh" in row["fix"] and "/sigma-kg" in row["fix"]


def test_auto_refresh_on_with_a_built_graph_is_ok():
    """The positive case. Without it the row above passes for a check hardcoded to False."""
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"knowledge_graph": {"enabled": True, "builder": "graphify",
                                             "auto_refresh": True}})
        out = pathlib.Path(t) / "graphify-out"; out.mkdir(); (out / "graph.json").write_text("{}")
        c = _by_name(d.check(base, run=_runner(builder="graphify 1.0")))
        assert c["knowledge graph auto-refresh is working"]["ok"] is True


# --------------------------------------------------------------------------- issue #2055
# Presence was only ever the WEAKER tell. AGENTS.md's LIVENESS property is explicit that age is the
# tell, not error state: a graph built once and then frozen (trigger silently broken again, the
# builder failing every run, an expired credential) reads OK indefinitely under a presence-only
# check. These three tests pin the three states the doctor row must now distinguish -- absent,
# stale (built, but the corpus has grown since), and fresh (built, and caught up with the corpus).


def test_flags_stale_graph_when_corpus_has_a_newer_document():
    """The graph exists but is older than a corpus document -- exactly the "frozen, no error
    anywhere" shape #1562 was itself a two-week-long instance of."""
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"knowledge_graph": {"enabled": True, "builder": "graphify",
                                             "auto_refresh": True}})
        out = pathlib.Path(t) / "graphify-out"; out.mkdir()
        graph = out / "graph.json"; graph.write_text("{}")
        old = time.time() - 3600
        os.utime(graph, (old, old))                      # the graph is an hour old
        research = pathlib.Path(base) / "knowledge" / "research"; research.mkdir(parents=True)
        (research / "new-finding.md").write_text("# fresh corpus doc\n")   # written just now
        c = _by_name(d.check(base, run=_runner(builder="graphify 1.0")))
        row = c["knowledge graph auto-refresh is working"]
        assert row["ok"] is False
        assert "stale" in row["fix"].lower()
        assert "/sigma-kg" in row["fix"]


def test_graph_newer_than_the_corpus_is_fresh_not_stale():
    """The positive case with a REAL corpus present (not the trivial "no corpus at all" case
    `test_auto_refresh_on_with_a_built_graph_is_ok` above already covers) -- the graph has caught up
    with everything the corpus holds."""
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"knowledge_graph": {"enabled": True, "builder": "graphify",
                                             "auto_refresh": True}})
        research = pathlib.Path(base) / "knowledge" / "research"; research.mkdir(parents=True)
        doc = research / "old-finding.md"; doc.write_text("# old corpus doc\n")
        old = time.time() - 3600
        os.utime(doc, (old, old))                         # the corpus document is an hour old
        out = pathlib.Path(t) / "graphify-out"; out.mkdir()
        (out / "graph.json").write_text("{}")             # graph built just now, after the doc
        c = _by_name(d.check(base, run=_runner(builder="graphify 1.0")))
        assert c["knowledge graph auto-refresh is working"]["ok"] is True


def test_a_quiet_corpus_is_not_stale():
    """Definition of done: staleness is measured against the corpus, not a fixed interval -- a
    corpus that has not grown since the graph was built must never read as stale just because
    wall-clock time has passed."""
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"knowledge_graph": {"enabled": True, "builder": "graphify",
                                             "auto_refresh": True}})
        research = pathlib.Path(base) / "knowledge" / "research"; research.mkdir(parents=True)
        doc = research / "old-finding.md"; doc.write_text("# old corpus doc\n")
        very_old = time.time() - (86400 * 30)
        os.utime(doc, (very_old, very_old))               # a month old
        out = pathlib.Path(t) / "graphify-out"; out.mkdir()
        graph = out / "graph.json"; graph.write_text("{}")
        os.utime(graph, (very_old + 60, very_old + 60))    # built shortly AFTER the doc, ages ago
        c = _by_name(d.check(base, run=_runner(builder="graphify 1.0")))
        assert c["knowledge graph auto-refresh is working"]["ok"] is True


def test_auto_refresh_off_produces_no_such_row_at_all():
    """NEGATIVE CONTROL on the gate. `enabled` alone must not raise this row: a project that
    deliberately builds the graph by hand with /sigma-kg has nothing wrong with it, and reporting a
    never-built graph as a fault there would be a false alarm on every doctor run."""
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"knowledge_graph": {"enabled": True, "builder": "graphify",
                                             "auto_refresh": False}})
        names = [c["name"] for c in d.check(base, run=_runner(builder="graphify 1.0"))]
        assert "knowledge graph auto-refresh is working" not in names
        assert "graphify installed" in names            # the existing row is untouched


def test_skips_irrelevant_checks_for_local_zero_dep():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"discovery": {"source": "local-goals"}})
        names = [c["name"] for c in d.check(base, run=_runner())]
        assert not any("gh" in n or "graphify" in n or "north-star" in n for n in names)
        assert "project layer" in names                              # always checked


def test_main_runs():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"discovery": {"source": "local-goals"}})
        assert d.main(["doctor.py", "check", base]) == 0


def test_detects_companions_never_failing_on_absence():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"discovery": {"source": "local-goals"}})
        def run(args):
            return "superpowers@claude-plugins-official" if args == ["claude", "plugin", "list"] else _runner()(args)
        checks = d.check(base, run=run)
        names = " | ".join(c["name"] for c in checks)
        assert "superpowers: present" in names            # detected installed
        assert "code-review: absent" in names             # detected missing
        assert all(c["ok"] for c in checks if "superpowers" in c["name"] or "code-review" in c["name"])


def _codex_plugin_runner(installed, *, latest="1.4.13"):
    """Codex's observed `plugin list --json` envelope; gh serves the same marketplace source."""
    import base64
    calls = []

    def run(args):
        calls.append(args)
        if args == ["codex", "plugin", "list", "--json"]:
            return json.dumps({"installed": installed, "available": []}) if installed is not None else "not json"
        if args[:2] == ["gh", "api"] and any("contents/.claude-plugin/marketplace.json" in a for a in args):
            return base64.b64encode(json.dumps({"plugins": [{"name": "sigmaloop", "version": latest}]}).encode()).decode()
        return _runner()(args)

    return run, calls


def _codex_entry(name, version="1.4.13", *, installed=True, enabled=True):
    return {"pluginId": f"{name}@sigmaloop" if name == "sigmaloop" else f"{name}@claude-plugins-official",
            "name": name, "version": version, "installed": installed, "enabled": enabled,
            "marketplaceName": "sigmaloop" if name == "sigmaloop" else "claude-plugins-official"}


def test_codex_doctor_uses_enabled_install_for_floor_version_and_companions(monkeypatch, tmp_path):
    """A Codex session must not inherit another host's healthy install or companion inventory."""
    monkeypatch.setenv("CODEX_SESSION_ID", "session")
    d = _doc()
    v = _one_above(d._agents_floor())
    base = _sdlc(tmp_path, {})
    run, calls = _codex_plugin_runner([_codex_entry("sigmaloop", v),
                                       _codex_entry("superpowers"),
                                       _codex_entry("code-review", installed=False, enabled=False)],
                                      latest=v)

    checks = d.check(base, run=run)
    names = _by_name(checks)
    floor_row = next(c for c in checks if c["name"].startswith("sigma Codex install:"))
    assert floor_row["ok"] is True and v in floor_row["name"]
    assert names[f"sigma up to date (installed {v})"]["ok"] is True
    assert "superpowers: present" in names
    assert "code-review: absent — portable executor used" in names
    assert calls.count(["codex", "plugin", "list", "--json"]) == 1
    assert not any(a[:2] == ["claude", "plugin"] for a in calls)


def test_codex_doctor_rejects_below_floor_install_even_with_healthy_claude_scopes(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_SESSION_ID", "session")
    d = _doc()
    base = _sdlc(tmp_path / "repo", {})
    claude_plugins = _plugins_file(tmp_path, [_entry("user", _one_above(d._agents_floor()))])
    run, _ = _codex_plugin_runner([_codex_entry("sigmaloop", _one_below(d._agents_floor()))])

    checks = d.check(base, run=run, installed_plugins_path=claude_plugins)
    row = next(c for c in checks if c["name"].startswith("sigma Codex install:"))
    assert row["ok"] is False
    assert "below" in row["name"] and "AGENTS.md floor" in row["name"]
    assert _scope_row(checks) is None
    assert "claude plugin" not in row["fix"]


def test_codex_doctor_does_not_show_healthy_when_plugin_list_is_unreadable(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_SESSION_ID", "session")
    d = _doc()
    run, calls = _codex_plugin_runner(None)
    checks = d.check(_sdlc(tmp_path, {}), run=run)
    row = next(c for c in checks if c["name"].startswith("sigma Codex install:"))
    assert row["ok"] is False and "unverified" in row["name"]
    assert not any(c["name"].startswith("sigma up to date") for c in checks)
    assert not any(c["name"].startswith("superpowers: present") for c in checks)
    assert calls.count(["codex", "plugin", "list", "--json"]) == 1


@pytest.mark.parametrize("entry", [None, _codex_entry("sigmaloop", enabled=False),
                                    _codex_entry("sigmaloop", installed=False)])
def test_codex_doctor_refuses_missing_or_disabled_loop_install(monkeypatch, tmp_path, entry):
    monkeypatch.setenv("CODEX_SESSION_ID", "session")
    d = _doc()
    run, _ = _codex_plugin_runner([entry] if entry else [])
    checks = d.check(_sdlc(tmp_path, {}), run=run)
    row = next(c for c in checks if c["name"].startswith("sigma Codex install:"))
    assert row["ok"] is False
    assert "absent or disabled" in row["name"]
    assert not any(c["name"].startswith("sigma up to date") for c in checks)


def test_codex_cheap_check_does_not_spawn_plugin_cli_or_claim_floor(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_SESSION_ID", "session")
    d = _doc()
    run, calls = _codex_plugin_runner([_codex_entry("sigmaloop")])
    checks = d.check(_sdlc(tmp_path, {}), run=run, cheap_only=True)
    assert not any(a[:2] == ["codex", "plugin"] for a in calls)
    assert not any(c["name"].startswith("sigma Codex install:") for c in checks)
    assert not any(c["name"].startswith("sigma up to date") for c in checks)


def test_codex_update_nudge_uses_codex_remediation(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_SESSION_ID", "session")
    d = _doc()
    run, _ = _codex_plugin_runner([_codex_entry("sigmaloop")], latest="1.4.14")
    row = _by_name(d.check(_sdlc(tmp_path, {}), run=run))["sigma up to date (installed 1.4.13)"]
    assert row["ok"] is False
    assert "Codex" in row["fix"] and "claude plugin" not in row["fix"]


def test_claude_marker_takes_precedence_over_codex_marker(monkeypatch, tmp_path):
    """A Claude session with inherited Codex env must keep its original CLI and scope route."""
    monkeypatch.setenv("CODEX_SESSION_ID", "inherited")
    monkeypatch.setenv("CLAUDECODE", "1")
    d = _doc()
    calls = []

    def run(args):
        calls.append(args)
        return _version_runner()(args)

    checks = d.check(_sdlc(tmp_path, {}), run=run)
    assert _by_name(checks)["sigma up to date (installed 0.9.7)"]["ok"] is False
    assert any(a[:3] == ["claude", "plugin", "list"] for a in calls)
    assert not any(a[:2] == ["codex", "plugin"] for a in calls)


# --------------------------------------------------------------- plugin update awareness (#378)
# Auto-update is off by default for a non-Anthropic marketplace, so a stale install can otherwise
# persist silently forever. Only reported when BOTH the installed and latest versions actually
# resolve -- an unreachable network or unrecognized output must never read as a false alarm OR a
# false all-clear.

_PLUGIN_LIST_JSON = json.dumps([{"id": "sigmaloop@sigmaloop", "version": "0.9.7"},
                                 {"id": "other-plugin@some-marketplace", "version": "1.2.3"}])
_MARKETPLACE_JSON = json.dumps({"plugins": [{"name": "sigmaloop", "version": "0.9.23"},
                                             {"name": "other-plugin", "version": "1.2.3"}]})


def _version_runner(plugin_list=_PLUGIN_LIST_JSON, marketplace=_MARKETPLACE_JSON):
    """#1739: `marketplace` is still handed in as plain JSON text (every existing caller of this
    fixture keeps working unchanged) -- base64-encoded here, matching what real `gh api ... --jq
    .content` actually returns, so `_plugin_versions`'s own decode step is exercised for real
    rather than bypassed by the fixture."""
    import base64
    def run(args):
        if args[:3] == ["claude", "plugin", "list"] and "--json" in args:
            return plugin_list
        if args[:2] == ["gh", "api"] and any("contents/.claude-plugin/marketplace.json" in a for a in args):
            return base64.b64encode(marketplace.encode()).decode() if marketplace else ""
        return _runner()(args)
    return run


def test_version_tuple_parses_plain_dotted_integers():
    d = _doc()
    assert d._version_tuple("0.9.23") == (0, 9, 23)
    assert d._version_tuple("1.0.0") == (1, 0, 0)


def test_version_tuple_is_none_for_anything_unparseable():
    d = _doc()
    assert d._version_tuple("not-a-version") is None
    assert d._version_tuple(None) is None
    assert d._version_tuple("") is None


def test_version_tuple_compares_numerically_not_lexicographically():
    """0.9.23 > 0.9.7 numerically, even though '2' < '7' as characters -- a naive string compare
    would get this exactly backwards."""
    d = _doc()
    assert d._version_tuple("0.9.23") > d._version_tuple("0.9.7")


def test_plugin_versions_resolves_both_sides():
    d = _doc()
    installed, latest = d._plugin_versions(_version_runner())
    assert installed == (0, 9, 7)
    assert latest == (0, 9, 23)


def _api_paths(seen):
    """The `gh api` paths a runner recorded, in order."""
    return [a[2] for a in seen if a[:2] == ["gh", "api"]]


def _recording_runner(plugin_list=None):
    """`_version_runner`, recording every call: the fake handler matches ANY `gh api` call to the
    marketplace-contents shape regardless of repo, so nothing but this recording pins WHICH repo
    the fetch names -- a stale or ignored slug would still pass every other test in this file."""
    seen = []
    inner = _version_runner() if plugin_list is None else _version_runner(plugin_list=plugin_list)
    def run(args):
        seen.append(args)
        return inner(args)
    return run, seen


def _known_marketplaces(text):
    """Claude Code's marketplace registry, under the hermetic `CLAUDE_CONFIG_DIR` the autouse
    fixture points at; `text` is written verbatim (so a malformed registry can be planted)."""
    path = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "plugins" / "known_marketplaces.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if isinstance(text, str) else json.dumps(text), encoding="utf-8")
    return path


def _contents_path(repo):
    return "repos/%s/contents/.claude-plugin/marketplace.json" % repo


def test_plugin_versions_falls_back_to_the_public_slug_without_a_marketplace_record():
    """D12 (#2730): with no marketplace record to read, the fetch names the public repository --
    the one string `_MARKETPLACE_REPO` holds -- through authenticated `gh api`."""
    d = _doc()
    run, seen = _recording_runner()
    d._plugin_versions(run)
    assert _api_paths(seen) == [_contents_path(d._MARKETPLACE_REPO)], seen
    assert d._MARKETPLACE_REPO == "Agrim-Intelligence/sigmaloop"
    assert " ".join(next(a for a in seen if a[:2] == ["gh", "api"])).startswith("gh api")


def test_plugin_versions_reads_the_marketplace_the_plugin_was_installed_from():
    """#2730: a fork installed as `sigmaloop@acme-market` is compared against ITS marketplace, not the
    public one -- otherwise a fork's version would be judged against upstream (a false alarm).
    The decoy `sigma` key sorts first and names the public repo; the match must be by the
    marketplace half of the installed id."""
    d = _doc()
    _known_marketplaces({"sigmaloop": {"source": {"source": "github", "repo": "Agrim-Intelligence/sigmaloop"}},
                         "acme-market": {"source": {"source": "github", "repo": "Acme/sigma-fork"}}})
    run, seen = _recording_runner(json.dumps([{"id": "sigmaloop@acme-market", "version": "0.9.7"}]))
    installed, latest = d._plugin_versions(run)
    assert _api_paths(seen) == [_contents_path("Acme/sigma-fork")], seen
    assert installed == (0, 9, 7) and latest == (0, 9, 23)


@pytest.mark.parametrize("registry", [
    "not json",
    "[]",
    "{}",
    {"acme-market": {"source": {"source": "git", "url": "https://github.com/Acme/x.git"}}},
    {"acme-market": {"source": {"source": "directory", "path": "/tmp/x"}}},
    {"acme-market": {"source": {"source": "github", "repo": "../../etc"}}},
    {"acme-market": {"source": {"source": "github", "repo": "Acme/../etc"}}},
    {"acme-market": {"source": {"source": "github", "repo": 7}}},
    {"acme-market": {"source": "github"}},
    {"acme-market": "Acme/sigma-fork"},
])
def test_plugin_versions_falls_back_on_an_unverified_or_malformed_record(registry):
    """Only Claude's `github` source shape is verified; anything else -- or a repo that is not a
    plain `owner/repo` slug -- falls back to the public repository and never raises."""
    d = _doc()
    _known_marketplaces(registry)
    run, seen = _recording_runner(json.dumps([{"id": "sigmaloop@acme-market", "version": "0.9.7"}]))
    installed, latest = d._plugin_versions(run)
    assert _api_paths(seen) == [_contents_path(d._MARKETPLACE_REPO)], (registry, seen)
    assert installed == (0, 9, 7) and latest == (0, 9, 23)


@pytest.mark.parametrize("source,expected", [
    ({"sourceType": "git", "source": "https://github.com/Acme/sigma-fork.git"}, "Acme/sigma-fork"),
    ({"sourceType": "git", "source": "https://github.com/Acme/sigma-https"}, "Acme/sigma-https"),
    ({"sourceType": "git", "source": "git@github.com:Acme/sigma-ssh.git"}, "Acme/sigma-ssh"),
    ({"sourceType": "git", "source": "https://gitlab.example/Acme/elsewhere.git"}, None),
    ({"sourceType": "git", "source": "https://github.com/Acme/../x"}, None),
    ({"sourceType": "local", "source": "/tmp/x"}, None),
    (None, None),
])
def test_plugin_versions_reads_the_codex_marketplace_source(source, expected):
    """#2730: Codex records the marketplace it installed from as `marketplaceSource` (shape verified
    live: `sourceType: git` with a GitHub URL, HTTPS or SSH). Anything else -> the public repo."""
    d = _doc()
    entry = _codex_entry("sigmaloop", "0.9.7")
    if source is not None:
        entry["marketplaceSource"] = source
    run, seen = _recording_runner()
    installed, latest = d._plugin_versions(run, host="codex", codex_plugins=[entry])
    assert _api_paths(seen) == [_contents_path(expected or d._MARKETPLACE_REPO)], (source, seen)
    assert installed == (0, 9, 7) and latest == (0, 9, 23)
    assert not any(a[:2] == ["claude", "plugin"] for a in seen)


def _stub(bin_dir, name, body):
    path = bin_dir / name
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.mark.live_gh          # the guard keys on argv[0]; what this reaches is the PATH stub below
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell stubs")
def test_version_nudge_via_a_stubbed_gh_reaches_the_public_repo_and_never_the_private_one(
        tmp_path, monkeypatch):
    """The issue's doctor control (#2730), through REAL subprocesses (hence `live_gh`, the suite's
    one escape hatch for a process named `gh` -- no network is reached: PATH is set so the stub
    shadows any real CLI, and the stub answers every other path with a 404): a `gh` that serves
    the public repository's marketplace.json and 404s every other path, and a `claude` that reports
    `sigmaloop@sigmaloop 1.0.0`. (a) a record naming the public repo -> the nudge fires; (b) a record naming
    another (the private repository's stand-in, which the stub 404s) -> no row at all: can't-tell,
    never a false all-clear, never a false alarm from upstream's version."""
    d = _doc()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    payload = base64.b64encode(json.dumps({"plugins": [{"name": "sigmaloop", "version": "9.9.9"}]}).encode()).decode()
    _stub(bin_dir, "gh", 'for a in "$@"; do\n  if [ "$a" = "%s" ]; then echo "%s"; exit 0; fi\ndone\n'
                        'echo "gh: Not Found (HTTP 404)" >&2\nexit 1\n' % (_contents_path(d._MARKETPLACE_REPO), payload))
    _stub(bin_dir, "claude", "echo '[{\"id\": \"sigmaloop@sigmaloop\", \"version\": \"1.0.0\"}]'\n")
    monkeypatch.setenv("PATH", "%s%s%s" % (bin_dir, os.pathsep, os.defpath))
    run = lambda a: d._real_run(a) if a and a[0] in ("gh", "claude") else ""
    base = _sdlc(tmp_path / "repo", {"discovery": {"source": "local-goals"}})

    _known_marketplaces({"sigmaloop": {"source": {"source": "github", "repo": d._MARKETPLACE_REPO}}})
    c = _by_name(d.check(base, run=run))
    row = c["sigma up to date (installed 1.0.0)"]
    assert row["ok"] is False and "9.9.9 is available" in row["fix"], row

    _known_marketplaces({"sigmaloop": {"source": {"source": "github", "repo": "Example-Org/closed-core"}}})
    names = [x["name"] for x in d.check(base, run=run)]
    assert not any("sigma up to date" in n for n in names), names


def test_plugin_versions_ignores_other_plugins_in_the_list():
    d = _doc()
    installed, _ = d._plugin_versions(_version_runner(
        plugin_list=json.dumps([{"id": "other-plugin@some-marketplace", "version": "9.9.9"}])))
    assert installed is None


def test_plugin_versions_is_none_when_claude_cli_is_unavailable():
    d = _doc()
    installed, latest = d._plugin_versions(_version_runner(plugin_list=""))
    assert installed is None
    assert latest == (0, 9, 23)          # the OTHER side still resolves independently


def test_plugin_versions_is_none_when_the_network_fetch_fails():
    d = _doc()
    installed, latest = d._plugin_versions(_version_runner(marketplace=""))
    assert installed == (0, 9, 7)
    assert latest is None


def test_plugin_versions_is_none_on_malformed_json_never_raises():
    d = _doc()
    installed, latest = d._plugin_versions(_version_runner(
        plugin_list="not json", marketplace="also not json"))
    assert (installed, latest) == (None, None)


def test_check_flags_an_out_of_date_install():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"discovery": {"source": "local-goals"}})
        c = _by_name(d.check(base, run=_version_runner()))
        entry = c["sigma up to date (installed 0.9.7)"]
        assert entry["ok"] is False
        assert "0.9.23 is available" in entry["fix"]
        # #1741: the bare plugin name fails live ("Plugin not found") -- pin the exact command,
        # not a loose substring a bare-name regression would still satisfy.
        assert "claude plugin update sigmaloop@sigmaloop" in entry["fix"]


def test_check_passes_when_already_current():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"discovery": {"source": "local-goals"}})
        same = json.dumps([{"id": "sigmaloop@sigmaloop", "version": "0.9.23"}])
        c = _by_name(d.check(base, run=_version_runner(plugin_list=same)))
        assert c["sigma up to date (installed 0.9.23)"]["ok"] is True


def test_check_adds_no_entry_at_all_when_either_side_is_undeterminable():
    """A can't-tell (offline, claude CLI missing, whatever) must never show as a false alarm --
    the default fake runner returns "" for everything, so both sides fail to resolve."""
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"discovery": {"source": "local-goals"}})
        names = [c["name"] for c in d.check(base, run=_runner())]
        assert not any("sigma up to date" in n for n in names)


# ------------------------------------------------------- cheap_only: the wizard's SessionStart path
# The guided setup wizard (issue #1560) calls check() UNCONDITIONALLY, once per session, in every
# repo. `_plugin_versions()` alone spawns the `claude` CLI and (#1739) an authenticated `gh api`
# fetch of the installed marketplace's own contents (#2730) --
# measured on this box at 3.82s cold / 3.00s warm for the whole call, against 0.01s with
# cheap_only=True. AGENTS.md's SAFETY property forbids spending that without the operator opting
# in, so the wizard takes the cheap subset and `/sigma-doctor` keeps the full sweep.


def _exploding_runner(*forbidden):
    """A runner that RAISES if any of the named command prefixes is ever attempted, and records
    what it did serve. Proof by explosion, this repo's own convention for "the expensive thing was
    genuinely not reached", rather than inferring it from the absence of an output row."""
    seen = []
    def run(args):
        seen.append(args)
        for prefix in forbidden:
            if args[:len(prefix)] == list(prefix):
                raise AssertionError("cheap_only=True still ran: %r" % (args,))
        return _version_runner()(args)
    return run, seen


# #1739: the exact 3-element prefix, not a bare ("gh", "api") -- doctor.py has THREE OTHER,
# legitimate `gh api` call sites (branch protection, a GraphQL query) that a bare prefix would
# wrongly also forbid if cheap_only ever reaches them; this names the marketplace-contents fetch
# specifically.
_MARKETPLACE_FETCH_PREFIX = ("gh", "api", "repos/Agrim-Intelligence/sigmaloop/contents/.claude-plugin/marketplace.json")


def test_cheap_only_skips_the_plugin_version_subprocess_and_network_calls():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"discovery": {"source": "local-goals"}})
        run, seen = _exploding_runner(("claude", "plugin", "list"), _MARKETPLACE_FETCH_PREFIX)
        checks = d.check(base, run=run, cheap_only=True)
        assert not any(a[:len(_MARKETPLACE_FETCH_PREFIX)] == list(_MARKETPLACE_FETCH_PREFIX)
                       or a[:2] == ["claude", "plugin"] for a in seen)
        names = [c["name"] for c in checks]
        assert not any("sigma up to date" in n for n in names)
        assert not any("superpowers" in n or "code-review" in n for n in names)
        # ...and the CHEAP checks are still returned normally -- this is a subset, not a stub.
        assert "project layer" in names


def test_the_default_full_check_still_makes_those_calls():
    """THE CONTROL. Without this, the test above would pass just as happily against a check()
    that had stopped doing the work entirely, or against a `cheap_only` parameter nothing reads."""
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"discovery": {"source": "local-goals"}})
        seen = []
        def run(args):
            seen.append(args)
            return _version_runner()(args)
        names = [c["name"] for c in d.check(base, run=run)]           # cheap_only defaults to False
        assert any(a[:len(_MARKETPLACE_FETCH_PREFIX)] == list(_MARKETPLACE_FETCH_PREFIX) for a in seen), \
            "the control never reached the network call"
        assert any(a[:3] == ["claude", "plugin", "list"] for a in seen)
        assert "sigma up to date (installed 0.9.7)" in names
        assert any("superpowers" in n for n in names)


def test_cheap_only_still_reports_real_setup_gaps():
    """The rows the wizard actually acts on must survive the skip -- otherwise cheap_only would
    quietly make the wizard blind instead of cheap."""
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"discovery": {"source": "github",
                                       "github": {"project": {"enabled": True}}},
                         "knowledge_graph": {"enabled": True, "builder": "graphify"}})
        run, _ = _exploding_runner(("claude", "plugin", "list"), ("curl",))
        c = _by_name(d.check(base, run=run, cheap_only=True))
        assert c["gh auth"]["ok"] is False              # the fake serves "" for gh auth status
        assert c["graphify installed"]["ok"] is False


def test_features_dashboard_reports_states(tmp_path):
    import json, importlib.util, pathlib as _pl
    spec = importlib.util.spec_from_file_location(
        "doctor", _pl.Path(__file__).resolve().parent.parent / "skills" / "sigma-doctor" / "scripts" / "doctor.py")
    d = importlib.util.module_from_spec(spec); spec.loader.exec_module(d)
    base = tmp_path / ".sdlc"; base.mkdir()
    base.joinpath("config.json").write_text(json.dumps(
        {"model_selection": "auto", "verify": {"enforce": True}}))
    rows = {name: state for name, state, _ in d.features(str(base))}
    assert "AUTO" in rows["model+effort auto-selection"]
    assert rows["machine-checked done (verify.enforce)"].startswith("ON")
    assert "off" in rows["hard plan-gate (deny source edits w/o fresh plan)"]
    assert rows["backlog source"] == "local-goals"
    assert rows["team ledger"].startswith("off")           # an absent block reads as off


def test_features_reports_the_plan_review_gate(tmp_path):
    """#258: the dashboard answers "is the plan-review gate on here?" the way `work.py pr` would --
    including the ON-but-not-enforced case, where `work.enabled` is off and `pr` never runs."""
    import json, importlib.util, pathlib as _pl
    spec = importlib.util.spec_from_file_location(
        "doctor", _pl.Path(__file__).resolve().parent.parent / "skills" / "sigma-doctor" / "scripts" / "doctor.py")
    d = importlib.util.module_from_spec(spec); spec.loader.exec_module(d)
    key = "plan-review gate (PR push needs an approving review of the exact plan)"

    def state(config, name):
        base = tmp_path / name / ".sdlc"; base.mkdir(parents=True)
        base.joinpath("config.json").write_text(json.dumps(config))
        return {row: value for row, value, _ in d.features(str(base))}[key]

    on = {"work": {"enabled": True}, "gates": {"plan_review": {"enabled": True}}}
    assert state(on, "on").startswith("ON"), state(on, "on")
    assert "NOT ENFORCED" in state({**on, "work": {"enabled": False}}, "work-off")
    assert state({}, "absent") == "off"


def test_features_flags_a_legacy_env_var_name_under_any_env_key(tmp_path):
    """#2729 (D24/D25, plan D-p): one informational row names a `*_env` config value that still
    carries the retired brand's env-var prefix, at any nesting, by dotted key path -- and reads
    `none` on a clean config. The fixture value is built from the doctor's own fragment-built
    attribute, never spelled here, so neither file carries the retired spelling whole."""
    import json, importlib.util, pathlib as _pl
    spec = importlib.util.spec_from_file_location(
        "doctor", _pl.Path(__file__).resolve().parent.parent / "skills" / "sigma-doctor" / "scripts" / "doctor.py")
    d = importlib.util.module_from_spec(spec); spec.loader.exec_module(d)
    legacy = d._RETIRED_ENV_PREFIX + "SLACK_BOT_TOKEN"
    assert legacy.endswith("_SLACK_BOT_TOKEN") and legacy[:1].isupper() and "SIGMA" not in legacy
    stale = tmp_path / "stale" / ".sdlc"; stale.mkdir(parents=True)
    stale.joinpath("config.json").write_text(json.dumps({
        "slack_commands": {"bot_token_env": legacy},
        "watch": {"pass_env": ["HOME", "SIGMA_RUN_ID", d._RETIRED_ENV_PREFIX + "CLAUDE_CMD"]},
        "notify": {"smtp": {"pass_env": "SIGMA_SMTP_PASS"}},
    }))
    clean = tmp_path / "clean" / ".sdlc"; clean.mkdir(parents=True)
    clean.joinpath("config.json").write_text(json.dumps({"slack_commands": {"bot_token_env": "SIGMA_SLACK_BOT_TOKEN"}}))
    rows_stale = {name: (state, fix) for name, state, fix in d.features(str(stale))}
    rows_clean = {name: (state, fix) for name, state, fix in d.features(str(clean))}
    state, fix = rows_stale["legacy env-var names in config"]
    assert "legacy env-var name in config: slack_commands.bot_token_env=%s; rename to its SIGMA_* spelling" % legacy in state, state
    assert "watch.pass_env=%sCLAUDE_CMD" % d._RETIRED_ENV_PREFIX in state, state
    assert "SIGMA_SMTP_PASS" not in state and "SIGMA_RUN_ID" not in state, state
    # Post-PR review of #2750: the remedy must be one the user can perform today. Since #239 that is
    # the migration script that now ships -- and the named script must exist on disk.
    assert fix == ("run: python3 skills/sigma-doctor/scripts/migrate.py .sdlc (dry run), then --apply; "
                   "and rename the environment variable it names to match"), fix
    assert (_pl.Path(d.__file__).resolve().parent / "migrate.py").is_file()
    assert "rebrand_migrate" not in state and "rebrand_migrate" not in fix, (state, fix)
    assert "rebrand_migrate" not in _pl.Path(d.__file__).read_text()
    assert rows_clean["legacy env-var names in config"][0] == "none"
    assert d._legacy_env_names_in_config({}) == [] and d._legacy_env_names_in_config([1, "x"]) == []


def test_features_reports_decision_tier_state(tmp_path):
    """#1185: decision_tier was undiscoverable (absent from README, SKILL.md, the scaffolded
    template, and sigma-doctor alike). This pins the doctor half of that fix -- the dashboard must
    show whether it's on, matching every other opt-in feature row's convention."""
    import json, importlib.util, pathlib as _pl
    spec = importlib.util.spec_from_file_location(
        "doctor", _pl.Path(__file__).resolve().parent.parent / "skills" / "sigma-doctor" / "scripts" / "doctor.py")
    d = importlib.util.module_from_spec(spec); spec.loader.exec_module(d)

    base_off = tmp_path / "off" / ".sdlc"; base_off.mkdir(parents=True)
    base_off.joinpath("config.json").write_text(json.dumps({}))
    rows_off = {name: state for name, state, _ in d.features(str(base_off))}
    assert rows_off["decision tier (advisory park-detail classifier)"].lower().startswith("off")

    base_on = tmp_path / "on" / ".sdlc"; base_on.mkdir(parents=True)
    base_on.joinpath("config.json").write_text(json.dumps({"decision_tier": "auto"}))
    rows_on = {name: state for name, state, _ in d.features(str(base_on))}
    assert "AUTO" in rows_on["decision tier (advisory park-detail classifier)"]


def test_features_budgets_row_reports_off_for_absent_or_zero_max_iterations(tmp_path):
    """#432: PR #431 (F18/#349) made loop.py's _budget_spent() treat an absent/zero max_iterations as
    unlimited, matching max_minutes/max_tokens -- but this row still defaulted iterations to 20,
    printing a cap that's no longer enforced. It must read "off" here exactly like its minutes/tokens
    siblings do, and absent must match explicit-zero byte for byte (same convention those two use)."""
    d = _doc()
    absent = _sdlc(tmp_path / "absent", {})
    zero = _sdlc(tmp_path / "zero", {"budget": {"max_iterations": 0}})
    rows_absent = {name: state for name, state, _ in d.features(absent)}
    rows_zero = {name: state for name, state, _ in d.features(zero)}
    assert rows_absent["budgets"] == "iterations=off minutes=off tokens=off codex_raw=off"
    assert rows_zero["budgets"] == rows_absent["budgets"]


def test_features_budgets_row_reports_a_real_max_iterations_value(tmp_path):
    """Regression guard alongside the off-case above: a genuine positive cap must still print as-is."""
    d = _doc()
    base = _sdlc(tmp_path, {"budget": {"max_iterations": 5}})
    rows = {name: state for name, state, _ in d.features(base)}
    assert rows["budgets"] == "iterations=5 minutes=off tokens=off codex_raw=off"


# --------------------------------------------------------------------- hand-off (#2521) ----------


def test_features_handoff_row_reports_the_default_when_the_key_is_absent(tmp_path):
    """Mirrors test_features_budgets_row_reports_off_for_absent_or_zero_max_iterations above, for
    the OPPOSITE default: absent means ON at 20, not off -- see loop.py's _handoff_reason."""
    d = _doc()
    absent = _sdlc(tmp_path / "absent", {})
    rows = {name: state for name, state, _ in d.features(absent)}
    assert rows["hand-off (context bound, resets the session after N goals)"] == "after_goals=20"


def test_features_handoff_row_reports_off_when_explicitly_disabled(tmp_path):
    d = _doc()
    off = _sdlc(tmp_path / "off", {"handoff": {"enabled": False}})
    rows = {name: state for name, state, _ in d.features(off)}
    assert rows["hand-off (context bound, resets the session after N goals)"] == "off"


def test_features_handoff_row_reports_a_configured_value(tmp_path):
    d = _doc()
    base = _sdlc(tmp_path, {"handoff": {"after_goals": 5}})
    rows = {name: state for name, state, _ in d.features(base)}
    assert rows["hand-off (context bound, resets the session after N goals)"] == "after_goals=5"


def test_doctor_handoff_default_matches_loop_pys_own_constant():
    """Plan-review round 1, finding 5 (nit): doctor.py cannot `import loop` (architecture rule 3 —
    skills do not import each other's Python), so `_handoff_row_state`'s own fallback default is a
    SECOND literal, hand-kept equal to loop.py's `DEFAULT_HANDOFF_AFTER_GOALS`. This test is the
    sync mechanism instead of an import — it reads loop.py's real constant by loading that module
    directly from its file path (not `import loop`, which would violate the same architecture rule
    inside the TEST — a subtlety worth stating, not just avoiding) and fails loudly if the two
    numbers ever diverge, which Top Risk 2 already predicts will happen once a real multi-goal run
    argues for retuning `after_goals`."""
    import re
    loop_path = D.parent.parent.parent / "sigma-loop" / "scripts" / "loop.py"   # same D.parent chain
    spec = importlib.util.spec_from_file_location("loop_for_sync_check", loop_path)          # GH_SESSION uses above
    loop_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loop_mod)
    doctor_src = D.read_text()
    m = re.search(r'block\.get\("after_goals",\s*(\d+)\)', doctor_src)
    assert m, "doctor.py's _handoff_row_state no longer has the expected fallback literal shape"
    assert int(m.group(1)) == loop_mod.DEFAULT_HANDOFF_AFTER_GOALS, (
        f"doctor.py's hardcoded handoff-default fallback ({m.group(1)}) has drifted from loop.py's "
        f"own DEFAULT_HANDOFF_AFTER_GOALS ({loop_mod.DEFAULT_HANDOFF_AFTER_GOALS}) — update "
        f"doctor.py's literal to match; architecture rule 3 forbids importing loop.py directly there")


def test_features_auto_unpark_is_ON_by_default():
    """#1394 inverted this. A goal waiting on machine-resolvable work should resume when that work
    lands -- that is the loop doing its job, not an extra feature -- so opting OUT is the deliberate
    gesture now. Safe to default because the sweep no longer touches a human's park."""
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"discovery": {"source": "github"}})
        rows = {name: state for name, state, _ in d.features(base)}
        assert rows["auto-unpark sweep (#1129)"].startswith("ON (default)")


def test_features_auto_unpark_reports_off_when_explicitly_disabled():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"discovery": {"source": "github", "auto_unpark": {"mode": "off"}}})
        rows = {name: state for name, state, _ in d.features(base)}
        assert rows["auto-unpark sweep (#1129)"].startswith("off")


def test_the_doctor_row_and_the_code_agree_on_the_auto_unpark_default():
    """These are two independent copies of the same rule -- doctor reads raw config rather than
    cross-loading sources.py. Left to drift, doctor reports a feature as off while the loop runs it,
    which is the config/reality drift this branch spent 17 fixes removing."""
    d, src = _doc(), _sources()
    with tempfile.TemporaryDirectory() as t:
        for cfg, expect_on in (({}, True), ({"mode": "off"}, False), ({"mode": "on"}, True),
                               ({"mode": 1}, True)):
            conf = {"discovery": {"source": "github", "auto_unpark": cfg}}
            base = _sdlc(t + "/x%s" % abs(hash(str(cfg))), conf)
            row = {n: s for n, s, _ in d.features(base)}["auto-unpark sweep (#1129)"]
            assert row.startswith("ON") is expect_on, (cfg, row)
            assert (src._auto_unpark(conf) == "on") is expect_on, cfg


def test_features_auto_unpark_reports_on():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"discovery": {"source": "github", "auto_unpark": {"mode": "on"}}})
        rows = {name: state for name, state, _ in d.features(base)}
        state = rows["auto-unpark sweep (#1129)"]
        assert "ON" in state
        # #1351 review finding (MINOR): the ON message must name BOTH mutation classes this toggle
        # now arms, not just the original #1129 unpark behavior -- a maintainer reading this after
        # #1351 shipped needs to know sdlc:blocking is also live, not just the resume behavior.
        assert "auto-resume" in state
        assert "sdlc:blocking" in state


def test_features_auto_unpark_not_applicable_for_local_goals():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"discovery": {"auto_unpark": {"mode": "on"}}})   # source unset -> local-goals
        rows = {name: state for name, state, _ in d.features(base)}
        assert rows["auto-unpark sweep (#1129)"] == "n/a — backlog source is not github"


def test_features_auto_unpark_malformed_mode_reads_as_off():
    # #1351 review finding (MINOR): malforms just the `mode` FIELD inside an otherwise-valid
    # `auto_unpark` dict -- matching this suite's own established convention for this shape
    # (test_malformed_nested_gates_block_does_not_crash et al: nested field malformed, parent still
    # a dict). The name previously promised this but the body malformed the whole `auto_unpark`
    # block instead (see the dedicated test below for that case), never actually reaching a code
    # path where `mode` itself holds a bad value inside a valid dict.
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"discovery": {"source": "github", "auto_unpark": {"mode": 1}}})
        rows = {name: state for name, state, _ in d.features(base)}
        # #1394: a malformed value lands on the DEFAULT (now on), the mirror of the old rule --
        # a config mistake still never changes behaviour silently, it just leaves you the default
        assert rows["auto-unpark sweep (#1129)"].startswith("ON (default)")


def test_features_auto_unpark_malformed_block_reads_as_off():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"discovery": {"source": "github", "auto_unpark": "not-a-dict"}})
        rows = {name: state for name, state, _ in d.features(base)}
        # #1394: a malformed value lands on the DEFAULT (now on), the mirror of the old rule --
        # a config mistake still never changes behaviour silently, it just leaves you the default
        assert rows["auto-unpark sweep (#1129)"].startswith("ON (default)")


def test_features_blocking_priority_override_is_ON_by_default():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"discovery": {"source": "github"}})
        rows = {name: state for name, state, _ in d.features(base)}
        # #1394: a non-bool lands on the DEFAULT (now on) -- the mirror of the old rule. A config
        # mistake still never changes behaviour silently; it leaves you the default either way.
        assert rows["blocking-priority picker override (#1352)"].startswith("ON (default)")


def test_features_blocking_priority_override_reports_on():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"discovery": {"source": "github", "blocking_priority_override": True}})
        rows = {name: state for name, state, _ in d.features(base)}
        assert "ON" in rows["blocking-priority picker override (#1352)"]


def test_features_blocking_priority_override_not_applicable_for_local_goals():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"discovery": {"blocking_priority_override": True}})   # source unset -> local-goals
        rows = {name: state for name, state, _ in d.features(base)}
        assert rows["blocking-priority picker override (#1352)"] == "n/a — backlog source is not github"


def test_features_blocking_priority_override_non_bool_reads_as_off():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"discovery": {"source": "github", "blocking_priority_override": "true"}})
        rows = {name: state for name, state, _ in d.features(base)}
        # #1394: a non-bool lands on the DEFAULT (now on) -- the mirror of the old rule. A config
        # mistake still never changes behaviour silently; it leaves you the default either way.
        assert rows["blocking-priority picker override (#1352)"].startswith("ON (default)")


def test_features_reports_the_ledger_and_counts_its_entries(tmp_path):
    """#1599 moved this fixture onto a REAL ledger worktree with a REAL origin, and it had to. The
    fixture used to fake `.sdlc/ledger/.git` with a text file, which is the exact shape a check that
    ACTUALLY LOOKS at what has been published cannot read -- so the old assertion was pinning the
    healthy message against a ledger nothing could verify was healthy. That is the same blind spot
    one level up: a green row that means "we did not look"."""
    _repo, base = _ledger_clone(tmp_path)
    _sync().bootstrap(str(base), _LEDGER_ON)
    _note(base)
    _publish(base)
    rows = {name: state for name, state, _ in d_features(base)}
    assert rows["team ledger"] == "ON — 1 entry in .sdlc/ledger/entries/", rows["team ledger"]


# --- #1599: a bootstrapped ledger is still not a DELIVERING one ------------------------------


def _sync():
    import importlib.util, pathlib as _pl
    path = (_pl.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"
            / "sync.py")
    spec = importlib.util.spec_from_file_location("sync", path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def _git(cwd, *args):
    import subprocess
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert proc.returncode == 0, f"git {' '.join(args)}: {proc.stderr or proc.stdout}"
    return proc.stdout.strip()


def _ledger_clone(tmp_path, cfg=None):
    """A real repo, a real bare origin, a real `.sdlc`. The row under test reports what GIT says
    about what has been published, so a stub would only ever agree with itself."""
    import json, subprocess
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    repo = tmp_path / "repo"; repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "remote", "add", "origin", str(origin))
    (repo / ".gitignore").write_text(".sdlc/\n")
    _git(repo, "add", "--", ".gitignore")
    _git(repo, "commit", "-q", "-m", "base")
    base = repo / ".sdlc"; (base / "state").mkdir(parents=True)
    (base / "config.json").write_text(json.dumps(cfg or {"ledger": {"enabled": True, "actor": "amy"}}))
    return repo, base


_LEDGER_ON = {"ledger": {"enabled": True, "actor": "amy"}}


def _ledger_mod():
    import importlib.util, pathlib as _pl
    path = (_pl.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"
            / "ledger.py")
    spec = importlib.util.spec_from_file_location("ledger", path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def _note(base):
    """Written through the REAL writer, never by hand: `publish()` stages the files
    `ledger.files_for()` names, so a hand-invented filename is one `publish()` will not carry --
    which would make these fixtures test a shape the product never produces."""
    assert _ledger_mod().append(str(base), _LEDGER_ON, "note", "g.md", why="a note")


def _publish(base):
    assert _sync().publish(str(base), _LEDGER_ON) == "published"


def d_features(base):
    """Through the REAL `features()`, so the row is proved WIRED IN, not merely correct in
    isolation."""
    return _doc().features(str(base))


def _ledger_row(base, now=None):
    """Straight at `_ledger_feature_state`, the only seam that takes `now` -- matching how
    every other age-based row's cases are tested. `features()` deliberately grows no clock
    parameter for one row."""
    d = _doc()
    return d._ledger_feature_state(pathlib.Path(base), d._cfg(str(base)), now=now)


def test_features_ledger_reports_writes_that_never_left_the_machine(tmp_path):
    """THE #1599 DEFECT. Once `.sdlc/ledger/.git` existed this row printed `ON — N entries` no
    matter how much of that N had never reached origin. Measured in the field: 174 entry files /
    189 entries, 67% of one machine's history, held back for two and a half weeks with this row
    green the whole time."""
    _repo, base = _ledger_clone(tmp_path)
    _sync().bootstrap(str(base), _LEDGER_ON)
    _note(base)
    row = {n: s for n, s, _ in d_features(base)}["team ledger"]
    assert "BEHIND" in row and "1 entry file" in row, row
    assert "no other machine can see" in row, row


def test_features_ledger_names_a_dead_watcher_as_the_reason_nothing_publishes(tmp_path):
    """A backlog is a symptom; a watcher that stopped ticking is the cause, and #1227 already proved
    only the heartbeat's AGE can tell a dead watcher from a live one. Named HERE, where something is
    actually waiting -- a watcher that is not running is harmless when nothing is."""
    _repo, base = _ledger_clone(tmp_path)
    _sync().bootstrap(str(base), _LEDGER_ON)
    _note(base)
    hb = base / "state" / "watch.heartbeat"; hb.write_text("")
    import os
    os.utime(hb, (1000.0, 1000.0))
    row = _ledger_row(base, now=1000.0 + 86400)
    assert "it is not running" in row, row
    assert "24.0h ago" in row, row


def test_features_ledger_credits_a_live_watcher_instead_of_blaming_it(tmp_path):
    """The other half, and the one that keeps this row honest rather than alarmist: a fresh
    heartbeat means the backlog is about to move, so say so."""
    _repo, base = _ledger_clone(tmp_path)
    _sync().bootstrap(str(base), _LEDGER_ON)
    _note(base)
    (base / "state" / "watch.heartbeat").write_text("")
    row = {n: s for n, s, _ in d_features(base)}["team ledger"]
    assert "BEHIND" in row and "watcher is live" in row, row


def test_features_ledger_counts_a_commit_that_never_reached_the_remote(tmp_path):
    """`publish()` commits before it pushes, so a push that failed leaves a clean working tree and a
    local commit. A check that only looked at dirty files would call that clone fully published."""
    _repo, base = _ledger_clone(tmp_path)
    _sync().bootstrap(str(base), _LEDGER_ON)
    _note(base)
    wt = base / "ledger"
    _git(wt, "add", "-A"); _git(wt, "commit", "-q", "-m", "ledger: local only")
    row = {n: s for n, s, _ in d_features(base)}["team ledger"]
    assert "1 commit never pushed" in row, row


def test_features_ledger_says_it_could_not_look_rather_than_reporting_health(tmp_path):
    """The rule `_secret_file_coverage` set: "we did not look" must never be reported as "we looked
    and found nothing". A features row cannot omit ITSELF -- the ledger's state still has to print --
    so what is omitted is the CLAIM, replaced by an explicit refusal to make one."""
    import json
    base = tmp_path / ".sdlc"; (base / "state").mkdir(parents=True)
    (base / "config.json").write_text(json.dumps({"ledger": {"enabled": True}}))
    (base / "ledger" / "entries").mkdir(parents=True)
    (base / "ledger" / "entries" / "amy.jsonl").write_text('{"kind":"done"}\n')
    (base / "ledger" / ".git").write_text("gitdir: nowhere\n")     # looks like a worktree, is not
    row = {n: s for n, s, _ in d_features(base)}["team ledger"]
    assert "COULD NOT CHECK" in row and "not an all-clear" in row, row
    assert "BEHIND" not in row, row


def test_features_ledger_refuses_to_report_health_when_sync_itself_will_not_load(tmp_path):
    """The other way this check can go blind, and it must fail the same direction. A cross-load that
    raises is "we could not look", exactly like a git that will not answer -- `_secret_file_coverage`
    treats its own `_load_loop_script` failure the same way."""
    _repo, base = _ledger_clone(tmp_path)
    _sync().bootstrap(str(base), _LEDGER_ON)
    d = _doc()

    def boom(name):
        raise RuntimeError("simulated cross-load failure")

    d._load_loop_script = boom
    row = d._ledger_feature_state(pathlib.Path(base), d._cfg(str(base)))
    assert "COULD NOT CHECK" in row and "not an all-clear" in row, row


def test_features_ledger_flags_a_clone_that_has_never_seen_the_remote_branch(tmp_path):
    """Nothing dirty and nothing ahead, but no `origin/sdlc-ledger` to compare against either --
    which is what a clone that has never pushed looks like. Reading that as zero would make the
    never-published case the healthiest-looking row on the dashboard."""
    _repo, base = _ledger_clone(tmp_path)
    _sync().init(str(base), _LEDGER_ON)                                      # init only, no push
    row = {n: s for n, s, _ in d_features(base)}["team ledger"]
    assert "COULD NOT COMPARE" in row and "origin/sdlc-ledger" in row, row


def test_features_ledger_stays_quiet_when_everything_really_is_published(tmp_path):
    """The control that stops this becoming a permanently-red row nobody reads."""
    _repo, base = _ledger_clone(tmp_path)
    _sync().bootstrap(str(base), _LEDGER_ON)
    _note(base)
    _publish(base)
    row = {n: s for n, s, _ in d_features(base)}["team ledger"]
    assert row == "ON — 1 entry in .sdlc/ledger/entries/", row


def test_features_ledger_with_entries_but_no_worktree_reports_local_only(tmp_path):
    """#1391 step 7: entry COUNT was masking the thing that matters. A ledger that has never been
    published shares nothing -- and it is the only cross-machine claim arbiter, so unpublished means
    there is no cross-machine coordination at all. Measured live on this repo: 263 entries, none
    ever on the ops branch, reported as a healthy 'ON — 263 entries' the whole time."""
    import json, importlib.util, pathlib as _pl
    spec = importlib.util.spec_from_file_location(
        "doctor", _pl.Path(__file__).resolve().parent.parent / "skills" / "sigma-doctor" / "scripts" / "doctor.py")
    d = importlib.util.module_from_spec(spec); spec.loader.exec_module(d)
    base = tmp_path / ".sdlc"; base.mkdir()
    base.joinpath("config.json").write_text(json.dumps({"ledger": {"enabled": True}}))
    entries = base / "ledger" / "entries"; entries.mkdir(parents=True)
    (entries / "amy.jsonl").write_text('{"kind":"done"}\n')          # entries, but no .git worktree
    rows = {name: state for name, state, _ in d.features(str(base))}
    assert "LOCAL-ONLY" in rows["team ledger"]
    assert "1 entry" in rows["team ledger"]


# --- the journal row: config block + doctor reporting (#138, rewritten by #2574/S1-G3) --------
# PRD §6.1 gives this row four readings, and they answer a question "on/off" cannot: WHO turned the
# journal on, and how fresh that claim is. An org lock that has gone stale reads as OFF, and the
# row has to say so -- a silent off is the five-day freeze of 2026-08-22 all over again, where
# there were zero errors the whole time and age was the only tell.

_ROW = "journal (local event records)"

def _managed(base, locked, refreshed_at, status="ok"):
    """A managed-settings file, written the way `managed_settings.read()` parses it. Authored here
    rather than through a helper so the LEASE FIELD is visible in the test that depends on it: the
    switch keys on `refreshed_at`, never on this file's mtime, and a test whose staleness came from
    `os.utime` would pass against an mtime implementation too."""
    payload = {"version": 1, "status": status}
    if locked is not None:
        payload["locked"] = locked
    if refreshed_at is not None:
        payload["refreshed_at"] = refreshed_at
    (pathlib.Path(base) / "managed-settings.json").write_text(json.dumps(payload))


def _lease_iso(seconds_ago):
    """An ISO-8601 `refreshed_at` this many seconds in the past.

    NOT named `_iso`: this module already defines an `_iso(epoch_seconds)` further down, and a
    second `def _iso` here would be silently overwritten by it at import time -- `_lease_iso(3 * 3600)`
    would then mean "three hours after the epoch", and every lease in this section would read as
    20,709 days stale. Caught by running these tests; nothing about the name would have shown it."""
    import datetime
    when = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds_ago)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_features_journal_absent_block_is_off():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {})
        rows = {name: state for name, state, _ in d.features(base)}
        assert rows[_ROW].startswith("off")


def test_features_journal_enabled_false_matches_absent_byte_for_byte():
    """The headline behavior from the goal statement, carried over from the old journal row: an
    absent block and an explicit enabled:false must read identically — not just both 'look off',
    the literal same string."""
    d = _doc()
    with tempfile.TemporaryDirectory() as t1, tempfile.TemporaryDirectory() as t2:
        absent = _sdlc(t1, {})
        explicit_off = _sdlc(t2, {"journal": {"enabled": False}})
        rows_absent = {name: state for name, state, _ in d.features(absent)}
        rows_off = {name: state for name, state, _ in d.features(explicit_off)}
        assert rows_absent[_ROW] == rows_off[_ROW]
        assert rows_absent[_ROW].startswith("off")


def test_features_journal_enabled_is_strict_true_not_truthy():
    """Guards the same is-True idiom as ledger.enabled() — a truthy string or int must not
    silently switch a write surface on."""
    d = _doc()
    for bad in ("yes", 1, "true"):
        with tempfile.TemporaryDirectory() as t:
            base = _sdlc(t, {"journal": {"enabled": bad}})
            rows = {name: state for name, state, _ in d.features(base)}
            assert rows[_ROW].startswith("off"), f"enabled={bad!r} must not turn the journal on"


def test_features_journal_on_by_config_names_the_config_and_the_path():
    """PRD §6.1 state 2, 'on by your config'. The path is named because it is the one thing an
    adopter has to know to find (or gitignore, or delete) what this writes."""
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"journal": {"enabled": True}})
        rows = {name: state for name, state, _ in d.features(base)}
        assert rows[_ROW].startswith("ON")
        assert "your config" in rows[_ROW]
        assert ".sdlc/events/" in rows[_ROW]
        assert "ledger/events" not in rows[_ROW]       # nothing publishes any more


def test_features_journal_on_by_config_names_the_journal_key():
    """The row names the key that switched it on: `journal.enabled`, the only key read (#2706)."""
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"journal": {"enabled": True}})
        rows = {name: state for name, state, _ in d.features(base)}
        assert "on by your config (`journal.enabled`)" in rows[_ROW]


def test_features_journal_row_flags_a_legacy_block_without_changing_the_verdict():
    """#2738: a config still carrying the pre-rename block gets told the value is dead; the
    verdict is untouched (no alias, D24) and a clean config gets no note."""
    d = _doc()
    legacy = d._LEGACY_JOURNAL_BLOCK
    note = "legacy `%s` block present; the journal reads `journal.enabled` only" % legacy
    with tempfile.TemporaryDirectory() as t:
        rows = {n: s for n, s, _ in d.features(_sdlc(t, {legacy: {"enabled": True}}))}
        assert rows[_ROW].startswith("off"), rows[_ROW]      # not aliased: still off
        assert note in rows[_ROW], rows[_ROW]
    for clean in ({"journal": {"enabled": True}}, {}):
        with tempfile.TemporaryDirectory() as t:
            rows = {n: s for n, s, _ in d.features(_sdlc(t, clean))}
            assert "legacy `" not in rows[_ROW], rows[_ROW]


def test_features_journal_on_by_managed_settings_reports_the_lease_age():
    """PRD §6.1 state 3. The config says NOTHING here — the org lock is the whole reason it is on,
    and the row names the hours since the lease was refreshed so a reader can tell a live
    policy writer from one that stopped this morning."""
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {})                                    # no journal key at all
        _managed(base, {"journal.enabled": True}, _lease_iso(3 * 3600))
        rows = {name: state for name, state, _ in d.features(base)}
        assert rows[_ROW].startswith("ON")
        assert "managed settings" in rows[_ROW]
        assert "3 hours ago" in rows[_ROW] or "3h ago" in rows[_ROW], rows[_ROW]


def test_features_journal_off_because_the_managed_settings_are_stale():
    """PRD §6.1 state 4, and the state R-8 accepted the ceiling for: a lease older than 7 days
    turns the journal OFF terminally, and doctor is the thing that stops that being silent. The
    config says `true` here — under fall-through semantics this row would read ON, which is
    exactly the reading §6.2's Ceiling sentence rules out."""
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"journal": {"enabled": True}})
        _managed(base, {"journal.enabled": True}, _lease_iso(8 * 86400))
        rows = {name: state for name, state, _ in d.features(base)}
        assert rows[_ROW].startswith("off")
        assert "managed settings" in rows[_ROW]
        assert "8 days old" in rows[_ROW] or "8d" in rows[_ROW], rows[_ROW]


def test_features_journal_off_because_a_locked_false_beats_a_config_true():
    """"An organisation that forbids the journal gets none" (PRD §6.2). The row must not read as
    the adopter's own choice — they set `true` and are being overruled."""
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"journal": {"enabled": True}})
        _managed(base, {"journal.enabled": False}, _lease_iso(3600))
        rows = {name: state for name, state, _ in d.features(base)}
        assert rows[_ROW].startswith("off")
        assert "managed settings" in rows[_ROW]


def test_features_journal_off_when_the_managed_settings_refuse():
    """`access-revoked` / `locked-key-unverifiable`. Not one of §6.1's four readings, and
    deliberately given its own: calling a revoked lock "off by your config" would be false, and
    "off because the settings are N days old" would name an age nobody measured."""
    d = _doc()
    for status in ("access-revoked", "locked-key-unverifiable"):
        with tempfile.TemporaryDirectory() as t:
            base = _sdlc(t, {"journal": {"enabled": True}})
            _managed(base, None, _lease_iso(3600), status=status)
            rows = {name: state for name, state, _ in d.features(base)}
            assert rows[_ROW].startswith("off"), status
            assert "managed settings" in rows[_ROW], status


def test_features_journal_counts_both_destinations():
    """One release of legacy history lives in `.sdlc/ledger/events/` and is never moved. A row that
    counted only the new directory would report a drop that never happened."""
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"journal": {"enabled": True}})
        local = pathlib.Path(base) / "events"; local.mkdir(parents=True)
        (local / "dana.jsonl").write_text('{"kind":"phase"}\n{"kind":"gate"}\n{"kind":"verify"}\n')
        legacy = pathlib.Path(base) / "ledger" / "events"; legacy.mkdir(parents=True)
        (legacy / "stale.jsonl").write_text('{"kind":"phase"}\n{"kind":"gate"}\n')
        rows = {name: state for name, state, _ in d.features(base)}
        assert "3 events" in rows[_ROW]          # the journal itself
        assert "2" in rows[_ROW] and "ledger/events" in rows[_ROW]   # the legacy history, named


def test_features_journal_no_events_dir_at_all_reports_zero():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"journal": {"enabled": True}})
        rows = {name: state for name, state, _ in d.features(base)}
        assert "0 events" in rows[_ROW]


def test_features_journal_empty_events_dir_reports_zero():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"journal": {"enabled": True}})
        (pathlib.Path(base) / "events").mkdir(parents=True)
        rows = {name: state for name, state, _ in d.features(base)}
        assert "0 events" in rows[_ROW]


def test_features_journal_malformed_block_does_not_crash():
    """Fail-open against a non-dict `journal` value — the one shape a bare `or {}` idiom would not
    catch (a non-empty string/list is truthy)."""
    d = _doc()
    for bad in ("oops", ["a", "b"], None, True):
        with tempfile.TemporaryDirectory() as t:
            base = _sdlc(t, {"journal": bad})
            rows = {name: state for name, state, _ in d.features(base)}
            assert rows[_ROW].startswith("off"), f"journal={bad!r} must fail open, not crash"


def test_features_journal_malformed_jsonl_line_does_not_crash():
    """A garbage line still counts as a non-blank line (doctor only counts lines, it never
    parses JSON — same convention as _ledger_entries) and must not raise."""
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"journal": {"enabled": True}})
        events = pathlib.Path(base) / "events"; events.mkdir(parents=True)
        (events / "dana.jsonl").write_text('not valid json at all\n{"kind":"phase"}\n')
        rows = {name: state for name, state, _ in d.features(base)}
        assert "2 events" in rows[_ROW]


def test_features_survives_a_half_written_non_utf8_line_in_either_stream():
    """A process killed mid-append truncates a multi-byte UTF-8 sequence. The resulting
    UnicodeDecodeError is a ValueError, NOT an OSError, so it used to sail past the counter's
    catch and crash the WHOLE dashboard — every row, not just this one. Both counters share one
    implementation, so neither can regress alone."""
    d = _doc()
    for rel, row in (("ledger/entries", "team ledger"), ("events", _ROW)):
        with tempfile.TemporaryDirectory() as t:
            base = _sdlc(t, {"journal": {"enabled": True},
                             "ledger": {"enabled": True, "actor": "dana"}})
            files = pathlib.Path(base) / rel
            files.mkdir(parents=True)
            (files / "dana.jsonl").write_bytes(b'{"kind":"note"}\n\xff\xfe truncated mid-sequence\n')
            rows = {name: state for name, state, _ in d.features(base)}   # must not raise
            assert "2" in rows[row]


# --- ledger setup: enabled-but-not-created is a real gap doctor can fix -----------------------

def test_flags_ledger_enabled_but_not_initialised():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"ledger": {"enabled": True}})               # on in config, never created
        c = _by_name(d.check(base, run=_runner()))
        assert c["team ledger initialized"]["ok"] is False
        assert "/sigma-ledger" in c["team ledger initialized"]["fix"]


def test_ledger_check_passes_once_the_worktree_exists():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"ledger": {"enabled": True}})
        (pathlib.Path(base) / "ledger").mkdir()
        (pathlib.Path(base) / "ledger" / ".git").write_text("gitdir: elsewhere\n")   # worktree present
        c = _by_name(d.check(base, run=_runner()))
        assert c["team ledger initialized"]["ok"] is True


def test_no_ledger_check_when_the_ledger_is_off():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"ledger": {"enabled": False}})
        assert "team ledger initialized" not in _by_name(d.check(base, run=_runner()))


# --- #2698: knowledge notes sync, the same row one channel over --------------------------------

_KG_SYNC_ON = {"knowledge_graph": {"enabled": True, "sync": {"enabled": True}}}


def test_knowledge_sync_check_fails_naming_the_bootstrap_command_when_no_worktree():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, _KG_SYNC_ON)
        c = _by_name(d.check(base, run=_runner()))
        assert c["knowledge notes sync initialized"]["ok"] is False
        assert "--channel knowledge" in c["knowledge notes sync initialized"]["fix"]


def test_knowledge_sync_check_passes_once_the_worktree_exists():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, _KG_SYNC_ON)
        (pathlib.Path(base) / "knowledge" / "analysis").mkdir(parents=True)
        (pathlib.Path(base) / "knowledge" / "analysis" / ".git").write_text("gitdir: elsewhere\n")
        c = _by_name(d.check(base, run=_runner()))
        assert c["knowledge notes sync initialized"]["ok"] is True


@pytest.mark.parametrize("kg", [
    {"enabled": True},                                        # sync absent
    {"enabled": True, "sync": {"enabled": False}},
    {"enabled": True, "sync": {"enabled": "true"}},           # strict: a quoted true is off
    {"enabled": False, "sync": {"enabled": True}},            # graph off means sync off
    {"enabled": True, "sync": "yes"},                         # malformed block reads as off
], ids=["absent", "false", "quoted-true", "graph-off", "malformed"])
def test_no_knowledge_sync_check_unless_both_flags_are_strictly_true(kg):
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"knowledge_graph": kg})
        assert "knowledge notes sync initialized" not in _by_name(d.check(base, run=_runner()))


def test_features_flags_an_enabled_but_unset_up_ledger(tmp_path):
    d = _doc()
    base = _sdlc(tmp_path, {"ledger": {"enabled": True}})            # enabled, nothing created yet
    rows = {name: state for name, state, _ in d.features(base)}
    assert "NOT set up" in rows["team ledger"] and "/sigma-ledger" in rows["team ledger"]


# --- the verify permanent-refusal trap: enforce on with no command ---------------------------

def test_flags_verify_enforce_with_empty_command():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"verify": {"enforce": True, "command": ""}})
        c = _by_name(d.check(base, run=_runner()))["verify command present (enforce is on)"]
        assert c["ok"] is False and "every `done` is refused" in c["fix"]


def test_verify_check_ok_with_a_command():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"verify": {"enforce": True, "command": "pytest -q"}})
        assert _by_name(d.check(base, run=_runner()))["verify command present (enforce is on)"]["ok"]


# --- #615: a configured verify command whose checkout never granted the git-local trust -----------

_TRUST_ROW = "verify command trusted in this checkout"
_TRUST_KEY = "sigma.allowRepositoryShellCommands"


def _trust_row(d, base):
    row = _by_name(d.check(base, run=_runner())).get(_TRUST_ROW)
    assert row is not None, "no %r row" % _TRUST_ROW      # an assertion, so a missing row is a clean red
    return row


def _git_project(t, verify):
    root = pathlib.Path(t)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return _sdlc(t, {"verify": verify})


def test_flags_a_configured_verify_command_without_local_trust():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _git_project(t, {"enforce": True, "command": "pytest -q"})
        c = _trust_row(d, base)
        assert c["ok"] is False
        assert "config --local %s true" % _TRUST_KEY in c["fix"] and "loop.py verify" in c["fix"]


def test_trust_row_is_green_once_the_checkout_granted_trust():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _git_project(t, {"enforce": True, "command": "pytest -q"})
        subprocess.run(["git", "-C", t, "config", "--local", _TRUST_KEY, "true"], check=True)
        assert _trust_row(d, base)["ok"] is True


def test_trust_row_also_covers_a_configured_command_with_enforce_off():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _git_project(t, {"command": "pytest -q"})
        assert _trust_row(d, base)["ok"] is False


def test_no_trust_row_without_a_configured_command():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _git_project(t, {"enforce": False, "command": ""})
        assert _TRUST_ROW not in _by_name(d.check(base, run=_runner()))


def test_trust_row_treats_a_whitespace_only_command_as_configured_like_init():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _git_project(t, {"command": "   "})
        assert _trust_row(d, base)["ok"] is False


def test_trust_row_outside_a_git_worktree_is_red_and_says_so():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"verify": {"command": "pytest -q"}})
        c = _trust_row(d, base)
        assert c["ok"] is False and "Git worktree" in c["fix"]


def test_doctor_trust_read_agrees_with_the_loops_own_policy():
    """doctor duplicates the read (standalone script); this keeps it in lockstep with shell_policy."""
    d = _doc()
    spec = importlib.util.spec_from_file_location(
        "shell_policy", D.parent.parent.parent / "sigma-loop" / "scripts" / "shell_policy.py")
    sp = importlib.util.module_from_spec(spec); spec.loader.exec_module(sp)
    for value in (None, "true", "false", "yes", "1", "maybe", ""):
        with tempfile.TemporaryDirectory() as t:
            subprocess.run(["git", "init", "-q", t], check=True)
            if value is not None:
                subprocess.run(["git", "-C", t, "config", "--local", _TRUST_KEY, value], check=True)
            assert d._verify_trusted(pathlib.Path(t)) == sp.repository_shell_commands_allowed(t), value
    with tempfile.TemporaryDirectory() as t:                      # not a repository
        assert d._verify_trusted(pathlib.Path(t)) == sp.repository_shell_commands_allowed(t) is False


# --- ledger.autowatch: dashboard row + enabled-but-unwired warning (#1321) --------------------

def test_features_autowatch_absent_block_is_off():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {})
        rows = {name: state for name, state, _ in d.features(base)}
        assert rows["ledger autowatch"] == "off (no ledger-triggered unattended runs)"


def test_features_autowatch_enabled_false_matches_absent_byte_for_byte():
    d = _doc()
    with tempfile.TemporaryDirectory() as t1, tempfile.TemporaryDirectory() as t2:
        absent = _sdlc(t1, {})
        explicit_off = _sdlc(t2, {"ledger": {"autowatch": {"enabled": False}}})
        rows_absent = {name: state for name, state, _ in d.features(absent)}
        rows_off = {name: state for name, state, _ in d.features(explicit_off)}
        assert rows_absent["ledger autowatch"] == rows_off["ledger autowatch"] == \
            "off (no ledger-triggered unattended runs)"


def test_features_autowatch_enabled_reports_scope_and_hop_limit_and_unwired():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"ledger": {"autowatch": {
            "enabled": True, "scope": ["mentions", "blockers"], "hop_limit": 2}}})
        empty_tasks = pathlib.Path(t) / "no-scheduled-tasks"
        rows = {name: state for name, state, _ in d.features(base, scheduled_tasks_dir=str(empty_tasks))}
        state = rows["ledger autowatch"]
        assert "ON" in state and "mentions,blockers" in state and "hop_limit=2" in state
        assert "NOT yet wired" in state


def test_features_autowatch_default_scope_and_hop_limit_when_absent():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"ledger": {"autowatch": {"enabled": True}}})
        rows = {name: state for name, state, _ in
                d.features(base, scheduled_tasks_dir=str(pathlib.Path(t) / "none"))}
        state = rows["ledger autowatch"]
        assert "mentions,assignments,blockers" in state and "hop_limit=1" in state


def test_features_autowatch_reports_wired_via_channel_webhook_url():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"ledger": {"autowatch": {
            "enabled": True, "channel_webhook_url": "http://127.0.0.1:8788"}}})
        rows = {name: state for name, state, _ in
                d.features(base, scheduled_tasks_dir=str(pathlib.Path(t) / "none"))}
        assert "NOT yet wired" not in rows["ledger autowatch"]


def test_features_autowatch_reports_wired_via_desktop_scheduled_task():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"ledger": {"autowatch": {"enabled": True}}})
        tasks = pathlib.Path(t) / "scheduled-tasks" / "sigma-autowatch-myrepo"
        tasks.mkdir(parents=True)
        (tasks / "SKILL.md").write_text(
            "---\nname: sigma-autowatch-myrepo\n---\nrun autowatch.py tick .sdlc\n")
        rows = {name: state for name, state, _ in
                d.features(base, scheduled_tasks_dir=str(pathlib.Path(t) / "scheduled-tasks"))}
        assert "NOT yet wired" not in rows["ledger autowatch"]


def test_features_autowatch_scope_with_non_string_element_does_not_crash():
    """A hand-edited config.json with a malformed scope entry (e.g. a stray number) must degrade
    to a readable row, not crash features() -- and since main()'s `check` subcommand also calls
    features() to print the dashboard, a crash here would take down the whole diagnostic tool."""
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"ledger": {"autowatch": {"enabled": True, "scope": ["mentions", 5, None]}}})
        rows = {name: state for name, state, _ in
                d.features(base, scheduled_tasks_dir=str(pathlib.Path(t) / "none"))}
        assert "mentions,5,None" in rows["ledger autowatch"]


def test_check_omits_autowatch_row_when_disabled():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {})
        assert "autowatch adapter wired up" not in _by_name(d.check(base, run=_runner()))


def test_check_warns_when_autowatch_enabled_but_no_adapter_wired():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"ledger": {"autowatch": {"enabled": True}}})
        c = _by_name(d.check(base, run=_runner(),
                              scheduled_tasks_dir=str(pathlib.Path(t) / "none")))["autowatch adapter wired up"]
        assert c["ok"] is False
        assert "one-time setup" in c["fix"]


def test_check_passes_when_autowatch_wired_via_channel_webhook_url():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"ledger": {"autowatch": {
            "enabled": True, "channel_webhook_url": "http://127.0.0.1:8788"}}})
        c = _by_name(d.check(base, run=_runner(),
                              scheduled_tasks_dir=str(pathlib.Path(t) / "none")))["autowatch adapter wired up"]
        assert c["ok"] is True


def test_check_passes_when_autowatch_wired_via_desktop_scheduled_task():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"ledger": {"autowatch": {"enabled": True}}})
        tasks = pathlib.Path(t) / "scheduled-tasks" / "sigma-autowatch-myrepo"
        tasks.mkdir(parents=True)
        (tasks / "SKILL.md").write_text("run autowatch.py tick .sdlc\n")
        c = _by_name(d.check(base, run=_runner(),
                              scheduled_tasks_dir=str(pathlib.Path(t) / "scheduled-tasks")))["autowatch adapter wired up"]
        assert c["ok"] is True


def test_autowatch_adapter_wired_ignores_unrelated_scheduled_tasks():
    """A SKILL.md exists but never mentions autowatch.py — some other unrelated scheduled task
    must not be misread as evidence the Desktop adapter is set up."""
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"ledger": {"autowatch": {"enabled": True}}})
        tasks = pathlib.Path(t) / "scheduled-tasks" / "daily-code-review"
        tasks.mkdir(parents=True)
        (tasks / "SKILL.md").write_text("---\nname: daily-code-review\n---\nreview today's commits\n")
        c = _by_name(d.check(base, run=_runner(),
                              scheduled_tasks_dir=str(pathlib.Path(t) / "scheduled-tasks")))["autowatch adapter wired up"]
        assert c["ok"] is False


def test_autowatch_adapter_wired_ignores_a_coincidental_mention_of_the_filename():
    """#1337 review finding: a bare 'autowatch.py' substring match false-positives on a task whose
    prose merely mentions the filename without ever actually invoking it, permanently suppressing
    this check's own warning row. The real marker is the actual invocation loop.py's own setup
    nudge asks Desktop to create ('autowatch.py tick')."""
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"ledger": {"autowatch": {"enabled": True}}})
        tasks = pathlib.Path(t) / "scheduled-tasks" / "old-experiment"
        tasks.mkdir(parents=True)
        (tasks / "SKILL.md").write_text(
            "This task predates autowatch.py and has nothing to do with it.\n")
        c = _by_name(d.check(base, run=_runner(),
                              scheduled_tasks_dir=str(pathlib.Path(t) / "scheduled-tasks")))["autowatch adapter wired up"]
        assert c["ok"] is False


def test_autowatch_adapter_wired_tolerates_missing_scheduled_tasks_dir():
    """The real ~/.claude/scheduled-tasks/ may not exist at all (nobody has ever created any
    scheduled task) — must fail open to 'not wired', never crash the whole check() run."""
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"ledger": {"autowatch": {"enabled": True}}})
        checks = d.check(base, run=_runner(),
                          scheduled_tasks_dir=str(pathlib.Path(t) / "definitely-does-not-exist"))
        assert _by_name(checks)["autowatch adapter wired up"]["ok"] is False


def test_verify_check_ok_when_a_goal_sets_verify_command():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"verify": {"enforce": True, "command": ""}})
        (pathlib.Path(base) / "goals").mkdir()
        (pathlib.Path(base) / "goals" / "0001.md").write_text(
            "---\nstatus: pending\nverify_command: pytest\n---\nx\n")
        assert _by_name(d.check(base, run=_runner()))["verify command present (enforce is on)"]["ok"]


def test_no_verify_check_when_enforce_is_off():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"verify": {"command": ""}})
        assert "verify command present (enforce is on)" not in _by_name(d.check(base, run=_runner()))


# --- F17/#342 review: doctor.py reads the SAME verify.enforce as loop.py's own done-gate --------
# Independent review of the original F17 fix found the two were built together as a matched pair
# (loop.py's `record done` gate + doctor's own "permanent-refusal trap" check) and both used the
# same fragile `is True`. Fixing only loop.py would have turned a latent inconsistency (both sides
# silently NOT enforcing a non-bool truthy value) into an actively misleading one: loop.py now
# genuinely refuses every `done` for `enforce: 1`, while doctor stayed silent and its status row
# claimed "off". These pin doctor's own generous read AND keep it in lockstep with loop.py's.

def test_flags_verify_enforce_with_empty_command_for_non_bool_truthy_values():
    d = _doc()
    for enforce_value in (1, "true", "yes"):
        with tempfile.TemporaryDirectory() as t:
            base = _sdlc(t, {"verify": {"enforce": enforce_value, "command": ""}})
            c = _by_name(d.check(base, run=_runner()))["verify command present (enforce is on)"]
            assert c["ok"] is False and "every `done` is refused" in c["fix"], enforce_value


def test_features_dashboard_reports_enforce_on_for_non_bool_truthy_values():
    d = _doc()
    for enforce_value in (1, "true", "yes"):
        with tempfile.TemporaryDirectory() as t:
            base = _sdlc(t, {"verify": {"enforce": enforce_value}})
            rows = {name: state for name, state, _ in d.features(base)}
            assert rows["machine-checked done (verify.enforce)"].startswith("ON"), enforce_value


def test_doctor_and_loop_enforce_reads_agree_on_the_same_truth_table():
    """The parity check the duplication comment promises: doctor.py's _enforce_enabled and loop.py's
    own copy must never silently drift apart. Runs the SAME representative inputs (the full set from
    F17's own investigation, including the ones that only matter for a safety-gate's fail-direction)
    through both and asserts identical results."""
    d = _doc()
    L = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts" / "loop.py"
    spec = importlib.util.spec_from_file_location("loop", L)
    loop = importlib.util.module_from_spec(spec); spec.loader.exec_module(loop)

    for value in (True, False, 1, 0, -1, "true", "True", "FALSE", "false", "yes", "no", "off", "on",
                  "", None, [], {}, ["true"], 1.0, "disabled", "null", "  false  "):
        verify = {"enforce": value} if value is not None else {}
        assert d._enforce_enabled(verify) == loop._enforce_enabled(verify), value


# --- #416: gates.hard_plan_gate.enabled / gates.stop_gate.enabled read with the same fragile
# `is True` pattern F17/#342 already fixed for verify.enforce. Same fix direction, same tests.


def test_gate_enabled_reads_the_full_generous_truth_table():
    """`_gate_enabled` (#416) must answer the identical truth table `_enforce_enabled` already
    does -- same representative input set F17's own investigation used. Also pins doctor.py's copy
    against triage.py's own independent `_gate_enabled` (a fourth site sharing this exact read,
    found and fixed alongside the issue's original three -- triage.py cannot import doctor.py, no
    cross-skill import, so its copy is a genuine duplicate, not a re-export): the same lockstep
    discipline `test_doctor_and_loop_enforce_reads_agree_on_the_same_truth_table` above already
    established for the loop.py/doctor.py `_enforce_enabled` pair, extended to this third copy so
    a future edit to any ONE of the three fails loudly here rather than silently drifting."""
    d = _doc()
    T = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts" / "triage.py"
    spec = importlib.util.spec_from_file_location("triage", T)
    triage = importlib.util.module_from_spec(spec); spec.loader.exec_module(triage)

    for value in (True, False, 1, 0, -1, "true", "True", "FALSE", "false", "yes", "no", "off", "on",
                  "", None, [], {}, ["true"], 1.0, "disabled", "null", "  false  "):
        gate = {"enabled": value} if value is not None else {}
        expected = d._enforce_enabled({"enforce": value} if value is not None else {})
        assert d._gate_enabled(gate) == expected, value
        assert triage._gate_enabled(gate) == expected, value


def test_features_dashboard_reports_hard_plan_gate_on_for_non_bool_truthy_values():
    d = _doc()
    for enabled_value in (1, "true", "yes"):
        with tempfile.TemporaryDirectory() as t:
            base = _sdlc(t, {"gates": {"hard_plan_gate": {"enabled": enabled_value}}})
            rows = {name: state for name, state, _ in d.features(base)}
            assert rows["hard plan-gate (deny source edits w/o fresh plan)"].startswith("ON"), enabled_value


def test_features_dashboard_reports_stop_gate_on_for_non_bool_truthy_values():
    d = _doc()
    for enabled_value in (1, "true", "yes"):
        with tempfile.TemporaryDirectory() as t:
            base = _sdlc(t, {"gates": {"stop_gate": {"enabled": enabled_value}}})
            rows = {name: state for name, state, _ in d.features(base)}
            assert rows["Stop gate (refuse to end a session with unplanned source)"].startswith("ON"), enabled_value


# --- worktree footgun: a relative interpreter path fails exit=127 once work.enabled -------------

def test_flags_a_relative_venv_in_verify_command_when_work_on():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"work": {"enabled": True},
                         "verify": {"command": "cd backend && .venv/bin/python3 -m pytest -q"}})
        c = _by_name(d.check(base, run=_runner()))["verify.command resolves in the goal worktree"]
        assert c["ok"] is False and "exit=127" in c["fix"]


def test_an_absolute_interpreter_path_is_not_flagged():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"work": {"enabled": True},
                         "verify": {"command": "/abs/proj/.venv/bin/python3 -m pytest -q"}})
        assert _by_name(d.check(base, run=_runner()))["verify.command resolves in the goal worktree"]["ok"]


def test_flags_a_dot_slash_prefixed_relative_dep_when_work_on():
    """F23/#351: `./node_modules/…` is still relative to the goal worktree — the lookbehind that
    exempts absolute paths must not also swallow an explicit `./` prefix."""
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"work": {"enabled": True},
                         "verify": {"command": "./node_modules/.bin/eslint ."}})
        c = _by_name(d.check(base, run=_runner()))["verify.command resolves in the goal worktree"]
        assert c["ok"] is False and "exit=127" in c["fix"]


def test_flags_a_dot_dot_slash_prefixed_relative_dep_when_work_on():
    """F23/#351: same footgun for `../venv/…` (and repeated `../../…`) — a parent-relative dep path
    is still relative to the goal worktree, not an absolute path, so it must be flagged too."""
    d = _doc()
    with tempfile.TemporaryDirectory() as t1, tempfile.TemporaryDirectory() as t2:
        base = _sdlc(t1, {"work": {"enabled": True},
                          "verify": {"command": "../venv/bin/python -m pytest -q"}})
        c = _by_name(d.check(base, run=_runner()))["verify.command resolves in the goal worktree"]
        assert c["ok"] is False and "exit=127" in c["fix"]

        base2 = _sdlc(t2, {"work": {"enabled": True},
                           "verify": {"command": "../../node_modules/.bin/eslint ."}})
        c2 = _by_name(d.check(base2, run=_runner()))["verify.command resolves in the goal worktree"]
        assert c2["ok"] is False and "exit=127" in c2["fix"]


def test_no_worktree_dep_check_when_work_is_off():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"verify": {"command": ".venv/bin/python3 -m pytest"}})   # work off -> no worktree
        assert "verify.command resolves in the goal worktree" not in _by_name(d.check(base, run=_runner()))


# --- the dashboard surfaces the two silent-adoption states --------------------------------------

def test_features_work_off_says_nothing_is_written_to_git(tmp_path):
    d = _doc()
    rows = {n: s for n, s, _ in d.features(_sdlc(tmp_path, {}))}
    assert "writes NOTHING to git" in rows["per-goal worktree + PR"]


def test_features_reports_which_mechanism_ignores_the_runtime_dirs(tmp_path):
    d = _doc()
    base = _sdlc(tmp_path, {})                                       # repo root = tmp_path
    rows_none = {n: s for n, s, _ in d.features(base)}
    assert "NOT ignored" in rows_none["runtime dirs ignored via"]
    (tmp_path / ".gitignore").write_text(".sdlc/\n")
    rows_tracked = {n: s for n, s, _ in d.features(base)}
    assert "tracked .gitignore" in rows_tracked["runtime dirs ignored via"]


def test_features_reports_the_pr_review_gate(tmp_path):
    d = _doc()
    row = "PR review gate (independent of branch protection)"
    off = {n: s for n, s, _ in d.features(_sdlc(tmp_path / "a", {}))}
    assert off[row].startswith("off")
    on = {n: s for n, s, _ in d.features(_sdlc(tmp_path / "b", {"work": {"require_review": "approval"}}))}
    assert "ON (approval)" in on[row]


def test_features_reports_the_stop_gate(tmp_path):
    d = _doc()
    row = "Stop gate (refuse to end a session with unplanned source)"
    off = {n: s for n, s, _ in d.features(_sdlc(tmp_path / "a", {}))}
    assert off[row] == "off"
    on = {n: s for n, s, _ in d.features(_sdlc(tmp_path / "b", {"gates": {"stop_gate": {"enabled": True}}}))}
    assert "ON" in on[row]


def test_features_reports_session_start(tmp_path):
    d = _doc()
    row = "SessionStart policy brief"
    off = {n: s for n, s, _ in d.features(_sdlc(tmp_path / "a", {}))}
    assert off[row] == "off"
    on = {n: s for n, s, _ in d.features(_sdlc(tmp_path / "b", {"session_start": {"enabled": True}}))}
    assert on[row].startswith("ON")


def test_features_reports_goal_parallelism(tmp_path):
    """#1200 gap 1: doctor.features() has a "slice parallelism" row for `parallel.{enabled,
    max_concurrent}` but says nothing about the SIBLING `parallel.goals.{enabled,max_concurrent}`
    block that actually governs how many goals a run dispatches concurrently (loop.py's
    `goals_parallel`) -- the setting an operator most needs to see, on a default scaffold vs a
    config with it turned on must read visibly differently, matching the existing "slice
    parallelism" row's ON/off shape."""
    d = _doc()
    row = "goal parallelism"
    off = {n: s for n, s, _ in d.features(_sdlc(tmp_path / "a", {}))}
    assert off[row].startswith("off")
    on = {n: s for n, s, _ in d.features(
        _sdlc(tmp_path / "b", {"parallel": {"goals": {"enabled": True, "max_concurrent": 5}}}))}
    assert on[row].startswith("ON") and "5" in on[row]


def test_features_goal_parallelism_default_max_concurrent_is_3(tmp_path):
    d = _doc()
    on = {n: s for n, s, _ in d.features(_sdlc(tmp_path, {"parallel": {"goals": {"enabled": True}}}))}
    assert "3" in on["goal parallelism"]


def test_features_goal_parallelism_remediation_names_the_exact_config_path(tmp_path):
    d = _doc()
    rows = {n: (s, e) for n, s, e in d.features(_sdlc(tmp_path, {}))}
    _, enable = rows["goal parallelism"]
    assert '"parallel"' in enable and '"goals"' in enable and '"enabled": true' in enable


def _managing_session_check(checks):
    return next(c for c in checks if c["name"].startswith("managing session:"))


def test_check_reports_managing_session_not_registered_when_no_marker_exists(tmp_path):
    """#1200 gap 2: doctor.check() has no concurrency/session check at all. No marker has ever
    been written -> reads as "not registered", never an error (loop.session_active's own
    guarantee) and never a failed check (informational, ok=True either way -- same idiom the
    "companions" rows already use), and must NOT be confused with the unrelated "SessionStart
    policy brief" row in features() (the SessionStart HOOK config, not the loop's own session
    marker -- a real naming collision worth disambiguating)."""
    d = _doc()
    base = _sdlc(tmp_path, {})
    checks = d.check(base, run=_runner())
    c = _managing_session_check(checks)
    assert "not registered" in c["name"]
    assert c["ok"] is True and c["fix"] == ""
    feature_rows = {n: s for n, s, _ in d.features(base)}
    assert feature_rows["SessionStart policy brief"] == "off"    # unrelated hook config, untouched
    assert "not registered" not in feature_rows["SessionStart policy brief"]
    assert not any(n == "managing session" for n in feature_rows)   # lives in check(), not features()


def test_check_reports_managing_session_registered_for_a_live_pid(tmp_path):
    """Loads `loop` through the existing `_load_loop_script` cross-skill path rather than
    duplicating loop.py's session_active liveness logic (pid_alive + lease TTL) locally."""
    d = _doc()
    base = _sdlc(tmp_path, {})
    loop = d._load_loop_script("loop")
    loop.session_start(base, os.getpid())            # this test process is unambiguously alive
    c = _managing_session_check(d.check(base, run=_runner()))
    assert "registered" in c["name"] and "not registered" not in c["name"]
    assert c["ok"] is True


def test_check_reports_managing_session_not_registered_for_a_dead_pid(tmp_path):
    d = _doc()
    base = _sdlc(tmp_path, {})
    loop = d._load_loop_script("loop")
    loop.session_start(base, 2 ** 30)                 # not a real pid on any sane system
    c = _managing_session_check(d.check(base, run=_runner()))
    assert "not registered" in c["name"]
    assert c["ok"] is True


def test_features_surfaces_skill_selection_advisory(tmp_path):
    # a plugin can't disable a built-in, so this row is a static advisory (not a toggle) pointing at
    # the user-side remedies — it must always be present so adopters learn the limitation
    d = _doc()
    rows = {n: (s, e) for n, s, e in d.features(_sdlc(tmp_path, {}))}
    state, enable = rows["skill selection vs platform built-ins"]
    assert "can't disable a built-in" in state
    assert "skillOverrides" in enable and "/plugin disable" in enable


# --- pre-work backlog cross-check (0.9.22) ---------------------------------------------------

def _bc_rows(d, tmp_path, cfg):
    return {n: s for n, s, _ in d.features(_sdlc(tmp_path, cfg))}


def test_features_backlog_check_off_by_default_and_on_when_enabled(tmp_path):
    d = _doc()
    assert _bc_rows(d, tmp_path, {})["pre-work backlog cross-check"].startswith("off")   # absent -> off
    on = _bc_rows(d, tmp_path / "on", {"backlog_check": {"enabled": True}})["pre-work backlog cross-check"]
    assert on.startswith("ON") and "parks a confident" in on
    flag = _bc_rows(d, tmp_path / "flag",
                    {"backlog_check": {"enabled": True, "action": "flag"}})["pre-work backlog cross-check"]
    assert "flag mode" in flag and "never parks" in flag


def test_features_backlog_check_enabled_is_strict_true_not_truthy(tmp_path):
    d = _doc()
    # a stringy "true" / 1 must NOT switch a pick-path behavior on
    assert _bc_rows(d, tmp_path, {"backlog_check": {"enabled": "true"}})["pre-work backlog cross-check"].startswith("off")
    assert _bc_rows(d, tmp_path / "one", {"backlog_check": {"enabled": 1}})["pre-work backlog cross-check"].startswith("off")


def test_features_backlog_check_malformed_block_does_not_crash(tmp_path):
    d = _doc()
    assert _bc_rows(d, tmp_path, {"backlog_check": "yes please"})["pre-work backlog cross-check"].startswith("off")


# --- pre-work oversized-goal classifier (1.0.7) -----------------------------------------------

def _gd_rows(d, tmp_path, cfg):
    return {n: s for n, s, _ in d.features(_sdlc(tmp_path, cfg))}


def test_features_goal_decompose_off_by_default_and_on_when_enabled(tmp_path):
    d = _doc()
    assert _gd_rows(d, tmp_path, {})["pre-work oversized-goal classifier"].startswith("off")   # absent -> off
    park = _gd_rows(d, tmp_path / "park",
                     {"goal_decompose": {"enabled": True, "mode": "park"}})["pre-work oversized-goal classifier"]
    assert park.startswith("ON") and "parks an oversized goal" in park
    log = _gd_rows(d, tmp_path / "log",
                   {"goal_decompose": {"enabled": True, "mode": "log"}})["pre-work oversized-goal classifier"]
    assert "log mode" in log and "never parks" in log
    filed = _gd_rows(d, tmp_path / "file",
                     {"goal_decompose": {"enabled": True, "mode": "file"}})["pre-work oversized-goal classifier"]
    assert "Decompose #N" in filed


def test_features_goal_decompose_enabled_is_strict_true_not_truthy(tmp_path):
    d = _doc()
    # a stringy "true" / 1 must NOT switch a pick-path behavior on
    assert _gd_rows(d, tmp_path, {"goal_decompose": {"enabled": "true"}})["pre-work oversized-goal classifier"].startswith("off")
    assert _gd_rows(d, tmp_path / "one", {"goal_decompose": {"enabled": 1}})["pre-work oversized-goal classifier"].startswith("off")


def test_features_goal_decompose_malformed_block_does_not_crash(tmp_path):
    d = _doc()
    assert _gd_rows(d, tmp_path, {"goal_decompose": "yes please"})["pre-work oversized-goal classifier"].startswith("off")


def test_features_goal_decompose_unrecognized_mode_falls_back_to_log(tmp_path):
    d = _doc()
    # mirrors decompose_check's own fallback (loop.py): an unrecognized mode string reads as 'log',
    # never crashes and never reports a state the guard itself would never actually take.
    row = _gd_rows(d, tmp_path,
                   {"goal_decompose": {"enabled": True, "mode": "bogus-mode"}})["pre-work oversized-goal classifier"]
    assert "log mode" in row and "never parks" in row


# --- no-dangling-goal unit classification (#2429) -----------------------------------------------

_NDG_ROW = "no-dangling-goal unit classification (#2429)"


def _ndg_rows(d, tmp_path, cfg):
    return {n: s for n, s, _ in d.features(_sdlc(tmp_path, cfg))}


def _adopt_unit(base, name, open_=True, owner="me"):
    """A real, minimal `.sdlc/features/units/<name>.json` entry -- through `feature_registry.
    write_unit`, so this suite pins the shape the row actually reads rather than a hand-rolled
    one, mirroring `tests/test_feature_labels.py`'s own `_adopt` helper."""
    fr = _load_loop_script_for_test("feature_registry")
    features_dir = fr.registry_dir(base)
    features_dir.mkdir(parents=True, exist_ok=True)
    fr.write_unit(features_dir, name,
                  {"title": name, "owner": owner, "open": open_, "repos": {}})


def _load_loop_script_for_test(name):
    path = D.parent.parent.parent / "sigma-loop" / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def test_features_no_dangling_goal_off_by_default(tmp_path):
    d = _doc()
    assert _ndg_rows(d, tmp_path, {})[_NDG_ROW].startswith("off")


def test_features_no_dangling_goal_on_with_no_catch_all_configured(tmp_path):
    d = _doc()
    row = _ndg_rows(d, tmp_path,
                    {"discovery": {"no_dangling_goal": {"enabled": True}}})[_NDG_ROW]
    assert row.startswith("ON") and "no catch-all configured" in row and "sdlc:needs-unit" in row


def test_features_no_dangling_goal_catch_all_configured_but_registry_not_adopted(tmp_path):
    d = _doc()
    row = _ndg_rows(d, tmp_path,
                    {"discovery": {"no_dangling_goal": {"enabled": True, "core": "core"}}})[_NDG_ROW]
    assert "does not exist yet" in row and "inert" in row


def test_features_no_dangling_goal_catch_all_resolves_to_a_real_unit(tmp_path):
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"no_dangling_goal": {"enabled": True, "core": "core"}}})
    _adopt_unit(base, "core")
    row = {n: s for n, s, _ in d.features(base)}[_NDG_ROW]
    assert "resolves to the registered unit 'core'" in row


def test_features_no_dangling_goal_catch_all_does_not_resolve(tmp_path):
    """A typo, or a unit that was never opened -- `resolve_open_unit` answers None and the row says
    so proactively, rather than the typo sitting invisible until the classifier actually runs."""
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"no_dangling_goal": {"enabled": True, "core": "cor"}}})
    _adopt_unit(base, "core")                # registered as "core", configured as "cor" (a typo)
    row = {n: s for n, s, _ in d.features(base)}[_NDG_ROW]
    assert "does NOT resolve" in row and "typo" in row


def test_features_no_dangling_goal_catch_all_closed_unit_does_not_resolve(tmp_path):
    """`resolve_open_unit` answers None for a CLOSED unit too (#1820's own deliberate rule) -- a
    catch-all pointed at one must be flagged the same way a typo is, not silently treated as fine."""
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"no_dangling_goal": {"enabled": True, "core": "core"}}})
    _adopt_unit(base, "core", open_=False)
    row = {n: s for n, s, _ in d.features(base)}[_NDG_ROW]
    assert "does NOT resolve" in row


def test_features_no_dangling_goal_live_judge_state(tmp_path):
    d = _doc()
    off = _ndg_rows(d, tmp_path,
                    {"discovery": {"no_dangling_goal": {"enabled": True}}})[_NDG_ROW]
    assert "live judge off" in off
    on = _ndg_rows(d, tmp_path / "on", {"discovery": {"no_dangling_goal": {
        "enabled": True, "live_judge": {"enabled": True, "rounds": 5,
                                        "spend_ceiling_usd_per_day": 2.5}}}})[_NDG_ROW]
    assert "live judge ON" in on and "rounds=5" in on and "$2.50/day" in on and "corrected" not in on


def test_features_no_dangling_goal_live_judge_rounds_correction_is_named(tmp_path):
    """#2428's own sibling: a `rounds: 1` that the config-reader silently corrects to 3 is named
    here, by comparing the RAW configured value against the resolved one -- a human sees their own
    typo instead of a number that quietly stopped meaning what they typed."""
    d = _doc()
    row = _ndg_rows(d, tmp_path, {"discovery": {"no_dangling_goal": {
        "enabled": True, "live_judge": {"enabled": True, "rounds": 1}}}})[_NDG_ROW]
    assert "rounds=3" in row and "configured 1 was invalid" in row and "corrected to 3" in row


def test_features_no_dangling_goal_malformed_discovery_block_does_not_crash(tmp_path):
    """The real bug this test pins: `sources._no_dangling_goal_enabled` (and its siblings) trust
    `config['discovery']` to already be a dict, which every OTHER caller's config guarantees --
    doctor's own fuzz coverage does not, and `(cfg.get('discovery') or {})` evaluates a TRUTHY
    non-dict value (a bare string/int/bool/list) unchanged, crashing the next `.get()`. Found live
    while adding this very row; fixed by sanitizing through `_block()` before handing anything to
    the cross-loaded `sources` functions."""
    d = _doc()
    for i, bad in enumerate(("a string", ["a", "list"], 42, True)):
        row = _ndg_rows(d, tmp_path / str(i), {"discovery": bad})[_NDG_ROW]
        assert row.startswith("off"), (bad, row)


def test_check_flags_park_threshold_below_dup_threshold(tmp_path):
    d = _doc()
    base = _sdlc(tmp_path, {"backlog_check": {"enabled": True, "dup_threshold": 0.72, "park_threshold": 0.5}})
    checks = {c["name"]: c for c in d.check(base, run=_runner())}
    assert checks["backlog cross-check thresholds sane"]["ok"] is False
    assert "park_threshold" in checks["backlog cross-check thresholds sane"]["fix"]


def test_check_flags_embed_enabled_with_no_command(tmp_path):
    d = _doc()
    dead = _sdlc(tmp_path, {"backlog_check": {"enabled": True, "embed": {"enabled": True, "command": ""}}})
    assert {c["name"]: c for c in d.check(dead, run=_runner())}["backlog cross-check embedder configured"]["ok"] is False
    live = _sdlc(tmp_path / "live",
                 {"backlog_check": {"enabled": True, "embed": {"enabled": True, "command": "my-embedder"}}})
    assert {c["name"]: c for c in d.check(live, run=_runner())}["backlog cross-check embedder configured"]["ok"] is True


def test_check_does_not_crash_on_a_non_dict_backlog_check_block(tmp_path):
    d = _doc()
    base = _sdlc(tmp_path, {"backlog_check": "yes please"})     # malformed -> reads as off, no crash
    names = {c["name"] for c in d.check(base, run=_runner())}
    assert "backlog cross-check thresholds sane" not in names


def test_check_backlog_thresholds_ok_when_sane_and_absent_when_disabled(tmp_path):
    d = _doc()
    sane = _sdlc(tmp_path, {"backlog_check": {"enabled": True, "dup_threshold": 0.72, "park_threshold": 0.8}})
    assert {c["name"]: c for c in d.check(sane, run=_runner())}["backlog cross-check thresholds sane"]["ok"] is True
    off = _sdlc(tmp_path / "off", {"backlog_check": {"enabled": False, "park_threshold": 0.1}})
    assert "backlog cross-check thresholds sane" not in {c["name"] for c in d.check(off, run=_runner())}


# --- #389: /sigma-doctor dependency-marker check -- a comment matching backlog_check._BLOCK_RE with
# NO matching body marker is likely-intended-but-silently-ignored by precheck(). Cost-bounded (R6:
# default max_issues=10, ~6s added on a real repo, down from an initial 30/~18.5s draft) and the
# bound is always visibly reported in the check's own `name`, pass or fail -- never silently applied.
# NOT gated on backlog_check.enabled (same github-only gating as the existing gh auth/project checks).

def _dm_run(issues, comments=None, view_calls=None):
    """Fake doctor runner: `gh issue list` answers `issues` ([{"number", "body"}, ...]); every
    `gh issue view ... --json comments` answers the SAME canned `comments` list (tests that care which
    issue was asked don't need to here -- there is only ever one real candidate in play). Records every
    `issue view` call into `view_calls` when given, so a test can assert on cost (how many, not just
    whether)."""
    comments = comments if comments is not None else []

    def run(args):
        if args[:3] == ["gh", "auth", "status"]:
            return "Logged in."
        if _is_list(args):
            return json.dumps(issues)
        if args[:3] == ["gh", "issue", "view"]:
            if view_calls is not None:
                view_calls.append(list(args))
            return json.dumps({"comments": comments})
        t = gqlfake.rest_issue_target(args[1:])      # #895: fetch_comments reads REST first
        if t is not None:
            if view_calls is not None and not t[1]:  # one issue read per issue, comment pages aside
                view_calls.append(list(args))
            return gqlfake.rest_issue(args[1:], lambda n, f: {"comments": comments})
        return ""
    return run


def _dm_check(checks):
    return next(c for n, c in checks.items() if n.startswith("dependency markers:"))


def test_dependency_marker_doctor_check_flags_comment_only_marker(tmp_path):
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}})
    issues = [{"number": 42, "body": "no marker here"}]
    comment = {"id": "IC_1", "author": {"login": "bob"}, "body": "blocked by #9 until that lands",
               "createdAt": "2026-08-01T00:00:00Z"}
    checks = {c["name"]: c for c in d.check(base, run=_dm_run(issues, [comment]))}
    hit = _dm_check(checks)
    assert hit["ok"] is False
    assert "#42" in hit["fix"]


def test_dependency_marker_doctor_check_fix_nudges_toward_enabling_backlog_check_when_off(tmp_path):
    """C1 (independent review of PR #480): this check is deliberately NOT gated on
    backlog_check.enabled (a nudge toward turning it on) -- but precheck() returns OFF before ever
    reaching cross_check() while it's off, so a BODY marker is ignored exactly as much as a
    comment-only one is. The fix text must say so -- otherwise its own suggested next step (re-file,
    or add a body marker) does nothing while the setting stays off."""
    d = _doc()
    issues = [{"number": 42, "body": "no marker here"}]
    comment = {"id": "IC_1", "author": {"login": "bob"}, "body": "blocked by #9 until that lands",
               "createdAt": "2026-08-01T00:00:00Z"}

    off = _sdlc(tmp_path / "off", {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}})
    off_hit = _dm_check({c["name"]: c for c in d.check(off, run=_dm_run(issues, [comment]))})
    assert off_hit["ok"] is False
    assert "backlog_check.enabled" in off_hit["fix"]
    assert "true" in off_hit["fix"]

    # once actually enabled, the base advice IS actionable -- the off-specific nudge would be noise,
    # so it must NOT appear.
    on = _sdlc(tmp_path / "on", {"discovery": {"source": "github", "github": {"repo": "acme/widget"}},
                                 "backlog_check": {"enabled": True}})
    on_hit = _dm_check({c["name"]: c for c in d.check(on, run=_dm_run(issues, [comment]))})
    assert on_hit["ok"] is False
    assert "backlog_check.enabled" not in on_hit["fix"]


def test_dependency_marker_doctor_check_silent_when_body_already_has_marker(tmp_path):
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}})
    issues = [{"number": 9, "body": "do the migration\n\n**Blocked by:** #3"}]
    view_calls = []
    checks = {c["name"]: c for c in d.check(base, run=_dm_run(issues, view_calls=view_calls))}
    hit = _dm_check(checks)
    assert hit["ok"] is True
    assert view_calls == []          # a body-marked issue is never charged a comment fetch (cost proof)


def test_dependency_marker_doctor_check_caps_at_max_issues_and_reports_the_bound(tmp_path):
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}},
                            "backlog_check": {"doctor_scan": {"max_issues": 5}}})
    issues = [{"number": n, "body": "no marker"} for n in range(1, 51)]     # 50 candidates, none pre-marked
    view_calls = []
    checks = {c["name"]: c for c in d.check(base, run=_dm_run(issues, view_calls=view_calls))}
    hit = _dm_check(checks)
    assert len(view_calls) == 5              # capped at max_issues, not charged for all 50
    assert "5/50" in hit["name"]             # the bound is visible on every run, pass or fail


def test_dependency_marker_doctor_check_skipped_in_local_mode(tmp_path):
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "local-goals"}})
    names = [c["name"] for c in d.check(base, run=_runner())]
    assert not any(n.startswith("dependency markers:") for n in names)


def test_dependency_marker_doctor_check_default_max_issues_is_ten(tmp_path):
    """R6: the plan-review measured ~0.62s/call for `gh issue view --json comments` on a real repo;
    at the ORIGINAL draft default of 30 that is ~18.5s added to a routine /sigma-doctor run (4-7x
    regression), and since candidates are issues WITHOUT a body marker -- nearly all of them in
    practice -- that cap is hit on essentially any real backlog, so it was the TYPICAL cost, not a
    worst case. Lowered to 10 (~6s) by default, still configurable."""
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}})
    issues = [{"number": n, "body": "no marker"} for n in range(1, 31)]    # 30 candidates, no override
    view_calls = []
    checks = {c["name"]: c for c in d.check(base, run=_dm_run(issues, view_calls=view_calls))}
    assert len(view_calls) == 10
    assert "10/30" in _dm_check(checks)["name"]


def test_dependency_marker_doctor_check_survives_a_malformed_doctor_scan_block(tmp_path):
    # F6 class: a truthy non-dict backlog_check.doctor_scan (a hand-edited config typo) must degrade
    # to the defaults, never crash the one tool an adopter runs BECAUSE their config is wrong --
    # matches every other block reader in this file (_block()'s own documented contract).
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}},
                            "backlog_check": {"doctor_scan": "oops"}})
    issues = [{"number": 1, "body": "no marker"}]
    d.check(base, run=_dm_run(issues))    # must not raise


# --- #1205: /sigma-doctor blocked-label check -- discovery.github.blocked_label is what makes the
# label-queue path (sources.py's _fetch_pending) actually honor a repo-local "do not auto-pick"
# label; unset (the default), an issue carrying such a label is fully visible to the picker with
# nothing warning the convention is inert. This nudges that gap at setup time: unset + a
# blocked-looking label already on an open goal issue -> named, with the one-line fix.

def _bl_run(issues):
    """Fake doctor runner for the blocked-label scan: `gh issue list ... --json number,labels`
    answers `issues` ([{"number", "labels": [{"name": ...}, ...]}, ...])."""
    def run(args):
        if args[:3] == ["gh", "auth", "status"]:
            return "Logged in."
        if _is_list(args):
            return json.dumps(issues)
        return ""
    return run


_BL_NAME = "blocked-ish label(s) found but discovery.github.blocked_label is unset"


def test_blocked_label_doctor_check_flags_blocked_ish_label_when_key_unset(tmp_path):
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}})
    # This check spots a REPO'S OWN blocked-ish convention, so the fixture needs a label that is
    # not one of the kit's. #1393: `sdlc:blocked`/`sdlc:blocking` are now skipped -- `_BLOCKED_HINT`
    # is /block/i and a blocked goal keeps `sdlc:goal`, so without that skip this check fired on
    # every machine-blocked goal, telling adopters to configure `blocked_label` for a label the loop
    # already manages and already excludes.
    issues = [
        {"number": 42, "labels": [{"name": "sdlc:goal"}, {"name": "blocked-on-legal"}]},
        {"number": 43, "labels": [{"name": "sdlc:goal"}]},
    ]
    checks = {c["name"]: c for c in d.check(base, run=_bl_run(issues))}
    hit = checks[_BL_NAME]
    assert hit["ok"] is False
    assert "#42" in hit["fix"]
    assert "#43" not in hit["fix"]                      # the non-blocked issue is never named
    assert "blocked-on-legal" in hit["fix"]
    assert "discovery.github.blocked_label" in hit["fix"]


def test_blocked_label_doctor_check_silent_when_no_blocked_ish_label_found(tmp_path):
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}})
    issues = [{"number": 42, "labels": [{"name": "sdlc:goal"}, {"name": "priority:P1"}]}]
    names = {c["name"] for c in d.check(base, run=_bl_run(issues))}
    assert _BL_NAME not in names


def test_blocked_label_doctor_check_silent_once_blocked_label_is_configured(tmp_path):
    # The whole point of the key: once set, the gap it warns about no longer exists -- the check
    # must not keep nagging just because a matching label is still present on some issue.
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github",
                                          "github": {"repo": "acme/widget", "blocked_label": "sdlc:blocked"}}})
    # #1393: `goal`+`blocked` is the CORRECT shape of a blocked goal now (blocked is an overlay,
    # not a state) -- the genuinely contradictory pair is membership plus the human-owned exit.
    issues = [{"number": 42, "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:parked"}]}]
    names = {c["name"] for c in d.check(base, run=_bl_run(issues))}
    assert _BL_NAME not in names


def test_blocked_label_doctor_check_ignores_an_issue_already_parked(tmp_path):
    # Already excluded from the picker via parked_label regardless of blocked_label -- not a real
    # exposure, so flagging it would be a false alarm.
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}})
    issues = [{"number": 42, "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:parked"}, {"name": "sdlc:blocked"}]}]
    names = {c["name"] for c in d.check(base, run=_bl_run(issues))}
    assert _BL_NAME not in names


def test_blocked_label_doctor_check_degrades_cleanly_when_backlog_cannot_be_read(tmp_path):
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}})

    def broken(args):
        if args[:3] == ["gh", "auth", "status"]:
            return "Logged in."
        if _is_list(args):
            return ""          # simulates a failed/empty gh call
        return ""
    checks = d.check(base, run=broken)                 # must not raise
    assert _BL_NAME not in {c["name"] for c in checks}

    def malformed(args):
        if args[:3] == ["gh", "auth", "status"]:
            return "Logged in."
        if _is_list(args):
            return "not json"
        return ""
    checks = d.check(base, run=malformed)               # must not raise
    assert _BL_NAME not in {c["name"] for c in checks}


def test_blocked_label_doctor_check_skipped_in_local_mode(tmp_path):
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "local-goals"}})
    # #1393: `goal`+`blocked` is the CORRECT shape of a blocked goal now (blocked is an overlay,
    # not a state) -- the genuinely contradictory pair is membership plus the human-owned exit.
    issues = [{"number": 42, "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:parked"}]}]
    names = {c["name"] for c in d.check(base, run=_bl_run(issues))}
    assert _BL_NAME not in names


# --- #1391 step 5a / #1579: the CENSUS row. doctor's own scans query BY LABEL and so cannot see an
# issue whose defect is a MISSING label; the census enumerates the population instead. The row had
# NO test at all -- doctor calls `reconcile.census` directly and renders whatever it returns, so
# every census enumerator bug was silently a doctor bug too, and #1579 (`sdlc:goal` missing from
# E3) made doctor blind to 23 of the 24 real cases on this repo without a single test going red.

_CENSUS_NAME = "backlog label state is coherent"
_CENSUS_READ_NAME = "backlog label census could be read in full"


def _census_run(by_label, fail_labels=()):
    """Fake doctor runner keyed on `(label, state)`, so the census row can be driven precisely.
    `_bl_run` answers every `issue list` with one fixed list regardless of label, which cannot tell
    a census that queried a label from one that did not -- exactly the seam #1579 lived in.

    A "failure" is unparseable stdout rather than a raised exception: doctor's OTHER label scans
    share this runner and are not all fail-open against a raise, so raising would test the harness
    rather than the row. `_fetch_by_label` reads both as the same `ok=False`."""
    def run(args):
        if args[:3] == ["gh", "auth", "status"]:
            return "Logged in."
        if not _is_list(args):
            return ""
        label, state = _list_label(args), _list_state(args)
        if label in fail_labels:
            return "not json"
        return json.dumps([_rest_row(r) for r in by_label.get((label, state), [])])
    return run


def _closed_issue(number, *labels):
    return {"number": number, "state": "CLOSED", "closedAt": "2020-01-01T00:00:00Z",
            "labels": [{"name": n} for n in labels]}


def test_census_doctor_row_flags_a_closed_issue_still_carrying_the_goal_label(tmp_path):
    """The #1579 shape end to end: no overlay, so only an E3 query on `sdlc:goal` itself finds it,
    and doctor is repaired for free by the enumerator fix rather than by a new row."""
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}})
    run = _census_run({("sdlc:goal", "closed"): [_closed_issue(7, "sdlc:goal")]})
    checks = {c["name"]: c for c in d.check(base, run=run)}
    hit = checks[_CENSUS_NAME]
    assert hit["ok"] is False
    assert "closed-with-state" in hit["fix"] and "#7" in hit["fix"]


def test_census_doctor_row_is_silent_on_a_coherent_board(tmp_path):
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}})
    run = _census_run({("sdlc:goal", "open"): [{"number": 42, "state": "OPEN",
                                                "labels": [{"name": "sdlc:goal"}]}]})
    names = {c["name"] for c in d.check(base, run=run)}
    assert _CENSUS_NAME not in names and _CENSUS_READ_NAME not in names


def test_census_doctor_row_says_it_could_not_look_rather_than_found_nothing(tmp_path):
    """"I found nothing" and "I could not look" must never render the same."""
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}})
    run = _census_run({}, fail_labels={"sdlc:goal"})
    checks = {c["name"]: c for c in d.check(base, run=run)}
    hit = checks[_CENSUS_READ_NAME]
    assert hit["ok"] is False
    assert "at least one query failed" in hit["fix"]
    assert "sdlc:goal" in hit["fix"]


def test_census_doctor_row_sources_its_reason_from_the_census_not_a_fixed_string(tmp_path):
    """#1579: a failed query and a saturated read both clear `complete` and have different remedies,
    so the row now asks the census WHY instead of hardcoding "at least one census query failed".
    Naming the failed label is the observable proof the text is sourced rather than fixed -- the
    truncation branch itself is pinned in `test_reconcile` (`_load_loop_script` re-execs a fresh
    reconcile module per call, so a monkeypatched ceiling cannot reach the one doctor loads)."""
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}})
    checks = {c["name"]: c for c in d.check(base, run=_census_run({}, fail_labels={"sdlc:parked"}))}
    assert "sdlc:parked (open)" in checks[_CENSUS_READ_NAME]["fix"]


# --- #1354: the single-primary-state-label invariant. NOT "no issue has more than one sdlc:*
# label ever" -- sdlc:in-progress is an orthogonal, additive marker (coexists with sdlc:goal by
# design) and is excluded from this check entirely. The real invariant: no issue should carry more
# than one of {sdlc:goal, sdlc:parked, sdlc:needs-confirmation} at once. #1393 removed
# sdlc:blocked from that set: it became an OVERLAY that rides alongside sdlc:goal, exactly like
# sdlc:in-progress, so goal+blocked is correct rather than contradictory.

_MSL_NAME = "no issue carries more than one primary sdlc:* state label"


def test_multi_state_label_doctor_check_flags_an_issue_carrying_two_primary_labels(tmp_path):
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}})
    issues = [
        {"number": 42, "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:parked"}]},
        {"number": 43, "labels": [{"name": "sdlc:goal"}]},
    ]
    checks = {c["name"]: c for c in d.check(base, run=_bl_run(issues))}
    hit = checks[_MSL_NAME]
    assert hit["ok"] is False
    assert "#42" in hit["fix"]
    assert "#43" not in hit["fix"]


def test_multi_state_label_doctor_check_silent_when_every_issue_has_at_most_one(tmp_path):
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}})
    issues = [{"number": 42, "labels": [{"name": "sdlc:goal"}, {"name": "priority:P1"}]},
             {"number": 43, "labels": [{"name": "sdlc:parked"}]}]
    names = {c["name"] for c in d.check(base, run=_bl_run(issues))}
    assert _MSL_NAME not in names


def test_multi_state_label_doctor_check_excludes_in_progress_from_the_scanned_set(tmp_path):
    """The corrected invariant's whole point: sdlc:in-progress legitimately coexists with
    sdlc:goal on every normal active-work issue -- flagging that combination would be constant,
    correct-behavior noise, not a real defect."""
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}})
    issues = [{"number": 42, "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:in-progress"}]}]
    names = {c["name"] for c in d.check(base, run=_bl_run(issues))}
    assert _MSL_NAME not in names


def test_multi_state_label_doctor_check_flags_three_or_more_primary_labels_at_once(tmp_path):
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}})
    issues = [{"number": 7, "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:parked"},
                                       {"name": "sdlc:blocked"}]}]
    checks = {c["name"]: c for c in d.check(base, run=_bl_run(issues))}
    hit = checks[_MSL_NAME]
    assert hit["ok"] is False
    assert "#7" in hit["fix"]


def test_multi_state_label_doctor_check_honors_a_configured_proposed_label(tmp_path):
    """The needs-confirmation label is read the same way handoff.proposed_label(config) resolves
    it (ledger.handoff.proposed_label) -- a repo overriding it must still be scanned correctly."""
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}},
                            "ledger": {"handoff": {"proposed_label": "sdlc:needs-review"}}})
    issues = [{"number": 42, "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:needs-review"}]}]
    checks = {c["name"]: c for c in d.check(base, run=_bl_run(issues))}
    hit = checks[_MSL_NAME]
    assert hit["ok"] is False
    assert "#42" in hit["fix"]


def test_multi_state_label_doctor_check_degrades_cleanly_when_backlog_cannot_be_read(tmp_path):
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}})

    def broken(args):
        if args[:3] == ["gh", "auth", "status"]:
            return "Logged in."
        if _is_list(args):
            return ""
        return ""
    checks = d.check(base, run=broken)                   # must not raise
    assert _MSL_NAME not in {c["name"] for c in checks}

    def malformed(args):
        if args[:3] == ["gh", "auth", "status"]:
            return "Logged in."
        if _is_list(args):
            return "not json"
        return ""
    checks = d.check(base, run=malformed)                 # must not raise
    assert _MSL_NAME not in {c["name"] for c in checks}


def test_multi_state_label_doctor_check_survives_a_malformed_ledger_config_block(tmp_path):
    """A truthy non-dict `ledger` block (a plausible config typo, e.g. a string instead of an
    object) must never crash check() -- every other nested-block read in this file goes through
    `_block()` for exactly this reason; the proposed_label read here must too."""
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}},
                            "ledger": "enabled"})
    # #1393: `goal`+`blocked` is the CORRECT shape of a blocked goal now (blocked is an overlay,
    # not a state) -- the genuinely contradictory pair is membership plus the human-owned exit.
    issues = [{"number": 42, "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:parked"}]}]
    checks = d.check(base, run=_bl_run(issues))            # must not raise
    hit = {c["name"]: c for c in checks}[_MSL_NAME]
    assert hit["ok"] is False
    assert "#42" in hit["fix"]


def test_multi_state_label_doctor_check_survives_a_malformed_handoff_config_block(tmp_path):
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}},
                            "ledger": {"handoff": "sdlc:needs-confirmation"}})
    # #1393: `goal`+`blocked` is the CORRECT shape of a blocked goal now (blocked is an overlay,
    # not a state) -- the genuinely contradictory pair is membership plus the human-owned exit.
    issues = [{"number": 42, "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:parked"}]}]
    checks = d.check(base, run=_bl_run(issues))            # must not raise
    hit = {c["name"]: c for c in checks}[_MSL_NAME]
    assert hit["ok"] is False
    assert "#42" in hit["fix"]


def test_multi_state_label_doctor_check_skipped_in_local_mode(tmp_path):
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "local-goals"}})
    # #1393: `goal`+`blocked` is the CORRECT shape of a blocked goal now (blocked is an overlay,
    # not a state) -- the genuinely contradictory pair is membership plus the human-owned exit.
    issues = [{"number": 42, "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:parked"}]}]
    names = {c["name"] for c in d.check(base, run=_bl_run(issues))}
    assert _MSL_NAME not in names


# --- #1207: /sigma-doctor states plainly whether board columns (including Blocked) actually gate
# the pick path. Under `queue_source: "label"` (or with the board/project off entirely), sources.py's
# `_ready_lane()` short-circuits BEFORE it ever reads the board -- `if not self.project_enabled or
# self.queue_source != "status": return None` -- so a card sitting in Blocked is not protected from
# being picked and nothing at run time said so. This is the doctor-side statement of that fact.
#
# Lives in `features()`, not `check()`: `_chk()` only ever prints its `fix` text on a FAILING
# (`ok=False`) row, so a plain, always-legible statement -- true for every queue mode, not just a
# "wrong" one -- has to be a `features()` row (name+state always print, no pass/fail framing).
# `queue_source: "label"` is frequently a deliberate, correct choice; treating it as a doctor
# "gap" would nag forever with nothing to fix. Deliberately config-only, mirroring `_ready_lane()`'s
# own FIRST line (`project_enabled` / `queue_source` are both plain config reads in
# `GitHubSource.__init__`, never a live `gh` call) -- so there is no live board read here to fail,
# and the "degrades cleanly when project/board state can't be read" acceptance criterion is met by
# construction: nothing is ever read that could fail to be read.

def _qs_features(base, run=None):
    return {name: state for name, state, _ in _doc().features(base, run=run)}


_QS_NAME = "pick-path board gating (queue_source)"

#: A live board WITH a "Ready" option already in its Status field -- the state `_ready_lane()`
#: needs to actually treat "status" as board-authoritative. Matches on subcommand prefix only
#: (same convention as `_board_dup_risk`'s / `_unmapped_board_fields`'s fixtures above), and the
#: default title `GitHubSource._proj_title()` produces when no `repo` is configured ("project —
#: SDLC") so it resolves without every test having to set one.
def _ready_board_run(a):
    if a[:3] == ["gh", "project", "list"]:
        return json.dumps({"projects": [{"number": 1, "id": "PID_1", "title": "project — SDLC"}]})
    if a[:3] == ["gh", "project", "field-list"]:
        return json.dumps({"fields": [{"id": "F1", "name": "Status",
                                       "options": [{"id": "s1", "name": "Ready"}]}]})
    return ""


def test_features_reports_board_gated_for_the_default_status_queue(tmp_path):
    # BOARD-GATED requires BOTH: config says "status" AND a live board with a real "Ready" option
    # in its Status field (#1268 review finding) -- this is that live-confirmed case.
    base = _sdlc(tmp_path, {"discovery": {"source": "github",
                                          "github": {"project": {"enabled": True}}}})
    state = _qs_features(base, run=_ready_board_run)[_QS_NAME]
    assert "BOARD-GATED" in state
    assert "Blocked" in state and "un-pickable" in state


def test_features_does_not_claim_board_gated_before_the_board_exists(tmp_path):
    """PR #1268 review finding: `_pick_path_gate_state` used to report BOARD-GATED from config
    alone (`project.enabled=True`, `queue_source` defaulting to "status"), but the REAL pick-path
    gate -- `GitHubSource._ready_lane()` -- goes on past that config check to do a live read: it
    confirms a board actually EXISTS (`_find_project`), which is false right after `sigma-init`
    (board creation is lazy -- the first status WRITE, per sources.py's own comment), not at config
    time. `gh project list` returning no matching project is exactly that state. The real
    `_ready_lane()` returns None in this state -- Blocked gates nothing, the pick path falls
    through to the label queue -- so doctor claiming BOARD-GATED here is the exact false assurance
    issue #1207 was filed to prevent."""
    base = _sdlc(tmp_path, {"discovery": {"source": "github",
                                          "github": {"project": {"enabled": True}}}})
    run = lambda a: json.dumps({"projects": []}) if a[:3] == ["gh", "project", "list"] else ""
    state = _qs_features(base, run=run)[_QS_NAME]
    assert "BOARD-GATED" not in state, state
    assert "does NOT gate" in state or "does not gate" in state.lower()


def test_features_does_not_claim_board_gated_before_the_board_is_migrated(tmp_path):
    """Same false-assurance gap, the OTHER live precondition: a board exists, but its Status field
    has never been migrated to add a "Ready" option (`_ready_lane()`'s own docstring names this
    "THE BACKWARD-COMPATIBILITY GATE" -- any existing adopter who hasn't run `board_migrate.py`).
    `_ready_lane()` returns None here too -- must not read as BOARD-GATED."""
    base = _sdlc(tmp_path, {"discovery": {"source": "github",
                                          "github": {"project": {"enabled": True}}}})

    def run(a):
        if a[:3] == ["gh", "project", "list"]:
            return json.dumps({"projects": [{"number": 1, "id": "PID_1", "title": "project — SDLC"}]})
        if a[:3] == ["gh", "project", "field-list"]:
            # an adopted, un-migrated board: Status exists but has no "Ready" option
            return json.dumps({"fields": [{"id": "F1", "name": "Status",
                                           "options": [{"id": "s1", "name": "Todo"},
                                                       {"id": "s2", "name": "Done"}]}]})
        return ""

    state = _qs_features(base, run=run)[_QS_NAME]
    assert "BOARD-GATED" not in state, state
    assert "board_migrate" in state


def test_features_pick_path_gating_degrades_cleanly_when_the_board_read_fails(tmp_path):
    """A `gh` call that fails entirely (no auth, network down, `gh` missing) must degrade to the
    same non-BOARD-GATED message as "board doesn't exist yet" -- never crash `/sigma-doctor`, and
    never claim protection that can't be confirmed live."""
    base = _sdlc(tmp_path, {"discovery": {"source": "github",
                                          "github": {"project": {"enabled": True}}}})
    state = _qs_features(base, run=lambda a: "")[_QS_NAME]
    assert "BOARD-GATED" not in state, state


def test_features_reports_label_only_when_queue_source_is_label(tmp_path):
    base = _sdlc(tmp_path, {"discovery": {"source": "github",
                                          "github": {"project": {"enabled": True,
                                                                  "queue_source": "label"}}}})
    state = _qs_features(base)[_QS_NAME]
    assert "LABEL-ONLY" in state
    assert "Blocked" in state and "does NOT gate" in state


def test_features_reports_label_only_when_project_is_not_enabled(tmp_path):
    # No board at all -- still worth saying plainly: no column, Blocked included, gates picking,
    # because there is no board for a card to even sit on.
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {}}})
    state = _qs_features(base)[_QS_NAME]
    assert "LABEL-ONLY" in state
    assert "no board configured" in state


def test_features_pick_path_gating_is_na_for_non_github_discovery(tmp_path):
    base = _sdlc(tmp_path, {"discovery": {"source": "local-goals"}})
    state = _qs_features(base)[_QS_NAME]
    assert "n/a" in state.lower()


def test_features_pick_path_gating_differs_meaningfully_between_label_and_status(tmp_path):
    # The acceptance criterion, made explicit: the two configurations must not read alike.
    (tmp_path / "label").mkdir()
    (tmp_path / "status").mkdir()
    label_base = _sdlc(tmp_path / "label", {"discovery": {"source": "github",
                        "github": {"project": {"enabled": True, "queue_source": "label"}}}})
    status_base = _sdlc(tmp_path / "status", {"discovery": {"source": "github",
                         "github": {"project": {"enabled": True, "queue_source": "status"}}}})
    label_state = _qs_features(label_base, run=_ready_board_run)[_QS_NAME]
    status_state = _qs_features(status_base, run=_ready_board_run)[_QS_NAME]
    assert label_state != status_state
    assert "does NOT gate" in label_state and "does NOT gate" not in status_state
    assert "un-pickable" in status_state and "un-pickable" not in label_state


def _sources():
    #: skills/sigma-doctor/scripts/doctor.py -> skills/sigma-loop/scripts/sources.py, the module
    #: `_pick_path_gate_state`'s docstring claims to mirror byte for byte.
    S = D.parent.parent.parent / "sigma-loop" / "scripts" / "sources.py"
    spec = importlib.util.spec_from_file_location("sources", S)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def test_pick_path_gate_enabled_check_matches_sources_bool_coercion(tmp_path):
    """PR #1268 review finding: `_pick_path_gate_state`'s enabled check must mirror
    `GitHubSource.project_enabled = bool(cfg.get("enabled", False))` (sources.py:480) -- plain
    truthy coercion -- not a strict `is not True` identity check. A truthy-but-non-bool `enabled`
    (`1`, a non-empty string, a non-empty list/dict) is genuinely BOARD-GATED on the real pick
    path (`_ready_lane()` branches on `bool(value)`), so doctor must report BOARD-GATED too, not
    fall back to LABEL-ONLY -- which is what the strict identity check wrongly does."""
    d = _doc()
    src = _sources()
    for i, enabled_value in enumerate((1, "yes", [1], {"x": 1})):
        cfg = {"discovery": {"source": "github",
                              "github": {"project": {"enabled": enabled_value,
                                                      "queue_source": "status"}}}}
        real = src.GitHubSource(cfg)
        assert real.project_enabled is True, enabled_value    # sanity: the real gate reads it as on

        base = _sdlc(tmp_path / f"truthy{i}", cfg)
        state = _qs_features(base, run=_ready_board_run)[_QS_NAME]
        assert "BOARD-GATED" in state, (enabled_value, state)


def test_check_never_flags_queue_source_label_as_a_pass_fail_gap(tmp_path):
    # Deliberate: queue_source is frequently a correct, intentional choice -- not something with a
    # one-line "fix". check()'s pass/fail list must stay free of it; features() is where it lives.
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github",
                                          "github": {"repo": "acme/widget",
                                                     "project": {"enabled": True,
                                                                 "queue_source": "label"}}}})
    checks = d.check(base, run=_runner(gh_auth="Logged in. project"))
    assert _QS_NAME not in {c["name"] for c in checks}


# --- standing-doc hygiene: the mechanical half of context maintenance -------------------------
# Rot that a script can settle (a reference that no longer resolves), NOT the judgment half
# (demoting a rule CI now enforces) — that's sigma-retro's, because it changes files.

def _hyg(d, project_md=None, north_star=None):
    base = pathlib.Path(d) / ".sdlc"
    (base / "context").mkdir(parents=True)
    (base / "config.json").write_text("{}")
    if project_md is not None:
        (base / "project.md").write_text(project_md)
    if north_star is not None:
        (base / "context" / "north-star.md").write_text(north_star)
    return str(base)


def test_hygiene_is_silent_without_standing_docs():
    """A drop-in project has nothing to rot — it must not gain a new nag."""
    with tempfile.TemporaryDirectory() as d:
        assert _doc().hygiene(_hyg(d), d) == []


def test_hygiene_flags_a_cited_path_that_no_longer_exists():
    with tempfile.TemporaryDirectory() as d:
        (pathlib.Path(d) / "src").mkdir()
        (pathlib.Path(d) / "src" / "live.py").write_text("x = 1")
        sdlc = _hyg(d, project_md="Entry point is `src/live.py`; config in `src/gone.py`.")
        rows = {c["name"]: c for c in _doc().hygiene(sdlc, d)}
        paths = rows["standing docs: cited paths resolve"]
        assert not paths["ok"]
        assert "src/gone.py" in paths["fix"] and "src/live.py" not in paths["fix"]


def test_hygiene_ignores_patterns_and_urls():
    """Globs and <placeholders> are patterns, not references. A check that cries wolf gets
    ignored along with its true positives."""
    with tempfile.TemporaryDirectory() as d:
        sdlc = _hyg(d, project_md=(
            "Goals live in `.sdlc/goals/NNNN-*.md`, docs at `https://example.com/a/b`, "
            "research in `.sdlc/research/<slug>.md`."))
        assert all(c["ok"] for c in _doc().hygiene(sdlc, d))


# --- #1210: a slash-command mention is not a file-path citation --------------------------------
# `_CITED` matches any backticked token with a `/` in it, and a standing doc naming the very
# commands that operate on it (`/sigma-doctor`, `/sigma-loop`) satisfies that trivially. Resolved as
# repo-root-relative (the leading-slash convention #545 established), `/sigma-doctor` is checked
# against `<repo>/sigma-doctor`, which never exists, so the best-written docs failed the check that
# exists to validate them.

def test_hygiene_does_not_flag_a_known_slash_command_reference():
    """A doc naming the shipped commands that operate on it must not read as stale -- none of
    these can ever resolve as a repo-root-relative file, by design."""
    with tempfile.TemporaryDirectory() as d:
        sdlc = _hyg(d, project_md=(
            "Run `/sigma-doctor` for a check-up, `/sigma-loop` to drain the backlog, "
            "or `/sigma-init` to scaffold a new project."))
        rows = {c["name"]: c for c in _doc().hygiene(sdlc, d)}
        assert rows["standing docs: cited paths resolve"]["ok"]


def test_hygiene_still_flags_a_slash_reference_that_names_no_real_command():
    """The suppression is narrow: a single-segment leading-slash reference that does NOT name a
    shipped skill is an ordinary (bogus) repo-root-relative citation and must still be flagged --
    no over-suppression just because it LOOKS like a command."""
    with tempfile.TemporaryDirectory() as d:
        sdlc = _hyg(d, project_md="See `/sigma-doctor` and `/this-does-not-exist` for details.")
        rows = {c["name"]: c for c in _doc().hygiene(sdlc, d)}
        paths = rows["standing docs: cited paths resolve"]
        assert not paths["ok"]
        assert "/this-does-not-exist" in paths["fix"] and "/sigma-doctor" not in paths["fix"]


def test_hygiene_command_suppression_does_not_swallow_a_real_multi_segment_path():
    """A reference that merely STARTS with a command name but names a deeper path is a real file
    citation, not a command mention, and must still resolve normally."""
    with tempfile.TemporaryDirectory() as d:
        sdlc = _hyg(d, project_md="Source at `/sigma-doctor/scripts/doctor.py`.")
        rows = {c["name"]: c for c in _doc().hygiene(sdlc, d)}
        paths = rows["standing docs: cited paths resolve"]
        assert not paths["ok"]
        assert "/sigma-doctor/scripts/doctor.py" in paths["fix"]


def test_hygiene_bare_relative_citation_still_flags_with_a_repo_root_explanation():
    """AC4/#1210: a `.sdlc/project.md` citing `goals/` (meaning its own sibling `.sdlc/goals/`)
    still fails to resolve -- `_stale_paths` anchors every cited path at the repo root, same as
    `/sigma-doctor` would if it weren't a known command, NOT at the citing document's own directory
    (that would be `_dangling_links`' convention, and flipping `_stale_paths` to match it would
    break the repo-root convention every OTHER citation in this codebase already relies on --
    `src/live.py` in the fixture above, `skills/sigma-doctor/scripts/doctor.py` throughout this
    file's own comments). The decision made for AC4 is the OTHER branch it offers: the reference
    stays flagged, but the fix line now says outright that cited paths resolve from the repo root,
    so the operator does not have to read `_under`'s docstring to know that `goals/` needs to be
    written `.sdlc/goals/` (or dropped)."""
    with tempfile.TemporaryDirectory() as d:
        sdlc = _hyg(d, project_md="Goals: `goals/`.")
        rows = {c["name"]: c for c in _doc().hygiene(sdlc, d)}
        paths = rows["standing docs: cited paths resolve"]
        assert not paths["ok"]
        assert "goals/" in paths["fix"]
        assert "repo root" in paths["fix"].lower()


def test_hygiene_flags_a_dangling_relative_link_but_not_external_ones():
    with tempfile.TemporaryDirectory() as d:
        sdlc = _hyg(d, north_star=(
            "See [gone](./missing.md), [site](https://example.com), [here](#anchor)."))
        rows = {c["name"]: c for c in _doc().hygiene(sdlc, d)}
        links = rows["standing docs: links resolve"]
        assert not links["ok"]
        assert "./missing.md" in links["fix"]
        assert "example.com" not in links["fix"] and "#anchor" not in links["fix"]


def test_hygiene_caps_the_offender_list():
    """A wall of paths is a report nobody reads."""
    with tempfile.TemporaryDirectory() as d:
        cited = " ".join(f"`src/gone{i}.py`" for i in range(7))
        rows = {c["name"]: c for c in _doc().hygiene(_hyg(d, project_md=cited), d)}
        fix = rows["standing docs: cited paths resolve"]["fix"]
        assert "+4 more" in fix and fix.count("src/gone") == 3


# --- #545: a leading "/" is repo-root-relative, never the OS filesystem root -------------------
# `repo_root / ref` and `doc_dir / target` DISCARD the left operand when the right side is absolute
# (pathlib semantics), so both scanners silently resolved a `/...` reference against the machine's
# own filesystem. That broke the check in both directions at once, and the second is the nastier
# one: the answer depended on what happened to exist on whichever box ran doctor.

def test_hygiene_treats_a_leading_slash_as_repo_root_relative():
    """`/docs/architecture.md` is the ordinary repo-root-relative citation form. It was checked
    against the OS root, found nothing there, and reported STALE for a file sitting right in the
    repo — the false positive that gets a whole advisory check tuned out."""
    with tempfile.TemporaryDirectory() as d:
        (pathlib.Path(d) / "docs").mkdir()
        (pathlib.Path(d) / "docs" / "architecture.md").write_text("# arch")
        sdlc = _hyg(d, project_md="Architecture: `/docs/architecture.md`.",
                    north_star="See [arch](/docs/architecture.md).")
        rows = {c["name"]: c for c in _doc().hygiene(sdlc, d)}
        cited, links = rows["standing docs: cited paths resolve"], rows["standing docs: links resolve"]
        assert cited["ok"], cited["fix"]
        assert links["ok"], links["fix"]


def test_hygiene_flags_an_absolute_reference_that_lives_outside_the_repo():
    """The machine-dependent false OK: an absolute path that EXISTS on this box but is nowhere in
    the repo used to pass silently. The fixture plants a real file OUTSIDE the repo root and cites
    it by absolute path, so this proves the resolution target itself rather than depending on
    whichever system paths happen to exist wherever CI runs."""
    with tempfile.TemporaryDirectory() as d:
        outside = pathlib.Path(d) / "outside.md"
        outside.write_text("# not in the repo")
        repo = pathlib.Path(d) / "repo"
        repo.mkdir()
        assert outside.is_absolute() and outside.exists()      # the precondition the bug relied on
        sdlc = _hyg(str(repo), project_md=f"Spec: `{outside}`.",
                    north_star=f"See [spec]({outside}).")
        rows = {c["name"]: c for c in _doc().hygiene(sdlc, str(repo))}
        assert not rows["standing docs: cited paths resolve"]["ok"]
        assert not rows["standing docs: links resolve"]["ok"]


def test_check_surfaces_rot_without_scoring_it_as_setup(capsys):
    """Setup readiness and content rot are different questions: the rot must show up in a `check`
    run (or nobody runs it) but must NOT move the N/M ready score."""
    with tempfile.TemporaryDirectory() as d:
        sdlc = _hyg(d, project_md="Broken ref to `src/gone.py`.")
        m = _doc()
        m._real_run = lambda args: ""                 # hermetic: no real gh / claude probes
        n = len(m.check(sdlc, run=_runner()))
        m.main(["doctor.py", "check", sdlc])
        out = capsys.readouterr().out
        assert "standing-doc hygiene" in out and "src/gone.py" in out
        assert f"{n}/{n} ready" in out                # rot did NOT become a failed setup check


_BOARD_FIELDS = {"fields": [
    {"id": "F_status", "name": "Status", "options": [{"id": "s1", "name": "Todo"}]},
    {"id": "F_pri", "name": "Priority", "options": [{"id": "p1", "name": "High"}]},
    {"id": "F_sec", "name": "Section", "options": [{"id": "x1", "name": "Task"}]},
    {"id": "F_due", "name": "Due date", "type": "ProjectV2Field"},   # text/date -> no options -> not flaggable
]}


def test_unmapped_board_fields_flags_only_unmapped_single_selects():
    import json as _json
    d = _doc()
    run = (lambda a: _json.dumps(_BOARD_FIELDS) if a[:3] == ["gh", "project", "field-list"] else "")
    cfg = {"repo": "acme/widget", "project": {"enabled": True, "number": 8}}
    # Status (driven) is excluded; the two custom single-selects are flagged; the date field isn't
    assert d._unmapped_board_fields(cfg, run) == ["Priority", "Section"]
    # mapping one leaves only the other
    cfg["project"]["custom_fields"] = {"Priority": "High"}
    assert d._unmapped_board_fields(cfg, run) == ["Section"]
    # a custom status_field name is the one excluded instead of "Status"
    cfg2 = {"repo": "acme/widget", "project": {"enabled": True, "number": 8, "status_field": "Priority"}}
    assert "Priority" not in d._unmapped_board_fields(cfg2, run)


def test_doctor_output_survives_a_non_utf8_locale(tmp_path):
    """Windows cp1252 / C locale: the em-dash-heavy dashboard must not crash with UnicodeEncodeError
    or garble to '?'. The UTF-8 output guard reconfigures the streams so non-ASCII is emitted cleanly."""
    import subprocess, os, sys, json, pathlib as _pl
    base = tmp_path / ".sdlc"; base.mkdir()
    base.joinpath("config.json").write_text(json.dumps({}))   # default features incl. an em-dash row
    D = _pl.Path(__file__).resolve().parent.parent / "skills" / "sigma-doctor" / "scripts" / "doctor.py"
    env = dict(os.environ, LC_ALL="C", LANG="C", PYTHONIOENCODING="ascii")
    p = subprocess.run([sys.executable, str(D), "features", str(base)],
                       capture_output=True, text=True, env=env)
    assert p.returncode == 0, p.stderr                        # no UnicodeEncodeError crash
    assert "per-goal worktree + PR" in p.stdout               # output actually came through


def test_board_dup_risk_flags_unpinned_number_with_existing_boards():
    import json as _json
    d = _doc()
    boards = {"projects": [{"number": 7, "title": "Acme Delivery Board"}]}
    run = (lambda a: _json.dumps(boards) if a[:3] == ["gh", "project", "list"] else "")
    # no number pinned + owner already has a board => duplicate-board risk
    cfg = {"repo": "acme/widget", "project": {"enabled": True, "owner": "acme"}}
    fix = d._board_dup_risk(cfg, run)
    assert fix and "project.number" in fix and "Acme Delivery Board" in fix
    # pinning a number removes the risk (resolves directly, no create path)
    cfg["project"]["number"] = 7
    assert d._board_dup_risk(cfg, run) is None
    # owner with ZERO boards => a fresh create is safe, not flagged
    run0 = (lambda a: _json.dumps({"projects": []}) if a[:3] == ["gh", "project", "list"] else "")
    assert d._board_dup_risk({"repo": "acme/widget", "project": {"enabled": True}}, run0) is None
    # a board already titled sigma's default `<repo> — SDLC` resolves by title => NO false alarm
    match = (lambda a: _json.dumps({"projects": [{"number": 3, "title": "widget — SDLC"}]})
             if a[:3] == ["gh", "project", "list"] else "")
    assert d._board_dup_risk({"repo": "acme/widget", "project": {"enabled": True}}, match) is None
    # can't read the board list => None (no false alarm)
    assert d._board_dup_risk({"repo": "acme/widget", "project": {"enabled": True}}, lambda a: "") is None


def test_self_merge_risk_flags_the_full_hazardous_combination():
    """#821: auto_merge always + require_review approval + no branch protection = nothing
    independent ever stands between an unattended merge and the base."""
    d = _doc()
    wk = {"enabled": True, "auto_merge": "always", "require_review": "approval"}
    gh_cfg = {"repo": "acme/widget"}

    def run(a):
        if a[:2] == ["gh", "api"] and a[2] == "repos/acme/widget":
            return '{"id": 1}'                                    # canary: repo IS reachable
        if a[:2] == ["gh", "api"] and "branches/main/protection" in a[2]:
            return ""                                              # genuine 404: NOT protected
        return ""
    fix = d._self_merge_risk(gh_cfg, wk, run)
    assert fix and "auto_merge" in fix and "approval" in fix and "'main'" in fix


def test_self_merge_risk_clears_when_branch_protection_exists():
    d = _doc()
    wk = {"enabled": True, "auto_merge": "always", "require_review": "approval"}
    gh_cfg = {"repo": "acme/widget"}

    def run(a):
        if a[:2] == ["gh", "api"] and a[2] == "repos/acme/widget":
            return '{"id": 1}'
        if a[:2] == ["gh", "api"] and "branches/main/protection" in a[2]:
            return "https://api.github.com/repos/acme/widget/branches/main/protection"
        return ""
    assert d._self_merge_risk(gh_cfg, wk, run) is None


def test_self_merge_risk_does_not_cry_wolf_when_the_repo_is_unreachable():
    """The protection endpoint and a genuine reachability failure both come back empty through
    `_real_run`'s success-only contract -- without the canary, this would misreport "no branch
    protection" on every auth/network blip. The canary failing must produce no verdict at all."""
    d = _doc()
    wk = {"enabled": True, "auto_merge": "always", "require_review": "approval"}
    assert d._self_merge_risk({"repo": "acme/widget"}, wk, lambda a: "") is None


#: Second-round review of #821 caught two tests below sharing a mock truthy for BOTH the canary
#: AND the protection-check call -- so removing the guard each test claims to cover still hit the
#: "genuinely protected" short-circuit and returned None for the WRONG reason, undetected. This
#: mock matches the ACTUAL hazard shape (canary reachable, protection empty) instead, so a removed
#: early-exit guard falls through to a real, non-None hazard string and the test genuinely fails.
def _unprotected_but_reachable(a):
    if a[:2] == ["gh", "api"] and len(a) > 2 and a[2] == "repos/acme/widget":
        return '{"id": 1}'
    return ""


def test_self_merge_risk_ignores_changes_mode():
    """require_review: "changes" never requires a comment to merge at all -- only the ABSENCE of a
    block -- so there is no false-approval illusion this check exists to catch."""
    d = _doc()
    wk = {"enabled": True, "auto_merge": "always", "require_review": "changes"}
    assert d._self_merge_risk({"repo": "acme/widget"}, wk, _unprotected_but_reachable) is None


def test_self_merge_risk_off_when_work_disabled_or_auto_merge_not_always():
    d = _doc()
    gh_cfg = {"repo": "acme/widget"}
    assert d._self_merge_risk(gh_cfg, {"enabled": False, "auto_merge": "always",
                                       "require_review": "approval"}, _unprotected_but_reachable) is None
    assert d._self_merge_risk(gh_cfg, {"enabled": True, "auto_merge": "protected",
                                       "require_review": "approval"}, _unprotected_but_reachable) is None


def test_check_surfaces_self_merge_risk(tmp_path):
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}},
                            "work": {"enabled": True, "auto_merge": "always",
                                    "require_review": "approval"}})

    def run(a):
        if a[:3] == ["gh", "auth", "status"]:
            return "Logged in ... token scopes: project"
        if a[:2] == ["gh", "api"] and a[2] == "repos/acme/widget":
            return '{"id": 1}'
        if a[:2] == ["gh", "api"] and "protection" in a[2]:
            return ""
        return ""
    checks = {c["name"]: c for c in d.check(base, run=run)}
    assert checks["an approval from someone other than the author is required before auto-merge"]["ok"] is False


def test_check_surfaces_board_dup_risk(tmp_path):
    import json as _json
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github",
                 "github": {"repo": "acme/widget", "project": {"enabled": True, "owner": "acme"}}}})

    def run(a):
        if a[:3] == ["gh", "auth", "status"]:
            return "Logged in ... token scopes: project"
        if a[:3] == ["gh", "project", "list"]:
            return _json.dumps({"projects": [{"number": 7, "title": "Acme Delivery Board"}]})
        return ""
    checks = {c["name"]: c for c in d.check(base, run=run)}
    assert checks["project.number pinned (no duplicate-board risk)"]["ok"] is False


#: Row name for #2452's check, spelled once here and reused by every test below (and by `check()`
#: itself) -- no module constant per plan §0.2, this is just the test file's own de-dup.
_STRAY_ROW = "checked-out branch has no stray commits past its own PR's merge/close"


def test_stray_commits_after_merge_flags_the_real_incident_shape():
    """#2452's real incident shape: PR #3604 (on a downstream deployment of this plugin)
    squash-merged, the checkout never left that branch, and commits kept landing on it afterward.
    `headRefOid` is the anchor (not the merge commit -- see the function's own docstring for why),
    so `rev-list --count <headRefOid>..HEAD` is exactly the stray count."""
    d = _doc()
    gh_cfg = {"repo": "acme/widget"}

    def run(a):
        if a[:5] == ["git", "-C", "/repo", "branch", "--show-current"]:
            return "sdlc/2450\n"
        if a[:2] == ["gh", "api"] and a[2] == "repos/acme/widget":
            return "main\n"
        if a[:3] == ["gh", "pr", "view"]:
            assert a[3] == "sdlc/2450"
            return json.dumps({"state": "MERGED", "headRefOid": "abc123", "number": 3604,
                                "mergedAt": "2026-09-08T11:51:30Z", "closedAt": None,
                                "headRefName": "sdlc/2450"})
        if a[:4] == ["git", "-C", "/repo", "rev-list"]:
            return "5\n"
        return ""

    fix = d._stray_commits_after_merge(gh_cfg, "/repo", run)
    assert fix
    for needle in ("sdlc/2450", "#3604", "5", "main", "already merged"):
        assert needle in fix, (needle, fix)


def test_stray_commits_after_merge_quiet_when_pr_still_open():
    d = _doc()
    gh_cfg = {"repo": "acme/widget"}

    def run(a):
        if a[:5] == ["git", "-C", "/repo", "branch", "--show-current"]:
            return "sdlc/2450\n"
        if a[:2] == ["gh", "api"] and a[2] == "repos/acme/widget":
            return "main\n"
        if a[:3] == ["gh", "pr", "view"]:
            return json.dumps({"state": "OPEN", "headRefOid": "abc123", "number": 3604})
        if a[:4] == ["git", "-C", "/repo", "rev-list"]:
            return "5\n"
        return ""

    assert d._stray_commits_after_merge(gh_cfg, "/repo", run) is None


def test_stray_commits_after_merge_quiet_when_no_pr_for_branch():
    """`gh pr view` for a branch with no PR exits 1 ("no pull requests found for branch ..."),
    which collapses to falsy through `_real_run`'s contract -- no special-casing needed here."""
    d = _doc()
    gh_cfg = {"repo": "acme/widget"}

    def run(a):
        if a[:5] == ["git", "-C", "/repo", "branch", "--show-current"]:
            return "sdlc/2450\n"
        if a[:2] == ["gh", "api"] and a[2] == "repos/acme/widget":
            return "main\n"
        return ""   # gh pr view -> no PR for this branch

    assert d._stray_commits_after_merge(gh_cfg, "/repo", run) is None


def test_stray_commits_after_merge_quiet_on_the_default_branch():
    """The healthy resting state: current branch already IS the default. No PR lookup is even
    attempted -- a `run` that would raise on one proves it, the way this file has no existing
    'assert not called' idiom to reach for."""
    d = _doc()
    gh_cfg = {"repo": "acme/widget"}

    def run(a):
        if a[:5] == ["git", "-C", "/repo", "branch", "--show-current"]:
            return "main\n"
        if a[:2] == ["gh", "api"] and a[2] == "repos/acme/widget":
            return "main\n"
        if a[:3] == ["gh", "pr", "view"]:
            raise AssertionError(f"must not look up a PR while already on the default branch: {a}")
        return ""

    assert d._stray_commits_after_merge(gh_cfg, "/repo", run) is None


def test_stray_commits_after_merge_quiet_when_head_ref_oid_is_head():
    """`headRefOid == HEAD` (stray count 0) is the correct state right after a merge/close with
    nothing further committed -- not a risk."""
    d = _doc()
    gh_cfg = {"repo": "acme/widget"}

    def run(a):
        if a[:5] == ["git", "-C", "/repo", "branch", "--show-current"]:
            return "sdlc/2450\n"
        if a[:2] == ["gh", "api"] and a[2] == "repos/acme/widget":
            return "main\n"
        if a[:3] == ["gh", "pr", "view"]:
            return json.dumps({"state": "MERGED", "headRefOid": "abc123", "number": 3604,
                                "mergedAt": "2026-09-08T11:51:30Z"})
        if a[:4] == ["git", "-C", "/repo", "rev-list"]:
            return "0\n"
        return ""

    assert d._stray_commits_after_merge(gh_cfg, "/repo", run) is None


def test_stray_commits_after_merge_does_not_cry_wolf_when_unreachable_or_unconfigured():
    """Two can't-tell shapes in one test, mirroring `_self_merge_risk`'s own combined-gate style:
    (a) no `discovery.github.repo` configured at all -- nothing to query, `run` must never even be
    called; (b) a repo configured but the default-branch canary comes back empty -- can't reach the
    repo/auth path, must never read as a false alarm."""
    d = _doc()

    def raises(a):
        raise AssertionError(f"must not call run with no repo configured: {a}")

    assert d._stray_commits_after_merge({}, "/repo", raises) is None
    assert d._stray_commits_after_merge({"repo": "acme/widget"}, "/repo", lambda a: "") is None


def test_stray_commits_after_merge_quiet_on_detached_head():
    """`git branch --show-current` on a detached HEAD exits 0 and prints nothing (live-verified) --
    indistinguishable here from git itself being unreachable, and both collapse to the same
    fail-open reading: no branch to look a PR up by. No `gh` call should even be attempted."""
    d = _doc()

    def run(a):
        if a[:5] == ["git", "-C", "/repo", "branch", "--show-current"]:
            return ""
        if a and a[0] == "gh":
            raise AssertionError(f"must not call gh with no branch to look a PR up by: {a}")
        return ""

    assert d._stray_commits_after_merge({"repo": "acme/widget"}, "/repo", run) is None


def test_stray_commits_after_merge_quiet_when_rev_list_fails():
    """A `headRefOid` unreachable locally (shallow clone, rewritten history) makes `git rev-list
    --count` fail -- `_RawFailure`, same falsy-through-`_real_run` contract every other check here
    relies on. Can't compute a count is not the same as zero, and must not misreport as either."""
    d = _doc()
    gh_cfg = {"repo": "acme/widget"}

    def run(a):
        if a[:5] == ["git", "-C", "/repo", "branch", "--show-current"]:
            return "sdlc/2450\n"
        if a[:2] == ["gh", "api"] and a[2] == "repos/acme/widget":
            return "main\n"
        if a[:3] == ["gh", "pr", "view"]:
            return json.dumps({"state": "MERGED", "headRefOid": "deadbeef", "number": 3604,
                                "mergedAt": "2026-09-08T11:51:30Z"})
        if a[:4] == ["git", "-C", "/repo", "rev-list"]:
            return d._RawFailure("fatal: bad revision 'deadbeef..HEAD'")
        return ""

    assert d._stray_commits_after_merge(gh_cfg, "/repo", run) is None


def test_check_surfaces_stray_commits_after_merge(tmp_path):
    """Integration, mirroring `test_check_surfaces_self_merge_risk`. Deliberately NO `work` block in
    config -- confirms this row fires independent of `work.enabled`, unlike `_self_merge_risk`."""
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}})
    repo_root = str(tmp_path)

    def run(a):
        if a[:3] == ["gh", "auth", "status"]:
            return "Logged in ... token scopes: project"
        if a[:5] == ["git", "-C", repo_root, "branch", "--show-current"]:
            return "sdlc/2450\n"
        if a[:2] == ["gh", "api"] and a[2] == "repos/acme/widget":
            return "main\n"
        if a[:3] == ["gh", "pr", "view"]:
            return json.dumps({"state": "MERGED", "headRefOid": "abc123", "number": 3604,
                                "mergedAt": "2026-09-08T11:51:30Z"})
        if a[:4] == ["git", "-C", repo_root, "rev-list"]:
            return "3\n"
        return ""

    checks = {c["name"]: c for c in d.check(base, run=run)}
    row = checks[_STRAY_ROW]
    assert row["ok"] is False
    assert "sdlc/2450" in row["fix"]


def test_real_git_stray_commits_after_merge_flags_it_then_switching_branch_clears_it(tmp_path):
    """Per AGENTS.md's "run the control, or the check is decoration": a real `tmp_path` git repo,
    real commits, real `git branch --show-current` / `git rev-list --count`. Only `gh` is faked.
    Mirrors `test_real_git_the_doctor_row_flags_an_unignored_dotenv_then_the_ignore_clears_it`'s
    real-git idiom and `_secret_runner`'s `real=True` split between real probes and faked ones.
    Catches a reversed `..` operand or a `-C` path bug a fully-mocked suite would let through
    silently."""
    import subprocess
    d = _doc()
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(repo)] + list(args), check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (repo / "f.txt").write_text("1\n")
    git("add", "f.txt")
    git("commit", "-q", "-m", "C1")
    git("checkout", "-q", "-b", "feature-x")
    (repo / "f.txt").write_text("2\n")
    git("commit", "-q", "-am", "C2")
    c2 = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         check=True, capture_output=True, text=True).stdout.strip()
    git("checkout", "-q", "main")
    (repo / "g.txt").write_text("3\n")
    git("add", "g.txt")
    git("commit", "-q", "-m", "C3")   # stands in for the squash-merge commit; no ancestry to feature-x
    git("checkout", "-q", "feature-x")   # mirrors "the main checkout stayed on that branch"
    (repo / "f.txt").write_text("4\n")
    git("commit", "-q", "-am", "C4")   # the stray commit

    gh_cfg = {"repo": "acme/widget"}

    def run(a):
        if a and a[0] == "git":
            return d._real_run(a)
        if a[:2] == ["gh", "api"]:
            return "main\n"
        if a[:3] == ["gh", "pr", "view"]:
            return json.dumps({"state": "MERGED", "headRefOid": c2, "number": 42,
                                "mergedAt": "2026-09-08T00:00:00Z", "closedAt": None,
                                "headRefName": "feature-x"})
        return ""

    fix = d._stray_commits_after_merge(gh_cfg, str(repo), run)
    assert fix and "feature-x" in fix and "#42" in fix and "1 commit" in fix

    git("checkout", "-q", "main")
    assert d._stray_commits_after_merge(gh_cfg, str(repo), run) is None


def test_stray_commits_after_merge_flags_the_closed_without_merging_shape():
    """Mirrors `..._flags_the_real_incident_shape` but the PR was CLOSED, never merged -- the fix
    string must carry the CLOSED-specific advice ("closed without merging", "open a new PR") and
    NOT the MERGED wording ("already merged", "that work already landed via the PR")."""
    d = _doc()
    gh_cfg = {"repo": "acme/widget"}

    def run(a):
        if a[:5] == ["git", "-C", "/repo", "branch", "--show-current"]:
            return "sdlc/2450\n"
        if a[:2] == ["gh", "api"] and a[2] == "repos/acme/widget":
            return "main\n"
        if a[:3] == ["gh", "pr", "view"]:
            assert a[3] == "sdlc/2450"
            return json.dumps({"state": "CLOSED", "headRefOid": "abc123", "number": 3604,
                                "mergedAt": None, "closedAt": "2026-09-08T11:51:30Z",
                                "headRefName": "sdlc/2450"})
        if a[:4] == ["git", "-C", "/repo", "rev-list"]:
            return "5\n"
        return ""

    fix = d._stray_commits_after_merge(gh_cfg, "/repo", run)
    assert fix
    for needle in ("sdlc/2450", "#3604", "5", "main", "closed without merging", "open a new PR"):
        assert needle in fix, (needle, fix)
    assert "already merged" not in fix
    assert "that work already landed via the PR" not in fix


def test_stray_commits_after_merge_quiet_when_default_branch_lookup_fails():
    """The default-branch canary (`gh api repos/<repo> --jq .default_branch`) coming back empty --
    can't reach the repo/auth path at all -- is a DIFFERENT can't-tell shape than an empty current
    branch (covered by the detached-HEAD test above): here `git branch --show-current` succeeds
    with a real branch, only the `gh api` call fails. `gh pr view` must never even be attempted."""
    d = _doc()
    gh_cfg = {"repo": "acme/widget"}

    def run(a):
        if a[:5] == ["git", "-C", "/repo", "branch", "--show-current"]:
            return "sdlc/2450\n"
        if a[:2] == ["gh", "api"] and a[2] == "repos/acme/widget":
            return ""
        if a[:3] == ["gh", "pr", "view"]:
            raise AssertionError(f"must not look up a PR with no default branch to compare against: {a}")
        return ""

    assert d._stray_commits_after_merge(gh_cfg, "/repo", run) is None


def test_stray_commits_after_merge_quiet_when_pr_json_is_malformed():
    """`gh pr view --json ...` returning something that isn't valid JSON must not raise out of
    `_stray_commits_after_merge` -- the `json.loads` `except Exception` path, fail-open like every
    other can't-tell shape here."""
    d = _doc()
    gh_cfg = {"repo": "acme/widget"}

    def run(a):
        if a[:5] == ["git", "-C", "/repo", "branch", "--show-current"]:
            return "sdlc/2450\n"
        if a[:2] == ["gh", "api"] and a[2] == "repos/acme/widget":
            return "main\n"
        if a[:3] == ["gh", "pr", "view"]:
            return "not valid json {{{"
        if a[:4] == ["git", "-C", "/repo", "rev-list"]:
            raise AssertionError(f"must not compute a stray count from unparseable PR JSON: {a}")
        return ""

    assert d._stray_commits_after_merge(gh_cfg, "/repo", run) is None


def test_stray_commits_after_merge_quiet_when_head_ref_oid_missing():
    """An otherwise-valid MERGED PR JSON with no `headRefOid` key at all -- defensive: no anchor to
    compute a stray count from, so `git rev-list` must never even be called."""
    d = _doc()
    gh_cfg = {"repo": "acme/widget"}

    def run(a):
        if a[:5] == ["git", "-C", "/repo", "branch", "--show-current"]:
            return "sdlc/2450\n"
        if a[:2] == ["gh", "api"] and a[2] == "repos/acme/widget":
            return "main\n"
        if a[:3] == ["gh", "pr", "view"]:
            return json.dumps({"state": "MERGED", "number": 3604,
                                "mergedAt": "2026-09-08T11:51:30Z"})
        if a[:4] == ["git", "-C", "/repo", "rev-list"]:
            raise AssertionError(f"must not compute a stray count with no headRefOid anchor: {a}")
        return ""

    assert d._stray_commits_after_merge(gh_cfg, "/repo", run) is None


def test_stray_commits_after_merge_quiet_when_rev_list_count_is_not_numeric():
    """`git rev-list --count ...` is expected to print a bare integer; if it ever prints something
    `int()` can't parse, that must read as can't-tell (None), not crash and not a false alarm."""
    d = _doc()
    gh_cfg = {"repo": "acme/widget"}

    def run(a):
        if a[:5] == ["git", "-C", "/repo", "branch", "--show-current"]:
            return "sdlc/2450\n"
        if a[:2] == ["gh", "api"] and a[2] == "repos/acme/widget":
            return "main\n"
        if a[:3] == ["gh", "pr", "view"]:
            return json.dumps({"state": "MERGED", "headRefOid": "abc123", "number": 3604,
                                "mergedAt": "2026-09-08T11:51:30Z"})
        if a[:4] == ["git", "-C", "/repo", "rev-list"]:
            return "not-a-number\n"
        return ""

    assert d._stray_commits_after_merge(gh_cfg, "/repo", run) is None


# --- #2452 follow-up: a registered, OPEN feature/<name> unit branch is exempt ------------------
# Independent review on PR #2461 blocked the original #2452 check with a real, reproduced false
# positive: `feature/dangling-completion`'s own completion PR (#2379) merged, and the branch then
# kept collecting ordinary, independently-reviewed goal commits afterward -- exactly what
# docs/branching-model.md §13/§13b says a `feature/<name>` unit branch is supposed to do. These
# tests pin the fix: `_open_unit_branch` (used by `_stray_commits_after_merge` via its new
# `sdlc_dir` parameter) must exempt such a branch, and ONLY such a branch.


def test_the_unit_branch_prefix_matches_the_loops_own():
    """`_UNIT_BRANCH_PREFIX` is a deliberate second copy of `features.BRANCH_PREFIX` (doctor.py has
    no cross-skill import by design -- see `_enforce_enabled`'s docstring) -- pinned so the two
    cannot silently drift, the same discipline `test_the_managed_settings_filename_matches_the_
    loops_own` already applies to `_MANAGED_SETTINGS_FILE`."""
    d = _doc()
    features = _load_loop_script_for_test("features")
    assert d._UNIT_BRANCH_PREFIX == features.BRANCH_PREFIX


def test_open_unit_branch_true_for_a_registered_open_unit(tmp_path):
    d = _doc()
    base = _sdlc(tmp_path, {})
    _adopt_unit(base, "widget")
    assert d._open_unit_branch(base, "feature/widget") is True


def test_open_unit_branch_false_for_a_closed_unit(tmp_path):
    """A CLOSED unit's branch is exactly the abandoned-checkout shape `_stray_commits_after_merge`
    exists to catch -- `feature_registry.resolve_open_unit` already answers None for one by design,
    and this helper must not override that."""
    d = _doc()
    base = _sdlc(tmp_path, {})
    _adopt_unit(base, "widget", open_=False)
    assert d._open_unit_branch(base, "feature/widget") is False


def test_open_unit_branch_false_for_an_unregistered_feature_branch(tmp_path):
    """A repo with NO `.sdlc/features/` at all -- most adopters -- is completely unaffected: a
    `feature/<name>`-shaped branch with nothing registered reads exactly as it did before this
    fix."""
    d = _doc()
    base = _sdlc(tmp_path, {})
    assert d._open_unit_branch(base, "feature/widget") is False


def test_open_unit_branch_false_for_a_non_unit_shaped_branch(tmp_path):
    d = _doc()
    base = _sdlc(tmp_path, {})
    _adopt_unit(base, "widget")
    assert d._open_unit_branch(base, "sdlc/2450") is False


def test_open_unit_branch_false_with_no_sdlc_dir():
    d = _doc()
    assert d._open_unit_branch(None, "feature/widget") is False


def test_open_unit_branch_false_for_a_units_own_sub_branch(tmp_path):
    """`feature/<unit>/<sub>` is the model's own SUB-branch shape (`docs/branching-model.md`), a
    different thing from the unit's own base branch -- `<unit>/<sub>` is not a legal unit name (a
    `/` is excluded, see `features._is_unit_name`), so this must read exactly like an unregistered
    branch, not silently match unit "widget"."""
    d = _doc()
    base = _sdlc(tmp_path, {})
    _adopt_unit(base, "widget")
    assert d._open_unit_branch(base, "feature/widget/sub") is False


def test_stray_commits_after_merge_quiet_when_branch_is_a_registered_open_unit_base(tmp_path):
    """The independent review's blocking finding on PR #2461, pinned as a hermetic control: a
    `feature/<name>` branch whose own historical PR merged must NOT be flagged just because commits
    landed on it afterward, as long as the branch is a registered, OPEN unit base. The fake `run`
    raises if `gh pr view` is even called -- the exemption fires BEFORE that lookup, so this also
    pins that an exempt branch costs no extra `gh` call."""
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}})
    _adopt_unit(base, "widget")
    gh_cfg = {"repo": "acme/widget"}

    def run(a):
        if a[:5] == ["git", "-C", "/repo", "branch", "--show-current"]:
            return "feature/widget\n"
        if a[:2] == ["gh", "api"] and a[2] == "repos/acme/widget":
            return "main\n"
        if a[:3] == ["gh", "pr", "view"]:
            raise AssertionError(f"must not look up a PR for an exempt unit branch: {a}")
        return ""

    assert d._stray_commits_after_merge(gh_cfg, "/repo", run, base) is None


def test_stray_commits_after_merge_still_flags_a_closed_unit_branch(tmp_path):
    """The exemption is gated on OPEN, not merely registered (see `_open_unit_branch`'s own
    docstring): a CLOSED unit's branch is exactly the abandoned-checkout shape this check exists to
    catch, so it must still be flagged exactly as before this fix."""
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}})
    _adopt_unit(base, "widget", open_=False)
    gh_cfg = {"repo": "acme/widget"}

    def run(a):
        if a[:5] == ["git", "-C", "/repo", "branch", "--show-current"]:
            return "feature/widget\n"
        if a[:2] == ["gh", "api"] and a[2] == "repos/acme/widget":
            return "main\n"
        if a[:3] == ["gh", "pr", "view"]:
            return json.dumps({"state": "MERGED", "headRefOid": "abc123", "number": 9001,
                                "mergedAt": "2026-09-08T11:51:30Z"})
        if a[:4] == ["git", "-C", "/repo", "rev-list"]:
            return "4\n"
        return ""

    fix = d._stray_commits_after_merge(gh_cfg, "/repo", run, base)
    assert fix and "feature/widget" in fix and "#9001" in fix


def test_stray_commits_after_merge_flags_unaffected_repos_with_no_feature_registry_at_all(tmp_path):
    """The requirement stated by #2452's own follow-up: a repo that never adopted `.sdlc/features/`
    (most installs) is completely unaffected by this fix -- a `feature/<name>`-shaped branch there
    still gets flagged exactly as it did before, even with a real (but empty-of-that-unit)
    `sdlc_dir` passed in."""
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}})
    gh_cfg = {"repo": "acme/widget"}

    def run(a):
        if a[:5] == ["git", "-C", "/repo", "branch", "--show-current"]:
            return "feature/widget\n"
        if a[:2] == ["gh", "api"] and a[2] == "repos/acme/widget":
            return "main\n"
        if a[:3] == ["gh", "pr", "view"]:
            return json.dumps({"state": "MERGED", "headRefOid": "abc123", "number": 9002,
                                "mergedAt": "2026-09-08T11:51:30Z"})
        if a[:4] == ["git", "-C", "/repo", "rev-list"]:
            return "2\n"
        return ""

    fix = d._stray_commits_after_merge(gh_cfg, "/repo", run, base)
    assert fix and "feature/widget" in fix and "#9002" in fix


def test_real_git_stray_commits_after_merge_exempts_the_reviewers_reproduced_scenario(tmp_path):
    """The independent review's OWN live reproduction, pinned as a real-git regression control (per
    AGENTS.md's "run the control, or the check is decoration"): a real repo, a real `feature/<name>`
    branch, a real merged-PR anchor, and REAL commits landing on that branch after the anchor -- the
    exact shape the review reproduced live against this repo's own `feature/dangling-completion`
    (its completion PR #2379 merged at `headRefOid=278bb7647c...`, with 6 real, independently-
    reviewed commits past that head today). Registering the unit as OPEN in a real
    `.sdlc/features/` must read this as clean, not stray -- and the first assertion below proves
    the scenario genuinely reproduces the false positive (this exact real-git shape, with NO
    registered unit, still gets flagged) before the second assertion proves the fix clears it."""
    import subprocess
    d = _doc()
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(repo)] + list(args), check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (repo / "f.txt").write_text("1\n")
    git("add", "f.txt")
    git("commit", "-q", "-m", "C1")
    git("checkout", "-q", "-b", "feature/widget")      # the unit's own long-lived integration branch
    (repo / "f.txt").write_text("2\n")
    git("commit", "-q", "-am", "unit work before completion")
    head_at_completion = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True).stdout.strip()
    # stands in for the squash-merge of the unit's OWN completion PR into main -- no ancestry back
    # to feature/widget, mirroring the real PR #2379 / main relationship the review reproduced.
    git("checkout", "-q", "main")
    (repo / "g.txt").write_text("3\n")
    git("add", "g.txt")
    git("commit", "-q", "-m", "completion PR landed on main")
    git("checkout", "-q", "feature/widget")
    # ordinary goal work continuing on the unit branch AFTER its completion PR merged -- exactly
    # what docs/branching-model.md §13 says is expected, not stray.
    for i, msg in enumerate(("goal A merge", "goal B merge", "goal C merge"), start=3):
        (repo / "f.txt").write_text(f"{i}\n")
        git("commit", "-q", "-am", msg)

    gh_cfg = {"repo": "acme/widget"}

    def run(a):
        if a and a[0] == "git":
            return d._real_run(a)
        if a[:2] == ["gh", "api"]:
            return "main\n"
        if a[:3] == ["gh", "pr", "view"]:
            return json.dumps({"state": "MERGED", "headRefOid": head_at_completion, "number": 2379,
                                "mergedAt": "2026-09-11T09:37:09Z", "closedAt": None,
                                "headRefName": "feature/widget"})
        return ""

    # (a) genuinely reproduces the false positive: same real-git shape, no registered unit.
    unregistered_base = _sdlc(tmp_path / "unadopted", {})
    fix_without_registry = d._stray_commits_after_merge(gh_cfg, str(repo), run, unregistered_base)
    assert fix_without_registry and "feature/widget" in fix_without_registry
    assert "3 commit" in fix_without_registry

    # (b) the fix: registered as an OPEN unit, the same real scenario reads as clean.
    base = _sdlc(tmp_path / "adopted", {"discovery": {"source": "github",
                                                        "github": {"repo": "acme/widget"}}})
    _adopt_unit(base, "widget")
    assert d._stray_commits_after_merge(gh_cfg, str(repo), run, base) is None


def test_check_does_not_surface_stray_commits_for_a_registered_open_unit_branch(tmp_path):
    """Integration, mirroring `test_check_surfaces_stray_commits_after_merge`: the doctor row must
    not appear at all when the checked-out branch is a registered, OPEN unit base."""
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}})
    _adopt_unit(base, "widget")
    repo_root = str(tmp_path)

    def run(a):
        if a[:3] == ["gh", "auth", "status"]:
            return "Logged in ... token scopes: project"
        if a[:5] == ["git", "-C", repo_root, "branch", "--show-current"]:
            return "feature/widget\n"
        if a[:2] == ["gh", "api"] and a[2] == "repos/acme/widget":
            return "main\n"
        if a[:3] == ["gh", "pr", "view"]:
            raise AssertionError(f"must not look up a PR for an exempt unit branch: {a}")
        return ""

    checks = {c["name"]: c for c in d.check(base, run=run)}
    assert _STRAY_ROW not in checks


def test_unmapped_board_fields_survives_a_malformed_custom_fields():
    """A non-dict custom_fields (e.g. a list) must NOT crash the doctor run — the helper treats it as
    'nothing mapped' and still reports the fields, staying fail-open like the rest of doctor."""
    import json as _json
    d = _doc()
    run = (lambda a: _json.dumps(_BOARD_FIELDS) if a[:3] == ["gh", "project", "field-list"] else "")
    cfg = {"repo": "acme/widget", "project": {"enabled": True, "number": 8, "custom_fields": ["Priority"]}}
    assert d._unmapped_board_fields(cfg, run) == ["Priority", "Section"]      # no crash; list => nothing mapped


def test_unmapped_board_fields_none_when_board_unreadable():
    d = _doc()
    # no project number yet -> can't enumerate -> None (never a false all-clear)
    assert d._unmapped_board_fields({"project": {"enabled": True, "owner": "acme"}}, lambda a: "") is None
    # number present but the call returns nothing (e.g. missing `project` scope) -> None
    cfg = {"repo": "acme/widget", "project": {"enabled": True, "number": 8}}
    assert d._unmapped_board_fields(cfg, lambda a: "") is None


def test_check_surfaces_unmapped_board_fields(tmp_path):
    import json as _json
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github",
                 "github": {"repo": "acme/widget", "project": {"enabled": True, "number": 8}}}})

    def run(a):
        if a[:3] == ["gh", "auth", "status"]:
            return "Logged in to github.com ... token scopes: project"
        if a[:3] == ["gh", "project", "field-list"]:
            return _json.dumps(_BOARD_FIELDS)
        return ""
    checks = {c["name"]: c for c in d.check(base, run=run)}
    assert checks["board custom fields mapped"]["ok"] is False
    assert "Priority" in checks["board custom fields mapped"]["fix"] and "Section" in checks["board custom fields mapped"]["fix"]


def test_features_reports_independent_review_states(tmp_path):
    d = _doc()
    row = "independent review (advisory; the code cannot prove who reviewed)"
    # default (no block) reads as ON — separation is the default
    assert d._review_independence_state({}).startswith("ON")
    # explicit off is called out as the maker reviewing its own work
    assert "INLINE" in d._review_independence_state({"review": {"independent": False}})
    # and it appears in the live dashboard
    base = _sdlc(tmp_path, {"review": {"independent": True}})
    rows = {name: state for name, state, _ in d.features(base)}
    assert row in rows, "the doctor row must say the review is advisory"
    assert rows[row].startswith("ON")


# --- F6: a truthy non-dict config block must never crash check()/features() -------------------


def test_block_helper_degrades_a_non_dict_to_empty():
    d = _doc()
    assert d._block({"x": "oops"}, "x") == {}
    assert d._block({"x": ["a", "list"]}, "x") == {}
    assert d._block({"x": 42}, "x") == {}
    assert d._block({"x": True}, "x") == {}
    assert d._block({"x": None}, "x") == {}
    assert d._block({}, "x") == {}
    assert d._block({"x": {"y": 1}}, "x") == {"y": 1}          # a real dict passes through unchanged
    assert d._block("not a dict", "x") == {}                   # a non-dict cfg itself is also guarded
    assert d._block(["a", "list"], "x") == {}


def test_non_dict_top_level_config_json_reads_as_empty(tmp_path):
    # json.loads can succeed on a non-object top level ("[1,2]", "\"oops\"", "42") — _cfg must not
    # hand a list/str/int to every downstream cfg.get() call.
    d = _doc()
    base = pathlib.Path(tmp_path) / ".sdlc"
    base.mkdir(parents=True)
    (base / "config.json").write_text(json.dumps([1, 2, 3]))
    assert d._cfg(str(base)) == {}
    d.check(str(base), run=_runner())      # must not raise
    d.features(str(base))                  # must not raise


def test_the_issues_exact_repro_does_not_crash(tmp_path):
    # {"verify": "pytest"} — a common shape typo (config value where a block was meant), and the
    # exact repro from the finding this test guards against regressing.
    d = _doc()
    base = _sdlc(tmp_path, {"verify": "pytest"})
    d.features(base)                       # must not raise AttributeError
    d.check(base, run=_runner())           # must not raise AttributeError


_MALFORMED_BLOCKS = ["discovery", "knowledge_graph", "ledger", "verify", "work", "gates",
                     "review", "budget", "parallel", "session_start", "backlog_check", "journal",
                     "goal_decompose"]


@pytest.mark.parametrize("bad_value", ["a string", ["a", "list"], 42, True], ids=["str", "list", "int", "bool"])
@pytest.mark.parametrize("block", _MALFORMED_BLOCKS)
def test_malformed_config_block_does_not_crash_check_or_features(tmp_path, block, bad_value):
    """A truthy non-dict value for ANY declared config block must degrade to reading as off, never
    raise — doctor is the one tool an adopter runs BECAUSE their config is wrong, so it must survive
    exactly the malformed input that brought them here."""
    d = _doc()
    base = _sdlc(tmp_path, {block: bad_value})
    checks = d.check(base, run=_runner())
    assert isinstance(checks, list) and all("ok" in c for c in checks)
    rows = d.features(base)
    assert isinstance(rows, list) and all(len(r) == 3 for r in rows)


def test_malformed_nested_gates_block_does_not_crash(tmp_path):
    d = _doc()
    base = _sdlc(tmp_path, {"gates": {"hard_plan_gate": "oops", "stop_gate": ["x"], "decision_gate": 1}})
    d.check(base, run=_runner())
    d.features(base)


def test_malformed_decision_gate_block_does_not_crash_with_a_registry_present(tmp_path):
    # `_decision_gate_state` short-circuits BEFORE reaching the malformed-block read when
    # decisions.json is absent (`if not reg.exists(): return "off..."`) — so a malformed
    # gates.decision_gate only actually reaches the vulnerable line when a registry exists (the
    # realistic state for any adopter who has run /sigma-decide). Without the registry present,
    # this case would pass even on the unfixed code — reaching the line is the whole test.
    d = _doc()
    base = _sdlc(tmp_path, {"gates": {"decision_gate": "oops"}})
    (pathlib.Path(base) / "decisions.json").write_text(json.dumps({"decisions": []}))
    d.check(base, run=_runner())
    d.features(base)


def test_malformed_nested_github_project_block_does_not_crash(tmp_path):
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"project": "oops"}}})
    d.check(base, run=_runner(gh_auth="Logged in. Token scopes: 'repo', 'project'"))


# --- F33/#358: "north-star filled" must clear every tier, not just Vision ----------------------
# The check used to test the file for ONLY the Vision-tier placeholder, so a north-star with Vision
# written up but Strategy/Design/Architecture still on the scaffolded placeholder text read as
# "filled" anyway.

_NS_VISION_ONLY = """# demo - North Star

## Vision (why this exists, for whom)
We help QA teams ship browser tests fast, for engineers who hate writing selectors by hand.

## Strategy (what we're building now)
- Priorities: <the few things that matter this cycle>
- Non-goals: <what we are deliberately NOT doing - the alignment gate uses these>

## Design (how the product should feel)
<the experience + the principles a change must respect>

## Architecture (how it's built + the rules we develop by)
<the shape of the system - the stack itself lives in project.md.>
1. <e.g. the UI layer holds no business logic>
"""

_NS_FULLY_FILLED = """# demo - North Star

## Vision (why this exists, for whom)
We help QA teams ship browser tests fast.

## Strategy (what we're building now)
- Priorities: ship the parser this cycle
- Non-goals: no mobile support yet

## Design (how the product should feel)
Fast, forgiving, terse output.

## Architecture (how it's built + the rules we develop by)
A LangGraph engine with Postgres state.
1. The UI layer holds no business logic
"""


def _ns(d, content):
    base = _sdlc(d, {})
    (pathlib.Path(base) / "context").mkdir(parents=True, exist_ok=True)
    (pathlib.Path(base) / "context" / "north-star.md").write_text(content)
    return base


def test_flags_not_filled_when_only_vision_tier_is_written(tmp_path):
    # The issue's own acceptance case: Vision filled, the other three tiers still on the scaffolded
    # placeholder text. Must NOT read as "filled".
    d = _doc()
    base = _ns(tmp_path, _NS_VISION_ONLY)
    c = _by_name(d.check(base, run=_runner()))
    assert c["north-star filled"]["ok"] is False
    assert "Strategy" in c["north-star filled"]["fix"]        # names the first unfilled tier


def test_north_star_filled_when_every_tier_is_written(tmp_path):
    d = _doc()
    base = _ns(tmp_path, _NS_FULLY_FILLED)
    c = _by_name(d.check(base, run=_runner()))
    assert c["north-star filled"]["ok"] is True
    assert c["north-star filled"]["fix"] == ""


def test_flags_not_filled_when_only_the_last_tier_is_a_placeholder(tmp_path):
    # Every tier but Architecture is written - proves the check walks ALL FOUR tiers (not just
    # Vision, the bug) and correctly names a LATER tier, not only ever the first one.
    content = _NS_FULLY_FILLED.replace(
        "A LangGraph engine with Postgres state.\n1. The UI layer holds no business logic\n",
        "<the shape of the system - the stack itself lives in project.md.>\n"
        "1. <e.g. the UI layer holds no business logic>\n",
    )
    d = _doc()
    base = _ns(tmp_path, content)
    c = _by_name(d.check(base, run=_runner()))
    assert c["north-star filled"]["ok"] is False
    assert "Architecture" in c["north-star filled"]["fix"]


# --- #445: sub-field within a tier can stay a placeholder undetected (F33 follow-up) --------------
# F33/#358 fixed tier-level granularity (walk all four tiers, not just Vision) but a tier can
# scaffold MORE THAN ONE placeholder: Strategy has two (Priorities, Non-goals), Architecture has
# three (the intro line + two numbered example rules). The tier-level check only ever tested ONE
# representative placeholder per tier, so filling in Priorities while leaving Non-goals on its
# scaffolded text still read as a fully "filled" Strategy tier - undetected.

def test_flags_not_filled_when_one_of_two_strategy_subfields_is_still_a_placeholder(tmp_path):
    # Priorities genuinely written; Non-goals still on the scaffolded placeholder. A tier-level-only
    # check (one placeholder tested per tier) misses this because the ONE placeholder it tests for
    # (Priorities') is gone from the text - it never looks for Non-goals' placeholder at all.
    content = _NS_FULLY_FILLED.replace(
        "- Non-goals: no mobile support yet\n",
        "- Non-goals: <what we are deliberately NOT doing - the alignment gate uses these>\n",
    )
    d = _doc()
    base = _ns(tmp_path, content)
    c = _by_name(d.check(base, run=_runner()))
    assert c["north-star filled"]["ok"] is False
    assert "Strategy" in c["north-star filled"]["fix"]


def test_flags_not_filled_when_one_of_three_architecture_subfields_is_still_a_placeholder(tmp_path):
    # The intro line and rule 1 are genuinely written; rule 2 is still the scaffolded example.
    content = _NS_FULLY_FILLED.replace(
        "A LangGraph engine with Postgres state.\n1. The UI layer holds no business logic\n",
        "A LangGraph engine with Postgres state.\n"
        "1. The UI layer holds no business logic\n"
        "2. <e.g. dependencies point inward; no sibling imports across modules>\n",
    )
    d = _doc()
    base = _ns(tmp_path, content)
    c = _by_name(d.check(base, run=_runner()))
    assert c["north-star filled"]["ok"] is False
    assert "Architecture" in c["north-star filled"]["fix"]


# --- #577: '..' still walked out of the repo -----------------------------------------------------
# #545 taught `_under` that a leading "/" means repo-root-relative, but nothing normalized "..", so
# the reference was still free to climb back out after the strip. `/../outside.md` is the sharp case:
# BEFORE #545 it was flagged (by accident — the leading slash sent it to the OS root, where nothing
# was), and AFTER #545 it reads OK, because the strip turns it into a real walk up out of the repo.
# So #545 traded an accidental catch for a real miss. The deep `/../..(x8)/etc/passwd` form escaped
# in both eras. `_ABSTRACT` (doctor.py) filters "..." but not "..", which is why these reach the
# resolver at all rather than being dismissed as patterns.
#
# Under-reporting only: this is an `exists()` check on team-authored docs, no content is read and
# the exit code is 0 either way. What it costs is trust in the check.

def _outside_repo_fixture(d):
    """A repo with a real file planted just OUTSIDE it — so a test proves where a reference RESOLVES
    rather than depending on whichever system paths happen to exist wherever CI runs."""
    outer = pathlib.Path(d)
    (outer / "outside.md").write_text("# not in the repo")
    repo = outer / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "architecture.md").write_text("# arch")
    return repo


def test_hygiene_flags_a_reference_that_climbs_out_of_the_repo():
    """The regression #545 introduced: a shallow `/../` walks straight back out, onto a file that
    really exists, so the reference read as resolvable."""
    with tempfile.TemporaryDirectory() as d:
        repo = _outside_repo_fixture(d)
        assert (repo / ".." / "outside.md").exists()      # the precondition the miss relies on
        sdlc = _hyg(str(repo), project_md="Spec: `/../outside.md`.",
                    north_star="See [spec](/../outside.md).")
        rows = {c["name"]: c for c in _doc().hygiene(sdlc, str(repo))}
        assert not rows["standing docs: cited paths resolve"]["ok"]
        assert not rows["standing docs: links resolve"]["ok"]


def test_hygiene_flags_a_deep_climb_out_of_the_repo():
    """The form that escaped in BOTH eras: enough `../` to reach the filesystem root whatever the
    repo's depth, landing on a path that exists on every Unix box."""
    with tempfile.TemporaryDirectory() as d:
        repo = _outside_repo_fixture(d)
        deep = "/" + "../" * 8 + "etc/passwd"
        sdlc = _hyg(str(repo), project_md=f"Spec: `{deep}`.", north_star=f"See [spec]({deep}).")
        rows = {c["name"]: c for c in _doc().hygiene(sdlc, str(repo))}
        assert not rows["standing docs: cited paths resolve"]["ok"]
        assert not rows["standing docs: links resolve"]["ok"]


def test_hygiene_still_follows_a_dot_dot_link_that_stays_inside_the_repo():
    """The guard is about leaving the REPO, not about the `..` character: a north-star linking up to
    `../project.md` is ordinary and must keep resolving. Boundary-checking against the linking
    document's own directory instead of the repo root would have broken this."""
    with tempfile.TemporaryDirectory() as d:
        repo = _outside_repo_fixture(d)
        sdlc = _hyg(str(repo), project_md="# project", north_star="See [proj](../project.md).")
        rows = {c["name"]: c for c in _doc().hygiene(sdlc, str(repo))}
        assert rows["standing docs: links resolve"]["ok"], rows["standing docs: links resolve"]["fix"]


# --- #577 cycle 2: the containment boundary must share an origin with the docs -------------------
# The first cut anchored the boundary with `abspath`, which resolves a relative `repo_root` against
# the PROCESS CWD while the join base comes from `sdlc_dir`. When those two origins disagree the
# guard fires on ordinary in-repo links. `abspath` reconciles neither relative-vs-absolute origin
# nor symlink-vs-real form, and BOTH mismatches are reachable through the module's own one-arg
# `hygiene(sdlc_dir)` call inside the `check` verb.

def _repo_with_an_ordinary_in_repo_link(outer):
    """A north-star linking up to `../project.md` — the most ordinary cross-doc reference there is,
    and the one an over-strict boundary destroys."""
    repo = outer / "repo"
    (repo / ".sdlc" / "context").mkdir(parents=True)
    (repo / ".sdlc" / "config.json").write_text("{}")
    (repo / ".sdlc" / "project.md").write_text("# project")
    (repo / ".sdlc" / "context" / "north-star.md").write_text("See [proj](../project.md).")
    return repo


def test_hygiene_one_arg_from_an_unrelated_cwd_does_not_flag_an_in_repo_link(monkeypatch):
    """`check <abs>/.sdlc` is the module's own one-arg call, and it must not depend on where the
    process happens to be standing. cwd is a SIBLING of the repo here, not an ancestor — an ancestor
    hides the bug, because the repo still sits underneath it."""
    with tempfile.TemporaryDirectory() as d:
        outer = pathlib.Path(d).resolve()
        repo = _repo_with_an_ordinary_in_repo_link(outer)
        elsewhere = outer / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        rows = {c["name"]: c for c in _doc().hygiene(str(repo / ".sdlc"))}
        links = rows["standing docs: links resolve"]
        assert links["ok"], links["fix"]


def test_hygiene_one_arg_through_a_symlinked_repo_path_does_not_flag_an_in_repo_link(monkeypatch):
    """The symlink face of the same mismatch: one side keeps the alias, the other resolves to the
    real path, and the comparison fails. macOS hands this out for free — /tmp IS a symlink to
    /private/tmp — so every checkout under /tmp hit it."""
    with tempfile.TemporaryDirectory() as d:
        real = pathlib.Path(d).resolve()
        repo = _repo_with_an_ordinary_in_repo_link(real)
        alias = pathlib.Path(d).parent / (pathlib.Path(d).name + "-alias")
        os.symlink(real, alias)
        try:
            assert pathlib.Path(os.path.realpath(alias)) == real   # the precondition
            monkeypatch.chdir(repo)
            rows = {c["name"]: c for c in _doc().hygiene(str(alias / "repo" / ".sdlc"))}
            links = rows["standing docs: links resolve"]
            assert links["ok"], links["fix"]
        finally:
            os.unlink(alias)


# --- #586: the features dashboard must report the window the GATES actually use ------------------
# Both gate rows rendered `plan_freshness_hours` straight from config, so a value the gates reject
# was still advertised as live: "24h" printed as "ON (24hh window)" and -5 as "ON (-5h window)"
# while plan_gate.sh/completion_gate.sh both fell back to 24. A dashboard whose job is to tell an
# adopter what is switched on must not report a setting that is not in force — that is the same
# crying-wolf failure the hygiene checks are built to avoid, one surface over.
#
# The gates' rule is two-step and is mirrored exactly: an inner try around int() (so a bad freshness
# cannot corrupt the mode line), THEN a digits-only guard on the result (which is what rejects a
# negative). `0` and a null both fall back too, because the gates read `value or 24`.

@pytest.mark.parametrize("raw, expected", [
    ("24h",  24),    # the issue's first case: unparseable -> the gates use the default
    (-5,     24),    # the issue's second: parses fine, but the digits-only guard rejects it
    (12,     12),    # a good value must still render itself, or the fix is just a constant
    ("12",   12),    # ...including the string spelling of one
    ("lots", 24),    # any other unparseable text
    (0,      24),    # int zero is falsy, so `value or 24` hands the gates the default instead
    (None,   24),    # key PRESENT but null: .get(k, 24) returns None, so the default never applied
    ("0",    0),     # #602: the STRING "0" is truthy and all-digits, so a 0h window really is
                     # reachable and both gates enforce it. Green on arrival — it is here to prove
                     # the mirror is FAITHFUL rather than over-eager, and to correct the prose that
                     # claimed zero could never reach the gates (true of int 0/null only).
    ((2 ** 63 - 1) // 60,     8760),                  # #602: the largest window that does not
                     # wrap is still far past a year, so it clamps like the rest -- the clamp
                     # bounds MAGNITUDE, it is not merely an overflow patch
    ((2 ** 63 - 1) // 60 + 1, 8760),                  # one past it: $(( h * 60 )) goes NEGATIVE
    (10 ** 18,                8760),                  # far past: wraps back POSITIVE and huge
    (99999999999999999999,    8760),                  # the issue's probed 20-digit value
])
def test_features_reports_the_effective_freshness_window(tmp_path, raw, expected):
    d = _doc()
    base = tmp_path / ".sdlc"
    base.mkdir()
    base.joinpath("config.json").write_text(json.dumps(
        {"gates": {"hard_plan_gate": {"enabled": True, "plan_freshness_hours": raw},
                   "stop_gate": {"enabled": True, "plan_freshness_hours": raw}}}))
    rows = {name: state for name, state, _ in d.features(str(base))}
    # both gates share the value, the fallback rule AND the bug — neither row may drift from it
    assert rows["hard plan-gate (deny source edits w/o fresh plan)"].startswith(
        f"ON ({expected}h window)")     # #2116 appended where it is enforced; the window is the pin
    assert rows["Stop gate (refuse to end a session with unplanned source)"] == f"ON ({expected}h window)"


def test_the_effective_window_agrees_with_the_gate_scripts_own_fallback(tmp_path):
    """Pins the SHAPE of the rule against the shell, not just its outputs: the gates parse with an
    inner try, clamp, and then reject anything non-digit, and doctor has to do all three steps or it
    drifts the next time either side is touched. Asserted against BOTH hooks — they are siblings and
    a rule that lands in only one of them is the drift this pin exists to catch."""
    d = _doc()
    hooks = pathlib.Path(__file__).resolve().parent.parent / "hooks"
    for name in ("completion_gate.sh", "plan_gate.sh"):
        text = (hooks / name).read_text(encoding="utf-8")
        assert 'hours = int(g.get("plan_freshness_hours") or 24)' in text or \
               'hours = int(gate.get("plan_freshness_hours") or 24)' in text     # 1: the inner try
        assert f"hours = {d._MAX_WINDOW_HOURS}" in text                          # 2: the clamp
        assert "''|*[!0-9]*) fresh_hours=24" in text                             # 3: digits guard
    assert d._effective_window({"plan_freshness_hours": "24h"}) == 24
    assert d._effective_window({"plan_freshness_hours": -5}) == 24
    assert d._effective_window({"plan_freshness_hours": 12}) == 12
    assert d._effective_window({}) == 24                                  # absent -> the default
    # the clamp is a MAGNITUDE bound; the digits guard never saw these coming
    assert d._effective_window({"plan_freshness_hours": 10 ** 20}) == d._MAX_WINDOW_HOURS


# --- #600: three follow-ups from the #577 cycle-2 review -----------------------------------------
# All three are about the containment check being HONEST: about what it rejects, and about which of
# its own lines are actually holding the behaviour up.

def _repo_with_an_in_repo_link(outer):
    repo = outer / "repo"
    (repo / ".sdlc" / "context").mkdir(parents=True)
    (repo / ".sdlc" / "config.json").write_text("{}")
    (repo / ".sdlc" / "project.md").write_text("# project")
    (repo / ".sdlc" / "context" / "north-star.md").write_text("See [proj](../project.md).")
    return repo


def test_a_reference_outside_the_repo_is_reported_differently_from_a_missing_one():
    """Item 1. `realpath` resolves THROUGH symlinks, so a reference can point at something that
    genuinely EXISTS and still be rejected for living outside the repo. Telling that adopter "no
    such file" about a path they can `cat` is the crying-wolf direction the whole check is built to
    avoid — the two failures have different fixes and must read differently."""
    with tempfile.TemporaryDirectory() as d:
        outer = pathlib.Path(d).resolve()
        (outer / "outside.md").write_text("# really does exist, just not in the repo")
        repo = outer / "repo"
        (repo / ".sdlc").mkdir(parents=True)
        (repo / ".sdlc" / "config.json").write_text("{}")
        (repo / ".sdlc" / "project.md").write_text(
            "Outside: `/../outside.md`. Missing: `src/gone.py`.")
        assert (outer / "outside.md").exists()          # the precondition that makes the old text wrong
        fix = {c["name"]: c for c in _doc().hygiene(str(repo / ".sdlc"))}[
            "standing docs: cited paths resolve"]["fix"]
        assert "/../outside.md" in fix and "src/gone.py" in fix
        assert "outside the repo" in fix               # said about the one that exists...
        assert "no such file" in fix                   # ...and NOT said about it
        outside_part = fix.split("/../outside.md")[1].split(",")[0]
        assert "no such file" not in outside_part


def test_the_hygiene_verb_derives_the_repo_root_from_the_sdlc_path(tmp_path):
    """Item 2. The verb passes `None` (not ".") so `hygiene()` derives the boundary from the .sdlc
    path. Reverting that one argument leaves every in-process test green while breaking the real
    command, because the in-process tests never go through the CLI — so this pin drives the actual
    subprocess, from a cwd OUTSIDE the repo, which is the only place the wiring shows."""
    import subprocess, sys
    outer = pathlib.Path(tmp_path).resolve()
    repo = _repo_with_an_in_repo_link(outer)
    elsewhere = outer / "elsewhere"
    elsewhere.mkdir()
    p = subprocess.run([sys.executable, str(D), "hygiene", str(repo / ".sdlc")],
                       capture_output=True, text=True, cwd=str(elsewhere))
    assert p.returncode == 0, p.stderr
    assert "../project.md" not in p.stdout, p.stdout      # an ordinary in-repo link, not rot
    assert "STALE" not in p.stdout, p.stdout


def test_an_explicitly_passed_symlinked_repo_root_still_contains_the_docs(tmp_path):
    """Item 3. The root-derivation half of #577 hides this: when `repo_root` is passed EXPLICITLY in
    a different symlink form from `sdlc_dir`, only resolving both sides keeps them comparable. A
    lexical normalizer leaves the alias an alias while its partner is real, and an ordinary in-repo
    link is flagged."""
    outer = pathlib.Path(tmp_path).resolve()
    repo = _repo_with_an_in_repo_link(outer)
    alias = outer.parent / (outer.name + "-alias")
    os.symlink(outer, alias)
    try:
        assert pathlib.Path(os.path.realpath(alias)) == outer      # the precondition
        rows = {c["name"]: c for c in _doc().hygiene(str(repo / ".sdlc"), str(alias / "repo"))}
        links = rows["standing docs: links resolve"]
        assert links["ok"], links["fix"]
    finally:
        os.unlink(alias)


# --- #695: the board's built-in "Item closed" workflow ------------------------------------------
# Measured on this repo's own board (project #6): {"number":1,"name":"Item closed","enabled":false}.
# With it off, the ONLY thing that ever moves a card to Done is the loop's own complete() path, so
# every close the loop did not perform strands its card wherever it was — 92 of them here.
# sigma cannot self-heal it: the GraphQL schema exposes only `deleteProjectV2Workflow`, there is
# no enable/update mutation, and `gh project` has no `workflow` subcommand. Detect and say so.

def _wf(enabled, name="Item closed"):
    import json as _json
    return _json.dumps({"data": {"repositoryOwner": {"projectV2": {"workflows": {
        "nodes": [{"number": 1, "name": name, "enabled": enabled},
                  {"number": 2, "name": "Pull request merged", "enabled": False}]}}}}})


def _wf_run(payload):
    return lambda a: payload if a[:3] == ["gh", "api", "graphql"] else ""


_WF_CFG = {"repo": "acme/widget", "project": {"enabled": True, "number": 8, "owner": "acme"}}


def test_item_closed_workflow_disabled_is_reported_with_the_manual_fix():
    d = _doc()
    fix = d._item_closed_workflow_off(_WF_CFG, _wf_run(_wf(False)))
    assert fix and "Item closed" in fix
    assert "workflows" in fix.lower()          # points at the page, since no command can do it


def test_item_closed_workflow_enabled_is_silent():
    d = _doc()
    assert d._item_closed_workflow_off(_WF_CFG, _wf_run(_wf(True))) is None


def test_workflow_check_is_read_only():
    d = _doc()
    calls = []
    def run(a):
        calls.append(list(a)); return _wf(False) if a[:3] == ["gh", "api", "graphql"] else ""
    d._item_closed_workflow_off(_WF_CFG, run)
    joined = [" ".join(c) for c in calls]
    assert all("mutation" not in c for c in joined)
    assert all(c.startswith("gh api graphql") for c in joined)


def test_workflow_check_fails_open_on_every_unreadable_case():
    """Same convention as its two siblings: a doctor check that can crash /sigma-doctor is worse
    than no check. No scope, no board, an API blip, malformed JSON — all silent."""
    d = _doc()
    def boom(a):
        raise RuntimeError("missing `project` scope")
    for run in (_wf_run(""), _wf_run("not json"), _wf_run('{"data":{"repositoryOwner":null}}'),
                _wf_run('{"errors":[{"message":"nope"}]}'), boom):
        assert d._item_closed_workflow_off(_WF_CFG, run) is None


def test_workflow_check_needs_a_pinned_number_and_does_nothing_without_one():
    d = _doc()
    cfg = {"repo": "acme/widget", "project": {"enabled": True}}
    calls = []
    d._item_closed_workflow_off(cfg, lambda a: calls.append(a) or "")
    assert calls == []


def test_workflow_check_uses_viewer_for_an_at_me_owner():
    """`@me` is not a login GraphQL can look up — it needs the `viewer` root instead."""
    d = _doc()
    seen = []
    def run(a):
        seen.append(" ".join(a))
        return '{"data":{"viewer":{"projectV2":{"workflows":{"nodes":[{"name":"Item closed","enabled":false}]}}}}}'
    cfg = {"repo": "acme/widget", "project": {"enabled": True, "number": 8, "owner": "@me"}}
    assert d._item_closed_workflow_off(cfg, run) is not None
    assert any("viewer" in q for q in seen)


# --- #1206: an OPEN issue whose board card already reads Done -----------------------------------
# The opposite direction from _item_closed_workflow_off above. GitHub's built-in workflows move a
# card TO Done in exactly one direction (closing an issue fires "Item closed") -- there is no
# "Item reopened" workflow to move it back, so a reopened issue's card is stranded at Done for the
# rest of its life unless a human resets it by hand. Silent: every check/metric that only measures
# the open-issue set gets BETTER when an issue is accidentally closed.

_DONE_CFG = {"repo": "acme/widget", "project": {"enabled": True, "number": 8, "owner": "acme"}}


def _done_run(issues_json, items_json):
    def run(a):
        if _is_list(a):
            return issues_json
        if a[:2] == ["gh", "project"]:
            return items_json
        return ""
    return run


def test_open_issue_with_a_done_card_is_reported_with_the_manual_fix():
    d = _doc()
    issues = json.dumps([{"number": 5}, {"number": 9}])
    items = json.dumps({"items": [{"content": {"number": 5}, "status": "Done"},
                                  {"content": {"number": 9}, "status": "In Progress"}]})
    fix = d._open_issue_done_card(_DONE_CFG, _done_run(issues, items))
    assert fix and "#5" in fix and "#9" not in fix
    assert "Item reopened" in fix


def test_no_open_issue_stranded_at_done_is_silent():
    d = _doc()
    issues = json.dumps([{"number": 9}])
    items = json.dumps({"items": [{"content": {"number": 9}, "status": "In Progress"}]})
    assert d._open_issue_done_card(_DONE_CFG, _done_run(issues, items)) is None


def test_open_issue_done_card_check_fails_open_on_every_unreadable_case():
    """Same convention as its sibling: a doctor check that can crash /sigma-doctor is worse than no
    check. No pinned number, no repo, no `project` scope (empty read), an API blip, malformed
    JSON on either call -- all silent, never a false alarm."""
    d = _doc()
    no_number = {"repo": "acme/widget", "project": {"enabled": True, "owner": "acme"}}
    no_repo = {"project": {"enabled": True, "number": 8, "owner": "acme"}}
    one_issue = json.dumps([{"number": 5}])
    cases = [
        (no_number, _done_run("[]", "{}")),
        (no_repo, _done_run("[]", "{}")),
        (_DONE_CFG, _done_run("", "{}")),                       # no issue list -- can't even list open goals
        (_DONE_CFG, _done_run("not json", "{}")),                # malformed issue json
        (_DONE_CFG, _done_run(one_issue, "")),                    # no item list -- missing `project` scope
        (_DONE_CFG, _done_run(one_issue, "not json")),            # malformed item json
    ]
    for cfg, run in cases:
        assert d._open_issue_done_card(cfg, run) is None


def test_open_issue_done_card_check_is_read_only():
    d = _doc()
    calls = []
    def run(a):
        calls.append(list(a))
        if _is_list(a):
            return json.dumps([{"number": 5}])
        if a[:2] == ["gh", "project"]:
            return json.dumps({"items": [{"content": {"number": 5}, "status": "Done"}]})
        return ""
    d._open_issue_done_card(_DONE_CFG, run)
    joined = [" ".join(c) for c in calls]
    assert all("edit" not in c for c in joined)
    assert all("mutation" not in c for c in joined)


def test_open_issue_done_card_check_honours_a_configured_done_column_name():
    d = _doc()
    cfg = {"repo": "acme/widget",
          "project": {"enabled": True, "number": 8, "owner": "acme", "columns": {"done": "Shipped"}}}
    issues = json.dumps([{"number": 5}])
    items = json.dumps({"items": [{"content": {"number": 5}, "status": "Shipped"}]})
    fix = d._open_issue_done_card(cfg, _done_run(issues, items))
    assert fix and "#5" in fix and "Shipped" in fix


# --- #1391 step 7: the blocked_label collision, and the orphan class nothing else can see --------

_COLLIDE = "blocked_label and goal_blocked_label are DIFFERENT labels"
_ORPHAN = "no issue carries sdlc:in-progress without a lifecycle label"


_UNREACHABLE = "every sdlc:blocking issue is one the loop can actually pick"


def _by_label_run(by_label):
    """Label-AWARE variant of `_bl_run`: `gh issue list --label X` answers `by_label[X]`. The
    #1392 scan queries a DIFFERENT label from every scan before it, so a fixture that answers every
    list call with one payload cannot express "this issue is blocking but not a goal"."""
    def run(args):
        if args[:3] == ["gh", "auth", "status"]:
            return "Logged in."
        if _is_list(args):
            return json.dumps(by_label.get(_list_label(args), []))
        return ""
    return run


def test_a_blocking_issue_with_no_goal_label_is_flagged_as_a_deadlock(tmp_path):
    """#1392: `sdlc:blocking` is a TIE-BREAK among already-eligible issues, never a queue --
    `_blocking_priority_pending` reuses `_fetch_pending`, whose base query always carries `--label
    sdlc:goal`, and `gh issue list --label` ANDs. So a blocker without the goal label is reachable
    by nothing, while the auto-unpark sweep will not resume what it blocks until it CLOSES."""
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}})
    run = _by_label_run({"sdlc:blocking": [
        {"number": 7, "labels": [{"name": "sdlc:blocking"},
                                  {"name": "sdlc:needs-confirmation"}]}]})
    hit = {c["name"]: c for c in d.check(base, run=run)}[_UNREACHABLE]
    assert hit["ok"] is False and "#7" in hit["fix"]
    assert "/sigma-promote" in hit["fix"]          # a finding must name its route out


def test_a_blocking_issue_that_is_already_a_goal_is_not_flagged(tmp_path):
    """The normal shape: something blocks other work AND is itself pickable. Never a finding."""
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}})
    run = _by_label_run({"sdlc:blocking": [
        {"number": 8, "labels": [{"name": "sdlc:blocking"}, {"name": "sdlc:goal"}]}]})
    assert _UNREACHABLE not in {c["name"] for c in d.check(base, run=run)}


def test_unreachable_blocker_scan_degrades_cleanly_when_the_backlog_cannot_be_read(tmp_path):
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}})
    for payload in ("", "not json", json.dumps({"oops": 1})):
        def run(args, payload=payload):
            if args[:3] == ["gh", "auth", "status"]:
                return "Logged in."
            if _is_list(args):
                return payload
            return ""
        assert _UNREACHABLE not in {c["name"] for c in d.check(base, run=run)}, payload


def test_blocked_label_collision_is_flagged(tmp_path):
    """Live on the os adopter: the HUMAN hold (#1205) and the MACHINE-managed blocked state (#1350)
    resolve to the same string, so clearing the machine state overrides deliberate human intent."""
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {
        "repo": "acme/widget", "blocked_label": "sdlc:blocked"}}})
    checks = {c["name"]: c for c in d.check(base, run=_bl_run([]))}
    hit = checks[_COLLIDE]
    assert hit["ok"] is False
    assert "sdlc:blocked" in hit["fix"] and "human" in hit["fix"].lower()


def test_no_collision_when_the_two_labels_differ(tmp_path):
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {
        "repo": "acme/widget", "blocked_label": "on-hold"}}})
    assert _COLLIDE not in {c["name"] for c in d.check(base, run=_bl_run([]))}


def test_no_collision_when_the_human_label_is_unset(tmp_path):
    """Unset is the shipped default and is not a collision -- only an explicit equal value is."""
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}})
    assert _COLLIDE not in {c["name"] for c in d.check(base, run=_bl_run([]))}


def test_orphan_in_progress_issue_is_flagged(tmp_path):
    """Invisible to the picker (queries by goal label) AND to the multi-label invariant (flags only
    issues carrying MORE than one). Nothing else in the system can find these."""
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}})
    issues = [{"number": 42, "labels": [{"name": "sdlc:in-progress"}]}]
    checks = {c["name"]: c for c in d.check(base, run=_bl_run(issues))}
    hit = checks[_ORPHAN]
    assert hit["ok"] is False and "#42" in hit["fix"]


def test_in_progress_alongside_a_lifecycle_label_is_not_flagged(tmp_path):
    """The normal active-work signature -- must never be reported."""
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}})
    issues = [{"number": 42, "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:in-progress"}]}]
    assert _ORPHAN not in {c["name"] for c in d.check(base, run=_bl_run(issues))}


def test_orphan_scan_counts_only_membership_labels(tmp_path):
    """#1393: `sdlc:blocked` is an OVERLAY, not membership. This scan was the one place the
    membership refactor missed, and the effect was to HIDE the orphan class it exists to find -- an
    issue carrying {in-progress, blocked} and no membership label passed the "does it have any
    primary?" test on the strength of its own second overlay, so the exact shape a half-applied park
    leaves behind was reported as fine."""
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}})
    for lbl in ("sdlc:goal", "sdlc:parked", "sdlc:needs-confirmation"):
        issues = [{"number": 42, "labels": [{"name": lbl}, {"name": "sdlc:in-progress"}]}]
        assert _ORPHAN not in {c["name"] for c in d.check(base, run=_bl_run(issues))}, lbl
    # ...and the overlay-only shape is now FLAGGED, where it used to pass
    issues = [{"number": 42, "labels": [{"name": "sdlc:blocked"}, {"name": "sdlc:in-progress"}]}]
    hit = {c["name"]: c for c in d.check(base, run=_bl_run(issues))}[_ORPHAN]
    assert hit["ok"] is False and "#42" in hit["fix"]


def test_orphan_scan_degrades_cleanly_when_the_backlog_cannot_be_read(tmp_path):
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}})
    for payload in ("", "not json", json.dumps({"oops": 1})):
        def broken(args, _p=payload):
            if args[:3] == ["gh", "auth", "status"]:
                return "Logged in."
            if _is_list(args):
                return _p
            return ""
        assert _ORPHAN not in {c["name"] for c in d.check(base, run=broken)}   # must not raise


def test_orphan_scan_skipped_in_local_mode(tmp_path):
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "local-goals"}})
    issues = [{"number": 42, "labels": [{"name": "sdlc:in-progress"}]}]
    assert _ORPHAN not in {c["name"] for c in d.check(base, run=_bl_run(issues))}


def test_a_blocked_goal_is_not_reported_as_multi_state_drift(tmp_path):
    """#1393: `mark_blocked` KEEPS `sdlc:goal`, so `goal`+`blocked` is what a correctly-blocked goal
    looks like. Reporting it would flag every blocked goal on the board as corruption -- the exact
    class of false alarm that teaches people to stop reading a check."""
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}})
    issues = [{"number": 42, "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:blocked"}]}]
    assert _MSL_NAME not in {c["name"] for c in d.check(base, run=_bl_run(issues))}


def test_goal_plus_in_progress_is_still_not_reported_either(tmp_path):
    """The pre-existing overlay, re-asserted beside the new one so the two are visibly one rule."""
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}})
    issues = [{"number": 42, "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:in-progress"}]}]
    assert _MSL_NAME not in {c["name"] for c in d.check(base, run=_bl_run(issues))}

# --- stray worktree install: a shared site-packages holds one slot per import name -------------

_STRAY_CHECK = "no stray local install shadows this worktree's own packages"


def _fake_dist_info(site_packages, dist_name, top_level, source_url=None):
    """A minimal `*.dist-info/` directory shaped like pip actually writes one: `top_level.txt`
    always present, `direct_url.json` only when `source_url` is given (pip omits it for ordinary
    PyPI installs)."""
    d = pathlib.Path(site_packages) / f"{dist_name}-1.0.dist-info"
    d.mkdir(parents=True)
    (d / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {dist_name}\nVersion: 1.0\n")
    (d / "top_level.txt").write_text(top_level + "\n")
    if source_url is not None:
        (d / "direct_url.json").write_text(json.dumps({"url": source_url}))


def test_flags_a_stray_install_shadowing_this_worktrees_own_package():
    d = _doc()
    with tempfile.TemporaryDirectory() as t, tempfile.TemporaryDirectory() as other, \
         tempfile.TemporaryDirectory() as sp:
        base = _sdlc(t, {})
        (pathlib.Path(t) / "pyproject.toml").write_text("[project]\nname = 'mypkg'\n")
        (pathlib.Path(t) / "mypkg").mkdir()
        (pathlib.Path(other) / "mypkg").mkdir(parents=True)
        _fake_dist_info(sp, "mypkg", "mypkg", source_url=f"file://{pathlib.Path(other) / 'mypkg'}")

        c = _by_name(d.check(base, run=_runner(), site_packages_dirs=[sp]))[_STRAY_CHECK]
        assert c["ok"] is False
        assert "mypkg" in c["fix"] and str(other) in c["fix"]


def test_no_stray_flagged_when_the_install_points_at_this_same_worktree():
    d = _doc()
    with tempfile.TemporaryDirectory() as t, tempfile.TemporaryDirectory() as sp:
        base = _sdlc(t, {})
        (pathlib.Path(t) / "pyproject.toml").write_text("[project]\nname = 'mypkg'\n")
        (pathlib.Path(t) / "mypkg").mkdir()
        _fake_dist_info(sp, "mypkg", "mypkg", source_url=f"file://{pathlib.Path(t) / 'mypkg'}")

        assert _by_name(d.check(base, run=_runner(), site_packages_dirs=[sp]))[_STRAY_CHECK]["ok"]


def test_ordinary_pypi_install_with_no_direct_url_is_not_flagged():
    """A dist-info with no `direct_url.json` at all (the common case — pip omits it unless the
    install came from a local path or VCS) must never be flagged, even if its name happens to
    collide with a directory that exists in this worktree for unrelated reasons."""
    d = _doc()
    with tempfile.TemporaryDirectory() as t, tempfile.TemporaryDirectory() as sp:
        base = _sdlc(t, {})
        (pathlib.Path(t) / "pyproject.toml").write_text("[project]\nname = 'mypkg'\n")
        (pathlib.Path(t) / "requests").mkdir()   # coincidental same-name local dir
        _fake_dist_info(sp, "requests", "requests", source_url=None)

        assert _by_name(d.check(base, run=_runner(), site_packages_dirs=[sp]))[_STRAY_CHECK]["ok"]


def test_no_check_emitted_when_worktree_has_no_python_project():
    d = _doc()
    with tempfile.TemporaryDirectory() as t, tempfile.TemporaryDirectory() as sp:
        base = _sdlc(t, {})
        assert _STRAY_CHECK not in _by_name(d.check(base, run=_runner(), site_packages_dirs=[sp]))


def test_python_project_one_level_down_still_gates_the_check_on():
    """A package one level down (`<pkg>/pyproject.toml`), not a root-level `pyproject.toml`."""
    d = _doc()
    with tempfile.TemporaryDirectory() as t, tempfile.TemporaryDirectory() as sp:
        base = _sdlc(t, {})
        (pathlib.Path(t) / "sub").mkdir()
        (pathlib.Path(t) / "sub" / "pyproject.toml").write_text("[project]\nname = 'mypkg'\n")
        assert _STRAY_CHECK in _by_name(d.check(base, run=_runner(), site_packages_dirs=[sp]))


# ---------------------------------------------------------------- watcher health (#1509)
# A status file nobody looks at is not a health signal. #1509 makes a watcher write one;
# this row is what puts it in front of a human. The STALE case is the one that matters: a watcher
# that DIED leaves a healthy-looking file behind — zero failures, no error — with only its
# last-success time drifting into the past. That is exactly what the private-side shipper looked like for the
# five days it shipped a frozen store.


def _dr():
    import importlib.util, pathlib as _p
    spec = importlib.util.spec_from_file_location(
        "doctor_1509", _p.Path(__file__).resolve().parent.parent
        / "skills" / "sigma-doctor" / "scripts" / "doctor.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ---------------------------------------------------------------- ledger watcher health
# _ledger_feature_state only ever checks that .sdlc/ledger/.git exists -- a one-time, permanent-
# once-true fact, not a freshness one. It reported "published" for 18+ days while the one watcher
# that ever ran (#1293) sat with a live pid and zero progress. Mirrors the watcher-health block
# above exactly, against watch.sh's own heartbeat file instead of a status JSON.


def _touch(path, mtime):
    path.write_text("")
    os.utime(path, (mtime, mtime))


def test_doctor_says_ledger_watcher_off_when_not_enabled(tmp_path):
    assert "off" in _dr()._ledger_watcher_state(tmp_path, {}).lower()


def test_doctor_flags_ledger_watcher_enabled_but_never_run(tmp_path):
    state = _dr()._ledger_watcher_state(tmp_path, {"ledger": {"enabled": True}})
    assert "NEVER RUN" in state


def test_doctor_flags_ledger_watcher_dead_with_pid_but_no_heartbeat(tmp_path):
    """THE ASSERTION THAT MATTERS. This is this repo's actual, currently-live bug shape: a pid
    file (predating #1293, or a crash before its first heartbeat write) with no heartbeat at
    all -- distinguishable from "never run" only by the pid file's presence."""
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "watch.pid").write_text("12345")
    state = _dr()._ledger_watcher_state(tmp_path, {"ledger": {"enabled": True}})
    assert "DEAD" in state


def test_doctor_flags_a_STALE_ledger_watcher(tmp_path):
    (tmp_path / "state").mkdir()
    _touch(tmp_path / "state" / "watch.heartbeat", 1000.0)
    state = _dr()._ledger_watcher_state(tmp_path, {"ledger": {"enabled": True}},
                                        now=1000.0 + 2 * 3600)
    assert "STALE" in state and "2.0h" in state


def test_doctor_reports_a_healthy_ledger_watcher(tmp_path):
    (tmp_path / "state").mkdir()
    _touch(tmp_path / "state" / "watch.heartbeat", 1000.0)
    state = _dr()._ledger_watcher_state(tmp_path, {"ledger": {"enabled": True}}, now=1000.0 + 600)
    assert state.startswith("ON —") and "10 min ago" in state


def test_doctor_ledger_watcher_respects_configured_interval(tmp_path):
    """stale_after must track ledger.watch.interval_seconds, not a hardcoded number -- a slower-
    configured watcher must not be flagged dead just for sleeping its own configured interval."""
    (tmp_path / "state").mkdir()
    _touch(tmp_path / "state" / "watch.heartbeat", 1000.0)
    cfg = {"ledger": {"enabled": True, "watch": {"interval_seconds": 3600}}}
    state = _dr()._ledger_watcher_state(tmp_path, cfg, now=1000.0 + 2 * 3600)
    assert state.startswith("ON —")      # 2h < stale_after (3 * 3600s interval)


# ---------------------------------------------------------------- #2499: watch.log size on the ON row
# watch_daemon.py rolls state/watch.log to ONE predecessor at the start of a tick once it reaches
# ledger.watch.log_max_bytes (sync.watch_log_cap_bytes, disk-clamped). A healthy rotator therefore
# leaves the file at up to cap + one tick for a whole interval, so only >= 2 x cap proves a missed
# roll; and only the ON row carries the note -- a STALE/DEAD/NEVER RUN verdict already says the
# watcher is not running, and a size clause there would bury it.

_CAP_CFG = {"ledger": {"enabled": True, "watch": {"log_max_bytes": 1024}}}


def _watcher_state_with_log(tmp_path, size, heartbeat_at=1000.0, now=1000.0 + 600, pid=False):
    (tmp_path / "state").mkdir(exist_ok=True)
    if heartbeat_at is not None:
        _touch(tmp_path / "state" / "watch.heartbeat", heartbeat_at)
    if pid:
        (tmp_path / "state" / "watch.pid").write_text("12345")
    if size is not None:
        (tmp_path / "state" / "watch.log").write_bytes(b"x" * size)
    return _dr()._ledger_watcher_state(tmp_path, _CAP_CFG, now=now)


def test_doctor_watcher_row_is_bare_on_just_after_a_healthy_roll(tmp_path):
    """F-1 non-vacuity half: a fresh heartbeat with the log at cap + 100 B is exactly what a HEALTHY
    rotator looks like for one whole interval after the tick that crossed the cap -- the row must be
    the bare `ON — …` string, with no size clause. A `size > cap` rule fabricates a fault here."""
    import re
    row = _watcher_state_with_log(tmp_path, 1024 + 100)
    assert re.fullmatch(r"ON — last heartbeat \d+ min ago", row), row


def test_doctor_watcher_row_names_log_size_at_twice_the_cap(tmp_path):
    row = _watcher_state_with_log(tmp_path, 2048)
    assert row.startswith("ON — ")
    assert "watch.log" in row and "exceeds" in row
    assert "2.0 KiB" in row and "1.0 KiB" in row          # adaptive units: never a "0.0 MiB" cap
    assert "0.0" not in row


def test_doctor_watcher_row_silent_about_log_under_bound(tmp_path):
    import re
    assert re.fullmatch(r"ON — last heartbeat \d+ min ago", _watcher_state_with_log(tmp_path, 100))
    for stray in ("watch.log",):
        (tmp_path / "state" / stray).unlink()
    assert re.fullmatch(r"ON — last heartbeat \d+ min ago", _watcher_state_with_log(tmp_path, None))


def test_doctor_size_note_only_on_the_on_row(tmp_path):
    """A 4 KB log (4 x cap) with (a) a stale heartbeat, (b) a pid file and no heartbeat, (c) neither:
    the verdict prefix is unchanged and none of the three rows mentions the log."""
    stale = _watcher_state_with_log(tmp_path, 4096, heartbeat_at=1000.0, now=1000.0 + 2 * 3600)
    assert stale.startswith("STALE") and "watch.log" not in stale
    (tmp_path / "state" / "watch.heartbeat").unlink()
    dead = _watcher_state_with_log(tmp_path, 4096, heartbeat_at=None, pid=True)
    assert dead.startswith("DEAD") and "watch.log" not in dead
    (tmp_path / "state" / "watch.pid").unlink()
    never = _watcher_state_with_log(tmp_path, 4096, heartbeat_at=None)
    assert "NEVER RUN" in never and "watch.log" not in never


def test_fmt_size_units():
    d = _dr()
    assert d._fmt_size(512) == "512 B"
    assert d._fmt_size(1024) == "1.0 KiB"
    assert d._fmt_size(1048576) == "1.0 MiB"
    assert d._fmt_size(3 * 1048576 + 200 * 1024) == "3.2 MiB"


def test_doctor_features_cli_reports_oversized_watch_log(tmp_path):
    """The documented gesture -- `python3 doctor.py features <sdlc>` -- against a fresh heartbeat
    (touched now) and a 4 KB log over a 1 KiB cap: the `ledger watcher` line says `exceeds`."""
    import subprocess
    base = tmp_path / ".sdlc"
    (base / "state").mkdir(parents=True)
    base.joinpath("config.json").write_text(json.dumps(_CAP_CFG))
    (base / "state" / "watch.heartbeat").touch()
    (base / "state" / "watch.log").write_bytes(b"x" * 4096)
    p = subprocess.run([sys.executable, str(D), "features", str(base)], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    line = next(l for l in p.stdout.splitlines() if "ledger watcher" in l)
    assert "exceeds" in line and "watch.log" in line, line


# ---------------------------------------------------------------- slack-commands listener liveness (#2339)
# Component F (`.sdlc/design/2329.md`): "doctor.py gains a 'slack-commands listener wired up' check
# mirroring _autowatch_adapter_wired ... configured (a channel_id and both token envs set) but the
# heartbeat stale or absent reads as 'configured but not running' -- a component that has died must
# read differently from one with nothing to do, per this repo's own LIVENESS bar." Mirrors
# _ledger_watcher_state's exact age-based shape (see block above) against slack_commands_listen.py's
# own JSON heartbeat (write_heartbeat: {"pid": ..., "last_seen": ...}) rather than watch.sh's
# bare-touch file -- the write path differs, the freshness reasoning does not.


def _sc_config(channel="C1111111", app_env="SC_APP_ENV_T", bot_env="SC_BOT_ENV_T", enabled=True):
    return {"slack_commands": {"enabled": enabled, "channel_id": channel,
                                "app_token_env": app_env, "bot_token_env": bot_env}}


def _write_sc_heartbeat(base, pid, last_seen):
    state = pathlib.Path(base) / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "slack-commands.heartbeat.json").write_text(json.dumps({"pid": pid, "last_seen": last_seen}))


def test_doctor_says_slack_commands_off_when_not_configured(tmp_path):
    assert "off" in _dr()._slack_commands_listener_state(tmp_path, {}).lower()


def test_doctor_says_slack_commands_off_when_enabled_is_a_truthy_string(tmp_path):
    """Strict `is True`, matching sc.enabled()'s own truth table (test_slack_commands_listen.py's
    test_enabled_rejects_a_truthy_string_not_real_true) -- doctor must not disagree with the
    listener's own gate about what counts as "on"."""
    cfg = {"slack_commands": {"enabled": "true", "channel_id": "C1"}}
    assert "off" in _dr()._slack_commands_listener_state(tmp_path, cfg).lower()


def test_doctor_flags_slack_commands_misconfigured_without_channel_id(tmp_path, monkeypatch):
    monkeypatch.setenv("SC_APP_ENV_T", "xapp-1")
    monkeypatch.setenv("SC_BOT_ENV_T", "xoxb-1")
    state = _dr()._slack_commands_listener_state(tmp_path, _sc_config(channel=None))
    assert "MISCONFIGURED" in state and "channel_id" in state


def test_doctor_flags_slack_commands_missing_env_vars(tmp_path, monkeypatch):
    monkeypatch.delenv("SC_APP_ENV_T", raising=False)
    monkeypatch.delenv("SC_BOT_ENV_T", raising=False)
    state = _dr()._slack_commands_listener_state(tmp_path, _sc_config())
    assert "MISCONFIGURED" in state and "SC_APP_ENV_T" in state and "SC_BOT_ENV_T" in state


def test_doctor_flags_slack_commands_never_run(tmp_path, monkeypatch):
    monkeypatch.setenv("SC_APP_ENV_T", "xapp-1")
    monkeypatch.setenv("SC_BOT_ENV_T", "xoxb-1")
    state = _dr()._slack_commands_listener_state(tmp_path, _sc_config())
    assert "NEVER RUN" in state


def test_doctor_flags_slack_commands_dead_with_pid_but_no_heartbeat(tmp_path, monkeypatch):
    """THE ASSERTION THAT MATTERS, mirroring the ledger-watcher case this design cites as its own
    precedent: a pidfile with no heartbeat at all must be distinguishable from "never run"."""
    monkeypatch.setenv("SC_APP_ENV_T", "xapp-1")
    monkeypatch.setenv("SC_BOT_ENV_T", "xoxb-1")
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "slack-commands.pid").write_text("12345")
    state = _dr()._slack_commands_listener_state(tmp_path, _sc_config())
    assert "DEAD" in state


def test_doctor_flags_a_stale_slack_commands_listener(tmp_path, monkeypatch):
    monkeypatch.setenv("SC_APP_ENV_T", "xapp-1")
    monkeypatch.setenv("SC_BOT_ENV_T", "xoxb-1")
    _write_sc_heartbeat(tmp_path, 111, 1000.0)
    state = _dr()._slack_commands_listener_state(tmp_path, _sc_config(), now=1000.0 + 2 * 3600)
    assert "STALE" in state and "2.0h" in state


def test_doctor_reports_a_healthy_slack_commands_listener(tmp_path, monkeypatch):
    monkeypatch.setenv("SC_APP_ENV_T", "xapp-1")
    monkeypatch.setenv("SC_BOT_ENV_T", "xoxb-1")
    _write_sc_heartbeat(tmp_path, 111, 1000.0)
    state = _dr()._slack_commands_listener_state(tmp_path, _sc_config(), now=1000.0 + 60)
    assert state.startswith("ON —") and "1 min ago" in state


def test_doctor_slack_commands_custom_token_env_names_are_honored(tmp_path, monkeypatch):
    """A repo that overrides app_token_env/bot_token_env in config must be checked against THOSE
    names, not the hardcoded defaults -- mirrors sc.app_token_env()/sc.bot_token_env()'s own
    config-override-with-default-fallback behavior."""
    monkeypatch.delenv("SIGMA_SLACK_BOT_SOCKET_TOKEN", raising=False)
    monkeypatch.delenv("SIGMA_SLACK_BOT_TOKEN", raising=False)
    monkeypatch.setenv("MY_APP_TOK", "xapp-1")
    monkeypatch.setenv("MY_BOT_TOK", "xoxb-1")
    cfg = _sc_config(app_env="MY_APP_TOK", bot_env="MY_BOT_TOK")
    state = _dr()._slack_commands_listener_state(tmp_path, cfg)
    assert "MISCONFIGURED" not in state


def test_check_omits_slack_commands_row_when_disabled():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {})
        assert "slack-commands listener wired up" not in _by_name(d.check(base, run=_runner()))


def test_check_warns_when_slack_commands_enabled_but_not_running(monkeypatch):
    d = _doc()
    monkeypatch.setenv("SC_APP_ENV_T", "xapp-1")
    monkeypatch.setenv("SC_BOT_ENV_T", "xoxb-1")
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, _sc_config())
        c = _by_name(d.check(base, run=_runner()))["slack-commands listener wired up"]
        assert c["ok"] is False
        assert "SLACK_COMMANDS.md" in c["fix"]


def test_check_passes_when_slack_commands_listener_is_actually_running(monkeypatch):
    d = _doc()
    monkeypatch.setenv("SC_APP_ENV_T", "xapp-1")
    monkeypatch.setenv("SC_BOT_ENV_T", "xoxb-1")
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, _sc_config())
        _write_sc_heartbeat(base, os.getpid(), time.time())
        c = _by_name(d.check(base, run=_runner()))["slack-commands listener wired up"]
        assert c["ok"] is True


def test_features_lists_slack_commands_listener_state():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {})
        rows = {name: state for name, state, _ in d.features(base)}
        assert "off" in rows["slack-commands listener (inbound Slack commands)"].lower()


def test_features_slack_commands_listener_reports_stale_when_dead(monkeypatch):
    d = _doc()
    monkeypatch.setenv("SC_APP_ENV_T", "xapp-1")
    monkeypatch.setenv("SC_BOT_ENV_T", "xoxb-1")
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, _sc_config())
        _write_sc_heartbeat(base, 111, time.time() - 10000)
        rows = {name: state for name, state, _ in d.features(base)}
        assert "STALE" in rows["slack-commands listener (inbound Slack commands)"]


# --- #1456: hand-off owner resolution has no source, so hand-off views can never populate ------
# handoff.py resolves a recipient from the repo's own CODEOWNERS (or config ledger.owners). With
# neither present EVERY hand-off resolves to "(unowned)" and is recorded with no recipient, which a
# hand-off-graph reader discards. handoff.py warns at the moment of
# the hand-off, but by then the row is already written and nobody re-reads that stderr line.

_OWNER_ROW = "hand-off owner roster configured"


def test_flags_handoffs_with_no_owner_source_at_all():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"ledger": {"enabled": True}})          # no CODEOWNERS, no ledger.owners
        c = _by_name(d.check(base, run=_runner()))
        assert c[_OWNER_ROW]["ok"] is False
        assert "CODEOWNERS" in c[_OWNER_ROW]["fix"]


def test_owner_check_passes_with_a_codeowners_file():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"ledger": {"enabled": True}})
        root = pathlib.Path(base).parent
        (root / ".github").mkdir(parents=True, exist_ok=True)
        (root / ".github" / "CODEOWNERS").write_text("web/  @someone\n")
        c = _by_name(d.check(base, run=_runner()))
        assert c[_OWNER_ROW]["ok"] is True


def test_owner_check_passes_with_a_config_owners_map():
    """`ledger.owners` is the documented alternative for a team whose layout doesn't map to paths
    (owners.py:103, 'An explicit ledger.owners map in config always wins')."""
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"ledger": {"enabled": True, "owners": {"web": "@someone"}}})
        c = _by_name(d.check(base, run=_runner()))
        assert c[_OWNER_ROW]["ok"] is True


def test_no_owner_check_when_the_ledger_is_off():
    """Hand-offs are a ledger feature; an adopter without one should not be told to write a roster."""
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"ledger": {"enabled": False}})
        assert _OWNER_ROW not in _by_name(d.check(base, run=_runner()))
# --- #1484: a hand-written north-star with no tier headings must not read as "filled" ----------
# _NORTH_STAR_TIERS lists PLACEHOLDER strings from /sigma-init's scaffold. That answers "does any
# placeholder survive?", which is right for a scaffolded-then-edited file and wrong for a
# hand-written one: with nothing to find, every tier read as complete. This repo's own north-star
# has zero of the four tier headings and doctor reported OK.

_NS_ROW = "north-star filled"


def _north_star(base, text):
    ns = pathlib.Path(base) / "context"; ns.mkdir(parents=True, exist_ok=True)
    (ns / "north-star.md").write_text(text, encoding="utf-8")


_COMPLETE_NS = """# North star
## Vision
Make the thing good.
## Strategy
### Priorities
Ship the loop.
### Non-goals
Not a SaaS.
## Design
Terminal-first.
## Architecture
1. The UI layer holds no business logic.
2. Dependencies point inward.
"""


def test_north_star_with_no_tier_headings_is_not_filled():
    """THE bug. A rules-only stub carrying no placeholders passed, because there was nothing left
    to find. Modelled on this repo's real file: a heading, prose, and standing rules -- no tiers."""
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {})
        _north_star(base, "# North star\n\nRULES-ONLY STUB, no tiers yet.\n\n## Standing rules\n\n"
                          "### 1. A claim in prose is not evidence.\n\nExecute it.\n")
        c = _by_name(d.check(base, run=_runner()))
        assert c[_NS_ROW]["ok"] is False
        assert "Vision" in c[_NS_ROW]["fix"]


def test_north_star_missing_only_one_tier_is_not_filled():
    """#358's 'every tier must clear' rule, now enforced by heading too."""
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {})
        _north_star(base, _COMPLETE_NS.replace("## Architecture", "## Somethingelse"))
        c = _by_name(d.check(base, run=_runner()))
        assert c[_NS_ROW]["ok"] is False
        assert "Architecture" in c[_NS_ROW]["fix"]


def test_a_complete_north_star_still_passes():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {})
        _north_star(base, _COMPLETE_NS)
        assert _by_name(d.check(base, run=_runner()))[_NS_ROW]["ok"] is True


def test_placeholder_detection_still_fires_on_a_scaffolded_file():
    """The ORIGINAL behaviour (#358/#445) must survive: headings present but placeholders intact
    is still unfilled."""
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {})
        _north_star(base, _COMPLETE_NS.replace("Make the thing good.", "<the change you want to see"))
        c = _by_name(d.check(base, run=_runner()))
        assert c[_NS_ROW]["ok"] is False
        assert "Vision" in c[_NS_ROW]["fix"]


# --- #1555: secret-file coverage, reported BEFORE the loop ever commits --------------------------


def _secret_runner(ls_files="", fail=False, real=False):
    """A runner that answers only the `ls-files --others --exclude-standard` probe and stays silent
    (hermetic) on every other one. `fail=True` returns doctor's own falsy-with-`.raw` failure object
    — what `_real_run` produces outside a git repo — so the "could not answer" path is reached the
    way production reaches it. `real=True` delegates that ONE probe to the production runner, so a
    real-git test pays for git and for nothing else (no `gh`, no `claude plugin list`)."""
    doc = _doc()

    def run(args):
        if "ls-files" not in args:
            return ""
        if fail:
            return doc._RawFailure("fatal: not a git repository")
        return doc._real_run(args) if real else ls_files
    return run


def test_check_reports_secret_coverage_as_ok_when_nothing_unignored_matches():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {})
        c = _by_name(d.check(base, run=_secret_runner("src/app.py\nREADME.md\n.env.example\n")))
        assert c[d._SECRET_COVERAGE_ROW]["ok"] is True
        assert c[d._SECRET_COVERAGE_ROW]["fix"] == ""


def test_check_flags_an_unignored_dotenv_before_the_loop_ever_commits():
    """The whole point of doing this at setup time: `work.commit()`'s guard is the LAST line, and
    hitting it stops an unattended run. This row is readable from a command an adopter already runs,
    and it names the same two remedies the refusal would."""
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {})
        c = _by_name(d.check(base, run=_secret_runner("src/app.py\n.env\ncerts/prod.pem\n")))
        row = c[d._SECRET_COVERAGE_ROW]
        assert row["ok"] is False
        assert ".env" in row["fix"] and "certs/prod.pem" in row["fix"]
        assert ".gitignore" in row["fix"] and "allow_secret_paths" in row["fix"]
        assert "src/app.py" not in row["fix"]


def test_check_secret_coverage_honours_the_commit_guards_own_allowlist():
    """A path the adopter has deliberately allowed must stop being reported here too. Otherwise the
    row can never be driven to green, and a check nobody can clear gets ignored along with its true
    positives."""
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"work": {"allow_secret_paths": ["tests/fixtures/rsa_test.key"]}})
        c = _by_name(d.check(base, run=_secret_runner("tests/fixtures/rsa_test.key\n")))
        assert c[d._SECRET_COVERAGE_ROW]["ok"] is True


def test_check_omits_the_secret_coverage_row_entirely_when_git_cannot_answer():
    """"git failed" is not "nothing found". Reporting an all-clear for a directory that is not even
    a git repo would be a false assurance about the one thing this row exists to give."""
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {})
        assert d._SECRET_COVERAGE_ROW not in _by_name(d.check(base, run=_secret_runner(fail=True)))


def test_the_doctor_carries_no_copy_of_the_commit_guards_denylist():
    """Half of the divergence guard, stated where a copy would have to appear: in this file's own
    source. A doctor-local denylist would report coverage against one set of patterns while
    `work.commit()` refused against another, and every drift between them is either a false
    all-clear here or a warning about a file that would have committed fine."""
    source = D.read_text(encoding="utf-8")
    for spelled in ('".pem"', '".key"', '"id_rsa"', '"credentials.json"', '".env"'):
        assert spelled not in source, f"doctor.py spells {spelled} itself — cross-load work.py instead"


def test_the_doctor_row_agrees_with_the_commit_guard_on_every_name():
    """The other half, behavioural: whatever `work._is_offender` says, the row says. Driven from
    that function rather than from a second hand-written expectation, so the two cannot drift apart
    without this failing.

    #1577: driven from `_is_offender` rather than `_secret_shaped`, which is the WHOLE predicate
    `commit()` refuses on. The narrower one left the registry exemption out of the comparison, and
    a divergence guard that omits a term cannot see a divergence in it."""
    d = _doc()
    work = d._load_loop_script("work")
    names = [".env", ".env.example", ".env.production", "env.sh", "id_rsa", "id_rsa.pub", "a.pem",
             "b.key", "credentials.json", "package.json", "README.md", "svc-service-account.json",
             ".sdlc/features/units/credentials.json", ".sdlc/features/units/.env"]
    found = d._secret_file_coverage(".", {}, lambda _a: "\n".join(names))
    assert found == sorted(n for n in names if work._is_offender(n, frozenset()))
    assert found and len(found) < len(names)      # the table is discriminating, not all-or-nothing


def test_the_row_does_not_ask_an_adopter_to_gitignore_their_own_feature_registry():
    """#1577 item 3. A unit named `credentials` (or `service-account`, or `client_secret-anything`)
    gets a shard at `.sdlc/features/units/<name>.json`, which the denylist read as a service-account
    download — and the row's first remedy is "add each to .gitignore", the one thing that must not
    happen to a registry every participating repo has to hold.

    The row is asserted GREEN rather than merely re-worded: `.sdlc/features/` is not gitignored, so
    the shard is untracked-and-unignored until the adopter commits it, and until then a re-worded
    row is still a red row nobody can clear — which this function's own docstring says is how a
    check earns being ignored along with its true positives."""
    d = _doc()
    assert d._secret_file_coverage(
        ".", {}, lambda _a: ".sdlc/features/units/credentials.json\n"
                            ".sdlc/features/units/service-account.json\n") == []


def test_real_git_the_doctor_row_flags_an_unignored_dotenv_then_the_ignore_clears_it(tmp_path):
    """Against real git, because `--exclude-standard` is the entire mechanism: the row's claim is
    "your ignore rules already cover this", and only git can say whether they do. A stub answering
    from a fixture would agree just as happily with an implementation that never passed
    `--exclude-standard` at all — and that mutant is exactly the false all-clear this row exists to
    prevent."""
    import subprocess
    d = _doc()
    repo = tmp_path / "repo"
    (repo / ".sdlc").mkdir(parents=True)
    (repo / ".sdlc" / "config.json").write_text("{}")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".env").write_text("TOKEN=live\n")
    (repo / ".env.example").write_text("TOKEN=\n")
    (repo / "app.py").write_text("x = 1\n")
    base = str(repo / ".sdlc")

    row = _by_name(d.check(base, run=_secret_runner(real=True)))[d._SECRET_COVERAGE_ROW]
    assert row["ok"] is False
    assert ".env" in row["fix"]

    (repo / ".gitignore").write_text(".env\n")
    row = _by_name(d.check(base, run=_secret_runner(real=True)))[d._SECRET_COVERAGE_ROW]
    assert row["ok"] is True, row["fix"]      # .env.example is unignored and still not a secret


# --- install scoping: a project-scope entry shadows user scope for its own path (#1604) --------
#
# `~/.claude/plugins/installed_plugins.json` records one entry PER SCOPE. `user` applies
# everywhere; `project` (and `local`) carry a `projectPath` and shadow user scope for that path
# alone. So a directory can sit on an old version indefinitely while `claude plugin update --scope
# user` reports success — measured on a real machine as a main checkout pinned to 1.1.2 while
# twelve other paths were at 1.3.7, in the one directory where AGENTS.md forbids starting the loop.

_SCOPE_PREFIX = "sigma install scopes"
_PID = "sigmaloop@sigmaloop"


def _scope_row(checks):
    """The install-scoping row, or None when it was omitted — `None` is the assertion for every
    can't-see case, so this must never fall back to a synthesised row."""
    return next((c for c in checks if c["name"].startswith(_SCOPE_PREFIX)), None)


def _entry(scope="user", version="9.9.9", project_path=None, **extra):
    """One installed_plugins.json entry, shaped like the real file: `projectPath` present only for
    a per-project scope, plus whatever future fields a caller wants to plant."""
    e = {"scope": scope, "version": version,
         "installPath": f"/cache/sigma/{version}", "installedAt": "2026-08-20T14:35:44.025Z"}
    if project_path is not None:
        e["projectPath"] = str(project_path)
    e.update(extra)
    return e


def _plugins_file(tmp, entries, pid=_PID, raw=None):
    p = pathlib.Path(tmp) / "installed_plugins.json"
    p.write_text(raw if raw is not None else json.dumps({"version": 2, "plugins": {pid: entries}}))
    return str(p)


def _one_below(floor):
    """A version string one increment below `floor` — derived, so this test keeps meaning the same
    thing on the day the floor moves."""
    parts = list(floor)
    for i in range(len(parts) - 1, -1, -1):
        if parts[i] > 0:
            parts[i] -= 1
            return ".".join(str(x) for x in parts)
    raise AssertionError("a 0.0.0 floor has nothing below it")


def _at_floor(floor):
    """The floor itself as a version string -- the `>=` boundary, derived like `_one_below`."""
    return ".".join(str(x) for x in floor)


def _one_above(floor):
    """A version string one increment above `floor` (last component bumped; 9.9.9 -> 9.9.10, which
    doctor's int-wise tuple compare orders correctly). Means 'comfortably healthy' at ANY floor."""
    parts = list(floor)
    parts[-1] += 1
    return ".".join(str(x) for x in parts)


def test_the_floor_is_derived_from_agents_md_not_copied_into_doctor():
    """THE ANTI-DRIFT PIN. The floor is stated in prose in AGENTS.md and nowhere else; doctor parses
    that sentence rather than holding a second copy of the number. This fails the day a rewording
    breaks the parse — which is the only way the hard stop could otherwise go quietly inert."""
    d = _doc()
    agents = (pathlib.Path(d.__file__).resolve().parent.parent.parent.parent / "AGENTS.md")
    assert agents.is_file(), "AGENTS.md must ship inside the plugin for the floor to be derivable"
    floor = d._agents_floor()
    assert floor is not None, "AGENTS.md no longer states the floor in a shape doctor can read"
    assert ".".join(str(x) for x in floor) in agents.read_text(encoding="utf-8")
    assert len(d._FLOOR_RE.findall(agents.read_text(encoding="utf-8"))) == 1, \
        "the floor must be stated exactly once — a second statement is a second copy to drift"


def test_an_unparseable_agents_md_yields_no_floor(tmp_path):
    d = _doc()
    doc = tmp_path / "AGENTS.md"
    doc.write_text("# rules\n\nKeep the plugin current, please.\n")
    assert d._agents_floor(str(doc)) is None
    assert d._agents_floor(str(tmp_path / "nope.md")) is None


def test_a_scope_below_the_floor_is_flagged_as_a_hard_stop(tmp_path):
    """The measured failure: a project-scope entry below the AGENTS.md floor, sitting there while
    user scope reports success."""
    d = _doc()
    base = _sdlc(tmp_path / "repo", {})
    stale = tmp_path / "other"; stale.mkdir()
    above = _one_above(d._agents_floor())
    plugins = _plugins_file(tmp_path, [
        _entry("user", above),
        _entry("project", _one_below(d._agents_floor()), stale),
    ])

    row = _scope_row(d.check(base, run=_runner(), installed_plugins_path=plugins))
    assert row is not None and row["ok"] is False
    floor = _at_floor(d._agents_floor())
    assert f"1 below the AGENTS.md floor {floor}" in row["name"]
    assert str(stale) in row["fix"]


def test_at_the_floor_exactly_is_not_flagged(tmp_path):
    """`>=`, not `>` — AGENTS.md forbids running BELOW the floor, so the floor itself is fine."""
    d = _doc()
    base = _sdlc(tmp_path / "repo", {})
    at = _at_floor(d._agents_floor())
    plugins = _plugins_file(tmp_path, [_entry("user", at)])

    row = _scope_row(d.check(base, run=_runner(), installed_plugins_path=plugins))
    assert row is not None and row["ok"] is True, row["fix"]


def test_a_foreign_project_path_is_reported_as_not_fixable_from_here(tmp_path):
    """The entire point of the row. `update-sigma.sh` already skips another project's entry by
    design and says nothing; doctor has to be the thing that says it — naming the path, saying the
    gesture must be run from there, and naming REMOVAL rather than telling anyone to maintain N
    installs forever."""
    d = _doc()
    base = _sdlc(tmp_path / "repo", {})
    elsewhere = tmp_path / "elsewhere"; elsewhere.mkdir()
    above = _one_above(d._agents_floor())
    plugins = _plugins_file(tmp_path, [
        _entry("user", above),
        _entry("project", _one_below(d._agents_floor()), elsewhere),
    ])

    row = _scope_row(d.check(base, run=_runner(), installed_plugins_path=plugins))
    assert row["ok"] is False
    assert "NOT fixable from here" in row["fix"]
    assert str(elsewhere) in row["fix"]
    assert f"claude plugin uninstall {_PID} --scope project" in row["fix"]


def test_this_repos_own_project_scope_is_reported_as_fixable_from_here(tmp_path):
    """The other half of the distinction: an override recorded for THIS repo root can be acted on
    where the user already is, so it must not be filed under 'you must go there'."""
    d = _doc()
    repo = tmp_path / "repo"
    base = _sdlc(repo, {})
    above = _one_above(d._agents_floor())
    plugins = _plugins_file(tmp_path, [
        _entry("user", above),
        _entry("project", _one_below(d._agents_floor()), repo),
    ])

    row = _scope_row(d.check(base, run=_runner(), installed_plugins_path=plugins))
    assert row["ok"] is False
    assert "fixable from THIS directory" in row["fix"]
    assert "NOT fixable from here" not in row["fix"]


def test_a_stale_override_for_this_repo_is_offered_removal_not_only_an_update(tmp_path):
    """Same advice on both halves of the distinction. An override that is stale AND redundant is a
    place a stale version will hide again next release; updating it keeps N installs to maintain,
    removing it does not. Only offered when user scope actually clears the floor — otherwise
    inheriting from it would be inheriting another stale version."""
    d = _doc()
    repo = tmp_path / "repo"
    base = _sdlc(repo, {})
    above = _one_above(d._agents_floor())
    plugins = _plugins_file(tmp_path, [
        _entry("user", above),
        _entry("project", _one_below(d._agents_floor()), repo),
    ])

    fix = _scope_row(d.check(base, run=_runner(), installed_plugins_path=plugins))["fix"]
    assert f"claude plugin update {_PID} --scope project" in fix
    assert f"claude plugin uninstall {_PID} --scope project" in fix
    assert above in fix                       # names the version the path would inherit


def test_removal_is_not_offered_here_when_user_scope_is_stale_too(tmp_path):
    """Inheriting from a user scope that is itself below the floor fixes nothing."""
    d = _doc()
    repo = tmp_path / "repo"
    base = _sdlc(repo, {})
    below = _one_below(d._agents_floor())
    plugins = _plugins_file(tmp_path, [_entry("user", below), _entry("project", below, repo)])

    fix = _scope_row(d.check(base, run=_runner(), installed_plugins_path=plugins))["fix"]
    assert f"claude plugin uninstall {_PID} --scope project` here" not in fix


def test_every_stale_scope_reachable_from_here_gets_its_own_command(tmp_path):
    """One command naming one scope would leave the other silently stale — the exact failure this
    row exists to end."""
    d = _doc()
    repo = tmp_path / "repo"
    base = _sdlc(repo, {})
    below = _one_below(d._agents_floor())
    plugins = _plugins_file(tmp_path, [_entry("user", below), _entry("project", below, repo)])

    fix = _scope_row(d.check(base, run=_runner(), installed_plugins_path=plugins))["fix"]
    assert f"claude plugin update {_PID} --scope user" in fix
    assert f"claude plugin update {_PID} --scope project" in fix


def test_user_scope_below_the_floor_is_fixable_from_here(tmp_path):
    """`--scope user` works from anywhere, so a stale user scope is always this run's to fix."""
    d = _doc()
    base = _sdlc(tmp_path / "repo", {})
    plugins = _plugins_file(tmp_path, [_entry("user", _one_below(d._agents_floor()))])

    row = _scope_row(d.check(base, run=_runner(), installed_plugins_path=plugins))
    assert row["ok"] is False
    assert "fixable from THIS directory" in row["fix"]
    assert f"claude plugin update {_PID} --scope user" in row["fix"]


def test_the_healthy_no_project_scope_case_reads_healthy_and_says_so(tmp_path):
    """The common case. Silence would be indistinguishable from the can't-see case below, so this
    says the reassuring thing out loud: user scope, and nothing that can shadow it."""
    d = _doc()
    base = _sdlc(tmp_path / "repo", {})
    above = _one_above(d._agents_floor())
    plugins = _plugins_file(tmp_path, [_entry("user", above)])

    row = _scope_row(d.check(base, run=_runner(), installed_plugins_path=plugins))
    assert row is not None and row["ok"] is True, row["fix"]
    assert "no project-scope overrides" in row["name"]
    assert f"user {above}" in row["name"]


def test_current_project_scopes_read_healthy_and_still_name_the_removal_gesture(tmp_path):
    """A redundant override is not a failure — but it IS a place a stale version can hide later, and
    the advice has to be 'delete it', never 'keep N installs in step'."""
    d = _doc()
    base = _sdlc(tmp_path / "repo", {})
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
    above = _one_above(d._agents_floor())
    plugins = _plugins_file(tmp_path, [
        _entry("user", above),
        _entry("project", above, tmp_path / "a"),
        _entry("project", above, tmp_path / "b"),
    ])

    row = _scope_row(d.check(base, run=_runner(), installed_plugins_path=plugins))
    assert row["ok"] is True, row["fix"]
    assert "2 project-scope override(s)" in row["name"]
    assert f"claude plugin uninstall {_PID} --scope project" in row["name"]


def test_an_override_ahead_of_user_scope_is_not_called_redundant(tmp_path):
    """Removal restores inheritance from user scope — which would be a DOWNGRADE for an override
    that is ahead of it. Advice that silently downgrades someone is worse than no advice."""
    d = _doc()
    base = _sdlc(tmp_path / "repo", {})
    (tmp_path / "a").mkdir()
    at = _at_floor(d._agents_floor())
    plugins = _plugins_file(tmp_path, [
        _entry("user", at),
        _entry("project", _one_above(d._agents_floor()), tmp_path / "a"),
    ])

    row = _scope_row(d.check(base, run=_runner(), installed_plugins_path=plugins))
    assert row["ok"] is True, row["fix"]
    assert "uninstall" not in row["name"]


def test_a_dead_project_path_is_counted_not_treated_as_a_hard_stop(tmp_path):
    """Worktree-per-goal is the workflow the kit recommends, so deleted worktrees leave entries
    behind. Such an entry governs nothing and CANNOT be cleared (the directory to run the uninstall
    from is gone) — flagging it would be a permanently red row, which is how a check earns being
    ignored along with its true positives."""
    d = _doc()
    base = _sdlc(tmp_path / "repo", {})
    above = _one_above(d._agents_floor())
    plugins = _plugins_file(tmp_path, [
        _entry("user", above),
        _entry("project", _one_below(d._agents_floor()), tmp_path / "deleted-worktree"),
    ])

    row = _scope_row(d.check(base, run=_runner(), installed_plugins_path=plugins))
    assert row["ok"] is True, row["fix"]
    assert "no longer exists" in row["name"]


def test_a_malformed_installed_plugins_omits_the_row_rather_than_reading_healthy(tmp_path):
    """Fail closed on visibility, exactly like `_secret_file_coverage`: a file we cannot parse tells
    us nothing, and a green row that means 'we did not look' is the worst of the three outcomes."""
    d = _doc()
    base = _sdlc(tmp_path / "repo", {})
    for raw in ("{ not json at all", "[]", '{"plugins": "nope"}', '{"plugins": {}}'):
        plugins = _plugins_file(tmp_path, None, raw=raw)
        assert _scope_row(d.check(base, run=_runner(), installed_plugins_path=plugins)) is None, raw


def test_install_scopes_says_none_not_empty_when_it_cannot_tell(tmp_path):
    """The helper's own contract, pinned separately from the row's. "Cannot tell" and "installed
    nowhere" are different facts, and `None` is the only one of the two this file's fail-closed
    convention can act on — an empty list would let a future caller render a census of zero scopes
    as a clean bill of health."""
    d = _doc()
    assert d._install_scopes(str(tmp_path / "nowhere.json")) is None
    assert d._install_scopes(_plugins_file(tmp_path, None, raw="{ not json")) is None
    assert d._install_scopes(_plugins_file(tmp_path, None, raw='{"plugins": {}}')) is None
    assert d._install_scopes(_plugins_file(tmp_path, [])) is None
    got = d._install_scopes(_plugins_file(tmp_path, [_entry("user", "1.4.1")]))
    assert got == [("sigmaloop@sigmaloop", "user", (1, 4, 1), None)]


def test_a_missing_installed_plugins_omits_the_row(tmp_path):
    d = _doc()
    base = _sdlc(tmp_path / "repo", {})
    gone = str(tmp_path / "nowhere" / "installed_plugins.json")
    assert _scope_row(d.check(base, run=_runner(), installed_plugins_path=gone)) is None


def test_an_unreadable_floor_omits_the_row(tmp_path):
    """Same convention, other input: with no floor there is nothing to judge against, and a row
    that enumerates scopes under an implied all-clear is the false-healthy this must never be."""
    d = _doc()
    base = _sdlc(tmp_path / "repo", {})
    plugins = _plugins_file(tmp_path, [_entry("user", "9.9.9")])
    assert _scope_row(d.check(base, run=_runner(), installed_plugins_path=plugins,
                              agents_md=str(tmp_path / "no-agents.md"))) is None


def test_unknown_future_fields_and_a_forked_marketplace_id_still_resolve(tmp_path):
    """The schema is Claude Code's, not ours: it will gain fields, and the id's marketplace half is
    whatever the marketplace was named. Neither may make the row disappear."""
    d = _doc()
    base = _sdlc(tmp_path / "repo", {})
    plugins = _plugins_file(tmp_path, [
        _entry("user", _one_below(d._agents_floor()), gitCommitSha="abc", futureField={"a": 1}),
    ], pid="sigmaloop@acme-fork")

    row = _scope_row(d.check(base, run=_runner(), installed_plugins_path=plugins))
    assert row is not None and row["ok"] is False
    assert "claude plugin update sigmaloop@acme-fork --scope user" in row["fix"]


def test_an_unreadable_version_is_flagged_rather_than_assumed_current(tmp_path):
    """Fail closed: a version we cannot compare has not been shown to clear the floor."""
    d = _doc()
    base = _sdlc(tmp_path / "repo", {})
    plugins = _plugins_file(tmp_path, [_entry("user", "not-a-version")])

    row = _scope_row(d.check(base, run=_runner(), installed_plugins_path=plugins))
    assert row is not None and row["ok"] is False
    assert "?" in row["fix"]
    # "below the floor" would be a claim we cannot make about a version we could not read.
    assert "not confirmed at or above" in row["name"] and "below the AGENTS.md floor" not in row["name"]


def test_the_floor_row_is_not_the_marketplace_nudge(tmp_path):
    """Two different staleness conditions that must never read alike: below the floor is a hard
    stop; merely behind the marketplace is informational. They are separate rows, and this one says
    which it is."""
    d = _doc()
    base = _sdlc(tmp_path / "repo", {})
    plugins = _plugins_file(tmp_path, [_entry("user", _one_below(d._agents_floor()))])

    row = _scope_row(d.check(base, run=_runner(), installed_plugins_path=plugins))
    assert "hard stop" in row["fix"]
    assert not row["name"].startswith("sigma up to date")


# --- #1983: independence is a resolved MECHANISM, not just a boolean flag ------------------------

def test_doctor_reports_the_resolved_mechanism_not_just_the_flag(tmp_path):
    """`independent: true` on a machine that actually resolves `inline` is an actively misleading
    report -- it says a fresh reviewer is spawned when none is. Assert that a mechanism is NAMED,
    not which one: which one depends on the host this test happens to run on."""
    d = _doc()
    base = _sdlc(tmp_path, {"review": {"independent": True, "host": "cursor"}})
    state = d._review_independence_state({"review": {"independent": True, "host": "cursor"}}, base)
    assert "mechanism" in state.lower(), state


def test_review_independence_state_keeps_its_old_single_argument_contract():
    """Every existing caller passes only cfg. Widening the signature must not break them."""
    d = _doc()
    assert d._review_independence_state({}).startswith("ON")
    assert "INLINE" in d._review_independence_state({"review": {"independent": False}})


# ------------------------------------------------------- #2116: the hard plan-gate row tells the
# truth about WHERE it is enforced, and stops asserting values it cannot see.

_GATE_ROW = "hard plan-gate (deny source edits w/o fresh plan)"


def _gate_row(cfg):
    d = _doc()
    with tempfile.TemporaryDirectory() as tmp:
        base = _sdlc(tmp, cfg)
        return {name: state for name, state, _ in d.features(base)}[_GATE_ROW]


def test_the_gate_row_names_both_enforcement_points_when_work_is_on():
    row = _gate_row({"work": {"enabled": True},
                     "gates": {"hard_plan_gate": {"enabled": True}}})
    assert row.startswith("ON (24h window)")
    assert "work.py pr" in row and "hook" in row


def test_the_gate_row_refuses_to_say_ON_when_work_is_off():
    """`work.py main()` refuses every verb with `work.enabled` off, so `pr()` -- the host-agnostic
    half -- never runs and the Claude Code hook is again the only enforcement. Reporting a plain
    "ON" there presents a lock as enforced where it is not, which is the silent half-guarantee
    AGENTS.md rejects. This row is where the key PRESENTS its state to a human, so this is the
    surface the DoD's "refuses loudly rather than presenting as locked" lands on."""
    row = _gate_row({"work": {"enabled": False},
                     "gates": {"hard_plan_gate": {"enabled": True}}})
    assert "NOT ENFORCED here" in row
    assert "Cursor/Codex get none" in row


def test_a_scalar_gate_block_reaches_the_GATE_ROW_not_just_the_helper(tmp_path):
    """THE CONTROL THAT MATTERS FOR #2116's doctor half. `_block` coerces a non-dict to `{}` at the
    DERIVATION, before `_gate_enabled` can see it -- so a test written against `_gate_enabled`
    alone goes green while this row still reports `off` for a config `work.py` refuses PRs on.
    Asserted through `features()` for exactly that reason."""
    for value in (True, 1, "true"):
        row = _gate_row({"work": {"enabled": True}, "gates": {"hard_plan_gate": value}})
        assert row.startswith("ON ("), (value, row)


def test_an_unadopted_checkout_keeps_the_old_off_string_byte_for_byte():
    """Every install in the wild. The new adopted-checkout qualifier must not leak into it."""
    assert _gate_row({"gates": {"hard_plan_gate": {"enabled": False}}}) == \
        "off (prompt-gate reminder only)"


def _managed_settings_module():
    spec = importlib.util.spec_from_file_location(
        "managed_settings", D.parent.parent.parent / "sigma-loop" / "scripts" / "managed_settings.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _adoptions(d):
    """The two ways a checkout is adopted (D1, #2580; the one-release alias went in #2706): the key,
    or the managed-settings file beside the config. Each yields (label, config-extra, seed-the-file)."""
    return (("key", {d._MANAGED_SETTINGS_KEY: {"project_id": "p1"}}, False),
            ("file", {}, True))


def test_an_adopted_checkout_does_not_fabricate_an_off_reading():
    """`doctor._cfg` reads `<sdlc_dir>/config.json` and NOTHING else -- doctor resolves no org
    policy itself. So on an adopted checkout an Org lock may say ON while this file says off, and
    printing `off` there is not silence, it is a fabricated reading. The row says what it actually
    read, and its hint names the one file that holds the org lock -- true in a core-only install,
    which has no other resolver."""
    d = _doc()
    for label, cfg, seed in _adoptions(d):
        with tempfile.TemporaryDirectory() as tmp:
            base = _sdlc(tmp, dict(cfg, gates={"hard_plan_gate": {"enabled": False}}))
            if seed:
                pathlib.Path(base, d._MANAGED_SETTINGS_FILE).write_text("{}")
            row = {n: s for n, s, _ in d.features(base)}[_GATE_ROW]
            assert "off in local config" in row, (label, row)
            assert "an org policy lock may differ" in row, (label, row)
            assert ".sdlc/" + d._MANAGED_SETTINGS_FILE in row, (label, row)


def test_managed_settings_adoption_matches_the_loops_own():
    """Parity: doctor's copy of the adopted check answers `managed_settings.is_adopted`'s truth
    table on the same inputs, and the key constant is the loop's own."""
    d, m = _doc(), _managed_settings_module()
    assert d._MANAGED_SETTINGS_KEY == m.CONFIG_KEY
    new = m.CONFIG_KEY
    cases = (("none", {}, False), ("new", {new: {"project_id": "p1"}}, False),
             ("blank", {new: {"project_id": "  "}}, False),
             ("not-a-dict", {new: "p1"}, False),
             ("file-only", {}, True))
    for label, cfg, seed in cases:
        with tempfile.TemporaryDirectory() as tmp:
            base = _sdlc(tmp, cfg)
            if seed:
                pathlib.Path(base, d._MANAGED_SETTINGS_FILE).write_text("{}")
            assert d._managed_settings_adopted(base, cfg) == m.is_adopted(base, cfg), label


def test_the_managed_settings_filename_matches_the_loops_own():
    """doctor.py carries a deliberate second copy of _MANAGED_SETTINGS_FILE (S1-G5, standalone-diagnostic
    convention). Pinned so the two cannot drift the way an un-pinned copy always eventually does."""
    spec = importlib.util.spec_from_file_location(
        "managed_settings", D.parent.parent.parent / "sigma-loop" / "scripts" / "managed_settings.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert _doc()._MANAGED_SETTINGS_FILE == m.MANAGED_SETTINGS_FILENAME


def test_a_scalar_stop_gate_block_reaches_its_row_too():
    """#2116 fixed `hooks/completion_gate.sh` and `triage._bucket_context` to read a scalar
    `stop_gate` block for its plain intent. All three readers agreed before that change and must
    still agree after it — so this row cannot keep `_block`'s coercion, or doctor becomes the one
    reader reporting `off` for a gate that is genuinely refusing session end. `stop_gate` is not
    itself org-lockable; the lockstep is the point, not the lock."""
    row = "Stop gate (refuse to end a session with unplanned source)"
    for value in (True, 1, "true"):
        with tempfile.TemporaryDirectory() as tmp:
            base = _sdlc(tmp, {"gates": {"stop_gate": value}})
            state = {n: s for n, s, _ in _doc().features(base)}[row]
            assert state.startswith("ON ("), (value, state)


def test_the_ON_row_also_refuses_to_assert_more_than_it_read():
    """The ON direction's mirror of `test_an_adopted_checkout_does_not_fabricate_an_off_reading`,
    and it needs its own pin: doctor reads LOCAL config only, so an Org lock of `false` over a local
    `true` would otherwise leave this row asserting enforcement `pr()` has already resolved away.
    An UNSET Org key correctly falls back to local, so the caveat is about the explicitly-locked-off
    case, not a general doubt. The caveat appears ONLY when adopted, by any of the three signals."""
    d = _doc()
    gate = {"work": {"enabled": True}, "gates": {"hard_plan_gate": {"enabled": True}}}
    assert "an org policy lock may differ" not in _gate_row(gate)          # unadopted: no caveat
    for label, cfg, seed in _adoptions(d):
        with tempfile.TemporaryDirectory() as tmp:
            base = _sdlc(tmp, dict(gate, **cfg))
            if seed:
                pathlib.Path(base, d._MANAGED_SETTINGS_FILE).write_text("{}")
            row = {n: s for n, s, _ in d.features(base)}[_GATE_ROW]
            assert row.startswith("ON ("), (label, row)
            assert "an org policy lock may differ" in row, (label, row)
            assert ".sdlc/" + d._MANAGED_SETTINGS_FILE in row, (label, row)


def test_work_enabled_is_read_the_way_work_py_reads_it_not_generously():
    """`work.enabled()` is a plain `bool()`, so `{"work": {"enabled": "false"}}` is ON there and
    every work verb runs. A generous read here — the right direction for the GATE flags (#416) —
    would report "NOT ENFORCED here" for a checkout where `work.py pr` is enforcing it perfectly
    well. The two rules genuinely differ, and this row must follow the code that executes."""
    row = _gate_row({"work": {"enabled": "false"},
                     "gates": {"hard_plan_gate": {"enabled": True}}})
    assert "NOT ENFORCED here" not in row, row
    assert row.startswith("ON (24h window) — Claude Code hook denies the edit"), row


# ---------------------------------------------------------------- dispatch compliance (#1779)
# #1703 decided a Task-tool dispatch (`agent_dispatch --role phase`) can never be code-enforced on
# every host -- but the resulting silence was never made OBSERVABLE. A phase that regresses from
# "ran as its own dispatched subagent" to "ran inline" broke the maker!=checker guarantee with
# zero error, the exact "no errors, nothing happening" shape AGENTS.md's LIVENESS property already
# names as the dangerous failure mode. These pin the cross-reference between the ledger's own
# `phase` events (ground truth: a phase boundary happened) and each goal's local action log
# (`agent_dispatch`/`agent_done`, role=="phase": did it happen as a DISPATCHED subagent).


def _write_jsonl(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


def _phase_end(goal, phase, ts):
    return {"id": "a:1", "ts": ts, "actor": "a", "kind": "phase",
            "goal": goal, "phase": phase, "state": "end"}


def _dispatch_pair(goal, phase, ts, model=None):
    dispatch = {"ts": ts, "goal": goal, "thread": "main", "actor": "a",
                "kind": "agent_dispatch", "role": "phase", "phase": phase}
    if model:
        dispatch["model"] = model
    return [
        dispatch,
        {"ts": ts, "goal": goal, "thread": "main", "actor": "a",
         "kind": "agent_done", "role": "phase", "phase": phase, "result": "pass"},
    ]


def _iso_ago(now, days_ago=0):
    import time as _time
    return _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(now - days_ago * 86400))


_ACTIONLOG_MOD = None


def _iso_ago_ms(now, days_ago=0):
    """Millisecond-precision counterpart to _iso_ago, delegating to actionlog.py's own `_stamp()`
    (cached after the first load) rather than a test-local reimplementation of its `.mmmZ` format --
    sigma-log's log.py._epoch() requires that fractional-second suffix (_TS_RE) and returns None
    without it, unlike ledger.py._epoch()'s whole-second format _iso_ago produces. Local
    action-log fixtures (agent_dispatch/agent_done entries) must use this one, not _iso_ago, or a
    recency-filtering read silently sees an unparseable timestamp on every line."""
    global _ACTIONLOG_MOD
    if _ACTIONLOG_MOD is None:
        _ACTIONLOG_MOD = _actionlog()
    return _ACTIONLOG_MOD._stamp(now - days_ago * 86400)


def test_dispatch_compliance_off_when_the_journal_is_not_enabled(tmp_path):
    """No journal means nothing is recorded to check against -- this is the 'off' state every
    sibling watcher check already reserves for its own autostart flag, not a reported gap.
    #2574/S1-G3 renamed the flag this gates on and the word the row prints."""
    state = _dr()._dispatch_compliance_state(tmp_path, {})
    assert state.startswith("off"), state
    assert "journal" in state


def test_dispatch_compliance_reports_no_data_yet_with_nothing_in_the_window(tmp_path):
    state = _dr()._dispatch_compliance_state(tmp_path, {"journal": {"enabled": True}})
    assert "nothing to check yet" in state


def test_dispatch_compliance_is_the_row_that_matters_a_phase_ran_with_no_dispatch(tmp_path):
    """THE ASSERTION THAT MATTERS (mirrors the watcher-health STALE test's own framing): the ledger
    says a phase boundary happened; the goal's own local log has no agent_dispatch/agent_done for
    it at all -- exactly the silent regression #1703 could not itself observe. This is #1779's own
    empirical shape (`.sdlc/state/log/1627.jsonl`: 36 real lines, zero `"kind": "phase"` rows) made
    into a fixture: a naive single-store check would report 100% compliance here."""
    now = 2_000_000_000.0
    _write_jsonl(tmp_path / "ledger" / "events" / "a-h.1.jsonl",
                 [_phase_end("1712", "plan_review", _iso_ago(now, 1))])
    # goal 1712's local log exists but never logged the dispatch/done pair for this phase.
    _write_jsonl(tmp_path / "state" / "log" / "1712.jsonl",
                 [{"ts": _iso_ago(now, 1), "goal": "1712", "thread": "main", "actor": "a",
                   "kind": "file", "path": "x.py", "op": "edit"}])
    state = _dr()._dispatch_compliance_state(tmp_path, {"journal": {"enabled": True}}, now=now)
    assert state.startswith("MISSING"), state
    assert "0/1 phase boundaries" in state, state
    assert "#1712 (plan_review)" in state, state


def test_dispatch_compliance_credits_a_goal_that_actually_dispatched_the_phase(tmp_path):
    now = 2_000_000_000.0
    _write_jsonl(tmp_path / "ledger" / "events" / "a-h.1.jsonl",
                 [_phase_end("1600", "research", _iso_ago(now, 1))])
    _write_jsonl(tmp_path / "state" / "log" / "1600.jsonl",
                 _dispatch_pair("1600", "research", _iso_ago(now, 1)))
    state = _dr()._dispatch_compliance_state(tmp_path, {"journal": {"enabled": True}}, now=now)
    assert state.startswith("READY"), state
    assert "1/1 phase boundaries" in state, state


def test_dispatch_compliance_bands_PARTIAL_and_counts_a_repeated_gap_with_an_x_suffix(tmp_path):
    """Reproduces the issue's own worked example shape: one goal complies, one has a single gap,
    and one ran the SAME phase twice with no dispatch either time -- counted as 2 gaps, not 1,
    because the denominator is phase-boundary OCCURRENCES, not distinct (goal, phase) pairs."""
    now = 2_000_000_000.0
    events = [
        _phase_end("1600", "research", _iso_ago(now, 1)),
        _phase_end("1712", "plan_review", _iso_ago(now, 1)),
        _phase_end("1734", "review", _iso_ago(now, 1)),
        _phase_end("1734", "review", _iso_ago(now, 1)),
    ]
    _write_jsonl(tmp_path / "ledger" / "events" / "a-h.1.jsonl", events)
    _write_jsonl(tmp_path / "state" / "log" / "1600.jsonl",
                 _dispatch_pair("1600", "research", _iso_ago(now, 1)))
    state = _dr()._dispatch_compliance_state(tmp_path, {"journal": {"enabled": True}}, now=now)
    assert state.startswith("PARTIAL"), state
    assert "1/4 phase boundaries" in state, state
    assert "3 gaps" in state, state
    assert "#1712 (plan_review)" in state, state
    assert "#1734 (review) x2" in state, state


def test_dispatch_compliance_reads_the_LOCAL_events_dir_when_share_is_false(tmp_path):
    """The nuance the issue itself calls out by name: `share: false` routed phase writes to
    `.sdlc/events/`, a SIBLING of `.sdlc/ledger/`, never inside it (the key sat in the journal's
    old block, unread since #2706; a leftover one under `journal` routes nothing either). Reading
    only the shared `.sdlc/ledger/events/` path (what `ledger.read_all` alone would see) must not
    silently under-report every phase boundary on a share:false install -- nothing is written to
    `.sdlc/ledger/events/` in this fixture at all."""
    now = 2_000_000_000.0
    _write_jsonl(tmp_path / "events" / "a-h.1.jsonl",
                 [_phase_end("1600", "research", _iso_ago(now, 1))])
    _write_jsonl(tmp_path / "state" / "log" / "1600.jsonl",
                 _dispatch_pair("1600", "research", _iso_ago(now, 1)))
    cfg = {"journal": {"enabled": True, "share": False}}
    state = _dr()._dispatch_compliance_state(tmp_path, cfg, now=now)
    assert state.startswith("READY"), state
    assert "1/1 phase boundaries" in state, state
    # confirm the shared path genuinely has nothing -- this is testing the LOCAL route, not a
    # fallback that happens to also work if both directories were read unconditionally.
    assert not (tmp_path / "ledger").exists()


def test_dispatch_compliance_ignores_a_phase_end_older_than_the_30_day_window(tmp_path):
    now = 2_000_000_000.0
    _write_jsonl(tmp_path / "ledger" / "events" / "a-h.1.jsonl",
                 [_phase_end("1712", "plan_review", _iso_ago(now, 45))])
    state = _dr()._dispatch_compliance_state(tmp_path, {"journal": {"enabled": True}}, now=now)
    assert "nothing to check yet" in state, state


def test_dispatch_compliance_ignores_phase_start_events_never_double_counts(tmp_path):
    """A `start` and its matching `end` describe ONE phase boundary -- counting both would inflate
    the denominator for no extra information (every real `end` implies a prior `start`)."""
    now = 2_000_000_000.0
    _write_jsonl(tmp_path / "ledger" / "events" / "a-h.1.jsonl", [
        {"id": "a:1", "ts": _iso_ago(now, 1), "actor": "a", "kind": "phase",
         "goal": "1600", "phase": "research", "state": "start"},
        _phase_end("1600", "research", _iso_ago(now, 1)),
    ])
    _write_jsonl(tmp_path / "state" / "log" / "1600.jsonl",
                 _dispatch_pair("1600", "research", _iso_ago(now, 1)))
    state = _dr()._dispatch_compliance_state(tmp_path, {"journal": {"enabled": True}}, now=now)
    assert "1/1 phase boundaries" in state, state


def test_dispatch_compliance_row_is_wired_into_features(tmp_path):
    """Surfaced in `/sigma-doctor`'s dashboard, not just importable in isolation -- `check` (the CLI
    verb the issue names) prints `features()` too (main()'s own 'check' branch), so wiring it here
    is what actually reaches a human running `/sigma-doctor`. Uses real wall-clock time (no `now`
    seam on `features()` itself) so the fixture falls inside the real 30-day window."""
    import time as _time
    now = _time.time()
    cfg = {"journal": {"enabled": True}}
    base = _sdlc(str(tmp_path), cfg)
    _write_jsonl(pathlib.Path(base, "ledger", "events", "a-h.1.jsonl"),
                 [_phase_end("1600", "research", _iso_ago(now, 1))])
    _write_jsonl(pathlib.Path(base, "state", "log", "1600.jsonl"),
                 _dispatch_pair("1600", "research", _iso_ago(now, 1)))
    rows = {n: s for n, s, _ in _doc().features(base)}
    match = [name for name in rows if name.startswith("dispatch compliance")]
    assert len(match) == 1, rows.keys()
    assert rows[match[0]].startswith("READY"), rows[match[0]]


# ------------------------------------------------------------ dispatch model compliance (#2514)
# A sibling to the dispatch-compliance check above, NOT a rewrite of it: that check answers "did a
# dispatch happen at all"; this one answers the narrower, conditional follow-on "given a dispatch
# happened, did it carry the resolved --model tier" -- the enforcement half of #2514 landed in
# actionlog.py's `append()` (refusing a bare `agent_dispatch --role phase/slice` with no --model at
# log time); this is the DETECTION half, for anything that reached the log by bypassing that CLI
# refusal (a hand-forced log line, an older log predating the rule).


def test_dispatch_model_compliance_off_when_the_journal_is_not_enabled(tmp_path):
    """Mirrors test_dispatch_compliance_off_when_the_journal_is_not_enabled."""
    state = _dr()._dispatch_model_compliance_state(tmp_path, {})
    assert state.startswith("off"), state
    assert "journal" in state


def test_dispatch_model_compliance_reports_no_data_yet_with_nothing_in_the_window(tmp_path):
    state = _dr()._dispatch_model_compliance_state(tmp_path, {"journal": {"enabled": True}})
    assert "nothing to check yet" in state


def test_dispatch_model_compliance_is_the_row_that_matters_a_dispatch_with_no_model(tmp_path):
    """THE CONTROL (AGENTS.md: "run the control, or the check is decoration"). Deliberately write
    an agent_dispatch row with role: "phase" and NO model field, paired with a matching phase
    ledger `end` event, and assert the new state function actually flags it -- proving the
    detection half of #2514 fires for real, not just that a clean run stays clean."""
    now = 2_000_000_000.0
    _write_jsonl(tmp_path / "ledger" / "events" / "a-h.1.jsonl",
                 [_phase_end("1712", "plan_review", _iso_ago(now, 1))])
    _write_jsonl(tmp_path / "state" / "log" / "1712.jsonl",
                 _dispatch_pair("1712", "plan_review", _iso_ago(now, 1)))  # no model
    state = _dr()._dispatch_model_compliance_state(tmp_path, {"journal": {"enabled": True}}, now=now)
    assert state.startswith("MISSING"), state
    assert "0/1 phase boundaries" in state, state
    assert "#1712 (plan_review)" in state, state


def test_dispatch_model_compliance_credits_a_dispatch_that_carried_a_model(tmp_path):
    """The positive counterpart: a dispatch row WITH model: "sonnet" reports READY."""
    now = 2_000_000_000.0
    _write_jsonl(tmp_path / "ledger" / "events" / "a-h.1.jsonl",
                 [_phase_end("1600", "research", _iso_ago(now, 1))])
    _write_jsonl(tmp_path / "state" / "log" / "1600.jsonl",
                 _dispatch_pair("1600", "research", _iso_ago(now, 1), model="sonnet"))
    state = _dr()._dispatch_model_compliance_state(tmp_path, {"journal": {"enabled": True}}, now=now)
    assert state.startswith("LOGGED ONLY"), state
    assert "1/1 phase boundaries" in state, state


def test_dispatch_model_compliance_bands_PARTIAL_and_lists_the_gap(tmp_path):
    """Mirrors test_dispatch_compliance_bands_PARTIAL_and_counts_a_repeated_gap_with_an_x_suffix'
    shape for the model-carrying case: one goal's dispatch carried a model, two others' phase
    boundaries have no model-carrying dispatch at all (one of them occurring twice)."""
    now = 2_000_000_000.0
    events = [
        _phase_end("1600", "research", _iso_ago(now, 1)),
        _phase_end("1712", "plan_review", _iso_ago(now, 1)),
        _phase_end("1734", "review", _iso_ago(now, 1)),
        _phase_end("1734", "review", _iso_ago(now, 1)),
    ]
    _write_jsonl(tmp_path / "ledger" / "events" / "a-h.1.jsonl", events)
    _write_jsonl(tmp_path / "state" / "log" / "1600.jsonl",
                 _dispatch_pair("1600", "research", _iso_ago(now, 1), model="sonnet"))
    state = _dr()._dispatch_model_compliance_state(tmp_path, {"journal": {"enabled": True}}, now=now)
    assert state.startswith("PARTIAL"), state
    assert "1/4 phase boundaries" in state, state
    assert "3 gaps" in state, state
    assert "#1712 (plan_review)" in state, state
    assert "#1734 (review) x2" in state, state


# ------------------------------------------------------------ dispatch model compliance — slice (#2557)
# A sibling to _dispatch_model_compliance_state above, NOT a rewrite: that row cross-references
# ledger phase/end events as its denominator (a clean 1:1 boundary signal). Slice has no equivalent
# -- the ledger's own "slice" event kind (slices.py `plan`) fires at PLAN time, one row per
# manifest-declared slice, not at dispatch/completion, and has never fired in this repo (see
# slices.py's own comment, ~line 550-554: no plan has ever declared slices). So this audits the
# local action log directly: every agent_dispatch role=="slice" entry in the last 30 days, keyed by
# (goal, thread) -- thread carries the slice id per running.md's documented invocation
# (`--thread <slice-id> --role slice --phase implement --model <tier>`). Gated on
# action_log.enabled, not journal.enabled -- this check's denominator comes entirely from the
# local log, a separate config block from the ledger journal the phase row above depends on.


def _slice_dispatch(goal, slice_id, ts, model=None):
    entry = {"ts": ts, "goal": goal, "thread": slice_id, "actor": "a",
             "kind": "agent_dispatch", "role": "slice", "phase": "implement"}
    if model:
        entry["model"] = model
    return entry


def test_dispatch_model_compliance_slice_off_when_action_log_is_not_enabled(tmp_path):
    """Mirrors test_dispatch_model_compliance_off_when_the_journal_is_not_enabled, but this row's
    gate is action_log.enabled -- it has no ledger dependency to gate on the journal for."""
    state = _dr()._dispatch_model_compliance_slice_state(tmp_path, {})
    assert state.startswith("off"), state


def test_dispatch_model_compliance_slice_stays_off_when_only_telemetry_is_enabled(tmp_path):
    """The gate really is action_log.enabled, not journal.enabled -- proves it by giving the
    row the WRONG flag on (the one its sibling row above gates on) and confirming it still reads
    as off, not as though the right flag were set."""
    state = _dr()._dispatch_model_compliance_slice_state(tmp_path, {"journal": {"enabled": True}})
    assert state.startswith("off"), state
    assert "action_log" in state
    assert "action_log" in state


def test_dispatch_model_compliance_slice_reports_no_data_yet_with_nothing_in_the_window(tmp_path):
    state = _dr()._dispatch_model_compliance_slice_state(
        tmp_path, {"action_log": {"enabled": True}})
    assert "nothing to check yet" in state


def test_dispatch_model_compliance_slice_is_the_row_that_matters_a_dispatch_with_no_model(tmp_path):
    """THE CONTROL (AGENTS.md: "run the control, or the check is decoration"). A --role slice
    dispatch with no model field must be flagged, proving the detection actually fires."""
    now = 2_000_000_000.0
    _write_jsonl(tmp_path / "state" / "log" / "1712.jsonl",
                 [_slice_dispatch("1712", "1712.2a", _iso_ago_ms(now, 1))])  # no model
    state = _dr()._dispatch_model_compliance_slice_state(
        tmp_path, {"action_log": {"enabled": True}}, now=now)
    assert state.startswith("MISSING"), state
    assert "0/1 slice dispatches" in state, state
    assert "#1712 (slice 1712.2a)" in state, state


def test_dispatch_model_compliance_slice_credits_a_dispatch_that_carried_a_model(tmp_path):
    """The positive counterpart: a slice dispatch WITH model: "sonnet" reports LOGGED ONLY."""
    now = 2_000_000_000.0
    _write_jsonl(tmp_path / "state" / "log" / "1600.jsonl",
                 [_slice_dispatch("1600", "1600.1a", _iso_ago_ms(now, 1), model="sonnet")])
    state = _dr()._dispatch_model_compliance_slice_state(
        tmp_path, {"action_log": {"enabled": True}}, now=now)
    assert state.startswith("LOGGED ONLY"), state
    assert "1/1 slice dispatches" in state, state


def test_dispatch_model_compliance_slice_bands_PARTIAL_and_lists_the_gap(tmp_path):
    """One goal's slice dispatch carried a model, two others' didn't (one of them dispatched
    twice with no model, under the SAME slice id -- proving the (goal, thread) key aggregates a
    repeated gap with an x-suffix instead of listing it twice, mirroring the phase row's own
    repeated-gap shape)."""
    now = 2_000_000_000.0
    _write_jsonl(tmp_path / "state" / "log" / "1600.jsonl",
                 [_slice_dispatch("1600", "1600.1a", _iso_ago_ms(now, 1), model="sonnet")])
    _write_jsonl(tmp_path / "state" / "log" / "1712.jsonl",
                 [_slice_dispatch("1712", "1712.2a", _iso_ago_ms(now, 1))])
    _write_jsonl(tmp_path / "state" / "log" / "1734.jsonl",
                 [_slice_dispatch("1734", "1734.3a", _iso_ago_ms(now, 1)),
                  _slice_dispatch("1734", "1734.3a", _iso_ago_ms(now, 1))])
    state = _dr()._dispatch_model_compliance_slice_state(
        tmp_path, {"action_log": {"enabled": True}}, now=now)
    assert state.startswith("PARTIAL"), state
    assert "1/4 slice dispatches" in state, state
    assert "3 gaps" in state, state
    assert "#1712 (slice 1712.2a)" in state, state
    assert "#1734 (slice 1734.3a) x2" in state, state


def test_dispatch_model_compliance_slice_ignores_a_dispatch_older_than_the_30_day_window(tmp_path):
    """Mirrors test_dispatch_compliance_ignores_a_phase_end_older_than_the_30_day_window. This
    row's own recency filter is genuinely new logic (the phase row's local-log scan has none --
    its ledger side supplies the window instead), so it needs its own direct control."""
    now = 2_000_000_000.0
    _write_jsonl(tmp_path / "state" / "log" / "1712.jsonl",
                 [_slice_dispatch("1712", "1712.2a", _iso_ago_ms(now, 45))])
    state = _dr()._dispatch_model_compliance_slice_state(
        tmp_path, {"action_log": {"enabled": True}}, now=now)
    assert "nothing to check yet" in state, state


def test_dispatch_model_compliance_slice_row_is_wired_into_features(tmp_path):
    """Mirrors test_dispatch_compliance_row_is_wired_into_features -- surfaced in /sigma-doctor's
    dashboard, not just importable in isolation."""
    import time as _time
    now = _time.time()
    cfg = {"action_log": {"enabled": True}}
    base = _sdlc(str(tmp_path), cfg)
    _write_jsonl(pathlib.Path(base, "state", "log", "1600.jsonl"),
                 [_slice_dispatch("1600", "1600.1a", _iso_ago_ms(now, 1), model="sonnet")])
    rows = {n: s for n, s, _ in _doc().features(base)}
    match = [name for name in rows if name.startswith("dispatch model log coverage — slice")]
    assert len(match) == 1, rows.keys()
    assert rows[match[0]].startswith("LOGGED ONLY"), rows[match[0]]


# ------------------------------------------------------------ budget enforcement state (#2515)
# A sibling to the existing "budgets" row, NOT a rewrite of it: that row answers "is
# budget.max_tokens CONFIGURED"; this one answers the narrower, conditional follow-on "given it's
# configured, has anything on THIS host ever actually fed it" -- since #2515's fix
# (`phase_report.py cmd_end` -> `state.add_tokens`, Decision 2) feeds the counter DIRECTLY,
# independent of the journal, this check's "off" state is reserved for max_tokens itself not being
# configured -- a journal-off host gets its own honest blind-spot note instead, per Decision 4.


def _phase_end_measured(goal, phase, ts, tokens_in=1000, tokens_out=200,
                        attempt_id="attempt-1"):
    """The measured-but-maybe-unpriced shape `cmd_end` itself writes for the `phase`/`end` ledger
    event once a real transcript source was found (`tokens_in`/`tokens_out` populated) -- distinct
    from `_phase_end` above, which mirrors the fully `unavailable` shape (no token fields at all)."""
    return {"id": "a:1", "ts": ts, "actor": "a", "kind": "phase",
            "goal": goal, "phase": phase, "state": "end",
            "tokens_in": str(tokens_in), "tokens_out": str(tokens_out),
            "attempt_id": attempt_id}


def _spend_event(goal, phase, ts, model="claude-sonnet-5", tokens_in=1000, tokens_out=200,
                  cost_cents=100, attempt_id="attempt-1"):
    """Mirrors the real `spend` ledger event shape `cmd_end` writes (`ledger.EVENT_FIELDS["spend"]
    = ("phase", "model", "tokens_in", "tokens_out", "cost_cents")`) -- written only once a real
    dollar cost was priced."""
    return {"id": "a:2", "ts": ts, "actor": "a", "kind": "spend",
            "goal": goal, "phase": phase, "model": model,
            "tokens_in": str(tokens_in), "tokens_out": str(tokens_out),
            "cost_cents": str(cost_cents), "attempt_id": attempt_id}


def _budget_credit(base, now, attempt_id="attempt-1"):
    path = pathlib.Path(base) / "state" / "STATE.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"run_started_at: {now - 2 * 86400}\nrun_tokens: 1000\n"
                    f"run_token_credits: {json.dumps([attempt_id])}\n")


def test_budget_enforcement_off_when_max_tokens_not_configured(tmp_path):
    state = _dr()._budget_enforcement_state(tmp_path, {}, now=2_000_000_000.0)
    assert state.startswith("off"), state
    assert "max_tokens" in state


def test_budget_enforcement_off_with_an_empty_budget_block(tmp_path):
    state = _dr()._budget_enforcement_state(tmp_path, {"budget": {}}, now=2_000_000_000.0)
    assert state.startswith("off"), state


def test_budget_enforcement_reports_no_data_yet_with_nothing_in_the_window(tmp_path):
    cfg = {"budget": {"max_tokens": 60000000}, "journal": {"enabled": True}}
    state = _dr()._budget_enforcement_state(tmp_path, cfg, now=2_000_000_000.0)
    assert "no phase boundaries recorded" in state, state


def test_budget_enforcement_flags_measured_but_unpriced_phases_as_missing(tmp_path):
    """THE CONTROL (AGENTS.md: "run the control, or the check is decoration"). Phase-end events with
    NO tokens_in/tokens_out (the `unavailable`-source shape) and no spend events at all must be
    flagged MISSING -- proving the detection distinguishes "never measured" from "measured and
    priced", not just that a clean READY case reports READY."""
    now = 2_000_000_000.0
    _write_jsonl(tmp_path / "ledger" / "events" / "a-h.1.jsonl",
                 [_phase_end("1712", "plan_review", _iso_ago(now, 1))])
    cfg = {"budget": {"max_tokens": 60000000}, "journal": {"enabled": True}}
    state = _dr()._budget_enforcement_state(tmp_path, cfg, now=now)
    assert state.startswith("MISSING"), state
    assert "1" in state


def test_budget_enforcement_flags_measured_unpriced_phases_as_partial(tmp_path):
    now = 2_000_000_000.0
    _write_jsonl(tmp_path / "ledger" / "events" / "a-h.1.jsonl",
                 [_phase_end_measured("1600", "research", _iso_ago(now, 1))])
    cfg = {"budget": {"max_tokens": 60000000}, "journal": {"enabled": True}}
    state = _dr()._budget_enforcement_state(tmp_path, cfg, now=now)
    assert state.startswith("PARTIAL"), state
    assert "0/1" in state


def test_budget_enforcement_reports_ready_when_spend_events_are_present(tmp_path):
    now = 2_000_000_000.0
    _budget_credit(tmp_path, now)
    _write_jsonl(tmp_path / "ledger" / "events" / "a-h.1.jsonl", [
        _phase_end_measured("1600", "research", _iso_ago(now, 1)),
        _spend_event("1600", "research", _iso_ago(now, 1)),
    ])
    cfg = {"budget": {"max_tokens": 60000000}, "journal": {"enabled": True}}
    state = _dr()._budget_enforcement_state(tmp_path, cfg, now=now)
    assert state.startswith("READY"), state
    assert "1/1" in state


def test_budget_enforcement_refuses_legacy_events_without_cursor_credit(tmp_path):
    """Pre-#2515 phase/spend pairs existed while budget.run_tokens stayed at zero."""
    now = 2_000_000_000.0
    end = _phase_end_measured("1600", "research", _iso_ago(now, 1))
    spend = _spend_event("1600", "research", _iso_ago(now, 1))
    end.pop("attempt_id")
    spend.pop("attempt_id")
    _write_jsonl(tmp_path / "ledger" / "events" / "a-h.1.jsonl", [end, spend])
    cfg = {"budget": {"max_tokens": 60000000}, "journal": {"enabled": True}}
    state = _dr()._budget_enforcement_state(tmp_path, cfg, now=now)
    assert state.startswith("PARTIAL"), state
    assert "0/1" in state, state


def test_budget_enforcement_does_not_count_one_spend_for_two_measured_ends(tmp_path):
    """A single priced event cannot substantiate two phase ends, even for one goal/phase."""
    now = 2_000_000_000.0
    _budget_credit(tmp_path, now)
    _write_jsonl(tmp_path / "ledger" / "events" / "a-h.1.jsonl", [
        _phase_end_measured("1600", "research", _iso_ago(now, 1)),
        _phase_end_measured("1600", "research", _iso_ago(now, 1)),
        _spend_event("1600", "research", _iso_ago(now, 1)),
    ])
    cfg = {"budget": {"max_tokens": 60000000}, "journal": {"enabled": True}}
    state = _dr()._budget_enforcement_state(tmp_path, cfg, now=now)
    assert state.startswith("PARTIAL"), state
    assert "1/2" in state, state


def test_budget_enforcement_notes_the_blind_spot_when_the_journal_is_off(tmp_path):
    """max_tokens IS configured but the journal is off -- the mechanism (Decision 2: cmd_end feeds
    state.add_tokens directly, unconditional on the journal) may still be WORKING; only this
    check's own detection is blind. Must NOT read as "off" -- that would misreport a working
    mechanism as inert, the exact regression this test guards against (copying
    `_dispatch_model_compliance_state`'s gate, which this check deliberately does not)."""
    cfg = {"budget": {"max_tokens": 60000000}}   # no journal block at all
    state = _dr()._budget_enforcement_state(tmp_path, cfg, now=2_000_000_000.0)
    assert not state.startswith("off"), state
    assert "the journal is off" in state
    assert "run_tokens" in state


def test_budget_enforcement_row_is_wired_into_features(tmp_path):
    import time as _time
    now = _time.time()
    cfg = {"budget": {"max_tokens": 60000000}, "journal": {"enabled": True}}
    base = _sdlc(str(tmp_path), cfg)
    _budget_credit(base, now)
    _write_jsonl(pathlib.Path(base, "ledger", "events", "a-h.1.jsonl"), [
        _phase_end_measured("1600", "research", _iso_ago(now, 1)),
        _spend_event("1600", "research", _iso_ago(now, 1)),
    ])
    rows = {n: s for n, s, _ in _doc().features(base)}
    match = [name for name in rows if name.startswith("budget enforcement")]
    assert len(match) == 1, rows.keys()
    assert rows[match[0]].startswith("READY"), rows[match[0]]


# ------------------------------------------- S1-G3 / #2574: doctor's three journal rows, unioned


@pytest.mark.parametrize("cfg", [
    {"journal": {"enabled": True}},
    {"journal": {"enabled": True, "share": False}},
    {"journal": {"enabled": True, "share": True}},
])
@pytest.mark.parametrize("where", ["ledger/events", "events"])
def test_dispatch_compliance_reads_both_event_dirs_whatever_the_config_says(tmp_path, cfg, where):
    """The three doctor rows used to ROUTE on `share`, so each read exactly one directory (a leftover
    `share` routes nothing now). After the move a repo has history in both, and a routed read under-reports the one
    it did not pick -- silently, as a confident compliance percentage."""
    now = 2_000_000_000.0
    _write_jsonl(tmp_path / where / "a-h.1.jsonl",
                 [_phase_end("1600", "research", _iso_ago(now, 1))])
    _write_jsonl(tmp_path / "state" / "log" / "1600.jsonl",
                 _dispatch_pair("1600", "research", _iso_ago(now, 1)))
    state = _dr()._dispatch_compliance_state(tmp_path, cfg, now=now)
    assert state.startswith("READY"), state
    assert "1/1 phase boundaries" in state, state


def test_dispatch_compliance_unions_both_dirs_without_double_counting(tmp_path):
    now = 2_000_000_000.0
    _write_jsonl(tmp_path / "ledger" / "events" / "a-h.1.jsonl",
                 [_phase_end("1600", "research", _iso_ago(now, 1))])
    _write_jsonl(tmp_path / "events" / "a-h.2.jsonl",
                 [_phase_end("1712", "plan_review", _iso_ago(now, 1))])
    _write_jsonl(tmp_path / "state" / "log" / "1600.jsonl",
                 _dispatch_pair("1600", "research", _iso_ago(now, 1)))
    state = _dr()._dispatch_compliance_state(tmp_path, {"journal": {"enabled": True}}, now=now)
    assert "1/2 phase boundaries" in state, state
    assert "#1712 (plan_review)" in state, state


def test_dispatch_compliance_says_journal_off_not_telemetry_off(tmp_path):
    state = _dr()._dispatch_compliance_state(tmp_path, {})
    assert state.startswith("off"), state
    assert "journal" in state, state


@pytest.mark.parametrize("where", ["ledger/events", "events"])
def test_dispatch_model_compliance_reads_both_event_dirs(tmp_path, where):
    now = 2_000_000_000.0
    _write_jsonl(tmp_path / where / "a-h.1.jsonl",
                 [_phase_end("1600", "research", _iso_ago(now, 1))])
    _write_jsonl(tmp_path / "state" / "log" / "1600.jsonl",
                 _dispatch_pair("1600", "research", _iso_ago_ms(now, 1), model="sonnet"))
    state = _dr()._dispatch_model_compliance_state(
        tmp_path, {"journal": {"enabled": True}}, now=now)
    assert "1/1" in state, state


def test_dispatch_model_compliance_says_journal_off(tmp_path):
    state = _dr()._dispatch_model_compliance_state(tmp_path, {})
    assert state.startswith("off"), state
    assert "journal" in state, state


@pytest.mark.parametrize("where", ["ledger/events", "events"])
def test_budget_enforcement_reads_both_event_dirs(tmp_path, where):
    now = 2_000_000_000.0
    _write_jsonl(tmp_path / where / "a-h.1.jsonl",
                 [_phase_end_measured("1600", "research", _iso_ago(now, 1))])
    cfg = {"journal": {"enabled": True}, "budget": {"max_tokens": 1_000_000}}
    state = _dr()._budget_enforcement_state(tmp_path, cfg, now=now)
    assert "journal is off" not in state, state


def test_budget_enforcement_note_says_journal_not_telemetry(tmp_path):
    """The budget row keeps its longer honest note -- budget enforcement may STILL be working with
    the journal off, because `state.add_tokens` is fed by `phase_report.py` directly. Only the
    word changes."""
    cfg = {"budget": {"max_tokens": 1_000_000}}
    state = _dr()._budget_enforcement_state(tmp_path, cfg, now=2_000_000_000.0)
    assert "journal is off" in state, state
    assert "may still be enforced" in state, state


def test_the_journal_counters_are_renamed_and_both_are_kept(tmp_path):
    """Both destinations keep a counter for the legacy release: the shared one is what an existing
    adopter's history still lives in."""
    d = _dr()
    assert hasattr(d, "_journal_shared_events_count")
    assert hasattr(d, "_journal_local_events_count")
    _write_jsonl(tmp_path / "ledger" / "events" / "a.jsonl", [{"kind": "phase"}, {"kind": "phase"}])
    _write_jsonl(tmp_path / "events" / "a.jsonl", [{"kind": "phase"}])
    assert d._journal_shared_events_count(tmp_path) == 2
    assert d._journal_local_events_count(tmp_path) == 1


# --- #2593: a conflicted landing PR runs zero CI, indistinguishable from "unchecked" -------------
# GitHub cannot build a merge ref for a conflicted PR, so no `pull_request` workflow ever fires --
# the PR shows NO checks, visually identical to "CI not configured yet". `_landing_pr_unverifiable`
# distinguishes that from "checks pending", "checks passed", AND an ordinary branch with no CI
# wired up at all (also an empty rollup, but `mergeable` tells the two apart -- see test 9).


def _feature_registry():
    spec = importlib.util.spec_from_file_location(
        "feature_registry", D.parent.parent.parent / "sigma-loop" / "scripts" / "feature_registry.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def _open_unit(sdlc_dir, name, open_=True):
    fr = _feature_registry()
    fr.write_unit(fr.registry_dir(sdlc_dir), name,
                  {"title": name, "owner": "@x", "open": open_, "parent": None,
                   "tracking_issue": None, "repos": {}})


def _pr_row(mergeable="MERGEABLE", rollup=None, number=42):
    return json.dumps([{"number": number, "mergeable": mergeable,
                        "mergeStateStatus": "CLEAN" if mergeable == "MERGEABLE" else "DIRTY",
                        "statusCheckRollup": rollup if rollup is not None else []}])


def test_landing_pr_unverifiable_true_for_a_conflicted_pr_with_zero_checks():
    d = _doc()
    def run(a):
        if a[:3] == ["gh", "pr", "list"]:
            return _pr_row(mergeable="CONFLICTING", rollup=[])
        return ""
    detail = d._landing_pr_unverifiable(run, "acme/widget", "checkout")
    assert detail and "UNVERIFIABLE" in detail and "checkout" in detail


def test_landing_pr_unverifiable_false_when_checks_did_run_despite_conflict():
    """Isolates guard 2 -- checks DID run despite the conflict, a rarer, different shape."""
    d = _doc()
    def run(a):
        if a[:3] == ["gh", "pr", "list"]:
            return _pr_row(mergeable="CONFLICTING", rollup=[{"name": "ci", "conclusion": "SUCCESS"}])
        return ""
    assert d._landing_pr_unverifiable(run, "acme/widget", "checkout") is None


def test_landing_pr_unverifiable_false_for_a_clean_mergeable_pr():
    d = _doc()
    def run(a):
        if a[:3] == ["gh", "pr", "list"]:
            return _pr_row(mergeable="MERGEABLE",
                           rollup=[{"name": "ci", "conclusion": "SUCCESS"}] * 16)
        return ""
    assert d._landing_pr_unverifiable(run, "acme/widget", "checkout") is None


def test_landing_pr_unverifiable_false_for_pending_checks():
    d = _doc()
    def run(a):
        if a[:3] == ["gh", "pr", "list"]:
            return _pr_row(mergeable="MERGEABLE", rollup=[{"name": "ci", "status": "IN_PROGRESS"}])
        return ""
    assert d._landing_pr_unverifiable(run, "acme/widget", "checkout") is None


def test_landing_pr_unverifiable_false_with_no_open_landing_pr():
    d = _doc()
    def run(a):
        if a[:3] == ["gh", "pr", "list"]:
            return "[]"
        return ""
    assert d._landing_pr_unverifiable(run, "acme/widget", "checkout") is None


def test_landing_pr_unverifiable_fails_open_on_an_unreadable_gh_reply():
    d = _doc()
    for reply in ("", "not json", "{}"):
        def run(a, _r=reply):
            return _r
        assert d._landing_pr_unverifiable(run, "acme/widget", "checkout") is None


def test_landing_pr_unverifiable_false_for_a_mergeable_branch_with_no_ci_at_all():
    """The ONE fixture shape that isolates guard 1 (round 2 finding 1) -- a real, distinct case:
    an ordinary branch with no CI wired up, not a conflict. Both empty-rollup cases (this one and
    the conflicted one) must be told apart by `mergeable`, never by rollup emptiness alone."""
    d = _doc()
    def run(a):
        if a[:3] == ["gh", "pr", "list"]:
            return _pr_row(mergeable="MERGEABLE", rollup=[])
        return ""
    assert d._landing_pr_unverifiable(run, "acme/widget", "checkout") is None


def test_landing_pr_unverifiable_call_includes_repo(tmp_path):
    """Round 2 finding 2: --repo is required, matching every real precedent for this call shape."""
    d = _doc()
    calls = []
    def run(a):
        calls.append(a)
        return "[]"
    d._landing_pr_unverifiable(run, "acme/widget", "checkout")
    assert calls and "--repo" in calls[0] and "acme/widget" in calls[0]


def test_doctor_row_names_every_affected_open_unit_in_one_aggregated_message(tmp_path):
    d = _doc()
    sdlc = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}})
    _open_unit(sdlc, "checkout")
    _open_unit(sdlc, "payments")
    def run(a):
        if a[:3] == ["gh", "pr", "list"] and "feature/checkout" in a:
            return _pr_row(mergeable="CONFLICTING", rollup=[])
        if a[:3] == ["gh", "pr", "list"] and "feature/payments" in a:
            return _pr_row(mergeable="MERGEABLE", rollup=[{"name": "ci", "conclusion": "SUCCESS"}])
        return "[]"
    detail = d._landing_pr_unverifiable_units(sdlc, {"repo": "acme/widget"}, run)
    assert detail and "checkout" in detail and "payments" not in detail


def test_doctor_row_ok_true_with_no_open_units(tmp_path):
    d = _doc()
    sdlc = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}})
    assert d._landing_pr_unverifiable_units(sdlc, {"repo": "acme/widget"}, lambda a: "[]") is None


def test_doctor_model_max_tier_default_and_vocabulary_match_predict_pys_own_constants():
    """Code review, PR #2564: `_model_max_tier`'s own docstring claims to stay "trivially in
    lockstep" with predict.py's `_MAX_TIER_DEFAULT`/`_TIER_PRICE_ORDER`, but nothing checked that
    claim -- mirrors `test_doctor_handoff_default_matches_loop_pys_own_constant`'s own sync-check
    shape exactly (same architecture-rule-3 reason: doctor.py cannot `import predict`, so this is
    the mechanism instead of an import), loading predict.py directly from its file path and
    failing loudly if either constant ever drifts from doctor.py's own hand-duplicated copy."""
    predict_path = (D.parent.parent.parent / "sigma-model" / "scripts" / "predict.py")
    spec = importlib.util.spec_from_file_location("predict_for_sync_check", predict_path)
    predict_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(predict_mod)
    d = _doc()
    assert d._model_max_tier({}) == predict_mod._MAX_TIER_DEFAULT, (
        f"doctor.py's _model_max_tier fallback ({d._model_max_tier({})!r}) has drifted from "
        f"predict.py's own _MAX_TIER_DEFAULT ({predict_mod._MAX_TIER_DEFAULT!r})")
    for tier in predict_mod._TIER_PRICE_ORDER:
        assert d._model_max_tier({"model_selection_max_tier": tier}) == tier, (
            f"doctor.py's vocabulary check rejects {tier!r}, a real tier in predict.py's own "
            f"_TIER_PRICE_ORDER {predict_mod._TIER_PRICE_ORDER!r}")


# ---------------------------------------------------------------- #2704: the row reports, not guesses
# The absent/stale row used to say "the usual cause is that '<builder>' has no LLM backend
# configured" -- a guess. `kg.py refresh` now records its last real outcome in
# `.sdlc/state/kg-refresh.json` (the builder's own text), and the row quotes that instead.

def _kg_on(t):
    return _sdlc(t, {"knowledge_graph": {"enabled": True, "builder": "graphify", "auto_refresh": True}})


def test_the_absent_row_quotes_the_last_recorded_refresh_failure():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _kg_on(t)
        (pathlib.Path(base) / "state").mkdir()
        (pathlib.Path(base) / "state" / "kg-refresh.json").write_text(json.dumps({
            "at": time.time() - 7200, "ok": False, "ran": True,
            "detail": "No LLM backend configured. Set one of: X, Y"}))
        row = _by_name(d.check(base, run=_runner(builder="graphify 1.0")))["knowledge graph auto-refresh is working"]
        assert row["ok"] is False
        assert "No LLM backend configured. Set one of: X, Y" in row["fix"]
        assert "2h ago" in row["fix"]


def test_the_absent_row_says_no_attempt_was_recorded_when_none_was():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        row = _by_name(d.check(_kg_on(t), run=_runner(builder="graphify 1.0")))["knowledge graph auto-refresh is working"]
        assert "none recorded" in row["fix"]


def test_the_stale_row_carries_the_last_attempt_too():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _kg_on(t)
        out = pathlib.Path(t) / "graphify-out"; out.mkdir()
        graph = out / "graph.json"; graph.write_text("{}")
        old = time.time() - 3600; os.utime(graph, (old, old))
        corpus = pathlib.Path(base) / "knowledge" / "analysis"; corpus.mkdir(parents=True)
        (corpus / "issue-9.md").write_text("# newer than the graph\n")
        (pathlib.Path(base) / "state").mkdir()
        (pathlib.Path(base) / "state" / "kg-refresh.json").write_text(json.dumps({
            "at": time.time() - 60, "ok": False, "ran": False,
            "detail": "scope: full auto-refresh is not implemented"}))
        row = _by_name(d.check(base, run=_runner(builder="graphify 1.0")))["knowledge graph auto-refresh is working"]
        assert row["ok"] is False and "STALE" in row["fix"]
        assert "scope: full auto-refresh is not implemented" in row["fix"]


def test_a_garbage_refresh_record_reads_as_none_recorded():
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _kg_on(t)
        (pathlib.Path(base) / "state").mkdir()
        (pathlib.Path(base) / "state" / "kg-refresh.json").write_text("{not json")
        row = _by_name(d.check(base, run=_runner(builder="graphify 1.0")))["knowledge graph auto-refresh is working"]
        assert "none recorded" in row["fix"]


def test_verify_trap_fix_names_the_one_line_gesture():
    """#228: the fix is a command the user can paste, not only advice."""
    d = _doc()
    with tempfile.TemporaryDirectory() as t:
        base = _sdlc(t, {"verify": {"enforce": True, "command": ""}})
        fix = _by_name(d.check(base, run=_runner()))["verify command present (enforce is on)"]["fix"]
        assert "confirm .sdlc <n> <id>" in fix and "decline .sdlc" in fix


def test_an_empty_goal_verify_command_does_not_satisfy_the_verify_trap_row():
    """#228: `verify_command: ""` declares nothing -- loop.py verify reads it as NO-COMMAND -- so it
    must not turn this row green while every `done` is still refused."""
    d = _doc()
    for empty in ('verify_command: ""', "verify_command:", "verify_command:   ", "verify_command: ''"):
        with tempfile.TemporaryDirectory() as t:
            base = _sdlc(t, {"verify": {"enforce": True, "command": ""}})
            (pathlib.Path(base) / "goals").mkdir()
            (pathlib.Path(base) / "goals" / "0001.md").write_text(
                f"---\nstatus: pending\n{empty}\n---\nx\n")
            row = _by_name(d.check(base, run=_runner()))["verify command present (enforce is on)"]
            assert row["ok"] is False, empty


# --- #229: the init preflight's checks, as doctor rows; the fix depends on the FAILING check ---


def _pf_fake(remotes="origin\n", auth="Logged in. Token: gho_x\nToken scopes: 'repo', 'read:org'"):
    def run(args):
        table = {
            ("git", "rev-parse", "--is-inside-work-tree"): "true",
            ("git", "rev-parse", "--verify"): "abc",
            ("git", "rev-parse", "--abbrev-ref"): "main",
            ("git", "remote", "get-url"): "git@github.com:alice/app.git",
            ("git", "remote"): remotes,
            ("git", "ls-remote"): "abc\trefs/heads/main",
            ("gh", "auth", "status"): auth,
            ("gh", "api"): "User",
        }
        for prefix, answer in table.items():
            if tuple(args[:len(prefix)]) == prefix:
                return answer
        return ""
    return run


def test_control_gh_absent_doctor_never_says_gh_auth_login(tmp_path):
    """THE CONTROL the issue names: with `gh` absent, the old text said `gh auth login` (there is no
    gh to log in with). Now the gh row says install it, and no row anywhere says `gh auth login`."""
    d = _doc()
    base = _sdlc(tmp_path, {"work": {"enabled": True},
                            "discovery": {"source": "github", "github": {"project": {"enabled": True}}}})
    checks = d.check(base, run=_pf_fake(), which=lambda n: None if n == "gh" else n)
    c = _by_name(checks)
    assert c["gh installed"]["ok"] is False
    assert "cli.github.com" in c["gh installed"]["fix"]
    assert not any(n.startswith("gh auth") for n in c), c     # skipped: its prerequisite failed
    assert c["gh project scope"]["ok"] is False
    assert not [x["name"] for x in checks if "gh auth login" in x["fix"]], checks


def test_preflight_rows_all_green_but_missing_workflow(tmp_path):
    d = _doc()
    base = _sdlc(tmp_path, {"work": {"enabled": True}})
    c = _by_name(d.check(base, run=_pf_fake()))
    assert c["git repository"]["ok"] and c["git remote 'origin'"]["ok"] and c["gh auth"]["ok"]
    assert c["base branch 'main' on 'origin'"]["ok"] is True
    assert c["gh token scopes"]["ok"] is False
    assert c["gh token scopes"]["fix"].startswith("run: gh auth refresh -s workflow -h github.com")


def test_preflight_rows_no_remote_names_the_fallback(tmp_path):
    d = _doc()
    base = _sdlc(tmp_path, {"work": {"enabled": True}})
    c = _by_name(d.check(base, run=_pf_fake(remotes="")))
    assert c["git remote 'origin'"]["ok"] is False
    assert "git remote add origin" in c["git remote 'origin'"]["fix"]
    assert not any(n.startswith("base branch") for n in c)     # skipped behind the remote


def test_preflight_rows_absent_when_neither_work_nor_github(tmp_path):
    d = _doc()
    base = _sdlc(tmp_path, {"work": {"enabled": False}})
    names = _by_name(d.check(base, run=_pf_fake()))
    assert not any(n.startswith(("git ", "gh ")) for n in names), names


def test_cheap_only_runs_no_network_preflight_call_without_github(tmp_path):
    d = _doc()
    base = _sdlc(tmp_path, {"work": {"enabled": True}})
    calls = []
    fake = _pf_fake()

    def run(args):
        calls.append(args)
        return fake(args)
    d.check(base, run=run, cheap_only=True)
    assert not any(a[:2] in (["git", "ls-remote"], ["gh", "auth"], ["gh", "api"]) for a in calls), calls


def test_cheap_only_never_runs_ls_remote_or_owner_lookup_even_under_github(tmp_path):
    """Review block #1 (BLOCKING 2): the SessionStart wizard runs doctor with cheap_only=True in every
    repo. github discovery opting into `gh auth status` is NOT consent to `git ls-remote` (ssh, can
    stall for the whole call bound) or `gh api users/<owner>`: those are /sigma-doctor's."""
    d = _doc()
    base = _sdlc(tmp_path, {"work": {"enabled": True}, "discovery": {"source": "github"}})
    calls = []
    fake = _pf_fake()

    def run(args):
        calls.append(args)
        return fake(args)
    names = _by_name(d.check(base, run=run, cheap_only=True))
    assert not any(a[:2] == ["git", "ls-remote"]
                   or (a[:2] == ["gh", "api"] and any(str(x).startswith("users/") for x in a))
                   for a in calls), calls
    assert not any(n.startswith("base branch") for n in names)   # not checked here -> no row, no pass


def _hanging_ls_remote_repo(tmp_path, monkeypatch, cfg):
    """A real repo with an unreachable `origin`, and a `git` on PATH that delegates to the real git
    except `ls-remote`, which hangs (sleeps 60s) -- the dead-ssh-host shape the review measured."""
    real_git = shutil.which("git")
    repo = tmp_path / "repo"
    subprocess.run([real_git, "-c", "init.defaultBranch=main", "init", "-q", str(repo)], check=True)
    subprocess.run([real_git, "-C", str(repo), "-c", "user.email=a@b", "-c", "user.name=a",
                    "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    subprocess.run([real_git, "-C", str(repo), "remote", "add", "origin",
                    "ssh://git@unreachable.invalid/a/b.git"], check=True)
    stub = tmp_path / "bin"
    stub.mkdir()
    git = stub / "git"
    git.write_text(f"#!{sys.executable}\nimport os, sys, time\n"
                   "if sys.argv[1:2] == ['ls-remote']:\n    time.sleep(60)\n"
                   f"os.execv({real_git!r}, [{real_git!r}] + sys.argv[1:])\n", encoding="utf-8")
    git.chmod(0o755)
    monkeypatch.setenv("PATH", str(stub))                 # no gh on PATH: gh rows stop at "installed"
    return _sdlc(repo, cfg)


@pytest.mark.skipif(os.name == "nt" or not shutil.which("git"), reason="POSIX stub on PATH")
def test_wizard_status_is_fast_with_a_hanging_ls_remote(tmp_path, monkeypatch):
    base = _hanging_ls_remote_repo(tmp_path, monkeypatch,
                                   {"work": {"enabled": True}, "discovery": {"source": "github"}})
    spec = importlib.util.spec_from_file_location(
        "setup_wizard_229", D.parent.parent.parent / "sigma-init" / "scripts" / "setup_wizard.py")
    wiz = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wiz)
    start = time.monotonic()
    wiz.wizard_status(base, dismissed=set(), allow_cache=False)
    assert time.monotonic() - start < 2


@pytest.mark.skipif(os.name == "nt" or not shutil.which("git"), reason="POSIX stub on PATH")
def test_full_doctor_degrades_a_hanging_ls_remote_to_cannot_verify_within_the_bound(tmp_path,
                                                                                    monkeypatch):
    base = _hanging_ls_remote_repo(tmp_path, monkeypatch, {"work": {"enabled": True}})
    monkeypatch.setenv("SIGMA_WATCH_CALL_TIMEOUT", "2")
    d = _doc()
    pf = d._load_init_script("preflight")
    assert pf.network_timeout({}) == 15                   # the default bound is small, not 120s
    start = time.monotonic()
    rows = d._preflight_rows(pathlib.Path(base), json.loads(
        (pathlib.Path(base) / "config.json").read_text()), None, shutil.which, False, False)
    assert time.monotonic() - start < 10
    c = _by_name(rows)
    row = c["base branch 'main' on 'origin' (cannot verify)"]
    assert row["ok"] is False and "timed out" in row["fix"]


def _alias_run(ssh_answer):
    """Review block #2: origin is `git@github-work:alice/app.git` (an ~/.ssh/config alias). gh is
    logged in to github.com and -- exactly like real gh 2.98 -- to nothing named github-work."""
    base_fake = _pf_fake()
    calls = []

    def run(args):
        calls.append(list(args))
        if args[:2] == ["ssh", "-G"]:
            return ssh_answer
        if args[:3] == ["git", "remote", "get-url"]:
            return "git@github-work:alice/app.git"
        if args[:3] == ["gh", "auth", "status"]:
            host = args[args.index("--hostname") + 1] if "--hostname" in args else "github.com"
            return ("Logged in to github.com\n- Token: gho_x\n- Token scopes: 'repo', 'workflow', "
                    "'read:org', 'project'") if host == "github.com" else ""
        return base_fake(args)
    return run, calls


def _wizard():
    spec = importlib.util.spec_from_file_location(
        "setup_wizard_229b", D.parent.parent.parent / "sigma-init" / "scripts" / "setup_wizard.py")
    wiz = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wiz)
    return wiz


_ALIAS_CFG = {"work": {"enabled": True},
              "discovery": {"source": "github", "github": {"project": {"enabled": True}}}}


def test_wizard_never_demands_a_login_to_an_unresolvable_ssh_alias(tmp_path):
    """BLOCKING 1 of review block #2: the SessionStart wizard must not turn an ssh alias into a
    `gh auth login -h github-work` step (gh cannot log in to an alias). Unresolvable -> CANNOT
    VERIFY rows only, and a CANNOT VERIFY is never a wizard step."""
    base = _sdlc(tmp_path, _ALIAS_CFG)
    run, calls = _alias_run("")                     # ssh -G failed / unavailable
    status = _wizard().wizard_status(base, run=run, dismissed=set(), allow_cache=False)
    assert not [s for s in status["steps"] if s["name"].startswith("gh ")], status
    assert "github-work" not in json.dumps(status)
    assert not any("github-work" in x for a in calls if a[0] == "gh" for x in a), calls
    c = _by_name(_doc().check(base, run=_alias_run("")[0]))
    assert "gh auth (cannot verify)" in c and "gh project scope (cannot verify)" in c, list(c)
    assert not any("gh auth login" in r["fix"] for r in c.values())


def test_doctor_resolves_an_ssh_alias_to_its_real_host(tmp_path):
    base = _sdlc(tmp_path, _ALIAS_CFG)
    run, calls = _alias_run("user git\nhostname github.com\n")
    c = _by_name(_doc().check(base, run=run))
    assert c["gh auth"]["ok"] is True and c["gh project scope"]["ok"] is True
    assert ["gh", "auth", "status", "--active", "--hostname", "github.com"] in calls
    status = _wizard().wizard_status(base, run=_alias_run("hostname github.com")[0], dismissed=set(),
                                     allow_cache=False)
    assert not [s for s in status["steps"] if s["name"].startswith("gh ")], status


def _board_view(d, found=12, failure=None):
    """A doctor `run` for `gh project view`: board `found` answers; any other number fails with
    `failure` -- gh's real stderr for a missing board by default (measured: `gh project view 99999
    --owner <org>` -> "GraphQL: Could not resolve to a ProjectV2 with the number 99999.")."""
    import json as _json
    calls = []

    def run(a):
        calls.append(a)
        if a[:3] == ["gh", "project", "view"]:
            if a[3] == str(found) and "acme" in a:
                return _json.dumps({"number": found, "id": "PVT_x", "title": "widget — SDLC"})
            return d._RawFailure(failure if failure is not None else
                                 "GraphQL: Could not resolve to a ProjectV2 with the number %s. "
                                 "(organization.projectV2)" % a[3])
        return ""
    run.calls = calls
    return run


def test_pinned_board_reachable_row_235():
    """#235: a pinned project.number gets its own row -- ok when `gh project view` returns that
    board under the owner, FAIL (with the fix) when GitHub ANSWERED that it does not exist. No pin
    -> no row (the duplicate-board check owns that case)."""
    d = _doc()
    view = _board_view(d)
    gh = {"repo": "acme/widget", "project": {"enabled": True, "number": 12}}
    assert d._pinned_board_unreachable(gh, view) is None
    gone = d._pinned_board_unreachable(dict(gh, project={"enabled": True, "number": 13}), view)
    assert gone and "#13" in gone and "does not exist" in gone
    assert d._pinned_board_unreachable({"repo": "acme/widget", "project": {"enabled": True}},
                                       view) is None
    with tempfile.TemporaryDirectory() as t:
        cfg = {"discovery": {"source": "github", "github": gh}}
        rows = _by_name(d.check(_sdlc(t, cfg), run=view))
        assert rows["pinned board #12 reachable"]["ok"] is True
    with tempfile.TemporaryDirectory() as t:
        cfg = {"discovery": {"source": "github",
                             "github": dict(gh, project={"enabled": True, "number": 13})}}
        row = _by_name(d.check(_sdlc(t, cfg), run=view))["pinned board #13 reachable"]
        assert row["ok"] is False and "does not exist" in row["fix"]


@pytest.mark.parametrize("failure", [
    "",                                                        # gh gave nothing (killed, missing)
    "error connecting to api.github.com\ncheck your internet connection",
    "GraphQL: API rate limit exceeded for user ID 1.",
    "gh: No such file or directory",
    "error: your authentication token is missing required scopes [read:project]",
], ids=["empty", "offline", "rate-limit", "gh-missing", "scope"])
def test_pinned_board_row_is_silent_when_the_read_itself_failed_235(failure):
    """Review of PR #279: a read that FAILED says nothing about the board, so it is no row at all
    (never a FAIL, never a pass) -- only GitHub answering "could not resolve" is an alarm. The
    missing-scope case has its own row ("gh project scope") and is not doubled here."""
    d = _doc()
    view = _board_view(d, failure=failure)
    gh = {"repo": "acme/widget", "project": {"enabled": True, "number": 13}}
    assert d._pinned_board_unreachable(gh, view) is None
    with tempfile.TemporaryDirectory() as t:
        cfg = {"discovery": {"source": "github", "github": gh}}
        names = _by_name(d.check(_sdlc(t, cfg), run=view))
        assert not [n for n in names if n.startswith("pinned board")], names


def test_pinned_board_read_is_skipped_under_cheap_only_235():
    """The read is a GraphQL call (`gh project view`): the unconditional SessionStart wizard
    (`cheap_only=True`) must not spend it, like the preflight network checks."""
    d = _doc()
    view = _board_view(d)
    gh = {"repo": "acme/widget", "project": {"enabled": True, "number": 12}}
    with tempfile.TemporaryDirectory() as t:
        cfg = {"discovery": {"source": "github", "github": gh}}
        names = _by_name(d.check(_sdlc(t, cfg), run=view, cheap_only=True))
    assert not [c for c in view.calls if c[:3] == ["gh", "project", "view"]], view.calls
    assert not [n for n in names if n.startswith("pinned board")]


def test_runner_failure_contract_reaches_the_pinned_board_row_235():
    """Review of PR #279: `_pinned_board_state` tells "GitHub answered: no such board" from "the
    read failed" only through the text the REAL runner attaches to a failure. Pin that coupling end
    to end: `_real_run` on a process that exits 1 with gh's real stderr must come back falsy, carry
    the text through `_failure_text`, and turn the row into "gone"; the same text on a SUCCESS is
    not a failure. Break `_RawFailure.raw` or `_failure_text` and this goes red."""
    import sys as _sys
    d = _doc()
    said = "GraphQL: Could not resolve to a ProjectV2 with the number 13. (organization.projectV2)"
    fail = [_sys.executable, "-c", "import sys; sys.stderr.write(%r); sys.exit(1)" % said]
    res = d._real_run(fail)
    assert not res and d._failure_text(res) == said
    assert d._failure_text("") == "" and d._failure_text('{"number": 1}') == ""
    gh = {"repo": "acme/widget", "project": {"enabled": True, "number": 13}}
    state, fix = d._pinned_board_state(gh, lambda _a: d._real_run(fail))
    assert state == "gone" and "#13" in fix
    offline = [_sys.executable, "-c", "import sys; sys.stderr.write('error connecting'); sys.exit(1)"]
    assert d._pinned_board_state(gh, lambda _a: d._real_run(offline))[0] == "unverifiable"


def test_decision_gate_state_is_not_on_when_the_repo_is_not_adopted(tmp_path):
    """#622: the hook is inert without .sdlc/config.json, so a registry alone must not read ON.
    Only a direct call reaches this branch (`_sdlc` always writes config.json)."""
    d = _doc()
    base = tmp_path / ".sdlc"; base.mkdir()
    (base / "decisions.json").write_text(json.dumps({"decisions": [
        {"id": "INV-1", "class": "invariant", "status": "active"}]}))
    state = d._decision_gate_state(str(base), {})
    assert not state.startswith("ON") and "not adopted" in state.lower() and "/sigma-init" in state
    (base / "config.json").write_text("{}")
    assert d._decision_gate_state(str(base), {}).startswith("ON")


# ---------------------------------------------------------- #614: stale core.hooksPath (AC-3)

#: The value earlier Sigma releases wrote (#218). Assembled: the public-surface guard rejects the
#: raw directory name.
_STALE_HOOKS = "." + "git" + "hooks"
_HOOKS_ROW = "git hooks are not switched off by a stale core.hooksPath"


def _hooks_repo(tmp_path, value=None, make_dir=False):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, env=env)
    if value:
        subprocess.run(["git", "-C", str(tmp_path), "config", "--local", "core.hooksPath", value],
                       check=True, env=env)
    if make_dir:
        (tmp_path / value).mkdir(parents=True)
    return _sdlc(tmp_path, {})


def test_a_stale_hook_path_is_a_failing_row_carrying_the_unset_command(tmp_path):
    """#614 AC-3: the key an earlier /sigma-init wrote, naming a directory that does not exist,
    makes git run no hooks -- doctor fails the row and prints the one command that undoes it."""
    base = _hooks_repo(tmp_path, _STALE_HOOKS)
    rows = _by_name(_doc().check(base, run=_runner()))
    assert _HOOKS_ROW in rows, sorted(rows)
    row = rows[_HOOKS_ROW]
    assert row["ok"] is False
    assert "git config --local --unset core.hooksPath" in row["fix"]


@pytest.mark.parametrize("value, make_dir", [(None, False), (_STALE_HOOKS, True),
                                             (".husky/_", False)])
def test_no_hook_path_row_unless_the_key_is_stale(tmp_path, value, make_dir):
    """No key, the adopter's own directory under that name, or any other value: no row at all."""
    base = _hooks_repo(tmp_path, value, make_dir)
    assert _HOOKS_ROW not in _by_name(_doc().check(base, run=_runner()))


# --- #801 slice 1: advisory GraphQL-capability row (detection and reporting only) ----------------

GQL_ROW = "GitHub GraphQL unavailable (cloud proxy)"


def test_graphql_row_appears_in_cloud_env_as_advisory_ok_true(tmp_path, monkeypatch):
    d = _doc()
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    base = _sdlc(tmp_path, {"work": {"enabled": True}})
    checks = d.check(base, run=_pf_fake())
    row = _by_name(checks)[GQL_ROW]
    assert row["ok"] is True                         # advisory: not a MISSING gap the user can fix
    for text in ("board/Projects mirroring", "gh pr merge --auto", "timelineItems (blocker/dependency edges)",
                 "gh issue|pr via GraphQL until migrated (#801 slices 2-4)",
                 "detection only; REST migration is in progress, not complete", "SIGMA_GH_GRAPHQL"):
        assert text in row["fix"], text
    assert row not in [c for c in checks if not c["ok"]]


def test_graphql_row_names_the_landing_degradation(tmp_path, monkeypatch):
    d = _doc()
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    base = _sdlc(tmp_path, {"work": {"enabled": True}})
    row = _by_name(d.check(base, run=_pf_fake()))[GQL_ROW]
    for text in ("draft readiness (gh pr ready)", "Unit landing uses REST only"):
        assert text in row["fix"], text


def test_graphql_row_is_printed_by_the_check_command(tmp_path, monkeypatch, capsys):
    d = _doc()
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "1")
    base = _sdlc(tmp_path, {"work": {"enabled": True}})
    orig = d.check
    monkeypatch.setattr(d, "check", lambda sdlc_dir=".sdlc": orig(sdlc_dir, run=_pf_fake()))
    d.main(["doctor.py", "check", base])
    out = capsys.readouterr().out
    assert GQL_ROW in out and "board/Projects mirroring" in out    # hand-built ok=True fix text is printed


def test_graphql_row_absent_when_no_cloud_signal(tmp_path):
    d = _doc()
    base = _sdlc(tmp_path, {"work": {"enabled": True}})
    assert GQL_ROW not in _by_name(d.check(base, run=_pf_fake()))


def test_graphql_row_override_off_names_the_source(tmp_path, monkeypatch):
    d = _doc()
    monkeypatch.setenv("SIGMA_GH_GRAPHQL", "off")
    base = _sdlc(tmp_path, {"work": {"enabled": True}})
    row = _by_name(d.check(base, run=_pf_fake()))[GQL_ROW]
    assert row["ok"] is True and "override" in row["fix"]


def test_graphql_row_absent_when_neither_work_nor_github(tmp_path, monkeypatch):
    d = _doc()
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    base = _sdlc(tmp_path, {"work": {"enabled": False}})
    assert GQL_ROW not in _by_name(d.check(base, run=_pf_fake()))


def test_graphql_row_changes_nothing_else_and_never_runs_graphql(tmp_path, monkeypatch):
    d = _doc()
    base = _sdlc(tmp_path, {"work": {"enabled": True}})
    calls = []
    fake = _pf_fake()

    def run(args):
        calls.append(list(args))
        return fake(args)
    before = {c["name"] for c in d.check(base, run=run)}
    calls_before = list(calls)
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    calls.clear()
    after = {c["name"] for c in d.check(base, run=run)}
    assert after - before == {GQL_ROW} and before <= after
    assert calls == calls_before        # the capability row adds NO `run` call (no probe, no graphql)


def test_graphql_row_load_failure_is_fail_open(tmp_path, monkeypatch):
    d = _doc()
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    real = d._load_loop_script
    monkeypatch.setattr(d, "_load_loop_script",
                        lambda n: (_ for _ in ()).throw(OSError("x")) if n == "gh_api" else real(n))
    base = _sdlc(tmp_path, {"work": {"enabled": True}})
    assert GQL_ROW not in _by_name(d.check(base, run=_pf_fake()))


# --- #895 slice 2c: doctor's six label-scan list reads go REST first (gh_api.list_issues_gh) ------
# Doctor's `run` takes the FULL argv (with "gh") and NEVER raises: a failed call is a falsy
# `_RawFailure`. Fed straight to the REST fetch that reads as an EMPTY list = success (the failure-as-
# empty trap), so the list reads go through `_raising_gh`. Doctor stays READ-ONLY: it passes no sdlc_dir,
# so it never writes the breaker or the fallback log.

_CFG = {"repo": "acme/widget"}
_429 = "gh: API rate limit exceeded (HTTP 429)"


def _scan_run(rest, fallback="[]", log=None):
    """`rest`: a str (returned for every REST list call) or a callable(args) -> str|_RawFailure.
    A `gh issue list` argv (the ONE fallback) is recorded in `log['fb']`."""
    log = log if log is not None else {}
    log.setdefault("fb", []); log.setdefault("rest", []); log.setdefault("other", [])

    def run(args):
        if _is_list(args):
            log["rest"].append(list(args))
            return rest(args) if callable(rest) else rest
        if args[:3] == ["gh", "issue", "list"]:
            log["fb"].append(list(args))
            return fallback(args) if callable(fallback) else fallback
        log["other"].append(list(args))
        return ""
    run.log = log
    return run


@pytest.fixture
def gh_env(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)
    monkeypatch.delenv("SIGMA_GH_GRAPHQL", raising=False)


def test_unreachable_scan_requests_rest_created_desc_and_never_falls_back_when_healthy(gh_env):
    d = _doc()
    run = _scan_run(json.dumps([{"number": 7, "labels": [{"name": "sdlc:blocking"}]}]))
    assert d._unreachable_blocker_scan(_CFG, run) == [7]
    p = gqlfake.rest_list_params(run.log["rest"][0][1:])
    assert p["labels"] == "sdlc:blocking" and p["state"] == "open" and p["per_page"] == "100"
    assert p["sort"] == "created" and p["direction"] == "desc"
    assert run.log["fb"] == [] and run.log["rest"][0][1] == "api"


def test_dependency_scan_asks_for_updated_desc_not_a_search(gh_env):
    d = _doc()
    run = _scan_run(json.dumps([{"number": 1, "body": "x"}]))
    d._dependency_marker_scan(_CFG, {}, run)
    p = gqlfake.rest_list_params(run.log["rest"][0][1:])
    assert p["sort"] == "updated" and p["direction"] == "desc" and "--search" not in run.log["rest"][0]


def test_open_issue_done_card_lists_with_the_required_repo(gh_env):
    d = _doc()
    cfg = {"repo": "acme/widget", "project": {"enabled": True, "number": 8, "owner": "acme"}}
    run = _scan_run(json.dumps([{"number": 5}]))
    d._open_issue_done_card(cfg, run)
    assert run.log["rest"][0][2] == "repos/acme/widget/issues"


def test_a_429_makes_the_site_fall_back_exactly_once_not_read_as_an_empty_board(gh_env):
    d = _doc()
    fb = json.dumps([{"number": 7, "labels": [{"name": "sdlc:blocking"}]}])
    run = _scan_run(d._RawFailure(_429), fallback=fb)
    assert d._unreachable_blocker_scan(_CFG, run) == [7]       # the fallback's data, not []
    assert run.log["fb"] == [["gh", "issue", "list", "--repo", "acme/widget", "--label", "sdlc:blocking",
                              "--state", "open", "--json", "number,labels", "--limit", "200"]]
    assert len(run.log["rest"]) == 1


def test_dependency_scan_fallback_keeps_the_updated_order(gh_env):
    d = _doc()
    run = _scan_run(d._RawFailure(_429), fallback=json.dumps([{"number": 1, "body": "x"}]))
    d._dependency_marker_scan(_CFG, {}, run)
    assert run.log["fb"][0][-2:] == ["--search", "sort:updated-desc"] and len(run.log["fb"]) == 1


@pytest.mark.parametrize("raw", ["gh: Bad credentials (HTTP 401)", "gh: Not Found (HTTP 404)",
                                 "gh: Validation Failed (HTTP 422)"])
def test_client_errors_are_none_without_a_fallback(gh_env, raw):
    d = _doc()
    run = _scan_run(d._RawFailure(raw), fallback="[]")
    assert d._blocked_label_scan(_CFG, run) is None and d._unreachable_blocker_scan(_CFG, run) == []
    assert run.log["fb"] == []


def test_empty_and_malformed_pages_are_kind_other_so_none_not_an_empty_census(gh_env):
    d = _doc()
    for payload in ("", "not json", json.dumps({"oops": 1})):
        run = _scan_run(payload)
        assert d._blocked_label_scan(_CFG, run) is None, payload
        assert run.log["fb"] == [], payload


def test_cloud_session_never_falls_back_from_doctor(monkeypatch):
    d = _doc()
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    run = _scan_run(d._RawFailure(_429), fallback=json.dumps([{"number": 7, "labels": []}]))
    assert d._blocked_label_scan(_CFG, run) is None
    assert run.log["fb"] == []


def test_transport_failure_falls_back_once_and_a_failed_fallback_is_none(gh_env):
    d = _doc()
    fail = d._RawFailure("dial tcp: i/o timeout")
    run = _scan_run(fail, fallback=fail)
    assert d._blocked_label_scan(_CFG, run) is None and len(run.log["fb"]) == 1


def test_multi_state_scan_survives_one_label_failing_and_still_flags_the_rest(gh_env):
    d = _doc()
    issue = {"number": 3, "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:parked"}]}

    def rest(args):
        p = gqlfake.rest_list_params(args[1:])
        return d._RawFailure("gh: Not Found (HTTP 404)") if p["labels"] == "sdlc:needs-confirmation" \
            else json.dumps([issue])
    run = _scan_run(rest)
    assert d._multi_state_label_scan(_CFG, {}, run) == [(3, ["sdlc:goal", "sdlc:parked"])]


def _census_fb_run(by_label, rest_fail, log):
    """REST list -> `rest_fail` (a _RawFailure); the ONE `gh issue list` fallback answers `by_label`."""
    def run(args):
        if args[:3] == ["gh", "auth", "status"]:
            return "Logged in."
        if _is_list(args):
            return rest_fail
        if args[:3] == ["gh", "issue", "list"]:
            log.append(list(args))
            label = args[args.index("--label") + 1]
            state = args[args.index("--state") + 1]
            return json.dumps(by_label.get((label, state), []))
        return ""
    return run


def test_census_row_completes_from_the_fallback_when_rest_is_rate_limited(tmp_path, gh_env):
    """A wrapper that SWALLOWED the failure (returned "" for it) would read 429 as an empty census, and
    this would see no anomaly at all: the fallback's data must reach the census."""
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}})
    log = []
    run = _census_fb_run({("sdlc:goal", "closed"): [_closed_issue(7, "sdlc:goal", "sdlc:parked")]},
                         d._RawFailure(_429), log)
    names = {c["name"]: c for c in d.check(base, run=run)}
    assert _CENSUS_NAME in names and names[_CENSUS_NAME]["ok"] is False and "#7" in names[_CENSUS_NAME]["fix"]
    assert _CENSUS_READ_NAME not in names and log


def test_census_row_says_it_could_not_read_in_full_on_an_outage(tmp_path, gh_env):
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}})
    log = []
    run = _census_fb_run({}, d._RawFailure("gh: Server Error (HTTP 503)"), log)

    def both_fail(args):
        out = run(args)
        if args[:3] == ["gh", "issue", "list"]:
            return d._RawFailure("gh: Server Error (HTTP 503)")
        return out
    names = {c["name"]: c for c in d.check(base, run=both_fail)}
    assert _CENSUS_READ_NAME in names and names[_CENSUS_READ_NAME]["ok"] is False
    assert _CENSUS_NAME not in names
    assert "NOT a clean bill of health" in names[_CENSUS_READ_NAME]["fix"]


def test_doctor_is_read_only_it_writes_no_breaker_or_fallback_log_even_after_a_fallback_class_failure(tmp_path, gh_env):
    d = _doc()
    base = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}})
    log = []
    for _ in range(4):                                  # more than the breaker threshold
        d.check(base, run=_census_fb_run({}, d._RawFailure(_429), log))
    state = pathlib.Path(base) / "state"
    assert log                                          # the fallback WAS exercised
    assert not (state / "gh-rest-breaker.json").exists() and not (state / "gh-fallback.json").exists()
    assert not list(pathlib.Path(base).rglob("gh-fallback.json")) and not list(pathlib.Path(base).rglob("gh-rest-breaker.json"))
