# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Full clean tracked-source materialization checks."""

import os
import re
import stat
from .errors import SDKError
from .paths import canonical_existing, entry
from .process import capture, git_environment


def git_source(root, *, kind=None, git='git', env=None):
    root = canonical_existing(root)
    env = git_environment(env)

    def invoke(*args):
        return capture([git, '-C', root, *args], env=env).stdout

    if os.fsdecode(invoke('rev-parse', '--show-toplevel').rstrip(b'\n')) != root or invoke('status', '--porcelain=v1', '--untracked-files=all').strip():
        raise SDKError(f'source must be a clean Git root: {root}')
    if invoke('config', '--bool', '--default', 'false', '--get', 'core.sparseCheckout').strip() == b'true':
        raise SDKError(f'sparse checkout is not a complete source tree: {root}')
    external = []
    for row in invoke('ls-files', '-v', '--stage', '-z').split(b'\0'):
        if not row:
            continue
        try:
            header, raw_name = row.split(b'\t', 1)
            tag, mode, obj, stage = header.decode('ascii').split(' ')
            name = raw_name.decode('utf-8', 'strict')
        except (ValueError, UnicodeError) as exc:
            raise SDKError(f'unsupported source index entry: {root}') from exc
        if tag != 'H':
            raise SDKError(f'source index must not hide tracked paths: {root}: {name}')
        if stage != '0' or re.fullmatch('[0-9a-f]{40}', obj) is None:
            raise SDKError(f'unsupported source index entry: {root}: {name}')
        if mode == '160000':
            if kind != 'ports' or name != 'upstream/pkgsrc':
                raise SDKError(f'unexpected source gitlink: {root}: {name}')
            external.append(dict(path=name, commit=obj, usage='not-used-by-selected-producer', verification='explicit-pkgsrc-verified-separately', qualification='not-assessed'))
            continue
        if mode not in ('100644', '100755', '120000'):
            raise SDKError(f'unsupported source index entry: {root}: {name}')
        info = entry(os.path.join(root, name))
        present = info is not None and (stat.S_ISLNK(info.st_mode) if mode == '120000' else stat.S_ISREG(info.st_mode))
        if not present:
            raise SDKError(f'tracked source path is not materialized: {root}: {name}')
        # Refuse a directory replaced by an ignored alias, even if final lstat is regular.
        current = root
        for part in name.split('/')[:-1]:
            current = os.path.join(current, part)
            ancestor = entry(current)
            if ancestor is None or not stat.S_ISDIR(ancestor.st_mode):
                raise SDKError(f'tracked source ancestor is not a real directory: {root}: {name}')
    sha = invoke('rev-parse', '--verify', 'HEAD^{commit}').decode('ascii').strip()
    if re.fullmatch('[0-9a-f]{40}', sha) is None:
        raise SDKError(f'expected a full Git commit: {root}')
    return dict(path=root, commit=sha, external_gitlinks=external)
