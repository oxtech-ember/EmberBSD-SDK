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

# Per-target ABI flags are shared by application compiler bindings. Keep these
# aligned with the OS target ABI; they are not compiler-family fallbacks.
TARGET_COMPILER_FLAGS = {
    'aarch64': (),
    'amd64': (),
    'riscv32': ('-march=rv32imac_zicsr', '-mabi=ilp32'),
}


def target_definition(name):
    if not isinstance(name, str) or name not in TARGETS:
        raise SDKError('target must be aarch64, amd64 or riscv32')
    return TARGETS[name]


def target_compiler_flags(name):
    if not isinstance(name, str) or name not in TARGET_COMPILER_FLAGS:
        raise SDKError('target must be aarch64, amd64 or riscv32')
    return TARGET_COMPILER_FLAGS[name]
