# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""One bounded history validation scope. No persistent cache or accepted receipts."""

from contextvars import ContextVar
from copy import deepcopy
import hashlib
import json
import os

from .errors import SDKError
from .paths import stable_identity

_CURRENT = ContextVar('bootstrap_history_validation', default=None)
MAX_NODES = 256
MAX_DEPTH = 64
MAX_GUARDS = 32768
MAX_KEY_BYTES = 16 * 1024 * 1024


def _digest(value):
    try:
        data = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, RecursionError) as exc:
        raise SDKError('invalid history validation key') from exc
    if len(data) > MAX_KEY_BYTES:
        raise SDKError('history validation key exceeds bound')
    return hashlib.sha256(data).hexdigest()


class _Context:
    def __init__(self):
        self.memo = {}
        self.active = set()
        self.frames = []
        self.guards = {}
        self.guard_weight = 0
        self.checking = False

    def observe(self, key, value, weight=1):
        if self.checking or not self.frames:
            return
        if key in self.guards:
            if self.guards[key] != value:
                raise SDKError('history input changed within validation: ' + key[1])
        else:
            if self.guard_weight + weight > MAX_GUARDS:
                raise SDKError('history validation input guard bound exceeded')
            self.guards[key] = value
            self.guard_weight += weight
        self.frames[-1].add(key)

    def recheck(self, dependencies):
        # Re-read hashes and complete source inventories. Never trust a timestamp cache.
        from .inventory import sha256_file, snapshot_tree
        self.checking = True
        try:
            for key in sorted(dependencies, key=repr):
                expected = self.guards[key]
                if key[0] == 'file':
                    actual = (sha256_file(key[1]), stable_identity(os.lstat(key[1])))
                else:
                    rows = snapshot_tree(key[1], machine=key[2], exclude=key[3])
                    actual = _tree_guard(key[1], rows)
                if actual != expected:
                    raise SDKError('memoized history input changed: ' + key[1])
        finally:
            self.checking = False

    def call(self, key, validate, memoize):
        if key in self.active:
            raise SDKError('cycle in bootstrap history validation')
        if memoize and key in self.memo:
            result, dependencies = self.memo[key]
            self.recheck(dependencies)
            if self.frames:
                self.frames[-1].update(dependencies)
            return deepcopy(result)
        if len(self.active) >= MAX_DEPTH or len(self.memo) + len(self.active) >= MAX_NODES:
            raise SDKError('bootstrap history validation graph bound exceeded')
        self.active.add(key)
        self.frames.append(set())
        try:
            result = validate()
            dependencies = frozenset(self.frames[-1])
            if memoize:
                self.memo[key] = (deepcopy(result), dependencies)
        finally:
            self.frames.pop()
            self.active.remove(key)
        if self.frames:
            self.frames[-1].update(dependencies)
        return deepcopy(result)


def validate_history(arguments, validate, *, memoize=True):
    """Nested histories share this context; each independent top-level call starts fresh."""
    context = _CURRENT.get()
    token = None
    if context is None:
        context = _Context()
        token = _CURRENT.set(context)
    try:
        return context.call(_digest(arguments), validate, memoize)
    finally:
        if token is not None:
            _CURRENT.reset(token)


def observe_file(path, digest):
    context = _CURRENT.get()
    if context is not None and not context.checking:
        path = os.fspath(path)
        context.observe(('file', path), (digest, stable_identity(os.lstat(path))))


def _tree_guard(root, rows):
    # Include identities because archive-derived source proofs can bind mtimes too.
    identities = tuple((name, stable_identity(os.lstat(os.path.join(root, name))))
                       for name in ('', *sorted(rows)))
    return _digest(rows), identities


def observe_tree(root, machine, exclude, rows):
    context = _CURRENT.get()
    if context is not None and not context.checking:
        root = os.fspath(root)
        context.observe(('tree', root, machine, tuple(exclude)), _tree_guard(root, rows), len(rows) + 1)
