# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Use the OS install producer to append native METALOG records."""

import os
import stat

from . import paths
from .errors import SDKError


class NativeRuntimeInstaller:
    """Materialize verified rows inside an existing unprivileged distribution.

    The caller selects the native OS install executable once and owns the
    destination writer throughout. ``run(argv)`` executes it in that selected
    environment and must report failures; no shell or per-file tool selection
    is added here. The shared runtime copier retains final mode verification.
    """

    def __init__(self, destination, tools, os_source, run):
        self.root = paths.path_string(destination)
        paths.real_directory(self.root)
        if paths.canonical_existing(self.root) != self.root:
            raise SDKError('native runtime install requires real destination ancestors')
        executable = tools.get('install') if isinstance(tools, dict) else None
        if (not isinstance(executable, str) or not os.path.isabs(executable) or
                not os.path.isfile(executable) or not os.access(executable, os.X_OK)):
            raise SDKError('native runtime install requires the selected OS install executable')
        database = os.path.join(paths.path_string(os_source), 'etc')
        paths.real_directory(database)
        metalog = os.path.join(self.root, 'METALOG')
        info = paths.entry(metalog)
        if info is None or not stat.S_ISREG(info.st_mode):
            raise SDKError('native runtime install requires the existing distribution METALOG')
        self.command = (executable, '-U', '-M', metalog, '-D', self.root,
                        '-N', database, '-o', 'root', '-g', 'wheel')
        if not callable(run):
            raise SDKError('native runtime install requires an execution callback')
        self.run = run

    def _path(self, path):
        path = paths.path_string(path)
        if os.path.commonpath((path, self.root)) != self.root:
            raise SDKError('native runtime install destination escapes its root')
        parent = os.path.dirname(path) if path != self.root else path
        paths.real_directory(parent)
        if paths.canonical_existing(parent) != parent:
            raise SDKError('native runtime install destination has a linked ancestor')
        return path

    def _invoke(self, mode, options, arguments):
        if type(mode) is not int or not 0 <= mode <= 0o777:
            raise SDKError('native runtime install requires ordinary permission bits')
        result = self.run((*self.command, *options, '-m', f'{mode:04o}', '--', *arguments))
        if getattr(result, 'returncode', 0) != 0:
            raise SDKError('native runtime install command failed')

    def directory(self, path, mode):
        path = self._path(path)
        info = paths.entry(path)
        if info is not None:
            if not stat.S_ISDIR(info.st_mode):
                raise SDKError('native runtime install directory conflicts with an existing path')
            return
        self._invoke(mode, ('-d',), (path,))

    def file(self, source, destination, mode):
        destination = self._path(destination)
        if paths.entry(destination) is not None:
            raise SDKError('native runtime install refuses an existing file destination')
        self._invoke(mode, ('-p', '-h', 'sha256'), (paths.path_string(source), destination))

    def link(self, target, destination, mode):
        destination = self._path(destination)
        if paths.entry(destination) is not None:
            raise SDKError('native runtime install refuses an existing link destination')
        if not isinstance(target, str) or not target or '\0' in target:
            raise SDKError('native runtime install requires a literal link target')
        # -l sr would resolve/collapse the manifest's link chain.
        self._invoke(mode, ('-l', 's'), (target, destination))
