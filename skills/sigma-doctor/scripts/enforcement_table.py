#!/usr/bin/env python3
"""Render `docs/enforcement.md`: what enforces each control, on which hosts, and its shipped default.

    python3 skills/sigma-doctor/scripts/enforcement_table.py > docs/enforcement.md

WHAT IT READS. (a) The `ENFORCEMENT_GATES` / `ENFORCEMENT_EXEMPT` tuples kept beside the gate code
in `skills/sigma-loop/scripts/{work,state,loop}.py`; (b) `hooks/hooks.json`, whose every command is
matched to a fact in `HOOK_FACTS` below (hooks.json says neither whether a hook blocks nor what
turns it on); (c) `skills/sigma-init/templates/config.json.tmpl`, for the value every enabling key
ships with; (d) `EXTERNAL_CONTROLS` below, for the controls no Python in the kit implements: skill
prose the agent is asked to follow, and the git host's own branch protection.

WHY AST, NOT IMPORT. `doctor.py`'s standing rule is no cross-skill import, and `work.py` imports its
siblings at module load. The registries are pure literals so `ast.literal_eval` on the module-level
`Assign` node is the whole reader -- nothing in those modules runs.

THE LIMIT. `problems()` fails on a module-level function whose name ends `_refusal`/`_gate`/`_hold`/
`_guard`, is `gate`, or contains `blocked_by` and is neither registered nor exempt. This proves only
what it enumerates: a gate whose function name does not follow the convention is invisible to it.
Registering such a gate is still required; this check just cannot notice when you forget.

A structural problem is a REFUSAL: each goes to stderr as `enforcement_table.py: REFUSED: ...`,
nothing reaches stdout, exit 2 -- so `> docs/enforcement.md` never half-writes the doc.
"""
import ast
import json
import pathlib
import re
import sys

MODULES = ("work.py", "state.py", "loop.py")
SCRIPTS = pathlib.Path("skills") / "sigma-loop" / "scripts"
HOOKS_JSON = pathlib.Path("hooks") / "hooks.json"
TEMPLATE = pathlib.Path("skills") / "sigma-init" / "templates" / "config.json.tmpl"
REGENERATE = "python3 skills/sigma-doctor/scripts/enforcement_table.py > docs/enforcement.md"

#: The planted-gate net (see the module docstring for what it cannot see).
GATE_NAME = re.compile(r"(_refusal|_gate|_hold|_guard)$|^gate$|blocked_by")

KINDS = ("python-gate", "git-host", "claude-hook", "advice")
KIND_LABEL = {"python-gate": "Python gate", "git-host": "git host",
              "claude-hook": "Claude Code hook", "advice": "advice"}
HOSTS = {"all": "Claude Code, Cursor, Codex", "claude-code": "Claude Code only"}
REQUIRED = ("control", "function", "kind", "hosts", "enabled_by", "mechanism")
OPTIONAL = ("settings", "condition", "readme")
TEXT_FIELDS = ("control", "function", "mechanism", "condition", "readme")

#: Every `advice` row must say who is asked to do what -- the one shape that cannot be misread as a
#: gate. Also the check the README rewording is held to (`readme_problems`).
ASKS = re.compile(r"\bask(s)? the (agent|reviewer) to\b")
#: The words an `advice` row's README line may not use about itself.
GATE_WORDS = re.compile(r"(?i)\b(gate|gates|guard\w*|refuses?|refused|blocks?|blocked)\b")
D5_WORDING = "cannot prove the maker did not influence the reviewer"
REVIEWER_ROW = "Independent review"

#: A value read as "off" when computing a row's default cell; anything else is on.
_FALSY = (None, False, 0, "", "off", [], {})

#: One fact per hooks.json command, keyed by the hook id `hook_ids()` derives from the command.
#: `control: None` means the hook is not a control (time tracking, capture) and gets no Controls row.
HOOK_FACTS = (
    {"id": "sigma_gate.sh", "control": "SDLC policy reminder on every prompt", "blocks": False,
     "kind": "advice", "hosts": "claude-code", "enabled_by": (), "settings": (),
     "mechanism": "injects the standing policy that asks the agent to follow the 7-phase spine on "
                  "every prompt; enforces nothing",
     "condition": "on wherever `.sdlc/` exists (or `SIGMA_GATE_GLOBAL=1`). Not this hook: Codex "
                  "and Cursor get the same standing rule only as an opt-in rule file, "
                  "`/sigma-init --codex` (managed block in `AGENTS.md`) or `/sigma-init --cursor` "
                  "(always-applied `.cursor/rules/sdlc.mdc`)",
     "readme": "Repo-scoped SDLC reminder"},
    {"id": "time_track.py prompt", "control": None, "blocks": False, "note": "time tracking"},
    {"id": "research_capture.py", "control": None, "blocks": False, "note": "knowledge capture",
     "enabled_by": ("knowledge_graph.enabled",)},
    {"id": "issue_field_gate.py", "control": "New issue needs a priority label", "blocks": True,
     "kind": "claude-hook", "hosts": "claude-code", "enabled_by": (), "settings": (),
     "mechanism": "denies a `gh issue create` (PreToolUse on Bash) that carries no `priority:P<n>` "
                  "label, so an issue is born ranked",
     "condition": "always on (no key) inside an adopted repo -- `.sdlc/config.json` at or above "
                  "the project dir (#2737); inert elsewhere; fails open"},
    {"id": "plan_gate.sh", "control": "Hard plan gate at edit", "blocks": True,
     "kind": "claude-hook", "hosts": "claude-code",
     "enabled_by": ("gates.hard_plan_gate.enabled",), "settings": (),
     "mechanism": "denies the edit itself (PreToolUse), in an adopted repo (#2737), when no plan "
                  "under `.sdlc/plans/` is fresher than `plan_freshness_hours`; honours `touch "
                  ".sdlc/.allow-direct-edits` only while the key is not org-locked ON -- under "
                  "`.sdlc/managed-settings.json` the sentinel is ignored (#2138); an accelerator "
                  "for the PR-push gate above, which holds on every host",
     "readme": "Hard plan-gate (opt-in)"},
    {"id": "decision_gate.py", "control": "Decision registry invariants", "blocks": True,
     "kind": "claude-hook", "hosts": "claude-code", "enabled_by": (), "settings": (),
     "mechanism": "denies an edit (PreToolUse) that assigns a registered invariant a violating "
                  "value; fails open on its own errors",
     "condition": "on once `/sigma-decide` writes `.sdlc/decisions.json` in an adopted repo -- "
                  "`.sdlc/config.json` present (#2737), so a repo holding only `decisions.json` is "
                  "not gated; `gates.decision_gate.enabled: false` turns it off. On every host, "
                  "`decision_gate.py check .` is a manual backstop -- nothing in `work.py` or "
                  "`loop.py` runs it, so there it is not a gate",
     "readme": "Decisions that actually hold"},
    {"id": "time_track.py skill", "control": None, "blocks": False, "note": "time tracking"},
    {"id": "completion_gate.sh", "control": "Stop gate", "blocks": True,
     "kind": "claude-hook", "hosts": "claude-code",
     "enabled_by": ("gates.stop_gate.enabled",), "settings": (),
     "mechanism": "refuses to let the session stop (Stop), in an adopted repo (#2737), with source "
                  "changed in the working tree and no fresh plan under `.sdlc/plans/`; honours "
                  "`touch .sdlc/.allow-direct-edits`",
     "readme": "Stop gate (opt-in)"},
    {"id": "time_track.py stop", "control": None, "blocks": False, "note": "time tracking"},
    {"id": "session_start.sh", "control": "SessionStart policy brief", "blocks": False,
     "kind": "advice", "hosts": "claude-code", "enabled_by": ("session_start.enabled",),
     "settings": (),
     "mechanism": "injects the SDLC policy brief and a doctor-lite install self-check at session "
                  "start, which asks the agent to follow the conventions; warns, never blocks",
     "condition": "the brief needs `session_start.enabled`; separately, the ledger-watcher "
                  "staleness warning fires whenever `ledger.enabled` is `true`, with no key of "
                  "its own",
     "readme": "SessionStart brief (opt-in)"},
)

#: Controls with no implementing function in the three modules: skill prose (`advice`) and the git
#: host's own settings (`git-host`). A control named here AND in a module registry is a problem --
#: the day one of these becomes a Python gate, its advice row must go.
EXTERNAL_CONTROLS = (
    {"control": "Strategy alignment (FIX-FIRST)", "kind": "advice", "hosts": "all",
     "enabled_by": (), "settings": (),
     "mechanism": "`sigma-plan-review` asks the reviewer to send back a plan that contradicts the "
                  "north-star; no code checks it",
     "readme": "Strategy-alignment check"},
    {"control": "Independent review", "kind": "advice", "hosts": "all",
     "enabled_by": ("review.independent",), "settings": (),
     "mechanism": "The skills ask the agent to dispatch an independent reviewer with an "
                  "author-blind brief; the core cannot prove the maker did not influence the "
                  "reviewer. It does not observe who called the host's task tool, a maker can "
                  "record its own approving verdict, the route comes from `review.host`, "
                  "`review.command` and session environment variables alone, and the merge check "
                  "reads the posted `sigma:approve`/`sigma:block` comment from an OWNER, MEMBER or "
                  "COLLABORATOR commenter, not the evidence, and the default `require_review: changes` does not require an "
                  "approval at all. What it does prove, for a PR review: the brief digest, the PR and "
                  "the head are bound to one generation; evidence is refused if the worktree head "
                  "or diff moved; a generation's result and evidence are written once per "
                  "generation, so a re-review needs a new one; a post is refused unless the "
                  "evidence is the goal's current generation and the PR head is unchanged.",
     "readme": "Independent review (advisory)"},
    {"control": "Upkeep conflict reviewer route", "kind": "advice", "hosts": "claude-code",
     "enabled_by": ("upkeep.enabled", "upkeep.conflicts.resolve"), "settings": (),
     "mechanism": "`feature_upkeep_review.py` asks the reviewer to judge a resolved unit conflict in a second headless "
                  "session given a brief without the resolver's transcript; the route is `verified: false`, was never "
                  "run against the real command line, and cannot prove the resolver did not influence the reviewer. "
                  "It can only add a refusal; a block, a bad reply, a timeout or a moved tree all stop the push",
     "condition": "needs a validated model id; the shipped catalog holds only a placeholder, so the route stays closed"},
    {"control": "Unit landing is user-requested", "kind": "python-gate", "hosts": "all",
     "enabled_by": ("upkeep.enabled",), "settings": (),
     "mechanism": "`feature_land.py` `land` and `feature_land_merge.py`: land a unit only on an explicit request, behind the "
                  "upkeep project door, with the merge pinned to the head that was rehearsed. Landing is deliberately "
                  "not in `gates.irreversible_actions`: it has its own gate and an explicit yes in `sigma-rebase`",
     "condition": "closed refuses before any read; the chat door and a hosted ruleset were not covered by the hosted run"},
    {"control": "Irreversible actions park", "kind": "advice", "hosts": "all",
     "enabled_by": (), "settings": ("gates.irreversible_actions", "gates.on_block"),
     "mechanism": "the loop's instructions ask the agent to park rather than run a "
                  "deploy/delete/overwrite/spend/migrate",
     "condition": "no code reads these keys; the code's own lease force-push of a unit branch and its prune of "
                  "backup refs (once upkeep is enabled) are outside it, see docs/branching-model.md section 13b"},
    {"control": "Branch protection (required checks / reviews)", "kind": "git-host", "hosts": "all",
     "enabled_by": (), "settings": (),
     "mechanism": "GitHub refuses the merge; `work.auto_merge: \"protected\"` merges only where the "
                  "base requires them",
     "condition": "your repository's own settings; Sigma never configures them"},
    {"control": "Quality-drift eval corpus", "kind": "git-host", "hosts": "all",
     "enabled_by": (), "settings": (),
     "mechanism": "`evals/run.py` fails the CI job; blocks a merge only where branch protection "
                  "requires the check",
     "condition": "Sigma's own public CI on push/PR; nothing installs it into your repository",
     "readme": "Quality-drift gate"},
)

#: Gates that exist but are deliberately not registered (S5): pick-time gates outside the three
#: modules, and one config refusal that guards no work.
NOT_YET = (
    "Pick-time gates in `feature_owner.py`, `feature_propagate.py`, `promote.py` and "
    "`feature_labels.py` (`gate_at_pick` and the promote-time label checks) are not registered "
    "yet: they back the README's \"Human approval gate, in one transition\" and \"Findings become "
    "work\" rows, and a follow-up registers them. `work.py` also refuses to load a config with "
    "`ledger.receipt_sharing: true` (not supported yet); that is a config refusal, not a control "
    "on the work, and is deferred with them.",
    "Reported, not enforced: live status (`action_log.enabled`, off) and agent liveness "
    "(`agent_watch.enabled`, off) show state and refuse nothing, so they are monitors, not "
    "controls. There is no rollback control at all: `work.py` aborts a failed rebase, and nothing "
    "reverts a landed goal. Acceptance criteria are the verify rows' command source, above. Cursor "
    "/ Codex parity is the Hosts column, not a row.",
)


# ------------------------------------------------------------------------------- readers


def _module_path(root, module):
    return pathlib.Path(root) / SCRIPTS / module


def _module_tree(root, module):
    return ast.parse(_module_path(root, module).read_text(encoding="utf-8"))


def _module_level_literal(tree, name):
    """`ast.literal_eval` of the module-level `NAME = <literal>`, or `None` when absent."""
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    return None


def load_registries(root):
    """{module: (ENFORCEMENT_GATES, ENFORCEMENT_EXEMPT)} for the three modules. Raises on a
    module that does not parse or has no literal registry -- `problems()` reports that."""
    out = {}
    for module in MODULES:
        tree = _module_tree(root, module)
        gates = _module_level_literal(tree, "ENFORCEMENT_GATES")
        exempt = _module_level_literal(tree, "ENFORCEMENT_EXEMPT")
        if gates is None or exempt is None:
            raise ValueError(f"{module}: ENFORCEMENT_GATES / ENFORCEMENT_EXEMPT missing at module level")
        out[module] = (tuple(gates), tuple(exempt))
    return out


def module_functions(root, module):
    return [n.name for n in _module_tree(root, module).body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def gate_candidates(root):
    """[(module, function)] for every module-level def whose name matches `GATE_NAME`."""
    return [(module, name) for module in MODULES
            for name in module_functions(root, module) if GATE_NAME.search(name)]


def hook_commands(root):
    """[(event, matcher, hook id)] in hooks.json order. The id is the script name after the last
    `hooks/` in the command plus whatever unquoted argument follows it (`time_track.py prompt`)."""
    data = json.loads((pathlib.Path(root) / HOOKS_JSON).read_text(encoding="utf-8"))
    out = []
    for event, groups in data.get("hooks", {}).items():
        for group in groups:
            for hook in group.get("hooks", []):
                command = hook.get("command", "")
                scripts = re.findall(r'hooks/([\w.]+)"', command)
                if not scripts:
                    raise ValueError(f"hooks.json: no hooks/<script> in command {command!r}")
                tail = command.rsplit('"', 1)[1].strip()
                out.append((event, group.get("matcher") or "—",
                            scripts[-1] + (" " + tail if tail else "")))
    return out


def hook_ids(root):
    return [hid for _event, _matcher, hid in hook_commands(root)]


def load_template(root):
    """The scaffolded config as JSON; `_`-prefixed keys are comments and stay in place."""
    return json.loads((pathlib.Path(root) / TEMPLATE).read_text(encoding="utf-8"))


def template_value(template, dotted):
    """The value at a dotted path, or the sentinel string `absent`."""
    node = template
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return "absent"
        node = node[part]
    return node


# ------------------------------------------------------------------------------- rows


def _validate(entry, where, require_function):
    """Structural problems of one registry / fact / external entry."""
    found = []
    required = REQUIRED if require_function else tuple(k for k in REQUIRED if k != "function")
    allowed = set(required) | set(OPTIONAL)
    for key in required:
        if key not in entry:
            found.append(f"{where}: missing key {key!r}")
    for key in entry:
        if key not in allowed:
            found.append(f"{where}: unknown key {key!r}")
    if entry.get("kind") not in KINDS:
        found.append(f"{where}: unknown kind {entry.get('kind')!r}")
    if entry.get("hosts") not in HOSTS:
        found.append(f"{where}: unknown hosts {entry.get('hosts')!r}")
    for key in ("enabled_by", "settings"):
        value = entry.get(key, ())
        if not isinstance(value, tuple) or not all(isinstance(v, str) for v in value):
            found.append(f"{where}: {key} must be a tuple of dotted keys")
    for key in TEXT_FIELDS:
        value = entry.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            found.append(f"{where}: {key} must be non-empty text")
        elif "|" in value or "\n" in value:
            found.append(f"{where}: {key} contains `|` or a newline")
    return found


def _row(entry, source):
    return {"control": entry["control"], "kind": entry["kind"], "hosts": entry["hosts"],
            "enabled_by": tuple(entry.get("enabled_by", ())),
            "settings": tuple(entry.get("settings", ())),
            "mechanism": entry["mechanism"], "condition": entry.get("condition", ""),
            "readme": entry.get("readme", ""), "source": source}


def rows(root):
    """Every Controls row: module registries, hook facts that name a control, external controls."""
    out = []
    for module, (gates, _exempt) in load_registries(root).items():
        for entry in gates:
            out.append(_row(entry, f"`{module}` `{entry['function']}`"))
    for fact in HOOK_FACTS:
        if fact.get("control"):
            out.append(_row(fact, f"`hooks/{fact['id'].split()[0]}`"))
    for entry in EXTERNAL_CONTROLS:
        out.append(_row(entry, ""))
    return out


# ------------------------------------------------------------------------------- checks


def problems(root):
    """Every structural problem in the tree, as one line each; empty means the doc can render."""
    found = []
    try:
        registries = load_registries(root)
    except (OSError, SyntaxError, ValueError) as exc:
        return [f"registries: {exc}"]
    for module, (gates, exempt) in registries.items():
        for entry in gates:
            if not isinstance(entry, dict):
                found.append(f"{module}: ENFORCEMENT_GATES entry is not a dict: {entry!r}")
                continue
            found.extend(_validate(entry, f"{module} {entry.get('control', '?')!r}", True))
        defs = set(module_functions(root, module))
        registered = {e.get("function") for e in gates if isinstance(e, dict)}
        for name in sorted(registered):
            if name not in defs:
                found.append(f"{module}: registered function {name!r} is not a module-level def")
        exempt_names = set()
        for item in exempt:
            if not (isinstance(item, tuple) and len(item) == 2 and all(isinstance(x, str) for x in item)):
                found.append(f"{module}: ENFORCEMENT_EXEMPT item is not (name, reason): {item!r}")
                continue
            name, reason = item
            exempt_names.add(name)
            if name not in defs:
                found.append(f"{module}: exempt function {name!r} is not a module-level def")
            if not reason.strip():
                found.append(f"{module}: exempt {name!r} has no reason")
        for name in module_functions(root, module):
            if GATE_NAME.search(name) and name not in registered and name not in exempt_names:
                found.append(f"{module}: {name!r} looks like a gate but is neither in "
                             "ENFORCEMENT_GATES nor ENFORCEMENT_EXEMPT")
    for fact in HOOK_FACTS:
        if fact.get("control"):
            entry = {k: v for k, v in fact.items() if k not in ("id", "blocks")}
            found.extend(_validate(entry, f"HOOK_FACTS {fact['id']!r}", False))
            if fact.get("hosts") != "claude-code":
                found.append(f"HOOK_FACTS {fact['id']!r}: every hooks.json hook is Claude Code only, "
                             f"hosts must be 'claude-code'")
    for entry in EXTERNAL_CONTROLS:
        found.extend(_validate(entry, f"EXTERNAL_CONTROLS {entry.get('control', '?')!r}", False))
    try:
        ids = hook_ids(root)
    except (OSError, ValueError, KeyError) as exc:
        found.append(f"hooks.json: {exc}")
        ids = []
    facts = [f["id"] for f in HOOK_FACTS]
    for hid in ids:
        if hid not in facts:
            found.append(f"hooks.json {hid!r} has no HOOK_FACTS entry")
    for hid in facts:
        if hid not in ids:
            found.append(f"HOOK_FACTS {hid!r} names no hooks.json command")
    if found:
        return found
    all_rows = rows(root)
    keys = {k for r in all_rows for k in r["enabled_by"] + r["settings"]}
    keys |= {k for f in HOOK_FACTS for k in f.get("enabled_by", ())}
    template = load_template(root)
    for gate_key in template.get("gates", {}):
        if gate_key.startswith("_"):
            continue
        prefix = f"gates.{gate_key}"
        if not any(k == prefix or k.startswith(prefix + ".") for k in keys):
            found.append(f"template key {prefix!r} is covered by no row")
    seen = {}
    for r in all_rows:
        if r["control"] in seen:
            found.append(f"control {r['control']!r} appears twice ({seen[r['control']]} and "
                         f"{r['source'] or 'EXTERNAL_CONTROLS'})")
        seen[r["control"]] = r["source"] or "EXTERNAL_CONTROLS"
        if r["kind"] == "advice" and not ASKS.search(r["mechanism"]):
            found.append(f"advice row {r['control']!r} does not say who is asked to do what")
        if r["kind"] == "claude-hook" and r["hosts"] != "claude-code":
            found.append(f"claude-hook row {r['control']!r} must be hosts 'claude-code'")
    reviewer = [r for r in all_rows if r["control"] == REVIEWER_ROW]
    if len(reviewer) != 1 or reviewer[0]["kind"] != "advice" or D5_WORDING not in reviewer[0]["mechanism"]:
        found.append(f"the {REVIEWER_ROW!r} row must be advice and carry {D5_WORDING!r}")
    return found


def readme_problems(readme_text, all_rows):
    """Problems with the README's feature table against `all_rows` (a pure function over text; not
    part of `main`). Every `readme` label names exactly one `| **<label>** |` row, that row links
    to `docs/enforcement.md`, and a label whose rows are all `advice` does not call itself a gate."""
    found = []
    lines = readme_text.splitlines()
    by_label = {}
    for row in all_rows:
        if row["readme"]:
            by_label.setdefault(row["readme"], []).append(row)
    for label in sorted(by_label):
        matches = [l for l in lines if l.startswith(f"| **{label}** |")]
        if len(matches) != 1:
            found.append(f"README: {label!r} has {len(matches)} feature-table rows, "
                         f"expected exactly one (missing or ambiguous)")
            continue
        line = matches[0]
        if "docs/enforcement.md" not in line:
            found.append(f"README: the {label!r} row does not link to docs/enforcement.md")
        if all(r["kind"] == "advice" for r in by_label[label]):
            hit = GATE_WORDS.search(line)
            if hit:
                found.append(f"README: the {label!r} row is advice in the table but says "
                             f"{hit.group(0)!r}")
    return found


# ------------------------------------------------------------------------------- render


def _is_on(value):
    return not any(value is f or value == f for f in _FALSY) and value != "absent"


def default_cell(row, template):
    """`on`/`off` plus the literal template values for the row's keys, then its settings in
    parentheses, then `; <condition>`. A row with no enabling key shows its condition, or
    `always on (no key)` when it has none."""
    parts = []
    if row["enabled_by"]:
        values = {k: template_value(template, k) for k in row["enabled_by"]}
        state = "on" if all(_is_on(v) for v in values.values()) else "off"
        parts.append(state + " — " + ", ".join(_key_value(k, v) for k, v in values.items()))
    if row["settings"]:
        shown = ", ".join(_key_value(k, template_value(template, k)) for k in row["settings"])
        parts.append(f"({shown})" if parts else shown)
    cell = " ".join(parts)
    if row["condition"]:
        cell = f"{cell}; {row['condition']}" if cell else row["condition"]
    return cell or "always on (no key)"


def _key_value(key, value):
    shown = "absent" if value == "absent" else json.dumps(value)
    return f"`{key}: {shown}`"


def _sorted_rows(all_rows):
    return sorted(all_rows, key=lambda r: (KINDS.index(r["kind"]), r["control"].lower()))


def render(root):
    """The whole of `docs/enforcement.md`, LF-terminated, deterministic for a given tree."""
    template = load_template(root)
    lines = [
        "<!-- GENERATED by skills/sigma-doctor/scripts/enforcement_table.py. Do not edit by hand. -->",
        "# What enforces each control",
        "",
        "Generated from `hooks/hooks.json`, `skills/sigma-init/templates/config.json.tmpl` and the",
        "`ENFORCEMENT_GATES` registries in `skills/sigma-loop/scripts/{work,state,loop}.py`. Regenerate:",
        "",
        f"    {REGENERATE}",
        "",
        "**Kinds.** *Python gate*: plain Python in the kit refuses, parks or holds; identical on every",
        "host. *git host*: your repository's own branch protection or CI refuses the merge. *Claude Code",
        "hook*: a `hooks/hooks.json` hook denies the action; Cursor and Codex have no hooks, so it holds",
        "nowhere else. *advice*: the skills ask the agent to do it and no code checks that it did.",
        "",
        "**Default (scaffolded)** is the value `/sigma-init` writes from `config.json.tmpl`, read from",
        "that template at generation time. `work.enabled` ships `true` directly as of #2741 (1.0.0)",
        "-- so on a bare `/sigma-init` every `work.py` row below reads `on` unless its own second key",
        "says otherwise. `verify.enforce` ships `false` (#228): the template cannot know a command, and",
        "enforce on with an empty one refuses every `done`. `/sigma-init` turns it on once a verify",
        "command is confirmed (`verify_detect.py confirm`). `/sigma-setup` still writes",
        "`ledger.enabled: true` where it is `null` (unchanged, still off by default).",
        "",
        "**Hosts** names where the mechanism holds *by construction* -- a Python gate runs wherever",
        "the kit runs; a hook exists only on Claude Code -- not where it has been run end to end. The",
        "Cursor adapter is not verified in a live session; Codex has partial live validation.",
        "",
        "## Controls",
        "",
        "| Control | Enforced by | Mechanism | Default (scaffolded) | Hosts |",
        "|---|---|---|---|---|",
    ]
    for row in _sorted_rows(rows(root)):
        mechanism = f"{row['source']}: {row['mechanism']}" if row["source"] else row["mechanism"]
        lines.append("| %s | %s | %s | %s | %s |" % (
            row["control"], KIND_LABEL[row["kind"]], mechanism, default_cell(row, template),
            HOSTS[row["hosts"]]))
    lines += ["", "## Every hook in hooks/hooks.json", "",
              "| Event | Matcher | Hook | Blocks? | Control |", "|---|---|---|---|---|"]
    facts = {f["id"]: f for f in HOOK_FACTS}
    for event, matcher, hid in hook_commands(root):
        fact = facts[hid]
        control = fact["control"] or f"— ({fact['note']})"
        lines.append("| %s | %s | `%s` | %s | %s |" % (
            event, matcher.replace("|", "\\|"), hid, "yes" if fact["blocks"] else "no", control))
    lines += ["", "## Not yet in this table", ""]
    for paragraph in NOT_YET:
        lines += [paragraph, ""]
    return "\n".join(lines).rstrip("\n") + "\n"


USAGE = (
    "usage: enforcement_table.py [-h | --help]\n"
    "  Takes no other arguments; an unknown one prints this and exits 2 without rendering.\n"
    "  Renders docs/enforcement.md to stdout from the gate registries, hooks.json and the config template.\n"
    "  Regenerate the committed doc with:\n"
    "    python3 skills/sigma-doctor/scripts/enforcement_table.py > docs/enforcement.md"
)


def main(argv=None, root=None):
    argv = list(argv or ())
    if argv in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    if argv:
        # Unknown arguments are a REFUSAL, not a shrug: `--check > docs/enforcement.md` must not
        # quietly overwrite the doc. Usage goes to stderr so stdout stays empty.
        print(f"enforcement_table.py: unknown argument(s) {' '.join(argv)!r}\n{USAGE}", file=sys.stderr)
        return 2
    root = pathlib.Path(root) if root is not None else pathlib.Path(__file__).resolve().parents[3]
    found = problems(root)
    if found:
        for problem in found:
            print(f"enforcement_table.py: REFUSED: {problem}", file=sys.stderr)
        return 2
    sys.stdout.buffer.write(render(root).encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
