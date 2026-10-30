# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD; AI-assisted shared target definitions.
"""Canonical target names. These values do not qualify a payload."""

from .errors import SDKError

TARGETS = {
    'aarch64': dict(machine='evbarm', arch='aarch64', triplet='aarch64--netbsd', elf=183),
    'amd64': dict(machine='amd64', arch='x86_64', triplet='x86_64--netbsd', elf=62),
    'riscv32': dict(machine='riscv', arch='riscv32', triplet='riscv32--netbsd', elf=243),
}


def target_definition(name):
    if not isinstance(name, str) or name not in TARGETS:
        raise SDKError('target must be aarch64, amd64 or riscv32')
    return TARGETS[name]
