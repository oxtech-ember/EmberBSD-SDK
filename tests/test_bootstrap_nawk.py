# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Finite nawk bootstrap controls. Test children are data, never compilers."""

import copy
import contextlib
import io
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_bootstrap_seed import SeedFixture
from test_bootstrap_netbsd_gcc16 import PkgsrcToolsFixture
from emberbsd_sdk import bootstrap_nawk as nawk, bootstrap_pilot, bootstrap_program as program
from emberbsd_sdk.bootstrap_producer import validate_producer_plan
from emberbsd_sdk.bootstrap_seed import _history, load_bootstrap_seed
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.elf import read_elf_dynamic
from emberbsd_sdk.inventory import snapshot_tree
from emberbsd_sdk.native_toolset import _json


REAL_MANIFEST_SHA256 = nawk.MANIFEST_SHA256


def elf(kind=2, interpreter=None):
    raw = bytearray(64)
    raw[:7] = b'\x7fELF\x02\x01\x01'
    struct.pack_into('<HHI', raw, 16, kind, 62, 1)
    struct.pack_into('<H', raw, 52, 64)
    if interpreter is not None:
        literal = os.fsencode(interpreter) + b'\0'
        struct.pack_into('<Q', raw, 32, 64)
        struct.pack_into('<HH', raw, 54, 56, 1)
        raw += struct.pack('<IIQQQQQQ', 3, 4, 120, 0, 0, len(literal), len(literal), 1) + literal
    return bytes(raw)


class NawkFixture:
    def __init__(self, case, *, real=False):
        temporary = tempfile.TemporaryDirectory()
        case.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.seed = SeedFixture(self.base)
        self.f, self.descriptor = self.seed.native, self.seed.descriptor
        self.descriptor['host'] = dict(os='NetBSD', cpu='amd64')
        self.descriptor['compiler']['triplet'] = 'x86_64--netbsd'
        self.descriptor['admission'] = 'netbsd11-amd64-first-native-v1'
        # The initial recipe seed has no make or shell roles.
        for role in ('sh', 'make', 'gmake'):
            self.descriptor['tools'].pop(role, None)
        self.shell = PkgsrcToolsFixture(self.base / 'shell-fixture')
        self.shell.mksh.write_bytes(elf())
        self.shell.inventory = snapshot_tree(str(self.shell.prefix))
        self.shell.inventory_path.write_text(json.dumps(self.shell.inventory))
        self.shell.result_value['output_inventory'] = self.shell.ref(self.shell.inventory_path)
        for row in self.shell.result_value['outputs']:
            if row['name'] == 'mksh': row['sha256'] = self.shell.digest(self.shell.mksh)
        self.shell.result.write_text(json.dumps(self.shell.result_value))
        self.shell.acceptance_value.update(result=self.shell.ref(self.shell.result),
                                          output_inventory=self.shell.ref(self.shell.inventory_path))
        self.shell.acceptance.write_text(json.dumps(self.shell.acceptance_value))
        self.source, self.work = self.base / 'source', self.base / 'nawk-work'
        self.source.mkdir()
        if real:
            owner = os.environ.get('EMBERBSD_TEST_PORTS_SOURCE')
            if not owner:
                case.skipTest('select the real Ports owner for the source-data check')
            selected_manifest = Path(os.environ.get('EMBERBSD_TEST_NAWK_MANIFEST', str(Path(owner, nawk.MANIFEST_RELATIVE))))
            self.manifest = json.loads(selected_manifest.read_bytes())
            case.assertEqual(hashlib.sha256(selected_manifest.read_bytes()).hexdigest(), nawk.MANIFEST_SHA256)
            for name in [*nawk.SOURCES, *nawk.HEADERS, *nawk.PROVENANCE, nawk.PROBE]:
                target = self.source / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(Path(owner, name), target)
            (self.source / nawk.MANIFEST_RELATIVE).write_bytes(selected_manifest.read_bytes())
        else:
            for name in [*nawk.SOURCES, *nawk.HEADERS, *nawk.PROVENANCE, nawk.PROBE]:
                target = self.source / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(('synthetic member ' + name + '\n').encode())
            generated = {kind: {name: hashlib.sha256((self.source / nawk.RECIPE / 'files' / name).read_bytes()).hexdigest()
                         for name in names} for kind, names in dict(
                inputs=('awkgram.y', 'awk.h', 'proto.h', 'maketab.c'),
                outputs=('awkgram.tab.c', 'awkgram.tab.h', 'proctab.c')).items()}
            (self.source / nawk.RECIPE / 'files/GENERATED.json').write_text(json.dumps(generated))
            self.manifest = dict(format='emberbsd-native-bootstrap-program', version=1, name='nawk',
                release='nawk-20260426', initial_only=True, compiler=dict(family='gcc', major=16, abi='native'),
                canonical_host_recipe=nawk.RECIPE + '/Makefile', source_makefile=nawk.RECIPE + '/files/Makefile',
                sources=list(nawk.SOURCES), headers=list(nawk.HEADERS), provenance=list(nawk.PROVENANCE), probe=nawk.PROBE,
                include_dirs=[nawk.RECIPE + '/files'], cppflags=['-DYYMAXDEPTH=300'], cflags=[], link_flags=[], libraries=['m'],
                selected_command_shell=dict(role='sh', macro='NAWK_COMMAND_SHELL'), invocations=['nawk', 'awk'], files={},
                runtime_cases=[dict(name='synthetic-' + str(index), invocation='nawk' if index < 7 else 'command-shell-probe',
                    args=['BEGIN { print 1 }'] if index < 7 else [], stdin='', exit=0, stdout='1\n', stderr='') for index in range(8)])
            (self.source / nawk.MANIFEST_RELATIVE).write_text(json.dumps(self.manifest))
            mocked = patch.object(nawk, 'MANIFEST_SHA256', self.shell.digest(self.source / nawk.MANIFEST_RELATIVE))
            mocked.start(); case.addCleanup(mocked.stop)
        for path in self.source.rglob('*'):
            path.chmod(0o555 if path.is_dir() else 0o444)
        self.source.chmod(0o555)
        def writable():
            self.source.chmod(0o755)
            for path in self.source.rglob('*'):
                if path.is_dir(): path.chmod(0o755)
        case.addCleanup(writable)
        inventory = snapshot_tree(str(self.source))
        members = {}
        for name, row in inventory.items():
            if row['kind'] == 'file':
                raw = (self.source / name).read_bytes()
                members[name] = dict(sha256=row['sha256'],git_blob=hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest())
        self.inputs = dict(root=str(self.source), source=self.descriptor['sources']['ports'], inventory=self.f.document(inventory))
        self.inputs['source_proof'] = self.f.document(dict(format='emberbsd-bootstrap-program-source', version=1,
            source=self.inputs['source'], inventory=self.inputs['inventory'], members=self.f.document(members)))
        self.source_ref = self.f.document(self.inputs)
        self.commands = {role: words for role, words in self.seed.commands.items() if role in self.descriptor['tools']}
        self.commands['sh'] = (str(self.shell.mksh),)
        self.flags = self.descriptor['abi_flags']
        self.descriptor_ref = self.f.document(self.descriptor)
        self.pkgsrc_ref = self.shell.ref(self.shell.acceptance)
        shell=self.shell.ref(self.shell.mksh)
        self.shell_runtime=dict(format='emberbsd-netbsd-nawk-shell-runtime',version=1,scope='gcc16-bootstrap',
            host=self.descriptor['host'],pkgsrc_tools_acceptance=self.pkgsrc_ref,selected_shell=shell,
            nodes={shell['path']:dict(sha256=shell['sha256'],elf=read_elf_dynamic(shell['path'],machine=62),
                   interpreter=None,interpreter_target=None,dependencies={})})
        self.shell_runtime_ref=self.f.document(self.shell_runtime)
        self.plan = nawk.make_plan(self.descriptor_ref, self.source_ref, self.pkgsrc_ref, self.shell_runtime_ref,
            work_root=str(self.work), resources=dict(cpu_jobs=1, link_jobs=1, runtime_seconds=120,
            lock_root=str(self.base), heavy_link_context=str(self.base / 'heavy')))

    def validate(self):
        return validate_producer_plan(self.plan, self.descriptor, self.commands, self.flags)

    def fake(self, argv, log, **kwargs):
        step = next(row for row in self.plan['commands'] if row['id'] == Path(log).stem)
        Path(log).write_bytes(b'synthetic combined producer log\n')
        if step['phase'] in ('compile','link'):
            Path(argv[-1]).write_bytes(elf(1 if step['phase'] == 'compile' else 2))
            Path(argv[-1]).chmod(0o755)
        if step['phase'] == 'program-case':
            case = next(row for row in self.manifest['runtime_cases'] if step['id'] == 'case-' + row['name'])
            for stream in ('stdout','stderr'):
                (self.work / (case['name'] + '.' + stream)).write_text(case[stream])
        return subprocess.CompletedProcess(argv, 0)

    def run(self):
        case = self
        class Selection:
            commands = case.commands
            def as_dict(self):
                return dict(plan=case.validate())
        self.plan_ref = self.f.document(self.plan)
        self.accepted_ref = self.f.document(dict(input_route_audit=self.f.raw(b'synthetic acceptance')))
        with patch('emberbsd_sdk.bootstrap_seed.load_bootstrap_seed', side_effect=lambda *a, **kw: Selection()), \
             patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=self.fake):
            return bootstrap_pilot.run_producer(self.descriptor_ref['path'], descriptor_sha256=self.descriptor_ref['sha256'],
                accepted_seed_receipt=self.accepted_ref, bootstrap_plan=self.plan_ref, required_roles=self.plan['required_roles'])


class NawkProducerTests(unittest.TestCase):
    def setUp(self):
        self.c = NawkFixture(self)

    def test_fixed_route_uses_only_compiler_and_accepted_shell(self):
        c = self.c
        with patch('subprocess.Popen', side_effect=AssertionError('no child')):
            self.assertIs(c.validate(), c.plan)
        self.assertNotIn('sh', c.descriptor['tools'])
        self.assertNotIn('make', c.plan['required_roles'])
        self.assertEqual(len(c.plan['commands']), 20)
        compiles = [row for row in c.plan['commands'] if row['phase'] == 'compile']
        self.assertEqual(len(compiles), 10)
        macro = '-DNAWK_COMMAND_SHELL="' + str(c.shell.mksh) + '"'
        self.assertTrue(all(macro in row['argv'] for row in compiles))
        self.assertTrue(all(row['env']['PATH'] == str(c.work / 'no-tools') for row in c.plan['commands']))
        self.assertTrue(all('-Wl,--threads=1' not in row['argv'] for row in c.plan['commands']))
        self.assertTrue(all(row['argv'][0] == str(c.shell.mksh) for row in c.plan['commands'] if row['phase']=='program-case'))

    def test_dispatch_drains_and_keeps_output_unqualified(self):
        result = self.c.run()
        self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW')
        self.assertFalse(result['ordinary_provider_qualified'])
        self.assertTrue(all(result[key] for key in ('all_commands_drained','all_leases_closed','inputs_revalidated_after_drain')))
        self.assertEqual(os.readlink(self.c.work/'awk'), 'nawk')
        self.assertIn('command-shell-probe', result['outputs'])
        self.assertEqual(len(result['commands']),20)

    def test_missing_changed_or_system_shell_receipt_refuses(self):
        c=self.c
        for change in ('path','hash','command'):
            plan, commands = copy.deepcopy(c.plan), dict(c.commands)
            if change=='path': plan['program']['pkgsrc_tools_acceptance']['path'] = str(c.base/'absent')
            elif change=='hash': plan['program']['pkgsrc_tools_acceptance']['sha256'] = '0'*64
            else: commands['sh'] = ('/bin/sh',)
            with self.subTest(change=change), self.assertRaises((SDKError,OSError)):
                validate_producer_plan(plan,c.descriptor,commands,c.flags)

    def test_generic_host_profile_or_additional_role_refuses(self):
        c=self.c
        for change in ('host','profile','stage','roles'):
            plan, desc=copy.deepcopy(c.plan),copy.deepcopy(c.descriptor)
            if change=='host': plan['host']=desc['host']=dict(os='Linux',cpu='x86_64')
            elif change=='profile': plan['netbsd_profile']='ordinary-native'
            elif change=='stage': plan['stage']='arbitrary-bootstrap'
            else: plan['required_roles'].append('awk')
            with self.subTest(change=change),self.assertRaises(SDKError):
                validate_producer_plan(plan,desc,c.commands,c.flags)

    def test_runner_leases_literal_loader_alias_and_every_runtime_node(self):
        c = self.c
        runtime = c.base / 'runtime'
        aliases = c.base / 'aliases'
        runtime.mkdir(); aliases.mkdir()
        loader = runtime / 'loader'
        loader.write_bytes(elf(3))
        alias = aliases / 'interpreter'
        alias.symlink_to(loader)
        sf = c.shell
        sf.mksh.write_bytes(elf(interpreter=alias))
        sf.inventory_path.write_text(json.dumps(snapshot_tree(str(sf.prefix))))
        sf.result_value['output_inventory'] = sf.ref(sf.inventory_path)
        for row in sf.result_value['outputs']:
            if row['name'] == 'mksh':
                row['sha256'] = sf.digest(sf.mksh)
        sf.result.write_text(json.dumps(sf.result_value))
        sf.acceptance_value.update(result=sf.ref(sf.result), output_inventory=sf.ref(sf.inventory_path))
        sf.acceptance.write_text(json.dumps(sf.acceptance_value))
        c.pkgsrc_ref = sf.ref(sf.acceptance)
        shell = sf.ref(sf.mksh)
        c.shell_runtime.update(pkgsrc_tools_acceptance=c.pkgsrc_ref, selected_shell=shell, nodes={
            shell['path']: dict(sha256=shell['sha256'], elf=read_elf_dynamic(sf.mksh, machine=62),
                interpreter=str(alias), interpreter_target=str(loader), dependencies={}),
            str(loader): dict(sha256=sf.digest(loader), elf=read_elf_dynamic(loader, machine=62),
                interpreter=None, interpreter_target=None, dependencies={})})
        c.shell_runtime_ref = c.f.document(c.shell_runtime)
        c.descriptor['host_abi']['libraries'].append(dict(root=str(runtime), inventory=c.f.document(snapshot_tree(str(runtime)))))
        c.descriptor_ref = c.f.document(c.descriptor)
        c.plan = nawk.make_plan(c.descriptor_ref, c.source_ref, c.pkgsrc_ref, c.shell_runtime_ref,
                               work_root=str(c.work), resources=c.plan['resources'])
        calls = []
        original = bootstrap_pilot.ContextLocks
        def capture(paths, lock_root, *, shared=False):
            calls.append((set(paths), shared))
            return original(paths, lock_root, shared=shared)
        with patch.object(bootstrap_pilot, 'ContextLocks', side_effect=capture):
            result = c.run()
        self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW')
        shared = next(paths for paths, mode in calls if mode)
        self.assertTrue({str(alias), str(loader), str(sf.mksh)} <= shared)

    def test_input_shell_runtime_is_required_and_live_before_any_child(self):
        c=self.c
        missing=copy.deepcopy(c.plan);del missing['program']['shell_runtime']
        with patch('subprocess.Popen',side_effect=AssertionError('no child')), self.assertRaises(SDKError):
            validate_producer_plan(missing,c.descriptor,c.commands,c.flags)
        for mutation in ('provider','needed','unrelated','live-elf','machine'):
            audit=copy.deepcopy(c.shell_runtime)
            node=audit['nodes'][str(c.shell.mksh)]
            if mutation=='provider':audit['pkgsrc_tools_acceptance']['sha256']='0'*64
            elif mutation=='needed':node['elf']['needed']=['libmissing.so.1']
            elif mutation=='unrelated':audit['nodes']['/unrelated']=copy.deepcopy(node)
            elif mutation=='live-elf':node['elf']['rpath']='/unrecorded/lib'
            else:node['elf']['machine']=183
            plan=copy.deepcopy(c.plan);plan['program']['shell_runtime']=c.f.document(audit)
            with self.subTest(mutation=mutation), patch('subprocess.Popen',side_effect=AssertionError('no child')), self.assertRaises(SDKError):
                validate_producer_plan(plan,c.descriptor,c.commands,c.flags)

    def test_mutated_command_or_source_refuses(self):
        c=self.c
        for key,value in (('argv',['/bin/sh','-c','true']),('env',dict(PATH='/usr/bin')),('expected_exit',1)):
            plan=copy.deepcopy(c.plan);plan['commands'][0][key]=value
            with self.subTest(key=key),self.assertRaises(SDKError):
                validate_producer_plan(plan,c.descriptor,c.commands,c.flags)
        path=c.source/nawk.SOURCES[0];path.chmod(0o644);path.write_text('changed parser')
        with self.assertRaises(SDKError):c.validate()

    def test_initial_plan_has_no_missing_command_search_directory(self):
        self.c.work.mkdir();(self.c.work/'no-tools').mkdir()
        with self.assertRaisesRegex(SDKError,'PATH must contain no tools'): self.c.validate()

    def test_real_source_manifest_and_generated_provenance(self):
        with patch.object(nawk, 'MANIFEST_SHA256', REAL_MANIFEST_SHA256):
            real=NawkFixture(self,real=True)
            real.validate()
        self.assertEqual(sum(x=='\\' for x in real.manifest['runtime_cases'][4]['args'][0]),2)
        self.assertEqual(sum(x=='\\' for x in real.manifest['runtime_cases'][6]['args'][0]),2)
        self.assertIn('NAWK_COMMAND_SHELL',(real.source/nawk.RECIPE/'files/command-shell.h').read_text())

    def test_seed_loader_adds_only_the_accepted_shell_for_this_exact_plan(self):
        c=self.c;seed=c.seed
        seed.plan=c.plan;seed.commands=c.commands
        seed.descriptor['dynamic_dependencies']=seed.dynamic(seed.descriptor)
        seed.seal()
        seed.acceptance['evidence_level']='input-and-route-only'
        seed.acceptance['input_route_audit']=c.f.document(dict(
            format='emberbsd-bootstrap-seed-input-route-audit',version=1,scope='gcc16-bootstrap',status='PASS',
            descriptor_sha256=seed.descriptor_ref['sha256'],bootstrap_plan=seed.plan_ref,
            tools=seed.descriptor['tools'],dynamic_dependencies=seed.descriptor['dynamic_dependencies'],
            expected_commands=seed.plan['commands'],execution_observed=False,
            unresolved_tool_routes=[],unresolved_abi_inputs=[],unresolved_dsos=[]))
        for role in ('cc','cxx'):
            for option,value in (('-dumpmachine',seed.descriptor['compiler']['triplet']),
                                 ('-dumpfullversion',seed.descriptor['compiler']['version'])):
                seed.acceptance['checks']['identity'][role][option]=c.f.command([*c.commands[role],option],(value+'\n').encode())
        seed.acceptance_ref=c.f.document(seed.acceptance)
        options=dict(descriptor_sha256=seed.descriptor_ref['sha256'],accepted_seed_receipt=seed.acceptance_ref,
                     bootstrap_plan=seed.plan_ref,required_roles=seed.plan['required_roles'])
        with patch('emberbsd_sdk.bootstrap_seed.platform.system',return_value='NetBSD'), \
             patch('emberbsd_sdk.bootstrap_seed.platform.machine',return_value='amd64'), \
             patch('subprocess.Popen',side_effect=AssertionError('no child')):
            selected=load_bootstrap_seed(seed.descriptor_ref['path'],**options)
            self.assertEqual(selected.command('sh'),(str(c.shell.mksh),))
            self.assertNotIn('make',selected.commands)
            self.assertNotIn('gmake',selected.commands)
            wrong=copy.deepcopy(seed.plan);wrong['commands'][0]['env']['PATH']='/usr/bin'
            with self.assertRaisesRegex(SDKError,'not accepted for this exact bootstrap plan'):
                load_bootstrap_seed(seed.descriptor_ref['path'],**dict(options,bootstrap_plan=c.f.document(wrong)))

    def test_cli_prepares_without_executing_or_issuing_acceptance(self):
        c=self.c
        script=Path(__file__).resolve().parents[1]/'scripts/bootstrap-seed.py'
        spec=importlib.util.spec_from_file_location('nawk_cli_test',script)
        cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
        output=c.base/'planned.json'
        args=['plan-nawk']
        for name,ref in (('descriptor',c.descriptor_ref),('program-source',c.source_ref),('pkgsrc-tools-acceptance',c.pkgsrc_ref),('shell-runtime',c.shell_runtime_ref)):
            args.extend(['--'+name,ref['path'],'--'+name+'-sha256',ref['sha256']])
        args.extend(['--work-root',str(c.work),'--lock-root',str(c.base),'--heavy-link-context',str(c.base/'heavy'),'--output',str(output)])
        with patch('subprocess.Popen',side_effect=AssertionError('no child')), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(args),0)
        self.assertEqual(json.loads(output.read_bytes()),c.plan)
        self.assertFalse(c.work.exists())
        self.assertNotIn('status',c.plan)


if __name__=='__main__':unittest.main()
