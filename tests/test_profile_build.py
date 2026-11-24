# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""profile-build: Ninja runs the nodes of a build plan that are not ready."""

import copy
import io
import json
import os
from pathlib import Path
import shutil
import sys
import unittest
from contextlib import redirect_stderr
from unittest import mock

from test_build_profile import PROFILE, ProfileFixture, git
from emberbsd_sdk import profile_build, workspace
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.jsonio import write_json
from emberbsd_sdk.sysroot_builder import SysrootBuilder

NINJA = shutil.which('ninja')
NEW_NINJA = bool(NINJA) and profile_build.ninja_version(NINJA) >= profile_build.NINJA_MINIMUM

# A Ports source producer with the common interface: find the archive of
# the recipe in --distdir and copy it into the new output tree.
PRODUCER = '''import os, sys
arguments = sys.argv[1:]
value = lambda name: arguments[arguments.index(name) + 1]
# A producer is one job and does not see the jobserver.
assert 'MAKEFLAGS' not in os.environ, os.environ['MAKEFLAGS']
assert arguments[4] == 'prepare' and os.path.isdir(value('--pkgsrc-export'))
archive = [name for name in os.listdir(value('--distdir')) if name.startswith(PACKAGE.split('/')[1])][0]
os.makedirs(value('--output'))
open(os.path.join(value('--output'), 'emberbsd-prepared-source.json'), 'w').write(archive)
open(os.environ['TEST_LOG'], 'a').write('prepare ' + archive + '\\n')
'''
# ember/build-kernel.sh: record the selected sources, then write the outputs.
KERNEL = '''set -eu
out=$1; config=${2##*/}
echo "kernel $2 byacc=$EMBER_BYACC_SOURCE llvm=$EMBER_LLVM_SOURCE jobs=$NETBSD2_JOBS target=$EMBER_KERNEL_TARGET" >> LOG
# The builder runs in a clean environment; a file selects the failure.
[ ! -e LOG.fail ] || exit 1
: > "$out/netbsd-$config"; : > "$out/netbsd-$config.img"
'''


# Ninja: record the arguments and the free jobserver tokens.
FAKE_NINJA = '''import os, re, sys
if sys.argv[1:] == ['--version']:
    print(os.environ['FAKE_VERSION'])
    sys.exit(0)
path = re.search(r'--jobserver-auth=fifo:(\\S+)', os.environ['MAKEFLAGS']).group(1)
descriptor = os.open(path, os.O_RDWR | os.O_NONBLOCK)
tokens = os.read(descriptor, 64)
os.write(descriptor, tokens)
open(os.environ['FAKE_LOG'], 'w').write(' '.join(sys.argv[1:]) + ' tokens=%d' % len(tokens))
'''


class ReleaseNodeTests(ProfileFixture):
    """The os:sets edge runs the OS release builder; a stub stands for it."""

    def setUp(self):
        super().setUp()
        self.work = self.root / 'work'
        llvm = self.write('llvm/llvm-source.json', '{}').parent
        self.config = dict(host_prefix=str(self.native.bundle_root), os_source=str(self.os),
                           ports_root=str(self.ports), sdk_root=str(self.sdk), host_bfd_config=str(self.bfd),
                           distfiles=str(self.root / 'distfiles'),
                           selected_sources={'EMBER_LLVM_SOURCE': str(llvm)}, jobs=3)
        profile = copy.deepcopy(PROFILE)
        profile['os']['sets'] = ['base', 'comp', 'etc']
        self.plan_data = self.plan(profile, state=self.work, selected_sources=self.config['selected_sources'])
        self.nodes = {node['id']: node for node in self.plan_data['nodes']}
        self.work.mkdir()
        write_json(self.work / profile_build.PLAN_FILE, self.plan_data, exclusive=False)
        for variable, manifest in (('EMBER_BYACC_SOURCE', 'emberbsd-prepared-source.json'),
                                   ('EMBER_HEIMDAL_SOURCE', 'emberbsd-prepared-source.json'),
                                   ('EMBER_LIBEDIT_SOURCE', 'libedit-source.json')):
            tree = self.write(f'sources/{variable}/{manifest}', '{}').parent
            profile_build._write_record(self.work, self.nodes['source:' + variable], {variable: str(tree)})
        profile_build._workspace(self.work, self.plan_data, create=True)
        self.calls = []

    def run_sets(self):
        calls = self.calls

        class Builder:
            RESUMES_FAILED_STAGES = False

            def __init__(self, options):
                self.options = options

            def run(self):
                calls.append((dict(self.options), type(self).RESUMES_FAILED_STAGES))
                sets = Path(self.options['destination'], 'release/evbarm-aarch64/binary/sets')
                sets.mkdir(parents=True)
                for name in ('base', 'comp', 'etc'):
                    (sets / f'{name}.tar.xz').write_text(name)

        with mock.patch('emberbsd_sdk.os_release_builder.OSReleaseBuilder', Builder):
            profile_build.run_node(self.work, 'os:sets', self.nodes['os:sets']['input_key'], self.config)

    def test_sets_run_the_release_builder_with_the_plan_inputs(self):
        self.run_sets()
        options, resume = self.calls[0]
        workspace = os.path.realpath(self.work / 'sdk')
        self.assertTrue(resume)
        self.assertEqual(options['sdk'], workspace)
        self.assertEqual((options['os'], options['target'], options['compiler-family']), (str(self.os), 'aarch64', 'clang'))
        self.assertEqual(options['llvm-source'], self.config['selected_sources']['EMBER_LLVM_SOURCE'])
        self.assertEqual(options['byacc-source'], str(self.root / 'sources/EMBER_BYACC_SOURCE'))
        self.assertEqual(options['host-binutils'], str(self.bfd.parent))
        self.assertIsInstance(options['jobs'], int)
        # The objects and tools of build.sh -u belong to the host and target tools.
        self.assertTrue(options['objects'].startswith(workspace + '/obj/os-'))
        self.assertEqual(Path(options['llvm-objects']).name.removeprefix('llvm-'),
                         Path(options['objects']).name.removeprefix('os-'))
        self.assertTrue(options['destination'].startswith(workspace + '/stages/os-sets-'))
        record = json.loads((self.work / profile_build.record_name('os:sets')).read_text())
        self.assertEqual(sorted(record['outputs']), ['base.tar.xz', 'comp.tar.xz', 'etc.tar.xz'])
        self.assertTrue(all(Path(path).is_file() for path in record['outputs'].values()))

    def test_other_sources_select_new_objects_and_stages(self):
        # The builder compares source paths and manifests, so another path or
        # another manifest needs new directories for every reused build state.
        self.run_sets()
        before = self.calls[-1][0]
        self.assertEqual(Path(before['llvm-stage-root']).name, 'llvm-' + Path(before['objects']).name.removeprefix('os-'))
        self.run_sets()
        self.assertEqual(self.calls[-1][0]['objects'], before['objects'])
        tree = self.root / 'sources/EMBER_BYACC_SOURCE'
        moved = self.root / 'moved/EMBER_BYACC_SOURCE'
        moved.parent.mkdir()
        tree.rename(moved)
        profile_build._write_record(self.work, self.nodes['source:EMBER_BYACC_SOURCE'], {'EMBER_BYACC_SOURCE': str(moved)})
        self.run_sets()
        after_move = self.calls[-1][0]
        (moved / 'emberbsd-prepared-source.json').write_text('{"tree_sha256": "%s"}' % ('1' * 64))
        self.run_sets()
        after_edit = self.calls[-1][0]
        for name in ('objects', 'tools', 'llvm-objects', 'runtime-work', 'llvm-stage-root', 'destination'):
            self.assertNotIn(after_move[name], (before[name], after_edit[name]), name)
            self.assertNotEqual(after_edit[name], before[name], name)

    def test_profile_make_options_reach_the_builder_with_their_own_objects(self):
        self.run_sets()
        before = self.calls[-1][0]
        self.assertEqual(before['mk'], [])
        profile = copy.deepcopy(self.plan_data['profile'])
        profile['os']['mk'] = {'MKNTP': 'no'}
        self.plan_data = self.plan(profile, state=self.work, selected_sources=self.config['selected_sources'])
        self.nodes = {node['id']: node for node in self.plan_data['nodes']}
        write_json(self.work / profile_build.PLAN_FILE, self.plan_data, exclusive=False)
        # If the stage defaults already supply this value, its key is the old
        # final-only key. Leave existing objects there when the stages change.
        with mock.patch.object(SysrootBuilder, 'FLAGS', [*SysrootBuilder.FLAGS, 'MKNTP=no']):
            self.run_sets()
        legacy = Path(self.calls[-1][0]['llvm-objects'])
        legacy.mkdir(parents=True)
        marker = legacy / 'kept'
        marker.touch()
        self.run_sets()
        after = self.calls[-1][0]
        self.assertEqual(after['mk'], ['MKNTP=no'])
        for name in ('objects', 'tools', 'llvm-objects'):
            self.assertNotEqual(after[name], before[name], name)
        self.assertNotEqual(Path(after['llvm-objects']), legacy)
        self.assertTrue(marker.is_file())
        self.run_sets()
        self.assertEqual(self.calls[-1][0]['llvm-objects'], after['llvm-objects'])

    def test_a_new_os_commit_installs_to_the_same_release_directory(self):
        # The compiler flags name the install directory: the same directory
        # keeps them, so make does not configure the GNU programs again.
        self.run_sets()
        before, key = self.calls[-1][0], self.nodes['os:sets']['input_key']
        self.write('os/bin/cat/cat.c', 'int main(void) { return 0; }\n')
        git(self.os, 'add', '-A')
        git(self.os, 'commit', '-q', '-m', 'cat')
        self.plan_data = self.plan(self.plan_data['profile'], state=self.work,
                                   selected_sources=self.config['selected_sources'])
        self.nodes = {node['id']: node for node in self.plan_data['nodes']}
        self.assertNotEqual(self.nodes['os:sets']['input_key'], key)
        write_json(self.work / profile_build.PLAN_FILE, self.plan_data, exclusive=False)
        self.run_sets()
        after = self.calls[-1][0]
        for name in ('destination', 'objects', 'tools'):
            self.assertEqual(after[name], before[name], name)

    def test_a_new_run_starts_the_release_again(self):
        self.run_sets()
        destination = Path(self.calls[0][0]['destination'])
        (destination / 'left-over').write_text('x')
        self.run_sets()
        self.assertFalse((destination / 'left-over').exists())

    def test_sets_with_local_changes_are_refused(self):
        node = dict(self.nodes['os:sets'], input_key=None)
        self.assertIn('commit the OS, Ports and SDK changes', profile_build.runnable(node))


class NinjaClientTests(ProfileFixture):
    def run_ninja(self, version):
        ninja = self.write('fake-ninja', f'#!{sys.executable}\n' + FAKE_NINJA, 0o755)
        log = self.root / 'ninja.log'
        os.environ.update(FAKE_VERSION=version, FAKE_LOG=str(log))
        self.addCleanup(os.environ.pop, 'FAKE_VERSION', None)
        self.addCleanup(os.environ.pop, 'FAKE_LOG', None)
        self.write('distfiles/byacc-1.tgz', '')
        self.write('distfiles/heimdal-7.tar.gz', '')
        llvm = self.write('llvm/llvm-source.json', '{}').parent
        plan = self.plan(selected_sources={'EMBER_LLVM_SOURCE': str(llvm)})
        status = profile_build.build(plan, work=self.root / 'work', command=['runner'], ninja=str(ninja), jobs=4)
        return status, log.read_text()

    def test_ninja_takes_the_budget_from_the_jobserver(self):
        status, log = self.run_ninja('1.13.2')
        self.assertEqual(status, 0)
        # No -j: a -j option would turn the jobserver off.
        self.assertEqual(log, f'-C {self.root / "work"} tokens=3')
        self.assertFalse((self.root / 'work/.jobserver').exists())

    def test_ninja_shares_the_outer_fifo(self):
        from emberbsd_sdk import jobserver
        with jobserver.serve(self.root / 'outer', 3) as flags:
            with jobserver.slots(flags, 2), mock.patch.dict(os.environ, MAKEFLAGS=flags):
                status, log = self.run_ninja('1.13.2')
                self.assertEqual((status, log), (0, f'-C {self.root / "work"} tokens=1'))
            with jobserver.slots(flags, 9) as slots:
                self.assertEqual(slots, 3)

    def test_older_ninja_is_refused(self):
        with self.assertRaisesRegex(Exception, 'older than Ninja 1.13'):
            self.run_ninja('1.11.1')


@unittest.skipUnless(NEW_NINJA, 'Ninja 1.13 or later is not installed')
class ProfileBuildTests(ProfileFixture):
    def setUp(self):
        super().setUp()
        for path, text in (('scripts/source-inputs/byacc/source.py', "PACKAGE = 'devel/byacc'\n" + PRODUCER),
                           ('scripts/source-inputs/heimdal/source.py', "PACKAGE = 'security/heimdal'\n" + PRODUCER)):
            self.write('ports/' + path, text)
        git(self.ports, 'commit', '-q', '-am', 'producers')
        self.log = self.root / 'log'
        self.write('os/ember/build-kernel.sh', KERNEL.replace('LOG', str(self.log)))
        git(self.os, 'add', '-A')
        git(self.os, 'commit', '-q', '-m', 'kernel builder')
        prefix = Path(self.native.bundle_root)
        (prefix / 'bin/mksh').symlink_to(shutil.which('sh'))
        self.write('prefix/bin/gpatch', '#!/bin/sh\n', 0o755)
        for name in ('byacc-1.tgz', 'heimdal-7.tar.gz'):
            self.write('distfiles/' + name, name)
        self.llvm = self.write('llvm/llvm-source.json', '{}').parent
        self.work = self.root / 'work'
        self.config = dict(host_prefix=str(prefix), os_source=str(self.os), ports_root=str(self.ports),
                           sdk_root=str(self.sdk), host_bfd_config=str(self.bfd), distfiles=str(self.root / 'distfiles'),
                           selected_sources={'EMBER_LLVM_SOURCE': str(self.llvm)}, jobs=3)
        runner = self.write('runner.py', '\n'.join([
            'import json, sys', f'sys.path.insert(0, {str(Path(profile_build.__file__).parents[1])!r})',
            'from emberbsd_sdk import profile_build',
            'arguments = sys.argv[1:]',
            f'config = json.load(open({str(self.root / "config.json")!r}))',
            'sys.exit(profile_build.run_node(sys.argv[1], arguments[arguments.index("--node") + 1],',
            '                                arguments[arguments.index("--key") + 1], config))', '']))
        self.write('config.json', json.dumps(self.config))
        self.command = [sys.executable, '-B', str(runner), str(self.work)]
        os.environ['TEST_LOG'] = str(self.log)
        self.addCleanup(os.environ.pop, 'TEST_LOG', None)

    def build(self):
        plan = self.plan(state=self.work,
                         selected_sources=self.config['selected_sources'])
        self.log.write_text('')
        with redirect_stderr(io.StringIO()):
            status = profile_build.build(plan, work=self.work, command=self.command, ninja=NINJA, jobs=2)
        return status, plan, self.log.read_text().splitlines()

    def test_build_runs_once_then_only_what_changed(self):
        status, plan, log = self.build()
        self.assertEqual(status, 0)
        prepared = self.work / 'sources'
        self.assertEqual(sorted(line for line in log if line.startswith('prepare')),
                         ['prepare byacc-1.tgz', 'prepare heimdal-7.tar.gz'])
        kernel = [line for line in log if line.startswith('kernel')]
        self.assertEqual(len(kernel), 1)
        self.assertIn(f'byacc={prepared}/EMBER_BYACC_SOURCE-', kernel[0])
        # The kernel runs alone: it gets its own slot and the one free token.
        self.assertIn(f'llvm={self.llvm} jobs=2', kernel[0])
        # The OS kernel builder takes the target of the profile.
        self.assertIn('target=aarch64', kernel[0])
        self.assertTrue((self.work / 'kernels/netbsd-EMBER64.img').is_file())
        # Nothing changed: the plan is ready and Ninja does not run.
        status, plan, log = self.build()
        self.assertEqual((status, log), (0, []))
        self.assertEqual({node['decision']['action'] for node in plan['nodes']}, {'ready'})
        # A new byacc recipe prepares byacc again and rebuilds the kernel, not Heimdal.
        self.write('ports/pkgsrc/devel/byacc/Makefile', 'DISTNAME= byacc-2\n')
        git(self.ports, 'commit', '-q', '-am', 'byacc 2')
        status, plan, log = self.build()
        self.assertEqual(status, 0)
        self.assertEqual([line.split()[0:2] for line in log], [['prepare', 'byacc-1.tgz'], ['kernel', 'EMBER64']])

    def test_a_kernel_configuration_outside_the_os_is_built_by_its_path(self):
        trials = self.root / 'trials'
        self.write('trials/EMBER64_TRIAL', 'include "arch/evbarm/conf/EMBER64"\n')
        self.config['kernel_configs'] = [str(trials)]
        self.write('config.json', json.dumps(self.config))
        profile = copy.deepcopy(PROFILE)
        profile['os']['kernels'] = ['EMBER64_TRIAL']
        plan = self.plan(profile, state=self.work, selected_sources=self.config['selected_sources'],
                         kernel_configs=[str(trials)])
        self.log.write_text('')
        with redirect_stderr(io.StringIO()):
            status = profile_build.build(plan, work=self.work, command=self.command, ninja=NINJA, jobs=2)
        self.assertEqual(status, 0)
        kernel = [line for line in self.log.read_text().splitlines() if line.startswith('kernel')]
        self.assertEqual([line.split()[1] for line in kernel], [str(trials / 'EMBER64_TRIAL')])
        self.assertTrue((self.work / 'kernels/netbsd-EMBER64_TRIAL.img').is_file())

    def test_a_missing_output_runs_just_its_producer_with_the_same_key(self):
        status, plan, _ = self.build()
        self.assertEqual(status, 0)
        node_id = 'os:kernel:EMBER64'
        key = self.nodes(plan)[node_id]['input_key']
        image = self.work / 'kernels/netbsd-EMBER64.img'
        image.unlink()
        self.assertTrue((self.work / profile_build.record_name(node_id)).is_file())
        status, plan, log = self.build()
        self.assertEqual(status, 0)
        self.assertEqual(self.nodes(plan)[node_id]['input_key'], key)
        self.assertEqual([line.split()[0] for line in log], ['kernel'])
        self.assertTrue(image.is_file())
        self.assertEqual(self.build()[2], [])

    def test_local_changes_run_the_kernel_on_each_build(self):
        self.write('os/sys/arch/evbarm/conf/EMBER64', 'include "arch/evbarm/conf/std"\n# local\n')
        for _ in range(2):
            status, _, log = self.build()
            self.assertEqual(status, 0)
            self.assertEqual(sum(line.startswith('kernel') for line in log), 1)

    def test_failed_producer_leaves_no_record_and_is_planned_again(self):
        failure = self.write('log.fail', '')
        status, _, _ = self.build()
        self.assertNotEqual(status, 0)
        failure.unlink()
        status, plan, log = self.build()
        self.assertEqual(status, 0)
        actions = {node['id']: node['decision']['action'] for node in plan['nodes']}
        self.assertEqual(actions['os:kernel:EMBER64'], 'build')
        self.assertEqual(actions['source:EMBER_BYACC_SOURCE'], 'ready')
        self.assertFalse(any(line.startswith('prepare') for line in log))

    def test_missing_distfile_fails_its_source_and_stops_the_consumers(self):
        (self.root / 'distfiles/heimdal-7.tar.gz').unlink()
        status, _, log = self.build()
        self.assertNotEqual(status, 0)
        # The producer of Heimdal fails; the kernel does not start.
        self.assertFalse(any(line.startswith('kernel') for line in log))
        self.assertFalse((self.work / profile_build.record_name('source:EMBER_HEIMDAL_SOURCE')).exists())

    def test_failed_rebuild_does_not_revive_the_old_result(self):
        self.assertEqual(self.build()[0], 0)
        profile = dict(PROFILE, os=dict(PROFILE['os'], buildinfo='EmberBSD:test-2'))
        failure = self.write('log.fail', '')
        # The builder removes the old outputs, then fails.
        self.write('os/ember/build-kernel.sh', KERNEL.replace('LOG', str(self.log)).replace(
            'set -eu\n', 'set -eu\nrm -f "$1/netbsd-$2" "$1/netbsd-$2.img"\n'))
        git(self.os, 'commit', '-q', '-am', 'builder removes outputs first')
        plan = self.plan(profile, state=self.work,
                         selected_sources=self.config['selected_sources'])
        with redirect_stderr(io.StringIO()):
            self.assertNotEqual(profile_build.build(plan, work=self.work, command=self.command, ninja=NINJA, jobs=2), 0)
        failure.unlink()
        status, plan, log = self.build()
        self.assertEqual(status, 0)
        self.assertEqual(sum(line.startswith('kernel') for line in log), 1)

    def test_paths_with_spaces_and_dollars_reach_the_runner(self):
        plan = self.plan(selected_sources=self.config['selected_sources'])
        text = profile_build.ninja_file(plan, command=['/a b/python', '$HOME'])
        self.assertIn("command = '/a b/python' '$$HOME' --node $id --key $key", text)


class BuilderSourcesTests(ProfileFixture):
    """build-sysroot.py and build-os-release.py prepare the sources of their build plan."""

    class Builder:
        @classmethod
        def prepared_source_profile(cls, options):
            return ['MKX11=no'], (('lib', frozenset(('dependall',))),)

    def setUp(self):
        super().setUp()
        for path, text in (('scripts/source-inputs/byacc/source.py', "PACKAGE = 'devel/byacc'\n" + PRODUCER),
                           ('scripts/source-inputs/heimdal/source.py', "PACKAGE = 'security/heimdal'\n" + PRODUCER),
                           ('pkgsrc/devel/editline/distinfo', 'SHA512 (libedit-1.tar.gz) = 00\n'
                                                              'Size (libedit-1.tar.gz) = 1 bytes\n')):
            self.write('ports/' + path, text)
        git(self.ports, 'add', '-A')
        git(self.ports, 'commit', '-q', '-m', 'producers')
        prefix = Path(self.native.bundle_root)
        (prefix / 'bin/mksh').symlink_to(shutil.which('sh'))
        self.write('prefix/bin/gpatch', '#!/bin/sh\n', 0o755)
        for name in ('byacc-1.tgz', 'heimdal-7.tar.gz', 'libedit-1.tar.gz'):
            self.write('distfiles/' + name, name)
        self.llvm = self.write('llvm/llvm-source.json', '{}').parent
        self.log = self.root / 'log'
        os.environ['TEST_LOG'] = str(self.log)
        self.addCleanup(os.environ.pop, 'TEST_LOG', None)
        workspace.create(str(self.root / 'sdk-work'), target='aarch64')

    def options(self, **changes):
        return {'sdk': str(self.root / 'sdk-work'), 'os': str(self.os), 'ports': str(self.ports),
                'sdk-source': str(self.sdk), 'host-prefix': self.native.bundle_root, 'target': 'aarch64',
                'llvm-source': str(self.llvm), 'distdir': str(self.root / 'distfiles'), 'jobs': 2} | changes

    def select(self, options):
        log = self.log

        def libedit(ports, archive, output, patch_tool):
            # The SDK libedit producer; its own tests cover the tree.
            os.makedirs(output)
            Path(output, 'libedit-source.json').write_text(os.path.basename(archive))
            with open(log, 'a') as stream:
                stream.write('prepare ' + os.path.basename(archive) + '\n')

        self.log.write_text('')
        with mock.patch('emberbsd_sdk.libedit.prepare_source', libedit):
            result = profile_build.builder_sources(options, self.Builder, required_sources=self.required_sources,
                                                   native=self.native)
        return result, self.log.read_text().splitlines()

    def test_the_plan_names_the_sources_and_the_build_prepares_them_once(self):
        prepared = self.root / 'sdk-work' / profile_build.BUILDER_SOURCES
        options = self.options(plan=True)
        result, log = self.select(options)
        self.assertEqual(sorted(result['prepare']), ['ports:pkgsrc-export', 'source:EMBER_BYACC_SOURCE',
                                                     'source:EMBER_HEIMDAL_SOURCE', 'source:EMBER_LIBEDIT_SOURCE'])
        self.assertEqual(log, [])
        self.assertFalse(prepared.exists())
        options = self.options()
        result, log = self.select(options)
        self.assertIsNone(result)
        self.assertEqual(sorted(log), ['prepare byacc-1.tgz', 'prepare heimdal-7.tar.gz', 'prepare libedit-1.tar.gz'])
        for name, manifest, archive in (('byacc-source', 'emberbsd-prepared-source.json', 'byacc-1.tgz'),
                                        ('heimdal-source', 'emberbsd-prepared-source.json', 'heimdal-7.tar.gz'),
                                        ('libedit-source', 'libedit-source.json', 'libedit-1.tar.gz')):
            path = Path(options[name])
            self.assertEqual(path.parent, prepared.resolve() / 'sources')
            self.assertEqual((path / manifest).read_text(), archive)
        self.assertEqual(options['llvm-source'], str(self.llvm))
        # The next build and its plan use the same trees and prepare nothing.
        for changes in ({}, {'plan': True}):
            again = self.options(**changes)
            self.assertEqual(self.select(again), (None, []))
            self.assertEqual(again['byacc-source'], options['byacc-source'])

    def test_a_missing_distdir_stops_before_the_producer(self):
        options = self.options()
        del options['distdir']
        with self.assertRaisesRegex(SDKError, '--distdir'):
            self.select(options)
        self.assertEqual(self.log.read_text(), '')

    def test_a_source_without_a_producer_is_refused_before_all_producers(self):
        # release.select names LLVM; no common producer prepares it.
        for changes in ({}, {'plan': True}):
            options = self.options(**changes)
            del options['llvm-source']
            with self.assertRaisesRegex(SDKError, r'EMBER_LLVM_SOURCE \(.*release.select.*\): select --llvm-source'):
                self.select(options)
        self.assertFalse((self.root / 'sdk-work' / profile_build.BUILDER_SOURCES).exists())

    def test_the_walk_has_the_flags_and_directories_of_the_build(self):
        walks = []
        original = self.required_sources
        self.required_sources = lambda traversals, flags: walks.append((traversals, flags)) or original(traversals, flags)
        cases = (({}, ['MKCXX=yes', 'MKLIBCXX=yes'], 'lib'),
                 ({'base-cxx': False}, ['MKCXX=yes', 'MKLIBCXX=no'], 'lib'),
                 ({'c-foundation': True}, ['MKCXX=no', 'MKLIBCXX=no'], None))
        for changes, cxx, library in cases:
            with self.subTest(changes=changes):
                walks.clear()
                self.Builder.FLAGS = ('MKCXX=yes', 'MKLIBCXX=no')
                self.select(self.options(plan=True, **changes))
                traversals, flags = walks[0]
                self.assertEqual([flag for flag in flags if flag.startswith('MK') and 'CXX' in flag], cxx)
                self.assertEqual([path for path, _ in traversals], ['tools'] + ([library] if library else []))
        # GCC keeps the flags of its builder.
        walks.clear()
        self.select(self.options(plan=True, **{'compiler-family': 'gcc'}))
        self.assertNotIn('MKCXX=yes', walks[0][1])

    def test_the_sdk_must_be_a_workspace_of_the_target(self):
        with self.assertRaisesRegex(SDKError, '--sdk is not a Prebuild SDK workspace for aarch64'):
            self.select(self.options(sdk=str(self.root / 'other')))


if __name__ == '__main__':
    unittest.main()
