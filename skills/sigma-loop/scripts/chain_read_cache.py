"""chain_read_cache.py -- a short-lived on-disk cache of what Chain's plan reads of one member. Library only; no main.

A plan reads each member's body and newest comments (`chain_waits._read`: one issue GET and up to two comment pages,
about three requests). Plans of neighbouring chains in one session read the SAME members again. This keeps that answer
in `.sdlc/state/chain-reads.json` so the second plan does not ask GitHub for what the first just read.

What makes it safe:
  * the key is the issue number AND its exact `updated_at`, which the caller takes from the issue it already holds (the
    live closure) or from the stored map's node. A hit needs the cached stamp to equal the caller's, both non-empty,
    and the entry to be at most MAX_AGE_SECONDS old. Anything else reads fresh and overwrites the entry. A new comment
    or an edit moves `updated_at`, so a changed issue is never answered from here. With no stamp known there is no hit;
  * it is read-only toward GitHub: nothing on this path writes there, so no invalidation-on-write exists or is needed;
  * it is bounded: entries older than MAX_AGE_SECONDS are not written back, at most MAX_ENTRIES are kept, and the file
    never exceeds MAX_BYTES; the oldest entries go first;
  * it fails open: a missing, corrupt, wrong-version or unwritable file reads as an empty cache and is never an error;
  * the write is atomic (a temp file in the same directory, then `os.replace`). Several plan processes may race: the
    last writer wins, which costs a missed hit, never a wrong answer.

Not measured here: the saving on a live repository; the offline counting fake is the only count.
"""
import json
import os
import tempfile

VERSION = 1
MAX_AGE_SECONDS = 900
MAX_ENTRIES = 400
MAX_BYTES = 8 * 1024 * 1024
FILE = "chain-reads.json"


def _path(sdlc_dir):
    return os.path.join(str(sdlc_dir), "state", FILE)


def load(sdlc_dir, now):
    """-> the store (never raises). An unreadable, corrupt or wrong-version file is an empty store."""
    store = {"entries": {}, "now": now, "dirty": False}
    try:
        with open(_path(sdlc_dir), encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and data.get("version") == VERSION and isinstance(data.get("entries"), dict):
            for k, e in data["entries"].items():
                if isinstance(e, dict) and isinstance(e.get("saved_at"), (int, float)) and isinstance(e.get("updated_at"), str) \
                        and isinstance(e.get("issue"), dict) and isinstance(e.get("comments"), list):
                    store["entries"][str(k)] = e
    except Exception:                      # noqa: BLE001 - fail open: nothing here may fail a plan
        store["entries"] = {}
    return store


def get(store, number, updated_at):
    """-> (issue, comments, maybe_older) or None. A hit needs equal, non-empty stamps and an entry of at most
    MAX_AGE_SECONDS."""
    if not updated_at or not isinstance(updated_at, str):
        return None
    e = store["entries"].get(str(number))
    if not e or not e.get("updated_at") or e["updated_at"] != updated_at:
        return None
    if store["now"] - e["saved_at"] > MAX_AGE_SECONDS:
        return None
    return e["issue"], e["comments"], bool(e.get("maybe_older"))


def put(store, number, updated_at, got, now):
    """Keep `got` (issue, comments, maybe_older) for `number` at `updated_at`. No stamp: nothing is kept."""
    if not updated_at or not isinstance(updated_at, str):
        return
    issue, comments, maybe_older = got
    store["entries"][str(number)] = {"updated_at": updated_at, "saved_at": now, "issue": issue,
                                     "comments": comments, "maybe_older": bool(maybe_older)}
    store["dirty"] = True


def save(sdlc_dir, store):
    """Write the store, newest first within the bounds. Never raises; nothing is written when nothing changed."""
    try:
        if not store.get("dirty"):
            return
        live = [(k, e) for k, e in store["entries"].items() if store["now"] - e["saved_at"] <= MAX_AGE_SECONDS]
        live.sort(key=lambda kv: kv[1]["saved_at"], reverse=True)
        kept, size = {}, 64
        for k, e in live[:MAX_ENTRIES]:
            n = len(json.dumps(e)) + len(k) + 8
            if size + n > MAX_BYTES:
                continue
            kept[k] = e
            size += n
        path = _path(sdlc_dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"version": VERSION, "entries": kept}, fh)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception:                      # noqa: BLE001 - fail open
        return
