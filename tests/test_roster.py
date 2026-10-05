"""#109: the implementer roster. tests/fixtures/roster-cases.json is also what test_launch.py feeds
Get-RosterProblem and Get-FailoverOrderProblem, so the Python and PowerShell rules cannot drift."""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'lib'))

import roster  # noqa: E402

CASES = json.loads((ROOT / 'tests/fixtures/roster-cases.json').read_text(encoding='utf-8'))


class Validation(unittest.TestCase):
    def test_every_roster_case_of_the_shared_fixture(self):
        for case in CASES['roster']:
            with self.subTest(case=case['name']):
                self.assertEqual(case['ok'], roster.problem(case['roster']) is None, roster.problem(case['roster']))

    def test_every_failover_order_case_of_the_shared_fixture(self):
        for case in CASES['failoverOrder']:
            with self.subTest(case=case['name']):
                entries = case['roster'] if case['roster'] is not None else roster.DEFAULT_ROSTER
                why = roster.failover_problem(case['order'], entries)
                self.assertEqual(case['ok'], why is None, why)

    def test_the_default_roster_is_the_issues_example_and_valid(self):
        self.assertIsNone(roster.problem(roster.DEFAULT_ROSTER))
        self.assertEqual(['claude-sonnet', 'claude-opus', 'kimi', 'codex-sol', 'codex-luna'],
                         [e['id'] for e in roster.DEFAULT_ROSTER])
        self.assertFalse(any('astra' in json.dumps(e).casefold() for e in roster.DEFAULT_ROSTER))

    def test_load_returns_a_private_copy_of_the_default_and_names_what_is_wrong(self):
        first = roster.load({})
        first[0]['id'] = 'changed'
        self.assertEqual('claude-sonnet', roster.load({})[0]['id'])
        self.assertEqual(1, len(roster.load({'implementerRoster': [{'id': 'a', 'tool': 'kimi', 'note': 'x'}]})))
        with self.assertRaisesRegex(roster.RosterError, 'astra'):
            roster.load({'implementerRoster': [{'id': 'a', 'tool': 'codex', 'model': 'gpt-6-astra', 'note': 'x'}]})

    def test_model_problem(self):
        self.assertIsNone(roster.model_problem('claude-sonnet-5-5'))
        for bad in ('gpt-6-astra', 'GPT-6-ASTRA', 'a b', '', 7, None):
            with self.subTest(model=bad):
                self.assertIsNotNone(roster.model_problem(bad))


class Failover(unittest.TestCase):
    ROSTER = roster.DEFAULT_ROSTER

    def test_a_tool_name_stands_for_the_first_entry_of_that_tool(self):
        got = roster.resolve_order(['claude', 'codex', 'kimi'], self.ROSTER)
        self.assertEqual(['claude-sonnet', 'codex-sol', 'kimi'], [e['id'] for e in got])

    def test_a_roster_id_is_its_own_entry_and_carries_its_model(self):
        got = roster.resolve_order(['claude-opus', 'codex-luna'], self.ROSTER)
        self.assertEqual([('claude-opus', 'claude-opus-5-5'), ('codex-luna', 'gpt-6-luna')], [(e['id'], e['model']) for e in got])

    def test_a_limited_tool_is_skipped_even_when_another_entry_of_it_comes_next(self):
        order = ['claude-sonnet', 'claude-opus', 'codex-sol', 'kimi']
        self.assertEqual('codex-sol', roster.next_failover(order, self.ROSTER, 'claude')['id'])

    def test_recorded_limits_are_skipped_and_none_left_is_none(self):
        order = ['claude', 'codex', 'kimi']
        self.assertEqual('kimi', roster.next_failover(order, self.ROSTER, 'claude', recorded={'codex'})['id'])
        self.assertIsNone(roster.next_failover(order, self.ROSTER, 'claude', recorded={'codex', 'kimi'}))

    def test_the_default_order_keeps_todays_behaviour(self):
        order = ['claude', 'codex', 'kimi']
        for limited, expected in (('codex', 'claude'), ('claude', 'codex'), ('kimi', 'claude')):
            with self.subTest(limited=limited):
                self.assertEqual(expected, roster.next_failover(order, self.ROSTER, limited)['tool'])

    def test_a_custom_roster_without_a_tool_still_resolves_the_bare_tool(self):
        only = [{'id': 'k', 'tool': 'kimi', 'note': 'x'}]
        got = roster.resolve_order(['claude', 'k'], only)
        self.assertEqual([('claude', 'claude'), ('k', 'kimi')], [(e['id'], e['tool']) for e in got])
        self.assertNotIn('model', got[0])


if __name__ == '__main__':
    unittest.main()
