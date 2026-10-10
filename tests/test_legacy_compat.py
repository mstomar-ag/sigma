"""#239: a repository adopted under the plugin's PREVIOUS name keeps working under Sigma.

Read side only here (the one-shot `migrate.py` has its own file, `test_legacy_migrate.py`). Every
legacy spelling below is built from fragments -- `RETIRED` -- because the previous name is a guarded
private name in this tree (`tests/test_no_private_names.py`, #2729): no shipped line, this file
included, carries it whole.

What is pinned:
  - the ONE helper (`skills/sigma-loop/scripts/legacy.py`): env precedence (Sigma wins; empty is
    unset; internal hand-off names never fall back), schema ids, marker spellings, the renamed
    `drift_watch.channels` key;
  - each reader that used to compare against the Sigma spelling only now reads the legacy one too;
  - structural pins: no shipped Python reads an operator-facing `SIGMA_*` name directly, every
    `SIGMA_*` literal is a name the helper knows, and every marker literal is registered.
"""
import importlib.util
import json
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOOP = ROOT / "skills" / "sigma-loop" / "scripts"
INIT = ROOT / "skills" / "sigma-init" / "scripts"

RETIRED = "loop" + "smith"
RETIRED_ENV = RETIRED.upper() + "_"


def _load(directory, stem):
    spec = importlib.util.spec_from_file_location(stem, directory / (stem + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def legacy():
    return _load(LOOP, "legacy")


# --------------------------------------------------------------------------- env


def test_sigma_name_wins_when_both_are_set(legacy):
    env = {"SIGMA_WATCH_INTERVAL": "60", RETIRED_ENV + "WATCH_INTERVAL": "5"}
    assert legacy.getenv("SIGMA_WATCH_INTERVAL", environ=env) == "60"


def test_legacy_name_is_read_when_the_sigma_name_is_unset(legacy):
    env = {RETIRED_ENV + "WATCH_INTERVAL": "5"}
    assert legacy.getenv("SIGMA_WATCH_INTERVAL", environ=env) == "5"


def test_an_empty_sigma_value_counts_as_unset(legacy):
    env = {"SIGMA_GATE_GLOBAL": "", RETIRED_ENV + "GATE_GLOBAL": "1"}
    assert legacy.getenv("SIGMA_GATE_GLOBAL", environ=env) == "1"


def test_with_no_legacy_value_behaviour_is_exactly_the_old_get(legacy):
    assert legacy.getenv("SIGMA_CLAUDE_CMD", "dflt", environ={}) == "dflt"
    assert legacy.getenv("SIGMA_CLAUDE_CMD", "dflt", environ={"SIGMA_CLAUDE_CMD": ""}) == ""
    assert legacy.getenv("SIGMA_CLAUDE_CMD", environ={}) is None


@pytest.mark.parametrize("name", ["SIGMA_RUN_ID", "SIGMA_AUTOWATCH_HOP"])
def test_internal_hand_off_names_never_fall_back(legacy, name):
    """Sigma sets these for its OWN children; a stale value from the old plugin's supervisor must not
    be adopted as this run's identity (plan-review finding 3)."""
    env = {RETIRED_ENV + name[len("SIGMA_"):]: "stale"}
    assert legacy.getenv(name, environ=env) is None
    assert name in legacy.INTERNAL_ENV and name not in legacy.FALLBACK_ENV


def test_a_config_value_naming_the_legacy_variable_prefers_the_sigma_one(legacy):
    legacy_name = RETIRED_ENV + "SLACK_BOT_TOKEN"
    assert legacy.getenv(legacy_name, environ={legacy_name: "old"}) == "old"
    assert legacy.getenv(legacy_name, environ={legacy_name: "old",
                                               "SIGMA_SLACK_BOT_TOKEN": "new"}) == "new"


def test_an_unrelated_config_named_variable_is_read_literally(legacy):
    assert legacy.getenv("MY_SMTP", environ={"MY_SMTP": "x"}) == "x"
    assert legacy.getenv(RETIRED_ENV + "NOT_A_KNOWN_NAME", environ={}) is None


def test_legacy_env_values_walks_every_env_key(legacy):
    cfg = {"a": {"bot_token_env": RETIRED_ENV + "SLACK_BOT_TOKEN"},
           "b": [{"pass_env": RETIRED_ENV + "SMTP_PASS"}], "c_env": "SIGMA_X", "note": RETIRED_ENV}
    assert legacy.legacy_env_values(cfg) == [("a.bot_token_env", RETIRED_ENV + "SLACK_BOT_TOKEN"),
                                             ("b.pass_env", RETIRED_ENV + "SMTP_PASS")]


# --------------------------------------------------------------------------- schema / markers / keys


def test_schema_ids(legacy):
    assert legacy.schema_is(RETIRED + "/features@1", "sigma/features@1")
    assert legacy.schema_is("sigma/features@1", "sigma/features@1")
    assert not legacy.schema_is(RETIRED + "/features@2", "sigma/features@1")
    assert not legacy.schema_is(RETIRED + "/unknown@1", "sigma/unknown@1")
    assert legacy.is_legacy_schema(RETIRED + "/landing@1")
    assert not legacy.is_legacy_schema("sigma/landing@1") and not legacy.is_legacy_schema(None)


def test_marker_spellings(legacy):
    assert legacy.spellings("<!-- sigma:begin managed") == (
        "<!-- sigma:begin managed", "<!-- %s:begin managed" % RETIRED)
    assert legacy.spellings("sigma:keep-parked") == ("sigma:keep-parked", RETIRED + ":keep-parked")
    assert legacy.has_marker("x %s:keep-parked y" % RETIRED, "sigma:keep-parked")
    assert not legacy.has_marker("sigma:keep", "sigma:keep-parked")
    text = "a <!-- %s:unpark-qa:start --> b <!-- sigma:unpark-qa:start -->" % RETIRED
    at, spelled = legacy.find_marker(text, "<!-- sigma:unpark-qa:start -->")
    assert at == 2 and spelled.startswith("<!-- " + RETIRED)
    assert legacy.find_marker("nothing", "sigma:x") == (-1, None)


def test_channel_key_fallback(legacy):
    assert legacy.channel_value({RETIRED: "C1"}, "sigma") == "C1"
    assert legacy.channel_value({"sigma": "C2", RETIRED: "C1"}, "sigma") == "C2"
    assert legacy.channel_value({"sigma": "", RETIRED: "C1"}, "sigma") == "C1"
    assert legacy.channel_value({RETIRED: "C1"}, "org") is None


# --------------------------------------------------------------------------- readers


def _legacy_registry(tmp_path):
    sdlc = tmp_path / ".sdlc"
    units = sdlc / "features" / "units"
    units.mkdir(parents=True)
    entry = {"title": "Voice", "owner": "o", "repos": {"a/b": {"branch": "feature/voice",
                                                                "goals": [7]}}}
    (sdlc / "features" / "index.json").write_text(json.dumps(
        {"schema": RETIRED + "/features@1", "features": {"voice": entry, "bill": {"title": "B"}}}))
    (units / "bill.json").write_text(json.dumps(
        {"schema": RETIRED + "/features@1", "features": {"bill": {"title": "Billing"}}}))
    return sdlc


def test_the_registry_reads_a_legacy_index_and_shard(tmp_path):
    reg = _load(LOOP, "feature_registry")
    sdlc = _legacy_registry(tmp_path)
    got = reg.read(reg.registry_dir(str(sdlc)))
    assert got["voice"]["title"] == "Voice" and got["voice"]["repos"]["a/b"]["goals"] == [7]
    assert got["bill"]["title"] == "Billing"
    assert reg.parse({"schema": RETIRED + "/features@2", "features": {"v": {}}}) == {}


def test_a_fold_of_a_legacy_registry_loses_nothing(tmp_path):
    fsync = _load(LOOP, "feature_sync")
    sdlc = _legacy_registry(tmp_path)
    fsync.fold(str(sdlc))
    doc = json.loads((sdlc / "features" / "index.json").read_text())
    assert doc["schema"] == "sigma/features@1"
    assert set(doc["features"]) == {"voice", "bill"}


def test_a_registry_write_that_replaces_a_legacy_schema_id_says_so_once(tmp_path, capsys):
    """#239: the fold's `write_index` and a pick's `write_unit` respell an old schema id, so they
    say so on stderr -- the same line `feature_doc.sync` and `upstream._history` print -- and stay
    silent when the file already carried Sigma's id or did not exist."""
    fsync = _load(LOOP, "feature_sync")
    reg = _load(LOOP, "feature_registry")
    sdlc = _legacy_registry(tmp_path)
    fdir = reg.registry_dir(str(sdlc))
    fsync.fold(str(sdlc))
    err = capsys.readouterr().err
    assert "migrated legacy schema id '%s/features@1'" % RETIRED in err
    assert "index.json to sigma/features@1 on use" in err
    fsync.fold(str(sdlc))                              # already Sigma's id: silent
    assert "migrated legacy" not in capsys.readouterr().err
    (fdir / "units" / "bill.json").write_text(json.dumps(
        {"schema": RETIRED + "/features@1", "features": {"bill": {"title": "Billing"}}}))
    reg.write_unit(fdir, "bill", {"title": "Billing"})
    err = capsys.readouterr().err
    assert err.count("migrated legacy schema id") == 1 and "bill.json" in err
    reg.write_unit(fdir, "fresh", {"title": "New"})    # no file before: silent
    assert "migrated legacy" not in capsys.readouterr().err


def test_the_landing_record_reads_the_legacy_schema(tmp_path):
    cross = _load(LOOP, "cross_repo")
    path = cross.decision_path(str(tmp_path), "101")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema": RETIRED + "/landing@1", "goal": "101",
                                "outcome": "not-cross-repo"}))
    assert cross.recorded(str(tmp_path), "101")["outcome"] == "not-cross-repo"


def test_the_propagation_record_reads_the_legacy_schema(tmp_path):
    prop = _load(LOOP, "feature_propagate")
    path = prop.record_path(str(tmp_path), "101")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema": RETIRED + "/propagation@1", "goal": "101"}))
    assert prop.recorded(str(tmp_path), "101")["goal"] == "101"


def test_the_withheld_index_reads_the_legacy_schema(tmp_path):
    up = _load(LOOP, "upstream")
    path = up.index_path(str(tmp_path), "101")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema": RETIRED + "/withheld@1", "findings": {"f": 1},
                                "upstream": []}))
    got, readable = up._history(str(tmp_path), "101")
    assert readable and got["findings"] == {"f": 1}


def test_propagation_refuses_to_overwrite_a_sibling_still_on_the_legacy_schema():
    """Plan-review BLOCKER 1: accepting the legacy id on READ must not become a licence to rewrite a
    sibling repository that the old plugin still owns."""
    prop = _load(LOOP, "feature_propagate")
    current = json.dumps({"schema": RETIRED + "/features@1", "features": {"voice": {"title": "V"}}})
    with pytest.raises(prop._Unreadable, match="previous name"):
        prop._their_entry(current, "voice")


def _legacy_doc(name, entry):
    fdoc = _load(LOOP, "feature_doc")
    text = fdoc.render_doc(name, entry)
    return fdoc, text.replace("<!-- sigma:", "<!-- %s:" % RETIRED)


def test_a_legacy_feature_doc_is_located_and_synced_without_a_second_block(tmp_path):
    entry = {"title": "Voice", "owner": "o"}
    fdoc, text = _legacy_doc("voice", entry)
    features = tmp_path / ".sdlc" / "features"
    features.mkdir(parents=True)
    path = features / "voice.md"
    path.write_bytes((text + "\n## Notes\n\nmine\n").encode())
    state, parsed, _ = fdoc.parse_doc(path.read_bytes())
    assert state == fdoc.INTACT and parsed["title"] == "Voice"
    report = fdoc.sync(features, "voice", entry, sdlc_dir=tmp_path / ".sdlc")
    after = path.read_text()
    assert report["outcome"] == fdoc.UPDATED and not report["diverged"]
    assert after.count("<!-- sigma:begin managed") == 1 and RETIRED not in after
    assert after.endswith("## Notes\n\nmine\n")


def test_a_sigma_block_beside_a_legacy_block_is_managed_and_the_legacy_one_left(tmp_path):
    """Plan-review BLOCKER 2: a teammate still on the old plugin writes its own block; Sigma manages
    its own pair and leaves the other as prose -- no GARBLED lock-up."""
    entry = {"title": "Voice"}
    fdoc, legacy_text = _legacy_doc("voice", entry)
    both = fdoc.render_block("voice", entry) + "\n\n" + legacy_text
    where = fdoc.locate(both.encode())
    assert where.state == fdoc.INTACT and where.start == 0


def test_a_mixed_legacy_begin_and_sigma_end_is_garbled():
    fdoc = _load(LOOP, "feature_doc")
    text = "<!-- %s:begin managed -->\nbody\n<!-- sigma:end managed -->\n" % RETIRED
    assert fdoc.locate(text.encode()).state == fdoc.GARBLED


def test_the_codex_block_replaces_a_legacy_block_in_place(tmp_path):
    init = _load(INIT, "sdlc_init")
    old = ("# Mine\n\n<!-- %s:codex:start -->\nold words\n<!-- %s:codex:end -->\n\ntail\n"
           % (RETIRED, RETIRED))
    (tmp_path / "AGENTS.md").write_text(old)
    assert init.scaffold_codex(tmp_path)
    new = (tmp_path / "AGENTS.md").read_text()
    assert new.startswith("# Mine\n\n<!-- sigma:codex:start -->") and new.endswith("\n\ntail\n")
    assert RETIRED not in new and new.count("sigma:codex:start") == 1
    assert not init.scaffold_codex(tmp_path)          # idempotent


def test_the_drift_channel_reads_the_renamed_key():
    drift = _load(LOOP, "drift_watch")
    slack = _load(LOOP, "slack_client")
    cfg = {"drift_watch": {"channels": {RETIRED: "C0LEGACY01", "org": None}}}
    assert drift._channel_id(cfg) == "C0LEGACY01"
    assert slack._resolve_channel(cfg, "sigma") == ("C0LEGACY01", None)


def test_the_slack_token_named_by_config_falls_back(monkeypatch):
    slack = _load(LOOP, "slack_client")
    sent = []
    monkeypatch.setenv(RETIRED_ENV + "SLACK_BOT_TOKEN", "xoxb-legacy")
    monkeypatch.delenv("SIGMA_SLACK_BOT_TOKEN", raising=False)
    post = lambda token, ch, text: sent.append(token) or True   # noqa: E731 - DI seam
    assert slack.post_message("C0123456", "hi", {}, post=post)
    assert sent == ["xoxb-legacy"]


def test_a_legacy_pr_block_directive_is_seen_by_the_merge_gate():
    work = _load(LOOP, "work")
    assert work._line_directive("%s:block\nplease fix" % RETIRED) == "block"
    assert work._line_directive("%s:approve" % RETIRED.upper()) == "approve"


def test_a_legacy_keep_parked_opt_out_is_honoured():
    unpark = _load(LOOP, "auto_unpark")
    assert unpark._is_exempt(["please leave it <!-- %s:keep-parked -->" % RETIRED])


def test_a_legacy_dismissal_is_honoured():
    bc = _load(LOOP, "backlog_check")
    text = "<!-- %s:dismissed-finding kind=blocked-by ref=9 -->" % RETIRED
    assert bc._dismissed_findings(text) == {("blocked-by", "9")}
    assert bc._filter_dismissal_comments([text, "needs #9"]) == ["needs #9"]


def test_a_legacy_unpark_qa_block_is_stripped_before_the_blocker_scan():
    scan = _load(LOOP, "blocker_scan")
    body = "a <!-- %s:unpark-qa:start --> needs #9 <!-- %s:unpark-qa:end --> b" % (RETIRED, RETIRED)
    assert scan.strip_unpark_qa(body) == "a  b"


def test_the_watch_interval_env_falls_back():
    wd = _load(LOOP, "watch_daemon")
    got, warning = wd.resolve_interval({}, {RETIRED_ENV + "WATCH_INTERVAL": "77"})
    assert (got, warning) == (77, None)


def test_the_run_identity_never_adopts_a_legacy_run_id(monkeypatch):
    state = _load(LOOP, "state")
    monkeypatch.delenv("SIGMA_RUN_ID", raising=False)
    monkeypatch.setenv(RETIRED_ENV + "RUN_ID", "legacy-run")
    assert state.run_identity() is None


# --------------------------------------------------------------------------- structural pins

_SHIPPED = ([p for p in sorted((ROOT / "skills").rglob("*")) if p.suffix in (".py", ".sh", ".ts")
             and "node_modules" not in p.parts]
            + sorted((ROOT / "hooks").glob("*.py")) + sorted((ROOT / "hooks").glob("*.sh")))
_SIGMA_NAME = re.compile(r"\bSIGMA_[A-Z][A-Z0-9_]*[A-Z0-9]\b")
_DIRECT_READ = re.compile(r"""(?:environ\.get\(|environ\[|\benv\.get\(|\bos\.getenv\()\s*["'](SIGMA_[A-Z0-9_]+)""")


def test_every_sigma_env_literal_is_a_name_the_helper_knows(legacy):
    known = legacy.FALLBACK_ENV | legacy.INTERNAL_ENV | legacy.POST_RENAME_ENV
    unknown = sorted({"%s: %s" % (p.relative_to(ROOT).as_posix(), m.group())
                      for p in _SHIPPED for m in _SIGMA_NAME.finditer(p.read_text(encoding="utf-8"))
                      if m.group() not in known})
    assert not unknown, "SIGMA_* names the legacy helper does not know: %s" % unknown


def _direct_reads(paths, fallback):
    return sorted("%s: %s" % (p.name, m.group(1)) for p in paths if p.name != "legacy.py"
                  for m in _DIRECT_READ.finditer(p.read_text(encoding="utf-8"))
                  if m.group(1) in fallback)


def test_no_shipped_python_reads_an_operator_env_name_directly(legacy):
    found = _direct_reads([p for p in _SHIPPED if p.suffix == ".py"], legacy.FALLBACK_ENV)
    assert not found, "read these through legacy.getenv so the previous name still works: %s" % found


def test_the_direct_read_pin_can_fail(legacy, tmp_path):
    """The control for the pin above, run every time: a planted direct read is found."""
    plant = tmp_path / "planted.py"
    plant.write_text('x = env.get("SIGMA_WATCH_INTERVAL")\ny = os.environ["SIGMA_GATE_GLOBAL"]\n')
    assert _direct_reads([plant], legacy.FALLBACK_ENV) == [
        "planted.py: SIGMA_GATE_GLOBAL", "planted.py: SIGMA_WATCH_INTERVAL"]


def _gate(tmp_path, extra):
    import os
    import subprocess
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SIGMA_", RETIRED_ENV))}
    env.update(extra, CLAUDE_PROJECT_DIR=str(tmp_path))        # no .sdlc/ here: scoped hook is silent
    proc = subprocess.run(["bash", str(ROOT / "hooks" / "sigma_gate.sh")], env=env,
                          input=json.dumps({"prompt": "implement the parser in parser.py"}),
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]


def test_the_gate_hook_reads_the_legacy_gate_variable(tmp_path):
    """Run the SHIPPED hook, as `tests/test_hook.py` does: the previous name's switch still turns the
    global gate on, an explicit `SIGMA_GATE_GLOBAL` wins over it, and neither set stays silent."""
    assert _gate(tmp_path, {}) == ""
    assert _gate(tmp_path, {RETIRED_ENV + "GATE_GLOBAL": "1"}) != ""
    assert _gate(tmp_path, {RETIRED_ENV + "GATE_GLOBAL": "1", "SIGMA_GATE_GLOBAL": "0"}) == ""


_MARKER_LITERAL = re.compile(r"""["'](?:<!-- )?sigma[:-][a-z][a-z0-9:-]*""")
#: Brand-prefixed literals that are names, not markers: temp-file/ruleset prefixes, a doc name, and
#: the git-dir directory #278's refused-push records live in (new with #278, so no legacy spelling).
_NOT_MARKERS = ("sigma-doctor", "sigma-doctor:", "sigma-init", "sigma-init:", "sigma-kg", "sigma-log", "sigma-loop",
                "sigma-model", "sigma-rebase", "sigma-scope", "sigma-scope-assign", "sigma-scope-plan",
                "sigma-setup", "sigma-setup:", "sigma-velocity",    # skill names in printed output (#523)
                "sigma-managed-enrolled-", "sigma-demo", "sigma-dossier", "sigma-flake-", "sigma-merge-queue-", "sigma-push-refused",
                "sigma-receipt-snapshot-",
                "sigma:spend-approved", "sigma:spend-approval-used",   # new with #722: no legacy spelling
                "sigma-resolution",   # new with #945: a commit-body trailer key, lowercase, no legacy spelling
                "sigma-claim:",   # new with #858: no legacy spelling
                "sigma-upkeep-")   # new with #1085: a tempfile prefix for the replay worktree parent, not a marker


def test_every_marker_literal_is_registered_with_the_helper(legacy):
    """A new marker must be added to `legacy.MARKERS` -- which is where a reviewer sees that its
    readers need the legacy spelling too (plan-review finding 5)."""
    found = set()
    for p in _SHIPPED:
        if p.suffix == ".py":
            for m in _MARKER_LITERAL.finditer(p.read_text(encoding="utf-8")):
                found.add(m.group()[1:])
    missing = sorted(f for f in found if f not in _NOT_MARKERS
                     and not any(f.startswith(k) or k.startswith(f) for k in legacy.MARKERS))
    assert found and not missing, "unregistered marker literals: %s" % missing
