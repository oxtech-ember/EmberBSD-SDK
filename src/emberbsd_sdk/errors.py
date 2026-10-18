# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Shared host-tool refusal type."""


class SDKError(Exception):
    """An input, producer, or inspection failed its explicit contract."""
