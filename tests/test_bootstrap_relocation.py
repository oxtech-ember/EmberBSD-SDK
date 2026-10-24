# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Developed with AI assistance. Synthetic bytes do not qualify native tools.

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk import bootstrap_relocation as R
from emberbsd_sdk.errors import SDKError
from test_darwin_runtime import command


def signed_image(name, loads, *, install=None, rpaths=(), payload=b'original-program-section', cpu=0x100000c,
                 entry=2048):
    """Build a small independent Mach-O/LLVM-signature fixture, without tools."""
    base, start, section_offset = 0x100000000, 4096, 2048
    headers = (112 + len(name.encode()) + 1 + 15) // 16 * 16
    signature_size = headers + 32
    commands = [struct.pack('<II16s4Q4I', 0x19, 152, b'__TEXT', base, 4096, 0, 4096, 5, 5, 1, 0) +
        struct.pack('<16s16s2Q8I', b'__text', b'__TEXT', base + section_offset, len(payload),
                    section_offset, 2, 0, 0, 0, 0, 0, 0),
        struct.pack('<II16s4Q4I', 0x19, 72, b'__LINKEDIT', base + 4096, 4096, start, signature_size, 1, 1, 0, 0),
        struct.pack('<II16s', 0x1b, 24, b'u' * 16), struct.pack('<6I', 0x32, 24, 1, 0x1b0000, 0x1b0000, 0)]
    if install is not None:
        commands.append(command(0xd, install))
    else:
        commands.append(struct.pack('<2I2Q', 0x80000028, 24, entry, 0))
    commands.extend(command(0xc, path) for path in loads)
    for path in rpaths:
        text = path.encode() + b'\0'
        size = (12 + len(text) + 7) // 8 * 8
        commands.append(struct.pack('<3I', 0x8000001c, size, 12) + text.ljust(size - 12, b'\0'))
    commands.append(struct.pack('<4I', 0x1d, 16, start, signature_size))
    body = b''.join(commands)
    data = bytearray(struct.pack('<8I', 0xfeedfacf, cpu, 0, 6 if install else 2,
        len(commands), len(body), 0, 0) + body)
    assert len(data) < section_offset
    data.extend(b'\0' * (start - len(data)))
    data[section_offset:section_offset + len(payload)] = payload
    signature = bytearray(signature_size)
    struct.pack_into('>5I', signature, 0, 0xfade0cc0, signature_size, 1, 0, 24)
    struct.pack_into('>9I4B4I4Q', signature, 24, 0xfade0c02, signature_size - 24,
        0x20400, 0x20002, headers - 24, 88, 0, 1, start, 32, 2, 0, 12,
        0, 0, 0, 0, 0, 0, 4096, 1 if install is None else 0)
    signature[112:112 + len(name.encode()) + 1] = name.encode() + b'\0'
    signature[headers:headers + 32] = hashlib.sha256(data).digest()
    return bytes(data + signature)


class Fixture:
    def __init__(self, test):
        temporary = tempfile.TemporaryDirectory(prefix='relocation-fixture-')
        test.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.original = self.root / 'original'
        self.final = self.root / 'bundle/native-utilities-v1'
        self.work = self.root / 'work'
        self.original.mkdir()
        self.final.parent.mkdir()
        self.profile = dict(tool=dict(invocation='llvm-install-name-tool', payload_sha256='e' * 64,
            archive_sha256='a' * 64, source_commit='1' * 40), library=dict(role='libintl',
            package='gettext-lib-1.0nb1', pkgpath='devel/gettext-lib', original_member='lib/libintl.8.dylib',
            final_regular='lib/libintl.8.dylib'), executables=[])
        for index in range(20):
            self.profile['executables'].append(dict(role='u' + str(index), package='coreutils-9.11',
                pkgpath='sysutils/coreutils', original_member=f'bin/u{index}', original_invocation=f'bin/u{index}',
                original_chain=[], final_regular=f'bin/u{index}', selected_invocation=f'bin/u{index}',
                aliases=[], requires_libintl_change=index < 17))
        self.profile_ref = self.document(self.profile)
        guard = patch.object(R, 'PROFILE_SHA256', self.profile_ref['sha256'])
        guard.start()
        test.addCleanup(guard.stop)
        self.members = R._members(self.profile)
        self.originals = {}
        for row in self.members:
            path = self.original / row['original_member']
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(self.image(row, transformed=False))
            self.originals[row['original_member']] = self.reference(path)
        sources = dict(sdk=dict(repository='https://example.invalid/sdk', commit='2' * 40),
                       ports=dict(repository='https://example.invalid/ports', commit='3' * 40))
        self.descriptor = dict(host=dict(os='Darwin', cpu='arm64'), sources=sources,
            tools=dict(install_name_tool=dict(path=str(self.final.parent / 'llvm-install-name-tool'),
                sha256='e' * 64, provider='llvm')), history=dict(llvm=dict(kind='binary-release',
                    artifact=dict(path='/retained/archive', sha256='a' * 64), source_commit=dict(commit='1' * 40))))
        self.descriptor_ref = self.document(self.descriptor)
        self.commands = dict(install_name_tool=(self.descriptor['tools']['install_name_tool']['path'],))
        self.plan = dict(format='emberbsd-native-gcc16-producer-plan', version=1, scope='gcc16-bootstrap',
            stage='utility-relocation', seed_descriptor=self.descriptor_ref, host=self.descriptor['host'],
            sources=sources, work_root=str(self.work), required_roles=['install_name_tool'],
            resources=dict(cpu_jobs=1, link_jobs=1, runtime_seconds=120, lock_root=str(self.root),
                heavy_link_context=str(self.root / 'heavy-link')),
            relocation=dict(profile=self.profile_ref, original_prefix=str(self.original),
                final_prefix=str(self.final), originals=self.document(self.originals)))
        self.plan['commands'] = R.relocation_steps(self.plan, self.descriptor, self.commands)

    def reference(self, path):
        return dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())

    def document(self, value):
        path = self.root / ('document-' + str(len(list(self.root.glob('document-*')))) + '.json')
        path.write_text(json.dumps(value, sort_keys=True) + '\n')
        return self.reference(path)

    def image(self, row, *, transformed, **kwargs):
        prefix = self.final if transformed else self.original
        library = str(prefix / 'lib/libintl.8.dylib')
        loads = ['/usr/lib/libSystem.B.dylib']
        if row['requires_libintl_change']:
            loads.insert(0, library)
        return signed_image(Path(row['final_regular']).name, loads,
            install=library if row['role'] == 'libintl' else None,
            rpaths=() if transformed else (str(self.original / 'lib'),), **kwargs)

    def outputs(self):
        self.work.mkdir()
        R.prepare_relocation_work(self.plan)
        for run in ('run-a', 'run-b'):
            for row in self.members:
                (self.work / run / row['final_regular']).write_bytes(self.image(row, transformed=True))

    def result(self):
        self.outputs()
        plan = self.document(self.plan)
        acceptance = self.document(dict(status='PASS', accepted_input_scope='bootstrap-seed',
            descriptor_sha256=self.descriptor_ref['sha256'], bootstrap_plan=plan))
        log = self.root / 'empty.log'
        log.write_bytes(b'')
        result = dict(format='emberbsd-native-gcc16-producer-result', version=1, scope='gcc16-bootstrap',
            stage='utility-relocation', status='COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW',
            all_commands_drained=True, inputs_revalidated_after_drain=True, ordinary_provider_qualified=False,
            bootstrap_plan=plan, seed_descriptor=self.descriptor_ref, accepted_seed_receipt=acceptance,
            commands=[dict(step, actual_exit=0, log=self.reference(log)) for step in self.plan['commands']],
            relocation=R.verify_relocation_outputs(self.plan))
        return result


class RelocationTests(unittest.TestCase):
    def check_history(self, value, name, descriptor, *, inspect_inputs):
        from emberbsd_sdk.bootstrap_seed import _history
        return _history({name: value}, {name: dict(origin='installed-package-derived')},
                        descriptor=descriptor, inspect_inputs=inspect_inputs)[name]

    def test_exact_steps_and_complete_repeatable_postchecks_without_tool_execution(self):
        f = Fixture(self)
        with patch('subprocess.Popen', side_effect=AssertionError('no native process')):
            R.validate_relocation_plan(f.plan, f.descriptor, f.commands)
            self.assertEqual(len(f.plan['commands']), 42)
            self.assertEqual(sum('-change' in s['argv'] for s in f.plan['commands']), 34)
            self.assertEqual(sum('-id' in s['argv'] for s in f.plan['commands']), 2)
            f.outputs()
            result = R.verify_relocation_outputs(f.plan)
            self.assertEqual(len(result['members']), 21)
            self.assertTrue(all(m['outputs'][0]['sha256'] == m['outputs'][1]['sha256']
                                for m in result['members'].values()))

    def test_fixed_profile_path_tool_and_command_refusals(self):
        for case in ('profile', 'path', 'tool', 'flag', 'tail', 'overlap', 'role'):
            with self.subTest(case=case):
                f = Fixture(self)
                if case == 'profile': f.plan['relocation']['profile']['sha256'] = '0' * 64
                elif case == 'path': f.plan['relocation']['final_prefix'] = str(f.root / 'arbitrary-prefix')
                elif case == 'tool': f.commands['install_name_tool'] = ('/usr/bin/install_name_tool',)
                elif case == 'flag': f.plan['commands'][0]['argv'].insert(1, '-add_rpath')
                elif case == 'tail': f.commands['install_name_tool'] += ('--extra',)
                elif case == 'overlap': f.plan['relocation']['original_prefix'] = str(f.work)
                else: f.plan['required_roles'] += ['sh']
                with self.assertRaises(SDKError): R.validate_relocation_plan(f.plan, f.descriptor, f.commands)

    def test_original_sha_and_missing_old_load_refuse_before_tool(self):
        for case in ('sha', 'load'):
            with self.subTest(case=case):
                f = Fixture(self)
                row = f.members[0]
                path = f.original / row['original_member']
                path.write_bytes(f.image(row, transformed=True))
                if case == 'load':
                    f.originals[row['original_member']] = f.reference(path)
                    f.plan['relocation']['originals'] = f.document(f.originals)
                with self.assertRaises(SDKError): R.validate_relocation_plan(f.plan, f.descriptor, f.commands)

    def test_changed_sections_cpu_loads_rpaths_signature_and_repeatability_refuse(self):
        for case in ('sections', 'cpu', 'entrypoint', 'rpath', 'load', 'signature', 'different-runs'):
            with self.subTest(case=case):
                f = Fixture(self)
                f.outputs()
                row = f.members[0]
                path = f.work / 'run-a' / row['final_regular']
                if case == 'sections': path.write_bytes(f.image(row, transformed=True, payload=b'changed-section'))
                elif case == 'cpu': path.write_bytes(f.image(row, transformed=True, cpu=0x1000007))
                elif case == 'entrypoint': path.write_bytes(f.image(row, transformed=True, entry=3072))
                elif case in ('rpath', 'load'):
                    prefix = f.final if case == 'rpath' else f.original
                    path.write_bytes(signed_image(path.name,
                        [str(prefix / 'lib/libintl.8.dylib'), '/usr/lib/libSystem.B.dylib'],
                        rpaths=(str(f.original / 'lib'),) if case == 'rpath' else ()))
                elif case == 'signature':
                    data = bytearray(path.read_bytes()); data[-16] ^= 1; path.write_bytes(data)
                else:
                    # Change only unmodeled padding, then produce a consistent
                    # page hash. Identity and section checks still pass.
                    data = bytearray(path.read_bytes()); data[3072] ^= 1
                    hashes = struct.unpack_from('>I', data, 4096 + 24 + 16)[0]
                    data[4096 + 24 + hashes:4096 + 24 + hashes + 32] = hashlib.sha256(data[:4096]).digest()
                    path.write_bytes(data)
                with self.assertRaises(SDKError): R.verify_relocation_outputs(f.plan)

    def test_signature_layout_page_and_identifier_are_checked_independently(self):
        f = Fixture(self)
        path = f.root / 'test-image'
        path.write_bytes(signed_image(path.name, ['/usr/lib/libSystem.B.dylib']))
        image = R._inspect(f.reference(path), f.plan['host'], generated=True)
        self.assertTrue(image['sections'])
        original = path.read_bytes()
        for offset in (4096 + 16, 4096 + 24 + 12, 4096 + 24 + 20,
                       4096 + 24 + 39, 4096 + 112, len(original) - 1):
            data = bytearray(original); data[offset] ^= 1; path.write_bytes(data)
            with self.subTest(offset=offset):
                with self.assertRaises(SDKError): R._inspect(f.reference(path), f.plan['host'], generated=True)

    def test_dylib_timestamp_and_version_bits_cannot_change_with_valid_signature(self):
        f = Fixture(self)
        f.outputs()
        path = f.work / 'run-a' / f.members[0]['final_regular']
        original = path.read_bytes()
        position = 32
        while struct.unpack_from('<I', original, position)[0] != 0xc:
            position += struct.unpack_from('<I', original, position + 4)[0]
        for field in (12, 16, 20):
            data = bytearray(original)
            data[position + field] ^= 1
            hashes = struct.unpack_from('>I', data, 4096 + 24 + 16)[0]
            data[4096 + 24 + hashes:4096 + 24 + hashes + 32] = hashlib.sha256(data[:4096]).digest()
            # Change both runs identically. Reproducibility must not mask
            # the original-to-transformed metadata preservation check.
            for run in ('run-a', 'run-b'):
                (f.work / run / f.members[0]['final_regular']).write_bytes(data)
            with self.subTest(field=field):
                R._inspect(f.reference(path), f.plan['host'], generated=True)
                with self.assertRaises(SDKError): R.verify_relocation_outputs(f.plan)

    def test_derivation_binds_actual_commands_exits_logs_and_input_acceptance(self):
        f = Fixture(self)
        original = f.result()
        R._derivation(f.document(original), lambda ref: R._reference(ref))
        for case in ('exit', 'command', 'count', 'reproduction', 'scope', 'acceptance'):
            with self.subTest(case=case):
                value = copy.deepcopy(original)
                if case == 'exit': value['commands'][0]['actual_exit'] = 1
                elif case == 'command': value['commands'][0]['argv'] += ['extra']
                elif case == 'count': value['commands'].pop()
                elif case == 'reproduction': next(iter(value['relocation']['members'].values()))['outputs'][1]['sha256'] = '0' * 64
                elif case == 'scope': value['ordinary_provider_qualified'] = True
                else: value['accepted_seed_receipt'] = f.document(dict(status='PASS'))
                with self.assertRaises(SDKError): R._derivation(f.document(value), lambda ref: R._reference(ref))

    def derived_history(self, f, result, *, library=False):
        rows = [row for row in f.members if (row['role'] == 'libintl') == library]
        name = 'derived-gettext' if library else 'derived-utilities'
        originals, current, tools = [], [], {}
        for row in rows:
            relative = row['original_member']
            original = f.originals[relative]
            originals.append(dict(installed_relative=relative,
                selected_path=str(f.original / row.get('original_invocation', relative)), sha256=original['sha256']))
            member = result['relocation']['members'][relative]
            selected = str(f.final / row.get('selected_invocation', row['final_regular']))
            current.append(dict(installed_relative=relative, selected_path=selected,
                original_sha256=original['sha256'], sha256=member['sha256']))
            target = f.final / row['final_regular']
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f.work / 'run-a' / row['final_regular'], target)
            if not library:
                tools[row['role']] = dict(provider=name, path=selected, sha256=member['sha256'])
        first = rows[0]
        info = f.root / (name + '-BUILD_INFO')
        info.write_text('PKGPATH=' + first['pkgpath'] + '\nCC_VERSION=gcc-16\n')
        contents = f.root / (name + '-CONTENTS')
        contents.write_text('@name ' + first['package'] + '\n')
        correlation = f.root / (name + '-correlation')
        correlation.write_text('Synthetic source correlation only.\n')
        history = dict(kind='installed-package', package_name=first['package'], package_path=first['pkgpath'],
            installation_prefix=str(f.original), package_metadata=dict(build_info=f.reference(info),
                contents=f.reference(contents)), installed_member_inventory=f.document({
                    row['installed_relative']: dict(kind='file', sha256=row['sha256']) for row in originals}),
            provisioned_members=originals, source_correlation=f.reference(correlation), source_archives=[],
            patches=[], package_archive=None, source_commit=None, historical_build_log=None,
            historical_compiler='gcc-16', historical_toolchain_closure=None, patches_complete=False,
            unknowns=sorted(['package-archive', 'source-commit', 'historical-build-log',
                'historical-toolchain-closure', 'historical-patches', 'source-archives']))
        derived = dict(kind='installed-package-derived', original_history=history,
            derivation=f.document(result), provisioned_members=current)
        descriptor = dict(bundle_root=str(f.final.parent), host=f.plan['host'], tools=tools)
        return name, derived, descriptor

    def test_derived_original_roster_and_unknowns_stay_distinct_from_current_outputs(self):
        f = Fixture(self)
        result = f.result()
        for library in (False, True):
            name, history, descriptor = self.derived_history(f, result, library=library)
            missing = self.check_history(history, name, descriptor, inspect_inputs=True)
            self.assertEqual(missing, history['original_history']['unknowns'])
            self.assertNotEqual(history['provisioned_members'][0]['sha256'],
                                history['provisioned_members'][0]['original_sha256'])
            for field in ('original_sha256', 'sha256', 'selected_path'):
                changed = copy.deepcopy(history)
                changed['provisioned_members'][0][field] = '/wrong/member' if field == 'selected_path' else '0' * 64
                with self.assertRaises(SDKError):
                    self.check_history(changed, name, descriptor, inspect_inputs=False)

    def test_historical_validation_does_not_reopen_retired_original_tree(self):
        f = Fixture(self)
        result = f.result()
        name, history, descriptor = self.derived_history(f, result)
        shutil.rmtree(f.original)
        with patch('subprocess.Popen', side_effect=AssertionError('no native process')):
            self.assertEqual(self.check_history(history, name, descriptor,
                inspect_inputs=False), history['original_history']['unknowns'])

    def test_original_absolute_and_provisioned_relative_aliases_are_distinct(self):
        f = Fixture(self)
        row = f.profile['executables'][0]
        row.update(original_invocation='bin/invocation', selected_invocation='bin/invocation',
                   original_chain=[dict(path='bin/invocation', target_relative='bin/u0')],
                   aliases=[dict(path='bin/invocation', target='u0')])
        old_alias = f.original / 'bin/invocation'
        old_alias.symlink_to(f.original / 'bin/u0')
        reference = f.document(f.profile)
        f.plan['relocation']['profile'] = reference
        with patch.object(R, 'PROFILE_SHA256', reference['sha256']):
            f.plan['commands'] = R.relocation_steps(f.plan, f.descriptor, f.commands)
            R.validate_relocation_plan(f.plan, f.descriptor, f.commands)
            old_alias.unlink()
            old_alias.symlink_to('u0')
            with self.assertRaises(SDKError):
                R.validate_relocation_plan(f.plan, f.descriptor, f.commands)
            old_alias.unlink()
            old_alias.symlink_to(f.original / 'bin/u0')
            result = f.result()
            name, history, descriptor = self.derived_history(f, result)
            alias = f.final / 'bin/invocation'
            alias.symlink_to('u0')
            self.check_history(history, name, descriptor, inspect_inputs=True)
            alias.unlink()
            alias.symlink_to(f.final / 'bin/u0')
            with self.assertRaises(SDKError):
                self.check_history(history, name, descriptor, inspect_inputs=True)


if __name__ == '__main__':
    unittest.main()
