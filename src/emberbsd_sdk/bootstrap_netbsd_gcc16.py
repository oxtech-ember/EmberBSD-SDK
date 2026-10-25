# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Admit the narrow pkgsrc tools used by the first NetBSD GCC16 package build."""

from dataclasses import dataclass
import os
import stat
from types import MappingProxyType

from .errors import SDKError
from .inventory import sha256_file, snapshot_tree
from .native_toolset import _absolute, _json, _require, _sha
from .paths import canonical_existing, inside, verify_stable
from .process import capture

BOOTSTRAP_NETBSD_GCC16_API = 1
ACCEPTANCE_FORMAT = 'emberbsd-bootstrap-pkgsrc-provider-acceptance'
RESULT_FORMAT = 'emberbsd-bootstrap-pkgsrc-provider-result'
PLAN_FORMAT = 'emberbsd-bootstrap-pkgsrc-provider-plan'
SCOPE = 'pkgsrc-bootstrap-tools'
_ROLES = ('gmake', 'make', 'sh')


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class PkgsrcBootstrapTools:
    """Externally accepted pkgsrc tools; never an ordinary NativeToolset."""

    acceptance_sha256: str
    plan: object
    result: object
    output_inventory: object
    tools: object
    pkgsrc_mk: str

    @property
    def scope(self):
        return SCOPE

    @property
    def accepted_roles(self):
        return _ROLES

    @property
    def ordinary_provider_qualified(self):
        return False

    def command(self, role):
        try:
            return (self.tools[role],)
        except KeyError as exc:
            raise SDKError(f'pkgsrc bootstrap role is not accepted: {role}') from exc


@dataclass(frozen=True)
class NetBSDGCC16CompilerSeed:
    """The accepted source-bound GCC seed, usable only for this bootstrap route."""

    seed_sha256: str
    prefix: str
    compiler: str
    cxx: str
    assembler: str
    linker: str
    version: str
    triplet: str
    source_provenance: object

    @property
    def scope(self):
        return 'bmake-bootstrap'

    def command(self, role):
        if role == 'cc':
            return (self.compiler,)
        if role == 'cpp':
            return (self.compiler, '-E')
        if role == 'cxx':
            return (self.cxx,)
        if role == 'as':
            return (self.assembler,)
        if role == 'ld':
            return (self.linker,)
        raise SDKError(f'compiler seed role is not accepted: {role}')


def _reference(value, label):
    _require(isinstance(value, dict) and set(value) == {'path', 'sha256'},
             f'{label} needs an exact path and SHA256 reference')
    return dict(path=_absolute(value['path']), sha256=_sha(value['sha256']))


def _read_reference(value, label):
    reference = _reference(value, label)
    _require(sha256_file(reference['path']) == reference['sha256'],
             f'{label} changed')
    return reference, _json(reference)


def _file(path, expected_sha, label):
    path = _absolute(path)
    info = os.lstat(path)
    _require(stat.S_ISREG(info.st_mode) and info.st_mode & 0o111,
             f'{label} is not an executable regular file')
    resolved = canonical_existing(path)
    _require(resolved == path and sha256_file(path) == _sha(expected_sha),
             f'{label} changed or escaped its selected spelling')
    verify_stable(path, info)
    return path


def _output_rows(result):
    rows = result.get('outputs')
    _require(isinstance(rows, list) and len(rows) == 3,
             'pkgsrc bootstrap result must report exactly bmake, gmake, and mksh')
    mapped = {}
    for row in rows:
        _require(isinstance(row, dict) and
                 set(row) == {'name', 'path', 'sha256', 'mode'} and
                 row['name'] in ('bmake', 'gmake', 'mksh') and row['name'] not in mapped and
                 type(row['mode']) is int and row['mode'] & 0o111,
                 'pkgsrc bootstrap output role record is invalid')
        mapped[row['name']] = row
    _require(set(mapped) == {'bmake', 'gmake', 'mksh'},
             'pkgsrc bootstrap output roles are incomplete')
    return mapped


def load_pkgsrc_bootstrap_tools(acceptance_path, *, acceptance_sha256, inspect_inputs=True):
    """Load a gate-pinned narrow receipt and recheck its selected tool bytes.

    The expected receipt hash must come from the independent reviewer. The
    producer result alone cannot qualify or select its own tools.
    """
    acceptance_path = _absolute(acceptance_path)
    acceptance_sha256 = _sha(acceptance_sha256)
    _require(sha256_file(acceptance_path) == acceptance_sha256,
             'pkgsrc bootstrap acceptance receipt changed')
    receipt = _json(dict(path=acceptance_path, sha256=acceptance_sha256))
    fields = {'format', 'version', 'scope', 'status', 'plan', 'result',
              'output_inventory', 'export_acceptance', 'transfer_acceptance',
              'compiler_seed', 'accepted_roles', 'checks'}
    _require(isinstance(receipt, dict) and set(receipt) == fields and
             receipt.get('format') == ACCEPTANCE_FORMAT and
             type(receipt.get('version')) is int and receipt['version'] == 1 and
             receipt.get('scope') == SCOPE and receipt.get('status') == 'PASS',
             'independent pkgsrc bootstrap tools acceptance receipt is invalid')
    _require(receipt['accepted_roles'] == list(_ROLES),
             'pkgsrc bootstrap acceptance must contain exactly make, gmake, and sh')
    checks = {'all_commands_drained': True, 'inputs_revalidated_after_drain': True,
              'all_leases_closed': True, 'source_export_matches': True,
              'output_inventory_matches': True}
    _require(receipt['checks'] == checks and
             all(type(receipt['checks'].get(key)) is bool and receipt['checks'][key] is True
                 for key in checks),
             'pkgsrc bootstrap acceptance is missing a required independent check')

    plan_ref, plan = _read_reference(receipt['plan'], 'pkgsrc bootstrap plan')
    result_ref, result = _read_reference(receipt['result'], 'pkgsrc bootstrap result')
    inventory_ref, inventory = _read_reference(receipt['output_inventory'],
                                               'pkgsrc bootstrap output inventory')
    _require(isinstance(plan, dict) and plan.get('format') == PLAN_FORMAT and
             type(plan.get('version')) is int and plan['version'] == 1 and
             plan.get('scope') == SCOPE and plan.get('purpose') == 'bootstrap-current-pkgsrc-tools' and
             plan.get('host') == 'NetBSD11.0-amd64',
             'accepted pkgsrc tools must come from a NetBSD 11 amd64 source-bound plan')
    _require(isinstance(result, dict) and result.get('format') == RESULT_FORMAT and
             type(result.get('version')) is int and result['version'] == 1 and
             result.get('scope') == SCOPE and
             result.get('status') == 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW' and
             result.get('ordinary_provider_qualified') is False and
             result.get('bootstrap_plan') == plan_ref and
             result.get('output_inventory') == inventory_ref and
             result.get('all_commands_drained') is True and
             result.get('inputs_revalidated_after_drain') is True and
             result.get('all_leases_closed') is True,
             'pkgsrc bootstrap result differs from its narrow acceptance')
    input_refs = {}
    for name in ('export_acceptance', 'transfer_acceptance', 'compiler_seed'):
        input_refs[name] = (_read_reference(receipt[name], f'pkgsrc bootstrap {name.replace("_", " ")}')[0]
                            if inspect_inputs else
                            _reference(receipt[name], f'pkgsrc bootstrap {name.replace("_", " ")}'))
    _require(input_refs['export_acceptance'] == plan.get('export_acceptance') and
             input_refs['transfer_acceptance'] == plan.get('transfer_acceptance') and
             input_refs['compiler_seed'] == plan.get('compiler_seed') and
             result.get('export_acceptance') == input_refs['export_acceptance'] and
             result.get('transfer_acceptance') == input_refs['transfer_acceptance'] and
             result.get('compiler_seed') == input_refs['compiler_seed'],
             'accepted pkgsrc tools do not retain their original source and seed pins')
    if inspect_inputs:
        compiler_seed = _json(receipt['compiler_seed'])
        _require(compiler_seed.get('format') == 'emberbsd-bootstrap-compiler-seed' and
                 compiler_seed.get('scope') == 'bmake-bootstrap',
                 'pkgsrc tools must retain the original scoped compiler seed')
    _require(isinstance(inventory, dict) and inventory and
             result.get('output_root') == plan.get('prefix_root'),
             'accepted pkgsrc output root or inventory is incomplete')

    work_root = _absolute(plan['work_root'])
    bootstrap_work = _absolute(plan['bootstrap_work'])
    prefix = _absolute(plan['prefix_root'])
    _require((not inspect_inputs or canonical_existing(work_root) == work_root) and
             (not inspect_inputs or canonical_existing(bootstrap_work) == bootstrap_work) and
             (not inspect_inputs or canonical_existing(prefix) == prefix) and
             inside(bootstrap_work, work_root) and inside(prefix, work_root) and
             bootstrap_work != prefix,
             'pkgsrc bootstrap tool roots overlap or escape their work root')
    _require(not inspect_inputs or snapshot_tree(prefix) == inventory,
             'accepted pkgsrc bootstrap prefix inventory changed')
    rows = _output_rows(result)
    expected_paths = {
        'make': os.path.join(bootstrap_work, 'bin', 'bmake'),
        'gmake': os.path.join(prefix, 'bin', 'gmake'),
        'sh': os.path.join(prefix, 'bin', 'mksh'),
    }
    output_names = {'make': 'bmake', 'gmake': 'gmake', 'sh': 'mksh'}
    tools = {}
    for role, path in expected_paths.items():
        row = rows[output_names[role]]
        _require(_absolute(row['path']) == path,
                 f'pkgsrc bootstrap output escaped its accepted root: {role}')
        tools[role] = (_file(path, row['sha256'], f'accepted pkgsrc tool {role}')
                       if inspect_inputs else path)
    for relative, role in (('bin/gmake', 'gmake'), ('bin/mksh', 'sh')):
        row = inventory.get(relative)
        _require(isinstance(row, dict) and row.get('kind') == 'file' and
                 row.get('sha256') == rows[output_names[role]]['sha256'] and
                 row.get('mode') == rows[output_names[role]]['mode'],
                 f'pkgsrc output inventory differs from selected tool {role}')
    pkgsrc_mk = os.path.join(prefix, 'share/mk')
    _require(any(path.startswith('share/mk/') for path in inventory),
             'accepted pkgsrc bootstrap prefix omits its share/mk files')
    if inspect_inputs:
        _require(canonical_existing(pkgsrc_mk) == pkgsrc_mk and os.path.isdir(pkgsrc_mk),
                 'accepted pkgsrc share/mk tree changed or is unavailable')
    return PkgsrcBootstrapTools(acceptance_sha256, _freeze(plan_ref), _freeze(result_ref),
                                _freeze(inventory_ref), _freeze(tools), pkgsrc_mk)


def load_netbsd_gcc16_compiler_seed(seed_path, *, seed_sha256):
    """Select GCC and G++ from the independently accepted bmake-scoped seed.

    This proves current GCC16 identities for the one source-build route. It
    does not extend the seed to an ordinary provider or compile a test object.
    """
    from .bootstrap_bmake_seed import validate_compiler_seed

    seed_path = _absolute(seed_path)
    seed_sha256 = _sha(seed_sha256)
    _require(sha256_file(seed_path) == seed_sha256, 'compiler seed descriptor changed')
    seed = _json(dict(path=seed_path, sha256=seed_sha256))
    validate_compiler_seed(seed, inspect_inputs=True)
    prefix = _absolute(seed['prefix'])
    manifest = _json(seed['prefix_manifest'])
    cxx_path = os.path.join(prefix, 'bin', 'g++')
    cxx_row = manifest.get('bin/g++')
    _require(isinstance(cxx_row, dict) and set(cxx_row) == {'sha256', 'size'} and
             type(cxx_row['size']) is int and cxx_row['size'] > 0,
             'accepted GCC16 seed does not inventory its native g++ driver')
    cxx_path = _file(cxx_path, cxx_row['sha256'], 'accepted GCC16 C++ driver')
    compiler_path = canonical_existing(seed['compiler']['path'])
    assembler = canonical_existing(seed['driver_components']['as']['path'])
    linker = canonical_existing(seed['driver_components']['ld']['path'])
    for executable, option, expected in (
            (compiler_path, '-dumpfullversion', seed['compiler']['version']),
            (compiler_path, '-dumpmachine', seed['compiler']['triplet']),
            (cxx_path, '-dumpfullversion', seed['compiler']['version']),
            (cxx_path, '-dumpmachine', seed['compiler']['triplet']),
            (cxx_path, '-print-prog-name=as', seed['driver_components']['as']['path']),
            (cxx_path, '-print-prog-name=ld', seed['driver_components']['ld']['path'])):
        result = capture([executable, option],
                         env={'PATH': os.path.dirname(executable), 'LC_ALL': 'C', 'TZ': 'UTC'})
        observed = result.stdout.decode('utf-8', 'strict').strip()
        _require(result.returncode == 0 and observed == expected,
                 f'accepted GCC16 seed identity differs for {executable} {option}')
    return NetBSDGCC16CompilerSeed(
        seed_sha256, prefix, compiler_path, cxx_path, assembler, linker,
        seed['compiler']['version'], seed['compiler']['triplet'],
        _json(seed['accepted_record'])['source_plan'])
