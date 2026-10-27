# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""First-base LLVM ordering, using the common OS and Ports producers."""

import os
import tempfile

from . import inventory, jsonio, llvm_runtime, paths, process, workspace
from .errors import SDKError
from .sysroot_builder import SysrootBuilder
from .llvm_runtime import runtime_paths

PRODUCER = 'pkgsrc/lang/compiler-rt/files/runtime.py'


def llvm_flags(selected, *, phase, base_cxx=False):
    if phase not in ('headers', 'base', 'final'):
        raise SDKError('unsupported LLVM OS phase')
    if type(base_cxx) is not bool:
        raise SDKError('base C++ selection must be boolean')
    flags = ['EMBER_COMPILER_FAMILY=clang', 'EXTERNAL_TOOLCHAIN=' + selected['compiler'],
             'EMBER_LLVM_SOURCE=' + selected['llvm-source'],
             'MKCXX=' + ('yes' if phase == 'final' else 'no'),
             'MKLIBCXX=' + ('yes' if phase == 'final' and base_cxx else 'no')]
    if phase != 'headers':
        flags += ['EMBER_LIBUNWIND_ARCHIVE=' + selected['llvm-unwind'],
                  'EMBER_LLVM_RUNTIME_PHASE=' + ('full' if phase == 'final' else 'bootstrap'),
                  'EMBER_EXTERNAL_LLVM_RUNTIME=' + selected['runtime-prefix'],
                  'EMBER_LLVM_RESOURCE_DIR=' + selected['llvm-resource']]
    return flags


class LLVMPhaseBuilder(SysrootBuilder):
    """Use the ordinary locked builder for the two incomplete OS inputs."""
    def __init__(self, options, phase):
        if phase not in ('headers', 'base'):
            raise SDKError('unsupported LLVM OS bootstrap phase')
        super().__init__(options)
        self.llvm_phase = phase

    def empty_destination(self):
        destination = self.paths['destination']
        if paths.entry(destination) is not None and os.listdir(destination):
            saved = jsonio.read_json(os.path.join(destination, self.RECEIPT))
            if (paths.entry(os.path.join(destination, self.MARKER)) is not None or
                    saved.get('format') != 'emberbsd-llvm-os-stage' or
                    saved.get('qualification') != 'built-and-inspected' or
                    saved.get('phase') != self.llvm_phase):
                raise SDKError('incomplete or unrelated LLVM OS stage; leave it unchanged')
            self.completed = saved
            return
        super().empty_destination()

    def preflight(self):
        result = super().preflight()
        result.update(format='emberbsd-llvm-os-stage', phase=self.llvm_phase,
                      scope='llvm-' + self.llvm_phase)
        result.pop('copy', None)
        if self.llvm_phase == 'headers':
            self.build_command = self.build_command[:-2] + ['tools', 'bootstrap-headers']
            self.phase_commands = [self.build_command]
        else:
            self.phase_commands = [self.build_command, self.loader_command, self.install_command]
        result['plan'] = self.phase_commands
        return result

    def build_payload(self):
        for command in self.phase_commands:
            self.dispatch(command)

    def inspect_payload(self):
        if self.llvm_phase == 'base':
            if self.verify_runtime(base=self.paths['destination']) != self.runtime_record:
                raise SDKError('bootstrap LLVM runtime changed during OS base build')
            return super().inspect_payload()
        for name in ('stdio.h', 'sys/types.h', 'sys/param.h', 'stdint.h', 'limits.h', 'math.h', 'pthread.h'):
            inventory.required_file(self.paths['destination'], 'usr/include/' + name)
        return inventory.snapshot_tree(self.paths['destination'],
            exclude=(self.RECEIPT, self.MARKER, self.LOG))

    def run(self):
        self.preflight()
        if self.options.get('plan'):
            return self.receipt
        if hasattr(self, 'completed'):
            with process.ContextLocks([self.paths[name] for name in ('objects', 'tools')],
                                      os.path.join(self.paths['sdk'], 'obj')):
                if (not self.compatible_context(self.completed, self.paths['objects']) or
                        self.completed['outputs'] != self.inspect_payload()):
                    raise SDKError('LLVM OS stage inputs or outputs changed; select new stage objects')
                self.validate_source_ancestry(self.completed, self.paths['destination'])
                # Object reuse permits incremental rebuilding after source changes.
                # Returning a completed stage does not rebuild, so its OS tree
                # must still be identical. Ports recipes are checked in preflight.
                try:
                    self.git(self.sources['os']['path'], 'diff', '--quiet',
                             self.completed['sources']['os']['commit'],
                             self.sources['os']['commit'], '--')
                except SDKError as exc:
                    raise SDKError('LLVM OS source tree changed; build a new stage with compatible objects') from exc
                return self.completed
        return super().run()


class LLVMCoordinator:
    """Headers -> PIC unwind/builtins -> libc/CRT -> C++ runtimes -> final SDK."""
    def __init__(self, options, builder_type):
        self.options = options.copy()
        self.builder_type = builder_type
        self.work = os.path.abspath(self.options['runtime-work'])
        self.stage_root = os.path.join(self.options['sdk'], 'stages',
                                      'llvm-bootstrap-' + self.options['target'])
        self.object_root = os.path.join(self.options['sdk'], 'obj', 'llvm-bootstrap', self.options['target'])
        self.tools = self.options.get('tools', os.path.join(self.object_root, 'tools'))

    def stage_options(self, phase):
        options = self.options | {'destination': os.path.join(self.stage_root, phase),
                                  'objects': os.path.join(self.object_root, phase),
                                  'tools': self.tools}
        options.pop('llvm-runtime-stage', None)
        if phase != 'final':
            options['base-cxx'] = False
        if phase == 'base':
            options['llvm-runtime-stage'] = os.path.join(self.work, 'bootstrap-stage')
            options['llvm-runtime-base'] = os.path.join(self.stage_root, 'headers')
        return options

    def prepare(self):
        layout = workspace.inspect_layout(self.options['sdk'])
        workspace.require_target(layout, self.options['target'])
        self.headers = LLVMPhaseBuilder(self.stage_options('headers'), 'headers')
        self.headers.preflight()
        self.work = self.headers.output_path(self.work, os.path.join(self.headers.paths['sdk'], 'obj'))
        for name, value in self.headers.paths.items():
            if name != 'sdk':
                paths.reject_overlap(self.work, value)
        # Refuse an already used final destination before building its prerequisites.
        final = self.headers.output_path(self.options.get('destination',
            os.path.join(self.options['sdk'], 'sysroot')), self.headers.paths['sdk'])
        if paths.inside(final, os.path.join(self.headers.paths['sdk'], 'sysroot')):
            raise SDKError('destination must not be below the active sysroot; use SDK/stages/NAME')
        paths.reject_overlap(final, self.work)
        paths.reject_overlap(final, self.stage_root)
        if paths.entry(final) is not None and os.listdir(final):
            raise SDKError('destination must be new or empty; active sysroot is never overwritten')

    def lock(self):
        return process.ContextLocks([self.work], os.path.join(self.headers.paths['sdk'], 'obj'))

    def runtime_command(self, phase, base, *, verify=False):
        selected = self.headers
        stage = os.path.join(self.work, phase + '-stage')
        command = [selected.paths['python'], '-B', os.path.join(selected.paths['ports'], PRODUCER),
                   '--sdk-source', selected.paths['sdk-source']]
        if verify:
            return command + ['verify', '--stage', stage, '--target', self.options['target'],
                              '--phase', phase, '--git', selected.git_command, '--base', base]
        command += ['build', '--source', selected.paths['llvm-source'],
                    '--target', self.options['target'], '--phase', phase,
                    '--sysroot', base, '--os-source', selected.paths['os'],
                    '--host-prefix', selected.paths['host-prefix'],
                    '--host-compiler-family', self.options.get('host-compiler-family', 'clang'),
                    '--target-prefix', selected.paths['compiler'], '--git', selected.git_command,
                    '--jobs', self.options['jobs'],
                    '--work', os.path.join(self.work, phase + '-work'), '--stage', stage]
        if phase == 'full':
            command += ['--bootstrap-stage', os.path.join(self.work, 'bootstrap-stage')]
        return command

    def reuse_bootstrap(self, phase, base, stage):
        """Let the common producer restage compatible bytes; retain their origin."""
        selected = self.headers
        original_hash = inventory.sha256_file(os.path.join(stage, 'runtime.json'))
        retained = stage + '-before-reuse-' + original_hash
        if paths.entry(retained) is not None:
            raise SDKError('previous runtime reuse destination already exists: ' + retained)
        with tempfile.TemporaryDirectory(prefix='.bootstrap-reuse-', dir=self.work) as temporary:
            candidate = os.path.join(temporary, 'stage')
            command = [selected.paths['python'], '-B', os.path.join(selected.paths['ports'], PRODUCER),
                '--sdk-source', selected.paths['sdk-source'], 'reuse',
                '--source', selected.paths['llvm-source'], '--stage', stage,
                '--work', os.path.join(self.work, phase + '-work'), '--destination', candidate,
                '--sysroot', base, '--target', self.options['target'], '--phase', phase,
                '--host-prefix', selected.paths['host-prefix'],
                '--host-compiler-family', self.options.get('host-compiler-family', 'clang'),
                '--target-prefix', selected.paths['compiler'], '--git', selected.git_command]
            selected.execute_external(command, os.path.join(self.work, phase + '-reuse.json'),
                                      os.path.join(self.work, phase + '-reuse.log'))
            record = llvm_runtime.validate_stage(candidate, target=self.options['target'],
                required_components=('builtins', 'libunwind-static'), base=base)
            if (record.get('reuse', {}).get('previous_manifest_sha256') != original_hash or
                    inventory.sha256_file(os.path.join(stage, 'runtime.json')) != original_hash):
                raise SDKError('runtime reuse does not retain its previous manifest')
            os.rename(stage, retained)
            try:
                os.rename(candidate, stage)
            except BaseException:
                os.rename(retained, stage)
                raise

    def runtime(self, phase, base):
        selected = self.headers
        stage = os.path.join(self.work, phase + '-stage')
        if paths.entry(stage) is None:
            selected.mkdirs(self.work, os.path.join(selected.paths['sdk'], 'obj'))
            selected.execute_external(self.runtime_command(phase, base),
                os.path.join(self.work, phase + '.json'), os.path.join(self.work, phase + '.log'))
        try:
            result = process.capture(self.runtime_command(phase, base, verify=True), env=selected.environment)
        except SDKError:
            if phase != 'bootstrap':
                raise
            # The owner independently checks bytes, source/recipe history,
            # actual dependency closure and configure inputs before restaging.
            self.reuse_bootstrap(phase, base, stage)
            result = process.capture(self.runtime_command(phase, base, verify=True), env=selected.environment)
        record = jsonio.loads_json(result.stdout)
        llvm_runtime.validate_identity(record, target=self.options['target'])
        expected = llvm_runtime.validate_stage(stage, target=self.options['target'],
            required_components=('builtins', 'libunwind-static') if phase == 'bootstrap' else llvm_runtime.FULL_COMPONENTS,
            ports=selected.paths['ports'], ports_revision=record['recipe']['ports_revision'], base=base)
        if record != expected:
            raise SDKError('LLVM runtime producer and shared validator disagree')

    def run(self):
        self.prepare()
        if self.options.get('plan'):
            return dict(format='emberbsd-llvm-build-plan', version=1, phases=[
                self.headers.receipt,
                {'phase': 'bootstrap', 'command': self.runtime_command('bootstrap', os.path.join(self.stage_root, 'headers'))},
                {'phase': 'base', 'inputs': ['headers', 'bootstrap']},
                {'phase': 'full', 'command': self.runtime_command('full', os.path.join(self.stage_root, 'base'))},
                {'phase': 'final', 'inputs': ['full'], 'destination': self.options.get('destination')}])
        with self.lock():
            LLVMPhaseBuilder(self.stage_options('headers'), 'headers').run()
            self.runtime('bootstrap', os.path.join(self.stage_root, 'headers'))
            LLVMPhaseBuilder(self.stage_options('base'), 'base').run()
            self.runtime('full', os.path.join(self.stage_root, 'base'))
            final_options = self.options | {'llvm-runtime-stage': os.path.join(self.work, 'full-stage'),
                                            'tools': self.tools}
            return self.builder_type(final_options).run()
