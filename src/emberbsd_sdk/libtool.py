# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted); GNU Libtool configuration output parsing.
"""Parse default and selected-tag scalar fields without evaluating shell code."""

import re
import shlex
from .errors import SDKError

LIBTOOL_CONFIG_API = 1
_FIELDS = ('host', 'host_os', 'CC', 'build_libtool_libs')
_MARKER = re.compile(r'# ### (BEGIN|END) LIBTOOL TAG CONFIG: ([-_A-Za-z0-9,/]+)')


def parse_libtool_config(text, tag):
    """Return defaults, explicit overrides and effective fields for one tag.

    GNU Libtool 2.6.2 prints unmarked defaults before a selected tag block.
    The CC tag uses only defaults. Every required scalar is unique in its
    scope. This function does not validate target or compiler identities.
    """
    if type(text) is not str or '\0' in text:
        raise SDKError('Libtool configuration must be text without NUL')
    if type(tag) is not str or re.fullmatch(r'[-_A-Za-z0-9,/]+', tag) is None:
        raise SDKError('Invalid Libtool tag name')
    lines = text.splitlines()
    markers = []
    for index, line in enumerate(lines):
        if line.lstrip().startswith('# ###') and 'LIBTOOL' in line:
            match = _MARKER.fullmatch(line)
            if match is None:
                raise SDKError('Unknown Libtool configuration marker')
            markers.append((index, match.group(1), match.group(2)))
    if tag == 'CC':
        if markers:
            raise SDKError('The CC configuration must contain defaults only')
        defaults_text, selected_text = text, ''
    else:
        if len(markers) != 2 or [(kind, name) for _, kind, name in markers] != [('BEGIN', tag), ('END', tag)]:
            raise SDKError('Require exactly one complete selected Libtool tag')
        begin, end = markers[0][0], markers[1][0]
        if begin >= end or any(line.strip() for line in lines[end + 1:]):
            raise SDKError('Invalid Libtool tag order or trailing output')
        defaults_text = '\n'.join(lines[:begin])
        selected_text = '\n'.join(lines[begin + 1:end])

    def fields(scope, required):
        try:
            lexer = shlex.shlex(scope, posix=True, punctuation_chars=';&|<>()')
            lexer.whitespace_split = True
            lexer.commenters = '#'
            words = list(lexer)
        except ValueError as error:
            raise SDKError('Invalid quoted Libtool configuration') from error
        result = {}
        for word in words:
            match = re.fullmatch(r'([A-Za-z_][A-Za-z0-9_]*)=(.*)', word, re.S)
            if match is None:
                raise SDKError('Libtool configuration must contain scalar assignments')
            name, value = match.groups()
            if name in _FIELDS:
                if name in result or not value:
                    raise SDKError('Empty or duplicate Libtool field: ' + name)
                result[name] = value
        if any(name not in result for name in required):
            raise SDKError('Missing required Libtool configuration field')
        return result

    defaults = fields(defaults_text, _FIELDS)
    overrides = {} if tag == 'CC' else fields(selected_text, ('CC',))
    effective = {name: overrides[name] if name in overrides else defaults[name] for name in _FIELDS}
    return {'tag': tag, 'defaults': defaults, 'overrides': overrides, 'effective': effective}
