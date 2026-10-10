"""The independent reviewer route for a resolved unit conflict (upkeep part B, slice 9; #950).

A library, loaded by path; no command line, nothing at import time, and NOTHING CALLS IT YET: the caller is the upkeep
pass, behind its gate. While the gate is closed (the default) `review` returns `closed` having read nothing, written
nothing and spawned nothing.

WHAT IT BINDS. A second headless session, started through the resolver launcher (`feature_upkeep_launcher.py`: the same
isolation, environment rules, metering and ceilings), given a brief of the base, ours, theirs and resolved contents of the
conflicted files plus the capped pull-request descriptions, and NO resolver transcript: the request has no field that
could carry one. WHAT IT CANNOT PROVE: that the resolver did not influence the reviewer (they share a repository, a
catalog, a team ledger and an operator). It is a separate session given a different brief, nothing stronger. The route is
labelled `verified: false` in every result until it has run end to end against the real command line.

THE ORDER of `review(request)` (it returns a `Review` and raises only for a programming error):
  1. the gate: the project block on and `conflicts.resolve` set to `agent`, else `closed`;
  2. the mechanism: `inline` (a self-review) and `subagent` (cannot be spawned from Python) are refused by construction,
     and so is anything else that is not `headless`;
  3. the unit name, then the caps (the reviewer's cap must be smaller than the resolver's), then the model: the catalog
     id that differs from the resolver's when a second exists, else the placeholder, which keeps the route closed;
  4. the brief is built, and the leak gate `reviewer._check_brief_text` runs over the exact text that will be sent;
  5. a write-once manifest is written (unit key, base, head, resolved tree, each path with its sha256, the brief's sha256);
  6. the launcher runs the session;
  7. the reply goes through a strict local validator; anything else, an unreadable reply and a timeout are `block`;
  8. the tree is re-read and compared with the manifest BEFORE a verdict is accepted; a moved tree is `block`.
It never calls `reviewer.resolve` and adds no key to the host command table, so the session markers a background job may
have inherited cannot route it and `review.host: claude` for ordinary pull-request reviews is untouched.

THE REVIEWER CAN ONLY ADD A REFUSAL. An `approve` here is one input to the caller's gate; it never overrides a refusal.

UNVERIFIED, and labelled where used: the shape of the reply envelope of `claude -p --output-format json` (this module
accepts either the bare verdict object or an object whose `result` is the verdict as text; nothing here ran the real
command line), and the CLI's schema flag, which is not used.

THE STORE: `<sdlc>/state/upkeep/reviews/<unit-key>/<generation>.json`, one manifest per review, never rewritten. At most
MAX_GENERATIONS per unit; `prune` ages them out. Empty unit directories are left (one per unit, bounded by the unit count).
This namespace is its own: it never shares the goal-keyed review generations or their pruner.
"""
import collections
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import time

_HERE = pathlib.Path(__file__).resolve().parent
_LOADED = {}


def _sibling(name):
    if name not in _LOADED:
        spec = importlib.util.spec_from_file_location(name, _HERE / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _LOADED[name] = module
    return _LOADED[name]


CLOSED, REFUSED, APPROVE, BLOCK = "closed", "refused", "approve", "block"
OUTCOMES = (CLOSED, REFUSED, APPROVE, BLOCK)
#: The route is unverified until it has run end to end against the real command line.
VERIFIED = False
Review = collections.namedtuple("Review", "outcome reasons verified charged_usd manifest", defaults=((), False, 0.0, None))

MECHANISM = "headless"
REFUSED_MECHANISMS = ("inline", "subagent")

STORE_REL = "state/upkeep/reviews"
SCHEMA_ID = "upkeep-review/1"
MAX_GENERATIONS = 50
MAX_MANIFEST_CHARS = 65536
MAX_PRUNE_SCAN = 100000
DEFAULT_KEEP_DAYS = 14
_MANIFEST_NAME = re.compile(r"[0-9]{1,6}\.json")

MAX_FILE_CHARS = 20000
MAX_BRIEF_CHARS = 120000
MAX_FILES = 40
MAX_PR_CHARS = 2000
MAX_PR_COUNT = 10
MAX_REASONS = 20
MAX_REASON_CHARS = 300

Request = collections.namedtuple(
    "Request",
    "config unit base_sha head_sha tree_sha files pr_descriptions reread launch cap_usd resolver_cap_usd "
    "resolver_model model catalog mechanism timeout now",
    defaults=(None,) * 17)


# ------------------------------------------------------------------------------------------ the verdict

def _no_dupes(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def _no_constant(name):
    raise ValueError("non-finite number")


def _loads(text):
    return json.loads(text, object_pairs_hook=_no_dupes, parse_constant=_no_constant)


def validate(obj):
    """-> (verdict, reasons) for an object that is EXACTLY {"verdict": "approve"|"block", "reasons": [str, ...]};
    anything else -> (BLOCK, [why]). Strict: no extra keys, no non-string reasons, bounded counts."""
    if not isinstance(obj, dict) or set(obj) != {"verdict", "reasons"}:
        return BLOCK, ["the reply is not an object with exactly the keys verdict and reasons"]
    verdict, reasons = obj["verdict"], obj["reasons"]
    if verdict not in (APPROVE, BLOCK) or type(verdict) is not str:
        return BLOCK, ["the verdict is neither approve nor block"]
    if not isinstance(reasons, list) or len(reasons) > MAX_REASONS or not all(type(r) is str for r in reasons):
        return BLOCK, ["reasons is not a short list of strings"]
    return verdict, neutralise(reasons)


def parse_reply(text):
    """-> (verdict, reasons). Total: any text, including none, gives a pair, and the pair is BLOCK unless it validates.
    UNVERIFIED envelope: a bare verdict object, or an object whose `result` is the verdict as text."""
    if not isinstance(text, str) or not text.strip():
        return BLOCK, ["the reply is empty or unreadable"]
    try:
        obj = _loads(text.strip())
        if isinstance(obj, dict) and "verdict" not in obj and isinstance(obj.get("result"), str):
            obj = _loads(obj["result"].strip())
    except (ValueError, RecursionError):
        return BLOCK, ["the reply is not valid JSON"]
    return validate(obj)


_CLOSING = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b\s*:?\s*(?:#|GH-)?\d+", re.IGNORECASE)
_MARKER = re.compile(r"sigma:[A-Za-z_-]+", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x1f\x7f  ]+")


def neutralise(reasons):
    """Model output is untrusted text: control characters, markup openers, mentions, closing keywords and marker strings
    are removed, and each reason is bounded. The list is bounded too."""
    out = []
    for item in list(reasons)[:MAX_REASONS]:
        text = _CONTROL.sub(" ", str(item))
        text = text.replace("<!--", "").replace("-->", "").replace("`", "'").replace("@", "(at)")
        text = _MARKER.sub("[marker removed]", text)
        text = _CLOSING.sub("[reference removed]", text)
        out.append(" ".join(text.split())[:MAX_REASON_CHARS])
    return out


# ------------------------------------------------------------------------------------------ the brief

def _cap(text, limit):
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[:limit] + "\n[truncated]"


def build_brief(files, pr_descriptions):
    """-> (prompt, blocks). `files` is a list of (path, base, ours, theirs, resolved). The resolver's transcript is not an
    input: nothing in the request could carry it."""
    prompt = ("You are an independent reviewer of a merge conflict resolution. For each file you are given the common base, "
              "our side, their side and the resolved content. Judge whether the resolved content preserves the intent of "
              "both sides and loses nothing. Reply with ONLY a JSON object: "
              '{"verdict": "approve" or "block", "reasons": ["short reason", ...]}. Block when unsure.')
    blocks, used = [], 0
    for index, item in enumerate(list(files or ())[:MAX_FILES]):
        path, base, ours, theirs, resolved = item
        for label, text in (("base", base), ("ours", ours), ("theirs", theirs), ("resolved", resolved)):
            body = _cap(text, MAX_FILE_CHARS)
            used += len(body)
            if used > MAX_BRIEF_CHARS:
                break
            blocks.append(("file%d-%s-%s" % (index, label, _label_safe(path)), body))
    for index, text in enumerate(list(pr_descriptions or ())[:MAX_PR_COUNT]):
        blocks.append(("pr%d" % index, _cap(text, MAX_PR_CHARS)))
    return prompt, blocks


def truncation(files, pr_descriptions):
    """-> reasons the brief would be incomplete: a cap that cuts or drops input. A reviewer shown part of the material cannot
    approve the whole, so the entry point blocks on any reason here rather than launching on a partial brief."""
    files, prs, why, used = list(files or ()), list(pr_descriptions or ()), [], 0
    if len(files) > MAX_FILES:
        why.append("the brief was truncated: %d files exceed the %d-file cap" % (len(files), MAX_FILES))
    for item in files[:MAX_FILES]:
        for text in item[1:5]:
            size = len("" if text is None else str(text))
            if size > MAX_FILE_CHARS:
                why.append("the brief was truncated: a file side exceeds %d characters" % MAX_FILE_CHARS)
            used += min(size, MAX_FILE_CHARS) + (len("\n[truncated]") if size > MAX_FILE_CHARS else 0)
    if used > MAX_BRIEF_CHARS:
        why.append("the brief was truncated: file content exceeds the %d-character brief budget" % MAX_BRIEF_CHARS)
    if len(prs) > MAX_PR_COUNT:
        why.append("the brief was truncated: %d PR descriptions exceed the %d cap" % (len(prs), MAX_PR_COUNT))
    if any(len("" if t is None else str(t)) > MAX_PR_CHARS for t in prs[:MAX_PR_COUNT]):
        why.append("the brief was truncated: a PR description exceeds %d characters" % MAX_PR_CHARS)
    return list(dict.fromkeys(why))


def _label_safe(path):
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(path))[:40]


def brief_text(prompt, blocks):
    return "\n".join([prompt] + [str(text) for _, text in blocks])


def brief_hash(prompt, blocks):
    digest = hashlib.sha256()
    digest.update(prompt.encode("utf-8", "replace"))
    for label, text in blocks:
        digest.update(b"\0" + label.encode() + b"\0" + str(text).encode("utf-8", "replace"))
    return digest.hexdigest()


def sha256_text(text):
    return hashlib.sha256(("" if text is None else str(text)).encode("utf-8", "replace")).hexdigest()


# ------------------------------------------------------------------------------------------ the store

def store_dir(sdlc_dir, unit):
    """The one place a unit NAME becomes the directory of its review manifests. Refuses what `feature_registry.unit_path`
    refuses and folds the name the same way, AFTER the refusal."""
    registry = _sibling("feature_registry")
    if not (isinstance(unit, str) and registry.is_unit_name(unit)):
        raise registry.InvalidUnitName("%r is not a unit name, so it has no review store" % (unit,))
    return pathlib.Path(sdlc_dir).joinpath(*STORE_REL.split("/"), registry.unit_key(unit))


def make_manifest(unit, base_sha, head_sha, tree_sha, files, prompt, blocks):
    """The generation is not an input: `write_manifest` assigns it under the store's own listing, so the builder carries no
    placeholder that could be mistaken for the real number."""
    return {"schema": SCHEMA_ID, "unit": unit, "base": base_sha, "head": head_sha,
            "tree": tree_sha, "paths": {str(f[0]): sha256_text(f[4]) for f in files or ()},
            "brief_sha256": brief_hash(prompt, blocks)}


def write_manifest(sdlc_dir, unit, doc_fields):
    """Write the next generation, once. -> (path, doc) or (None, reason). Never rewrites an existing generation."""
    state = _sibling("state")
    directory = store_dir(sdlc_dir, unit)
    try:
        state.refuse_symlinks(sdlc_dir, (directory / "x").relative_to(pathlib.Path(sdlc_dir)), create_parents=True)
        directory.mkdir(exist_ok=True)
        taken = [int(p.stem) for p in directory.iterdir() if _MANIFEST_NAME.fullmatch(p.name)]
    except (OSError, ValueError):
        return None, "the review store is unwritable"
    generation = (max(taken) if taken else 0) + 1
    if len(taken) >= MAX_GENERATIONS:
        return None, "the unit has %d review manifests already; prune before another review" % len(taken)
    doc = dict(doc_fields, generation=generation)
    text = json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n"
    if len(text) > MAX_MANIFEST_CHARS:
        return None, "the manifest is too large"
    path = directory / ("%d.json" % generation)
    try:
        with open(path, "x", encoding="utf-8") as handle:
            handle.write(text)
    except FileExistsError:
        return None, "the manifest generation already exists (write-once)"
    except OSError:
        return None, "the review store is unwritable"
    return path, doc


def prune(sdlc_dir, config, keep_days=DEFAULT_KEEP_DAYS, now=None):
    """Remove manifest-shaped files older than `keep_days` under the review store -> how many. Never follows or removes a
    symlink; scans at most MAX_PRUNE_SCAN entries. Closed gate: 0."""
    if _sibling("feature_upkeep").conflict_level(config) != "agent":
        return 0
    if type(keep_days) is not int or keep_days < 1:
        raise ValueError("keep_days must be a positive whole number")
    cutoff = (time.time() if now is None else now) - keep_days * 86400
    removed, seen = 0, 0
    root = pathlib.Path(sdlc_dir).joinpath(*STORE_REL.split("/"))
    try:
        with os.scandir(root) as units:
            for unit in units:
                if unit.is_symlink() or not unit.is_dir(follow_symlinks=False):
                    continue
                with os.scandir(unit.path) as entries:
                    for entry in entries:
                        seen += 1
                        if seen > MAX_PRUNE_SCAN:
                            return removed
                        if not _MANIFEST_NAME.fullmatch(entry.name) or entry.is_symlink():
                            continue
                        try:
                            if entry.is_file(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_mtime < cutoff:
                                os.unlink(entry.path)
                                removed += 1
                        except OSError:
                            continue
    except OSError:
        return removed
    return removed


# ------------------------------------------------------------------------------------------ the model

def pick_model(catalog, resolver_model, override=None):
    """The reviewer's model id: the override when given, else a catalog id that differs from the resolver's, else the
    placeholder (which the launcher refuses, so the route stays closed when no second id exists)."""
    launcher = _sibling("feature_upkeep_launcher")
    if override is not None:
        return override
    ids = sorted({v for v in (catalog or launcher.CATALOG).values() if v != launcher.PLACEHOLDER_MODEL})
    others = [i for i in ids if i != resolver_model]
    return others[0] if others else launcher.PLACEHOLDER_MODEL


# ------------------------------------------------------------------------------------------ the entry point

def _blocked(reasons, charged=0.0, manifest=None):
    return Review(BLOCK, tuple(neutralise(reasons)), VERIFIED, charged, manifest)


def review(request, environ=None):
    """-> Review. See the module docstring for the order."""
    req = request
    if _sibling("feature_upkeep").conflict_level(req.config) != "agent":
        return Review(CLOSED, ("the upkeep gate or conflicts.resolve=agent is not on",), VERIFIED)
    mechanism = req.mechanism or MECHANISM
    if mechanism in REFUSED_MECHANISMS or mechanism != MECHANISM:
        return Review(REFUSED, ("the %s mechanism is refused: only a separate headless session is an independent review"
                                % (mechanism if isinstance(mechanism, str) else "given"),), VERIFIED)
    try:
        store_dir(req.launch["sdlc_dir"], req.unit)
    except ValueError as exc:
        return Review(REFUSED, (str(exc),), VERIFIED)
    if req.cap_usd is None or req.resolver_cap_usd is None or not 0 < req.cap_usd < req.resolver_cap_usd:
        return Review(REFUSED, ("the reviewer needs its own per-run cap, smaller than the resolver's",), VERIFIED)
    launcher = _sibling("feature_upkeep_launcher")
    model = pick_model(req.catalog, req.resolver_model, req.model)
    cut = truncation(req.files, req.pr_descriptions)
    if cut:
        return _blocked(cut)
    prompt, blocks = build_brief(req.files, req.pr_descriptions)
    ok, findings = _sibling("reviewer")._check_brief_text(brief_text(prompt, blocks), ())
    if not ok:
        return _blocked(["the brief failed the leak gate: " + "; ".join(findings)])
    sdlc_dir = req.launch["sdlc_dir"]
    path, doc = write_manifest(sdlc_dir, req.unit, make_manifest(
        req.unit, req.base_sha, req.head_sha, req.tree_sha, req.files, prompt, blocks))
    if path is None:
        return _blocked([doc])
    fields = dict(req.launch, config=req.config, prompt=prompt, blocks=blocks, conflicted=(), cap_usd=req.cap_usd,
                  model=model, timeout=req.timeout or req.launch.get("timeout"))
    result = launcher.launch(launcher.Request(**fields), environ=environ)
    charged = result.charged_usd
    if result.outcome == launcher.CLOSED:
        return Review(CLOSED, (result.reason,), VERIFIED, charged, doc)
    if result.outcome == launcher.REFUSED:
        return Review(REFUSED, tuple(neutralise([result.reason])), VERIFIED, charged, doc)
    if result.outcome != launcher.OK:
        return _blocked(["the reviewer session ended %s" % result.outcome], charged, doc)
    verdict, reasons = parse_reply(result.reason)
    try:
        tree, hashes = req.reread()
    except Exception:                                       # noqa: BLE001 - an unreadable tree is a block
        return _blocked(["the tree could not be re-read, so the verdict is not accepted"], charged, doc)
    if tree != doc["tree"] or dict(hashes or {}) != doc["paths"]:
        return _blocked(["the tree changed during the review, so the verdict is not accepted"], charged, doc)
    return Review(verdict, tuple(reasons), VERIFIED, charged, doc)
