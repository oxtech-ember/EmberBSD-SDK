# SPDX-License-Identifier: BSD-2-Clause
# Origin: EmberBSD (AI-assisted). Scoped dependency invalidation.
"""Plan and apply LLVM prepared-source dependency refresh under object lease."""
import os
import re

from .errors import SDKError
from . import paths

_SUFFIXES = ('.o', '.po', '.pico', '.go', '.ln')


def _root(value):
    if (not isinstance(value, str) or not os.path.isabs(value) or
            os.path.normpath(value) != value or value == '/' or
            re.fullmatch(r'/[A-Za-z0-9_./+\-]+', value) is None):
        raise SDKError('invalid LLVM source root')
    return value.rstrip('/')


def _read(path):
    stream, _ = paths.open_regular(path)
    with stream:
        return stream.read().decode('utf-8', 'strict')


def _matches(text, roots):
    return any(re.search(r'(?<![A-Za-z0-9_./+@%=-])' + re.escape(root) + r'/', text)
               for root in roots)


def _dependency_tokens(data):
    """Parse literal make dependency words; reject dynamic or malformed input."""
    words = []
    for line in data.replace("\\\n", " ").splitlines():
        if not line.strip():
            continue
        if " : " in line or ":" not in line:
            raise SDKError("unsupported LLVM dependency rule")
        _, values = line.split(":", 1)
        token = ""
        escaped = False
        for char in values:
            if escaped:
                token += char
                escaped = False
            elif char == "\\":
                escaped = True
            elif char in "$#":
                raise SDKError("dynamic LLVM dependency token")
            elif char.isspace():
                if token:
                    words.append(token)
                    token = ""
            else:
                token += char
        if escaped:
            raise SDKError("incomplete LLVM dependency escape")
        if token:
            words.append(token)
    return words


def plan(objects, old, new, changed_relative_paths=None):
    """Return metadata and outputs to delete; no build output is modified."""
    roots = tuple(dict.fromkeys((_root(old), _root(new))))
    changed = None
    if changed_relative_paths is not None:
        if not isinstance(changed_relative_paths, (list, tuple)):
            raise SDKError('invalid changed LLVM source paths')
        changed = set()
        for name in changed_relative_paths:
            if (not isinstance(name, str) or not name or name.startswith('/') or
                    any(x in ('', '.', '..') for x in name.split('/')) or
                    re.fullmatch(r'[A-Za-z0-9_./+\-]+', name) is None):
                raise SDKError('invalid changed LLVM source path')
            changed.add(name)
    canonical_cache = {}
    def canonical(path):
        if path not in canonical_cache:
            try:
                canonical_cache[path] = os.path.realpath(path, strict=True)
            except OSError:
                canonical_cache[path] = None
        return canonical_cache[path]

    changed_absolute = set()
    if changed is not None:
        for name in changed:
            candidates = [canonical(root_path + '/' + name) for root_path in roots]
            if not any(candidates):
                # Missing proof for a changed input cannot justify preserving outputs.
                changed = None
                break
            changed_absolute.update(path for path in candidates if path is not None)
    root = os.path.abspath(objects)
    if os.path.realpath(root) != root:
        raise SDKError('LLVM object root must not be a symlink')
    paths.real_directory(root)
    matched, depends, parent_depends = set(), set(), set()
    metadata = []
    def reject_walk_error(error):
        raise error

    try:
        for parent, dirs, files in os.walk(root, followlinks=False, onerror=reject_walk_error):
            # Never traverse aliases, even when they point into this object tree.
            dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(parent, name))]
            if os.path.realpath(parent) != parent:
                raise SDKError('unsafe LLVM object directory')
            for name in files:
                if name == '.depend' or name.endswith('.d'):
                    file = os.path.join(parent, name)
                    if os.path.islink(file):
                        raise SDKError('symlinked LLVM dependency metadata: ' + file)
                    data = _read(file)
                    if name == '.depend':
                        parent_depends.add(file)
                    if _matches(data, roots):
                        metadata.append((file, data))
                        if name != '.depend':
                            matched.add(file)
        for file, data in metadata:
            if os.path.basename(file) != '.depend':
                continue
            depends.add(file)
            for line in data.replace('\\\n', ' ').splitlines():
                if not _matches(line, roots):
                    continue
                if ':' not in line:
                    raise SDKError('unsupported LLVM dependency rule')
                targets = line.split(':', 1)[0].split()
                if not targets:
                    raise SDKError('LLVM dependency rule has no targets')
                owners = set()
                for target in targets:
                    if '/' in target or not re.fullmatch(r'[A-Za-z0-9_+.-]+', target):
                        raise SDKError('unsafe LLVM dependency target')
                    suffix = next((s for s in ('.d',) + _SUFFIXES if target.endswith(s)), None)
                    if suffix is None:
                        raise SDKError('unsupported LLVM dependency target')
                    owners.add(os.path.join(os.path.dirname(file), target[:-len(suffix)] + '.d'))
                if not owners or not owners <= matched:
                    raise SDKError('LLVM dependency rule lacks matching .d metadata')
        # A parent .depend can include .d files indirectly, without naming
        # LLVM itself. Rebuild it whenever any child dependency is removed.
        for dep in matched:
            parent_depend = os.path.join(os.path.dirname(dep), ".depend")
            if parent_depend in parent_depends:
                depends.add(parent_depend)
        outputs = []
        for dep in sorted(matched):
            if changed is not None:
                tokens = _dependency_tokens(_read(dep))
                affected = False
                for token in tokens:
                    path = token if os.path.isabs(token) else os.path.join(os.path.dirname(dep), token)
                    resolved = canonical(path)
                    if resolved in changed_absolute:
                        affected = True
                        break
                    if resolved is None:
                        lexical = os.path.normpath(path)
                        if (not os.path.isabs(token) or any(
                                lexical == root_path or lexical.startswith(root_path + '/')
                                for root_path in roots)):
                            affected = True
                            break
                if not affected:
                    continue
            stem = dep[:-2]
            for suffix in _SUFFIXES:
                path = stem + suffix
                if os.path.lexists(path):
                    paths.regular_file(path)
                    outputs.append(path)
        return dict(depends=sorted(depends), outputs=outputs, metadata=sorted(matched))
    except (OSError, UnicodeError) as exc:
        raise SDKError('cannot safely inspect LLVM dependencies: ' + str(exc)) from exc


def apply(objects, expected, old, new, changed_relative_paths=None):
    """Replan under the lease and delete in retry-safe order."""
    actual = plan(objects, old, new, changed_relative_paths)
    if actual != expected:
        raise SDKError('LLVM dependency metadata changed after preflight')
    for category in ('depends', 'outputs', 'metadata'):
        for file in actual[category]:
            paths.regular_file(file)
            os.unlink(file)
