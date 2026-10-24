# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Case-preserved metadata paths. No real tools or recipes run."""

import copy
import unittest

import test_bootstrap_producer
from emberbsd_sdk import bootstrap_producer as producer, bootstrap_recipe as recipe
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.inventory import snapshot_tree


MIXED_CASE = ('misc/p5-Locale-libintl', 'textproc/p5-Text-Unidecode',
              'converters/p5-Unicode-EastAsianWidth')


class RecipePathTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_bootstrap_producer.MetadataTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.plan, self.f = self.fixture.plan, self.fixture.f

    def set_recipes(self, names):
        for name in names:
            path = self.fixture.export / name / 'Makefile'
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text('# Synthetic source data; never executed.\n')
        inventory = self.f.document(snapshot_tree(str(self.fixture.export)))
        self.fixture.proof['inventory'] = inventory
        self.plan['metadata']['export'].update(
            inventory=inventory, source_proof=self.f.document(self.fixture.proof))
        self.plan['metadata']['recipes'] = names
        self.plan['commands'] = producer.producer_steps(self.plan, self.fixture.descriptor,
            self.fixture.commands, self.fixture.descriptor['abi_flags'])

    def test_actual_mixed_case_paths_keep_their_invocation_and_inventory_names(self):
        names = ['lang/gcc16', *MIXED_CASE]
        self.set_recipes(names)
        self.assertIs(self.fixture.validate(), self.plan)
        self.assertEqual([step['recipe'] for step in self.plan['commands'][::6]], names)
        for step in self.plan['commands']:
            argv = step['argv']
            self.assertEqual(argv[argv.index('-C') + 1],
                             str(self.fixture.export / step['recipe']))
        self.plan['metadata']['recipes'][-1] = MIXED_CASE[-1].lower()
        with self.assertRaisesRegex(SDKError, 'absent from the complete prepared export'):
            self.fixture.validate()

    def test_metadata_allows_256_explicit_recipes_and_refuses_257(self):
        names = ['lang/gcc16', *['devel/fixture-' + str(i) for i in range(255)]]
        self.set_recipes(names)
        self.assertEqual(len(self.plan['commands']), 6 * 256)
        self.assertIs(self.fixture.validate(), self.plan)
        self.plan['metadata']['recipes'].append('devel/one-too-many')
        with self.assertRaisesRegex(SDKError, 'finite explicit recipe list'):
            self.fixture.validate()

    def test_metadata_refuses_traversal_duplicates_and_case_aliases(self):
        for name in ('lang/.', 'lang/..', '../gcc16', '/lang/gcc16',
                     'lang/gcc16/extra', 'lang/gcc16', 'lang/GCC16'):
            with self.subTest(name=name):
                self.plan['metadata']['recipes'] = ['lang/gcc16', name]
                with self.assertRaisesRegex(SDKError, 'finite explicit recipe list'):
                    self.fixture.validate()

    def test_full_dependency_order_preserves_case_and_refuses_graph_errors(self):
        graph = {'lang/gcc16': dict(dependencies=list(MIXED_CASE))}
        graph.update({name: dict(dependencies=[]) for name in MIXED_CASE})
        self.assertEqual(recipe.recipe_order(dict(recipes=graph)),
                         [*sorted(MIXED_CASE), 'lang/gcc16'])
        for kind in ('cycle', 'duplicate', 'case-alias', 'traversal', 'missing'):
            with self.subTest(kind=kind):
                bad = copy.deepcopy(graph)
                if kind == 'cycle':
                    bad[MIXED_CASE[0]]['dependencies'] = ['lang/gcc16']
                elif kind == 'duplicate':
                    bad['lang/gcc16']['dependencies'].append(MIXED_CASE[0])
                elif kind == 'case-alias':
                    bad[MIXED_CASE[0].lower()] = dict(dependencies=[])
                elif kind == 'traversal':
                    bad['lang/..'] = dict(dependencies=[])
                else:
                    bad['lang/gcc16']['dependencies'].append('devel/not-declared')
                with self.assertRaises(SDKError):
                    recipe.recipe_order(dict(recipes=bad))


if __name__ == '__main__':
    unittest.main()
