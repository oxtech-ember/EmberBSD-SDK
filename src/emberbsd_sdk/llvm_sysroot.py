# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""First-base LLVM ordering, using the common OS and Ports producers."""

import hashlib
import json
import os

from . import inventory, jsonio, llvm_runtime, llvm_stage_inputs, paths, prepared_sources, process, workspace
from .errors import SDKError
from .elf import read_archive, read_elf_header
from .sysroot_builder import SysrootBuilder
from .verification_receipts import RunReceipts
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
        flags += ['EMBER_LIBUNWIND_ARCHIVE=' + (selected.get('llvm-libc-unwind', selected['llvm-unwind'])
                                               if phase == 'final' else selected['llvm-unwind']),
                  'EMBER_LLVM_RUNTIME_PHASE=' + ('full' if phase == 'final' else 'bootstrap'),
                  'EMBER_EXTERNAL_LLVM_RUNTIME=' + selected['runtime-prefix'],
                  'EMBER_LLVM_RESOURCE_DIR=' + selected['llvm-resource']]
    return flags


class LLVMPhaseBuilder(SysrootBuilder):
    """Use the ordinary locked builder for the two incomplete OS inputs."""
    def __init__(self, options, phase, *, receipts=None, select_only=False, resume=False,
                 stage_inputs=None):
        if phase not in ('headers', 'base'):
            raise SDKError('unsupported LLVM OS bootstrap phase')
        super().__init__(options, receipts=receipts)
        self.llvm_phase = phase
        # Selects the stage key from the inputs; its destination is not used.
        self.select_only = select_only
        self.resume = resume
        # The stage set's recorded OS inputs, when it has an inputs.json.
        self.stage_inputs = stage_inputs

    def empty_destination(self):
        if self.select_only:
            return
        destination = self.paths['destination']
        if paths.entry(destination) is not None and os.listdir(destination):
            saved = jsonio.read_json(os.path.join(destination, self.RECEIPT))
            if self.failed_attempt(saved):
                # Preflight accepts it; the locked build moves it aside and restarts.
                if self.locks:
                    set_aside(os.path.dirname(destination), [destination])
                    super().empty_destination()
                return
            if (paths.entry(os.path.join(destination, self.MARKER)) is not None or
                    saved.get('format') != 'emberbsd-llvm-os-stage' or
                    saved.get('qualification') != 'built-and-inspected' or
                    saved.get('phase') != self.llvm_phase):
                raise SDKError('incomplete or unrelated LLVM OS stage; leave it unchanged')
            self.completed = saved
            return
        super().empty_destination()

    def failed_attempt(self, saved):
        # Only a stage built with these objects: holding their lock proves
        # that no other build is still writing it.
        return (self.resume and paths.entry(os.path.join(self.paths['destination'], self.MARKER)) is not None and
                saved.get('format') == 'emberbsd-llvm-os-stage' and saved.get('phase') == self.llvm_phase and
                saved.get('qualification') == 'incomplete' and
                saved.get('paths', {}).get('objects') == self.paths['objects'])

    def prepared_source_traversals(self):
        # bootstrap-headers enters only header directories; base is "tools libs".
        if self.llvm_phase == 'headers' or self.options.get('c-foundation'):
            return prepared_sources.TOOLS_TRAVERSALS
        return super().prepared_source_traversals()

    def preflight(self):
        result = super().preflight()
        result.update(format='emberbsd-llvm-os-stage', phase=self.llvm_phase,
                      scope='llvm-' + self.llvm_phase)
        result.pop('copy', None)
        if self.llvm_phase == 'headers':
            self.build_command = self.build_command[:-2] + ['tools', 'bootstrap-headers']
            self.phase_commands = [self.build_command]
        elif self.options.get('c-foundation'):
            self.build_command = self.build_command[:-2] + ['tools', 'bootstrap-c']
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
            if not self.options.get('c-foundation'):
                return super().inspect_payload()
            root = self.paths['destination']
            for name in ('crt0.o', 'crti.o', 'crtbegin.o', 'crtend.o', 'crtn.o'):
                path = inventory.required_file(root, 'usr/lib/' + name)
                if read_elf_header(path, self.target['elf'], 1,
                                   riscv32_soft_float=self.options.get('target') == 'riscv32') is None:
                    raise SDKError('required ELF CRT missing: ' + name)
            archive = inventory.required_file(root, 'usr/lib/libc.a')
            if not read_archive(archive, self.target['elf'],
                    riscv32_soft_float=self.options.get('target') == 'riscv32')['elf_members']:
                raise SDKError('empty C foundation libc archive')
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
                # Returning a completed stage does not rebuild, so the OS files
                # it read must still be identical. Ports recipes are checked in
                # preflight.
                self.require_unchanged_inputs()
                return self.completed
        self.require_runtime_work()
        return self._run_preflighted()

    def require_unchanged_inputs(self):
        root = self.sources['os']['path']
        built = self.completed['sources']['os']['commit']
        current = self.sources['os']['commit']
        if self.stage_inputs is None:
            # A stage without an input record compares the whole OS tree.
            try:
                self.git(root, 'diff', '--quiet', built, current, '--')
            except SDKError as exc:
                raise SDKError('LLVM OS source tree changed; build a new stage with compatible objects') from exc
            return
        directories = self.stage_inputs['directories']
        if (self.stage_inputs['os_commit'] != built or
                llvm_stage_inputs.inputs_digest(llvm_stage_inputs.tree_rows(self.git, root, built), directories) !=
                llvm_stage_inputs.inputs_digest(llvm_stage_inputs.tree_rows(self.git, root, current), directories)):
            raise SDKError('LLVM stage OS inputs changed; build a new stage with compatible objects')


def set_aside(root, outputs):
    """Keep a failed attempt's outputs for inspection under ROOT/failed-N."""
    number = 1
    while paths.entry(os.path.join(root, f'failed-{number}')) is not None:
        number += 1
    failed = os.path.join(root, f'failed-{number}')
    os.mkdir(failed)
    for output in outputs:
        if paths.entry(output) is not None:
            os.rename(output, os.path.join(failed, os.path.basename(output)))


class LLVMCoordinator:
    """Headers -> PIC unwind/builtins -> libc/CRT -> C++ runtimes -> final SDK."""
    def __init__(self, options, builder_type, *, receipts=None):
        self.options = options.copy()
        self.builder_type = builder_type
        # All phases of this run share the receipts; a new run starts empty.
        self.receipts = RunReceipts() if receipts is None else receipts
        self.work = os.path.abspath(self.options['runtime-work'])
        # Without an explicit --llvm-stages, stages are keyed by their content
        # inputs, so an OS change selects a new stage instead of refusing the old one.
        self.keyed = 'llvm-stages' not in self.options
        stem = 'llvm-c-foundation-' if self.options.get('c-foundation') else 'llvm-bootstrap-'
        self.stage_root = self.options.get('llvm-stages', os.path.join(self.options['sdk'], 'stages',
                                      stem + self.options['target']))
        self.runtime_root = self.work
        self.stage_inputs = None
        # Set by prepare(): whether this run found the tools directory new.
        self.tools_new = False
        # Only SDK-named stages restart; explicit stage directories are left as they are.
        self.resume = self.keyed and builder_type.RESUMES_FAILED_STAGES
        self.object_root = self.options.get('llvm-objects', os.path.join(
            self.options['sdk'], 'obj', 'llvm-c-foundation' if self.options.get('c-foundation') else 'llvm-bootstrap', self.options['target']))
        self.tools = self.options.get('tools', os.path.join(self.object_root, 'tools'))

    def stage_options(self, phase):
        options = self.options | {'destination': os.path.join(self.stage_root, phase),
                                  'objects': os.path.join(self.object_root, phase),
                                  'tools': self.tools}
        options.pop('llvm-runtime-stage', None)
        if phase != 'final':
            options['base-cxx'] = False
        if phase == 'base':
            options['llvm-runtime-stage'] = os.path.join(self.runtime_root, 'bootstrap-stage')
            options['llvm-runtime-base'] = os.path.join(self.stage_root, 'headers')
        return options

    def prepare(self):
        layout = workspace.inspect_layout(self.options['sdk'])
        workspace.require_target(layout, self.options['target'])
        if self.keyed:
            selector = LLVMPhaseBuilder(self.stage_options('headers'), 'headers',
                                        receipts=self.receipts, select_only=True)
            selector.preflight()
            key = self.matching_stage(selector) or self.stage_key(selector)
            self.stage_root = os.path.join(self.stage_root, key)
            self.runtime_root = os.path.join(self.work, key)
        record = os.path.join(self.stage_root, llvm_stage_inputs.RECORD)
        if paths.entry(record) is not None:
            self.stage_inputs = llvm_stage_inputs.read_record(record)
            if self.stage_inputs['target'] != self.options['target']:
                raise SDKError(f'LLVM stage input record is for another target: {record}')
        self.headers = LLVMPhaseBuilder(self.stage_options('headers'), 'headers', receipts=self.receipts,
                                        resume=self.resume, stage_inputs=self.stage_inputs)
        self.headers.preflight()
        self.require_prepared_sources()
        self.headers.output_path(self.object_root, os.path.join(self.headers.paths['sdk'], 'obj'))
        self.headers.output_path(self.stage_root, os.path.join(self.headers.paths['sdk'], 'stages'))
        self.work = self.headers.output_path(self.work, os.path.join(self.headers.paths['sdk'], 'obj'))
        self.runtime_root = self.headers.output_path(self.runtime_root, os.path.join(self.headers.paths['sdk'], 'obj'))
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
        # The OS phases build the shared tools first; a fresh release needs
        # them new at the start of the run. Its objects are checked by the
        # final builder.
        self.tools_new = paths.entry(self.tools) is None or not os.listdir(self.tools)
        if self.options.get('object-mode') == 'fresh' and not self.tools_new:
            raise SDKError('fresh object mode needs a new or empty tools directory: ' + self.tools)

    def require_prepared_sources(self):
        """Refuse, before the first phase, sources a later phase will need.

        The base phase builds "tools libs" with the headers phase's flags;
        the final builder uses its own profile with the final LLVM knobs."""
        headers = self.headers
        if self.options.get('c-foundation'):
            headers.require_prepared_sources(headers.flags, prepared_sources.TOOLS_TRAVERSALS)
            return
        headers.require_prepared_sources(headers.flags, SysrootBuilder.prepared_source_profile(self.options)[1])
        flags, traversals = self.builder_type.prepared_source_profile(self.options)
        final_cxx = ['MKCXX=yes', 'MKLIBCXX=' + ('yes' if self.options.get('base-cxx', False) else 'no')]
        replaced = {flag.split('=', 1)[0] for flag in [*SysrootBuilder.FLAGS, *flags, *final_cxx]}
        selected = [flag for flag in headers.flags if flag.split('=', 1)[0] not in replaced]
        final_keys = {flag.split('=', 1)[0] for flag in final_cxx}
        flags = [flag for flag in flags if flag.split('=', 1)[0] not in final_keys]
        headers.require_prepared_sources(flags + final_cxx + selected, traversals)

    def stage_key(self, selected):
        """Name a stage set by its OS tree, LLVM source and compiler.

        The OS tree, not the commit, matches the completed-stage check; the
        other inputs are still checked against each stage's receipt."""
        source = selected.sources['os']
        material = dict(format='emberbsd-llvm-stage-key', version=1, target=self.options['target'],
                        os_tree=selected.git(source['path'], 'rev-parse', source['commit'] + '^{tree}'),
                        llvm_source=selected.llvm_source_record, compiler=selected.compiler_record)
        encoded = json.dumps(material, sort_keys=True, separators=(',', ':')).encode('utf-8')
        return 'os-' + hashlib.sha256(encoded).hexdigest()[:20]

    def matching_stage(self, selected):
        """Return the name of a completed stage set whose recorded inputs match.

        A stage set records the OS files its build read (inputs.json). It is
        reused when the Git ids of those files, the LLVM source and the
        compiler are unchanged at the selected OS commit, so an OS change that
        the stage never reads, such as usr.bin/config, keeps the stage."""
        if paths.entry(self.stage_root) is None:
            return None
        source = selected.sources['os']
        rows = None
        for name in sorted(os.listdir(self.stage_root)):
            record = os.path.join(self.stage_root, name, llvm_stage_inputs.RECORD)
            if paths.entry(record) is None:
                continue
            record = llvm_stage_inputs.read_record(record)
            if record['target'] != self.options['target']:
                continue
            if rows is None:
                rows = llvm_stage_inputs.tree_rows(selected.git, source['path'], source['commit'])
            digest = llvm_stage_inputs.inputs_digest(rows, record['directories'])
            if llvm_stage_inputs.stage_key(self.options['target'], selected.llvm_source_record,
                                           selected.compiler_record, digest) == record['key']:
                return name
        return None

    def record_inputs(self, receipts):
        """Write the stage set's input record once both OS phases completed."""
        if self.stage_inputs is not None:
            return
        record = llvm_stage_inputs.make_record(receipts, self.headers.git, self.options['target'])
        llvm_stage_inputs.write_record(os.path.join(self.stage_root, llvm_stage_inputs.RECORD), record)
        self.stage_inputs = record

    def lock(self):
        return process.ContextLocks([self.work], os.path.join(self.headers.paths['sdk'], 'obj'))

    def runtime_command(self, phase, base, *, verify=False):
        selected = self.headers
        stage = os.path.join(self.runtime_root, phase + '-stage')
        command = [selected.paths['python'], '-B', os.path.join(selected.paths['ports'], PRODUCER),
                   '--sdk-source', selected.paths['sdk-source']]
        if verify:
            command += ['verify', '--stage', stage, '--target', self.options['target'],
                        '--phase', phase, '--git', selected.git_command, '--base', base]
            if self.options.get('llvm-runtime-original-source'):
                command += ['--source', selected.paths['llvm-source'], '--original-source',
                            paths.canonical_existing(self.options['llvm-runtime-original-source'])]
            return command
        command += ['build', '--source', selected.paths['llvm-source'],
                    '--target', self.options['target'], '--phase', phase,
                    '--sysroot', base, '--os-source', selected.paths['os'],
                    '--host-prefix', selected.paths['host-prefix'],
                    '--host-compiler-family', self.options.get('host-compiler-family', 'clang'),
                    '--target-prefix', selected.paths['compiler'], '--git', selected.git_command,
                    '--jobs', self.options['jobs'],
                    '--work', os.path.join(self.runtime_root, phase + '-work'), '--stage', stage]
        if phase == 'full':
            command += ['--bootstrap-stage', os.path.join(self.runtime_root, 'bootstrap-stage')]
        return command

    def runtime(self, phase, base):
        selected = self.headers
        stage = os.path.join(self.runtime_root, phase + '-stage')
        # The producer writes runtime.json last and removes it on failure.
        if (self.resume and paths.entry(stage) is not None and
                paths.entry(os.path.join(stage, 'runtime.json')) is None):
            set_aside(self.runtime_root, [os.path.join(self.runtime_root, phase + suffix)
                                          for suffix in ('-stage', '-work', '.json', '.log')])
        if paths.entry(stage) is None:
            selected.mkdirs(self.runtime_root, os.path.join(selected.paths['sdk'], 'obj'))
            selected.execute_external(self.runtime_command(phase, base),
                os.path.join(self.runtime_root, phase + '.json'), os.path.join(self.runtime_root, phase + '.log'))
        result = process.capture(self.runtime_command(phase, base, verify=True), env=selected.environment)
        record = jsonio.loads_json(result.stdout)
        llvm_runtime.validate_identity(record, target=self.options['target'])
        recipe_check = ({} if self.options.get('llvm-runtime-original-source') else
                        dict(ports=selected.paths['ports'], ports_revision=record['recipe']['ports_revision']))
        expected = llvm_runtime.validate_stage(stage, target=self.options['target'],
            required_components=('builtins', 'libunwind-static') if phase == 'bootstrap' else llvm_runtime.FULL_COMPONENTS,
            base=base, content=False, **recipe_check)
        if record != expected:
            raise SDKError('LLVM runtime producer and shared validator disagree')

    def run(self):
        self.prepare()
        if self.options.get('plan'):
            phases = [
                self.headers.receipt,
                {'phase': 'bootstrap', 'command': self.runtime_command('bootstrap', os.path.join(self.stage_root, 'headers'))},
                {'phase': 'base', 'inputs': ['headers', 'bootstrap']}]
            if self.options.get('c-foundation'):
                return dict(format='emberbsd-llvm-build-plan', version=1, phases=phases)
            phases += [{'phase': 'full', 'command': self.runtime_command('full', os.path.join(self.stage_root, 'base'))},
                {'phase': 'final', 'inputs': ['full'], 'destination': self.options.get('destination')}]
            return dict(format='emberbsd-llvm-build-plan', version=1, phases=phases)
        with self.lock():
            headers = LLVMPhaseBuilder(self.stage_options('headers'), 'headers', receipts=self.receipts,
                                       resume=self.resume, stage_inputs=self.stage_inputs).run()
            self.runtime('bootstrap', os.path.join(self.stage_root, 'headers'))
            base = LLVMPhaseBuilder(self.stage_options('base'), 'base', receipts=self.receipts,
                                    resume=self.resume, stage_inputs=self.stage_inputs).run()
            self.record_inputs(dict(headers=headers, base=base))
            if self.options.get('c-foundation'):
                return dict(format='emberbsd-llvm-c-foundation', version=1,
                            scope='c-foundation', target=self.options['target'],
                            target_execution='not-run', headers=headers, base=base,
                            bootstrap_runtime=os.path.join(self.runtime_root, 'bootstrap-stage'))
            self.runtime('full', os.path.join(self.stage_root, 'base'))
            final_options = self.options | {'llvm-runtime-stage': os.path.join(self.runtime_root, 'full-stage'),
                                            'llvm-runtime-base': os.path.join(self.stage_root, 'base'),
                                            'llvm-bootstrap-stage': os.path.join(self.runtime_root, 'bootstrap-stage'),
                                            'tools': self.tools}
            final = self.builder_type(final_options, receipts=self.receipts)
            if self.tools_new:
                final.tools_new_in_run = True
            return final.run()
