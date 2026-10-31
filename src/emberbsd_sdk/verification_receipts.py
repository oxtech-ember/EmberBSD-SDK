# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Per-run receipts for repeated input verification.

A run checks each input by content the first time.  A later check of the
same input in the same run compares a metadata fingerprint of the named
files: device, inode, mode, size, modification time and change time.  The
inputs are input-key files, such as a stage manifest, and the executables
of the verifier.  A directory is never walked: a stage is identified by its
manifest, not by its files.  Receipts live only in memory and are never
written, so a new run always starts with content verification.  Clean
source checkouts are checked once per run (RunReceipts.once) and are not
checked again at the end of the build.
"""

import copy
import hashlib
import os
import stat


def _row(digest, name, info):
    digest.update(os.fsencode(name) + b'\0' + ' '.join(str(value) for value in (
        info.st_mode, info.st_dev, info.st_ino, info.st_size,
        info.st_mtime_ns, info.st_ctime_ns)).encode('ascii') + b'\n')


def fingerprint(inputs, memo=None):
    """Return a metadata fingerprint of the inputs, or None if one is unsafe.

    None means the caller must verify content: an input is missing or a
    symbolic link inside it points outside the declared inputs.
    """
    digest = hashlib.sha256()
    for value in sorted(set(inputs)):
        if memo is not None and value in memo:
            part = memo[value]
        else:
            part = _fingerprint_one(value)
            if memo is not None:
                memo[value] = part
        if part is None:
            return None
        digest.update(part)
    return digest.hexdigest()


def _fingerprint_one(value):
    """Metadata of one named file. A directory is not walked: its input key
    file names it, so a directory input gets no fingerprint."""
    if not os.path.isabs(value):
        return None
    try:
        root = os.path.realpath(value)
        info = os.lstat(root)
    except (OSError, ValueError):
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    digest = hashlib.sha256()
    _row(digest, value + '\0' + root, info)
    return digest.digest()


class RunReceipts:
    """Results of content verifiers, valid only while their inputs are unchanged."""
    def __init__(self):
        self._entries = {}
        self._once = {}
        self.content_checks = {}

    def verify(self, key, inputs, verifier, memo=None):
        before = fingerprint(inputs, memo)
        saved = self._entries.get(key)
        if before is not None and saved is not None and saved[0] == before:
            return copy.deepcopy(saved[1])
        self.content_checks[key] = self.content_checks.get(key, 0) + 1
        self._entries.pop(key, None)
        result = verifier()
        # The fingerprint was taken before reading, so a change during the
        # content check also differs from it next time.
        if before is not None:
            self._entries[key] = (before, copy.deepcopy(result))
        return result

    def once(self, key, verifier):
        """Run the verifier once per run, for example for the clean source
        checkouts. A failed verifier leaves no record."""
        if key in self._once:
            return copy.deepcopy(self._once[key])
        self.content_checks[key] = self.content_checks.get(key, 0) + 1
        self._once.pop(key, None)
        result = verifier()
        self._once[key] = copy.deepcopy(result)
        return result
