"""The proof gate's checks for a machine-resolved unit rebase, as pure functions. Nothing here resolves, stamps,
continues or pushes; a later slice assembles these into one gate.

A library, loaded by path; no CLI. It reads no configuration, so it is not a reader of the `upkeep` block and
carries no gate: the callers that act on a verdict sit behind the gate. Every check returns a list of REFUSALS,
each `{"code", "path"|"sha", "detail"}`; an empty list means the check passes. Nothing partially passes, and an
input a check cannot read is a refusal, never a pass (a check that fails open is a check someone trusts wrongly).

THE CHECKS (letters follow the design):
  B  pair_commits        every original commit is replayed, skipped as already upstream, or emptied at a stop
  C  only_conflicted_differ / multiset_differences   nothing outside the conflicted set changed
  D  check_markers / new_whitespace_findings         no new conflict marker, no new whitespace finding
  E  check_tests         no net loss of tests or assertions in a conflicted Python test file

PAIRING DOES NOT REST ON PATCH-ID EQUALITY. A base edit within three lines of a hunk changes a clean replay's
patch-id although nothing the unit changed differs, and a resolved commit's patch-id differs from its original's by
construction. A commit pairs by its STOP RECORD (exact, for a step that stopped), else by its AUTHORSHIP KEY (author
name, email, timestamp and subject, which `git rebase` carries over unchanged), and the key must be unique in both
ranges. Skipped-as-upstream stays a patch-id test because git's own skip is one. `pair_commits` takes the key as a
parameter so a control can swap it for patch-id equality and watch the context-drift fixture turn red.

LIMITS, all of which err toward parking:
  * the test counter reads Python only; a conflicted test file in another language refuses (D-26). A resolution that
    keeps one copy of a test both sides added identically fails `resolved >= ours + theirs - base` and parks.
  * a commit that becomes empty on a clean replay has no key and is unaccounted (D-7); a same-second same-subject
    pair collides and parks (D-41); a merge commit pairs by the key alone.
  * `git range-diff` is not the authority. It paired context-drifted commits but did not pair a resolved commit with
    its original in the one-line fixture measured, and it paired it in a 40-line fixture, so whether it can pair a
    resolved commit depends on the fixture. Its output is porcelain that may change; nothing here parses it.
  * the merge comparison (`-m`, one diff per parent) is exercised on one real merge stop only (D-6).
"""
import collections
import re
import subprocess

# ------------------------------------------------------------------------------------------------ refusal codes
KEY_COLLISION = "key-collision"
STOP_KEY_DISAGREE = "stop-key-disagree"
STOP_MISSING = "stop-record-unmatched"
UNACCOUNTED = "unaccounted-original"
UNMATCHED_REPLAYED = "replayed-matches-no-original"
REORDERED = "reordered"
STAGE0_CHANGED = "outside-conflicted-set-changed"
MULTISET_CHANGED = "outside-conflicted-lines-changed"
MARKER = "conflict-marker"
WHITESPACE = "new-whitespace-finding"
TEST_LANGUAGE = "test-language-unsupported"
TEST_UNREADABLE = "test-blob-unreadable"
TEST_LOSS = "test-or-assertion-loss"
TEST_SKIP = "test-skip-added"
UNPARSEABLE_LOG = "unparseable-git-log"

DEADLINE_SECONDS = 60

_HEADER_SEP = "\x1f"
_RECORD_SEP = "\x1e"


def _refuse(code, detail, **where):
    out = {"code": code, "detail": detail}
    out.update(where)
    return out


# ------------------------------------------------------------------------------------------------ readers (git)
def read_commits(run, cwd, rng, patch_id_fn=None):
    """The commits of `rng` (a rev range), oldest first, merges included, from ONE `git log -p -U0 --no-renames -m`.

    Each is `{"sha", "parents", "author", "subject", "key", "diff", "patch_id"}`. `diff` is
    `per_path_multisets(...)`: a merge shows one diff per parent, accumulated under `path@<n>` for the n-th parent.
    `patch_id` comes from one batched `git patch-id --stable` over a default-context `git log -p` of the range (not a process per commit), or ""
    when `patch_id_fn` is None. RAISES whatever the runner raises."""
    text = str(run(cwd, ["git", "-c", "core.quotePath=false", "log", "--reverse", "-m", "-p", "-U0", "--no-renames",
                         "--binary", "--full-index", "--format=%x00%H%n%P%n%an%n%ae%n%at%n%s", rng]) or "")
    pids = {}
    if patch_id_fn:        # git's own skip test reads the default-context patch, so it gets its own one batched log
        pids = _patch_ids(patch_id_fn, str(run(cwd, ["git", "-c", "core.quotePath=false", "log", "--reverse", "-p",
                                                    "--no-renames", "--format=commit %H", rng]) or ""))
    commits, seen = [], collections.Counter()
    for rec in text.split("\x00"):     # NUL cannot occur in a commit header or a text diff; control bytes 0x1e/0x1f can
        if not rec:
            continue
        fields = rec.split("\n", 6)
        if len(fields) != 7:           # an unparseable chunk must never be dropped: both sides would shrink alike
            raise ValueError("%s: git log chunk has %d fields, expected 7" % (UNPARSEABLE_LOG, len(fields)))
        sha, parents, name, email, stamp, subject, body = fields
        nth = seen[sha]
        seen[sha] += 1
        diff = per_path_multisets(body, suffix="@%d" % nth if (nth or len(parents.split()) > 1) else "")
        if nth:
            commits[-1]["diff"].update(diff)
            continue
        commits.append({"sha": sha, "parents": parents.split(), "author": (name, email, stamp),
                        "subject": subject, "key": authorship_key(name, email, stamp, subject),
                        "diff": diff, "patch_id": pids.get(sha, "")})
    return commits


def _patch_ids(patch_id_fn, log_text):
    """`{commit sha: stable patch-id}` from the output of the injected batch function (one process for the lot)."""
    out = {}
    for line in str(patch_id_fn(log_text) or "").splitlines():
        fields = line.split()
        if len(fields) == 2:
            out[fields[1]] = fields[0]
    return out


def git_patch_id_fn(cwd, timeout=DEADLINE_SECONDS):
    """The default batched patch-id function: `git patch-id --stable` fed the whole `git log -p` text, bounded by a
    deadline. A timeout or failure RAISES: a pairing that could not be computed is not "nothing was skipped"."""
    def run_patch_id(text):
        proc = subprocess.run(["git", "patch-id", "--stable"], input=text, cwd=str(cwd), capture_output=True,
                              text=True, timeout=timeout, check=True)
        return proc.stdout
    return run_patch_id


def upstream_patch_ids(run, cwd, base_rng, patch_id_fn):
    """The set of patch-ids of the base-side commits in `base_rng`, for the skipped-as-upstream test."""
    text = str(run(cwd, ["git", "log", "-p", "--no-renames", "--format=commit %H", base_rng]) or "")
    return set(_patch_ids(patch_id_fn, text).values())


# ------------------------------------------------------------------------------------------------ B: pairing
def authorship_key(name, email, stamp, subject):
    return (name, email, str(stamp), subject)


def key_of(commit):
    return commit["key"]


def patch_id_of(commit):
    """The key a control swaps in: the patch-id, which context drift changes."""
    return commit["patch_id"]


def pair_commits(originals, replayed, stops=None, upstream=None, emptied=None, key=key_of):
    """Account for every original commit. `originals` and `replayed` come from `read_commits` (oldest first).

    `stops` maps an original sha to its stop record `{"new_head": sha}` (written before resolving, completed after
    the engine's own stamped commit). `upstream` is the set of base-side patch-ids; `emptied` the shas that became
    empty at a stop. Returns `{"pairs": [(orig_sha, new_sha)], "skipped": [shas], "emptied": [shas], "refusals": []}`.

    Each original is exactly one of: replayed (stop record, else a key unique in BOTH ranges), skipped as upstream,
    or emptied at a stop. Anything else parks with a named code, and so does a stop record that disagrees with the
    key, a replayed commit matching no original, and any reordering. Ambiguity never passes."""
    stops, upstream, emptied = stops or {}, upstream or set(), set(emptied or ())
    by_sha = {c["sha"]: c for c in replayed}
    orig_keys = collections.Counter(key(c) for c in originals)
    new_keys = collections.Counter(key(c) for c in replayed)
    by_key = {key(c): c for c in replayed}
    pairs, skipped, empty, refusals, claimed = [], [], [], [], set()
    for orig in originals:
        sha = orig["sha"]
        record = stops.get(sha)
        if record:
            new = by_sha.get(record.get("new_head"))
            if new is None:
                refusals.append(_refuse(STOP_MISSING, "the stop record names a commit not in the replayed range", sha=sha))
            elif key(new) != key(orig) and key(orig) in by_key and by_key[key(orig)]["sha"] != new["sha"]:
                refusals.append(_refuse(STOP_KEY_DISAGREE, "the stop record and the key name different commits", sha=sha))
            else:
                pairs.append((sha, new["sha"]))
                claimed.add(new["sha"])
            continue
        k = key(orig)
        if k and (orig_keys[k] > 1 or new_keys[k] > 1):
            refusals.append(_refuse(KEY_COLLISION, "the pairing key is shared by more than one commit", sha=sha))
        elif k and k in by_key:
            pairs.append((sha, by_key[k]["sha"]))
            claimed.add(by_key[k]["sha"])
        elif orig.get("patch_id") and orig["patch_id"] in upstream:
            skipped.append(sha)
        elif sha in emptied:
            empty.append(sha)
        else:
            refusals.append(_refuse(UNACCOUNTED, "neither replayed, skipped as upstream, nor emptied at a stop", sha=sha))
    for new in replayed:
        if new["sha"] not in claimed:
            refusals.append(_refuse(UNMATCHED_REPLAYED, "a replayed commit matches no original", sha=new["sha"]))
    order = {c["sha"]: i for i, c in enumerate(replayed)}
    walk = [order[n] for _, n in pairs if n in order]
    if walk != sorted(walk):
        refusals.append(_refuse(REORDERED, "the replayed commits are not in the original order"))
    return {"pairs": pairs, "skipped": skipped, "emptied": empty, "refusals": refusals}


# ------------------------------------------------------------------------------------------------ C: only conflicted
def only_conflicted_differ(stage0_before, stage0_after, conflicted):
    """Every index entry that changed from the stop's stage-0 baseline must be in the unmerged set. `stage0_*` are
    `conflict_state.stage0_listing` dicts. A path added, removed or changed outside `conflicted` refuses; paths in
    `conflicted` may differ freely (they have no stage-0 entry before resolving)."""
    allowed = set(conflicted)
    out = []
    for path in sorted(set(stage0_before) | set(stage0_after)):
        if path not in allowed and stage0_before.get(path) != stage0_after.get(path):
            out.append(_refuse(STAGE0_CHANGED, "the index entry differs from the stop's baseline", path=path))
    return out


_PSEUDO = ("old mode ", "new mode ", "new file mode ", "deleted file mode ")


def per_path_multisets(diff_text, suffix=""):
    """`{path+suffix: (Counter of added lines, Counter of removed lines)}` from a `-U0 --no-renames` patch. Hunk
    headers, context and placement are ignored by construction, so a base edit near a hunk cannot change it. A mode
    change and a binary change are recorded as pseudo lines (the `index <old>..<new>` full blob ids of a binary path,
    taken from the log's `--binary --full-index` output, so differing binary content differs); a text path's index
    line is skipped."""
    out, path, added, removed, in_hunk, index = {}, None, None, None, False, ""
    for line in str(diff_text or "").split("\n"):
        if line.startswith("diff --git "):
            path = line.rsplit(" b/", 1)[-1] + suffix
            added, removed = out.setdefault(path, (collections.Counter(), collections.Counter()))
            in_hunk, index = False, ""
        elif path is None:
            continue
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk and line[:1] == "+":
            added[line[1:]] += 1
        elif in_hunk and line[:1] == "-":
            removed[line[1:]] += 1
        elif not in_hunk and line.startswith(_PSEUDO):
            added["\0" + line] += 1
        elif not in_hunk and line.startswith("index ") and "\0" not in line:
            index = line
        elif not in_hunk and line.startswith(("Binary files", "GIT binary patch")):
            added["\0" + line] += 1
            added["\0" + index] += 1
    return out


def multiset_differences(original_diff, replayed_diff, conflicted):
    """Paths OUTSIDE `conflicted` whose added or removed line multiset differs between a paired original and its
    replay (the arguments are `per_path_multisets` results). A path present on one side only differs unless it is
    empty there. A merge's per-parent keys (`path@n`) are compared as their own paths, and a conflicted path is
    matched by its bare name. Known false positive (parks): a base-side rename of a file the unit edits."""
    skip = set(conflicted)
    out = []
    for path in sorted(set(original_diff) | set(replayed_diff)):
        if path.split("@")[0] in skip:
            continue
        empty = (collections.Counter(), collections.Counter())
        if original_diff.get(path, empty) != replayed_diff.get(path, empty):
            out.append(_refuse(MULTISET_CHANGED, "the added or removed lines differ from the original commit", path=path))
    return out


# ------------------------------------------------------------------------------------------------ D: markers
_START = re.compile(r"^<{7}( |$)", re.M)
_MARKER = re.compile(r"^(<{7}|\|{7}|>{7})( |$)", re.M)
_MIDDLE = re.compile(r"^={7}$", re.M)


def marker_count(text):
    """Conflict-marker lines in a blob: `<<<<<<<`, `|||||||`, `>>>>>>>`, and `=======` only when a start marker
    also appears (so a Markdown setext rule is not a marker)."""
    text = str(text or "")
    n = len(_MARKER.findall(text))
    if _START.search(text):
        n += len(_MIDDLE.findall(text))
    return n


def check_markers(resolved_blobs, original_blobs=None):
    """Refuse a resolved blob holding more marker lines than the original commit's own blob (a file that is about
    conflict markers is allowed what it already had). `resolved_blobs` is `{path: text}` for the conflicted paths
    only; `original_blobs` the same paths at the original commit. Known false negative: a language where the
    marker is legitimate text."""
    original_blobs = original_blobs or {}
    return [_refuse(MARKER, "%d marker line(s), %d allowed" % (marker_count(text), marker_count(original_blobs.get(path))),
                    path=path)
            for path, text in sorted(resolved_blobs.items())
            if marker_count(text) > marker_count(original_blobs.get(path))]


_FINDING = re.compile(r"^(?P<path>.+?):(?P<line>\d+): (?P<msg>.*)$")


def whitespace_findings(check_output):
    """`Counter` of `(path, message, source line)` from `git diff --check` text, line numbers dropped so a finding
    that merely moved is the same finding."""
    found, lines = collections.Counter(), str(check_output or "").split("\n")
    for i, line in enumerate(lines):
        m = _FINDING.match(line)
        if m:
            src = lines[i + 1] if i + 1 < len(lines) and lines[i + 1][:1] in "+-" else ""
            found[(m.group("path"), m.group("msg"), src)] += 1
    return found


def new_whitespace_findings(resolved_check, original_check):
    """Findings in the resolved commit's `git diff --check` that the original commit's own `--check` did not have.
    Scoped this way because 6 of 151 non-merge commits on one real history add `--check` findings, so a bare
    `--check` would park about 4 percent of ordinary commits."""
    fresh = whitespace_findings(resolved_check) - whitespace_findings(original_check)
    return [_refuse(WHITESPACE, msg, path=path) for (path, msg, _src), _n in sorted(fresh.items())]


# ------------------------------------------------------------------------------------------------ E: tests
_TEST_PATH = re.compile(r"(^|/)tests?/|(^|/)test_[^/]*$|_test\.|\.test\.|\.spec\.")
_COUNTED = (re.compile(r"^\s*(async\s+)?def\s+test_\w*"), re.compile(r"^\s*class\s+Test\w*"),
            re.compile(r"^\s*assert\b"), re.compile(r"\bself\.assert\w*\("), re.compile(r"\bpytest\.raises\b"),
            re.compile(r"\.assert_\w+\("))
_SKIP = re.compile(r"pytest\.mark\.(skip|skipif|xfail)\b|unittest\.skip\w*|\bpytest\.(skip|xfail)\(|@skip\w*\b")


def is_test_path(path):
    """A broad test shape (`tests/`, `test_`, `_test.`, `.test.`, `.spec.`), wider than the tamper scan's own, which
    is not widened."""
    return bool(_TEST_PATH.search(str(path or "")))


def count_tests(text):
    """`(tests and assertions, skip markers)` in a Python source text. It executes nothing."""
    total = skips = 0
    for line in str(text).split("\n"):
        total += sum(1 for rx in _COUNTED if rx.search(line))
        skips += 1 if _SKIP.search(line) else 0
    return total, skips


def check_tests(path, base, ours, theirs, resolved):
    """The fail-closed counter for ONE conflicted path. Blobs are str, or None for an absent stage. A path that is
    not a test file passes; a non-Python test file, an unreadable (non-str, non-None) blob or an absent resolved
    blob refuses. Requires `resolved >= ours + theirs - base`, and no skip marker beyond the larger of ours and
    theirs. Known false positives: identical additions on both sides, a test moved between files. Known false
    negatives: a weakened assertion that keeps the count, a non-test file with logic."""
    if not is_test_path(path):
        return []
    if not str(path).endswith(".py"):
        return [_refuse(TEST_LANGUAGE, "only Python test files are counted", path=path)]
    blobs = {"base": base, "ours": ours, "theirs": theirs, "resolved": resolved}
    if any(v is not None and not isinstance(v, str) for v in blobs.values()) or resolved is None:
        return [_refuse(TEST_UNREADABLE, "a stage or the resolved blob could not be read as text", path=path)]
    c = {name: count_tests(v or "") for name, v in blobs.items()}
    out = []
    if c["resolved"][0] < c["ours"][0] + c["theirs"][0] - c["base"][0]:
        out.append(_refuse(TEST_LOSS, "resolved %d < ours %d + theirs %d - base %d" % (
            c["resolved"][0], c["ours"][0], c["theirs"][0], c["base"][0]), path=path))
    if c["resolved"][1] > max(c["ours"][1], c["theirs"][1]):
        out.append(_refuse(TEST_SKIP, "a skip marker was added beyond both sides", path=path))
    return out


# ------------------------------------------------------------------------------------------------ append-only union (#1515)

_DATED = re.compile(r"^## (\d{4}-\d{2}(-\d{2})?)\b")
_BULLET = re.compile(r"^\s*[-*] \S")


def would_drop(resolved, *sides):
    """Non-blank lines of any side that the resolved text holds fewer times than that side does (empty = none lost)."""
    have = collections.Counter(ln for ln in resolved.split("\n") if ln.strip())
    lost = []
    for side in sides:
        for ln, n in collections.Counter(x for x in side.split("\n") if x.strip()).items():
            if have[ln] < n and ln not in lost:
                lost.append(ln)
    return lost


def _added(whole, part):
    """Lines of `whole` left when `part` is matched as an in-order subsequence (a pure addition), else None."""
    extras, j = [], 0
    for ln in whole:
        if j < len(part) and ln == part[j]:
            j += 1
        else:
            extras.append(ln)
    return extras if j == len(part) else None


def _trim(lines):
    lines = list(lines)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _merge_lines(base, ours, theirs, per_line):
    """ours, then what theirs added to base that ours lacks -> lines, or None when either side edited or removed."""
    if _added(ours, base) is None:
        return None
    extras = _added(theirs, base)
    if extras is None:
        return None
    head = _trim(ours)
    if per_line:
        extras = [ln for ln in extras if not ln.strip() or ln not in head]
    elif _trim(extras) and "\n".join(_trim(extras)) in "\n".join(head):
        extras = []
    return head + (_trim(extras) if any(x.strip() for x in extras) else [])


def _sections(lines):
    """{key: lines} in file order (key "" is the preamble), or None for a repeated dated heading."""
    out, key = {"": []}, ""
    for ln in lines:
        hit = _DATED.match(ln)
        if hit:
            key = ln.strip() if hit.group(2) else hit.group(1)
            if key in out:
                return None
            out[key] = []
        out[key].append(ln)
    return out


def _date_of(key):
    """The `YYYY-MM` or `YYYY-MM-DD` a section key leads with (#1949: a day-form key is the whole heading line)."""
    return key.removeprefix("## ")[:10] if key.startswith("## ") else key


def _without_own_heading(key, sb, so, st):
    """#1968: theirs' lines of section `key`; when both sides added the same new day heading, theirs' copy of the
    heading (and the blank lines after it) is left out so the merged section holds ONE heading and both bullet sets."""
    mine = st.get(key, [])
    if key.startswith("## ") and key not in sb and key in so and mine and mine[0].strip() == key:
        mine = list(mine[1:])
        while mine and not mine[0].strip():
            mine.pop(0)
    return mine


def line_union(base, ours, theirs):
    """#1968: the generic line union of two pure additions to `base` -> the text, or None. Base order is kept, ours
    additions stay where ours put them, then the lines theirs added that ours lacks; a duplicate added line collapses.
    None when either side edited or removed a base line, or when the result would drop a line of either side."""
    b, o, t = (str(x).split("\n") for x in (base, ours, theirs))
    merged = _merge_lines(b, o, t, True)
    if merged is None:
        return None
    text = "\n".join(merged) + "\n"
    return None if would_drop(text, ours, theirs) else text


def append_union(base, ours, theirs):
    """The union of two pure appends to a dated `## YYYY-MM` log (sections in date order, lines unioned per section)
    or a bullet-list file (ours first, then theirs' new items) -> the text, or None. None is every other shape and
    any edit or removal on either side: the caller falls through exactly as before. Refuses a result that
    `would_drop` finds a line of either side missing from."""
    b, o, t = (str(x).split("\n") for x in (base, ours, theirs))
    if not any(ln.strip() for ln in b):
        return None
    body = [ln for side in (b, o, t) for ln in side if ln.strip()]
    if all(_BULLET.match(ln) for ln in body):
        merged = _merge_lines(b, o, t, True)
        text = None if merged is None else "\n".join(merged) + "\n"
    elif any(_DATED.match(ln) for ln in body):
        sb, so, st = _sections(b), _sections(o), _sections(t)
        if None in (sb, so, st) or not set(sb) <= set(so) & set(st):
            return None
        keys = list(dict.fromkeys(k for seq in (sb, so, st) for k in seq if k))
        for seq in (sb, so, st):
            order = [_date_of(k) for k in seq if k]
            if len(order) > 1:
                keys.sort(key=_date_of, reverse=order == sorted(order, reverse=True))
                break
        else:
            keys.sort(key=_date_of)
        parts = []
        for key in [""] + keys:
            got = _merge_lines(sb.get(key, []), so.get(key, []), _without_own_heading(key, sb, so, st), False)
            if got is None:
                return None
            parts.append(got)
        text = "\n\n".join("\n".join(p) for p in parts if p) + "\n"
    else:
        return None
    if text is None or text == ours or would_drop(text, ours, theirs):
        return None
    return text
