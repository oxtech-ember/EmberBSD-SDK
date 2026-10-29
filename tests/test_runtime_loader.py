# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD; AI-assisted.
"""A runtime view is scoped to the selected Darwin child process."""
from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk import runtime_loader


class RuntimeLoaderTests(unittest.TestCase):
    def test_scoped_environment_and_exact_argv(self):
        with tempfile.TemporaryDirectory(prefix='runtime view ') as view:
            arguments = ['--runtime-view', view, '--', '/path with space/tool',
                         'argument with space', '--literal', '']
            before = dict(os.environ)
            with patch.object(runtime_loader.sys, 'platform', 'darwin'), \
                 patch.object(runtime_loader.os, 'execvpe') as execute:
                runtime_loader.main(arguments)
            program, argv, environment = execute.call_args.args
            self.assertEqual(program, '/path with space/tool')
            self.assertEqual(argv, ['/path with space/tool', 'argument with space',
                                    '--literal', ''])
            self.assertEqual(environment['DYLD_LIBRARY_PATH'], view)
            self.assertEqual(dict(os.environ), before)
            self.assertEqual({k: v for k, v in environment.items()
                              if k != 'DYLD_LIBRARY_PATH'},
                             {k: v for k, v in before.items()
                              if k != 'DYLD_LIBRARY_PATH'})

    def test_print_inputs_is_independent_and_exact(self):
        script = Path(__file__).resolve().parents[1]/'scripts/run-darwin-with-runtime.py'
        result = subprocess.run([sys.executable, '-B', str(script), '--print-inputs'],
                                text=True, capture_output=True, check=True)
        inputs = json.loads(result.stdout)
        self.assertEqual(inputs, [str(script.resolve()), str(Path(runtime_loader.__file__).resolve())])
        self.assertTrue(all(Path(path).is_file() and Path(path).is_absolute() for path in inputs))
        with patch.object(runtime_loader.os, 'execvpe') as execute, \
             redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            runtime_loader.main(['--print-inputs', '--runtime-view', '/tmp'])
        execute.assert_not_called()

    def test_rejects_ambiguous_or_missing_inputs(self):
        for platform, arguments in [('darwin', ['--runtime-view', 'relative', '--', 'tool']),
                                    ('darwin', ['--runtime-view', '/missing', '--', 'tool']),
                                    ('darwin', ['--runtime-view', '/tmp:other', '--', 'tool']),
                                    ('darwin', ['--runtime-view', '/tmp', 'tool']),
                                    ('linux', ['--runtime-view', '/tmp', '--', 'tool'])]:
            with self.subTest(arguments=arguments), \
                 patch.object(runtime_loader.sys, 'platform', platform), \
                 redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                runtime_loader.main(arguments)

    @unittest.skipUnless(sys.platform == 'darwin', 'Darwin loader environment')
    def test_exec_preserves_stdio_exit_and_signal(self):
        script = Path(__file__).resolve().parents[1]/'scripts/run-darwin-with-runtime.py'
        with tempfile.TemporaryDirectory(prefix='runtime view ') as view:
            env = dict(os.environ, DYLD_LIBRARY_PATH='parent-value')
            code = ('import json,os,sys; print(json.dumps([os.environ["DYLD_LIBRARY_PATH"],'
                    'sys.argv[1:],sys.stdin.read()])); sys.stderr.write("child-error"); sys.exit(37)')
            args = [sys.executable, '-B', str(script), '--runtime-view', view, '--',
                    sys.executable, '-c', code, 'arg with space', '']
            result = subprocess.run(args, input='input', text=True, capture_output=True, env=env)
            self.assertEqual(result.returncode, 37, result.stderr)
            self.assertEqual(result.stderr, 'child-error')
            self.assertEqual(json.loads(result.stdout), [view, ['arg with space', ''], 'input'])
            signal_code = 'import os,signal; os.kill(os.getpid(),signal.SIGTERM)'
            result = subprocess.run(args[:8]+[signal_code], capture_output=True, env=env)
            self.assertEqual(result.returncode, -signal.SIGTERM)


if __name__ == '__main__':
    unittest.main()
