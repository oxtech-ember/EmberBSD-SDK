# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Read the running host's CPU architecture independently of its board family."""

import platform
import re

from . import process
from .errors import SDKError
from .host_services import _service_path


def native_cpu():
    """NetBSD uname -m names a machine port, while its -p names the CPU ABI.

    Use the existing OS-service route, not the build PATH or a board-to-CPU
    guess. Other hosts retain their established platform.machine() identity.
    https://man.netbsd.org/uname.1
    """
    if platform.system() != 'NetBSD':
        return platform.machine()
    result = process.capture([_service_path('uname'), '-p'], env={'LC_ALL': 'C'})
    try:
        cpu = result.stdout.decode('ascii').strip()
    except UnicodeDecodeError as error:
        raise SDKError('NetBSD uname -p returned an invalid CPU architecture') from error
    if not re.fullmatch(r'[A-Za-z0-9_]+', cpu) or cpu.lower() == 'unknown':
        raise SDKError('NetBSD uname -p returned an invalid CPU architecture')
    return cpu
