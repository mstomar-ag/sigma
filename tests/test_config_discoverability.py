"""A feature nobody can find is a feature that doesn't ship.

`/sigma-init` scaffolds `.sdlc/config.json` from a template, and that file is where an adopter looks
to learn what this kit can do — before the README, before any skill. So a knob the CODE reads must
appear there. When it doesn't, the feature is real, tested, documented in its skill, and invisible:
the user has to already know it exists to go looking.

That is how `gates.decision_gate` shipped — enforced by a hook, reported by /sigma-doctor, described
in /sigma-decide, and absent from the one file every adopter opens.

This is the same bug family as `lane: auto` (scaffolded, nothing read it) and the plan file
(`hard_plan_gate` gated on a path the Plan phase was never told to write). The halves are built by
different changes at different times and nothing checks they meet."""
import pathlib, re

ROOT = pathlib.Path(__file__).resolve().parent.parent
TMPL = (ROOT / "skills" / "sigma-init" / "templates" / "config.json.tmpl").read_text(encoding="utf-8")

#: Every `.py` under skills/ + hooks/ that could read config. Widened deliberately: the point is to
#: catch a knob added anywhere, including in a script nobody thought of as config-reading.
_SOURCES = sorted(ROOT.glob("skills/*/scripts/*.py")) + sorted(ROOT.glob("hooks/*.py"))

#: `(cfg.get("gates") or {}).get("<name>")` — the codebase's one idiom for reading a gate.
_GATE_READ = re.compile(r'"gates"\)\s*(?:or\s*\{\})?\s*\)?\.get\(\s*"([a-z_]+)"')


def _gates_read_by_code():
    found = set()
    for path in _SOURCES:
        found.update(_GATE_READ.findall(path.read_text(encoding="utf-8")))
    return found


def test_the_idiom_still_matches_something():
    """If the codebase changes how it reads gates, this file silently passes forever. Fail loudly
    instead — an empty result means the guard stopped guarding, not that the repo got clean."""
    assert _gates_read_by_code(), "no gate reads matched; the detection idiom went stale"


def test_every_gate_the_code_reads_is_discoverable_in_the_scaffolded_config():
    """The check that would have caught decision_gate before it shipped invisible."""
    missing = sorted(g for g in _gates_read_by_code() if g not in TMPL)
    assert not missing, (
        "read by code but absent from the scaffolded config, so no adopter will ever find "
        f"them: {missing}. Add each to `gates` in config.json.tmpl — a default line if it takes "
        "one, or a `_<name>` comment explaining how it is switched on.")


def test_a_gate_with_no_enabled_flag_says_how_it_is_actually_turned_on():
    """decision_gate is the odd one out: authoring the registry (in an adopted repo) is the opt-in, so there is no
    `enabled: true` to set. Left unexplained next to two gates that DO take that flag, the natural
    reading is 'add enabled: true' — which does nothing, and the user concludes the gate is broken."""
    assert "_decision_gate" in TMPL
    section = TMPL.split("_decision_gate")[1][:1200]
    assert "decisions.json" in section                 # what to author
    assert "/sigma-decide" in section                   # the skill that authors it
    assert "AUTHORING" in section or "authoring" in section


def test_the_off_switch_is_documented_where_someone_would_look_for_it():
    """Turning a gate OFF is what you search for at 2am when it is blocking you. If that is only in
    the skill, the person being blocked is reading their config file instead."""
    section = TMPL.split("_decision_gate")[1][:1200]
    assert "enabled" in section and ("OFF" in section or "off" in section)


def test_plan_review_gate_ships_off_and_says_how_it_is_turned_on():
    """#258: `gates.plan_review` is read through `_gate_block(config, "plan_review")`, which the
    `_GATE_READ` idiom above cannot see -- so its discoverability is pinned by name. It ships OFF, and
    its comment names the verb that satisfies it, the key it needs, and what it does not cover."""
    import json
    gates = json.loads(TMPL)["gates"]
    assert gates["plan_review"] == {"enabled": False}
    for needed in ("record-plan-review", "work.enabled", "slices.json"):
        assert needed in gates["_plan_review"], needed


def test_template_is_valid_json_once_comments_are_stripped():
    """The `_name` comment convention means a stray quote breaks EVERY adopter's scaffold, and the
    added entry embeds escaped quotes — exactly where that goes wrong."""
    import json
    cfg = json.loads(TMPL)
    assert isinstance(cfg.get("gates"), dict)
    assert "decision_gate" in " ".join(cfg["gates"].keys())      # present as a key or a _comment


# --- #546: the documented board title must be the one the code actually generates ---------------
# `_find_project` matches a board title BYTE-EXACTLY, so the separator in the auto-generated title
# is load-bearing, not typography. The scaffolded config is the first place an adopter looks when a
# board fails to resolve — documenting it there with an ASCII hyphen while the code emits an em-dash
# sends exactly the person who is already confused looking for the wrong string.

def _generated_board_title():
    """The real thing, from the code that builds it — never a literal copied into this test, or the
    pin would drift the same way the prose did."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "sources", ROOT / "skills" / "sigma-loop" / "scripts" / "sources.py")
    sources = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sources)
    src = sources.GitHubSource.__new__(sources.GitHubSource)
    src.repo, src._project_cfg = "acme/widget", {}
    return src._proj_title()


def test_the_board_title_separator_pin_still_matches_something():
    """Guard the guard: if `_proj_title` stops producing a `<name> <sep> SDLC` shape, the two tests
    below would silently pass forever on a string they no longer describe."""
    title = _generated_board_title()
    assert title.endswith(" SDLC") and title.startswith("widget"), title


def test_the_scaffolded_config_documents_the_title_the_code_generates():
    separator = _generated_board_title()[len("widget"):-len("SDLC")]     # " — ", whatever it is
    assert f"<repo>{separator}SDLC" in TMPL, (
        "config.json.tmpl documents the default board title with a different separator than "
        f"sources._proj_title() generates ({separator!r}); _find_project matches titles "
        "byte-exactly, so an adopter reading their own config would search for a title that "
        "does not exist")


# --- #1805: a private block's project id was live and load-bearing, but had no scaffold entry ---
# #2578 DELETED #1805's guard, the test that the scaffolded config documented that id.
# Its subject is gone, not merely renamed: the core no longer scaffolds the private side's block at
# all: #2578 removed the private-side starters from the core, which has no key of the private
# side's left to document. A
# discoverability test for a block that is deliberately absent cannot be made to say anything true.
#
# THE RESIDUE, NAMED RATHER THAN ASSUMED AWAY: a fresh `/sigma-init` now scaffolds no place to turn
# emit on, and no place to declare that project id. A home for both exists in the private
# package's own README, but `doctor.py` still reported those rows from a block nothing scaffolds --
# S1-G8 (#2575) reconciles that, and this test is not the thing to keep alive in the meantime.
#
# NOTHING NEW IS ADDED IN ITS PLACE, AND THAT IS A MEASURED CONCLUSION, NOT AN OMISSION. The plan
# asked for "assert the template still parses as JSON after the block and its trailing comma go --
# add/extend a test if none covers it". One already does:
# `test_template_is_valid_json_once_comments_are_stripped` above (line 67) calls `json.loads(TMPL)`
# directly. It was verified to be the guard that catches this exact breakage by re-inserting a
# dangling comma at the deletion point and watching it fail -- a second copy of the same assertion
# would be duplicate coverage, not a control.


def test_no_shipped_prose_renders_the_board_title_with_an_ascii_hyphen():
    """The same one-character drift had crept into FOUR separate places describing one string.
    Pinned across the surfaces that document it so a future edit cannot reintroduce it in a corner
    this test does not watch. Enumerates EVERY occurrence, not the first per file — the fourth site
    sat one line below the third, and a first-match-only scan called that file clean."""
    surfaces = ([ROOT / "skills" / "sigma-init" / "templates" / "config.json.tmpl",
                 ROOT / "README.md"]
                + sorted(ROOT.glob("skills/*/scripts/*.py"))
                + sorted(ROOT.glob("tests/*.py")))
    wrong = re.compile(r"(?:repo|name|project|widget|\}|>)['\"`]?\s-\sSDLC")
    offenders = []
    for path in surfaces:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        offenders += [f"{path.relative_to(ROOT)}:{text[:m.start()].count(chr(10)) + 1}"
                      for m in wrong.finditer(text)]
    assert not offenders, (
        f"board title written with an ASCII hyphen instead of the generated em-dash: {offenders}")


def test_template_documents_every_key():
    """Every key skills/sigma-loop/scripts/decision_rubric_cfg.py reads appears in the template."""
    import json
    block = json.loads(TMPL)["decision_rubric"]
    assert block["records"]["enabled"] is False
    assert block["records"]["repo_visibility"] == "private"
    assert "_decision_rubric" in json.loads(TMPL)
    src = (ROOT / "skills" / "sigma-loop" / "scripts" / "decision_rubric_cfg.py").read_text(encoding="utf-8")
    for key in ("decision_rubric", "repo_visibility", "enabled"):
        assert key in src and key in TMPL


def test_template_documents_the_store_keys():
    import json
    rec = json.loads(TMPL)["decision_rubric"]["records"]
    note = json.loads(TMPL)["_decision_rubric"]
    for key in ("store_dir", "lock_timeout_s", "archive_after_days", "max_files_warn"):
        assert key in rec and "`%s`" % key in note
