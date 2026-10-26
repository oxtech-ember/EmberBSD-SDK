# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Nawk output admission and historical reuse use synthetic ELF data only."""

import copy
import os
from pathlib import Path
import shutil
import struct
import unittest
from unittest.mock import patch

from test_bootstrap_nawk import NawkFixture, elf
from emberbsd_sdk import bootstrap_nawk as nawk, bootstrap_nawk_runtime as runtime
from emberbsd_sdk.bootstrap_program_history import verify_program_build_history
from emberbsd_sdk.bootstrap_seed import _history
from emberbsd_sdk.elf import read_elf_dynamic
from emberbsd_sdk.errors import SDKError


class NawkHistoryTests(unittest.TestCase):
    def setUp(self):
        c=self.c=NawkFixture(self)
        self.result=c.run()
        accepted=dict(format='emberbsd-bootstrap-seed-acceptance',version=1,scope='gcc16-bootstrap',status='PASS',
            accepted_input_scope='bootstrap-seed',descriptor_sha256=c.descriptor_ref['sha256'],bootstrap_plan=c.plan_ref)
        for key in ('host','compiler','sources','providers','host_abi','abi_flags','history','dynamic_dependencies',
                    'driver_prefix','driver_binding','make_binding'):
            accepted[key]=copy.deepcopy(c.descriptor.get(key))
        accepted['accepted_history_unknowns']=_history(c.descriptor['history'],c.descriptor['providers'],
                                                       descriptor=c.descriptor,inspect_inputs=False)
        self.result['accepted_seed_receipt']=c.f.document(accepted)
        self.image=copy.deepcopy(next(row['output'] for row in self.result['commands'] if row['id']=='link-nawk'))
        shell=c.shell.ref(c.shell.mksh)
        self.audit=dict(format=runtime.AUDIT_FORMAT,version=1,scope='producer-output',host=c.plan['host'],
            artifact=self.image,selected_shell=shell,nodes={},observations=[])
        for reference in (self.image,shell):
            self.audit['nodes'][reference['path']]=dict(sha256=reference['sha256'],
                elf=read_elf_dynamic(reference['path'],machine=62),interpreter=None,interpreter_target=None,dependencies={})
        for step,case in zip([row for row in c.plan['commands'] if row['phase']=='program-case'],c.manifest['runtime_cases']):
            self.audit['observations'].append(dict(command=step,ktrace=c.f.raw(b'synthetic binary ktrace'),
                decoded_trace=c.f.raw(b'synthetic decoded trace; independent fixture, no process ran'),
                executed_paths=[shell['path'],str(c.work/case['invocation'])],undeclared_tools=[],unresolved_dsos=[]))
        self.accepted=dict(format='emberbsd-bootstrap-program-output-acceptance',version=1,scope='gcc16-bootstrap',status='PASS',
            accepted_input_scope='bootstrap-seed',ordinary_provider_qualified=False,bootstrap_plan=c.plan_ref,
            source_proof=c.inputs['source_proof'],checks=list(runtime.CHECKS),artifact=self.image,
            semantic_review=c.f.raw(b'Synthetic independent semantics. No native compilation.'),
            limitations=['Bootstrap-only synthetic fixture. No ordinary SDK qualification.'])
        self.record=dict(format='emberbsd-bootstrap-program-build-history',version=1,scope='gcc16-bootstrap',
            manifest=c.plan['program']['manifest'],source_proof=c.inputs['source_proof'],bootstrap_plan=c.plan_ref)
        self.target=c.base/'next-inputs';(self.target/'bin').mkdir(parents=True)
        self.selected=self.target/'bin/awk'
        shutil.copyfile(self.image['path'],self.selected);self.selected.chmod(0o755)
        provider=dict(origin='source-build',source='ports_previous',recipe=nawk.MANIFEST_RELATIVE,
                      recipe_sha256=c.plan['program']['manifest']['sha256'])
        self.history=dict(kind='source-build',source_archives=[],patches=[],
            provisioned_members=[dict(invocation='awk',original_path=self.image['path'],selected_path=str(self.selected),sha256=self.image['sha256'])])
        self.descriptor=dict(bundle_root=str(self.target),host=c.plan['host'],host_abi=dict(libraries=[]),
            sources=dict(ports_previous=c.inputs['source']),providers={'nawk':provider},
            tools=dict(awk=dict(path=str(self.selected),sha256=self.image['sha256'],kind='native',provider='nawk',version='nawk-20260426')))
        self.descriptor['history']={'nawk':self.history}

    def seal(self):
        c=self.c
        self.accepted['producer_result']=c.f.document(self.result)
        self.accepted['runtime_audit']=c.f.document(self.audit)
        self.accepted['runtime_closure']=c.f.document(runtime.runtime_closure(self.audit,self.accepted['runtime_audit']))
        self.record.update(producer_result=self.accepted['producer_result'],output_acceptance=c.f.document(self.accepted))
        self.history['program_build']=c.f.document(self.record)

    def verify(self,inspect_inputs=True):
        self.seal()
        return verify_program_build_history(self.history,'nawk',self.descriptor,inspect_inputs=inspect_inputs)

    def test_complete_scoped_history_binds_awk_role_without_reexecution(self):
        with patch('subprocess.Popen',side_effect=AssertionError('no child')):
            self.assertEqual(self.verify(),[])
            runtime.validate_shell_selection(self.descriptor,self.c.commands)
        self.assertEqual(self.descriptor['tools']['awk']['version'],'nawk-20260426')

    def test_retired_compiler_and_original_output_are_not_reopened(self):
        shutil.rmtree(self.c.f.bundle)
        Path(self.image['path']).unlink()
        self.assertEqual(self.verify(),[])

    def test_later_seed_keeps_loader_policy_and_rejects_new_configuration(self):
        c = self.c
        self.assertEqual(runtime.policy_read_paths(self.descriptor), [str(c.loader_config)])
        unrelated = copy.deepcopy(self.descriptor)
        unrelated['providers']['nawk']['recipe'] = 'other/bootstrap.json'
        self.assertEqual(runtime.policy_read_paths(unrelated), [])
        for kind in ('file', 'directory', 'symlink', 'dangling-symlink'):
            with self.subTest(kind=kind):
                if kind == 'file': c.loader_config.write_text('/changed/search/path\n')
                elif kind == 'directory': c.loader_config.mkdir()
                else: c.loader_config.symlink_to(c.shell.mksh if kind == 'symlink' else c.base / 'absent')
                try:
                    with self.assertRaisesRegex(SDKError, 'requires absent loader configuration'):
                        self.verify()
                    self.assertEqual(self.verify(inspect_inputs=False), [])
                finally:
                    if kind == 'directory': c.loader_config.rmdir()
                    else: c.loader_config.unlink()

    def test_selected_shell_remains_a_live_dependency(self):
        self.c.shell.mksh.write_bytes(elf()+b'changed shell')
        with self.assertRaises(SDKError):self.verify()
        self.assertEqual(self.verify(inspect_inputs=False),[])

    def test_output_cannot_relabel_the_initial_shell_runtime_when_inputs_are_retired(self):
        shell = str(self.c.shell.mksh)
        self.audit['nodes'][shell]['elf']['rpath'] = '/different/runtime'
        with self.assertRaisesRegex(SDKError, 'runtime graph differs from its accepted inputs'):
            self.verify(inspect_inputs=False)

    def test_copied_output_hash_and_elf_must_match(self):
        self.selected.write_bytes(elf()+b'changed nawk')
        with self.assertRaises(SDKError):self.verify()

    def test_subsequent_seed_cannot_switch_nawk_command_shell(self):
        self.verify()
        commands=dict(self.c.commands,sh=('/bin/sh',))
        with self.assertRaisesRegex(SDKError,'selected shell differs'):
            runtime.validate_shell_selection(self.descriptor,commands)

    def test_runtime_root_hash_or_route_changes_refuse(self):
        for mutation in ('shell-hash','system-shell','extra-exec','missing-case','missing-trace','command'):
            old=copy.deepcopy(self.audit)
            if mutation=='shell-hash':self.audit['selected_shell']['sha256']='0'*64
            elif mutation=='system-shell':self.audit['selected_shell']['path']='/bin/sh'
            elif mutation=='extra-exec':self.audit['observations'][0]['executed_paths'].append('/bin/sh')
            elif mutation=='missing-case':self.audit['observations'].pop()
            elif mutation=='missing-trace':self.audit['observations'][0]['ktrace']=self.c.f.raw(b'')
            else:self.audit['observations'][0]['command']['env']['PATH']='/usr/bin'
            with self.subTest(mutation=mutation),self.assertRaises(SDKError):self.verify()
            self.audit=old

    def test_incomplete_or_unrelated_runtime_graph_refuses(self):
        for mutation in ('missing-edge','unrelated','missing-loader'):
            old=copy.deepcopy(self.audit)
            row=self.audit['nodes'][self.image['path']]
            if mutation=='missing-edge':row['elf']['needed']=['libmissing.so.1']
            elif mutation=='missing-loader':row.update(interpreter='/libexec/ld.elf_so',interpreter_target='/libexec/ld.elf_so')
            else:self.audit['nodes']['/unrelated']=copy.deepcopy(row)
            with self.subTest(mutation=mutation),self.assertRaises(SDKError):self.verify()
            self.audit=old

    def test_failed_or_self_qualified_result_refuses(self):
        for key,value in (('all_commands_drained',False),('ordinary_provider_qualified',True),('status','FAILED')):
            old=copy.deepcopy(self.result);self.result[key]=value
            with self.subTest(key=key),self.assertRaises(SDKError):self.verify()
            self.result=old
        for key,value in (('ordinary_provider_qualified',True),('checks',runtime.CHECKS[:-1]),('scope','native-build')):
            old=copy.deepcopy(self.accepted);self.accepted[key]=value
            with self.subTest(key=key),self.assertRaises(SDKError):self.verify()
            self.accepted=old

    def test_successful_exit_does_not_replace_semantic_output_evidence(self):
        row=next(row for row in self.result['commands'] if row['phase']=='program-case')
        Path(row['stdout']['path']).write_text('wrong result\n')
        with self.assertRaises(SDKError):self.verify()

    def test_loader_alias_is_separate_from_literal_interp_and_must_remain_bound(self):
        root=self.c.base/'runtime';root.mkdir()
        loader=root/'loader';loader.write_bytes(elf(3))
        alias=root/'interpreter';alias.symlink_to('loader')
        literal=str(alias).encode()+b'\0'
        image=bytearray(elf());struct.pack_into('<Q',image,32,64);struct.pack_into('<HH',image,54,56,1)
        image+=struct.pack('<IIQQQQQQ',3,4,120,0,0,len(literal),len(literal),1)+literal
        self.selected.write_bytes(image)
        self.assertEqual(runtime._interpreter(self.selected),str(alias))
        artifact=dict(path='/retired/nawk',sha256=self.c.shell.digest(self.selected))
        shell=self.c.shell.ref(self.c.shell.mksh)
        audit=dict(artifact=artifact,selected_shell=shell,nodes={})
        for ref in (artifact,shell,self.c.shell.ref(loader)):
            current=str(self.selected) if ref==artifact else ref['path']
            audit['nodes'][ref['path']]=dict(sha256=ref['sha256'],elf=read_elf_dynamic(current,machine=62),
                interpreter=str(alias) if ref==artifact else None,interpreter_target=str(loader) if ref==artifact else None,dependencies={})
        accepted=dict(runtime_audit=self.c.f.document(audit))
        descriptor=dict(host_abi=dict(libraries=[dict(root=str(root))]))
        runtime.revalidate_runtime(accepted,artifact,str(self.selected),descriptor)
        other=root/'other';other.write_bytes(elf(3));alias.unlink();alias.symlink_to('other')
        with self.assertRaisesRegex(SDKError,'loader alias changed'):
            runtime.revalidate_runtime(accepted,artifact,str(self.selected),descriptor)


if __name__=='__main__':unittest.main()
