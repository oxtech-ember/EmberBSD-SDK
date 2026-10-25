# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Fixed NetBSD 11 first-native recipe plan and source receipt validation."""

import os
import platform
import re
import shlex
import stat

from .inventory import sha256_file, snapshot_tree
from .errors import SDKError
from .native_toolset import _absolute, _gcc, _require, _sha, _source, _envelope, _json, _file
from .paths import canonical_existing, canonical_new, inside, reject_overlap

PROFILE = 'netbsd11-amd64-pkgsrc-native-gcc16-v1'
PROFILE_FILE = 'EMBERBSD-NETBSD-BOOTSTRAP-MK.CONF'
_SAFE_CONFIG = re.compile(r'[A-Za-z0-9_./+:-]+\Z')


def _ref(value, label):
    _require(isinstance(value, dict) and set(value) == {'path', 'sha256'},
             f'{label} needs an exact path and SHA256')
    return dict(path=_absolute(value['path']), sha256=_sha(value['sha256']))


def _opsys_version(release):
    match = re.fullmatch(r'(\d+)\.(\d+)(?:\.(\d+))?', release)
    _require(match is not None, 'NetBSD release cannot map to pkgsrc OPSYS_VERSION')
    major, minor, patch = (int(match.group(1)), int(match.group(2)),
                           int(match.group(3) or 0))
    _require(major < 100 and minor < 100 and patch < 100,
             'NetBSD release components exceed pkgsrc OPSYS_VERSION format')
    return major * 10000 + minor * 100 + patch


def _compiler_prefix(descriptor, commands, *, inspect_inputs=True):
    cc = _absolute(commands['cc'][0])
    cxx = _absolute(commands['cxx'][0])
    _require(descriptor.get('tools', {}).get('cc', {}).get('path') == cc and
             descriptor.get('tools', {}).get('cxx', {}).get('path') == cxx,
             'GCC16 seed compiler commands differ from accepted descriptor tools')
    prefix = os.path.dirname(os.path.dirname(cc))
    _require(os.path.basename(cc) == 'gcc' and os.path.basename(cxx) == 'g++' and
             os.path.dirname(os.path.dirname(cxx)) == prefix and
             inside(prefix, descriptor['bundle_root']) and prefix != descriptor['bundle_root'],
             'GCC16 seed compiler prefix must be derived from its selected cc/cxx pair')
    if inspect_inputs:
        _require(canonical_existing(cc) == cc and canonical_existing(cxx) == cxx and
                 canonical_existing(prefix) == prefix,
                 'GCC16 seed compiler spelling changed')
    return prefix


def render_netbsd_makeconf(plan, descriptor, commands):
    """Render the one supported NetBSD pkgsrc make configuration."""
    netbsd = plan['netbsd']
    roots = netbsd['host_facts']
    work = _absolute(plan['work_root'])
    localbase = _absolute(plan['output_bundle_root'])
    compiler_prefix = _compiler_prefix(descriptor, commands, inspect_inputs=False)
    fields = [
        ('EMBERBSD_NETBSD_BOOTSTRAP', 'yes'),
        ('EMBERBSD_NETBSD_HOST_OS', roots['os']),
        ('EMBERBSD_NETBSD_HOST_CPU', roots['cpu']),
        ('EMBERBSD_NETBSD_KERNEL_VERSION', roots['kernel_version']),
        ('EMBERBSD_NETBSD_PKGSRC_OPSYS_VERSION', str(roots['pkgsrc_opsys_version'])),
        ('EMBERBSD_NETBSD_SH', netbsd['pkgsrc_tools']['sh']),
        ('EMBERBSD_NETBSD_MAKE', netbsd['pkgsrc_tools']['make']),
        ('EMBERBSD_NETBSD_GMAKE', netbsd['pkgsrc_tools']['gmake']),
        ('EMBERBSD_NETBSD_UTILITY_BIN', netbsd['utility_bin']),
        ('EMBERBSD_NETBSD_NATIVE_TOOLS', ' '.join(sorted(netbsd['utility_tools']))),
        ('EMBERBSD_NETBSD_JOBS', str(plan['resources']['cpu_jobs'])),
        ('MAKE_JOBS', str(plan['resources']['cpu_jobs'])),
        ('PKGSRC_COMPILER', 'gcc'), ('GCC_REQD', '16'),
        ('GCCBASE', compiler_prefix), ('LOCALBASE', localbase), ('PREFIX', localbase),
        ('WRKOBJDIR', os.path.join(work, 'work')),
        ('DISTDIR', os.path.join(work, 'distfiles')),
        ('PKG_DBDIR', os.path.join(work, 'pkgdb')),
        ('PACKAGES', os.path.join(work, 'packages')),
        ('EMBERBSD_GCC16_SEED_CC', shlex.join(commands['cc'])),
        ('EMBERBSD_GCC16_SEED_CXX', shlex.join(commands['cxx'])),
        ('EMBERBSD_GCC16_SEED_CPP', shlex.join(commands['cpp'])),
    ]
    for role in ('as', 'ld', 'ar', 'ranlib', 'nm', 'strip'):
        fields.append(('EMBERBSD_GCC16_' + role.upper(), commands[role][0]))
    lines = []
    for name, value in fields:
        _require(re.fullmatch(r'[A-Za-z0-9_.]+', name) is not None and
                 re.fullmatch(r'[A-Za-z0-9_./+: -]+', value) is not None,
                 f'unsafe NetBSD make configuration value for {name}')
        lines.append(f'{name}={value}')
    for role in sorted(netbsd['utility_tools']):
        value = netbsd['utility_tools'][role]['path']
        _require(_SAFE_CONFIG.fullmatch(role) and _SAFE_CONFIG.fullmatch(value),
                 f'unsafe accepted NetBSD utility mapping for {role}')
        lines.append(f'EMBERBSD_NETBSD_TOOL.{role}={value}')
    profile = _absolute(netbsd['profile']['path'])
    _require(_SAFE_CONFIG.fullmatch(profile), 'unsafe NetBSD profile path')
    lines.append(f'.include "{profile}"')
    return ('\n'.join(lines) + '\n').encode('ascii')


def _accepted_sources(netbsd, source, *, inspect_inputs=True):
    """Check target-side source-transfer acceptance without opening exporter paths."""
    from .bootstrap_pkgsrc_export import validate_export_transfer_acceptance
    export = _ref(netbsd['source_export_acceptance'], 'source export acceptance')
    accepted = _json(export)
    transfer = _ref(netbsd['source_transfer_acceptance'], 'source transfer acceptance')
    transfer_ref, transfer_receipt = validate_export_transfer_acceptance(
        transfer['path'], transfer['sha256'], export_acceptance_ref=export,
        inspect_inputs=inspect_inputs)
    _require(accepted.get('format') == 'emberbsd-bootstrap-pkgsrc-export-acceptance' and
             accepted.get('status') == 'PASS' and
             accepted.get('scope') == 'pkgsrc-source-export' and
             accepted.get('source_proof') == transfer_receipt['source_proof'] and
             accepted['source_proof']['ports_source'] == source and
             transfer_receipt['output_root'] == netbsd['ports_export_root'] and
             transfer_receipt['output_inventory'] == netbsd['source_inventory'] and
             source == netbsd['ports_source'],
             'NetBSD bootstrap source receipts do not describe the selected Ports source')
    return export, transfer_ref, transfer_receipt


_RESERVED_UTILITY_ROLES = {
    'cc', 'cxx', 'cpp', 'c++', 'gcc', 'g++', 'clang', 'clang++',
    'make', 'bmake', 'gmake', 'sh', 'as', 'ld', 'ar', 'ranlib', 'nm', 'strip',
}


def _validate_utility_roster(utility_bin, utility_tools, *, inspect_inputs=True):
    """Pin a dedicated executable helper directory without compiler fallbacks."""
    role_name = re.compile(r'[a-z][a-z0-9_-]*\Z')
    _require(isinstance(utility_tools, dict) and utility_tools and
             all(isinstance(role, str) and role_name.fullmatch(role) and
                 role not in _RESERVED_UTILITY_ROLES for role in utility_tools),
             'NetBSD native utility roster contains an unsafe or reserved role')
    for role, value in utility_tools.items():
        reference = _ref(value, f'NetBSD native utility {role}')
        _require(os.path.dirname(reference['path']) == utility_bin and
                 os.path.basename(reference['path']) == role,
                 f'NetBSD utility {role} must be the exact named member of its declared directory')
    if inspect_inputs:
        _require(canonical_existing(utility_bin) == utility_bin and
                 os.path.isdir(utility_bin) and not os.path.islink(utility_bin),
                 'NetBSD utility directory changed or is a symlink')
        _require(set(os.listdir(utility_bin)) == set(utility_tools),
                 'NetBSD utility directory has undeclared or missing members')
        for role, value in utility_tools.items():
            reference = _ref(value, f'NetBSD native utility {role}')
            try:
                metadata = os.lstat(reference['path'])
            except OSError as exc:
                raise SDKError(f'NetBSD native utility {role} is unavailable: {exc}') from exc
            _require(stat.S_ISREG(metadata.st_mode) and metadata.st_mode & 0o111,
                     f'NetBSD utility {role} must be a regular executable file')
            _file(reference)
    return utility_tools


def netbsd_recipe_steps(plan, descriptor, commands, flags):
    """Return a closed, deterministic make/install sequence for the recipe DAG."""
    from .bootstrap_recipe import recipe_order
    from .bootstrap_seed import _environment
    netbsd = plan['netbsd']
    tools = netbsd['pkgsrc_tools']
    base_commands = dict(commands)
    base_commands['make'] = (tools['make'],)
    base_commands['gmake'] = (tools['gmake'],)
    environment = _environment({role: words[0] for role, words in base_commands.items()},
                               base_commands, flags, descriptor)
    work = _absolute(plan['work_root'])
    export = _absolute(plan['ports_export']['root'])
    localbase = _absolute(plan['output_bundle_root'])
    binutils_as = os.path.join(localbase, 'bin/gas')
    binutils_ld = os.path.join(localbase, 'bin/gld')
    _require(netbsd['binutils_as'] == binutils_as and netbsd['binutils_ld'] == binutils_ld,
             'NetBSD GCC stage must use source-built binutils output paths')
    workobj = os.path.join(work, 'work')
    distdir = os.path.join(work, 'distfiles')
    pkgdb = os.path.join(work, 'pkgdb')
    packages = os.path.join(work, 'packages')
    shell = tools['sh']
    compiler_prefix = _compiler_prefix(descriptor, commands, inspect_inputs=False)
    utility_bin = _absolute(netbsd['utility_bin'])
    environment.update(LC_ALL='C', TZ='UTC', TMPDIR=work, HOME=os.path.join(work, 'home'),
        PATH=os.pathsep.join((utility_bin, os.path.dirname(tools['make']), compiler_prefix + '/bin')),
        SHELL=shell, CONFIG_SHELL=shell, MAKECONF=netbsd['makeconf']['path'],
        PKGSRC_COMPILER='gcc', GCC_REQD='16', GCCBASE=compiler_prefix,
        EMBERBSD_NETBSD_BOOTSTRAP='yes', MAKE_JOBS=str(plan['resources']['cpu_jobs']),
        LOCALBASE=localbase, PREFIX=localbase, WRKOBJDIR=workobj,
        DISTDIR=distdir, PKG_DBDIR=pkgdb, PACKAGES=packages)
    pkgsrc_mk = netbsd['pkgsrc_mk']
    steps = []
    built = []
    for number, recipe in enumerate(recipe_order(plan)):
        row = plan['recipes'][recipe]
        dependencies = row['dependencies']
        # pkgsrc's GCC consumer must see the source-built binutils provider.
        if recipe == 'lang/gcc16':
            _require('devel/binutils' in dependencies and 'devel/binutils' in built,
                     'GCC recipe must follow its source-built binutils provider')
        argv_prefix = [tools['make'], '-m', pkgsrc_mk]
        for phase in ('build', 'install'):
            step_env = dict(environment)
            step_argv = list(argv_prefix)
            if recipe == 'lang/gcc16':
                step_env.update(AS=binutils_as, LD=binutils_ld)
                step_argv.extend(('AS=' + binutils_as, 'LD=' + binutils_ld))
            steps.append(dict(id=f'r{number:03d}-{phase}', recipe=recipe, phase=phase,
                role='make', cwd=work, env=step_env,
                argv=[*step_argv, '-C', os.path.join(export, recipe), phase]))
        built.append(recipe)
        if recipe == 'lang/gcc16':
            object_dir = _absolute(row['object_directory'])
            steps.insert(-1, dict(id='gcc16-compare', recipe=recipe, phase='compare', role='gmake',
                cwd=work, env=dict(environment),
                argv=[tools['gmake'], '--no-print-directory', '-j1', '-C', object_dir,
                      '--eval=.PHONY: compare', 'compare']))
    return steps


def validate_netbsd_recipe_plan(plan, descriptor, commands, flags, *, inspect_inputs=True):
    """Validate the NetBSD branch of the existing first-native GCC16 plan."""
    allowed = {'format', 'version', 'scope', 'purpose', 'seed_descriptor', 'host', 'sources',
        'output_compiler', 'ports_export', 'recipes', 'work_root', 'output_bundle_root',
        'required_roles', 'commands', 'comparison_command', 'resources', 'netbsd_profile', 'netbsd'}
    _require(set(plan) == allowed and plan['netbsd_profile'] == PROFILE,
             'unsupported NetBSD first-native plan fields or profile')
    _envelope(plan, 'emberbsd-native-gcc16-bootstrap-plan', scope='gcc16-bootstrap')
    _require(plan['purpose'] == 'first-native-gcc16' and
             plan['output_compiler']['version'] == '16.2.0',
             'NetBSD plan is limited to the first GCC16 provider')
    _gcc(plan['output_compiler'], plan['host'])
    for source in plan['sources'].values():
        _source(source)
    _require(descriptor['host'] == {'os': 'NetBSD', 'cpu': 'amd64'} and
             plan['host'] == descriptor['host'] and plan['sources'] == descriptor['sources'],
             'NetBSD first-native plan host/source pins mismatch')
    netbsd = plan['netbsd']
    _require(isinstance(netbsd, dict) and set(netbsd) == {'pkgsrc_tools_acceptance',
        'source_export_acceptance', 'source_transfer_acceptance', 'source_inventory',
        'ports_export_root', 'ports_source', 'profile', 'host_facts', 'utility_bin',
        'utility_tools', 'makeconf', 'pkgsrc_tools', 'pkgsrc_mk', 'binutils_as', 'binutils_ld'},
        'unsupported NetBSD binding fields')
    _ref(netbsd['source_inventory'], 'transferred source inventory')
    _require(netbsd['ports_source'] == plan['sources']['ports'] and
             netbsd['ports_export_root'] == plan['ports_export']['root'],
             'NetBSD selected Ports export differs from the pinned source')
    _, _, source_receipt = _accepted_sources(netbsd, netbsd['ports_source'],
                                               inspect_inputs=inspect_inputs)
    from .bootstrap_netbsd_gcc16 import load_pkgsrc_bootstrap_tools
    pkgsrc_ref = _ref(netbsd['pkgsrc_tools_acceptance'], 'pkgsrc tools acceptance')
    selected_pkgsrc = load_pkgsrc_bootstrap_tools(pkgsrc_ref['path'],
        acceptance_sha256=pkgsrc_ref['sha256'], inspect_inputs=inspect_inputs)
    tools = dict(make=selected_pkgsrc.command('make')[0],
                 gmake=selected_pkgsrc.command('gmake')[0],
                 sh=selected_pkgsrc.command('sh')[0])
    _require(netbsd['pkgsrc_tools'] == tools,
             'NetBSD plan tool spellings differ from the accepted pkgsrc outputs')
    _require(netbsd['pkgsrc_mk'] == selected_pkgsrc.pkgsrc_mk,
             'NetBSD pkgsrc make files differ from the independently accepted bootstrap prefix')
    facts = netbsd['host_facts']
    _require(isinstance(facts, dict) and set(facts) ==
             {'os', 'cpu', 'kernel_version', 'pkgsrc_opsys_version'} and
             facts['os'] == 'NetBSD' and facts['cpu'] == 'x86_64' and
             type(facts['pkgsrc_opsys_version']) is int and
             facts['pkgsrc_opsys_version'] == _opsys_version(facts['kernel_version']),
             'NetBSD host facts do not match the pkgsrc release encoding')
    if inspect_inputs:
        _require(platform.system() == 'NetBSD' and platform.machine() == 'amd64' and
                 platform.release() == facts['kernel_version'],
                 'NetBSD host identity or kernel release changed')
    utility_bin = _absolute(netbsd['utility_bin'])
    utility_tools = netbsd['utility_tools']
    _validate_utility_roster(utility_bin, utility_tools, inspect_inputs=inspect_inputs)
    for role, reference in utility_tools.items():
        admitted = descriptor.get('tools', {}).get(role, {})
        _require(admitted.get('path') == reference['path'] and
                 admitted.get('sha256') == reference['sha256'],
                 f'NetBSD utility {role} differs from the accepted seed member')
    profile_ref = _ref(netbsd['profile'], 'NetBSD source-owned profile')
    _require(profile_ref['path'] == os.path.join(plan['ports_export']['root'], PROFILE_FILE),
             'NetBSD source profile does not use the fixed export filename')
    makeconf = _ref(netbsd['makeconf'], 'NetBSD MAKECONF')
    if inspect_inputs:
        _file(makeconf)
    export = plan['ports_export']
    _require(export['source'] == plan['sources']['ports'], 'NetBSD export source pin mismatch')
    from .bootstrap_producer import _inventory
    inventory = _inventory(export['inventory'])
    transferred_inventory = _inventory(netbsd['source_inventory'])
    _require(inventory == transferred_inventory and
             source_receipt['output_inventory'] == netbsd['source_inventory'],
             'NetBSD export plan does not pin the target accepted inventory')
    profile_row = inventory.get(PROFILE_FILE)
    _require(isinstance(profile_row, dict) and profile_row.get('kind') == 'file' and
             profile_row.get('sha256') == profile_ref['sha256'],
             'NetBSD source profile differs from the transferred export inventory')
    if inspect_inputs:
        _require(sha256_file(profile_ref['path']) == profile_ref['sha256'],
                 'NetBSD source profile changed after transfer')
    expected_makeconf = render_netbsd_makeconf(plan, descriptor, commands)
    if inspect_inputs:
        with open(makeconf['path'], 'rb') as stream:
            observed_makeconf = stream.read(len(expected_makeconf) + 1)
        _require(observed_makeconf == expected_makeconf,
                 'NetBSD MAKECONF differs from its closed source-owned profile binding')
    if inspect_inputs:
        _require(snapshot_tree(export['root']) == inventory,
                 'NetBSD Ports source export inventory changed')
    from .bootstrap_recipe import recipe_order, recipe_distfiles, _local_source_distfiles
    order = recipe_order(plan)
    _require('devel/binutils' in order and order.index('devel/binutils') < order.index('lang/gcc16'),
             'NetBSD GCC16 closure must source-build binutils before GCC')
    recipes = plan['recipes']
    _require(isinstance(recipes, dict),
             'NetBSD recipe set is not contained in the source export')
    for name, row in recipes.items():
        _require(row['directory'] == name,
                 'NetBSD recipe directory differs from its common Ports name')
        prefix = name + '/'
        expected_tree = {path[len(prefix):]: item for path, item in inventory.items()
                         if path.startswith(prefix)}
        _require(expected_tree and _json(row['inventory']) == expected_tree and
                 'Makefile' in expected_tree,
                 'NetBSD recipe inventory differs from the accepted source export')
        patches = row['patches']
        available = {path for path, item in expected_tree.items()
                     if path.startswith('patches/patch-') and item.get('kind') == 'file'}
        _require(isinstance(patches, list) and len(patches) == len(set(patches)) and
                 set(patches) == available, 'NetBSD recipe patch inventory is incomplete')
        for patch in patches:
            _require(patch.startswith('patches/') and expected_tree.get(patch, {}).get('kind') == 'file',
                     'NetBSD recipe patch is not in the accepted source export')
        if not row['distfiles']:
            _local_source_distfiles(name, row, _json(row['inventory']))
        else:
            for distfile in row['distfiles']:
                _require(isinstance(distfile, dict) and set(distfile) == {'url', 'file', 'relative'},
                         'NetBSD distfile must have an explicit source and destination')
                _sha(distfile['file']['sha256'])
                _absolute(distfile['file']['path'])
                _require(isinstance(distfile['relative'], str) and
                         not os.path.isabs(distfile['relative']) and '..' not in distfile['relative'].split('/'),
                         'NetBSD distfile destination escapes its isolated cache')
    roles = plan['required_roles']
    _require(isinstance(roles, list) and all(isinstance(role, str) for role in roles) and
             len(roles) == len(set(roles)) and set(roles) >=
             {'cc', 'cxx', 'cpp', 'as', 'ld', 'ar', 'ranlib', 'nm', 'strip', 'make', 'gmake', 'sh'},
             'NetBSD first-native tool role closure is incomplete')
    # Control-plane Python does not provide a native recipe generator role.
    # Consumers that request one still need an explicitly admitted command.
    _require(set(roles) <= set(commands) | {'make', 'gmake', 'sh'},
             'NetBSD required native role has no accepted command')
    _require(descriptor.get('make_binding') is None,
             'NetBSD route cannot consume a Darwin-bound make profile')
    from .bootstrap_pilot import _validate_resources
    _validate_resources(plan['resources'], recipe=True, inspect_inputs=inspect_inputs)
    work = _absolute(plan['work_root'])
    output = _absolute(plan['output_bundle_root'])
    _require(canonical_new(work) == work and inside(output, work) and
             output != work and canonical_new(output) == output,
             'NetBSD bootstrap outputs must be fresh children of its work root')
    for path in (descriptor['bundle_root'], export['root'], makeconf['path'], profile_ref['path'],
                 utility_bin, pkgsrc_ref['path'],
                 netbsd['source_export_acceptance']['path'],
                 netbsd['source_transfer_acceptance']['path']):
        if inspect_inputs:
            reject_overlap(work, path)
    recipe_distfiles(plan)
    expected = netbsd_recipe_steps(plan, descriptor, commands, flags)
    _require(plan['comparison_command'] == 'gcc16-compare' and plan['commands'] == expected,
             'NetBSD recipe commands differ from the closed source-owned route')
    return plan


def prepare_netbsd_recipe_work(plan):
    """Stage pinned source distfiles in the fresh isolated NetBSD work root."""
    from pathlib import Path
    import shutil
    from .bootstrap_recipe import recipe_distfiles
    work = plan['work_root']
    for name in ('distfiles', 'work', 'pkgdb', 'packages', 'home'):
        os.mkdir(os.path.join(work, name))
    os.mkdir(plan['output_bundle_root'])
    for relative, reference in recipe_distfiles(plan).items():
        destination = Path(work, 'distfiles', relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with open(reference['path'], 'rb') as source, destination.open('xb') as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)
        _require(sha256_file(str(destination)) == reference['sha256'],
                 'staged NetBSD recipe distfile differs from its accepted input')
