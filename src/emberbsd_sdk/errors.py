# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Shared host-tool refusal type."""


class SDKError(Exception):
    """An input, producer, or inspection failed its explicit contract."""


class CommandError(SDKError):
    """A completed producer failed. Keep its exact status and captured bytes."""

    def __init__(self, returncode, argv, stdout=b'', stderr=b'', log=None):
        self.returncode = returncode
        self.argv = tuple(argv)
        self.stdout = stdout
        self.stderr = stderr
        self.log = log
        detail = f'; inspect {log}' if log is not None else ': ' + stderr.decode('utf-8', 'replace').strip()
        super().__init__(f'command failed ({returncode}): {self.argv!r}{detail}')
