# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Write ordinary first-prefix Darwin configuration for the common Ports policy."""

import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import tempfile

from .bootstrap_make import _safe_path
from .bootstrap_producer import _current_host_facts, _word
from . import process
from .errors import SDKError
from .host_prefix import _member
from .native_toolset import _require
from .paths import canonical_existing, canonical_new, regular_file
from .publication import publish_directory_noreplace

PROFILE = 'EMBERBSD-DARWIN-BOOTSTRAP-MK.CONF'
COMPILERS = ('cc', 'cxx', 'cpp')
LLVM = {'LLVM_MC': 'as', 'LD64_LLD': 'ld', 'DSYMUTIL': 'dsymutil',
        'AR': 'ar', 'RANLIB': 'ranlib', 'NM': 'nm', 'STRIP': 'strip', 'UNAME': 'uname'}
CMAKE = {'LIBTOOL': 'libtool_darwin', 'INSTALL_NAME_TOOL': 'install_name_tool', 'TAPI': 'llvm_readtapi'}
BOUND = frozenset((*COMPILERS, 'as', 'ld', 'make', 'gmake', 'sh'))
REQUIRED = frozenset((*BOUND, *LLVM.values(), 'pkg_info', 'pkg_admin', 'pkg_add', 'pkg_create'))
ROLE = re.compile(r'[a-z][a-z0-9_.+-]*\Z')


def render_variables(variables, profile, *, nested_ldflags=False, metadata_make=None,
                     comment='Source-owned SDK metadata inputs. This file is not qualification.'):
    """Keep the existing sorted make syntax and nested link-flag behavior."""
    lines = ['# SPDX-License-Identifier: BSD-2-Clause', '# ' + comment]
    for name, value in sorted(variables.items()):
        if name == 'LDFLAGS' and nested_ldflags:
            lines += ['.if defined(BSD_PKG_MK)', name + '= ' + value, '.endif']
        else:
            lines += [name + '= ' + value]
    lines += ['.include "' + _safe_path(profile) + '"']
    if metadata_make is not None:
        lines += ['_MAKE= ' + metadata_make, 'MAKE= ' + metadata_make]
    return ('\n'.join(lines) + '\n').encode('ascii')


def _path(value, *, directory=False, existing=False):
    value = _safe_path(os.fspath(value))
    _require(os.path.normpath(value) == value, 'use normalized absolute paths')
    _require((canonical_existing(value) if existing else canonical_new(value)) == value,
             'use canonical paths for build configuration')
    if directory and existing:
        _require(os.path.isdir(value), 'expected directory: ' + value)
    return value


def _inside(path, root):
    return path != root and os.path.commonpath((path, root)) == root


def _mapping(destination, tools):
    """Publish only links to explicit tools; keep a matching existing map."""
    if os.path.lexists(destination):
        _require(os.path.isdir(destination) and not os.path.islink(destination) and
                 set(os.listdir(destination)) == set(tools) and
                 all(os.path.islink(os.path.join(destination, role)) and
                     os.readlink(os.path.join(destination, role)) == path for role, path in tools.items()),
                 'existing utility mapping differs from selected tools')
        return False
    stage = tempfile.mkdtemp(prefix='.darwin-tools-', dir=os.path.dirname(destination))
    try:
        for role, path in tools.items():
            os.symlink(path, os.path.join(stage, role))
        publish_directory_noreplace(stage, destination)
    finally:
        if os.path.isdir(stage):
            shutil.rmtree(stage)
    return True


def write_makeconf(*, pkgsrc, output, prefix, work_root, distfiles, packages, pkgdb,
                   utility_bin, makefiles, macos_sdk, deployment, tools, compiler_args,
                   tool_roots, jobs=None, ldflags=(), source_tools=(), tool_phase='full',
                   compiler_family='clang', cpu_budget=None, link_python=None,
                   lock_root=None, heavy_link_context=None, sdk_source=None,
                   native_cxx_runtime=None, native_cxx_bindings=None, native_cxx_python=None):
    """Render explicit inputs. Do not run tools, compiler queries, or a build.

    Initial tools can use several declared roots before the final Ports prefix
    exists. Compiler argument order is retained; this is not a compiler probe.
    """
    if jobs is None:
        jobs = os.cpu_count()
        _require(jobs is not None, 'host CPU count is unavailable; select jobs explicitly')
    _require(type(jobs) is int and jobs > 0, 'jobs must be a positive integer')
    cpu_budget = jobs if cpu_budget is None else cpu_budget
    _require(type(cpu_budget) is int and cpu_budget > 0 and jobs <= cpu_budget,
             'CPU budget must be positive and at least jobs')
    link_inputs = (link_python, lock_root, heavy_link_context)
    _require(all(link_inputs) if jobs > 1 or any(value is not None for value in link_inputs) else True,
             'parallel builds require link-python, lock-root and heavy-link-context; '
             'select jobs=1 for a serial build')
    _require(compiler_family in ('clang', 'gcc'), 'unsupported compiler family')
    _require(tool_phase in ('initial', 'full'), 'invalid Darwin tool phase')
    _require(isinstance(deployment, str) and re.fullmatch(r'[0-9]+(?:\.[0-9]+){0,2}', deployment),
             'invalid macOS deployment version')
    facts = _current_host_facts()
    _require(facts['os'] == 'Darwin' and facts['cpu'] == 'aarch64', 'Darwin arm64 host required')
    host = dict(os='Darwin', cpu='aarch64')
    roots = tuple(_path(root, directory=True, existing=True) for root in tool_roots)
    _require(roots and '/' not in roots, 'explicit initial tool roots are required')
    work = _path(work_root, directory=True, existing=True)
    pkgsrc = _path(pkgsrc, directory=True, existing=True)
    makefiles = _path(makefiles, directory=True, existing=True)
    sdk = _path(macos_sdk, directory=True, existing=True)
    output, utility_bin = (_path(value) for value in (output, utility_bin))
    _require(_inside(output, work) and _inside(utility_bin, work),
             'configuration outputs must remain inside the explicit work root')
    _require(not _inside(output, utility_bin) and not _inside(utility_bin, output) and output != utility_bin,
             'configuration outputs overlap')
    for value in (output, utility_bin):
        _require(os.path.isdir(os.path.dirname(value)), 'output parent must exist')
        _require(not any(value == path or _inside(value, path) for path in (pkgsrc, makefiles, sdk)),
                 'configuration output overlaps source inputs')
    if os.path.lexists(output):
        regular_file(output)
    profile = os.path.join(pkgsrc, PROFILE)
    regular_file(profile)
    regular_file(os.path.join(pkgsrc, 'EMBERBSD-DEVELOPMENT-MK.CONF'))
    profile_text = Path(profile).read_text(encoding='ascii')
    early = re.findall(r'^_EMBERBSD_DARWIN_EARLY_TOOLS= ([a-z0-9_ -]+)$', profile_text, re.M)
    compression = re.findall(r'^\.for _ember_tool in (gzip gunzip gzcat)$', profile_text, re.M)
    _require(len(early) == len(compression) == 1, 'unsupported Ports Darwin profile inputs')
    _require(isinstance(tools, dict) and all(isinstance(name, str) and ROLE.fullmatch(name) for name in tools),
             'invalid selected tool role')
    # Retain executable aliases: GCC support lookup can depend on argv[0].
    tools = {name: _safe_path(os.fspath(path)) for name, path in tools.items()}
    _require(all(os.path.normpath(path) == path for path in tools.values()),
             'use normalized absolute tool paths')
    required = REQUIRED | set(early[0].split())
    _require(required <= set(tools), 'missing initial tool roles: ' + ' '.join(sorted(required - set(tools))))
    _require(set(compiler_args) == set(COMPILERS), 'supply each full compiler argument tuple')
    commands = {}
    for role in COMPILERS:
        args = compiler_args[role]
        _require(isinstance(args, (list, tuple)), 'compiler arguments must be an argv sequence')
        commands[role] = [tools[role], *(_word(word) for word in args)]
    _require(all(isinstance(role, str) and ROLE.fullmatch(role) for role in source_tools),
             'invalid source tool role')
    sources = set(source_tools) | (set(compression[0].split()) - set(tools))
    _require(not sources & set(tools) and not sources & required, 'source and native tool roles overlap')
    sys_mk = Path(makefiles, 'sys.mk').read_text()
    _require(re.search(r'^\.SHELL:\s+name=sh\s+path=' + re.escape(tools['sh']) + r'\s*$', sys_mk, re.M),
             'initial make shell must be bound in the selected sys.mk')
    with open(os.path.join(sdk, 'SDKSettings.plist'), 'rb') as stream:
        sdk_version = plistlib.load(stream).get('Version')
    _require(isinstance(sdk_version, str) and re.fullmatch(r'[0-9]+(?:\.[0-9]+){0,2}', sdk_version),
             'missing native SDK version')
    iconv = canonical_existing(os.path.join(sdk, 'usr/lib/libiconv.tbd'))
    _require(_inside(iconv, sdk), 'native libiconv target escapes the selected SDK')
    regular_file(iconv)
    make = [tools['make'], '-m', makefiles, 'SHELL=' + tools['sh'], 'MAKECONF=' + output]
    gmake = [tools['gmake'], 'SHELL=' + tools['sh'], 'MAKECONF=' + output]
    utilities = {name: path for name, path in tools.items() if name not in BOUND}
    variables = {'EMBERBSD_DARWIN_HOST_' + name.upper(): _word(value) for name, value in facts.items()}
    variables.update(WRKOBJDIR=os.path.join(work, 'obj'), DISTDIR=_path(distfiles),
                     PACKAGES=_path(packages), LOCALBASE=_path(prefix), PKG_DBDIR=_path(pkgdb),
                     BATCH='yes',
                     EMBERBSD_DARWIN_MAKE=shlex.join(make), EMBERBSD_DARWIN_GMAKE=shlex.join(gmake),
                     EMBERBSD_DARWIN_SH=tools['sh'], EMBERBSD_DARWIN_TOOL_PHASE=tool_phase,
                     OSX_SDK_PATH=sdk, EMBERBSD_DARWIN_SDK_VERSION=sdk_version,
                     EMBERBSD_COMPILER_FAMILY=compiler_family,
                     MACOSX_DEPLOYMENT_TARGET=deployment, EMBERBSD_DARWIN_JOBS=str(jobs),
                     EMBERBSD_DARWIN_CPU_BUDGET=str(cpu_budget), EMBERBSD_DARWIN_UTILITY_BIN=utility_bin,
                     EMBERBSD_DARWIN_NATIVE_TOOLS=' '.join(sorted(utilities)),
                     EMBERBSD_DARWIN_SOURCE_TOOLS=' '.join(sorted(sources)),
                     LDFLAGS=shlex.join([_word(word) for word in ldflags]))
    if sdk_source is not None:
        source = _path(sdk_source, directory=True, existing=True)
        regular_file(os.path.join(source, 'src/emberbsd_sdk/__init__.py'))
        variables['EMBERBSD_SDK_SOURCE'] = source
    if all(link_inputs):
        from .bootstrap_recipe import recipe_cmake_linker_launcher, recipe_linker_launcher
        link_python = _safe_path(os.fspath(link_python))
        _require(os.path.normpath(link_python) == link_python, 'use a normalized link Python path')
        launcher = _path(Path(__file__).resolve().parents[2] / 'scripts/link-launcher.py', existing=True)
        regular_file(launcher)
        # Reuse the existing SDK service and both native argument encodings.
        link = dict(link_control={'python': {'path': link_python}, 'launcher': {'path': launcher}},
                    resources={'lock_root': _path(lock_root, directory=True, existing=True),
                               'heavy_link_context': _path(heavy_link_context)})
        variables['EMBERBSD_DARWIN_CMAKE_LINKER_LAUNCHER'] = recipe_cmake_linker_launcher(link)
        variables['EMBERBSD_DARWIN_LINKER_LAUNCHER'] = recipe_linker_launcher(link)
        if compiler_family == 'gcc':
            variables['EMBERBSD_GCC16_LINKER_LAUNCHER'] = recipe_linker_launcher(link)
    for role, command in commands.items():
        variables['EMBERBSD_DARWIN_SEED_' + role.upper()] = shlex.join(command)
    variables['EMBERBSD_DARWIN_SEED_AS'] = shlex.join(
        [*commands['cc'], '-c', '-x', 'assembler'] if compiler_family == 'clang' else [tools['as']])
    for name, role in LLVM.items():
        variables['EMBERBSD_DARWIN_' + name] = tools[role]
    for name, role in CMAKE.items():
        if role in tools:
            variables['EMBERBSD_DARWIN_' + name] = tools[role]
            if name == 'TAPI':
                variables['EMBERBSD_DARWIN_TAPI_KIND'] = 'llvm-readtapi'
    for role in ('pkg_info', 'pkg_admin', 'pkg_add', 'pkg_create'):
        variables['NATIVE_PKG_' + role[4:].upper() + '_CMD'] = tools[role]
    for role, path in utilities.items():
        variables['EMBERBSD_DARWIN_TOOL.' + role] = path
    created = _mapping(utility_bin, utilities)
    temporary = None
    try:
        for selected in tools.values():
            _member(selected, (*roots, utility_bin), host, search_path=utility_bin)
        if link_python is not None:
            _member(link_python, (*roots, utility_bin), host, search_path=utility_bin)
        native_selection = (native_cxx_runtime, native_cxx_bindings, native_cxx_python)
        _require(all(value is not None for value in native_selection) or
                 all(value is None for value in native_selection),
                 'native C++ runtime, binding directory and Python must be selected together')
        if native_cxx_runtime is not None:
            from . import compiler_bindings
            _require(isinstance(native_cxx_runtime, dict) and set(native_cxx_runtime) in
                     ({'include_dir', 'library_dir', 'sysroot', 'targets', 'deployment'},
                      {'include_dir', 'library_dir', 'sysroot', 'targets', 'deployment', 'platform_cxxabi'},
                      {'include_dir', 'library_dir', 'sysroot', 'targets', 'deployment', 'platform_cxxabi', 'abi_identity'}),
                     'invalid native C++ runtime selection')
            _require(compiler_family == 'clang', 'native C++ runtime requires selected Clang')
            targets = native_cxx_runtime.get('targets')
            _require(isinstance(targets, (list, tuple)) and bool(targets) and
                     all(isinstance(target, str) and
                         re.fullmatch(r'(?:arm64|aarch64)-apple-(?:darwin|macosx?)[0-9.]*', target)
                         for target in targets),
                     'native C++ runtime targets differ from selected Darwin arm64 host')
            _require(native_cxx_runtime.get('sysroot') == sdk,
                     'native C++ runtime sysroot differs from selected macOS SDK')
            _require(compiler_bindings._deployment_version(deployment) >=
                     compiler_bindings._deployment_version(native_cxx_runtime.get('deployment')),
                     'configured deployment is older than the native C++ runtime')
            binding = _path(native_cxx_bindings)
            _require(_inside(binding, work) and binding not in (utility_bin, output) and
                     not _inside(binding, utility_bin) and not _inside(utility_bin, binding) and
                     not _inside(output, binding), 'native binding must be in work root')
            python = _path(native_cxx_python, existing=True)
            _member(python, (*roots, utility_bin), host, search_path=utility_bin)
            compiler_bindings._native_cxx_options(native_cxx_runtime, commands['cxx'][1:])
            bound_commands = dict(commands, cxx=[*commands['cxx'], '--target=' + targets[0]])
            adapters = compiler_bindings.bind_compilers(binding, commands=bound_commands,
                flags=dict(cppflags=[], cflags=[], cxxflags=[], ldflags=[]),
                python=python, native_cxx_runtime=native_cxx_runtime)
            variables['EMBERBSD_DARWIN_SEED_CXX'] = shlex.join([adapters['cxx']])
            variables['EMBERBSD_NATIVE_CXX_BINDING_RECORD'] = os.path.join(binding, 'commands.json')
        data = render_variables(variables, profile, nested_ldflags=True,
                                comment='Explicit initial Darwin tools. Package policy belongs to Ports.')
        with tempfile.NamedTemporaryFile(prefix='.darwin-makeconf-', dir=os.path.dirname(output),
                                         delete=False) as stream:
            temporary = stream.name
            stream.write(data)
        os.replace(temporary, output)
    except BaseException:
        if created:
            shutil.rmtree(utility_bin)
        raise
    finally:
        if temporary and os.path.lexists(temporary):
            os.unlink(temporary)
    environment = process.sanitized_environment()
    environment.update(PATH=utility_bin, MAKECONF=output, SHELL=tools['sh'], CONFIG_SHELL=tools['sh'],
                       TMPDIR=work, TZ='UTC', PATH_SEPARATOR=os.pathsep)
    return dict(makeconf=output, compiler_family=compiler_family,
                selected_roles=sorted(tools), required_roles=sorted(required),
                source_tools=sorted(sources), utility_bin=utility_bin,
                initial_command=[*make, 'EMBERBSD_DARWIN_BOOTSTRAP=yes', '-C',
                                 os.path.join(pkgsrc, 'devel/bmake'), 'build', 'install'],
                environment=environment)
