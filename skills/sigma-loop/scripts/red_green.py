"""Planned pytest red-before-green proof (#267), separate from legacy advisory witnesses.

One explicit collection and test run per verify. No pytest plugin dependency. Only assertion
failures attributed by pytest's per-node summary qualify. Whole test-file bytes bind red to green;
external fixtures/helpers are not part of that identity. Local evidence is not tamper-proof.
"""
import hashlib
import importlib.util
import json
import math
import os
import pathlib
import re
import subprocess
import time


def _witness():
    spec = importlib.util.spec_from_file_location('witness', pathlib.Path(__file__).with_name('witness.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PROVENANCE = 'planned-pytest-v1'
MAX_SELECTORS = 25  # protocol scope ceiling, matching flake_check.MAX_SCOPED_FILES
WIDE_COLUMNS = '4000'


def digest(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


_NONE = re.compile(r'none: (\S.*)')
_DOC_DIRS = ('docs/', '.sdlc/')
_DOC_SUFFIXES = ('.md', '.txt', '.png', '.svg')


def none_reason(plan):
    """#1942: the reason of a `## Tests` section that is exactly one `none: <reason>` line, else None."""
    if plan is None:
        return None
    body, active = [], False
    for line in pathlib.Path(plan).read_text().splitlines():
        if line.strip() == '## Tests':
            if active or body:
                return None
            active = True
            continue
        if line.startswith('## '):
            active = False
        if active and line.strip():
            body.append(line.strip())
    match = _NONE.fullmatch(body[0]) if len(body) == 1 else None
    return match[1].strip() if match else None


def docs_only(paths):
    """#1942: True when every changed path is documentation (never an empty list, never a `..` path)."""
    paths = [p.strip() for p in paths if p.strip()]
    return bool(paths) and all(
        '..' not in pathlib.PurePosixPath(p).parts
        and (p.startswith(_DOC_DIRS) or p.endswith(_DOC_SUFFIXES)) for p in paths)


def selectors(plan):
    """Canonical ## Tests entries are backticked pytest file/node selectors, one per bullet."""
    if plan is None:
        raise ValueError('plan has no ## Tests section')
    active, found, result = False, False, []
    for line in pathlib.Path(plan).read_text().splitlines():
        if line.strip() == '## Tests':
            if found:
                raise ValueError('duplicate ## Tests section')
            active = found = True
            continue
        if line.startswith('## '):
            active = False
        if not active or not line.strip():
            continue
        match = re.fullmatch(r'\s*- `([^`]+)`\s*', line)
        if not match:
            raise ValueError('## Tests requires one backticked pytest selector per bullet')
        selector = match[1]
        file = selector.split('::')[0]
        parts = pathlib.PurePosixPath(file).parts
        if (not file.endswith('.py') or not parts or file.startswith(('/', '-'))
                or any(p in ('.', '..') for p in parts) or '\\' in selector
                or any(c.isspace() for c in selector) or any(c in selector for c in ';|&')):
            raise ValueError('unsafe pytest selector: ' + selector)
        if selector not in result:
            result.append(selector)
    if not result or len(result) > MAX_SELECTORS:
        raise ValueError('## Tests requires 1–25 pytest selectors; use --no-tests, or the line `none: <reason>` for a docs-only change')
    return result


def _run(root, argv):
    # Piped pytest sizes its -rA summary from COLUMNS (80 if unset) and drops the ' - reason' of a
    # longer line (#414); an inherited narrow value is overridden on purpose.
    return subprocess.run(argv, cwd=root, capture_output=True, text=True,
                          env={**os.environ, 'COLUMNS': WIDE_COLUMNS})


def _selected(node, scope):
    return any(node == s or node.startswith(s + '::') or node.startswith(s + '[') for s in scope)


def collect(root, scope, run=None):
    run = run or _run
    proc = run(root, ['python3', '-m', 'pytest', '--collect-only', '-q', '--color=no',
                      '--rootdir=' + str(root), *scope])
    nodes = [line.strip() for line in proc.stdout.splitlines() if '::' in line]
    if proc.returncode or not nodes or any(not _selected(n, scope) for n in nodes):
        raise ValueError('planned pytest collection failed or returned unexpected node ids')
    if any(not any(_selected(n, [s]) for n in nodes) for s in scope):
        raise ValueError('a planned selector collected no tests')
    return sorted(set(nodes))


def _hashes(root, nodes):
    root = pathlib.Path(root).resolve()
    result, cache = {}, {}
    for node in nodes:
        file = (root / node.split('::')[0]).resolve()
        if not file.is_relative_to(root):
            raise ValueError('planned test escapes the repository')
        if file not in cache:
            cache[file] = digest(file)
        result[node] = cache[file]
    return result


def _split_traceback_assertions(output, failed):
    """Return uniquely attributable assertion failures from pytest's report blocks.

    Pytest 7--9 prints bare ``FAILED <node>`` lines in its short summary, while
    the assertion type lives in the preceding traceback.  The summary still
    supplies the authoritative failing node; this only joins it to a report
    block when its file and unparameterized function name select exactly one
    such node.  Ambiguous parameterized reports fail closed.
    """
    reports = re.split(r"^=+ short test summary info =+\s*$", output, flags=re.M)[0]
    # Captured test stdout/stderr shares pytest's report stream.  It is
    # untrusted test-controlled text, so do not parse any split traceback
    # attribution from a run that contains it. Refusing a noisy real assertion
    # is safer than crediting a forged one.
    if re.search(r"^-+ Captured (?:stdout|stderr|log) .*-+$", reports, re.M):
        return set()
    assertions = set()
    for block in re.split(r"(?m)^_{3,}.*_{3,}\s*$", reports):
        if not re.search(r"^E\s+(?:AssertionError\b|assert\b)", block, re.M):
            continue
        locations = re.findall(r"^(?P<file>[^:\n]+\.py):\d+: in (?P<name>[^\s]+)",
                               block, re.M)
        for report_file, report_name in locations:
            report_file = report_file.lstrip("./")
            candidates = {
                node for node in failed
                if (node.split("::", 1)[0] == report_file
                    or report_file.endswith("/" + node.split("::", 1)[0]))
                and node.rsplit("::", 1)[-1].split("[", 1)[0] == report_name
            }
            if len(candidates) == 1:
                assertions.update(candidates)
    return assertions


def observe(sdlc_dir, goal, root, plan, run=None):
    """Return green proof or an explicit absence; write only attributed, stable assertion reds."""
    run = run or _run
    try:
        plan_hash = digest(plan) if plan else None
        scope = selectors(plan)
        # Snapshot before collection too: imports may edit tests or the plan.
        before_files = _hashes(root, scope)
        nodes = collect(root, scope, run)
        hashes = _hashes(root, nodes)
        if before_files != _hashes(root, scope) or plan_hash != digest(plan):
            raise ValueError('test or plan changed during collection')
        started = time.time()
        proc = run(root, ['python3', '-m', 'pytest', '-q', '-rA', '--tb=short', '--color=no',
                          '--rootdir=' + str(root), *nodes])
        completed = time.time()
        if hashes != _hashes(root, nodes) or plan_hash != digest(plan):
            raise ValueError('test or plan changed during observation')
        passed, assertion = set(), set()
        # Captured test output can itself contain FAILED/PASSED diagnostics. Only pytest's
        # final summary is authoritative; absent summaries earn no proof.
        summaries = re.split(r"^=+ short test summary info =+\s*$", proc.stdout, flags=re.M)
        summary = summaries[-1] if len(summaries) > 1 else ""
        failed = set()
        # An expected failure (XFAIL/XPASS) ran and behaved as declared, so it is accounted for; a
        # whole-file selector over a file with one used to fail every verify. SKIPPED proves nothing.
        accounted = set()
        for line in summary.splitlines():
            if line.startswith(('PASSED ', 'XFAIL ', 'XPASS ')):
                kind, _, rest = line.partition(' ')
                node = rest.partition(' - ')[0].strip() if kind != 'PASSED' else rest.strip()
                if node in hashes:
                    (passed if kind == 'PASSED' else accounted).add(node)
            elif line.startswith('FAILED '):
                node, sep, reason = line[len('FAILED '):].partition(' - ')
                if node in hashes:
                    failed.add(node)
                    if sep and re.match(r'(AssertionError\b|assert\b)', reason):
                        assertion.add(node)
        assertion.update(_split_traceback_assertions(proc.stdout, failed))
        # Collection/interruption/internal/usage failures cannot create trustworthy red.
        if proc.returncode == 1:
            w = _witness()
            for node in sorted(assertion):
                w.record(sdlc_dir, goal, node, 'assertion',
                         w.source_hash(w.test_source(node, root=root)),
                         detail='planned pytest assertion', provenance=PROVENANCE,
                         file_hash=hashes[node], observed_at=completed)
        missing = sorted(set(nodes) - passed - accounted)
        return {'provenance': PROVENANCE, 'plan_sha256': plan_hash, 'selectors': scope,
                'nodes': hashes, 'started_at': started, 'completed_at': completed,
                'passed': proc.returncode == 0 and not missing,
                'exit': proc.returncode,
                'error': '' if not missing else 'planned tests did not all pass: %d not PASSED/XFAIL (first: %s)' % (
                    len(missing), ', '.join(missing[:3]))}
    except (OSError, ValueError, TypeError) as exc:
        return {'passed': False, 'error': str(exc)}


def _timestamp(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def refusal(sdlc_dir, goal, root, plan, proof):
    """Fail closed; stream one goal's journal, retaining at most one eligible red per node."""
    try:
        scope = selectors(plan)
        if not isinstance(proof, dict) or proof.get('provenance') != PROVENANCE or proof.get('passed') is not True:
            return 'no observed green for planned tests'
        if proof.get('plan_sha256') != digest(plan) or proof.get('selectors') != scope:
            return 'plan changed since observed green'
        nodes = proof.get('nodes')
        if not isinstance(nodes, dict) or not nodes or any(not _selected(n, scope) for n in nodes):
            return 'invalid planned test identities'
        if any(not any(_selected(n, [s]) for n in nodes) for s in scope) or nodes != _hashes(root, nodes):
            return 'planned test files changed since observed green'
        started, completed = proof.get('started_at'), proof.get('completed_at')
        if not _timestamp(started) or not _timestamp(completed) or completed < started:
            return 'invalid observed green ordering'
        missing = set(nodes)
        try:
            with _witness().path(sdlc_dir, goal).open() as journal:
                for line in journal:
                    try:
                        row = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(row, dict):
                        continue
                    node, at = row.get('test'), row.get('observed_at')
                    if (isinstance(node, str) and node in missing and row.get('provenance') == PROVENANCE
                            and row.get('kind') == 'assertion' and row.get('file_hash') == nodes[node]
                            and _timestamp(at) and at < started):
                        missing.remove(node)
        except FileNotFoundError:
            pass
        if missing:
            return 'no matching assertion red before green: ' + ', '.join(sorted(missing))
        return ''
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return 'cannot validate planned test proof: ' + str(exc)


SEALED_PROVENANCE = 'sealed-pytest-v1'  # #847: red = ANY test-body failure on base content (see classify_sealed)


def classify_sealed(collect_rc, collect_out, run_rc, run_out, nodes):
    """Pure classifier for the sealed-pytest-v1 proof. -> {collection_errors, red, passed, other, error}.

    Unlike `observe`, ANY `FAILED` node counts as red once collection was clean and the run exited 1,
    including an ImportError raised inside the test body (D-1; that proves "symbol missing", not
    "assertion meaningful", and the exception class is returned per red so a reader can see it). Never
    red: a collection error (exit 2 or `ERROR <file>`), fixture `ERROR <node>`, skip, xfail, xpass,
    exit 3/4/5, a missing summary. Nodes are matched by collected id (never by splitting on ' - ').
    `red` maps node -> ('assertion'|'other', ExceptionClass-or-''). `run_rc=None` classifies the
    collect-only step alone. `witness.classify` is deliberately not used: it labels an in-body
    ImportError a collection error."""
    out = {'collection_errors': [], 'red': {}, 'passed': [], 'other': [], 'error': ''}
    files = []
    for line in (collect_out or '').splitlines():
        line = line.strip()
        if line.startswith('ERROR '):
            files.append(line[len('ERROR '):].partition(' - ')[0].replace('collecting ', '', 1).strip())
    if files or collect_rc == 2:
        out['collection_errors'] = files or ['<file not named by pytest>']
        return out
    if collect_rc == 5 or (collect_rc == 0 and not nodes):
        out['error'] = 'pytest collected no tests (exit %s)' % collect_rc
        return out
    if collect_rc != 0:
        out['error'] = 'pytest --collect-only exited %s' % collect_rc
        return out
    if run_rc is None:
        return out
    if run_rc not in (0, 1):
        out['error'] = 'pytest run exited %s (not 0 or 1)' % run_rc
        return out
    parts = re.split(r'^=+ short test summary info =+\s*$', run_out or '', flags=re.M)
    if len(parts) < 2:
        out['error'] = 'pytest run printed no short test summary'
        return out
    lines = parts[-1].splitlines()
    for node in nodes:
        failed = [ln for ln in lines if ln == 'FAILED ' + node or ln.startswith('FAILED ' + node + ' - ')]
        if failed:
            rest = failed[0][len('FAILED ' + node):]
            reason = rest[3:] if rest.startswith(' - ') else ''
            if re.match(r'(AssertionError\b|assert\b)', reason):
                out['red'][node] = ('assertion', 'AssertionError')
            else:
                match = re.match(r'[A-Za-z_][\w.]*', reason)
                out['red'][node] = ('other', match[0].rstrip('.') if match else '')
        elif 'PASSED ' + node in lines:
            out['passed'].append(node)
        else:
            out['other'].append(node)
    if run_rc == 0 and out['red']:
        out.update(red={}, error='pytest exited 0 but the summary lists FAILED nodes')
    return out
