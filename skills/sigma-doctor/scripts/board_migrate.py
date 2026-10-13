#!/usr/bin/env python3
"""Add a `Ready` lane to an EXISTING GitHub Projects v2 board, without wiping card Status (#696).

New boards Sigma creates already carry the six lanes (`Backlog | Ready | In Progress | QC |
Done | Blocked`). An existing board — every current adopter, including this repo's own project #6 —
has no `Ready` option, which is precisely what keeps it on the historical label queue: `sources.py`'s
`_ready_lane()` returns None and nothing changes. This script is the deliberate, human-invoked way
to opt such a board in.

It is NOT reachable from any loop path, by construction: nothing imports it, no hook calls it, and
it does nothing at all unless a human names the project on the command line and adds `--apply`.

    python3 board_migrate.py --owner <login|@me> --project <number>            # dry run (default)
    python3 board_migrate.py --owner <login|@me> --project <number> --apply

WHY ITS OWN OPTION BUILDER, rather than reusing `sources._options_mutation`: verified on a
throwaway project before this was written —

    [2] append option WITH ids -> card keeps Status:   PASS (status='In Progress')
    [3] rewrite WITHOUT ids    -> card LOSES Status:   PASS (status=None)

`ProjectV2SingleSelectFieldOptionInput` accepts an `id`. Re-send every existing option with its id
and cards keep their Status; omit the ids and every card's Status is wiped to null. `sources.py`'s
builder omits them — correct for a board the kit creates from scratch, catastrophic for an adopted
one. That is also why `_ensure_status_field`'s `created_now` gate must never be relaxed.

Additive only: an existing option is never removed or renamed, and its colour and description ride
along so the board's appearance is not silently reset.
"""
import json, sys

READY = "Ready"
_NEW_COLOR = "BLUE"


def _run_gh(args):
    import subprocess
    p = subprocess.run(args, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip() or "gh failed")
    return p.stdout


def _root(owner):
    """`@me` is not a login GraphQL can look up — it needs the `viewer` root. A real login may be a
    User or an Organization, so both inline fragments cover it without knowing which."""
    if str(owner).startswith("@"):
        return "viewer { %s }", "viewer"
    return ('repositoryOwner(login: "%s") {' % owner) + " ... on User { %s } ... on Organization { %s } }", \
           "repositoryOwner"


def _query(owner, inner):
    tmpl, key = _root(owner)
    body = tmpl % ((inner,) * tmpl.count("%s"))
    return "query { %s }" % body, key


def _unwrap(raw, key):
    data = (json.loads(raw or "{}") or {}).get("data") or {}
    return (data.get(key) or {}).get("projectV2") or {}


def read_status_field(owner, number, field_name, run):
    """The board id plus the Status field with its options — id, name, colour and description, all
    of which have to be re-sent. `gh project field-list` returns only id+name, so this reads through
    GraphQL instead: dropping colour/description would reset the board's appearance on write."""
    inner = ('projectV2(number: %d) { id field(name: "%s") { '
             '... on ProjectV2SingleSelectField { id name options { id name color description } } } }'
             % (int(number), field_name))
    q, key = _query(owner, inner)
    proj = _unwrap(run(["gh", "api", "graphql", "-f", "query=" + q]), key)
    return proj.get("id"), (proj.get("field") or {})


def build_options_mutation(field_id, options, ready_name=READY):
    """The `updateProjectV2Field` mutation that APPENDS `ready_name`, preserving everything else.

    Every pre-existing option is re-sent WITH its id, which is what keeps each card's Status intact
    (rehearsal step [2]); the appended option is the only one without one, because it does not exist
    yet. Colour and description are carried through verbatim."""
    parts = []
    new = '{name: "%s", color: %s, description: ""}' % (ready_name, _NEW_COLOR)
    for o in options:
        # #1319 (PROVISIONAL): Ready goes before a `Parked` lane so the board reads in flow order;
        # with no `Parked` lane it is appended, exactly as before.
        if str(o.get("name") or "").strip().casefold() == "parked" and new not in parts:
            parts.append(new)
        parts.append('{id: "%s", name: "%s", color: %s, description: "%s"}'
                     % (o.get("id"), o.get("name"), o.get("color") or "GRAY",
                        (o.get("description") or "").replace('"', "'")))
    if new not in parts:
        parts.append(new)
    return ('query=mutation { updateProjectV2Field(input: {fieldId: "%s", singleSelectOptions: [%s]}) '
            '{ projectV2Field { ... on ProjectV2SingleSelectField { id } } } }'
            % (field_id, ", ".join(parts)))


def card_statuses(owner, number, field_name, run):
    """{issue number -> Status name or None} for every item, paged. The before/after snapshot the
    post-condition check compares."""
    out, after = {}, None
    while True:
        page = 'items(first: 100%s)' % (', after: "%s"' % after if after else "")
        inner = ('projectV2(number: %d) { %s { nodes { content { ... on Issue { number } } '
                 'fieldValueByName(name: "%s") { ... on ProjectV2ItemFieldSingleSelectValue { name } } } '
                 'pageInfo { hasNextPage endCursor } } }' % (int(number), page, field_name))
        q, key = _query(owner, inner)
        items = _unwrap(run(["gh", "api", "graphql", "-f", "query=" + q]), key).get("items") or {}
        for node in items.get("nodes") or []:
            n = (node.get("content") or {}).get("number")
            if n is not None:
                out[int(n)] = (node.get("fieldValueByName") or {}).get("name")
        info = items.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            return out
        after = info.get("endCursor")


_SEED_LIMIT = 5000        # mirrors sources.GitHubSource._BOARD_ITEM_LIMIT; `--limit 0` is NOT unlimited


def seedable_cards(owner, number, goal_label, backlog_name, run):
    """Board items that a migration has to move into `Ready`: OPEN, carrying the goal label, and
    still sitting in the backlog column. Selected SERVER-SIDE by `--query` (verified live: 46 items
    on project #6), so the filter cannot silently disagree with what the loop will later read."""
    query = 'is:open status:"%s" label:"%s"' % (backlog_name, goal_label)
    raw = run(["gh", "project", "item-list", str(number), "--owner", owner, "--format", "json",
               "--limit", str(_SEED_LIMIT), "--query", query])
    data = json.loads(raw or "{}")
    return (data.get("items") if isinstance(data, dict) else data) or []


def seed_ready(project_id, field_id, ready_option_id, cards, run):
    """Move each card to `Ready`. Returns the number moved; a card that fails is reported by the
    caller rather than aborting the rest — a partial seed is strictly better than none, and the
    command is idempotent, so re-running finishes the job."""
    moved, failed = 0, []
    for c in cards:
        try:
            run(["gh", "project", "item-edit", "--project-id", project_id, "--id", c.get("id"),
                 "--field-id", field_id, "--single-select-option-id", ready_option_id])
            moved += 1
        except Exception as exc:
            failed.append("%s (%s)" % ((c.get("content") or {}).get("number", c.get("id")), exc))
    return moved, failed


def _flags(argv):
    out = {}
    for i, a in enumerate(argv):
        if a.startswith("--"):
            nxt = argv[i + 1] if i + 1 < len(argv) else ""
            out[a[2:]] = True if (not nxt or nxt.startswith("--")) else nxt
    return out


USAGE = ("usage: board_migrate.py --owner <login|@me> --project <number> "
         "[--ready Ready] [--status-field Status] [--apply]\n"
         "       dry run unless --apply is given; never inferred from config.")


def main(argv, run=None):
    if argv[1:] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    run = run or _run_gh
    f = _flags(argv[1:])
    owner, number = f.get("owner"), f.get("project")
    # Refuse to infer the board. Migrating whatever project the config happens to point at is exactly
    # the "helpful" behaviour that must not exist here — the operator names it or nothing happens.
    if not owner or not number or owner is True or number is True:
        print(USAGE, file=sys.stderr)
        return 2
    ready = f.get("ready") if isinstance(f.get("ready"), str) else READY
    field_name = f.get("status-field") if isinstance(f.get("status-field"), str) else "Status"
    apply_it = f.get("apply") is True

    try:
        project_id, field = read_status_field(owner, number, field_name, run)
    except Exception as exc:
        print("could not read the board's %s field: %s" % (field_name, exc), file=sys.stderr)
        return 1
    options = field.get("options") or []
    if not field.get("id"):
        print("no single-select field named %r on project %s" % (field_name, number), file=sys.stderr)
        return 1
    goal_label = f.get("goal-label") if isinstance(f.get("goal-label"), str) else "sdlc:goal"
    backlog = f.get("backlog") if isinstance(f.get("backlog"), str) else "Backlog"
    names = [o.get("name") for o in options]
    have_lane = ready in names

    # Adding the lane and SEEDING it are separate, independently idempotent steps (#707). They have
    # to be: adding `Ready` is what makes Status authoritative, and a board where Status is
    # authoritative but NOTHING is in Ready reads as a permanently empty queue — next_pending()
    # returns None, the loop reports DONE, and `_sync_backlog` (the only thing that would promote
    # those cards) never runs, because it fires from a status WRITE that needs a goal to have been
    # picked first. So a board that already HAS the lane is not a no-op to be skipped; if its Ready
    # column is empty it is precisely the stuck board this command exists to repair.
    try:
        cards = seedable_cards(owner, number, goal_label, backlog, run)
    except Exception as exc:
        print("could not list the cards to seed: %s" % exc, file=sys.stderr)
        return 1

    if have_lane:
        print("project %s already has a %r lane (%s)" % (number, ready, " | ".join(names)))
    else:
        print("before: %s" % " | ".join(names))
        print("after:  %s" % " | ".join(names + [ready]))
    print("to seed: %d card(s) open, labelled %s, still in %s" % (len(cards), goal_label, backlog))
    if have_lane and not cards:
        print("\nnothing to do — the lane exists and no card is waiting in %s." % backlog)
        return 0
    if not apply_it:
        print("\ndry run — nothing was written. Re-run with --apply to make the change.")
        return 0

    before = card_statuses(owner, number, field_name, run)
    if not have_lane:
        try:
            run(["gh", "api", "graphql", "-f", build_options_mutation(field["id"], options, ready)])
        except Exception as exc:
            print("the option update failed: %s" % exc, file=sys.stderr)
            return 1
        # Re-read: the option ids the seed step needs only exist after the write.
        project_id, field = read_status_field(owner, number, field_name, run)
    ready_id = next((o.get("id") for o in (field.get("options") or []) if o.get("name") == ready), None)

    # Post-condition. Rehearsal step [3] showed exactly how this goes wrong, so it is checked rather
    # than trusted: a card that HAD a Status and now has none was damaged by the write.
    after = card_statuses(owner, number, field_name, run)
    lost = sorted(n for n, s in before.items() if s and not after.get(n))
    if lost:
        print("FAILED: %d card(s) lost their Status: %s" % (len(lost), ", ".join(str(n) for n in lost)))
        print("Restore them by hand on the board. Do NOT re-run this until that is understood.")
        return 1

    moved, failed = (0, [])
    if cards:
        if not ready_id:
            print("FAILED: the %r lane exists but its option id could not be read, so no card was "
                  "seeded. The board is in the stuck state — re-run this command." % ready)
            return 1
        moved, failed = seed_ready(project_id, field["id"], ready_id, cards, run)

    print("\n%s; %d card(s) kept their Status; %d moved into %r."
          % (("added the %r lane" % ready) if not have_lane else ("%r lane already present" % ready),
             len(before), moved, ready))
    if failed:
        # Partial seeds leave a usable board (the moved cards are pickable) but an incomplete one,
        # and the command is idempotent, so the honest instruction is "run it again".
        print("could not move %d card(s): %s" % (len(failed), "; ".join(failed)))
        print("Re-run this command to finish — it only ever moves %s -> %r." % (backlog, ready))
        return 1
    if moved == 0 and not have_lane:
        print("No card was waiting in %s, so nothing is queued yet. Move the cards you want worked "
              "into %r on the board, or the loop will report an empty backlog." % (backlog, ready))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
