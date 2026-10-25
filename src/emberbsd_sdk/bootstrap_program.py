# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Fixed OS-owned native bootstrap programs. Never accept arbitrary argv."""

import hashlib
import os
from pathlib import Path
import re
import shlex
import socket

from .bootstrap_producer import PRODUCER_FORMAT
from .bootstrap_identity import IDENTITY_OUTPUTS, identity_output
from .bootstrap_program_data import decode_data, data_size, MAX_FILE_BYTES
from . import bootstrap_program_fs as filesystem
from . import bootstrap_compat_component as component
from .bootstrap_shell import literal_stdin
from .bootstrap_gnu_program import GNU_PROGRAM_STAGES
from .bootstrap_compat import (EFUN_PROGRAMS, EFUN_PROGRAM_STAGES,
                               SOURCES as _EFUN_SOURCES, HEADERS as _EFUN_HEADERS,
                               PROVENANCE as _EFUN_PROVENANCE)
from .inventory import sha256_file, snapshot_tree
from .native_toolset import MAX_JSON, _absolute, _bytes, _envelope, _json, _require, _source
from .paths import canonical_existing, entry

BOOTSTRAP_PROGRAM_API = 1
MANIFEST_RELATIVE = 'tools/grep/bootstrap.json'
MANIFEST_SHA256 = 'cdb8f56fd1107b1cc9ccdbe4cc352599e8575b7984f3c6b6a074426c71e56d3e'
_SOURCES = tuple('usr.bin/grep/' + name + '.c' for name in ('fastgrep', 'file', 'grep', 'queue', 'util'))
_PROVENANCE = ('tools/grep/Makefile', 'tools/Makefile.host', 'usr.bin/grep/Makefile', 'tools/grep/README.md')
PROGRAM_STAGES = (component.STAGE, 'grep-bootstrap', 'cmp-bootstrap', 'hostname-bootstrap',
                  'basename-bootstrap', 'dirname-bootstrap',
                  'cat-bootstrap', 'head-bootstrap', 'uniq-bootstrap', 'wc-bootstrap', 'tr-bootstrap', 'printf-bootstrap', 'pwd-bootstrap', 'cut-bootstrap', 'env-bootstrap', 'uname-bootstrap', 'tail-bootstrap', 'sort-bootstrap', 'date-bootstrap', 'id-bootstrap', 'expr-bootstrap', 'hexdump-bootstrap',
                  *(name + '-bootstrap' for name in filesystem.PROGRAMS))
CMP_MANIFEST_SHA256 = 'a3267d3387b08780fb1e372f5a742dccd9faa84568292506d3389f35e869ad56'
HOSTNAME_MANIFEST_SHA256 = '32450fa3c2d68e63c13f1d32795f4d3e14cab8b5beac909ee27242cef279750d'
_CMP_SOURCES = tuple('usr.bin/cmp/' + name + '.c' for name in ('cmp', 'misc', 'regular', 'special'))
_HOSTNAME_SOURCES = ('bin/hostname/hostname.c',)
_BOOTSTRAP_CDEFS = 'tools/compat/bootstrap_cdefs.h'
BASENAME_MANIFEST_SHA256 = '28d250d01b132c2fab6619094ebd2a00862baafb3610d759a0d84ad53874bd46'
DIRNAME_MANIFEST_SHA256 = '581b5d3fced84f25c3b185de626ff78a7eb45009c01b916a255438655ba1dd47'
CAT_MANIFEST_SHA256 = 'f9db202d70a9cb0b39a0486f14f137d3fb29b1c00ed75fcffe93aaf86c5faf1a'
HEAD_MANIFEST_SHA256 = '34f117e1bbcc6801597c85c682b5252075b939815527b9c47b2a04e79fbddfe6'
UNIQ_MANIFEST_SHA256 = '88376a81e9c96b455840a0fb636912442cba7325cee89fbbe9df8eb43401236e'
WC_MANIFEST_SHA256 = '38e7672e67b4b423062596252fde87ad4b09ee4977d61f53526214d0e8d6ae88'
TR_MANIFEST_SHA256 = '88114937d4d600fd4c228836657f03c296ffca4badb5dea446cc67768ac6c05b'
PRINTF_MANIFEST_SHA256 = 'f21cac7215af7aeaf2a57e0eacc78dadc67c63d934e14bf1cf6f521a02da4282'
CUT_MANIFEST_SHA256 = '169b46d73ce9b36094bd5589bc6def4a061f78fe5d9d3c2579641d41f5c181dc'
PWD_MANIFEST_SHA256 = '598906e66805438e5386f6f12478d69147e36d020849dec655d7a1826026a6bd'
ENV_MANIFEST_SHA256 = 'b16ad21f0c3b820ba1345c40606f42ee379eb92c3d098448e0e61b5c83b39846'
UNAME_MANIFEST_SHA256 = '668f39859e9bcf7995f9402e046bf1598527f2db294515a40dceb895ca97148d'
TAIL_MANIFEST_SHA256 = '15ab76bc438699a789dc94351e71151301b2560072345243040aa446028f34c8'
SORT_MANIFEST_SHA256 = 'ed61a6a5caae08acd3334bf5769f22df82930cc2a7fd4ae8cdfcf4abffdfd2db'
DATE_MANIFEST_SHA256 = '51840761173803ca69439053d8ee5fa72c4b331d7cb6ddf729f54c9eb4cfd8c8'
ID_MANIFEST_SHA256 = '7184d420bdd70778b1c290f028bc43df9441db16087657d06915c2b9d3fc2e55'
_ID_GROUP_HEADER = 'common/include/libutil/compat_grouplist.h'
EXPR_MANIFEST_SHA256 = '505c27cdc0db4b2d22b6f348559e91735bf723b1f47251d6e98fa3d979e71854'
HEXDUMP_MANIFEST_SHA256 = '9fe663af4124f714bcac65dce3bb7666bef0af2f74a9f97fb638699c8c42d974'
MKDIR_MANIFEST_SHA256 = '6d52c3dd9fe813f652e7d98110f56af8078b9617d6d1f3ca0b3b08edf73936a9'
MKDIR_LEGACY_MANIFEST_SHA256 = '111fe0d327e4ccdc7de008ac3844e49893edfbd43d62400ef2dbc4ceb76c5453'
MKDIR_MANIFEST_CASE_COUNTS = {MKDIR_MANIFEST_SHA256: 11, MKDIR_LEGACY_MANIFEST_SHA256: 10}
_FILESYSTEM_MANIFESTS = {
    'chmod': '0139b41d150bb82e79a99547c4d3a6f5cac975152c32e850922af7bdb269e1ae',
    'mkdir': MKDIR_MANIFEST_SHA256,
    'ln': '3b3fc97c04775eb92f447b3b466b2f2867002fba2736da33971a441d630260e5',
    'rmdir': '22e24af317a8e1c18559c407f0497a18f24bfb9c88ff8a89b48a9aa09b3ae995',
    'rm': '8ae57707433b60bb726b4b070d3389abc775514fc663064dca75180b816342fc'}
_NBTOOL_CONFIG = 'tools/compat/bootstrap/nbtool_config.h'
_NATIVE_LOCAL_HEADERS = {'cmp': ('extern.h',), 'tr': ('extern.h',), 'tail': ('extern.h',), 'hexdump': ('hexdump.h',),
                         'date': ('extern.h',), 'cut': ('x_cut.c',), 'sort': ('sort.h', 'fsort.h', 'pathnames.h')}
_NATIVE_DIRECTORIES = {**{name: 'bin/' + name for name in filesystem.PROGRAMS}, 'hexdump': 'usr.bin/hexdump', 'cmp': 'usr.bin/cmp', 'hostname': 'bin/hostname',
                       'basename': 'usr.bin/basename', 'dirname': 'usr.bin/dirname',
                       'cat': 'bin/cat', 'head': 'usr.bin/head', 'uniq': 'usr.bin/uniq',
                       'wc': 'usr.bin/wc', 'tr': 'usr.bin/tr', 'printf': 'usr.bin/printf', 'pwd': 'bin/pwd', 'cut': 'usr.bin/cut', 'env': 'usr.bin/env', 'uname': 'usr.bin/uname', 'tail': 'usr.bin/tail', 'sort': 'usr.bin/sort', 'date': 'bin/date', 'id': 'usr.bin/id', 'expr': 'bin/expr'}
_NATIVE_SOURCES = {**{name: ('bin/' + name + '/' + name + '.c',) for name in filesystem.PROGRAMS}, 'hexdump': (*('usr.bin/hexdump/' + name + '.c' for name in
                              ('conv', 'display', 'hexdump', 'hexsyntax', 'odsyntax', 'parse')), *_EFUN_SOURCES),
                   'expr': ('bin/expr/expr.y',), 'date': ('bin/date/date.c',), 'sort': (*('usr.bin/sort/' + name + '.c' for name in
                           ('append', 'fields', 'files', 'fsort', 'init', 'msort', 'sort', 'tmp', 'radix_sort')),
                           *_EFUN_SOURCES),
                   'tail': tuple('usr.bin/tail/' + name + '.c' for name in
                           ('forward', 'misc', 'read', 'reverse', 'tail')),
                   'cmp': _CMP_SOURCES, 'hostname': _HOSTNAME_SOURCES, 'cat': ('bin/cat/cat.c',),
                   'tr': ('usr.bin/tr/str.c', 'usr.bin/tr/tr.c'), 'pwd': ('bin/pwd/pwd.c',), 'cut': ('usr.bin/cut/cut.c', *_EFUN_SOURCES),
                   **{name: ('usr.bin/' + name + '/' + name + '.c',)
                      for name in ('basename', 'dirname', 'head', 'uniq', 'wc', 'printf', 'env', 'uname', 'id')}}
_LITERAL_CASE_COUNTS = dict(chmod=12, mkdir=11, ln=12, rmdir=9, rm=25, basename=14, dirname=11, cat=18, head=16, uniq=16, wc=18, tr=18, printf=24, pwd=10, cut=22, env=19, uname=15, tail=31, sort=36, date=41, id=31, expr=52, hexdump=37)
_RUNTIME_ARGS = {'grep': ('-q', 'needle', 'input.txt'), 'cmp': ('-s', 'same.txt', 'same.txt'),
                 'hostname': ()}
_ALIASES = {'grep': ('egrep', 'fgrep'), 'tail': ('tac',), 'id': ('groups', 'whoami'), 'hexdump': ('od',)}
_UNAME_FIELDS = ('sysname', 'nodename', 'release', 'version', 'machine', 'machine_arch')


def _machine_arch():
    """Map the admitted Darwin native machine to Apple's processor-family spelling."""
    try:
        current = os.uname()
    except OSError as exc:
        _require(False, 'controller machine architecture API failed: ' + str(exc))
    _require(current.sysname == 'Darwin', 'processor-family oracle requires Darwin')
    families = {'arm': b'arm', 'arm64': b'arm', 'aarch64': b'arm',
                'i386': b'i386', 'x86_64': b'i386', 'amd64': b'i386',
                'ppc': b'powerpc', 'ppc64': b'powerpc',
                'powerpc': b'powerpc', 'powerpc64': b'powerpc'}
    _require(current.machine in families, 'unsupported Darwin processor architecture')
    return families[current.machine]


def _uname_output(fields):
    """Keep the source-owned field order and exact native bytes, including whitespace."""
    current = os.uname()
    return b' '.join(_machine_arch() if field == 'machine_arch' else
                     os.fsencode(getattr(current, field)) for field in fields) + b'\n'


def _literal_cases(manifest, count, *, work_root_output=False, uname_output=False, identity_oracle=False, filesystem_cases=False):
    """Check finite stream-case structure. The pinned owner supplies semantics."""
    cases = manifest['runtime_cases']
    _require(isinstance(cases, list) and len(cases) == count,
             'unsupported fixed program case count')
    names = set()
    for case in cases:
        output_key = ('stdout_work_root' if work_root_output and isinstance(case, dict) and
                      'stdout_work_root' in case else 'stdout_uname' if uname_output and
                      isinstance(case, dict) and 'stdout_uname' in case else 'stdout_identity' if identity_oracle and
                      isinstance(case, dict) and 'stdout_identity' in case else 'stdout')
        base = {'name', 'invocation', 'args', 'stdin', 'exit', output_key}
        if filesystem_cases:
            base.add('filesystem')
        _require(isinstance(case, dict) and
                 set(case) in (base | {'stderr'}, base | {'stderr_nonempty'}),
                 'unsupported literal program case fields')
        _require(isinstance(case['name'], str) and re.fullmatch(r'[a-z][a-z0-9-]*', case['name']) and
                 case['name'] not in names and case['invocation'] in manifest['invocations'] and
                 isinstance(case['args'], list) and
                 all(isinstance(word, str) and '\0' not in word for word in case['args']) and
                 isinstance(case['stdin'], str) and
                 (data_size(case['stdout']) >= 0 if output_key == 'stdout' else
                  case['stdout_work_root'] is True and case['exit'] == 0 if output_key == 'stdout_work_root' else
                  isinstance(case['stdout_uname'], list) and bool(case['stdout_uname']) and
                  all(field in _UNAME_FIELDS for field in case['stdout_uname']) and
                  len(set(case['stdout_uname'])) == len(case['stdout_uname']) and case['exit'] == 0
                  if output_key == 'stdout_uname' else
                  isinstance(case['stdout_identity'], str) and case['stdout_identity'] in IDENTITY_OUTPUTS and case['exit'] == 0) and
                 type(case['exit']) is int and 0 <= case['exit'] <= 255 and
                 (isinstance(case['stderr'], str) if 'stderr' in case else case['stderr_nonempty'] is True),
                 'invalid literal program case or output path')
        if filesystem_cases:
            _require(case['name'] != 'runtime-observation', 'reserved filesystem case name')
            filesystem.validate(case['filesystem'])
        names.add(case['name'])


def _profile(plan):
    stage = plan.get('stage')
    if stage == component.STAGE:
        return component.MANIFEST, component.DIGEST, component.SOURCES, component.PROVENANCE
    _require(stage in PROGRAM_STAGES, 'unsupported direct-source bootstrap program stage')
    if stage == 'grep-bootstrap':
        return MANIFEST_RELATIVE, MANIFEST_SHA256, _SOURCES, _PROVENANCE
    name = stage.removesuffix('-bootstrap')
    directory = _NATIVE_DIRECTORIES[name]
    digest = {**_FILESYSTEM_MANIFESTS, 'hexdump': HEXDUMP_MANIFEST_SHA256, 'cmp': CMP_MANIFEST_SHA256, 'hostname': HOSTNAME_MANIFEST_SHA256,
              'basename': BASENAME_MANIFEST_SHA256, 'dirname': DIRNAME_MANIFEST_SHA256,
              'cat': CAT_MANIFEST_SHA256, 'head': HEAD_MANIFEST_SHA256, 'uniq': UNIQ_MANIFEST_SHA256,
              'wc': WC_MANIFEST_SHA256, 'tr': TR_MANIFEST_SHA256,
              'printf': PRINTF_MANIFEST_SHA256, 'pwd': PWD_MANIFEST_SHA256, 'cut': CUT_MANIFEST_SHA256,
              'env': ENV_MANIFEST_SHA256, 'uname': UNAME_MANIFEST_SHA256, 'tail': TAIL_MANIFEST_SHA256, 'sort': SORT_MANIFEST_SHA256, 'date': DATE_MANIFEST_SHA256, 'id': ID_MANIFEST_SHA256, 'expr': EXPR_MANIFEST_SHA256}[name]
    if name == 'mkdir' and 'program' in plan:
        _require(isinstance(plan['program'], dict), 'unsupported mkdir program input')
        if 'manifest' in plan['program']:
            reference = plan['program']['manifest']
            _require(isinstance(reference, dict), 'unsupported mkdir manifest reference')
            digest = reference.get('sha256')
            _require(isinstance(digest, str) and digest in MKDIR_MANIFEST_CASE_COUNTS,
                     'unsupported mkdir manifest digest')
    return ('tools/' + name + '/bootstrap.json',
            digest, _NATIVE_SOURCES[name],
            ('tools/' + name + '/Makefile', 'tools/Makefile.host', directory + '/Makefile',
             'tools/' + name + '/README.md', *(_EFUN_PROVENANCE if name in EFUN_PROGRAMS else ()),
             *(('tools/rm/test-source.py',) if name == 'rm' else ())))


def _native_program_contract(manifest, name, *, manifest_digest=None):
    cmp = name == 'cmp'
    efun = name in EFUN_PROGRAMS
    nbtool = name == 'date'
    directory = _NATIVE_DIRECTORIES[name]
    _require(set(manifest) == {'format', 'version', 'name', 'origin', 'canonical_host_recipe', 'source_makefile',
        'compiler', 'sources', 'headers', 'include_dirs', 'cppflags', 'cflags', 'link_flags', 'libraries',
        'invocations', 'files', 'runtime_cases'} | ({'compat'} if efun else set()) | ({'generation'} if name == 'expr' else set()) and
        manifest['format'] == 'emberbsd-native-bootstrap-program' and
        type(manifest['version']) is int and manifest['version'] == 1 and manifest['name'] == name and
        manifest['canonical_host_recipe'] == 'tools/' + name + '/Makefile' and
        manifest['source_makefile'] == directory + '/Makefile' and
        manifest['compiler'] == dict(family='gcc', major=16, abi='native') and
        manifest['sources'] == list(_NATIVE_SOURCES[name]) and
        manifest['headers'] == ([_ID_GROUP_HEADER, _BOOTSTRAP_CDEFS] if name == 'id' else [*(directory + '/' + header for header in _NATIVE_LOCAL_HEADERS.get(name, ())),
                                _BOOTSTRAP_CDEFS, *(_EFUN_HEADERS if efun else (_NBTOOL_CONFIG,) if nbtool else ())]) and
        manifest['include_dirs'] == (['common/include'] if name == 'id' else [directory, 'tools/compat', 'common/lib/libc/stdlib'] if efun else [directory, 'tools/compat/bootstrap'] if nbtool else [directory] if name in _NATIVE_LOCAL_HEADERS else []) and
        manifest['cppflags'] == (['-DHAVE_NBTOOL_CONFIG_H=1', '-include', 'nbtool_config.h'] if efun else (['-DHAVE_NBTOOL_CONFIG_H=1'] if nbtool else []) + ['-include', _BOOTSTRAP_CDEFS]) and
        manifest['cflags'] == (['-fwrapv'] if name == 'expr' else []) and
        all(manifest[key] == [] for key in ('link_flags', 'libraries')) and
        manifest['invocations'] == [name, *_ALIASES.get(name, ())], 'unsupported direct-source ' + name + ' contract')
    if name == 'expr':
        _require(manifest['generation'] == dict(tool='yacc', source='bin/expr/expr.y',
                 output='expr.c', arguments=['-o', 'expr.c']), 'unsupported expr generator contract')
    if cmp:
        _require(set(manifest['files']) == {'same.txt', 'different.txt', 'short.txt', 'skip-left.txt', 'skip-right.txt'} and
                 all(isinstance(value, str) for value in manifest['files'].values()) and
                 len(manifest['runtime_cases']) == 18 and
                 all(case['invocation'] == 'cmp' and 'stdout_nonempty' not in case
                     for case in manifest['runtime_cases']), 'unsupported fixed cmp semantic cases')
    elif name in _LITERAL_CASE_COUNTS:
        if name in ('basename', 'dirname', 'tr', 'printf', 'pwd', 'env', 'uname', 'date', 'id', 'expr', *filesystem.PROGRAMS):
            _require(manifest['files'] == {}, 'this program needs no fixture files')
        else:
            files = manifest['files']
            _require(isinstance(files, dict) and bool(files) and
                     all(isinstance(key, str) and re.fullmatch(r'[a-z][a-z0-9-]*\.txt', key) and
                         key != 'missing.txt' for key in files) and
                     sum(data_size(value) for value in files.values()) <= MAX_FILE_BYTES,
                     'filter programs need bounded immutable fixture files')
        case_count = _LITERAL_CASE_COUNTS[name]
        if name == 'mkdir':
            selected = MKDIR_MANIFEST_SHA256 if manifest_digest is None else manifest_digest
            _require(isinstance(selected, str) and selected in MKDIR_MANIFEST_CASE_COUNTS,
                     'unsupported mkdir manifest digest')
            case_count = MKDIR_MANIFEST_CASE_COUNTS[selected]
        _literal_cases(manifest, case_count, work_root_output=name == 'pwd', uname_output=name == 'uname', identity_oracle=name == 'id', filesystem_cases=name in filesystem.PROGRAMS)
    else:
        # These are the only accepted tails. No hostname operand can reach a child.
        _require(manifest['files'] == {} and manifest['runtime_cases'] == [
            dict(name='read-hostname', invocation='hostname', args=[], stdin='', exit=0,
                 stdout_nonempty=True, stderr=''),
            dict(name='short-hostname', invocation='hostname', args=['-s'], stdin='', exit=0,
                 stdout_nonempty=True, stderr=''),
            dict(name='invalid-option', invocation='hostname', args=['-z'], stdin='', exit=1,
                 stdout='', stderr_nonempty=True)], 'hostname permits only its fixed read-only cases')


def program_manifest(plan):
    if plan.get('stage') == component.STAGE:
        return component.manifest(plan)
    if plan.get('stage') == 'byacc-bootstrap':
        from .bootstrap_byacc import byacc_manifest
        return byacc_manifest(plan)
    if plan.get('stage') == 'pkg-install-bootstrap':
        from .bootstrap_pkg_install import pkg_install_manifest
        return pkg_install_manifest(plan)
    if plan.get('stage') == 'bsdinstall-bootstrap':
        from .bootstrap_bsdinstall import bsdinstall_manifest
        return bsdinstall_manifest(plan)
    if plan.get('stage') in GNU_PROGRAM_STAGES:
        from .bootstrap_gnu_program import gnu_manifest
        return gnu_manifest(plan)
    source, reference = plan['program']['source'], plan['program']['manifest']
    relative, digest, _, _ = _profile(plan)
    _require(reference == dict(path=os.path.join(source['root'], relative), sha256=digest),
             'unsupported source-owner bootstrap program manifest')
    manifest = _json(reference)
    if plan['stage'] != 'grep-bootstrap':
        _native_program_contract(manifest, plan['stage'].removesuffix('-bootstrap'), manifest_digest=digest)
        if plan['stage'] in EFUN_PROGRAM_STAGES:
            from .bootstrap_compat import compat_manifest
            compat_manifest(plan, manifest)
        return manifest
    _require(set(manifest) == {'format', 'version', 'name', 'origin', 'canonical_host_recipe', 'source_makefile',
        'compiler', 'sources', 'headers', 'include_dirs', 'cppflags', 'cflags', 'link_flags', 'libraries',
        'invocations', 'files', 'runtime_cases'} and manifest['format'] == 'emberbsd-native-bootstrap-program' and
        type(manifest['version']) is int and manifest['version'] == 1 and manifest['name'] == 'grep' and
        manifest['canonical_host_recipe'] == 'tools/grep/Makefile' and
        manifest['source_makefile'] == 'usr.bin/grep/Makefile' and
        manifest['compiler'] == dict(family='gcc', major=16, abi='native') and
        manifest['sources'] == list(_SOURCES) and manifest['headers'] == ['usr.bin/grep/grep.h'] and
        manifest['include_dirs'] == ['usr.bin/grep'] and
        manifest['cppflags'] == ['-DWITHOUT_NLS', '-DWITHOUT_BZ2', '-DWITHOUT_GZIP'] and
        all(manifest[name] == [] for name in ('cflags', 'link_flags', 'libraries')) and
        manifest['invocations'] == ['grep', 'egrep', 'fgrep'] and len(manifest['runtime_cases']) == 14 and
        set(manifest['files']) == {'input.txt', 'patterns.txt'}, 'unsupported direct-source grep contract')
    return manifest


def program_sources(plan):
    if plan.get('stage') == component.STAGE:
        return component.sources(plan)
    if plan.get('stage') == 'byacc-bootstrap':
        from .bootstrap_byacc import byacc_sources
        return byacc_sources(plan)
    if plan.get('stage') == 'pkg-install-bootstrap':
        from .bootstrap_pkg_install import pkg_install_sources
        return pkg_install_sources(plan)
    manifest = program_manifest(plan)
    result = {name: decode_data(value) for name, value in manifest['files'].items()}
    if plan.get('stage') in EFUN_PROGRAM_STAGES:
        from .bootstrap_compat import probe_sources
        result.update(probe_sources(plan, manifest))
    return result


def program_outputs(plan):
    if plan.get('stage') == component.STAGE:
        return component.outputs(plan)
    if plan.get('stage') == 'byacc-bootstrap':
        from .bootstrap_byacc import byacc_outputs
        return byacc_outputs(plan)
    if plan.get('stage') == 'pkg-install-bootstrap':
        from .bootstrap_pkg_install import pkg_install_outputs
        return pkg_install_outputs(plan)
    if plan.get('stage') == 'bsdinstall-bootstrap':
        from .bootstrap_bsdinstall import bsdinstall_outputs
        return bsdinstall_outputs(plan)
    if plan.get('stage') in GNU_PROGRAM_STAGES:
        from .bootstrap_gnu_program import gnu_outputs
        return gnu_outputs(plan)
    manifest = program_manifest(plan)
    extra = [manifest['generation']['output']] if manifest['name'] == 'expr' else []
    if plan.get('stage') in EFUN_PROGRAM_STAGES:
        from .bootstrap_compat import probe_outputs
        extra = [*program_sources(plan), *probe_outputs(plan, manifest)]
    if manifest['name'] in filesystem.PROGRAMS:
        extra.extend(case['name'] + '.filesystem.json' for case in manifest['runtime_cases'])
        extra.append('runtime-observation.filesystem.json')
    return [*extra, *manifest['files'], *(Path(name).stem + '.o' for name in manifest['sources']), manifest['name'],
            *(case['name'] + suffix for case in manifest['runtime_cases'] for suffix in ('.stdout', '.stderr'))]


def program_steps(plan, descriptor, commands, flags):
    if plan.get('stage') == component.STAGE:
        return component.steps(plan, descriptor, commands, flags)
    if plan.get('stage') == 'byacc-bootstrap':
        from .bootstrap_byacc import byacc_steps
        return byacc_steps(plan, descriptor, commands, flags)
    if plan.get('stage') == 'pkg-install-bootstrap':
        from .bootstrap_pkg_install import pkg_install_steps
        return pkg_install_steps(plan, descriptor, commands, flags)
    if plan.get('stage') == 'bsdinstall-bootstrap':
        from .bootstrap_bsdinstall import bsdinstall_steps
        return bsdinstall_steps(plan, descriptor, commands, flags)
    if plan.get('stage') in GNU_PROGRAM_STAGES:
        from .bootstrap_gnu_program import gnu_steps
        return gnu_steps(plan, descriptor, commands, flags)
    manifest, work = program_manifest(plan), _absolute(plan['work_root'])
    source = _absolute(plan['program']['source']['root'])
    directories = sorted({os.path.dirname(words[0]) for words in commands.values()})
    _require(all(os.pathsep not in path for path in directories), 'program tool directory cannot be encoded in PATH')
    env = dict(PATH=os.pathsep.join(directories), LC_ALL='C', TZ='UTC', TMPDIR=work)
    driver = [*commands['cc']]
    result, objects = [], []
    cppflags = manifest['cppflags'] if manifest['name'] == 'grep' else ['-include', os.path.join(source, _BOOTSTRAP_CDEFS)]
    if manifest['name'] in EFUN_PROGRAMS:
        from .bootstrap_compat import probe_steps
        result.extend(probe_steps(plan, manifest, commands, flags, env))
        cppflags = ['-DHAVE_NBTOOL_CONFIG_H=1', '-include', os.path.join(work, 'nbtool_config.h'), '-I', work]
    elif manifest['name'] == 'date':
        cppflags = ['-DHAVE_NBTOOL_CONFIG_H=1', *cppflags]
    includes = [word for directory in manifest['include_dirs'] for word in ('-I', os.path.join(source, directory))]
    if manifest['name'] == 'expr':
        selected = descriptor['tools'].get('yacc', {})
        _require(selected.get('kind') == 'native' and
                 tuple(commands.get('yacc', ())) == (selected.get('path'),) and
                 os.path.isabs(selected.get('path', '')) and
                 selected['path'] != descriptor['bundle_root'] and
                 os.path.commonpath((_absolute(selected['path']), _absolute(descriptor['bundle_root']))) ==
                 descriptor['bundle_root'],
                 'expr needs one explicitly selected native yacc invocation')
        result.append(dict(id='generate-expr', phase='generator', cwd=work, env=dict(env), expected_exit=0,
            argv=[*commands['yacc'], '-o', os.path.join(work, 'expr.c'), os.path.join(source, 'bin/expr/expr.y')]))
    for relative in manifest['sources']:
        name = Path(relative).stem
        output = os.path.join(work, name + '.o')
        objects.append(output)
        result.append(dict(id='compile-' + name, phase='compile', cwd=work, env=dict(env), expected_exit=0,
            argv=[*driver, *flags['cppflags'], *flags['cflags'], *manifest['cflags'], '-v', *cppflags,
                  *includes, '-c', (os.path.join(work, 'expr.c') if manifest['name'] == 'expr' else os.path.join(source, relative)), '-o', output]))
    result.append(dict(id='link-' + manifest['name'], phase='link', cwd=work, env=dict(env), expected_exit=0,
        argv=[*driver, *flags['cflags'], *manifest['cflags'], '-v', *objects, *flags['ldflags'], '-Wl,--threads=1',
              *(['-Wl,-v'] if descriptor['host']['os'] == 'Darwin' else []),
              '-o', os.path.join(work, manifest['name'])]))
    for case in manifest['runtime_cases']:
        command = shlex.join([os.path.join(work, case['invocation']), *case['args']])
        command = literal_stdin(case['stdin']) + ' | ' + command
        command += ' >' + shlex.quote(os.path.join(work, case['name'] + '.stdout'))
        command += ' 2>' + shlex.quote(os.path.join(work, case['name'] + '.stderr'))
        cwd = work
        if 'filesystem' in case:
            cwd = str(filesystem.case_directory(work, case['name']))
            command = 'umask ' + case['filesystem']['umask'] + '; ' + command
        result.append(dict(id='case-' + case['name'], phase='program-case', cwd=cwd, env=dict(env),
                           expected_exit=case['exit'], argv=[*commands['sh'], '-c', command]))
    observed = dict(env, DYLD_PRINT_LIBRARIES='1')
    tail = (manifest['runtime_cases'][0]['args'] if manifest['name'] in _LITERAL_CASE_COUNTS
            else _RUNTIME_ARGS[manifest['name']])
    runtime_argv = [os.path.join(work, manifest['name']), *tail]
    runtime_cwd = work
    if manifest['name'] in filesystem.PROGRAMS:
        first = manifest['runtime_cases'][0]
        _require(first['exit'] == 0, 'filesystem runtime observation needs a successful first case')
        runtime_cwd = str(filesystem.case_directory(work, 'runtime-observation'))
        observed = dict(env)
        runtime_argv = [*commands['sh'], '-c', 'umask ' + first['filesystem']['umask'] +
                        '; export DYLD_PRINT_LIBRARIES=1; exec ' + shlex.join(runtime_argv)]
    result.append(dict(id='runtime-observation', phase='run', cwd=runtime_cwd, env=observed, expected_exit=0,
                       argv=runtime_argv))
    return result


def prepare_program_step(step, plan, completed=()):
    if plan.get('stage') == component.STAGE:
        return component.prepare(step, plan, completed)
    from .bootstrap_generated import before_step
    before_step(step, plan, completed)
    if plan.get('stage') in tuple(name + '-bootstrap' for name in filesystem.PROGRAMS):
        case = filesystem.step_case(step, program_manifest(plan))
        if case is not None:
            filesystem.prepare(plan['work_root'], case, key=filesystem.step_key(step))


def verify_program_step(step, work, plan, *, actual_exit=None, completed=()):
    if plan.get('stage') == component.STAGE:
        return component.verify(step, work, plan, actual_exit, completed)
    if plan.get('stage') == 'byacc-bootstrap':
        from .bootstrap_byacc import verify_byacc_step
        return verify_byacc_step(step, work, plan, actual_exit, completed)
    if plan.get('stage') in EFUN_PROGRAM_STAGES:
        from .bootstrap_compat import verify_probe_step
        value = verify_probe_step(step, work, plan, program_manifest(plan), actual_exit, completed)
        if step['phase'].startswith('compat-probe-'):
            return value
    if plan.get('stage') == 'pkg-install-bootstrap':
        from .bootstrap_pkg_install import verify_pkg_install_step
        return verify_pkg_install_step(step, work, plan)
    """Check separate actual streams only after the owned command drains."""
    if plan.get('stage') == 'bsdinstall-bootstrap':
        from .bootstrap_bsdinstall import verify_bsdinstall_step
        return verify_bsdinstall_step(step, work, plan)
    if plan.get('stage') in GNU_PROGRAM_STAGES:
        from .bootstrap_gnu_program import verify_gnu_step
        return verify_gnu_step(step, work, plan)
    if plan.get('stage') == 'expr-bootstrap':
        from .bootstrap_generated import after_step
        generated = after_step(step, plan, actual_exit, completed)
        if step['phase'] == 'generator':
            return generated
    if step['phase'] == 'link':
        name = program_manifest(plan)['name']
        if step['id'] != 'link-' + name:
            return {}
        aliases = {alias: name for alias in _ALIASES.get(name, ())}
        for alias, target in aliases.items():
            os.symlink(target, os.path.join(work, alias))
        return {'invocation_aliases': aliases} if aliases else {}
    if step['phase'] != 'program-case':
        if plan.get('stage') in tuple(name + '-bootstrap' for name in filesystem.PROGRAMS):
            case = filesystem.step_case(step, program_manifest(plan))
            if case is not None:
                return {'filesystem': filesystem.observe(work, case, key=filesystem.step_key(step))}
        return {}
    case = next(row for row in program_manifest(plan)['runtime_cases'] if step['id'] == 'case-' + row['name'])
    program = program_manifest(plan)['name']
    streams = {}
    for name in ('stdout', 'stderr'):
        path = os.path.join(work, case['name'] + '.' + name)
        reference = dict(path=path, sha256=sha256_file(path))
        data = _bytes(reference, limit=max(MAX_JSON, data_size(case[name])) if name in case else MAX_JSON)
        if name == 'stdout' and program == 'hostname' and case.get('stdout_nonempty') is True:
            current = socket.gethostname()
            expected = current if case['args'] == [] else current.split('.', 1)[0]
            _require(data == os.fsencode(expected) + b'\n',
                     'hostname actual stdout differs from the read-only control-plane hostname')
        elif name == 'stdout' and program == 'pwd' and case.get('stdout_work_root') is True:
            expected = os.fsencode(canonical_existing(work)) + b'\n'
            _require(data == expected, 'pwd actual stdout differs from the controller work root')
        elif name == 'stdout' and program == 'uname' and 'stdout_uname' in case:
            _require(data == _uname_output(case['stdout_uname']),
                     'uname actual stdout differs from the read-only controller APIs')
        elif name == 'stdout' and program == 'id' and 'stdout_identity' in case:
            _require(data == identity_output(case['stdout_identity']),
                     'id actual stdout differs from the independent OS identity APIs')
        elif name == 'stderr' and case.get('stderr_nonempty') is True:
            _require(bool(data), program + ' error case did not produce actual stderr')
        else:
            _require(data == decode_data(case[name]), program + ' case actual ' + name + ' differs from its source contract')
        streams[name] = reference
    if 'filesystem' in case:
        streams['filesystem'] = filesystem.observe(work, case)
    return streams


def validate_program_source(plan, source_pin):
    """Re-read the complete pinned source and provenance chain without children."""
    value = plan['program']
    _require(set(value) == {'source', 'manifest'}, 'unsupported program input fields')
    source = value['source']
    _require(set(source) == {'root', 'source', 'inventory', 'source_proof'} and
             source['source'] == source_pin, 'bootstrap program OS source pin mismatch')
    _source(source['source'])
    root = _absolute(source['root'])
    inventory = _json(source['inventory'])
    _require(canonical_existing(root) == root and snapshot_tree(root) == inventory, 'bootstrap program source changed')
    manifest = program_manifest(plan)
    proof = _json(source['source_proof'])
    _require(set(proof) == {'format', 'version', 'source', 'inventory', 'members'} and
        proof['format'] == 'emberbsd-bootstrap-program-source' and type(proof['version']) is int and
        proof['version'] == 1 and proof['source'] == source['source'] and proof['inventory'] == source['inventory'],
        'bootstrap program source proof mismatch')
    members = _json(proof['members'])
    files = {name for name, row in inventory.items() if row.get('kind') == 'file'}
    relative, _, sources, provenance = _profile(plan)
    expected = {*sources, *manifest['headers'], relative, *provenance}
    _require(files == set(members) == expected and
             all(row.get('kind') in ('file', 'directory') for row in inventory.values()),
             'bootstrap program needs its exact complete source roster without links')
    for relative, row in members.items():
        raw = _bytes(dict(path=os.path.join(root, relative), sha256=inventory[relative]['sha256']))
        blob = hashlib.sha1(b'blob ' + str(len(raw)).encode('ascii') + b'\0' + raw).hexdigest()
        _require(set(row) == {'git_blob', 'sha256'} and row['git_blob'] == blob and
                 row['sha256'] == inventory[relative]['sha256'], 'bootstrap program Git blob or content differs')
    return manifest


def validate_program_plan(plan, descriptor, commands, flags):
    if plan.get('stage') == 'byacc-bootstrap':
        from .bootstrap_byacc import validate_byacc_plan
        return validate_byacc_plan(plan, descriptor, commands, flags)
    if plan.get('stage') == 'pkg-install-bootstrap':
        from .bootstrap_pkg_install import validate_pkg_install_plan
        return validate_pkg_install_plan(plan, descriptor, commands, flags)
    if plan.get('stage') == 'bsdinstall-bootstrap':
        from .bootstrap_bsdinstall import validate_bsdinstall_plan
        return validate_bsdinstall_plan(plan, descriptor, commands, flags)
    if plan.get('stage') in GNU_PROGRAM_STAGES:
        from .bootstrap_gnu_program import validate_gnu_plan
        return validate_gnu_plan(plan, descriptor, commands, flags)
    _envelope(plan, PRODUCER_FORMAT, scope='gcc16-bootstrap')
    _require(set(plan) == {'format', 'version', 'scope', 'stage', 'seed_descriptor', 'host', 'sources',
        'work_root', 'required_roles', 'resources', 'program', 'commands'} and plan['stage'] in PROGRAM_STAGES and
        plan['host'] == descriptor['host'] and plan['sources'] == descriptor['sources'] and
        descriptor['host']['os'] == 'Darwin', 'unsupported direct-source bootstrap program stage')
    for source in plan['sources'].values():
        _source(source)
    from .bootstrap_pilot import _validate_resources
    _validate_resources(plan['resources'])
    required = {'cc', 'sh', *descriptor['driver_components'].values()}
    if plan['stage'] == component.STAGE:
        required.update(('ar', 'ranlib'))
    if plan['stage'] == 'expr-bootstrap':
        required.add('yacc')
    _require(isinstance(plan['required_roles'], list) and
             len(plan['required_roles']) == len(set(plan['required_roles'])) and
             required <= set(plan['required_roles']) <= set(commands), 'program input roles are incomplete')
    manifest = validate_program_source(plan, descriptor['sources'].get('os'))
    if manifest['name'] in ('grep', 'cmp', 'cat', 'head', 'uniq', 'wc', 'cut', 'env', 'tail', 'sort'):
        _require(entry(os.path.join(plan['work_root'], 'missing.txt')) is None,
                 manifest['name'] + ' missing-file control already exists')
    _require(plan['commands'] == program_steps(plan, descriptor, commands, flags),
             'program command differs from the source-owned finite route')
    return plan


def program_step_exits(step, plan):
    if plan.get('stage') == component.STAGE and step['id'] in component.PROBES:
        _require(step['expected_exit'] == [0, 1], 'unsupported component probe observations')
        return (0, 1)
    if plan.get('stage') in EFUN_PROGRAM_STAGES and step['phase'] in ('compat-probe-compile', 'compat-probe-link'):
        _require(step['expected_exit'] == [0, 1], 'unsupported efun probe exit observations')
        return (0, 1)
    _require(type(step['expected_exit']) is int, 'program command needs one fixed exit')
    return (step['expected_exit'],)
