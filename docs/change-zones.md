# Change zones

A change zone tells an agent how carefully to touch a file: leave legacy code almost alone, and
design modern code freely. You declare the zones; **nothing infers them**, and `/sigma-vision` may
only suggest candidate paths, never write the key.

## The key

`design.zones` in `.sdlc/config.json` is a list of entries, absent by default:

```json
{ "design": { "zones": [
  { "glob": "src/*",         "zone": "legacy" },
  { "glob": "src/payments/*", "zone": "modern" }
] } }
```

Each entry has exactly two keys, `glob` (a non-empty string) and `zone` (`legacy` or `modern`).
A malformed entry is refused by name (`design.zones[1]`), exit 2, empty stdout.

- **Glob semantics.** `fnmatch` on the repo-relative path with forward slashes, case-sensitive
  (`fnmatch.fnmatchcase`). `*` crosses `/` (so `src/*` matches `src/a/b/c.py`), `?` is one character,
  `[abc]` a set.
- **Last match wins.** Entries are tried from the end; the zone of the last entry that matches is
  used, which is the last match. Put the broad entry first and the exceptions after it. No match is `undeclared`.

## Print each file's zone and rule

```
python3 skills/sigma-loop/scripts/zones.py classify src/legacy/a.py
```

One row per path, tab-separated: `<path>`, `<zone>`, `<state>`, `<rule>`. The zone is `legacy`,
`modern`, `undeclared`, or `unreachable` (the config could not be read, which is not the same as
undeclared). The state is `new` when the path is absent at the fork point of the goal branch (the
merge-base with `<remote>/<base>` from the goal's work record; `--base REF` pins another ref),
`existing` when present, and `unknown` when the fork point cannot be resolved (no record, detached
HEAD, shallow history, unresolvable ref, a failing `git cat-file --batch-check`, or a path inside a
submodule). A rename counts as `new`. A directory argument is classified by its own name only.

The rule printed, in this precedence:

| Case | Rule |
|---|---|
| state `unknown` | `unknown: merge-base unresolved; apply no zone rule` |
| zone `unreachable` | `unreachable: config unreadable; apply no zone rule` |
| any path that is `new`, or zone `modern` | `follow sound design: structure for the forces the goal names` |
| `legacy` and `existing` | `legacy: change only what the goal needs; extend by sprout or wrap at one seam` |
| `undeclared` and `existing` | `follow the surrounding code` |

An unreachable config prints ahead of the unknown fallback. Paths may be relative (joined to the
current directory), absolute, or contain `./`; a path outside the checkout, or one containing a
newline, carriage return or tab (the output is tab-separated), is refused; so is a `--base` value
that starts with a dash. A backslash is an ordinary name byte on POSIX, never a separator. A path that starts with a dash needs `--`: `zones.py classify -- -odd.py`.
It is read-only apart from the kit-wide script-time record, runs a fixed number of git
processes plus one `ls-tree` per 200 paths the batch reports missing (git 2.49 reports a gitlink whose
target commit is absent as missing too, so the tree is asked, #1853), and uses no network and no `gh`.

Measured (macOS, git 2.49, Python 3.12, `--base main`, one run each on a busy shared machine): 1,000 paths
(all new, each in its own directory, in a one-file repo) take 0.27 s wall, and 20,000 such paths take
5.2 s, of which the `ls-tree` calls (200 of them, since each path and its directory is a missing key) are
about half; the earlier figures, 0.20 s and 2.6 s, were taken on git 2.55 before that call existed and are
not comparable. Wall time grows roughly linearly with the path count; a de-duplication that scanned a
list instead of a hash made the 20,000 case 12.6 s. Not measured: a large real repository, a networked
filesystem, or more than 20,000 paths.

## Limits of the `new` reading

Newness is a byte-for-byte lookup of the path you pass in git's tree at the fork point. Two
spellings of a file that is tracked therefore print `new` although the file exists:

- **Unicode normalisation (macOS).** macOS can open a file by a different Unicode spelling than the
  one git stored, for example the decomposed (NFD) spelling of a tracked precomposed (NFC) name
  such as `cafe` plus a combining accent for `café`. Git does not equate them, so the row prints `new`.
- **Case (case-insensitive filesystems, the macOS default).** `readme.py` opens `Readme.py` on such
  a filesystem, but git tracks `Readme.py`, so the row for `readme.py` prints `new`.

In both cases the safe reading is that `new` for such a spelling may be wrong; pass the spelling git
tracks (`git ls-files` shows it). The zone glob match is case-sensitive for the same reason. This is
a documented limit, not handled; a test pins the current behaviour so a change is noticed.

## Sprout and wrap

In legacy code, extend by **sprout** or **wrap** at one seam rather than editing through it.

- **Sprout:** add a new function or module beside the legacy code and call it from one seam, for
  example a new `billing/tax_v2.py` called from the single line in `billing/legacy_calc.py`.
- **Wrap:** leave the legacy function untouched and put a new function around it that handles the
  new case, for example `price_with_discount()` calling the legacy `price()`.

## Per-machine caveat

`.sdlc/config.json` is gitignored, so the map is **per-machine**: another clone has its own copy, or
none, and reads `undeclared` for everything. A stale map misclassifies silently, so review it when
the legacy boundary moves. Nothing infers zones from the code or the history; if you want them
everywhere, declare them on every machine.
