"""#272: record intent before code; execute the same intent before publication."""
import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'skills/sigma-loop/scripts'


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CRITERIA = '## Done when\n- [ ] First works.\n- [ ] Second refuses bad input.\n- [ ] Third recovers.\n'


class Source:
    def __init__(self, body=CRITERIA):
        self.body, self.comments, self.posts = body, [], 0

    def fetch_title_body(self, goal):
        return {'title': 'Goal', 'body': self.body}

    def fetch_comments_strict(self, goal):
        return {'comments': self.comments}

    def note(self, goal, text):
        self.posts += 1
        self.comments.append({'body': text})


def capture(tmp_path, command=None, source=None, allow_shell_commands=False):
    if allow_shell_commands:
        subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
        subprocess.run(['git', '-C', str(tmp_path), 'config', '--local',
                        'sigma.allowRepositoryShellCommands', 'true'], check=True)
    sdlc = tmp_path / '.sdlc'
    sdlc.mkdir(exist_ok=True)
    a = load('acceptance')
    a.record(sdlc, '42', source=source or Source(), config={}, verify_command=command)
    return sdlc, a


def test_enforced_pr_refuses_missing_acceptance_before_push(tmp_path, monkeypatch):
    work = load('work')
    monkeypatch.setattr(work, '_record', lambda *a: {'worktree': str(tmp_path), 'remote': 'origin', 'base': 'main', 'branch': 'sdlc/42'})
    calls = []
    result = work.pr(tmp_path / '.sdlc', {'verify': {'enforce': True}}, '42',
                     run=lambda cwd, argv: calls.append(argv) or '1')
    assert 'acceptance' in result.lower()
    assert not calls


def test_record_is_immutable_and_short(tmp_path):
    source = Source()
    sdlc, a = capture(tmp_path, source=source)
    before = (sdlc / 'acceptance/42.md').read_bytes()
    source.body = 'Changed issue without criteria'
    a.record(sdlc, '42', source=source, config={})
    assert (sdlc / 'acceptance/42.md').read_bytes() == before
    assert a.read(sdlc, '42')['criteria'] == ['First works.', 'Second refuses bad input.', 'Third recovers.']
    assert source.posts == 0


@pytest.mark.parametrize('body', ['## Done when\n- Only one\n', CRITERIA + '- [ ] x\n' * 5, '## Done when\n- [ ] ' + 'x' * 1025])
def test_invalid_criteria_refuse_without_partial_record(tmp_path, body):
    a = load('acceptance')
    with pytest.raises(ValueError):
        a.record(tmp_path, '42', source=Source(body), config={})
    assert not (tmp_path / 'acceptance/42.md').exists()


def test_draft_comment_survives_lost_ack_retry(tmp_path):
    a = load('acceptance')
    source = Source('No criteria here')
    draft = tmp_path / 'draft.md'
    draft.write_text(CRITERIA)
    note = source.note
    def lost_ack(goal, text):
        note(goal, text)
        raise RuntimeError('ack lost')
    source.note = lost_ack
    with pytest.raises(RuntimeError):
        a.record(tmp_path, '42', source=source, config={}, draft=draft)
    assert not (tmp_path / 'acceptance/42.md').exists()
    source.note = note
    a.record(tmp_path, '42', source=source, config={}, draft=draft)
    assert source.posts == 1


def test_draft_comment_gh_api_error_propagates_and_writes_no_record(tmp_path):
    """#895 slice 8: `note()` raises `GhApiError` at once on a 4xx (no retry); the record must not be written."""
    a = load('acceptance')
    source = Source('No criteria here')
    draft = tmp_path / 'draft.md'
    draft.write_text(CRITERIA)
    err = load('gh_api').GhApiError('gh api failed', 'gh: Not Found (HTTP 404)', 404, 'not_found')
    def refuse(goal, text):
        raise err
    source.note = refuse
    with pytest.raises(type(err)):
        a.record(tmp_path, '42', source=source, config={}, draft=draft)
    assert not (tmp_path / 'acceptance/42.md').exists() and source.posts == 0


@pytest.mark.parametrize('phase', ['pr-review', 'retro'])
def test_brief_contains_record_verbatim(tmp_path, phase):
    sdlc, a = capture(tmp_path)
    text = (sdlc / 'acceptance/42.md').read_text()
    brief = load('review_context').brief(sdlc, '42', phase, repo_root=str(tmp_path), source=Source('Current mutable issue'))
    assert text in brief
    assert 'each criterion' in brief


def test_command_resolution_is_shared_with_merge_gate(tmp_path):
    sdlc, a = capture(tmp_path, 'echo goal')
    state = load('state')
    config = {'verify': {'command': 'echo repo'}}
    assert state.declared_verify_commands('42', config, sdlc) == ['echo repo', 'echo goal']
    assert state.verify_required(config, '42', sdlc)
    assert json.loads(state.declared_verify_command('42', config, sdlc)) == ['echo repo', 'echo goal']


def test_changed_acceptance_invalidates_green_evidence(tmp_path):
    sdlc, a = capture(tmp_path)
    state = load('state')
    state.start_run(sdlc)
    ev = state.evidence_path(sdlc, '42')
    ev.parent.mkdir(parents=True, exist_ok=True)
    import time
    ev.write_text(json.dumps({'exit': 0, 'at': time.time(), 'acceptance_sha256': a.digest(sdlc, '42')}))
    assert state.done_refusal(sdlc, '42') is None
    p = sdlc / 'acceptance/42.md'
    p.write_text(p.read_text().replace('First works.', 'Different intent.'))
    assert 'acceptance' in state.done_refusal(sdlc, '42')


def test_documented_cli_local_capture(tmp_path):
    sdlc = tmp_path / '.sdlc'
    sdlc.mkdir()
    (sdlc / 'config.json').write_text('{}')
    goal = tmp_path / 'goal.md'
    goal.write_text('---\nverify_command: "exit 0"\n---\n' + CRITERIA)
    cmd = [sys.executable, str(SCRIPTS / 'acceptance.py'), 'record', str(sdlc), str(goal)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert load('acceptance').read(sdlc, str(goal))['verify_command'] == 'exit 0'


def test_contract_acceptance_entry_and_golden(tmp_path):
    ledger = load('ledger')
    assert 'acceptance' in ledger.KINDS
    assert (ROOT / 'contract/VERSION').read_text().strip() == '1.3.0'
    entry = ledger.append(tmp_path, {'ledger': {'enabled': True, 'actor': 'test'}},
                          'acceptance', '42', ref='acceptance/42.md', acceptance_sha256='a' * 64)
    assert entry['acceptance_sha256'] == 'a' * 64
    proc = subprocess.run([sys.executable, str(ROOT / 'contract/validate.py'),
                           str(ROOT / 'contract/golden/acceptance/42.md')], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    with pytest.raises(ValueError, match='acceptance_sha256'):
        ledger.append(tmp_path, {'ledger': {'enabled': True}}, 'acceptance', '42', acceptance_sha256='bad')


@pytest.mark.parametrize('repo,goal,expected,lines', [
    ('set -e; false; echo unexpected >> ran', 'echo goal >> ran', 1, []),
    ('echo repo >> ran; exit 0', 'echo goal >> ran; exit 7', 7, ['repo', 'goal']),
    ('echo repo >> ran', 'echo goal >> ran', 0, ['repo', 'goal']),
])
def test_verify_preserves_each_commands_shell_semantics(tmp_path, repo, goal, expected, lines):
    sdlc, a = capture(tmp_path, goal, allow_shell_commands=True)
    (sdlc / 'config.json').write_text(json.dumps({'verify': {'command': repo}}))
    loop = load('loop')
    assert loop.verify_goal(sdlc, '42') == (1 if expected else 0)
    evidence = json.loads(load('state').evidence_path(sdlc, '42').read_text())
    assert evidence['exit'] == expected
    assert evidence['acceptance_sha256'] == a.digest(sdlc, '42')
    assert ((tmp_path / 'ran').read_text().splitlines() if (tmp_path / 'ran').exists() else []) == lines


@pytest.mark.parametrize('phase', ['pr-review', 'retro'])
def test_committed_acceptance_reaches_review_from_main(tmp_path, phase):
    def git(*args):
        return subprocess.run(['git', *args], cwd=tmp_path, check=True, capture_output=True, text=True)
    git('init', '-q', '-b', 'main')
    git('config', 'user.name', 'Test')
    git('config', 'user.email', 'test@example.invalid')
    git('commit', '--allow-empty', '-qm', 'base')
    git('checkout', '-qb', 'sdlc/42')
    sdlc, a = capture(tmp_path)
    original = a.read(sdlc, '42')['text']
    git('add', '.sdlc/acceptance/42.md')
    git('commit', '-qm', 'acceptance')
    git('checkout', '-q', 'main')
    brief = load('review_context').brief(sdlc, '42', phase, repo_root=str(tmp_path), source=Source())
    assert original in brief


def test_done_when_allows_issue_metadata_after_checklist(tmp_path):
    source = Source(CRITERIA + '\nPart of epic #257.\n**Blocked by:** #258\n')
    sdlc, a = capture(tmp_path, source=source)
    assert len(a.read(sdlc, '42')['criteria']) == 3


@pytest.mark.parametrize('enforce,present', [(True, True), (False, False)])
def test_pr_gate_allows_valid_record_or_enforcement_opt_out(tmp_path, monkeypatch, enforce, present):
    work = load('work')
    sdlc = capture(tmp_path)[0] if present else tmp_path / '.sdlc'
    monkeypatch.setattr(work, '_record', lambda *a: {'worktree': str(tmp_path), 'remote': 'origin', 'base': 'main', 'branch': 'sdlc/42'})
    monkeypatch.setattr(work, '_push_refused', lambda *a: None)
    calls = []
    result = work.pr(sdlc, {'verify': {'enforce': enforce}}, '42',
                     run=lambda cwd, argv: calls.append(argv) or 'dirty')
    assert 'uncommitted' in result
    assert len(calls) == 1


def test_atomic_create_failure_leaves_no_record_or_temporary(tmp_path, monkeypatch):
    a = load('acceptance')
    def unsupported(*args):
        raise OSError('hard links unsupported')
    monkeypatch.setattr(a.os, 'link', unsupported)
    with pytest.raises(OSError, match='unsupported'):
        a.record(tmp_path, '42', config={}, source=Source())
    assert list((tmp_path / 'acceptance').iterdir()) == []


def test_failed_draft_history_read_cannot_publish(tmp_path):
    a = load('acceptance')
    source = Source('No criteria')
    def failed(goal):
        raise RuntimeError('offline')
    source.fetch_comments_strict = failed
    draft = tmp_path / 'draft.md'
    draft.write_text(CRITERIA)
    with pytest.raises(RuntimeError, match='offline'):
        a.record(tmp_path, '42', config={}, source=source, draft=draft)
    assert source.posts == 0
    assert not (tmp_path / 'acceptance/42.md').exists()


def test_paragraph_cannot_hide_later_acceptance_criteria(tmp_path):
    source = Source(CRITERIA + '\nFailure handling:\n- Interrupted writes recover without data loss.\n')
    with pytest.raises(ValueError, match='after'):
        capture(tmp_path, source=source)


def test_empty_checkboxes_are_not_acceptance_statements(tmp_path):
    with pytest.raises(ValueError):
        capture(tmp_path, source=Source('## Done when\n- [ ]\n- [ ]\n- [ ]\n'))


def test_a_valid_draft_is_recorded_when_the_body_list_is_invalid(tmp_path):
    """#1811: the refusal says to pass --draft, so --draft must work when the body's own list is short."""
    a = load('acceptance')
    source = Source('## Done when\n- [ ] Only one\n')
    draft = tmp_path / 'draft.md'
    draft.write_text(CRITERIA)
    a.record(tmp_path, '42', source=source, config={}, draft=draft)
    assert a.read(tmp_path, '42')['criteria'] == ['First works.', 'Second refuses bad input.', 'Third recovers.']
    assert source.posts == 1


def test_an_invalid_body_list_without_a_draft_names_the_body_as_the_thing_to_fix(tmp_path):
    a = load('acceptance')
    with pytest.raises(ValueError) as err:
        a.record(tmp_path, '42', source=Source('## Done when\n- [ ] Only one\n'), config={})
    assert 'issue body' in str(err.value) and '--draft' in str(err.value)
    assert not (tmp_path / 'acceptance/42.md').exists()


def test_an_invalid_draft_still_refuses_when_the_body_list_is_invalid(tmp_path):
    a = load('acceptance')
    draft = tmp_path / 'draft.md'
    draft.write_text('## Done when\n- [ ] one\n')
    source = Source('## Done when\n- [ ] Only one\n')
    with pytest.raises(ValueError):
        a.record(tmp_path, '42', source=source, config={}, draft=draft)
    assert source.posts == 0 and not (tmp_path / 'acceptance/42.md').exists()
