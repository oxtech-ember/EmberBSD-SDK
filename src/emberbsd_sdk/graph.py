# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Deterministic dependency waves for SDK and Ports graphs."""

from .errors import SDKError


class DependencyCycle(SDKError):
    def __init__(self, remaining):
        self.remaining = remaining
        super().__init__('build plan has a dependency cycle: ' + ', '.join(sorted(remaining)))


def dependency_waves(dependencies, selected=None):
    """Order the selected nodes into waves, ignoring prerequisites outside the selection."""
    names = set(dependencies)
    selected = names if selected is None else set(selected)
    unknown = selected - names
    if unknown:
        raise SDKError('unknown graph nodes: ' + ', '.join(sorted(unknown)))
    remaining = {}
    reverse = {}
    for name in selected:
        required = set(dependencies[name])
        if required - names:
            raise SDKError(f'{name}: unknown dependencies: ' + ', '.join(sorted(required - names)))
        remaining[name] = required & selected
        for dependency in remaining[name]:
            reverse.setdefault(dependency, []).append(name)
    ready = sorted(name for name, required in remaining.items() if not required)
    waves = []
    while ready:
        waves.append(ready)
        next_ready = []
        for name in ready:
            del remaining[name]
            for consumer in reverse.get(name, ()):
                remaining[consumer].remove(name)
                if not remaining[consumer]:
                    next_ready.append(consumer)
        ready = sorted(next_ready)
    if remaining:
        raise DependencyCycle(remaining)
    return waves
