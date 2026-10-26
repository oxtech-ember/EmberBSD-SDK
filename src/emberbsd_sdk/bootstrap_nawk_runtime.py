# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Independent runtime evidence for the initial NetBSD nawk provider.

The independent gate checks trace completeness and source membership. This
reader binds that gate's observations. It does not issue acceptance or trace
native children. Historical output and compiler payloads may be retired.
"""

import os
import struct

from .elf import MAX_HEADERS, parse_elf_header, read_elf_dynamic
from .native_toolset import _absolute, _bytes, _envelope, _file, _json, _require, _sha
from .paths import canonical_existing, inside, open_regular, verify_stable

AUDIT_FORMAT = 'emberbsd-netbsd-nawk-bootstrap-runtime-audit'
CHECKS = ['git-source-membership', 'native-compile-link', 'source-semantics',
          'reported-tool-routes', 'netbsd-output-runtime', 'selected-command-shell']
_ELF_FIELDS = {'class', 'endian', 'machine', 'type', 'soname', 'needed', 'rpath', 'runpath', 'relro'}


def _interpreter(path):
    handle, before = open_regular(path)
    with handle:
        header = handle.read(64)
        parsed = parse_elf_header(header, machine=62)
        _require(parsed is not None and parsed['class'] == 64, 'nawk runtime requires ELF64 amd64')
        offset = struct.unpack_from('<Q', header, 32)[0]
        size, count = struct.unpack_from('<HH', header, 54)
        _require(count <= MAX_HEADERS and (count == 0 or (size == 56 and offset >= 64)) and
                 offset <= before.st_size and count * size <= before.st_size - offset,
                 'invalid nawk runtime ELF program-header table')
        handle.seek(offset)
        table = handle.read(size * count)
        result = []
        for index in range(count):
            kind, flags, start, address, physical, length, memory, align = struct.unpack_from('<IIQQQQQQ', table, index * 56)
            if kind != 3:
                continue
            _require(1 < length <= 4096 and start <= before.st_size and length <= before.st_size - start,
                     'invalid nawk runtime ELF interpreter')
            handle.seek(start)
            data = handle.read(length)
            _require(data.endswith(b'\0') and b'\0' not in data[:-1], 'invalid nawk ELF interpreter string')
            result.append(_absolute(data[:-1].decode('utf-8', 'strict')))
        _require(len(result) <= 1, 'multiple nawk runtime ELF interpreters')
    verify_stable(path, before)
    return result[0] if result else None


def runtime_closure(audit, reference):
    return dict(format='emberbsd-netbsd-nawk-bootstrap-runtime-closure', version=1,
        scope='gcc16-bootstrap', runtime_audit=reference,
        roots=[audit['artifact']['path'], audit['selected_shell']['path']],
        nodes={path: dict(sha256=row['sha256'], dependencies=list(row['dependencies'].values()),
                         interpreter=row['interpreter'], interpreter_target=row['interpreter_target']) for path, row in audit['nodes'].items()})


def _verify_graph(nodes, roots):
    _require(isinstance(nodes, dict) and 0 < len(nodes) <= 256 and
             all(reference['path'] in nodes and nodes[reference['path']]['sha256'] == reference['sha256']
                 for reference in roots), 'nawk runtime roots are missing or changed')
    for path, row in nodes.items():
        _absolute(path)
        _require(set(row) == {'sha256', 'elf', 'interpreter', 'interpreter_target', 'dependencies'}, 'unsupported nawk runtime node')
        _sha(row['sha256'])
        elf = row['elf']
        _require(set(elf) == _ELF_FIELDS and elf['class'] == 64 and elf['endian'] == 'little' and
                 elf['machine'] == 62 and type(elf['type']) is int and elf['type'] in (2, 3) and
                 isinstance(elf['needed'], list) and all(isinstance(name, str) and name for name in elf['needed']) and
                 len(elf['needed']) == len(set(elf['needed'])) and
                 isinstance(row['dependencies'], dict) and set(row['dependencies']) == set(elf['needed']) and
                 all(target in nodes for target in row['dependencies'].values()) and
                 ((row['interpreter'] is None and row['interpreter_target'] is None) or
                  (isinstance(row['interpreter'], str) and _absolute(row['interpreter']) == row['interpreter'] and
                   row['interpreter_target'] in nodes)),
                 'nawk runtime ELF dependency closure is incomplete')
    reached, todo = set(), [reference['path'] for reference in roots]
    while todo:
        path = todo.pop()
        if path in reached:
            continue
        reached.add(path)
        row = nodes[path]
        todo.extend(row['dependencies'].values())
        if row['interpreter'] is not None:
            todo.append(row['interpreter_target'])
    _require(reached == set(nodes), 'nawk runtime closure contains unrelated nodes')


def verify_acceptance(accepted, plan, result, image):
    from .bootstrap_nawk import manifest, selected_shell
    audit = _json(accepted['runtime_audit'])
    _envelope(audit, AUDIT_FORMAT, scope='producer-output')
    _require(set(audit) == {'format', 'version', 'scope', 'host', 'artifact', 'selected_shell',
                          'nodes', 'observations'} and audit['host'] == plan['host'] and
             audit['artifact'] == image, 'nawk runtime audit is not bound to this artifact')
    shell = audit['selected_shell']
    _require(isinstance(shell, dict) and set(shell) == {'path', 'sha256'} and
             shell['path'] == selected_shell(plan, inspect_inputs=False)[0],
             'nawk runtime audit changed the compiled-in selected shell')
    _sha(shell['sha256'])
    # Bind the shell hash to its independent producer receipt, not only its path.
    receipt = _json(plan['program']['pkgsrc_tools_acceptance'])
    shell_result = _json(receipt['result'])
    selected_row = next(row for row in shell_result['outputs'] if row['name'] == 'mksh')
    _require(shell == {key: selected_row[key] for key in ('path', 'sha256')},
             'nawk runtime shell differs from accepted pkgsrc output bytes')
    _verify_graph(audit['nodes'], (image, shell))
    inputs = _json(plan['program']['shell_runtime'])['nodes']
    _require(all(audit['nodes'].get(path) == row for path, row in inputs.items()),
             'nawk output shell runtime graph differs from its accepted inputs')
    cases = [row for row in plan['commands'] if row['phase'] == 'program-case']
    observations = audit['observations']
    _require(isinstance(observations, list) and len(observations) == len(cases),
             'nawk runtime audit needs every finite semantic observation')
    data = manifest(plan)
    for step, observed, case in zip(cases, observations, data['runtime_cases']):
        program = os.path.join(plan['work_root'], case['invocation'])
        _require(set(observed) == {'command', 'ktrace', 'decoded_trace', 'executed_paths',
                                  'undeclared_tools', 'unresolved_dsos'} and observed['command'] == step and
                 isinstance(observed['executed_paths'], list) and
                 len(observed['executed_paths']) == len(set(observed['executed_paths'])) and
                 set(observed['executed_paths']) == {program, shell['path']} and
                 observed['undeclared_tools'] == [] and observed['unresolved_dsos'] == [],
                 'nawk semantic trace did not close its exact program and shell routes')
        for key in ('ktrace', 'decoded_trace'):
            _require(bool(_bytes(observed[key])), 'nawk runtime trace evidence is empty')
    _require(_json(accepted['runtime_closure']) == runtime_closure(audit, accepted['runtime_audit']),
             'nawk accepted runtime closure differs from its exact audit')
    return audit


def _revalidate_graph(nodes, root_paths, descriptor):
    roots = [row['root'] for row in descriptor['host_abi']['libraries']]
    for path, node in nodes.items():
        actual = root_paths.get(path, path)
        _require(canonical_existing(actual) == actual, 'nawk runtime path changed or is aliased')
        if path not in root_paths:
            _require(any(inside(path, root) for root in roots), 'nawk runtime DSO is outside the declared host ABI')
        _file(dict(path=actual, sha256=node['sha256']))
        _require(read_elf_dynamic(actual, machine=62) == node['elf'] and _interpreter(actual) == node['interpreter'],
                 'nawk runtime ELF metadata changed')
        if node['interpreter'] is not None:
            _require(canonical_existing(node['interpreter']) == node['interpreter_target'],
                     'nawk runtime loader alias changed its audited canonical target')


def revalidate_runtime(accepted, image, selected_path, descriptor):
    """Check current copied image and its still-required runtime; no old compiler."""
    audit = _json(accepted['runtime_audit'])
    shell = audit['selected_shell']['path']
    _revalidate_graph(audit['nodes'], {image['path']: selected_path, shell: shell}, descriptor)


def validate_shell_runtime(plan, descriptor, *, inspect_inputs=True):
    """Bind the initial external shell's ELF closure before any producer child."""
    from .bootstrap_nawk import selected_shell
    reference = plan['program']['shell_runtime']
    audit = _json(reference)
    _envelope(audit, 'emberbsd-netbsd-nawk-shell-runtime', scope='gcc16-bootstrap')
    _require(set(audit) == {'format', 'version', 'scope', 'host', 'pkgsrc_tools_acceptance',
                          'selected_shell', 'nodes'} and audit['host'] == descriptor['host'] and
             audit['pkgsrc_tools_acceptance'] == plan['program']['pkgsrc_tools_acceptance'],
             'nawk input shell runtime does not bind its accepted provider and host')
    shell = audit['selected_shell']
    _require(isinstance(shell, dict) and set(shell) == {'path', 'sha256'} and
             shell['path'] == selected_shell(plan, inspect_inputs=False)[0],
             'nawk input runtime changed the selected shell')
    receipt = _json(plan['program']['pkgsrc_tools_acceptance'])
    result = _json(receipt['result'])
    row = next(row for row in result['outputs'] if row['name'] == 'mksh')
    _require(shell == {key: row[key] for key in ('path', 'sha256')},
             'nawk input shell runtime differs from its accepted executable bytes')
    _verify_graph(audit['nodes'], (shell,))
    if inspect_inputs:
        _revalidate_graph(audit['nodes'], {shell['path']: shell['path']}, descriptor)
    return audit


def validate_shell_selection(descriptor, commands):
    """A later seed cannot route sh away from an accepted nawk's compiled path."""
    from .bootstrap_nawk import MANIFEST_RELATIVE, selected_shell
    for provider, row in descriptor['providers'].items():
        if row.get('origin') != 'source-build' or row.get('recipe') != MANIFEST_RELATIVE:
            continue
        record = _json(descriptor['history'][provider]['program_build'])
        original = _json(record['bootstrap_plan'])
        _require(tuple(commands.get('sh', ())) == selected_shell(original, inspect_inputs=False),
                 'selected shell differs from the source-built nawk command shell')
