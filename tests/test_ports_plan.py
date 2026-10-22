# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Ports planning and resumable evidence contracts. No package builds."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
SDK_SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SDK_SOURCE / 'src'))
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.jsonio import json_bytes, loads_json, read_json
from emberbsd_sdk.ports_plan import lock_graph, main, make_plan, plan_status, record_result, validate_lock, validate_plan


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def graph():
    def node(name, deps=()):
        return dict(id=name, version='1.0', input_sha256=digest(name),
                    dependencies=[dict(id=d, kind='runtime') for d in deps])
    return dict(schema='ports-graph-v1',
                context=dict(target='aarch64', triplet='aarch64--netbsd',
                             toolchain_sha256=digest('gcc16'), base_sha256=digest('base'),
                             shared_inputs={'mk': digest('mk')}),
                provenance=dict(source='https://example.org/ports.git', revision='commit-a'),
                nodes=[node('openssl'), node('curl', ['openssl']), node('app', ['curl']), node('zlib')])


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sdk-ports-plan-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.old = lock_graph(graph())
        self.next = graph()
        self.next['nodes'][0]['version'] = '2.0'
        self.plan = make_plan(self.old, lock_graph(self.next))

    def receipt(self, name, outcome='success', plan=None):
        plan = self.plan if plan is None else plan
        nodes = {n['id']: n for n in plan['candidate']['graph']['nodes']}
        keys = plan['candidate']['input_keys']
        path = self.root / (name.replace('/', '_').replace(':', '_') + '.tgz')
        path.write_text(name, encoding='utf-8')
        log = self.root / 'producer.log'
        log.write_text('producer log', encoding='utf-8')
        return dict(schema='ports-result-v1', plan_sha256=plan['plan_sha256'], id=name,
                    input_key=keys[name], dependency_input_keys={d['id']: keys[d['id']] for d in nodes[name]['dependencies']},
                    outcome=outcome, log=dict(path=str(log), sha256=digest('producer log')), artifacts=[dict(path=str(path), sha256=digest(name))] if outcome == 'success' else [])

    def test_openssl_failure_patch_resume_and_stale_receipts(self):
        self.assertEqual(self.plan['impacted'], ['app', 'curl', 'openssl'])
        self.assertEqual(self.plan['waves'], [['openssl'], ['curl'], ['app']])
        self.assertEqual(plan_status(self.plan, [])['ready'], ['openssl'])
        ssl = self.receipt('openssl')
        failure = self.receipt('curl', 'failure')
        status = plan_status(self.plan, [ssl, failure])
        self.assertEqual(status['failed'], ['curl'])
        self.assertEqual(status['blocked'], {'app': ['curl']})
        with self.assertRaisesRegex(SDKError, 'failed dependencies'):
            plan_status(self.plan, [ssl, failure, self.receipt('app')])
        self.next['nodes'][1]['input_sha256'] = digest('curl compatibility patch')
        revised = make_plan(self.old, lock_graph(self.next))
        with self.assertRaisesRegex(SDKError, 'stale receipt'):
            plan_status(revised, [ssl])
        refreshed = [self.receipt(n, plan=revised) for n in ['openssl', 'curl', 'app']]
        self.assertTrue(plan_status(revised, refreshed)['complete'])
        Path(refreshed[1]['artifacts'][0]['path']).write_bytes(b'stale artifact')
        with self.assertRaisesRegex(SDKError, 'artifact hash mismatch'):
            plan_status(revised, refreshed)

    def test_unrelated_commit_and_order_preserve_input_keys(self):
        other = graph()
        other['provenance']['revision'] = 'unrelated-commit'
        other['nodes'].reverse()
        updated = lock_graph(other)
        self.assertEqual(updated['input_keys'], self.old['input_keys'])
        self.assertNotEqual(updated['lock_sha256'], self.old['lock_sha256'])
        self.assertEqual(make_plan(self.old, updated)['impacted'], [])
        reversed_graph = graph()
        reversed_graph['nodes'].reverse()
        self.assertEqual(lock_graph(reversed_graph), self.old)

    def test_new_removed_edges_and_dependency_kinds(self):
        changed = graph()
        changed['nodes'][1]['dependencies'] = [dict(id='zlib', kind='test')]
        changed['nodes'] = [n for n in changed['nodes'] if n['id'] != 'openssl']
        plan = make_plan(self.old, lock_graph(changed))
        self.assertEqual(plan['removed'], ['openssl'])
        self.assertEqual(plan['changed'], ['curl'])
        self.assertEqual(plan['impacted'], ['app', 'curl'])
        self.assertIn('changed:dependencies', plan['reasons']['curl'])
        status = plan_status(plan, [])
        self.assertEqual(status['required_evidence'], ['zlib'])
        self.assertEqual(status['ready'], [])
        status = plan_status(plan, [self.receipt('zlib', plan=plan)])
        self.assertEqual(status['ready'], ['curl'])
        changed['nodes'].append(dict(id='new', version='1', input_sha256=digest('new'), dependencies=[]))
        self.assertEqual(make_plan(self.old, lock_graph(changed))['new'], ['new'])

    def test_each_shared_input_change_invalidates_all_nodes(self):
        for field in ('target', 'triplet', 'toolchain_sha256', 'base_sha256', 'shared_inputs'):
            with self.subTest(field=field):
                changed = graph()
                if field == 'shared_inputs':
                    changed['context'][field]['mk'] = digest('new mk')
                elif field.endswith('sha256'):
                    changed['context'][field] = digest('new')
                else:
                    changed['context'][field] = 'new'
                plan = make_plan(self.old, lock_graph(changed))
                self.assertEqual(plan['context_changed'], [field])
                self.assertEqual(len(plan['impacted']), 4)
                for name in self.old['input_keys']:
                    self.assertNotEqual(self.old['input_keys'][name], plan['candidate']['input_keys'][name])

    def test_reject_invalid_graphs(self):
        variants = []
        bad = graph(); bad['schema'] = 'future'; variants.append(bad)
        bad = graph(); bad['extra'] = True; variants.append(bad)
        bad = graph(); bad['nodes'].append(bad['nodes'][0]); variants.append(bad)
        bad = graph(); bad['nodes'][1]['dependencies'] *= 2; variants.append(bad)
        bad = graph(); bad['nodes'][0]['dependencies'] = [dict(id='absent', kind='build')]; variants.append(bad)
        bad = graph(); bad['nodes'][0]['dependencies'] = [dict(id='app', kind='tool')]; variants.append(bad)
        bad = graph(); bad['nodes'][0]['dependencies'] = [dict(id='openssl', kind='runtime')]; variants.append(bad)
        bad = graph(); bad['nodes'][0]['input_sha256'] = 'bad'; variants.append(bad)
        bad = graph(); bad['nodes'][0]['dependencies'] = [dict(id='zlib', kind='unknown')]; variants.append(bad)
        for bad in variants:
            with self.subTest(graph=bad), self.assertRaises(SDKError):
                lock_graph(bad)
        with self.assertRaisesRegex(SDKError, 'duplicate metadata key'):
            loads_json('{"schema":1,"schema":2}')

    def test_lock_and_plan_tampering(self):
        changed = deepcopy(self.old)
        changed['input_keys']['openssl'] = digest('wrong')
        with self.assertRaises(SDKError):
            validate_lock(changed)
        changed = deepcopy(self.plan)
        changed['impacted'] = []
        with self.assertRaises(SDKError):
            validate_plan(changed)
        validate_plan(self.plan)

    def test_receipt_refusals(self):
        receipt = self.receipt('openssl')
        for field, value in [('schema', 'unknown'), ('input_key', digest('wrong')),
                             ('dependency_input_keys', {'ghost': digest('wrong')}),
                             ('outcome', 'passed'), ('artifacts', [])]:
            bad = dict(receipt, **{field: value})
            with self.subTest(field=field), self.assertRaises(SDKError):
                plan_status(self.plan, [bad])
        with self.assertRaisesRegex(SDKError, 'duplicate receipt'):
            plan_status(self.plan, [receipt, receipt])
        with self.assertRaisesRegex(SDKError, 'unverified'):
            plan_status(self.plan, [self.receipt('curl')])
        bad = self.receipt('openssl', 'failure')
        bad['artifacts'] = receipt['artifacts']
        with self.assertRaises(SDKError):
            plan_status(self.plan, [bad])

    def test_long_chain_without_recursion_and_host_tools(self):
        changed = graph()
        changed['nodes'] = [dict(id=f'host:{i}', version='1', input_sha256=digest(str(i)),
                                 dependencies=[dict(id=f'host:{i-1}', kind='tool')] if i else [])
                            for i in range(1100)]
        locked = lock_graph(changed)
        self.assertEqual(len(locked['input_keys']), 1100)

    def test_record_explicit_outcomes_and_evidence(self):
        log = self.root / 'log'
        artifact = self.root / 'package'
        log.write_text('failure log', encoding='utf-8')
        artifact.write_text('old output', encoding='utf-8')
        failed = record_result(self.plan, 'openssl', 'failure', str(log))
        self.assertEqual(failed['artifacts'], [])
        with self.assertRaisesRegex(SDKError, 'forbids'):
            record_result(self.plan, 'openssl', 'failure', str(log), [str(artifact)])
        with self.assertRaisesRegex(SDKError, 'requires'):
            record_result(self.plan, 'openssl', 'success', str(log))
        with self.assertRaisesRegex(SDKError, 'unknown'):
            record_result(self.plan, 'missing', 'success', str(log), [str(artifact)])
        with self.assertRaisesRegex(SDKError, 'regular file'):
            record_result(self.plan, 'openssl', 'success', str(log), [str(self.root / 'missing')])
        with self.assertRaises(SDKError):
            record_result(self.plan, 'openssl', 'success', 'relative', [str(artifact)])
        success = record_result(self.plan, 'openssl', 'success', str(log), [str(artifact)])
        self.assertEqual(plan_status(self.plan, [success])['ready'], ['curl'])
        log.write_text('replaced', encoding='utf-8')
        with self.assertRaisesRegex(SDKError, 'log hash mismatch'):
            plan_status(self.plan, [success])

    def test_whole_ports_sized_plan_roundtrip(self):
        large = graph()
        large['nodes'] = [dict(id=f'target:category/package-{i:05d}', version='1.0', input_sha256=digest(str(i)), dependencies=[])
                          for i in range(26000)]
        locked = self.root / 'large-lock.json'
        output = self.root / 'large-plan.json'
        locked.write_bytes(json_bytes(lock_graph(large)))
        self.assertEqual(main(['plan', str(locked), str(locked), '--output', str(output)]), 0)
        self.assertGreater(output.stat().st_size, 16 * 1024 * 1024)
        with redirect_stdout(io.StringIO()) as result:
            self.assertEqual(main(['status', str(output)]), 0)
        self.assertTrue(json.loads(result.getvalue())['complete'])

    def test_plan_output_limit_refuses_unreadable_document_before_write(self):
        baseline = self.root / 'old.json'
        candidate = self.root / 'new.json'
        output = self.root / 'plan.json'
        baseline.write_bytes(json_bytes(self.old))
        candidate.write_bytes(json_bytes(lock_graph(self.next)))
        size = len(json_bytes(self.plan))
        self.assertLess(baseline.stat().st_size, size - 1)
        self.assertLess(candidate.stat().st_size, size - 1)
        with patch('emberbsd_sdk.ports_plan.PORTS_JSON_LIMIT', size - 1), redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(main(['plan', str(baseline), str(candidate), '--output', str(output)]), 1)
        self.assertIn('output exceeds the JSON size limit', errors.getvalue())
        self.assertFalse(output.exists())
        self.assertEqual(main(['plan', str(baseline), str(candidate), '--output', str(output)]), 0)
        validate_plan(read_json(output))

    def test_cli_exclusive_outputs_and_unrelated_cwd(self):
        script = SDK_SOURCE / 'scripts' / 'ports-plan.py'
        source = self.root / 'graph.json'
        source.write_text(json.dumps(graph()), encoding='utf-8')
        lock = self.root / 'lock.json'
        plan = self.root / 'plan.json'
        def run(*args):
            return subprocess.run([sys.executable, '-B', '-I', '-S', str(script), *map(str, args)],
                                  cwd=self.root, capture_output=True, text=True)
        result = run('lock', source, '--output', lock)
        self.assertEqual(result.returncode, 0, result.stderr)
        original = lock.read_bytes()
        self.assertEqual(run('lock', source, '--output', lock).returncode, 1)
        self.assertEqual(lock.read_bytes(), original)
        result = run('plan', lock, lock, '--output', plan)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = run('status', plan)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['complete'])
        log = self.root / 'producer.log'
        log.write_text('failed producer', encoding='utf-8')
        receipt = self.root / 'failure.json'
        result = run('record', plan, 'openssl', '--outcome', 'failure', '--log', log, '--output', receipt)
        self.assertEqual(result.returncode, 0, result.stderr)
        original = receipt.read_bytes()
        self.assertEqual(run('record', plan, 'openssl', '--outcome', 'failure', '--log', log, '--output', receipt).returncode, 1)
        self.assertEqual(receipt.read_bytes(), original)
        source.write_text('{"schema":1,"schema":2}', encoding='utf-8')
        self.assertEqual(run('lock', source, '--output', self.root / 'bad.json').returncode, 1)


if __name__ == '__main__':
    unittest.main()
