#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD contributors (AI-assisted).
"""Causal checks for selected archive and patch metadata."""
import hashlib
from pathlib import Path
import tempfile

from emberbsd_sdk import archive_metadata as api
from emberbsd_sdk.errors import SDKError

with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    (root / 'patches').mkdir()
    (root / 'Makefile').write_text('DISTNAME= example\n')
    patch = b'$NetBSD$\n\n--- a\n+++ a\n'
    (root / 'patches/patch-a').write_bytes(patch)
    checksum = hashlib.sha1(b'\n--- a\n+++ a\n').hexdigest()
    info = (f'BLAKE2s (example.tar.gz) = {"a" * 64}\n'
            f'SHA512 (example.tar.gz) = {"b" * 128}\n'
            f'Size (example.tar.gz) = 3 bytes\nSHA1 (patch-a) = {checksum}\n')
    distinfo = root / 'distinfo'
    distinfo.write_text(info)
    metadata, patches = api.read_recipe(root, 'example.tar.gz', 'example-root')
    assert metadata['archive_root'] == 'example-root'
    assert metadata['size'] == 3 and len(patches) == 1

    def reject(name='example.tar.gz', archive_root='example-root'):
        try:
            api.read_recipe(root, name, archive_root)
        except SDKError:
            return
        raise AssertionError('invalid metadata was accepted')

    distinfo.write_text(info + 'SHA512 (example.tar.gz) = INVALID\n')
    reject()
    distinfo.write_text(info + 'SHA1 (patch-missing) = INVALID\n')
    reject()
    distinfo.write_text(info)
    reject(archive_root='bad\nname')
    reject(name='bad\x00name')
    (root / 'patches/patch-extra').write_bytes(b'x')
    reject()
    (root / 'patches/patch-extra').unlink()
    (root / 'patches/patch-a').write_bytes(b'changed')
    reject()
print('PASS: valid metadata and malformed duplicate, patch and path rejections')
