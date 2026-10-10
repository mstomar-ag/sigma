# SPDX-License-Identifier: MIT
"""The canonical Sigma board (#234): its fields and its six views, as DATA, built at call time from
the kit's own vocabulary. This is the one source of truth `board_layout.py` applies and verifies,
and the one `docs/board.md` describes. Nothing here talks to GitHub.

Nothing is copied from elsewhere. The Status options are the configured columns
(`sources.GitHubSource.col`), Priority's are `discovery.PRIORITIES` (`P0`..`P4`), Phase's are
`phase_report.PHASE_TOKENS` with `PHASE_BOARD_COLORS`, and the "Needs a human" labels are the
source's own `parked_label` / `goal_blocked_label` (the legacy confirmation label is NOT in the
filter any more -- nothing writes it; bootstrap still creates it for old boards). Change one of those and the
spec follows.

WHAT THE API CAN SET (read-only schema introspection, 2026-09-29, the evidence recorded on #234): a view's
name, layout and visible fields (`createProjectV2View`), and its filter (`updateProjectV2View`
only). Group-by, sort, the board's column field, roadmap date fields and markers, and the view
order have NO input anywhere; they are in each view as `manual` properties, which `board_layout.py`
prints as exact UI steps and `verify` reads back.
"""
import importlib.util
import pathlib

_HERE = pathlib.Path(__file__).resolve().parent
_LOOP = _HERE.parent.parent / "sigma-loop" / "scripts"

#: GitHub's built-in fields the views show. They exist on every Projects v2 board (read-only on
#: board #17, 2026-09-29) and are never created here.
BUILTINS = ("Title", "Assignees", "Labels", "Linked pull requests", "Sub-issues progress",
            "Milestone", "Updated")
AREA = "Area"
MODEL_TIER = "Model tier"

#: View names, fixed: the runbook, the verify report and a second person's reproduction all say
#: these exact words.
BOARD, PRIORITY_TABLE, IN_FLIGHT, NEEDS_HUMAN, ROADMAP, EPICS = (
    "Board · by Status", "Priority table", "In flight", "Needs a human", "v1.0 roadmap", "Epics")

#: The label an epic carries (the issue's view 6).
EPIC_LABEL = "epic"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MODS = {}


def phase_vocabulary():
    """[(token, colour)] -- exactly the list `phase_report.py start` hands the loop's Phase mirror,
    so the field this creates and the one the loop creates cannot differ."""
    if "pr" not in _MODS:
        _MODS["pr"] = _load(_LOOP / "phase_report.py", "board_spec_phase_report")
    pr = _MODS["pr"]
    return [(token, pr.PHASE_BOARD_COLORS.get(kind, "GRAY")) for kind, token in pr.PHASE_TOKENS]


def value(text):
    """One filter value: quoted when it carries a space, comma or quote (GitHub's filter syntax
    splits on those)."""
    text = str(text)
    if any(ch in text for ch in ' ,"'):
        return '"%s"' % text.replace('"', '\\"')
    return text


def qualifier(field):
    """A field's filter qualifier: its name, lower-cased (`Status` -> `status:`)."""
    return value(str(field).lower()) + ":"


def build(src, priorities):
    """-> {"fields": [...], "views": [...]} for a `sources.GitHubSource` `src` (its columns, field
    names and labels) and the priority list (`discovery.PRIORITIES`).

    A field is {"name", "type" (SINGLE_SELECT | TEXT), "options": [(name, colour)] | None,
    "owner", "create_appends"}; `owner` "create" means `board_setup.py create` provisions it (Status), "layout" means
    `board_layout.py fields` does; `create_appends` says `board_setup.py create --number N --yes`
    can append its missing options id-preservingly (Status, Priority). A field turned off in config (`priority_field` / `phase_field`
    false) is left out, and so is every column, filter, group and sort that names it."""
    proj = src._project_cfg
    status = proj.get("status_field") or "Status"
    prio = src.priority_field if isinstance(src.priority_field, str) and src.priority_field else None
    phase = src.phase_field if isinstance(src.phase_field, str) and src.phase_field.strip() else None
    col = src.col
    fields = [{"name": status, "type": "SINGLE_SELECT", "owner": "create", "create_appends": True,
               "options": [(col[k], None) for k in ("backlog", "ready", "in_progress", "qc", "done",
                                                    "blocked", "parked")]}]
    if prio:
        fields.append({"name": prio, "type": "SINGLE_SELECT", "owner": "layout", "create_appends": True,
                       "options": [(p, "GRAY") for p in priorities]})
    if phase:
        fields.append({"name": phase, "type": "SINGLE_SELECT", "owner": "layout",
                       "options": phase_vocabulary()})
    fields += [{"name": AREA, "type": "TEXT", "owner": "layout", "options": None},
               {"name": MODEL_TIER, "type": "TEXT", "owner": "layout", "options": None}]

    def cols(*names):
        return [n for n in names if n]

    def one_of(field, values):
        return qualifier(field) + ",".join(value(v) for v in values)

    labels = (src.parked_label, src.goal_blocked_label)
    views = [
        {"name": BOARD, "layout": "BOARD_LAYOUT", "filter": "",
         "fields": cols("Title", prio, phase, "Assignees", "Linked pull requests"),
         "column_by": status, "group_by": None, "sort": [(prio, "ASC")] if prio else [],
         "default": True,
         "why": "The default view: one card per goal in its Status lane, so the loop's state reads "
                "at a glance; Priority and Phase on the card answer which goal and which phase."},
        {"name": PRIORITY_TABLE, "layout": "TABLE_LAYOUT", "filter": "is:open",
         "fields": cols("Title", status, phase, AREA, "Assignees", "Linked pull requests",
                        "Updated"),
         "group_by": prio, "sort": [],
         "why": "What should we work on: every open item grouped P0 first, the order the loop's "
                "queue ranks by."},
        {"name": IN_FLIGHT, "layout": "TABLE_LAYOUT",
         "filter": "is:open " + one_of(status, (col["in_progress"], col["qc"], col["blocked"])),
         "fields": cols("Title", phase, "Linked pull requests", "Assignees", "Updated"),
         "group_by": None, "sort": [("Updated", "DESC")],
         "why": "The operator's live view (the board's Block A): what is being worked, reviewed or "
                "stuck, most recently touched first."},
        {"name": NEEDS_HUMAN, "layout": "TABLE_LAYOUT",
         "filter": "is:open " + one_of("label", labels),
         "fields": cols("Title", prio, status, "Labels", "Updated"),
         "group_by": None, "sort": [],
         "why": "The /sigma-promote and /sigma-unpark inbox: parked and blocked "
                "items. On labels, one qualifier, because a project filter cannot OR "
                "across two fields; the labels are what the Status lanes mirror."},
        {"name": ROADMAP, "layout": "ROADMAP_LAYOUT",
         "filter": one_of(prio, ("P0", "P1")) if prio else "",
         "fields": cols("Title", status, prio, "Milestone"),
         "group_by": "Milestone", "sort": [], "markers": "Milestones",
         "why": "The v1.0 plan: the P0/P1 items laid out by Milestone, with the milestones' due "
                "dates as markers."},
        {"name": EPICS, "layout": "TABLE_LAYOUT", "filter": qualifier("label") + value(EPIC_LABEL),
         "fields": cols("Title", "Sub-issues progress", prio, "Milestone"),
         "group_by": None, "sort": [],
         "why": "Epics and how far through their sub-issues each one is."},
    ]
    return {"fields": fields, "views": views, "builtins": list(BUILTINS)}


def as_json(spec):
    """The spec as plain JSON-able data (`board_layout.py spec`)."""
    out = {"fields": [], "views": [], "builtins": spec["builtins"]}
    for f in spec["fields"]:
        out["fields"].append({"name": f["name"], "type": f["type"], "provisioned_by": f["owner"],
                              "options": [o for o, _c in f["options"]] if f["options"] else None})
    for v in spec["views"]:
        out["views"].append({k: v.get(k) for k in ("name", "layout", "filter", "fields", "column_by",
                                                   "group_by", "sort", "markers", "default", "why")
                             if v.get(k) not in (None, [])})
    return out
