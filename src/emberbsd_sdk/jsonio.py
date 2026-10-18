# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Strict bounded UTF-8 JSON and durable file writes."""

import json
import os
import stat
from .errors import SDKError
from .paths import open_regular, verify_stable

MAX_JSON = 16 * 1024 * 1024


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SDKError(f'duplicate metadata key: {key}')
        result[key] = value
    return result


def _constant(value):
    raise SDKError(f'nonfinite JSON value: {value}')


def loads_json(data, max_bytes=MAX_JSON, max_depth=32):
    try:
        if isinstance(data, bytes):
            raw = data
            data = raw.decode('utf-8', 'strict')
        elif isinstance(data, str):
            raw = data.encode('utf-8', 'strict')
        else:
            raise SDKError('JSON input must be bytes or str')
        if len(raw) > max_bytes:
            raise SDKError('JSON exceeds the size limit')
        # Limit nesting before the recursive decoder sees attacker-controlled input.
        depth = 0
        quoted = escaped = False
        for char in data:
            if quoted:
                if escaped:
                    escaped = False
                elif char == '\\':
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in '[{':
                depth += 1
                if depth > max_depth:
                    raise SDKError('JSON exceeds the nesting limit')
            elif char in ']}':
                depth -= 1
        value = json.loads(data, object_pairs_hook=_object, parse_constant=_constant)
        pending = [value]
        while pending:
            item = pending.pop()
            if isinstance(item, str):
                item.encode('utf-8', 'strict')
            elif isinstance(item, dict):
                pending.extend(item.keys())
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
            elif isinstance(item, float) and not __import__('math').isfinite(item):
                raise SDKError('nonfinite JSON number')
        return value
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise SDKError(f'invalid JSON: {exc}') from exc


def read_json(path, max_bytes=MAX_JSON, max_depth=32):
    handle, before = open_regular(path)
    with handle:
        data = handle.read(max_bytes + 1)
    verify_stable(path, before)
    return loads_json(data, max_bytes=max_bytes, max_depth=max_depth)


def json_bytes(value):
    try:
        return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
    except (ValueError, TypeError, UnicodeError) as exc:
        raise SDKError(f'invalid output JSON: {exc}') from exc


def write_json(path, value, exclusive=True, mode=0o644):
    data = json_bytes(value)
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    if exclusive:
        flags |= os.O_EXCL
    fd = os.open(path, flags, mode)
    with os.fdopen(fd, 'wb') as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise SDKError(f'expected a regular output JSON: {path}')
        handle.write(data)
        handle.truncate()
        handle.flush()
        os.fsync(handle.fileno())
