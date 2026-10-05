"""#109: the implementer router. The model is always a fake (injected like triage's), gh a recorder."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'lib'))

import roster  # noqa: E402
import route  # noqa: E402


def done(stdout='', stderr='', code=0):
    return subprocess.CompletedProcess([], code, stdout, stderr)


def envelope(**answer):
    return done(json.dumps({'is_error': False, 'structured_output': answer}))


class Fixtures(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix='route-test-'))
        self.addCleanup(__import__('shutil').rmtree, self.temp, True)
        old = {k: os.environ.get(k) for k in ('AGWORKBENCH_ROUTE_ROOT', 'AGWORKBENCH_ROUTE_OUTCOMES')}
        os.environ['AGWORKBENCH_ROUTE_ROOT'] = str(self.temp / 'work')
        os.environ['AGWORKBENCH_ROUTE_OUTCOMES'] = str(self.temp / 'outcomes.jsonl')
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v) for k, v in old.items()])
        self.model_calls = []
        self.answer = dict(implementer='claude-sonnet', reason='ordinary code', rule='default-code')
        self.issue = {'number': 5, 'title': 'Fix the thing', 'body': 'Details.', 'state': 'open',
                      'labels': [{'name': 'priority:P2'}], 'author_association': 'OWNER'}
        self.gh_calls = []

    def gh(self, *args, timeout=120):
        self.gh_calls.append(args)
        if args[:1] == ('api',):
            return done(json.dumps(self.issue))
        return done('')

    def model(self, argv, cwd):
        facts_dir = Path(argv[argv.index('--add-dir') + 1])
        self.model_calls.append(dict(argv=argv, facts=json.loads((facts_dir / 'facts.json').read_text(encoding='utf-8'))))
        return envelope(**self.answer)

    def route(self, settings=None, limited=(), records=None):
        return route.route_issue('o/r', 5, settings=settings or {}, limited=limited, gh=self.gh, model=self.model,
                                 records=[] if records is None else records)

    def labels(self, *names):
        self.issue['labels'] = [{'name': n} for n in names]


class Contract(Fixtures):
    def test_a_valid_answer_is_returned_with_tool_and_model(self):
        got = self.route()
        self.assertEqual(('claude-sonnet', 'claude', 'claude-sonnet-5-5', 'default-code', 'router'),
                         (got['implementer'], got['tool'], got['model'], got['rule'], got['source']))
        self.assertEqual('ordinary code', got['reason'])
        self.assertEqual(1, len(self.model_calls))

    def test_the_model_call_is_cheap_headless_and_schema_bound(self):
        self.route(settings={'route': {'model': 'claude-haiku-4-5-20251001'}})
        argv = self.model_calls[0]['argv']
        self.assertEqual('claude-haiku-4-5-20251001', argv[argv.index('--model') + 1])
        for flag in ('-p', '--restricted', '--no-session-persistence', '--json-schema'):
            self.assertIn(flag, argv)
        schema = json.loads(argv[argv.index('--json-schema') + 1])
        self.assertEqual(['claude-sonnet', 'claude-opus', 'codex-sol', 'codex-luna'], schema['properties']['implementer']['enum'])
        self.assertIn('Kimi suitability', argv[argv.index('-p') + 1])

    def test_the_facts_hold_the_issue_the_roster_and_the_untrusted_note(self):
        self.route()
        facts = self.model_calls[0]['facts']
        self.assertEqual('Fix the thing', facts['issue']['title'])
        self.assertEqual('P2', facts['issue']['priority'])
        self.assertIn('untrusted', facts['note'])
        self.assertEqual({'id', 'tool', 'note'} <= set(facts['roster'][0]), True)
        self.assertEqual(['claude-sonnet', 'claude-opus', 'codex-sol', 'codex-luna'], [e['id'] for e in facts['roster']])

    def test_the_default_model_and_a_bad_route_section(self):
        self.assertEqual(route.DEFAULT_MODEL, route.route_model({}))
        for bad in ({'route': {'model': 'gpt-6-astra'}}, {'route': {'model': 5}}, {'route': {'other': 1}}, {'route': 'x'}):
            with self.subTest(bad=bad):
                with self.assertRaises(route.RouteError):
                    route.route_model(bad)

    def test_an_unknown_id_is_an_error(self):
        self.answer['implementer'] = 'gpt-6-astra'
        with self.assertRaisesRegex(route.RouteError, 'not an implementerRoster id'):
            self.route()

    def test_a_bad_reason_or_rule_or_shape_is_an_error(self):
        for change in ({'reason': ''}, {'reason': 'x' * 1001}, {'rule': ''}, {'rule': 'Not Kebab'}, {'reason': 5}):
            with self.subTest(change=change):
                self.answer.update(change)
                with self.assertRaises(route.RouteError):
                    self.route()
                self.answer = dict(implementer='claude-sonnet', reason='ok', rule='default-code')

    def test_a_failed_or_garbled_model_call_is_an_error_never_a_default(self):
        for output in (done('', 'boom', 1), done('not json'), done(json.dumps({'is_error': True, 'result': 'x'})),
                       done(json.dumps({'result': '["a"]'}))):
            with self.subTest(output=output.stdout):
                with self.assertRaises(route.RouteError):
                    route.route_issue('o/r', 5, settings={}, gh=self.gh, model=lambda argv, cwd: output, records=[])

    def test_a_timeout_is_an_error(self):
        def slow(argv, cwd):
            raise subprocess.TimeoutExpired(argv, 1)
        with self.assertRaisesRegex(route.RouteError, 'timed out'):
            route.route_issue('o/r', 5, settings={}, gh=self.gh, model=slow, records=[])

    def test_astra_is_never_offered_even_from_a_hand_edited_roster(self):
        with self.assertRaisesRegex(roster.RosterError, 'astra'):
            self.route(settings={'implementerRoster': [{'id': 'a', 'tool': 'codex', 'model': 'gpt-6-astra', 'note': 'x'}]})
        self.route()
        self.assertNotIn('astra', json.dumps(self.model_calls[0]).casefold())

    def test_the_facts_directory_is_removed_afterwards(self):
        self.route()
        self.assertEqual([], [p for p in (self.temp / 'work').glob('facts-*')])


class KimiRules(Fixtures):
    def test_kimi_is_offered_only_for_a_kimi_labelled_p2_p3_issue(self):
        for labels, expected in ((('priority:P2', 'kimi'), True), (('priority:P3', 'Kimi'), True), (('priority:P2',), False),
                                 (('priority:P1', 'kimi'), False), (('priority:P0', 'kimi'), False), (('kimi',), True)):
            with self.subTest(labels=labels):
                self.labels(*labels)
                self.model_calls.clear()
                self.route()
                ids = [e['id'] for e in self.model_calls[0]['facts']['roster']]
                self.assertEqual(expected, 'kimi' in ids)
                self.assertEqual(expected, self.model_calls[0]['facts']['kimi']['eligible'])

    def test_a_kimi_answer_for_an_ineligible_issue_is_replaced_by_the_guard(self):
        self.labels('priority:P1', 'kimi')
        self.answer.update(implementer='kimi', rule='kimi-narrow-fix', reason='tiny')
        got = self.route()
        self.assertEqual(('claude-sonnet', 'kimi-guard', 'claude'), (got['implementer'], got['rule'], got['tool']))
        self.assertIn('kimi', got['reason'])

    def test_the_guard_skips_a_limited_tool_and_fails_when_nothing_else_is_offered(self):
        self.labels('priority:P2')
        self.answer.update(implementer='kimi', rule='x', reason='tiny')
        self.assertEqual('codex-sol', self.route(limited={'claude'})['implementer'])
        with self.assertRaisesRegex(route.RouteError, 'no roster entry is usable'):
            self.route(limited={'claude', 'codex'})

    def test_an_eligible_kimi_answer_stands(self):
        self.labels('priority:P3', 'kimi')
        self.answer.update(implementer='kimi', rule='kimi-narrow-fix', reason='one crate')
        got = self.route()
        self.assertEqual(('kimi', 'kimi-narrow-fix', 'kimi'), (got['implementer'], got['rule'], got['tool']))
        self.assertNotIn('model', got)

    def test_the_prompt_carries_kimis_rules(self):
        text = route.prompt_text(Path('facts.json'))
        for rule in ('p0-p1', 'save-path', 'outside-format', 'umbrella-batch', 'narrow-fix', 'leftovers-one-area',
                     'harness-two-crates', 'ui-single-view'):
            with self.subTest(rule=rule):
                self.assertIn(rule if rule != 'p0-p1' else 'P0/P1', text)


class Limits(Fixtures):
    def test_a_limited_tool_has_no_entry_in_the_offered_roster(self):
        self.route(limited={'codex'})
        self.assertEqual(['claude-sonnet', 'claude-opus'], [e['id'] for e in self.model_calls[0]['facts']['roster']])
        schema = json.loads(self.model_calls[0]['argv'][self.model_calls[0]['argv'].index('--json-schema') + 1])
        self.assertNotIn('codex-sol', schema['properties']['implementer']['enum'])

    def test_an_answer_naming_a_limited_tools_entry_is_an_error(self):
        self.answer['implementer'] = 'codex-sol'
        with self.assertRaisesRegex(route.RouteError, 'usage limit'):
            self.route(limited={'codex'})

    def test_every_tool_limited_is_an_error_before_any_model_call(self):
        with self.assertRaisesRegex(route.RouteError, 'no roster entry is usable'):
            self.route(limited={'claude', 'codex', 'kimi'})
        self.assertFalse(self.model_calls)


class LabelOverride(Fixtures):
    def test_an_impl_label_wins_and_the_model_is_not_called(self):
        self.labels('priority:P2', 'impl:codex-luna')
        got = self.route()
        self.assertEqual(('codex-luna', 'codex', 'gpt-6-luna', 'label', 'label-override'),
                         (got['implementer'], got['tool'], got['model'], got['source'], got['rule']))
        self.assertFalse(self.model_calls)

    def test_a_stale_label_is_ignored_with_a_warning_and_the_router_runs(self):
        self.labels('impl:retired-entry')
        got = self.route()
        self.assertEqual('router', got['source'])
        self.assertTrue(any('retired-entry' in w for w in got['warnings']))
        self.assertEqual(1, len(self.model_calls))

    def test_two_impl_labels_refuse(self):
        self.labels('impl:kimi', 'impl:codex-sol')
        with self.assertRaisesRegex(route.RouteError, 'more than one impl:'):
            self.route()
        self.assertFalse(self.model_calls)

    def test_a_stale_and_a_valid_label_use_the_valid_one(self):
        self.labels('impl:gone', 'impl:claude-opus')
        self.assertEqual('claude-opus', self.route()['implementer'])

    def test_a_label_naming_a_limited_tool_refuses(self):
        self.labels('impl:codex-sol')
        with self.assertRaisesRegex(route.RouteError, 'usage limit'):
            self.route(limited={'codex'})

    def test_an_owner_label_may_name_kimi_without_the_kimi_label(self):
        self.labels('priority:P1', 'impl:kimi')
        self.assertEqual('kimi', self.route()['implementer'])

    def test_apply_label_creates_the_label_then_adds_it(self):
        route.apply_label('o/r', 5, 'claude-opus', gh=self.gh)
        self.assertEqual(('label', 'create', 'impl:claude-opus'), self.gh_calls[0][:3])
        self.assertEqual(('issue', 'edit', '5'), self.gh_calls[1][:3])
        self.assertIn('impl:claude-opus', self.gh_calls[1])

    def test_apply_label_tolerates_an_existing_label_and_reports_a_failure(self):
        route.apply_label('o/r', 5, 'kimi', gh=lambda *a, **k: done('', 'label already exists') if a[0] == 'label' else done(''))
        with self.assertRaisesRegex(route.RouteError, 'cannot label'):
            route.apply_label('o/r', 5, 'kimi', gh=lambda *a, **k: done('') if a[0] == 'label' else done('', 'nope', 1))
        with self.assertRaisesRegex(route.RouteError, 'cannot create label'):
            route.apply_label('o/r', 5, 'kimi', gh=lambda *a, **k: done('', 'denied', 1))


class PastOutcomes(Fixtures):
    RECORDS = [
        dict(repo='o/r', number=1, rosterId='claude-sonnet', outcome='merged', reviewRounds=2, majors=1, wallSeconds=3600,
             claudeOutputTokens=300000, claudeCacheReadTokens=9e7, labels=['bug'], priority='P2', sizeBucket='small'),
        dict(repo='o/r', number=2, rosterId='claude-sonnet', outcome='closed', reviewRounds=4, majors=2, wallSeconds=7200,
             labels=['bug'], priority='P2', sizeBucket='large'),
        dict(repo='o/r', number=3, rosterId='kimi', outcome='merged', reviewRounds=1, majors=0, labels=['docs'], priority='P3'),
    ]

    def test_summarize_per_roster_id(self):
        table = route.summarize(self.RECORDS)
        self.assertEqual(dict(merged=1, notMerged=1, meanReviewRounds=3.0, majors=3, meanWallMinutes=90.0,
                              meanClaudeOutputTokens=300000.0, meanClaudeCacheReadTokens=9e7), table['claude-sonnet'])
        self.assertEqual((1, 0, None), (table['kimi']['merged'], table['kimi']['notMerged'], table['kimi']['meanWallMinutes']))

    def test_comparable_issues_share_the_priority_or_a_label(self):
        got = route.comparable(self.RECORDS, ['bug', 'impl:x'], 'P3')
        self.assertEqual([1, 2, 3], [r['number'] for r in got])
        self.assertEqual([3], [r['number'] for r in route.comparable(self.RECORDS, ['other'], 'P3')])
        self.assertEqual([], route.comparable(self.RECORDS, ['priority:P2'], None))

    def test_past_outcomes_reach_the_facts_file(self):
        self.labels('bug', 'priority:P2')
        self.route(records=self.RECORDS)
        past = self.model_calls[0]['facts']['past']
        self.assertEqual(2, past['perRoster']['claude-sonnet']['merged'] + past['perRoster']['claude-sonnet']['notMerged'])
        self.assertEqual([1, 2], [c['number'] for c in past['comparable']])
        self.assertNotIn('kimi', past['perRoster'])        # a docs P3 loop is not comparable to a bug P2

    def test_read_outcomes_skips_bad_lines_and_a_missing_file(self):
        self.assertEqual([], route.read_outcomes())
        (self.temp / 'outcomes.jsonl').write_text('not json\n' + json.dumps(self.RECORDS[0]) + '\n[]\n{"x": 1}\n', encoding='utf-8')
        self.assertEqual([1], [r['number'] for r in route.read_outcomes()])


class Outcomes(Fixtures):
    """record_outcome: what a loop leaves behind while its checkout still exists."""

    def setUp(self):
        super().setUp()
        self.checkout = self.temp / 'repo-issue-5'
        self.state = self.checkout / '.workbench/state'
        self.state.mkdir(parents=True)
        self.claude_home = self.temp / 'claude-home'
        old = os.environ.get('CLAUDE_CONFIG_DIR')
        os.environ['CLAUDE_CONFIG_DIR'] = str(self.claude_home)
        self.addCleanup(lambda: os.environ.pop('CLAUDE_CONFIG_DIR', None) if old is None else os.environ.__setitem__('CLAUDE_CONFIG_DIR', old))
        (self.state / 'implementer.json').write_text(json.dumps(
            {'tool': 'claude', 'model': 'claude-sonnet-5-5', 'rosterId': 'claude-sonnet'}), encoding='utf-8')
        (self.state / 'claude.json').write_text(json.dumps({'issue': 'o/r#5', 'sessionId': 'x'}), encoding='utf-8')
        (self.state / 'launch.log').write_text('2026-10-05T10:00:00 config starting\n2026-10-05T10:00:01 step x\n', encoding='utf-8')
        (self.state / 'review-rounds.json').write_text(json.dumps([
            {'round': 1, 'severe': 2, 'decision': 'continue'}, {'round': 2, 'severe': 0, 'decision': 'stop'}]), encoding='utf-8')
        self.issue['labels'] = [{'name': 'priority:P2'}, {'name': 'bug'}]
        self.issue_pr = {'additions': 60, 'deletions': 90}

    def gh(self, *args, timeout=120):
        if args[1].startswith('repos/o/r/pulls/'):
            return done(json.dumps(self.issue_pr))
        return super().gh(*args, timeout=timeout)

    def transcript(self, directory, *entries):
        folder = self.claude_home / 'projects' / directory
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'a.jsonl').write_text('\n'.join(json.dumps(e) for e in entries) + '\n', encoding='utf-8')

    def slug(self):
        return __import__('re').sub(r'[^A-Za-z0-9]', '-', str(self.checkout.resolve()))

    def record(self, outcome='merged', **kwargs):
        return route.record_outcome(self.checkout, outcome, settings={}, gh=self.gh, now=1790000000 + 3600 * 3, **kwargs)

    def test_the_record_holds_the_roster_entry_rounds_majors_labels_and_size(self):
        got = self.record(pr=9)
        self.assertEqual(('o/r', 5, 'claude-sonnet', 'claude', 'claude-sonnet-5-5', 'merged'),
                         tuple(got[k] for k in ('repo', 'number', 'rosterId', 'tool', 'model', 'outcome')))
        self.assertEqual((2, 2, ['priority:P2', 'bug'], 'P2', 150, 'medium'),
                         (got['reviewRounds'], got['majors'], got['labels'], got['priority'], got['changedLines'], got['sizeBucket']))
        self.assertEqual([got['number']], [r['number'] for r in route.read_outcomes()])

    def test_it_is_once_per_issue(self):
        self.assertIsNotNone(self.record())
        self.assertIsNone(self.record())
        self.assertEqual(1, len(route.read_outcomes()))

    def test_wall_time_runs_from_the_first_launch_log_line(self):
        start = __import__('time').mktime(__import__('time').strptime('2026-10-05T10:00:00', '%Y-%m-%dT%H:%M:%S'))
        got = route.record_outcome(self.checkout, 'merged', settings={}, gh=self.gh, now=start + 5400)
        self.assertEqual(5400, got['wallSeconds'])

    def test_a_closed_loop_is_recorded_as_closed_and_an_unsized_one_has_no_bucket(self):
        got = self.record('closed')
        self.assertEqual(('closed', None, None), (got['outcome'], got['changedLines'], got['sizeBucket']))
        self.assertEqual('closed', route.read_outcomes()[0]['outcome'])

    def test_a_loop_outside_the_roster_or_without_an_issue_is_not_recorded(self):
        (self.state / 'implementer.json').write_text(json.dumps({'tool': 'codex'}), encoding='utf-8')   # no codex entry without a model
        self.assertIsNone(self.record())
        (self.state / 'implementer.json').write_text(json.dumps({'tool': 'kimi'}), encoding='utf-8')    # matches the kimi entry
        self.assertEqual('kimi', self.record()['rosterId'])
        (self.state / 'claude.json').unlink()
        self.assertIsNone(route.record_outcome(self.checkout, 'merged', settings={}, gh=self.gh, path=self.temp / 'other.jsonl'))
        self.assertFalse((self.temp / 'other.jsonl').exists())

    def test_it_never_raises_on_a_broken_checkout_or_roster(self):
        self.assertIsNone(route.record_outcome(self.temp / 'nothing-here', 'merged', settings={}, gh=self.gh))
        self.assertIsNone(route.record_outcome(self.checkout, 'merged', settings={'implementerRoster': []}, gh=self.gh))

    def test_claude_tokens_are_summed_once_per_message_over_this_checkouts_transcripts(self):
        usage = lambda out, cache: {'output_tokens': out, 'cache_read_input_tokens': cache}
        self.transcript(self.slug(),
                        {'message': {'id': 'm1', 'usage': usage(100, 1000)}, 'requestId': 'r1'},
                        {'message': {'id': 'm1', 'usage': usage(100, 1000)}, 'requestId': 'r1'},      # the same message again
                        {'message': {'id': 'm2', 'usage': usage(50, 500)}, 'requestId': 'r2'},
                        {'type': 'user', 'message': {'content': 'hi'}}, {'message': {'usage': {'output_tokens': 'x'}}})
        self.transcript(self.slug() + '--claude-worktrees-w', {'message': {'id': 'm3', 'usage': usage(7, 70)}, 'requestId': 'r3'})
        self.transcript(self.slug() + '0', {'message': {'id': 'm4', 'usage': usage(999, 999)}, 'requestId': 'r4'})   # issue-5 vs issue-50
        self.transcript('-somewhere-else', {'message': {'id': 'm5', 'usage': usage(999, 999)}, 'requestId': 'r5'})
        self.assertEqual((157, 1570), route.claude_tokens(self.checkout))
        got = self.record()
        self.assertEqual((157, 1570), (got['claudeOutputTokens'], got['claudeCacheReadTokens']))

    def test_no_transcripts_is_zero_tokens(self):
        self.assertEqual((0, 0), route.claude_tokens(self.checkout))


class Stats(Fixtures):
    RECORDS = PastOutcomes.RECORDS

    def test_the_table_has_a_row_per_roster_id_and_says_what_the_tokens_are(self):
        text = route.stats_table(self.RECORDS)
        lines = text.splitlines()
        self.assertRegex(lines[0], r'^roster id\s+merged\s+not merged\s+mean rounds\s+Majors\s+mean wall\s+Claude out tokens\s+Claude cache reads$')
        row = next(line for line in lines if line.startswith('claude-sonnet'))
        self.assertRegex(row, r'1\s+1\s+3\s+3\s+1\.5 h\s+300k\s+90\.0M')
        self.assertTrue(next(line for line in lines if line.startswith('kimi')).split()[-1] == '-')
        self.assertIn('planner', text)
        self.assertIn('3 loop(s)', text)

    def test_an_empty_history_says_so(self):
        self.assertIn('no outcomes recorded yet', route.stats_table([]))

    def test_the_command_reads_the_outcomes_file_and_filters_by_repo(self):
        (self.temp / 'outcomes.jsonl').write_text('\n'.join(json.dumps(r) for r in self.RECORDS + [
            dict(self.RECORDS[2], repo='x/y', rosterId='codex-sol')]) + '\n', encoding='utf-8')
        env = dict(os.environ, AGWORKBENCH_ROUTE_OUTCOMES=str(self.temp / 'outcomes.jsonl'))
        out = subprocess.run([sys.executable, str(ROOT / 'lib/wb.py'), 'route-stats', '--repo', 'o/r', '--json'],
                             capture_output=True, text=True, env=env)
        self.assertEqual(0, out.returncode, out.stderr)
        self.assertEqual({'claude-sonnet', 'kimi'}, set(json.loads(out.stdout)))
        table = subprocess.run([sys.executable, str(ROOT / 'lib/wb.py'), 'route-stats'], capture_output=True, text=True, env=env)
        self.assertIn('codex-sol', table.stdout)


class CommandLine(Fixtures):
    def run_cli(self, *argv):
        out = subprocess.run([sys.executable, str(ROOT / 'lib/route.py'), *argv], capture_output=True, text=True,
                             env=dict(os.environ), timeout=60)
        return out

    def test_a_missing_repo_or_an_unreadable_config_is_a_clean_failure(self):
        self.assertNotEqual(0, self.run_cli('route-issue', '5').returncode)
        bad = self.temp / 'bad.json'
        bad.write_text('{', encoding='utf-8')
        out = self.run_cli('route-issue', '5', '--repo', 'o/r', '--config', str(bad))
        self.assertEqual(1, out.returncode)
        self.assertIn('route: cannot read', out.stderr)

    def test_wb_exposes_route_issue(self):
        out = subprocess.run([sys.executable, str(ROOT / 'lib/wb.py'), 'route-issue', '--help'], capture_output=True, text=True)
        self.assertEqual(0, out.returncode, out.stderr)
        self.assertIn('--repo', out.stdout)


if __name__ == '__main__':
    unittest.main()
