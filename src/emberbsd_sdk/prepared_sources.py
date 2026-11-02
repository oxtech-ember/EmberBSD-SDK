# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Find the prepared common sources that an OS build will reach.

An OS makefile that builds from a source prepared by a Ports owner refuses
to run without it (".error Set EMBER_<NAME>_SOURCE ..."), often only for
the targets that compile or install it. Without a check before the build,
that refusal surfaced hours into a release. This module walks the SUBDIR
graph that the selected make flags enable, from the directories and with
the targets that the build runs, and collects those refusals from the
makefiles that each reached directory reads (bmake's .MAKE.MAKEFILES).
A refusal that does not depend on the target stops the directory's parse
and is reported by bmake itself.
"""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import re

from . import process
from .cpu_capacity import resolve_jobs
from .errors import CommandError, SDKError

VARIABLES = r'EMBER_[A-Z0-9_]+_SOURCE(?:[ \t]+and[ \t]+EMBER_[A-Z0-9_]+_SOURCE)*'
GUARD = re.compile(r'^\.[ \t]*error[ \t]+"?Set[ \t]+(' + VARIABLES + r')\b')
REFUSAL = re.compile(r': "?Set[ \t]+(' + VARIABLES + r')\b')
CONDITIONAL = re.compile(r'^\.[ \t]*(elifn?def|elifn?make|elif|ifn?def|ifn?make|if|else|endif)\b(.*)$')
MAKE_PREDICATE = re.compile(r'make\(\s*([A-Za-z0-9_.+-]+)\s*\)\Z')
TARGETS_EMPTY = re.compile(r'empty\(\s*\.TARGETS((?::N[^:()\s]+)*)\s*\)\Z')
TARGET_FILTER = re.compile(r':N([A-Za-z0-9_.+-]+)')

# Targets that compile or install a directory: build_install, do-build and
# the release reach each subdirectory with these.
BUILD_TARGETS = frozenset(('all', 'dependall', 'install'))
TOOLS_TRAVERSALS = (('tools', BUILD_TARGETS),)
# build.sh libs: "make includes" from the top, then "make do-lib", which is
# "build_install" (dependall, install) in lib.
LIBS_TRAVERSALS = (('.', frozenset(('includes',))), ('lib', BUILD_TARGETS))
# build.sh release: includes, libraries and the whole tree.
RELEASE_TRAVERSALS = (('.', frozenset(('includes',)) | BUILD_TARGETS),)
# build.sh builds host tools in its own "tools" step; the top Makefile
# skips them for includes and builds the tree with BUILD_tools=no.
SEPARATE = ('tools',)
STUB_LOOP = re.compile(r'^\.for[ \t]+dir[ \t]+in[ \t]+([^\n]+)\n((?:[A-Za-z_]+-\$\{dir\}[ \t]*)+):[ \t]*\.PHONY[ \t]*\n\t@true[ \t]*\n\.endfor', re.M)
STUB_RULE = re.compile(r'^((?:[A-Za-z_]+-[A-Za-z0-9_.]+[ \t]+)*[A-Za-z_]+-[A-Za-z0-9_.]+)[ \t]*:[ \t]*\.PHONY[ \t]*\n\t@true[ \t]*$', re.M)


class Refused:
    """bmake stopped parsing a directory because a source is not selected."""
    def __init__(self, variables):
        self.variables = tuple(variables)


class TargetCondition:
    """Conservatively project a bmake conditional onto requested make targets.

    Non-target predicates are unknown: they may enable a source guard, but
    only an evaluated target predicate may rule a requested target out.
    """
    def __init__(self, expression, *, tree=None):
        self.expression = _strip_make_comment(expression).strip()
        self.tree = _parse_target_expression(self.expression) if tree is None else tree
        self.has_target_predicate = (_tree_has_target_predicate(self.tree) or
                                     bool(re.search(r'\bmake\s*\(|\.TARGETS\b', self.expression)))

    def applies(self, requested):
        if not self.has_target_predicate or self.tree is None:
            return True
        requested = frozenset(requested)
        if not requested:
            return True
        # Build traversals currently contain at most four targets. Preserve a
        # conservative answer if a future caller supplies a much larger set.
        if len(requested) > 8:
            return True
        targets = tuple(requested)
        for mask in range(1, 1 << len(targets)):
            invocation = frozenset(targets[index] for index in range(len(targets))
                                   if mask & (1 << index))
            if _condition_value(self.tree, invocation) is not False:
                return True
        return False


def _strip_make_comment(text):
    """Remove an unescaped bmake comment; quoted-hash syntax stays opaque."""
    escaped = False
    for index, char in enumerate(text):
        if escaped:
            escaped = False
            continue
        if char == '\\':
            escaped = True
            continue
        if char == '#':
            return text[:index].rstrip()
    return text


def _condition_parts(text, operator):
    """Split a bmake expression at top-level boolean operators only."""
    parts = []
    depth = 0
    quote = None
    escaped = False
    start = 0
    index = 0
    while index < len(text):
        char = text[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char in ('"', "'"):
            quote = char
            index += 1
            continue
        if char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
            if depth < 0:
                return None
        elif depth == 0 and text.startswith(operator, index):
            parts.append(text[start:index].strip())
            index += len(operator)
            start = index
            continue
        index += 1
    if depth != 0 or quote is not None:
        return None
    if parts:
        parts.append(text[start:].strip())
        return parts if all(parts) else None
    return []


def _strip_condition_group(text):
    """Remove balanced parentheses that enclose the entire expression."""
    while text.startswith('('):
        depth = 0
        quote = None
        escaped = False
        closes_at_end = False
        for index, char in enumerate(text):
            if escaped:
                escaped = False
                continue
            if quote is not None:
                if char == quote:
                    quote = None
                continue
            if char in ('"', "'"):
                quote = char
            elif char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
                if depth < 0:
                    return text
                if depth == 0:
                    closes_at_end = index == len(text) - 1
                    break
        if depth != 0 or not closes_at_end:
            return text
        text = text[1:-1].strip()
    return text


def _parse_target_expression(expression):
    """Parse boolean groups and target predicates; unsupported atoms stay opaque."""
    if '\\' in expression:
        return None
    text = _strip_condition_group(expression.strip())
    if not text:
        return None
    parts = _condition_parts(text, '||')
    if parts is None:
        return None
    if parts:
        children = tuple(_parse_target_expression(part) for part in parts)
        return None if any(child is None for child in children) else ('or', children)
    parts = _condition_parts(text, '&&')
    if parts is None:
        return None
    if parts:
        children = tuple(_parse_target_expression(part) for part in parts)
        return None if any(child is None for child in children) else ('and', children)
    if text.startswith('!') and not text.startswith('!='):
        child = _parse_target_expression(text[1:].strip())
        return None if child is None else ('not', child)
    make = MAKE_PREDICATE.fullmatch(text)
    if make:
        return ('make', make.group(1))
    empty_targets = TARGETS_EMPTY.fullmatch(text)
    if empty_targets:
        modifiers = empty_targets.group(1)
        filters = tuple(match.group(1) for match in TARGET_FILTER.finditer(modifiers))
        if modifiers and ''.join(match.group(0) for match in TARGET_FILTER.finditer(modifiers)) != modifiers:
            return ('unknown', text)
        return ('targets-empty', filters)
    return ('unknown', text)


def _tree_has_target_predicate(tree):
    if tree is None:
        return False
    kind = tree[0]
    if kind in ('make', 'targets-empty'):
        return True
    if kind == 'unknown':
        return bool(re.search(r'\bmake\s*\(|\.TARGETS\b', tree[1]))
    if kind == 'not':
        return _tree_has_target_predicate(tree[1])
    if kind in ('and', 'or'):
        return any(_tree_has_target_predicate(child) for child in tree[1])
    return False


def _and_condition(*trees):
    children = []
    for tree in trees:
        if tree[0] == 'constant':
            if not tree[1]:
                return ('constant', False)
            continue
        children.extend(tree[1] if tree[0] == 'and' else (tree,))
    if not children:
        return ('constant', True)
    return children[0] if len(children) == 1 else ('and', tuple(children))


def _or_condition(*trees):
    children = []
    for tree in trees:
        if tree[0] == 'constant':
            if tree[1]:
                return ('constant', True)
            continue
        children.extend(tree[1] if tree[0] == 'or' else (tree,))
    if not children:
        return ('constant', False)
    return children[0] if len(children) == 1 else ('or', tuple(children))


def _not_condition(tree):
    if tree[0] == 'constant':
        return ('constant', not tree[1])
    if tree[0] == 'not':
        return tree[1]
    return ('not', tree)


def _branch_condition(directive, expression):
    expression = _strip_make_comment(expression).strip()
    if directive in ('if', 'elif'):
        tree = _parse_target_expression(expression)
        return tree if tree is not None else ('unknown', expression)
    return ('unknown', '.' + directive + ' ' + expression)


def _condition_value(tree, targets):
    """Return True, False, or None when an atom is not target-specific."""
    kind = tree[0]
    if kind == 'constant':
        return tree[1]
    if kind == 'make':
        return tree[1] in targets
    if kind == 'targets-empty':
        remaining = set(targets)
        for pattern in tree[1]:
            remaining.discard(pattern)
        return not remaining
    if kind == 'unknown':
        return None
    if kind == 'not':
        value = _condition_value(tree[1], targets)
        return None if value is None else not value
    values = [_condition_value(child, targets) for child in tree[1]]
    if kind == 'and':
        if False in values:
            return False
        return True if all(value is True for value in values) else None
    if kind == 'or':
        if True in values:
            return True
        return False if all(value is False for value in values) else None
    return None


def source_option(variable):
    """EMBER_TCP_WRAPPERS_SOURCE -> tcp-wrappers-source."""
    return variable[len('EMBER_'):-len('_SOURCE')].lower().replace('_', '-') + '-source'


def guards(path):
    """Return (variables, target condition) for each missing-source refusal."""
    try:
        text = Path(path).read_text(encoding='utf-8', errors='surrogateescape')
    except OSError as exc:
        raise SDKError(f'cannot read OS makefile {path}') from exc
    lines = re.sub(r'\\\n', ' ', text).split('\n')
    found = []
    stack = []
    current = ('constant', True)
    invalid_context = False
    for line in lines:
        stripped = _strip_make_comment(line).rstrip()
        directive = CONDITIONAL.match(stripped)
        if directive:
            name, expression = directive.groups()
            if name in ('if', 'ifdef', 'ifndef', 'ifmake', 'ifnmake'):
                branch = _branch_condition(name, expression)
                stack.append({'parent': current, 'branches': [branch], 'else_seen': False})
                current = _and_condition(current, branch)
            elif name in ('elif', 'elifdef', 'elifndef', 'elifmake', 'elifnmake'):
                if not stack or stack[-1]['else_seen']:
                    invalid_context = True
                    current = ('unknown', 'unmatched ' + name)
                else:
                    frame = stack[-1]
                    branch = _branch_condition(name, expression)
                    previous = _or_condition(*frame['branches'])
                    current = _and_condition(frame['parent'], _not_condition(previous), branch)
                    frame['branches'].append(branch)
            elif name == 'else':
                if not stack or stack[-1]['else_seen']:
                    invalid_context = True
                    current = ('unknown', 'unmatched else')
                else:
                    frame = stack[-1]
                    current = _and_condition(frame['parent'],
                                             _not_condition(_or_condition(*frame['branches'])))
                    frame['else_seen'] = True
            elif name == 'endif':
                if not stack:
                    invalid_context = True
                    current = ('unknown', 'unmatched endif')
                else:
                    current = stack.pop()['parent']
            continue
        match = GUARD.match(stripped)
        if match:
            condition = ('unknown', 'invalid conditional context') if invalid_context else current
            target_condition = (TargetCondition('', tree=condition)
                                if _tree_has_target_predicate(condition) else None)
            found.append((tuple(re.findall(r'EMBER_[A-Z0-9_]+_SOURCE', match.group(1))), target_condition))
    if invalid_context or stack:
        found = [(variables, None) for variables, _ in found]
    return found


def top_stubs(os_root):
    """Return the TARGET-DIR stubs that tell bsd.subdir.mk not to recurse.

    The top Makefile defines targets such as includes-distrib as "@true";
    bsd.subdir.mk then skips that subtree for that target."""
    try:
        text = Path(os_root, 'Makefile').read_text(encoding='utf-8')
    except (OSError, UnicodeError) as exc:
        raise SDKError('cannot read the OS top Makefile') from exc
    stubs = set()
    for match in STUB_LOOP.finditer(text):
        for target in match.group(2).split():
            stubs.update(target.replace('${dir}', name) for name in match.group(1).split())
    for match in STUB_RULE.finditer(text):
        stubs.update(match.group(1).split())
    return stubs


def bmake_query(make, os_root, flags, *, env):
    """Return QUERY(directory) -> (subdirs, makefiles) or Refused.

    The query names the "obj" target, which build.sh runs in every
    directory first: -V prints without building, and checks that only
    guard compiling targets stay inactive."""
    base = [make, '-m', os.path.join(os_root, 'share/mk'), *flags,
            '-V', '${SUBDIR}', '-V', '${.MAKE.MAKEFILES}', 'obj']

    def query(directory):
        try:
            output = process.capture([base[0], '-C', directory, *base[1:]], env=env).stdout
        except CommandError as exc:
            message = exc.stderr.decode('utf-8', 'replace')
            match = REFUSAL.search(message)
            if match:
                return Refused(re.findall(r'EMBER_[A-Z0-9_]+_SOURCE', match.group(1)))
            lines = [line.strip() for line in message.splitlines() if line.strip()]
            first = next((line for line in lines if re.search(r':[0-9]+: ', line)), lines[0] if lines else 'no diagnostic')
            raise SDKError(f'cannot inspect OS directory {os.path.relpath(directory, os_root)}: {first}') from exc
        try:
            lines = output.decode('utf-8', 'strict').split('\n')
        except UnicodeError as exc:
            raise SDKError(f'OS directory {os.path.relpath(directory, os_root)} reports non-UTF-8 names') from exc
        if len(lines) < 2:
            raise SDKError(f'cannot inspect OS directory {os.path.relpath(directory, os_root)}')
        return lines[0].split(), lines[1].split()
    return query


def required_sources(os_root, query, traversals, *, jobs=None):
    """Map each EMBER_*_SOURCE that TRAVERSALS reach to the first directory needing it."""
    jobs = resolve_jobs(jobs)
    root = os.path.realpath(os_root)
    separate = {os.path.join(root, name) for name in SEPARATE}
    stubs = top_stubs(root)
    replies = {}
    parsed = {}

    def inside(path):
        return path == root or path.startswith(root + os.sep)

    def reply(directory):
        if directory not in replies:
            replies[directory] = query(directory)
        return replies[directory]

    needed = {}
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        for start, selected in traversals:
            start = os.path.normpath(os.path.join(root, start))
            # Each directory carries the targets that the build runs in it.
            pending = [(start, frozenset(selected))]
            seen = {start}
            while pending:
                results = list(pool.map(reply, [directory for directory, _ in pending]))
                following = []
                for (directory, targets), result in zip(pending, results):
                    where = os.path.relpath(directory, root)
                    if isinstance(result, Refused):
                        for variable in result.variables:
                            needed.setdefault(variable, where)
                        continue
                    subdirs, makefiles = result
                    for name in makefiles:
                        path = os.path.normpath(os.path.join(directory, name))
                        if not inside(path) or not os.path.isfile(path):
                            continue
                        if path not in parsed:
                            parsed[path] = guards(path)
                        for variables, guarded in parsed[path]:
                            if guarded is None or guarded.applies(targets):
                                for variable in variables:
                                    needed.setdefault(variable, where)
                    for name in subdirs:
                        if name == '.WAIT':
                            continue
                        path = os.path.normpath(os.path.join(directory, name))
                        if not inside(path):
                            raise SDKError(f'OS SUBDIR leaves the source tree: {where}/{name}')
                        entered = targets
                        if directory == root:
                            entered = frozenset(t for t in targets if f'{t}-{name}' not in stubs)
                        # bsd.subdir.mk skips a SUBDIR entry without a directory.
                        if entered and path not in seen and path not in separate and os.path.isdir(path):
                            seen.add(path)
                            following.append((path, entered))
                pending = following
    return needed


def missing_sources(needed, selected):
    """Return '--option (VARIABLE for directory)' for each needed source not in SELECTED."""
    return sorted(f'--{source_option(variable)} ({variable} for {directory})'
                  for variable, directory in needed.items() if source_option(variable) not in selected)
