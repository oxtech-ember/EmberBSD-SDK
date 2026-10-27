# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Read an explicit macOS ABI SDK without executing Apple tools."""
import hashlib
import os
from pathlib import Path
import plistlib
import re
from xml.parsers.expat import ExpatError
from .errors import SDKError
from . import paths

DARWIN_SDK_API = 1
_VERSION = r'[0-9]+(?:\.[0-9]+){0,2}'


def _absolute(value):
    if not os.path.isabs(os.fspath(value)):
        raise SDKError('select an absolute Darwin metadata path: ' + str(value))
    return Path(paths.canonical_existing(value))


def _plist(path, inputs, *, root=None):
    selected = _absolute(path)
    if root is not None and not selected.is_relative_to(root):
        raise SDKError('Darwin SDK metadata escapes the selected SDK: ' + str(path))
    paths.regular_file(selected)
    try:
        data = selected.read_bytes()
        record = plistlib.loads(data)
    except (OSError, ValueError, AttributeError, plistlib.InvalidFileException, ExpatError) as error:
        raise SDKError('invalid Darwin metadata plist: ' + str(selected)) from error
    if not isinstance(record, dict):
        raise SDKError('Darwin metadata plist must contain a dictionary: ' + str(selected))
    inputs[str(selected)] = hashlib.sha256(data).hexdigest()
    return record


def _field(record, name, pattern):
    value = record.get(name)
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise SDKError('missing or invalid Darwin metadata field: ' + name)
    return value


def _version(value):
    return tuple((list(map(int, value.split('.'))) + [0, 0])[:3])


def read_metadata(macos_sdk, *, xcode_version_plist):
    """Return measured metadata; compiler and native tools stay separate."""
    sdk = _absolute(macos_sdk)
    paths.real_directory(sdk)
    inputs = {}
    settings = _plist(sdk / 'SDKSettings.plist', inputs, root=sdk)
    system = _plist(sdk / 'System/Library/CoreServices/SystemVersion.plist', inputs, root=sdk)
    xcode = _plist(xcode_version_plist, inputs)
    version = _field(settings, 'Version', _VERSION)
    name = _field(settings, 'CanonicalName', 'macosx' + _VERSION)
    product = _field(system, 'ProductVersion', _VERSION)
    if _version(name[len('macosx'):]) != _version(version) or _version(product) != _version(version):
        raise SDKError('macOS SDK metadata versions disagree')
    result = dict(format='emberbsd-darwin-sdk-metadata', version=1,
        sdk_path=str(sdk), sdk_version=version, sdk_name=name,
        sdk_build=_field(system, 'ProductBuildVersion', r'[A-Za-z0-9]+'),
        xcode_version=_field(xcode, 'CFBundleShortVersionString', _VERSION),
        xcode_build=_field(xcode, 'ProductBuildVersion', r'[A-Za-z0-9]+'), inputs=inputs)
    for path, digest in inputs.items():
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != digest:
            raise SDKError('Darwin metadata changed while reading: ' + path)
    return result
