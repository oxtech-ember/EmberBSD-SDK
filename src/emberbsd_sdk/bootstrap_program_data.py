# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Bounded UTF-8 data in pinned source-owner program contracts."""

from .native_toolset import _require

MAX_DATA_BYTES = 32 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_SEGMENTS = 16


def _segments(value):
    _require(isinstance(value, str) or
             isinstance(value, list) and 0 < len(value) <= MAX_SEGMENTS,
             'program data needs a string or bounded segment list')
    result, size = [], 0
    for segment in ([value] if isinstance(value, str) else value):
        if isinstance(segment, str):
            text, repeat = segment, 1
        else:
            _require(isinstance(segment, dict) and set(segment) == {'text', 'repeat'} and
                     isinstance(segment['text'], str) and bool(segment['text']) and
                     type(segment['repeat']) is int and 0 <= segment['repeat'] <= MAX_DATA_BYTES,
                     'invalid program data repetition')
            text, repeat = segment['text'], segment['repeat']
        try:
            raw = text.encode('utf-8')
        except UnicodeEncodeError:
            _require(False, 'program data is not valid UTF-8')
        size += len(raw) * repeat
        _require(len(raw) <= MAX_DATA_BYTES and size <= MAX_DATA_BYTES,
                 'program data exceeds its byte limit')
        result.append((raw, repeat))
    return result, size


def data_size(value):
    """Validate the exact data schema and byte bound without repetition allocation."""
    return _segments(value)[1]


def decode_data(value):
    """Create only the validated bytes. No path, expression or command is evaluated."""
    segments, _ = _segments(value)
    return b''.join(raw * repeat for raw, repeat in segments)
