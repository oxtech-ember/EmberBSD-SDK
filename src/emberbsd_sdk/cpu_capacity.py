# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Resolve the number of CPUs available to this process, without load sampling."""

import os

from .errors import SDKError


def _positive_count(value):
    return value if type(value) is int and value > 0 else None


def available_cpu_count():
    """Return available CPUs, preferring process limits and then host count."""
    process_count = getattr(os, 'process_cpu_count', None)
    if callable(process_count):
        try:
            count = _positive_count(process_count())
        except (OSError, NotImplementedError):
            count = None
        if count is not None:
            return count

    affinity = getattr(os, 'sched_getaffinity', None)
    if callable(affinity):
        try:
            count = _positive_count(len(affinity(0)))
        except (OSError, NotImplementedError):
            count = None
        if count is not None:
            return count

    return _positive_count(os.cpu_count())


def resolve_jobs(override=None, *, option='--jobs'):
    """Resolve an optional positive integer override to the available CPU count."""
    if override is None:
        count = available_cpu_count()
        if count is None:
            raise SDKError(f'CPU count is unavailable; select {option} explicitly')
        return count
    if type(override) is int:
        count = override
    elif isinstance(override, str) and override.isascii() and override.isdecimal():
        try:
            count = int(override)
        except ValueError:
            count = 0
    else:
        count = 0
    if count <= 0:
        name = option.removeprefix('--')
        raise SDKError(f'{name} must be a positive integer')
    return count
