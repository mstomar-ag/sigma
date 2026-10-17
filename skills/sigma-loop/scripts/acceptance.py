"""P1 acceptance record, shared by verify, publication, and retrospective review.

    python3 skills/sigma-loop/scripts/acceptance.py record .sdlc <goal> [--draft file]

No background work. Capture is explicit; only drafted GitHub criteria cause a comment.
The file is create-only. Existing records win over later issue edits. Protocol bounds
keep review context short; these are document limits, not host resource quotas.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import sys
import tempfile

MAX_BYTES = 32768


def _load(name):
    spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def path(sdlc_dir, goal):
    raw = str(goal)
    stem = pathlib.Path(raw).stem if raw.endswith('.md') else raw
    reason = _load('state').unsafe_goal_reason(stem)
    if reason:
        raise ValueError('unsafe acceptance goal: ' + reason)
    return pathlib.Path(sdlc_dir) / 'acceptance' / (stem + '.md')


def _text(file):
    with pathlib.Path(file).open('rb') as handle:
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError('acceptance record exceeds 32 KiB')
    return data.decode('utf-8')


def criteria(text):
    """Read a real level-two Done when section; fenced examples do not count."""
    found, fenced, lines, separated, footer = False, None, [], False, False
    for line in text.splitlines():
        fence = re.match(r'^\s*(`{3,}|~{3,})', line)
        if fence:
            char = fence.group(1)[0]
            fenced = None if fenced == char else char if fenced is None else fenced
            continue
        if fenced:
            continue
        if re.match(r'^##\s+', line):
            if found:
                break
            found = bool(re.fullmatch(r'##\s+Done when\s*', line, re.I))
            continue
        if found and lines and not line.strip():
            separated = True
        if found and line.strip():
            item = re.fullmatch(r'\s*[-*]\s+(?:\[[ xX]\]\s+)?(.+?)\s*', line)
            if not item:
                if lines and separated:
                    footer = True  # allow issue metadata, but inspect the rest for lost criteria
                    continue
                raise ValueError('Done when must contain single-line checklist statements')
            if footer:
                raise ValueError('checklist item after footer paragraph; keep all acceptance criteria together')
            if re.fullmatch(r'\[[ xX]\]', item.group(1)):
                raise ValueError('acceptance statement is empty')
            lines.append(item.group(1))
            separated = False
    if not found:
        return None
    if not 3 <= len(lines) <= 7 or any(len(x) > 1024 for x in lines):
        raise ValueError('acceptance requires 3–7 statements of at most 1024 characters each')
    return lines


def read(sdlc_dir, goal):
    file = path(sdlc_dir, goal)
    return parse(_text(file), file.stem)


def parse(text, goal_stem):
    """Validate local or committed bytes through the same bounded record contract."""
    if len(text.encode('utf-8')) > MAX_BYTES:
        raise ValueError('acceptance record exceeds 32 KiB')
    match = re.fullmatch(r'---\n(.*?)\n---\n(.*)', text, re.S)
    if not match:
        raise ValueError('acceptance metadata is missing')
    try:
        metadata = dict((key, json.loads(value)) for key, value in
                        (line.split(': ', 1) for line in match.group(1).splitlines()))
    except (ValueError, TypeError) as exc:
        raise ValueError('acceptance metadata is malformed') from exc
    if metadata.get('kind') != 'acceptance' or metadata.get('goal') != goal_stem:
        raise ValueError('acceptance kind/goal does not match this goal')
    command = metadata.get('verify_command')
    if command is not None and (not isinstance(command, str) or not command.strip() or '\0' in command):
        raise ValueError('acceptance verify_command must be a nonempty string or null')
    items = criteria(match.group(2))
    if items is None:
        raise ValueError('acceptance Done when is missing')
    return {'text': text, 'criteria': items, 'verify_command': command}


def digest(sdlc_dir, goal):
    try:
        return hashlib.sha256(read(sdlc_dir, goal)['text'].encode('utf-8')).hexdigest()
    except FileNotFoundError:
        return None


def refusal(sdlc_dir, goal):
    try:
        read(sdlc_dir, goal)
    except (OSError, ValueError) as exc:
        return ('acceptance record missing or invalid; run `acceptance.py record .sdlc <goal>` '
                'during P1 GOAL before code: %s (nothing pushed)' % exc)
    return None


def record(sdlc_dir, goal, config=None, source=None, draft=None, verify_command=None):
    file = path(sdlc_dir, goal)
    if file.exists():
        read(sdlc_dir, goal)  # never overwrite intent, even on a retry after the issue changes
        return file
    config = config if config is not None else _load('state').load_config(sdlc_dir)
    source = source or _load('sources').get_source(sdlc_dir, config)
    issue = source.fetch_title_body(goal)
    body = issue.get('body', '')
    if not body.strip():
        raise ValueError('goal body unavailable; restore source access before recording acceptance')
    try:
        items = criteria(body)
    except ValueError as exc:
        if draft is None:           # #1811: an invalid body list is the thing to fix, or a draft replaces it
            raise ValueError('%s; edit the issue body, or pass --draft with 3–7 criteria' % exc)
        items = None
    drafted = items is None
    if drafted:
        if draft is None:
            raise ValueError('no Done when section; draft 3–7 criteria in a file and pass --draft')
        items = criteria(_text(draft))
        if items is None:
            raise ValueError('draft requires a Done when section')
    if verify_command is None and str(goal).endswith('.md'):
        verify_command = _load('frontmatter').get(_text(goal), 'verify_command')
        if verify_command and not verify_command.strip().strip('\'"').strip():
            verify_command = None
    metadata = {'kind': 'acceptance', 'goal': file.stem, 'verify_command': verify_command}
    text = ('---\n' + '\n'.join(k + ': ' + json.dumps(v, ensure_ascii=False) for k, v in metadata.items())
            + '\n---\n## Done when\n' + ''.join('- [ ] ' + item + '\n' for item in items))
    if len(text.encode('utf-8')) > MAX_BYTES:
        raise ValueError('acceptance record exceeds 32 KiB')
    if verify_command is not None and (not isinstance(verify_command, str) or not verify_command.strip() or '\0' in verify_command):
        raise ValueError('acceptance verify_command must be a nonempty string or null')
    sha = hashlib.sha256(text.encode('utf-8')).hexdigest()
    if drafted and not str(goal).endswith('.md'):
        comment = '<!-- sigma:acceptance ' + sha + ' -->\n' + text
        comments = source.fetch_comments_strict(goal)['comments']
        if not isinstance(comments, list):
            raise ValueError('could not read acceptance comment history')
        if not any(c.get('body') == comment for c in comments if isinstance(c, dict)):
            source.note(goal, comment)  # REST re-POST on 5xx/transport only; an ambiguous ack may duplicate
    file.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.acceptance-', dir=str(file.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, file)  # atomic create-only, refuse unsupported filesystems
        except FileExistsError:
            if read(sdlc_dir, goal)['text'] != text:
                raise ValueError('concurrent acceptance record differs; retain original intent')
    finally:
        os.unlink(temporary)
    _load('ledger').safe_append(sdlc_dir, 'acceptance', goal, config=config,
                               ref='acceptance/' + file.name, acceptance_sha256=sha)
    return file


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('verb', choices=['record'])
    parser.add_argument('sdlc_dir')
    parser.add_argument('goal')
    parser.add_argument('--draft')
    parser.add_argument('--verify-command')
    args = parser.parse_args(argv)
    try:
        print(record(args.sdlc_dir, args.goal, draft=args.draft, verify_command=args.verify_command))
    except (OSError, ValueError, RuntimeError) as exc:
        print('acceptance.py: REFUSED: ' + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
