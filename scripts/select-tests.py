#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD; AI-assisted local test selection.
"""Select the test modules affected by local changes.

Changed paths are taken against the merge base with BASE (origin/main by
default), plus staged, unstaged and untracked files. A test module is
selected when it imports a changed module directly or through other SDK,
script or test helper modules, or when its text names a changed file.
The whole suite is selected when a changed SDK module, script or test
helper is used by at least half of the test modules (the shared core), when a changed file outside
docs is not named by any test, or when the change cannot be mapped.

This is for local checks before a push. CI always runs every test.
Usage: select-tests.py [--base REF] [--run [PYTHON]]
"""
import argparse
import ast
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
PACKAGE = 'emberbsd_sdk'
CORE_SHARE = 0.5


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True,
                          capture_output=True).stdout.decode('utf-8', 'strict')


def changed_paths(root, base):
    merge = git(root, 'merge-base', base, 'HEAD').strip()
    names = set()
    for args in (('diff', '--name-only', '-z', merge), ('diff', '--name-only', '-z', '--cached', merge),
                 ('ls-files', '--others', '--exclude-standard', '-z')):
        names.update(name for name in git(root, *args).split('\0') if name)
    return names


def python_files(root):
    files = {}
    for path in sorted((root / 'src' / PACKAGE).rglob('*.py')):
        relative = path.relative_to(root / 'src').with_suffix('')
        parts = relative.parts[:-1] if relative.name == '__init__' else relative.parts
        files['.'.join(parts)] = path
    for folder in ('tests', 'scripts'):
        for path in sorted((root / folder).glob('*.py')):
            files[folder + ':' + path.stem] = path
    return files


def imports(node_name, path, known):
    """Return the known modules that a file imports."""
    tree = ast.parse(path.read_bytes(), str(path))
    package = node_name.split('.')[:-1] if '.' in node_name and path.name != '__init__.py' else node_name.split('.')
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[:len(package) - node.level + 1] if node.level > 1 else package
                module = '.'.join(base + ([node.module] if node.module else []))
            else:
                module = node.module or ''
            names = [module] + [module + '.' + alias.name for alias in node.names]
        else:
            continue
        for name in names:
            for candidate in (name, 'tests:' + name, 'scripts:' + name):
                if candidate in known:
                    found.add(candidate)
            while '.' in name:
                name = name.rsplit('.', 1)[0]
                if name in known and name != PACKAGE:
                    found.add(name)
    return found


def graph(root):
    files = python_files(root)
    edges = {name: imports(name, path, files) for name, path in files.items()}
    texts = {name: path.read_text('utf-8', 'replace') for name, path in files.items()}
    # A file that runs another one as a program or loads it by path names it.
    spelled = {other: (path.name if other.startswith(('scripts:', 'tests:')) else
                       other if path.name != '__init__.py' else None)
               for other, path in files.items()}
    for name, text in texts.items():
        for other, spelling in spelled.items():
            if other != name and spelling and spelling in text:
                edges[name].add(other)
    return files, edges, texts


def closure(name, edges):
    seen, pending = set(), [name]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        pending.extend(edges.get(current, ()))
    return seen


def select(root, changed):
    """Return (sorted test module names, reason). None means the whole suite."""
    files, edges, texts = graph(root)
    tests = sorted(name for name in files if name.startswith('tests:test_'))
    reach = {test: closure(test, edges) for test in tests}
    by_path = {str(path.relative_to(root)): name for name, path in files.items()}
    selected = set()
    for path in sorted(changed):
        name = by_path.get(path)
        if name is None and path.endswith('.py') and path.startswith(('src/', 'tests/', 'scripts/')):
            return None, f'{path} is not a mapped module'
        if name is not None:
            users = {test for test in tests if name in reach[test]}
            # A test module that other tests import is a shared helper too.
            if len(users - {name}) >= CORE_SHARE * len(tests):
                return None, f'{path} is shared SDK core'
            selected |= users
            continue
        # Other files are selected by tests that name them.
        base = path.rsplit('/', 1)[-1]
        users = {test for test in tests if path in texts[test] or base in texts[test]}
        if not users and not (path.startswith('docs/') or '/' not in path and path.endswith('.md')):
            return None, f'{path} is not named by any test'
        selected |= users
    return sorted(test[6:] for test in selected), f'{len(changed)} changed paths'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--base', default='origin/main')
    parser.add_argument('--run', nargs='?', const=sys.executable, metavar='PYTHON')
    args = parser.parse_args(argv)
    modules, reason = select(ROOT, changed_paths(ROOT, args.base))
    if modules is None:
        print(f'select-tests: whole suite ({reason})', file=sys.stderr)
        command = ['-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_*.py']
    else:
        print(f'select-tests: {len(modules)} modules ({reason})', file=sys.stderr)
        print('\n'.join(modules), flush=True)
        if not modules:
            return 0
        command = ['-m', 'unittest', *modules]
    if args.run is None:
        return 0
    cwd = ROOT if modules is None else ROOT / 'tests'
    return subprocess.run([args.run, '-B', *command], cwd=cwd).returncode


if __name__ == '__main__':
    sys.exit(main())
