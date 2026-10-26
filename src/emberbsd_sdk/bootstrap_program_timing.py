# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Bounded owned-command call observations. No clock, executor or native hooks."""

from .native_toolset import _require

MAX_ELAPSED_NS = 120_000_000_000


def limits(case):
    """Return optional strict nanosecond bounds from one owner case."""
    if case is None or 'elapsed' not in case:
        return None
    value = case['elapsed']
    _require(isinstance(value, dict) and set(value) == {'min_ns', 'max_ns'} and
             all(type(v) is int for v in value.values()) and
             0 <= value['min_ns'] <= value['max_ns'] <= MAX_ELAPSED_NS and
             value['max_ns'] > 0, 'invalid owned-command elapsed bounds')
    return value


def observation(start, end):
    return dict(clock='monotonic_ns', scope='owned-command-call',
                start_ns=start, end_ns=end, elapsed_ns=end - start)


def verify(case, row):
    """Verify current or retained evidence against the exact owner interval."""
    bounds = limits(case)
    if bounds is None:
        _require('elapsed' not in row, 'unexpected owned-command elapsed evidence')
        return
    value = row.get('elapsed')
    _require(isinstance(value, dict) and set(value) ==
             {'clock', 'scope', 'start_ns', 'end_ns', 'elapsed_ns'} and
             value['clock'] == 'monotonic_ns' and value['scope'] == 'owned-command-call' and
             all(type(value[k]) is int for k in ('start_ns', 'end_ns', 'elapsed_ns')) and
             0 <= value['start_ns'] <= value['end_ns'] and
             value['elapsed_ns'] == value['end_ns'] - value['start_ns'] and
             bounds['min_ns'] <= value['elapsed_ns'] <= bounds['max_ns'],
             'owned-command elapsed observation is missing, invalid or outside bounds')
