# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Developed with AI assistance. See docs/bootstrap-relocation.md for sources.
"""One initial utility transformation. No executor or ordinary admission."""

from contextlib import ExitStack
import copy
import hashlib
import os
import struct

from .darwin_runtime import _File, _cstring, _macho, _reference
from .native_toolset import _absolute, _envelope, _json, _require, _source
from .paths import canonical_existing, canonical_new, inside, reject_overlap

BOOTSTRAP_RELOCATION_API = 1
PROFILE_RELATIVE = 'profiles/common-build-tools/bootstrap/darwin-installed-utilities-v1.json'
PROFILE_SHA256 = 'bae4b2d60e37de12b86d23ef4e5333f03f5479c1766fc471704361876c8f211b'
PRINTF_PROFILE_RELATIVE = 'profiles/common-build-tools/bootstrap/darwin-installed-printf-v1.json'
PRINTF_PROFILE_SHA256 = '48927c02453841feaafaaba0c617e3c702821a39cd572d0a1a84822fdca281b7'
_LIMIT = 16 * 1024 * 1024
_RUNS = ('run-a', 'run-b')
# LLVM can relayout segments and LINKEDIT offsets. Preserve all other raw
# commands, including LC_MAIN and the selected dynamic loader.
_EDITED_COMMANDS = {0x19, 0x1d, 0xc, 0x80000018, 0x8000001f, 0x20,
                    0x80000023, 0xd, 0x8000001c, 2, 0xb, 0x22, 0x80000022,
                    0x1e, 0x26, 0x29, 0x80000033, 0x80000034}


def _profile(reference):
    reference = _reference(reference)
    _require(reference['sha256'] in (PROFILE_SHA256, PRINTF_PROFILE_SHA256),
             'unsupported utility relocation profile')
    return _json(reference)


def _members(profile):
    return [*profile['executables'], dict(profile['library'], role='libintl', requires_libintl_change=False)]


def _inputs(plan, *, inspect_inputs=False):
    data = plan['relocation']
    _require(set(data) == {'profile', 'original_prefix', 'final_prefix', 'originals'},
             'unsupported utility relocation fields')
    profile = _profile(data['profile'])
    original = _absolute(data['original_prefix'])
    final = _absolute(data['final_prefix'])
    work = _absolute(plan['work_root'])
    _require(os.path.basename(final) == 'native-utilities-v1', 'unsupported initial utility prefix')
    for left, right in ((original, work), (original, final), (final, work)):
        _require(os.path.commonpath((left, right)) not in (left, right), 'overlapping relocation paths')
        if inspect_inputs:
            reject_overlap(left, right)
    originals = _json(data['originals'])
    members = _members(profile)
    _require(set(originals) == {row['original_member'] for row in members},
             f'relocation needs the exact {len(members)} original members')
    for row in members:
        reference = _reference(originals[row['original_member']])
        _require(reference['path'] == os.path.join(original, row['original_member']),
                 'relocation original differs from its fixed member')
        if inspect_inputs:
            _require(canonical_existing(reference['path']) == reference['path'],
                     'relocation original must have its real regular spelling')
    if inspect_inputs:
        _require(canonical_existing(original) == original and canonical_new(final) == final,
                 'relocation prefix has aliased ancestors')
        for row in profile['executables']:
            for link in row['original_chain']:
                path = os.path.join(original, link['path'])
                _require(os.path.islink(path) and os.readlink(path) ==
                         os.path.join(original, link['target_relative']),
                         'original utility alias differs from its source-owned chain')
    return profile, originals


def relocation_steps(plan, descriptor, commands, flags=None):
    """Return only the fixed LLVM argv arrays. Do not execute a tool."""
    profile, originals = _inputs(plan)
    tool = commands['install_name_tool']
    _require(len(tool) == 1 and os.path.basename(tool[0]) == profile['tool']['invocation'] and
             descriptor['tools']['install_name_tool']['sha256'] == profile['tool']['payload_sha256'],
             'relocation needs the admitted LLVM argv0 alias and exact payload')
    work, data = plan['work_root'], plan['relocation']
    old = os.path.join(data['original_prefix'], 'lib/libintl.8.dylib')
    new = os.path.join(data['final_prefix'], 'lib/libintl.8.dylib')
    result = []
    for run in _RUNS:
        for index, row in enumerate(_members(profile)):
            output = os.path.join(work, run, row['final_regular'])
            argv = [tool[0], '--output', output, '-delete_all_rpaths']
            if row['requires_libintl_change']:
                argv += ['-change', old, new]
            if row['role'] == 'libintl':
                argv += ['-id', new]
            argv.append(originals[row['original_member']]['path'])
            result.append(dict(id=f'{run}-{index:02d}-{row["role"]}', phase='transform',
                cwd=work, argv=argv, env=dict(PATH='', LC_ALL='C', TZ='UTC', TMPDIR=work), expected_exit=0))
    return result


def _plan_header(plan, descriptor):
    _envelope(plan, 'emberbsd-native-gcc16-producer-plan', scope='gcc16-bootstrap')
    _require(set(plan) == {'format', 'version', 'scope', 'stage', 'seed_descriptor', 'host', 'sources',
                          'work_root', 'required_roles', 'resources', 'relocation', 'commands'} and
             plan['stage'] == 'utility-relocation' and plan['host'] == descriptor['host'] and
             plan['host']['os'] == 'Darwin' and plan['host']['cpu'] in ('arm64', 'aarch64') and
             plan['sources'] == descriptor['sources'] and plan['required_roles'] == ['install_name_tool'],
             'unsupported initial utility relocation plan')
    for source in plan['sources'].values():
        _source(source)
    from .bootstrap_pilot import _validate_resources
    _validate_resources(plan['resources'])


def validate_relocation_plan(plan, descriptor, commands, flags=None):
    _plan_header(plan, descriptor)
    _inputs(plan, inspect_inputs=True)
    _require(plan['commands'] == relocation_steps(plan, descriptor, commands, flags),
             'relocation command differs from the fixed source-owned steps')
    # A successful LLVM -change does not prove that the old name was present.
    profile, originals = _inputs(plan)
    for row in _members(profile):
        image = _inspect(originals[row['original_member']], plan['host'], library=row['role'] == 'libintl')
        _original_loads(image, row, plan)
    return plan


def prepare_relocation_work(plan):
    """Create only fixed empty output directories under an already leased work."""
    for run in _RUNS:
        root = os.path.join(plan['work_root'], run)
        os.mkdir(root)
        for name in ('bin', 'sbin', 'lib'):
            os.mkdir(os.path.join(root, name))


def relocation_read_roots(plan):
    return (_absolute(plan['relocation']['original_prefix']),)


def relocation_work_outputs(plan):
    profile, _ = _inputs(plan)
    return tuple(os.path.join(run, row['final_regular']) for run in _RUNS for row in _members(profile))


def _signature(data, offset, size, image, basename):
    _require(offset % 16 == 0 and size % 16 == 0 and offset + size == len(data) and size >= 112,
             'unsupported generated code signature extent')
    blob = data[offset:]
    _require(struct.unpack_from('>5I', blob) == (0xfade0cc0, size, 1, 0, 24) and not any(blob[20:24]),
             'expected one generated CodeDirectory')
    directory = blob[24:]
    magic, length, version, flags, hashes, identity, special, count, limit = struct.unpack_from('>9I', directory)
    hash_size, hash_type, platform, page = struct.unpack_from('>4B', directory, 36)
    _require((magic, length, version, flags, identity, special, limit, hash_size, hash_type, platform, page) ==
             (0xfade0c02, size - 24, 0x20400, 0x20002, 88, 0, offset, 32, 2, 0, 12),
             'unsupported generated CodeDirectory fields')
    _require(count == (offset + 4095) // 4096 and 88 <= hashes <= len(directory) and
             count * 32 <= len(directory) - hashes, 'generated CodeDirectory hash extent differs')
    _require(hashes == ((112 + len(basename.encode('utf-8')) + 1 + 15) // 16 * 16) - 24,
             'generated CodeDirectory layout differs from the fixed LLVM writer')
    name, end = _cstring(directory, identity, hashes)
    _require(name == basename and not any(directory[end:hashes]), 'generated signature identifier differs')
    _require(not any(directory[40:64]), 'unsupported generated CodeDirectory extension')
    texts = [row for row in image['segments'] if row['name'] == '__TEXT']
    _require(len(texts) == 1, 'generated signature needs exactly one __TEXT segment')
    text = texts[0]
    _require(struct.unpack_from('>3Q', directory, 64) ==
             (text['file_offset'], text['file_size'], 1 if image['filetype'] == 2 else 0),
             'generated signature execution segment differs')
    for index in range(count):
        digest = hashlib.sha256(data[index * 4096:min(offset, (index + 1) * 4096)]).digest()
        _require(directory[hashes + index * 32:hashes + (index + 1) * 32] == digest,
                 'generated signature page hash differs')
    _require(not any(directory[hashes + count * 32:]), 'unsupported generated signature trailing data')


def _inspect(reference, host, *, library=False, generated=False):
    reference = _reference(reference)
    with ExitStack() as stack:
        file = _File(reference['path'], stack)
        _require(file.size <= _LIMIT, 'initial utility exceeds the supported file size')
        data = file.read(0, file.size)
        _require(hashlib.sha256(data).hexdigest() == reference['sha256'], 'initial utility bytes changed')
        image = _macho(file.read, host, executable=not library)
        _require(image['cpu'] == 0x100000c and image['subtype'] == 0,
                 'initial relocation supports only generic arm64')
        sections, signatures, stable_commands, segments, dylib_metadata = [], [], [], [], []
        for segment in image['segments']:
            _require(segment['file_offset'] <= len(data) and
                     segment['file_size'] <= len(data) - segment['file_offset'],
                     'initial utility segment exceeds its file')
        position = 32
        for _ in range(struct.unpack_from('<I', data, 16)[0]):
            command, length = struct.unpack_from('<II', data, position)
            if command == 0x19:
                count = struct.unpack_from('<I', data, position + 64)[0]
                _require(len(sections) + count <= 4096, 'initial utility has too many sections')
                vm, vm_size, file_offset, file_size = struct.unpack_from('<4Q', data, position + 24)
                name = data[position + 8:position + 24]
                maximum, initial, _, flags = struct.unpack_from('<4I', data, position + 56)
                segments.append(dict(name=name.hex(), address=vm,
                    vm_size=None if name.rstrip(b'\0') == b'__LINKEDIT' else vm_size,
                    maximum_protection=maximum, initial_protection=initial, flags=flags))
                for number in range(count):
                    fields = struct.unpack_from('<16s16s2Q8I', data, position + 72 + number * 80)
                    name, segment, address, size, offset, align, relocation, relocations, flags, r1, r2, r3 = fields
                    _require(relocations == 0 and relocation == 0, 'initial utility section relocations unsupported')
                    zero = flags & 0xff in (1, 12, 18)
                    _require(vm <= address and size <= vm_size - (address - vm),
                             'initial utility section exceeds its virtual segment')
                    _require(zero or file_offset <= offset and size <= file_size - (offset - file_offset),
                             'initial utility section exceeds its file')
                    sections.append(dict(name=name.hex(), segment=segment.hex(), address=address, size=size,
                        align=align, flags=flags, reserved=[r1, r2, r3],
                        sha256=None if zero else hashlib.sha256(data[offset:offset + size]).hexdigest()))
            elif command == 0x1d:
                _require(length == 16, 'invalid initial utility signature command')
                signatures.append(struct.unpack_from('<2I', data, position + 8))
            if command in (0xc, 0x80000018, 0x8000001f, 0x20, 0x80000023, 0xd):
                # The name and command size may change. Timestamp, current
                # version, and compatibility version must remain exact.
                dylib_metadata.append((command, data[position + 12:position + 24].hex()))
            if command not in _EDITED_COMMANDS:
                stable_commands.append(data[position:position + length].hex())
            position += length
        _require(len(signatures) == 1, 'initial utility needs exactly one existing code signature')
        offset, size = signatures[0]
        header_end = 32 + struct.unpack_from('<I', data, 20)[0]
        _require(header_end <= offset <= len(data) and 0 < size <= len(data) - offset and
                 any(row['name'] == '__LINKEDIT' and row['file_offset'] <= offset and
                     size <= row['file_size'] - (offset - row['file_offset']) for row in image['segments']),
                 'initial utility signature exceeds its file')
        if generated:
            _signature(data, offset, size, image, os.path.basename(reference['path']))
        return dict(image=image, sections=sections, segments=segments, stable_commands=stable_commands,
                    dylib_metadata=dylib_metadata,
                    header_flags=struct.unpack_from('<I', data, 24)[0])


def _original_loads(value, row, plan):
    image = value['image']
    old = os.path.join(plan['relocation']['original_prefix'], 'lib/libintl.8.dylib')
    matching = [load for load in image['loads'] if load['name'] == old]
    _require(len(matching) == int(row['requires_libintl_change']) and
             all(load['command'] == 0xc and load['attributes'] == 0 for load in matching),
             'original libintl dependency differs from the fixed operation')
    if row['role'] == 'libintl':
        _require(image['install_name'] == old, 'original libintl install ID differs')


def _verify_member(plan, row, source, path):
    before = _inspect(source, plan['host'], library=row['role'] == 'libintl')
    _original_loads(before, row, plan)
    expected = copy.deepcopy(before['image'])
    old = os.path.join(plan['relocation']['original_prefix'], 'lib/libintl.8.dylib')
    new = os.path.join(plan['relocation']['final_prefix'], 'lib/libintl.8.dylib')
    for load in expected['loads']:
        if load['name'] == old:
            load['name'] = new
    if row['role'] == 'libintl':
        expected['install_name'] = new
    with ExitStack() as stack:
        file = _File(path, stack)
        output = dict(path=path, sha256=hashlib.sha256(file.read(0, file.size)).hexdigest())
    after = _inspect(output, plan['host'], library=row['role'] == 'libintl', generated=True)
    _require(all(after[key] == before[key] for key in
             ('header_flags', 'sections', 'segments', 'stable_commands', 'dylib_metadata')),
             'relocation changed native flags, program sections, or unsupported commands')
    image = after['image']
    _require(not image['rpaths'], 'relocation left a runtime search path')
    for key in ('cpu', 'subtype', 'filetype', 'uuid', 'dylib_version', 'build_versions',
                'install_name', 'loads', 'implicit_loads', 'lazy_info'):
        _require(image[key] == expected[key], f'relocation changed unsupported image field: {key}')
    return output


def verify_relocation_step(plan, descriptor, commands, step):
    expected = relocation_steps(plan, descriptor, commands)
    _require(step in expected, 'unsupported relocation result step')
    profile, originals = _inputs(plan)
    members = _members(profile)
    row = members[expected.index(step) % len(members)]
    return _verify_member(plan, row, originals[row['original_member']], step['argv'][2])


def verify_relocation_outputs(plan, descriptor=None, commands=None):
    """Check all fresh outputs and original bytes before the runner closes leases."""
    profile, originals = _inputs(plan)
    result = {}
    for row in _members(profile):
        source = originals[row['original_member']]
        output_refs = [_verify_member(plan, row, source, os.path.join(
            plan['work_root'], run, row['final_regular'])) for run in _RUNS]
        _require(output_refs[0]['sha256'] == output_refs[1]['sha256'], 'relocation outputs are not reproducible')
        result[row['original_member']] = dict(original_sha256=source['sha256'],
            sha256=output_refs[0]['sha256'], outputs=output_refs,
            checks=['native-identity', 'exact-load-edit', 'rpaths-absent', 'program-sections',
                    'generated-sha256-signature', 'two-run-exact-equality'])
    return dict(profile=plan['relocation']['profile'], members=result)


def _derivation(reference, file):
    """Check retained operation evidence without reopening retired input trees."""
    result = _json(reference)
    _envelope(result, 'emberbsd-native-gcc16-producer-result', scope='gcc16-bootstrap')
    _require(result.get('stage') == 'utility-relocation' and
             result.get('status') == 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW' and
             result.get('all_commands_drained') is True and
             result.get('inputs_revalidated_after_drain') is True and
             result.get('ordinary_provider_qualified') is False,
             'missing completed scoped utility derivation')
    plan = _json(result['bootstrap_plan'])
    _require(plan['stage'] == 'utility-relocation' and plan['seed_descriptor'] == result['seed_descriptor'],
             'utility derivation selects another plan or seed')
    profile, originals = _inputs(plan)
    seed = _json(plan['seed_descriptor'])
    _plan_header(plan, seed)
    tool = seed['tools']['install_name_tool']
    history = seed['history'][tool['provider']]
    _require(history['kind'] == 'binary-release' and
             history['artifact']['sha256'] == profile['tool']['archive_sha256'] and
             history['source_commit']['commit'] == profile['tool']['source_commit'],
             'utility derivation tool lacks the fixed official LLVM archive and source')
    accepted = _json(result['accepted_seed_receipt'])
    _require(accepted.get('status') == 'PASS' and accepted.get('accepted_input_scope') == 'bootstrap-seed' and
             accepted.get('descriptor_sha256') == plan['seed_descriptor']['sha256'] and
             accepted.get('bootstrap_plan') == result['bootstrap_plan'],
             'utility derivation did not use its exact independently accepted input plan')
    expected = relocation_steps(plan, seed, dict(install_name_tool=(tool['path'],)))
    _require(len(result['commands']) == len(expected), 'utility derivation omitted a command')
    for actual, step in zip(result['commands'], expected):
        _require(all(actual.get(key) == value for key, value in step.items()) and
                 type(actual.get('actual_exit')) is int and actual['actual_exit'] == 0 and
                 'error' not in actual, 'utility derivation command differs or did not succeed')
        file(actual['log'])
    derived = result['relocation']
    _require(set(derived) == {'profile', 'members'} and derived['profile'] == plan['relocation']['profile'] and
             set(derived['members']) == set(originals), 'utility derivation member scope differs')
    checks = ['native-identity', 'exact-load-edit', 'rpaths-absent', 'program-sections',
              'generated-sha256-signature', 'two-run-exact-equality']
    for row in _members(profile):
        member = derived['members'][row['original_member']]
        _require(set(member) == {'original_sha256', 'sha256', 'outputs', 'checks'} and
                 member['original_sha256'] == originals[row['original_member']]['sha256'] and
                 member['checks'] == checks and isinstance(member['outputs'], list) and len(member['outputs']) == 2,
                 'utility derivation original or postconditions differ')
        for run, output in zip(_RUNS, member['outputs']):
            output = _reference(output)
            _require(output['sha256'] == member['sha256'] and output['path'] ==
                     os.path.join(plan['work_root'], run, row['final_regular']),
                     'utility derivation output is not the fixed reproducible member')
    return plan, profile, originals, derived


def validate_derived_installed_history(value, name, descriptor, file, *, inspect_inputs, origins):
    """Keep original installed identity separate from transformed current bytes."""
    _require(set(value) == {'kind', 'original_history', 'derivation', 'provisioned_members'} and
             value['kind'] == 'installed-package-derived', 'unsupported derived installed history')
    plan, profile, originals, derived = _derivation(value['derivation'], file)
    original = value['original_history']
    _require(original['kind'] == 'installed-package', 'derived history requires unchanged installed provenance')
    origins(original['source_archives'])
    origins(original['patches'])
    rows = [row for row in _members(profile) if row['pkgpath'] == original['package_path']]
    _require(rows and original['package_name'] == rows[0]['package'] and
             original['installation_prefix'] == plan['relocation']['original_prefix'],
             'derived package differs from the fixed profile or original prefix')
    # This descriptor is a provenance-only view. inspect_inputs=False is
    # deliberate: retained records must not reopen the retired installation.
    old_tools, expected_original, expected_current = {}, [], []
    for row in rows:
        relative = row['original_member']
        source = originals[relative]
        selected = os.path.join(original['installation_prefix'], row.get('original_invocation', relative))
        expected_original.append(dict(installed_relative=relative, selected_path=selected, sha256=source['sha256']))
        if row['role'] != 'libintl':
            old_tools[row['role']] = dict(provider=name, path=selected, sha256=source['sha256'])
        target = os.path.join(plan['relocation']['final_prefix'], row.get('selected_invocation', row['final_regular']))
        expected_current.append(dict(installed_relative=relative, selected_path=target,
            original_sha256=source['sha256'], sha256=derived['members'][relative]['sha256']))
        _require(inside(target, descriptor['bundle_root']) if inspect_inputs else
                 os.path.commonpath((target, descriptor['bundle_root'])) == descriptor['bundle_root'],
                 'derived installed member escapes its provisioned bundle')
        if inspect_inputs:
            regular = os.path.join(plan['relocation']['final_prefix'], row['final_regular'])
            _require(canonical_existing(target) == regular, 'derived invocation differs from its fixed regular member')
            _inspect(dict(path=regular, sha256=derived['members'][relative]['sha256']),
                     descriptor['host'], library=row['role'] == 'libintl', generated=True)
            for alias in row.get('aliases', []):
                alias_path = os.path.join(plan['relocation']['final_prefix'], alias['path'])
                _require(os.path.islink(alias_path) and os.readlink(alias_path) == alias['target'],
                         'derived alias differs from the source-owned relative route')
    _require(original['provisioned_members'] == expected_original and
             value['provisioned_members'] == expected_current, 'derived installed mapping differs from its original or output')
    selected_roles = {role: row for role, row in descriptor['tools'].items() if row['provider'] == name}
    mapped = {row['selected_path']: row['sha256'] for row in expected_current}
    _require(set(selected_roles) <= set(old_tools) and
             all(mapped.get(row['path']) == row['sha256'] for row in selected_roles.values()),
             'derived history omits or changes a selected provider member')
    from .bootstrap_seed import _installed_history
    # The caller cannot supply a package map. Only the recognized profile can
    # add the data-only gettext member to this provenance-only check.
    packages = {rows[0]['pkgpath']: (rows[0]['package'].rsplit('-', 1)[0], set(old_tools))}
    return _installed_history(original, name,
        dict(bundle_root=original['installation_prefix'], tools=old_tools), file,
        inspect_inputs=False, packages=packages)
