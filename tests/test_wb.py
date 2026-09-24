"""Helper workspace selection and inbox waiting; no live terminal calls or real sleeps."""

import contextlib
import io
import os
import shutil
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))
import agw
import hub
import wb


class QueueReports(unittest.TestCase):
    def setUp(self):
        import conductor
        self.q = conductor
        self.folder = Path(__file__).resolve().parent.parent / ('test wb queue ' + uuid.uuid4().hex)
        self.folder.mkdir()
        self.addCleanup(shutil.rmtree, self.folder)
        self.state = self.folder / '.workbench/state'
        self.state.mkdir(parents=True)
        self.loop = str(uuid.uuid4())
        self.q.atomic_json(self.state / 'queue-member.json', dict(queue=str(self.folder / 'queue.json'), repo='o/r', number=1))
        self.q.atomic_json(self.state / 'claude.json', dict(sessionId=self.loop))
        self.enterContext(patch.dict(os.environ, AI_HUB=str(self.folder / '.workbench'), CLAUDE_CODE_SESSION_ID=self.loop))
        self.enterContext(patch.object(agw, 'request', side_effect=AssertionError('terminal access')))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))

    def report(self, state, pr=None, reason=None):
        return wb.cmd_loop_state(wb.argparse.Namespace(state=state, pr=pr, reason=reason))

    def test_reports_keep_pr_increment_revision_and_need_no_terminal(self):
        self.assertEqual(0, self.report('pr-open', pr='https://github.com/o/r/pull/2'))
        self.assertEqual(0, self.report('blocked', reason='human answer'))
        self.assertEqual(0, self.report('resumed'))
        report = self.q.read_json(self.state / 'loop.json')
        self.assertEqual(3, report['rev'])
        self.assertEqual(self.loop, report['loopId'])
        self.assertEqual('https://github.com/o/r/pull/2', report['pr'])
        self.assertIsNone(report['reason'])
        self.assertEqual([], list(self.state.glob('*.tmp')))

    def test_invalid_state_url_reason_or_identity_does_not_publish(self):
        for state, pr, reason in [('bad', None, None), ('pr-open', None, None),
                                  ('pr-open', 'https://github.com/other/repo/pull/1', None), ('blocked', None, None)]:
            self.assertEqual(2, self.report(state, pr, reason))
        with patch.dict(os.environ, CLAUDE_CODE_SESSION_ID=str(uuid.uuid4())):
            self.assertEqual(2, self.report('blocked', reason='x'))
        self.assertFalse((self.state / 'loop.json').exists())

    def test_queue_instructions_are_at_each_decision_point(self):
        text = (Path(__file__).resolve().parent.parent / 'claude/commands/start-github-issue.md').read_text()
        for heading, next_heading, needle in [
            ('## Phase 2', '## Phase 3', 'loop-state blocked'),
            ('## Phase 4', '## Phase 5', 'loop-state blocked'),
            ('## Phase 6', '## Phase 7', 'loop-state pr-open --pr'),
            ('## Phase 7', '## Rules', 'loop-state blocked --reason "PR closed"'),
        ]:
            self.assertIn(needle, text.split(heading)[1].split(next_heading)[0])
        phase6 = text.split('## Phase 6')[1].split('## Phase 7')[0]
        self.assertIn('Do not open revdiff automatically', phase6)
        self.assertIn('Outside queue mode', phase6)
        self.assertIn('loop-state resumed', text)
        self.assertIn('loop-state blocked --reason "mail waiter configuration error"', text)


class HelperWorkspace(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.tree = self.enterContext(patch.object(agw, 'tree', return_value={
            'workspaces': [
                {'id': 'active-workspace', 'active': True,
                 'sessions': [{'id': 'active-session'}]},
                {'id': 'caller-workspace', 'sessions': [
                    {'id': 'caller-session', 'paneIds': ['caller-left', 'caller-right']},
                    {'id': 'unsplit-session'}]},
            ]}))
        self.request = self.enterContext(patch.object(agw, 'request', return_value='new-session'))
        self.stderr = io.StringIO()
        self.enterContext(contextlib.redirect_stderr(self.stderr))

    def test_both_helpers_use_callers_workspace_even_when_another_is_active(self):
        os.environ.update(AGWINTERM_PANE_ID='caller-right', AGWINTERM_SESSION_ID='active-session')
        for name, select in [('revmux', False), ('human-review', True)]:
            self.assertEqual('new-session', wb.open_session(name, Path('checkout'), 'command', select))
            args = self.request.call_args.kwargs['args']
            self.assertEqual('session.new', self.request.call_args.args[0])
            self.assertEqual('caller-workspace', args['workspace'])
            self.assertEqual(name, args['name'])
            self.assertEqual('checkout', args['cwd'])
            self.assertEqual('command', args['command'])
            self.assertEqual(not select, args.get('no-select', False))
        self.assertEqual('', self.stderr.getvalue())

    def test_session_id_is_used_when_pane_id_is_absent(self):
        os.environ['AGWINTERM_SESSION_ID'] = 'unsplit-session'
        wb.open_session('revmux', Path('checkout'), 'command', False)
        self.assertEqual('caller-workspace', self.request.call_args.kwargs['args']['workspace'])
        self.assertEqual('', self.stderr.getvalue())

    def test_unknown_or_missing_pane_omits_workspace_and_warns_once(self):
        for pane in ['', 'missing-pane']:
            os.environ['AGWINTERM_PANE_ID'] = pane
            self.stderr.seek(0)
            self.stderr.truncate()
            wb.open_session('revmux', Path('checkout'), 'command', False)
            self.assertNotIn('workspace', self.request.call_args.kwargs['args'])
            self.assertEqual(1, len(self.stderr.getvalue().splitlines()))
            self.assertIn('workspace', self.stderr.getvalue())


class WaitMail(unittest.TestCase):
    def setUp(self):
        self.folder = Path(__file__).resolve().parent.parent / ('test wb ' + uuid.uuid4().hex)
        self.folder.mkdir()
        self.addCleanup(shutil.rmtree, self.folder)
        self.addCleanup(hub.reload_paths)
        self.enterContext(patch.dict(os.environ, {'AI_HUB': str(self.folder), 'AI_BOX': 'claude'}, clear=True))
        hub.reload_paths()
        self.t = 0.0
        self.sleeps = []
        self.on_sleep = lambda: None
        self.enterContext(patch.object(wb, 'now', lambda: self.t))
        self.enterContext(patch.object(wb, 'pause', self.advance))
        self.enterContext(patch.object(agw, 'request', side_effect=AssertionError('terminal request')))
        self.enterContext(patch.object(agw, 'tree', side_effect=AssertionError('terminal tree lookup')))
        self.enterContext(patch.object(wb.subprocess, 'run', side_effect=AssertionError('subprocess run')))
        self.enterContext(patch.object(wb.subprocess, 'Popen', side_effect=AssertionError('subprocess Popen')))

    def advance(self, seconds):
        self.assertGreater(seconds, 0)
        self.sleeps.append(seconds)
        self.t += seconds
        self.on_sleep()

    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', ['wb', 'wait-mail', *args]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = wb.main()
            except SystemExit as exit:
                code = exit.code
        return code, out.getvalue(), err.getvalue()

    def post(self, box='claude', subject='review ready'):
        return hub.write_message(to=box, sender='codex', subject=subject, body='private message body')

    def files(self):
        return {path.relative_to(self.folder): path.read_bytes()
                for path in self.folder.rglob('*') if path.is_file()}

    def test_unread_at_start_returns_every_message_immediately_without_consuming_it(self):
        paths = [self.post(subject='first review'), self.post(subject='second review')]
        before = self.files()
        code, output, error = self.invoke()
        self.assertEqual(0, code)
        self.assertEqual('', error)
        self.assertEqual(2, len(output.splitlines()))
        for path, subject in zip(paths, ['first review', 'second review']):
            self.assertIn(f'NEW MAIL: {path.stem} codex {subject}\n', output)
        self.assertNotIn('private message body', output)
        self.assertEqual([], self.sleeps)
        self.assertEqual(before, self.files())

    def test_cp1252_output_replaces_unicode_subject_without_losing_wakeup(self):
        subject = 'review ? \U0001f600 \u0436 \u2192'
        path = self.post(subject=subject)
        stdout_bytes, stderr_bytes = io.BytesIO(), io.BytesIO()
        with io.TextIOWrapper(stdout_bytes, encoding='cp1252', errors='strict') as out, \
                io.TextIOWrapper(stderr_bytes, encoding='cp1252', errors='strict') as err, \
                patch.object(sys, 'argv', ['wb', 'wait-mail', '--timeout', '0']), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(0, wb.main())
            out.flush()
            err.flush()
            self.assertEqual([f'NEW MAIL: {path.stem} codex review ? ? ? ?'],
                             stdout_bytes.getvalue().decode('cp1252').splitlines())
            self.assertEqual(b'', stderr_bytes.getvalue())
        self.assertEqual(subject, hub.parse_message(path)['subject'])

    def test_cp1252_stderr_can_report_invalid_unicode_box(self):
        stderr_bytes = io.BytesIO()
        with io.TextIOWrapper(stderr_bytes, encoding='cp1252', errors='strict') as err, \
                patch.object(sys, 'argv', ['wb', 'wait-mail', '--box', '\u0436']), \
                contextlib.redirect_stderr(err):
            self.assertEqual(2, wb.main())
            err.flush()
            self.assertIn('bad box name', stderr_bytes.getvalue().decode('cp1252'))

    def test_interval_and_timeout_upper_bounds_are_inclusive(self):
        self.post()  # All accepted inputs return immediately, even before the bounds fix.
        self.assertEqual(0, self.invoke('--interval', '3600', '--timeout', '1440')[0])
        for option, values in [('interval', ['3600.001', '1e308']), ('timeout', ['1440.001', '1e308'])]:
            for value in values:
                with self.subTest(option=option, value=value):
                    code, output, error = self.invoke(f'--{option}={value}')
                    self.assertEqual(2, code)
                    self.assertEqual('', output)
                    self.assertIn(f'--{option}', error)
        self.assertEqual([], self.sleeps)

    def test_read_and_archived_messages_do_not_wake_but_later_unread_mail_does(self):
        read = self.post(subject='already read')
        archived = self.post(subject='archived')
        hub.mark_read(read)
        archived.rename(archived.parent / 'archive' / archived.name)
        posted = []
        self.on_sleep = lambda: posted.append(self.post(subject='new reply'))
        code, output, error = self.invoke('--timeout', '1', '--interval', '2')
        self.assertEqual(0, code)
        self.assertEqual('', error)
        self.assertEqual([2], self.sleeps)
        self.assertEqual(f'NEW MAIL: {posted[0].stem} codex new reply\n', output)
        self.assertTrue(posted[0].is_file())

    def test_timeout_clamps_the_last_sleep_and_does_not_create_a_missing_box(self):
        code, output, error = self.invoke('--timeout', '0.25', '--interval', '10')
        self.assertEqual(3, code)
        self.assertEqual('', error)
        self.assertEqual('no new mail in 0.25 minutes\n', output)
        self.assertEqual(2, len(self.sleeps))
        self.assertEqual(10, self.sleeps[0])
        self.assertAlmostEqual(5, self.sleeps[1])
        self.assertAlmostEqual(15, self.t)
        self.assertEqual([], list(self.folder.iterdir()))

    def test_default_timeout_is_55_minutes_and_default_interval_is_10_seconds(self):
        code, output, error = self.invoke()
        self.assertEqual(3, code)
        self.assertEqual('', error)
        self.assertEqual('no new mail in 55 minutes\n', output)
        self.assertAlmostEqual(3300, self.t)
        self.assertTrue(all(0 < seconds <= 10 for seconds in self.sleeps))
        self.assertEqual(10, self.sleeps[0])

    def test_zero_timeout_checks_exactly_once_with_and_without_mail(self):
        for present in [False, True]:
            if present:
                self.post()
            with patch.object(hub, 'unread', wraps=hub.unread) as unread:
                code, _, _ = self.invoke('--timeout', '0')
            self.assertEqual(0 if present else 3, code)
            unread.assert_called_once_with('claude')
            self.assertEqual([], self.sleeps)

    def test_reply_between_timed_out_waiter_and_replacement_is_not_lost(self):
        code, _, _ = self.invoke('--timeout', '0.1')
        self.assertEqual(3, code)
        path = self.post()
        sleeps = list(self.sleeps)
        code, output, _ = self.invoke('--timeout', '0')
        self.assertEqual(0, code)
        self.assertIn(path.stem, output)
        self.assertEqual(sleeps, self.sleeps)

    def test_box_precedence_and_registry_pane_fallback(self):
        paths = {box: self.post(box) for box in ['claude', 'codex', 'reviewer']}
        hub.save_registry({'agents': {'reviewer': {'box': 'reviewer', 'pane': 'registered-pane'},
                                      'codex': {'box': 'codex', 'pane': 'other-pane'}}})
        cases = [
            (['--box', 'codex'], {'AI_BOX': 'claude', 'AGWINTERM_PANE_ID': 'registered-pane'}, 'codex'),
            ([], {'AI_BOX': 'claude', 'AGWINTERM_PANE_ID': 'registered-pane'}, 'claude'),
            ([], {'AGWINTERM_PANE_ID': 'registered-pane', 'AGWINTERM_SESSION_ID': 'other-pane'}, 'reviewer'),
            ([], {'AGWINTERM_SESSION_ID': 'registered-pane'}, 'reviewer'),
            ([], {'AGWINTERM_PANE_ID': 'unknown-pane'}, 'claude'),
            ([], {}, 'claude'),
        ]
        for args, env, expected in cases:
            with self.subTest(env=env, args=args), \
                    patch.dict(os.environ, {'AI_HUB': str(self.folder), **env}, clear=True):
                code, output, error = self.invoke(*args, '--timeout', '0')
                self.assertEqual(0, code)
                self.assertEqual('', error)
                self.assertEqual(f'NEW MAIL: {paths[expected].stem} codex review ready\n', output)
        self.assertEqual([], self.sleeps)

    def test_environment_hub_is_reloaded_before_scanning(self):
        wrong = self.folder / 'old-hub'
        wrong.mkdir()
        with patch.dict(os.environ, {'AI_HUB': str(wrong)}):
            hub.reload_paths()
            self.post(subject='wrong hub')
        code, output, error = self.invoke('--timeout', '0')
        self.assertEqual(3, code)
        self.assertEqual('', error)
        self.assertNotIn('wrong hub', output)
        self.assertEqual(self.folder, hub.HUB)

    def test_missing_nonexistent_or_file_hub_is_an_error(self):
        regular_file = self.folder / 'not-a-directory'
        regular_file.write_text('file', encoding='utf-8')
        for root in ['', str(self.folder / 'missing'), str(regular_file)]:
            with self.subTest(root=root), patch.dict(os.environ, {'AI_HUB': root}):
                code, output, error = self.invoke('--timeout', '0')
                self.assertEqual(2, code)
                self.assertEqual('', output)
                self.assertIn('AI_HUB', error)
        self.assertEqual([], self.sleeps)

    def test_invalid_box_names_are_errors_instead_of_falling_back(self):
        for box in ['../outside', 'Bad Box', '']:
            with self.subTest(box=box):
                code, output, error = self.invoke('--box', box, '--timeout', '0')
                self.assertEqual(2, code)
                self.assertEqual('', output)
                self.assertIn('bad box name', error)
        with patch.dict(os.environ, {'AI_BOX': '../outside'}):
            self.assertEqual(2, self.invoke('--timeout', '0')[0])
        self.assertEqual([], self.sleeps)

    def test_invalid_intervals_and_timeouts_fail_without_sleeping(self):
        for option, values in [('interval', ['0', '-1', 'nan', 'inf', '-inf', 'invalid']),
                               ('timeout', ['-1', 'nan', 'inf', '-inf', 'invalid'])]:
            for value in values:
                with self.subTest(option=option, value=value):
                    code, output, error = self.invoke(f'--{option}={value}')
                    self.assertEqual(2, code)
                    self.assertEqual('', output)
                    self.assertIn(f'--{option}', error)
        self.assertEqual([], self.sleeps)

    def test_file_read_by_another_agent_between_listing_and_parse_is_skipped(self):
        path = self.post()
        parse = hub.parse_message

        def read_elsewhere(candidate):
            hub.mark_read(candidate)
            return parse(candidate)  # FileNotFoundError from the path that was listed.

        with patch.object(hub, 'parse_message', side_effect=read_elsewhere):
            code, output, error = self.invoke('--timeout', '0')
        self.assertEqual(3, code)
        self.assertEqual('', error)
        self.assertNotIn('NEW MAIL', output)
        self.assertTrue((path.parent / 'read' / path.name).is_file())


if __name__ == '__main__':
    unittest.main()


class RevmuxProfile(unittest.TestCase):
    """#20: the review round's profile follows the implementer the launcher saved for the checkout."""

    def setUp(self):
        self.folder = Path(__file__).resolve().parent.parent / ('test wb revmux ' + uuid.uuid4().hex)
        (self.folder / '.workbench/state').mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.folder)
        (self.folder / 'scope.md').write_text('scope', encoding='utf-8')
        self.enterContext(patch.dict(os.environ, {'AI_HUB': str(self.folder / '.workbench')}))
        self.opened = self.enterContext(patch.object(wb, 'open_session', return_value='sid'))
        self.enterContext(patch.object(wb, 'issue_number', return_value='20'))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def run_round(self, *extra):
        with patch.object(sys, 'argv', ['wb.py', 'revmux', '--round', '1', '--scope', 'scope.md', *extra]):
            self.assertEqual(0, wb.main())
        return self.opened.call_args.args[2]

    def save(self, text):
        (self.folder / '.workbench/state/implementer.json').write_text(text, encoding='utf-8')

    def test_no_saved_implementer_keeps_comprehensive(self):
        self.assertIn("-Profile 'comprehensive'", self.run_round())

    def test_claude_implementer_uses_the_saved_claude_only_profile(self):
        self.save('{"tool": "claude", "revmuxProfile": "claude-only"}')
        self.assertIn("-Profile 'claude-only'", self.run_round())

    def test_explicit_profile_wins(self):
        self.save('{"tool": "claude", "revmuxProfile": "claude-only"}')
        self.assertIn("-Profile 'codex-final'", self.run_round('--profile', 'codex-final'))

    def test_unreadable_or_unsafe_saved_profile_falls_back(self):
        for text in ('not json', '{"revmuxProfile": "x\' ; calc"}', '[]'):
            with self.subTest(text=text):
                self.save(text)
                self.assertIn("-Profile 'comprehensive'", self.run_round())
