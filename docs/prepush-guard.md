# Pre-push guard

`tools/prepush_scan.py` refuses a push whose new commits carry a denied pattern in a commit
message or an added line. It scans only commits not reachable from any remote-tracking ref, so a
merge of the default branch never drags the upstream commits into the scan. Its output says how
many commits were scanned and skipped. Fetch first: a stale remote-tracking ref widens the scan,
never narrows it. Any failing git command (for example a remote sha not fetched locally) makes
the guard exit 2 with a one-line refusal, never a clean pass.

## Patterns file

One Python regular expression per line, case-insensitive; blank lines and `#` lines are skipped.
Keep the file outside every repository; its contents are yours and are never committed. A neutral
example:

```
# example patterns
/home/[a-z]+/
ghp_[A-Za-z0-9]{20,}
-----BEGIN [A-Z ]*PRIVATE KEY-----
```

A pattern that does not compile, or matches the empty string, is refused by line number; so is an
empty file. A hit is reported as commit, surface and pattern line number, never the matched text.

## Install

In the repository to protect, create `.git/hooks/pre-push` (mode 755) holding:

```sh
#!/bin/sh
exec python3 /path/to/sigma/tools/prepush_scan.py --patterns /path/to/patterns.txt "$@"
```

Exit codes: 0 clean, 1 a hit (the push is refused), 2 a usage or patterns refusal (also refused).
`--base REF` scans against one ref instead of every remote-tracking ref. The tool only reads git
state; skipping it needs `git push --no-verify`, which is the operator's choice.
