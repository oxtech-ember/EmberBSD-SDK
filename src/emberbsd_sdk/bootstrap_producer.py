# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Read-only Ports metadata for one accepted first-native GCC16 input."""

import hashlib
import os
import platform
import plistlib
import re
import shlex
import stat

from .bootstrap_make import _safe_path
from .errors import SDKError
from .inventory import snapshot_tree
from .jsonio import loads_json
from .native_toolset import _absolute, _bytes, _envelope, _json, _require, _source
from .paths import canonical_existing
from .ports_plan import PORTS_JSON_LIMIT

BOOTSTRAP_PRODUCER_API = 1
RECIPE_LIMIT = 256
PRODUCER_FORMAT = 'emberbsd-native-gcc16-producer-plan'
PROFILE_RELATIVE = 'profiles/development-toolchain/darwin-bootstrap.mk.conf'
PROFILE_EXPORT = 'EMBERBSD-DARWIN-BOOTSTRAP-MK.CONF'
PROFILE_SHA256 = '3b663ca1e6183f500dcc143c160d916da6088541f8a01b7376983b090d7c8298'
PARALLEL_PROFILE_SHA256 = '6adbb69112056a867b463fd825a0842294b1eb7e67331433184c1f31d5de518a'
BUILTIN_PROFILE_SHA256 = '73209380dcfeeb36abe13f0637a87bc1fd441817c98458ffb92751605c7b543b'
LIBTOOL_PROFILE_SHA256 = 'ec0354fe315fb02605be2e08299b7495590fc1086d511f67093f4fbb8f4775e0'
RANLIB_PROFILE_SHA256 = '5b48cba0ea1bd64b883631028d94db1aca574d87f235299a3fc9e768253907db'
NATIVE_PROVIDER_PROFILE_SHA256 = '9fe8cf29f9d8a5dfb92f3a7f8bc44d0c9f6bcbb584f37636934bf54b6299c3cb'
NO_DEFAULT_RPATH_PROFILE_SHA256 = '8ddcfcc680622d9990289c51e9d44a476397a09999e1609c20e7051b0844e417'
UNPRIVILEGED_PROFILE_SHA256 = '34648560a0c6fd66f74b134d6a832ff28a7a1bc74ba06cf554a18bbf6203a8a8'
BSDINSTALL_PROFILE_SHA256 = 'ccc42348223b31cc62ff90588104dc616c46947576e2b0569b9f427448e3012b'
FOUNDATION_PROFILE_SHA256 = 'f96b910712f3a3bf15365704805d8101563398b757389cbf337e8351564990f5'
BOOTSTRAP_PKGTOOLS_PROFILE_SHA256 = 'e9221d9b002724963d9c3eb642429d12b3d33e8a1d6977b316c6a8745a5867cd'
HOST_RUBY_PROFILE_SHA256 = '658eda5e9f0718ae8a3ea319ae3adb346f75498911f1afb7ffc72d804c8fc689'
MAKE_HANDOFF_PROFILE_SHA256 = '92dc223512de9427030e7a2af85d7673cf658d6f8ba68564a8ca62dd5dfaf4a8'
SHELL_HANDOFF_PROFILE_SHA256 = 'caef7465f704a40a481eb082d56dc3d5b9434350b9ebd1735c12a4408332f4eb'
BUILTIN_HELPER_PROFILE_SHA256 = 'd601e1332d820b922c43cbf19b9150ad3527fd00eea6d67615850960ed320833'
CA_PREFIX_PROFILE_SHA256 = '3366a715faa4c6271d3020f484b969d552164cdfe23bf8dc08e266234d82501f'
METADATA_PHASE_PROFILE_SHA256 = '87b856b28ff8757bd9c3c62cefaeb032d95be12da79e4fff7415b6b7b8a4a9b9'
CA_METADATA_PHASE_PROFILE_SHA256 = '06009d0687d0553bcb2de546415caf185b9bdff372006df9147b1e92bc8a9052'
GZIP_HANDOFF_PROFILE_SHA256 = '75f3fa97e7e346f7abded91fef0f52e802af481f480f4474ef78ec9a4e47804b'
SOURCE_GZIP_HANDOFF_PROFILE_SHA256 = '39f572f94655383e3a4041e41d51477dc6cd2dbf10861c811274964a4d98c58a'
SOURCE_GZIP_MAKE_SPLIT_PROFILE_SHA256 = 'c707c392394e9765fe48ce3cc10e13bb924b1c731d529e35e89736813a214169'
RUNTIME_FLAGS_REAPPLY_PROFILE_SHA256 = '15793fddb726c8fdd56c41b5000e414060e67f9e0991fe09622e3cbdece1f17d'
NATIVE_RUNTIME_SHELL_PROFILE_SHA256 = '42772112699331e4c091eec7d7cad0a5ce04461e7db3c807497673b69046681c'
INITIAL_DARWIN_PROFILE_SHA256 = 'd54e703a263c307917f6b3d376e83bae04720aa74d1325d530faea50cfb6b765'
PERL_DTRACE_PROFILE_SHA256 = 'd61850c6dcd6ffa7c722a623912e00588a8de13317275ff91dea70bed300f1f9'
ICONV_PROFILE_SHA256 = 'e6fa6a573e39cbe581b4fa697b4e77e213ea73b072d75acd8761c999396204d7'
LIBTOOL_ICONV_PROFILE_SHA256 = '843a90ddd938c7a88a9929c452adc44f0f34b9aa1e5a667d2add5edce55dd408'
FINAL_RUNTIME_FLAGS_PROFILE_SHA256 = '00142405ab9cff5e02cbd47bc9bad6906402e5d61a16cb1cc9a7837618d7fcd8'
EARLY_TOOLS = tuple('awk basename cat cut date dirname env expr grep head id pwd sed sort tail tr uname uniq wc'.split())
# Recipe builds run these bare commands, which pkgsrc does not create as tools:
# ninja-build's bootstrap runs od, and meson's Darwin compiler checks run lipo.
RECIPE_UTILITIES = {'devel/meson': ('lipo',), 'devel/ninja-build': ('od',)}
_DARWIN_CMAKE_TOOLS = (('LIBTOOL', 'libtool_darwin'), ('INSTALL_NAME_TOOL', 'install_name_tool'),
                       ('TAPI', 'llvm_readtapi'))
LEGACY_VARIABLES = tuple('PKGNAME PKGPATH PKGVERSION PKGREVISION OPSYS MACHINE_ARCH NATIVE_OPSYS NATIVE_OS_VERSION '
                  'NATIVE_OPSYS_VERSION PKGSRC_COMPILER GCC_REQD _GCC_VERSION CC CXX CPP PKG_CC PKG_CXX '
                  'PKG_CPP CPPFLAGS CFLAGS CXXFLAGS LDFLAGS CWRAPPERS_PREPEND.cc CWRAPPERS_PREPEND.cxx '
                  'CWRAPPERS_PREPEND.cpp _WRAP_EXTRA_ARGS.CC _WRAP_EXTRA_ARGS.CXX _WRAP_EXTRA_ARGS.CPP '
                  'CONFIGURE_ENV CONFIGURE_ARGS USE_TOOLS TOOL_DEPENDS BUILD_DEPENDS DEPENDS TEST_DEPENDS '
                  'WRKDIR WRKSRC OBJDIR CONFIGURE_DIRS BUILD_DIRS MAKE_DIRS DISTFILES EXTRACT_ONLY '
                  'MASTER_SITES DIST_SUBDIR PATCHFILES PATCH_SITES MACHINE_GNU_PLATFORM GCC_TARGET_MACHINE '
                  'MAKE SHELL MAKECONF NATIVE_PKG_INFO_CMD NATIVE_PKG_ADMIN_CMD .MAKE.MAKEFILES'.split())
VARIABLES = (*LEGACY_VARIABLES, 'BOOTSTRAP_DEPENDS')


def _parallel_profile(digest):
    return digest in (PARALLEL_PROFILE_SHA256, BUILTIN_PROFILE_SHA256, LIBTOOL_PROFILE_SHA256,
                      RANLIB_PROFILE_SHA256) or _native_provider_profile(digest)


def _native_provider_profile(digest):
    return digest in (NATIVE_PROVIDER_PROFILE_SHA256, NO_DEFAULT_RPATH_PROFILE_SHA256,
                      UNPRIVILEGED_PROFILE_SHA256, BSDINSTALL_PROFILE_SHA256, FOUNDATION_PROFILE_SHA256,
                      BOOTSTRAP_PKGTOOLS_PROFILE_SHA256, HOST_RUBY_PROFILE_SHA256,
                      MAKE_HANDOFF_PROFILE_SHA256, SHELL_HANDOFF_PROFILE_SHA256,
                      BUILTIN_HELPER_PROFILE_SHA256, CA_PREFIX_PROFILE_SHA256,
                      METADATA_PHASE_PROFILE_SHA256, CA_METADATA_PHASE_PROFILE_SHA256,
                      GZIP_HANDOFF_PROFILE_SHA256, SOURCE_GZIP_HANDOFF_PROFILE_SHA256,
                      SOURCE_GZIP_MAKE_SPLIT_PROFILE_SHA256, RUNTIME_FLAGS_REAPPLY_PROFILE_SHA256,
                      NATIVE_RUNTIME_SHELL_PROFILE_SHA256, INITIAL_DARWIN_PROFILE_SHA256,
                      PERL_DTRACE_PROFILE_SHA256, ICONV_PROFILE_SHA256, LIBTOOL_ICONV_PROFILE_SHA256,
                      FINAL_RUNTIME_FLAGS_PROFILE_SHA256)


def _recognized_profile(digest):
    return digest == PROFILE_SHA256 or _parallel_profile(digest)


def darwin_tool_phase(plan):
    """Select the bounded initial phase without changing historical full plans."""
    data = plan['metadata']
    phase = data.get('darwin_tool_phase', 'full')
    _require(type(phase) is str and phase in ('initial', 'full'), 'unsupported Darwin tool phase')
    _require(phase != 'initial' or data['profile']['sha256'] in (
                 INITIAL_DARWIN_PROFILE_SHA256, PERL_DTRACE_PROFILE_SHA256),
             'initial Darwin tools require their pinned common profile')
    return phase


def required_recipe_utilities(plan):
    """Keep future graph utilities separate from the fixed executed prefix."""
    if darwin_tool_phase(plan) == 'full':
        recipes = plan['metadata']['recipes']
    elif plan.get('stage') == 'metadata' and plan.get('format') == PRODUCER_FORMAT:
        recipes = ()  # The fixed -V queries run no recipe configure or build.
    else:
        from .bootstrap_recipe import initial_execution_order
        recipes = initial_execution_order(plan)
    return {name: roles for name, roles in RECIPE_UTILITIES.items() if name in recipes}


def _recipe_name(value):
    return (isinstance(value, str) and
            re.fullmatch(r'[A-Za-z0-9_+-]+/[A-Za-z0-9_+.-]+', value) is not None and
            value.split('/')[1] not in ('.', '..'))


def metadata_values(reference):
    """Read fixed named observations as data. Never execute shell expressions."""
    try:
        text = _bytes(reference).decode('utf-8')
    except UnicodeDecodeError as exc:
        raise SDKError('metadata observations must be UTF-8') from exc
    _require(text.endswith('\n') and '\r' not in text and '\x00' not in text,
             'unsupported metadata observation framing')
    lines = text.split('\n')[:-1]
    rosters = {len(VARIABLES): VARIABLES, len(LEGACY_VARIABLES): LEGACY_VARIABLES}
    _require(len(lines) in rosters, 'metadata observations must contain a complete fixed roster')
    result = {}
    for expected, line in zip(rosters[len(lines)], lines):
        name, separator, value = line.partition('=')
        _require(separator and name == expected,
                 'metadata observation names or order differ from the fixed roster')
        result[name] = value
    return result


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
    phase = darwin_tool_phase(plan)
    facts = data['host_facts']
    variables = {'EMBERBSD_DARWIN_HOST_' + key.upper(): _word(value) for key, value in facts.items()}
    variables.update(metadata_paths(work))
    if 'darwin_tool_phase' in data:
        variables['EMBERBSD_DARWIN_TOOL_PHASE'] = phase
    if plan.get('format') == PRODUCER_FORMAT and plan.get('stage') == 'metadata':
        variables['EMBERBSD_DARWIN_METADATA'] = 'yes'
    for key, role in [('MAKE', 'make'), ('GMAKE', 'gmake')]:
        variables['EMBERBSD_DARWIN_' + key] = shlex.join([_word(word) for word in commands[role]])
    variables['EMBERBSD_DARWIN_SH'] = _safe_path(commands['sh'][0])
    for role, language in [('cc', 'cflags'), ('cxx', 'cxxflags'), ('cpp', None)]:
        words = [*commands[role], *flags['cppflags'], *(flags[language] if language else ())]
        variables['EMBERBSD_GCC16_SEED_' + role.upper()] = shlex.join([_word(word) for word in words])
    for key, role in [('LLVM_MC', 'as'), ('LD64_LLD', 'ld'), ('DSYMUTIL', 'dsymutil'),
                      ('AR', 'ar'), ('RANLIB', 'ranlib'), ('NM', 'nm'), ('STRIP', 'strip'),
                      ('UNAME', 'uname')]:
        _require(len(commands[role]) == 1, 'metadata utility must have one selected invocation')
        variables['EMBERBSD_GCC16_' + key] = _safe_path(commands[role][0])
    if _native_provider_profile(data['profile']['sha256']):
        for key, role in _DARWIN_CMAKE_TOOLS:
            if role in plan['required_roles']:
                _require(len(commands.get(role, ())) == 1 and
                         descriptor['tools'].get(role, {}).get('kind') == 'native',
                         'Darwin CMake selection needs one explicit native role: ' + role)
                variables['EMBERBSD_DARWIN_' + key] = _safe_path(commands[role][0])
                if role == 'llvm_readtapi':
                    variables['EMBERBSD_DARWIN_TAPI_KIND'] = 'llvm-readtapi'
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
    if plan.get('format') == 'emberbsd-native-gcc16-bootstrap-plan':
        for role, name in [('pkg_add', 'ADD'), ('pkg_create', 'CREATE')]:
            _require(role in commands and len(commands[role]) == 1,
                     'bound recipe installation needs one selected native package command')
            variables['NATIVE_PKG_' + name + '_CMD'] = _safe_path(commands[role][0])
    for role in data['native_tools']:
        _require(len(commands[role]) == 1, 'raw metadata tools cannot omit a selected command tail')
        variables['EMBERBSD_DARWIN_TOOL.' + role] = _safe_path(commands[role][0])
    if _parallel_profile(data['profile']['sha256']):
        variables['EMBERBSD_DARWIN_CPU_BUDGET'] = str(plan['resources']['cpu_jobs'])
        variables['EMBERBSD_GCC16_LINKER_LAUNCHER'] = ''
        if 'link_control' in plan:
            from .bootstrap_recipe import recipe_linker_launcher
            variables['EMBERBSD_GCC16_LINKER_LAUNCHER'] = recipe_linker_launcher(plan)
        if _native_provider_profile(data['profile']['sha256']):
            variables['EMBERBSD_DARWIN_CMAKE_LINKER_LAUNCHER'] = ''
            if 'link_control' in plan:
                from .bootstrap_recipe import recipe_cmake_linker_launcher
                variables['EMBERBSD_DARWIN_CMAKE_LINKER_LAUNCHER'] = recipe_cmake_linker_launcher(plan)
    return variables


def metadata_makeconf(plan, descriptor, commands, flags):
    from .darwin_makeconf import render_variables
    variables = metadata_assignments(plan, descriptor, commands, flags)
    return render_variables(variables, plan['metadata']['profile']['path'],
        nested_ldflags=plan.get('format') == 'emberbsd-native-gcc16-bootstrap-plan',
        metadata_make=variables['EMBERBSD_DARWIN_MAKE'] if
            plan.get('format') == PRODUCER_FORMAT and plan.get('stage') == 'metadata' else None)


def metadata_environment(plan, commands):
    """Use the same exact environment for metadata and bound recipe commands."""
    data, work = plan['metadata'], _safe_path(plan['work_root'])
    result = dict(PATH=_safe_path(data['utility_bin']), LC_ALL='C', TZ='UTC', TMPDIR=work,
               MAKECONF=_safe_path(data['makeconf']['path']), SHELL=_safe_path(commands['sh'][0]),
               CONFIG_SHELL=_safe_path(commands['sh'][0]))
    if plan['format'] == 'emberbsd-native-gcc16-bootstrap-plan':
        _require(len(commands.get('strip', ())) == 1,
                 'GCC comparison needs one selected strip invocation')
        _require(len(commands.get('uname', ())) == 1,
                 'GCC diagnostics need one selected uname invocation')
        result['GCC_CONFIGURE_SDK'] = 'yes'
        result['GCC_CONFIGURE_UNAME'] = _safe_path(commands['uname'][0])
        result['GCC_COMPARE_DEBUG_STRIP'] = _safe_path(commands['strip'][0])
        prefix = _safe_path(plan['output_bundle_root'])
        result['GCC_COMPARE_DEBUG_OBJDUMP'] = os.path.join(prefix, 'bin', 'llvm-objdump')
        result['GCC_COMPARE_DEBUG_OBJCOPY'] = os.path.join(prefix, 'bin', 'llvm-objcopy')
    if 'link_control' in plan:
        from .bootstrap_recipe import recipe_linker_launcher
        # The direct GNU compare command does not pass through pkgsrc MAKE_ENV.
        result['GCC_HOST_LINKER_LAUNCHER'] = recipe_linker_launcher(plan)
    return result


def producer_steps(plan, descriptor, commands, flags):
    """Construct only fixed read-only queries. Never accept an argv extension."""
    data, work = plan['metadata'], _safe_path(plan['work_root'])
    env = metadata_environment(plan, commands)
    queries = [('vars', [word for name in VARIABLES for word in ('-V', name + '=${' + name + '}')]),
               ('tools', ['show-tools'])]
    queries += [('depends-' + name.lower(), ['show-depends', 'VARNAME=' + name])
                for name in ('TOOL_DEPENDS', 'BUILD_DEPENDS', 'DEPENDS', 'TEST_DEPENDS')]
    result = []
    for number, recipe in enumerate(data['recipes']):
        for name, tail in queries:
            result.append(dict(id=f'r{number:02d}-' + name, phase='metadata', recipe=recipe, cwd=work,
                               argv=[*commands['make'], 'EMBERBSD_DARWIN_BOOTSTRAP=yes',
                                     'MAKE=' + shlex.join(commands['make']), '-j' + str(plan['resources']['cpu_jobs']), '-C',
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


def _export_proof(data, descriptor, inventory, *, inspect_inputs=True):
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
        if inspect_inputs:
            _bytes(row['file'])
    profile_sha = data['profile']['sha256']
    _require(_recognized_profile(profile_sha) and
             manifest.get(PROFILE_RELATIVE, {}).get('sha256') == profile_sha and
             inventory.get(PROFILE_EXPORT, {}).get('sha256') == profile_sha,
             'prepared export profile differs from its source-owner bytes')


def validate_metadata_inputs(plan, descriptor, commands, flags, *, inspect_inputs=True, recipe_limit=RECIPE_LIMIT):
    """Validate shared source-owner inputs. Offline graduation retains their pins."""
    data = plan['metadata']
    _require(set(data) == {'export', 'profile', 'makeconf', 'host_facts', 'utility_bin',
                          'native_tools', 'source_tools', 'recipes'} |
             ({'darwin_tool_phase'} if 'darwin_tool_phase' in data else set()),
             'unsupported metadata input fields')
    phase = darwin_tool_phase(plan)
    parallel = 'link_control' in plan
    _require(not parallel or plan.get('format') == 'emberbsd-native-gcc16-bootstrap-plan' and
             _parallel_profile(data['profile']['sha256']),
             'link control needs the recognized parallel full-plan profile')
    _require(not _parallel_profile(data['profile']['sha256']) or parallel or
             plan['resources']['cpu_jobs'] == 1,
             'parallel profile jobs require the full-plan link control service')
    _require((not inspect_inputs or data['host_facts'] == _current_host_facts()) and
             data['host_facts']['os'] == 'Darwin' and
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
    if {'lang/llvm', 'devel/lld', 'devel/cmake'} & set(data['recipes']):
        _require(_native_provider_profile(data['profile']['sha256']),
                 'CMake provider recipes need the native-provider source profile')
        for _, role in (_DARWIN_CMAKE_TOOLS if phase == 'full' else ()):
            _require(role in plan['required_roles'] and len(commands.get(role, ())) == 1 and
                     descriptor['tools'].get(role, {}).get('kind') == 'native',
                     'CMake provider recipes need an explicit native role: ' + role)
    _require(not set(data['native_tools']) & {'cc', 'cxx', 'cpp', 'as', 'ld', 'make', 'gmake', 'sh'},
             'metadata raw utility PATH cannot bypass compiler or make bindings')
    _require(isinstance(data['recipes'], list) and 1 <= len(data['recipes']) <= recipe_limit and
             all(_recipe_name(name) for name in data['recipes']) and
             len(data['recipes']) == len({name.casefold() for name in data['recipes']}) and
             'lang/gcc16' in data['recipes'], 'metadata needs a finite explicit recipe list')
    for recipe, roles in required_recipe_utilities(plan).items():
        for role in roles:
            _require(role in data['native_tools'] and role in plan['required_roles'] and
                     descriptor['tools'].get(role, {}).get('kind') == 'native',
                     f'{recipe} needs native utility {role} in the utility PATH')
    export = data['export']
    _require(set(export) == {'root', 'inventory', 'source_proof'}, 'unsupported prepared export fields')
    root = _safe_path(export['root'])
    inventory = _inventory(export['inventory'])
    if inspect_inputs:
        _require(canonical_existing(root) == root and snapshot_tree(root) == inventory,
                 'prepared canonical export changed')
    _export_proof(data, descriptor, inventory, inspect_inputs=inspect_inputs)
    _require(_recognized_profile(data['profile']['sha256']) and
             data['profile'] == dict(path=os.path.join(root, PROFILE_EXPORT), sha256=data['profile']['sha256']),
             'metadata needs the recognized source-owner profile')
    if inspect_inputs:
        _bytes(data['profile'])
    _require(all(inventory.get(name + '/Makefile', {}).get('kind') == 'file' for name in data['recipes']),
             'metadata recipe is absent from the complete prepared export')
    _require(descriptor.get('make_binding') is not None and
             data['makeconf'] == descriptor['make_binding']['makeconf'] and
             data['makeconf']['sha256'] == hashlib.sha256(
                 metadata_makeconf(plan, descriptor, commands, flags)).hexdigest(),
             'metadata MAKECONF differs from its accepted native selection')
    if inspect_inputs:
        _bytes(data['makeconf'])
        _utility_directory(data, descriptor, commands)
    return data


def validate_producer_plan(plan, descriptor, commands, flags):
    if plan.get('stage') == 'utility-relocation':
        from .bootstrap_relocation import validate_relocation_plan
        return validate_relocation_plan(plan, descriptor, commands, flags)
    from .bootstrap_program import PROGRAM_STAGES, validate_program_plan
    from .bootstrap_gnu_program import GNU_PROGRAM_STAGES
    if plan.get('stage') in (*PROGRAM_STAGES, *GNU_PROGRAM_STAGES,
                            'bsdinstall-bootstrap', 'pkg-install-bootstrap', 'byacc-bootstrap', 'ports-libnbcompat-bootstrap',
                            'nawk-bootstrap'):
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
    validate_metadata_inputs(plan, descriptor, commands, flags)
    _require(plan['commands'] == producer_steps(plan, descriptor, commands, flags),
             'metadata command differs from the fixed source-owned query')
    return plan
