# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""One bounded history validation scope. No persistent cache or accepted receipts.

A memo hit reuses its result without rereading its inputs; a new scope reads them again.
"""

from contextvars import ContextVar
from copy import deepcopy
import hashlib
import json
import os

from .errors import SDKError
from .paths import stable_identity
from .jsonio import MAX_JSON, loads_json

_CURRENT = ContextVar('bootstrap_history_validation', default=None)
MAX_NODES = 256
MAX_DEPTH = 64
MAX_GUARDS = 32768
MAX_KEY_BYTES = 16 * 1024 * 1024
MAX_JSON_DOCUMENTS = 1024
MAX_JSON_VALIDATED_BYTES = 64 * 1024 * 1024


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
        self.json_validated = set()
        self.json_weight = 0

    def observe(self, key, value, weight=1):
        if not self.frames:
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

    def call(self, key, validate, memoize):
        if key in self.active:
            raise SDKError('cycle in bootstrap history validation')
        if memoize and key in self.memo:
            # The key binds the recorded input hashes; a hit in this scope
            # does not read, hash or inventory those inputs again.
            result, dependencies = self.memo[key]
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


def load_json(data, *, max_bytes=MAX_JSON, max_depth=32):
    """Reuse strict validation of exact bytes; return a fresh mutable value."""
    context = _CURRENT.get()
    if context is None or type(data) is not bytes:
        return loads_json(data, max_bytes=max_bytes, max_depth=max_depth)
    key = (hashlib.sha256(data).digest(), len(data), max_bytes, max_depth,
           MAX_JSON_DOCUMENTS, MAX_JSON_VALIDATED_BYTES)
    try:
        if key in context.json_validated:
            # The identical bytes passed all strict checks under these limits.
            # Decode again in C instead of sharing or deep-copying mutable objects.
            return json.loads(data)
        value = loads_json(data, max_bytes=max_bytes, max_depth=max_depth)
        if (len(context.json_validated) < MAX_JSON_DOCUMENTS and
                context.json_weight + len(data) <= MAX_JSON_VALIDATED_BYTES):
            context.json_validated.add(key)
            context.json_weight += len(data)
        return value
    except (SDKError, UnicodeError, ValueError, RecursionError) as exc:
        context.json_validated.clear()
        context.json_weight = 0
        if isinstance(exc, SDKError):
            raise
        raise SDKError(f'invalid JSON: {exc}') from exc


def observe_file(path, digest):
    context = _CURRENT.get()
    if context is not None:
        path = os.fspath(path)
        context.observe(('file', path), (digest, stable_identity(os.lstat(path))))


def _tree_guard(root, rows):
    # Include identities because archive-derived source proofs can bind mtimes too.
    identities = tuple((name, stable_identity(os.lstat(os.path.join(root, name))))
                       for name in ('', *sorted(rows)))
    return _digest(rows), identities


def observe_tree(root, machine, exclude, rows):
    context = _CURRENT.get()
    if context is not None:
        root = os.fspath(root)
        context.observe(('tree', root, machine, tuple(exclude)), _tree_guard(root, rows), len(rows) + 1)
