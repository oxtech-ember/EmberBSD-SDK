# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Literal input for finite fixtures with the explicitly selected mksh."""

import shlex

from .native_toolset import _require


def literal_stdin(value):
    """Use mksh builtins only. Preserve every UTF-8 byte without a final LF."""
    _require(isinstance(value, str) and '\0' not in value, 'unsupported literal shell input')
    return ('case "${KSH_VERSION-}" in *MIRBSD*) ;; *) exit 42;; esac; '
            'print -rn -- ' + shlex.quote(value))
