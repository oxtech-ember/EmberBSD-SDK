# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Per-run receipts for repeated input verification.

A run checks each input by content the first time.  A later check of the
same input in the same run compares a metadata fingerprint of every file
and directory below the declared inputs: device, inode, mode, size,
modification time and change time.  Writing a file, replacing it or
setting its times changes its change time, so any difference leads to a
full content verification again.  Receipts live only in memory and are
never written, so a new run always starts with content verification,
unless the run selects a SourceReceiptStore for its prepared sources.
"""

import copy
import hashlib
import json
import os
import stat
import tempfile


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
    if not os.path.isabs(value):
        return None
    try:
        root = os.path.realpath(value)
        digest = hashlib.sha256()
        _row(digest, value + '\0' + root, os.lstat(root))
        if not stat.S_ISDIR(os.lstat(root).st_mode):
            return digest.digest()
        pending = [root]
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                rows = sorted(entries, key=lambda item: item.name)
            for item in rows:
                info = item.stat(follow_symlinks=False)
                _row(digest, item.path, info)
                if stat.S_ISDIR(info.st_mode):
                    pending.append(item.path)
                elif stat.S_ISLNK(info.st_mode):
                    target = os.path.realpath(item.path)
                    if os.path.commonpath((target, root)) != root:
                        return None
    except (OSError, ValueError):
        return None
    return digest.digest()


class RunReceipts:
    """Results of content verifiers, valid only while their inputs are unchanged."""
    def __init__(self):
        self._entries = {}
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


class SourceReceiptStore:
    """Prepared-source records that a later run may reuse, when selected.

    A run selects a store directory explicitly; without one nothing is
    written and every run verifies by content.  An entry is reused only for
    the same verifier command, environment and Ports and SDK checkout records,
    and only while the metadata fingerprint of every declared input equals
    the one taken before its content verification: the same rule a run uses
    for its own later checks.  Any difference verifies by content again and
    replaces the entry.
    """
    FORMAT = 'emberbsd-source-receipt'

    def __init__(self, directory):
        if not isinstance(directory, str) or not os.path.isabs(directory):
            raise ValueError('source receipt directory must be absolute')
        os.makedirs(directory, mode=0o755, exist_ok=True)
        if os.path.islink(directory) or not os.path.isdir(directory):
            raise ValueError('source receipt directory must be a real directory')
        self.directory = os.path.realpath(directory)

    @staticmethod
    def _encode(value):
        return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')

    def _path(self, material):
        return os.path.join(self.directory, hashlib.sha256(self._encode(material)).hexdigest() + '.json')

    def load(self, material, current):
        """Return the stored record, or None when it must be verified by content."""
        if current is None:
            return None
        try:
            with open(self._path(material), 'rb') as handle:
                saved = json.loads(handle.read(1 << 22))
        except (OSError, ValueError):
            return None
        if (not isinstance(saved, dict) or saved.get('format') != self.FORMAT or saved.get('version') != 1 or
                saved.get('material') != json.loads(self._encode(material)) or saved.get('fingerprint') != current):
            return None
        return copy.deepcopy(saved.get('record'))

    def save(self, material, current, record):
        if current is None:
            return
        data = self._encode(dict(format=self.FORMAT, version=1, material=material,
                                 fingerprint=current, record=record))
        fd, temporary = tempfile.mkstemp(prefix='.receipt-', dir=self.directory)
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path(material))
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
