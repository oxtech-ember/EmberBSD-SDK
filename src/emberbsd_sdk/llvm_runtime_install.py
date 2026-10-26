# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Base install aliases derived from the single common LLVM runtime inventory."""

import os
import posixpath
import stat
import unicodedata

from . import llvm_runtime, paths
from .errors import SDKError


def _directories(leaves):
    result = set()
    for name in leaves:
        parent = posixpath.dirname(name)
        while parent:
            result.add(parent)
            parent = posixpath.dirname(parent)
    return result


def _portable_names(leaves):
    names = set(leaves) | _directories(leaves)
    folded = {}
    for name in names:
        key = unicodedata.normalize('NFD', name).casefold()
        if key in folded and folded[key] != name:
            raise SDKError('LLVM base alias case-insensitive path collision: ' + name)
        folded[key] = name
    if set(leaves) & _directories(leaves):
        raise SDKError('LLVM base alias has a non-directory ancestor')


def base_alias_plan(record):
    """Return destination-to-relative-link mappings without reading local files.

    Only full Clang runtimes have this base install contract. Artifact roles
    select the libraries; the manifest selects their actual SONAME aliases,
    linker entry files and C++ header leaves. No header list is maintained here.
    """
    llvm_runtime.validate_identity(record, required_components=llvm_runtime.FULL_COMPONENTS)
    files, links = record['files'], record['symlinks']
    payload = set(files) | set(links)
    _portable_names(payload)
    resolved = {}

    def resolve(name):
        trail = []
        while name not in resolved:
            if name in trail or name not in payload:
                raise SDKError('LLVM base alias has a cyclic or missing runtime target: ' + name)
            trail.append(name)
            if name in files:
                break
            name = posixpath.normpath(posixpath.join(posixpath.dirname(name), links[name]))
        final = resolved.get(name, name)
        for item in trail:
            resolved[item] = final
        return final

    # Directory links cannot replace a header subtree or hide unlisted files.
    for name in payload:
        resolve(name)
    plan = {}

    def add(destination, source):
        target = posixpath.relpath(source, posixpath.dirname(destination))
        if destination in plan and plan[destination] != target:
            raise SDKError('LLVM base alias destination collision: ' + destination)
        plan[destination] = target

    headers = record['artifacts']['cxx_headers'] + '/'
    for name in sorted(payload):
        if name.startswith(headers):
            add('usr/include/c++/' + name[len(headers):], name)
    for component in ('libcxx', 'libcxxabi', 'libunwind'):
        role = record['artifacts'][component]
        static, shared = role['static'], role['shared']
        if not static.endswith('.a'):
            raise SDKError('LLVM static runtime artifact does not identify its linker entry')
        linker = static[:-2] + '.so'
        if linker not in payload:
            raise SDKError('LLVM runtime linker entry is missing: ' + linker)
        if linker in links and resolve(linker) != resolve(shared):
            raise SDKError('LLVM runtime linker entry selects a different shared role: ' + linker)
        selected = {static, shared, linker, resolve(static), resolve(shared)}
        selected.update(name for name in links
                        if posixpath.dirname(name) == posixpath.dirname(shared) and
                        resolve(name) == resolve(shared))
        for name in sorted(selected):
            add('usr/lib/' + posixpath.basename(name), name)
    _portable_names(plan)
    return dict(sorted(plan.items()))


def _root(root):
    root = paths.path_string(root)
    paths.real_directory(root)
    if paths.canonical_existing(root) != root:
        raise SDKError('LLVM base alias root must have real ancestors')
    return root


def preflight_base_aliases(root, record):
    """Refuse all conflicts before writes, including before copying the payload.

    Existing exact aliases and real expected directories can be reused. The
    caller owns the sysroot writer lock throughout preflight and installation.
    """
    root = _root(root)
    plan = base_alias_plan(record)
    directories = _directories(plan)
    for name in sorted(directories | set(plan), key=lambda name: (name.count('/'), name)):
        path = os.path.join(root, name)
        info = paths.entry(path)
        if info is None:
            continue
        # lstat alone can accept a differently cased name on the host filesystem.
        exact_name = posixpath.basename(name) in os.listdir(os.path.dirname(path))
        expected = (stat.S_ISDIR(info.st_mode) if name in directories else
                    stat.S_ISLNK(info.st_mode) and os.readlink(path) == plan[name])
        if not exact_name or not expected:
            raise SDKError('LLVM base alias conflict: ' + name)
    headers = os.path.join(root, 'usr/include/c++')
    if paths.entry(headers) is not None:
        for directory, subdirs, files in os.walk(headers, followlinks=False):
            for basename in subdirs + files:
                name = os.path.relpath(os.path.join(directory, basename), root).replace(os.sep, '/')
                if name not in directories and name not in plan:
                    raise SDKError('LLVM base alias conflict with an unlisted header path: ' + name)
    return plan


def install_base_aliases(root, record):
    """Install relative leaf aliases after verifying the imported runtime bytes."""
    root = _root(root)
    llvm_runtime.validate_install(root, record)
    plan = preflight_base_aliases(root, record)
    for name in sorted(_directories(plan), key=lambda name: (name.count('/'), name)):
        os.makedirs(os.path.join(root, name), mode=0o755, exist_ok=True)
    for name, target in plan.items():
        path = os.path.join(root, name)
        if paths.entry(path) is None:
            os.symlink(target, path)
    return plan


def validate_base_aliases(root, record):
    """Check the runtime and exact selected base paths without changing files."""
    root = _root(root)
    llvm_runtime.validate_install(root, record)
    plan = preflight_base_aliases(root, record)
    for name in plan:
        if paths.entry(os.path.join(root, name)) is None:
            raise SDKError('LLVM base alias is missing: ' + name)
    return plan
