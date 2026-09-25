"""The label query behind `-Queue 'where: ...'` (#38): pure parsing, evaluation and canonical form."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))
import labelquery as q
from labelquery import And, In, Label, Not, Or


def matches(query, *labels):
    return q.evaluate(q.parse(query), q.labels_of(labels))


class Parsing(unittest.TestCase):
    def test_precedence_is_not_then_and_then_or(self):
        self.assertEqual(Or((And((Label('a'), Not(Label('b')))), Label('c'))), q.parse('a AND NOT b OR c'))
        self.assertEqual(Or((Label('a'), And((Label('b'), Label('c'))))), q.parse('a OR b AND c'))
        self.assertEqual(Not(Not(Label('a'))), q.parse('NOT NOT a'))

    def test_parentheses_override_precedence(self):
        grouped = q.parse('bug AND (priority IN [P0] OR ux)')
        flat = q.parse('bug AND priority IN [P0] OR ux')
        self.assertEqual(And((Label('bug'), Or((In('priority', ('P0',)), Label('ux'))))), grouped)
        self.assertEqual(Or((And((Label('bug'), In('priority', ('P0',)))), Label('ux'))), flat)
        # ...and they select different issues: `ux` alone.
        self.assertFalse(q.evaluate(grouped, q.labels_of(['ux'])))
        self.assertTrue(q.evaluate(flat, q.labels_of(['ux'])))

    def test_membership_and_its_negation(self):
        self.assertEqual(In('priority', ('P0', 'P1')), q.parse('priority IN [P0, P1]'))
        self.assertEqual(In('priority', ('P2', 'P3'), True), q.parse('priority NOT IN [P2,P3]'))
        self.assertEqual(Not(In('priority', ('P0',))), q.parse('NOT priority IN [P0]'))
        self.assertEqual(In('label', ('bug', 'regression')), q.parse('label in [bug, regression]'))

    def test_keywords_are_case_insensitive_and_must_be_quoted_as_labels(self):
        self.assertEqual(q.parse('a AND NOT b OR c'), q.parse('a and not b Or c'))
        self.assertEqual(Label('and'), q.parse('"and"'))
        with self.assertRaises(q.QueryError):
            q.parse('and')

    def test_labels_bare_and_quoted(self):
        for text, label in (('priority:P0', 'priority:P0'), ('follow-up', 'follow-up'), ('v1.2/x_y', 'v1.2/x_y'),
                            ('"good first issue"', 'good first issue'), ("'good first issue'", 'good first issue'),
                            ('"c++"', 'c++'), ('"🐛 bug"', '🐛 bug'), ('"a[1],b"', 'a[1],b'),
                            (r'"say \"hi\""', 'say "hi"'), (r"'it\'s'", "it's"), (r'"back\\slash"', 'back\\slash'),
                            ('größe', 'größe')):
            with self.subTest(text=text):
                self.assertEqual(Label(label), q.parse(text))
        self.assertEqual(In('area', ('needs design', 'ui')), q.parse('"area" IN ["needs design", ui]'))

    def test_errors_name_the_column_and_what_was_expected(self):
        for text, column, message in (
                ('', 1, 'expected an expression'),
                ('bug AND', 8, "expected a label, NOT or '(', found the end of the query"),
                ('bug wontfix', 5, "expected AND, OR or the end of the query, found 'wontfix'"),
                ('bug NOT wontfix', 5, 'expected AND, OR or the end of the query, found NOT'),
                ('priority IN []', 14, 'expected a value (the list may not be empty)'),
                ('priority IN [P0,]', 17, "expected a value after ','"),
                ('priority IN [P0', 16, "expected ',' or ']'"),
                ('priority IN P0', 13, "expected '['"),
                ('(bug', 5, "expected AND, OR or ')'"),
                ('bug)', 4, "(')' has no matching '(')"),
                ('bug AND "needs design', 9, 'unterminated quote "'),
                ('c++', 2, "unexpected character '+'"),
                ('bug AND ()', 10, "expected a label, NOT or '('")):
            with self.subTest(text=text):
                with self.assertRaises(q.QueryError) as caught:
                    q.parse(text)
                self.assertEqual(column, caught.exception.column)
                self.assertIn(message, str(caught.exception))
                lines = str(caught.exception).splitlines()
                self.assertEqual(lines[2].index('^') - 2, column - 1)          # the caret points at it


class Evaluation(unittest.TestCase):
    def test_matching_is_case_insensitive(self):
        self.assertTrue(matches('Bug AND priority IN [p0]', 'bug', 'Priority:P0'))

    def test_key_in_means_key_colon_value_exactly(self):
        self.assertTrue(matches('priority IN [P0, P1]', 'priority:P1'))
        self.assertFalse(matches('priority IN [P0]', 'priority: P0'))           # no whitespace normalisation
        self.assertFalse(matches('priority IN [P0]', 'P0'))
        self.assertTrue(matches('label IN [bug, regression]', 'regression'))

    def test_not_in_also_matches_issues_without_such_a_label(self):
        self.assertTrue(matches('bug AND priority NOT IN [P2, P3]', 'bug'))    # untriaged: included
        self.assertFalse(matches('priority NOT IN [P2, P3]', 'priority:P2'))
        self.assertTrue(matches('nokey NOT IN [x]', 'bug'))                     # an unknown key matches everything...
        self.assertFalse(matches('nokey IN [x]', 'bug'))                        # ...and nothing under IN

    def test_the_issue_examples(self):
        first = 'bug AND priority IN [P0, P1] AND NOT wontfix'
        self.assertTrue(matches(first, 'bug', 'priority:P1'))
        self.assertFalse(matches(first, 'bug', 'priority:P1', 'wontfix'))
        second = '(bug OR follow-up) AND priority NOT IN [P2, P3]'
        self.assertTrue(matches(second, 'follow-up', 'priority:P0'))
        self.assertFalse(matches(second, 'follow-up', 'priority:P3'))
        third = 'label IN [bug, regression] AND NOT "needs design"'
        self.assertTrue(matches(third, 'regression'))
        self.assertFalse(matches(third, 'bug', 'Needs Design'))


class Canonical(unittest.TestCase):
    CASES = ['bug AND priority IN [P0, P1] AND NOT wontfix', '(bug OR follow-up) AND priority NOT IN [P2, P3]',
             'label IN [bug, regression] AND NOT "needs design"', 'bug AND (priority IN [P0] OR ux)',
             'bug AND priority IN [P0] OR ux', 'NOT (a OR "b c") AND "and"', r'"say \"hi\"" OR "back\\slash"',
             "'c++' AND x"]

    def test_canonical_round_trips(self):
        for text in self.CASES:
            with self.subTest(text=text):
                node = q.parse(text)
                self.assertEqual(node, q.parse(q.canonical(node)))

    def test_canonical_form(self):
        self.assertEqual('((bug AND priority IN [P0]) OR ux)', q.canonical(q.parse('bug and priority in [P0] or ux')))
        self.assertEqual('(label IN [bug, regression] AND NOT "needs design")',
                         q.canonical(q.parse("label IN [bug,regression] AND NOT 'needs design'")))

    def test_the_identity_ignores_spelling_but_not_logic(self):
        key = lambda text: q.compile_query(text)[1].key
        self.assertEqual(key('Bug AND priority IN [P0]'), key('bug and PRIORITY in ["p0"]'))
        self.assertEqual(key('a AND (b AND c)'), key('(a AND b) AND c'))           # redundant grouping
        self.assertEqual(key('a AND b AND c'), key('a  AND  b AND c'))
        self.assertNotEqual(key('a AND b'), key('b AND a'))                         # reordering is another query
        self.assertNotEqual(key('label IN [a]'), key('a'))                          # semantic equivalence: out of scope
        self.assertEqual('(Bug AND priority IN [P0])', q.compile_query('Bug AND priority IN [P0]')[1].text)  # as written


if __name__ == '__main__':
    unittest.main()
