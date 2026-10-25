# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""NetBSD first-native compiler route contracts; no compiler runs here."""

import json
from pathlib import Path
import tempfile
import unittest

from emberbsd_sdk.bootstrap_netbsd_gcc16 import (
    BOOTSTRAP_NETBSD_GCC16_API,
    load_pkgsrc_bootstrap_tools,
)
from emberbsd_sdk.errors import SDKError


class PkgsrcToolsFixture:
    def __init__(self, root):
        self.root = Path(root)
        self.provider = self.root / 'provider'
        self.prefix = self.provider / 'prefix'
        (self.prefix / 'bin').mkdir(parents=True)
        (self.prefix / 'share' / 'mk').mkdir(parents=True)
        (self.provider / 'bootstrap' / 'bin').mkdir(parents=True)
        (self.prefix / 'share' / 'mk' / 'sys.mk').write_text('# synthetic pkgsrc make files\n')
        self.bmake = self.provider / 'bootstrap' / 'bin' / 'bmake'
        self.gmake = self.prefix / 'bin' / 'gmake'
        self.mksh = self.prefix / 'bin' / 'mksh'
        for path in (self.bmake, self.gmake, self.mksh):
            path.write_bytes(b'fixture executable\n')
            path.chmod(0o755)
        self.inventory = {
            'bin': {'kind': 'directory', 'mode': 0o755},
            'bin/gmake': {'kind': 'file', 'mode': 0o755, 'size': self.gmake.stat().st_size,
                          'sha256': self.digest(self.gmake)},
            'bin/mksh': {'kind': 'file', 'mode': 0o755, 'size': self.mksh.stat().st_size,
                         'sha256': self.digest(self.mksh)},
            'share': {'kind': 'directory', 'mode': 0o755},
            'share/mk': {'kind': 'directory', 'mode': 0o755},
            'share/mk/sys.mk': {'kind': 'file', 'mode': 0o644,
                                'size': (self.prefix / 'share/mk/sys.mk').stat().st_size,
                                'sha256': self.digest(self.prefix / 'share/mk/sys.mk')},
        }
        self.inventory_path = self.root / 'output-inventory.json'
        self.inventory_path.write_text(json.dumps(self.inventory, sort_keys=True))
        self.plan = self.root / 'plan.json'
        self.result = self.root / 'result.json'
        self.export_acceptance = self.root / 'export-acceptance.json'
        self.transfer_acceptance = self.root / 'transfer-acceptance.json'
        self.compiler_seed = self.root / 'compiler-seed.json'
        for path in (self.export_acceptance, self.transfer_acceptance):
            path.write_text('{}')
        self.compiler_seed.write_text(json.dumps({
            'format': 'emberbsd-bootstrap-compiler-seed', 'scope': 'bmake-bootstrap',
        }, sort_keys=True))
        self.plan_value = {
            'format': 'emberbsd-bootstrap-pkgsrc-provider-plan', 'version': 1,
            'scope': 'pkgsrc-bootstrap-tools', 'purpose': 'bootstrap-current-pkgsrc-tools',
            'host': 'NetBSD11.0-amd64',
            'export_acceptance': self.ref(self.export_acceptance),
            'transfer_acceptance': self.ref(self.transfer_acceptance),
            'compiler_seed': self.ref(self.compiler_seed),
            'source_root': '/tmp/source', 'work_root': str(self.provider),
            'bootstrap_work': str(self.provider / 'bootstrap'),
            'prefix_root': str(self.prefix), 'commands': [],
        }
        self.plan.write_text(json.dumps(self.plan_value, sort_keys=True))
        self.plan_ref = self.ref(self.plan)
        self.inventory_ref = self.ref(self.inventory_path)
        self.result_value = {
            'format': 'emberbsd-bootstrap-pkgsrc-provider-result', 'version': 1,
            'scope': 'pkgsrc-bootstrap-tools',
            'status': 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW',
            'bootstrap_plan': self.plan_ref,
            'export_acceptance': self.ref(self.export_acceptance),
            'transfer_acceptance': self.ref(self.transfer_acceptance),
            'compiler_seed': self.ref(self.compiler_seed),
            'output_root': str(self.prefix), 'output_inventory': self.inventory_ref,
            'outputs': [
                {'name': 'bmake', 'path': str(self.bmake), 'sha256': self.digest(self.bmake), 'mode': 0o755},
                {'name': 'mksh', 'path': str(self.mksh), 'sha256': self.digest(self.mksh), 'mode': 0o755},
                {'name': 'gmake', 'path': str(self.gmake), 'sha256': self.digest(self.gmake), 'mode': 0o755},
            ],
            'all_commands_drained': True, 'inputs_revalidated_after_drain': True,
            'all_leases_closed': True, 'ordinary_provider_qualified': False,
        }
        self.result.write_text(json.dumps(self.result_value, sort_keys=True))
        self.result_ref = self.ref(self.result)
        self.acceptance = self.root / 'acceptance.json'
        self.acceptance_value = {
            'format': 'emberbsd-bootstrap-pkgsrc-provider-acceptance', 'version': 1,
            'scope': 'pkgsrc-bootstrap-tools', 'status': 'PASS',
            'plan': self.plan_ref, 'result': self.result_ref,
            'output_inventory': self.inventory_ref,
            'export_acceptance': self.ref(self.export_acceptance),
            'transfer_acceptance': self.ref(self.transfer_acceptance),
            'compiler_seed': self.ref(self.compiler_seed),
            'accepted_roles': ['gmake', 'make', 'sh'],
            'checks': {
                'all_commands_drained': True,
                'inputs_revalidated_after_drain': True,
                'all_leases_closed': True,
                'source_export_matches': True,
                'output_inventory_matches': True,
            },
        }
        self.acceptance.write_text(json.dumps(self.acceptance_value, sort_keys=True))

    @staticmethod
    def digest(path):
        import hashlib
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    @classmethod
    def ref(cls, path):
        path = Path(path)
        return {'path': str(path), 'sha256': cls.digest(path)}


class NetBSDGCC16RouteTests(unittest.TestCase):
    def test_api_is_explicitly_scoped_to_pkgsrc_bootstrap_tools(self):
        self.assertEqual(BOOTSTRAP_NETBSD_GCC16_API, 1)

    def test_accepts_only_the_independently_reviewed_bootstrap_tool_roles(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = PkgsrcToolsFixture(temporary)
            selected = load_pkgsrc_bootstrap_tools(
                str(fixture.acceptance), acceptance_sha256=fixture.digest(fixture.acceptance),
            )
            self.assertEqual(selected.scope, 'pkgsrc-bootstrap-tools')
            self.assertEqual(selected.command('make'), (str(fixture.bmake),))
            self.assertEqual(selected.command('gmake'), (str(fixture.gmake),))
            self.assertEqual(selected.command('sh'), (str(fixture.mksh),))
            self.assertFalse(selected.ordinary_provider_qualified)

    def test_rejects_receipt_role_expansion(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = PkgsrcToolsFixture(temporary)
            fixture.acceptance_value['accepted_roles'].append('cc')
            fixture.acceptance.write_text(json.dumps(fixture.acceptance_value, sort_keys=True))
            with self.assertRaises(SDKError):
                load_pkgsrc_bootstrap_tools(
                    str(fixture.acceptance), acceptance_sha256=fixture.digest(fixture.acceptance),
                )

    def test_rejects_claimed_ordinary_qualification(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = PkgsrcToolsFixture(temporary)
            fixture.result_value['ordinary_provider_qualified'] = True
            fixture.result.write_text(json.dumps(fixture.result_value, sort_keys=True))
            fixture.result_ref = fixture.ref(fixture.result)
            fixture.acceptance_value['result'] = fixture.result_ref
            fixture.acceptance.write_text(json.dumps(fixture.acceptance_value, sort_keys=True))
            with self.assertRaises(SDKError):
                load_pkgsrc_bootstrap_tools(
                    str(fixture.acceptance), acceptance_sha256=fixture.digest(fixture.acceptance),
                )

    def test_rejects_integer_instead_of_true_independent_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = PkgsrcToolsFixture(temporary)
            fixture.acceptance_value['checks']['source_export_matches'] = 1
            fixture.acceptance.write_text(json.dumps(fixture.acceptance_value, sort_keys=True))
            with self.assertRaises(SDKError):
                load_pkgsrc_bootstrap_tools(
                    str(fixture.acceptance), acceptance_sha256=fixture.digest(fixture.acceptance),
                )

    def test_rejects_changed_prefix_member(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = PkgsrcToolsFixture(temporary)
            fixture.gmake.write_bytes(b'changed executable')
            with self.assertRaises(SDKError):
                load_pkgsrc_bootstrap_tools(
                    str(fixture.acceptance), acceptance_sha256=fixture.digest(fixture.acceptance),
                )

    def test_rejects_output_path_outside_accepted_roots(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = PkgsrcToolsFixture(temporary)
            fixture.result_value['outputs'][0]['path'] = '/usr/bin/bmake'
            fixture.result.write_text(json.dumps(fixture.result_value, sort_keys=True))
            fixture.result_ref = fixture.ref(fixture.result)
            fixture.acceptance_value['result'] = fixture.result_ref
            fixture.acceptance.write_text(json.dumps(fixture.acceptance_value, sort_keys=True))
            with self.assertRaises(SDKError):
                load_pkgsrc_bootstrap_tools(
                    str(fixture.acceptance), acceptance_sha256=fixture.digest(fixture.acceptance),
                )


if __name__ == '__main__':
    unittest.main()
