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
from .errors import CommandError, SDKError

VARIABLES = r'EMBER_[A-Z0-9_]+_SOURCE(?:[ \t]+and[ \t]+EMBER_[A-Z0-9_]+_SOURCE)*'
GUARD = re.compile(r'^\.[ \t]*error[ \t]+"?Set[ \t]+(' + VARIABLES + r')\b')
REFUSAL = re.compile(r': "?Set[ \t]+(' + VARIABLES + r')\b')
CONDITIONAL = re.compile(r'^\.[ \t]*(?:el)?if(?:n?def|n?make)?\b')
TARGET = re.compile(r'make\(([^()\s]+)\)')

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


def source_option(variable):
    """EMBER_TCP_WRAPPERS_SOURCE -> tcp-wrappers-source."""
    return variable[len('EMBER_'):-len('_SOURCE')].lower().replace('_', '-') + '-source'


def guards(path):
    """Return (variables, targets) for each missing-source refusal in PATH.

    TARGETS holds the make targets that the enclosing condition names, or
    None when the refusal does not depend on the requested target."""
    try:
        text = Path(path).read_text(encoding='utf-8', errors='surrogateescape')
    except OSError as exc:
        raise SDKError(f'cannot read OS makefile {path}') from exc
    lines = re.sub(r'\\\n', ' ', text).split('\n')
    found = []
    for index, line in enumerate(lines):
        match = GUARD.match(line)
        if not match:
            continue
        condition = next((lines[i] for i in range(index - 1, -1, -1) if CONDITIONAL.match(lines[i])), '')
        targets = frozenset(TARGET.findall(condition)) or None
        found.append((tuple(re.findall(r'EMBER_[A-Z0-9_]+_SOURCE', match.group(1))), targets))
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


def required_sources(os_root, query, traversals, *, jobs=8):
    """Map each EMBER_*_SOURCE that TRAVERSALS reach to the first directory needing it."""
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
                            if guarded is None or guarded & targets:
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
