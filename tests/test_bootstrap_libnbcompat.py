# SPDX-License-Identifier: BSD-2-Clause
# Origin: EmberBSD (AI-assisted).
"""Finite Ports library contracts. No native build is executed."""
import importlib.util
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import struct
import subprocess
import unittest
from unittest.mock import patch
import test_bootstrap_bsdinstall as bsd_tests
from emberbsd_sdk import bootstrap_libnbcompat as lib, bootstrap_program as program
from emberbsd_sdk import bootstrap_producer as producer
from emberbsd_sdk import bootstrap_pilot
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.inventory import snapshot_tree


class SharedComponentTests(unittest.TestCase):
    def test_shared_component_owner_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('emberbsd_sdk.bootstrap_libnbcompat'),
                             'both consumers need the shared Ports component owner')

    def test_consumers_share_the_exact_legacy_validator(self):
        from emberbsd_sdk import bootstrap_bsdinstall, bootstrap_pkg_install
        self.assertIs(bootstrap_bsdinstall._compat,lib.validate_input)
        self.assertIs(bootstrap_pkg_install._compat,lib.validate_input)


class InitialPlanTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(lib,'STAGE'),'missing finite Ports component stage')
        self.old=bsd_tests.BSDInstallTests();self.old.setUp();self.addCleanup(self.old.doCleanups)
        self.f,self.base=self.old.f,self.old.base
        self.descriptor,self.commands,self.flags=self.old.descriptor,self.old.commands,self.old.flags
        self.descriptor['compiler']['triplet']='aarch64-apple-darwin25'
        self.work=self.base/'lib-work';self.source=self.base/'lib-source';self.owner=self.base/'lib-owner'
        self.exports=['include/nbcompat.h','include/nbcompat/config.h','include/nbcompat/nbconfig.h',
            'include/nbcompat/db.h',*('include/nbcompat/f'+str(i)+'.h' for i in range(36)),'lib/libnbcompat.a']
        self.manifest=dict(format='emberbsd-native-bootstrap-component',version=1,name='ports-libnbcompat',
            recipe='pkgtools/libnbcompat',source_directory='pkgtools/libnbcompat/files',
            initial_only=True,build_system='pkgsrc-library',compiler=dict(family='gcc',major=16,abi='native'),
            configure_args=['--enable-bsd-getopt','--enable-db'],exports=self.exports,
            make_targets=['all'],stage_target='install',required_tool_roles=sorted(lib.ROLES),
            infrastructure_patches=[lib.PATCH],diagnostic_helper=lib.HELPER,origin='synthetic')
        data={'pkgtools/libnbcompat/Makefile':'CONFIGURE_ARGS+= --enable-bsd-getopt --enable-db\n',
              'pkgtools/libnbcompat/PLIST':'\n'.join(self.exports)+'\n',
              'bootstrap/bootstrap':'--enable-bsd-getopt --enable-db\n',
              'pkgtools/libnbcompat/files/configure':'synthetic complete configure\n',
              'pkgtools/libnbcompat/files/Makefile.in':'synthetic complete Makefile.in\n',
              'pkgtools/libnbcompat/files/db/Makefile.inc':'OBJS+= db.o\n',
              'pkgtools/libnbcompat/files/emberbsd-configure-diagnostics.sh':'synthetic shared helper\n'}
        for name,raw in data.items():self.old.put(self.source/name,raw.encode())
        self.old.readonly(self.source)
        for name in (lib.PATCH,'pkgsrc/export.conf'):self.old.put(self.owner/name,b'synthetic owner\n')
        self.old.put(self.owner/lib.HELPER,b'synthetic shared helper\n')
        raw=self.f.document(self.manifest)
        self.old.put(self.owner/lib.MANIFEST_RELATIVE,Path(raw['path']).read_bytes())
        ref=dict(path=str(self.owner/lib.MANIFEST_RELATIVE),sha256=raw['sha256'])
        selected=patch.object(lib,'MANIFEST_SHA256',raw['sha256']);selected.start();self.addCleanup(selected.stop)
        for role in lib.ROLES:
            if role not in self.commands:
                self.commands[role]=(str(self.base/('selected-'+role)),)
                self.descriptor['tools'][role]=dict(kind='native')
        self.commands['make']=(str(self.base/'selected-make'),'-m',str(self.base/'make-support'),
            'SHELL='+self.commands['sh'][0],'MAKECONF='+str(self.base/'selected.conf'))
        export=self.base/'lib-export';export.mkdir()
        self.old.put(export/producer.PROFILE_EXPORT,b'synthetic current profile\n')
        profile=dict(path=str(export/producer.PROFILE_EXPORT),sha256=self.f.raw(b'synthetic current profile\n')['sha256'])
        self.plan=dict(format=producer.PRODUCER_FORMAT,version=1,scope='gcc16-bootstrap',stage=lib.STAGE,
            seed_descriptor={},host=self.descriptor['host'],sources=self.descriptor['sources'],work_root=str(self.work),
            required_roles=sorted(lib.ROLES|set(self.descriptor['driver_components'].values())),
            resources=dict(cpu_jobs=1,link_jobs=1,runtime_seconds=1200,lock_root=str(self.base),heavy_link_context=str(self.base/'heavy')),
            program=dict(source=self.old.tree(self.source,owner=False),owner=self.old.tree(self.owner,owner=True),
                manifest=ref,profile=profile,export=dict(root=str(export),inventory=self.f.document(snapshot_tree(str(self.source))),source_proof=self.f.document({}))))
        self.plan['commands']=program.program_steps(self.plan,self.descriptor,self.commands,self.flags)
        self.plan['seed_descriptor']=self.f.document(self.descriptor)
        binding=patch('emberbsd_sdk.bootstrap_binding.seed_commands',return_value=self.commands)
        binding.start();self.addCleanup(binding.stop)

    def validate(self):
        with patch.object(producer,'_export_proof'),patch('subprocess.Popen',side_effect=AssertionError('no native')):
            return producer.validate_producer_plan(self.plan,self.descriptor,self.commands,self.flags)

    def test_three_exact_owner_steps_and_copy_install(self):
        self.assertIs(self.validate(),self.plan)
        self.assertEqual([r['id'] for r in self.plan['commands']],['libnbcompat-configure','libnbcompat-build','libnbcompat-stage'])
        first,build,stage=self.plan['commands']
        self.assertEqual(shlex.split(first['env']['CC']),list(self.commands['cc']))
        self.assertEqual(shlex.split(first['env']['MAKE']),list(self.commands['make']))
        self.assertEqual(first['env']['PATH'],str(self.work/'bin'))
        self.assertEqual(shlex.split(first['env']['INSTALL']),[*self.commands['sh'],str(self.work/'source/install-sh'),'-c'])
        self.assertIn('DESTDIR=',stage['argv']);self.assertIn('LIBMODE=644',stage['argv'])
        self.assertIn('SHELL='+self.commands['sh'][0],build['argv'])
        self.assertEqual(first['argv'][-2:],['--enable-bsd-getopt','--enable-db'])

    def test_missing_utilities_and_incomplete_make_shell_refuse(self):
        for role in ('cp','mv','ls','diff'):
            old=list(self.plan['required_roles']);self.plan['required_roles'].remove(role)
            with self.subTest(role=role),self.assertRaises(SDKError):self.validate()
            self.plan['required_roles']=old
        self.commands['make']=self.commands['make'][:3]
        self.plan['commands']=program.program_steps(self.plan,self.descriptor,self.commands,self.flags)
        with self.assertRaises(SDKError):self.validate()

    def test_no_install_move_or_arbitrary_option_or_export_omission(self):
        for mutation in ('move','option','member'):
            saved=copy.deepcopy(self.plan)
            if mutation=='move':self.plan['commands'][2]['env']['INSTALL']=str(self.work/'source/install-sh')
            elif mutation=='option':self.plan['commands'][0]['argv'].append('--disable-db')
            else:
                inv=snapshot_tree(str(self.source));del inv['pkgtools/libnbcompat/files/db/Makefile.inc']
                self.plan['program']['export']['inventory']=self.f.document(inv)
            with self.subTest(mutation=mutation),self.assertRaises(SDKError):self.validate()
            self.plan=saved

    def test_preparation_and_stage_keep_full_source_and_native_db_objects(self):
        self.assertTrue(hasattr(lib,'prepare_work'),'missing shared source staging')
        self.work.mkdir()
        lib.prepare_work(self.plan,self.commands)
        for role in ('cp','mv','ls','diff'):
            self.assertEqual(os.readlink(self.work/'bin'/role),self.commands[role][0])
        self.assertEqual((self.work/'source/configure').read_bytes(),b'synthetic complete configure\n')
        self.fake_outputs()
        evidence=lib.verify(self.plan['commands'][2],str(self.work),self.plan,0,())
        self.assertEqual(len(evidence['staged_inventory']),44)
        self.assertEqual(set(evidence['built_objects']),{'glob.o','db.o'})
        header = self.work/'stage/include/nbcompat/db.h'
        header.chmod(0o644)
        header.write_bytes(b'wrong DB header')
        header.chmod(0o444)
        with self.assertRaises(SDKError):lib.verify(self.plan['commands'][2],str(self.work),self.plan,0,())

    def fake_outputs(self):
        source=self.work/'source'
        env=self.plan['commands'][0]['env']
        assignments={k:env[k] for k in ('CC','CPPFLAGS','LDFLAGS','AWK','AR','RANLIB','INSTALL')}
        assignments.update(CCLD=env['CC'],CFLAGS='-I$(srcdir) -I. '+env['CFLAGS'],
            srcdir=str(source),prefix=str(self.work/'stage'))
        self.old.put(source/'Makefile',('OBJS= glob.o\nWITH_DB= yes\n'+
            ''.join(k+'= '+v+'\n' for k,v in assignments.items())).encode())
        for relative in ('config.log','config.status','nbcompat/config.h','nbcompat/nbconfig.h'):
            self.old.put(source/relative,('actual generated '+relative+'\n').encode())
        obj=struct.pack('<8I',0xfeedfacf,0x100000c,0,1,0,0,0,0)
        archive=b'!<arch>\n'
        for name in ('glob.o','db.o'):
            self.old.put(source/name,obj)
            archive+=(name+'/').ljust(16).encode()+b'0'.ljust(12)+b'0'.ljust(6)+b'0'.ljust(6)+b'100644'.ljust(8)+str(len(obj)).ljust(10).encode()+b'`\n'+obj
        self.old.put(source/'libnbcompat.a',archive)
        for name in self.exports:
            if name.endswith('.a'):raw=archive
            else:
                path=source/name.removeprefix('include/')
                if not path.exists():self.old.put(path,('canonical '+name+'\n').encode())
                raw=path.read_bytes()
            self.old.put(self.work/'stage'/name,raw)
            (self.work/'stage'/name).chmod(0o644 if name.endswith('.a') else 0o444)

    def test_configured_route_and_generated_mode_drift_refuse(self):
        self.work.mkdir();lib.prepare_work(self.plan,self.commands);self.fake_outputs()
        path=self.work/'source/Makefile';old=path.read_bytes()
        path.write_bytes(old.replace(self.plan['commands'][0]['env']['CC'].encode(),b'/usr/bin/cc'))
        with self.assertRaises(SDKError):lib.verify(self.plan['commands'][0],str(self.work),self.plan,0,())
        path.write_bytes(old)
        configuration=lib.verify(self.plan['commands'][0],str(self.work),self.plan,0,())
        (self.work/'source/nbcompat/config.h').chmod(0o600)
        with self.assertRaises(SDKError):lib.prepare(self.plan['commands'][1],self.plan,[configuration])

    def test_namespace_drift_refuses_before_child(self):
        self.work.mkdir();lib.prepare_work(self.plan,self.commands)
        lib.prepare(self.plan['commands'][0],self.plan,())
        alias=self.work/'bin/uname';alias.unlink();alias.symlink_to('/usr/bin/uname')
        with self.assertRaises(SDKError):lib.prepare(self.plan['commands'][0],self.plan,())
        alias.unlink();alias.symlink_to(self.commands['uname'][0])
        (self.work/'bin/unselected').symlink_to('/bin/sh')
        with self.assertRaises(SDKError):lib.prepare(self.plan['commands'][0],self.plan,())

    def test_archive_members_and_header_export_refuse_omissions(self):
        self.work.mkdir();lib.prepare_work(self.plan,self.commands);self.fake_outputs()
        archive=self.work/'source/libnbcompat.a';saved=archive.read_bytes()
        for changed in (saved[:-1],saved[:8],saved+saved[8:]):
            archive.write_bytes(changed)
            with self.assertRaises(SDKError):lib.verify(self.plan['commands'][1],str(self.work),self.plan,0,())
        archive.write_bytes(saved)
        extra=self.work/'stage/include/unselected.h';extra.write_bytes(b'not in the package\n')
        with self.assertRaises(SDKError):lib.verify(self.plan['commands'][2],str(self.work),self.plan,0,())
        extra.unlink();extra.symlink_to('nbcompat.h')
        with self.assertRaises(SDKError):lib.verify(self.plan['commands'][2],str(self.work),self.plan,0,())

    def test_common_pilot_holds_all_roots_and_runs_three_owned_steps(self):
        descriptor_ref=self.plan['seed_descriptor'];plan_ref=self.f.document(self.plan)
        accepted=self.f.document({});case=self
        class Selection:
            commands=case.commands
            def as_dict(self):return dict(plan=case.validate())
        def locked(context):
            name='.prebuild-sdk-lock-'+hashlib.sha256(str(context).encode()).hexdigest()
            with open(self.base/name,'rb') as handle:
                with self.assertRaises(BlockingIOError):fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        def fake_command(argv,log,**kwargs):
            step=next(row for row in self.plan['commands'] if row['id']==Path(log).stem)
            self.assertEqual(argv,step['argv']);self.assertEqual(kwargs['env'],step['env'])
            for root in (self.source,self.owner,self.work,self.plan['program']['export']['root'],
                         self.plan['resources']['heavy_link_context']):locked(root)
            if step['phase']=='libnbcompat-configure':self.fake_outputs()
            Path(log).write_bytes(b'synthetic drained child; no native executable ran\n')
            return subprocess.CompletedProcess(argv,0)
        with patch('emberbsd_sdk.bootstrap_seed.load_bootstrap_seed',side_effect=lambda *a,**kw:Selection()) as loader, \
             patch.object(bootstrap_pilot,'run_logged',side_effect=fake_command), \
             patch('subprocess.Popen',side_effect=AssertionError('no native child')):
            result=bootstrap_pilot.run_producer(descriptor_ref['path'],descriptor_sha256=descriptor_ref['sha256'],
                accepted_seed_receipt=accepted,bootstrap_plan=plan_ref,required_roles=self.plan['required_roles'])
        self.assertEqual(result['status'],'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW',result.get('error'))
        self.assertEqual(result['stage'],lib.STAGE);self.assertEqual(len(result['commands']),3)
        self.assertEqual(len(result['outputs']),48)
        self.assertTrue(result['all_commands_drained'] and result['inputs_revalidated_after_drain'] and result['all_leases_closed'])
        self.assertFalse(result['ordinary_provider_qualified']);self.assertEqual(loader.call_count,2)

    def test_candidate_requires_complete_result_and_independent_sealed_acceptance(self):
        self.assertTrue(hasattr(lib,'candidate'),'missing independent component candidate')
        self.work.mkdir();lib.prepare_work(self.plan,self.commands);self.fake_outputs()
        self.plan['seed_descriptor']=self.f.document(self.descriptor)
        plan_ref=self.f.document(self.plan)
        accepted=dict(format='emberbsd-bootstrap-seed-acceptance',version=1,scope='gcc16-bootstrap',
            status='PASS',accepted_input_scope='bootstrap-seed',descriptor_sha256=plan_ref['sha256'],bootstrap_plan=plan_ref)
        accepted['descriptor_sha256']=self.plan['seed_descriptor']['sha256']
        for key in lib.SEED_FIELDS:accepted[key]=self.descriptor.get(key)
        rows=[]
        for step in self.plan['commands']:
            path=self.work/(step['id']+'.log');path.write_bytes(b'completed synthetic route log\n')
            row=dict(step,actual_exit=0,log=lib._regular(path))
            row.update(lib.verify(step,str(self.work),self.plan,0,rows));rows.append(row)
        result=dict(format='emberbsd-native-gcc16-producer-result',version=1,scope='gcc16-bootstrap',api=1,
            stage=lib.STAGE,status='COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW',bootstrap_plan=plan_ref,
            seed_descriptor=self.plan['seed_descriptor'],accepted_seed_receipt=self.f.document(accepted),
            commands=rows,ordinary_provider_qualified=False,all_commands_drained=True,
            inputs_revalidated_after_drain=True,all_leases_closed=True,
            outputs={n:lib._regular(self.work/n)['sha256'] for n in lib.outputs(self.plan)})
        result_ref=self.f.document(result)
        with patch.object(producer,'_export_proof'),patch('emberbsd_sdk.bootstrap_binding.seed_commands',return_value=self.commands):
            self.old.readonly(self.work/'stage')
            pending=lib.candidate(plan_ref,result_ref)
            self.assertEqual(pending['status'],'PENDING_INDEPENDENT_COMPONENT_REVIEW')
            self.assertFalse(pending['ordinary_provider_qualified'])
            candidate_ref=self.f.document(pending)
            review=self.f.document(dict(status='REVIEWED_INITIAL_PORTS_LIBNBCOMPAT_COMPONENT',candidate=candidate_ref,
                findings=[],all_leases_closed=True,ordinary_provider_qualified=False))
            receipt=self.f.document(dict(format='emberbsd-ports-libnbcompat-component-acceptance',version=1,
                scope='gcc16-bootstrap',status='ACCEPTED_INITIAL_PORTS_LIBNBCOMPAT_COMPONENT',recipe='pkgtools/libnbcompat',
                candidate=candidate_ref,independent_review=review,initial_only=True,ordinary_provider_qualified=False))
            value=dict(root=str(self.work/'stage'),inventory=self.f.document(snapshot_tree(str(self.work/'stage'))),
                producer_result=result_ref,bootstrap_plan=plan_ref,component_receipt=receipt)
            lib.validate_input(value,self.descriptor)
            unreviewed=copy.deepcopy(value)
            envelope=json.loads(Path(receipt['path']).read_text())
            envelope['independent_review']=self.f.document(dict(status='REVIEWED_INITIAL_PORTS_LIBNBCOMPAT_COMPONENT',
                candidate=candidate_ref,findings=['unresolved'],all_leases_closed=True,ordinary_provider_qualified=False))
            unreviewed['component_receipt']=self.f.document(envelope)
            with self.assertRaises(SDKError):lib.validate_input(unreviewed,self.descriptor)
            unreviewed['component_receipt']=self.f.document(dict(format='emberbsd-os-compat-component-acceptance',
                status='PASS',candidate=candidate_ref))
            with self.assertRaises(SDKError):lib.validate_input(unreviewed,self.descriptor)
            (self.work/'stage').chmod(0o755)
            with self.assertRaises(SDKError):lib.validate_input(value,self.descriptor)
            (self.work/'stage').chmod(0o555)
            for mutation in ('failed','partial','bool-exit','open-lease'):
                bad=copy.deepcopy(result)
                if mutation=='failed':bad['status']='FAILED'
                elif mutation=='partial':bad['commands']=bad['commands'][:2]
                elif mutation=='bool-exit':bad['commands'][0]['actual_exit']=False
                else:bad['all_leases_closed']=False
                with self.subTest(mutation=mutation),self.assertRaises(SDKError):
                    lib.candidate(plan_ref,self.f.document(bad))
            changed=copy.deepcopy(self.descriptor);changed['compiler']['triplet']+='-wrong'
            with self.assertRaises(SDKError):lib.validate_input(value,changed)


if __name__ == '__main__':
    unittest.main()
