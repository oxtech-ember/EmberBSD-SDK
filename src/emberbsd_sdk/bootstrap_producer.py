# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Read-only Ports metadata for one accepted first-native GCC16 input."""

import os
import platform
import plistlib
import re
import shlex
import stat

from .bootstrap_make import _safe_path
from .inventory import snapshot_tree
from .jsonio import loads_json
from .native_toolset import _absolute, _bytes, _envelope, _json, _require, _source
from .paths import canonical_existing
from .ports_plan import PORTS_JSON_LIMIT

BOOTSTRAP_PRODUCER_API = 1
PRODUCER_FORMAT = 'emberbsd-native-gcc16-producer-plan'
PROFILE_RELATIVE = 'profiles/development-toolchain/darwin-bootstrap.mk.conf'
PROFILE_EXPORT = 'EMBERBSD-DARWIN-BOOTSTRAP-MK.CONF'
PROFILE_SHA256 = '3b663ca1e6183f500dcc143c160d916da6088541f8a01b7376983b090d7c8298'
EARLY_TOOLS = tuple('awk basename cat cut date dirname env expr grep head id pwd sed sort tail tr uname uniq wc'.split())
VARIABLES = tuple('PKGNAME PKGPATH PKGVERSION PKGREVISION OPSYS MACHINE_ARCH NATIVE_OPSYS NATIVE_OS_VERSION '
                  'NATIVE_OPSYS_VERSION PKGSRC_COMPILER GCC_REQD _GCC_VERSION CC CXX CPP PKG_CC PKG_CXX '
                  'PKG_CPP CPPFLAGS CFLAGS CXXFLAGS LDFLAGS CWRAPPERS_PREPEND.cc CWRAPPERS_PREPEND.cxx '
                  'CWRAPPERS_PREPEND.cpp _WRAP_EXTRA_ARGS.CC _WRAP_EXTRA_ARGS.CXX _WRAP_EXTRA_ARGS.CPP '
                  'CONFIGURE_ENV CONFIGURE_ARGS USE_TOOLS TOOL_DEPENDS BUILD_DEPENDS DEPENDS TEST_DEPENDS '
                  'MAKE SHELL MAKECONF NATIVE_PKG_INFO_CMD NATIVE_PKG_ADMIN_CMD .MAKE.MAKEFILES'.split())


def _inventory(reference):
    value = loads_json(_bytes(reference, limit=PORTS_JSON_LIMIT), max_bytes=PORTS_JSON_LIMIT)
    _require(isinstance(value, dict) and value, 'a complete nonempty source inventory is required')
    return value


def _current_host_facts():
    # Read the OS service data. Do not discover or execute a host utility.
    with open('/System/Library/CoreServices/SystemVersion.plist', 'rb') as handle:
        product = plistlib.load(handle)['ProductVersion']
    parts = product.split('.')
    _require(1 <= len(parts) <= 3 and all(part.isdigit() and int(part) < 100 for part in parts),
             'unsupported macOS product version')
    version = [int(part) for part in parts] + [0] * (3 - len(parts))
    return dict(os=platform.system(), cpu={'arm64': 'aarch64'}.get(platform.machine(), platform.machine()),
                kernel_version=platform.release(), product_version=product,
                pkgsrc_opsys_version=str(version[0] * 10000 + version[1] * 100 + version[2]))


def _word(value):
    _require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_./+@%=:,\-]+', value),
             'metadata make input contains unsupported make or shell syntax')
    return value


def metadata_paths(work):
    return {name: os.path.join(work, child) for name, child in
            [('WRKOBJDIR', 'obj'), ('DISTDIR', 'distfiles'), ('PACKAGES', 'packages'),
             ('LOCALBASE', 'prefix'), ('PKG_DBDIR', 'pkgdb')]}


def metadata_assignments(plan, descriptor, commands, flags):
    """Construct source-owner variables. This function issues no acceptance."""
    data, work = plan['metadata'], _safe_path(plan['work_root'])
    facts = data['host_facts']
    variables = {'EMBERBSD_DARWIN_HOST_' + key.upper(): _word(value) for key, value in facts.items()}
    variables.update(metadata_paths(work))
    for key, role in [('MAKE', 'make'), ('GMAKE', 'gmake')]:
        variables['EMBERBSD_DARWIN_' + key] = shlex.join([_word(word) for word in commands[role]])
    variables['EMBERBSD_DARWIN_SH'] = _safe_path(commands['sh'][0])
    for role, language in [('cc', 'cflags'), ('cxx', 'cxxflags'), ('cpp', None)]:
        words = [*commands[role], *flags['cppflags'], *(flags[language] if language else ())]
        variables['EMBERBSD_GCC16_SEED_' + role.upper()] = shlex.join([_word(word) for word in words])
    for key, role in [('LLVM_MC', 'as'), ('LD64_LLD', 'ld'), ('DSYMUTIL', 'dsymutil'),
                      ('AR', 'ar'), ('RANLIB', 'ranlib'), ('NM', 'nm'), ('STRIP', 'strip')]:
        _require(len(commands[role]) == 1, 'metadata utility must have one selected invocation')
        variables['EMBERBSD_GCC16_' + key] = _safe_path(commands[role][0])
    binding = descriptor['driver_binding']
    variables.update(OSX_SDK_PATH=_safe_path(os.path.dirname(binding['sdk_metadata']['path'])),
                     EMBERBSD_GCC16_DARWIN_SDK_VERSION=_word(binding['sdk_version']),
                     MACOSX_DEPLOYMENT_TARGET=_word(binding['deployment']),
                     EMBERBSD_DARWIN_JOBS=str(plan['resources']['cpu_jobs']),
                     EMBERBSD_DARWIN_UTILITY_BIN=_safe_path(data['utility_bin']),
                     EMBERBSD_DARWIN_NATIVE_TOOLS=' '.join(data['native_tools']),
                     EMBERBSD_DARWIN_SOURCE_TOOLS=' '.join(data['source_tools']),
                     NATIVE_PKG_INFO_CMD=_safe_path(commands['pkg_info'][0]),
                     NATIVE_PKG_ADMIN_CMD=_safe_path(commands['pkg_admin'][0]),
                     LDFLAGS=shlex.join([_word(word) for word in flags['ldflags']]))
    for role in data['native_tools']:
        _require(len(commands[role]) == 1, 'raw metadata tools cannot omit a selected command tail')
        variables['EMBERBSD_DARWIN_TOOL.' + role] = _safe_path(commands[role][0])
    return variables


def metadata_makeconf(plan, descriptor, commands, flags):
    variables = metadata_assignments(plan, descriptor, commands, flags)
    lines = ['# SPDX-License-Identifier: BSD-2-Clause',
             '# Source-owned SDK metadata inputs. This file is not qualification.']
    lines += [name + '= ' + value for name, value in sorted(variables.items())]
    lines += ['.include "' + _safe_path(plan['metadata']['profile']['path']) + '"']
    return ('\n'.join(lines) + '\n').encode('ascii')


def producer_steps(plan, descriptor, commands, flags):
    """Construct only fixed read-only queries. Never accept an argv extension."""
    data, work = plan['metadata'], _safe_path(plan['work_root'])
    env = dict(PATH=_safe_path(data['utility_bin']), LC_ALL='C', TZ='UTC', TMPDIR=work,
               MAKECONF=_safe_path(data['makeconf']['path']), SHELL=_safe_path(commands['sh'][0]),
               CONFIG_SHELL=_safe_path(commands['sh'][0]))
    queries = [('vars', ['show-vars', 'VARNAMES=' + ' '.join(VARIABLES)]), ('tools', ['show-tools'])]
    queries += [('depends-' + name.lower(), ['show-depends', 'VARNAME=' + name])
                for name in ('TOOL_DEPENDS', 'BUILD_DEPENDS', 'DEPENDS', 'TEST_DEPENDS')]
    result = []
    for number, recipe in enumerate(data['recipes']):
        for name, tail in queries:
            result.append(dict(id=f'r{number:02d}-' + name, phase='metadata', recipe=recipe, cwd=work,
                               argv=[*commands['make'], 'EMBERBSD_DARWIN_BOOTSTRAP=yes',
                                     '-j' + str(plan['resources']['cpu_jobs']), '-C',
                                     os.path.join(data['export']['root'], recipe), *tail], env=dict(env)))
    return result


def _utility_directory(data, descriptor, commands):
    root, bundle = _safe_path(data['utility_bin']), _absolute(descriptor['bundle_root'])
    _require(root != bundle and os.path.commonpath((root, bundle)) == bundle and
             canonical_existing(root) == root, 'metadata utility directory must be in its real seed bundle')
    _require(sorted(os.listdir(root)) == data['native_tools'], 'metadata utility PATH has missing or extra members')
    for role in data['native_tools']:
        path = os.path.join(root, role)
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(path)
            _require(not os.path.isabs(target) and os.path.normpath(os.path.join(root, target)) == commands[role][0],
                     'metadata utility alias must name its exact selected spelling')
        else:
            _require(stat.S_ISREG(info.st_mode) and path == commands[role][0],
                     'metadata utility invocation differs from its selected member')


def _export_proof(data, descriptor, inventory):
    proof = _json(data['export']['source_proof'])
    _require(proof.get('format') == 'emberbsd-ports-export-source-proof' and
             type(proof.get('version')) is int and proof['version'] == 1,
             'unsupported prepared export source proof')
    _require(set(proof) == {'format', 'version', 'ports_source', 'pkgsrc_source', 'ports_manifest',
                           'exporter', 'export_config', 'inventory'} and
             proof['ports_source'] == descriptor['sources']['ports'] and
             proof['inventory'] == data['export']['inventory'], 'prepared export source chain mismatch')
    _source(proof['pkgsrc_source'])
    manifest = _inventory(proof['ports_manifest'])
    _require(manifest.get('upstream/pkgsrc') == dict(kind='gitlink', commit=proof['pkgsrc_source']['commit']),
             'prepared export does not bind the recorded pkgsrc gitlink')
    for name, relative in [('export_config', 'pkgsrc/export.conf'), ('exporter', proof['exporter']['relative'])]:
        row = proof[name]
        allowed = (('pkgsrc/export.conf',) if name == 'export_config' else
                   ('scripts/prepare-pkgsrc.sh', 'scripts/prepare-pkgsrc.py'))
        _require(set(row) == {'relative', 'file'} and row['relative'] == relative and
                 relative in allowed and
                 manifest.get(relative, {}).get('sha256') == row['file']['sha256'],
                 'prepared export omits its canonical producer source')
        _bytes(row['file'])
    _require(manifest.get(PROFILE_RELATIVE, {}).get('sha256') == PROFILE_SHA256 and
             inventory.get(PROFILE_EXPORT, {}).get('sha256') == PROFILE_SHA256,
             'prepared export profile differs from its source-owner bytes')


def validate_producer_plan(plan, descriptor, commands, flags):
    if plan.get('stage') == 'utility-relocation':
        from .bootstrap_relocation import validate_relocation_plan
        return validate_relocation_plan(plan, descriptor, commands, flags)
    if plan.get('stage') == 'grep-bootstrap':
        from .bootstrap_program import validate_program_plan
        return validate_program_plan(plan, descriptor, commands, flags)
    _envelope(plan, PRODUCER_FORMAT, scope='gcc16-bootstrap')
    _require(set(plan) == {'format', 'version', 'scope', 'stage', 'seed_descriptor', 'host', 'sources',
                          'work_root', 'required_roles', 'resources', 'metadata', 'commands'} and
             plan['stage'] == 'metadata' and plan['host'] == descriptor['host'] and
             plan['sources'] == descriptor['sources'] and descriptor['host']['os'] == 'Darwin' and
             descriptor['host']['cpu'] in ('arm64', 'aarch64'), 'unsupported first-native producer stage')
    for source in plan['sources'].values():
        _source(source)
    from .bootstrap_pilot import _validate_resources
    _validate_resources(plan['resources'], metadata=True)
    data = plan['metadata']
    _require(set(data) == {'export', 'profile', 'makeconf', 'host_facts', 'utility_bin',
                          'native_tools', 'source_tools', 'recipes'}, 'unsupported metadata input fields')
    _require(data['host_facts'] == _current_host_facts() and data['host_facts']['os'] == 'Darwin' and
             data['host_facts']['cpu'] == 'aarch64', 'metadata facts differ from the current native host')
    for name in ('native_tools', 'source_tools'):
        values = data[name]
        _require(isinstance(values, list) and values == sorted(set(values)) and len(values) <= 64 and
                 all(isinstance(role, str) and re.fullmatch('[a-z][a-z0-9_-]*', role) for role in values),
                 'metadata tool names must be finite, unique and sorted')
    _require(not set(data['native_tools']) & set(data['source_tools']),
             'metadata source and native utility selections overlap')
    required = set(EARLY_TOOLS) | {'make', 'gmake', 'sh', 'pkg_info', 'pkg_admin', 'cc', 'cxx', 'cpp',
                                  'as', 'ld', 'dsymutil', 'ar', 'ranlib', 'nm', 'strip'} | set(data['native_tools'])
    _require(set(EARLY_TOOLS) <= set(data['native_tools']) and isinstance(plan['required_roles'], list) and
             len(plan['required_roles']) == len(set(plan['required_roles'])) and
             required <= set(plan['required_roles']) <= set(commands), 'metadata input tool roles are incomplete')
    _require(not set(data['native_tools']) & {'cc', 'cxx', 'cpp', 'as', 'ld', 'make', 'gmake', 'sh'},
             'metadata raw utility PATH cannot bypass compiler or make bindings')
    _require(isinstance(data['recipes'], list) and 1 <= len(data['recipes']) <= 16 and
             len(data['recipes']) == len(set(data['recipes'])) and 'lang/gcc16' in data['recipes'] and
             all(isinstance(name, str) and re.fullmatch('[a-z0-9_+-]+/[a-z0-9_+.-]+', name)
                 for name in data['recipes']), 'metadata needs a finite explicit recipe list')
    export = data['export']
    _require(set(export) == {'root', 'inventory', 'source_proof'}, 'unsupported prepared export fields')
    root = _safe_path(export['root'])
    inventory = _inventory(export['inventory'])
    _require(canonical_existing(root) == root and snapshot_tree(root) == inventory,
             'prepared canonical export changed')
    _export_proof(data, descriptor, inventory)
    _require(data['profile'] == dict(path=os.path.join(root, PROFILE_EXPORT), sha256=PROFILE_SHA256),
             'metadata needs the recognized source-owner profile')
    _bytes(data['profile'])
    _require(all(inventory.get(name + '/Makefile', {}).get('kind') == 'file' for name in data['recipes']),
             'metadata recipe is absent from the complete prepared export')
    _require(descriptor.get('make_binding') is not None and
             data['makeconf'] == descriptor['make_binding']['makeconf'] and
             _bytes(data['makeconf']) == metadata_makeconf(plan, descriptor, commands, flags),
             'metadata MAKECONF differs from its accepted native selection')
    _utility_directory(data, descriptor, commands)
    _require(plan['commands'] == producer_steps(plan, descriptor, commands, flags),
             'metadata command differs from the fixed source-owned query')
    return plan
